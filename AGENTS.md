# AGENTS.md — Meridian

## What this project is

Meridian is a local Python MCP server that gives multiple Claude Code sessions a
shared persistent brain. It solves the context loss problem for multi-session AI
workflows by providing: goal state, task log, HITL queue, session registry, and
handoff generation.

**Stack:** Python, FastAPI, aiosqlite, Pydantic v2, MCP Python SDK, pixi

## STARTUP PROTOCOL — do this before anything else

Every Claude Code session MUST follow this sequence in order:

### Step 0 — If you don't know the project_id
```python
list_projects()                        # see all projects with names + ids
# or
get_project_by_name("meridian-build")  # fuzzy match → returns id + goal summary
```
The UUID is `5787cc92-ba7d-4788-b17c-28ab7938b839` but you never need to
memorise it — look it up by name when in doubt.

### Step 1 — Read the handoff file if it exists
```
read: data/meridian-build_handoff.md
```
This file contains everything that happened before you arrived — recent task log,
current goal version, active sessions. If the file exists, read it BEFORE calling
any MCP tools. It is more current than AGENTS.md.

### Step 2 — Register with Meridian (call this REGARDLESS of how you were launched)

Use `start_session` — it's the composite helper that registers, reads goal, reads
recent tasks, and returns everything in one call. Call it whether you're a normal
session, an RC session (`/rc`), a cloud session, or a worker session.

```python
start_session(
    project_id="5787cc92-ba7d-4788-b17c-28ab7938b839",
    session_name="claude-[model]-[task]",
    human_id="adam"  # or your name if you're a contributor
)
# Returns: session_id, goal (north_star + content + sprint + version), recent tasks
# Store the session_id — you need it for log_task and heartbeat
```

Do NOT skip this step for RC sessions. RC sessions (`/rc`) must call `start_session`
the same as any other session. The `.mcp.json` in the repo root configures the MCP
server automatically for RC sessions.

### Step 3 — Read the current goal (already returned by start_session)

The goal fields (north_star, version_goal, sprint) come back in the `start_session`
response. You don't need a separate `get_goal` call — they're already there.

### Step 4 — Read recent tasks (already returned by start_session)

Recent tasks are also included in the `start_session` response. Don't redo work
already marked done.

### Step 5 — Log that you've started
```python
log_task(
    project_id="5787cc92-ba7d-4788-b17c-28ab7938b839",
    session_id="<your session_id from start_session>",
    description="Starting session — read handoff, goal v{N}, recent tasks reviewed. Plan: ...",
    status="pending"
)
```

## During the session

- Call `log_task` after every significant action
- Call `log_task(status="pending")` when starting a task, `status="done"` when finished
- If context is filling up: call `generate_handoff` BEFORE ending
- If goal changes significantly: call `set_goal` — but only if you own the project
  (non-owners must use the HITL queue to propose goal changes)

## Before ending the session

```python
generate_handoff(project_id="5787cc92-ba7d-4788-b17c-28ab7938b839")
log_task(..., description="Session ending. Handoff generated.", status="done")
```

## Project layout

```
meridian/
  server.py       — FastAPI REST + MCP tool handlers
  dashboard.py    — Dashboard HTML + JS + SSE chat proxy
  db.py           — aiosqlite database layer
  models.py       — Pydantic v2 models
  enqueue.py      — async worker dispatch
  handoff.py      — handoff file generation
data/
  meridian.db     — SQLite database (WAL mode)
  meridian-build_handoff.md — latest handoff (read this first!)
tests/
  test_core.py    — full test suite (must pass before every commit)
pixi.toml         — environment + task definitions
```

## Running

```bash
pixi run start   # start server at localhost:7878
pixi run test    # run all tests (must be green before committing)
```

## MCP tools available

| Tool | When to call |
|------|-------------|
| `list_projects` | When you don't know the project_id — lists all projects |
| `get_project_by_name` | Cold start by name — returns id + goal summary |
| `start_session` | Cold start shortcut — replaces steps 2-5 in one call |
| `register_session` | First thing, every session (or use start_session) |
| `get_goal` | After registering, and whenever you need the directive |
| `get_tasks` | After registering, to see recent work |
| `log_task` | Frequently — after every significant action |
| `set_goal` | Only on deliberate milestones, only if you're the project owner |
| `set_north_star` | Rarely — only the project owner, only on major pivots |
| `set_sprint` | Per-session or per-week focus update |
| `claim_task` | Before starting a task — prevents duplicate work |
| `release_task` | If you can't finish a claimed task |
| `generate_handoff` | Before ending any session |
| `heartbeat` | Every 5 min in long-running sessions |

## Key constants

```
PROJECT ID: 5787cc92-ba7d-4788-b17c-28ab7938b839
START CMD:  pixi run start
TESTS:      pixi run test (138+ must pass)
REPO:       C:\Users\13144\Documents\Meridian\repository
```

## Code standards

- Surgical edits only — never rewrite whole files
- dashboard.py is large — find/replace only
- pixi run test must pass before every commit
- Commit after every logical unit: `git commit -m "feat: vX.Y.Z — description"`


