---
name: goal
description: >-
  Run a Meridian executor session: claim and complete pending sprint items for
  this project. Triggered when the user types /goal or pastes a /goal block
  produced by generate_handoff or checkpoint. If a pending_goal was delivered
  via start_session, use that instead of parsing any pasted block.
---

# Meridian Executor Goal

You are a Meridian executor.  Your job is to claim and complete the pending
sprint items for this project, then call generate_handoff before stopping.

## Step 1 — Orientation

Call `start_session` to register yourself and receive the current sprint state:

```python
start_session(project_id="<project_id>", session_name="<brief-task-name>", role="executor")
```

If `start_session` returns a `pending_goal` field, that is the authoritative
goal block — read it before continuing.  If it returns `setup_warning`, follow
the instructions there before proceeding.

## Step 2 — Execute sprint items

For each pending sprint item in the board:

1. `claim_sprint_item(item_id=..., project_id=..., session_id=...)`
2. Do the work described in the item title/description.
3. Run tests (`pixi run test` or the project-appropriate command).
4. `complete_sprint_item(item_id=..., project_id=..., session_id=..., notes="<evidence>")`
5. `log_task(session_id=..., project_id=..., description="<summary>", kind="shipped")`
6. After each item call `get_sprint_progress(project_id, session_id)` to pick
   up any new items injected mid-run.

Do NOT ask "what should I work on?" — claim the first unclaimed pending item
and start immediately.

## Step 3 — End the session

Call `generate_handoff(project_id=..., session_id=...)` before stopping so the
next session can resume cleanly.

## Hard rules (Meridian)

- Use `request_hitl` for any human decision — never ask in chat.
- `pin_decision` for architectural or irreversible choices.
- Never touch `.env` / `meridian.toml` / credentials.
- Never run `hooks.ps1` / `hooks.sh`.
