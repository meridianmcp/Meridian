"""e2e1b682 — independent fresh-session verifier gate for sprint completion.

Closes the "hallucinated-compliance completion" gap: a sprint item flagged
``require_verification`` may only be completed once an INDEPENDENT PASS
verdict — filed by a session distinct from the one completing the item — is
on file in ``sprint_item_verifications``. Coverage:

  1. patch_sprint_item can set/clear require_verification.
  2. complete_sprint_item refuses (SprintItemVerificationRequired) when no
     verification is on file yet.
  3. complete_sprint_item refuses when the latest verdict is 'fail'.
  4. complete_sprint_item refuses a same-session self-report (not independent).
  5. complete_sprint_item refuses when no actor= identity is given at all
     (cannot prove independence).
  6. complete_sprint_item succeeds when an independent fresh-session PASS is
     already on file.
  7. complete_sprint_item can file-and-check the verdict in the same call via
     verifier_session_id + verification_verdict.
  8. record_sprint_item_verification / get_latest_sprint_item_verification
     round-trip and validate their inputs.
  9. Items WITHOUT require_verification are completely unaffected (backward
     compatible with the pre-existing evidence-only flow).
 10. count_sprint_items_awaiting_verification (backs the Stop-hook guard's
     advisory verification_pending_count) and the /sprint/pending_count
     endpoint surface the new field without altering exit-code semantics.
"""
from __future__ import annotations

import pytest

from meridian import db as db_module


@pytest.mark.asyncio
async def test_patch_sprint_item_sets_and_clears_require_verification(db):
    p = await db_module.create_project(db, "verif-flag-test")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "needs a fresh check")
    assert not item.get("require_verification")

    flagged = await db_module.patch_sprint_item(
        db, p["id"], item["id"], require_verification=True
    )
    assert flagged["require_verification"] == 1

    cleared = await db_module.patch_sprint_item(
        db, p["id"], item["id"], require_verification=False
    )
    assert cleared["require_verification"] == 0


@pytest.mark.asyncio
async def test_complete_blocked_with_no_verification_on_file(db):
    p = await db_module.create_project(db, "verif-none-test")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "risky change")
    await db_module.patch_sprint_item(db, p["id"], item["id"], require_verification=True)
    await db_module.claim_sprint_item(db, p["id"], item["id"], actor="implementer-session")

    with pytest.raises(db_module.SprintItemVerificationRequired):
        await db_module.complete_sprint_item(
            db, p["id"], item["id"], actor="implementer-session"
        )
    # Never flipped to done.
    still = await db_module.get_sprint_item(db, item["id"])
    assert still["status"] == "in_progress"


@pytest.mark.asyncio
async def test_complete_blocked_on_fail_verdict(db):
    p = await db_module.create_project(db, "verif-fail-test")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "broken fix")
    await db_module.patch_sprint_item(db, p["id"], item["id"], require_verification=True)
    await db_module.claim_sprint_item(db, p["id"], item["id"], actor="implementer-session")
    await db_module.record_sprint_item_verification(
        db, p["id"], item["id"], "verifier-session-1", "fail",
        notes="the referenced function does not exist",
    )

    with pytest.raises(db_module.SprintItemVerificationRequired):
        await db_module.complete_sprint_item(
            db, p["id"], item["id"], actor="implementer-session"
        )


@pytest.mark.asyncio
async def test_complete_blocked_on_same_session_self_report(db):
    p = await db_module.create_project(db, "verif-selfreport-test")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "self-graded work")
    await db_module.patch_sprint_item(db, p["id"], item["id"], require_verification=True)
    await db_module.claim_sprint_item(db, p["id"], item["id"], actor="same-session")
    # The SAME session that did the work files its own PASS — not independent.
    await db_module.record_sprint_item_verification(
        db, p["id"], item["id"], "same-session", "pass",
    )

    with pytest.raises(db_module.SprintItemVerificationRequired):
        await db_module.complete_sprint_item(
            db, p["id"], item["id"], actor="same-session"
        )


