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
