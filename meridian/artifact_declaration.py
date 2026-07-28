"""Typed, per-sprint-item artifact declaration contract (2f9cb288, b7308039 /
665 follow-up).

A normalized, persisted declaration an artifact-sensitive sprint item can
carry, so a receiving executor (or a downstream classifier — see the
follow-on chain item that consumes ``artifact_kind`` directly) never has to
guess "is this item producing a document, a figure, or a table, where
exactly is that output supposed to land, and how strictly should a missing
pointer be enforced."

Three fields, each independently optional:

* ``artifact_kind`` — ``"document_only" | "figure" | "table"`` (see
  :data:`ARTIFACT_KINDS`). Allow future values WITHOUT weakening validation:
  the frozenset below is the ONE place a new kind gets added — validation
  itself never loosens to accept an unlisted string. A plain enum column
  (like ``milestone_type`` / ``priority`` / ``blocker_kind``), not JSON —
  read it straight off the item dict via :func:`effective_artifact_kind`,
  the ONE clean access path.
* ``planned_output`` — a TYPED POINTER, not a free-form path. Validated via
  :func:`meridian.pointers.validate_pointer` (reused, never reimplemented)
  so it carries the SAME shape as every other pointer in this codebase:
  ``source_type``, ``targets: [{uri, selector, target_kind, ...}]``,
  ``label?``. This module owns exactly one extra sibling field on top of
  the pointer shape: ``provenance_required`` (bool) — whether the receiving
  executor must record provenance (``record_provenance``) for this output
  before it counts as satisfied. Do NOT silently infer a planned output
  from a bare directory or a generic ``mcp_tool:`` resource id — only an
  explicit, fully-typed pointer counts; this module deliberately provides
  no inference helper.
* ``policy`` — how strictly a missing/wrong output pointer is enforced:
  ``artifact_pointer_check`` (``off|warn|strict``, default ``warn``),
  ``require_exact_figure_output_pointer``,
  ``require_exact_table_output_pointer``, ``allow_document_only_override``
  (all bool, default ``False``).

Backward compatibility: an item written before this field existed (or one
that simply never declared artifact metadata) has ``artifact_kind`` /
``planned_output`` / ``artifact_policy`` all ``NULL``. That is "unknown",
never silently defaulted to a specific kind or to a strict policy —
:func:`effective_artifact_kind` / :func:`effective_planned_output` return
``None``, and :func:`effective_artifact_policy` returns the project default
policy (``artifact_pointer_check="warn"``, every bool flag ``False``) rather
than raising or inventing a value.

Foundation only, mirroring 76dde31f's ``meridian.tool_requirements`` shape
and validation discipline closely (same optional-JSON-column split, same
secret/local-path screening — REUSED, not reimplemented, from
``capability_manifest._check_no_secrets_or_local_paths``): pure
validation/normalization, no DB, no network, no model call. Persistence
lives in ``meridian.db.sprint_items`` (``sprint_items.artifact_kind`` /
``planned_output`` / ``artifact_policy`` columns); rendering lives in
``meridian.handoff`` (``build_item_briefing``'s ``<artifact_declaration>``
clause).
"""
from __future__ import annotations

import json
from typing import Any

from . import capability_manifest as _cm
from . import pointers as _pointers

ARTIFACT_DECLARATION_SCHEMA_VERSION = 1

# artifact_kind — allow future values without weakening validation: this
# frozenset is the ONE place a new kind gets added; the validation logic
# itself never loosens to accept an unlisted string.
ARTIFACT_KINDS = frozenset({"document_only", "figure", "table"})

ARTIFACT_POINTER_CHECK_LEVELS = frozenset({"off", "warn", "strict"})
DEFAULT_ARTIFACT_POINTER_CHECK = "warn"

_POLICY_BOOL_FIELDS = (
    "require_exact_figure_output_pointer",
    "require_exact_table_output_pointer",
    "allow_document_only_override",
)
_POLICY_ALLOWED_FIELDS = frozenset({"artifact_pointer_check", *_POLICY_BOOL_FIELDS})

_PLANNED_OUTPUT_ALLOWED_TOP_FIELDS = frozenset(
    {"source_type", "targets", "label", "provenance_required"}
)


class ArtifactDeclarationError(ValueError):
    """Raised when an artifact_kind / planned_output / policy declaration
    fails schema or safety validation."""


# ---------------------------------------------------------------------------
# artifact_kind — plain enum, not JSON.
# ---------------------------------------------------------------------------

