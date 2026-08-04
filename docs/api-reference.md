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
  "version": "0.2.6",
  "git_sha": "a555660ef312"
}
```

> `version` is read from `pyproject.toml` at startup (or the `MERIDIAN_VERSION` env var). `git_sha` is the 12-char HEAD SHA of the running build -- useful for deploy-drift checks.

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

## Route Inventory

> **Auto-generated** from the live FastAPI route table by `scripts/gen_docs.py`. Edit `meridian/server.py` (not this file) to add or remove routes. CI fails when committed docs drift from generator output.


### `/`

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Landing page — headline, CTAs, waitlist form |


### `/.well-known`

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/.well-known/agent.json` | Return the A2A agent card so other agents can discover this server |
| `POST` | `/.well-known/agent.json` | Support POST discovery per A2A spec |
| `GET` | `/.well-known/oauth-authorization-server` |  |
| `GET` | `/.well-known/oauth-protected-resource` |  |
| `GET` | `/.well-known/openid-configuration` | OIDC discovery alias for clients that probe this path before OAuth AS metadata |


### `/__gate__`

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/__gate__` |  |


### `/a2a`

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/a2a/{agent_id}/tasks/send` | Receive an A2A task, store it as 'submitted', and return the task envelope |
| `GET` | `/a2a/{agent_id}/tasks/{task_id}` | Return the current status of an A2A task |


### `/account`

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/account/delete` | Self-service account deletion. Requires JSON body: {"confirmation": "DELETE"} |
| `GET` | `/account/sessions` | 3c28450d — list the account's active web sessions (device + recency) |
| `POST` | `/account/sessions/{session_id}/revoke` | 3c28450d — revoke (sign out) one of the account's web sessions |


### `/activate`

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/activate` | Device activation page — shows approval UI for a pending device_code |
| `POST` | `/activate` | Handle device approval or denial |


### `/admin`

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/admin` | Admin dashboard — restricted to MERIDIAN_ADMIN_EMAILS + optional password |
| `GET` | `/admin/__error_test` | Force an error response to exercise the 5xx counter + admin alerting |
| `POST` | `/admin/blog/generate-draft` | Build a draft from recent shipped activity (no AI call). Returns |
| `GET` | `/admin/blog/posts` |  |
| `POST` | `/admin/blog/posts` |  |
| `DELETE` | `/admin/blog/posts/{post_id}` |  |
| `GET` | `/admin/blog/posts/{post_id}` |  |
| `POST` | `/admin/blog/posts/{post_id}/publish` |  |
| `POST` | `/admin/blog/posts/{post_id}/unpublish` |  |
| `GET` | `/admin/git-status` | Check if local repo is behind/ahead of remote |
| `GET` | `/admin/health` | JSON health check for ops/curl — restricted to admin users |
| `GET` | `/admin/login` | Admin password gate for the /admin panel |
| `POST` | `/admin/login` | Validate admin password and set signed cookie |
| `POST` | `/admin/restart` | Restart the server by spawning a new process then shutting down |
| `POST` | `/admin/shutdown` | Gracefully stop the server process |
| `GET` | `/admin/snapshot` | Download the current DB as a SQLite snapshot file |
| `GET` | `/admin/stats` | d1cb1100 — launch/user stats for the admin Users widget: free-tier count |
| `GET` | `/admin/waitlist` | Admin waitlist management page — shows signups, tenant stats, approve/delete buttons |
| `DELETE` | `/admin/waitlist/{entry_id}` | Delete a waitlist entry by id. Admin only |


### `/api`

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/admin/changelog-entries` | Admin-only: create a new changelog entry |
| `DELETE` | `/api/admin/changelog-entries/{entry_id}` | Admin-only: delete a changelog entry |
| `PATCH` | `/api/admin/changelog-entries/{entry_id}` | Admin-only: update a changelog entry |
| `GET` | `/api/changelog-entries` | Return changelog entries as JSON |
| `DELETE` | `/api/keys/orphaned` | Purge this tenant's orphaned OAuth API keys (``label='oauth'``, >24h old) |


