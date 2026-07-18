"""SQLite / Postgres migration functions for Meridian.

All functions are pure append-only schema migrations called once at startup
by :func:.  Extracted from db/__init__.py to keep
that module manageable.
"""
from __future__ import annotations

import os
import re
from typing import Any

import aiosqlite

__all__ = ['_migrate_task_log_backlog_future', '_migrate_task_log_backburner', '_migrate_task_log_hitl', '_column_exists', '_migrate_add_column_if_missing', '_migrate_human_identity', '_migrate_v24_task_tree_and_framework', '_migrate_v25_feedback_and_notifications', '_migrate_v33_hitl_kind_payload', '_migrate_v34_hitl_auto_answer', '_migrate_v34_workspace_settings', '_migrate_dunning_fields', '_migrate_overage_fields', '_migrate_v26_client_type', '_migrate_ntfy_notifications', '_migrate_notify_email', '_migrate_github_integration', '_migrate_sprint_item_dependencies', '_migrate_v09_notes_and_magic_links', '_migrate_v24_pinned_decisions_and_hitl', '_migrate_goal_field_timestamps', '_migrate_task_claims', '_migrate_task_sprint_link', '_migrate_session_type', '_migrate_session_summary', '_migrate_parent_session_id', '_migrate_decisions', '_migrate_goal_mode', '_migrate_worker_pid', '_migrate_rewind_token', '_migrate_project_settings', '_migrate_neon_pool_projects_free_tier', '_migrate_tenants_free_plan', '_migrate_decisions_free_category', '_migrate_sessions_archived', '_migrate_goal_hierarchy', '_migrate_sprint_items_v2', '_migrate_drop_chat_tables', '_migrate_hosted_tables', '_migrate_session_notes', '_migrate_milestone_type', '_migrate_executor_runs', '_migrate_file_locks', '_migrate_file_symbol_claims', '_migrate_blog_posts', '_migrate_workspace_layer', '_migrate_checkpoint_data', 'init_hosted_tables', '_migrate_sprint_item_tree', '_migrate_api_token_type', '_migrate_api_tokens_expires_at', '_migrate_github_to_projects', '_migrate_touches_files', '_migrate_touches_resources', '_migrate_resource_locks', '_migrate_sprint_item_stall_count', '_migrate_oauth_codes_table', '_migrate_device_codes_table', '_migrate_device_codes_denied_polled', '_migrate_sprint_items_indeterminate', '_migrate_sprint_items_provisional_complete', '_migrate_workspace_members_rbac', '_migrate_workspace_members_project_scope', '_migrate_project_icon', '_migrate_project_parent_id', '_internal_emails', '_migrate_tenants_is_internal', '_migrate_admin_plan', '_migrate_active_worktrees', '_migrate_workspace_tenant_isolation', '_migrate_workspace_sprint_board', '_migrate_registered_hostnames', '_migrate_queued_session', '_migrate_pending_goal', '_migrate_parallel_safety', '_migrate_changelog_entries', '_migrate_agent_instructions', '_migrate_note_kind', '_migrate_tunnel_active', '_backfill_agent_instructions', '_migrate_code_intel', '_migrate_tunnel_plugins', '_migrate_tunnel_plugins_by_host', '_migrate_notes_priority', '_migrate_task_log_kind', '_migrate_note_slug', '_slugify_note', '_migrate_oauth_refresh_tokens', '_migrate_decision_priority_edit_log', '_migrate_code_anchored_notes', '_migrate_note_source', '_migrate_session_sprint_version', '_migrate_project_execution_mode', '_migrate_decision_code_anchor', '_migrate_session_graph_snapshots', '_migrate_agent_tasks_table', '_migrate_sprint_item_owner', '_migrate_session_note_kind', '_migrate_handoffs_table', '_migrate_decision_assumption', '_migrate_github_connections', '_migrate_sprint_item_quality_gates', '_migrate_parallel_primitives', '_migrate_project_status_priority', '_migrate_signup_attempts', '_migrate_user_session_metadata', '_migrate_provision_queue', '_migrate_codebase_graph_entities', '_migrate_insights_table', '_migrate_sprint_item_slug', '_migrate_sprint_item_nickname', '_migrate_capture_insight_notes_to_insights', '_migrate_blog_posts_tenant', '_migrate_session_goal_compliance', '_migrate_sprint_item_pointers', '_migrate_sprint_item_deferral', '_migrate_sprint_item_priority_blocker', '_migrate_sprint_item_wave', '_migrate_mcp_rate_counters', '_migrate_workspace_proposals', '_migrate_pending_goal_at', '_migrate_file_patch_counters', '_migrate_session_activity', '_migrate_sprint_item_resources_amended', '_migrate_connection_events', '_migrate_redis_overage_fields', '_migrate_sprint_version_descriptions', '_migrate_workspace_settings_active_session_threshold', '_migrate_sprint_item_sprint_name', '_migrate_proposal_slug_nickname', '_migrate_decision_slug_nickname', '_migrate_note_nickname', '_migrate_sprint_item_prospect_bypass', '_migrate_handoff_tokens', '_migrate_wave_gate_results', '_migrate_wave_gate_configs', '_migrate_server_logs', '_migrate_custom_hooks', '_migrate_sprint_item_require_verification', '_migrate_sprint_item_verifications_table', '_migrate_proposal_github_issue']

async def _migrate_task_log_backlog_future(db: aiosqlite.Connection) -> None:
    """Rebuild ``task_log`` to add 'backlog' and 'future' statuses (v1.9.x).

    SQLite cannot ALTER a CHECK constraint, so the table is rebuilt in-place
    with the extended status list. No-op when already migrated.
    """
    async with db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='task_log'"
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return
    table_sql = (row["sql"] if isinstance(row, dict) else row[0]) or ""
    if "'backlog'" in table_sql:
        return  # already migrated
    # Introspect exact columns â€” handles any schema version (4, 8, 10 cols)
    async with db.execute("PRAGMA table_info(task_log)") as _cur:
        _col_rows = list(await _cur.fetchall())
    _col_names = [(r["name"] if isinstance(r, dict) else r[1]) for r in _col_rows]

    def _make_col_def(r: Any) -> str:
        cid, name, ctype, notnull, dflt, pk = r
        if pk:
            return f"{name} {ctype} PRIMARY KEY"
        if name == "status":
            return (
                f"{name} {ctype} NOT NULL DEFAULT 'done' CHECK "
                "(status IN ('pending','in_progress','done','failed',"
                "'pending-hitl','backlog','future'))"
            )
        parts = [f"{name} {ctype}"]
        if notnull:
            parts.append("NOT NULL")
        if dflt is not None:
            # PRAGMA returns datetime('now') without outer parens; wrap if needed
            wrapped = dflt if dflt.startswith("'") or dflt.lstrip('-').isdigit() else f"({dflt})"
            parts.append(f"DEFAULT {wrapped}")
        return " ".join(parts)

    col_defs = ",\n            ".join(_make_col_def(r) for r in _col_rows)
    col_list = ", ".join(_col_names)

    await db.executescript(
        f"""
        BEGIN;
        ALTER TABLE task_log RENAME TO task_log_pre_backlog;
        CREATE TABLE task_log (
            {col_defs}
        );
        INSERT INTO task_log ({col_list})
        SELECT {col_list} FROM task_log_pre_backlog;
        DROP TABLE task_log_pre_backlog;
        CREATE INDEX IF NOT EXISTS idx_tasks_project
            ON task_log(project_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_tasks_session
            ON task_log(session_id);
        COMMIT;
        """
    )


async def _migrate_task_log_backburner(db: aiosqlite.Connection) -> None:
    """Rebuild ``task_log`` to add 'backburner' status (v2.5.x).

    SQLite cannot ALTER a CHECK constraint, so the table is rebuilt in-place
    with the extended status list. No-op when already migrated.
    """
    async with db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='task_log'"
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return
    table_sql = (row["sql"] if isinstance(row, dict) else row[0]) or ""
    if "'backburner'" in table_sql:
        return  # already migrated
    async with db.execute("PRAGMA table_info(task_log)") as _cur:
        _col_rows = list(await _cur.fetchall())
    _col_names = [(r["name"] if isinstance(r, dict) else r[1]) for r in _col_rows]

    def _make_col_def(r: Any) -> str:
        cid, name, ctype, notnull, dflt, pk = r
        if pk:
            return f"{name} {ctype} PRIMARY KEY"
        if name == "status":
            return (
                f"{name} {ctype} NOT NULL DEFAULT 'done' CHECK "
                "(status IN ('pending','in_progress','done','failed',"
                "'pending-hitl','backlog','future','backburner'))"
            )
        parts = [f"{name} {ctype}"]
        if notnull:
            parts.append("NOT NULL")
        if dflt is not None:
            wrapped = dflt if dflt.startswith("'") or dflt.lstrip('-').isdigit() else f"({dflt})"
            parts.append(f"DEFAULT {wrapped}")
        return " ".join(parts)

    col_defs = ",\n            ".join(_make_col_def(r) for r in _col_rows)
    col_list = ", ".join(_col_names)

    await db.executescript(
        f"""
        BEGIN;
        ALTER TABLE task_log RENAME TO task_log_pre_backburner;
        CREATE TABLE task_log (
            {col_defs}
        );
        INSERT INTO task_log ({col_list})
        SELECT {col_list} FROM task_log_pre_backburner;
        DROP TABLE task_log_pre_backburner;
        CREATE INDEX IF NOT EXISTS idx_tasks_project
            ON task_log(project_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_tasks_session
            ON task_log(session_id);
        COMMIT;
        """
    )


async def _migrate_task_log_hitl(db: aiosqlite.Connection) -> None:
    """Rebuild ``task_log`` if its CHECK constraint predates v0.2.0.

    SQLite can't ``ALTER`` a CHECK constraint, so on an older database we
    rebuild the table in place: copy rows out, drop, recreate with the new
    constraint, copy rows back. No-op when the schema is already current.
    """
    async with db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='task_log'"
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return
    table_sql = (row["sql"] if isinstance(row, dict) else row[0]) or ""
    if "pending-hitl" in table_sql:
        return  # already migrated

    await db.executescript(
        """
        BEGIN;
        ALTER TABLE task_log RENAME TO task_log_v01;
        CREATE TABLE task_log (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES sessions(id),
            project_id TEXT NOT NULL REFERENCES projects(id),
            description TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'done'
                CHECK (status IN ('pending','in_progress','done','failed','pending-hitl')),
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        INSERT INTO task_log (id, session_id, project_id, description, status, created_at)
        SELECT id, session_id, project_id, description, status, created_at
        FROM task_log_v01;
        DROP TABLE task_log_v01;
        CREATE INDEX IF NOT EXISTS idx_tasks_project
            ON task_log(project_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_tasks_session
            ON task_log(session_id);
        COMMIT;
        """
    )


async def _column_exists(
    db: aiosqlite.Connection, table: str, column: str
) -> bool:
    """Return True if ``column`` already exists on ``table`` in this DB."""
    async with db.execute(f"PRAGMA table_info({table})") as cur:
        rows = await cur.fetchall()
    return any((row["name"] if isinstance(row, dict) else row[1]) == column for row in rows)


async def _migrate_add_column_if_missing(
    db: aiosqlite.Connection, table: str, column: str, decl: str
) -> None:
    """Idempotently ``ALTER TABLE ADD COLUMN`` if it's not already there."""
    if not await _column_exists(db, table, column):
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        await db.commit()


async def _migrate_project_status_priority(db: aiosqlite.Connection) -> None:
    """8db00fcb — project organization: status (active|parked|archived) +
    priority (P0|P1|P2). Enums are enforced at the app layer (SQLite ADD COLUMN
    can't carry a CHECK); fresh DBs get the CHECK from the CREATE TABLE."""
    await _migrate_add_column_if_missing(
        db, "projects", "status", "TEXT NOT NULL DEFAULT 'active'"
    )
    await _migrate_add_column_if_missing(
        db, "projects", "priority", "TEXT NOT NULL DEFAULT 'P2'"
    )


async def _migrate_signup_attempts(db: aiosqlite.Connection) -> None:
    """925909aa — persistent per-IP signup-attempt log for magic-link abuse
    limiting (survives restarts, unlike the in-process slowapi window). Stores
    only salted hashes of the IP + email, never the raw values."""
    await db.executescript(
        """
        CREATE TABLE IF NOT EXISTS signup_attempts (
            id TEXT PRIMARY KEY,
            ip_hash TEXT NOT NULL,
            email_hash TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_signup_attempts_ip
            ON signup_attempts(ip_hash, created_at);
        """
    )
    await db.commit()


async def _migrate_user_session_metadata(db: aiosqlite.Connection) -> None:
    """3c28450d — device metadata on user_sessions for the active-sessions view:
    user_agent, ip, and last_seen_at (all nullable, best-effort)."""
    await _migrate_add_column_if_missing(db, "user_sessions", "user_agent", "TEXT")
    await _migrate_add_column_if_missing(db, "user_sessions", "ip", "TEXT")
    await _migrate_add_column_if_missing(db, "user_sessions", "last_seen_at", "TEXT")


async def _migrate_provision_queue(db: aiosqlite.Connection) -> None:
    """4c559d4e — durable provisioning queue so a failed Neon provision is
    retried later instead of being lost (fixes Kyle-class onboarding failures)."""
    await db.executescript(
        """
        CREATE TABLE IF NOT EXISTS provision_queue (
            tenant_id TEXT PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_provision_queue_status
            ON provision_queue(status);
        """
    )
    await db.commit()


async def _migrate_codebase_graph_entities(db: aiosqlite.Connection) -> None:
    """c00b1ccf — opt-in cached codebase-graph snapshot: entities (symbols/files)
    persisted per project so handoffs can surface code pointers offline (no live
    code-intel tunnel needed)."""
    await db.executescript(
        """
        CREATE TABLE IF NOT EXISTS codebase_graph_entities (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            qualified_name TEXT NOT NULL,
            file TEXT,
            kind TEXT,
            signature TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_cge_project
            ON codebase_graph_entities(project_id);
        """
    )
    await db.commit()


def _slugify_note(title: str) -> str:
    """Kebab-case a note title into a URL/handle-safe slug.

    Lowercase, collapse any run of non-alphanumerics into a single dash, trim
    leading/trailing dashes. Empty titles fall back to ``note`` so the column is
    never blank. Mirrors db._slugify_note (kept here to avoid an import cycle —
    migrations is imported *by* db/__init__)."""
    slug = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    return slug or "note"


async def _migrate_human_identity(db: aiosqlite.Connection) -> None:
    """v0.3.2 â€” add nullable human-identity columns to legacy DBs."""
    await _migrate_add_column_if_missing(db, "projects", "creator_human_id", "TEXT")
    await _migrate_add_column_if_missing(db, "sessions", "human_id", "TEXT")


async def _migrate_v24_task_tree_and_framework(db: aiosqlite.Connection) -> None:
    """v2.4 â€” task_log.parent_task_id (task tree for sub-agent work) and
    sessions.agent_framework (claude_code | cursor | windsurf | langgraph
    | autogen | openviking | custom). Also projects.project_token for the
    POST /events webhook intake (framework integrations push events with
    X-Meridian-Token header)."""
    await _migrate_add_column_if_missing(db, "task_log", "parent_task_id", "TEXT")
    await _migrate_add_column_if_missing(
        db, "sessions", "agent_framework", "TEXT DEFAULT 'claude_code'"
    )
    await _migrate_add_column_if_missing(db, "projects", "project_token", "TEXT")


async def _migrate_v25_feedback_and_notifications(db: aiosqlite.Connection) -> None:
    """v2.5 â€” sprint_items feedback columns + tenants notification_prefs."""
    await _migrate_add_column_if_missing(
        db, "sprint_items", "feedback_thumb", "SMALLINT"
    )
    await _migrate_add_column_if_missing(
        db, "sprint_items", "feedback_note", "TEXT"
    )
    await _migrate_add_column_if_missing(
        db, "tenants", "notification_prefs", "TEXT NOT NULL DEFAULT '{}'"
    )


