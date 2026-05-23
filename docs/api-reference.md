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
  "version": "1.9.0",
  "db": "postgres"
}
```

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

### `GET /projects/{project_id}/sprint-items`

List sprint items.

**Query params:** `status` (optional filter)

---

### `POST /projects/{project_id}/sprint-items`

Add a sprint item.

**Body:**
```json
{
  "title": "Add rate limiting",
  "version": "v2.3",
  "group": "Performance",
  "human_id": "alice"
}
```

---

### `POST /projects/{project_id}/sprint-items/{item_id}/complete`

Mark a sprint item done. Body: `{"task_id": "uuid"}` (optional)

### `POST /projects/{project_id}/sprint-items/{item_id}/fail`

Mark failed. Body: `{"reason": "..."}` (optional)

### `POST /projects/{project_id}/sprint-items/{item_id}/skip`

Mark skipped. Body: `{"reason": "..."}` (optional)

### `POST /projects/{project_id}/sprint-items/{item_id}/push`

Push to future version. Body: `{"to_version": "v2.4"}`

### `DELETE /projects/{project_id}/sprint-items/{item_id}`

Delete a sprint item.

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
