"""Tests for meridian.profile_resolution (ac95d206, PROFILE-3).

Pure logic only -- no DB, no fixtures beyond plain dicts/ProfileLayer
objects constructed in-line. Mirrors the acceptance criteria in the sprint
item notes: reject secrets/absolute-paths/unsafe-commands/cross-scope-
writes/invalid-capability-contracts/stale-revisions; return source layers,
generation key, changed fields, executable/degraded status, and restart/
refresh classification per tunnel/connector/capability; preserve defaults
when no profile exists.
"""

from __future__ import annotations

import pytest

from meridian import profile_resolution as pr


# ---------------------------------------------------------------------------
# Defaults preserved when no profile exists
# ---------------------------------------------------------------------------


def test_no_layers_preserves_registry_defaults():
    effective = pr.resolve_effective_profile([])
    for name, spec in pr.FIELD_REGISTRY.items():
        assert effective.fields[name] == spec.default
        assert effective.field_sources[name] == "default"
    assert effective.layers_applied == []
    assert effective.changed_fields == {}
    assert effective.executable is True
    assert effective.degraded is False
    assert effective.restart_required is False
    assert effective.refresh_required is False
    assert all(v == "none" for v in effective.restart_report.values())


def test_generation_key_deterministic_for_no_layers():
    first = pr.resolve_effective_profile([])
    second = pr.resolve_effective_profile([])
    assert first.generation_key == second.generation_key
    assert first.generation_key.startswith("sha256:")


# ---------------------------------------------------------------------------
# Basic scalar precedence across all 5 layers
# ---------------------------------------------------------------------------


def test_scalar_override_precedence_hosted_to_session():
    layers = [
        {"scope_type": "hosted_default", "scope_id": "hd", "revision": 1,
         "fields": {"max_pinned_decisions": 30}, "lifecycle_state": "active"},
        {"scope_type": "workspace", "scope_id": "ws1", "revision": 1,
         "fields": {"max_pinned_decisions": 40}},
        {"scope_type": "user", "scope_id": "u1", "revision": 1,
         "fields": {"max_pinned_decisions": 50}},
        {"scope_type": "project", "scope_id": "p1", "revision": 1,
         "fields": {"max_pinned_decisions": 60}},
        {"scope_type": "session", "scope_id": "s1", "revision": 1,
         "fields": {"max_pinned_decisions": 70}},
    ]
    effective = pr.resolve_effective_profile(layers)
    assert effective.fields["max_pinned_decisions"] == 70
    assert effective.field_sources["max_pinned_decisions"] == "session"
    # 4 overrides recorded: workspace->user->project->session (hosted_default's
    # initial declaration over the seeded "default" source is NOT an override).
    mpd_overrides = [o for o in effective.overrides if o["field"] == "max_pinned_decisions"]
    assert len(mpd_overrides) == 4
    assert mpd_overrides[-1]["to_layer"] == "session"
    assert mpd_overrides[-1]["conflict"] is False


def test_layers_may_be_passed_out_of_order():
    layers = [
        {"scope_type": "project", "scope_id": "p1", "revision": 1, "fields": {"auto_worktrees": 0}},
        {"scope_type": "hosted_default", "scope_id": "hd", "revision": 1, "fields": {"auto_worktrees": 1}},
    ]
    effective = pr.resolve_effective_profile(layers)
    assert effective.fields["auto_worktrees"] == 0
    assert effective.field_sources["auto_worktrees"] == "project"


# ---------------------------------------------------------------------------
# dict_merge_by_key (tool_priority_map)
# ---------------------------------------------------------------------------


def test_dict_merge_by_key_shallow_merges_and_overrides_keys():
    layers = [
        {"scope_type": "workspace", "scope_id": "ws1", "revision": 1,
         "fields": {"tool_priority_map": {"search": "meridian-code", "docs": "meridian-docs"}}},
        {"scope_type": "project", "scope_id": "p1", "revision": 1,
         "fields": {"tool_priority_map": {"search": "codebase-memory"}}},
    ]
    effective = pr.resolve_effective_profile(layers)
    assert effective.fields["tool_priority_map"] == {
        "search": "codebase-memory", "docs": "meridian-docs",
    }


