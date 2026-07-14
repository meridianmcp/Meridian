"""2ce5bc76 — robust symbol prospecting with a three-rung fallback chain.

Extracted here (not in mcp/handler.py) so the implementation is importable
without triggering the handler→server circular import. Tests import directly
from this module; handler.py imports and calls prospect_symbol_impl.
"""
from __future__ import annotations

import os
from typing import Any


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

    Only fires when the dict has an ``"error"`` or ``"message"`` key AND has
    NONE of the recognised hit-container keys.  This avoids false-positives on
    a legitimate (but empty) result that happens to carry an ``"error"`` field.
    """
    if not isinstance(payload, dict):
        return None
    _hit_keys = frozenset({"results", "matches", "hits", "nodes", "symbols", "entities"})
    if any(k in payload for k in _hit_keys):
        return None
    msg = payload.get("error") or payload.get("message") or payload.get("detail")
    if msg and isinstance(msg, str):
        return msg.strip()
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
      stale_graph=True or when no code tunnel is active.
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

    # ------------- Rung 1: codebase__search_graph -------------------------
    if not stale_graph and tenant_id:
        try:
            from .routes import tunnel as _tunnel_mod  # noqa: PLC0415
            if _tunnel_mod.has_active_tunnel(tenant_id):
                graph_args: dict[str, Any] = {"query": symbol, "limit": limit}
                if project_id:
                    graph_args["project_id"] = project_id
                graph_result = await _tunnel_mod.call_tunnel_tool(
                    tenant_id, "codebase__search_graph", graph_args,
                )
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
