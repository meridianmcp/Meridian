"""Hardened local BM25 secondary path (sprint item 58e64c86).

:mod:`meridian_codeindex.code_index` already gives this package a real,
zero-Meridian-dependency, incremental BM25 (+ optional vector) code index
(:class:`meridian_codeindex.code_index.CodeIndex`). This module is an
ADDITIVE layer on top of it -- it does not change ``CodeIndex``'s own
behavior or tests -- that closes three concrete gaps the "harden local
research indexing" item called out:

1. **Explicit walk-health state.** ``CodeIndex``'s own tree walk
   (``_iter_indexable_files``) uses a plain ``os.walk`` with no ``onerror``
   callback, so a permission-denied subdirectory (or any other per-directory
   ``OSError``) is silently pruned -- the caller sees a smaller-than-real
   index with no signal that anything was skipped. :func:`bm25_fallback_search`
   runs its own directory-only health walk (:func:`_safe_walk_errors`) over
   the SAME root first and reports ``inconclusive=True`` whenever that walk
   hit a real error, so an empty/short ``hits`` list is never silently read
   as an authoritative "no match" -- exactly the failure mode the item's
   acceptance criteria calls out by name.
2. **Exact-path / exact-hash lookup.** ``CodeIndex.search`` is always a BM25
   relevance query. :func:`lookup_exact` is a direct, ranking-free row match
   against the persisted chunk store by path and/or content hash.
3. **A refresh-selected-subtree command.** ``CodeIndex.reindex`` walks the
   WHOLE ``root_dir`` (cheap when unchanged, via the Merkle diff, but still an
   O(tree) pass every call). :func:`refresh_subtree` walks and re-chunks ONLY
   files under one named subdirectory, bounding cost to that subtree instead
   -- the code-index counterpart of ``CodeIndex.index_paths`` for a caller
   that doesn't already have an explicit file list, and of
   ``meridian_outputs.outputs_local.OutputsFtsIndex.refresh_subtree`` /
   ``meridian.outputs_indexer.OutputsFtsIndex.refresh_subtree`` on the
   outputs side.

:func:`resolve_canonical_root` additionally makes this module worktree-aware:
a linked git worktree (a ``.git`` FILE pointing at
``<common>/worktrees/<name>``, not a ``.git`` directory) resolves to its own
distinct canonical root while still reporting the shared git common dir it
traces back to -- so two worktrees checked out from the same repository are
never silently conflated into one index scope.

Every public function here is a standalone, storage-independent function
over a caller-supplied ``root_dir`` -- no Serena, no codebase-memory-mcp, no
Meridian import, no hosted-mode awareness (that guard lives in
``meridian.code_index.search_code_semantic``, one layer up, same as it
already does for ``search_code_semantic`` itself). This is precisely the
"useful when graph/Serena/MCP are unavailable" fallback rung the item asks
for.
"""
from __future__ import annotations

import os
from typing import Any, Callable

from . import code_index as impl

__all__ = [
    "bm25_fallback_search",
    "lookup_exact",
    "refresh_subtree",
    "resolve_canonical_root",
]


# ---------------------------------------------------------------------------
# Worktree-aware canonical root resolution
# ---------------------------------------------------------------------------