# ---------------------------------------------------------------------------
# list_replace_via_capability_profile (capability_manifest_ref)
# ---------------------------------------------------------------------------


def _cap(id_, required_tools=("Serena",), availability_policy="required"):
    return {"id": id_, "purpose": "x", "required_tools": list(required_tools),
            "availability_policy": availability_policy}


def test_capability_manifest_ref_delegates_to_capability_profile_merge():
    layers = [
        {"scope_type": "workspace", "scope_id": "ws1", "revision": 1,
         "fields": {"capability_manifest_ref": [_cap("serena")]}},
        {"scope_type": "project", "scope_id": "p1", "revision": 1,
         "fields": {"capability_manifest_ref": [_cap("code_intel")]}},
    ]
    effective = pr.resolve_effective_profile(layers)
    ids = sorted(c["id"] for c in effective.fields["capability_manifest_ref"])
    assert ids == ["code_intel", "serena"]


def test_capability_manifest_ref_conflict_detected_via_capability_profile():
    layers = [
        {"scope_type": "workspace", "scope_id": "ws1", "revision": 1,
         "fields": {"capability_manifest_ref": [_cap("serena", availability_policy="required")]}},
        {"scope_type": "project", "scope_id": "p1", "revision": 1,
         "fields": {"capability_manifest_ref": [_cap("serena", availability_policy="optional")]}},
    ]
    effective = pr.resolve_effective_profile(layers)
    ref_overrides = [o for o in effective.overrides if o["field"] == "capability_manifest_ref"]
    assert ref_overrides and ref_overrides[-1]["conflict"] is True


# ---------------------------------------------------------------------------
# narrow_only + safe_direction widen blocking
# ---------------------------------------------------------------------------


def test_narrow_only_decrease_direction_blocks_widen_without_override():
    # hitl_auto_answer: safe_direction=decrease -> a rise is a widen.
    layers = [
        {"scope_type": "project", "scope_id": "p1", "revision": 1, "fields": {"hitl_auto_answer": 0}},
        {"scope_type": "session", "scope_id": "s1", "revision": 1, "fields": {"hitl_auto_answer": 2}},
    ]
    effective = pr.resolve_effective_profile(layers)
    assert effective.fields["hitl_auto_answer"] == 0  # session's widen rejected, safer value kept
    assert effective.field_sources["hitl_auto_answer"] == "project"
    assert len(effective.blocked_widens) == 1
    assert effective.blocked_widens[0]["field"] == "hitl_auto_answer"
    assert effective.degraded is True
    assert "narrow_only_widen_blocked" in effective.degraded_reasons


def test_narrow_only_widen_allowed_with_explicit_override_reason():
    layers = [
        {"scope_type": "project", "scope_id": "p1", "revision": 1, "fields": {"hitl_auto_answer": 0}},
        {"scope_type": "session", "scope_id": "s1", "revision": 1, "fields": {"hitl_auto_answer": 2},
         "override_reasons": {"hitl_auto_answer": "one-off debugging session, human present"}},
    ]
    effective = pr.resolve_effective_profile(layers)
    assert effective.fields["hitl_auto_answer"] == 2
    assert effective.field_sources["hitl_auto_answer"] == "session"
    assert effective.blocked_widens == []
    assert len(effective.acknowledged_widens) == 1
    assert effective.acknowledged_widens[0]["override_reason"] == "one-off debugging session, human present"


def test_narrow_only_increase_direction_blocks_relax_without_override():
    # require_merge_approval: safe_direction=increase -> a drop is a widen.
    layers = [
        {"scope_type": "project", "scope_id": "p1", "revision": 1, "fields": {"require_merge_approval": 2}},
        {"scope_type": "session", "scope_id": "s1", "revision": 1, "fields": {"require_merge_approval": 0}},
    ]
    effective = pr.resolve_effective_profile(layers)
    assert effective.fields["require_merge_approval"] == 2
    assert len(effective.blocked_widens) == 1


