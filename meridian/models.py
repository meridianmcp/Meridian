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
    agent_framework: str = Field(
        default="claude_code",
        description=(
            "v2.4 — framework label (claude_code | cursor | windsurf | "
            "langgraph | autogen | openviking | custom). Surfaces as a badge "
            "in the Team tab."
        ),
    )


class TaskCreate(BaseModel):
    """Body for POST /tasks."""

    session_id: str
    project_id: str
    description: str = Field(..., min_length=1)
    status: Literal["pending", "done", "failed", "in_progress", "pending-hitl", "backlog", "future", "backburner"] = "done"
    parent_task_id: str | None = Field(
        default=None,
        description=(
            "v2.4 — when this task is a sub-step of another, point at the "
            "parent task_log.id. Dashboard renders the tree."
        ),
    )


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
    icon: str | None = None
    created_at: str


class ExecutorConfig(BaseModel):
    """Per-project executor defaults injected into executor sessions."""

    model_config = {"extra": "allow"}

    repo_path: str | None = None
    repo_paths: list[dict] | None = None
    env_file: str | None = None
    test_cmd: str | None = None
    test_min: int | None = Field(default=None, ge=0)
    deploy_cmd: str | None = None
    shell_type: str | None = None
    branch: str | None = None


class ProjectSettings(BaseModel):
    """Persisted per-project settings shown in the dashboard."""

    project_id: str
    max_pinned_decisions: int = Field(
        default=20,
        ge=1,
        le=200,
        description="Warn when the live constitution reaches this many items.",
    )
    executor_config: ExecutorConfig = Field(default_factory=ExecutorConfig)
    hitl_auto_answer: int = Field(
        default=0,
        ge=0,
        le=2,
        description="HITL auto-answer mode: 0=off, 1=safe (executor questions "
        "only, no destructive keywords), 2=aggressive (everything except "
        "correction + security-sensitive requests).",
    )
    auto_worktrees: int = Field(
        default=1,
        ge=0,
        le=1,
        description="0716c9e0: suggest git worktree on claim_sprint_item (default ON).",
    )
    require_merge_approval: int = Field(
        default=1,
        ge=0,
        le=1,
        description="0716c9e0: warn via HITL when completing an item with an active worktree (default ON).",
    )
    code_intel_enabled: int = Field(
        default=0,
        ge=0,
        le=1,
        description="Sprint-2/3: show codebase-memory-mcp install URL in dashboard and agent guidance.",
    )
    execution_mode: Literal["autonomous", "interactive"] = Field(
        default="autonomous",
        description="ecf69de8: executor posture. 'autonomous' (default) claims "
        "and runs sprint items immediately; 'interactive' asks for direction "
        "before executing.",
    )


class ProjectSettingsPatch(BaseModel):
    """Body for PATCH /projects/{id}/settings."""

    max_pinned_decisions: int | None = Field(default=None, ge=1, le=200)
    executor_config: ExecutorConfig | None = None
    hitl_auto_answer: int | None = Field(default=None, ge=0, le=2)
    auto_worktrees: int | None = Field(default=None, ge=0, le=1)
    require_merge_approval: int | None = Field(default=None, ge=0, le=1)
    code_intel_enabled: int | None = Field(default=None, ge=0, le=1)
    execution_mode: Literal["autonomous", "interactive"] | None = None


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
    status: Literal["active", "idle", "closed", "archived"]
    last_seen: str
    created_at: str
    session_summary: Any = None
    agent_framework: str | None = None  # v2.4
    client_type: str | None = None  # v2.6


class Task(BaseModel):
    """A task-log entry."""

    id: str
    session_id: str
    project_id: str
    description: str
    # 'skipped' is read-tolerated here (not a settable write status): Postgres
    # task_log has no CHECK constraint, so historical rows can carry it and the
    # GET /projects/{id}/tasks response must serialize them without 500ing.
    status: Literal["pending", "in_progress", "done", "failed", "pending-hitl", "backlog", "future", "backburner", "skipped"]
    parent_task_id: str | None = None  # v2.4
    sprint_item_id: str | None = None  # v2.6
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
    sprint_item_id: str | None = None
    error: str | None = None
    blocking_item_id: str | None = None
    blocking_item_title: str | None = None


class HandoffResult(BaseModel):
    """Return value of POST /projects/{id}/handoff."""

    path: str
    content: str
    mode: str | None = None


class TaskUpdate(BaseModel):
    """Body for PATCH /tasks/{task_id}. Either field may be omitted."""

    status: Literal["pending", "in_progress", "done", "failed", "pending-hitl", "backlog", "future", "backburner"] | None = None
    description: str | None = None


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
    client: str | None = Field(
        default=None,
        description="Client app identifier: claude-code, claude-desktop, cursor, other.",
    )
    role: str | None = Field(
        default=None,
        description="Optional session role. Use 'executor' to inject executor_config.",
    )
    source: str | None = Field(
        default=None,
        description=(
            "G8.34 — Optional hint about why start_session was called: "
            "'startup' (fresh client boot), 'resume' (cleared chat / continued "
            "work), 'clear' (user wiped context), or 'compact' (context window "
            "ran out, fresh process). SessionStart hooks forward this so the "
            "server can return a continuation block instead of a full reset."
        ),
    )


class WorktreeCreate(BaseModel):
    """Body for POST /projects/{id}/worktrees."""

    session_id: str = Field(..., description="Session that owns this worktree.")
    branch: str = Field(..., min_length=1, description="Git branch name, e.g. worktree/abc12345.")
    path: str = Field(..., min_length=1, description="Filesystem path of the worktree.")
    item_id: str | None = Field(default=None, description="Sprint item this worktree was created for.")


