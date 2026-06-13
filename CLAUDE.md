# CLAUDE.md — Meridian User Template

This file is read by Claude Code at session start. Copy it to your project root and
fill in your `project_id` to get Meridian session coordination automatically.

---

## Connect to Meridian

```json
{
  "mcpServers": {
    "meridian": {
      "command": "pixi",
      "args": ["run", "python", "-m", "meridian", "--mcp"],
      "cwd": "/path/to/Meridian"
    }
  }
}
```

Or use the hosted tier (no install):
```json
{
  "mcpServers": {
    "meridian": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://usemeridian.us/mcp"],
      "env": { "BEARER_TOKEN": "sk_meridian_YOUR_TOKEN" }
    }
  }
}
```

---

## Your project ID

```
PROJECT_ID=your-project-id-here
```

Get your project ID from the Meridian dashboard at `http://localhost:7878` after
running `create_project(name="your-project")`.

---

## Session rules

ALWAYS at session start:
- Call `start_session(project_id="PROJECT_ID", session_name="describe-what-youre-doing")`
- This returns the goal, recent tasks, pending sprint items, and active sessions in one call.

ALWAYS during work:
- Call `log_task(session_id, project_id, description)` after completing meaningful work.
- Call `pin_decision(project_id, title, body, category)` for any architectural choice.
- Call `request_hitl(project_id, question)` when you need a human decision before continuing.

ALWAYS before ending:
- Call `checkpoint(session_id, project_id)` — snapshots progress, generates delta handoff, returns next `/goal` string.

---

## Parallel sessions

Before editing shared files, call `claim_file(session_id, path)` and check the `file_warnings` returned by `start_session`. If another active session has claimed a file you need, serialize the work or ask the human before editing.

High-contention files are always sequential:

- `meridian/static/dashboard.js`
- `meridian/server.py`
- `meridian/db/__init__.py`
- `hooks.ps1` — ⛔ NEVER edit or run. User-facing installer; running it rotates the API token and kills the human's active Claude Code session.
- `hooks.sh` — ⛔ NEVER edit or run. Same token-rotation hazard as `hooks.ps1`.

Sprint items can carry `touches_files` so handoffs and dashboards can warn when planned work overlaps with a live session.

### Worktree isolation (parallel safe)

When `claim_sprint_item()` returns `worktree_suggested: true`, use the provided commands to isolate your work:

```
1. git worktree add {worktree_path} -b {worktree_branch}    # from worktree_setup_cmd
2. POST /projects/{id}/worktrees  {"session_id":..., "branch":..., "path":..., "item_id":...}
3. cd {worktree_path} — do ALL work here (copy .env from parent dir)
4. When done: git checkout dev && git merge {worktree_branch} --no-edit
5. DELETE /projects/{id}/worktrees/{worktree_id}
6. git worktree remove {worktree_path} --force && git branch -d {worktree_branch}
```

Enable worktree mode project-wide via `set_executor_config(isolation="worktree")`.

---

## The 5 tools you use 90% of the time

| Tool | When | Example |
|------|------|---------|
| `start_session` | First thing, every session | `start_session(project_id="abc", session_name="auth-refactor")` |
| `log_task` | After finishing anything meaningful | `log_task(session_id, project_id, "Fixed OAuth redirect bug")` |
| `checkpoint` | Before context fills, before ending | `checkpoint(session_id, project_id)` |
| `pin_decision` | Architectural choices | `pin_decision(project_id, "Use psycopg3", "asyncpg has DLL issues on Windows", "TECHNICAL")` |
| `request_hitl` | Blocking questions for a human | `request_hitl(project_id, "Should we rate-limit per IP or per token?")` |

---

## Auto-checkpoint with hooks

Wire Claude Code to checkpoint automatically on every session end:

```bash
# Mac/Linux
curl -fsSL https://usemeridian.us/hooks.sh | bash

# Windows
irm https://usemeridian.us/hooks.ps1 | iex
```

Prompts for your Meridian URL and project ID, then writes `SessionStart` and `Stop`
hooks to `~/.claude/settings.json`. From that point on, every session auto-injects
your project context on start and snapshots progress on end.

---

## Docs

- Full MCP tool reference: `http://localhost:7878/mcp/tools-doc`
- Quick reference: `http://localhost:7878/mcp/quickstart`
- Web docs: https://docs.usemeridian.us

---
## Executor rules (Meridian project only)

- **Launching executor sessions with --rc**: Set `ENABLE_TOOL_SEARCH=false` before invoking `claude --rc` to ensure MCP tools load. Without it, deferred tools may not resolve in `--rc` mode.
  ```powershell
  $env:ENABLE_TOOL_SEARCH="false"
  claude --rc --dangerously-skip-permissions
  ```
- **Secrets hygiene**: Never put credentials, connection strings, API keys, or secrets in chat or task descriptions. Mention env var names only.
- **Before every push**: Run `pixi run test` locally first. CI is a safety net — not the first check. Never push broken code.
- **End every session**: If tests pass, merge `dev → main` and push `main` to trigger deploy. Do not end the session with work stranded only on `dev`.
- **Set sprint name**: Use `PATCH /projects/{id}/goal` with body `{"sprint": "name"}` directly (or the `set_sprint` MCP tool). Do NOT use `set_goal` for sprint-only updates.
- **Handoff**: Use `get_context_block(project_id)` for the handoff context block. Do NOT read from `data/meridian-build_handoff.md` — that file is local-only and not reliable across sessions.
- **Project discovery**: Use `list_projects()` when the project ID is unknown. Never call `create_project()` without explicit human instruction — use `list_projects()` to find existing projects first. Never create a project just to get a working ID.
- **Staging pipeline**: `dev push → test → deploy preview → smoke test → merge main → prod`. Rollback fires automatically if prod /health returns non-200 after deploy.
- **NEVER run hooks.ps1 or hooks.sh**: These are user-facing installers. Running them generates a new API token and invalidates the human's active Claude Code session. Never run `irm hooks.ps1 | iex` or `bash hooks.sh` during a sprint.
- **Demo write protection**: Adding a new write endpoint requires NO demo exception — the middleware in `server.py` handles it globally. When adding a new write UI element, add it to the `hideDemoAdminControls()` selector list in `dashboard.js`.
- **Set display name**: Settings → Account & Workspace → Workspace → "Your display name" → Adam
- **Mid-run sprint item pickup**: After completing each sprint item, call `get_sprint_items(status='pending')` to check for newly added items before moving to the next one. New items added to the board mid-run get picked up automatically at the next item boundary.

---
## Meridian-managed notes

<!-- Agent-proposed via update_md_section, human-approved via the dashboard HITL queue. -->
<!-- MERIDIAN:ANCHOR:START:claude-body -->
<!-- MERIDIAN:ANCHOR:END:claude-body -->

---
<!-- MERIDIAN STATE — auto-generated, do not edit below -->
## Current Sprint State  _(auto-updated 2026-06-13 08:51 UTC)_

**Key Files:**
- `meridian/server.py` — FastAPI app + MCP handlers
- `meridian/db.py` — all DB functions (SQLite + Postgres)
- `meridian/static/dashboard.js` — dashboard UI
- `tests/test_core.py` — full test suite
- `data/meridian-build_handoff.md` — session handoff
