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


# ---------------------------------------------------------------------------
# Project family / template revisions — DESIGN ONLY (5060eea1, parent
# ddcf6984). See docs/meridian-project-family-template-revisions-design.md
# for the full design this implements as data contracts.
#
# These classes are PURELY ADDITIVE and INTENTIONALLY UNWIRED: no route, MCP
# tool, or handler constructs or returns any of them. No table backing them
# exists (see the design doc's section (j) for the proposed, not-yet-written
# schema). They exist so the API shapes for create/fork/override/preview/
# adopt/reject/rollback have a concrete, reviewable, type-checked form ahead
# of any real implementation item.
# ---------------------------------------------------------------------------


class ProjectTemplateCreate(BaseModel):
    """Request shape for creating a brand-new template (design doc section k,
    "create"). Implicitly creates the template's first revision
    (revision_number=1) from ``fields`` — there is no separate "create an
    empty template" operation in this design.
    """

    name: str = Field(..., min_length=1, description="Template display name.")
    description: str | None = Field(default=None, description="Human-readable summary of what this template provisions.")
    fields: dict[str, Any] = Field(
        default_factory=dict,
        description="Full configuration payload for revision 1. See design doc section (i): must never contain "
        "secrets or machine-local absolute paths — validated the same way meridian.capability_manifest "
        "validates provenance/manifest fields (not yet wired here; design only).",
    )
    schema_version: int = Field(default=1, ge=1, description="Initial schema_version — see design doc section (e).")
    provenance: dict[str, Any] | str | None = Field(
        default=None,
        description="Where this template came from (a doc section, an admin note, a URL). Same typing convention "
        "as meridian.capability_manifest capability provenance.",
    )
    created_by_human_id: str | None = Field(default=None, description="Optional creator identifier.")


class ProjectTemplate(BaseModel):
    """A template lineage (design doc section k, "create" response; also the
    response for "fork"). ``latest_revision_id`` is the ONLY mutable pointer
    in this whole design — see design doc section (f), Supersession.
    """

    id: str
    name: str
    description: str | None = None
    schema_version: int = 1
    latest_revision_id: str | None = Field(
        default=None,
        description="Stable revision id ('{template_id}:r{revision_number}') of the newest revision. "
        "None only in the impossible-in-practice case of a template with zero revisions.",
    )
    latest_revision_number: int = Field(default=0, ge=0)
    forked_from_template_id: str | None = Field(
        default=None, description="Set when this template was created via 'fork' (design doc section k)."
    )
    forked_from_revision_id: str | None = Field(
        default=None, description="The specific source revision this template's first revision was forked from."
    )
    created_by_human_id: str | None = None
    created_at: str


class TemplateRevisionCreate(BaseModel):
    """Request shape for adding a new immutable revision to an existing
    template (design doc section k, "create" — the revision-on-existing-
    template case). ``fields`` is a full replacement of the payload, not a
    delta — same "replace, not merge" contract as
    ``meridian.db.profile_layers.set_profile_layer``.
    """

    template_id: str
    fields: dict[str, Any] = Field(default_factory=dict)
    schema_version: int | None = Field(
        default=None,
        ge=1,
        description="Omit to inherit the template's current schema_version unchanged. Set explicitly to bump it "
        "— see design doc section (e) for what should trigger a bump.",
    )
    changelog: str | None = Field(default=None, description="Human-authored summary of what changed and why.")
    provenance: dict[str, Any] | str | None = None
    actor: str | None = Field(default=None, description="Who/what created this revision, for the audit trail.")


class TemplateRevisionSnapshot(BaseModel):
    """One immutable template revision (design doc sections a, b, f, g).
    Never mutated after creation except for ``superseded_by_revision_id``,
    which is set exactly once, when a later revision is created.
    """

    id: str
    revision_id: str = Field(..., description="Stable id: '{template_id}:r{revision_number}'. See design doc section (a).")
    template_id: str
    revision_number: int = Field(..., ge=1)
    schema_version: int = Field(..., ge=1)
    fields: dict[str, Any]
    content_hash: str = Field(..., description="'sha256:...' canonical hash — see design doc section (b).")
    changelog: str | None = None
    provenance: dict[str, Any] | str | None = None
    superseded_by_revision_id: str | None = Field(
        default=None, description="None means this IS the current latest revision."
    )
    rollback_of_revision_id: str | None = Field(
        default=None,
        description="Set only when this revision was minted by a template-level rollback (design doc section g) "
        "— records which older revision's payload this one intentionally reproduces.",
    )
    created_at: str


