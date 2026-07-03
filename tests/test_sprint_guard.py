"""c0d2356d — Stop-hook sprint guard: pending-count helper + endpoint + the
generate_handoff hook-writer."""
from __future__ import annotations

import asyncio

from meridian import db as db_module
from meridian import handoff as handoff_module


def test_count_pending_sprint_items():
    async def _run():
        db = await db_module.init_db(":memory:")
        p = await db_module.create_project(db, "sg-count")
        await db_module.add_sprint_item(db, p["id"], "v1", "one")
        i2 = await db_module.add_sprint_item(db, p["id"], "v1", "two")
        c_both = await db_module.count_pending_sprint_items(db, p["id"])
        await db_module.patch_sprint_item(db, p["id"], i2["id"], status="done")
        c_one = await db_module.count_pending_sprint_items(db, p["id"])
        c_missing = await db_module.count_pending_sprint_items(db, "no-such-project")
        return c_both, c_one, c_missing

    c_both, c_one, c_missing = asyncio.run(_run())
    assert c_both == 2
    assert c_one == 1          # a 'done' item isn't counted
    assert c_missing == 0


def test_sprint_pending_count_endpoint(client):
    db = client.app.state.db
    p = asyncio.run(db_module.create_project(db, "sg-endpoint"))
    asyncio.run(db_module.add_sprint_item(db, p["id"], "v1", "todo one"))

    r = client.get(f"/projects/{p['id']}/sprint/pending_count")
    assert r.status_code == 200, r.text
    assert r.json()["pending_count"] == 1

    r404 = client.get("/projects/no-such-project/sprint/pending_count")
    assert r404.status_code == 404


def test_write_sprint_guard_hooks_bakes_project_id(tmp_path):
    # root= makes it write to an isolated dir (real runs skip under pytest).
    handoff_module._write_sprint_guard_hooks("proj-xyz-123", root=tmp_path)
    sh = (tmp_path / ".claude" / "hooks" / "sprint_guard.sh").read_text(encoding="utf-8")
    ps1 = (tmp_path / ".claude" / "hooks" / "sprint_guard.ps1").read_text(encoding="utf-8")
    for text in (sh, ps1):
        assert "proj-xyz-123" in text                 # PROJECT_ID baked in
        assert "stop_hook_active" in text             # infinite-loop guard
        assert "pending_count" in text                # hits the endpoint
        assert "__PROJECT_ID__" not in text           # placeholders replaced
        assert "__URL__" not in text
    assert "exit 2" in sh                             # blocks the stop when pending>0


def test_write_sprint_guard_hooks_skipped_under_pytest_without_root():
    # Without an explicit root, the auto-writer no-ops under pytest so it can
    # never dirty the committed .claude/hooks during the suite.
    handoff_module._write_sprint_guard_hooks("proj-should-not-write")  # no exception, no write