def test_narrow_only_narrowing_direction_always_allowed():
    # Tightening (2 -> stays safe) never counts as a widen, no override needed.
    layers = [
        {"scope_type": "project", "scope_id": "p1", "revision": 1, "fields": {"require_merge_approval": 0}},
        {"scope_type": "session", "scope_id": "s1", "revision": 1, "fields": {"require_merge_approval": 2}},
    ]
    effective = pr.resolve_effective_profile(layers)
    assert effective.fields["require_merge_approval"] == 2
    assert effective.blocked_widens == []


# ---------------------------------------------------------------------------
# reset_fields
# ---------------------------------------------------------------------------


def test_reset_fields_retracts_inherited_value_to_default():
    layers = [
        {"scope_type": "workspace", "scope_id": "ws1", "revision": 1,
         "fields": {"code_intel_enabled": 1}},
        {"scope_type": "project", "scope_id": "p1", "revision": 1,
         "reset_fields": ["code_intel_enabled"]},
    ]
    effective = pr.resolve_effective_profile(layers)
    assert effective.fields["code_intel_enabled"] == pr.FIELD_REGISTRY["code_intel_enabled"].default
    assert effective.field_sources["code_intel_enabled"] == "default"
    assert len(effective.reset_log) == 1
    assert effective.reset_log[0]["reset_by_layer"] == "project"
    assert effective.reset_log[0]["previously_from_layer"] == "workspace"


def test_reset_fields_noop_when_nothing_inherited_is_not_logged():
    layers = [
        {"scope_type": "project", "scope_id": "p1", "revision": 1,
         "reset_fields": ["code_intel_enabled"]},
    ]
    effective = pr.resolve_effective_profile(layers)
    assert effective.reset_log == []


# ---------------------------------------------------------------------------
# Rejections: secrets / absolute paths / unsafe commands / cross-scope / bad contracts
# ---------------------------------------------------------------------------


def test_rejects_secret_shaped_value():
    layers = [{"scope_type": "project", "scope_id": "p1", "revision": 1,
               "fields": {"execution_mode": "sk-abcdefghijklmnop"}}]
    with pytest.raises(pr.ProfileValidationError, match="secret-shaped"):
        pr.resolve_effective_profile(layers)


def test_rejects_absolute_path_at_hosted_default():
    layers = [{"scope_type": "hosted_default", "scope_id": "hd", "revision": 1,
               "fields": {"executor_config.test_cmd": r"C:\Users\adam\run_tests.ps1"}}]
    with pytest.raises(pr.ProfileValidationError, match="absolute path"):
        pr.resolve_effective_profile(layers)


def test_rejects_absolute_path_at_workspace_but_allows_at_project():
    bad = [{"scope_type": "workspace", "scope_id": "ws1", "revision": 1,
            "fields": {"executor_config.test_cmd": "/home/adam/run.sh"}}]
    with pytest.raises(pr.ProfileValidationError, match="absolute path"):
        pr.resolve_effective_profile(bad)

    ok = [{"scope_type": "project", "scope_id": "p1", "revision": 1,
           "fields": {"executor_config.repo_path": r"C:\Users\adam\repo"}}]
    effective = pr.resolve_effective_profile(ok)  # must not raise
    assert effective.fields["executor_config.repo_path"] == r"C:\Users\adam\repo"


def test_rejects_unsafe_command_in_command_shaped_field():
    layers = [{"scope_type": "project", "scope_id": "p1", "revision": 1,
               "fields": {"executor_config.deploy_cmd": "rm -rf / && echo done"}}]
    with pytest.raises(pr.ProfileValidationError, match="unsafe"):
        pr.resolve_effective_profile(layers)


def test_rejects_cross_scope_write():
    # executor_config.repo_path is only allowed at project/session.
    layers = [{"scope_type": "hosted_default", "scope_id": "hd", "revision": 1,
               "fields": {"executor_config.repo_path": r"C:\Users\adam\repo"}}]
    with pytest.raises(pr.ProfileValidationError, match="cross-scope write"):
        pr.resolve_effective_profile(layers)


def test_rejects_claim_verification_mode_at_hosted_default():
    layers = [{"scope_type": "hosted_default", "scope_id": "hd", "revision": 1,
               "fields": {"claim_verification_mode": "strict"}}]
    with pytest.raises(pr.ProfileValidationError, match="cross-scope write"):
        pr.resolve_effective_profile(layers)


