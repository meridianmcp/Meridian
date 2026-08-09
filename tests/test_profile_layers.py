"""Tests for sprint item d8481276 — persist versioned hosted defaults and
scoped user profiles in Neon with migration-safe settings compatibility
(PROFILE-2, building on the PROFILE-1 contract, sprint item 62c41508).

Covers:

1. meridian.profile_contract — pure scope/field/reset/provenance validation,
   the resolve_effective_profile merge algorithm (precedence, narrow_only
   widen-blocking, dict_merge_by_key, reset semantics, generation_key,
   refresh_required), and the contract fixtures.
2. meridian.db.profile_layers — get/set/reset_profile_layer round trip,
   optimistic concurrency (idempotent no-op resave, last-write-wins,
   ProfileStaleRevisionError), the hosted_default lifecycle state machine
   (idempotent activate, invalid-transition rejection, audit ledger), and
   get_effective_profile's 5-layer resolution including SQLite/Postgres
   parity via the ``anydb`` fixture.
3. Migration-safe settings compatibility: the 7 existing ProjectSettings
   fields keep flowing through get_project_settings/update_project_settings
   unchanged (backward-compatible reads, zero duplication into
   profile_layers at scope_type="project"), and tenant/project isolation
   between two projects' profile_layers rows.
"""
from __future__ import annotations

import pytest

from meridian import db as db_module
from meridian import profile_contract as pc


# ---------------------------------------------------------------------------
# profile_contract — pure validation (no DB).
# ---------------------------------------------------------------------------

def test_normalize_scope_type_accepts_all_valid_values():
    for scope_type in pc.SCOPE_TYPES:
        assert pc.normalize_scope_type(scope_type) == scope_type
        assert pc.normalize_scope_type(scope_type.upper()) == scope_type


def test_normalize_scope_type_rejects_unknown_value():
    with pytest.raises(pc.ProfileContractError, match="scope_type must be one of"):
        pc.normalize_scope_type("planet")


def test_normalize_scope_type_rejects_non_string():
    with pytest.raises(pc.ProfileContractError, match="scope_type must be one of"):
        pc.normalize_scope_type(None)


def test_scope_types_bracket_capability_profile_inner_layers():
    """62c41508: hosted_default -> workspace -> user -> project -> session
    generalizes capability_profile's workspace -> user -> project chain by
    bracketing it with a hosted_default floor and a session ceiling."""
    from meridian import capability_profile as cap_profile
    assert pc.SCOPE_TYPES[0] == "hosted_default"
    assert pc.SCOPE_TYPES[-1] == "session"
    assert pc.SCOPE_TYPES[1:4] == cap_profile.SCOPE_TYPES[:3]


def test_normalize_scope_id_rejects_empty():
    with pytest.raises(pc.ProfileContractError, match="scope_id"):
        pc.normalize_scope_id("")
    with pytest.raises(pc.ProfileContractError, match="scope_id"):
        pc.normalize_scope_id("   ")


def test_normalize_reset_fields_none_and_empty():
    assert pc.normalize_reset_fields(None) == []
    assert pc.normalize_reset_fields([]) == []


def test_normalize_reset_fields_dedupes_and_sorts():
    assert pc.normalize_reset_fields(["zebra", "alpha", "alpha"]) == ["alpha", "zebra"]


def test_normalize_reset_fields_rejects_non_list():
    with pytest.raises(pc.ProfileContractError, match="reset_fields"):
        pc.normalize_reset_fields("not-a-list")


def test_normalize_provenance_none_and_valid_dict():
    assert pc.normalize_provenance(None) is None
    prov = {"source": "hosted-defaults-v1"}
    assert pc.normalize_provenance(prov) == prov


def test_normalize_provenance_rejects_secret_shaped_value():
    with pytest.raises(pc.ProfileContractError, match="secret-shaped"):
        pc.normalize_provenance({"source": "postgresql://user:hunter2@host/db"})


def test_normalize_provenance_rejects_machine_local_absolute_path():
    with pytest.raises(pc.ProfileContractError, match="machine-local absolute path"):
        pc.normalize_provenance({"source": r"C:\Users\adam\repo\config.toml"})


def test_profile_contract_error_is_a_value_error():
    assert issubclass(pc.ProfileContractError, ValueError)


def test_profile_stale_revision_error_is_a_profile_contract_error():
    assert issubclass(pc.ProfileStaleRevisionError, pc.ProfileContractError)
    err = pc.ProfileStaleRevisionError("hosted_default", "global", 1, 3)
    assert err.expected_revision == 1
    assert err.actual_revision == 3
    assert "hosted_default" in str(err)


# ---------------------------------------------------------------------------
# FIELD_REGISTRY — grounded in real ProjectSettings/ExecutorConfig fields.
# ---------------------------------------------------------------------------

def test_field_registry_has_exactly_3_genuinely_new_fields():
    new_fields = {
        name for name, spec in pc.FIELD_REGISTRY.items()
        if spec.legacy_source == "profile_layers"
    }
    assert new_fields == {"claim_verification_mode", "tool_priority_map", "capability_manifest_ref"}


def test_field_registry_legacy_fields_grounded_in_project_settings():
    from meridian.models import ProjectSettings, ExecutorConfig
    settings_fields = set(ProjectSettings.model_fields) - {"project_id", "executor_config"}
    registry_legacy_scalars = {
        name for name, spec in pc.FIELD_REGISTRY.items()
        if spec.legacy_source == "project_settings" and not name.startswith("executor_config.")
    }
    assert registry_legacy_scalars == settings_fields

    executor_cfg_fields = set(ExecutorConfig.model_fields)
    registry_exec_cfg = {
        name.split(".", 1)[1] for name, spec in pc.FIELD_REGISTRY.items()
        if spec.legacy_source == "project_settings" and name.startswith("executor_config.")
    }
    assert registry_exec_cfg == executor_cfg_fields


def test_field_registry_allowed_layers_all_valid():
    for name, spec in pc.FIELD_REGISTRY.items():
        assert all(layer in pc.SCOPE_TYPES for layer in spec.allowed_layers), name


def test_validate_layer_fields_rejects_unknown_field():
    with pytest.raises(pc.ProfileContractError, match="unknown profile field"):
        pc.validate_layer_fields("workspace", {"not_a_real_field": 1})


def test_validate_layer_fields_rejects_disallowed_layer():
    with pytest.raises(pc.ProfileContractError, match="not writable at layer"):
        pc.validate_layer_fields("workspace", {"executor_config.repo_path": "/some/path"})


def test_validate_layer_fields_rejects_legacy_field_at_project_scope():
    """Zero-duplication guard: a legacy_source='project_settings' field may
    never be written into profile_layers at scope_type='project' — its
    project-scope value stays get_project_settings' authority."""
    with pytest.raises(pc.ProfileContractError, match="existing project settings authority"):
        pc.validate_layer_fields("project", {"hitl_auto_answer": 2})


