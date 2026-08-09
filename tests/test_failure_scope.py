"""6e3f5e44 -- typed stop/continue scope policy across items, clusters,
waves, runs, sprints, and proposals.

Coverage (pure ``meridian.failure_scope`` module -- no DB, mirrors
``tests/test_blocker_policy.py``'s "1. Pure module" section):

1. Taxonomy: scopes, actions, restrictiveness order, per-scope defaults.
2. normalize_scope / normalize_action / normalize_declaration validation.
3. The item spec's own acceptance-test list, each as a dedicated test:
   ordinary continue, ordinary stop (via the legacy adapter), dependency-
   subgraph pause, resource-cluster isolation, wave abort, sprint-version
   abort, proposal-only review, conflicting policies, stale revisions,
   two-project isolation, and corrective resume (supersession lineage).
4. Fail-closed reason-code flooring, including the conflict-resolution edge
   case where the floor must win even when it is not the raw-most-restrictive
   candidate.
5. The legacy ``failure_mode`` bridge, including its lockstep assertion
   against ``meridian.db.wave_runs.WAVE_RUN_CHILD_FAILURE_MODES``.
"""
from __future__ import annotations

import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

import pytest

from meridian import failure_scope as fs

# ---------------------------------------------------------------------------
# 1. Taxonomy
# ---------------------------------------------------------------------------

def test_failure_scopes_taxonomy_has_exactly_eight():
    assert fs.FAILURE_SCOPES == {
        "item", "dependency_subgraph", "resource_cluster", "wave",
        "wave_run", "sprint_version", "project", "proposal",
    }


def test_failure_actions_taxonomy_has_exactly_six():
    assert fs.FAILURE_ACTIONS == {
        "continue_unaffected", "pause_affected", "block_future_claims",
        "require_planner_review", "require_human_decision", "abort_run",
    }


def test_scope_order_excludes_proposal_and_covers_the_rest():
    assert "proposal" not in fs.SCOPE_ORDER
    assert set(fs.SCOPE_ORDER) == (fs.FAILURE_SCOPES - {"proposal"})
    assert fs.SCOPE_ORDER[0] == "item"
    assert fs.SCOPE_ORDER[-1] == "project"


def test_every_scope_has_a_valid_default_action():
    assert set(fs.DEFAULT_ACTION_BY_SCOPE) == fs.FAILURE_SCOPES
    for action in fs.DEFAULT_ACTION_BY_SCOPE.values():
        assert action in fs.FAILURE_ACTIONS


def test_item_default_is_continue_and_proposal_default_is_review():
    assert fs.DEFAULT_ACTION_BY_SCOPE["item"] == "continue_unaffected"
    assert fs.DEFAULT_ACTION_BY_SCOPE["proposal"] == "require_planner_review"


def test_restrictiveness_order_is_total_and_injective():
    assert set(fs.ACTION_RESTRICTIVENESS) == fs.FAILURE_ACTIONS
    values = list(fs.ACTION_RESTRICTIVENESS.values())
    assert len(values) == len(set(values)), "restrictiveness order must be injective"
    assert fs.ACTION_RESTRICTIVENESS["continue_unaffected"] == min(values)
    assert fs.ACTION_RESTRICTIVENESS["abort_run"] == max(values)


# ---------------------------------------------------------------------------
# 2. Normalization
# ---------------------------------------------------------------------------

def test_normalize_scope_accepts_case_and_whitespace_and_rejects_junk():
    assert fs.normalize_scope("  Wave_Run  ") == "wave_run"
    with pytest.raises(fs.FailureScopeError):
        fs.normalize_scope("not_a_scope")
    with pytest.raises(fs.FailureScopeError):
        fs.normalize_scope("")
    with pytest.raises(fs.FailureScopeError):
        fs.normalize_scope(None)


def test_normalize_action_accepts_case_and_whitespace_and_rejects_junk():
    assert fs.normalize_action(" Abort_Run ") == "abort_run"
    with pytest.raises(fs.FailureScopeError):
        fs.normalize_action("not_a_real_action")
    with pytest.raises(fs.FailureScopeError):
        fs.normalize_action("")


def test_normalize_declaration_requires_core_fields():
    with pytest.raises(fs.FailureScopeError):
        fs.normalize_declaration({"id": "d1", "project_id": "p1", "scope": "item"})
    with pytest.raises(fs.FailureScopeError):
        fs.normalize_declaration("not a dict")  # type: ignore[arg-type]


def test_normalize_declaration_defaults_optional_fields():
    d = fs.normalize_declaration({
        "id": "d1", "project_id": "p1", "scope": "item", "key": "i1",
        "action": "continue_unaffected",
    })
    assert d["reason_code"] == "unspecified"
    assert d["evidence_refs"] == ()
    assert d["actor"] is None
    assert d["session_id"] is None
    assert d["board_revision"] is None
    assert d["supersedes"] is None


