"""Per-tool handlers for guidestones: create_guidestone, list_guidestones, load_guidestone.

Thin wrappers over :mod:`meridian.db.guidestones`, in the same shape as
:mod:`meridian.mcp.handlers.remote_task_tools`. The dispatcher has already
settled ``project_id`` from ``project_name`` (``_resolve_project_reference``)
before these run, so the handlers read ``project_id`` only.
"""
from __future__ import annotations

from typing import Any


async def handle_create_guidestone(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> dict[str, Any]:
    from meridian.db import guidestones as guidestone_db  # noqa: PLC0415

    project_id = args.get("project_id")
    if not project_id:
        return {"error": "project_id is required (or pass project_name)"}
    try:
        return await guidestone_db.create_guidestone(
            db,
            project_id,
            name=args.get("name"),
            question=args.get("question"),
            session_id=args.get("session_id"),
            experiment_id=args.get("experiment_id"),
            open_threads=args.get("open_threads"),
            next_steps=args.get("next_steps"),
            findings_limit=args.get("findings_limit"),
        )
    except ValueError as exc:
        return {"error": str(exc)}


async def handle_list_guidestones(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> dict[str, Any]:
    from meridian.db import guidestones as guidestone_db  # noqa: PLC0415

    project_id = args.get("project_id")
    if not project_id:
        return {"error": "project_id is required (or pass project_name)"}
    try:
        rows = await guidestone_db.list_guidestones(db, project_id, limit=args.get("limit"))
    except ValueError as exc:
        return {"error": str(exc)}
    return {"guidestones": rows, "count": len(rows)}


async def handle_load_guidestone(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> dict[str, Any]:
    from meridian.db import guidestones as guidestone_db  # noqa: PLC0415

    project_id = args.get("project_id")
    if not project_id:
        return {"error": "project_id is required (or pass project_name)"}
    loaded = await guidestone_db.load_guidestone(
        db, project_id, guidestone_id=args.get("guidestone_id"),
    )
    if loaded is None:
        return {"error": "no guidestone found for this project"}
    return loaded
