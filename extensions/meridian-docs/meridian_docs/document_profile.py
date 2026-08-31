"""Project-owned document policy profiles.

Profiles keep thesis- or journal-specific formatting out of the core parser.
They are declarative, deterministic, and safe to persist as JSON.  A profile
never contains a file path, credential, or executable command.
"""
from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from typing import Any


class DocumentProfileError(ValueError):
    """Raised when a document profile is ambiguous or unsafe."""


DEFAULT_DOCUMENT_PROFILE: dict[str, Any] = {
    "version": "1",
    "equations": {
        "representation": "native_omml",
        "numbering": "document",
        "number_format": "parenthesized",
        "allow_unnumbered": True,
        "punctuation_owned_by_prose": True,
    },
    "render": {
        "required_for_release": False,
        "preferred_backends": [],
    },
    "tables": {
        "equation_layout_tables_excluded": True,
    },
    "style": {},
}


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def _validate_strings(value: Any, path: str = "profile") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or not key.strip():
                raise DocumentProfileError(f"{path} has a non-string key")
            key_lower = key.casefold()
            if any(word in key_lower for word in ("path", "file", "command", "executable", "secret", "token", "password", "credential", "api_key", "url", "endpoint")):
                raise DocumentProfileError(f"{path}.{key} is not allowed in a shared profile")
            _validate_strings(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _validate_strings(item, f"{path}[{index}]")
    elif isinstance(value, str):
        lowered = value.casefold()
        if any(secret_word in lowered for secret_word in ("password", "bearer ", "api_key", "secret_key", "begin private key", "://")):
            raise DocumentProfileError(f"{path} must not contain credentials")
        if "\\" in value or "/" in value or re.match(r"^[A-Za-z]:", value):
            raise DocumentProfileError(f"{path} must not contain machine-local paths")
    elif not isinstance(value, (bool, int, float)) and value is not None:
        raise DocumentProfileError(f"{path} contains an unsupported value type")


def normalize_document_profile(profile: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return a validated profile with generic defaults filled in."""

    if profile is None:
        profile = {}
    if not isinstance(profile, dict):
        raise DocumentProfileError("document profile must be an object")
    unknown = set(profile) - {"version", "equations", "render", "tables", "style"}
    if unknown:
        raise DocumentProfileError(f"unknown profile field(s): {', '.join(sorted(unknown))}")
    _validate_strings(profile)
    merged = _merge(DEFAULT_DOCUMENT_PROFILE, profile)
    for section in ("equations", "render", "tables", "style"):
        if not isinstance(merged.get(section), dict):
            raise DocumentProfileError(f"profile.{section} must be an object")
    unknown_equations = set(merged["equations"]) - set(DEFAULT_DOCUMENT_PROFILE["equations"])
    if unknown_equations:
        raise DocumentProfileError(f"unknown equations field(s): {', '.join(sorted(unknown_equations))}")
    unknown_render = set(merged["render"]) - set(DEFAULT_DOCUMENT_PROFILE["render"])
    if unknown_render:
        raise DocumentProfileError(f"unknown render field(s): {', '.join(sorted(unknown_render))}")
    unknown_tables = set(merged["tables"]) - set(DEFAULT_DOCUMENT_PROFILE["tables"])
    if unknown_tables:
        raise DocumentProfileError(f"unknown tables field(s): {', '.join(sorted(unknown_tables))}")
    if not isinstance(merged.get("version"), (str, int)):
        raise DocumentProfileError("profile.version must be a string or integer")
    merged["version"] = str(merged["version"]).strip()
    equations = merged["equations"]
    if equations["representation"] not in {"native_omml", "mathml", "latex_annotation"}:
        raise DocumentProfileError("equations.representation is unsupported")
    if equations["numbering"] not in {"document", "section", "none"}:
        raise DocumentProfileError("equations.numbering is unsupported")
    if equations["number_format"] not in {"parenthesized", "bare", "custom"}:
        raise DocumentProfileError("equations.number_format is unsupported")
    for key in ("allow_unnumbered", "punctuation_owned_by_prose"):
        if not isinstance(equations[key], bool):
            raise DocumentProfileError(f"equations.{key} must be boolean")
    if not isinstance(merged["render"]["required_for_release"], bool):
        raise DocumentProfileError("render.required_for_release must be boolean")
    if not isinstance(merged["render"]["preferred_backends"], list) or any(
        not isinstance(item, str) or not item.strip() for item in merged["render"]["preferred_backends"]
    ):
        raise DocumentProfileError("render.preferred_backends must be a list of strings")
    return merged


def profile_digest(profile: dict[str, Any] | None = None) -> str:
    normalized = normalize_document_profile(profile)
    payload = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def merge_document_profiles(
    base: dict[str, Any] | None,
    override: dict[str, Any] | None,
) -> dict[str, Any]:
    """Merge a project profile with a local/review override deterministically."""

    return normalize_document_profile(_merge(normalize_document_profile(base), override or {}))


__all__ = [
    "DEFAULT_DOCUMENT_PROFILE",
    "DocumentProfileError",
    "merge_document_profiles",
    "normalize_document_profile",
    "profile_digest",
]
