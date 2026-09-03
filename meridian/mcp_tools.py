"""Shared MCP tool metadata for the Meridian server."""

from __future__ import annotations

import re
from typing import Any


_TOOL_EXAMPLES: dict[str, str] = {
    "create_project": 'create_project(name="my-app")',
    "set_parent_project": 'set_parent_project(project_name="ms-thesis", parent_project_name="Camerer_MS_Graduation_2026")',
    "rename_project": 'rename_project(project_name="old-name", new_name="new-name")',
    "merge_project": 'merge_project(source_project_id="dup-uuid", target_project_id="keep-uuid")',
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
    "add_proposal_gate": 'add_proposal_gate(project_id="abc-123", category="destructive_ops", question="Drop the legacy table?", affected=["item-uuid"], evidence="no backup confirmed yet")',
    "resolve_proposal_gate": 'resolve_proposal_gate(project_id="abc-123", gate_id="gate-uuid", state="allowed", decision="backup confirmed", actor="adam")',
    "reopen_proposal_gate": 'reopen_proposal_gate(project_id="abc-123", gate_id="gate-uuid", actor="adam", reason="new evidence surfaced")',
    "get_proposal_gates": 'get_proposal_gates(project_id="abc-123", sprint_item_id="item-uuid")',
    "checkpoint": 'checkpoint(session_id="session-uuid", project_id="abc-123")',
    "request_hitl": 'request_hitl(project_id="abc-123", question="Should we add rate limiting here?", urgency="normal")',
    "get_hitl_request": 'get_hitl_request(request_id="hitl-uuid")',
    "add_note": 'add_note(project_id="abc-123", title="Deploy note", body="Reminder: update env vars before deploy", tags="ops,deploy")',
    "ingest_document": 'ingest_document(project_id="abc-123", file_path="docs/spec.docx", tags="spec")  # or, for a PDF: ingest_document(project_id="abc-123", content="<text you extracted>", title="Q3 report", source="https://example.com/q3.pdf")',
    "get_document_structure": 'get_document_structure(file_path="thesis/chapter1.docx")',
    "get_latex_structure": 'get_latex_structure(file_path="thesis/chapter1.tex")',
    "get_citation_edges": 'get_citation_edges(project_id="abc-123", source="thesis/chapter1.tex")',
    "resolve_citations": 'resolve_citations(project_id="abc-123")',
    "index_equation": 'index_equation(project_id="abc-123", doc="thesis/chapter1.docx", omml_or_latex="E=mc^2", semantic_label="mass-energy equivalence")',
    "find_similar_equation": 'find_similar_equation(project_id="abc-123", doc="thesis/chapter1.docx", latex="E=mc^2")',
    "insert_equation": 'insert_equation(project_id="abc-123", doc="thesis/chapter1.docx", para_id="0000B002", equation_id_or_omml="E=mc^2", position="append")',
    "update_paragraph": 'update_paragraph(project_id="abc-123", doc="thesis/chapter1.docx", para_id="1F2A3B4C", new_text="The revised conclusion sentence.")',
    "find_symbol_usages": 'find_symbol_usages(project_id="abc-123", doc="thesis/chapter1.docx", symbol_or_equation_id="E=mc^2")',
    "index_figure": 'index_figure(project_id="abc-123", doc="thesis/chapter1.docx", file_path="figures/setup.png", caption="Figure 3: The experimental setup", semantic_label="apparatus diagram")',
    "find_similar_figure": 'find_similar_figure(project_id="abc-123", doc="thesis/chapter1.docx", description_or_path="experimental setup diagram")',
    "link_figure_caption": 'link_figure_caption(project_id="abc-123", doc="thesis/chapter1.docx", figure_id="fig-uuid-here", caption_element_id="el-uuid-here")',
    "index_table": 'index_table(project_id="abc-123", doc="thesis/chapter1.docx", table_index=2, caption="Table 2: Summary of experimental results", semantic_label="results table")',
    "find_similar_table": 'find_similar_table(project_id="abc-123", doc="thesis/chapter1.docx", description="summary of experimental results")',
    "link_table_caption": 'link_table_caption(project_id="abc-123", doc="thesis/chapter1.docx", table_id="tbl-uuid-here", caption_element_id="el-uuid-here")',
    "search_outputs": 'search_outputs(outputs_dir="/repo/outputs", query="temperature pressure sweep")',
    "annotate_outputs": 'annotate_outputs(outputs_dir="/repo/outputs", path="/repo/outputs/run_42", note="PCA on, BFS off — final params", run_params={"lr": 0.001, "epochs": 100})',
    "find_outputs_by_source": 'find_outputs_by_source(outputs_dir="/repo/outputs", source_path="analysis/run.py")',
    "search_code_semantic": 'search_code_semantic(root_dir="/repo/src", query="parse the auth token and refresh it")',
    "get_flag_registry": 'get_flag_registry(root_dir="/repo/src")',
    "link_flag_to_section": 'link_flag_to_section(project_id="abc-123", doc="thesis/chapter4.docx", element_id="el-uuid-here", flag_name="DT_ONLY_WIDTH", value=1, default=0, source_file="pipeline/gt.py", source_line=142)',
    "get_flag_drift": 'get_flag_drift(project_id="abc-123", root_dir="/repo/src", doc="thesis/chapter4.docx")',
    "add_sprint_item_pointer": 'add_sprint_item_pointer(project_id="abc-123", sprint_item_id="item-uuid", source_type="code", targets=[{"uri": "meridian/server.py", "selector": {"type": "symbol", "qualified_name": "meridian.server.mcp_tools_doc"}}], label="the tool-doc generator")',
    "get_sprint_item_pointers": 'get_sprint_item_pointers(project_id="abc-123", sprint_item_id="item-uuid")',
    "resolve_sprint_item_pointers": 'resolve_sprint_item_pointers(project_id="abc-123", sprint_item_id="item-uuid")',
    "delete_sprint_item_pointer": 'delete_sprint_item_pointer(pointer_id="pointer-uuid")',
    "execute_batch": 'execute_batch(project_id="abc-123", operation="sprint_items", entries=[{"title": "Add rate limiting", "correlation_key": "a"}, {"title": "Add retry backoff", "correlation_key": "b"}], mode="all_or_nothing", idempotency_key="my-2026-08-05-batch-1")',
    "batch_read": 'batch_read(project_id="abc-123", requests=[{"request_id": "items", "adapter": "sprint_board", "operation": "get_sprint_items", "args": {"status": "pending"}}, {"request_id": "ptrs", "adapter": "sprint_board", "operation": "get_sprint_item_pointers", "args": {"sprint_item_id": "item-uuid"}, "depends_on": ["items"]}])',
    "batch_mutate": 'batch_mutate(project_id="abc-123", entries=[{"kind": "sprint_item_update", "item_id": "item-uuid", "priority": "high", "correlation_key": "a"}, {"kind": "sprint_item_pointer", "sprint_item_id": "item-uuid", "source_type": "file", "targets": [{"uri": "meridian/db/batch_management.py"}], "correlation_key": "b"}], mode="all_or_nothing", idempotency_key="my-2026-08-07-mutate-1")',
    "get_notes": 'get_notes(project_id="abc-123")',
    "read_note": 'read_note(project_id="abc-123", slug="deploy-note")',
    "add_workspace_note": 'add_workspace_note(title="Onboarding", body="All repos use pixi", tags="setup")',
    "get_workspace_notes": 'get_workspace_notes(tag="setup")',
    "add_workspace_proposal": 'add_workspace_proposal(title="IDEA: expose auth as plugin", body="Could ship auth as a separate optional plugin so self-hosters can swap it out", tags="arch")',
    "get_workspace_proposals": 'get_workspace_proposals(status="investigating")',
    "advance_proposal_status": 'advance_proposal_status(proposal_id="prop-uuid", status="investigating")',
    "promote_proposal": 'promote_proposal(proposal_id="prop-uuid", project_id="proj-uuid", sprint_item_title="Expose auth as plugin")',
    "preview_proposal_promotion": 'preview_proposal_promotion(proposal_id="prop-uuid", project_id="proj-uuid", depth="sprint_items")',
    "commit_proposal_promotion": 'commit_proposal_promotion(proposal_id="prop-uuid", project_id="proj-uuid", depth="sprint_items", preview_hash="sha256:...")',
    "pin_workspace_decision": 'pin_workspace_decision(title="Monorepo", body="One repo for all services", category="ARCHITECTURAL")',
    "get_workspace_decisions": 'get_workspace_decisions()',
    "get_workspace_settings": 'get_workspace_settings()',
    "update_workspace_settings": 'update_workspace_settings(hitl_auto_answer_default=True, sprint_name_default="june-sprint")',
    "refresh_tool_manifest": 'refresh_tool_manifest()',
    "save_blog_post": 'save_blog_post(title="Shipping the Blog tab", body="# What changed\\n...", status="published")',
    "get_blog_posts": 'get_blog_posts(status="published")',
    "add_sprint_item": 'add_sprint_item(project_id="abc-123", title="Add OAuth login", item_group="auth")',
    "fan_out_sprint_items": 'fan_out_sprint_items(project_id="abc-123", items=[{"title": "Design DB schema", "group": "backend"}, {"title": "Build API endpoints", "group": "backend"}, {"title": "Wire up frontend", "group": "frontend"}])',
    "update_sprint_item": 'update_sprint_item(project_id="abc-123", item_id="item-uuid", title="Add OAuth + SAML login", group="auth", human_id="alice")',
    "reconcile_sprint_drift": 'reconcile_sprint_drift(project_id="abc-123")',
    "assign_sprint_waves": 'assign_sprint_waves(project_id="abc-123")',
    "start_wave_run": 'start_wave_run(project_id="abc-123", version="v0.2.5", wave_label="wave-2", item_ids=["item-uuid-a", "item-uuid-b"], failure_modes={"item-uuid-a": "stop"})',
    "finalize_wave_run": 'finalize_wave_run(wave_run_id="run-uuid", evidence={"status": "ok", "exit_code": 0, "passed": 1780, "failed": 0}, expected_revision_hash="sha256:...")',
    "resume_wave": 'resume_wave(wave_run_id="run-uuid", goal_token="a1b2c3d4e5f6a7b8", presented_body="/goal\\n<sprint_items>...</sprint_items>")',
    "record_handoff_correction": 'record_handoff_correction(project_id="abc-123", source_handoff_id="handoff-uuid", blocker_classification="pointer_unresolved", investigation_evidence={"finding": "the file was renamed since the handoff was rendered"}, regenerate=True)',
    "export_ai_log": 'export_ai_log(project_id="abc-123", event_type="tool.invoked", limit=500)',
    "export_ai_log_artifacts": 'export_ai_log_artifacts(project_id="abc-123", content_hashes=["sha256:..."])',
    "purge_ai_log": 'purge_ai_log(project_id="abc-123", cutoff="2025-01-01T00:00:00Z")',
    "complete_wave_gate": 'complete_wave_gate(project_id="abc-123", wave_label="wave-1", verification_payload={"status": "ok", "exit_code": 0, "passed": 42, "failed": 0, "stdout_tail": "42 passed in 5.3s", "stderr_tail": ""})',
    "configure_wave_gate": 'configure_wave_gate(project_id="abc-123", wave_end="wave-3", actions=[{"type": "push_dev"}, {"type": "run_verification"}, {"type": "push_main"}, {"type": "deploy"}])',
    "get_planning_brief": 'get_planning_brief(project_id="abc-123")',
    "get_sprint_items": 'get_sprint_items(project_id="abc-123")',
    "complete_sprint_item": 'complete_sprint_item(item_id="item-uuid")',
    "claim_sprint_item": 'claim_sprint_item(project_id="abc-123", item_id="item-uuid")',
    "heartbeat": 'heartbeat(session_id="session-uuid")',
    "list_projects": 'list_projects()',
    "get_sessions": 'get_sessions(project_id="abc-123")',
    "set_executor_config": 'set_executor_config(project_id="abc-123", repo_path="/repo", env_file="/repo/.env", test_cmd="pixi run test", test_min=619, deploy_cmd="git push", shell_type="powershell", branch="dev")',
    "get_capability_manifest": 'get_capability_manifest(project_id="abc-123")',
    "set_capability_manifest": 'set_capability_manifest(project_id="abc-123", capabilities=[{"id": "code-search", "purpose": "find symbols/functions/classes", "required_tools": ["Serena: find_symbol"], "fallback_chain": ["search_code_semantic"], "availability_policy": "required"}])',
    "set_capability_profile": 'set_capability_profile(scope_type="project", scope_id="abc-123", capabilities=[{"id": "code-search", "purpose": "find symbols/functions/classes", "required_tools": ["Serena: find_symbol"], "availability_policy": "required"}], disabled_capability_ids=["legacy-grep-search"])',
    "clear_capability_profile": 'clear_capability_profile(scope_type="item", scope_id="item-uuid")',
    "get_effective_capability_profile": 'get_effective_capability_profile(project_id="abc-123", sprint_item_id="item-uuid")',
    "list_profile_layers": 'list_profile_layers(scope_type="workspace")',
    "get_profile_layer": 'get_profile_layer(scope_type="hosted_default", scope_id="global")',
    "save_profile_layer": 'save_profile_layer(scope_type="workspace", scope_id="singleton", fields={"tool_priority_map": {"docs": "meridian-docs"}})',
    "clone_profile_layer": 'clone_profile_layer(source_scope_type="hosted_default", source_scope_id="global", target_scope_type="hosted_default", target_scope_id="global-v2")',
    "activate_profile_layer": 'activate_profile_layer(scope_id="global")',
    "reset_profile_layer": 'reset_profile_layer(scope_type="session", scope_id="session-uuid")',
    "get_profile_layer_revisions": 'get_profile_layer_revisions(scope_id="global", limit=10)',
    "get_effective_profile": 'get_effective_profile(project_id="abc-123", session_id="session-uuid")',
    "claim_file": 'claim_file(session_id="session-uuid", file_path="meridian/server.py")',
    "release_file": 'release_file(session_id="session-uuid", file_path="meridian/server.py")',
    "idle_until_session_done": 'idle_until_session_done(watching_session_id="session-uuid")',
    "get_session_log": 'get_session_log(session_id="session-uuid")',
    "get_session_activity": 'get_session_activity(session_id="session-uuid")',
    "set_active_repo": 'set_active_repo(repo_path="C:\\\\Users\\\\me\\\\project")',
    "analyze_model_efficiency": 'analyze_model_efficiency(title="Refactor auth across 12 files + migration", file_count=12, touches_resources=["auth_db", "sessions_table"], size="xl")',
    "run_verification": 'run_verification(project_id="abc-123")  # runs stored test_cmd on your local machine via tunnel',
    "add_custom_hook": 'add_custom_hook(project_id="abc-123", name="no-secrets", event="PreToolUse", matcher="Read|Bash", script_sh="grep -q SECRET_KEY <<<\\"$(cat)\\" && exit 2 || exit 0", blocking=True)',
    "get_custom_hooks": 'get_custom_hooks(project_id="abc-123", event="PreToolUse")',
    "delete_custom_hook": 'delete_custom_hook(project_id="abc-123", hook_id="hook-uuid")',
    "update_custom_hook": 'update_custom_hook(project_id="abc-123", hook_id="hook-uuid", enabled=False)  # or edit name/event/matcher/script_sh/script_ps1/blocking',
}


# Directory-review disclosure: every durable write advertises the same
# persistence contract in tools/list so clients can explain it before use.
_PERSISTENCE_NOTICE = (
    "Persistent-state disclosure: on hosted Meridian, supplied text and "
    "project/session metadata -- including task log entries, pinned "
    "decisions, sprint items, notes, handoff/goal state, and HITL queue "
    "items -- are sent to and stored in Meridian's service, in an isolated "
    "per-tenant Postgres database (Neon); self-hosted deployments keep the "
    "same categories in the configured local SQLite/Postgres database. This "
    "data is visible in the dashboard and API, and may resurface in later "
    "project context or handoffs. Notes and pinned decisions can be deleted "
    "individually; task log entries and sprint items can be deleted via the "
    "dashboard/API (not exposed as an agent-facing tool); HITL queue items "
    "and handoff state have no per-record delete. Full removal of any of "
    "this data is available via project or account deletion, using the "
    "documented controls. Do not include secrets."
)


# 76dde31f (665 follow-up) — typed per-item tool_requirements schema, shared
# verbatim between add_sprint_item and update_sprint_item so the two tool
# definitions can never drift on field names/enum values. Distinct from
# touches_resources (parallel-conflict scheduling metadata) and the legacy
# free-form required_tool pin (a single string) — see
# meridian.tool_requirements.normalize_tool_requirement for the canonical
# validation this mirrors.
_TOOL_REQUIREMENTS_SCHEMA: dict[str, Any] = {
    "type": "array",
    "description": (
        "76dde31f — typed per-item MCP tool-requirement contract, distinct from "
        "touches_resources (scheduling metadata) and the legacy free-form "
        "required_tool pin (a single string). Each entry: name, server_or_namespace, "
        "required_or_preferred ('required'|'preferred'), purpose (all required); "
        "call_template, fallback (a string or list of alternate tool ids), "
        "availability_check, verification (all optional). Once set, this structured "
        "field is the CANONICAL source build_item_briefing / the batch /goal's "
        "<tool_requirements> clause / the machine-readable capability contract render "
        "— required_tool keeps working and is used as a read-time compatibility "
        "fallback only when this is empty. No secrets or machine-local absolute paths "
        "(validated, same check as set_capability_manifest). Pass [] to clear."
    ),
    "items": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "The tool's name, e.g. 'find_symbol'."},
            "server_or_namespace": {"type": "string", "description": "Which server/namespace it lives under, e.g. 'Serena', 'meridian', 'Filesystem'."},
            "required_or_preferred": {"type": "string", "enum": ["required", "preferred"],
                "description": "'required' = hard requirement; 'preferred' = soft preference, never blocking."},
            "purpose": {"type": "string", "description": "Why this item needs it."},
            "call_template": {"type": "string", "description": "Optional example invocation/signature."},
            "fallback": {
                "anyOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}],
                "description": "Optional alternate tool id(s) to try, in order, if this one is unavailable.",
            },
            "availability_check": {"type": "string", "description": "Optional: how to confirm the tool is present (e.g. a tools/list name match)."},
            "verification": {"type": "string", "description": "Optional: how to confirm the call actually worked."},
        },
        "required": ["name", "server_or_namespace", "required_or_preferred", "purpose"],
    },
}


# 2f9cb288 (665 follow-up) — typed per-item artifact declaration schema,
# shared verbatim between add_sprint_item and update_sprint_item (same
# sharing discipline as _TOOL_REQUIREMENTS_SCHEMA above) so the three tool
# definitions can never drift on field names/enum values. See
# meridian.artifact_declaration for the canonical validation this mirrors.
_ARTIFACT_KIND_SCHEMA: dict[str, Any] = {
    "type": "string",
    "enum": ["document_only", "figure", "table"],
    "description": (
        "2f9cb288 — the kind of artifact this item produces. Omit when unknown "
        "(never guessed/inferred) — an absent value is distinct from any listed "
        "kind. Pass an empty string on update_sprint_item to CLEAR it."
    ),
}

_PLANNED_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        "2f9cb288 — a TYPED POINTER declaring where this item's output is "
        "expected to land — NOT a free-form path. Validated via meridian.pointers."
        "validate_pointer: source_type + a non-empty targets array of "
        "{uri, selector, target_kind?, subSelector?}, plus an optional label. "
        "Do not infer this from a directory or a generic 'mcp_tool:' resource id "
        "— only an explicit pointer counts. No secrets or machine-local absolute "
        "paths (same check as set_capability_manifest / tool_requirements). Pass "
        "null on update_sprint_item to clear."
    ),
    "properties": {
        "source_type": {"type": "string", "description": "e.g. 'code', 'docs', 'experiment' — what kind of source the target lives in."},
        "targets": {
            "type": "array",
            "description": "Non-empty array of {uri, selector, target_kind?, subSelector?, freshness?} — see add_sprint_item_pointer for the full selector shape (range/symbol/node_id/zotero_key/text_quote/finding_id/directory/git/remote_fs/artifact, 62640241).",
            "items": {"type": "object"},
        },
        "label": {"type": "string", "description": "Optional human-readable label for this output."},
        "provenance_required": {"type": "boolean", "description": "Whether the executor must record_provenance for this output before it counts as satisfied. Default false."},
    },
    "required": ["source_type", "targets"],
}

