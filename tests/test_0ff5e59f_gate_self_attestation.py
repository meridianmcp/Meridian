"""0ff5e59f — stop gate self-attestation.

The 2026-09-27 enforcement audit found three completion/unlock gates that the
gated agent could satisfy on its own say-so:

  * ``require_verification``: ``verifier_session_id`` was compared to the
    completer as a plain string, so any made-up id passed as "independent".
  * ``complete_wave_gate``: a hand-typed ``{"status": "ok", "exit_code": 0}``
    unlocked the next wave; nothing tied it to a real ``run_verification`` run.
  * Override flags (``override_ci``, ``force_foreign_claim``,
    ``prospect_bypass`` set by an agent) needed no reason and left no audit
    trail; and an override HITL could be auto-answered.

Coverage (every test below fails against the pre-fix code):

  A. verifier_session_id must be a REAL session of THIS project that is neither
     the completer nor the claim holder (filing time AND on-file time).
  B. complete_wave_gate is bound to a stored run_verification record; a raw
     dict is refused unless carried by a human-approved, audited override whose
     approval HITL is filed require_human=True.
  C. Every override needs a non-empty reason and leaves an action_audit_log row
     (override_ci, force_foreign_claim, prospect_bypass).
"""
from __future__ import annotations

import json

import pytest

from meridian import db as db_module
from meridian import gate_override
from meridian import github_ci
from meridian import server as srv
from meridian.db import sprint_items as si_mod

_GOOD_PAYLOAD = {
    "status": "ok", "exit_code": 0, "passed": 42, "failed": 0,
    "stdout_tail": "42 passed", "stderr_tail": "",
}
_YES = "Yes — approve this override"


async def _project(db, name):
    proj = await srv._dispatch_mcp_tool("create_project", {"name": name}, db, "/tmp")
    return proj["id"]


async def _recorded_run(db, pid, *, status="ok", exit_code=0, complete=True):
    """Persist a run exactly as the run_verification MCP tool does (create, then
    complete from the real synchronous result)."""
    run = await db_module.create_verification_run(db, pid, "pixi run test")
    if not complete:
        return run["id"]
    done = await db_module.complete_verification_run(
        db, run["id"], status=status, exit_code=exit_code, passed=42,
        failed=0 if exit_code == 0 else 3, stdout_tail="42 passed",
    )
    return done["id"]


async def _audit(db, pid, event_type):
    return await db_module.get_action_audit_log(db, project_id=pid, event_type=event_type)


# ---------------------------------------------------------------------------
# A. verifier_session_id must be a real, distinct session of this project
# ---------------------------------------------------------------------------

async def _verified_item(db, name, *, claimer="implementer-session"):
    p = await db_module.create_project(db, name)
    item = await db_module.add_sprint_item(db, p["id"], "v1", "needs independent check")
    await db_module.patch_sprint_item(db, p["id"], item["id"], require_verification=True)
    await db_module.claim_sprint_item(db, p["id"], item["id"], actor=claimer)
    return p, item


async def _verification_rows(db, pid, item_id):
    async with db.execute(
        "SELECT COUNT(*) AS c FROM sprint_item_verifications "
        "WHERE project_id = ? AND sprint_item_id = ?", (pid, item_id),
    ) as cur:
        row = await cur.fetchone()
    return row["c"] if isinstance(row, dict) else row[0]


@pytest.mark.asyncio
async def test_fabricated_verifier_session_id_is_refused_and_not_recorded(db):
    p, item = await _verified_item(db, "fab-verifier")
    with pytest.raises(si_mod.SprintItemVerificationRequired, match="not a registered session"):
        await db_module.complete_sprint_item(
            db, p["id"], item["id"], actor="implementer-session",
            verifier_session_id="verifier-1", verification_verdict="pass",
        )
    assert await _verification_rows(db, p["id"], item["id"]) == 0
    assert (await db_module.get_sprint_item(db, item["id"]))["status"] == "in_progress"


@pytest.mark.asyncio
async def test_verifier_session_from_another_project_is_refused(db):
    p, item = await _verified_item(db, "xproj-verifier")
    other = await db_module.create_project(db, "other-project")
    foreign = await db_module.register_session(db, other["id"], "foreign-verifier")
    with pytest.raises(si_mod.SprintItemVerificationRequired, match="different project"):
        await db_module.complete_sprint_item(
            db, p["id"], item["id"], actor="implementer-session",
            verifier_session_id=foreign["id"], verification_verdict="pass",
        )
    assert await _verification_rows(db, p["id"], item["id"]) == 0


