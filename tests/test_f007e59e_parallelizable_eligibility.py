"""f007e59e — stale claim metadata was making get_parallelizable_groups
mis-report genuinely free items as ineligible forever.

CONFIRMED BUG: get_parallelizable_groups' per-item eligibility loop gated on
TWO independent signals — `status not in {"pending", "todo"}` AND a truthy
`claimed_at` — even though `status` alone is the atomically-maintained,
authoritative "is this claimed" signal (claim_sprint_item only ever moves an
item to in_progress inside the same write that stamps claimed_at, and only
ever refuses a re-claim by status, never by inspecting claimed_at
independently). A `status='pending'` item with a stale, leftover `claimed_at`
(e.g. left behind by an administrative reset that didn't clear it) was
therefore permanently invisible to eligible/groups, even though it was
genuinely free to claim — reproduced live for items 68b7bd9a and f1c6dd63.

Root mechanism gap: `patch_sprint_item`'s stuck-claim recovery path
(`update_sprint_item(status='pending', force=true)`, dcbd55a0) transitioned
status back to pending but never cleared `claimed_at`/`actor`, and wrote no
audit record at all — unlike `_reset_stale_claim` (the OTHER reset path,
reached via claim_sprint_item's own inline auto-reconciliation), which did
clear `claimed_at` (but not `actor`) and did write an audit record.

FIX (three independent, non-conflicting parts):
  1. get_parallelizable_groups' eligibility loop no longer treats
     `claimed_at` as a second, independent eligibility gate — status is
     authoritative. The same anti-pattern in the function's `running`
     diagnostic list (status in {pending,todo} AND claimed_at truthy) is
     fixed identically, since it produced the exact same self-contradictory
     report live: both flagged items showed up in `running` while
     simultaneously missing from `groups`.
  2. patch_sprint_item's administrative reset path (in_progress -> pending/
     todo/indeterminate) now atomically clears claimed_at AND actor and
     writes a RECONCILE_STALE_CLAIM_AUDIT_EVENT record (capturing prior_actor/
     prior_claimed_at) BEFORE nulling them — mirroring (and closing a small
     pre-existing gap in) _reset_stale_claim's own contract, which is also
     fixed here to clear `actor` in addition to `claimed_at`.
  3. A new clear_stale_claim_metadata(db, project_id, item_id) repairs an
     item that is ALREADY sitting pending/todo/indeterminate with leftover
     claim metadata — the live shape of 68b7bd9a/f1c6dd63 today, where
     re-issuing status='pending' is a no-op (status doesn't change) so part
     (2)'s auto-clear never gets a chance to fire on them.

This file tests all three parts plus the invariant that must NOT change: a
genuinely live-claimed item (status=in_progress, real live owning session)
stays excluded. `test_production_repro_68b7bd9a_f1c6dd63_shape` reproduces
the exact multi-item data shape confirmed via a READ-ONLY
get_sprint_items/get_parallelizable_groups check against Meridian's own live
hosted project on 2026-08-29 (never against production itself — this test
only touches the disposable per-test `db` fixture).
"""
from __future__ import annotations

import json

import pytest

from meridian import db as db_module
from meridian import server as srv
import meridian.db.sprint_items as _sprint_items_mod


def _group_ids(result: dict) -> set[str]:
    return {it["id"] for grp in result["groups"] for it in grp}


def _eligible_ids(result: dict) -> set[str]:
    # Every eligible item ends up in `groups` one way or another — coloring
    # places declared-resource items into conflict-free groups, and each
    # undeclared item gets its own singleton group — so `groups` alone is a
    # complete membership check for "did this pass the eligibility gate."
    return _group_ids(result)


async def _archive_session(db, session_id: str) -> None:
    await db.execute("UPDATE sessions SET status = 'archived' WHERE id = ?", (session_id,))
    await db.commit()


# ---------------------------------------------------------------------------
# (a) / core repro — a pending item with stale claim metadata is eligible.
# ---------------------------------------------------------------------------


