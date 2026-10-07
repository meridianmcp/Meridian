"""Human-in-the-loop (HITL) routes — extracted from server.py."""
from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from .. import _deps
from .._deps import _db, _deny_unless_in_scope, _get_tenant_from_request
from .. import db as db_module

router = APIRouter()

# urgency order used by db.list_hitl_requests; reused when merging per-project lists.
_URGENCY_RANK = {"blocking": 0, "high": 1}


@router.get("/hitl")
async def list_all_hitl(
    request: Request, status: str = "pending", limit: int = 50
) -> list[dict[str, Any]]:
    """Pending HITL requests across all projects (top-level dashboard panel).

    RT-TI-005 (wave 2) — a project-scoped member only sees HITL requests of the
    projects in their scope (pinned decision 6fe5210c). Owners, workspace-wide
    members, self-hosted and demo callers see every project, as before.
    """
    db = await _db(request)
    _status = status if status != "all" else None
    scoped = await _deps._scoped_project_ids_for_request(request)
    try:
        if scoped is None:
            return await db_module.list_hitl_requests(db, None, status=_status, limit=limit)
        # One query per scoped project (a scope is a handful of ids), merged back
        # into the same order the single query uses, so the LIMIT applies to the
        # caller's own requests and not to rows that would be filtered out.
        merged: list[dict[str, Any]] = []
        for pid in dict.fromkeys(scoped):
            merged.extend(
                await db_module.list_hitl_requests(db, pid, status=_status, limit=limit)
            )
        merged.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
        merged.sort(key=lambda r: _URGENCY_RANK.get(r.get("urgency"), 2))
        return merged[:limit] if limit >= 0 else merged
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
    # RT-TI-005 (pass 3, F-D6) -- a request is filed against the project in the path; a
    # session of another project must not be attached to it (404, like POST /tasks).
    _hitl_sid = body.get("session_id")
    if isinstance(_hitl_sid, str):
        await _deps._reject_foreign_session(db, _hitl_sid, project_id)
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
    # RT-TI-005 (wave 2) — /hitl/{id} is outside the /projects/{uuid} middleware, so
    # the request's own project decides whether a project-scoped caller may see it.
    scoped = await _deps._scoped_project_ids_for_request(request)
    r = await db_module.get_hitl_request(await _db(request), request_id)
    if r is None:
        _deny_unless_in_scope(scoped, None)  # scoped callers: same 403 as a foreign id
        raise HTTPException(status_code=404, detail="hitl request not found")
    _deny_unless_in_scope(scoped, r.get("project_id"))
    return r


@router.patch("/hitl/{request_id}")
async def patch_hitl_endpoint(
    request_id: str, body: dict[str, Any], request: Request
) -> dict[str, Any]:
    """Answer or dismiss a HITL request."""
    db = await _db(request)
    # RT-TI-005 (wave 2) — answering/dismissing applies side effects (an approved
    # gate-override or md_section_update HITL acts on its own project), so a
    # project-scoped caller must own the request's project BEFORE anything runs.
    # Owners / self-hosted / demo callers resolve no scope and pay no extra SELECT.
    scoped = await _deps._scoped_project_ids_for_request(request)
    if scoped is not None:
        existing = await db_module.get_hitl_request(db, request_id)
        _deny_unless_in_scope(scoped, existing.get("project_id") if existing else None)
    action = (body.get("action") or "answer").lower()
    if action == "answer":
        answer = body.get("answer", "").strip()
        if not answer:
            raise HTTPException(status_code=400, detail="answer required")
        # Funnel through the server chokepoint so an approved md_section_update
        # HITL actually writes its file (same single path as the MCP tool).
        from meridian.server import _answer_hitl_and_apply  # noqa: PLC0415

        tenant = await _get_tenant_from_request(request)
        result = await _answer_hitl_and_apply(
            db, request_id, answer,
            answered_by=body.get("answered_by"), approved=True,
            tenant=tenant,
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