_ARTIFACT_POLICY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        "2f9cb288 — per-item override of how strictly a missing/wrong artifact "
        "output pointer is enforced. Absent (omit, or on update_sprint_item pass "
        "null to clear) falls back to the project default: artifact_pointer_check="
        "'warn', every guard flag false — never a silent 'off', never a silent "
        "'strict'. See meridian.artifact_declaration.effective_artifact_policy."
    ),
    "properties": {
        "artifact_pointer_check": {"type": "string", "enum": ["off", "warn", "strict"],
            "description": "off = no enforcement; warn = surface but don't block (default); strict = block completion without a valid planned_output pointer."},
        "require_exact_figure_output_pointer": {"type": "boolean", "description": "When true, a figure-kind item must declare an exact planned_output pointer (default false)."},
        "require_exact_table_output_pointer": {"type": "boolean", "description": "When true, a table-kind item must declare an exact planned_output pointer (default false)."},
        "allow_document_only_override": {"type": "boolean", "description": "When true, a document_only-kind item may override/bypass the pointer check (default false)."},
    },
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
    {"name": "merge_project", "description":
        "d6bd60e0 — merge a phantom-duplicate project INTO another. Re-parents EVERY "
        "child row of the source project (sprint items, tasks, decisions, insights, "
        "notes, HITL requests, sessions, handoffs, pointers, …) to the target project "
        "via pure UPDATEs — NO row is ever deleted. By default the now-empty source "
        "project is soft-archived (status='archived', name prefixed with '[merged] '), "
        "never hard-deleted; pass archive_source=false to leave it untouched. Returns "
        "{source_project_id, target_project_id, moved: {table: count}, source_archived}. "
        "Returns {error} if source==target or either project does not exist.",
     "inputSchema": {"type": "object", "properties": {
         "source_project_id": {"type": "string", "description": "The id of the project to merge FROM (its rows are re-parented; it is archived unless archive_source=false)."},
         "target_project_id": {"type": "string", "description": "The id of the project to merge INTO (receives all of the source's rows)."},
         "archive_source": {"type": "boolean", "description": "Default true — soft-archive the emptied source project (status='archived', name prefixed '[merged] '). Set false to leave the source project row untouched. The source is NEVER hard-deleted either way."}},
         "required": ["source_project_id", "target_project_id"]}},
    {"name": "register_session", "description": "Low-level: register this session without loading goal context. Use start_session instead for executor/human sessions — it registers AND returns goal + tasks in one call. Use register_session when you only need a session ID and will fetch context separately.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"}, "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."}, "session_name": {"type": "string"},
         "human_id": {"type": "string"},
         "client": {"type": "string", "enum": ["claude-code", "claude-desktop", "cursor", "other"]}},
         "required": ["session_name"]}},
    {"name": "start_session", "description": "Register a session and return orientation. Compact by default (session_id, sprint focus + status counts, 3 recent tasks, board_change count) to keep an executor's context small. Pass compact=false for the full block (goal XML, decisions, MERIDIAN.md instructions, workspace context, sprint items) — or fetch it later with get_session_brief. Pass version to scope the session to one sprint-version bucket (e.g. 'v0.1.x'): the orientation's sprint counts/items filter to it and the scope is remembered for the /goal template. Omit version to auto-scope to the bucket with the most pending items (empty board → unscoped). Also returns capability_contract (98aaccf4): a machine-readable {requested, effective, availability, manifest_hash, executable, executable_reasons, generated_at} object describing the project's declared capabilities and whether an executor can run right now — null if contract-building failed. Also returns execution_policy (75ac1c8e): a machine-readable {execution_mode, max_planning_turns, required_first_action, no_confirmation, permitted_parallel_wave, claim_before_edit, genuine_blocker_escalation} object — 'immediate' (default) names the exact first tool call to make and bounds planning turns before it; 'relaxed' is the explicit ask-first/planning posture. Derived from the project's execution_mode; max_planning_turns is executor_config-overridable via set_executor_config.",
     "inputSchema": {"type": "object", "properties": {
          "project_id": {"type": "string"}, "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."}, "session_name": {"type": "string", "description": "Optional (599d0097): omit or leave blank to auto-generate a meaningful name from the first pending sprint item title + a timestamp, instead of inventing a string."},
          "human_id": {"type": "string"},
          "client": {"type": "string", "enum": ["claude-code", "claude-desktop", "cursor", "other"]},
          "role": {"type": "string", "enum": ["executor", "planner"], "description": "325276f8 — 'executor' injects executor_config and credentials guidance and narrows active_tool_set to executor-oriented tools; 'planner' narrows active_tool_set to planner-oriented tools (no executor_config injection). Previously this enum only allowed 'executor', which made every connector/client-generated schema reject role='planner' with an enum validation error before the call ever reached the server, even though the server itself (_select_active_tool_set) has always supported both roles."},
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
        "mode='planner' returns strategic context for a claude.ai planning chat; "
        "mode='goal' (682005f4) returns ONLY the bare /goal block itself - no readiness "
        "header, no workspace decisions/notes, no L0/L1/L2 context - with each pending "
        "item's resolved code pointer(s), if any, rendered inline in <sprint_items>. "
        "FORWARD THE RETURNED content FIELD VERBATIM to the user (a5e8aa74) - the "
        "server delivers content as the EXACT raw handoff text, with NO Markdown "
        "code fence, header, or blockquote added around it (earlier versions wrapped "
        "it in a 4-backtick fence under 5234877f; that wrapping was removed because it "
        "broke copy-paste fidelity for the /goal trust protocol - see "
        "format_handoff_mcp_content in meridian/handoff.py). Output the field value "
        "as-is, as the sole plain-text bubble - do NOT add your own fence, header, "
        "blockquote, or any other wrapping on the calling side either. "
        "Do NOT just narrate that the handoff succeeded; paste the actual text. "
        "Also returns capability_contract (98aaccf4) on every mode: a machine-readable "
        "{requested, effective, availability, manifest_hash, executable, "
        "executable_reasons, generated_at} object describing the project's declared "
        "capabilities and whether an executor can run right now — null if contract-"
        "building failed. Every executor-facing /goal payload also includes an "
        "explicit <executor_item_ids> manifest containing every claimable item ID "
        "in deterministic order; receivers must use that manifest rather than "
        "parsing presentation prose or a truncated starter preview. Also returns "
        "scope (b8f89491) on every mode: "
        "{requested_version, effective_version, session_id} — which sprint-version "
        "bucket the handoff actually resolved to (explicit version arg wins over the "
        "session's own stored sprint_version; both null means genuinely unscoped, "
        "every version). Every mode's /goal text (full/delta/starter/goal, embedded in "
        "content or returned bare) also carries a structured <execution_policy "
        "execution_mode=... max_planning_turns=... required_first_action=... no_confirmation=... "
        "permitted_parallel_wave=... claim_before_edit=...> tag (75ac1c8e) right after "
        "<executor_directive> — the SAME canonical policy start_session's execution_policy "
        "field returns, so a receiver can identify the required first action from the tag "
        "attributes without interpreting prose. "
        "Also returns handoff_evidence_status (8a883f60) on every mode: an explicit "
        "{code_pointer_enrichment, resolved_pointer_annotation, freshness_requery, "
        "wave_gate_exclusion, graph_search_availability} object — each a "
        "{status: verified|skipped|failed|degraded, reason, fallback} entry for that "
        "best-effort step, so a silently-degraded handoff is never indistinguishable "
        "from a fully-verified one. Pass strict_evidence=true to fail CLOSED instead: "
        "if any capability comes back failed/degraded, nothing is rendered or persisted "
        "and the call returns {error: HANDOFF_EVIDENCE_BLOCKED, evidence_status, "
        "evidence_errors, message} — default (strict_evidence omitted/false) behavior is "
        "completely unchanged. "
        "Also returns continuation_status (ecc8b280) for full/delta modes: a "
        "{continuation_required, terminal_ready, execution_mode, actionable_count, "
        "actionable_pending_count, actionable_in_progress_count, actionable_item_ids, "
        "blocked_count, blocked_item_ids, reason} object reporting whether actionable "
        "pending/in_progress work remains on the live, version-scoped board with no "
        "recorded blocker_kind, while execution_mode=autonomous — the machine-readable "
        "signal that an autonomous session may NOT yet treat itself as finished. Pass "
        "checkpoint=true when THIS call is a mid-run progress report, not a final "
        "session-ending handoff — a checkpoint is never blocked by the gate below. Pass "
        "strict_continuation=true to fail CLOSED instead of just reporting: if "
        "continuation_required is true and checkpoint is not set, nothing is rendered "
        "or persisted and the call returns {error: HANDOFF_CONTINUATION_BLOCKED, "
        "continuation_status, message} — resolve/claim the remaining item(s), record a "
        "genuine blocker_kind on them, or call again with checkpoint=true. Default "
        "(strict_continuation omitted/false) behavior never blocks — continuation_status "
        "is still always returned so a caller can act on it voluntarily. "
        "Also supports selected_item_ids (cffb9323) — an explicit INCLUDE-ONLY item scope "
        "for safe parallel-follow-up handoffs. force_include_ids only ever WIDENS the "
        "pending list (re-adds specific deferred ids); selected_item_ids NARROWS it: when "
        "given, generate_handoff resolves a dependency-closed scope (the requested ids "
        "plus any transitively-required depends_on ancestor still todo/pending) and "
        "applies it identically across every executable mode (full/delta/starter/goal), so "
        "an isolated two-item follow-up handoff never emits the rest of the eligible "
        "version backlog or overlaps an active wave/batch a sibling session already owns. "
        "The rendered /goal carries a <selected_item_scope requested=... closure=... "
        "closure_hash=...> tag stating the exact selected ids and the wave plan — embedded "
        "in the body BEFORE the provenance token is minted, so the selection is bound into "
        "the SAME body-hash/token-integrity mechanism (efaa918a) as the rest of the /goal "
        "block. Fails CLOSED, not silently widened: a missing/foreign/wrong-version/"
        "already-in_progress/otherwise-non-pending requested id raises a structured "
        "refusal (error=HANDOFF_SELECTION_BLOCKED, selection_rejected) — nothing is "
        "rendered or persisted for that call. (7a373f41) A selection that validates "
        "cleanly but collapses to zero executable items once the manual/backburner/"
        "unprospected/wave-gate exclusion filters run instead refuses with "
        "error=HANDOFF_SCOPE_NON_EXECUTABLE, requested_ids, and an excluded_requested "
        "reason list — this same contract is identical across every connector surface "
        "(hosted HTTP MCP, stdio, and the REST /handoff route).",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"}, "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "mode": {"type": "string", "enum": ["full", "delta", "planner", "starter", "goal"], "description": "(aec043cb) Optional — omitting mode is now INTENT-BASED, never a silent 'full'. Omission resolves to: 'delta' if session_id already produced a handoff this session (resumed/continuation); else 'goal' if session_id was started with role='executor'; else 'planner' if role='planner'; else 'goal' (the safe, bounded default — no workspace decisions/notes, no other project's state) when intent can't otherwise be determined. 'full' — the unbounded, whole-workspace archival/diagnostic dump, including cross-project workspace decisions/notes — is now returned ONLY for an explicit mode='full' request, never for an omitted one. (d2fc7465) Persistence differs by mode too, and is now explicit on the response: only 'full'/'delta'/'goal' write to the `handoffs` history table and the trusted pending_goal channel load_handoff() reads back — 'planner'/'starter'/'compact' are call-and-forget renders meant to be pasted directly, never the canonical stored handoff. The response's `retrievable_via_load_handoff` field states this per-call rather than requiring a caller to infer it from mode name."},
         "session_id": {"type": "string", "description": "Optional session id for auto-delta on repeated calls in the same session."},
         "root_dir": {"type": "string", "description": "Optional request-local absolute source-tree root used by live pointer resolution's local semantic fallback when no code tunnel is available. Never persisted."},
         "version": {"type": "string", "description": "(b8f89491) Optional explicit sprint-version bucket (e.g. 'v0.2.6') to scope this handoff to — applies to every mode (full/delta/starter/compact/goal), not just starter. Wins over the calling session's own stored sprint_version. Omit to fall back to session_id's scope, or to the whole project's cross-version backlog when neither is set."},
         "force_include_ids": {"type": "array", "items": {"type": "string"}, "description": "(45f519a0, validated by 3cab355a) Optional list of sprint-item ids to force-include in the pending list even when their deferred_until is in the future. This is a one-off visibility override for this handoff call only — deferred_until is NOT cleared, so claim_sprint_item's own deferral gate is unaffected. Use when a human wants a backburnered item back in scope for one planning run without permanently re-enabling claiming. Every id is validated: it must belong to this project, match the effective version scope (when one applies), and be genuinely todo/pending — an unknown/cross-project/cross-version/not-pending id is rejected (reported in the response's force_include_rejected list, never silently dropped) rather than honoured. Accepted ids are also exempt from the code-pointer enrichment cap, so a requested item always gets prospected regardless of how large the pending board is."},
         "selected_item_ids": {"type": "array", "items": {"type": "string"}, "description": "(cffb9323) Optional explicit INCLUDE-ONLY item scope for a safe, isolated parallel-follow-up handoff — the opposite direction from force_include_ids (which WIDENS the pending list). When given, the pending batch on EVERY mode (full/delta/starter/compact/goal) is narrowed to exactly these ids plus their dependency closure (any depends_on ancestor still todo/pending in this project/version) — nothing else from the eligible backlog is included. Every requested id is validated (must exist, belong to this project, match the effective version scope when one applies, and be genuinely todo/pending — not already in_progress under another session, not done/failed/skipped): if ANY id fails validation, generate_handoff raises rather than silently falling back to the unfiltered backlog. The dependency-closure ids and a stable hash of that closure are rendered in a <selected_item_scope> tag inside the /goal block, bound into the same token body-hash as the rest of the content. (d2fc7465) The SAME closure ids/hash, plus which of the requested ids survived every downstream claimability filter (unprospected/backburner/manual/wave_gate_pending, each with a reason) and why, are ALSO returned as a structured `selected_scope` field on the response — the parse-free counterpart to the embedded tag, and the only place to learn about a PARTIAL exclusion (some, not all, requested ids dropped); a TOTAL exclusion instead raises HANDOFF_SCOPE_NON_EXECUTABLE. `selected_scope` is null when selected_item_ids was never passed."},
         "skip_ai_summary": {"type": "boolean", "description": "65c8b426 — skip the optional AI (Haiku) narrative calls (session summaries, ai_summary blurb, sprint retrospective). Default true on the MCP path for fast, reliable handoffs. Pass false to include AI-generated narrative sugar when you have budget and time."},
         "strict_evidence": {"type": "boolean", "description": "(8a883f60) Opt-in, off by default — mirrors complete_sprint_item's strict_evidence shape exactly. When true, a failed/degraded pointer-enrichment/freshness/wave-gate/graph-search capability makes this call refuse to render or persist a handoff at all, returning {error: HANDOFF_EVIDENCE_BLOCKED, evidence_status, evidence_errors, message} instead. Leave false/omitted for today's graceful-degrade behavior (handoff_evidence_status is still returned either way)."},
         "strict_pointer_evidence": {"type": "boolean", "description": "(eb8b6894) Opt-in, off by default, separate from strict_evidence above. When true, the claimable/goal batch's UNPROSPECTED exclusion requires a pending item's durable pointer(s) to have actually RESOLVED (resolve_pointer succeeded), not merely be PRESENT as a row — a structurally-valid-but-unresolved pointer no longer silently satisfies the gate. Never raises/blocks the whole handoff (unlike strict_evidence): an affected item is simply excluded from the claimable batch, the same way today's presence-only UNPROSPECTED gate already excludes items. Every pending item's pointer_resolution_status (structural_valid/target_resolved/provenance_verified/resolution_source/strict_satisfied) is always returned regardless of this flag — it only changes which items make the claimable cut."},
         "checkpoint": {"type": "boolean", "description": "(ecc8b280) Mark THIS call as a mid-run progress report rather than a final, session-ending handoff. Applies to full/delta modes only. A checkpoint=true call is never refused by strict_continuation below, regardless of how much actionable work remains — it changes nothing about what gets rendered, only whether the continuation gate can engage."},
         "strict_continuation": {"type": "boolean", "description": "(ecc8b280) Opt-in, off by default — mirrors strict_evidence's shape. When true and checkpoint is not set, refuses to render/persist this handoff (full/delta modes only) if actionable pending/in_progress items remain on the live board with no recorded blocker_kind while execution_mode=autonomous, returning {error: HANDOFF_CONTINUATION_BLOCKED, continuation_status, message} instead. Leave false/omitted for today's behavior (continuation_status is still always returned either way)."},
         "emit_manifest": {"type": "boolean", "description": "(acf6f51a) Opt-in, off by default. mode='goal' only (for now): when true, embeds a canonical <handoff_manifest> XML block — schema_version, board_revision (a deterministic digest of every item's id/status/depends_on), project/tenant origin identity, generated_at, the selected/closure item ids, the full item id/status/depends_on/resources list, and the wave plan — into the rendered /goal text BEFORE the goal token is minted, so verify_handoff_token's existing body_hash check also covers the manifest; no separate verification path. A receiver re-fetches the live board and compares against board_revision (see handoff.verify_board_revision) to detect drift before acting. Other modes are unaffected by this flag for now."}},
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
    {"name": "verify_handoff_token", "description":
        "Read-only: Verify a handoff provenance token (dd07ece0). When a /goal block "
        "is copy-pasted into chat rather than delivered via the trusted MCP channel "
        "(start_session pending_goal / load_handoff), a receiving session can call "
        "this tool to independently confirm the <goal_token> line was produced by a "
        "real generate_handoff call on this server — not injected or spoofed text. "
        "The token is single-use and short-lived (a few minutes); verify immediately "
        "on receipt. Returns {valid: bool, reason: str}. reason is 'ok' on success; "
        "on failure: 'not_found', 'expired', 'already_consumed', 'wrong_project', or "
        "'body_mismatch'. "
        "efaa918a body-hash binding (closes the 2ee0000c gap): pass presented_body "
        "— the FULL pasted block, token and SECURITY banner included — and this tool "
        "strips those back out and checks the remaining text against the body hash "
        "bound at mint time. A genuine token re-attached to a DIFFERENT (edited) body "
        "now returns 'body_mismatch' instead of a false 'ok'. Omitting presented_body "
        "preserves the exact prior token-only provenance check. "
        "f46372e8: every non-'ok' result also carries a structured recovery object "
        "{signal, message, next_step, next_step_hint} telling you what to do next — "
        "next_step is 'load_handoff' (fetch the canonical stored handoff; not_found/ "
        "wrong_project/body_mismatch) or 'cross_check_live_board' (re-derive the task "
        "list from get_sprint_items across all non-done statuses; already_consumed/ "
        "expired) — so you don't have to improvise a recovery path per failure reason.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string",
             "description": "The project_id the /goal block claims to be for."},
         "project_name": {"type": "string",
             "description": "Project name — an alternative to project_id; resolved to the id internally."},
         "token": {"type": "string",
             "description": "The token value from the <goal_token>…</goal_token> line in the /goal block."},
         "presented_body": {"type": "string",
             "description": "Optional: the full pasted /goal block (token + SECURITY banner included) to check against the token's stored body_hash, if any. Closes the 2ee0000c body-integrity gap — see description."}},
         "required": ["token"]}},
    {"name": "accept_handoff", "description":
        "Read-only: (1bd5e810) Canonical receiver-side acceptance check for a handoff "
        "envelope — composes token verification, capability/tool availability, "
        "tool-manifest drift, and board-revision divergence into ONE structured "
        "verdict, so MCP/HTTP/stdio all produce identical results for identical "
        "input (same underlying meridian.handoff.accept_handoff_envelope every "
        "transport calls). Every input is optional and independently gated — supply "
        "whatever you have; an omitted check is skipped, never failed. "
        "Returns {accepted: bool, result: 'ok'|'STALE_HANDOFF'|'FOREIGN_PROJECT_CONFIG'|"
        "'BOARD_DIVERGENCE'|'TOOL_MANIFEST_DRIFT'|'BODY_HASH_MISMATCH'|"
        "'CAPABILITY_UNAVAILABLE', reasons: [str], token_check, identity_check, "
        "capability_check, tool_manifest_check, board_check, "
        "is_trusted_channel: false, delivery_source: str}. Checks run in this "
        "order, short-circuiting on first failure: "
        "(1) token — token/presented_body via the same verify_handoff_token check; "
        "a body_mismatch reason maps to BODY_HASH_MISMATCH, every other invalid "
        "reason (not_found/wrong_project/already_consumed/expired) maps to "
        "STALE_HANDOFF — the raw token_check.reason sub-field always preserves "
        "which one, since AGENTS.md treats not_found/wrong_project as real spoofing "
        "signals and already_consumed/expired as usually just a sibling session "
        "having already acted. (2) identity binding (22f2604d) — presented_body's "
        "own <project_start_config> tag vs THIS call's project_id/"
        "expected_repo_path, via meridian.handoff.check_project_start_config_identity; "
        "runs whenever step (1) did not already reject the envelope on its own "
        "basis — i.e. token verification passed or no token was presented — so a "
        "body whose embedded identity disagrees with project_id is "
        "FOREIGN_PROJECT_CONFIG even when the token itself verified ok. This catches "
        "a genuine token paired with a foreign project's start-config, which step "
        "(1)'s wrong_project check alone cannot (that only catches a token minted "
        "for a DIFFERENT project_id, not a body whose own tag disagrees with a "
        "token that legitimately matches project_id). It does NOT re-run after "
        "step (1) already failed (STALE_HANDOFF/BODY_HASH_MISMATCH) — that failure "
        "is independently sufficient to reject the envelope. "
        "(3) capability — required_tools vs available_tools: "
        "any required name missing from available_tools is CAPABILITY_UNAVAILABLE. "
        "(4) tool-manifest drift — expected_required_tools_hash vs a hash computed "
        "live from live_items' own tool_requirements fields (see "
        "meridian.handoff.compute_required_tools_hash): mismatch is "
        "TOOL_MANIFEST_DRIFT. (5) board revision — expected_board_revision "
        "(acf6f51a's manifest <handoff_manifest board_revision=...>) vs a hash "
        "computed live from live_items via meridian.handoff.compute_board_revision: "
        "mismatch is BOARD_DIVERGENCE. live_items is YOUR OWN get_sprint_items(...) "
        "result — this tool never queries the board itself, so you control exactly "
        "which project/version/status filter \"live\" means; pass the same filter "
        "used when the compared handoff/manifest was generated. "
        "is_trusted_channel is always false here (calling this tool at all means "
        "verifying something other than the trusted pending_goal/load_handoff "
        "channel — see those tools' own docs). "
        "Scope note: this is a validation/report tool, not a hard gate — it is not "
        "wired into claim_sprint_item in this pass.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"}, "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "goal_token": {"type": "string", "description": "Optional: the token value from the <goal_token>…</goal_token> line in the /goal block being accepted."},
         "presented_body": {"type": "string", "description": "Optional: the full pasted /goal block (token + SECURITY banner included), checked against the token's stored body_hash AND against project_id/expected_repo_path via its own <project_start_config> tag — same contract as verify_handoff_token's presented_body, plus the 22f2604d identity-binding check."},
         "live_items": {"type": "array", "items": {"type": "object"}, "description": "Optional: your own get_sprint_items(...) result (the exact items/filter the compared handoff/manifest was generated from) — required for the tool-manifest-drift and board-revision checks; omit to skip both."},
         "expected_board_revision": {"type": "string", "description": "Optional: the board_revision value from a manifest's <handoff_manifest board_revision=\"...\"> attribute, or any prior meridian.handoff.compute_board_revision(...) result to compare live_items against."},
         "expected_required_tools_hash": {"type": "string", "description": "Optional: a prior meridian.handoff.compute_required_tools_hash(...) result to compare against live_items' current tool_requirements."},
         "required_tools": {"type": "array", "items": {"type": "string"}, "description": "Optional: tool names the handoff declared as required. Paired with available_tools to detect CAPABILITY_UNAVAILABLE."},
         "available_tools": {"type": "array", "items": {"type": "string"}, "description": "Optional: tool names actually available to you right now (e.g. from a live tools/list). Paired with required_tools."},
         "expected_repo_path": {"type": "string", "description": "Optional (22f2604d): YOUR OWN independently-known repo root (e.g. from your own meridian.toml/cwd) — never a value read out of presented_body itself. Compared against presented_body's <project_start_config repo_path=...>; a disagreement is FOREIGN_PROJECT_CONFIG."},
         "delivery_source": {"type": "string", "description": "Optional (22f2604d): a label for how you received this content (default 'chat_paste'). Echoed back verbatim; purely informational bookkeeping alongside the always-false is_trusted_channel."}},
         "required": []}},
    {"name": "record_handoff_correction", "description":
        "3af86d28 — record a corrective handoff when a blocked executor session "
        "reaches a wall after receiving a handoff (its evidence/scope no longer "
        "holds, a pointer stopped resolving, a required capability went away, "
        "etc.). Links to the immutable source_handoff_id (never mutated), "
        "classifies the blocker, and carries structured investigation evidence, "
        "added/removed/superseded pointers, and changed resources. Records "
        "status='draft' by default (or pass status explicitly). Pass "
        "idempotency_key to make retries safe — a repeat call with the same "
        "key returns the existing correction unchanged rather than duplicating "
        "it. Pass regenerate=true to ALSO, in this same call: re-resolve every "
        "added pointer live (repair), mark the source handoff invalidated/"
        "non-executable (its body is left untouched, for audit), and produce a "
        "new deterministic handoff revision (new body hash + provenance token) "
        "via the normal generate_handoff renderer. Idempotent: a correction "
        "that already produced a revision returns that same result again "
        "instead of regenerating a second time. A receiving executor should "
        "load the result via load_handoff (its 'correction' field) or a "
        "direct get_sprint — not by reconstructing the correction from "
        "log_task/note text. No DOCX or canonical project mutation is implied "
        "by recording a correction.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"}, "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "source_handoff_id": {"type": "string", "description": "The immutable handoffs.id row this correction is for (e.g. load_handoff's handoff.id is NOT the row id directly — use a handoff id you have from generate_handoff/get_handoffs)."},
         "blocker_classification": {"type": "string", "enum": ["evidence_invalid", "scope_stale", "pointer_unresolved", "dependency_missing", "environment_blocked", "capability_unavailable", "other"], "description": "Controlled vocabulary for why the handoff needed correcting."},
         "session_id": {"type": "string", "description": "The blocked executor session recording this correction."},
         "investigation_evidence": {"description": "Free-form JSON: what you found during investigation."},
         "added_pointers": {"type": "array", "items": {"type": "object"}, "description": "Pointer dicts (same shape as add_sprint_item_pointer's source_type/targets/label) asserting new evidence. Re-resolved live when regenerate=true."},
         "removed_pointers": {"type": "array", "items": {}, "description": "Pointer dicts or ids being removed as evidence."},
         "superseded_pointers": {"type": "array", "items": {}, "description": "Pointer dicts or ids whose premise was replaced."},
         "changed_resources": {"type": "array", "items": {"type": "string"}, "description": "File/resource paths that changed since the source handoff was rendered."},
         "requested_scope": {"description": "Free-form JSON describing the scope the ORIGINAL handoff asked for, for comparison against what actually got emitted."},
         "version": {"type": "string", "description": "Sprint-version bucket in scope, if any — also used to scope the regenerated revision when regenerate=true."},
         "source_token": {"type": "string", "description": "Optional: the <goal_token> value from the original /goal block, preserved for audit only (not re-verified by this call)."},
         "idempotency_key": {"type": "string", "description": "Optional caller-supplied dedup key so a retried call returns the existing correction instead of duplicating it."},
         "status": {"type": "string", "enum": ["draft", "verified", "superseded", "blocked"], "description": "Initial status. Default draft."},
         "regenerate": {"type": "boolean", "description": "When true, also repairs pointers, invalidates the source handoff, and produces a new deterministic revision in this SAME call. Default false (record only)."},
         "mode": {"type": "string", "enum": ["full", "delta", "planner", "starter", "goal"], "description": "Only used when regenerate=true — forwarded to generate_handoff."},
         "output_dir": {"type": "string", "description": "Only used when regenerate=true; defaults to the server's data directory."}},
         "required": ["source_handoff_id", "blocker_classification"]}},
    {"name": "export_ai_log", "description":
        "c0168425 — Read-only: project-scoped, receipted export of ai_log_events "
        "(meridian.db.ai_log — the append-only ExecutionEvent log). Nothing "
        "captures events into this table automatically yet (see meridian.ai_log's "
        "module docstring); this exports whatever has been recorded via "
        "append_event so far. Filter with session_id/event_type/correlation_id/"
        "parent_event_id exactly like list_events. limit defaults to 5000 and is "
        "capped at 5000 — the response's truncated field is true when more "
        "matching rows exist than were returned. Returns {project_id, "
        "exported_at, filters, event_count, truncated, events, export_hash} — "
        "export_hash is a sha256 over the exported events so a caller can "
        "independently verify nothing was altered in transit.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"}, "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "session_id": {"type": "string"}, "event_type": {"type": "string"},
         "correlation_id": {"type": "string"}, "parent_event_id": {"type": "string"},
         "limit": {"type": "integer", "description": "Default/max 5000."}},
         "required": []}},
    {"name": "export_ai_log_artifacts", "description":
        "c0168425 — Read-only: project-scoped, receipted export of stored ai_log "
        "artifacts (meridian.artifact_store — the local-first, content-addressed "
        "blob store an ExecutionEvent payload can point to via artifact_ref "
        "instead of inlining large content). Pass content_hashes to export an "
        "explicit subset (sha256:... values) — every requested hash must exist "
        "for this project, or the call errors rather than silently returning a "
        "shorter list; omit it to export every artifact currently stored for the "
        "project. Returns {project_id, exported_at, artifact_count, total_size, "
        "artifacts, export_hash} — each artifact entry includes its metadata plus "
        "base64-encoded content. export_hash covers the metadata only (not the "
        "base64 payloads) so it stays cheap to verify.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"}, "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "content_hashes": {"type": "array", "items": {"type": "string"}, "description": "Explicit subset of 'sha256:...' hashes to export. Omit to export every artifact stored for the project."}},
         "required": []}},
    {"name": "purge_ai_log", "description":
        "c0168425 — Project-scoped, cutoff-based retention sweep spanning BOTH "
        "ai_log_events (meridian.db.ai_log.purge_events_before) and their stored "
        "artifacts (meridian.artifact_store.purge_artifacts_before) in one call, "
        "with a single receipt. Deletes every event/artifact strictly older than "
        "cutoff (an ISO-8601 UTC datetime, e.g. '2025-01-01T00:00:00Z') for the "
        "given project. Irreversible — this is a hard bulk delete, not a soft "
        "archive (call export_ai_log / export_ai_log_artifacts first if the data "
        "needs to survive the sweep). Returns {project_id, cutoff, "
        "events_deleted, artifacts_deleted, purged_at}.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"}, "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "cutoff": {"type": "string", "description": "ISO-8601 UTC datetime, e.g. '2025-01-01T00:00:00Z'. Events/artifacts recorded strictly before this are deleted."}},
         "required": ["cutoff"]}},
    {"name": "get_context_block", "description":
        "Read-only: Return a compact project context block (north star, sprint, "
        "pending sprint items, recent tasks, recent decisions, active sessions) "
        "wrapped in a <meridian_context project_id=\"...\" mode=\"...\"> XML envelope "
        "for structured parsing by AI clients (v2.5+). "
        "The 'text' field in the response contains the XML-wrapped content. "
        "mode='full' (default) for Code Handoff into a fresh Claude Code session; "
        "mode='chat' for a shorter paste into a new claude.ai conversation. "
        "The HTTP route /projects/{id}/context-block returns the same content as "
        "unwrapped plain text.",
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
    {"name": "add_proposal_gate", "description":
        "Raise a typed, lane-blocking HITL gate for a materially ambiguous decision — "
        "legal/IP, product scope, destructive operations, production deployment, human "
        "acceptance of a contradiction, or other materially ambiguous decisions "
        "(category must be one of: legal_ip, product_scope, destructive_ops, "
        "production_deploy, contradiction_acceptance, other_ambiguous). Always starts "
        "state='blocked' (fail-safe) with no decision yet — routine read-only "
        "decomposition and bounded fallback work never needs a gate. affected is a "
        "non-empty list of sprint_item_id strings and/or generic pointer objects "
        "({source_type, targets:[...]})  naming exactly what this gate blocks. Resolve "
        "with resolve_proposal_gate once a human decides.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"}, "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "category": {"type": "string", "enum": ["legal_ip", "product_scope", "destructive_ops", "production_deploy", "contradiction_acceptance", "other_ambiguous"]},
         "question": {"type": "string", "description": "The materially ambiguous question this gate raises for human judgment."},
         "affected": {"type": "array", "items": {}, "description": "Non-empty list of sprint_item_id strings and/or generic pointer objects naming what this gate blocks."},
         "evidence": {"type": "string", "description": "Why this is ambiguous — the evidence that triggered raising the gate."},
         "created_by": {"type": "string", "description": "Who/what raised this gate. Defaults to session_id when omitted."},
         "session_id": {"type": "string"},
         "expires_at": {"type": "string", "description": "Optional ISO timestamp after which the decision lapses (see reopen_policy)."},
         "reopen_policy": {"type": "string", "enum": ["manual", "auto_on_expiry", "on_new_evidence"], "description": "manual (default): a decided gate stays decided until reopen_proposal_gate is called explicitly. auto_on_expiry: once expires_at passes, the gate reports 'blocked' again regardless of the last decision. on_new_evidence: same as manual, just a policy label for UIs."}},
         "required": ["category", "question", "affected", "evidence"]}},
    {"name": "resolve_proposal_gate", "description":
        "Record a human decision on a proposal gate: the lane's new state "
        "(blocked | quarantined | allowed), the free-text decision, and the actor who "
        "decided (decided_at is auto-stamped). Refuses with {error} if the gate was "
        "already decided and has not yet expired — call reopen_proposal_gate first. "
        "An expired prior decision is treated as lapsed and a fresh decision is "
        "accepted directly.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"}, "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "gate_id": {"type": "string"},
         "state": {"type": "string", "enum": ["blocked", "quarantined", "allowed"]},
         "decision": {"type": "string", "description": "Free-text explanation of the decision."},
         "actor": {"type": "string", "description": "Who made this decision."},
         "expires_at": {"type": "string", "description": "Optionally update the expiry; omit to leave unchanged."},
         "reopen_policy": {"type": "string", "enum": ["manual", "auto_on_expiry", "on_new_evidence"], "description": "Optionally update the reopen policy; omit to leave unchanged."}},
         "required": ["gate_id", "state", "decision", "actor"]}},
    {"name": "reopen_proposal_gate", "description":
        "Invalidate a still-standing proposal gate decision (e.g. new evidence "
        "surfaced) so resolve_proposal_gate can be called again. Resets the lane to "
        "'blocked' (fail-safe), snapshots the prior decision into previous_decision / "
        "previous_actor / previous_decided_at, and increments reopen_count. Refuses "
        "with {error} if the gate was never decided.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"}, "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "gate_id": {"type": "string"},
         "actor": {"type": "string"},
         "reason": {"type": "string"}},
         "required": ["gate_id", "actor", "reason"]}},
    {"name": "get_proposal_gates", "description":
        "Read-only: list proposal HITL gates for a project, optionally filtered by "
        "category and/or (raw, stored) state. Pass sprint_item_id to instead list only "
        "the gates currently blocking/quarantining that one item (an effective-state-"
        "aware view — an expired auto_on_expiry gate is included even if its stored "
        "state says 'allowed').",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"}, "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "category": {"type": "string", "enum": ["legal_ip", "product_scope", "destructive_ops", "production_deploy", "contradiction_acceptance", "other_ambiguous"]},
         "state": {"type": "string", "enum": ["blocked", "quarantined", "allowed"]},
         "sprint_item_id": {"type": "string", "description": "When given, returns only gates currently blocking/quarantining this sprint item (ignores category/state filters)."}},
         "required": []}},
    {"name": "checkpoint", "description":
        "Save progress mid-session. Runs auto_capture (buckets done tasks into a note), "
        "generates a delta handoff, and returns a compact summary with what was done, "
        "what's pending, and the suggested next /goal string (now the same canonical, "
        "token-embedded continuation block generate_handoff renders — verify it with "
        "verify_handoff_token exactly like any other /goal block). Call before context "
        "fills up or before ending a session.",
     "inputSchema": {"type": "object", "properties": {
         "session_id": {"type": "string"},
         "project_id": {"type": "string"}, "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "version": {"type": "string", "description": "(455cfc36) Optional explicit sprint-version bucket (e.g. 'v0.2.6') to scope this checkpoint to — wins over the calling session's own stored sprint_version, exactly like generate_handoff's own version kwarg. Omit to fall back to the session's resolved scope (unchanged default behavior)."}},
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
    {"name": "request_manual_issue_screening_toggle", "description":
        "5dfe34b2 — request enabling/disabling the OFF-by-default opt-in extension "
        "that lets the automated GitHub-issue comment/propose flow (never auto-close) "
        "also act on issues Meridian did not itself create, gated behind hardcoded "
        "content screening. enable=true ALWAYS files a require_human=true HITL "
        "(kind/require_human are hardcoded — this tool cannot be used to self-escalate; "
        "only a genuine human answering in the dashboard/API can enable it). "
        "enable=false disables immediately with no HITL (fail-safe direction) and is "
        "audit-logged either way.",
     "inputSchema": {"type": "object", "properties": {
         "enable": {"type": "boolean", "description": "true to request enabling (files a human-only HITL); false to disable immediately."},
         "project_id": {"type": "string", "description": "Optional — a project to file the enable-request HITL under; defaults to a workspace-level request."},
         "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "session_id": {"type": "string"},
         "context": {"type": "string"}},
         "required": ["enable"]}},
    {"name": "link_manual_github_issue", "description":
        "5dfe34b2 — attempt to link a manually-filed GitHub issue (one Meridian did "
        "NOT create) to a sprint item, extending fdaa5b55's automated comment/propose "
        "flow to it. No-ops safely (action='skipped') unless "
        "manual_issue_screening_enabled is on. When enabled: reads the issue's raw "
        "content, logs it (hashed, append-only) before any processing, runs a "
        "wave-relative velocity/anomaly check (non-blocking escalation only), then "
        "screens title/body/comments for hardcoded injection shapes — flagged content "
        "is never auto-linked (a human-review HITL is filed instead); only "
        "screening-clean content gets linked (github_issue_source='manual'). Linking "
        "NEVER by itself closes the issue — fdaa5b55's existing propose+HITL flow "
        "still applies at sprint-item completion time.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"},
         "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "item_id": {"type": "string", "description": "The sprint item to link the issue to."},
         "issue_number": {"type": "integer"},
         "session_id": {"type": "string"}},
         "required": ["item_id", "issue_number"]}},
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
    {"name": "index_equation", "description":
        "06df6ab3 — index ONE Word equation (OMML) against a document already "
        "stored in the doc-structure store — populated by ingest_document (which "
        "registers a docx/latex document's structure here in addition to storing "
        "the flat note text). Pass the SAME source/path you ingested "
        "under as `doc`. "
        "omml_or_latex is auto-detected: a string starting with '<' is treated "
        "as raw OMML XML (stored as-is); anything else is treated as LaTeX "
        "source (real OMML is generated best-effort — pure-Python latex2mathml "
        "piped through a hand-written MathML->OOXML mapper; returns null omml "
        "on an unsupported construct, never an error). Before inserting, the "
        "normalized LaTeX is fuzzy-matched against every equation already "
        "stored for this document — a near-duplicate is NOT silently dropped "
        "(the equation is still inserted) but IS surfaced via "
        "near_duplicates:[{equation_id, matched_id, matched_latex, score}] so "
        "you can spot accidental re-derivations. Returns {equation, "
        "near_duplicates}.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"},
         "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "doc": {"type": "string", "description": "The stored document's source (the path/URL you ingested it under via ingest_document, which registers a docx/latex document in the doc-structure store)."},
         "omml_or_latex": {"type": "string", "description": "Raw OMML XML (starts with '<') OR a LaTeX source string."},
         "semantic_label": {"type": "string", "description": "Optional human label for the equation (e.g. 'mass-energy equivalence')."}},
         "required": ["doc", "omml_or_latex"]}},
    {"name": "find_similar_equation", "description":
        "06df6ab3 — fuzzy-match a LaTeX string against every equation already "
        "indexed (via index_equation) for one stored document, "
        "best match first. Each result carries the stored equation row PLUS a "
        "difflib similarity score (0..1) against its latex_normalized. Useful "
        "before index_equation to check whether an equation is already present "
        "under a slightly different LaTeX spelling. Returns {document_id, "
        "matches:[...]} — an empty list (never an error) when the document has "
        "no stored equations, or doc doesn't resolve to a stored document.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"},
         "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "doc": {"type": "string", "description": "The stored document's source (the path/URL you ingested it under via ingest_document, which registers a docx/latex document in the doc-structure store)."},
         "latex": {"type": "string", "description": "LaTeX source to fuzzy-match against this document's stored equations."},
         "limit": {"type": "integer", "description": "Max matches to return (default 5)."}},
         "required": ["doc", "latex"]}},
    {"name": "insert_equation", "description":
        "51a595e7 — write an OMML equation DIRECTLY into a stored document's "
        "source .docx (real OOXML write-back), collapsing the manual "
        "resolve->open->parse->splice->rewrite->reindex flow into one call. The "
        "document must already be stored in the doc-structure store via "
        "ingest_document (which registers a docx/latex document's structure here) "
        "AND have a filesystem `source` path to write back to. Locate the target "
        "paragraph by `para_id` — the paragraph's w14:paraId (or the synthesized "
        "'p{index}' id that get_document_structure / find_similar_equation surface "
        "as element_id). equation_id_or_omml is resolved in order: the id of an "
        "equation already indexed for THIS document (its stored OMML is reused); "
        "else a string starting with '<' is raw OMML XML; else a LaTeX source "
        "(converted best-effort via latex2mathml -> MathML -> OOXML). position "
        "controls placement: 'append' (default) drops the <m:oMath> inline at the "
        "end of the paragraph; 'before'/'after' add it as its own display-equation "
        "paragraph adjacent to the target. After the write the document's equation "
        "index is resynced from the modified file (no separate re-verify step). "
        "Returns {document_id, source, para_id, position, omml, resync} on "
        "success, or {error} for a bad para_id / unresolvable equation / missing "
        "file (the file is never mutated when resolution fails).",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"},
         "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "doc": {"type": "string", "description": "The stored document's source (the path you ingested it under via ingest_document; must resolve to a .docx on disk)."},
         "para_id": {"type": "string", "description": "Target paragraph id — its w14:paraId, or the synthesized 'p{index}' id surfaced as element_id by the read tools."},
         "equation_id_or_omml": {"type": "string", "description": "An existing indexed equation id (reuses its OMML), OR raw OMML XML (starts with '<'), OR a LaTeX source string."},
         "position": {"type": "string", "enum": ["append", "before", "after"], "description": "Where to place the equation relative to the paragraph. Default 'append' (inline, end of paragraph)."}},
         "required": ["doc", "para_id", "equation_id_or_omml"]}},
    {"name": "update_paragraph", "description":
        "f978e588 — ID-addressable docx WRITE (the write counterpart of the "
        "get_element_by_id / paraId read primitive). Targets ONE paragraph in a "
        "stored .docx by its w14:paraId (the 'p{index}' fallback Word writes for "
        "an unlabelled paragraph) — NEVER by text match — rewrites its runs, "
        "saves the .docx in place, and re-syncs the doc_elements index row so it "
        "matches the new text. The document must already be stored in the "
        "doc-structure store via ingest_document (which registers a docx/latex "
        "document's structure here). Pass the SAME source/path you ingested "
        "under as `doc`. Provide EXACTLY ONE of: `new_text` (a "
        "plain string — one unformatted run) OR `runs` (a list of runs, each a "
        "bare string or {text, bold?, italic?, underline?} — basic run formatting "
        "is applied; the paragraph's original run formatting is replaced, not "
        "merged; its paragraph style/numbering is preserved). Returns "
        "{document_id, para_id, new_text, elements_resynced, source_path}. "
        "elements_resynced is 0 for a plain body paragraph (only headings are "
        "persisted as elements) — that is expected, not a failure. Errors "
        "(never a silent no-op) when the doc/source/para_id doesn't resolve. "
        "f7ee1ba7 — pass session_id to enable scoped-region claim enforcement: "
        "if another session has claimed the target para_id (or holds a whole-file "
        "lock), the write is REJECTED with error='docx_region_conflict'. Use "
        "claim_docx_region to acquire your region before writing. "
        "5988a5bb — mandatory post-write verification now re-reads the file "
        "from disk and confirms the target paragraph's text actually landed "
        "before this ever reports success; on a rare verification failure the "
        "write is best-effort restored from backup and an error is returned "
        "instead. Response also now includes pre_counts/post_counts (the "
        "media/style/equation/relationship structural manifest from before "
        "and after the write). Three further OPT-IN parameters (each omitted "
        "by default, byte-identical behavior when omitted): "
        "expected_content_hash — a fail-closed precondition: if the source "
        "file's current on-disk content hash doesn't match, the write is "
        "REJECTED before anything is touched (get the current hash from a "
        "prior get_document_structure/get_structure staleness check). "
        "draft_output_path + wave_run_id (both-or-neither, with session_id "
        "also required) — writes to an ISOLATED draft path instead of the "
        "canonical file, claiming the paragraph as this wave's anchor via the "
        "real docx-merge manifest so a conflicting concurrent draft on the "
        "same paragraph is rejected; response carries draft_path/wave_run_id/ "
        "is_draft instead of elements_resynced (the canonical index is not "
        "touched until a merge).",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"},
         "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "doc": {"type": "string", "description": "The stored document's source (the path/URL you ingested it under via ingest_document, which registers a docx/latex document in the doc-structure store)."},
         "para_id": {"type": "string", "description": "The target paragraph's w14:paraId (or 'p{index}' fallback), as reported by the read side."},
         "new_text": {"type": "string", "description": "New paragraph text as a single unformatted run. Provide this OR runs, not both."},
         "runs": {"type": "array", "description": "List of runs — each a plain string or a {text, bold?, italic?, underline?} object. Provide this OR new_text, not both.",
                  "items": {"type": ["string", "object"]}},
         "session_id": {"type": "string", "description": "f7ee1ba7 — calling session id. When provided, scoped-region claim enforcement activates: the write is rejected if another session claims the target para_id or holds a whole-file lock. Without session_id the guard is skipped (legacy/unclaimed writes pass through). 5988a5bb — also required (together with draft_output_path/wave_run_id) to use wave-scoped draft mode."},
         "expected_content_hash": {"type": "string", "description": "5988a5bb — opt-in fail-closed precondition: the write is rejected BEFORE touching the file if this doesn't match the source's CURRENT on-disk content hash. Omit for the pre-5988a5bb advisory-only staleness warning instead."},
         "draft_output_path": {"type": "string", "description": "5988a5bb — opt-in wave-scoped draft mode: write to this isolated path instead of the canonical `doc`. Must be given together with wave_run_id and session_id; must differ from `doc`."},
         "wave_run_id": {"type": "string", "description": "5988a5bb — the wave identifier scoping this draft's meridian.db.docx_merge manifest. Must be given together with draft_output_path and session_id."}},
         "required": ["doc", "para_id"]}},
    {"name": "find_symbol_usages", "description":
        "9605edb0 — READ-ONLY cross-reference tracking: given a document and "
        "EITHER a doc_equations row id OR a symbol / normalized-LaTeX string, "
        "resolve it to ONE target normalized-LaTeX (an equation id uses that "
        "row's stored latex_normalized as-is; a raw string is normalized with "
        "the SAME normalize_latex that produced every stored latex_normalized) "
        "and return every place that target reappears in the document — matching "
        "equations (exact normalized-latex equality) AND paragraphs whose text "
        "textually contains the symbol. Each hit carries element_id, "
        "document_id, ordinal, matched_text, context (equation|paragraph) and an "
        "is_definition/is_reuse flag: the EARLIEST occurrence by ordinal is the "
        "definition, later ones are reuse — so a later mention can be checked to "
        "point back to the definition instead of assuming the reader remembers "
        "it. Hits are ordered by ordinal (definition first). Returns "
        "{document_id, target, resolved_from, hits:[...]} — an empty hits list "
        "(never an error) when nothing matches, or doc doesn't resolve to a "
        "stored document.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"},
         "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "doc": {"type": "string", "description": "The stored document's source (the path/URL you ingested it under via ingest_document, which registers a docx/latex document in the doc-structure store)."},
         "symbol_or_equation_id": {"type": "string", "description": "A doc_equations row id, OR a raw symbol / normalized-LaTeX string to track (e.g. 'E=mc^2' or '\\\\sigma')."}},
         "required": ["doc", "symbol_or_equation_id"]}},
    {"name": "index_figure", "description":
        "c623e648 — index ONE figure into the SEMANTIC figure index against a "
        "document already stored in the doc-structure store — populated by "
        "ingest_document (which registers a docx/latex document's structure here "
        "in addition to storing the flat note text). Pass the SAME "
        "source/path you ingested under as `doc`. This is the figure parallel "
        "of index_equation and is "
        "COMPLEMENTARY to the structural kind='figure' section-tree placement "
        "(it adds caption dedup + similarity, it does not replace placement). "
        "Provide file_path and/or caption. Before inserting, the normalized "
        "caption is fuzzy-matched against every figure already indexed for this "
        "document — a near-duplicate is NOT silently dropped (the figure is "
        "still inserted) but IS surfaced via near_duplicates:[{figure_id, "
        "matched_id, matched_caption, score}] so you can spot an accidental "
        "re-index. The referenced file_path is checked on disk: a missing file "
        "is FLAGGED (file_exists on the row + a missing_files entry), never a "
        "hard failure. Returns {figure, near_duplicates, missing_files}.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"},
         "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "doc": {"type": "string", "description": "The stored document's source (the path/URL you ingested it under via ingest_document, which registers a docx/latex document in the doc-structure store)."},
         "file_path": {"type": "string", "description": "Path to the figure's asset on disk (checked for existence; missing is flagged, not fatal)."},
         "caption": {"type": "string", "description": "The figure's caption (drives normalized-caption dedup/similarity)."},
         "semantic_label": {"type": "string", "description": "Optional human label for the figure (e.g. 'apparatus diagram')."}},
         "required": ["doc"]}},
    {"name": "find_similar_figure", "description":
        "c623e648 — fuzzy-match a free-text description OR a file path against "
        "every figure already indexed (index_figure) for one stored document, "
        "best match first. Each result carries the stored figure row PLUS a "
        "difflib similarity score (0..1) — the better of the match against its "
        "normalized_caption and against its file_path. Useful before "
        "index_figure to check whether a figure is already present under a "
        "slightly different caption or path. Returns {document_id, matches:[...]} "
        "— an empty list (never an error) when the document has no indexed "
        "figures, or doc doesn't resolve to a stored document. "
        "d2a3537a — pass outputs_dir to RESOLVE THROUGH to the outputs index: "
        "every matched figure with a file_path that names an already-indexed run "
        "output gains a linked_output field (the output's path, generating_script, "
        "canonical/archival flag, fingerprint), so 'does this plot already exist "
        "as a run output?' and 'where is it referenced in my thesis?' are one "
        "lookup (linked_output is null when the figure names no indexed output).",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"},
         "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "doc": {"type": "string", "description": "The stored document's source (the path/URL you ingested it under via ingest_document, which registers a docx/latex document in the doc-structure store)."},
         "description_or_path": {"type": "string", "description": "A free-text description OR a file path to fuzzy-match against this document's indexed figures."},
         "outputs_dir": {"type": "string", "description": "d2a3537a — optional outputs tree root. When given, each matched figure resolves THROUGH to its outputs_index row (linked_output) by file_path. Omit for a pure fuzzy match."},
         "limit": {"type": "integer", "description": "Max matches to return (default 5)."}},
         "required": ["doc", "description_or_path"]}},
    {"name": "link_figure_caption", "description":
        "0ff8b982 — DURABLY link an already-indexed figure (doc_figures row) to "
        "its caption paragraph (a doc_elements id), by stable structural id "
        "rather than paragraph proximity. Use this to confirm an advisory "
        "suggested_caption_element_id returned by index_figure, or to "
        "backfill a durable link on a figure that was indexed before caption "
        "linkage was supported. Provide figure_id (the doc_figures.id of the "
        "figure to link) and caption_element_id (the doc_elements.id of the "
        "caption paragraph — a kind='figure' SEQ-field element from the "
        "section-tree store). This is the confirmation primitive for the "
        "'Figure 3b used twice' ambiguity scenario: when index_figure surfaces "
        "suggested_caption_candidates (multiple captions in the same section), "
        "inspect them and call this tool with the correct one to confirm the "
        "durable link. Returns the updated figure row on success, or {error} "
        "when figure_id doesn't resolve to a known figure.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"},
         "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "doc": {"type": "string", "description": "The stored document's source (the path/URL you ingested it under via ingest_document)."},
         "figure_id": {"type": "string", "description": "The doc_figures.id of the figure row to update (from index_figure or find_similar_figure)."},
         "caption_element_id": {"type": "string", "description": "The doc_elements.id of the caption paragraph to durably link to this figure."}},
         "required": ["doc", "figure_id", "caption_element_id"]}},
    {"name": "index_table", "description":
        "2622182d — index ONE table into the SEMANTIC table index against a "
        "document already stored in the doc-structure store — populated by "
        "ingest_document (which registers a docx/latex document's structure here "
        "in addition to storing the flat note text). Pass the SAME "
        "source/path you ingested under as `doc`. This is the table parallel "
        "of index_figure and is "
        "COMPLEMENTARY to the structural kind='table' section-tree placement "
        "(it adds caption dedup + similarity, it does not replace placement). "
        "Provide caption and/or table_index. Before inserting, the normalized "
        "caption is fuzzy-matched against every table already indexed for this "
        "document — a near-duplicate is NOT silently dropped (the table is "
        "still inserted) but IS surfaced via near_duplicates:[{table_id, "
        "matched_id, matched_caption, score}] so you can spot an accidental "
        "re-index. When paired_figure_id is omitted, the nearest figure in the "
        "same structural section is surfaced as suggested_figure_id (advisory, "
        "never auto-applied). Returns {table, near_duplicates}.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"},
         "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "doc": {"type": "string", "description": "The stored document's source (the path/URL you ingested it under via ingest_document, which registers a docx/latex document in the doc-structure store)."},
         "table_index": {"type": "integer", "description": "The table's document-order index (0-based or 1-based, your convention)."},
         "caption": {"type": "string", "description": "The table's caption (drives normalized-caption dedup/similarity)."},
         "semantic_label": {"type": "string", "description": "Optional human label for the table (e.g. 'results table')."},
         "paired_figure_id": {"type": "string", "description": "Optional: the doc_figures or doc_elements id of a related figure. When omitted, the nearest figure in the same structural section is suggested (advisory only)."}},
         "required": ["doc"]}},
    {"name": "find_similar_table", "description":
        "2622182d — fuzzy-match a free-text description against "
        "every table already indexed (index_table) for one stored document, "
        "best match first. Each result carries the stored table row PLUS a "
        "difflib similarity score (0..1) against its normalized_caption. Useful "
        "before index_table to check whether a table is already present under a "
        "slightly different caption. Returns {document_id, matches:[...]} "
        "— an empty list (never an error) when the document has no indexed "
        "tables, or doc doesn't resolve to a stored document.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"},
         "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "doc": {"type": "string", "description": "The stored document's source (the path/URL you ingested it under via ingest_document, which registers a docx/latex document in the doc-structure store)."},
         "description": {"type": "string", "description": "A free-text description to fuzzy-match against this document's indexed tables."},
         "limit": {"type": "integer", "description": "Max matches to return (default 5)."}},
         "required": ["doc", "description"]}},
    {"name": "link_table_caption", "description":
        "42d398a5 — DURABLY link an already-indexed table (doc_tables row) to "
        "its caption paragraph (a doc_elements id), by stable structural id "
        "rather than paragraph proximity. The table analogue of "
        "link_figure_caption. Use this to confirm an advisory "
        "suggested_caption_element_id returned by index_table, or to "
        "backfill a durable link on a table that was indexed before caption "
        "linkage was supported. Provide table_id (the doc_tables.id of the "
        "table to link) and caption_element_id (the doc_elements.id of the "
        "caption paragraph — a kind='table' SEQ-field element from the "
        "section-tree store). This is the confirmation primitive for the "
        "ambiguous-multi-candidate scenario: when index_table surfaces "
        "multiple caption candidates in the same section, inspect them and "
        "call this tool with the correct one to confirm the durable link. "
        "Returns the updated table row on success, or {error} "
        "when table_id doesn't resolve to a known table.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"},
         "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "doc": {"type": "string", "description": "The stored document's source (the path/URL you ingested it under via ingest_document)."},
         "table_id": {"type": "string", "description": "The doc_tables.id of the table row to update (from index_table or find_similar_table)."},
         "caption_element_id": {"type": "string", "description": "The doc_elements.id of the caption paragraph to durably link to this table."}},
         "required": ["doc", "table_id", "caption_element_id"]}},
    {"name": "ingest_document_structure", "description":
        "db42acce — persist pre-parsed structural data (headings/figures/tables) "
        "into the doc-structure store, keyed on the SAME source as "
        "ingest_document(content=...) so find_similar_figure / index_figure / "
        "index_table / index_equation see the correct document_id.\n\n"
        "Use this when the .docx lives on the caller's local machine (not on the "
        "Meridian server): call the tunnel-side ingest_local_document_structure "
        "tool (from the meridian-docs extension) which parses the file locally and "
        "forwards the blocks JSON here. The source must exactly match the source "
        "that ingest_document stored the flat note under (default: the local file "
        "path). Returns {document_id, source, doc_type, element_count}.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"},
         "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "source": {"type": "string", "description": "The source key (usually the local file path) matching what ingest_document stored the flat note under."},
         "blocks": {"type": "string", "description": "JSON-encoded list of body blocks from document_content_tree (the 'blocks' key) — headings, paragraphs, tables in document order. The server converts these to structural elements via elements_from_docx_content_tree."},
         "doc_type": {"type": "string", "description": "Document type: 'docx' (default) or 'latex'."},
         "title": {"type": "string", "description": "Document title (optional; stored for display)."}},
         "required": ["source", "blocks"]}},
    {"name": "search_outputs", "description":
        "a0e9133e — READ-ONLY full-text search over a run's OUTPUTS tree "
        "(numeric/tabular/array artifacts), backed by DuckDB native FTS (Okapi "
        "BM25). Walks outputs_dir recursively and builds a persistent index: "
        "each .csv/.json contributes its extracted TEXT content plus a cheap "
        "fingerprint (CSV column names / JSON top-level keys / an inferred "
        "generating_script); each .npy contributes METADATA ONLY (never array "
        "content); images/other binaries contribute filesystem metadata + name "
        "only. The multi-word query is scored with BM25 and ranked hits are "
        "returned. Canonical-vs-archival is handled TWO-STAGE and is NEVER "
        "destructive: a filename heuristic (_old / _old_N / leading underscore) "
        "flags a CANDIDATE, and a SHA-256 content hash CONFIRMS — an archival "
        "copy byte-identical to its canonical twin is DEPRIORITIZED in ranking "
        "(is_archival=true, canonical_path set), while a same-name-pattern file "
        "whose content DIFFERS is surfaced as its own distinct hit (never "
        "collapsed). Nothing is ever deleted or hidden from disk. Pass "
        "include_archival=false to drop archival hits entirely. Returns "
        "{outputs_dir, query, total_indexed, hits:[{path, score, bm25, "
        "is_archival, canonical_path, kind, generating_script, csv_columns, "
        "json_keys, size, mtime, annotations:[{path, note, run_params, "
        "created_at, updated_at, source}]}]}. annotations is auto-included "
        "for each hit (any annotation keyed to the hit path OR a nearest "
        "ancestor directory) — no second tool call needed. A missing dir / "
        "empty tree returns an empty hits list, never an error. "
        "3535b9ad — pass max_seconds to raise/lower the indexing budget "
        "(the \"indexing slider\"): a large or cold tree may not fully "
        "converge within the default budget on the first call — the result's "
        "partial=true field signals more indexing remains; call again to "
        "continue (each call resumes where the last left off, never restarts).",
     "inputSchema": {"type": "object", "properties": {
         "outputs_dir": {"type": "string", "description": "Absolute path to the outputs directory tree to index and search (walked recursively)."},
         "query": {"type": "string", "description": "The BM25 query — one or more search terms (column names, keys, script names, or any text in a csv/json)."},
         "limit": {"type": "integer", "description": "Max ranked hits to return (default 10)."},
         "include_archival": {"type": "boolean", "description": "Default true — archival copies are deprioritized but still returned. Set false to exclude confirmed-archival files entirely."},
         "max_seconds": {"type": "number", "description": "Wall-clock budget (seconds) for this call's incremental indexing before returning. Omit for the library default. Lower it for a faster first response on a huge tree (check partial=true and call again); raise it to converge in fewer calls on a tree too large for the default budget."}},
         "required": ["outputs_dir", "query"]}},
    {"name": "annotate_outputs", "description":
        "9e02e448 — capture a human annotation for a path inside an outputs "
        "tree WITHOUT touching the filesystem. Upserts a row into the "
        "annotations layer of the local DuckDB outputs index for outputs_dir. "
        "Two tiers, same mechanism: Tier 1 = pass outputs_dir as path to "
        "annotate the whole tree ('what this experiment tree is about'); "
        "Tier 2 = pass any sub-path (file or directory) to annotate a specific "
        "run, file, or subdirectory ('PCA on, BFS off, overwritten 5x'). "
        "run_params is an optional free-form dict of parameters logged alongside "
        "the note (e.g. {\"lr\": 0.001, \"batch_size\": 32}). Annotations are "
        "automatically surfaced in search_outputs results — any hit's path (or "
        "its nearest ancestor directory) that has an annotation will have it "
        "included in the hit's 'annotations' field without a second tool call. "
        "A MERIDIAN_NOTES.md file placed anywhere in the tree is also "
        "auto-ingested into the same table on every rebuild, keyed to its "
        "containing directory. Returns the stored annotation as a dict.",
     "inputSchema": {"type": "object", "properties": {
         "outputs_dir": {"type": "string", "description": "Absolute path to the outputs directory tree root (same value you pass to search_outputs)."},
         "path": {"type": "string", "description": "The path to annotate — either the outputs_dir root (Tier 1, tree-level annotation) or any file/subdirectory path within the tree (Tier 2, per-run or per-file annotation)."},
         "note": {"type": "string", "description": "The annotation text (e.g. 'PCA on, BFS off — results from run on 2026-07-12 with lr=0.001')."},
         "run_params": {"type": "object", "description": "Optional free-form key-value dict of run parameters to log alongside the note (e.g. {\"lr\": 0.001, \"epochs\": 100})."}},
         "required": ["outputs_dir", "path", "note"]}},
    {"name": "find_outputs_by_source", "description":
        "2ae25966 — READ-ONLY reverse provenance lookup over a run's OUTPUTS "
        "tree: the mirror image of resolve_figure_output's forward direction "
        "(figure -> source). Given a script or data file's source_path, scans "
        "the same local DuckDB outputs index search_outputs/annotate_outputs "
        "use for every indexed output whose recorded generating_script traces "
        "back to it — an exact (case/slash-insensitive) string match OR a "
        "basename match, so 'analysis/run.py' also matches an output recorded "
        "with generating_script='run.py'. This is the direction plain "
        "exact-path resolution can never answer, because that always starts "
        "from the output side: 'what did this script/data file produce?' — "
        "useful for auditing a stale Outputs_*_BACKUP folder mess by walking "
        "a source file's outputs forward, newest first, and comparing against "
        "what a document actually cites. Returns {outputs_dir, source_path, "
        "outputs:[{path, generating_script, is_archival, canonical_path, "
        "sha256, kind, size, mtime, csv_columns, json_keys}], total} sorted "
        "newest-first by mtime; total is the full match count before limit "
        "truncation. outputs is empty (not an error) when nothing in the "
        "tree cites this source, or when outputs_dir doesn't exist.",
     "inputSchema": {"type": "object", "properties": {
         "outputs_dir": {"type": "string", "description": "Absolute path to the outputs directory tree to index and search (same value you pass to search_outputs)."},
         "source_path": {"type": "string", "description": "The generating script or data file to trace forward from (e.g. 'analysis/run.py') — matched against each indexed output's recorded generating_script."},
         "limit": {"type": "integer", "description": "Max matched outputs to return, newest-first by mtime (default 25)."}},
         "required": ["outputs_dir", "source_path"]}},
    {"name": "search_code_semantic", "description":
        "93fce816 — Cursor-style LOCAL semantic code search over a source tree, "
        "entirely in a DuckDB sidecar (no cloud round-trip). Parses Python "
        "(stdlib ast) and TypeScript/JavaScript (tree-sitter) into SEMANTIC "
        "CHUNKS at function/class/method boundaries PLUS the un-named logical "
        "blocks that a named-symbols-only graph search can't reach (module-level "
        "dict/list literals, bare calls, __main__ guards, imports) — so a term "
        "that only appears in a bare top-level call is still findable. "
        "Incremental by a content MERKLE TREE: the root hash is compared first "
        "and only divergent subtrees are walked, so only the files that actually "
        "changed since the last pass are re-chunked (repeat calls on an "
        "unchanged tree are near-free). Search is HYBRID — DuckDB native FTS "
        "(Okapi BM25) for keyword match, fused via Reciprocal Rank Fusion with "
        "an OPTIONAL local-embedding vector leg (DuckDB VSS / HNSW cosine over a "
        "Model2Vec static model) when MERIDIAN_CODE_INDEX_VECTORS is enabled; "
        "with vectors off (the default) it is a complete pure-BM25 code search. "
        "Returns {root_dir, query, total_indexed, vectors_enabled, "
        "vectors_active, hits:[{chunk_id, path, language, kind, name, "
        "line_start, line_end, content, score, bm25, bm25_rank, vector_rank}]}. "
        "A missing dir / empty tree returns an empty hits list, never an error.",
     "inputSchema": {"type": "object", "properties": {
         "root_dir": {"type": "string", "description": "Absolute path to the source-tree root to index and search (walked recursively; vendored/build dirs like node_modules/.git/dist are pruned)."},
         "query": {"type": "string", "description": "The search query — keywords and/or a natural-language description of the code you want to find."},
         "limit": {"type": "integer", "description": "Max ranked hits to return (default 10)."},
         "kind": {"type": "string", "description": "Optional chunk-kind filter: one of 'function', 'class', 'method', 'interface', 'enum', 'module'."},
         "reindex": {"type": "boolean", "description": "Default true — run an incremental Merkle-diff reindex before searching so results reflect the current tree. Set false to search the last-built index as-is."}},
         "required": ["root_dir", "query"]}},
    {"name": "get_flag_registry", "description":
        "45802b67 — scan a source tree for `os.environ.get(...)` / `os.getenv(...)` "
        "call sites (AST-based, not regex) and return a flat inventory of every "
        "config flag the codebase reads: {flag_name, file, line, default}. "
        "Only call sites where the flag name is a STRING LITERAL first argument "
        "are included — dynamic names (a variable, f-string, etc.) are skipped "
        "gracefully rather than erroring. The default is best-effort literal-eval'd "
        "from the second positional arg (or a `default=` keyword); a non-literal "
        "default evaluates to null. Useful for auditing config drift — 'what env "
        "flags exist, where are they read, what do they default to' — without "
        "grepping by hand. Returns {repo_root, flags:[...], count, "
        "unique_flag_names:[...], unique_count}. A missing/empty tree returns an "
        "empty flags list, never an error.",
     "inputSchema": {"type": "object", "properties": {
         "root_dir": {"type": "string", "description": "Absolute path to the source-tree root to scan recursively (vendored/build/cache dirs like node_modules/.git/dist/__pycache__ are pruned). Defaults to the server's current working directory (the current project's repo root) when omitted."}},
         "required": []}},
    {"name": "link_flag_to_section", "description":
        "8ca89e8f — DURABLY link a docx section/paragraph/figure/table (any "
        "doc_elements id — the same id space index_figure/index_table/"
        "link_figure_caption already anchor to) to the config-flag state that "
        "produced its underlying numbers. This is the check that catches "
        "'results computed with the wrong flag state, then cited as current' — "
        "e.g. a flag that silently skipped a whole code path regardless of "
        "another flag, or a stale count cited after a fix superseded it. "
        "Typical flow: call get_flag_registry to find the flag's current "
        "file/line/default, compute the section, then call this tool with "
        "value=the value actually used and default=the default get_flag_registry "
        "reported (so get_flag_drift has something to compare the codebase's "
        "CURRENT default against later). Insert-only: re-linking the same "
        "(element_id, flag_name) pair after a re-verification adds a new "
        "history row rather than overwriting the old one. Returns "
        "{project_id, document_id, link}.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"},
         "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "doc": {"type": "string", "description": "The stored document's source (the path/URL you ingested it under via ingest_document)."},
         "element_id": {"type": "string", "description": "The doc_elements.id of the section/paragraph/figure/table this flag state applies to."},
         "flag_name": {"type": "string", "description": "The config flag's name (as scanned by get_flag_registry, e.g. 'DT_ONLY_WIDTH')."},
         "value": {"description": "The value the flag actually had when this section's numbers were produced (any JSON scalar — string/number/boolean/null)."},
         "default": {"description": "The flag's default AS RECORDED by get_flag_registry at link time — what a later get_flag_drift compares the current codebase default against. Optional but recommended."},
         "source_file": {"type": "string", "description": "Optional: the file the flag was read from (from get_flag_registry's 'file'), pinning drift detection to this exact call site."},
         "source_line": {"type": "integer", "description": "Optional: the line the flag was read at (from get_flag_registry's 'line'), paired with source_file."}},
         "required": ["doc", "element_id", "flag_name", "value"]}},
    {"name": "get_flag_drift", "description":
        "8ca89e8f — read side of link_flag_to_section: for every recorded "
        "flag link (optionally scoped to one doc / element_id / flag_name — "
        "pass flag_name alone with no doc for the REVERSE query 'flag X "
        "changed, which sections does it touch'), re-scan the CURRENT "
        "codebase (same AST scan as get_flag_registry) and diff each link's "
        "recorded default against what the flag defaults to NOW. Only the "
        "most recently recorded link per (element, flag) pair is diffed — a "
        "re-verified section's older links are history, not live claims. "
        "Each result carries status: 'removed' (the flag, or this exact call "
        "site, no longer exists — the strongest staleness signal), 'drifted' "
        "(the flag still exists but its default changed since this section "
        "was computed — the section is possibly stale, needs re-verification), "
        "or 'ok' (no evidence of drift found). Returns {project_id, root_dir, "
        "links:[{...link fields, current_default, current_call_sites, "
        "status}], summary:{ok, drifted, removed}}. No recorded links returns "
        "an empty list, never an error — this is advisory, not a hard gate.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"},
         "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "root_dir": {"type": "string", "description": "Absolute path to the source-tree root to re-scan for current flag defaults (same as get_flag_registry's root_dir). Defaults to the server's current working directory when omitted."},
         "doc": {"type": "string", "description": "Optional: scope to links recorded against one stored document (the doc source you ingested it under)."},
         "element_id": {"type": "string", "description": "Optional: scope to links recorded against one specific doc_elements id."},
         "flag_name": {"type": "string", "description": "Optional: scope to links recorded for one flag name — the reverse query, omit 'doc' to search project-wide."}},
         "required": []}},
    {"name": "prospect_symbol", "description":
        "2ce5bc76 — ROBUST symbol prospecting with a three-rung fallback chain: "
        "tries codebase__search_graph FIRST (fast, graph-indexed); when it returns "
        "zero results OR the caller flags a mismatch (stale_graph=true), "
        "automatically retries via Serena extractor__find_symbol / "
        "extractor__find_declaration (AST-accurate, never stale); falls back to "
        "a BM25 keyword grep over search_code_semantic as a last resort so the "
        "caller NEVER has to notice a miss and switch tools by hand. Each rung is "
        "labelled in the result ({rung: 'graph'|'serena'|'semantic', hits:[...], "
        "fallback_reason: str?}) so the caller knows which level succeeded. "
        "All three legs are best-effort: a missing tunnel, inactive slot, or "
        "missing root_dir degrades to the next rung, never a bare error with "
        "no diagnostic. d5e60791 — every rung's outcome is recorded under "
        "result.rungs.{graph,serena,semantic}: status "
        "('not_attempted'|'skipped'|'attempted'|'succeeded'|'empty'|'error'), "
        "attempted_tool/selected_tool, and (on skip/error) a reason/error + "
        "error_kind ('dependency_error' vs 'runtime_error') — a missing "
        "dependency (e.g. an uninstalled local package) or any other runtime "
        "failure in a rung is NEVER silently collapsed into rung='none' with "
        "no trace; fallback_reason is always populated when every rung misses. "
        "4b8f083f — when root_dir is a git checkout, the graph rung is ALSO "
        "auto-skipped (same as an explicit stale_graph=true, with fallback_reason "
        "'graph_skipped_commit_drift_detected') whenever a cheap local "
        "`git rev-list --count` finds real commits since the last "
        "index_repository run for this project — no waiting for a "
        "_graph_staleness warning from the server, which only fires when a "
        "SIBLING process re-indexes, never when nobody re-indexes at all. "
        "Pass root_dir to get this protection. "
        "Use this instead of calling codebase__search_graph directly whenever "
        "you are prospecting for a symbol, function, or class location — it "
        "is structurally immune to the class of silent graph-index miss that "
        "previously returned wrong line numbers or empty results for real symbols.",
     "inputSchema": {"type": "object", "properties": {
         "symbol": {"type": "string", "description": "The symbol/function/class/method name or short search query to prospect for."},
         "project_id": {"type": "string", "description": "Code-intel project id (repo-path slug) passed to codebase__search_graph."},
         "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "root_dir": {"type": "string", "description": "Absolute path to the source tree root — used for the search_code_semantic fallback. If omitted, the semantic leg is skipped."},
         "limit": {"type": "integer", "description": "Max results per rung (default 5)."},
         "stale_graph": {"type": "boolean", "description": "Set true to SKIP the graph rung and go straight to Serena (e.g. you already know the graph is stale from a _graph_staleness warning)."},
         "kind": {"type": "string", "description": "Optional symbol kind filter passed to search_code_semantic fallback (function/class/method/etc)."},
         "session_id": {"type": "string", "description": "a8c0f3b7 — optional Meridian session id. Purely for attribution: when supplied, the durable code-intel prospecting receipt this call records (meridian.code_intel_receipt) is attributed to this session, strengthening complete_sprint_item's prospecting-receipt gate. Never required and never affects the prospect result itself."}},
         "required": ["symbol"]}},
    {"name": "add_sprint_item_pointer", "description":
        "2976e168 — attach a GENERIC POINTER to a sprint item: a portable, composable "
        "reference to a thing-in-a-source, grounded in LSP Location + W3C Web Annotation "
        "Selector composition. targets is an ARRAY of {uri, selector, subSelector?} "
        "objects (native multi-file, the LSP WorkspaceEdit pattern); the whole composite "
        "shape is stored as JSON, not per-domain columns. Every selector is an object "
        "with an explicit \"type\" PLUS that type's own field(s):\n"
        "• range — {\"type\":\"range\", \"start_line\":int, \"end_line\":int, "
        "\"start_char\"?:int, \"end_char\"?:int} (an LSP Range); the pointer IS the "
        "location.\n"
        "• symbol — {\"type\":\"symbol\", \"qualified_name\":\"pkg.mod.func\"} resolved "
        "against the cached code graph to a file+line.\n"
        "• node_id — {\"type\":\"node_id\", \"id\":\"<element-id>\"} of a doc_store "
        "element (an ingested-document structure node). NOTE: the field is \"id\", NOT "
        "\"value\".\n"
        "• zotero_key — {\"type\":\"zotero_key\", \"key\":\"<zotero-key>\"} of a Zotero "
        "library item.\n"
        "• text_quote — {\"type\":\"text_quote\", \"exact\":str, \"prefix\"?:str, "
        "\"suffix\"?:str, \"archived_url\"?:str, \"archived_at\"?:str, "
        "\"canonical_url\"?:str, \"retrieval_hash\"?:str} (W3C TextQuoteSelector; "
        "source_type \"web\" — a URL — OR a local .docx path, resolving via a docx "
        "paragraph-text match instead of an HTTP GET). Resolving re-fetches live and "
        "flags content drift (the cited passage silently changed/vanished).\n"
        "• finding_id — {\"type\":\"finding_id\", \"id\":\"<finding-note-id>\"} "
        "(source_type \"experiment\") addresses a save_finding artifact.\n"
        "• directory — {\"type\":\"directory\", \"root\":str, \"include\"?:[str,...], "
        "\"exclude\"?:[str,...], \"manifest_id\"?:str, \"snapshot_id\"?:str} "
        "(62640241) — a directory ROOT + glob include/exclude selector + optional "
        "snapshot/manifest identity. Resolving it (local paths only by default) walks "
        "the tree and returns a deterministic manifest + manifest_hash.\n"
        "• git — {\"type\":\"git\", \"repository\":str, \"ref\"?:str, \"commit\"?:str, "
        "\"path\"?:str} (62640241) — a Git repository identity; at least one of "
        "\"ref\"/\"commit\" is required. A line range within \"path\" is expressed via "
        "subSelector (a nested range), NOT a new field. Resolving it (local clones only "
        "by default) checks reachability against the repo's current HEAD via `git "
        "rev-parse`.\n"
        "• remote_fs — {\"type\":\"remote_fs\", \"host_id\":str, \"filesystem_slot\":str, "
        "\"path\":str, \"lease_id\"?:str, \"session_id\"?:str, \"snapshot_id\"?:str} "
        "(62640241) — an opaque tunnel-connector host + filesystem slot + remote path, "
        "optionally bound to the lease/session that captured it. No core-local default "
        "resolver exists (requires an injected, tunnel-backed resolver) — reported "
        "explicitly unresolved without one, never silently dropped.\n"
        "• artifact — {\"type\":\"artifact\", \"manifest_uri\":str, \"fingerprint\"?:str, "
        "\"run_id\"?:str, \"item_id\"?:str, \"provenance_id\"?:str} (62640241) — a "
        "build/output artifact's manifest URI plus an optional fingerprint and a link to "
        "the producing run/sprint-item/provenance record. Resolving it (local files only "
        "by default) hashes the manifest file to report its current fingerprint.\n"
        "An optional selector.subSelector nests finer granularity (W3C hasSubSelector) — "
        "e.g. {\"type\":\"symbol\", \"qualified_name\":\"a.b.f\", \"subSelector\": "
        "{\"type\":\"range\", \"start_line\":3, \"end_line\":4}} = 'these lines, within "
        "this function'. A subSelector is itself a FULL selector and MUST carry its OWN "
        "explicit \"type\" (it does not inherit the parent's). source_type names the "
        "domain (code | docs | citation | web | experiment | …). Each target may also "
        "carry target_kind: \"existing\" | \"planned_new\" (300a063d) — set \"existing\" "
        "ONLY when the file/symbol already exists (this is checked against the real "
        "filesystem and REJECTED if the path isn't there); set \"planned_new\" for a "
        "file this sprint item will CREATE, which is explicitly exempt from that check. "
        "Omitting target_kind keeps the pre-existing, unchecked behavior (defaults to "
        "\"existing\" in the stored shape but is never filesystem-verified) — set it "
        "explicitly to get real verification. 62640241 — a target may ALSO carry an "
        "optional freshness proof: {\"content_hash\"?:str, \"source_revision\"?:str, "
        "\"resolver_version\"?:str, \"captured_at\"?:str, \"state\"?: \"current\"|"
        "\"stale\"|\"unknown\"|\"unavailable\"|\"ambiguous\"}. Purely additive/opt-in; "
        "resolve_sprint_item_pointers recomputes a LIVE freshness_state for directory/"
        "git/remote_fs/artifact/text_quote targets by comparing this declared proof "
        "against what resolution finds right now. Malformed pointers are rejected with a "
        "clear error: a bad/missing selector.type, a missing required selector field "
        "(e.g. node_id without \"id\", git without ref or commit, a subSelector with no "
        "\"type\", an invalid target_kind or freshness.state, or target_kind=\"existing\" "
        "at a path that doesn't exist). Returns the stored pointer.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"},
         "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "sprint_item_id": {"type": "string", "description": "The sprint item to attach the pointer to."},
         "source_type": {"type": "string", "description": "Domain of the pointer: code | docs | citation | web | experiment | … (free text)."},
         "targets": {"type": "array", "description":
             "Non-empty array of {uri, selector, subSelector?, target_kind?, freshness?} "
             "targets. Each selector is an object carrying an explicit \"type\" plus that "
             "type's field(s): range {\"type\":\"range\", start_line, end_line, "
             "start_char?, end_char?}; symbol {\"type\":\"symbol\", qualified_name}; "
             "node_id {\"type\":\"node_id\", id} (field is \"id\", NOT \"value\"); "
             "zotero_key {\"type\":\"zotero_key\", key}; text_quote {\"type\":"
             "\"text_quote\", exact, prefix?, suffix?, archived_url?, archived_at?, "
             "canonical_url?, retrieval_hash?}; finding_id {\"type\":\"finding_id\", id}; "
             "directory {\"type\":\"directory\", root, include?, exclude?, manifest_id?, "
             "snapshot_id?}; git {\"type\":\"git\", repository, ref?, commit? "
             "(>=1 required), path?}; remote_fs {\"type\":\"remote_fs\", host_id, "
             "filesystem_slot, path, lease_id?, session_id?, snapshot_id?}; artifact "
             "{\"type\":\"artifact\", manifest_uri, fingerprint?, run_id?, item_id?, "
             "provenance_id?} (62640241 for the last five). An optional subSelector is "
             "itself a full selector and MUST carry its own \"type\". target_kind is "
             "\"existing\" (default; explicit \"existing\" is verified against the real "
             "filesystem) or \"planned_new\" (a file not created yet — exempt from that "
             "check). freshness (62640241) is an optional {content_hash?, "
             "source_revision?, resolver_version?, captured_at?, state?} proof of what "
             "the source looked like at capture time.",
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
        "as-is; symbol resolves the qualified_name against the SAME live three-rung "
        "chain prospect_symbol uses (graph → Serena → semantic, 653579c5) when this "
        "session has an active code tunnel, falling back to the cached code-graph "
        "snapshot when it doesn't; node_id looks the element up in the doc-structure "
        "store; zotero_key resolves via Zotero's local API; text_quote re-fetches the "
        "URL (or docx paragraph text) and flags content drift; finding_id looks up a "
        "save_finding artifact note. 62640241 — directory walks the local root and "
        "returns a manifest + manifest_hash; git shells out to `git rev-parse` against a "
        "local clone to check ref/commit reachability against HEAD; artifact hashes a "
        "local manifest file for its current fingerprint; remote_fs has no core-local "
        "default (requires a tunnel-backed resolver — reported explicitly unresolved "
        "without one). Every one of these five ALSO gets a recomputed freshness_state "
        "(current/stale/unknown/unavailable/ambiguous) on its resolved target, comparing "
        "the target's declared freshness proof (if any) against what resolution finds "
        "right now. A subSelector narrows the outer resolution ('these lines, within "
        "this function'). Every dispatch is best-effort: an unresolvable target yields "
        "{resolved:false, reason} instead of an error, and the pass NEVER fails. Returns "
        "{pointers:[{id, source_type, label, targets:[<resolved-target>]}]}. Requires no "
        "network for range/symbol/node_id/directory/git(local)/artifact(local); "
        "zotero_key needs Zotero running locally and text_quote needs live web access "
        "(else those targets are just unresolved).",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"},
         "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "sprint_item_id": {"type": "string", "description": "The sprint item whose pointers to resolve."},
         "root_dir": {"type": "string", "description": "Optional absolute path to the source tree root, passed through to the symbol resolver's search_code_semantic fallback rung (same as prospect_symbol's root_dir) when no code tunnel is active. If omitted, that rung is skipped."}},
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
    {"name": "execute_batch", "description":
        "627187b8 — run a HOMOGENEOUS batch of management writes (all entries the "
        "SAME operation) with real atomic-or-independent semantics. Every entry in "
        "``entries`` is validated and reported individually — no guessing whether a "
        "partial write happened. operation selects the entry shape:\n"
        "• sprint_items — create new sprint items. Each entry needs a non-empty "
        "'title' plus any add_sprint_item kwarg (version, group, human_id, "
        "depends_on, priority, touches_resources, ...). Every entry's own 'action' "
        "(if present) must be 'create'.\n"
        "• item_updates — patch existing sprint items. Each entry needs a non-empty "
        "'item_id' plus at least one patch_sprint_item field to change (title, "
        "status, notes, priority, ...). Every entry's own 'action' (if present) "
        "must be 'update'.\n"
        "• pointers — attach generic pointers (see add_sprint_item_pointer). Each "
        "entry needs 'sprint_item_id', 'source_type', 'targets' (+ optional "
        "'label').\n"
        "• notes — file sprint notes (see add_sprint_note). Each entry needs "
        "'title' and 'body' (+ optional 'session_id' — falls back to this call's "
        "own top-level session_id when omitted — and 'note_kind').\n"
        "Any entry MAY carry a 'correlation_key' (any non-empty string) echoed "
        "back on its result for reconciliation; every result also carries its "
        "0-based input 'index' regardless.\n"
        "mode is REQUIRED and controls failure semantics: 'all_or_nothing' "
        "validates every entry BEFORE mutating anything — if any entry fails "
        "validation, NOTHING is written (status 'rejected'); if a mutation fails "
        "partway through, every entry this call already wrote is rolled back via "
        "a compensating delete/revert (status 'failed'). 'best_effort' processes "
        "each entry independently — one entry's failure never blocks the others "
        "(status 'ok' | 'partial' | 'failed' depending on how many succeeded).\n"
        "idempotency_key is REQUIRED (pass null or \"\" to explicitly opt out of "
        "idempotency protection for this call) — a retried call with the identical "
        "(project_id, operation, idempotency_key) tuple returns the FIRST call's "
        "stored result verbatim (idempotent_replay:true) instead of re-executing, "
        "making retries safe even for all_or_nothing batches that already wrote "
        "and rolled back once.\n"
        "Returns {status, mode, entry_kind, operation, project_id, idempotency_key, "
        "idempotent_replay, created_count, error_count, results:[{index, "
        "correlation_key, status, id, outcome, error_code, error_message, "
        "retryable}]} — results is ALWAYS in input order regardless of processing "
        "order. Each result status is 'ok' (mutated), 'error' (validation or "
        "mutation failure — see error_code/error_message/retryable), "
        "'rolled_back' (succeeded, then undone by a later all_or_nothing failure), "
        "or 'not_attempted' (never reached because an earlier entry aborted the "
        "batch). max_entries caps this call (default 100); exceeding it is "
        "rejected before anything is attempted.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"},
         "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "operation": {"type": "string", "enum": ["sprint_items", "item_updates", "pointers", "notes"], "description": "Stable operation name selecting the entry shape and forced per-entry action (sprint_items=create, item_updates=update). See the tool description for each shape."},
         "entries": {"type": "array", "description": "Non-empty list of entry objects, ALL matching the chosen operation's shape. Each entry may carry an optional 'correlation_key' string echoed back on its result.", "items": {"type": "object"}},
         "mode": {"type": "string", "enum": ["all_or_nothing", "best_effort"], "description": "REQUIRED — no default. 'all_or_nothing': validate-then-mutate with compensating rollback on any mutation failure. 'best_effort': every entry processed independently."},
         "idempotency_key": {"type": "string", "description": "REQUIRED key (value may be null or \"\" to explicitly opt out). A retried call with the same (project_id, operation, idempotency_key) replays the first call's stored result instead of re-executing."},
         "session_id": {"type": "string", "description": "Batch-level default session_id used by 'notes' entries that omit their own session_id."},
         "max_entries": {"type": "integer", "description": "Optional cap on len(entries) for this call (default 100). Exceeding it rejects the whole call before anything is attempted."}},
         "required": ["operation", "entries", "mode", "idempotency_key"]}},
    {"name": "batch_read", "description":
        "133bfff6 — run a batch of DOMAIN-AWARE, CONCURRENT read requests in ONE call. "
        "Each request names an 'adapter' + 'operation' + 'args'; independent requests "
        "(no depends_on) execute concurrently via asyncio.gather — this is pure in-process "
        "dispatch, no subagents/worktrees involved. A request with 'depends_on' (a list of "
        "other requests' 'request_id's in this SAME batch) waits only for its own declared "
        "prerequisites, not the whole batch; if a prerequisite fails, the dependent resolves "
        "immediately with error_code='DEPENDENCY_FAILED' and is never executed. Two requests "
        "with the identical adapter+operation+normalized-args+depends_on-set COALESCE to one "
        "execution — duplicates come back with cache_hit=true and coalesced_with=<the request_id "
        "that actually ran>; pass a non-default cache_policy to opt a specific request out of "
        "coalescing. Adapters currently registered: 'sprint_board' with operations "
        "'get_sprint_items' (args: status, show_blocked, include_human, version, "
        "include_manual_blocker, include_deferred — same meaning as the get_sprint_items tool) "
        "and 'get_sprint_item_pointers' (args: sprint_item_id — 404s if that item belongs to a "
        "different project); and 'profile' (PROFILE-7) with operations 'get_profile_layer' (args: "
        "scope_type, scope_id), 'list_profile_layers' (args: optional scope_type filter), "
        "'get_effective_profile' (args: optional session_id, user_scope_id, workspace_scope_id — "
        "returns the merged, generation-keyed effective profile across all 5 layers), and "
        "'get_profile_layer_revisions' (args: scope_id, optional limit). Returns {results: [{request_id, status, adapter, operation, result, "
        "error_code, error_message, elapsed_ms, cache_hit, coalesced_with}], elapsed_ms} — "
        "results is ALWAYS in input order. error_code is one of VALIDATION_ERROR, "
        "ADAPTER_NOT_FOUND, OPERATION_NOT_FOUND, DEPENDENCY_NOT_FOUND, DEPENDENCY_CYCLE, "
        "DEPENDENCY_FAILED, NOT_FOUND, TIMEOUT, INTERNAL_ERROR. This tool is READ-ONLY — for "
        "mutations use batch_mutate or execute_batch.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"},
         "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "requests": {"type": "array", "description": "Non-empty list of typed read requests.", "items": {"type": "object", "properties": {
             "request_id": {"type": "string", "description": "Required, unique within this batch."},
             "adapter": {"type": "string", "description": "Registered adapter name, e.g. 'sprint_board'."},
             "operation": {"type": "string", "description": "Operation the adapter exposes, e.g. 'get_sprint_items'."},
             "args": {"type": "object", "description": "Operation-specific arguments. Defaults to {}."},
             "depends_on": {"type": "array", "items": {"type": "string"}, "description": "Optional list of this batch's own request_ids that must resolve first."},
             "timeout_ms": {"type": "integer", "description": "Optional per-request timeout in milliseconds (default 10000)."},
             "cache_policy": {"type": "string", "description": "Optional. Any value other than omitted/\"\"/\"default\" opts this request OUT of duplicate-coalescing."}},
             "required": ["request_id", "adapter", "operation"]}},
         "max_requests": {"type": "integer", "description": "Optional cap on len(requests) for this call (default 100)."}},
         "required": ["requests"]}},
    {"name": "batch_mutate", "description":
        "133bfff6 — run a batch of TRANSACTIONAL mutation entries in ONE call, mixing entry "
        "kinds selected per-entry via 'kind': 'sprint_item_pointer' (attach a pointer — same "
        "shape as add_sprint_item_pointer: sprint_item_id, source_type, targets, optional label), "
        "'sprint_item_update' (patch an EXISTING sprint item — same shape as update_sprint_item: "
        "item_id + at least one patchable field; sprint-item CREATION is not supported here, use "
        "execute_batch(operation='sprint_items', ...) or add_sprint_item for that), and (PROFILE-7) "
        "'profile_layer' (upsert one scope_type+scope_id profile layer — same shape as "
        "set_profile_layer: scope_type, scope_id, optional fields/reset_fields/provenance/"
        "expected_revision; a stale expected_revision surfaces error_code='CONFLICT' with "
        "expected_revision/actual_revision in the outcome payload). Reuses the exact "
        "same validated apply/compensate logic execute_batch and the single-item tools already use — "
        "no separate/duplicated mutation path. mode is REQUIRED: 'all_or_nothing' validates every "
        "entry BEFORE mutating anything — any validation failure writes NOTHING (status 'rejected'); "
        "a mutation failure partway through rolls back every entry this call already wrote via a "
        "compensating delete/revert (status 'failed', per-entry status 'rolled_back'). 'best_effort' "
        "processes each entry independently (status 'ok' | 'partial' | 'failed'). idempotency_key is "
        "REQUIRED (pass null or \"\" to explicitly opt out) — a retried call with the identical "
        "(project_id, idempotency_key) tuple returns the FIRST call's stored result verbatim "
        "(idempotent_replay:true) instead of re-executing. PROJECT ISOLATION: an entry MAY carry its "
        "own 'project_id' field, but it MUST match this call's own project_id or the entry is rejected "
        "outright — a mutation entry can never target a different project. Returns {status, mode, "
        "project_id, idempotency_key, idempotent_replay, created_count, error_count, results:[{index, "
        "correlation_key, status, id, outcome, error_code, error_message, retryable}], request_id, "
        "committed_count, failures:[...failed results...], rollback_status: 'none'|'rolled_back'|"
        "'rejected'} — results is ALWAYS in input order.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"},
         "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "entries": {"type": "array", "description": "Non-empty list of entries, each carrying its own 'kind' ('sprint_item_pointer', 'sprint_item_update', or 'profile_layer'). Each entry may carry an optional 'correlation_key' string echoed back on its result.", "items": {"type": "object"}},
         "mode": {"type": "string", "enum": ["all_or_nothing", "best_effort"], "description": "REQUIRED — no default."},
         "idempotency_key": {"type": "string", "description": "REQUIRED key (value may be null or \"\" to explicitly opt out)."},
         "session_id": {"type": "string", "description": "Optional attribution for the idempotency receipt."},
         "max_entries": {"type": "integer", "description": "Optional cap on len(entries) for this call (default 100)."}},
         "required": ["entries", "mode", "idempotency_key"]}},
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
        "tool_priority_map (object) sets a durable default MCP tool per semantic task "
        "category (e.g. {\"code-reading\": \"Serena: find_symbol\"}) — generalizes the "
        "per-item required_tool pin up one level; rendered as a HARD, unconditional "
        "directive in every /goal for matching pending items that have no item-level "
        "required_tool override. Pass {} to clear it. "
        "claim_verification_mode ('off'|'advisory'|'strict', '' to clear back to 'off') "
        "controls whether a PostToolUse hook re-checks claim_sprint_item/"
        "complete_sprint_item calls against live sprint-item state before trusting the "
        "calling session's own narration: 'off' = no check; 'advisory' = logs a warning "
        "on mismatch but never blocks; 'strict' = blocks the session on mismatch. "
        "Pass an empty string to revert a field to the server default.",
     "inputSchema": {"type": "object", "properties": {
         "hitl_auto_answer_default": {"type": "boolean"},
         "sprint_name_default": {"type": "string"},
         "handoff_template": {"type": "string"},
         "execution_mode_default": {"type": "string", "description": "Seed new projects' execution mode: 'autonomous', 'interactive', or '' to clear."},
         "code_intel_enabled_default": {"type": "boolean", "description": "Seed new projects' code-intel toggle."},
         "loop_enabled_default": {"type": "boolean", "description": "Workspace default for /loop auto-continue; projects with loop_enabled='workspace' inherit it. True = sessions auto-continue."},
         "tool_priority_map": {"type": "object", "description": "Default MCP tool per semantic task category, e.g. {\"code-reading\": \"Serena: find_symbol\"}. Hard-enforced in /goal. {} clears."},
         "claim_verification_mode": {"type": "string", "description": "'off' (default) | 'advisory' (log-only) | 'strict' (blocking) — verify claim_sprint_item/complete_sprint_item calls against live DB state via a PostToolUse hook. '' clears back to 'off'."}},
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
    {"name": "add_workspace_proposal", "description":
        "Capture a workspace-level flash of insight into the 'drawer of inspiration' — "
        "cross-project ideas that don't belong to any one project yet. Unlike sprint items "
        "these are NOT executor-claimable; they require a human to review and promote them. "
        "Proposals start at status='raw' and progress through an enforced lifecycle: "
        "raw → investigating → promoted|rejected. Use advance_proposal_status to move "
        "through the lifecycle; use promote_proposal to convert one into a real sprint item.",
     "inputSchema": {"type": "object", "properties": {
         "title": {"type": "string", "description": "Short idea title."},
         "body": {"type": "string", "description": "Full description of the insight or idea."},
         "tags": {"type": "string", "description": "Optional comma-separated tags."}},
         "required": ["title", "body"]}},
    {"name": "get_workspace_proposals", "description":
        "Read-only: List a bounded page of workspace proposals (human-authored flashes of insight), newest first. "
        "When status is omitted, defaults to 'live' proposals only (raw + investigating) — "
        "terminal proposals (promoted/rejected) are excluded so the default view reflects "
        "what's actually still open. Pass status='all' to fetch every status, or an explicit "
        "status (including promoted/rejected) to filter to just that one. Optional tag "
        "substring filter. Pagination defaults to 20 rows (maximum 100); pass offset to "
        "fetch the next page.",
     "inputSchema": {"type": "object", "properties": {
         "status": {"type": "string", "enum": ["raw", "investigating", "promoted", "rejected", "all"],
                    "description": "Filter to proposals in this status. Defaults to raw+investigating "
                    "('live') when omitted; use 'all' for every status."},
         "tag": {"type": "string", "description": "Substring filter on tags."},
         "limit": {"type": "integer", "minimum": 1, "maximum": 100,
                   "description": "Maximum proposals to return (default 20, clamped to 1..100)."},
         "offset": {"type": "integer", "minimum": 0,
                    "description": "Zero-based pagination offset (default 0)."}},
         "required": []}},
    {"name": "advance_proposal_status", "description":
        "Transition a workspace proposal through its lifecycle. Enforced transitions: "
        "raw → investigating|rejected; investigating → promoted|rejected|raw; "
        "rejected → raw. 'promoted' is a terminal status reachable only via "
        "promote_proposal (which also creates the sprint item). "
        "Returns the updated proposal.",
     "inputSchema": {"type": "object", "properties": {
         "proposal_id": {"type": "string"},
         "status": {"type": "string", "enum": ["raw", "investigating", "rejected"],
                    "description": "Target status. 'promoted' is not allowed here — use promote_proposal instead."}},
         "required": ["proposal_id", "status"]}},
    {"name": "promote_proposal", "description":
        "Promote a workspace proposal into a real sprint item, creating the link between them. "
        "The proposal must be in 'raw' or 'investigating' state. Creates a sprint item under "
        "the given project and sets the proposal's status to 'promoted' with "
        "promoted_to_sprint_item_id pointing to the new item. "
        "Returns {proposal, sprint_item_id, sprint_item_title, project_id}.",
     "inputSchema": {"type": "object", "properties": {
         "proposal_id": {"type": "string"},
         "project_id": {"type": "string", "description": "Project to create the sprint item under."},
         "project_name": {"type": "string", "description": "Project name — alternative to project_id; resolved to the id internally."},
         "sprint_item_title": {"type": "string", "description": "Override title for the sprint item; defaults to the proposal title."},
         "sprint_item_version": {"type": "string", "description": "Sprint version for the new item; defaults to 'current'."}},
         "required": ["proposal_id"]}},
    {"name": "preview_proposal_promotion", "description":
        "Read-only (ce4883f3): preview what commit_proposal_promotion would do for a proposal at "
        "a given depth, WITHOUT writing anything. Depths are cumulative, shallow to deep: "
        "'proposal' (intake/scope only) -> 'investigation' (+ raw->investigating transition) -> "
        "'pointers' (+ pointer recording) -> 'sprint_items' (+ promote_workspace_proposal) -> "
        "'executable_handoff' (+ a handoff scoped to the new sprint item). Returns "
        "{proposal_id, project_id, depth, already_satisfied, contract_status, would_create, "
        "wave_preview, preview_hash, computed_at}. 'already_satisfied'=true when the proposal's "
        "current status already reaches this depth (e.g. already 'promoted' and depth='sprint_items') "
        "— no-op, nothing further to preview. Otherwise 'contract_status' reports each of the 8 "
        "proposal-to-execution contract parts as present/would_create/optional_at_commit/not_applicable; "
        "'would_create' shows the synthetic sprint-item preview (title/version/touches_resources) once "
        "depth>='sprint_items'; 'wave_preview' shows which existing parallel-safe group the item would "
        "join or conflict with (via get_parallelizable_groups, unmodified). Pass the returned "
        "'preview_hash' to commit_proposal_promotion — a stale hash (proposal or board changed since) "
        "is rejected rather than silently committed.",
     "inputSchema": {"type": "object", "properties": {
         "proposal_id": {"type": "string"},
         "project_id": {"type": "string", "description": "Target project — where the sprint item would land."},
         "project_name": {"type": "string", "description": "Project name — alternative to project_id; resolved to the id internally."},
         "depth": {"type": "string", "enum": ["proposal", "investigation", "pointers", "sprint_items", "executable_handoff"],
                   "description": "How far to preview promoting, cumulative over every shallower depth."},
         "sprint_item_title": {"type": "string", "description": "Override title for the would-be sprint item; defaults to the proposal title."},
         "sprint_item_version": {"type": "string", "description": "Sprint version for the would-be item; defaults to 'current'."},
         "touches_resources": {"type": "array", "items": {"type": "string"}, "description": "Explicit resource ids for the would-be sprint item; overrides inference."},
         "infer_touches_resources": {"type": "boolean", "description": "Infer touches_resources from the proposal's title/body via recent git history when touches_resources is omitted. Default true."}},
         "required": ["proposal_id", "depth"]}},
    {"name": "commit_proposal_promotion", "description":
        "Commit a proposal's promotion through 'depth' (ce4883f3), cumulative over every shallower "
        "depth. Requires 'preview_hash' from a just-called preview_proposal_promotion with the SAME "
        "arguments — a mismatch (proposal or target project's board changed since) is rejected rather "
        "than silently committed against stale information, and nothing is written. Committing an "
        "already-satisfied depth is an idempotent no-op success (matches the preview's "
        "already_satisfied=true case). A genuine lost race against a concurrent caller (caught by the "
        "underlying race-safe functions) is reported honestly as a failure with a "
        "'deviation_auto_resolved' audit trail — never silently retried or swallowed. When the promoted "
        "sprint item's resources or the proposal's own text match one of 3 narrow deviation heuristics "
        "(production_deployment / tenant_security_boundary / destructive_behavior), this files a durable "
        "HITL via request_hitl and returns hitl_pending=true WITHOUT completing remaining steps — pass "
        "a non-empty 'override_reason' to acknowledge and proceed anyway (audited). "
        "'investigation_findings' and 'pointers' are recorded via append_proposal_update at the "
        "matching depth; 'pointers' entries are validated via meridian.pointers.validate_pointer "
        "(each needs source_type + a non-empty targets array of {uri, selector, target_kind?}). "
        "depth='executable_handoff' calls generate_handoff(selected_item_ids=[the new/reused sprint "
        "item id]) — scoped to exactly that item's dependency closure, with project/version/session "
        "identity. Returns {proposal_id, project_id, depth, already_satisfied, committed, deviation, "
        "hitl_pending, hitl_request_id}.",
     "inputSchema": {"type": "object", "properties": {
         "proposal_id": {"type": "string"},
         "project_id": {"type": "string", "description": "Target project — where the sprint item lands."},
         "project_name": {"type": "string", "description": "Project name — alternative to project_id; resolved to the id internally."},
         "depth": {"type": "string", "enum": ["proposal", "investigation", "pointers", "sprint_items", "executable_handoff"],
                   "description": "How far to commit promoting, cumulative over every shallower depth."},
         "preview_hash": {"type": "string", "description": "The 'preview_hash' from a fresh preview_proposal_promotion call with the SAME arguments."},
         "sprint_item_title": {"type": "string", "description": "Override title for the sprint item; defaults to the proposal title."},
         "sprint_item_version": {"type": "string", "description": "Sprint version for the new item; defaults to 'current'."},
         "touches_resources": {"type": "array", "items": {"type": "string"}, "description": "Explicit resource ids for the sprint item; overrides inference."},
         "infer_touches_resources": {"type": "boolean", "description": "Infer touches_resources from the proposal's title/body via recent git history when touches_resources is omitted. Default true."},
         "investigation_findings": {"type": "string", "description": "Investigation findings to record (depth>='investigation'); recorded via append_proposal_update."},
         "pointers": {"type": "array", "items": {"type": "object"}, "description": "Pointer declarations to record (depth>='pointers'); each validated via meridian.pointers.validate_pointer ({source_type, targets: [{uri, selector, target_kind?}], label?})."},
         "session_id": {"type": "string", "description": "Caller session id; threaded into recorded events and into the depth='executable_handoff' handoff."},
         "actor": {"type": "string", "description": "Optional actor identity recorded on proposal events."},
         "override_reason": {"type": "string", "description": "Non-empty reason to acknowledge and proceed past a triggered HITL deviation instead of stopping (audited)."}},
         "required": ["proposal_id", "depth", "preview_hash"]}},
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
         "notes": {"type": "string", "description": "Optional free-form context stored on the item at creation time."},
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
                   "description": "dec69708 — named lane for the item (e.g. 'paper'). Buckets items so a whole track can be deferred/skipped."},
         "priority": {"type": "string", "enum": ["urgent", "high", "normal", "low"],
                      "description": "e08fee30 — item priority (default 'normal'). Higher-priority PENDING items are surfaced, claimed, and grouped FIRST: get_sprint_items and get_parallelizable_groups order urgent-first within their existing ordering, so an executor picks up higher-priority work before lower. Ordering-only for now; a running-session preemption/interrupt mechanism is deferred."},
         "blocker_kind": {"type": "string", "enum": ["manual", "superseded"],
                          "description": "2282a636 — omit for an ordinary item; 'manual' marks the item as blocked on a REAL-WORLD action OUTSIDE Meridian (publish something, obtain an API key, talk to an advisor). DISTINCT from milestone_type='human' (which is about WHO executes): a manual-blocker item is surfaced distinctly and is EXCLUDED from executor 'just claim the next pending' scoping, so an executor never treats it as claimable work. f89d440f — 'superseded' marks the item's premise as replaced by other work (e.g. a workspace proposal); UNLIKE 'manual' this is a HARD gate — claim_sprint_item refuses it outright even on a direct claim by item_id, not just a listing exclusion."},
         "wave": {"type": "string",
                  "description": "58a45b92 — stored, deterministic wave/batch label (e.g. 'wave-1') for enforced wave-a/wave-b grouping. Usually auto-filled by assign_sprint_waves from the conflict-free parallel groups; set it here only to pin an item to a specific wave up front. Omit to leave unassigned."},
         "required_tool": {"type": "string",
                  "description": "4d1fb28f — pin the specific MCP tool/plugin the executor MUST use for this item (e.g. 'Serena: replace_symbol_body', 'meridian__patch_file', a named tunnel plugin) instead of leaving tool choice to executor habit. Rendered as a hard directive in the /goal block (not a soft hint) — see build_item_briefing / the batch /goal's <required_tool> clause. Omit for ordinary executor discretion."},
         "tool_requirements": _TOOL_REQUIREMENTS_SCHEMA,
         "artifact_kind": _ARTIFACT_KIND_SCHEMA,
         "planned_output": _PLANNED_OUTPUT_SCHEMA,
         "policy": _ARTIFACT_POLICY_SCHEMA},
         "required": ["version", "title"]}},
    {"name": "fan_out_sprint_items",
     "description":
        "Bulk-insert sprint items from a single orchestrator call — decompose a goal into "
        "parallel work items without N sequential add_sprint_item calls. Pass a list of "
        "{title, description?, group?, version?} dicts; returns the list of new item_ids "
        "in insertion order. By DEFAULT (strict omitted/false) no duplicate guard is applied "
        "(the caller is assumed to have deduped) and titles that resolve to an empty string "
        "are silently skipped — unchanged, original behavior, kept for compatibility.\n"
        "468ab67d — pass strict=true to opt into the SAME shared engine execute_batch uses "
        "(meridian.db.batch_management, add_sprint_item-backed): the 60%-word-overlap "
        "duplicate guard applies (per-item force:true still overrides it), idempotency_key "
        "makes a retried call with the same key replay the first call's result instead of "
        "re-inserting, and mode picks all_or_nothing (validate-then-insert with compensating "
        "rollback on failure, default) or best_effort (each item processed independently). "
        "In strict mode the response is the execute_batch response shape "
        "({status, mode, entry_kind, project_id, idempotency_key, idempotent_replay, "
        "created_count, error_count, results:[{index, correlation_key, status, id, outcome, "
        "error_code, error_message, retryable}]}) PLUS the usual item_ids/count keys — a "
        "different, richer shape than the legacy bare item_ids/count, by design (a new "
        "opt-in contract, not a silent change to the old one). Each item may carry its own "
        "correlation_key (echoed back on its strict-mode result) and force (per-item "
        "duplicate-guard override, strict mode only).",
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
                     "force": {"type": "boolean", "description": "strict mode only — override the duplicate-title guard for this item (same meaning as add_sprint_item's own force). Ignored in legacy (non-strict) mode, which never applies the guard at all."},
                     "correlation_key": {"type": "string", "description": "strict mode only — an arbitrary caller-chosen id echoed back on this item's result for reconciliation. Ignored in legacy mode."},
                 },
                 "required": ["title"],
             },
         },
         "strict": {"type": "boolean", "description": "468ab67d — default false (legacy: no duplicate guard, bare item_ids/count response). Pass true to opt into the shared batch_management engine's duplicate guard + idempotency-key replay + mode semantics — see the tool description."},
         "mode": {"type": "string", "enum": ["all_or_nothing", "best_effort"], "description": "strict mode only — default 'all_or_nothing'. Ignored unless strict=true."},
         "idempotency_key": {"type": "string", "description": "strict mode only — a retried call with the same (project_id, idempotency_key) replays the first call's stored result instead of re-inserting. Ignored unless strict=true."},
         },
         "required": ["items"]}},
    {"name": "update_sprint_item", "description":
        "Edit fields on an existing sprint item: title, version, notes, human_id (assignee), "
        "group, deferred_until (enforced deferral), track, or depends_on (dependency ordering). "
        "Only the fields you pass are changed; omitted fields are left untouched. Pass an empty "
        "string for human_id, group, deferred_until, track, or depends_on to clear it. Returns "
        "the updated item, or an error if the id is unknown. For TWO OR MORE independent item "
        "patches, prefer the single execute_batch(operation='item_updates', entries=[...], "
        "mode='best_effort' or 'all_or_nothing', idempotency_key='...') call instead of "
        "repeating this tool: it validates and reports each item in input order, supports "
        "per-item correlation_key values, and makes retries idempotent. Use best_effort when "
        "one invalid item must not block the rest; use all_or_nothing when the whole patch set "
        "must succeed together.",
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
         "track": {"type": "string", "description": "dec69708 — named lane (e.g. 'paper'). Pass an empty string to clear; omit to leave unchanged."},
         "priority": {"type": "string", "enum": ["urgent", "high", "normal", "low"],
                      "description": "e08fee30 — set the item's priority (urgent|high|normal|low). Higher-priority pending items are surfaced/claimed/grouped first. Omit to leave unchanged."},
         "blocker_kind": {"type": "string", "enum": ["manual", "superseded"],
                          "description": "2282a636 — 'manual' marks the item as blocked on a real-world action OUTSIDE Meridian (distinct from milestone_type='human'; excluded from executor scoping only — claim_sprint_item still allows a direct claim). f89d440f — 'superseded' marks the item's premise as replaced by other work; claim_sprint_item HARD-refuses it even on a direct claim by item_id, until a human clears blocker_kind. Pass an empty string to CLEAR it (ordinary item); omit to leave unchanged."},
         "wave": {"type": "string",
                  "description": "58a45b92 — set/clear the stored wave label (e.g. 'wave-1') for enforced parallel-batch grouping. Hand-override of what assign_sprint_waves computes. Pass an empty string to CLEAR (unassigned); omit to leave unchanged."},
         "prospect_bypass": {"type": "boolean",
                             "description": "94c26322 — HUMAN/PLANNING SESSIONS ONLY. Set true to explicitly allow this item through the prospecting safety gate even without code_pointers or confirmed prospect_status. This is the ONLY way to include an unprospected item in a /goal's auto-run claimable batch. Set false to re-enable the structural gate. Omit to leave unchanged. Executor sessions must NOT set this field."},
         "depends_on": {"type": "string",
                        "description": "56f607ec — set/fix another sprint item's id this one depends on (must complete first before this item is claimable/surfaced by get_parallelizable_groups). Previously depends_on could only be set at creation time via add_sprint_item, with no way to correct ordering on an already-filed item — real ordering had to fall back to prose in notes, which get_parallelizable_groups cannot see. Pass an empty string to CLEAR it (independently claimable); omit to leave unchanged. Cannot equal item_id itself (self-dependency)."},
         "require_verification": {"type": "boolean",
                             "description": "e2e1b682 — set true to require an independent fresh-session PASS (see complete_sprint_item's verifier_session_id/verification_verdict) before the item can be completed. A same-session self-report does not satisfy this gate. Set false to re-enable ordinary completion (evidence gate only). Omit to leave unchanged."},
         "require_strict_evidence": {"type": "boolean",
                             "description": "5fe3502e — set true to require STRICT (fail-closed) completion-evidence verification: complete_sprint_item then refuses (STRICT_EVIDENCE_BLOCKED) unless declared evidence is present, resolves to something real on disk/in the DB, isn't stale (predates the current claim), matches the completing session's own worktree, and no file was edited without a claim_file/claim_symbol lock — unless the caller explicitly passes override_strict_evidence=true with a non-empty override_reason (audited). Set false to re-enable ordinary advisory-only evidence checks. Omit to leave unchanged. Equivalent to passing strict_evidence=true on a single complete_sprint_item call, but persists across attempts."},
         "required_tool": {"type": "string",
                  "description": "4d1fb28f — pin (or re-pin) the specific MCP tool/plugin the executor MUST use for this item, rendered as a hard directive in the /goal block — not left to executor habit. Pass an empty string to CLEAR the pin (ordinary executor discretion); omit to leave unchanged."},
         "tool_requirements": _TOOL_REQUIREMENTS_SCHEMA,
         "artifact_kind": _ARTIFACT_KIND_SCHEMA,
         "planned_output": _PLANNED_OUTPUT_SCHEMA,
         "policy": _ARTIFACT_POLICY_SCHEMA,
         "github_channel": {"type": "string", "enum": ["nightly", "stable", "graduated"],
                  "description": "7c82f7c8 — release-channel classification for this item's linked, auto-filed GitHub issue (fdaa5b55), mirroring the channel:nightly / channel:stable labels applied via which issue template (.github/ISSUE_TEMPLATE/) the reporter picked. 'graduated' marks a bug that started as nightly-only noise but is now confirmed reproducing on stable too — needs a real fix before general release. Pass an empty string to CLEAR it; omit to leave unchanged."}},
         "required": ["item_id"]}},
    {"name": "complete_sprint_item", "description":
        "Mark a sprint item done. Pass task_id to link the task that shipped it. "
        "Pass session_id to get a board_change field (items injected mid-run) and an "
        "active-worktree merge reminder in the response. If the item is flagged "
        "required_notes, you MUST pass notes= (evidence: what shipped / how verified) "
        "or a task_id, or completion is refused (EVIDENCE_REQUIRED). If the item is "
        "flagged require_verification (e2e1b682), completion is refused "
        "(VERIFICATION_REQUIRED) unless an independent PASS is on file: pass "
        "verifier_session_id (a DIFFERENT session id from actor — a fresh, no-memory "
        "subsession that inspected the change with read-only tools) and "
        "verification_verdict='pass' to file and check the verdict in this same call. "
        "fdaa5b55 — if the item has a linked GitHub issue, the response carries a "
        "github_issue_action field: issues Meridian itself created (github_issue_source="
        "'meridian_auto') are commented on and auto-closed; any other issue (manual/legacy) "
        "only gets a proposed-closure comment plus a non-blocking HITL for human review — "
        "never auto-closed. "
        "8693b6a8 — claim-ownership gate: if the item is claimed by a DIFFERENT actor "
        "than the one completing it, completion is refused (CLAIM_MISMATCH) UNLESS that "
        "claim is stale (claimed 2h+ ago, or the claiming session is dead/closed) — the "
        "exact stale-cleanup pattern of closing items left behind by a dead session keeps "
        "working automatically. For a live, non-stale foreign claim, pass "
        "force_foreign_claim=true to explicitly acknowledge and complete anyway. "
        "5fe3502e — pass strict_evidence=true (or flag the item require_strict_evidence=true "
        "via update_sprint_item) for STRICT, fail-closed evidence verification: completion is "
        "refused (STRICT_EVIDENCE_BLOCKED, with typed evidence_errors codes — EVIDENCE_ABSENT/"
        "EVIDENCE_INVALID/EVIDENCE_STALE/WRONG_WORKTREE/UNCLAIMED_EDIT) unless evidence is "
        "present, verifiable, fresh, from the right worktree, and every modified file was "
        "claimed. Default (no strict_evidence, no require_strict_evidence) behavior is exactly "
        "the pre-existing advisory-only evidence checks — nothing changes unless you opt in. "
        "a8c0f3b7 — CODE-INTEL PROSPECTING RECEIPT gate: opt in at the PROJECT level via "
        "set_capability_manifest(capabilities=[{id:'code_intel_prospecting', ...}]) — no "
        "per-call flag needed, and a no-op for projects that never declared it. When declared, "
        "completion of an item that has touches_resources and no prospect_bypass is refused "
        "(CODE_INTEL_RECEIPT_MISSING) unless a durable receipt shows a real search_graph/"
        "find_symbol/prospect_symbol call happened since the item was claimed (see "
        "meridian.code_intel_receipt) — or refused (CODE_INTEL_UNAVAILABLE) when the capability "
        "is availability_policy='required' and code-intel itself is unavailable. Pass "
        "override_code_intel_receipt=true with a non-empty override_reason to acknowledge and "
        "complete anyway (audited). 'optional'/'degraded_ok' policies never block — they degrade "
        "with a code_intel_receipt_warning on the returned item instead.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"}, "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "item_id": {"type": "string"},
         "task_id": {"type": "string"},
         "notes": {"type": "string", "description": "Evidence for the completion (what shipped / how it was verified). Persisted on the item; satisfies the required_notes gate."},
         "actor": {"type": "string", "description": "Executor id/name recorded as having completed the item (defaults to session_id). Checked against the item's claim owner (8693b6a8) — a mismatch on a live, non-stale claim is refused unless force_foreign_claim=true."},
         "session_id": {"type": "string", "description": "Optional: include board_change + worktree merge reminder."},
         "verifier_session_id": {"type": "string", "description": "e2e1b682 — session id of the fresh, independent, read-only-tools verifier subsession that PASSED/FAILED this item. Must differ from actor/session_id or the require_verification gate rejects it as non-independent. Ignored on items without require_verification set."},
         "verification_verdict": {"type": "string", "enum": ["pass", "fail"], "description": "e2e1b682 — the fresh verifier subsession's independent PASS/FAIL determination. Required (with verifier_session_id) to satisfy require_verification in the same call as completion."},
         "verification_notes": {"type": "string", "description": "e2e1b682 — optional free-text explanation from the verifier (especially useful on a fail verdict)."},
         "force_foreign_claim": {"type": "boolean", "description": "8693b6a8 — set true to complete an item claimed by a DIFFERENT, still-live (non-stale) actor. An explicit override, never inferred; omit/false for normal completion. Not needed to close items left behind by a stale/dead claiming session — that is detected automatically."},
         "strict_evidence": {"type": "boolean", "description": "5fe3502e — opt in to the STRICT, fail-closed evidence gate for THIS call only (see meridian.sprint_evidence_guard). Omit/false preserves the exact pre-existing advisory-only behavior. Equivalent, persistent alternative: update_sprint_item(require_strict_evidence=true)."},
         "override_strict_evidence": {"type": "boolean", "description": "5fe3502e — explicit, audited override of a STRICT_EVIDENCE_BLOCKED rejection. Must be paired with a non-empty override_reason in the SAME call, or it is ignored and the block stands. Never inferred; omit/false for normal strict behavior."},
         "override_reason": {"type": "string", "description": "5fe3502e — REQUIRED alongside override_strict_evidence=true (or a8c0f3b7's override_code_intel_receipt=true): why the rejection is being overridden. Recorded to action_audit_log (who/when/why) — an override with no reason is refused, not silently accepted."},
         "override_code_intel_receipt": {"type": "boolean", "description": "a8c0f3b7 — explicit, audited override of a CODE_INTEL_RECEIPT_MISSING / CODE_INTEL_UNAVAILABLE rejection. Must be paired with a non-empty override_reason in the SAME call, or it is ignored and the block stands. Only relevant for a project that declared the 'code_intel_prospecting' capability."}},
         "required": ["item_id"]}},
    {"name": "reconcile_sprint_drift", "description":
        "Read-only: Cross-reference pending sprint items against recent git commits and "
        "return items that may already be done. Uses keyword matching — confidence 'high' "
        "means 3+ keywords overlap (safe to mark done), 'medium' means 1-2 (verify first). "
        "Also surfaces 'notes_blocker_drift': pending items whose notes describe a deferral "
        "or blocker (keywords: FLAGGED, DEFERRED, BLOCKED, 'not implementable', etc.) but "
        "whose structured fields (blocker_kind, deferred_until) are both unset — these items "
        "will keep surfacing as ordinary claimable work until you call update_sprint_item "
        "with blocker_kind='manual' or deferred_until=<ISO timestamp>. "
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
        "only handoffs filed since you last checked. pending_items/in_progress default-collapse "
        "any parent_id/item_group cluster (2+ items) into one summary row — pass expand=true "
        "for the full ungrouped list.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"}, "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "since": {"type": "string", "description": "Optional ISO timestamp (a prior brief's generated_at). When given, new_handoff_available flags only handoffs filed after it."},
         "expand": {"type": "boolean", "description": "Default false: collapse parent_id/item_group clusters in pending_items/in_progress into one summary row each. Pass true for the full ungrouped list."}},
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
        "Cold sessions read this to know what's still owed. By default, items sharing a "
        "``parent_id`` (subtasks) or ``item_group`` collapse into one summary row per "
        "cluster ({collapsed, cluster_kind, item_group_or_parent, count, done, description, ids}) "
        "instead of listing every item — pass expand=true for the full ungrouped list.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"}, "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "status": {"type": "string",
                    "enum": ["pending", "todo", "in_progress", "provisional_complete",
                             "done", "failed", "skipped", "pushed", "indeterminate"],
                    "description": "Filter by status."},
         "expand": {"type": "boolean",
                    "description": "Default false: collapse parent_id/item_group clusters "
                    "(2+ items) into one summary row each. Pass true for the full "
                    "ungrouped item list (pre-9d8e858c behavior)."}},
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
    {"name": "assign_sprint_waves", "description":
        "58a45b92 — PERSIST the parallel grouping: writes the conflict-free batches "
        "get_parallelizable_groups computes onto each eligible item's stored `wave` "
        "field (group i -> 'wave-{i+1}'), so parallelism becomes deterministic and "
        "inspectable (get_sprint_items surfaces `wave`) instead of recomputed every "
        "call. Only currently-eligible items (pending/todo, dependency-satisfied, "
        "unclaimed, non-manual-blocker) are labelled; blocked/in-flight/done items are "
        "left untouched (re-run once they clear). Idempotent — recomputes from the live "
        "board each call. Hand-override any item afterwards with update_sprint_item(wave=...). "
        "Returns {version, wave_count, assigned, waves: {'wave-1': [ids...], ...}, "
        "blocked_count, undeclared_count}. 605ca2c4 — if active executor sessions are "
        "detected, the response also includes active_session_warning: re-labeling wave "
        "numbers while a session is mid-flight can desync it from a /goal string that "
        "already references specific wave labels.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"}, "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "version": {"type": "string", "description": "Optional: only assign waves to items in this sprint-version bucket."}},
         "required": []}},
    {"name": "complete_wave_gate", "description":
        "d2430713 — EXECUTOR GATE: call this AFTER you have actually run a wave's gate "
        "action list (push, deploy, wait, run_verification) to unblock the next wave's "
        "sprint items. You MUST pass the REAL structured result from run_verification as "
        "verification_payload — the server validates it (status=='ok', exit_code==0). "
        "A self-report ('I think it passed') or a fabricated payload is rejected with a "
        "clear error. On success, writes a wave_gate_results row and returns "
        "{gate_completed, wave_label, next_wave_label, next_wave_item_count, "
        "next_wave_item_ids, gate_id}. Each wave gate may only be completed once "
        "(duplicate calls return an error). Security note: this is a deploy-adjacent "
        "gate — only actual run_verification output satisfies it. ed8e4524 — SCOPED "
        "TO SPRINT VERSION: pass version (or session_id to auto-resolve the calling "
        "session's scope) so two different sprint versions that happen to share the "
        "SAME wave_label (e.g. both have a 'wave-2') never satisfy or unblock each "
        "other's gate — omit both to keep the exact prior project-wide behavior for "
        "a single-version project.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"},
         "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "wave_label": {"type": "string", "description": "The wave whose gate is being completed, e.g. 'wave-1'. Must match the wave field on sprint_items that were just executed."},
         "verification_payload": {"type": "object", "description": "The FULL dict returned by run_verification. Must have status='ok' and exit_code=0. Any other value (non-zero exit, error, not_configured, not_connected) is rejected. Do NOT fabricate or self-report — the server validates the payload."},
         "actor": {"type": "string", "description": "Optional session_id or actor name to record who completed the gate."},
         "version": {"type": "string", "description": "ed8e4524 — Optional sprint-version bucket this gate belongs to (e.g. 'v0.2.6'). Wins over session_id's resolved scope. Omit (and omit session_id) for the legacy project-wide gate behavior."},
         "session_id": {"type": "string", "description": "ed8e4524 — Optional: resolve the version scope from this session's own sprint_version (same helper handoff._resolve_session_sprint_version uses for checkpoint) when version is not given explicitly."}},
         "required": ["wave_label", "verification_payload"]}},
    {"name": "start_wave_run", "description":
        "2a654cb0 — DURABLE WAVE STATE: open a wave run before dispatching a parallel "
        "wave. Returns an immutable wave_run_id pinned to the canonical expanded board "
        "snapshot (revision_hash + monotonic revision_counter) the wave was planned "
        "against, so a session that dies mid-wave can be resumed against a manifest "
        "whose staleness is DETECTABLE instead of assumed. The snapshot is built "
        "server-side — you cannot supply one, because the point is to pin what the "
        "server saw. Pass item_ids (the sprint items in this wave) and optionally "
        "failure_modes ({item_id: 'stop'|'continue'}) to register them as children up "
        "front: a failed 'stop' child then structurally BLOCKS finalize_wave_run. "
        "degraded_tools ([{tool, reason, fallback}]) records which tools were "
        "unavailable while the wave ran, so a later reader knows the evidence quality. "
        "Returns {wave_run_id, run, children, revision_hash, revision_counter}.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"},
         "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "version": {"type": "string", "description": "Optional sprint-version bucket this wave covers. Scopes the pinned board snapshot to that bucket."},
         "wave_label": {"type": "string", "description": "Optional label for the wave, e.g. 'wave-2'."},
         "item_ids": {"type": "array", "items": {"type": "string"}, "description": "Sprint item ids in this wave. Registered as children in status='running'."},
         "failure_modes": {"type": "object", "description": "Optional {item_id: 'stop'|'continue'}. A 'stop' child that later fails blocks finalization. Unlisted items default to 'continue'."},
         "degraded_tools": {"type": "array", "items": {"type": "object"}, "description": "Optional [{tool, reason, fallback}] provenance for tools unavailable during this wave (e.g. Serena tunnel inactive)."},
         "actor": {"type": "string", "description": "Optional session_id or actor name recorded as opening the run."}},
         "required": []}},
    {"name": "finalize_wave_run", "description":
        "2a654cb0 — IDEMPOTENT FINALIZATION: close a wave run opened by start_wave_run. "
        "Safe to retry: if the run is already merged this returns the ORIGINAL result "
        "with already_finalized=true, writes no row and appends no event (event_count is "
        "identical across the retry — that is the observable proof). Fails CLOSED in "
        "three cases: (1) a failure_mode='stop' child has failed — returns "
        "{finalized: false, blocked_by: [...]} naming the items; (2) "
        "expected_revision_hash does not match the board the run was planned against — "
        "you are holding a stale manifest, re-read the board first; (3) evidence is not "
        "a genuine run_verification result (status='ok', exit_code=0) — the SAME "
        "evidence contract complete_wave_gate enforces; a self-report is rejected. "
        "Returns {finalized, already_finalized, wave_run_id, status, finalized_at, "
        "finalizer_evidence, children_summary, event_count}.",
     "inputSchema": {"type": "object", "properties": {
         "wave_run_id": {"type": "string", "description": "The immutable id returned by start_wave_run."},
         "evidence": {"type": "object", "description": "The FULL dict returned by run_verification. Must have status='ok' and exit_code=0. Not required when replaying an already-finalized run."},
         "expected_revision_hash": {"type": "string", "description": "Optional staleness gate: the board revision_hash you believe this run was planned against. A mismatch refuses the finalization instead of merging against unseen state."},
         "actor": {"type": "string", "description": "Optional session_id or actor name recorded as finalizing the run."}},
         "required": ["wave_run_id"]}},
    {"name": "resume_wave", "description":
        "efaa918a — STALE-MANIFEST GATING: check whether a wave run opened by "
        "start_wave_run is still safe to resume against the LIVE board before you "
        "act on its pinned manifest. Re-queries the board across ALL non-done "
        "statuses (pending, todo, in_progress, provisional_complete, indeterminate, "
        "failed, skipped, pushed) via build_board_snapshot — NEVER status='pending' "
        "alone, which is the exact b763d2ba bug class (a sibling-claimed in_progress "
        "item looks like it vanished). Fails CLOSED with SPECIFIC, actionable reasons "
        "the moment the live board differs from the pinned manifest in ANY of: "
        "revision_hash mismatch (added/removed items, status/dependency/resource/"
        "pointer changes — reusing diff_board_snapshots's added/removed/changed_items "
        "shape verbatim as resume_delta), an item's wave membership changed, or an "
        "item was newly marked blocker_kind='superseded' (its premise was replaced). "
        "Optionally also verifies a handoff token: pass goal_token (+ presented_body "
        "to additionally check body-hash binding, efaa918a — closes the 2ee0000c gap "
        "where a genuine token could be re-attached to an edited body and still verify). "
        "Token outcomes keep the four existing distinct meanings from verify_handoff_token "
        "(not_found/wrong_project are real spoofing signals; already_consumed/expired "
        "usually mean a sibling already acted) PLUS the new body_mismatch (a real "
        "spoofing signal — genuine token, edited body). Read-only w.r.t. the wave run "
        "itself (does not advance wave_run status — call advance_wave_run_status "
        "separately once resumable). Returns {resumable, wave_run_id, status, "
        "resume_delta, pinned_revision_hash, live_revision_hash, token_check} on "
        "success, or {error, resumable: false, reasons, resume_delta, token_check} "
        "naming exactly what is stale.",
     "inputSchema": {"type": "object", "properties": {
         "wave_run_id": {"type": "string", "description": "The immutable id returned by start_wave_run."},
         "goal_token": {"type": "string", "description": "Optional <goal_token> value to verify via verify_handoff_token, scoped to this run's project_id."},
         "presented_body": {"type": "string", "description": "Optional canonical body text (e.g. the /goal block) to check against the token's stored body_hash, if any. Only meaningful together with goal_token."}},
         "required": ["wave_run_id"]}},
    {"name": "configure_wave_gate", "description":
        "74a8f420 — PLANNING: configure (or on-the-fly reconfigure) a deterministic action "
        "pipeline attached to a wave or wave-range, ENFORCED STRUCTURALLY — not just "
        "advisory /goal prose. Once set, claim_sprint_item refuses (WAVE_GATE_PENDING) to "
        "claim any item whose wave sorts beyond wave_end until complete_wave_gate records "
        "real run_verification evidence for that boundary. actions is an ordered, non-empty "
        "list of {\"type\": ...} dicts — type must be one of push_dev | push_main | deploy | "
        "wait | run_verification (push_dev/push_main/deploy are run by the executor via "
        "trigger_workflow; run_verification maps onto the run_verification tool whose output "
        "complete_wave_gate requires as evidence; wait is a plain pause step; extra keys per "
        "action, e.g. {\"type\": \"wait\", \"seconds\": 30}, are preserved verbatim). "
        "wave_start (defaults to wave_end) documents a multi-wave range covered by one gate "
        "checkpoint. Re-configuring an un-passed wave_end is an upsert — the pipeline can be "
        "revised right up until an executor completes it; once passed the config is immutable "
        "(returns {\"error\": ...}). Returns {configured, gate_config_id, project_id, "
        "wave_start, wave_end, actions} on success. ed8e4524 — SCOPED TO SPRINT VERSION: "
        "pass version (or session_id to auto-resolve the calling session's scope) so two "
        "different sprint versions that happen to share the SAME wave_end label never "
        "reconfigure or immutably block each other's gate — omit both to keep the exact "
        "prior project-wide behavior for a single-version project.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"},
         "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "wave_end": {"type": "string", "description": "The boundary wave, e.g. 'wave-3'. Any item in a later wave (same 'prefix-N' family) is structurally blocked from claim_sprint_item until this gate completes."},
         "wave_start": {"type": "string", "description": "Optional: first wave covered by this gate (documentation only, defaults to wave_end) — e.g. wave_start='wave-1' with wave_end='wave-3' covers waves 1-3 under one checkpoint."},
         "actions": {"type": "array", "description": "Non-empty ordered list of {\"type\": push_dev|push_main|deploy|wait|run_verification, ...params} action dicts — the deterministic pipeline that must run before the next wave unlocks.", "items": {"type": "object"}},
         "actor": {"type": "string", "description": "Optional session_id or actor name to record who configured the gate."},
         "version": {"type": "string", "description": "ed8e4524 — Optional sprint-version bucket this gate belongs to (e.g. 'v0.2.6'). Wins over session_id's resolved scope. Omit (and omit session_id) for the legacy project-wide gate behavior."},
         "session_id": {"type": "string", "description": "ed8e4524 — Optional: resolve the version scope from this session's own sprint_version (same helper handoff._resolve_session_sprint_version uses for checkpoint) when version is not given explicitly."}},
         "required": ["wave_end", "actions"]}},
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
        "Returns every log_task description logged during the session "
        "(transcript/task_count) PLUS a recent_activity ring-buffer of the "
        "last tool calls the executor made — even before log_task() was called. "
        "Use recent_activity to check signs of life in a running executor. "
        "Useful for post-session review, handoff, or remote planner polling.",
     "inputSchema": {"type": "object", "properties": {
         "session_id": {"type": "string"},
         "activity_limit": {"type": "integer", "description": "Max recent_activity entries to return (default 20, max 50)."}},
         "required": ["session_id"]}},
    {"name": "get_session_activity", "description":
        "Read-only: Return the raw MCP-tool-call heartbeat feed for the given "
        "executor session — a ring-buffer of the last tool calls (newest first, "
        "up to 50 entries). Populated automatically by the MCP dispatcher on "
        "every executor tool call, no log_task() needed. Use this to check "
        "whether an executor is still running when task_count is 0.",
     "inputSchema": {"type": "object", "properties": {
         "session_id": {"type": "string"},
         "limit": {"type": "integer", "description": "Max entries to return (default 20, max 50)."}},
         "required": ["session_id"]}},
    {"name": "get_connection_log", "description":
        "Read-only: Return the recent /mcp connection-event log for this tenant "
        "(newest first, up to 200 entries). Every HTTP /mcp request Meridian "
        "receives is recorded: timestamp, MCP method (initialize/tools/list/"
        "tools/call/...), auth_result (success/oauth/no_token/invalid_token/"
        "expired), tools_returned (tool count for tools/list responses), "
        "client_user_agent, and HTTP response_status. Use this to diagnose "
        "client-side outages (zero tools returned, auth failures, unexpected "
        "User-Agents) in real time or after the fact without needing raw "
        "Fly.io log access.",
     "inputSchema": {"type": "object", "properties": {
         "since": {"type": "string", "description": "ISO timestamp (UTC). Only return events at or after this time. Example: '2026-07-15 03:00:00'"},
         "limit": {"type": "integer", "description": "Max entries to return (default 100, max 200)."}},
         "required": []}},
    {"name": "get_server_logs", "description":
        "Read-only: Return recent application-level WARNING/ERROR/EXCEPTION log records "
        "(newest first, up to 500 entries). Captures any logging.warning() / "
        "logging.error() / unhandled-exception records emitted anywhere in the "
        "Meridian process — not just /mcp request metadata. Use this to diagnose "
        "server-side errors (OAuth flow failures, tools/list timeouts, deploy "
        "health issues, DB connection errors) without needing raw Fly.io log access. "
        "Complements get_connection_log (which covers per-request /mcp metadata only). "
        "Returns {count, since, level_filter, module_filter, entries}.",
     "inputSchema": {"type": "object", "properties": {
         "since": {"type": "string", "description": "ISO timestamp (UTC). Only return entries at or after this time. Example: '2026-07-15 03:00:00'"},
         "seek_to": {"type": "string", "description": "b241a437: Positional seek hint. ISO timestamp (UTC) of the point you want to navigate to. When provided (and since= is absent), the checkpoint index supplies a tight since= bound so the DB scan skips rows older than the target. Use get_server_log_checkpoint first to warm the index. Falls back to a full scan when the index is empty. Example: '2026-07-17 03:00:00'"},
         "limit": {"type": "integer", "description": "Max entries to return (default 100, max 500)."},
         "level_filter": {"type": "string", "enum": ["WARNING", "ERROR", "EXCEPTION"], "description": "Filter to a specific log level. Omit to return all WARNING-and-above entries."},
         "module_filter": {"type": "string", "description": "Substring match against the logger name (e.g. 'meridian.server', 'hosted'). Omit for no filter."}},
         "required": []}},
    {"name": "search_server_logs", "description":
        "222d54f8 — BM25 full-text search over the server_logs ring-buffer. "
        "Complements get_server_logs (which filters by level/module/since) with "
        "keyword-ranked retrieval — useful when you know WHAT went wrong but not "
        "exactly WHEN (e.g. search 'OAuth token refresh' or 'psycopg connection pool' "
        "across the last 2000 log records). Uses DuckDB native FTS (Okapi BM25) with "
        "Porter stemming over a concatenated body of level + logger + message + exc_text. "
        "Incremental: re-syncs only new/evicted rows on each call; repeat queries over "
        "an unchanged log window are near-free. Ring-buffer eviction is handled "
        "consistently — rows pruned from server_logs are removed from the FTS index on "
        "the next call. Returns {query, total_in_index, count, hits:[{id, level, logger, "
        "message, exc_text, recorded_at, score, bm25}]}. Empty/no-match query returns "
        "{hits:[]}.",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "BM25 search terms — keywords across log level, logger name, message text, and traceback (e.g. 'OAuth refresh', 'connection pool timeout', 'psycopg')."},
         "since": {"type": "string", "description": "ISO timestamp (UTC). Only return hits at or after this time (post-BM25 filter). Example: '2026-07-15 03:00:00'"},
         "level": {"type": "string", "enum": ["WARNING", "ERROR", "EXCEPTION"], "description": "Filter hits to a specific log level (post-BM25 filter). Omit to return all levels."},
         "limit": {"type": "integer", "description": "Max ranked hits to return (default 20)."}},
         "required": ["query"]}},
    {"name": "get_server_log_checkpoint", "description":
        "b241a437 -- Read-only: Return the positional/checkpoint index for the server_logs "
        "ring-buffer. The checkpoint is a lightweight 'table of contents' mapping "
        "minute-level timestamp buckets to the first/last row id and row count in that "
        "bucket. Use this for fast navigation through large log windows: find the bucket "
        "just before your target timestamp, then use its min_recorded_at as the since= "
        "argument to get_server_logs to skip all older rows without scanning. "
        "Complementary to search_server_logs (BM25 text search): this is positional "
        "navigation (WHERE in the log?) not semantic ranking (WHAT text?). "
        "The optional seek_to= argument returns the best since= hint directly. "
        "The index is rebuilt from the in-memory snapshot on every get_server_logs / "
        "search_server_logs call, so it is always current. Returns {total_rows, "
        "bucket_granularity_label, min_recorded_at, max_recorded_at, bucket_count, "
        "buckets:[{bucket, count, min_recorded_at, max_recorded_at, first_id, last_id}], "
        "seek_hint (when seek_to= given)}.",
     "inputSchema": {"type": "object", "properties": {
         "seek_to": {"type": "string", "description": "Optional ISO timestamp (UTC). When provided, returns a seek_hint field with the best since= value to pass to get_server_logs to start near this timestamp. Example: '2026-07-17 03:00:00'"}},
         "required": []}},
    {"name": "search_all", "description":
        "Read-only: Universal search across all project content: tasks, notes, pinned decisions, "
        "and sprint items. Uses LIKE matching (SQLite) or ILIKE (Postgres). "
        "Returns grouped results: {tasks, notes, decisions, sprint_items, total}. "
        "sprint_items default-collapse any parent_id/item_group cluster (2+ items) into one "
        "summary row — pass expand=true for the full ungrouped list.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"}, "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "query": {"type": "string"},
         "limit": {"type": "integer", "description": "Max results per type (default 10)."},
         "expand": {"type": "boolean", "description": "Default false: collapse parent_id/item_group clusters in sprint_items into one summary row each. Pass true for the full ungrouped list."}},
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
    {"name": "social_search", "description":
        "Search public social-media / discussion content — a REAL external lookup "
        "(keyless), sibling to paper_search but for social discussion rather than "
        "academic papers. Currently one keyless source via the 'source' param: 'hn' "
        "(default; Hacker News via the Algolia HN Search API, story submissions only, "
        "not raw comments). Returns {query, count, results:[{title, authors, summary, "
        "published, url, discussion_url, points, num_comments, hn_id, ...}]}.",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "Search terms (matches title / story text)."},
         "limit": {"type": "integer", "description": "Max results to return (default 10, max 50)."},
         "source": {"type": "string", "enum": ["hn"], "description": "Which keyless source to search (default 'hn', the only source today)."},
         "sort_by": {"type": "string", "enum": ["relevance", "date"], "description": "Sort order (default relevance; 'date' = most recently submitted first)."}},
         "required": ["query"]}},
    {"name": "github_search", "description":
        "Search GitHub — a REAL external lookup (keyless), sibling to paper_search/"
        "social_search for external prior-art / competitive-repo research. Distinct "
        "from search_code, which only searches the CALLING project's own connected "
        "repo. Two keyless endpoints via the 'type' param: 'code' (default; GitHub "
        "Code Search — actual usage of a symbol/pattern/API across public repos) and "
        "'repo' (GitHub Repository Search — competitor/prior-art repositories by "
        "topic/description/stars). Returns {query, count, results:[{title, authors, "
        "summary, published, url, ...}]} — code rows carry path/repo/sha/score, repo "
        "rows carry repo/stars/forks/language/score.",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "Search terms (GitHub search-qualifier syntax is accepted, e.g. 'language:python foo')."},
         "limit": {"type": "integer", "description": "Max results to return (default 10, max 50)."},
         "type": {"type": "string", "enum": ["code", "repo"], "description": "Which keyless GitHub endpoint to search (default 'code')."},
         "sort_by": {"type": "string", "enum": ["relevance", "date"], "description": "Sort order (default relevance; 'date' = most recently indexed/updated first)."}},
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
         "max_turns": {"type": "integer", "description": "Turn ceiling injected into the /goal string ('Stop after N turns'). Default 200."},
         "max_planning_turns": {"type": "integer", "description": "75ac1c8e — override for the execution_policy planning-turn ceiling (turns allowed before the required first action). Default 1 in immediate/autonomous mode, 10 in relaxed/interactive mode; clamped 1-50. Invalid/non-positive values fall back to the mode default rather than erroring."}},
         "required": []}},
    {"name": "get_capability_manifest", "description":
        "649e095f — Read-only: return a project's structured capability manifest "
        "(id/purpose/required_tools/fallback_chain/provenance/availability_policy/"
        "verification_command per capability), plus its schema version and a "
        "stable content hash for change detection. A project that has never set "
        "one gets an empty manifest back, never an error — old projects continue "
        "unaffected. Foundation-only: this is the raw declared manifest, not yet "
        "resolved against live tool/tunnel availability or profile inheritance.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"}, "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."}},
         "required": []}},
    {"name": "set_capability_manifest", "description":
        "649e095f — Persist a project's structured capability manifest: a list of "
        "capability declarations, each with id, purpose, required_tools (non-empty "
        "list of tool/server names), optional fallback_chain, optional provenance "
        "(string or object), availability_policy ('required'|'optional'|"
        "'degraded_ok', default 'required'), and an optional verification_command. "
        "REPLACES the existing manifest wholesale (not a merge). Rejects "
        "deterministically with {error} on any unknown/missing field, duplicate "
        "capability id, secret-shaped value, or machine-local absolute path — "
        "manifests are shared, multi-machine project state, never a place for "
        "secrets or one executor's local filesystem layout. Normalizes to a "
        "stable, sorted-by-id order so the same capability set always hashes "
        "identically regardless of input order.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"}, "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "capabilities": {"type": "array", "items": {"type": "object", "properties": {
             "id": {"type": "string"},
             "purpose": {"type": "string"},
             "required_tools": {"type": "array", "items": {"type": "string"}},
             "fallback_chain": {"type": "array", "items": {"type": "string"}},
             "availability_policy": {"type": "string", "enum": ["required", "optional", "degraded_ok"]},
             "verification_command": {"type": "string"},
             "provenance": {"type": "string"}}},
             "description": "The full manifest — replaces whatever is currently stored."}},
         "required": ["capabilities"]}},
    {"name": "set_capability_profile", "description":
        "02038afe — Persist ONE layer of the capability-inheritance chain: "
        "workspace -> user -> project -> sprint_version -> item (least to most "
        "specific). scope_type selects the layer; scope_id is that layer's key "
        "(a tenant/workspace id for 'workspace', a user/human id for 'user', the "
        "project_id for 'project', the sprint item's id for 'item', or the "
        "project_id for 'sprint_version' — the sprint version itself is resolved "
        "from whichever sprint item you query via get_effective_capability_profile). "
        "capabilities uses the exact same schema as set_capability_manifest and "
        "REPLACES this scope's capabilities wholesale (not a merge). "
        "disabled_capability_ids explicitly retracts capability ids this scope "
        "inherited from a less specific layer, without redeclaring them — that "
        "list also REPLACES whatever was previously disabled at this scope. "
        "provenance is an optional object recording non-secret context (e.g. "
        "config source label, a config/tool-list hash, observed_at, client/server "
        "identity, fallback policy) — never raw secrets or machine-local absolute "
        "paths, rejected the same way set_capability_manifest rejects them. Use "
        "clear_capability_profile to remove a scope's row entirely instead of "
        "replacing it with an empty one.",
     "inputSchema": {"type": "object", "properties": {
         "scope_type": {"type": "string", "enum": ["workspace", "user", "project", "sprint_version", "item"]},
         "scope_id": {"type": "string", "description": "The key for this layer — see the tool description for what goes here per scope_type."},
         "capabilities": {"type": "array", "items": {"type": "object", "properties": {
             "id": {"type": "string"},
             "purpose": {"type": "string"},
             "required_tools": {"type": "array", "items": {"type": "string"}},
             "fallback_chain": {"type": "array", "items": {"type": "string"}},
             "availability_policy": {"type": "string", "enum": ["required", "optional", "degraded_ok"]},
             "verification_command": {"type": "string"},
             "provenance": {"type": "string"}}},
             "description": "This layer's capability declarations — replaces whatever is currently stored at this scope."},
         "disabled_capability_ids": {"type": "array", "items": {"type": "string"},
             "description": "Capability ids to retract at this scope even though a less specific layer declared them. Replaces this scope's previous disable list."},
         "provenance": {"type": "object", "description": "Non-secret provenance for this layer's declaration (config source, hashes, observed_at, client/server identity, fallback policy). No secrets or machine-local absolute paths."}},
         "required": ["scope_type", "scope_id"]}},
    {"name": "clear_capability_profile", "description":
        "02038afe — Delete a scope's ENTIRE capability profile row (both its "
        "capabilities and its disabled_capability_ids) so it reverts to purely "
        "inheriting from less specific layers. Distinct from disabling individual "
        "capability ids via set_capability_profile's disabled_capability_ids — "
        "this clears the whole layer. Idempotent: clearing an already-empty or "
        "never-set scope is a no-op, not an error.",
     "inputSchema": {"type": "object", "properties": {
         "scope_type": {"type": "string", "enum": ["workspace", "user", "project", "sprint_version", "item"]},
         "scope_id": {"type": "string"}},
         "required": ["scope_type", "scope_id"]}},
    {"name": "get_effective_capability_profile", "description":
        "02038afe — Read-only: resolve and return the MERGED capability profile "
        "for a project (optionally narrowed to one sprint item) across every "
        "applicable layer — workspace -> user -> project -> sprint_version -> item, "
        "least to most specific. A capability id declared at more than one layer "
        "resolves to the most specific layer's declaration; the response's "
        "capability_sources maps each effective capability id to the layer that "
        "won. overrides lists every capability id declared by more than one layer "
        "(each entry flagged conflict=true when the two declarations disagree on "
        "required_tools or availability_policy — the fields that change what an "
        "executor can actually rely on). disabled lists every disable that "
        "actually retracted an inherited capability. Pass sprint_item_id to also "
        "resolve that item's sprint_version and item layers; omit it to get just "
        "workspace/user/project. Never resolves against live tool/tunnel "
        "availability — this is the declared, merged profile only.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"}, "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "sprint_item_id": {"type": "string", "description": "Optional — also resolve this item's sprint_version and item-scoped layers."},
         "user_scope_id": {"type": "string", "description": "Optional — a user/human id whose 'user' layer should be included in the merge."},
         "workspace_scope_id": {"type": "string", "description": "Optional — defaults to 'singleton' (the self-host default workspace key)."}},
         "required": []}},
    {"name": "list_profile_layers", "description":
        "0bec79a7 (PROFILE-5) — Read-only: enumerate every persisted "
        "profile_layers row across the 5-layer hosted_default -> workspace "
        "-> user -> project -> session contract (see "
        "meridian.profile_contract / meridian.db.profile_layers for the "
        "full design), optionally narrowed to one scope_type. Each entry is "
        "shaped exactly like get_profile_layer's return value: scope_type, "
        "scope_id, schema_version, revision, fields, reset_fields, "
        "lifecycle_state (hosted_default only), content_hash, provenance, "
        "updated_at. Ordered by (scope_type, scope_id) for deterministic "
        "output — this is a raw listing, not a resolved/merged view; use "
        "get_effective_profile for the merged per-project result. An empty "
        "table returns [], never an error.",
     "inputSchema": {"type": "object", "properties": {
         "scope_type": {"type": "string", "enum": ["hosted_default", "workspace", "user", "project", "session"],
             "description": "Optional — narrow the listing to one layer. Omit to list every layer of every scope_type."}},
         "required": []}},
    {"name": "get_profile_layer", "description":
        "0bec79a7 (PROFILE-5) — Read-only: return the raw, single-layer "
        "profile for one (scope_type, scope_id) — one row of the "
        "hosted_default -> workspace -> user -> project -> session "
        "contract, with no merging against any other layer. A scope with "
        "no persisted row gets an empty profile back (revision=0, "
        "fields={}), never an error — mirrors get_capability_manifest's "
        "'never a read error' contract. Use get_effective_profile instead "
        "when you want the MERGED, multi-layer view for a project.",
     "inputSchema": {"type": "object", "properties": {
         "scope_type": {"type": "string", "enum": ["hosted_default", "workspace", "user", "project", "session"]},
         "scope_id": {"type": "string", "description": "The key for this layer — a hosted_default policy id (typically 'global'), a tenant/workspace id, a user/human id, the project_id, or the session_id, depending on scope_type."}},
         "required": ["scope_type", "scope_id"]}},
    {"name": "save_profile_layer", "description":
        "0bec79a7 (PROFILE-5) — Validate and persist ONE layer of the "
        "hosted_default -> workspace -> user -> project -> session profile "
        "contract (see meridian.profile_contract.FIELD_REGISTRY for the "
        "full field list and per-field allowed_layers/merge_strategy/"
        "narrow_only rules). REPLACES this scope's stored fields/"
        "reset_fields wholesale (not a merge) — to add one field to an "
        "existing layer, pass the full desired field set, not a delta. "
        "Rejects deterministically with {error} on an unknown field, a "
        "field not allowed at this scope_type, a secret-shaped value, a "
        "machine-local absolute path (outside the field's "
        "path_allowed_from_layer), an unsafe/destructive shell command "
        "(executor_config.test_cmd/deploy_cmd), or a malformed "
        "capability_manifest_ref. expected_revision enables optimistic "
        "concurrency: omit it for last-write-wins, or pass the revision "
        "you last read from get_profile_layer to fail with a structured "
        "{error, code: 'STALE_REVISION', current_revision} instead of "
        "silently clobbering a concurrent write. override_reason is "
        "accepted for forward symmetry with the narrow_only-widen override "
        "knob used at resolve time (get_effective_profile) but this tool "
        "itself never blocks a write on it — narrow_only/safe_direction "
        "enforcement happens at MERGE time, not write time, since a layer "
        "may legitimately declare any value for a field it owns. An "
        "idempotent no-op resave (identical fields/reset_fields) returns "
        "the current row unchanged with no revision bump.",
     "inputSchema": {"type": "object", "properties": {
         "scope_type": {"type": "string", "enum": ["hosted_default", "workspace", "user", "project", "session"]},
         "scope_id": {"type": "string"},
         "fields": {"type": "object", "description": "This layer's field declarations — replaces whatever is currently stored at this scope. See meridian.profile_contract.FIELD_REGISTRY for the valid field names."},
         "reset_fields": {"type": "array", "items": {"type": "string"}, "description": "Field names this layer explicitly resets back to inheriting from a less-specific layer. Replaces this scope's previous reset_fields list."},
         "provenance": {"type": "object", "description": "Non-secret provenance for this layer's declaration (config source, hashes, observed_at, client/server identity). No secrets or machine-local absolute paths."},
         "expected_revision": {"type": "integer", "description": "Optimistic-concurrency guard — must match the scope's current revision or the write is rejected with STALE_REVISION. Omit for last-write-wins."},
         "override_reason": {"type": "string", "description": "Accepted for symmetry with resolve-time override_reason; this tool does not itself gate on it."},
         "actor": {"type": "string", "description": "Optional human/session identity recorded on the hosted_default audit ledger (ignored for non-hosted_default scopes, which are not ledgered)."}},
         "required": ["scope_type", "scope_id"]}},
    {"name": "clone_profile_layer", "description":
        "0bec79a7 (PROFILE-5) — Copy one layer's fields/reset_fields/"
        "provenance onto another scope, going through the exact same "
        "validation/hashing path as save_profile_layer (not a raw copy) — "
        "the target scope's allowed_layers may differ from the source's, "
        "so a field the source layer legally carries can still be rejected "
        "at the target. Rejects with {error} when the source layer does "
        "not exist (revision=0 — cloning nothing is a caller error, not a "
        "silent no-op). Cloning INTO a hosted_default target never carries "
        "over the source's lifecycle_state — a fresh clone always lands in "
        "'draft', exactly like any other first-ever write on a "
        "hosted_default scope; use activate_profile_layer afterward to "
        "publish it.",
     "inputSchema": {"type": "object", "properties": {
         "source_scope_type": {"type": "string", "enum": ["hosted_default", "workspace", "user", "project", "session"]},
         "source_scope_id": {"type": "string"},
         "target_scope_type": {"type": "string", "enum": ["hosted_default", "workspace", "user", "project", "session"]},
         "target_scope_id": {"type": "string"},
         "actor": {"type": "string", "description": "Optional human/session identity recorded on the target's hosted_default audit ledger, if the target is hosted_default."}},
         "required": ["source_scope_type", "source_scope_id", "target_scope_type", "target_scope_id"]}},
    {"name": "activate_profile_layer", "description":
        "0bec79a7 (PROFILE-5) — Advance a hosted_default layer's lifecycle "
        "to 'active' — the single 'publish' operation for the "
        "hosted_default floor (fae6e882 pinned decision collapsed "
        "'publish' and 'activate' into this one tool: a hosted_default "
        "layer becomes authoritative the moment it reaches 'active', so "
        "there is no separate publish step to expose). Only a "
        "draft -> active or deprecated -> active transition is valid; any "
        "other current state (e.g. retired, which is terminal) rejects "
        "with {error}. Idempotent: calling on an already-active scope is a "
        "no-op success (same revision, no new audit row). See "
        "reset_profile_layer for the non-audited 'clear the row entirely' "
        "path, or save_profile_layer followed by this tool for the "
        "audited draft-then-publish flow.",
     "inputSchema": {"type": "object", "properties": {
         "scope_id": {"type": "string", "description": "The hosted_default scope id to activate (typically 'global')."},
         "actor": {"type": "string", "description": "Optional human/session identity recorded on the audit ledger for this transition."}},
         "required": ["scope_id"]}},
    {"name": "reset_profile_layer", "description":
        "0bec79a7 (PROFILE-5) — Delete a scope's ENTIRE profile-layer row "
        "so it reverts to purely inheriting from less-specific layers — "
        "mirrors clear_capability_profile's semantics for the "
        "profile-layers contract. Idempotent: resetting an already-empty "
        "or never-set scope is a no-op, not an error. For hosted_default "
        "this clears the row (back to no-row / implicit draft) but is NOT "
        "an audited lifecycle transition — prefer activate_profile_layer's "
        "lifecycle machinery when you need an audited retire/reactivate "
        "path instead of an unaudited wipe.",
     "inputSchema": {"type": "object", "properties": {
         "scope_type": {"type": "string", "enum": ["hosted_default", "workspace", "user", "project", "session"]},
         "scope_id": {"type": "string"}},
         "required": ["scope_type", "scope_id"]}},
    {"name": "get_profile_layer_revisions", "description":
        "0bec79a7 (PROFILE-5) — Read-only: the hosted_default revision/"
        "audit history for one scope_id, newest first — the rollback/audit "
        "trail the profile contract requires for the one layer that is "
        "'immutable once published'. Only hosted_default writes are "
        "ledgered; a non-hosted_default scope_id always returns []. Each "
        "entry carries revision, content_hash, lifecycle_state, fields, "
        "reset_fields, actor, and created_at.",
     "inputSchema": {"type": "object", "properties": {
         "scope_id": {"type": "string"},
         "limit": {"type": "integer", "description": "Maximum rows to return, newest first. Defaults to 50."}},
         "required": ["scope_id"]}},
    {"name": "get_effective_profile", "description":
        "0bec79a7 (PROFILE-5) — Read-only: resolve and return the MERGED "
        "profile for a project across every applicable layer — "
        "hosted_default -> workspace -> user -> project -> session, least "
        "to most specific (see meridian.db.profile_layers."
        "get_effective_profile). The 'project' layer is synthetic: its 7 "
        "legacy ProjectSettings/executor_config fields come from the "
        "existing get_project_settings authority (zero duplication), and "
        "its 3 new fields (tool_priority_map, capability_manifest_ref, "
        "claim_verification_mode) come from the real profile_layers row. A "
        "hosted_default layer only applies when its lifecycle_state is "
        "'active' or 'deprecated' — 'draft' and 'retired' never contribute "
        "fields but still mark the result degraded/not-executable via the "
        "returned executable/degraded/*_reasons fields. Pass "
        "session_id/user_scope_id to also fold in those layers; "
        "workspace_scope_id/hosted_default_scope_id default to "
        "'singleton'/'global'. Returns {error} for an unknown project_id.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"}, "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "session_id": {"type": "string", "description": "Optional — also resolve this session's session-scoped layer."},
         "user_scope_id": {"type": "string", "description": "Optional — a user/human id whose 'user' layer should be included in the merge."},
         "workspace_scope_id": {"type": "string", "description": "Optional — defaults to 'singleton' (the self-host default workspace key)."},
         "hosted_default_scope_id": {"type": "string", "description": "Optional — defaults to 'global' (the self-host default hosted_default key)."}},
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
        "them before editing. Pass `item_id` (the sprint item you're claiming this "
        "file/symbol for) whenever you know it — it disambiguates the "
        "touches_resources amendment side-effect (c027922d) when your session "
        "holds more than one sprint item in_progress at once.",
     "inputSchema": {"type": "object", "properties": {
         "session_id": {"type": "string"},
         "file_path": {"type": "string"},
         "mode": {"type": "string", "enum": ["read", "write"], "description": "Claim grain (ffa03655). 'write' (default) = EXCLUSIVE: blocks other writers and is blocked by any other session's read claim. 'read' = SHARED: many sessions can read-claim the same file at once (no false contention for parallel reader agents), blocked only by another session's write lock."},
         "symbol": {"type": "string", "description": "Optional symbol to claim (class/function/method name, e.g. 'AuthRouter' or 'AuthRouter.login'). Requires `content`."},
         "content": {"type": "string", "description": "Full source of the file, required when `symbol` is given so the server can resolve the symbol's line range."},
         "item_id": {"type": "string", "description": "Optional sprint item id this claim is being made for (c027922d). When your session holds 2+ sprint items in_progress concurrently, pass this so the touches_resources amendment side-effect is attributed to the right item instead of guessed."}},
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
    {"name": "claim_docx_region", "description":
        "f7ee1ba7 — Model B scoped-region claiming for .docx files. Claim a "
        "specific paragraph/element by its durable `element_id` (the w14:paraId "
        "surfaced by get_document_structure / update_paragraph) so another session "
        "cannot overwrite it concurrently. Two sessions can hold NON-OVERLAPPING "
        "element claims on the SAME file — the real precision benefit vs. a "
        "whole-file lock. An edit to a claimed element_id by another session is "
        "REJECTED structurally (not just advisory) at the update_paragraph level. "
        "A whole-file lock by another session blocks this claim. Returns "
        "{claimed: true, file_path, session_id, element_id} on success or "
        "{claimed: false, reason, message, conflicts} on conflict.",
     "inputSchema": {"type": "object", "properties": {
         "session_id": {"type": "string", "description": "The calling session."},
         "file_path": {"type": "string", "description": "The .docx source path (the same value as the `doc` arg to update_paragraph / ingest_document)."},
         "element_id": {"type": "string", "description": "The target element's durable id (w14:paraId or p{index} fallback) as surfaced by get_document_structure."}},
         "required": ["session_id", "file_path", "element_id"]}},
    {"name": "get_docx_region_claims", "description":
        "f7ee1ba7 — Read-only: list active scoped docx-region claims on a file "
        "(who owns which element_ids). Use before update_paragraph to see whether "
        "the target element is claimed.",
     "inputSchema": {"type": "object", "properties": {
         "file_path": {"type": "string", "description": "The .docx source path."}},
         "required": ["file_path"]}},
    {"name": "release_docx_region_claims", "description":
        "f7ee1ba7 — Release scoped docx-region claims held by a session. "
        "Without element_id releases all claims on the file; with element_id "
        "releases only that one element. Without file_path releases ALL region "
        "claims held by the session across all files.",
     "inputSchema": {"type": "object", "properties": {
         "session_id": {"type": "string", "description": "The session releasing its claims."},
         "file_path": {"type": "string", "description": "Optional: scope release to one file."},
         "element_id": {"type": "string", "description": "Optional: scope release to one element (requires file_path)."}},
         "required": ["session_id"]}},
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
    {"name": "reset_plugin_override", "description":
        "Clear a tenant's stored command/config override for one plugin slot, "
        "resetting it back to the built-in default. Fixes the gap where "
        "stale_override detection (surfaced by list_plugins/get_plugin_details) "
        "could flag a stale per-tenant override but nothing could programmatically "
        "clear it — only dashboard editing worked. Self-hosted only for now: in "
        "hosted mode, returns an explicit error rather than risk writing to the "
        "wrong database (tunnel_plugins lives on the control-plane tenants table, "
        "which this tool call's db handle cannot reach there) — use the dashboard's "
        "Tunnel Plugins settings page for hosted tenants.",
     "inputSchema": {"type": "object", "properties": {
         "slot": {"type": "string", "description": "Plugin slot or name to reset (e.g. 'docs', 'outputs', or the plugin's 'name' field from list_plugins)."},
         "hostname": {"type": "string", "description": "Optional: reset only this machine's override (tunnel_plugins_by_host) instead of the per-tenant default."}},
         "required": ["slot"]}},
    {"name": "get_tunnel_diagnostics", "description":
        "f1e0df55 — Read-only: ONE layered diagnostic snapshot of your tunnel/"
        "connectors, separating what's SAVED in the dashboard from what's "
        "ACTUALLY running so a saved-but-not-yet-applied setting is never "
        "reported as active. Per slot (fs/code/extract/ppt/word/dc/docs/zotero/"
        "outputs/debug): dashboard_configured (persisted), process_active (live "
        "server-side socket), external_child_state (last client-reported "
        "lifecycle state), last_error, and exact remediation text — plus one of "
        "five distinct states: healthy, stale, degraded, quarantined, or "
        "restart_required (persisted config and observed runtime disagree). "
        "Also reports the server-side tool routing cache size, a config "
        "generation + manifest_hash fingerprint for drift detection, and whether "
        "a tools/list re-discovery is pending. Includes a run_id + timestamp for "
        "correlating with support requests. Tokens/credentials are redacted. "
        "Requires an authenticated hosted tenant (tunnel mode) — self-hosted "
        "callers with no tenant get an empty, unauthenticated-shaped snapshot.",
     "inputSchema": {"type": "object", "properties": {
         "hostname": {"type": "string", "description": "Optional: report only this machine's per-host config override instead of the per-tenant default (mirrors get_tunnel_plugins's ?hostname=)."}},
         "required": []}},
    {"name": "refresh_tool_manifest", "description":
        "Read-only: return the authoritative, compact manifest of ALL built-in "
        "Meridian MCP tools (name + one-line summary). Call this when you suspect "
        "your client's tool schema went stale ('I nuked the schema', a tool you "
        "expected is suddenly 'not found', or right after a /compact) — it is a "
        "plain tool CALL, so it works even on clients that ignore the "
        "notifications/tools/list_changed signal (e.g. Claude Desktop). Names "
        "returned here are canonical: a name present here but absent from your "
        "tool list is a stale-schema artifact, not a removed tool. If you're "
        "tunnel-connected, this also forces a synchronous re-aggregation of your "
        "tunnel's plugin tools (filesystem/code-intel/office/etc.) — the returned "
        "`tunnel` object carries a manifest_hash + slot_health + config_generation "
        "snapshot so you can tell a recovered/newly-configured slot is now visible "
        "without a reconnect.",
     "inputSchema": {"type": "object", "properties": {},
         "required": []}},
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
        "Read-only: Poll every 30 seconds until another session is closed or archived. Use this when you need to wait before editing a locked file. "
        "6f9503a9 — BOUNDED: the wait times out after timeout_seconds (default 1800s / 30 min) and returns {done:false, timed_out:true, status} so a stuck subagent in a parallel fan-out fails that one item fast instead of hanging the whole batch. Pass timeout_seconds=0 or a large value to tune; there is no unbounded wait.",
     "inputSchema": {"type": "object", "properties": {
         "watching_session_id": {"type": "string"},
         "timeout_seconds": {"type": "number", "description": "Max seconds to wait before returning done=false, timed_out=true (default 1800). A stuck/never-closing session can't hang the caller past this."}},
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
     "description": "Claim a pending sprint item: sets status to in_progress and records claimed_at + actor. Read-only: false. Rejects if the item is already in_progress, done, failed, skipped, its touches_files overlap active file claims from another live session, or (18c488b6) a touches_resources file:/symbol: entry is locked by another live session — this last check ACQUIRES the resource lock (via claim_file/claim_symbol) as part of claiming, is a hard block regardless of worktree isolation, and rolls back cleanly if the claim itself doesn't land. 54c488b6/54d2c2af: every symbol:/file: resource this acquires also gets a durable lock-granularity receipt (achieved symbol vs. coarse-fallback grain, and why), auditable after the fact independent of this call's response payload.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"}, "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "item_id": {"type": "string"},
         "actor": {"type": "string", "description": "Executor id/name recorded as having claimed the item (5823db0b; defaults to session_id)."},
         "session_id": {"type": "string", "description": "Optional caller session id; its own file claims are ignored for conflict checks, and it is the identity any touches_resources symbol/file locks are acquired under (18c488b6). Omitting it skips resource-lock acquisition entirely (fail-open — no behavior change from before 18c488b6) UNLESS strict_resource_locking=true, in which case a missing session_id on an item that declares resources is refused outright (MISSING_EXECUTION_IDENTITY)."},
         "resource_contents": {"type": "object", "description": "18c488b6 — optional map of {file_path: file_content} for any symbol: entries in the item's touches_resources. The server has no direct filesystem access to your repo, so supplying a file's current content here is what lets a symbol: resource get a REAL AST-resolved line-range lock (via claim_symbol) instead of falling back to a whole-file lock. Omit a file's content (or omit this arg entirely) and its symbol: resources fall back to a whole-file lock with an explicit fallback_reason in the response's resource_lock_scope — never a silent downgrade, UNLESS strict_resource_locking=true (see below), in which case that same fallback is REJECTED instead."},
         "strict_resource_locking": {"type": "boolean", "description": "54d2c2af — default false (zero behavior change). Set true to opt this call into the HARDENED, fail-closed contract: a symbol: resource that cannot get a real symbol-range lock (missing resource_contents for its file, or claim_symbol itself couldn't resolve the symbol — unparseable / not found / ambiguous) is REJECTED (ok=false, error=SYMBOL_LOCK_NOT_APPROVED, all-or-nothing rollback) instead of silently widening to a whole-file lock, unless allow_file_fallback=true is ALSO passed. Also promotes a missing session_id (on an item that declares resources) from the default fail-open skip to a hard MISSING_EXECUTION_IDENTITY block."},
         "allow_file_fallback": {"type": "boolean", "description": "54d2c2af — explicit, audited approval for the whole-file-lock fallback that strict_resource_locking=true would otherwise reject for an unresolved symbol: resource. Ignored when strict_resource_locking is not set (the pre-54d2c2af default already allows this fallback implicitly). Pass true to say 'yes, lock the whole file for this resource' instead of supplying real resource_contents."}},
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
     "description": "Update the tunnel's active Serena repo at runtime. When a planning session switches to a different codebase, call this so subsequent Serena requests (find_symbol, find_referencing_symbols, etc.) route to the new repo without restarting the tunnel. Has no effect when no tunnel is connected. 32ba4125 — pass worktree_id instead of repo_path to activate a REGISTERED git worktree (one created via create_worktree / POST /projects/{id}/worktrees) as a validated code-intel context: the repo_path is resolved server-side from that worktree's own record, so an unregistered/arbitrary path can never be activated this way, and the response's `worktree` field carries fingerprint metadata (worktree_id, project_id, branch, path, registered_at). Passing repo_path directly is unchanged and still works for any path (main-repo/non-worktree use).",
     "inputSchema": {"type": "object", "properties": {
         "repo_path": {"type": "string", "description": "Absolute path to the repository to activate (e.g. /home/me/project or C:\\\\Users\\\\me\\\\project). Ignored when worktree_id is given."},
         "worktree_id": {"type": "string", "description": "32ba4125 — id of a REGISTERED active_worktrees row (from create_worktree) to activate as a validated code-intel context. Takes precedence over repo_path; resolves and validates the path from the worktree's own DB record instead of trusting a caller-supplied path."}},
         "required": []}},
    {"name": "run_verification",
     "description":
        "0e973e52 — run the project's stored test_cmd on YOUR local machine via the "
        "tunnel and return a REAL, structured result — not self-reported. "
        "Fields: {exit_code, passed, failed, stdout_tail, stderr_tail, status, timed_out}. "
        "Returns {status: 'not_configured'} (never an error) when no test_cmd is set; "
        "call set_executor_config(test_cmd='pixi run test') first. "
        "Requires an active `meridian --tunnel`; the hosted server has no access to "
        "your machine (same architectural class as ingest_document / search_code_semantic "
        "/ search_outputs — decision 0dedff91). "
        "Per-project: only runs when test_cmd is configured for that project.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string", "description": "Meridian project id — whose stored test_cmd to run."},
         "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."}},
         "required": []}},
    {"name": "analyze_model_efficiency",
     "description":
        "0fba4cb6 — MECHANICAL (zero-token) model-tier suggestion for a task or "
        "sprint item. Deterministic, rule/heuristic classifier: NO model call, NO "
        "DB, NO network — it mirrors how the ultracode orchestration script spends "
        "zero model tokens on routing. Pass a task descriptor (any of title, "
        "description, file_count, files, touches_resources, size) and it returns a "
        "suggested tier: {tier: 'haiku'|'sonnet'|'opus', score, signals:[{signal, "
        "detail, weight}...], rationale, mode:'mechanical'}. Cheap-leaning signals "
        "(title keywords like 'typo'/'docstring'/'lint', 1 file, size 'xs'/'s') "
        "pull toward 'haiku'; expensive-leaning signals ('refactor'/'migration'/"
        "'auth', many files, touched resources, size 'l'/'xl') pull toward 'opus'. "
        "Use it to route a task to the cheapest sufficient model before spawning an "
        "executor. FOLLOW-UP (out of scope this pass): a second LLM-backed "
        "'semantic' mode that reads the full item for a nuanced second opinion.",
     "inputSchema": {"type": "object", "properties": {
         "title": {"type": "string", "description": "Task / sprint-item title. Scanned for cheap/expensive keyword signals."},
         "description": {"type": "string", "description": "Optional longer description; also scanned for keyword signals."},
         "file_count": {"type": "integer", "description": "Number of files the task touches. Fewer files -> cheaper tier."},
         "files": {"type": "array", "items": {"type": "string"}, "description": "Alternative to file_count: the list of files touched; its length is used when file_count is omitted."},
         "touches_resources": {"type": "array", "items": {"type": "string"}, "description": "Resources (DB/schema/infra/services) the task touches. May also be an integer count. More/any resources -> more expensive."},
         "size": {"type": "string", "enum": ["xs", "s", "m", "l", "xl"], "description": "Optional explicit sprint-item size estimate (case-insensitive). Larger -> more expensive."}},
         "required": []}},
    {"name": "add_custom_hook", "description":
        "273287cb — define a user-creatable Claude Code hook (PreToolUse | PostToolUse | "
        "Stop), generalizing past sprint_guard.sh/.ps1 (the only hook Meridian auto-writes "
        "today). Written into the repo's .claude/hooks/<slug>.sh / .ps1 on the next "
        "generate_handoff — the same auto-inject mechanism sprint_guard already uses. "
        "script_sh (POSIX shell body) is required; script_ps1 (PowerShell body) is "
        "optional — omit it to only ever write the .sh file. matcher is a Claude Code "
        "tool-name regex (e.g. \"Edit|Write\"), ignored for Stop hooks. blocking (default "
        "true) controls determinism vs. suggestion power: true writes the script "
        "byte-for-byte so its own exit code drives REAL Claude Code exit-code-blocking "
        "semantics (exit 2 blocks a PreToolUse call / a Stop / feeds PostToolUse output "
        "back to the model); false wraps it so an exit 2 is downgraded to 1 before it's "
        "written — the hook still runs and its output still surfaces, but it can never "
        "hard-block ('strong suggestion power' without determinism). name must not be "
        "'sprint_guard' (reserved for Meridian's own hook) or collide with an existing "
        "hook's derived slug on this project — both raise a clear {error}.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"},
         "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "name": {"type": "string", "description": "Human-readable hook name; sanitized to a filesystem-safe slug used for the written filename(s). Must not be 'sprint_guard'."},
         "event": {"type": "string", "enum": ["PreToolUse", "PostToolUse", "Stop"], "description": "Which Claude Code hook event this fires on."},
         "script_sh": {"type": "string", "description": "POSIX shell script body (required). Receives the same stdin JSON payload Claude Code passes to any hook."},
         "script_ps1": {"type": "string", "description": "Optional PowerShell script body. Omit to only write the .sh file."},
         "matcher": {"type": "string", "description": "Optional Claude Code tool-name matcher regex (e.g. \"Edit|Write\"); ignored for Stop hooks."},
         "blocking": {"type": "boolean", "description": "Default true. true = real exit-code-blocking semantics (script written verbatim). false = advisory/non-blocking (an exit 2 is downgraded to 1 before writing)."},
         "enabled": {"type": "boolean", "description": "Default true. Disabled hooks are skipped on the next generate_handoff write (their files aren't touched, but also aren't refreshed)."}},
         "required": ["name", "event", "script_sh"]}},
    {"name": "get_custom_hooks", "description":
        "273287cb — list a project's user-defined hooks (newest first). Optional event "
        "filter and enabled_only flag. Each entry includes the derived slug (the "
        "filename stem used when written to .claude/hooks/) alongside the stored fields.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"},
         "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "event": {"type": "string", "enum": ["PreToolUse", "PostToolUse", "Stop"], "description": "Optional filter to only this event's hooks."},
         "enabled_only": {"type": "boolean", "description": "When true, only return hooks with enabled=true."}},
         "required": []}},
    {"name": "delete_custom_hook", "description":
        "273287cb — delete a user-defined hook by id (the id returned by add_custom_hook "
        "/ get_custom_hooks). Idempotent: deleting an already-gone hook returns "
        "{deleted:false} rather than erroring, matching delete_sprint_item_pointer's "
        "convention. Does NOT remove any already-written .claude/hooks/<slug>.* files — "
        "those are simply no longer refreshed on the next generate_handoff.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"},
         "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "hook_id": {"type": "string", "description": "The hook id to delete."}},
         "required": ["hook_id"]}},
    {"name": "update_custom_hook", "description":
        "b4f4627f — the previously-missing generic enable/disable/edit path for a "
        "user-defined hook (id returned by add_custom_hook / get_custom_hooks): patches "
        "name, event, matcher, script_sh, script_ps1, blocking, and/or enabled without "
        "the delete+recreate round-trip add_custom_hook/delete_custom_hook would "
        "otherwise require. At least one editable field is required. Renaming re-derives "
        "the slug (same reserved-name / uniqueness checks as add_custom_hook); the db "
        "layer raises ValueError for a bad event, the reserved 'sprint_guard' name, or a "
        "slug collision — surfaced as {error}. Returns {error} (never raises) when "
        "hook_id doesn't resolve for this project. Flipping enabled true -> false also "
        "removes any already-written .claude/hooks/<slug>.* files immediately (best-"
        "effort, when the project has a resolvable repo_path) instead of waiting for the "
        "next generate_handoff to simply stop re-writing them — reported back as "
        "removed_files when any were deleted.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"},
         "project_name": {"type": "string", "description": "Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given."},
         "hook_id": {"type": "string", "description": "The hook id to update."},
         "name": {"type": "string", "description": "New human-readable name; re-derives the slug (must not be 'sprint_guard' or collide with another hook's slug on this project)."},
         "event": {"type": "string", "enum": ["PreToolUse", "PostToolUse", "Stop"], "description": "New hook event."},
         "script_sh": {"type": "string", "description": "New POSIX shell script body."},
         "script_ps1": {"type": "string", "description": "New PowerShell script body."},
         "matcher": {"type": "string", "description": "New Claude Code tool-name matcher regex; ignored for Stop hooks."},
         "blocking": {"type": "boolean", "description": "true = real exit-code-blocking semantics. false = advisory/non-blocking."},
         "enabled": {"type": "boolean", "description": "Enable/disable this hook. Disabling immediately removes any already-written artifact files for it (best-effort)."}},
         "required": ["hook_id"]}},
]