@pytest.mark.asyncio
async def test_verifier_equal_to_completing_session_is_refused_at_filing(db):
    p = await db_module.create_project(db, "self-verifier")
    me = await db_module.register_session(db, p["id"], "implementer")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "self graded")
    await db_module.patch_sprint_item(db, p["id"], item["id"], require_verification=True)
    await db_module.claim_sprint_item(db, p["id"], item["id"], actor=me["id"])
    with pytest.raises(si_mod.SprintItemVerificationRequired, match="not independent"):
        await db_module.complete_sprint_item(
            db, p["id"], item["id"], actor=me["id"],
            verifier_session_id=me["id"], verification_verdict="pass",
        )
    assert await _verification_rows(db, p["id"], item["id"]) == 0


@pytest.mark.asyncio
async def test_verifier_equal_to_claim_holder_is_refused_even_for_another_completer(db):
    # The completing actor may legitimately be an orchestrator id, but the
    # session that HOLDS the claim (the implementer) still cannot verify.
    problem = await si_mod._verifier_session_problem(
        db, "p", "impl-session",
        completing_actor="orchestrator",
        item={"actor": "impl-session", "lock_session_id": "impl-lock"},
    )
    assert problem is not None and "claim" in problem
    problem_lock = await si_mod._verifier_session_problem(
        db, "p", "impl-lock",
        completing_actor="orchestrator",
        item={"actor": "impl-session", "lock_session_id": "impl-lock"},
    )
    assert problem_lock is not None and "claim" in problem_lock


@pytest.mark.asyncio
async def test_stored_pass_from_an_unregistered_session_cannot_complete(db):
    """A row filed through any other path (here: the raw recorder, as legacy
    rows were) is re-checked at completion time — the gate does not trust the
    table just because the row exists."""
    p, item = await _verified_item(db, "stored-fake-pass")
    await db_module.record_sprint_item_verification(
        db, p["id"], item["id"], "made-up-verifier", "pass",
    )
    with pytest.raises(si_mod.SprintItemVerificationRequired, match="not a registered session"):
        await db_module.complete_sprint_item(
            db, p["id"], item["id"], actor="implementer-session",
        )
    assert (await db_module.get_sprint_item(db, item["id"]))["status"] == "in_progress"


@pytest.mark.asyncio
async def test_real_distinct_verifier_session_completes(db):
    p, item = await _verified_item(db, "real-verifier")
    verifier = await db_module.register_session(db, p["id"], "fresh-verifier")
    done = await db_module.complete_sprint_item(
        db, p["id"], item["id"], actor="implementer-session",
        verifier_session_id=verifier["id"], verification_verdict="pass",
    )
    assert done["status"] == "done"
    assert await _verification_rows(db, p["id"], item["id"]) == 1


@pytest.mark.asyncio
async def test_verifier_gate_via_mcp_reports_verification_required(db):
    p, item = await _verified_item(db, "mcp-verifier")
    res = await srv._dispatch_mcp_tool(
        "complete_sprint_item",
        {"project_id": p["id"], "item_id": item["id"], "actor": "implementer-session",
         "verifier_session_id": "verifier-1", "verification_verdict": "pass"},
        db, "/tmp",
    )
    assert res["error"] == "VERIFICATION_REQUIRED"
    assert "not a registered session" in res["message"]


# ---------------------------------------------------------------------------
# B. complete_wave_gate is bound to a stored run_verification record
# ---------------------------------------------------------------------------

async def _gated_project(db, name):
    pid = await _project(db, name)
    await db_module.configure_wave_gate(db, pid, "wave-1", [{"type": "run_verification"}])
    item = await db_module.add_sprint_item(db, pid, "v1", "wave-2 item", force=True)
    await db_module.patch_sprint_item(db, pid, item["id"], wave="wave-2")
    return pid, item


async def _still_blocked(db, pid, item):
    claim = await db_module.claim_sprint_item(db, pid, item["id"])
    return claim.get("error") == "WAVE_GATE_PENDING"