async def test_pending_item_with_stale_claimed_at_is_eligible(db):
    """Direct repro of the reported bug shape, constructed without going
    through any reset path at all: status='pending' but claimed_at/actor are
    stale leftovers from a since-abandoned claim. This isolates fix half (1)
    — get_parallelizable_groups must not resurrect a second gate on
    claimed_at once status already says the item is free."""
    p = await db_module.create_project(db, "f007e59e-stale-eligibility")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "stuck stale item")
    dead = await db_module.register_session(db, p["id"], "archived-owner")
    claimed = await db_module.claim_sprint_item(db, p["id"], item["id"], actor=dead["id"])
    assert claimed["status"] == "in_progress"
    await _archive_session(db, dead["id"])

    # Simulate exactly the stale data shape the bug produced: status flipped
    # back to pending, but claimed_at/actor left untouched (bypassing
    # patch_sprint_item entirely so this test targets ONLY the eligibility
    # gate, independent of whether the reset path itself is fixed).
    await db.execute(
        "UPDATE sprint_items SET status = 'pending' WHERE id = ? AND project_id = ?",
        (item["id"], p["id"]),
    )
    await db.commit()

    stale = await db_module.get_sprint_item(db, item["id"])
    assert stale["status"] == "pending"
    assert stale["claimed_at"] is not None
    assert stale["actor"] == dead["id"]

    result = await db_module.get_parallelizable_groups(db, p["id"], version="v1")
    assert item["id"] in _group_ids(result), (
        "a pending item with stale claimed_at must be schedulable — "
        f"groups={result['groups']!r} blocked={result['blocked']!r}"
    )
    assert result["eligible_count"] == 1
    assert item["id"] not in {b["id"] for b in result["blocked"]}
    # f007e59e — also must not simultaneously report as "running": that's the
    # self-contradictory shape confirmed live (2026-08-29, read-only) against
    # production items 68b7bd9a-f3b8-4994-a63d-4cf9fff43424 and
    # f1c6dd63-8c9b-4006-8dcc-3845e3915cd2 — both showed up in `running` while
    # simultaneously missing from `groups`.
    assert item["id"] not in {r["id"] for r in result["running"]}


async def test_production_repro_68b7bd9a_f1c6dd63_shape(db):
    """Exact real-world reproduction, live-verified read-only against Meridian's
    own hosted production project (5787cc92-ba7d-4788-b17c-28ab7938b839) on
    2026-08-29: TWO items, same dead actor (4a8d2c35-0db5-4dcd-9979-9c3a1f814b52),
    status='pending', stall_count=0 (proving the patch_sprint_item path was
    used, not _reset_stale_claim, which would have incremented it), both
    reported 'running' while absent from 'groups'. This test reproduces that
    exact multi-item shape as a synthetic fixture (never touches production)."""
    p = await db_module.create_project(db, "f007e59e-live-repro-shape")
    dead_actor = "4a8d2c35-0db5-4dcd-9979-9c3a1f814b52"
    await db_module.register_session(db, p["id"], "same-dead-owner", human_id=None)
    items = []
    for title, resources in (
        ("audit mcp directory resubmission fix two", ["file:meridian/mcp_tools.py", "file:meridian/server.py"]),
        ("locate github repo tools fix three", ["file:meridian/server.py", "file:tests/test_github.py"]),
    ):
        it = await db_module.add_sprint_item(
            db, p["id"], "current", title, touches_resources=resources,
            force=True,  # b0d42ef6 duplicate-title guard: both are near-duplicates of each
                         # other by word-overlap in spirit; force=True keeps them as two
                         # genuinely distinct items, matching the real live pair.
        )
        await db.execute(
            "UPDATE sprint_items SET status = 'in_progress', claimed_at = datetime('now'), "
            "actor = ? WHERE id = ? AND project_id = ?",
            (dead_actor, it["id"], p["id"]),
        )
        await db.commit()
        # Now reproduce the buggy reset exactly: status flipped to pending,
        # claimed_at/actor left stale, stall_count stays 0 (no _reset_stale_claim
        # ever ran) — the confirmed live signature.
        await db.execute(
            "UPDATE sprint_items SET status = 'pending' WHERE id = ? AND project_id = ?",
            (it["id"], p["id"]),
        )
        await db.commit()
        items.append(await db_module.get_sprint_item(db, it["id"]))

    for it in items:
        assert it["status"] == "pending"
        assert it["claimed_at"] is not None
        assert it["actor"] == dead_actor
        assert (it.get("stall_count") or 0) == 0

    result = await db_module.get_parallelizable_groups(db, p["id"], version="current")
    ids = {it["id"] for it in items}
    assert ids <= _group_ids(result), "both items must now be schedulable"
    assert not (ids & {r["id"] for r in result["running"]}), (
        "neither item should still be misreported as running"
    )