_READ_ONLY_TOOLS = {
    "list_projects", "get_project_by_name", "get_goal", "get_notes", "read_note",
    "get_pinned_decisions", "get_proposal_gates", "get_tasks", "search_tasks", "search_all", "search_synthesis",
    "paper_search", "social_search", "github_search",
    "get_session_brief", "get_context_block", "get_hitl_request",
    "list_hitl_requests", "list_sessions", "get_sprint_notes",
    "get_session_log", "get_session_activity", "get_connection_log", "get_server_logs",
    "search_server_logs", "get_server_log_checkpoint",
    "idle_until_session_done", "generate_handoff", "load_handoff",
    "verify_handoff_token",
    "export_ai_log", "export_ai_log_artifacts",
    "get_insights",
    "get_workspace_notes", "get_workspace_decisions", "get_workspace_settings",
    "get_blog_posts",
    "get_sprint_items", "get_sprint_progress", "get_agent_instructions",
    "reconcile_sprint_drift", "get_planning_brief", "get_file_claims",
    "list_plugins", "get_plugin_details", "refresh_tool_manifest",
    "get_tunnel_diagnostics",
    "get_symbol_claims", "get_symbol_hotspots", "get_graph_diff",
    "get_citation_edges",
    "find_similar_equation", "find_symbol_usages",
    "find_similar_figure",
    "find_similar_table",
    "search_outputs",
    "find_outputs_by_source",
    "search_code_semantic",
    "prospect_symbol",
    "get_flag_registry",
    "get_flag_drift",
    "get_sprint_item_pointers", "resolve_sprint_item_pointers",
    "analyze_model_efficiency",
    "get_custom_hooks",
    "batch_read",
    "list_profile_layers", "get_profile_layer", "get_profile_layer_revisions",
    "get_effective_profile",
    "preview_proposal_promotion",
}
_DESTRUCTIVE_TOOLS = {"delete_note", "archive_decision", "dismiss_hitl", "delete_sprint_item_pointer", "delete_custom_hook", "purge_ai_log"}

