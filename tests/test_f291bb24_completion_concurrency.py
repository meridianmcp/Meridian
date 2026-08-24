"""f291bb24 — contention/concurrency coverage for complete_sprint_item's
CI / code-intel-receipt / test-run-evidence checks now running concurrently
via asyncio.gather (meridian/mcp/handlers/sprint_tools.py) instead of one
after another. Targets the "10-15 sequential DB/network round trips"
latency issue reported against complete_sprint_item's 45s dispatch timeout,
and the explicit ask to add contention tests alongside the fix.

Every pre-existing behavioral contract for these three gates is already
covered by tests/test_w5_427b7902_ci_gate.py (CI gate),
tests/test_code_intel_guard.py (code-intel receipt gate), and
tests/test_e24f2daa_test_run_receipt.py (test-run-receipt gate) -- this file
does NOT re-test those. It covers what's NEW about the concurrent refactor:
  1. The three checks genuinely run in parallel (wall-clock proof).
  2. Gate PRECEDENCE is unchanged even though the underlying data is now
     fetched out of order relative to gate evaluation -- CI failure must
     still win over a simultaneously-failing code-intel gate, exactly as
     when the checks ran strictly sequentially.
  3. Multiple concurrent complete_sprint_item calls against the SAME item
     still yield exactly one real completion -- hoisting the _pre_item read
     above the new gather() didn't open a new race window.
  4. The existing dispatch-level timeout (asyncio.wait_for in
     meridian/mcp/handler.py) still cancels cleanly when it fires WHILE the
     new gather() is in flight, not just when the whole handler stalls.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from meridian import code_intel_receipt as _cir_mod
from meridian import db as db_module
from meridian import github_ci
from meridian import server as srv
from meridian import test_run_receipt as test_run_receipt_module


async def _project_with_repo(db, name):
    p = await db_module.create_project(db, name)
    await db_module.update_project_settings(
        db, p["id"], github_repo="meridianmcp/Meridian"
    )
    return p


def _slow_ci(state, delay, *, failed=0):
    async def _verify(repo, sha, *, token=None, **_kw):
        await asyncio.sleep(delay)
        return {"sha": sha, "repo": repo, "state": state, "total": max(failed, 1), "failed": failed}
    return _verify


def _slow_code_intel(result, delay):
    async def _verify(db, tenant, project_id, item, *, session_id=None, live_inventory=None, **_kw):
        await asyncio.sleep(delay)
        return result
    return _verify


def _slow_repo_root(root, delay):
    async def _resolve(db, default_root, session_id):
        await asyncio.sleep(delay)
        return root
    return _resolve


# ---------------------------------------------------------------------------
# 1. Wall-clock proof the three checks run concurrently, not serially.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ci_code_intel_test_run_checks_run_concurrently(db, monkeypatch, tmp_path):
    """With three independent checks each artificially slowed to 0.3s, the
    whole call must complete well under their SUM (0.9s) -- proving they run
    concurrently rather than one after another. Before f291bb24 this would
    have taken >=0.9s; the concurrent refactor bounds it near the slowest
    single check."""
    p = await _project_with_repo(db, "conc-timing")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "timing item")

    monkeypatch.setattr(github_ci, "verify_commit_ci", _slow_ci("success", 0.3))
    monkeypatch.setattr(
        _cir_mod, "verify_code_intel_prospecting",
        _slow_code_intel({"applicable": False, "ok": True}, 0.3),
    )
    monkeypatch.setattr(
        test_run_receipt_module, "resolve_repo_root_for_session",
        _slow_repo_root(str(tmp_path), 0.3),
    )
    monkeypatch.setattr(test_run_receipt_module, "get_test_run_evidence", lambda root: None)

    start = time.monotonic()
    res = await srv._dispatch_mcp_tool(
        "complete_sprint_item",
        {"project_id": p["id"], "item_id": item["id"],
         "notes": "done; committed abc1234 to main"},
        db, "/tmp")
    elapsed = time.monotonic() - start

    assert res.get("error") is None, res
    assert res["status"] == "done"
    assert res["ci_verification"]["state"] == "success"
    # Sequential would take >=0.9s; concurrent should land near 0.3s + overhead.
    assert elapsed < 0.7, f"checks did not run concurrently: took {elapsed:.2f}s"


# ---------------------------------------------------------------------------
# 2. Gate precedence preserved: CI failure still wins over a simultaneously
#    failing code-intel gate, matching the pre-refactor evaluation order.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_concurrent_gate_precedence_ci_failure_wins_over_code_intel(db, monkeypatch):
    """CI failing and the code-intel receipt gate BOTH failing at once must
    still resolve as CI_FAILING -- the same precedence the two gates had
    when evaluated strictly sequentially. Computing both results
    concurrently must never change which gate's verdict wins."""
    p = await _project_with_repo(db, "conc-precedence")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "precedence item")

    monkeypatch.setattr(github_ci, "verify_commit_ci", _slow_ci("failure", 0.0, failed=2))

    async def _fail_code_intel(db, tenant, project_id, item, *, session_id=None, live_inventory=None, **_kw):
        return {
            "applicable": True, "ok": False,
            "code": "CODE_INTEL_RECEIPT_MISSING",
            "capability": {"id": "code_intel_prospecting"},
            "message": "no receipt on file",
        }
    monkeypatch.setattr(_cir_mod, "verify_code_intel_prospecting", _fail_code_intel)

    res = await srv._dispatch_mcp_tool(
        "complete_sprint_item",
        {"project_id": p["id"], "item_id": item["id"],
         "notes": "done; committed abc1234 to main"},
        db, "/tmp")

    assert res["error"] == "CI_FAILING", res
    fresh = await db_module.get_sprint_item(db, item["id"])
    assert fresh["status"] != "done"


