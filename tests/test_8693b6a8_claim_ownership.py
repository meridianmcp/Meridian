"""8693b6a8 — claim-ownership verification for complete_sprint_item.

Previously ANY caller could complete ANY in_progress sprint item regardless
of who held its claim (the ``actor`` stamped by claim_sprint_item) — a
structural gap, not a deliberate design. Coverage:

  1. Same-actor completion (or no actor at all) is unaffected — no regression.
  2. A different actor completing a LIVE, non-stale claim is refused
     (SprintItemClaimMismatch); the item stays in_progress.
  3. The refusal is bypassable with an explicit force_foreign_claim=True.
  4. The established "close out a dead session's stale claim" pattern keeps
     working WITHOUT force, via two independent staleness signals:
       a. claimed_at is older than the 2h threshold, even if the claiming
          session row still looks "active" (e.g. it never closed cleanly).
       b. the claiming session's own heartbeat (last_seen) has gone cold,
          even though claimed_at itself is recent.
  5. An unrecognised actor string (no matching session row) is treated as
     "can't tell" rather than proof of death — still requires force.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from meridian import db as db_module


def _past_ts(hours: float) -> str:
    return (datetime.utcnow() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")


@pytest.mark.asyncio
async def test_same_actor_completion_unaffected(db):
    p = await db_module.create_project(db, "ownership-same-actor")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "do the thing")
    await db_module.claim_sprint_item(db, p["id"], item["id"], actor="session-a")

    done = await db_module.complete_sprint_item(
        db, p["id"], item["id"], actor="session-a"
    )
    assert done["status"] == "done"


@pytest.mark.asyncio
async def test_no_actor_supplied_is_fail_open(db):
    p = await db_module.create_project(db, "ownership-no-actor")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "do the thing")
    await db_module.claim_sprint_item(db, p["id"], item["id"], actor="session-a")

    # Completing call supplies no actor identity at all — nothing to compare
    # against, so this must not newly break (matches the fail-open convention
    # used by every other structural gate in this module).
    done = await db_module.complete_sprint_item(db, p["id"], item["id"])
    assert done["status"] == "done"


@pytest.mark.asyncio
async def test_different_actor_live_claim_refused(db):
    p = await db_module.create_project(db, "ownership-mismatch")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "do the thing")
    owner = await db_module.register_session(db, p["id"], "owner-session")
    await db_module.claim_sprint_item(db, p["id"], item["id"], actor=owner["id"])

    with pytest.raises(db_module.SprintItemClaimMismatch):
        await db_module.complete_sprint_item(
            db, p["id"], item["id"], actor="a-different-live-session"
        )

    still = await db_module.get_sprint_item(db, item["id"])
    assert still["status"] == "in_progress"


@pytest.mark.asyncio
async def test_force_foreign_claim_overrides_refusal(db):
    p = await db_module.create_project(db, "ownership-force")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "do the thing")
    owner = await db_module.register_session(db, p["id"], "owner-session")
    await db_module.claim_sprint_item(db, p["id"], item["id"], actor=owner["id"])

    done = await db_module.complete_sprint_item(
        db, p["id"], item["id"],
        actor="a-different-live-session",
        force_foreign_claim=True,
    )
    assert done["status"] == "done"


@pytest.mark.asyncio
async def test_stale_claimed_at_allows_completion_without_force(db):
    p = await db_module.create_project(db, "ownership-stale-claimed-at")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "do the thing")
    owner = await db_module.register_session(db, p["id"], "owner-session")
    await db_module.claim_sprint_item(db, p["id"], item["id"], actor=owner["id"])

    # Backdate claimed_at past the staleness threshold, but leave the owning
    # session row looking otherwise normal/active (the common real-world case:
    # a crashed session that was never cleanly closed).
    await db.execute(
        "UPDATE sprint_items SET claimed_at = ? WHERE id = ?",
        (_past_ts(3), item["id"]),
    )
    await db.commit()

    done = await db_module.complete_sprint_item(
        db, p["id"], item["id"], actor="cleanup-session"
    )
    assert done["status"] == "done"


@pytest.mark.asyncio
async def test_dead_session_allows_completion_without_force(db):
    p = await db_module.create_project(db, "ownership-dead-session")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "do the thing")
    owner = await db_module.register_session(db, p["id"], "owner-session")
    await db_module.claim_sprint_item(db, p["id"], item["id"], actor=owner["id"])

    # claimed_at stays fresh, but the owning session's own heartbeat has gone
    # cold well past the staleness threshold — simulates a session that died
    # without a clean close_session().
    await db.execute(
        "UPDATE sessions SET last_seen = ? WHERE id = ?",
        (_past_ts(5), owner["id"]),
    )
    await db.commit()

    done = await db_module.complete_sprint_item(
        db, p["id"], item["id"], actor="cleanup-session"
    )
    assert done["status"] == "done"


@pytest.mark.asyncio
async def test_closed_session_allows_completion_without_force(db):
    p = await db_module.create_project(db, "ownership-closed-session")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "do the thing")
    owner = await db_module.register_session(db, p["id"], "owner-session")
    await db_module.claim_sprint_item(db, p["id"], item["id"], actor=owner["id"])

    # Explicitly mark the owning session row closed directly (bypassing
    # close_session's own in_progress-requeue side effect, which would move
    # this item back to pending and defeat the scenario under test).
    await db.execute(
        "UPDATE sessions SET status = 'closed' WHERE id = ?", (owner["id"],)
    )
    await db.commit()

    done = await db_module.complete_sprint_item(
        db, p["id"], item["id"], actor="cleanup-session"
    )
    assert done["status"] == "done"


@pytest.mark.asyncio
async def test_unrecognised_actor_string_is_not_treated_as_dead(db):
    p = await db_module.create_project(db, "ownership-unknown-actor")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "do the thing")
    # Claim with an actor string that has no corresponding sessions row at
    # all (e.g. a human name rather than a session id) — nothing to check
    # liveness against, so it must NOT be treated as proof of death.
    await db_module.claim_sprint_item(db, p["id"], item["id"], actor="adam")

    with pytest.raises(db_module.SprintItemClaimMismatch):
        await db_module.complete_sprint_item(
            db, p["id"], item["id"], actor="a-different-session"
        )

    done = await db_module.complete_sprint_item(
        db, p["id"], item["id"],
        actor="a-different-session",
        force_foreign_claim=True,
    )
    assert done["status"] == "done"
