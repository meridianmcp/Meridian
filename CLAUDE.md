# CLAUDE.md â€” Meridian Project Context

## What is Meridian

Meridian is a local Python MCP server that gives multiple Claude sessions
a shared persistent brain. Client-agnostic â€” works with Claude Desktop,
Claude Code, Cursor, Windsurf, or any MCP-compatible client on any OS.

It solves three problems:

1. Claude sessions are completely isolated â€” no shared state between tabs
2. Context fills up and dies â€” you lose everything mid-project
3. Running parallel sessions means manually syncing what each one knows

Meridian fixes all three via a local server every session connects to.
Sessions share goal state, see each other's task logs, and generate
compressed handoff files so new sessions resume with full context instantly.

## Long-term product vision

Free tier: local MCP server, MSL-1.0 licensed, personal use only.
Paid tier: advanced MCP coordination tools + browser GUI dashboard.
This is a real product being built for commercial release, not a demo.

Paid tier MCP tools (NOT in this build â€” write as ROADMAP.md only):
- enqueue_claude_task: shells out to Claude Code async, non-blocking,
  result written to task_log when done. Solves subprocess timeout problem.
- watch_goal: subscribe to goal state changes, notified on update
- broadcast: send message to all active sessions simultaneously
- diff_goal: semantic diff between goal state versions
- claim_task / release_task: distributed task locking, no conflicts
- session_health: score session drift from goal state
These are paid server-side features. Implementation never ships in free tier.

## Current repo state

- LICENSE written (MSL-1.0, custom)
- README.md written (minimal)
- ROADMAP.md â€” YOU WRITE THIS (one page, free vs paid tier features)
- pixi.toml â€” NOT YET WRITTEN
- Source code â€” NOT YET WRITTEN
- Tests â€” NOT YET WRITTEN
- Podman â€” NOT YET WRITTEN

## Your job

Build Meridian completely. Every file. Production quality.
Run pixi install to verify environment resolves. Make it actually run.
Write ROADMAP.md documenting paid tier features as future work.

---

## Stack â€” non-negotiable

- Python 3.11+
- pixi for environment management (NOT pip, NOT conda directly)
- FastAPI for HTTP layer
- aiosqlite for async SQLite
- Pydantic v2 for all models
- Jinja2 for handoff template
- mcp Python SDK for MCP server
- pytest + pytest-asyncio for tests
- Single SQLite file at ./data/meridian.db (auto-created on start)
- NO Postgres, NO Redis, NO external services required
- NO Podman required to run â€” pixi run start is enough
- Podman is optional convenience layer on top

---

## File structure â€” build exactly this

```
meridian/
â”œâ”€â”€ LICENSE                     (exists â€” do not touch)
â”œâ”€â”€ README.md                   (exists â€” do not touch)
â”œâ”€â”€ CLAUDE.md                   (this file â€” do not touch)
â”œâ”€â”€ ROADMAP.md                  (YOU WRITE THIS)
â”œâ”€â”€ pixi.toml                   (YOU WRITE THIS)
â”œâ”€â”€ Dockerfile                  (YOU WRITE THIS)
â”œâ”€â”€ podman-compose.yml          (YOU WRITE THIS)
â”œâ”€â”€ meridian/
â”‚   â”œâ”€â”€ __init__.py             (YOU WRITE THIS)
â”‚   â”œâ”€â”€ __main__.py             (YOU WRITE THIS)
â”‚   â”œâ”€â”€ server.py               (YOU WRITE THIS â€” main file)
â”‚   â”œâ”€â”€ db.py                   (YOU WRITE THIS)
â”‚   â”œâ”€â”€ models.py               (YOU WRITE THIS)
â”‚   â”œâ”€â”€ handoff.py              (YOU WRITE THIS)
â”‚   â””â”€â”€ templates/
â”‚       â””â”€â”€ handoff.md.j2       (YOU WRITE THIS)
â”œâ”€â”€ scripts/
â”‚   â”œâ”€â”€ demo.py                 (YOU WRITE THIS)
â”‚   â””â”€â”€ test_mcp.py             (YOU WRITE THIS)
â””â”€â”€ tests/
    â”œâ”€â”€ conftest.py             (YOU WRITE THIS)
    â””â”€â”€ test_core.py            (YOU WRITE THIS)
```

---

## pixi.toml

```toml
[project]
name = "meridian"
version = "0.1.0"
description = "Multi-session Claude coordinator MCP server"
channels = ["conda-forge"]
platforms = ["win-64", "linux-64", "osx-arm64", "osx-64"]

[dependencies]
python = ">=3.11,<3.13"
fastapi = ">=0.115"
uvicorn = ">=0.30"
aiosqlite = ">=0.20"
pydantic = ">=2.0,<3"
jinja2 = ">=3.1"
httpx = ">=0.27"
pytest = ">=8.0"
pytest-asyncio = ">=0.23"

[pypi-dependencies]
mcp = ">=1.0"

[tasks]
start = "python -m meridian"
dev = "uvicorn meridian.server:app --reload --port 7878"
test = "pytest tests/ -v"
demo = "python scripts/demo.py"
test-mcp = "python scripts/test_mcp.py"
```