# ---------------------------------------------------------------------------
# (b) — a genuinely live-claimed item must still be excluded.
# ---------------------------------------------------------------------------


async def test_live_owner_in_progress_item_still_excluded(db):
    """Regression guard: the fix must not weaken real ownership protection.
    An item claimed by a session that is NOT closed/archived and has a fresh
    heartbeat must stay excluded from eligible/groups."""
    p = await db_module.create_project(db, "f007e59e-live-owner-excluded")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "genuinely in flight")
    live = await db_module.register_session(db, p["id"], "live-owner")
    claimed = await db_module.claim_sprint_item(db, p["id"], item["id"], actor=live["id"])
    assert claimed["status"] == "in_progress"
    # register_session's documented contract is a fresh, 'active' session row
    # (see its docstring) — no manual tampering with status/last_seen here at
    # all, so this is a genuinely live owner by construction.

    result = await db_module.get_parallelizable_groups(db, p["id"], version="v1")
    assert item["id"] not in _eligible_ids(result)
    assert item["id"] not in {b["id"] for b in result["blocked"]}
    # Surfaced instead as live/in-flight work, per the function's own contract.
    assert any(r["id"] == item["id"] for r in result["running"])

    # And a second actor genuinely cannot steal it via claim_sprint_item either
    # — confirms the live-owner refusal itself (unrelated to this fix) is intact.
    with pytest.raises(ValueError):
        await db_module.claim_sprint_item(db, p["id"], item["id"], actor="someone-else")


# ---------------------------------------------------------------------------
# (c) / (d) — the reset path itself: right fields cleared, history preserved.
# ---------------------------------------------------------------------------


async def test_patch_sprint_item_reset_clears_claim_columns_and_writes_audit(db):
    """The actual mechanism gap this item closes: patch_sprint_item's
    administrative reset (the update_sprint_item(status='pending',
    force=true) stuck-claim recovery path) must atomically clear claimed_at
    AND actor, and must write a durable audit record capturing the prior
    owner BEFORE nulling them — not silently erase the only evidence the item
    was ever claimed."""
    p = await db_module.create_project(db, "f007e59e-reset-path")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "recovered via patch")
    dead = await db_module.register_session(db, p["id"], "dead-owner-patch")
    claimed = await db_module.claim_sprint_item(db, p["id"], item["id"], actor=dead["id"])
    prior_claimed_at = claimed["claimed_at"]
    assert prior_claimed_at is not None
    await _archive_session(db, dead["id"])

    reset = await db_module.patch_sprint_item(
        db, p["id"], item["id"], status="pending", actor="operator-session",
    )
    assert reset["status"] == "pending"
    assert reset["claimed_at"] is None, "stale claimed_at must be cleared, not left stale"
    assert reset["actor"] is None, "stale actor must be cleared, not left stale"

    # History preserved: a real audit row exists recording who held the claim.
    audit_rows = await db_module.get_action_audit_log(
        db, project_id=p["id"], event_type=db_module.RECONCILE_STALE_CLAIM_AUDIT_EVENT,
    )
    assert len(audit_rows) == 1
    row = audit_rows[0]
    assert row["actor"] == "operator-session"
    detail = json.loads(row["detail"])
    assert detail["item_id"] == item["id"]
    assert detail["prior_actor"] == dead["id"]
    assert detail["prior_claimed_at"] == prior_claimed_at
    assert detail["reset_via"] == "patch_sprint_item"

    # End-to-end: the freshly-reset item is now genuinely schedulable.
    result = await db_module.get_parallelizable_groups(db, p["id"], version="v1")
    assert item["id"] in _group_ids(result)