# MCP directory metadata: these tools contact a public third-party service
# rather than only reading Meridian's own state.  Keep this separate from the
# read-only set: a GitHub/paper/social search can be read-only for Meridian
# while still operating in an external open world.
_OPEN_WORLD_TOOLS = {"paper_search", "social_search", "github_search"}

# ---------------------------------------------------------------------------
# a749f87c — Deterministic tool pre-selection metadata.
#
# Each tool carries two declared tags (authored once at definition time, not
# inferred at runtime):
#
#   category        — functional domain the tool belongs to.
#   role_relevance  — which session role primarily uses this tool:
#                       "executor"  — code-execution, file-claiming, sprint-item flow
#                       "planner"   — planning, research, high-level orchestration
#                       "both"      — universally useful
#
# These are used by _select_active_tool_set (mcp/handler.py) to produce a
# curated "active_tool_set" pushed to the agent in the start_session response —
# zero LLM calls, same deterministic pattern as _classify_task_tier and
# _infer_sprint_type.
#
# Category vocabulary (add new categories here as the tool set grows):
#   "session"            — session lifecycle (start, register, log, handoff, checkpoint)
#   "sprint-management"  — sprint items, board operations, planning briefs
#   "project"            — project CRUD and settings
#   "notes"              — project notes, wiki, insights, findings, research
#   "decisions"          — pinned decisions, workspace decisions
#   "hitl"               — human-in-the-loop requests/answers
#   "workspace"          — workspace-level notes, proposals, sprint items, settings, blog
#   "code-intel"         — semantic code search, prospect_symbol, graph metrics
#   "docx"               — Word/LaTeX document ingestion, equations, figures, tables
#   "file-locking"       — file/symbol claiming, locking, docx region claims
#   "parallel-coord"     — multi-session coordination (messages, findings, barriers)
#   "analysis"           — read-only analysis/synthesis (analyze_sprint, model efficiency)
#   "plugin"             — tunnel plugin management
#   "config"             — executor config, active repo, workspace settings
#   "research"           — paper search, web-captured findings
# ---------------------------------------------------------------------------

