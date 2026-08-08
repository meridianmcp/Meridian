"""268d4e9b -- adversarial coordination matrix: regression coverage for the
cross-cutting parallel-execution safety boundaries that have repeatedly
failed in live Meridian runs (symbol/file locks, stale-claim reconciliation,
dependency-frontier replanning, pointer/evidence gating, owned-process
cleanup).

This module is regression coverage ONLY (per this sprint item's scope) --
no production code is changed here. Every scenario below was chosen after
reading the existing coverage in tests/test_resource_locks.py,
tests/test_pointers.py, tests/test_claim_concurrency.py,
tests/test_process_lifecycle.py and tests/test_tunnel_client.py to avoid
duplicating what those files already assert; see this sprint item's session
report for the itemized "new vs already-covered" breakdown.

Sections:
  1. Typed/legacy resource-lock scope: file_locks/file_symbol_claims are
     NOT project-scoped -- documents a real cross-project collision surface
     (reported as a finding, not fixed here -- out of scope for this item).
  2. Dependency-frontier replan: get_parallelizable_groups() re-queried
     across a multi-level chain and a fast/slow sibling-completion race.
  3. Stale-claim atomic reset: releasing an ``inferred:``-marked resource,
     and asserting the audit trail records every released resource, not
     just that *a* reset happened.
  4. touches_resources (including auto-inferred guesses) is a SCHEDULING
     declaration, never itself COMPLETION EVIDENCE -- the explicit
     "never satisfied merely because inferred touches_resources exists"
     assertion called out by name in this item's notes.
"""
from __future__ import annotations

import json

from meridian import db as db_module
from meridian import server as srv

_PY_SRC = (
    "class Widget:\n"      # 1
    "    def render(self):\n"  # 2
    "        return 1\n"       # 3
)


# ---------------------------------------------------------------------------
# 1. Typed symbol claims vs legacy keys -- cross-project lock scope
# ---------------------------------------------------------------------------
#
# file_locks.file_path and file_symbol_claims.file_path carry NO project_id
# column (see meridian/db/__init__.py's CREATE TABLE literals -- file_locks
# even declares file_path UNIQUE, globally). claim_file()/claim_symbol()'s
# own SQL (meridian/db/locks.py) filters ONLY on file_path (+ session_id to
# exclude the caller's own claims) -- never on the claiming session's
# project. Two UNRELATED projects whose sessions happen to claim the
# identical relative path (extremely plausible: "README.md", "src/index.ts",
# "meridian/server.py" if a user runs two logical Meridian projects against
# the same repo) collide with EACH OTHER today. These two tests document
# that CURRENT behavior precisely so it cannot silently change (in either
# direction) without a test noticing -- this is a reported finding, not a
# fix (fixes are out of scope for this TEST-ONLY sprint item).
# ---------------------------------------------------------------------------


async def test_file_lock_not_scoped_by_project_cross_project_collision(db):
    pa = await db_module.create_project(db, "268d4e9b-file-cross-a")
    pb = await db_module.create_project(db, "268d4e9b-file-cross-b")
    sa = await db_module.register_session(db, pa["id"], "sess-a")
    sb = await db_module.register_session(db, pb["id"], "sess-b")
    # Identical relative path in two otherwise-unrelated projects.
    path = "src/shared_module_name.py"

    ra = await db_module.claim_file(db, path, sa["id"], mode="write")
    assert ra["claimed"] is True

    # CURRENT behavior: project B's session is blocked by project A's lock on
    # the identical path string, even though the two projects are unrelated.
    rb = await db_module.claim_file(db, path, sb["id"], mode="write")
    assert rb["claimed"] is False
    assert rb["holder_session_id"] == sa["id"]

    # Releasing A's claim frees it for B -- confirms this is a real shared
    # global row, not a coincidental error shape.
    assert await db_module.release_file(db, path, sa["id"]) is True
    rb2 = await db_module.claim_file(db, path, sb["id"], mode="write")
    assert rb2["claimed"] is True


