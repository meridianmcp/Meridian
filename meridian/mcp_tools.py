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
    "set_north_star": 'set_north_star(project_id="abc-123", north_star="Ship by Q3")',
    "pin_decision": 'pin_decision(project_id="abc-123", title="Use psycopg3", body="asyncpg has DLL issues on Windows", category="TECHNICAL")',
    "get_pinned_decisions": 'get_pinned_decisions(project_id="abc-123")',
    "generate_handoff": 'generate_handoff(project_id="abc-123", mode="delta", session_id="session-uuid")',
    "get_session_brief": 'get_session_brief(project_id="abc-123")',
    "delete_decision": 'delete_decision(decision_id="decision-uuid")',
    "checkpoint": 'checkpoint(session_id="session-uuid", project_id="abc-123")',
    "request_hitl": 'request_hitl(project_id="abc-123", question="Should we add rate limiting here?", urgency="normal")',
    "get_hitl_request": 'get_hitl_request(request_id="hitl-uuid")',
    "add_note": 'add_note(project_id="abc-123", title="Deploy note", body="Reminder: update env vars before deploy", tags="ops,deploy")',
    "get_notes": 'get_notes(project_id="abc-123")',
    "add_workspace_note": 'add_workspace_note(title="Onboarding", body="All repos use pixi", tags="setup")',
    "get_workspace_notes": 'get_workspace_notes(tag="setup")',
    "pin_workspace_decision": 'pin_workspace_decision(title="Monorepo", body="One repo for all services", category="ARCHITECTURAL")',
    "get_workspace_decisions": 'get_workspace_decisions()',
    "add_sprint_item": 'add_sprint_item(project_id="abc-123", title="Add OAuth login", item_group="auth")',
    "get_sprint_items": 'get_sprint_items(project_id="abc-123")',
    "complete_sprint_item": 'complete_sprint_item(item_id="item-uuid")',
    "heartbeat": 'heartbeat(session_id="session-uuid")',
    "list_projects": 'list_projects()',
    "get_sessions": 'get_sessions(project_id="abc-123")',
    "set_executor_config": 'set_executor_config(project_id="abc-123", repo_path="/repo", env_file="/repo/.env", test_cmd="pixi run test", test_min=619, deploy_cmd="git push", shell_type="powershell", branch="dev")',
    "claim_file": 'claim_file(session_id="session-uuid", file_path="meridian/server.py")',
    "release_file": 'release_file(session_id="session-uuid", file_path="meridian/server.py")',
    "idle_until_session_done": 'idle_until_session_done(watching_session_id="session-uuid")',
}