### `/api-reference-doc`

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api-reference-doc` | Generate docs/api-reference.md from the live FastAPI route table |


### `/auth`

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/auth/callback` | Handle Google OAuth callback — create/update tenant, set session cookie |
| `GET` | `/auth/email-required` | Shown when OAuth provider returned no usable email (e.g. GitHub with private email) |
| `GET` | `/auth/github/callback` | Handle GitHub OAuth callback — create/update tenant, set session cookie |
| `GET` | `/auth/github/login` | Redirect browser to GitHub OAuth consent page |
| `GET` | `/auth/github/repo-callback` | Handle GitHub repo-connect callback and store repo access |
| `GET` | `/auth/github/repo-connect` | Redirect browser to GitHub OAuth for repo connection |
| `GET` | `/auth/google/login` | Redirect browser directly to Google OAuth consent page |
| `GET` | `/auth/hooks-connect` | Browser endpoint: register THIS machine to the logged-in tenant and show |
| `GET` | `/auth/hooks-status` | Return {registered, token} for the logged-in tenant's hostname. The token |
| `GET` | `/auth/install` | One-time install token page — requires browser session, returns a short-lived token |
| `GET` | `/auth/login` | Serve sign-in page with Google and GitHub OAuth buttons |
| `GET` | `/auth/logout` | Clear session cookie and delete DB session |
| `POST` | `/auth/magic` | v0.9 — request a magic-link email. Rate-limited |
| `GET` | `/auth/magic/verify` | v0.9 — consume a magic-link token, create a session, redirect |
| `GET` | `/auth/me` | Return the authenticated tenant's profile (session cookie or bearer), including projects |
| `GET` | `/auth/microsoft/callback` | Handle Microsoft OAuth callback — create/update tenant, set session cookie |
| `GET` | `/auth/microsoft/login` | Redirect browser to Microsoft OAuth consent page |
| `GET` | `/auth/tokens` | List API bearer tokens for the authenticated tenant |
| `POST` | `/auth/tokens` | Generate a new API bearer token for the authenticated tenant |
| `DELETE` | `/auth/tokens/{token_id}` | Revoke an API bearer token for the authenticated tenant |
| `GET` | `/auth/tunnel-connect` | Device-code page for `meridian --tunnel` browser auth |
| `POST` | `/auth/tunnel-connect` | Complete the device-code flow — create a tunnel token and register it for polling |
| `GET` | `/auth/tunnel-poll` | Poll endpoint for the tunnel device-code flow |


### `/billing`

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/billing/portal` | G2.11 — open a Stripe Customer Portal session for the signed-in tenant |
| `POST` | `/billing/portal` | Return Stripe billing portal URL as JSON for dashboard AJAX calls (e7d4400b) |


### `/blog`

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/blog` |  |
| `GET` | `/blog/{slug}` |  |


### `/changelog`

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/changelog` | Public changelog rendered from DB — newest entries first |


### `/checkout`

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/checkout` | Create a Stripe Checkout Session and redirect to it |


### `/code`

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/code/mcp/{tenant_id}` | Proxy requests to the tenant's codebase-memory-mcp over the code tunnel |
| `OPTIONS` | `/code/mcp/{tenant_id}` | Proxy requests to the tenant's codebase-memory-mcp over the code tunnel |
| `POST` | `/code/mcp/{tenant_id}` | Proxy requests to the tenant's codebase-memory-mcp over the code tunnel |
| `GET` | `/code/mcp/{tenant_id}/{rest:path}` | Same as code_mcp_proxy but for sub-paths |
| `OPTIONS` | `/code/mcp/{tenant_id}/{rest:path}` | Same as code_mcp_proxy but for sub-paths |
| `POST` | `/code/mcp/{tenant_id}/{rest:path}` | Same as code_mcp_proxy but for sub-paths |


### `/config`

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/config` | v0.6.5 — expose runtime configuration to the dashboard |
| `GET` | `/config/api-key` | Tell the dashboard which auth method is active |
| `POST` | `/config/connections` | v1.9.x — save a new connection profile to meridian.toml |
| `DELETE` | `/config/connections/{name}` | v1.9.x — remove a named connection profile from meridian.toml |


### `/dashboard`

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/dashboard` | Serve the Meridian dashboard from a Jinja2 template |


### `/dc`

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/dc/mcp/{tenant_id}` | Proxy requests to the tenant's desktop-commander over the dc tunnel |
| `OPTIONS` | `/dc/mcp/{tenant_id}` | Proxy requests to the tenant's desktop-commander over the dc tunnel |
| `POST` | `/dc/mcp/{tenant_id}` | Proxy requests to the tenant's desktop-commander over the dc tunnel |
| `GET` | `/dc/mcp/{tenant_id}/{rest:path}` | Same as dc_mcp_proxy but for sub-paths |
| `OPTIONS` | `/dc/mcp/{tenant_id}/{rest:path}` | Same as dc_mcp_proxy but for sub-paths |
| `POST` | `/dc/mcp/{tenant_id}/{rest:path}` | Same as dc_mcp_proxy but for sub-paths |


### `/debug`

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/debug/mcp/{tenant_id}` | 121e6a27 — Proxy requests to the tenant's mcp-debugger server over the debug tunnel |
| `OPTIONS` | `/debug/mcp/{tenant_id}` | 121e6a27 — Proxy requests to the tenant's mcp-debugger server over the debug tunnel |
| `POST` | `/debug/mcp/{tenant_id}` | 121e6a27 — Proxy requests to the tenant's mcp-debugger server over the debug tunnel |
| `GET` | `/debug/mcp/{tenant_id}/{rest:path}` | Same as debug_mcp_proxy but for sub-paths |
| `OPTIONS` | `/debug/mcp/{tenant_id}/{rest:path}` | Same as debug_mcp_proxy but for sub-paths |
| `POST` | `/debug/mcp/{tenant_id}/{rest:path}` | Same as debug_mcp_proxy but for sub-paths |


### `/demo`

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/demo` | Public read-only demo dashboard backed by MERIDIAN_DEMO_DB_URL |


