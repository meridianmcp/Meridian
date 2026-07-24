"""2ce5bc76 — robust symbol prospecting with a three-rung fallback chain.

Extracted here (not in mcp/handler.py) so the implementation is importable
without triggering the handler→server circular import. Tests import directly
from this module; handler.py imports and calls prospect_symbol_impl.
"""
from __future__ import annotations

import os
import re
import subprocess
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
    result: dict[str, Any] = {
        "symbol": symbol,
        "rung": "none",
        "hits": [],
        "fallback_reason": None,
    }

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
                    result["hits"] = broad_hits
                    result["graph_raw"] = broad_payload
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
                        result["hits"] = hits
                        result["graph_raw"] = payload
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
                                result["hits"] = broad_hits
                                result["graph_raw"] = broad_payload
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
                    else:
                        # Fall through to Serena.
                        result["fallback_reason"] = (
                            "graph_stale" if staleness_info else "graph_empty"
                        )
                    if staleness_info:
                        result["_graph_staleness"] = staleness_info
        except Exception as _e:  # noqa: BLE001 — fallback must never raise
            result["fallback_reason"] = f"graph_error: {_e}"

    elif stale_graph:
        if commit_drift:
            result["fallback_reason"] = (
                "graph_skipped_commit_drift_detected: "
                f"{commit_drift['commits_since_index']} commit(s) since last index "
                f"({commit_drift['stored_commit'][:12]} -> {commit_drift['head_commit'][:12]})"
            )
        else:
            result["fallback_reason"] = "graph_skipped_stale_graph=true"

    # ------------- Rung 2: extractor__find_symbol / find_declaration ------
    if tenant_id:
        try:
            from .routes import tunnel as _tunnel_mod  # noqa: PLC0415
            if _tunnel_mod.has_active_tunnel(tenant_id):
                # Try find_symbol first, then find_declaration.
                serena_hits: list[Any] = []
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
                    except Exception:  # noqa: BLE001
                        serena_hits = []
                    if serena_hits:
                        break
                if serena_hits:
                    result["rung"] = "serena"
                    result["hits"] = serena_hits[:limit]
                    return result
        except Exception:  # noqa: BLE001 — fallback chain never raises
            pass

    # ------------- Rung 3: search_code_semantic BM25 ----------------------
    if root_dir:
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
            sem_hits = (sem_result or {}).get("hits") or []
            if sem_hits:
                result["rung"] = "semantic"
                result["hits"] = sem_hits
                result["semantic_raw"] = sem_result
                return result
        except Exception:  # noqa: BLE001
            pass

    # All rungs exhausted — return the (empty) result with diagnostic info.
    return result


def _normalize_prospect_hit(hit: Any, qualified_name: str) -> dict[str, Any]:
    """Normalise ONE prospect_symbol_impl hit into the ``{qualified_name, file,
    ...}`` shape :func:`pointers._resolve_symbol` expects.

    Hit shape varies by which rung produced it: ``graph``/``serena`` hits
    typically carry ``qualified_name``/``file`` (or ``name``), while
    ``semantic`` (search_code_semantic) hits carry ``name``/``path`` instead.
    Preserves every original key (callers that want the raw hit still get it)
    and only backfills ``qualified_name``/``file`` when missing/falsy so
    :func:`pointers._resolve_symbol`'s exact-match + ``file`` read never KeyErrors
    or silently mismatches across rungs.
    """
    if not isinstance(hit, dict):
        return {"qualified_name": qualified_name}
    out = dict(hit)
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
                    return [_normalize_prospect_hit(h, qn) for h in hits]
            except Exception:  # noqa: BLE001 — fall through to the snapshot search
                pass
        try:
            from .db import search_graph_entities as _sg  # noqa: PLC0415
            return await _sg(db, project_id or "", qn, limit)
        except Exception:  # noqa: BLE001 — resolver seam must never raise
            return []

    return _resolver
