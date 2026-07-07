"""Coverage-raising tests for meridian.limits (G4.15 safety limits).

Pure module — no FastAPI/DB. We exercise:
  * `_int_env` parsing: valid positive override, non-positive fallback,
    blank fallback, and ValueError fallback.
  * every `check_*` guard's trip branch (>= limit / > limit) and its
    pass-through (below limit) path.
  * `LimitExceeded` context attributes + the canonical 429 message.

Guards read their threshold at call time via `_current_limit`, so we
monkeypatch the module attribute rather than re-import.
"""
from __future__ import annotations

import pytest

from meridian import limits


# ---------------------------------------------------------------------------
# _int_env — lines 27-31
# ---------------------------------------------------------------------------


def test_int_env_blank_returns_default(monkeypatch):
    monkeypatch.delenv("MERIDIAN_TEST_LIMIT", raising=False)
    assert limits._int_env("MERIDIAN_TEST_LIMIT", 42) == 42


def test_int_env_whitespace_only_returns_default(monkeypatch):
    monkeypatch.setenv("MERIDIAN_TEST_LIMIT", "   ")
    assert limits._int_env("MERIDIAN_TEST_LIMIT", 7) == 7


def test_int_env_valid_positive_override(monkeypatch):
    monkeypatch.setenv("MERIDIAN_TEST_LIMIT", "123")
    assert limits._int_env("MERIDIAN_TEST_LIMIT", 42) == 123


def test_int_env_valid_positive_with_surrounding_whitespace(monkeypatch):
    monkeypatch.setenv("MERIDIAN_TEST_LIMIT", "  55  ")
    assert limits._int_env("MERIDIAN_TEST_LIMIT", 42) == 55


def test_int_env_zero_falls_back_to_default(monkeypatch):
    # val > 0 is False -> default (line 29 else branch)
    monkeypatch.setenv("MERIDIAN_TEST_LIMIT", "0")
    assert limits._int_env("MERIDIAN_TEST_LIMIT", 99) == 99


def test_int_env_negative_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("MERIDIAN_TEST_LIMIT", "-5")
    assert limits._int_env("MERIDIAN_TEST_LIMIT", 99) == 99


def test_int_env_non_numeric_raises_valueerror_falls_back(monkeypatch):
    # int("abc") -> ValueError -> default (lines 30-31)
    monkeypatch.setenv("MERIDIAN_TEST_LIMIT", "not-a-number")
    assert limits._int_env("MERIDIAN_TEST_LIMIT", 13) == 13


def test_int_env_float_string_falls_back(monkeypatch):
    # "1.5" is not a valid int literal -> ValueError -> default
    monkeypatch.setenv("MERIDIAN_TEST_LIMIT", "1.5")
    assert limits._int_env("MERIDIAN_TEST_LIMIT", 8) == 8


# ---------------------------------------------------------------------------
# LimitExceeded — context + 429 message
# ---------------------------------------------------------------------------


def test_limit_exceeded_carries_context():
    exc = LimitExceeded = limits.LimitExceeded("projects_per_tenant", 1000, 1000)
    assert exc.kind == "projects_per_tenant"
    assert exc.limit == 1000
    assert exc.current == 1000
    msg = str(exc)
    assert limits.LIMIT_429_MESSAGE in msg
    assert "limit: projects_per_tenant=1000" in msg


def test_limit_exceeded_current_optional():
    exc = limits.LimitExceeded("body_bytes", 100_000)
    assert exc.current is None
    assert "body_bytes=100000" in str(exc)


# ---------------------------------------------------------------------------
# check_* guards — trip branches (74, 80, 92, 96-98, 102-104, 108-110) + passes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "func_name, attr, kind",
    [
        ("check_projects_per_tenant", "PROJECTS_PER_TENANT", "projects_per_tenant"),
        ("check_sprint_items_per_project", "SPRINT_ITEMS_PER_PROJECT", "sprint_items_per_project"),
        ("check_notes_per_project", "NOTES_PER_PROJECT", "notes_per_project"),
        ("check_decisions_per_project", "DECISIONS_PER_PROJECT", "decisions_per_project"),
        ("check_sessions_per_project", "SESSIONS_PER_PROJECT", "sessions_per_project"),
        ("check_tasks_per_project", "TASKS_PER_PROJECT", "tasks_per_project"),
        ("check_open_hitl_per_project", "OPEN_HITL_PER_PROJECT", "open_hitl_per_project"),
    ],
)
def test_count_guard_trips_at_and_above_limit(monkeypatch, func_name, attr, kind):
    func = getattr(limits, func_name)
    monkeypatch.setattr(limits, attr, 5)

    # below limit -> no raise
    func(4)

    # at limit (current >= limit) -> raise, carrying current
    with pytest.raises(limits.LimitExceeded) as ei:
        func(5)
    assert ei.value.kind == kind
    assert ei.value.limit == 5
    assert ei.value.current == 5

    # above limit -> raise
    with pytest.raises(limits.LimitExceeded) as ei2:
        func(6)
    assert ei2.value.current == 6


def test_check_body_bytes_strict_greater_than(monkeypatch):
    # body_bytes uses > (not >=): equal to limit must pass.
    monkeypatch.setattr(limits, "BODY_BYTES", 100)

    limits.check_body_bytes(99)
    limits.check_body_bytes(100)  # exactly at limit is allowed

    with pytest.raises(limits.LimitExceeded) as ei:
        limits.check_body_bytes(101)
    assert ei.value.kind == "body_bytes"
    assert ei.value.limit == 100
    assert ei.value.current == 101


def test_current_limit_reads_module_attr_at_call_time(monkeypatch):
    # _current_limit resolves via sys.modules -> monkeypatched value is picked up.
    monkeypatch.setattr(limits, "NOTES_PER_PROJECT", 3)
    assert limits._current_limit("NOTES_PER_PROJECT") == 3
    # and the guard honours it immediately
    with pytest.raises(limits.LimitExceeded):
        limits.check_notes_per_project(3)