@pytest.mark.asyncio
async def test_complete_blocked_without_actor_identity(db):
    p = await db_module.create_project(db, "verif-noactor-test")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "anonymous completion")
    await db_module.patch_sprint_item(db, p["id"], item["id"], require_verification=True)
    await db_module.claim_sprint_item(db, p["id"], item["id"])
    await db_module.record_sprint_item_verification(
        db, p["id"], item["id"], "verifier-session-2", "pass",
    )

    # No actor= passed at all — cannot prove the PASS is independent.
    with pytest.raises(db_module.SprintItemVerificationRequired):
        await db_module.complete_sprint_item(db, p["id"], item["id"])


@pytest.mark.asyncio
async def test_complete_succeeds_with_independent_pass_on_file(db):
    p = await db_module.create_project(db, "verif-pass-test")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "genuinely fixed")
    await db_module.patch_sprint_item(db, p["id"], item["id"], require_verification=True)
    await db_module.claim_sprint_item(db, p["id"], item["id"], actor="implementer-session")
    await db_module.record_sprint_item_verification(
        db, p["id"], item["id"], "fresh-verifier-session", "pass",
        notes="re-read the diff and confirmed the function exists and is called",
    )

    result = await db_module.complete_sprint_item(
        db, p["id"], item["id"], actor="implementer-session"
    )
    assert result["status"] == "done"


@pytest.mark.asyncio
async def test_complete_files_and_checks_verdict_in_same_call(db):
    p = await db_module.create_project(db, "verif-inline-test")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "inline verdict")
    await db_module.patch_sprint_item(db, p["id"], item["id"], require_verification=True)
    await db_module.claim_sprint_item(db, p["id"], item["id"], actor="implementer-session")

    result = await db_module.complete_sprint_item(
        db, p["id"], item["id"],
        actor="implementer-session",
        verifier_session_id="fresh-verifier-session-2",
        verification_verdict="pass",
        verification_notes="independently confirmed via read-only inspection",
    )
    assert result["status"] == "done"

    verification = await db_module.get_latest_sprint_item_verification(
        db, p["id"], item["id"]
    )
    assert verification["verdict"] == "pass"
    assert verification["verifier_session_id"] == "fresh-verifier-session-2"


@pytest.mark.asyncio
async def test_record_and_get_latest_sprint_item_verification(db):
    p = await db_module.create_project(db, "verif-record-test")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "audit trail")

    assert await db_module.get_latest_sprint_item_verification(
        db, p["id"], item["id"]
    ) is None

    await db_module.record_sprint_item_verification(
        db, p["id"], item["id"], "verifier-a", "fail", notes="first attempt broken",
    )
    await db_module.record_sprint_item_verification(
        db, p["id"], item["id"], "verifier-b", "pass", notes="fixed and re-checked",
    )

    latest = await db_module.get_latest_sprint_item_verification(db, p["id"], item["id"])
    assert latest["verdict"] == "pass"
    assert latest["verifier_session_id"] == "verifier-b"

    with pytest.raises(ValueError):
        await db_module.record_sprint_item_verification(
            db, p["id"], item["id"], "verifier-c", "maybe",
        )
    with pytest.raises(ValueError):
        await db_module.record_sprint_item_verification(
            db, p["id"], item["id"], "", "pass",
        )


@pytest.mark.asyncio
async def test_items_without_require_verification_unaffected(db):
    p = await db_module.create_project(db, "verif-unflagged-test")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "ordinary item")
    await db_module.claim_sprint_item(db, p["id"], item["id"], actor="any-session")

    # No require_verification flag, no verification on file — completes exactly
    # like before this feature existed.
    result = await db_module.complete_sprint_item(
        db, p["id"], item["id"], actor="any-session"
    )
    assert result["status"] == "done"


