"""Shared MCP tool metadata for the Meridian server."""

from __future__ import annotations

from typing import Any


_TOOL_EXAMPLES: dict[str, str] = {
    "create_project": 'create_project(name="my-app")',
    "set_parent_project": 'set_parent_project(project_name="ms-thesis", parent_project_name="Camerer_MS_Graduation_2026")',
    "rename_project": 'rename_project(project_name="old-name", new_name="new-name")',
    "start_session": 'start_session(project_name="my-project", session_name="feature-x", human_id="alice", role="executor")',
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
    "ingest_document": 'ingest_document(project_id="abc-123", file_path="docs/spec.docx", tags="spec")  # or, for a PDF: ingest_document(project_id="abc-123", content="<text you extracted>", title="Q3 report", source="https://example.com/q3.pdf")',
    "get_document_structure": 'get_document_structure(file_path="thesis/chapter1.docx")',
    "get_latex_structure": 'get_latex_structure(file_path="thesis/chapter1.tex")',
    "get_citation_edges": 'get_citation_edges(project_id="abc-123", source="thesis/chapter1.tex")',
    "resolve_citations": 'resolve_citations(project_id="abc-123")',
    "add_sprint_item_pointer": 'add_sprint_item_pointer(project_id="abc-123", sprint_item_id="item-uuid", source_type="code", targets=[{"uri": "meridian/server.py", "selector": {"type": "symbol", "qualified_name": "meridian.server.mcp_tools_doc"}}], label="the tool-doc generator")',
    "get_sprint_item_pointers": 'get_sprint_item_pointers(project_id="abc-123", sprint_item_id="item-uuid")',
    "resolve_sprint_item_pointers": 'resolve_sprint_item_pointers(project_id="abc-123", sprint_item_id="item-uuid")',
    "delete_sprint_item_pointer": 'delete_sprint_item_pointer(pointer_id="pointer-uuid")',
    "get_notes": 'get_notes(project_id="abc-123")',
    "read_note": 'read_note(project_id="abc-123", slug="deploy-note")',
    "add_workspace_note": 'add_workspace_note(title="Onboarding", body="All repos use pixi", tags="setup")',
    "get_workspace_notes": 'get_workspace_notes(tag="setup")',
    "pin_workspace_decision": 'pin_workspace_decision(title="Monorepo", body="One repo for all services", category="ARCHITECTURAL")',
    "get_workspace_decisions": 'get_workspace_decisions()',
    "get_workspace_settings": 'get_workspace_settings()',
    "update_workspace_settings": 'update_workspace_settings(hitl_auto_answer_default=True, sprint_name_default="june-sprint")',
    "save_blog_post": 'save_blog_post(title="Shipping the Blog tab", body="# What changed\\n...", status="published")',
    "get_blog_posts": 'get_blog_posts(status="published")',
    "add_sprint_item": 'add_sprint_item(project_id="abc-123", title="Add OAuth login", item_group="auth")',
    "fan_out_sprint_items": 'fan_out_sprint_items(project_id="abc-123", items=[{"title": "Design DB schema", "group": "backend"}, {"title": "Build API endpoints", "group": "backend"}, {"title": "Wire up frontend", "group": "frontend"}])',
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
    "set_active_repo": 'set_active_repo(repo_path="C:\\\\Users\\\\me\\\\project")',
}