async def _migrate_v33_hitl_kind_payload(db: aiosqlite.Connection) -> None:
    """v3.3 â€” hitl_requests.kind discriminates a normal 'question' from an
    'md_section_update' (a proposed markdown section replacement); payload is a
    JSON blob carrying {file, anchor, content, base_hash, diff} for the latter."""
    await _migrate_add_column_if_missing(
        db, "hitl_requests", "kind", "TEXT NOT NULL DEFAULT 'question'"
    )
    await _migrate_add_column_if_missing(db, "hitl_requests", "payload", "TEXT")


async def _migrate_v34_hitl_auto_answer(db: aiosqlite.Connection) -> None:
    """v3.4 â€” projects.hitl_auto_answer: when on, request_hitl auto-resolves
    immediately (first option, answered_by='auto') so trusted projects never
    block on the HITL queue. Auto-answered rows stay in the queue for audit."""
    await _migrate_add_column_if_missing(
        db, "projects", "hitl_auto_answer", "INTEGER NOT NULL DEFAULT 0"
    )


async def _migrate_v34_workspace_settings(db: aiosqlite.Connection) -> None:
    """v3.4 â€” workspace_settings singleton table (tenant-global defaults).
    CREATE_TABLES covers fresh DBs; this is the upgrade path for existing ones."""
    await db.executescript(
        "CREATE TABLE IF NOT EXISTS workspace_settings ("
        "    id TEXT PRIMARY KEY DEFAULT 'singleton',"
        "    hitl_auto_answer_default INTEGER NOT NULL DEFAULT 0,"
        "    sprint_name_default TEXT,"
        "    updated_at TEXT NOT NULL DEFAULT (datetime('now'))"
        ");"
    )
    await db.commit()
    # v2.8 â€” display_name: tenant-global identity applied to hook/Codex sessions
    # that don't pass an explicit human_id, so their activity is attributed to a
    # person on the timeline instead of "(unknown)".
    await _migrate_add_column_if_missing(db, "workspace_settings", "display_name", "TEXT")
    await _migrate_add_column_if_missing(
        db, "workspace_settings", "log_task_sprint_nudge_threshold", "INTEGER NOT NULL DEFAULT 5"
    )
    # v1.1 — per-user handoff template (NULL = server default Jinja2 template)
    await _migrate_add_column_if_missing(db, "workspace_settings", "handoff_template", "TEXT")
    # 0bf67524 — workspace-default settings that seed NEW projects in the
    # workspace (cascade-at-creation). NULL = no default (project uses its own
    # built-in default). execution_mode_default ∈ {autonomous, interactive};
    # code_intel_enabled_default ∈ {0, 1}.
    await _migrate_add_column_if_missing(db, "workspace_settings", "execution_mode_default", "TEXT")
    await _migrate_add_column_if_missing(db, "workspace_settings", "code_intel_enabled_default", "INTEGER")
    # 76cf8bda — /loop auto-continue workspace default (1 = on for new sessions).
    await _migrate_add_column_if_missing(
        db, "workspace_settings", "loop_enabled_default", "INTEGER NOT NULL DEFAULT 1"
    )
    # bf51b12e — planner context-refresh: workspace-global nudge config. When
    # auto_refresh_enabled, the MCP dispatch hook attaches a compact context
    # refresh to planner (non-executor) tool results either on a trigger tool or
    # every refresh_interval_turns calls. refresh_triggers is a JSON list of tool
    # names (NULL = use the built-in default trigger set).
    await _migrate_add_column_if_missing(
        db, "workspace_settings", "auto_refresh_enabled", "INTEGER NOT NULL DEFAULT 0"
    )
    await _migrate_add_column_if_missing(
        db, "workspace_settings", "refresh_interval_turns", "INTEGER NOT NULL DEFAULT 10"
    )
    await _migrate_add_column_if_missing(db, "workspace_settings", "refresh_triggers", "TEXT")
    # 36fea6ca — inline each pending item's RESOLVED pointers directly in the
    # handoff markdown (default 1 = on) so a resuming session sees them without a
    # separate resolve_sprint_item_pointers call. A stored 0 keeps them DB-only.
    await _migrate_add_column_if_missing(
        db, "workspace_settings", "handoff_inline_pointers", "INTEGER NOT NULL DEFAULT 1"
    )


async def _migrate_dunning_fields(db: aiosqlite.Connection) -> None:
    """v2.6 â€” dunning: track when a tenant's payment first failed."""
    await _migrate_add_column_if_missing(db, "tenants", "payment_failed_at", "TEXT")
    await _migrate_add_column_if_missing(db, "tenants", "dunning_email_sent", "INTEGER NOT NULL DEFAULT 0")
    # v2.7 â€” add github_sub for GitHub OAuth
    await _migrate_add_column_if_missing(db, "tenants", "github_sub", "TEXT")


async def _migrate_overage_fields(db: aiosqlite.Connection) -> None:
    """v2.6 â€” per-tenant compute + storage overage tracking and caps."""
    await _migrate_add_column_if_missing(db, "tenants", "compute_overage_cap_usd", "NUMERIC(8,2) DEFAULT 0")
    await _migrate_add_column_if_missing(db, "tenants", "storage_overage_cap_usd", "NUMERIC(8,2) DEFAULT 0")
    await _migrate_add_column_if_missing(db, "tenants", "compute_cu_hours_used", "NUMERIC(10,4) DEFAULT 0")
    await _migrate_add_column_if_missing(db, "tenants", "storage_gb_used", "NUMERIC(10,4) DEFAULT 0")
    await _migrate_add_column_if_missing(db, "tenants", "overage_reset_at", "TEXT")
    await _migrate_add_column_if_missing(db, "tenants", "compute_throttled_at", "TEXT")


async def _migrate_v26_client_type(db: aiosqlite.Connection) -> None:
    """v2.6 â€” sessions.client_type: claude-code | claude-desktop | cursor | other."""
    await _migrate_add_column_if_missing(db, "sessions", "client_type", "TEXT")


async def _migrate_ntfy_notifications(db: aiosqlite.Connection) -> None:
    """v1.0.1 â€” projects.ntfy_url: optional ntfy push notification endpoint.

    Stores the ntfy server URL + topic (e.g. https://ntfy.sh/my-topic) so
    the server can push HITL and sprint-complete notifications without email.
    Works for self-hosted (SQLite) and hosted (Postgres via the pg_adapter).
    """
    await _migrate_add_column_if_missing(db, "projects", "ntfy_url", "TEXT")


async def _migrate_notify_email(db: aiosqlite.Connection) -> None:
    """v2.5.1 â€” projects.notify_email: separate email column for notifications."""
    await _migrate_add_column_if_missing(db, "projects", "notify_email", "TEXT")


async def _migrate_github_integration(db: aiosqlite.Connection) -> None:
    """v3.1 â€” per-tenant GitHub PAT and repo for the GitHub MCP tools.

    github_pat stores the encrypted personal access token.
    github_repo stores owner/repo (e.g. "acme/myapp").
    github_branch stores the default branch (default: "main").
    """
    await _migrate_add_column_if_missing(db, "tenants", "github_pat", "TEXT")
    await _migrate_add_column_if_missing(db, "tenants", "github_repo", "TEXT")
    await _migrate_add_column_if_missing(db, "tenants", "github_branch", "TEXT")


async def _migrate_sprint_item_dependencies(db: aiosqlite.Connection) -> None:
    """v2.6 â€” sprint_items dependency tracking.

    depends_on: foreign key to parent sprint item (NULL = no dependency).
    failure_mode: what to do when depends_on item has failed.
      'continue' (default) â€” this item can still be claimed.
      'stop' â€” this item is blocked when the parent has failed.
    """
    await _migrate_add_column_if_missing(db, "sprint_items", "depends_on", "TEXT")
    await _migrate_add_column_if_missing(
        db, "sprint_items", "failure_mode", "TEXT NOT NULL DEFAULT 'continue'"
    )


async def _migrate_v09_notes_and_magic_links(db: aiosqlite.Connection) -> None:
    """v0.9 â€” project_notes (per-project wiki) + magic_link_tokens
    (email magic-link auth). Both new in v0.9; idempotent CREATE."""
    await db.executescript(
        """
        CREATE TABLE IF NOT EXISTS project_notes (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            tags TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_notes_project
            ON project_notes(project_id);

        CREATE TABLE IF NOT EXISTS magic_link_tokens (
            id TEXT PRIMARY KEY,
            email TEXT NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            used_at TEXT,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_magic_email
            ON magic_link_tokens(email, used_at);
        """
    )
    await db.commit()


async def _migrate_v24_pinned_decisions_and_hitl(db: aiosqlite.Connection) -> None:
    """v2.4 â€” decisions_pinned + hitl_requests tables. CREATE_TABLES adds
    them on fresh DBs; this migration covers existing dev/prod DBs."""
    await db.executescript(
        """
        CREATE TABLE IF NOT EXISTS decisions_pinned (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'TECHNICAL',
            status TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active','superseded')),
            superseded_by TEXT REFERENCES decisions_pinned(id),
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_decisions_pinned_project
            ON decisions_pinned(project_id, status);

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
            answered_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_hitl_project
            ON hitl_requests(project_id, status);
        CREATE INDEX IF NOT EXISTS idx_hitl_assigned
            ON hitl_requests(assigned_to, status);
        """
    )
    await db.commit()


async def _migrate_goal_field_timestamps(db: aiosqlite.Connection) -> None:
    """v2.3 â€” per-field updated-at timestamps on goal_states.

    Pre-v2.3 every goal save inserted a new row, so per-field freshness
    could be walked from row history. v2.3's dedup means sprint-only
    changes are in-place UPDATEs on the latest row, collapsing the
    per-field history. Three new nullable columns let us record which
    field changed when. ``get_goal_field_ages`` reads these; absent
    values fall back to the row's ``updated_at`` so pre-migration data
    still renders.
    """
    await _migrate_add_column_if_missing(db, "goal_states", "ns_updated_at", "TEXT")
    await _migrate_add_column_if_missing(db, "goal_states", "content_updated_at", "TEXT")
    await _migrate_add_column_if_missing(db, "goal_states", "sprint_updated_at", "TEXT")


async def _migrate_task_claims(db: aiosqlite.Connection) -> None:
    """v0.3.3 â€” add ``claimed_by`` / ``claimed_at`` columns for the
    distributed task lock. Both nullable, so ALTER TABLE is safe."""
    await _migrate_add_column_if_missing(db, "task_log", "claimed_by", "TEXT")
    await _migrate_add_column_if_missing(db, "task_log", "claimed_at", "TEXT")


async def _migrate_task_sprint_link(db: aiosqlite.Connection) -> None:
    """v2.6 â€” link task_log rows back to their sprint item when applicable."""
    await _migrate_add_column_if_missing(
        db, "task_log", "sprint_item_id", "TEXT"
    )


async def _migrate_session_type(db: aiosqlite.Connection) -> None:
    """v1.2.0 â€” distinguish human vs worker sessions.

    'human' = the default startup protocol's session (full goal +
    decisions + ambient context). 'worker' = a slim context built
    by ``start_worker_session`` with just the task it needs to ship.
    """
    await _migrate_add_column_if_missing(
        db, "sessions", "session_type", "TEXT DEFAULT 'human'"
    )


async def _migrate_session_summary(db: aiosqlite.Connection) -> None:
    """v1.2.1 â€” store an LLM-generated session retrospective on the
    session row itself. Populated by :func:`summarize_session` on
    handoff / TTL expiry when the session shipped >=3 tasks."""
    await _migrate_add_column_if_missing(
        db, "sessions", "session_summary", "TEXT"
    )


async def _migrate_parent_session_id(db: aiosqlite.Connection) -> None:
    """v1.2.1 â€” propagate parent session through enqueued worker
    subprocesses. Stored on each task_log row so the timeline can
    show 'this task was kicked off by that other session'."""
    await _migrate_add_column_if_missing(
        db, "task_log", "parent_session_id", "TEXT"
    )


async def _migrate_decisions(db: aiosqlite.Connection) -> None:
    """v1.1.4 â€” append-only decisions log per project.

    Stored as a TEXT blob on ``projects``. New entries are prepended
    by :func:`set_decision` with a UTC date stamp so the file reads
    newest-first.
    """
    await _migrate_add_column_if_missing(db, "projects", "decisions", "TEXT")


async def _migrate_goal_mode(db: aiosqlite.Connection) -> None:
    """v0.4.2 â€” add ``goal_mode`` column to projects.

    SQLite ``ALTER TABLE ADD COLUMN`` cannot include a CHECK constraint,
    so we add the column with a plain default and rely on the Python
    layer (``set_goal_mode``) to validate the input value.
    """
    await _migrate_add_column_if_missing(
        db,
        "projects",
        "goal_mode",
        "TEXT NOT NULL DEFAULT 'manual'",
    )


async def _migrate_worker_pid(db: aiosqlite.Connection) -> None:
    """v1.0.1 â€” add ``worker_pid`` column for the PID watchdog.

    Stores the OS PID of the subprocess spawned by ``enqueue_claude_task``.
    The auto-summary loop uses this to detect orphaned in_progress tasks
    whose worker process has died."""
    await _migrate_add_column_if_missing(db, "task_log", "worker_pid", "INTEGER")


async def _migrate_rewind_token(db: aiosqlite.Connection) -> None:
    """v1.3.0 â€” add ``rewind_token`` column to projects for shareable
    read-only links into the rewind endpoint. Nullable: a project
    has no token until POST /rewind-token mints one."""
    await _migrate_add_column_if_missing(db, "projects", "rewind_token", "TEXT")


async def _migrate_project_settings(db: aiosqlite.Connection) -> None:
    """v2.6.1 â€” add per-project settings columns."""
    await _migrate_add_column_if_missing(
        db,
        "projects",
        "max_pinned_decisions",
        "INTEGER NOT NULL DEFAULT 20",
    )
    await _migrate_add_column_if_missing(
        db,
        "projects",
        "executor_config",
        "TEXT",
    )


async def _migrate_neon_pool_projects_free_tier(db: aiosqlite.Connection) -> None:
    """v3.1 â€” widen ``neon_pool_projects.tier`` to include ``free``."""
    async with db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='neon_pool_projects'"
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return
    ddl = (row["sql"] if isinstance(row, dict) else row[0]) or ""
    if "'free'" in ddl or '"free"' in ddl:
        return
    await db.execute("PRAGMA foreign_keys = OFF")
    await db.executescript(
        """
        CREATE TABLE neon_pool_projects_new (
            id TEXT PRIMARY KEY,
            neon_project_id TEXT NOT NULL UNIQUE,
            tier TEXT NOT NULL DEFAULT 'standard'
                CHECK (tier IN ('free','standard','pro')),
            customer_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        INSERT INTO neon_pool_projects_new (id, neon_project_id, tier, customer_count, created_at)
            SELECT id, neon_project_id, tier, customer_count, created_at
            FROM neon_pool_projects;
        DROP TABLE neon_pool_projects;
        ALTER TABLE neon_pool_projects_new RENAME TO neon_pool_projects;
        CREATE INDEX IF NOT EXISTS idx_neon_pool_projects_tier
            ON neon_pool_projects(tier, customer_count);
        """
    )
    await db.execute("PRAGMA foreign_keys = ON")
    await db.commit()


async def _migrate_tenants_free_plan(db: aiosqlite.Connection) -> None:
    """v2.9 â€” expand tenants.plan to allow 'free' and add trial/expiry columns.

    The old plan CHECK constraint restricted values to 'standard'|'pro'.
    SQLite can't ALTER a CHECK, so rebuild the table if constrained.
    Also adds trial_started_at + inactivity_expires_at for the free tier.
    """
    async with db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='tenants'"
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return
    ddl = (row["sql"] if isinstance(row, dict) else row[0]) or ""
    needs_rebuild = "CHECK (plan IN" in ddl
    if needs_rebuild:
        await db.execute("PRAGMA foreign_keys = OFF")
        await db.executescript(
            """
            CREATE TABLE tenants_new (
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
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            INSERT INTO tenants_new (
                id, email, google_sub, github_sub, microsoft_sub,
                neon_project_id, neon_db_url, stripe_customer_id,
                stripe_metered_item_id, plan, pool_project_id,
                notification_prefs, created_at
            )
            SELECT
                id, email, google_sub, github_sub, microsoft_sub,
                neon_project_id, neon_db_url, stripe_customer_id,
                stripe_metered_item_id, plan, pool_project_id,
                COALESCE(notification_prefs, '{}'), created_at
            FROM tenants;
            DROP TABLE tenants;
            ALTER TABLE tenants_new RENAME TO tenants;
            """
        )
        await db.execute("PRAGMA foreign_keys = ON")
        await db.commit()
    # Idempotently add new columns (safe even after rebuild)
    await _migrate_add_column_if_missing(db, "tenants", "trial_started_at", "TEXT")
    await _migrate_add_column_if_missing(db, "tenants", "inactivity_expires_at", "TEXT")


