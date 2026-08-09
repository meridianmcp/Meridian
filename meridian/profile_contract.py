"""Versioned hosted-default / scoped-profile contract (PROFILE-1 62c41508,
implemented here per PROFILE-2 d8481276).

Pure Pydantic models + merge algorithm. No DB, no network — see
``meridian.db.profile_layers`` for persistence and resolution against real
project/session data.

Five scope layers, least -> most specific::

    hosted_default -> workspace -> user -> project -> session

This generalizes the already-shipped :mod:`meridian.capability_profile`
``merge_layers`` chain (workspace -> user -> project) by bracketing it with a
``hosted_default`` floor and a ``session`` ceiling — see that module's own
docstring for the inner three layers' precedent. ``capability_profile.py``'s
finer ``sprint_version``/``item`` layers stay that module's own business for
capability-shaped fields (see the ``capability_manifest_ref`` field below,
which explicitly delegates to it) — not superseded here.

Design note on the "session/run" ceiling (62c41508 called this "session/run"):
this module implements a single ``session`` scope_type only (scope_id =
session_id). A finer per-run layer was left as an explicit open question by
62c41508 and is not implemented here — a genuine cross-item gap, not silently
dropped (see meridian.db.profile_layers module docstring and the PROFILE-2
handoff notes).

COMPOSITION (:func:`resolve_effective_profile`): per layer, in the order
given (least -> most specific), apply that layer's ``reset_fields`` (mirrors
``capability_profile``'s ``disabled_capability_ids`` — removes a field this
layer explicitly opts out of, previously set by an earlier layer) and THEN
apply that layer's own declared ``fields`` per each field's
``merge_strategy``. ``allowed_layers`` is enforced at WRITE time (see
:func:`validate_layer_fields` — raise, never silent drop). ``narrow_only`` +
``safe_direction`` are enforced during merge: a more-specific layer widening a
safety dial (e.g. ``hitl_auto_answer`` toward more-automatic) is rejected into
``blocked_widens`` unless ``override_reason`` is explicitly supplied to
:func:`resolve_effective_profile` — this directly closes the
HITL-suppression-injection risk this project has already hit once (see
project memory ``feedback_hitl_suppression_injection``).
``generation_key = sha256(canonical_json(every contributing layer's
(scope_type, scope_id, revision)))`` — content-addressed, no separate
invalidation ledger needed: a write is automatically a new generation_key.

VERSIONING: two counters, distinct on purpose.

* ``SCHEMA_VERSION`` — envelope shape, breaking-only, mirrors
  :data:`meridian.ai_log.EVENT_SCHEMA_VERSION`'s discipline. Starts at 1;
  bump only for an incompatible ``ProfileLayer``/``EffectiveProfile`` shape
  change, never for adding an optional field or a new registry entry.
* ``revision`` — per ``(scope_type, scope_id)`` row, content-hash ledgered
  exactly like ``meridian.db.board_snapshot``'s revision counter: an
  idempotent no-op resave (identical fields/reset_fields) never bumps it. See
  ``meridian.db.profile_layers.set_profile_layer``.

``generation_key`` is DERIVED at resolve time, never stored.

LIFECYCLE: ``hosted_default`` ONLY (the one layer that's "immutable once
published") gets ``draft -> {active, retired}``, ``active -> {deprecated}``,
``deprecated -> {retired, active}``, ``retired -> {}`` (terminal, audit-only)
— mirrors ``meridian.db.workspace._PROPOSAL_TRANSITIONS``'s explicit style.
The other four layers stay the existing binary row-exists-or-not (matches
``capability_profile.set_capability_profile`` today). See
:data:`LIFECYCLE_TRANSITIONS`.

OPTIMISTIC CONCURRENCY: ``set_profile_layer(expected_revision=None)`` is
last-write-wins (matches ``capability_profile.set_capability_profile``'s
current behavior — nothing existing breaks); ``expected_revision`` given and
stale raises :class:`ProfileStaleRevisionError` (mirrors
``meridian.db.wave_runs.finalize_wave_run``'s ``expected_revision_hash``
gate).

FIELD REGISTRY (:data:`FIELD_REGISTRY`) is grounded in real
``ProjectSettings``/``ExecutorConfig`` fields (see ``meridian.models``), not
placeholders. Fields whose ``legacy_source`` is ``"project_settings"`` keep
flowing through the EXISTING ``meridian.db.get_project_settings`` /
``update_project_settings`` + ``projects`` table columns at the ``project``
scope — ``profile_layers`` rows at ``scope_type="project"`` carry ONLY the 3
genuinely-new fields (``tool_priority_map``, ``capability_manifest_ref``,
``claim_verification_mode`` — ``legacy_source="profile_layers"``). This is
the concrete mechanism satisfying "do not create a parallel
executor_config/settings authority" — see
``meridian.db.profile_layers.get_effective_profile``, which is the only
place the two sources are stitched back together into one ``project`` layer.
Prohibited universally: secret-shaped strings (reuses
``capability_manifest._SECRET_LIKE_RE`` verbatim). Prohibited at every layer
EXCEPT a field's own ``path_allowed_from_layer`` list: absolute local paths
(reuses ``capability_manifest._ABSOLUTE_PATH_RE`` verbatim).
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from meridian import capability_profile as _capability_profile
from meridian.capability_manifest import (
    CapabilityManifestError,
    _ABSOLUTE_PATH_RE,
    _SECRET_LIKE_RE,
    normalize_manifest as _normalize_capability_manifest,
)

#: Envelope schema version this module writes — breaking-only, mirrors
#: meridian.ai_log.EVENT_SCHEMA_VERSION's discipline.
SCHEMA_VERSION = 1

ScopeType = Literal["hosted_default", "workspace", "user", "project", "session"]
#: Least specific -> most specific. A field declared by a later (more
#: specific) layer overrides the same field declared by an earlier one,
#: exactly like meridian.capability_profile.SCOPE_TYPES' inner three layers.
SCOPE_TYPES: tuple[str, ...] = ("hosted_default", "workspace", "user", "project", "session")

MergeStrategy = Literal["scalar_override", "dict_merge_by_key", "list_replace_via_capability_profile"]
RestartClass = Literal["hot_reload", "explicit_refresh_required", "restart_required"]
LifecycleState = Literal["draft", "active", "deprecated", "retired"]
SafeDirection = Literal["increase", "decrease"]
#: Restart/refresh-report bucket a field's changes are classified under.
#: Folded in from profile_resolution.py (ac95d206) during the
#: PROFILE-RECON reconciliation (732c113e) — see ProfileFieldSpec.component
#: and _compute_restart_report below.
RestartComponent = Literal["tunnel", "connector", "capability", "general"]

LIFECYCLE_STATES: tuple[str, ...] = ("draft", "active", "deprecated", "retired")

#: hosted_default lifecycle state machine — mirrors
#: meridian.db.workspace._PROPOSAL_TRANSITIONS's explicit dict-of-sets style.
LIFECYCLE_TRANSITIONS: dict[str, set[str]] = {
    "draft": {"active", "retired"},
    "active": {"deprecated"},
    "deprecated": {"retired", "active"},
    "retired": set(),  # terminal
}

#: Lifecycle states whose hosted_default layer is live/authoritative during
#: resolve_effective_profile. "deprecated" still participates (it's still in
#: effect, just marked for eventual retirement); "draft" (not yet published)
#: and "retired" (terminal) do not.
LIVE_HOSTED_DEFAULT_STATES: frozenset[str] = frozenset({"active", "deprecated"})


class ProfileContractError(ValueError):
    """Raised on profile-layer schema/precedence/safety violations."""


class ProfileStaleRevisionError(ProfileContractError):
    """Raised when a caller-supplied ``expected_revision`` no longer matches
    the stored row (mirrors ``wave_runs.finalize_wave_run``'s
    ``expected_revision_hash`` gate)."""

    def __init__(self, scope_type: str, scope_id: str, expected: int | None, actual: int | None) -> None:
        self.scope_type = scope_type
        self.scope_id = scope_id
        self.expected_revision = expected
        self.actual_revision = actual
        super().__init__(
            f"stale revision for ({scope_type}, {scope_id}): expected "
            f"{expected!r}, actual is {actual!r}. Re-read the layer and "
            "reconcile before writing."
        )


def normalize_scope_type(scope_type: Any) -> str:
    """Validate and lowercase a scope_type; raises on anything else."""
    if not isinstance(scope_type, str) or scope_type.strip().lower() not in SCOPE_TYPES:
        raise ProfileContractError(f"scope_type must be one of {list(SCOPE_TYPES)}, got {scope_type!r}")
    return scope_type.strip().lower()


def normalize_scope_id(scope_id: Any) -> str:
    """Validate a scope_id: a required non-empty string."""
    if not isinstance(scope_id, str) or not scope_id.strip():
        raise ProfileContractError("scope_id must be a non-empty string")
    return scope_id.strip()


def normalize_reset_fields(raw: Any) -> list[str]:
    """Validate+normalize a layer's reset-field list (mirrors
    capability_profile.normalize_disabled_capability_ids: deterministic,
    sorted, deduped)."""
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
        raise ProfileContractError("reset_fields must be a list of strings")
    return sorted({x.strip() for x in raw if x.strip()})


def _check_value_safety(value: Any, *, path: str, allow_absolute_path: bool) -> None:
    """Recursively reject secret-shaped strings, and (unless
    ``allow_absolute_path``) absolute machine-local paths. Reuses
    capability_manifest's regexes verbatim, per this module's contract."""
    if isinstance(value, str):
        if _SECRET_LIKE_RE.search(value):
            raise ProfileContractError(f"{path}: secret-shaped value not allowed in shared profile state")
        if not allow_absolute_path and _ABSOLUTE_PATH_RE.search(value):
            raise ProfileContractError(
                f"{path}: machine-local absolute path not allowed at this layer: {value!r}"
            )
    elif isinstance(value, dict):
        for key, sub in value.items():
            _check_value_safety(sub, path=f"{path}.{key}", allow_absolute_path=allow_absolute_path)
    elif isinstance(value, (list, tuple)):
        for idx, sub in enumerate(value):
            _check_value_safety(sub, path=f"{path}[{idx}]", allow_absolute_path=allow_absolute_path)


#: Fields whose string value is a shell/deploy command -- subject to the
#: unsafe-command heuristic below. Folded in from profile_resolution.py
#: (ac95d206) during the PROFILE-RECON reconciliation (732c113e): a
#: conservative, documented deny-list, defense in depth only -- same spirit
#: as the secret-shaped/absolute-path checks above.
_COMMAND_SHAPED_FIELDS = frozenset({"executor_config.test_cmd", "executor_config.deploy_cmd"})

_UNSAFE_COMMAND_RE = re.compile(
    r"(?i)("
    r"rm\s+-rf\s+[/~]|"
    r"del\s+/[fsq]{1,3}\s|"
    r"remove-item\s+-recurse\s+-force\s+[a-z]:\\|"
    r"format\s+[a-z]:|"
    r"mkfs\.|"
    r"dd\s+if=.*of=/dev/|"
    r">\s*/dev/sd|"
    r"shutdown\b|reboot\b|"
    r"sudo\s+rm\b|"
    r"curl[^|]*\|\s*(sh|bash)\b|"
    r"wget[^|]*\|\s*(sh|bash)\b|"
    r"drop\s+(database|table)\b|"
    r":\(\)\s*\{\s*:\|\s*:&\s*\}\s*;\s*:|"
    r"git\s+push\s+--force\b"
    r")"
)


def _check_unsafe_command(value: Any, *, path: str) -> None:
    """Reject a shell/deploy-command-shaped field value matching a
    conservative destructive-command deny-list. Folded in from
    profile_resolution.py (ac95d206) verbatim during the PROFILE-RECON
    reconciliation (732c113e)."""
    if isinstance(value, str) and _UNSAFE_COMMAND_RE.search(value):
        raise ProfileContractError(f"{path}: unsafe/destructive command pattern not allowed")


#: Python types backing each FIELD_REGISTRY "type" string -- used by
#: _check_field_type. Folded in from profile_resolution.py (ac95d206)
#: during the PROFILE-RECON reconciliation (732c113e): profile_contract.py
#: previously only recorded ``type`` as an informational string and never
#: actually checked a written value against it.
_PY_TYPES: dict[str, type] = {"int": int, "str": str, "dict": dict, "list": list}


def _check_field_type(spec: ProfileFieldSpec, value: Any, *, path: str) -> None:
    """Reject a value whose Python type doesn't match its field's registry
    ``type``. ``bool`` is explicitly rejected for an ``int`` field (Python's
    ``bool`` is an ``int`` subclass, which would otherwise silently pass)."""
    py_type = _PY_TYPES.get(spec.type)
    if py_type is None:
        return  # unrecognized/informational type string -- nothing to check
    if spec.type == "int" and isinstance(value, bool):
        raise ProfileContractError(f"{path}: expected int, got bool")
    if not isinstance(value, py_type):
        raise ProfileContractError(f"{path}: expected {spec.type}, got {type(value).__name__}")


def normalize_provenance(raw: Any) -> dict[str, Any] | None:
    """Validate the layer-level provenance blob — same non-secret,
    non-machine-local-path contract as capability_profile.normalize_provenance
    (this state is project/workspace-shared, multi-machine)."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ProfileContractError("provenance must be an object or null")
    _check_value_safety(raw, path="profile.provenance", allow_absolute_path=False)
    return raw