---

## Database schema â€” db.py

Four tables. Write all DDL in db.py as a single CREATE_TABLES string.
Run on startup via lifespan. Use aiosqlite throughout, async everywhere.

```sql
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS goal_states (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    content TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active','idle','closed')),
    last_seen TEXT NOT NULL DEFAULT (datetime('now')),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS task_log (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    project_id TEXT NOT NULL REFERENCES projects(id),
    description TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'done'
        CHECK (status IN ('pending','done','failed')),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_goal_project
    ON goal_states(project_id);
CREATE INDEX IF NOT EXISTS idx_sessions_project
    ON sessions(project_id, status);
CREATE INDEX IF NOT EXISTS idx_tasks_project
    ON task_log(project_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tasks_session
    ON task_log(session_id);
```

db.py must expose these async functions:

- `init_db(db_path)` â€” create tables, return connection pool
- `create_project(db, name)` â†’ project dict
- `get_project(db, project_id)` â†’ project dict or None
- `get_project_by_name(db, name)` â†’ project dict or None
- `list_projects(db)` â†’ list of project dicts
- `get_goal(db, project_id)` â†’ goal_state dict or None
- `set_goal(db, project_id, content)` â†’ goal_state dict (upsert, increment version)
- `register_session(db, project_id, name)` â†’ session dict
- `update_session_seen(db, session_id)` â†’ None
- `close_session(db, session_id)` â†’ None
- `get_sessions(db, project_id, active_only=True)` â†’ list
- `log_task(db, session_id, project_id, description, status)` â†’ task dict
- `get_tasks(db, project_id, limit=20)` â†’ list (newest first)

All IDs are uuid4 strings. All timestamps are ISO format strings.

---

## models.py â€” Pydantic v2

```python
class ProjectCreate(BaseModel):
    name: str

class GoalSet(BaseModel):
    content: dict | str  # flexible â€” JSON object or plain text

class SessionRegister(BaseModel):
    project_id: str
    name: str

class TaskCreate(BaseModel):
    session_id: str
    project_id: str
    description: str
    status: Literal['pending', 'done', 'failed'] = 'done'
```

Full response models for Project, GoalState, Session, Task with all fields.

---

## server.py â€” FastAPI + MCP

Use FastAPI lifespan to:
1. Create ./data/ directory if not exists
2. Init SQLite DB
3. Store db connection on app.state

### FastAPI endpoints

All async. 404 when not found. 422 on validation errors.

```
GET  /health
GET  /projects
POST /projects                    body: ProjectCreate
GET  /projects/{project_id}
GET  /projects/{project_id}/goal
POST /projects/{project_id}/goal  body: GoalSet
GET  /projects/{project_id}/sessions
GET  /projects/{project_id}/tasks?limit=20
POST /projects/{project_id}/handoff
POST /sessions/register           body: SessionRegister
POST /sessions/{session_id}/close
POST /tasks                       body: TaskCreate
```

### MCP server â€” eight tools

Server name: "meridian". Write full tool descriptions â€” they matter for
Claude to use the tools correctly without prompting.

1. `create_project(name)` â†’ project id + name
   "Create a new Meridian project to coordinate sessions around."

2. `register_session(project_id, session_name)` â†’ session_id
   "Register this Claude session with a project. Call at the START of
   every session before using any other tools. Store the returned
   session_id â€” you need it for log_task."

3. `get_goal(project_id)` â†’ content + version
   "Read the current goal state for a project. This is the shared
   directive all sessions work toward. Read this after registering."

4. `set_goal(project_id, content)` â†’ updated goal state
   "Set or update the goal state. All sessions see this immediately.
   Version increments on each update."

5. `log_task(session_id, project_id, description, status)` â†’ task
   "Log what this session just did, is doing, or failed at. Call
   frequently to keep all sessions informed of progress."

6. `get_tasks(project_id, limit=20)` â†’ task list
   "Get recent tasks across all sessions. Shows what everyone has done."

7. `get_sessions(project_id)` â†’ session list
   "List all active sessions connected to this project."

8. `generate_handoff(project_id)` â†’ file_path + content
   "Generate a context handoff file. Call when context is filling up
   or before ending a session. A new session can read this file to
   resume with full context. Returns file path and rendered content."

### Running both together

In `__main__.py`:
- `--mcp` flag: run MCP server via stdio (for Claude Desktop / Code)
- Default: run FastAPI via uvicorn on port 7878 (for local dev + demo)

