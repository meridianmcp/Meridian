"""meridian/a2a.py — Google A2A (Agent-to-Agent) protocol support.

Implements the minimal viable A2A protocol:
- Agent card served at /.well-known/agent.json
- Task reception (POST /a2a/{agent_id}/tasks/send)
- Task status polling (GET /a2a/{agent_id}/tasks/{task_id})

Reference: https://google.github.io/A2A/

This module contains the data models and the agent card builder.
The HTTP routes live in meridian/routes/a2a.py.
"""
from __future__ import annotations

import os
from typing import Any


# ---------------------------------------------------------------------------
# A2A Agent Card
# The agent card describes this agent's identity and capabilities.
# It is served at /.well-known/agent.json per the A2A spec.
# ---------------------------------------------------------------------------

def build_agent_card(base_url: str | None = None) -> dict[str, Any]:
    """Return the A2A agent card for the Meridian coordinator agent.

    ``base_url`` is the public URL of the server (e.g. https://usemeridian.us).
    Falls back to the MERIDIAN_BASE_URL environment variable, then a localhost
    default so the card is always valid.
    """
    url = (
        base_url
        or os.environ.get("MERIDIAN_BASE_URL", "")
        or "http://localhost:7878"
    ).rstrip("/")

    return {
        "schema_version": "1.0",
        "name": "Meridian Coordinator",
        "description": (
            "Meridian is an MCP coordination server that gives AI coding sessions "
            "shared persistent memory, task coordination, and human-in-the-loop tooling. "
            "Send tasks to coordinate multi-agent workflows, delegate sprint items, "
            "or request human review."
        ),
        "url": url,
        "version": _meridian_version(),
        "capabilities": {
            "streaming": False,
            "push_notifications": False,
            "state_transition_history": True,
        },
        "default_input_modes": ["application/json"],
        "default_output_modes": ["application/json"],
        "skills": [
            {
                "id": "task_coordination",
                "name": "Task Coordination",
                "description": (
                    "Create and track sprint items, log tasks, and coordinate "
                    "multi-agent work across a shared project board."
                ),
                "input_modes": ["application/json"],
                "output_modes": ["application/json"],
                "examples": [
                    "Add a sprint item to decompose a goal",
                    "Log that a task was completed",
                    "Request human review for a blocking question",
                ],
            },
            {
                "id": "human_in_the_loop",
                "name": "Human-in-the-Loop",
                "description": (
                    "Surface questions to a human reviewer and wait for approval "
                    "before proceeding with irreversible actions."
                ),
                "input_modes": ["application/json"],
                "output_modes": ["application/json"],
            },
        ],
        "endpoints": {
            "tasks_send": f"{url}/a2a/{{agent_id}}/tasks/send",
            "tasks_get": f"{url}/a2a/{{agent_id}}/tasks/{{task_id}}",
        },
    }


def _meridian_version() -> str:
    """Return the Meridian package version, or 'unknown' if not installed."""
    try:
        from importlib.metadata import version  # noqa: PLC0415
        return version("meridian-server")
    except Exception:
        pass
    try:
        from importlib.metadata import version  # noqa: PLC0415
        return version("meridian")
    except Exception:
        return "unknown"


# ---------------------------------------------------------------------------
# A2A Task data model helpers
# ---------------------------------------------------------------------------

_VALID_STATUSES = frozenset(
    {"submitted", "working", "completed", "failed", "canceled"}
)


def task_to_a2a(task: dict[str, Any]) -> dict[str, Any]:
    """Convert an agent_tasks DB row to the A2A task response envelope."""
    return {
        "task_id": task["id"],
        "agent_id": task.get("agent_id"),
        "status": {
            "state": task.get("status", "submitted"),
        },
        "artifacts": _artifacts_from_output(task.get("output")),
        "metadata": task.get("metadata") or {},
        "created_at": task.get("created_at"),
        "updated_at": task.get("updated_at"),
    }


def _artifacts_from_output(output: Any) -> list[dict[str, Any]]:
    """Wrap the task output as an A2A artifact list."""
    if output is None:
        return []
    if isinstance(output, dict):
        return [{"type": "application/json", "data": output}]
    return [{"type": "text/plain", "data": str(output)}]