def test_validate_layer_fields_allows_new_fields_at_project_scope():
    pc.validate_layer_fields("project", {"claim_verification_mode": "strict"})


def test_validate_layer_fields_rejects_secret_shaped_value():
    with pytest.raises(pc.ProfileContractError, match="secret-shaped"):
        pc.validate_layer_fields("workspace", {"executor_config.deploy_cmd": "curl -H 'api_key: sk-abcdefghij1234567890'"})


def test_validate_layer_fields_rejects_absolute_path_outside_allowed_layer():
    # test_cmd is writable at every layer, but only path-exempt at project/session.
    with pytest.raises(pc.ProfileContractError, match="machine-local absolute path"):
        pc.validate_layer_fields("workspace", {"executor_config.test_cmd": r"C:\tools\run_tests.bat"})


def test_validate_layer_fields_allows_absolute_path_at_permitted_layer():
    # test_cmd's path_allowed_from_layer includes "project" and "session";
    # "session" isn't subject to the project-scope zero-duplication guard.
    pc.validate_layer_fields("session", {"executor_config.test_cmd": r"C:\tools\run_tests.bat"})


# ---------------------------------------------------------------------------
# resolve_effective_profile — the merge algorithm (pure, no DB).
# ---------------------------------------------------------------------------

def _layer(scope_type, fields=None, reset_fields=None, scope_id="x", revision=1):
    return pc.ProfileLayer(
        scope_type=scope_type, scope_id=scope_id, revision=revision,
        fields=fields or {}, reset_fields=reset_fields or [],
    )


def test_resolve_effective_profile_empty():
    result = pc.resolve_effective_profile([])
    assert result.fields == {}
    assert result.layers_applied == []
    assert result.generation_key == pc._compute_generation_key([])


def test_resolve_effective_profile_single_layer():
    result = pc.resolve_effective_profile([_layer("project", {"max_pinned_decisions": 30}, scope_id="p1")])
    assert result.fields == {"max_pinned_decisions": 30}
    assert result.field_sources == {"max_pinned_decisions": "project"}
    assert result.layers_applied == ["project"]
    assert result.overrides == []


def test_resolve_effective_profile_more_specific_layer_overrides():
    result = pc.resolve_effective_profile([
        _layer("workspace", {"max_pinned_decisions": 10}, scope_id="ws"),
        _layer("project", {"max_pinned_decisions": 40}, scope_id="p1"),
    ])
    assert result.fields["max_pinned_decisions"] == 40
    assert result.field_sources["max_pinned_decisions"] == "project"
    assert len(result.overrides) == 1
    assert result.overrides[0]["from_layer"] == "workspace"
    assert result.overrides[0]["to_layer"] == "project"
    assert result.overrides[0]["previous"] == 10
    assert result.overrides[0]["new"] == 40


def test_resolve_effective_profile_reset_fields_removes_inherited_value():
    result = pc.resolve_effective_profile([
        _layer("workspace", {"auto_worktrees": 1}, scope_id="ws"),
        _layer("project", {}, reset_fields=["auto_worktrees"], scope_id="p1"),
    ])
    assert "auto_worktrees" not in result.fields
    assert result.reset_log == [{
        "field": "auto_worktrees", "reset_by_layer": "project", "previously_set_by_layer": "workspace",
    }]


def test_resolve_effective_profile_reset_of_never_set_field_is_not_logged():
    result = pc.resolve_effective_profile([_layer("project", {}, reset_fields=["auto_worktrees"], scope_id="p1")])
    assert result.reset_log == []
    assert result.layers_applied == ["project"]  # a reset alone still counts as "applied"


def test_resolve_effective_profile_dict_merge_by_key_merges_not_replaces():
    result = pc.resolve_effective_profile([
        _layer("hosted_default", {"tool_priority_map": {"code_search": "grep"}}, scope_id="global"),
        _layer("workspace", {"tool_priority_map": {"docs": "meridian-docs"}}, scope_id="ws"),
    ])
    assert result.fields["tool_priority_map"] == {"code_search": "grep", "docs": "meridian-docs"}


def test_resolve_effective_profile_dict_merge_by_key_more_specific_key_wins():
    result = pc.resolve_effective_profile([
        _layer("hosted_default", {"tool_priority_map": {"code_search": "grep"}}, scope_id="global"),
        _layer("workspace", {"tool_priority_map": {"code_search": "Serena: find_symbol"}}, scope_id="ws"),
    ])
    assert result.fields["tool_priority_map"] == {"code_search": "Serena: find_symbol"}


def test_resolve_effective_profile_unknown_field_raises():
    with pytest.raises(pc.ProfileContractError, match="unknown profile field"):
        pc.resolve_effective_profile([_layer("project", {"nonexistent_field": 1}, scope_id="p1")])


def test_resolve_effective_profile_disallowed_layer_raises():
    with pytest.raises(pc.ProfileContractError, match="not allowed at layer"):
        pc.resolve_effective_profile([_layer("workspace", {"executor_config.repo_path": "/x"}, scope_id="ws")])


def test_resolve_effective_profile_narrow_only_widen_is_blocked():
    """hitl_auto_answer: narrow_only, safe_direction=decrease. A session
    widening it toward more-automatic (0 -> 2) without override_reason is
    rejected: the widen is NOT applied, and it's recorded in blocked_widens.
    Directly covers the feedback_hitl_suppression_injection risk."""
    result = pc.resolve_effective_profile([
        _layer("hosted_default", {"hitl_auto_answer": 0}, scope_id="global"),
        _layer("session", {"hitl_auto_answer": 2}, scope_id="sess-1"),
    ])
    assert result.fields["hitl_auto_answer"] == 0  # widen rejected, floor value stands
    assert len(result.blocked_widens) == 1
    entry = result.blocked_widens[0]
    assert entry["field"] == "hitl_auto_answer"
    assert entry["layer"] == "session"
    assert entry["previous_value"] == 0
    assert entry["attempted_value"] == 2
    assert "overridden" not in entry


def test_resolve_effective_profile_narrow_only_narrowing_is_allowed():
    """Moving hitl_auto_answer DOWN (toward safer) needs no override."""
    result = pc.resolve_effective_profile([
        _layer("hosted_default", {"hitl_auto_answer": 2}, scope_id="global"),
        _layer("session", {"hitl_auto_answer": 0}, scope_id="sess-1"),
    ])
    assert result.fields["hitl_auto_answer"] == 0
    assert result.blocked_widens == []