class ProfileFieldSpec(BaseModel):
    """One field's registry entry: type, allowed layers, merge behavior,
    default, restart semantics, and optional narrow-only safety-dial gate."""

    name: str
    type: str = Field(description="Informational python-shaped type name: int|str|dict|list.")
    allowed_layers: list[str]
    merge_strategy: MergeStrategy = "scalar_override"
    default: Any = None
    restart_class: RestartClass = "hot_reload"
    #: Which restart/refresh-report bucket (tunnel/connector/capability/
    #: general) this field's changes are classified under. Folded in from
    #: profile_resolution.py (ac95d206) during the PROFILE-RECON
    #: reconciliation (732c113e) — see _compute_restart_report. "tunnel" is
    #: not used by any field today (reserved for when tunnel.py's
    #: has_active_tunnel()-gated fields land) but still appears in every
    #: restart_report so callers never special-case its absence.
    component: RestartComponent = "general"
    narrow_only: bool = False
    safe_direction: SafeDirection | None = None
    #: Layers at which an absolute machine-local path is permitted in this
    #: field's value (e.g. executor_config.repo_path at project/session).
    #: Empty = never permitted anywhere for this field.
    path_allowed_from_layer: list[str] = Field(default_factory=list)
    #: "project_settings" — this field's `project`-scope value is NOT read
    #: from a profile_layers row; it flows through the existing
    #: meridian.db.get_project_settings/update_project_settings authority.
    #: "profile_layers" — genuinely new field, stored in profile_layers rows
    #: at every layer including project.
    legacy_source: Literal["profile_layers", "project_settings"] = "profile_layers"

    @field_validator("allowed_layers")
    @classmethod
    def _validate_allowed_layers(cls, v: list[str]) -> list[str]:
        bad = [s for s in v if s not in SCOPE_TYPES]
        if bad:
            raise ValueError(f"unknown scope_type(s) in allowed_layers: {bad}")
        return v

    @field_validator("path_allowed_from_layer")
    @classmethod
    def _validate_path_allowed_from_layer(cls, v: list[str]) -> list[str]:
        bad = [s for s in v if s not in SCOPE_TYPES]
        if bad:
            raise ValueError(f"unknown scope_type(s) in path_allowed_from_layer: {bad}")
        return v


