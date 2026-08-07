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


# ---------------------------------------------------------------------------
# 56e9b3c7 — classify_stale_claim: the richer, multi-signal classifier behind
# autonomous stale-claim reconciliation. Reuses the SAME session-liveness
# signals (heartbeat, explicit close, unrecognised-actor "can't tell")
# already proven above for complete_sprint_item's claim-ownership gate, so
# these tests deliberately mirror the scenarios above one-for-one — the
# CLAIM-time classifier must never contradict the already-shipped
# COMPLETION-time precedent it explicitly documents itself as mirroring.
#
# SAFETY: every test below drives ONLY the ephemeral, disposable `db` test
# fixture (a fresh in-memory SQLite DB with synthetic projects/items/sessions
# created inline) — never any real Meridian project. No test in this file
# ever passes dry_run=False against anything but that throwaway fixture.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_classify_live_heartbeat_is_active_even_when_claimed_at_is_ancient(db):
    """A confirmed-alive session (recent heartbeat, not closed/archived) is
    ALWAYS 'active' regardless of claimed_at age — a 70-hour-old claim under
    a still-heartbeating session is genuine long-running work, not
    abandonment. This is the core safety property the sprint item exists to
    preserve."""
    p = await db_module.create_project(db, "classify-live-heartbeat")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "long runner")
    owner = await db_module.register_session(db, p["id"], "long-running-owner")
    await db_module.claim_sprint_item(db, p["id"], item["id"], actor=owner["id"])
    await db.execute(
        "UPDATE sprint_items SET claimed_at = ? WHERE id = ?",
        (_past_ts(70), item["id"]),
    )
    await db.commit()

    fresh_item = await db_module.get_sprint_item(db, item["id"])
    verdict = await db_module.classify_stale_claim(db, fresh_item)
    assert verdict["classification"] == "active"
    assert verdict["signals"]["session_verified_alive"] is True


@pytest.mark.asyncio
async def test_classify_no_actor_is_ambiguous_never_stale(db):
    """No actor recorded at all -> nothing to verify liveness against ->
    'ambiguous', never 'stale'. Resetting blind is the one thing this
    classifier must never do."""
    p = await db_module.create_project(db, "classify-no-actor")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "no actor")
    await db_module.claim_sprint_item(db, p["id"], item["id"])  # no actor arg
    await db.execute(
        "UPDATE sprint_items SET claimed_at = ? WHERE id = ?",
        (_past_ts(10), item["id"]),
    )
    await db.commit()

    fresh_item = await db_module.get_sprint_item(db, item["id"])
    verdict = await db_module.classify_stale_claim(db, fresh_item)
    assert verdict["classification"] == "ambiguous"
    assert "no actor recorded" in verdict["reasons"][0]


@pytest.mark.asyncio
async def test_classify_explicitly_closed_session_is_stale_unconditionally(db):
    """Mirrors 8693b6a8's own precedent: an explicitly closed/archived
    session is unconditional proof of death, even with a FRESH claimed_at —
    no age or corroborating signal required."""
    p = await db_module.create_project(db, "classify-closed-session")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "closed owner")
    owner = await db_module.register_session(db, p["id"], "closes-abruptly")
    await db_module.claim_sprint_item(db, p["id"], item["id"], actor=owner["id"])
    await db.execute("UPDATE sessions SET status = 'closed' WHERE id = ?", (owner["id"],))
    await db.commit()

    fresh_item = await db_module.get_sprint_item(db, item["id"])
    verdict = await db_module.classify_stale_claim(db, fresh_item)
    assert verdict["classification"] == "stale"
    assert verdict["signals"]["age_stale"] is False  # fresh claim — age was NOT the signal