async def _migrate_decisions_free_category(db: aiosqlite.Connection) -> None:
    """v2.9 â€” drop the hard category CHECK constraint on decisions_pinned.

    Category is now free-text (any string); the old enum was too rigid.
    SQLite can't DROP a CHECK constraint, so rebuild the table in place.
    No-op when the constraint is already absent.
    """
    async with db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='decisions_pinned'"
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return
    ddl = (row["sql"] if isinstance(row, dict) else row[0]) or ""
    if "CHECK (category IN" not in ddl:
        return  # already migrated or freshly created without constraint
    await db.execute("PRAGMA foreign_keys = OFF")
    await db.executescript(
        """
        CREATE TABLE decisions_pinned_new (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'TECHNICAL',
            status TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active','superseded')),
            superseded_by TEXT REFERENCES decisions_pinned_new(id),
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        INSERT INTO decisions_pinned_new
            SELECT id, project_id, title, body, category, status,
                   superseded_by, created_at, updated_at
            FROM decisions_pinned;
        DROP TABLE decisions_pinned;
        ALTER TABLE decisions_pinned_new RENAME TO decisions_pinned;
        CREATE INDEX IF NOT EXISTS idx_decisions_pinned_project
            ON decisions_pinned(project_id, status);
        """
    )
    await db.execute("PRAGMA foreign_keys = ON")
    await db.commit()


async def _migrate_sessions_archived(db: aiosqlite.Connection) -> None:
    """v1.8.x â€” add 'archived' to the sessions CHECK constraint.

    SQLite can't ALTER a CHECK constraint, so we rebuild the table in place
    when the existing CHECK doesn't include 'archived'. No-op when current.

    task_log has a FK â†’ sessions, so we disable FK enforcement for the
    rename/drop cycle (PRAGMA foreign_keys is a no-op inside a transaction,
    so it must be toggled at the Python level before executescript).
    """
    async with db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='sessions'"
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return
    if "archived" in ((row["sql"] if isinstance(row, dict) else row[0]) or ""):
        return  # already migrated

    # SQLite 3.26+ auto-rewrites FK references in other tables when a table is
    # RENAMED â€” so renaming sessions â†’ sessions_v17 would silently update
    # task_log's schema to say "REFERENCES sessions_v17" and break after the
    # drop.  The safe pattern is: create sessions_new, copy, DROP old (not
    # rename), then RENAME newâ†’sessions.  task_log's "REFERENCES sessions(id)"
    # is never touched and remains valid once sessions is back.
    await db.execute("PRAGMA foreign_keys = OFF")
    await db.executescript(
        """
        CREATE TABLE sessions_new (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id),
            name TEXT NOT NULL,
            human_id TEXT,
            session_type TEXT DEFAULT 'human',
            status TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active','idle','closed','archived')),
            last_seen TEXT NOT NULL DEFAULT (datetime('now')),
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            session_summary TEXT
        );
        INSERT INTO sessions_new
            SELECT id, project_id, name, human_id, session_type, status,
                   last_seen, created_at, session_summary
            FROM sessions;
        DROP TABLE sessions;
        ALTER TABLE sessions_new RENAME TO sessions;
        CREATE INDEX IF NOT EXISTS idx_sessions_project
            ON sessions(project_id, status);
        """
    )
    await db.execute("PRAGMA foreign_keys = ON")
    await db.commit()


async def _migrate_goal_hierarchy(db: aiosqlite.Connection) -> None:
    """v0.5.2 â€” add ``goal_north_star`` and ``goal_sprint`` columns.

    Seeding: for each project's latest goal row that has no north_star
    set yet, copy the current content into north_star so existing goals
    are promoted to the structured hierarchy automatically.
    """
    await _migrate_add_column_if_missing(
        db, "goal_states", "goal_north_star", "TEXT"
    )
    await _migrate_add_column_if_missing(
        db, "goal_states", "goal_sprint", "TEXT"
    )
    # Seed: promote content â†’ north_star for the latest version per project
    # where north_star is still NULL (i.e., legacy rows from before v0.5.2).
    await db.execute(
        """
        UPDATE goal_states
        SET goal_north_star = content
        WHERE goal_north_star IS NULL
          AND id IN (
              SELECT id FROM goal_states g2
              WHERE g2.project_id = goal_states.project_id
              ORDER BY version DESC
              LIMIT 1
          )
        """
    )
    await db.commit()


async def _migrate_sprint_items_v2(db: aiosqlite.Connection) -> None:
    """v1.9x â€” expand sprint_items: add item_group/pushed_to/human_id and
    widen status CHECK to include 'todo', 'failed', 'pushed'.

    Can't ALTER a CHECK constraint in SQLite, so the table is rebuilt in
    place using the create-copy-drop-rename pattern. sprint_items has no
    FK references FROM other tables (it only has a FK TO projects) so the
    RENAME is safe without toggling foreign_keys.

    No-op when the schema is already current (all new columns present and
    status constraint includes 'failed').
    """
    async with db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='sprint_items'"
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return  # fresh DB â€” CREATE_TABLES builds the current schema
    sql = (row["sql"] if isinstance(row, dict) else row[0]) or ""
    # Already migrated when all three new columns + 'failed' status are present.
    if ("item_group" in sql and "pushed_to" in sql
            and "human_id" in sql and "'failed'" in sql):
        return
    # Rebuild the table with the new schema.
    await db.executescript(
        """
        CREATE TABLE sprint_items_new (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id),
            version TEXT NOT NULL,
            title TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN
                    ('pending','todo','in_progress','done','failed','skipped','pushed')),
            item_group TEXT,
            pushed_to TEXT,
            human_id TEXT,
            added_at TEXT NOT NULL DEFAULT (datetime('now')),
            completed_at TEXT,
            task_id TEXT,
            notes TEXT
        );
        INSERT INTO sprint_items_new
            (id, project_id, version, title, status,
             added_at, completed_at, task_id, notes)
            SELECT id, project_id, version, title, status,
                   added_at, completed_at, task_id, notes
            FROM sprint_items;
        DROP TABLE sprint_items;
        ALTER TABLE sprint_items_new RENAME TO sprint_items;
        CREATE INDEX IF NOT EXISTS idx_sprint_items_project
            ON sprint_items(project_id, status);
        CREATE INDEX IF NOT EXISTS idx_sprint_items_version
            ON sprint_items(project_id, version);
        """
    )
    await db.commit()


async def _migrate_drop_chat_tables(db: aiosqlite.Connection) -> None:
    """v1.9.x â€” drop abandoned chat_sessions and chat_messages tables.

    These were removed in v1.1.0 when the in-dashboard chat feature was
    dropped. DROP IF EXISTS is idempotent â€” safe on both old and fresh DBs.
    """
    await db.execute("DROP TABLE IF EXISTS chat_messages")
    await db.execute("DROP TABLE IF EXISTS chat_sessions")
    await db.commit()


async def _migrate_hosted_tables(db: aiosqlite.Connection) -> None:
    """v2.0/v2.2 â€” add tenants, user_sessions, api_tokens, neon_pool_projects.

    Uses CREATE TABLE IF NOT EXISTS so it is idempotent on existing DBs.
    v2.2: plan column migrated free/teamâ†’standard; pool_project_id column added.
    """
    await db.executescript(
        """
        CREATE TABLE IF NOT EXISTS tenants (
            id TEXT PRIMARY KEY,
            email TEXT NOT NULL UNIQUE,
            google_sub TEXT UNIQUE,
            neon_project_id TEXT,
            neon_db_url TEXT,
            stripe_customer_id TEXT,
            plan TEXT NOT NULL DEFAULT 'standard',
            pool_project_id TEXT,
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
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS oauth_tokens (
            token_hash TEXT PRIMARY KEY,
            tenant_id TEXT REFERENCES tenants(id),
            client_id TEXT,
            exp BIGINT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS neon_pool_projects (
            id TEXT PRIMARY KEY,
            neon_project_id TEXT NOT NULL UNIQUE,
            tier TEXT NOT NULL DEFAULT 'standard',
            customer_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        """
    )
    # Migrate legacy plan values (freeâ†’standard, teamâ†’standard)
    await db.execute(
        "UPDATE tenants SET plan='standard' WHERE plan IN ('free','team')"
    )
    await _migrate_add_column_if_missing(db, "tenants", "pool_project_id", "TEXT")
    await _migrate_add_column_if_missing(db, "tenants", "microsoft_sub", "TEXT")
    await _migrate_add_column_if_missing(db, "tenants", "stripe_metered_item_id", "TEXT")
    await _migrate_github_integration(db)
    await db.commit()


async def _migrate_session_notes(db: aiosqlite.Connection) -> None:
    """v2.6 â€” create session_notes table if not present. Idempotent."""
    await db.execute(
        """CREATE TABLE IF NOT EXISTS session_notes (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )"""
    )
    await db.commit()


async def _migrate_milestone_type(db: aiosqlite.Connection) -> None:
    """v3.0 â€” add milestone_type to sprint_items. Idempotent."""
    await _migrate_add_column_if_missing(
        db, "sprint_items", "milestone_type", "TEXT NOT NULL DEFAULT 'task'"
    )


async def _migrate_executor_runs(db: aiosqlite.Connection) -> None:
    """v3.0 â€” create executor_runs table if not present. Idempotent."""
    await db.execute(
        """CREATE TABLE IF NOT EXISTS executor_runs (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            started_at TEXT NOT NULL DEFAULT (datetime('now')),
            ended_at TEXT,
            status TEXT NOT NULL DEFAULT 'running',
            transcript TEXT NOT NULL DEFAULT '',
            task_count INTEGER NOT NULL DEFAULT 0
        )"""
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_executor_runs_session ON executor_runs(session_id)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_executor_runs_project "
        "ON executor_runs(project_id, started_at DESC)"
    )
    await db.commit()


async def _migrate_file_locks(db: aiosqlite.Connection) -> None:
    """v3.1 â€” add file_locks table for cross-session edit coordination."""
    await db.execute(
        """CREATE TABLE IF NOT EXISTS file_locks (
            id TEXT PRIMARY KEY,
            file_path TEXT NOT NULL UNIQUE,
            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            claimed_at TEXT NOT NULL DEFAULT (datetime('now')),
            expires_at TEXT NOT NULL
        )"""
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_file_locks_session ON file_locks(session_id)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_file_locks_expires ON file_locks(expires_at)"
    )
    await db.commit()


async def _migrate_file_symbol_claims(db: aiosqlite.Connection) -> None:
    """4bac57ff â€” symbol-level parallel protection: per-symbol line-range claims."""
    await db.execute(
        """CREATE TABLE IF NOT EXISTS file_symbol_claims (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            file_path TEXT NOT NULL,
            symbol_name TEXT NOT NULL,
            symbol_type TEXT NOT NULL,
            line_start INTEGER NOT NULL,
            line_end INTEGER NOT NULL,
            claimed_at TEXT NOT NULL DEFAULT (datetime('now')),
            released_at TEXT
        )"""
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_file_symbol_claims_file ON file_symbol_claims(file_path)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_file_symbol_claims_session ON file_symbol_claims(session_id)"
    )
    # Defensive: add released_at for any DB created during this sprint's dev.
    await _migrate_add_column_if_missing(db, "file_symbol_claims", "released_at", "TEXT")
    await db.commit()


async def _migrate_blog_posts(db: aiosqlite.Connection) -> None:
    """6234f9b8 â€” blog_posts table for the admin Blog CMS.

    8843250f later widened the status CHECK to include 'archived' and added a
    nullable tenant_id (workspace scope); those are reflected here so fresh DBs
    are created with the final shape (_migrate_blog_posts_tenant then no-ops)."""
    await db.execute(
        """CREATE TABLE IF NOT EXISTS blog_posts (
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
        )"""
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_blog_posts_status ON blog_posts(status)"
    )
    # idx_blog_posts_tenant is created by _migrate_blog_posts_tenant, which first
    # ALTERs tenant_id onto pre-8843250f tables. Creating it here would crash on an
    # existing blog_posts that predates the column (CREATE TABLE IF NOT EXISTS
    # can't add it) — the same missing-column crash that took prod down on the
    # 2026-07-04 promote via the Postgres CORE schema. Mirror PG: index lives in
    # the tenant migration only.
    await db.commit()


async def _migrate_workspace_layer(db: aiosqlite.Connection) -> None:
    """v3.1 â€” workspace_notes + workspace_decisions tables (tenant-global,
    above projects). Idempotent."""
    await db.execute(
        """CREATE TABLE IF NOT EXISTS workspace_notes (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            tags TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )"""
    )
    await db.execute(
        """CREATE TABLE IF NOT EXISTS workspace_decisions (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'TECHNICAL',
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )"""
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_workspace_notes_created "
        "ON workspace_notes(created_at DESC)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_workspace_decisions_status "
        "ON workspace_decisions(status, created_at DESC)"
    )
    await db.commit()


async def _migrate_checkpoint_data(db: aiosqlite.Connection) -> None:
    """v3.1 â€” add sessions.checkpoint_data for per-session checkpoint snapshots
    (replaces the old checkpoint: project_notes hack). Idempotent. Also clears
    the legacy checkpoint:* notes now superseded by the column."""
    await _migrate_add_column_if_missing(db, "sessions", "checkpoint_data", "TEXT")
    await db.execute(
        "DELETE FROM project_notes WHERE title LIKE ?", ("checkpoint:%",)
    )
    await db.commit()


async def init_hosted_tables(db: aiosqlite.Connection) -> None:
    """Ensure hosted-tier tables exist on the given connection.

    Idempotent â€” safe to call on both fresh and existing databases.
    Delegates to ``_migrate_hosted_tables`` for SQLite; for Postgres the
    tables are already included in ``CREATE_TABLES_PG``.
    """
    await _migrate_hosted_tables(db)


async def _migrate_sprint_item_tree(db: aiosqlite.Connection) -> None:
    """Add parent_id, split_from, merged_into, merged_from to sprint_items."""
    await _migrate_add_column_if_missing(db, "sprint_items", "parent_id", "TEXT DEFAULT NULL")
    await _migrate_add_column_if_missing(db, "sprint_items", "split_from", "TEXT DEFAULT NULL")
    await _migrate_add_column_if_missing(db, "sprint_items", "merged_into", "TEXT DEFAULT NULL")
    await _migrate_add_column_if_missing(db, "sprint_items", "merged_from", "TEXT DEFAULT NULL")


async def _migrate_api_token_type(db: aiosqlite.Connection) -> None:
    """Add token_type column to api_tokens for read-only token support."""
    await _migrate_add_column_if_missing(
        db, "api_tokens", "token_type", "TEXT NOT NULL DEFAULT 'readwrite'"
    )


async def _migrate_api_tokens_expires_at(db: aiosqlite.Connection) -> None:
    """Add expires_at column to api_tokens for short-lived install tokens."""
    await _migrate_add_column_if_missing(db, "api_tokens", "expires_at", "TEXT")