@pytest.mark.asyncio
async def test_require_verification_and_strict_evidence_gates_compose():
    """5fe3502e coexistence check — an item can carry BOTH the pre-existing
    require_verification gate (e2e1b682, this file's subject) and the new
    strict_evidence fail-closed gate (meridian.sprint_evidence_guard) at the
    same time, via the full MCP complete_sprint_item dispatch. Each gate is
    independent: satisfying one does not satisfy the other, and both must be
    satisfied before completion succeeds.
    """
    import meridian.server  # noqa: F401 — import first, avoids handler/server import cycle
    from meridian.mcp import handler as mh

    db_conn = await db_module.init_db(":memory:")
    try:
        p = await mh._dispatch_mcp_tool(
            "create_project", {"name": "verif-plus-strict"}, db_conn, "/tmp"
        )
        item = await mh._dispatch_mcp_tool(
            "add_sprint_item",
            {"project_id": p["id"], "title": "double-gated item", "version": "v1"},
            db_conn, "/tmp",
        )
        await db_module.patch_sprint_item(
            db_conn, p["id"], item["id"],
            require_verification=True, require_strict_evidence=True,
        )
        await db_module.claim_sprint_item(
            db_conn, p["id"], item["id"], actor="implementer-session"
        )

        # Neither gate satisfied yet: completion must be refused by ONE of
        # the two gates (which fires first is an implementation detail —
        # the strict_evidence gate happens to run before require_verification
        # in the current handler ordering, since it is checked in the MCP
        # handler before db.complete_sprint_item is ever called).
        blocked = await mh._dispatch_mcp_tool(
            "complete_sprint_item",
            {"project_id": p["id"], "item_id": item["id"], "actor": "implementer-session"},
            db_conn, "/tmp",
        )
        assert blocked.get("error") in ("STRICT_EVIDENCE_BLOCKED", "VERIFICATION_REQUIRED")

        # Supply real evidence (satisfies strict_evidence) but STILL no
        # independent verification on file — require_verification now blocks.
        still_blocked = await mh._dispatch_mcp_tool(
            "complete_sprint_item",
            {"project_id": p["id"], "item_id": item["id"], "actor": "implementer-session",
             "notes": "fixed the referenced function"},
            db_conn, "/tmp",
        )
        assert still_blocked.get("error") == "VERIFICATION_REQUIRED"

        # Satisfy require_verification too (independent fresh-session PASS).
        # The prior VERIFICATION_REQUIRED attempt raised before persisting
        # anything (db.complete_sprint_item never reaches its write on that
        # path), so notes= must be supplied again here to keep strict_evidence
        # satisfied on this final, successful call.
        await db_module.record_sprint_item_verification(
            db_conn, p["id"], item["id"], "fresh-verifier-session", "pass",
        )
        done = await mh._dispatch_mcp_tool(
            "complete_sprint_item",
            {"project_id": p["id"], "item_id": item["id"], "actor": "implementer-session",
             "notes": "fixed the referenced function and re-verified"},
            db_conn, "/tmp",
        )
        assert done.get("error") is None
        assert done["status"] == "done"
    finally:
        await db_conn.close()


@pytest.mark.asyncio
async def test_count_sprint_items_awaiting_verification(db):
    p = await db_module.create_project(db, "verif-count-test")

    unflagged = await db_module.add_sprint_item(db, p["id"], "v1", "ordinary")
    await db_module.claim_sprint_item(db, p["id"], unflagged["id"], actor="s1")

    unverified = await db_module.add_sprint_item(db, p["id"], "v1", "needs check")
    await db_module.patch_sprint_item(db, p["id"], unverified["id"], require_verification=True)
    await db_module.claim_sprint_item(db, p["id"], unverified["id"], actor="s2")

    verified = await db_module.add_sprint_item(db, p["id"], "v1", "already checked")
    await db_module.patch_sprint_item(db, p["id"], verified["id"], require_verification=True)
    await db_module.claim_sprint_item(db, p["id"], verified["id"], actor="s3")
    await db_module.record_sprint_item_verification(
        db, p["id"], verified["id"], "s4-fresh", "pass"
    )

    n = await db_module.count_sprint_items_awaiting_verification(db, p["id"])
    assert n == 1  # only `unverified`


@pytest.mark.asyncio
async def test_sprint_pending_count_endpoint_surfaces_verification_pending(client):
    db = client.app.state.db
    p = await db_module.create_project(db, "verif-endpoint-test")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "needs a fresh check")
    await db_module.patch_sprint_item(db, p["id"], item["id"], require_verification=True)
    await db_module.claim_sprint_item(db, p["id"], item["id"], actor="s1")

    r = client.get(f"/projects/{p['id']}/sprint/pending_count")
    assert r.status_code == 200, r.text
    body = r.json()
    # An in_progress, unverified item is NOT counted as "pending" (unchanged
    # semantics — only pending/todo block a Stop) but IS surfaced advisorily.
    assert body["pending_count"] == 0
    assert body["verification_pending_count"] == 1
