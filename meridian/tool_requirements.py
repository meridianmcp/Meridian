"""Typed, per-sprint-item tool_requirements contract (76dde31f, 665 follow-up).

An item can name the exact MCP tool(s) an executor needs, with enough
structure that a receiving session (or a dashboard) never has to parse free
text to answer "what tool, from which server, is this, is it a hard
requirement or a preference, how do I call it, what do I do if it's
unavailable, and how do I confirm the call worked."

Distinct from two existing, narrower mechanisms this module deliberately does
NOT replace:

* ``sprint_items.touches_resources`` — parallel-conflict SCHEDULING metadata
  (which files/symbols/routes an item touches, for co-batching). Nothing to
  do with which TOOL executes the work.
* ``sprint_items.required_tool`` (4d1fb28f) — a single free-form string pin
  ("Serena: replace_symbol_body"), rendered as a hard /goal directive. Kept
  working unchanged for backward compatibility; still the WRITE path a caller
  may use. This module's structured ``tool_requirements`` field is the
  CANONICAL source once set on an item — see :func:`effective_tool_requirements`
  for the exact precedence rule.

Foundation only, mirroring 649e095f's ``meridian.capability_manifest`` shape
and validation discipline closely (same required/optional field split, same
secret/local-path screening — REUSED, not reimplemented, from
``capability_manifest._check_no_secrets_or_local_paths``): pure
validation/normalization, no DB, no network, no model call. Persistence lives
in ``meridian.db.sprint_items`` (``sprint_items.tool_requirements`` column);
rendering lives in ``meridian.handoff`` (batch /goal XML clause,
``build_item_briefing``) and ``meridian.capability_contract`` (the
machine-readable JSON contract) — both read this module's normalized shape so
neither ever drifts from the other.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from . import capability_manifest as _cm

TOOL_REQUIREMENTS_SCHEMA_VERSION = 1

VALID_REQUIRED_OR_PREFERRED = frozenset({"required", "preferred"})

_REQUIREMENT_REQUIRED_FIELDS = (
    "name", "server_or_namespace", "required_or_preferred", "purpose",
)
_REQUIREMENT_ALLOWED_FIELDS = frozenset({
    "name", "server_or_namespace", "required_or_preferred", "purpose",
    "call_template", "fallback", "availability_check", "verification",
})


class ToolRequirementError(ValueError):
    """Raised when a tool_requirements entry fails schema or safety validation."""


def normalize_tool_requirement(raw: Any) -> dict[str, Any]:
    """Validate and normalize a single tool_requirements entry.

    Required: ``name`` (the tool, e.g. ``'find_symbol'``),
    ``server_or_namespace`` (e.g. ``'Serena'``, ``'meridian'``,
    ``'Filesystem'``), ``required_or_preferred`` (one of ``'required'`` /
    ``'preferred'``), ``purpose`` (why this item needs it).

    Optional: ``call_template`` (an example invocation/signature),
    ``fallback`` (a string or list of alternate tool identifiers to try, in
    order, if this one is unavailable), ``availability_check`` (how to
    confirm the tool is present — e.g. a ``tools/list`` name match),
    ``verification`` (how to confirm the call actually worked).

    Raises :class:`ToolRequirementError` on any schema or safety violation —
    malformed entries reject deterministically, never partially normalize.
    """
    if not isinstance(raw, dict):
        raise ToolRequirementError("tool_requirements entry must be an object")
    unknown = set(raw) - _REQUIREMENT_ALLOWED_FIELDS
    if unknown:
        raise ToolRequirementError(
            f"unknown tool_requirements field(s): {sorted(unknown)}"
        )
    for field in _REQUIREMENT_REQUIRED_FIELDS:
        if not raw.get(field):
            raise ToolRequirementError(
                f"tool_requirements entry missing required field: {field}"
            )

    name = raw["name"]
    if not isinstance(name, str) or not name.strip():
        raise ToolRequirementError("tool_requirements.name must be a non-empty string")
    name = name.strip()

    server_or_namespace = raw["server_or_namespace"]
    if not isinstance(server_or_namespace, str) or not server_or_namespace.strip():
        raise ToolRequirementError(
            f"tool_requirements[{name}]: server_or_namespace must be a non-empty string"
        )
    server_or_namespace = server_or_namespace.strip()

    purpose = raw["purpose"]
    if not isinstance(purpose, str) or not purpose.strip():
        raise ToolRequirementError(
            f"tool_requirements[{name}]: purpose must be a non-empty string"
        )
    purpose = purpose.strip()

    required_or_preferred = raw["required_or_preferred"]
    if (
        not isinstance(required_or_preferred, str)
        or required_or_preferred.strip().lower() not in VALID_REQUIRED_OR_PREFERRED
    ):
        raise ToolRequirementError(
            f"tool_requirements[{name}]: required_or_preferred must be one of "
            f"{sorted(VALID_REQUIRED_OR_PREFERRED)}"
        )
    required_or_preferred = required_or_preferred.strip().lower()

    call_template = raw.get("call_template")
    if call_template is not None:
        if not isinstance(call_template, str) or not call_template.strip():
            raise ToolRequirementError(
                f"tool_requirements[{name}]: call_template must be a non-empty string or null"
            )
        call_template = call_template.strip()

    fallback = raw.get("fallback") or []
    if isinstance(fallback, str):
        fallback = [fallback]
    if not isinstance(fallback, list) or not all(isinstance(f, str) for f in fallback):
        raise ToolRequirementError(
            f"tool_requirements[{name}]: fallback must be a string, a list of strings, or null"
        )
    fallback = [f.strip() for f in fallback if f and f.strip()]

    availability_check = raw.get("availability_check")
    if availability_check is not None:
        if not isinstance(availability_check, str) or not availability_check.strip():
            raise ToolRequirementError(
                f"tool_requirements[{name}]: availability_check must be a non-empty string or null"
            )
        availability_check = availability_check.strip()

    verification = raw.get("verification")
    if verification is not None:
        if not isinstance(verification, str) or not verification.strip():
            raise ToolRequirementError(
                f"tool_requirements[{name}]: verification must be a non-empty string or null"
            )
        verification = verification.strip()

    normalized = {
        "name": name,
        "server_or_namespace": server_or_namespace,
        "required_or_preferred": required_or_preferred,
        "purpose": purpose,
        "call_template": call_template,
        "fallback": fallback,
        "availability_check": availability_check,
        "verification": verification,
    }
    # Reuse — never reimplement — 649e095f's secret/local-path screen. Re-raised
    # as ToolRequirementError so a caller of this module only ever needs to
    # catch one exception type, never capability_manifest's internal one.
    try:
        _cm._check_no_secrets_or_local_paths(normalized, path=f"tool_requirements[{name}]")
    except _cm.CapabilityManifestError as exc:
        raise ToolRequirementError(str(exc)) from exc
    return normalized


def normalize_tool_requirements(raw: Any) -> list[dict[str, Any]]:
    """Validate and canonicalize a full tool_requirements list.

    Deterministic ordering: normalized entries are sorted by
    ``(server_or_namespace, name)`` so the same set of requirements always
    serializes identically regardless of input order. A duplicate
    ``(name, server_or_namespace)`` pair rejects deterministically (the same
    tool from the same server declared twice is always a caller bug — use one
    entry, or a different ``purpose`` note on that one entry).
    """
    if raw is None or raw == []:
        return []
    if not isinstance(raw, list):
        raise ToolRequirementError("tool_requirements must be a list of entries")
    normalized = [normalize_tool_requirement(entry) for entry in raw]
    keys = [(r["name"], r["server_or_namespace"]) for r in normalized]
    dupes = sorted({k for k in keys if keys.count(k) > 1})
    if dupes:
        raise ToolRequirementError(
            f"duplicate tool_requirements entry (name, server_or_namespace): {dupes}"
        )
    return sorted(normalized, key=lambda r: (r["server_or_namespace"], r["name"]))


def canonical_json(requirements: list[dict[str, Any]]) -> str:
    """Canonical, byte-stable JSON encoding of a normalized requirements list.

    Same sorted-keys / compact-separators convention as
    ``capability_manifest.manifest_hash`` and
    ``capability_contract._canonical_json`` — this is what the batch /goal's
    ``<tool_requirements>`` XML clause embeds verbatim, so the XML rendering
    and the structured JSON response can be compared byte-for-byte.
    """
    return json.dumps(requirements, sort_keys=True, separators=(",", ":"))


def tool_requirements_hash(requirements: list[dict[str, Any]]) -> str:
    """Stable content hash: identical requirement sets hash identically.

    Callers should pass an already-normalized (``normalize_tool_requirements``)
    list; the sorted-keys canonical JSON encoding makes the hash independent
    of dict key order too.
    """
    return hashlib.sha256(canonical_json(requirements).encode("utf-8")).hexdigest()


def has_tool_requirements(requirements: "list[dict[str, Any]] | None") -> bool:
    """Return True when at least one tool requirement is declared."""
    return bool(requirements)


def requirement_risk_class(requirement: dict[str, Any]) -> str:
    """Classify a normalized entry's unavailable-without-a-substitute risk.

    Explicit, not left for every caller to re-derive from
    ``required_or_preferred`` + ``fallback`` independently:

    * ``'hard_block'`` — ``required_or_preferred == 'required'`` and NO
      fallback declared. If the named tool is unavailable there is no
      documented substitute; a consuming executor/orchestrator MUST treat
      this as blocking (mirrors ``capability_manifest``'s
      ``availability_policy == 'required'`` with an exhausted
      ``fallback_chain``).
    * ``'has_fallback'`` — ``required_or_preferred == 'required'`` but at
      least one ``fallback`` IS declared. Unavailability degrades to trying
      the fallback chain rather than blocking outright.
    * ``'soft'`` — ``required_or_preferred == 'preferred'``. Unavailability
      is never blocking regardless of fallback.
    """
    if requirement.get("required_or_preferred") != "required":
        return "soft"
    if requirement.get("fallback"):
        return "has_fallback"
    return "hard_block"


def legacy_required_tool_as_requirement(required_tool: str) -> dict[str, Any]:
    """Synthesize a canonical-shaped requirement from a legacy free-form
    ``sprint_items.required_tool`` pin (4d1fb28f), for READ-TIME
    compatibility only — this is never persisted and never itself passed
    through :func:`normalize_tool_requirement`'s write-time validation.

    The existing convention for ``required_tool`` values is
    ``'<server_or_namespace>: <name>'`` (e.g. ``'Serena: replace_symbol_body'``);
    when a colon is present it is split on the first one. A bare value with
    no colon (e.g. a named tunnel plugin id) gets the sentinel namespace
    ``'legacy'`` so the synthesized entry still satisfies the non-empty
    ``server_or_namespace`` invariant every structured entry carries.
    """
    text = (required_tool or "").strip()
    if ":" in text:
        head, _, tail = text.partition(":")
        server_or_namespace = head.strip() or "legacy"
        name = tail.strip() or text
    else:
        server_or_namespace = "legacy"
        name = text
    return {
        "name": name,
        "server_or_namespace": server_or_namespace,
        "required_or_preferred": "required",
        "purpose": "legacy required_tool pin (pre-tool_requirements migration)",
        "call_template": None,
        "fallback": [],
        "availability_check": None,
        "verification": None,
    }


def parse_tool_requirements(raw: Any) -> list[dict[str, Any]]:
    """Decode a sprint item's ``tool_requirements`` DB field into a list.

    Accepts a JSON text column value, an already-decoded Python list, or
    ``None``. Best-effort on read (mirrors
    ``db.get_project_capability_manifest``'s own read-time leniency): the
    column is only ever WRITTEN through :func:`serialize_tool_requirements`,
    which already enforces full validation, so a decode failure here means
    corrupted/foreign data rather than a legitimate reject — degrade to
    ``[]`` instead of raising on a read.
    """
    if raw is None:
        return []
    if isinstance(raw, list):
        return [r for r in raw if isinstance(r, dict)]
    text = str(raw).strip()
    if not text:
        return []
    try:
        decoded = json.loads(text)
    except (TypeError, ValueError):
        return []
    if not isinstance(decoded, list):
        return []
    return [r for r in decoded if isinstance(r, dict)]


def serialize_tool_requirements(raw: Any) -> str | None:
    """Validate, normalize, and JSON-encode a tool_requirements input for
    storage.

    Returns ``None`` when there is nothing valid (so the column stays NULL
    rather than holding ``"[]"``). Raises :class:`ToolRequirementError` on
    malformed input — WRITE-time strictness (mirrors
    ``capability_manifest.normalize_manifest``), unlike the lenient read path
    above.
    """
    normalized = normalize_tool_requirements(raw)
    return json.dumps(normalized) if normalized else None


def effective_tool_requirements(item: dict[str, Any]) -> list[dict[str, Any]]:
    """The CANONICAL, typed, per-item tool requirements (76dde31f).

    Structured ``item['tool_requirements']`` (already normalized at write
    time) wins whenever present. A legacy ``item['required_tool']`` pin is
    used as a read-time compatibility bridge ONLY when the item carries no
    structured requirements at all — so an item migrated to the structured
    field never sees its legacy pin re-appear alongside it, and an
    unmigrated item keeps working unchanged. This is the single function
    ``handoff.build_item_briefing``, the batch /goal's ``<tool_requirements>``
    clause, and ``capability_contract.extract_tool_requirements`` all call,
    so none of the three can independently drift on the precedence rule.
    """
    structured = parse_tool_requirements(item.get("tool_requirements"))
    if structured:
        return structured
    legacy = (item.get("required_tool") or "").strip()
    if legacy:
        return [legacy_required_tool_as_requirement(legacy)]
    return []
