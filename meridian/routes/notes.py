"""Project notes (per-project wiki) routes — extracted from server.py."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from .._deps import _db
from .. import db as db_module

router = APIRouter()


@router.get("/projects/{project_id}/notes")
async def list_project_notes_endpoint(
    project_id: str, request: Request, tag: str | None = None
) -> list[dict[str, Any]]:
    """Project notes (newest first). ``?tag=X`` filters by substring match."""
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return await db_module.get_project_notes(await _db(request), project_id, tag=tag)


@router.post("/projects/{project_id}/notes", status_code=201)
async def create_project_note_endpoint(
    project_id: str, body: dict[str, Any], request: Request
) -> dict[str, Any]:
    """Create a new note. Body: {title, body, tags?}."""
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    title = (body.get("title") or "").strip()
    text = (body.get("body") or "").strip()
    if not title or not text:
        raise HTTPException(status_code=400, detail="title and body required")
    # G4.15 — safety limit
    from .. import limits as _limits  # noqa: PLC0415
    existing = await db_module.get_project_notes(await _db(request), project_id)
    _limits.check_notes_per_project(len(existing))
    return await db_module.add_project_note(
        await _db(request), project_id, title, text, body.get("tags"),
    )


@router.patch("/projects/{project_id}/notes/{note_id}")
async def update_project_note_endpoint(
    project_id: str, note_id: str, body: dict[str, Any], request: Request
) -> dict[str, Any]:
    """Patch title/body/tags."""
    result = await db_module.update_project_note(
        await _db(request), note_id,
        title=body.get("title"),
        body=body.get("body"),
        tags=body.get("tags"),
    )
    if result is None:
        raise HTTPException(status_code=404, detail="note not found")
    return result


@router.delete("/projects/{project_id}/notes/{note_id}", status_code=204)
async def delete_project_note_endpoint(
    project_id: str, note_id: str, request: Request
) -> Response:
    """Hard-delete a note. Returns 204 or 404."""
    ok = await db_module.delete_project_note(await _db(request), note_id)
    if not ok:
        raise HTTPException(status_code=404, detail="note not found")
    return Response(status_code=204)