async def test_patch_sprint_item_reset_noop_when_not_previously_claimed(db):
    """No spurious audit record when there was nothing to reset — patching an
    already-pending/todo item to another administrative status must not
    fabricate stale-claim history that never happened."""
    p = await db_module.create_project(db, "f007e59e-reset-noop")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "never claimed")
    await db_module.patch_sprint_item(db, p["id"], item["id"], status="todo")
    await db_module.patch_sprint_item(db, p["id"], item["id"], status="pending")

    audit_rows = await db_module.get_action_audit_log(
        db, project_id=p["id"], event_type=db_module.RECONCILE_STALE_CLAIM_AUDIT_EVENT,
    )
    assert audit_rows == []


async def test_mcp_update_sprint_item_force_true_reset_path_end_to_end(db):
    """The exact documented recovery command from AGENTS.md/dcbd55a0 —
    update_sprint_item(status='pending', force=true) — exercised through the
    real MCP tool surface (handle_update_sprint_item), not the db layer
    directly. Confirms actor is forwarded for audit attribution too."""
    p = await db_module.create_project(db, "f007e59e-mcp-recovery")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "stuck via MCP")
    dead = await db_module.register_session(db, p["id"], "dead-owner-mcp")
    await db_module.claim_sprint_item(db, p["id"], item["id"], actor=dead["id"])
    await _archive_session(db, dead["id"])

    result = await srv._dispatch_mcp_tool(
        "update_sprint_item",
        {
            "project_id": p["id"],
            "item_id": item["id"],
            "status": "pending",
            "force": True,
            "session_id": "rescuer-session",
        },
        db, "/tmp",
    )
    assert result["status"] == "pending"
    assert result["claimed_at"] is None
    assert result["actor"] is None

    audit_rows = await db_module.get_action_audit_log(
        db, project_id=p["id"], event_type=db_module.RECONCILE_STALE_CLAIM_AUDIT_EVENT,
    )
    assert len(audit_rows) == 1
    assert audit_rows[0]["actor"] == "rescuer-session"
    detail = json.loads(audit_rows[0]["detail"])
    assert detail["prior_actor"] == dead["id"]

    groups = await db_module.get_parallelizable_groups(db, p["id"], version="v1")
    assert item["id"] in _group_ids(groups)


# ---------------------------------------------------------------------------
# _reset_stale_claim itself must also clear `actor` now (small pre-existing
# gap the discovery flagged: it already cleared claimed_at, but not actor).
# ---------------------------------------------------------------------------


async def test_reset_stale_claim_clears_actor_too(db):
    p = await db_module.create_project(db, "f007e59e-reset-stale-claim-actor")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "auto-reconciled")
    dead = await db_module.register_session(db, p["id"], "dead-owner-auto")
    await db_module.claim_sprint_item(db, p["id"], item["id"], actor=dead["id"])
    await db.execute("UPDATE sessions SET status = 'closed' WHERE id = ?", (dead["id"],))
    await db.commit()

    fresh = await db_module.get_sprint_item(db, item["id"])
    verdict = await db_module.classify_stale_claim(db, fresh)
    assert verdict["classification"] == "stale"

    reset_result = await _sprint_items_mod._reset_stale_claim(
        db, p["id"], item["id"], verdict, actor="sweeper",
    )
    assert reset_result is not None
    assert reset_result["prior_actor"] == dead["id"]

    reset_item = await db_module.get_sprint_item(db, item["id"])
    assert reset_item["status"] == "pending"
    assert reset_item["claimed_at"] is None
    assert reset_item["actor"] is None

    # History still recorded even though the live column is cleared.
    audit_rows = await db_module.get_action_audit_log(
        db, project_id=p["id"], event_type=db_module.RECONCILE_STALE_CLAIM_AUDIT_EVENT,
    )
    assert len(audit_rows) == 1
    detail = json.loads(audit_rows[0]["detail"])
    assert detail["prior_actor"] == dead["id"]


# ---------------------------------------------------------------------------
# clear_stale_claim_metadata — the standalone repair for an item that is
# ALREADY sitting pending/todo/indeterminate with leftover claim metadata
# (the exact live shape of 68b7bd9a / f1c6dd63, where the recovery path had
# already flipped status before this fix, so patch_sprint_item's new
# in_progress->reset auto-clear never gets a chance to fire on them again —
# re-issuing status='pending' against an already-pending item is a no-op
# transition). This is the exact remediation call an operator would run
# against those two production rows.
# ---------------------------------------------------------------------------


