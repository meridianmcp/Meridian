"""2ce5bc76 — robust symbol prospecting with a three-rung fallback chain.

Extracted here (not in mcp/handler.py) so the implementation is importable
without triggering the handler→server circular import. Tests import directly
from this module; handler.py imports and calls prospect_symbol_impl.
"""
from __future__ import annotations

import os
from typing import Any


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
                    hits = []
                    if isinstance(payload, dict):
                        hits = (
                            payload.get("results")
                            or payload.get("matches")
                            or payload.get("hits")
                            or []
                        )
                    elif isinstance(payload, list):
                        hits = payload
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
                    # Zero results — fall through to Serena.
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
