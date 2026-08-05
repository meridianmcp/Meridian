"""Compatibility shim (extraction: 2b2433ca).

The ``CodeIndex`` implementation (tree-sitter/ast semantic chunking,
Merkle-incremental reindex, DuckDB FTS hybrid BM25/VSS search) moved OUT of the
Meridian package into the detachable, standalone-installable
``extensions/meridian-codeindex`` sub-package (see its README) -- the same
"real independent tool, Meridian is just one caller" relationship Meridian
already has with codebase-memory-mcp, rather than a Meridian feature wearing a
native-sounding name. It is a deliberate fallback layer for when Serena and/or
codebase-memory-mcp are unavailable or misbehaving, so it carries zero
dependency on either.

This shim re-exports the full ``meridian_codeindex.code_index`` namespace so
existing ``meridian.code_index`` / ``from ..code_index import X`` importers
(``meridian/mcp/handler.py``, ``meridian/prospect.py``, tests) keep working
unchanged, and re-implements ONLY ``search_code_semantic`` -- the one piece of
behavior that is genuinely Meridian-deployment-specific: the hosted-mode guard
(workspace decision 0dedff91 / fix 90c593d). ``search_code_semantic`` reads
``root_dir`` off the LOCAL filesystem of whatever process runs it; on HOSTED
Meridian that's the server, which can never reach a caller's own machine, so
this fails honestly here instead of delegating to a function that would
silently mis-resolve the path against the server's own filesystem. (This guard
is statically enforced by ``tests/test_no_local_fs_access.py``, which AST-scans
this exact function for both the filesystem sink and the guard token.)

New code should import ``meridian_codeindex`` directly; it needs no Meridian
involvement at all and also exposes a ``meridian-codeindex`` CLI.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from meridian_codeindex import code_index as _impl

# Re-export EVERYTHING (public + private helpers, not dunders) so no caller
# breaks -- CodeIndex, CodeChunk, MerkleTree, chunk_file, get_code_index,
# normalize_root_dir, reindex_at_checkpoint, _vectors_enabled, etc.
globals().update({k: v for k, v in vars(_impl).items() if not k.startswith("__")})

del _impl


def search_code_semantic(
    root_dir: str,
    query: str,
    *,
    limit: int = 10,
    kind: str | None = None,
    db_path: str = ":memory:",
    reindex: bool = True,
) -> dict[str, Any]:
    """Meridian's thin caller over the extracted ``meridian_codeindex`` package.

    Workspace decision 0dedff91 (2026-07-12) -- this function reads root_dir
    off the LOCAL filesystem of whatever process is running it. On hosted
    Meridian that's the server, which can never reach a caller's own
    machine -- fail honestly here instead of letting the underlying package
    silently mis-resolve a Windows path against the server's own cwd (the
    original bug this guard was written to close for good). All the actual
    chunking/reindex/search work below is the extracted package's
    (``normalize_root_dir`` / ``get_code_index`` / ``_vectors_enabled``,
    re-exported above); this wrapper adds nothing but the guard + orchestration.
    """
    from meridian_codeindex import code_index as impl

    if os.environ.get("MERIDIAN_HOSTED", "").lower() in ("1", "true", "yes"):
        return {
            "root_dir": root_dir, "query": query, "hits": [],
            "total_indexed": 0, "vectors_enabled": impl._vectors_enabled(),
            "error": (
                "search_code_semantic requires access to YOUR local filesystem "
                "and cannot run on hosted Meridian -- the server has no access "
                "to your machine. Use the tunnel-routed equivalent instead "
                "(e.g. codebase__search_graph, extractor__find_symbol, or a "
                "meridian-docs/desktop-commander tool)."
            ),
        }
    # a0cf71ef — normalize (unquote / expanduser / abspath) so a valid local dir
    # handed to us in a quoted or ~-prefixed shape resolves; report the resolved
    # path back so the caller sees exactly what was searched. "does not exist" is
    # then returned ONLY when the normalized path truly is not a directory.
    root_dir = impl.normalize_root_dir(root_dir)
    result: dict[str, Any] = {
        "root_dir": root_dir,
        "query": query,
        "hits": [],
        "total_indexed": 0,
        "vectors_enabled": impl._vectors_enabled(),
    }
    if not query or not str(query).strip():
        result["error"] = "query is required"
        return result
    if not root_dir or not os.path.isdir(root_dir):
        result["error"] = f"root_dir does not exist: {root_dir}"
        return result
    idx = impl.get_code_index(root_dir, db_path=db_path)
    if reindex:
        idx.reindex()
    result["total_indexed"] = idx.count()
    result["vectors_active"] = idx._vss_ready
    result["hits"] = idx.search(query, limit=limit, kind=kind)
    return result


# ---------------------------------------------------------------------------
# c95d0c12 — bound codebase-memory reindex to the active repository and
# exclude nested worktrees.
#
# codebase-memory's index_repository tool takes no include/exclude parameter
# -- the only lever Meridian (or an agent following its guidance) has is
# WHICH repo_path to hand it, and whether to trust the response. Reproduced
# 2026-08-05: this repo nests 138 .claude/worktrees + .codex/worktrees
# copies of itself; a full-root index_repository call returned a hosted 502,
# while a narrow index of just meridian/ succeeded (4,570 nodes, 21,405
# edges) -- proving the failure is scope/volume-related, not a universal
# indexer failure. These two helpers give a caller (agent or Meridian's own
# guidance) a cheap, top-level-only pre-flight check and an explicit
# success/failure classifier, so a 502 (or any other error-shaped response)
# is never silently treated as a current index.
# ---------------------------------------------------------------------------

_DEFAULT_EXCLUDED_DIR_NAMES = frozenset({
    ".git", ".pixi", ".venv", "venv", "node_modules", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", "dist", "build",
    ".codebase-memory",
})

_WORKTREE_CONTAINER_DIR_NAMES = frozenset({".claude", ".codex"})


def compute_bounded_reindex_scope(
    repo_path: str,
    *,
    worktree_threshold: int = 5,
) -> dict[str, Any]:
    """Pre-flight scope check for an index_repository(repo_path=...) call.

    Non-recursive top-level scan only (cheap, no full walk): looks for
    ``.claude/worktrees`` / ``.codex/worktrees`` containers directly under
    *repo_path* and counts the worktree copies inside each, plus any of the
    usual cache/VCS directories that should never be indexed as source.

    Returns ``{repo_path, excluded_paths, nested_worktree_count, safe,
    recommended_repo_path}``. ``safe`` is False once the nested worktree
    count exceeds *worktree_threshold*; when unsafe, ``recommended_repo_path``
    falls back to a conventional source subdirectory (the repo's own package
    dir, else ``meridian``) instead of the bloated root -- the same narrow
    scope confirmed to succeed in the 2026-08-05 reproduction. This function
    never raises and never itself calls index_repository; it only advises.
    """
    root = Path(repo_path)
    excluded_paths: list[str] = []
    nested_worktree_count = 0

    if root.is_dir():
        for container_name in sorted(_WORKTREE_CONTAINER_DIR_NAMES):
            container = root / container_name / "worktrees"
            if not container.is_dir():
                continue
            excluded_paths.append(str(container))
            try:
                nested_worktree_count += sum(1 for p in container.iterdir() if p.is_dir())
            except OSError:
                pass
        for name in sorted(_DEFAULT_EXCLUDED_DIR_NAMES):
            candidate = root / name
            if candidate.is_dir():
                excluded_paths.append(str(candidate))

    safe = nested_worktree_count <= worktree_threshold
    recommended_repo_path = repo_path
    if not safe:
        for candidate_name in (root.name, "meridian"):
            candidate = root / candidate_name
            if candidate.is_dir() and candidate != root:
                recommended_repo_path = str(candidate)
                break

    return {
        "repo_path": repo_path,
        "excluded_paths": excluded_paths,
        "nested_worktree_count": nested_worktree_count,
        "safe": safe,
        "recommended_repo_path": recommended_repo_path,
    }


def is_index_repository_failure(result: "dict[str, Any] | None") -> bool:
    """True when an index_repository response must NOT be treated as a
    successful/current index (c95d0c12).

    A missing result, a non-dict result, or a dict carrying a truthy
    ``error`` field are all failures. This is a fail-SAFE classifier for the
    specific gap this item closes (a 502/error response silently treated as
    a fresh index) -- it is not a schema validator for the success shape.
    """
    if not result or not isinstance(result, dict):
        return True
    return bool(result.get("error"))
