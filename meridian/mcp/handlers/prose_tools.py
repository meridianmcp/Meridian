"""MCP tool: prose_check. Flags and questions for a text, with a preset applied.

Thin wrapper over :mod:`meridian.prose`. Pure local computation: no network call,
no database write. The result is plain data. It holds flags, questions and plain
counts, never a score or a verdict.

Owner rules come from ``prose_rules.toml`` in the server's data directory and are
read on every call. On a multi-tenant server that file applies to every tenant on
it, so keep owner-only rules out of a shared data directory.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from meridian.prose import RULE_FILE_NAME, RuleConfig, RuleFileError, load_rule_config, run_prose_check


async def handle_prose_check(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: prose_check.

    Args: ``text`` (required), ``preset`` (general, academic, overleaf or docx;
    default general), ``include_custom_rules`` (default true).
    """
    text = args.get("text")
    if not isinstance(text, str) or not text.strip():
        return {"error": "text is required: pass the writing to check as a string"}

    preset = str(args.get("preset") or "general").strip().lower()
    include_custom = args.get("include_custom_rules", True) is not False

    config = RuleConfig.empty()
    if include_custom and data_dir:
        try:
            config = load_rule_config(Path(data_dir) / RULE_FILE_NAME)
        except RuleFileError as exc:
            return {"error": str(exc), "rule_file": RULE_FILE_NAME}

    try:
        return run_prose_check(text, preset=preset, rule_config=config)
    except ValueError as exc:
        return {"error": str(exc)}
