"""Strategic insights routes (0b711a9d) — durable understanding per project,
distinct from decisions (choices) and notes (reference). Backs the dashboard
Insights tab."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from .._deps import _db, validate_input_size
from .. import db as db_module

router = APIRouter()


@router.get("/projects/{project_id}/insights")
async def list_insights_endpoint(
    project_id: str,
    request: Request,
    horizon: str | None = None,
) -> list[dict[str, Any]]:
    """Project insights, newest first. ``?horizon=permanent|year|quarter`` filters."""
    db = await _db(request)
    project = await db_module.get_project(db, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return await db_module.get_insights(db, project_id, horizon=horizon)


@router.post("/projects/{project_id}/insights", status_code=201)
async def create_insight_endpoint(
    project_id: str, body: dict[str, Any], request: Request
) -> dict[str, Any]:
    """Create a strategic insight (title required; horizon defaults to 'quarter')."""
    db = await _db(request)
    project = await db_module.get_project(db, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    title = (body.get("title") or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="title is required")
    validate_input_size(title, "insight title", 500)
    validate_input_size(body.get("body"), "insight body", 1_000_000)
    return await db_module.create_insight(
        db, project_id, title, body.get("body") or "",
        horizon=body.get("horizon", "quarter"),
        tags=body.get("tags"),
    )
