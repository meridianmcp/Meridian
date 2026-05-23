# MCP Tools Reference

All Meridian tools are available as MCP tools in Claude Code, Claude Desktop, Cursor, and Windsurf.
Add Meridian to your MCP config and these tools appear automatically.

---

## Session Management

### `start_session`

Single call to start a coordinated session. Registers you, reads goal + ambient context, shows recent work, lists active sessions, and tells you where the handoff file is.

**Use this INSTEAD of `register_session` + `get_goal` + `get_tasks` separately.**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | ✓ | Project UUID to connect to |
| `session_name` | string | ✓ | Descriptive name for this session (e.g. `"v2.1-auth-fix"`) |
| `human_id` | string | — | Your identifier (e.g. `"alice"`). Groups sessions per person in the dashboard. |

**Returns:** `session_id`, `goal` (with `ambient_tasks`), `recent_tasks` (last 10), `active_sessions`, `handoff_exists`, `handoff_path`, `files`

**Example:**
```json
{
  "project_id": "5787cc92-ba7d-4788-b17c-28ab7938b839",
  "session_name": "feature-rate-limiting",
  "human_id": "alice"
}
```

---

### `register_session`

Register this Claude session with a project. Use when you need the `session_id` separately, or prefer the low-level flow. Normally, use `start_session` instead.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | ✓ | Project UUID |
| `session_name` | string | ✓ | Descriptive name for this session |
| `human_id` | string | — | Your identifier |

**Returns:** Full session dict including `session_id`, `status`, `created_at`, `last_seen`

---

### `heartbeat`

Keep a long-running session alive. Long-running workers should call this every ~5 minutes between `log_task` calls to prevent idle expiry.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | ✓ | Project UUID |
| `session_id` | string | ✓ | Your session ID |

**Returns:** `{"ok": true}`

---

### `get_sessions`

List all active sessions connected to this project — name, status, last_seen, and human_id.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | ✓ | Project UUID |

**Returns:** List of session dicts

---

## Task Management

### `log_task`

Log what this session just did, is doing, or failed at. Call frequently to keep all sessions informed.

| Parameter | Type | Required | Description | Example |
|-----------|------|----------|-------------|---------|
| `session_id` | string | ✓ | Your session ID from `start_session` | `"abc-123"` |
| `project_id` | string | ✓ | Project UUID | `"5787cc92..."` |
| `description` | string | ✓ | What you did / are doing | `"Fixed JWT refresh bug — tokens now rotated on use"` |
| `status` | string | — | `"done"` \| `"pending"` \| `"failed"` | `"done"` |

**Returns:** Task dict with `id`, `description`, `status`, `created_at`

---

### `get_tasks`

Get recent tasks across all sessions — what everyone has done. Newest first.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | ✓ | Project UUID |
| `limit` | integer | — | Max tasks to return (default: 20) |

**Returns:** List of task dicts with `session_id`, `description`, `status`, `created_at`

---

### `claim_task`

Atomically claim a pending task so no other session picks it up. Returns `claimed=True` on success or `claimed=False` (with the current holder) when another session already holds it.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | ✓ | Project UUID |
| `task_id` | string | ✓ | ID of the pending task to claim |
| `session_id` | string | ✓ | Your session ID |

**Returns:** `{"claimed": true, "task": {...}}` or `{"claimed": false, "held_by": "session-name"}`

---

### `release_task`

Release a task previously claimed by this session, making it available for others.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | ✓ | Project UUID |
| `task_id` | string | ✓ | ID of the claimed task |
| `session_id` | string | ✓ | Your session ID |