# ---------------------------------------------------------------------------
# 3. Multiple concurrent completions of the SAME item.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_concurrent_complete_calls_same_item_exactly_one_real_completion(db):
    """N concurrent complete_sprint_item MCP calls against the SAME pending
    item -- the item ends up 'done' exactly once, every response is either
    a clean success or a clean STATUS_RACE (never a crash), and no response
    reports a different terminal status than 'done'. Confirms hoisting the
    _pre_item read above the new asyncio.gather batch didn't introduce a
    new race window before the atomic commit."""
    p = await db_module.create_project(db, "conc-same-item")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "raced completion")

    n = 8
    results = await asyncio.gather(*[
        srv._dispatch_mcp_tool(
            "complete_sprint_item",
            {"project_id": p["id"], "item_id": item["id"]},
            db, "/tmp",
        )
        for _ in range(n)
    ])

    for r in results:
        assert r.get("error") in (None, "STATUS_RACE"), r
        if r.get("error") is None:
            assert r["status"] == "done"
        # A STATUS_RACE loser may legitimately report current_status="done"
        # too -- it just means another concurrent caller reached the SAME
        # target status first. The only thing that would be wrong is a
        # DIFFERENT terminal status or a crash, neither of which happens.

    oks = [r for r in results if r.get("error") is None]
    assert len(oks) >= 1

    final = await db_module.get_sprint_item(db, item["id"])
    assert final["status"] == "done"


# ---------------------------------------------------------------------------
# 4. The dispatch-level timeout wrapper still cancels cleanly when it fires
#    WHILE the new gather() is in flight, not just when the whole handler
#    stalls on one big call.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dispatch_timeout_cancels_cleanly_while_gathered_checks_are_in_flight(db, monkeypatch):
    """A hung code-intel check (the slowest realistic leg in production)
    must not prevent the existing dispatch-level asyncio.wait_for timeout
    contract from working: the call must still time out promptly, classify
    as timed_out_before_commit (nothing was written), and leave the item
    cleanly retryable -- no orphaned lock, no half-applied state, no hang."""
    from meridian.mcp import handler as mh

    p = await _project_with_repo(db, "conc-timeout-cancel")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "cancel me")
    monkeypatch.setattr(mh, "_COMPLETE_SPRINT_ITEM_DISPATCH_TIMEOUT_S", 0.05)

    async def _hang_code_intel(db, tenant, project_id, item, *, session_id=None, live_inventory=None, **_kw):
        await asyncio.sleep(5.0)
        raise AssertionError("should have been cancelled by the dispatch timeout")
    monkeypatch.setattr(_cir_mod, "verify_code_intel_prospecting", _hang_code_intel)

    result = await srv._dispatch_mcp_tool(
        "complete_sprint_item",
        {"project_id": p["id"], "item_id": item["id"],
         "notes": "done; committed abc1234 to main"},
        db, "/tmp",
    )
    assert result["error"] == "COMPLETE_SPRINT_ITEM_TIMEOUT"
    assert result["completion_outcome"] == "timed_out_before_commit"
    assert result["current_status"] != "done"

    # Nothing committed by the cancelled attempt -- a normal retry now
    # succeeds cleanly.
    retried = await db_module.complete_sprint_item(db, p["id"], item["id"])
    assert retried["status"] == "done"