### `/demo-auth`

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/demo-auth` | Validate demo password and set a signed access cookie |


### `/docs`

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/docs/mcp/{tenant_id}` | Proxy requests to the tenant's meridian-docs server over the docs tunnel |
| `OPTIONS` | `/docs/mcp/{tenant_id}` | Proxy requests to the tenant's meridian-docs server over the docs tunnel |
| `POST` | `/docs/mcp/{tenant_id}` | Proxy requests to the tenant's meridian-docs server over the docs tunnel |
| `GET` | `/docs/mcp/{tenant_id}/{rest:path}` | Same as docs_mcp_proxy but for sub-paths |
| `OPTIONS` | `/docs/mcp/{tenant_id}/{rest:path}` | Same as docs_mcp_proxy but for sub-paths |
| `POST` | `/docs/mcp/{tenant_id}/{rest:path}` | Same as docs_mcp_proxy but for sub-paths |


### `/document-peeks`

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/document-peeks` | 79ee73e8 — recent 'viewed but not saved' get_document_structure peeks |


### `/export`

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/export/my-data` | GDPR data portability — returns a JSON file of all account data |


### `/extract`

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/extract/mcp/{tenant_id}` | Proxy requests to the tenant's mcp-server-code-extractor over the extract tunnel |
| `OPTIONS` | `/extract/mcp/{tenant_id}` | Proxy requests to the tenant's mcp-server-code-extractor over the extract tunnel |
| `POST` | `/extract/mcp/{tenant_id}` | Proxy requests to the tenant's mcp-server-code-extractor over the extract tunnel |
| `GET` | `/extract/mcp/{tenant_id}/{rest:path}` | Same as extract_mcp_proxy but for sub-paths |
| `OPTIONS` | `/extract/mcp/{tenant_id}/{rest:path}` | Same as extract_mcp_proxy but for sub-paths |
| `POST` | `/extract/mcp/{tenant_id}/{rest:path}` | Same as extract_mcp_proxy but for sub-paths |


### `/failover-status`

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/failover-status` | ITEM 7 — report whether this instance is serving in failover mode |


### `/feedback`

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/feedback` | Submit user feedback. Requires JSON body: {"type": "...", "message": "...", "email": "..."} |


### `/fs`

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/fs/mcp/{tenant_id}` | Proxy a GET/POST/OPTIONS request through the tenant's active tunnel socket |
| `OPTIONS` | `/fs/mcp/{tenant_id}` | Proxy a GET/POST/OPTIONS request through the tenant's active tunnel socket |
| `POST` | `/fs/mcp/{tenant_id}` | Proxy a GET/POST/OPTIONS request through the tenant's active tunnel socket |
| `GET` | `/fs/mcp/{tenant_id}/{rest:path}` | Same as fs_mcp_proxy but for sub-paths under the MCP root |
| `OPTIONS` | `/fs/mcp/{tenant_id}/{rest:path}` | Same as fs_mcp_proxy but for sub-paths under the MCP root |
| `POST` | `/fs/mcp/{tenant_id}/{rest:path}` | Same as fs_mcp_proxy but for sub-paths under the MCP root |


### `/github`

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/github/connections` | List all connected GitHub accounts for the current tenant |
| `DELETE` | `/github/connections/{account_login}` | Remove a connected GitHub account |


### `/health`

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Liveness probe. 056b712f — also report the running build's git SHA + version so an |
| `GET` | `/health/deep` | 4c559d4e — deep health probe for post-deploy checks + external monitoring |


### `/hitl`

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/hitl` | Pending HITL requests across all projects (top-level dashboard panel) |
| `GET` | `/hitl/{request_id}` | Single HITL request lookup — sessions poll this to get the answer |
| `PATCH` | `/hitl/{request_id}` | Answer or dismiss a HITL request |


### `/hooks`

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/hooks/session-start` | Claude Code / Codex SessionStart hook |
| `POST` | `/hooks/stop` | Claude Code / Codex Stop hook |


### `/mcp`

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/mcp` |  |
| `POST` | `/mcp` |  |
| `GET` | `/mcp/quickstart` | One-page MCP quick reference — the 5 tools you use 90% of the time |
| `GET` | `/mcp/sse` | MCP SSE transport GET — opens event stream for dnakov/claude-mcp |
| `OPTIONS` | `/mcp/sse` | CORS preflight for chrome-extension:// origin |
| `POST` | `/mcp/sse` | MCP SSE transport POST — JSON-RPC handler for dnakov/claude-mcp |
| `GET` | `/mcp/tools-doc` | Generate organized markdown MCP tool reference |


