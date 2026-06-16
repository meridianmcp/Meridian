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
- If the response contains pending sprint items, immediately call `claim_sprint_item` on the first unclaimed one and start working. Do NOT ask "what would you like to work on?" when there are pending items.

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

## Python one-liners

For stdlib-only scripts (secret generation, JSON parsing, base64, file ops):
- **Windows:** `py -c "import secrets; print(secrets.token_hex(32))"`
- **Linux/Mac:** `python3 -c "import secrets; print(secrets.token_hex(32))"`

Use `pixi run python` only when Meridian deps are needed (psycopg3, cryptography, etc).
Never use PowerShell for logic that Python handles cleanly.

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

## Executor rules

Executor rules (the behavioral guidelines injected into every `start_session` response)
are managed in the **Meridian dashboard → Settings → Executor Rules**.

You can view, edit, or reset them to the Meridian defaults there.
New projects get the Meridian defaults automatically — no file configuration required.

---
## Meridian-managed notes

<!-- Agent-proposed via update_md_section, human-approved via the dashboard HITL queue. -->
<!-- MERIDIAN:ANCHOR:START:claude-body -->
<!-- MERIDIAN:ANCHOR:END:claude-body -->

---
<!-- MERIDIAN STATE — auto-generated, do not edit below -->
## Current Sprint State  _(auto-updated 2026-06-16 16:23 UTC)_

**Key Files:**
- `meridian/server.py` — FastAPI app + MCP handlers
- `meridian/db.py` — all DB functions (SQLite + Postgres)
- `meridian/static/dashboard.js` — dashboard UI
- `tests/test_core.py` — full test suite
- `data/meridian-build_handoff.md` — session handoff
