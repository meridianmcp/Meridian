# CONTRIBUTING

## Before you contribute

Read OWNERSHIP.md. By submitting a pull request or commit to this repository
you acknowledge that:

1. Your contribution is your original work.
2. You grant joint ownership of your contribution to the existing project owners
   as described in OWNERSHIP.md.
3. You have read and agree to the LICENSE (MSL-1.0) — no commercial use without
   written permission from Adam Camerer.

Add yourself to CONTRIBUTORS.md in your first PR.

## Session startup protocol

Every Claude Code session working on this project MUST:

1. Run `pixi run start` to ensure Meridian is running at localhost:7878
2. Call `register_session(project_id, session_name, human_id="yourname")`
3. Call `get_goal(project_id)` — read before touching any code
4. Call `log_task` frequently — keep other sessions informed
5. Call `generate_handoff` before ending if context is filling up

**Project ID:** `5787cc92-ba7d-4788-b17c-28ab7938b839`

## Code standards

- **Surgical edits only** — never rewrite whole files
- `pixi run test` must pass (all tests green) before every commit
- `dashboard.py` is large — find/replace only, never read and rewrite the whole file
- Commit after every logical unit: `git commit -m "feat: vX.Y.Z — description"`

## Pull request process

1. Open a PR against `main`
2. All tests must pass
3. At least one review from Adam Camerer required to merge
4. Reference the relevant task log entry from Meridian in the PR description

## What goes where

| File | Purpose |
|------|---------|
| `meridian/db.py` | SQLite schema and async database functions |
| `meridian/server.py` | FastAPI REST endpoints + MCP tool handlers |
| `meridian/dashboard.py` | Single-file dashboard HTML + JS + SSE chat |
| `meridian/models.py` | Pydantic v2 request/response models |
| `meridian/enqueue.py` | Async worker subprocess lifecycle |
| `meridian/handoff.py` | Handoff file generation |
| `tests/test_core.py` | Full test suite |
| `ROADMAP.md` | Version plan — check before starting new work |
| `DEVLOG.md` | Incident log — read before debugging |

## Running tests

```bash
pixi run test
```

All tests must pass before committing. No exceptions.