_TOOL_CATEGORY: dict[str, str] = {
    # session lifecycle
    "start_session":           "session",
    "register_session":        "session",
    "log_task":                "session",
    "generate_handoff":        "session",
    "load_handoff":            "session",
    "record_handoff_correction": "session",
    "verify_handoff_token":    "session",
    "export_ai_log":           "notes",
    "export_ai_log_artifacts": "notes",
    "purge_ai_log":            "notes",
    "checkpoint":              "session",
    "get_session_brief":       "session",
    "get_context_block":       "session",
    "get_session_log":         "session",
    "get_connection_log":         "session",
    "get_server_logs":            "session",
    "search_server_logs":         "session",
    "get_server_log_checkpoint":  "session",
    "list_sessions":           "session",
    "refresh_context":         "session",
    "heartbeat":               "session",
    "add_sprint_note":         "session",
    "get_sprint_notes":        "session",
    "idle_until_session_done": "session",
    # sprint management
    "add_sprint_item":               "sprint-management",
    "fan_out_sprint_items":          "sprint-management",
    "update_sprint_item":            "sprint-management",
    "complete_sprint_item":          "sprint-management",
    "claim_sprint_item":             "sprint-management",
    "get_sprint_items":              "sprint-management",
    "get_sprint_progress":           "sprint-management",
    "reconcile_sprint_drift":        "sprint-management",
    "get_planning_brief":            "sprint-management",
    "get_parallelizable_groups":     "sprint-management",
    "assign_sprint_waves":           "sprint-management",
    "complete_wave_gate":            "sprint-management",
    "configure_wave_gate":           "sprint-management",
    "start_wave_run":                "sprint-management",
    "finalize_wave_run":             "sprint-management",
    "resume_wave":                   "sprint-management",
    "analyze_sprint":                "sprint-management",
    "split_sprint_item":             "sprint-management",
    "merge_sprint_items":            "sprint-management",
    "add_subtask":                   "sprint-management",
    "add_sprint_item_pointer":       "sprint-management",
    "get_sprint_item_pointers":      "sprint-management",
    "resolve_sprint_item_pointers":  "sprint-management",
    "delete_sprint_item_pointer":    "sprint-management",
    "execute_batch":                 "sprint-management",
    "batch_read":                    "sprint-management",
    "batch_mutate":                  "sprint-management",
    # project CRUD
    "create_project":      "project",
    "set_parent_project":  "project",
    "rename_project":      "project",
    "merge_project":       "project",
    "list_projects":       "project",
    "get_project_by_name": "project",
    "get_goal":            "project",
    "set_goal":            "project",
    "set_north_star":      "project",
    "set_sprint":          "project",
    # notes / knowledge
    "add_note":               "notes",
    "get_notes":              "notes",
    "read_note":              "notes",
    "delete_note":            "notes",
    "get_agent_instructions": "notes",
    "set_agent_instructions": "notes",
    "add_insight":            "notes",
    "get_insights":           "notes",
    "save_finding":           "notes",
    "capture_research_finding": "notes",
    "ingest_document":        "notes",
    "search_all":             "notes",
    "search_tasks":           "notes",
    "search_synthesis":       "notes",
    "get_tasks":              "notes",
    "store_finding":          "notes",
    "get_findings":           "notes",
    # decisions
    "pin_decision":        "decisions",
    "update_decision":     "decisions",
    "validate_assumption": "decisions",
    "get_pinned_decisions": "decisions",
    "archive_decision":    "decisions",
    "add_proposal_gate":   "decisions",
    "resolve_proposal_gate": "decisions",
    "reopen_proposal_gate": "decisions",
    "get_proposal_gates":  "decisions",
    # hitl
    "request_hitl":     "hitl",
    "get_hitl_request": "hitl",
    "list_hitl_requests": "hitl",
    "answer_hitl":      "hitl",
    "dismiss_hitl":     "hitl",
    # workspace-level
    "add_workspace_note":              "workspace",
    "get_workspace_notes":             "workspace",
    "pin_workspace_decision":          "workspace",
    "get_workspace_decisions":         "workspace",
    "get_workspace_settings":          "workspace",
    "update_workspace_settings":       "workspace",
    "add_workspace_sprint_item":       "workspace",
    "get_workspace_sprint_items":      "workspace",
    "update_workspace_sprint_item":    "workspace",
    "complete_workspace_sprint_item":  "workspace",
    "add_workspace_proposal":          "workspace",
    "get_workspace_proposals":         "workspace",
    "advance_proposal_status":         "workspace",
    "promote_proposal":                "workspace",
    "preview_proposal_promotion":      "workspace",
    "commit_proposal_promotion":       "workspace",
    "save_blog_post":                  "workspace",
    "get_blog_posts":                  "workspace",
    "update_md_section":               "workspace",
    "refresh_tool_manifest":           "workspace",
    # code-intel
    "search_code_semantic": "code-intel",
    "prospect_symbol":      "code-intel",
    "get_flag_registry":    "code-intel",
    "search_outputs":       "code-intel",
    "annotate_outputs":     "code-intel",
    "find_outputs_by_source": "code-intel",
    "snapshot_graph_metrics": "code-intel",
    "get_graph_diff":       "code-intel",
    "get_symbol_hotspots":  "code-intel",
    # docx / document editing
    "get_document_structure": "docx",
    "get_latex_structure":    "docx",
    "get_citation_edges":     "docx",
    "resolve_citations":      "docx",
    "index_equation":         "docx",
    "find_similar_equation":  "docx",
    "insert_equation":        "docx",
    "update_paragraph":       "docx",
    "find_symbol_usages":     "docx",
    "index_figure":                   "docx",
    "find_similar_figure":            "docx",
    "link_figure_caption":            "docx",
    "index_table":                    "docx",
    "find_similar_table":             "docx",
    "link_table_caption":             "docx",
    "ingest_document_structure":      "docx",
    "link_flag_to_section":           "docx",
    "get_flag_drift":                 "docx",
    # file locking
    "claim_file":               "file-locking",
    "release_file":             "file-locking",
    "get_file_claims":          "file-locking",
    "get_symbol_claims":        "file-locking",
    "claim_docx_region":        "file-locking",
    "get_docx_region_claims":   "file-locking",
    "release_docx_region_claims": "file-locking",
    # parallel coordination
    "send_message":      "parallel-coord",
    "receive_messages":  "parallel-coord",
    "idle_until_all_done": "parallel-coord",
    # analysis
    "analyze_model_efficiency": "analysis",
    # plugin management
    "list_plugins":      "plugin",
    "get_plugin_details": "plugin",
    "reset_plugin_override": "plugin",
    "get_tunnel_diagnostics": "plugin",
    # config / infra
    "set_executor_config": "config",
    "get_capability_manifest": "config",
    "set_capability_manifest": "config",
    "set_capability_profile": "config",
    "clear_capability_profile": "config",
    "get_effective_capability_profile": "config",
    "list_profile_layers": "config",
    "get_profile_layer": "config",
    "save_profile_layer": "config",
    "clone_profile_layer": "config",
    "activate_profile_layer": "config",
    "reset_profile_layer": "config",
    "get_profile_layer_revisions": "config",
    "get_effective_profile": "config",
    "set_active_repo":     "config",
    "run_verification":    "config",
    "add_custom_hook":     "config",
    "get_custom_hooks":    "config",
    "delete_custom_hook":  "config",
    "update_custom_hook":  "config",
    # research
    "paper_search": "research",
    "social_search": "research",
    "github_search": "research",
}