_ALL_LAYERS = list(SCOPE_TYPES)
_EXEC_CFG_PATH_LAYERS = ["project", "session"]


def _spec(**kwargs: Any) -> ProfileFieldSpec:
    return ProfileFieldSpec(**kwargs)


#: The field registry — grounded in real ProjectSettings/ExecutorConfig
#: fields (meridian.models) plus the 3 genuinely-new PROFILE-1 fields.
FIELD_REGISTRY: dict[str, ProfileFieldSpec] = {
    # --- 6 existing top-level ProjectSettings scalar fields -----------------
    "max_pinned_decisions": _spec(
        name="max_pinned_decisions", type="int", allowed_layers=_ALL_LAYERS,
        merge_strategy="scalar_override", default=20, restart_class="hot_reload",
        component="general", legacy_source="project_settings",
    ),
    "hitl_auto_answer": _spec(
        name="hitl_auto_answer", type="int", allowed_layers=_ALL_LAYERS,
        merge_strategy="scalar_override", default=0, restart_class="hot_reload",
        component="general", narrow_only=True, safe_direction="decrease",
        legacy_source="project_settings",
    ),
    "auto_worktrees": _spec(
        name="auto_worktrees", type="int", allowed_layers=_ALL_LAYERS,
        merge_strategy="scalar_override", default=1, restart_class="hot_reload",
        component="general", legacy_source="project_settings",
    ),
    "require_merge_approval": _spec(
        name="require_merge_approval", type="int", allowed_layers=_ALL_LAYERS,
        merge_strategy="scalar_override", default=1, restart_class="hot_reload",
        component="general", narrow_only=True, safe_direction="increase",
        legacy_source="project_settings",
    ),
    "code_intel_enabled": _spec(
        name="code_intel_enabled", type="int", allowed_layers=_ALL_LAYERS,
        merge_strategy="scalar_override", default=0, restart_class="hot_reload",
        component="general", legacy_source="project_settings",
    ),
    "execution_mode": _spec(
        name="execution_mode", type="str", allowed_layers=_ALL_LAYERS,
        merge_strategy="scalar_override", default="autonomous",
        restart_class="explicit_refresh_required", component="general",
        legacy_source="project_settings",
    ),
    # --- executor_config.* sub-fields (the 7th ProjectSettings field) ------
    "executor_config.repo_path": _spec(
        name="executor_config.repo_path", type="str", allowed_layers=["project", "session"],
        merge_strategy="scalar_override", default=None, restart_class="restart_required",
        component="connector", path_allowed_from_layer=_EXEC_CFG_PATH_LAYERS,
        legacy_source="project_settings",
    ),
    "executor_config.repo_paths": _spec(
        name="executor_config.repo_paths", type="list", allowed_layers=["project", "session"],
        merge_strategy="scalar_override", default=None, restart_class="restart_required",
        component="connector", path_allowed_from_layer=_EXEC_CFG_PATH_LAYERS,
        legacy_source="project_settings",
    ),
    "executor_config.env_file": _spec(
        name="executor_config.env_file", type="str", allowed_layers=["project", "session"],
        merge_strategy="scalar_override", default=None, restart_class="restart_required",
        component="connector", path_allowed_from_layer=_EXEC_CFG_PATH_LAYERS,
        legacy_source="project_settings",
    ),
    "executor_config.test_cmd": _spec(
        name="executor_config.test_cmd", type="str", allowed_layers=_ALL_LAYERS,
        merge_strategy="scalar_override", default=None, restart_class="hot_reload",
        component="connector", path_allowed_from_layer=_EXEC_CFG_PATH_LAYERS,
        legacy_source="project_settings",
    ),
    "executor_config.test_min": _spec(
        name="executor_config.test_min", type="int", allowed_layers=_ALL_LAYERS,
        merge_strategy="scalar_override", default=None, restart_class="hot_reload",
        component="connector", narrow_only=True, safe_direction="increase",
        legacy_source="project_settings",
    ),
    "executor_config.deploy_cmd": _spec(
        name="executor_config.deploy_cmd", type="str", allowed_layers=_ALL_LAYERS,
        merge_strategy="scalar_override", default=None, restart_class="hot_reload",
        component="connector", path_allowed_from_layer=_EXEC_CFG_PATH_LAYERS,
        legacy_source="project_settings",
    ),
    "executor_config.shell_type": _spec(
        name="executor_config.shell_type", type="str", allowed_layers=_ALL_LAYERS,
        merge_strategy="scalar_override", default=None, restart_class="hot_reload",
        component="connector", legacy_source="project_settings",
    ),
    "executor_config.branch": _spec(
        name="executor_config.branch", type="str", allowed_layers=_ALL_LAYERS,
        merge_strategy="scalar_override", default=None, restart_class="hot_reload",
        component="connector", legacy_source="project_settings",
    ),
    # --- 3 genuinely-new PROFILE-1 fields ------------------------------------
    "claim_verification_mode": _spec(
        name="claim_verification_mode", type="str", allowed_layers=_ALL_LAYERS,
        merge_strategy="scalar_override", default="advisory", restart_class="hot_reload",
        component="connector", narrow_only=True, safe_direction="increase",
        legacy_source="profile_layers",
    ),
    "tool_priority_map": _spec(
        name="tool_priority_map", type="dict", allowed_layers=_ALL_LAYERS,
        merge_strategy="dict_merge_by_key", default=None, restart_class="hot_reload",
        component="capability", legacy_source="profile_layers",
    ),
    "capability_manifest_ref": _spec(
        name="capability_manifest_ref", type="list", allowed_layers=_ALL_LAYERS,
        merge_strategy="list_replace_via_capability_profile", default=None,
        restart_class="explicit_refresh_required", component="capability",
        legacy_source="profile_layers",
    ),
}

