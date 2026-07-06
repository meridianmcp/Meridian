"""c00b1ccf — cached codebase-graph snapshot.

Size caps + an offline searcher that feeds handoff code-pointer enrichment from
the persisted snapshot (``codebase_graph_entities``) so a fresh planning/executor
session gets code awareness even when no live code-intel tunnel is connected.
Opt-in per project; refreshed from the tunnel when available (deferred).
"""
from __future__ import annotations

from typing import Any, Callable

DEFAULT_ENTITY_CAP = 500
MAX_ENTITY_CAP = 5000


def graph_snapshot_enabled(settings: Any) -> bool:
    """True when a project has opted into the codebase-graph snapshot."""
    if not isinstance(settings, dict):
        return False
    return bool(
        settings.get("graph_snapshot_enabled")
        or settings.get("codebase_graph_snapshot")
    )


def resolve_entity_cap(settings: Any) -> int:
    """Clamp a project's configured entity cap into [1, MAX_ENTITY_CAP];
    defaults to DEFAULT_ENTITY_CAP when unset or unparseable."""
    raw = settings.get("graph_snapshot_cap") if isinstance(settings, dict) else None
    try:
        cap = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_ENTITY_CAP
    return max(1, min(cap, MAX_ENTITY_CAP))


def make_snapshot_searcher(db: Any, project_id: str) -> Callable[[str], Any]:
    """Return an async ``searcher(query)`` over ``project_id``'s persisted
    snapshot, suitable for ``handoff.set_graph_searcher_resolver``. Degrades to
    ``[]`` on any error so it can never break handoff generation."""
    from . import db as db_module

    async def _search(query: str) -> list[dict[str, Any]]:
        try:
            return await db_module.search_graph_entities(db, project_id, query)
        except Exception:  # noqa: BLE001 — never break the handoff
            return []

    return _search