### `/me`

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/me` | Return the current user's plan info. Returns {} for anonymous/self-hosted |
| `GET` | `/me/workspaces` | Return all workspaces the current user belongs to |


### `/oauth`

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/oauth/authorize` |  |
| `POST` | `/oauth/device` | RFC 8628 device authorization endpoint |
| `GET` | `/oauth/device-callback` | Show auth code; JS auto-redirects to the local callback so MCP SDK completes the flow |
| `POST` | `/oauth/register` |  |
| `POST` | `/oauth/token` |  |


### `/onboarding`

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/onboarding` | Plan selection page for new users after first login |


### `/outputs`

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/outputs/mcp/{tenant_id}` | 469d89b4 — Proxy requests to the tenant's meridian-outputs server over the outputs tunnel |
| `OPTIONS` | `/outputs/mcp/{tenant_id}` | 469d89b4 — Proxy requests to the tenant's meridian-outputs server over the outputs tunnel |
| `POST` | `/outputs/mcp/{tenant_id}` | 469d89b4 — Proxy requests to the tenant's meridian-outputs server over the outputs tunnel |
| `GET` | `/outputs/mcp/{tenant_id}/{rest:path}` | Same as outputs_mcp_proxy but for sub-paths |
| `OPTIONS` | `/outputs/mcp/{tenant_id}/{rest:path}` | Same as outputs_mcp_proxy but for sub-paths |
| `POST` | `/outputs/mcp/{tenant_id}/{rest:path}` | Same as outputs_mcp_proxy but for sub-paths |


### `/ppt`

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/ppt/mcp/{tenant_id}` | Proxy requests to the tenant's powerpoint-mcp over the ppt tunnel |
| `OPTIONS` | `/ppt/mcp/{tenant_id}` | Proxy requests to the tenant's powerpoint-mcp over the ppt tunnel |
| `POST` | `/ppt/mcp/{tenant_id}` | Proxy requests to the tenant's powerpoint-mcp over the ppt tunnel |
| `GET` | `/ppt/mcp/{tenant_id}/{rest:path}` | Same as ppt_mcp_proxy but for sub-paths |
| `OPTIONS` | `/ppt/mcp/{tenant_id}/{rest:path}` | Same as ppt_mcp_proxy but for sub-paths |
| `POST` | `/ppt/mcp/{tenant_id}/{rest:path}` | Same as ppt_mcp_proxy but for sub-paths |


### `/pricing`

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/pricing` | Pricing page — Free / Solo / Team tiers with waitlist forms when hosted launch is pending |


