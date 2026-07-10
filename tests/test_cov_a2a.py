"""Coverage tests for meridian/a2a.py — A2A agent card + task envelope helpers.

meridian/a2a.py is a pure-function module (no FastAPI routes live here — the HTTP
routes are in meridian/routes/a2a.py), so these tests call the functions directly.

Targets:
- build_agent_card: url resolution (explicit arg, MERIDIAN_BASE_URL env, localhost
  default, trailing-slash stripping) and card structure/endpoints.
- _meridian_version: primary "meridian-server" lookup, fallback to "meridian",
  and final "unknown" when neither is installed.
- task_to_a2a: envelope shape with a full row and a minimal row (status default).
- _artifacts_from_output: None / dict / str-coercion branches.
"""
from __future__ import annotations

import importlib.metadata

import pytest

from meridian import a2a


# ---------------------------------------------------------------------------
# build_agent_card — url resolution (covers lines 32-38)
# ---------------------------------------------------------------------------

def test_build_agent_card_explicit_base_url_wins():
    card = a2a.build_agent_card("https://example.com")
    assert card["url"] == "https://example.com"
    # endpoints are built from the resolved url
    assert card["endpoints"]["tasks_send"] == (
        "https://example.com/a2a/{agent_id}/tasks/send"
    )
    assert card["endpoints"]["tasks_get"] == (
        "https://example.com/a2a/{agent_id}/tasks/{task_id}"
    )


def test_build_agent_card_strips_trailing_slash():
    card = a2a.build_agent_card("https://example.com/")
    assert card["url"] == "https://example.com"


def test_build_agent_card_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("MERIDIAN_BASE_URL", "https://env.usemeridian.us")
    card = a2a.build_agent_card()
    assert card["url"] == "https://env.usemeridian.us"


def test_build_agent_card_defaults_to_localhost(monkeypatch):
    # No explicit arg and no env var -> localhost default.
    monkeypatch.delenv("MERIDIAN_BASE_URL", raising=False)
    card = a2a.build_agent_card()
    assert card["url"] == "http://localhost:7878"


def test_build_agent_card_empty_env_falls_through_to_localhost(monkeypatch):
    # An empty env var is falsy -> still hits the localhost default.
    monkeypatch.setenv("MERIDIAN_BASE_URL", "")
    card = a2a.build_agent_card()
    assert card["url"] == "http://localhost:7878"


def test_build_agent_card_structure():
    card = a2a.build_agent_card("http://localhost:7878")
    assert card["schema_version"] == "1.0"
    assert card["name"] == "Meridian Coordinator"
    assert card["capabilities"] == {
        "streaming": False,
        "push_notifications": False,
        "state_transition_history": True,
    }
    assert card["default_input_modes"] == ["application/json"]
    assert card["default_output_modes"] == ["application/json"]
    skill_ids = {s["id"] for s in card["skills"]}
    assert skill_ids == {"task_coordination", "human_in_the_loop"}
    # version comes from _meridian_version(); always a non-empty string.
    assert isinstance(card["version"], str) and card["version"]


# ---------------------------------------------------------------------------
# _meridian_version — primary / fallback / unknown (covers lines 92-101)
# ---------------------------------------------------------------------------

def test_meridian_version_primary_package(monkeypatch):
    def fake_version(name):
        if name == "meridian-server":
            return "9.9.9"
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", fake_version)
    assert a2a._meridian_version() == "9.9.9"


def test_meridian_version_fallback_to_meridian(monkeypatch):
    # "meridian-server" not installed -> fall through to "meridian".
    def fake_version(name):
        if name == "meridian-server":
            raise importlib.metadata.PackageNotFoundError(name)
        if name == "meridian":
            return "1.2.3"
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", fake_version)
    assert a2a._meridian_version() == "1.2.3"


def test_meridian_version_unknown_when_neither_installed(monkeypatch):
    def fake_version(name):
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", fake_version)
    assert a2a._meridian_version() == "unknown"


# ---------------------------------------------------------------------------
# task_to_a2a — envelope shape (covers line 115)
# ---------------------------------------------------------------------------

def test_task_to_a2a_full_row():
    row = {
        "id": "task-42",
        "agent_id": "agent-7",
        "status": "working",
        "output": {"result": "ok"},
        "metadata": {"k": "v"},
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-02T00:00:00Z",
    }
    env = a2a.task_to_a2a(row)
    assert env["task_id"] == "task-42"
    assert env["agent_id"] == "agent-7"
    assert env["status"] == {"state": "working"}
    assert env["artifacts"] == [{"type": "application/json", "data": {"result": "ok"}}]
    assert env["metadata"] == {"k": "v"}
    assert env["created_at"] == "2026-01-01T00:00:00Z"
    assert env["updated_at"] == "2026-01-02T00:00:00Z"


def test_task_to_a2a_minimal_row_defaults():
    # Only the required id key present -> status defaults to "submitted",
    # metadata defaults to {}, missing keys become None.
    env = a2a.task_to_a2a({"id": "t1"})
    assert env["task_id"] == "t1"
    assert env["agent_id"] is None
    assert env["status"] == {"state": "submitted"}
    assert env["artifacts"] == []
    assert env["metadata"] == {}
    assert env["created_at"] is None
    assert env["updated_at"] is None


def test_task_to_a2a_falsy_metadata_becomes_empty_dict():
    env = a2a.task_to_a2a({"id": "t2", "metadata": None})
    assert env["metadata"] == {}


# ---------------------------------------------------------------------------
# _artifacts_from_output — None / dict / str branches (covers lines 130-134)
# ---------------------------------------------------------------------------

def test_artifacts_from_output_none():
    assert a2a._artifacts_from_output(None) == []


def test_artifacts_from_output_dict():
    assert a2a._artifacts_from_output({"a": 1}) == [
        {"type": "application/json", "data": {"a": 1}}
    ]


def test_artifacts_from_output_string_coercion():
    assert a2a._artifacts_from_output("hello") == [
        {"type": "text/plain", "data": "hello"}
    ]


def test_artifacts_from_output_non_string_scalar_coerced_to_str():
    # A non-None, non-dict value is stringified.
    assert a2a._artifacts_from_output(123) == [
        {"type": "text/plain", "data": "123"}
    ]


def test_valid_statuses_frozenset():
    # Sanity: the module-level status set is present (import-time coverage).
    assert a2a._VALID_STATUSES == frozenset(
        {"submitted", "working", "completed", "failed", "canceled"}
    )
