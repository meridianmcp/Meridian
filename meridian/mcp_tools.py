"""Shared MCP tool metadata for the Meridian server."""

from __future__ import annotations

from typing import Any


_TOOL_EXAMPLES: dict[str, str] = {
    "create_project": 'create_project(name="my-app")',
    "start_session": 'start_session(project_id="abc-123", session_name="feature-x", human_id="alice", role="executor")',
    "register_session": 'register_session(project_id="abc-123", session_name="feature-x", human_id="alice")',
    "log_task": 'log_task(session_id="session-uuid", project_id="abc-123", description="Fixed auth bug", status="done")',
    "get_context_block": 'get_context_block(project_id="abc-123", mode="chat")',
    "claim_task": 'claim_task(task_id="task-uuid-here")',
    "complete_task": 'complete_task(task_id="task-uuid-here")',
    "get_tasks": 'get_tasks(project_id="abc-123")',
    "search_tasks": 'search_tasks(project_id="abc-123", query="rate limiting bug")',
    "get_goal": 'get_goal(project_id="abc-123")',
    "set_goal": 'set_goal(project_id="abc-123", content="Build a great product")',
    "set_sprint": 'set_sprint(project_id="abc-123", sprint="v2.0 - auth + dashboard")',
    "get_sprint_progress": 'get_sprint_progress(project_id="abc-123")',
    "set_north_star": 'set_north_star(project_id="abc-123", north_star="Ship by Q3")',
    "pin_decision": 'pin_decision(project_id="abc-123", title="Use psycopg3", body="asyncpg has DLL issues on Windows", category="TECHNICAL")',
    "get_pinned_decisions": 'get_pinned_decisions(project_id="abc-123")',
    "generate_handoff": 'generate_handoff(project_id="abc-123", mode="delta", session_id="session-uuid")',
    "get_session_brief": 'get_session_brief(project_id="abc-123")',
    "archive_decision": 'archive_decision(decision_id="decision-uuid")',
    "checkpoint": 'checkpoint(session_id="session-uuid", project_id="abc-123")',
    "request_hitl": 'request_hitl(project_id="abc-123", question="Should we add rate limiting here?", urgency="normal")',
    "get_hitl_request": 'get_hitl_request(request_id="hitl-uuid")',
    "add_note": 'add_note(project_id="abc-123", title="Deploy note", body="Reminder: update env vars before deploy", tags="ops,deploy")',
    "get_notes": 'get_notes(project_id="abc-123")',
    "read_note": 'read_note(project_id="abc-123", slug="deploy-note")',
    "add_workspace_note": 'add_workspace_note(title="Onboarding", body="All repos use pixi", tags="setup")',
    "get_workspace_notes": 'get_workspace_notes(tag="setup")',
    "pin_workspace_decision": 'pin_workspace_decision(title="Monorepo", body="One repo for all services", category="ARCHITECTURAL")',
    "get_workspace_decisions": 'get_workspace_decisions()',
    "get_workspace_settings": 'get_workspace_settings()',
    "update_workspace_settings": 'update_workspace_settings(hitl_auto_answer_default=True, sprint_name_default="june-sprint")',
    "add_sprint_item": 'add_sprint_item(project_id="abc-123", title="Add OAuth login", item_group="auth")',
    "update_sprint_item": 'update_sprint_item(project_id="abc-123", item_id="item-uuid", title="Add OAuth + SAML login", group="auth", human_id="alice")',
    "reconcile_sprint_drift": 'reconcile_sprint_drift(project_id="abc-123")',
    "get_planning_brief": 'get_planning_brief(project_id="abc-123")',
    "get_sprint_items": 'get_sprint_items(project_id="abc-123")',
    "complete_sprint_item": 'complete_sprint_item(item_id="item-uuid")',
    "claim_sprint_item": 'claim_sprint_item(project_id="abc-123", item_id="item-uuid")',
    "heartbeat": 'heartbeat(session_id="session-uuid")',
    "list_projects": 'list_projects()',
    "get_sessions": 'get_sessions(project_id="abc-123")',
    "set_executor_config": 'set_executor_config(project_id="abc-123", repo_path="/repo", env_file="/repo/.env", test_cmd="pixi run test", test_min=619, deploy_cmd="git push", shell_type="powershell", branch="dev")',
    "claim_file": 'claim_file(session_id="session-uuid", file_path="meridian/server.py")',
    "release_file": 'release_file(session_id="session-uuid", file_path="meridian/server.py")',
    "idle_until_session_done": 'idle_until_session_done(watching_session_id="session-uuid")',
    "get_session_log": 'get_session_log(session_id="session-uuid")',
}


