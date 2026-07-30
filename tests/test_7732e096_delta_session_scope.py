"""Regression tests for 7732e096.

generate_handoff(mode='delta') dumped the whole project instead of a delta:

  a) since_ts found no prior handoff row on a session's genuinely FIRST delta
     call and fell back to None, which _completed_after() treats as "no lower
     bound" -- completed_items then included EVERY historical done/skipped/
     failed/pushed item in the project (confirmed live: 496KB+, reproduced
     worse at 577KB). Fix: fall back to the session's own `created_at` (start
     time) instead of None, so a first delta call scopes to "since I started"
     rather than "since forever".
  b) completed_items had ZERO cap in _render_delta_handoff, unlike pending
     (already capped at 20 by bc834237). Fix: mirror the exact same cap/
     truncation/"+N more" pattern for completed_items.
  c) The session-span footer (compute_session_span, 302db181) was built from
     project-wide `tasks` / `sessions` (get_tasks/get_sessions have no
     session_id filter) even in delta mode, so a session open for minutes
     could report a footer spanning the project's entire history. Fix: scope
     the footer to this session's own task_log rows + its own created_at/
     last_seen when mode='delta'.
"""
from __future__ import annotations

import asyncio

import pytest

from meridian import db as db_module
from meridian import handoff as handoff_module
from meridian.handoff import _render_delta_handoff


# ---------------------------------------------------------------------------
# (a) since_ts fallback: first-ever delta call for a session must scope to
# that session's start, not dump the whole project's completed-item history.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_delta_call_excludes_history_from_before_session_started(db, tmp_path):
    """A long-lived project with a big completed-item history, followed by a
    BRAND NEW session's first-ever delta call, must not see the pre-existing
    history -- only items completed at/after this session's own start."""
    p = await db_module.create_project(db, "delta-scope-history")
    await db_module.set_goal(db, p["id"], "ship it", sprint="s1")

    # Simulate a project with a long history of completed work, all finished
    # well before the new session below even exists.
    for i in range(15):
        old = await db_module.add_sprint_item(
            db, p["id"], "v1", f"Old historical item {i:03d}", force=True
        )
        await db_module.complete_sprint_item(db, p["id"], old["id"])

    # A real gap so the new session's created_at is unambiguously after the
    # old items' completed_at (both are second-resolution timestamps).
    await asyncio.sleep(1.1)

    s = await db_module.register_session(db, p["id"], "sess-brand-new")

    await asyncio.sleep(1.1)
    new_item = await db_module.add_sprint_item(
        db, p["id"], "v1", "freshly shipped after session start", force=True
    )
    await db_module.complete_sprint_item(db, p["id"], new_item["id"])

    # First-ever delta call for this session: no in-memory state, no durable
    # prior handoff row for session s.
    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="delta",
        session_id=s["id"],
    )

    assert "freshly shipped after session start" in content
    for i in range(15):
        assert f"Old historical item {i:03d}" not in content, (
            "first delta call for a new session leaked pre-session project "
            "history into 'Completed since last handoff'"
        )
    # Sanity: the payload should be small, not hundreds of KB.
    assert len(content) < 10_000


@pytest.mark.asyncio
async def test_first_delta_call_with_no_session_id_still_shows_everything(db, tmp_path):
    """When there is genuinely no session context (session_id=None), there is
    no session start to scope by -- unchanged legacy behavior: show everything
    (there is no better bound available in that case)."""
    p = await db_module.create_project(db, "delta-scope-no-session")
    await db_module.set_goal(db, p["id"], "ship it", sprint="s1")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "only shipped thing", force=True)
    await db_module.complete_sprint_item(db, p["id"], item["id"])

    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="delta",
        session_id=None,
    )
    assert "only shipped thing" in content


# ---------------------------------------------------------------------------
# (b) completed_items cap -- unit tests directly against _render_delta_handoff,
# mirroring tests/test_bc834237_delta_pending_cap.py's pattern for pending.
# ---------------------------------------------------------------------------


def _make_completed(i: int, title_len: int = 200) -> dict:
    return {
        "id": f"done-{i:04d}",
        "status": "done",
        "title": f"Completed {i} " + ("x" * title_len),
        "completed_at": "2026-07-17 00:00:00",
    }


def test_delta_completed_cap_limits_output_lines():
    """30 completed items -> at most 20 shown + 1 summary line."""
    items = [_make_completed(i) for i in range(30)]
    result = _render_delta_handoff(
        {"id": "proj-1", "name": "Test Project"},
        generated_at="2026-07-17 00:00:00",
        completed_items=items,
        in_progress_items=[],
        pending_sprint_items=[],
        quick_start_goal="/goal\nstart_session()",
    )
    completed_lines = [ln for ln in result.splitlines() if ln.startswith("- done-")]
    assert len(completed_lines) == 20, (
        f"Expected exactly 20 completed lines shown, got {len(completed_lines)}"
    )
    assert "+10 more completed" in result