async def _migrate_github_to_projects(db: aiosqlite.Connection) -> None:
    """Move github_repo + github_branch from tenants to projects.

    Adds the two columns to projects (idempotent). For SQLite installs where
    tenants and projects share one DB, copies any non-NULL tenant values to
    projects that were created by that tenant (matched via creator_human_id =
    tenant email). Hosted Neon DBs have projects only (no tenants table), so
    the copy step is skipped there â€” users re-connect their repo per project.
    """
    await _migrate_add_column_if_missing(db, "projects", "github_repo", "TEXT")
    await _migrate_add_column_if_missing(db, "projects", "github_branch", "TEXT")
    # Best-effort copy for SQLite installs (tenants table may not exist on Neon)
    try:
        async with db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='tenants'"
        ) as cur:
            row = await cur.fetchone()
        if row is not None:
            await db.execute(
                """UPDATE projects
                   SET github_repo = (
                         SELECT github_repo FROM tenants
                         WHERE email = projects.creator_human_id
                           AND github_repo IS NOT NULL
                       ),
                       github_branch = (
                         SELECT github_branch FROM tenants
                         WHERE email = projects.creator_human_id
                           AND github_branch IS NOT NULL
                       )
                   WHERE github_repo IS NULL
                     AND EXISTS (
                           SELECT 1 FROM tenants
                           WHERE email = projects.creator_human_id
                             AND github_repo IS NOT NULL
                         )"""
            )
            await db.commit()
    except Exception:  # noqa: BLE001
        pass


async def _migrate_touches_files(db: aiosqlite.Connection) -> None:
    """Add touches_files TEXT column to sprint_items for file conflict tracking."""
    await _migrate_add_column_if_missing(db, "sprint_items", "touches_files", "TEXT")


async def _migrate_touches_resources(db: aiosqlite.Connection) -> None:
    """501ec93f — sprint_items.touches_resources: typed resource identifiers.

    Generalizes touches_files (file:path only) to any conflict-bearing resource —
    file:path, db:migrations, mcp_tool:name, route:METHOD:/path, pypi:publish,
    github:tag. Stored as a JSON list of strings; NULL means "no declared
    resources". Consumed by get_parallelizable_groups for pre-fanout conflict
    detection and backed at runtime by the resource_locks table.
    """
    await _migrate_add_column_if_missing(db, "sprint_items", "touches_resources", "TEXT")


async def _migrate_sprint_item_stall_count(db: aiosqlite.Connection) -> None:
    """bc9259b8 — sprint_items.stall_count: worker stall auto-retry counter.

    Incremented each time a worker session closes (or goes stale) with the item
    still in_progress instead of completing it. The item is re-queued to pending
    while stall_count is within the retry budget, then marked failed silently.
    Nullable INTEGER defaulting to 0 so existing rows read as "never stalled".
    """
    await _migrate_add_column_if_missing(
        db, "sprint_items", "stall_count", "INTEGER NOT NULL DEFAULT 0"
    )


async def _migrate_resource_locks(db: aiosqlite.Connection) -> None:
    """501ec93f — resource_locks: generalize file_locks to any typed resource.

    Same TTL + UNIQUE primitive as file_locks (one holder per resource at a time,
    auto-expiring by explicit TTL or owning-session heartbeat) but keyed by a
    typed resource_id ('file:path', 'db:migrations', 'mcp_tool:name',
    'route:POST:/x', 'pypi:publish', 'github:tag') so non-file conflicts can be
    serialized the same way file edits already are. CREATE_TABLES covers fresh
    DBs; this is the upgrade path for existing ones.
    """
    await db.execute(
        """CREATE TABLE IF NOT EXISTS resource_locks (
            id TEXT PRIMARY KEY,
            resource_id TEXT NOT NULL UNIQUE,
            resource_type TEXT NOT NULL,
            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            claimed_at TEXT NOT NULL DEFAULT (datetime('now')),
            expires_at TEXT NOT NULL
        )"""
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_resource_locks_session ON resource_locks(session_id)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_resource_locks_expires ON resource_locks(expires_at)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_resource_locks_type ON resource_locks(resource_type)"
    )
    await db.commit()


async def _migrate_note_kind(db: aiosqlite.Connection) -> None:
    """9d44998b — add note_kind to project_notes (wiki | insight | reference).

    Nullable; the app treats NULL as 'wiki'. Existing rows are left untouched so
    the dashboard renders them as compact wiki notes by default.
    """
    await _migrate_add_column_if_missing(db, "project_notes", "note_kind", "TEXT")


async def _migrate_oauth_codes_table(db: aiosqlite.Connection) -> None:
    """vG9.40 â€” create oauth_codes table for PKCE authorization code persistence."""
    await db.execute(
        """CREATE TABLE IF NOT EXISTS oauth_codes (
            code TEXT PRIMARY KEY,
            tenant_id TEXT,
            redirect_uri TEXT NOT NULL,
            code_challenge TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )"""
    )
    await db.commit()


async def _migrate_device_codes_table(db: aiosqlite.Connection) -> None:
    """RFC 8628 device authorization flow â€” device_codes table."""
    await db.execute(
        """CREATE TABLE IF NOT EXISTS device_codes (
            device_code TEXT PRIMARY KEY,
            user_code TEXT NOT NULL UNIQUE,
            tenant_id TEXT,
            expires_at TEXT NOT NULL,
            approved INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )"""
    )
    await db.commit()


async def _migrate_device_codes_denied_polled(db: aiosqlite.Connection) -> None:
    """e9f18530 â€” harden the RFC 8628 device_codes table.

    Adds ``denied`` (explicit access_denied state, distinct from a deleted row)
    and ``last_polled_at`` (backs the slow_down poll-rate limiter). Idempotent
    ADD COLUMN. device_code / user_code now store SHA-256 hashes, not raw codes,
    but that is enforced by the app layer â€” no schema change is needed for it.
    """
    await _migrate_add_column_if_missing(
        db, "device_codes", "denied", "INTEGER NOT NULL DEFAULT 0"
    )
    await _migrate_add_column_if_missing(
        db, "device_codes", "last_polled_at", "TEXT"
    )


async def _migrate_sprint_items_indeterminate(db: aiosqlite.Connection) -> None:
    """Add 'indeterminate' status + claimed_at column to sprint_items.

    Widens the CHECK constraint (requires table rebuild in SQLite) and adds
    the claimed_at column. Idempotent: no-op when already migrated.
    """
    async with db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='sprint_items'"
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return  # fresh DB â€” CREATE_TABLES already has the new schema
    sql = (row["sql"] if isinstance(row, dict) else row[0]) or ""
    # Already migrated when both new values are present in the schema.
    if "'indeterminate'" in sql and "claimed_at" in sql:
        return
    # Ensure claimed_at exists before the rebuild references it.
    await _migrate_add_column_if_missing(db, "sprint_items", "claimed_at", "TEXT")
    # Rebuild with the new CHECK constraint.
    await db.executescript(
        """
        CREATE TABLE sprint_items_new (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id),
            version TEXT NOT NULL,
            title TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN
                    ('pending','todo','in_progress','done','failed','skipped','pushed','indeterminate')),
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
            milestone_type TEXT NOT NULL DEFAULT 'task'
        );
        INSERT INTO sprint_items_new
            SELECT id, project_id, version, title, status,
                   item_group, pushed_to, human_id,
                   added_at, completed_at, claimed_at,
                   task_id, notes, feedback_thumb, feedback_note,
                   COALESCE(milestone_type, 'task')
            FROM sprint_items;
        DROP TABLE sprint_items;
        ALTER TABLE sprint_items_new RENAME TO sprint_items;
        CREATE INDEX IF NOT EXISTS idx_sprint_items_project
            ON sprint_items(project_id, status);
        CREATE INDEX IF NOT EXISTS idx_sprint_items_version
            ON sprint_items(project_id, version);
        """
    )
    await db.commit()


async def _migrate_sprint_items_provisional_complete(db: aiosqlite.Connection) -> None:
    """Widen the sprint_items status CHECK to include 'provisional_complete'.

    SQLite-only: Postgres stores status as free TEXT (no CHECK), so the hosted
    tier needs no migration. Idempotent. Preserves ALL existing columns by
    rebuilding from the live CREATE statement with only the status CHECK tuple
    widened — robust against columns added by later migrations.
    """
    import re

    async with db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='sprint_items'"
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return  # fresh DB — CREATE_TABLES already lists provisional_complete
    sql = (row["sql"] if isinstance(row, dict) else row[0]) or ""
    if "'provisional_complete'" in sql:
        return  # already migrated
    if "status IN" not in sql:
        return  # no status CHECK to widen (unexpected for SQLite)

    new_sql = sql.replace(
        "'in_progress','done'", "'in_progress','provisional_complete','done'"
    )
    if new_sql == sql:  # spacing differed — fall back to inserting before 'done'
        new_sql = sql.replace("'done'", "'provisional_complete','done'", 1)
    if new_sql == sql:
        return  # could not locate the tuple — leave the schema untouched

    tmp_sql = re.sub(
        r'(CREATE TABLE\s+(?:IF NOT EXISTS\s+)?)["\']?sprint_items["\']?',
        r"\1sprint_items_pc_new",
        new_sql,
        count=1,
    )
    async with db.execute("SELECT * FROM sprint_items LIMIT 0") as cur:
        cols = [d[0] for d in cur.description]
    collist = ", ".join(cols)
    await db.executescript(
        tmp_sql + ";\n"
        f"INSERT INTO sprint_items_pc_new ({collist}) SELECT {collist} FROM sprint_items;\n"
        "DROP TABLE sprint_items;\n"
        "ALTER TABLE sprint_items_pc_new RENAME TO sprint_items;\n"
        "CREATE INDEX IF NOT EXISTS idx_sprint_items_project ON sprint_items(project_id, status);\n"
        "CREATE INDEX IF NOT EXISTS idx_sprint_items_version ON sprint_items(project_id, version);\n"
    )
    await db.commit()


async def _migrate_workspace_members_rbac(db: aiosqlite.Connection) -> None:
    """G5.19 / G5.20 â€” widen workspace_members.role to allow 'admin' and add
    github_access. Idempotent.

    Strategy:
     - Add github_access column if missing (default 'read').
     - Drop the legacy CHECK constraint on role by rebuilding the table
       if and only if it's present. Existing rows preserve their role
       and pick up github_access defaulted by current role.
    """
    # github_access â€” new column, simple ADD COLUMN.
    await _migrate_add_column_if_missing(
        db, "workspace_members", "github_access",
        "TEXT NOT NULL DEFAULT 'read'",
    )

    # Detect the legacy CHECK by inspecting the table SQL. If it has the
    # ('owner','member','viewer') tuple, rebuild without CHECK.
    async with db.execute(
        "SELECT sql FROM sqlite_master "
        "WHERE type='table' AND name='workspace_members'"
    ) as cur:
        row = await cur.fetchone()
    sql = (row["sql"] if row else "") or ""
    if "'owner','member','viewer'" not in sql:
        return  # already widened or never had the CHECK

    # Rebuild. We pull a stable snapshot, drop, recreate, restore.
    async with db.execute("SELECT * FROM workspace_members") as cur:
        existing = await cur.fetchall()
    columns = [d[0] for d in cur.description] if cur.description else []
    await db.execute("DROP TABLE workspace_members")
    await db.execute(
        "CREATE TABLE workspace_members ("
        "  id TEXT PRIMARY KEY,"
        "  tenant_id TEXT NOT NULL REFERENCES tenants(id),"
        "  email TEXT NOT NULL,"
        "  role TEXT NOT NULL DEFAULT 'member',"
        "  github_access TEXT NOT NULL DEFAULT 'read',"
        "  token_hash TEXT,"
        "  invited_at TEXT NOT NULL DEFAULT (datetime('now')),"
        "  joined_at TEXT"
        ")"
    )
    for r in existing:
        d = {k: r[k] for k in columns}
        await db.execute(
            "INSERT INTO workspace_members "
            "(id, tenant_id, email, role, github_access, token_hash, "
            " invited_at, joined_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                d.get("id"), d.get("tenant_id"), d.get("email"),
                d.get("role") or "member",
                d.get("github_access") or "read",
                d.get("token_hash"),
                d.get("invited_at"),
                d.get("joined_at"),
            ),
        )
    await db.commit()


async def _migrate_workspace_members_project_scope(db: aiosqlite.Connection) -> None:
    """d116642e — project-level invites foundation.

    Adds a nullable ``project_id`` to ``workspace_members``:
      - NULL  = workspace-wide member (current behavior, sees all projects)
      - set   = project-scoped member (listing-only scoping for now)

    Backward compatible: existing rows keep NULL and are unaffected. This is
    the safe foundation only — airtight per-request access enforcement is
    intentionally deferred pending the open product decision (pin b11c7cf6).
    """
    await _migrate_add_column_if_missing(
        db, "workspace_members", "project_id", "TEXT",
    )


async def _migrate_project_icon(db: aiosqlite.Connection) -> None:
    """G4.17 â€” single-emoji icon on projects for sidebar/tab rendering."""
    await _migrate_add_column_if_missing(db, "projects", "icon", "TEXT")


async def _migrate_project_parent_id(db: aiosqlite.Connection) -> None:
    """3b6ff466 — projects.parent_project_id: one-level-deep subprojects.

    Nullable self-reference to a parent project. A top-level project has
    parent_project_id = NULL; a subproject points at its (top-level) parent.
    The hierarchy is enforced ONE level deep at the app layer (create_project
    rejects a parent that itself has a parent). Isolation is free — everything
    is already keyed by project_id — so the only behavioural wiring is a
    north_star fall-back to the parent when a child has none of its own.

    The index lives INSIDE this guarded migration (never inline in the
    CREATE_TABLES literal), because an unguarded ``CREATE INDEX ... (parent_
    project_id)`` in the base schema would crash startup on an existing DB
    whose projects table predates the column — the same missing-column boot
    crash that took prod down on 2026-07-04. Idempotent. Mirrored in
    pg_adapter._migrate_pg_project_parent_id.
    """
    await _migrate_add_column_if_missing(db, "projects", "parent_project_id", "TEXT")
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_projects_parent "
        "ON projects(parent_project_id)"
    )
    await db.commit()


# G2.10 â€” Set of email addresses considered "internal" for lifecycle
# purposes. The migrator backfills these to is_internal=true after the
# column is added. Read from MERIDIAN_INTERNAL_EMAILS env var (comma-separated).
def _internal_emails() -> frozenset[str]:
    import os
    default_emails = (
        "ajc123private@gmail.com,"
        "dradamawsome@gmail.com,"
        "ajc123shopping@gmail.com,"
        "termh4@umsystem.edu"
    )
    emails_str = os.environ.get("MERIDIAN_INTERNAL_EMAILS", default_emails)
    return frozenset(e.strip().lower() for e in emails_str.split(",") if e.strip())


async def _migrate_tenants_is_internal(db: aiosqlite.Connection) -> None:
    """G2.10 â€” Add tenants.is_internal flag and backfill known internal
    emails. is_internal tenants are excluded from churn/dunning/overage/
    free-expiry warnings and deletion. Idempotent.
    """
    await _migrate_add_column_if_missing(
        db, "tenants", "is_internal", "INTEGER NOT NULL DEFAULT 0"
    )
    # Backfill known internal emails. Idempotent.
    for email in sorted(_internal_emails()):
        await db.execute(
            "UPDATE tenants SET is_internal = 1 WHERE LOWER(email) = ?",
            (email,),
        )
    await db.commit()


async def _migrate_admin_plan(db: aiosqlite.Connection) -> None:
    """Set plan='admin' for tenants whose email is in MERIDIAN_ADMIN_EMAILS / ADMIN_EMAIL.

    This makes the DB plan column authoritative for admin-DB routing in _deps.py.
    Idempotent — safe to run on every startup.
    """
    import os
    whitelist_raw = os.environ.get("MERIDIAN_ADMIN_EMAILS", os.environ.get("ADMIN_EMAIL", ""))
    if not whitelist_raw:
        return
    admin_emails = {e.strip().lower() for e in whitelist_raw.split(",") if e.strip()}
    for email in sorted(admin_emails):
        await db.execute(
            "UPDATE tenants SET plan = 'admin' WHERE LOWER(email) = ? AND plan != 'admin'",
            (email,),
        )
    await db.commit()


