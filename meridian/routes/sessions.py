"""Session lifecycle routes — extracted from server.py."""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from .._deps import _db, _require_project_in_scope
from .. import db as db_module
from .. import handoff as handoff_module
from ..models import Session, SessionRegister

router = APIRouter()


async def _session_project_id(request: Request, session_id: str) -> "str | None":
    """Project a session belongs to, or ``None`` for an unknown session id."""
    _req_db = await _db(request)
    async with _req_db.execute(
        "SELECT project_id FROM sessions WHERE id = ?", (session_id,)
    ) as cur:
        row = await cur.fetchone()
    return row["project_id"] if row is not None else None


@router.post("/sessions/register", response_model=Session, status_code=201)
async def register_session(
    body: SessionRegister, request: Request
) -> dict[str, Any]:
    """Create a session row tied to a project."""
    # RT-TI-005 — the project arrives in the body, outside the /projects/{uuid}
    # middleware: a project-scoped member may only register into their own.
    await _require_project_in_scope(request, body.project_id)
    project = await db_module.get_project(await _db(request), body.project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return await db_module.register_session(
        await _db(request), body.project_id, body.name,
        human_id=body.human_id,
        agent_framework=body.agent_framework,
    )


@router.post("/sessions/{session_id}/close")
async def close_session(session_id: str, request: Request) -> dict[str, str]:
    """Mark a session closed."""
    _req_db = await _db(request)
    async with _req_db.execute(
        "SELECT id, project_id FROM sessions WHERE id = ?", (session_id,)
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="session not found")
    project_id = row["project_id"]
    await _require_project_in_scope(request, project_id)  # RT-TI-005
    await db_module.close_session(_req_db, session_id)
    try:
        await db_module.delete_session_notes(await _db(request), session_id)
    except Exception:
        pass
    try:
        await db_module.summarize_session(await _db(request), session_id)
    except Exception:
        pass
    try:
        await db_module.auto_capture_session(await _db(request), project_id, session_id)
    except Exception:
        pass
    # Lazy import to avoid circular dependency on server.py at module level.
    try:
        from meridian.server import _regenerate_claude_md, _REPO_ROOT  # noqa: PLC0415
        await _regenerate_claude_md(await _db(request), project_id, _REPO_ROOT)
    except Exception:
        pass
    # v2.5 — auto-save handoff on session close so the file is always fresh.
    async def _auto_save_handoff() -> None:
        try:
            await asyncio.wait_for(
                handoff_module.generate_handoff(
                    await _db(request), project_id, request.app.state.data_dir
                ),
                timeout=30.0,
            )
        except Exception:  # noqa: BLE001 — never block session close
            pass
    asyncio.create_task(_auto_save_handoff())
    return {"status": "closed", "session_id": session_id}


@router.patch("/sessions/{session_id}")
async def patch_session(
    session_id: str, body: dict[str, Any], request: Request
) -> dict[str, str]:
    """Update lightweight session state used by the dashboard."""
    status = (body.get("status") or "").strip()
    if status not in {"active", "idle", "closed"}:
        raise HTTPException(status_code=422, detail="status must be active, idle, or closed")
    # RT-TI-005 — /sessions/{id} is outside the /projects/{uuid} middleware, so
    # resolve the session's project and apply the scope rule here.
    _pid = await _session_project_id(request, session_id)
    if _pid is None:
        raise HTTPException(status_code=404, detail="session not found")
    await _require_project_in_scope(request, _pid)
    db = await _db(request)
    cursor = await db.execute(
        "UPDATE sessions SET status = ? WHERE id = ?",
        (status, session_id),
    )
    await db.commit()
    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="session not found")
    # 8a665a03 -- "Mark idle" / reopen used to repaint only the acting tab.
    db_module._publish_project_event(
        _pid, "session_updated", {"session_id": session_id, "status": status}
    )
    return {"status": status, "session_id": session_id}


@router.post("/sessions/{session_id}/heartbeat")
async def heartbeat_session(
    session_id: str, request: Request
) -> dict[str, str]:
    """Touch ``last_seen`` to keep this session alive.

    404 when the session id is unknown or already closed.
    """
    _pid = await _session_project_id(request, session_id)
    if _pid is not None:
        await _require_project_in_scope(request, _pid)  # RT-TI-005
    ok = await db_module.heartbeat_session(await _db(request), session_id)
    if not ok:
        raise HTTPException(status_code=404, detail="session not found")
    return {"status": "ok", "session_id": session_id}


@router.get("/sessions/{session_id}/notes")
async def get_session_notes(
    session_id: str, request: Request
) -> list[dict[str, Any]]:
    """Return sprint scratch-pad notes for a session (newest first)."""
    _pid = await _session_project_id(request, session_id)
    if _pid is not None:
        await _require_project_in_scope(request, _pid)  # RT-TI-005
    return await db_module.get_session_notes(await _db(request), session_id)
