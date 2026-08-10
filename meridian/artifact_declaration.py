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

A FOURTH, independently-optional field — ``artifact_recipe`` (f6912e2d) —
was added later, closing the remaining gaps against that item's acceptance
criteria (exact MCP tool names / local-vs-hosted path / structural checks /
Word-COM render checks / Outputs hash-provenance checks / fallback-degraded
semantics / rollback policy / exact focused tests / "no ambiguous filename-
label match authorizes promotion"). Most of that criteria list turned out to
already be covered by this module's original three fields plus
``meridian.tool_requirements`` — see the constants block near
``_MERGER_LOCK_PREFIX`` and :func:`check_artifact_recipe_completeness` for
exactly what ``artifact_recipe`` adds vs. what was already there.
``artifact_recipe`` follows the SAME normalize_X/serialize_X/parse_X/
effective_X shape as the original three, but (unlike them) is NOT yet wired
into a ``sprint_items`` DB column or ``meridian.handoff``'s rendering —
both are outside f6912e2d's touches_resources scope; a follow-up item wires
persistence + rendering.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
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
    {"source_type", "targets", "label", "provenance_required", "promotion"}
)

# ---------------------------------------------------------------------------
# 24f5146d — promotion: an OPTIONAL sibling field on planned_output carrying
# deterministic, idempotent script->artifact->document PROMOTION metadata for
# a docx target, built to be applied via tools/meridian_fallbacks/patch_manifest.py
# + transactional_merge.py (durable JSON manifest + all-or-nothing apply,
# reused verbatim here -- never reimplemented). See the functions below this
# block for the actual precondition-check / canonical-merger-lock machinery;
# this section owns only the DECLARATION schema:
#
#   * base_sha256          -- optional. The sha256 the promotion's TARGET docx
#                              had when this promotion was planned/declared
#                              (format-validated here as a 64-hex-char string;
#                              the REAL check against the live file happens at
#                              apply time via check_promotion_preconditions /
#                              transactional_merge.apply_patch_manifest's own
#                              verify_base_unchanged staleness gate).
#   * resource_footprint    -- optional list of declared STABLE targets this
#                              promotion touches, each a typed string tagged
#                              with one of _FOOTPRINT_PREFIXES (paraId/table/
#                              figure/media/part). This is deliberately a
#                              plain string list here, not a pointers.py
#                              selector: pointers.py is out of this item's
#                              touches_resources scope, and these identifiers
#                              (a Word paraId, a zip part name) have no
#                              equivalent selector type there today.
#   * merger_lock_key       -- optional. The identifier the ONE canonical
#                              merger lock (acquire_promotion_merger_lock /
#                              release_promotion_merger_lock below) is keyed
#                              on. Defaults to the planned_output's own first
#                              target uri when omitted -- most promotions have
#                              exactly one target, so this is almost always
#                              unnecessary to set explicitly.
#
# Never a secret, never a machine-local absolute path: reuses the SAME
# capability_manifest._check_no_secrets_or_local_paths screen the rest of
# planned_output already runs (see normalize_planned_output) since promotion
# nests inside the same normalized dict.
# ---------------------------------------------------------------------------

_PROMOTION_ALLOWED_FIELDS = frozenset(
    {"base_sha256", "resource_footprint", "merger_lock_key"}
)

# Stable target-kind prefixes a resource_footprint entry may declare. Kept as
# a frozenset (mirrors ARTIFACT_KINDS' own "one place to add a value" shape)
# so a future target kind is additive, never a loosening of validation.
_FOOTPRINT_PREFIXES = ("paraId:", "table:", "figure:", "media:", "part:")

# Bound on resource_footprint length -- a declaration is a small, reviewable
# list of stable anchors, not an unbounded dump of every part in the archive.
_MAX_FOOTPRINT_ENTRIES = 200

_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")

# 24f5146d — the ONE canonical merger-lock namespace prefix. Every promotion
# path (a wave-dispatched worker, a direct MCP caller, a test) that wants
# exclusive access to a docx promotion target derives its lock key through
# merger_lock_key_for_target() below -- never a hand-rolled string -- so two
# call sites can never accidentally pick different keys for the same target.
_MERGER_LOCK_PREFIX = "docx-merger-lock::"