@pytest.mark.asyncio
async def test_handtyped_ok_payload_no_longer_unlocks_the_next_wave(db):
    pid, item = await _gated_project(db, "gate-handtyped")
    with pytest.raises(si_mod.WaveGateUnboundPayload, match="verification_run_id"):
        await db_module.complete_wave_gate(db, pid, "wave-1", _GOOD_PAYLOAD)
    assert await _still_blocked(db, pid, item)
    async with db.execute("SELECT COUNT(*) AS c FROM wave_gate_results WHERE project_id = ?", (pid,)) as cur:
        row = await cur.fetchone()
    assert (row["c"] if isinstance(row, dict) else row[0]) == 0


@pytest.mark.asyncio
async def test_handtyped_payload_refused_through_the_mcp_tool(db):
    pid, item = await _gated_project(db, "gate-handtyped-mcp")
    res = await srv._dispatch_mcp_tool(
        "complete_wave_gate",
        {"project_id": pid, "wave_label": "wave-1", "verification_payload": _GOOD_PAYLOAD},
        db, "/tmp",
    )
    assert "error" in res and "verification_run_id" in res["error"]
    assert "gate_completed" not in res
    assert await _still_blocked(db, pid, item)


@pytest.mark.asyncio
async def test_recorded_passing_run_unlocks_and_the_recorded_row_is_the_evidence(db):
    pid, item = await _gated_project(db, "gate-recorded")
    run_id = await _recorded_run(db, pid)
    res = await srv._dispatch_mcp_tool(
        "complete_wave_gate",
        {"project_id": pid, "wave_label": "wave-1", "verification_run_id": run_id},
        db, "/tmp",
    )
    assert res["gate_completed"] is True
    assert res["evidence_source"] == "verification_run"
    assert res["verification_run_id"] == run_id
    assert res["next_wave_item_ids"] == [item["id"]]
    claimed = await db_module.claim_sprint_item(db, pid, item["id"])
    assert claimed.get("status") == "in_progress"
    async with db.execute(
        "SELECT evidence_snapshot FROM wave_gate_results WHERE project_id = ?", (pid,),
    ) as cur:
        row = await cur.fetchone()
    snap = json.loads(row["evidence_snapshot"] if isinstance(row, dict) else row[0])
    assert snap["verification_run_id"] == run_id and snap["evidence_source"] == "verification_run"


@pytest.mark.asyncio
async def test_recorded_failing_run_cannot_unlock_even_with_a_typed_ok_payload(db):
    """The RECORDED result decides — a caller-typed payload sent alongside a
    real run id is never consulted."""
    pid, item = await _gated_project(db, "gate-failing-run")
    run_id = await _recorded_run(db, pid, exit_code=1)
    res = await srv._dispatch_mcp_tool(
        "complete_wave_gate",
        {"project_id": pid, "wave_label": "wave-1", "verification_run_id": run_id,
         "verification_payload": _GOOD_PAYLOAD},
        db, "/tmp",
    )
    assert "exit_code=1" in res["error"]
    assert await _still_blocked(db, pid, item)


@pytest.mark.parametrize("status", ["error", "timeout"])
@pytest.mark.asyncio
async def test_recorded_non_ok_run_cannot_unlock(db, status):
    pid, item = await _gated_project(db, f"gate-run-{status}")
    run_id = await _recorded_run(db, pid, status=status, exit_code=None)
    with pytest.raises(ValueError, match=f"status='{status}'"):
        await db_module.complete_wave_gate(db, pid, "wave-1", verification_run_id=run_id)
    assert await _still_blocked(db, pid, item)


@pytest.mark.asyncio
async def test_in_flight_unknown_and_foreign_runs_cannot_unlock(db):
    pid, item = await _gated_project(db, "gate-bad-runs")
    running = await _recorded_run(db, pid, complete=False)
    with pytest.raises(ValueError, match="has not completed"):
        await db_module.complete_wave_gate(db, pid, "wave-1", verification_run_id=running)
    with pytest.raises(ValueError, match="not a recorded run_verification run"):
        await db_module.complete_wave_gate(db, pid, "wave-1", verification_run_id="no-such-run")
    other = await db_module.create_project(db, "other-gate-project")
    foreign = await _recorded_run(db, other["id"])
    with pytest.raises(ValueError, match="different project"):
        await db_module.complete_wave_gate(db, pid, "wave-1", verification_run_id=foreign)
    assert await _still_blocked(db, pid, item)