def test_delta_completed_title_truncated_to_150_chars():
    long_title = "Y" * 300
    items = [{"id": "done-long", "status": "done", "title": long_title,
              "completed_at": "2026-07-17 00:00:00"}]
    result = _render_delta_handoff(
        {"id": "proj-1", "name": "Test Project"},
        generated_at="2026-07-17 00:00:00",
        completed_items=items,
        in_progress_items=[],
        pending_sprint_items=[],
        quick_start_goal="/goal\nstart_session()",
    )
    assert long_title not in result
    assert long_title[:150] in result


def test_delta_completed_few_items_no_summary_line():
    items = [_make_completed(i) for i in range(3)]
    result = _render_delta_handoff(
        {"id": "proj-1", "name": "Test Project"},
        generated_at="2026-07-17 00:00:00",
        completed_items=items,
        in_progress_items=[],
        pending_sprint_items=[],
        quick_start_goal="/goal\nstart_session()",
    )
    assert "more completed" not in result
    for i in range(3):
        assert f"done-{i:04d}" in result


def test_delta_completed_cap_bounded_character_count():
    """50 completed items x 300-char titles: whole delta output stays small,
    matching the same firm bound bc834237 established for pending."""
    items = [_make_completed(i, title_len=300) for i in range(50)]
    result = _render_delta_handoff(
        {"id": "proj-1", "name": "Test Project"},
        generated_at="2026-07-17 00:00:00",
        completed_items=items,
        in_progress_items=[],
        pending_sprint_items=[],
        quick_start_goal="/goal\nstart_session()",
    )
    assert len(result) < 20_000, (
        f"Delta handoff with 50 completed items exceeded 20 000 chars: {len(result)} chars"
    )


@pytest.mark.asyncio
async def test_generate_handoff_delta_large_completed_history_is_bounded(db, tmp_path):
    """End-to-end: many completed items within the session's own window still
    get capped -- the completed_items cap is an independent bound from the
    since_ts scoping fix, for e.g. a long-lived session with lots of throughput."""
    p = await db_module.create_project(db, "delta-completed-cap-integ")
    await db_module.set_goal(db, p["id"], "ship it", sprint="s1")
    s = await db_module.register_session(db, p["id"], "sess-prolific")

    for i in range(30):
        it = await db_module.add_sprint_item(
            db, p["id"], "v1", f"Prolific item {i:03d}", force=True
        )
        await db_module.complete_sprint_item(db, p["id"], it["id"])

    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="delta",
        session_id=s["id"],
    )
    completed_lines = [
        ln for ln in content.splitlines()
        if ln.strip().startswith("-") and "Prolific item" in ln
    ]
    assert len(completed_lines) == 20
    assert "more completed" in content


# ---------------------------------------------------------------------------
# (c) session-span footer must be scoped to the current session in delta mode.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delta_session_span_footer_excludes_other_sessions_activity(db, tmp_path):
    """A project with an OLDER session's activity, followed by a brand new
    session logging its own single task: the delta footer's span must reflect
    only the new session's activity, not stretch back to the older session."""
    p = await db_module.create_project(db, "delta-span-scope")
    await db_module.set_goal(db, p["id"], "ship it", sprint="s1")

    old_session = await db_module.register_session(db, p["id"], "sess-old")
    await db_module.log_task(
        db, old_session["id"], p["id"], "old session did something", status="done",
    )
    await asyncio.sleep(1.1)

    new_session = await db_module.register_session(db, p["id"], "sess-new")
    await db_module.log_task(
        db, new_session["id"], p["id"], "new session did something", status="done",
    )

    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="delta",
        session_id=new_session["id"],
    )

    assert "## Session span" in content
    span_section = content.split("## Session span", 1)[1]
    first_line = [
        ln for ln in span_section.splitlines() if ln.startswith("- First activity:")
    ][0]
    old_ts = (old_session.get("created_at") or "").replace("T", " ")[:19]
    # The reported "First activity" must not be the OLD session's creation
    # time -- it must be at/after the new session's own created_at.
    assert old_ts not in first_line, (
        "delta mode's session-span footer leaked another session's earlier "
        "activity instead of scoping to the current session"
    )


