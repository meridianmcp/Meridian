"""Versioned, schema-validated capability manifest (649e095f).

A capability manifest is a project's declared list of capabilities a
deterministic executor toolchain depends on -- each with an id, purpose,
required tools/servers, an ordered fallback chain, a provenance reference,
an availability policy, and an optional verification command. This module
is pure validation/normalization: no DB, no network, no model call. See
``meridian.db.get_project_capability_manifest`` / ``set_project_capability_manifest``
for persistence.

Foundation only -- profile inheritance (workspace/user -> project ->
sprint/version -> item override) and availability probing against the live
tunnel/tool inventory are separate, later slices layered on top of this
schema, not implemented here.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

MANIFEST_SCHEMA_VERSION = 1

VALID_AVAILABILITY_POLICIES = frozenset({"required", "optional", "degraded_ok"})

_CAPABILITY_REQUIRED_FIELDS = ("id", "purpose", "required_tools")
_CAPABILITY_ALLOWED_FIELDS = frozenset({
    "id", "purpose", "required_tools", "fallback_chain", "provenance",
    "availability_policy", "verification_command",
})

# Machine-local absolute path shapes that must never enter shared manifest
# state (Windows drive letters, UNC paths, POSIX home/root/etc dirs) -- this
# is project-shared, multi-machine state, not a single executor's local env.
_ABSOLUTE_PATH_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\|/home/|/Users/|/root/|/etc/|/var/)")

# Common secret-shaped tokens (API keys, bearer tokens, credentials embedded
# in a connection string). Defense in depth -- not a substitute for real
# secret storage; callers must never pass real secrets into a manifest.
_SECRET_LIKE_RE = re.compile(
    r"(?i)(sk-[a-z0-9]{10,}|api[_-]?key\s*[:=]|bearer\s+[a-z0-9._-]{10,}|"
    r"://[^/\s:]+:[^/\s@]+@|password\s*=)"
)


class CapabilityManifestError(ValueError):
    """Raised when a capability manifest fails schema or safety validation."""


def _check_no_secrets_or_local_paths(value: Any, *, path: str) -> None:
    """Recursively reject secret-shaped strings and absolute local paths."""
    if isinstance(value, str):
        if _ABSOLUTE_PATH_RE.search(value):
            raise CapabilityManifestError(
                f"{path}: machine-local absolute path not allowed in shared manifest state: {value!r}"
            )
        if _SECRET_LIKE_RE.search(value):
            raise CapabilityManifestError(
                f"{path}: secret-shaped value not allowed in shared manifest state"
            )
    elif isinstance(value, dict):
        for key, sub in value.items():
            _check_no_secrets_or_local_paths(sub, path=f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for idx, sub in enumerate(value):
            _check_no_secrets_or_local_paths(sub, path=f"{path}[{idx}]")


def normalize_capability(raw: Any) -> dict[str, Any]:
    """Validate and normalize a single capability entry.

    Raises :class:`CapabilityManifestError` on any schema or safety
    violation -- malformed manifests must reject deterministically, never
    partially normalize.
    """
    if not isinstance(raw, dict):
        raise CapabilityManifestError("capability entry must be an object")
    unknown = set(raw) - _CAPABILITY_ALLOWED_FIELDS
    if unknown:
        raise CapabilityManifestError(f"unknown capability field(s): {sorted(unknown)}")
    for field in _CAPABILITY_REQUIRED_FIELDS:
        if not raw.get(field):
            raise CapabilityManifestError(f"capability missing required field: {field}")

    cap_id = raw["id"]
    if not isinstance(cap_id, str) or not cap_id.strip():
        raise CapabilityManifestError("capability id must be a non-empty string")
    cap_id = cap_id.strip()

    purpose = raw["purpose"]
    if not isinstance(purpose, str) or not purpose.strip():
        raise CapabilityManifestError(f"capability[{cap_id}]: purpose must be a non-empty string")
    purpose = purpose.strip()

    required_tools = raw["required_tools"]
    if (
        not isinstance(required_tools, list)
        or not required_tools
        or not all(isinstance(t, str) and t.strip() for t in required_tools)
    ):
        raise CapabilityManifestError(
            f"capability[{cap_id}]: required_tools must be a non-empty list of non-empty strings"
        )
    required_tools = [t.strip() for t in required_tools]

    fallback_chain = raw.get("fallback_chain") or []
    if not isinstance(fallback_chain, list) or not all(isinstance(t, str) for t in fallback_chain):
        raise CapabilityManifestError(f"capability[{cap_id}]: fallback_chain must be a list of strings")
    fallback_chain = [t.strip() for t in fallback_chain if t.strip()]

    availability_policy = raw.get("availability_policy") or "required"
    if not isinstance(availability_policy, str) or availability_policy.strip().lower() not in VALID_AVAILABILITY_POLICIES:
        raise CapabilityManifestError(
            f"capability[{cap_id}]: availability_policy must be one of {sorted(VALID_AVAILABILITY_POLICIES)}"
        )
    availability_policy = availability_policy.strip().lower()

    verification_command = raw.get("verification_command")
    if verification_command is not None:
        if not isinstance(verification_command, str) or not verification_command.strip():
            raise CapabilityManifestError(
                f"capability[{cap_id}]: verification_command must be a non-empty string or null"
            )
        verification_command = verification_command.strip()

    provenance = raw.get("provenance")
    if provenance is not None and not isinstance(provenance, (str, dict)):
        raise CapabilityManifestError(f"capability[{cap_id}]: provenance must be a string, object, or null")

    normalized = {
        "id": cap_id,
        "purpose": purpose,
        "required_tools": required_tools,
        "fallback_chain": fallback_chain,
        "availability_policy": availability_policy,
        "verification_command": verification_command,
        "provenance": provenance,
    }
    _check_no_secrets_or_local_paths(normalized, path=f"capability[{cap_id}]")
    return normalized


def normalize_manifest(raw: Any) -> list[dict[str, Any]]:
    """Validate and canonicalize a full manifest (a list of capabilities).

    Deterministic ordering: normalized entries are sorted by capability id
    so the same set of capabilities always serializes identically regardless
    of input order. Duplicate ids reject deterministically.
    """
    if raw is None or raw == []:
        return []
    if not isinstance(raw, list):
        raise CapabilityManifestError("manifest must be a list of capability entries")
    normalized = [normalize_capability(entry) for entry in raw]
    ids = [c["id"] for c in normalized]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise CapabilityManifestError(f"duplicate capability id(s): {dupes}")
    return sorted(normalized, key=lambda c: c["id"])


def manifest_hash(manifest: list[dict[str, Any]]) -> str:
    """Stable content hash: identical capability sets hash identically.

    Callers should pass an already-normalized (``normalize_manifest``)
    manifest; the sorted-keys canonical JSON encoding makes the hash
    independent of dict key order too.
    """
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def has_capability_manifest(manifest: list[dict[str, Any]] | None) -> bool:
    """Return True when a project has at least one declared capability."""
    return bool(manifest)
