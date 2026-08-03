"""Coverage for meridian/executor_config.py (d8a9dcbb / 45e4d10c).

Pure helpers for executor session defaults + the starter/handoff config block.
Targets the previously-uncovered lines: the non-dict normalize guard, the
credentials-rule injection, and the full build_executor_config_block rendering.
"""
from __future__ import annotations

from meridian import executor_config as ec


def test_normalize_drops_non_dict_and_empty_and_none():
    assert ec.normalize_executor_config(None) == {}
    assert ec.normalize_executor_config("nope") == {}
    assert ec.normalize_executor_config(42) == {}
    # None values skipped; strings stripped; empty/whitespace strings dropped;
    # unsupported keys ignored.
    out = ec.normalize_executor_config({
        "test_cmd": "  pixi run test  ",
        "branch": "   ",          # blank after strip → dropped
        "deploy_cmd": None,       # None → skipped
        "not_a_key": "x",         # unsupported → ignored
        "max_turns": 300,         # non-str value kept as-is
    })
    assert out == {"test_cmd": "pixi run test", "max_turns": 300}


def test_executor_config_for_output_injects_credentials_rule():
    out = ec.executor_config_for_output({"branch": "dev"})
    assert out["branch"] == "dev"
    assert out["credentials_rule"] == ec.EXECUTOR_CREDENTIALS_RULE
    # Even with no config, the credentials rule is always present.
    assert ec.executor_config_for_output(None) == {
        "credentials_rule": ec.EXECUTOR_CREDENTIALS_RULE
    }


def test_has_executor_config():
    assert ec.has_executor_config({"branch": "dev"}) is True
    assert ec.has_executor_config(None) is False
    assert ec.has_executor_config({}) is False
    assert ec.has_executor_config({"unsupported": "x"}) is False


def test_build_executor_config_block_renders_labeled_lines():
    block = ec.build_executor_config_block({
        "test_cmd": "pixi run test",
        "test_min": 2150,
        "branch": "dev",
        "context_threshold": 50,
        "env_file": ".env",
    })
    lines = block.splitlines()
    assert lines[0] == "# Executor Config"
    assert "test_cmd: pixi run test" in lines
    assert "branch: dev" in lines
    assert "context_threshold (turns before warning): 50" in lines
    # credentials_rule is always injected + rendered.
    assert any(line.startswith("credentials_rule: ") for line in lines)
    # A key not set is not rendered.
    assert not any(line.startswith("deploy_cmd:") for line in lines)


def test_build_executor_config_block_empty_config_is_just_header_plus_creds():
    block = ec.build_executor_config_block(None)
    lines = block.splitlines()
    assert lines[0] == "# Executor Config"
    # Only the always-injected credentials rule shows up (no other keys).
    assert any(line.startswith("credentials_rule:") for line in lines)
    assert all(line == "# Executor Config" or line.startswith("credentials_rule:") for line in lines)


def test_outputs_dirs_is_preserved_by_normalize():
    # e2688fc1 — outputs_dirs must be kept so the codeintel vtab can read it.
    out = ec.normalize_executor_config({
        "outputs_dirs": ["/data/outputs", "/scratch/results"],
        "branch": "dev",
    })
    assert out["outputs_dirs"] == ["/data/outputs", "/scratch/results"]
    assert out["branch"] == "dev"
    # Confirm it survives round-trip through executor_config_for_output too.
    full = ec.executor_config_for_output({"outputs_dirs": ["/data/outputs"]})
    assert full["outputs_dirs"] == ["/data/outputs"]
    assert "credentials_rule" in full


def test_normalize_execution_policy_mode_accepts_both_vocabularies():
    # Policy vocabulary passes through unchanged.
    assert ec.normalize_execution_policy_mode("immediate") == "immediate"
    assert ec.normalize_execution_policy_mode("relaxed") == "relaxed"
    # Underlying project-posture vocabulary is translated.
    assert ec.normalize_execution_policy_mode("autonomous") == "immediate"
    assert ec.normalize_execution_policy_mode("interactive") == "relaxed"
    # Case/whitespace tolerant.
    assert ec.normalize_execution_policy_mode("  INTERACTIVE  ") == "relaxed"
    # Unknown/missing/wrong type -> default 'immediate', never raises.
    assert ec.normalize_execution_policy_mode(None) == "immediate"
    assert ec.normalize_execution_policy_mode("bogus") == "immediate"
    assert ec.normalize_execution_policy_mode(42) == "immediate"


