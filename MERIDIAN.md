# Meridian Session Instructions

This project uses Meridian as the shared source of truth for session state,
sprint work, task logs, and handoffs.

## Session Start

- Call `start_session(project_id, session_name)` once before working.
- Read the returned sprint items, active sessions, decisions, and handoff context.

## Executor Sprint Work

- Before starting each sprint item, call `claim_sprint_item(project_id, item_id)`.
- Work only on the claimed item until it is ready to mark complete, failed,
  skipped, or pushed.
- After meaningful progress, call `log_task(session_id, project_id, description)`.
- When the item is shipped, call `complete_sprint_item(project_id, item_id)`.

## Session End

- Call `checkpoint(session_id, project_id)` before stopping.
- Call `generate_handoff(project_id, mode="delta")` so the next session can
  resume from the latest state.