#: claim_verification_mode's ordered advisory->strict scale, used only to
#: interpret safe_direction="increase" for this one non-numeric field.
_CLAIM_VERIFICATION_MODE_ORDER: dict[str, int] = {"off": 0, "advisory": 1, "strict": 2}


def validate_layer_fields(
    scope_type: str,
    fields: dict[str, Any] | None,
    *,
    reset_fields: list[str] | None = None,
    field_registry: dict[str, ProfileFieldSpec] | None = None,
) -> None:
    """Validate a layer write BEFORE persistence — raise, never silent drop.

    Checks: scope_type is valid, every ``reset_fields`` name exists in the
    registry (folded in from profile_resolution.py's validate_profile_layer
    during the second PROFILE-RECON re-verification pass, 732c113e --
    previously a reset_fields entry naming an unknown field silently no-opped
    instead of raising), every ``fields`` name exists in the registry, the
    layer's scope_type is in that field's ``allowed_layers``, the value
    is not ``None`` (use ``reset_fields`` instead), the value's Python type
    matches the field's registry ``type``, the value carries no
    secret-shaped string or (outside ``path_allowed_from_layer``)
    machine-local absolute path, no unsafe/destructive shell command (for
    ``executor_config.test_cmd``/``deploy_cmd``), and -- for
    ``capability_manifest_ref`` -- a structurally valid capability contract
    (delegates to ``capability_manifest.normalize_manifest``). The null,
    type, unsafe-command, and capability-contract checks were folded in from
    profile_resolution.py (ac95d206) during the PROFILE-RECON reconciliation
    (732c113e) — profile_contract.py previously only checked field/layer
    existence, the legacy-authority guard, and secret/path safety.
    """
    registry = field_registry or FIELD_REGISTRY
    scope_type = normalize_scope_type(scope_type)
    for reset_field in reset_fields or []:
        if reset_field not in registry:
            raise ProfileContractError(f"reset_fields: unknown profile field: {reset_field!r}")
    for field_name, value in (fields or {}).items():
        spec = registry.get(field_name)
        if spec is None:
            raise ProfileContractError(f"unknown profile field: {field_name!r}")
        if scope_type not in spec.allowed_layers:
            raise ProfileContractError(
                f"field {field_name!r} is not writable at layer {scope_type!r} "
                f"(allowed_layers={spec.allowed_layers})"
            )
        if scope_type == "project" and spec.legacy_source == "project_settings":
            # Zero-duplication guard: this field's project-scope value is the
            # EXISTING get_project_settings/update_project_settings authority
            # (see meridian.db.profile_layers module docstring) — writing it
            # into a profile_layers row at scope_type="project" would create
            # exactly the parallel settings authority PROFILE-2 is required
            # not to create, and would silently win at resolve time (a
            # profile_layers row is applied AFTER the legacy-settings seed —
            # see get_effective_profile). Reject at write time instead.
            raise ProfileContractError(
                f"field {field_name!r} is sourced from the existing project "
                "settings authority at the project layer (see "
                "meridian.db.update_project_settings) — profile_layers rows "
                "at scope_type='project' may only carry genuinely-new fields "
                "(legacy_source='profile_layers')."
            )
        if value is None:
            raise ProfileContractError(
                f"field {field_name!r}: null is not a valid override value -- use "
                "reset_fields to clear a field back to its default"
            )
        path = f"profile.{field_name}"
        _check_field_type(spec, value, path=path)
        allow_path = scope_type in spec.path_allowed_from_layer
        _check_value_safety(value, path=path, allow_absolute_path=allow_path)
        if field_name in _COMMAND_SHAPED_FIELDS:
            _check_unsafe_command(value, path=path)
        if field_name == "capability_manifest_ref" and value:
            try:
                _normalize_capability_manifest(value)
            except CapabilityManifestError as exc:
                raise ProfileContractError(f"{path}: invalid capability contract: {exc}") from exc


