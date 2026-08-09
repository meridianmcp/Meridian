"""394bcbdf — R2-E: resource-aware / asynchronously-recoverable / idempotent
completion timeouts. DB-layer coverage for ``complete_sprint_item``.

This file covers the two pieces of the design that live in
``meridian/db/sprint_items.py``:

  1. Resource-aware retry-after diagnostics: when the bounded advisory-work
     phase (rollup / task-chain-advance, ``_ADVISORY_PHASE_TIMEOUT_S``) gets
     deferred, the returned dict gains a ``resource_diagnostics`` field
     (self-sampled from ``meridian.process_budget.sample_server_process``)
     so a caller can tell whether the deferral is plausibly explained by the
     server process itself being over its configured memory/CPU budget.
  2. "Preserve verifier evidence": an independent fresh-session verification
     recorded during a completion attempt that is then interrupted before
     the active->done transition lands must not be lost -- a retry that
     omits verifier_session_id/verification_verdict entirely must still
     succeed off the already-filed verdict.

The broader idempotent-retry / correlation_id / phase_timings_ms /
dispatch-level timeout-classification behavior (a2a027cf) is already covered
by tests/test_sprint_item_status_race.py; this file only adds NEW coverage
for the 394bcbdf additions, not a re-test of everything complete_sprint_item
already does.
"""
import asyncio

import pytest

from meridian import db as db_module
import meridian.db.sprint_items as _sprint_items_mod
import meridian.process_budget as process_budget_module


async def _project_with_item(db):
    p = await db_module.create_project(db, "resource-aware-completion")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "resource-aware item")
    return p, item


# ---------------------------------------------------------------------------
# Resource-aware retry-after diagnostics on advisory-work deferral.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resource_diagnostics_absent_on_normal_completion(db):
    """The common-case payload is unchanged: no resource_diagnostics key
    when advisory work was NOT deferred."""
    p, item = await _project_with_item(db)
    result = await db_module.complete_sprint_item(db, p["id"], item["id"])
    assert result["status"] == "done"
    assert result.get("advisory_work_deferred") is not True
    assert "resource_diagnostics" not in result


@pytest.mark.asyncio
async def test_resource_diagnostics_present_when_advisory_work_deferred(db, monkeypatch):
    """When the bounded advisory-work budget is exceeded, the response now
    also carries a resource_diagnostics shape (best-effort self-sample)."""
    p, item = await _project_with_item(db)

    monkeypatch.setattr(_sprint_items_mod, "_ADVISORY_PHASE_TIMEOUT_S", 0.05)

    async def _slow_side_effects(*a, **k):
        await asyncio.sleep(5.0)

    monkeypatch.setattr(_sprint_items_mod, "_run_post_commit_side_effects", _slow_side_effects)

    result = await db_module.complete_sprint_item(db, p["id"], item["id"])

    assert result["status"] == "done"
    assert result["advisory_work_deferred"] is True
    assert "resource_diagnostics" in result
    diag = result["resource_diagnostics"]
    assert set(diag.keys()) == {"action", "reason", "retry_after_seconds"}
    assert isinstance(diag["retry_after_seconds"], (int, float))


@pytest.mark.asyncio
async def test_resource_diagnostics_reflects_a_real_breach(db, monkeypatch):
    """The diagnostics are not a static placeholder -- a genuine breach
    reported by the server-self monitor (quiesce/kill) flows through into
    the response, including a non-zero, budget-derived retry_after_seconds."""
    p, item = await _project_with_item(db)

    monkeypatch.setattr(_sprint_items_mod, "_ADVISORY_PHASE_TIMEOUT_S", 0.05)

    async def _slow_side_effects(*a, **k):
        await asyncio.sleep(5.0)

    monkeypatch.setattr(_sprint_items_mod, "_run_post_commit_side_effects", _slow_side_effects)

    fake_budget = process_budget_module.ProcessBudget(sample_interval_seconds=42.0)
    fake_report = process_budget_module.BudgetReport(
        label="server-self", pid=4242, run_id=None, sample=None,
        budget=fake_budget, action="quiesce",
        reason="memory 999999 bytes exceeds budget 100 bytes",
    )
    monkeypatch.setattr(
        process_budget_module, "sample_server_process", lambda *a, **k: fake_report
    )

    result = await db_module.complete_sprint_item(db, p["id"], item["id"])

    diag = result["resource_diagnostics"]
    assert diag["action"] == "quiesce"
    assert diag["retry_after_seconds"] == 42.0