# ---------------------------------------------------------------------------
# f6912e2d -- artifact_recipe: an OPTIONAL, independently-declared sibling
# field closing the remaining acceptance-criteria gaps the pre-existing
# artifact_kind / planned_output / policy / promotion fields did not already
# cover. (Confirmed already covered, so deliberately NOT duplicated here:
# existing-vs-planned_new -- planned_output.targets[].target_kind; source
# module/symbol or DOCX node pointer -- planned_output.targets[].selector's
# "symbol"/"node_id" types via meridian.pointers; exact MCP namespace/tool
# names -- meridian.tool_requirements's server_or_namespace+name, already
# rendered into handoffs; fallback/degraded semantics -- tool_requirements'
# per-tool fallback chain plus this module's own artifact_pointer_check
# off/warn/strict.) Four genuinely new fields:
#
#   * execution_path  -- "local" | "hosted": WHICH deployment path (a
#     self-hosted pixi/from-source MCP connection vs. the hosted
#     usemeridian.us tier -- see AGENTS.md's two connection blocks) this
#     item's recipe runs against. Never inferred: an item mixing local-only
#     tooling (Word COM automation, a local outputs_dir) with a hosted
#     assumption is exactly the kind of silent mismatch this field exists to
#     surface explicitly.
#   * rollback_policy -- one of ROLLBACK_POLICIES: what an executor must do
#     if this recipe's write/promotion fails partway. "transactional_atomic"
#     names the EXISTING tools/meridian_fallbacks/transactional_merge.py
#     all-or-nothing apply (reused, never reimplemented, by
#     check_promotion_preconditions's sibling apply path); "manual_restore"
#     and "none" are honest, explicit declarations for a recipe that does
#     NOT go through that pipeline -- never silently assumed atomic.
#   * checks -- which of the three verification classes the acceptance
#     criteria name (structural / Word-COM render / Outputs hash-provenance)
#     this item's recipe requires. Each is a plain bool, default False --
#     absent means "not required", never silently "yes". See
#     meridian.docx_integrity_gate.RECIPE_CHECK_REGISTRY for the EXACT
#     function/tool each flag names.
#   * focused_tests -- a non-empty list of exact pytest node ids / file
#     paths an executor must run to verify this item. "No ambiguous
#     filename/label match" applies here too: this module does not
#     normalize a bare module name into a guessed node id.
#
# Persistence/handoff-rendering note: this field mirrors the SAME
# normalize_X/serialize_X/parse_X/effective_X shape as artifact_kind /
# planned_output / policy, but (unlike those three) is NOT yet wired into a
# sprint_items DB column or meridian.handoff's <artifact_declaration> clause
# -- both live outside this item's touches_resources scope. A follow-up item
# wires persistence + rendering, mirroring how those three fields were
# originally introduced.
# ---------------------------------------------------------------------------

EXECUTION_PATHS = frozenset({"local", "hosted"})
ROLLBACK_POLICIES = frozenset({"transactional_atomic", "manual_restore", "none"})

_RECIPE_CHECK_FIELDS = (
    "structural_check_required",
    "word_com_render_check_required",
    "outputs_provenance_check_required",
)
_RECIPE_CHECKS_ALLOWED_FIELDS = frozenset(_RECIPE_CHECK_FIELDS)
_RECIPE_ALLOWED_FIELDS = frozenset(
    {"execution_path", "rollback_policy", "checks", "focused_tests"}
)

# A recipe's focused_tests is a small, reviewable, EXACT list -- not an
# unbounded dump. Mirrors _MAX_FOOTPRINT_ENTRIES's bounding discipline above.
_MAX_FOCUSED_TESTS = 50


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


def merger_lock_key_for_target(target_uri: Any) -> str:
    """The canonical, namespaced lock key for a promotion target's ONE
    merger lock. The single place the ``docx-merger-lock::`` prefix is
    applied — every acquire/release/read-only-check call below routes
    through this function so two call sites can never derive different keys
    for what is semantically the same target."""
    if not isinstance(target_uri, str) or not target_uri.strip():
        raise ArtifactDeclarationError(
            "target_uri is required to derive a merger_lock_key"
        )
    return f"{_MERGER_LOCK_PREFIX}{target_uri.strip()}"