_TOOL_ROLE_RELEVANCE: dict[str, str] = {
    # ---- executor-focused ----
    "claim_sprint_item":         "executor",
    "complete_sprint_item":      "executor",
    "complete_wave_gate":        "executor",
    "start_wave_run":            "executor",
    "finalize_wave_run":         "executor",
    "resume_wave":               "executor",
    "add_sprint_item":           "executor",
    "update_sprint_item":        "executor",
    "split_sprint_item":         "executor",
    "merge_sprint_items":        "executor",
    "add_subtask":               "executor",
    "add_sprint_item_pointer":       "both",
    "get_sprint_item_pointers":      "both",
    "resolve_sprint_item_pointers":  "both",
    "delete_sprint_item_pointer":    "executor",
    "execute_batch":                 "both",
    "batch_read":                    "both",
    "batch_mutate":                  "both",
    "claim_file":                "executor",
    "release_file":              "executor",
    "get_file_claims":           "executor",
    "get_symbol_claims":         "executor",
    "claim_docx_region":         "executor",
    "get_docx_region_claims":    "executor",
    "release_docx_region_claims": "executor",
    "insert_equation":           "executor",
    "update_paragraph":          "executor",
    "index_equation":            "executor",
    "index_figure":              "executor",
    "link_figure_caption":       "executor",
    "index_table":               "executor",
    "link_table_caption":        "executor",
    "ingest_document_structure": "executor",
    "link_flag_to_section":      "executor",
    "annotate_outputs":          "executor",
    "log_task":                  "executor",
    "generate_handoff":          "executor",
    "record_handoff_correction": "executor",
    "checkpoint":                "executor",
    "add_sprint_note":           "executor",
    "heartbeat":                 "executor",
    "run_verification":          "executor",
    "set_active_repo":           "executor",
    # ---- planner-focused ----
    "get_planning_brief":        "planner",
    "assign_sprint_waves":       "planner",
    "configure_wave_gate":       "planner",
    "get_parallelizable_groups": "planner",
    "analyze_sprint":            "planner",
    "reconcile_sprint_drift":    "planner",
    "analyze_model_efficiency":  "planner",
    "set_sprint":                "planner",
    "set_goal":                  "planner",
    "set_north_star":            "planner",
    "pin_decision":              "planner",
    "update_decision":           "planner",
    "validate_assumption":       "planner",
    "archive_decision":          "planner",
    "add_workspace_proposal":    "planner",
    "get_workspace_proposals":   "planner",
    "advance_proposal_status":   "planner",
    "promote_proposal":          "planner",
    "preview_proposal_promotion": "planner",
    "commit_proposal_promotion": "planner",
    "update_md_section":         "planner",
    "save_blog_post":            "planner",
    "paper_search":              "planner",
    "social_search":             "planner",
    "github_search":             "planner",
    "capture_research_finding":  "planner",
    "add_insight":               "planner",
    "get_insights":              "planner",
    "save_finding":              "planner",
    "add_workspace_sprint_item":       "planner",
    "update_workspace_sprint_item":    "planner",
    "complete_workspace_sprint_item":  "planner",
    "get_workspace_sprint_items":      "planner",
    "snapshot_graph_metrics":    "planner",
    "get_graph_diff":            "planner",
    "get_symbol_hotspots":       "planner",
    "pin_workspace_decision":    "planner",
    "update_workspace_settings": "planner",
    # ---- both ----
    "start_session":             "both",
    "register_session":          "both",
    "load_handoff":              "both",
    "verify_handoff_token":      "both",
    "export_ai_log":             "both",
    "export_ai_log_artifacts":   "both",
    "purge_ai_log":              "executor",
    "refresh_context":           "both",
    "get_context_block":         "both",
    "get_session_brief":         "both",
    "get_session_log":           "both",
    "get_connection_log":           "both",
    "get_server_logs":              "both",
    "search_server_logs":           "both",
    "get_server_log_checkpoint":    "both",
    "list_sessions":             "both",
    "idle_until_session_done":   "both",
    "get_sprint_notes":          "both",
    "create_project":            "both",
    "set_parent_project":        "both",
    "rename_project":            "both",
    "merge_project":             "both",
    "list_projects":             "both",
    "get_project_by_name":       "both",
    "get_goal":                  "both",
    "get_sprint_items":          "both",
    "get_sprint_progress":       "both",
    "get_agent_instructions":    "both",
    "set_agent_instructions":    "both",
    "set_executor_config":       "both",
    "get_capability_manifest":   "both",
    "set_capability_manifest":   "both",
    "set_capability_profile":    "both",
    "clear_capability_profile":  "both",
    "get_effective_capability_profile": "both",
    "list_profile_layers":       "both",
    "get_profile_layer":         "both",
    "save_profile_layer":        "both",
    "clone_profile_layer":       "both",
    "activate_profile_layer":    "both",
    "reset_profile_layer":       "both",
    "get_profile_layer_revisions": "both",
    "get_effective_profile":     "both",
    "add_custom_hook":           "both",
    "get_custom_hooks":          "both",
    "delete_custom_hook":        "both",
    "update_custom_hook":        "both",
    "add_note":                  "both",
    "get_notes":                 "both",
    "read_note":                 "both",
    "delete_note":               "both",
    "get_tasks":                 "both",
    "search_tasks":              "both",
    "search_all":                "both",
    "search_synthesis":          "both",
    "get_pinned_decisions":      "both",
    "add_proposal_gate":         "both",
    "resolve_proposal_gate":     "both",
    "reopen_proposal_gate":      "both",
    "get_proposal_gates":        "both",
    "get_workspace_decisions":   "both",
    "get_workspace_notes":       "both",
    "add_workspace_note":        "both",
    "get_workspace_settings":    "both",
    "get_blog_posts":            "both",
    "request_hitl":              "both",
    "get_hitl_request":          "both",
    "list_hitl_requests":        "both",
    "answer_hitl":               "both",
    "dismiss_hitl":              "both",
    "search_code_semantic":      "both",
    "prospect_symbol":           "both",
    "get_flag_registry":         "both",
    "get_flag_drift":            "both",
    "search_outputs":            "both",
    "find_outputs_by_source":    "both",
    "get_document_structure":    "both",
    "get_latex_structure":       "both",
    "get_citation_edges":        "both",
    "resolve_citations":         "both",
    "find_similar_equation":     "both",
    "find_symbol_usages":        "both",
    "find_similar_figure":       "both",
    "find_similar_table":        "both",
    "store_finding":             "both",
    "get_findings":              "both",
    "send_message":              "both",
    "receive_messages":          "both",
    "idle_until_all_done":       "both",
    "list_plugins":              "both",
    "get_plugin_details":        "both",
    "reset_plugin_override":     "both",
    "get_tunnel_diagnostics":    "both",
    "refresh_tool_manifest":     "both",
    "ingest_document":           "both",
    "fan_out_sprint_items":      "both",  # orchestrators also use it; keep "both"
}

