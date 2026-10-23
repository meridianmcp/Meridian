"""Per-tool handlers for the Durable Remote Task primitive v1 (W1-E, item
32d3d5de): start_remote_task, get_remote_task_status, list_remote_tasks.

Same shape as :mod:`meridian.mcp.handlers.session_tools`'s external-job
handlers (thin wrappers over a ``meridian.db.*`` module) -- a new sibling
module rather than growing session_tools.py further, matching the precedent
set by :mod:`meridian.mcp.handlers.session_recovery_tools` and
:mod:`meridian.mcp.handlers.research_watchlist` for this same dispatch
group.
"""
from __future__ import annotations

import sys
from typing import Any

from meridian._deps import _hosted_mode as _deps_hosted_mode


def _hosted_mode() -> bool:
    """Same answer as handler.py's ``_hosted_mode`` (the name tests monkeypatch,
    as in handlers/notes_decisions.py), without importing the handler here.

    Importing ``meridian.mcp.handler`` from inside a handler call can hit a
    circular import (handler imports server, which imports handler), so this
    reads the handler's binding only when that module is already loaded and
    otherwise uses ``meridian._deps._hosted_mode``, which is what the handler
    binds to. Either way the answer comes from the MERIDIAN_HOSTED env var
    unless a test has patched the handler's name."""
    handler_mod = sys.modules.get("meridian.mcp.handler")
    patched = getattr(handler_mod, "_hosted_mode", None)
    return bool((patched or _deps_hosted_mode)())


def _hosted_refusal(tool_name: str) -> dict[str, Any]:
    """Structured refusal for the hosted tier, in the same
    ``{"error": ..., "hosted": True}`` shape handler.py's other hosted guards
    return. Remote tasks need SSH access to the caller's own hosts, which
    hosted Meridian does not have."""
    return {
        "error": (
            f"{tool_name} is not available on hosted Meridian, which has no SSH "
            "access to your hosts. Run Meridian self-hosted (from source) to use "
            "remote tasks."
        ),
        "hosted": True,
    }


async def handle_start_remote_task(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: "dict[str, Any] | None",
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: start_remote_task (32d3d5de)."""
    if _hosted_mode():
        return _hosted_refusal("start_remote_task")
    from meridian.db import remote_tasks as job_db  # noqa: PLC0415

    project_id = args["project_id"]
    result = await job_db.start_remote_task(
        db, project_id, args["session_id"],
        host=args["host"], command=args["command"],
        sprint_item_id=args.get("sprint_item_id"),
        ttl_seconds=args.get("ttl_seconds"),
    )
    return result


async def handle_get_remote_task_status(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: "dict[str, Any] | None",
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: get_remote_task_status (32d3d5de)."""
    if _hosted_mode():
        return _hosted_refusal("get_remote_task_status")
    from meridian.db import remote_tasks as job_db  # noqa: PLC0415

    project_id = args["project_id"]
    try:
        return await job_db.get_remote_task_status(db, project_id, args["job_id"])
    except ValueError as exc:
        return {"error": str(exc)}


async def handle_list_remote_tasks(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: "dict[str, Any] | None",
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: list_remote_tasks (32d3d5de)."""
    if _hosted_mode():
        return _hosted_refusal("list_remote_tasks")
    from meridian.db import remote_tasks as job_db  # noqa: PLC0415

    project_id = args["project_id"]
    jobs = await job_db.list_remote_tasks(
        db, project_id,
        session_id=args.get("session_id"),
        include_terminal=args.get("include_terminal", False),
        limit=args.get("limit", 50),
    )
    return {"jobs": jobs, "count": len(jobs)}
