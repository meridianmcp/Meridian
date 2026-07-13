"""3617361d — post-compaction context / tool-list refresh routine.

The server-side planner nudge (bf51b12e) is *turn-count* based: it attaches a
compact context-refresh to a planner tool result every N tool calls. That works
for gradual context drift, but it is structurally blind to a **compaction event**
— ``/compact`` is a purely client-side action inside the Claude Code / Codex
harness that never touches the Meridian server, so no server-side counter can
observe it. After a compaction the transcript is summarised and the live
tool-manifest / sprint-goal framing can be lost, yet the turn counter may have
just reset or be nowhere near its threshold.

The established external pattern is a Claude Code ``SessionStart`` hook that
matches ``source == "compact"`` and re-injects orientation context
(cf. github.com/Dicklesworthstone/post_compact_reminder). This module is the
canonical, unit-testable core of that hook: given the JSON payload Claude Code
writes to the hook's stdin, it decides whether this is a post-compaction start
and, if so, produces the ``hookSpecificOutput.additionalContext`` reminder that
tells the resumed session to re-orient — call ``refresh_context`` /
``refresh_tool_manifest`` and re-read the sprint goal.

The ``.claude/hooks/post_compact_refresh.{sh,ps1}`` wrappers mirror this exact
logic in pure shell / PowerShell so the hook runs even where a Python
interpreter for this package is not on PATH; keeping the decision here lets the
behaviour be tested directly.

Design notes:
- **Fails open, always.** A SessionStart hook must never wedge a session. Any
  malformed / missing payload yields a no-op (empty ``additionalContext``), never
  an exception that Claude Code would surface as a hook error.
- **Only ``source == "compact"`` fires.** ``startup`` / ``resume`` / ``clear``
  are handled by the existing ``meridian-start`` hook and must stay untouched, so
  this hook is a no-op for them (returns the empty-context envelope).
- **Idempotent & stateless.** No files written, no network calls; the same
  payload always yields the same output.
"""

from __future__ import annotations

import json
from typing import Any

# The one SessionStart source we react to. Claude Code emits this when the
# session is (re)started as a result of a /compact (auto- or manual).
_COMPACT_SOURCE = "compact"

# The reminder injected into the resumed session's context. Kept terse and
# action-oriented — it mirrors the tools named by the server-side nudge
# (bf51b12e / d8bd59c4 refresh_context) plus the tool-manifest re-list guidance
# from AGENTS.md ("Tool not found" != "tool doesn't exist", b74099b2).
_REFRESH_REMINDER = (
    "[Meridian] Context was just compacted. Before continuing, RE-ORIENT:\n"
    "1. Call refresh_context(project_name=...) for a compact snapshot of the "
    "sprint, progress, active session id, recent handoffs, and key decisions.\n"
    "2. Call refresh_tool_manifest (re-issue tools/list) so the tool list is "
    "current after the compaction — a mid-session deploy may have added or "
    "renamed tools, and the pre-compaction manifest can be stale.\n"
    "3. Re-read the sprint goal / next pending item so you resume the SAME work "
    "instead of re-deriving it. Do not treat the summarized transcript as the "
    "full plan."
)


def _empty_output() -> dict[str, Any]:
    """The no-op SessionStart envelope (adds nothing to context)."""
    return {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": "",
        }
    }


def build_compact_refresh(payload: Any) -> dict[str, Any]:
    """Return the SessionStart hook output for a (possibly-compaction) payload.

    ``payload`` is the parsed JSON object Claude Code writes to the hook's
    stdin. When ``payload["source"] == "compact"`` the returned envelope carries
    the re-orientation reminder in ``additionalContext``; for every other source
    (or any malformed input) it returns the empty no-op envelope.

    This function never raises — a SessionStart hook must fail open.
    """
    try:
        source = payload.get("source") if isinstance(payload, dict) else None
    except Exception:  # pragma: no cover - defensive, dict.get shouldn't raise
        source = None
    if source != _COMPACT_SOURCE:
        return _empty_output()
    return {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": _REFRESH_REMINDER,
        }
    }


def run(raw_stdin: str) -> str:
    """Parse raw hook stdin and return the JSON string to print on stdout.

    Fails open: unparseable stdin yields the empty no-op envelope.
    """
    try:
        payload = json.loads(raw_stdin) if raw_stdin.strip() else {}
    except (ValueError, TypeError):
        payload = {}
    return json.dumps(build_compact_refresh(payload))


def main() -> int:
    """CLI entry point: read stdin, emit the hook envelope on stdout.

    Always exits 0 (fail open) so the hook can never block a session start.
    """
    import sys

    try:
        raw = sys.stdin.read()
    except Exception:
        raw = ""
    try:
        sys.stdout.write(run(raw))
    except Exception:
        # Last-resort fail-open: emit a minimal valid envelope.
        sys.stdout.write(json.dumps(_empty_output()))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess
    raise SystemExit(main())
