"""G4.15 — Safety limits to protect shared infrastructure from runaway loops
and abusive load. The numbers are intentionally far above any honest
single-team workload; they're guard-rails, not quotas.

Every threshold can be overridden by environment variable so a self-hosted
operator can dial them up (or down) without a code change.

A single :class:`LimitExceeded` exception maps to a 429 response with a
fixed user-facing message that names the limit, points to docs, and gives
a contact for legitimate raise requests.
"""
from __future__ import annotations

import os
from typing import Final

# ---------------------------------------------------------------------------
# Thresholds — read once at import; tests that need to override should monkey-
# patch these module attributes (or set the env var before importing).
# ---------------------------------------------------------------------------


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        val = int(raw)
        return val if val > 0 else default
    except ValueError:
        return default


PROJECTS_PER_TENANT: int = _int_env("MERIDIAN_LIMIT_PROJECTS_PER_TENANT", 1_000)
SPRINT_ITEMS_PER_PROJECT: int = _int_env("MERIDIAN_LIMIT_SPRINT_ITEMS_PER_PROJECT", 50_000)
NOTES_PER_PROJECT: int = _int_env("MERIDIAN_LIMIT_NOTES_PER_PROJECT", 100_000)
DECISIONS_PER_PROJECT: int = _int_env("MERIDIAN_LIMIT_DECISIONS_PER_PROJECT", 10_000)
SESSIONS_PER_PROJECT: int = _int_env("MERIDIAN_LIMIT_SESSIONS_PER_PROJECT", 100_000)
TASKS_PER_PROJECT: int = _int_env("MERIDIAN_LIMIT_TASKS_PER_PROJECT", 1_000_000)
OPEN_HITL_PER_PROJECT: int = _int_env("MERIDIAN_LIMIT_OPEN_HITL_PER_PROJECT", 1_000)
BODY_BYTES: int = _int_env("MERIDIAN_LIMIT_BODY_BYTES", 100_000)

# Canonical 429 message — quoted verbatim so the dashboard / SDK can match.
LIMIT_429_MESSAGE: Final[str] = (
    "Safety limit reached — protects shared infra. "
    "Legit need? hello@usemeridian.us · "
    "See docs.usemeridian.us/safety-limits"
)


class LimitExceeded(Exception):
    """Raised when a create-side guard trips. Carries enough context for the
    HTTP layer to render a useful 429 message."""

    def __init__(self, kind: str, limit: int, current: int | None = None):
        self.kind = kind
        self.limit = limit
        self.current = current
        detail = f"{LIMIT_429_MESSAGE} (limit: {kind}={limit})"
        super().__init__(detail)


def _current_limit(name: str) -> int:
    """Look up a limit by name at call time. Tests can monkeypatch the module
    attribute and the check will pick up the new value without re-importing."""
    import sys
    mod = sys.modules[__name__]
    return getattr(mod, name)


def check_projects_per_tenant(current: int) -> None:
    limit = _current_limit("PROJECTS_PER_TENANT")
    if current >= limit:
        raise LimitExceeded("projects_per_tenant", limit, current)


def check_sprint_items_per_project(current: int) -> None:
    limit = _current_limit("SPRINT_ITEMS_PER_PROJECT")
    if current >= limit:
        raise LimitExceeded("sprint_items_per_project", limit, current)


def check_notes_per_project(current: int) -> None:
    limit = _current_limit("NOTES_PER_PROJECT")
    if current >= limit:
        raise LimitExceeded("notes_per_project", limit, current)


def check_decisions_per_project(current: int) -> None:
    limit = _current_limit("DECISIONS_PER_PROJECT")
    if current >= limit:
        raise LimitExceeded("decisions_per_project", limit, current)


def check_sessions_per_project(current: int) -> None:
    limit = _current_limit("SESSIONS_PER_PROJECT")
    if current >= limit:
        raise LimitExceeded("sessions_per_project", limit, current)


def check_tasks_per_project(current: int) -> None:
    limit = _current_limit("TASKS_PER_PROJECT")
    if current >= limit:
        raise LimitExceeded("tasks_per_project", limit, current)


def check_open_hitl_per_project(current: int) -> None:
    limit = _current_limit("OPEN_HITL_PER_PROJECT")
    if current >= limit:
        raise LimitExceeded("open_hitl_per_project", limit, current)


def check_body_bytes(size: int) -> None:
    limit = _current_limit("BODY_BYTES")
    if size > limit:
        raise LimitExceeded("body_bytes", limit, size)