class TemplateForkRequest(BaseModel):
    """Request shape for 'fork' (design doc section k): branch a specific
    source revision into a brand-new, independent template lineage.
    ``field_overrides`` (if given) is applied on top of the source
    revision's payload to produce the new template's own first revision —
    the fork is not required to be byte-identical to its source.
    """

    source_template_id: str
    source_revision_id: str = Field(..., description="Must belong to source_template_id.")
    new_template_name: str = Field(..., min_length=1)
    description: str | None = None
    field_overrides: dict[str, Any] | None = Field(
        default=None, description="Fields to change relative to the source revision's payload at fork time."
    )
    actor: str | None = None


class TemplateOverrideSet(BaseModel):
    """Request shape for 'override' (design doc section k, c): set a child's
    entire local override layer for one template. Wholesale-replaces the
    child's stored ``fields``/``reset_fields`` — same "replace, not merge"
    contract as ``set_profile_layer``, not a delta.
    """

    child_project_id: str
    template_id: str
    fields: dict[str, Any] = Field(default_factory=dict)
    reset_fields: list[str] = Field(
        default_factory=list,
        description="Keys to explicitly retract from the template base rather than inherit. See design doc "
        "section (c), rule 2, for precedence vs. fields.",
    )
    expected_override_revision: int | None = Field(
        default=None, description="Optimistic concurrency — mirrors profile_layers' expected_revision."
    )
    actor: str | None = None


class ChildTemplateOverride(BaseModel):
    """A child's persisted local override layer (design doc section c)."""

    child_project_id: str
    template_id: str
    fields: dict[str, Any] = Field(default_factory=dict)
    reset_fields: list[str] = Field(default_factory=list)
    override_revision: int = Field(default=0, ge=0)
    content_hash: str
    updated_at: str | None = None


class ConfigDiffEntry(BaseModel):
    """One entry of a structural config diff (design doc section d). Used
    both for revision-to-revision diffs and for a child's effective-vs-
    template-base diff (the shape ``preview`` returns).
    """

    path: str = Field(..., description="Top-level field name, e.g. 'build.timeout_seconds'.")
    op: Literal["added", "removed", "changed"]
    base_value: Any | None = Field(default=None, description="Value on the 'from' side. None + op=='added' means absent.")
    new_value: Any | None = Field(default=None, description="Value on the 'to' side. None + op=='removed' means absent.")
    source: Literal["template", "override"] = Field(
        default="template",
        description="For a child effective-vs-base diff: whether this delta comes from the child's own override "
        "layer or from the template revision itself changing. Always 'template' for a pure "
        "revision-to-revision diff. See design doc section (d).",
    )


class TemplateAdoptionPreviewRequest(BaseModel):
    """Request shape for 'preview' (design doc section k): dry-run the
    effective-config diff a child would see if it adopted ``candidate_revision_id``,
    without changing any stored state.
    """

    child_project_id: str
    candidate_revision_id: str


class TemplateOverridePreview(BaseModel):
    """Response shape for 'preview' (design doc sections d, h). Never
    mutates anything — pure dry-run.
    """

    child_project_id: str
    template_id: str
    current_revision_id: str | None = Field(
        default=None, description="The child's currently-adopted revision. None if the child has never adopted anything."
    )
    candidate_revision_id: str
    current_effective_hash: str | None = None
    candidate_effective_hash: str
    diff: list[ConfigDiffEntry] = Field(default_factory=list)
    conflicts: list[str] = Field(
        default_factory=list,
        description="Field paths where the child's override is flagged incompatible with the candidate revision "
        "— only populated when schema_version_change is True AND the override declares an affected key. "
        "See design doc section (h).",
    )
    compatible: bool = Field(default=True, description="False whenever conflicts is non-empty.")
    schema_version_change: bool = Field(
        default=False, description="True when candidate_revision's schema_version differs from current_revision's."
    )


class TemplateAdoptRequest(BaseModel):
    """Request shape for 'adopt' (design doc sections f, h, k): pin a child to
    a specific revision. Refused when preview-equivalent conflict detection
    finds a non-empty conflict set, unless ``force_accept_conflicts`` is set
    together with a non-empty ``override_reason`` — same acknowledged-override
    pattern as ``override_merge_approval`` / ``override_code_intel_receipt``.
    """

    child_project_id: str
    revision_id: str
    expected_snapshot_revision: int | None = Field(
        default=None, description="Optimistic concurrency on the child's snapshot row."
    )
    force_accept_conflicts: bool = False
    override_reason: str | None = Field(
        default=None, description="Required when force_accept_conflicts is True — persisted for audit."
    )
    actor: str | None = None


class TemplateRejectRequest(BaseModel):
    """Request shape for 'reject' (design doc section k): a child explicitly
    declines a proposed revision without changing its currently-adopted one.
    Recorded in ``ChildTemplateSnapshot.declined_revision_ids`` so the same
    revision isn't re-offered as a fresh suggestion.
    """

    child_project_id: str
    revision_id: str
    reason: str | None = None
    actor: str | None = None


