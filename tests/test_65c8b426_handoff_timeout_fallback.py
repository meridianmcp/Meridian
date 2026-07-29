"""Tests for sprint item 65c8b426 — generate_handoff timeout fallback and AI-skip.

Three parts:

PART 1 (honest fallback): When generate_handoff times out the MCP handler must
return mode='l0_fallback' and degraded=True, NOT mode='full' and degraded=False.
Callers (dashboard, executors) rely on this to distinguish the emergency 4-field
version from a real full handoff.

PART 2 (default skip_ai_summary=True): The MCP generate_handoff tool path must
default skip_ai_summary=True so the 3 serial Haiku seams are skipped unless the
caller explicitly opts in. This alone eliminates the live timeout root cause
(serial Haiku fan-out > 90s wall time with 5 active sessions).

PART 3 (parallel summarize_session): generate_handoff's session-summary fan-out
must use asyncio.gather (parallel) instead of serial await-in-a-loop. N sessions
now take ~1 Haiku latency instead of N × Haiku latency.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

import meridian.server  # noqa: F401 — import before handler to avoid circular import
from meridian import db as db_module
from meridian import handoff as handoff_module
from meridian.mcp import handler as mh


# ---------------------------------------------------------------------------
# PART 1 — honest timeout fallback: mode='l0_fallback', degraded=True
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_handoff_l0_fallback_mode_and_degraded_flag(db, tmp_path, monkeypatch):
    """When generate_handoff times out, mode must be 'l0_fallback' and
    degraded must be True — never mode='full'/degraded=False (the old bug)."""
    p = await db_module.create_project(db, "timeout-proj")
    await db_module.set_goal(db, p["id"], "do things", sprint="v1")

    # Patch asyncio.wait_for inside the handler module so _handle_task_tools sees it.
    # Close the coroutine before raising to avoid the "coroutine was never awaited"
    # RuntimeWarning that would fire if we just drop the coro on the floor.
    async def _mock_wait_for(coro, timeout):
        coro.close()  # clean up the abandoned coroutine
        raise asyncio.TimeoutError()

    monkeypatch.setattr(asyncio, "wait_for", _mock_wait_for)

    result = await mh._handle_task_tools(
        "generate_handoff",
        {"project_id": p["id"]},
        db,
        str(tmp_path),
        tenant=None,
        _mcp_tenant_id=None,
    )
    assert result["mode"] == "l0_fallback", (
        f"Expected mode='l0_fallback' on timeout, got mode={result['mode']!r}. "
        "Callers cannot detect a degraded handoff if mode stays 'full'."
    )
    assert result.get("degraded") is True, (
        "Expected degraded=True on timeout — the old bug returned degraded=False "
        "(key absent), making a degraded handoff indistinguishable from success."
    )
    # The L0 content must still be present (north star + pinned decisions).
    assert result.get("content"), "L0 fallback content must not be empty"


@pytest.mark.asyncio
async def test_generate_handoff_l0_fallback_capability_contract_reports_stale_non_executable(
    db, tmp_path, monkeypatch
):
    """9c6cac08 (665 follow-up) — the L0 emergency fallback's own capability_
    contract must visibly report board_stale=True / executable=False with a
    'stale_board_snapshot' reason, end-to-end through the REAL timeout
    trigger (mcp/handler.py's _handoff_degraded -> board_stale plumbing),
    not just via a direct build_capability_contract(board_stale=True) unit
    call (already covered in test_capability_contract.py). A degraded
    handoff that silently reported executable=True would be exactly the
    'silently becoming an empty normal handoff' failure mode this item
    guards against."""
    p = await db_module.create_project(db, "stale-e2e-proj")
    await db_module.set_goal(db, p["id"], "do things", sprint="v1")

    async def _mock_wait_for(coro, timeout):
        coro.close()
        raise asyncio.TimeoutError()

    monkeypatch.setattr(asyncio, "wait_for", _mock_wait_for)

    result = await mh._handle_task_tools(
        "generate_handoff",
        {"project_id": p["id"]},
        db,
        str(tmp_path),
        tenant=None,
        _mcp_tenant_id=None,
    )
    assert result["degraded"] is True
    contract = result.get("capability_contract")
    assert contract is not None, "L0 fallback must still emit a capability_contract"
    assert contract["board_stale"] is True
    assert contract["executable"] is False
    assert "stale_board_snapshot" in contract["executable_reasons"]


@pytest.mark.asyncio
async def test_generate_handoff_success_has_degraded_false(db, tmp_path):
    """On a successful handoff, degraded must be False (not absent, not True)."""
    p = await db_module.create_project(db, "success-proj")
    await db_module.set_goal(db, p["id"], "ship it", sprint="v1")

    result = await mh._handle_task_tools(
        "generate_handoff",
        {"project_id": p["id"]},
        db,
        str(tmp_path),
        tenant=None,
        _mcp_tenant_id=None,
    )
    assert result.get("degraded") is False, (
        f"Expected degraded=False on success, got {result.get('degraded')!r}"
    )
    assert result.get("mode") in ("full", "delta", "starter", "planner", "compact"), (
        f"Expected a real mode on success, got {result.get('mode')!r}"
    )


# ---------------------------------------------------------------------------
# PART 2 — default skip_ai_summary=True on the MCP path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_generate_handoff_skips_ai_by_default(db, tmp_path):
    """The MCP tool path must default skip_ai_summary=True so no Haiku calls
    are made unless the caller explicitly passes skip_ai_summary=false."""
    p = await db_module.create_project(db, "skip-ai-proj")
    await db_module.set_goal(db, p["id"], "ship it", sprint="v1")
    s = await db_module.register_session(db, p["id"], "sess-ai")
    for i in range(5):
        await db_module.log_task(db, s["id"], p["id"], f"task {i}", "done")

    ai_calls: list[str] = []

    def _spy_summarizer(prompt: str):
        ai_calls.append(prompt)
        return "SHOULD NOT APPEAR"

    # Patch generate_handoff to inspect which skip_ai_summary value reaches it.
    received_skip: list[bool] = []
    original_generate = handoff_module.generate_handoff

    async def _patched_generate(*args, **kwargs):
        received_skip.append(kwargs.get("skip_ai_summary", None))
        return await original_generate(*args, **kwargs)

    with patch.object(handoff_module, "generate_handoff", _patched_generate):
        await mh._handle_task_tools(
            "generate_handoff",
            {"project_id": p["id"]},  # no skip_ai_summary key → defaults to True
            db,
            str(tmp_path),
            tenant=None,
            _mcp_tenant_id=None,
        )

    assert received_skip, "generate_handoff was never called"
    assert received_skip[0] is True, (
        f"MCP path did not default skip_ai_summary=True — got {received_skip[0]!r}. "
        "This means Haiku calls are made on every handoff unless explicitly opted out."
    )


@pytest.mark.asyncio
async def test_mcp_generate_handoff_respects_explicit_skip_ai_false(db, tmp_path):
    """Callers who pass skip_ai_summary=false must get AI summaries (opt-in works)."""
    p = await db_module.create_project(db, "optin-ai-proj")
    await db_module.set_goal(db, p["id"], "ship it", sprint="v1")

    received_skip: list[bool] = []
    original_generate = handoff_module.generate_handoff

    async def _patched_generate(*args, **kwargs):
        received_skip.append(kwargs.get("skip_ai_summary", None))
        return await original_generate(*args, **kwargs)

    with patch.object(handoff_module, "generate_handoff", _patched_generate):
        await mh._handle_task_tools(
            "generate_handoff",
            {"project_id": p["id"], "skip_ai_summary": False},
            db,
            str(tmp_path),
            tenant=None,
            _mcp_tenant_id=None,
        )

    assert received_skip, "generate_handoff was never called"
    assert received_skip[0] is False, (
        f"Explicit skip_ai_summary=False was not honoured — got {received_skip[0]!r}."
    )


@pytest.mark.asyncio
async def test_mcp_generate_handoff_skip_ai_string_false_coerced(db, tmp_path):
    """String 'false' (from JSON over HTTP) must coerce to Python False correctly."""
    p = await db_module.create_project(db, "coerce-false-proj")
    await db_module.set_goal(db, p["id"], "ship it", sprint="v1")

    received_skip: list[bool] = []
    original_generate = handoff_module.generate_handoff

    async def _patched_generate(*args, **kwargs):
        received_skip.append(kwargs.get("skip_ai_summary", None))
        return await original_generate(*args, **kwargs)

    with patch.object(handoff_module, "generate_handoff", _patched_generate):
        await mh._handle_task_tools(
            "generate_handoff",
            {"project_id": p["id"], "skip_ai_summary": "false"},
            db,
            str(tmp_path),
            tenant=None,
            _mcp_tenant_id=None,
        )

    assert received_skip and received_skip[0] is False, (
        "String 'false' must coerce to Python False"
    )


# ---------------------------------------------------------------------------
# PART 3 — parallel summarize_session fan-out
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_summarize_session_fanout_is_parallel(db, tmp_path):
    """With N sessions, summarize_session must run concurrently, not serially.

    Strategy: inject a slow async summarizer that records when each call STARTS
    and ENDS. If the calls are parallel, the end-time of ANY call should be close
    to the start-time of the first call (all run concurrently). If serial, the
    calls would queue up and total wall time ≈ N × per-call time.

    We assert that all calls START before any call ENDS — the hallmark of
    concurrent execution. For serial execution, call 2 always starts AFTER call 1
    ends, so this assertion would fail for N>=2.
    """
    import time

    p = await db_module.create_project(db, "parallel-summ-proj")
    await db_module.set_goal(db, p["id"], "parallelize", sprint="v1")

    # Create 3 sessions each with enough tasks to be summarised (min_tasks=3).
    session_ids = []
    for i in range(3):
        s = await db_module.register_session(db, p["id"], f"sess-par-{i}")
        for j in range(4):
            await db_module.log_task(db, s["id"], p["id"], f"task s{i} t{j}", "done")
        session_ids.append(s["id"])

    call_starts: list[float] = []
    call_ends: list[float] = []

    async def _slow_summarizer(prompt: str):
        call_starts.append(time.monotonic())
        await asyncio.sleep(0.05)  # 50ms per call — fast enough for tests, slow enough to detect serial
        call_ends.append(time.monotonic())
        # Return a valid summary dict so summarize_session stores it.
        return {
            "session_type": "executor",
            "tasks_completed": 4,
            "key_decisions": [],
            "summary": f"stub summary for {prompt[:20]}",
        }

    await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path),
        summarizer=_slow_summarizer,
        skip_ai_summary=False,  # must be False so AI path runs
        mode="full",
    )

    # The summarizer is called for summarize_session (one per session) AND for
    # _generate_ai_summary (one more call). With 3 sessions that is >= 3 calls total.
    assert len(call_starts) >= 3, (
        f"Expected at least 3 summarize_session calls (one per session), got {len(call_starts)}. "
        "Either the sessions were not summarised or the count changed."
    )

    # Parallelism check on the FIRST 3 calls (the session-summary fan-out batch).
    # If execution is serial: start0 < end0 < start1 < end1 < start2 ...
    # If parallel: all starts cluster near the same time, before any end.
    # We check only the parallel batch (first 3 calls); the ai_summary blurb runs
    # AFTER the gather completes so its timing is separate and not checked here.
    fanout_starts = call_starts[:3]
    fanout_ends = call_ends[:3]
    first_end = min(fanout_ends)
    last_start = max(fanout_starts)
    assert last_start < first_end, (
        "summarize_session fan-out appears to be SERIAL (last call started after "
        f"first call ended: last_start={last_start:.4f}, first_end={first_end:.4f}). "
        "Expected asyncio.gather()-based parallel execution."
    )


@pytest.mark.asyncio
async def test_summarize_session_fanout_skipped_when_ai_disabled(db, tmp_path):
    """When skip_ai_summary=True (the new default), no summarize_session calls
    are made even when there are multiple eligible sessions."""
    p = await db_module.create_project(db, "no-summ-proj")
    await db_module.set_goal(db, p["id"], "skip summaries", sprint="v1")

    # Create sessions with enough tasks.
    for i in range(3):
        s = await db_module.register_session(db, p["id"], f"sess-noskip-{i}")
        for j in range(4):
            await db_module.log_task(db, s["id"], p["id"], f"task {i} {j}", "done")

    calls: list[str] = []

    def _spy(prompt: str):
        calls.append(prompt)
        return "SHOULD NOT BE CALLED"

    await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path),
        summarizer=_spy,
        skip_ai_summary=True,  # the new MCP default
        mode="full",
    )

    # No summarizer calls at all (includes summarize_session AND ai_summary blurb).
    summarize_calls = [p for p in calls if "session" in p.lower() or "task" in p.lower()]
    assert not summarize_calls, (
        f"skip_ai_summary=True must suppress ALL summarizer calls; got {len(calls)}"
    )