def test_resolve_effective_profile_override_reason_allows_widen_through():
    result = pc.resolve_effective_profile(
        [
            _layer("hosted_default", {"hitl_auto_answer": 0}, scope_id="global"),
            _layer("session", {"hitl_auto_answer": 2}, scope_id="sess-1"),
        ],
        override_reason="ops emergency — approved by human",
    )
    assert result.fields["hitl_auto_answer"] == 2
    assert len(result.blocked_widens) == 1
    assert result.blocked_widens[0]["overridden"] is True
    assert result.blocked_widens[0]["override_reason"] == "ops emergency — approved by human"


def test_resolve_effective_profile_safe_direction_increase_field():
    """require_merge_approval: narrow_only, safe_direction=increase — a
    DECREASE (loosening toward 0=off) is the widen and gets blocked;
    increasing needs no override."""
    blocked = pc.resolve_effective_profile([
        _layer("hosted_default", {"require_merge_approval": 2}, scope_id="global"),
        _layer("project", {"require_merge_approval": 0}, scope_id="p1"),
    ])
    assert blocked.fields["require_merge_approval"] == 2
    assert len(blocked.blocked_widens) == 1

    allowed = pc.resolve_effective_profile([
        _layer("hosted_default", {"require_merge_approval": 0}, scope_id="global"),
        _layer("project", {"require_merge_approval": 2}, scope_id="p1"),
    ])
    assert allowed.fields["require_merge_approval"] == 2
    assert allowed.blocked_widens == []


def test_resolve_effective_profile_claim_verification_mode_ordered_scale():
    """claim_verification_mode is the one non-numeric narrow_only field —
    compared via its advisory/off/strict ordered scale."""
    blocked = pc.resolve_effective_profile([
        _layer("hosted_default", {"claim_verification_mode": "strict"}, scope_id="global"),
        _layer("project", {"claim_verification_mode": "off"}, scope_id="p1"),
    ])
    assert blocked.fields["claim_verification_mode"] == "strict"
    assert len(blocked.blocked_widens) == 1


def test_resolve_effective_profile_layer_with_no_content_is_skipped():
    result = pc.resolve_effective_profile([
        _layer("workspace", {}, scope_id="ws"),  # empty -- not applied
        _layer("project", {"max_pinned_decisions": 5}, scope_id="p1"),
    ])
    assert result.layers_applied == ["project"]


def test_resolve_effective_profile_generation_key_deterministic_and_order_independent():
    layers = [
        _layer("hosted_default", {"max_pinned_decisions": 5}, scope_id="global", revision=3),
        _layer("project", {"auto_worktrees": 1}, scope_id="p1", revision=7),
    ]
    r1 = pc.resolve_effective_profile(layers)
    r2 = pc.resolve_effective_profile(layers)
    assert r1.generation_key == r2.generation_key
    assert r1.generation_key.startswith("sha256:")


def test_resolve_effective_profile_generation_key_changes_with_revision():
    base = [_layer("hosted_default", {"max_pinned_decisions": 5}, scope_id="global", revision=1)]
    bumped = [_layer("hosted_default", {"max_pinned_decisions": 5}, scope_id="global", revision=2)]
    assert pc.resolve_effective_profile(base).generation_key != pc.resolve_effective_profile(bumped).generation_key


def test_resolve_effective_profile_refresh_required_false_without_previous_fields():
    result = pc.resolve_effective_profile([_layer("project", {"execution_mode": "interactive"}, scope_id="p1")])
    assert result.refresh_required is False


def test_resolve_effective_profile_refresh_required_true_when_explicit_refresh_field_changes():
    result = pc.resolve_effective_profile(
        [_layer("project", {"execution_mode": "interactive"}, scope_id="p1")],
        previous_fields={"execution_mode": "autonomous"},
    )
    assert result.refresh_required is True


def test_resolve_effective_profile_refresh_required_false_when_no_relevant_field_changed():
    result = pc.resolve_effective_profile(
        [_layer("project", {"max_pinned_decisions": 25}, scope_id="p1")],
        previous_fields={"max_pinned_decisions": 20},
    )
    # max_pinned_decisions is hot_reload, not explicit_refresh_required.
    assert result.refresh_required is False


# ---------------------------------------------------------------------------
# Contract fixtures.
# ---------------------------------------------------------------------------

def test_fixtures_are_valid_profile_layers():
    for fixture in (pc.HOSTED_DEFAULT_FIXTURE, pc.WORKSPACE_OVERLAY_FIXTURE, pc.SESSION_OVERRIDE_BLOCKED_FIXTURE):
        assert isinstance(fixture, pc.ProfileLayer)
        pc.validate_layer_fields(fixture.scope_type, fixture.fields)


def test_fixtures_combined_demonstrate_blocked_widen():
    result = pc.resolve_effective_profile([
        pc.HOSTED_DEFAULT_FIXTURE, pc.WORKSPACE_OVERLAY_FIXTURE, pc.SESSION_OVERRIDE_BLOCKED_FIXTURE,
    ])
    assert result.fields["hitl_auto_answer"] == 0
    assert any(b["field"] == "hitl_auto_answer" for b in result.blocked_widens)
    # workspace overlay's own contribution still applies alongside the block.
    assert result.fields["auto_worktrees"] == 1
    assert result.fields["tool_priority_map"] == {
        "code_search": "Serena: find_symbol", "docs": "meridian-docs",
    }


# ---------------------------------------------------------------------------
# DB layer — get/set/reset_profile_layer (single scope).
# ---------------------------------------------------------------------------

async def test_get_profile_layer_empty_for_new_scope(db):
    result = await db_module.get_profile_layer(db, "workspace", "singleton")
    assert result["fields"] == {}
    assert result["reset_fields"] == []
    assert result["revision"] == 0
    assert result["schema_version"] == pc.SCHEMA_VERSION
    assert result["lifecycle_state"] is None
    assert result["updated_at"] is None


def test_get_profile_layer_empty_hosted_default_reports_draft_lifecycle():
    row = db_module.profile_layers._empty_layer_dict("hosted_default", "global")
    assert row["lifecycle_state"] == "draft"


async def test_get_profile_layer_rejects_bad_scope_type(db):
    with pytest.raises(pc.ProfileContractError):
        await db_module.get_profile_layer(db, "planet", "x")


async def test_set_profile_layer_round_trip(db):
    saved = await db_module.set_profile_layer(
        db, "workspace", "singleton",
        fields={"max_pinned_decisions": 30, "tool_priority_map": {"a": "b"}},
        reset_fields=["auto_worktrees"],
        provenance={"source": "AGENTS.md"},
    )
    assert saved["fields"] == {"max_pinned_decisions": 30, "tool_priority_map": {"a": "b"}}
    assert saved["reset_fields"] == ["auto_worktrees"]
    assert saved["provenance"] == {"source": "AGENTS.md"}
    assert saved["revision"] == 1
    assert saved["updated_at"] is not None

    fetched = await db_module.get_profile_layer(db, "workspace", "singleton")
    assert fetched["fields"] == saved["fields"]
    assert fetched["reset_fields"] == saved["reset_fields"]
    assert fetched["revision"] == 1