def normalize_promotion(raw: Any) -> "dict[str, Any] | None":
    """Validate + normalize a ``planned_output.promotion`` declaration.

    ``None`` passes through as ``None``. Otherwise ``raw`` is an object with
    the three OPTIONAL fields documented above :data:`_PROMOTION_ALLOWED_FIELDS`:
    ``base_sha256`` (a 64-hex-char sha256 string), ``resource_footprint`` (a
    bounded list of ``paraId:``/``table:``/``figure:``/``media:``/``part:``
    tagged strings), and ``merger_lock_key`` (a non-empty string identifier;
    left ``None`` here means "derive from the first target uri at read
    time" — see :func:`normalize_planned_output`, which is the only caller
    with the sibling ``targets`` list in scope to fill that default).

    Raises :class:`ArtifactDeclarationError` on any schema violation.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ArtifactDeclarationError("planned_output.promotion must be an object")
    unknown = set(raw) - _PROMOTION_ALLOWED_FIELDS
    if unknown:
        raise ArtifactDeclarationError(
            f"unknown planned_output.promotion field(s): {sorted(unknown)}"
        )

    normalized: dict[str, Any] = {}

    base_sha256 = raw.get("base_sha256")
    if base_sha256 is not None:
        if not isinstance(base_sha256, str) or not _SHA256_HEX_RE.match(
            base_sha256.strip().lower()
        ):
            raise ArtifactDeclarationError(
                "planned_output.promotion.base_sha256 must be a 64-character "
                f"hex sha256 string, got {base_sha256!r}"
            )
        normalized["base_sha256"] = base_sha256.strip().lower()
    else:
        normalized["base_sha256"] = None

    footprint = raw.get("resource_footprint")
    if footprint is not None:
        if not isinstance(footprint, list):
            raise ArtifactDeclarationError(
                "planned_output.promotion.resource_footprint must be a list"
            )
        if len(footprint) > _MAX_FOOTPRINT_ENTRIES:
            raise ArtifactDeclarationError(
                "planned_output.promotion.resource_footprint has "
                f"{len(footprint)} entries, exceeding the cap of "
                f"{_MAX_FOOTPRINT_ENTRIES}"
            )
        normalized_footprint: list[str] = []
        seen: set[str] = set()
        for idx, entry in enumerate(footprint):
            if not isinstance(entry, str) or not entry.strip():
                raise ArtifactDeclarationError(
                    f"planned_output.promotion.resource_footprint[{idx}] must "
                    "be a non-empty string"
                )
            entry = entry.strip()
            if not entry.startswith(_FOOTPRINT_PREFIXES):
                raise ArtifactDeclarationError(
                    f"planned_output.promotion.resource_footprint[{idx}] "
                    f"{entry!r} must start with one of {_FOOTPRINT_PREFIXES}"
                )
            if entry not in seen:
                seen.add(entry)
                normalized_footprint.append(entry)
        normalized["resource_footprint"] = normalized_footprint
    else:
        normalized["resource_footprint"] = []

    merger_lock_key = raw.get("merger_lock_key")
    if merger_lock_key is not None:
        if not isinstance(merger_lock_key, str) or not merger_lock_key.strip():
            raise ArtifactDeclarationError(
                "planned_output.promotion.merger_lock_key must be a "
                "non-empty string"
            )
        normalized["merger_lock_key"] = merger_lock_key.strip()
    else:
        normalized["merger_lock_key"] = None

    return normalized


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

    # 24f5146d — promotion is OPTIONAL and independently normalized above;
    # a caller that never declares it sees no new key at all in previously
    # round-tripped dicts other than this explicit None (matches the sibling
    # optional-field convention already used for artifact_kind/planned_output
    # themselves: absent is "unknown", never fabricated).
    promotion = normalize_promotion(raw.get("promotion"))
    if promotion is not None and promotion.get("merger_lock_key") is None:
        # Default the lock key from the pointer's own first target uri — the
        # common case of exactly one promotion target per declaration.
        first_targets = normalized_pointer.get("targets") or []
        if first_targets and isinstance(first_targets[0], dict) and first_targets[0].get("uri"):
            promotion["merger_lock_key"] = first_targets[0]["uri"]
    normalized["promotion"] = promotion

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
# f6912e2d — artifact_recipe: execution_path / rollback_policy / checks /
# focused_tests. See the constants block above (near _MERGER_LOCK_PREFIX) for
# the full rationale of why each field exists and what it deliberately does
# NOT duplicate from artifact_kind/planned_output/policy/promotion.
# ---------------------------------------------------------------------------

def normalize_artifact_recipe(raw: Any) -> "dict[str, Any] | None":
    """Validate + normalize an ``artifact_recipe`` declaration.

    ``None`` passes through as ``None`` (no declaration — see
    :func:`check_artifact_recipe_completeness` for how an artifact-sensitive
    item without one is reported). Otherwise ``raw`` is an object with:

    * ``execution_path`` — required, one of :data:`EXECUTION_PATHS`
      (``"local" | "hosted"``).
    * ``rollback_policy`` — required, one of :data:`ROLLBACK_POLICIES`.
    * ``checks`` — optional object with up to the three
      :data:`_RECIPE_CHECK_FIELDS` bool flags, each defaulting to ``False``
      when the ``checks`` object itself is present but a given flag is
      omitted, and all ``False`` when ``checks`` is omitted entirely.
    * ``focused_tests`` — required, a non-empty list of non-empty strings
      (exact pytest node ids / file paths), bounded to
      :data:`_MAX_FOCUSED_TESTS`, de-duplicated while preserving first-seen
      order.

    Raises :class:`ArtifactDeclarationError` on any schema violation —
    required fields missing/malformed, an unknown top-level or ``checks``
    field, or a ``focused_tests`` entry that is not a real string. Never
    guesses a value for a required field: an item that wants an
    ``artifact_recipe`` at all must supply ``execution_path``,
    ``rollback_policy``, and ``focused_tests`` explicitly.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ArtifactDeclarationError("artifact_recipe must be an object")
    unknown = set(raw) - _RECIPE_ALLOWED_FIELDS
    if unknown:
        raise ArtifactDeclarationError(
            f"unknown artifact_recipe field(s): {sorted(unknown)}"
        )

    execution_path = raw.get("execution_path")
    if not isinstance(execution_path, str) or execution_path.strip().lower() not in EXECUTION_PATHS:
        raise ArtifactDeclarationError(
            f"artifact_recipe.execution_path must be one of {sorted(EXECUTION_PATHS)}, "
            f"got {execution_path!r}"
        )
    execution_path = execution_path.strip().lower()

    rollback_policy = raw.get("rollback_policy")
    if not isinstance(rollback_policy, str) or rollback_policy.strip().lower() not in ROLLBACK_POLICIES:
        raise ArtifactDeclarationError(
            f"artifact_recipe.rollback_policy must be one of {sorted(ROLLBACK_POLICIES)}, "
            f"got {rollback_policy!r}"
        )
    rollback_policy = rollback_policy.strip().lower()

    checks_raw = raw.get("checks") or {}
    if not isinstance(checks_raw, dict):
        raise ArtifactDeclarationError("artifact_recipe.checks must be an object")
    unknown_checks = set(checks_raw) - _RECIPE_CHECKS_ALLOWED_FIELDS
    if unknown_checks:
        raise ArtifactDeclarationError(
            f"unknown artifact_recipe.checks field(s): {sorted(unknown_checks)}"
        )
    checks: dict[str, Any] = {}
    for field in _RECIPE_CHECK_FIELDS:
        val = checks_raw.get(field, False)
        if not isinstance(val, bool):
            raise ArtifactDeclarationError(f"artifact_recipe.checks.{field} must be a boolean")
        checks[field] = val

    focused_tests_raw = raw.get("focused_tests")
    if not isinstance(focused_tests_raw, list) or not focused_tests_raw:
        raise ArtifactDeclarationError(
            "artifact_recipe.focused_tests must be a non-empty list of strings"
        )
    if len(focused_tests_raw) > _MAX_FOCUSED_TESTS:
        raise ArtifactDeclarationError(
            f"artifact_recipe.focused_tests has {len(focused_tests_raw)} entries, "
            f"exceeding the cap of {_MAX_FOCUSED_TESTS}"
        )
    focused_tests: list[str] = []
    seen: set[str] = set()
    for idx, entry in enumerate(focused_tests_raw):
        if not isinstance(entry, str) or not entry.strip():
            raise ArtifactDeclarationError(
                f"artifact_recipe.focused_tests[{idx}] must be a non-empty string"
            )
        entry = entry.strip()
        if entry not in seen:
            seen.add(entry)
            focused_tests.append(entry)

    normalized: dict[str, Any] = {
        "execution_path": execution_path,
        "rollback_policy": rollback_policy,
        "checks": checks,
        "focused_tests": focused_tests,
    }
    try:
        _cm._check_no_secrets_or_local_paths(normalized, path="artifact_recipe")
    except _cm.CapabilityManifestError as exc:
        raise ArtifactDeclarationError(str(exc)) from exc
    return normalized