class ProfileLayer(BaseModel):
    """One persisted (scope_type, scope_id) row's contribution."""

    scope_type: str
    scope_id: str
    schema_version: int = SCHEMA_VERSION
    revision: int = 0
    fields: dict[str, Any] = Field(default_factory=dict)
    reset_fields: list[str] = Field(default_factory=list)
    #: Only meaningful for scope_type == "hosted_default"; None elsewhere.
    lifecycle_state: str | None = None
    provenance: dict[str, Any] | None = None
    updated_at: str | None = None

    @field_validator("scope_type")
    @classmethod
    def _validate_scope_type(cls, v: str) -> str:
        return normalize_scope_type(v)


class EffectiveProfile(BaseModel):
    """The merged view resolve_effective_profile returns."""

    project_id: str | None = None
    session_id: str | None = None
    schema_version: int = SCHEMA_VERSION
    fields: dict[str, Any] = Field(default_factory=dict)
    field_sources: dict[str, str] = Field(default_factory=dict)
    overrides: list[dict[str, Any]] = Field(default_factory=list)
    blocked_widens: list[dict[str, Any]] = Field(default_factory=list)
    reset_log: list[dict[str, Any]] = Field(default_factory=list)
    layers_applied: list[str] = Field(default_factory=list)
    generation_key: str = ""
    refresh_required: bool = False
    #: Per-field ``{"old": ..., "new": ...}`` diff report. Folded in from
    #: profile_resolution.py's ``EffectiveProfile.changed_fields``
    #: (ac95d206) during the PROFILE-RECON reconciliation (732c113e) --
    #: this was the one piece dropped by the initial reconciliation pass
    #: (caught on re-verification). See _compute_changed_fields.
    #:
    #: When ``previous_fields`` is supplied to resolve_effective_profile,
    #: this piggybacks on the exact same touched-field-name comparison
    #: already used for ``refresh_required``/``restart_report`` -- each
    #: entry's "old"/"new" come straight from ``previous_fields``/
    #: ``effective`` via the same ``.get()`` calls, so a field absent from
    #: ``previous_fields`` reports ``old=None`` (matching this module's
    #: sparse-``fields`` convention -- an untouched field is simply absent,
    #: not defaulted -- unlike profile_resolution.py's registry-defaults
    #: -pre-seeded ``effective``; see test_profile_contract.py's
    #: "documented divergences" section). When ``previous_fields`` is
    #: omitted, every field actually set by some layer is diffed against
    #: its FIELD_REGISTRY default instead (mirrors profile_resolution.py's
    #: "no baseline -> diff against defaults" behavior, including that a
    #: layer re-declaring its own default value still counts as changed --
    #: it was still explicitly set by a layer, not silently defaulted).
    changed_fields: dict[str, dict[str, Any]] = Field(default_factory=dict)
    #: Per-component (tunnel/connector/capability/general) restart/refresh
    #: severity among CHANGED fields — "none" | "hot_reload" |
    #: "explicit_refresh_required" | "restart_required". Folded in from
    #: profile_resolution.py (ac95d206) during the PROFILE-RECON
    #: reconciliation (732c113e); see _compute_restart_report. Only
    #: meaningful when ``previous_fields`` was supplied to
    #: resolve_effective_profile — otherwise every component reports "none"
    #: and ``restart_required`` is False, mirroring ``refresh_required``'s
    #: own "nothing to diff without a baseline" contract.
    restart_report: dict[str, str] = Field(default_factory=dict)
    restart_required: bool = False
    #: Whether this resolution is safe to act on at all -- False only when a
    #: hosted_default floor is "retired" (terminal, no longer authoritative).
    #: Folded in from profile_resolution.py's EffectiveProfile.executable
    #: (ac95d206) during the PROFILE-RECON re-verification pass (732c113e,
    #: second verification round) -- the one piece the first re-verification
    #: pass (which restored changed_fields) still missed. See
    #: resolve_effective_profile's docstring for why this field only ever
    #: reflects the ``blocked_widens``-driven half of the original signal;
    #: the hosted_default-lifecycle half is computed by
    #: ``db.profile_layers.get_effective_profile`` instead (this module is
    #: pure/storage-free and, by design, never receives a non-live
    #: hosted_default layer to begin with -- see that module's docstring).
    executable: bool = True
    executable_reasons: list[str] = Field(default_factory=list)
    #: True when the resolution is usable but running under a caveat (a
    #: draft/deprecated hosted_default floor, or a narrow_only widen that got
    #: rejected into blocked_widens). Unlike ``executable``, a degraded
    #: resolution is still safe to act on.
    degraded: bool = False
    degraded_reasons: list[str] = Field(default_factory=list)