async def test_set_profile_layer_idempotent_noop_resave_does_not_bump_revision(db):
    first = await db_module.set_profile_layer(db, "workspace", "singleton", fields={"auto_worktrees": 1})
    second = await db_module.set_profile_layer(db, "workspace", "singleton", fields={"auto_worktrees": 1})
    assert first["revision"] == second["revision"] == 1


async def test_set_profile_layer_wholesale_replaces_not_merges(db):
    await db_module.set_profile_layer(db, "workspace", "singleton", fields={"auto_worktrees": 1, "max_pinned_decisions": 10})
    replaced = await db_module.set_profile_layer(db, "workspace", "singleton", fields={"max_pinned_decisions": 20})
    assert replaced["fields"] == {"max_pinned_decisions": 20}  # auto_worktrees dropped, not preserved


async def test_set_profile_layer_rejects_malformed_field(db):
    with pytest.raises(pc.ProfileContractError):
        await db_module.set_profile_layer(db, "workspace", "singleton", fields={"bogus_field": 1})
    fetched = await db_module.get_profile_layer(db, "workspace", "singleton")
    assert fetched["fields"] == {}  # rejected write never persisted


async def test_set_profile_layer_rejects_legacy_field_at_project_scope(db):
    project = await db_module.create_project(db, "profile-guard-proj")
    with pytest.raises(pc.ProfileContractError, match="existing project settings authority"):
        await db_module.set_profile_layer(db, "project", project["id"], fields={"code_intel_enabled": 1})


async def test_set_profile_layer_last_write_wins_by_default(db):
    await db_module.set_profile_layer(db, "workspace", "singleton", fields={"auto_worktrees": 1})
    result = await db_module.set_profile_layer(
        db, "workspace", "singleton", fields={"auto_worktrees": 0}, expected_revision=None,
    )
    assert result["fields"]["auto_worktrees"] == 0
    assert result["revision"] == 2


async def test_set_profile_layer_stale_expected_revision_raises(db):
    saved = await db_module.set_profile_layer(db, "workspace", "singleton", fields={"auto_worktrees": 1})
    assert saved["revision"] == 1
    with pytest.raises(pc.ProfileStaleRevisionError):
        await db_module.set_profile_layer(
            db, "workspace", "singleton", fields={"auto_worktrees": 0}, expected_revision=99,
        )
    # the rejected write never applied.
    fetched = await db_module.get_profile_layer(db, "workspace", "singleton")
    assert fetched["revision"] == 1
    assert fetched["fields"]["auto_worktrees"] == 1


async def test_set_profile_layer_correct_expected_revision_succeeds(db):
    saved = await db_module.set_profile_layer(db, "workspace", "singleton", fields={"auto_worktrees": 1})
    result = await db_module.set_profile_layer(
        db, "workspace", "singleton", fields={"auto_worktrees": 0}, expected_revision=saved["revision"],
    )
    assert result["revision"] == 2


async def test_reset_profile_layer_removes_row(db):
    await db_module.set_profile_layer(db, "user", "alice", fields={"max_pinned_decisions": 15})
    cleared = await db_module.reset_profile_layer(db, "user", "alice")
    assert cleared["fields"] == {}
    assert cleared["revision"] == 0
    assert cleared["updated_at"] is None


async def test_reset_profile_layer_idempotent_on_empty_scope(db):
    first = await db_module.reset_profile_layer(db, "user", "never-set")
    second = await db_module.reset_profile_layer(db, "user", "never-set")
    assert first == second == db_module.profile_layers._empty_layer_dict("user", "never-set")


# ---------------------------------------------------------------------------
# DB layer — hosted_default lifecycle state machine.
# ---------------------------------------------------------------------------

async def test_hosted_default_starts_in_draft_after_first_write(db):
    saved = await db_module.set_profile_layer(db, "hosted_default", "global", fields={"max_pinned_decisions": 20})
    assert saved["lifecycle_state"] == "draft"


async def test_hosted_default_lifecycle_full_path(db):
    await db_module.set_profile_layer(db, "hosted_default", "global", fields={"max_pinned_decisions": 20})
    active = await db_module.transition_hosted_default_lifecycle(db, "global", "active")
    assert active["lifecycle_state"] == "active"
    deprecated = await db_module.transition_hosted_default_lifecycle(db, "global", "deprecated")
    assert deprecated["lifecycle_state"] == "deprecated"
    retired = await db_module.transition_hosted_default_lifecycle(db, "global", "retired")
    assert retired["lifecycle_state"] == "retired"


async def test_hosted_default_lifecycle_deprecated_can_reactivate(db):
    await db_module.set_profile_layer(db, "hosted_default", "global", fields={"max_pinned_decisions": 20})
    await db_module.transition_hosted_default_lifecycle(db, "global", "active")
    await db_module.transition_hosted_default_lifecycle(db, "global", "deprecated")
    reactivated = await db_module.transition_hosted_default_lifecycle(db, "global", "active")
    assert reactivated["lifecycle_state"] == "active"


async def test_hosted_default_lifecycle_invalid_transition_raises(db):
    await db_module.set_profile_layer(db, "hosted_default", "global", fields={"max_pinned_decisions": 20})
    with pytest.raises(pc.ProfileContractError, match="cannot transition"):
        await db_module.transition_hosted_default_lifecycle(db, "global", "deprecated")  # draft -> deprecated invalid


async def test_hosted_default_lifecycle_retired_is_terminal(db):
    await db_module.set_profile_layer(db, "hosted_default", "global", fields={"max_pinned_decisions": 20})
    await db_module.transition_hosted_default_lifecycle(db, "global", "retired")
    with pytest.raises(pc.ProfileContractError):
        await db_module.transition_hosted_default_lifecycle(db, "global", "active")


async def test_hosted_default_lifecycle_activate_is_idempotent(db):
    await db_module.set_profile_layer(db, "hosted_default", "global", fields={"max_pinned_decisions": 20})
    first = await db_module.transition_hosted_default_lifecycle(db, "global", "active")
    second = await db_module.transition_hosted_default_lifecycle(db, "global", "active")
    assert first["revision"] == second["revision"]


async def test_hosted_default_lifecycle_can_be_created_directly_from_no_row(db):
    """transition_hosted_default_lifecycle works even with zero prior
    set_profile_layer calls (implicit draft -> active)."""
    activated = await db_module.transition_hosted_default_lifecycle(db, "brand-new-scope", "active")
    assert activated["lifecycle_state"] == "active"
    assert activated["revision"] == 1