def test_normalize_declaration_rejects_bad_evidence_refs_type():
    with pytest.raises(fs.FailureScopeError):
        fs.normalize_declaration({
            "id": "d1", "project_id": "p1", "scope": "item", "key": "i1",
            "action": "continue_unaffected", "evidence_refs": "not-a-list",
        })


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

def _chain(item="I1", dep=None, cluster=None, wave="W1", wave_run="R1",
           version="v1.0", project="P1"):
    return [
        ("item", item),
        ("dependency_subgraph", dep),
        ("resource_cluster", cluster),
        ("wave", wave),
        ("wave_run", wave_run),
        ("sprint_version", version),
        ("project", project),
    ]


def _decl(id, scope, key, action, project_id="P1", **kw):
    out = {"id": id, "project_id": project_id, "scope": scope, "key": key, "action": action}
    out.update(kw)
    return out


# ---------------------------------------------------------------------------
# 3a. Ordinary continue
# ---------------------------------------------------------------------------

def test_ordinary_continue_no_declarations_defaults_to_item_continue():
    result = fs.resolve_failure_scope([], target_chain=_chain(), project_id="P1")
    assert result["resolved_scope"] == "item"
    assert result["action"] == "continue_unaffected"
    assert result["source"] == "default"
    assert result["declaration_id"] is None
    assert result["conflict"] is False


# ---------------------------------------------------------------------------
# 3b. Ordinary stop (via legacy adapter, and via a direct item declaration)
# ---------------------------------------------------------------------------

def test_ordinary_stop_direct_item_declaration_wins_over_default():
    decls = [_decl("d1", "item", "I1", "block_future_claims", reason_code="manual_stop")]
    result = fs.resolve_failure_scope(decls, target_chain=_chain(), project_id="P1")
    assert result["resolved_scope"] == "item"
    assert result["action"] == "block_future_claims"
    assert result["source"] == "declared"
    assert result["declaration_id"] == "d1"


def test_legacy_adapter_continue_and_stop_map_correctly():
    assert fs.scope_action_from_legacy_failure_mode(None) == ("item", "continue_unaffected")
    assert fs.scope_action_from_legacy_failure_mode("continue") == ("item", "continue_unaffected")
    assert fs.scope_action_from_legacy_failure_mode("STOP") == ("wave_run", "pause_affected")
    with pytest.raises(fs.FailureScopeError):
        fs.scope_action_from_legacy_failure_mode("bogus")


def test_legacy_failure_modes_stay_in_lockstep_with_wave_runs_module():
    from meridian.db import wave_runs as wr
    assert fs.LEGACY_FAILURE_MODES == wr.WAVE_RUN_CHILD_FAILURE_MODES


# ---------------------------------------------------------------------------
# 3c. Dependency-subgraph pause
# ---------------------------------------------------------------------------

def test_dependency_subgraph_declaration_pauses_without_item_override():
    decls = [_decl("d1", "dependency_subgraph", "ROOT1", "pause_affected")]
    chain = _chain(dep="ROOT1")
    result = fs.resolve_failure_scope(decls, target_chain=chain, project_id="P1")
    assert result["resolved_scope"] == "dependency_subgraph"
    assert result["action"] == "pause_affected"
    assert result["declaration_id"] == "d1"


def test_narrower_item_declaration_beats_wider_dependency_subgraph_declaration():
    decls = [
        _decl("wide", "dependency_subgraph", "ROOT1", "abort_run"),
        _decl("narrow", "item", "I1", "continue_unaffected"),
    ]
    chain = _chain(dep="ROOT1")
    result = fs.resolve_failure_scope(decls, target_chain=chain, project_id="P1")
    assert result["resolved_scope"] == "item"
    assert result["action"] == "continue_unaffected"
    assert result["declaration_id"] == "narrow"


# ---------------------------------------------------------------------------
# 3d. Resource-cluster isolation
# ---------------------------------------------------------------------------

def test_resource_cluster_failure_does_not_affect_a_different_cluster():
    decls = [_decl("d1", "resource_cluster", "CLUSTER_A", "abort_run")]
    unaffected_chain = _chain(cluster="CLUSTER_B")
    result = fs.resolve_failure_scope(decls, target_chain=unaffected_chain, project_id="P1")
    # No match for CLUSTER_B and no other declarations anywhere -> default.
    assert result["source"] == "default"
    assert result["action"] == "continue_unaffected"

    affected_chain = _chain(cluster="CLUSTER_A")
    result2 = fs.resolve_failure_scope(decls, target_chain=affected_chain, project_id="P1")
    assert result2["resolved_scope"] == "resource_cluster"
    assert result2["action"] == "abort_run"


# ---------------------------------------------------------------------------
# 3e. Wave abort
# ---------------------------------------------------------------------------