def serialize_artifact_recipe(raw: Any) -> "str | None":
    """Validate, normalize, and JSON-encode an ``artifact_recipe`` input for
    storage. Returns ``None`` when there is nothing declared. Raises
    :class:`ArtifactDeclarationError` on malformed input."""
    normalized = normalize_artifact_recipe(raw)
    return json.dumps(normalized, sort_keys=True) if normalized else None


def parse_artifact_recipe(raw: Any) -> "dict[str, Any] | None":
    """Decode a sprint item's ``artifact_recipe`` field. Best-effort on read,
    mirrors :func:`parse_planned_output` — degrades to ``None`` on any
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
    ``provenance_required``, plus an optional ``promotion`` block — 24f5146d —
    carrying base-hash preconditions / declared resource footprint / merger
    lock key for a docx script->artifact->document promotion), or ``None``
    when absent — never inferred from a directory or a generic ``mcp_tool:``
    resource id. Prefer :func:`effective_promotion` when only the promotion
    sub-block is needed."""
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


def effective_artifact_recipe(item: dict[str, Any]) -> "dict[str, Any] | None":
    """The item's declared ``artifact_recipe`` (``execution_path`` /
    ``rollback_policy`` / ``checks`` / ``focused_tests``), or ``None`` when
    absent — never guessed. See :func:`check_artifact_recipe_completeness`
    for the composed "is this item's recipe genuinely executable" verdict
    across this field AND the pre-existing artifact_kind/planned_output/
    tool_requirements fields."""
    return parse_artifact_recipe(item.get("artifact_recipe"))


