"""Regression test for bc834237.

generate_handoff(mode='delta') iterated over the ENTIRE pending sprint-item
list with full untruncated titles, causing 566K-599K character payloads on
projects with large backlogs (confirmed live). Fix: cap at 20 items, truncate
titles to 150 chars, emit "+N more pending (M high-priority)" summary line.
"""
from __future__ import annotations

import pytest

from meridian import db as db_module
from meridian import handoff as handoff_module
from meridian.handoff import _render_delta_handoff


# ---------------------------------------------------------------------------
# Pure-unit tests against _render_delta_handoff directly
# (no DB, fast, deterministic)
# ---------------------------------------------------------------------------


def _make_item(i: int, title_len: int = 200, priority: str = "normal") -> dict:
    return {
        "id": f"item-{i:04d}",
        "status": "todo",
        "title": f"Item {i} " + ("x" * title_len),
        "priority": priority,
        "possibly_done": False,
    }


def test_delta_pending_cap_limits_output_lines():
    """50 pending items → at most 20 shown + 1 summary line."""
    items = [_make_item(i) for i in range(50)]
    result = _render_delta_handoff(
        {"id": "proj-1", "name": "Test Project"},
        generated_at="2026-07-17 00:00:00",
        completed_items=[],
        in_progress_items=[],
        pending_sprint_items=items,
        quick_start_goal="/goal\nstart_session()",
    )
    pending_lines = [ln for ln in result.splitlines() if ln.startswith("- item-")]
    assert len(pending_lines) == 20, (
        f"Expected exactly 20 pending lines shown, got {len(pending_lines)}"
    )


def test_delta_pending_cap_emits_plus_n_more_line():
    """50 items → '+30 more pending' summary line present and accurate."""
    items = [_make_item(i) for i in range(50)]
    result = _render_delta_handoff(
        {"id": "proj-1", "name": "Test Project"},
        generated_at="2026-07-17 00:00:00",
        completed_items=[],
        in_progress_items=[],
        pending_sprint_items=items,
        quick_start_goal="/goal\nstart_session()",
    )
    assert "+30 more pending" in result, (
        "Expected '+30 more pending' summary line in delta output"
    )


def test_delta_pending_cap_promotes_priority_items_into_shown():
    """A high-priority item ranked LAST in raw order must still be SHOWN.

    Selection is priority-ranked (urgent > high > normal > low), not a raw
    positional truncation — a genuinely important item must never be hidden
    just because of where it happened to fall in dependency-topological order.
    """
    items = [_make_item(i) for i in range(25)]  # 25 normal items
    items.append(_make_item(999, priority="urgent"))  # last in raw order
    result = _render_delta_handoff(
        {"id": "proj-1", "name": "Test Project"},
        generated_at="2026-07-17 00:00:00",
        completed_items=[],
        in_progress_items=[],
        pending_sprint_items=items,
        quick_start_goal="/goal\nstart_session()",
    )
    assert "item-0999" in result, (
        "An urgent item must be promoted into the shown list even though it "
        "was last in raw/dependency order"
    )
    assert "+6 more pending" in result  # 26 total items, cap 20 -> 6 hidden


def test_delta_pending_cap_high_priority_count_in_summary():
    """More high/urgent items than the cap → summary names the overflow count."""
    # 25 urgent + 25 high: only the cap (20) is shown, all ranked ahead of
    # priority, so the hidden tail is entirely urgent/high overflow -- 5
    # urgent and 25 high remain hidden (20 of the 25 urgent are shown first).
    items = [_make_item(i, priority="urgent") for i in range(25)]
    items += [_make_item(100 + i, priority="high") for i in range(25)]
    result = _render_delta_handoff(
        {"id": "proj-1", "name": "Test Project"},
        generated_at="2026-07-17 00:00:00",
        completed_items=[],
        in_progress_items=[],
        pending_sprint_items=items,
        quick_start_goal="/goal\nstart_session()",
    )
    assert "+30 more pending" in result
    assert "(5 urgent, 25 high-priority)" in result, (
        "Expected the hidden-tail urgent/high counts in the '+N more' summary line"
    )


