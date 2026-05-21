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
    human_id: str | None = Field(
        default=None,
        description="Optional creator identifier. Becomes the project's owner.",
    )


class GoalSet(BaseModel):
    """Body for POST /projects/{id}/goal. Content may be a JSON object or
    a plain string — both forms are accepted. ``human_id`` is optional
    but when provided is checked against the project's creator; a
    mismatch returns 403 to prevent silent overwrites between teammates.

    ``north_star`` and ``sprint`` are optional (v0.5.2). When omitted,
    previously-set values are preserved.
    """

    content: dict[str, Any] | str
    human_id: str | None = None
    north_star: str | None = None
    sprint: str | None = None
    minor: bool = False  # if True, update in-place without version bump (for AUTO BLOCKS)


class SetNorthStarRequest(BaseModel):
    """Body for POST /projects/{id}/goal/north-star (v0.5.2).

    Requires the project owner's human_id — non-owners receive 403.
    """

    north_star: str = Field(..., min_length=1)
    human_id: str = Field(..., min_length=1, description="Must match project owner.")


class SetSprintRequest(BaseModel):
    """Body for POST /projects/{id}/goal/sprint (v0.5.2).

    Any team member can update the sprint — no ownership check.
    """

    sprint: str = Field(..., min_length=1)
    session_id: str | None = Field(
        default=None,
        description="Optional session identifier for logging.",
    )


class SessionRegister(BaseModel):
    """Body for POST /sessions/register."""

    project_id: str
    name: str = Field(..., min_length=1)
    human_id: str | None = Field(
        default=None,
        description="Optional human owner of this session.",
    )


class TaskCreate(BaseModel):
    """Body for POST /tasks."""

    session_id: str
    project_id: str
    description: str = Field(..., min_length=1)
    status: Literal["pending", "done", "failed", "pending-hitl"] = "done"


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
    creator_human_id: str | None = None
    created_at: str


class GoalState(BaseModel):
    """A goal-state row. Content is decoded back to its original form
    (dict if it was stored as JSON, str otherwise).

    ``ambient_tasks`` (v0.4.2/3) carries the last few task descriptions
    so cold sessions read recent activity inline with the directive
    they get from a single ``get_goal`` call — no extra round trip.

    ``north_star`` and ``sprint`` (v0.5.2) are the structured goal
    hierarchy fields. Both are None when not yet set.
    """

    id: str
    project_id: str
    content: dict[str, Any] | str
    version: int
    created_at: str
    updated_at: str
    ambient_tasks: list[dict[str, Any]] | None = None
    north_star: str | None = None
    sprint: str | None = None
    # v0.6.1 — XML-serialised goal envelope. Mirrors the same fields
    # under one wire format so MCP consumers can hand the whole thing
    # to Claude as a single block with structured cache hints.
    xml: str | None = None
    # v0.6.2 — Anthropic-API content blocks with cache_control on the
    # static fields (north_star + version_goal). Caller passes these
    # straight to messages.create() to get prompt caching.
    cache_blocks: list[dict[str, Any]] | None = None
    # v1.1.3 — coherence warning: how stale are the goal fields?
    # {level: ok|warn|critical, message, stale_fields, max_age_seconds}
    coherence_warning: dict[str, Any] | None = None
    # v1.1.3 — per-field freshness so the dashboard can render the
    # green / amber / red dot next to each field.
    field_ages: dict[str, dict[str, Any]] | None = None
    # v1.1.4 — append-only decisions log, newest first.
    decisions: str | None = None


class GoalModeSet(BaseModel):
    """Body for PATCH /projects/{id}/goal-mode."""

    mode: Literal["manual", "auto"]


class Session(BaseModel):
    """A session row."""

    id: str
    project_id: str
    name: str
    human_id: str | None = None
    status: Literal["active", "idle", "closed"]
    last_seen: str
    created_at: str


class Task(BaseModel):
    """A task-log entry."""

    id: str
    session_id: str
    project_id: str
    description: str
    status: Literal["pending", "in_progress", "done", "failed", "pending-hitl"]
    claimed_by: str | None = None
    claimed_at: str | None = None
    created_at: str
    session_name: str | None = None
    human_id: str | None = None
    claimed_by_human_id: str | None = None
    claimed_by_session_name: str | None = None


class ClaimTaskRequest(BaseModel):
    """Body for POST /projects/{id}/tasks/claim and /tasks/release."""

    task_id: str
    session_id: str


class ClaimTaskResponse(BaseModel):
    """Result of a claim attempt — ``claimed`` is False when another
    worker beat us to the lock."""

    task_id: str
    claimed: bool
    claimed_by: str | None = None


class HandoffResult(BaseModel):
    """Return value of POST /projects/{id}/handoff."""

    path: str
    content: str


class TaskUpdate(BaseModel):
    """Body for PATCH /tasks/{task_id}. Either field may be omitted."""

    status: Literal["pending", "in_progress", "done", "failed", "pending-hitl"] | None = None
    description: str | None = None


class ChatMessage(BaseModel):
    """One turn in the dashboard chat history."""

    role: Literal["user", "assistant"]
    content: str


class ChatHistoryItem(BaseModel):
    """A persisted chat message row returned by GET /projects/{id}/chat/history."""

    id: str
    project_id: str
    role: Literal["user", "assistant"]
    content: str
    created_at: str


class FileContent(BaseModel):
    """Body for PUT /projects/{id}/files/{filename}."""

    content: str


class StartSessionRequest(BaseModel):
    """Body for POST /projects/{id}/start-session (v0.4.4)."""

    session_name: str = Field(..., min_length=1)
    human_id: str | None = Field(
        default=None,
        description="Optional human owner of this session.",
    )


class ChatRequest(BaseModel):
    """Body for POST /dashboard/chat.

    ``mode`` selects the backend:

    * ``"cli"`` (default) — shell out to the ``claude`` CLI binary,
      which uses the OAuth token from ``~/.claude/.credentials.json``
      and draws from the user's Max-plan allowance. No API credits.
    * ``"api"`` — call ``api.anthropic.com`` directly via the official
      Anthropic SDK. Bills metered API credits and needs
      ``ANTHROPIC_API_KEY`` (or an OAuth token usable as a bearer).
    """

    project_id: str
    messages: list[ChatMessage]
    system_prompt: str | None = None
    model: str = "claude-sonnet-4-6"
    max_tokens: int = 4096
    mode: Literal["cli", "api"] = "cli"