def has_artifact_declaration(item: dict[str, Any]) -> bool:
    """True when the item carries ANY of the three ORIGINAL declarations
    (artifact_kind / planned_output / artifact_policy) — used by callers
    that want to distinguish "nothing declared at all" from "some fields
    declared, defaults filling the rest". Deliberately does NOT fold in
    ``artifact_recipe`` (f6912e2d): that field is not yet wired into
    ``meridian.handoff``'s ``<artifact_declaration>`` clause (see this
    module's own docstring history above), so folding it in here would flip
    this predicate to ``True`` for an item whose recipe would then render as
    an empty/default JSON body — silently misleading, not informative. Use
    :func:`effective_artifact_recipe` directly to check for a recipe."""
    return bool(
        item.get("artifact_kind")
        or item.get("planned_output")
        or item.get("artifact_policy")
    )


def effective_promotion(item: dict[str, Any]) -> "dict[str, Any] | None":
    """The item's declared ``planned_output.promotion`` block, or ``None``
    when the item has no ``planned_output`` at all or that ``planned_output``
    never declared a ``promotion`` block. Never guessed — mirrors every other
    ``effective_*`` accessor in this module."""
    planned = effective_planned_output(item)
    if not planned:
        return None
    promotion = planned.get("promotion")
    return promotion if isinstance(promotion, dict) else None


# ---------------------------------------------------------------------------
# 24f5146d — deterministic base-file hash preconditions.
#
# These functions are the read-time half of the SAME staleness contract
# ``tools/meridian_fallbacks/patch_manifest.py``'s ``PatchManifest.
# verify_base_unchanged`` / ``transactional_merge.apply_patch_manifest``
# already enforce at APPLY time (sibling item 4ff6ff22, reused verbatim —
# not reimplemented). ``check_promotion_preconditions`` lets a caller (a
# wave-planning step, a resume-staleness check, a pre-flight MCP call) ask
# "would this promotion's base hash still be considered fresh" WITHOUT
# constructing a full PatchManifest or touching transactional_merge at all —
# useful anywhere only the yes/no answer (and why) is needed, while the real
# atomic apply/rollback still always goes through transactional_merge, which
# re-checks the identical hash immediately before writing (defense in depth
# against a TOCTOU race between this read-only check and the real apply).
# ---------------------------------------------------------------------------

def compute_base_sha256(target_docx_path: "str | Path") -> "str | None":
    """The sha256 of ``target_docx_path``'s CURRENT on-disk bytes, or
    ``None`` when the file does not exist — mirrors
    ``PatchManifest.create_from_file``'s own "unknown base" semantics
    exactly (a promotion whose first operation will CREATE the file has no
    base to hash yet, which is a valid state, not an error).

    Reuses ``tools.meridian_fallbacks.safe_ooxml_writer.compute_sha256`` for
    the actual digest (imported lazily so this module never hard-depends on
    ``tools/`` being importable at module load time — mirrors this
    codebase's existing optional-sibling-import discipline, e.g.
    ``docx_integrity_gate.py``'s ``meridian_docs`` resolution). Falls back to
    a local ``hashlib.sha256`` (the exact same algorithm) if ``tools`` is not
    importable in this deployment, so the precondition check degrades
    gracefully rather than failing outright.
    """
    path = Path(target_docx_path)
    if not path.is_file():
        return None
    data = path.read_bytes()
    try:
        from tools.meridian_fallbacks.safe_ooxml_writer import (  # noqa: PLC0415
            compute_sha256 as _compute_sha256,
        )
    except Exception:  # noqa: BLE001 — tools/ not on the import path in this deployment
        import hashlib  # noqa: PLC0415
        return hashlib.sha256(data).hexdigest()
    return _compute_sha256(data)