# ---------------------------------------------------------------------------
# b905da5a — 3-tier MCP tool workflow classification.
#
# Every tool is classified into one of three tiers that reflect HOW OFTEN it
# is realistically called in a normal single-session workflow:
#
#   "main-workflow"    — called in virtually every session; the essential loop.
#   "common-support"   — called frequently but not every single call; regular
#                        hygiene that most healthy sessions use.
#   "maintenance-only" — genuinely occasional: orchestrator-only primitives,
#                        periodic diagnostics, display-only persistence helpers.
#
# This is stamped onto every tool entry in the loop below (tool["workflow_tier"])
# and is visible in tools/list responses so any MCP client can read it.
# The dashboard Tools Reference view groups tools by this field.
#
# Classification rationale for unlisted tools (those not in Adam's explicit lists):
#   - get_notes, read_note, add_note: common-support — routine note lookups
#   - log_task: common-support — sessions log meaningful work often
#   - get_tasks, search_tasks, search_all, search_synthesis: common-support — regular lookups
#   - get_pinned_decisions, pin_decision, update_decision, archive_decision:
#     common-support — decision lifecycle used regularly in planning sessions
#   - get_hitl_request, list_hitl_requests, answer_hitl, dismiss_hitl: common-support
#   - claim_file, release_file: common-support — file locking per AGENTS.md protocol
#   - get_file_claims: Adam listed under MAINTENANCE but it's used as a file-locking
#     hygiene step in the claim sequence; kept common-support since it's the read-
#     check step before every claim_file call
#   - fan_out_sprint_items, add_subtask: common-support — orchestrators call often
#   - get_sprint_item_pointers, resolve_sprint_item_pointers: common-support
#   - search_code_semantic, prospect_symbol, search_outputs: common-support
#   - ingest_document, get_document_structure, get_latex_structure: common-support
#     in document-focused sessions (occasional for code-only sessions)
#   - paper_search, social_search, github_search, capture_research_finding,
#     save_finding: common-support
#   - get_workspace_proposals, advance_proposal_status: common-support — part of
#     the add_workspace_proposal -> promote_proposal main-workflow arc
#   - run_verification: common-support — routine test running in executor sessions
#   - get_goal, get_sprint_progress: common-support — orientation calls
#   - get_session_brief, get_context_block, refresh_context: common-support
#   - add_sprint_note, get_sprint_notes: common-support — session scratchpad
#   - annotate_outputs: common-support — used alongside search_outputs
#   - validate_assumption: explicitly listed under COMMON SUPPORT by Adam
#   - Everything else (docx write-back, workspace CRUD, config, admin diagnostics,
#     parallel-coord primitives, graph metrics, plugin mgmt, session recovery):
#     maintenance-only — specialized or orchestrator-only use
# ---------------------------------------------------------------------------

