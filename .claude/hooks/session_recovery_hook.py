#!/usr/bin/env python3
"""Keep provider recovery identity and agent lifecycle on the caller machine."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from meridian.session_recovery import (  # noqa: E402
    client_local_recovery_context,
    default_client_local_recovery_data_dir,
    prepare_client_local_registration,
    record_client_local_agent_lifecycle,
)


def _is_tool(name: Any, suffix: str) -> bool:
    return isinstance(name, str) and name.endswith("__" + suffix)


def handle_hook_payload(
    payload: dict[str, Any], *, data_dir: str | Path | None = None
) -> dict[str, Any] | None:
    """Return Claude hook JSON when action is needed; otherwise stay silent."""
    local_dir = data_dir or default_client_local_recovery_data_dir()
    event = payload.get("hook_event_name")
    tool_name = payload.get("tool_name")

    if event == "PreToolUse" and _is_tool(tool_name, "register_session_recovery"):
        tool_input = payload.get("tool_input")
        if not isinstance(tool_input, dict):
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": "Could not inspect recovery registration; local identity was not sent.",
                }
            }
        if tool_input.get("local_identity") in (None, {}):
            return None
        try:
            redacted = prepare_client_local_registration(
                local_dir,
                tool_input,
                provider_session_id=payload.get("session_id"),
            )
        except Exception:  # noqa: BLE001 -- fail closed before a hosted request
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": (
                        "Could not store recovery identity on this workstation. "
                        "The hosted registration was blocked before sending it."
                    ),
                }
            }
        if redacted is None:
            return None
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "updatedInput": redacted,
            }
        }

    if event == "PostToolUse" and _is_tool(tool_name, "get_session_recovery"):
        tool_input = payload.get("tool_input")
        expected_session_id = (
            tool_input.get("session_id") if isinstance(tool_input, dict) else None
        )
        active_provider_session_id = payload.get("session_id")
        if (
            not isinstance(expected_session_id, str)
            or not isinstance(active_provider_session_id, str)
            or not active_provider_session_id
        ):
            return None
        try:
            context = client_local_recovery_context(
                local_dir,
                payload.get("tool_response"),
                expected_session_id=expected_session_id,
                expected_provider_session_id=active_provider_session_id,
            )
        except (OSError, TypeError, ValueError):
            return None
        if context:
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": context,
                }
            }
        return None

    if event in {"SubagentStart", "SubagentStop"}:
        try:
            record_client_local_agent_lifecycle(local_dir, payload)
        except (OSError, TypeError, ValueError, TimeoutError):
            pass  # Lifecycle capture is best-effort and must not interrupt the agent.
    return None


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read())
        if not isinstance(payload, dict):
            return 0
        response = handle_hook_payload(payload)
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        return 0
    if response is not None:
        sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
