"""Default agent_instructions injected into every new project.

This text is set automatically on project creation and is returned by
``start_session`` so every AI coding session picks up the coordination rules
without any per-repo file configuration.

Users can view / edit / reset this in the dashboard:
  Project → Settings → Executor Rules
"""

DEFAULT_AGENT_INSTRUCTIONS = """\
# Meridian — executor rules

These rules are injected automatically via `start_session`. Edit or reset them
in the Meridian dashboard → Settings → Executor Rules.

## Session start
- Call `start_session(project_id=..., session_name=...)` **first**, every session.
- If the response lists pending sprint items, immediately call `claim_sprint_item`
  on the first unclaimed one and start working. Do NOT ask "what would you like
  to work on?" when items are waiting.

## File & symbol ownership
- Before editing a shared file, call `claim_file(session_id, file_path)` and
  check the `file_warnings` field from `start_session`. If another session already
  holds the file, coordinate before editing.
- For symbol-level edits (one class or function inside a large file), use
  `claim_symbol(session_id, file_path, symbol, content)` — two sessions can safely
  work in the same file when they own different symbols.
- To check active locks before starting parallel work, call
  `get_file_claims(file_path)`.

## Sprint board discipline
- `complete_sprint_item(item_id, project_id)` is **mandatory** for every sprint
  item you finish — call it before `checkpoint`. The board never auto-reconciles
  from git; skipping this drifts the board and breaks handoffs.
- After completing each item, call `get_sprint_progress(project_id, session_id)`
  (pass `session_id`) before claiming the next — the `board_change` field surfaces
  items injected mid-run. Never call `get_sprint_items` just to check for new work.
- When `claim_sprint_item()` returns `worktree_suggested: true`, use the provided
  worktree commands to isolate your work from parallel sessions.

## During work
- Call `log_task(session_id, project_id, description)` after every meaningful action.
- Call `pin_decision(project_id, title, body, category)` for architectural choices.
- Call `request_hitl(project_id, question)` when blocked on a human decision before
  continuing — do not guess; pause and ask.

## Before ending
- Call `checkpoint(session_id, project_id)` before context fills and before ending
  the session. It snapshots progress, generates a delta handoff, and returns the
  next `/goal` string.

## Secrets hygiene
- Never put credentials, connection strings, or API keys in chat messages, task
  descriptions, or committed files — reference env var names only.

## Deploy gate
- Never fire production deployments without first calling `request_hitl` and
  receiving explicit human approval.

## Code intelligence (if available)
- If `trace_path` appears in your MCP tool list, use it before opening any file.
- Prefer structural graph queries (`trace_path`, `search_graph`, `get_architecture`,
  `detect_changes`) over raw file reads — they are faster and use far fewer tokens.
- Fall back to reading files only when a graph query is insufficient.
- If `trace_path` is not in your tool list, ignore this section.
"""