def test_rejects_invalid_capability_contract():
    layers = [{"scope_type": "project", "scope_id": "p1", "revision": 1,
               "fields": {"capability_manifest_ref": [{"id": "x"}]}}]  # missing purpose/required_tools
    with pytest.raises(pr.ProfileValidationError, match="invalid capability contract"):
        pr.resolve_effective_profile(layers)


def test_rejects_unknown_field():
    layers = [{"scope_type": "project", "scope_id": "p1", "revision": 1,
               "fields": {"totally_made_up_field": 1}}]
    with pytest.raises(pr.ProfileValidationError, match="unknown profile field"):
        pr.resolve_effective_profile(layers)


def test_rejects_unknown_reset_field():
    layers = [{"scope_type": "project", "scope_id": "p1", "revision": 1,
               "reset_fields": ["totally_made_up_field"]}]
    with pytest.raises(pr.ProfileValidationError, match="unknown field"):
        pr.resolve_effective_profile(layers)


def test_rejects_wrong_type_for_field():
    layers = [{"scope_type": "project", "scope_id": "p1", "revision": 1,
               "fields": {"max_pinned_decisions": "thirty"}}]
    with pytest.raises(pr.ProfileValidationError, match="expected int"):
        pr.resolve_effective_profile(layers)


def test_rejects_null_field_value_use_reset_fields_instead():
    layers = [{"scope_type": "project", "scope_id": "p1", "revision": 1,
               "fields": {"max_pinned_decisions": None}}]
    with pytest.raises(pr.ProfileValidationError, match="reset_fields"):
        pr.resolve_effective_profile(layers)


def test_rejects_lifecycle_state_on_non_hosted_default_layer():
    layers = [{"scope_type": "project", "scope_id": "p1", "revision": 1,
               "lifecycle_state": "active"}]
    with pytest.raises(pr.ProfileValidationError, match="lifecycle_state"):
        pr.resolve_effective_profile(layers)


def test_rejects_unsupported_schema_version():
    layers = [{"scope_type": "project", "scope_id": "p1", "revision": 1, "schema_version": 99}]
    with pytest.raises(pr.ProfileValidationError, match="schema_version"):
        pr.resolve_effective_profile(layers)


# ---------------------------------------------------------------------------
# Stale revision rejection
# ---------------------------------------------------------------------------


def test_check_expected_revision_none_is_last_write_wins():
    pr.check_expected_revision(current_revision=5, expected_revision=None)  # must not raise


def test_check_expected_revision_matching_passes():
    pr.check_expected_revision(current_revision=5, expected_revision=5)  # must not raise


def test_check_expected_revision_stale_raises():
    with pytest.raises(pr.ProfileStaleRevisionError):
        pr.check_expected_revision(current_revision=5, expected_revision=3)


def test_profile_stale_revision_error_is_a_profile_validation_error():
    assert issubclass(pr.ProfileStaleRevisionError, pr.ProfileValidationError)


# ---------------------------------------------------------------------------
# Lifecycle state machine
# ---------------------------------------------------------------------------


def test_lifecycle_new_profile_must_start_draft():
    pr.validate_lifecycle_transition(None, "draft")  # must not raise
    with pytest.raises(pr.ProfileValidationError):
        pr.validate_lifecycle_transition(None, "active")


@pytest.mark.parametrize("current,new", [
    ("draft", "active"), ("draft", "retired"),
    ("active", "deprecated"),
    ("deprecated", "retired"), ("deprecated", "active"),
])
def test_lifecycle_valid_transitions(current, new):
    pr.validate_lifecycle_transition(current, new)  # must not raise


@pytest.mark.parametrize("current,new", [
    ("active", "draft"), ("retired", "active"), ("draft", "deprecated"),
])
def test_lifecycle_invalid_transitions_rejected(current, new):
    with pytest.raises(pr.ProfileValidationError):
        pr.validate_lifecycle_transition(current, new)


