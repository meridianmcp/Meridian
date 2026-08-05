# MCP Tool Reference

Meridian exposes **158 tools** over MCP.

They fall into two usage patterns:

- **Planner sessions** (claude.ai, planning work) - `start_session` · `pin_decision` · `update_decision` · `add_note` · `get_context_block` · `generate_handoff`
- **Executor sessions** (Claude Code, Cursor, automated workers) - `start_session` · `log_task` · `request_hitl` · `get_session_brief` · `generate_handoff`

---

## Quick Reference - 5 tools you use 90% of the time

| Tool | One-liner | Example call |
|------|-----------|-------------|
| `start_session` | Register session, get full project context | `start_session(project_name="my-project", session_name="feature-x", human_id="alice")` |
| `log_task` | Record completed work to the shared task log | `log_task(session_id="sid", project_id="abc-123", description="Wired OAuth redirect")` |
| `checkpoint` | Snapshot progress: auto-capture + delta handoff + next /goal | `checkpoint(session_id="sid", project_id="abc-123")` |
| `pin_decision` | Add an architectural decision to the live constitution | `pin_decision(project_id="abc-123", title="Use psycopg3", body="asyncpg has DLL issues on Windows", category="TECHNICAL")` |
| `request_hitl` | Surface a blocking question to the human queue | `request_hitl(project_id="abc-123", question="Should we rate-limit per IP or per token?", urgency="blocking")` |

> **Tip:** Use `checkpoint()` instead of `generate_handoff()` when ending a session — it also runs `auto_capture` and returns the next `/goal` string.

---
## Starting a session

### `start_session`
Register a session and get the full project context (goal, sprint, recent tasks, decisions) in one call. **Use this instead of `register_session`.**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | optional |  |
| `project_name` | string | optional | Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given. |
| `session_name` | string | optional | Optional (599d0097): omit or leave blank to auto-generate a meaningful name from the first pending sprint item title + a timestamp, instead of inventing a string. |
| `human_id` | string | optional |  |
| `client` | string | optional |  |
| `role` | string | optional | Pass 'executor' to inject executor_config and credentials guidance. |
| `compact` | boolean | optional | Default true — slim orientation. Set false for the full goal/instructions payload. |
| `version` | string | optional | Optional sprint-version bucket (e.g. 'v0.1.x') to scope this session to. Sprint progress/items in the orientation and /goal filter to it. Omit to auto-infer the bucket with the most pending items. |
| `mode` | string | optional | Pass 'continue' to resume an already-active same-name session WITHOUT re-reading the full L0/L1/L2 orientation: returns just session_id + live pending items + the ready-to-paste /goal string. Auto-detected anyway within a 5-min heartbeat window; 'continue' widens that so a known-yours session resumes cleanly even after a longer gap. |

**Example:**
```
start_session(project_name="my-project", session_name="feature-x", human_id="alice", role="executor")
```

---


### `get_session_brief`
Read-only: Call this FIRST for project summaries or to see what a session did — returns session, tasks, decisions, and recent commits in one call. Compact session orientation (<500 tokens): sprint focus, pending items, recent tasks, blocking failures, and open HITL requests. Ideal for worker/automation sessions that don't need the full context.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | optional |  |
| `project_name` | string | optional | Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given. |
| `role` | string | optional | Tailors the brief. 'worker'=sprint+tasks only; 'executor'=adds version-scoped pending items, this session's file claims, and decisions code-anchored to them (pass session_id); 'planner'=adds full decisions/notes/sessions, last-session summary, and decisions needing revisit. |
| `session_id` | string | optional | Caller session id — enables session-scratchpad notes, board-change detection, and (role='executor') file-claim + version scoping. |

**Example:**
```
get_session_brief(project_id="abc-123")
```

---

## Tasks

### `log_task`
Log what this session did, is doing, or failed at. Call frequently — this is the primary signal in the timeline and handoffs.

Valid statuses: `pending` · `in_progress` · `done` · `failed` · `backlog` · `future` · `backburner`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `session_id` | string | required |  |
| `project_id` | string | optional |  |
| `project_name` | string | optional | Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given. |
| `description` | string | required |  |
| `status` | string | optional |  |
| `kind` | string | optional | Entry taxonomy. shipped=work done, found=discovery, decided=arch choice, blocked=blocker. |

**Example:**
```
log_task(session_id="session-uuid", project_id="abc-123", description="Fixed auth bug", status="done")
```

---