def _direction_is_safe(old: Any, new: Any, safe_direction: str, field_name: str) -> bool:
    """True when moving from ``old`` to ``new`` is a narrowing (safe) move.

    Numeric fields compare directly. ``claim_verification_mode`` is the one
    non-numeric narrow_only field — compared via its ordered scale. Any other
    non-numeric/non-comparable pair is conservatively treated as unsafe
    (any change requires override_reason) unless the values are equal.
    """
    if old is None:
        return True  # nothing accumulated yet — nothing to narrow away from
    if field_name == "claim_verification_mode":
        old_rank = _CLAIM_VERIFICATION_MODE_ORDER.get(old)
        new_rank = _CLAIM_VERIFICATION_MODE_ORDER.get(new)
        if old_rank is None or new_rank is None:
            return old == new
        old, new = old_rank, new_rank
    elif not isinstance(old, (int, float)) or isinstance(old, bool) or not isinstance(new, (int, float)) or isinstance(new, bool):
        return old == new
    if safe_direction == "decrease":
        return new <= old
    if safe_direction == "increase":
        return new >= old
    return True


def _compute_generation_key(contributing: list[tuple[str, str, int]]) -> str:
    payload = sorted(contributing)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


_RESTART_SEVERITY: dict[str, int] = {"hot_reload": 1, "explicit_refresh_required": 2, "restart_required": 3}
_SEVERITY_LABEL: dict[int, str] = {0: "none", 1: "hot_reload", 2: "explicit_refresh_required", 3: "restart_required"}
_RESTART_COMPONENTS: tuple[str, ...] = ("tunnel", "connector", "capability", "general")


def _compute_restart_report(
    changed_field_names: set[str], registry: dict[str, ProfileFieldSpec]
) -> tuple[dict[str, str], bool]:
    """Per-component (tunnel/connector/capability/general) classification of
    the most severe ``restart_class`` among ``changed_field_names``. Folded
    in from profile_resolution.py's ``_compute_restart_report`` (ac95d206)
    during the PROFILE-RECON reconciliation (732c113e).

    ``tunnel`` is always "none" in today's registry — no field is currently
    classified tunnel-component; reserved for when routes/tunnel.py's
    ``has_active_tunnel()``-gated fields land. The component still appears
    in the report (not omitted) so a caller's shape never has to
    special-case its absence.
    """
    severities: dict[str, int] = {c: 0 for c in _RESTART_COMPONENTS}
    for field_name in changed_field_names:
        spec = registry.get(field_name)
        if spec is None:
            continue
        sev = _RESTART_SEVERITY[spec.restart_class]
        if sev > severities[spec.component]:
            severities[spec.component] = sev
    report = {component: _SEVERITY_LABEL[sev] for component, sev in severities.items()}
    restart_required = any(v == "restart_required" for v in report.values())
    return report, restart_required


