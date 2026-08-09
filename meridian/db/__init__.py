"""Persistence layer for Meridian — SQLite (default) or Postgres.

All functions are async.  The ``db`` parameter accepted by every function is
either an ``aiosqlite.Connection`` (SQLite path) or a
``meridian.pg_adapter.PostgresConnection`` (Postgres path, activated when
``MERIDIAN_DB_URL`` is set).  Both expose the same async cursor API so the
function bodies are identical for both backends.

IDs are uuid4 strings; timestamps are ISO-format strings produced by
SQLite's ``datetime('now')`` or the Postgres equivalent in pg_adapter.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import logging
import math
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

import aiosqlite

from meridian import capability_manifest as _capability_manifest
from meridian import capability_profile as _capability_profile

_log = logging.getLogger(__name__)

_UNSET = object()  # sentinel for "not passed" in optional keyword args

# ecf69de8 — per-project executor posture. 'autonomous' (default): claim and run
# pending sprint items immediately without asking for direction. 'interactive':
# review the items and ask the human which to start first. Anything else falls
# back to the default so a bad value never persists.
EXECUTION_MODES = ("autonomous", "interactive")
DEFAULT_EXECUTION_MODE = "autonomous"


def normalize_execution_mode(mode: str | None) -> str:
    """Coerce an execution_mode input to a valid value.

    Returns the lowercased value when it's one of EXECUTION_MODES, otherwise the
    default ('autonomous'). Never raises — callers can pass user input directly.
    """
    if isinstance(mode, str):
        candidate = mode.strip().lower()
        if candidate in EXECUTION_MODES:
            return candidate
    return DEFAULT_EXECUTION_MODE

# ---------------------------------------------------------------------------
# Transparent field encryption (Fernet, MERIDIAN_ENCRYPTION_KEY env var).
# enc:<base64> prefix allows zero-downtime migration — plaintext values are
# returned as-is until overwritten, then automatically encrypted on next write.
# ---------------------------------------------------------------------------

_FERNET_INSTANCE: Any = None


def _fernet() -> Any:
    global _FERNET_INSTANCE
    if _FERNET_INSTANCE is None:
        key = os.environ.get("MERIDIAN_ENCRYPTION_KEY", "")
        if key:
            from cryptography.fernet import Fernet  # noqa: PLC0415
            _FERNET_INSTANCE = Fernet(key.encode() if isinstance(key, str) else key)
    return _FERNET_INSTANCE


def encrypt_field(value: str | None) -> str | None:
    """Encrypt value; returns ``enc:<base64>`` string. Passthrough when no key."""
    if not value:
        return value
    f = _fernet()
    if not f:
        return value
    return "enc:" + f.encrypt(value.encode()).decode()


def decrypt_field(value: str | None) -> str | None:
    """Decrypt an ``enc:`` prefixed value; plaintext values pass through."""
    if not value:
        return value
    if not value.startswith("enc:"):
        return value
    f = _fernet()
    if not f:
        return value
    return f.decrypt(value[4:].encode()).decode()


# In-process pub/sub. Subscribers register an asyncio.Queue keyed by
# project_id; any call to log_task / update_task forwards a serialisable
# event dict so dashboard WebSockets see MCP-driven activity in real time.
_TASK_LISTENERS: dict[str, set[asyncio.Queue]] = {}


def subscribe_tasks(project_id: str) -> asyncio.Queue:
    """Register a new listener queue for a project's task stream."""
    q: asyncio.Queue = asyncio.Queue()
    _TASK_LISTENERS.setdefault(project_id, set()).add(q)
    return q


def unsubscribe_tasks(project_id: str, queue: asyncio.Queue) -> None:
    """Drop a previously-registered listener queue. Safe to call twice."""
    bucket = _TASK_LISTENERS.get(project_id)
    if bucket and queue in bucket:
        bucket.discard(queue)
        if not bucket:
            _TASK_LISTENERS.pop(project_id, None)


def publish_global(event: dict[str, Any]) -> None:
    """Fan-out a global event (e.g. update_available) to every WS subscriber."""
    for listeners in list(_TASK_LISTENERS.values()):
        for q in list(listeners):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass


def _publish_task(event_type: str, task: dict[str, Any]) -> None:
    """Fan-out a task event to every subscriber of the project.

    Synchronous, non-blocking: drops the event if a queue is full. The
    dashboard WebSocket reader drains its queue continuously so a full
    queue means the socket is wedged — letting it back-pressure is wrong.
    """
    project_id = task.get("project_id")
    if not project_id:
        return
    listeners = _TASK_LISTENERS.get(project_id)
    if not listeners:
        return
    event = {"type": event_type, "task": task}
    for q in list(listeners):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass


def _publish_project_event(project_id: str, event_type: str, payload: dict[str, Any]) -> None:
    """Fan-out a project-scoped event (not necessarily task-shaped) to WS subscribers.

    Used for sprint item status changes, goal updates, and session start events
    so the dashboard refreshes in real-time without polling.
    """
    listeners = _TASK_LISTENERS.get(project_id)
    n_listeners = len(listeners) if listeners else 0
    _log.debug(
        "WS broadcast: type=%s project=%s listeners=%d",
        event_type, project_id[:8], n_listeners,
    )
    if not listeners:
        return
    event = {"type": event_type, "project_id": project_id, **payload}
    for q in list(listeners):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            _log.warning("WS broadcast queue full for project %s — event dropped", project_id[:8])

CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    creator_human_id TEXT,
    goal_mode TEXT NOT NULL DEFAULT 'manual'
        CHECK (goal_mode IN ('manual', 'auto')),
    decisions TEXT,
    max_pinned_decisions INTEGER NOT NULL DEFAULT 20,
    executor_config TEXT,
    hitl_auto_answer INTEGER NOT NULL DEFAULT 0,
    icon TEXT,
    agent_instructions TEXT,
    parent_project_id TEXT,
    execution_mode TEXT NOT NULL DEFAULT 'autonomous'
        CHECK (execution_mode IN ('autonomous', 'interactive')),
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'parked', 'archived')),
    priority TEXT NOT NULL DEFAULT 'P2'
        CHECK (priority IN ('P0', 'P1', 'P2')),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS goal_states (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    content TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    goal_north_star TEXT,
    goal_sprint TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    name TEXT NOT NULL,
    human_id TEXT,
    session_type TEXT DEFAULT 'human',
    client_type TEXT,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active','idle','closed','archived')),
    last_seen TEXT NOT NULL DEFAULT (datetime('now')),
    checkpoint_data TEXT,
    sprint_version TEXT,
    goal_compliance TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS task_log (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    project_id TEXT NOT NULL REFERENCES projects(id),
    description TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'done'
        CHECK (status IN ('pending','in_progress','done','failed','pending-hitl','backlog','future','backburner')),
    claimed_by TEXT,
    claimed_at TEXT,
    sprint_item_id TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- v1.9.x — waitlist: pre-launch email capture for hosted tier.
-- email is unique; note is optional "how will you use it" context.
CREATE TABLE IF NOT EXISTS waitlist (
    id TEXT PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    note TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- v1.1 — sprint_items: machine-trackable checklist alongside the
-- free-text sprint field. Each item is one thing the sprint needs
-- shipped (a version number, a feature, a fix). status moves through
-- todo → in_progress → done|failed|skipped|pushed. task_id optionally
-- links the item to the task that finished it. item_group lets items
-- be grouped by objective. pushed_to records the version an item was
-- deferred to. human_id attributes the item to a person.
CREATE TABLE IF NOT EXISTS sprint_items (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    version TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','todo','in_progress','provisional_complete','done','failed','skipped','pushed','indeterminate')),
    item_group TEXT,
    pushed_to TEXT,
    human_id TEXT,
    added_at TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at TEXT,
    claimed_at TEXT,
    task_id TEXT,
    notes TEXT,
    feedback_thumb SMALLINT,
    feedback_note TEXT,
    milestone_type TEXT NOT NULL DEFAULT 'task',
    parent_id TEXT DEFAULT NULL REFERENCES sprint_items(id),
    split_from TEXT DEFAULT NULL,
    merged_into TEXT DEFAULT NULL,
    merged_from TEXT DEFAULT NULL,
    -- 4f02340e: mixed-ownership task chains. owner of a subtask: 'human' | 'ai'
    -- | NULL (unassigned). Drives the alternating claim/handoff state machine
    -- in _advance_task_chain. NULL on parents and on legacy single-owner items.
    owner TEXT DEFAULT NULL,
    -- b944c905: human-readable per-project id (title slug, deduped). UUID stays
    -- the primary key; slug is the board-facing identifier.
    slug TEXT,
    -- b6b0cee6: short, memorable per-project NICKNAME (1-2 words, e.g.
    -- "search-synthesis" / "brisk-otter"), distinct from the long title slug.
    -- Deduped per project. Plain column — no inline index here (guarded-migration
    -- rule); nickname lookups scan the small per-project item set.
    nickname TEXT,
    -- dec69708: ENFORCED deferral. deferred_until is an ISO timestamp; while it is
    -- in the future, claim_sprint_item REFUSES the item (a structural block, not a
    -- text-only "we decided to defer this"). track buckets an item into a named
    -- lane (e.g. 'paper') so a whole track can be skipped by executors. Both
    -- nullable; NULL = immediately claimable / no track. Plain columns — no inline
    -- index here (guarded-migration rule).
    deferred_until TEXT,
    track TEXT,
    -- e08fee30: app-layer priority enum {urgent, high, normal, low}. Higher-
    -- priority PENDING items are surfaced (claimed/grouped) first — get_sprint_items
    -- and get_parallelizable_groups order urgent-first within their existing
    -- ordering. NOT NULL DEFAULT 'normal' so legacy rows read as 'normal'. Enum is
    -- enforced at the app layer (add/patch raise on a bad value); no CHECK here so
    -- the migration ADD COLUMN stays a plain, safe alter. Plain column — no inline
    -- index (guarded-migration rule).
    priority TEXT NOT NULL DEFAULT 'normal',
    -- 2282a636: NULL = ordinary item; 'manual' = blocked on a real-world action
    -- OUTSIDE Meridian (publish something, get an API key, talk to an advisor).
    -- DISTINCT from milestone_type='human' (that is about WHO executes). A manual-
    -- blocker item is surfaced distinctly and excluded from executor "just claim
    -- the next pending" scoping, mirroring milestone_type='human'. Nullable plain
    -- column — no inline index (guarded-migration rule).
    blocker_kind TEXT,
    -- 58a45b92: stored, deterministic wave label (e.g. 'wave-1'). Replaces
    -- recompute-every-time parallel grouping with an inspectable, editable field:
    -- assign_sprint_waves auto-fills it from get_parallelizable_groups, and
    -- update_sprint_item(wave=...) edits it by hand. NULL = unassigned. Nullable
    -- plain column — no inline index (guarded-migration rule).
    wave TEXT,
    -- 3d6bd938: separate human-readable sprint name from the structural version
    -- identifier. version stays a semver-like slug (e.g. 'v0.2.x'); sprint_name
    -- is a nullable free-text label for the bucket (e.g. 'docs-cloudflare').
    -- No inline index (guarded-migration rule). Nullable — legacy rows are NULL.
    sprint_name TEXT,
    -- 94c26322: human-set bypass flag for the prospecting safety gate. When 1,
    -- an unprospected item may still be included in /goal auto-run batches and
    -- claimed without a hard warning. 0/NULL = no bypass (default). Settable
    -- ONLY by planning/human sessions via update_sprint_item; executor claim
    -- paths cannot set this. Plain INTEGER column — no inline index.
    prospect_bypass INTEGER NOT NULL DEFAULT 0
);

-- v2.4 — decisions_pinned: editable constitution alongside the append-only
-- decisions log. Pinned decisions are short-lived authoritative statements
-- ("we're using psycopg3", "pricing tier X is $20") that supersede each
-- other. The append-only log captures every micro-decision; pinned holds
-- the current truth. status='superseded' rows keep history; UI filters
-- to 'active'.
CREATE TABLE IF NOT EXISTS decisions_pinned (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'TECHNICAL',
    priority TEXT NOT NULL DEFAULT 'normal',
    edit_log TEXT,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active','superseded')),
    superseded_by TEXT REFERENCES decisions_pinned(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 0b711a9d — insights: durable strategic understanding, a first-class knowledge
-- type distinct from decisions (choices w/ lifecycle) and notes (reference).
-- horizon ∈ permanent|year|quarter (validated in Python, no DB CHECK so the
-- vocabulary can grow without a table rebuild). permanent insights always
-- surface in get_planning_brief.
CREATE TABLE IF NOT EXISTS insights (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    horizon TEXT NOT NULL DEFAULT 'quarter',
    tags TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- v0.9 — project_notes: per-project wiki for setup, gotchas, env vars,
-- how-tos. Plain table; no goal hierarchy, no version, no history.
-- Tags are comma-separated free-form (setup, gotcha, howto, env, ...).
CREATE TABLE IF NOT EXISTS project_notes (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    tags TEXT,
    note_kind TEXT,
    slug TEXT,
    file_path TEXT,
    symbol TEXT,
    source TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- v2.4 — hitl_requests: human-in-the-loop coordination queue. Sessions
-- can pause waiting for a human answer (urgency='blocking') or surface a
-- non-blocking question (urgency='normal'/'high'). assigned_to routes to
-- a specific human_id; null = broadcast. answered_at + answered_by close
-- the loop for audit.
CREATE TABLE IF NOT EXISTS hitl_requests (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    session_id TEXT REFERENCES sessions(id),
    question TEXT NOT NULL,
    context TEXT,
    urgency TEXT NOT NULL DEFAULT 'normal'
        CHECK (urgency IN ('normal','high','blocking')),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','answered','dismissed')),
    answer TEXT,
    answered_by TEXT,
    assigned_to TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    answered_at TEXT,
    kind TEXT NOT NULL DEFAULT 'question',
    payload TEXT
);

CREATE INDEX IF NOT EXISTS idx_goal_project
    ON goal_states(project_id);
CREATE INDEX IF NOT EXISTS idx_sessions_project
    ON sessions(project_id, status);
CREATE INDEX IF NOT EXISTS idx_decisions_pinned_project
    ON decisions_pinned(project_id, status);
CREATE INDEX IF NOT EXISTS idx_hitl_project
    ON hitl_requests(project_id, status);
CREATE INDEX IF NOT EXISTS idx_hitl_assigned
    ON hitl_requests(assigned_to, status);
CREATE INDEX IF NOT EXISTS idx_notes_project
    ON project_notes(project_id);
CREATE INDEX IF NOT EXISTS idx_tasks_project
    ON task_log(project_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tasks_session
    ON task_log(session_id);
CREATE INDEX IF NOT EXISTS idx_sprint_items_project
    ON sprint_items(project_id, status);
CREATE INDEX IF NOT EXISTS idx_sprint_items_version
    ON sprint_items(project_id, version);

-- v2.0 — hosted tier: tenants, web sessions, API bearer tokens
-- v2.2 — plan updated to standard/pro (was free/pro/team)
-- v2.9 — free tier re-introduced as default plan for new signups
CREATE TABLE IF NOT EXISTS tenants (
    id TEXT PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    google_sub TEXT UNIQUE,
    github_sub TEXT UNIQUE,
    microsoft_sub TEXT UNIQUE,
    neon_project_id TEXT,
    neon_db_url TEXT,
    stripe_customer_id TEXT,
    stripe_metered_item_id TEXT,
    plan TEXT NOT NULL DEFAULT 'free',
    pool_project_id TEXT,
    notification_prefs TEXT NOT NULL DEFAULT '{}',
    trial_started_at TEXT,
    inactivity_expires_at TEXT,
    is_internal INTEGER NOT NULL DEFAULT 0,
    tunnel_active INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS user_sessions (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS api_tokens (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    token_hash TEXT NOT NULL UNIQUE,
    label TEXT,
    token_type TEXT NOT NULL DEFAULT 'readwrite',
    expires_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS oauth_tokens (
    token_hash TEXT PRIMARY KEY,
    tenant_id TEXT REFERENCES tenants(id),
    client_id TEXT,
    exp BIGINT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS oauth_codes (
    code TEXT PRIMARY KEY,
    tenant_id TEXT,
    redirect_uri TEXT NOT NULL,
    code_challenge TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- RFC 8628 device authorization grant (e9f18530). device_code / user_code
-- hold SHA-256 HASHES of the codes, never the raw values. last_polled_at
-- backs the poll-rate limiter (RFC 8628 slow_down).
CREATE TABLE IF NOT EXISTS device_codes (
    device_code TEXT PRIMARY KEY,
    user_code TEXT NOT NULL UNIQUE,
    tenant_id TEXT,
    expires_at TEXT NOT NULL,
    approved INTEGER NOT NULL DEFAULT 0,
    denied INTEGER NOT NULL DEFAULT 0,
    last_polled_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- v2.1 dark — multi-user roles, not exposed in UI or API at launch
-- G5.19/G5.20 — role widened to include 'admin'; github_access caps
-- repo-touching MCP tools per invitee. App layer (meridian.roles) is the
-- source of truth for valid values; no DB-level CHECK so adding a future
-- role doesn't require a table rebuild on every SQLite install.
CREATE TABLE IF NOT EXISTS workspace_members (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    email TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'member',
    github_access TEXT NOT NULL DEFAULT 'read',
    -- d116642e: project-level invites foundation.
    -- NULL = workspace-wide member (sees all projects, current behavior);
    -- set = project-scoped member (listing-only scoping, see get_workspaces_for_email).
    -- Airtight per-request access enforcement is intentionally NOT implemented
    -- here — it is gated on the open product decision in pin b11c7cf6.
    project_id TEXT,
    token_hash TEXT,
    invited_at TEXT NOT NULL DEFAULT (datetime('now')),
    joined_at TEXT
);

-- v2.1 dark — per-tenant named environments (prod/staging/dev), not exposed at launch
CREATE TABLE IF NOT EXISTS tenant_environments (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    name TEXT NOT NULL,
    neon_db_name TEXT,
    token_hash TEXT,
    is_default INTEGER NOT NULL DEFAULT 0
        CHECK (is_default IN (0,1))
);

-- v2.2 — Neon pool project registry.  Each pool project holds up to
-- MAX_CUSTOMERS_PER_PROJECT customer databases.  Pool projects are
-- provisioned lazily: a new Neon project is created when all existing
-- ones are full.  Tier 'standard' uses NEON_API_KEY; 'pro' uses NEON_API_KEY_PRO.
CREATE TABLE IF NOT EXISTS neon_pool_projects (
    id TEXT PRIMARY KEY,
    neon_project_id TEXT NOT NULL UNIQUE,
    tier TEXT NOT NULL DEFAULT 'standard'
        CHECK (tier IN ('free','standard','pro')),
    customer_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- v2.5 — admins: DB-managed admin email list. Replaces MERIDIAN_ADMIN_EMAILS env var.
-- Env var still works as bootstrap fallback (before DB is available).
CREATE TABLE IF NOT EXISTS admins (
    id TEXT PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    added_by TEXT,
    added_at TEXT NOT NULL DEFAULT (datetime('now')),
    notes TEXT
);

-- v2.6 — session_notes: ephemeral per-session scratch pad.
-- Auto-deleted when session closes. Exposed via add_sprint_note / get_sprint_notes MCP tools.
CREATE TABLE IF NOT EXISTS session_notes (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    -- 0d7de2a2: thinking_sync. note_kind classifies a sprint note. NULL/'note'
    -- = a normal note; 'thinking' = a HOOKS_DEBUG_STATE scratchpad note
    -- auto-persisted by the client-side thinking_sync hook. The dashboard
    -- renders 'thinking' notes with a distinct icon.
    note_kind TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- v3.0 — executor_runs: one row per Claude Code / worker session execution.
-- transcript accumulates task descriptions as the session logs work;
-- finalized on session close with ended_at + task_count.
CREATE TABLE IF NOT EXISTS executor_runs (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    ended_at TEXT,
    status TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN ('running','done','failed')),
    transcript TEXT NOT NULL DEFAULT '',
    task_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_executor_runs_session
    ON executor_runs(session_id);
CREATE INDEX IF NOT EXISTS idx_executor_runs_project
    ON executor_runs(project_id, started_at DESC);

CREATE TABLE IF NOT EXISTS file_locks (
    id TEXT PRIMARY KEY,
    file_path TEXT NOT NULL UNIQUE,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    claimed_at TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_file_locks_session
    ON file_locks(session_id);
CREATE INDEX IF NOT EXISTS idx_file_locks_expires
    ON file_locks(expires_at);

-- 501ec93f — resource_locks: generalize file_locks to any typed resource
-- (file:path, db:migrations, mcp_tool:name, route:METHOD:/path, pypi:publish,
-- github:tag). Same TTL + UNIQUE primitive: one holder per resource_id at a
-- time, auto-expiring by TTL or owning-session heartbeat.
CREATE TABLE IF NOT EXISTS resource_locks (
    id TEXT PRIMARY KEY,
    resource_id TEXT NOT NULL UNIQUE,
    resource_type TEXT NOT NULL,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    claimed_at TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_resource_locks_session
    ON resource_locks(session_id);
CREATE INDEX IF NOT EXISTS idx_resource_locks_expires
    ON resource_locks(expires_at);
CREATE INDEX IF NOT EXISTS idx_resource_locks_type
    ON resource_locks(resource_type);

-- Symbol-level parallel protection (4bac57ff): claim individual class/function
-- /method line ranges so two sessions can edit the same file without colliding.
CREATE TABLE IF NOT EXISTS file_symbol_claims (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    file_path TEXT NOT NULL,
    symbol_name TEXT NOT NULL,
    symbol_type TEXT NOT NULL,
    line_start INTEGER NOT NULL,
    line_end INTEGER NOT NULL,
    claimed_at TEXT NOT NULL DEFAULT (datetime('now')),
    -- Soft-release: an active claim has released_at IS NULL; released rows are
    -- retained so hotspot scoring can count distinct sessions over time.
    released_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_file_symbol_claims_file
    ON file_symbol_claims(file_path);
CREATE INDEX IF NOT EXISTS idx_file_symbol_claims_session
    ON file_symbol_claims(session_id);

-- Blog CMS (6234f9b8): admin-authored posts; draft until published.
-- 8843250f: workspace-scoped via tenant_id + archived lifecycle status.
CREATE TABLE IF NOT EXISTS blog_posts (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    slug TEXT NOT NULL UNIQUE,
    body_md TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft','published','archived')),
    tenant_id TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    published_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_blog_posts_status ON blog_posts(status);
-- idx_blog_posts_tenant is created by _migrate_blog_posts_tenant (which ALTERs
-- tenant_id onto pre-8843250f tables first). It must NOT be inline here: this
-- literal is run via an unguarded executescript, and on an existing DB
-- CREATE TABLE IF NOT EXISTS keeps the old columnless blog_posts, so an inline
-- CREATE INDEX ... (tenant_id) crashes startup ('no such column: tenant_id').
-- Same missing-column class took prod down on the 2026-07-04 promote.

CREATE TABLE IF NOT EXISTS active_worktrees (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    project_id TEXT NOT NULL REFERENCES projects(id),
    item_id TEXT,
    branch TEXT NOT NULL,
    path TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    removed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_active_worktrees_session
    ON active_worktrees(session_id);
CREATE INDEX IF NOT EXISTS idx_active_worktrees_project
    ON active_worktrees(project_id, removed_at);

-- v3.1 — workspace layer: tenant-global notes + decisions that live above
-- individual projects. Unlike project_notes / decisions_pinned (keyed by
-- project_id), these belong to the workspace as a whole (one workspace per
-- Meridian instance / hosted tenant DB) and are injected at the top of every
-- project's context block + handoff. Tags are comma-separated free-form.
CREATE TABLE IF NOT EXISTS workspace_notes (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    tags TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS workspace_decisions (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'TECHNICAL',
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active','superseded')),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_workspace_notes_created
    ON workspace_notes(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_workspace_decisions_status
    ON workspace_decisions(status, created_at DESC);

-- workspace_sprint_items: a tenant-global personal backlog that is NOT tied to
-- any single project, so a solo dev can track cross-project goals (thesis,
-- Meridian, personal) in one board. Mirrors the useful subset of sprint_items
-- but keyed by tenant_id instead of project_id; ``item_group`` is the
-- cross-project bucket (e.g. 'thesis'/'meridian'/'personal'). ``position``
-- orders items within a group. One workspace per DB, same isolation model as
-- workspace_notes / workspace_decisions.
CREATE TABLE IF NOT EXISTS workspace_sprint_items (
    id TEXT PRIMARY KEY,
    tenant_id TEXT,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'todo'
        CHECK (status IN ('todo','pending','in_progress','done','skipped','failed')),
    item_group TEXT,
    human_id TEXT,
    position INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at TEXT
);
-- idx_workspace_sprint_items_tenant is created by _migrate_workspace_sprint_board.
-- Not inline here for the same reason as idx_blog_posts_tenant above: an inline
-- CREATE INDEX on tenant_id in this unguarded literal would crash startup on any
-- pre-existing copy of this table that lacks the column.

-- v3.4 — workspace-level settings: tenant-global defaults that every project
-- session can read at startup (e.g. a default HITL auto-answer posture, a
-- default sprint label). Singleton row (id='singleton') per workspace DB, same
-- one-workspace-per-DB model as workspace_notes / workspace_decisions.
CREATE TABLE IF NOT EXISTS workspace_settings (
    id TEXT PRIMARY KEY DEFAULT 'singleton',
    hitl_auto_answer_default INTEGER NOT NULL DEFAULT 0,
    sprint_name_default TEXT,
    display_name TEXT,
    log_task_sprint_nudge_threshold INTEGER NOT NULL DEFAULT 5,
    handoff_template TEXT,
    execution_mode_default TEXT,
    code_intel_enabled_default INTEGER,
    loop_enabled_default INTEGER NOT NULL DEFAULT 1,
    auto_refresh_enabled INTEGER NOT NULL DEFAULT 0,
    refresh_interval_turns INTEGER NOT NULL DEFAULT 10,
    refresh_triggers TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS feedback (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    type TEXT NOT NULL,
    message TEXT NOT NULL,
    email TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_feedback_tenant
    ON feedback(tenant_id);
CREATE INDEX IF NOT EXISTS idx_feedback_created
    ON feedback(created_at);

-- 2976e168 — sprint_item_pointers: the GENERIC POINTER PRIMITIVE. ONE table for
-- pointers of ANY source_type (code/docs/citation/…), keyed to a sprint item.
-- ``targets`` is a JSON array of {uri, selector, subSelector?} objects (native
-- multi-file, LSP WorkspaceEdit pattern); the per-target selector.type
-- (range|symbol|node_id|zotero_key) is what the resolver dispatches on. Storing
-- the composite shape as JSON — NOT per-domain columns — is the core design
-- requirement (W3C Web Annotation Selector composition, LSP Location). The
-- idx_sprint_item_pointers_item index is created ONLY inside the guarded
-- _migrate_sprint_item_pointers migration, never inline here — an unguarded
-- CREATE INDEX in this base literal would crash startup on a DB whose table
-- predates it (the 2026-07-04 inline-index outage trap).
CREATE TABLE IF NOT EXISTS sprint_item_pointers (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    sprint_item_id TEXT NOT NULL,
    source_type TEXT NOT NULL,
    targets TEXT NOT NULL,
    label TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 3295c784 — mcp_rate_counters: cross-instance (shared) hit-counting for the
-- consolidated /mcp tenant-tier rate limiter. Per-process _tenant_rl_hits under-
-- counts across N Fly machines (effective limit ~Nx). This windowed counter is
-- keyed by (tenant_id, window_start) where window_start is an epoch-minute
-- bucket; increment_rate_counter does an atomic upsert (count = count + 1) so
-- concurrent requests across instances agree on a single shared count. Gated by
-- MERIDIAN_SHARED_RATE_LIMIT (default OFF — the per-process path is unchanged).
-- The composite PRIMARY KEY already indexes (tenant_id, window_start); the
-- prune-by-window index lives ONLY in the guarded _migrate_mcp_rate_counters
-- migration, never inline here (2026-07-04 inline-index outage trap).
CREATE TABLE IF NOT EXISTS mcp_rate_counters (
    tenant_id TEXT NOT NULL,
    window_start INTEGER NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant_id, window_start)
);

-- 5c4dcc0f — workspace_proposals: human-only "drawer of inspiration" for
-- cross-project flashes of insight. Workspace-scoped (tenant_id, not
-- project_id). Distinct from workspace_notes (no lifecycle) and sprint items
-- (executor-claimable). status enum: raw → investigating → promoted|rejected.
-- promoted_to_sprint_item_id links a promoted proposal to the sprint item it
-- became. NOT auto-claimable by executors — human-reviewed promotion gate only.
-- idx_workspace_proposals_tenant is created by _migrate_workspace_proposals
-- (guarded migration), never inline here (2026-07-04 inline-index outage rule).
CREATE TABLE IF NOT EXISTS workspace_proposals (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    tags TEXT,
    status TEXT NOT NULL DEFAULT 'raw'
        CHECK (status IN (
            'raw', 'investigating', 'paused', 'promoted', 'rejected',
            'closed', 'superseded'
        )),
    promoted_to_sprint_item_id TEXT,
    tenant_id TEXT,
    family_id TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_activity_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- v0.2.2 proposal lifecycle — immutable evidence and decision history.
-- Events are append-only records owned by a proposal.  The flexible payload
-- keeps the initial schema stable while later lifecycle features add typed
-- evidence, decisions, pointers, and resume metadata.
CREATE TABLE IF NOT EXISTS proposal_events (
    id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL,
    tenant_id TEXT,
    sequence INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    payload TEXT,
    actor TEXT,
    session_id TEXT,
    source TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 356d6ac8 — file_patch_counters: structural-degradation early-warning signal.
-- Tracks how many times (session_id, file_path) has been write-claimed within a
-- session, approximating "how many patch cycles hit this file without a refactor."
-- refactor_flagged is set by the executor when the session contains a deliberate
-- refactor of this file (resets the degradation signal). patch_count is
-- incremented on every exclusive write claim. get_structural_degradation_warnings
-- queries rows where patch_count >= threshold AND refactor_flagged = 0.
-- idx_file_patch_counters_session is created ONLY inside the guarded
-- _migrate_file_patch_counters migration, never inline here (2026-07-04
-- inline-index outage rule).
CREATE TABLE IF NOT EXISTS file_patch_counters (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    file_path TEXT NOT NULL,
    patch_count INTEGER NOT NULL DEFAULT 0,
    refactor_flagged INTEGER NOT NULL DEFAULT 0,
    first_patched_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_patched_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (session_id, file_path)
);

-- 8c147109 — session_activity: lightweight ring-buffer heartbeat feed.
-- Records one row per significant MCP tool call in an executor session so a
-- remote planner can see signs of life via get_session_log even before the
-- executor calls log_task(). Bounded to the last 50 entries per session
-- (enforced by record_session_activity at write time — no DB trigger needed).
-- idx_session_activity_session is created by _migrate_session_activity
-- (guarded migration), never inline here (2026-07-04 inline-index outage rule).
CREATE TABLE IF NOT EXISTS session_activity (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    tool_name TEXT NOT NULL,
    summary TEXT NOT NULL,
    recorded_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- b12cc29f — connection_events: per-/mcp-request auth+method event log.
-- Every real HTTP /mcp request is written here with the auth outcome, MCP
-- method, tool count (for tools/list), User-Agent, and response status so a
-- live or post-mortem client-side outage can be diagnosed without raw log
-- access. Capped at 1000 rows per tenant_id (enforced at write time).
-- idx_connection_events_tenant is created by _migrate_connection_events
-- (guarded migration), never inline here.
CREATE TABLE IF NOT EXISTS connection_events (
    id TEXT PRIMARY KEY,
    tenant_id TEXT,
    method TEXT NOT NULL DEFAULT '',
    auth_result TEXT NOT NULL DEFAULT 'unknown',
    tools_returned INTEGER,
    client_user_agent TEXT,
    response_status INTEGER NOT NULL DEFAULT 200,
    recorded_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def get_default_human_id() -> str | None:
    """Return the best available human identifier for the current environment.

    Checks in order: ``MERIDIAN_HUMAN_ID`` env var, ``USER`` (POSIX),
    ``USERNAME`` (Windows), then the short hostname.  Returns ``None`` only
    when none of the above are set (very unusual).
    """
    import socket

    return (
        os.environ.get("MERIDIAN_HUMAN_ID")
        or os.environ.get("USER")
        or os.environ.get("USERNAME")
        or (socket.gethostname().split(".")[0][:20] or None)
    )


def _new_id() -> str:
    """Return a fresh uuid4 string."""
    return str(uuid.uuid4())


def _row_to_dict(row: aiosqlite.Row | None) -> dict[str, Any] | None:
    """Convert an aiosqlite Row to a plain dict, or None."""
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}


def _decode_content(raw: str) -> Any:
    """Goal content is stored as text. If it parses as JSON, return the
    parsed object; otherwise return the raw string."""
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return raw


def _encode_content(content: Any) -> str:
    """Serialize goal content to text for storage."""
    if isinstance(content, str):
        return content
    return json.dumps(content)


from .migrations import *  # noqa: F401,F403

async def init_db(db_path: str) -> aiosqlite.Connection:
    """Open the database, apply schema, and return the connection.

    When ``db_path`` starts with ``postgresql://`` or ``postgres://`` the
    function opens a psycopg3 connection pool and returns a
    :class:`~meridian.pg_adapter.PostgresConnection` instead of an
    aiosqlite connection.  Both expose the same async cursor API so all
    callers are unaffected.

    For Postgres, only ``CREATE TABLE IF NOT EXISTS`` is run â€” migration
    helpers are skipped because a fresh Postgres DB already has the full
    current schema.

    The caller owns the returned connection and is responsible for closing it.
    """
    if db_path.startswith(("postgresql://", "postgres://")):
        from ..pg_adapter import init_pg_db  # local import keeps SQLite path fast

        return await init_pg_db(db_path)  # type: ignore[return-value]

    db = await aiosqlite.connect(db_path)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode = WAL")   # concurrent read+write
    await db.execute("PRAGMA busy_timeout = 5000")  # retry up to 5 s before LOCKED
    await db.execute("PRAGMA foreign_keys = ON")
    await db.executescript(CREATE_TABLES)
    await db.commit()
    await _migrate_task_log_hitl(db)
    await _migrate_task_log_backlog_future(db)
    await _migrate_task_log_backburner(db)
    await _migrate_human_identity(db)
    await _migrate_task_claims(db)
    await _migrate_task_sprint_link(db)
    await _migrate_decisions(db)
    await _migrate_session_type(db)
    await _migrate_session_summary(db)
    await _migrate_parent_session_id(db)
    await _migrate_goal_mode(db)
    await _migrate_goal_hierarchy(db)
    await _migrate_worker_pid(db)
    await _migrate_rewind_token(db)
    await _migrate_project_settings(db)
    await _migrate_neon_pool_projects_free_tier(db)
    await _migrate_sessions_archived(db)
    await _migrate_sprint_items_v2(db)
    await _migrate_drop_chat_tables(db)
    await _migrate_hosted_tables(db)
    await _migrate_goal_field_timestamps(db)
    await _migrate_v24_task_tree_and_framework(db)
    await _migrate_v24_pinned_decisions_and_hitl(db)
    await _migrate_v09_notes_and_magic_links(db)
    await _migrate_v25_feedback_and_notifications(db)
    await _migrate_dunning_fields(db)
    await _migrate_overage_fields(db)
    await _migrate_sprint_item_dependencies(db)
    await _migrate_v26_client_type(db)
    await _migrate_decisions_free_category(db)
    await _migrate_tenants_free_plan(db)
    await _migrate_session_notes(db)
    await _migrate_executor_runs(db)
    await _migrate_file_locks(db)
    await _migrate_file_symbol_claims(db)
    await _migrate_blog_posts(db)
    await _migrate_milestone_type(db)
    await _migrate_ntfy_notifications(db)
    await _migrate_notify_email(db)
    await _migrate_github_integration(db)
    await _migrate_workspace_layer(db)
    await _migrate_checkpoint_data(db)
    await _migrate_v33_hitl_kind_payload(db)
    await _migrate_v34_hitl_auto_answer(db)
    await _migrate_v34_workspace_settings(db)
    await _migrate_project_icon(db)
    await _migrate_tenants_is_internal(db)
    await _migrate_admin_plan(db)
    await _migrate_workspace_members_rbac(db)
    await _migrate_workspace_members_project_scope(db)
    await _migrate_sprint_items_indeterminate(db)
    await _migrate_sprint_item_tree(db)
    await _migrate_sprint_items_provisional_complete(db)
    await _migrate_api_token_type(db)
    await _migrate_api_tokens_expires_at(db)
    await _migrate_oauth_codes_table(db)
    await _migrate_device_codes_table(db)
    await _migrate_device_codes_denied_polled(db)
    await _migrate_github_to_projects(db)
    await _migrate_touches_files(db)
    await _migrate_touches_resources(db)
    await _migrate_resource_locks(db)
    await _migrate_sprint_item_stall_count(db)
    await _migrate_active_worktrees(db)
    await _migrate_workspace_tenant_isolation(db)
    await _migrate_workspace_sprint_board(db)
    await _migrate_registered_hostnames(db)
    await _migrate_queued_session(db)
    await _migrate_pending_goal(db)
    await _migrate_parallel_safety(db)
    await _migrate_changelog_entries(db)
    await _migrate_agent_instructions(db)
    await _backfill_agent_instructions(db)
    await _migrate_note_kind(db)
    await _migrate_tunnel_active(db)
    await _migrate_code_intel(db)
    await _migrate_tunnel_plugins(db)
    await _migrate_tunnel_plugins_by_host(db)
    await _migrate_notes_priority(db)
    await _migrate_task_log_kind(db)
    await _migrate_note_slug(db)
    await _migrate_oauth_refresh_tokens(db)
    await _migrate_decision_priority_edit_log(db)
    await _migrate_code_anchored_notes(db)
    await _migrate_note_source(db)
    await _migrate_session_sprint_version(db)
    await _migrate_project_execution_mode(db)
    await _migrate_decision_code_anchor(db)
    await _migrate_session_graph_snapshots(db)
    await _migrate_agent_tasks_table(db)
    await _migrate_sprint_item_owner(db)
    await _migrate_session_note_kind(db)
    await _migrate_handoffs_table(db)
    await _migrate_decision_assumption(db)
    await _migrate_github_connections(db)
    await _migrate_sprint_item_quality_gates(db)
    await _migrate_parallel_primitives(db)
    await _migrate_project_status_priority(db)
    await _migrate_signup_attempts(db)
    await _migrate_user_session_metadata(db)
    await _migrate_provision_queue(db)
    await _migrate_codebase_graph_entities(db)
    await _migrate_insights_table(db)
    await _migrate_sprint_item_slug(db)
    await _migrate_sprint_item_nickname(db)
    await _migrate_capture_insight_notes_to_insights(db)
    await _migrate_blog_posts_tenant(db)
    await _migrate_project_parent_id(db)
    await _migrate_session_goal_compliance(db)
    await _migrate_sprint_item_pointers(db)
    await _migrate_sprint_item_deferral(db)
    await _migrate_sprint_item_priority_blocker(db)
    await _migrate_sprint_item_wave(db)
    await _migrate_mcp_rate_counters(db)
    await _migrate_workspace_proposals(db)
    await _migrate_docx_region_claims(db)
    await _migrate_pending_goal_at(db)
    await _migrate_file_patch_counters(db)
    await _migrate_sprint_item_resources_amended(db)
    await _migrate_session_activity(db)
    await _migrate_connection_events(db)
    await _migrate_redis_overage_fields(db)
    await _migrate_sprint_version_descriptions(db)
    await _migrate_workspace_settings_active_session_threshold(db)
    await _migrate_sprint_item_sprint_name(db)
    await _migrate_proposal_slug_nickname(db)
    await _migrate_decision_slug_nickname(db)
    await _migrate_note_nickname(db)
    await _migrate_sprint_item_prospect_bypass(db)
    await _migrate_handoff_tokens(db)
    await _migrate_wave_gate_results(db)
    await _migrate_wave_gate_configs(db)
    await _migrate_server_logs(db)
    await _migrate_custom_hooks(db)
    await _migrate_sprint_item_require_verification(db)
    await _migrate_sprint_item_verifications_table(db)
    await _migrate_proposal_github_issue(db)
    await _migrate_sprint_item_required_tool(db)
    await _migrate_sprint_item_github_issue_link(db)
    await _migrate_manual_issue_screening_toggle(db)
    await _migrate_action_audit_log_table(db)
    await _migrate_manual_issue_content_log_table(db)
    await _migrate_workspace_tool_priority_map(db)
    await _migrate_sprint_item_github_channel(db)
    await _migrate_workspace_claim_verification_mode(db)
    await _migrate_handoff_tokens_consumed_at(db)
    await _migrate_board_snapshot_revisions(db)
    await _migrate_wave_runs(db)
    await _migrate_handoff_tokens_body_hash(db)
    await _migrate_project_capabilities(db)
    await _migrate_capability_profiles(db)
    await _migrate_sprint_item_tool_requirements(db)
    await _migrate_sprint_item_artifact_declaration(db)
    await _migrate_docx_merge_manifests(db)
    await _migrate_proposal_evidence_links(db)
    await _migrate_wave_base_manifests(db)
    await _migrate_sprint_batch_claims(db)
    await _migrate_verification_runs(db)
    await _migrate_sprint_item_require_strict_evidence(db)
    await _migrate_handoffs_invalidation(db)
    await _migrate_handoff_corrections_table(db)
    await _migrate_vector_index_state(db)
    await _migrate_pixi_env_roots(db)
    await _migrate_executor_reports_table(db)
    await _migrate_wave_run_summaries(db)
    await _migrate_decision_evidence(db)
    await _migrate_ai_log_events_table(db)
    await _migrate_proposal_intake_drafts(db)
    await _migrate_profile_layers(db)
    return db


async def create_project(
    db: aiosqlite.Connection,
    name: str,
    human_id: str | None = None,
    execution_mode: str | None = None,
    tenant_id: str | None = None,
    parent_project_id: str | None = None,
) -> dict[str, Any]:
    """Insert a project and return it as a dict. Raises if the name exists.

    ``human_id`` (when provided) is recorded as the project's
    ``creator_human_id``. The creator's id is the only one allowed to
    update the goal state once goal-ownership enforcement is active.

    ``execution_mode`` (ecf69de8) sets the project's executor posture —
    'autonomous' (default) or 'interactive'. Invalid values normalise to
    'autonomous' so a bad input never persists.

    ``tenant_id`` (0bf67524) — when given, the new project is seeded from the
    workspace's cascade defaults (execution_mode_default, hitl_auto_answer_default,
    code_intel_enabled_default) for any field the caller didn't set explicitly.
    Existing projects are never touched; this is a creation-time cascade only.

    ``parent_project_id`` (3b6ff466) — when given, the new project is a
    subproject of that parent. The hierarchy is ONE level deep: the parent
    must exist AND must itself be top-level (its own parent_project_id is
    NULL) — a grandchild is rejected with ValueError. A subproject with no
    north_star of its own falls back to its parent's north_star (see
    ``get_goal``). Top-level projects pass parent_project_id=None (default).
    """
    # 3b6ff466 — validate the subproject parent before inserting anything.
    if parent_project_id is not None:
        parent = await get_project(db, parent_project_id)
        if parent is None:
            raise ValueError(
                f"parent project '{parent_project_id}' does not exist"
            )
        if parent.get("parent_project_id"):
            raise ValueError(
                "subprojects are one level deep — cannot nest under a "
                f"project that is itself a subproject "
                f"('{parent_project_id}' has a parent)"
            )
    # Resolve workspace defaults for seeding (best-effort; never block creation).
    _ws: dict[str, Any] = {}
    if tenant_id is not None:
        try:
            _ws = await get_workspace_settings(db, tenant_id=tenant_id)
        except Exception:  # noqa: BLE001 — missing/old workspace_settings → no seed
            _ws = {}
    if execution_mode is None and _ws.get("execution_mode_default"):
        execution_mode = _ws["execution_mode_default"]
    pid = _new_id()
    mode = normalize_execution_mode(execution_mode)
    await db.execute(
        "INSERT INTO projects "
        "(id, name, creator_human_id, execution_mode, parent_project_id) "
        "VALUES (?, ?, ?, ?, ?)",
        (pid, name, human_id, mode, parent_project_id),
    )
    await db.commit()
    # Seed the remaining cascade defaults onto the fresh project row.
    _ci_default = _ws.get("code_intel_enabled_default")
    if _ci_default is not None:
        await update_project_settings(db, pid, code_intel_enabled=1 if _ci_default else 0)
    if _ws.get("hitl_auto_answer_default"):
        # Workspace default ON → seed the project to "safe" (mode 1) auto-answer.
        await update_project_settings(db, pid, hitl_auto_answer=1)
    project = await get_project(db, pid)
    assert project is not None
    return project


async def set_project_execution_mode(
    db: aiosqlite.Connection, project_id: str, mode: str
) -> dict[str, Any] | None:
    """Set the executor posture for a project. Invalid values normalise to
    'autonomous'. Returns the updated project dict, or None if not found.
    """
    normalized = normalize_execution_mode(mode)
    await db.execute(
        "UPDATE projects SET execution_mode = ? WHERE id = ?",
        (normalized, project_id),
    )
    await db.commit()
    return await get_project(db, project_id)


async def get_project(
    db: aiosqlite.Connection, project_id: str
) -> dict[str, Any] | None:
    """Look up a project by id."""
    async with db.execute(
        "SELECT * FROM projects WHERE id = ?", (project_id,)
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


_PROJECT_STATUSES = ("active", "parked", "archived")
_PROJECT_PRIORITIES = ("P0", "P1", "P2")


async def set_project_status(
    db: aiosqlite.Connection,
    project_id: str,
    status: str | None = None,
    priority: str | None = None,
) -> dict[str, Any] | None:
    """8db00fcb — update a project's organization fields (status/priority).
    Validates the enums; only the provided fields change."""
    sets: list[str] = []
    params: list[Any] = []
    if status is not None:
        if status not in _PROJECT_STATUSES:
            raise ValueError(
                f"invalid status {status!r}; expected one of {_PROJECT_STATUSES}"
            )
        sets.append("status = ?")
        params.append(status)
    if priority is not None:
        if priority not in _PROJECT_PRIORITIES:
            raise ValueError(
                f"invalid priority {priority!r}; expected one of {_PROJECT_PRIORITIES}"
            )
        sets.append("priority = ?")
        params.append(priority)
    if sets:
        params.append(project_id)
        await db.execute(
            f"UPDATE projects SET {', '.join(sets)} WHERE id = ?", params
        )
        await db.commit()
    return await get_project(db, project_id)


async def get_agent_instructions(
    db: aiosqlite.Connection, project_id: str
) -> str | None:
    """Return the agent_instructions field for a project, or None if not set."""
    async with db.execute(
        "SELECT agent_instructions FROM projects WHERE id = ?", (project_id,)
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    val = row["agent_instructions"] if isinstance(row, dict) else row[0]
    return val or None


async def set_agent_instructions(
    db: aiosqlite.Connection, project_id: str, instructions: str | None
) -> dict[str, Any]:
    """Set agent_instructions for a project. Pass None to clear."""
    await db.execute(
        "UPDATE projects SET agent_instructions = ? WHERE id = ?",
        (instructions or None, project_id),
    )
    await db.commit()
    return {"project_id": project_id, "agent_instructions": instructions or None}


async def get_project_by_name(
    db: aiosqlite.Connection, name: str
) -> dict[str, Any] | None:
    """Look up a project by name using exact, then fuzzy case-insensitive match."""
    async with db.execute(
        "SELECT p.*, gs.goal_sprint AS sprint "
        "FROM projects p "
        "LEFT JOIN goal_states gs ON gs.project_id = p.id "
        "WHERE p.name = ? "
        "ORDER BY gs.version DESC "
        "LIMIT 1",
        (name,),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        # Case-insensitive EXACT match fallback (not substring — avoids false 409s)
        async with db.execute(
            "SELECT p.*, gs.goal_sprint AS sprint "
            "FROM projects p "
            "LEFT JOIN goal_states gs ON gs.project_id = p.id "
            "WHERE LOWER(p.name) = ? "
            "ORDER BY p.created_at DESC, gs.version DESC "
            "LIMIT 1",
            (name.lower(),),
        ) as cur:
            row = await cur.fetchone()
    return _row_to_dict(row)


async def list_projects(db: aiosqlite.Connection) -> list[dict[str, Any]]:
    """Return every project row, newest first."""
    async with db.execute(
        "SELECT * FROM projects ORDER BY created_at DESC"
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows]  # type: ignore[misc]


async def list_project_summaries(db: aiosqlite.Connection) -> list[dict[str, Any]]:
    """Return project discovery summaries with sprint metadata, newest first."""
    async with db.execute(
        "SELECT p.id, p.name, gs.goal_sprint AS sprint, p.created_at "
        "FROM projects p "
        "LEFT JOIN goal_states gs "
        "ON gs.project_id = p.id "
        "AND gs.version = (SELECT MAX(version) FROM goal_states WHERE project_id = p.id) "
        "ORDER BY p.created_at DESC"
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows]  # type: ignore[misc]


async def rename_project(
    db: aiosqlite.Connection, project_id: str, new_name: str
) -> dict[str, Any] | None:
    """Rename a project. Returns the updated project dict or None if not found."""
    await db.execute(
        "UPDATE projects SET name = ? WHERE id = ?",
        (new_name, project_id),
    )
    await db.commit()
    return await get_project(db, project_id)


async def set_parent_project(
    db: aiosqlite.Connection,
    project_id: str,
    parent_project_id: str | None,
) -> dict[str, Any] | None:
    """7acb8563 — set / change / clear a project's parent AFTER creation.

    ``create_project`` only accepted ``parent_project_id`` at creation time; this
    makes the same relationship editable so retroactively fixing a phantom-duplicate
    (e.g. ms-thesis under Camerer_MS_Graduation) no longer needs a raw DB write.

    Enforces the SAME one-level-deep invariant as ``create_project`` (3b6ff466):
    the parent must exist and itself be top-level, a project cannot be its own
    parent, and a project that already HAS subprojects cannot become a subproject
    (that would create a two-level chain). Pass ``parent_project_id=None`` to
    detach (make it top-level again). Validates BEFORE any write; raises
    ``ValueError`` on any violation. Returns the updated project, or ``None`` if
    ``project_id`` does not exist.
    """
    project = await get_project(db, project_id)
    if project is None:
        return None
    if parent_project_id is not None:
        if parent_project_id == project_id:
            raise ValueError("a project cannot be its own parent")
        parent = await get_project(db, parent_project_id)
        if parent is None:
            raise ValueError(
                f"parent project '{parent_project_id}' does not exist"
            )
        if parent.get("parent_project_id"):
            raise ValueError(
                "subprojects are one level deep — cannot nest under a project "
                f"that is itself a subproject ('{parent_project_id}' has a parent)"
            )
        async with db.execute(
            "SELECT 1 FROM projects WHERE parent_project_id = ? LIMIT 1",
            (project_id,),
        ) as cur:
            has_children = await cur.fetchone() is not None
        if has_children:
            raise ValueError(
                f"project '{project_id}' has subprojects of its own — cannot make "
                "it a subproject (subprojects are one level deep)"
            )
    await db.execute(
        "UPDATE projects SET parent_project_id = ? WHERE id = ?",
        (parent_project_id, project_id),
    )
    await db.commit()
    return await get_project(db, project_id)


# d6bd60e0 — every table that carries a project_id FK to projects(id) and whose
# rows should follow the project when two projects are merged. This is the
# authoritative re-parent set for merge_project: a source project's child rows
# are UPDATEd to the target's id, table by table. Kept as an explicit, auditable
# tuple (rather than reflected from the schema) so a merge NEVER silently skips —
# or silently sweeps up — a table: adding a new project-scoped table is a
# deliberate one-line addition here, mirrored by a test.
#
# NOTE: session_notes / session_findings-style tables keyed ONLY by session_id
# re-parent transitively via ``sessions`` and are intentionally absent. Tables
# listed here each have a real ``project_id`` column (verified against
# CREATE_TABLES + the migration table literals).
_MERGE_PROJECT_TABLES: tuple[str, ...] = (
    "goal_states",
    "sessions",
    "task_log",
    "sprint_items",
    "decisions_pinned",
    "insights",
    "project_notes",
    "hitl_requests",
    "executor_runs",
    "active_worktrees",
    "codebase_graph_entities",
    "handoffs",
    "session_findings",
    "session_messages",
    "session_graph_snapshots",
    "sprint_item_pointers",
    "proposal_evidence_links",
)


async def merge_project(
    conn: aiosqlite.Connection,
    source_project_id: str,
    target_project_id: str,
    *,
    archive_source: bool = True,
) -> dict[str, Any]:
    """d6bd60e0 — merge a phantom-duplicate project INTO another.

    Re-parents every child row of ``source_project_id`` to
    ``target_project_id`` via ``UPDATE ... SET project_id = target WHERE
    project_id = source`` for each table in :data:`_MERGE_PROJECT_TABLES` (the
    full set of tables carrying a ``project_id`` FK to ``projects``). This is a
    pure re-parent: NO row is ever deleted.

    The source project itself is not hard-deleted. When ``archive_source`` is
    true (default) it is soft-archived in place: its ``status`` is set to
    ``'archived'`` and its ``name`` is prefixed with ``'[merged] '`` (unless it
    already carries that prefix), which also frees the original name for reuse.
    Pass ``archive_source=False`` to leave the now-empty source project
    untouched.

    Guards (return an ``{'error': ...}`` dict, mutate nothing):
      * ``source_project_id == target_project_id`` — a project cannot merge into
        itself.
      * either project row does not exist.

    Works on both SQLite and Postgres through the shared adapter (``?`` is
    rewritten to ``%s``; ``autocommit`` — never ``conn.commit()`` on Postgres,
    while the SQLite path commits explicitly, matching the rest of this module).

    Returns::

        {
            "source_project_id": <str>,
            "target_project_id": <str>,
            "moved": {table: count, ...},   # rows re-parented per table
            "source_archived": <bool>,
        }
    """
    if source_project_id == target_project_id:
        return {"error": "source and target project are the same"}

    source = await get_project(conn, source_project_id)
    if source is None:
        return {"error": f"source project '{source_project_id}' does not exist"}
    target = await get_project(conn, target_project_id)
    if target is None:
        return {"error": f"target project '{target_project_id}' does not exist"}

    moved: dict[str, int] = {}
    for table in _MERGE_PROJECT_TABLES:
        # Table names come from the module-local constant tuple, never user
        # input, so this f-string interpolation cannot be an injection vector.
        async with conn.execute(
            f"UPDATE {table} SET project_id = ? WHERE project_id = ?",
            (target_project_id, source_project_id),
        ) as cur:
            moved[table] = cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else 0

    source_archived = False
    if archive_source:
        current_name = source.get("name") or ""
        new_name = (
            current_name
            if current_name.startswith("[merged] ")
            else f"[merged] {current_name}"
        )
        await conn.execute(
            "UPDATE projects SET name = ?, status = 'archived' WHERE id = ?",
            (new_name, source_project_id),
        )
        source_archived = True

    await conn.commit()

    return {
        "source_project_id": source_project_id,
        "target_project_id": target_project_id,
        "moved": moved,
        "source_archived": source_archived,
    }


async def get_project_settings(
    db: aiosqlite.Connection, project_id: str
) -> dict[str, Any] | None:
    """Return the persisted settings for a project."""
    async with db.execute(
        "SELECT id, max_pinned_decisions, executor_config, hitl_auto_answer, "
        "auto_worktrees, require_merge_approval, code_intel_enabled, "
        "execution_mode "
        "FROM projects WHERE id = ?",
        (project_id,),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    data = _row_to_dict(row) or {}
    raw_executor_config = data.get("executor_config")
    try:
        executor_config = json.loads(raw_executor_config) if raw_executor_config else {}
    except (TypeError, ValueError):
        executor_config = {}
    return {
        "project_id": data["id"],
        "max_pinned_decisions": int(data.get("max_pinned_decisions") or 20),
        "executor_config": executor_config,
        # 035edf47 — 0=off, 1=safe, 2=aggressive (was a 0/1 bool).
        "hitl_auto_answer": int(data.get("hitl_auto_answer") or 0),
        # 0716c9e0 — parallel safety toggles; default ON (1).
        "auto_worktrees": int(data.get("auto_worktrees") if data.get("auto_worktrees") is not None else 1),
        # e7548587 — tri-state: 0=off, 1=advisory (default, warn only via
        # HITL), 2=strict (blocks completion on a genuine active, unmerged
        # worktree unless explicitly overridden).
        "require_merge_approval": int(data.get("require_merge_approval") if data.get("require_merge_approval") is not None else 1),
        # Sprint-2/3 — codebase-memory-mcp toggle.
        "code_intel_enabled": int(data.get("code_intel_enabled") or 0),
        # ecf69de8 — executor posture: 'autonomous' (default) | 'interactive'.
        "execution_mode": normalize_execution_mode(data.get("execution_mode")),
    }


async def update_project_settings(
    db: aiosqlite.Connection,
    project_id: str,
    *,
    max_pinned_decisions: int | None = None,
    executor_config: dict[str, Any] | None = None,
    hitl_auto_answer: int | None = None,
    auto_worktrees: int | None = None,
    require_merge_approval: int | None = None,
    code_intel_enabled: int | None = None,
    execution_mode: str | None = None,
    github_repo: str | None = _UNSET,
    github_branch: str | None = _UNSET,
    github_account_login: str | None = _UNSET,
) -> dict[str, Any] | None:
    """Persist project settings and return the updated values."""
    project = await get_project(db, project_id)
    if project is None:
        return None
    updates: list[str] = []
    params: list[Any] = []
    if max_pinned_decisions is not None:
        updates.append("max_pinned_decisions = ?")
        params.append(int(max_pinned_decisions))
    if executor_config is not None:
        updates.append("executor_config = ?")
        params.append(json.dumps(executor_config))
    if hitl_auto_answer is not None:
        # 035edf47 — 0=off, 1=safe, 2=aggressive. Clamp to the valid range.
        updates.append("hitl_auto_answer = ?")
        params.append(max(0, min(2, int(hitl_auto_answer))))
    if auto_worktrees is not None:
        updates.append("auto_worktrees = ?")
        params.append(1 if auto_worktrees else 0)
    if require_merge_approval is not None:
        # e7548587 — tri-state: 0=off, 1=advisory (warn only), 2=strict
        # (blocks). Clamp like hitl_auto_answer's own tri-state, not a bool
        # coercion — a bare truthy coercion here would silently collapse a
        # caller's requested strict (2) down to advisory (1).
        updates.append("require_merge_approval = ?")
        params.append(max(0, min(2, int(require_merge_approval))))
    if code_intel_enabled is not None:
        updates.append("code_intel_enabled = ?")
        params.append(1 if code_intel_enabled else 0)
    if execution_mode is not None:
        # ecf69de8 — normalise to a valid posture so a bad value never persists.
        updates.append("execution_mode = ?")
        params.append(normalize_execution_mode(execution_mode))
    if github_repo is not _UNSET:
        updates.append("github_repo = ?")
        params.append(github_repo or None)
    if github_branch is not _UNSET:
        updates.append("github_branch = ?")
        params.append(github_branch or None)
    if github_account_login is not _UNSET:
        updates.append("github_account_login = ?")
        params.append(github_account_login or None)
    if updates:
        params.append(project_id)
        await db.execute(
            f"UPDATE projects SET {', '.join(updates)} WHERE id = ?",
            tuple(params),
        )
        await db.commit()
    return await get_project_settings(db, project_id)


async def get_tenant_id_for_project(
    db: aiosqlite.Connection, project_id: str
) -> str | None:
    """Return the tenant id that owns this project, or None.

    81b10dec — used by the slot-readiness endpoint so the code-intel guard
    hook can probe the Serena/code-intel tunnel slot without knowing the
    tenant id directly. Resolves via creator_human_id (project creator email)
    -> tenants.id JOIN. Returns None for self-hosted installs where the
    tenants table is absent or the project has no creator_human_id.
    """
    # Get the creator email from the project.
    async with db.execute(
        "SELECT creator_human_id FROM projects WHERE id = ?", (project_id,)
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    creator_email = (row["creator_human_id"] if isinstance(row, dict) else row[0])
    if not creator_email:
        return None
    # Look up the tenant by email (self-hosted SQLite: tenants table exists).
    # On hosted Neon DBs the tenants table lives in a separate control-plane DB
    # that this per-project db connection doesn't reach, so we tolerate the miss.
    try:
        async with db.execute(
            "SELECT id FROM tenants WHERE email = ?", (creator_email,)
        ) as cur2:
            trow = await cur2.fetchone()
    except Exception:  # noqa: BLE001 — tenants table absent (hosted per-project DB)
        return None
    if trow is None:
        return None
    return trow["id"] if isinstance(trow, dict) else trow[0]


async def get_executor_config(
    db: aiosqlite.Connection, project_id: str
) -> dict[str, Any]:
    """Return the persisted executor_config payload for a project."""
    settings = await get_project_settings(db, project_id)
    if settings is None:
        raise ValueError(f"unknown project: {project_id}")
    cfg = settings.get("executor_config") or {}
    return cfg if isinstance(cfg, dict) else {}


# ---------------------------------------------------------------------------
# 0b061f45 — Multi-account GitHub OAuth
# ---------------------------------------------------------------------------

async def add_github_connection(
    db: aiosqlite.Connection,
    tenant_id: str,
    account_login: str,
    token: str,
    scope: str | None = None,
) -> dict[str, Any]:
    """Upsert a GitHub account connection for a tenant (encrypted token)."""
    conn_id = _new_id()
    await db.execute(
        "INSERT INTO github_connections (id, tenant_id, account_login, token, scope) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(tenant_id, account_login) DO UPDATE "
        "SET token = excluded.token, scope = excluded.scope",
        (conn_id, tenant_id, account_login, encrypt_field(token), scope),
    )
    await db.commit()
    return {"tenant_id": tenant_id, "account_login": account_login, "scope": scope}


async def get_github_connections(
    db: aiosqlite.Connection, tenant_id: str
) -> list[dict[str, Any]]:
    """List all connected GitHub accounts for a tenant (tokens are not returned)."""
    async with db.execute(
        "SELECT id, account_login, scope, connected_at FROM github_connections "
        "WHERE tenant_id = ? ORDER BY connected_at",
        (tenant_id,),
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows]  # type: ignore[misc]


async def remove_github_connection(
    db: aiosqlite.Connection, tenant_id: str, account_login: str
) -> None:
    """Remove a GitHub account connection."""
    await db.execute(
        "DELETE FROM github_connections WHERE tenant_id = ? AND account_login = ?",
        (tenant_id, account_login),
    )
    await db.commit()


async def get_github_token_for_project(
    db: aiosqlite.Connection,
    tenant_id: str,
    project_id: str | None = None,
) -> tuple[str | None, str | None]:
    """Resolve the GitHub token for a project. Returns (token, account_login).

    Resolution order:
    1. Project's pinned github_account_login → that account's token.
    2. First connected account in github_connections (ordered by connected_at).
    3. Returns (None, None) — callers fall back to tenants.github_pat legacy.
    """
    if project_id:
        project = await get_project(db, project_id)
        pinned_login = (project or {}).get("github_account_login") or ""
        if pinned_login:
            async with db.execute(
                "SELECT token FROM github_connections "
                "WHERE tenant_id = ? AND account_login = ?",
                (tenant_id, pinned_login),
            ) as cur:
                row = await cur.fetchone()
            if row:
                r = _row_to_dict(row)
                return (decrypt_field(r.get("token")), pinned_login)

    async with db.execute(
        "SELECT account_login, token FROM github_connections "
        "WHERE tenant_id = ? ORDER BY connected_at LIMIT 1",
        (tenant_id,),
    ) as cur:
        row = await cur.fetchone()
    if row:
        r = _row_to_dict(row)
        return (decrypt_field(r.get("token")), r.get("account_login"))

    return (None, None)


async def set_executor_config(
    db: aiosqlite.Connection,
    project_id: str,
    executor_config: dict[str, Any],
) -> dict[str, Any]:
    """Persist and return the per-project executor configuration."""
    settings = await update_project_settings(
        db,
        project_id,
        executor_config=executor_config,
    )
    if settings is None:
        raise ValueError(f"unknown project: {project_id}")
    cfg = settings.get("executor_config") or {}
    return cfg if isinstance(cfg, dict) else {}


async def get_project_capability_manifest(
    db: aiosqlite.Connection, project_id: str
) -> dict[str, Any]:
    """Return the persisted capability manifest for a project (649e095f).

    A project with no ``project_capabilities`` row -- every project that
    predates this feature, or one that has simply never set one -- gets an
    empty profile back, never an error. Foundation-only: profile
    inheritance and live availability probing are separate, later slices.
    """
    async with db.execute(
        "SELECT manifest, manifest_version, manifest_hash, updated_at "
        "FROM project_capabilities WHERE project_id = ?",
        (project_id,),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return {
            "project_id": project_id,
            "manifest_version": _capability_manifest.MANIFEST_SCHEMA_VERSION,
            "capabilities": [],
            "manifest_hash": _capability_manifest.manifest_hash([]),
            "updated_at": None,
        }
    data = _row_to_dict(row) or {}
    try:
        capabilities = json.loads(data.get("manifest") or "[]")
    except (TypeError, ValueError):
        capabilities = []
    return {
        "project_id": project_id,
        "manifest_version": int(data.get("manifest_version") or _capability_manifest.MANIFEST_SCHEMA_VERSION),
        "capabilities": capabilities,
        "manifest_hash": data.get("manifest_hash"),
        "updated_at": data.get("updated_at"),
    }


async def set_project_capability_manifest(
    db: aiosqlite.Connection,
    project_id: str,
    capabilities: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Validate, normalize, and persist a project's capability manifest (649e095f).

    Raises ``capability_manifest.CapabilityManifestError`` on any malformed
    entry (unknown/missing fields, secret-shaped values, absolute
    machine-local paths, duplicate ids) -- callers (the MCP handler) turn
    that into an ``{error}`` dict rather than a partial write. Raises
    ``ValueError`` if the project does not exist.
    """
    project = await get_project(db, project_id)
    if project is None:
        raise ValueError(f"unknown project: {project_id}")
    normalized = _capability_manifest.normalize_manifest(capabilities)
    digest = _capability_manifest.manifest_hash(normalized)
    await db.execute(
        "INSERT INTO project_capabilities "
        "(project_id, manifest, manifest_version, manifest_hash, updated_at) "
        "VALUES (?, ?, ?, ?, datetime('now')) "
        "ON CONFLICT(project_id) DO UPDATE SET "
        "manifest = excluded.manifest, manifest_version = excluded.manifest_version, "
        "manifest_hash = excluded.manifest_hash, updated_at = excluded.updated_at",
        (
            project_id,
            json.dumps(normalized),
            _capability_manifest.MANIFEST_SCHEMA_VERSION,
            digest,
        ),
    )
    await db.commit()
    return await get_project_capability_manifest(db, project_id)


async def get_capability_profile(
    db: aiosqlite.Connection, scope_type: str, scope_id: str
) -> dict[str, Any]:
    """Return the raw, single-layer capability profile for one scope (02038afe).

    A scope with no persisted row gets an empty profile back, never an error
    — mirrors get_project_capability_manifest's "never a read error" contract.
    This is one layer only; see get_effective_capability_profile for the
    merged, multi-layer view.
    """
    scope_type = _capability_profile.normalize_scope_type(scope_type)
    scope_id = _capability_profile.normalize_scope_id(scope_id)
    async with db.execute(
        "SELECT manifest, disabled_ids, manifest_version, manifest_hash, provenance, updated_at "
        "FROM capability_profiles WHERE scope_type = ? AND scope_id = ?",
        (scope_type, scope_id),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return {
            "scope_type": scope_type,
            "scope_id": scope_id,
            "manifest_version": _capability_manifest.MANIFEST_SCHEMA_VERSION,
            "capabilities": [],
            "disabled_capability_ids": [],
            "manifest_hash": _capability_manifest.manifest_hash([]),
            "provenance": None,
            "updated_at": None,
        }
    data = _row_to_dict(row) or {}
    try:
        capabilities = json.loads(data.get("manifest") or "[]")
    except (TypeError, ValueError):
        capabilities = []
    try:
        disabled_ids = json.loads(data.get("disabled_ids") or "[]")
    except (TypeError, ValueError):
        disabled_ids = []
    raw_provenance = data.get("provenance")
    try:
        provenance = json.loads(raw_provenance) if raw_provenance else None
    except (TypeError, ValueError):
        provenance = None
    return {
        "scope_type": scope_type,
        "scope_id": scope_id,
        "manifest_version": int(data.get("manifest_version") or _capability_manifest.MANIFEST_SCHEMA_VERSION),
        "capabilities": capabilities,
        "disabled_capability_ids": disabled_ids,
        "manifest_hash": data.get("manifest_hash"),
        "provenance": provenance,
        "updated_at": data.get("updated_at"),
    }


async def set_capability_profile(
    db: aiosqlite.Connection,
    scope_type: str,
    scope_id: str,
    capabilities: list[dict[str, Any]] | None = None,
    disabled_capability_ids: list[str] | None = None,
    provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate, normalize, and persist one layer's capability profile (02038afe).

    Wholesale-replaces this scope's stored capabilities and disabled-id list
    (not a merge) — same "replace, not merge" contract as
    set_project_capability_manifest, just scoped to one layer of the
    workspace/user/project/sprint_version/item inheritance chain. Raises
    ``capability_profile.CapabilityProfileError`` (a CapabilityManifestError
    subclass) on any malformed capability entry, bad scope_type, malformed
    disabled_capability_ids, or unsafe (secret-shaped / machine-local-path)
    provenance — callers (the MCP handler) turn that into an {error} dict
    rather than a partial write.
    """
    scope_type = _capability_profile.normalize_scope_type(scope_type)
    scope_id = _capability_profile.normalize_scope_id(scope_id)
    normalized_caps = _capability_manifest.normalize_manifest(capabilities)
    normalized_disabled = _capability_profile.normalize_disabled_capability_ids(disabled_capability_ids)
    normalized_provenance = _capability_profile.normalize_provenance(provenance)
    digest = _capability_manifest.manifest_hash(normalized_caps)
    await db.execute(
        "INSERT INTO capability_profiles "
        "(scope_type, scope_id, manifest, disabled_ids, manifest_version, manifest_hash, provenance, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now')) "
        "ON CONFLICT(scope_type, scope_id) DO UPDATE SET "
        "manifest = excluded.manifest, disabled_ids = excluded.disabled_ids, "
        "manifest_version = excluded.manifest_version, manifest_hash = excluded.manifest_hash, "
        "provenance = excluded.provenance, updated_at = excluded.updated_at",
        (
            scope_type,
            scope_id,
            json.dumps(normalized_caps),
            json.dumps(normalized_disabled),
            _capability_manifest.MANIFEST_SCHEMA_VERSION,
            digest,
            json.dumps(normalized_provenance) if normalized_provenance is not None else None,
        ),
    )
    await db.commit()
    return await get_capability_profile(db, scope_type, scope_id)


async def clear_capability_profile(
    db: aiosqlite.Connection, scope_type: str, scope_id: str
) -> dict[str, Any]:
    """Delete a scope's entire capability profile row (02038afe).

    Explicit "clear" semantics distinct from "disable": clearing removes this
    layer's contribution entirely (both its capabilities and its disabled-id
    list), so the scope reverts to inheriting purely from less-specific
    layers. Idempotent — clearing an already-empty/never-set scope is a no-op.
    """
    scope_type = _capability_profile.normalize_scope_type(scope_type)
    scope_id = _capability_profile.normalize_scope_id(scope_id)
    await db.execute(
        "DELETE FROM capability_profiles WHERE scope_type = ? AND scope_id = ?",
        (scope_type, scope_id),
    )
    await db.commit()
    return await get_capability_profile(db, scope_type, scope_id)


async def get_effective_capability_profile(
    db: aiosqlite.Connection,
    project_id: str,
    sprint_item_id: str | None = None,
    *,
    workspace_scope_id: str = "singleton",
    user_scope_id: str | None = None,
) -> dict[str, Any]:
    """Resolve the merged capability profile across all applicable layers (02038afe).

    Walks workspace -> user -> project -> sprint_version -> item (least to
    most specific — see meridian.capability_profile.merge_layers), skipping
    any layer that has no applicable scope_id (e.g. no ``user_scope_id``
    given, or no ``sprint_item_id`` so there's no sprint_version/item layer).
    Read-only: never persists anything. Raises ValueError for an unknown
    project_id, or an unknown sprint_item_id / one that belongs to a
    different project.
    """
    project = await get_project(db, project_id)
    if project is None:
        raise ValueError(f"unknown project: {project_id}")

    sprint_version: str | None = None
    if sprint_item_id:
        item = await get_sprint_item(db, sprint_item_id)
        if item is None:
            raise ValueError(f"unknown sprint item: {sprint_item_id}")
        if item.get("project_id") != project_id:
            raise ValueError(
                f"sprint item {sprint_item_id} does not belong to project {project_id}"
            )
        sprint_version = item.get("version")

    layer_scopes: list[tuple[str, str | None]] = [
        ("workspace", workspace_scope_id),
        ("user", user_scope_id),
        ("project", project_id),
        ("sprint_version", f"{project_id}:{sprint_version}" if sprint_version else None),
        ("item", sprint_item_id),
    ]

    layers_for_merge: list[dict[str, Any]] = []
    layers_applied: list[str] = []
    for layer_name, scope_id in layer_scopes:
        if not scope_id:
            continue
        profile = await get_capability_profile(db, layer_name, scope_id)
        if profile["capabilities"] or profile["disabled_capability_ids"]:
            layers_applied.append(layer_name)
        layers_for_merge.append({
            "layer": layer_name,
            "capabilities": profile["capabilities"],
            "disabled_capability_ids": profile["disabled_capability_ids"],
        })

    effective, sources, overrides, disabled_log = _capability_profile.merge_layers(layers_for_merge)
    return {
        "project_id": project_id,
        "sprint_item_id": sprint_item_id,
        "sprint_version": sprint_version,
        "capabilities": effective,
        "manifest_hash": _capability_manifest.manifest_hash(effective),
        "capability_sources": sources,
        "layers_applied": layers_applied,
        "overrides": overrides,
        "disabled": disabled_log,
    }


async def get_project_ntfy_url(
    db: aiosqlite.Connection, project_id: str
) -> str | None:
    """Return the ntfy URL for a project, or None if not set."""
    async with db.execute(
        "SELECT ntfy_url FROM projects WHERE id = ?", (project_id,)
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    return row["ntfy_url"] or None


async def set_project_ntfy_url(
    db: aiosqlite.Connection, project_id: str, ntfy_url: str | None
) -> None:
    """Save (or clear) the ntfy URL for a project."""
    await db.execute(
        "UPDATE projects SET ntfy_url = ? WHERE id = ?",
        (ntfy_url or None, project_id),
    )
    await db.commit()


async def get_project_notify_email(
    db: aiosqlite.Connection, project_id: str
) -> str | None:
    """Return the notify_email for a project, or None if not set."""
    async with db.execute(
        "SELECT notify_email FROM projects WHERE id = ?", (project_id,)
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    return row["notify_email"] or None


async def set_project_notify_email(
    db: aiosqlite.Connection, project_id: str, notify_email: str | None
) -> None:
    """Save (or clear) the notify_email for a project."""
    await db.execute(
        "UPDATE projects SET notify_email = ? WHERE id = ?",
        (notify_email or None, project_id),
    )
    await db.commit()


def _is_no_such_table(exc: Exception) -> bool:
    """Return True when ``exc`` indicates the target table does not exist.

    Used by :func:`delete_project` to silently skip tables that were added in a
    later schema version so the delete remains forward/backward-compatible across
    SQLite and Postgres.  Any OTHER exception (FK violation, syntax error, …) is
    NOT swallowed — it propagates so the caller sees a real failure.

    SQLite raises ``sqlite3.OperationalError`` (exposed as
    ``aiosqlite.OperationalError``) with the text "no such table: <name>".
    Postgres raises ``psycopg.errors.UndefinedTable`` (a subclass of
    ``psycopg.ProgrammingError``).  We check Postgres via a lazy import so this
    module stays importable in SQLite-only environments without psycopg installed.
    """
    if isinstance(exc, aiosqlite.OperationalError) and "no such table" in str(exc).lower():
        return True
    try:
        import psycopg.errors as _pe  # type: ignore[import]
        if isinstance(exc, _pe.UndefinedTable):
            return True
    except ImportError:
        pass
    return False


async def delete_project(
    db: aiosqlite.Connection, project_id: "str | list[str]"
) -> None:
    """Delete one or more projects and all associated data.

    ``project_id`` accepts either a single id (``str``, the original shape —
    all existing callers are unaffected) or a list of ids to batch-delete
    many projects in one call (0e4980d4). Batching runs each table's DELETE
    once with a single ``WHERE project_id IN (...)`` across every id in the
    batch, rather than looping the whole per-table statement list once per
    project — the same number of round trips regardless of batch size.

    Raises ``ValueError`` if any tasks across the batch are currently
    ``in_progress`` so callers can surface a warning before proceeding. The
    check (and therefore the abort) applies to the whole batch: if any one
    project in the batch has in-progress tasks, nothing in the batch is
    deleted, matching the single-project all-or-nothing behavior. The
    delete is unconditional for all other data.

    Deletion order follows FK dependencies (child-before-parent).

    Session-scoped children first (must precede ``sessions`` deletion):
      file_read_claims, session_findings, session_messages,
      session_graph_snapshots, session_notes, executor_runs,
      file_locks, resource_locks, file_symbol_claims,
      file_docx_region_claims, file_patch_counters, session_activity,
      active_worktrees, hitl_requests, task_log.

    Then sessions, then remaining project-scoped children
      (goal_states, sprint_item_pointers, sprint_items,
       decisions_pinned, insights, project_notes, handoffs,
       codebase_graph_entities, sprint_version_descriptions),
    and finally the project row(s) itself.

    Each DELETE is wrapped in a narrow exception guard that silently skips
    only a "table does not exist" error (tables added in later schema versions
    may not be present on older installs).  Any OTHER exception — FK violation,
    constraint error, connection failure — is re-raised so the caller sees a
    real failure rather than a silent false-success.
    """
    project_ids = [project_id] if isinstance(project_id, str) else list(project_id)
    if not project_ids:
        return
    placeholders = ", ".join("?" for _ in project_ids)
    ids_params = tuple(project_ids)

    async with db.execute(
        f"SELECT COUNT(*) as cnt FROM task_log "
        f"WHERE project_id IN ({placeholders}) AND status = 'in_progress'",
        ids_params,
    ) as cur:
        row = await cur.fetchone()
        count = int(row["cnt"] if row else 0)
    if count:
        raise ValueError(f"{count} task(s) in_progress — complete or cancel first")

    # Ordered child-before-parent.  Tables with session_id FK must come before
    # ``sessions``; tables with sprint_item_id references before ``sprint_items``;
    # all project children before ``projects``.
    #
    # Tables without an explicit FK (session_findings, session_messages,
    # file_read_claims, session_graph_snapshots, handoffs,
    # codebase_graph_entities, sprint_item_pointers) are included here because
    # they carry project_id / session_id data that would otherwise become orphaned
    # ghost rows invisible to the rest of the system.
    #
    # ``sessions_archived`` is intentionally NOT present — it was never a real
    # table; the archived status lives in the ``sessions`` table itself.
    stmts = [
        # --- session-scoped children (delete before sessions) ---
        # file_read_claims has no explicit FK but is session-scoped (parallel primitives).
        ("DELETE FROM file_read_claims WHERE session_id IN "
         f"(SELECT id FROM sessions WHERE project_id IN ({placeholders}))", ids_params),
        # session_findings / session_messages are project-scoped but also session-linked.
        (f"DELETE FROM session_findings WHERE project_id IN ({placeholders})", ids_params),
        (f"DELETE FROM session_messages WHERE project_id IN ({placeholders})", ids_params),
        # session_graph_snapshots: project_id + session_id columns, no explicit FK.
        (f"DELETE FROM session_graph_snapshots WHERE project_id IN ({placeholders})", ids_params),
        # ON DELETE CASCADE session children — explicit for clarity and cross-backend safety.
        ("DELETE FROM session_notes WHERE session_id IN "
         f"(SELECT id FROM sessions WHERE project_id IN ({placeholders}))", ids_params),
        (f"DELETE FROM executor_runs WHERE project_id IN ({placeholders})", ids_params),
        ("DELETE FROM file_locks WHERE session_id IN "
         f"(SELECT id FROM sessions WHERE project_id IN ({placeholders}))", ids_params),
        ("DELETE FROM resource_locks WHERE session_id IN "
         f"(SELECT id FROM sessions WHERE project_id IN ({placeholders}))", ids_params),
        ("DELETE FROM file_symbol_claims WHERE session_id IN "
         f"(SELECT id FROM sessions WHERE project_id IN ({placeholders}))", ids_params),
        ("DELETE FROM file_docx_region_claims WHERE session_id IN "
         f"(SELECT id FROM sessions WHERE project_id IN ({placeholders}))", ids_params),
        ("DELETE FROM file_patch_counters WHERE session_id IN "
         f"(SELECT id FROM sessions WHERE project_id IN ({placeholders}))", ids_params),
        ("DELETE FROM session_activity WHERE session_id IN "
         f"(SELECT id FROM sessions WHERE project_id IN ({placeholders}))", ids_params),
        # active_worktrees: FK → sessions(id) + projects(id).
        (f"DELETE FROM active_worktrees WHERE project_id IN ({placeholders})", ids_params),
        # hitl_requests: FK → projects(id) ON DELETE CASCADE + sessions(id).
        (f"DELETE FROM hitl_requests WHERE project_id IN ({placeholders})", ids_params),
        # task_log: FK → sessions(id) + projects(id).
        (f"DELETE FROM task_log WHERE project_id IN ({placeholders})", ids_params),
        # --- sessions ---
        (f"DELETE FROM sessions WHERE project_id IN ({placeholders})", ids_params),
        # --- remaining project-scoped children ---
        (f"DELETE FROM goal_states WHERE project_id IN ({placeholders})", ids_params),
        # sprint_item_pointers references sprint_item_id — delete before sprint_items.
        (f"DELETE FROM sprint_item_pointers WHERE project_id IN ({placeholders})", ids_params),
        # sprint_items has a self-referential parent_id FK; deleting all rows for one
        # project at once resolves the cycle without ordering between rows.
        (f"DELETE FROM sprint_items WHERE project_id IN ({placeholders})", ids_params),
        (f"DELETE FROM decisions_pinned WHERE project_id IN ({placeholders})", ids_params),
        (f"DELETE FROM insights WHERE project_id IN ({placeholders})", ids_params),
        (f"DELETE FROM project_notes WHERE project_id IN ({placeholders})", ids_params),
        # handoffs: project_id TEXT (no explicit FK), plain delete.
        (f"DELETE FROM handoffs WHERE project_id IN ({placeholders})", ids_params),
        # codebase_graph_entities: project_id TEXT (no explicit FK).
        (f"DELETE FROM codebase_graph_entities WHERE project_id IN ({placeholders})", ids_params),
        # sprint_version_descriptions: FK → projects(id) ON DELETE CASCADE.
        (f"DELETE FROM sprint_version_descriptions WHERE project_id IN ({placeholders})", ids_params),
        # --- project row(s) itself ---
        (f"DELETE FROM projects WHERE id IN ({placeholders})", ids_params),
    ]

    for stmt, params in stmts:
        try:
            await db.execute(stmt, params)
        except Exception as exc:  # noqa: BLE001
            if _is_no_such_table(exc):
                # Table added in a later schema version — safe to skip on older installs.
                _log.debug("delete_project: skipping missing table in: %s", stmt)
            else:
                # Real error (FK violation, connection failure, …) — propagate.
                raise
    await db.commit()


def _stale_collapse_map(
    coherence_warning: dict[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    """Map ``stale_fields`` → per-field collapse metadata.

    Keyed on the goal field name (``north_star`` / ``version_goal`` /
    ``sprint``) so :func:`build_goal_xml` can look up, for each element,
    whether the coherence check flagged it stale and by how much. Values
    carry ``age_seconds`` (raw), ``age_days`` (rounded), and ``date`` (the
    ``YYYY-MM-DD`` the field was last touched, derived from now − age).
    Returns ``{}`` when nothing is flagged so the caller stays lean.
    """
    if not coherence_warning:
        return {}
    stale = coherence_warning.get("stale_fields") or []
    if not stale:
        return {}
    import time as _time
    now = _time.time()
    out: dict[str, dict[str, Any]] = {}
    for entry in stale:
        field = entry.get("field")
        if not field:
            continue
        age = entry.get("age_seconds")
        meta: dict[str, Any] = {"age_seconds": age}
        if isinstance(age, (int, float)):
            meta["age_days"] = int(age // 86400)
            from datetime import datetime as _dt, timezone as _tz
            meta["date"] = _dt.fromtimestamp(
                max(0.0, now - age), _tz.utc
            ).strftime("%Y-%m-%d")
        else:
            meta["age_days"] = None
            meta["date"] = None
        out[field] = meta
    return out


def _collapsed_field_summary(tag: str, meta: dict[str, Any]) -> str:
    """Plain-text one-line stand-in for a stale goal field.

    Shared by the XML collapse path (:func:`_collapsed_field_line`) and the
    raw-dict / cache-block collapse path (14847f20) so the wording — and the
    "pass expand_stale=true" escape hatch — stays in sync across every
    representation of the same field.
    """
    date = meta.get("date")
    age_days = meta.get("age_days")
    if date and age_days is not None:
        return (
            f"[stale {tag} from {date} ({age_days}d old) — collapsed; pass "
            "expand_stale=true or call get_session_brief for full text]"
        )
    return (
        f"[stale {tag} — collapsed; pass expand_stale=true or call "
        "get_session_brief for full text]"
    )


def _collapsed_field_line(
    tag: str, cache: str, meta: dict[str, Any], escape_fn: Any
) -> str:
    """Render a single one-line collapsed replacement for a stale field.

    Instead of the full body, emit a self-closing element that names the
    staleness and points at the explicit opt-in for the full text. The
    ``collapsed`` / ``stale`` attributes are machine-checkable; the text
    of the summary attribute is the human-readable nudge.
    """
    date = meta.get("date")
    age_days = meta.get("age_days")
    summary = _collapsed_field_summary(tag, meta)
    parts = [f'  <{tag} cache="{cache}" stale="true" collapsed="true"']
    if date:
        parts.append(f' stale_since="{escape_fn(str(date))}"')
    if age_days is not None:
        parts.append(f' age_days="{int(age_days)}"')
    parts.append(f' summary={_quoteattr_local(summary)} />')
    return "".join(parts)


def _quoteattr_local(value: str) -> str:
    from xml.sax.saxutils import quoteattr
    return quoteattr(value)


def build_goal_xml(
    goal: dict[str, Any] | None,
    project_name: str,
    recent_tasks: list[dict[str, Any]] | None = None,
    coherence_warning: dict[str, Any] | None = None,
    decisions: str | None = None,
    expand_stale: bool = True,
) -> str:
    """Serialise the goal + ambient context as XML for MCP consumers.

    Layout (v0.6.1):

        <goal version="N" project="NAME">
          <north_star cache="true">...</north_star>
          <version_goal cache="true">...</version_goal>
          <sprint cache="false">...</sprint>
          <recent_tasks cache="false">
            <task status="done" ts="...">...</task>
          </recent_tasks>
        </goal>

    ``cache="true"`` on fields that change rarely (north_star,
    version_goal) is a hint for v0.6.2's Anthropic prompt-cache
    plumbing — the field text doesn't drive any cache behaviour by
    itself but it makes the contract explicit in the wire format.
    Returns a valid XML document even when ``goal`` is None so cold
    sessions get a parseable response instead of a 404.

    ``expand_stale`` (2b4e69aa): when ``False`` and ``coherence_warning``
    has flagged a goal field (north_star / version_goal / sprint) as
    stale, that field's full body is replaced by a one-line collapsed
    summary — trimming dead week-old context from the default session
    orientation. The data is never dropped: passing ``expand_stale=True``
    (the default, so every existing caller is unchanged) or reading
    ``get_session_brief`` / the ``/goal`` endpoint returns the full text.
    Non-stale fields are always rendered in full.
    """
    from xml.sax.saxutils import escape, quoteattr

    if goal is None:
        version = 0
        north_star = version_goal = sprint = ""
    else:
        version = int(goal.get("version") or 0)
        north_star = goal.get("north_star") or ""
        content = goal.get("content")
        if isinstance(content, str):
            version_goal = content
        elif content is None:
            version_goal = ""
        else:
            version_goal = json.dumps(content, indent=2)
        sprint = goal.get("sprint") or ""

    # 2b4e69aa — which fields did the coherence check flag stale? Only
    # consult it when the caller opted out of expansion; an empty map
    # means "render everything in full" (the legacy path).
    collapse = {} if expand_stale else _stale_collapse_map(coherence_warning)

    out: list[str] = []
    out.append(
        f'<goal version="{version}" project={quoteattr(project_name)}>'
    )
    if "north_star" in collapse and north_star:
        out.append(
            _collapsed_field_line(
                "north_star", "true", collapse["north_star"], escape
            )
        )
    else:
        out.append(
            f'  <north_star cache="true">{escape(north_star)}</north_star>'
        )
    if "version_goal" in collapse and version_goal:
        out.append(
            _collapsed_field_line(
                "version_goal", "true", collapse["version_goal"], escape
            )
        )
    else:
        out.append(
            f'  <version_goal cache="true">{escape(version_goal)}</version_goal>'
        )
    if "sprint" in collapse and sprint:
        out.append(
            _collapsed_field_line(
                "sprint", "false", collapse["sprint"], escape
            )
        )
    else:
        out.append(f'  <sprint cache="false">{escape(sprint)}</sprint>')
    # v1.1.4 — append-only decisions log. Cached because it changes
    # rarely and only by explicit set_decision calls.
    if decisions is not None and decisions.strip():
        out.append(
            f'  <decisions cache="true">{escape(decisions)}</decisions>'
        )
    out.append('  <recent_tasks cache="false">')
    for t in recent_tasks or []:
        status = escape(str(t.get("status") or ""))
        ts = escape(str(t.get("created_at") or ""))
        desc = escape(str(t.get("description") or ""))
        out.append(
            f'    <task status="{status}" ts="{ts}">{desc}</task>'
        )
    out.append("  </recent_tasks>")
    # v1.1.3 — coherence_warning surfaced inline so cold sessions
    # immediately see which goal fields have gone stale.
    if coherence_warning is not None:
        level = escape(str(coherence_warning.get("level") or "ok"))
        msg = escape(str(coherence_warning.get("message") or ""))
        out.append(f'  <coherence_warning level="{level}">{msg}')
        stale = coherence_warning.get("stale_fields") or []
        for entry in stale:
            f = escape(str(entry.get("field") or ""))
            age = entry.get("age_seconds")
            age_str = f"{int(age)}" if isinstance(age, (int, float)) else ""
            out.append(
                f'    <stale field="{f}" age_seconds="{age_str}" />'
            )
        out.append('  </coherence_warning>')
    out.append("</goal>")
    return "\n".join(out)


def collapse_stale_goal_fields(
    goal: dict[str, Any] | None,
    coherence_warning: dict[str, Any] | None,
    expand_stale: bool = True,
) -> dict[str, Any] | None:
    """Trim coherence-flagged-stale text out of the raw ``goal`` dict.

    2b4e69aa taught :func:`build_goal_xml` to collapse a stale north_star /
    version_goal / sprint field to a one-line summary; the raw ``goal`` dict
    handed back alongside ``goal_xml`` (e.g. by ``start_session``'s full
    orientation) never got the same treatment — so a week-old field was
    STILL dumped in full even though ``coherence_warning`` already flagged
    it stale, and duplicated across ``goal``, ``goal["xml"]`` and
    ``goal["cache_blocks"]``. That duplication of stale content was the
    dominant contributor to a 269KB default ``start_session`` payload
    (14847f20).

    Mirrors ``build_goal_xml``'s default: ``expand_stale=True`` (the
    default) returns ``goal`` unchanged so every existing caller keeps its
    current behaviour byte-for-byte. Pass ``expand_stale=False`` to collapse.
    Returns a shallow copy — the input dict (and anything already derived
    from its FULL text, like ``goal["xml"]`` / ``goal["cache_blocks"]``
    built before this runs) is never mutated.
    """
    if goal is None or expand_stale:
        return goal
    collapse = _stale_collapse_map(coherence_warning)
    if not collapse:
        return goal
    trimmed = dict(goal)
    if "north_star" in collapse and trimmed.get("north_star"):
        trimmed["north_star"] = _collapsed_field_summary(
            "north_star", collapse["north_star"]
        )
    if "version_goal" in collapse and trimmed.get("content"):
        trimmed["content"] = _collapsed_field_summary(
            "version_goal", collapse["version_goal"]
        )
    if "sprint" in collapse and trimmed.get("sprint"):
        trimmed["sprint"] = _collapsed_field_summary("sprint", collapse["sprint"])
    return trimmed


def build_goal_cache_blocks(
    goal: dict[str, Any] | None,
    project_name: str,
    recent_tasks: list[dict[str, Any]] | None = None,
    coherence_warning: dict[str, Any] | None = None,
    expand_stale: bool = True,
) -> list[dict[str, Any]]:
    """Return goal text as Anthropic-API content blocks with cache hints.

    Layout (v0.6.2): four ordered text blocks ready to splat into
    ``messages[0].content`` or ``system`` on an Anthropic request.

    ``cache_control: {"type": "ephemeral"}`` is attached to the two
    blocks that change rarely:

      1. north_star  — cached
      2. version_goal — cached
      3. sprint      — no cache marker (moves every sprint review)
      4. recent_tasks — no cache marker (moves every task)

    Putting the cached blocks first matters: Anthropic's cache key is
    a prefix of the full prompt, so a hit requires the cached blocks
    to lead. Anything mutable that appears before a cached block
    invalidates the cache for every cold session.

    ``expand_stale`` (14847f20, mirroring :func:`build_goal_xml`'s 2b4e69aa
    flag): when ``False`` and ``coherence_warning`` flagged a field stale,
    that block's body is replaced with the same one-line collapsed summary
    used by the XML path, instead of the full text. Defaults to ``True`` so
    every existing caller is unchanged.
    """
    collapse = {} if expand_stale else _stale_collapse_map(coherence_warning)
    if goal is None:
        north_star = version_goal = sprint = ""
        version = 0
    else:
        version = int(goal.get("version") or 0)
        north_star = goal.get("north_star") or ""
        content = goal.get("content")
        if isinstance(content, str):
            version_goal = content
        elif content is None:
            version_goal = ""
        else:
            version_goal = json.dumps(content, indent=2)
        sprint = goal.get("sprint") or ""

    if "north_star" in collapse and north_star:
        north_star = _collapsed_field_summary("north_star", collapse["north_star"])
    if "version_goal" in collapse and version_goal:
        version_goal = _collapsed_field_summary(
            "version_goal", collapse["version_goal"]
        )
    if "sprint" in collapse and sprint:
        sprint = _collapsed_field_summary("sprint", collapse["sprint"])

    header = (
        f"# Meridian goal — project: {project_name} (v{version})\n\n"
    )
    blocks: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": f"{header}## North star\n{north_star}".rstrip(),
            "cache_control": {"type": "ephemeral"},
        },
        {
            "type": "text",
            "text": f"## Version goal\n{version_goal}".rstrip(),
            "cache_control": {"type": "ephemeral"},
        },
        {
            "type": "text",
            "text": f"## Sprint\n{sprint}".rstrip(),
        },
    ]
    if recent_tasks:
        task_lines = ["## Recent tasks (newest first)"]
        for t in recent_tasks:
            status = (t.get("status") or "").upper()
            ts = t.get("created_at") or ""
            desc = (t.get("description") or "").replace("\n", " ")
            task_lines.append(f"- [{status}] {ts} — {desc}")
        blocks.append({"type": "text", "text": "\n".join(task_lines)})
    else:
        blocks.append(
            {"type": "text", "text": "## Recent tasks\n(no activity yet)"}
        )
    return blocks


async def get_goal(
    db: aiosqlite.Connection, project_id: str
) -> dict[str, Any] | None:
    """Return the latest goal state for a project, or None if unset.

    Since v0.5.2 the returned dict also includes ``north_star`` and
    ``sprint`` pulled from the ``goal_north_star`` / ``goal_sprint``
    columns. Both are None when not yet set.

    3b6ff466 — subproject north_star inheritance. When the project is a
    subproject (``parent_project_id`` set) and has NO north_star of its own
    (empty/NULL), the parent's north_star is inherited. The returned dict then
    carries ``north_star_inherited=True`` and ``north_star_source_project_id``
    (the parent's id) so callers can render "inherited" state. Top-level
    projects and subprojects with their own north_star are unaffected. This is
    the single fall-back point every read-path shares — ``get_goal``,
    ``get_planning_brief`` and ``get_context_block`` all resolve the goal
    through here, so the inheritance is wired into all three at once.
    """
    async with db.execute(
        "SELECT * FROM goal_states WHERE project_id = ? "
        "ORDER BY version DESC LIMIT 1",
        (project_id,),
    ) as cur:
        row = await cur.fetchone()
    goal = _row_to_dict(row)
    if goal is not None:
        goal["content"] = _decode_content(goal["content"])
        goal["north_star"] = goal.pop("goal_north_star", None)
        goal["sprint"] = goal.pop("goal_sprint", None)
        goal["north_star_inherited"] = False
        goal["north_star_source_project_id"] = None
        # Own north_star present → nothing to inherit; return as-is.
        if (goal.get("north_star") or "").strip():
            return goal
    # No goal row, or a goal row with an empty north_star: try the parent.
    inherited = await _inherited_north_star(db, project_id)
    if inherited is None:
        return goal
    parent_ns, parent_id = inherited
    if goal is None:
        # Child has no goal state at all — synthesise a minimal one so the
        # inherited north_star still surfaces (content/sprint stay empty).
        return {
            "project_id": project_id,
            "content": "",
            "version": 0,
            "north_star": parent_ns,
            "sprint": None,
            "north_star_inherited": True,
            "north_star_source_project_id": parent_id,
        }
    goal["north_star"] = parent_ns
    goal["north_star_inherited"] = True
    goal["north_star_source_project_id"] = parent_id
    return goal


async def _inherited_north_star(
    db: aiosqlite.Connection, project_id: str
) -> tuple[str, str] | None:
    """3b6ff466 — resolve a subproject's inherited north_star from its parent.

    Returns ``(parent_north_star, parent_project_id)`` when ``project_id`` is a
    subproject whose parent has a non-empty north_star, else None. One level
    deep by construction (create_project rejects grandchildren), so no
    recursion / cycle handling is needed. Best-effort: a missing
    ``parent_project_id`` column (pre-migration DB) simply yields no inheritance.
    """
    try:
        proj = await get_project(db, project_id)
    except Exception:  # noqa: BLE001 — pre-migration schema → no inheritance
        return None
    if proj is None:
        return None
    parent_id = proj.get("parent_project_id")
    if not parent_id:
        return None
    async with db.execute(
        "SELECT goal_north_star FROM goal_states WHERE project_id = ? "
        "ORDER BY version DESC LIMIT 1",
        (parent_id,),
    ) as cur:
        prow = await cur.fetchone()
    if prow is None:
        return None
    parent_ns = (prow["goal_north_star"] if isinstance(prow, dict) else prow[0]) or ""
    if not parent_ns.strip():
        return None
    return parent_ns, parent_id


async def set_goal(
    db: aiosqlite.Connection,
    project_id: str,
    content: Any,
    north_star: str | None = None,
    sprint: str | None = None,
    minor: bool = False,
) -> dict[str, Any]:
    """Upsert the goal state for a project.

    ``north_star`` and ``sprint`` are optional. When omitted, the values
    from the previous goal row are carried forward (backward compat). Pass
    an explicit value to change them. Since v0.5.2.

    When ``minor=True``, the latest row is updated in-place without
    incrementing the version counter. Use this for AUTO BLOCKS appends
    that should not pollute the goal history.
    """
    existing = await get_goal(db, project_id)
    # 3b6ff466 — a subproject with no goal row of its own gets a *synthesised*
    # goal dict back from get_goal (parent's inherited north_star, no real
    # goal_states row → no "id"). That is NOT a row to update or version off
    # of; treat it as "no existing goal" for all row surgery below, otherwise
    # the in-place UPDATE (WHERE id = existing["id"]) would KeyError and the
    # inherited north_star would be materialised into the child's first row.
    if existing is not None and "id" not in existing:
        existing = None
    encoded = _encode_content(content)
    # Never carry forward an INHERITED north_star as if it were the child's own.
    # get_goal flags a borrowed parent north_star with north_star_inherited;
    # treat that as "the child has no own north_star" for carry-forward.
    _own_existing_ns = (
        existing.get("north_star")
        if (existing and not existing.get("north_star_inherited"))
        else None
    )
    # Carry forward north_star / sprint from the previous row when not given.
    final_north_star = north_star if north_star is not None else _own_existing_ns
    final_sprint = sprint if sprint is not None else (
        existing.get("sprint") if existing else None
    )
    # v2.3 — dedup: if the main content didn't change, treat this as a
    # minor in-place update on the existing row. Prevents the version
    # counter from spamming when only sprint / north_star changes (every
    # save would otherwise insert a new row + bump version).
    if not minor and existing is not None:
        existing_content = existing.get("content")
        existing_encoded = (
            existing_content if isinstance(existing_content, str)
            else _encode_content(existing_content)
        )
        if encoded == existing_encoded:
            minor = True
        else:
            # If ONLY the AUTO BLOCKS section changed (human prefix identical),
            # treat as minor — auto-summary updates must never bump version.
            _AUTO_SPLIT = "--- AUTO BLOCKS BELOW ---"
            def _strip_auto(s: Any) -> str:
                t = s if isinstance(s, str) else (s.get("content", "") if isinstance(s, dict) else str(s or ""))
                return t.split(_AUTO_SPLIT)[0].rstrip() if _AUTO_SPLIT in t else t.rstrip()
            if _strip_auto(encoded) == _strip_auto(existing_encoded):
                minor = True
    if minor and existing is not None:
        # In-place update — no version bump, no new row.
        # Strict AUTO BLOCKS replace: strip ALL occurrences from both sides,
        # take the new AUTO BLOCKS section from incoming only, then reconstruct.
        from datetime import datetime, timezone
        AUTO_SPLIT = "--- AUTO BLOCKS BELOW ---"

        def _to_plain(v: Any) -> str:
            """Decode goal content to a plain string for marker surgery."""
            if isinstance(v, str):
                return v
            if isinstance(v, dict):
                return v.get("content", "") or str(v)
            decoded = _decode_content(v)
            if isinstance(decoded, dict):
                return decoded.get("content", "") or ""
            return str(decoded) if decoded else ""

        existing_str = _to_plain(existing.get("content") or "")
        incoming_str = _to_plain(encoded)

        # Strip ALL occurrences of AUTO_SPLIT-and-below from existing → base.
        base = existing_str.split(AUTO_SPLIT)[0].rstrip()

        # Extract new AUTO BLOCKS from incoming (split on first occurrence).
        incoming_parts = incoming_str.split(AUTO_SPLIT, 1)
        if len(incoming_parts) > 1:
            new_auto = AUTO_SPLIT + incoming_parts[1]
            # v2.3 — use `\n\n` so the resulting layout matches
            # _AUTO_SECTION_MARKER ("\n\n--- AUTO BLOCKS BELOW ---\n").
            # Single `\n` here would make append_auto_summary's marker
            # check miss on the next cycle, producing duplicate auto
            # sections instead of replacing the old one.
            final_content = base + "\n\n" + new_auto
        else:
            # No AUTO BLOCKS in incoming — use incoming content as-is (minor
            # update that happens to contain no auto section is fine).
            final_content = incoming_str

        final_encoded = _encode_content(final_content)
        now_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        # v2.3 — per-field timestamps: bump only the columns whose value
        # actually changed. Lets get_goal_field_ages report accurate
        # per-field freshness even when in-place UPDATEs replace what
        # used to be multi-row history.
        existing_content_str = existing.get("content")
        if not isinstance(existing_content_str, str):
            existing_content_str = _encode_content(existing_content_str)
        ns_ts = now_ts if final_north_star != existing.get("north_star") else (
            existing.get("ns_updated_at") or existing.get("updated_at")
        )
        content_ts = now_ts if final_encoded != existing_content_str else (
            existing.get("content_updated_at") or existing.get("updated_at")
        )
        sprint_ts = now_ts if final_sprint != existing.get("sprint") else (
            existing.get("sprint_updated_at") or existing.get("updated_at")
        )
        await db.execute(
            "UPDATE goal_states SET content = ?, goal_north_star = ?, goal_sprint = ?, "
            "updated_at = ?, ns_updated_at = ?, content_updated_at = ?, sprint_updated_at = ? "
            "WHERE id = ?",
            (final_encoded, final_north_star, final_sprint, now_ts,
             ns_ts, content_ts, sprint_ts, existing["id"]),
        )
        await db.commit()
    else:
        from datetime import datetime, timezone
        new_version = 1 if existing is None else int(existing["version"]) + 1
        gid = _new_id()
        now_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        # v2.3 — per-field timestamps. For a brand-new row, every field
        # that has a value is timestamped now; null fields stay null. For
        # a fresh INSERT-style write where content changed, content_ts is
        # always now (content is non-null by schema).
        if existing is not None:
            existing_content_str = existing.get("content")
            if not isinstance(existing_content_str, str):
                existing_content_str = _encode_content(existing_content_str)
            ns_ts = now_ts if final_north_star != existing.get("north_star") else (
                existing.get("ns_updated_at") or existing.get("updated_at")
            )
            content_ts = now_ts if encoded != existing_content_str else (
                existing.get("content_updated_at") or existing.get("updated_at")
            )
            sprint_ts = now_ts if final_sprint != existing.get("sprint") else (
                existing.get("sprint_updated_at") or existing.get("updated_at")
            )
        else:
            ns_ts = now_ts if final_north_star else None
            content_ts = now_ts
            sprint_ts = now_ts if final_sprint else None
        await db.execute(
            "INSERT INTO goal_states "
            "(id, project_id, content, version, goal_north_star, goal_sprint, "
            "ns_updated_at, content_updated_at, sprint_updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (gid, project_id, encoded, new_version, final_north_star, final_sprint,
             ns_ts, content_ts, sprint_ts),
        )
        await db.commit()
    goal = await get_goal(db, project_id)
    assert goal is not None
    # Broadcast to dashboard WebSocket subscribers so the goal panel refreshes live.
    _publish_project_event(project_id, "goal_updated", {"version": goal.get("version")})
    return goal


async def set_north_star(
    db: aiosqlite.Connection, project_id: str, north_star: str
) -> dict[str, Any]:
    """Update only the north_star field, preserving current content and sprint.

    Creates a new goal row (increments version). 404-equivalent: raises
    ValueError if no goal exists yet — set the version goal first.
    """
    existing = await get_goal(db, project_id)
    if existing is None:
        raise ValueError("no goal set — call set_goal before set_north_star")
    return await set_goal(
        db, project_id, existing["content"],
        north_star=north_star, sprint=existing.get("sprint")
    )


async def set_sprint(
    db: aiosqlite.Connection, project_id: str, sprint: str
) -> dict[str, Any]:
    """Update only the sprint field, preserving current content and north_star.

    Any team member can call this (no ownership check at the db layer).
    Creates a new goal row (increments version).
    """
    existing = await get_goal(db, project_id)
    if existing is None:
        raise ValueError("no goal set — call set_goal before set_sprint")
    return await set_goal(
        db, project_id, existing["content"],
        north_star=existing.get("north_star"), sprint=sprint
    )


async def register_session(
    db: aiosqlite.Connection,
    project_id: str,
    name: str,
    human_id: str | None = None,
    session_type: str = "human",
    agent_framework: str = "claude_code",
    client_type: str | None = None,
    sprint_version: str | None = None,
) -> dict[str, Any]:
    """Create a session row in 'active' state.

    ``human_id`` lets a session attach a human owner identifier so the
    dashboard can group ``adam/claude-sonnet-xyz`` sessions together and
    so the goal-ownership rule can match a writer to the project creator.

    ``session_type`` (v1.2.0) is ``human`` by default and ``worker`` for
    sessions started via :func:`start_worker_session`. The timeline /
    auto-summary loops use it to distinguish the two kinds.

    ``agent_framework`` (v2.4) labels the originating framework so the
    Team tab can render badges. One of: claude_code (default), cursor,
    windsurf, langgraph, autogen, openviking, custom. Free-form string —
    unknown values render as 'custom' in the UI.

    ``client_type`` (v2.6) identifies the client app: claude-code, claude-desktop,
    cursor, or other. Optional — used for presence indicators in the dashboard.

    ``sprint_version`` (a76cb7c0) scopes the session to one sprint-version
    bucket. start_session passes the explicit or inferred version so later calls
    can auto-filter sprint progress/items to it. NULL = unscoped (all versions),
    the legacy behaviour.
    """
    if session_type not in {"human", "worker"}:
        raise ValueError(f"invalid session_type: {session_type!r}")
    sid = _new_id()
    await db.execute(
        "INSERT INTO sessions (id, project_id, name, human_id, session_type, agent_framework, client_type, sprint_version) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (sid, project_id, name, human_id, session_type, agent_framework, client_type, sprint_version),
    )
    await db.commit()
    async with db.execute(
        "SELECT * FROM sessions WHERE id = ?", (sid,)
    ) as cur:
        row = await cur.fetchone()
    session = _row_to_dict(row)
    assert session is not None
    # Broadcast to dashboard WebSocket subscribers so the session list refreshes live.
    _publish_project_event(project_id, "session_started", {
        "session_id": sid, "session_name": name, "human_id": human_id,
    })
    return session


async def generate_default_session_name(
    db: aiosqlite.Connection,
    project_id: str,
) -> str:
    """599d0097 — derive a meaningful session name when start_session gets none.

    Uses the first pending/todo sprint item's title (slugified to its first few
    words) plus a short UTC timestamp, so an unnamed session reads as e.g.
    ``wire-billing-oauth-0701-2130`` instead of forcing the caller to invent a
    string. 2bce89ed — when the board is empty, falls back to a memorable
    adjective+noun+timestamp slug (e.g. ``brisk-otter-0701-213045``) instead of
    the anonymous ``session-<ts>``. The timestamp keeps the name unique (the
    board rejects duplicate active names); year is dropped for brevity but
    seconds are kept so two sessions in the same minute don't collide.
    """
    from datetime import datetime, timezone  # local: db/__init__ has no top import
    ts = datetime.now(timezone.utc).strftime("%m%d-%H%M%S")
    try:
        items = await get_sprint_items(db, project_id, include_human=False)
    except Exception:  # noqa: BLE001 — naming must never block start_session
        items = []
    first = next(
        (it for it in items
         if (it.get("status") or "pending") in {"pending", "todo"}),
        None,
    )
    title = (first or {}).get("title") or ""
    words = re.findall(r"[a-z0-9]+", title.lower())
    if words:
        slug = "-".join(words[:5])[:48].strip("-") or "session"
        return f"{slug}-{ts}"
    # 2bce89ed — memorable adjective+noun for an empty board, chosen
    # deterministically from the timestamp (readable + unique via the ts suffix).
    _adj = ("brisk", "calm", "clever", "bold", "quiet", "swift", "warm", "keen",
            "bright", "steady", "nimble", "lucid")
    _noun = ("otter", "harbor", "cedar", "falcon", "meadow", "ember", "delta",
             "willow", "quartz", "sparrow", "atlas", "cove")
    _h = sum(ord(c) for c in ts)
    return f"{_adj[_h % len(_adj)]}-{_noun[(_h // len(_adj)) % len(_noun)]}-{ts}"


# --- worker_context XML prompt-budget contract (bfb18ea2) -----------------
#
# WORKER_CONTEXT_XML_BUDGET_CHARS (contract version
# WORKER_CONTEXT_XML_BUDGET_VERSION): build_worker_context_xml()'s return
# value is GUARANTEED to never exceed this many CHARACTERS. This block is
# spliced into every worker's first prompt, so the limit is a deliberate
# context/latency budget decision — not an artifact of an XML parser or an
# MCP protocol limit.
#
# Unit chosen: characters, not tokens. This codebase has no tokenizer
# dependency anywhere (no tiktoken / token-count utility exists here), so a
# token budget would mean adding a new dependency for one call site. A
# character count is deterministic, dependency-free, and cheap to enforce
# on every call. As a rough sanity cross-check only (not an exact
# conversion): ~4 chars/token is typical for English prose, so 700 chars is
# roughly ~175 tokens — comfortably inside the "~500 tokens" this function
# has described in prose since v1.2.0. That "~500 tokens" language was
# never actually checked against a tokenizer; the only enforced number was
# always this char count, via the test. This constant makes that the
# explicit, single source of truth instead of a bare literal (`700`) in
# the test with no named contract behind it.
#
# Bump WORKER_CONTEXT_XML_BUDGET_VERSION whenever the number or the unit
# changes, so callers/tests can tell "the contract changed on purpose"
# apart from "someone edited a bare literal".
WORKER_CONTEXT_XML_BUDGET_VERSION = 1
WORKER_CONTEXT_XML_BUDGET_CHARS = 700

# Visible marker appended when a field had to be compacted to fit the
# budget above. Never cut silently — a worker (or a test) can always tell
# truncation happened by looking for this marker.
_WORKER_CONTEXT_TRUNCATION_MARKER = " …[truncated]"


def _worker_context_bounded_field(raw: str, budget_chars: int) -> str:
    """XML-escape ``raw``, bounded to at most ``budget_chars`` characters.

    If the escaped text already fits, it is returned unchanged — this is
    the common case and behaves exactly like a plain ``escape(raw)`` call.

    If it doesn't fit, the RAW text (never the escaped text) is cut via
    binary search to the largest prefix whose ESCAPED form still fits —
    this is what guarantees an XML entity like ``&amp;`` is never split
    in half, since we always escape-then-measure instead of slicing
    already-escaped text. The cut is then backed off to the nearest
    preceding whitespace boundary so a word is never chopped in the
    middle either, and an explicit ``"...[truncated]"`` marker is
    appended so truncation is always visible. The result is always
    <= ``budget_chars`` (best-effort on the marker only: if
    ``budget_chars`` is too small to fit the marker at all — not
    expected for this contract's normal fields, since sub-budgets are
    hundreds of characters — the marker is dropped, but the entity- and
    word-boundary safety above still holds).
    """
    from xml.sax.saxutils import escape

    escaped = escape(raw)
    if len(escaped) <= budget_chars:
        return escaped

    if budget_chars <= 0:
        return ""

    marker = _WORKER_CONTEXT_TRUNCATION_MARKER
    use_marker = budget_chars > len(marker)
    room = (budget_chars - len(marker)) if use_marker else budget_chars

    # Binary-search the largest raw-text prefix whose ESCAPED form fits
    # in `room`. Escaping can expand length ("&" -> "&amp;"), so we can't
    # just slice the escaped string directly without risking a split
    # entity — this holds regardless of whether a marker is being added,
    # which is what the earlier, buggy degenerate-budget fallback got
    # wrong (it sliced an already-escaped string directly).
    lo, hi = 0, len(raw)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if len(escape(raw[:mid])) <= room:
            lo = mid
        else:
            hi = mid - 1
    cut = lo

    # Back off to the nearest preceding whitespace so a word is never
    # split in half. If there's no whitespace to back off to (one huge
    # "word"), keep the exact character-accurate cut instead. Only
    # meaningful when we're about to append a marker — without one,
    # keep the exact binary-search cut to use the full budget.
    if use_marker and 0 < cut < len(raw) and not raw[cut].isspace():
        ws = raw.rfind(" ", 0, cut)
        if ws > 0:
            cut = ws

    prefix = raw[:cut].rstrip() if use_marker else raw[:cut]
    result = escape(prefix)
    return (result + marker) if use_marker else result


def build_worker_context_xml(
    *,
    version_goal: str,
    task_id: str,
    task_description: str,
    repo: str,
    test_cmd: str = "pixi run test",
    commit_pattern: str = (
        "Write commit message to tmp/commit.py, run via pixi run "
        "python tmp/commit.py, then delete it in the same command. "
        "Include GOAL.md in the staged files if it was modified "
        "(git-tracked)."
    ),
    done_when: str = (
        "log_task done, tests green, committed (no stray .py at repo root)."
    ),
) -> str:
    """v1.3.0 — slim XML for worker sessions, under an explicit char budget.

    Workers don't need north_star, decisions, or sprint history —
    they need the version goal + the one task they're claiming, plus
    the operational machinery (repo path, test cmd, commit pattern,
    completion criteria).

    Prompt-budget contract: the returned string never exceeds
    ``WORKER_CONTEXT_XML_BUDGET_CHARS`` characters — see that constant's
    comment above for the unit/number rationale and version. When the
    full, escaped ``version_goal``/``task_description`` wouldn't fit,
    they (and only they — the operational fields repo/test_cmd/
    commit_pattern/done_when are never truncated) are compacted at a
    whole-word boundary with a visible ``"...[truncated]"`` marker rather
    than being cut silently or mid-word.
    """
    from xml.sax.saxutils import escape, quoteattr

    def _assemble(goal_xml: str, task_xml: str) -> str:
        return "\n".join([
            "<worker_context>",
            f"  <version_goal>{goal_xml}</version_goal>",
            f"  <task id={quoteattr(task_id)}>{task_xml}</task>",
            f"  <repo>{escape(repo)}</repo>",
            f"  <test_cmd>{escape(test_cmd)}</test_cmd>",
            f"  <commit_pattern>{escape(commit_pattern)}</commit_pattern>",
            f"  <done_when>{escape(done_when)}</done_when>",
            "</worker_context>",
        ])

    goal_full = escape(version_goal)
    task_full = escape(task_description)
    candidate = _assemble(goal_full, task_full)
    if len(candidate) <= WORKER_CONTEXT_XML_BUDGET_CHARS:
        return candidate

    # Over budget: the fixed overhead (wrapper tags + repo/test_cmd/
    # commit_pattern/done_when) is exactly whatever's left once the two
    # dynamic fields are subtracted back out of the full candidate —
    # exact regardless of the "\n".join formatting above.
    fixed_len = len(candidate) - len(goal_full) - len(task_full)
    dynamic_budget = max(0, WORKER_CONTEXT_XML_BUDGET_CHARS - fixed_len)

    total_raw = len(version_goal) + len(task_description)
    if total_raw == 0:
        goal_budget = task_budget = dynamic_budget // 2
    else:
        goal_budget = int(dynamic_budget * (len(version_goal) / total_raw))
        task_budget = dynamic_budget - goal_budget

    goal_bounded = _worker_context_bounded_field(version_goal, goal_budget)
    task_bounded = _worker_context_bounded_field(task_description, task_budget)
    return _assemble(goal_bounded, task_bounded)


async def start_worker_session(
    db: aiosqlite.Connection,
    project_id: str,
    task_id: str | None = None,
    repo: str | None = None,
    test_cmd: str | None = None,
) -> dict[str, Any]:
    """v1.2.0 — register a worker session + claim its task in one call.

    When ``task_id`` is None we pick the oldest unclaimed pending task
    for this project. The returned dict carries the new worker session
    id, the claimed task row, and the ``worker_context`` XML the
    worker should splat into its first prompt.

    Raises ``ValueError`` when no claimable task is available (and
    none was provided explicitly).
    """
    if task_id is None:
        claimable = await get_claimable_tasks(db, project_id, limit=1)
        if not claimable:
            raise ValueError("no claimable tasks available for this project")
        task = claimable[0]
    else:
        task = await get_task(db, task_id)
        if task is None or task["project_id"] != project_id:
            raise ValueError(f"task not found: {task_id}")

    short = task["id"][:8]
    session = await register_session(
        db, project_id, f"worker/{short}",
        human_id=None, session_type="worker",
    )

    claimed = await claim_task(db, task["id"], session["id"])
    if claimed is None:
        # Another worker beat us to it. Mark this session closed so
        # the timeline doesn't show a zombie row, and surface the
        # contention to the caller.
        await close_session(db, session["id"])
        raise ValueError(
            f"task {task['id']} already claimed by another worker"
        )

    goal = await get_goal(db, project_id)
    version_goal_text = ""
    if goal is not None:
        content = goal.get("content")
        if isinstance(content, str):
            version_goal_text = content
        elif content is not None:
            version_goal_text = str(content)

    xml = build_worker_context_xml(
        version_goal=version_goal_text,
        task_id=claimed["id"],
        task_description=claimed["description"] or "",
        repo=repo or os.environ.get(
            "MERIDIAN_WORKER_REPO",
            str(Path(__file__).resolve().parent.parent),
        ),
        test_cmd=test_cmd or os.environ.get(
            "MERIDIAN_WORKER_TEST_CMD", "pixi run test"
        ),
    )
    return {
        "session_id": session["id"],
        "task": claimed,
        "worker_context": xml,
    }


async def set_goal_mode(
    db: aiosqlite.Connection, project_id: str, mode: str
) -> None:
    """Switch a project between 'manual' and 'auto' goal modes (v0.4.2).

    Auto mode lets a background task append [AUTO SUMMARY] blocks to
    the goal every ten minutes so cold sessions read recent activity
    inline with the human directive.
    """
    if mode not in {"manual", "auto"}:
        raise ValueError(f"invalid goal mode: {mode!r}")
    await db.execute(
        "UPDATE projects SET goal_mode = ? WHERE id = ?", (mode, project_id)
    )
    await db.commit()


async def get_goal_mode(
    db: aiosqlite.Connection, project_id: str
) -> str:
    """Return 'manual' or 'auto' for a project (defaults to 'manual')."""
    async with db.execute(
        "SELECT goal_mode FROM projects WHERE id = ?", (project_id,)
    ) as cur:
        row = await cur.fetchone()
    if row is None or row["goal_mode"] is None:
        return "manual"
    return row["goal_mode"]


async def list_auto_mode_projects(
    db: aiosqlite.Connection,
) -> list[dict[str, Any]]:
    """Every project currently in auto-summary mode."""
    async with db.execute(
        "SELECT * FROM projects WHERE goal_mode = 'auto'"
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows]  # type: ignore[misc]


def format_auto_summary_block(
    tasks: list[dict[str, Any]], timestamp: str | None = None
) -> str:
    """Render a ``[AUTO SUMMARY - <ts>]`` block from recent tasks.

    Pure function so the periodic worker is trivial to unit-test. The
    summary is plain text: one line per task with status + description.
    """
    if timestamp is None:
        from datetime import datetime, timezone
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    if not tasks:
        return f"[AUTO SUMMARY - {timestamp}]\n(no recent activity)"
    lines = [f"[AUTO SUMMARY - {timestamp}]"]
    for t in tasks:
        status = t.get("status", "?")
        desc = (t.get("description") or "").strip().splitlines()[0][:200]
        lines.append(f"- [{status.upper()}] {desc}")
    return "\n".join(lines)


# Anchor that separates the human-written goal text from auto-appended
# blocks. Anything BELOW this marker may be rewritten by the periodic
# task; anything above is sacred.
_AUTO_SECTION_MARKER = "\n\n--- AUTO BLOCKS BELOW ---\n"


async def run_auto_summary_cycle(
    db: aiosqlite.Connection, task_limit: int = 10
) -> int:
    """Run one pass of the v0.4.2 auto-summary loop.

    For every project in ``auto`` mode: take the last ``task_limit``
    tasks, render an [AUTO SUMMARY] block, and append it to the goal.
    Returns the number of projects updated. Exposed as a standalone
    function so the background task is trivial *and* unit-testable.
    """
    updated = 0
    projects = await list_auto_mode_projects(db)
    for project in projects:
        tasks = await get_tasks(db, project["id"], limit=task_limit)
        block = format_auto_summary_block(tasks)
        result = await append_auto_summary(db, project["id"], block)
        if result is not None:
            updated += 1
    return updated


async def append_auto_summary(
    db: aiosqlite.Connection,
    project_id: str,
    summary_block: str,
) -> dict[str, Any] | None:
    """Append a fresh ``[AUTO SUMMARY ...]`` block to the project goal.

    Strategy: preserve the human-written prefix above
    ``--- AUTO BLOCKS BELOW ---`` exactly, then replace the auto
    section with just the new block (single, freshest summary — old
    blocks are discarded to keep the goal compact). Returns the new
    goal row, or None when there's no goal yet.
    """
    existing = await get_goal(db, project_id)
    if existing is None:
        return None
    content = existing["content"]
    if not isinstance(content, str):
        # JSON-typed goals are out of scope for auto-append; bail safely.
        return existing
    if _AUTO_SECTION_MARKER in content:
        prefix = content.split(_AUTO_SECTION_MARKER, 1)[0]
    else:
        prefix = content
    new_content = prefix.rstrip() + _AUTO_SECTION_MARKER + summary_block
    # v2.3 — auto-summary must NOT bump the version goal. minor=True does
    # an in-place UPDATE of the latest row, preserving the human prefix
    # above AUTO BLOCKS exactly and only swapping the auto section. The
    # main "version goal" content stays under explicit save-goal control.
    return await set_goal(db, project_id, new_content, minor=True)


async def get_decisions(
    db: aiosqlite.Connection, project_id: str
) -> str | None:
    """v1.1.4 — return the project's append-only decisions log,
    newest first. ``None`` when no decisions have been recorded."""
    async with db.execute(
        "SELECT decisions FROM projects WHERE id = ?", (project_id,)
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    return row["decisions"]


async def set_decision(
    db: aiosqlite.Connection,
    project_id: str,
    text: str,
    timestamp: str | None = None,
) -> str:
    """Prepend a decision entry to the project's decisions log.

    Format per entry: ``[YYYY-MM-DD] <text>\\n\\n``. The entry is
    prepended so the latest decision sits at the top of the log —
    cold sessions read it first. Returns the full updated log.
    """
    from datetime import datetime, timezone
    ts = timestamp or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    entry = f"[{ts}] {text.strip()}\n"
    async with db.execute(
        "SELECT decisions FROM projects WHERE id = ?", (project_id,)
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        raise ValueError(f"unknown project: {project_id}")
    existing = (row["decisions"] or "").rstrip()
    updated = (entry + ("\n" + existing if existing else "")).rstrip() + "\n"
    await db.execute(
        "UPDATE projects SET decisions = ? WHERE id = ?",
        (updated, project_id),
    )
    await db.commit()
    return updated


async def get_project_owner(
    db: aiosqlite.Connection, project_id: str
) -> str | None:
    """Return the ``creator_human_id`` for a project, or None if unset.

    Used by the ``POST /projects/{id}/goal`` endpoint to enforce the
    "only the project owner can set goal" contract introduced in v0.3.2.
    """
    async with db.execute(
        "SELECT creator_human_id FROM projects WHERE id = ?", (project_id,)
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    return row["creator_human_id"]


async def update_session_seen(
    db: aiosqlite.Connection, session_id: str
) -> None:
    """Bump a session's last_seen timestamp to now."""
    await db.execute(
        "UPDATE sessions SET last_seen = datetime('now') WHERE id = ?",
        (session_id,),
    )
    await db.commit()


async def count_active_sessions(db: aiosqlite.Connection) -> int:
    """Count sessions currently live (status active or idle, not closed/expired).

    Read-only; used by the public ``/status/sessions`` shields badge. Mirrors the
    'live' definition used by the idle-expiry sweep (status IN active/idle).
    """
    async with db.execute(
        "SELECT COUNT(*) AS n FROM sessions WHERE status IN ('active', 'idle')"
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return 0
    return int(row["n"] if isinstance(row, dict) else row[0])


async def heartbeat_session(
    db: aiosqlite.Connection, session_id: str
) -> bool:
    """Touch ``last_seen`` so the idle-expiry sweep leaves this session
    alone. Returns True when the session exists; False otherwise so the
    HTTP layer can 404 cleanly. Used by long-running workers that don't
    call ``log_task`` often enough to keep the 30 minute TTL fresh.

    Optimization: if ``last_seen`` is within 5 minutes (e.g. because a
    tool call already updated it), skip the write — the session is fresh."""
    from datetime import datetime, timezone
    async with db.execute(
        "SELECT id, last_seen FROM sessions WHERE id = ? AND status != 'closed'",
        (session_id,),
    ) as cur:
        row = await cur.fetchone()
    if not row:
        return False
    last_seen = (row["last_seen"] if isinstance(row, dict) else row[1]) or ""
    try:
        ls_dt = datetime.fromisoformat(last_seen.replace(" ", "T")).replace(tzinfo=timezone.utc)
        age_s = (datetime.now(timezone.utc) - ls_dt).total_seconds()
        if age_s < 300:
            return True  # fresh from a recent tool call — no-op
    except (ValueError, AttributeError):
        pass
    cursor = await db.execute(
        "UPDATE sessions SET last_seen = datetime('now') "
        "WHERE id = ? AND status != 'closed'",
        (session_id,),
    )
    await db.commit()
    return cursor.rowcount > 0


async def keepalive_sessions(
    db: aiosqlite.Connection, session_ids: "list[str]"
) -> int:
    """Bump ``last_seen`` to now for each still-open session in *session_ids*.

    Driven by the server's keepalive loop so a session busy on non-MCP work
    (git/bash/file ops) — and therefore making no tool calls — doesn't drift
    past the live window and get mistaken for dead by a coordinating session.
    Closed/archived rows are skipped. Returns the number of rows refreshed."""
    if not session_ids:
        return 0
    placeholders = ", ".join("?" for _ in session_ids)
    cursor = await db.execute(
        f"UPDATE sessions SET last_seen = datetime('now') "
        f"WHERE id IN ({placeholders}) AND status NOT IN ('closed', 'archived')",
        tuple(session_ids),
    )
    await db.commit()
    return cursor.rowcount


async def touch_latest_active_session(
    db: aiosqlite.Connection, project_id: str | None = None
) -> str | None:
    """4b698ea5 — bump ``last_seen`` on the most-recently-active live session and
    return its id (or None when there is no live session to touch).

    This is the DB side of the *passive* tunnel-liveness signal: the server's
    keepalive loop calls it once per tick for every tenant that holds a live
    tunnel WebSocket, so an executor doing minutes of NON-Meridian work (reading
    files, thinking, running tests) — and therefore touching no Meridian tool —
    still keeps a fresh ``last_seen`` and isn't mistaken for dead.

    The signal is genuinely passive because the liveness *proof* is the open
    tunnel socket (the tenant's local ``meridian --tunnel`` binary is running),
    NOT ``last_seen`` itself — so it is not circular the way a loop that renews
    based on ``last_seen`` would be.

    Association: a tunnel is per-TENANT and a tenant's project DB typically has a
    single active executor. We resolve the ambiguity by touching the ONE
    most-recently-active ('active'/'idle') session — the one a planner would most
    plausibly read as "the live executor". ``project_id`` narrows the scope when a
    tenant DB holds more than one project; None (the common single-project tenant
    DB) considers all of them.
    """
    where = "status IN ('active', 'idle')"
    params: tuple[Any, ...] = ()
    if project_id is not None:
        where += " AND project_id = ?"
        params = (project_id,)
    async with db.execute(
        f"SELECT id FROM sessions WHERE {where} "
        f"ORDER BY last_seen DESC LIMIT 1",
        params,
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    sid = row["id"] if isinstance(row, dict) else row[0]
    await db.execute(
        "UPDATE sessions SET last_seen = datetime('now') WHERE id = ?",
        (sid,),
    )
    await db.commit()
    return sid


# ---------------------------------------------------------------------------
# Sprint-item stall helpers — moved to meridian/db/sprint_items.py
# Imported back via `from .sprint_items import *` (see end of sprint block).
# ---------------------------------------------------------------------------

async def close_session(db: aiosqlite.Connection, session_id: str) -> None:
    """Mark a session as closed and finalize its executor_run.

    bc9259b8 — before tearing down, any sprint item this worker left in_progress
    (closed without complete_sprint_item) is re-queued to pending, or failed
    silently once it exhausts its stall-retry budget.
    """
    await db.execute(
        "UPDATE sessions SET status = 'closed' WHERE id = ?",
        (session_id,),
    )
    await release_file_locks_for_session(db, session_id)
    await release_resource_locks_for_session(db, session_id)
    await release_symbol_claims_for_session(db, session_id)
    await db.commit()
    try:
        await handle_session_stall(db, session_id)
    except Exception:  # noqa: BLE001 — stall recovery must never block session close
        pass
    try:
        await finalize_executor_run(db, session_id, status="done")
    except Exception:
        pass


async def archive_stale_sessions(
    db: aiosqlite.Connection, project_id: str, days: int = 7
) -> int:
    """Move sessions unseen for *days* days to 'archived'.

    Only 'active' and 'idle' sessions are touched — 'closed' and already-
    'archived' rows are left unchanged.  Returns the count updated.

    Any in_progress tasks held by the archived sessions are released back
    to 'pending' so they can be picked up by the next worker session.
    """
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    async with db.execute(
        "SELECT id, sprint_item_id FROM task_log "
        "WHERE claimed_by IN ("
        "  SELECT id FROM sessions "
        "  WHERE project_id = ? AND status NOT IN ('closed', 'archived') AND last_seen < ?"
        ") AND status = 'in_progress'",
        (project_id, cutoff),
    ) as cur:
        stale_rows = await cur.fetchall()
    stale_task_ids = [row["id"] for row in stale_rows]
    linked_item_ids = [
        row["sprint_item_id"] for row in stale_rows if row["sprint_item_id"]
    ]
    if stale_task_ids:
        placeholders = ", ".join("?" for _ in stale_task_ids)
        await db.execute(
            f"UPDATE task_log SET status = 'pending', claimed_by = NULL, claimed_at = NULL "
            f"WHERE id IN ({placeholders})",
            tuple(stale_task_ids),
        )
    if linked_item_ids:
        placeholders = ", ".join("?" for _ in linked_item_ids)
        await db.execute(
            f"UPDATE sprint_items SET status = 'pending', completed_at = NULL "
            f"WHERE id IN ({placeholders}) AND status = 'in_progress'",
            tuple(linked_item_ids),
        )
    cursor = await db.execute(
        "UPDATE sessions SET status = 'archived' "
        "WHERE project_id = ? "
        "AND status NOT IN ('closed', 'archived') "
        "AND last_seen < ?",
        (project_id, cutoff),
    )
    await db.commit()
    return cursor.rowcount


async def archive_empty_sessions(
    db: aiosqlite.Connection, days: int = 7
) -> int:
    """Archive sessions older than *days* that never logged any tasks."""
    from datetime import datetime, timedelta, timezone

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    cursor = await db.execute(
        "UPDATE sessions SET status = 'archived' "
        "WHERE created_at < ? "
        "AND status != 'active' "
        "AND id NOT IN (SELECT DISTINCT session_id FROM task_log)",
        (cutoff,),
    )
    await db.commit()
    return cursor.rowcount


async def get_sessions(
    db: aiosqlite.Connection,
    project_id: str,
    active_only: bool = True,
) -> list[dict[str, Any]]:
    """List sessions for a project, newest first."""
    if active_only:
        query = (
            "SELECT * FROM sessions WHERE project_id = ? "
            "AND status NOT IN ('closed', 'archived') ORDER BY last_seen DESC"
        )
    else:
        query = (
            "SELECT * FROM sessions WHERE project_id = ? "
            "ORDER BY last_seen DESC"
        )
    async with db.execute(query, (project_id,)) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows]  # type: ignore[misc]


def _session_item_outcome(item_status: str | None, session_status: str) -> str:
    """1e1bd6b0 — derive a sprint item's outcome WITHIN a session.

    Distinguishes 'stopped-ambiguously' (the session ended while the item it had
    claimed was still in_progress — a silent stop, not a real failure) from
    'failed' (the item actively errored). This is the crux of the session-timeline
    view: telling "the session died" apart from "the work failed".
    """
    s = (item_status or "").lower()
    if s in ("done", "pushed", "provisional_complete"):
        return "done"
    if s == "failed":
        return "failed"
    if s == "in_progress":
        return "stopped-ambiguously" if session_status in ("closed", "archived") else "in_progress"
    return s or "unknown"


async def get_executor_session_timeline(
    db: aiosqlite.Connection, project_id: str
) -> dict[str, Any]:
    """1e1bd6b0 — per-executor-session timeline, items grouped by item_group.

    Each session carries its start (``created_at``) / end (``last_seen`` once
    closed) + the sprint items it acted on (matched via ``sprint_items.actor``),
    grouped by ``item_group``, each item tagged with a derived outcome
    (done / failed / stopped-ambiguously / in_progress). Reuses existing data only
    — no new tracking. Sessions are newest-first (``get_sessions`` order).
    """
    sessions = await get_sessions(db, project_id, active_only=False)
    items = await get_sprint_items(db, project_id)
    by_actor: dict[str, list[dict[str, Any]]] = {}
    for it in items:
        actor = it.get("actor")
        if actor:
            by_actor.setdefault(str(actor), []).append(it)

    out_sessions: list[dict[str, Any]] = []
    for sess in sessions:
        sid = str(sess.get("id"))
        s_status = (sess.get("status") or "active").lower()
        ended = sess.get("last_seen") if s_status in ("closed", "archived") else None
        my_items = by_actor.get(sid, [])
        groups: dict[str, list[dict[str, Any]]] = {}
        counts = {"done": 0, "failed": 0, "stopped_ambiguously": 0, "in_progress": 0, "other": 0}
        for it in my_items:
            outcome = _session_item_outcome(it.get("status"), s_status)
            key = it.get("item_group") or "(ungrouped)"
            groups.setdefault(key, []).append({
                "id": it.get("id"), "title": it.get("title"),
                "nickname": it.get("nickname"), "status": it.get("status"),
                "completed_at": it.get("completed_at"), "outcome": outcome,
            })
            ck = outcome.replace("-", "_")
            counts[ck if ck in counts else "other"] += 1
        out_sessions.append({
            "id": sid, "name": sess.get("name"),
            "session_type": sess.get("session_type"), "client_type": sess.get("client_type"),
            "status": s_status,
            "started_at": sess.get("created_at"), "ended_at": ended,
            "groups": [{"item_group": g, "items": its} for g, its in groups.items()],
            "outcome_counts": counts, "item_count": len(my_items),
        })
    return {"project_id": project_id, "sessions": out_sessions}


async def log_task(
    db: aiosqlite.Connection,
    session_id: str,
    project_id: str,
    description: str,
    status: str = "done",
    parent_session_id: str | None = None,
    parent_task_id: str | None = None,
    sprint_item_id: str | None = None,
    kind: str | None = None,
) -> dict[str, Any]:
    """Append a task-log entry and broadcast to live subscribers.

    ``parent_session_id`` (v1.2.1) records that this task was kicked
    off by another session — typically when an enqueue_claude_task
    worker logs its result. The timeline + auto-summary use it to
    correlate parent / child sessions.

    ``parent_task_id`` (v2.4) records that this task is a sub-step of
    another task. Lets the dashboard render multi-agent work as a tree
    (researcher → fetched 3 sources, writer → drafted reply, etc.).

    ``kind`` (Sprint-4) is the entry taxonomy: shipped/found/decided/blocked.
    Defaults to 'shipped'. Unknown values are coerced to NULL.
    """
    if status not in {"pending", "in_progress", "done", "failed", "pending-hitl", "backlog", "future", "backburner"}:
        raise ValueError(f"invalid task status: {status}")
    if kind not in ("shipped", "found", "decided", "blocked"):
        kind = None
    from meridian.secret_redaction import check_for_secrets
    check_for_secrets(description, context="task description")
    tid = _new_id()
    await db.execute(
        "INSERT INTO task_log "
        "(id, session_id, project_id, description, status, parent_session_id, parent_task_id, sprint_item_id, kind) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            tid,
            session_id,
            project_id,
            description,
            status,
            parent_session_id,
            parent_task_id,
            sprint_item_id,
            kind,
        ),
    )
    await update_session_seen(db, session_id)
    await db.commit()
    async with db.execute(
        "SELECT * FROM task_log WHERE id = ?", (tid,)
    ) as cur:
        row = await cur.fetchone()
    task = _row_to_dict(row)
    assert task is not None
    _publish_task("task_created", task)
    try:
        await append_executor_run_transcript(db, session_id, description)
    except Exception:
        pass
    return task


async def get_task(
    db: aiosqlite.Connection, task_id: str
) -> dict[str, Any] | None:
    """Look up a single task by id."""
    async with db.execute(
        "SELECT * FROM task_log WHERE id = ?", (task_id,)
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


async def update_task(
    db: aiosqlite.Connection,
    task_id: str,
    *,
    status: str | None = None,
    description: str | None = None,
    project_id: str | None = None,
) -> dict[str, Any] | None:
    """Update a task's status and/or description in place.

    Returns the updated task dict, or None if the id doesn't exist. Used by
    the paid-tier ``enqueue_claude_task`` worker to mark a pending task done
    or failed once the subprocess returns.

    ``project_id`` is an optional safety guard: when supplied the UPDATE
    includes ``AND project_id = ?`` so a caller cannot accidentally mutate a
    task that belongs to a different project.
    """
    fields: list[str] = []
    values: list[Any] = []
    if status is not None:
        if status not in {"pending", "in_progress", "done", "failed", "pending-hitl"}:
            raise ValueError(f"invalid task status: {status}")
        fields.append("status = ?")
        values.append(status)
    if description is not None:
        from meridian.secret_redaction import check_for_secrets
        check_for_secrets(description, context="task description")
        fields.append("description = ?")
        values.append(description)
    if not fields:
        return await get_task(db, task_id)
    if project_id is not None:
        values.extend([task_id, project_id])
        where = "id = ? AND project_id = ?"
    else:
        values.append(task_id)
        where = "id = ?"
    await db.execute(
        f"UPDATE task_log SET {', '.join(fields)} WHERE {where}", values
    )
    await db.commit()
    updated = await get_task(db, task_id)
    if updated is not None:
        _publish_task("task_updated", updated)
    return updated


async def update_task_worker_pid(
    db: aiosqlite.Connection, task_id: str, pid: int
) -> None:
    """Store the worker subprocess PID on the task row.

    Called immediately after the subprocess spawns so the PID watchdog
    can detect orphaned in_progress tasks whose worker process has died.
    """
    await db.execute(
        "UPDATE task_log SET worker_pid = ? WHERE id = ?", (pid, task_id)
    )
    await db.commit()


async def get_in_progress_tasks_with_pid(
    db: aiosqlite.Connection,
) -> list[dict[str, Any]]:
    """Return all in_progress task rows that have a worker_pid set.

    Used by the PID watchdog in the auto-summary loop to detect orphaned
    workers. A task is orphaned if its worker_pid process is no longer
    running (i.e., ``os.kill(pid, 0)`` raises ``ProcessLookupError``).
    """
    async with db.execute(
        "SELECT * FROM task_log WHERE status = 'in_progress' AND worker_pid IS NOT NULL"
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows]


async def get_tasks(
    db: aiosqlite.Connection, project_id: str, limit: int = 20, offset: int = 0
) -> list[dict[str, Any]]:
    """Return recent tasks for a project, newest first.

    Joins sessions to include session_name and human_id for display.
    Supports pagination via limit/offset.
    """
    async with db.execute(
        "SELECT t.*, s.name AS session_name, s.human_id AS human_id, "
        "cs.human_id AS claimed_by_human_id, cs.name AS claimed_by_session_name "
        "FROM task_log t "
        "LEFT JOIN sessions s ON s.id = t.session_id "
        "LEFT JOIN sessions cs ON cs.id = t.claimed_by "
        "WHERE t.project_id = ? "
        "ORDER BY t.created_at DESC, t.rowid DESC LIMIT ? OFFSET ?",
        (project_id, limit, offset),
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows]  # type: ignore[misc]


def _like_escape(term: str) -> str:
    """f51e38d8 — escape LIKE/ILIKE wildcard characters in a search term.

    SQLite (and Postgres) LIKE/ILIKE treat ``%``, ``_``, and the escape
    character itself as special. When user input is used as a LIKE operand
    via a parameterized placeholder the special chars are still interpreted
    as wildcards, so searching for ``file_name`` would match ``file1name``
    and searching for ``100%`` would widen to match anything containing
    ``100``.

    This function escapes ``!`` → ``!!``, ``%`` → ``!%``, ``_`` → ``!_``
    so the caller can use ``LIKE ? ESCAPE '!'`` and get literal substring
    matching even when the term contains those characters.
    """
    return term.replace("!", "!!").replace("%", "!%").replace("_", "!_")


def _multiword_match_clause(
    columns: "list[str]", query: str, *, op: str = "LIKE"
) -> "tuple[str, list[str]]":
    """fcf90f3a — WHERE fragment matching rows where EVERY whitespace-separated
    term in ``query`` appears in at least one of ``columns`` (AND across terms,
    OR across columns).

    A single ``%query%`` LIKE matched only the contiguous phrase, so multi-word
    queries whose words weren't adjacent ("RAG problem definition") — or that
    crossed punctuation ("retrieval-augmented generation") — silently returned
    zero. ANDing the terms fixes that while staying more precise than OR-ing:
    these searches have no relevance ranking (just created_at DESC), so OR would
    flood the top with rows matching one common word. Terms shorter than 2 chars
    are dropped and the list is capped; a single-token query falls back to the
    whole string. Returns ``(sql_fragment, params)`` — a parenthesized boolean
    safe to AND into an existing WHERE. Placeholders are ``?`` (the pg adapter
    rewrites to %s); ``op`` is LIKE (SQLite, case-insensitive) or ILIKE (PG)."""
    terms = [t for t in query.split() if len(t) >= 2][:8] or [query]
    per_term: list[str] = []
    params: list[str] = []
    for term in terms:
        ors = " OR ".join(f"{c} {op} ?" for c in columns)
        per_term.append(f"({ors})")
        params.extend([f"%{term}%"] * len(columns))
    return "(" + " AND ".join(per_term) + ")", params


def _search_terms(query: str) -> "list[str]":
    """Tokenize a free-text query into search terms for search_all.

    Whitespace-split, drop terms shorter than 2 chars, cap at 8 terms. When
    every token is too short (or the query is a single short token) fall back to
    the whole trimmed query as one term. Mirrors :func:`_multiword_match_clause`
    tokenization so the two paths agree on what a "term" is.
    """
    terms = [t for t in query.split() if len(t) >= 2][:8]
    if not terms:
        stripped = query.strip()
        return [stripped] if stripped else []
    return terms


def _multiword_or_ranked_clause(
    columns: "list[str]", query: str, *, op: str = "LIKE"
) -> "tuple[str, str, list[str], list[str]]":
    """25155e91 — graceful-degradation match+rank fragment for search_all (SQLite).

    Unlike :func:`_multiword_match_clause` (which ANDs every term, so one rare
    absent token — ``x=768`` in a long natural-language query — zeroes the whole
    result), this matches rows where ANY term appears (OR across terms, OR across
    columns) and returns a *score* expression counting how many distinct terms
    matched, so more-complete matches rank first. A completely unrelated query
    still matches no rows (no term appears anywhere), and a single-term query
    behaves exactly like the old contiguous-substring match.

    Returns ``(where_sql, score_sql, where_params, score_params)``:

    * ``where_sql`` — a parenthesized ``(... OR ...)`` boolean, safe to AND into
      an existing WHERE. Matches a row if at least one term is a substring of at
      least one column.
    * ``score_sql`` — ``(CASE WHEN <term1 present> THEN 1 ELSE 0 END + ...)``,
      the count of matched terms, for ``ORDER BY <score> DESC``.
    * ``where_params`` / ``score_params`` — the LIKE params for each, in the
      order the fragments emit their placeholders. ``score_sql`` is emitted in
      the SELECT list (before WHERE), so callers bind ``score_params`` first.

    Placeholders are ``?`` (the pg adapter rewrites to %s); ``op`` is LIKE
    (SQLite, case-insensitive) or ILIKE (PG).

    f51e38d8 — wildcard characters (``%``, ``_``, ``!``) in query terms are
    escaped via :func:`_like_escape` and ``ESCAPE '!'`` is appended to every
    LIKE/ILIKE clause so that literal substring matching is preserved even when
    the query contains those characters.
    """
    terms = _search_terms(query)
    where_parts: list[str] = []
    score_parts: list[str] = []
    where_params: list[str] = []
    score_params: list[str] = []
    for term in terms:
        col_or = " OR ".join(f"{c} {op} ? ESCAPE '!'" for c in columns)
        like = f"%{_like_escape(term)}%"
        where_parts.append(f"({col_or})")
        where_params.extend([like] * len(columns))
        score_parts.append(f"(CASE WHEN ({col_or}) THEN 1 ELSE 0 END)")
        score_params.extend([like] * len(columns))
    where_sql = "(" + " OR ".join(where_parts) + ")"
    score_sql = "(" + " + ".join(score_parts) + ")"
    return where_sql, score_sql, where_params, score_params


def _or_tsquery_source(query: str) -> str:
    """25155e91 — rewrite a free-text query into an OR form for websearch_to_tsquery.

    ``websearch_to_tsquery('english', 'a b c')`` ANDs its terms (``a & b & c``),
    so a long natural-language query with any rare/absent token matches nothing.
    websearch treats the bare word ``or`` as the OR operator, so joining the
    terms with `` or `` yields ``a | b | c`` — match ANY term, and ``ts_rank``
    then floats rows matching more (and better) terms to the top: graceful
    degradation instead of zero results.

    Each term is stripped of the characters websearch reads as operators at a
    term boundary (quotes and a leading ``-`` NOT) so a punctuation-heavy token
    can't flip the whole query into a phrase/NOT search. websearch still splits
    intra-token punctuation (``x=768`` -> ``x <-> 768``) on its own. A
    single-term query is returned unchanged (no ``or`` injected), so existing
    single-word / stemmed behavior is preserved exactly. The reserved words
    ``or``/``and`` appearing as literal query terms are dropped from the operator
    join (they'd be no-ops as lexemes anyway).
    """
    cleaned: list[str] = []
    for raw in query.split():
        term = raw.strip("\"'")
        term = term.lstrip("-")
        if not term or term.lower() in ("or", "and"):
            continue
        cleaned.append(term)
    if not cleaned:
        return query
    return " or ".join(cleaned)


async def search_tasks(
    db: Any,
    project_id: str,
    query: str,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Full-text task search.

    Postgres: uses pg_trgm similarity() for ranking — no ML model required,
    fast on the GIN index added by _migrate_pg_v27_pg_trgm — OR'd with a
    multiword ILIKE term-match and (82e0b887) an on-the-fly tsvector full-text
    predicate so stemmed / non-substring queries also match. Ordering is
    similarity DESC, ts_rank DESC, created_at DESC.

    SQLite: keyword substring term-match on description (unchanged).

    Returns [{id, description, status, created_at, similarity, session_name}].
    """
    is_pg = hasattr(db, "_pool")
    if is_pg:
        # fcf90f3a — AND the query terms so a multi-word query isn't a single
        # %...% ILIKE that only matches the contiguous phrase. Similarity ranking
        # still orders results; the term-match just widens the candidate set.
        match_sql, match_params = _multiword_match_clause(
            ["t.description"], query, op="ILIKE")
        # 82e0b887 — additively widen the candidate set with tsvector full-text
        # match so stemmed / morphological queries ("authenticating users" vs a
        # task "authentication for the user") also match. websearch_to_tsquery
        # tolerates arbitrary user input without raising. similarity() drives
        # ordering (trigram score); ts_rank is a secondary tiebreak so rows
        # matched purely by FTS still order sensibly.
        #
        # NOTE: similarity() is intentionally NOT used in the WHERE predicate.
        # A low threshold (e.g. 0.05) caused false-positive matches for queries
        # whose terms partially overlap the description — e.g. "authentication
        # payments" matched "Fix the authentication bug in the login flow"
        # because "authentication" contributes enough shared trigrams to exceed
        # 0.05 even though "payments" is absent. The tsvector (AND) and ILIKE
        # (per-term AND) predicates correctly enforce that the query terms must
        # actually appear in the description; similarity is a ranking signal only.
        sql = (
            "SELECT t.id, t.description, t.status, t.created_at, "
            "s.name AS session_name, "
            "COALESCE(similarity(t.description, ?), 0.0) AS similarity "
            "FROM task_log t "
            "LEFT JOIN sessions s ON s.id = t.session_id "
            "WHERE t.project_id = ? "
            "AND (to_tsvector('english', coalesce(t.description,'')) "
            "@@ websearch_to_tsquery('english', ?) "
            f"OR {match_sql}) "
            "ORDER BY similarity DESC, "
            "ts_rank(to_tsvector('english', coalesce(t.description,'')), "
            "websearch_to_tsquery('english', ?)) DESC, "
            "t.created_at DESC LIMIT ?"
        )
        params: tuple = (
            query, project_id, query, *match_params, query, limit)
    else:
        match_sql, match_params = _multiword_match_clause(["t.description"], query)
        sql = (
            "SELECT t.id, t.description, t.status, t.created_at, "
            "s.name AS session_name, 1.0 AS similarity "
            "FROM task_log t "
            "LEFT JOIN sessions s ON s.id = t.session_id "
            f"WHERE t.project_id = ? AND {match_sql} "
            "ORDER BY t.created_at DESC LIMIT ?"
        )
        params = (project_id, *match_params, limit)
    async with db.execute(sql, params) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows]  # type: ignore[misc]


def _search_snippet(text: str | None, query: str, window: int = 60) -> str:
    """Return a short context window of ``text`` centered on ``query``.

    Used by :func:`search_all` to give the dashboard a preview of the matching
    body text. Case-insensitive. Adds leading/trailing ellipses when the snippet
    is clipped from a longer body. Returns an empty string when ``text`` is
    falsy or the query term is not present (e.g. a title-only match).
    """
    if not text or not query:
        return ""
    low = text.lower()
    anchor = query.lower()
    idx = low.find(anchor)
    if idx == -1:
        # fcf90f3a — multi-word query whose exact phrase isn't contiguous: anchor
        # the preview on the first query term that IS present.
        for term in query.split():
            if len(term) >= 2 and (i := low.find(term.lower())) != -1:
                anchor, idx = term.lower(), i
                break
    if idx == -1:
        return ""
    start = max(0, idx - window)
    end = min(len(text), idx + len(anchor) + window)
    snippet = text[start:end].strip()
    if start > 0:
        snippet = "…" + snippet
    if end < len(text):
        snippet = snippet + "…"
    return snippet


# ---------------------------------------------------------------------------
# 56cd8712 — Model2Vec semantic escalation for search_all (Postgres only).
# SAFE-BY-DEFAULT: OFF unless MERIDIAN_SEMANTIC_ENABLED is truthy, model2vec is
# importable, and the runtime RSS circuit breaker is not tripped. All of that is
# folded into semantic_search.is_available(); when it's False this is a NO-OP and
# the plain keyword/tsvector results are returned UNCHANGED.
# ---------------------------------------------------------------------------

# Cap on how many corpus rows we embed per escalation. The static model is fast but
# we still bound the work (and memory) on a 512MB box.
_SEMANTIC_CORPUS_CAP = 200


async def _pure_fts_count_and_trigram_top(
    db: Any, project_id: str, query: str
) -> "tuple[int, float]":
    """Return (pure_fts_count, trigram_top) over the project's searchable text — PG.

    ``pure_fts_count`` — number of rows across task_log / project_notes /
    decisions_pinned / sprint_items whose text matches PURE
    ``websearch_to_tsquery`` FTS (no OR with trigram/ILIKE). ``trigram_top`` — the
    single best pg_trgm ``similarity`` of the query against any of those text
    fields. Together they answer "did keyword search genuinely find nothing good?"
    for :func:`meridian.semantic_search.should_escalate`.

    psycopg3: ``?`` placeholders (adapter rewrites to %s), autocommit. On any error
    returns ``(1, 1.0)`` — a "found something" signal that suppresses escalation, so
    a metadata hiccup never spuriously spins up the model.
    """
    sql = (
        "WITH corpus AS ("
        "  SELECT coalesce(description,'') AS txt FROM task_log WHERE project_id = ? "
        "  UNION ALL "
        "  SELECT coalesce(title,'') || ' ' || coalesce(body,'') FROM project_notes WHERE project_id = ? "
        "  UNION ALL "
        "  SELECT coalesce(title,'') || ' ' || coalesce(body,'') FROM decisions_pinned "
        "    WHERE project_id = ? AND status = 'active' "
        "  UNION ALL "
        "  SELECT coalesce(title,'') || ' ' || coalesce(notes,'') FROM sprint_items WHERE project_id = ? "
        ") "
        "SELECT "
        "  count(*) FILTER ("
        "    WHERE to_tsvector('english', txt) @@ websearch_to_tsquery('english', ?)"
        "  ) AS fts_count, "
        "  coalesce(max(similarity(txt, ?)), 0.0) AS trigram_top "
        "FROM corpus"
    )
    params: tuple = (project_id, project_id, project_id, project_id, query, query)
    try:
        async with db.execute(sql, params) as cur:
            row = await cur.fetchone()
    except Exception:  # noqa: BLE001 - never let metadata query break search
        _log.warning("semantic gate: pre-count failed — suppressing escalation", exc_info=True)
        return 1, 1.0
    if row is None:
        # 56cd8712 — fail-safe consistent with the exception path: a missing row
        # SUPPRESSES escalation (1,1.0) rather than firing it (0,0.0). Unreachable
        # for a single-row count/max aggregate, but defensive.
        return 1, 1.0
    d = _row_to_dict(row)  # type: ignore[misc]
    try:
        return int(d.get("fts_count") or 0), float(d.get("trigram_top") or 0.0)
    except (TypeError, ValueError):
        return 1, 1.0


async def _semantic_candidate_corpus(
    db: Any, project_id: str, cap: int
) -> "list[tuple[str, str, str]]":
    """Fetch the project's candidate corpus for semantic ranking — PG.

    Returns ``[(match_type, id, text)]`` across the four content types (title+body
    concatenated). Bounded by ``cap`` rows total. On error returns ``[]``.
    """
    sql = (
        "SELECT 'task' AS match_type, id, coalesce(description,'') AS txt "
        "  FROM task_log WHERE project_id = ? "
        "UNION ALL "
        "SELECT 'note', id, coalesce(title,'') || ' ' || coalesce(body,'') "
        "  FROM project_notes WHERE project_id = ? "
        "UNION ALL "
        "SELECT 'decision', id, coalesce(title,'') || ' ' || coalesce(body,'') "
        "  FROM decisions_pinned WHERE project_id = ? AND status = 'active' "
        "UNION ALL "
        "SELECT 'sprint_item', id, coalesce(title,'') || ' ' || coalesce(notes,'') "
        "  FROM sprint_items WHERE project_id = ? "
        "LIMIT ?"
    )
    params: tuple = (project_id, project_id, project_id, project_id, cap)
    try:
        async with db.execute(sql, params) as cur:
            rows = await cur.fetchall()
    except Exception:  # noqa: BLE001
        _log.warning("semantic escalation: corpus fetch failed", exc_info=True)
        return []
    out: list[tuple[str, str, str]] = []
    for r in rows:  # type: ignore[assignment]
        d = _row_to_dict(r)  # type: ignore[misc]
        out.append((d.get("match_type", ""), d.get("id", ""), d.get("txt", "") or ""))
    return out


async def _hydrate_semantic_rows(
    db: Any, project_id: str, by_type: "dict[str, list[str]]"
) -> "dict[str, list[dict[str, Any]]]":
    """Load full result rows for the semantically-matched ids, grouped by type.

    Mirrors the column shape of the keyword-path SELECTs so merged results are
    homogeneous. Ordering within each group follows the id order passed in (which is
    the semantic rank order). On error a type yields no rows.
    """
    result: dict[str, list[dict[str, Any]]] = {
        "task": [], "note": [], "decision": [], "sprint_item": []
    }
    selects = {
        "task": (
            "SELECT id, description, status, created_at, 'task' AS match_type "
            "FROM task_log WHERE project_id = ? AND id = ?"
        ),
        "note": (
            "SELECT id, title, body, tags, created_at, 'note' AS match_type "
            "FROM project_notes WHERE project_id = ? AND id = ?"
        ),
        "decision": (
            "SELECT id, title, body, category, status, created_at, 'decision' AS match_type "
            "FROM decisions_pinned WHERE project_id = ? AND status = 'active' AND id = ?"
        ),
        "sprint_item": (
            "SELECT id, title, notes, version, status, added_at AS created_at, 'sprint_item' AS match_type "
            "FROM sprint_items WHERE project_id = ? AND id = ?"
        ),
    }
    for mtype, ids in by_type.items():
        sql = selects.get(mtype)
        if not sql:
            continue
        for rid in ids:
            try:
                async with db.execute(sql, (project_id, rid)) as cur:
                    row = await cur.fetchone()
            except Exception:  # noqa: BLE001
                continue
            if row is not None:
                result[mtype].append(_row_to_dict(row))  # type: ignore[misc]
    return result


async def _maybe_semantic_escalate(
    db: Any,
    project_id: str,
    query: str,
    limit: int,
    tasks: "list[dict[str, Any]]",
    notes: "list[dict[str, Any]]",
    decisions: "list[dict[str, Any]]",
    sprint_items: "list[dict[str, Any]]",
) -> "tuple[list[dict], list[dict], list[dict], list[dict]]":
    """Optionally augment the keyword results with semantic hits (PG-only).

    Returns the (possibly augmented) ``(tasks, notes, decisions, sprint_items)``.
    A NO-OP — returns the inputs unchanged — unless ALL hold:

    * ``semantic_search.is_available()`` (enabled + importable + not tripped), AND
    * the CORRECTED gate fires: pure-FTS count == 0 AND pg_trgm top < ~0.1.

    Semantic hits are ranked by cosine (floor-filtered) and appended AFTER the
    keyword rows, deduped by id, keyword-first — so existing behavior is a strict
    prefix of the augmented result and the return shape is identical.

    e631d54f — every semantically-augmented row carries ``semantic: True``
    (unchanged) plus two additive fields: ``embedding_model`` (the model
    that produced the ranking, :func:`semantic_search.model_name`) and
    ``degraded`` (:func:`semantic_search.is_corpus_capped` against
    :data:`_SEMANTIC_CORPUS_CAP`). ``degraded=True`` means the candidate
    corpus hit the cap — these rows are real matches within that bounded
    window, but the window is not exhaustive, so callers must not treat a
    capped escalation as an authoritative "nothing else matches" answer.

    3d3ccf2d (follow-up on 2204ce80) — candidates are ranked, then scored via
    :func:`semantic_search.score_confidence` into typed
    :class:`semantic_search.SemanticMatch` results (lexical/semantic/fused
    score, threshold, runner-up margin, reason). ONLY ``confident=True``
    matches are merged into the results — a match that clears the absolute
    cosine floor but sits too close to the next-best DIFFERENT candidate
    (``reason="ambiguous_runner_up"``, e.g. a stale record and its fresh
    replacement scoring near-identically) is deterministic-abstained:
    excluded rather than guessed, exactly like a sub-floor match
    (``reason="below_confidence_threshold"``) always was. Every surfaced
    semantic hit is annotated with its score breakdown for transparency.
    Project/version/pointer scoping is an upstream hard gate this function
    never touches: ``_semantic_candidate_corpus`` already scopes every
    candidate to ``project_id`` before ranking ever runs, so cross-project
    leakage cannot occur regardless of confidence scoring.
    """
    from meridian import semantic_search

    if not semantic_search.is_available():
        return tasks, notes, decisions, sprint_items

    pure_fts, trigram_top = await _pure_fts_count_and_trigram_top(db, project_id, query)
    if not semantic_search.should_escalate(pure_fts, trigram_top):
        return tasks, notes, decisions, sprint_items

    corpus = await _semantic_candidate_corpus(db, project_id, _SEMANTIC_CORPUS_CAP)
    if not corpus:
        return tasks, notes, decisions, sprint_items

    # rank() takes [(id, text)]; keep a side map id -> match_type to regroup.
    type_by_id: dict[str, str] = {cid: mtype for mtype, cid, _ in corpus}
    ranked = semantic_search.rank(query, [(cid, txt) for _, cid, txt in corpus])
    if not ranked:  # unavailable mid-flight (breaker tripped) or all sub-floor
        return tasks, notes, decisions, sprint_items

    # 3d3ccf2d — typed, confidence-scored verdict per candidate. No lexical
    # score is supplied: this escalation path only ever fires when keyword
    # search genuinely found nothing (should_escalate's own precondition), so
    # every candidate here is semantic-only by construction.
    matches = semantic_search.score_confidence(ranked)
    if not matches:
        return tasks, notes, decisions, sprint_items
    match_by_id = {m.id: m for m in matches}

    # Exclude ids already present in the keyword results (keyword-first dedupe).
    existing: dict[str, set[str]] = {
        "task": {t.get("id") for t in tasks},
        "note": {n.get("id") for n in notes},
        "decision": {d.get("id") for d in decisions},
        "sprint_item": {s.get("id") for s in sprint_items},
    }
    new_by_type: dict[str, list[str]] = {
        "task": [], "note": [], "decision": [], "sprint_item": []
    }
    for m in matches:
        if not m.confident:
            # Deterministic abstention: below the confidence floor or too
            # close to a runner-up — refuse automatic binding rather than
            # presenting an ambiguous candidate as a solid match.
            continue
        mtype = type_by_id.get(m.id)
        if not mtype or mtype not in new_by_type:
            continue
        if m.id in existing.get(mtype, set()):
            continue
        if len(new_by_type[mtype]) >= limit:
            continue
        new_by_type[mtype].append(m.id)

    hydrated = await _hydrate_semantic_rows(db, project_id, new_by_type)
    # Mark provenance so callers/UI can distinguish semantic-augmented rows.
    # e631d54f (follow-up to 56cd8712) — also mark `degraded` + the embedding
    # model that produced the ranking whenever the candidate corpus hit the
    # cap: semantic_search.is_corpus_capped() means ranking only considered a
    # bounded WINDOW of the project's rows, never an exhaustive pass, so a
    # caller must treat these hits as candidates only — never as proof that
    # nothing better exists elsewhere (an authoritative pointer/provenance
    # gate must not be satisfied by a capped semantic escalation alone).
    # 3d3ccf2d — also attach the full typed confidence-score breakdown for
    # transparency/debugging.
    row_degraded = semantic_search.is_corpus_capped(len(corpus), _SEMANTIC_CORPUS_CAP)
    embedding_model = semantic_search.model_name()
    for mtype, rows in hydrated.items():
        for r in rows:
            r["semantic"] = True
            r["degraded"] = row_degraded
            r["embedding_model"] = embedding_model
            m = match_by_id.get(r.get("id"))
            if m is not None:
                r["semantic_score"] = m.semantic_score
                r["semantic_fused_score"] = m.fused_score
                r["semantic_threshold"] = m.threshold
                r["semantic_margin"] = m.margin
                r["semantic_reason"] = m.reason
    return (
        tasks + hydrated["task"],
        notes + hydrated["note"],
        decisions + hydrated["decision"],
        sprint_items + hydrated["sprint_item"],
    )


# ---------------------------------------------------------------------------
# 2204ce80 — hybrid candidate retrieval: exact-filter-first lexical discovery
# with OPTIONAL bounded local semantic reranking. Shared by handoff generation
# (meridian.handoff._gather_related_planning_records) and available to any
# future caller needing the same "exact filters -> lexical discovery -> bounded
# semantic rerank" shape search_all's own semantic escalation (56cd8712)
# pioneered — but stricter: semantic here ONLY re-scores rows the lexical pass
# already discovered, it never adds candidates lexical search missed (see
# ``hybrid_candidate_retrieval`` docstring). No paid LLM call anywhere in this
# path — the only ML step is the local Model2Vec cosine rerank, itself gated
# behind ``semantic_search.is_available()`` (OFF unless explicitly enabled).
# ---------------------------------------------------------------------------

# table/column shape for each source type this path knows how to retrieve.
# ``status_col``/``version_col`` are ``None`` when the table has no such
# column (an exact status/version filter for that type is then a no-op).
_HYBRID_SOURCE_SPECS: dict[str, dict[str, Any]] = {
    "task": {
        "table": "task_log",
        "text_cols": ("description",),
        "title_col": None,
        "created_col": "created_at",
        "status_col": "status",
        "version_col": None,
    },
    "note": {
        "table": "project_notes",
        "text_cols": ("title", "body"),
        "title_col": "title",
        "created_col": "created_at",
        "status_col": None,
        "version_col": None,
    },
    "decision": {
        "table": "decisions_pinned",
        "text_cols": ("title", "body"),
        "title_col": "title",
        "created_col": "created_at",
        "status_col": "status",
        "version_col": None,
    },
    "sprint_item": {
        "table": "sprint_items",
        "text_cols": ("title", "notes"),
        "title_col": "title",
        "created_col": "added_at",
        "status_col": "status",
        "version_col": "version",
    },
}
_HYBRID_DEFAULT_SOURCE_TYPES: "tuple[str, ...]" = ("task", "note", "decision", "sprint_item")
# Fusion weights: lexical is the deterministic/trusted signal (substring or
# FTS/pg_trgm backed); semantic is a best-effort nudge applied only within the
# already-bounded candidate pool, never the sole basis for a candidate existing.
_HYBRID_LEXICAL_WEIGHT = 0.6
_HYBRID_SEMANTIC_WEIGHT = 0.4
# pg_trgm similarity floor used only to WIDEN the lexical WHERE predicate
# (alongside the tsvector match) so near-miss keyword phrasing still surfaces
# as a lexical candidate; ranking itself is driven by the computed score, not
# this threshold.
_HYBRID_TRIGRAM_WHERE_FLOOR = 0.05


async def _hybrid_lexical_candidates(
    db: Any,
    project_id: str,
    query: str,
    stype: str,
    spec: dict[str, Any],
    *,
    version: str | None,
    status_list: "list[str] | None",
    is_pg: bool,
    pool: int,
) -> list[dict[str, Any]]:
    """Exact-filter-first lexical candidate discovery for one source type.

    Filter order matters: ``project_id`` (always), then the exact ``version``/
    ``status`` filters (when the table has that column), are ALL applied in
    the SQL ``WHERE`` before the lexical predicate ever runs — so lexical/
    semantic scoring only ever sees rows that already passed the exact scope
    filters, never the reverse.
    """
    table = spec["table"]
    text_cols = spec["text_cols"]
    title_col = spec["title_col"]
    created_col = spec["created_col"]
    status_col = spec["status_col"]
    version_col = spec["version_col"]
    text_expr = " || ' ' || ".join(f"coalesce({c}, '')" for c in text_cols)
    title_select = f"{title_col} AS _title" if title_col else "NULL AS _title"

    where_parts = ["project_id = ?"]
    where_params: list[Any] = [project_id]
    if version is not None and version_col is not None:
        where_parts.append(f"{version_col} = ?")
        where_params.append(version)
    if status_list is not None and status_col is not None:
        placeholders = ", ".join("?" for _ in status_list)
        where_parts.append(f"{status_col} IN ({placeholders})")
        where_params.extend(status_list)
    elif status_list is None and status_col is not None and stype == "decision":
        # Preserve the convention every other read path in this module uses:
        # decisions default to active-only unless a status is explicitly given.
        where_parts.append("status = 'active'")

    if is_pg:
        tsq = _or_tsquery_source(query)
        score_sql = f"similarity({text_expr}, ?)"
        score_params: list[Any] = [query]
        where_parts.append(
            f"(to_tsvector('english', {text_expr}) @@ websearch_to_tsquery('english', ?) "
            f"OR similarity({text_expr}, ?) > {_HYBRID_TRIGRAM_WHERE_FLOOR})"
        )
        where_params.extend([tsq, query])
    else:
        w, sc, wp, sp = _multiword_or_ranked_clause(list(text_cols), query, op="LIKE")
        where_parts.append(w)
        where_params.extend(wp)
        score_sql = sc
        score_params = sp

    where_sql = " AND ".join(where_parts)
    sql = (
        f"SELECT id, {text_expr} AS _text, {created_col} AS _created, {title_select}, "
        f"{score_sql} AS _score "
        f"FROM {table} WHERE {where_sql} "
        f"ORDER BY _score DESC LIMIT ?"
    )
    params = tuple(score_params + where_params + [pool])
    try:
        async with db.execute(sql, params) as cur:
            rows = await cur.fetchall()
    except Exception:  # noqa: BLE001 - one source type's failure must not break the rest
        _log.warning("hybrid_candidate_retrieval: lexical query failed for %s", stype, exc_info=True)
        return []
    out: list[dict[str, Any]] = []
    for r in rows:  # type: ignore[assignment]
        d = _row_to_dict(r)  # type: ignore[misc]
        try:
            score = float(d.get("_score") or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        out.append({
            "id": d.get("id"),
            "source_type": stype,
            "title": d.get("_title"),
            "text": d.get("_text") or "",
            "freshness": d.get("_created"),
            "lexical_score": score,
        })
    return out


async def _hybrid_fetch_exact(
    db: Any, project_id: str, rid: str, stype: str, spec: dict[str, Any]
) -> dict[str, Any] | None:
    """Fetch one row by exact id — bypasses lexical/semantic scoring entirely.

    Used for ``exact_ids`` (e.g. a resolved sprint-item-pointer target): the
    row is included regardless of whether it would have scored as a lexical
    or semantic candidate. Returns ``None`` when the id doesn't exist under
    ``project_id`` for this source type, or on any query error.
    """
    table = spec["table"]
    text_cols = spec["text_cols"]
    title_col = spec["title_col"]
    created_col = spec["created_col"]
    text_expr = " || ' ' || ".join(f"coalesce({c}, '')" for c in text_cols)
    title_select = f"{title_col} AS _title" if title_col else "NULL AS _title"
    sql = (
        f"SELECT id, {text_expr} AS _text, {created_col} AS _created, {title_select} "
        f"FROM {table} WHERE project_id = ? AND id = ?"
    )
    try:
        async with db.execute(sql, (project_id, rid)) as cur:
            row = await cur.fetchone()
    except Exception:  # noqa: BLE001
        return None
    if row is None:
        return None
    d = _row_to_dict(row)  # type: ignore[misc]
    return {
        "id": d.get("id"),
        "source_type": stype,
        "title": d.get("_title"),
        "text": d.get("_text") or "",
        "freshness": d.get("_created"),
    }


async def hybrid_candidate_retrieval(
    db: Any,
    project_id: str,
    query: str,
    *,
    source_types: "list[str] | tuple[str, ...] | None" = None,
    version: str | None = None,
    status: "str | list[str] | None" = None,
    visibility: str | None = None,
    limit: int = 10,
    candidate_pool: int = 50,
    exact_ids: "list[str] | set[str] | None" = None,
    allow_semantic: bool = True,
) -> dict[str, Any]:
    """Shared hybrid candidate-retrieval path (2204ce80) for handoffs and
    other planning-record consumers.

    Pipeline — EXACT filters always run first, before any lexical/semantic
    step ever touches a row:

    1. **Exact filters.** ``project_id`` (always — this module scopes every
       query by project, which is also the effective tenant boundary: a
       project belongs to exactly one tenant and ids are uuid4, so no
       separate tenant filter is needed on top of it). ``version`` — exact
       match, ``sprint_item`` only (a no-op for the other source types, which
       have no version column). ``status`` — exact match against one or more
       values; when omitted, ``decision`` rows still default to
       ``status='active'`` (matching every other read path in this module),
       and every other source type is unfiltered by status. ``visibility`` —
       only ``None``/``"project"`` is supported today (every wired source
       table is inherently project-scoped); any other value returns zero
       candidates rather than silently ignoring the filter — workspace-level
       visibility is a documented follow-up (see the module docstring above
       ``hybrid_candidate_retrieval`` in the sprint-item notes).
    2. **Lexical candidate discovery** over the exactly-filtered rows only:
       pg_trgm ``similarity`` + tsvector FTS on Postgres, the OR-ranked
       substring match on SQLite — the same primitives ``search_all`` already
       uses — bounded to ``candidate_pool`` rows total across the requested
       ``source_types``.
    3. **Optional bounded semantic rerank.** When ``allow_semantic`` and
       :func:`meridian.semantic_search.is_available`, the ALREADY-bounded
       lexical pool (never the whole corpus) is cosine-ranked via
       ``semantic_search.rank`` — local Model2Vec only, no paid LLM call.
       Unlike ``search_all``'s escalation, semantic here can only RE-SCORE a
       row the lexical pass already found; it never adds a candidate lexical
       search missed.
    4. **Fuse.** ``fused_score = 0.6 * lexical_norm + 0.4 * semantic`` when a
       semantic score exists for that row, else ``fused_score = lexical_norm``
       (``lexical_norm`` = the row's lexical score divided by the pool's max).

    ``exact_ids`` (e.g. a resolved ``sprint_item_pointers`` target, or any id
    a caller already knows is the right answer) are ALWAYS included, pinned
    ahead of every lexical/semantic row, ``provenance="exact"``,
    ``fused_score=1.0`` — their identity and rank are NEVER touched by the
    semantic step. Semantic similarity can add a nudge to already-discovered
    lexical rows; it can never create or replace an executable pointer.

    Returns ``{"query", "source_types", "filters", "semantic_used",
    "candidates"}`` where each candidate is ``{id, source_type, title,
    snippet, lexical_score, semantic_score, fused_score, freshness,
    provenance}``. ``provenance`` is one of ``"exact"`` / ``"lexical"`` /
    ``"lexical+semantic"``. IDs are the underlying tables' own stable ids
    (never synthesized), so a candidate's id is always a valid, directly
    resolvable reference back to its source row.
    """
    types = tuple(source_types) if source_types else _HYBRID_DEFAULT_SOURCE_TYPES
    types = tuple(t for t in types if t in _HYBRID_SOURCE_SPECS)
    filters = {
        "project_id": project_id, "version": version,
        "status": status, "visibility": visibility,
    }
    result: dict[str, Any] = {
        "query": query, "source_types": list(types), "filters": filters,
        "semantic_used": False, "candidates": [],
    }
    if not types or not query or not query.strip():
        return result
    # visibility (2204ce80): only project-scoped records are wired into the
    # corpus today — see the docstring above. Any other explicit value yields
    # zero candidates rather than silently matching everything.
    if visibility not in (None, "project"):
        return result

    is_pg = hasattr(db, "_pool")
    status_list: "list[str] | None"
    if status is None:
        status_list = None
    elif isinstance(status, str):
        status_list = [status]
    else:
        status_list = list(status)

    exact_id_set = {str(i) for i in exact_ids} if exact_ids else set()

    per_type_pool = max(1, candidate_pool // max(1, len(types)))
    pooled: list[dict[str, Any]] = []
    for stype in types:
        spec = _HYBRID_SOURCE_SPECS[stype]
        rows = await _hybrid_lexical_candidates(
            db, project_id, query, stype, spec,
            version=version, status_list=status_list,
            is_pg=is_pg, pool=per_type_pool,
        )
        pooled.extend(rows)

    pooled_ids = {(r["source_type"], str(r["id"])) for r in pooled}
    exact_rows: list[dict[str, Any]] = []
    for rid in exact_id_set:
        for stype in types:
            if (stype, rid) in pooled_ids:
                continue
            row = await _hybrid_fetch_exact(db, project_id, rid, stype, _HYBRID_SOURCE_SPECS[stype])
            if row is not None:
                exact_rows.append(row)
                pooled_ids.add((stype, rid))
                break  # ids are unique per-table; stop scanning other types once found

    # Optional bounded semantic rerank — ONLY over the lexical pool. exact_rows
    # never enter this step (see the "never let semantic touch a pointer" contract).
    semantic_scores: dict[tuple[str, str], float] = {}
    if allow_semantic and pooled:
        from meridian import semantic_search
        if semantic_search.is_available():
            cand_pairs = [(f"{r['source_type']}:{r['id']}", r["text"]) for r in pooled]
            ranked = semantic_search.rank(query, cand_pairs)
            if ranked:
                result["semantic_used"] = True
            for combo_id, score in ranked:
                stype, _, rid = combo_id.partition(":")
                semantic_scores[(stype, rid)] = score

    max_lexical = max((r["lexical_score"] for r in pooled), default=0.0) or 1.0
    scored: list[dict[str, Any]] = []
    for r in pooled:
        key = (r["source_type"], str(r["id"]))
        lex_norm = (r["lexical_score"] / max_lexical) if max_lexical else 0.0
        sem = semantic_scores.get(key)
        if sem is not None:
            fused = _HYBRID_LEXICAL_WEIGHT * lex_norm + _HYBRID_SEMANTIC_WEIGHT * sem
            provenance = "lexical+semantic"
        else:
            fused = lex_norm
            provenance = "lexical"
        scored.append({
            "id": r["id"],
            "source_type": r["source_type"],
            "title": r.get("title"),
            "snippet": _search_snippet(r.get("text"), query),
            "lexical_score": round(r["lexical_score"], 4),
            "semantic_score": (round(sem, 4) if sem is not None else None),
            "fused_score": round(fused, 4),
            "freshness": r.get("freshness"),
            "provenance": provenance,
        })
    scored.sort(key=lambda c: c["fused_score"], reverse=True)

    candidates: list[dict[str, Any]] = []
    for r in exact_rows:
        candidates.append({
            "id": r["id"],
            "source_type": r["source_type"],
            "title": r.get("title"),
            "snippet": _search_snippet(r.get("text"), query),
            "lexical_score": None,
            "semantic_score": None,
            "fused_score": 1.0,
            "freshness": r.get("freshness"),
            "provenance": "exact",
        })
    candidates.extend(scored[:limit])
    result["candidates"] = candidates
    return result


async def search_all(
    db: Any,
    project_id: str,
    query: str,
    limit: int = 10,
    expand: bool = False,
) -> dict[str, Any]:
    """Universal search across task_log, project_notes, sprint_items, and decisions_pinned.

    Matches both header fields (title) and body text:
      - task_log.description
      - project_notes.title + project_notes.body
      - decisions_pinned.title + decisions_pinned.body
      - sprint_items.title + sprint_items.notes

    SQLite (25155e91): keyword match — a row matches if ANY whitespace-separated
    query term appears (as a substring) in one of its text fields, ranked by how
    many terms matched (see _multiword_or_ranked_clause). A long multi-word
    natural-language query degrades gracefully to the most-relevant rows instead
    of returning zero the moment one rare token is absent.
    Postgres (82e0b887, 25155e91): on-the-fly tsvector full-text search via
    websearch_to_tsquery, ordered by ts_rank. The query terms are OR-combined
    (a | b | c) so any term can match, with ts_rank surfacing the best rows
    first. Stemming/word-form tolerance means "authenticating users" matches
    "authentication for the user" — which the SQLite substring path cannot. Zero
    schema / zero index (evaluated per query).

    Returns grouped results: {tasks, notes, decisions, sprint_items}.
    Each item includes a ``match_type`` key for the source table and a
    ``snippet`` key — a short window of the matching body text centered on the
    query term (empty string when no body field matched, e.g. a title-only
    match).

    9d8e858c — ``sprint_items`` default-collapse (``expand=False``, the
    default) any cluster sharing a ``parent_id`` or ``item_group`` (2+ items)
    into one summary row via :func:`collapse_sprint_item_clusters`, mirroring
    ``get_sprint_items``/``get_planning_brief``. Pass ``expand=True`` for the
    full ungrouped match list (pre-9d8e858c behavior).
    """
    is_pg = hasattr(db, "_pool")
    op = "ILIKE" if is_pg else "LIKE"

    async def _search(sql: str, params: tuple) -> list[dict[str, Any]]:
        async with db.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [_row_to_dict(r) for r in rows if r is not None]  # type: ignore[misc]

    if is_pg:
        # 82e0b887 — Postgres full-text search. The keyword AND-of-substrings
        # path (below, SQLite) can't stem or cross word forms: querying
        # "authenticating users" misses a note reading "authentication for the
        # user". On PG we build an on-the-fly tsvector over each content type's
        # text field(s) and match with websearch_to_tsquery (which tolerates
        # arbitrary punctuation/user input without raising), then rank by
        # ts_rank so the most relevant rows surface first. Zero schema / zero
        # index — the expression is evaluated per query. 'english' regconfig
        # matches the pg_trgm/English assumption already used by search_tasks.
        #
        # Param order per type is: <tsvector-query>, <existing filters incl.
        # project_id>, <ts_rank-query>, <limit>. The predicate's ? comes first
        # in the SQL, the ORDER BY ts_rank ? comes last before LIMIT — the
        # params tuple below mirrors that exactly.
        def _tsv(*cols: str) -> str:
            expr = " || ' ' || ".join(f"coalesce({c},'')" for c in cols)
            return f"to_tsvector('english', {expr})"

        tv_task = _tsv("description")
        tv_note = _tsv("title", "body")
        tv_dec = _tsv("title", "body")
        tv_sprint = _tsv("title", "notes")

        tasks_sql = (
            "SELECT id, description, status, created_at, 'task' AS match_type "
            "FROM task_log "
            f"WHERE project_id = ? AND {tv_task} @@ websearch_to_tsquery('english', ?) "
            f"ORDER BY ts_rank({tv_task}, websearch_to_tsquery('english', ?)) DESC, "
            "created_at DESC LIMIT ?"
        )
        notes_sql = (
            "SELECT id, title, body, tags, created_at, 'note' AS match_type "
            "FROM project_notes "
            f"WHERE project_id = ? AND {tv_note} @@ websearch_to_tsquery('english', ?) "
            f"ORDER BY ts_rank({tv_note}, websearch_to_tsquery('english', ?)) DESC, "
            "created_at DESC LIMIT ?"
        )
        decisions_sql = (
            "SELECT id, title, body, category, status, created_at, 'decision' AS match_type "
            "FROM decisions_pinned "
            "WHERE project_id = ? AND status = 'active' "
            f"AND {tv_dec} @@ websearch_to_tsquery('english', ?) "
            f"ORDER BY ts_rank({tv_dec}, websearch_to_tsquery('english', ?)) DESC, "
            "created_at DESC LIMIT ?"
        )
        sprint_sql = (
            "SELECT id, title, notes, version, status, parent_id, item_group, "
            "added_at AS created_at, 'sprint_item' AS match_type "
            "FROM sprint_items "
            f"WHERE project_id = ? AND {tv_sprint} @@ websearch_to_tsquery('english', ?) "
            f"ORDER BY ts_rank({tv_sprint}, websearch_to_tsquery('english', ?)) DESC, "
            "added_at DESC LIMIT ?"
        )

        # 25155e91 — OR the query terms instead of ANDing them. Plain
        # websearch_to_tsquery('a b c') builds 'a & b & c', so one rare/absent
        # token in a long natural-language query ("... BFS x=768 x=1511") zeroed
        # the whole result. websearch reads the bare word 'or' as the OR
        # operator, so feeding it "a or b or c" builds 'a | b | c' — match ANY
        # term. ts_rank still orders by how many/how well terms matched, so the
        # result degrades gracefully to the most-relevant rows rather than to
        # nothing. A single-term query is passed through unchanged (still
        # stemmed). The SQL is untouched — only the bound tsquery source changes.
        tsq = _or_tsquery_source(query)
        tasks = await _search(tasks_sql, (project_id, tsq, tsq, limit))
        notes = await _search(notes_sql, (project_id, tsq, tsq, limit))
        decisions = await _search(decisions_sql, (project_id, tsq, tsq, limit))
        sprint_items = await _search(sprint_sql, (project_id, tsq, tsq, limit))

        # 56cd8712 — SAFE-BY-DEFAULT, OPT-IN semantic escalation. When keyword /
        # tsvector search genuinely found nothing good, and semantic search is
        # enabled+importable+not-tripped, embed the query and rank the project's
        # candidate corpus, merging any above-cosine-floor hits (keyword first,
        # deduped). This is a NO-OP unless MERIDIAN_SEMANTIC_ENABLED is truthy —
        # is_available() is False by default so the gate never fires on prod.
        tasks, notes, decisions, sprint_items = await _maybe_semantic_escalate(
            db, project_id, query, limit,
            tasks, notes, decisions, sprint_items,
        )
    else:
        # 25155e91 — SQLite keyword path: OR the query terms and rank by how many
        # matched, instead of ANDing them. The old AND-of-all-terms
        # (_multiword_match_clause) meant one rare/absent token in a long
        # natural-language query ("... single-path BFS x=768 x=1511") zeroed the
        # whole result even when several terms clearly matched a row. Now a row
        # matches if ANY term appears (OR across terms, OR across the row's
        # fields), and a _match_score column counts matched terms so the most
        # complete matches sort first — graceful degradation to relevant results
        # rather than to nothing. A single-term query is identical to the old
        # contiguous-substring match; a wholly unrelated query still matches no
        # rows. created_at is the tiebreak within an equal score.
        w_task, sc_task, wp_task, sp_task = _multiword_or_ranked_clause(
            ["description"], query, op=op)
        w_note, sc_note, wp_note, sp_note = _multiword_or_ranked_clause(
            ["title", "body"], query, op=op)
        w_dec, sc_dec, wp_dec, sp_dec = _multiword_or_ranked_clause(
            ["title", "body"], query, op=op)
        w_sprint, sc_sprint, wp_sprint, sp_sprint = _multiword_or_ranked_clause(
            ["title", "notes"], query, op=op)

        tasks_sql = (
            f"SELECT id, description, status, created_at, 'task' AS match_type, {sc_task} AS _match_score "
            "FROM task_log "
            f"WHERE project_id = ? AND {w_task} "
            "ORDER BY _match_score DESC, created_at DESC LIMIT ?"
        )
        notes_sql = (
            f"SELECT id, title, body, tags, created_at, 'note' AS match_type, {sc_note} AS _match_score "
            "FROM project_notes "
            f"WHERE project_id = ? AND {w_note} "
            "ORDER BY _match_score DESC, created_at DESC LIMIT ?"
        )
        decisions_sql = (
            f"SELECT id, title, body, category, status, created_at, 'decision' AS match_type, {sc_dec} AS _match_score "
            "FROM decisions_pinned "
            f"WHERE project_id = ? AND status = 'active' AND {w_dec} "
            "ORDER BY _match_score DESC, created_at DESC LIMIT ?"
        )
        sprint_sql = (
            f"SELECT id, title, notes, version, status, parent_id, item_group, "
            f"added_at AS created_at, 'sprint_item' AS match_type, {sc_sprint} AS _match_score "
            "FROM sprint_items "
            f"WHERE project_id = ? AND {w_sprint} "
            "ORDER BY _match_score DESC, added_at DESC LIMIT ?"
        )

        # score params bind first (SELECT list), then project_id + WHERE params.
        tasks = await _search(tasks_sql, (*sp_task, project_id, *wp_task, limit))
        notes = await _search(notes_sql, (*sp_note, project_id, *wp_note, limit))
        decisions = await _search(decisions_sql, (*sp_dec, project_id, *wp_dec, limit))
        sprint_items = await _search(sprint_sql, (*sp_sprint, project_id, *wp_sprint, limit))

        # _match_score is an internal ranking column; drop it from returned rows
        # so the result shape is identical to the Postgres path.
        for _grp in (tasks, notes, decisions, sprint_items):
            for _row in _grp:
                _row.pop("_match_score", None)

    # Attach a body-text snippet for each result so the dashboard search bar can
    # surface matching context. The body field name differs per content type.
    for t in tasks:
        t["snippet"] = _search_snippet(t.get("description"), query)
    for n in notes:
        n["snippet"] = _search_snippet(n.get("body"), query)
    for d in decisions:
        d["snippet"] = _search_snippet(d.get("body"), query)
    for s in sprint_items:
        s["snippet"] = _search_snippet(s.get("notes"), query)

    # 9d8e858c — default-collapse sprint_items sharing a parent_id/item_group
    # (2+ items) into one summary row each; expand=True keeps every match.
    # Applied last (after snippet attachment) so a pass-through item still
    # carries its snippet.
    sprint_items = collapse_sprint_item_clusters(sprint_items, expand=expand)

    return {
        "query": query,
        "tasks": tasks,
        "notes": notes,
        "decisions": decisions,
        "sprint_items": sprint_items,
        "total": len(tasks) + len(notes) + len(decisions) + len(sprint_items),
    }


# ---------------------------------------------------------------------------
# 0dc5a35d — planning_search: a ranked, scoped planning-search operation.
#
# search_all (above) is a COMPATIBILITY universal-search surface: LIKE/ILIKE
# on SQLite, additive tsvector on Postgres, no stable ranked-result contract,
# no source-type/status/version filters, no explainable scores, no
# pagination. search_synthesis calls the exact same retrieval and only adds
# optional LLM summarization on top — it does not fix any of that either.
# search_all's semantics are left completely untouched by this section; this
# is a SEPARATE operation with its own contract:
#
#   - query tokens AND quoted "..." phrases (honoured on every backend)
#   - project + version + status + source-type filters
#   - a deterministic per-result ranking score plus a human-readable
#     rank_explanation describing how it was computed
#   - source_type / source_id / title / bounded context snippet per result
#   - a stable tie-break order (score DESC, created_at DESC, source_type ASC,
#     source_id ASC) so pagination never repeats or drops a row
#   - an integer pagination cursor — an offset into the fully-ranked, deduped
#     result list, mirroring get_project_notes_page's cursor contract
#   - explicit backend + freshness metadata on every response
#
# Backend selection (deliberately zero persistent schema change — this
# item's touches_resources does not include migrations.py / pg_adapter.py,
# and any persisted index would need a guarded migration in both):
#
#   Postgres — on-the-fly to_tsvector / websearch_to_tsquery / ts_rank, the
#              same "zero schema / zero index, evaluated per query" pattern
#              search_all / _hybrid_lexical_candidates already use.
#   SQLite   — prefers a REAL FTS5 index: a TEMP (connection-local, never
#              written to the database file) virtual table is built fresh
#              for every call from the already project/version/status
#              scoped candidate rows, tokenized with 'porter unicode61'
#              (real stemming) and ranked with bm25(). TEMP tables are not
#              part of the persistent schema, so this needs no migration and
#              can never go stale — it is rebuilt from scratch every call.
#              When the sqlite3 build lacks the FTS5 module (some minimal
#              self-hosted builds), this degrades to a clearly-documented
#              fallback: the same project/version/status-scoped candidate
#              pool, ranked with a locally-computed Okapi BM25 pass (the
#              "parity FTS/BM25 implementation" for that case) instead of
#              plain substring matching. Both SQLite tiers are exercised by
#              tests (test_new_v25.py forces the fallback tier by
#              monkeypatching FTS5 unavailable).
#
# No LLM or embedding call anywhere in this path.
# ---------------------------------------------------------------------------

_PLANNING_SEARCH_POOL_CAP = 200  # per-source-type candidate pool cap (mirrors _SEMANTIC_CORPUS_CAP)
_PLANNING_BM25_K1 = 1.2
_PLANNING_BM25_B = 0.75
_PLANNING_FTS5_TEMP_TABLE = "_meridian_planning_search_fts5"
_PLANNING_WORD_RE = re.compile(r"[A-Za-z0-9]+")
# Best-effort suffix list for the SQLite BM25-fallback tier's naive stemmer —
# see _planning_naive_stem for why/when this is used.
_PLANNING_NAIVE_STEM_SUFFIXES = ("ing", "ion", "ed", "es", "s")

# table/column shape for each source type planning_search knows how to
# retrieve. ``status_col``/``version_col`` are None when the table has no
# such column (a status/version filter for that type is then a no-op).
# ``scope_col`` is "project_id" for every type except workspace_proposal,
# which is workspace(tenant)-scoped, not project-scoped (5c4dcc0f) — see
# _resolve the tenant via get_tenant_id_for_project below.
_PLANNING_SOURCE_SPECS: dict[str, dict[str, Any]] = {
    "task": {
        "table": "task_log", "id_col": "id", "text_cols": ("description",),
        "title_col": None, "body_col": "description", "created_col": "created_at",
        "status_col": "status", "version_col": None, "scope_col": "project_id",
    },
    "note": {
        "table": "project_notes", "id_col": "id", "text_cols": ("title", "body"),
        "title_col": "title", "body_col": "body", "created_col": "created_at",
        "status_col": None, "version_col": None, "scope_col": "project_id",
    },
    "decision": {
        "table": "decisions_pinned", "id_col": "id", "text_cols": ("title", "body"),
        "title_col": "title", "body_col": "body", "created_col": "created_at",
        "status_col": "status", "version_col": None, "scope_col": "project_id",
    },
    "sprint_item": {
        "table": "sprint_items", "id_col": "id", "text_cols": ("title", "notes"),
        "title_col": "title", "body_col": "notes", "created_col": "added_at",
        "status_col": "status", "version_col": "version", "scope_col": "project_id",
    },
    "workspace_proposal": {
        "table": "workspace_proposals", "id_col": "id", "text_cols": ("title", "body"),
        "title_col": "title", "body_col": "body", "created_col": "created_at",
        "status_col": "status", "version_col": None, "scope_col": "tenant_id",
    },
    "finding": {
        "table": "session_findings", "id_col": "id", "text_cols": ("title", "content"),
        "title_col": "title", "body_col": "content", "created_col": "created_at",
        "status_col": None, "version_col": None, "scope_col": "project_id",
    },
    # 9149e132 — typed, code-linked decision evidence (meridian.db.decision_evidence).
    # No title column (mirrors "task"/"finding": title is derived from the body's
    # first line via _planning_derive_title). status defaults to active-only, same
    # convention as "decision" — see the stype == "decision" branches below, both
    # extended to also cover "decision_evidence".
    "decision_evidence": {
        "table": "decision_evidence", "id_col": "id", "text_cols": ("evidence",),
        "title_col": None, "body_col": "evidence", "created_col": "created_at",
        "status_col": "status", "version_col": "version", "scope_col": "project_id",
    },
}
_PLANNING_SOURCE_TYPES: "tuple[str, ...]" = tuple(_PLANNING_SOURCE_SPECS.keys())


def _planning_resolve_source_types(source_types: "list[str] | None") -> "list[str]":
    """Validate/dedupe a requested source-type filter, preserving order.

    ``None`` means "every known type". An explicit list that resolves to no
    valid type (all unknown/typo'd) deliberately returns an EMPTY list — the
    filter itself is honoured (zero results), it is not silently ignored.
    """
    if source_types is None:
        return list(_PLANNING_SOURCE_TYPES)
    seen: list[str] = []
    for s in source_types:
        if s in _PLANNING_SOURCE_SPECS and s not in seen:
            seen.append(s)
    return seen


def _planning_normalize_status(status: "str | list[str] | None") -> "list[str] | None":
    """Normalize the ``status`` filter to ``None`` or a non-empty list[str]."""
    if status is None:
        return None
    if isinstance(status, str):
        return [status] if status else None
    normalized = [s for s in status if s]
    return normalized or None


def _planning_parse_query(query: str) -> "tuple[list[str], list[str]]":
    """Split a free-text query into bare terms and quoted "phrases".

    Quoted substrings are extracted as phrases (matched literally/
    contiguously); everything outside quotes is tokenized into terms the
    same way :func:`_search_terms` does. Used by every SQLite ranking tier.
    Postgres gets the raw query string instead and relies on
    websearch_to_tsquery's own native phrase syntax (see
    :func:`_planning_pg_tsquery_source`).
    """
    phrases = [p.strip() for p in re.findall(r'"([^"]+)"', query or "")]
    phrases = [p for p in phrases if p]
    remainder = re.sub(r'"[^"]*"', " ", query or "")
    terms = _search_terms(remainder)
    return terms, phrases


def _planning_pg_tsquery_source(query: str) -> str:
    """Build a websearch_to_tsquery source string for planning_search.

    Differs from :func:`_or_tsquery_source` (search_all's graceful-
    degradation helper) in one deliberate way: quoted ``"phrase"`` spans are
    preserved VERBATIM — websearch_to_tsquery's own parser turns a quoted
    span into a phrase (FOLLOWED BY) tsquery — instead of being split into
    individually OR'd words. Bare terms outside quotes are still OR'd
    together so a multi-word query degrades gracefully instead of requiring
    every bare term (websearch's default whitespace join is AND).
    """
    phrases = re.findall(r'"[^"]+"', query or "")
    remainder = re.sub(r'"[^"]+"', " ", query or "")
    bare = [
        t.lstrip("-") for t in remainder.split()
        if t.lstrip("-") and t.lstrip("-").lower() not in ("or", "and")
    ]
    parts = phrases + bare
    if len(parts) <= 1:
        return (query or "").strip() or (query or "")
    return " or ".join(parts)


def _planning_derive_title(body: str | None, max_len: int = 80) -> str:
    """Fallback title for source types with no (or a null) title column: the
    first line of the body text, truncated. Used for ``task``/``finding``."""
    if not body:
        return ""
    first_line = body.strip().splitlines()[0].strip()
    if len(first_line) <= max_len:
        return first_line
    return first_line[: max_len - 1].rstrip() + "…"


def _fts5_quote(term: str) -> str:
    """Quote a token/phrase for a safe, literal SQLite FTS5 MATCH clause."""
    return '"' + term.replace('"', '""') + '"'


def _planning_naive_stem(word: str) -> str:
    """Best-effort suffix-stripping stemmer for the SQLite BM25-fallback tier
    ONLY (used when the sqlite3 build lacks FTS5's real 'porter' tokenizer).

    This is not a real Porter stemmer — it strips at most one common English
    suffix, and only when the remaining stem is still >=3 chars, e.g.
    "authenticating"/"authentication" both reduce to "authenticat". That is
    enough to bridge the common verb/noun-form pairs planning_search's tests
    exercise. Postgres (websearch_to_tsquery) and SQLite-FTS5
    (tokenize='porter') both do real linguistic stemming and never call
    this function at all.
    """
    low = word.lower()
    for suf in _PLANNING_NAIVE_STEM_SUFFIXES:
        if low.endswith(suf) and len(low) - len(suf) >= 3:
            return low[: -len(suf)]
    return low


def _planning_tokenize_and_stem(text: str) -> "list[str]":
    """Word-tokenize ``text`` and naive-stem each token (fallback tier)."""
    return [_planning_naive_stem(w) for w in _PLANNING_WORD_RE.findall(text)]


def _planning_bm25_scores(docs: "list[str]", terms: "list[str]") -> "list[float]":
    """Okapi BM25 (k1=1.2, b=0.75) over an in-memory, already-scoped corpus.

    ``docs`` is a source type's bounded candidate pool (title+body text,
    lowercased); ``terms`` are the bare (non-phrase) query terms. Word-
    tokenized + naive-stemmed (see :func:`_planning_tokenize_and_stem`) so
    this tier still gets an approximate stemming match even without SQLite's
    FTS5 module. Returns one score per doc, in ``docs`` order; a term that
    matches nothing in the corpus (idf undefined/zero-signal) contributes 0.
    """
    n = len(docs)
    if n == 0 or not terms:
        return [0.0] * n
    stemmed_terms = [_planning_naive_stem(t) for t in terms]
    doc_tokens = [_planning_tokenize_and_stem(d) for d in docs]
    lengths = [len(toks) or 1 for toks in doc_tokens]
    avgdl = sum(lengths) / n
    scores = [0.0] * n
    for term in stemmed_terms:
        doc_freq = sum(1 for toks in doc_tokens if term in toks)
        if doc_freq == 0:
            continue
        idf = math.log(1 + (n - doc_freq + 0.5) / (doc_freq + 0.5))
        for i, toks in enumerate(doc_tokens):
            tf = toks.count(term)
            if tf == 0:
                continue
            denom = tf + _PLANNING_BM25_K1 * (
                1 - _PLANNING_BM25_B + _PLANNING_BM25_B * (lengths[i] / avgdl)
            )
            scores[i] += idf * (tf * (_PLANNING_BM25_K1 + 1)) / denom
    return scores


async def _sqlite_fts5_available(db: Any) -> bool:
    """Best-effort probe: can this SQLite build create an FTS5 virtual table?

    Cheap (in-memory TEMP table, dropped immediately) and safe to call once
    per planning_search invocation — SQLite compiles FTS5 in on most modern
    builds (confirmed available in this project's own pixi env), but some
    minimal self-hosted builds omit it, so this must never be assumed.
    """
    try:
        await db.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS temp._meridian_fts5_probe "
            "USING fts5(x)"
        )
        await db.execute("DROP TABLE IF EXISTS temp._meridian_fts5_probe")
        return True
    except Exception:  # noqa: BLE001 — any failure means "not available"
        return False


async def _planning_sqlite_fts5_rank(
    db: Any, rows: "list[dict[str, Any]]", terms: "list[str]", phrases: "list[str]",
) -> "list[tuple[dict[str, Any], float, str]]":
    """Rank ``rows`` with a fresh connection-local TEMP FTS5 index.

    TEMP tables live only on this connection (never written to the actual
    database file), so building one needs no schema migration — it is
    dropped again before returning, and rebuilt from scratch on every call,
    which is also why planning_search's freshness metadata always reports
    ``stale: False`` for this backend: there is no persisted index that
    could go stale in the first place.
    """
    match_terms = phrases + terms
    if not match_terms:
        return []
    try:
        await db.execute(f"DROP TABLE IF EXISTS temp.{_PLANNING_FTS5_TEMP_TABLE}")
        await db.execute(
            f"CREATE VIRTUAL TABLE temp.{_PLANNING_FTS5_TEMP_TABLE} USING fts5("
            "title, body, tokenize='porter unicode61')"
        )
    except Exception:  # noqa: BLE001 — defensive: _sqlite_fts5_available already checked this
        return _planning_sqlite_bm25_rank(rows, terms, phrases)
    try:
        for i, r in enumerate(rows):
            await db.execute(
                f"INSERT INTO {_PLANNING_FTS5_TEMP_TABLE}(rowid, title, body) VALUES (?, ?, ?)",
                (i, r.get("_title") or "", r.get("_body") or ""),
            )
        match_query = " OR ".join(_fts5_quote(t) for t in match_terms)
        async with db.execute(
            f"SELECT rowid, bm25({_PLANNING_FTS5_TEMP_TABLE}) AS _bm25 "
            f"FROM {_PLANNING_FTS5_TEMP_TABLE} "
            f"WHERE {_PLANNING_FTS5_TEMP_TABLE} MATCH ?",
            (match_query,),
        ) as cur:
            matches = await cur.fetchall()
    finally:
        await db.execute(f"DROP TABLE IF EXISTS temp.{_PLANNING_FTS5_TEMP_TABLE}")
    out: "list[tuple[dict[str, Any], float, str]]" = []
    for m in matches:  # type: ignore[assignment]
        d = _row_to_dict(m)  # type: ignore[arg-type]
        if d is None:
            continue
        idx = int(d["rowid"])
        raw_bm25 = float(d["_bm25"])
        score = -raw_bm25  # SQLite bm25(): more negative == more relevant
        explanation = (
            f"bm25={score:.6f} via SQLite FTS5 (tokenize='porter unicode61') "
            f"MATCH {match_query!r}"
        )
        out.append((rows[idx], score, explanation))
    out.sort(key=lambda t: -t[1])
    return out


def _planning_sqlite_bm25_rank(
    rows: "list[dict[str, Any]]", terms: "list[str]", phrases: "list[str]",
) -> "list[tuple[dict[str, Any], float, str]]":
    """Computed Okapi BM25 over the already-scoped candidate pool — the
    "parity FTS/BM25 implementation" used when the sqlite3 build lacks the
    FTS5 extension. Quoted phrases are a hard literal-substring filter (a row
    must contain every phrase to be a candidate at all); bare terms then
    drive BM25 ranking (with naive stemming — see
    :func:`_planning_bm25_scores`). A row matching neither any term nor any
    phrase is excluded, matching search_all's "wholly unrelated query still
    matches no rows" contract.
    """
    docs = [
        f"{(r.get('_title') or '')} {(r.get('_body') or '')}".strip() for r in rows
    ]
    lowered = [d.lower() for d in docs]
    keep_idx = [
        i for i, d in enumerate(lowered) if all(p.lower() in d for p in phrases)
    ]
    if not keep_idx:
        return []
    kept_docs = [lowered[i] for i in keep_idx]
    term_scores = _planning_bm25_scores(kept_docs, terms) if terms else [0.0] * len(kept_docs)
    out: "list[tuple[dict[str, Any], float, str]]" = []
    for local_i, orig_i in enumerate(keep_idx):
        term_score = term_scores[local_i]
        if terms and term_score <= 0.0 and not phrases:
            continue  # matched no term and no phrase — not a hit
        phrase_freq = sum(kept_docs[local_i].count(p.lower()) for p in phrases)
        score = term_score + phrase_freq
        explanation = (
            f"bm25={term_score:.6f} (python fallback, k1={_PLANNING_BM25_K1}, "
            f"b={_PLANNING_BM25_B}, naive-stemmed) over terms={terms!r}"
            + (f"; +{phrase_freq} literal phrase hit(s) of {phrases!r}" if phrases else "")
            + " — LIKE/substring candidate discovery, FTS5 unavailable"
        )
        out.append((rows[orig_i], score, explanation))
    out.sort(key=lambda t: -t[1])
    return out


async def _planning_pg_source_results(
    db: Any,
    scope_col: str,
    scope_value: str,
    spec: dict[str, Any],
    stype: str,
    query: str,
    version: str | None,
    status_list: "list[str] | None",
) -> "tuple[list[dict[str, Any]], bool]":
    """Postgres candidate + rank for one source type: on-the-fly tsvector /
    websearch_to_tsquery / ts_rank — the same zero-schema pattern search_all
    already uses. Returns ``(results, capped)``."""
    table = spec["table"]
    text_cols = spec["text_cols"]
    title_col = spec["title_col"]
    body_col = spec["body_col"]
    created_col = spec["created_col"]
    status_col = spec["status_col"]
    version_col = spec["version_col"]
    id_col = spec["id_col"]

    tv_expr = "to_tsvector('english', " + " || ' ' || ".join(
        f"coalesce({c}, '')" for c in text_cols
    ) + ")"
    title_select = f"{title_col} AS _title" if title_col else "NULL AS _title"
    body_select = f"{body_col} AS _body" if body_col else "NULL AS _body"
    status_select = f"{status_col} AS _status" if status_col else "NULL AS _status"
    version_select = f"{version_col} AS _version" if version_col else "NULL AS _version"

    where_parts = [f"{scope_col} = ?"]
    params: list[Any] = [scope_value]
    if version is not None and version_col is not None:
        where_parts.append(f"{version_col} = ?")
        params.append(version)
    if status_list is not None and status_col is not None:
        placeholders = ", ".join("?" for _ in status_list)
        where_parts.append(f"{status_col} IN ({placeholders})")
        params.extend(status_list)
    elif status_list is None and status_col is not None and stype in (
        "decision", "decision_evidence",
    ):
        # Preserve the convention every other read path in this module uses:
        # decisions (and, since 9149e132, decision_evidence links) default to
        # active-only unless a status is explicitly given — a superseded or
        # reversed evidence link is excluded by default, not just marked.
        where_parts.append("status = 'active'")

    tsq = _planning_pg_tsquery_source(query)
    where_parts.append(f"{tv_expr} @@ websearch_to_tsquery('english', ?)")
    params.append(tsq)
    where_sql = " AND ".join(where_parts)

    sql = (
        f"SELECT {id_col} AS _id, {title_select}, {body_select}, {status_select}, "
        f"{version_select}, {created_col} AS _created, "
        f"ts_rank({tv_expr}, websearch_to_tsquery('english', ?)) AS _score "
        f"FROM {table} WHERE {where_sql} "
        f"ORDER BY _score DESC, {created_col} DESC, {id_col} ASC LIMIT ?"
    )
    bound = [tsq, *params, _PLANNING_SEARCH_POOL_CAP + 1]
    async with db.execute(sql, bound) as cur:
        raw_rows = await cur.fetchall()
    rows = [_row_to_dict(r) for r in raw_rows if r is not None]  # type: ignore[misc]
    capped = len(rows) > _PLANNING_SEARCH_POOL_CAP
    rows = rows[:_PLANNING_SEARCH_POOL_CAP]

    results: list[dict[str, Any]] = []
    for r in rows:
        score = float(r.get("_score") or 0.0)
        body = r.get("_body")
        title = r.get("_title") or _planning_derive_title(body)
        results.append({
            "source_type": stype,
            "source_id": r.get("_id"),
            "title": title,
            "snippet": _search_snippet(body, query),
            "score": score,
            "rank_explanation": (
                f"ts_rank={score:.6f} via to_tsvector('english', ...) "
                f"@@ websearch_to_tsquery('english', {tsq!r})"
            ),
            "status": r.get("_status"),
            "version": r.get("_version"),
            "created_at": r.get("_created"),
        })
    return results, capped


async def _planning_sqlite_source_results(
    db: Any,
    scope_col: str,
    scope_value: str,
    spec: dict[str, Any],
    stype: str,
    terms: "list[str]",
    phrases: "list[str]",
    version: str | None,
    status_list: "list[str] | None",
    *,
    fts5_ok: bool,
) -> "tuple[list[dict[str, Any]], bool]":
    """SQLite candidate + rank for one source type.

    Always fetches a bounded, project/version/status-scoped candidate pool
    first (``ORDER BY created DESC LIMIT cap+1`` — no text filter yet), then
    ranks/filters that pool by the query: a fresh TEMP FTS5 index + bm25()
    when the sqlite3 build supports FTS5 (``fts5_ok``), else the locally-
    computed BM25 fallback (see :func:`_planning_sqlite_bm25_rank`).
    """
    table = spec["table"]
    id_col = spec["id_col"]
    title_col = spec["title_col"]
    body_col = spec["body_col"]
    created_col = spec["created_col"]
    status_col = spec["status_col"]
    version_col = spec["version_col"]

    where_parts = [f"{scope_col} = ?"]
    params: list[Any] = [scope_value]
    if version is not None and version_col is not None:
        where_parts.append(f"{version_col} = ?")
        params.append(version)
    if status_list is not None and status_col is not None:
        placeholders = ", ".join("?" for _ in status_list)
        where_parts.append(f"{status_col} IN ({placeholders})")
        params.extend(status_list)
    elif status_list is None and status_col is not None and stype in (
        "decision", "decision_evidence",
    ):
        # 9149e132 — mirrors the Postgres path above: decisions (and
        # decision_evidence links) default to active-only unless a status is
        # explicitly given, so a superseded/reversed link is excluded by
        # default, not just marked.
        where_parts.append("status = 'active'")
    where_sql = " AND ".join(where_parts)

    title_select = f"{title_col} AS _title" if title_col else "NULL AS _title"
    status_select = f"{status_col} AS _status" if status_col else "NULL AS _status"
    version_select = f"{version_col} AS _version" if version_col else "NULL AS _version"
    body_select = f"{body_col} AS _body" if body_col else "NULL AS _body"

    sql = (
        f"SELECT {id_col} AS _id, {title_select}, {body_select}, {status_select}, "
        f"{version_select}, {created_col} AS _created "
        f"FROM {table} WHERE {where_sql} "
        f"ORDER BY {created_col} DESC LIMIT ?"
    )
    async with db.execute(sql, (*params, _PLANNING_SEARCH_POOL_CAP + 1)) as cur:
        raw_rows = await cur.fetchall()
    rows = [_row_to_dict(r) for r in raw_rows if r is not None]  # type: ignore[misc]
    capped = len(rows) > _PLANNING_SEARCH_POOL_CAP
    rows = rows[:_PLANNING_SEARCH_POOL_CAP]
    if not rows:
        return [], capped

    if fts5_ok:
        scored = await _planning_sqlite_fts5_rank(db, rows, terms, phrases)
    else:
        scored = _planning_sqlite_bm25_rank(rows, terms, phrases)

    query_text = " ".join([*phrases, *terms])
    results: list[dict[str, Any]] = []
    for r, score, explanation in scored:
        body = r.get("_body")
        title = r.get("_title") or _planning_derive_title(body)
        results.append({
            "source_type": stype,
            "source_id": r.get("_id"),
            "title": title,
            "snippet": _search_snippet(body, query_text),
            "score": score,
            "rank_explanation": explanation,
            "status": r.get("_status"),
            "version": r.get("_version"),
            "created_at": r.get("_created"),
        })
    return results, capped


async def planning_search(
    db: Any,
    project_id: str,
    query: str,
    *,
    source_types: "list[str] | None" = None,
    version: str | None = None,
    status: "str | list[str] | None" = None,
    limit: int = 20,
    cursor: int = 0,
    rerank_semantic: bool = False,
) -> dict[str, Any]:
    """0dc5a35d — ranked, scoped planning search (v1).

    A separate operation from :func:`search_all` (never mutates its
    semantics — see the module comment above this section for the full
    rationale). Searches tasks, notes, decisions, sprint items, workspace
    proposals, and findings for ``project_id``, returning a stable, ranked,
    paginated, explainable result contract instead of search_all's grouped
    raw-row dump.

    Args:
        source_types: restrict to a subset of {task, note, decision,
            sprint_item, workspace_proposal, finding}; ``None`` = all. An
            explicit list containing only unknown values yields zero results
            (the filter is honoured, not silently dropped).
        version: exact match against sprint_items.version; a no-op for every
            other source type (they have no version column).
        status: exact match (single value or list) against each type's
            status column; a no-op for types with no status column (note,
            finding). ``decision`` still defaults to active-only when status
            is omitted, matching the convention every other read path in
            this module already uses (see _HYBRID_SOURCE_SPECS); every other
            type is unfiltered by status when omitted.
        limit: page size, clamped to 1..100.
        cursor: zero-based OFFSET into the fully-ranked result list (mirrors
            get_project_notes_page's cursor contract). Pass the previous
            response's ``next_cursor`` to fetch the next page.
        rerank_semantic: 9149e132 — OPTIONAL, OFF BY DEFAULT. When True (and
            :func:`meridian.semantic_search.is_available` — itself gated
            behind ``MERIDIAN_SEMANTIC_ENABLED`` + an importable model2vec,
            off by default), re-orders the ALREADY-RETRIEVED ``all_results``
            candidate set by a lexical/semantic FUSED score
            (:func:`meridian.semantic_search.score_confidence`'s existing
            0.6/0.4 blend). This is deliberately NOT the same shape as
            :func:`_maybe_semantic_escalate` (used by :func:`search_all`),
            which ESCALATES — adds NEW rows semantic search alone found when
            lexical search found nothing. Reranking here NEVER adds or drops
            a row: lexical retrieval alone decides the CANDIDATE SET (what
            can appear at all); semantic scoring only ever decides the
            ORDER of that already-fixed set, and every row's fused score
            always includes its own lexical component (rows here all came
            from lexical retrieval, so a "semantic-only" ranking of a row
            lexical search never found is structurally impossible). See
            ``freshness.reranked`` / ``freshness.rerank_backend`` on the
            response, and each reranked result's additive ``semantic`` field
            (semantic_score/fused_score/confident/reason). Unavailable or
            no-op semantic search silently leaves lexical ordering untouched
            — never an error.

    Returns a dict with ``query``, ``filters``, ``results`` (each carrying
    source_type/source_id/title/snippet/score/rank_explanation/status/
    version/created_at, plus an optional additive ``semantic`` breakdown when
    reranked), ``total_matched``, ``has_more``, ``next_cursor``, ``backend``,
    ``freshness`` (index_type/generated_at/stale/capped/capped_source_types/
    pool_cap/reranked/rerank_backend), and ``skipped_source_types`` (a
    type -> reason map for e.g. workspace_proposal when no tenant can be
    resolved for this project).

    No LLM call anywhere in this function. The only embedding call possible
    is the OPTIONAL, off-by-default ``rerank_semantic`` path above, and even
    then it never authorizes a mutation or expands what lexical search
    already found — see :mod:`meridian.db.decision_evidence`'s module
    docstring for the full safety contract this exists to uphold.
    """
    limit = max(1, min(int(limit or 20), 100))
    cursor = max(0, int(cursor or 0))
    types = _planning_resolve_source_types(source_types)
    status_list = _planning_normalize_status(status)
    is_pg = hasattr(db, "_pool")
    generated_at = _dt.datetime.now(_dt.timezone.utc).isoformat()

    terms, phrases = _planning_parse_query(query or "")
    has_query_content = bool(terms or phrases)

    sqlite_fts5_ok = False
    if not is_pg and has_query_content and types:
        sqlite_fts5_ok = await _sqlite_fts5_available(db)

    all_results: list[dict[str, Any]] = []
    capped_types: list[str] = []
    skipped_types: dict[str, str] = {}
    tenant_cache: dict[str, Any] = {}

    if has_query_content:
        for stype in types:
            spec = _PLANNING_SOURCE_SPECS[stype]
            scope_col = spec["scope_col"]
            if scope_col == "tenant_id":
                if "id" not in tenant_cache:
                    tenant_cache["id"] = await get_tenant_id_for_project(db, project_id)
                scope_value = tenant_cache["id"]
                if scope_value is None:
                    skipped_types[stype] = (
                        "no tenant resolved for this project — workspace_proposals "
                        "are workspace(tenant)-scoped, not project-scoped; self-hosted "
                        "installs or projects with no creator_human_id have nothing to "
                        "resolve, so this source type is skipped rather than guessed"
                    )
                    continue
            else:
                scope_value = project_id

            if is_pg:
                rows, capped = await _planning_pg_source_results(
                    db, scope_col, scope_value, spec, stype, query or "",
                    version, status_list,
                )
            else:
                rows, capped = await _planning_sqlite_source_results(
                    db, scope_col, scope_value, spec, stype, terms, phrases,
                    version, status_list, fts5_ok=sqlite_fts5_ok,
                )
            if capped:
                capped_types.append(stype)
            all_results.extend(rows)

    # Deterministic, stable tie-break: score DESC, created_at DESC,
    # source_type ASC, source_id ASC. Python's sort() is stable, so applying
    # ascending sorts in REVERSE priority order and finishing with the score
    # sort (also reverse=True) produces the full combined ordering.
    all_results.sort(key=lambda r: str(r["source_id"]))
    all_results.sort(key=lambda r: r["source_type"])
    all_results.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    all_results.sort(key=lambda r: r["score"], reverse=True)

    # 9149e132 — OPTIONAL, READ-ONLY semantic rerank. See the `rerank_semantic`
    # docstring above for the full contract; in short: this can only re-sort
    # `all_results` (already fully assembled by lexical retrieval above), it
    # can never add a row lexical search did not already find, and every
    # fused score always includes its own lexical component (never
    # semantic-only), so it can never become the sole ranking signal.
    reranked = False
    rerank_backend: str | None = None
    if rerank_semantic and all_results:
        from meridian import semantic_search  # noqa: PLC0415 — lazy, mirrors _maybe_semantic_escalate

        if semantic_search.is_available():
            raw_scores = [r["score"] for r in all_results]
            lo, hi = min(raw_scores), max(raw_scores)
            span = (hi - lo) or 1.0
            # Normalized to [0, 1] purely as fusion/fallback-ordering input —
            # the authoritative `score` field on each result is never
            # overwritten by this block.
            lexical_scores = {
                f"{r['source_type']}:{r['source_id']}": (r["score"] - lo) / span
                for r in all_results
            }
            candidates = [
                (f"{r['source_type']}:{r['source_id']}", f"{r['title']} {r['snippet']}")
                for r in all_results
            ]
            matches = semantic_search.rank_confident(
                query, candidates, lexical_scores=lexical_scores,
            )
            if matches:
                fused_by_id = {m.id: m for m in matches}

                def _rerank_key(r: "dict[str, Any]") -> float:
                    rid = f"{r['source_type']}:{r['source_id']}"
                    m = fused_by_id.get(rid)
                    # A row with no semantic verdict (below the cosine floor,
                    # or embedding failed) keeps its normalized lexical score
                    # — it is never dropped, only possibly out-ranked by rows
                    # whose fused score is higher.
                    return m.fused_score if m is not None else lexical_scores[rid]

                all_results.sort(key=_rerank_key, reverse=True)  # stable: ties keep lexical order
                reranked = True
                rerank_backend = semantic_search.model_name()
                for r in all_results:
                    m = fused_by_id.get(f"{r['source_type']}:{r['source_id']}")
                    if m is not None:
                        r["semantic"] = {
                            "semantic_score": m.semantic_score,
                            "fused_score": m.fused_score,
                            "confident": m.confident,
                            "reason": m.reason,
                        }

    total_matched = len(all_results)
    page = all_results[cursor: cursor + limit]
    has_more = (cursor + limit) < total_matched
    next_cursor = cursor + len(page) if has_more else None

    if is_pg:
        backend = "postgres_tsvector_ts_rank"
        index_type = "on_the_fly_tsvector"
    elif sqlite_fts5_ok:
        backend = "sqlite_fts5_bm25"
        index_type = "ephemeral_fts5_temp_table"
    else:
        backend = "sqlite_bm25_like_fallback"
        index_type = "computed_bm25_over_candidate_pool"

    return {
        "query": query,
        "filters": {
            "project_id": project_id,
            "source_types": types,
            "version": version,
            "status": status_list,
        },
        "results": [
            {
                "source_type": r["source_type"],
                "source_id": r["source_id"],
                "title": r["title"],
                "snippet": r["snippet"],
                "score": round(float(r["score"]), 6),
                "rank_explanation": r["rank_explanation"],
                "status": r.get("status"),
                "version": r.get("version"),
                "created_at": r.get("created_at"),
                # 9149e132 — additive, only present when rerank_semantic=True
                # AND semantic search was available AND this row cleared the
                # cosine floor. Advisory ranking metadata only — never used
                # to authorize a mutation or to decide what to retrieve.
                **({"semantic": r["semantic"]} if "semantic" in r else {}),
            }
            for r in page
        ],
        "total_matched": total_matched,
        "has_more": has_more,
        "next_cursor": next_cursor,
        "backend": backend,
        "freshness": {
            "index_type": index_type,
            "generated_at": generated_at,
            "stale": False,
            "capped": bool(capped_types),
            "capped_source_types": sorted(capped_types),
            "pool_cap": _PLANNING_SEARCH_POOL_CAP,
            "reranked": reranked,
            "rerank_backend": rerank_backend,
        },
        "skipped_source_types": skipped_types,
    }


# ---------------------------------------------------------------------------
# Distributed task locking (v0.3.3)
# ---------------------------------------------------------------------------


async def claim_task(
    db: aiosqlite.Connection, task_id: str, session_id: str
) -> dict[str, Any] | None:
    """Atomically claim a pending task for ``session_id``.

    Returns the freshly-claimed task row, or ``None`` if the task is
    already claimed / not pending / does not exist. The single UPDATE
    statement encodes the "first writer wins" race: SQLite serialises
    writes so even concurrent claims from two parallel workers will
    only see one of them flip ``claimed_by`` from NULL.
    """
    cursor = await db.execute(
        "UPDATE task_log SET claimed_by = ?, claimed_at = datetime('now'), "
        "status = 'in_progress' "
        "WHERE id = ? AND claimed_by IS NULL AND status = 'pending'",
        (session_id, task_id),
    )
    await db.commit()
    if cursor.rowcount == 0:
        return None
    updated = await get_task(db, task_id)
    if updated is not None:
        _publish_task("task_updated", updated)
    return updated


# sprint-item task helpers — moved to sprint_items.py, re-exported via *

async def release_stale_task_claims(
    db: aiosqlite.Connection,
    project_id: str,
    *,
    exclude_session_id: str | None = None,
    max_age_hours: int = 2,
) -> int:
    """Release stale claimed tasks older than ``max_age_hours`` for a project."""
    from datetime import datetime, timedelta, timezone

    cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    ).strftime("%Y-%m-%d %H:%M:%S")
    query = (
        "SELECT id, sprint_item_id FROM task_log "
        "WHERE project_id = ? AND status = 'in_progress' "
        "AND claimed_by IS NOT NULL AND claimed_at IS NOT NULL "
        "AND claimed_at < ?"
    )
    params: list[Any] = [project_id, cutoff]
    if exclude_session_id is not None:
        query += " AND claimed_by != ?"
        params.append(exclude_session_id)
    async with db.execute(query, tuple(params)) as cur:
        rows = await cur.fetchall()
    task_ids = [row["id"] for row in rows]
    if not task_ids:
        return 0
    linked_item_ids = [row["sprint_item_id"] for row in rows if row["sprint_item_id"]]
    placeholders = ", ".join("?" for _ in task_ids)
    await db.execute(
        f"UPDATE task_log SET status = 'pending', claimed_by = NULL, claimed_at = NULL "
        f"WHERE id IN ({placeholders})",
        tuple(task_ids),
    )
    if linked_item_ids:
        placeholders = ", ".join("?" for _ in linked_item_ids)
        await db.execute(
            f"UPDATE sprint_items SET status = 'pending', completed_at = NULL "
            f"WHERE id IN ({placeholders}) AND status = 'in_progress'",
            tuple(linked_item_ids),
        )
    await db.commit()
    return len(task_ids)


async def release_task(
    db: aiosqlite.Connection, task_id: str, session_id: str
) -> bool:
    """Release a claim previously taken by ``session_id``.

    Returns True if a claim was released; False if the task wasn't held
    by that session (someone else's claim is left untouched).
    """
    cursor = await db.execute(
        "UPDATE task_log SET status = 'pending', claimed_by = NULL, claimed_at = NULL "
        "WHERE id = ? AND claimed_by = ?",
        (task_id, session_id),
    )
    await db.commit()
    if cursor.rowcount == 0:
        return False
    updated = await get_task(db, task_id)
    if updated is not None and updated.get("sprint_item_id"):
        await db.execute(
            "UPDATE sprint_items SET status = 'pending', completed_at = NULL "
            "WHERE id = ? AND status = 'in_progress'",
            (updated["sprint_item_id"],),
        )
        await db.commit()
        updated = await get_task(db, task_id)
    if updated is not None:
        _publish_task("task_updated", updated)
    return True


async def get_claimable_tasks(
    db: aiosqlite.Connection, project_id: str, limit: int = 20
) -> list[dict[str, Any]]:
    """Return unclaimed pending tasks, newest first.

    Worker pattern: poll this, pick a row, call :func:`claim_task` —
    if the claim returns None another worker beat you to it, try the
    next row.
    """
    async with db.execute(
        "SELECT * FROM task_log WHERE project_id = ? "
        "AND status = 'pending' AND claimed_by IS NULL "
        "ORDER BY created_at ASC, rowid ASC LIMIT ?",
        (project_id, limit),
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows]  # type: ignore[misc]


async def expire_idle_sessions(
    db: aiosqlite.Connection, max_age_minutes: int = 30
) -> dict[str, Any]:
    """Mark sessions idle when their last_seen is older than *max_age_minutes*.

    Returns ``{"count": n, "project_ids": [...]}`` where ``project_ids`` is
    the list of distinct projects that had at least one session expire. The
    caller can use this to trigger handoff generation for affected projects
    (v0.4.5). Only 'active' sessions are considered; 'idle' and 'closed'
    sessions are left untouched.
    """
    async with db.execute(
        "SELECT DISTINCT project_id FROM sessions "
        "WHERE status = 'active' "
        "AND last_seen < datetime('now', ? || ' minutes')",
        (f"-{max_age_minutes}",),
    ) as cur:
        rows = await cur.fetchall()
    affected_project_ids: list[str] = [row["project_id"] for row in rows]

    cursor = await db.execute(
        "UPDATE sessions SET status = 'idle' "
        "WHERE status = 'active' "
        "AND last_seen < datetime('now', ? || ' minutes')",
        (f"-{max_age_minutes}",),
    )
    await db.commit()
    return {"count": cursor.rowcount, "project_ids": affected_project_ids}


async def expire_inactive_sessions(
    db: aiosqlite.Connection, max_age_hours: int = 24
) -> dict[str, Any]:
    """Archive sessions whose ``last_seen`` is older than *max_age_hours*.

    A session that hasn't checked in for a day is treated as dead: it is moved
    to 'archived' (so it drops out of the dashboard's active list) and any
    'in_progress' tasks it held — plus their linked sprint_items — are released
    back to 'pending' so another worker can claim them. Runs globally across all
    projects, unlike the per-project :func:`archive_stale_sessions`. Only
    'active' and 'idle' rows are touched. Returns ``{"count", "project_ids"}``.
    """
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    async with db.execute(
        "SELECT DISTINCT project_id FROM sessions "
        "WHERE status IN ('active', 'idle') AND last_seen < ?",
        (cutoff,),
    ) as cur:
        affected_project_ids = [row["project_id"] for row in await cur.fetchall()]

    async with db.execute(
        "SELECT id, sprint_item_id, claimed_by FROM task_log "
        "WHERE claimed_by IN ("
        "  SELECT id FROM sessions WHERE status IN ('active', 'idle') AND last_seen < ?"
        ") AND status = 'in_progress'",
        (cutoff,),
    ) as cur:
        stale_rows = [_row_to_dict(r) or {} for r in await cur.fetchall()]
    stale_task_ids = [row["id"] for row in stale_rows]
    if stale_task_ids:
        placeholders = ", ".join("?" for _ in stale_task_ids)
        await db.execute(
            f"UPDATE task_log SET status = 'pending', claimed_by = NULL, claimed_at = NULL "
            f"WHERE id IN ({placeholders})",
            tuple(stale_task_ids),
        )
    await db.commit()
    # bc9259b8 — a crashed worker never calls close_session, so route each linked
    # in_progress sprint item through the stall-retry budget here too: re-queue
    # while under budget, fail silently once exhausted (instead of resetting to
    # pending forever). Dedup on item_id; keep the claiming session for the log.
    _seen_items: set[str] = set()
    for row in stale_rows:
        iid = row.get("sprint_item_id")
        if not iid or iid in _seen_items:
            continue
        _seen_items.add(iid)
        item = await get_sprint_item(db, iid)
        if item is None:
            continue
        await requeue_or_fail_stalled_item(
            db, item["project_id"], iid, session_id=row.get("claimed_by")
        )
    cursor = await db.execute(
        "UPDATE sessions SET status = 'archived' "
        "WHERE status IN ('active', 'idle') AND last_seen < ?",
        (cutoff,),
    )
    await db.commit()
    return {"count": cursor.rowcount, "project_ids": affected_project_ids}


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Sprint items (v1.1) --- moved to meridian/db/sprint_items.py
# (re-exported at bottom of this file via from .sprint_items import *)
# ---------------------------------------------------------------------------

async def get_goal_field_ages(
    db: aiosqlite.Connection, project_id: str, now: float | None = None
) -> dict[str, dict[str, Any]]:
    """v1.1.3 — per-field freshness for the three goal fields.

    Walks ``goal_states`` history and finds the most recent version
    where each of north_star / version_goal / sprint *actually
    changed* (or was first set). Returns
    ``{field: {updated_at, age_seconds}}`` with empty / never-set
    fields reported as ``{updated_at: None, age_seconds: None}``.

    v2.3 — prefers the per-field ``ns_updated_at`` /
    ``content_updated_at`` / ``sprint_updated_at`` columns when set
    (the latest row carries them). Falls back to history walking when
    they are NULL (pre-migration rows).
    """
    import time as _time
    now = now if now is not None else _time.time()
    async with db.execute(
        "SELECT version, goal_north_star, content, goal_sprint, updated_at, "
        "ns_updated_at, content_updated_at, sprint_updated_at "
        "FROM goal_states WHERE project_id = ? ORDER BY version ASC",
        (project_id,),
    ) as cur:
        rows = await cur.fetchall()
    field_ts: dict[str, str | None] = {
        "north_star": None, "version_goal": None, "sprint": None,
    }
    # v2.3 preferred path — if the latest row carries per-field
    # timestamps, use them directly. Cheaper and accurate even when
    # in-place UPDATEs have collapsed multi-row history.
    if rows:
        last = rows[-1]
        ns_t = last["ns_updated_at"]
        ct_t = last["content_updated_at"]
        sp_t = last["sprint_updated_at"]
        if ns_t and (last["goal_north_star"] or ""):
            field_ts["north_star"] = ns_t
        if ct_t and (last["content"] or ""):
            field_ts["version_goal"] = ct_t
        if sp_t and (last["goal_sprint"] or ""):
            field_ts["sprint"] = sp_t

    # Fall back to history walking for any field still unstamped (pre-
    # v2.3 rows). This preserves backward compat with legacy DBs.
    if any(v is None for v in field_ts.values()):
        prev: dict[str, Any] = {"north_star": "", "version_goal": "", "sprint": ""}
        for r in rows:
            row = {
                "north_star": r["goal_north_star"] or "",
                "version_goal": (r["content"] or "")
                    if isinstance(r["content"], str) else str(r["content"] or ""),
                "sprint": r["goal_sprint"] or "",
            }
            for field in ("north_star", "version_goal", "sprint"):
                if field_ts[field] is None and row[field] and row[field] != prev[field]:
                    field_ts[field] = r["updated_at"]
            prev = row

    from datetime import datetime, timezone
    out: dict[str, dict[str, Any]] = {}
    for field, ts in field_ts.items():
        if ts is None:
            out[field] = {"updated_at": None, "age_seconds": None}
            continue
        try:
            dt = datetime.fromisoformat(ts.replace(" ", "T")).replace(
                tzinfo=timezone.utc
            )
            age = max(0.0, now - dt.timestamp())
        except ValueError:
            age = None  # type: ignore[assignment]
        out[field] = {"updated_at": ts, "age_seconds": age}
    return out


def compute_coherence_warning(
    field_ages: dict[str, dict[str, Any]],
    warn_days: float = 7.0,
    critical_days: float = 30.0,
) -> dict[str, Any]:
    """Turn per-field ages into a single warning level.

    ``ok`` when every field's age < ``warn_days``.
    ``warn`` when one or more sit between ``warn`` and ``critical``.
    ``critical`` when any field is older than ``critical_days``.
    ``stale_fields`` lists every field older than ``warn_days``
    (sorted oldest-first so the UI can highlight the worst offender).
    """
    warn = warn_days * 86400
    crit = critical_days * 86400
    stale: list[dict[str, Any]] = []
    level = "ok"
    max_age = 0.0
    for field, info in field_ages.items():
        age = info.get("age_seconds")
        if age is None:
            continue
        if age >= warn:
            stale.append({"field": field, "age_seconds": age})
        if age > max_age:
            max_age = age
    stale.sort(key=lambda x: x["age_seconds"], reverse=True)
    if max_age >= crit:
        level = "critical"
    elif max_age >= warn:
        level = "warn"
    if level == "ok":
        message = "Goal fields are fresh."
    elif level == "warn":
        message = (
            "Some goal fields haven't been touched in over "
            f"{int(warn_days)} days."
        )
    else:
        message = (
            "Goal fields are stale — review and update before "
            "starting more work."
        )
    return {
        "level": level,
        "message": message,
        "stale_fields": stale,
        "max_age_seconds": max_age,
    }


#: Free-text human_id aliases that refer to the same person. Collapsed to a
#: single canonical identity so the timeline shows one calendar row per person
#: instead of one per spelling. Keys are lowercased; extend as new aliases show
#: up. (item 30 — identity unification.)
_PERSON_ALIASES: dict[str, str] = {
    "adam camerer": "adam",
    "adamcamerer": "adam",
    "adam camerer (executor)": "adam",
}


def canonical_person(human_id: str | None) -> str:
    """Map a free-text ``human_id`` to a stable canonical identity.

    Emails are already canonical (lowercased). Other values are lowercased,
    trimmed, and run through :data:`_PERSON_ALIASES`. Empty / "(unknown)"
    values collapse to a single ``"(unknown)"`` bucket.
    """
    if not human_id:
        return "(unknown)"
    raw = human_id.strip()
    if not raw or raw.lower() in ("(unknown)", "unknown", "none"):
        return "(unknown)"
    key = raw.lower()
    if "@" in key:
        return key
    return _PERSON_ALIASES.get(key, key)


async def get_timeline(
    db: aiosqlite.Connection, project_id: str
) -> dict[str, Any]:
    """v1.1.1 — return everything the Activity Timeline needs in one call.

    Returns ``{tasks, sessions, goal_events}`` where:

      * ``tasks``     — every task in the project with its session
        name attached so the frontend can lay them out per swimlane.
      * ``sessions``  — id, name, human_id, created_at (registered),
        last_seen. Drives one swimlane per row.
      * ``goal_events`` — every (north_star, version_goal, sprint)
        update with the field that changed and the timestamp. Drives
        the vertical dashed lines on the time axis.
    """
    async with db.execute(
        "SELECT t.id, t.created_at, t.status, t.description, "
        "       t.session_id, s.name AS session_name, s.human_id, "
        "       s.client_type "
        "FROM task_log t LEFT JOIN sessions s ON s.id = t.session_id "
        "WHERE t.project_id = ? "
        "ORDER BY t.created_at DESC, t.rowid DESC",
        (project_id,),
    ) as cur:
        task_rows = await cur.fetchall()
    tasks = [
        {
            "id": r["id"],
            "created_at": r["created_at"],
            "status": r["status"],
            "description": r["description"],
            "session_id": r["session_id"],
            "session_name": r["session_name"] or "(unknown)",
            "human_id": r["human_id"],
            "person": canonical_person(r["human_id"]),
            "client": r["client_type"] or "(none)",
        }
        for r in task_rows
    ]

    async with db.execute(
        "SELECT id, name, human_id, session_type, status, created_at, "
        "       last_seen, session_summary "
        "FROM sessions WHERE project_id = ? "
        "ORDER BY created_at DESC, rowid DESC",
        (project_id,),
    ) as cur:
        session_rows = await cur.fetchall()
    sessions = []
    for r in session_rows:
        summary = None
        if r["session_summary"]:
            try:
                summary = json.loads(r["session_summary"])
            except (ValueError, TypeError):
                summary = None
        sessions.append({
            "id": r["id"],
            "name": r["name"],
            "human_id": r["human_id"],
            "session_type": r["session_type"] or "human",
            "status": r["status"],
            "registered_at": r["created_at"],
            "last_seen": r["last_seen"],
            "summary": summary,
        })

    async with db.execute(
        "SELECT version, goal_north_star, content, goal_sprint, "
        "       created_at, updated_at "
        "FROM goal_states WHERE project_id = ? "
        "ORDER BY version ASC",
        (project_id,),
    ) as cur:
        goal_rows = await cur.fetchall()
    # Derive change events by diffing successive goal rows. Each row
    # produces 0–3 events depending on which fields actually changed.
    goal_events: list[dict[str, Any]] = []
    prev: dict[str, Any] | None = None
    for r in goal_rows:
        row = {
            "north_star": r["goal_north_star"] or "",
            "version_goal": _decode_content(r["content"] or ""),
            "sprint": r["goal_sprint"] or "",
            "version": r["version"],
            "updated_at": r["updated_at"],
        }
        if prev is None:
            # Seed events for v1 of each field.
            for field in ("north_star", "version_goal", "sprint"):
                if row[field]:
                    goal_events.append({
                        "field": field,
                        "version": row["version"],
                        "updated_at": row["updated_at"],
                    })
        else:
            for field in ("north_star", "version_goal", "sprint"):
                if row[field] != prev[field]:
                    goal_events.append({
                        "field": field,
                        "version": row["version"],
                        "updated_at": row["updated_at"],
                    })
        prev = row

    # Per-day contribution counts for the heatmap calendar. Aggregated in
    # Python from task_rows so the math is identical on SQLite and Postgres
    # (no DATE()/array_agg dialect differences). Date key = first 10 chars
    # of the stored UTC timestamp ("YYYY-MM-DD HH:MM:SS" → "YYYY-MM-DD").
    by_day: dict[str, dict[str, Any]] = {}
    people_set: set[str] = set()
    clients_set: set[str] = set()
    for r in task_rows:
        ts = r["created_at"] or ""
        day = ts[:10]
        if len(day) != 10:
            continue
        human = r["human_id"] or "(unknown)"
        person = canonical_person(r["human_id"])
        client = r["client_type"] or "(none)"
        people_set.add(person)
        clients_set.add(client)
        bucket = by_day.get(day)
        if bucket is None:
            bucket = {"date": day, "count": 0, "humans": {}, "people": {}, "_sess": {}}
            by_day[day] = bucket
        bucket["count"] += 1
        bucket["humans"][human] = bucket["humans"].get(human, 0) + 1
        bucket["people"][person] = bucket["people"].get(person, 0) + 1
        sid = r["session_id"] or "(none)"
        se = bucket["_sess"].get(sid)
        if se is None:
            se = {
                "session_id": r["session_id"],
                "name": r["session_name"] or "(unknown)",
                "human": human,
                "person": person,
                "client": client,
                "count": 0,
            }
            bucket["_sess"][sid] = se
        se["count"] += 1
    daily_counts = []
    for day in sorted(by_day):
        b = by_day[day]
        day_sessions = sorted(
            b["_sess"].values(), key=lambda s: (-s["count"], s["name"])
        )
        daily_counts.append({
            "date": b["date"],
            "count": b["count"],
            "sessions": day_sessions,
            "session_count": len(day_sessions),
            "humans": b["humans"],
            "people": b["people"],
        })

    return {
        "tasks": tasks,
        "sessions": sessions,
        "goal_events": goal_events,
        "daily_counts": daily_counts,
        "people": sorted(people_set),
        "clients": sorted(clients_set),
    }


SESSION_SUMMARY_SCHEMA: dict[str, Any] = {
    "name": "session_summary",
    "description": (
        "Structured retrospective for a Meridian session. Generated by "
        "claude-haiku at handoff / TTL expiry when the session shipped "
        ">=3 tasks. Stored verbatim on sessions.session_summary."
    ),
    "schema": {
        "type": "object",
        "required": [
            "session_type", "tasks_completed", "key_decisions", "summary",
        ],
        "properties": {
            "session_type": {
                "type": "string",
                "enum": ["human", "worker"],
            },
            "tasks_completed": {"type": "integer", "minimum": 0},
            "key_decisions": {
                "type": "array",
                "items": {"type": "string"},
            },
            "summary": {"type": "string", "minLength": 1},
        },
        "additionalProperties": False,
    },
}


def _build_summary_prompt(
    session: dict[str, Any], tasks: list[dict[str, Any]]
) -> str:
    """Render the session's tasks into a one-shot prompt for haiku."""
    lines = [
        f"Session: {session.get('name')}",
        f"Type: {session.get('session_type') or 'human'}",
        f"Started: {session.get('created_at')}",
        f"Last seen: {session.get('last_seen')}",
        "",
        "Tasks (oldest first):",
    ]
    for t in tasks:
        status = (t.get("status") or "").upper()
        desc = (t.get("description") or "").replace("\n", " ")
        lines.append(f"- [{status}] {desc[:240]}")
    lines.extend([
        "",
        "Summarise this session as JSON matching the session_summary schema.",
    ])
    return "\n".join(lines)


async def _default_haiku_summarizer(prompt: str) -> dict[str, Any] | None:
    """Default summarizer: call claude-haiku via the Anthropic SDK
    with response_format pinned to ``SESSION_SUMMARY_SCHEMA``.

    Returns ``None`` when the SDK / API key are unavailable so the
    caller can degrade gracefully (no exception). Tests inject their
    own summarizer via the ``summarize_session(summarizer=...)`` arg.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    try:
        from anthropic import AsyncAnthropic
    except ImportError:
        return None
    try:
        client = AsyncAnthropic(api_key=api_key)
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
            response_format={
                "type": "json_schema",
                "json_schema": SESSION_SUMMARY_SCHEMA,
            },
        )
        text_blocks = [
            getattr(b, "text", "") for b in (resp.content or [])
        ]
        text = "".join(text_blocks).strip()
        if not text:
            return None
        return json.loads(text)
    except Exception:  # noqa: BLE001 — degrade gracefully
        return None


async def summarize_session(
    db: aiosqlite.Connection,
    session_id: str,
    *,
    min_tasks: int = 3,
    summarizer: Any = None,
) -> dict[str, Any] | None:
    """Generate (or refresh) the auto-summary for a session.

    Returns the structured summary dict that got stored, or ``None``
    when the session shipped fewer than ``min_tasks`` tasks (the
    summary cost isn't worth it for trivial sessions) or the
    summarizer failed.

    ``summarizer`` is an async callable ``(prompt: str) -> dict|None``.
    Defaults to the haiku-backed implementation; tests pass a stub
    that returns a fixed dict so the suite doesn't hit the network.
    """
    async with db.execute(
        "SELECT * FROM sessions WHERE id = ?", (session_id,)
    ) as cur:
        sess_row = await cur.fetchone()
    if sess_row is None:
        return None
    session = _row_to_dict(sess_row)
    assert session is not None

    async with db.execute(
        "SELECT * FROM task_log WHERE session_id = ? "
        "ORDER BY created_at DESC, rowid DESC",
        (session_id,),
    ) as cur:
        task_rows = await cur.fetchall()
    tasks = [_row_to_dict(r) for r in task_rows]
    if len(tasks) < min_tasks:
        return None

    fn = summarizer or _default_haiku_summarizer
    prompt = _build_summary_prompt(session, tasks)
    summary = await fn(prompt)
    if not isinstance(summary, dict):
        return None
    # Defensive: ensure required keys are present + sane types.
    required = {"session_type", "tasks_completed", "key_decisions", "summary"}
    if not required.issubset(summary):
        return None
    if not isinstance(summary["summary"], str) or not summary["summary"].strip():
        return None

    await db.execute(
        "UPDATE sessions SET session_summary = ? WHERE id = ?",
        (json.dumps(summary), session_id),
    )
    await db.commit()
    return summary


async def auto_capture_session(
    db: aiosqlite.Connection, project_id: str, session_id: str
) -> None:
    """Rule-based session capture: bucket done tasks into Fixed/Added/Changed
    and save as an ephemeral *session* note (scratch pad). No-op for sessions
    with fewer than 2 done tasks so trivial sessions don't generate noise.

    9d44998b — this writes to session_notes, NOT project_notes. The permanent
    artifact of a session is its task_log entries; the bucketed summary is a
    transient convenience that expires with the session rather than polluting
    the project wiki with "Session summary (date)" notes.
    """
    from datetime import datetime, timezone

    async with db.execute(
        "SELECT description FROM task_log "
        "WHERE session_id = ? AND status = 'done' "
        "ORDER BY created_at DESC",
        (session_id,),
    ) as cur:
        rows = await cur.fetchall()
    tasks = [_row_to_dict(r) for r in rows]
    if len(tasks) < 2:
        return

    categories: dict[str, list[str]] = {
        "Fixed": [
            t["description"] for t in tasks
            if any(w in t["description"].lower() for w in ["fix", "bug", "error", "broken"])
        ],
        "Added": [
            t["description"] for t in tasks
            if any(w in t["description"].lower() for w in ["add", "feat", "new", "implement"])
        ],
        "Changed": [
            t["description"] for t in tasks
            if any(w in t["description"].lower() for w in ["update", "change", "refactor", "improve"])
        ],
    }
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = [f"Auto-capture {date_str}"]
    for label, items in categories.items():
        if items:
            lines.append(f"\n{label}:")
            for desc in items[:5]:
                lines.append(f"  - {desc[:100]}")
    if len(lines) <= 1:
        return
    await add_session_note(
        db, session_id, f"Session summary ({date_str})", "\n".join(lines)
    )


# ---------------------------------------------------------------------------
# Rewind (v1.3.0) — "Last X days" shareable project summary.
# Aggregates versions shipped, goal changes, decisions logged, session
# summaries, sprint items completed, and task counts in one async call.
# ---------------------------------------------------------------------------


def _summarize(text: str | None, limit: int = 100) -> str:
    """Truncate a multi-line string to a one-line ``limit``-char summary."""
    if not text:
        return ""
    flat = " ".join(str(text).split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1].rstrip() + "…"


async def get_rewind_data(
    db: aiosqlite.Connection, project_id: str, days: int
) -> dict[str, Any]:
    """Aggregate the v1.3.0 rewind payload for a project over ``days`` days.

    Returns a dict matching the documented response shape. Empty arrays
    + zero counts (never errors) when the period has no activity. Caller
    is responsible for the project existence 404 — this function trusts
    its input.
    """
    from datetime import datetime, timedelta, timezone

    if days <= 0:
        days = 7
    now_dt = datetime.now(timezone.utc)
    cutoff = (now_dt - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    now_iso = now_dt.isoformat()

    # --- Task aggregates ---------------------------------------------------
    async with db.execute(
        "SELECT status, COUNT(*) AS n FROM task_log "
        "WHERE project_id = ? AND created_at >= ? "
        "GROUP BY status",
        (project_id, cutoff),
    ) as cur:
        status_rows = await cur.fetchall()
    tasks_by_status: dict[str, int] = {r["status"]: r["n"] for r in status_rows}
    tasks_total = sum(tasks_by_status.values())

    # --- Versions shipped (best-effort proxy) ------------------------------
    # See module docstring above for the rationale: scan task_log
    # descriptions for "shipped" / commit-style prefixes ("feat:", "fix:",
    # "v1.2.0 —") in the period. Cap at 20.
    async with db.execute(
        "SELECT description, created_at FROM task_log "
        "WHERE project_id = ? AND created_at >= ? "
        "AND ("
        "    description LIKE '% shipped%' "
        " OR description LIKE 'shipped %' "
        " OR description LIKE 'feat:%' "
        " OR description LIKE 'fix:%' "
        " OR description LIKE 'docs:%' "
        " OR description LIKE 'feat(%' "
        " OR description LIKE 'v_._._%' ESCAPE '_'"
        ") "
        "ORDER BY created_at DESC LIMIT 20",
        (project_id, cutoff),
    ) as cur:
        ver_rows = await cur.fetchall()
    versions_shipped: list[str] = []
    seen: set[str] = set()
    for r in ver_rows:
        desc = _summarize(r["description"], limit=160)
        if desc and desc not in seen:
            seen.add(desc)
            versions_shipped.append(desc)

    # --- Goal changes ------------------------------------------------------
    # Walk goal_states in version order and diff successive rows. Only
    # rows whose created_at is within the period are emitted, but the
    # comparison anchor is the prior row regardless of period so a
    # field changed on day 8 isn't double-emitted on day 7.
    async with db.execute(
        "SELECT version, goal_north_star, content, goal_sprint, created_at "
        "FROM goal_states WHERE project_id = ? ORDER BY version ASC",
        (project_id,),
    ) as cur:
        all_goal_rows = await cur.fetchall()
    goal_changes: list[dict[str, Any]] = []
    prev = {"north_star": "", "version_goal": "", "sprint": ""}
    for r in all_goal_rows:
        current = {
            "north_star": r["goal_north_star"] or "",
            "version_goal": r["content"] or "",
            "sprint": r["goal_sprint"] or "",
        }
        in_period = False
        try:
            ts = (r["created_at"] or "").replace(" ", "T")
            dt = datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)
            cutoff_dt = datetime.now(timezone.utc).timestamp() - days * 86400
            in_period = dt.timestamp() >= cutoff_dt
        except (ValueError, TypeError):
            in_period = False
        if in_period:
            for field_key, label in (
                ("north_star", "north_star"),
                ("version_goal", "version_goal"),
                ("sprint", "sprint"),
            ):
                if current[field_key] != prev[field_key]:
                    goal_changes.append({
                        "field": label,
                        "old_summary": _summarize(prev[field_key]),
                        "new_summary": _summarize(current[field_key]),
                        "old_full": prev[field_key],
                        "new_full": current[field_key],
                        "changed_at": r["created_at"],
                    })
        prev = current

    # --- Decisions logged --------------------------------------------------
    # v1.1.4 stores decisions as a single TEXT blob on projects with
    # ``[YYYY-MM-DD] ...`` entries newest-first. Parse + filter by date.
    decisions_logged: list[dict[str, Any]] = []
    async with db.execute(
        "SELECT decisions FROM projects WHERE id = ?", (project_id,)
    ) as cur:
        drow = await cur.fetchone()
    decisions_blob = drow["decisions"] if drow else None
    if decisions_blob:
        import re as _re
        from datetime import date as _date, timedelta as _td
        cutoff_date = _date.today() - _td(days=days)
        # Each entry: ``[YYYY-MM-DD] text`` separated by blank lines.
        for chunk in _re.split(r"\n\s*\n", decisions_blob.strip()):
            m = _re.match(r"^\[(\d{4}-\d{2}-\d{2})\]\s*(.*)$", chunk.strip(), _re.S)
            if not m:
                continue
            try:
                logged_date = _date.fromisoformat(m.group(1))
            except ValueError:
                continue
            if logged_date < cutoff_date:
                continue
            decisions_logged.append({
                "text": _summarize(m.group(2), limit=240),
                "logged_at": m.group(1),
            })

    # --- Session summaries -------------------------------------------------
    async with db.execute(
        "SELECT id, name, session_summary FROM sessions "
        "WHERE project_id = ? AND created_at >= ? "
        "ORDER BY created_at DESC",
        (project_id, cutoff),
    ) as cur:
        sess_rows = await cur.fetchall()
    session_summaries: list[dict[str, Any]] = []
    for s in sess_rows:
        async with db.execute(
            "SELECT COUNT(*) AS cnt FROM task_log WHERE session_id = ? AND status = 'done'",
            (s["id"],),
        ) as cur:
            row = await cur.fetchone()
        completed = int(row["cnt"]) if row else 0
        summary_text: str = s["name"]
        if s["session_summary"]:
            try:
                parsed = json.loads(s["session_summary"])
                if isinstance(parsed, dict) and parsed.get("summary"):
                    summary_text = _summarize(parsed["summary"], limit=240)
            except (ValueError, TypeError):
                pass
        session_summaries.append({
            "session_name": s["name"],
            "summary": summary_text,
            "tasks_completed": completed,
        })

    # --- Sprint items completed -------------------------------------------
    async with db.execute(
        "SELECT version, title, completed_at, status, item_group FROM sprint_items "
        "WHERE project_id = ? AND status IN ('done','skipped','failed','pushed') "
        "AND completed_at IS NOT NULL "
        "AND completed_at >= ? "
        "ORDER BY completed_at DESC",
        (project_id, cutoff),
    ) as cur:
        sprint_rows = await cur.fetchall()
    sprint_items_completed = [
        {
            "version": r["version"],
            "title": r["title"],
            "completed_at": r["completed_at"],
            "status": r["status"],
        }
        for r in sprint_rows
    ]

    # All pending/todo sprint items regardless of time window (current sprint state)
    async with db.execute(
        "SELECT version, title, status, item_group, added_at FROM sprint_items "
        "WHERE project_id = ? AND status IN ('pending','todo','in_progress') "
        "ORDER BY added_at ASC",
        (project_id,),
    ) as cur:
        pending_rows = await cur.fetchall()
    sprint_items_pending = [
        {
            "version": r["version"],
            "title": r["title"],
            "status": r["status"],
            "item_group": r["item_group"],
        }
        for r in pending_rows
    ]

    return {
        "period_days": days,
        "generated_at": now_iso,
        "versions_shipped": versions_shipped,
        "goal_changes": goal_changes,
        "decisions_logged": decisions_logged,
        "session_summaries": session_summaries,
        "sprint_items_completed": sprint_items_completed,
        "sprint_items_pending": sprint_items_pending,
        "tasks_total": tasks_total,
        "tasks_by_status": tasks_by_status,
    }


def _strip_auto_blocks(content: str) -> str:
    """Return content with the AUTO BLOCKS section removed.

    Used to detect versions that only differ in the auto-generated
    section so they can be collapsed in the goal history view.
    """
    for marker in ("\n\n--- AUTO BLOCKS BELOW ---\n", "--- AUTO BLOCKS BELOW ---"):
        if marker in content:
            return content.split(marker)[0].rstrip()
    return content


async def get_goal_history(
    db: aiosqlite.Connection, project_id: str
) -> list[dict[str, Any]]:
    """Return meaningful goal versions for a project, newest first.

    Filters out versions where only the AUTO BLOCKS section changed —
    those are housekeeping updates and create noise in the history view.
    Each entry has: version, north_star, version_goal, sprint, created_at.
    """
    async with db.execute(
        "SELECT version, goal_north_star, content, goal_sprint, created_at "
        "FROM goal_states WHERE project_id = ? ORDER BY version DESC",
        (project_id,),
    ) as cur:
        rows = await cur.fetchall()

    filtered: list[dict[str, Any]] = []
    prev_key: tuple[str, str, str] | None = None
    for r in rows:
        stripped = _strip_auto_blocks(r["content"] or "")
        key = (r["goal_north_star"] or "", stripped, r["goal_sprint"] or "")
        if key != prev_key:
            filtered.append(
                {
                    "version": r["version"],
                    "north_star": r["goal_north_star"] or "",
                    "version_goal": r["content"] or "",
                    "sprint": r["goal_sprint"] or "",
                    "created_at": r["created_at"],
                }
            )
            prev_key = key
    return filtered


async def get_project_stats(
    db: aiosqlite.Connection, project_id: str, days: int = 30
) -> dict[str, Any]:
    """Return activity stats for the charts subtab.

    Returns tasks/day series (last ``days`` days) and sprint completion
    percentage per version. Data is sourced from task_log + sprint_items.
    """
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

    # Tasks done per day per human_id (join sessions for human attribution)
    async with db.execute(
        "SELECT substr(t.created_at, 1, 10) AS day, s.human_id, COUNT(*) AS cnt "
        "FROM task_log t LEFT JOIN sessions s ON t.session_id = s.id "
        "WHERE t.project_id = ? AND t.status = 'done' "
        "AND t.created_at >= ? GROUP BY day, s.human_id ORDER BY day ASC",
        (project_id, cutoff),
    ) as cur:
        task_rows = await cur.fetchall()

    tasks_by_day: dict[str, dict[str, int]] = {}
    for r in task_rows:
        day = r["day"]
        human = r["human_id"] or "unknown"
        if day not in tasks_by_day:
            tasks_by_day[day] = {}
        tasks_by_day[day][human] = r["cnt"]

    # Build full date series so days with 0 tasks still appear
    all_days = [
        (now - timedelta(days=days - 1 - i)).strftime("%Y-%m-%d")
        for i in range(days)
    ]
    tasks_per_day = [
        {
            "day": d,
            "by_human": tasks_by_day.get(d, {}),
            "total": sum(tasks_by_day.get(d, {}).values()),
        }
        for d in all_days
    ]

    async with db.execute(
        "SELECT substr(completed_at, 1, 10) AS day, COUNT(*) AS cnt "
        "FROM sprint_items "
        "WHERE project_id = ? AND status = 'done' "
        "AND completed_at IS NOT NULL AND completed_at >= ? "
        "GROUP BY day ORDER BY day ASC",
        (project_id, cutoff),
    ) as cur:
        sprint_day_rows = await cur.fetchall()
    sprint_by_day = {r["day"]: r["cnt"] for r in sprint_day_rows}
    sprint_items_per_day = [
        {"day": d, "total": sprint_by_day.get(d, 0)}
        for d in all_days
    ]

    # Sprint completion % per version
    async with db.execute(
        "SELECT version, status, COUNT(*) AS cnt FROM sprint_items "
        "WHERE project_id = ? GROUP BY version, status",
        (project_id,),
    ) as cur:
        sprint_rows = await cur.fetchall()

    sprint_by_version: dict[str, dict[str, int]] = {}
    for r in sprint_rows:
        v = r["version"]
        if v not in sprint_by_version:
            sprint_by_version[v] = {"done": 0, "total": 0}
        sprint_by_version[v]["total"] += r["cnt"]
        if r["status"] in ("done", "skipped"):
            sprint_by_version[v]["done"] += r["cnt"]

    sprint_velocity = [
        {
            "version": v,
            "done": d["done"],
            "total": d["total"],
            "pct": round(d["done"] / d["total"] * 100) if d["total"] else 0,
        }
        for v, d in sorted(sprint_by_version.items())
    ]

    # c975b6ef — activity by domain per day: GROUP BY source_type + day on the
    # pointer primitive (which docs/web/experiment/code/citation targets were
    # touched each day). Daily AGGREGATE totals only — no time-of-day/session detail.
    async with db.execute(
        "SELECT substr(created_at, 1, 10) AS day, source_type, COUNT(*) AS cnt "
        "FROM sprint_item_pointers "
        "WHERE project_id = ? AND created_at >= ? "
        "GROUP BY day, source_type ORDER BY day ASC",
        (project_id, cutoff),
    ) as cur:
        domain_rows = await cur.fetchall()
    domains_by_day: dict[str, dict[str, int]] = {}
    domain_keys: set[str] = set()
    for r in domain_rows:
        st = r["source_type"] or "other"
        domains_by_day.setdefault(r["day"], {})[st] = r["cnt"]
        domain_keys.add(st)
    activity_by_domain = [
        {
            "day": d,
            "by_domain": domains_by_day.get(d, {}),
            "total": sum(domains_by_day.get(d, {}).values()),
        }
        for d in all_days
    ]

    return {
        "period_days": days,
        "tasks_per_day": tasks_per_day,
        "sprint_items_per_day": sprint_items_per_day,
        "sprint_velocity": sprint_velocity,
        "activity_by_domain": activity_by_domain,
        "activity_domains": sorted(domain_keys),
    }


async def get_or_create_rewind_token(
    db: aiosqlite.Connection, project_id: str
) -> str:
    """Return the project's rewind_token, minting a uuid4 if missing.

    Idempotent: a project keeps the same token for life so previously
    distributed share-links don't break on the next POST. Returns the
    final token regardless of whether it was just created.
    """
    async with db.execute(
        "SELECT rewind_token FROM projects WHERE id = ?", (project_id,)
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        raise ValueError(f"unknown project: {project_id}")
    existing = row["rewind_token"]
    if existing:
        return existing
    token = _new_id()
    await db.execute(
        "UPDATE projects SET rewind_token = ? WHERE id = ?",
        (token, project_id),
    )
    await db.commit()
    return token


async def get_rewind_token(
    db: aiosqlite.Connection, project_id: str
) -> str | None:
    """Return the stored rewind_token, or None if not yet minted."""
    async with db.execute(
        "SELECT rewind_token FROM projects WHERE id = ?", (project_id,)
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    return row["rewind_token"]


# ---------------------------------------------------------------------------
# Waitlist
# ---------------------------------------------------------------------------


async def add_waitlist_entry(
    db: aiosqlite.Connection,
    email: str,
    note: str | None = None,
) -> dict[str, Any]:
    """Add an email to the waitlist. Returns the entry. Raises on duplicate."""
    entry_id = _new_id()
    await db.execute(
        "INSERT INTO waitlist (id, email, note) VALUES (?, ?, ?)",
        (entry_id, email.strip().lower(), note),
    )
    await db.commit()
    async with db.execute(
        "SELECT * FROM waitlist WHERE id = ?", (entry_id,)
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


async def get_waitlist(
    db: aiosqlite.Connection,
) -> list[dict[str, Any]]:
    """Return all waitlist entries, newest first."""
    async with db.execute(
        "SELECT * FROM waitlist ORDER BY created_at DESC"
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows]


# ---------------------------------------------------------------------------
# v2.0 — Hosted tier: tenants, user sessions, API tokens
# ---------------------------------------------------------------------------

async def upsert_tenant(
    db: aiosqlite.Connection,
    email: str,
    google_sub: str | None = None,
    github_sub: str | None = None,
    microsoft_sub: str | None = None,
) -> dict[str, Any]:
    """Create or update a tenant record by email.

    Updates provider sub IDs when provided. Returns the tenant dict.
    """
    email = email.strip().lower()
    async with db.execute("SELECT * FROM tenants WHERE email = ?", (email,)) as cur:
        row = await cur.fetchone()
    if row:
        tenant = _row_to_dict(row)
        updates: list[tuple[str, str]] = []
        if google_sub and tenant.get("google_sub") != google_sub:
            updates.append(("google_sub", google_sub))
        if github_sub and tenant.get("github_sub") != github_sub:
            updates.append(("github_sub", github_sub))
        if microsoft_sub and tenant.get("microsoft_sub") != microsoft_sub:
            updates.append(("microsoft_sub", microsoft_sub))
        for col, val in updates:
            await db.execute(
                f"UPDATE tenants SET {col} = ? WHERE id = ?",  # noqa: S608
                (val, tenant["id"]),
            )
            tenant[col] = val
        if updates:
            await db.commit()
        return tenant
    tid = _new_id()
    await db.execute(
        "INSERT INTO tenants (id, email, google_sub, github_sub, microsoft_sub, "
        "plan, notification_prefs) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (tid, email, google_sub, github_sub, microsoft_sub, "free",
         '{"storage":true,"sprint":true}'),
    )
    await db.commit()
    async with db.execute("SELECT * FROM tenants WHERE id = ?", (tid,)) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


async def get_tenant_by_id(
    db: aiosqlite.Connection,
    tenant_id: str,
) -> dict[str, Any] | None:
    """Return tenant by primary key, or None."""
    async with db.execute("SELECT * FROM tenants WHERE id = ?", (tenant_id,)) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


async def update_tenant(
    db: aiosqlite.Connection,
    tenant_id: str,
    **fields: object,
) -> dict[str, Any] | None:
    """Update arbitrary columns on a tenant row. Returns updated dict or None."""
    allowed = {
        "neon_project_id", "neon_db_url", "stripe_customer_id", "plan", "pool_project_id",
        "stripe_metered_item_id", "notification_prefs",
        "payment_failed_at", "dunning_email_sent",
        "compute_overage_cap_usd", "storage_overage_cap_usd",
        "compute_cu_hours_used", "storage_gb_used",
        "overage_reset_at", "compute_throttled_at",
        "trial_started_at", "inactivity_expires_at",
        "github_pat", "tunnel_active", "tunnel_plugins", "tunnel_plugins_by_host",
        "redis_commands_used", "redis_overage_cap_usd",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return await get_tenant_by_id(db, tenant_id)
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    await db.execute(
        f"UPDATE tenants SET {set_clause} WHERE id = ?",
        (*updates.values(), tenant_id),
    )
    await db.commit()
    return await get_tenant_by_id(db, tenant_id)


async def create_user_session(
    db: aiosqlite.Connection,
    tenant_id: str,
    expires_at: str,
    user_agent: str | None = None,
    ip: str | None = None,
) -> dict[str, Any]:
    """Create a web session for a tenant. Returns the session dict.

    3c28450d — optional device metadata (``user_agent``/``ip``) is stored for
    the active-sessions view; ``last_seen_at`` is seeded to now."""
    sid = _new_id()
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    await db.execute(
        "INSERT INTO user_sessions "
        "(id, tenant_id, expires_at, user_agent, ip, last_seen_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (sid, tenant_id, expires_at,
         (user_agent or None), (ip or None), now),
    )
    await db.commit()
    async with db.execute("SELECT * FROM user_sessions WHERE id = ?", (sid,)) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


async def get_user_session(
    db: aiosqlite.Connection,
    session_id: str,
) -> dict[str, Any] | None:
    """Return a user_session row, or None if missing or expired."""
    async with db.execute(
        "SELECT * FROM user_sessions WHERE id = ?", (session_id,)
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    s = _row_to_dict(row)
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    if s["expires_at"] < now:
        await delete_user_session(db, session_id)
        return None
    return s


async def delete_user_session(
    db: aiosqlite.Connection,
    session_id: str,
) -> None:
    """Delete a web session (logout)."""
    await db.execute("DELETE FROM user_sessions WHERE id = ?", (session_id,))
    await db.commit()


async def get_user_sessions_for_tenant(
    db: aiosqlite.Connection, tenant_id: str
) -> list[dict[str, Any]]:
    """3c28450d — all non-expired web sessions for a tenant, most-recently-seen
    first, for the active-sessions view."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    async with db.execute(
        "SELECT * FROM user_sessions WHERE tenant_id = ? AND expires_at > ? "
        "ORDER BY COALESCE(last_seen_at, created_at) DESC",
        (tenant_id, now),
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows if r is not None]  # type: ignore[misc]


async def touch_user_session(
    db: aiosqlite.Connection, session_id: str
) -> None:
    """3c28450d — bump last_seen_at so the active-sessions view shows recency."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    await db.execute(
        "UPDATE user_sessions SET last_seen_at = ? WHERE id = ?", (now, session_id)
    )
    await db.commit()


async def revoke_user_session(
    db: aiosqlite.Connection, session_id: str, tenant_id: str
) -> bool:
    """3c28450d — delete a session only if it belongs to ``tenant_id`` (so a user
    can never revoke another tenant's session). Returns True if a row was
    removed."""
    cur = await db.execute(
        "DELETE FROM user_sessions WHERE id = ? AND tenant_id = ?",
        (session_id, tenant_id),
    )
    await db.commit()
    return (cur.rowcount or 0) > 0


async def enqueue_provision(
    db: aiosqlite.Connection, tenant_id: str, last_error: str | None = None
) -> None:
    """4c559d4e — record a tenant needing (re)provisioning. Bumps attempts and
    resets status to 'pending' so a background drain can retry it later."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    async with db.execute(
        "SELECT attempts FROM provision_queue WHERE tenant_id = ?", (tenant_id,)
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        await db.execute(
            "INSERT INTO provision_queue "
            "(tenant_id, status, attempts, last_error, updated_at) "
            "VALUES (?, 'pending', 1, ?, ?)",
            (tenant_id, last_error, now),
        )
    else:
        await db.execute(
            "UPDATE provision_queue SET status = 'pending', attempts = attempts + 1, "
            "last_error = ?, updated_at = ? WHERE tenant_id = ?",
            (last_error, now, tenant_id),
        )
    await db.commit()


async def mark_provision_done(db: aiosqlite.Connection, tenant_id: str) -> None:
    """4c559d4e — mark a tenant's provisioning complete (clears it from pending)."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    await db.execute(
        "UPDATE provision_queue SET status = 'done', updated_at = ? WHERE tenant_id = ?",
        (now, tenant_id),
    )
    await db.commit()


async def get_pending_provisions(
    db: aiosqlite.Connection, limit: int = 50
) -> list[dict[str, Any]]:
    """4c559d4e — tenants still awaiting provisioning, oldest first."""
    async with db.execute(
        "SELECT * FROM provision_queue WHERE status = 'pending' "
        "ORDER BY created_at ASC LIMIT ?",
        (int(limit),),
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows if r is not None]  # type: ignore[misc]


async def count_pending_provisions(db: aiosqlite.Connection) -> int:
    """4c559d4e — number of tenants awaiting (re)provisioning."""
    async with db.execute(
        "SELECT COUNT(*) FROM provision_queue WHERE status = 'pending'"
    ) as cur:
        row = await cur.fetchone()
    if not row:
        return 0
    return int(row["count"] if isinstance(row, dict) else row[0])


async def upsert_graph_entities(
    db: aiosqlite.Connection,
    project_id: str,
    entities: list[dict[str, Any]],
    cap: int = 500,
) -> int:
    """c00b1ccf — replace a project's cached graph snapshot with up to ``cap``
    entities (each: qualified_name + optional file/kind/signature). Returns the
    number stored. Entities beyond the cap are dropped."""
    cap = max(0, int(cap))
    await db.execute(
        "DELETE FROM codebase_graph_entities WHERE project_id = ?", (project_id,)
    )
    stored = 0
    for ent in (entities or [])[:cap]:
        qn = str(ent.get("qualified_name") or ent.get("name") or "").strip()
        if not qn:
            continue
        await db.execute(
            "INSERT INTO codebase_graph_entities "
            "(id, project_id, qualified_name, file, kind, signature) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (_new_id(), project_id, qn,
             (ent.get("file") or None), (ent.get("kind") or None),
             (ent.get("signature") or None)),
        )
        stored += 1
    await db.commit()
    return stored


async def count_graph_entities(db: aiosqlite.Connection, project_id: str) -> int:
    """c00b1ccf — number of cached graph entities for a project."""
    async with db.execute(
        "SELECT COUNT(*) FROM codebase_graph_entities WHERE project_id = ?",
        (project_id,),
    ) as cur:
        row = await cur.fetchone()
    if not row:
        return 0
    return int(row["count"] if isinstance(row, dict) else row[0])


async def search_graph_entities(
    db: aiosqlite.Connection, project_id: str, query: str, limit: int = 10
) -> list[dict[str, Any]]:
    """c00b1ccf — keyword search over a project's cached graph snapshot. Matches
    tokens (>=3 chars) of ``query`` against qualified_name/file. Returns entity
    rows (each carrying ``file``) for handoff code-pointer enrichment."""
    import re as _re
    tokens = [t for t in _re.split(r"[^A-Za-z0-9_]+", (query or "")) if len(t) >= 3]
    if not tokens:
        return []
    clauses: list[str] = []
    params: list[Any] = [project_id]
    for tok in tokens[:6]:
        clauses.append("(qualified_name LIKE ? OR file LIKE ?)")
        like = f"%{tok}%"
        params.extend([like, like])
    sql = (
        "SELECT * FROM codebase_graph_entities WHERE project_id = ? AND ("
        + " OR ".join(clauses)
        + ") LIMIT ?"
    )
    params.append(int(limit))
    async with db.execute(sql, params) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows if r is not None]  # type: ignore[misc]


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Sprint-item pointer functions — moved to sprint_items.py, re-exported via *
# ---------------------------------------------------------------------------

async def delete_api_tokens_by_label(
    db: aiosqlite.Connection,
    tenant_id: str,
    label: str,
    exclude_id: str | None = None,
) -> int:
    """Delete all tokens with a given label for a tenant. Returns count deleted.
    Used so label acts as a unique slot -- regenerating hooks-installer token
    doesn't leave stale tokens that cause 401 loops.

    ``exclude_id`` (0e9bb6ef) — when set, the row with this id is preserved. This
    lets a caller mint the replacement token FIRST and then prune the tenant's
    older same-label tokens, so there is never an instant with zero valid keys
    (create-new-then-revoke-old ordering). Placeholders are ``?`` (the psycopg3
    adapter rewrites ``?`` -> ``%s``), so this stays SQLite+Postgres compatible."""
    if exclude_id is not None:
        cur = await db.execute(
            "DELETE FROM api_tokens WHERE tenant_id = ? AND label = ? AND id != ?",
            (tenant_id, label, exclude_id),
        )
    else:
        cur = await db.execute(
            "DELETE FROM api_tokens WHERE tenant_id = ? AND label = ?",
            (tenant_id, label),
        )
    await db.commit()
    return cur.rowcount


async def delete_orphaned_oauth_tokens(
    db: aiosqlite.Connection,
    tenant_id: str,
    *,
    label: str = "oauth",
    older_than_hours: int = 24,
) -> int:
    """Purge a tenant's stale OAuth-minted API tokens. Returns count deleted.

    The Claude Code MCP ``authorization_code`` flow mints an ``oauth``-labelled
    ``api_tokens`` row at ``/oauth/token`` even when the redirect back to the
    local callback fails; the user then retries, orphaning the previous token.
    Left alone these accumulate as dead key-list entries. This deletes rows
    matching ``label`` whose ``created_at`` is older than ``older_than_hours``,
    leaving recent (possibly in-use) tokens untouched.

    ``created_at`` is the canonical ``YYYY-MM-DD HH:MM:SS`` UTC form, so a
    lexicographic comparison against the Python-computed cutoff is correct on
    both SQLite and Postgres.
    """
    from datetime import datetime, timezone, timedelta
    cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=max(0, older_than_hours))
    ).strftime("%Y-%m-%d %H:%M:%S")
    cur = await db.execute(
        "DELETE FROM api_tokens WHERE tenant_id = ? AND label = ? AND created_at < ?",
        (tenant_id, label, cutoff),
    )
    await db.commit()
    return cur.rowcount


async def create_api_token(
    db: aiosqlite.Connection,
    tenant_id: str,
    label: str | None = None,
    token_type: str = "readwrite",
    expires_at: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Generate a bearer token for a tenant.

    Returns ``(raw_token, token_row_dict)``.  The raw token is shown once
    and never stored.  Only the SHA-256 hash is persisted.
    ``token_type`` is 'readwrite' (default) or 'readonly'.
    ``expires_at`` is an ISO 8601 datetime string for short-lived tokens (e.g. install tokens).
    """
    if token_type not in ("readwrite", "readonly"):
        token_type = "readwrite"
    import secrets
    import hashlib
    raw = f"sk_meridian_{secrets.token_urlsafe(32)}"
    token_hash = hashlib.sha256(raw.encode()).hexdigest()
    tid = _new_id()
    await db.execute(
        "INSERT INTO api_tokens (id, tenant_id, token_hash, label, token_type, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
        (tid, tenant_id, token_hash, label, token_type, expires_at),
    )
    await db.commit()
    async with db.execute("SELECT * FROM api_tokens WHERE id = ?", (tid,)) as cur:
        row = await cur.fetchone()
    return raw, _row_to_dict(row)


async def list_api_tokens(
    db: aiosqlite.Connection,
    tenant_id: str,
) -> list[dict[str, Any]]:
    """Return API token rows for a tenant, newest first."""
    async with db.execute(
        "SELECT id, label, token_hash, token_type, created_at FROM api_tokens "
        "WHERE tenant_id = ? ORDER BY created_at DESC, id DESC",
        (tenant_id,),
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows if r is not None]


async def delete_api_token(
    db: aiosqlite.Connection,
    tenant_id: str,
    token_id: str,
) -> bool:
    """Delete one API token for a tenant. Returns True when a row was removed."""
    cur = await db.execute(
        "DELETE FROM api_tokens WHERE tenant_id = ? AND id = ?",
        (tenant_id, token_id),
    )
    await db.commit()
    return cur.rowcount > 0


async def get_tenant_from_token_hash(
    db: aiosqlite.Connection,
    token_hash: str,
) -> dict[str, Any] | None:
    """Look up a tenant by a pre-hashed API token.

    Returns tenant dict with an extra ``_token_type`` field ('readwrite' or
    'readonly') so callers can enforce read-only restrictions without a second
    query. Returns None for expired tokens (and deletes them).
    """
    from datetime import datetime, timezone
    async with db.execute(
        "SELECT t.*, a.token_type AS _token_type, a.id AS _token_id, a.expires_at AS _token_expires_at "
        "FROM tenants t "
        "JOIN api_tokens a ON a.tenant_id = t.id "
        "WHERE a.token_hash = ?",
        (token_hash,),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    d = _row_to_dict(row)
    expires_at = d.pop("_token_expires_at", None)
    token_id = d.pop("_token_id", None)
    if expires_at:
        try:
            exp_dt = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            if exp_dt < datetime.now(timezone.utc):
                if token_id:
                    await db.execute("DELETE FROM api_tokens WHERE id = ?", (token_id,))
                    await db.commit()
                return None
        except Exception:
            pass
    return d


async def count_tenants_by_plan(
    db: aiosqlite.Connection,
    plan: str,
    *,
    provisioned_only: bool = False,
) -> int:
    """Count tenant rows for a plan, optionally limiting to provisioned rows."""
    query = "SELECT COUNT(*) AS n FROM tenants WHERE plan = ?"
    if provisioned_only:
        query += " AND (neon_project_id IS NOT NULL OR neon_db_url IS NOT NULL)"
    async with db.execute(query, (plan,)) as cur:
        row = await cur.fetchone()
    if row is None:
        return 0
    if isinstance(row, dict):
        return int(row.get("n", 0) or 0)
    keys = row.keys() if hasattr(row, "keys") else []
    if keys:
        return int(row["n"] or 0)
    return int(row[0] or 0)


# ---------------------------------------------------------------------------
# v2.2 — Neon pool project management
# ---------------------------------------------------------------------------

async def get_available_pool_project(
    db: aiosqlite.Connection,
    tier: str = "standard",
    max_customers: int = 8,
) -> dict[str, Any] | None:
    """Return a pool project for the given tier that has room for more customers.

    Returns the pool project dict, or None if all projects are full (caller
    should create a new Neon project and register it via register_pool_project).
    """
    async with db.execute(
        "SELECT * FROM neon_pool_projects "
        "WHERE tier = ? AND customer_count < ? "
        "ORDER BY customer_count DESC LIMIT 1",
        (tier, max_customers),
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


async def register_pool_project(
    db: aiosqlite.Connection,
    neon_project_id: str,
    tier: str = "standard",
) -> dict[str, Any]:
    """Register a newly created Neon project as an available pool project."""
    import uuid
    pid = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO neon_pool_projects (id, neon_project_id, tier, customer_count) "
        "VALUES (?, ?, ?, 0)",
        (pid, neon_project_id, tier),
    )
    await db.commit()
    async with db.execute(
        "SELECT * FROM neon_pool_projects WHERE id = ?", (pid,)
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


async def increment_pool_project_count(
    db: aiosqlite.Connection,
    neon_project_id: str,
) -> None:
    """Increment customer_count on a pool project after assigning a new customer.

    Prefer :func:`claim_pool_project_slot` for race-free atomic claim+increment.
    This helper is kept for backfill / fix-up scripts that aren't on the
    signup hot path.
    """
    await db.execute(
        "UPDATE neon_pool_projects SET customer_count = customer_count + 1 "
        "WHERE neon_project_id = ?",
        (neon_project_id,),
    )
    await db.commit()


async def claim_pool_project_slot(
    db: aiosqlite.Connection,
    tier: str = "standard",
    max_customers: int = 8,
) -> dict[str, Any] | None:
    """Item 38 — atomically reserve a slot in an available pool project.

    Replaces the read/then/write race in ``get_available_pool_project`` +
    ``increment_pool_project_count``: two concurrent signups previously could
    both see the same pool with 7/8 slots, both proceed, both increment, and
    leave the pool at 9/8 — overprovisioned past the soft cap.

    The single UPDATE-with-subquery is atomic on SQLite (statement-level) and
    safe on Postgres MVCC because the *outer* ``customer_count < ?`` re-check
    runs against the locked row, so the second concurrent UPDATE harmlessly
    no-ops once T1 has bumped the count to the cap.

    Returns the updated pool project row (with the post-increment count), or
    ``None`` if no pool has room — caller should create a new pool project
    and try again.
    """
    async with db.execute(
        "UPDATE neon_pool_projects "
        "SET customer_count = customer_count + 1 "
        "WHERE id = ("
        "  SELECT id FROM neon_pool_projects "
        "  WHERE tier = ? AND customer_count < ? "
        "  ORDER BY customer_count DESC LIMIT 1"
        ") "
        "AND customer_count < ? "
        "RETURNING *",
        (tier, max_customers, max_customers),
    ) as cur:
        row = await cur.fetchone()
    await db.commit()
    return _row_to_dict(row) if row else None


async def decrement_pool_project_count(
    db: aiosqlite.Connection,
    neon_project_id: str,
) -> None:
    """Decrement customer_count after removing a customer database."""
    await db.execute(
        "UPDATE neon_pool_projects "
        "SET customer_count = CASE "
        "WHEN customer_count > 0 THEN customer_count - 1 ELSE 0 END "
        "WHERE neon_project_id = ?",
        (neon_project_id,),
    )
    await db.commit()


async def get_pool_project_counts(
    db: aiosqlite.Connection,
    tier: str = "standard",
) -> dict[str, int]:
    """Return total project count and total customer count for a tier."""
    async with db.execute(
        "SELECT COUNT(*) as projects, COALESCE(SUM(customer_count),0) as customers "
        "FROM neon_pool_projects WHERE tier = ?",
        (tier,),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return {"projects": 0, "customers": 0}
    if isinstance(row, dict):
        return {"projects": row.get("projects", 0), "customers": row.get("customers", 0)}
    keys = row.keys() if hasattr(row, "keys") else []
    if keys:
        return {"projects": row["projects"], "customers": row["customers"]}
    return {"projects": row[0] or 0, "customers": row[1] or 0}


# ---------------------------------------------------------------------------
# v3.1 — file lock coordination
# ---------------------------------------------------------------------------


# 39544099 — shared staleness constant so file_locks and file_symbol_claims use the
# same TTL. Both mechanisms now expire via heartbeat (session.last_seen > TTL) in
# addition to the explicit expires_at column.


async def get_code_notes_for_file(
    db: aiosqlite.Connection,
    project_id: str,
    file_path: str,
    symbol: str | None = None,
) -> list[dict[str, Any]]:
    """771c00d7 — code-anchored notes for a file (newest first).

    Returns full project_notes rows where ``note_kind='code'`` and ``file_path``
    matches (after the same normalization ``claim_file`` applies) for the given
    project. Symbol scoping:

    * file-level anchors (note ``symbol`` is NULL/empty) always surface — they
      apply to any symbol in the file;
    * symbol anchors surface only when ``symbol`` is given and matches the
      note's ``symbol``.

    So ``symbol=None`` returns file-level anchors only; passing a ``symbol``
    additionally returns anchors pinned to that symbol. Returns ``[]`` for a
    blank path so callers can pass an un-anchored claim through unchanged.
    """
    normalized = _normalize_file_path(file_path)
    if not normalized or not project_id:
        return []
    sym = (symbol or "").strip()
    if sym:
        clause = "AND (symbol IS NULL OR symbol = '' OR symbol = ?)"
        params: list[Any] = [project_id, normalized, sym]
    else:
        clause = "AND (symbol IS NULL OR symbol = '')"
        params = [project_id, normalized]
    async with db.execute(
        "SELECT * FROM project_notes "
        "WHERE project_id = ? AND note_kind = 'code' AND file_path = ? "
        f"{clause} ORDER BY created_at DESC",
        params,
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows if r is not None]  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Generalized typed-resource locks (501ec93f)
# ---------------------------------------------------------------------------

# Valid resource-type prefixes for a touches_resources / resource_locks entry.
# A typed identifier is "<type>:<value>"; 'route' additionally carries the HTTP
# method, e.g. "route:POST:/projects". Unknown types are rejected so a typo
# never silently becomes an un-conflicting resource.
RESOURCE_TYPES = (
    "file",       # file:meridian/db/__init__.py
    "symbol",     # symbol:meridian/db/__init__.py::create_project (63b030a6)
    "db",         # db:migrations
    "mcp_tool",   # mcp_tool:get_parallelizable_groups
    "route",      # route:POST:/projects
    "pypi",       # pypi:publish
    "github",     # github:tag
    "note",       # note:<slug>  — project note
    "decision",   # decision:<id>  — pinned decision
)


def parse_resource_identifier(identifier: str) -> tuple[str, str]:
    """Split a typed resource identifier into ``(resource_type, value)``.

    ``identifier`` is ``"<type>:<value>"``. The type is matched case-insensitively
    against :data:`RESOURCE_TYPES`; the value keeps any further colons intact so
    ``"route:POST:/x"`` parses to ``("route", "POST:/x")``. Raises ``ValueError``
    for a missing/unknown type or an empty value.

    07bdfdbb — a leading ``inferred:`` provenance marker (used for
    auto-populated touches_resources) is stripped before type parsing, so an
    inferred resource canonicalizes to the SAME id as an explicit one and still
    participates in conflict detection. The marker only survives in the raw
    stored value, where it flags the resource as a guess that can be overridden.
    """
    text = (identifier or "").strip()
    if text.lower().startswith("inferred:"):
        text = text[len("inferred:"):].strip()
    if ":" not in text:
        raise ValueError(
            f"invalid resource identifier {identifier!r}: expected '<type>:<value>'"
        )
    rtype, value = text.split(":", 1)
    rtype = rtype.strip().lower()
    value = value.strip()
    if rtype not in RESOURCE_TYPES:
        raise ValueError(
            f"unknown resource type {rtype!r}: expected one of {', '.join(RESOURCE_TYPES)}"
        )
    if not value:
        raise ValueError(f"resource identifier {identifier!r} has an empty value")
    return rtype, value


def _normalize_resource_file_path(value: str) -> str:
    """Normalize a file path for a ``file:``/``symbol:`` resource id: backslashes
    → slashes, drop a leading ``./`` (so the same file referenced two ways
    collides on one resource lock). Distinct from :func:`_normalize_file_path`,
    which is strip-only to match how ``claim_file`` / code-note anchors store
    paths — do not merge the two. (63b030a6)"""
    value = (value or "").replace("\\", "/")
    if value.startswith("./"):
        value = value[2:]
    return value


def normalize_resource_id(identifier: str) -> str:
    """Canonicalize a typed resource identifier for storage / comparison.

    Lowercases the type, strips whitespace, and normalizes ``file:`` paths the
    same way file locks do (backslashes → slashes, drop a leading ``./``) so the
    same file referenced two ways collides on one lock. ``symbol:`` ids
    (63b030a6, ``symbol:<path>::<symbol>``) normalize the path part the same way
    while preserving the ``::<symbol>`` scope. Returns the canonical
    ``"<type>:<value>"`` string. Raises ``ValueError`` on an invalid identifier.
    """
    rtype, value = parse_resource_identifier(identifier)
    if rtype == "file":
        value = _normalize_resource_file_path(value)
    elif rtype == "symbol":
        # symbol:<path>::<symbol> — normalize the file path, keep the symbol scope.
        path, sep, sym = value.partition("::")
        value = _normalize_resource_file_path(path) + (sep + sym if sep else "")
    return f"{rtype}:{value}"


def _resource_file_of(rid: str) -> "str | None":
    """Return the file path a ``file:`` / ``symbol:`` resource id refers to, or
    None for other resource types. (63b030a6 — cross-type conflict detection.)

    2a176d6d — a ``file:<path>:<symbol>`` declaration (a single extra colon,
    NOT the ``::`` double-colon ``symbol:`` convention) is a widely-used,
    accepted shorthand elsewhere in this codebase (see the SYMBOL_SCOPE_HINT
    helper, which treats it as the *preferred* form and is why it is never
    rewritten/rejected at declaration time by ``normalize_resource_id``). But
    left as an opaque ``file:`` value, the trailing ``:<symbol>`` suffix became
    part of the "file identity" this function returns — so two items each
    using this shape with DIFFERENT trailing symbols on the SAME real file
    (e.g. ``file:x.py:funcA`` and ``file:x.py:funcB``) resolved to two
    DIFFERENT file identities and were treated as disjoint, unconflicting
    resources. That is exactly the false-negative the 2026-08-04 V026-batch6
    audit flagged ("Group 0 contains items with overlapping ... resources").
    Strip a trailing ``:<symbol>`` suffix here — for CONFLICT COMPARISON ONLY,
    never for the stored/serialized resource string — so both declarations
    correctly resolve to the same real file ``x.py`` and conflict like any
    other pair of whole-file claims on it. A genuine Windows drive-letter path
    (``C:/...``) is exempted so it is never mistaken for this pattern.
    """
    if rid.startswith("file:"):
        value = rid[len("file:"):]
        if ":" in value and not re.match(r"^[A-Za-z]:[/\\]", value):
            value = value.split(":", 1)[0]
        return value
    if rid.startswith("symbol:"):
        return rid[len("symbol:"):].partition("::")[0]
    return None


def _two_resources_conflict(r1: str, r2: str) -> bool:
    """True if two normalized resource ids conflict under the file⊃symbol hierarchy.

    Rules (63b030a6):
      - identical ids conflict (file:X vs file:X, symbol:X::a vs symbol:X::a);
      - a whole-file lock conflicts with ANY symbol on that file
        (file:X vs symbol:X::a) — file is the most exclusive grain;
      - two DIFFERENT symbols on the same file do NOT conflict
        (symbol:X::a vs symbol:X::b) — that's the point of symbol-level locking;
      - everything else conflicts only on exact string equality.
    """
    if r1 == r2:
        return True
    f1, f2 = _resource_file_of(r1), _resource_file_of(r2)
    if f1 is not None and f1 == f2:
        # Same file. Conflict unless BOTH are (distinct) symbol claims.
        both_symbols = r1.startswith("symbol:") and r2.startswith("symbol:")
        return not both_symbols
    return False


def _resource_sets_conflict(a: "set[str] | frozenset[str]", b: "set[str] | frozenset[str]") -> bool:
    """True if any resource in *a* conflicts with any in *b* under the file/symbol
    hierarchy. Replaces a plain ``set.isdisjoint`` so file-vs-symbol overlaps are
    caught. (63b030a6)"""
    for ra in a:
        for rb in b:
            if _two_resources_conflict(ra, rb):
                return True
    return False


def parse_touches_resources(raw: Any) -> list[str]:
    """Decode a sprint item's ``touches_resources`` field into normalized ids.

    Accepts a JSON list, a Python list, or a comma-separated string. Each entry
    is normalized via :func:`normalize_resource_id`; entries that fail validation
    are skipped (best-effort decode so a single bad value never breaks reads).
    Duplicates are collapsed while preserving first-seen order. ``None``/empty
    yields ``[]``.
    """
    if raw is None:
        return []
    if isinstance(raw, list):
        values: list[Any] = raw
    else:
        text = str(raw).strip()
        if not text:
            return []
        try:
            decoded = json.loads(text)
            values = decoded if isinstance(decoded, list) else [decoded]
        except Exception:  # noqa: BLE001
            values = [part.strip() for part in text.split(",")]
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        candidate = str(value or "").strip()
        if not candidate:
            continue
        try:
            normalized = normalize_resource_id(candidate)
        except ValueError:
            continue
        if normalized not in seen:
            seen.add(normalized)
            out.append(normalized)
    return out


def serialize_touches_resources(raw: Any) -> str | None:
    """Normalize and JSON-encode a touches_resources input for storage.

    Returns a JSON array string, or ``None`` when there are no valid resources
    (so the column stays NULL rather than holding ``"[]"``). Raises ``ValueError``
    when an explicit identifier is malformed, so writers surface bad input
    instead of silently dropping it.
    """
    if raw is None:
        return None
    if isinstance(raw, list):
        items = raw
    else:
        text = str(raw).strip()
        if not text:
            return None
        try:
            decoded = json.loads(text)
            items = decoded if isinstance(decoded, list) else [decoded]
        except Exception:  # noqa: BLE001
            items = [part.strip() for part in text.split(",")]
    normalized: list[str] = []
    seen: set[str] = set()
    for value in items:
        candidate = str(value or "").strip()
        if not candidate:
            continue
        # 07bdfdbb — preserve an `inferred:` provenance marker in storage so the
        # guess stays distinguishable/overridable, while deduping + comparing on
        # the underlying canonical id (normalize_resource_id strips the marker).
        marker = ""
        body = candidate
        if candidate.lower().startswith("inferred:"):
            marker = "inferred:"
            body = candidate[len("inferred:"):].strip()
        norm = normalize_resource_id(body)  # raises on bad input
        if norm not in seen:
            seen.add(norm)
            normalized.append(marker + norm)
    return json.dumps(normalized) if normalized else None


# f5f2a89d — [[slug]] backlink parsing for notes/decisions
_BACKLINK_RE = re.compile(r'\[\[([^\]]+)\]\]')


def parse_note_backlinks(body: str) -> list[str]:
    """Extract [[slug]] references from a note or decision body."""
    return _BACKLINK_RE.findall(body or "")


async def get_notes_backlinked_to(
    db: aiosqlite.Connection, project_id: str, slug: str
) -> list[dict[str, Any]]:
    """Return lightweight records for notes whose body references [[slug]]."""
    pattern = f"%[[{slug}]]%"
    async with db.execute(
        "SELECT id, slug, title FROM project_notes "
        "WHERE project_id = ? AND body LIKE ?",
        (project_id, pattern),
    ) as cur:
        rows = await cur.fetchall()
    return [
        {"id": r["id"], "slug": r["slug"], "title": r["title"]}
        if isinstance(r, dict)
        else {"id": r[0], "slug": r[1], "title": r[2]}
        for r in (rows or [])
    ]


# get_sprint_items_for_resource — moved to sprint_items.py, re-exported via *


# _is_manual_sprint_item, get_parallelizable_groups, assign_sprint_waves,
# analyze_sprint — moved to sprint_items.py, re-exported via *

# ---------------------------------------------------------------------------

async def store_finding(
    db: aiosqlite.Connection,
    project_id: str,
    content: str,
    *,
    session_id: str | None = None,
    key: str | None = None,
    title: str | None = None,
    task_id: str | None = None,
) -> dict[str, Any]:
    """c35370cc — persist a per-task intermediate result to ``session_findings``.

    Parallel reader agents write findings that survive session boundaries; an
    orchestrator or writer agent reads them back via :func:`get_findings`. ``key``
    is an optional bucket (e.g. a subsystem name) for scoped retrieval.
    """
    fid = _new_id()
    await db.execute(
        "INSERT INTO session_findings "
        "(id, project_id, session_id, key, title, content, task_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (fid, project_id, session_id, key, title, content, task_id),
    )
    await db.commit()
    async with db.execute(
        "SELECT * FROM session_findings WHERE id = ?", (fid,)
    ) as cur:
        return _row_to_dict(await cur.fetchone()) or {}


async def get_findings(
    db: aiosqlite.Connection,
    project_id: str,
    *,
    key: str | None = None,
    session_id: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """c35370cc — read stored findings for a project (newest first), optionally
    scoped by ``key`` and/or ``session_id``."""
    sql = "SELECT * FROM session_findings WHERE project_id = ?"
    params: list[Any] = [project_id]
    if key is not None:
        sql += " AND key = ?"
        params.append(key)
    if session_id is not None:
        sql += " AND session_id = ?"
        params.append(session_id)
    sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
    params.append(int(limit))
    async with db.execute(sql, params) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows]


async def send_message(
    db: aiosqlite.Connection,
    project_id: str,
    to_session_id: str,
    payload: str,
    *,
    from_session_id: str | None = None,
    kind: str | None = None,
    tenant_id: str | None = None,
) -> dict[str, Any]:
    """d3a3a01d — enqueue an actor-model message to another session.

    0bfde7ad — after the durable DB write (still the source of truth), best-
    effort publishes the row to Redis (meridian/redis_bridge.py) so a live
    subscriber gets pushed instead of having to poll receive_messages. Purely
    additive: publish failures / no Redis configured never affect this
    function's return value or the persisted message.

    342dd15f — optional ``tenant_id`` is forwarded to
    ``publish_session_message`` to enable per-tenant Upstash cost-guard
    enforcement. When absent (self-hosted / unauthenticated), the budget check
    is skipped entirely.
    """
    mid = _new_id()
    await db.execute(
        "INSERT INTO session_messages "
        "(id, project_id, from_session_id, to_session_id, kind, payload) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (mid, project_id, from_session_id, to_session_id, kind, payload),
    )
    await db.commit()
    async with db.execute(
        "SELECT * FROM session_messages WHERE id = ?", (mid,)
    ) as cur:
        row = _row_to_dict(await cur.fetchone()) or {}
    if row:
        from .. import redis_bridge as _redis_bridge  # noqa: PLC0415 — lazy, optional dep

        try:
            await _redis_bridge.publish_session_message(
                to_session_id, row, tenant_id=tenant_id, db=db
            )
        except Exception:  # noqa: BLE001
            pass  # never let a push-augmentation failure affect the DB write above
    return row


async def receive_messages(
    db: aiosqlite.Connection,
    session_id: str,
    *,
    mark_read: bool = True,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """d3a3a01d — fetch unread messages addressed to ``session_id`` (oldest
    first) and, by default, mark them read so the next poll only sees new ones."""
    async with db.execute(
        "SELECT * FROM session_messages WHERE to_session_id = ? AND read_at IS NULL "
        "ORDER BY created_at ASC, id ASC LIMIT ?",
        (session_id, int(limit)),
    ) as cur:
        rows = [_row_to_dict(r) for r in await cur.fetchall()]
    if mark_read and rows:
        ids = [r["id"] for r in rows]
        placeholders = ",".join("?" for _ in ids)
        # 6adba18c — pass now as a parameter (%s / ?) rather than embedding
        # datetime('now') in SQL.  The pg_adapter converts datetime('now') to
        # to_char(clock_timestamp()...) which returns TEXT — but session_messages
        # .read_at is TIMESTAMPTZ in Postgres, so assigning TEXT directly inside
        # the SQL expression fails with "expression is of type text".  Passing a
        # formatted ISO string as a bound parameter avoids the type mismatch:
        # psycopg3 sends it as a typed parameter and Postgres applies the
        # implicit TEXT→TIMESTAMPTZ cast; SQLite stores it as-is (TEXT column).
        now_ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        await db.execute(
            f"UPDATE session_messages SET read_at = ? WHERE id IN ({placeholders})",
            [now_ts, *ids],
        )
        await db.commit()
    return rows


async def idle_until_all_done(
    db: aiosqlite.Connection,
    session_ids: list[str],
) -> dict[str, Any]:
    """d3a3a01d — non-blocking barrier check across sibling sessions.

    Returns ``{all_done, pending, statuses}``. A session counts as done when it
    is closed/archived (or missing); active/idle sessions are still running. The
    server can't block, so the caller polls until ``all_done`` is True — the
    A2A-compatible "wait for X, Y, Z to finish" primitive.
    """
    statuses: dict[str, Any] = {}
    pending: list[str] = []
    for sid in session_ids or []:
        async with db.execute(
            "SELECT status FROM sessions WHERE id = ?", (sid,)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            statuses[sid] = "missing"
            continue
        status = row["status"] if isinstance(row, dict) else row[0]
        statuses[sid] = status
        if status in ("active", "idle"):
            pending.append(sid)
    return {"all_done": not pending, "pending": pending, "statuses": statuses}


# ---------------------------------------------------------------------------
# Symbol-level parallel protection (4bac57ff)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Blog CMS (6234f9b8) — admin-authored posts
# ---------------------------------------------------------------------------


def _slugify_title(title: str) -> str:
    """Lowercase, hyphenated, alphanumeric slug from a title."""
    import re as _re
    slug = _re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    return slug or "post"


async def _unique_blog_slug(db: aiosqlite.Connection, base: str, exclude_id: str | None = None) -> str:
    """Return ``base``, or base-2/base-3/... if the slug is already taken."""
    slug = base
    n = 1
    while True:
        async with db.execute(
            "SELECT id FROM blog_posts WHERE slug = ?", (slug,)
        ) as cur:
            row = await cur.fetchone()
        existing = _row_to_dict(row)
        if existing is None or existing.get("id") == exclude_id:
            return slug
        n += 1
        slug = f"{base}-{n}"


async def upsert_blog_post(
    db: aiosqlite.Connection,
    *,
    post_id: str | None = None,
    title: str,
    body_md: str = "",
    slug: str | None = None,
) -> dict[str, Any]:
    """Create a draft, or update an existing post's title/body/slug. Status is
    not changed here — use publish_blog_post / unpublish_blog_post for that."""
    title = (title or "").strip() or "Untitled"
    if post_id:
        existing = await get_blog_post(db, post_id)
        if existing is None:
            raise ValueError("blog post not found")
        new_slug = await _unique_blog_slug(
            db, _slugify_title(slug or existing.get("slug") or title), exclude_id=post_id
        )
        await db.execute(
            "UPDATE blog_posts SET title = ?, body_md = ?, slug = ?, "
            "updated_at = datetime('now') WHERE id = ?",
            (title, body_md or "", new_slug, post_id),
        )
        await db.commit()
        return await get_blog_post(db, post_id)
    bid = _new_id()
    new_slug = await _unique_blog_slug(db, _slugify_title(slug or title))
    await db.execute(
        "INSERT INTO blog_posts (id, title, slug, body_md, status) "
        "VALUES (?, ?, ?, ?, 'draft')",
        (bid, title, new_slug, body_md or ""),
    )
    await db.commit()
    return await get_blog_post(db, bid)


async def get_blog_post(db: aiosqlite.Connection, post_id: str) -> dict[str, Any] | None:
    async with db.execute("SELECT * FROM blog_posts WHERE id = ?", (post_id,)) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


async def get_blog_post_by_slug(
    db: aiosqlite.Connection, slug: str, *, published_only: bool = True
) -> dict[str, Any] | None:
    sql = "SELECT * FROM blog_posts WHERE slug = ?"
    params: list[Any] = [(slug or "").strip()]
    if published_only:
        sql += " AND status = 'published'"
    async with db.execute(sql, tuple(params)) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


async def list_blog_posts(
    db: aiosqlite.Connection, status: str | None = None
) -> list[dict[str, Any]]:
    """List posts newest-first. ``status`` filters to draft/published."""
    if status in ("draft", "published"):
        sql = ("SELECT * FROM blog_posts WHERE status = ? "
               "ORDER BY COALESCE(published_at, updated_at) DESC")
        params: tuple[Any, ...] = (status,)
    else:
        sql = "SELECT * FROM blog_posts ORDER BY updated_at DESC"
        params = ()
    async with db.execute(sql, params) as cur:
        rows = await cur.fetchall()
    return [r for r in (_row_to_dict(row) for row in rows) if r]


async def publish_blog_post(db: aiosqlite.Connection, post_id: str) -> dict[str, Any] | None:
    cur = await db.execute(
        "UPDATE blog_posts SET status = 'published', "
        "published_at = COALESCE(published_at, datetime('now')), "
        "updated_at = datetime('now') WHERE id = ?",
        (post_id,),
    )
    await db.commit()
    if cur.rowcount == 0:
        return None
    return await get_blog_post(db, post_id)


async def unpublish_blog_post(db: aiosqlite.Connection, post_id: str) -> dict[str, Any] | None:
    cur = await db.execute(
        "UPDATE blog_posts SET status = 'draft', published_at = NULL, "
        "updated_at = datetime('now') WHERE id = ?",
        (post_id,),
    )
    await db.commit()
    if cur.rowcount == 0:
        return None
    return await get_blog_post(db, post_id)


async def delete_blog_post(db: aiosqlite.Connection, post_id: str) -> bool:
    cur = await db.execute("DELETE FROM blog_posts WHERE id = ?", (post_id,))
    await db.commit()
    return cur.rowcount > 0


_BLOG_STATUSES = ("draft", "published", "archived")


def _blog_url(post: dict[str, Any] | None) -> dict[str, Any] | None:
    """Attach a computed ``url`` (``/blog/<slug>``) to a blog-post dict."""
    if post is None:
        return None
    slug = post.get("slug")
    post["url"] = f"/blog/{slug}" if slug else None
    return post


async def save_blog_post(
    db: aiosqlite.Connection,
    title: str,
    body: str = "",
    *,
    status: str = "draft",
    tenant_id: str | None = None,
    slug: str | None = None,
    post_id: str | None = None,
) -> dict[str, Any]:
    """8843250f — create (or update, when ``post_id`` is given) a
    workspace-scoped blog post with a draft|published|archived lifecycle.

    Workspace-scoped by ``tenant_id`` like ``add_workspace_note``. Reuses the
    existing ``_slugify_title`` / ``_unique_blog_slug`` helpers. Sets
    ``published_at`` the first time a post becomes 'published'. Returns the
    stored row with a computed ``/blog/<slug>`` ``url`` field.
    """
    title = (title or "").strip() or "Untitled"
    status = status if status in _BLOG_STATUSES else "draft"

    if post_id:
        existing = await get_blog_post(db, post_id)
        if existing is None:
            raise ValueError("blog post not found")
        new_slug = await _unique_blog_slug(
            db, _slugify_title(slug or existing.get("slug") or title), exclude_id=post_id
        )
        # First publish stamps published_at; keep it once set.
        pub = existing.get("published_at")
        pub_sql = ", published_at = COALESCE(published_at, datetime('now'))" if status == "published" else ""
        await db.execute(
            "UPDATE blog_posts SET title = ?, body_md = ?, slug = ?, status = ?, "
            "tenant_id = ?, updated_at = datetime('now')" + pub_sql + " WHERE id = ?",
            (title, body or "", new_slug, status, tenant_id, post_id),
        )
        await db.commit()
        return _blog_url(await get_blog_post(db, post_id))

    bid = _new_id()
    new_slug = await _unique_blog_slug(db, _slugify_title(slug or title))
    if status == "published":
        await db.execute(
            "INSERT INTO blog_posts (id, title, slug, body_md, status, tenant_id, published_at) "
            "VALUES (?, ?, ?, ?, ?, ?, datetime('now'))",
            (bid, title, new_slug, body or "", status, tenant_id),
        )
    else:
        await db.execute(
            "INSERT INTO blog_posts (id, title, slug, body_md, status, tenant_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (bid, title, new_slug, body or "", status, tenant_id),
        )
    await db.commit()
    return _blog_url(await get_blog_post(db, bid))


async def get_blog_posts(
    db: aiosqlite.Connection,
    tenant_id: str | None = None,
    status: str | None = None,
) -> list[dict[str, Any]]:
    """8843250f — list workspace-scoped blog posts, newest first.

    Scoped to ``tenant_id`` (like ``get_workspace_notes``) and optionally
    filtered by ``status`` (draft|published|archived). Each row carries a
    computed ``url`` (``/blog/<slug>``) rather than a stored column.
    """
    clauses: list[str] = []
    params: list[Any] = []
    if status in _BLOG_STATUSES:
        clauses.append("status = ?")
        params.append(status)
    scope, scope_params = _ws_tenant_clause(tenant_id)
    if scope:
        clauses.append(scope)
        params.extend(scope_params)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    async with db.execute(
        "SELECT * FROM blog_posts" + where
        + " ORDER BY COALESCE(published_at, updated_at) DESC, created_at DESC",
        params or None,
    ) as cur:
        rows = await cur.fetchall()
    return [_blog_url(r) for r in (_row_to_dict(row) for row in rows) if r]  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Active worktrees — per-session git worktree tracking
# ---------------------------------------------------------------------------


async def register_worktree(
    db: aiosqlite.Connection,
    session_id: str,
    project_id: str,
    branch: str,
    path: str,
    item_id: str | None = None,
    pid: int | None = None,
) -> dict[str, Any]:
    """Register a new active git worktree. Returns the inserted row.

    ``pid`` (eb2e44f8) — the OS PID of the process that created this
    worktree, when the caller knows it. Optional and best-effort: when set,
    ``worktree_cleanup.validate_worktree_cleanup_target`` uses it to refuse
    real disk removal while that process is still alive. Omitting it simply
    skips that liveness check (same fail-open-on-absent-data posture as
    every other optional worktree field here).
    """
    wid = _new_id()
    await db.execute(
        "INSERT INTO active_worktrees (id, session_id, project_id, item_id, branch, path, pid) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (wid, session_id, project_id, item_id, branch, path, pid),
    )
    await db.commit()
    async with db.execute(
        "SELECT * FROM active_worktrees WHERE id = ?", (wid,)
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)  # type: ignore[return-value]


async def get_worktree(
    db: aiosqlite.Connection,
    worktree_id: str,
) -> dict[str, Any] | None:
    """Return a single active_worktrees row by id, or None."""
    async with db.execute(
        "SELECT * FROM active_worktrees WHERE id = ?", (worktree_id,)
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row) if row is not None else None


async def remove_worktree(
    db: aiosqlite.Connection,
    worktree_id: str,
) -> bool:
    """Mark a worktree as removed. Returns True when a row was updated."""
    cursor = await db.execute(
        "UPDATE active_worktrees SET removed_at = datetime('now') "
        "WHERE id = ? AND removed_at IS NULL",
        (worktree_id,),
    )
    await db.commit()
    return (cursor.rowcount or 0) > 0


async def list_active_worktrees(
    db: aiosqlite.Connection,
    project_id: str,
) -> list[dict[str, Any]]:
    """Return active (not removed) worktrees for a project, newest first."""
    async with db.execute(
        "SELECT aw.*, s.name AS session_name "
        "FROM active_worktrees aw "
        "LEFT JOIN sessions s ON s.id = aw.session_id "
        "WHERE aw.project_id = ? AND aw.removed_at IS NULL "
        "ORDER BY aw.created_at DESC",
        (project_id,),
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows if r is not None]  # type: ignore[misc]


async def get_active_worktree_for_session(
    db: aiosqlite.Connection,
    session_id: str,
) -> dict[str, Any] | None:
    """Return the most recent active worktree for a session, or None."""
    async with db.execute(
        "SELECT * FROM active_worktrees "
        "WHERE session_id = ? AND removed_at IS NULL "
        "ORDER BY created_at DESC LIMIT 1",
        (session_id,),
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row) if row is not None else None


async def list_worktrees_pending_cleanup(
    db: aiosqlite.Connection,
    project_id: str | None = None,
) -> list[dict[str, Any]]:
    """a03c0eeb — real disk-cleanup candidates: worktree rows still marked
    active in the DB (``removed_at IS NULL``) whose owning sprint item has
    reached a terminal status (``done``/``skipped``/``failed``/``pushed`` —
    i.e. the item is merged/integrated or otherwise fully resolved) OR whose
    owning session has ``closed``/``archived``.

    DB bookkeeping (``remove_worktree``) never by itself guarantees the
    worktree directory was actually removed from disk — an executor may
    have called ``complete_sprint_item``/ended its session without ever
    running ``git worktree remove`` (or the follow-up DELETE call). This is
    the query the periodic sweep (``worktree_cleanup.sweep_stale_worktrees``)
    uses to find those orphans so they can be reclaimed for real.

    Pass ``project_id`` to scope to one project; omit to sweep every project
    (used by the server-wide periodic sweep loop).
    """
    where = ["aw.removed_at IS NULL"]
    params: list[Any] = []
    if project_id is not None:
        where.append("aw.project_id = ?")
        params.append(project_id)
    where.append(
        "(si.status IN ('done', 'skipped', 'failed', 'pushed') "
        "OR s.status IN ('closed', 'archived'))"
    )
    query = (
        "SELECT aw.* FROM active_worktrees aw "
        "LEFT JOIN sprint_items si ON si.id = aw.item_id "
        "LEFT JOIN sessions s ON s.id = aw.session_id "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY aw.created_at ASC"
    )
    async with db.execute(query, params or None) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows if r is not None]  # type: ignore[misc]


# ---------------------------------------------------------------------------
# v2.4 — Pinned decisions (editable constitution alongside the append-only log)
# ---------------------------------------------------------------------------

_SUGGESTED_DECISION_CATEGORIES = {
    "STRATEGIC", "COMPETITIVE", "TECHNICAL", "TACTICAL",
    "BUSINESS", "PRODUCT", "ARCHITECTURAL",
}

# 366317e9 — decision priority drives dashboard ordering + context-injection
# weight. urgent surfaces first, then normal, then low. Rank map is reused by
# get_pinned_decisions and the handoff/context-block sort keys.
_DECISION_PRIORITIES = ("urgent", "normal", "low")
_DECISION_PRIORITY_RANK = {"urgent": 0, "normal": 1, "low": 2}


def _normalize_decision_priority(priority: str | None) -> str:
    """Coerce an arbitrary priority into the {urgent, normal, low} set.

    Unknown/empty values normalize to 'normal' so a bad input never rejects a
    pin; the dashboard and context injection only understand the three levels.
    """
    p = (priority or "").strip().lower()
    return p if p in _DECISION_PRIORITY_RANK else "normal"


def _parse_decision_edit_log(raw: Any) -> list[dict[str, Any]]:
    """Parse the ``edit_log`` JSON blob into a list. NULL/empty/garbage → []."""
    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _hydrate_decision_row(row: Any) -> dict[str, Any] | None:
    """Row → dict with ``priority`` defaulted and ``edit_log`` parsed to a list."""
    d = _row_to_dict(row)
    if d is None:
        return None
    d["priority"] = _normalize_decision_priority(d.get("priority"))
    d["edit_log"] = _parse_decision_edit_log(d.get("edit_log"))
    # 2b39549d — always surface the assumption fields (keys present even on
    # pre-migration rows). A decision with an assumption but no recorded status
    # is treated as 'unvalidated'.
    _assump = d.get("assumption")
    d["assumption"] = _assump
    d["assumption_status"] = d.get("assumption_status") or (
        "unvalidated" if _assump else None
    )
    return d


async def pin_decision(
    db: aiosqlite.Connection,
    project_id: str,
    title: str,
    body: str,
    category: str = "TECHNICAL",
    priority: str = "normal",
    assumption: str | None = None,
) -> dict[str, Any]:
    """Create a new pinned decision row. Returns the inserted row.

    Pinned decisions live alongside the append-only ``projects.decisions``
    log. Use this for the "current truth" set that supersedes earlier
    statements (pricing tiers, driver choices, etc). The log captures
    micro-decisions; this captures the constitution.

    category is free-text; suggested values: STRATEGIC, COMPETITIVE, TECHNICAL,
    TACTICAL, BUSINESS, PRODUCT, ARCHITECTURAL.

    priority (366317e9) is one of urgent | normal | low (default normal);
    invalid values normalize to 'normal'. It weights dashboard ordering and the
    decisions injected into start_session / generate_handoff context.

    6fb48898 — a kebab-cased ``slug`` and a short memorable ``nickname`` are
    auto-generated from the title, unique per project, mirroring sprint_items.
    """
    from meridian.secret_redaction import check_for_secrets
    check_for_secrets(body, context="decision body")
    did = _new_id()
    priority = _normalize_decision_priority(priority)
    # 2b39549d — an assumption starts life 'unvalidated'; no assumption → NULL.
    _assump = (assumption or "").strip() or None
    _assump_status = "unvalidated" if _assump else None
    # 6fb48898 — derive human-readable secondary keys from the title.
    _slug = await _unique_decision_slug(
        db, project_id, _sprint_item_slug_base(title)
    )
    _nickname = await _unique_decision_nickname(
        db, project_id, _sprint_item_nickname_base(title, did)
    )
    await db.execute(
        "INSERT INTO decisions_pinned "
        "(id, project_id, title, body, category, priority, assumption, assumption_status, "
        "slug, nickname) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (did, project_id, title, body, category, priority, _assump, _assump_status,
         _slug, _nickname),
    )
    await db.commit()
    # ITEM 6 — live push so the constitution view refreshes without polling.
    _publish_project_event(project_id, "decision_pinned", {"decision_id": did})
    return (await get_pinned_decision(db, did)) or {"id": did}


async def get_pinned_decision(
    db: aiosqlite.Connection, decision_id: str
) -> dict[str, Any] | None:
    async with db.execute(
        "SELECT * FROM decisions_pinned WHERE id = ?", (decision_id,)
    ) as cur:
        row = await cur.fetchone()
    return _hydrate_decision_row(row)


async def get_pinned_decisions(
    db: aiosqlite.Connection,
    project_id: str,
    include_superseded: bool = False,
) -> list[dict[str, Any]]:
    """Return all pinned decisions for a project, highest priority first.

    Rows are ordered urgent → normal → low, then newest-first within a
    priority band, so the dashboard and context injection both surface the
    most important decisions at the top. Each row carries a normalized
    ``priority`` and a parsed ``edit_log`` list (366317e9).

    Defaults to active only — superseded entries stay in history but
    are filtered out of the live constitution view.
    """
    if include_superseded:
        sql = (
            "SELECT * FROM decisions_pinned WHERE project_id = ? "
            "ORDER BY created_at DESC"
        )
        args = (project_id,)
    else:
        sql = (
            "SELECT * FROM decisions_pinned WHERE project_id = ? "
            "AND status = 'active' ORDER BY created_at DESC"
        )
        args = (project_id,)
    async with db.execute(sql, args) as cur:
        rows = await cur.fetchall()
    decisions = [_hydrate_decision_row(r) for r in rows if r is not None]
    # Stable sort by priority rank keeps the SQL newest-first order within each
    # band. urgent (0) < normal (1) < low (2).
    decisions.sort(key=lambda d: _DECISION_PRIORITY_RANK.get(d["priority"], 1))  # type: ignore[index,union-attr]
    return decisions  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# 0b711a9d — strategic insights (first-class knowledge type)
# ---------------------------------------------------------------------------

_INSIGHT_HORIZONS = ("permanent", "year", "quarter")


async def create_insight(
    db: aiosqlite.Connection,
    project_id: str,
    title: str,
    body: str,
    horizon: str = "quarter",
    tags: "list | str | None" = None,
) -> dict[str, Any]:
    """Create a durable strategic insight. horizon is coerced to a valid value
    (permanent|year|quarter, default quarter). tags may be a list or a
    comma-string; stored comma-joined."""
    iid = _new_id()
    _horizon = horizon if horizon in _INSIGHT_HORIZONS else "quarter"
    if isinstance(tags, (list, tuple)):
        _tags = ",".join(str(t).strip() for t in tags if str(t).strip()) or None
    else:
        _tags = (str(tags).strip() or None) if tags else None
    await db.execute(
        "INSERT INTO insights (id, project_id, title, body, horizon, tags) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (iid, project_id, title, body or "", _horizon, _tags),
    )
    await db.commit()
    _publish_project_event(project_id, "insight_added", {"insight_id": iid, "horizon": _horizon})
    return (await get_insight(db, iid)) or {"id": iid}


async def get_insight(db: aiosqlite.Connection, insight_id: str) -> dict[str, Any] | None:
    """Fetch a single insight row by id."""
    async with db.execute("SELECT * FROM insights WHERE id = ?", (insight_id,)) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


async def get_insights(
    db: aiosqlite.Connection,
    project_id: str,
    horizon: str | None = None,
) -> list[dict[str, Any]]:
    """Return active insights for a project, newest first. Optional horizon filter
    (permanent|year|quarter); any other value is ignored (returns all horizons)."""
    if horizon in _INSIGHT_HORIZONS:
        sql = (
            "SELECT * FROM insights WHERE project_id = ? AND horizon = ? "
            "AND status = 'active' ORDER BY created_at DESC"
        )
        args: tuple[Any, ...] = (project_id, horizon)
    else:
        sql = (
            "SELECT * FROM insights WHERE project_id = ? AND status = 'active' "
            "ORDER BY created_at DESC"
        )
        args = (project_id,)
    async with db.execute(sql, args) as cur:
        rows = await cur.fetchall()
    return [d for d in (_row_to_dict(r) for r in rows) if d is not None]


async def get_decisions_for_file(
    db: aiosqlite.Connection,
    project_id: str,
    file_path: str,
) -> list[dict[str, Any]]:
    """Return active decisions with a code_anchor matching ``file_path``.

    777f26b0 — decisions with a ``code_anchor`` set are surfaced automatically
    when an executor calls ``claim_file`` for a matching path, so architectural
    decisions relevant to the file are injected into the executor's context
    before it edits. Only active decisions are returned; superseded ones are
    excluded. Returns an empty list when no matches or when the decisions_pinned
    table has no code_anchor column (pre-migration DBs).
    """
    normalized = _normalize_file_path(file_path)
    if not normalized:
        return []
    try:
        async with db.execute(
            "SELECT * FROM decisions_pinned "
            "WHERE project_id = ? AND status = 'active' "
            "AND code_anchor IS NOT NULL AND code_anchor != '' "
            "AND code_anchor = ? "
            "ORDER BY created_at DESC",
            (project_id, normalized),
        ) as cur:
            rows = await cur.fetchall()
    except Exception:
        # Column may not exist yet on pre-migration DBs — degrade gracefully.
        return []
    return [d for d in (_hydrate_decision_row(r) for r in rows) if d is not None]


async def update_pinned_decision(
    db: aiosqlite.Connection,
    decision_id: str,
    *,
    body: str | None = None,
    category: str | None = None,
    title: str | None = None,
    status: str | None = None,
    superseded_by: str | None = None,
    priority: str | None = None,
    assumption: str | None = None,
    assumption_status: str | None = None,
) -> dict[str, Any] | None:
    """Patch any combination of body / category / title / status / superseded_by /
    priority / assumption / assumption_status.

    Use ``status='superseded'`` + ``superseded_by=<new_id>`` to retire a
    decision while preserving the audit trail. Pass only the fields you
    intend to change; others stay untouched.

    366317e9 — when ``body`` is patched to a *different* value, the previous
    body is appended to the append-only ``edit_log`` JSON array as
    ``{"body": <previous body>, "ts": <iso timestamp>}`` BEFORE the row is
    overwritten. History is never dropped; multiple edits accumulate.
    ``priority`` is normalized to {urgent, normal, low} (invalid → 'normal').
    """
    from datetime import datetime, timezone
    existing = await get_pinned_decision(db, decision_id)
    if existing is None:
        return None
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    fields: dict[str, Any] = {}
    if body is not None:
        from meridian.secret_redaction import check_for_secrets
        check_for_secrets(body, context="decision body")
        fields["body"] = body
        # Append-only edit history: snapshot the prior body before overwriting,
        # but only when the body actually changed (no-op edits don't pollute it).
        if body != existing.get("body"):
            history = _parse_decision_edit_log(existing.get("edit_log"))
            history.append({"body": existing.get("body"), "ts": now_iso})
            fields["edit_log"] = json.dumps(history)
    if title is not None:
        fields["title"] = title
    if category is not None:
        fields["category"] = category
    if priority is not None:
        fields["priority"] = _normalize_decision_priority(priority)
    if status is not None:
        if status not in ("active", "superseded"):
            raise ValueError("status must be 'active' or 'superseded'")
        fields["status"] = status
    if superseded_by is not None:
        fields["superseded_by"] = superseded_by
    # 2b39549d — assumption text + validation state.
    if assumption is not None:
        fields["assumption"] = assumption.strip() or None
    if assumption_status is not None:
        if assumption_status not in ("unvalidated", "confirmed", "invalidated"):
            raise ValueError(
                "assumption_status must be unvalidated|confirmed|invalidated"
            )
        fields["assumption_status"] = assumption_status
    if not fields:
        return existing
    fields["updated_at"] = now_iso
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    args = list(fields.values()) + [decision_id]
    await db.execute(
        f"UPDATE decisions_pinned SET {set_clause} WHERE id = ?", args
    )
    await db.commit()
    return await get_pinned_decision(db, decision_id)


async def supersede_pinned_decision(
    db: aiosqlite.Connection,
    old_decision_id: str,
    new_title: str,
    new_body: str,
    category: str | None = None,
    priority: str | None = None,
) -> dict[str, Any]:
    """Atomic supersede: create a new active decision and mark the old as superseded.

    Returns the new decision row. The old row keeps the back-link via
    ``superseded_by`` so the dashboard can render the chain. The new row inherits
    the old decision's priority unless ``priority`` overrides it (366317e9).
    """
    old = await get_pinned_decision(db, old_decision_id)
    if old is None:
        raise ValueError("decision not found")
    new = await pin_decision(
        db,
        old["project_id"],
        new_title,
        new_body,
        category or old.get("category", "TECHNICAL"),
        priority or old.get("priority", "normal"),
    )
    await update_pinned_decision(
        db, old_decision_id, status="superseded", superseded_by=new["id"]
    )
    return new


async def count_decisions(db: aiosqlite.Connection, project_id: str) -> int:
    """Return number of active pinned decisions for a project."""
    async with db.execute(
        "SELECT COUNT(*) FROM decisions_pinned WHERE project_id = ? AND status = 'active'",
        (project_id,),
    ) as cur:
        row = await cur.fetchone()
    if not row:
        return 0
    return (row["count"] if isinstance(row, dict) else row[0]) or 0


async def delete_pinned_decision(
    db: aiosqlite.Connection, decision_id: str
) -> bool:
    """Hard-delete a pinned decision by id. Returns True if deleted, False if not found."""
    cur = await db.execute(
        "DELETE FROM decisions_pinned WHERE id = ?", (decision_id,)
    )
    await db.commit()
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# v2.4 — HITL (human-in-the-loop) request queue
# ---------------------------------------------------------------------------

_VALID_HITL_URGENCY = {"normal", "high", "blocking"}
_VALID_HITL_STATUS = {"pending", "answered", "dismissed"}


async def request_hitl(
    db: aiosqlite.Connection,
    project_id: str,
    question: str,
    *,
    session_id: str | None = None,
    context: str | None = None,
    urgency: str = "normal",
    assigned_to: str | None = None,
    kind: str = "question",
    payload: str | None = None,
    options: list[str] | None = None,
    recommended: str | int | None = None,
    require_human: bool = False,
    blocker_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a HITL request. Returns the inserted row.

    Sessions paused on ``urgency='blocking'`` should poll
    :func:`get_hitl_request` until ``status='answered'`` to receive
    the human's reply. Non-blocking requests still show up in the
    dashboard queue — callers can keep working and check later.

    ``options`` renders selectable answer buttons in the dashboard. ``recommended``
    (an option string, or a 0-based index into ``options``) flags the safe default:
    the dashboard highlights it with a "(recommended)" badge + border and Enter
    submits it, and an auto-answer picks it instead of the first option. Explicit
    ``payload`` JSON, when given, is merged with the options/recommended fields.

    ``blocker_context`` (0d0cada7, optional) — structured scheduler-lock
    diagnostics (``resource``, ``item_id``, ``holder_session_id``,
    ``lease_expiry``, ``claim_granularity``, ``retry_after``, ``wait_reason``,
    ``plan_generation`` — any subset; unknown/extra keys are dropped) merged
    into ``payload["blocker"]`` so it is durably queryable via
    ``get_hitl_request``/``list_hitl_requests`` ("the Meridian HITL/blocker
    APIs"). This is the TRACKED counterpart to "do not open an untracked
    native HITL for ordinary lock contention": pass ``kind="scheduler_blocker"``
    and leave ``require_human`` False for routine contention an executor
    should poll through with bounded backoff (see the returned
    ``retry_after``); reserve ``require_human=True`` — independent of this
    parameter, unchanged from its existing meaning — for what the scheduler
    contract actually calls a genuine escalation: a real human decision, a
    stale-lease ownership ambiguity, or a destructive action. Passing
    ``blocker_context`` never changes ``require_human``'s default (False) or
    the auto-answer eligibility rules on its own — those still key off
    ``kind``/``question`` exactly as before.
    """
    if urgency not in _VALID_HITL_URGENCY:
        raise ValueError(
            f"urgency must be one of {sorted(_VALID_HITL_URGENCY)}; got {urgency!r}"
        )
    # cd134cf1 — fold options + recommended into the payload JSON. e43e6941 —
    # also persist require_human there (no migration) so the dashboard can flag a
    # human-only request and the no-auto-answer rule survives a reload.
    # 0d0cada7 — blocker_context folds in alongside them (additive; a caller
    # supplying none of these three sees byte-for-byte the same payload as
    # before this parameter existed).
    if options is not None or recommended is not None or require_human or blocker_context:
        try:
            _pl = json.loads(payload) if payload else {}
            if not isinstance(_pl, dict):
                _pl = {}
        except (TypeError, ValueError):
            _pl = {}
        _opts = [str(o) for o in options] if options is not None else _pl.get("options")
        if _opts is not None:
            _pl["options"] = _opts
        _rec = _resolve_recommended_option(_opts, recommended)
        if _rec is not None:
            _pl["recommended"] = _rec
        if require_human:
            _pl["require_human"] = True
        if blocker_context:
            _blocker_fields = (
                "resource", "item_id", "holder_session_id", "lease_expiry",
                "claim_granularity", "retry_after", "wait_reason", "plan_generation",
            )
            _pl["blocker"] = {
                k: blocker_context[k] for k in _blocker_fields if k in blocker_context
            }
        payload = json.dumps(_pl)
    hid = _new_id()
    await db.execute(
        "INSERT INTO hitl_requests "
        "(id, project_id, session_id, question, context, urgency, assigned_to, "
        "kind, payload) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (hid, project_id, session_id, question, context, urgency, assigned_to,
         kind, payload),
    )
    await db.commit()
    # ITEM 6 — live push so the HITL queue refreshes without polling.
    _publish_project_event(project_id, "hitl_filed", {"hitl_id": hid, "kind": kind})
    # e43e6941 — require_human forbids auto-answer entirely; reserve it for
    # genuinely irreversible/destructive actions (token rotation, data migrations,
    # rollbacks) so the auto-answer can never approve something that can't be
    # undone. Otherwise apply the per-project 3-way mode (035edf47); an
    # auto-answered row stays in the queue (status='answered', answered_by='auto')
    # for audit. See _hitl_should_auto_answer for the rules.
    if not require_human:
        _aa_mode = await _project_hitl_auto_answer_mode(db, project_id)
        if _hitl_should_auto_answer(_aa_mode, kind, question):
            auto = _auto_hitl_answer(payload)
            answered = await answer_hitl_request(db, hid, auto, answered_by="auto")
            if answered is not None:
                return answered
    return (await get_hitl_request(db, hid)) or {"id": hid}


async def _project_hitl_auto_answer_mode(
    db: aiosqlite.Connection, project_id: str
) -> int:
    """Per-project HITL auto-answer mode: 0=off, 1=safe, 2=aggressive."""
    async with db.execute(
        "SELECT hitl_auto_answer FROM projects WHERE id = ?", (project_id,)
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return 0
    data = _row_to_dict(row) or {}
    try:
        return max(0, min(2, int(data.get("hitl_auto_answer") or 0)))
    except (TypeError, ValueError):
        return 1 if data.get("hitl_auto_answer") else 0


# 035edf47 — keyword guards for the 3-way HITL auto-answer modes.
_HITL_DESTRUCTIVE_KEYWORDS = (
    "delete", "drop ", "truncate", "destroy", "wipe", "purge", "nuke",
    "production", "prod ", "remove", "revoke", "rm -rf", "force push",
    "force-push", "overwrite", "reset --hard",
)
_HITL_SECURITY_KEYWORDS = (
    "security", "secret", "credential", "password", "api key", "apikey",
    "private key", "token", "encrypt", "permission", "vulnerab", "auth ",
)


def _hitl_should_auto_answer(mode: int, kind: str, question: str) -> bool:
    """Decide whether a HITL of ``kind``/``question`` auto-answers under ``mode``.

    0 (off): never. 1 (safe): only an executor ``question`` with no destructive
    keyword — correction / md_section_update / hook_cwd_mismatch stay human-gated.
    2 (aggressive): everything except ``correction`` and security-sensitive text.
    """
    if mode <= 0:
        return False
    q = (question or "").lower()
    if mode == 1:  # safe
        if (kind or "question") != "question":
            return False
        return not any(k in q for k in _HITL_DESTRUCTIVE_KEYWORDS)
    # mode >= 2: aggressive
    if (kind or "") == "correction":
        return False
    return not any(k in q for k in _HITL_SECURITY_KEYWORDS)


def _resolve_recommended_option(
    options: list[str] | None, recommended: str | int | None
) -> str | None:
    """Resolve ``recommended`` (option string or 0-based index) to an option string.

    Returns None when there's nothing valid to recommend. A bool is rejected
    (``True``/``False`` are ints in Python but never a meaningful option index).
    """
    if recommended is None or isinstance(recommended, bool):
        return None
    if isinstance(recommended, int):
        if options and 0 <= recommended < len(options):
            return str(options[recommended])
        return None
    rec = str(recommended)
    if options and rec in {str(o) for o in options}:
        return rec
    # A free-text recommendation with no options list is still meaningful.
    return rec if not options else None


def _auto_hitl_answer(payload: str | None) -> str:
    """Derive the auto-answer string. Prefer an explicit ``recommended`` option;
    else the first of a non-empty ``options`` list; else a generic ack."""
    if payload:
        try:
            parsed = json.loads(payload)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
            rec = parsed.get("recommended")
            if isinstance(rec, str) and rec:
                return rec
            options = parsed.get("options")
            if isinstance(options, list) and options:
                return str(options[0])
    return "[auto-answered]"


async def get_hitl_request(
    db: aiosqlite.Connection, request_id: str
) -> dict[str, Any] | None:
    async with db.execute(
        "SELECT * FROM hitl_requests WHERE id = ?", (request_id,)
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


async def list_hitl_requests(
    db: aiosqlite.Connection,
    project_id: str | None = None,
    *,
    status: str | None = "pending",
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Return HITL requests, newest first.

    ``project_id=None`` returns across all projects — used by the
    dashboard's top-level HITL panel. ``status=None`` returns every
    status; default ``'pending'`` shows only the active queue.
    ``status='recent'`` returns pending + answered/dismissed in the last 24h.
    """
    # dcf1e428 — 'recent' pseudo-status: pending OR resolved in last 24h
    if status == "recent":
        from datetime import datetime, timezone, timedelta
        cutoff_24h = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
        where = []
        args: list[Any] = []
        if project_id is not None:
            where.append("project_id = ?")
            args.append(project_id)
        proj_clause = (" AND ".join(where) + " AND ") if where else ""
        args.extend([cutoff_24h, limit])
        sql = (
            f"SELECT * FROM hitl_requests WHERE {proj_clause}"
            "(status = 'pending' OR "
            " (status IN ('answered', 'dismissed') AND "
            "  COALESCE(answered_at, created_at) >= ?)) "
            "ORDER BY "
            "  CASE urgency WHEN 'blocking' THEN 0 WHEN 'high' THEN 1 ELSE 2 END, "
            "  created_at DESC LIMIT ?"
        )
        async with db.execute(sql, args) as cur:
            rows = await cur.fetchall()
        return [_row_to_dict(r) for r in rows if r is not None]  # type: ignore[misc]
    if status is not None and status not in _VALID_HITL_STATUS:
        raise ValueError(
            f"status must be one of {sorted(_VALID_HITL_STATUS)} or None"
        )
    where2 = []
    args2: list[Any] = []
    if project_id is not None:
        where2.append("project_id = ?")
        args2.append(project_id)
    if status is not None:
        where2.append("status = ?")
        args2.append(status)
    where_clause = (" WHERE " + " AND ".join(where2)) if where2 else ""
    args2.append(limit)
    sql = (
        f"SELECT * FROM hitl_requests{where_clause} "
        "ORDER BY "
        "  CASE urgency WHEN 'blocking' THEN 0 WHEN 'high' THEN 1 ELSE 2 END, "
        "  created_at DESC LIMIT ?"
    )
    async with db.execute(sql, args2) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows if r is not None]  # type: ignore[misc]


async def answer_hitl_request(
    db: aiosqlite.Connection,
    request_id: str,
    answer: str,
    answered_by: str | None = None,
) -> dict[str, Any] | None:
    """Mark a HITL request answered. Sessions polling for the answer pick it up."""
    from datetime import datetime, timezone
    now_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    await db.execute(
        "UPDATE hitl_requests SET status = 'answered', answer = ?, "
        "answered_by = ?, answered_at = ? WHERE id = ?",
        (answer, answered_by, now_ts, request_id),
    )
    await db.commit()
    return await get_hitl_request(db, request_id)


async def dismiss_hitl_request(
    db: aiosqlite.Connection, request_id: str
) -> dict[str, Any] | None:
    """Mark a HITL request dismissed (won't-answer). Stays in audit trail."""
    await db.execute(
        "UPDATE hitl_requests SET status = 'dismissed' WHERE id = ?",
        (request_id,),
    )
    await db.commit()
    return await get_hitl_request(db, request_id)


def _hitl_payload_flag(payload: Any, key: str) -> bool:
    """True if the HITL payload JSON has a truthy ``key``. Tolerant of a
    None/non-JSON/non-dict payload (returns False)."""
    try:
        pl = json.loads(payload) if isinstance(payload, str) else (payload or {})
    except (TypeError, ValueError):
        return False
    return bool(isinstance(pl, dict) and pl.get(key))


async def get_recoverable_hitl_answers(
    db: aiosqlite.Connection,
    project_id: str,
    *,
    active_session_ids: "set[str] | None" = None,
    within_hours: int = 48,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """9dad83fd — answered **blocking** HITLs whose originating session is no
    longer live, so a resuming session can pick up an answer that would otherwise
    be lost (the dead session was the only poller). Excludes ones already handed
    to a recovery surface (a ``recovered_delivered`` JSON-payload flag — no schema
    change) and anything answered more than ``within_hours`` ago."""
    from datetime import datetime, timezone, timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=within_hours)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    async with db.execute(
        "SELECT * FROM hitl_requests WHERE project_id = ? AND urgency = 'blocking' "
        "AND status = 'answered' AND COALESCE(answered_at, created_at) >= ? "
        "ORDER BY COALESCE(answered_at, created_at) DESC LIMIT ?",
        (project_id, cutoff, limit * 4),
    ) as cur:
        rows = await cur.fetchall()
    active = active_session_ids or set()
    out: list[dict[str, Any]] = []
    for r in rows:
        row = _row_to_dict(r)
        if row is None:
            continue
        sid = row.get("session_id")
        # A still-live originating session will consume its own answer.
        if sid and sid in active:
            continue
        if _hitl_payload_flag(row.get("payload"), "recovered_delivered"):
            continue
        out.append(row)
        if len(out) >= limit:
            break
    return out


async def mark_hitl_recovery_delivered(
    db: aiosqlite.Connection, request_id: str
) -> None:
    """9dad83fd — set a ``recovered_delivered`` payload flag so a recovered
    blocking-HITL answer is surfaced to a resuming session exactly once
    (idempotent; JSON payload, no schema change)."""
    row = await get_hitl_request(db, request_id)
    if not row:
        return
    try:
        pl = json.loads(row.get("payload")) if row.get("payload") else {}
        if not isinstance(pl, dict):
            pl = {}
    except (TypeError, ValueError):
        pl = {}
    pl["recovered_delivered"] = True
    await db.execute(
        "UPDATE hitl_requests SET payload = ? WHERE id = ?",
        (json.dumps(pl), request_id),
    )
    await db.commit()


# ---------------------------------------------------------------------------
# v2.4 — Project token for webhook intake (framework integrations)
# ---------------------------------------------------------------------------


async def ensure_project_token(
    db: aiosqlite.Connection, project_id: str
) -> str | None:
    """Return the project's webhook token, minting one if not yet set.

    Returns ``None`` if the project doesn't exist. Tokens are 32 bytes
    of url-safe base64 (~43 chars). Stored plain — these tokens grant
    write-access to a single project's task_log only, not to tenants.
    """
    project = await get_project(db, project_id)
    if project is None:
        return None
    token = project.get("project_token")
    if token:
        return token
    import secrets
    token = "pt_" + secrets.token_urlsafe(32)
    await db.execute(
        "UPDATE projects SET project_token = ? WHERE id = ?",
        (token, project_id),
    )
    await db.commit()
    return token


# ---------------------------------------------------------------------------
# v3.0 — Executor runs: one row per session execution, with transcript
# ---------------------------------------------------------------------------


async def create_executor_run(
    db: aiosqlite.Connection,
    session_id: str,
    project_id: str,
) -> dict[str, Any]:
    """Create a new executor_run row for a session. Called at session start."""
    rid = _new_id()
    await db.execute(
        "INSERT INTO executor_runs (id, session_id, project_id) VALUES (?, ?, ?)",
        (rid, session_id, project_id),
    )
    await db.commit()
    async with db.execute("SELECT * FROM executor_runs WHERE id = ?", (rid,)) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)  # type: ignore[return-value]


async def _get_active_run_id(
    db: aiosqlite.Connection, session_id: str
) -> str | None:
    """Return the id of the active (running) executor_run for a session, if any."""
    async with db.execute(
        "SELECT id FROM executor_runs WHERE session_id = ? AND status = 'running' "
        "ORDER BY started_at DESC LIMIT 1",
        (session_id,),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    return row["id"] if isinstance(row, dict) else row[0]


async def append_executor_run_transcript(
    db: aiosqlite.Connection,
    session_id: str,
    entry: str,
) -> None:
    """Append a line to the running executor_run's transcript. No-op if no active run."""
    run_id = await _get_active_run_id(db, session_id)
    if run_id is None:
        return
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {entry}\n"
    await db.execute(
        "UPDATE executor_runs SET transcript = transcript || ?, task_count = task_count + 1 "
        "WHERE id = ?",
        (line, run_id),
    )
    await db.commit()


async def finalize_executor_run(
    db: aiosqlite.Connection,
    session_id: str,
    status: str = "done",
) -> None:
    """Mark the active executor_run for a session as finished."""
    run_id = await _get_active_run_id(db, session_id)
    if run_id is None:
        return
    from datetime import datetime, timezone
    now_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    await db.execute(
        "UPDATE executor_runs SET status = ?, ended_at = ? WHERE id = ?",
        (status, now_ts, run_id),
    )
    await db.commit()


async def get_executor_runs(
    db: aiosqlite.Connection,
    project_id: str,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """List executor_runs for a project, newest first."""
    async with db.execute(
        "SELECT er.*, s.name AS session_name, s.status AS session_status FROM executor_runs er "
        "LEFT JOIN sessions s ON s.id = er.session_id "
        "WHERE er.project_id = ? "
        "ORDER BY er.started_at DESC LIMIT ?",
        (project_id, limit),
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows if r is not None]  # type: ignore[misc]


async def get_executor_run(
    db: aiosqlite.Connection,
    run_id: str,
) -> dict[str, Any] | None:
    """Return a single executor_run by id."""
    async with db.execute(
        "SELECT * FROM executor_runs WHERE id = ?", (run_id,)
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


async def get_executor_run_by_session(
    db: aiosqlite.Connection,
    session_id: str,
) -> dict[str, Any] | None:
    """Return the most recent executor_run for a session (any status)."""
    async with db.execute(
        "SELECT * FROM executor_runs WHERE session_id = ? "
        "ORDER BY started_at DESC LIMIT 1",
        (session_id,),
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


# ---------------------------------------------------------------------------
# 8c147109 — session_activity: lightweight ring-buffer heartbeat feed
# ---------------------------------------------------------------------------

_SESSION_ACTIVITY_RING_SIZE = 50  # max entries retained per session


async def record_session_activity(
    db: aiosqlite.Connection,
    session_id: str,
    tool_name: str,
    summary: str,
) -> None:
    """Append one activity entry and prune the oldest rows beyond the ring size.

    Called by the MCP tool dispatcher on every executor tool call so a remote
    planner session can observe signs of life via get_session_log even before
    the executor calls log_task(). Best-effort: callers must wrap in try/except.

    Ordering tie-break: rowid (SQLite implicit integer PK) increases with each
    INSERT, so ORDER BY recorded_at DESC, rowid DESC is stable under concurrent
    sub-second inserts where timestamps collide.
    """
    from datetime import datetime, timezone  # noqa: PLC0415
    entry_id = _new_id()
    now_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    await db.execute(
        "INSERT INTO session_activity (id, session_id, tool_name, summary, recorded_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (entry_id, session_id, tool_name, summary[:200], now_ts),
    )
    # Prune rows beyond the ring-size limit — delete all but the most recent N.
    # Tie-break sub-second batches (recorded_at collisions) deterministically:
    # SQLite has an implicit rowid; Postgres has no equivalent pseudo-column, so
    # use ctid (physical row location) instead — this table is INSERT-only
    # (never UPDATEd), so ctid order tracks insertion order just like rowid does.
    tiebreak = "ctid" if hasattr(db, "_pool") else "rowid"
    await db.execute(
        f"DELETE FROM session_activity WHERE session_id = ? AND {tiebreak} NOT IN ("
        f"  SELECT {tiebreak} FROM session_activity WHERE session_id = ? "
        f"  ORDER BY recorded_at DESC, {tiebreak} DESC LIMIT ?"
        f")",
        (session_id, session_id, _SESSION_ACTIVITY_RING_SIZE),
    )
    await db.commit()


async def get_session_activity(
    db: aiosqlite.Connection,
    session_id: str,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Return the most recent activity entries for a session, newest first.

    Uses the backend-appropriate pseudo-column as a secondary sort key so
    sub-second inserts (same recorded_at) are returned in stable insertion
    order (newest = highest rowid/ctid first). rowid is SQLite's implicit
    integer PK; ctid is Postgres's physical row location — this table is
    INSERT-only so ctid order tracks insertion order identically.

    8c147109 / ordering fix: the companion record_session_activity already
    uses ctid/rowid in the pruning DELETE; get_session_activity now mirrors
    that so both paths agree on ordering across backends.
    """
    tiebreak = "ctid" if hasattr(db, "_pool") else "rowid"
    async with db.execute(
        f"SELECT * FROM session_activity WHERE session_id = ? "
        f"ORDER BY recorded_at DESC, {tiebreak} DESC LIMIT ?",
        (session_id, limit),
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows if r is not None]  # type: ignore[misc]


# ---------------------------------------------------------------------------
# b12cc29f — connection_events: per-/mcp-request auth+method event log.
#
# Every HTTP POST /mcp the server actually receives gets written here so a
# live or post-mortem client-side outage (Claude Desktop showing zero tools,
# auth failures) can be diagnosed without raw Fly.io log access or guessing
# by elimination.
# ---------------------------------------------------------------------------

_CONNECTION_EVENTS_RING_SIZE = 1000  # max entries retained per tenant_id


async def record_connection_event(
    db: aiosqlite.Connection,
    *,
    tenant_id: str | None,
    method: str,
    auth_result: str,
    tools_returned: int | None = None,
    client_user_agent: str | None = None,
    response_status: int = 200,
) -> None:
    """Write one connection-event row and prune the oldest beyond the ring size.

    Best-effort: callers must wrap in try/except so a logging failure never
    breaks a real /mcp response. Pruning is per-(tenant_id IS NOT NULL) bucket;
    unauthenticated events (tenant_id=NULL) use a separate NULL bucket capped
    at the same size.

    auth_result vocabulary:
      success        — valid API token or OAuth token accepted
      oauth          — OAuth bearer token accepted (sub-type of success)
      no_token       — no Authorization header present
      invalid_token  — bearer present but not found in DB
      expired        — OAuth token found but past expiry
      parse_error    — request body was unparseable JSON
    """
    from datetime import datetime, timezone  # noqa: PLC0415
    entry_id = _new_id()
    now_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    ua = (client_user_agent or "")[:200] or None
    await db.execute(
        "INSERT INTO connection_events "
        "(id, tenant_id, method, auth_result, tools_returned, client_user_agent, "
        " response_status, recorded_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (entry_id, tenant_id, method[:100], auth_result[:40],
         tools_returned, ua, response_status, now_ts),
    )
    # Prune: keep only the most recent N rows for this tenant bucket.
    # NULL tenant_id is a valid bucket (unauthenticated/failed attempts).
    tiebreak = "ctid" if hasattr(db, "_pool") else "rowid"
    if tenant_id is not None:
        await db.execute(
            f"DELETE FROM connection_events "
            f"WHERE tenant_id = ? AND {tiebreak} NOT IN ("
            f"  SELECT {tiebreak} FROM connection_events WHERE tenant_id = ? "
            f"  ORDER BY recorded_at DESC, {tiebreak} DESC LIMIT ?"
            f")",
            (tenant_id, tenant_id, _CONNECTION_EVENTS_RING_SIZE),
        )
    else:
        await db.execute(
            f"DELETE FROM connection_events "
            f"WHERE tenant_id IS NULL AND {tiebreak} NOT IN ("
            f"  SELECT {tiebreak} FROM connection_events WHERE tenant_id IS NULL "
            f"  ORDER BY recorded_at DESC, {tiebreak} DESC LIMIT ?"
            f")",
            (_CONNECTION_EVENTS_RING_SIZE,),
        )
    await db.commit()


async def get_connection_log(
    db: aiosqlite.Connection,
    tenant_id: str | None = None,
    since: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Return recent connection events, newest first.

    When ``tenant_id`` is given, scopes to that tenant's events only.
    ``since`` is an ISO timestamp string — only events at or after this time
    are returned. ``limit`` caps the result set (max 500).
    """
    limit = min(limit, 500)
    params: list[Any] = []
    clauses: list[str] = []
    if tenant_id is not None:
        clauses.append("tenant_id = ?")
        params.append(tenant_id)
    if since:
        clauses.append("recorded_at >= ?")
        params.append(since)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    async with db.execute(
        f"SELECT * FROM connection_events {where} "
        f"ORDER BY recorded_at DESC LIMIT ?",
        params,
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows if r is not None]  # type: ignore[misc]


# ---------------------------------------------------------------------------
# f0a48685 — server_logs: application-wide WARNING/ERROR/EXCEPTION log capture.
#
# A custom logging.Handler writes qualifying records here so any session
# (including hosted-only claude.ai sessions without local machine access) can
# inspect server-side errors without raw Fly.io log access.  Kept separate from
# connection_events: connection_events is one structured row per /mcp HTTP
# request; server_logs is one row per arbitrary application log record.
# ---------------------------------------------------------------------------

_SERVER_LOGS_RING_SIZE = 2000  # global cap — not per-tenant


async def record_server_log(
    db: aiosqlite.Connection,
    *,
    level: str,
    logger: str,
    message: str,
    exc_text: str | None = None,
) -> None:
    """Write one server_log row and prune the oldest beyond the ring size.

    Best-effort: callers must wrap in try/except so a persistence failure
    never propagates back through the logging framework and into the original
    call site.

    level vocabulary: 'WARNING', 'ERROR', 'EXCEPTION'
    """
    from datetime import datetime, timezone  # noqa: PLC0415
    entry_id = _new_id()
    now_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    await db.execute(
        "INSERT INTO server_logs (id, level, logger, message, exc_text, recorded_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            entry_id,
            level[:20],
            logger[:200],
            message[:2000],
            exc_text[:4000] if exc_text else None,
            now_ts,
        ),
    )
    # Prune: keep only the most recent N rows globally.
    tiebreak = "ctid" if hasattr(db, "_pool") else "rowid"
    await db.execute(
        f"DELETE FROM server_logs WHERE {tiebreak} NOT IN ("
        f"  SELECT {tiebreak} FROM server_logs "
        f"  ORDER BY recorded_at DESC, {tiebreak} DESC LIMIT ?"
        f")",
        (_SERVER_LOGS_RING_SIZE,),
    )
    await db.commit()


async def get_server_logs(
    db: aiosqlite.Connection,
    since: str | None = None,
    limit: int = 100,
    level_filter: str | None = None,
    module_filter: str | None = None,
) -> list[dict[str, Any]]:
    """Return recent server log entries, newest first.

    ``since`` is an ISO timestamp string — only entries at or after this time
    are returned. ``limit`` caps the result set (max 500). ``level_filter``
    restricts to a specific level (e.g. 'ERROR'). ``module_filter`` is a
    substring match against the logger name.
    """
    limit = min(limit, 500)
    params: list[Any] = []
    clauses: list[str] = []
    if since:
        clauses.append("recorded_at >= ?")
        params.append(since)
    if level_filter:
        clauses.append("level = ?")
        params.append(level_filter.upper()[:20])
    if module_filter:
        clauses.append("logger LIKE ?")
        params.append(f"%{module_filter}%")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    async with db.execute(
        f"SELECT * FROM server_logs {where} "
        f"ORDER BY recorded_at DESC LIMIT ?",
        params,
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows if r is not None]  # type: ignore[misc]


async def get_project_by_token(
    db: aiosqlite.Connection, token: str
) -> dict[str, Any] | None:
    """Look up a project by its webhook token (for POST /events auth)."""
    async with db.execute(
        "SELECT * FROM projects WHERE project_token = ?", (token,)
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


# ---------------------------------------------------------------------------
# v2.4 — Per-human team summary (Team tab data source)
# ---------------------------------------------------------------------------


async def get_team_summary(
    db: aiosqlite.Connection,
    project_id: str | None = None,
    days: int = 1,
) -> dict[str, Any]:
    """Aggregate task_log + sessions by human_id over the last N days.

    Returns ``{"period_days": N, "humans": [{"human_id", "tasks_done",
    "tasks_failed", "tasks_pending", "last_seen", "active_session",
    "recent": [...]}], "active_count": N}``. Powers the dashboard Team
    tab — live presence cards, standup digests, swimlane data source.
    """
    from datetime import datetime, timezone, timedelta
    since = (
        datetime.now(timezone.utc) - timedelta(days=days)
    ).strftime("%Y-%m-%d %H:%M:%S")

    proj_clause = "AND s.project_id = ?" if project_id else ""
    proj_args: tuple[Any, ...] = (project_id,) if project_id else ()

    # Pull sessions joined with tasks (over the window) grouped by human_id.
    sql = f"""
        SELECT
            s.human_id as human_id,
            s.id as session_id,
            s.name as session_name,
            s.status as session_status,
            s.last_seen as last_seen,
            s.agent_framework as agent_framework,
            t.id as task_id,
            t.description as task_description,
            t.status as task_status,
            t.created_at as task_created_at
        FROM sessions s
        LEFT JOIN task_log t
            ON t.session_id = s.id AND t.created_at >= ?
        WHERE s.human_id IS NOT NULL {proj_clause}
        ORDER BY s.last_seen DESC, t.created_at DESC
    """
    async with db.execute(sql, (since, *proj_args)) as cur:
        rows = await cur.fetchall()

    humans: dict[str, dict[str, Any]] = {}
    for r in rows:
        rd = _row_to_dict(r) or {}
        hid = rd.get("human_id")
        if not hid:
            continue
        bucket = humans.setdefault(
            hid,
            {
                "human_id": hid,
                "tasks_done": 0,
                "tasks_failed": 0,
                "tasks_pending": 0,
                "last_seen": rd.get("last_seen"),
                "active_session": rd.get("session_name"),
                "active_session_id": rd.get("session_id"),
                "session_status": rd.get("session_status"),
                "agent_framework": rd.get("agent_framework") or "claude_code",
                "recent": [],
            },
        )
        # First row per human (sorted by last_seen DESC) already gives us
        # the most recent session — don't overwrite.
        status = rd.get("task_status")
        if status == "done":
            bucket["tasks_done"] += 1
        elif status == "failed":
            bucket["tasks_failed"] += 1
        elif status in ("pending", "pending-hitl", "in_progress"):
            bucket["tasks_pending"] += 1
        desc = rd.get("task_description")
        if desc and len(bucket["recent"]) < 100:
            bucket["recent"].append(
                {
                    "description": desc,
                    "status": status,
                    "created_at": rd.get("task_created_at"),
                }
            )

    now_utc = datetime.now(timezone.utc)
    active_count = 0
    for h in humans.values():
        last_seen = h.get("last_seen") or ""
        try:
            ls_dt = datetime.fromisoformat(last_seen.replace(" ", "T")).replace(
                tzinfo=timezone.utc
            )
            age = (now_utc - ls_dt).total_seconds()
            if age < 300:
                h["presence"] = "active"
                active_count += 1
            elif age < 1800:
                h["presence"] = "recent"
            else:
                h["presence"] = "idle"
        except (ValueError, AttributeError):
            h["presence"] = "idle"

    return {
        "period_days": days,
        "humans": sorted(humans.values(), key=lambda x: x.get("last_seen") or "", reverse=True),
        "active_count": active_count,
    }


# ---------------------------------------------------------------------------
# v0.9 — project_notes: per-project wiki
# ---------------------------------------------------------------------------


async def _unique_note_slug(
    db: aiosqlite.Connection,
    project_id: str,
    base: str,
    exclude_id: str | None = None,
) -> str:
    """Return ``base``, or base-2/base-3/… if the slug is taken in this project.

    Slugs are unique *per project* (Obsidian ``mem:name`` style), so the same
    slug may exist under different projects. ``exclude_id`` lets a row keep its
    own slug on update without colliding with itself.
    """
    slug = base
    n = 1
    while True:
        async with db.execute(
            "SELECT id FROM project_notes WHERE project_id = ? AND slug = ?",
            (project_id, slug),
        ) as cur:
            row = await cur.fetchone()
        existing = _row_to_dict(row)
        if existing is None or existing.get("id") == exclude_id:
            return slug
        n += 1
        slug = f"{base}-{n}"


async def _unique_note_nickname(
    db: aiosqlite.Connection,
    project_id: str,
    base: str,
    exclude_id: str | None = None,
) -> str:
    """6fb48898 — return ``base``, or base-2/base-3/… if the nickname is taken
    in this project. Mirrors _unique_sprint_nickname for project_notes.
    """
    nickname = base
    n = 1
    while True:
        async with db.execute(
            "SELECT id FROM project_notes WHERE project_id = ? AND nickname = ?",
            (project_id, nickname),
        ) as cur:
            row = await cur.fetchone()
        existing = _row_to_dict(row)
        if existing is None or existing.get("id") == exclude_id:
            return nickname
        n += 1
        nickname = f"{base}-{n}"


async def _unique_decision_slug(
    db: aiosqlite.Connection,
    project_id: str,
    base: str,
    exclude_id: str | None = None,
) -> str:
    """6fb48898 — return ``base``, or base-2/base-3/… if the slug is taken in
    this project's decisions_pinned table. Unique per project.
    """
    slug = base
    n = 1
    while True:
        async with db.execute(
            "SELECT id FROM decisions_pinned WHERE project_id = ? AND slug = ?",
            (project_id, slug),
        ) as cur:
            row = await cur.fetchone()
        existing = _row_to_dict(row)
        if existing is None or existing.get("id") == exclude_id:
            return slug
        n += 1
        slug = f"{base}-{n}"


async def _unique_decision_nickname(
    db: aiosqlite.Connection,
    project_id: str,
    base: str,
    exclude_id: str | None = None,
) -> str:
    """6fb48898 — return ``base``, or base-2/base-3/… if the nickname is taken
    in this project's decisions_pinned table. Unique per project.
    """
    nickname = base
    n = 1
    while True:
        async with db.execute(
            "SELECT id FROM decisions_pinned WHERE project_id = ? AND nickname = ?",
            (project_id, nickname),
        ) as cur:
            row = await cur.fetchone()
        existing = _row_to_dict(row)
        if existing is None or existing.get("id") == exclude_id:
            return nickname
        n += 1
        nickname = f"{base}-{n}"


async def _unique_proposal_slug(
    db: aiosqlite.Connection,
    tenant_id: str | None,
    base: str,
    exclude_id: str | None = None,
) -> str:
    """6fb48898 — return ``base``, or base-2/base-3/… if the slug is taken in
    this tenant's workspace_proposals table. Unique per tenant (NULL tenant
    treated as its own scope to match the workspace_proposals tenancy model).
    """
    slug = base
    n = 1
    while True:
        if tenant_id is not None:
            async with db.execute(
                "SELECT id FROM workspace_proposals WHERE tenant_id = ? AND slug = ?",
                (tenant_id, slug),
            ) as cur:
                row = await cur.fetchone()
        else:
            async with db.execute(
                "SELECT id FROM workspace_proposals WHERE tenant_id IS NULL AND slug = ?",
                (slug,),
            ) as cur:
                row = await cur.fetchone()
        existing = _row_to_dict(row)
        if existing is None or existing.get("id") == exclude_id:
            return slug
        n += 1
        slug = f"{base}-{n}"


async def _unique_proposal_nickname(
    db: aiosqlite.Connection,
    tenant_id: str | None,
    base: str,
    exclude_id: str | None = None,
) -> str:
    """6fb48898 — return ``base``, or base-2/base-3/… if the nickname is taken
    in this tenant's workspace_proposals table. Mirrors _unique_proposal_slug.
    """
    nickname = base
    n = 1
    while True:
        if tenant_id is not None:
            async with db.execute(
                "SELECT id FROM workspace_proposals WHERE tenant_id = ? AND nickname = ?",
                (tenant_id, nickname),
            ) as cur:
                row = await cur.fetchone()
        else:
            async with db.execute(
                "SELECT id FROM workspace_proposals WHERE tenant_id IS NULL AND nickname = ?",
                (nickname,),
            ) as cur:
                row = await cur.fetchone()
        existing = _row_to_dict(row)
        if existing is None or existing.get("id") == exclude_id:
            return nickname
        n += 1
        nickname = f"{base}-{n}"


async def add_project_note(
    db: aiosqlite.Connection,
    project_id: str,
    title: str,
    body: str,
    tags: str | None = None,
    kind: str | None = None,
    priority: str = "normal",
    file_path: str | None = None,
    symbol: str | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    """Insert a project_notes row. tags is comma-separated free-form.

    ``kind`` is the note taxonomy (wiki | insight | reference | code |
    document); NULL is treated as 'wiki' by readers. Unknown values are coerced
    to NULL so the column stays a closed vocabulary.

    e3f150d0 — ``source`` records where a note was ingested from (a URL or file
    path), set by ``ingest_document`` for ``kind='document'`` notes. Nullable;
    NULL for normal notes.

    ``priority`` is high/normal/low; defaults to 'normal'. High-priority notes
    are surfaced first in generate_handoff and get_session_brief (planner role).

    771c00d7 — a code-anchored note pins a warning/context to a file: pass
    ``kind='code'`` plus a non-empty ``file_path`` (and optional ``symbol``).
    The path is normalized the same way ``claim_file`` stores it so the anchor
    matches a claim, and the note surfaces automatically at claim_file /
    get_file_claims. ``file_path``/``symbol`` are NULL for normal notes.

    5a5bba43 — a kebab-cased ``slug`` is generated from the title and stored,
    unique per project (collisions get a ``-2``/``-3``/… suffix). The slug is the
    handle ``read_note(slug)`` and the dashboard's ``mem:name`` links resolve.

    6fb48898 — a short memorable ``nickname`` (1-2 words) is also generated and
    stored, unique per project, using the same algorithm as sprint_items.nickname.
    """
    if kind not in ("wiki", "insight", "reference", "code", "document"):
        kind = None
    if priority not in ("high", "normal", "low"):
        priority = "normal"
    from meridian.secret_redaction import check_for_secrets
    check_for_secrets(body, context="note body")
    stored_source = (source or "").strip() or None
    # 771c00d7 — code anchor: require a real path, normalize it to match claims,
    # and keep ``symbol`` only alongside a path. Anchors are independent of kind
    # so an explicitly anchored note still resolves even if kind was coerced.
    anchor_path = _normalize_file_path(file_path)
    if file_path is not None and not anchor_path:
        raise ValueError("file_path must be a non-empty string when provided")
    sym = (symbol or "").strip()
    stored_path = anchor_path or None
    stored_symbol = sym or None
    if stored_path is None:
        stored_symbol = None  # a symbol is meaningless without a file anchor
    nid = _new_id()
    slug = await _unique_note_slug(db, project_id, _slugify_note(title))
    # 6fb48898 — auto-generate a short memorable nickname alongside the slug.
    nickname = await _unique_note_nickname(
        db, project_id, _sprint_item_nickname_base(title, nid)
    )
    await db.execute(
        "INSERT INTO project_notes "
        "(id, project_id, title, body, tags, note_kind, priority, slug, nickname, "
        "file_path, symbol, source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (nid, project_id, title, body, tags, kind, priority, slug, nickname,
         stored_path, stored_symbol, stored_source),
    )
    await db.commit()
    # ITEM 6 — live push so the Notes tab refreshes without polling.
    _publish_project_event(project_id, "note_added", {"note_id": nid})
    return (await get_project_note(db, nid)) or {"id": nid}


async def get_project_note(
    db: aiosqlite.Connection, note_id: str
) -> dict[str, Any] | None:
    async with db.execute(
        "SELECT * FROM project_notes WHERE id = ?", (note_id,)
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


async def get_project_document_by_source(
    db: aiosqlite.Connection, project_id: str, source: str
) -> dict[str, Any] | None:
    """e9addcb0 — fetch a ``kind='document'`` note by its stable source identity.

    ``source`` is the document's stable handle — a file path or a Drive file id —
    set by :func:`ingest_document`. Re-ingesting the same document targets this
    row so an upsert refreshes it in place instead of creating a duplicate.

    Scoped to (``project_id``, ``source``, ``note_kind='document'``): a plain
    note that happens to share a source string is never matched. Returns the
    newest matching row (oldest are legacy pre-upsert duplicates) or None.
    """
    src = (source or "").strip()
    if not src:
        return None
    async with db.execute(
        "SELECT * FROM project_notes "
        "WHERE project_id = ? AND note_kind = 'document' AND source = ? "
        "ORDER BY created_at DESC, id DESC LIMIT 1",
        (project_id, src),
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


async def ingest_document(
    db: aiosqlite.Connection,
    project_id: str,
    file_path: str | None = None,
    content: str | None = None,
    title: str | None = None,
    source: str | None = None,
    tags: str | None = None,
) -> dict[str, Any]:
    """e3f150d0 — turn a document into a queryable ``kind='document'`` note.

    Body selection:
      * ``content`` given  → use it verbatim (the caller extracted the text, e.g.
        from a PDF with its own tooling — Meridian never parses PDFs server-side).
      * else ``file_path`` → extract the text server-side with the stdlib-only
        ``doc_ingest.extract_text`` (.txt/.md/.docx). Unsupported types (.pdf, …)
        raise, telling the caller to pass ``content`` instead.
      * neither             → ``ValueError``.

    Defaults: ``title`` falls back to the file's basename (or "Untitled
    document"); ``source`` falls back to ``file_path``. The body is capped at
    ``doc_ingest.DOC_BODY_MAX_CHARS`` (truncated with a clear "…[truncated]"
    marker if longer; the kept prefix stays full-text searchable). The note is
    stored via :func:`add_project_note` with ``kind='document'`` and ``source``.

    e9addcb0 — **upsert by stable identity.** When a ``source`` resolves (an
    explicit ``source``, else ``file_path``), re-ingesting the same document
    UPDATES the existing ``kind='document'`` note for this project in place
    (refreshing body / title / tags / ``updated_at``) instead of creating a
    duplicate — so re-touching a path or Drive file id yields one row, not N.
    When no source can be resolved (``content`` with no ``source``/``file_path``)
    the document is not identifiable, so it always inserts a fresh note — two
    anonymous ingests never silently merge.

    Meridian is a coordination store, not an LLM: this never summarizes. Pass a
    summary as ``content`` if you want one stored instead of the raw text.
    """
    from ..doc_ingest import extract_text, cap_body  # local import: avoid cycle

    if content is not None and str(content).strip() != "":
        body = content
        ingest_source = source if (source and source.strip()) else (file_path or None)
    elif file_path and str(file_path).strip():
        # Server-side, stdlib-only extraction. extract_text raises a clear
        # UnsupportedDocumentError for .pdf and other unparseable types.
        body = extract_text(file_path)
        ingest_source = source if (source and source.strip()) else file_path
    else:
        raise ValueError(
            "ingest_document requires either 'content' (pre-extracted text) or "
            "'file_path' (a .txt/.md/.docx file to extract server-side)"
        )

    # Title defaults to the file's basename, else a generic placeholder.
    doc_title = (title or "").strip()
    if not doc_title:
        if file_path and str(file_path).strip():
            doc_title = os.path.basename(file_path.strip()) or "Untitled document"
        else:
            doc_title = "Untitled document"

    capped = cap_body(body or "")
    stored_source = (ingest_source or "").strip() or None

    # e9addcb0 — upsert by stable identity: if this project already has a
    # kind='document' note for the same source, refresh it in place instead of
    # creating a duplicate. Only when a source resolves — an anonymous ingest
    # (content with no source/file_path) can't be identified, so it inserts.
    if stored_source is not None:
        existing = await get_project_document_by_source(db, project_id, stored_source)
        if existing is not None:
            # tags is passed through as-is: None (caller omitted it) leaves the
            # prior ingest's tags untouched; a value replaces them.
            updated = await update_project_note(
                db,
                existing["id"],
                title=doc_title,
                body=capped,
                tags=tags,
            )
            return updated or existing

    return await add_project_note(
        db,
        project_id,
        doc_title,
        capped,
        tags,
        kind="document",
        source=stored_source,
    )


def _project_notes_where(
    db: aiosqlite.Connection,
    project_id: str,
    tag: str | None,
    query: str | None,
) -> tuple[str, list[Any]]:
    """Build the shared WHERE clause + params for the project_notes filters.

    Factored out so get_project_notes and get_project_notes_page apply the
    exact same tag / full-text filtering before list vs. paginated fetch.
    """
    is_pg = hasattr(db, "_pool")
    clauses: list[str] = ["project_id = ?"]
    params: list[Any] = [project_id]
    if tag:
        clauses.append("tags LIKE ?")
        params.append(f"%{tag}%")
    if query:
        like_pat = f"%{query}%"
        if is_pg:
            clauses.append("(title ILIKE ? OR body ILIKE ?)")
        else:
            clauses.append("(title LIKE ? OR body LIKE ?)")
        params.extend([like_pat, like_pat])
    return " AND ".join(clauses), params


def _project_notes_cols(bodies: bool) -> str:
    """Column list for a notes fetch — full row when ``bodies`` else the
    lightweight id/slug/title/kind/priority/timestamps projection."""
    return (
        "*"
        if bodies
        else "id, project_id, title, tags, note_kind, priority, slug, "
        "created_at, updated_at"
    )


async def get_project_notes(
    db: aiosqlite.Connection,
    project_id: str,
    tag: str | None = None,
    query: str | None = None,
    bodies: bool = False,
    limit: int | None = None,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Return notes for a project, newest first. Optional tag filter (substring
    match on tags) and/or text query (searches title + body).

    5a5bba43 — pull model: by default (``bodies=False``) the returned rows OMIT
    the ``body`` field — a lightweight id/slug/title/kind/priority/timestamps
    list — so bulk note injection can't overflow an agent's context. Callers
    that actually render note bodies (dashboard notes view, handoff, planner
    context) pass ``bodies=True``; agents fetch a single body on demand via
    ``read_note(slug)`` → ``get_project_note_by_slug``.

    The ``query`` filter always searches the body even when bodies are omitted
    from the result — the search happens in SQL, the body just isn't returned.

    9fa119dd — ``limit``/``offset`` add SQL LIMIT/OFFSET paging (clamped to
    1..500) mirroring get_sprint_items_page. ``limit=None`` (the default) keeps
    the legacy "return every matching row" behaviour for existing callers.
    """
    where, params = _project_notes_where(db, project_id, tag, query)
    cols = _project_notes_cols(bodies)
    sql = f"SELECT {cols} FROM project_notes WHERE {where} ORDER BY created_at DESC"
    if limit is not None:
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))
        sql += " LIMIT ? OFFSET ?"
        params = [*params, limit, offset]
    async with db.execute(sql, params or None) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows if r is not None]  # type: ignore[misc]


def _note_relevance_score(
    reference_count: int, age_days: float, has_decision_link: bool, read_count: int = 0
) -> float:
    """98890df1 — PageRank-ish note relevance. Reference/read counts pass through a
    saturating transform so a heavily-referenced note ranks high without a single
    outlier dominating; recency decays on a ~30-day scale. read_count stays 0 until
    note-read tracking lands (semantic similarity is the pgvector Phase 2)."""
    ref = 1.0 - 1.0 / (1.0 + max(0, reference_count))
    rec = 1.0 / (1.0 + max(0.0, age_days) / 30.0)
    read = 1.0 - 1.0 / (1.0 + max(0, read_count))
    dec = 1.0 if has_decision_link else 0.0
    return 0.45 * ref + 0.30 * rec + 0.15 * read + 0.10 * dec


async def get_project_notes_ranked(
    db: aiosqlite.Connection,
    project_id: str,
    tag: str | None = None,
    query: str | None = None,
    bodies: bool = False,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """98890df1 — like get_project_notes but ordered by a relevance score
    (reference_count / recency / decision-link) instead of pure recency, so
    heavily cross-referenced notes surface and stale ones sink. Each returned row
    carries a ``relevance`` float."""
    from datetime import datetime, timezone  # noqa: PLC0415
    where, params = _project_notes_where(db, project_id, tag, query)
    # Fetch full rows (bodies are needed to count [[slug]] references) unpaged so
    # scoring sees the whole corpus, then trim after ranking.
    sql = f"SELECT * FROM project_notes WHERE {where}"
    async with db.execute(sql, params or None) as cur:
        rows = await cur.fetchall()
    notes = [_row_to_dict(r) for r in rows if r is not None]
    combined_bodies = " ".join((n.get("body") or "") for n in notes)
    try:
        decisions = await get_pinned_decisions(db, project_id)
        decision_blob = " ".join((d.get("body") or "") for d in (decisions or []))
    except Exception:  # noqa: BLE001
        decision_blob = ""
    now = datetime.now(timezone.utc)
    for n in notes:
        slug = n.get("slug") or ""
        marker = f"[[{slug}]]"
        # count references from OTHER notes (subtract self-references in own body)
        ref_count = 0
        if slug:
            ref_count = combined_bodies.count(marker) - (n.get("body") or "").count(marker)
        has_dec = bool(slug) and (marker in decision_blob)
        age_days = 0.0
        created = n.get("created_at")
        if created:
            try:
                cdt = datetime.strptime(
                    str(created)[:19], "%Y-%m-%d %H:%M:%S"
                ).replace(tzinfo=timezone.utc)
                age_days = max(0.0, (now - cdt).total_seconds() / 86400.0)
            except (ValueError, TypeError):
                pass
        n["relevance"] = round(
            _note_relevance_score(max(0, ref_count), age_days, has_dec), 4
        )
    notes.sort(key=lambda x: (x.get("relevance", 0.0), x.get("created_at") or ""), reverse=True)
    if not bodies:
        for n in notes:
            n.pop("body", None)
    if limit is not None:
        notes = notes[: max(1, min(int(limit), 500))]
    return notes


async def get_project_notes_page(
    db: aiosqlite.Connection,
    project_id: str,
    tag: str | None = None,
    query: str | None = None,
    bodies: bool = False,
    limit: int = 100,
    cursor: int = 0,
) -> dict[str, Any]:
    """9fa119dd — one cursor page of project notes (newest first) for the
    dashboard's "Load More" notes list.

    Mirrors get_sprint_items_page's offset paging, but returns the
    cursor envelope the dashboard / get_notes tool consume::

        {"notes": [...], "has_more": bool, "next_cursor": int | None}

    The cursor is the next offset (notes are ordered ``created_at DESC`` with no
    stable secondary key, so an offset cursor is the consistent mechanism — same
    as sprint items / tasks). One extra row is fetched internally to compute
    ``has_more`` without a second COUNT query; ``next_cursor`` is the offset to
    pass back for the following page, or ``None`` when the list is exhausted.

    ``tag``, ``query`` and ``bodies`` behave exactly as in get_project_notes.
    ``limit`` is clamped to 1..500.
    """
    limit = max(1, min(int(limit), 500))
    cursor = max(0, int(cursor))
    # Fetch limit+1: the extra row tells us another page exists without COUNT.
    # Build the query directly (not via get_project_notes) so the +1 probe row
    # isn't lost to that function's own 500-row clamp at the limit==500 boundary.
    where, params = _project_notes_where(db, project_id, tag, query)
    cols = _project_notes_cols(bodies)
    # 7000554a — a real total for the page envelope, so the dashboard can show the
    # ACTUAL remaining count (and a per-subtab total) instead of a hardcoded page
    # size. One cheap COUNT with the same filter; PG-safe row access.
    async with db.execute(
        f"SELECT COUNT(*) AS c FROM project_notes WHERE {where}", list(params),
    ) as ccur:
        crow = await ccur.fetchone()
    total_count = int(crow["c"] if isinstance(crow, dict) else crow[0]) if crow else 0
    async with db.execute(
        f"SELECT {cols} FROM project_notes WHERE {where} "
        "ORDER BY created_at DESC LIMIT ? OFFSET ?",
        [*params, limit + 1, cursor],
    ) as cur:
        raw = await cur.fetchall()
    rows = [_row_to_dict(r) for r in raw if r is not None]  # type: ignore[misc]
    has_more = len(rows) > limit
    notes = rows[:limit]
    next_cursor = cursor + len(notes) if has_more else None
    # remaining = notes not yet loaded after this page (>=0). Drives "Load N more".
    remaining = max(0, total_count - (cursor + len(notes)))
    return {
        "notes": notes, "has_more": has_more, "next_cursor": next_cursor,
        "total_count": total_count, "remaining": remaining,
    }


async def get_project_note_by_slug(
    db: aiosqlite.Connection, project_id: str, slug: str
) -> dict[str, Any] | None:
    """5a5bba43 — fetch a single full note (incl. body) by its per-project slug.

    Backs the ``read_note(slug)`` MCP tool — the pull half of the list→read
    model. Returns None when no note with that slug exists in the project.

    f5f2a89d — includes ``referenced_by``: lightweight list of notes whose body
    contains ``[[slug]]`` linking back to this note.
    """
    async with db.execute(
        "SELECT * FROM project_notes WHERE project_id = ? AND slug = ?",
        (project_id, slug),
    ) as cur:
        row = await cur.fetchone()
    note = _row_to_dict(row)
    if note is not None:
        note["referenced_by"] = await get_notes_backlinked_to(db, project_id, slug)
    return note


async def update_project_note(
    db: aiosqlite.Connection,
    note_id: str,
    *,
    title: str | None = None,
    body: str | None = None,
    tags: str | None = None,
    priority: str | None = None,
) -> dict[str, Any] | None:
    """Patch any combination of title/body/tags/priority. Returns updated row."""
    existing = await get_project_note(db, note_id)
    if existing is None:
        return None
    fields: dict[str, Any] = {}
    if title is not None:
        fields["title"] = title
    if body is not None:
        from meridian.secret_redaction import check_for_secrets
        check_for_secrets(body, context="note body")
        fields["body"] = body
    if tags is not None:
        fields["tags"] = tags
    if priority is not None and priority in ("high", "normal", "low"):
        fields["priority"] = priority
    if not fields:
        return existing
    from datetime import datetime, timezone
    fields["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    args = list(fields.values()) + [note_id]
    await db.execute(
        f"UPDATE project_notes SET {set_clause} WHERE id = ?", args
    )
    await db.commit()
    return await get_project_note(db, note_id)


async def delete_project_note(
    db: aiosqlite.Connection, note_id: str
) -> bool:
    """Hard-delete a note. Returns True if a row was removed."""
    async with db.execute(
        "DELETE FROM project_notes WHERE id = ?", (note_id,)
    ) as cur:
        rc = cur.rowcount or 0
    await db.commit()
    return rc > 0



# ---------------------------------------------------------------------------
# registered_hostnames — token-based OAuth hooks (control-plane / auth DB)
# ---------------------------------------------------------------------------


async def register_hostname(
    db: aiosqlite.Connection, tenant_id: str, hostname: str
) -> str:
    """Register (or rotate) a machine for token-based hooks and return its
    registration_token. Re-registering the same (tenant, hostname) rotates the
    token so a lost machine can be re-authorized from the dashboard."""
    import secrets
    token = secrets.token_hex(16)
    async with db.execute(
        "SELECT id FROM registered_hostnames WHERE tenant_id = ? AND hostname = ?",
        (tenant_id, hostname),
    ) as cur:
        existing = await cur.fetchone()
    if existing is not None:
        await db.execute(
            "UPDATE registered_hostnames "
            "SET registration_token = ?, registered_at = datetime('now') "
            "WHERE tenant_id = ? AND hostname = ?",
            (token, tenant_id, hostname),
        )
    else:
        await db.execute(
            "INSERT INTO registered_hostnames "
            "(id, tenant_id, hostname, registration_token) VALUES (?, ?, ?, ?)",
            (_new_id(), tenant_id, hostname, token),
        )
    await db.commit()
    return token


async def resolve_hostname_registration(
    db: aiosqlite.Connection, hostname: str, registration_token: str
) -> str | None:
    """Return the tenant_id for a hostname+token match (and bump last_seen), or
    None when there is no match. Never raises — the hook path must fail open to
    an empty context, not a 401."""
    if not hostname or not registration_token:
        return None
    async with db.execute(
        "SELECT tenant_id FROM registered_hostnames "
        "WHERE hostname = ? AND registration_token = ?",
        (hostname, registration_token),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    tenant_id = row["tenant_id"] if isinstance(row, dict) else row[0]
    await db.execute(
        "UPDATE registered_hostnames SET last_seen = datetime('now') "
        "WHERE hostname = ? AND registration_token = ?",
        (hostname, registration_token),
    )
    await db.commit()
    return tenant_id


async def get_hostname_status(
    db: aiosqlite.Connection, tenant_id: str, hostname: str
) -> dict[str, Any]:
    """Return {registered, token} for a tenant's hostname (token echoed so the
    installer can finish writing the hook script after the browser connect)."""
    async with db.execute(
        "SELECT registration_token FROM registered_hostnames "
        "WHERE tenant_id = ? AND hostname = ?",
        (tenant_id, hostname),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return {"registered": False, "token": None}
    tok = row["registration_token"] if isinstance(row, dict) else row[0]
    return {"registered": True, "token": tok}


async def list_registered_hostnames(
    db: aiosqlite.Connection, tenant_id: str
) -> list[dict[str, Any]]:
    """List a tenant's registered machines (token omitted) newest first."""
    async with db.execute(
        "SELECT id, hostname, registered_at, last_seen FROM registered_hostnames "
        "WHERE tenant_id = ? ORDER BY registered_at DESC",
        (tenant_id,),
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows if r is not None]  # type: ignore[misc]


async def revoke_registered_hostname(
    db: aiosqlite.Connection, tenant_id: str, machine_id: str
) -> bool:
    """Delete one of the tenant's registrations by id. Returns True if removed.
    Tenant-scoped so a tenant can never revoke another tenant's machine."""
    async with db.execute(
        "DELETE FROM registered_hostnames WHERE id = ? AND tenant_id = ?",
        (machine_id, tenant_id),
    ) as cur:
        rc = cur.rowcount or 0
    await db.commit()
    return rc > 0


# ---------------------------------------------------------------------------
# 10e6b265 — session queue (projects.queued_session)
# ---------------------------------------------------------------------------


async def set_queued_session(
    db: aiosqlite.Connection, project_id: str, goal: str | None
) -> None:
    """Queue the next /goal string to run after this session. Empty/None clears."""
    await db.execute(
        "UPDATE projects SET queued_session = ? WHERE id = ?",
        ((goal or None), project_id),
    )
    await db.commit()


async def get_queued_session(
    db: aiosqlite.Connection, project_id: str
) -> str | None:
    """Return the queued next-session goal, or None when nothing is queued."""
    async with db.execute(
        "SELECT queued_session FROM projects WHERE id = ?", (project_id,)
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    val = row["queued_session"] if isinstance(row, dict) else row[0]
    return val or None


async def pop_queued_session(
    db: aiosqlite.Connection, project_id: str
) -> str | None:
    """Return the queued goal and clear it (read-once) so a handoff surfaces it
    exactly once."""
    goal = await get_queued_session(db, project_id)
    if goal:
        await db.execute(
            "UPDATE projects SET queued_session = NULL WHERE id = ?", (project_id,)
        )
        await db.commit()
    return goal


# ---------------------------------------------------------------------------
# 5efe254b / 590dcdd5 — trusted handoff channel (projects.pending_goal)
# ---------------------------------------------------------------------------

# 590dcdd5 — goals older than this many hours are flagged as possibly-stale
# when surfaced by pop_pending_goal_with_meta.  A fresh /goal from a human in
# chat makes the persisted pending_goal obsolete, but the server has no way to
# detect it server-side; the staleness flag lets executors recognise they should
# defer to any direct /goal instruction they received in chat.
PENDING_GOAL_STALE_HOURS: int = 24


async def set_pending_goal(
    db: aiosqlite.Connection, project_id: str, goal: str | None
) -> None:
    """Persist the handoff /goal so the next start_session can surface it through
    a trusted MCP tool result (keyed on project_id) instead of a copy-pasted,
    spoofable chat string. Empty/None clears it. Read-once via pop_pending_goal.

    590dcdd5: also writes pending_goal_at (UTC ISO-8601) so pop_pending_goal_with_meta
    can expose the goal's age and flag it as possibly-stale when it is older than
    PENDING_GOAL_STALE_HOURS hours."""
    now_iso: str | None = (
        _dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ") if goal else None
    )
    await db.execute(
        "UPDATE projects SET pending_goal = ?, pending_goal_at = ? WHERE id = ?",
        ((goal or None), now_iso, project_id),
    )
    await db.commit()


async def get_pending_goal(
    db: aiosqlite.Connection, project_id: str
) -> str | None:
    """Return the stored handoff /goal, or None when nothing is pending."""
    async with db.execute(
        "SELECT pending_goal FROM projects WHERE id = ?", (project_id,)
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    val = row["pending_goal"] if isinstance(row, dict) else row[0]
    return val or None


async def pop_pending_goal(
    db: aiosqlite.Connection, project_id: str
) -> str | None:
    """Return the pending /goal and clear it (read-once) so start_session
    surfaces it exactly once and a stale goal never resurfaces in a later
    session."""
    goal = await get_pending_goal(db, project_id)
    if goal:
        await db.execute(
            "UPDATE projects SET pending_goal = NULL, pending_goal_at = NULL"
            " WHERE id = ?",
            (project_id,),
        )
        await db.commit()
    return goal


async def pop_pending_goal_with_meta(
    db: aiosqlite.Connection, project_id: str
) -> dict[str, object] | None:
    """590dcdd5 — read-once pop with staleness metadata.

    Returns a dict ``{"goal": str, "age_hours": float, "stale": bool}`` when a
    pending_goal exists, or ``None`` when nothing is pending.

    ``stale`` is ``True`` when the goal is older than PENDING_GOAL_STALE_HOURS.
    Executors SHOULD treat a stale pending_goal as advisory only and defer to
    any direct /goal instruction received in chat, because the human may have
    started the session with a completely different intent since the handoff was
    written.  The goal is still cleared read-once regardless of staleness.
    """
    async with db.execute(
        "SELECT pending_goal, pending_goal_at FROM projects WHERE id = ?",
        (project_id,),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    if isinstance(row, dict):
        raw_goal = row.get("pending_goal")
        raw_at = row.get("pending_goal_at")
    else:
        raw_goal = row[0]
        raw_at = row[1] if len(row) > 1 else None
    if not raw_goal:
        return None
    # Compute age in hours; treat missing/malformed timestamp as 0 (unknown age).
    age_hours: float = 0.0
    if raw_at:
        try:
            written = _dt.datetime.strptime(raw_at, "%Y-%m-%dT%H:%M:%SZ")
            age_hours = (
                _dt.datetime.utcnow() - written
            ).total_seconds() / 3600.0
        except (ValueError, TypeError):
            age_hours = 0.0
    stale = age_hours >= PENDING_GOAL_STALE_HOURS
    # Clear read-once regardless of staleness.
    await db.execute(
        "UPDATE projects SET pending_goal = NULL, pending_goal_at = NULL"
        " WHERE id = ?",
        (project_id,),
    )
    await db.commit()
    return {"goal": raw_goal, "age_hours": round(age_hours, 2), "stale": stale}


# ---------------------------------------------------------------------------
# v3.1 — per-session checkpoint snapshots (sessions.checkpoint_data)
# ---------------------------------------------------------------------------


async def set_session_checkpoint(
    db: aiosqlite.Connection, session_id: str, data: dict[str, Any]
) -> None:
    """Store a checkpoint snapshot on the session row as JSON text.
    Replaces the legacy checkpoint:* project_notes hack."""
    await db.execute(
        "UPDATE sessions SET checkpoint_data = ? WHERE id = ?",
        (json.dumps(data), session_id),
    )
    await db.commit()


async def get_session_checkpoint(
    db: aiosqlite.Connection, session_id: str
) -> dict[str, Any] | None:
    """Return the latest checkpoint snapshot for a session, or None."""
    async with db.execute(
        "SELECT checkpoint_data FROM sessions WHERE id = ?", (session_id,)
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    raw = row["checkpoint_data"]
    if not raw:
        return None
    if isinstance(raw, dict):  # Postgres may return parsed JSON
        return raw
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# 5abf3e12 — per-session goal-compliance metric (sessions.goal_compliance)
# ---------------------------------------------------------------------------


async def compute_session_goal_compliance(
    db: aiosqlite.Connection, project_id: str, session_id: str
) -> dict[str, Any]:
    """Measure whether a session's /goal item list was fully completed.

    The /goal instructs an executor to "claim and execute the following sprint
    items … you are DONE only when complete_sprint_item() has been called for
    every listed item". This computes that compliance from the durable board
    state, using ``sprint_items.actor`` as the link between a session and the
    items it took on. ``actor`` is set by claim_sprint_item (COALESCE — the
    first claimer wins) and OVERWRITTEN by complete_sprint_item to the completing
    session (5823db0b). For the standard executor loop — where ONE session both
    claims and completes each item — the claimer and completer are the same, so
    this measures exactly that session's compliance. In a cross-session hand-off
    (parallel / coordinator patterns) the item is reattributed to whichever
    session called complete_sprint_item(): credit follows the completer, and a
    claimer whose item is finalised by another session no longer counts it.
    Attribution therefore reflects the FINAL owner of each item.

    * ``listed`` (N): sprint items whose ``actor`` is this session at compute
      time — the items it currently owns (claimed and/or completed). This
      includes items left in any non-'done' terminal state (e.g. skipped /
      failed / pushed), so an item this session owns but legitimately skipped
      still counts toward N while never counting toward M.
    * ``completed`` (M): of those, how many reached status 'done' (i.e. were
      finished via complete_sprint_item()). Only 'done' counts as compliance.
    * ``fully_completed``: True iff N > 0 and M == N (every taken-on item shipped).
    * ``zero_listed``: True when N == 0 (the session claimed no items, so there is
      nothing to have completed — ``fully_completed`` is False, not vacuously
      True).
    * ``compliance_pct``: round(100 * M / N) when N > 0 else 0.

    Returns a plain dict (JSON-serialisable) so callers can store it verbatim on
    ``sessions.goal_compliance`` and surface it in progress / handoff output.
    """
    async with db.execute(
        "SELECT "
        "  COUNT(*) AS listed, "
        "  COALESCE(SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END), 0) AS completed "
        "FROM sprint_items WHERE project_id = ? AND actor = ?",
        (project_id, session_id),
    ) as cur:
        row = await cur.fetchone()
    listed = int((row["listed"] if row else 0) or 0)
    completed = int((row["completed"] if row else 0) or 0)
    return {
        "session_id": session_id,
        "listed": listed,
        "completed": completed,
        "fully_completed": listed > 0 and completed == listed,
        "zero_listed": listed == 0,
        "compliance_pct": round(100 * completed / listed) if listed else 0,
    }


async def set_session_goal_compliance(
    db: aiosqlite.Connection, session_id: str, metric: dict[str, Any]
) -> None:
    """Persist the computed goal-compliance metric on the session row (JSON text)."""
    await db.execute(
        "UPDATE sessions SET goal_compliance = ? WHERE id = ?",
        (json.dumps(metric), session_id),
    )
    await db.commit()


async def get_session_goal_compliance(
    db: aiosqlite.Connection, session_id: str
) -> dict[str, Any] | None:
    """Return the stored goal-compliance metric for a session, or None."""
    async with db.execute(
        "SELECT goal_compliance FROM sessions WHERE id = ?", (session_id,)
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    raw = row["goal_compliance"]
    if not raw:
        return None
    if isinstance(raw, dict):  # Postgres may return parsed JSON
        return raw
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


async def record_session_goal_compliance(
    db: aiosqlite.Connection, project_id: str, session_id: str
) -> dict[str, Any]:
    """Compute the goal-compliance metric for a session and store it. Returns it.

    Called at session end (generate_handoff) so every completed session leaves a
    durable, queryable record of whether its /goal item list was fully done.
    """
    metric = await compute_session_goal_compliance(db, project_id, session_id)
    await set_session_goal_compliance(db, session_id, metric)
    return metric


# ---------------------------------------------------------------------------
# v0.9 — Magic-link email auth tokens
# ---------------------------------------------------------------------------


async def get_active_magic_token(
    db: aiosqlite.Connection, email: str
) -> dict[str, Any] | None:
    """Return the most recent unused, unexpired magic token for an email
    (or None). Used by the rate-limit gate to avoid sending duplicates
    when the user clicks the resend button while a valid link is in
    their inbox."""
    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    async with db.execute(
        "SELECT * FROM magic_link_tokens WHERE email = ? "
        "AND used_at IS NULL AND expires_at > ? "
        "ORDER BY created_at DESC LIMIT 1",
        (email.lower(), now_iso),
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


async def store_magic_token(
    db: aiosqlite.Connection,
    email: str,
    token_hash: str,
    expires_at: str,
) -> dict[str, Any]:
    """Persist a single-use magic-link token. ``token_hash`` is the
    sha256 of the raw token; the raw token is shown to the user (in
    the email) and never stored."""
    tid = _new_id()
    await db.execute(
        "INSERT INTO magic_link_tokens (id, email, token_hash, expires_at) "
        "VALUES (?, ?, ?, ?)",
        (tid, email.lower(), token_hash, expires_at),
    )
    await db.commit()
    return {"id": tid, "email": email.lower(), "expires_at": expires_at}


async def record_signup_attempt(
    db: aiosqlite.Connection, ip_hash: str, email_hash: str
) -> None:
    """925909aa — log one magic-link signup attempt keyed by salted IP/email
    hashes, for persistent per-IP rate limiting (survives restarts)."""
    await db.execute(
        "INSERT INTO signup_attempts (id, ip_hash, email_hash) VALUES (?, ?, ?)",
        (_new_id(), ip_hash, email_hash),
    )
    await db.commit()


async def count_recent_signup_attempts(
    db: aiosqlite.Connection, ip_hash: str, since_iso: str
) -> int:
    """Count signup attempts from ``ip_hash`` at or after ``since_iso``
    (``YYYY-MM-DD HH:MM:SS`` UTC)."""
    async with db.execute(
        "SELECT COUNT(*) FROM signup_attempts WHERE ip_hash = ? AND created_at >= ?",
        (ip_hash, since_iso),
    ) as cur:
        row = await cur.fetchone()
    if not row:
        return 0
    return int(row["count"] if isinstance(row, dict) else row[0])


async def consume_magic_token(
    db: aiosqlite.Connection, token_hash: str
) -> dict[str, Any] | None:
    """Validate-and-mark-used. Returns the row when fresh; None if the
    token doesn't exist, was already used, or has expired. Atomic so two
    concurrent verify clicks can't both succeed."""
    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    async with db.execute(
        "SELECT * FROM magic_link_tokens WHERE token_hash = ?",
        (token_hash,),
    ) as cur:
        row = await cur.fetchone()
    row_dict = _row_to_dict(row)
    if row_dict is None:
        return None
    if row_dict.get("used_at"):
        return None
    if (row_dict.get("expires_at") or "") < now_iso:
        return None
    await db.execute(
        "UPDATE magic_link_tokens SET used_at = ? WHERE id = ? AND used_at IS NULL",
        (now_iso, row_dict["id"]),
    )
    await db.commit()
    return row_dict


# ---------------------------------------------------------------------------
# Dunning helpers
# ---------------------------------------------------------------------------

async def get_tenant_by_stripe_customer(
    db: aiosqlite.Connection,
    stripe_customer_id: str,
) -> dict[str, Any] | None:
    """Return tenant by stripe_customer_id, or None."""
    async with db.execute(
        "SELECT * FROM tenants WHERE stripe_customer_id = ?", (stripe_customer_id,)
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


async def list_tenants_with_neon(
    db: aiosqlite.Connection,
) -> list[dict[str, Any]]:
    """Return all tenants that have a provisioned Neon database."""
    async with db.execute(
        "SELECT * FROM tenants WHERE neon_project_id IS NOT NULL"
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows if r is not None]


async def get_tenants_with_payment_failures(
    db: aiosqlite.Connection,
) -> list[dict[str, Any]]:
    """Return all tenants currently in dunning (payment_failed_at IS NOT NULL)."""
    async with db.execute(
        "SELECT * FROM tenants WHERE payment_failed_at IS NOT NULL"
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows if r is not None]


async def list_active_tunnel_tenant_ids(
    db: aiosqlite.Connection,
) -> list[str]:
    """Return the ids of tenants whose local binary tunnel is marked active (b74099b2).

    ``tenants.tunnel_active`` is set to 1 when the tenant's binary opens its tunnel
    WebSocket and back to 0 on a clean disconnect (b43b0c6a). A server-side deploy
    kills the old process WITHOUT clearing the flag, so on the fresh process's
    startup these are exactly the tenants that were connected (and whose
    already-connected MCP sessions will re-issue a ``tools/list`` after reconnect).

    Used by the startup ``notifications/tools/list_changed`` trigger so a deploy that
    adds a new hosted MCP tool becomes visible to those sessions on their next list,
    instead of staying hidden until a full reconnect. Cheap (single indexed column
    scan of the small control-plane ``tenants`` table); returns [] when none active.
    """
    async with db.execute(
        "SELECT id FROM tenants WHERE tunnel_active = 1"
    ) as cur:
        rows = await cur.fetchall()
    out: list[str] = []
    for r in rows:
        if r is None:
            continue
        tid = r["id"] if isinstance(r, dict) else r[0]
        if tid:
            out.append(tid)
    return out


# ---------------------------------------------------------------------------
# GDPR / account management
# ---------------------------------------------------------------------------

async def export_tenant_data(
    db: aiosqlite.Connection,
    tenant_id: str,
    project_db: aiosqlite.Connection | None = None,
) -> dict[str, Any]:
    """Collect all data belonging to a tenant as a plain dict for GDPR export.

    Account-level rows (tenant, api_tokens, workspace_members) live in the auth
    DB (``db``). In hosted mode the tenant's *project* data lives in a separate
    per-tenant Neon DB — pass it as ``project_db`` so the export actually
    contains the projects. When omitted (self-hosted, single DB) the project
    data is read from ``db`` like before.
    """
    from datetime import datetime, timezone

    pdb = project_db if project_db is not None else db

    async with db.execute(
        "SELECT id, email, google_sub, microsoft_sub, plan, created_at FROM tenants WHERE id = ?",
        (tenant_id,),
    ) as cur:
        tenant_row = await cur.fetchone()
    tenant_data = _row_to_dict(tenant_row) if tenant_row else {}

    async with db.execute(
        "SELECT id, label, created_at FROM api_tokens WHERE tenant_id = ?",
        (tenant_id,),
    ) as cur:
        tokens = [_row_to_dict(r) for r in await cur.fetchall()]

    async with db.execute(
        "SELECT id, email, role, github_access, invited_at, joined_at FROM workspace_members WHERE tenant_id = ?",
        (tenant_id,),
    ) as cur:
        members = [_row_to_dict(r) for r in await cur.fetchall()]

    async with pdb.execute(
        "SELECT id, name, creator_human_id, decisions, created_at FROM projects ORDER BY created_at DESC",
    ) as cur:
        project_rows = await cur.fetchall()

    projects = []
    for pr in project_rows:
        p = _row_to_dict(pr)
        pid = p["id"]

        async with pdb.execute(
            "SELECT content, goal_north_star, goal_sprint, version, updated_at FROM goal_states WHERE project_id = ?",
            (pid,),
        ) as cur:
            goals = [_row_to_dict(r) for r in await cur.fetchall()]

        async with pdb.execute(
            "SELECT id, name, human_id, status, session_summary, created_at FROM sessions WHERE project_id = ? ORDER BY created_at DESC LIMIT 500",
            (pid,),
        ) as cur:
            sessions = [_row_to_dict(r) for r in await cur.fetchall()]

        async with pdb.execute(
            "SELECT id, session_id, description, status, created_at FROM task_log WHERE project_id = ? ORDER BY created_at DESC LIMIT 2000",
            (pid,),
        ) as cur:
            tasks = [_row_to_dict(r) for r in await cur.fetchall()]

        async with pdb.execute(
            "SELECT id, version, title, status, item_group, added_at FROM sprint_items WHERE project_id = ?",
            (pid,),
        ) as cur:
            sprint = [_row_to_dict(r) for r in await cur.fetchall()]

        try:
            async with pdb.execute(
                "SELECT id, title, body, tags, created_at FROM project_notes WHERE project_id = ?",
                (pid,),
            ) as cur:
                notes = [_row_to_dict(r) for r in await cur.fetchall()]
        except Exception:
            notes = []

        try:
            async with pdb.execute(
                "SELECT id, question, urgency, status, answer, created_at FROM hitl_requests WHERE project_id = ? ORDER BY created_at DESC LIMIT 500",
                (pid,),
            ) as cur:
                hitl = [_row_to_dict(r) for r in await cur.fetchall()]
        except Exception:
            hitl = []

        p["goal_states"] = goals
        p["sessions"] = sessions
        p["tasks"] = tasks
        p["sprint_items"] = sprint
        p["notes"] = notes
        p["hitl_requests"] = hitl
        projects.append(p)

    # Workspace-global notes/decisions live in the project DB too.
    try:
        ws_notes = await get_workspace_notes(pdb)
    except Exception:
        ws_notes = []
    try:
        ws_decisions = await get_workspace_decisions(pdb)
    except Exception:
        ws_decisions = []

    return {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "tenant": tenant_data,
        "api_tokens": tokens,
        "workspace_members": members,
        "workspace_notes": ws_notes,
        "workspace_decisions": ws_decisions,
        "projects": projects,
    }


async def delete_tenant_records(
    db: aiosqlite.Connection,
    tenant_id: str,
) -> None:
    """Remove all records for a tenant from the main DB. Irreversible."""
    for stmt, params in [
        ("DELETE FROM user_sessions WHERE tenant_id = ?", (tenant_id,)),
        ("DELETE FROM api_tokens WHERE tenant_id = ?", (tenant_id,)),
        ("DELETE FROM workspace_members WHERE tenant_id = ?", (tenant_id,)),
        ("DELETE FROM tenants WHERE id = ?", (tenant_id,)),
    ]:
        await db.execute(stmt, params)
    await db.commit()


# ---------------------------------------------------------------------------
# v2.6 — Session-scoped ephemeral notes (sprint scratch pad)
# ---------------------------------------------------------------------------


async def add_session_note(
    db: aiosqlite.Connection,
    session_id: str,
    title: str,
    body: str,
    note_kind: str | None = None,
) -> dict[str, Any]:
    """Add an ephemeral note scoped to a session. Auto-deleted on session close.

    0d7de2a2 — ``note_kind`` classifies the note. None/'note' is a normal
    sprint note. 'thinking' marks a HOOKS_DEBUG_STATE scratchpad note
    auto-persisted by Claude's client-side thinking_sync hook; the dashboard
    renders these with a distinct icon and they round-trip into the next
    session brief. Any other value is normalized to None (a normal note).
    """
    if note_kind not in (None, "note", "thinking"):
        note_kind = None
    if note_kind == "note":
        note_kind = None
    nid = _new_id()
    await db.execute(
        "INSERT INTO session_notes (id, session_id, title, body, note_kind) "
        "VALUES (?, ?, ?, ?, ?)",
        (nid, session_id, title, body, note_kind),
    )
    await db.commit()
    async with db.execute(
        "SELECT * FROM session_notes WHERE id = ?", (nid,)
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)  # type: ignore[return-value]


async def get_session_notes(
    db: aiosqlite.Connection,
    session_id: str,
    note_kind: str | None = None,
) -> list[dict[str, Any]]:
    """Return all notes for a session, newest first.

    0d7de2a2 — pass ``note_kind='thinking'`` to return only thinking_sync
    scratchpad notes, or ``'note'`` for only normal notes. Omit to return all.
    """
    if note_kind == "thinking":
        sql = (
            "SELECT * FROM session_notes WHERE session_id = ? "
            "AND note_kind = 'thinking' ORDER BY created_at DESC"
        )
        params: tuple = (session_id,)
    elif note_kind == "note":
        sql = (
            "SELECT * FROM session_notes WHERE session_id = ? "
            "AND (note_kind IS NULL OR note_kind = 'note') ORDER BY created_at DESC"
        )
        params = (session_id,)
    else:
        sql = (
            "SELECT * FROM session_notes WHERE session_id = ? ORDER BY created_at DESC"
        )
        params = (session_id,)
    async with db.execute(sql, params) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows if r is not None]  # type: ignore[misc]


async def delete_session_notes(
    db: aiosqlite.Connection,
    session_id: str,
) -> int:
    """Delete all notes for a session. Called on session close."""
    async with db.execute(
        "DELETE FROM session_notes WHERE session_id = ?", (session_id,)
    ) as cur:
        count = cur.rowcount if cur.rowcount is not None else 0
    await db.commit()
    return count


async def add_feedback(
    db: aiosqlite.Connection,
    tenant_id: str,
    type: str,
    message: str,
    email: str = None,
) -> str:
    """Save user feedback to the feedback table."""
    feedback_id = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO feedback (id, tenant_id, type, message, email) VALUES (?, ?, ?, ?, ?)",
        (feedback_id, tenant_id, type, message, email),
    )
    await db.commit()
    return feedback_id


async def list_changelog_entries(db: aiosqlite.Connection) -> list[dict[str, Any]]:
    """Return all changelog entries ordered newest first."""
    async with db.execute(
        "SELECT id, version, title, body, published_at, created_at "
        "FROM changelog_entries ORDER BY published_at DESC"
    ) as cur:
        rows = await cur.fetchall()
    return [dict(r) if isinstance(r, dict) else {
        "id": r[0], "version": r[1], "title": r[2], "body": r[3],
        "published_at": r[4], "created_at": r[5],
    } for r in rows]


async def create_changelog_entry(
    db: aiosqlite.Connection,
    title: str,
    body: str,
    version: str | None = None,
    published_at: str | None = None,
) -> dict[str, Any]:
    """Create a new changelog entry. Returns the created row."""
    import datetime
    entry_id = str(uuid.uuid4())
    ts = published_at or datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    await db.execute(
        "INSERT INTO changelog_entries (id, version, title, body, published_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (entry_id, version, title, body, ts),
    )
    await db.commit()
    return {"id": entry_id, "version": version, "title": title, "body": body,
            "published_at": ts, "created_at": ts}


async def update_changelog_entry(
    db: aiosqlite.Connection,
    entry_id: str,
    title: str | None = None,
    body: str | None = None,
    version: str | None = None,
    published_at: str | None = None,
) -> dict[str, Any] | None:
    """Patch a changelog entry. Returns the updated row or None if not found."""
    async with db.execute(
        "SELECT id, version, title, body, published_at, created_at "
        "FROM changelog_entries WHERE id = ?",
        (entry_id,),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    r = dict(row) if isinstance(row, dict) else {
        "id": row[0], "version": row[1], "title": row[2], "body": row[3],
        "published_at": row[4], "created_at": row[5],
    }
    new_title = title if title is not None else r["title"]
    new_body = body if body is not None else r["body"]
    new_version = version if version is not None else r["version"]
    new_published_at = published_at if published_at is not None else r["published_at"]
    await db.execute(
        "UPDATE changelog_entries SET title=?, body=?, version=?, published_at=? WHERE id=?",
        (new_title, new_body, new_version, new_published_at, entry_id),
    )
    await db.commit()
    return {**r, "title": new_title, "body": new_body, "version": new_version,
            "published_at": new_published_at}


async def delete_changelog_entry(db: aiosqlite.Connection, entry_id: str) -> bool:
    """Delete a changelog entry. Returns True if it existed."""
    async with db.execute(
        "SELECT 1 FROM changelog_entries WHERE id = ?", (entry_id,)
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return False
    await db.execute("DELETE FROM changelog_entries WHERE id = ?", (entry_id,))
    await db.commit()
    return True


# ---------------------------------------------------------------------------
# f773a99a — Graph diff: per-session code-graph metric snapshots
# ---------------------------------------------------------------------------


async def snapshot_graph_metrics(
    db: aiosqlite.Connection,
    session_id: str,
    project_id: str,
) -> dict[str, Any]:
    """Compute proxy graph metrics for a session and persist them.

    Proxy metrics (no live code-graph traversal needed):
    - node_count: distinct file_paths in file_symbol_claims for this project.
    - edge_count: distinct (session_id, file_path) pairs — sessions touching files.
    - hotspot_count: files with 3+ distinct sessions in file_symbol_claims.
    - file_churn: distinct files touched in the last 7 days.

    Idempotent: inserts a new snapshot row each time so callers can compare
    across checkpoints. Returns the snapshot dict.
    """
    import json as _json

    # node_count: distinct files claimed in this project across all sessions
    async with db.execute(
        "SELECT COUNT(DISTINCT fsc.file_path) AS cnt "
        "FROM file_symbol_claims fsc "
        "JOIN sessions s ON s.id = fsc.session_id "
        "WHERE s.project_id = ?",
        (project_id,),
    ) as cur:
        row = await cur.fetchone()
    node_count = (_row_to_dict(row) or {}).get("cnt") or 0

    # edge_count: distinct (session, file) pairs for this project
    async with db.execute(
        "SELECT COUNT(*) AS cnt "
        "FROM ("
        "    SELECT DISTINCT fsc.session_id, fsc.file_path "
        "    FROM file_symbol_claims fsc "
        "    JOIN sessions s ON s.id = fsc.session_id "
        "    WHERE s.project_id = ?"
        ")",
        (project_id,),
    ) as cur:
        row = await cur.fetchone()
    edge_count = (_row_to_dict(row) or {}).get("cnt") or 0

    # hotspot_count: files with 3+ distinct sessions
    async with db.execute(
        "SELECT COUNT(*) AS cnt FROM ("
        "    SELECT fsc.file_path "
        "    FROM file_symbol_claims fsc "
        "    JOIN sessions s ON s.id = fsc.session_id "
        "    WHERE s.project_id = ? "
        "    GROUP BY fsc.file_path "
        "    HAVING COUNT(DISTINCT fsc.session_id) >= 3"
        ")",
        (project_id,),
    ) as cur:
        row = await cur.fetchone()
    hotspot_count = (_row_to_dict(row) or {}).get("cnt") or 0

    # file_churn: distinct files touched in last 7 days
    async with db.execute(
        "SELECT COUNT(DISTINCT fsc.file_path) AS cnt "
        "FROM file_symbol_claims fsc "
        "JOIN sessions s ON s.id = fsc.session_id "
        "WHERE s.project_id = ? "
        "AND fsc.claimed_at > datetime('now', '-7 days')",
        (project_id,),
    ) as cur:
        row = await cur.fetchone()
    file_churn = (_row_to_dict(row) or {}).get("cnt") or 0

    metrics = {
        "node_count": node_count,
        "edge_count": edge_count,
        "hotspot_count": hotspot_count,
        "file_churn": file_churn,
    }
    snap_id = _new_id()
    await db.execute(
        "INSERT INTO session_graph_snapshots "
        "(id, session_id, project_id, node_count, edge_count, hotspot_count, file_churn, metrics_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (snap_id, session_id, project_id, node_count, edge_count, hotspot_count, file_churn, _json.dumps(metrics)),
    )
    await db.commit()
    return {"snapshot_id": snap_id, "session_id": session_id, "project_id": project_id, **metrics}


async def get_graph_diff(
    db: aiosqlite.Connection,
    session_a_id: str,
    session_b_id: str,
) -> dict[str, Any]:
    """Compare the latest graph snapshots of two sessions.

    f773a99a — returns the delta between session_a and session_b's most recent
    ``session_graph_snapshots`` rows: nodes_added, nodes_removed, new_hotspots,
    file_churn_delta. Positive values mean session_b has more; negative means
    session_a has more. Returns an error dict when either session has no snapshot.
    """
    async def _latest_snap(sid: str) -> dict[str, Any] | None:
        async with db.execute(
            "SELECT * FROM session_graph_snapshots WHERE session_id = ? "
            "ORDER BY snapshot_at DESC LIMIT 1",
            (sid,),
        ) as cur:
            row = await cur.fetchone()
        return _row_to_dict(row)

    snap_a = await _latest_snap(session_a_id)
    snap_b = await _latest_snap(session_b_id)

    if snap_a is None:
        return {"error": f"No graph snapshot found for session {session_a_id[:8]}"}
    if snap_b is None:
        return {"error": f"No graph snapshot found for session {session_b_id[:8]}"}

    node_a = snap_a.get("node_count") or 0
    node_b = snap_b.get("node_count") or 0
    hotspot_a = snap_a.get("hotspot_count") or 0
    hotspot_b = snap_b.get("hotspot_count") or 0
    churn_a = snap_a.get("file_churn") or 0
    churn_b = snap_b.get("file_churn") or 0
    edge_a = snap_a.get("edge_count") or 0
    edge_b = snap_b.get("edge_count") or 0

    return {
        "session_a": session_a_id,
        "session_b": session_b_id,
        "snapshot_a_at": snap_a.get("snapshot_at"),
        "snapshot_b_at": snap_b.get("snapshot_at"),
        "nodes_added": node_b - node_a,
        "nodes_removed": max(0, node_a - node_b),
        "new_hotspots": hotspot_b - hotspot_a,
        "file_churn_delta": churn_b - churn_a,
        "edge_delta": edge_b - edge_a,
        "summary": (
            f"session_b has {node_b - node_a:+d} nodes, "
            f"{hotspot_b - hotspot_a:+d} hotspots, "
            f"{churn_b - churn_a:+d} file churn vs session_a"
        ),
    }


# ---------------------------------------------------------------------------
# A2A protocol — agent_tasks table (99e71b9e)
# ---------------------------------------------------------------------------

async def create_agent_task(
    db: aiosqlite.Connection,
    agent_id: str,
    input_data: dict[str, Any],
    session_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Insert a new agent task with status 'submitted'. Returns the task row."""
    task_id = _new_id()
    input_json = json.dumps(input_data)
    meta_json = json.dumps(metadata) if metadata else None
    await db.execute(
        "INSERT INTO agent_tasks (id, agent_id, session_id, input, metadata) "
        "VALUES (?, ?, ?, ?, ?)",
        (task_id, agent_id, session_id, input_json, meta_json),
    )
    await db.commit()
    return await _get_agent_task(db, task_id)  # type: ignore[return-value]


async def _get_agent_task(
    db: aiosqlite.Connection, task_id: str
) -> dict[str, Any] | None:
    """Fetch one agent task by id."""
    async with db.execute(
        "SELECT * FROM agent_tasks WHERE id = ?", (task_id,)
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    task = _row_to_dict(row)
    # Deserialize JSON blobs
    if task.get("input"):
        try:
            task["input"] = json.loads(task["input"])
        except Exception:
            pass
    if task.get("output"):
        try:
            task["output"] = json.loads(task["output"])
        except Exception:
            pass
    if task.get("metadata"):
        try:
            task["metadata"] = json.loads(task["metadata"])
        except Exception:
            pass
    return task


async def get_agent_task(
    db: aiosqlite.Connection, agent_id: str, task_id: str
) -> dict[str, Any] | None:
    """Fetch an agent task, scoped to agent_id for security."""
    async with db.execute(
        "SELECT * FROM agent_tasks WHERE id = ? AND agent_id = ?",
        (task_id, agent_id),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    task = _row_to_dict(row)
    for field in ("input", "output", "metadata"):
        if task.get(field):
            try:
                task[field] = json.loads(task[field])
            except Exception:
                pass
    return task


async def update_agent_task_status(
    db: aiosqlite.Connection,
    task_id: str,
    status: str,
    output: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Update the status (and optionally output) of an agent task."""
    output_json = json.dumps(output) if output is not None else None
    # agent_tasks.updated_at is TIMESTAMPTZ on Postgres; the shared datetime('now')
    # form is adapter-rewritten to a to_char(...) *text* expression, which Postgres
    # refuses to implicitly cast into a timestamptz column. Same class as 6adba18c.
    now_expr = "now()" if hasattr(db, "_pool") else "datetime('now')"
    if output_json is not None:
        await db.execute(
            f"UPDATE agent_tasks SET status=?, output=?, updated_at={now_expr} WHERE id=?",
            (status, output_json, task_id),
        )
    else:
        await db.execute(
            f"UPDATE agent_tasks SET status=?, updated_at={now_expr} WHERE id=?",
            (status, task_id),
        )
    await db.commit()
    return await _get_agent_task(db, task_id)


# ---------------------------------------------------------------------------
# Handoff history (8819d6b1) — each generated handoff is its own row in the
# handoffs table, so the dashboard/planner can list history, diff between
# sessions, and detect "a new handoff arrived since you last checked"
# (ab514e43). record_handoff is called from handoff.generate_handoff.
# ---------------------------------------------------------------------------
async def record_handoff(
    db: aiosqlite.Connection,
    project_id: str,
    mode: str,
    body: str,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Insert a handoff history row and return it.

    ``mode`` is the rendered handoff mode (full/delta/…). ``session_id`` links
    the handoff to the session that produced it (NULL for project-level renders).

    00dbeed0 — durable since_ts: the handoff's created_at is the anchor used
    by the next delta call to scope "Completed since last handoff". On Postgres
    the table DEFAULT is NOW() which is frozen at transaction-start, so two
    calls within one transaction would get the same timestamp — making the
    anchor useless for ordering relative to clock_timestamp()-stamped completions
    (and breaking tests that rely on real-time ordering with asyncio.sleep).
    Explicitly pass clock_timestamp() on Postgres to guarantee a monotonically
    advancing value regardless of the surrounding transaction.
    """
    hid = _new_id()
    is_pg = hasattr(db, "_pool")
    if is_pg:
        # handoffs.created_at is TIMESTAMPTZ on Postgres (see
        # _migrate_pg_handoffs_table) -- insert a native timestamptz value
        # directly, matching amend_handoff's now() below. clock_timestamp()
        # (not now(), which is frozen at transaction-start) preserves the
        # monotonic-ordering guarantee this function needs. A prior version
        # wrapped this in to_char(...) to produce a formatted TEXT string,
        # which raised psycopg.errors.DatatypeMismatch against the real
        # timestamptz column -- always broken, never exercised without a
        # live Postgres in the loop.
        now_expr = "clock_timestamp()"
        await db.execute(
            f"INSERT INTO handoffs (id, project_id, session_id, mode, body, created_at) "
            f"VALUES (?, ?, ?, ?, ?, {now_expr})",
            (hid, project_id, session_id, mode, body),
        )
    else:
        await db.execute(
            "INSERT INTO handoffs (id, project_id, session_id, mode, body) "
            "VALUES (?, ?, ?, ?, ?)",
            (hid, project_id, session_id, mode, body),
        )
    await db.commit()
    return (await get_handoff(db, hid)) or {"id": hid}


async def amend_handoff(
    db: aiosqlite.Connection,
    project_id: str,
    body: str,
    mode: str,
) -> dict[str, Any] | None:
    """edd9c54b — update the most recent handoff row for a project in-place.

    Called when generate_handoff detects that the prior handoff was never
    consumed (pending_goal is still set, meaning no start_session has popped
    it since the last generate_handoff). Amending avoids inflating the handoffs
    table with redundant rows and suppresses the context-refresh nudge.

    Returns the updated row, or None if no prior row exists (caller falls back
    to record_handoff).
    """
    rows = await get_handoffs(db, project_id, limit=1)
    if not rows:
        return None
    hid = rows[0]["id"]
    # handoffs.created_at is TIMESTAMPTZ on Postgres; see update_agent_task_status
    # for why the shared datetime('now') form breaks there.
    now_expr = "now()" if hasattr(db, "_pool") else "datetime('now')"
    await db.execute(
        f"UPDATE handoffs SET body = ?, mode = ?, created_at = {now_expr} WHERE id = ?",
        (body, mode, hid),
    )
    await db.commit()
    return await get_handoff(db, hid)


async def get_handoff(
    db: aiosqlite.Connection, handoff_id: str
) -> dict[str, Any] | None:
    """Fetch a single handoff row by id."""
    async with db.execute(
        "SELECT * FROM handoffs WHERE id = ?", (handoff_id,)
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


async def get_handoffs(
    db: aiosqlite.Connection,
    project_id: str,
    limit: int = 20,
    *,
    session_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return handoff history for a project, newest first (body included).

    Pass ``session_id`` to scope to one session's handoffs. ``limit`` is clamped
    to 1..200.
    """
    limit = max(1, min(int(limit or 20), 200))
    if session_id:
        sql = (
            "SELECT * FROM handoffs WHERE project_id = ? AND session_id = ? "
            "ORDER BY created_at DESC, id DESC LIMIT ?"
        )
        params: tuple[Any, ...] = (project_id, session_id, limit)
    else:
        sql = (
            "SELECT * FROM handoffs WHERE project_id = ? "
            "ORDER BY created_at DESC, id DESC LIMIT ?"
        )
        params = (project_id, limit)
    async with db.execute(sql, params) as cur:
        rows = await cur.fetchall()
    return [d for d in (_row_to_dict(r) for r in rows) if d is not None]


async def get_latest_handoff(
    db: aiosqlite.Connection, project_id: str
) -> dict[str, Any] | None:
    """Return the most recent handoff row for a project, or None."""
    rows = await get_handoffs(db, project_id, limit=1)
    return rows[0] if rows else None


async def get_handoff_for_scope(
    db: aiosqlite.Connection,
    project_id: str,
    handoff_id: str,
    *,
    session_id: str | None = None,
) -> dict[str, Any]:
    """63b602ff — fetch + validate a ``handoffs`` row for an explicit scoped
    amendment (``meridian.handoff.amend_handoff``).

    A pure ownership check, one level below the version-scope check (which
    needs the ``sessions.sprint_version`` lookup already implemented in
    ``meridian.handoff._resolve_session_sprint_version`` — kept there, not
    duplicated here, since it is a handoff-orchestration concern, not a raw
    DB-row concern):

    - Raises ``ValueError`` when ``handoff_id`` does not name an existing
      ``handoffs`` row.
    - Raises ``ValueError`` when the row belongs to a different project —
      a correction/amendment must never cross a project boundary.
    - Raises ``ValueError`` when ``session_id`` is given, the row recorded
      one, and they differ — refuses a cross-session amendment. A source
      row with NO recorded session (a project-level render) is not scoped
      to any one session, so it is never rejected on this check alone.

    Returns the row (same shape as :func:`get_handoff`) on success. Plain
    ``ValueError`` (not a handoff.py-specific exception type) so this stays
    import-cycle-free — ``meridian.handoff`` already imports this module and
    wraps the message in its own error class where a distinct type matters.
    """
    source = await get_handoff(db, handoff_id)
    if source is None:
        raise ValueError(f"handoff {handoff_id!r} not found")
    if source.get("project_id") != project_id:
        raise ValueError(f"handoff {handoff_id!r} belongs to a different project")
    if session_id and source.get("session_id") and source["session_id"] != session_id:
        raise ValueError(
            f"handoff {handoff_id!r} was recorded for session "
            f"{source['session_id']!r}, not {session_id!r} -- refusing a "
            "cross-session amendment"
        )
    return source


# ---------------------------------------------------------------------------
# validate_assumption (8ec5493b) — one-call assumption validation: stamp the
# decision's assumption_status, save a code-anchored finding note, and fire a
# blocking HITL when the assumption is invalidated. Builds on the assumption
# fields added in 2b39549d.
# ---------------------------------------------------------------------------
async def validate_assumption(
    db: aiosqlite.Connection,
    decision_id: str,
    finding: str,
    confirmed: bool,
    *,
    file_path: str | None = None,
    symbol: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Confirm or invalidate the assumption a pinned decision rests on.

    - Stamps ``assumption_status`` = confirmed | invalidated on the decision.
    - Saves a code-anchored ``kind='insight'`` note carrying ``finding`` (tagged
      ``assumption`` + the status; high priority when invalidated).
    - On invalidation, fires a **blocking** HITL so work depending on the
      decision pauses for human judgment.

    Returns ``{decision, assumption_status, note, hitl}`` (``hitl`` is None when
    confirmed). Raises ValueError if the decision does not exist.
    """
    decision = await get_pinned_decision(db, decision_id)
    if decision is None:
        raise ValueError("decision not found")
    status = "confirmed" if confirmed else "invalidated"
    project_id = decision["project_id"]
    title = (decision.get("title") or "decision").strip()
    updated = await update_pinned_decision(
        db, decision_id, assumption_status=status
    )
    note = await add_project_note(
        db, project_id,
        f"Assumption {status}: {title}"[:500],
        finding or "(no finding text)",
        f"assumption,{status}",
        kind="insight",
        priority="high" if not confirmed else "normal",
        file_path=file_path, symbol=symbol,
    )
    hitl: dict[str, Any] | None = None
    if not confirmed:
        # hitl_requests.session_id is a FK — only link a session that exists so a
        # stale/unknown id degrades to a project-level HITL instead of crashing.
        _sid = session_id or None
        if _sid:
            async with db.execute(
                "SELECT 1 FROM sessions WHERE id = ?", (_sid,)
            ) as _cur:
                if await _cur.fetchone() is None:
                    _sid = None
        hitl = await request_hitl(
            db, project_id,
            (
                f"Assumption INVALIDATED for decision '{title}': "
                f"{(finding or '').strip()[:300]}. Work depending on this "
                "decision is blocked — decide how to proceed (revise the "
                "decision, re-scope dependent items, or proceed anyway)."
            ),
            session_id=_sid,
            context=(
                f"decision_id={decision_id}; assumption="
                f"{(decision.get('assumption') or '(none recorded)')[:300]}"
            ),
            urgency="blocking",
        )
    return {
        "decision": updated,
        "assumption_status": status,
        "note": note,
        "hitl": hitl,
    }


# ---------------------------------------------------------------------------
# save_finding (e1f43ee7) — phase-agnostic capture primitive. Decoupled from any
# search tool: turns a finding from web/arxiv/code/conversation into a durable,
# addressable note with provenance. capture_research_finding (b1d36e93) is the
# web/paper-shaped wrapper over it.
# ---------------------------------------------------------------------------
_FINDING_SOURCE_TYPES = ("web", "arxiv", "code", "conversation")


async def save_finding(
    db: aiosqlite.Connection,
    project_id: str,
    summary: str,
    *,
    source_url: str | None = None,
    source_type: str = "web",
    decision_id: str | None = None,
) -> dict[str, Any]:
    """Persist a finding as an addressable ``kind='finding'`` note with provenance.

    The note is tagged ``finding`` + the source_type (so it's discoverable via
    get_notes(tag='finding')), carries ``source_url`` as provenance, and — when
    ``decision_id`` is given — is also tagged ``decision:<id>`` to link it to that
    pinned decision. The note title is derived from the first line of ``summary``.

    Returns ``{note, source_type, decision_id}``. Raises ValueError if
    ``decision_id`` is given but no such decision exists.
    """
    st = (source_type or "web").strip().lower()
    if st not in _FINDING_SOURCE_TYPES:
        st = "web"
    summary = summary or ""
    stripped = summary.strip()
    first_line = stripped.splitlines()[0] if stripped else "finding"
    title = f"Finding: {first_line}"[:200]
    tags = f"finding,{st}"
    linked: str | None = None
    if decision_id:
        if await get_pinned_decision(db, decision_id) is None:
            raise ValueError("decision not found")
        tags = f"{tags},decision:{decision_id}"
        linked = decision_id
    note = await add_project_note(
        db, project_id, title, summary, tags,
        kind="finding", source=source_url,
    )
    return {"note": note, "source_type": st, "decision_id": linked}


# ---------------------------------------------------------------------------
# get_last_session_brief (81170c84) — "what did the last session do" for a
# planning chat: the most recent session's completed sprint items, task log, and
# the latest pinned decisions, so a planner sees executor output without manual
# copy-paste.
# ---------------------------------------------------------------------------
async def get_last_session_brief(
    db: aiosqlite.Connection, project_id: str, *, exclude_session_id: str | None = None
) -> dict[str, Any] | None:
    """Summarize the most recent session for a project, or None if there are none.

    ``exclude_session_id`` skips a session (e.g. the planner's own) so the brief
    reports the last *other* session. Completed items are linked via task_id →
    task.session_id. Returns name/status/last_seen + completed_items +
    recent_tasks + recent_pinned_decisions.
    """
    sessions = await get_sessions(db, project_id, active_only=False)
    if exclude_session_id:
        sessions = [s for s in sessions if s.get("id") != exclude_session_id]
    if not sessions:
        return None
    last = sessions[0]
    sid = last["id"]
    all_tasks = await get_tasks(db, project_id, limit=200)
    session_task_ids = {t["id"] for t in all_tasks if t.get("session_id") == sid}
    session_tasks = [t for t in all_tasks if t.get("session_id") == sid][:15]
    items_all = await get_sprint_items(db, project_id)
    completed_items = [
        {
            "id": it["id"],
            "title": (it.get("title") or "")[:120],
            "status": it.get("status"),
        }
        for it in items_all
        if it.get("task_id") in session_task_ids
        and it.get("status") in {"done", "skipped", "failed", "pushed"}
    ][:20]
    pinned = await get_pinned_decisions(db, project_id)
    recent_decisions = [
        {"id": d.get("id"), "title": (d.get("title") or "")[:120]}
        for d in pinned[:3]
    ]
    return {
        "session_id": sid,
        "name": last.get("name"),
        "human_id": last.get("human_id"),
        "status": last.get("status"),
        "last_seen": last.get("last_seen"),
        "session_summary": last.get("session_summary"),
        "completed_items": completed_items,
        "recent_tasks": [
            {
                "description": (t.get("description") or "")[:160],
                "status": t.get("status"),
                "kind": t.get("kind"),
            }
            for t in session_tasks
        ],
        "recent_pinned_decisions": recent_decisions,
    }


# ---------------------------------------------------------------------------
# 3295c784 — shared (cross-Fly-instance) rate-limit counters
# ---------------------------------------------------------------------------

async def increment_rate_counter(
    db: aiosqlite.Connection, tenant_id: str, window_start_minute: int
) -> int:
    """Atomically bump and return the shared hit count for a tenant's window.

    ``window_start_minute`` is an epoch-minute bucket (``int(time.time() // 60)``).
    The upsert (``INSERT ... ON CONFLICT (tenant_id, window_start) DO UPDATE SET
    count = count + 1``) is a single atomic statement on BOTH backends, so
    concurrent requests — even across different Fly machines hitting the same
    Postgres row — never lose an increment. The value is then read back with a
    follow-up SELECT rather than ``RETURNING`` because the PG adapter only
    surfaces rows for SELECT/WITH statements (an INSERT...RETURNING would execute
    but its row would not be fetched). Under heavy concurrency the read-back may
    observe a value a hair HIGHER than this caller's own increment (a sibling
    request bumped it in between); that only ever over-counts, which is the safe
    direction for a limiter (it never lets a tenant slip past its budget).

    Returns the current shared count for ``window_start_minute`` after this
    request's increment.
    """
    await db.execute(
        "INSERT INTO mcp_rate_counters (tenant_id, window_start, count) "
        "VALUES (?, ?, 1) "
        "ON CONFLICT (tenant_id, window_start) "
        "DO UPDATE SET count = mcp_rate_counters.count + 1",
        (tenant_id, window_start_minute),
    )
    await db.commit()
    async with db.execute(
        "SELECT count FROM mcp_rate_counters "
        "WHERE tenant_id = ? AND window_start = ?",
        (tenant_id, window_start_minute),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return 0
    return int(row["count"])


async def prune_rate_counters(
    db: aiosqlite.Connection, older_than_minute: int
) -> None:
    """Delete counter rows for windows strictly older than ``older_than_minute``.

    Called opportunistically from the limiter path so the table never grows
    without bound. Cheap (one DELETE against the ``idx_mcp_rate_counters_window``
    index); best-effort — a failure here must never affect the request.
    """
    await db.execute(
        "DELETE FROM mcp_rate_counters WHERE window_start < ?",
        (older_than_minute,),
    )
    await db.commit()


# ---------------------------------------------------------------------------
# f7ee1ba7 — Model B: scoped docx-region claims
#
# A .docx is a zip container with no partial write; every mutating tool
# re-saves the whole file (last-save-wins). ``file_locks`` (whole-file) and
# ``file_symbol_claims`` (code line-ranges) already guard code files at two
# granularities. This extends the same pattern to DOCX documents:
#
# * A session may claim a specific paragraph/element by its durable ``para_id``
#   (the ``w14:paraId`` the OOXML layer already surfaces — the same id used by
#   ``update_paragraph`` and ``get_document_structure``).
# * Two sessions can hold NON-OVERLAPPING element claims on the same file
#   concurrently — the precision benefit mirroring symbol claims for code.
# * An edit attempt OUTSIDE the caller's claimed element is REJECTED before
#   touching the filesystem — structural prevention, not advice.
# * A whole-file (unscoped) claim still works as before; scoped claims
#   compose with it (a whole-file lock blocks all scoped writers, exactly as
#   file_locks blocks symbol claims).
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Sprint-item functions re-exported from sprint_items.py submodule.
# Placed at the BOTTOM so all shared helpers (serialize_touches_resources,
# parse_touches_resources, _resource_sets_conflict, _new_id, _row_to_dict,
# _publish_project_event, _UNSET) are fully defined before sprint_items.py
# loads and tries to import them.
# ---------------------------------------------------------------------------
from .sprint_items import (  # noqa: F401
    # Public sprint-item functions
    add_sprint_item,
    add_sprint_item_pointer,
    add_subtask,
    analyze_sprint,
    assign_sprint_waves,
    audit_and_quarantine_sprint_item_dependency_mismatches,
    build_github_completion_comment,
    build_sprint_items_xml,
    claim_parallel_batch,
    claim_sprint_item,
    collapse_sprint_item_clusters,
    complete_sprint_item,
    complete_wave_gate,
    configure_wave_gate,
    count_pending_sprint_items,
    count_sprint_items_awaiting_verification,
    delete_sprint_item_pointer,
    evaluate_board_blockers,
    fail_sprint_item,
    fan_out_sprint_items,
    find_cross_project_dependency_mismatches,
    get_blocking_dependency_for_sprint_item,
    get_open_task_for_sprint_item,
    get_parallelizable_groups,
    get_project_blocker_policy,
    get_sprint_item,
    get_sprint_item_pointers,
    get_pointer_evidence_item_ids,
    get_sprint_items,
    get_sprint_items_cached,
    get_sprint_items_for_resource,
    get_sprint_items_page,
    get_wave_gate_configs,
    get_latest_sprint_item_verification,
    get_sprint_version_description,
    get_all_sprint_version_descriptions,
    upsert_sprint_version_description,
    handle_session_stall,
    infer_active_sprint_version,
    link_sprint_item_github_issue,
    merge_sprint_items,
    move_sprint_item_to_project,
    patch_sprint_item,
    provisional_complete_sprint_item,
    push_sprint_item,
    record_sprint_item_verification,
    requeue_or_fail_stalled_item,
    set_project_blocker_policy,
    skip_sprint_item,
    split_sprint_item,
    sprint_test_coverage_expected,
    start_sprint_item,
    # 56e9b3c7 — autonomous stale-claim reconciliation
    classify_stale_claim,
    reconcile_stale_claims,
    RECONCILE_STALE_CLAIM_AUDIT_EVENT,
    RECONCILE_ACTIVE,
    RECONCILE_STALE,
    RECONCILE_AMBIGUOUS,
    RECONCILE_NOT_APPLICABLE,
    # Public classes
    SprintItemClaimMismatch,
    SprintItemEvidenceRequired,
    SprintItemStatusRace,
    SprintItemVerificationRequired,
    # Private helpers also imported directly in tests/callers
    _ACTIVE_SPRINT_STATUSES,
    _DUP_BLOCKING_SPRINT_STATUSES,
    _MAX_SPRINT_STALL_RETRIES,
    _NEG_TS,
    _NICKNAME_ADJ,
    _NICKNAME_NOUN,
    _NICKNAME_STOPWORDS,
    _PATCH_SPRINT_ITEM_ALLOWED_STATUSES,
    _RECONCILE_STALE_HOURS,
    _RECONCILE_DEFAULT_BATCH,
    _RECONCILE_MAX_BATCH,
    _SPRINT_DUP_OVERLAP_THRESHOLD,
    _SPRINT_ITEMS_CACHE,
    _SPRINT_ITEMS_CACHE_TTL,
    _SPRINT_PRIORITY_DEFAULT_RANK,
    _SPRINT_PRIORITY_RANK,
    _SPRINT_STALL_FLAG_HOURS,
    _TEST_COVERAGE_KEYWORDS,
    _VALID_SPRINT_BLOCKER_KINDS,
    _VALID_SPRINT_PRIORITIES,
    _VALID_SPRINT_STATUSES,
    _advance_task_chain,
    _claim_session_liveness,
    _claim_worktree_activity,
    _claim_recent_task_evidence,
    _invalidate_sprint_items_cache,
    _is_manual_sprint_item,
    _item_declares_resources,
    is_item_claim_prospected,
    _maybe_rollup_parent,
    _parse_deferral_ts,
    _reset_stale_claim,
    _split_wave_label,
    _get_blocking_wave_gate,
    _session_stall_summary,
    _sprint_item_nickname_base,
    _sprint_item_slug_base,
    _sprint_priority_order_sql,
    _stalled_item_ids_for_session,
    _text_calls_for_test_coverage,
    _auto_generate_version_description,
    _title_word_overlap,
    _title_word_set,
    _unique_sprint_nickname,
    _unique_sprint_slug,
    _topo_depth_map,
    _transition_status,
    _update_sprint_item_status,
    # 0d0cada7 — lease-local scheduler diagnostics, also called directly by
    # meridian.mcp.handler._sprint_item_resource_claim_gate and by tests.
    _compute_plan_generation,
    _seconds_until,
    _live_resource_holder,
    # 6b3b2c0e — canonical single-colon-legacy-shorthand classification,
    # also called directly by tests.
    _predict_resource_granularity,
    _is_legacy_file_symbol_shorthand,
)


# ef665ef8 — canonical expanded-board snapshots, revisions, and resume diffs.
# Imported after sprint_items (needs get_sprint_items/get_sprint_item_pointers/
# parse_touches_resources already bound onto this module's namespace).
from .board_snapshot import (  # noqa: F401
    canonical_json,
    build_board_snapshot,
    diff_board_snapshots,
    compute_scope_diff,
    find_stale_reference_ids,
    get_latest_board_snapshot_revision,
    get_project_item_index,
    record_board_snapshot_revision,
)


# 2a654cb0 — durable wave-run state, append/supersede history, idempotent
# finalization. Imported after board_snapshot: create_wave_run records a board
# snapshot revision, so record_board_snapshot_revision must already be bound.
from .wave_runs import (  # noqa: F401
    # State machine constants
    WAVE_RUN_STATUSES,
    WAVE_RUN_TERMINAL_STATUSES,
    WAVE_RUN_TRANSITIONS,
    WAVE_RUN_CHILD_FAILURE_MODES,
    WAVE_RUN_CHILD_STATUSES,
    WaveRunFinalizationBlocked,
    # Runs
    create_wave_run,
    get_wave_run,
    list_wave_runs,
    advance_wave_run_status,
    record_degraded_tool,
    # Append-only history
    append_wave_run_event,
    get_wave_run_events,
    supersede_wave_run_event,
    # Children
    record_wave_run_child,
    get_wave_run_children,
    # Finalization
    finalize_wave_run,
    # 7d71d6bc — RESCUE-R2: child leases, dispatch provenance, no-op resume
    # protection.
    WAVE_RUN_CHILD_DEFAULT_LEASE_TTL_SECONDS,
    WAVE_RUN_CHILD_LEASE_LIVE,
    WAVE_RUN_CHILD_LEASE_STALE_ORPHAN,
    WAVE_RUN_CHILD_LEASE_COMPLETED,
    WAVE_RUN_CHILD_LEASE_EMPTY_INVALID,
    WAVE_RUN_CHILD_LEASE_STATES,
    ForeignWaveRunChildLeaseError,
    classify_wave_run_child_lease,
    find_active_wave_run_child_for_item,
    claim_wave_run_child,
    heartbeat_wave_run_child,
    record_wave_run_child_outcome,
    get_wave_run_recovery_plan,
)


# efaa918a — resume_wave stale-manifest gating. Imported after wave_runs (needs
# get_wave_run/WAVE_RUN_TERMINAL_STATUSES already bound) and after board_snapshot
# (needs build_board_snapshot/diff_board_snapshots already bound).
from .wave_resume import (  # noqa: F401
    WaveResumeStale,
    check_wave_resume,
)


# bbb447ec — immutable, queryable wave-completion summaries keyed by wave_id.
# Imported after board_snapshot (needs canonical_json already bound) and after
# wave_runs (a summary's wave_run_id typically references a wave_runs.id,
# though this module never enforces that FK — see wave_run_summary.py).
from .wave_run_summary import (  # noqa: F401
    WAVE_SUMMARY_ITEM_OUTCOMES,
    WAVE_SUMMARY_TEST_SCOPES,
    _migrate_wave_run_summaries,
    canonical_wave_summary_hash,
    persist_wave_summary,
    get_wave_summary,
    get_wave_summary_by_id,
    get_wave_summary_history,
    record_wave_summary_correction,
)


from .locks import (  # noqa: F401
    # Constants
    _FILE_LOCK_TTL_HOURS,
    _CLAIM_LIVE_HOURS,
    # Private helpers (also called directly by tests/callers)
    _cutoff_dt,
    _normalize_file_path,
    _code_notes_for_session_file,
    _decision_notes_for_session_file,
    _other_read_claims,
    _all_read_claims,
    _claim_file_read,
    _live_symbol_claims_for_file,
    _ranges_overlap,
    _live_docx_region_claims_for_file,
    _migrate_docx_region_claims,
    # 2593a5fe — resource-amendment helper
    _amend_sprint_item_resources_for_session,
    # Public file-lock functions
    expire_file_locks,
    expire_stale_symbol_claims,
    expire_file_read_claims,
    claim_file,
    release_file,
    release_file_locks_for_session,
    get_file_conflict_warnings,
    get_file_claims,
    # Public resource-lock functions
    expire_resource_locks,
    claim_resource,
    release_resource,
    release_resource_locks_for_session,
    get_resource_claims,
    get_resource_conflicts,
    # Public symbol-claim functions
    claim_symbol,
    get_symbol_claims,
    release_symbol_claims_for_session,
    release_symbol,
    get_symbol_hotspots,
    get_hotspot_suggestions,
    # Public docx-region claim functions
    claim_docx_region,
    get_docx_region_claims,
    release_docx_region_claims,
    check_docx_region_write_conflict,
    # Session file claims view
    get_session_file_claims,
    # 356d6ac8 — structural-degradation signal
    _PATCH_DEGRADATION_THRESHOLD,
    _increment_file_patch_counter,
    get_structural_degradation_warnings,
    flag_file_refactor,
)


from .docx_merge import (  # noqa: F401
    # Private helpers (also called directly by tests)
    _MERGE_OWNER_TTL_MINUTES,
    _get_manifest_row,
    _get_draft_row,
    _migrate_docx_merge_manifests,
    # fe989980 — wave-scoped merge manifest + serialized canonical merge gate
    open_merge_manifest,
    declare_merge_anchors,
    claim_merge_owner,
    release_merge_owner,
    check_merge_stale_or_overlap,
    record_merge_result,
    finalize_merge_manifest,
    get_merge_manifest,
)


from .workspace import (  # noqa: F401
    # Private helpers (also called directly by tests/callers)
    _WORKSPACE_SETTINGS_ID,
    _VALID_PROPOSAL_STATUSES,
    _PROPOSAL_TRANSITIONS,
    _VALID_WS_SPRINT_STATUSES,
    _ws_tenant_clause,
    _ws_settings_key,
    # Public workspace-note functions
    add_workspace_note,
    get_workspace_notes,
    delete_workspace_note,
    move_workspace_note_to_project,
    update_workspace_note,
    # Public workspace-decision functions
    pin_workspace_decision,
    get_workspace_decisions,
    delete_workspace_decision,
    # Public workspace-proposal functions
    add_workspace_proposal,
    append_proposal_update,
    get_workspace_proposals,
    advance_workspace_proposal_status,
    promote_workspace_proposal,
    set_proposal_github_issue,
    delete_workspace_proposal,
    # 3f892ea6 — deterministic proposal intake blocks
    parse_proposal_intake_blocks,
    _migrate_proposal_intake_drafts,
    ingest_proposal_intake,
    get_proposal_intake_drafts,
    promote_intake_draft,
    # Public workspace sprint-board functions
    add_workspace_sprint_item,
    get_workspace_sprint_items,
    update_workspace_sprint_item,
    complete_workspace_sprint_item,
    # Public workspace-settings functions
    get_workspace_settings,
    update_workspace_settings,
    seed_workspace_settings_from_toml,
    # 5dfe34b2 / cd495afa — manual-issue-screening toggle + audit trail
    _MANUAL_ISSUE_SCREENING_RISK_WARNING,
    ManualIssueScreeningToggleError,
    set_manual_issue_screening_enabled,
    record_action_audit_event,
    get_action_audit_log,
    # 0d95003f — generic cross-project quarantine mechanism
    CROSS_PROJECT_QUARANTINE_EVENT_TYPE,
    CROSS_PROJECT_QUARANTINE_RESOLVED_EVENT_TYPE,
    _VALID_QUARANTINE_RESOLUTIONS,
    quarantine_cross_project_record,
    resolve_cross_project_quarantine,
    get_cross_project_quarantine_status,
    is_cross_project_quarantined,
    list_quarantined_cross_project_records,
    # Public workspace-member / invite functions
    create_workspace_invite,
    get_workspace_invite_by_token_hash,
    accept_workspace_invite,
    get_pending_invites_for_email,
    resolve_member_role,
    workspace_member_accepted_for_email,
    get_workspace_member_by_id,
    refresh_workspace_invite_token,
    list_workspace_members,
    count_workspace_members,
    delete_workspace_member,
    update_workspace_member,
    get_workspaces_for_email,
    get_scoped_project_ids_for_member,
)

# 5dfe34b2 — manual-issue content-screening extension. Imported last (after
# workspace's set_manual_issue_screening_enabled / get_action_audit_log are
# already bound on this module) since manual_issue_intel's velocity-anomaly
# check lazily imports those names back from meridian.db.
from .manual_issue_intel import (  # noqa: F401
    screen_manual_issue_content,
    screen_manual_issue,
    sanitize_manual_issue_excerpt,
    log_raw_manual_issue_content,
    get_raw_manual_issue_content_log,
    check_manual_issue_action_velocity,
)


from .hooks import (  # noqa: F401
    VALID_HOOK_EVENTS,
    _RESERVED_HOOK_SLUGS,
    _sanitize_hook_slug,
    add_custom_hook,
    get_custom_hooks,
    get_custom_hook,
    update_custom_hook,
    delete_custom_hook,
)


# 6cdc5df3 — durable, typed proposal-to-evidence linkage. Imported LAST (after
# sprint_items / workspace) since link_proposal_evidence validates entity_ids
# against get_sprint_item's table (sprint_items), project_notes, and
# decisions_pinned, all defined earlier in this module or in sprint_items.py.
from .proposal_links import (  # noqa: F401
    _VALID_PROPOSAL_ENTITY_TYPES,
    _PROPOSAL_ENTITY_TABLE,
    _PROPOSAL_ENTITY_BUCKET,
    _migrate_proposal_evidence_links,
    link_proposal_evidence,
    unlink_proposal_evidence,
    get_proposal_links,
    get_proposal_evidence,
    get_proposal_ids_for_project,
)


# eb2e44f8 — immutable wave base manifests for git worktrees (repo identity,
# base branch/SHA, owning sprint item), checked by
# meridian.worktree_merge_guard.validate_worktree_merge before a
# merge/completion is allowed to proceed.
from .worktree_manifest import (  # noqa: F401
    _migrate_wave_base_manifests,
    persist_worktree_manifest,
    get_worktree_manifest,
    get_worktree_manifest_history,
)


# 22cad9b8 — immutable batch-claim manifests for atomic parallel sprint-item
# claims (durable "what batch was decided" record), backing
# sprint_items.claim_parallel_batch. Mirrors the worktree_manifest import
# immediately above.
from .batch_claim import (  # noqa: F401
    _migrate_sprint_batch_claims,
    compute_batch_key,
    persist_batch_claim_manifest,
    get_batch_claim_manifest,
    get_batch_claim_manifest_by_id,
    get_batch_claim_manifest_history,
    mark_batch_claim_outcome,
)


# 525d86bb — durable synchronous verification-run lifecycle records (start,
# real exit_code/status, log artifact) for run_verification. Mirrors the
# batch_claim import immediately above — a single-table, no-state-machine
# shape.
from .verification_runs import (  # noqa: F401
    VERIFICATION_RUN_STATUSES,
    _migrate_verification_runs,
    create_verification_run,
    get_verification_run,
    list_verification_runs,
    complete_verification_run,
)


# e1475682 — durable metadata for the backend-neutral vector-index contract
# (meridian_codeindex.vector_index): active backend, embedding model/version,
# dimension, source fingerprint, and the benchmark evidence gating pgvector.
# Mirrors the verification_runs import immediately above.
from .vector_index_state import (  # noqa: F401
    VECTOR_INDEX_BACKENDS,
    DEFAULT_SCOPE as VECTOR_INDEX_DEFAULT_SCOPE,
    _migrate_vector_index_state,
    get_vector_index_state,
    list_vector_index_states,
    upsert_vector_index_state,
    record_vector_backend_benchmark,
)


# 15610335 — external Pixi detached-environment registry, per worktree.
# Records where each worktree's detached environment resolved to on disk
# (outside the git-tracked tree) so orphan_reaper / pixi_env_retention can
# reclaim it once the owning worktree is gone. Mirrors the worktree_manifest
# import above — a single-table, no-state-machine shape.
from .worktrees import (  # noqa: F401
    _migrate_pixi_env_roots,
    register_pixi_env_root,
    get_pixi_env_root_for_worktree,
    list_unreclaimed_pixi_env_roots,
    mark_pixi_env_root_reclaimed,
)


# 9154aa9a — durable executor_report / planner_checkpoint records + corrective
# lineage. Imported last (after board_snapshot, needed by
# meridian.handoff.record_executor_report's board_revision_hash capture, and
# after sprint_items, needed by get_project_item_index) — a single-table,
# no-state-machine shape, mirroring the verification_runs import above.
from .executor_reports import (  # noqa: F401
    EXECUTOR_REPORT_STATUSES,
    canonical_report_hash,
    _migrate_executor_reports_table,
    create_executor_report,
    get_executor_report,
    list_executor_reports,
    update_executor_report_status,
    mark_executor_report_accepted,
)

# 9149e132 — typed, code-linked decision evidence + deterministic planning
# retrieval. Imported last (after everything above) — a single-table,
# no-state-machine shape, mirroring the vector_index_state import above.
# Wired into planning_search via the _PLANNING_SOURCE_SPECS["decision_evidence"]
# entry earlier in this file, not via anything imported here.
from .decision_evidence import (  # noqa: F401
    DECISION_EVIDENCE_STATUSES,
    _migrate_decision_evidence,
    create_decision_evidence,
    get_decision_evidence,
    list_decision_evidence,
    supersede_decision_evidence,
    reverse_decision_evidence,
)

# 9e83be4a (Round 1 proposal e143949d) — canonical, versioned, append-only
# ExecutionEvent storage scaffold (meridian.db.ai_log). Schema/contract only
# — see meridian.ai_log's module docstring for scope. Imported last (after
# everything above), a single-table, no-state-machine shape mirroring the
# decision_evidence import immediately above. Nothing in this codebase calls
# append_event yet — no capture/ingestion pipeline is wired to this table.
# ea972129 additionally adds AiLogStore + purge_events_before (retention) —
# see meridian.db.ai_log's module docstring's "ea972129 ... RETENTION" note.
# c0168425 additionally adds export_events (implementation follow-up to
# ea972129's design — see meridian.db.ai_log's "c0168425 — export" note).
from .ai_log import (  # noqa: F401
    AiLogStore,
    _migrate_ai_log_events_table,
    append_event,
    export_events,
    get_event,
    list_events,
    purge_events_before,
)


# d8481276 — versioned hosted-default/scoped-profile persistence (PROFILE-1
# contract, meridian.profile_contract). Imported after get_project/
# get_project_settings are already bound onto this module's namespace (both
# defined earlier in this file) — profile_layers.get_effective_profile reads
# them at call time via `from meridian.db import ...`, not at import time, so
# strict ordering isn't load-bearing here, but this placement matches the
# other db/*.py submodules' "imported after its dependencies" convention.
from .profile_layers import (  # noqa: F401
    get_profile_layer,
    list_profile_layers,
    set_profile_layer,
    reset_profile_layer,
    transition_hosted_default_lifecycle,
    get_profile_layer_revisions,
    get_effective_profile,
    _restore_profile_layer_row,
)