def test_lifecycle_same_state_is_idempotent_noop():
    for state in ("draft", "active", "deprecated", "retired"):
        pr.validate_lifecycle_transition(state, state)  # must not raise


# ---------------------------------------------------------------------------
# executable / degraded status from hosted_default lifecycle
# ---------------------------------------------------------------------------


def test_retired_hosted_default_is_not_executable():
    layers = [{"scope_type": "hosted_default", "scope_id": "hd", "revision": 1,
               "lifecycle_state": "retired"}]
    effective = pr.resolve_effective_profile(layers)
    assert effective.executable is False
    assert "hosted_default_retired" in effective.executable_reasons
    assert effective.degraded is True


def test_draft_hosted_default_is_degraded_but_executable():
    layers = [{"scope_type": "hosted_default", "scope_id": "hd", "revision": 1,
               "lifecycle_state": "draft"}]
    effective = pr.resolve_effective_profile(layers)
    assert effective.executable is True
    assert effective.degraded is True
    assert "hosted_default_lifecycle_draft" in effective.degraded_reasons


def test_active_hosted_default_is_neither_degraded_nor_blocked():
    layers = [{"scope_type": "hosted_default", "scope_id": "hd", "revision": 1,
               "lifecycle_state": "active"}]
    effective = pr.resolve_effective_profile(layers)
    assert effective.executable is True
    assert effective.degraded is False


# ---------------------------------------------------------------------------
# changed_fields
# ---------------------------------------------------------------------------


def test_changed_fields_against_defaults_when_no_previous_given():
    layers = [{"scope_type": "project", "scope_id": "p1", "revision": 1,
               "fields": {"auto_worktrees": 0}}]
    effective = pr.resolve_effective_profile(layers)
    assert effective.changed_fields == {
        "auto_worktrees": {"old": 1, "new": 0},
    }


def test_changed_fields_against_explicit_previous():
    layers = [{"scope_type": "project", "scope_id": "p1", "revision": 1,
               "fields": {"auto_worktrees": 0, "code_intel_enabled": 1}}]
    # A complete previous baseline (registry defaults with auto_worktrees
    # already at 0) -- only code_intel_enabled should show up as changed.
    previous = {name: spec.default for name, spec in pr.FIELD_REGISTRY.items()}
    previous["auto_worktrees"] = 0
    effective = pr.resolve_effective_profile(layers, previous_effective_fields=previous)
    assert effective.changed_fields == {"code_intel_enabled": {"old": 0, "new": 1}}


def test_no_change_yields_no_restart_or_refresh_required():
    layers = [{"scope_type": "project", "scope_id": "p1", "revision": 1,
               "fields": {"auto_worktrees": 0}}]
    effective = pr.resolve_effective_profile(
        layers, previous_effective_fields=effective_fields_after_first_apply(layers),
    )
    assert effective.changed_fields == {}
    assert effective.restart_required is False
    assert effective.refresh_required is False
    assert all(v == "none" for v in effective.restart_report.values())


def effective_fields_after_first_apply(layers):
    return pr.resolve_effective_profile(layers).fields


# ---------------------------------------------------------------------------
# Restart/refresh classification per component
# ---------------------------------------------------------------------------


def test_connector_restart_required_for_shell_type_change():
    layers = [{"scope_type": "project", "scope_id": "p1", "revision": 1,
               "fields": {"executor_config.shell_type": "pwsh"}}]
    effective = pr.resolve_effective_profile(layers)
    assert effective.restart_report["connector"] == "restart_required"
    assert effective.restart_report["tunnel"] == "none"
    assert effective.restart_report["capability"] == "none"
    assert effective.restart_required is True
    assert effective.refresh_required is True


def test_general_explicit_refresh_required_for_hitl_auto_answer_change():
    layers = [{"scope_type": "project", "scope_id": "p1", "revision": 1,
               "fields": {"hitl_auto_answer": 1}}]
    effective = pr.resolve_effective_profile(layers)
    assert effective.restart_report["general"] == "explicit_refresh_required"
    assert effective.restart_required is False
    assert effective.refresh_required is True