### `get_tasks`
[SUPPORT] Read-only: Get recent tasks across all sessions.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | optional |  |
| `project_name` | string | optional | Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given. |
| `limit` | integer | optional |  |

**Example:**
```
get_tasks(project_id="abc-123")
```

---


### `search_tasks`
[SUPPORT] Read-only: Search tasks by keyword or natural-language query. Uses trigram similarity on Postgres, LIKE on SQLite. Returns top matches with similarity score.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | optional |  |
| `project_name` | string | optional | Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given. |
| `query` | string | required |  |
| `limit` | integer | optional |  |

**Example:**
```
search_tasks(project_id="abc-123", query="rate limiting bug")
```

---

## Goal & sprint

### `get_goal`
[SUPPORT] Read-only: Fine-grained — return just the goal fields (north_star, sprint, version_goal) in isolation. Use start_session or get_session_brief for full context including tasks and decisions. Use get_goal when you only need the raw goal fields.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | optional |  |
| `project_name` | string | optional | Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given. |

**Example:**
```
get_goal(project_id="abc-123")
```

---


### `set_goal`
[MAINTENANCE] Set or update the goal state. Persistent-state disclosure: on hosted Meridian, supplied text and project/session metadata are sent to and stored in Meridian's service; self-hosted deployments keep them in the configured local SQLite/Postgres database. This data is visible in the dashboard/API and later project context or handoffs. Delete individual tasks, notes, or decisions where supported, or delete the project/account using the documented controls. Do not include secrets.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | optional |  |
| `project_name` | string | optional | Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given. |
| `content` | string | required |  |

**Example:**
```
set_goal(project_id="abc-123", content="Build a great product")
```

---


### `get_sprint_progress`
Read-only: Sprint progress summary — counts by status, `percent_complete`, and the item list.

**Poll this between tasks.** After each `complete_sprint_item`, call `get_sprint_progress(project_id, session_id)` (pass `session_id`) before claiming the next item. The `board_change` field reports items a planner injected since this session started, so an executor picks them up at the item boundary without restarting — never idle-poll, only poll at task boundaries. The result is cached server-side for **10 seconds**, so parallel sessions polling together share a single DB query.

Statuses include `provisional_complete` — work finished but not yet verified/deployed, a non-terminal state between `in_progress` and `done` that does not count toward `percent_complete`.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | optional |  |
| `project_name` | string | optional | Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given. |
| `session_id` | string | optional | Optional: include board_change (items added since this session started). |
| `version` | string | optional | Filter to a specific sprint version bucket. |
| `item_group` | string | optional | Filter to a specific item group. |

**Example:**
```
get_sprint_progress(project_id="abc-123")
```

---

## Executor config & file coordination