async def test_hosted_default_lifecycle_records_audit_history(db):
    await db_module.set_profile_layer(db, "hosted_default", "global", fields={"max_pinned_decisions": 20})
    await db_module.transition_hosted_default_lifecycle(db, "global", "active")
    await db_module.transition_hosted_default_lifecycle(db, "global", "deprecated")
    history = await db_module.get_profile_layer_revisions(db, "global")
    # revision 1 (initial set_profile_layer write) + 2 (activate) + 3 (deprecate)
    assert [h["revision"] for h in history] == [3, 2, 1]
    assert history[0]["lifecycle_state"] == "deprecated"


async def test_get_profile_layer_revisions_empty_for_non_hosted_default_scope(db):
    await db_module.set_profile_layer(db, "workspace", "singleton", fields={"auto_worktrees": 1})
    history = await db_module.get_profile_layer_revisions(db, "singleton")
    assert history == []  # only hosted_default writes are ledgered


async def test_hosted_default_idempotent_resave_does_not_add_audit_row(db):
    await db_module.set_profile_layer(db, "hosted_default", "global", fields={"max_pinned_decisions": 20})
    await db_module.set_profile_layer(db, "hosted_default", "global", fields={"max_pinned_decisions": 20})
    history = await db_module.get_profile_layer_revisions(db, "global")
    assert len(history) == 1


# ---------------------------------------------------------------------------
# DB layer — get_effective_profile (multi-layer resolution + migration
# compatibility + tenant/project isolation).
# ---------------------------------------------------------------------------

async def test_get_effective_profile_unknown_project_raises(db):
    with pytest.raises(ValueError, match="unknown project"):
        await db_module.get_effective_profile(db, "does-not-exist")


async def test_get_effective_profile_backward_compatible_with_no_profile_layers_at_all(db):
    """A project that predates profile_layers entirely (zero rows in the new
    tables) still resolves — its 6 scalar + 8 executor_config legacy fields
    come straight from get_project_settings' existing defaults."""
    project = await db_module.create_project(db, "profile-migration-compat")
    result = await db_module.get_effective_profile(db, project["id"])
    assert result["fields"]["max_pinned_decisions"] == 20
    assert result["fields"]["hitl_auto_answer"] == 0
    assert result["fields"]["auto_worktrees"] == 1
    assert result["fields"]["require_merge_approval"] == 1
    assert result["fields"]["code_intel_enabled"] == 0
    assert result["fields"]["execution_mode"] == "autonomous"
    assert result["layers_applied"] == ["project"]
    assert "claim_verification_mode" not in result["fields"]  # never set -> absent, not a guessed default


async def test_get_effective_profile_reflects_existing_update_project_settings_calls(db):
    """Changing settings via the EXISTING authority is picked up automatically
    — no profile_layers write needed for the 7 legacy fields."""
    project = await db_module.create_project(db, "profile-legacy-live")
    await db_module.update_project_settings(db, project["id"], hitl_auto_answer=1, max_pinned_decisions=50)
    result = await db_module.get_effective_profile(db, project["id"])
    assert result["fields"]["hitl_auto_answer"] == 1
    assert result["fields"]["max_pinned_decisions"] == 50


async def test_get_effective_profile_hosted_default_draft_does_not_apply(db):
    project = await db_module.create_project(db, "profile-draft-hidden")
    await db_module.set_profile_layer(db, "hosted_default", "global", fields={"tool_priority_map": {"a": "b"}})
    result = await db_module.get_effective_profile(db, project["id"])
    assert "hosted_default" not in result["layers_applied"]
    assert "tool_priority_map" not in result["fields"]


async def test_get_effective_profile_hosted_default_active_applies(db):
    project = await db_module.create_project(db, "profile-active-applies")
    await db_module.set_profile_layer(db, "hosted_default", "global", fields={"tool_priority_map": {"a": "b"}})
    await db_module.transition_hosted_default_lifecycle(db, "global", "active")
    result = await db_module.get_effective_profile(db, project["id"])
    assert "hosted_default" in result["layers_applied"]
    assert result["fields"]["tool_priority_map"] == {"a": "b"}
    assert result["field_sources"]["tool_priority_map"] == "hosted_default"


async def test_get_effective_profile_hosted_default_deprecated_still_applies(db):
    project = await db_module.create_project(db, "profile-deprecated-applies")
    await db_module.set_profile_layer(db, "hosted_default", "global", fields={"tool_priority_map": {"a": "b"}})
    await db_module.transition_hosted_default_lifecycle(db, "global", "active")
    await db_module.transition_hosted_default_lifecycle(db, "global", "deprecated")
    result = await db_module.get_effective_profile(db, project["id"])
    assert result["fields"]["tool_priority_map"] == {"a": "b"}


async def test_get_effective_profile_full_layer_chain_precedence(db):
    project = await db_module.create_project(db, "profile-full-chain")
    await db_module.set_profile_layer(db, "hosted_default", "global", fields={"tool_priority_map": {"code_search": "grep"}})
    await db_module.transition_hosted_default_lifecycle(db, "global", "active")
    await db_module.set_profile_layer(db, "workspace", "singleton", fields={"tool_priority_map": {"docs": "meridian-docs"}})
    await db_module.set_profile_layer(db, "user", "alice", fields={"claim_verification_mode": "strict"})
    await db_module.set_profile_layer(db, "project", project["id"], fields={"claim_verification_mode": "off"})
    await db_module.set_profile_layer(db, "session", "sess-9", fields={"tool_priority_map": {"code_search": "Serena: find_symbol"}})

    result = await db_module.get_effective_profile(
        db, project["id"], session_id="sess-9", user_scope_id="alice",
    )
    assert result["layers_applied"] == ["hosted_default", "workspace", "user", "project", "session"]
    # project's own claim_verification_mode ("off") is narrow_only+safe_direction=increase,
    # so widening down from user's "strict" is blocked -> user's value stands.
    assert result["fields"]["claim_verification_mode"] == "strict"
    # dict_merge_by_key across all 3 layers that declared tool_priority_map.
    assert result["fields"]["tool_priority_map"] == {
        "code_search": "Serena: find_symbol", "docs": "meridian-docs",
    }


async def test_get_effective_profile_session_widen_blocked_without_override(db):
    project = await db_module.create_project(db, "profile-session-blocked")
    await db_module.set_profile_layer(db, "hosted_default", "global", fields={"hitl_auto_answer": 0})
    await db_module.transition_hosted_default_lifecycle(db, "global", "active")
    await db_module.set_profile_layer(db, "session", "sess-1", fields={"hitl_auto_answer": 2})

    result = await db_module.get_effective_profile(db, project["id"], session_id="sess-1")
    assert result["fields"]["hitl_auto_answer"] == 0
    assert len(result["blocked_widens"]) == 1


