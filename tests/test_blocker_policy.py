"""b108f2e0 — typed blocker triage: configurable quarantine/continue/stop
policy for executor board changes.

Coverage:
1. Pure meridian.blocker_policy: taxonomy, classification priority order,
   fail-closed rules, dependency closure, whole-run evaluation.
2. DB persistence: get/set_project_blocker_policy (project default +
   version-scoped override), auditable via action_audit_log, never breaks
   an unconfigured project.
3. DB-backed evaluate_board_blockers end-to-end against real sprint items.
4. The sprint-item spec's own acceptance tests:
   (1) empty critical item + independent items -> independent items
       continue, empty item is quarantined.
   (2) empty item with a dependent -> only the item + dependency closure
       are held.
   (3) verified security/integrity blocker -> global stop stays fail-closed
       regardless of policy.
   (4) optional tool unavailable -> item-level degrade, not a run stop.
   (5) two-project isolation -> blocker state never leaks across projects.
   (6) resume after pointer repair -> quarantine clears deterministically
       (classification is a pure function of current item state).
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from typing import Any

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

import meridian.db as db_module
from meridian import blocker_policy as bp


def _run(coro: Any) -> Any:
    # asyncio.run() (not get_event_loop().run_until_complete()) — matches
    # this repo's other DB test files (e.g. test_94c26322_prospect_gate.py)
    # so this file is safe to share an xdist worker with them.
    return asyncio.run(coro)


async def _make_db() -> Any:
    tmp = tempfile.mktemp(suffix=".db")
    return await db_module.init_db(tmp)


# ---------------------------------------------------------------------------
# 1. Pure module: taxonomy + classification
# ---------------------------------------------------------------------------

def test_blocker_kinds_taxonomy_has_exactly_eight():
    assert bp.BLOCKER_KINDS == {
        "needs_prospecting", "needs_scope", "optional_tool_unavailable",
        "dependency_blocked", "verified_security", "integrity_corruption",
        "human_action", "run_global_blocker",
    }


def test_fail_closed_kinds_are_exactly_three():
    assert bp.FAIL_CLOSED_KINDS == {
        "verified_security", "integrity_corruption", "run_global_blocker",
    }
    for kind in bp.BLOCKER_KINDS - bp.FAIL_CLOSED_KINDS:
        assert not bp.is_fail_closed(kind)
    for kind in bp.FAIL_CLOSED_KINDS:
        assert bp.is_fail_closed(kind)
    assert not bp.is_fail_closed(None)


def test_normalize_policy_defaults_and_validates():
    assert bp.normalize_policy(None) == bp.DEFAULT_POLICY == "quarantine_continue"
    assert bp.normalize_policy("RUN_STOP") == "run_stop"
    import pytest
    with pytest.raises(bp.BlockerPolicyError):
        bp.normalize_policy("not_a_real_policy")
    with pytest.raises(bp.BlockerPolicyError):
        bp.normalize_policy("")


def test_bare_critical_title_alone_is_not_evidence():
    """The exact incident this module fixes: a CRITICAL-titled item with
    empty notes, no touches_resources, no reproduction must classify as
    needs_scope — priority/title is NEVER, by itself, treated as evidence.
    """
    item = {
        "id": "efea329f", "title": "CRITICAL: tenant isolation breach",
        "priority": "urgent", "notes": "",
    }
    result = bp.classify_item_blocker(item)
    assert result["kind"] == "needs_scope"
    assert result["fail_closed"] is False
    assert result["evidence"]["has_scope_evidence"] is False


def test_item_with_notes_only_is_not_blocked():
    item = {"id": "i1", "title": "fix bug", "notes": "root cause is X, fix is Y"}
    result = bp.classify_item_blocker(item)
    assert result["kind"] is None


def test_item_with_resources_but_unprospected_needs_prospecting():
    item = {
        "id": "i2", "title": "refactor auth",
        "touches_resources": '["file:meridian/auth.py"]',
    }
    result = bp.classify_item_blocker(item, prospected=False)
    assert result["kind"] == "needs_prospecting"
    # Once prospected, the SAME item data clears — idempotent re-classify.
    result2 = bp.classify_item_blocker(item, prospected=True)
    assert result2["kind"] is None


def test_verified_security_and_integrity_require_explicit_signal():
    item = {"id": "i3", "title": "CRITICAL security", "notes": ""}
    # No explicit signal -> just needs_scope, NOT verified_security.
    assert bp.classify_item_blocker(item)["kind"] == "needs_scope"
    # Explicit signal -> verified_security, fail closed.
    result = bp.classify_item_blocker(item, verified_security=True)
    assert result["kind"] == "verified_security"
    assert result["fail_closed"] is True
    result2 = bp.classify_item_blocker(item, integrity_corruption=True)
    assert result2["kind"] == "integrity_corruption"
    assert result2["fail_closed"] is True


def test_optional_tool_unavailable_needs_no_fallback():
    item = {"id": "i4", "title": "use semantic search", "notes": "n/a"}
    # Fallback available -> not blocked.
    result = bp.classify_item_blocker(item, tool_unavailable=True, has_approved_fallback=True)
    assert result["kind"] is None
    # No fallback -> optional_tool_unavailable, NOT fail closed.
    result2 = bp.classify_item_blocker(item, tool_unavailable=True, has_approved_fallback=False)
    assert result2["kind"] == "optional_tool_unavailable"
    assert result2["fail_closed"] is False


def test_human_action_from_explicit_flag_or_milestone_type():
    item = {"id": "i5", "title": "talk to legal", "notes": "n/a"}
    result = bp.classify_item_blocker(item, require_human_review=True)
    assert result["kind"] == "human_action"
    assert result["fail_closed"] is False
    item2 = {"id": "i6", "title": "human task", "notes": "n/a", "milestone_type": "human"}
    assert bp.classify_item_blocker(item2)["kind"] == "human_action"


def test_run_global_blocker_is_fail_closed():
    item = {"id": "i7", "title": "anything", "notes": "n/a"}
    result = bp.classify_item_blocker(item, run_global_blocker=True)
    assert result["kind"] == "run_global_blocker"
    assert result["fail_closed"] is True


def test_classification_priority_order_fail_closed_wins():
    """A fail-closed signal outranks dependency/tool/scope signals even when
    several are simultaneously true.
    """
    item = {"id": "i8", "title": "x", "notes": ""}
    result = bp.classify_item_blocker(
        item, dependency_blocked=True, tool_unavailable=True,
        has_approved_fallback=False, verified_security=True,
    )
    assert result["kind"] == "verified_security"


# ---------------------------------------------------------------------------
# 2. Pure module: dependency closure + whole-run evaluation
# ---------------------------------------------------------------------------

def test_dependent_closure_walks_transitive_chain():
    items = [
        {"id": "a"},
        {"id": "b", "depends_on": "a"},
        {"id": "c", "depends_on": "b"},
        {"id": "d", "depends_on": "z"},  # depends on something NOT blocked
    ]
    closure = bp.compute_dependent_closure(items, ["a"])
    assert closure["a"] == ["b", "c"]


def test_acceptance_1_empty_item_quarantined_independents_continue():
    empty_critical = {
        "id": "efea329f", "title": "CRITICAL tenant isolation", "priority": "urgent",
        "notes": "",
    }
    indep1 = {"id": "ind1", "title": "unrelated fix", "notes": "do X because Y"}
    indep2 = {"id": "ind2", "title": "unrelated fix 2", "touches_resources": '["file:a.py"]'}
    items = [empty_critical, indep1, indep2]
    decision = bp.classify_and_evaluate(items, signals={"ind2": {"prospected": True}})
    assert decision["run_stop"] is False
    assert decision["blocked_item_ids"] == ["efea329f"]
    assert set(decision["eligible_item_ids"]) == {"ind1", "ind2"}
    assert decision["quarantined_item_ids"] == ["efea329f"]


def test_acceptance_2_dependents_of_blocked_item_are_held():
    empty_critical = {"id": "efea329f", "title": "CRITICAL", "notes": ""}
    dependent = {"id": "d1", "title": "depends on broken item", "depends_on": "efea329f", "notes": "n"}
    indep = {"id": "ind1", "title": "unrelated", "notes": "fine"}
    decision = bp.classify_and_evaluate([empty_critical, dependent, indep])
    assert decision["skipped_dependents"]["efea329f"] == ["d1"]
    assert "d1" not in decision["eligible_item_ids"]
    assert "ind1" in decision["eligible_item_ids"]


def test_acceptance_3_verified_security_stays_fail_closed_under_any_policy():
    item = {"id": "sec1", "title": "CRITICAL", "notes": ""}
    other = {"id": "ok1", "title": "fine", "notes": "fine"}
    for policy in bp.VALID_POLICIES:
        decision = bp.classify_and_evaluate(
            [item, other], signals={"sec1": {"verified_security": True}}, policy=policy,
        )
        assert decision["run_stop"] is True, f"policy={policy} must still fail closed"
        assert decision["eligible_item_ids"] == []


def test_acceptance_4_optional_tool_unavailable_degrades_not_run_stop():
    item = {"id": "t1", "title": "use tool", "notes": "n/a"}
    other = {"id": "ok1", "title": "fine", "notes": "fine"}
    decision = bp.classify_and_evaluate(
        [item, other],
        signals={"t1": {"tool_unavailable": True, "has_approved_fallback": False}},
        policy="quarantine_continue",
    )
    assert decision["run_stop"] is False
    assert decision["classifications"]["t1"] == "optional_tool_unavailable"
    assert "ok1" in decision["eligible_item_ids"]


def test_acceptance_6_resume_after_pointer_repair_clears_quarantine():
    empty_critical = {"id": "efea329f", "title": "CRITICAL", "notes": ""}
    before = bp.classify_and_evaluate([empty_critical])
    assert before["blocked_item_ids"] == ["efea329f"]

    fixed = dict(empty_critical)
    fixed["notes"] = "Root cause found; repro steps + fix plan documented."
    after = bp.classify_and_evaluate([fixed])
    assert after["blocked_item_ids"] == []
    assert after["continuation_rationale"] == "no blocked items; full board eligible."


def test_explicit_run_stop_policy_halts_on_any_blocker():
    empty_item = {"id": "e1", "title": "x", "notes": ""}
    other = {"id": "ok1", "title": "fine", "notes": "fine"}
    decision = bp.classify_and_evaluate([empty_item, other], policy="run_stop")
    assert decision["run_stop"] is True
    assert decision["run_stop_reason"] == "explicit_project_run_stop_policy"
    assert decision["eligible_item_ids"] == []


# ---------------------------------------------------------------------------
# 3. DB persistence: get/set_project_blocker_policy
# ---------------------------------------------------------------------------

def test_unconfigured_project_gets_safe_default():
    async def go():
        db = await _make_db()
        proj = await db_module.create_project(db, "p1")
        policy = await db_module.get_project_blocker_policy(db, proj["id"])
        assert policy["policy"] == "quarantine_continue"
        assert policy["source"] == "unset"
    _run(go())


def test_set_project_blocker_policy_persists_and_audits():
    async def go():
        db = await _make_db()
        proj = await db_module.create_project(db, "p2")
        result = await db_module.set_project_blocker_policy(
            db, proj["id"], "run_stop", actor="test-actor",
        )
        assert result["policy"] == "run_stop"
        reread = await db_module.get_project_blocker_policy(db, proj["id"])
        assert reread["policy"] == "run_stop"
        assert reread["source"] == "default"

        audit = await db_module.get_action_audit_log(
            db, project_id=proj["id"], event_type="blocker_policy_set",
        )
        assert len(audit) == 1
        assert audit[0]["actor"] == "test-actor"
    _run(go())


def test_set_project_blocker_policy_does_not_clobber_other_executor_config_keys():
    async def go():
        db = await _make_db()
        proj = await db_module.create_project(db, "p3")
        await db_module.set_executor_config(db, proj["id"], {"test_cmd": "pytest -x"})
        await db_module.set_project_blocker_policy(db, proj["id"], "item_stop")
        cfg = await db_module.get_executor_config(db, proj["id"])
        assert cfg["test_cmd"] == "pytest -x"
        assert cfg["blocker_policy"]["default"] == "item_stop"
    _run(go())


def test_version_scoped_policy_overrides_default():
    async def go():
        db = await _make_db()
        proj = await db_module.create_project(db, "p4")
        await db_module.set_project_blocker_policy(db, proj["id"], "quarantine_continue")
        await db_module.set_project_blocker_policy(db, proj["id"], "run_stop", version="v2")

        default_read = await db_module.get_project_blocker_policy(db, proj["id"])
        assert default_read["policy"] == "quarantine_continue"

        v2_read = await db_module.get_project_blocker_policy(db, proj["id"], version="v2")
        assert v2_read["policy"] == "run_stop"
        assert v2_read["source"] == "version"

        v3_read = await db_module.get_project_blocker_policy(db, proj["id"], version="v3")
        assert v3_read["policy"] == "quarantine_continue"
        assert v3_read["source"] == "default"
    _run(go())


def test_set_project_blocker_policy_rejects_invalid_value():
    async def go():
        db = await _make_db()
        proj = await db_module.create_project(db, "p5")
        import pytest
        with pytest.raises(bp.BlockerPolicyError):
            await db_module.set_project_blocker_policy(db, proj["id"], "not_a_policy")
    _run(go())


# ---------------------------------------------------------------------------
# 4. DB-backed evaluate_board_blockers end-to-end
# ---------------------------------------------------------------------------

def test_evaluate_board_blockers_end_to_end():
    async def go():
        db = await _make_db()
        proj = await db_module.create_project(db, "p6")
        pid = proj["id"]

        empty_item = await db_module.add_sprint_item(
            db, pid, "v1", "CRITICAL tenant isolation", notes="", priority="urgent",
        )
        good1 = await db_module.add_sprint_item(db, pid, "v1", "fix a", notes="clear scope")
        good2 = await db_module.add_sprint_item(db, pid, "v1", "fix b", notes="clear scope 2")

        decision = await db_module.evaluate_board_blockers(db, pid)
        assert decision["run_stop"] is False
        assert decision["blocked_item_ids"] == [empty_item["id"]]
        assert set(decision["eligible_item_ids"]) == {good1["id"], good2["id"]}
        assert decision["policy"] == "quarantine_continue"
        assert decision["policy_source"] == "unset"
    _run(go())


def test_evaluate_board_blockers_run_stop_policy_end_to_end():
    async def go():
        db = await _make_db()
        proj = await db_module.create_project(db, "p7")
        pid = proj["id"]
        await db_module.set_project_blocker_policy(db, pid, "run_stop")
        await db_module.add_sprint_item(db, pid, "v1", "CRITICAL", notes="", priority="urgent")
        await db_module.add_sprint_item(db, pid, "v1", "fine item", notes="clear")

        decision = await db_module.evaluate_board_blockers(db, pid)
        assert decision["run_stop"] is True
        assert decision["eligible_item_ids"] == []
    _run(go())


def test_acceptance_5_two_project_isolation():
    """Blocker state, board_change classification, and policy for one
    project must never leak into another project's evaluation.
    """
    async def go():
        db = await _make_db()
        proj_a = await db_module.create_project(db, "iso-a")
        proj_b = await db_module.create_project(db, "iso-b")

        await db_module.set_project_blocker_policy(db, proj_a["id"], "run_stop")
        # proj_b never configures a policy -> stays on the safe default.
        await db_module.add_sprint_item(db, proj_a["id"], "v1", "CRITICAL a", notes="", priority="urgent")
        await db_module.add_sprint_item(db, proj_b["id"], "v1", "CRITICAL b", notes="", priority="urgent")
        await db_module.add_sprint_item(db, proj_b["id"], "v1", "fine b", notes="clear")

        decision_a = await db_module.evaluate_board_blockers(db, proj_a["id"])
        decision_b = await db_module.evaluate_board_blockers(db, proj_b["id"])

        assert decision_a["policy"] == "run_stop"
        assert decision_a["run_stop"] is True

        assert decision_b["policy"] == "quarantine_continue"
        assert decision_b["run_stop"] is False
        assert len(decision_b["eligible_item_ids"]) == 1

        policy_b = await db_module.get_project_blocker_policy(db, proj_b["id"])
        assert policy_b["policy"] == "quarantine_continue"
        assert policy_b["source"] == "unset"
    _run(go())


def test_acceptance_6_db_backed_resume_after_pointer_repair():
    """DB-backed mirror of the pure-module idempotency test: patching an
    item's notes (simulating a human/executor filling in scope) clears its
    quarantine on the NEXT evaluate_board_blockers call — no separate
    "un-quarantine" mutation needed, since classification is a pure function
    of live item state.
    """
    async def go():
        db = await _make_db()
        proj = await db_module.create_project(db, "p8")
        pid = proj["id"]
        item = await db_module.add_sprint_item(
            db, pid, "v1", "CRITICAL", notes="", priority="urgent",
        )
        before = await db_module.evaluate_board_blockers(db, pid)
        assert before["blocked_item_ids"] == [item["id"]]

        await db_module.patch_sprint_item(
            db, pid, item["id"], notes="Root cause + repro steps now documented.",
        )
        after = await db_module.evaluate_board_blockers(db, pid)
        assert after["blocked_item_ids"] == []
    _run(go())