### `set_executor_config`
Store project-level executor defaults so worker sessions start with repo path, env file, test command, deploy command, shell, branch, and the injected credentials rule.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | optional |  |
| `project_name` | string | optional | Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given. |
| `repo_path` | string | optional |  |
| `repo_paths` | array | optional | Known locations [{cwd, hostname}] — merged into existing repo_paths, not overwritten. |
| `env_file` | string | optional |  |
| `test_cmd` | string | optional |  |
| `test_min` | integer | optional |  |
| `deploy_cmd` | string | optional |  |
| `shell_type` | string | optional |  |
| `branch` | string | optional |  |
| `filesystem_roots` | array | optional | Directories the tunnel's filesystem connector may serve (unioned across the tenant's projects). Overwrites the existing list. |
| `serena_repo_path` | string | optional | b970fe07 — default repo path for Serena (the tunnel's code-extractor slot). Auto-fetched at tunnel start; used only when --repo is not passed on the CLI. |
| `codebase_code_dirs` | array | optional | b970fe07 — directories codebase-memory-mcp (the tunnel's code-intel slot) auto-indexes. Deduped-union across the tenant's projects; used only when --code-dir is not passed on the CLI. Overwrites the existing list. |
| `context_threshold` | integer | optional | Turns before a context-budget warning is surfaced to the session. |
| `max_turns` | integer | optional | Turn ceiling injected into the /goal string ('Stop after N turns'). Default 200. |
| `max_planning_turns` | integer | optional | 75ac1c8e — override for the execution_policy planning-turn ceiling (turns allowed before the required first action). Default 1 in immediate/autonomous mode, 10 in relaxed/interactive mode; clamped 1-50. Invalid/non-positive values fall back to the mode default rather than erroring. |

**Example:**
```
set_executor_config(project_id="abc-123", repo_path="/repo", env_file="/repo/.env", test_cmd="pixi run test", test_min=619, deploy_cmd="git push", shell_type="powershell", branch="dev")
```

---


### `claim_file`
Claim exclusive edit rights on a file path for this session. Locks auto-expire after 2 hours.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `session_id` | string | required |  |
| `file_path` | string | required |  |
| `mode` | string | optional | Claim grain (ffa03655). 'write' (default) = EXCLUSIVE: blocks other writers and is blocked by any other session's read claim. 'read' = SHARED: many sessions can read-claim the same file at once (no false contention for parallel reader agents), blocked only by another session's write lock. |
| `symbol` | string | optional | Optional symbol to claim (class/function/method name, e.g. 'AuthRouter' or 'AuthRouter.login'). Requires `content`. |
| `content` | string | optional | Full source of the file, required when `symbol` is given so the server can resolve the symbol's line range. |

**Example:**
```
claim_file(session_id="session-uuid", file_path="meridian/server.py")
```

---


### `release_file`
Release a file lock held by this session when you're done editing.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `session_id` | string | required |  |
| `file_path` | string | required |  |

**Example:**
```
release_file(session_id="session-uuid", file_path="meridian/server.py")
```

---


### `idle_until_session_done`
Read-only: Wait on another session before touching a shared file. The tool polls every 30 seconds until the watched session is done.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `watching_session_id` | string | required |  |
| `timeout_seconds` | number | optional | Max seconds to wait before returning done=false, timed_out=true (default 1800). A stuck/never-closing session can't hang the caller past this. |

**Example:**
```
idle_until_session_done(watching_session_id="session-uuid")
```

---

## Parallel coordination

### `store_finding`
[MAINTENANCE] PARALLEL COORDINATION (c35370cc): persist a per-task intermediate result to the session_findings table so it survives session boundaries. Parallel reader agents write findings; an orchestrator or writer agent reads them via get_findings. Unlike save_finding (which creates a research note), this is a lightweight key→content store for agent-to-agent handoff of intermediate work. Persistent-state disclosure: on hosted Meridian, supplied text and project/session metadata are sent to and stored in Meridian's service; self-hosted deployments keep them in the configured local SQLite/Postgres database. This data is visible in the dashboard/API and later project context or handoffs. Delete individual tasks, notes, or decisions where supported, or delete the project/account using the documented controls. Do not include secrets.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | optional |  |
| `project_name` | string | optional | Project name — an alternative to project_id. |
| `content` | string | required | The finding body. |
| `key` | string | optional | Optional bucket/topic for scoped retrieval (e.g. a subsystem name). |
| `title` | string | optional | Optional short title. |
| `session_id` | string | optional | Optional writing session. |
| `task_id` | string | optional | Optional task this finding belongs to. |

---


### `get_findings`
[MAINTENANCE] Read-only (c35370cc): read stored session_findings for a project (newest first), optionally scoped by key and/or session_id. The read side of store_finding. Persistent-state disclosure: on hosted Meridian, supplied text and project/session metadata are sent to and stored in Meridian's service; self-hosted deployments keep them in the configured local SQLite/Postgres database. This data is visible in the dashboard/API and later project context or handoffs. Delete individual tasks, notes, or decisions where supported, or delete the project/account using the documented controls. Do not include secrets.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | optional |  |
| `project_name` | string | optional | Project name — an alternative to project_id. |
| `key` | string | optional | Only findings in this bucket. |
| `session_id` | string | optional | Only findings from this session. |
| `limit` | integer | optional | Max rows (default 50). |

---


### `send_message`
[MAINTENANCE] PARALLEL COORDINATION (d3a3a01d): enqueue an actor-model message to another session (session_messages table). 'Done with X, you do Y' between parallel agents. The recipient reads with receive_messages. A2A-compatible. Persistent-state disclosure: on hosted Meridian, supplied text and project/session metadata are sent to and stored in Meridian's service; self-hosted deployments keep them in the configured local SQLite/Postgres database. This data is visible in the dashboard/API and later project context or handoffs. Delete individual tasks, notes, or decisions where supported, or delete the project/account using the documented controls. Do not include secrets.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | optional |  |
| `project_name` | string | optional | Project name — an alternative to project_id. |
| `to_session_id` | string | required | Recipient session id. |
| `payload` | string | required | Message body (text or JSON). |
| `from_session_id` | string | optional | Sender session id (defaults to session_id). |
| `kind` | string | optional | Optional message kind/tag. |

---


### `receive_messages`
[MAINTENANCE] PARALLEL COORDINATION (d3a3a01d): fetch unread messages addressed to a session (oldest first) and mark them read by default. The receive side of send_message. Persistent-state disclosure: on hosted Meridian, supplied text and project/session metadata are sent to and stored in Meridian's service; self-hosted deployments keep them in the configured local SQLite/Postgres database. This data is visible in the dashboard/API and later project context or handoffs. Delete individual tasks, notes, or decisions where supported, or delete the project/account using the documented controls. Do not include secrets.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `session_id` | string | required | The recipient session. |
| `mark_read` | boolean | optional | Mark fetched messages read (default true). |
| `limit` | integer | optional | Max messages (default 50). |

---


### `idle_until_all_done`
[MAINTENANCE] PARALLEL COORDINATION (d3a3a01d): non-blocking barrier check across sibling sessions. Returns {all_done, pending, statuses}; a session is done when closed/archived/missing. The server can't block, so poll until all_done is true — the A2A 'wait for X, Y, Z to finish' primitive. Persistent-state disclosure: on hosted Meridian, supplied text and project/session metadata are sent to and stored in Meridian's service; self-hosted deployments keep them in the configured local SQLite/Postgres database. This data is visible in the dashboard/API and later project context or handoffs. Delete individual tasks, notes, or decisions where supported, or delete the project/account using the documented controls. Do not include secrets.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `session_ids` | array | required | Sessions to wait on. |

---

## Decisions

### `pin_decision`
Record an authoritative decision that supersedes earlier statements. Pinned decisions appear in every session's context block.

Categories: `STRATEGIC` · `COMPETITIVE` · `TECHNICAL` · `TACTICAL` · `BUSINESS` · `PRODUCT` · `ARCHITECTURAL`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | optional |  |
| `project_name` | string | optional | Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given. |
| `title` | string | required |  |
| `body` | string | required |  |
| `category` | string | optional |  |
| `priority` | string | optional | urgent decisions sort first and are weighted higher in start_session / generate_handoff context. Default normal. |
| `assumption` | string | optional | Optional unverified assumption this decision rests on. Recorded with status 'unvalidated' and surfaced in get_planning_brief until validate_assumption confirms or invalidates it. |

**Example:**
```
pin_decision(project_id="abc-123", title="Use psycopg3", body="asyncpg has DLL issues on Windows", category="TECHNICAL")
```

---


### `update_decision`
Patch a pinned decision. Pass `new_title` + `new_body` to atomically supersede (creates a new row, marks old as superseded). Otherwise patches in place.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `decision_id` | string | required |  |
| `new_title` | string | optional |  |
| `new_body` | string | optional |  |
| `title` | string | optional |  |
| `body` | string | optional |  |
| `category` | string | optional |  |
| `priority` | string | optional | Change ordering/weight (urgent \| normal \| low). |
| `status` | string | optional |  |
| `assumption` | string | optional | Set/replace the decision's underlying assumption text. |
| `assumption_status` | string | optional | Stamp the assumption's validation state. Usually set via the validate_assumption tool, which also fires HITL on invalidation. |

---


### `get_pinned_decisions`
[SUPPORT] Read-only: List pinned decisions, highest priority first (urgent → normal → low, then newest-first). Active only by default. Each row includes its priority and a parsed edit_log array of prior bodies ({body, ts}) recorded on every in-place body edit.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | optional |  |
| `project_name` | string | optional | Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given. |
| `include_superseded` | boolean | optional |  |

**Example:**
```
get_pinned_decisions(project_id="abc-123")
```

---

## Human-in-the-loop (HITL)

### `request_hitl`
Surface a question to the human queue. Response includes `chat_prompt` (question + options formatted for inline display) and, when `urgency='blocking'`, a `poll_instruction`. Dual-channel: filed in the dashboard AND shown in Claude Code chat — first answer wins. For blocking: display `chat_prompt` to the user, then poll `get_hitl_request(request_id)` every 30 s. If the user answers in chat, call `answer_hitl(request_id, answer)`. `normal`/`high` land in the dashboard without blocking the session.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | optional |  |
| `project_name` | string | optional | Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given. |
| `question` | string | required |  |
| `session_id` | string | optional |  |
| `context` | string | optional |  |
| `urgency` | string | optional |  |
| `kind` | string | optional | question (default, auto-answerable) or correction (non-blocking mid-run human correction). |
| `assigned_to` | string | optional |  |
| `options` | array | optional | Answer choices rendered as selectable buttons in the dashboard. |
| `recommended` | string | optional | The safe-default option — an option string or a 0-based index into options. Highlighted in the dashboard; Enter submits it; auto-answer prefers it. |
| `require_human` | boolean | optional | When true, the HITL can never be auto-answered — only an explicit human response unblocks it. Reserve for irreversible/destructive actions. |

**Example:**
```
request_hitl(project_id="abc-123", question="Should we add rate limiting here?", urgency="normal")
```

---


### `get_hitl_request`
Read-only: Poll a HITL request for the human's answer. Returns the row including `status` (`pending`/`answered`/`dismissed`) and `answer` text.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `request_id` | string | required |  |

**Example:**
```
get_hitl_request(request_id="hitl-uuid")
```

---


### `answer_hitl`
Answer a pending HITL request programmatically. Marks it answered so the waiting session can resume. Use when the human answers in Claude Code chat rather than the dashboard.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `request_id` | string | required |  |
| `answer` | string | required |  |
| `answered_by` | string | optional | Optional human_id of the answerer. |

---


### `dismiss_hitl`
Dismiss a HITL request (won't-answer / no longer relevant).

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `request_id` | string | required |  |

---

## Handoff & context

### `generate_handoff`
Read-only: Generate a context handoff document. `mode='full'` writes the complete L0/L1/L2 handoff. `mode='delta'` returns a compact session summary with completed items, pending items, and the next `/goal` string.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | optional |  |
| `project_name` | string | optional | Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given. |
| `mode` | string | optional |  |
| `session_id` | string | optional | Optional session id for auto-delta on repeated calls in the same session. |
| `root_dir` | string | optional | Optional request-local absolute source-tree root used by live pointer resolution's local semantic fallback when no code tunnel is available. Never persisted. |
| `version` | string | optional | (b8f89491) Optional explicit sprint-version bucket (e.g. 'v0.2.6') to scope this handoff to — applies to every mode (full/delta/starter/compact/goal), not just starter. Wins over the calling session's own stored sprint_version. Omit to fall back to session_id's scope, or to the whole project's cross-version backlog when neither is set. |
| `force_include_ids` | array | optional | (45f519a0, validated by 3cab355a) Optional list of sprint-item ids to force-include in the pending list even when their deferred_until is in the future. This is a one-off visibility override for this handoff call only — deferred_until is NOT cleared, so claim_sprint_item's own deferral gate is unaffected. Use when a human wants a backburnered item back in scope for one planning run without permanently re-enabling claiming. Every id is validated: it must belong to this project, match the effective version scope (when one applies), and be genuinely todo/pending — an unknown/cross-project/cross-version/not-pending id is rejected (reported in the response's force_include_rejected list, never silently dropped) rather than honoured. Accepted ids are also exempt from the code-pointer enrichment cap, so a requested item always gets prospected regardless of how large the pending board is. |
| `skip_ai_summary` | boolean | optional | 65c8b426 — skip the optional AI (Haiku) narrative calls (session summaries, ai_summary blurb, sprint retrospective). Default true on the MCP path for fast, reliable handoffs. Pass false to include AI-generated narrative sugar when you have budget and time. |
| `strict_evidence` | boolean | optional | (8a883f60) Opt-in, off by default — mirrors complete_sprint_item's strict_evidence shape exactly. When true, a failed/degraded pointer-enrichment/freshness/wave-gate/graph-search capability makes this call refuse to render or persist a handoff at all, returning {error: HANDOFF_EVIDENCE_BLOCKED, evidence_status, evidence_errors, message} instead. Leave false/omitted for today's graceful-degrade behavior (handoff_evidence_status is still returned either way). |
| `strict_pointer_evidence` | boolean | optional | (eb8b6894) Opt-in, off by default, separate from strict_evidence above. When true, the claimable/goal batch's UNPROSPECTED exclusion requires a pending item's durable pointer(s) to have actually RESOLVED (resolve_pointer succeeded), not merely be PRESENT as a row — a structurally-valid-but-unresolved pointer no longer silently satisfies the gate. Never raises/blocks the whole handoff (unlike strict_evidence): an affected item is simply excluded from the claimable batch, the same way today's presence-only UNPROSPECTED gate already excludes items. Every pending item's pointer_resolution_status (structural_valid/target_resolved/provenance_verified/resolution_source/strict_satisfied) is always returned regardless of this flag — it only changes which items make the claimable cut. |
| `checkpoint` | boolean | optional | (ecc8b280) Mark THIS call as a mid-run progress report rather than a final, session-ending handoff. Applies to full/delta modes only. A checkpoint=true call is never refused by strict_continuation below, regardless of how much actionable work remains — it changes nothing about what gets rendered, only whether the continuation gate can engage. |
| `strict_continuation` | boolean | optional | (ecc8b280) Opt-in, off by default — mirrors strict_evidence's shape. When true and checkpoint is not set, refuses to render/persist this handoff (full/delta modes only) if actionable pending/in_progress items remain on the live board with no recorded blocker_kind while execution_mode=autonomous, returning {error: HANDOFF_CONTINUATION_BLOCKED, continuation_status, message} instead. Leave false/omitted for today's behavior (continuation_status is still always returned either way). |

**Example:**
```
generate_handoff(project_id="abc-123", mode="delta", session_id="session-uuid")
```

---


### `get_context_block`
Read-only: Return a compact project context block (north star, sprint, pending sprint items, recent tasks, recent decisions, active sessions) wrapped in a <meridian_context> XML envelope for structured parsing by AI clients (v2.5+). Use `mode='full'` to paste into a fresh Claude Code session; `mode='chat'` for a shorter paste into claude.ai. The HTTP route `/projects/{id}/context-block` returns the same content as unwrapped plain text.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | optional |  |
| `project_name` | string | optional | Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given. |
| `mode` | string | optional |  |

**Example:**
```
get_context_block(project_id="abc-123", mode="chat")
```

---

## Planning tools

### `fan_out_sprint_items`
Bulk-insert sprint items in one call — lets an orchestrator LLM decompose a goal into parallel work items without N sequential `add_sprint_item` calls. Pass a list of `{title, description?, group?, version?}` dicts; returns the list of new item IDs.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | optional |  |
| `project_name` | string | optional | Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given. |
| `items` | array | required | List of sprint item specs. Each must have at least a 'title'. |

**Example:**
```
fan_out_sprint_items(project_id="abc-123", items=[{"title": "Design DB schema", "group": "backend"}, {"title": "Build API endpoints", "group": "backend"}, {"title": "Wire up frontend", "group": "frontend"}])
```

---


### `get_planning_brief`
Read-only: Return a compact planning context (sprint, north star, pending items, in-progress items, recent tasks, active sessions, recent decisions, pending HITLs). No session registration needed — designed for planning chat sessions that need to see project state without side effects.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | optional |  |
| `project_name` | string | optional | Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given. |
| `since` | string | optional | Optional ISO timestamp (a prior brief's generated_at). When given, new_handoff_available flags only handoffs filed after it. |
| `expand` | boolean | optional | Default false: collapse parent_id/item_group clusters in pending_items/in_progress into one summary row each. Pass true for the full ungrouped list. |

**Example:**
```
get_planning_brief(project_id="abc-123")
```

---


### `analyze_sprint`
[MAINTENANCE] PLANNING: Read-only synthesis of the current sprint into one structured brief — parallelizability (conflict-free groups + max fan-out), dependency chains (depends_on walked to the root), resource/file conflicts (items sharing touches_resources), and stalls (stall_count>0). Returns {summary, recommended_strategy, parallelism, dependency_chains, longest_chain, file_conflicts, stalls, blocked, running}. Call in planning sessions instead of stitching together get_parallelizable_groups + manual dependency/conflict analysis. Persistent-state disclosure: on hosted Meridian, supplied text and project/session metadata are sent to and stored in Meridian's service; self-hosted deployments keep them in the configured local SQLite/Postgres database. This data is visible in the dashboard/API and later project context or handoffs. Delete individual tasks, notes, or decisions where supported, or delete the project/account using the documented controls. Do not include secrets.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | optional |  |
| `project_name` | string | optional | Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given. |
| `version` | string | optional | Optional: only analyze items in this sprint-version bucket. |

---


### `reconcile_sprint_drift`
Read-only: Cross-reference pending sprint items against recent git commits and return items that may already be done. confidence='high' means 3+ keywords overlap (safe to mark done via `complete_sprint_item`); confidence='medium' means 1–2 (verify first). Call during planning sessions to identify board drift.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | optional |  |
| `project_name` | string | optional | Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given. |

**Example:**
```
reconcile_sprint_drift(project_id="abc-123")
```

---

## Rate limits

The hosted MCP surface (Bearer-token requests) is metered per tenant per minute by plan:

| Plan | Requests / minute |
|------|-------------------|
| `free` | 500 |
| `standard` | 2000 |
| `pro` | unlimited |

Over-limit requests receive `429 Too Many Requests` with a `Retry-After` header. Dashboard (cookie) traffic, `/health`, and `/static` are never metered, and self-hosted instances are unmetered. Polling `get_sprint_progress` between tasks stays well within these limits — the 10 s server-side cache keeps parallel polling cheap.

---

## Workspace proposals

### `add_workspace_proposal`
Capture a workspace-level flash of insight into the 'drawer of inspiration' — cross-project ideas that don't belong to any one project yet. NOT executor-claimable. status: raw → investigating → promoted|rejected. Use `advance_proposal_status` to move through the lifecycle; `promote_proposal` to convert one into a real sprint item.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `title` | string | required | Short idea title. |
| `body` | string | required | Full description of the insight or idea. |
| `tags` | string | optional | Optional comma-separated tags. |

**Example:**
```
add_workspace_proposal(title="IDEA: expose auth as plugin", body="Could ship auth as a separate optional plugin so self-hosters can swap it out", tags="arch")
```

---


### `get_workspace_proposals`
Read-only: List workspace proposals (human-authored flashes of insight), newest first. When status is omitted, defaults to 'live' proposals only (raw + investigating) — terminal proposals (promoted/rejected) are excluded. Pass status='all' for every status, or an explicit status (including promoted/rejected) to filter to just that one. Optional tag substring filter.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `status` | string | optional | Filter to proposals in this status. Defaults to raw+investigating ('live') when omitted; use 'all' for every status. |
| `tag` | string | optional | Substring filter on tags. |
| `limit` | integer | optional | Maximum proposals to return (default 20, clamped to 1..100). |
| `offset` | integer | optional | Zero-based pagination offset (default 0). |

**Example:**
```
get_workspace_proposals(status="investigating")
```

---


### `advance_proposal_status`
Transition a workspace proposal through its lifecycle (raw → investigating|rejected; investigating → promoted|rejected|raw; rejected → raw). 'promoted' is a terminal status reachable only via `promote_proposal`.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `proposal_id` | string | required |  |
| `status` | string | required | Target status. 'promoted' is not allowed here — use promote_proposal instead. |

**Example:**
```
advance_proposal_status(proposal_id="prop-uuid", status="investigating")
```

---


### `promote_proposal`
Promote a workspace proposal into a real sprint item, creating the link between them. The proposal must be in 'raw' or 'investigating' state. Returns {proposal, sprint_item_id, sprint_item_title, project_id}.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `proposal_id` | string | required |  |
| `project_id` | string | optional | Project to create the sprint item under. |
| `project_name` | string | optional | Project name — alternative to project_id; resolved to the id internally. |
| `sprint_item_title` | string | optional | Override title for the sprint item; defaults to the proposal title. |
| `sprint_item_version` | string | optional | Sprint version for the new item; defaults to 'current'. |

**Example:**
```
promote_proposal(proposal_id="prop-uuid", project_id="proj-uuid", sprint_item_title="Expose auth as plugin")
```

---

## Notes

### `add_note`
Add a per-project wiki note. Use for setup instructions, gotchas, environment details, how-tos.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | optional |  |
| `project_name` | string | optional | Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given. |
| `title` | string | required |  |
| `body` | string | required |  |
| `tags` | string | optional |  |
| `kind` | string | optional |  |
| `priority` | string | optional | high-priority notes surface first in generate_handoff and planner context. |
| `file_path` | string | optional | Code anchor (kind='code'): repo-relative or absolute path this note warns about. Surfaced at claim_file/get_file_claims for the same path. |
| `symbol` | string | optional | Optional symbol (class/function/method) to scope the code anchor to. File-level anchors (no symbol) surface for any symbol in the file. |
| `source` | string | optional | Provenance: a URL or file path this note was ingested from. Stored on the note (used by kind='document'). |
| `category` | string | optional |  |

**Example:**
```
add_note(project_id="abc-123", title="Deploy note", body="Reminder: update env vars before deploy", tags="ops,deploy")
```

---


### `get_notes`
Read-only: List project notes (newest first), LIGHTWEIGHT by default — id/slug/title/tags/kind/priority/timestamps with NO body, so the list can't overflow context. Pull model: scan the list, then `read_note(project_id, slug)` for one note's full body. Filter by tag substring or `query` full-text search. Pass `bodies=true` only when you truly need every body inline. Pass `limit` (default 100, max 500) and/or `cursor` for a `{notes, has_more, next_cursor}` page, then re-call with `cursor=next_cursor`.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | optional |  |
| `project_name` | string | optional | Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given. |
| `tag` | string | optional |  |
| `query` | string | optional | Text search across note title and body (case-insensitive). |
| `bodies` | boolean | optional | Default false. true returns full note bodies inline (legacy behavior) — usually unnecessary; prefer read_note(slug). |
| `limit` | integer | optional | Page size (default 100, clamped 1..500). Passing limit or cursor switches the result to the {notes, has_more, next_cursor} pagination envelope. |
| `cursor` | integer | optional | Offset cursor from a prior page's next_cursor. Passing it switches the result to the {notes, has_more, next_cursor} envelope. |
| `sort` | string | optional | 98890df1 — 'relevance' ranks notes by reference_count/recency/decision-link (heavily cross-referenced notes surface, stale ones sink) and returns a bare list with a per-note 'relevance' score; default 'recency'. |

**Example:**
```
get_notes(project_id="abc-123")
```

---


### `read_note`
Read-only: Fetch one project note's full body by its per-project `slug` (the `slug` field from `get_notes`). The pull half of the list→read model.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | optional |  |
| `project_name` | string | optional | Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given. |
| `slug` | string | required | The note's slug (kebab-cased, unique per project) as returned by get_notes. |

**Example:**
```
read_note(project_id="abc-123", slug="deploy-note")
```

---


### `delete_note`
[MAINTENANCE] Hard-delete a project note by id. Persistent-state disclosure: on hosted Meridian, supplied text and project/session metadata are sent to and stored in Meridian's service; self-hosted deployments keep them in the configured local SQLite/Postgres database. This data is visible in the dashboard/API and later project context or handoffs. Delete individual tasks, notes, or decisions where supported, or delete the project/account using the documented controls. Do not include secrets.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `note_id` | string | required |  |

---

## Projects

### `create_project`
[MAINTENANCE] Create a new Meridian project. Persistent-state disclosure: on hosted Meridian, supplied text and project/session metadata are sent to and stored in Meridian's service; self-hosted deployments keep them in the configured local SQLite/Postgres database. This data is visible in the dashboard/API and later project context or handoffs. Delete individual tasks, notes, or decisions where supported, or delete the project/account using the documented controls. Do not include secrets.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `name` | string | required |  |
| `execution_mode` | string | optional | Executor posture for sessions on this project. 'autonomous' (default) claims and runs sprint items immediately without asking; 'interactive' asks for direction first. Editable later in dashboard Settings. |
| `parent_project_id` | string | optional | Optional parent project id — makes this a subproject that inherits the parent's north_star when it has none of its own. Subprojects are one level deep: the parent must exist and must not itself be a subproject. |

**Example:**
```
create_project(name="my-app")
```

---


### `merge_project`
[MAINTENANCE] d6bd60e0 — merge a phantom-duplicate project INTO another. Re-parents EVERY child row of the source project (sprint items, tasks, decisions, insights, notes, HITL requests, sessions, handoffs, pointers, …) to the target project via pure UPDATEs — NO row is ever deleted. By default the now-empty source project is soft-archived (status='archived', name prefixed with '[merged] '), never hard-deleted; pass archive_source=false to leave it untouched. Returns {source_project_id, target_project_id, moved: {table: count}, source_archived}. Returns {error} if source==target or either project does not exist. Persistent-state disclosure: on hosted Meridian, supplied text and project/session metadata are sent to and stored in Meridian's service; self-hosted deployments keep them in the configured local SQLite/Postgres database. This data is visible in the dashboard/API and later project context or handoffs. Delete individual tasks, notes, or decisions where supported, or delete the project/account using the documented controls. Do not include secrets.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `source_project_id` | string | required | The id of the project to merge FROM (its rows are re-parented; it is archived unless archive_source=false). |
| `target_project_id` | string | required | The id of the project to merge INTO (receives all of the source's rows). |
| `archive_source` | boolean | optional | Default true — soft-archive the emptied source project (status='archived', name prefixed '[merged] '). Set false to leave the source project row untouched. The source is NEVER hard-deleted either way. |

**Example:**
```
merge_project(source_project_id="dup-uuid", target_project_id="keep-uuid")
```

---

## Legacy

### `register_session`
!!! note "Deprecated"
    Use `start_session` instead — it registers the session **and** returns goal + context in one call.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | optional |  |
| `project_name` | string | optional | Project name — an alternative to project_id; resolved to the id internally. project_id wins if both are given. |
| `session_name` | string | required |  |
| `human_id` | string | optional |  |
| `client` | string | optional |  |

**Example:**
```
register_session(project_id="abc-123", session_name="feature-x", human_id="alice")
```
