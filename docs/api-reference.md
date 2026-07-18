# HTTP API Reference

Meridian exposes a REST API at `http://localhost:7878` (local) or `https://usemeridian.us` (hosted).

Most endpoints are for internal use by the dashboard and MCP layer. The MCP tools are the recommended interface for AI sessions. This reference is for developers building integrations.

---

## Health

### `GET /health`

Check server health.

**Auth:** None required

**Response:**
```json
{
  "status": "ok",
  "service": "meridian",
  "version": "0.1.9",
  "git_sha": "a555660ef312"
}
```

> `version` is read from `pyproject.toml` at startup (or the `MERIDIAN_VERSION` env var). `git_sha` is the 12-char HEAD SHA of the running build — useful for deploy-drift checks.

---

## Projects

### `GET /projects`

List all projects.

**Auth:** None (local) / Session cookie or Bearer token (hosted)

**Response:** `[{id, name, created_at, ...}]`

---

### `POST /projects`

Create a project.

**Body:**
```json
{"name": "backend-api"}
```

**Response (201):**
```json
{"id": "uuid", "name": "backend-api", "created_at": "2026-05-23T00:00:00Z"}
```

---

### `GET /projects/{project_id}`

Get a single project.

**Response (404):** `{"detail": "Project not found"}`

---

### `POST /projects/{project_id}/rename`

Rename a project.

**Body:** `{"name": "new-name"}`

---

### `DELETE /projects/{project_id}`

Delete a project and all its data.

**Response:** 204 No Content

---

## Goals

### `GET /projects/{project_id}/goal`

Get the current goal state.

**Response:**
```json
{
  "id": "uuid",
  "project_id": "uuid",
  "content": "SHIPPED:\n...",
  "north_star": "Long-term vision...",
  "sprint": "v2.3 — this week",
  "version": 42,
  "created_at": "...",
  "updated_at": "..."
}
```

---

### `POST /projects/{project_id}/goal`

Set the goal content.

**Body:**
```json
{
  "content": "SHIPPED:\n...\nCURRENT FOCUS:\n...",
  "north_star": "optional — update north star",
  "sprint": "optional — update sprint",
  "minor": false
}
```

`minor: true` updates in place without bumping the version number (used for AUTO BLOCKS).

---

### `POST /projects/{project_id}/goal/north-star`

Update only the north star.

**Body:** `{"north_star": "...", "human_id": "alice"}`

---

### `POST /projects/{project_id}/goal/sprint`

Update only the sprint.

**Body:** `{"sprint": "v2.3 — rate limiting + tests"}`

---

### `GET /projects/{project_id}/goal-history`

Get all goal versions for a project. Newest first. Strips AUTO BLOCKS for clean diffs.

**Response:** `[{version, content, north_star, sprint, created_at}]`

---

## Sessions

### `GET /projects/{project_id}/sessions`

List active sessions for a project.

**Response:**
```json
[{
  "id": "uuid",
  "project_id": "uuid",
  "name": "feature-auth-fix",
  "status": "active",
  "human_id": "alice",
  "last_seen": "2026-05-23T10:00:00Z",
  "created_at": "..."
}]
```

---

### `POST /sessions/register`

Register a new session.

**Body:**
```json
{
  "project_id": "uuid",
  "name": "my-session",
  "human_id": "alice"
}
```

---

### `POST /sessions/{session_id}/heartbeat`

Update a session's `last_seen` timestamp.

**Response:** `{"ok": true}`

---

### `POST /sessions/{session_id}/close`

Close a session.

---

## Tasks

### `GET /projects/{project_id}/tasks`

Get recent tasks. Newest first.

**Query params:** `limit` (default: 50)

**Response:**
```json
[{
  "id": "uuid",
  "session_id": "uuid",
  "project_id": "uuid",
  "description": "Fixed auth bug",
  "status": "done",
  "created_at": "..."
}]
```

---

### `POST /tasks`

Log a task.

**Body:**
```json
{
  "session_id": "uuid",
  "project_id": "uuid",
  "description": "Implemented rate limiting",
  "status": "done"
}
```

---

### `PATCH /tasks/{task_id}`

Update a task (status, description).

**Body:** `{"status": "done", "description": "updated description"}`

---

### `GET /projects/{project_id}/tasks/claimable`

List unclaimed pending tasks.

---

### `POST /projects/{project_id}/tasks/release`

Release a claimed task.

**Body:** `{"task_id": "uuid", "session_id": "uuid"}`

---

## Sprint Items

Sprint items are the machine-trackable checklist alongside the free-text sprint field. Each item has a lifecycle: `todo` → `in_progress` → `done` (or `failed` / `skipped` / `pushed`).

### Sprint item fields

