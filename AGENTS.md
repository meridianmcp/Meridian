# AGENTS.md — Meridian

## What this project is

Meridian is a local Python MCP server that gives multiple Claude sessions a shared persistent brain. It solves the context window problem for multi-session AI workflows by providing:

- **Goal state** — a shared directive all sessions read on connect
- **Task log** — what every session has done, is doing, or failed at
- **Session registry** — who is active right now
- **HITL queue** — workers surface questions/confirmations to a human operator
- **Handoff generation** — compressed context files for cold session resumption

**Stack:** Python, FastAPI, aiosqlite, Pydantic v2, MCP Python SDK, pixi

## Project layout

```
meridian/
  server.py       — MCP stdio server (9 tools)
  dashboard.py    — FastAPI REST + WebSocket dashboard at localhost:7878
  db.py           — aiosqlite database layer
  models.py       — Pydantic v2 models
  enqueue.py      — async worker dispatch (enqueue_claude_task)
  handoff.py      — handoff file generation
data/
  meridian.db     — SQLite database (auto-created)
tests/            — pytest test suite (42 tests)
pixi.toml         — environment and task definitions
```

## How to run

```bash
# Start the server (MCP + dashboard at localhost:7878)
pixi run start

# Run tests
pixi run test
```

## MCP tools available

| Tool | Description |
|------|-------------|
| `create_project` | Create a new coordination project |
| `register_session` | Register this session with a project |
| `get_goal` | Read the current shared goal |
| `set_goal` | Update the shared goal (do this on milestones) |
| `log_task` | Record what this session did/is doing/failed |
| `get_tasks` | See what all sessions have done |
| `get_sessions` | List active sessions |
| `enqueue_claude_task` | Spawn an async Claude Code worker |
| `generate_handoff` | Generate a context handoff file |

## Session startup protocol

Every Claude Code session working on this project MUST:

1. Call `register_session(project_id, session_name)` — store the returned session_id
2. Call `get_goal(project_id)` — read the current directive before doing anything
3. Call `log_task` frequently — keep other sessions informed
4. Call `generate_handoff` before ending if context is filling up

**Project ID:** `5787cc92-ba7d-4788-b17c-28ab7938b839`
**Server URL:** `http://127.0.0.1:7878`

## Goal state discipline

- `get_goal` on connect — always read before acting
- `set_goal` only on deliberate milestones — not every commit
- Goal drift prevention: the goal is a contract between sessions

## Current version: v0.2.1

HITL queue, OAuth auth, dashboard with WebSocket live feed. See ROADMAP.md for next steps.

## Key files to know

- `ROADMAP.md` — full version plan v0.1.0 → v1.0.0
- `DEVLOG.md` — incident log and architecture decisions (read this before debugging)
- `data/meridian-build_handoff.md` — latest context handoff file
