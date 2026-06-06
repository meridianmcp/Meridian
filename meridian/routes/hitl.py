"""Human-in-the-loop (HITL) routes — extracted from server.py."""
from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from .._deps import _db, _get_tenant_from_request
from .. import db as db_module

router = APIRouter()


@router.get("/hitl")
async def list_all_hitl(
    request: Request, status: str = "pending", limit: int = 50
) -> list[dict[str, Any]]:
    """Pending HITL requests across all projects (top-level dashboard panel)."""
    try:
        return await db_module.list_hitl_requests(
            await _db(request), None,
            status=status if status != "all" else None,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/projects/{project_id}/hitl")
async def list_project_hitl(
    project_id: str, request: Request, status: str = "pending", limit: int = 50
) -> list[dict[str, Any]]:
    """HITL requests scoped to a single project."""
    try:
        return await db_module.list_hitl_requests(
            await _db(request), project_id,
            status=status if status != "all" else None,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/projects/{project_id}/hitl", status_code=201)
async def create_hitl_endpoint(
    project_id: str, body: dict[str, Any], request: Request
) -> dict[str, Any]:
    """Create a HITL request. Sessions paused on blocking should POST then poll
    GET /hitl/{id} until status='answered'."""
    db = await _db(request)
    project = await db_module.get_project(db, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    question = (body.get("question") or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="question required")
    try:
        result = await db_module.request_hitl(
            db, project_id, question,
            session_id=body.get("session_id"),
            context=body.get("context"),
            urgency=body.get("urgency", "normal"),
            assigned_to=body.get("assigned_to"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # v3.4 — auto-answered requests need no human; skip the notification.
    if result.get("answered_by") == "auto":
        return result
    try:
        from meridian.server import _maybe_notify  # noqa: PLC0415

        tenant = await _get_tenant_from_request(request)
        urgency = str(body.get("urgency", "normal")).upper()
        base = os.environ.get("MERIDIAN_BASE_URL", "https://usemeridian.us").rstrip("/")
        await _maybe_notify(
            db,
            project_id,
            f"Action needed ({urgency})",
            f"{question[:200]}\n\nAnswer at: {base}/dashboard",
            event="hitl",
            tenant=tenant,
            pref_key="hitl",
        )
    except Exception:  # noqa: BLE001
        pass
    return result


@router.get("/hitl/{request_id}")
async def get_hitl_endpoint(request_id: str, request: Request) -> dict[str, Any]:
    """Single HITL request lookup — sessions poll this to get the answer."""
    r = await db_module.get_hitl_request(await _db(request), request_id)
    if r is None:
        raise HTTPException(status_code=404, detail="hitl request not found")
    return r


@router.patch("/hitl/{request_id}")
async def patch_hitl_endpoint(
    request_id: str, body: dict[str, Any], request: Request
) -> dict[str, Any]:
    """Answer or dismiss a HITL request."""
    db = await _db(request)
    action = (body.get("action") or "answer").lower()
    if action == "answer":
        answer = body.get("answer", "").strip()
        if not answer:
            raise HTTPException(status_code=400, detail="answer required")
        # Funnel through the server chokepoint so an approved md_section_update
        # HITL actually writes its file (same single path as the MCP tool).
        from meridian.server import _answer_hitl_and_apply  # noqa: PLC0415

        result = await _answer_hitl_and_apply(
            db, request_id, answer,
            answered_by=body.get("answered_by"), approved=True,
        )
    elif action == "dismiss":
        result = await db_module.dismiss_hitl_request(db, request_id)
        if result is not None:
            from meridian.server import _on_hitl_answered  # noqa: PLC0415

            await _on_hitl_answered(db, result, approved=False)
    else:
        raise HTTPException(status_code=400, detail="action must be 'answer' or 'dismiss'")
    if result is None:
        raise HTTPException(status_code=404, detail="hitl request not found")
    return result