@pytest.mark.asyncio
async def test_classify_unrecognised_actor_requires_two_corroborators(db):
    """An unrecognised actor (per 8693b6a8's own 'can't tell != proof of
    death' precedent) needs claimed_at age STALE *plus* a second independent
    corroborator (here: zero task_log evidence) before landing on 'stale'.
    Age alone, with genuine recent evidence on file, must stay 'ambiguous'."""
    p = await db_module.create_project(db, "classify-unknown-actor")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "human claimed")
    await db_module.claim_sprint_item(db, p["id"], item["id"], actor="adam")
    await db.execute(
        "UPDATE sprint_items SET claimed_at = ? WHERE id = ?",
        (_past_ts(5), item["id"]),
    )
    await db.commit()

    # No task_log evidence at all -> two corroborators (age + no evidence) -> stale.
    stale_item = await db_module.get_sprint_item(db, item["id"])
    stale_verdict = await db_module.classify_stale_claim(db, stale_item)
    assert stale_verdict["classification"] == "stale"

    # Now add genuine evidence AFTER the claim: only one corroborator (age
    # alone) remains -> must stay ambiguous, not stale.
    logger_session = await db_module.register_session(db, p["id"], "evidence-logger")
    await db_module.log_task(
        db, logger_session["id"], p["id"], "made real progress on this",
        sprint_item_id=item["id"],
    )
    evidenced_item = await db_module.get_sprint_item(db, item["id"])
    ambiguous_verdict = await db_module.classify_stale_claim(db, evidenced_item)
    assert ambiguous_verdict["classification"] == "ambiguous"
    assert ambiguous_verdict["signals"]["recent_evidence"] is True


@pytest.mark.asyncio
async def test_classify_cold_heartbeat_with_genuine_evidence_stays_ambiguous(db):
    """A cold (not explicitly closed) heartbeat alone is a WEAK signal —
    paired with genuine recent task_log evidence and a fresh claim, it must
    stay 'ambiguous', never auto-reset. Matches 'require multiple signals...
    when evidence is ambiguous' from the acceptance criteria."""
    p = await db_module.create_project(db, "classify-cold-heartbeat-evidenced")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "cold but working")
    owner = await db_module.register_session(db, p["id"], "cold-heartbeat-owner")
    await db_module.claim_sprint_item(db, p["id"], item["id"], actor=owner["id"])
    # Log the evidence via a SEPARATE session — log_task bumps the LOGGING
    # session's own heartbeat (update_session_seen), so logging as the owner
    # itself would undo the cold-heartbeat setup below. This still counts as
    # genuine recent_evidence, which matches on sprint_item_id regardless of
    # which session logged it.
    logger_session = await db_module.register_session(db, p["id"], "evidence-logger")
    await db_module.log_task(
        db, logger_session["id"], p["id"], "still chugging along", sprint_item_id=item["id"],
    )
    await db.execute(
        "UPDATE sessions SET last_seen = ? WHERE id = ?", (_past_ts(5), owner["id"]),
    )
    await db.commit()

    fresh_item = await db_module.get_sprint_item(db, item["id"])
    verdict = await db_module.classify_stale_claim(db, fresh_item)
    assert verdict["classification"] == "ambiguous"
    assert verdict["signals"]["session_heartbeat_cold"] is True
    assert verdict["signals"]["recent_evidence"] is True


@pytest.mark.asyncio
async def test_classify_cold_heartbeat_plus_one_corroborator_is_stale(db):
    """A cold heartbeat PLUS at least one corroborating signal (here: no
    task_log evidence since the claim) is enough to classify 'stale' even
    though the session was never explicitly closed."""
    p = await db_module.create_project(db, "classify-cold-heartbeat-stale")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "cold and silent")
    owner = await db_module.register_session(db, p["id"], "cold-heartbeat-silent")
    await db_module.claim_sprint_item(db, p["id"], item["id"], actor=owner["id"])
    await db.execute(
        "UPDATE sessions SET last_seen = ? WHERE id = ?", (_past_ts(5), owner["id"]),
    )
    await db.commit()

    fresh_item = await db_module.get_sprint_item(db, item["id"])
    verdict = await db_module.classify_stale_claim(db, fresh_item)
    assert verdict["classification"] == "stale"
    assert verdict["signals"]["session_heartbeat_cold"] is True


@pytest.mark.asyncio
async def test_classify_not_in_progress_is_not_applicable(db):
    p = await db_module.create_project(db, "classify-not-in-progress")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "still pending")
    verdict = await db_module.classify_stale_claim(db, item)
    assert verdict["classification"] == "not_applicable"