def test_explicit_wave_declaration_aborts_only_that_wave():
    decls = [_decl("d1", "wave", "W1", "abort_run", reason_code="wave_integrity_failure")]
    result = fs.resolve_failure_scope(decls, target_chain=_chain(wave="W1"), project_id="P1")
    assert result["resolved_scope"] == "wave"
    assert result["action"] == "abort_run"

    other_wave = fs.resolve_failure_scope(decls, target_chain=_chain(wave="W2"), project_id="P1")
    assert other_wave["source"] == "default"
    assert other_wave["action"] == "continue_unaffected"


# ---------------------------------------------------------------------------
# 3f. Sprint-version abort
# ---------------------------------------------------------------------------

def test_explicit_sprint_version_declaration_governs_whole_version():
    decls = [_decl("d1", "sprint_version", "v1.0", "abort_run")]
    result = fs.resolve_failure_scope(decls, target_chain=_chain(version="v1.0"), project_id="P1")
    assert result["resolved_scope"] == "sprint_version"
    assert result["action"] == "abort_run"

    other_version = fs.resolve_failure_scope(
        decls, target_chain=_chain(version="v2.0"), project_id="P1",
    )
    assert other_version["source"] == "default"


# ---------------------------------------------------------------------------
# 3g. Proposal-only review
# ---------------------------------------------------------------------------

def test_proposal_default_is_review_never_silently_executable():
    result = fs.resolve_proposal_failure_scope([], proposal_id="PROP1", project_id="P1")
    assert result["resolved_scope"] == "proposal"
    assert result["action"] == "require_planner_review"
    assert result["source"] == "default"


def test_proposal_explicit_declaration_is_honored_even_if_permissive():
    decls = [_decl("d1", "proposal", "PROP1", "continue_unaffected", reason_code="planner_reviewed")]
    result = fs.resolve_proposal_failure_scope(decls, proposal_id="PROP1", project_id="P1")
    assert result["action"] == "continue_unaffected"
    assert result["source"] == "declared"


def test_proposal_scope_never_appears_in_an_item_target_chain():
    # An executing item's own containment chain must never accidentally
    # pick up a proposal-scope declaration -- proposal is a separate track.
    decls = [_decl("d1", "proposal", "P1", "abort_run")]
    result = fs.resolve_failure_scope(decls, target_chain=_chain(project="P1"), project_id="P1")
    assert result["source"] == "default"
    assert result["resolved_scope"] == "item"


# ---------------------------------------------------------------------------
# 3h. Conflicting policies (fail-closed)
# ---------------------------------------------------------------------------

def test_conflicting_same_scope_declarations_resolve_to_more_restrictive():
    decls = [
        _decl("a", "wave", "W1", "continue_unaffected"),
        _decl("b", "wave", "W1", "abort_run"),
    ]
    result = fs.resolve_failure_scope(decls, target_chain=_chain(wave="W1"), project_id="P1")
    assert result["conflict"] is True
    assert result["action"] == "abort_run"
    assert result["conflicting_declaration_ids"] == ["a", "b"]


def test_conflict_resolution_applies_floor_even_off_the_raw_max_candidate():
    # 'x' has the raw-highest action (block_future_claims) but no fail-closed
    # reason. 'y' has a lower raw action (pause_affected) but a fail-closed
    # reason code that floors it to require_human_decision, which OUTRANKS
    # block_future_claims. The floored value must win.
    decls = [
        _decl("x", "wave", "W1", "block_future_claims", reason_code="unspecified"),
        _decl("y", "wave", "W1", "pause_affected", reason_code="verified_security"),
    ]
    result = fs.resolve_failure_scope(decls, target_chain=_chain(wave="W1"), project_id="P1")
    assert result["action"] == "require_human_decision"
    assert result["declaration_id"] == "y"


# ---------------------------------------------------------------------------
# 3i. Stale revisions
# ---------------------------------------------------------------------------

def test_stale_board_revision_declaration_is_ignored():
    decls = [_decl("d1", "item", "I1", "abort_run", board_revision="rev-old")]
    result = fs.resolve_failure_scope(
        decls, target_chain=_chain(), project_id="P1",
        expected_board_revision="rev-new",
    )
    assert result["source"] == "default"
    assert result["stale_declaration_ids"] == ["d1"]


def test_matching_board_revision_declaration_is_honored():
    decls = [_decl("d1", "item", "I1", "abort_run", board_revision="rev-new")]
    result = fs.resolve_failure_scope(
        decls, target_chain=_chain(), project_id="P1",
        expected_board_revision="rev-new",
    )
    assert result["source"] == "declared"
    assert result["action"] == "abort_run"