### `/privacy`

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/privacy` | Static Privacy Policy page |


### `/projects`

| Method | Path | Description |
|--------|------|-------------|
| `DELETE` | `/projects` | 0e4980d4 — batch delete: accepts multiple project ids in one call |
| `GET` | `/projects` | List projects visible to the caller |
| `POST` | `/projects` | Create a new project. 409 if the name is already in use |
| `GET` | `/projects/by-name/{name}` | Look up a project by name (case-insensitive substring match) |
| `DELETE` | `/projects/{project_id}` | v1.9.x — delete a project and all data |
| `GET` | `/projects/{project_id}` | Look up a project by id |
| `GET` | `/projects/{project_id}/agent-instructions` | Return the current agent_instructions for a project |
| `PATCH` | `/projects/{project_id}/agent-instructions` | Set or clear agent_instructions. Pass null to reset to server defaults |
| `GET` | `/projects/{project_id}/agent-instructions/default` | Return the server DEFAULT_AGENT_INSTRUCTIONS (for the Reset button) |
| `POST` | `/projects/{project_id}/codebase-map` | 5813affe — render a package-level codebase map (graphviz) from the graph |
| `GET` | `/projects/{project_id}/context` | Return a onboarding context payload for new chat sessions (v1.9.x) |
| `GET` | `/projects/{project_id}/context-block` | v2.3 — plain-text context block suitable for direct clipboard paste |
| `POST` | `/projects/{project_id}/decisions` | v1.1.4 — append a decision entry to the project's append-only |
| `GET` | `/projects/{project_id}/decisions-pinned` | Active pinned decisions (newest first). ``?include_superseded=true`` returns full history |
| `POST` | `/projects/{project_id}/decisions-pinned` | Create a new pinned decision |
| `POST` | `/projects/{project_id}/decisions-pinned/archive-oldest` | Archive the oldest active pinned decisions without creating replacements |
| `POST` | `/projects/{project_id}/decisions-pinned/replace-all` | Atomically replace all active pinned decisions with a new set (AI consolidation) |
| `DELETE` | `/projects/{project_id}/decisions-pinned/{decision_id}` | Hard-delete a pinned decision. Use update (status=superseded) to archive instead |
| `PATCH` | `/projects/{project_id}/decisions-pinned/{decision_id}` | Patch fields, or supersede (pass new_title + new_body to atomically retire+create) |
| `POST` | `/projects/{project_id}/decisions/consolidate` | Call an external LLM to deduplicate and consolidate pinned decisions |
| `POST` | `/projects/{project_id}/devlog` | Append a user-written line to DEVLOG.md via the devlog anchor |
| `GET` | `/projects/{project_id}/document-structure` | 3f596f81 — heading-tree structure of an ingested .docx for the Documents |
| `POST` | `/projects/{project_id}/documents/upload` | f1c7e7d1 — tunnel-free document upload (plain .txt/.md only, v1) |
| `POST` | `/projects/{project_id}/events` | Normalize a framework event into Meridian's task_log |
| `GET` | `/projects/{project_id}/export/pdf` | Generate a tamper-evident IP attribution PDF for the project |
| `GET` | `/projects/{project_id}/files` | Return the list of editable markdown files for a project |
| `GET` | `/projects/{project_id}/files/{filename}` | Read one editable markdown file and return its content |
| `PUT` | `/projects/{project_id}/files/{filename}` | Write content to one editable markdown file |
| `PATCH` | `/projects/{project_id}/github/account` | Pin a specific GitHub account to a project (or clear the pin) |
| `GET` | `/projects/{project_id}/github/branches` | v2.8 — list the branches of a repo so the Branch field can be a dropdown |
| `POST` | `/projects/{project_id}/github/connect` | Connect or update the tenant's GitHub repo settings |
| `DELETE` | `/projects/{project_id}/github/disconnect` | Clear the project's stored GitHub repo (keeps tenant PAT for other projects) |
| `POST` | `/projects/{project_id}/github/push-mcp-template` | Push template.mcp.json to the connected GitHub repo |
| `GET` | `/projects/{project_id}/github/repos` | Return the tenant's accessible GitHub repos for the connect dropdown |
| `GET` | `/projects/{project_id}/github/status` | Return the project's current GitHub connection status |
| `GET` | `/projects/{project_id}/goal` | Read the latest goal state plus ambient task context |
| `POST` | `/projects/{project_id}/goal` | Upsert the goal state, incrementing version |
| `GET` | `/projects/{project_id}/goal-history` | Return meaningful goal versions for a project, newest first |
| `GET` | `/projects/{project_id}/goal-mode` | Return the current goal mode for a project |
| `PATCH` | `/projects/{project_id}/goal-mode` | Switch a project between 'manual' and 'auto' goal modes |
| `POST` | `/projects/{project_id}/goal/north-star` | v0.5.2 — update only the north star field |
| `POST` | `/projects/{project_id}/goal/sprint` | v0.5.2 — update only the sprint field |
| `POST` | `/projects/{project_id}/handoff` | Render and write the handoff file for a project |
| `POST` | `/projects/{project_id}/handoff/corrections` | 3af86d28 — record a corrective handoff for a blocked executor session |
| `GET` | `/projects/{project_id}/handoff/corrections/latest` | 3af86d28 — load a corrective handoff directly (never reconstruct from notes) |
| `GET` | `/projects/{project_id}/handoff/planner` | GET the planner-optimised handoff for a project |
| `GET` | `/projects/{project_id}/hitl` | HITL requests scoped to a single project |
| `POST` | `/projects/{project_id}/hitl` | Create a HITL request. Sessions paused on blocking should POST then poll |
| `PATCH` | `/projects/{project_id}/icon` | G4.17 — set or clear the single-emoji icon for a project |
| `GET` | `/projects/{project_id}/insights` | Project insights, newest first. ``?horizon=permanent\|year\|quarter`` filters |
| `POST` | `/projects/{project_id}/insights` | Create a strategic insight (title required; horizon defaults to 'quarter') |
| `GET` | `/projects/{project_id}/notes` | Project notes (newest first). ``?tag=X`` filters by tag; ``?query=X`` searches title+body |
| `POST` | `/projects/{project_id}/notes` | Create a new note. Body: {title, body, tags?} |
| `DELETE` | `/projects/{project_id}/notes/{note_id}` | Hard-delete a note. Returns 204 or 404 |
| `PATCH` | `/projects/{project_id}/notes/{note_id}` | Patch title/body/tags |
| `POST` | `/projects/{project_id}/notify/test` | Send a test notification to verify the configured notify URL and/or email |
| `GET` | `/projects/{project_id}/ntfy` | Return the current notification settings for this project |
| `PATCH` | `/projects/{project_id}/ntfy` | Save (or clear) the notify URL and/or notify_email for this project |
| `PATCH` | `/projects/{project_id}/organization` | 8db00fcb — set a project's status (active\|parked\|archived) and/or |
| `GET` | `/projects/{project_id}/orphan_reaper` | f7084ed0 — dashboard-facing status for the orphan-process-reaper Stop |
| `POST` | `/projects/{project_id}/orphan_reaper/toggle` | f7084ed0 — dashboard opt-in/opt-out for the orphan-process-reaper Stop |
| `POST` | `/projects/{project_id}/parent` | 0fed6a42 — set / change / clear a project's parent (subproject hierarchy) |
| `POST` | `/projects/{project_id}/queue-session` | Queue the next /goal string; it's appended to the next handoff and then |
| `GET` | `/projects/{project_id}/queued-session` | Return the currently queued next-session goal, or null |
| `GET` | `/projects/{project_id}/reconcile` | Cross-reference pending sprint items against recent git commits |
| `GET` | `/projects/{project_id}/registered-machines` | List the tenant's registered hook machines (token omitted) for the |
| `DELETE` | `/projects/{project_id}/registered-machines/{machine_id}` | Revoke one of the tenant's registered machines |
| `POST` | `/projects/{project_id}/rename` | v1.9.x — rename a project.  Broadcasts project_renamed WS event |
| `GET` | `/projects/{project_id}/repo-image` | G7.32 — proxy a repo-relative image through the project's GitHub PAT |
| `GET` | `/projects/{project_id}/resources/sprint-items` | f5f2a89d — reverse lookup: sprint items whose touches_resources includes resource |
| `GET` | `/projects/{project_id}/rewind` | v1.3.0 — "Last X days" project rewind summary |
| `POST` | `/projects/{project_id}/rewind-token` | v1.3.0 — mint (or return) the project's shareable rewind token |
| `GET` | `/projects/{project_id}/runs` | List executor_runs for a project, newest first |
| `GET` | `/projects/{project_id}/runs/{run_id}` | Return a single executor_run with full transcript |
| `GET` | `/projects/{project_id}/search` | Universal search across tasks, notes, decisions, and sprint items |
| `GET` | `/projects/{project_id}/session-timeline` | 1e1bd6b0 — per-executor-session timeline: each session's start/end + the |
| `GET` | `/projects/{project_id}/sessions` | List sessions attached to the project |
| `GET` | `/projects/{project_id}/sessions/{session_id}/tasks/live` | Return the last N task_log rows for a session — live Queue feed |
| `GET` | `/projects/{project_id}/settings` | Return persisted per-project dashboard settings |
| `PATCH` | `/projects/{project_id}/settings` | Update persisted per-project dashboard settings |
| `GET` | `/projects/{project_id}/slot-readiness` | Probe whether the code/Serena tunnel slot is ready for this project |
| `GET` | `/projects/{project_id}/sprint-items` | List sprint items, optionally filtered by status |
| `POST` | `/projects/{project_id}/sprint-items` | Append a todo sprint item. Body: ``{version, title, group?, human_id?}`` |
| `DELETE` | `/projects/{project_id}/sprint-items/{item_id}` | Delete a sprint item permanently |
| `GET` | `/projects/{project_id}/sprint-items/{item_id}` | 4ef6ce5e — fetch ONE sprint item's live row, scoped to ``project_id`` |
| `PATCH` | `/projects/{project_id}/sprint-items/{item_id}` | Update editable fields (title, version) of a sprint item |
| `POST` | `/projects/{project_id}/sprint-items/{item_id}/complete` | Mark a sprint item ``done``. Optional body: ``{task_id}`` |
| `POST` | `/projects/{project_id}/sprint-items/{item_id}/fail` | Mark a sprint item ``failed``. Optional body: ``{reason}`` |
| `POST` | `/projects/{project_id}/sprint-items/{item_id}/push` | Push a sprint item to a future version. Body: ``{to_version}`` |
| `POST` | `/projects/{project_id}/sprint-items/{item_id}/skip` | Mark a sprint item ``skipped``. Optional body: ``{reason}`` |
| `GET` | `/projects/{project_id}/sprint/pending_count` | c0d2356d — count of not-yet-done sprint items for a project. Powers the |
| `GET` | `/projects/{project_id}/sprint/test_coverage_expected` | 43539c70 - does the project's current in-progress sprint item call for |
| `POST` | `/projects/{project_id}/start-session` | v0.4.4 — one call to start a coordinated session |
| `POST` | `/projects/{project_id}/start-worker-session` | v1.2.0 — REST mirror of the MCP ``start_worker_session`` tool |
| `GET` | `/projects/{project_id}/stats` | Return activity stats for the Charts subtab |
| `GET` | `/projects/{project_id}/tasks` | List recent tasks for a project, newest first. Supports pagination via limit/offset |
| `POST` | `/projects/{project_id}/tasks/claim` | Atomically claim a pending task. Returns ``claimed=False`` when |
| `GET` | `/projects/{project_id}/tasks/claimable` | List unclaimed pending tasks for a project |
| `POST` | `/projects/{project_id}/tasks/release` | Release a previously-claimed task |
| `GET` | `/projects/{project_id}/tasks/search` | Text search over task descriptions |
| `GET` | `/projects/{project_id}/timeline` | v1.1.1 — return the data needed to render the Activity Timeline |
| `GET` | `/projects/{project_id}/webhook-token` | Mint-and-return the project webhook token. Shown ONCE in the UI |
| `GET` | `/projects/{project_id}/worktrees` | List active git worktrees registered for a project |
| `POST` | `/projects/{project_id}/worktrees` | Register a git worktree for a session. Call after `git worktree add` |
| `GET` | `/projects/{project_id}/worktrees/pending_cleanup` | e401221d — read-only: worktree rows still marked active in the DB whose |
| `POST` | `/projects/{project_id}/worktrees/sweep` | a03c0eeb — on-demand real disk cleanup for this project's worktrees |
| `DELETE` | `/projects/{project_id}/worktrees/{worktree_id}` | Mark a registered worktree as removed — and, self-hosted only, actually |


### `/sessions`

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/sessions/register` | Create a session row tied to a project |
| `PATCH` | `/sessions/{session_id}` | Update lightweight session state used by the dashboard |
| `POST` | `/sessions/{session_id}/close` | Mark a session closed |
| `POST` | `/sessions/{session_id}/heartbeat` | Touch ``last_seen`` to keep this session alive |
| `GET` | `/sessions/{session_id}/notes` | Return sprint scratch-pad notes for a session (newest first) |


