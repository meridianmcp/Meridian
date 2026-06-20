"""Helpers for executor session defaults and rendering."""

from __future__ import annotations

from typing import Any

EXECUTOR_CONFIG_KEYS = (
    "repo_path",
    "repo_paths",
    "filesystem_roots",
    "hostnames",
    "env_file",
    "test_cmd",
    "test_min",
    "deploy_cmd",
    "shell_type",
    "branch",
    "context_threshold",
    "isolation",
)

EXECUTOR_CREDENTIALS_RULE = (
    "Read secrets from env_file only, never remote shell."
)


def normalize_executor_config(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Keep only the supported executor_config keys."""
    if not isinstance(raw, dict):
        return {}
    normalized: dict[str, Any] = {}
    for key in EXECUTOR_CONFIG_KEYS:
        value = raw.get(key)
        if value is None:
            continue
        if isinstance(value, str):
            value = value.strip()
            if not value:
                continue
        normalized[key] = value
    return normalized


def executor_config_for_output(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Return normalized config plus the always-injected credentials rule."""
    return {
        **normalize_executor_config(raw),
        "credentials_rule": EXECUTOR_CREDENTIALS_RULE,
    }


def has_executor_config(raw: dict[str, Any] | None) -> bool:
    """Return True when any persisted executor setting is present."""
    return bool(normalize_executor_config(raw))


def build_executor_config_block(raw: dict[str, Any] | None) -> str:
    """Render a compact starter/handoff block for executor sessions."""
    config = executor_config_for_output(raw)
    labels = {
        "repo_path": "repo_path",
        "env_file": "env_file",
        "test_cmd": "test_cmd",
        "test_min": "test_min",
        "deploy_cmd": "deploy_cmd",
        "shell_type": "shell_type",
        "branch": "branch",
        "context_threshold": "context_threshold (turns before warning)",
        "credentials_rule": "credentials_rule",
    }
    lines = ["# Executor Config"]
    for key in (
        "repo_path",
        "env_file",
        "test_cmd",
        "test_min",
        "deploy_cmd",
        "shell_type",
        "branch",
        "context_threshold",
        "credentials_rule",
    ):
        value = config.get(key)
        if value is None or value == "":
            continue
        lines.append(f"{labels[key]}: {value}")
    return "\n".join(lines)