async def _migrate_workspace_tenant_isolation(db: aiosqlite.Connection) -> None:
    """Add tenant_id to workspace_notes/decisions/settings so the workspace
    layer is isolated per tenant inside a *shared* control-plane DB. 2026-06-13.

    On a dedicated per-tenant Neon DB the local ``tenants`` table is empty, so
    the backfill below is a no-op and pre-isolation rows stay ``tenant_id IS
    NULL``. The query layer treats a NULL tenant_id as "belongs to this DB" so
    those legacy rows remain visible to that DB's sole tenant. In the shared
    admin DB the admin tenant row exists, so legacy rows are claimed by admin
    and stop leaking across the internal accounts that share that DB.
    """
    await _migrate_add_column_if_missing(db, "workspace_notes", "tenant_id", "TEXT")
    await _migrate_add_column_if_missing(db, "workspace_decisions", "tenant_id", "TEXT")
    await _migrate_add_column_if_missing(db, "workspace_settings", "tenant_id", "TEXT")
    # Backfill legacy NULL rows to the admin tenant, but only where that tenant
    # actually exists in this DB (the shared control-plane DB). Elsewhere the
    # EXISTS guard skips the UPDATE entirely, leaving rows NULL.
    _admin_email = "ajc123private@gmail.com"
    for _table in ("workspace_notes", "workspace_decisions", "workspace_settings"):
        await db.execute(
            f"UPDATE {_table} SET tenant_id = ("
            "    SELECT id FROM tenants WHERE email = ? LIMIT 1"
            ") WHERE tenant_id IS NULL "
            "  AND EXISTS (SELECT 1 FROM tenants WHERE email = ?)",
            (_admin_email, _admin_email),
        )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_ws_notes_tenant "
        "ON workspace_notes(tenant_id)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_ws_decisions_tenant "
        "ON workspace_decisions(tenant_id)"
    )
    await db.commit()


async def _migrate_workspace_sprint_board(db: aiosqlite.Connection) -> None:
    """workspace_sprint_items — tenant-global personal backlog (cross-project).

    CREATE_TABLES covers fresh DBs; this is the upgrade path for existing ones.
    Idempotent: CREATE TABLE / INDEX IF NOT EXISTS. Tenant-scoped (tenant_id,
    NOT project_id), mirroring the workspace_notes / workspace_decisions layer.
    """
    await db.execute(
        "CREATE TABLE IF NOT EXISTS workspace_sprint_items ("
        "    id TEXT PRIMARY KEY,"
        "    tenant_id TEXT,"
        "    title TEXT NOT NULL,"
        "    status TEXT NOT NULL DEFAULT 'todo'"
        "        CHECK (status IN ('todo','pending','in_progress','done','skipped','failed')),"
        "    item_group TEXT,"
        "    human_id TEXT,"
        "    position INTEGER NOT NULL DEFAULT 0,"
        "    created_at TEXT NOT NULL DEFAULT (datetime('now')),"
        "    updated_at TEXT NOT NULL DEFAULT (datetime('now')),"
        "    completed_at TEXT"
        ")"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_workspace_sprint_items_tenant "
        "ON workspace_sprint_items(tenant_id, status)"
    )
    await db.commit()


async def _migrate_queued_session(db: aiosqlite.Connection) -> None:
    """projects.queued_session — the next /goal string to run back-to-back,
    appended to the handoff (then cleared) so multi-sprint days don't need a
    manual paste loop. Nullable. 2026-06-13."""
    await _migrate_add_column_if_missing(db, "projects", "queued_session", "TEXT")


async def _migrate_pending_goal(db: aiosqlite.Connection) -> None:
    """projects.pending_goal — the handoff /goal string persisted by
    generate_handoff so the next start_session can deliver it through a trusted
    MCP tool result (keyed on project_id) instead of a spoofable copy-pasted
    chat string. Read-once (cleared on read). Nullable. 5efe254b."""
    await _migrate_add_column_if_missing(db, "projects", "pending_goal", "TEXT")


async def _migrate_registered_hostnames(db: aiosqlite.Connection) -> None:
    """registered_hostnames maps a machine hostname -> tenant for token-based
    OAuth hooks, so hook scripts carry a per-machine registration_token instead
    of a long-lived Bearer API token. Lives in the control-plane (auth) DB.
    2026-06-13. Idempotent."""
    await db.execute(
        """CREATE TABLE IF NOT EXISTS registered_hostnames (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            hostname TEXT NOT NULL,
            registration_token TEXT NOT NULL,
            registered_at TEXT NOT NULL DEFAULT (datetime('now')),
            last_seen TEXT,
            UNIQUE(tenant_id, hostname)
        )"""
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_reg_hostnames "
        "ON registered_hostnames(hostname)"
    )
    await db.commit()


async def _migrate_active_worktrees(db: aiosqlite.Connection) -> None:
    """Add active_worktrees table for tracking live git worktrees per session."""
    await db.execute(
        """CREATE TABLE IF NOT EXISTS active_worktrees (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES sessions(id),
            project_id TEXT NOT NULL REFERENCES projects(id),
            item_id TEXT,
            branch TEXT NOT NULL,
            path TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            removed_at TEXT
        )"""
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_active_worktrees_session "
        "ON active_worktrees(session_id)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_active_worktrees_project "
        "ON active_worktrees(project_id, removed_at)"
    )
    await db.commit()


async def _migrate_changelog_entries(db: aiosqlite.Connection) -> None:
    """03744d18 — changelog_entries: user-facing release notes stored in DB.

    Replaces DEVLOG.md as the source for /changelog so the public page shows
    curated release notes instead of the raw internal dev log.
    """
    await db.execute(
        """CREATE TABLE IF NOT EXISTS changelog_entries (
            id TEXT PRIMARY KEY,
            version TEXT,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            published_at TEXT NOT NULL DEFAULT (datetime('now')),
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )"""
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_changelog_published "
        "ON changelog_entries(published_at DESC)"
    )
    await db.commit()


async def _migrate_parallel_safety(db: aiosqlite.Connection) -> None:
    """0716c9e0 — per-project parallel safety toggles.

    auto_worktrees=1 (default ON): claim_sprint_item suggests a git worktree.
    require_merge_approval=1 (default ON): complete_sprint_item warns when an
    active worktree exists for the session (merge reminder HITL).
    """
    await _migrate_add_column_if_missing(
        db, "projects", "auto_worktrees", "INTEGER NOT NULL DEFAULT 1"
    )
    await _migrate_add_column_if_missing(
        db, "projects", "require_merge_approval", "INTEGER NOT NULL DEFAULT 1"
    )


async def _migrate_agent_instructions(db: aiosqlite.Connection) -> None:
    """8a0c5a78 — projects.agent_instructions: per-project custom instructions
    injected into start_session so every AI session picks them up automatically.
    """
    await _migrate_add_column_if_missing(db, "projects", "agent_instructions", "TEXT")


async def _migrate_tunnel_active(db: aiosqlite.Connection) -> None:
    """b43b0c6a — tenants.tunnel_active: set to 1 when the tenant's local binary
    holds an open WebSocket on /tunnel/{tenant_id}. Reset to 0 on disconnect.
    Dashboard reads this to show a green/red tunnel status dot.
    """
    await _migrate_add_column_if_missing(
        db, "tenants", "tunnel_active", "INTEGER NOT NULL DEFAULT 0"
    )


async def _backfill_agent_instructions(db: aiosqlite.Connection) -> None:
    """Set DEFAULT_AGENT_INSTRUCTIONS on every project that has no custom rules.

    Idempotent: only touches rows where agent_instructions is empty (NULL or the
    empty string). Projects that already carry custom rules are never touched, so
    user edits are preserved. Called after _migrate_agent_instructions ensures
    the column exists.
    """
    from ..agent_defaults import DEFAULT_AGENT_INSTRUCTIONS  # avoid circular import
    await db.execute(
        "UPDATE projects SET agent_instructions = ? "
        "WHERE agent_instructions IS NULL OR agent_instructions = ''",
        (DEFAULT_AGENT_INSTRUCTIONS,),
    )
    await db.commit()


async def _migrate_code_intel(db: aiosqlite.Connection) -> None:
    """Sprint-2/3 — projects.code_intel_enabled: per-project Code Intelligence toggle.
    When 1, the dashboard shows the codebase-memory-mcp install command and
    permanent URL. The agent_instructions already include conditional guidance.
    """
    await _migrate_add_column_if_missing(
        db, "projects", "code_intel_enabled", "INTEGER NOT NULL DEFAULT 0"
    )


async def _migrate_tunnel_plugins(db: aiosqlite.Connection) -> None:
    """Tunnel plugin registry — tenants.tunnel_plugins: per-tenant JSON config
    overriding what `meridian --tunnel` spawns behind each of the three transport
    slots (fs/code/extract). NULL means "use the built-in defaults", so existing
    tenants keep the current filesystem/code-intel/code-extractor behaviour.
    Swapping a plugin's command (e.g. code-intel → codegraph) is a pure config
    change here — no client/server code change, no redeploy.
    """
    await _migrate_add_column_if_missing(db, "tenants", "tunnel_plugins", "TEXT")


async def _migrate_tunnel_plugins_by_host(db: aiosqlite.Connection) -> None:
    """8660d701 — tenants.tunnel_plugins_by_host: per-machine tunnel plugin config,
    JSON ``{hostname: <overrides>}``. Each machine running ``meridian --tunnel`` has
    different software installed, so config is keyed by (tenant_id, hostname). The
    legacy ``tenants.tunnel_plugins`` stays the default for any host without a
    per-host entry. NULL → no per-host overrides (every host uses the default).
    Idempotent."""
    await _migrate_add_column_if_missing(db, "tenants", "tunnel_plugins_by_host", "TEXT")


async def _migrate_notes_priority(db: aiosqlite.Connection) -> None:
    """Sprint-4 — project_notes.priority: high/normal/low ranking for generate_handoff
    and get_session_brief planner role. High-priority notes are surfaced first.
    """
    await _migrate_add_column_if_missing(
        db, "project_notes", "priority", "TEXT NOT NULL DEFAULT 'normal'"
    )


async def _migrate_task_log_kind(db: aiosqlite.Connection) -> None:
    """Sprint-4 — task_log.kind: shipped/found/decided/blocked taxonomy so log
    entries are differentiated beyond status. Defaults to 'shipped'.
    """
    await _migrate_add_column_if_missing(
        db, "task_log", "kind", "TEXT DEFAULT 'shipped'"
    )


async def _migrate_note_slug(db: aiosqlite.Connection) -> None:
    """5a5bba43 — project_notes.slug: Obsidian ``mem:name`` style stable handle.

    Adds a nullable ``slug`` column and backfills every existing row with a
    kebab-cased slug derived from its title, unique per project (collisions get
    a ``-2``/``-3``/… suffix). Idempotent: the column add is guarded, and the
    backfill only touches rows whose slug is still NULL/empty, so re-running on
    an already-migrated DB is a no-op.
    """
    await _migrate_add_column_if_missing(db, "project_notes", "slug", "TEXT")
    # Backfill: assign slugs to any pre-existing rows that lack one. Oldest
    # first so the unsuffixed slug goes to the earliest note on a title clash.
    async with db.execute(
        "SELECT id, project_id, title FROM project_notes "
        "WHERE slug IS NULL OR slug = '' ORDER BY created_at ASC, id ASC"
    ) as cur:
        rows = list(await cur.fetchall())
    if not rows:
        return
    # Seed used-slug sets per project from rows that already have one so the
    # backfill never collides with an existing slug.
    used: dict[str, set[str]] = {}
    async with db.execute(
        "SELECT project_id, slug FROM project_notes "
        "WHERE slug IS NOT NULL AND slug != ''"
    ) as cur:
        for r in await cur.fetchall():
            pid = r["project_id"] if isinstance(r, dict) else r[0]
            existing_slug = r["slug"] if isinstance(r, dict) else r[1]
            used.setdefault(pid, set()).add(existing_slug)
    for r in rows:
        nid = r["id"] if isinstance(r, dict) else r[0]
        pid = r["project_id"] if isinstance(r, dict) else r[1]
        title = r["title"] if isinstance(r, dict) else r[2]
        seen = used.setdefault(pid, set())
        base = _slugify_note(title)
        slug = base
        n = 1
        while slug in seen:
            n += 1
            slug = f"{base}-{n}"
        seen.add(slug)
        await db.execute(
            "UPDATE project_notes SET slug = ? WHERE id = ?", (slug, nid)
        )
    await db.commit()


async def _migrate_decision_priority_edit_log(db: aiosqlite.Connection) -> None:
    """366317e9 — decisions_pinned.priority + decisions_pinned.edit_log.

    priority (urgent | normal | low, default 'normal') drives dashboard ordering
    and context-injection weight so the most important decisions surface first.
    edit_log is an append-only JSON array; every in-place body edit pushes a
    ``{"body": <previous body>, "ts": <iso timestamp>}`` entry BEFORE the row is
    overwritten, so the full edit history is preserved. Both nullable/defaulted,
    so the ADD COLUMN is safe on existing rows (priority defaults to 'normal',
    edit_log stays NULL). Idempotent: column adds are guarded.
    """
    await _migrate_add_column_if_missing(
        db, "decisions_pinned", "priority", "TEXT NOT NULL DEFAULT 'normal'"
    )
    await _migrate_add_column_if_missing(db, "decisions_pinned", "edit_log", "TEXT")


async def _migrate_oauth_refresh_tokens(db: aiosqlite.Connection) -> None:
    """Sprint-5 — oauth_refresh_tokens: RFC 6749 refresh_token support with rotation.

    token_hash: sha256 of the opaque refresh token, PRIMARY KEY.
    tenant_id:  owning tenant (nullable for anonymous/open sessions).
    client_id:  OAuth client that issued the token.
    expires_at: ISO-8601 UTC; tokens expire after 90 days by default.
    used_at:    set on rotation so replayed old tokens are rejected.
    """
    await db.execute(
        """CREATE TABLE IF NOT EXISTS oauth_refresh_tokens (
            token_hash TEXT PRIMARY KEY,
            tenant_id TEXT,
            client_id TEXT,
            expires_at TEXT NOT NULL,
            used_at TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )"""
    )
    await db.commit()


async def _migrate_code_anchored_notes(db: aiosqlite.Connection) -> None:
    """771c00d7 — project_notes.file_path + project_notes.symbol: code-anchored notes.

    A note with note_kind='code' plus a ``file_path`` (and optional ``symbol``)
    anchors a warning/context to a specific file/symbol in the codebase. These are
    surfaced automatically when an executor calls ``claim_file``/``get_file_claims``
    for that path, so it sees relevant context before touching the file. Both
    columns are nullable — normal notes leave them NULL and are unaffected.
    Idempotent: the column adds are guarded.
    """
    await _migrate_add_column_if_missing(db, "project_notes", "file_path", "TEXT")
    await _migrate_add_column_if_missing(db, "project_notes", "symbol", "TEXT")


async def _migrate_note_source(db: aiosqlite.Connection) -> None:
    """e3f150d0 — project_notes.source: provenance for an ingested note.

    A document-ingested note (``note_kind='document'``) records the URL or file
    path it was extracted from in ``source`` so the dashboard can show "ingested
    from <path>" and link back. Nullable — normal notes leave it NULL and are
    unaffected. Idempotent: the column add is guarded.
    """
    await _migrate_add_column_if_missing(db, "project_notes", "source", "TEXT")


async def _migrate_session_sprint_version(db: aiosqlite.Connection) -> None:
    """a76cb7c0 — sessions.sprint_version: the sprint-version bucket a session
    is scoped to.

    start_session may receive an explicit ``version`` (e.g. "v0.1.x"), or infer
    the bucket with the most pending items. The chosen version is stored here so
    later calls (the orientation response, the /goal template) can auto-filter
    sprint progress/items to it instead of drowning an executor in the whole
    backlog. Nullable — sessions with no scope (NULL) behave exactly as before
    (all versions). Idempotent: the column add is guarded.
    """
    await _migrate_add_column_if_missing(db, "sessions", "sprint_version", "TEXT")