_MCP_TOOLS_LIST: list[dict[str, Any]] = [
    {"name": "create_project", "description": "Create a new Meridian project.",
     "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "register_session", "description": "Low-level: register this session without loading goal context. Use start_session instead for executor/human sessions — it registers AND returns goal + tasks in one call. Use register_session when you only need a session ID and will fetch context separately.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"}, "session_name": {"type": "string"},
         "human_id": {"type": "string"},
         "client": {"type": "string", "enum": ["claude-code", "claude-desktop", "cursor", "other"]}},
         "required": ["project_id", "session_name"]}},
    {"name": "start_session", "description": "Register a session and return orientation. Compact by default (session_id, sprint focus + status counts, 3 recent tasks, board_change count) to keep an executor's context small. Pass compact=false for the full block (goal XML, decisions, MERIDIAN.md instructions, workspace context, sprint items) — or fetch it later with get_session_brief.",
     "inputSchema": {"type": "object", "properties": {
          "project_id": {"type": "string"}, "session_name": {"type": "string"},
          "human_id": {"type": "string"},
          "client": {"type": "string", "enum": ["claude-code", "claude-desktop", "cursor", "other"]},
          "role": {"type": "string", "enum": ["executor"], "description": "Pass 'executor' to inject executor_config and credentials guidance."},
          "compact": {"type": "boolean", "description": "Default true — slim orientation. Set false for the full goal/instructions payload."}},
          "required": ["project_id", "session_name"]}},
    {"name": "list_projects", "description":
        "Read-only: List all projects — find, browse, or look up your projects and their IDs. "
        "Call this first when you have a project name but need its project_id, or to discover "
        "which projects exist. Returns [{id, name, sprint, created_at}] newest first.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "get_project_by_name", "description":
        "Read-only: Find a project by name — look up, search, or resolve a project's project_id "
        "from its name (case-insensitive substring match). Use when the user names a project but "
        "you need its id. Returns the first hit with id, name, and sprint.",
     "inputSchema": {"type": "object", "properties": {
         "name": {"type": "string"}},
         "required": ["name"]}},
    {"name": "get_goal", "description": "Read-only: Fine-grained — return just the goal fields (north_star, sprint, version_goal) in isolation. Use start_session or get_session_brief for full context including tasks and decisions. Use get_goal when you only need the raw goal fields.",
     "inputSchema": {"type": "object", "properties": {"project_id": {"type": "string"}}, "required": ["project_id"]}},
    {"name": "set_goal", "description": "Set or update the goal state.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"}, "content": {"type": "string"}}, "required": ["project_id", "content"]}},
    {"name": "set_north_star", "description": "Update only the north star — the long-lived product vision that rarely changes. Distinct from the version goal (set_goal). Any team member can call this.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"},
         "north_star": {"type": "string"}},
         "required": ["project_id", "north_star"]}},
    {"name": "log_task", "description": "Log a task this session completed or is working on. Valid statuses: pending, in_progress, done, failed, backlog, future, backburner.",
     "inputSchema": {"type": "object", "properties": {
         "session_id": {"type": "string"}, "project_id": {"type": "string"},
         "description": {"type": "string"}, "status": {"type": "string"},
         "kind": {"type": "string", "enum": ["shipped", "found", "decided", "blocked"], "description": "Entry taxonomy. shipped=work done, found=discovery, decided=arch choice, blocked=blocker."}},
         "required": ["session_id", "project_id", "description"]}},
    {"name": "get_tasks", "description": "Read-only: Get recent tasks across all sessions.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["project_id"]}},
    {"name": "search_tasks", "description": "Read-only: Search tasks by keyword or natural-language query. Uses trigram similarity on Postgres, LIKE on SQLite. Returns top matches with similarity score.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"}, "query": {"type": "string"}, "limit": {"type": "integer"}},
         "required": ["project_id", "query"]}},
    {"name": "generate_handoff", "description":
        "Read-only: Generate a context handoff. mode='full' writes the complete L0/L1/L2 handoff; "
        "mode='delta' returns a compact session update (completed + pending + /goal); "
        "mode='starter' returns a <=20-line block for paste-after-/compact or cold start - "
        "project_id, start_session command, last 5 completed titles, top 3 pending IDs, /goal; "
        "mode='planner' returns strategic context for a claude.ai planning chat.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"},
         "mode": {"type": "string", "enum": ["full", "delta", "planner", "starter"]},
         "session_id": {"type": "string", "description": "Optional session id for auto-delta on repeated calls in the same session."}},
         "required": ["project_id"]}},
    {"name": "get_context_block", "description":
        "Read-only: Return a compact plain-text project context block (north star, sprint, "
        "pending sprint items, recent tasks, recent decisions, active sessions). "
        "mode='full' (default) for Code Handoff into a fresh Claude Code session; "
        "mode='chat' for a shorter paste into a new claude.ai conversation.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"},
         "mode": {"type": "string", "enum": ["full", "chat"]}},
         "required": ["project_id"]}},
    {"name": "pin_decision", "description":
        "Create a pinned decision (editable constitution row). Use for the "
        "current authoritative truth that supersedes earlier statements. "
        "category is free-text; suggested values: STRATEGIC, COMPETITIVE, "
        "TECHNICAL, TACTICAL, BUSINESS, PRODUCT, ARCHITECTURAL.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"},
         "title": {"type": "string"},
         "body": {"type": "string"},
         "category": {"type": "string"}},
         "required": ["project_id", "title", "body"]}},
    {"name": "update_decision", "description":
        "Patch a pinned decision. Pass new_title + new_body to atomically "
        "supersede (creates a new active row, marks old as superseded with "
        "back-link). Otherwise patches body/title/category/status in place.",
     "inputSchema": {"type": "object", "properties": {
         "decision_id": {"type": "string"},
         "new_title": {"type": "string"},
         "new_body": {"type": "string"},
         "title": {"type": "string"},
         "body": {"type": "string"},
         "category": {"type": "string"},
         "status": {"type": "string"}},
         "required": ["decision_id"]}},
    {"name": "get_pinned_decisions", "description":
        "Read-only: List pinned decisions (active only by default, newest first).",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"},
         "include_superseded": {"type": "boolean"}},
         "required": ["project_id"]}},
    {"name": "archive_decision", "description":
        "Archive a pinned decision by id. Soft-deletes to preserve the audit trail. "
        "Use when something was filed by mistake or is a duplicate. "
        "For retiring a valid but superseded decision, prefer update_decision(status=superseded).",
     "inputSchema": {"type": "object", "properties": {
         "decision_id": {"type": "string"}},
         "required": ["decision_id"]}},
    {"name": "checkpoint", "description":
        "Save progress mid-session. Runs auto_capture (buckets done tasks into a note), "
        "generates a delta handoff, and returns a compact summary with what was done, "
        "what's pending, and the suggested next /goal string. Call before context fills "
        "up or before ending a session.",
     "inputSchema": {"type": "object", "properties": {
         "session_id": {"type": "string"},
         "project_id": {"type": "string"}},
         "required": ["session_id", "project_id"]}},
    {"name": "request_hitl", "description":
        "Surface a question to the human-in-the-loop queue. urgency='blocking' "
        "means this session pauses until answered (poll get_hitl_request). "
        "urgency='normal'/'high' lands in the dashboard but doesn't block. "
        "assigned_to routes to a specific human_id (null = broadcast). "
        "kind='correction' files a non-blocking mid-run correction: never "
        "auto-answered, never blocks — an unattended executor picks it up at the "
        "next sprint-item boundary, applies it, and continues. Pass `options` "
        "(answer choices, rendered as buttons) and `recommended` (an option "
        "string or 0-based index) to flag the safe default — the dashboard "
        "highlights it and Enter submits it, and an auto-answer picks it.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"},
         "question": {"type": "string"},
         "session_id": {"type": "string"},
         "context": {"type": "string"},
         "urgency": {"type": "string", "enum": ["normal", "high", "blocking"]},
         "kind": {"type": "string", "enum": ["question", "correction"], "description": "question (default, auto-answerable) or correction (non-blocking mid-run human correction)."},
         "assigned_to": {"type": "string"},
         "options": {"type": "array", "items": {"type": "string"}, "description": "Answer choices rendered as selectable buttons in the dashboard."},
         "recommended": {"description": "The safe-default option — an option string or a 0-based index into options. Highlighted in the dashboard; Enter submits it; auto-answer prefers it."}},
         "required": ["project_id", "question"]}},
    {"name": "get_hitl_request", "description":
        "Read-only: Poll a HITL request for the human's answer. Returns the row including "
        "status ('pending'|'answered'|'dismissed') and answer text.",
     "inputSchema": {"type": "object", "properties": {
         "request_id": {"type": "string"}},
         "required": ["request_id"]}},
    {"name": "add_note", "description":
        "Add a per-project wiki note (setup, gotcha, howto, env, ...). "
        "Free-form title/body; comma-separated tags optional. Optional kind "
        "(wiki=gotcha/rule/howto, insight=strategic/product analysis, "
        "reference=external/one-off docs) controls how the dashboard renders it. "
        "Tag a note 'roadmap' AND pass a committable category (TECHNICAL/"
        "ARCHITECTURAL/PRODUCT) to also append it to ROADMAP.md's roadmap-notes anchor.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"},
         "title": {"type": "string"},
         "body": {"type": "string"},
         "tags": {"type": "string"},
         "kind": {"type": "string", "enum": ["wiki", "insight", "reference"]},
         "priority": {"type": "string", "enum": ["high", "normal", "low"], "description": "high-priority notes surface first in generate_handoff and planner context."},
         "category": {"type": "string"}},
         "required": ["project_id", "title", "body"]}},
    {"name": "capture_insight", "description":
        "Save a key takeaway from a planning (claude.ai) conversation in one call — "
        "persists a prominent kind='insight' note that's searchable, filterable, and "
        "surfaced in generate_handoff(mode='planner'), WITHOUT the auto-capture "
        "'Session summary' noise checkpoint() makes. Pass body (markdown) OR "
        "bullet_points (a list, joined into a bullet list). Use mid-conversation "
        "whenever you're afraid of losing context.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"},
         "title": {"type": "string"},
         "body": {"type": "string", "description": "Markdown body. Omit if using bullet_points."},
         "bullet_points": {"type": "array", "items": {"type": "string"}, "description": "Key takeaways, joined into a markdown bullet list."},
         "tags": {"type": "string", "description": "Optional comma-separated tags (an 'insight' tag is always added)."},
         "priority": {"type": "string", "enum": ["high", "normal", "low"], "description": "high-priority notes appear first in planner context and generate_handoff."}},
         "required": ["project_id", "title"]}},
    {"name": "get_notes", "description":
        "Read-only: List project notes (newest first), LIGHTWEIGHT by default — "
        "each item is id/slug/title/tags/kind/priority/timestamps with NO body, "
        "so the list never overflows context. This is the pull model: scan the "
        "list, then call read_note(project_id, slug) to fetch one note's full "
        "body on demand. Optional ?tag substring filter and ?query full-text "
        "search (matches title+body even though bodies aren't returned). Pass "
        "bodies=true only when you truly need every body inline.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"},
         "tag": {"type": "string"},
         "query": {"type": "string", "description": "Text search across note title and body (case-insensitive)."},
         "bodies": {"type": "boolean", "description": "Default false. true returns full note bodies inline (legacy behavior) — usually unnecessary; prefer read_note(slug)."}},
         "required": ["project_id"]}},
    {"name": "read_note", "description":
        "Read-only: Fetch one project note's full body by its per-project slug "
        "(the ``slug`` field from get_notes). The pull half of the list→read "
        "model — get_notes returns slugs without bodies, read_note pulls a "
        "single body when you need it.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"},
         "slug": {"type": "string", "description": "The note's slug (kebab-cased, unique per project) as returned by get_notes."}},
         "required": ["project_id", "slug"]}},
    {"name": "delete_note", "description":
        "Hard-delete a project note by id.",
     "inputSchema": {"type": "object", "properties": {
         "note_id": {"type": "string"}},
         "required": ["note_id"]}},
    {"name": "add_workspace_note", "description":
        "Add a workspace-level wiki note that applies across ALL projects in this "
        "workspace (onboarding, cross-cutting conventions, shared infra). Unlike "
        "add_note, it is not tied to a project and is injected at the top of every "
        "project's context block + handoff. Comma-separated tags optional.",
     "inputSchema": {"type": "object", "properties": {
         "title": {"type": "string"},
         "body": {"type": "string"},
         "tags": {"type": "string"}},
         "required": ["title", "body"]}},
    {"name": "get_workspace_notes", "description":
        "Read-only: List workspace-level notes (newest first). Optional ?tag substring filter.",
     "inputSchema": {"type": "object", "properties": {
         "tag": {"type": "string"}},
         "required": []}},
    {"name": "pin_workspace_decision", "description":
        "Pin a workspace-level decision that applies across ALL projects (shared "
        "architecture, org-wide standards). Injected at the top of every project's "
        "context block + handoff. category is free-text (STRATEGIC, TECHNICAL, "
        "ARCHITECTURAL, PRODUCT, ...).",
     "inputSchema": {"type": "object", "properties": {
         "title": {"type": "string"},
         "body": {"type": "string"},
         "category": {"type": "string"}},
         "required": ["title", "body"]}},
    {"name": "get_workspace_decisions", "description":
        "Read-only: List workspace-level pinned decisions (active only by default, newest first).",
     "inputSchema": {"type": "object", "properties": {
         "include_superseded": {"type": "boolean"}},
         "required": []}},
    {"name": "get_workspace_settings", "description":
        "Read-only: Read workspace-global default settings (applies across ALL projects in this "
        "workspace): hitl_auto_answer_default and sprint_name_default. Returns the "
        "singleton settings row.",
     "inputSchema": {"type": "object", "properties": {},
         "required": []}},
    {"name": "update_workspace_settings", "description":
        "Update workspace-global default settings. Pass only the fields you want to "
        "change. hitl_auto_answer_default (bool) seeds new projects' HITL auto-answer "
        "behaviour; sprint_name_default (string) is the default sprint name; "
        "handoff_template (string) overrides the default full-mode handoff with a "
        "custom template — supports {{sprint}}, {{recent_tasks}}, {{decisions}}, "
        "{{north_star}}, {{version_goal}}, {{pending_items}}, {{notes}} placeholders. "
        "Pass an empty string to revert to the server default.",
     "inputSchema": {"type": "object", "properties": {
         "hitl_auto_answer_default": {"type": "boolean"},
         "sprint_name_default": {"type": "string"},
         "handoff_template": {"type": "string"}},
         "required": []}},
    {"name": "get_session_brief", "description":
        "Read-only: Call this FIRST for project summaries or to see what a session did — "
        "returns session, tasks, decisions, and recent commits in one call. "
        "Replaces the start_session + get_context_block two-call pattern for "
        "worker/automation sessions. Returns sprint focus, pending sprint items, "
        "recent tasks, any blocking failures, and pending HITL requests in a compact "
        "XML envelope (<500 tokens).",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"},
         "role": {"type": "string", "enum": ["worker", "planner", "review"],
                  "description": "Controls verbosity. 'worker'=sprint+tasks only, 'planner'=full context."}},
         "required": ["project_id"]}},
    {"name": "list_hitl_requests", "description":
        "Read-only: List HITL requests without needing UUIDs. OMIT project_id to list pending "
        "HITLs across ALL your projects (matches the dashboard) — planning sessions should call it "
        "this way so HITLs filed under another project aren't missed (a common cause of false "
        "'no pending HITLs' confidence). Pass project_id to scope to one project. Returns pending "
        "queue plus answered/dismissed from the last 24 h by default so planning sessions can see "
        "what was recently decided without a separate call. Pass status='pending' for only the "
        "active queue, or status='answered'/'dismissed'/'all' for specific history.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string",
                        "description": "Optional. Omit to list across all projects."},
         "status": {"type": "string",
                    "description": "Filter: omit for pending+recent-answered (default), 'pending', 'answered', 'dismissed', or 'all'."},
         "limit": {"type": "integer", "description": "Max results, default 50."}},
         "required": []}},
    {"name": "answer_hitl", "description":
        "Answer a pending HITL request programmatically. Marks it answered so "
        "the waiting session can resume. Use list_hitl_requests to find request IDs.",
     "inputSchema": {"type": "object", "properties": {
         "request_id": {"type": "string"},
         "answer": {"type": "string"},
         "answered_by": {"type": "string", "description": "Optional human_id of the answerer."}},
         "required": ["request_id", "answer"]}},
    {"name": "dismiss_hitl", "description":
        "Dismiss a HITL request (won't-answer / no longer relevant). "
        "Stays in audit trail. Use list_hitl_requests to find request IDs.",
     "inputSchema": {"type": "object", "properties": {
         "request_id": {"type": "string"}},
         "required": ["request_id"]}},
    {"name": "list_sessions", "description":
        "Read-only: List active sessions for a project. Useful for planning chat to see "
        "what's currently running before filing new sprint items.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"},
         "status": {"type": "string",
                    "description": "Filter by status: 'active' (default), or 'all' for all sessions."}},
         "required": ["project_id"]}},
    {"name": "add_sprint_note", "description":
        "Add an ephemeral note to the current session's scratch pad. "
        "Use for constraints, blockers, working assumptions valid only this session. "
        "Notes are auto-deleted when the session closes.",
     "inputSchema": {"type": "object", "properties": {
         "session_id": {"type": "string"},
         "title": {"type": "string"},
         "body": {"type": "string"}},
         "required": ["session_id", "title", "body"]}},
    {"name": "get_sprint_notes", "description":
        "Read-only: Get all ephemeral scratch-pad notes for the current session. "
        "Shown at the top of session briefs so every cold start sees active constraints.",
     "inputSchema": {"type": "object", "properties": {
         "session_id": {"type": "string"}},
         "required": ["session_id"]}},
    {"name": "set_sprint", "description":
        "Update only the sprint — the short-term focus that changes each session or week. "
        "Any team member can call this; no ownership check. If pending items from the current "
        "sprint were never started, returns a WARNING block listing them. Pass force=true to "
        "override and overwrite anyway.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"},
         "sprint": {"type": "string"},
         "force": {"type": "boolean",
                   "description": "Skip the unstarted-items guard and overwrite the sprint anyway."}},
         "required": ["project_id", "sprint"]}},
    {"name": "get_sprint_progress", "description":
        "Read-only: Return summary of sprint items by status (pending/in_progress/done/failed) "
        "optionally filtered by version or item_group. Returns total, done, in_progress, pending, "
        "failed, percent_complete, and item list. Useful for planning sessions to see how far "
        "through the sprint we are without listing all items. Pass session_id to also get a "
        "board_change field reporting items added since that session started (live-queue signal "
        "— call this between sprint items to pick up mid-run injections).",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"},
         "session_id": {"type": "string", "description": "Optional: include board_change (items added since this session started)."},
         "version": {"type": "string", "description": "Filter to a specific sprint version bucket."},
         "item_group": {"type": "string", "description": "Filter to a specific item group."}},
         "required": ["project_id"]}},
    {"name": "add_sprint_item", "description":
        "Append a todo item to the project's sprint checklist. Use when starting work on a "
        "new version so the next session sees what's in flight. Optional: group items under "
        "a named objective with 'group'; attribute to a person with 'human_id'. "
        "Use 'depends_on' to block until another item finishes.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"},
         "version": {"type": "string"},
         "title": {"type": "string"},
         "group": {"type": "string", "description": "Optional objective name for grouping."},
         "human_id": {"type": "string", "description": "Optional: person this item is assigned to."},
         "depends_on": {"type": "string", "description": "Sprint item id that must complete first."},
         "failure_mode": {"type": "string", "enum": ["continue", "stop"],
                          "description": "'stop' blocks this item if the parent fails."},
         "milestone_type": {"type": "string", "enum": ["task", "milestone", "human"],
                            "description": "'milestone' renders as a timeline marker; 'human' marks a task for a human (hidden from executor sessions)."}},
         "required": ["project_id", "version", "title"]}},
    {"name": "update_sprint_item", "description":
        "Edit fields on an existing sprint item: title, version, notes, human_id (assignee), "
        "or group. Only the fields you pass are changed; omitted fields are left untouched. "
        "Pass an empty string for human_id or group to clear it. Returns the updated item, "
        "or an error if the id is unknown.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"},
         "item_id": {"type": "string"},
         "title": {"type": "string", "description": "New title."},
         "version": {"type": "string", "description": "Move the item to a different version/sprint bucket."},
         "notes": {"type": "string", "description": "Free-form note/context shown on the item."},
         "human_id": {"type": "string", "description": "Reassign to a person (assignee); empty string clears it."},
         "group": {"type": "string", "description": "Objective name to group the item under (item_group); empty string clears it."}},
         "required": ["project_id", "item_id"]}},
    {"name": "complete_sprint_item", "description":
        "Mark a sprint item done. Pass task_id to link the task that shipped it. "
        "Pass session_id to get a board_change field (items injected mid-run) and an "
        "active-worktree merge reminder in the response.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"},
         "item_id": {"type": "string"},
         "task_id": {"type": "string"},
         "session_id": {"type": "string", "description": "Optional: include board_change + worktree merge reminder."}},
         "required": ["project_id", "item_id"]}},
    {"name": "reconcile_sprint_drift", "description":
        "Read-only: Cross-reference pending sprint items against recent git commits and "
        "return items that may already be done. Uses keyword matching — confidence 'high' "
        "means 3+ keywords overlap (safe to mark done), 'medium' means 1-2 (verify first). "
        "Call during planning sessions to identify board drift before filing new items.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"}},
         "required": ["project_id"]}},
    {"name": "get_planning_brief", "description":
        "Read-only: Return a compact planning context — sprint, north star, pending items, "
        "in-progress items, recent tasks, active sessions, recent decisions, and pending HITLs. "
        "No session registration needed. Designed for planning chat sessions that need to see "
        "project state without side effects.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"}},
         "required": ["project_id"]}},
    {"name": "get_sprint_items", "description":
        "Read-only: List sprint items for a project. Optional status filter "
        "(todo|pending|in_progress|provisional_complete|done|failed|skipped|pushed|indeterminate). "
        "Cold sessions read this to know what's still owed.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"},
         "status": {"type": "string",
                    "enum": ["pending", "todo", "in_progress", "provisional_complete",
                             "done", "failed", "skipped", "pushed", "indeterminate"],
                    "description": "Filter by status."}},
         "required": ["project_id"]}},
    {"name": "get_session_log", "description":
        "Read-only: Return the full task log for the given session. "
        "Returns every log_task description logged during the session, "
        "with timestamps. Useful for post-session review or handoff.",
     "inputSchema": {"type": "object", "properties": {
         "session_id": {"type": "string"}},
         "required": ["session_id"]}},
    {"name": "search_all", "description":
        "Read-only: Universal search across all project content: tasks, notes, pinned decisions, "
        "and sprint items. Uses LIKE matching (SQLite) or ILIKE (Postgres). "
        "Returns grouped results: {tasks, notes, decisions, sprint_items, total}.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"},
         "query": {"type": "string"},
         "limit": {"type": "integer", "description": "Max results per type (default 10)."}},
         "required": ["project_id", "query"]}},
    {"name": "get_agent_instructions", "description":
        "Read-only: Return the custom agent_instructions for a project. "
        "These are injected automatically by start_session so every session picks them up. "
        "Use this when you need to read or display the current instructions.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"}},
         "required": ["project_id"]}},
    {"name": "set_agent_instructions", "description":
        "Set or update the custom agent_instructions for a project. "
        "Instructions are injected into every start_session response so AI sessions see them "
        "automatically — no need to repeat in every session. "
        "Pass null or empty string to clear. "
        "Use for persistent rules like coding conventions, deploy steps, or codebase notes.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"},
         "instructions": {"type": "string", "description": "Markdown text injected at session start. Pass null to clear."}},
         "required": ["project_id", "instructions"]}},
    {"name": "set_executor_config", "description":
        "Store per-project executor defaults (repo_path, env_file, test_cmd, test_min, deploy_cmd, shell_type, branch). "
        "Merges onto the existing config — other keys (hostnames, filesystem_roots, …) are preserved. "
        "Pass repo_paths as an array of {cwd, hostname} known locations; they are merged into the existing "
        "repo_paths (deduped) rather than overwriting, so manual + hook-registered entries coexist. "
        "Executor sessions auto-load these when start_session(role='executor') is used. "
        "Credentials rule is always injected separately: read secrets from env_file only, never remote shell.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"},
         "repo_path": {"type": "string"},
         "repo_paths": {"type": "array", "items": {"type": "object", "properties": {
             "cwd": {"type": "string"}, "hostname": {"type": "string"}}},
             "description": "Known locations [{cwd, hostname}] — merged into existing repo_paths, not overwritten."},
         "env_file": {"type": "string"},
         "test_cmd": {"type": "string"},
         "test_min": {"type": "integer"},
         "deploy_cmd": {"type": "string"},
         "shell_type": {"type": "string"},
         "branch": {"type": "string"}},
         "required": ["project_id"]}},
    {"name": "claim_file", "description":
        "Claim edit rights on a file for this session. Whole-file by default "
        "(auto-expires after 2 hours). For symbol-level claims — so two sessions "
        "can edit the same file if they own different classes/functions — also "
        "pass `symbol` (e.g. 'AuthRouter' or 'AuthRouter.login') AND `content` "
        "(the file's full source). Meridian parses the source (stdlib ast for "
        "Python, tree-sitter for JS/TS/C/C++/Go/Rust/Java/C#), and hard-blocks if "
        "another live session already owns an overlapping line range — the block "
        "lists which symbols are still safe to claim. Unparseable content falls "
        "back to a whole-file lock.",
     "inputSchema": {"type": "object", "properties": {
         "session_id": {"type": "string"},
         "file_path": {"type": "string"},
         "symbol": {"type": "string", "description": "Optional symbol to claim (class/function/method name, e.g. 'AuthRouter' or 'AuthRouter.login'). Requires `content`."},
         "content": {"type": "string", "description": "Full source of the file, required when `symbol` is given so the server can resolve the symbol's line range."}},
         "required": ["session_id", "file_path"]}},
    {"name": "release_file", "description":
        "Release a file lock (and any symbol claims this session holds on it).",
     "inputSchema": {"type": "object", "properties": {
         "session_id": {"type": "string"},
         "file_path": {"type": "string"}},
         "required": ["session_id", "file_path"]}},
    {"name": "get_file_claims", "description":
        "Read-only: show active claims on a file — the whole-file lock (with the "
        "holder's session name, if any) plus any symbol-level claims. Use to check "
        "who owns a file before editing it.",
     "inputSchema": {"type": "object", "properties": {
         "file_path": {"type": "string"}},
         "required": ["file_path"]}},
    {"name": "get_symbol_claims", "description":
        "Read-only: list symbol-level claims on a file (who owns which class/function/method line ranges).",
     "inputSchema": {"type": "object", "properties": {
         "file_path": {"type": "string"}},
         "required": ["file_path"]}},
    {"name": "get_symbol_hotspots", "description":
        "Read-only: symbols claimed by 3+ distinct sessions within 14 days — a refactor/ownership smell. Optionally scope to one file.",
     "inputSchema": {"type": "object", "properties": {
         "file_path": {"type": "string"},
         "min_sessions": {"type": "integer"},
         "days": {"type": "integer"}},
         "required": []}},
    {"name": "idle_until_session_done", "description":
        "Read-only: Poll every 30 seconds until another session is closed or archived. Use this when you need to wait before editing a locked file.",
     "inputSchema": {"type": "object", "properties": {
         "watching_session_id": {"type": "string"}},
         "required": ["watching_session_id"]}},
    {"name": "update_md_section", "description":
        "Propose a replacement for an anchored section of an agent template doc "
        "(CLAUDE.md or AGENTS.md). Does NOT write the file directly — it creates a "
        "human-in-the-loop request carrying a diff preview. A human approves it in "
        "the dashboard, then Meridian replaces that section and stages the file "
        "for the next checkpoint commit. 'anchor' is the section name between the "
        "MERIDIAN:ANCHOR:START/END comments. (ROADMAP/DECISIONS/DEVLOG are "
        "append-only and not replaceable.) Pass force=true from a human planning "
        "session (claude.ai) to skip the HITL and apply the replacement directly; "
        "autonomous executor sessions should omit force so the diff stays gated.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"},
         "file": {"type": "string", "description": "CLAUDE.md | AGENTS.md"},
         "anchor": {"type": "string"},
         "content": {"type": "string", "description": "Full proposed body for the section."},
         "session_id": {"type": "string"},
         "force": {"type": "boolean", "description": "Human planning sessions pass true to apply directly without HITL. Default false."},
         "urgency": {"type": "string", "enum": ["normal", "high", "blocking"]}},
         "required": ["project_id", "file", "anchor", "content"]}},
    {"name": "claim_sprint_item",
     "description": "Claim a pending sprint item: sets status to in_progress and records claimed_at. Read-only: false. Rejects if the item is already in_progress, done, failed, skipped, or its touches_files overlap active file claims from another live session.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"},
         "item_id": {"type": "string"},
         "session_id": {"type": "string", "description": "Optional caller session id; its own file claims are ignored for conflict checks."}},
         "required": ["project_id", "item_id"]}},
    {"name": "add_subtask",
     "description": "Add a child sprint item under an existing parent item. Inherits the parent's version. Status starts as pending. Rejects if the parent is already done, failed, or skipped.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"},
         "parent_id": {"type": "string", "description": "ID of the parent sprint item."},
         "title": {"type": "string", "description": "Title of the new subtask."}},
         "required": ["project_id", "parent_id", "title"]}},
    {"name": "split_sprint_item",
     "description": "Split a sprint item into multiple smaller items. The original is closed (skipped) and N new items are created with split_from referencing the original. Returns list of new items.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"},
         "item_id": {"type": "string", "description": "ID of the item to split."},
         "titles": {"type": "array", "items": {"type": "string"},
                    "description": "Titles for the new items (minimum 2)."}},
         "required": ["project_id", "item_id", "titles"]}},
    {"name": "merge_sprint_items",
     "description": "Merge multiple sprint items into one. Source items are closed (skipped, merged_into=survivor). Returns the new survivor item.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"},
         "item_ids": {"type": "array", "items": {"type": "string"},
                      "description": "IDs of items to merge (minimum 2)."},
         "new_title": {"type": "string", "description": "Title for the merged survivor item."}},
         "required": ["project_id", "item_ids", "new_title"]}},
]

