# CLAUDE.md — Meridian Project

<context>
Meridian is a production SaaS product at v2.3, live at usemeridian.us.
It is an open-source MCP server + dashboard that gives AI coding sessions
shared persistent memory, task coordination, and human-in-the-loop tooling.

This is NOT a demo or prototype. It is a real product with real infrastructure.
DO NOT scaffold, stub, or simplify. Write production-quality code only.

Repo: C:\Users\13144\Documents\Meridian\repository
Live: https://usemeridian.us
Project ID: 5787cc92-ba7d-4788-b17c-28ab7938b839
</context>

<rules>
ALWAYS:
- Run `pixi run test` before and after any change. Target: 350+ passing.
- Call `log_task` after every meaningful action via Meridian MCP.
- Call `set_decision` for any architectural or irreversible choice.
- Call `generate_handoff` before ending a session.
- Read dashboard.js + dashboard.css before touching any UI.
- Read db.py before touching any DB schema or queries.
- Read pg_adapter.py before writing any SQL — psycopg3 uses %s not ?.

NEVER:
- Use asyncpg — replaced by psycopg3 entirely.
- Use `? ` placeholders in SQL — use `%s` (adapter converts ? → %s automatically).
- Write literal `%` in SQL LIKE patterns — write `%%` (adapter handles quoted strings).
- Touch .env or meridian.toml — contain live credentials.
- Push to main directly — push to dev, merge to main to deploy.
- Use `asyncio.run()` on Windows — use `uvicorn.Server` + `loop.run_until_complete()`.
- Import watchfiles on Windows — deadlocks ProactorEventLoop.
</rules>

<architecture>
STACK:
- Python 3.12, FastAPI, psycopg3 (Postgres) + aiosqlite (SQLite fallback)
- Uvicorn with SelectorEventLoop on Windows (see __main__.py)
- pixi for env management, pytest for tests
- Fly.io hosting, Neon Postgres, Resend email, Stripe billing

KEY FILES (read before editing):
  meridian/server.py       — FastAPI app, lifespan, ALL routes, MCP tools
  meridian/db.py           — ALL DB operations, SQLite + Postgres compatible
  meridian/pg_adapter.py   — psycopg3 adapter (recently rewritten, careful)
  meridian/hosted.py       — Auth (Google/GitHub OAuth), Stripe, Neon provisioning
  meridian/goal_md.py      — GOAL.md bidirectional sync (skip on win32)
  meridian/__main__.py     — Entry point, Windows SelectorEventLoop fix
  meridian/static/dashboard.js  — ALL frontend, single file
  meridian/static/dashboard.css — CSS variables, component styles
  meridian/MERIDIAN.md     — Auto-injected session instructions

PSYCOPG3 RULES (non-negotiable):
  - Pool: `async with self._pool.connection() as conn:`
  - Cursor: `async with conn.cursor() as cur:`
  - Execute: `await cur.execute(sql, params or None)`
  - Fetch: `rows = await cur.fetchall()` → list of dicts
  - autocommit=True — never call conn.commit()
  - LIKE patterns: `%%` not `%` for literal percent

DB SCHEMA (key tables):
  projects       — id, name, decisions (TEXT blob), rewind_token
  goal_states    — id, project_id, content, north_star, sprint, version,
                   ns_updated_at, content_updated_at, sprint_updated_at
  sessions       — id, project_id, name, human_id, status, last_seen
  task_log       — id, session_id, project_id, description, status, created_at
  sprint_items   — id, project_id, version, title, status, item_group
  tenants        — id, email, neon_project_id, neon_db_url, stripe_customer_id, plan
</architecture>

<current_state>
<!-- Auto-updated by Meridian. Do not edit manually. -->
</current_state>
