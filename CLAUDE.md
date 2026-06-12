# CLAUDE.md — Meridian User Template

This file is read by Claude Code at session start. Copy it to your project root and
fill in your `project_id` to get Meridian session coordination automatically.

---

## Connect to Meridian

Hosted tier (no install):
```json
{
  "mcpServers": {
    "meridian": {
      "type": "http",
      "url": "https://usemeridian.us/mcp",
      "headers": { "Authorization": "Bearer sk_meridian_YOUR_TOKEN" }
    }
  }
}
```

Self-hosted (from source):
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

---

## Your project ID

```
PROJECT_ID=your-project-id-here
```

Get your project ID from the Meridian dashboard after running `create_project(name="your-project")`.

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

## The 5 tools you use 90% of the time

| Tool | When | Example |
|------|------|---------|
| `start_session` | First thing, every session | `start_session(project_id="abc", session_name="auth-refactor")` |
| `log_task` | After finishing anything meaningful | `log_task(session_id, project_id, "Fixed OAuth redirect bug")` |
| `checkpoint` | Before context fills, before ending | `checkpoint(session_id, project_id)` |
| `pin_decision` | Architectural choices | `pin_decision(project_id, "Use psycopg3", "asyncpg has DLL issues on Windows", "TECHNICAL")` |
| `request_hitl` | Blocking questions for a human | `request_hitl(project_id, "Should we rate-limit per IP or per token?")` |

---

## Parallel sessions — prevent file conflicts

Running two Claude Code sessions on the same repo without isolation causes silent overwrites.

**Option A — Claude Code native (recommended):**
```bash
claude --worktree
```
Each session gets its own branch + working directory. Conflicts surface as PR merges, not silent overwrites. Available in Claude Code v2.1.50+.

**Option B — AGENTS.md isolation:**
```yaml
## executor
description: Executes sprint items.
tools: read, write, bash, edit
isolation: worktree
model: claude-sonnet-4-6
```

**Option C — Meridian file claims (cross-client, works with Codex too):**
- Call `claim_file(session_id, path)` before editing any shared file.
- Check `start_session` response for `file_warnings` — if another session has claimed a file you need, call `request_hitl` to ask the human to serialize the work.
- High-contention files (always run sequentially): `dashboard.js`, `server.py`, `db/__init__.py`

---

## Auto-checkpoint with hooks

```bash
# Mac/Linux
curl -fsSL https://usemeridian.us/hooks.sh | bash

# Windows
irm https://usemeridian.us/hooks.ps1 | iex
```

Writes `SessionStart` and `Stop` hooks to `~/.claude/settings.json`. Every session auto-injects project context on start and snapshots progress on end.

---

## Docs

- Full MCP tool reference: https://docs.usemeridian.us/mcp-tools/
- Web docs: https://docs.usemeridian.us

---
## Executor rules (Meridian project only)

- **Secrets hygiene**: Never put credentials, connection strings, API keys, or secrets in chat or task descriptions. Mention env var names only.
- **Before every push**: Run `pixi run test` locally first. CI is a safety net — not the first check. Never push broken code.
- **End every session**: If tests pass, merge `dev → main` and push `main` to trigger deploy. Do not end the session with work stranded only on `dev`.
- **Set sprint name**: Use the `set_sprint` MCP tool. Do NOT use `set_goal` for sprint-only updates.
- **Handoff**: Use `get_context_block(project_id)` for the handoff context block.
- **Project discovery**: Use `list_projects()` when the project ID is unknown. Never call `create_project()` without explicit human instruction.
- **Staging pipeline**: `dev push → test → deploy preview → smoke test → merge main → prod`.
- **Demo write protection**: When adding a new write UI element, add it to the `hideDemoAdminControls()` selector list in `dashboard.js`.
- **Parallel sessions**: Claim files before editing with `claim_file()`. Never run two sessions touching the same file simultaneously. Check `start_session` file_warnings before proceeding.

---
## Meridian-managed notes

<!-- Agent-proposed via update_md_section, human-approved via the dashboard HITL queue. -->
<!-- MERIDIAN:ANCHOR:START:claude-body -->
<!-- MERIDIAN:ANCHOR:END:claude-body -->