async def _migrate_agent_tasks_table(db: aiosqlite.Connection) -> None:
    """99e71b9e — agent_tasks: Google A2A protocol task storage.

    Creates the agent_tasks table used by the A2A endpoint
    (POST /a2a/{agent_id}/tasks/send).  Each row represents one task received
    via the A2A protocol.  The table is idempotent (CREATE TABLE IF NOT EXISTS).
    """
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_tasks (
            id TEXT PRIMARY KEY,
            agent_id TEXT NOT NULL,
            session_id TEXT,
            status TEXT NOT NULL DEFAULT 'submitted'
                CHECK (status IN ('submitted','working','completed','failed','canceled')),
            input TEXT NOT NULL,
            output TEXT,
            metadata TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )


async def _migrate_handoffs_table(db: aiosqlite.Connection) -> None:
    """8819d6b1 — handoffs: per-session handoff history.

    Records each generated executor handoff (mode full/delta) as its own row
    instead of overwriting a single field on the session row, so the dashboard
    and planner can list historical handoffs, diff between sessions, and surface
    a "new handoff since you last checked" signal (ab514e43). Idempotent
    (CREATE TABLE / INDEX IF NOT EXISTS). Mirrored in
    pg_adapter._migrate_pg_handoffs_table.
    """
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS handoffs (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            session_id TEXT,
            mode TEXT NOT NULL DEFAULT 'full',
            body TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_handoffs_project "
        "ON handoffs(project_id, created_at)"
    )


async def _migrate_session_note_kind(db: aiosqlite.Connection) -> None:
    """0d7de2a2 — session_notes.note_kind: thinking_sync scratchpad notes.

    Nullable ``note_kind`` ('thinking' | NULL/'note'). 'thinking' marks a
    HOOKS_DEBUG_STATE note auto-persisted by the client-side thinking_sync hook
    so the dashboard can render it distinctly. Idempotent. Existing rows keep
    NULL (treated as a normal note).
    """
    await _migrate_add_column_if_missing(db, "session_notes", "note_kind", "TEXT")


async def _migrate_sprint_item_owner(db: aiosqlite.Connection) -> None:
    """4f02340e — sprint_items.owner: mixed-ownership task chains.

    Adds a nullable ``owner`` column ('human' | 'ai' | NULL) used by the
    alternating claim/handoff state machine (_advance_task_chain). NULL on
    parents and legacy items, so existing data is unaffected. Idempotent.
    """
    await _migrate_add_column_if_missing(db, "sprint_items", "owner", "TEXT")
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_tasks_agent "
        "ON agent_tasks(agent_id, status)"
    )
    await db.commit()


async def _migrate_sprint_item_quality_gates(db: aiosqlite.Connection) -> None:
    """5823db0b — quality gates + actor attribution on sprint_items.

    Two additive, nullable columns (existing rows unaffected, idempotent):
    - ``required_notes`` (INTEGER DEFAULT 0): when set, complete_sprint_item is
      blocked until the item has evidence (existing notes, a linked task, or a
      ``notes`` argument on the completing call).
    - ``actor`` (TEXT): the executor id/name that last claimed or completed the
      item, so a parallel board records *who* did each piece of work.

    Borrowed from task-orchestrator (jpicklyk): a note gate the server enforces,
    plus actor attribution on every state transition.
    """
    await _migrate_add_column_if_missing(
        db, "sprint_items", "required_notes", "INTEGER DEFAULT 0"
    )
    await _migrate_add_column_if_missing(db, "sprint_items", "actor", "TEXT")
    await db.commit()


async def _migrate_parallel_primitives(db: aiosqlite.Connection) -> None:
    """Wave-4 parallel-coordination primitives (ffa03655, c35370cc, d3a3a01d).

    Three additive tables (CREATE ... IF NOT EXISTS — idempotent):
    - session_findings — materialized per-task intermediate results that survive
      session boundaries (parallel readers write, orchestrator/writers read).
    - session_messages — actor-model messages between sessions (send/receive).
    - file_read_claims — shared read claims so many reader sessions coexist on a
      file without the exclusive file_locks contention (writes stay exclusive).
    Mirrored in pg_adapter._migrate_pg_parallel_primitives.
    """
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS session_findings (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            session_id TEXT,
            key TEXT,
            title TEXT,
            content TEXT NOT NULL,
            task_id TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_findings_project "
        "ON session_findings(project_id, key)"
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS session_messages (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            from_session_id TEXT,
            to_session_id TEXT NOT NULL,
            kind TEXT,
            payload TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            read_at TEXT
        )
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_to "
        "ON session_messages(to_session_id, read_at)"
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS file_read_claims (
            id TEXT PRIMARY KEY,
            file_path TEXT NOT NULL,
            session_id TEXT NOT NULL,
            claimed_at TEXT NOT NULL DEFAULT (datetime('now')),
            expires_at TEXT NOT NULL,
            UNIQUE(file_path, session_id)
        )
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_read_claims_file ON file_read_claims(file_path)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_read_claims_expires ON file_read_claims(expires_at)"
    )
    await db.commit()


async def _migrate_project_execution_mode(db: aiosqlite.Connection) -> None:
    """ecf69de8 — projects.execution_mode: per-project executor posture.

    'autonomous' (default) — a session claims and runs pending sprint items
    immediately without asking for direction. 'interactive' — the session
    reviews the pending items and asks the human which to start before
    executing. The value is injected at the protocol level by start_session
    (a leading EXECUTION MODE directive line) and selects the /goal framing in
    ``_build_quick_start_goal``. SQLite ``ALTER TABLE ADD COLUMN`` can't carry a
    CHECK constraint, so the column is added with a plain default and the Python
    layer (``set_project_execution_mode`` / ``create_project``) validates the
    value. Existing rows default to 'autonomous'. Idempotent: the add is guarded.
    """
    await _migrate_add_column_if_missing(
        db, "projects", "execution_mode", "TEXT NOT NULL DEFAULT 'autonomous'"
    )


async def _migrate_decision_code_anchor(db: aiosqlite.Connection) -> None:
    """777f26b0 — decisions_pinned.code_anchor: optional file path anchor.

    When set, ``get_decisions_for_file`` surfaces this decision automatically
    when an executor calls ``claim_file`` for the matching path, so architectural
    decisions relevant to a file are injected into the executor's context before
    it edits. Nullable — existing decisions are unaffected. Idempotent.
    """
    await _migrate_add_column_if_missing(db, "decisions_pinned", "code_anchor", "TEXT")


async def _migrate_decision_assumption(db: aiosqlite.Connection) -> None:
    """2b39549d — decisions_pinned.assumption + assumption_status.

    A decision can record the unverified assumption it rests on (``assumption``,
    free text) and the validation state of that assumption
    (``assumption_status``: unvalidated | confirmed | invalidated). The planner
    surfaces decisions sitting on unvalidated assumptions; the validate_assumption
    tool (8ec5493b) stamps the status and fires a blocking HITL on invalidation.
    Both columns are nullable so existing decisions are unaffected. Idempotent.
    Mirrored in pg_adapter._migrate_pg_decision_assumption.
    """
    await _migrate_add_column_if_missing(db, "decisions_pinned", "assumption", "TEXT")
    await _migrate_add_column_if_missing(
        db, "decisions_pinned", "assumption_status", "TEXT"
    )


async def _migrate_insights_table(db: aiosqlite.Connection) -> None:
    """0b711a9d — strategic insights table (durable understanding, distinct from
    decisions/notes). horizon validated in Python (no DB CHECK so the vocabulary
    can grow without a table rebuild). Idempotent; mirrored in
    pg_adapter._migrate_pg_insights_table + both CREATE_TABLES for fresh DBs."""
    await db.executescript(
        "CREATE TABLE IF NOT EXISTS insights ("
        "    id TEXT PRIMARY KEY,"
        "    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,"
        "    title TEXT NOT NULL,"
        "    body TEXT NOT NULL,"
        "    horizon TEXT NOT NULL DEFAULT 'quarter',"
        "    tags TEXT,"
        "    status TEXT NOT NULL DEFAULT 'active',"
        "    created_at TEXT NOT NULL DEFAULT (datetime('now')),"
        "    updated_at TEXT NOT NULL DEFAULT (datetime('now'))"
        ");"
        "CREATE INDEX IF NOT EXISTS idx_insights_project ON insights(project_id, horizon);"
    )


async def _migrate_sprint_item_slug(db: aiosqlite.Connection) -> None:
    """b944c905 — sprint_items.slug: a human-readable per-project id derived from
    the title (UUID stays the primary key). Idempotent; mirrored in
    pg_adapter._migrate_pg_sprint_item_slug."""
    await _migrate_add_column_if_missing(db, "sprint_items", "slug", "TEXT")


async def _migrate_sprint_item_nickname(db: aiosqlite.Connection) -> None:
    """b6b0cee6 — sprint_items.nickname: a short (1-2 word) memorable per-project
    handle, distinct from the long title slug. Idempotent; mirrored in
    pg_adapter._migrate_pg_sprint_item_nickname."""
    await _migrate_add_column_if_missing(db, "sprint_items", "nickname", "TEXT")


async def _migrate_capture_insight_notes_to_insights(db: aiosqlite.Connection) -> None:
    """b5ed8a61 — retire the legacy ``capture_insight`` tool: MOVE every
    ``project_notes`` row with ``note_kind = 'insight'`` into the dedicated
    ``insights`` table (shipped by 0b711a9d).

    Per row: INSERT into ``insights`` (reusing the note's id, horizon='quarter',
    status='active') THEN DELETE the note — insert-before-delete so a crash
    mid-row can never lose data. Reusing the note id makes this a pure MOVE and
    idempotent: after it runs there are no ``note_kind = 'insight'`` rows left,
    so a re-run selects nothing and is a no-op (and the reused id would collide
    on the PRIMARY KEY were it somehow re-attempted). Mirrored in
    pg_adapter._migrate_pg_capture_insight_notes_to_insights.
    """
    # bb16f9a7 — set-based (was a per-row loop) so a large project_notes table
    # can't slow-loop and delay init_db past the deploy health-check window.
    # Still a pure idempotent MOVE: reuse the note id, insert-before-delete, and
    # a re-run selects nothing (no insight-kind rows left).
    await db.execute(
        "INSERT INTO insights (id, project_id, title, body, horizon, tags, status) "
        "SELECT id, project_id, title, COALESCE(body, ''), 'quarter', tags, 'active' "
        "FROM project_notes WHERE note_kind = 'insight'"
    )
    await db.execute("DELETE FROM project_notes WHERE note_kind = 'insight'")
    await db.commit()


async def _migrate_blog_posts_tenant(db: aiosqlite.Connection) -> None:
    """8843250f — workspace-scope the blog: add a nullable ``tenant_id`` to
    ``blog_posts`` so posts belong to a workspace (like workspace_notes), and
    an index on it. Idempotent. Mirrored in
    pg_adapter._migrate_pg_blog_posts_tenant.

    (The 'archived' lifecycle status is enforced at the app layer — SQLite
    ADD COLUMN can't rewrite the CREATE-time CHECK, and fresh DBs get the
    widened CHECK from the CREATE literal.)
    """
    await _migrate_add_column_if_missing(db, "blog_posts", "tenant_id", "TEXT")
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_blog_posts_tenant ON blog_posts(tenant_id)"
    )
    await db.commit()


async def _migrate_github_connections(db: aiosqlite.Connection) -> None:
    """0b061f45 — multi-account GitHub OAuth.

    ``github_connections`` stores N encrypted PATs per tenant keyed by
    account_login (personal + org accounts). ``projects.github_account_login``
    pins a specific account to a project; the token resolver falls back to
    the first connected account, then to the legacy ``tenants.github_pat``.
    Idempotent. Mirrored in pg_adapter._migrate_pg_github_connections.
    """
    await db.executescript(
        "CREATE TABLE IF NOT EXISTS github_connections ("
        "    id TEXT PRIMARY KEY,"
        "    tenant_id TEXT NOT NULL,"
        "    account_login TEXT NOT NULL,"
        "    token TEXT NOT NULL,"
        "    scope TEXT,"
        "    connected_at TEXT NOT NULL DEFAULT (datetime('now')),"
        "    UNIQUE(tenant_id, account_login)"
        ");"
        "CREATE INDEX IF NOT EXISTS idx_github_connections_tenant"
        "    ON github_connections(tenant_id);"
    )
    await _migrate_add_column_if_missing(db, "projects", "github_account_login", "TEXT")


async def _migrate_session_graph_snapshots(db: aiosqlite.Connection) -> None:
    """f773a99a — session_graph_snapshots: per-session code-graph metric snapshots.

    Stores lightweight proxy metrics (node/edge/hotspot/churn counts) computed
    from file_symbol_claims and task_log at checkpoint time. Used by
    ``get_graph_diff`` to compare two sessions' graph impact without a live
    code-graph traversal. Idempotent: CREATE TABLE IF NOT EXISTS.
    """
    await db.execute(
        """CREATE TABLE IF NOT EXISTS session_graph_snapshots (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            snapshot_at TEXT NOT NULL DEFAULT (datetime('now')),
            node_count INTEGER NOT NULL DEFAULT 0,
            edge_count INTEGER NOT NULL DEFAULT 0,
            hotspot_count INTEGER NOT NULL DEFAULT 0,
            file_churn INTEGER NOT NULL DEFAULT 0,
            metrics_json TEXT
        )"""
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_session_graph_snapshots_session "
        "ON session_graph_snapshots(session_id)"
    )
    await db.commit()


async def _migrate_session_goal_compliance(db: aiosqlite.Connection) -> None:
    """5abf3e12 — sessions.goal_compliance: a stored per-session goal-compliance
    metric.

    JSON blob recording whether a session's /goal item list was fully completed:
    ``listed`` (N = sprint items this session claimed/was attributed as ``actor``)
    vs ``completed`` (M = of those, how many reached status 'done'), plus a
    derived ``fully_completed`` flag. Computed at generate_handoff (session end)
    by :func:`meridian.db.compute_session_goal_compliance` and read back for the
    dashboard / analytics. Nullable — sessions that never generated a handoff
    simply have no metric. Idempotent (ADD COLUMN IF NOT EXISTS). No index: the
    column is only ever read by the session's own primary key. Mirrored in
    pg_adapter._migrate_pg_session_goal_compliance.
    """
    await _migrate_add_column_if_missing(
        db, "sessions", "goal_compliance", "TEXT"
    )


async def _migrate_sprint_item_pointers(db: aiosqlite.Connection) -> None:
    """2976e168 — sprint_item_pointers: the GENERIC POINTER PRIMITIVE.

    ONE table for pointers of ANY source_type (code/docs/citation/…), keyed to a
    sprint item. ``targets`` is a JSON array of {uri, selector, subSelector?} —
    the composite shape (LSP Location + W3C Web Annotation Selector composition)
    stored as JSON, NOT per-domain columns (the core design requirement). One
    resolver dispatches by selector.type (range|symbol|node_id|zotero_key).

    CREATE_TABLES covers fresh DBs; this is the upgrade path for existing ones.
    The index lives INSIDE this guarded migration (CREATE INDEX IF NOT EXISTS),
    never inline in the CREATE_TABLES literal — an unguarded inline index on a
    migration-added table would crash startup on a DB predating it (the
    2026-07-04 outage trap). Idempotent. Mirrored in
    pg_adapter._migrate_pg_sprint_item_pointers.
    """
    await db.execute(
        """CREATE TABLE IF NOT EXISTS sprint_item_pointers (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            sprint_item_id TEXT NOT NULL,
            source_type TEXT NOT NULL,
            targets TEXT NOT NULL,
            label TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )"""
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_sprint_item_pointers_item "
        "ON sprint_item_pointers(sprint_item_id)"
    )
    await db.commit()


