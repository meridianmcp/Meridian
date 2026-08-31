# Data Handling

This page explains, in plain language, where your Meridian data actually lives, what (if anything) leaves your control, and how to delete it. It applies to both the self-hosted server and the hosted tier at usemeridian.us.

If anything here seems unclear or you want specifics beyond what's published, reach out at <hello@usemeridian.us>.

---

## Where your data lives

Meridian stores project state — goals, sprint items, task logs, decisions, notes, session activity — in one database. Which database depends entirely on which mode you run.

### Self-hosted

If you run Meridian yourself (binary, Docker, or from source with pixi), **all data stays on your machine**:

- By default, Meridian writes to a local SQLite file at `./data/meridian.db`.
- You can point it at your own Postgres instance instead (`MERIDIAN_DB_URL=postgresql://...`) — still infrastructure you control, not Meridian's.
- The server binds to `localhost` unless you explicitly configure it otherwise. There is no telemetry call-home and no background sync to usemeridian.us.
- Deleting your data is exactly as simple as it sounds: it's files and/or a database you own. See [Deleting your data](#deleting-your-data) below.

### Hosted (usemeridian.us)

If you use the hosted tier, your project data lives in a Postgres database (Neon) provisioned for your workspace, isolated from other customers' data. Meridian staff do not access your project data in the normal course of operating the service — the HITL (human-in-the-loop) queue exists specifically so that *you* stay the human reviewing and approving what your AI sessions do, not us.

You can also bring your own database (**BYODB**): paste your own Neon connection string at signup and Meridian initializes its schema there instead of the managed one. In that mode your data never touches Meridian's own database at all.

!!! info "Sign-in vs. data access"
    Signing in to the hosted tier with Google or GitHub OAuth only authenticates *you* — it does not grant Meridian access to your GitHub repositories, Google Drive, or anything beyond your name/email for account identification. Repo access is a separate, optional step — see below.

---

## GitHub: an optional, separate connection

Connecting a GitHub repository is **entirely optional** and unrelated to signing in. It only applies if you explicitly set it up in a project's Settings tab to give AI sessions access to your code and repository metadata (file reads, code search, commit history, GitHub Actions, and issues).

If you use it, here's what actually happens, per the [GitHub Integration guide](github-integration.md):

- Your personal access token (PAT) is **encrypted at rest** (AES-256 / Fernet) before storage — the raw token is never logged or returned by any API response.
- Read tools are read-only. Three separate tools can make explicit repository-adjacent writes when an AI session invokes them and the PAT has the required permission: `patch_file` commits a targeted file replacement, `trigger_workflow` dispatches a GitHub Actions workflow, and `create_issue` opens an issue. Meridian does not invoke these writes merely because a repository is connected.
- The connection is scoped to your account, and you can **revoke it at any time** — either by disconnecting in Settings or by revoking the PAT directly on GitHub.

If you never connect a repo, none of this applies — Meridian has no GitHub access at all.

→ [Full GitHub Integration guide](github-integration.md)

---

## Deleting your data

### Self-hosted

There's no delete flow to run because there's no remote copy to clean up. Stop the server and remove your data directory (or the SQLite file / Postgres database you pointed it at) and the data is gone. Nothing persists anywhere else.

### Hosted

**From the dashboard:**

- **Delete a project** — kebab menu (⋮) on a project card → **Delete**. This removes that project's sessions, tasks, goals, decisions, and notes.
- **Delete your account** — Dashboard → ⚙️ Settings → **Delete your account**. This removes your account and its data entirely.
- **Export before deleting** — Dashboard → Settings → **Export** gives you a full SQLite snapshot of your data (all projects, sessions, tasks, decisions, notes) at any time, so you can keep a copy or import it into a self-hosted instance.

**Via the API**, using your bearer token:

| Action | Endpoint |
|---|---|
| Delete a single project and all its data | `DELETE /projects/{project_id}` |
| Batch-delete multiple projects | `DELETE /projects` |
| Delete your account | `POST /account/delete` with JSON body `{"confirmation": "DELETE"}` |
| Export all account data (GDPR data portability) | `GET /export/my-data` |

See the [API Reference](api-reference.md) for full request/response details and authentication.

!!! warning "Deletion is permanent"
    Project and account deletion on the hosted tier removes the underlying data — there is no recovery step after confirming. Export first if you want a copy.

---

## Summary

| | Self-hosted | Hosted (usemeridian.us) |
|---|---|---|
| **Where data lives** | Your local SQLite file or your own Postgres | Isolated Neon Postgres per workspace (or your own via BYODB) |
| **Who can access it** | Only you | You; Meridian staff do not access project data in normal operation |
| **GitHub access** | Only if you configure it; read tools plus explicitly invoked write tools; revokable | Only if you configure it; read tools plus explicitly invoked write tools; revokable |
| **How to delete it** | Delete your local files/DB | Dashboard delete flows, or the API endpoints above |