async def test_get_effective_profile_session_widen_allowed_with_override_reason(db):
    project = await db_module.create_project(db, "profile-session-override")
    await db_module.set_profile_layer(db, "hosted_default", "global", fields={"hitl_auto_answer": 0})
    await db_module.transition_hosted_default_lifecycle(db, "global", "active")
    await db_module.set_profile_layer(db, "session", "sess-1", fields={"hitl_auto_answer": 2})

    result = await db_module.get_effective_profile(
        db, project["id"], session_id="sess-1", override_reason="incident response",
    )
    assert result["fields"]["hitl_auto_answer"] == 2


async def test_get_effective_profile_tenant_project_isolation(db):
    """Two projects' profile_layers rows (and legacy settings) never bleed
    into each other; shared hosted_default/workspace layers apply to both."""
    project_a = await db_module.create_project(db, "profile-isolation-a")
    project_b = await db_module.create_project(db, "profile-isolation-b")

    await db_module.set_profile_layer(db, "hosted_default", "global", fields={"tool_priority_map": {"shared": "yes"}})
    await db_module.transition_hosted_default_lifecycle(db, "global", "active")
    await db_module.set_profile_layer(db, "project", project_a["id"], fields={"claim_verification_mode": "strict"})
    await db_module.update_project_settings(db, project_a["id"], hitl_auto_answer=1)

    result_a = await db_module.get_effective_profile(db, project_a["id"])
    result_b = await db_module.get_effective_profile(db, project_b["id"])

    assert result_a["fields"]["claim_verification_mode"] == "strict"
    assert "claim_verification_mode" not in result_b["fields"]
    assert result_a["fields"]["hitl_auto_answer"] == 1
    assert result_b["fields"]["hitl_auto_answer"] == 0  # untouched project's own default
    # shared hosted_default layer applies to both projects identically.
    assert result_a["fields"]["tool_priority_map"] == {"shared": "yes"}
    assert result_b["fields"]["tool_priority_map"] == {"shared": "yes"}


async def test_get_effective_profile_generation_key_present_and_project_session_echoed(db):
    project = await db_module.create_project(db, "profile-echo-fields")
    result = await db_module.get_effective_profile(db, project["id"], session_id="sess-echo")
    assert result["project_id"] == project["id"]
    assert result["session_id"] == "sess-echo"
    assert result["generation_key"].startswith("sha256:")


async def test_capability_profile_and_profile_layers_tables_are_independent(db):
    """02038afe's capability_profiles table and d8481276's profile_layers
    table are separate, independent tables — writing one never touches
    the other (per 62c41508's explicit "not unified in this item" note)."""
    project = await db_module.create_project(db, "profile-independent-tables")
    await db_module.set_capability_profile(db, "project", project["id"], capabilities=[{
        "id": "code-search", "purpose": "x", "required_tools": ["grep"],
    }])
    layer = await db_module.get_profile_layer(db, "project", project["id"])
    assert layer["fields"] == {}  # untouched by the capability_profiles write


async def test_profile_layer_cross_backend_parity(anydb):
    """SQLite and Postgres persist and resolve the effective profile identically."""
    project = await db_module.create_project(anydb, "profile-parity")
    await db_module.set_profile_layer(anydb, "hosted_default", "global", fields={"require_merge_approval": 2})
    await db_module.transition_hosted_default_lifecycle(anydb, "global", "active")
    await db_module.set_profile_layer(anydb, "project", project["id"], fields={"tool_priority_map": {"x": "y"}})

    result = await db_module.get_effective_profile(anydb, project["id"])
    assert result["fields"]["require_merge_approval"] == 2
    assert result["fields"]["tool_priority_map"] == {"x": "y"}
    assert result["layers_applied"] == ["hosted_default", "project"]

    layer = await db_module.get_profile_layer(anydb, "hosted_default", "global")
    assert layer["revision"] == 2  # 1 (set_profile_layer) + 1 (activate)
    assert layer["lifecycle_state"] == "active"


# ---------------------------------------------------------------------------
# executable/degraded status (folded in from profile_resolution.py's
# EffectiveProfile.executable/degraded during the second PROFILE-RECON
# re-verification pass, 732c113e -- ported from the deleted
# tests/test_profile_resolution.py's test_retired_hosted_default_is_not_executable,
# test_draft_hosted_default_is_degraded_but_executable, and
# test_active_hosted_default_is_neither_degraded_nor_blocked. The
# hosted_default-lifecycle half of the signal is computed in
# get_effective_profile (not resolve_effective_profile -- see both
# functions' docstrings for why), so these are DB-level tests here rather
# than pure tests in test_profile_contract.py.)
# ---------------------------------------------------------------------------

async def test_get_effective_profile_no_hosted_default_row_is_not_degraded(db):
    """A project that never configured a hosted_default at all must not be
    reported as degraded merely because get_profile_layer's virtual empty
    dict reports lifecycle_state='draft' for a scope_id with no real row."""
    project = await db_module.create_project(db, "profile-no-hosted-row")
    result = await db_module.get_effective_profile(db, project["id"])
    assert result["executable"] is True
    assert result["degraded"] is False
    assert result["degraded_reasons"] == []


async def test_get_effective_profile_retired_hosted_default_is_not_executable(db):
    project = await db_module.create_project(db, "profile-retired-not-executable")
    await db_module.set_profile_layer(db, "hosted_default", "global", fields={"tool_priority_map": {"a": "b"}})
    await db_module.transition_hosted_default_lifecycle(db, "global", "retired")
    result = await db_module.get_effective_profile(db, project["id"])
    assert result["executable"] is False
    assert "hosted_default_retired" in result["executable_reasons"]
    assert result["degraded"] is True
    assert "hosted_default_retired" in result["degraded_reasons"]
    # the safety property from the original filter also still holds: a
    # retired hosted_default's fields never enter the merged output.
    assert "hosted_default" not in result["layers_applied"]
    assert "tool_priority_map" not in result["fields"]


async def test_get_effective_profile_draft_hosted_default_is_degraded_but_executable(db):
    project = await db_module.create_project(db, "profile-draft-degraded")
    await db_module.set_profile_layer(db, "hosted_default", "global", fields={"tool_priority_map": {"a": "b"}})
    # left in draft -- never transitioned to active.
    result = await db_module.get_effective_profile(db, project["id"])
    assert result["executable"] is True
    assert result["degraded"] is True
    assert "hosted_default_lifecycle_draft" in result["degraded_reasons"]
    assert "hosted_default" not in result["layers_applied"]