async def test_symbol_claim_not_scoped_by_project_cross_project_collision(db):
    pa = await db_module.create_project(db, "268d4e9b-symbol-cross-a")
    pb = await db_module.create_project(db, "268d4e9b-symbol-cross-b")
    sa = await db_module.register_session(db, pa["id"], "sess-a")
    sb = await db_module.register_session(db, pb["id"], "sess-b")
    path = "src/widget.py"

    ra = await db_module.claim_symbol(db, sa["id"], path, "Widget.render", _PY_SRC)
    assert ra["claimed"] is True

    # CURRENT behavior: project B's session sees a symbol_conflict against
    # project A's claim on the identical path, despite being unrelated
    # projects -- file_symbol_claims carries no project_id column either.
    rb = await db_module.claim_symbol(db, sb["id"], path, "Widget.render", _PY_SRC)
    assert rb["claimed"] is False
    assert rb["reason"] == "symbol_conflict"
    assert rb["conflicts"][0]["holder_session_id"] == sa["id"]


# ---------------------------------------------------------------------------
# 2. Dependency-frontier replan after fast/slow item completion
# ---------------------------------------------------------------------------
#
# get_parallelizable_groups' existing coverage in test_resource_locks.py
# checks a SINGLE parent/child pair, once, before and after the parent
# completes. Neither a multi-level chain (does the frontier advance
# ONE level at a time, not all-at-once?) nor a "one sibling finishes fast
# while another sibling in the SAME eligible group is still running" replan
# is covered anywhere -- both are exactly the shape of scheduling bug that
# would only show up when an orchestrator polls get_parallelizable_groups
# repeatedly across a real multi-item execution, which is what these two
# tests simulate.
# ---------------------------------------------------------------------------


async def test_dependency_frontier_replan_advances_one_level_at_a_time(db):
    p = await db_module.create_project(db, "268d4e9b-frontier-chain")
    a = await db_module.add_sprint_item(
        db, p["id"], "v1", "chain a", touches_resources=["file:chain_a.py"],
    )
    b = await db_module.add_sprint_item(
        db, p["id"], "v1", "chain b", touches_resources=["file:chain_b.py"],
        depends_on=a["id"],
    )
    c = await db_module.add_sprint_item(
        db, p["id"], "v1", "chain c", touches_resources=["file:chain_c.py"],
        depends_on=b["id"],
    )

    res0 = await db_module.get_parallelizable_groups(db, p["id"], version="v1")
    eligible0 = {it["id"] for g in res0["groups"] for it in g}
    blocked0 = {x["id"] for x in res0["blocked"]}
    assert eligible0 == {a["id"]}
    assert blocked0 == {b["id"], c["id"]}

    # Complete only the ROOT. The frontier must advance exactly one level:
    # b becomes eligible, c must stay blocked (its own parent b isn't done).
    await db_module.complete_sprint_item(db, p["id"], a["id"])
    res1 = await db_module.get_parallelizable_groups(db, p["id"], version="v1")
    eligible1 = {it["id"] for g in res1["groups"] for it in g}
    blocked1 = {x["id"] for x in res1["blocked"]}
    assert eligible1 == {b["id"]}
    assert blocked1 == {c["id"]}
    assert res1["plan_generation"] != res0["plan_generation"]

    # Complete b -- now c is the only frontier item.
    await db_module.complete_sprint_item(db, p["id"], b["id"])
    res2 = await db_module.get_parallelizable_groups(db, p["id"], version="v1")
    eligible2 = {it["id"] for g in res2["groups"] for it in g}
    assert eligible2 == {c["id"]}
    assert res2["blocked"] == []
    assert res2["plan_generation"] != res1["plan_generation"]


