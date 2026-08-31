"""Semantic notation manifests for equation/document workflows.

This module sits above the math expression grammar. Math IR and OMML describe
how an expression is structured; this manifest describes what a symbol means,
where that meaning is valid, and how it should be typeset. It is read-only and
never renames document content.
"""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from typing import Any


class NotationManifestError(ValueError):
    """Raised when a semantic notation manifest is malformed."""


ALLOWED_KINDS = frozenset(
    {
        "unknown", "quantity", "scalar", "vector", "matrix", "set", "mask",
        "function", "operator", "constant", "label",
    }
)
ALLOWED_STYLES = frozenset({"italic", "upright", "bold", "bold_italic"})
_DEFAULT_SCOPE = "document"


def _string(value: Any, field: str, index: int, *, required: bool = False) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value.strip()):
        requirement = "non-empty " if required else ""
        raise NotationManifestError(
            f"symbols[{index}].{field} must be a {requirement}string"
        )
    return value.strip()


def _strings(
    value: Any,
    field: str,
    index: int,
    *,
    default: list[str] | None = None,
) -> list[str]:
    if value is None:
        return list(default or [])
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise NotationManifestError(f"symbols[{index}].{field} must be a string list")
    return sorted(
        {item.strip() for item in value}, key=lambda item: (item.casefold(), item)
    )


def _preferred_notation(value: Any, index: int) -> list[str]:
    """Normalize preferred qualified spellings without interpreting them.

    A preferred spelling is a publication/project convention, not an automatic
    rename instruction. Keeping it in the semantic manifest lets audits tell a
    genuinely structured ``R_depth`` term from a bare ``R`` or a flattened
    ``Rdepth`` occurrence.
    """

    if isinstance(value, str):
        # Role manifests often use a compact human-readable form such as
        # ``λ_1, λ_2`` or ``ρ_jl or ρ_fb``. Split only at top-level separators
        # so argument lists such as ``R_depth(x,y)`` and grouped subscripts
        # such as ``M_{allowed,i}`` remain one spelling.
        parts: list[str] = []
        current: list[str] = []
        depths = {"(": 0, "{": 0, "[": 0}
        closing = {")": "(",
            "}": "{",
            "]": "[",
        }
        for character in value:
            if character in depths:
                depths[character] += 1
            elif character in closing and depths[closing[character]]:
                depths[closing[character]] -= 1
            if not any(depths.values()) and character == ",":
                if "".join(current).strip():
                    parts.append("".join(current).strip())
                current = []
                continue
            current.append(character)
        if "".join(current).strip():
            parts.append("".join(current).strip())
        split_or: list[str] = []
        for part in parts or [value]:
            split_or.extend(
                item.strip() for item in part.split(" or ") if item.strip()
            )
        return _strings(split_or, "preferred_notation", index)
    return _strings(value, "preferred_notation", index)


def _typography(value: Any, index: int) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise NotationManifestError(f"symbols[{index}].typography must be an object")
    allowed = {"base", "subscript", "superscript", "operator"}
    unknown = set(value) - allowed
    if unknown:
        raise NotationManifestError(
            f"symbols[{index}].typography has unknown field(s): {', '.join(sorted(unknown))}"
        )
    result: dict[str, str] = {}
    for key, style in value.items():
        if style not in ALLOWED_STYLES:
            raise NotationManifestError(
                f"symbols[{index}].typography.{key} has unsupported style"
            )
        result[key] = style
    return {key: result[key] for key in sorted(result)}


def _entries(raw_symbols: Any) -> list[dict[str, Any]]:
    if isinstance(raw_symbols, dict):
        values: list[dict[str, Any]] = []
        for symbol, config in raw_symbols.items():
            if not isinstance(symbol, str) or not symbol.strip():
                raise NotationManifestError("symbol names must be non-empty strings")
            if config is None:
                config = {}
            if not isinstance(config, dict):
                raise NotationManifestError(f"symbols.{symbol} must be an object")
            values.append({"symbol": symbol, **config})
        return values
    if isinstance(raw_symbols, list):
        return raw_symbols
    raise NotationManifestError("notation_manifest.symbols must be a list or object")