| Field | Type | Description |
|-------|------|-------------|
| `id` | UUID | Primary key |
| `project_id` | UUID | Parent project |
| `version` | TEXT | Sprint bucket identifier (e.g. `"v2.3"`) |
| `title` | TEXT | Item description |
| `status` | TEXT | `todo`, `pending`, `in_progress`, `done`, `failed`, `skipped`, `pushed`, `indeterminate`, `provisional_complete` |
| `item_group` | TEXT | Optional named group / swimlane for dashboard rendering |
| `human_id` | TEXT | Person who added the item |
| `notes` | TEXT | Free-text notes; updated via PATCH or on completion |
| `depends_on` | UUID | ID of a parent sprint item that must be `done` before this item is claimable |
| `failure_mode` | TEXT | What to do when the `depends_on` parent fails: `"continue"` (default) or `"stop"` |
| `milestone_type` | TEXT | `"task"` (default), `"milestone"` (timeline marker), or `"human"` (human-executed) |
| `track` | TEXT | Named lane (e.g. `"paper"`) — a whole track can be deferred or skipped by executors |
| `wave` | TEXT | Stored parallel-execution wave label (e.g. `"wave-1"`), auto-filled by `assign_sprint_waves` |
| `priority` | TEXT | `urgent`, `high`, `normal` (default), or `low` — higher-priority pending items are surfaced first |
| `blocker_kind` | TEXT | `null` (ordinary), `"manual"` (blocked on a real-world action outside Meridian), or `"superseded"` (hard-blocked, not re-claimable) |
| `deferred_until` | TEXT (ISO timestamp) | Enforced deferral — `claim_sprint_item` refuses the item while this is in the future |
| `touches_resources` | JSON TEXT | Typed resource identifiers for conflict detection (e.g. `["file:meridian/server.py", "db:migrations"]`) |
| `parent_id` | UUID | ID of the parent sprint item when this item is a subtask (created via `add_subtask`) |
| `owner` | TEXT | `"human"` or `"ai"` — for mixed-ownership task chains (set by `add_subtask`) |
| `required_notes` | INTEGER (0/1) | When 1, `complete_sprint_item` refuses without evidence in `notes` or a linked `task_id` |
| `slug` | TEXT | Human-readable per-project identifier derived from the title |
| `nickname` | TEXT | Short (1-2 word) memorable per-project handle |
| `sprint_name` | TEXT | Human-readable sprint bucket label (e.g. `"docs-cloudflare"`), separate from `version` |
| `prospect_bypass` | INTEGER (0/1) | When 1, `claim_sprint_item` skips the prospect gate |
| `stall_count` | INTEGER | Times this item was re-queued after a worker closed without completing it |
| `pushed_to` | TEXT | Target version when status is `"pushed"` |
| `task_id` | UUID | Linked task log entry |
| `claimed_at` | TEXT | When the item entered `in_progress` |
| `completed_at` | TEXT | When the item reached a terminal status |
| `added_at` | TEXT | Creation timestamp |

---

### `GET /projects/{project_id}/sprint-items`

List sprint items.

**Query params:** `status` (optional filter)

---

### `POST /projects/{project_id}/sprint-items`

Add a sprint item. The REST endpoint accepts a subset of fields; use the MCP `add_sprint_item` tool for the full parameter set (track, wave, priority, blocker_kind, deferred_until, etc.).

**Body:**
```json
{
  "title": "Add rate limiting",
  "version": "v2.3",
  "group": "Performance",
  "human_id": "alice",
  "depends_on": "uuid-of-parent-item",
  "failure_mode": "continue",
  "touches_resources": ["file:meridian/server.py"],
  "force": false
}
```

**Response (409 Conflict):** returned when a near-duplicate title already exists as a pending/in-progress item. Pass `"force": true` to override.

---

### `PATCH /projects/{project_id}/sprint-items/{item_id}`

Update editable fields of a sprint item.

**Body:** `{"title": "...", "notes": "...", "group": "...", "human_id": "...", "touches_resources": [...], "status": "pending"}` (all optional)

> `status` via PATCH is restricted to non-terminal resets: `pending`, `todo`, `indeterminate`. Use the dedicated transition endpoints (complete / fail / skip / push) for terminal statuses.

---

### `POST /projects/{project_id}/sprint-items/{item_id}/complete`

Mark a sprint item done.

**Body:** `{"task_id": "uuid", "notes": "evidence of completion"}` (both optional; `notes` is required when the item has `required_notes=1`)

### `POST /projects/{project_id}/sprint-items/{item_id}/fail`

Mark failed. Body: `{"reason": "..."}` (optional)

### `POST /projects/{project_id}/sprint-items/{item_id}/skip`

Mark skipped. Body: `{"reason": "..."}` (optional)

### `POST /projects/{project_id}/sprint-items/{item_id}/push`

Push to future version. Body: `{"to_version": "v2.4"}`

### `DELETE /projects/{project_id}/sprint-items/{item_id}`