def resolve_canonical_root(root_dir: str) -> dict[str, Any]:
    """Canonicalize ``root_dir`` and report whether it is a git worktree.

    Uses :func:`meridian_codeindex.code_index.normalize_root_dir` for the
    usual quote/``~``/env-var/``abspath`` normalization, then inspects
    ``<canonical_root>/.git``:

    * A ``.git`` **directory** -- an ordinary (non-worktree) repo checkout,
      or not a git repo at all if absent entirely.
    * A ``.git`` **file** containing ``gitdir: <path>`` -- a LINKED git
      worktree. The pointed-to path is normally
      ``<common>/.git/worktrees/<name>``; when that shape matches, the
      shared common ``.git`` dir is reported in ``git_common_dir`` so a
      caller can tell "these two canonical roots are different worktrees of
      the SAME repository" without treating them as the same index scope --
      each worktree keeps its own ``canonical_root`` (and therefore its own
      cached :class:`~meridian_codeindex.code_index.CodeIndex` /
      Merkle-tree / ``index_revision``).

    Returns ``{"canonical_root", "resolved_from", "is_git_worktree",
    "git_common_dir"}``. Never raises: a missing/unreadable ``root_dir`` or a
    ``.git`` file this can't parse simply reports ``is_git_worktree=False``.
    """
    canonical_root = impl.normalize_root_dir(root_dir)
    result: dict[str, Any] = {
        "canonical_root": canonical_root,
        "resolved_from": root_dir,
        "is_git_worktree": False,
        "git_common_dir": None,
    }
    if not canonical_root or not os.path.isdir(canonical_root):
        return result
    git_path = os.path.join(canonical_root, ".git")
    if os.path.isdir(git_path):
        result["git_common_dir"] = git_path
        return result
    if not os.path.isfile(git_path):
        return result
    try:
        with open(git_path, "r", encoding="utf-8", errors="replace") as fh:
            first_line = fh.readline().strip()
    except OSError:
        return result
    if not first_line.lower().startswith("gitdir:"):
        return result
    gitdir = first_line.split(":", 1)[1].strip()
    if not os.path.isabs(gitdir):
        gitdir = os.path.normpath(os.path.join(canonical_root, gitdir))
    else:
        gitdir = os.path.normpath(gitdir)
    result["is_git_worktree"] = True
    parent = os.path.dirname(gitdir)
    if os.path.basename(parent) == "worktrees":
        # <common>/.git/worktrees/<name> -> shared common dir is <common>/.git
        result["git_common_dir"] = os.path.dirname(parent)
    else:
        # Non-standard layout -- report the raw pointer rather than guess.
        result["git_common_dir"] = gitdir
    return result


# ---------------------------------------------------------------------------
# Walk-health: never let a swallowed OSError masquerade as "nothing found"
# ---------------------------------------------------------------------------

def _safe_walk_errors(root_dir: str) -> list[str]:
    """Directory-only walk of ``root_dir`` purely to surface OS-level errors.

    ``CodeIndex``'s own indexable-file walk uses a plain ``os.walk`` with no
    ``onerror`` -- a permission-denied (or otherwise failing) subdirectory is
    silently pruned, so its absence from the resulting chunk index is
    indistinguishable from "there was nothing there". This walk prunes the
    SAME vendored/build directories (:data:`meridian_codeindex.code_index._SKIP_DIRS`)
    but never reads file content, so its cost is independent of file sizes --
    it exists only to answer "did the walk itself complete cleanly", not to
    duplicate the real indexing pass. Returns every captured error message
    (``[]`` for a clean walk). Never raises.
    """
    errors: list[str] = []

    def _onerror(exc: OSError) -> None:
        errors.append(str(exc))

    for _cur, dirs, _files in os.walk(root_dir, onerror=_onerror):
        dirs[:] = [d for d in dirs if d not in impl._SKIP_DIRS]
    return errors