def normalize_notation_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    """Normalize a semantic manifest into deterministic JSON-compatible data."""

    if not isinstance(manifest, dict):
        raise NotationManifestError("notation_manifest must be an object")
    version = manifest.get("version", "2")
    if not isinstance(version, (str, int)) or not str(version).strip():
        raise NotationManifestError("notation_manifest.version must be non-empty")
    case_sensitive = manifest.get("case_sensitive", True)
    if not isinstance(case_sensitive, bool):
        raise NotationManifestError("notation_manifest.case_sensitive must be boolean")

    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(_entries(manifest.get("symbols"))):
        if isinstance(raw, str):
            raw = {"symbol": raw}
        if not isinstance(raw, dict):
            raise NotationManifestError(f"symbols[{index}] must be an object")
        symbol = _string(raw.get("symbol", raw.get("name")), "symbol", index, required=True)
        stable_id = _string(raw.get("id"), "id", index) or f"symbol:{symbol.casefold()}"
        role = _string(raw.get("role"), "role", index)
        description = _string(raw.get("description"), "description", index)
        kind = _string(raw.get("kind"), "kind", index) or "unknown"
        if kind not in ALLOWED_KINDS:
            raise NotationManifestError(
                f"symbols[{index}].kind must be one of: {', '.join(sorted(ALLOWED_KINDS))}"
            )
        scope = _strings(raw.get("scope"), "scope", index, default=[_DEFAULT_SCOPE])
        if not scope:
            raise NotationManifestError(f"symbols[{index}].scope must not be empty")
        required = raw.get("required", False)
        allow_reuse = raw.get("allow_reuse", False)
        if not isinstance(required, bool):
            raise NotationManifestError(f"symbols[{index}].required must be boolean")
        if not isinstance(allow_reuse, bool):
            raise NotationManifestError(f"symbols[{index}].allow_reuse must be boolean")
        normalized.append(
            {
                "id": stable_id,
                "symbol": symbol,
                "aliases": _strings(raw.get("aliases"), "aliases", index),
                "flattened_aliases": _strings(
                    raw.get("flattened_aliases"), "flattened_aliases", index
                ),
                "preferred_notation": _preferred_notation(
                    raw.get("preferred_notation", raw.get("preferred")), index
                ),
                "role": role,
                "description": description,
                "kind": kind,
                "scope": scope,
                "indices": _strings(raw.get("indices"), "indices", index),
                "typography": _typography(raw.get("typography"), index),
                "required": required,
                "allow_reuse": allow_reuse,
            }
        )
    normalized.sort(key=lambda item: (item["symbol"].casefold(), item["symbol"], item["id"]))
    return {
        "version": str(version).strip(),
        "case_sensitive": case_sensitive,
        "symbols": normalized,
    }


def _scopes_overlap(left: list[str], right: list[str]) -> bool:
    if _DEFAULT_SCOPE in left or _DEFAULT_SCOPE in right or "global" in left or "global" in right:
        return True
    return bool(set(left) & set(right))


def validate_notation_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    """Return a normalized manifest and explicit semantic conflict findings."""

    try:
        normalized = normalize_notation_manifest(manifest)
    except NotationManifestError as exc:
        return {"valid": False, "error": str(exc), "error_type": "manifest_invalid"}

    findings: list[dict[str, Any]] = []
    by_id: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    by_symbol: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in normalized["symbols"]:
        by_id[entry["id"]].append(entry)
        by_symbol[entry["symbol"].casefold()].append(entry)

    for stable_id, entries in sorted(by_id.items()):
        if len(entries) > 1:
            findings.append(
                {
                    "type": "duplicate_symbol_id",
                    "severity": "error",
                    "id": stable_id,
                    "symbols": sorted({entry["symbol"] for entry in entries}),
                }
            )

    for entries in by_symbol.values():
        for position, left in enumerate(entries):
            for right in entries[position + 1 :]:
                if left["allow_reuse"] or right["allow_reuse"]:
                    continue
                if _scopes_overlap(left["scope"], right["scope"]):
                    if left["role"] != right["role"] or left["kind"] != right["kind"]:
                        findings.append(
                            {
                                "type": "semantic_symbol_collision",
                                "severity": "error",
                                "symbol": left["symbol"],
                                "left_id": left["id"],
                                "right_id": right["id"],
                                "left_role": left["role"],
                                "right_role": right["role"],
                                "left_kind": left["kind"],
                                "right_kind": right["kind"],
                                "left_scope": left["scope"],
                                "right_scope": right["scope"],
                            }
                        )
                elif left["role"] != right["role"] or left["kind"] != right["kind"]:
                    findings.append(
                        {
                            "type": "scoped_symbol_reuse",
                            "severity": "warning",
                            "symbol": left["symbol"],
                            "left_id": left["id"],
                            "right_id": right["id"],
                            "left_scope": left["scope"],
                            "right_scope": right["scope"],
                            "message": "same glyph has different meanings in disjoint scopes; review whether a qualified symbol is clearer",
                        }
                    )

    findings.sort(key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")))
    return {
        "valid": not any(item["severity"] == "error" for item in findings),
        "manifest": normalized,
        "manifest_sha256": manifest_digest(normalized),
        "findings": findings,
        "finding_count": len(findings),
    }


def manifest_digest(manifest: dict[str, Any]) -> str:
    payload = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = [
    "ALLOWED_KINDS",
    "ALLOWED_STYLES",
    "NotationManifestError",
    "manifest_digest",
    "normalize_notation_manifest",
    "validate_notation_manifest",
]