Both modes share the same db.py functions.

---

## handoff.py

```python
async def generate_handoff(db, project_id: str, output_dir: str) -> tuple[str, str]:
    """Fetch all state, render Jinja2 template, write file, return (path, content)."""
```

---

## templates/handoff.md.j2

```
---
MERIDIAN_CONTEXT
Generated: {{ generated_at }}
Project: {{ project.name }} ({{ project.id }})
---

## Goal State (v{{ goal.version }})

{{ goal.content }}

## Active Sessions ({{ sessions | length }})

{% for s in sessions %}
- {{ s.name }} â€” {{ s.status }} â€” last seen {{ s.last_seen }}
{% endfor %}

## Recent Task Log (last {{ tasks | length }} entries)

{% for t in tasks %}
[{{ t.created_at }}] [{{ t.status | upper }}] {{ session_names[t.session_id] }}: {{ t.description }}
{% endfor %}

## Resume Instructions

You are resuming a Meridian-coordinated session for "{{ project.name }}".

1. Read Goal State above â€” this is your primary directive
2. Read Task Log â€” this shows what has already been done
3. Call register_session(project_id="{{ project.id }}", session_name="<your-name>")
4. Call get_goal(project_id="{{ project.id }}") to confirm latest version
5. Continue from where the task log ends

Do not redo work already marked done in the task log.
```

---

## scripts/demo.py

Simulates two sessions via httpx against live FastAPI on port 7878.
Print clearly labeled output at each step so it reads as a story.

Flow:
1. Create project "demo-project"
2. Register "session-alpha"
3. Set goal: "Build a Python web scraper with async support"
4. session-alpha logs: "Set up project structure" (done)
5. session-alpha logs: "Wrote async HTTP client" (done)
6. Register "session-beta"
7. session-beta reads goal state
8. session-beta reads task log â€” sees session-alpha's work
9. session-beta logs: "Reviewing session-alpha's HTTP client" (done)
10. Generate handoff file
11. Print full handoff content
12. Print "Demo complete. Two sessions coordinated successfully."

---

## scripts/test_mcp.py

Connect to running Meridian MCP server via stdio using mcp Python SDK.
Test each tool in sequence. Print PASS/FAIL per tool.
Include usage instructions at top of file.

---

## tests/conftest.py

- `db` fixture: in-memory aiosqlite DB with schema applied, async
- `client` fixture: FastAPI TestClient with in-memory DB injected

## tests/test_core.py

Minimum 15 tests covering:
- All db.py functions
- All FastAPI endpoints (happy path + 404 cases)
- Handoff generation produces valid markdown with correct sections
- Session registration and task logging round-trip
- Goal state versioning increments correctly
- get_tasks returns newest first

---

## Dockerfile

```Dockerfile
FROM ghcr.io/prefix-dev/pixi:latest
WORKDIR /app
COPY pixi.toml pixi.lock* ./
RUN pixi install
COPY . .
EXPOSE 7878
CMD ["pixi", "run", "start"]
```

## podman-compose.yml

```yaml
version: "3.9"
services:
  meridian:
    build: .
    ports:
      - "7878:7878"
    volumes:
      - ./data:/app/data
    restart: unless-stopped
```

---

## Claude Desktop / Code MCP config

Include this in README.md and as a comment in __main__.py:

```json
{
  "mcpServers": {
    "meridian": {
      "command": "pixi",
      "args": ["run", "python", "-m", "meridian", "--mcp"],
      "cwd": "/absolute/path/to/meridian/repository"
    }
  }
}
```

---

## Quality bar

- Every function has a docstring
- Async throughout â€” zero blocking calls in async functions
- All errors caught, meaningful messages returned
- No bare except clauses
- IDs always uuid4 strings
- Timestamps always ISO format strings
- demo.py runs cleanly against a live server
- `pixi run test` passes with zero failures
- `pixi run start` starts without errors

---

## Definition of done

1. `pixi install` completes without errors
2. `pixi run start` starts FastAPI on port 7878
3. `pixi run demo` prints the full two-session story + handoff content
4. `pixi run test` passes all tests
5. data/demo-project_handoff.md exists and is clean readable markdown
6. A new Claude session reading that file knows exactly what happened
   and what to do next without any additional explanation

Do not stop until all six are true.

---
<!-- MERIDIAN STATE — auto-generated, do not edit below -->
## Current Sprint State  _(auto-updated 2026-05-21 21:06 UTC)_

**Key Files:**
- `meridian/server.py` — FastAPI app + MCP handlers
- `meridian/db.py` — all DB functions (SQLite + Postgres)
- `meridian/static/dashboard.js` — dashboard UI
- `tests/test_core.py` — full test suite
- `data/meridian-build_handoff.md` — session handoff
