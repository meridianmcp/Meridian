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


@router.get("/projects/{project_id}/document-structure")
async def document_structure_endpoint(
    project_id: str,
    request: Request,
    path: str,
) -> dict[str, Any]:
    """3f596f81 — heading-tree structure of an ingested .docx for the Documents
    panel. Calls the stateless docs_intel.document_outline (paragraph_count +
    heading_count + an ordered heading list). Returns ``{"error": ...}`` on a
    missing/unreadable/non-docx file rather than a 500 so the panel renders the
    failure inline.

    NOTE: ``path`` is resolved on the SERVER — this works for self-hosted / tunnel
    setups where the server can see the file; a hosted server has no access to
    the user's local filesystem, so it returns an error the panel surfaces.
    """
    db = await _db(request)
    project = await db_module.get_project(db, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    fp = (path or "").strip()
    validate_input_size(fp, "path", 2000)
    if not fp:
        return {"error": "path is required"}
    from .. import docs_intel  # local: pure-stdlib parse, cheap import
    try:
        return docs_intel.document_outline(fp)
    except FileNotFoundError:
        return {"error": f"file not found on server: {fp}"}
    except Exception as exc:  # noqa: BLE001 — surface parse errors inline
        return {"error": f"could not parse document: {exc}"}


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
    # 771c00d7 — optional code anchor (kind='code' + file_path[, symbol]).
    if body.get("file_path") is not None:
        validate_input_size(body.get("file_path"), "note file_path", 2_000)
    if body.get("symbol") is not None:
        validate_input_size(body.get("symbol"), "note symbol", 500)
    try:
        note = await db_module.add_project_note(
            await _db(request), project_id, title, text, body.get("tags"),
            kind=body.get("kind"),
            file_path=body.get("file_path"), symbol=body.get("symbol"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
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