@pytest.mark.asyncio
async def test_one_recorded_run_cannot_unlock_two_gates(db):
    pid = await _project(db, "gate-single-use")
    run_id = await _recorded_run(db, pid)
    await db_module.complete_wave_gate(db, pid, "wave-1", verification_run_id=run_id)
    with pytest.raises(ValueError, match="already unlocked another wave gate"):
        await db_module.complete_wave_gate(db, pid, "wave-2", verification_run_id=run_id)
    fresh = await _recorded_run(db, pid)
    assert (await db_module.complete_wave_gate(
        db, pid, "wave-2", verification_run_id=fresh,
    ))["gate_completed"] is True


@pytest.mark.asyncio
async def test_a_typed_payload_cannot_plant_a_run_id_to_burn_a_real_run(db):
    pid = await _project(db, "gate-plant-run-id")
    victim = await _recorded_run(db, pid)
    hitl_id = await _approved_override(db, pid, "wave-1", None)
    await db_module.complete_wave_gate(
        db, pid, "wave-1", {**_GOOD_PAYLOAD, "verification_run_id": victim},
        unbound_payload_override={"reason": "tunnel down", "hitl_id": hitl_id},
    )
    # The victim run is still spendable: the planted key was stripped.
    assert (await db_module.complete_wave_gate(
        db, pid, "wave-2", verification_run_id=victim,
    ))["gate_completed"] is True


# --- the human-approved override ------------------------------------------

async def _approved_override(db, pid, wave, version, *, answer=_YES, answered_by="adam"):
    """File the approval HITL exactly as the handler does, then answer it."""
    hitl = await gate_override.request_gate_override_hitl(
        db, pid, gate=gate_override.GATE_WAVE_GATE_UNBOUND_PAYLOAD,
        subject_id=gate_override.wave_gate_subject(wave, version),
        reason="tunnel down", description="test",
    )
    await db_module.answer_hitl_request(db, hitl["id"], answer, answered_by=answered_by)
    return hitl["id"]


@pytest.mark.asyncio
async def test_override_without_a_reason_is_refused(db):
    pid, item = await _gated_project(db, "gate-ov-noreason")
    res = await srv._dispatch_mcp_tool(
        "complete_wave_gate",
        {"project_id": pid, "wave_label": "wave-1", "verification_payload": _GOOD_PAYLOAD,
         "override_unbound_payload": True},
        db, "/tmp",
    )
    assert res["error"] == "OVERRIDE_REASON_REQUIRED"
    assert await _still_blocked(db, pid, item)


@pytest.mark.asyncio
async def test_override_files_a_require_human_hitl_that_auto_answer_cannot_touch(db):
    pid, item = await _gated_project(db, "gate-ov-hitl")
    # The exact configuration the audit flagged: aggressive auto-answer.
    await db_module.update_project_settings(db, pid, hitl_auto_answer=2)
    args = {"project_id": pid, "wave_label": "wave-1",
            "verification_payload": _GOOD_PAYLOAD,
            "override_unbound_payload": True, "override_reason": "tunnel is down"}
    res = await srv._dispatch_mcp_tool("complete_wave_gate", dict(args), db, "/tmp")
    assert res["error"] == "HUMAN_APPROVAL_REQUIRED"
    hitl = await db_module.get_hitl_request(db, res["hitl_id"])
    assert hitl["kind"] == gate_override.GATE_OVERRIDE_HITL_KIND
    assert hitl["status"] == "pending", "an override HITL must never be auto-answered"
    assert hitl["answered_by"] is None
    payload = json.loads(hitl["payload"])
    assert payload["require_human"] is True
    assert payload["gate"] == gate_override.GATE_WAVE_GATE_UNBOUND_PAYLOAD
    # Retrying does not spam the human queue: the pending request is reused.
    again = await srv._dispatch_mcp_tool("complete_wave_gate", dict(args), db, "/tmp")
    assert again["hitl_id"] == res["hitl_id"]
    assert await _still_blocked(db, pid, item)