### `/settings`

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/settings/mcp-config` | Return project list + base URL for building the MCP client config snippet |
| `GET` | `/settings/notifications` | v2.5 — return current notification preferences for the authenticated tenant |
| `PATCH` | `/settings/notifications` | v2.5 — save email notification preferences for the authenticated tenant |
| `GET` | `/settings/usage` | Return current compute + storage usage and overage caps for the tenant |
| `PATCH` | `/settings/usage` | Update compute and storage overage spending caps for the tenant |


### `/setup`

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/setup` | b6c9f20d — First-run setup alias for binary users |
| `GET` | `/setup/health` | 13583103 — self-hosted diagnostics: which auth providers are configured |
| `GET` | `/setup/needed` | Returns {needed: true} if no projects exist yet (first-run wizard trigger) |


### `/status`

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/status/server` | shields.io badge: server liveness |
| `GET` | `/status/sessions` | shields.io badge: count of currently-live sessions |
| `GET` | `/status/tools` | shields.io badge: MCP tool count (cached at startup) |


### `/tasks`

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/tasks` | Append a task-log entry |
| `POST` | `/tasks/enqueue` | Paid-tier: queue a Claude subprocess and return the pending task row |
| `DELETE` | `/tasks/{task_id}` | Hard-delete a task-log entry |
| `PATCH` | `/tasks/{task_id}` | Update a task's status and/or description in place |


