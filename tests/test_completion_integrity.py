"""07229675 — DOCS-R2-C completion-integrity reconciliation: WARN-ONLY
``blocker_kind`` re-check at ``complete_sprint_item`` time.

Scope (see the sprint item's own discovery findings for the full rationale
and the deliberate reduction from its original four-concern title):

Confirmed gap this closes: ``claim_sprint_item`` hard-gates
``blocker_kind in ('superseded', 'systemic_invalidated_run')`` at CLAIM
time (f89d440f/cc3864bd), but nothing re-read ``blocker_kind`` at COMPLETE
time. An item can transition INTO one of those two states AFTER a session
has already claimed it —
``block_sprint_items_for_systemic_invalidation`` explicitly documents that
an already ``in_progress`` item stays ``in_progress`` (never forced to a
new status) when marked invalidated — so a session holding a live claim
could complete the item anyway, unconditionally bypassing the hard gate's
entire purpose. This is now surfaced as a ``blocker_kind_completion_warning``
on the completed item — deliberately WARN-ONLY in this pass (mirrors
``meridian/handoff_receipt.py``'s own precedent: ship warn-only first on the
single hottest completion path in the codebase, defer a fail-closed gate +
override flag to a follow-up).

Explicitly NOT in scope here (see the sprint item's discovery notes):
"stale postconditions" (no existing design/vocabulary) and full three-way
receipt unification (code_intel/handoff/test_run receipts already exist
independently; unifying them is a separate, larger change).
"""
from __future__ import annotations

import pytest

from meridian import db as db_module


@pytest.mark.asyncio
async def test_complete_sprint_item_no_warning_for_ordinary_item(db):
    """Regression guard: an item with no blocker_kind at all sees zero
    behavior change — no new key on the returned dict."""
    p = await db_module.create_project(db, "ci-ordinary")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "ordinary item")
    await db_module.claim_sprint_item(db, p["id"], item["id"])

    result = await db_module.complete_sprint_item(db, p["id"], item["id"])

    assert result["status"] == "done"
    assert "blocker_kind_completion_warning" not in result


@pytest.mark.asyncio
async def test_complete_sprint_item_warns_but_does_not_block_on_superseded(db):
    """The confirmed gap: an item claimed BEFORE it was marked superseded can
    still be completed (never a hard block in this pass), but now carries a
    visible warning explaining why that might be wrong."""
    p = await db_module.create_project(db, "ci-superseded")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "will be superseded")
    await db_module.claim_sprint_item(db, p["id"], item["id"])
    # Simulate the item's premise being superseded AFTER the claim was taken
    # (block_sprint_items_for_systemic_invalidation's own documented
    # behavior: an in_progress item stays in_progress when marked blocked).
    await db_module.patch_sprint_item(db, p["id"], item["id"], blocker_kind="superseded")

    result = await db_module.complete_sprint_item(db, p["id"], item["id"])

    assert result["status"] == "done", "warn-only: completion must NOT be blocked"
    assert result["completion_outcome"] == "committed"
    assert "blocker_kind_completion_warning" in result
    assert "superseded" in result["blocker_kind_completion_warning"]
    assert item["id"] in result["blocker_kind_completion_warning"]


@pytest.mark.asyncio
async def test_complete_sprint_item_warns_but_does_not_block_on_systemic_invalidated_run(db):
    p = await db_module.create_project(db, "ci-systemic")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "will be invalidated")
    await db_module.claim_sprint_item(db, p["id"], item["id"])
    await db_module.patch_sprint_item(
        db, p["id"], item["id"], blocker_kind="systemic_invalidated_run",
    )

    result = await db_module.complete_sprint_item(db, p["id"], item["id"])

    assert result["status"] == "done"
    assert "blocker_kind_completion_warning" in result
    assert "systemic_invalidated_run" in result["blocker_kind_completion_warning"]


@pytest.mark.asyncio
async def test_complete_sprint_item_manual_blocker_kind_is_not_hard_blocked(db):
    """'manual' is a SOFT, listing-only exclusion (f89d440f) -- distinct from
    the two HARD-gated values. It must never trigger this warning, matching
    claim_sprint_item's own gate, which only hard-blocks 'superseded' and
    'systemic_invalidated_run'."""
    p = await db_module.create_project(db, "ci-manual")
    item = await db_module.add_sprint_item(
        db, p["id"], "v1", "manual item", blocker_kind="manual", force=True,
    )
    await db_module.claim_sprint_item(db, p["id"], item["id"])

    result = await db_module.complete_sprint_item(db, p["id"], item["id"])

    assert result["status"] == "done"
    assert "blocker_kind_completion_warning" not in result


@pytest.mark.asyncio
async def test_idempotent_replay_never_re_evaluates_blocker_kind_gate(db):
    """Critical regression guard (dcf78192/a2a027cf contract): the idempotent
    already_committed short-circuit must stay a total no-gates no-op. Complete
    an ordinary item first (no warning), THEN mark the now-DONE item
    superseded, then complete it again -- the replay path must return
    completion_outcome='already_committed' and must NOT retroactively surface
    the warning, because no gate of any kind runs on that path."""
    p = await db_module.create_project(db, "ci-idempotent-replay")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "replay item")
    await db_module.claim_sprint_item(db, p["id"], item["id"])

    first = await db_module.complete_sprint_item(db, p["id"], item["id"])
    assert first["completion_outcome"] == "committed"
    assert "blocker_kind_completion_warning" not in first

    # Mark the now-done item superseded (e.g. a later planning decision) --
    # this must never surface retroactively on a pure idempotent replay.
    await db_module.patch_sprint_item(db, p["id"], item["id"], blocker_kind="superseded")

    second = await db_module.complete_sprint_item(db, p["id"], item["id"])
    assert second["completion_outcome"] == "already_committed"
    assert "blocker_kind_completion_warning" not in second


@pytest.mark.asyncio
async def test_claim_sprint_item_still_hard_blocks_superseded_independently(db):
    """Belt-and-suspenders: confirms claim_sprint_item's PRE-EXISTING hard
    gate (f89d440f/cc3864bd) is untouched by this change -- this item's fix
    is scoped to the COMPLETE-time gap only, never weakening the CLAIM-time
    hard gate."""
    p = await db_module.create_project(db, "ci-claim-still-hard-blocked")
    item = await db_module.add_sprint_item(
        db, p["id"], "v1", "pre-superseded item", blocker_kind="superseded", force=True,
    )

    result = await db_module.claim_sprint_item(db, p["id"], item["id"])

    assert result.get("blocked") is True
    assert result.get("error") == "SUPERSEDED"