def test_delta_pending_cap_no_high_priority_suffix_when_none_hidden():
    """When all hidden items are normal priority, no '(N high-priority)' note."""
    items = [_make_item(i) for i in range(50)]  # all normal
    result = _render_delta_handoff(
        {"id": "proj-1", "name": "Test Project"},
        generated_at="2026-07-17 00:00:00",
        completed_items=[],
        in_progress_items=[],
        pending_sprint_items=items,
        quick_start_goal="/goal\nstart_session()",
    )
    assert "high-priority" not in result


def test_delta_pending_title_truncated_to_150_chars():
    """Each shown item's title is truncated to at most 150 chars."""
    long_title = "X" * 300
    items = [{"id": "item-long", "status": "todo", "title": long_title, "priority": "normal"}]
    result = _render_delta_handoff(
        {"id": "proj-1", "name": "Test Project"},
        generated_at="2026-07-17 00:00:00",
        completed_items=[],
        in_progress_items=[],
        pending_sprint_items=items,
        quick_start_goal="/goal\nstart_session()",
    )
    # The full 300-char title must NOT appear in the output
    assert long_title not in result
    # But the first 150 chars must appear
    assert long_title[:150] in result


def test_delta_pending_few_items_no_summary_line():
    """Fewer items than the cap → no '+N more' line, all items shown."""
    items = [_make_item(i) for i in range(5)]
    result = _render_delta_handoff(
        {"id": "proj-1", "name": "Test Project"},
        generated_at="2026-07-17 00:00:00",
        completed_items=[],
        in_progress_items=[],
        pending_sprint_items=items,
        quick_start_goal="/goal\nstart_session()",
    )
    assert "more pending" not in result
    # All 5 items present
    for i in range(5):
        assert f"item-{i:04d}" in result


def test_delta_pending_exactly_at_cap_no_summary_line():
    """Exactly 20 items (the cap) → no '+N more' line."""
    items = [_make_item(i) for i in range(20)]
    result = _render_delta_handoff(
        {"id": "proj-1", "name": "Test Project"},
        generated_at="2026-07-17 00:00:00",
        completed_items=[],
        in_progress_items=[],
        pending_sprint_items=items,
        quick_start_goal="/goal\nstart_session()",
    )
    assert "more pending" not in result
    for i in range(20):
        assert f"item-{i:04d}" in result


def test_delta_pending_possibly_done_suffix_preserved_on_shown_items():
    """'possibly done' warning suffix is preserved on shown items."""
    items = [
        {"id": "item-done-ish", "status": "todo", "title": "Maybe done thing",
         "priority": "normal", "possibly_done": True},
    ]
    result = _render_delta_handoff(
        {"id": "proj-1", "name": "Test Project"},
        generated_at="2026-07-17 00:00:00",
        completed_items=[],
        in_progress_items=[],
        pending_sprint_items=items,
        quick_start_goal="/goal\nstart_session()",
    )
    assert "possibly done" in result
    assert "verify before executing" in result


def test_delta_pending_status_prefix_preserved():
    """'[status]' prefix is included for each shown item."""
    items = [{"id": "item-pend", "status": "pending", "title": "A pending item",
              "priority": "normal"}]
    result = _render_delta_handoff(
        {"id": "proj-1", "name": "Test Project"},
        generated_at="2026-07-17 00:00:00",
        completed_items=[],
        in_progress_items=[],
        pending_sprint_items=items,
        quick_start_goal="/goal\nstart_session()",
    )
    assert "[pending]" in result


def test_delta_pending_cap_bounded_character_count():
    """50 items each with 300-char title: total output must be under 20 000 chars.

    Before the fix, 50 × 300-char titles + 50 × ~50 chars overhead = ~17 500 chars
    just for the pending section, but a realistic 100-item backlog with 250-char
    titles would be 25 000+ for pending alone, and the confirmed live payload was
    566K-599K (from the full handoff). This test uses a worst-case delta scenario
    and asserts a firm upper bound of 20 000 chars for the ENTIRE delta output.
    """
    items = [_make_item(i, title_len=300) for i in range(50)]
    result = _render_delta_handoff(
        {"id": "proj-1", "name": "Test Project"},
        generated_at="2026-07-17 00:00:00",
        completed_items=[],
        in_progress_items=[],
        pending_sprint_items=items,
        quick_start_goal="/goal\nstart_session()",
    )
    assert len(result) < 20_000, (
        f"Delta handoff with 50 items exceeded 20 000 chars: {len(result)} chars"
    )


