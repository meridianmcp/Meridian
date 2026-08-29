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
    parent_project_id: str | None = Field(
        default=None,
        description=(
            "Optional parent project id (3b6ff466). When set, this project is a "
            "subproject that inherits the parent's north_star when it has none of "
            "its own. Subprojects are one level deep: the parent must exist and "
            "must not itself be a subproject."
        ),
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
    status: str = "active"       # 8db00fcb — active | parked | archived
    priority: str = "P2"         # 8db00fcb — P0 | P1 | P2
    # 0fed6a42 — one-level-deep subproject hierarchy. Surfaced on the /projects
    # listing so the dashboard sidebar can render subprojects nested under their
    # parent; null/absent means a top-level project.
    parent_project_id: str | None = None
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
        le=2,
        description="0716c9e0/e7548587: merge-approval mode for completing an item with an "
        "active worktree. 0=off (no check), 1=advisory (warn via HITL, proceed — "
        "default), 2=strict (BLOCKS completion on a genuine active, unmerged worktree "
        "unless explicitly overridden with override_merge_approval + a reason).",
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
    require_merge_approval: int | None = Field(default=None, ge=0, le=2)
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
    # P0 VERIFY (106519eb) — db.get_goal already computes these two fields for a
    # subproject that borrows its parent's north_star (3b6ff466), but this response
    # model previously didn't declare them: FastAPI's response_model validation
    # silently stripped them before the JSON ever reached a caller, so the
    # inherited-vs-own distinction that db.get_goal computes never survived the
    # HTTP boundary (see tests/test_core.py's north_star inheritance tests, which
    # all asserted directly against db_module.get_goal and so never caught this).
    # Declaring them here is the actual fix — no db/route logic changes needed.
    north_star_inherited: bool | None = None
    north_star_source_project_id: str | None = None
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


class ProjectOrganizationSet(BaseModel):
    """Body for PATCH /projects/{id}/organization (8db00fcb)."""

    status: Literal["active", "parked", "archived"] | None = None
    priority: Literal["P0", "P1", "P2"] | None = None


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
    # 98aaccf4 — machine-readable effective capability contract (see
    # meridian.capability_contract). dict, not a typed submodel: its shape is
    # intentionally allowed to evolve (richer effective/availability data once
    # the 02038afe/ac80aaaf sibling items land) without a models.py migration
    # each time. None only if contract-building itself failed (best-effort).
    capability_contract: dict[str, Any] | None = None
    # 89a06e40 — compact effective profile identity/generation projection
    # (see meridian.profile_contract.project_profile_binding):
    # {"generation_key", "executable", "degraded", "restart_required",
    # "restart_report"}. dict, not a typed submodel, for the same
    # forward-compat reason as capability_contract above. None only if the
    # resolution itself failed (best-effort).
    profile_binding: dict[str, Any] | None = None
    # 6cdc5df3 — machine-readable proposal-to-evidence linkage (see
    # meridian.db.proposal_links): one hydrated entry per proposal id with
    # evidence linked in this project. list, not a typed submodel, for the
    # same forward-compat reason as capability_contract above. None only if
    # the lookup itself failed (best-effort); empty list means no linked
    # proposals yet.
    proposal_evidence: list[dict[str, Any]] | None = None
    # d09c29fe -- machine-readable DOCX-integrity gate (see
    # meridian.docx_integrity_gate): per-artifact render/equation-audit/
    # provenance findings plus the executable/executable_reasons readiness
    # signal. dict, not a typed submodel, for the same forward-compat reason
    # as capability_contract above. None only if gate-building itself failed
    # (best-effort).
    docx_integrity: dict[str, Any] | None = None
    # 3cab355a — one entry per requested force_include_ids id that failed
    # validation (unknown/cross-project/cross-version/not-pending — see
    # meridian.handoff.generate_handoff's force_include_rejected docstring).
    # None when force_include_ids was absent on this call; an empty list
    # means it was present and every requested id validated. list, not a
    # typed submodel, for the same forward-compat reason as
    # capability_contract above.
    force_include_rejected: list[dict[str, Any]] | None = None
    # ecc8b280 — machine-readable continuation_required/terminal_ready state
    # (see meridian.continuation_gate). dict, not a typed submodel, for the
    # same forward-compat reason as capability_contract above. None for
    # modes that don't compute it (planner/starter/compact/goal/l0_fallback)
    # or if generate_handoff's build itself failed before reaching the gate.
    continuation_status: dict[str, Any] | None = None


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
    pid: int | None = Field(
        default=None,
        description=(
            "eb2e44f8 — OS PID of the process that created this worktree. "
            "Used by the cleanup guard to confirm no live process is still "
            "using the directory before it is removed from disk."
        ),
    )
    base_sha: str | None = Field(
        default=None,
        description=(
            "eb2e44f8 — commit SHA the worktree was branched from. Supplying "
            "this together with base_branch persists an IMMUTABLE base "
            "manifest for the worktree, later checked before merge/completion "
            "is allowed to proceed. Omitting it skips manifest creation "
            "entirely (backward compatible)."
        ),
    )
    base_branch: str | None = Field(
        default=None,
        description="eb2e44f8 — branch the worktree was branched from, e.g. 'dev'.",
    )
    repo_identity: str | None = Field(
        default=None,
        description=(
            "eb2e44f8 — stable identity for the repo this worktree belongs to "
            "(e.g. a remote URL or repo name). Free-form; recorded on the base "
            "manifest for audit purposes only, never validated against disk. "
            "Defaults to project_id when omitted."
        ),
    )