_MCP_TOOLS_LIST: list[dict[str, Any]] = [
    {"name": "create_project", "description": "Create a new Meridian project.",
     "inputSchema": {"type": "object", "properties": {
         "name": {"type": "string"},
         "execution_mode": {"type": "string", "enum": ["autonomous", "interactive"], "description": "Executor posture for sessions on this project. 'autonomous' (default) claims and runs sprint items immediately without asking; 'interactive' asks for direction first. Editable later in dashboard Settings."},
         "parent_project_id": {"type": "string", "description": "Optional parent project id — makes this a subproject that inherits the parent's north_star when it has none of its own. Subprojects are one level deep: the parent must exist and must not itself be a subproject."}},
         "required": ["name"]}},
    {"name": "set_parent_project", "description":
        "7acb8563 — set, change, or clear a project's parent AFTER creation "
        "(create_project only accepted parent_project_id at creation time). Use this "
        "to retroactively nest a project under another, or to detach it. Enforces the "
        "one-level-deep invariant (3b6ff466): the parent must exist and be top-level, "
        "a project can't be its own parent, and a project that already has subprojects "
        "can't become one. Omit parent (or pass empty) to DETACH — make it top-level. "
        "Returns the updated project; {error} on an invariant violation.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"},
         "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "parent_project_id": {"type": "string", "description": "The parent project's id. Omit or leave empty to DETACH (make the project top-level)."},
         "parent_project_name": {"type": "string", "description": "The parent project's name — an alternative to parent_project_id; resolved to an id internally."}},
         "required": []}},
    {"name": "rename_project", "description":
        "7acb8563 — rename a project. Returns the updated project, or {error} if it "
        "does not exist.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"},
         "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "new_name": {"type": "string", "description": "The new project name."}},
         "required": ["new_name"]}},
    {"name": "register_session", "description": "Low-level: register this session without loading goal context. Use start_session instead for executor/human sessions — it registers AND returns goal + tasks in one call. Use register_session when you only need a session ID and will fetch context separately.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"}, "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."}, "session_name": {"type": "string"},
         "human_id": {"type": "string"},
         "client": {"type": "string", "enum": ["claude-code", "claude-desktop", "cursor", "other"]}},
         "required": ["session_name"]}},
    {"name": "start_session", "description": "Register a session and return orientation. Compact by default (session_id, sprint focus + status counts, 3 recent tasks, board_change count) to keep an executor's context small. Pass compact=false for the full block (goal XML, decisions, MERIDIAN.md instructions, workspace context, sprint items) — or fetch it later with get_session_brief. Pass version to scope the session to one sprint-version bucket (e.g. 'v0.1.x'): the orientation's sprint counts/items filter to it and the scope is remembered for the /goal template. Omit version to auto-scope to the bucket with the most pending items (empty board → unscoped).",
     "inputSchema": {"type": "object", "properties": {
          "project_id": {"type": "string"}, "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."}, "session_name": {"type": "string", "description": "Optional (599d0097): omit or leave blank to auto-generate a meaningful name from the first pending sprint item title + a timestamp, instead of inventing a string."},
          "human_id": {"type": "string"},
          "client": {"type": "string", "enum": ["claude-code", "claude-desktop", "cursor", "other"]},
          "role": {"type": "string", "enum": ["executor"], "description": "Pass 'executor' to inject executor_config and credentials guidance."},
          "compact": {"type": "boolean", "description": "Default true — slim orientation. Set false for the full goal/instructions payload."},
          "version": {"type": "string", "description": "Optional sprint-version bucket (e.g. 'v0.1.x') to scope this session to. Sprint progress/items in the orientation and /goal filter to it. Omit to auto-infer the bucket with the most pending items."},
          "mode": {"type": "string", "enum": ["continue"], "description": "Pass 'continue' to resume an already-active same-name session WITHOUT re-reading the full L0/L1/L2 orientation: returns just session_id + live pending items + the ready-to-paste /goal string. Auto-detected anyway within a 5-min heartbeat window; 'continue' widens that so a known-yours session resumes cleanly even after a longer gap."}},
          "required": []}},
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
     "inputSchema": {"type": "object", "properties": {"project_id": {"type": "string"}, "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."}}, "required": []}},
    {"name": "set_goal", "description": "Set or update the goal state.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"}, "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."}, "content": {"type": "string"}}, "required": ["content"]}},
    {"name": "set_north_star", "description": "Update only the north star — the long-lived product vision that rarely changes. Distinct from the version goal (set_goal). Any team member can call this.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"}, "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "north_star": {"type": "string"}},
         "required": ["north_star"]}},
    {"name": "log_task", "description": "Log a task this session completed or is working on. Valid statuses: pending, in_progress, done, failed, backlog, future, backburner.",
     "inputSchema": {"type": "object", "properties": {
         "session_id": {"type": "string"}, "project_id": {"type": "string"}, "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "description": {"type": "string"}, "status": {"type": "string"},
         "kind": {"type": "string", "enum": ["shipped", "found", "decided", "blocked"], "description": "Entry taxonomy. shipped=work done, found=discovery, decided=arch choice, blocked=blocker."}},
         "required": ["session_id", "description"]}},
    {"name": "get_tasks", "description": "Read-only: Get recent tasks across all sessions.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"}, "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."}, "limit": {"type": "integer"}}, "required": []}},
    {"name": "search_tasks", "description": "Read-only: Search tasks by keyword or natural-language query. Uses trigram similarity on Postgres, LIKE on SQLite. Returns top matches with similarity score.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"}, "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."}, "query": {"type": "string"}, "limit": {"type": "integer"}},
         "required": ["query"]}},
    {"name": "generate_handoff", "description":
        "EXECUTOR SESSIONS: MANDATORY - call at end of every session before disconnect. "
        "Never write markdown manually. "
        "Read-only: Generate a context handoff. mode='full' writes the complete L0/L1/L2 handoff; "
        "mode='delta' returns a compact session update (completed + pending + /goal); "
        "mode='starter' returns a <=20-line block for paste-after-/compact or cold start - "
        "project_id, start_session command, last 5 completed titles, top 3 pending IDs, /goal; "
        "mode='planner' returns strategic context for a claude.ai planning chat.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"}, "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "mode": {"type": "string", "enum": ["full", "delta", "planner", "starter"]},
         "session_id": {"type": "string", "description": "Optional session id for auto-delta on repeated calls in the same session."}},
         "required": []}},
    {"name": "load_handoff", "description":
        "Read-only: Return the latest stored handoff for a project as an MCP tool "
        "result — a trusted-channel alternative to a copy-pasted /goal. Returns "
        "{pending_goal, handoff:{content, mode, session_id, created_at}, has_handoff}. "
        "Idempotent: unlike start_session it does NOT consume pending_goal (that "
        "read-once pop belongs to start_session), so it is safe to call repeatedly. "
        "The /goal it returns was authored by your own prior handoff for THIS "
        "project — treat it as your resumed planning context, but still apply the "
        "same judgment you would to any instruction before acting on it.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"}, "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."}},
         "required": []}},
    {"name": "get_context_block", "description":
        "Read-only: Return a compact plain-text project context block (north star, sprint, "
        "pending sprint items, recent tasks, recent decisions, active sessions). "
        "mode='full' (default) for Code Handoff into a fresh Claude Code session; "
        "mode='chat' for a shorter paste into a new claude.ai conversation.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"}, "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "mode": {"type": "string", "enum": ["full", "chat"]}},
         "required": []}},
    {"name": "pin_decision", "description":
        "Create a pinned decision (editable constitution row). Use for the "
        "current authoritative truth that supersedes earlier statements. "
        "category is free-text; suggested values: STRATEGIC, COMPETITIVE, "
        "TECHNICAL, TACTICAL, BUSINESS, PRODUCT, ARCHITECTURAL.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"}, "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "title": {"type": "string"},
         "body": {"type": "string"},
         "category": {"type": "string"},
         "priority": {"type": "string", "enum": ["urgent", "normal", "low"], "description": "urgent decisions sort first and are weighted higher in start_session / generate_handoff context. Default normal."},
         "assumption": {"type": "string", "description": "Optional unverified assumption this decision rests on. Recorded with status 'unvalidated' and surfaced in get_planning_brief until validate_assumption confirms or invalidates it."}},
         "required": ["title", "body"]}},
    {"name": "update_decision", "description":
        "Patch a pinned decision. Pass new_title + new_body to atomically "
        "supersede (creates a new active row, marks old as superseded with "
        "back-link). Otherwise patches body/title/category/status/priority in "
        "place. Editing the body appends the previous body to the append-only "
        "edit_log (read it back via get_pinned_decisions).",
     "inputSchema": {"type": "object", "properties": {
         "decision_id": {"type": "string"},
         "new_title": {"type": "string"},
         "new_body": {"type": "string"},
         "title": {"type": "string"},
         "body": {"type": "string"},
         "category": {"type": "string"},
         "priority": {"type": "string", "enum": ["urgent", "normal", "low"], "description": "Change ordering/weight (urgent | normal | low)."},
         "status": {"type": "string"},
         "assumption": {"type": "string", "description": "Set/replace the decision's underlying assumption text."},
         "assumption_status": {"type": "string", "enum": ["unvalidated", "confirmed", "invalidated"], "description": "Stamp the assumption's validation state. Usually set via the validate_assumption tool, which also fires HITL on invalidation."}},
         "required": ["decision_id"]}},
    {"name": "validate_assumption", "description":
        "Confirm or invalidate the assumption a pinned decision rests on, in one "
        "call — no phase switching. Stamps the decision's assumption_status "
        "(confirmed|invalidated), saves a code-anchored note with your finding, "
        "and when confirmed=false fires a BLOCKING HITL so work depending on the "
        "decision pauses for human judgment. Use the moment you discover whether "
        "an assumption holds (a planning-session prospect moment).",
     "inputSchema": {"type": "object", "properties": {
         "decision_id": {"type": "string"},
         "finding": {"type": "string", "description": "What you found that confirms or refutes the assumption."},
         "confirmed": {"type": "boolean", "description": "true = assumption holds; false = invalidated (fires a blocking HITL)."},
         "file_path": {"type": "string", "description": "Optional file path the finding is anchored to (code-anchored note)."},
         "symbol": {"type": "string", "description": "Optional symbol within file_path."},
         "session_id": {"type": "string", "description": "Session firing the validation; linked to the blocking HITL on invalidation."}},
         "required": ["decision_id", "finding", "confirmed"]}},
    {"name": "get_pinned_decisions", "description":
        "Read-only: List pinned decisions, highest priority first (urgent → "
        "normal → low, then newest-first). Active only by default. Each row "
        "includes its priority and a parsed edit_log array of prior bodies "
        "({body, ts}) recorded on every in-place body edit.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"}, "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "include_superseded": {"type": "boolean"}},
         "required": []}},
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
         "project_id": {"type": "string"}, "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."}},
         "required": ["session_id"]}},
    {"name": "request_hitl", "description":
        "Surface a question to the human-in-the-loop queue. ALWAYS use this to ask "
        "the human a question — never just ask in chat, which is invisible to the "
        "dashboard and to an unattended/autonomous run. IMPORTANT: when the "
        "project's HITL auto-answer mode is on (1=safe, 2=aggressive) and the "
        "question is not destructive / not require_human, this tool RESOLVES "
        "IMMEDIATELY and returns the chosen answer inline in the response (it does "
        "NOT block) — so calling it is cheap and is the right move even when you "
        "expect a quick yes/no. The active mode is reported in the start_session "
        "orientation as hitl_auto_answer_mode. "
        "urgency='blocking' "
        "means this session pauses until answered (poll get_hitl_request). "
        "urgency='normal'/'high' lands in the dashboard but doesn't block. "
        "assigned_to routes to a specific human_id (null = broadcast). "
        "kind='correction' files a non-blocking mid-run correction: never "
        "auto-answered, never blocks — an unattended executor picks it up at the "
        "next sprint-item boundary, applies it, and continues. Pass `options` "
        "(answer choices, rendered as buttons) and `recommended` (an option "
        "string or 0-based index) to flag the safe default — the dashboard "
        "highlights it and Enter submits it, and an auto-answer picks it. Set "
        "require_human=true for genuinely irreversible/destructive actions (token "
        "rotation, data migrations, rollbacks) so auto-answer can never approve it "
        "— only an explicit human reply unblocks it.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"}, "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "question": {"type": "string"},
         "session_id": {"type": "string"},
         "context": {"type": "string"},
         "urgency": {"type": "string", "enum": ["normal", "high", "blocking"]},
         "kind": {"type": "string", "enum": ["question", "correction"], "description": "question (default, auto-answerable) or correction (non-blocking mid-run human correction)."},
         "assigned_to": {"type": "string"},
         "options": {"type": "array", "items": {"type": "string"}, "description": "Answer choices rendered as selectable buttons in the dashboard."},
         "recommended": {"description": "The safe-default option — an option string or a 0-based index into options. Highlighted in the dashboard; Enter submits it; auto-answer prefers it."},
         "require_human": {"type": "boolean", "description": "When true, the HITL can never be auto-answered — only an explicit human response unblocks it. Reserve for irreversible/destructive actions."}},
         "required": ["question"]}},
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
        "reference=external/one-off docs, code=warning/context anchored to a "
        "file, document=ingested report/spec/thesis) controls how the dashboard "
        "renders it. For a code anchor pass kind='code' plus file_path (and "
        "optional symbol): the note is then surfaced automatically when a session "
        "calls claim_file/get_file_claims for that path, so the executor sees the "
        "warning before editing. Pass source (a URL or file path) to record where "
        "the note came from — set automatically by ingest_document. "
        "Tag a note 'roadmap' AND pass a committable category (TECHNICAL/"
        "ARCHITECTURAL/PRODUCT) to also append it to ROADMAP.md's roadmap-notes anchor.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"}, "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "title": {"type": "string"},
         "body": {"type": "string"},
         "tags": {"type": "string"},
         "kind": {"type": "string", "enum": ["wiki", "insight", "reference", "code", "document"]},
         "priority": {"type": "string", "enum": ["high", "normal", "low"], "description": "high-priority notes surface first in generate_handoff and planner context."},
         "file_path": {"type": "string", "description": "Code anchor (kind='code'): repo-relative or absolute path this note warns about. Surfaced at claim_file/get_file_claims for the same path."},
         "symbol": {"type": "string", "description": "Optional symbol (class/function/method) to scope the code anchor to. File-level anchors (no symbol) surface for any symbol in the file."},
         "source": {"type": "string", "description": "Provenance: a URL or file path this note was ingested from. Stored on the note (used by kind='document')."},
         "category": {"type": "string"}},
         "required": ["title", "body"]}},
    {"name": "ingest_document", "description":
        "Turn a Word/PDF/text document into a queryable kind='document' note with "
        "a source link — a report, thesis chapter, or spec doc becomes searchable "
        "project memory. Pass file_path OR content (one is required):\n"
        "• file_path → Meridian extracts the text SERVER-SIDE, STDLIB ONLY: .txt/"
        ".md/.markdown and source files are read directly; .docx is unzipped and "
        "its paragraphs extracted (no python-docx). No new dependencies.\n"
        "• content → use this for .pdf and anything Meridian can't parse server-"
        "side: extract the text with YOUR OWN tools first, then pass it here. "
        "(Passing file_path for a .pdf returns an error telling you to do this.)\n"
        "title defaults to the file's basename; source defaults to file_path. The "
        "stored body is capped (truncated with a '…[truncated]' marker if very "
        "long; the kept prefix stays searchable). Meridian never summarizes — pass "
        "a summary as content if you want one stored instead of the raw text. "
        "Returns the created note (id, slug, title, source).",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"}, "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "file_path": {"type": "string", "description": "Path to a .txt/.md/.docx file to extract server-side (stdlib only). For .pdf or other types, pass pre-extracted text as 'content' instead."},
         "content": {"type": "string", "description": "Pre-extracted document text. Use for PDFs and any type Meridian can't parse server-side. Takes precedence over file_path when both are given."},
         "title": {"type": "string", "description": "Note title. Defaults to the file's basename."},
         "source": {"type": "string", "description": "Provenance URL/path stored on the note. Defaults to file_path."},
         "tags": {"type": "string", "description": "Comma-separated tags."}},
         "required": []}},
    {"name": "get_document_structure", "description":
        "13462df2 — return the heading outline of a Word .docx WITHOUT ingesting "
        "it as a note. Meridian parses the .docx server-side (stdlib only, no "
        "python-docx, no persistent index) and returns paragraph_count, "
        "heading_count, and an ordered list of headings (level, text, para_id) — a "
        "fast structural map of a thesis chapter / spec before deciding what to "
        "read or ingest. Pass file_path to a server-accessible .docx.",
     "inputSchema": {"type": "object", "properties": {
         "file_path": {"type": "string", "description": "Path to a server-accessible .docx file."}},
         "required": ["file_path"]}},
    {"name": "get_latex_structure", "description":
        "106118cd — parse a LaTeX (.tex) source's structure WITHOUT a PDF "
        "intermediary. Meridian parses the .tex server-side with pylatexenc "
        "(pure-Python, no LaTeX install) and returns heading_count, an ordered "
        "headings outline and a nested tree of "
        "\\part/\\chapter/\\section/\\subsection/\\subsubsection/\\paragraph "
        "(level, kind, text, children), plus unexpanded_inputs (\\input/\\include "
        "filenames, not expanded) and a bibliography list (thebibliography "
        "\\bibitem entries, and \\bibliography{...} + a sibling .bib when a path "
        "is given). Pass file_path to a server-accessible .tex, OR pass source "
        "with the raw LaTeX inline. Malformed LaTeX returns a partial/empty "
        "result, never an error crash.",
     "inputSchema": {"type": "object", "properties": {
         "file_path": {"type": "string", "description": "Path to a server-accessible .tex file. A sibling .bib referenced by \\bibliography is resolved relative to it."},
         "source": {"type": "string", "description": "Raw LaTeX source, as an alternative to file_path. Ignored when file_path is given."}},
         "required": []}},
    {"name": "get_citation_edges", "description":
        "fefb596a — read the CITATION GRAPH of a project's ingested documents. "
        "Returns every in-text citation marker (a kind='citation' element parsed "
        "from an ingested .tex/.docx) together with its resolved edges:\n"
        "• bibentry edges — the intra-document link from a \\cite{key} marker to a "
        "matching \\bibitem/bibliography entry in the SAME document (materialised "
        "automatically on ingest).\n"
        "• zotero_item edges — the cross-document link from a marker to a canonical "
        "Zotero library item, keyed on DOI (materialised by the opt-in "
        "resolve_citations pass); target_document_id is set when the cited paper "
        "is itself ingested in this project.\n"
        "Each marker carries {element_id, document_id, ordinal, ref, text, edges:"
        "[{edge_kind, target_kind, target_ref, target_element_id, "
        "target_document_id, resolved_at}]}. Scope to one document with source (a "
        "stored source path/URL) or document_id; omit both for the whole project. "
        "Returns an empty markers list (never an error) when no document structure "
        "has been persisted yet.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"},
         "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally."},
         "source": {"type": "string", "description": "Restrict to the document stored under this source (path/URL). Empty graph if the source is unknown."},
         "document_id": {"type": "string", "description": "Restrict to one stored document by its doc_store id."}},
         "required": []}},
    {"name": "resolve_citations", "description":
        "fefb596a — resolve this project's in-text citation markers to canonical "
        "Zotero items via Zotero's LOCAL API and materialise the cross-document "
        "'cites' -> zotero_item edges (keyed on DOI). An OPT-IN, network-making "
        "pass — deliberately separate from ingest, which stays offline. For each "
        "kind='citation' marker without a zotero_item edge, the marker's ref is "
        "resolved: a DOI (doi:.. / a bare 10.x/y / a doi.org URL) matches the "
        "library item with that DOI; a zotero:<key> ref is a direct item lookup; a "
        "bare BibTeX citekey is a best-effort text search (fuzzy without Better "
        "BibTeX). When the resolved DOI matches a paper ALSO ingested in this "
        "project, the edge's target_document_id is linked too. IDEMPOTENT — re-runs "
        "only fill gaps, never duplicate. If Zotero is closed or its local API is "
        "disabled, markers simply stay unresolved (no error). Returns {resolved, "
        "unresolved, cross_doc_linked} counts. Requires Zotero running locally with "
        "the local API enabled (endpoint configurable via MERIDIAN_ZOTERO_API_URL).",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"},
         "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally."},
         "max_items": {"type": "integer", "description": "Cap how many unresolved markers to attempt this pass. Omit to attempt all."}},
         "required": []}},
    {"name": "add_sprint_item_pointer", "description":
        "2976e168 — attach a GENERIC POINTER to a sprint item: a portable, composable "
        "reference to a thing-in-a-source, grounded in LSP Location + W3C Web Annotation "
        "Selector composition. targets is an ARRAY of {uri, selector, subSelector?} "
        "objects (native multi-file, the LSP WorkspaceEdit pattern); the whole composite "
        "shape is stored as JSON, not per-domain columns. Each selector.type is one of:\n"
        "• range — a line span {start_line, start_char?, end_line, end_char?} (an LSP "
        "Range); the pointer IS the location.\n"
        "• symbol — {qualified_name} resolved against the cached code graph to a file+line.\n"
        "• node_id — {id} of a doc_store element (an ingested-document structure node).\n"
        "• zotero_key — {key} of a Zotero library item.\n"
        "An optional selector.subSelector nests finer granularity (W3C hasSubSelector) — "
        "e.g. a symbol selector + a range subSelector = 'these lines, within this "
        "function'. source_type names the domain (code | docs | citation | …). Malformed "
        "pointers (bad selector.type, missing required selector fields) are rejected. "
        "Returns the stored pointer.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"},
         "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "sprint_item_id": {"type": "string", "description": "The sprint item to attach the pointer to."},
         "source_type": {"type": "string", "description": "Domain of the pointer: code | docs | citation | … (free text)."},
         "targets": {"type": "array", "description":
             "Non-empty array of {uri, selector, subSelector?} targets. selector.type ∈ "
             "range|symbol|node_id|zotero_key with its type-specific fields.",
             "items": {"type": "object"}},
         "label": {"type": "string", "description": "Optional human-readable label for the pointer."}},
         "required": ["sprint_item_id", "source_type", "targets"]}},
    {"name": "get_sprint_item_pointers", "description":
        "2976e168 — list the GENERIC POINTERS attached to a sprint item (oldest first). "
        "Each pointer is {id, source_type, targets:[{uri, selector, subSelector?}], label, "
        "created_at} — the stored shape with its JSON targets deserialized. Read-only; "
        "does NOT resolve the targets (use resolve_sprint_item_pointers for that).",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"},
         "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "sprint_item_id": {"type": "string", "description": "The sprint item whose pointers to list."}},
         "required": ["sprint_item_id"]}},
    {"name": "resolve_sprint_item_pointers", "description":
        "2976e168 — resolve EVERY generic pointer on a sprint item to its concrete "
        "location, dispatching by selector.type. A range target returns its location "
        "as-is; symbol resolves the qualified_name in the cached code graph to a "
        "file+line; node_id looks the element up in the doc-structure store; zotero_key "
        "resolves via Zotero's local API. A subSelector narrows the outer resolution "
        "('these lines, within this function'). Every dispatch is best-effort: an "
        "unresolvable target yields {resolved:false, reason} instead of an error, and "
        "the pass NEVER fails. Returns {pointers:[{id, source_type, label, "
        "targets:[<resolved-target>]}]}. Requires no network for range/symbol/node_id; "
        "zotero_key needs Zotero running locally (else that target is just unresolved).",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"},
         "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "sprint_item_id": {"type": "string", "description": "The sprint item whose pointers to resolve."}},
         "required": ["sprint_item_id"]}},
    {"name": "delete_sprint_item_pointer", "description":
        "2976e168 — delete ONE generic pointer from a sprint item by its pointer id "
        "(the id returned by add_sprint_item_pointer / get_sprint_item_pointers). A "
        "stored pointer is immutable, so 'editing' one is delete-then-re-add. "
        "Idempotent: returns {pointer_id, deleted:false} when no pointer had that id, "
        "rather than erroring.",
     "inputSchema": {"type": "object", "properties": {
         "pointer_id": {"type": "string", "description": "The id of the pointer to delete."}},
         "required": ["pointer_id"]}},
    {"name": "add_insight", "description":
        "Record a durable STRATEGIC INSIGHT — accumulated understanding that generates future "
        "decisions. A first-class knowledge type SEPARATE from decisions (choices with a "
        "lifecycle) and notes (reference). horizon sets its shelf-life: 'permanent' insights "
        "ALWAYS surface in get_planning_brief; 'year'/'quarter' are time-boxed. Returns the "
        "stored insight.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"}, "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "title": {"type": "string"},
         "body": {"type": "string", "description": "The insight (markdown)."},
         "horizon": {"type": "string", "enum": ["permanent", "year", "quarter"], "description": "Shelf-life. 'permanent' always appears in the planning brief. Default 'quarter'."},
         "tags": {"type": "array", "items": {"type": "string"}, "description": "Optional tags."}},
         "required": ["title"]}},
    {"name": "get_insights", "description":
        "Read-only: List a project's strategic insights (newest first), optionally filtered by "
        "horizon (permanent|year|quarter). Review accumulated understanding before planning. "
        "permanent insights also appear automatically in get_planning_brief.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"}, "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "horizon": {"type": "string", "enum": ["permanent", "year", "quarter"], "description": "Optional horizon filter."}},
         "required": []}},
    {"name": "save_finding", "description":
        "Phase-agnostic capture primitive: turn a finding into a durable, "
        "addressable note with provenance — works with ANY source (Claude's "
        "built-in web search, the arXiv MCP, Serena, a teammate). Decoupled from "
        "search so capture survives regardless of how you found it. The summary's "
        "first line becomes the note title; the note is tagged 'finding' + the "
        "source_type. Optionally links to a pinned decision. Returns the note.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"}, "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "summary": {"type": "string", "description": "The finding text (markdown). Its first line becomes the note title."},
         "source_url": {"type": "string", "description": "Provenance URL/path stored on the note."},
         "source_type": {"type": "string", "enum": ["web", "arxiv", "code", "conversation"], "description": "Where the finding came from. Default web; unknown values fall back to web."},
         "decision_id": {"type": "string", "description": "Optional pinned-decision id to link this finding to (tagged decision:<id>)."}},
         "required": ["summary"]}},
    {"name": "capture_research_finding", "description":
        "Inline capture for web/paper research during planning: save a finding "
        "from a URL as an addressable note with the source link, optionally linked "
        "to a decision. A research-shaped wrapper over save_finding — arXiv URLs "
        "are tagged source_type=arxiv automatically, everything else as web. Turns "
        "web-search results into durable Meridian artifacts instead of evaporating.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"}, "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "url": {"type": "string", "description": "Source URL of the web page or paper."},
         "summary": {"type": "string", "description": "Your summary of the finding (markdown)."},
         "related_decision_id": {"type": "string", "description": "Optional pinned-decision id to link the finding to."}},
         "required": ["url", "summary"]}},
    {"name": "get_notes", "description":
        "Read-only: List project notes (newest first), LIGHTWEIGHT by default — "
        "each item is id/slug/title/tags/kind/priority/timestamps with NO body, "
        "so the list never overflows context. This is the pull model: scan the "
        "list, then call read_note(project_id, slug) to fetch one note's full "
        "body on demand. Optional ?tag substring filter and ?query full-text "
        "search (matches title+body even though bodies aren't returned). Pass "
        "bodies=true only when you truly need every body inline. Pagination: "
        "pass limit (default 100, max 500) and/or cursor to get a "
        "{notes, has_more, next_cursor} envelope, then re-call with "
        "cursor=next_cursor for the next page; omit both for the full list.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"}, "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "tag": {"type": "string"},
         "query": {"type": "string", "description": "Text search across note title and body (case-insensitive)."},
         "bodies": {"type": "boolean", "description": "Default false. true returns full note bodies inline (legacy behavior) — usually unnecessary; prefer read_note(slug)."},
         "limit": {"type": "integer", "description": "Page size (default 100, clamped 1..500). Passing limit or cursor switches the result to the {notes, has_more, next_cursor} pagination envelope."},
         "cursor": {"type": "integer", "description": "Offset cursor from a prior page's next_cursor. Passing it switches the result to the {notes, has_more, next_cursor} envelope."},
         "sort": {"type": "string", "enum": ["recency", "relevance"], "description": "98890df1 — 'relevance' ranks notes by reference_count/recency/decision-link (heavily cross-referenced notes surface, stale ones sink) and returns a bare list with a per-note 'relevance' score; default 'recency'."}},
         "required": []}},
    {"name": "read_note", "description":
        "Read-only: Fetch one project note's full body by its per-project slug "
        "(the ``slug`` field from get_notes). The pull half of the list→read "
        "model — get_notes returns slugs without bodies, read_note pulls a "
        "single body when you need it.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"}, "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "slug": {"type": "string", "description": "The note's slug (kebab-cased, unique per project) as returned by get_notes."}},
         "required": ["slug"]}},
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
        "execution_mode_default ('autonomous'|'interactive', '' to clear) and "
        "code_intel_enabled_default (bool) are cascade defaults seeded onto NEW "
        "projects in this workspace (existing projects are unchanged). "
        "loop_enabled_default (bool) is the workspace default for /loop auto-continue "
        "that projects inherit when their loop_enabled is 'workspace'. "
        "Pass an empty string to revert a field to the server default.",
     "inputSchema": {"type": "object", "properties": {
         "hitl_auto_answer_default": {"type": "boolean"},
         "sprint_name_default": {"type": "string"},
         "handoff_template": {"type": "string"},
         "execution_mode_default": {"type": "string", "description": "Seed new projects' execution mode: 'autonomous', 'interactive', or '' to clear."},
         "code_intel_enabled_default": {"type": "boolean", "description": "Seed new projects' code-intel toggle."},
         "loop_enabled_default": {"type": "boolean", "description": "Workspace default for /loop auto-continue; projects with loop_enabled='workspace' inherit it. True = sessions auto-continue."}},
         "required": []}},
    {"name": "save_blog_post", "description":
        "Create or update a workspace-scoped blog post (draft|published|archived "
        "lifecycle). Posts belong to the whole workspace, not a single project, and "
        "are served publicly at /blog/<slug> once status='published'. Pass 'id' to "
        "update an existing post; omit it to create a new draft. 'slug' is optional "
        "(auto-derived from the title, de-duplicated). Returns the saved post with a "
        "computed 'url'.",
     "inputSchema": {"type": "object", "properties": {
         "title": {"type": "string"},
         "body": {"type": "string", "description": "Post body (Markdown)."},
         "status": {"type": "string", "enum": ["draft", "published", "archived"], "description": "Lifecycle status. Default 'draft'. 'published' makes it live at /blog/<slug>."},
         "slug": {"type": "string", "description": "Optional URL slug; auto-derived from the title when omitted."},
         "id": {"type": "string", "description": "Optional: id of an existing post to update instead of creating a new one."}},
         "required": ["title"]}},
    {"name": "get_blog_posts", "description":
        "Read-only: List workspace-scoped blog posts, newest first. Optional 'status' "
        "filter (draft|published|archived). Each post includes a 'url' (/blog/<slug>).",
     "inputSchema": {"type": "object", "properties": {
         "status": {"type": "string", "enum": ["draft", "published", "archived"]}},
         "required": []}},
    {"name": "add_workspace_sprint_item", "description":
        "Add an item to the workspace-level personal backlog — a cross-project board NOT "
        "tied to any single project (track thesis + Meridian + personal goals in one view). "
        "Use the per-project add_sprint_item for project work instead. 'group' is the "
        "cross-project bucket the item lives under (e.g. 'thesis', 'meridian', 'personal'); "
        "'human_id' assigns it to a person. New items start as 'todo'.",
     "inputSchema": {"type": "object", "properties": {
         "title": {"type": "string"},
         "group": {"type": "string", "description": "Cross-project bucket, e.g. 'thesis'/'meridian'/'personal'."},
         "human_id": {"type": "string", "description": "Optional: person this item is assigned to."}},
         "required": ["title"]}},
    {"name": "get_workspace_sprint_items", "description":
        "Read-only: List workspace personal-backlog items (grouped by 'group', then position). "
        "Optional 'status' (todo/pending/in_progress/done/skipped/failed) and 'group' filters.",
     "inputSchema": {"type": "object", "properties": {
         "status": {"type": "string", "enum": ["todo", "pending", "in_progress", "done", "skipped", "failed"]},
         "group": {"type": "string", "description": "Filter to a single cross-project bucket."}},
         "required": []}},
    {"name": "update_workspace_sprint_item", "description":
        "Edit a workspace personal-backlog item: title, status, group, or human_id (assignee). "
        "Only the fields you pass are changed. Pass an empty string for group/human_id to clear it. "
        "Setting status to done/skipped/failed stamps completed_at. Returns the updated item.",
     "inputSchema": {"type": "object", "properties": {
         "item_id": {"type": "string"},
         "title": {"type": "string"},
         "status": {"type": "string", "enum": ["todo", "pending", "in_progress", "done", "skipped", "failed"]},
         "group": {"type": "string", "description": "Move the item to a different cross-project bucket; empty string clears it."},
         "human_id": {"type": "string", "description": "Reassign to a person; empty string clears it."}},
         "required": ["item_id"]}},
    {"name": "complete_workspace_sprint_item", "description":
        "Mark a workspace personal-backlog item done (stamps completed_at). Returns the updated item.",
     "inputSchema": {"type": "object", "properties": {
         "item_id": {"type": "string"}},
         "required": ["item_id"]}},
    {"name": "get_session_brief", "description":
        "Read-only: Call this FIRST for project summaries or to see what a session did — "
        "returns session, tasks, decisions, and recent commits in one call. "
        "Replaces the start_session + get_context_block two-call pattern for "
        "worker/automation sessions. Returns sprint focus, pending sprint items, "
        "recent tasks, any blocking failures, and pending HITL requests in a compact "
        "XML envelope (<500 tokens).",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"}, "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "role": {"type": "string", "enum": ["worker", "executor", "planner", "review"],
                  "description": "Tailors the brief. 'worker'=sprint+tasks only; 'executor'=adds version-scoped pending items, this session's file claims, and decisions code-anchored to them (pass session_id); 'planner'=adds full decisions/notes/sessions, last-session summary, and decisions needing revisit."},
         "session_id": {"type": "string", "description": "Caller session id — enables session-scratchpad notes, board-change detection, and (role='executor') file-claim + version scoping."}},
         "required": []}},
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
         "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
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
         "project_id": {"type": "string"}, "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "status": {"type": "string",
                    "description": "Filter by status: 'active' (default), or 'all' for all sessions."}},
         "required": []}},
    {"name": "add_sprint_note", "description":
        "Add an ephemeral note to the current session's scratch pad. "
        "Use for constraints, blockers, working assumptions valid only this session. "
        "Notes are auto-deleted when the session closes. "
        "Pass note_kind='thinking' for a thinking_sync (HOOKS_DEBUG_STATE) note: a "
        "structured snapshot of the reasoning state (what was tried, what failed, "
        "current confirmed state) that the dashboard renders with a distinct icon. "
        "Intended for Claude's client-side thinking_sync post-tool-call hook, which "
        "extracts the extended-thinking scratchpad and persists it here so debugging "
        "state survives across turns and into the next session brief.",
     "inputSchema": {"type": "object", "properties": {
         "session_id": {"type": "string"},
         "title": {"type": "string"},
         "body": {"type": "string"},
         "note_kind": {"type": "string", "enum": ["note", "thinking"],
                       "description": "'note' (default) or 'thinking' for a thinking_sync scratchpad note."}},
         "required": ["session_id", "title", "body"]}},
    {"name": "get_sprint_notes", "description":
        "Read-only: Get all ephemeral scratch-pad notes for the current session. "
        "Shown at the top of session briefs so every cold start sees active constraints. "
        "Pass note_kind='thinking' to fetch only thinking_sync scratchpad notes, or "
        "'note' for only normal notes; omit for all.",
     "inputSchema": {"type": "object", "properties": {
         "session_id": {"type": "string"},
         "note_kind": {"type": "string", "enum": ["note", "thinking"]}},
         "required": ["session_id"]}},
    {"name": "set_sprint", "description":
        "Update only the sprint — the short-term focus that changes each session or week. "
        "Any team member can call this; no ownership check. If pending items from the current "
        "sprint were never started, returns a WARNING block listing them. Pass force=true to "
        "override and overwrite anyway.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"}, "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "sprint": {"type": "string"},
         "force": {"type": "boolean",
                   "description": "Skip the unstarted-items guard and overwrite the sprint anyway."}},
         "required": ["sprint"]}},
    {"name": "get_sprint_progress", "description":
        "Read-only: Return a SUMMARY of sprint items by status (pending/in_progress/done/failed) "
        "optionally filtered by version or item_group. Returns total, done, in_progress, pending, "
        "failed, percent_complete, and by_status (counts only — no per-item list; call "
        "get_sprint_items(status=\"pending\") for the live item list). Useful to see how far "
        "through the sprint we are without listing all items. Pass session_id to also get a "
        "board_change field reporting items added since that session started (live-queue signal "
        "— call this between sprint items to pick up mid-run injections).",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"}, "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "session_id": {"type": "string", "description": "Optional: include board_change (items added since this session started)."},
         "version": {"type": "string", "description": "Filter to a specific sprint version bucket."},
         "item_group": {"type": "string", "description": "Filter to a specific item group."}},
         "required": []}},
    {"name": "add_sprint_item", "description":
        "ALWAYS call get_sprint_items first to check for existing pending items before adding. "
        "Append a todo item to the project's sprint checklist. Use when starting work on a "
        "new version so the next session sees what's in flight. Optional: group items under "
        "a named objective with 'group'; attribute to a person with 'human_id'. "
        "Use 'depends_on' to block until another item finishes. Blocks near-duplicate titles "
        "(>=60% word overlap with an open pending/in_progress item) and returns the conflict; "
        "also warns (drift_warning) when the title looks already-shipped — 3+ keyword overlap "
        "with a migrations.py/_migrate_X or a recent commit; pass force=true to add anyway.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"}, "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "version": {"type": "string"},
         "title": {"type": "string"},
         "group": {"type": "string", "description": "Optional objective name for grouping."},
         "human_id": {"type": "string", "description": "Optional: person this item is assigned to."},
         "depends_on": {"type": "string", "description": "Sprint item id that must complete first."},
         "failure_mode": {"type": "string", "enum": ["continue", "stop"],
                          "description": "'stop' blocks this item if the parent fails."},
         "milestone_type": {"type": "string", "enum": ["task", "milestone", "human"],
                            "description": "'milestone' renders as a timeline marker; 'human' marks a task for a human (hidden from executor sessions)."},
         "touches_resources": {"type": "array", "items": {"type": "string"},
                               "description": "Typed resource identifiers this item touches, for parallel conflict detection: 'file:path.py', 'db:migrations', 'mcp_tool:name', 'route:METHOD:/path', 'pypi:publish', 'github:tag'. Used by get_parallelizable_groups to cluster non-overlapping items. SYMBOL-LEVEL: append ':symbol_name' to a file id — 'file:path.py:function_name' — so two items editing DIFFERENT symbols in the SAME file are treated as non-overlapping and co-batched in parallel (line ranges resolve via real AST/tree-sitter parsing). Prefer symbol-level ids when two items touch the same file but different functions/classes."},
         "force": {"type": "boolean",
                   "description": "Override the duplicate guard AND the codebase drift check (7e212375) and add the item even if its title matches an existing open item or looks already-shipped. Default false."},
         "deferred_until": {"type": "string",
                            "description": "dec69708 — ISO timestamp before which the item CANNOT be claimed. claim_sprint_item hard-refuses it until this time passes (enforced deferral, e.g. 'defer the paper-track until 2026-09-01'). Omit for an immediately-claimable item."},
         "track": {"type": "string",
                   "description": "dec69708 — named lane for the item (e.g. 'paper'). Buckets items so a whole track can be deferred/skipped."}},
         "required": ["version", "title"]}},
    {"name": "fan_out_sprint_items",
     "description":
        "Bulk-insert sprint items from a single orchestrator call — decompose a goal into "
        "parallel work items without N sequential add_sprint_item calls. Pass a list of "
        "{title, description?, group?, version?} dicts; returns the list of new item_ids "
        "in insertion order. No duplicate guard is applied (the caller is assumed to have "
        "deduped). Items with empty titles are silently skipped.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"},
         "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "items": {
             "type": "array",
             "description": "List of sprint item specs. Each must have at least a 'title'.",
             "items": {
                 "type": "object",
                 "properties": {
                     "title": {"type": "string", "description": "Sprint item title (required)."},
                     "description": {"type": "string", "description": "Optional notes / detail for the item."},
                     "group": {"type": "string", "description": "Optional objective group name."},
                     "version": {"type": "string", "description": "Optional sprint-version bucket; defaults to empty string."},
                     "touches_resources": {"type": "array", "items": {"type": "string"}, "description": "Optional typed resource identifiers (file:/db:/mcp_tool:/route:/pypi:/github:) for parallel conflict detection. For SYMBOL-LEVEL granularity append ':symbol_name' to a file id ('file:path.py:func') so items editing different symbols in the same file co-batch in parallel."},
                 },
                 "required": ["title"],
             },
         }},
         "required": ["items"]}},
    {"name": "update_sprint_item", "description":
        "Edit fields on an existing sprint item: title, version, notes, human_id (assignee), "
        "group, deferred_until (enforced deferral), or track. Only the fields you pass are "
        "changed; omitted fields are left untouched. Pass an empty string for human_id, group, "
        "deferred_until, or track to clear it. Returns the updated item, or an error if the id "
        "is unknown.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"}, "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "item_id": {"type": "string"},
         "title": {"type": "string", "description": "New title."},
         "version": {"type": "string", "description": "Move the item to a different version/sprint bucket."},
         "notes": {"type": "string", "description": "Free-form note/context shown on the item."},
         "human_id": {"type": "string", "description": "Reassign to a person (assignee); empty string clears it."},
         "group": {"type": "string", "description": "Objective name to group the item under (item_group); empty string clears it."},
         "touches_resources": {"type": "array", "items": {"type": "string"},
                               "description": "Replace the item's typed resource identifiers (file:/db:/mcp_tool:/route:/pypi:/github:). Pass [] to clear. Omit to leave unchanged. SYMBOL-LEVEL: append ':symbol_name' to a file id ('file:path.py:func') so items editing different symbols in the same file are non-overlapping and co-batch in parallel."},
         "required_notes": {"type": "boolean", "description": "Quality gate (5823db0b): when true, complete_sprint_item is blocked until the item has evidence (existing notes, a linked task, or a notes= argument on completion)."},
         "deferred_until": {"type": "string", "description": "dec69708 — ISO timestamp before which the item CANNOT be claimed (enforced deferral). Pass an empty string to CLEAR the deferral and make the item claimable now. Omit to leave unchanged."},
         "track": {"type": "string", "description": "dec69708 — named lane (e.g. 'paper'). Pass an empty string to clear; omit to leave unchanged."}},
         "required": ["item_id"]}},
    {"name": "complete_sprint_item", "description":
        "Mark a sprint item done. Pass task_id to link the task that shipped it. "
        "Pass session_id to get a board_change field (items injected mid-run) and an "
        "active-worktree merge reminder in the response. If the item is flagged "
        "required_notes, you MUST pass notes= (evidence: what shipped / how verified) "
        "or a task_id, or completion is refused (EVIDENCE_REQUIRED).",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"}, "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "item_id": {"type": "string"},
         "task_id": {"type": "string"},
         "notes": {"type": "string", "description": "Evidence for the completion (what shipped / how it was verified). Persisted on the item; satisfies the required_notes gate."},
         "actor": {"type": "string", "description": "Executor id/name recorded as having completed the item (defaults to session_id)."},
         "session_id": {"type": "string", "description": "Optional: include board_change + worktree merge reminder."}},
         "required": ["item_id"]}},
    {"name": "reconcile_sprint_drift", "description":
        "Read-only: Cross-reference pending sprint items against recent git commits and "
        "return items that may already be done. Uses keyword matching — confidence 'high' "
        "means 3+ keywords overlap (safe to mark done), 'medium' means 1-2 (verify first). "
        "Call during planning sessions to identify board drift before filing new items.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"}, "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."}},
         "required": []}},
    {"name": "get_planning_brief", "description":
        "PLANNING SESSIONS: CALL THIS FIRST before anything else. "
        "Read-only: Return a compact planning context — sprint, north star, pending items, "
        "in-progress items, recent tasks, active sessions, recent decisions, unvalidated "
        "assumptions, the last session's output (last_session), and a new-handoff signal. "
        "No session registration needed. Designed for planning chat sessions that need to see "
        "project state without side effects. Pass `since` (a prior call's generated_at) to flag "
        "only handoffs filed since you last checked.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"}, "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "since": {"type": "string", "description": "Optional ISO timestamp (a prior brief's generated_at). When given, new_handoff_available flags only handoffs filed after it."}},
         "required": []}},
    {"name": "refresh_context", "description":
        "Single-call post-compaction recovery for planning chats. Returns a "
        "COMPACT snapshot — current sprint + progress, next pending items, the "
        "active session id, recent handoffs, high-priority (urgent) decisions, "
        "unvalidated assumptions, and key note slugs — small enough not to "
        "overflow context. Call this the moment a chat feels disoriented (e.g. "
        "right after a /compact) to re-orient in one round-trip.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"}, "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."}},
         "required": []}},
    {"name": "get_sprint_items", "description":
        "Read-only: List sprint items for a project. Optional status filter "
        "(todo|pending|in_progress|provisional_complete|done|failed|skipped|pushed|indeterminate). "
        "Cold sessions read this to know what's still owed.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"}, "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "status": {"type": "string",
                    "enum": ["pending", "todo", "in_progress", "provisional_complete",
                             "done", "failed", "skipped", "pushed", "indeterminate"],
                    "description": "Filter by status."}},
         "required": []}},
    {"name": "get_parallelizable_groups", "description":
        "Read-only: Return clusters of pending sprint items that are safe to run "
        "simultaneously. Filters pending/todo items (optionally by version) whose "
        "depends_on is satisfied, then greedily partitions them into groups where no "
        "two items in a group share a touches_resources identifier. The orchestrator "
        "fans out each group as a parallel subagent batch and runs the groups in "
        "sequence. Returns {version, groups: [[item,...],...], group_count, "
        "eligible_count, undeclared_count, blocked: [...]}. Items still waiting on an "
        "unfinished dependency are listed under 'blocked', not in any group. "
        "Makes parallel sprints system-enforced rather than LLM-guessed.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"}, "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "version": {"type": "string", "description": "Optional: only consider items in this sprint-version bucket."}},
         "required": []}},
    {"name": "analyze_sprint", "description":
        "PLANNING: Read-only synthesis of the current sprint into one structured brief — "
        "parallelizability (conflict-free groups + max fan-out), dependency chains "
        "(depends_on walked to the root), resource/file conflicts (items sharing "
        "touches_resources), and stalls (stall_count>0). Returns {summary, "
        "recommended_strategy, parallelism, dependency_chains, longest_chain, "
        "file_conflicts, stalls, blocked, running}. Call in planning sessions instead of "
        "stitching together get_parallelizable_groups + manual dependency/conflict analysis.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"}, "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "version": {"type": "string", "description": "Optional: only analyze items in this sprint-version bucket."}},
         "required": []}},
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
         "project_id": {"type": "string"}, "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "query": {"type": "string"},
         "limit": {"type": "integer", "description": "Max results per type (default 10)."}},
         "required": ["query"]}},
    {"name": "search_synthesis", "description":
        "Read-only: Natural-language search that returns a short, CITED answer "
        "(which notes/items it drew from) synthesized over the same retrieval as "
        "search_all — not just a list of matches. Uses a cheap Haiku call when "
        "ANTHROPIC_API_KEY is set, with a deterministic fallback to the raw results "
        "(synthesized=false) otherwise. Returns {query, answer, cited, synthesized, results}.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"}, "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "query": {"type": "string"},
         "limit": {"type": "integer", "description": "Max results per type fed to synthesis (default 10)."}},
         "required": ["query"]}},
    {"name": "paper_search", "description":
        "Search academic papers — a REAL external lookup (keyless). Per the "
        "research-routing protocol, use this FIRST for academic/paper questions (cite "
        "the paper itself, not a secondary write-up), then capture_research_finding to "
        "save what you cite. Two keyless sources via the 'source' param: 'arxiv' "
        "(default; preprints, physics/CS/math) and 'openalex' (published journal/"
        "conference works across every discipline). Both return the same shape: {query, "
        "count, results:[{title, authors, summary, published, url, pdf_url, ...}]} — "
        "arxiv rows carry arxiv_id, openalex rows carry openalex_id + doi.",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "Search terms (matches title / abstract / authors)."},
         "limit": {"type": "integer", "description": "Max papers to return (default 10, max 50)."},
         "source": {"type": "string", "enum": ["arxiv", "openalex"], "description": "Which keyless source to search (default 'arxiv'). 'openalex' covers published cross-discipline works."},
         "sort_by": {"type": "string", "enum": ["relevance", "date"], "description": "Sort order (default relevance; 'date' = most recent first)."}},
         "required": ["query"]}},
    {"name": "get_agent_instructions", "description":
        "Read-only: Return the custom agent_instructions for a project. "
        "These are injected automatically by start_session so every session picks them up. "
        "Use this when you need to read or display the current instructions.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"}, "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."}},
         "required": []}},
    {"name": "set_agent_instructions", "description":
        "Set or update the custom agent_instructions for a project. "
        "Instructions are injected into every start_session response so AI sessions see them "
        "automatically — no need to repeat in every session. "
        "Pass null or empty string to clear. "
        "Use for persistent rules like coding conventions, deploy steps, or codebase notes.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"}, "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "instructions": {"type": "string", "description": "Markdown text injected at session start. Pass null to clear."}},
         "required": ["instructions"]}},
    {"name": "set_executor_config", "description":
        "Store per-project executor defaults (repo_path, env_file, test_cmd, test_min, deploy_cmd, shell_type, branch). "
        "Merges onto the existing config — other keys (hostnames, filesystem_roots, …) are preserved. "
        "Pass repo_paths as an array of {cwd, hostname} known locations; they are merged into the existing "
        "repo_paths (deduped) rather than overwriting, so manual + hook-registered entries coexist. "
        "Executor sessions auto-load these when start_session(role='executor') is used. "
        "Credentials rule is always injected separately: read secrets from env_file only, never remote shell.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"}, "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "repo_path": {"type": "string"},
         "repo_paths": {"type": "array", "items": {"type": "object", "properties": {
             "cwd": {"type": "string"}, "hostname": {"type": "string"}}},
             "description": "Known locations [{cwd, hostname}] — merged into existing repo_paths, not overwritten."},
         "env_file": {"type": "string"},
         "test_cmd": {"type": "string"},
         "test_min": {"type": "integer"},
         "deploy_cmd": {"type": "string"},
         "shell_type": {"type": "string"},
         "branch": {"type": "string"},
         "filesystem_roots": {"type": "array", "items": {"type": "string"},
             "description": "Directories the tunnel's filesystem connector may serve (unioned across the tenant's projects). Overwrites the existing list."},
         "serena_repo_path": {"type": "string",
             "description": "b970fe07 — default repo path for Serena (the tunnel's code-extractor slot). Auto-fetched at tunnel start; used only when --repo is not passed on the CLI."},
         "codebase_code_dirs": {"type": "array", "items": {"type": "string"},
             "description": "b970fe07 — directories codebase-memory-mcp (the tunnel's code-intel slot) auto-indexes. Deduped-union across the tenant's projects; used only when --code-dir is not passed on the CLI. Overwrites the existing list."},
         "context_threshold": {"type": "integer", "description": "Turns before a context-budget warning is surfaced to the session."},
         "max_turns": {"type": "integer", "description": "Turn ceiling injected into the /goal string ('Stop after N turns'). Default 200."}},
         "required": []}},
    {"name": "claim_file", "description":
        "Claim edit rights on a file for this session. Whole-file by default "
        "(auto-expires after 2 hours). For symbol-level claims — so two sessions "
        "can edit the same file if they own different classes/functions — also "
        "pass `symbol` (e.g. 'AuthRouter' or 'AuthRouter.login') AND `content` "
        "(the file's full source). Meridian parses the source (stdlib ast for "
        "Python, tree-sitter for JS/TS/C/C++/Go/Rust/Java/C#), and hard-blocks if "
        "another live session already owns an overlapping line range — the block "
        "lists which symbols are still safe to claim. Unparseable content falls "
        "back to a whole-file lock. The response includes a `code_notes` list of "
        "code-anchored project notes (kind='code') for this file/symbol — read "
        "them before editing.",
     "inputSchema": {"type": "object", "properties": {
         "session_id": {"type": "string"},
         "file_path": {"type": "string"},
         "mode": {"type": "string", "enum": ["read", "write"], "description": "Claim grain (ffa03655). 'write' (default) = EXCLUSIVE: blocks other writers and is blocked by any other session's read claim. 'read' = SHARED: many sessions can read-claim the same file at once (no false contention for parallel reader agents), blocked only by another session's write lock."},
         "symbol": {"type": "string", "description": "Optional symbol to claim (class/function/method name, e.g. 'AuthRouter' or 'AuthRouter.login'). Requires `content`."},
         "content": {"type": "string", "description": "Full source of the file, required when `symbol` is given so the server can resolve the symbol's line range."}},
         "required": ["session_id", "file_path"]}},
    {"name": "store_finding", "description":
        "PARALLEL COORDINATION (c35370cc): persist a per-task intermediate result to the "
        "session_findings table so it survives session boundaries. Parallel reader agents "
        "write findings; an orchestrator or writer agent reads them via get_findings. Unlike "
        "save_finding (which creates a research note), this is a lightweight key→content store "
        "for agent-to-agent handoff of intermediate work.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"}, "project_name": {"type": "string", "description": "Project name — an alternative to project_id."},
         "content": {"type": "string", "description": "The finding body."},
         "key": {"type": "string", "description": "Optional bucket/topic for scoped retrieval (e.g. a subsystem name)."},
         "title": {"type": "string", "description": "Optional short title."},
         "session_id": {"type": "string", "description": "Optional writing session."},
         "task_id": {"type": "string", "description": "Optional task this finding belongs to."}},
         "required": ["content"]}},
    {"name": "get_findings", "description":
        "Read-only (c35370cc): read stored session_findings for a project (newest first), "
        "optionally scoped by key and/or session_id. The read side of store_finding.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"}, "project_name": {"type": "string", "description": "Project name — an alternative to project_id."},
         "key": {"type": "string", "description": "Only findings in this bucket."},
         "session_id": {"type": "string", "description": "Only findings from this session."},
         "limit": {"type": "integer", "description": "Max rows (default 50)."}},
         "required": []}},
    {"name": "send_message", "description":
        "PARALLEL COORDINATION (d3a3a01d): enqueue an actor-model message to another session "
        "(session_messages table). 'Done with X, you do Y' between parallel agents. The "
        "recipient reads with receive_messages. A2A-compatible.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"}, "project_name": {"type": "string", "description": "Project name — an alternative to project_id."},
         "to_session_id": {"type": "string", "description": "Recipient session id."},
         "payload": {"type": "string", "description": "Message body (text or JSON)."},
         "from_session_id": {"type": "string", "description": "Sender session id (defaults to session_id)."},
         "kind": {"type": "string", "description": "Optional message kind/tag."}},
         "required": ["to_session_id", "payload"]}},
    {"name": "receive_messages", "description":
        "PARALLEL COORDINATION (d3a3a01d): fetch unread messages addressed to a session "
        "(oldest first) and mark them read by default. The receive side of send_message.",
     "inputSchema": {"type": "object", "properties": {
         "session_id": {"type": "string", "description": "The recipient session."},
         "mark_read": {"type": "boolean", "description": "Mark fetched messages read (default true)."},
         "limit": {"type": "integer", "description": "Max messages (default 50)."}},
         "required": ["session_id"]}},
    {"name": "idle_until_all_done", "description":
        "PARALLEL COORDINATION (d3a3a01d): non-blocking barrier check across sibling sessions. "
        "Returns {all_done, pending, statuses}; a session is done when closed/archived/missing. "
        "The server can't block, so poll until all_done is true — the A2A 'wait for X, Y, Z to "
        "finish' primitive.",
     "inputSchema": {"type": "object", "properties": {
         "session_ids": {"type": "array", "items": {"type": "string"}, "description": "Sessions to wait on."}},
         "required": ["session_ids"]}},
    {"name": "release_file", "description":
        "Release a file lock (and any symbol claims this session holds on it).",
     "inputSchema": {"type": "object", "properties": {
         "session_id": {"type": "string"},
         "file_path": {"type": "string"}},
         "required": ["session_id", "file_path"]}},
    {"name": "get_file_claims", "description":
        "Read-only: show active claims on a file — the whole-file lock (with the "
        "holder's session name, if any) plus any symbol-level claims. Use to check "
        "who owns a file before editing it. Pass project_id (and optional symbol) "
        "to also get a `code_notes` list of code-anchored notes (kind='code') for "
        "that path.",
     "inputSchema": {"type": "object", "properties": {
         "file_path": {"type": "string"},
         "project_id": {"type": "string", "description": "Include code-anchored notes (kind='code') for this project/path in the response."},
         "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "symbol": {"type": "string", "description": "Optional symbol to scope code-anchored notes to (requires project_id)."}},
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
    {"name": "list_plugins", "description":
        "Read-only: Lightweight index of active tunnel plugins — name, description, "
        "enabled state, and tool_count. Does NOT return full tool schemas (use "
        "get_plugin_details for that). Dramatically reduces context bloat vs. "
        "dumping all plugin schemas at startup (~500 tokens vs 50k+). "
        "Returns an 'active_plugins' list plus any stored skill notes.",
     "inputSchema": {"type": "object", "properties": {},
         "required": []}},
    {"name": "get_plugin_details", "description":
        "Read-only: Full schema for one named plugin (all tool definitions, "
        "description overrides, and stored skill guide if available). "
        "Use list_plugins first to see which plugins are active, then call "
        "get_plugin_details(name) to load the schema for a specific plugin "
        "on demand.",
     "inputSchema": {"type": "object", "properties": {
         "name": {"type": "string", "description": "Plugin name as returned by list_plugins (e.g. 'filesystem', 'code-intel', 'code-extractor')."}},
         "required": ["name"]}},
    {"name": "get_graph_diff", "description":
        "Read-only: compare the latest code-graph snapshots of two sessions — returns delta in node_count, hotspot_count, and file_churn. Use snapshot_graph_metrics first to record each session's current state.",
     "inputSchema": {"type": "object", "properties": {
         "session_a": {"type": "string", "description": "First session ID."},
         "session_b": {"type": "string", "description": "Second session ID to compare against session_a."}},
         "required": ["session_a", "session_b"]}},
    {"name": "snapshot_graph_metrics", "description":
        "Record a code-graph snapshot for a session (node count, edge count, hotspot count, file churn). Call at session start and end to enable get_graph_diff comparisons.",
     "inputSchema": {"type": "object", "properties": {
         "session_id": {"type": "string"},
         "project_id": {"type": "string"},
         "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."}},
         "required": ["session_id"]}},
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
         "project_id": {"type": "string"}, "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "file": {"type": "string", "description": "CLAUDE.md | AGENTS.md"},
         "anchor": {"type": "string"},
         "content": {"type": "string", "description": "Full proposed body for the section."},
         "session_id": {"type": "string"},
         "force": {"type": "boolean", "description": "Human planning sessions pass true to apply directly without HITL. Default false."},
         "urgency": {"type": "string", "enum": ["normal", "high", "blocking"]}},
         "required": ["file", "anchor", "content"]}},
    {"name": "claim_sprint_item",
     "description": "Claim a pending sprint item: sets status to in_progress and records claimed_at + actor. Read-only: false. Rejects if the item is already in_progress, done, failed, skipped, or its touches_files overlap active file claims from another live session.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"}, "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "item_id": {"type": "string"},
         "actor": {"type": "string", "description": "Executor id/name recorded as having claimed the item (5823db0b; defaults to session_id)."},
         "session_id": {"type": "string", "description": "Optional caller session id; its own file claims are ignored for conflict checks."}},
         "required": ["item_id"]}},
    {"name": "add_subtask",
     "description": "Add a child sprint item under an existing parent item. Inherits the parent's version. Status starts as pending. Rejects if the parent is already done, failed, or skipped. Pass owner='human' or owner='ai' to build a mixed-ownership task chain: owned subtasks added in sequence become a strict chain (each depends on the previous owned sibling), and completing one auto-advances ownership — an AI→human step files a HITL handoff, a human→AI step un-blocks the next AI subtask. The parent stays in_progress until all subtasks are terminal.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"}, "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "parent_id": {"type": "string", "description": "ID of the parent sprint item."},
         "title": {"type": "string", "description": "Title of the new subtask."},
         "owner": {"type": "string", "enum": ["human", "ai"], "description": "Optional owner for mixed-ownership task chains: 'human' or 'ai'. Omit for a legacy unchained subtask."}},
         "required": ["parent_id", "title"]}},
    {"name": "split_sprint_item",
     "description": "Split a sprint item into multiple smaller items. The original is closed (skipped) and N new items are created with split_from referencing the original. Returns list of new items.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"}, "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "item_id": {"type": "string", "description": "ID of the item to split."},
         "titles": {"type": "array", "items": {"type": "string"},
                    "description": "Titles for the new items (minimum 2)."}},
         "required": ["item_id", "titles"]}},
    {"name": "merge_sprint_items",
     "description": "Merge multiple sprint items into one. Source items are closed (skipped, merged_into=survivor). Returns the new survivor item.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"}, "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "item_ids": {"type": "array", "items": {"type": "string"},
                      "description": "IDs of items to merge (minimum 2)."},
         "new_title": {"type": "string", "description": "Title for the merged survivor item."}},
         "required": ["item_ids", "new_title"]}},
    {"name": "set_active_repo",
     "description": "Update the tunnel's active Serena repo at runtime. When a planning session switches to a different codebase, call this so subsequent Serena requests (find_symbol, find_referencing_symbols, etc.) route to the new repo without restarting the tunnel. Has no effect when no tunnel is connected.",
     "inputSchema": {"type": "object", "properties": {
         "repo_path": {"type": "string", "description": "Absolute path to the repository to activate (e.g. /home/me/project or C:\\\\Users\\\\me\\\\project)."}},
         "required": ["repo_path"]}},
]

