"""Workspace-level routes (notes, decisions, settings) — apply across ALL
projects in a workspace. Backs the dashboard Settings → Workspace section."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from .._deps import _db, _get_tenant_from_request
from .. import db as db_module

router = APIRouter()


async def _tenant_id(request: Request) -> str | None:
    """Resolve the current tenant id for workspace isolation, or None
    (self-host / demo / unauthenticated)."""
    tenant = await _get_tenant_from_request(request)
    return tenant["id"] if tenant else None


# --- Workspace notes -------------------------------------------------------

@router.get("/workspace/notes")
async def list_workspace_notes_endpoint(
    request: Request, tag: str | None = None
) -> list[dict[str, Any]]:
    """Workspace notes (newest first). ``?tag=X`` filters by substring match."""
    return await db_module.get_workspace_notes(
        await _db(request), tag=tag, tenant_id=await _tenant_id(request)
    )


@router.post("/workspace/notes", status_code=201)
async def create_workspace_note_endpoint(
    body: dict[str, Any], request: Request
) -> dict[str, Any]:
    """Create a workspace note. Body: {title, body, tags?}."""
    title = (body.get("title") or "").strip()
    text = (body.get("body") or "").strip()
    if not title or not text:
        raise HTTPException(status_code=400, detail="title and body required")
    return await db_module.add_workspace_note(
        await _db(request), title, text, body.get("tags"),
        tenant_id=await _tenant_id(request),
    )


@router.delete("/workspace/notes/{note_id}", status_code=204)
async def delete_workspace_note_endpoint(
    note_id: str, request: Request
) -> Response:
    """Hard-delete a workspace note. Returns 204 or 404."""
    ok = await db_module.delete_workspace_note(
        await _db(request), note_id, tenant_id=await _tenant_id(request)
    )
    if not ok:
        raise HTTPException(status_code=404, detail="note not found")
    return Response(status_code=204)


@router.post("/workspace/notes/{note_id}/move", status_code=201)
async def move_workspace_note_endpoint(
    note_id: str, body: dict[str, Any], request: Request
) -> dict[str, Any]:
    """Move a workspace note to a project (converts it to a project note and
    removes it from the workspace). Body: {project_id}. Returns the new note."""
    project_id = (body.get("project_id") or "").strip()
    if not project_id:
        raise HTTPException(status_code=400, detail="project_id required")
    moved = await db_module.move_workspace_note_to_project(
        await _db(request), note_id, project_id,
        tenant_id=await _tenant_id(request),
    )
    if moved is None:
        raise HTTPException(status_code=404, detail="note or project not found")
    return moved


# --- Workspace decisions ---------------------------------------------------

@router.get("/workspace/decisions")
async def list_workspace_decisions_endpoint(
    request: Request, include_superseded: bool = False
) -> list[dict[str, Any]]:
    """Active workspace decisions (newest first). ``?include_superseded=true``
    returns full history."""
    return await db_module.get_workspace_decisions(
        await _db(request), include_superseded=include_superseded,
        tenant_id=await _tenant_id(request),
    )


@router.post("/workspace/decisions", status_code=201)
async def create_workspace_decision_endpoint(
    body: dict[str, Any], request: Request
) -> dict[str, Any]:
    """Pin a workspace decision. Body: {title, body, category?}."""
    title = (body.get("title") or "").strip()
    text = (body.get("body") or "").strip()
    category = body.get("category", "TECHNICAL")
    if not title or not text:
        raise HTTPException(status_code=400, detail="title and body required")
    return await db_module.pin_workspace_decision(
        await _db(request), title, text, category,
        tenant_id=await _tenant_id(request),
    )


@router.delete("/workspace/decisions/{decision_id}", status_code=204)
async def delete_workspace_decision_endpoint(
    decision_id: str, request: Request
) -> Response:
    """Hard-delete a workspace decision. Returns 204 or 404."""
    ok = await db_module.delete_workspace_decision(
        await _db(request), decision_id, tenant_id=await _tenant_id(request)
    )
    if not ok:
        raise HTTPException(status_code=404, detail="decision not found")
    return Response(status_code=204)


# --- Workspace settings ----------------------------------------------------

@router.get("/workspace/settings")
async def get_workspace_settings_endpoint(request: Request) -> dict[str, Any]:
    """Read the workspace-global default settings (singleton)."""
    return await db_module.get_workspace_settings(
        await _db(request), tenant_id=await _tenant_id(request)
    )


@router.patch("/workspace/settings")
async def update_workspace_settings_endpoint(
    body: dict[str, Any], request: Request
) -> dict[str, Any]:
    """Patch workspace-global defaults. Only the fields passed are changed."""
    nudge_thresh = body.get("log_task_sprint_nudge_threshold")
    return await db_module.update_workspace_settings(
        await _db(request),
        hitl_auto_answer_default=body.get("hitl_auto_answer_default"),
        sprint_name_default=body.get("sprint_name_default"),
        display_name=body.get("display_name"),
        log_task_sprint_nudge_threshold=int(nudge_thresh) if nudge_thresh is not None else None,
        handoff_template=body.get("handoff_template"),
        tenant_id=await _tenant_id(request),
    )
