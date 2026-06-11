"""Session lifecycle routes — extracted from server.py."""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from .._deps import _db
from .. import db as db_module
from .. import handoff as handoff_module
from ..models import Session, SessionRegister

router = APIRouter()


@router.post("/sessions/register", response_model=Session, status_code=201)
async def register_session(
    body: SessionRegister, request: Request
) -> dict[str, Any]:
    """Create a session row tied to a project."""
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
    db = await _db(request)
    cursor = await db.execute(
        "UPDATE sessions SET status = ? WHERE id = ?",
        (status, session_id),
    )
    await db.commit()
    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="session not found")
    return {"status": status, "session_id": session_id}


@router.post("/sessions/{session_id}/heartbeat")
async def heartbeat_session(
    session_id: str, request: Request
) -> dict[str, str]:
    """Touch ``last_seen`` to keep this session alive.

    404 when the session id is unknown or already closed.
    """
    ok = await db_module.heartbeat_session(await _db(request), session_id)
    if not ok:
        raise HTTPException(status_code=404, detail="session not found")
    return {"status": "ok", "session_id": session_id}