@pytest.mark.asyncio
async def test_override_with_an_unanswered_hitl_is_refused(db):
    pid, item = await _gated_project(db, "gate-ov-unanswered")
    hitl = await gate_override.request_gate_override_hitl(
        db, pid, gate=gate_override.GATE_WAVE_GATE_UNBOUND_PAYLOAD,
        subject_id=gate_override.wave_gate_subject("wave-1", None),
        reason="tunnel down", description="test",
    )
    res = await srv._dispatch_mcp_tool(
        "complete_wave_gate",
        {"project_id": pid, "wave_label": "wave-1", "verification_payload": _GOOD_PAYLOAD,
         "override_unbound_payload": True, "override_reason": "tunnel down",
         "override_hitl_id": hitl["id"]},
        db, "/tmp",
    )
    assert res["error"] == "HITL_NOT_ANSWERED"
    assert await _still_blocked(db, pid, item)


@pytest.mark.asyncio
async def test_override_refused_when_the_human_says_no_or_it_was_auto_answered(db):
    pid, item = await _gated_project(db, "gate-ov-notapproved")
    for answer, by in (("No — do not approve", "adam"), (_YES, "auto")):
        hid = await _approved_override(db, pid, "wave-1", None, answer=answer, answered_by=by)
        res = await srv._dispatch_mcp_tool(
            "complete_wave_gate",
            {"project_id": pid, "wave_label": "wave-1",
             "verification_payload": _GOOD_PAYLOAD, "override_unbound_payload": True,
             "override_reason": "tunnel down", "override_hitl_id": hid},
            db, "/tmp",
        )
        assert res["error"] == "HITL_NOT_APPROVED", (answer, by, res)
    assert await _still_blocked(db, pid, item)


@pytest.mark.asyncio
async def test_only_a_server_filed_gate_override_hitl_authorizes(db):
    """An agent cannot mint one: the MCP request_hitl tool clamps `kind`, so a
    hitl it files (even answered Yes) is not a gate_override approval."""
    pid, item = await _gated_project(db, "gate-ov-forged")
    filed = await srv._dispatch_mcp_tool(
        "request_hitl",
        {"project_id": pid, "question": "approve the wave gate override?",
         "kind": "gate_override", "require_human": True},
        db, "/tmp",
    )
    assert filed["kind"] == "question"
    await db_module.answer_hitl_request(db, filed["id"], _YES, answered_by="adam")
    res = await srv._dispatch_mcp_tool(
        "complete_wave_gate",
        {"project_id": pid, "wave_label": "wave-1", "verification_payload": _GOOD_PAYLOAD,
         "override_unbound_payload": True, "override_reason": "tunnel down",
         "override_hitl_id": filed["id"]},
        db, "/tmp",
    )
    assert res["error"] == "HITL_INVALID"
    # ...and another project's approval is indistinguishable from a missing one.
    other = await db_module.create_project(db, "elsewhere")
    foreign = await gate_override.request_gate_override_hitl(
        db, other["id"], gate=gate_override.GATE_WAVE_GATE_UNBOUND_PAYLOAD,
        subject_id=gate_override.wave_gate_subject("wave-1", None),
        reason="x", description="x",
    )
    await db_module.answer_hitl_request(db, foreign["id"], _YES, answered_by="adam")
    res2 = await srv._dispatch_mcp_tool(
        "complete_wave_gate",
        {"project_id": pid, "wave_label": "wave-1", "verification_payload": _GOOD_PAYLOAD,
         "override_unbound_payload": True, "override_reason": "tunnel down",
         "override_hitl_id": foreign["id"]},
        db, "/tmp",
    )
    assert res2["error"] == "HITL_NOT_FOUND"
    assert await _still_blocked(db, pid, item)