### `/team`

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/team/summary` | Aggregate task_log + sessions by human_id over the last N days |


### `/terms`

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/terms` | Static Terms of Service page |


### `/tools`

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/tools` | Return MCP tool definitions for the dashboard Docs vtab |


### `/tunnel`

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/tunnel/active-repo` | Send a set_active_repo control message to the tenant's extract WebSocket |
| `DELETE` | `/tunnel/filesystem-roots` | live-fs-roots — remove a served filesystem root and apply it live |
| `GET` | `/tunnel/filesystem-roots` | Return the directories the tunnel's filesystem connector may read |
| `POST` | `/tunnel/filesystem-roots` | live-fs-roots — add a served filesystem root and push it live |
| `GET` | `/tunnel/manifest` | Read-only tools/list manifest snapshot for this tenant (49d8244d) |
| `GET` | `/tunnel/plugins` | Return the current tenant's resolved tunnel plugins + raw override config |
| `PUT` | `/tunnel/plugins` | Persist the tenant's tunnel plugin overrides (Settings → Tunnel Plugins) |
| `GET` | `/tunnel/plugins/check` | Check whether a plugin binary is available on the server's PATH |
| `DELETE` | `/tunnel/plugins/custom` | 9811d04c — remove a persisted custom plugin by name |
| `POST` | `/tunnel/plugins/custom` | 9811d04c — persist a chosen custom plugin into the tenant's tunnel config |
| `POST` | `/tunnel/plugins/install` | Run a plugin install command on the server machine (self-hosted deployments) |
| `POST` | `/tunnel/refresh` | Force a synchronous tunnel-tool re-aggregation and return the manifest |
| `GET` | `/tunnel/registry` | Proxy the official MCP Registry API to avoid browser CORS restrictions |
| `GET` | `/tunnel/status/{tenant_id}` | Return whether the tenant currently has an active tunnel socket |


