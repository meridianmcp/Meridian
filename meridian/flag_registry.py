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

8ca89e8f — flag-to-section drift check (the unbuilt half of workspace proposal
8d8bbe63). This module stays the pure, dependency-free scanning half; the
DURABLE storage of a "this docx section's numbers were produced with flag=value"
link lives in :class:`meridian.doc_store.DocStructureStore` (a
``doc_flag_links`` table, alongside the store's other self-contained
doc_documents/doc_elements/doc_figures/... tables — see that module for why),
keyed by stable ``doc_elements`` id exactly like the existing figure-caption
linkage. This module adds only the STATELESS comparison: given a recorded link
(carrying the flag's default AT RECORD TIME) and a fresh scan of the CURRENT
codebase, decide whether that link has drifted. :func:`diff_flag_links` is the
pure comparison (no DB, no filesystem — unit-testable with two plain lists);
:func:`check_flag_drift` is the convenience wrapper that also runs the scan.
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


# ---------------------------------------------------------------------------
# 8ca89e8f — flag-to-section drift check
# ---------------------------------------------------------------------------
#
# A "link" here is the durable record produced by
# ``DocStructureStore.link_flag_state`` (a ``doc_flag_links`` row, materialised
# as a plain dict): the claim "this doc_elements id's underlying numbers were
# produced with flag_name=recorded_value, and at record time the codebase's
# default for that flag was recorded_default". The two functions below never
# touch a DB or a store — they operate purely on the dict shape that store
# produces, so they are unit-testable (and independently useful) without any
# of that machinery running.
#
# Expected link shape (extra keys are ignored, so a full doc_flag_links row —
# id/project_id/document_id/created_at/etc — can be passed through untouched):
#   {"flag_name": str, "recorded_default": Any,
#    "element_id": str?, "source_file": str?, "source_line": int?, ...}


def dedupe_flag_links(links: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse a link history down to the MOST RECENT link per (element_id,
    flag_name) pair.

    ``link_flag_state`` is insert-only (mirrors the repo's append-only
    provenance convention — see DECISIONS.md/task_log): a section can be
    re-verified multiple times as flags change, and each recording is a fact
    about a point in time, not a single mutable "current state". Drift
    checking only cares about the LATEST claim for a given (element, flag)
    pair, so callers run their link history through this before
    :func:`diff_flag_links`.

    Recency is compared on ``(created_at, seq)``: ``created_at`` (ISO-8601
    strings sort correctly as plain strings) first, then ``seq`` — the
    process-local monotonic counter ``DocStructureStore.link_flag_state``
    stamps on every row — as a tiebreaker. The tiebreaker matters in practice:
    two links recorded back-to-back for the same (element, flag) pair can
    legitimately land on the IDENTICAL ``created_at`` (timestamp
    resolution/OS clock granularity is coarser than "two sequential DB
    inserts" on some platforms), and without ``seq`` there would be nothing
    left to break the tie correctly (a link's ``id`` is a random UUID with no
    relationship to insertion order). A link missing ``created_at``/``seq``
    (e.g. a hand-built test dict) sorts as the oldest possible value on that
    field so it never masks a properly-dated/sequenced sibling. Never raises.
    """
    latest: dict[tuple[Any, Any], dict[str, Any]] = {}
    for link in links or []:
        key = (link.get("element_id"), link.get("flag_name"))
        existing = latest.get(key)
        if existing is None:
            latest[key] = link
            continue
        link_rank = (link.get("created_at") or "", link.get("seq") or 0)
        existing_rank = (existing.get("created_at") or "", existing.get("seq") or 0)
        if link_rank > existing_rank:
            latest[key] = link
    return list(latest.values())


def diff_flag_links(
    links: list[dict[str, Any]],
    current_flags: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Pure drift comparison: recorded flag links vs. a fresh registry scan.

    ``current_flags`` is the ``flags`` list from :func:`get_flag_registry` /
    :func:`scan_env_flags` (``{flag_name, file, line, default}`` dicts).
    Returns one result dict per input link — every key from the input link is
    preserved (spread first) plus:

        {"current_default": Any, "current_call_sites": int,
         "status": "ok" | "drifted" | "removed"}

    Matching a link to call sites: by ``flag_name`` first; when the link also
    carries ``source_file``/``source_line`` (the call site recorded at link
    time), candidates are further narrowed to that EXACT call site — a flag
    name can legitimately be read at more than one call site with different
    defaults, and pinning to the recorded site avoids a false "drifted" from
    an unrelated read of a same-named flag elsewhere. If the pinned call site
    itself is gone (the line moved / file changed) the match falls back to
    every same-named call site, so a mere line-shift doesn't masquerade as
    ``removed``.

    Status:

    * ``"removed"`` — no current call site matches at all (name, or the
      pinned file/line) — the strongest staleness signal: the flag (or this
      specific read of it) isn't even in the codebase anymore.
    * ``"drifted"`` — at least one matching call site exists, but none of
      their current defaults equal ``recorded_default`` — the flag's default
      behaviour changed since this section's numbers were produced.
    * ``"ok"`` — a matching call site's current default equals
      ``recorded_default``. Does NOT prove the section is still correct —
      only that this specific check found no evidence of drift.

    Never raises; a link with no (or non-string) ``flag_name`` is silently
    skipped (not surfaced in the result — there is nothing to diff).
    """
    by_name: dict[str, list[dict[str, Any]]] = {}
    for f in current_flags or []:
        name = f.get("flag_name")
        if isinstance(name, str):
            by_name.setdefault(name, []).append(f)

    results: list[dict[str, Any]] = []
    for link in links or []:
        flag_name = link.get("flag_name")
        if not isinstance(flag_name, str) or not flag_name:
            continue
        candidates = by_name.get(flag_name, [])
        source_file = link.get("source_file")
        source_line = link.get("source_line")
        if source_file is not None and source_line is not None:
            pinned = [
                c for c in candidates
                if c.get("file") == source_file and c.get("line") == source_line
            ]
            matches = pinned if pinned else candidates
        else:
            matches = candidates

        if not matches:
            results.append({
                **link,
                "current_default": None,
                "current_call_sites": 0,
                "status": "removed",
            })
            continue

        current_defaults = [m.get("default") for m in matches]
        drifted = link.get("recorded_default") not in current_defaults
        results.append({
            **link,
            "current_default": (
                current_defaults[0] if len(current_defaults) == 1
                else current_defaults
            ),
            "current_call_sites": len(matches),
            "status": "drifted" if drifted else "ok",
        })
    return results


def check_flag_drift(
    links: list[dict[str, Any]],
    repo_root: str | None = None,
) -> list[dict[str, Any]]:
    """Convenience wrapper: scan *repo_root* fresh, then :func:`diff_flag_links`.

    This is the implementation behind the ``get_flag_drift`` MCP tool (it is
    also a plain importable entry point, same contract as
    :func:`get_flag_registry`, whose ``repo_root`` normalisation — default to
    ``os.getcwd()``, tolerate an accidentally-quoted path — it reuses
    unchanged). Callers that already have a fresh registry scan (e.g. to avoid
    re-walking a large tree for multiple checks) should call
    :func:`diff_flag_links` directly instead.
    """
    registry = get_flag_registry(repo_root)
    return diff_flag_links(links, registry["flags"])