def normalize_artifact_kind(raw: Any) -> str:
    """Validate + normalize a single ``artifact_kind`` value.

    Raises :class:`ArtifactDeclarationError` on anything not in
    :data:`ARTIFACT_KINDS`. Case/whitespace tolerant on write (stripped +
    lowercased); the stored value is always the canonical lowercase form.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise ArtifactDeclarationError("artifact_kind must be a non-empty string")
    kind = raw.strip().lower()
    if kind not in ARTIFACT_KINDS:
        raise ArtifactDeclarationError(
            f"artifact_kind must be one of {sorted(ARTIFACT_KINDS)}, got {raw!r}"
        )
    return kind


def parse_artifact_kind(raw: Any) -> "str | None":
    """Lenient read of the stored ``artifact_kind`` column.

    Already validated at write time (:func:`normalize_artifact_kind`), so a
    non-empty string is trusted as-is (stripped/lowercased); anything else
    (``None``, blank, a foreign value from corrupted/legacy data) degrades
    to ``None`` — "unknown", never a guess.
    """
    if isinstance(raw, str) and raw.strip():
        return raw.strip().lower()
    return None


# ---------------------------------------------------------------------------
# planned_output — a typed pointer (meridian.pointers) + provenance_required.
# ---------------------------------------------------------------------------

def normalize_planned_output(raw: Any) -> "dict[str, Any] | None":
    """Validate + normalize a ``planned_output`` declaration.

    ``None`` passes through as ``None`` (no declaration). Otherwise ``raw``
    must be an object carrying ``source_type`` + ``targets`` (validated via
    :func:`meridian.pointers.validate_pointer` — NOT reimplemented here, so
    every existing pointer rule — selector shapes, ``target_kind``
    existing/planned_new, the on-disk existence check for an explicit
    ``target_kind='existing'`` local path — applies identically), an
    optional ``label``, and an optional ``provenance_required`` bool
    (default ``False``) — the one field this module owns on top of the
    pointer shape.

    Reuses (never reimplements) 649e095f's secret/machine-local-absolute-
    path screen: this is project-shared, multi-machine state exactly like a
    capability manifest, so the same provenance rule applies — no secret-
    shaped string, no ``C:\\...`` / ``/home/...`` style absolute path.

    Raises :class:`ArtifactDeclarationError` on any schema or safety
    violation. Never silently infers a planned output from a bare directory
    or a generic ``mcp_tool:`` resource id — this function only accepts an
    EXPLICIT, fully-typed pointer; there is no inference path here.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ArtifactDeclarationError("planned_output must be an object")
    unknown = set(raw) - _PLANNED_OUTPUT_ALLOWED_TOP_FIELDS
    if unknown:
        raise ArtifactDeclarationError(
            f"unknown planned_output field(s): {sorted(unknown)}"
        )

    provenance_required = raw.get("provenance_required", False)
    if not isinstance(provenance_required, bool):
        raise ArtifactDeclarationError(
            "planned_output.provenance_required must be a boolean"
        )

    pointer_input: dict[str, Any] = {
        "source_type": raw.get("source_type"),
        "targets": raw.get("targets"),
    }
    if raw.get("label") is not None:
        pointer_input["label"] = raw["label"]
    try:
        normalized_pointer = _pointers.validate_pointer(pointer_input)
    except _pointers.PointerValidationError as exc:
        raise ArtifactDeclarationError(f"planned_output: {exc}") from exc

    normalized: dict[str, Any] = dict(normalized_pointer)
    normalized["provenance_required"] = provenance_required

    try:
        _cm._check_no_secrets_or_local_paths(normalized, path="planned_output")
    except _cm.CapabilityManifestError as exc:
        raise ArtifactDeclarationError(str(exc)) from exc
    return normalized


def serialize_planned_output(raw: Any) -> "str | None":
    """Validate, normalize, and JSON-encode a ``planned_output`` input for
    storage. Returns ``None`` when there is nothing declared (column stays
    NULL). Raises :class:`ArtifactDeclarationError` on malformed input."""
    normalized = normalize_planned_output(raw)
    return json.dumps(normalized, sort_keys=True) if normalized else None


def parse_planned_output(raw: Any) -> "dict[str, Any] | None":
    """Decode a sprint item's ``planned_output`` DB field.

    Accepts a JSON text column value, an already-decoded dict, or ``None``.
    Best-effort on read (mirrors ``tool_requirements.parse_tool_requirements``):
    the column is only ever WRITTEN through :func:`serialize_planned_output`,
    which already enforces full validation, so a decode failure here means
    corrupted/foreign data — degrade to ``None`` instead of raising.
    """
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    text = str(raw).strip()
    if not text:
        return None
    try:
        decoded = json.loads(text)
    except (TypeError, ValueError):
        return None
    return decoded if isinstance(decoded, dict) else None


# ---------------------------------------------------------------------------
# policy — artifact_pointer_check level + guard flags.
# ---------------------------------------------------------------------------

