# AGENTS.md — Meridian User Template (Codex)

This file is read by Codex at session start. Same content as CLAUDE.md —
both agents use the same Meridian MCP tools.

---

## Connect to Meridian

Hosted tier (no install) — add to `~/.codex/config.toml`:

```toml
[mcp_servers.meridian]
type = "http"
url = "https://usemeridian.us/mcp"

[mcp_servers.meridian.http_headers]
Authorization = "Bearer sk_meridian_YOUR_TOKEN"
```

Self-hosted (from source):
```toml
[mcp_servers.meridian]
type = "stdio"
command = "pixi"
args = ["run", "python", "-m", "meridian", "--mcp"]
cwd = "/path/to/Meridian"
```

---

## Your project ID

```
PROJECT_ID=your-project-id-here
```

---

## Session rules

ALWAYS at session start:
- Call `start_session(project_id="PROJECT_ID", session_name="describe-what-youre-doing")`

ALWAYS during work:
- Call `log_task(session_id, project_id, description)` after completing meaningful work.
- Call `pin_decision(project_id, title, body, category)` for architectural choices.
- Call `request_hitl(project_id, question)` when you need a human decision.
- After completing each sprint item, call `get_sprint_items(status='pending')` to check for newly added items before moving to the next one. New items added mid-run get picked up at the next item boundary.

ALWAYS before ending:
- Call `checkpoint(session_id, project_id)` — snapshots progress, generates delta handoff.

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

Running two Codex sessions on the same repo without isolation causes silent overwrites.

**Option A — Meridian file claims (works across all clients):**
- Call `claim_file(session_id, path)` before editing any shared file.
- Check `start_session` response for `file_warnings` — if another session has claimed a file you need, stop and call `request_hitl` to ask the human to serialize the work.
- High-contention files (always sequential): `dashboard.js`, `server.py`, `db/__init__.py`

**Option B — AGENTS.md worktree isolation (Claude Code only):**
```yaml
## executor
description: Executes sprint items.
tools: read, write, bash, edit
isolation: worktree
model: claude-sonnet-4-6
```

**Option C — Codex sandboxed environments:**
Codex runs tasks in parallel sandboxed environments by default when you use the web UI. For CLI, run each session in a separate directory/branch.

---

## Auto-checkpoint with hooks

```bash
# Mac/Linux
curl -fsSL https://usemeridian.us/hooks.sh | bash

# Windows
irm https://usemeridian.us/hooks.ps1 | iex
```

Writes hook config to `~/.codex/config.toml` — every Codex session auto-injects
project context on start and checkpoints on end.

---

## Docs

- Full MCP tool reference: https://docs.usemeridian.us/mcp-tools/
- Web docs: https://docs.usemeridian.us

---

## Launching Claude Code executor sessions with --rc

Set `ENABLE_TOOL_SEARCH=false` before invoking `claude --rc` to ensure MCP tools load.
Without it, deferred tools may not resolve and MCP calls will fail silently.

```powershell
$env:ENABLE_TOOL_SEARCH="false"
claude --rc --dangerously-skip-permissions
```

---

## PARALLEL SESSIONS

Multiple AI sessions (Claude Code + Codex, or two concurrent Claude Code windows) can work on the same project simultaneously. Follow these rules to avoid conflicts:

**Before editing any file:**
1. Call `claim_file(file_path, session_id)` to register your intent.
2. Check `start_session` response for `file_warnings` — if a file you need is already claimed by another active session, coordinate before editing.

**High-contention files — always sequential, never parallel:**
- `meridian/static/dashboard.js` — monolithic frontend, merge conflicts are painful
- `meridian/server.py` — central FastAPI app, concurrent edits cause import errors
- `meridian/db/__init__.py` — all DB logic, schema changes must be serialized
- `hooks.ps1` — ⛔ NEVER edit or run. User-facing installer; running it rotates the API token and kills the human's active Claude Code session.
- `hooks.sh` — ⛔ NEVER edit or run. Same token-rotation hazard as `hooks.ps1`.

**Rules:**
- Never edit a file another active session has claimed (within the last 10 minutes).
- `start_session` returns `file_warnings` when a conflict is detected — stop and coordinate.
- Sprint items carry a `touches_files` field auto-populated from recent git history — check it before starting.
- Cross-machine awareness works via the hosted DB — this covers Claude Code + Codex running simultaneously on separate machines.

**Release locks when done:**
Call `release_file(file_path, session_id)` after your changes are committed.

---
## Meridian-managed notes

<!-- Agent-proposed via update_md_section, human-approved via the dashboard HITL queue. -->
<!-- MERIDIAN:ANCHOR:START:agents-body -->
<!-- MERIDIAN:ANCHOR:END:agents-body -->
