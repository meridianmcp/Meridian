"""meridian/routes/a2a.py — Google A2A protocol HTTP routes.

Routes:
  POST /.well-known/agent.json     — agent card (discovery)
  GET  /.well-known/agent.json     — agent card (discovery)
  POST /a2a/{agent_id}/tasks/send  — receive a task (A2A spec §4.2)
  GET  /a2a/{agent_id}/tasks/{task_id}  — poll task status (A2A spec §4.3)

Reference: https://google.github.io/A2A/
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import db as db_module
from ..a2a import build_agent_card, task_to_a2a

router = APIRouter(tags=["a2a"])


async def _db(request: Request) -> Any:
    """Retrieve the DB connection from app state."""
    return request.app.state.db


# ---------------------------------------------------------------------------
# Agent card — served at /.well-known/agent.json
# ---------------------------------------------------------------------------

@router.get("/.well-known/agent.json", include_in_schema=False)
async def agent_card_get(request: Request) -> JSONResponse:
    """Return the A2A agent card so other agents can discover this server."""
    base_url = str(request.base_url).rstrip("/")
    card = build_agent_card(base_url=base_url)
    return JSONResponse(card)


@router.post("/.well-known/agent.json", include_in_schema=False)
async def agent_card_post(request: Request) -> JSONResponse:
    """Support POST discovery per A2A spec."""
    return await agent_card_get(request)


# ---------------------------------------------------------------------------
# Task send — POST /a2a/{agent_id}/tasks/send
# ---------------------------------------------------------------------------

@router.post("/a2a/{agent_id}/tasks/send", status_code=202)
async def send_task(
    agent_id: str,
    request: Request,
) -> JSONResponse:
    """Receive an A2A task, store it as 'submitted', and return the task envelope.

    Request body (A2A spec TaskSendParams):
      {
        "id": "<caller-supplied task id, optional>",
        "message": { "role": "user", "parts": [...] },
        "metadata": {...}       # optional
      }

    Response (202 Accepted) follows the A2A Task schema:
      {
        "task_id": "<uuid>",
        "agent_id": "<agent_id>",
        "status": { "state": "submitted" },
        "artifacts": [],
        "metadata": {...},
        "created_at": "...",
        "updated_at": "..."
      }
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=422, detail="Invalid JSON body")

    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="Request body must be a JSON object")

    message = body.get("message") or {}
    metadata = body.get("metadata") or {}

    # Normalise the input into a JSON-serialisable dict for storage.
    input_data: dict[str, Any] = {
        "message": message,
        "raw": body,
    }

    db = await _db(request)
    task = await db_module.create_agent_task(
        db,
        agent_id=agent_id,
        input_data=input_data,
        metadata=metadata if isinstance(metadata, dict) else {},
    )
    return JSONResponse(task_to_a2a(task), status_code=202)


# ---------------------------------------------------------------------------
# Task status — GET /a2a/{agent_id}/tasks/{task_id}
# ---------------------------------------------------------------------------

@router.get("/a2a/{agent_id}/tasks/{task_id}")
async def get_task(
    agent_id: str,
    task_id: str,
    request: Request,
) -> JSONResponse:
    """Return the current status of an A2A task.

    Scoped to agent_id so tasks from one agent are never visible to another.
    Returns 404 if the task does not exist or belongs to a different agent.
    """
    db = await _db(request)
    task = await db_module.get_agent_task(db, agent_id=agent_id, task_id=task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    return JSONResponse(task_to_a2a(task))