def test_declaration_without_board_revision_is_never_stale():
    decls = [_decl("d1", "item", "I1", "abort_run")]  # board_revision=None
    result = fs.resolve_failure_scope(
        decls, target_chain=_chain(), project_id="P1",
        expected_board_revision="rev-anything",
    )
    assert result["source"] == "declared"
    assert result["stale_declaration_ids"] == []


# ---------------------------------------------------------------------------
# 3j. Two-project isolation
# ---------------------------------------------------------------------------

def test_cross_project_declaration_never_influences_resolution():
    decls = [_decl("other", "wave", "W1", "abort_run", project_id="P2")]
    result = fs.resolve_failure_scope(decls, target_chain=_chain(wave="W1"), project_id="P1")
    assert result["source"] == "default"
    assert result["cross_project_ignored_count"] == 1


def test_same_scope_key_collision_across_projects_stays_isolated():
    decls = [
        _decl("p1_decl", "wave", "SHARED_NAME", "continue_unaffected", project_id="P1"),
        _decl("p2_decl", "wave", "SHARED_NAME", "abort_run", project_id="P2"),
    ]
    p1_result = fs.resolve_failure_scope(
        decls, target_chain=_chain(wave="SHARED_NAME"), project_id="P1",
    )
    assert p1_result["action"] == "continue_unaffected"
    assert p1_result["conflict"] is False

    p2_result = fs.resolve_failure_scope(
        decls, target_chain=_chain(wave="SHARED_NAME"), project_id="P2",
    )
    assert p2_result["action"] == "abort_run"
    assert p2_result["conflict"] is False


# ---------------------------------------------------------------------------
# 3k. Corrective resume (supersession lineage)
# ---------------------------------------------------------------------------

def test_superseding_declaration_replaces_the_original():
    decls = [
        _decl("original", "wave_run", "R1", "abort_run", reason_code="suspected_systemic"),
        _decl("correction", "wave_run", "R1", "pause_affected",
              reason_code="reviewed_not_systemic", supersedes="original"),
    ]
    result = fs.resolve_failure_scope(decls, target_chain=_chain(wave_run="R1"), project_id="P1")
    assert result["action"] == "pause_affected"
    assert result["declaration_id"] == "correction"
    assert result["superseded_declaration_ids"] == ["original"]
    assert result["conflict"] is False


def test_superseded_declaration_does_not_participate_in_conflict_resolution():
    decls = [
        _decl("original", "wave_run", "R1", "abort_run"),
        _decl("correction", "wave_run", "R1", "continue_unaffected", supersedes="original"),
        _decl("third_party", "wave_run", "R1", "pause_affected"),
    ]
    result = fs.resolve_failure_scope(decls, target_chain=_chain(wave_run="R1"), project_id="P1")
    # Only 'correction' and 'third_party' are active; most restrictive of
    # the two is pause_affected -- 'original' must not resurrect abort_run.
    assert result["conflict"] is True
    assert set(result["conflicting_declaration_ids"]) == {"correction", "third_party"}
    assert result["action"] == "pause_affected"


# ---------------------------------------------------------------------------
# 4. Fail-closed reason codes
# ---------------------------------------------------------------------------

def test_is_fail_closed_reason_matches_the_declared_set():
    for code in fs.FAIL_CLOSED_REASON_CODES:
        assert fs.is_fail_closed_reason(code)
    assert not fs.is_fail_closed_reason("unspecified")
    assert not fs.is_fail_closed_reason(None)


def test_floor_action_raises_but_never_lowers():
    assert fs.floor_action("continue_unaffected", "verified_security") == "require_human_decision"
    assert fs.floor_action("abort_run", "verified_security") == "abort_run"
    assert fs.floor_action("continue_unaffected", "unspecified") == "continue_unaffected"


def test_fail_closed_reason_floors_a_single_declaration_in_resolve():
    decls = [_decl(
        "d1", "item", "I1", "continue_unaffected",
        reason_code="foundational_hypothesis_disproven",
    )]
    result = fs.resolve_failure_scope(decls, target_chain=_chain(), project_id="P1")
    assert result["action"] == "require_human_decision"
    assert result["source"] == "declared"


# ---------------------------------------------------------------------------
# Malformed input handling
# ---------------------------------------------------------------------------

def test_invalid_declarations_are_reported_not_silently_dropped_or_fatal():
    decls = [
        {"id": "bad", "project_id": "P1", "scope": "not_a_scope", "key": "I1",
         "action": "continue_unaffected"},
        _decl("good", "item", "I1", "block_future_claims"),
    ]
    result = fs.resolve_failure_scope(decls, target_chain=_chain(), project_id="P1")
    assert result["action"] == "block_future_claims"
    assert len(result["invalid_declarations"]) == 1
    assert result["invalid_declarations"][0]["declaration"]["id"] == "bad"


def test_empty_target_chain_raises():
    with pytest.raises(fs.FailureScopeError):
        fs.resolve_failure_scope([], target_chain=[], project_id="P1")