async def test_fast_sibling_completion_unblocks_dependent_while_slow_sibling_still_running(db):
    """Two disjoint-resource siblings land in the same parallel-safe group;
    one (the 'dependent's parent) finishes fast while the other is still
    claimed and running ('slow'). A replan must surface the dependent as
    newly eligible while the still-running sibling stays excluded (visible
    only under 'running'), never silently dropped or double-counted."""
    p = await db_module.create_project(db, "268d4e9b-frontier-fastslow")
    # prospect_bypass=True on every item: this test is about scheduling/
    # replan mechanics, not the UNPROSPECTED evidence gate (covered
    # separately in section 4 below) -- claim_sprint_item enforces that gate
    # even at the raw db layer, so without the bypass neither claim below
    # would actually transition to in_progress.
    slow = await db_module.add_sprint_item(
        db, p["id"], "v1", "slow sibling", touches_resources=["file:slow.py"],
        prospect_bypass=True,
    )
    fast = await db_module.add_sprint_item(
        db, p["id"], "v1", "fast sibling", touches_resources=["file:fast.py"],
        prospect_bypass=True,
    )
    dependent = await db_module.add_sprint_item(
        db, p["id"], "v1", "dependent on fast", touches_resources=["file:dep.py"],
        depends_on=fast["id"], prospect_bypass=True,
    )

    res0 = await db_module.get_parallelizable_groups(db, p["id"], version="v1")
    eligible0 = {it["id"] for g in res0["groups"] for it in g}
    assert eligible0 == {slow["id"], fast["id"]}
    assert {x["id"] for x in res0["blocked"]} == {dependent["id"]}

    slow_owner = await db_module.register_session(db, p["id"], "slow-owner")
    fast_owner = await db_module.register_session(db, p["id"], "fast-owner")

    # slow claims and holds its file — still "running" throughout.
    assert (await db_module.claim_file(db, "slow.py", slow_owner["id"]))["claimed"] is True
    slow_claim = await db_module.claim_sprint_item(db, p["id"], slow["id"], actor=slow_owner["id"])
    assert slow_claim["status"] == "in_progress"

    # fast claims, does its work, and completes — releasing nothing declared
    # here matters for `dependent` since they share no resource, only the
    # dependency edge does.
    assert (await db_module.claim_file(db, "fast.py", fast_owner["id"]))["claimed"] is True
    fast_claim = await db_module.claim_sprint_item(db, p["id"], fast["id"], actor=fast_owner["id"])
    assert fast_claim["status"] == "in_progress"
    fast_done = await db_module.complete_sprint_item(db, p["id"], fast["id"], actor=fast_owner["id"])
    assert fast_done["status"] == "done"

    res1 = await db_module.get_parallelizable_groups(db, p["id"], version="v1")
    eligible1 = {it["id"] for g in res1["groups"] for it in g}
    running1 = {x["id"] for x in res1["running"]}
    assert dependent["id"] in eligible1
    assert slow["id"] not in eligible1  # still claimed, excluded from fresh groups
    assert slow["id"] in running1        # but visible as in-flight, not vanished
    assert res1["blocked"] == []
    assert fast["id"] not in running1 and fast["id"] not in eligible1  # done, gone from both


# ---------------------------------------------------------------------------
# 3. Stale-claim atomic reset -- inferred-resource release + audit content
# ---------------------------------------------------------------------------


async def test_reset_stale_claim_releases_inferred_prefixed_resource(db):
    """A resource stored with the `inferred:` provenance marker (07bdfdbb --
    e.g. auto-populated from a title-keyword guess, see section 4 below)
    must still be released correctly when its claiming session is proven
    dead: parse_touches_resources strips the marker to the canonical id
    before _reset_stale_claim iterates it, so the real underlying file lock
    (stored WITHOUT the marker in file_locks) is the thing that actually
    gets released. A regression here (e.g. a future edit that releases the
    literal `inferred:file:...` string instead of the canonical
    `file:...` id) would silently leave the real lock orphaned forever."""
    p = await db_module.create_project(db, "268d4e9b-inferred-release")
    item = await db_module.add_sprint_item(
        db, p["id"], "v1", "inferred release target",
        touches_resources=["inferred:file:infr_target.py"],
        prospect_bypass=True,
    )
    owner = await db_module.register_session(db, p["id"], "inferred-owner")
    assert (await db_module.claim_file(db, "infr_target.py", owner["id"]))["claimed"] is True
    await db_module.claim_sprint_item(db, p["id"], item["id"], actor=owner["id"])
    await db.execute("UPDATE sessions SET status = 'closed' WHERE id = ?", (owner["id"],))
    await db.commit()

    report = await db_module.reconcile_stale_claims(db, p["id"], dry_run=False, actor="sweeper")
    assert len(report["reset"]) == 1
    # Released as the CANONICAL id (marker stripped) -- not the raw stored
    # "inferred:file:infr_target.py" string.
    assert report["reset"][0]["released_resources"] == ["file:infr_target.py"]

    claims = await db_module.get_file_claims(db, "infr_target.py")
    assert claims["file_lock"] is None

    reset_item = await db_module.get_sprint_item(db, item["id"])
    assert reset_item["status"] == "pending"