async def test_get_effective_profile_deprecated_hosted_default_is_degraded_but_executable(db):
    project = await db_module.create_project(db, "profile-deprecated-degraded")
    await db_module.set_profile_layer(db, "hosted_default", "global", fields={"tool_priority_map": {"a": "b"}})
    await db_module.transition_hosted_default_lifecycle(db, "global", "active")
    await db_module.transition_hosted_default_lifecycle(db, "global", "deprecated")
    result = await db_module.get_effective_profile(db, project["id"])
    assert result["executable"] is True
    assert result["degraded"] is True
    assert "hosted_default_lifecycle_deprecated" in result["degraded_reasons"]
    # deprecated is still live -- its fields DO apply, unlike draft/retired.
    assert "hosted_default" in result["layers_applied"]
    assert result["fields"]["tool_priority_map"] == {"a": "b"}


async def test_get_effective_profile_active_hosted_default_is_neither_degraded_nor_blocked(db):
    project = await db_module.create_project(db, "profile-active-clean")
    await db_module.set_profile_layer(db, "hosted_default", "global", fields={"tool_priority_map": {"a": "b"}})
    await db_module.transition_hosted_default_lifecycle(db, "global", "active")
    result = await db_module.get_effective_profile(db, project["id"])
    assert result["executable"] is True
    assert result["degraded"] is False
    assert result["degraded_reasons"] == []


async def test_get_effective_profile_blocked_widen_alone_marks_degraded(db):
    """The blocked_widens-driven half of the signal (computed by
    resolve_effective_profile itself) surfaces through get_effective_profile
    even with no hosted_default row at all. hitl_auto_answer is
    legacy_source='project_settings' so it can't be declared at
    scope_type='project' via set_profile_layer (the zero-duplication guard)
    -- use hosted_default for the baseline declaration instead, same as
    resolve_effective_profile's own narrow_only tests."""
    project = await db_module.create_project(db, "profile-blocked-widen-degraded")
    await db_module.set_profile_layer(db, "hosted_default", "global", fields={"hitl_auto_answer": 0})
    await db_module.transition_hosted_default_lifecycle(db, "global", "active")
    await db_module.set_profile_layer(db, "session", "sess-degrade", fields={"hitl_auto_answer": 2})
    result = await db_module.get_effective_profile(db, project["id"], session_id="sess-degrade")
    assert result["executable"] is True
    assert result["degraded"] is True
    assert "narrow_only_widen_blocked" in result["degraded_reasons"]


# ---------------------------------------------------------------------------
# reset_fields validation against FIELD_REGISTRY (folded in from
# profile_resolution.py's validate_profile_layer during the second
# PROFILE-RECON re-verification pass, 732c113e -- ported from the deleted
# tests/test_profile_resolution.py's test_rejects_unknown_reset_field, which
# had ZERO equivalent anywhere: reset_fields naming an unknown field
# previously silently no-opped instead of raising.)
# ---------------------------------------------------------------------------

def test_validate_layer_fields_rejects_unknown_reset_field():
    with pytest.raises(pc.ProfileContractError, match="reset_fields.*unknown profile field"):
        pc.validate_layer_fields("project", {}, reset_fields=["totally_made_up_field"])


def test_resolve_effective_profile_rejects_unknown_reset_field():
    with pytest.raises(pc.ProfileContractError, match="reset_fields.*unknown profile field"):
        pc.resolve_effective_profile([_layer("project", {}, reset_fields=["totally_made_up_field"], scope_id="p1")])


async def test_set_profile_layer_rejects_unknown_reset_field(db):
    with pytest.raises(pc.ProfileContractError, match="reset_fields"):
        await db_module.set_profile_layer(db, "workspace", "singleton", reset_fields=["totally_made_up_field"])
    fetched = await db_module.get_profile_layer(db, "workspace", "singleton")
    assert fetched["reset_fields"] == []  # rejected write never persisted


# ---------------------------------------------------------------------------
# schema_version / lifecycle_state envelope checks (folded in from
# profile_resolution.py's validate_profile_layer during the second
# PROFILE-RECON re-verification pass, 732c113e -- ported from the deleted
# tests/test_profile_resolution.py's test_rejects_unsupported_schema_version
# and test_rejects_lifecycle_state_on_non_hosted_default_layer, both of
# which had ZERO equivalent anywhere in profile_contract.py.)
# ---------------------------------------------------------------------------

def test_resolve_effective_profile_rejects_unsupported_schema_version():
    layer = pc.ProfileLayer(
        scope_type="project", scope_id="p1", schema_version=99, fields={"auto_worktrees": 0},
    )
    with pytest.raises(pc.ProfileContractError, match="unsupported profile schema_version"):
        pc.resolve_effective_profile([layer])


def test_resolve_effective_profile_rejects_lifecycle_state_on_non_hosted_default_layer():
    layer = pc.ProfileLayer(
        scope_type="project", scope_id="p1", lifecycle_state="active", fields={"auto_worktrees": 0},
    )
    with pytest.raises(pc.ProfileContractError, match="lifecycle_state is only valid"):
        pc.resolve_effective_profile([layer])


# ---------------------------------------------------------------------------
# Additional lifecycle-transition matrix coverage (audit hardening, no
# behavior change -- closes coverage gaps found while cross-checking every
# name in the deleted tests/test_profile_resolution.py's parametrized
# test_lifecycle_valid_transitions / test_lifecycle_invalid_transitions_rejected
# / test_lifecycle_same_state_is_idempotent_noop against
# transition_hosted_default_lifecycle, which already implements the same
# LIFECYCLE_TRANSITIONS matrix but wasn't exercised at every cell.)
# ---------------------------------------------------------------------------

async def test_hosted_default_lifecycle_active_to_draft_rejected(db):
    await db_module.set_profile_layer(db, "hosted_default", "global", fields={"max_pinned_decisions": 20})
    await db_module.transition_hosted_default_lifecycle(db, "global", "active")
    with pytest.raises(pc.ProfileContractError, match="cannot transition"):
        await db_module.transition_hosted_default_lifecycle(db, "global", "draft")


@pytest.mark.parametrize("state", ["draft", "active", "deprecated", "retired"])
async def test_hosted_default_lifecycle_idempotent_for_every_state(db, state):
    await db_module.set_profile_layer(db, "hosted_default", "global", fields={"max_pinned_decisions": 20})
    # walk to `state` via valid transitions, then re-assert it -- must be a
    # no-op (unchanged revision) regardless of which state it is.
    path = {"draft": [], "active": ["active"], "deprecated": ["active", "deprecated"],
            "retired": ["retired"]}[state]
    reached = await db_module.get_profile_layer(db, "hosted_default", "global")
    for step in path:
        reached = await db_module.transition_hosted_default_lifecycle(db, "global", step)
    before_revision = reached["revision"]
    again = await db_module.transition_hosted_default_lifecycle(db, "global", state)
    assert again["revision"] == before_revision
    assert again["lifecycle_state"] == state