@pytest.mark.asyncio
async def test_resource_diagnostics_never_blocks_completion_on_sampling_failure(db, monkeypatch):
    """A sampling failure must degrade silently -- the completion itself
    (already committed by the time diagnostics run) is never affected."""
    p, item = await _project_with_item(db)

    monkeypatch.setattr(_sprint_items_mod, "_ADVISORY_PHASE_TIMEOUT_S", 0.05)

    async def _slow_side_effects(*a, **k):
        await asyncio.sleep(5.0)

    monkeypatch.setattr(_sprint_items_mod, "_run_post_commit_side_effects", _slow_side_effects)

    def _boom(*a, **k):
        raise RuntimeError("psutil exploded")

    monkeypatch.setattr(process_budget_module, "sample_server_process", _boom)

    result = await db_module.complete_sprint_item(db, p["id"], item["id"])
    assert result["status"] == "done"
    assert result["advisory_work_deferred"] is True
    # No resource_diagnostics key when sampling itself raised -- the
    # completion succeeded regardless.
    assert "resource_diagnostics" not in result


# ---------------------------------------------------------------------------
# "Preserve verifier evidence" — an independent verification recorded during
# an interrupted completion attempt must not be lost on retry.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verifier_evidence_survives_interrupted_transition_and_retry(db, monkeypatch):
    """394bcbdf — simulate a completion attempt that records a fresh
    independent PASS verification and is THEN interrupted (mirrors a
    dispatch-level cancellation landing mid-call) before the active->done
    transition itself lands. The verification's own commit already landed
    and must survive; a bare retry (no verifier_session_id/
    verification_verdict re-supplied) must succeed off the on-file verdict,
    not re-raise SprintItemVerificationRequired."""
    p, item = await _project_with_item(db)
    await db_module.patch_sprint_item(db, p["id"], item["id"], require_verification=True)
    await db_module.claim_sprint_item(db, p["id"], item["id"], actor="implementer-session")

    real_update = _sprint_items_mod._update_sprint_item_status
    interrupted = {"fired": False}

    async def _interrupt_once(db_conn, project_id, item_id, status, **kwargs):
        if not interrupted["fired"]:
            interrupted["fired"] = True
            raise asyncio.CancelledError()
        return await real_update(db_conn, project_id, item_id, status, **kwargs)

    # Patch both db_module and the sprint_items submodule — complete_sprint_item's
    # actual call to _update_sprint_item_status goes through sprint_items.py's
    # local namespace after the module split (same dual-patch pattern used
    # throughout tests/test_sprint_item_status_race.py).
    monkeypatch.setattr(db_module, "_update_sprint_item_status", _interrupt_once)
    monkeypatch.setattr(_sprint_items_mod, "_update_sprint_item_status", _interrupt_once)

    with pytest.raises(asyncio.CancelledError):
        await db_module.complete_sprint_item(
            db, p["id"], item["id"], actor="implementer-session",
            verifier_session_id="fresh-verifier-session",
            verification_verdict="pass",
            verification_notes="reviewed the diff independently",
        )

    # The verification's own write already committed before the interruption.
    on_file = await db_module.get_latest_sprint_item_verification(db, p["id"], item["id"])
    assert on_file is not None
    assert on_file["verdict"] == "pass"
    assert on_file["verifier_session_id"] == "fresh-verifier-session"

    # The item itself never flipped to done.
    still = await db_module.get_sprint_item(db, item["id"])
    assert still["status"] == "in_progress"

    # Retry WITHOUT re-supplying verifier_session_id/verification_verdict —
    # must succeed off the already-filed PASS.
    result = await db_module.complete_sprint_item(
        db, p["id"], item["id"], actor="implementer-session",
    )
    assert result["status"] == "done"
    assert result["completion_outcome"] == "committed"


@pytest.mark.asyncio
async def test_verifier_evidence_retry_does_not_duplicate_verification_rows(db, monkeypatch):
    """A retry that succeeds off the already-filed verdict must not file a
    second, redundant verification row."""
    p, item = await _project_with_item(db)
    await db_module.patch_sprint_item(db, p["id"], item["id"], require_verification=True)
    await db_module.claim_sprint_item(db, p["id"], item["id"], actor="implementer-session")

    real_update = _sprint_items_mod._update_sprint_item_status
    interrupted = {"fired": False}

    async def _interrupt_once(db_conn, project_id, item_id, status, **kwargs):
        if not interrupted["fired"]:
            interrupted["fired"] = True
            raise asyncio.CancelledError()
        return await real_update(db_conn, project_id, item_id, status, **kwargs)

    monkeypatch.setattr(db_module, "_update_sprint_item_status", _interrupt_once)
    monkeypatch.setattr(_sprint_items_mod, "_update_sprint_item_status", _interrupt_once)

    with pytest.raises(asyncio.CancelledError):
        await db_module.complete_sprint_item(
            db, p["id"], item["id"], actor="implementer-session",
            verifier_session_id="fresh-verifier-session",
            verification_verdict="pass",
        )

    await db_module.complete_sprint_item(
        db, p["id"], item["id"], actor="implementer-session",
    )

    async with db.execute(
        "SELECT COUNT(*) AS c FROM sprint_item_verifications "
        "WHERE project_id = ? AND sprint_item_id = ?",
        (p["id"], item["id"]),
    ) as cur:
        row = await cur.fetchone()
    count = row["c"] if isinstance(row, dict) else row[0]
    assert count == 1
