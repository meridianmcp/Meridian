"""2ce5bc76 — robust symbol prospecting with a three-rung fallback chain.

Extracted here (not in mcp/handler.py) so the implementation is importable
without triggering the handler→server circular import. Tests import directly
from this module; handler.py imports and calls prospect_symbol_impl.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any, Awaitable, Callable

# 4b8f083f — a stored fingerprint only counts as a git-commit-drift baseline
# when it actually looks like a git commit (short or full hex SHA). The same
# fingerprint slot can also hold an ISO "indexed_at" timestamp (see
# routes/tunnel.py's _extract_graph_fingerprint preference order) which is not
# a git ref and must never be handed to `git rev-list`.
_GIT_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")


def _looks_like_git_commit(value: "str | None") -> bool:
    return bool(value) and isinstance(value, str) and bool(_GIT_COMMIT_RE.match(value.strip()))


def _git_commit_drift_sync(root_dir: str, stored_commit: str) -> "dict[str, Any] | None":
    """Synchronous, best-effort local git check for index/repo drift.

    Runs entirely on-disk (`git rev-parse` + `git rev-list --count`) — no
    tunnel round-trip, no dependency on the external codebase-memory-mcp
    binary self-reporting freshness. Must be called from a worker thread
    (via ``hardening.run_in_bulkhead``), never awaited directly: asyncio
    subprocess creation (``create_subprocess_exec``) is unsupported on the
    SelectorEventLoop this project forces on Windows (see __main__.py), so a
    plain blocking ``subprocess.run`` inside a thread is the only safe way to
    shell out to git from here.

    Returns None (fail-open) whenever the signal can't be computed cleanly:
    not a git repo, `stored_commit` unresolvable in this checkout (e.g. a
    fingerprint from a different clone/fork), git missing, or any timeout —
    never raises.
    """
    if not root_dir or not os.path.isdir(root_dir):
        return None
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root_dir, capture_output=True, text=True, timeout=3,
        )
        if head.returncode != 0:
            return None
        head_commit = head.stdout.strip()
        if not head_commit or head_commit == stored_commit:
            return None
        count = subprocess.run(
            ["git", "rev-list", "--count", f"{stored_commit}..{head_commit}"],
            cwd=root_dir, capture_output=True, text=True, timeout=3,
        )
        if count.returncode != 0:
            # Most commonly: stored_commit isn't reachable in this checkout's
            # history at all (unrelated clone, rewritten history, shallow
            # clone). Nothing safe to report — fail open rather than guess.
            return None
        commits_since_index = int((count.stdout or "0").strip() or "0")
        if commits_since_index <= 0:
            return None
        return {
            "stored_commit": stored_commit,
            "head_commit": head_commit,
            "commits_since_index": commits_since_index,
        }
    except Exception:  # noqa: BLE001 — drift probe must never raise
        return None


async def _detect_graph_commit_drift(
    tenant_id: str, project_id: str, root_dir: str,
) -> "dict[str, Any] | None":
    """4b8f083f — cheap local staleness signal that closes a real gap in the
    2ce5bc76 fingerprint mechanism.

    ``_annotate_graph_result_staleness`` (routes/tunnel.py) only flags
    staleness when a DIFFERENT process re-indexes and ``codebase__index_status``
    echoes back a fingerprint newer than the one this process cached — it
    compares two readings of the INDEX's own self-report. It does nothing for
    the far more common real-world case (confirmed live): real commits land in
    the repo and nobody re-runs ``index_repository``. ``index_status`` just
    keeps echoing the same last-indexed fingerprint forever in that case, so
    ``search_graph`` never gets flagged even after the queried file has moved
    hundreds of lines.

    This closes that gap using nothing but a local ``git rev-list --count``
    against the stored ``index_repository`` fingerprint — no tunnel round
    trip, no dependency on the external binary self-reporting anything. Only
    runs when there is an actual baseline to compare against (mirrors the
    "no baseline, no probe" discipline in ``_annotate_graph_result_staleness``):
    no stored fingerprint, or a fingerprint that isn't a git commit (e.g. an
    ``indexed_at`` timestamp), means there is nothing safe to check, so this
    returns None immediately without shelling out to git at all.

    Never raises — any failure (no git repo, no git binary, unresolvable
    commit, timeout) degrades silently to None, same as every other rung in
    this fallback chain.
    """
    if not tenant_id or not root_dir:
        return None
    try:
        from .routes import tunnel as _tunnel_mod  # noqa: PLC0415
        stored = _tunnel_mod.get_cached_graph_fingerprint(tenant_id, project_id or None)
    except Exception:  # noqa: BLE001
        return None
    if not _looks_like_git_commit(stored):
        return None
    try:
        from . import hardening as _hardening  # noqa: PLC0415
        return await _hardening.run_in_bulkhead(
            _git_commit_drift_sync, root_dir, stored,
            timeout=5.0, label="prospect_symbol_commit_drift",
        )
    except Exception:  # noqa: BLE001 — drift probe must never break prospecting
        return None


def _extract_hits(payload: Any) -> list:
    """Normalise a codebase__search_graph payload into a flat hits list.

    Returns [] when the payload has no recognisable hit container.
    Does NOT treat application-level error payloads as empty results —
    call :func:`_payload_is_error` first to distinguish the two cases.
    """
    if isinstance(payload, dict):
        return (
            payload.get("results")
            or payload.get("matches")
            or payload.get("hits")
            or payload.get("nodes")
            or payload.get("symbols")
            or payload.get("entities")
            or []
        )
    if isinstance(payload, list):
        return payload
    return []


def _payload_is_error(payload: Any) -> "str | None":
    """Return an error message string if *payload* is an application-level
    error dict (e.g. ``{"error": "project not found"}``), else None.

    Detects the case where codebase-memory-mcp returns an error inside the
    MCP content envelope instead of a JSON-RPC error level response.  Without
    this check, prospect_symbol would silently treat the error as "zero
    results" and report ``fallback_reason="graph_empty"`` — hiding the real
    cause from the caller.

    Only fires when the dict has an ``"error"``/``"message"``/``"detail"`` key
    AND has NONE of the recognised hit-container keys.  This avoids
    false-positives on a legitimate (but empty) result that happens to carry
    an ``"error"`` field.

    1579bc1e — probes each candidate field for a STRING value instead of
    short-circuiting an ``or``-chain on the first *truthy* value regardless of
    type. The original ``payload.get("error") or payload.get("message") or
    payload.get("detail")`` returns whatever ``payload["error"]`` holds the
    instant it's truthy — even when that's a nested dict (e.g.
    ``{"error": {"message": "project not found"}}``, a shape some MCP servers
    use instead of a bare string). ``isinstance(msg, str)`` then fails and the
    whole function returns None, silently discarding a real, matchable error
    message and letting it masquerade as "zero results" (``graph_empty``)
    instead of triggering the project_id-mismatch retry. Falls through to a
    one-level-deep probe of the same key names when a candidate is a dict.
    """
    if not isinstance(payload, dict):
        return None
    _hit_keys = frozenset({"results", "matches", "hits", "nodes", "symbols", "entities"})
    if any(k in payload for k in _hit_keys):
        return None
    for _key in ("error", "message", "detail"):
        val = payload.get(_key)
        if isinstance(val, str) and val.strip():
            return val.strip()
        if isinstance(val, dict):
            for _subkey in ("message", "error", "detail", "reason"):
                subval = val.get(_subkey)
                if isinstance(subval, str) and subval.strip():
                    return subval.strip()
    # A string payload that came back non-JSON from _extract_graph_matches.
    return None


# ---------------------------------------------------------------------------
# MDE-2 rework — wrong-body resolution + active-repository scoping.
#
# The verifier's finding: every rung above returns FUZZY hits by design
# (BM25/keyword matching for `semantic`, a third-party graph search keyed on
# a free-text `query` for `graph`, name-substring matching for `serena`'s
# find_symbol). A caller that blindly reads ``hits[0]`` (the previous
# behavior at every call site below, and in the two production consumers —
# ``mcp/handler.py``'s receipt-writer and ``pointers._resolve_symbol``) can
# silently be handed a NEIGHBORING symbol's file/body under the queried
# symbol's identity: e.g. querying "compute_total" and getting back
# "compute_total_v2" or "compute_totals" because that happened to rank first
# in the underlying search. The receipt layer's file-existence check
# (``code_intel_receipt._file_exists_under_root``) cannot catch this at all
# — a neighboring symbol's file genuinely exists, just isn't the RIGHT file.
#
# The fix is at the resolution engine, not the receipt: never let a fuzzy hit
# be treated as authoritative for the queried symbol unless its OWN reported
# identity (qualified_name/name_path/name) is an EXACT match. Two composable
# pieces:
#   - `_hit_identity_matches` / `select_exact_hit` — the exactness check.
#   - `_hit_is_out_of_scope` / scope-filtering — the ACTIVE REPOSITORY
#     scoping half: a hit whose file resolves outside `root_dir` (or carries
#     a cross-tool contamination marker) is never in-scope for THIS session's
#     query, regardless of name match, so a graph/backend response about a
#     different indexed repository can never be silently accepted as
#     belonging to the active one.
# `_finalize_hits` applies both, uniformly, before a rung's hits are stored
# on `result["hits"]` — so every existing caller that reads `hits[0]`
# (unchanged call shape) gets the hardened selection for free.
# ---------------------------------------------------------------------------

def _hit_identity_matches(hit: Any, symbol: str) -> bool:
    """True when *hit*'s OWN reported identity (``qualified_name``,
    ``name_path``, or ``name``) is an EXACT (case-sensitive) match for
    *symbol*. The only signal that lets a caller safely treat a hit's
    file/line-range as authoritative for the QUERIED symbol rather than a
    neighboring one returned by fuzzy graph/BM25/keyword matching.
    """
    if not isinstance(hit, dict) or not symbol:
        return False
    for key in ("qualified_name", "name_path", "name"):
        val = hit.get(key)
        if isinstance(val, str) and val == symbol:
            return True
    return False


def select_exact_hit(hits: "list[Any] | None", symbol: str) -> "dict[str, Any] | None":
    """Return the first hit in *hits* whose own identity EXACTLY matches
    *symbol* (see :func:`_hit_identity_matches`), or ``None`` when every hit
    is a near-miss/fuzzy match. Any caller that wants to treat a hit's
    file/range as authoritative for the queried symbol (a receipt's
    ``resolved_file``, ``pointers._resolve_symbol``, a ``get_code_snippet``
    fetch) MUST go through this — or an equivalent exact check — instead of
    indexing ``hits[0]`` directly.
    """
    for hit in hits or []:
        if _hit_identity_matches(hit, symbol):
            return hit
    return None


def hit_path(hit: "dict[str, Any] | None") -> "str | None":
    """Extract the file/path field from a normalized or raw hit dict,
    across the field-name variance between rungs (graph/serena carry
    ``file``, semantic carries ``path``, some third-party shapes carry
    ``relative_path``/``uri``)."""
    if not isinstance(hit, dict):
        return None
    val = hit.get("file") or hit.get("path") or hit.get("relative_path") or hit.get("uri")
    return val if isinstance(val, str) and val else None


def hit_identity(hit: "dict[str, Any] | None") -> "str | None":
    """Extract the resolved symbol identity a hit itself reports
    (``qualified_name`` preferred, then ``name_path``/``name``) — the REAL
    resolved identity, as opposed to the raw (and receipt-truncated) query
    string a caller searched for."""
    if not isinstance(hit, dict):
        return None
    for key in ("qualified_name", "name_path", "name"):
        val = hit.get(key)
        if isinstance(val, str) and val:
            return val
    return None


def hit_range(hit: "dict[str, Any] | None") -> "dict[str, int] | None":
    """Extract a ``{start_line, end_line}`` line range from a hit, across
    the several shapes rungs report a location in (``line_start``/
    ``line_end`` — semantic; ``start_line``/``end_line`` or a nested
    ``range`` dict — some graph/serena shapes; a bare ``line`` — the most
    common minimal shape, treated as a single-line range). Returns ``None``
    when no line information is present at all — never fabricates one.
    """
    if not isinstance(hit, dict):
        return None
    start = hit.get("line_start")
    end = hit.get("line_end")
    if start is None:
        start = hit.get("start_line")
        end = hit.get("end_line")
    if start is None:
        rng = hit.get("range")
        if isinstance(rng, dict):
            start = rng.get("start_line") or rng.get("start")
            end = rng.get("end_line") or rng.get("end")
    if start is None:
        start = hit.get("line")
        end = end if end is not None else start
    if start is None:
        return None
    if end is None:
        end = start
    try:
        return {"start_line": int(start), "end_line": int(end)}
    except (TypeError, ValueError):
        return None


def hit_content(hit: "dict[str, Any] | None") -> "str | None":
    """Extract whatever body/snippet text a hit already carries inline
    (``content`` — the local semantic/BM25 index's own chunk text;
    ``snippet``/``body``/``text`` — plausible third-party shapes). Used by
    :mod:`code_intel_receipt`'s graph-vs-live-file hash comparison. ``None``
    when the hit carries no inline text at all (most graph/serena hits —
    those only report a location, not a body)."""
    if not isinstance(hit, dict):
        return None
    for key in ("content", "snippet", "body", "text"):
        val = hit.get(key)
        if isinstance(val, str) and val:
            return val
    return None


def _hit_is_out_of_scope(hit: Any, root_dir: "str | None") -> bool:
    """True only on POSITIVE evidence that *hit*'s file lies OUTSIDE the
    active repository (*root_dir*) — MDE-2's "scope graph queries to the
    active repository" fix. Two forms of positive evidence:

      1. a cross-tool contamination marker in the path (reuses
         ``code_intel_receipt.is_contaminated_repo_path`` — the SAME
         ``.codex/worktrees/...`` marker the receipt layer already excludes,
         applied here at hit-selection time instead of only after the fact);
      2. an ABSOLUTE path that resolves outside ``root_dir``'s real path —
         i.e. the backend genuinely returned a file from a different
         checkout on disk.

    Never rejects on an unverifiable case (no ``root_dir`` to scope against,
    no path on the hit, or a RELATIVE path — which could legitimately
    resolve under ``root_dir`` and can't be proven otherwise without
    guessing) — same fail-open, "don't overclaim" posture as the rest of
    this module and ``code_intel_receipt.py``.
    """
    if not root_dir or not isinstance(hit, dict):
        return False
    raw_path = hit_path(hit)
    if not raw_path:
        return False
    try:
        from .code_intel_receipt import is_contaminated_repo_path as _is_contaminated
    except Exception:  # noqa: BLE001 — scoping must never raise
        _is_contaminated = None
    if _is_contaminated is not None and _is_contaminated(raw_path):
        return True
    p = Path(raw_path)
    if not p.is_absolute():
        return False
    try:
        root_real = Path(root_dir).resolve()
        hit_real = p.resolve()
    except Exception:  # noqa: BLE001
        return False
    try:
        hit_real.relative_to(root_real)
        return False
    except ValueError:
        return True


def _reorder_exact_first(hits: "list[Any]", symbol: str) -> "list[Any]":
    """Stable-reorder *hits* so any exact-identity match (see
    :func:`_hit_identity_matches`) comes first; non-exact hits keep their
    relative order after. Uses ``id()`` (not ``==``) to split the list so
    content-equal-but-distinct hit dicts are never misclassified."""
    if not hits:
        return hits
    exact_ids: set = set()
    exact: list = []
    rest: list = []
    for h in hits:
        if _hit_identity_matches(h, symbol):
            exact.append(h)
            exact_ids.add(id(h))
        else:
            rest.append(h)
    if not exact:
        return hits
    return exact + rest


def _finalize_hits(hits: "list[Any]", symbol: str, root_dir: "str | None") -> "list[Any]":
    """Apply BOTH MDE-2 hardenings to a rung's raw hit list before it is
    stored on ``result["hits"]``:

      1. drop any hit with POSITIVE evidence of being outside the active
         repository (:func:`_hit_is_out_of_scope`) — never drops down to an
         EMPTY list when every hit happens to look out-of-scope (that would
         silently hide real, if imperfect, prospecting evidence); only
         filters when at least one hit remains in-scope;
      2. stable-reorder so an exact identity match comes first
         (:func:`_reorder_exact_first`), so any caller that only looks at
         ``hits[0]`` is never handed a neighboring symbol's body when an
         exact match was available in this rung's own results.
    """
    if not hits:
        return hits
    in_scope = [h for h in hits if not _hit_is_out_of_scope(h, root_dir)]
    working = in_scope if in_scope else hits
    return _reorder_exact_first(working, symbol)


def resolve_exact_hit_from_tunnel_result(
    tunnel_mod: Any, tunnel_result: Any, symbol: "str | None", root_dir: "str | None",
) -> "dict[str, Any] | None":
    """MDE-2 rework — parse a raw tunnel-FORWARDED tool result (a direct
    ``codebase__search_graph``/``extractor__find_symbol``/... call, NOT
    routed through :func:`prospect_symbol_impl`) the same way its own
    graph/serena rungs do, then apply the identical exact-match +
    active-repository scoping used everywhere else in this module.

    Used by ``mcp/handler.py``'s tunnel-forward receipt chokepoint (the
    ``tools/call`` dispatch path a THIRD-PARTY code-intel tool name takes
    when called directly, never touching ``prospect_symbol_impl`` at all) so
    that chokepoint gets the SAME wrong-body protection as the native
    ``prospect_symbol`` tool, instead of a separate, weaker code path.

    Never raises; returns ``None`` on any parse failure, when *symbol* is
    empty, or when no exact, in-scope hit is found (never a fuzzy fallback).
    """
    if not symbol:
        return None
    try:
        payload = tunnel_mod._extract_graph_matches(tunnel_result)
        hits = _extract_hits(payload)
    except Exception:  # noqa: BLE001 — parsing a third-party payload must never raise
        return None
    if not hits:
        return None
    scoped = _finalize_hits(hits, symbol, root_dir)
    return select_exact_hit(scoped, symbol)


async def prospect_symbol_impl(
    symbol: str,
    project_id: str,
    root_dir: str,
    limit: int,
    kind: "str | None",
    stale_graph: bool,
    tenant: "dict[str, Any] | None",
    data_dir: str,
) -> dict[str, Any]:
    """Three-rung fallback chain for robust symbol prospecting.

    Rung 1 (graph): codebase__search_graph — fast, indexed. Skipped when
      stale_graph=True, when no code tunnel is active, or (4b8f083f) when a
      local git-commit-drift probe finds real commits since the last
      index_repository run for this (tenant, project) — see
      ``_detect_graph_commit_drift``. (1579bc1e) When the graph rung misses
      because of a project_id/slug mismatch — whether that surfaces as a
      quietly-empty result, an app-level error payload, or a raised
      exception from ``call_tunnel_tool`` — a broad retry without
      project_id is attempted before giving up on this rung.
    Rung 2 (serena): extractor__find_symbol / extractor__find_declaration —
      AST-accurate, never stale. Tried when graph returns zero results or is
      skipped.
    Rung 3 (semantic): search_code_semantic BM25 grep-style — local,
      no tunnel needed. Used when both upper rungs miss or are unavailable
      (requires root_dir).

    Never raises. Each rung is labelled in the result so the caller knows
    which level succeeded.
    """
    tenant_id = (tenant or {}).get("id") or ""
    # d5e60791 -- structured per-rung diagnostics. Each rung entry tracks:
    #   status: "not_attempted" | "skipped" | "attempted" | "succeeded" |
    #           "empty" | "error"
    #   attempted_tool / selected_tool: which underlying tool(s) this rung
    #           tried, and which one (if any) actually produced the result.
    #   reason: why a rung was skipped (e.g. "no_active_tunnel").
    #   error / error_kind: the real exception text when a rung raised, and
    #           whether it looks like a missing-dependency problem
    #           ("dependency_error", e.g. ModuleNotFoundError/ImportError) or
    #           some other runtime failure ("runtime_error") -- so a caller
    #           never has to guess WHY a rung silently produced nothing. This
    #           closes the exact gap that let a swallowed
    #           `ModuleNotFoundError: No module named 'meridian_codeindex'`
    #           in the semantic rung collapse into a bare rung="none",
    #           fallback_reason=None with zero diagnostic (d5e60791).
    result: dict[str, Any] = {
        "symbol": symbol,
        "rung": "none",
        "hits": [],
        "fallback_reason": None,
        "rungs": {
            "graph": {
                "status": "not_attempted",
                "attempted_tool": None,
                "selected_tool": None,
            },
            "serena": {
                "status": "not_attempted",
                "attempted_tool": None,
                "selected_tool": None,
            },
            "semantic": {
                "status": "not_attempted",
                "attempted_tool": None,
                "selected_tool": None,
            },
        },
    }

    def _mark_rung(rung_name: str, status: str, **fields: Any) -> None:
        """Update one rung's diagnostic entry in-place (d5e60791)."""
        entry = result["rungs"][rung_name]
        entry["status"] = status
        for key, value in fields.items():
            if value is not None:
                entry[key] = value

    def _classify_error(exc: BaseException) -> str:
        """dependency_error (missing/broken import) vs runtime_error (all else)."""
        if isinstance(exc, (ImportError, ModuleNotFoundError)):
            return "dependency_error"
        return "runtime_error"

    # ------------- 4b8f083f: local commit-drift staleness probe -----------
    # Only runs when the caller hasn't already told us the graph is stale,
    # and only when there's a tenant + root_dir to check against — see
    # _detect_graph_commit_drift for why this is a real gap the 2ce5bc76
    # fingerprint mechanism leaves open (it can't see "nobody re-indexed
    # after N real commits", only "a sibling process re-indexed").
    commit_drift: "dict[str, Any] | None" = None
    if not stale_graph and tenant_id and root_dir:
        commit_drift = await _detect_graph_commit_drift(tenant_id, project_id, root_dir)
        if commit_drift:
            stale_graph = True
            result["_graph_commit_drift"] = commit_drift

    # ------------- Rung 1: codebase__search_graph -------------------------
    if not stale_graph and tenant_id:
        try:
            from .routes import tunnel as _tunnel_mod  # noqa: PLC0415
            if _tunnel_mod.has_active_tunnel(tenant_id):
                _mark_rung("graph", "attempted", attempted_tool="codebase__search_graph")
                graph_args: dict[str, Any] = {"query": symbol, "limit": limit}
                if project_id:
                    graph_args["project_id"] = project_id

                async def _broad_retry_without_project_id(
                    original_msg: str,
                ) -> bool:
                    """1579bc1e — retry search_graph WITHOUT project_id and,
                    if it turns up hits, populate *result* and return True.

                    The 9033914e fix already does this when search_graph
                    comes back with a project_id but *zero hits* — the
                    codebase-memory-mcp slug derived from repo_path may not
                    match the caller's project_id. But the SAME slug
                    mismatch can just as easily surface as an explicit
                    "project not found"/"not indexed" error — either an
                    app-level error payload inside the content envelope, or
                    an exception raised by ``call_tunnel_tool`` (e.g. a
                    JSON-RPC-level error, possibly already hint-enriched by
                    ``_enrich_code_intel_project_error``) — instead of a
                    quietly-empty result. Confirmed report (1579bc1e):
                    prospect_symbol's graph rung fails outright with that
                    error immediately after a fresh, successful
                    index_repository run on the exact same project slug,
                    while a direct call without project_id (or with the
                    auto-assigned slug) succeeds instantly. Only worth
                    trying when the error actually looks project-related —
                    never fires for unrelated errors (syntax errors, rate
                    limits, etc.) via ``_is_project_not_found_error``.

                    Never raises: any failure here just means the retry
                    didn't help, and the caller keeps its original error.
                    """
                    if not (
                        project_id
                        and graph_args.get("project_id")
                        and _tunnel_mod._is_project_not_found_error(original_msg)
                    ):
                        return False
                    try:
                        broad_args: dict[str, Any] = {"query": symbol, "limit": limit}
                        broad_result = await _tunnel_mod.call_tunnel_tool(
                            tenant_id, "codebase__search_graph", broad_args,
                        )
                    except Exception:  # noqa: BLE001 — retry itself may fail
                        return False
                    if broad_result is None:
                        return False
                    broad_payload = _tunnel_mod._extract_graph_matches(broad_result)
                    broad_hits = _extract_hits(broad_payload)
                    if not (broad_hits and isinstance(broad_hits, list) and len(broad_hits) > 0):
                        return False
                    result["rung"] = "graph"
                    result["hits"] = _finalize_hits(broad_hits, symbol, root_dir)
                    result["graph_raw"] = broad_payload
                    _mark_rung(
                        "graph", "succeeded",
                        attempted_tool="codebase__search_graph",
                        selected_tool="codebase__search_graph (broad retry)",
                    )
                    result["fallback_reason"] = (
                        f"graph_project_id_mismatch_error: search_graph with "
                        f"project_id={project_id!r} failed ({original_msg!r}); "
                        f"broad search (no project_id) succeeded — the "
                        f"caller's project_id may not match the repo-path "
                        f"slug auto-assigned by index_repository"
                    )
                    return True

                try:
                    graph_result = await _tunnel_mod.call_tunnel_tool(
                        tenant_id, "codebase__search_graph", graph_args,
                    )
                except Exception as _rung1_exc:  # noqa: BLE001
                    # 1579bc1e — an explicit "project not found"/"not indexed"
                    # error raised by call_tunnel_tool is the same
                    # slug-mismatch root cause as the zero-hits case below —
                    # just surfaced as an exception. Try the broad retry
                    # before giving up on the graph rung.
                    if await _broad_retry_without_project_id(str(_rung1_exc)):
                        return result
                    result["fallback_reason"] = f"graph_error: {_rung1_exc}"
                    _mark_rung(
                        "graph", "error",
                        attempted_tool="codebase__search_graph",
                        error=f"{type(_rung1_exc).__name__}: {_rung1_exc}",
                        error_kind=_classify_error(_rung1_exc),
                    )
                    graph_result = None

                if graph_result is not None:
                    # Check for injected staleness warning from Part 1.
                    staleness_info = None
                    if isinstance(graph_result, dict):
                        staleness_info = graph_result.get("_graph_staleness")
                    payload = _tunnel_mod._extract_graph_matches(graph_result)

                    # 9033914e — detect application-level errors returned
                    # inside the MCP content envelope.  Without this, an
                    # "unknown project_id" error from codebase-memory-mcp
                    # looks identical to zero results and is reported as
                    # "graph_empty", hiding the real cause.
                    app_err = _payload_is_error(payload)
                    if app_err is None and isinstance(payload, str):
                        # Non-JSON text block (e.g. "Error: project not found")
                        app_err = payload[:200]

                    hits = _extract_hits(payload)
                    if hits and isinstance(hits, list) and len(hits) > 0:
                        result["rung"] = "graph"
                        result["hits"] = _finalize_hits(hits, symbol, root_dir)
                        result["graph_raw"] = payload
                        _mark_rung(
                            "graph", "succeeded",
                            attempted_tool="codebase__search_graph",
                            selected_tool="codebase__search_graph",
                        )
                        if staleness_info:
                            result["_graph_staleness"] = staleness_info
                            result["fallback_reason"] = (
                                "graph_stale_warning_present_but_had_hits"
                            )
                        return result

                    # Zero results from search_graph with a project_id.
                    # 9033914e: if the payload looks like an app-level error,
                    # surface it.  Otherwise, when a project_id was passed and
                    # got zero hits, retry WITHOUT the project_id as a best-
                    # effort fallback — the codebase-memory-mcp slug derived
                    # from repo_path may not match the caller's project_id
                    # slug exactly (confirmed cause: graph reports graph_empty
                    # immediately after a successful index_repository because
                    # the planning project_id doesn't match the repo-path
                    # slug that index_repository auto-assigned).
                    if app_err is not None:
                        # 1579bc1e — an app-level error can be the very same
                        # slug mismatch (e.g. "project not found") rather
                        # than a genuinely-unindexed repo; try the broad
                        # retry before reporting it as a bare graph_error.
                        if await _broad_retry_without_project_id(app_err):
                            if staleness_info:
                                result["_graph_staleness"] = staleness_info
                            return result
                        result["fallback_reason"] = f"graph_error: {app_err}"
                        _mark_rung(
                            "graph", "error",
                            attempted_tool="codebase__search_graph",
                            error=f"app_error: {app_err}",
                            error_kind="runtime_error",
                        )
                    elif project_id and graph_args.get("project_id"):
                        # Retry without project_id — broader search.
                        broad_args: dict[str, Any] = {"query": symbol, "limit": limit}
                        broad_result = await _tunnel_mod.call_tunnel_tool(
                            tenant_id, "codebase__search_graph", broad_args,
                        )
                        if broad_result is not None:
                            broad_payload = _tunnel_mod._extract_graph_matches(broad_result)
                            broad_hits = _extract_hits(broad_payload)
                            if broad_hits and isinstance(broad_hits, list) and len(broad_hits) > 0:
                                result["rung"] = "graph"
                                result["hits"] = _finalize_hits(broad_hits, symbol, root_dir)
                                result["graph_raw"] = broad_payload
                                _mark_rung(
                                    "graph", "succeeded",
                                    attempted_tool="codebase__search_graph",
                                    selected_tool="codebase__search_graph (broad retry)",
                                )
                                # Surface the project_id mismatch as a note.
                                result["fallback_reason"] = (
                                    f"graph_project_id_mismatch: search_graph "
                                    f"with project_id={project_id!r} returned "
                                    f"zero results; broad search (no project_id) "
                                    f"succeeded — the caller's project_id may not "
                                    f"match the repo-path slug auto-assigned by "
                                    f"index_repository"
                                )
                                if staleness_info:
                                    result["_graph_staleness"] = staleness_info
                                return result
                        # Broad search also empty.
                        result["fallback_reason"] = (
                            "graph_stale" if staleness_info else "graph_empty"
                        )
                        _mark_rung(
                            "graph", "empty",
                            attempted_tool="codebase__search_graph",
                            reason=result["fallback_reason"],
                        )
                    else:
                        # Fall through to Serena.
                        result["fallback_reason"] = (
                            "graph_stale" if staleness_info else "graph_empty"
                        )
                        _mark_rung(
                            "graph", "empty",
                            attempted_tool="codebase__search_graph",
                            reason=result["fallback_reason"],
                        )
                    if staleness_info:
                        result["_graph_staleness"] = staleness_info
            else:
                _mark_rung("graph", "skipped", reason="no_active_tunnel")
        except Exception as _e:  # noqa: BLE001 — fallback must never raise
            result["fallback_reason"] = f"graph_error: {_e}"
            _mark_rung(
                "graph", "error",
                attempted_tool="codebase__search_graph",
                error=f"{type(_e).__name__}: {_e}",
                error_kind=_classify_error(_e),
            )

    elif stale_graph:
        if commit_drift:
            result["fallback_reason"] = (
                "graph_skipped_commit_drift_detected: "
                f"{commit_drift['commits_since_index']} commit(s) since last index "
                f"({commit_drift['stored_commit'][:12]} -> {commit_drift['head_commit'][:12]})"
            )
            _mark_rung("graph", "skipped", reason=result["fallback_reason"])
        else:
            result["fallback_reason"] = "graph_skipped_stale_graph=true"
            _mark_rung("graph", "skipped", reason=result["fallback_reason"])

    else:
        # d5e60791 — the ONE remaining silent gap: not stale_graph and no
        # tenant_id at all (e.g. a self-hosted call with no active tunnel
        # context). Neither branch above fires; make that explicit instead
        # of leaving rungs["graph"] at "not_attempted" with no explanation.
        _mark_rung("graph", "skipped", reason="no_tenant_id")

    # ------------- Rung 2: extractor__find_symbol / find_declaration ------
    _SERENA_TOOLS = "extractor__find_symbol, extractor__find_declaration"
    if tenant_id:
        try:
            from .routes import tunnel as _tunnel_mod  # noqa: PLC0415
            if _tunnel_mod.has_active_tunnel(tenant_id):
                _mark_rung("serena", "attempted", attempted_tool=_SERENA_TOOLS)
                # Try find_symbol first, then find_declaration.
                serena_hits: list[Any] = []
                serena_errors: list[str] = []
                serena_tool_used: "str | None" = None
                for serena_tool in ("extractor__find_symbol", "extractor__find_declaration"):
                    try:
                        s_result = await _tunnel_mod.call_tunnel_tool(
                            tenant_id, serena_tool,
                            {"symbol_name": symbol, "limit": limit},
                        )
                        if s_result is not None:
                            s_payload = _tunnel_mod._extract_graph_matches(s_result)
                            if isinstance(s_payload, list):
                                serena_hits = s_payload
                            elif isinstance(s_payload, dict):
                                serena_hits = (
                                    s_payload.get("results")
                                    or s_payload.get("matches")
                                    or s_payload.get("hits")
                                    or []
                                )
                            # Normalise to list.
                            if not isinstance(serena_hits, list):
                                serena_hits = []
                    except Exception as _serena_exc:  # noqa: BLE001
                        # d5e60791 — record the real error instead of
                        # silently discarding it (was a bare `serena_hits =
                        # []` with no trace); still try the next tool.
                        serena_errors.append(
                            f"{serena_tool}: {type(_serena_exc).__name__}: {_serena_exc}"
                        )
                        serena_hits = []
                    if serena_hits:
                        serena_tool_used = serena_tool
                        break
                if serena_hits:
                    result["rung"] = "serena"
                    result["hits"] = _finalize_hits(serena_hits, symbol, root_dir)[:limit]
                    _mark_rung(
                        "serena", "succeeded",
                        attempted_tool=_SERENA_TOOLS,
                        selected_tool=serena_tool_used,
                    )
                    return result
                if serena_errors:
                    _mark_rung(
                        "serena", "error",
                        attempted_tool=_SERENA_TOOLS,
                        error="; ".join(serena_errors),
                        error_kind="runtime_error",
                    )
                else:
                    _mark_rung("serena", "empty", attempted_tool=_SERENA_TOOLS)
            else:
                _mark_rung("serena", "skipped", reason="no_active_tunnel")
        except Exception as _serena_outer_exc:  # noqa: BLE001 — fallback chain never raises
            # d5e60791 — this used to be a bare `pass`, silently discarding
            # whatever failed here (e.g. the tunnel import itself, or
            # has_active_tunnel raising) with zero diagnostic. Record it
            # truthfully instead.
            _mark_rung(
                "serena", "error",
                attempted_tool=_SERENA_TOOLS,
                error=f"{type(_serena_outer_exc).__name__}: {_serena_outer_exc}",
                error_kind=_classify_error(_serena_outer_exc),
            )
    else:
        _mark_rung("serena", "skipped", reason="no_tenant_id")

    # ------------- Rung 3: search_code_semantic BM25 ----------------------
    if root_dir:
        _mark_rung("semantic", "attempted", attempted_tool="search_code_semantic")
        try:
            from . import code_index as _code_index  # noqa: PLC0415
            from . import hardening as _hardening  # noqa: PLC0415
            db_path = ":memory:"
            if data_dir:
                db_path = os.path.join(data_dir, "code_index.duckdb")
            sem_result = await _hardening.run_in_bulkhead(
                _code_index.search_code_semantic,
                root_dir,
                symbol,
                limit=limit,
                kind=kind,
                db_path=db_path,
                reindex=True,
                label="prospect_symbol_semantic",
            )
            if isinstance(sem_result, dict) and sem_result.get("error"):
                # search_code_semantic degrades to {"error": "..."} instead
                # of raising (e.g. root_dir missing, hosted-mode guard,
                # timeout) — treat that the same as a raised exception so it
                # is never silently mistaken for "zero hits, nothing wrong".
                result["fallback_reason"] = f"semantic_error: {sem_result['error']}"
                _mark_rung(
                    "semantic", "error",
                    attempted_tool="search_code_semantic",
                    error=str(sem_result["error"]),
                    error_kind="runtime_error",
                )
            else:
                sem_hits = (sem_result or {}).get("hits") or []
                if sem_hits:
                    result["rung"] = "semantic"
                    result["hits"] = _finalize_hits(sem_hits, symbol, root_dir)
                    result["semantic_raw"] = sem_result
                    _mark_rung(
                        "semantic", "succeeded",
                        attempted_tool="search_code_semantic",
                        selected_tool="search_code_semantic",
                    )
                    return result
                _mark_rung("semantic", "empty", attempted_tool="search_code_semantic")
        except Exception as _sem_exc:  # noqa: BLE001
            # d5e60791 — THE bug this item fixes: this used to be a bare
            # `pass`, which meant a ModuleNotFoundError for
            # 'meridian_codeindex' (the confirmed live failure — the
            # extracted extensions/meridian-codeindex package not being
            # importable in this runtime) silently collapsed into
            # rung="none", fallback_reason=None with NO trace of what went
            # wrong. Record the real exception — type, message, and whether
            # it looks like a missing/broken dependency vs. some other
            # runtime failure — both on this rung's own diagnostic entry and
            # (since this is the last rung) as the top-level fallback_reason
            # so a caller that only looks at fallback_reason still sees it.
            _error_kind = _classify_error(_sem_exc)
            _error_text = f"{type(_sem_exc).__name__}: {_sem_exc}"
            result["fallback_reason"] = f"semantic_error: {_error_text}"
            _mark_rung(
                "semantic", "error",
                attempted_tool="search_code_semantic",
                error=_error_text,
                error_kind=_error_kind,
            )
    else:
        _mark_rung("semantic", "skipped", reason="no_root_dir")

    # All rungs exhausted (rung stays "none"). d5e60791 — never return here
    # with fallback_reason still None: synthesize one from the per-rung
    # diagnostics so a caller ALWAYS has something actionable to look at,
    # even when every rung failed for a different reason. Only fires when
    # nothing upstream already set a specific fallback_reason (e.g.
    # "graph_empty"), so existing exact-string callers are unaffected.
    if result["rung"] == "none" and not result["fallback_reason"]:
        _summary = []
        for _rung_name in ("graph", "serena", "semantic"):
            _entry = result["rungs"][_rung_name]
            _piece = f"{_rung_name}={_entry['status']}"
            if _entry.get("error"):
                _piece += f" ({_entry['error']})"
            elif _entry.get("reason"):
                _piece += f" ({_entry['reason']})"
            _summary.append(_piece)
        result["fallback_reason"] = "all_rungs_missed: " + "; ".join(_summary)

    return result


