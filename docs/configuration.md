# Configuration

All Meridian configuration is via environment variables. Set them in a `.env` file
in the project root, via shell export, or via your deployment platform's secret store.

For a basic self-hosted install you only need **`SESSION_SECRET`** (and `APP_URL` if
you're exposing it on a domain). Everything else is optional.

---

## Database

| Variable | Description | Default | Required |
|----------|-------------|---------|----------|
| `MERIDIAN_DB_URL` | Postgres connection string. When set, Meridian uses Postgres instead of SQLite. Example: `postgresql://user:pass@host/dbname` | — | No (SQLite if unset) |
| `MERIDIAN_DB` | Path to the SQLite database file. Ignored when `MERIDIAN_DB_URL` is set. | `data/meridian.db` | No |
| `MERIDIAN_DATA_DIR` | Directory for data files (SQLite DB, handoff files). | `data/` | No |

SQLite is the default and is fine for a single instance. Use Postgres if you want
multiple instances or a managed backend (see [Self-Hosting](self-hosting.md)).

---

## Authentication

Only needed if you want OAuth sign-in. A purely local single-user install can skip these.

| Variable | Description | Default | Required |
|----------|-------------|---------|----------|
| `GOOGLE_CLIENT_ID` | Google OAuth app client ID. Get from [console.cloud.google.com](https://console.cloud.google.com). | — | For Google login |
| `GOOGLE_CLIENT_SECRET` | Google OAuth app client secret. | — | For Google login |
| `GITHUB_CLIENT_ID` | GitHub OAuth app client ID. Get from [github.com/settings/developers](https://github.com/settings/developers). | — | For GitHub login |
| `GITHUB_CLIENT_SECRET` | GitHub OAuth app client secret. | — | For GitHub login |
| `SESSION_SECRET` | Secret key for signing session cookies. Use a long random string. | `dev-secret-change-me` | **Yes for production** |
| `MERIDIAN_SESSION_SECRET` | Alias for `SESSION_SECRET`. Either name works. | — | — |

!!! tip
    Register your own OAuth apps and point their callback URLs at
    `https://your-domain/auth/google/callback` and
    `https://your-domain/auth/github/callback`.

---

## App / General

| Variable | Description | Default | Required |
|----------|-------------|---------|----------|
| `APP_URL` | Public base URL of the deployment. Used in OAuth callbacks, emails, and MCP endpoint URLs. Example: `https://meridian.example.com` | `http://localhost:7878` | **Yes for production** |
| `MERIDIAN_BASE_URL` | Alias for `APP_URL`. Either name works. | — | — |
| `MERIDIAN_PORT` | HTTP port for the dashboard/API server. | `7878` | No |
| `MERIDIAN_HOST` | Host to bind the server to. | `127.0.0.1` | No |
| `MERIDIAN_HUMAN_ID` | Default human identifier for task attribution. Falls back to `$USER` / `$USERNAME` / hostname. | — | No |
| `MERIDIAN_AFTER_LOGIN_URL` | Where to redirect after successful OAuth login. | `/dashboard` | No |
| `MERIDIAN_AUTO_SUMMARY_INTERVAL` | Seconds between auto-summary cycles (background task). | `600` | No |
| `SITE_PASSWORD` | When set, all routes (except `/health`) require entering this password in a gate page. Handy for locking down a staging/preview deployment. | — | No |

---

## Optional: billing & email

These are only relevant if you run Meridian as a **paid, hosted service** for others.
A normal self-hosted install does not need them.

| Variable | Description | Required |
|----------|-------------|----------|
| `STRIPE_SECRET_KEY` | Stripe secret key (`sk_test_...` / `sk_live_...`). | For billing |
| `STRIPE_WEBHOOK_SECRET` | Stripe webhook signing secret (`whsec_...`). | For billing |
| `STRIPE_PAYMENT_LINK` | URL of your Stripe Payment Link, shown as the upgrade button. | No |
| `RESEND_API_KEY` | [Resend](https://resend.com) API key for transactional email. If unset, email is silently skipped. | For email |
| `MERIDIAN_FROM_EMAIL` | Sender address for outgoing email. | No |

!!! warning
    Keep `STRIPE_SECRET_KEY=sk_test_...` during development. Never switch to a live
    key without thorough testing.

---

## Example `.env`

```bash
# --- Minimal self-hosted setup ---
SESSION_SECRET=replace-with-a-long-random-string
APP_URL=https://meridian.example.com

# --- Optional: Postgres instead of SQLite ---
# MERIDIAN_DB_URL=postgresql://user:pass@host/dbname

# --- Optional: OAuth sign-in ---
# GOOGLE_CLIENT_ID=...
# GOOGLE_CLIENT_SECRET=...
# GITHUB_CLIENT_ID=...
# GITHUB_CLIENT_SECRET=...

# --- Optional: lock down a preview deployment ---
# SITE_PASSWORD=preview-password
```

!!! danger
    Never commit `.env` to git. Meridian's `.gitignore` already excludes `.env`
    and `secrets.env`.

---

## Context layers

Meridian injects project context into every AI session through three distinct layers,
each with different lifetime and scope.

### STATIC — `CLAUDE.md` / `AGENTS.md`

Repo conventions and project-level instructions committed to git. This is the
document the AI reads at session start from the repository root. It never changes
automatically between sessions — only a human (or `update_md_section`) edits it.

**Use for:** architecture decisions, coding standards, file conventions, "never do X"
rules. Anything that should be true for every session forever.

### DYNAMIC — `additionalContext` from Session Start hook

Live state injected at session start via `POST /hooks/session-start`. Meridian
assembles this from `get_context_block` — includes the current goal, active sprint
items (with their status), recent tasks, and pinned decisions.

**Use for:** what's in-flight right now. Updates every session automatically. The AI
sees the latest goal and sprint queue without any manual copy-paste.

**Source:** `GET /projects/{id}/context-block` (called automatically by the hooks).

### WORKSPACE — workspace notes and decisions

Tenant-global context injected at the **top** of every project's context block.
Created via `add_workspace_note` / `set_workspace_decision` and visible across all
projects owned by your account.

**Use for:** team-wide conventions, shared infrastructure notes, org-level decisions
that apply to every project (e.g. "always use Neon for Postgres", "company style guide").

```
┌─────────────────────────────────────┐
│  WORKSPACE (tenant-global)          │  ← shared across all projects
│  workspace_notes + workspace_decisions
├─────────────────────────────────────┤
│  DYNAMIC (per-project, per-session) │  ← goal, sprint, tasks, decisions
│  get_context_block output           │
├─────────────────────────────────────┤
│  STATIC (per-repo)                  │  ← CLAUDE.md / AGENTS.md
│  committed to git                   │
└─────────────────────────────────────┘
```

---

## Running parallel sessions safely

Meridian supports multiple AI sessions working on the same project simultaneously — for example, Claude Code and Codex running on separate machines, or two Claude Code windows on different features.

### File conflict prevention

When you call `start_session`, Meridian checks the `file_locks` table for files claimed by other active sessions. If any overlap is found, the response includes a `file_warnings` array:

```json
{
  "session_id": "...",
  "file_warnings": [
    "dashboard.js claimed by session pre-launch-final (last_seen 2026-06-11 22:45:00)"
  ]
}
```

**What to do:** Stop and coordinate with the other session before editing that file. Either wait for it to finish (and release the lock), or plan your changes so they don't conflict.

### Claiming files with `claim_file`

Before editing any file, call `claim_file(file_path, session_id)`. This registers a lock that:
- Expires automatically after a configurable TTL (default: 4 hours)
- Is visible to all concurrent sessions via `start_session`
- Is released automatically when the session ends (via `checkpoint`)

```
claim_file(file_path="meridian/static/dashboard.js", session_id="<your-session-id>")
```

To release early (after you've committed), call `release_file(file_path, session_id)`.

### Sprint item `touches_files`

Each sprint item has a `touches_files` field — a JSON array of file paths the item is expected to modify. Meridian auto-populates this at `generate_handoff` time by running `git diff --name-only HEAD~3` and matching filenames against sprint item titles.

This gives the next session a head-start on knowing which items conflict with which files, even before the files are actively claimed.

### Cross-machine awareness

The hosted tier stores file locks in Neon Postgres, so awareness is global — a lock claimed by a Claude Code session on your laptop is visible to a Codex session on a CI runner or another machine. Self-hosted installs achieve the same if all instances share one `MERIDIAN_DB_URL`.