_TOOL_WORKFLOW_TIER: dict[str, str] = {
    # ---- MAIN WORKFLOW: every session ----
    "start_session":              "main-workflow",
    "get_planning_brief":         "main-workflow",
    "get_sprint_items":           "main-workflow",
    "add_workspace_proposal":     "main-workflow",
    "promote_proposal":           "main-workflow",
    "add_sprint_item":            "main-workflow",
    "claim_sprint_item":          "main-workflow",
    "complete_sprint_item":       "main-workflow",
    "generate_handoff":           "main-workflow",
    "request_hitl":               "main-workflow",

    # ---- COMMON SUPPORT: frequent hygiene ----
    # explicitly listed by Adam as common-support
    "checkpoint":                 "common-support",
    "add_insight":                "common-support",
    "get_insights":               "common-support",
    "validate_assumption":        "common-support",
    "merge_sprint_items":         "common-support",
    "split_sprint_item":          "common-support",
    "add_sprint_item_pointer":    "common-support",
    "execute_batch":              "common-support",
    "batch_read":                 "common-support",
    "batch_mutate":               "common-support",
    "update_sprint_item":         "common-support",
    # notes / knowledge (regular lookups)
    "log_task":                   "common-support",
    "get_notes":                  "common-support",
    "read_note":                  "common-support",
    "add_note":                   "common-support",
    "get_tasks":                  "common-support",
    "search_tasks":               "common-support",
    "search_all":                 "common-support",
    "search_synthesis":           "common-support",
    # decisions lifecycle
    "pin_decision":               "common-support",
    "get_pinned_decisions":       "common-support",
    "update_decision":            "common-support",
    "archive_decision":           "common-support",
    "add_proposal_gate":          "common-support",
    "resolve_proposal_gate":      "common-support",
    "reopen_proposal_gate":       "common-support",
    "get_proposal_gates":         "common-support",
    # goal / sprint read
    "get_goal":                   "common-support",
    "get_sprint_progress":        "common-support",
    # session orientation
    "get_session_brief":          "common-support",
    "get_context_block":          "common-support",
    "refresh_context":            "common-support",
    "add_sprint_note":            "common-support",
    "get_sprint_notes":           "common-support",
    # hitl management
    "get_hitl_request":           "common-support",
    "list_hitl_requests":         "common-support",
    "answer_hitl":                "common-support",
    "dismiss_hitl":               "common-support",
    # file-locking (claim sequence per AGENTS.md)
    "claim_file":                 "common-support",
    "release_file":               "common-support",
    "get_file_claims":            "common-support",
    # code-intel (used when searching codebase)
    "search_code_semantic":       "common-support",
    "prospect_symbol":            "common-support",
    "get_flag_registry":          "common-support",
    "search_outputs":             "common-support",
    "annotate_outputs":           "common-support",
    "find_outputs_by_source":     "common-support",
    # sprint decomposition / pointers
    "fan_out_sprint_items":       "common-support",
    "add_subtask":                "common-support",
    "get_sprint_item_pointers":   "common-support",
    "resolve_sprint_item_pointers": "common-support",
    # document ingestion (contextual, used in document-focused sessions)
    "ingest_document":            "common-support",
    "get_document_structure":     "common-support",
    "get_latex_structure":        "common-support",
    # research
    "paper_search":               "common-support",
    "social_search":              "common-support",
    "github_search":              "common-support",
    "capture_research_finding":   "common-support",
    "save_finding":               "common-support",
    # workspace proposals workflow arc
    "get_workspace_proposals":    "common-support",
    "advance_proposal_status":    "common-support",
    # test running
    "run_verification":           "common-support",

    # ---- MAINTENANCE ONLY: genuinely occasional ----
    # explicitly listed by Adam as maintenance-only
    "analyze_sprint":             "maintenance-only",
    "get_parallelizable_groups":  "maintenance-only",
    "assign_sprint_waves":        "maintenance-only",
    "reconcile_sprint_drift":     "maintenance-only",
    "get_symbol_hotspots":        "maintenance-only",
    "get_symbol_claims":          "maintenance-only",
    # parallel coordination primitives (orchestrator-only)
    "send_message":               "maintenance-only",
    "receive_messages":           "maintenance-only",
    "idle_until_all_done":        "maintenance-only",
    "store_finding":              "maintenance-only",
    # parallel coordination (single-session wait)
    "idle_until_session_done":    "maintenance-only",
    # session diagnostics / audit
    "get_session_log":            "maintenance-only",
    "get_session_activity":       "maintenance-only",
    "get_connection_log":           "maintenance-only",
    "get_server_logs":              "maintenance-only",
    "search_server_logs":           "maintenance-only",
    "get_server_log_checkpoint":    "maintenance-only",
    # config / setup (one-time or rare)
    "set_executor_config":        "maintenance-only",
    "set_agent_instructions":     "maintenance-only",
    "get_agent_instructions":     "maintenance-only",
    "set_active_repo":            "maintenance-only",
    "add_custom_hook":            "maintenance-only",
    "get_custom_hooks":           "maintenance-only",
    "delete_custom_hook":         "maintenance-only",
    "update_custom_hook":         "maintenance-only",
    # goal / sprint editing (planning boundaries only)
    "set_goal":                   "maintenance-only",
    "set_north_star":             "maintenance-only",
    "set_sprint":                 "maintenance-only",
    # project CRUD
    "create_project":             "maintenance-only",
    "rename_project":             "maintenance-only",
    "merge_project":              "maintenance-only",
    "set_parent_project":         "maintenance-only",
    "list_projects":              "maintenance-only",
    "get_project_by_name":        "maintenance-only",
    # low-level session management
    "register_session":           "maintenance-only",
    "list_sessions":              "maintenance-only",
    "heartbeat":                  "maintenance-only",
    "load_handoff":               "maintenance-only",
    "record_handoff_correction":  "maintenance-only",
    "verify_handoff_token":       "maintenance-only",
    "export_ai_log":              "maintenance-only",
    "export_ai_log_artifacts":    "maintenance-only",
    "purge_ai_log":               "maintenance-only",
    # sprint item pointer cleanup
    "delete_sprint_item_pointer": "maintenance-only",
    # note cleanup
    "delete_note":                "maintenance-only",
    # findings store (orchestrator-only parallel handoff)
    "get_findings":               "maintenance-only",
    # docx write-back / specialized document ops
    "index_equation":             "maintenance-only",
    "find_similar_equation":      "maintenance-only",
    "insert_equation":            "maintenance-only",
    "update_paragraph":           "maintenance-only",
    "find_symbol_usages":         "maintenance-only",
    "index_figure":               "maintenance-only",
    "find_similar_figure":        "maintenance-only",
    "link_figure_caption":        "maintenance-only",
    "index_table":                "maintenance-only",
    "find_similar_table":         "maintenance-only",
    "link_table_caption":         "maintenance-only",
    "ingest_document_structure":  "maintenance-only",
    "claim_docx_region":          "maintenance-only",
    "get_docx_region_claims":     "maintenance-only",
    "release_docx_region_claims": "maintenance-only",
    "get_citation_edges":         "maintenance-only",
    "resolve_citations":          "maintenance-only",
    "link_flag_to_section":       "maintenance-only",
    "get_flag_drift":             "maintenance-only",
    # workspace management (cross-project admin)
    "add_workspace_note":              "maintenance-only",
    "get_workspace_notes":             "maintenance-only",
    "pin_workspace_decision":          "maintenance-only",
    "get_workspace_decisions":         "maintenance-only",
    "get_workspace_settings":          "maintenance-only",
    "update_workspace_settings":       "maintenance-only",
    "add_workspace_sprint_item":       "maintenance-only",
    "get_workspace_sprint_items":      "maintenance-only",
    "update_workspace_sprint_item":    "maintenance-only",
    "complete_workspace_sprint_item":  "maintenance-only",
    "save_blog_post":                  "maintenance-only",
    "get_blog_posts":                  "maintenance-only",
    "update_md_section":               "maintenance-only",
    # plugin / manifest management
    "list_plugins":               "maintenance-only",
    "get_plugin_details":         "maintenance-only",
    "reset_plugin_override":      "maintenance-only",
    "get_tunnel_diagnostics":     "maintenance-only",
    "refresh_tool_manifest":      "maintenance-only",
    # analysis / graph
    "analyze_model_efficiency":   "maintenance-only",
    "snapshot_graph_metrics":     "maintenance-only",
    "get_graph_diff":             "maintenance-only",
}

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
    "add_proposal_gate": "Add Proposal Gate",
    "resolve_proposal_gate": "Resolve Proposal Gate",
    "reopen_proposal_gate": "Reopen Proposal Gate",
    "get_proposal_gates": "Get Proposal Gates",
    "get_workspace_decisions": "Get Workspace Decisions",
    "pin_workspace_decision": "Pin Workspace Decision",
    "get_workspace_notes": "Get Workspace Notes",
    "add_workspace_note": "Add Workspace Note",
    "get_workspace_proposals": "Get Workspace Proposals",
    "add_workspace_proposal": "Add Workspace Proposal",
    "advance_proposal_status": "Advance Proposal Status",
    "promote_proposal": "Promote Proposal to Sprint Item",
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
    "assign_sprint_waves": "Assign Sprint Waves",
    "complete_wave_gate": "Complete Wave Gate",
    "configure_wave_gate": "Configure Wave Gate",
    "start_wave_run": "Start Wave Run",
    "finalize_wave_run": "Finalize Wave Run",
    "resume_wave": "Resume Wave",
    "record_handoff_correction": "Record Handoff Correction",
    "export_ai_log": "Export AI Log",
    "export_ai_log_artifacts": "Export AI Log Artifacts",
    "purge_ai_log": "Purge AI Log",
    "get_planning_brief": "Get Planning Brief",
    "get_file_claims": "Get File Claims",
    "list_plugins": "List Plugins",
    "get_plugin_details": "Get Plugin Details",
    "reset_plugin_override": "Reset Plugin Override",
    "get_tunnel_diagnostics": "Get Tunnel Diagnostics",
    "refresh_tool_manifest": "Refresh Tool Manifest",
    "set_active_repo": "Set Active Repo",
    "analyze_model_efficiency": "Analyze Model Efficiency",
    "index_equation": "Index Equation",
    "find_similar_equation": "Find Similar Equation",
    "insert_equation": "Insert Equation",
    "update_paragraph": "Update Paragraph",
    "find_symbol_usages": "Find Symbol Usages",
    "index_figure": "Index Figure",
    "find_similar_figure": "Find Similar Figure",
    "index_table": "Index Table",
    "find_similar_table": "Find Similar Table",
    "search_outputs": "Search Outputs",
    "annotate_outputs": "Annotate Outputs",
    "find_outputs_by_source": "Find Outputs By Source",
    "search_code_semantic": "Search Code Semantic",
    "run_verification": "Run Verification",
    "prospect_symbol": "Prospect Symbol",
    "get_flag_registry": "Get Flag Registry",
    "link_flag_to_section": "Link Flag to Section",
    "get_flag_drift": "Get Flag Drift",
    "search_server_logs": "Search Server Logs",
    "get_server_log_checkpoint": "Get Server Log Checkpoint",
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
        "openWorldHint": _tool["name"] in _OPEN_WORLD_TOOLS,
        "idempotentHint": _is_read_only,
    }
    # a749f87c — stamp declared category + role_relevance onto every tool entry
    # so callers (e.g. _select_active_tool_set) can filter without re-declaring tags.
    _tool["category"] = _TOOL_CATEGORY.get(_tool["name"], "other")
    _tool["role_relevance"] = _TOOL_ROLE_RELEVANCE.get(_tool["name"], "both")
    # b905da5a — stamp workflow_tier: machine-readable 3-tier classification visible
    # in tools/list responses so any MCP client can see it. Default "common-support"
    # for any tool not explicitly mapped (safe: unknown tools are support-level).
    _tool["workflow_tier"] = _TOOL_WORKFLOW_TIER.get(_tool["name"], "common-support")
    # 37f8e868 — bake tier tag into the description string so ANY MCP client can
    # read it without knowing Meridian's custom workflow_tier field.
    # main-workflow tools are untagged (they ARE the main loop).
    # common-support tools get "[SUPPORT] " prefix.
    # maintenance-only tools get "[MAINTENANCE] " prefix.
    _tier_prefix = {
        "common-support":   "[SUPPORT] ",
        "maintenance-only": "[MAINTENANCE] ",
    }.get(_tool["workflow_tier"], "")
    if _tier_prefix and not _tool.get("description", "").startswith(_tier_prefix):
        _tool["description"] = _tier_prefix + _tool.get("description", "")
    # Directory disclosure contract: mutating tools, plus generate_handoff
    # (which persists a durable handoff despite being read-only-compatible for
    # MCP clients), must disclose hosted/self-hosted storage and deletion.
    if (
        _tool["name"] not in _READ_ONLY_TOOLS
        or _tool["name"] == "generate_handoff"
    ) and _PERSISTENCE_NOTICE not in _tool.get("description", ""):
        _tool["description"] = (
            _tool.get("description", "").rstrip() + " " + _PERSISTENCE_NOTICE
        )


# ---------------------------------------------------------------------------
# a749f87c — Deterministic tool pre-selection (pure function, no I/O)
# ---------------------------------------------------------------------------

# Keyword → category affinity: when a keyword appears in the /goal text, its
# mapped category is added to the active set (keyword expansion).
_KEYWORD_CATEGORY_AFFINITY: dict[str, str] = {
    "code":        "code-intel",
    "codebase":    "code-intel",
    "grep":        "code-intel",
    "search":      "code-intel",
    "semantic":    "code-intel",
    "symbol":      "code-intel",
    "refactor":    "code-intel",
    "extract":     "code-intel",
    "analyze":     "code-intel",
    "graph":       "code-intel",
    "docx":        "docx",
    "word":        "docx",
    "latex":       "docx",
    "equation":    "docx",
    "figure":      "docx",
    "table":       "docx",
    "paragraph":   "docx",
    "thesis":      "docx",
    "document":    "docx",
    "paper":       "research",
    "arxiv":       "research",
    "literature":  "research",
    "survey":      "research",
    "citation":    "research",
    "plan":        "sprint-management",
    "sprint":      "sprint-management",
    "backlog":     "sprint-management",
    "orchestrate": "sprint-management",
    "wave":        "sprint-management",
    "fan":         "sprint-management",
    "workspace":   "workspace",
    "proposal":    "workspace",
    "blog":        "workspace",
}

# Categories whose tools are always included regardless of role.
# Keep this minimal — it only covers the HITL primitives (every session needs
# to surface questions to humans). session and project are included via the
# per-role default sets below, not here, so that role_relevance on individual
# tools (e.g. set_goal=planner, log_task=executor) still applies correctly.
_CORE_CATEGORIES: frozenset[str] = frozenset({"hitl"})

# Base active categories by role.  Both sets include session + project so that
# role_relevance on individual tools filters them correctly (planner-only tools
# in those categories are excluded from executor and vice versa).
_EXECUTOR_DEFAULT_CATEGORIES: frozenset[str] = frozenset({
    "session", "project", "sprint-management", "file-locking",
    "parallel-coord", "hitl", "notes", "decisions", "config", "plugin",
})
_PLANNER_DEFAULT_CATEGORIES: frozenset[str] = frozenset({
    "session", "project", "sprint-management", "decisions", "notes",
    "workspace", "analysis", "research", "hitl", "plugin", "code-intel",
})

# Minimal stop-word set (mirrors handoff._extract_keywords) — inlined here to
# keep mcp_tools.py self-contained (no cross-module import at module level).
_KW_STOP: frozenset[str] = frozenset({
    "a", "an", "the", "and", "or", "in", "on", "at", "to", "for",
    "of", "is", "it", "fix", "add", "update", "remove", "change",
    "with", "from", "by", "via", "use", "set", "get", "put", "new",
    "this", "that", "into", "as", "be", "has", "was", "not", "no",
})


def _extract_kws(text: str) -> set[str]:
    """Minimal keyword extractor — same logic as handoff._extract_keywords."""
    words = re.findall(r"[a-z0-9_/-]{3,}", text.lower())
    return {w for w in words if w not in _KW_STOP}


def match_categories_by_keywords(
    text: "str | None",
    keyword_affinity: "dict[str, str]",
) -> "tuple[set[str], list[str]]":
    """Generalized keyword -> category-affinity matcher (e5a7ce7f, decision 2a3a3882).

    Extracted as a standalone, reusable primitive from the SAME keyword ->
    category lookup ``_select_active_tool_set`` performs inline below, so the
    identical deterministic matching logic is available to callers that are
    NOT selecting among Meridian's own MCP tools — e.g.
    ``meridian.tool_routing``'s "exact category matching" routing layer,
    which matches an arbitrary caller-supplied ``keyword_affinity`` mapping
    (its own categories/tool names, not ``_TOOL_CATEGORY``). This closes the
    "generalizing ``_select_active_tool_set`` beyond Meridian's own tools"
    gap recorded in finding 5569beca / pinned decision 2a3a3882 for item
    78127d55, per decision 2a3a3882: "generalize ... don't build new
    matching/ranking logic."

    Deliberately does NOT replace ``_select_active_tool_set``'s own inline
    loop (below) — that loop's "only record the keyword that FIRST newly
    adds a given category" dedup behavior is specific, already covered by
    passing regression tests, and out of scope to touch here. This function
    instead returns ALL matching keywords (no base-set-aware dedup), which
    is the correct, simpler contract for a generic "does this text match any
    of these categories" caller that has no pre-existing base set to dedup
    against.

    Pure, DB-free, no I/O, same determinism guarantees as ``_extract_kws``.

    Returns ``(matched_categories, keyword_signals)`` — ``keyword_signals``
    lists every keyword (in ``_extract_kws``'s own iteration order) whose
    affinity mapping produced a match, so a caller can surface which
    specific words drove a match even when several keywords map to the same
    category.
    """
    matched: set[str] = set()
    signals: list[str] = []
    if not text:
        return matched, signals
    for kw in _extract_kws(text):
        cat = keyword_affinity.get(kw)
        if cat:
            matched.add(cat)
            signals.append(kw)
    return matched, signals


def _select_active_tool_set(
    role: "str | None",
    goal_text: "str | None" = None,
) -> "dict[str, Any]":
    """a749f87c — Deterministically select a curated tool subset for a session.

    Pure function — NO model call, NO DB, NO network.  Mirrors
    ``_classify_task_tier`` (handler.py) and ``_infer_sprint_type``
    (handoff.py): rule/keyword-based, deterministic, zero-LLM.

    ``role``      — "executor", "planner", or None/other (returns all tools).
    ``goal_text`` — optional /goal string scanned for keyword → category
                    affinity signals to expand the active set.

    Returns::

        {
          "role": str,
          "active_categories": [str, ...],
          "active_tools": [str, ...],
          "excluded_tools": [str, ...],
          "keyword_signals": [str, ...],
          "mode": "deterministic",
        }
    """
    effective_role = (role or "").strip().lower()

    # Fast path: no role or unrecognised role → return everything.
    if effective_role not in ("executor", "planner"):
        all_names = [t["name"] for t in _MCP_TOOLS_LIST]
        return {
            "role": effective_role or "unset",
            "active_categories": sorted({
                _TOOL_CATEGORY.get(n, "other") for n in all_names
            }),
            "active_tools": all_names,
            "excluded_tools": [],
            "keyword_signals": [],
            "mode": "deterministic",
        }

    # Choose base category set by role.
    base_cats: set[str] = set(
        _EXECUTOR_DEFAULT_CATEGORIES
        if effective_role == "executor"
        else _PLANNER_DEFAULT_CATEGORIES
    )

    # Keyword expansion: scan goal text for category affinity signals.
    keyword_signals: list[str] = []
    if goal_text:
        for kw in _extract_kws(goal_text):
            matched_cat = _KEYWORD_CATEGORY_AFFINITY.get(kw)
            if matched_cat and matched_cat not in base_cats:
                base_cats.add(matched_cat)
                keyword_signals.append(kw)

    # Filter tool list.
    #
    # Priority:
    #  1. If the tool's category is in base_cats (default or keyword-expanded):
    #     include IF role_relevance is not explicitly the opposite role.
    #     Exception: core-category tools are always included regardless of
    #     role_relevance (hitl tools are universally required).
    #  2. If the tool's category is NOT in base_cats: exclude.
    #
    # This ordering means keyword expansion wins over role_relevance for
    # keyword-matched categories: if the /goal explicitly mentions "arxiv",
    # paper_search (normally planner-only) is included for an executor too.
    active: list[str] = []
    excluded: list[str] = []
    opposite = "planner" if effective_role == "executor" else "executor"
    for tool in _MCP_TOOLS_LIST:
        name = tool["name"]
        cat = _TOOL_CATEGORY.get(name, "other")
        rel = _TOOL_ROLE_RELEVANCE.get(name, "both")

        # Core categories: always include regardless of role_relevance.
        if cat in _CORE_CATEGORIES:
            active.append(name)
            continue

        if cat in base_cats:
            # Category is active; include UNLESS role is explicitly the opposite.
            # Exception: if the category was keyword-expanded (not in the role's
            # default set), include anyway — the user explicitly asked for it.
            in_role_default = cat in (
                _EXECUTOR_DEFAULT_CATEGORIES
                if effective_role == "executor"
                else _PLANNER_DEFAULT_CATEGORIES
            )
            if rel == opposite and in_role_default:
                # Planner/executor-only tool in a default category: exclude.
                excluded.append(name)
            else:
                active.append(name)
        else:
            excluded.append(name)

    return {
        "role": effective_role,
        "active_categories": sorted(base_cats),
        "active_tools": active,
        "excluded_tools": excluded,
        "keyword_signals": keyword_signals,
        "mode": "deterministic",
    }


# ---------------------------------------------------------------------------
# INVESTIGATE f30bbd89 (item_group: proposal:rag-semantic-tool-routing)
# "define offline routing benchmarks, shadow-mode telemetry, reproducible
# tie-breaking, audit provenance, and rollout gates" for a semantic
# (embedding-based) upgrade to MCP tool-routing/pre-selection.
#
# THIS BLOCK IS DESIGN ONLY -- nothing below is wired up or imported by
# anything. It exists so a future implementer of the "rag-semantic-tool-
# routing" proposal starts from a grounded design instead of a blank page,
# and so this investigation is reviewable without an ad hoc root-level
# markdown file -- mirroring how retrieval_eval.py's and
# db/vector_index_state.py's own module docstrings already carry design
# rationale next to the code they describe, rather than in a separate doc.
#
# WHAT EXISTS TODAY (read this before building anything new)
# ---------------------------------------------------------------------
# There are two unrelated "routing/search" mechanisms in this codebase and
# it is easy to conflate them:
#
#  1. Tool pre-selection (a749f87c, THIS file, `_select_active_tool_set`
#     above) -- decides which MCP TOOLS to recommend for a session. Pure
#     keyword-category membership: role -> base category set, /goal text
#     keywords -> category expansion (`_KEYWORD_CATEGORY_AFFINITY`). No
#     embeddings, no scores, no ties -- a tool's presence is a boolean
#     (category in base_cats), not a ranked decision. Its output
#     (`active_tool_set`) is ADVISORY ONLY today: `meridian/mcp/handlers/
#     project_tools.py`'s start_session handler stuffs it into the
#     orientation response, but the MCP `tools/list` surface (server.py,
#     backed by `_MCP_TOOLS_LIST`) always returns every tool regardless --
#     `active_tool_set` never filters what a client can actually call. In
#     the shadow-mode sense used below, this mechanism has effectively
#     always run in "shadow" (zero power to hide a tool), but with zero
#     telemetry on whether its recommendation was ever followed.
#
#  2. Semantic/RAG content search (56cd8712 / 3d3ccf2d,
#     meridian/semantic_search.py) -- decides which NOTES / DECISIONS /
#     SPRINT-ITEMS a keyword-miss query should surface. Real Model2Vec
#     embeddings, real cosine scores, and an already-built, already-tested
#     deterministic tie-break/abstention gate (`score_confidence`: absolute
#     floor + nearest-neighbor margin, see its docstring above in this
#     module's sibling file). This is the ONLY place in the codebase today
#     that actually needs, and has, reproducible tie-breaking.
#
# The "rag-semantic-tool-routing" proposal is: apply (2)'s machinery -- real
# embeddings + score_confidence-style scored/ranked candidates + deterministic
# abstention -- to (1)'s problem (choosing tools), instead of (1)'s current
# static keyword dict. That introduces something that does NOT exist today: a
# RANKED, SCORED tool-selection decision that CAN tie -- which is why all 5
# areas below are real gaps, not already-solved problems.
#
# DESIGN -- the 5 areas
# ---------------------------------------------------------------------
# (a) Offline routing benchmarks
#     Reuse retrieval_eval.py's shape rather than reinventing it: a
#     disposable shadow-project harness (mirroring `run_evaluation`) that
#     builds a labeled dataset and turns metrics into a pass/fail
#     `GateDecision` (mirroring `evaluate_gate`). For tool-routing the
#     "corpus" is (goal_text, role, expected_tools_or_categories) tuples
#     instead of (query, expected_record_id) -- and most of it can be
#     MINED, not hand-written: every historical `start_session(role=...)`
#     paired with the tools a session actually called before its next
#     `generate_handoff` is a free, real label (goal text -> tools actually
#     used). `tests/test_a749f87c_tool_preselection.py`'s existing
#     keyword-expansion cases (code/docx/research) are a ready-made starter
#     set of hand-labeled positives. Metrics: category-level
#     precision/recall against the mined "tools actually used" set (NOT
#     recall@1 -- routing is multi-label; a session legitimately uses many
#     tools across one goal), false-exclusion rate (a tool the session
#     needed but the router would have hidden -- the one unacceptable-by-
#     default metric, mirroring retrieval_eval's zero-leakage hard gate),
#     and decision churn (how much the active set differs run-to-run for
#     IDENTICAL input -- see (c), this must be zero).
#
# (b) Shadow-mode telemetry
#     Never change `active_tool_set`'s current advisory-only contract as
#     part of turning on a semantic scorer. Log BOTH the existing
#     deterministic result and the new semantic candidate result on every
#     `_select_active_tool_set` call, tagged with a decision id, without
#     ever acting on the semantic one. The natural sink is the same
#     append-only `action_audit_log` table `meridian/code_intel_receipt.py`
#     already writes prospecting receipts to (a new event_type
#     discriminates rows in one existing table rather than a new migration)
#     -- e.g. event_type="tool_routing_shadow_decision" with a JSON payload
#     of {deterministic_result, semantic_result, agreement, model_version}.
#     Divergence rate (deterministic active_tools != semantic active_tools)
#     over real traffic is the headline shadow metric and is a PRECONDITION
#     for any rollout-gate discussion in (e) -- the offline benchmark in (a)
#     alone is not sufficient evidence to promote out of shadow.
#
# (c) Reproducible tie-breaking
#     Do not invent a new abstention rule -- port `score_confidence`'s
#     contract verbatim: an absolute confidence floor on the raw semantic
#     score (never the fused one) PLUS a nearest-neighbor margin check in
#     BOTH directions, so two near-tied categories both abstain rather than
#     one being arbitrarily promoted. For tool-routing specifically,
#     "abstain" means: keep the CURRENT deterministic keyword result for
#     that category rather than trusting the semantic scorer -- the
#     deterministic router is always the safe fallback, exactly the role
#     keyword-only search already plays for semantic_search.py (never the
#     other way around). Determinism additionally requires (i) a FIXED
#     candidate order fed to the embedder -- sorted by tool/category name,
#     never dict/set iteration order, so re-runs on identical input cannot
#     silently reorder a tie -- and (ii) rounding scores (score_confidence
#     already rounds to 4dp) before any equality/margin comparison, so
#     float noise from a batch-size-dependent encode path can never flip a
#     decision. The regression tests added alongside this block
#     (test_score_confidence_exact_tie_is_reproducible_and_ambiguous,
#     test_score_confidence_three_way_near_tie_all_abstain,
#     test_select_active_tool_set_is_deterministic_across_repeated_calls)
#     lock in the CURRENT baseline behavior of both routers that this
#     future work must not regress.
#
# (d) Audit provenance
#     Mirror `db/vector_index_state.py`'s pattern exactly: persist, per
#     revision of the routing config (not per call -- volume), the
#     embedding model name/version (`semantic_search.model_name()` already
#     exists for this), a hash of the input (goal-text hash, never raw
#     text -- avoid writing arbitrary user text into a long-lived audit
#     row), the scored candidates, and the final decision + reason
#     ("confident_match" / "ambiguous_runner_up" /
#     "below_confidence_threshold" / "deterministic_fallback" -- the same
#     vocabulary `score_confidence` already returns, extended with the one
#     fallback reason that is new to routing). A benchmark-gated enable
#     flag (e.g. `semantic_routing_enabled`), flipped ONLY by a
#     `record_tool_routing_benchmark`-shaped function analogous to
#     `record_vector_backend_benchmark` -- i.e. the flag is evidence-gated,
#     never hand-flipped, and the evidence blob IS the audit trail (same
#     "do not introduce X merely because it exists" contract
#     vector_index_state.py's docstring states for pgvector).
#
# (e) Rollout gates
#     Three stages, phrased with the capability-manifest availability_policy
#     vocabulary this repo already uses elsewhere (degraded_ok / optional /
#     required -- AGENTS.md, meridian/capability_manifest.py) so a future
#     capability id like "semantic_tool_routing" slots into the existing
#     manifest system without inventing new terminology:
#       stage 0 "shadow"    -- (b)'s logging only, 0% behavioral effect.
#                              Gate to stage 1 requires a minimum shadow
#                              sample size (e.g. >=200 real sessions) AND a
#                              divergence-rate review showing no systematic
#                              false-exclusion pattern.
#       stage 1 "advisory"  -- the semantic result REPLACES the
#                              deterministic one in `active_tool_set`'s
#                              advisory metadata (still non-enforcing --
#                              tools/list is untouched). Gate to stage 2
#                              requires the offline benchmark (a) passing
#                              evaluate_gate-style thresholds
#                              (false_exclusion_rate == 0, precision/recall
#                              above a floor) AND zero unexplained
#                              high-severity divergences carried over from
#                              the stage-0 shadow window.
#       stage 2 "enforcing" -- `active_tool_set` actually filters the
#                              tools/list surface. This is a materially
#                              bigger behavioral change than anything
#                              semantic_search.py ever makes (that system
#                              only ever ADDS candidates atop lexical
#                              results; it never hides one) and needs its
#                              own separate, explicitly HITL-reviewed
#                              sprint item before it is ever built --
#                              deliberately out of scope for this
#                              investigation.
#     Any stage can roll back to "shadow" instantly by flipping the
#     evidence-gated flag in (d) back to disabled -- no code deploy
#     required, mirroring `pgvector_enabled`'s own rollback story.
#
# WHAT THIS INVESTIGATION DELIBERATELY DID NOT BUILD
# ---------------------------------------------------------------------
# No new production module, no new DB table/migration, no embedding
# integration for tool-routing. The lowest-risk, highest-signal next step
# is the offline benchmark harness (a): it needs no new persistence and no
# behavioral change, and it would answer whether a semantic scorer even
# beats the current keyword dict before any telemetry/provenance/rollout
# machinery is worth building at all. The regression tests added alongside
# this block establish the CURRENT deterministic behavior of both existing
# routers as the baseline that harness must be measured against.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# INVESTIGATE db7eb0f5 (item_group: proposal:rag-semantic-tool-routing)
# "compare deterministic sequential/graph orchestration with agentic
# routing and define Meridian's authoritative execution boundary."
#
# THIS BLOCK IS DOCUMENTATION ONLY -- it names and cross-references code that
# already exists and already behaves this way; nothing below changes runtime
# behavior. Companion to the f30bbd89 block directly above (same
# item_group): that block scoped ONE mechanism (MCP tool pre-selection,
# `_select_active_tool_set`); this one answers the broader question the
# item_group's title poses -- across the WHOLE session lifecycle, not just
# tool listing, where does Meridian's own server-side control end and an
# executing agent's free judgment begin. See pinned decision "db7eb0f5:
# Meridian's authoritative execution boundary" for the canonical statement;
# this comment is the code-grounded evidence trail behind it.
#
# TWO MECHANISMS THAT LOOK SIMILAR BUT ARE NOT
# ---------------------------------------------------------------------
#  1. Deterministic sequential/graph orchestration -- decides WHETHER a
#     state transition is allowed to happen at all, and CAN say no.
#     Lives in `meridian/db/sprint_items.py` (`claim_sprint_item`,
#     `complete_sprint_item`, `get_blocking_dependency_for_sprint_item`
#     (the `depends_on` graph), `_get_blocking_wave_gate` /
#     `get_parallelizable_groups` / `assign_sprint_waves` (the wave/
#     resource-conflict graph)), `meridian/capability_manifest.py` +
#     `meridian/capability_contract.py` (a `required` capability with no
#     available fallback makes the session non-executable -- fail closed,
#     per AGENTS.md's capability-manifest contract), and the completion
#     evidence gates layered onto `complete_sprint_item` itself
#     (`required_notes`, `require_verification`/e2e1b682,
#     `require_strict_evidence`/5fe3502e, the `code_intel_prospecting`
#     receipt/a8c0f3b7, the claim-ownership check/8693b6a8). Every one of
#     these can REFUSE the call outright (a typed rejection reason, not
#     advice) regardless of what the calling agent intended -- that is the
#     one property that makes this side "authoritative": it is enforced by
#     the server, not by the agent choosing to comply.
#
#  2. Agentic routing -- everything an executing session (Claude Code,
#     Codex, any MCP client) decides FOR ITSELF once it is inside a
#     claimed item, and that Meridian can only ever hint at, never compel:
#     which MCP tool to call and in what order
#     (`_select_active_tool_set`/a749f87c above -- advisory-only, per the
#     f30bbd89 block's own finding that `tools/list` ignores it
#     entirely), which notes/decisions are relevant
#     (`semantic_search.py`'s Model2Vec recall -- an ADDITIVE recall aid,
#     never a filter, per decision bbd05ceb), which tool a given item
#     probably needs first (`executor_contract.build_routing_hint` /
#     `build_routing_summary` -- explicit `tool_requirements` when
#     present, else a best-effort keyword-INFERRED default that is always
#     `required_or_preferred="preferred"`, never a hard block -- see its
#     own docstring), how many turns to spend planning before acting
#     (`executor_config.build_execution_policy`'s `max_planning_turns` /
#     `required_first_action` -- a strong, documented CONVENTION an
#     executor is expected to follow, but the field itself carries no
#     server-side enforcement path the way a `claim_sprint_item` rejection
#     does), and when a blocker is "genuine" enough to escalate via
#     `request_hitl` (`GENUINE_BLOCKER_ESCALATION_RULE` -- prose guidance
#     only). None of side (2) can make Meridian actually refuse a
#     `claim_sprint_item`/`complete_sprint_item` call; it only shapes what
#     the agent chooses to do on its own before making one.
#
# THE BOUNDARY
# ---------------------------------------------------------------------
# The authoritative execution boundary is the sprint-item claim/completion
# gate itself -- not a layer "above" orchestration or "below" it. Anything
# that determines WHICH items may run, in WHAT order, and WHETHER a given
# claim/completion is even permitted right now is side (1): deterministic,
# server-enforced, agent-intent-independent. Anything that determines HOW
# an already-claimed item gets done -- tool choice, reasoning order, note
# interpretation, escalation judgment, which routing/search hint to trust
# -- is side (2): agentic, advisory-only, and Meridian never gates
# completion on whether those hints were followed. This is precisely why
# f30bbd89's stage-2 "enforcing" tool-filter proposal was flagged as
# needing its OWN separate, explicitly HITL-reviewed sprint item rather
# than folding into that investigation: moving any part of side (2) so
# that it can refuse an action would be a boundary-crossing change, not an
# incremental one, and deserves review as exactly that.
#
# WHAT THIS INVESTIGATION DELIBERATELY DID NOT BUILD
# ---------------------------------------------------------------------
# No new gate, no new advisory mechanism, no change to any of the
# functions named above. The dependency graph, wave gates, capability
# fail-closed contract, and evidence gates already behave exactly as
# described (each already has its own dedicated test coverage --
# `tests/test_d2430713_complete_wave_gate.py`,
# `tests/test_capability_contract.py`,
# `tests/test_a749f87c_tool_preselection.py`, and the sprint-item
# claim/complete suites in `tests/test_core.py` -- so this block adds no
# new regression tests of its own; it would only be re-asserting facts
# those suites already pin). The deliverable is the boundary statement
# itself, made explicit and citable, so a future item proposing to move
# ANY mechanism from side (2) to side (1) (e.g. f30bbd89's own stage-2) has
# a documented line to say it is crossing.
# ---------------------------------------------------------------------------