Delete a sprint item.

---

## Human-in-the-Loop (HITL)

HITL requests let an AI session pause and ask a human a question. The session POSTs a request, then polls `GET /hitl/{id}` until `status` becomes `"answered"`.

### `GET /hitl`

List all pending HITL requests across all projects.

**Query params:** `status` (`pending` | `answered` | `dismissed` | `all`, default `pending`), `limit` (default 50)

---

### `GET /projects/{project_id}/hitl`

List HITL requests scoped to a single project.

**Query params:** `status`, `limit` (same as above)

---

### `POST /projects/{project_id}/hitl`

Create a HITL request.

**Auth:** None (local) / Session cookie or Bearer token (hosted)

**Body:**
```json
{
  "question": "Should I merge the auth branch now or wait for the review?",
  "context": "Optional background context for the human",
  "urgency": "normal",
  "session_id": "uuid",
  "assigned_to": "alice"
}
```

`urgency` values: `"normal"` (default), `"high"`, `"blocking"`

**Response (201):**
```json
{
  "id": "uuid",
  "project_id": "uuid",
  "session_id": "uuid",
  "question": "Should I merge the auth branch now or wait for the review?",
  "context": null,
  "urgency": "normal",
  "status": "pending",
  "answer": null,
  "answered_by": null,
  "assigned_to": "alice",
  "created_at": "...",
  "answered_at": null
}
```

> When the project has `hitl_auto_answer` enabled, the response comes back immediately with `answered_by: "auto"` and no notification is sent.

---

### `GET /hitl/{request_id}`

Get a single HITL request. Sessions poll this endpoint to read the human's answer.

**Response (404):** `{"detail": "hitl request not found"}`

---

### `PATCH /hitl/{request_id}`

Answer or dismiss a HITL request.

**Body (answer):**
```json
{"action": "answer", "answer": "Wait for the review.", "answered_by": "alice"}
```

**Body (dismiss):**
```json
{"action": "dismiss"}
```

`action` defaults to `"answer"` when omitted.

---

## Workspace Proposals

Workspace proposals are a human-only "drawer of inspiration" for cross-project ideas. They are **not** executor-claimable — a human must review and promote them.

> Proposals are managed via MCP tools only (`add_workspace_proposal`, `get_workspace_proposals`, `advance_proposal_status`, `promote_proposal`). There is no REST endpoint for proposals.

**Status lifecycle:** `raw` → `investigating` → `promoted` | `rejected`

- Use `advance_proposal_status` to move through `raw → investigating → rejected` (or back).
- Use `promote_proposal` to convert a proposal into a real sprint item (sets status to `"promoted"` and creates a linked sprint item).

---

## Handoff

### `POST /projects/{project_id}/handoff`

Generate a context handoff file.

**Response:**
```json
{
  "file_path": "data/my-project_handoff.md",
  "content": "---\nMERIDIAN_CONTEXT\n..."
}
```

---

## Auth (Hosted Tier)

### `GET /auth/login`

Serves the sign-in page with Google and GitHub OAuth buttons.

### `GET /auth/google/login`

Redirect to Google OAuth consent page.

### `GET /auth/callback`

Google OAuth callback — creates/updates tenant, sets session cookie.

**Query params:** `code` (from Google)

### `GET /auth/github/login`

Redirect to GitHub OAuth consent page.

### `GET /auth/github/callback`

GitHub OAuth callback — creates/updates tenant, sets session cookie.

**Query params:** `code` (from GitHub)

### `GET /auth/logout`

Clear session cookie, redirect to `/`.

---

## Remote MCP (Hosted Tier)

### `POST /mcp`

Remote MCP endpoint — HTTP transport.

**Auth:** `Authorization: Bearer sk_meridian_...`

**Rate limit:** 100 requests/minute per token

**Body:** Standard MCP JSON-RPC request

**Response:** Standard MCP JSON-RPC response

---

## Admin

### `GET /admin`

Admin dashboard. Shows active customers, churned, signups/day, Neon project count, recent signups, payment failures.

**Auth:** ADMIN_EMAIL Google OAuth only

### `GET /admin/git-status`

Check if the local repo is behind the remote.

**Response:**
```json
{
  "behind": 0,
  "ahead": 0,
  "branch": "main",
  "error": null
}
```

---

## Demo

### `GET /demo`

Public read-only demo dashboard. Sets a demo context cookie. Exempt from SITE_PASSWORD gate.

No auth required.

---

## Static Pages

| Route | Description |
|-------|-------------|
| `GET /` | Landing page with pricing |
| `GET /terms` | Terms of Service |
| `GET /privacy` | Privacy Policy |
| `GET /health` | Health check |
| `GET /dashboard` | Dashboard (auth required on hosted) |
| `GET /config` | Server config (version, db type, etc.) |