@pytest.mark.asyncio
async def test_full_mode_session_span_footer_still_project_wide(db, tmp_path):
    """mode='full' is a whole-project state dump by design -- its session-span
    footer must remain project-wide (unaffected by the delta-only scoping fix)."""
    p = await db_module.create_project(db, "full-span-unscoped")
    await db_module.set_goal(db, p["id"], "ship it", sprint="s1")

    old_session = await db_module.register_session(db, p["id"], "sess-old-full")
    await db_module.log_task(
        db, old_session["id"], p["id"], "old session activity", status="done",
    )
    await asyncio.sleep(1.1)

    new_session = await db_module.register_session(db, p["id"], "sess-new-full")
    await db_module.log_task(
        db, new_session["id"], p["id"], "new session activity", status="done",
    )

    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="full",
        session_id=new_session["id"],
    )

    assert "## Session span" in content
    span_section = content.split("## Session span", 1)[1]
    first_line = [
        ln for ln in span_section.splitlines() if ln.startswith("- First activity:")
    ][0]
    old_ts = (old_session.get("created_at") or "").replace("T", " ")[:19]
    assert old_ts in first_line, (
        "mode='full' session-span footer unexpectedly got scoped away from "
        "project-wide activity"
    )


# ---------------------------------------------------------------------------
# 8a883f60 — the dd19b6a4 freshness re-query (this file's own subject: full/
# delta re-check pending items + sessions right before finalizing the /goal,
# so a claim that lands mid-generation isn't handed out stale) now reports an
# explicit evidence_status outcome instead of degrading silently on failure.
# ---------------------------------------------------------------------------


def _fail_after_n_calls(original, n):
    """Return an async wrapper around `original` that behaves normally for
    the first `n` calls, then raises on every call after that — used to make
    ONLY generate_handoff's dd19b6a4 re-query (its SECOND get_sprint_items
    call) fail, while leaving the function's first, unguarded
    `sprint_items_all = await db_module.get_sprint_items(...)` call
    untouched (that one isn't wrapped in a try/except at all, so failing it
    would crash the whole handoff rather than exercising the freshness gate)."""
    calls = {"n": 0}

    async def _wrapped(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] > n:
            raise RuntimeError("sprint_items table temporarily unavailable")
        return await original(*args, **kwargs)

    return _wrapped


@pytest.mark.asyncio
async def test_delta_freshness_requery_verified_in_happy_path(db, tmp_path):
    """No injected failure -> freshness_requery reports verified with an
    explicit reason, on the exact mode (delta) this file's dd19b6a4 fix
    targets."""
    p = await db_module.create_project(db, "evidence-freshness-happy")
    s = await db_module.register_session(db, p["id"], "sess-freshness-happy")
    await db_module.add_sprint_item(db, p["id"], "v1", "some pending item")

    evidence_status: dict = {}
    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="delta",
        session_id=s["id"], evidence_status=evidence_status,
    )
    assert content
    assert evidence_status["freshness_requery"]["status"] == "verified"


@pytest.mark.asyncio
async def test_delta_freshness_requery_failure_reported_not_silently_degraded(
    db, tmp_path, monkeypatch,
):
    """A failure in the SECOND get_sprint_items call (the actual dd19b6a4
    re-query, not the function's initial fetch) must surface as an explicit
    failed evidence_status entry — previously this was a bare
    `except Exception: pass`, indistinguishable from success."""
    p = await db_module.create_project(db, "evidence-freshness-blowup")
    s = await db_module.register_session(db, p["id"], "sess-freshness-blowup")
    await db_module.add_sprint_item(db, p["id"], "v1", "some pending item")

    original = db_module.get_sprint_items
    monkeypatch.setattr(
        db_module, "get_sprint_items", _fail_after_n_calls(original, 1),
    )

    evidence_status: dict = {}
    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="delta",
        session_id=s["id"], evidence_status=evidence_status,
    )
    # The mandatory handoff still rendered (degrade, don't break) ...
    assert content
    # ... but the freshness re-query failure is now explicit and specific.
    fr = evidence_status["freshness_requery"]
    assert fr["status"] == "failed"
    assert "sprint_items table temporarily unavailable" in fr["reason"]
    assert fr["fallback"]


@pytest.mark.asyncio
async def test_delta_freshness_requery_strict_evidence_blocks_on_failure(
    db, tmp_path, monkeypatch,
):
    """Same broken re-query, but with strict_evidence=True: fail CLOSED —
    nothing rendered/written/persisted for this call — rather than handing
    back the same plausible-looking delta the non-strict test above got."""
    p = await db_module.create_project(db, "evidence-freshness-strict")
    s = await db_module.register_session(db, p["id"], "sess-freshness-strict")
    await db_module.add_sprint_item(db, p["id"], "v1", "some pending item")

    original = db_module.get_sprint_items
    monkeypatch.setattr(
        db_module, "get_sprint_items", _fail_after_n_calls(original, 1),
    )

    out_dir = tmp_path / "strict-freshness"
    out_dir.mkdir()
    with pytest.raises(handoff_module.HandoffEvidenceRequired) as excinfo:
        await handoff_module.generate_handoff(
            db, p["id"], str(out_dir), skip_ai_summary=True, mode="delta",
            session_id=s["id"], strict_evidence=True,
        )
    assert any(e["capability"] == "freshness_requery" for e in excinfo.value.errors)
    assert list(out_dir.iterdir()) == []
