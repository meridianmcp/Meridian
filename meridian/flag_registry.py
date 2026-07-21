"""45802b67 — flag registry: scan ``os.environ.get()`` / ``os.getenv()`` call
sites across a source tree into a flat inventory of {flag_name, file, line,
default}.

Motivated by real config-drift pain across multi-repo Meridian deployments
(dozens of ``os.environ.get("SOME_FLAG", ...)`` reads scattered through the
codebase with no single place listing what flags exist, where they're read,
or what their defaults are). This module is a standalone, dependency-free
AST scanner — no DB, no MCP round-trip required to use it as a library — so
it doubles as the implementation behind the ``get_flag_registry`` MCP tool
(wired in ``meridian/mcp/handler.py``) and as a plain importable utility.

Design notes:

* AST-based (``ast.parse`` + ``ast.walk``), not regex. A regex scanner would
  either over-match (comments, strings, ``foo.os.environ.get`` typos) or
  under-match multi-line calls; AST gives exact call-site identification and
  free syntax-error resilience (a file that fails to parse is skipped, not
  fatal to the whole scan).
* Only two call shapes are recognised, matching the item's spec: ``os.environ.get(...)``
  and ``os.getenv(...)``. Both take the env var name as the first positional
  arg and an optional default as the second positional arg (or a ``default=``
  keyword, which this scanner also honours defensively).
* The flag name is extracted ONLY when the first argument is a string literal
  (``ast.Constant`` with a ``str`` value). Calls where the name is computed at
  runtime (a variable, an f-string, a concatenation, ...) are structurally
  unresolvable without executing the program, so they are skipped gracefully
  — never raise, never invent a name.
* The default is best-effort ``ast.literal_eval``'d out of the second
  argument's AST node. A non-literal default (e.g. a function call or a
  variable reference) evaluates to ``None`` — same graceful-skip philosophy,
  just for the default rather than the name.
"""
from __future__ import annotations

import ast
import os
from pathlib import Path
from typing import Any

# Directories that are never worth walking into: VCS internals, dependency
# trees, build artifacts, caches. Anything starting with "." is pruned too
# (see _iter_python_files), so ".git"/".venv" are covered by both rules —
# listed explicitly anyway for clarity and in case of a dotless mirror.
_DEFAULT_PRUNE_DIRS: frozenset[str] = frozenset({
    ".git", ".hg", ".svn", "__pycache__", "node_modules", "dist", "build",
    "venv", ".venv", "env", ".env", "site-packages", ".pixi", ".tox",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "egg-info",
})


def _literal_or_none(node: ast.AST | None) -> Any:
    """Best-effort ``ast.literal_eval`` of *node*; ``None`` on any failure."""
    if node is None:
        return None
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return None


def _matches_environ_get(func: ast.expr) -> bool:
    """True for the ``os.environ.get`` attribute-access shape."""
    return (
        isinstance(func, ast.Attribute) and func.attr == "get"
        and isinstance(func.value, ast.Attribute) and func.value.attr == "environ"
        and isinstance(func.value.value, ast.Name) and func.value.value.id == "os"
    )


def _matches_getenv(func: ast.expr) -> bool:
    """True for the ``os.getenv`` attribute-access shape."""
    return (
        isinstance(func, ast.Attribute) and func.attr == "getenv"
        and isinstance(func.value, ast.Name) and func.value.id == "os"
    )


def _extract_call(node: ast.Call) -> dict[str, Any] | None:
    """Extract ``{flag_name, line, default}`` from a qualifying Call node, or
    ``None`` if this call doesn't match the recognised shapes or its flag
    name isn't a string literal (dynamic — skipped gracefully)."""
    if not (_matches_environ_get(node.func) or _matches_getenv(node.func)):
        return None
    if not node.args:
        return None  # os.environ.get() with no args — malformed, not our concern
    name_node = node.args[0]
    if not (isinstance(name_node, ast.Constant) and isinstance(name_node.value, str)):
        return None  # dynamic flag name (variable, f-string, ...) — skip
    default_node = node.args[1] if len(node.args) > 1 else None
    if default_node is None:
        for kw in node.keywords:
            if kw.arg == "default":
                default_node = kw.value
                break
    return {
        "flag_name": name_node.value,
        "line": node.lineno,
        "default": _literal_or_none(default_node),
    }


def scan_file(path: str | Path) -> list[dict[str, Any]]:
    """Scan a single ``.py`` file for qualifying ``os.environ.get``/``os.getenv``
    call sites. Returns ``[]`` (never raises) for unreadable or unparsable
    files — a single bad file must not abort a whole-tree scan."""
    path = Path(path)
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    try:
        tree = ast.parse(source, filename=str(path))
    except (SyntaxError, ValueError):
        return []
    hits: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            hit = _extract_call(node)
            if hit is not None:
                hit["file"] = str(path)
                hits.append(hit)
    return hits


def _iter_python_files(root: Path, prune_dirs: frozenset[str]):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames
            if d not in prune_dirs and not d.startswith(".")
        ]
        for fn in filenames:
            if fn.endswith(".py"):
                yield Path(dirpath) / fn


def scan_env_flags(
    root: str | Path,
    *,
    extra_prune_dirs: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    """Walk *root* recursively and return every qualifying flag call site as
    ``{flag_name, file, line, default}``, sorted by (file, line) for stable
    output. Vendored/build/cache directories are pruned; a single unreadable
    or unparsable file is skipped rather than aborting the scan."""
    root_path = Path(root)
    prune = _DEFAULT_PRUNE_DIRS | set(extra_prune_dirs)
    results: list[dict[str, Any]] = []
    if root_path.is_file():
        # A single file was passed rather than a directory — scan just it.
        if root_path.suffix == ".py":
            results.extend(scan_file(root_path))
    else:
        for py_file in _iter_python_files(root_path, prune):
            results.extend(scan_file(py_file))
    results.sort(key=lambda r: (r["file"], r["line"]))
    return results


def get_flag_registry(repo_root: str | None = None) -> dict[str, Any]:
    """Build the full flag registry for *repo_root* (defaults to the current
    working directory — "the current project's repo root" per the MCP tool
    contract). This is the function wired up as the ``get_flag_registry`` MCP
    tool; it is also a plain importable entry point for scripts/tests.

    Returns::

        {"repo_root": str, "flags": [{"flag_name", "file", "line", "default"}, ...],
         "count": int, "unique_flag_names": [str, ...], "unique_count": int}
    """
    root = str(repo_root).strip() if repo_root else os.getcwd()
    # Defensive: tolerate an accidentally-quoted path (a common copy/paste
    # artifact from shell history), same normalization search_code_semantic's
    # dispatch does for root_dir.
    if len(root) >= 2 and root[0] == root[-1] and root[0] in ("'", '"'):
        root = root[1:-1]
    flags = scan_env_flags(root)
    unique_names = sorted({f["flag_name"] for f in flags})
    return {
        "repo_root": root,
        "flags": flags,
        "count": len(flags),
        "unique_flag_names": unique_names,
        "unique_count": len(unique_names),
    }