# ---------------------------------------------------------------------------
# f6912e2d — "no ambiguous filename/label match authorizes promotion."
#
# check_promotion_preconditions (below) answers "is the base hash still
# fresh" — a real, necessary check, but one a promotion can satisfy while its
# ONLY declared identity is a bare target uri (a filename) plus that hash.
# A hash match proves the BYTES are the ones expected; it says nothing about
# WHICH structural element inside those bytes this promotion is entitled to
# touch — that is exactly the "ambiguous filename/label match" the
# acceptance criteria calls out. ``resource_footprint`` (24f5146d, see the
# constants block near _FOOTPRINT_PREFIXES above) already carries typed,
# unambiguous structural anchors (``paraId:``/``table:``/``figure:``/
# ``media:``/``part:``) for exactly this purpose — this function is the
# missing piece that actually REQUIRES at least one such anchor before a
# promotion counts as unambiguous, rather than leaving resource_footprint an
# optional field nothing ever checks for non-emptiness.
# ---------------------------------------------------------------------------

def check_no_ambiguous_promotion_match(item: dict[str, Any]) -> dict[str, Any]:
    """True (``ok``) iff this item's declared promotion (if any) identifies
    its target via a typed structural anchor, never a bare filename/label
    match alone.

    Returns ``{"ok": bool, "reason": str, "resource_footprint_count": int}``.

    * No ``promotion`` declared at all → ``ok=True`` (nothing to check —
      this function only judges promotions that exist).
    * ``promotion.resource_footprint`` has at least one entry → ``ok=True``
      (a base_sha256 match — or its absence — is not, by itself, judged
      here; this function only judges anchor SPECIFICITY).
    * ``promotion`` declared with an EMPTY ``resource_footprint`` →
      ``ok=False``: the promotion's only declared identity is a base hash
      against a bare target uri, which is exactly the ambiguous filename/
      label match this check exists to refuse.

    Never raises: a malformed/missing ``item`` or ``planned_output``
    degrades to ``ok=True`` via :func:`effective_promotion`'s own
    never-guessed ``None`` semantics (nothing declared, nothing to refuse).
    """
    promotion = effective_promotion(item)
    if promotion is None:
        return {
            "ok": True,
            "reason": "no promotion declared for this item — nothing to check",
            "resource_footprint_count": 0,
        }
    footprint = promotion.get("resource_footprint") or []
    count = len(footprint)
    if count:
        return {
            "ok": True,
            "reason": (
                f"promotion declares {count} typed structural anchor(s) "
                "(paraId:/table:/figure:/media:/part:) — not a bare "
                "filename/label match"
            ),
            "resource_footprint_count": count,
        }
    return {
        "ok": False,
        "reason": (
            "promotion declares no resource_footprint — its only declared "
            "identity is a base_sha256 hash against a bare target uri, "
            "which is exactly the ambiguous filename/label match this "
            "check exists to refuse. Add at least one typed "
            "resource_footprint anchor (paraId:/table:/figure:/media:/"
            "part:) before this promotion may be treated as unambiguous."
        ),
        "resource_footprint_count": 0,
    }


