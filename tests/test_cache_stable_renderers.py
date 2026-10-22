"""Cache-stable renderers (sprint 9cbaf379, W1: session_brief renderer).

The SessionStart brief and the get_session_brief payload put every static fact
before ``sb.VOLATILE_BOUNDARY`` and every clock or live-board value after it.
These tests pin that split: the static prefix is byte-identical across a clock
change and a board insertion-order change, and no timestamp or live counter
appears before the marker.
"""

from __future__ import annotations

import time

import meridian.server  # noqa: F401 -- import the server first, the handler depends on it
from meridian import db as db_module
from meridian import session_brief as sb
from meridian.mcp.handlers import session_tools as st_mod

PID = "5787cc92-ba7d-4788-b17c-28ab7938b839"
NOW = 1790942400
LATER = NOW + 3 * 86400 + 600


def _stamp(epoch: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime(epoch))


def _fetch(*args, **kwargs):
    return "Next pending: [abcd0001] first board item\nNext pending: [abcd0002] second board item"


def _session_start(tmp_path, now: int) -> str:
    env = {
        "MERIDIAN_PROJECT_ID": PID,
        "MERIDIAN_URL": "http://127.0.0.1:7878",
        "CLAUDE_PROJECT_DIR": str(tmp_path),
        "LOCALAPPDATA": str(tmp_path),
    }
    result = sb.build_brief(
        {"hook_event_name": "SessionStart", "session_id": "host-session-1", "cwd": str(tmp_path)},
        event="SessionStart", env=env, fs=sb.RealFS(), now=now, budget_s=2.0,
        snapshot=None, fetch_fn=_fetch,
    )
    return result["context"]


def test_session_start_static_prefix_ignores_the_clock(tmp_path):
    early = _session_start(tmp_path, NOW)
    late = _session_start(tmp_path, LATER)
    static_early, marker, volatile_early = early.partition(sb.VOLATILE_BOUNDARY)
    static_late, _, volatile_late = late.partition(sb.VOLATILE_BOUNDARY)
    assert marker and early.count(sb.VOLATILE_BOUNDARY) == 1
    assert static_early == static_late
    assert _stamp(NOW) not in static_early and _stamp(LATER) not in static_late
    assert "computed on this machine at" not in static_early
    assert volatile_early != volatile_late  # the computed-at line is the part that moves
    assert early.splitlines()[-1] == f"Computed at {_stamp(NOW)}."
    assert late.splitlines()[-1] == f"Computed at {_stamp(LATER)}."


def test_session_start_puts_clock_and_live_lines_after_the_boundary(tmp_path):
    lines = _session_start(tmp_path, NOW).splitlines()
    cut = lines.index(sb.VOLATILE_BOUNDARY)
    assert lines[0].startswith("[Meridian guard brief] Source: static guard rules plus facts computed on this machine")
    before = lines[:cut]
    assert not any(line.startswith(("Code intel:", "Guard audit", sb._SERVER_LABEL)) for line in before)
    assert lines[cut + 1].startswith("Code intel:")  # clock-derived index age, after the marker
    assert sb._SERVER_LABEL in lines[cut:]
    assert lines[-1].startswith("Computed at ")


def test_compose_keeps_audit_and_code_age_after_the_boundary():
    def render(ts: str, age: int) -> str:
        return sb.compose_session_brief(
            ts=ts,
            code_line=f"Code intel: /repo = codebase-memory project 'repo' (last activity {age} days ago, fresh).",
            pid_line=f"Meridian project_id: {PID} (from env). " + sb._ORIENT,
            guard_line=sb._guard_line("advisory", set()),
            audit_line="Guard audit, previous session abc: 2 fail-opens, 1 deny.",
            server_lines=["Next pending: [abcd0001] first board item"],
        )

    early = render("2026-10-01T00:00Z", 3)
    late = render("2026-10-04T00:10Z", 6)
    static_early = early.partition(sb.VOLATILE_BOUNDARY)[0]
    assert static_early == late.partition(sb.VOLATILE_BOUNDARY)[0]
    assert "fail-opens" not in static_early and "days ago" not in static_early
    assert "fail-opens" in early and "days ago" in early


async def test_get_session_brief_static_prefix_ignores_board_insertion_order(db, tmp_path, monkeypatch):
    project = await db_module.create_project(db, "cache-stable-renderers")
    pid = project["id"]
    await db_module.add_sprint_item(db, pid, "v0.9.0", "Quartz rebuild the glyph cache", priority="normal")
    await db_module.add_sprint_item(db, pid, "v0.9.0", "Harbor pilot the ferry schedule", priority="urgent")
    await db_module.add_sprint_item(db, pid, "v0.9.0", "Lantern audit the wiring diagram", priority="low")
    real_get_sprint_items = db_module.get_sprint_items

    async def as_returned(*args, **kwargs):
        return await real_get_sprint_items(*args, **kwargs)

    async def reversed_rows(*args, **kwargs):
        return list(reversed(await real_get_sprint_items(*args, **kwargs)))

    args = {"project_id": pid}
    monkeypatch.setattr(db_module, "get_sprint_items", as_returned)
    first = await st_mod.handle_get_session_brief(args, db, str(tmp_path), None, None)
    monkeypatch.setattr(db_module, "get_sprint_items", reversed_rows)
    second = await st_mod.handle_get_session_brief(args, db, str(tmp_path), None, None)

    static_first, marker, volatile_first = first["text"].partition(sb.VOLATILE_BOUNDARY)
    static_second = second["text"].partition(sb.VOLATILE_BOUNDARY)[0]
    assert marker
    assert static_first == static_second
    assert first["text"] == second["text"]  # the volatile order is stable as well
    for live_tag in ("<progress", "<pending_items", "<last_tasks"):
        assert live_tag not in static_first
        assert live_tag in volatile_first


def test_stable_item_order_ignores_input_order():
    rows = [
        {"id": "b", "priority": "normal", "added_at": "2026-10-01 00:00:00"},
        {"id": "a", "priority": "urgent", "added_at": "2026-10-03 00:00:00"},
        {"id": "c", "priority": None, "added_at": "2026-10-01 00:00:00"},
        {"id": "d", "priority": "low", "added_at": "2026-09-01 00:00:00"},
    ]
    expected = [row["id"] for row in sb.stable_item_order(rows)]
    assert expected == ["a", "b", "c", "d"]  # priority, then oldest first, then id
    assert [row["id"] for row in sb.stable_item_order(reversed(rows))] == expected