class ChildTemplateRollbackRequest(BaseModel):
    """Request shape for child-side rollback (design doc section g): re-point
    a child at a revision it previously adopted. Never touches the child's
    override layer or any template data — pure re-pointing of
    ``adopted_revision_id``.
    """

    child_project_id: str
    target_revision_id: str = Field(..., description="Must belong to the same template_id the child is already associated with.")
    expected_snapshot_revision: int | None = None
    actor: str | None = None


class TemplateRevisionRollbackRequest(BaseModel):
    """Request shape for template-side rollback (design doc section g):
    mint a brand-new revision whose payload reproduces an older revision's
    payload exactly. NEVER mutates or resurrects the old revision_id — see
    design doc section (a) immutability guarantee. Distinct from
    ``ChildTemplateRollbackRequest`` because it targets a different resource
    (the template's revision ledger, not one child's pointer).
    """

    template_id: str
    target_revision_id: str = Field(..., description="An existing, past revision_id of this template to reproduce.")
    changelog: str | None = None
    actor: str | None = None


class ChildTemplateSnapshot(BaseModel):
    """The durable per-child adoption record (design doc sections f, g) —
    the "child snapshot" this sprint item is named for. Returned by adopt,
    reject, and both rollback operations.
    """

    child_project_id: str
    template_id: str
    adopted_revision_id: str | None = Field(
        default=None, description="None if this child has never successfully adopted a revision."
    )
    adopted_at: str | None = None
    snapshot_revision: int = Field(
        default=0, ge=0, description="This snapshot's own optimistic-concurrency counter."
    )
    effective_content_hash: str | None = Field(
        default=None,
        description="Content hash of the fully-resolved effective config at the moment of the last adopt/rollback "
        "— the frozen audit record. See design doc section (b).",
    )
    declined_revision_ids: list[str] = Field(default_factory=list)
    last_action: Literal["adopted", "rejected", "rolled_back"] | None = None
    updated_at: str | None = None


class ProjectFamilyView(BaseModel):
    """Read-only aggregate: one template plus every child that has ever
    adopted one of its revisions (design doc "Composition with the legacy
    parent_project_id mechanism"). NOT a stored entity — no
    ``project_family`` table exists or is proposed; this is a join,
    computed at read time over ``child_template_snapshots``. Deliberately
    unrelated to ``Project.parent_project_id`` — see the design doc for why
    the two groupings are orthogonal.
    """

    template_id: str
    template_name: str
    latest_revision_id: str | None = None
    members: list[ChildTemplateSnapshot] = Field(default_factory=list)


class HandoffFamilyContext(BaseModel):
    """ea49362c — OPTIONAL, UNWIRED illustrative shape for the family-context
    block a future ``generate_handoff(..., include_family_context=True)``
    (see ``docs/meridian-project-family-integration-contract.md`` section a)
    would attach to a handoff response as a sibling field next to
    ``content`` — exactly like ``build_effective_capability_contract``'s and
    ``build_effective_profile_binding``'s existing pattern in
    ``meridian/handoff.py``. Nothing in ``meridian/handoff.py`` constructs or
    references this class today; this item makes no functional change to
    that module. See the integration contract doc for the full rationale,
    the test matrix, and everything this shape deliberately leaves open.

    Deliberately reuses ``template_id`` (not a new ``family_id`` field) as
    the family identifier -- see the integration contract's naming-collision
    note: ``workspace_proposals.family_id`` (``meridian/db/proposal_lineage.py``)
    is a pre-existing, unrelated proposal-lineage grouping concept, and this
    class must never be confused with it.

    Every field is ``None``/empty for a project with no family (integration
    contract section e) -- there is no required field here a family-less
    project could not trivially satisfy with its default, and no code
    constructs this model at all in that case (the field is simply absent
    from the response, not an empty instance).
    """

    child_project_id: str = Field(
        ..., description="This project's id -- matches ChildTemplateSnapshot.child_project_id."
    )
    template_id: str | None = Field(
        default=None,
        description="The family identifier: the ProjectTemplate this project has adopted from, if any. "
        "None means this project has no family. Matches ChildTemplateSnapshot.template_id / "
        "ProjectFamilyView.template_id -- deliberately NOT named family_id (collision, see class docstring).",
    )
    adopted_revision_id: str | None = Field(
        default=None,
        description="The template_revision this project is currently pinned to -- "
        "ChildTemplateSnapshot.adopted_revision_id, surfaced verbatim (5060eea1 section f/g).",
    )
    latest_revision_id: str | None = Field(
        default=None,
        description="ProjectTemplate.latest_revision_id at the time this context was built, so a receiver "
        "can tell 'behind' (adopted_revision_id != latest_revision_id) without a second lookup "
        "(5060eea1 section f, 'is this child behind' is a derived, read-time fact).",
    )
    inherited_vs_local: list[ConfigDiffEntry] = Field(
        default_factory=list,
        description="Reuses ConfigDiffEntry (5060eea1 section d) verbatim: each entry's `source` field "
        "('template' vs 'override') IS the inherited-vs-local provenance signal for that key. "
        "No new diff shape is introduced by this contract.",
    )
    executable_capability_status: Literal["executable", "non_executable", "unknown"] = Field(
        default="unknown",
        description="Mirrors the executable/executable_reasons vocabulary build_effective_capability_contract "
        "already emits (meridian/handoff.py) -- NOT a new status vocabulary. 'unknown' is the default "
        "for a project with no family, or when availability could not be checked.",
    )
    executable_reasons: list[str] = Field(default_factory=list)
    pending_promotion_revision_ids: list[str] = Field(
        default_factory=list,
        description="Candidate template revisions newer than adopted_revision_id that a human has not yet "
        "adopted or rejected for this child -- the 'pending promotion decisions' ea49362c's own "
        "acceptance notes name. Never auto-populated by silently adopting anything.",
    )


