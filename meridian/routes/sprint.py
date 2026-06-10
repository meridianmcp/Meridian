"""Sprint items routes — extracted from server.py."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from .._deps import _db, _get_tenant_from_request, validate_input_size
from .. import db as db_module

router = APIRouter()


@router.get("/projects/{project_id}/sprint-items")
async def list_sprint_items(
    project_id: str, request: Request, status: str | None = None
) -> list[dict[str, Any]]:
    """List sprint items, optionally filtered by status."""
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    try:
        return await db_module.get_sprint_items(
            await _db(request), project_id, status=status
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.post("/projects/{project_id}/sprint-items", status_code=201)
async def add_sprint_item_endpoint(
    project_id: str, body: dict[str, Any], request: Request
) -> dict[str, Any]:
    """Append a todo sprint item. Body: ``{version, title, group?, human_id?}``."""
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    version = (body.get("version") or "").strip()
    title = (body.get("title") or "").strip()
    if not version or not title:
        raise HTTPException(status_code=422, detail="version and title are required")
    validate_input_size(title, "sprint item title", 500)
    group = body.get("group") or body.get("item_group") or None
    human_id = body.get("human_id") or None
    depends_on = body.get("depends_on") or None
    failure_mode = body.get("failure_mode") or None
    # G4.15 — safety limit
    from .. import limits as _limits  # noqa: PLC0415
    existing = await db_module.get_sprint_items(await _db(request), project_id)
    _limits.check_sprint_items_per_project(len(existing))
    return await db_module.add_sprint_item(
        await _db(request), project_id, version, title,
        group=group, human_id=human_id,
        depends_on=depends_on, failure_mode=failure_mode,
    )


@router.post("/projects/{project_id}/sprint-items/{item_id}/complete")
async def complete_sprint_item_endpoint(
    project_id: str, item_id: str, request: Request,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Mark a sprint item ``done``. Optional body: ``{task_id}``."""
    db = await _db(request)
    item = await db_module.complete_sprint_item(
        db, project_id, item_id,
        task_id=(body or {}).get("task_id"),
    )
    if item is None:
        raise HTTPException(status_code=404, detail="sprint item not found")
    # Lazy import to avoid circular dependency on server.py at module level.
    try:
        from meridian.server import _update_roadmap_version_history, _REPO_ROOT  # noqa: PLC0415
        await _update_roadmap_version_history(
            db, project_id, item["version"], _REPO_ROOT
        )
    except Exception:  # noqa: BLE001
        pass
    try:
        active_statuses = {"pending", "todo", "in_progress"}
        remaining = await db_module.get_sprint_items(db, project_id)
        if not any((row.get("status") or "") in active_statuses for row in remaining):
            from meridian.server import _maybe_notify  # noqa: PLC0415

            tenant = await _get_tenant_from_request(request)
            await _maybe_notify(
                db,
                project_id,
                "Sprint done ✓",
                "All sprint items are complete.",
                event="sprint_done",
                tenant=tenant,
                pref_key="sprint",
            )
    except Exception:  # noqa: BLE001
        pass
    return item


@router.post("/projects/{project_id}/sprint-items/{item_id}/skip")
async def skip_sprint_item_endpoint(
    project_id: str, item_id: str, request: Request,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Mark a sprint item ``skipped``. Optional body: ``{reason}``."""
    item = await db_module.skip_sprint_item(
        await _db(request), project_id, item_id,
        reason=(body or {}).get("reason"),
    )
    if item is None:
        raise HTTPException(status_code=404, detail="sprint item not found")
    return item


@router.post("/projects/{project_id}/sprint-items/{item_id}/fail")
async def fail_sprint_item_endpoint(
    project_id: str, item_id: str, request: Request,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Mark a sprint item ``failed``. Optional body: ``{reason}``."""
    item = await db_module.fail_sprint_item(
        await _db(request), project_id, item_id,
        reason=(body or {}).get("reason"),
    )
    if item is None:
        raise HTTPException(status_code=404, detail="sprint item not found")
    return item


@router.delete("/projects/{project_id}/sprint-items/{item_id}", status_code=204)
async def delete_sprint_item_endpoint(
    project_id: str, item_id: str, request: Request
) -> None:
    """Delete a sprint item permanently."""
    db = await _db(request)
    await db.execute(
        "DELETE FROM sprint_items WHERE id = ? AND project_id = ?",
        (item_id, project_id),
    )
    await db.commit()


@router.patch("/projects/{project_id}/sprint-items/{item_id}")
async def patch_sprint_item_endpoint(
    project_id: str, item_id: str, body: dict[str, Any], request: Request
) -> dict[str, Any]:
    """Update editable fields (title, version) of a sprint item."""
    title = body.get("title")
    if title is not None:
        title = title.strip()
        if not title:
            raise HTTPException(status_code=422, detail="title cannot be empty")
        validate_input_size(title, "sprint item title", 500)
    version = body.get("version")
    if version is not None:
        version = version.strip() or None
    status = body.get("status")
    if status is not None and status not in {"pending", "indeterminate"}:
        raise HTTPException(status_code=422, detail="status patch only supports 'pending' or 'indeterminate'")
    feedback_thumb = body.get("feedback_thumb")
    if feedback_thumb is not None:
        try:
            feedback_thumb = int(feedback_thumb)
            if feedback_thumb not in (-1, 1):
                raise ValueError
        except (ValueError, TypeError):
            raise HTTPException(status_code=422, detail="feedback_thumb must be -1 or 1")
    feedback_note = body.get("feedback_note")
    item = await db_module.patch_sprint_item(
        await _db(request), project_id, item_id, title=title, version=version,
        status=status,
        feedback_thumb=feedback_thumb, feedback_note=feedback_note,
    )
    if item is None:
        raise HTTPException(status_code=404, detail="sprint item not found")
    return item


@router.post("/projects/{project_id}/sprint-items/{item_id}/push")
async def push_sprint_item_endpoint(
    project_id: str, item_id: str, body: dict[str, Any], request: Request
) -> dict[str, Any]:
    """Push a sprint item to a future version. Body: ``{to_version}``."""
    to_version = (body.get("to_version") or "").strip()
    if not to_version:
        raise HTTPException(status_code=422, detail="to_version is required")
    try:
        item = await db_module.push_sprint_item(
            await _db(request), project_id, item_id, to_version
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if item is None:
        raise HTTPException(status_code=404, detail="sprint item not found")
    return item