def normalize_artifact_policy(raw: Any) -> "dict[str, Any] | None":
    """Validate + normalize an artifact-pointer ``policy`` declaration.

    ``None`` passes through as ``None`` (no per-item override — see
    :func:`effective_artifact_policy` for the project-default fallback).
    Otherwise ``raw`` is an object with:

    * ``artifact_pointer_check`` — one of :data:`ARTIFACT_POINTER_CHECK_LEVELS`
      (``off|warn|strict``), default ``"warn"`` when omitted.
    * ``require_exact_figure_output_pointer`` / ``require_exact_table_output_pointer``
      / ``allow_document_only_override`` — bool, default ``False``.

    Raises :class:`ArtifactDeclarationError` on any schema violation.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ArtifactDeclarationError("policy must be an object")
    unknown = set(raw) - _POLICY_ALLOWED_FIELDS
    if unknown:
        raise ArtifactDeclarationError(f"unknown policy field(s): {sorted(unknown)}")

    check = raw.get("artifact_pointer_check", DEFAULT_ARTIFACT_POINTER_CHECK)
    if not isinstance(check, str) or check.strip().lower() not in ARTIFACT_POINTER_CHECK_LEVELS:
        raise ArtifactDeclarationError(
            f"policy.artifact_pointer_check must be one of "
            f"{sorted(ARTIFACT_POINTER_CHECK_LEVELS)}, got {check!r}"
        )
    normalized: dict[str, Any] = {"artifact_pointer_check": check.strip().lower()}
    for field in _POLICY_BOOL_FIELDS:
        val = raw.get(field, False)
        if not isinstance(val, bool):
            raise ArtifactDeclarationError(f"policy.{field} must be a boolean")
        normalized[field] = val
    return normalized


def serialize_artifact_policy(raw: Any) -> "str | None":
    """Validate, normalize, and JSON-encode a ``policy`` input for storage.
    Returns ``None`` when there is nothing declared (column stays NULL,
    which reads back as the project default via
    :func:`effective_artifact_policy`). Raises :class:`ArtifactDeclarationError`
    on malformed input."""
    normalized = normalize_artifact_policy(raw)
    return json.dumps(normalized, sort_keys=True) if normalized else None


def parse_artifact_policy(raw: Any) -> "dict[str, Any] | None":
    """Decode a sprint item's ``artifact_policy`` DB field. Best-effort on
    read, mirrors :func:`parse_planned_output` — degrades to ``None`` on any
    malformed/foreign value rather than raising."""
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    text = str(raw).strip()
    if not text:
        return None
    try:
        decoded = json.loads(text)
    except (TypeError, ValueError):
        return None
    return decoded if isinstance(decoded, dict) else None


def default_artifact_policy() -> dict[str, Any]:
    """The project default artifact-pointer policy applied when an item
    declares none: ``artifact_pointer_check="warn"``, every guard flag
    ``False``. Absent is "unknown", not "off" and not "strict" — warn is the
    deliberately middle-ground default (surface, don't silently skip; never
    hard-block an item that predates this feature)."""
    return {
        "artifact_pointer_check": DEFAULT_ARTIFACT_POINTER_CHECK,
        "require_exact_figure_output_pointer": False,
        "require_exact_table_output_pointer": False,
        "allow_document_only_override": False,
    }


# ---------------------------------------------------------------------------
# Effective accessors — the CLEAN access path a caller (e.g. the 5fd9d2fd
# classifier) reads. Each takes a sprint_item row dict (as returned by
# db.get_sprint_item / db.get_sprint_items) and never raises.
# ---------------------------------------------------------------------------

def effective_artifact_kind(item: dict[str, Any]) -> "str | None":
    """The item's declared ``artifact_kind``, or ``None`` when absent —
    NEVER guessed. This is the ONE access path a caller should read; do not
    re-derive it from ``touches_resources`` or any other signal."""
    return parse_artifact_kind(item.get("artifact_kind"))


def effective_planned_output(item: dict[str, Any]) -> "dict[str, Any] | None":
    """The item's declared ``planned_output`` (a typed pointer +
    ``provenance_required``), or ``None`` when absent — never inferred from
    a directory or a generic ``mcp_tool:`` resource id."""
    return parse_planned_output(item.get("planned_output"))


def effective_artifact_policy(item: dict[str, Any]) -> dict[str, Any]:
    """The EFFECTIVE artifact-pointer policy for one item: its own declared
    policy, field-by-field over the project default (an item declaring only
    ``artifact_pointer_check`` still gets the default ``False`` guard
    flags) — or the full project default when the item declares nothing at
    all. Absent is "unknown", never "strict" and never "off"."""
    merged = default_artifact_policy()
    stored = parse_artifact_policy(item.get("artifact_policy"))
    if not stored:
        return merged
    check = stored.get("artifact_pointer_check")
    if isinstance(check, str) and check.strip().lower() in ARTIFACT_POINTER_CHECK_LEVELS:
        merged["artifact_pointer_check"] = check.strip().lower()
    for field in _POLICY_BOOL_FIELDS:
        if isinstance(stored.get(field), bool):
            merged[field] = stored[field]
    return merged


def has_artifact_declaration(item: dict[str, Any]) -> bool:
    """True when the item carries ANY of the three declarations — used by
    callers that want to distinguish "nothing declared at all" from "some
    fields declared, defaults filling the rest"."""
    return bool(
        item.get("artifact_kind")
        or item.get("planned_output")
        or item.get("artifact_policy")
    )
