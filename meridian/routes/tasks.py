"""Task log + claim/release routes — extracted from server.py."""
from __future__ import annotations

from typing import Any

import aiosqlite
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from .._deps import _db
from .. import db as db_module
from ..models import ClaimTaskRequest, ClaimTaskResponse, Task, TaskCreate, TaskUpdate

router = APIRouter()


# ---------------------------------------------------------------------------
# Helper (previously in server.py)
# ---------------------------------------------------------------------------

async def _claim_task_result(
    db: aiosqlite.Connection,
    project_id: str,
    task_or_item_id: str,
    session_id: str,
) -> dict[str, Any]:
    """Claim a task row, resolving sprint-item ids when provided."""
    sprint_item = await db_module.get_sprint_item(db, task_or_item_id)
    task = None
    sprint_item_id: str | None = None
    if sprint_item is not None and sprint_item.get("project_id") == project_id:
        sprint_item_id = sprint_item["id"]
    else:
        task = await db_module.get_task(db, task_or_item_id)
        if task is None or task.get("project_id") != project_id:
            task = None
        elif task.get("sprint_item_id"):
            sprint_item_id = task["sprint_item_id"]
            sprint_item = await db_module.get_sprint_item(db, sprint_item_id)

    if sprint_item_id:
        blocking = await db_module.get_blocking_dependency_for_sprint_item(
            db, sprint_item_id
        )
        if blocking is not None:
            return {
                "task_id": task["id"] if task else task_or_item_id,
                "claimed": False,
                "claimed_by": task["claimed_by"] if task else None,
                "sprint_item_id": sprint_item_id,
                "error": "dependency_not_met",
                "blocking_item_id": blocking["id"],
                "blocking_item_title": blocking.get("title"),
            }
        if task is None:
            task = await db_module.get_open_task_for_sprint_item(db, sprint_item_id)
        if task is None:
            assert sprint_item is not None
            task = await db_module.log_task(
                db, session_id, project_id,
                sprint_item["title"], "pending",
                sprint_item_id=sprint_item_id,
            )
    elif task is None:
        return {"task_id": task_or_item_id, "claimed": False, "claimed_by": None}

    claimed = await db_module.claim_task(db, task["id"], session_id)
    if claimed is None:
        existing = await db_module.get_task(db, task["id"])
        return {
            "task_id": task["id"],
            "claimed": False,
            "claimed_by": existing["claimed_by"] if existing else None,
            "sprint_item_id": sprint_item_id,
        }
    if sprint_item_id and sprint_item is not None and sprint_item.get("status") in ("pending", "todo"):
        await db_module.start_sprint_item(db, project_id, sprint_item_id)
    return {
        "task_id": claimed["id"],
        "claimed": True,
        "claimed_by": claimed["claimed_by"],
        "sprint_item_id": sprint_item_id,
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/projects/{project_id}/tasks", response_model=list[Task])
async def get_tasks(
    project_id: str, request: Request, limit: int = 20
) -> list[dict[str, Any]]:
    """List recent tasks for a project, newest first."""
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return await db_module.get_tasks(await _db(request), project_id, limit=limit)


@router.get("/projects/{project_id}/sessions/{session_id}/tasks/live")
async def get_session_tasks_live(
    project_id: str,
    session_id: str,
    request: Request,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Return the last N task_log rows for a session — live Queue feed.

    Used by the dashboard's "Currently Running" section to show what a
    running Claude Code session is doing in real-time (polling every 5s).
    """
    db = await _db(request)
    async with db.execute(
        "SELECT t.*, s.name AS session_name, s.human_id AS human_id "
        "FROM task_log t "
        "LEFT JOIN sessions s ON s.id = t.session_id "
        "WHERE t.project_id = ? AND t.session_id = ? "
        "ORDER BY t.created_at DESC, t.rowid DESC LIMIT ?",
        (project_id, session_id, limit),
    ) as cur:
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


@router.get("/projects/{project_id}/tasks/search")
async def search_tasks_http(
    project_id: str, request: Request, q: str = "", limit: int = 5
) -> list[dict[str, Any]]:
    """Text search over task descriptions."""
    if not q:
        return []
    return await db_module.search_tasks(await _db(request), project_id, q, limit)


@router.get("/projects/{project_id}/tasks/claimable", response_model=list[Task])
async def get_claimable_tasks(
    project_id: str, request: Request, limit: int = 20
) -> list[dict[str, Any]]:
    """List unclaimed pending tasks for a project."""
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return await db_module.get_claimable_tasks(await _db(request), project_id, limit=limit)


@router.post("/projects/{project_id}/tasks/claim", response_model=ClaimTaskResponse)
async def claim_task_endpoint(
    project_id: str, body: ClaimTaskRequest, request: Request
) -> dict[str, Any]:
    """Atomically claim a pending task. Returns ``claimed=False`` when
    another worker holds the lock."""
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return await _claim_task_result(
        await _db(request), project_id, body.task_id, body.session_id
    )


@router.post("/projects/{project_id}/tasks/release")
async def release_task_endpoint(
    project_id: str, body: ClaimTaskRequest, request: Request
) -> dict[str, Any]:
    """Release a previously-claimed task."""
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    released = await db_module.release_task(
        await _db(request), body.task_id, body.session_id
    )
    if not released:
        raise HTTPException(status_code=404, detail="task not claimed by this session")
    return {"task_id": body.task_id, "released": True}


@router.post("/tasks", response_model=Task, status_code=201)
async def create_task(body: TaskCreate, request: Request) -> dict[str, Any]:
    """Append a task-log entry."""
    _req_db = await _db(request)
    project = await db_module.get_project(_req_db, body.project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    async with _req_db.execute(
        "SELECT id FROM sessions WHERE id = ?", (body.session_id,)
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="session not found")
    return await db_module.log_task(
        _req_db, body.session_id, body.project_id,
        body.description, body.status,
        parent_task_id=body.parent_task_id,
    )


@router.patch("/tasks/{task_id}", response_model=Task)
async def patch_task(task_id: str, body: TaskUpdate, request: Request) -> dict[str, Any]:
    """Update a task's status and/or description in place."""
    existing = await db_module.get_task(await _db(request), task_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="task not found")
    try:
        updated = await db_module.update_task(
            await _db(request), task_id,
            status=body.status, description=body.description,
            project_id=existing["project_id"],
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    assert updated is not None
    return updated


@router.delete("/tasks/{task_id}", status_code=204)
async def delete_task_endpoint(task_id: str, request: Request) -> Response:
    """Hard-delete a task-log entry."""
    db = await _db(request)
    await db.execute("DELETE FROM task_log WHERE id = ?", (task_id,))
    await db.commit()
    return Response(status_code=204)