async def _migrate_sprint_item_deferral(db: aiosqlite.Connection) -> None:
    """dec69708 — ENFORCED deferral for sprint items.

    Adds two nullable columns to ``sprint_items``:
      * ``deferred_until`` — an ISO timestamp. While it is in the future,
        ``claim_sprint_item`` REFUSES the item (a structural block, not merely a
        pinned "we decided to defer the paper-track" note that nothing enforces).
      * ``track`` — a named lane (e.g. ``'paper'``) so a whole track can be
        skipped by executors.

    Both nullable → plain ``ALTER TABLE ADD COLUMN`` is safe on existing DBs.
    CREATE_TABLES adds them on fresh DBs; this is the upgrade path. Idempotent
    (``_migrate_add_column_if_missing`` is a no-op when the column exists).
    Mirrored in pg_adapter._migrate_pg_sprint_item_deferral.
    """
    await _migrate_add_column_if_missing(db, "sprint_items", "deferred_until", "TEXT")
    await _migrate_add_column_if_missing(db, "sprint_items", "track", "TEXT")


async def _migrate_sprint_item_priority_blocker(db: aiosqlite.Connection) -> None:
    """e08fee30 + 2282a636 — priority + blocker_kind on sprint_items.

    Adds two columns to ``sprint_items``:
      * ``priority`` (e08fee30) — app-layer enum {urgent, high, normal, low},
        NOT NULL DEFAULT 'normal'. Higher-priority PENDING items are surfaced
        (and therefore claimed / grouped) first: get_sprint_items and
        get_parallelizable_groups order urgent-first within their existing
        ordering. (The enum is enforced at the app layer — SQLite ADD COLUMN
        can't carry a CHECK; fresh DBs get the plain DEFAULT from CREATE_TABLES.)
        NB: this is PART 1 only. A true RUNNING-session interrupt/preemption
        mechanism (PART 2 of e08fee30) is intentionally deferred and designed
        separately — this column is the ordering primitive it will build on.
      * ``blocker_kind`` (2282a636) — nullable. NULL = an ordinary item;
        'manual' = blocked on a real-world action OUTSIDE Meridian (publish
        something, obtain an API key, talk to an advisor). DISTINCT from
        milestone_type='human' (which is about WHO executes): a manual-blocker
        item is surfaced distinctly and, like milestone_type='human', is excluded
        from executor "just claim the next pending" scoping so an executor never
        treats a real-world blocker as claimable work.

    ``priority`` has a NOT NULL DEFAULT so existing rows backfill to 'normal';
    ``blocker_kind`` is nullable. Both are plain ``ALTER TABLE ADD COLUMN`` —
    safe on existing DBs, no inline index (guarded-migration rule). CREATE_TABLES
    adds them on fresh DBs; this is the upgrade path. Idempotent
    (``_migrate_add_column_if_missing`` is a no-op when the column exists).
    Mirrored in pg_adapter._migrate_pg_sprint_item_priority_blocker.
    """
    await _migrate_add_column_if_missing(
        db, "sprint_items", "priority", "TEXT NOT NULL DEFAULT 'normal'"
    )
    await _migrate_add_column_if_missing(db, "sprint_items", "blocker_kind", "TEXT")


async def _migrate_sprint_item_wave(db: aiosqlite.Connection) -> None:
    """58a45b92 — stored, deterministic wave label on sprint_items.

    ``wave`` — nullable TEXT (e.g. 'wave-1'). Turns recompute-every-time parallel
    grouping (get_parallelizable_groups) into an inspectable, editable STORED field:
    ``assign_sprint_waves`` auto-fills it from the conflict-free groups, and
    ``update_sprint_item(wave=...)`` edits it by hand. NULL = unassigned.

    Nullable plain ``ALTER TABLE ADD COLUMN`` — safe on existing DBs, no inline
    index (guarded-migration rule). CREATE_TABLES adds it on fresh DBs; this is the
    upgrade path. Idempotent (``_migrate_add_column_if_missing`` no-ops when present).
    Mirrored in pg_adapter._migrate_pg_sprint_item_wave.
    """
    await _migrate_add_column_if_missing(db, "sprint_items", "wave", "TEXT")


async def _migrate_mcp_rate_counters(db: aiosqlite.Connection) -> None:
    """3295c784 — mcp_rate_counters: cross-instance shared hit-counting for the
    consolidated /mcp tenant-tier rate limiter.

    The per-process ``_tenant_rl_hits`` dict counts requests on a single Fly
    machine only, so across N machines a tenant's effective limit is ~Nx the
    intended budget. This windowed counter keeps one atomic count per
    (tenant_id, epoch-minute window) so every instance agrees on a single shared
    total. Gated behind ``MERIDIAN_SHARED_RATE_LIMIT`` (default OFF); prod
    behavior is unchanged until opted in.

    CREATE_TABLES covers fresh DBs; this is the upgrade path for existing ones.
    The composite PRIMARY KEY (tenant_id, window_start) already indexes lookups;
    an extra index on ``window_start`` alone speeds the opportunistic prune of
    stale windows. That index lives HERE (guarded), never inline in the base
    CREATE_TABLES literal (2026-07-04 inline-index outage rule). Mirrored in
    pg_adapter._migrate_pg_mcp_rate_counters.
    """
    await db.execute(
        """CREATE TABLE IF NOT EXISTS mcp_rate_counters (
            tenant_id TEXT NOT NULL,
            window_start INTEGER NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (tenant_id, window_start)
        )"""
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_mcp_rate_counters_window "
        "ON mcp_rate_counters(window_start)"
    )
    await db.commit()


async def _migrate_workspace_proposals(db: aiosqlite.Connection) -> None:
    """5c4dcc0f — workspace_proposals: human-only "drawer of inspiration".

    A workspace-scoped (tenant_id, not project_id) table for cross-project
    flashes of insight. Distinct from workspace_notes (which has no lifecycle)
    and sprint_items (executor-claimable). The real-world "IDEA: ..." /
    "FUTURE IDEA: ..." notes that existed informally in workspace_notes now
    have a proper home with an enforced status machine.

    status:  raw → investigating → promoted | rejected
    promoted_to_sprint_item_id: set on promotion, links to the sprint item
        the proposal became. NULL for raw/investigating/rejected.

    NOT executor-auto-claimable. Human-reviewed promotion gate only.

    CREATE_TABLES covers fresh DBs; this is the upgrade path for existing
    ones. idx_workspace_proposals_tenant lives here (guarded migration), never
    inline in the base schema literal (2026-07-04 inline-index outage rule).
    Mirrors pg_adapter._migrate_pg_workspace_proposals.
    """
    await db.execute(
        """CREATE TABLE IF NOT EXISTS workspace_proposals (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            tags TEXT,
            status TEXT NOT NULL DEFAULT 'raw',
            promoted_to_sprint_item_id TEXT,
            tenant_id TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )"""
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_workspace_proposals_tenant "
        "ON workspace_proposals(tenant_id)"
    )
    await db.commit()


async def _migrate_pending_goal_at(db: aiosqlite.Connection) -> None:
    """590dcdd5 — projects.pending_goal_at: ISO-8601 UTC timestamp written by
    set_pending_goal alongside pending_goal so pop_pending_goal_with_meta can
    surface the age and flag goals older than PENDING_GOAL_STALE_HOURS as
    possibly-stale (written by a prior session whose human has since moved on).
    Nullable; NULL on rows that predate this migration."""
    await _migrate_add_column_if_missing(db, "projects", "pending_goal_at", "TEXT")


async def _migrate_file_patch_counters(db: aiosqlite.Connection) -> None:
    """356d6ac8 — file_patch_counters: structural-degradation early-warning signal.

    Tracks per-(session, file) patch cycles so get_structural_degradation_warnings
    can flag files that have been write-claimed N times within a session without a
    deliberate refactor (refactor_flagged). Motivated by the documented AI-agent
    pattern of patching symptoms locally, violating earlier architecture, then
    "cleaning up" and regressing previously-solved behavior.

    The table is created by CREATE_TABLES on fresh DBs; this is the upgrade path for
    existing ones. The UNIQUE (session_id, file_path) constraint enables idempotent
    upserts (INSERT OR IGNORE + UPDATE). The per-session index lives HERE (guarded
    migration), never inline in the base CREATE_TABLES literal (2026-07-04
    inline-index outage rule). Mirrored in pg_adapter._migrate_pg_file_patch_counters.
    """
    await db.execute(
        """CREATE TABLE IF NOT EXISTS file_patch_counters (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            file_path TEXT NOT NULL,
            patch_count INTEGER NOT NULL DEFAULT 0,
            refactor_flagged INTEGER NOT NULL DEFAULT 0,
            first_patched_at TEXT NOT NULL DEFAULT (datetime('now')),
            last_patched_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (session_id, file_path)
        )"""
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_file_patch_counters_session "
        "ON file_patch_counters(session_id)"
    )
    await db.commit()


async def _migrate_session_activity(db: aiosqlite.Connection) -> None:
    """8c147109 — session_activity: lightweight ring-buffer heartbeat feed.

    Creates the session_activity table so a remote planner can see signs of
    life in an executor session even before the executor calls log_task().
    Mirrors pg_adapter._migrate_pg_session_activity. Idempotent via
    CREATE TABLE IF NOT EXISTS + CREATE INDEX IF NOT EXISTS.
    """
    await db.execute(
        """CREATE TABLE IF NOT EXISTS session_activity (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            tool_name TEXT NOT NULL,
            summary TEXT NOT NULL,
            recorded_at TEXT NOT NULL DEFAULT (datetime('now'))
        )"""
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_session_activity_session "
        "ON session_activity(session_id, recorded_at DESC)"
    )
    await db.commit()


async def _migrate_sprint_item_resources_amended(db: aiosqlite.Connection) -> None:
    """2593a5fe — resources_amended flag on sprint_items.

    When claim_file/claim_symbol detects that an executor is touching a resource
    NOT in the item's original touches_resources declaration (a mid-execution
    pivot), it appends the new resource to touches_resources and sets
    resources_amended=1. This signals that the item's resource footprint grew
    after its wave label was computed, so wave-planning logic (or a human) can
    decide whether to re-run assign_sprint_waves.

    Nullable INTEGER (0/1/NULL). NULL and 0 both mean "not amended". 1 means
    at least one post-declaration resource was appended by claim_file/claim_symbol.

    Idempotent (_migrate_add_column_if_missing no-ops when already present).
    Mirrored in pg_adapter._migrate_pg_sprint_item_resources_amended.
    """
    await _migrate_add_column_if_missing(
        db, "sprint_items", "resources_amended", "INTEGER DEFAULT 0"
    )


async def _migrate_connection_events(db: aiosqlite.Connection) -> None:
    """b12cc29f — connection_events: per-/mcp-request auth+method event log.

    Every real HTTP /mcp request that Meridian's server receives is recorded
    here with enough context to diagnose client-side outages (zero tools, broken
    auth, unexpected UA) without needing raw Fly.io log access or guessing by
    elimination. A capped ring-buffer (last 1000 rows per tenant_id) prevents
    unbounded growth.

    Fields:
      tenant_id        — hashed or resolved tenant id (NULL for OAuth flow)
      method           — JSON-RPC method (initialize / tools/list / tools/call / ...)
      auth_result      — success / no_token / invalid_token / expired / oauth
      tools_returned   — count of tools in tools/list response (NULL otherwise)
      client_user_agent — first 200 chars of User-Agent header (NULL if absent)
      response_status  — HTTP status code returned (200 / 400 / 401 / 503 / ...)
      recorded_at      — wall-clock UTC timestamp

    Idempotent via CREATE TABLE IF NOT EXISTS + CREATE INDEX IF NOT EXISTS.
    Mirrored in pg_adapter._migrate_pg_connection_events.
    """
    await db.execute(
        """CREATE TABLE IF NOT EXISTS connection_events (
            id TEXT PRIMARY KEY,
            tenant_id TEXT,
            method TEXT NOT NULL DEFAULT '',
            auth_result TEXT NOT NULL DEFAULT 'unknown',
            tools_returned INTEGER,
            client_user_agent TEXT,
            response_status INTEGER NOT NULL DEFAULT 200,
            recorded_at TEXT NOT NULL DEFAULT (datetime('now'))
        )"""
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_connection_events_tenant "
        "ON connection_events(tenant_id, recorded_at DESC)"
    )
    await db.commit()


async def _migrate_redis_overage_fields(db: aiosqlite.Connection) -> None:
    """342dd15f — per-tenant Redis command budget for the send_message
    push-augmentation path.

    Mirrors the shape of _migrate_overage_fields (compute/storage). Two new
    tenants columns:
      - redis_commands_used  NUMERIC  current-month Upstash PUBLISH counter,
                                      reset on the same monthly cadence as
                                      compute_cu_hours_used / storage_gb_used.
      - redis_overage_cap_usd NUMERIC  operator-configured hard ceiling in USD
                                       (default NULL = use the code-defined
                                       tier defaults: $1 warning / $2 disable /
                                       $4 admin alert).

    Idempotent (_migrate_add_column_if_missing no-ops when already present).
    Mirrored in pg_adapter._migrate_pg_redis_overage_fields.
    """
    await _migrate_add_column_if_missing(
        db, "tenants", "redis_commands_used", "NUMERIC(14,0) DEFAULT 0"
    )
    await _migrate_add_column_if_missing(
        db, "tenants", "redis_overage_cap_usd", "NUMERIC(8,2)"
    )


async def _migrate_sprint_version_descriptions(db: aiosqlite.Connection) -> None:
    """f9188526 — sprint_version_descriptions: per-version-bucket summary text.

    Each (project_id, version) pair carries an auto-generated, human-readable
    description summarising what that sprint bucket is about as a whole — not
    just a concatenation of item titles, but a concise synthesis produced by
    _auto_generate_version_description in sprint_items.py whenever a new item
    is added to the bucket.

    The description is seeded on the first add_sprint_item call for a version
    and refreshed on every subsequent add so it always reflects the current
    set of items in that bucket. A human can overwrite it via
    upsert_sprint_version_description; the next add_sprint_item call will
    regenerate it unless the item count has not changed.

    Idempotent via CREATE TABLE IF NOT EXISTS + CREATE UNIQUE INDEX IF NOT EXISTS.
    Mirrored in pg_adapter._migrate_pg_sprint_version_descriptions.
    """
    await db.execute(
        """CREATE TABLE IF NOT EXISTS sprint_version_descriptions (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            version TEXT NOT NULL,
            description TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )"""
    )
    await db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_sprint_version_desc_pv "
        "ON sprint_version_descriptions(project_id, version)"
    )
    await db.commit()


async def _migrate_workspace_settings_active_session_threshold(
    db: aiosqlite.Connection,
) -> None:
    """6e0e5cea — configurable active-executor-session warning threshold.

    ``active_session_warning_minutes`` controls how recently a session must have
    been seen to be considered "active" for the add_sprint_item /
    fan_out_sprint_items / assign_sprint_waves warning that fires when a board
    is mutated while executors are mid-run.  Default 10 (minutes), matching
    the previously hardcoded 600-second constant.

    Idempotent (_migrate_add_column_if_missing no-ops when already present).
    Mirrored in pg_adapter._migrate_pg_workspace_settings_active_session_threshold.
    """
    await _migrate_add_column_if_missing(
        db,
        "workspace_settings",
        "active_session_warning_minutes",
        "INTEGER NOT NULL DEFAULT 10",
    )


async def _migrate_sprint_item_sprint_name(db: aiosqlite.Connection) -> None:
    """3d6bd938 — separate human-readable sprint name from the structural version field.

    version stays a semver-like structural identifier (e.g. 'v0.2.x');
    sprint_name is a nullable free-text label for the bucket (e.g.
    'docs-cloudflare'). This removes the need to overload the version string
    with a descriptive name when a genuinely separate sprint bucket is needed.

    Nullable TEXT. NULL on legacy rows (no name set). Idempotent via
    _migrate_add_column_if_missing. Mirrored in
    pg_adapter._migrate_pg_sprint_item_sprint_name.
    """
    await _migrate_add_column_if_missing(db, "sprint_items", "sprint_name", "TEXT")


