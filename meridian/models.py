"""Pydantic v2 request/response models for Meridian's HTTP layer."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class ProjectCreate(BaseModel):
    """Body for POST /projects."""

    name: str = Field(..., min_length=1, description="Unique project name.")


class GoalSet(BaseModel):
    """Body for POST /projects/{id}/goal. Content may be a JSON object or
    a plain string — both forms are accepted."""

    content: dict[str, Any] | str


class SessionRegister(BaseModel):
    """Body for POST /sessions/register."""

    project_id: str
    name: str = Field(..., min_length=1)


class TaskCreate(BaseModel):
    """Body for POST /tasks."""

    session_id: str
    project_id: str
    description: str = Field(..., min_length=1)
    status: Literal["pending", "done", "failed"] = "done"


class EnqueueTask(BaseModel):
    """Body for POST /tasks/enqueue (paid-tier).

    The prompt is handed to a Claude Code subprocess; the server returns
    a pending task row immediately and updates it when the worker exits.
    """

    session_id: str
    project_id: str
    prompt: str = Field(..., min_length=1)
    timeout: float | None = 600.0


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class Project(BaseModel):
    """A project row."""

    id: str
    name: str
    created_at: str


class GoalState(BaseModel):
    """A goal-state row. Content is decoded back to its original form
    (dict if it was stored as JSON, str otherwise)."""

    id: str
    project_id: str
    content: dict[str, Any] | str
    version: int
    created_at: str
    updated_at: str


class Session(BaseModel):
    """A session row."""

    id: str
    project_id: str
    name: str
    status: Literal["active", "idle", "closed"]
    last_seen: str
    created_at: str


class Task(BaseModel):
    """A task-log entry."""

    id: str
    session_id: str
    project_id: str
    description: str
    status: Literal["pending", "done", "failed"]
    created_at: str


class HandoffResult(BaseModel):
    """Return value of POST /projects/{id}/handoff."""

    path: str
    content: str