def test_delta_pending_order_preserved():
    """Items are shown in the order passed in (dependency-topological order from
    _prepare_pending_sprint_items is not disturbed by the cap logic)."""
    # Create items in a specific order; the first 20 should appear in that order.
    items = [_make_item(i) for i in range(30)]
    result = _render_delta_handoff(
        {"id": "proj-1", "name": "Test Project"},
        generated_at="2026-07-17 00:00:00",
        completed_items=[],
        in_progress_items=[],
        pending_sprint_items=items,
        quick_start_goal="/goal\nstart_session()",
    )
    # Check that item-0000 through item-0019 appear in order, and item-0020+ don't.
    lines = result.splitlines()
    pending_ids = [ln.split()[1] for ln in lines if ln.startswith("- item-")]
    assert pending_ids == [f"item-{i:04d}" for i in range(20)], (
        "Order of first 20 pending items was not preserved"
    )
    # Items 20-29 (hidden) must not appear as individual lines
    for i in range(20, 30):
        assert f"item-{i:04d}" not in result


def test_delta_no_pending_shows_none():
    """Empty pending list → '- none' line, no '+N more'."""
    result = _render_delta_handoff(
        {"id": "proj-1", "name": "Test Project"},
        generated_at="2026-07-17 00:00:00",
        completed_items=[],
        in_progress_items=[],
        pending_sprint_items=[],
        quick_start_goal="/goal\nstart_session()",
    )
    assert "- none" in result
    assert "more pending" not in result


# ---------------------------------------------------------------------------
# Integration test through generate_handoff (uses DB)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_handoff_delta_large_backlog_is_bounded(db, tmp_path):
    """End-to-end: generate_handoff(mode='delta') with 60 pending items (each
    with a 250-char title) must produce output under 30 000 chars.

    The confirmed-live pathological case was 566K-599K; after the fix a realistic
    worst case (60 items × 250-char titles capped at 20 × 150 chars) should produce
    a delta payload well under 30 000 chars.
    """
    p = await db_module.create_project(db, "delta-cap-integ")
    await db_module.set_goal(db, p["id"], "ship it", sprint="s1")
    s = await db_module.register_session(db, p["id"], "sess-large")
    long_title_base = "A very detailed sprint item with lots of context " * 5  # ~250 chars
    for i in range(60):
        await db_module.add_sprint_item(
            db, p["id"], "v1",
            f"Item {i:03d}: {long_title_base}",
            force=True,
        )

    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path),
        skip_ai_summary=True,
        mode="delta",
        session_id=s["id"],
    )

    assert len(content) < 30_000, (
        f"Delta handoff with 60 large-title pending items was {len(content)} chars "
        f"(expected < 30 000 after bc834237 fix)"
    )
    # The '+N more pending' line must be present
    assert "more pending" in content, (
        "Expected a '+N more pending' summary line in the delta output"
    )
    # Exactly 20 item lines shown
    item_lines = [ln for ln in content.splitlines() if "Item 0" in ln and ln.strip().startswith("-")]
    assert len(item_lines) == 20, (
        f"Expected 20 pending item lines, got {len(item_lines)}"
    )


@pytest.mark.asyncio
async def test_generate_handoff_delta_small_backlog_unaffected(db, tmp_path):
    """End-to-end: generate_handoff(mode='delta') with 5 pending items (below
    the cap) shows all 5 items and emits no '+N more' line."""
    p = await db_module.create_project(db, "delta-cap-small")
    await db_module.set_goal(db, p["id"], "ship it", sprint="s1")
    s = await db_module.register_session(db, p["id"], "sess-small")
    for i in range(5):
        await db_module.add_sprint_item(db, p["id"], "v1", f"Small item {i}", force=True)

    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path),
        skip_ai_summary=True,
        mode="delta",
        session_id=s["id"],
    )

    assert "more pending" not in content
    for i in range(5):
        assert f"Small item {i}" in content
