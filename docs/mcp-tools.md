# MCP Tool Reference

Meridian exposes **42 tools** over MCP.

They fall into two usage patterns:

- **Planner sessions** (claude.ai, planning work) - `start_session` · `pin_decision` · `update_decision` · `add_note` · `get_context_block` · `generate_handoff`
- **Executor sessions** (Claude Code, Cursor, automated workers) - `start_session` · `log_task` · `request_hitl` · `get_session_brief` · `generate_handoff`

---

## Quick Reference - 5 tools you use 90% of the time

| Tool | One-liner | Example call |
|------|-----------|-------------|
| `start_session` | Register session, get full project context | `start_session(project_id="abc-123", session_name="feature-x", human_id="alice")` |
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
| `project_id` | string | required |  |
| `session_name` | string | required |  |
| `human_id` | string | optional |  |
| `client` | string | optional |  |
| `role` | string | optional | Pass 'executor' to inject executor_config and credentials guidance. |

**Example:**
```
start_session(project_id="abc-123", session_name="feature-x", human_id="alice", role="executor")
```

---


### `get_session_brief`
Compact session orientation (<500 tokens). Returns sprint focus, pending items, recent tasks, blocking failures, and open HITL requests. Ideal for worker/automation sessions that don't need the full context.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | required |  |
| `role` | string | optional | Controls verbosity. 'worker'=sprint+tasks only, 'planner'=full context. |

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
| `project_id` | string | required |  |
| `description` | string | required |  |
| `status` | string | optional |  |

**Example:**
```
log_task(session_id="session-uuid", project_id="abc-123", description="Fixed auth bug", status="done")
```

---


### `get_tasks`
Get recent tasks across all sessions.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | required |  |
| `limit` | integer | optional |  |

**Example:**
```
get_tasks(project_id="abc-123")
```

---


### `search_tasks`
Search tasks by keyword or natural-language query. Uses trigram similarity on Postgres, LIKE on SQLite. Returns top matches with similarity score.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | required |  |
| `query` | string | required |  |
| `limit` | integer | optional |  |

**Example:**
```
search_tasks(project_id="abc-123", query="rate limiting bug")
```

---

## Goal & sprint

### `get_goal`
Read the current goal state.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | required |  |

**Example:**
```
get_goal(project_id="abc-123")
```

---


### `set_goal`
Set or update the goal state.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | required |  |
| `content` | string | required |  |

**Example:**
```
set_goal(project_id="abc-123", content="Build a great product")
```

---

## Executor config & file coordination

### `set_executor_config`
Store project-level executor defaults so worker sessions start with repo path, env file, test command, deploy command, shell, branch, and the injected credentials rule.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | required |  |
| `repo_path` | string | optional |  |
| `env_file` | string | optional |  |
| `test_cmd` | string | optional |  |
| `test_min` | integer | optional |  |
| `deploy_cmd` | string | optional |  |
| `shell_type` | string | optional |  |
| `branch` | string | optional |  |

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
Wait on another session before touching a shared file. The tool polls every 30 seconds until the watched session is done.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `watching_session_id` | string | required |  |

**Example:**
```
idle_until_session_done(watching_session_id="session-uuid")
```

---

## Decisions

### `pin_decision`
Record an authoritative decision that supersedes earlier statements. Pinned decisions appear in every session's context block.

Categories: `STRATEGIC` · `COMPETITIVE` · `TECHNICAL` · `TACTICAL` · `BUSINESS` · `PRODUCT` · `ARCHITECTURAL`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | required |  |
| `title` | string | required |  |
| `body` | string | required |  |
| `category` | string | optional |  |

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
| `status` | string | optional |  |

---


### `get_pinned_decisions`
List pinned decisions (active only by default, newest first).

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | required |  |
| `include_superseded` | boolean | optional |  |

**Example:**
```
get_pinned_decisions(project_id="abc-123")
```

---

## Human-in-the-loop (HITL)

### `request_hitl`
Surface a question to the human queue. `urgency='blocking'` pauses the session until answered — poll `get_hitl_request` to resume. `normal`/`high` land in the dashboard without blocking.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | required |  |
| `question` | string | required |  |
| `session_id` | string | optional |  |
| `context` | string | optional |  |
| `urgency` | string | optional |  |
| `assigned_to` | string | optional |  |

**Example:**
```
request_hitl(project_id="abc-123", question="Should we add rate limiting here?", urgency="normal")
```

---


### `get_hitl_request`
Poll a HITL request for the human's answer. Returns the row including `status` (`pending`/`answered`/`dismissed`) and `answer` text.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `request_id` | string | required |  |

**Example:**
```
get_hitl_request(request_id="hitl-uuid")
```

---

## Handoff & context

### `generate_handoff`
Generate a context handoff document. `mode='full'` writes the complete L0/L1/L2 handoff. `mode='delta'` returns a compact session summary with completed items, pending items, and the next `/goal` string.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | required |  |
| `mode` | string | optional |  |
| `session_id` | string | optional | Optional session id for auto-delta on repeated calls in the same session. |

**Example:**
```
generate_handoff(project_id="abc-123", mode="delta", session_id="session-uuid")
```

---


### `get_context_block`
Return a compact plain-text context block (north star, sprint, pending sprint items, recent tasks, recent decisions, active sessions). Use `mode='full'` to paste into a fresh Claude Code session; `mode='chat'` for a shorter paste into claude.ai.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | required |  |
| `mode` | string | optional |  |

**Example:**
```
get_context_block(project_id="abc-123", mode="chat")
```

---

## Notes

### `add_note`
Add a per-project wiki note. Use for setup instructions, gotchas, environment details, how-tos.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | required |  |
| `title` | string | required |  |
| `body` | string | required |  |
| `tags` | string | optional |  |
| `category` | string | optional |  |

**Example:**
```
add_note(project_id="abc-123", title="Deploy note", body="Reminder: update env vars before deploy", tags="ops,deploy")
```

---


### `get_notes`
List project notes (newest first). Filter by tag substring.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | required |  |
| `tag` | string | optional |  |

**Example:**
```
get_notes(project_id="abc-123")
```

---


### `delete_note`
Hard-delete a project note by id.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `note_id` | string | required |  |

---

## Projects

### `create_project`
Create a new Meridian project.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `name` | string | required |  |

**Example:**
```
create_project(name="my-app")
```

---

## Legacy

### `register_session`
!!! note "Deprecated"
    Use `start_session` instead — it registers the session **and** returns goal + context in one call.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | required |  |
| `session_name` | string | required |  |
| `human_id` | string | optional |  |
| `client` | string | optional |  |

**Example:**
```
register_session(project_id="abc-123", session_name="feature-x", human_id="alice")
```
