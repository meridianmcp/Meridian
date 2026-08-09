"""Pure validation + resolution for layered profile overlays (ac95d206, PROFILE-3).

Builds directly on two already-shipped precedents this module deliberately
reuses rather than reinvents:

* :mod:`meridian.capability_profile` (02038afe) -- the 3-layer
  ``workspace -> user -> project`` merge-with-precedence pattern
  (:func:`capability_profile.merge_layers`) this module GENERALIZES to 5
  layers by bracketing it with a ``hosted_default`` floor and a
  ``session`` (session/run) ceiling: ``hosted_default -> workspace -> user
  -> project -> session``.
* :mod:`meridian.capability_manifest` (649e095f) -- reused verbatim for its
  secret-shaped-string and absolute-local-path regexes
  (``_SECRET_LIKE_RE`` / ``_ABSOLUTE_PATH_RE``) and its capability-list
  validator (:func:`capability_manifest.normalize_manifest`).

Scope: this module is PURE validation/resolution logic. It operates on
:class:`ProfileLayer` objects handed to it by a caller -- there is no DB
access, no network, no import of a storage module. This is intentional:
sprint items d8481276 (Neon persistence) and 8d52b620 (Redis projection
cache) are running in parallel, separate worktrees and are NOT assumed to
exist. Nothing in this module imports either of their eventual modules.

Full field registry + design source
------------------------------------
The complete field registry (10 field groups, allowed layers, merge
strategies, defaults, narrow_only/safe_direction dials, and the Neon/Redis
architecture) was defined by sprint item 62c41508 (PROFILE-1). That item's
notes state the exact Pydantic model code ("ScopeType, MergeStrategy,
RestartClass, LifecycleState, SafeDirection literals; ProfileFieldSpec,
ProfileLayer, EffectiveProfile models... in the full session log_task
record") -- **that log_task record was checked (get_session_log on session
a841fa42) and it does not actually contain the fixture code**, only the
same condensed prose the sprint-item notes carry. Rather than block on an
unrecoverable pointer, this module reconstructs the three Pydantic models
and the field registry from that prose faithfully, documenting every place
a genuine design judgment call was required (mostly: per-field
``restart_class``/``component`` assignment, and the unsafe-command
heuristic, neither of which the prose fully enumerated). See inline
comments below at each such call.

Deliberately named ``profile_resolution.py``, NOT ``profile_contract.py``:
62c41508's notes reserve ``meridian/profile_contract.py`` for whichever of
items 2/4 (d8481276 / 8d52b620) implements the fixtures "verbatim" as their
own storage-layer contract module. Using a different filename here avoids
a future merge collision with that file while still shipping the pure
logic this item (ac95d206) actually owns.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, Field
from pydantic import ValidationError as _PydanticValidationError

from . import capability_manifest as _cm
from . import capability_profile as _cp

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ProfileValidationError(_cm.CapabilityManifestError):
    """Raised on profile-layer schema/safety/precedence violations.

    Subclasses CapabilityManifestError (matching capability_profile.py's own
    CapabilityProfileError precedent) so any caller that already catches
    that base type keeps working if extended to cover profile layers too.
    """


class ProfileStaleRevisionError(ProfileValidationError):
    """Raised by :func:`check_expected_revision` on an optimistic-concurrency
    mismatch -- mirrors ``wave_runs.finalize_wave_run``'s
    ``expected_revision_hash`` gate."""


# ---------------------------------------------------------------------------
# Literals
# ---------------------------------------------------------------------------

ScopeType = Literal["hosted_default", "workspace", "user", "project", "session"]
MergeStrategy = Literal[
    "scalar_override", "dict_merge_by_key", "list_replace_via_capability_profile"
]
RestartClass = Literal["hot_reload", "explicit_refresh_required", "restart_required"]
LifecycleState = Literal["draft", "active", "deprecated", "retired"]
SafeDirection = Literal["increase", "decrease"]
RestartComponent = Literal["tunnel", "connector", "capability", "general"]

# Least specific -> most specific. Generalizes capability_profile.SCOPE_TYPES
# (workspace, user, project) by bracketing it with a hosted_default floor and
# a session/run ceiling.
SCOPE_ORDER: tuple[ScopeType, ...] = ("hosted_default", "workspace", "user", "project", "session")
ALL_LAYERS: tuple[ScopeType, ...] = SCOPE_ORDER

PROFILE_SCHEMA_VERSION = 1  # envelope shape, breaking-only (mirrors ai_log.EVENT_SCHEMA_VERSION)

_LIFECYCLE_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "draft": ("active", "retired"),
    "active": ("deprecated",),
    "deprecated": ("retired", "active"),
    "retired": (),
}


# ---------------------------------------------------------------------------
# Field registry
# ---------------------------------------------------------------------------


class ProfileFieldSpec(BaseModel):
    """Static description of one resolvable profile field."""

    model_config = {"frozen": True}

    name: str
    type: Literal["int", "str", "dict", "list"]
    allowed_layers: tuple[ScopeType, ...]
    merge_strategy: MergeStrategy
    default: Any
    restart_class: RestartClass
    # RestartComponent classification (tunnel/connector/capability/general) --
    # not in 62c41508's prose verbatim; this module's own acceptance criteria
    # ("restart/refresh-required classification per tunnel/connector/
    # capability") requires grouping fields by component, so this is added
    # here as the natural place to carry that classification.
    component: RestartComponent = "general"
    narrow_only: bool = False
    safe_direction: SafeDirection | None = None
    # Only meaningful for the hosted_default/workspace absolute-path ban
    # below; informational elsewhere.
    path_allowed_from_layer: ScopeType | None = None


def _spec(**kwargs: Any) -> ProfileFieldSpec:
    return ProfileFieldSpec(**kwargs)


# Grounded in the REAL ProjectSettings/ExecutorConfig fields (meridian/models.py)
# plus the 3 genuinely-new fields (tool_priority_map, capability_manifest_ref,
# claim_verification_mode) 62c41508 called out. The 7 existing ProjectSettings
# fields are NOT duplicated as a new authority -- they keep flowing through
# get_project_settings/update_project_settings; this registry only describes
# how THOSE SAME field values behave when they also appear in a profile layer.
#
# restart_class / component judgment calls (not in the source prose, made
# here): fields baked into start_session's one-shot agent_instructions text
# (hitl_auto_answer, require_merge_approval, execution_mode, test_min,
# test_cmd, deploy_cmd, tool_priority_map, capability_manifest_ref) need at
# least a fresh session/handoff to take effect -> explicit_refresh_required.
# Fields read live from DB on every call (auto_worktrees, code_intel_enabled,
# max_pinned_decisions, claim_verification_mode) -> hot_reload. Fields that
# configure the actual OS-level executor process (cwd, shell, checked-out
# branch, env file) can only take effect on a fresh process spawn ->
# restart_required -- the first real user of the restart_class value 62c41508
# flagged as "reserved."
FIELD_REGISTRY: dict[str, ProfileFieldSpec] = {
    "max_pinned_decisions": _spec(
        name="max_pinned_decisions", type="int", allowed_layers=ALL_LAYERS,
        merge_strategy="scalar_override", default=20, restart_class="hot_reload",
        component="general",
    ),
    "hitl_auto_answer": _spec(
        name="hitl_auto_answer", type="int", allowed_layers=ALL_LAYERS,
        merge_strategy="scalar_override", default=0, restart_class="explicit_refresh_required",
        component="general", narrow_only=True, safe_direction="decrease",
    ),
    "auto_worktrees": _spec(
        name="auto_worktrees", type="int", allowed_layers=ALL_LAYERS,
        merge_strategy="scalar_override", default=1, restart_class="hot_reload",
        component="general",
    ),
    "require_merge_approval": _spec(
        name="require_merge_approval", type="int", allowed_layers=ALL_LAYERS,
        merge_strategy="scalar_override", default=1, restart_class="explicit_refresh_required",
        component="general", narrow_only=True, safe_direction="increase",
    ),
    "code_intel_enabled": _spec(
        name="code_intel_enabled", type="int", allowed_layers=ALL_LAYERS,
        merge_strategy="scalar_override", default=0, restart_class="hot_reload",
        component="general",
    ),
    "execution_mode": _spec(
        name="execution_mode", type="str", allowed_layers=ALL_LAYERS,
        merge_strategy="scalar_override", default="autonomous",
        restart_class="explicit_refresh_required", component="general",
    ),
    # Migrated from workspace_settings (62c41508) -- deliberately NOT allowed
    # at hosted_default: it is a workspace-and-below concern, not something a
    # hosted floor should be asserting for every tenant.
    "claim_verification_mode": _spec(
        name="claim_verification_mode", type="str",
        allowed_layers=("workspace", "user", "project", "session"),
        merge_strategy="scalar_override", default="advisory", restart_class="hot_reload",
        component="connector", narrow_only=True, safe_direction="increase",
    ),
    "executor_config.repo_path": _spec(
        name="executor_config.repo_path", type="str", allowed_layers=("project", "session"),
        merge_strategy="scalar_override", default=None, restart_class="restart_required",
        component="connector", path_allowed_from_layer="project",
    ),
    "executor_config.repo_paths": _spec(
        name="executor_config.repo_paths", type="list", allowed_layers=("project", "session"),
        merge_strategy="scalar_override", default=None, restart_class="restart_required",
        component="connector",
    ),
    "executor_config.env_file": _spec(
        name="executor_config.env_file", type="str", allowed_layers=("project", "session"),
        merge_strategy="scalar_override", default=None, restart_class="restart_required",
        component="connector", path_allowed_from_layer="project",
    ),
    "executor_config.test_cmd": _spec(
        name="executor_config.test_cmd", type="str", allowed_layers=ALL_LAYERS,
        merge_strategy="scalar_override", default=None,
        restart_class="explicit_refresh_required", component="connector",
    ),
    "executor_config.test_min": _spec(
        name="executor_config.test_min", type="int", allowed_layers=ALL_LAYERS,
        merge_strategy="scalar_override", default=None,
        restart_class="explicit_refresh_required", component="connector",
        narrow_only=True, safe_direction="increase",
    ),
    "executor_config.deploy_cmd": _spec(
        name="executor_config.deploy_cmd", type="str", allowed_layers=ALL_LAYERS,
        merge_strategy="scalar_override", default=None,
        restart_class="explicit_refresh_required", component="connector",
    ),
    "executor_config.shell_type": _spec(
        name="executor_config.shell_type", type="str", allowed_layers=("project", "session"),
        merge_strategy="scalar_override", default=None, restart_class="restart_required",
        component="connector",
    ),
    "executor_config.branch": _spec(
        name="executor_config.branch", type="str", allowed_layers=("project", "session"),
        merge_strategy="scalar_override", default=None, restart_class="restart_required",
        component="connector",
    ),
    "tool_priority_map": _spec(
        name="tool_priority_map", type="dict", allowed_layers=ALL_LAYERS,
        merge_strategy="dict_merge_by_key", default={},
        restart_class="explicit_refresh_required", component="capability",
    ),
    "capability_manifest_ref": _spec(
        name="capability_manifest_ref", type="list", allowed_layers=ALL_LAYERS,
        merge_strategy="list_replace_via_capability_profile", default=[],
        restart_class="restart_required", component="capability",
    ),
}

# Fields whose string value is a shell/deploy command -- subject to the
# unsafe-command heuristic below. Not part of 62c41508's prose (it named the
# check but not its shape); a conservative, documented deny-list, defense in
# depth only -- same spirit as capability_manifest's own secret/path regexes.
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

_ORDINALS: dict[str, dict[str, int]] = {
    "claim_verification_mode": {"off": 0, "advisory": 1, "strict": 2},
}


# ---------------------------------------------------------------------------
# Wire models
# ---------------------------------------------------------------------------


class ProfileLayer(BaseModel):
    """One scope's stored (or in-flight) profile overlay.

    Mirrors capability_profiles' row shape (scope_type/scope_id + declared
    fields + a disable/reset mechanism) plus PROFILE-1's versioning and
    hosted_default-only lifecycle fields. Pure data -- no persistence here;
    a caller (item 2/4's storage layer) constructs/persists these.
    """

    scope_type: ScopeType
    scope_id: str = Field(..., min_length=1)
    revision: int = Field(default=0, ge=0)
    schema_version: int = PROFILE_SCHEMA_VERSION
    fields: dict[str, Any] = Field(default_factory=dict)
    # Mirrors capability_profile's disabled_capability_ids: retract an
    # inherited field back to its registry default without redeclaring it.
    reset_fields: list[str] = Field(default_factory=list)
    # Only meaningful when scope_type == "hosted_default".
    lifecycle_state: LifecycleState | None = None
    # Explicit, audited acknowledgement that a narrow_only field is
    # deliberately being widened at THIS layer (e.g. an admin explicitly
    # relaxing hitl_auto_answer for one project). Keyed by field name.
    override_reasons: dict[str, str] = Field(default_factory=dict)


class EffectiveProfile(BaseModel):
    """Result of resolving an ordered set of :class:`ProfileLayer` rows."""

    fields: dict[str, Any]
    field_sources: dict[str, str]
    overrides: list[dict[str, Any]]
    blocked_widens: list[dict[str, Any]]
    acknowledged_widens: list[dict[str, Any]]
    reset_log: list[dict[str, Any]]
    layers_applied: list[dict[str, Any]]
    generation_key: str
    schema_version: int
    executable: bool
    executable_reasons: list[str]
    degraded: bool
    degraded_reasons: list[str]
    changed_fields: dict[str, dict[str, Any]]
    restart_report: dict[str, str]
    restart_required: bool
    refresh_required: bool


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _check_no_secrets(value: Any, *, path: str) -> None:
    """Universal secret-shaped-string ban, reusing capability_manifest's
    regex verbatim -- applies to every field at every layer."""
    if isinstance(value, str):
        if _cm._SECRET_LIKE_RE.search(value):
            raise ProfileValidationError(
                f"{path}: secret-shaped value not allowed in a profile layer"
            )
    elif isinstance(value, dict):
        for k, v in value.items():
            _check_no_secrets(v, path=f"{path}.{k}")
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            _check_no_secrets(v, path=f"{path}[{i}]")


def _check_no_absolute_path(value: Any, scope_type: str, *, path: str) -> None:
    """Absolute machine-local path ban, reusing capability_manifest's regex
    verbatim -- prohibited at hosted_default/workspace ONLY (62c41508's own
    wording); user/project/session layers are where a real local checkout
    path legitimately lives."""
    if scope_type not in ("hosted_default", "workspace"):
        return
    if isinstance(value, str):
        if _cm._ABSOLUTE_PATH_RE.search(value):
            raise ProfileValidationError(
                f"{path}: machine-local absolute path not allowed at scope_type={scope_type!r}"
            )
    elif isinstance(value, dict):
        for k, v in value.items():
            _check_no_absolute_path(v, scope_type, path=f"{path}.{k}")
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            _check_no_absolute_path(v, scope_type, path=f"{path}[{i}]")


def _check_unsafe_command(value: Any, *, path: str) -> None:
    if isinstance(value, str) and _UNSAFE_COMMAND_RE.search(value):
        raise ProfileValidationError(f"{path}: unsafe/destructive command pattern not allowed")


_PY_TYPES: dict[str, type] = {"int": int, "str": str, "dict": dict, "list": list}


def _check_field_type(spec: ProfileFieldSpec, value: Any, *, path: str) -> None:
    py_type = _PY_TYPES[spec.type]
    if spec.type == "int" and isinstance(value, bool):
        raise ProfileValidationError(f"{path}: expected int, got bool")
    if not isinstance(value, py_type):
        raise ProfileValidationError(
            f"{path}: expected {spec.type}, got {type(value).__name__}"
        )


def validate_profile_layer(raw: "dict[str, Any] | ProfileLayer") -> ProfileLayer:
    """Validate+normalize a single profile layer. Raises deterministically
    (never silently drops a bad field) on any schema or safety violation --
    matches capability_manifest.normalize_capability's own contract.

    Checks, in order: envelope schema_version, lifecycle_state only valid
    for hosted_default, every reset_fields entry is a known field, then per
    declared field: known field (else "unknown profile field"), allowed at
    this scope_type (else "cross-scope write"), correct Python type, no
    secret-shaped value, no absolute path at hosted_default/workspace, no
    unsafe command (test_cmd/deploy_cmd only), and -- for
    capability_manifest_ref -- a structurally valid capability contract
    (delegates to capability_manifest.normalize_manifest).
    """
    if isinstance(raw, ProfileLayer):
        layer = raw
    else:
        try:
            layer = ProfileLayer(**raw)
        except _PydanticValidationError as exc:
            raise ProfileValidationError(f"profile layer failed schema validation: {exc}") from exc

    if layer.schema_version != PROFILE_SCHEMA_VERSION:
        raise ProfileValidationError(
            f"unsupported profile schema_version {layer.schema_version} "
            f"(this resolver understands schema_version {PROFILE_SCHEMA_VERSION} only)"
        )

    if layer.lifecycle_state is not None and layer.scope_type != "hosted_default":
        raise ProfileValidationError(
            f"lifecycle_state is only valid for scope_type='hosted_default', "
            f"got scope_type={layer.scope_type!r}"
        )

    for fname in layer.reset_fields:
        if fname not in FIELD_REGISTRY:
            raise ProfileValidationError(f"reset_fields: unknown field {fname!r}")

    for fname, value in layer.fields.items():
        spec = FIELD_REGISTRY.get(fname)
        if spec is None:
            raise ProfileValidationError(f"unknown profile field: {fname!r}")
        if layer.scope_type not in spec.allowed_layers:
            raise ProfileValidationError(
                f"cross-scope write: field {fname!r} is not allowed at "
                f"scope_type={layer.scope_type!r} (allowed: {list(spec.allowed_layers)})"
            )
        if value is None:
            raise ProfileValidationError(
                f"field {fname!r}: null is not a valid override value -- use "
                f"reset_fields to clear a field back to its default"
            )
        path = f"{layer.scope_type}:{layer.scope_id}.{fname}"
        _check_field_type(spec, value, path=path)
        _check_no_secrets(value, path=path)
        _check_no_absolute_path(value, layer.scope_type, path=path)
        if fname in _COMMAND_SHAPED_FIELDS:
            _check_unsafe_command(value, path=path)
        if fname == "capability_manifest_ref" and value:
            try:
                _cm.normalize_manifest(value)
            except _cm.CapabilityManifestError as exc:
                raise ProfileValidationError(
                    f"{path}: invalid capability contract: {exc}"
                ) from exc

    return layer


def validate_lifecycle_transition(current: "LifecycleState | None", new: "LifecycleState") -> None:
    """hosted_default-only lifecycle state machine: draft -> {active,
    retired}; active -> {deprecated}; deprecated -> {retired, active};
    retired -> {} (terminal). Re-asserting the SAME state is always a valid
    idempotent no-op (mirrors the revision ledger's own idempotent-resave
    contract)."""
    if current is None:
        if new != "draft":
            raise ProfileValidationError(
                f"a new hosted_default profile must start in lifecycle_state='draft', got {new!r}"
            )
        return
    if new == current:
        return
    allowed = _LIFECYCLE_TRANSITIONS.get(current, ())
    if new not in allowed:
        raise ProfileValidationError(
            f"invalid hosted_default lifecycle transition: {current!r} -> {new!r} "
            f"(allowed: {list(allowed)})"
        )


def check_expected_revision(current_revision: "int | None", expected_revision: "int | None") -> None:
    """Optimistic-concurrency guard mirroring wave_runs.finalize_wave_run's
    expected_revision_hash gate and capability_profile.set_capability_profile's
    existing last-write-wins default.

    ``expected_revision=None`` -> last-write-wins, always a no-op (matches
    today's capability_profile behavior -- nothing existing breaks).
    ``expected_revision`` given and it does not match ``current_revision`` ->
    stale write, raises :class:`ProfileStaleRevisionError`. A future
    persistence layer (item 2/d8481276's ``set_profile_layer``) is expected
    to call this immediately before applying a new layer write.
    """
    if expected_revision is None:
        return
    if current_revision != expected_revision:
        raise ProfileStaleRevisionError(
            f"expected_revision={expected_revision!r} does not match current "
            f"revision {current_revision!r} -- refetch and retry"
        )


# ---------------------------------------------------------------------------
# Generation key
# ---------------------------------------------------------------------------


def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


def compute_generation_key(layers: "list[ProfileLayer]") -> str:
    """generation_key = sha256(canonical_json of every contributing layer's
    (scope_type, scope_id, revision)) -- content-addressed and DERIVED, not
    stored: a write is automatically a cache-miss-inducing new key with no
    explicit invalidation path needed (62c41508's own design). Deliberately
    keyed on (scope_type, scope_id, revision) ONLY, not field content: this
    trusts revision to already be content-hash-ledgered (mirrors
    board_snapshot_revisions -- revision only increments when content
    actually changed), a responsibility this pure module hands to whatever
    persistence layer (item 2/4) owns writing ProfileLayer rows.
    """
    contributing = [
        {"scope_type": layer.scope_type, "scope_id": layer.scope_id, "revision": layer.revision}
        for layer in layers
    ]
    contributing.sort(key=lambda c: (SCOPE_ORDER.index(c["scope_type"]), str(c["scope_id"])))
    digest = hashlib.sha256(_canonical_json(contributing).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


# ---------------------------------------------------------------------------
# Restart/refresh classification
# ---------------------------------------------------------------------------

_RESTART_SEVERITY: dict[str, int] = {"hot_reload": 1, "explicit_refresh_required": 2, "restart_required": 3}
_SEVERITY_LABEL: dict[int, str] = {0: "none", 1: "hot_reload", 2: "explicit_refresh_required", 3: "restart_required"}


def _compute_restart_report(changed_field_names: "set[str]") -> "tuple[dict[str, str], bool, bool]":
    """Per-component (tunnel/connector/capability/general) classification of
    the most severe restart_class among CHANGED fields.

    ``tunnel`` is always "none" in today's registry -- no field is currently
    classified tunnel-component; 62c41508 explicitly reserved restart_class's
    top severity for when routes/tunnel.py's ``has_active_tunnel()``-gated
    fields land. The component still appears in the report (not omitted) so
    a caller's shape never has to special-case its absence.
    """
    severities: dict[str, int] = {c: 0 for c in ("tunnel", "connector", "capability", "general")}
    for fname in changed_field_names:
        spec = FIELD_REGISTRY.get(fname)
        if spec is None:
            continue
        sev = _RESTART_SEVERITY[spec.restart_class]
        if sev > severities[spec.component]:
            severities[spec.component] = sev
    report = {component: _SEVERITY_LABEL[sev] for component, sev in severities.items()}
    restart_required = any(v == "restart_required" for v in report.values())
    refresh_required = any(v in ("explicit_refresh_required", "restart_required") for v in report.values())
    return report, restart_required, refresh_required


# ---------------------------------------------------------------------------
# Widen detection (narrow_only fields)
# ---------------------------------------------------------------------------


def _ordinal(spec: ProfileFieldSpec, value: Any) -> Any:
    if isinstance(value, str) and spec.name in _ORDINALS:
        return _ORDINALS[spec.name].get(value, value)
    return value


def _is_widen(spec: ProfileFieldSpec, previous_value: Any, new_value: Any) -> bool:
    """True when ``new_value`` relaxes (widens) a narrow_only safety dial
    relative to ``previous_value``, per ``spec.safe_direction``:
    ``"decrease"`` means lower is safer (a rise is a widen);
    ``"increase"`` means higher is safer (a drop is a widen)."""
    if spec.safe_direction is None:
        return False
    prev = _ordinal(spec, previous_value)
    new = _ordinal(spec, new_value)
    try:
        if spec.safe_direction == "decrease":
            return new > prev
        return new < prev
    except TypeError:
        return False  # incomparable values -- fail open on the comparison itself, not on safety


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def resolve_effective_profile(
    layers: "list[dict[str, Any]] | list[ProfileLayer]",
    *,
    previous_effective_fields: "dict[str, Any] | None" = None,
    validate: bool = True,
) -> EffectiveProfile:
    """Merge an unordered set of :class:`ProfileLayer` rows (or raw dicts)
    into one :class:`EffectiveProfile`, applying registry defaults for
    every field with NO contributing layer -- "preserve current defaults
    when no profile exists" holds trivially: ``resolve_effective_profile([])``
    returns exactly the registry defaults, executable, not degraded.

    Per layer, in ascending specificity (``SCOPE_ORDER`` -- callers may pass
    layers in any order, they are re-sorted here):

    1. Apply ``reset_fields`` -- retract an inherited (non-default) value
       back to the registry default. A no-op reset (nothing inherited yet)
       is not logged, mirroring capability_profile.merge_layers.
    2. Apply each declared field per its ``merge_strategy``:
       ``scalar_override`` (this layer's value wins outright),
       ``dict_merge_by_key`` (shallow dict merge, this layer's keys win),
       ``list_replace_via_capability_profile`` (delegates to
       capability_profile.merge_layers, treating the accumulated value and
       this layer's value as two capability-list layers -- so a genuine
       required_tools/availability_policy conflict is detected the exact
       same way capability_profile.py already detects one).
    3. A ``narrow_only`` field being widened at this layer is rejected into
       ``blocked_widens`` (the previous, safer value is kept) UNLESS this
       layer supplies ``override_reasons[field]``, in which case the widen
       is allowed through but still recorded, in ``acknowledged_widens``,
       for audit -- directly closes the HITL-suppression-injection risk
       this project has already hit once (see project memory
       feedback_hitl_suppression_injection): a widen can happen, but never
       silently.

    ``previous_effective_fields`` (optional) is a prior resolution's
    ``fields`` dict, used to compute ``changed_fields`` against a known
    baseline. Omitted, ``changed_fields`` is computed against the bare
    registry defaults instead (every field whose source isn't ``"default"``
    counts as changed).

    ``validate=False`` skips :func:`validate_profile_layer` re-validation
    for callers that already validated each layer (e.g. immediately after
    a passing ``set_profile_layer`` write) -- resolution logic itself is
    unaffected either way.
    """
    normalized: list[ProfileLayer] = []
    for raw in layers:
        normalized.append(validate_profile_layer(raw) if validate else (
            raw if isinstance(raw, ProfileLayer) else ProfileLayer(**raw)
        ))

    ordered = sorted(normalized, key=lambda layer: SCOPE_ORDER.index(layer.scope_type))

    effective: dict[str, Any] = {name: spec.default for name, spec in FIELD_REGISTRY.items()}
    sources: dict[str, str] = {name: "default" for name in FIELD_REGISTRY}
    overrides: list[dict[str, Any]] = []
    blocked_widens: list[dict[str, Any]] = []
    acknowledged_widens: list[dict[str, Any]] = []
    reset_log: list[dict[str, Any]] = []
    layers_applied: list[dict[str, Any]] = []
    hosted_default_lifecycle: "LifecycleState | None" = None

    for layer in ordered:
        layer_name = layer.scope_type
        layers_applied.append({
            "scope_type": layer_name, "scope_id": layer.scope_id, "revision": layer.revision,
        })
        if layer_name == "hosted_default":
            hosted_default_lifecycle = layer.lifecycle_state

        for fname in layer.reset_fields:
            if sources.get(fname) != "default":
                reset_log.append({
                    "field": fname, "reset_by_layer": layer_name,
                    "previously_from_layer": sources.get(fname),
                })
                effective[fname] = FIELD_REGISTRY[fname].default
                sources[fname] = "default"

        for fname, value in layer.fields.items():
            spec = FIELD_REGISTRY[fname]
            previous_value = effective.get(fname)
            previous_source = sources.get(fname)
            had_prior_value = previous_source != "default"

            if spec.narrow_only and had_prior_value and _is_widen(spec, previous_value, value):
                widen_record = {
                    "field": fname, "layer": layer_name,
                    "attempted_value": value, "current_value": previous_value,
                }
                reason = layer.override_reasons.get(fname)
                if not reason:
                    blocked_widens.append({**widen_record, "reason": "narrow_only_violation"})
                    continue  # keep the safer previous_value; do not apply
                acknowledged_widens.append({**widen_record, "override_reason": reason})

            conflict = False
            new_value = value
            if spec.merge_strategy == "dict_merge_by_key":
                base = previous_value if isinstance(previous_value, dict) else {}
                incoming = value if isinstance(value, dict) else {}
                new_value = {**base, **incoming}
            elif spec.merge_strategy == "list_replace_via_capability_profile":
                merged_list, _cap_sources, cap_overrides, _cap_disabled = _cp.merge_layers([
                    {"layer": previous_source or "default", "capabilities": previous_value or [],
                     "disabled_capability_ids": []},
                    {"layer": layer_name, "capabilities": value or [], "disabled_capability_ids": []},
                ])
                new_value = merged_list
                conflict = any(o.get("conflict") for o in cap_overrides)
            # else: scalar_override -- new_value already == value

            if had_prior_value:
                overrides.append({
                    "field": fname, "from_layer": previous_source, "to_layer": layer_name,
                    "previous": previous_value, "new": new_value, "conflict": conflict,
                })

            effective[fname] = new_value
            sources[fname] = layer_name

    generation_key = compute_generation_key(ordered)

    if previous_effective_fields is None:
        changed_fields = {
            fname: {"old": FIELD_REGISTRY[fname].default, "new": value}
            for fname, value in effective.items()
            if sources[fname] != "default"
        }
    else:
        changed_fields = {
            fname: {"old": previous_effective_fields.get(fname), "new": value}
            for fname, value in effective.items()
            if previous_effective_fields.get(fname) != value
        }

    restart_report, restart_required, refresh_required = _compute_restart_report(set(changed_fields))

    executable = True
    executable_reasons: list[str] = []
    degraded = False
    degraded_reasons: list[str] = []
    if hosted_default_lifecycle == "retired":
        executable = False
        executable_reasons.append("hosted_default_retired")
        degraded = True
        degraded_reasons.append("hosted_default_retired")
    elif hosted_default_lifecycle in ("draft", "deprecated"):
        degraded = True
        degraded_reasons.append(f"hosted_default_lifecycle_{hosted_default_lifecycle}")
    if blocked_widens:
        degraded = True
        degraded_reasons.append("narrow_only_widen_blocked")

    return EffectiveProfile(
        fields=effective,
        field_sources=sources,
        overrides=overrides,
        blocked_widens=blocked_widens,
        acknowledged_widens=acknowledged_widens,
        reset_log=reset_log,
        layers_applied=layers_applied,
        generation_key=generation_key,
        schema_version=PROFILE_SCHEMA_VERSION,
        executable=executable,
        executable_reasons=executable_reasons,
        degraded=degraded,
        degraded_reasons=degraded_reasons,
        changed_fields=changed_fields,
        restart_report=restart_report,
        restart_required=restart_required,
        refresh_required=refresh_required,
    )