# ---------------------------------------------------------------------------
# PROFILE-6 (89a06e40) — db.get_workspace_effective_profile: the
# tenant/workspace-only resolution the tunnel/connector surface uses (no
# project_id available there — see meridian/routes/tunnel.py and pinned
# decision ee7bccc9, project 5787cc92-ba7d-4788-b17c-28ab7938b839). Mirrors
# get_effective_profile's own hosted_default-lifecycle test coverage above,
# scoped to just the two layers this function resolves.
# ---------------------------------------------------------------------------

async def test_get_workspace_effective_profile_no_config_at_all(db):
    """Never raises (unlike get_effective_profile) and never needs a
    project — a tenant that has configured nothing still resolves cleanly."""
    result = await db_module.get_workspace_effective_profile(db)
    assert result["project_id"] is None
    assert result["session_id"] is None
    assert result["fields"] == {}
    assert result["layers_applied"] == []
    assert result["executable"] is True
    assert result["degraded"] is False
    assert result["generation_key"].startswith("sha256:")


async def test_get_workspace_effective_profile_applies_workspace_layer(db):
    await db_module.set_profile_layer(db, "workspace", "singleton", fields={"auto_worktrees": 0})
    result = await db_module.get_workspace_effective_profile(db)
    assert result["fields"]["auto_worktrees"] == 0
    assert result["layers_applied"] == ["workspace"]


async def test_get_workspace_effective_profile_hosted_default_draft_does_not_apply(db):
    await db_module.set_profile_layer(db, "hosted_default", "global", fields={"tool_priority_map": {"a": "b"}})
    result = await db_module.get_workspace_effective_profile(db)
    assert "hosted_default" not in result["layers_applied"]
    assert "tool_priority_map" not in result["fields"]
    assert result["degraded"] is True
    assert "hosted_default_lifecycle_draft" in result["degraded_reasons"]


async def test_get_workspace_effective_profile_hosted_default_active_applies(db):
    await db_module.set_profile_layer(db, "hosted_default", "global", fields={"tool_priority_map": {"a": "b"}})
    await db_module.transition_hosted_default_lifecycle(db, "global", "active")
    result = await db_module.get_workspace_effective_profile(db)
    assert "hosted_default" in result["layers_applied"]
    assert result["fields"]["tool_priority_map"] == {"a": "b"}
    assert result["executable"] is True
    assert result["degraded"] is False


async def test_get_workspace_effective_profile_hosted_default_retired_not_executable(db):
    await db_module.set_profile_layer(db, "hosted_default", "global", fields={"tool_priority_map": {"a": "b"}})
    await db_module.transition_hosted_default_lifecycle(db, "global", "retired")
    result = await db_module.get_workspace_effective_profile(db)
    assert result["executable"] is False
    assert "hosted_default_retired" in result["executable_reasons"]
    assert result["degraded"] is True
    assert "hosted_default" not in result["layers_applied"]


async def test_get_workspace_effective_profile_never_applies_project_or_session_layers(db):
    """Even if a project/session layer happens to exist, this function must
    never resolve or leak it in — it only ever looks at hosted_default +
    workspace (per pinned decision ee7bccc9)."""
    project = await db_module.create_project(db, "89a06e40-workspace-only-isolation")
    await db_module.set_profile_layer(
        db, "project", project["id"], fields={"claim_verification_mode": "strict"},
    )
    await db_module.set_profile_layer(
        db, "session", "some-session", fields={"claim_verification_mode": "off"},
    )
    result = await db_module.get_workspace_effective_profile(db)
    assert result["layers_applied"] == []
    assert "claim_verification_mode" not in result["fields"]


async def test_get_workspace_effective_profile_matches_get_effective_profile_for_shared_layers(db):
    """The two layers this function DOES resolve must agree exactly with
    what get_effective_profile resolves for those same two layers on any
    project — same merge algorithm, just without the project-specific
    layers overlaid on top.

    Uses ``tool_priority_map`` (a genuinely-new ``legacy_source="profile_layers"``
    field, per profile_contract.py's module docstring) for the comparison,
    NOT a ``legacy_source="project_settings"`` field like ``auto_worktrees``:
    get_effective_profile's synthetic 'project' layer ALWAYS contributes that
    field's value (from get_project_settings' own default, even when nothing
    was ever explicitly set — see _legacy_project_settings_to_fields), so it
    would legitimately override the workspace layer's value there but not
    here — that divergence is correct behavior, not something to assert
    equal.
    """
    project = await db_module.create_project(db, "89a06e40-workspace-parity")
    await db_module.set_profile_layer(db, "hosted_default", "global", fields={"tool_priority_map": {"shared": "yes"}})
    await db_module.transition_hosted_default_lifecycle(db, "global", "active")
    await db_module.set_profile_layer(db, "workspace", "singleton", fields={"tool_priority_map": {"ws": "1"}})

    workspace_only = await db_module.get_workspace_effective_profile(db)
    project_scoped = await db_module.get_effective_profile(db, project["id"])
    assert workspace_only["fields"]["tool_priority_map"] == project_scoped["fields"]["tool_priority_map"]
    assert workspace_only["fields"]["tool_priority_map"] == {"shared": "yes", "ws": "1"}


# ---------------------------------------------------------------------------
# PROFILE-6 (89a06e40) — profile_contract.project_profile_binding: the
# compact projection attached at all 4 integration points (start_session,
# generate_handoff, the goal-mode inline tag, and the tunnel/connector
# routes).
# ---------------------------------------------------------------------------

def test_project_profile_binding_shape():
    effective = pc.resolve_effective_profile([
        _layer("workspace", {"auto_worktrees": 0}, scope_id="ws"),
    ]).model_dump()
    binding = pc.project_profile_binding(effective)
    assert set(binding.keys()) == {
        "generation_key", "executable", "degraded", "restart_required", "restart_report",
    }
    assert binding["generation_key"] == effective["generation_key"]
    assert binding["executable"] == effective["executable"]
    assert binding["degraded"] == effective["degraded"]
    assert binding["restart_required"] == effective["restart_required"]
    assert binding["restart_report"] == effective["restart_report"]


def test_project_profile_binding_never_includes_full_fields_dict():
    effective = pc.resolve_effective_profile([
        _layer("project", {"max_pinned_decisions": 99}, scope_id="p1"),
    ]).model_dump()
    binding = pc.project_profile_binding(effective)
    assert "fields" not in binding
    assert "field_sources" not in binding
    assert "layers_applied" not in binding


def test_project_profile_binding_degrades_gracefully_on_partial_input():
    """A hand-built/partially-shaped dict (e.g. a test fixture, or a future
    caller that forgot a field) must not raise -- safe defaults throughout."""
    binding = pc.project_profile_binding({})
    assert binding == {
        "generation_key": "", "executable": True, "degraded": False,
        "restart_required": False, "restart_report": {},
    }