async def test_clear_stale_claim_metadata_repairs_already_pending_item(db):
    """Models the real production shape: item is ALREADY pending (the old,
    buggy patch_sprint_item already ran once, before this fix existed) but
    claimed_at/actor are still stale. clear_stale_claim_metadata must clear
    them, preserve history via an audit record, and the item must then be
    genuinely schedulable."""
    p = await db_module.create_project(db, "f007e59e-clear-stale-repair")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "already reset, still stale")
    dead = await db_module.register_session(db, p["id"], "dead-owner-already-reset")
    claimed = await db_module.claim_sprint_item(db, p["id"], item["id"], actor=dead["id"])
    prior_claimed_at = claimed["claimed_at"]
    await _archive_session(db, dead["id"])

    # Simulate the OLD (pre-fix) patch_sprint_item behaviour directly: status
    # flipped back to pending, claimed_at/actor left stale, no audit written.
    # This is the production data shape for 68b7bd9a / f1c6dd63 today.
    await db.execute(
        "UPDATE sprint_items SET status = 'pending' WHERE id = ? AND project_id = ?",
        (item["id"], p["id"]),
    )
    await db.commit()
    already_stuck = await db_module.get_sprint_item(db, item["id"])
    assert already_stuck["status"] == "pending"
    assert already_stuck["claimed_at"] is not None
    assert already_stuck["actor"] == dead["id"]

    # Re-issuing the documented recovery command is a no-op on this item now
    # (status doesn't change: already pending) — confirms clear_stale_claim_metadata
    # really is the correct, distinct remediation call for this exact case.
    noop = await db_module.patch_sprint_item(db, p["id"], item["id"], status="pending")
    assert noop["claimed_at"] is not None, "sanity: re-patching an already-pending item does not clear anything"

    repaired = await db_module.clear_stale_claim_metadata(
        db, p["id"], item["id"], actor="operator-cleanup",
    )
    assert repaired["status"] == "pending"
    assert repaired["claimed_at"] is None
    assert repaired["actor"] is None

    audit_rows = await db_module.get_action_audit_log(
        db, project_id=p["id"], event_type=db_module.RECONCILE_STALE_CLAIM_AUDIT_EVENT,
    )
    assert len(audit_rows) == 1
    assert audit_rows[0]["actor"] == "operator-cleanup"
    detail = json.loads(audit_rows[0]["detail"])
    assert detail["item_id"] == item["id"]
    assert detail["prior_actor"] == dead["id"]
    assert detail["prior_claimed_at"] == prior_claimed_at
    assert detail["reset_via"] == "clear_stale_claim_metadata"

    groups = await db_module.get_parallelizable_groups(db, p["id"], version="v1")
    assert item["id"] in _group_ids(groups)


async def test_clear_stale_claim_metadata_refuses_in_progress_item(db):
    """Safety rail: must never rip live claim metadata out from under a claim
    that might still be active — an in_progress item must go through the
    liveness-checked claim_sprint_item/_reset_stale_claim path instead."""
    p = await db_module.create_project(db, "f007e59e-clear-stale-refuses-live")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "still in progress")
    owner = await db_module.register_session(db, p["id"], "live-owner-clear-guard")
    await db_module.claim_sprint_item(db, p["id"], item["id"], actor=owner["id"])

    with pytest.raises(ValueError, match="in_progress"):
        await db_module.clear_stale_claim_metadata(db, p["id"], item["id"])

    unchanged = await db_module.get_sprint_item(db, item["id"])
    assert unchanged["status"] == "in_progress"
    assert unchanged["actor"] == owner["id"]
    assert unchanged["claimed_at"] is not None


async def test_clear_stale_claim_metadata_noop_when_already_clean(db):
    """Idempotent, auditless no-op when there's nothing stale to clear —
    calling this defensively must never fabricate history that never
    happened."""
    p = await db_module.create_project(db, "f007e59e-clear-stale-noop")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "never claimed at all")

    result = await db_module.clear_stale_claim_metadata(db, p["id"], item["id"])
    assert result["id"] == item["id"]
    assert result["claimed_at"] is None
    assert result["actor"] is None

    audit_rows = await db_module.get_action_audit_log(
        db, project_id=p["id"], event_type=db_module.RECONCILE_STALE_CLAIM_AUDIT_EVENT,
    )
    assert audit_rows == []