async def _migrate_proposal_slug_nickname(db: aiosqlite.Connection) -> None:
    """6fb48898 — workspace_proposals.slug + .nickname: human-referenceable
    secondary keys derived from the proposal title at creation time.

    slug is a kebab-cased 60-char handle (same algorithm as sprint_items.slug
    via _sprint_item_slug_base / _unique_sprint_slug); guaranteed unique per
    tenant scope. nickname is a short 1-2 word memorable alias (same algorithm
    as sprint_items.nickname). Both nullable so existing rows are unaffected
    (NULL = not yet backfilled).

    Idempotent (_migrate_add_column_if_missing no-ops when already present).
    Mirrored in pg_adapter._migrate_pg_proposal_slug_nickname.
    """
    await _migrate_add_column_if_missing(db, "workspace_proposals", "slug", "TEXT")
    await _migrate_add_column_if_missing(db, "workspace_proposals", "nickname", "TEXT")


async def _migrate_decision_slug_nickname(db: aiosqlite.Connection) -> None:
    """6fb48898 — decisions_pinned.slug + .nickname: human-referenceable
    secondary keys derived from the decision title at creation time.

    Mirrors the sprint_items slug/nickname pattern (ae87699d). slug is a
    kebab-cased handle unique per project; nickname is a short memorable alias.
    Both nullable so existing rows are unaffected.

    Idempotent (_migrate_add_column_if_missing no-ops when already present).
    Mirrored in pg_adapter._migrate_pg_decision_slug_nickname.
    """
    await _migrate_add_column_if_missing(db, "decisions_pinned", "slug", "TEXT")
    await _migrate_add_column_if_missing(db, "decisions_pinned", "nickname", "TEXT")


async def _migrate_note_nickname(db: aiosqlite.Connection) -> None:
    """6fb48898 — project_notes.nickname: short memorable secondary key to
    complement the existing slug column (5a5bba43).

    project_notes already has slug (added by _migrate_note_slug). This adds the
    companion nickname column matching the sprint_items pattern (b6b0cee6):
    1-2 distinctive words from the note title, deduped per project.

    Nullable so existing rows are unaffected.
    Idempotent (_migrate_add_column_if_missing no-ops when already present).
    Mirrored in pg_adapter._migrate_pg_note_nickname.
    """
    await _migrate_add_column_if_missing(db, "project_notes", "nickname", "TEXT")


async def _migrate_sprint_item_prospect_bypass(db: aiosqlite.Connection) -> None:
    """94c26322 — human-set bypass flag for the prospecting safety gate.

    prospect_bypass (BOOLEAN / INTEGER 0/1, default 0) is the ONLY way to include
    an unprospected sprint item in a /goal's auto-run claimable batch.  An item
    WITHOUT real prospecting evidence (no code_pointers, no pointers, no confirmed
    prospect_status) is otherwise EXCLUDED from goal generation and claim_sprint_item
    warns hard.

    This column is intentionally NOT settable by executor sessions — it is a
    human/planning-session override only (enforced at the MCP handler level).

    Nullable INTEGER; NULL and 0 both mean "no bypass" (default). 1 means bypass.
    Idempotent (_migrate_add_column_if_missing no-ops when already present).
    Mirrored in pg_adapter._migrate_pg_sprint_item_prospect_bypass.
    """
    await _migrate_add_column_if_missing(
        db, "sprint_items", "prospect_bypass", "INTEGER NOT NULL DEFAULT 0"
    )


async def _migrate_handoff_tokens(db: aiosqlite.Connection) -> None:
    """cb8e7c0f — handoff_tokens: DB-backed provenance token store for cross-machine
    verify_handoff_token.

    The previous in-process _HANDOFF_TOKENS dict was process-local: on a multi-
    machine deployment (fly.toml max_count=40) generate_handoff on machine A minted
    a token into A's dict, but verify_handoff_token called from a new session on
    machine B read from B's empty dict and always returned not_found — making the
    trust boundary silently useless. Storing tokens in the shared DB fixes this:
    all machines read from and write to the same DB.

    token (TEXT PK): the opaque random hex value embedded in <goal_token>.
    project_id (TEXT NOT NULL): the project this token was minted for.
    expires_at (TEXT NOT NULL): ISO-8601 UTC expiry timestamp.
    consumed (INTEGER NOT NULL DEFAULT 0): 1 once the token has been verified once.
    created_at (TEXT NOT NULL): for audit/cleanup purposes.

    Idempotent: CREATE TABLE IF NOT EXISTS + CREATE INDEX IF NOT EXISTS.
    Mirrored in pg_adapter._migrate_pg_handoff_tokens.
    """
    await db.execute(
        """CREATE TABLE IF NOT EXISTS handoff_tokens (
            token TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            consumed INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )"""
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_handoff_tokens_project "
        "ON handoff_tokens(project_id)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_handoff_tokens_expires "
        "ON handoff_tokens(expires_at)"
    )
    await db.commit()


async def _migrate_wave_gate_results(db: aiosqlite.Connection) -> None:
    """d2430713 — wave_gate_results: persist complete_wave_gate evidence records.

    Stores the verified run_verification payload when an executor calls
    complete_wave_gate after successfully running a wave's gate action list.
    One row per (project_id, wave_label) pair — UNIQUE constraint enforces
    that each wave gate can only be completed once.

    Fields:
      id                 — UUID primary key
      project_id         — owning project
      wave_label         — the wave whose gate was passed (e.g. 'wave-1')
      gate_passed        — always 1 (rejected gates never write a row)
      exit_code          — exit_code from the run_verification result (0 = pass)
      passed_count       — number of tests that passed (from run_verification)
      failed_count       — number of tests that failed (should always be 0 here)
      verification_status — the status field from run_verification ('ok')
      evidence_snapshot  — full JSON of the run_verification payload
      actor              — session/actor that called complete_wave_gate
      completed_at       — wall-clock UTC timestamp

    Idempotent via CREATE TABLE IF NOT EXISTS + CREATE INDEX IF NOT EXISTS.
    Mirrored in pg_adapter._migrate_pg_wave_gate_results.
    """
    await db.execute(
        "CREATE TABLE IF NOT EXISTS wave_gate_results ("
        "    id TEXT PRIMARY KEY,"
        "    project_id TEXT NOT NULL,"
        "    wave_label TEXT NOT NULL,"
        "    gate_passed INTEGER NOT NULL DEFAULT 1,"
        "    exit_code INTEGER,"
        "    passed_count INTEGER,"
        "    failed_count INTEGER,"
        "    verification_status TEXT,"
        "    evidence_snapshot TEXT,"
        "    actor TEXT,"
        "    completed_at TEXT NOT NULL DEFAULT (datetime('now')),"
        "    UNIQUE(project_id, wave_label)"
        ")"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_wave_gate_results_project "
        "ON wave_gate_results(project_id, wave_label)"
    )
    await db.commit()


async def _migrate_wave_gate_configs(db: aiosqlite.Connection) -> None:
    """74a8f420 — wave_gate_configs: the on-the-fly-configurable action pipeline
    (push_dev/push_main/deploy/wait/run_verification) attached to a wave or
    wave-range, keyed by its boundary wave (``wave_end``). claim_sprint_item
    reads this table (plus wave_gate_results) to structurally refuse claiming
    any item whose wave sorts beyond a configured-but-unpassed boundary.

    Fields:
      id           — UUID primary key
      project_id   — owning project
      wave_start   — first wave covered by this gate (documentation only)
      wave_end     — boundary wave; the enforcement key (e.g. 'wave-3')
      actions      — JSON array of {"type": ..., ...params} action dicts
      actor        — session/actor that configured the gate
      created_at / updated_at — wall-clock UTC timestamps

    One pipeline per (project_id, wave_end) — UNIQUE constraint. Reconfiguring
    an un-passed boundary is an upsert (see db.sprint_items.configure_wave_gate);
    once wave_gate_results has a row for wave_end the config is immutable.

    Idempotent via CREATE TABLE IF NOT EXISTS + CREATE INDEX IF NOT EXISTS.
    Mirrored in pg_adapter._migrate_pg_wave_gate_configs.
    """
    await db.execute(
        "CREATE TABLE IF NOT EXISTS wave_gate_configs ("
        "    id TEXT PRIMARY KEY,"
        "    project_id TEXT NOT NULL,"
        "    wave_start TEXT NOT NULL,"
        "    wave_end TEXT NOT NULL,"
        "    actions TEXT NOT NULL,"
        "    actor TEXT,"
        "    created_at TEXT NOT NULL DEFAULT (datetime('now')),"
        "    updated_at TEXT NOT NULL DEFAULT (datetime('now')),"
        "    UNIQUE(project_id, wave_end)"
        ")"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_wave_gate_configs_project "
        "ON wave_gate_configs(project_id, wave_end)"
    )
    await db.commit()


async def _migrate_server_logs(db: aiosqlite.Connection) -> None:
    """f0a48685 — server_logs: application-wide ERROR/WARNING log capture.

    A custom logging.Handler attached to the root logger at app startup writes
    every WARNING-or-above record here so post-mortem diagnosis of incidents
    (tools/list timeouts, OAuth failures, deploy health issues) is possible
    from a hosted-only claude.ai session with no local machine access — the
    same motivation as connection_events, but for arbitrary app-level log
    records rather than per-/mcp-request metadata.

    Fields:
      level      — 'WARNING', 'ERROR', or 'EXCEPTION' (exc_info present)
      logger     — logger name (e.g. 'meridian.server', 'meridian.db')
      message    — formatted log message (first 2000 chars)
      exc_text   — formatted traceback string when exc_info is present (nullable)
      recorded_at — wall-clock UTC timestamp

    No tenant_id column: server_logs are process-global (they capture things
    that happen before or outside of any tenant context). The ring-buffer cap
    is global (last 2000 rows total), not per-tenant, matching the nature of
    the data.

    Kept separate from connection_events intentionally: connection_events is
    one row per /mcp HTTP request with structured request metadata; server_logs
    is one row per log record from anywhere in the application. Merging them
    would conflate two different granularities and make both harder to query.

    Idempotent via CREATE TABLE IF NOT EXISTS + CREATE INDEX IF NOT EXISTS.
    Mirrored in pg_adapter._migrate_pg_server_logs.
    """
    await db.execute(
        """CREATE TABLE IF NOT EXISTS server_logs (
            id TEXT PRIMARY KEY,
            level TEXT NOT NULL DEFAULT 'ERROR',
            logger TEXT NOT NULL DEFAULT '',
            message TEXT NOT NULL DEFAULT '',
            exc_text TEXT,
            recorded_at TEXT NOT NULL DEFAULT (datetime('now'))
        )"""
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_server_logs_level_at "
        "ON server_logs(level, recorded_at DESC)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_server_logs_at "
        "ON server_logs(recorded_at DESC)"
    )
    await db.commit()


async def _migrate_custom_hooks(db: aiosqlite.Connection) -> None:
    """273287cb — custom_hooks: user-creatable Claude Code hooks.

    Generalizes past the single auto-written sprint_guard.sh/.ps1 pair (see
    handoff._write_sprint_guard_hooks) so a project can define its own
    arbitrary PreToolUse/PostToolUse/Stop hooks that get written into
    .claude/hooks/ by the same generate_handoff mechanism.

    Fields:
      id          — UUID primary key
      project_id  — owning project
      name        — human-entered display name
      slug        — filesystem-safe derived name (unique per project); used
                    for the written .claude/hooks/<slug>.sh / .ps1 filenames
      event       — 'PreToolUse' | 'PostToolUse' | 'Stop'
      matcher     — optional tool-name regex (e.g. "Edit|Write"); ignored
                    for Stop hooks
      script_sh   — POSIX shell script body (required)
      script_ps1  — optional PowerShell script body
      blocking    — 1: script's own exit code drives real Claude Code
                    exit-code-blocking semantics (exit 2 blocks). 0: written
                    wrapped so a would-be exit 2 is downgraded to 1 — output
                    still surfaces as a strong suggestion but never hard-blocks
      enabled     — 1: (re)written on every generate_handoff; 0: skipped
      created_at / updated_at — wall-clock UTC timestamps

    UNIQUE(project_id, slug) so two hooks on one project can never collide on
    the filename they'd be written to.

    Idempotent via CREATE TABLE IF NOT EXISTS + CREATE INDEX IF NOT EXISTS.
    Mirrored in pg_adapter._migrate_pg_custom_hooks.
    """
    await db.execute(
        "CREATE TABLE IF NOT EXISTS custom_hooks ("
        "    id TEXT PRIMARY KEY,"
        "    project_id TEXT NOT NULL,"
        "    name TEXT NOT NULL,"
        "    slug TEXT NOT NULL,"
        "    event TEXT NOT NULL CHECK (event IN ('PreToolUse','PostToolUse','Stop')),"
        "    matcher TEXT,"
        "    script_sh TEXT NOT NULL,"
        "    script_ps1 TEXT,"
        "    blocking INTEGER NOT NULL DEFAULT 1,"
        "    enabled INTEGER NOT NULL DEFAULT 1,"
        "    created_at TEXT NOT NULL DEFAULT (datetime('now')),"
        "    updated_at TEXT NOT NULL DEFAULT (datetime('now')),"
        "    UNIQUE(project_id, slug)"
        ")"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_custom_hooks_project "
        "ON custom_hooks(project_id, event)"
    )
    await db.commit()


async def _migrate_sprint_item_require_verification(db: aiosqlite.Connection) -> None:
    """e2e1b682 — opt-in independent fresh-session verifier gate flag.

    require_verification (INTEGER 0/1, default 0) marks a sprint item as
    needing an on-file, independent PASS (see sprint_item_verifications)
    before complete_sprint_item will let the completion stick. Mirrors
    prospect_bypass's shape exactly (nullable-by-default INTEGER flag,
    settable via patch_sprint_item / update_sprint_item).

    Nullable-equivalent INTEGER; NULL and 0 both mean "no gate" (default). 1
    means the independent-verification gate is required.
    Idempotent (_migrate_add_column_if_missing no-ops when already present).
    Mirrored in pg_adapter._migrate_pg_sprint_item_require_verification.
    """
    await _migrate_add_column_if_missing(
        db, "sprint_items", "require_verification", "INTEGER NOT NULL DEFAULT 0"
    )


async def _migrate_sprint_item_verifications_table(db: aiosqlite.Connection) -> None:
    """e2e1b682 — sprint_item_verifications: durable audit trail of independent
    fresh-session PASS/FAIL verdicts filed against a sprint item.

    One row per verdict filed (not one row per item — a FAIL can be followed
    by a later PASS once the issue is fixed and re-checked). complete_sprint_item
    reads the MOST RECENT row for a require_verification item and refuses to
    complete unless it is verdict='pass' AND verifier_session_id differs from
    the completing actor.

    Fields:
      verdict              — 'pass' | 'fail'
      verifier_session_id  — the session that performed the independent check
      notes                — optional free-text explanation from the verifier
      created_at           — wall-clock UTC timestamp

    CREATE_TABLES covers fresh DBs; this is the upgrade path for existing
    ones. Idempotent via CREATE TABLE IF NOT EXISTS + CREATE INDEX IF NOT
    EXISTS. Mirrored in pg_adapter._migrate_pg_sprint_item_verifications_table.
    """
    await db.execute(
        """CREATE TABLE IF NOT EXISTS sprint_item_verifications (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            sprint_item_id TEXT NOT NULL,
            verdict TEXT NOT NULL,
            verifier_session_id TEXT NOT NULL,
            notes TEXT,
            seq INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )"""
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_sprint_item_verifications_item "
        "ON sprint_item_verifications(sprint_item_id, seq DESC)"
    )
    await db.commit()


async def _migrate_proposal_github_issue(db: aiosqlite.Connection) -> None:
    """3999d90f — workspace_proposals.github_issue_number + .github_issue_url.

    Storage for the "also file a GitHub issue?" conditional HITL workflow:
    promote_workspace_proposal fires a HITL when a code-related proposal is
    promoted under a project with a connected GitHub repo; if answered yes,
    the created issue's number/URL is persisted back onto the proposal here
    via set_proposal_github_issue. Both columns nullable — most proposals
    never go through this path.

    Idempotent (_migrate_add_column_if_missing no-ops when already present).
    Mirrored in pg_adapter._migrate_pg_proposal_github_issue.
    """
    await _migrate_add_column_if_missing(
        db, "workspace_proposals", "github_issue_number", "INTEGER"
    )
    await _migrate_add_column_if_missing(
        db, "workspace_proposals", "github_issue_url", "TEXT"
    )