@pytest.mark.asyncio
async def test_human_approved_override_unlocks_once_and_is_audited(db):
    pid, item = await _gated_project(db, "gate-ov-approved")
    hid = await _approved_override(db, pid, "wave-1", None)
    args = {"project_id": pid, "wave_label": "wave-1", "verification_payload": _GOOD_PAYLOAD,
            "override_unbound_payload": True, "override_reason": "tunnel down",
            "override_hitl_id": hid, "session_id": None}
    res = await srv._dispatch_mcp_tool("complete_wave_gate", dict(args), db, "/tmp")
    assert res["gate_completed"] is True
    assert res["evidence_source"] == "unbound_payload_override"
    assert res["override"]["hitl_id"] == hid and res["override"]["reason"] == "tunnel down"
    assert (await db_module.claim_sprint_item(db, pid, item["id"])).get("status") == "in_progress"

    rows = await _audit(db, pid, gate_override.WAVE_GATE_UNBOUND_PAYLOAD_EVENT_TYPE)
    assert len(rows) == 1
    detail = json.loads(rows[0]["detail"])
    assert detail["reason"] == "tunnel down" and detail["hitl_id"] == hid
    assert detail["subject_id"] == gate_override.wave_gate_subject("wave-1", None)

    # The approval is spent: it cannot open a second gate (bound subject + used).
    with pytest.raises(gate_override.GateOverrideError) as exc:
        await db_module.complete_wave_gate(
            db, pid, "wave-2", _GOOD_PAYLOAD,
            unbound_payload_override={"reason": "again", "hitl_id": hid},
        )
    assert exc.value.code in ("HITL_INVALID", "HITL_ALREADY_USED")


@pytest.mark.asyncio
async def test_bad_payload_never_burns_an_approval(db):
    pid, item = await _gated_project(db, "gate-ov-badpayload")
    hid = await _approved_override(db, pid, "wave-1", None)
    with pytest.raises(ValueError, match="exit_code must be 0"):
        await db_module.complete_wave_gate(
            db, pid, "wave-1", {**_GOOD_PAYLOAD, "exit_code": 2},
            unbound_payload_override={"reason": "tunnel down", "hitl_id": hid},
        )
    # Validation failed before the approval was spent: it still works.
    ok = await db_module.complete_wave_gate(
        db, pid, "wave-1", _GOOD_PAYLOAD,
        unbound_payload_override={"reason": "tunnel down", "hitl_id": hid},
    )
    assert ok["gate_completed"] is True


# ---------------------------------------------------------------------------
# C. every override needs a reason and leaves an audit row
# ---------------------------------------------------------------------------

def _fake_ci(state):
    async def _verify(repo, sha, *, token=None, **_kw):
        return {"sha": sha, "repo": repo, "state": state, "total": 3, "failed": 1}
    return _verify


@pytest.mark.asyncio
async def test_override_ci_needs_a_reason_and_is_audited(db, monkeypatch):
    p = await db_module.create_project(db, "ci-override-audit")
    await db_module.update_project_settings(db, p["id"], github_repo="meridianmcp/Meridian")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "ship widget")
    monkeypatch.setattr(github_ci, "verify_commit_ci", _fake_ci("failure"))
    base = {"project_id": p["id"], "item_id": item["id"],
            "notes": "done; committed abc1234 to main", "override_ci": True,
            "actor": "exec-1"}

    refused = await srv._dispatch_mcp_tool("complete_sprint_item", dict(base), db, "/tmp")
    assert refused["error"] == "OVERRIDE_REASON_REQUIRED"
    assert (await db_module.get_sprint_item(db, item["id"]))["status"] != "done"
    assert await _audit(db, p["id"], gate_override.CI_OVERRIDE_EVENT_TYPE) == []

    done = await srv._dispatch_mcp_tool(
        "complete_sprint_item", {**base, "override_reason": "flaky unrelated job"}, db, "/tmp",
    )
    assert done["status"] == "done"
    assert done["ci_override"]["reason"] == "flaky unrelated job"
    rows = await _audit(db, p["id"], gate_override.CI_OVERRIDE_EVENT_TYPE)
    assert len(rows) == 1 and rows[0]["actor"] == "exec-1"
    assert json.loads(rows[0]["detail"])["reason"] == "flaky unrelated job"