_READ_ONLY_TOOLS = {
    "list_projects", "get_project_by_name", "get_goal", "get_notes", "read_note",
    "get_pinned_decisions", "get_tasks", "search_tasks", "search_all", "search_synthesis",
    "paper_search",
    "get_session_brief", "get_context_block", "get_hitl_request",
    "list_hitl_requests", "list_sessions", "get_sprint_notes",
    "get_session_log", "idle_until_session_done", "generate_handoff", "load_handoff",
    "get_insights",
    "get_workspace_notes", "get_workspace_decisions", "get_workspace_settings",
    "get_blog_posts",
    "get_sprint_items", "get_sprint_progress", "get_agent_instructions",
    "reconcile_sprint_drift", "get_planning_brief", "get_file_claims",
    "list_plugins", "get_plugin_details",
    "get_symbol_claims", "get_symbol_hotspots", "get_graph_diff",
    "get_citation_edges",
    "get_sprint_item_pointers", "resolve_sprint_item_pointers",
}
_DESTRUCTIVE_TOOLS = {"delete_note", "archive_decision", "dismiss_hitl", "delete_sprint_item_pointer"}

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
    "save_blog_post": "Save Blog Post",
    "get_blog_posts": "Get Blog Posts",
    "set_executor_config": "Set Executor Config",
    "set_north_star": "Set North Star",
    "idle_until_session_done": "Wait for Session to Close",
    "generate_handoff": "Generate Handoff",
    "get_agent_instructions": "Get Agent Instructions",
    "set_agent_instructions": "Set Agent Instructions",
    "reconcile_sprint_drift": "Reconcile Sprint Drift",
    "get_planning_brief": "Get Planning Brief",
    "get_file_claims": "Get File Claims",
    "list_plugins": "List Plugins",
    "get_plugin_details": "Get Plugin Details",
    "set_active_repo": "Set Active Repo",
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