def _iter_subtree_indexable_files(
    subtree_dir: str,
    *,
    root_dir: str | None = None,
    errors_out: list[str] | None = None,
    max_files: int = 20_000,
    max_file_bytes: int = 4_000_000,
    max_total_bytes: int = 512_000_000,
    max_seconds: float = 30.0,
) -> list[str]:
    """Absolute paths of every indexable file under ``subtree_dir`` ONLY.

    Applies the code-index file and directory exclusions within
    ``subtree_dir`` and filters candidates through Git's ignore engine before
    returning them. The narrower walk lets :func:`refresh_subtree` discover
    its bounded file set without the caller having to enumerate paths itself.
    Optionally records walk or filter errors into ``errors_out``.
    """

    import subprocess
    import time

    started = time.monotonic()
    root = impl.normalize_root_dir(root_dir or subtree_dir)
    scope = impl.normalize_root_dir(subtree_dir)
    excluded = impl._SKIP_DIRS | {
        ".codex", ".serena", ".cache", "cache", "caches",
        "OneDrive", "Dropbox", "Google Drive", "iCloud Drive", "iCloudDrive",
    }
    excluded_folded = {name.casefold() for name in excluded}

    def _report(reason: str) -> None:
        if errors_out is not None:
            errors_out.append(reason)

    def _onerror(exc: OSError) -> None:
        if errors_out is not None:
            errors_out.append(str(exc))

    found: list[str] = []
    total_bytes = 0
    for cur, dirs, files in os.walk(scope, onerror=_onerror, followlinks=False):
        if time.monotonic() - started > max_seconds:
            _report("time_budget_exceeded")
            return []
        dirs[:] = sorted(
            d for d in dirs
            if d.casefold() not in excluded_folded
            and not os.path.islink(os.path.join(cur, d))
        )
        for fn in sorted(files):
            if time.monotonic() - started > max_seconds:
                _report("time_budget_exceeded")
                return []
            if impl.is_indexable(fn):
                path = os.path.join(cur, fn)
                resolved = os.path.realpath(path)
                try:
                    if os.path.commonpath([scope, resolved]) != scope:
                        continue
                    size = os.path.getsize(path)
                except (OSError, ValueError):
                    _report("file_stat_failed")
                    continue
                if size > max_file_bytes:
                    _report("file_size_budget_exceeded")
                    continue
                if total_bytes + size > max_total_bytes:
                    _report("total_size_budget_exceeded")
                    return []
                if len(found) >= max_files:
                    _report("file_count_budget_exceeded")
                    return []
                total_bytes += size
                found.append(path)

    # The whole-tree index uses Git's ignore engine (including nested
    # .gitignore, .git/info/exclude, and core.excludesFile). Keep a targeted
    # refresh from reintroducing ignored files through its narrower walk.
    try:
        git_root = subprocess.run(
            ["git", "-C", root, "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=max(0.25, min(3.0, max_seconds)),
        )
    except subprocess.TimeoutExpired:
        _report("git_scope_probe_timed_out")
        return []
    except OSError:
        if os.path.exists(os.path.join(root, ".git")):
            _report("git_ignore_filter_unavailable")
            return []
        return found
    if git_root.returncode != 0 or not git_root.stdout.strip():
        if os.path.exists(os.path.join(root, ".git")):
            _report("git_ignore_filter_unavailable")
            return []
        return found

    repo_root = impl.normalize_root_dir(git_root.stdout.strip())
    rel_paths: list[str] = []
    for path in found:
        try:
            if os.path.commonpath([repo_root, os.path.realpath(path)]) != repo_root:
                _report("git_scope_path_outside_repo")
                return []
        except (OSError, ValueError):
            _report("git_scope_path_outside_repo")
            return []
        rel_paths.append(os.path.relpath(path, repo_root).replace(os.sep, "/"))
    if not rel_paths:
        return found
    remaining = max_seconds - (time.monotonic() - started)
    if remaining <= 0:
        _report("time_budget_exceeded")
        return []
    try:
        ignored = subprocess.run(
            ["git", "-C", repo_root, "check-ignore", "--no-index", "--stdin", "-z"],
            input=b"\0".join(os.fsencode(path) for path in rel_paths) + b"\0",
            capture_output=True,
            check=False,
            timeout=remaining,
        )
    except subprocess.TimeoutExpired:
        _report("time_budget_exceeded")
        return []
    if ignored.returncode not in (0, 1):
        _report("ignored_path_filter_unavailable")
        return []
    ignored_paths = {
        os.fsdecode(part).replace("\\", "/")
        for part in ignored.stdout.split(b"\0") if part
    }
    return [path for path, rel in zip(found, rel_paths) if rel not in ignored_paths]


# ---------------------------------------------------------------------------
# The hardened fallback search entry point
# ---------------------------------------------------------------------------

def bm25_fallback_search(
    root_dir: str,
    query: str,
    *,
    limit: int = 10,
    kind: str | None = None,
    db_path: str = ":memory:",
    reindex: bool = True,
) -> dict[str, Any]:
    """The hardened local BM25 secondary path.

    A thin, explicit-state wrapper around
    :func:`meridian_codeindex.code_index.search_code_semantic` -- identical
    ``hits``/``total_indexed`` behavior for a caller that only reads those,
    plus the additive state vocabulary this item's acceptance criteria
    requires:

    * ``canonical_root`` / ``is_git_worktree`` / ``git_common_dir`` --
      worktree-aware root resolution (:func:`resolve_canonical_root`).
    * ``index_revision`` / ``last_checkpoint_at`` -- freshness, mirroring
      :meth:`CodeIndex.get_convergence_state`.
    * ``partial_index`` -- True when the (optional) vector leg is behind the
      keyword leg (``CodeIndex.get_convergence_state()["degraded"]``); the
      BM25/keyword leg itself is always a complete pass over whatever the
      walk actually saw (``CodeIndex.reindex`` is not deadline-bound), so
      this tracks vector-leg staleness specifically.
    * ``inconclusive`` -- True when THIS call's directory walk hit a real OS
      error (:func:`_safe_walk_errors`) that ``CodeIndex``'s own walk would
      otherwise silently prune past. In this state, an empty or short
      ``hits`` list must NEVER be read as "no match anywhere in this tree" --
      only as "this pass could not fully examine it".
    * ``degraded`` -- the umbrella flag (``inconclusive or partial_index``).

    A missing ``root_dir`` or blank ``query`` sets ``inconclusive=True`` (not
    just an ``error`` string) precisely so a caller checking ``degraded``/
    ``inconclusive`` catches this case the same way it catches a mid-walk
    failure, rather than having to separately remember to check ``error``.
    """
    canonical = resolve_canonical_root(root_dir)
    root = canonical["canonical_root"]
    result: dict[str, Any] = {
        "root_dir": root,
        "query": query,
        "hits": [],
        "total_indexed": 0,
        "canonical_root": root,
        "is_git_worktree": canonical["is_git_worktree"],
        "git_common_dir": canonical["git_common_dir"],
        "index_revision": 0,
        "last_checkpoint_at": None,
        "partial_index": False,
        "inconclusive": False,
        "degraded": False,
    }
    if not query or not str(query).strip():
        result["error"] = "query is required"
        result["inconclusive"] = True
        result["degraded"] = True
        return result
    if not root or not os.path.isdir(root):
        result["error"] = f"root_dir does not exist: {root}"
        result["inconclusive"] = True
        result["degraded"] = True
        return result

    walk_errors = _safe_walk_errors(root)
    idx = impl.get_code_index(root, db_path=db_path)
    if reindex:
        idx.reindex()
    convergence = idx.get_convergence_state().to_dict()
    hits = idx.search(query, limit=limit, kind=kind)

    inconclusive = bool(walk_errors)
    partial_index = bool(convergence.get("degraded"))
    degraded = inconclusive or partial_index

    result.update({
        "hits": hits,
        "total_indexed": idx.count(),
        "index_revision": convergence.get("index_revision", 0),
        "last_checkpoint_at": convergence.get("last_checkpoint_at"),
        "partial_index": partial_index,
        "inconclusive": inconclusive,
        "degraded": degraded,
        "walk_errors": walk_errors,
        "convergence": convergence,
    })
    if inconclusive:
        result["error"] = (
            "bm25_fallback_search could not fully walk root_dir "
            f"({len(walk_errors)} directory error(s) during this pass) -- "
            "hits reflect only what the walk managed to see; re-invoke once "
            "the underlying filesystem issue clears rather than treating an "
            "empty/short hits list as a confirmed miss."
        )
    return result


# ---------------------------------------------------------------------------
# Exact-path / exact-hash lookup (bypasses BM25 ranking entirely)
# ---------------------------------------------------------------------------

def lookup_exact(
    root_dir: str,
    *,
    path: str | None = None,
    content_hash: str | None = None,
    db_path: str = ":memory:",
    reindex: bool = True,
) -> dict[str, Any]:
    """Exact chunk lookup by PATH and/or content HASH -- no BM25 ranking.

    A direct row match against the persisted ``code_chunks`` store: pass
    ``path`` (absolute, or relative to ``root_dir``) and/or ``content_hash``
    (a chunk's SHA-256, as produced by
    :attr:`~meridian_codeindex.code_index.CodeChunk.content_hash`). At least
    one of the two is required; when both are given, a match must satisfy
    both.

    Distinguishes three outcomes explicitly, so a caller never has to guess:

    * ``found=True`` -- one or more matching chunks in ``matches``.
    * ``found=False, inconclusive=False`` -- the walk was clean and the
      index genuinely has no matching chunk right now.
    * ``found=False, inconclusive=True`` -- either neither selector was
      given, ``root_dir`` doesn't exist, or this pass's directory walk hit a
      real OS error (:func:`_safe_walk_errors`) -- absence here is NOT
      confirmed.
    """
    canonical = resolve_canonical_root(root_dir)
    root = canonical["canonical_root"]
    result: dict[str, Any] = {
        "root_dir": root,
        "path": path,
        "content_hash": content_hash,
        "matches": [],
        "found": False,
        "inconclusive": False,
    }
    if not path and not content_hash:
        result["inconclusive"] = True
        result["error"] = "one of path or content_hash is required"
        return result
    if not root or not os.path.isdir(root):
        result["inconclusive"] = True
        result["error"] = f"root_dir does not exist: {root}"
        return result

    walk_errors = _safe_walk_errors(root)
    idx = impl.get_code_index(root, db_path=db_path)
    if reindex:
        idx.reindex()

    matches: list[dict[str, Any]] = []
    try:
        # Intentional internal reuse: lookup_exact lives in the same package
        # as CodeIndex and reuses its already-open connection/schema/row
        # mapping rather than re-implementing them.
        con = idx._connect()  # noqa: SLF001
        idx._ensure_schema(con)  # noqa: SLF001
        sql = (
            "SELECT chunk_id, path, language, kind, name, line_start, "
            "line_end, content, content_hash FROM code_chunks"
        )
        clauses: list[str] = []
        params: list[Any] = []
        if path:
            abs_path = path if os.path.isabs(path) else idx._abs(path)  # noqa: SLF001
            clauses.append("path = ?")
            params.append(os.path.normpath(abs_path))
        if content_hash:
            clauses.append("content_hash = ?")
            params.append(content_hash)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        relation = con.execute(sql, params)
        columns = [c[0] for c in relation.description]
        for row in relation.fetchall():
            matches.append(idx._row_to_hit(columns, row))  # noqa: SLF001
    except Exception:  # noqa: BLE001 -- a bad/empty index resolves to no matches
        pass

    result["matches"] = matches
    result["found"] = bool(matches)
    result["walk_errors"] = walk_errors
    result["inconclusive"] = bool(walk_errors) and not matches
    return result


# ---------------------------------------------------------------------------
# Refresh-selected-subtree command
# ---------------------------------------------------------------------------

def refresh_subtree(
    root_dir: str,
    subtree: str,
    *,
    db_path: str = ":memory:",
) -> dict[str, Any]:
    """Bounded refresh of ONE subdirectory of ``root_dir``.

    ``CodeIndex.reindex`` always walks the whole ``root_dir`` (cheap when
    unchanged, thanks to the Merkle diff, but still an O(tree) stat pass
    every call). When a caller already knows only one subdirectory changed
    (a single module just got regenerated, a targeted checkout refresh,
    ...), this discovers files under JUST that subtree
    (:func:`_iter_subtree_indexable_files`) and delegates the actual
    chunk upsert to :meth:`CodeIndex.index_paths`, so cost is bounded by the
    subtree's size, not ``root_dir``'s -- the explicit "refresh-selected-
    subtree" command the item's acceptance criteria calls for, mirroring
    ``OutputsFtsIndex.refresh_subtree`` on the outputs side.

    ``subtree`` may be absolute or relative to ``root_dir``; it must resolve
    to a real directory INSIDE ``root_dir`` (or ``root_dir`` itself) -- an
    out-of-tree or missing subtree returns ``{"error": ...}`` without
    touching the index. Returns
    ``{"root_dir", "subtree", "indexed", "skipped", "paths", "walk_errors",
    "inconclusive"}``. Never raises.
    """
    canonical = resolve_canonical_root(root_dir)
    root = canonical["canonical_root"]
    if not root or not os.path.isdir(root):
        return {
            "root_dir": root, "subtree": subtree, "indexed": 0, "skipped": 0,
            "paths": [], "walk_errors": [], "inconclusive": True,
            "error": f"root_dir does not exist: {root}",
        }
    abs_subtree = subtree if os.path.isabs(subtree) else os.path.join(root, subtree)
    abs_subtree = impl.normalize_root_dir(abs_subtree)
    abs_root = impl.normalize_root_dir(root)
    try:
        normalized_subtree = os.path.normcase(abs_subtree)
        normalized_root = os.path.normcase(abs_root)
        within = os.path.commonpath(
            [normalized_subtree, normalized_root]
        ) == normalized_root
    except ValueError:  # different drive on Windows
        within = False
    if not within or not os.path.isdir(abs_subtree):
        return {
            "root_dir": root, "subtree": subtree, "indexed": 0, "skipped": 0,
            "paths": [], "walk_errors": [], "inconclusive": True,
            "error": f"subtree does not exist under root_dir: {subtree}",
        }

    walk_errors: list[str] = []
    paths = _iter_subtree_indexable_files(
        abs_subtree, root_dir=root, errors_out=walk_errors,
    )
    if walk_errors:
        return {
            "root_dir": root,
            "subtree": subtree,
            "indexed": 0,
            "skipped": 0,
            "paths": [],
            "walk_errors": walk_errors,
            "inconclusive": True,
            "error": "subtree_walk_incomplete",
        }
    idx = impl.get_code_index(root, db_path=db_path)
    outcome = idx.index_paths(paths, prune_root=abs_subtree)
    outcome["root_dir"] = root
    outcome["subtree"] = subtree
    outcome["walk_errors"] = walk_errors
    outcome["inconclusive"] = bool(outcome.get("error"))
    return outcome
