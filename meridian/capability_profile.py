"""Layered capability profile inheritance and merge (02038afe).

Builds directly on :mod:`meridian.capability_manifest` — a capability
*profile* is a scoped declaration of the same typed capability entries that
module already validates (id/purpose/required_tools/fallback_chain/
provenance/availability_policy/verification_command), plus an explicit
"disable" list so a more specific scope can retract a capability id it
inherited without having to redeclare it.

Inheritance order (least specific -> most specific)::

    workspace -> user -> project -> sprint_version -> item

The *effective* profile for a project (optionally narrowed to one sprint
item) is the deterministic merge of every scope that has a stored profile,
applied in that order. A later (more specific) layer's declaration of a
capability id always wins over an earlier one — see :func:`merge_layers`.

This module is pure: no DB, no network. See
``meridian.db.get_capability_profile`` / ``set_capability_profile`` /
``clear_capability_profile`` / ``get_effective_capability_profile`` for
persistence and resolution against real project/sprint-item data.
"""

from __future__ import annotations

from typing import Any

from meridian.capability_manifest import (
    CapabilityManifestError,
    _check_no_secrets_or_local_paths,
)

# Inheritance order, least specific -> most specific. A capability id
# declared by a later (more specific) layer overrides the same id declared
# by an earlier one.
SCOPE_TYPES: tuple[str, ...] = ("workspace", "user", "project", "sprint_version", "item")


class CapabilityProfileError(CapabilityManifestError):
    """Raised on profile-layer schema/safety violations.

    Subclasses CapabilityManifestError so callers that already catch that
    type (e.g. the MCP handlers for get/set_capability_manifest) keep working
    unchanged if they're extended to cover profiles too.
    """


def normalize_scope_type(scope_type: Any) -> str:
    """Validate and lowercase a scope_type; raises on anything else."""
    if not isinstance(scope_type, str) or scope_type.strip().lower() not in SCOPE_TYPES:
        raise CapabilityProfileError(
            f"scope_type must be one of {list(SCOPE_TYPES)}, got {scope_type!r}"
        )
    return scope_type.strip().lower()


def normalize_scope_id(scope_id: Any) -> str:
    """Validate a scope_id: a required non-empty string."""
    if not isinstance(scope_id, str) or not scope_id.strip():
        raise CapabilityProfileError("scope_id must be a non-empty string")
    return scope_id.strip()


def normalize_disabled_capability_ids(raw: Any) -> list[str]:
    """Validate+normalize the explicit per-layer disable list.

    A disabled id need not be declared as a capability at this same layer —
    its purpose is to retract a capability id inherited from a *less*
    specific layer without redeclaring it. Deterministic (sorted, deduped).
    """
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
        raise CapabilityProfileError("disabled_capability_ids must be a list of strings")
    cleaned = sorted({x.strip() for x in raw if x.strip()})
    return cleaned


def normalize_provenance(raw: Any) -> dict[str, Any] | None:
    """Validate the profile-level provenance blob.

    Non-secret, non-machine-local-path only (reuses capability_manifest's
    guard) — this is project-shared, multi-machine state. Intended fields
    are informational only (not schema-enforced beyond the safety checks):
    a config source label/path, a normalized config hash, a tool-list/
    manifest hash, an observed_at timestamp, client/server identity, and a
    fallback policy label. Never raw secrets or machine-local absolute
    paths — reject those the same way capability_manifest does.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise CapabilityProfileError("provenance must be an object or null")
    try:
        _check_no_secrets_or_local_paths(raw, path="profile.provenance")
    except CapabilityManifestError as exc:
        # Re-raise as our own subclass so every capability_profile validation
        # failure is a CapabilityProfileError, even when the underlying check
        # is reused from capability_manifest (which raises its own base type).
        raise CapabilityProfileError(str(exc)) from exc
    return raw


def merge_layers(layers: list[dict[str, Any]]) -> tuple[
    list[dict[str, Any]], dict[str, str], list[dict[str, Any]], list[dict[str, Any]]
]:
    """Merge an ordered (least -> most specific) list of layer profiles.

    Each entry in ``layers`` is
    ``{"layer": <scope_type>, "capabilities": [...normalized...],
    "disabled_capability_ids": [...]}``.

    Per layer, in order:
    1. Apply this layer's disables — remove any of its ``disabled_capability_ids``
       that are currently present in the accumulated effective set (removing
       something inherited from a less specific layer). This is *not* a
       conflict; it's the layer explicitly opting out of an inherited capability.
    2. Apply this layer's own declared capabilities — each overwrites any
       existing entry for the same id (even one this same layer just disabled;
       explicitly declaring a capability is stronger intent than disabling it).
       Overwriting an id that a *different*, less specific layer had declared
       is recorded as an override. It's flagged ``conflict: True`` when the
       two declarations disagree on ``required_tools`` or
       ``availability_policy`` — the two fields that change what an executor
       can actually rely on; differences in ``fallback_chain``,
       ``verification_command``, or ``provenance`` alone are compatible
       refinements, not conflicts. Either way, resolution is always
       deterministic: **the more specific layer wins.**

    Returns ``(effective_capabilities, capability_sources, overrides, disabled_log)``:
    - ``effective_capabilities``: merged capability list, sorted by id.
    - ``capability_sources``: {capability_id: layer_name} for the winning entry.
    - ``overrides``: audit trail of every id declared by more than one layer.
    - ``disabled_log``: audit trail of every disable that actually removed
      something (a no-op disable of an id nothing had declared yet is not logged).
    """
    effective: dict[str, dict[str, Any]] = {}
    sources: dict[str, str] = {}
    overrides: list[dict[str, Any]] = []
    disabled_log: list[dict[str, Any]] = []

    for layer in layers:
        layer_name = layer["layer"]

        for cap_id in layer.get("disabled_capability_ids") or []:
            if cap_id in effective:
                disabled_log.append({
                    "capability_id": cap_id,
                    "disabled_by_layer": layer_name,
                    "previously_declared_by_layer": sources.get(cap_id),
                })
                del effective[cap_id]
                sources.pop(cap_id, None)

        for cap in layer.get("capabilities") or []:
            cap_id = cap["id"]
            if cap_id in effective:
                previous = effective[cap_id]
                conflict = (
                    previous.get("required_tools") != cap.get("required_tools")
                    or previous.get("availability_policy") != cap.get("availability_policy")
                )
                overrides.append({
                    "capability_id": cap_id,
                    "from_layer": sources.get(cap_id),
                    "to_layer": layer_name,
                    "conflict": conflict,
                    "previous": previous,
                    "new": cap,
                })
            effective[cap_id] = cap
            sources[cap_id] = layer_name

    ordered_ids = sorted(effective)
    effective_list = [effective[cap_id] for cap_id in ordered_ids]
    return effective_list, sources, overrides, disabled_log