@pytest.mark.asyncio
async def test_force_foreign_claim_needs_a_reason_and_is_audited(db):
    p = await db_module.create_project(db, "force-audit")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "handed off")
    owner = await db_module.register_session(db, p["id"], "owner")
    await db_module.claim_sprint_item(db, p["id"], item["id"], actor=owner["id"])

    with pytest.raises(db_module.SprintItemClaimMismatch, match="override_reason"):
        await db_module.complete_sprint_item(
            db, p["id"], item["id"], actor="someone-else", force_foreign_claim=True,
        )
    assert (await db_module.get_sprint_item(db, item["id"]))["status"] == "in_progress"
    assert await _audit(db, p["id"], gate_override.FOREIGN_CLAIM_OVERRIDE_EVENT_TYPE) == []

    done = await db_module.complete_sprint_item(
        db, p["id"], item["id"], actor="someone-else", force_foreign_claim=True,
        override_reason="owner went offline", tenant_id="tenant-1",
    )
    assert done["status"] == "done"
    assert done["foreign_claim_override"]["claim_owner"] == owner["id"]
    rows = await _audit(db, p["id"], gate_override.FOREIGN_CLAIM_OVERRIDE_EVENT_TYPE)
    assert len(rows) == 1
    assert rows[0]["actor"] == "someone-else" and rows[0]["tenant_id"] == "tenant-1"
    assert json.loads(rows[0]["detail"])["reason"] == "owner went offline"


@pytest.mark.asyncio
async def test_an_unneeded_force_flag_overrides_nothing_and_writes_no_audit(db):
    p = await db_module.create_project(db, "force-unneeded")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "own claim")
    await db_module.claim_sprint_item(db, p["id"], item["id"], actor="me")
    done = await db_module.complete_sprint_item(
        db, p["id"], item["id"], actor="me", force_foreign_claim=True,
    )
    assert done["status"] == "done" and "foreign_claim_override" not in done
    assert await _audit(db, p["id"], gate_override.FOREIGN_CLAIM_OVERRIDE_EVENT_TYPE) == []


@pytest.mark.asyncio
async def test_force_foreign_claim_via_mcp_is_refused_without_a_reason(db):
    p = await db_module.create_project(db, "force-mcp")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "handed off")
    owner = await db_module.register_session(db, p["id"], "owner")
    await db_module.claim_sprint_item(db, p["id"], item["id"], actor=owner["id"])
    res = await srv._dispatch_mcp_tool(
        "complete_sprint_item",
        {"project_id": p["id"], "item_id": item["id"], "actor": "someone-else",
         "force_foreign_claim": True},
        db, "/tmp",
    )
    assert res["error"] == "CLAIM_MISMATCH" and "override_reason" in res["message"]


@pytest.mark.asyncio
async def test_prospect_bypass_by_an_agent_needs_a_reason_and_is_audited(db):
    p = await db_module.create_project(db, "bypass-audit")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "unprospected work")
    base = {"project_id": p["id"], "item_id": item["id"], "prospect_bypass": True,
            "actor": "agent-7"}

    refused = await srv._dispatch_mcp_tool("update_sprint_item", dict(base), db, "/tmp")
    assert refused["error"] == "OVERRIDE_REASON_REQUIRED"
    assert not (await db_module.get_sprint_item(db, item["id"])).get("prospect_bypass")
    assert await _audit(db, p["id"], gate_override.PROSPECT_BYPASS_OVERRIDE_EVENT_TYPE) == []

    ok = await srv._dispatch_mcp_tool(
        "update_sprint_item", {**base, "override_reason": "planner-approved spike"}, db, "/tmp",
    )
    assert ok.get("error") is None and int(ok["prospect_bypass"]) == 1
    rows = await _audit(db, p["id"], gate_override.PROSPECT_BYPASS_OVERRIDE_EVENT_TYPE)
    assert len(rows) == 1 and rows[0]["actor"] == "agent-7"
    assert json.loads(rows[0]["detail"])["reason"] == "planner-approved spike"

    # Clearing the bypass re-enables the gate: no reason, no audit row.
    cleared = await srv._dispatch_mcp_tool(
        "update_sprint_item",
        {"project_id": p["id"], "item_id": item["id"], "prospect_bypass": False},
        db, "/tmp",
    )
    assert cleared.get("error") is None and int(cleared["prospect_bypass"]) == 0
    assert len(await _audit(db, p["id"], gate_override.PROSPECT_BYPASS_OVERRIDE_EVENT_TYPE)) == 1


def test_override_reason_helper_rejects_blank_reasons():
    for blank in (None, "", "   "):
        with pytest.raises(gate_override.GateOverrideError) as exc:
            gate_override.require_override_reason(blank, flag="x")
        assert exc.value.code == "OVERRIDE_REASON_REQUIRED"
    assert gate_override.require_override_reason("  because  ") == "because"