### `/waitlist`

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/waitlist` | GET all waitlist entries, newest first. Admin use only |
| `POST` | `/waitlist` | POST {"email": "...", "note": "..."} — add to hosted-tier waitlist |


### `/waitlist-pending`

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/waitlist-pending` | Landing page for non-admin users who sign in during pre-launch |


### `/webhooks`

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/webhooks/github-marketplace` | Handle a GitHub Marketplace ``marketplace_purchase`` event |
| `POST` | `/webhooks/stripe` | Handle Stripe webhook events |


### `/word`

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/word/mcp/{tenant_id}` | Proxy requests to the tenant's docx-mcp server over the word tunnel |
| `OPTIONS` | `/word/mcp/{tenant_id}` | Proxy requests to the tenant's docx-mcp server over the word tunnel |
| `POST` | `/word/mcp/{tenant_id}` | Proxy requests to the tenant's docx-mcp server over the word tunnel |
| `GET` | `/word/mcp/{tenant_id}/{rest:path}` | Same as word_mcp_proxy but for sub-paths |
| `OPTIONS` | `/word/mcp/{tenant_id}/{rest:path}` | Same as word_mcp_proxy but for sub-paths |
| `POST` | `/word/mcp/{tenant_id}/{rest:path}` | Same as word_mcp_proxy but for sub-paths |


### `/workspace`

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/workspace/accept` | Accept a workspace invite. Marks joined_at and redirects to dashboard |
| `GET` | `/workspace/blog` | Workspace-scoped blog posts (newest first). ``?status=`` filters to |
| `POST` | `/workspace/blog` | Create or update a workspace blog post. Body: {title, body?, status?, |
| `POST` | `/workspace/connect-db` | Store a custom Postgres connection string as the user's project DB |
| `GET` | `/workspace/decisions` | Active workspace decisions (newest first). ``?include_superseded=true`` |
| `POST` | `/workspace/decisions` | Pin a workspace decision. Body: {title, body, category?} |
| `DELETE` | `/workspace/decisions/{decision_id}` | Hard-delete a workspace decision. Returns 204 or 404 |
| `POST` | `/workspace/invite` | Invite a new workspace member. Sends invite email via Resend |
| `POST` | `/workspace/invite/{member_id}/resend` | Resend invite email for a pending workspace member |
| `GET` | `/workspace/members` | List all workspace members (pending and accepted) for the current tenant |
| `DELETE` | `/workspace/members/{member_id}` | Remove a workspace member or revoke a pending invite |
| `PATCH` | `/workspace/members/{member_id}` | v2.8 — change a workspace member's role (and github_access cap) |
| `GET` | `/workspace/notes` | Workspace notes (newest first). ``?tag=X`` filters by substring match |
| `POST` | `/workspace/notes` | Create a workspace note. Body: {title, body, tags?} |
| `DELETE` | `/workspace/notes/{note_id}` | Hard-delete a workspace note. Returns 204 or 404 |
| `PATCH` | `/workspace/notes/{note_id}` | Patch title/body/tags on a workspace note |
| `POST` | `/workspace/notes/{note_id}/move` | Move a workspace note to a project (converts it to a project note and |
| `GET` | `/workspace/settings` | Read the workspace-global default settings (singleton) |
| `PATCH` | `/workspace/settings` | Patch workspace-global defaults. Only the fields passed are changed |
| `GET` | `/workspace/sprint-items` | Workspace sprint items, grouped by item_group. ``?status=`` and |
| `POST` | `/workspace/sprint-items` | Add a workspace sprint item. Body: {title, group?, human_id?} |
| `PATCH` | `/workspace/sprint-items/{item_id}` | Patch title/status/group/human_id on a workspace sprint item |
| `POST` | `/workspace/sprint-items/{item_id}/complete` | Mark a workspace sprint item done (stamps completed_at) |


### `/zotero`

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/zotero/mcp/{tenant_id}` | Proxy requests to the tenant's zotero-mcp server over the zotero tunnel |
| `OPTIONS` | `/zotero/mcp/{tenant_id}` | Proxy requests to the tenant's zotero-mcp server over the zotero tunnel |
| `POST` | `/zotero/mcp/{tenant_id}` | Proxy requests to the tenant's zotero-mcp server over the zotero tunnel |
| `GET` | `/zotero/mcp/{tenant_id}/{rest:path}` | Same as zotero_mcp_proxy but for sub-paths |
| `OPTIONS` | `/zotero/mcp/{tenant_id}/{rest:path}` | Same as zotero_mcp_proxy but for sub-paths |
| `POST` | `/zotero/mcp/{tenant_id}/{rest:path}` | Same as zotero_mcp_proxy but for sub-paths |

