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

__all__ = ['_migrate_task_log_backlog_future', '_migrate_task_log_backburner', '_migrate_task_log_hitl', '_column_exists', '_migrate_add_column_if_missing', '_migrate_human_identity', '_migrate_v24_task_tree_and_framework', '_migrate_v25_feedback_and_notifications', '_migrate_v33_hitl_kind_payload', '_migrate_v34_hitl_auto_answer', '_migrate_v34_workspace_settings', '_migrate_dunning_fields', '_migrate_overage_fields', '_migrate_v26_client_type', '_migrate_ntfy_notifications', '_migrate_notify_email', '_migrate_github_integration', '_migrate_sprint_item_dependencies', '_migrate_v09_notes_and_magic_links', '_migrate_v24_pinned_decisions_and_hitl', '_migrate_goal_field_timestamps', '_migrate_task_claims', '_migrate_task_sprint_link', '_migrate_session_type', '_migrate_session_summary', '_migrate_parent_session_id', '_migrate_decisions', '_migrate_goal_mode', '_migrate_worker_pid', '_migrate_rewind_token', '_migrate_project_settings', '_migrate_neon_pool_projects_free_tier', '_migrate_tenants_free_plan', '_migrate_decisions_free_category', '_migrate_sessions_archived', '_migrate_goal_hierarchy', '_migrate_sprint_items_v2', '_migrate_drop_chat_tables', '_migrate_hosted_tables', '_migrate_session_notes', '_migrate_milestone_type', '_migrate_executor_runs', '_migrate_file_locks', '_migrate_file_symbol_claims', '_migrate_blog_posts', '_migrate_workspace_layer', '_migrate_checkpoint_data', 'init_hosted_tables', '_migrate_sprint_item_tree', '_migrate_api_token_type', '_migrate_api_tokens_expires_at', '_migrate_github_to_projects', '_migrate_touches_files', '_migrate_oauth_codes_table', '_migrate_device_codes_table', '_migrate_sprint_items_indeterminate', '_migrate_sprint_items_provisional_complete', '_migrate_workspace_members_rbac', '_migrate_project_icon', '_internal_emails', '_migrate_tenants_is_internal', '_migrate_admin_plan', '_migrate_active_worktrees', '_migrate_workspace_tenant_isolation', '_migrate_workspace_sprint_board', '_migrate_registered_hostnames', '_migrate_queued_session', '_migrate_parallel_safety', '_migrate_changelog_entries', '_migrate_agent_instructions', '_migrate_note_kind', '_migrate_tunnel_active', '_backfill_agent_instructions', '_migrate_code_intel', '_migrate_tunnel_plugins', '_migrate_notes_priority', '_migrate_task_log_kind', '_migrate_note_slug', '_slugify_note', '_migrate_oauth_refresh_tokens', '_migrate_decision_priority_edit_log', '_migrate_code_anchored_notes', '_migrate_note_source', '_migrate_session_sprint_version', '_migrate_project_execution_mode', '_migrate_decision_code_anchor', '_migrate_session_graph_snapshots', '_migrate_agent_tasks_table']

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
    """6234f9b8 â€” blog_posts table for the admin Blog CMS."""
    await db.execute(
        """CREATE TABLE IF NOT EXISTS blog_posts (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            slug TEXT NOT NULL UNIQUE,
            body_md TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'draft',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            published_at TEXT
        )"""
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_blog_posts_status ON blog_posts(status)"
    )
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


async def _migrate_project_icon(db: aiosqlite.Connection) -> None:
    """G4.17 â€” single-emoji icon on projects for sidebar/tab rendering."""
    await _migrate_add_column_if_missing(db, "projects", "icon", "TEXT")


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
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_tasks_agent "
        "ON agent_tasks(agent_id, status)"
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

