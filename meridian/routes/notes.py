"""Project notes (per-project wiki) routes — extracted from server.py."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from .._deps import _db, validate_input_size, _MANUAL_NOTE_LINT
from .. import db as db_module

router = APIRouter()


@router.get("/projects/{project_id}/notes")
async def list_project_notes_endpoint(
    project_id: str,
    request: Request,
    tag: str | None = None,
    query: str | None = None,
    paginate: bool = False,
    limit: int = 100,
    cursor: int = 0,
) -> list[dict[str, Any]] | dict[str, Any]:
    """Project notes (newest first). ``?tag=X`` filters by tag; ``?query=X`` searches title+body.

    5a5bba43 — the dashboard Notes tab renders full note bodies, so this HTTP
    endpoint returns the complete rows (``bodies=True``) for backward compat.
    The MCP ``get_notes`` tool defaults to the lightweight (no-body) list.

    9fa119dd — pass ``?paginate=true`` for the cursor "Load More" envelope
    ``{notes, has_more, next_cursor}`` (default ``limit=100``, ``cursor`` is the
    next offset), mirroring the sprint-items ``?page=`` envelope. Without
    ``paginate`` the legacy bare-list shape is unchanged.
    """
    db = await _db(request)
    project = await db_module.get_project(db, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    if paginate:
        return await db_module.get_project_notes_page(
            db, project_id, tag=tag, query=query, bodies=True,
            limit=limit, cursor=cursor,
        )
    return await db_module.get_project_notes(
        db, project_id, tag=tag, query=query, bodies=True
    )


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
    validate_input_size(title, "note title", 500)
    validate_input_size(text, "note body", 10_000_000)
    # G4.15 — safety limit
    from .. import limits as _limits  # noqa: PLC0415
    existing = await db_module.get_project_notes(await _db(request), project_id)
    _limits.check_notes_per_project(len(existing))
    note = await db_module.add_project_note(
        await _db(request), project_id, title, text, body.get("tags"),
        kind=body.get("kind"),
    )
    # e5592013 — non-blocking lint: "MANUAL" notes are usually human tasks.
    if isinstance(note, dict) and "MANUAL" in title:
        note = {**note, "lint": _MANUAL_NOTE_LINT}
    return note


@router.patch("/projects/{project_id}/notes/{note_id}")
async def update_project_note_endpoint(
    project_id: str, note_id: str, body: dict[str, Any], request: Request
) -> dict[str, Any]:
    """Patch title/body/tags."""
    if body.get("title") is not None:
        validate_input_size(body.get("title"), "note title", 500)
    if body.get("body") is not None:
        validate_input_size(body.get("body"), "note body", 10_000_000)
    result = await db_module.update_project_note(
        await _db(request), note_id,
        title=body.get("title"),
        body=body.get("body"),
        tags=body.get("tags"),
        priority=body.get("priority"),
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
