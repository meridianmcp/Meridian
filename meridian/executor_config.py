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
    "max_turns",  # d2c47f43 — /goal "Stop after N turns" ceiling (default 200)
    "loop_enabled",  # 76cf8bda — per-project /loop override: "workspace"|True|False
    "checkpoint_turns",  # 76cf8bda — checkpoint() cadence hint (ceiling matches max_turns)
    "serena_repo_path",  # b970fe07 — dashboard-configurable Serena default --project (extract slot)
    "codebase_code_dirs",  # b970fe07 — dashboard-configurable code-intel index dirs (code slot)
    "outputs_dirs",  # e2688fc1 — meridian-outputs indexing dirs surfaced by the codeintel vtab
    "timezone",  # 3d7b7aca — IANA zone (e.g. "America/Denver") for the session current_time block
)

EXECUTOR_CREDENTIALS_RULE = (
    "Read secrets from env_file only, never remote shell."
)


def merge_repo_paths(
    existing: Any, new: Any
) -> list[dict[str, str]]:
    """Merge two ``repo_paths`` lists of ``{cwd, hostname}`` entries.

    Dedupes by ``(cwd, hostname)`` and preserves order (existing entries first,
    then new ones). Entries are normalized to ``{"cwd", "hostname"}`` with
    stripped strings; anything without a ``cwd`` is dropped. Used so a manual
    path entry (dashboard / set_executor_config) coexists with hook-registered
    entries instead of overwriting them.
    """
    out: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def _add(entries: Any) -> None:
        if not isinstance(entries, (list, tuple)):
            return
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            cwd = str(entry.get("cwd") or "").strip()
            if not cwd:
                continue
            hostname = str(entry.get("hostname") or "").strip()
            key = (cwd, hostname)
            if key in seen:
                continue
            seen.add(key)
            out.append({"cwd": cwd, "hostname": hostname})

    _add(existing)
    _add(new)
    return out


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