_READ_ONLY_TOOLS = {
    "list_projects", "get_project_by_name", "get_goal", "get_notes", "read_note",
    "get_pinned_decisions", "get_tasks", "search_tasks", "search_all",
    "get_session_brief", "get_context_block", "get_hitl_request",
    "list_hitl_requests", "list_sessions", "get_sprint_notes",
    "get_session_log", "idle_until_session_done", "generate_handoff",
    "get_workspace_notes", "get_workspace_decisions", "get_workspace_settings",
    "get_sprint_items", "get_sprint_progress", "get_agent_instructions",
    "reconcile_sprint_drift", "get_planning_brief", "get_file_claims",
}
_DESTRUCTIVE_TOOLS = {"delete_note", "archive_decision", "dismiss_hitl"}

_TITLE_OVERRIDES: dict[str, str] = {
    "request_hitl": "Request HITL",
    "get_hitl_request": "Get HITL Request",
    "list_hitl_requests": "List HITL Requests",
    "answer_hitl": "Answer HITL",
    "dismiss_hitl": "Dismiss HITL",
    "update_md_section": "Update Markdown Section",
    "add_sprint_note": "Add Sprint Note",
    "get_sprint_notes": "Get Sprint Notes",
    "get_sprint_items": "Get Sprint Items",
    "get_sprint_progress": "Get Sprint Progress",
    "add_sprint_item": "Add Sprint Item",
    "update_sprint_item": "Update Sprint Item",
    "complete_sprint_item": "Complete Sprint Item",
    "claim_sprint_item": "Claim Sprint Item",
    "merge_sprint_items": "Merge Sprint Items",
    "split_sprint_item": "Split Sprint Item",
    "add_subtask": "Add Subtask",
    "get_context_block": "Get Context Block",
    "get_session_brief": "Get Session Brief",
    "get_session_log": "Get Session Log",
    "get_pinned_decisions": "Get Pinned Decisions",
    "pin_decision": "Pin Decision",
    "update_decision": "Update Decision",
    "archive_decision": "Archive Decision",
    "get_workspace_decisions": "Get Workspace Decisions",
    "pin_workspace_decision": "Pin Workspace Decision",
    "get_workspace_notes": "Get Workspace Notes",
    "add_workspace_note": "Add Workspace Note",
    "get_workspace_settings": "Get Workspace Settings",
    "update_workspace_settings": "Update Workspace Settings",
    "set_executor_config": "Set Executor Config",
    "set_north_star": "Set North Star",
    "idle_until_session_done": "Wait for Session to Close",
    "generate_handoff": "Generate Handoff",
    "get_agent_instructions": "Get Agent Instructions",
    "set_agent_instructions": "Set Agent Instructions",
    "reconcile_sprint_drift": "Reconcile Sprint Drift",
    "get_planning_brief": "Get Planning Brief",
    "get_file_claims": "Get File Claims",
}

for _tool in _MCP_TOOLS_LIST:
    _is_read_only = _tool["name"] in _READ_ONLY_TOOLS
    _is_destructive = _tool["name"] in _DESTRUCTIVE_TOOLS
    _title = _TITLE_OVERRIDES.get(_tool["name"]) or _tool["name"].replace("_", " ").title()
    _tool["title"] = _title
    _tool["annotations"] = {
        "title": _title,
        "readOnlyHint": _is_read_only,
        "destructiveHint": _is_destructive,
        "openWorldHint": False,
        "idempotentHint": _is_read_only,
    }