def check_promotion_preconditions(
    item: dict[str, Any],
    target_docx_path: "str | Path",
    *,
    require_resource_footprint: bool = False,
) -> dict[str, Any]:
    """Deterministic base-hash precondition check for a script->artifact->
    document promotion.

    Compares ``item``'s declared ``planned_output.promotion.base_sha256``
    (via :func:`effective_promotion`) against the TARGET's CURRENT on-disk
    hash (:func:`compute_base_sha256`). Returns::

        {"ok": bool, "reason": str, "declared_base_sha256", "current_base_sha256",
         "target_exists": bool, "target_docx_path": str,
         "no_ambiguous_match_check": {...}}

    ``ok=True`` when either (a) no ``base_sha256`` was declared at all — an
    "unknown base" is trivially unchanged, the SAME deliberate rule
    ``PatchManifest.verify_base_unchanged`` documents — or (b) the declared
    hash matches the current one. ``ok=False`` with an actionable
    ``reason`` on any mismatch, which is exactly the conflict-detection
    signal a caller (wave planning, resume staleness, a pre-flight check
    before enqueueing the real ``apply_patch_manifest`` call) needs before
    attempting to promote. This function never raises on a missing/garbled
    declaration and never touches disk beyond reading ``target_docx_path``.

    ``no_ambiguous_match_check`` (f6912e2d) — :func:`check_no_ambiguous_promotion_match`'s
    verdict is ALWAYS computed and attached (surface, never silently skip —
    the same convention ``artifact_policy``'s default "warn" level uses),
    but only FLIPS ``ok``/extends ``reason`` when
    ``require_resource_footprint=True`` is explicitly passed. Default
    ``False`` is fully backward compatible with every existing caller —
    this is an ADDITIVE, opt-in stricter gate, never a silent behavior
    change to the base-hash check that shipped in 24f5146d.
    """
    promotion = effective_promotion(item)
    declared = (promotion or {}).get("base_sha256")
    current = compute_base_sha256(target_docx_path)
    target_exists = current is not None
    match_check = check_no_ambiguous_promotion_match(item)

    if declared is None:
        result = {
            "ok": True,
            "reason": (
                "no base_sha256 declared for this promotion — an unknown "
                "base is trivially considered unchanged"
            ),
            "declared_base_sha256": None,
            "current_base_sha256": current,
            "target_exists": target_exists,
            "target_docx_path": str(target_docx_path),
        }
    else:
        ok = declared == current
        reason = (
            "base hash matches the current on-disk target — safe to promote"
            if ok
            else (
                f"base hash mismatch: promotion was declared against "
                f"{declared!r}, but the target's current hash is {current!r} "
                "— the file changed since this promotion was planned. Re-plan "
                "the promotion against the current file, or apply with "
                "allow_stale_base=True (transactional_merge.apply_patch_manifest) "
                "only if the change is understood and acceptable."
            )
        )
        result = {
            "ok": ok,
            "reason": reason,
            "declared_base_sha256": declared,
            "current_base_sha256": current,
            "target_exists": target_exists,
            "target_docx_path": str(target_docx_path),
        }

    result["no_ambiguous_match_check"] = match_check
    if require_resource_footprint and not match_check["ok"]:
        result["ok"] = False
        result["reason"] = f"{result['reason']} ALSO: {match_check['reason']}"
    return result


# ---------------------------------------------------------------------------
# 24f5146d — the ONE canonical merger lock.
#
# Reuses meridian.db.locks.claim_file / release_file / get_file_claims — the
# SAME cross-tool (Claude Code / Codex / Cursor) file-claim primitive
# AGENTS.md already documents for shared-file coordination — against a
# synthetic, namespaced path (see merger_lock_key_for_target). No new lock
# table, no schema change: a docx promotion target's merger lock is just
# another row in the existing file_locks table, which already has TTL
# expiry, atomic INSERT..ON CONFLICT DO NOTHING, and a read-only inspection
# path (get_file_claims) for free. Exactly ONE session may hold this lock for
# a given target at a time, across the WHOLE project — a second concurrent
# promotion attempt against the SAME target is refused (claimed=False),
# never silently interleaved with the first.
#
# These are thin async wrappers, not a new coordination mechanism — imported
# lazily (module-body import of meridian.db would be a real cycle: db/
# wave_runs.py and db/wave_resume.py both import THIS module, lazily, at
# call time, for the same reason).
# ---------------------------------------------------------------------------

async def acquire_promotion_merger_lock(
    db: Any, target_docx_path: "str | Path", session_id: str, *, ttl_hours: int = 2,
) -> dict[str, Any]:
    """Acquire the ONE canonical merger lock for ``target_docx_path``.

    A caller performing a real ``transactional_merge.apply_patch_manifest``
    call against a docx promotion target MUST hold this lock for the
    duration of that call (acquire before, release — see
    :func:`release_promotion_merger_lock` — after, success or failure).
    Returns the SAME shape :func:`meridian.db.locks.claim_file` returns:
    ``claimed=True`` on success, ``claimed=False`` with a ``reason``/
    ``holder_session_id`` when another live session already holds it.
    """
    from meridian.db import locks as _locks  # noqa: PLC0415 — avoid an import cycle
    key = merger_lock_key_for_target(str(target_docx_path))
    return await _locks.claim_file(db, key, session_id, mode="write", ttl_hours=ttl_hours)


async def release_promotion_merger_lock(
    db: Any, target_docx_path: "str | Path", session_id: str,
) -> bool:
    """Release the merger lock this ``session_id`` holds on
    ``target_docx_path``, if any. Mirrors :func:`meridian.db.locks.release_file`
    exactly (only releases a lock this session actually owns)."""
    from meridian.db import locks as _locks  # noqa: PLC0415
    key = merger_lock_key_for_target(str(target_docx_path))
    return await _locks.release_file(db, key, session_id)


