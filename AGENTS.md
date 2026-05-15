# AGENTS.md — Meridian

## What this project is

Meridian is a local Python MCP server that gives multiple Claude sessions a
shared persistent brain. It solves the context window problem for multi-session
AI workflows by providing goal state, task log, HITL queue, session registry,
and handoff generation.

**Stack:** Python, FastAPI, aiosqlite, Pydantic v2, MCP Python SDK, pixi

## Project layout

```
meridian/
  server.py       — FastAPI REST + MCP stdio server (9+ tools)
  dashboard.py    — Dashboard UI at localhost:7878
  db.py           — aiosqlite database layer
  models.py       — Pydantic v2 models
  enqueue.py      — async worker dispatch
  handoff.py      — handoff file generation
data/
  meridian.db     — SQLite database (auto-created)
  meridian-build_handoff.md — latest context handoff
tests/            — pytest test suite (66+ tests)
pixi.toml         — environment and task definitions
```

## How to run

```bash
pixi run start   # MCP + dashboard at localhost:7878
pixi run test    # run test suite
```

## Session startup protocol

Every Claude Code session MUST follow this protocol in order.
The behavior differs depending on how you're arriving:

---

### Case 1 — Normal resume (handoff file exists and is recent)

1. Read `data/meridian-build_handoff.md` — full context
2. `register_session(project_id, session_name, human_id="yourname")`
3. `get_goal(project_id)` — confirm latest version matches handoff
4. `get_tasks(project_id, limit=10)` — catch up on work since handoff
5. `get_sessions(project_id)` — see who else is active
6. Begin work. Log tasks frequently.

---

### Case 2 — Teammate did work while you were gone

1. Read `data/meridian-build_handoff.md` — baseline context
2. `register_session(project_id, session_name, human_id="yourname")`
3. `get_goal(project_id)` — may have changed since handoff
4. `get_tasks(project_id, limit=50)` — THIS is your changelog. Read all of it.
5. `get_sessions(project_id)` — who is active right now, what are they claiming
6. Read DEVLOG.md — any architectural decisions made outside the task log
7. Do NOT redo work marked `done` in the task log

---

### Case 3 — Nuked chat, no recent handoff

1. Read `data/meridian-build_handoff.md` — may be stale, use for structure only
2. `register_session(project_id, session_name, human_id="yourname")`
3. `get_goal(project_id)` — authoritative current directive
4. `get_tasks(project_id, limit=50)` — reconstruct what happened after handoff
5. Read ROADMAP.md — what versions are planned
6. Read DEVLOG.md — what decisions were made and why
7. Synthesize: goal + tasks + devlog = full current picture

---

### Case 4 — Brand new project (not meridian-build)

1. `create_project(name="your-project-name")`
2. Store the returned project_id — this is your anchor
3. `register_session(project_id, session_name, human_id="yourname")`
4. `set_goal(project_id, "your directive here")`
5. Create a project-specific AGENTS.md with the new project_id

---

## Project ID (meridian-build)

`5787cc92-ba7d-4788-b17c-28ab7938b839`

## MCP tools available

| Tool | Description |
|------|-------------|
| `create_project` | Create a new coordination project |
| `register_session` | Register this session (include human_id) |
| `get_goal` | Read current shared goal |
| `set_goal` | Update goal (owner only if human_id set) |
| `log_task` | Record what this session did/is doing/failed |
| `get_tasks` | See what all sessions have done |
| `get_sessions` | List active sessions |
| `generate_handoff` | Generate context handoff file |
| `enqueue_claude_task` | Spawn async Claude Code worker |

## Goal state discipline

- `get_goal` on every connect — always read before acting
- `set_goal` only on deliberate milestones — not every commit
- If human_id ownership is set — only the owner can set_goal
- Others must use the HITL queue to propose changes

## Task log discipline

- `log_task` at start of every significant action (status: pending)
- `log_task` on completion (status: done)
- `log_task` on failure with error detail (status: failed)
- The task log IS the project changelog — treat it that way

## Before ending any session

```
generate_handoff(project_id)
```

Always. Even if context is not full. The next session may arrive cold.

## Key files to know

- `ROADMAP.md` — full version plan, check before starting new work
- `DEVLOG.md` — incident log, read before debugging anything
- `CONTRIBUTING.md` — code standards, PR process, IP terms
- `OWNERSHIP.md` — who owns what and when
- `data/meridian-build_handoff.md` — latest context handoff
