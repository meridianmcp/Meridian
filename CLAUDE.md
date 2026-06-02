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
<!-- MERIDIAN STATE — auto-generated, do not edit below -->
## Current Sprint State  _(auto-updated 2026-06-02 02:30 UTC)_

**Key Files:**
- `meridian/server.py` — FastAPI app + MCP handlers
- `meridian/db.py` — all DB functions (SQLite + Postgres)
- `meridian/static/dashboard.js` — dashboard UI
- `tests/test_core.py` — full test suite
- `data/meridian-build_handoff.md` — session handoff