_MCP_TOOLS_LIST: list[dict[str, Any]] = [
    {"name": "create_project", "description": "Create a new Meridian project.",
     "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "register_session", "description": "Register this Claude session. Call at session start.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"}, "session_name": {"type": "string"},
         "human_id": {"type": "string"},
         "client": {"type": "string", "enum": ["claude-code", "claude-desktop", "cursor", "other"]}},
         "required": ["project_id", "session_name"]}},
    {"name": "start_session", "description": "Register session and return goal + recent tasks in one call.",
     "inputSchema": {"type": "object", "properties": {
          "project_id": {"type": "string"}, "session_name": {"type": "string"},
          "human_id": {"type": "string"},
          "client": {"type": "string", "enum": ["claude-code", "claude-desktop", "cursor", "other"]},
          "role": {"type": "string", "enum": ["executor"], "description": "Pass 'executor' to inject executor_config and credentials guidance."}},
          "required": ["project_id", "session_name"]}},
    {"name": "list_projects", "description":
        "Call first when project_id is unknown. Returns [{id, name, sprint, created_at}] newest first.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "get_project_by_name", "description":
        "Look up a project by name (case-insensitive substring match). Returns the first hit with id, name, and sprint.",
     "inputSchema": {"type": "object", "properties": {
         "name": {"type": "string"}},
         "required": ["name"]}},
    {"name": "get_goal", "description": "Read the current goal state.",
     "inputSchema": {"type": "object", "properties": {"project_id": {"type": "string"}}, "required": ["project_id"]}},
    {"name": "set_goal", "description": "Set or update the goal state.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"}, "content": {"type": "string"}}, "required": ["project_id", "content"]}},
    {"name": "log_task", "description": "Log a task this session completed or is working on. Valid statuses: pending, in_progress, done, failed, backlog, future, backburner.",
     "inputSchema": {"type": "object", "properties": {
         "session_id": {"type": "string"}, "project_id": {"type": "string"},
         "description": {"type": "string"}, "status": {"type": "string"}},
         "required": ["session_id", "project_id", "description"]}},
    {"name": "get_tasks", "description": "Get recent tasks across all sessions.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["project_id"]}},
    {"name": "search_tasks", "description": "Search tasks by keyword or natural-language query. Uses trigram similarity on Postgres, LIKE on SQLite. Returns top matches with similarity score.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"}, "query": {"type": "string"}, "limit": {"type": "integer"}},
         "required": ["project_id", "query"]}},
    {"name": "generate_handoff", "description":
        "Generate a context handoff. mode='full' writes the complete L0/L1/L2 handoff; "
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
        "Return a compact plain-text project context block (north star, sprint, "
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
        "List pinned decisions (active only by default, newest first).",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"},
         "include_superseded": {"type": "boolean"}},
         "required": ["project_id"]}},
    {"name": "delete_decision", "description":
        "Hard-delete a pinned decision by id. Use when something was filed by mistake or "
        "is a duplicate. For retiring a valid but superseded decision, use update_decision "
        "(status=superseded) instead to preserve the audit trail.",
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
        "assigned_to routes to a specific human_id (null = broadcast).",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"},
         "question": {"type": "string"},
         "session_id": {"type": "string"},
         "context": {"type": "string"},
         "urgency": {"type": "string", "enum": ["normal", "high", "blocking"]},
         "assigned_to": {"type": "string"}},
         "required": ["project_id", "question"]}},
    {"name": "get_hitl_request", "description":
        "Poll a HITL request for the human's answer. Returns the row including "
        "status ('pending'|'answered'|'dismissed') and answer text.",
     "inputSchema": {"type": "object", "properties": {
         "request_id": {"type": "string"}},
         "required": ["request_id"]}},
    {"name": "add_note", "description":
        "Add a per-project wiki note (setup, gotcha, howto, env, ...). "
        "Free-form title/body; comma-separated tags optional. Tag a note "
        "'roadmap' AND pass a committable category (TECHNICAL/ARCHITECTURAL/"
        "PRODUCT) to also append it to ROADMAP.md's roadmap-notes anchor.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"},
         "title": {"type": "string"},
         "body": {"type": "string"},
         "tags": {"type": "string"},
         "category": {"type": "string"}},
         "required": ["project_id", "title", "body"]}},
    {"name": "get_notes", "description":
        "List project notes (newest first). Optional ?tag substring filter.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"},
         "tag": {"type": "string"}},
         "required": ["project_id"]}},
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
        "List workspace-level notes (newest first). Optional ?tag substring filter.",
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
        "List workspace-level pinned decisions (active only by default, newest first).",
     "inputSchema": {"type": "object", "properties": {
         "include_superseded": {"type": "boolean"}},
         "required": []}},
    {"name": "get_session_brief", "description":
        "Single-call session orientation - returns sprint focus, pending sprint items, "
        "recent tasks, any blocking failures, and pending HITL requests in a compact "
        "XML envelope (<500 tokens). Replaces the start_session + get_context_block "
        "two-call pattern for worker/automation sessions.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"},
         "role": {"type": "string", "enum": ["worker", "planner", "review"],
                  "description": "Controls verbosity. 'worker'=sprint+tasks only, 'planner'=full context."}},
         "required": ["project_id"]}},
    {"name": "list_hitl_requests", "description":
        "List HITL requests for a project without needing UUIDs. Returns pending queue "
        "by default; pass status='all' to see answered/dismissed items too. "
        "Essential for planning chat to see what needs a human decision.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"},
         "status": {"type": "string",
                    "description": "Filter: 'pending' (default), 'answered', 'dismissed', or 'all'."},
         "limit": {"type": "integer", "description": "Max results, default 50."}},
         "required": ["project_id"]}},
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
        "List active sessions for a project. Useful for planning chat to see "
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
        "Get all ephemeral scratch-pad notes for the current session. "
        "Shown at the top of session briefs so every cold start sees active constraints.",
     "inputSchema": {"type": "object", "properties": {
         "session_id": {"type": "string"}},
         "required": ["session_id"]}},
    {"name": "get_run_transcript", "description":
        "Return the full transcript of the executor_run for the given session. "
        "The transcript accumulates every log_task description logged during the run, "
        "with timestamps. Useful for post-session review or handoff.",
     "inputSchema": {"type": "object", "properties": {
         "session_id": {"type": "string"}},
         "required": ["session_id"]}},
    {"name": "search_all", "description":
        "Universal search across all project content: tasks, notes, pinned decisions, "
        "and sprint items. Uses LIKE matching (SQLite) or ILIKE (Postgres). "
        "Returns grouped results: {tasks, notes, decisions, sprint_items, total}.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"},
         "query": {"type": "string"},
         "limit": {"type": "integer", "description": "Max results per type (default 10)."}},
         "required": ["project_id", "query"]}},
    {"name": "set_executor_config", "description":
        "Store per-project executor defaults (repo_path, env_file, test_cmd, test_min, deploy_cmd, shell_type, branch). "
        "Executor sessions auto-load these when start_session(role='executor') is used. "
        "Credentials rule is always injected separately: read secrets from env_file only, never remote shell.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"},
         "repo_path": {"type": "string"},
         "env_file": {"type": "string"},
         "test_cmd": {"type": "string"},
         "test_min": {"type": "integer"},
         "deploy_cmd": {"type": "string"},
         "shell_type": {"type": "string"},
         "branch": {"type": "string"}},
         "required": ["project_id"]}},
    {"name": "claim_file", "description":
        "Claim exclusive edit rights on a file path for this session. Locks auto-expire after 2 hours.",
     "inputSchema": {"type": "object", "properties": {
         "session_id": {"type": "string"},
         "file_path": {"type": "string"}},
         "required": ["session_id", "file_path"]}},
    {"name": "release_file", "description":
        "Release a file lock held by this session.",
     "inputSchema": {"type": "object", "properties": {
         "session_id": {"type": "string"},
         "file_path": {"type": "string"}},
         "required": ["session_id", "file_path"]}},
    {"name": "idle_until_session_done", "description":
        "Poll every 30 seconds until another session is closed or archived. Use this when you need to wait before editing a locked file.",
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
        "append-only and not replaceable.)",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"},
         "file": {"type": "string", "description": "CLAUDE.md | AGENTS.md"},
         "anchor": {"type": "string"},
         "content": {"type": "string", "description": "Full proposed body for the section."},
         "session_id": {"type": "string"},
         "urgency": {"type": "string", "enum": ["normal", "high", "blocking"]}},
         "required": ["project_id", "file", "anchor", "content"]}},
]

_READ_ONLY_TOOLS = {
    "list_projects", "get_project_by_name", "get_goal", "get_notes",
    "get_pinned_decisions", "get_tasks", "search_tasks", "search_all",
    "get_session_brief", "get_context_block", "get_hitl_request",
    "list_hitl_requests", "list_sessions", "get_sprint_notes",
    "get_run_transcript", "idle_until_session_done", "generate_handoff",
    "get_workspace_notes", "get_workspace_decisions",
}
_DESTRUCTIVE_TOOLS = {"delete_note", "delete_decision", "dismiss_hitl"}

for _tool in _MCP_TOOLS_LIST:
    _is_read_only = _tool["name"] in _READ_ONLY_TOOLS
    _is_destructive = _tool["name"] in _DESTRUCTIVE_TOOLS
    _tool["annotations"] = {
        "readOnlyHint": _is_read_only,
        "destructiveHint": _is_destructive,
        "openWorldHint": False,
        "idempotentHint": _is_read_only,
    }
