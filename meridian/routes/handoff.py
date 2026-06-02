"""Handoff generation route — extracted from server.py."""
from __future__ import annotations

import asyncio
import os
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from .._deps import _db, _data_dir
from .. import db as db_module
from .. import handoff as handoff_module
from ..models import HandoffResult

router = APIRouter()


@router.get("/projects/{project_id}/handoff/planner")
async def planner_handoff_endpoint(
    project_id: str, request: Request
) -> dict[str, Any]:
    """GET the planner-optimised handoff for a project.

    Returns strategic context (north star, decisions, notes, open HITLs,
    pending sprint items, recent tasks) as plain markdown. Intended for
    pasting into a claude.ai planning chat — excludes mechanical executor
    details like file paths and test commands.
    """
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    db = await _db(request)
    data_dir = _data_dir(request)
    try:
        path, content = await asyncio.wait_for(
            handoff_module.generate_handoff(
                db, project_id, data_dir, mode="planner"
            ),
            timeout=30.0,
        )
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="planner handoff timed out")
    return {"path": path, "content": content, "mode": "planner"}


@router.post("/projects/{project_id}/handoff", response_model=HandoffResult)
async def generate_handoff_endpoint(
    project_id: str, request: Request
) -> dict[str, Any]:
    """Render and write the handoff file for a project."""
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    try:
        body = await request.json()
        if not isinstance(body, dict):
            body = {}
    except Exception:
        body = {}
    session_id = body.get("session_id")
    mode = handoff_module.resolve_handoff_mode(
        body.get("mode"),
        session_id if isinstance(session_id, str) else None,
    )
    skip_summary = not os.environ.get("ANTHROPIC_API_KEY")
    db = await _db(request)
    data_dir = _data_dir(request)
    try:
        path, content = await asyncio.wait_for(
            handoff_module.generate_handoff(
                db, project_id, data_dir,
                skip_ai_summary=skip_summary,
                mode=mode,
                session_id=session_id if isinstance(session_id, str) else None,
            ),
            timeout=90.0,
        )
    except asyncio.TimeoutError:
        path, content = await handoff_module._generate_handoff_l0(db, project_id, data_dir)
        mode = "full"
    return {"path": path, "content": content, "mode": mode}