async def test_reconcile_recovery_multi_resource_item_audited_and_fully_reclaimable(db):
    """Crash/interruption/reconnect recovery, end to end, with TWO declared
    resources (not the single-resource shape the existing
    test_reconcile_stale_claims_live_run_resets_and_releases_locks covers):
    a session claims two files under one sprint item, then 'crashes'
    (session explicitly closed, mirroring a killed executor process).
    reconcile_stale_claims must release BOTH locks atomically, write ONE
    audit row whose detail actually enumerates both released resources (not
    merely that a reset happened), and leave the item cleanly reclaimable
    and completable by a fresh session -- no orphaned lock, no half-reset
    state."""
    p = await db_module.create_project(db, "268d4e9b-multi-resource-recovery")
    item = await db_module.add_sprint_item(
        db, p["id"], "v1", "multi resource crash recovery",
        touches_resources=["file:crash_a.py", "file:crash_b.py"],
        prospect_bypass=True,
    )
    owner = await db_module.register_session(db, p["id"], "crash-owner")
    assert (await db_module.claim_file(db, "crash_a.py", owner["id"]))["claimed"] is True
    assert (await db_module.claim_file(db, "crash_b.py", owner["id"]))["claimed"] is True
    await db_module.claim_sprint_item(db, p["id"], item["id"], actor=owner["id"])

    # Simulate a crashed/killed executor: session explicitly closed.
    await db.execute("UPDATE sessions SET status = 'closed' WHERE id = ?", (owner["id"],))
    await db.commit()

    report = await db_module.reconcile_stale_claims(db, p["id"], dry_run=False, actor="sweeper")
    assert len(report["reset"]) == 1
    released = set(report["reset"][0]["released_resources"])
    assert released == {"file:crash_a.py", "file:crash_b.py"}

    # Both underlying locks are actually gone, not just reported as released.
    assert (await db_module.get_file_claims(db, "crash_a.py"))["file_lock"] is None
    assert (await db_module.get_file_claims(db, "crash_b.py"))["file_lock"] is None

    # Audit evidence: exactly one row, and its detail JSON enumerates BOTH
    # released resources -- not just a bare "something was reset" marker.
    audit_rows = await db_module.get_action_audit_log(
        db, project_id=p["id"], event_type=db_module.RECONCILE_STALE_CLAIM_AUDIT_EVENT,
    )
    assert len(audit_rows) == 1
    detail = json.loads(audit_rows[0]["detail"])
    assert set(detail["released_resources"]) == {"file:crash_a.py", "file:crash_b.py"}
    assert detail["item_id"] == item["id"]
    assert detail["prior_actor"] == owner["id"]
    assert detail["classification"] == "stale"

    # Full recovery: a brand-new session can reclaim both resources and the
    # item, and complete it -- nothing about the reset left a broken state.
    rescuer = await db_module.register_session(db, p["id"], "rescuer")
    assert (await db_module.claim_file(db, "crash_a.py", rescuer["id"]))["claimed"] is True
    assert (await db_module.claim_file(db, "crash_b.py", rescuer["id"]))["claimed"] is True
    reclaimed = await db_module.claim_sprint_item(db, p["id"], item["id"], actor=rescuer["id"])
    assert reclaimed["status"] == "in_progress"
    done = await db_module.complete_sprint_item(db, p["id"], item["id"], actor=rescuer["id"])
    assert done["status"] == "done"


