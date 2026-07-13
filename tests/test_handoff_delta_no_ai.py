"""Regression test for 4c7cd788 — generate_handoff(mode='delta') must be AI-free.

Two independent sessions saw generate_handoff(mode='delta', session_id=...) hang
~4 minutes (client timeout) while a lighter call stayed instant. Root cause: the
delta path ran up to three network Haiku seams (session-summary fan-out, the
ai_summary blurb, the sprint retrospective) whose output the delta template never
uses — a slow/unreachable summarizer stalled the whole call. The delta path is now
fully AI-free; this pins that so the hang can't regress.
"""
import pytest

from meridian import db as db_module
from meridian import handoff as handoff_module


@pytest.mark.asyncio
async def test_delta_handoff_invokes_no_summarizer(db, tmp_path):
    p = await db_module.create_project(db, "delta-noai")
    await db_module.set_goal(db, p["id"], "ship it", sprint="s1")
    s = await db_module.register_session(db, p["id"], "sess-delta")
    for i in range(4):
        await db_module.log_task(db, s["id"], p["id"], f"did thing {i}", "done")
    done = await db_module.add_sprint_item(db, p["id"], "v1", "a completed thing")
    await db_module.complete_sprint_item(db, p["id"], done["id"])

    calls: list[str] = []

    def _spy(prompt):
        calls.append(prompt)
        return "AI OUTPUT SHOULD NOT APPEAR"

    # Delta mode with skip_ai_summary=False: NONE of the AI seams may run.
    _, content = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), summarizer=_spy, skip_ai_summary=False,
        mode="delta", session_id="sess-delta",
    )
    assert calls == [], f"delta handoff invoked the summarizer {len(calls)}x"
    assert "AI OUTPUT SHOULD NOT APPEAR" not in content
    # The delta content itself is still produced.
    assert "Session Update" in content or "Completed since last handoff:" in content


@pytest.mark.asyncio
async def test_full_handoff_still_invokes_summarizer(db, tmp_path):
    """Sanity: the same spy IS used by a full handoff — proving delta's silence is
    the mode gate, not a broken spy or an unreachable seam."""
    p = await db_module.create_project(db, "full-ai")
    await db_module.set_goal(db, p["id"], "ship it", sprint="s1")
    s = await db_module.register_session(db, p["id"], "sess-full")
    await db_module.log_task(db, s["id"], p["id"], "did real work", "done")

    calls: list[str] = []

    def _spy(prompt):
        calls.append(prompt)
        return "STUB SUMMARY"

    _, content = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), summarizer=_spy, skip_ai_summary=False,
        mode="full",
    )
    assert calls, "full handoff should have invoked the summarizer at least once"
    assert "STUB SUMMARY" in content