def _normalize_prospect_hit(
    hit: Any, qualified_name: str, *, resolution_source: str | None = None,
) -> dict[str, Any]:
    """Normalise ONE prospect_symbol_impl hit into the ``{qualified_name, file,
    ...}`` shape :func:`pointers._resolve_symbol` expects.

    Hit shape varies by which rung produced it: ``graph``/``serena`` hits
    typically carry ``qualified_name``/``file`` (or ``name``), while
    ``semantic`` (search_code_semantic) hits carry ``name``/``path`` instead.
    Preserves every original key (callers that want the raw hit still get it)
    and only backfills ``qualified_name``/``file`` when missing/falsy so
    :func:`pointers._resolve_symbol`'s exact-match + ``file`` read never KeyErrors
    or silently mismatches across rungs.

    ``resolution_source`` (eb8b6894) — when given, stamped onto the
    normalized hit (unless the raw hit already carries one) so
    :func:`pointers._resolve_symbol` can report explicitly whether this match
    came from the LIVE tunnel-connected graph or a fallback — see
    :func:`build_symbol_resolver`.
    """
    if not isinstance(hit, dict):
        base: dict[str, Any] = {"qualified_name": qualified_name}
        if resolution_source:
            base["resolution_source"] = resolution_source
        return base
    out = dict(hit)
    if resolution_source and not out.get("resolution_source"):
        out["resolution_source"] = resolution_source
    if not out.get("qualified_name"):
        out["qualified_name"] = (
            hit.get("name_path") or hit.get("name") or qualified_name
        )
    if not out.get("file"):
        out["file"] = hit.get("relative_path") or hit.get("path") or hit.get("uri")
    return out