# ---------------------------------------------------------------------------
# 4. touches_resources (incl. auto-inferred guesses) is never itself evidence
# ---------------------------------------------------------------------------
#
# Explicit ask from this sprint item's notes: "Assert that a pointer or
# claim is never considered satisfied merely because inferred
# touches_resources exists." _infer_touches_resources (meridian/mcp/
# handler.py, 07bdfdbb) auto-populates touches_resources from a TITLE
# KEYWORD MATCH alone when a caller supplies none -- a best-effort guess,
# never confirmed by a human or by real code inspection. _item_declares_
# resources() (meridian/db/sprint_items.py) treats an inferred-only
# declaration as a real prospecting candidate (correctly -- it must still
# be gated), but is_item_claim_prospected() must NEVER treat the marker's
# mere PRESENCE as if it were durable pointer evidence. These three tests
# pin that contract at the unit level and then end-to-end through the real
# add_sprint_item -> claim_sprint_item MCP path.
# ---------------------------------------------------------------------------


def test_item_declares_resources_true_for_inferred_only_marker():
    """An inferred-only declaration still counts as 'this item declared
    resources' for gating-applicability purposes -- the marker doesn't
    secretly exempt the item from needing real evidence."""
    item = {"touches_resources": ["inferred:file:x.py"]}
    assert db_module._item_declares_resources(item) is True


def test_is_item_claim_prospected_inferred_marker_alone_never_satisfies():
    """THE explicit assertion this sprint item's notes call out by name: a
    claim is never considered satisfied merely because (inferred)
    touches_resources exists. Presence of the auto-guessed marker with NO
    durable pointer evidence must be refused under both the default and
    strict gate shapes -- the marker is a scheduling hint, not proof of
    completion."""
    item_inferred_only = {"touches_resources": ["inferred:file:x.py"]}
    # No durable pointer evidence recorded anywhere for this item.
    assert db_module.is_item_claim_prospected(
        item_inferred_only, has_pointer_evidence=False,
    ) is False
    assert db_module.is_item_claim_prospected(
        item_inferred_only, has_pointer_evidence=False, strict=True, target_resolved=False,
    ) is False
    # Even under strict=True with an explicit target_resolved=False (a
    # pointer row exists but never resolved) the inferred marker's mere
    # existence still cannot rescue it.
    assert db_module.is_item_claim_prospected(
        item_inferred_only, has_pointer_evidence=True, strict=True, target_resolved=False,
    ) is False
    # Only REAL evidence (has_pointer_evidence=True, non-strict, or
    # strict+resolved) satisfies the gate.
    assert db_module.is_item_claim_prospected(
        item_inferred_only, has_pointer_evidence=True,
    ) is True


async def test_claim_sprint_item_blocks_on_autoinferred_resources_without_pointer_evidence(db):
    """End-to-end: add_sprint_item's real title-keyword auto-inference path
    (no explicit touches_resources supplied) attaches an `inferred:`
    resource, and claim_sprint_item's UNPROSPECTED gate still refuses the
    claim because no durable sprint_item_pointers row backs it -- the
    server-side guess alone never satisfies prospecting."""
    p = await db_module.create_project(db, "268d4e9b-autoinfer-gate")
    added = await srv._dispatch_mcp_tool(
        "add_sprint_item",
        {
            "project_id": p["id"], "version": "v1",
            # "dashboard" is a real _HOTSPOT_RULES keyword (meridian/mcp/
            # handler.py) -> auto-infers touches_resources without the
            # caller ever declaring any.
            "title": "Fix a dashboard rendering glitch",
            "force": True,
        },
        db, "/tmp",
    )
    touches = added.get("touches_resources")
    if isinstance(touches, str):
        touches = json.loads(touches)
    assert touches and touches[0].startswith("inferred:file:")

    sess = await db_module.register_session(db, p["id"], "autoinfer-claimant")
    result = await srv._dispatch_mcp_tool(
        "claim_sprint_item",
        {"project_id": p["id"], "item_id": added["id"], "session_id": sess["id"]},
        db, "/tmp",
    )
    assert result.get("blocked") is True
    assert result.get("error") == "UNPROSPECTED"
    reread = await db_module.get_sprint_item(db, added["id"])
    assert reread["status"] == "pending"
