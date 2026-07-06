"""056b712f — deploy-drift pure decision logic (no network)."""
from __future__ import annotations

import importlib.util
from pathlib import Path

# scripts/ is not a package — load the module directly by path.
_spec = importlib.util.spec_from_file_location(
    "deploy_drift",
    Path(__file__).resolve().parent.parent / "scripts" / "deploy_drift.py",
)
deploy_drift = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(deploy_drift)  # type: ignore[union-attr]


def test_running_sha_from_health():
    assert deploy_drift.running_sha_from_health({"status": "ok", "git_sha": "abc123def456"}) == "abc123def456"
    assert deploy_drift.running_sha_from_health({"status": "ok"}) == ""
    assert deploy_drift.running_sha_from_health(None) == ""
    assert deploy_drift.running_sha_from_health("not-a-dict") == ""


def test_no_drift_when_shas_match():
    assert deploy_drift.assess_drift("abc123", "abc123", 0, "identical")[0] is False
    # prod's short SHA is a prefix of the full main-head SHA -> same commit
    assert deploy_drift.assess_drift("abc123", "abc123def0", 5, "diverged")[0] is False


def test_detects_prod_behind_main():
    drifted, reason = deploy_drift.assess_drift("oldsha000000", "newsha111111", 3, "ahead")
    assert drifted is True
    assert "BEHIND" in reason


def test_fail_open_on_unknowns():
    # unknown prod sha -> never page on a /health hiccup
    assert deploy_drift.assess_drift("", "newsha", 3, "ahead")[0] is False
    # ahead_by unavailable (compare API failed) -> fail-open
    assert deploy_drift.assess_drift("old", "new", None, "")[0] is False
    # compare says prod is at/ahead of main -> not behind
    assert deploy_drift.assess_drift("old", "new", 2, "behind")[0] is False
    assert deploy_drift.assess_drift("old", "new", 0, "identical")[0] is False