def resolve_effective_profile(
    layers: list[ProfileLayer],
    *,
    field_registry: dict[str, ProfileFieldSpec] | None = None,
    override_reason: str | None = None,
    previous_fields: dict[str, Any] | None = None,
) -> EffectiveProfile:
    """Merge an ordered (least -> most specific) list of layers.

    ``layers`` should already be filtered to layers that actually apply to
    this resolution (e.g. a session layer only when a session_id was given) —
    a layer with no fields and no reset_fields is skipped and does not appear
    in ``layers_applied``, mirroring capability_profile's own "no applicable
    scope_id -> skip" convention. ``layers`` may be passed in any order —
    they are re-sorted here to ``SCOPE_TYPES`` order (least -> most
    specific) before merging, so an out-of-order caller can never
    accidentally let a less-specific layer win by list position. (Folded in
    from profile_resolution.py during the PROFILE-RECON reconciliation,
    732c113e; every existing caller already passes layers in scope order, so
    this is purely additive robustness.)

    ``override_reason``, when given, allows every narrow_only widen in this
    resolution through (still logged in ``blocked_widens`` with
    ``overridden: True``) — it is an all-or-nothing knob for one resolve
    call, matching set_profile_layer's identically-named parameter; callers
    that need per-field overrides should resolve twice (isolate the widening
    field into its own layer).

    ``previous_fields``, when given, is compared against the newly resolved
    ``fields`` for every ``explicit_refresh_required`` field to compute
    ``refresh_required`` (per PROFILE-1: "refresh_required = generation_key
    changed AND diff touches an explicit_refresh_required field"). Omitted
    (the common case — a first resolution, or a caller not tracking history)
    -> ``refresh_required`` is always False; there is nothing to diff. The
    same ``previous_fields`` diff also feeds ``restart_report``/
    ``restart_required`` (see :func:`_compute_restart_report`) — folded in
    from profile_resolution.py during the PROFILE-RECON reconciliation
    (732c113e).

    ``changed_fields`` (also folded in from profile_resolution.py during
    the PROFILE-RECON reconciliation, 732c113e) is always populated, in one
    of two modes: with ``previous_fields`` given, it reuses that same
    touched-field-name diff verbatim (one ``{"old", "new"}`` entry per name
    in ``changed_field_names``); without it, every field this resolution
    actually set is diffed against its FIELD_REGISTRY default instead. See
    the field's own docstring on :class:`EffectiveProfile` for the exact
    contract.

    ``executable``/``executable_reasons``/``degraded``/``degraded_reasons``
    (folded in from profile_resolution.py's EffectiveProfile.executable/
    degraded during the second PROFILE-RECON re-verification pass,
    732c113e) report only the HALF of the original signal this function can
    actually see: a ``narrow_only`` widen rejected into ``blocked_widens``
    always marks the resolution ``degraded`` (``"narrow_only_widen_blocked"``).
    The other half of the original signal -- a hosted_default floor whose
    ``lifecycle_state`` is ``"retired"`` (not ``executable`` at all) or
    ``"draft"``/``"deprecated"`` (``degraded`` but still ``executable``) --
    is deliberately NOT computed here, because this module is pure and
    storage-free: it only ever sees the layers its caller hands it, and
    ``db.profile_layers.get_effective_profile`` already OMITS a non-live
    (draft/retired) hosted_default layer from ``layers`` entirely before
    calling this function (see that module's docstring and
    ``LIVE_HOSTED_DEFAULT_STATES``) -- a design predating this fix, already
    relied upon by ``test_get_effective_profile_hosted_default_draft_does_not_apply``,
    and deliberately NOT changed here (passing every layer through
    regardless of lifecycle_state so this function could compute the whole
    signal uniformly was considered and rejected: it would mean a
    retired/draft hosted_default's field VALUES start flowing into
    ``fields`` again, which is exactly the outcome the pre-filter exists to
    prevent -- narrowing the safety property to "caller must remember to
    check ``executable``" instead of "the merge never sees the row at all").
    ``db.profile_layers.get_effective_profile`` therefore computes the
    hosted_default-lifecycle half itself, from the raw row it already reads
    before filtering, and folds it into this function's ``executable``/
    ``degraded``/``*_reasons`` on the returned dict -- see that function.
    """
    registry = field_registry or FIELD_REGISTRY
    ordered_layers = sorted(layers, key=lambda layer: SCOPE_TYPES.index(layer.scope_type))
    effective: dict[str, Any] = {}
    sources: dict[str, str] = {}
    overrides: list[dict[str, Any]] = []
    blocked_widens: list[dict[str, Any]] = []
    reset_log: list[dict[str, Any]] = []
    layers_applied: list[str] = []
    contributing: list[tuple[str, str, int]] = []

    for layer in ordered_layers:
        if not layer.fields and not layer.reset_fields:
            continue
        # Envelope/structural checks -- same tier as the "unknown profile
        # field"/"not allowed at layer" checks a few lines down (both
        # pre-date this pass and already run at resolve time regardless of
        # the "no content revalidation" divergence documented above; that
        # divergence is about secrets/paths/unsafe-commands/types, not
        # envelope shape). Folded in from profile_resolution.py's
        # validate_profile_layer during the second PROFILE-RECON
        # re-verification pass (732c113e) -- previously ZERO equivalent
        # existed anywhere in this module for either check.
        if layer.schema_version != SCHEMA_VERSION:
            raise ProfileContractError(
                f"unsupported profile schema_version {layer.schema_version!r} for layer "
                f"({layer.scope_type!r}, {layer.scope_id!r}) -- this module understands "
                f"schema_version {SCHEMA_VERSION} only"
            )
        if layer.lifecycle_state is not None and layer.scope_type != "hosted_default":
            raise ProfileContractError(
                f"lifecycle_state is only valid for scope_type='hosted_default', got "
                f"scope_type={layer.scope_type!r} for layer "
                f"({layer.scope_type!r}, {layer.scope_id!r})"
            )
        layers_applied.append(layer.scope_type)
        contributing.append((layer.scope_type, layer.scope_id, layer.revision))

        for reset_field in layer.reset_fields:
            if reset_field not in registry:
                raise ProfileContractError(f"reset_fields: unknown profile field: {reset_field!r}")
            if reset_field in effective:
                reset_log.append({
                    "field": reset_field,
                    "reset_by_layer": layer.scope_type,
                    "previously_set_by_layer": sources.get(reset_field),
                })
                del effective[reset_field]
                sources.pop(reset_field, None)

        for field_name, value in layer.fields.items():
            spec = registry.get(field_name)
            if spec is None:
                raise ProfileContractError(f"unknown profile field: {field_name!r}")
            if layer.scope_type not in spec.allowed_layers:
                raise ProfileContractError(
                    f"field {field_name!r} not allowed at layer {layer.scope_type!r} "
                    f"(allowed_layers={spec.allowed_layers})"
                )

            had_previous = field_name in effective
            previous_value = effective.get(field_name, spec.default)

            # narrow_only is only meaningful relative to a value a PRIOR
            # (less-specific) layer actually set — the first layer to
            # declare a field is establishing the baseline, not narrowing or
            # widening away from anything, even though effective.get(...,
            # spec.default) would otherwise supply a comparable "floor"
            # value. Gating on had_previous keeps hosted_default (or
            # whichever layer declares a field first) free to set any value.
            if had_previous and spec.narrow_only and spec.safe_direction and not _direction_is_safe(
                previous_value, value, spec.safe_direction, field_name
            ):
                entry = {
                    "field": field_name,
                    "layer": layer.scope_type,
                    "previous_value": previous_value,
                    "attempted_value": value,
                    "safe_direction": spec.safe_direction,
                }
                if not override_reason:
                    blocked_widens.append(entry)
                    continue  # rejected: value not applied, prior value (or default) stands
                blocked_widens.append({**entry, "overridden": True, "override_reason": override_reason})

            conflict = False
            if spec.merge_strategy == "dict_merge_by_key" and isinstance(value, dict):
                merged_value: Any = {**(previous_value or {}), **value}
            elif spec.merge_strategy == "list_replace_via_capability_profile" and isinstance(value, list):
                # DELEGATES actual capability-list merging to
                # capability_profile.merge_layers, treating the accumulated
                # value and this layer's value as two capability-list layers
                # — so a genuine required_tools/availability_policy conflict
                # is detected the exact same way capability_profile.py
                # already detects one. Folded in from profile_resolution.py
                # during the PROFILE-RECON reconciliation (732c113e): this
                # module previously only tracked which ref was active per
                # layer wholesale (last-write-wins), losing earlier layers'
                # capabilities instead of merging them.
                merged_list, _cap_sources, cap_overrides, _cap_disabled = _capability_profile.merge_layers([
                    {"layer": sources.get(field_name, "default"), "capabilities": previous_value or [],
                     "disabled_capability_ids": []},
                    {"layer": layer.scope_type, "capabilities": value, "disabled_capability_ids": []},
                ])
                merged_value = merged_list
                conflict = any(o.get("conflict") for o in cap_overrides)
            else:
                # scalar_override.
                merged_value = value

            if had_previous:
                overrides.append({
                    "field": field_name,
                    "from_layer": sources[field_name],
                    "to_layer": layer.scope_type,
                    "previous": previous_value,
                    "new": merged_value,
                    "conflict": conflict,
                })

            effective[field_name] = merged_value
            sources[field_name] = layer.scope_type

    generation_key = _compute_generation_key(contributing)

    refresh_required = False
    changed_field_names: set[str] = set()
    if previous_fields is not None:
        touched_field_names = set(effective) | set(previous_fields)
        for field_name in touched_field_names:
            if effective.get(field_name) != previous_fields.get(field_name):
                changed_field_names.add(field_name)
                spec = registry.get(field_name)
                if spec is not None and spec.restart_class == "explicit_refresh_required":
                    refresh_required = True
        # Piggyback on the touched-field-name diff just computed above for
        # refresh_required/restart_report -- same field names, same
        # effective/previous_fields .get() calls, just packaged as
        # {"old", "new"} pairs. Folded in from profile_resolution.py's
        # EffectiveProfile.changed_fields (ac95d206) during the
        # PROFILE-RECON reconciliation (732c113e).
        changed_fields = {
            field_name: {"old": previous_fields.get(field_name), "new": effective.get(field_name)}
            for field_name in changed_field_names
        }
    else:
        # No baseline to diff against -- fall back to each touched field's
        # FIELD_REGISTRY default, mirroring profile_resolution.py's
        # "previous_effective_fields is None" branch. Every field this
        # resolution actually set (i.e. every key in `effective`) counts as
        # changed, even if a layer happened to re-declare the default value
        # verbatim -- it was still an explicit declaration, not silence.
        changed_fields = {
            field_name: {
                "old": registry[field_name].default if field_name in registry else None,
                "new": value,
            }
            for field_name, value in effective.items()
        }

    restart_report, restart_required = _compute_restart_report(changed_field_names, registry)

    # executable/degraded: only the blocked_widens-driven half of the
    # original signal is computable here -- see the docstring above for why
    # the hosted_default-lifecycle half is NOT computed in this pure module
    # and instead lives in db.profile_layers.get_effective_profile.
    executable = True
    executable_reasons: list[str] = []
    degraded = False
    degraded_reasons: list[str] = []
    if blocked_widens:
        degraded = True
        degraded_reasons.append("narrow_only_widen_blocked")

    return EffectiveProfile(
        fields=effective,
        field_sources=sources,
        overrides=overrides,
        blocked_widens=blocked_widens,
        reset_log=reset_log,
        layers_applied=layers_applied,
        generation_key=generation_key,
        refresh_required=refresh_required,
        changed_fields=changed_fields,
        restart_report=restart_report,
        restart_required=restart_required,
        executable=executable,
        executable_reasons=executable_reasons,
        degraded=degraded,
        degraded_reasons=degraded_reasons,
    )