async def get_promotion_merger_lock(
    db: Any, target_docx_path: "str | Path", project_id: "str | None" = None,
) -> dict[str, Any]:
    """Read-only check: is the merger lock for ``target_docx_path`` CURRENTLY
    held (by anyone)? Never acquires or mutates anything — used by callers
    (e.g. :class:`meridian.dispatcher.Dispatcher`) that want to avoid
    enqueuing a second worker against a target another live promotion is
    already mid-flight on, without themselves participating in the lock's
    acquire/release lifecycle. Same shape as
    :func:`meridian.db.locks.get_file_claims`: ``result["file_lock"]`` is the
    active claim (or ``None`` when free)."""
    from meridian.db import locks as _locks  # noqa: PLC0415
    key = merger_lock_key_for_target(str(target_docx_path))
    return await _locks.get_file_claims(db, key, project_id)


# ---------------------------------------------------------------------------
# f6912e2d — "every document/Outputs sprint item must carry an executable
# artifact recipe": the ONE composed completeness verdict tying together
# every field this module (and meridian.tool_requirements) already
# validates independently. Pure/sync, never raises — mirrors every other
# check_* function in this module.
# ---------------------------------------------------------------------------

def check_artifact_recipe_completeness(item: dict[str, Any]) -> dict[str, Any]:
    """Is ``item``'s artifact recipe genuinely EXECUTABLE, per the f6912e2d
    acceptance criteria — exact MCP tool names, an output pointer (existing
    vs. planned_new), a declared execution path / rollback policy / focused
    tests, and (when a promotion is declared) an unambiguous structural
    anchor?

    Only applies to items that declare an ``artifact_kind`` at all —
    :func:`effective_artifact_kind` is authoritative, mirroring
    ``artifact_classification.classify_artifact_work``'s own "declared kind
    wins" rule; an item with no declared kind gets ``applicable=False`` and
    ``complete=True`` (this check has nothing to say about it, and must
    never manufacture a finding for ordinary non-artifact work).

    Composes, without re-deriving any of them:

    * :func:`effective_planned_output` — the output pointer (its own
      ``target_kind`` already distinguishes existing vs. planned_new; its
      selector already carries a source module/symbol or DOCX node pointer
      for ``"symbol"``/``"node_id"`` selector types — see
      ``meridian.pointers``).
    * :func:`effective_artifact_recipe` — ``execution_path`` /
      ``rollback_policy`` / ``checks`` / ``focused_tests`` (f6912e2d, this
      module).
    * ``meridian.tool_requirements.effective_tool_requirements`` — exact MCP
      ``server_or_namespace``/``name`` (76dde31f; lazily imported to avoid a
      cycle, mirroring this module's other lazy DB/pointers imports).
    * :func:`check_no_ambiguous_promotion_match` — a declared promotion (if
      any) is judged for a real structural anchor, never a bare filename/
      label match.

    Returns::

        {"complete": bool, "applicable": bool, "artifact_kind": str | None,
         "missing": [str, ...], "reason": str | None}

    ``missing`` lists every absent/insufficient piece by name (never just
    the first one found) so a caller sees the whole gap in one call.
    ``reason`` is ``None`` exactly when ``complete`` is ``True``.
    """
    kind = effective_artifact_kind(item)
    if kind is None:
        return {
            "complete": True,
            "applicable": False,
            "artifact_kind": None,
            "missing": [],
            "reason": None,
        }

    missing: list[str] = []

    if not effective_planned_output(item):
        missing.append("planned_output")

    if not effective_artifact_recipe(item):
        missing.append("artifact_recipe")

    try:
        from . import tool_requirements as _tool_requirements  # noqa: PLC0415 — avoid import cycle
        tool_reqs = _tool_requirements.effective_tool_requirements(item)
    except Exception:  # noqa: BLE001 — a completeness check must never raise
        tool_reqs = []
    if not tool_reqs:
        missing.append("tool_requirements")

    match_check = check_no_ambiguous_promotion_match(item)
    if not match_check["ok"]:
        missing.append("promotion.resource_footprint (ambiguous filename/label match)")

    complete = not missing
    return {
        "complete": complete,
        "applicable": True,
        "artifact_kind": kind,
        "missing": missing,
        "reason": (
            None if complete
            else f"artifact recipe incomplete for a {kind!r} item — missing: " + ", ".join(missing)
        ),
    }