def build_symbol_resolver(
    *,
    tenant: "dict[str, Any] | None" = None,
    root_dir: str = "",
    data_dir: str = "",
) -> "Callable[..., Awaitable[list[dict[str, Any]]]]":
    """653579c5 — a ``pointers.resolve_pointer``-compatible ``symbol_resolver``
    that tries the LIVE three-rung :func:`prospect_symbol_impl` chain first,
    falling back to the local cached-snapshot search
    (``db.search_graph_entities``) that was previously the ONLY thing
    ``resolve_pointer``'s default resolver ever consulted.

    Root cause this fixes: ``resolve_sprint_item_pointers`` (mcp/handlers/
    sprint_tools.py) already has ``tenant`` in scope (it's a parameter of the
    handler) but never threaded it anywhere — ``pointers.resolve_pointer`` was
    always called with its DEFAULT ``symbol_resolver``, which only queries the
    ``codebase_graph_entities`` snapshot table. That table has exactly zero
    production writers (``db.upsert_graph_entities`` is only ever called from
    tests / the c00b1ccf opt-in snapshot feature that was never wired to a
    refresh path — see graph_snapshot.py's own "refreshed from the tunnel when
    available (deferred)" docstring), so a symbol pointer could NEVER resolve
    through the default path even when the SAME tenant's live tunnel-connected
    code graph (the one ``prospect_symbol`` itself reaches, and that a direct
    ``codebase__search_graph`` call resolves instantly) had the answer.

    Threading ``tenant`` (and an optional ``root_dir``, for the semantic
    fallback rung) into a resolver built from :func:`prospect_symbol_impl`
    gives ``resolve_sprint_item_pointers`` the exact same reach as
    ``prospect_symbol`` — graph → Serena → semantic — while still degrading
    gracefully to the old cached-snapshot behaviour (never regressing existing
    callers) when no tenant/tunnel/root_dir is available, e.g. self-hosted
    sessions with no active code tunnel.

    Never raises: both the prospect attempt and the snapshot fallback are
    fully guarded, matching every other resolver seam in this module.
    """
    async def _resolver(
        db: Any, project_id: "str | None", qualified_name: "str | None", limit: int,
    ) -> list[dict[str, Any]]:
        qn = qualified_name or ""
        if qn:
            try:
                result = await prospect_symbol_impl(
                    symbol=qn,
                    project_id=project_id or "",
                    root_dir=root_dir,
                    limit=limit,
                    kind=None,
                    stale_graph=False,
                    tenant=tenant,
                    data_dir=data_dir,
                )
                hits = result.get("hits") or []
                if hits:
                    # eb8b6894 — rung "graph"/"serena" are both LIVE
                    # tunnel-connected lookups (codebase__search_graph /
                    # extractor__find_symbol); "semantic" is a local BM25
                    # fallback (no tunnel needed, but not the live graph
                    # either). Reuses prospect_symbol_impl's OWN "rung"
                    # label rather than inventing a new distinction.
                    _rung = result.get("rung")
                    _source = "live_graph" if _rung in ("graph", "serena") else "local_fallback"
                    return [
                        _normalize_prospect_hit(h, qn, resolution_source=_source)
                        for h in hits
                    ]
            except Exception:  # noqa: BLE001 — fall through to the snapshot search
                pass
        try:
            from .db import search_graph_entities as _sg  # noqa: PLC0415
            _snapshot_hits = await _sg(db, project_id or "", qn, limit)
            # eb8b6894 — this is the SAME production-empty codebase_graph_entities
            # snapshot table pointers.py's own default symbol_resolver falls
            # back to (see its docstring) — tag it identically so a caller
            # never mistakes "matched the stale snapshot" for "matched the
            # live graph".
            return [
                _normalize_prospect_hit(h, qn, resolution_source="stale_snapshot")
                for h in (_snapshot_hits or [])
            ]
        except Exception:  # noqa: BLE001 — resolver seam must never raise
            return []

    return _resolver
