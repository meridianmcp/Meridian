"""Configurable severity policy for nomenclature findings.

The symbol manifest says what a project means; this module says how strictly
to treat classes of findings.  It keeps the linter useful for exploratory
drafts without weakening the default release-oriented behavior.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any


class NotationRulesError(ValueError):
    """Raised when a notation rule pack is invalid."""


_LEVELS = {"error", "warning", "ignore"}
DEFAULT_NOTATION_RULES: dict[str, Any] = {
    "version": "1",
    "case_mismatch": "warning",
    "alias_used": "warning",
    "declared_symbol_unused": "warning",
    "missing_symbol": "error",
    "flattened_subscript": "error",
    "flattened_subscript_occurrence": "error",
    "symbol_role_collision": "error",
    "ambiguous_symbol_occurrence": "error",
    "unqualified_symbol_occurrence": "warning",
    "symbol_scope_mismatch": "warning",
    "scoped_symbol_reuse": "warning",
    "omml_invalid": "error",
    "notation_manifest_missing": "error",
}


def normalize_notation_rules(rules: dict[str, Any] | None = None) -> dict[str, Any]:
    if rules is None:
        return deepcopy(DEFAULT_NOTATION_RULES)
    if not isinstance(rules, dict):
        raise NotationRulesError("notation rules must be an object")
    result = deepcopy(DEFAULT_NOTATION_RULES)
    for key, value in rules.items():
        if key == "version":
            if not isinstance(value, (str, int)) or not str(value).strip():
                raise NotationRulesError("notation rules version must be non-empty")
            result[key] = str(value).strip()
            continue
        if key not in DEFAULT_NOTATION_RULES:
            raise NotationRulesError(f"unknown notation rule: {key}")
        if value not in _LEVELS:
            raise NotationRulesError(f"notation rule {key} must be error, warning, or ignore")
        result[key] = value
    return result


def apply_notation_rules(
    findings: list[dict[str, Any]],
    rules: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Apply a rule pack without mutating the linter's source findings."""

    normalized = normalize_notation_rules(rules)
    result: list[dict[str, Any]] = []
    for finding in findings:
        copied = dict(finding)
        finding_type = str(copied.get("type", ""))
        level = normalized.get(finding_type, copied.get("severity", "warning"))
        if level == "ignore":
            continue
        copied["severity"] = level
        result.append(copied)
    return result


__all__ = [
    "DEFAULT_NOTATION_RULES",
    "NotationRulesError",
    "apply_notation_rules",
    "normalize_notation_rules",
]