def test_build_execution_policy_immediate_default_shape():
    policy = ec.build_execution_policy(None, execution_mode="autonomous")
    assert policy == {
        "execution_mode": "immediate",
        "max_planning_turns": ec.DEFAULT_MAX_PLANNING_TURNS_IMMEDIATE,
        "required_first_action": ec.REQUIRED_FIRST_ACTION_IMMEDIATE,
        "no_confirmation": True,
        "permitted_parallel_wave": True,
        "claim_before_edit": True,
        "genuine_blocker_escalation": ec.GENUINE_BLOCKER_ESCALATION_RULE,
    }


def test_build_execution_policy_relaxed_default_shape():
    policy = ec.build_execution_policy(None, execution_mode="interactive")
    assert policy["execution_mode"] == "relaxed"
    assert policy["max_planning_turns"] == ec.DEFAULT_MAX_PLANNING_TURNS_RELAXED
    assert policy["required_first_action"] == ec.REQUIRED_FIRST_ACTION_RELAXED
    assert policy["no_confirmation"] is False
    assert policy["permitted_parallel_wave"] is False
    # claim_before_edit is non-negotiable regardless of mode.
    assert policy["claim_before_edit"] is True


def test_build_execution_policy_missing_mode_defaults_to_immediate():
    # No execution_mode at all -> same as passing nothing / an unknown value.
    policy = ec.build_execution_policy({})
    assert policy["execution_mode"] == "immediate"
    assert policy["required_first_action"] == "claim_sprint_item"


def test_build_execution_policy_honors_max_planning_turns_override():
    policy = ec.build_execution_policy(
        {"max_planning_turns": 5}, execution_mode="autonomous",
    )
    assert policy["max_planning_turns"] == 5
    # Other fields stay deterministic — not user-configurable.
    assert policy["execution_mode"] == "immediate"
    assert policy["no_confirmation"] is True


def test_build_execution_policy_rejects_unsafe_max_planning_turns():
    # Invalid/unsafe values fall back to the mode default rather than being
    # persisted verbatim into a live policy.
    for bad in (0, -5, "not-a-number", None, [], {}):
        policy = ec.build_execution_policy(
            {"max_planning_turns": bad}, execution_mode="autonomous",
        )
        assert policy["max_planning_turns"] == ec.DEFAULT_MAX_PLANNING_TURNS_IMMEDIATE
    # An absurdly large override is clamped to the hard ceiling, not honored
    # verbatim -- "relaxed" must never mean genuinely unbounded.
    policy = ec.build_execution_policy(
        {"max_planning_turns": 999999}, execution_mode="interactive",
    )
    assert policy["max_planning_turns"] == ec.MAX_PLANNING_TURNS_CEILING


def test_build_execution_policy_non_dict_executor_config_is_tolerated():
    for bad in (None, "nope", 42, ["list"]):
        policy = ec.build_execution_policy(bad, execution_mode="autonomous")
        assert policy["max_planning_turns"] == ec.DEFAULT_MAX_PLANNING_TURNS_IMMEDIATE


def test_merge_repo_paths_dedupes_and_normalizes():
    existing = [{"cwd": " /a ", "hostname": "h1"}, {"cwd": "/b", "hostname": ""}]
    new = [{"cwd": "/a", "hostname": "h1"}, {"cwd": "/c"}, {"no_cwd": "x"}, "bad"]
    out = ec.merge_repo_paths(existing, new)
    assert out == [
        {"cwd": "/a", "hostname": "h1"},
        {"cwd": "/b", "hostname": ""},
        {"cwd": "/c", "hostname": ""},
    ]
    # Non-list inputs are tolerated (return whatever is valid).
    assert ec.merge_repo_paths(None, None) == []
    assert ec.merge_repo_paths("x", 5) == []