**Returns:** `{"success": true}` or `{"success": false}` (if you don't hold the claim)

---

### `complete_task`

Mark a claimed task as done with an optional completion note.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | ✓ | Project UUID |
| `task_id` | string | ✓ | ID of the task |
| `session_id` | string | ✓ | Your session ID |
| `note` | string | — | Completion note appended to the description |

**Returns:** Updated task dict

---

## Goal Management

### `get_goal`

Read the current goal state plus ambient context. Returns all three goal levels (north_star, version goal, sprint) plus the last 5 task descriptions.

**Read this after `start_session` if you need to refresh the goal.**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | ✓ | Project UUID |

**Returns:**
```json
{
  "north_star": "Long-term vision...",
  "content": "CURRENT FOCUS:\n...",
  "sprint": "v2.3 — rate limiting + tests",
  "version": 42,
  "ambient_tasks": ["Fixed auth bug", "Added caching", ...]
}
```

---

### `set_goal`

Set or update the version goal (content). All sessions see the change immediately. Version increments on each update.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | ✓ | Project UUID |
| `content` | string \| object | ✓ | The new version goal content |
| `north_star` | string | — | Optionally update the north star at the same time |
| `sprint` | string | — | Optionally update the sprint at the same time |
| `minor` | boolean | — | `true` = update in place without bumping version (for AUTO BLOCKS) |

**Returns:** Updated goal state dict

---

### `set_north_star`

Update only the north star — the long-lived product vision. Owner-only: pass the `human_id` used when creating the project.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | ✓ | Project UUID |
| `north_star` | string | ✓ | New north star text |
| `human_id` | string | ✓ | Owner identifier (must match project creator) |

**Returns:** Updated goal state dict

---

### `set_sprint`

Update only the sprint — the short-term focus. Any team member can call this.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | ✓ | Project UUID |
| `sprint` | string | ✓ | New sprint description (e.g. `"v2.3 week of June 2 — auth + caching"`) |

**Returns:** Updated goal state dict

---

### `set_decision`

Append a decision entry to the project's append-only decisions log. Each entry is date-stamped automatically.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | ✓ | Project UUID |
| `text` | string | ✓ | Decision text (e.g. `"Chose Redis over Memcached — better pub/sub for cache invalidation"`) |

**Returns:** Updated goal state dict

---

## Sprint Items

### `add_sprint_item`

Append a todo item to the project's sprint board. Use when starting a new version so the next session sees what's in flight.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | ✓ | Project UUID |
| `version` | string | ✓ | Version this item belongs to (e.g. `"v2.2"`) |
| `title` | string | ✓ | What needs to get done |
| `group` | string | — | Objective group name (e.g. `"Auth"`) |
| `human_id` | string | — | Person this is assigned to |

**Returns:** New sprint item dict with `id`, `title`, `version`, `status: "pending"`

---

### `complete_sprint_item`

Mark a sprint item done.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | ✓ | Project UUID |
| `item_id` | string | ✓ | Sprint item UUID |
| `task_id` | string | — | Link the task that shipped it |

**Returns:** Updated item dict

---

### `fail_sprint_item`

Mark a sprint item failed — attempted but could not be shipped.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | ✓ | Project UUID |
| `item_id` | string | ✓ | Sprint item UUID |
| `reason` | string | — | One-line explanation of what went wrong |

**Returns:** Updated item dict

---

### `skip_sprint_item`

Mark a sprint item skipped — intentionally not shipped this sprint.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | ✓ | Project UUID |
| `item_id` | string | ✓ | Sprint item UUID |
| `reason` | string | — | Why it was skipped |

**Returns:** Updated item dict

---

### `push_sprint_item`

Push a sprint item to a future version when scope creep means it won't fit this sprint.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | ✓ | Project UUID |
| `item_id` | string | ✓ | Sprint item UUID |
| `to_version` | string | ✓ | Target version (e.g. `"v2.3"`) |

**Returns:** Updated item dict with `status: "pushed"`

---

### `get_sprint_items`

List sprint items for a project. Cold sessions read this to know what's still owed.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | ✓ | Project UUID |
| `status` | string | — | Filter: `todo` \| `pending` \| `in_progress` \| `done` \| `failed` \| `skipped` \| `pushed` |

**Returns:** List of sprint item dicts

---

## Handoff

### `generate_handoff`

Generate a context handoff file. Call when context is filling up or before ending a session. A new session reads this file to resume with full context.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | ✓ | Project UUID |

**Returns:**
```json
{
  "file_path": "data/my-project_handoff.md",
  "content": "---\nMERIDIAN_CONTEXT\n..."
}
```

**Usage pattern:**
```
# When context is getting full:
generate_handoff(project_id="...")
# → "data/my-project_handoff.md"

# Start a new Claude Code session, then:
# "Read data/my-project_handoff.md and continue from where I left off."
```

---

## Project Management

### `create_project`

Create a new Meridian project. Project names must be unique.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `name` | string | ✓ | Project name (e.g. `"backend-api"`) |

**Returns:** `{"id": "uuid", "name": "backend-api", "created_at": "..."}`

---

### `list_projects`

List all Meridian projects. Use this to find a `project_id` by name.

No parameters required.

**Returns:** `[{id, name, created_at}]` newest first

---

## Worker Sessions

### `start_worker_session`

Register a worker session and claim its task in one call. Returns a slim `worker_context` XML block (< 500 tokens) with only what the worker needs. Use for Claude Code subprocess workers spawned by `enqueue_claude_task`.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | ✓ | Project UUID |
| `task_id` | string | — | Specific task to claim. If omitted, claims the oldest unclaimed pending task. |

**Returns:** Slim worker context XML: `version_goal`, claimed task, `repo`, `test_cmd`, `commit_pattern`, `done_when`

---

### `enqueue_claude_task`

Queue a long-running Claude Code subprocess without blocking this session. Returns immediately with a `pending` task row; the worker writes its result back when it finishes.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `session_id` | string | ✓ | Your session ID |
| `project_id` | string | ✓ | Project UUID |
| `prompt` | string | ✓ | The prompt/task description for the worker |
| `timeout` | number | — | Seconds before the worker is killed (default: 600). Pass 0 to disable. |

**Returns:** Task dict with `status: "pending"` — poll `get_tasks` to see when it becomes `done` or `failed`

---

## Typical Session Pattern

```python
# 1. Start session — loads everything in one call
ctx = start_session(
    project_id="...",
    session_name="auth-refactor",
    human_id="alice"
)
session_id = ctx["session_id"]

# 2. Check what's pending
items = get_sprint_items(project_id="...", status="pending")

# 3. Claim a task so nobody else picks it up
claim_task(project_id="...", task_id=items[0]["id"], session_id=session_id)

# 4. Work... log progress
log_task(session_id=session_id, project_id="...",
  description="Extracted auth module — 847 lines → 3 focused classes",
  status="done")

# 5. Complete the sprint item
complete_sprint_item(project_id="...", item_id=items[0]["id"])

# 6. When context fills up, generate handoff
generate_handoff(project_id="...")
```