def test_general_hot_reload_for_auto_worktrees_change():
    layers = [{"scope_type": "project", "scope_id": "p1", "revision": 1,
               "fields": {"auto_worktrees": 0}}]
    effective = pr.resolve_effective_profile(layers)
    assert effective.restart_report["general"] == "hot_reload"
    assert effective.restart_required is False
    assert effective.refresh_required is False


def test_capability_restart_required_for_capability_manifest_ref_change():
    layers = [{"scope_type": "project", "scope_id": "p1", "revision": 1,
               "fields": {"capability_manifest_ref": [_cap("serena")]}}]
    effective = pr.resolve_effective_profile(layers)
    assert effective.restart_report["capability"] == "restart_required"


def test_tunnel_component_is_always_none_in_current_registry():
    # No field is currently classified as component="tunnel" -- reserved,
    # per 62c41508's own "restart_required, the last reserved" note.
    assert not any(spec.component == "tunnel" for spec in pr.FIELD_REGISTRY.values())
    layers = [{"scope_type": "hosted_default", "scope_id": "hd", "revision": 1,
               "fields": {"capability_manifest_ref": [_cap("x")]}}]
    effective = pr.resolve_effective_profile(layers)
    assert effective.restart_report["tunnel"] == "none"


# ---------------------------------------------------------------------------
# generation_key
# ---------------------------------------------------------------------------


def test_generation_key_order_independent():
    a = [
        {"scope_type": "session", "scope_id": "s1", "revision": 2, "fields": {}},
        {"scope_type": "project", "scope_id": "p1", "revision": 5, "fields": {}},
    ]
    b = list(reversed(a))
    ga = pr.resolve_effective_profile(a).generation_key
    gb = pr.resolve_effective_profile(b).generation_key
    assert ga == gb


def test_generation_key_changes_when_revision_changes():
    layers_v1 = [{"scope_type": "project", "scope_id": "p1", "revision": 1, "fields": {}}]
    layers_v2 = [{"scope_type": "project", "scope_id": "p1", "revision": 2, "fields": {}}]
    g1 = pr.resolve_effective_profile(layers_v1).generation_key
    g2 = pr.resolve_effective_profile(layers_v2).generation_key
    assert g1 != g2


def test_generation_key_keyed_on_identity_not_field_content():
    # Same (scope_type, scope_id, revision) triple -> same generation_key even
    # though field content differs -- generation_key trusts revision to
    # already be content-hash-ledgered by whatever persistence layer owns it
    # (see compute_generation_key's docstring).
    layers_a = [{"scope_type": "project", "scope_id": "p1", "revision": 1,
                 "fields": {"auto_worktrees": 0}}]
    layers_b = [{"scope_type": "project", "scope_id": "p1", "revision": 1,
                 "fields": {"auto_worktrees": 1}}]
    ga = pr.resolve_effective_profile(layers_a).generation_key
    gb = pr.resolve_effective_profile(layers_b).generation_key
    assert ga == gb


# ---------------------------------------------------------------------------
# layers_applied (source layers reporting)
# ---------------------------------------------------------------------------


def test_layers_applied_reports_source_layers_in_scope_order():
    layers = [
        {"scope_type": "session", "scope_id": "s1", "revision": 3, "fields": {}},
        {"scope_type": "hosted_default", "scope_id": "hd", "revision": 1, "fields": {},
         "lifecycle_state": "active"},
        {"scope_type": "project", "scope_id": "p1", "revision": 2, "fields": {}},
    ]
    effective = pr.resolve_effective_profile(layers)
    assert [l["scope_type"] for l in effective.layers_applied] == [
        "hosted_default", "project", "session",
    ]


# ---------------------------------------------------------------------------
# ProfileLayer schema validation surfaced through resolve_effective_profile
# ---------------------------------------------------------------------------


def test_invalid_scope_type_rejected():
    with pytest.raises(pr.ProfileValidationError):
        pr.resolve_effective_profile([{"scope_type": "galaxy", "scope_id": "x", "revision": 1}])


def test_empty_scope_id_rejected():
    with pytest.raises(pr.ProfileValidationError):
        pr.resolve_effective_profile([{"scope_type": "project", "scope_id": "", "revision": 1}])
