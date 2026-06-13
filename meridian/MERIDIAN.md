# Meridian Session Instructions

You are connected to Meridian — a shared project memory across AI coding sessions.
Multiple sessions (Claude Code tabs, Cursor, Windsurf, etc.) can be coordinating
on this project concurrently. Treat the Meridian state as the source of truth
for "what has been decided, claimed, and shipped."

## ALWAYS

- **`log_task` after every meaningful action** so parallel sessions can see your work.
  One line per task. Mark `status` honestly: `done`, `pending`, `failed`.
- **`set_decision`** for architectural, irreversible, or surprising choices.
  These appear in the decisions log (newest first) and persist across sessions.
- **`generate_handoff`** when context is filling up (~80k tokens) or before you stop.
  A clean handoff lets a fresh session resume without re-deriving everything.
- **`claim_task`** before starting work on something a parallel session might pick up.
  **`release_task`** if you bail — don't leave it claimed.
- **`claim_file(session_id, file_path)`** before editing shared files when parallel
  sessions are active. **`release_file(session_id, file_path)`** when done.
  Use **`idle_until_session_done(watching_session_id)`** to wait before taking
  over a file another session holds. Locks auto-expire after 2 hours.
- **After completing each sprint item**, call `get_sprint_items(status='pending')`
  to check for newly added items before moving to the next one. Sessions that only
  check pending items once at start miss anything added while running — re-checking
  at each item boundary picks up mid-run additions automatically.
- **`update_md_section`** proposes a replacement for an anchored CLAUDE.md/AGENTS.md
  section. Autonomous executor sessions leave it gated behind a dashboard HITL.
  Human planning sessions (claude.ai) may pass **`force=true`** to skip the HITL and
  apply the change directly — only use `force` when a human is driving the session.
- **Mid-run corrections**: after each `complete_sprint_item`, call
  `list_hitl_requests(status='pending')` and handle any request with
  `kind='correction'` before the next item: apply the correction, `log_task` it,
  and `answer_hitl(request_id, "acknowledged")`. Corrections never block an
  unattended run — fail open, log, and keep going. (Plain `kind='question'`
  requests stay blocking/auto-answerable as before.)

## ON SESSION START

Call `start_session(project_id, session_name)` once. That single call:
- registers this session
- returns the current goal (north star + version goal + sprint)
- returns the last 10 tasks (ambient context)
- returns the list of other active sessions
- tells you whether a handoff file exists from a prior session

If a handoff exists, read it before doing anything else.

## ON SESSION END

Before you stop, call `generate_handoff` so the next session can resume cleanly.
Ensure any tasks you logged as `pending` are either completed or released.

## DESIGN PRINCIPLES

- The version goal is **stable** — only humans (or you when explicitly directed)
  should change it. Auto-summaries and sprint changes do not bump it.
- The sprint changes per cycle; the north star changes rarely.
- Decisions are append-only — never edit prior entries. If a decision is reversed,
  log a new decision describing the reversal.
- Tasks are immutable once logged. If a task was wrong, log a corrective task.

## OVERRIDE

Drop a `MERIDIAN.md` file at your project root to replace these defaults.
The project-root file wins over Meridian's built-in.
