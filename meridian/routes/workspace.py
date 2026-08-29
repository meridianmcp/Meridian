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


@router.patch("/workspace/notes/{note_id}")
async def update_workspace_note_endpoint(
    note_id: str, body: dict[str, Any], request: Request
) -> dict[str, Any]:
    """Patch title/body/tags on a workspace note."""
    result = await db_module.update_workspace_note(
        await _db(request), note_id,
        title=body.get("title"),
        body=body.get("body"),
        tags=body.get("tags"),
        tenant_id=await _tenant_id(request),
    )
    if result is None:
        raise HTTPException(status_code=404, detail="note not found")
    return result


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
    """Patch workspace-global defaults. Only the fields passed are changed.

    handoff_template (7855e580): a blank/whitespace-only value here is treated
    as "field left untouched," never as an explicit clear-to-server-default.
    This dashboard endpoint is the one an ordinary Save Defaults click hits —
    e.g. while the settings form's own GET is still in flight, its template
    textarea renders empty — and that must never be able to silently wipe out
    a workspace's real, already-configured non-empty handoff_template. A
    genuine explicit clear (empty string forwarded verbatim) is still
    available through the MCP update_workspace_settings tool, which calls
    db.update_workspace_settings directly and does not go through this route.
    """
    nudge_thresh = body.get("log_task_sprint_nudge_threshold")
    interval = body.get("refresh_interval_turns")
    trigger_min_interval = body.get("refresh_trigger_min_interval")
    asw_mins = body.get("active_session_warning_minutes")
    handoff_template = body.get("handoff_template")
    if handoff_template is not None and not handoff_template.strip():
        handoff_template = None
    return await db_module.update_workspace_settings(
        await _db(request),
        hitl_auto_answer_default=body.get("hitl_auto_answer_default"),
        sprint_name_default=body.get("sprint_name_default"),
        display_name=body.get("display_name"),
        log_task_sprint_nudge_threshold=int(nudge_thresh) if nudge_thresh is not None else None,
        handoff_template=handoff_template,
        # 0bf67524 — cascade defaults seeded onto new projects.
        execution_mode_default=body.get("execution_mode_default"),
        code_intel_enabled_default=body.get("code_intel_enabled_default"),
        # 76cf8bda — /loop auto-continue workspace default.
        loop_enabled_default=body.get("loop_enabled_default"),
        # bf51b12e — planner context-refresh config.
        auto_refresh_enabled=body.get("auto_refresh_enabled"),
        refresh_interval_turns=int(interval) if interval is not None else None,
        refresh_triggers=body.get("refresh_triggers"),
        # db0361bb — separate, smaller floor gating the trigger branch only.
        refresh_trigger_min_interval=(
            int(trigger_min_interval) if trigger_min_interval is not None else None
        ),
        # 6e0e5cea — configurable active-session warning window.
        active_session_warning_minutes=int(asw_mins) if asw_mins is not None else None,
        tenant_id=await _tenant_id(request),
    )


# --- Workspace blog (workspace-scoped CMS) ---------------------------------

@router.get("/workspace/blog")
async def list_workspace_blog_endpoint(
    request: Request, status: str | None = None
) -> list[dict[str, Any]]:
    """Workspace-scoped blog posts (newest first). ``?status=`` filters to
    draft|published|archived. Each post carries a ``/blog/<slug>`` url (8843250f)."""
    return await db_module.get_blog_posts(
        await _db(request), tenant_id=await _tenant_id(request), status=status
    )


@router.post("/workspace/blog", status_code=201)
async def create_workspace_blog_endpoint(
    body: dict[str, Any], request: Request
) -> dict[str, Any]:
    """Create or update a workspace blog post. Body: {title, body?, status?,
    slug?, id?}. Pass ``id`` to update an existing post."""
    title = (body.get("title") or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="title required")
    return await db_module.save_blog_post(
        await _db(request), title, body.get("body") or "",
        status=body.get("status", "draft"),
        slug=body.get("slug"),
        post_id=body.get("id"),
        tenant_id=await _tenant_id(request),
    )


# --- Workspace sprint board (cross-project personal backlog) ----------------

@router.get("/workspace/sprint-items")
async def list_workspace_sprint_items_endpoint(
    request: Request, status: str | None = None, group: str | None = None
) -> list[dict[str, Any]]:
    """Workspace sprint items, grouped by item_group. ``?status=`` and
    ``?group=`` filter. Not tied to any project."""
    return await db_module.get_workspace_sprint_items(
        await _db(request), status=status, item_group=group,
        tenant_id=await _tenant_id(request),
    )


@router.post("/workspace/sprint-items", status_code=201)
async def create_workspace_sprint_item_endpoint(
    body: dict[str, Any], request: Request
) -> dict[str, Any]:
    """Add a workspace sprint item. Body: {title, group?, human_id?}."""
    title = (body.get("title") or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="title required")
    return await db_module.add_workspace_sprint_item(
        await _db(request), title,
        item_group=body.get("group", body.get("item_group")),
        human_id=body.get("human_id"),
        tenant_id=await _tenant_id(request),
    )


@router.patch("/workspace/sprint-items/{item_id}")
async def patch_workspace_sprint_item_endpoint(
    item_id: str, body: dict[str, Any], request: Request
) -> dict[str, Any]:
    """Patch title/status/group/human_id on a workspace sprint item."""
    item = await db_module.update_workspace_sprint_item(
        await _db(request), item_id,
        title=body.get("title"),
        status=body.get("status"),
        item_group=body.get("group", body.get("item_group")),
        human_id=body.get("human_id"),
        tenant_id=await _tenant_id(request),
    )
    if item is None:
        raise HTTPException(status_code=404, detail="sprint item not found")
    return item


@router.post("/workspace/sprint-items/{item_id}/complete")
async def complete_workspace_sprint_item_endpoint(
    item_id: str, request: Request
) -> dict[str, Any]:
    """Mark a workspace sprint item done (stamps completed_at)."""
    item = await db_module.complete_workspace_sprint_item(
        await _db(request), item_id, tenant_id=await _tenant_id(request)
    )
    if item is None:
        raise HTTPException(status_code=404, detail="sprint item not found")
    return item
