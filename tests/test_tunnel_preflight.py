"""31c7b1fc — focused tests for the standalone tunnel child preflight module.

Deliberately does not import SlotProxy/resolve_plugins or spin up any real
tunnel/websocket machinery — every test here spawns a short-lived, disposable
``sys.executable -c ...`` child directly, matching the module's own isolation
contract (see meridian/tunnel_preflight.py's module docstring).
"""

from __future__ import annotations

import sys

import pytest

from meridian import tunnel_preflight
from meridian.tunnel_client import SlotState


def _make_diagnostic(
    *,
    label: str = "unit-test-slot",
    healthy: bool,
    state: SlotState,
    recommend_quarantine: bool,
) -> tunnel_preflight.PreflightDiagnostic:
    return tunnel_preflight.PreflightDiagnostic(
        label=label,
        command=(sys.executable,),
        resolved_executable=sys.executable,
        cwd=None,
        healthy=healthy,
        state=state,
        reason=state.value,
        human_reason="stub",
        duration_seconds=0.0,
        exit_code=0 if healthy else 1,
        stdout_tail="",
        stderr_tail="",
        recommend_quarantine=recommend_quarantine,
    )


# ---------------------------------------------------------------------------
# preflight_child_entrypoint: real subprocess classification
# ---------------------------------------------------------------------------


def test_preflight_healthy_entrypoint_exits_zero():
    result = tunnel_preflight.preflight_child_entrypoint(
        "healthy-slot",
        [sys.executable, "-c", "import sys; sys.exit(0)"],
        timeout=10,
    )
    assert result.healthy is True
    assert result.state is SlotState.HEALTHY
    assert result.exit_code == 0
    assert result.recommend_quarantine is False


def test_preflight_missing_launcher_is_dependency_missing():
    result = tunnel_preflight.preflight_child_entrypoint(
        "missing-launcher-slot",
        ["definitely-not-a-real-launcher-xyz123"],
        timeout=5,
    )
    assert result.healthy is False
    assert result.state is SlotState.DEPENDENCY_MISSING
    assert result.exit_code is None
    assert result.recommend_quarantine is True


def test_preflight_import_error_is_dependency_missing():
    """Mirrors the live incident: mcp.server.fastmcp missing under mcp==2.0.0."""
    result = tunnel_preflight.preflight_child_entrypoint(
        "import-error-slot",
        [sys.executable, "-c", "import definitely_not_a_real_module_xyz"],
        timeout=10,
    )
    assert result.healthy is False
    assert result.state is SlotState.DEPENDENCY_MISSING
    assert "definitely_not_a_real_module_xyz" in result.human_reason
    assert result.recommend_quarantine is True


def test_preflight_fast_exit_without_signature_is_child_crashed():
    result = tunnel_preflight.preflight_child_entrypoint(
        "fast-crash-slot",
        [sys.executable, "-c", "import sys; sys.exit(1)"],
        timeout=10,
    )
    assert result.healthy is False
    assert result.state is SlotState.CHILD_CRASHED
    assert result.exit_code == 1
    assert result.recommend_quarantine is True


def test_preflight_cold_start_timeout_is_not_quarantine_recommended():
    result = tunnel_preflight.preflight_child_entrypoint(
        "slow-slot",
        [sys.executable, "-c", "import time; time.sleep(5)"],
        timeout=0.3,
    )
    assert result.healthy is False
    assert result.state is SlotState.STARTUP_TIMEOUT
    assert result.exit_code is None
    assert result.recommend_quarantine is False


def test_resolve_effective_executable_uses_path_lookup():
    resolved = tunnel_preflight.resolve_effective_executable([sys.executable, "-c", "pass"])
    assert resolved is not None
    assert resolved  # non-empty


def test_resolve_effective_executable_empty_command_returns_none():
    assert tunnel_preflight.resolve_effective_executable([]) is None


# ---------------------------------------------------------------------------
# preflight_for_label: budget derivation from tunnel_client._cold_spawn_budget
# ---------------------------------------------------------------------------


