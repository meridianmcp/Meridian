# MCP Tool Reference

_Auto-generated. 19 tools._


## `create_project`

Create a new Meridian project.


| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `name` | string | required |  |

**Example:**
```
create_project(name="my-app")
```


## `register_session`

Register this Claude session. Call at session start.


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


## `start_session`

Register session and return goal + recent tasks in one call.


| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | required |  |
| `session_name` | string | required |  |
| `human_id` | string | optional |  |
| `client` | string | optional |  |

**Example:**
```
start_session(project_id="abc-123", session_name="feature-x", human_id="alice")
```


## `get_goal`

Read the current goal state.


| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | required |  |

**Example:**
```
get_goal(project_id="abc-123")
```


## `set_goal`

Set or update the goal state.


| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | required |  |
| `content` | string | required |  |

**Example:**
```
set_goal(project_id="abc-123", content="Build a great product")
```


## `log_task`

Log a task this session completed or is working on. Valid statuses: pending, in_progress, done, failed, backlog, future, backburner.


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


## `get_tasks`

Get recent tasks across all sessions.


| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | required |  |
| `limit` | integer | optional |  |

**Example:**
```
get_tasks(project_id="abc-123")
```


## `search_tasks`

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


## `generate_handoff`

Generate a context handoff file. mode='full' writes the complete L0/L1/L2 handoff; mode='delta' returns a compact session update with completed items, pending items, and the next /goal string.


| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | required |  |
| `mode` | string | optional |  |
| `session_id` | string | optional | Optional session id for auto-delta on repeated calls in the same session. |

**Example:**
```
generate_handoff(project_id="abc-123", mode="delta", session_id="session-uuid")
```


## `get_context_block`

Return a compact plain-text project context block (north star, sprint, pending sprint items, recent tasks, recent decisions, active sessions). mode='full' (default) for Code Handoff into a fresh Claude Code session; mode='chat' for a shorter paste into a new claude.ai conversation.


| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | required |  |
| `mode` | string | optional |  |

**Example:**
```
get_context_block(project_id="abc-123", mode="chat")
```


## `pin_decision`

Create a pinned decision (editable constitution row). Use for the current authoritative truth that supersedes earlier statements. Category: STRATEGIC, COMPETITIVE, TECHNICAL, TACTICAL, BUSINESS, PRODUCT, ARCHITECTURAL.


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


## `update_decision`

Patch a pinned decision. Pass new_title + new_body to atomically supersede (creates a new active row, marks old as superseded with back-link). Otherwise patches body/title/category/status in place.


| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `decision_id` | string | required |  |
| `new_title` | string | optional |  |
| `new_body` | string | optional |  |
| `title` | string | optional |  |
| `body` | string | optional |  |
| `category` | string | optional |  |
| `status` | string | optional |  |


## `get_pinned_decisions`

List pinned decisions (active only by default, newest first).


| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | required |  |
| `include_superseded` | boolean | optional |  |

**Example:**
```
get_pinned_decisions(project_id="abc-123")
```


## `request_hitl`

Surface a question to the human-in-the-loop queue. urgency='blocking' means this session pauses until answered (poll get_hitl_request). urgency='normal'/'high' lands in the dashboard but doesn't block. assigned_to routes to a specific human_id (null = broadcast).


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


## `get_hitl_request`

Poll a HITL request for the human's answer. Returns the row including status ('pending'|'answered'|'dismissed') and answer text.


| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `request_id` | string | required |  |

**Example:**
```
get_hitl_request(request_id="hitl-uuid")
```


## `add_note`

Add a per-project wiki note (setup, gotcha, howto, env, ...). Free-form title/body; comma-separated tags optional.


| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | required |  |
| `title` | string | required |  |
| `body` | string | required |  |
| `tags` | string | optional |  |

**Example:**
```
add_note(project_id="abc-123", title="Deploy note", body="Reminder: update env vars before deploy", tags="ops,deploy")
```


## `get_notes`

List project notes (newest first). Optional ?tag substring filter.


| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | required |  |
| `tag` | string | optional |  |

**Example:**
```
get_notes(project_id="abc-123")
```


## `delete_note`

Hard-delete a project note by id.


| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `note_id` | string | required |  |


## `get_session_brief`

Single-call session orientation — returns sprint focus, pending sprint items, recent tasks, any blocking failures, and pending HITL requests in a compact XML envelope (<500 tokens). Replaces the start_session + get_context_block two-call pattern for worker/automation sessions.


| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | required |  |
| `role` | string | optional | Controls verbosity. 'worker'=sprint+tasks only, 'planner'=full context. |

**Example:**
```
get_session_brief(project_id="abc-123")
```