def project_profile_binding(effective: dict[str, Any]) -> dict[str, Any]:
    """Compact profile identity/generation projection (PROFILE-6, 89a06e40;
    see pinned decision ee7bccc9 for the tunnel/connector scoping rationale).

    Takes an ``EffectiveProfile``-shaped dict — either
    ``resolve_effective_profile(...).model_dump()`` directly, or the richer
    dict :func:`meridian.db.profile_layers.get_effective_profile` /
    :func:`meridian.db.profile_layers.get_workspace_effective_profile` return
    (which fold the hosted_default-lifecycle executable/degraded half in on
    top of the same base shape) — and returns the SMALL, STABLE subset this
    item attaches at all 4 integration points (``start_session``,
    ``generate_handoff`` and its ``build_effective_profile_binding``
    wrapper, the goal-mode ``<profile_generation>`` inline tag, and the
    tunnel/connector routes). Deliberately NOT the full ``fields`` merged
    dict — every caller gets the same small shape:
    ``generation_key``/``executable``/``degraded``/``restart_required``/
    ``restart_report``. ``.get(...)`` with safe defaults throughout so a
    partially-shaped input (e.g. a hand-built test dict) degrades gracefully
    instead of raising.
    """
    return {
        "generation_key": effective.get("generation_key") or "",
        "executable": bool(effective.get("executable", True)),
        "degraded": bool(effective.get("degraded", False)),
        "restart_required": bool(effective.get("restart_required", False)),
        "restart_report": dict(effective.get("restart_report") or {}),
    }


# ---------------------------------------------------------------------------
# Contract fixtures — example ProfileLayer instances demonstrating the
# hosted_default floor, an overlay layer, and the narrow_only blocked-widen
# case (feedback_hitl_suppression_injection's HITL-auto-answer scenario).
# ---------------------------------------------------------------------------

HOSTED_DEFAULT_FIXTURE = ProfileLayer(
    scope_type="hosted_default",
    scope_id="global",
    revision=1,
    fields={
        "hitl_auto_answer": 0,
        "require_merge_approval": 1,
        "claim_verification_mode": "advisory",
        "tool_priority_map": {"code_search": "Serena: find_symbol"},
    },
    lifecycle_state="active",
    provenance={"source": "hosted-defaults-v1"},
)

WORKSPACE_OVERLAY_FIXTURE = ProfileLayer(
    scope_type="workspace",
    scope_id="singleton",
    revision=1,
    fields={
        "auto_worktrees": 1,
        "tool_priority_map": {"docs": "meridian-docs"},
    },
)

#: Demonstrates the narrow_only rejection: this session tries to widen
#: hitl_auto_answer from the hosted_default floor (0) to 2 (aggressive) with
#: no override_reason. resolve_effective_profile([HOSTED_DEFAULT_FIXTURE,
#: WORKSPACE_OVERLAY_FIXTURE, SESSION_OVERRIDE_BLOCKED_FIXTURE]) leaves
#: fields["hitl_auto_answer"] == 0 (the widen is rejected into
#: blocked_widens, not applied).
SESSION_OVERRIDE_BLOCKED_FIXTURE = ProfileLayer(
    scope_type="session",
    scope_id="demo-session",
    revision=1,
    fields={
        "hitl_auto_answer": 2,
    },
)