def test_preflight_for_label_derives_timeout_from_cold_spawn_budget(monkeypatch):
    captured = {}

    def fake_preflight_child_entrypoint(label, command, *, cwd=None, env=None, timeout):
        captured[label] = timeout
        return _make_diagnostic(
            label=label, healthy=True, state=SlotState.HEALTHY, recommend_quarantine=False
        )

    monkeypatch.setattr(
        tunnel_preflight, "preflight_child_entrypoint", fake_preflight_child_entrypoint
    )
    monkeypatch.setattr(
        tunnel_preflight,
        "_cold_spawn_budget",
        lambda label: (6, 5.0) if label == "docs" else (3, 2.0),
    )

    tunnel_preflight.preflight_for_label("docs", [sys.executable])
    tunnel_preflight.preflight_for_label("some-other-slot", [sys.executable])

    assert captured["docs"] == pytest.approx(30.0)
    assert captured["some-other-slot"] == pytest.approx(6.0)
    assert captured["docs"] > captured["some-other-slot"]


def test_preflight_for_label_applies_minimum_timeout_floor(monkeypatch):
    captured = {}

    def fake_preflight_child_entrypoint(label, command, *, cwd=None, env=None, timeout):
        captured["timeout"] = timeout
        return _make_diagnostic(
            label=label, healthy=True, state=SlotState.HEALTHY, recommend_quarantine=False
        )

    monkeypatch.setattr(
        tunnel_preflight, "preflight_child_entrypoint", fake_preflight_child_entrypoint
    )
    monkeypatch.setattr(tunnel_preflight, "_cold_spawn_budget", lambda label: (1, 0.1))

    tunnel_preflight.preflight_for_label("tiny-budget-slot", [sys.executable])

    assert captured["timeout"] == tunnel_preflight._MIN_LABEL_TIMEOUT_SECONDS


# ---------------------------------------------------------------------------
# PreflightQuarantineTracker
# ---------------------------------------------------------------------------


def test_quarantine_tracker_triggers_after_threshold_consecutive_failures():
    tracker = tunnel_preflight.PreflightQuarantineTracker(threshold=3)
    failure = _make_diagnostic(
        healthy=False, state=SlotState.DEPENDENCY_MISSING, recommend_quarantine=True
    )

    d1 = tracker.record(failure)
    d2 = tracker.record(failure)
    d3 = tracker.record(failure)

    assert d1.quarantined is False
    assert d2.quarantined is False
    assert d3.quarantined is True
    assert d3.consecutive_failures == 3
    assert d3.reason is not None


def test_quarantine_tracker_resets_on_healthy_result():
    tracker = tunnel_preflight.PreflightQuarantineTracker(threshold=2)
    failure = _make_diagnostic(
        healthy=False, state=SlotState.DEPENDENCY_MISSING, recommend_quarantine=True
    )
    healthy = _make_diagnostic(healthy=True, state=SlotState.HEALTHY, recommend_quarantine=False)

    tracker.record(failure)
    tracker.record(healthy)
    d3 = tracker.record(failure)

    assert d3.quarantined is False
    assert d3.consecutive_failures == 1


def test_quarantine_tracker_does_not_count_cold_start_timeout():
    tracker = tunnel_preflight.PreflightQuarantineTracker(threshold=2)
    timeout_result = _make_diagnostic(
        healthy=False, state=SlotState.STARTUP_TIMEOUT, recommend_quarantine=False
    )

    d1 = tracker.record(timeout_result)
    d2 = tracker.record(timeout_result)

    assert d1.quarantined is False
    assert d2.quarantined is False


def test_quarantine_tracker_rejects_invalid_threshold():
    with pytest.raises(ValueError):
        tunnel_preflight.PreflightQuarantineTracker(threshold=0)


# ---------------------------------------------------------------------------
# as_dict: machine-readable serialization
# ---------------------------------------------------------------------------


def test_diagnostic_as_dict_is_json_serializable():
    import json

    result = tunnel_preflight.preflight_child_entrypoint(
        "serialize-slot",
        [sys.executable, "-c", "import sys; sys.exit(0)"],
        timeout=10,
    )
    payload = result.as_dict()
    encoded = json.dumps(payload)
    decoded = json.loads(encoded)
    assert decoded["state"] == "healthy"
    assert decoded["healthy"] is True