# ---------------------------------------------------------------------------
# 4376e655 — Experiment / Run / RunAttempt state model. See
# meridian.experiment_model (closed vocabularies, transition rules) and
# meridian.db.experiment_model (persistence, derived run status) for the
# full contract these wire-format shapes describe.
# ---------------------------------------------------------------------------


class ExperimentCreate(BaseModel):
    """Body for creating a new experiment."""

    project_id: str = Field(..., min_length=1)
    name: str | None = None
    config_template: dict[str, Any] | None = None
    created_by: str | None = None


class Experiment(BaseModel):
    """An experiment row — a named research question many runs belong to."""

    id: str
    project_id: str
    name: str | None = None
    config_template: dict[str, Any] | None = None
    created_by: str | None = None
    created_at: str
    updated_at: str | None = None


class ResearchRunCreate(BaseModel):
    """Body for creating a new run under an experiment.

    ``idempotency_key`` (optional): a repeat call with the SAME key returns
    the existing run rather than creating a duplicate — see
    ``meridian.db.experiment_model.create_run``.
    """

    project_id: str = Field(..., min_length=1)
    experiment_id: str = Field(..., min_length=1)
    params: dict[str, Any] | None = None
    source_revision: str | None = None
    idempotency_key: str | None = None
    created_by: str | None = None


class RunAttempt(BaseModel):
    """One concrete attempt to execute a run. ``status`` is one of
    ``meridian.experiment_model.ATTEMPT_STATUSES``; ``failure_class`` is set
    only when ``status`` is ``failed``/``crashed``."""

    id: str
    run_id: str
    project_id: str
    attempt_number: int
    status: Literal["queued", "running", "succeeded", "failed", "cancelled", "crashed", "unknown"]
    failure_class: Literal[
        "user_error", "infra_error", "timeout", "oom", "preempted", "dependency_error", "unknown"
    ] | None = None
    error_message: str | None = None
    checkpoint_ref: dict[str, Any] | None = None
    artifact_refs: list[Any] | None = None
    provenance_ref: dict[str, Any] | None = None
    started_at: str | None = None
    ended_at: str | None = None
    last_heartbeat_at: str | None = None
    created_by: str | None = None
    created_at: str
    updated_at: str | None = None


class ResearchRun(BaseModel):
    """A run row. ``status`` and ``latest_attempt`` are ALWAYS derived live
    from the run's attempts (see ``meridian.db.experiment_model.get_run``) —
    never an independently-settable, cacheable field, so restart recovery
    and handoff serialization can never replay a stale status."""

    id: str
    project_id: str
    experiment_id: str
    idempotency_key: str | None = None
    params: dict[str, Any] | None = None
    params_fingerprint: str | None = None
    source_revision: str | None = None
    attempt_count: int = 0
    status: Literal["queued", "running", "succeeded", "failed", "cancelled", "crashed", "unknown"]
    latest_attempt: RunAttempt | None = None
    created_by: str | None = None
    created_at: str
    updated_at: str | None = None


class AttemptTransitionRequest(BaseModel):
    """Body for transitioning a run attempt's status. See
    ``meridian.experiment_model.validate_attempt_transition`` for the legal
    transition table; an illegal jump (e.g. ``succeeded`` -> ``running``) is
    rejected with 400, not silently coerced."""

    project_id: str = Field(..., min_length=1)
    status: Literal["queued", "running", "succeeded", "failed", "cancelled", "crashed", "unknown"]
    failure_class: Literal[
        "user_error", "infra_error", "timeout", "oom", "preempted", "dependency_error", "unknown"
    ] | None = None
    error_message: str | None = None
    checkpoint_ref: dict[str, Any] | None = None
    artifact_refs: list[Any] | None = None
    provenance_ref: dict[str, Any] | None = None


