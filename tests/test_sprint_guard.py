"""c0d2356d — Stop-hook sprint guard: pending-count helper + endpoint + the
generate_handoff hook-writer."""
from __future__ import annotations

import asyncio
from pathlib import Path

from meridian import db as db_module
from meridian import handoff as handoff_module


def test_count_pending_sprint_items():
    async def _run():
        db = await db_module.init_db(":memory:")
        p = await db_module.create_project(db, "sg-count")
        await db_module.add_sprint_item(db, p["id"], "v1", "one")
        i2 = await db_module.add_sprint_item(db, p["id"], "v1", "two")
        c_both = await db_module.count_pending_sprint_items(db, p["id"])
        await db_module.complete_sprint_item(db, p["id"], i2["id"])
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
    # This is the existing explicit-root test-isolation path — unchanged by
    # the 34e94e0a cross-project-contamination fix.
    async def _run():
        db = await db_module.init_db(":memory:")
        await handoff_module._write_sprint_guard_hooks(db, "proj-xyz-123", root=tmp_path)

    asyncio.run(_run())
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
    async def _run():
        db = await db_module.init_db(":memory:")
        await handoff_module._write_sprint_guard_hooks(db, "proj-should-not-write")

    asyncio.run(_run())  # no exception, no write


def test_write_sprint_guard_hooks_uses_executor_config_repo_path(tmp_path, monkeypatch):
    # 34e94e0a — the production (root=None) path must resolve the write
    # target from the CALLING PROJECT's own executor_config.repo_path, not
    # from the server's own install directory. Simulate "production" by
    # removing PYTEST_CURRENT_TEST (which otherwise always short-circuits the
    # root=None path during the test suite).
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    repo_dir = tmp_path / "myrepo"
    (repo_dir / ".claude").mkdir(parents=True)

    async def _run():
        db = await db_module.init_db(":memory:")
        p = await db_module.create_project(db, "sg-repo-path")
        await db_module.set_executor_config(db, p["id"], {"repo_path": str(repo_dir)})
        await handoff_module._write_sprint_guard_hooks(db, p["id"])
        return p["id"]

    project_id = asyncio.run(_run())
    sh = (repo_dir / ".claude" / "hooks" / "sprint_guard.sh").read_text(encoding="utf-8")
    ps1 = (repo_dir / ".claude" / "hooks" / "sprint_guard.ps1").read_text(encoding="utf-8")
    assert project_id in sh
    assert project_id in ps1


def test_write_sprint_guard_hooks_skips_without_repo_path_no_cross_project_leak(monkeypatch):
    # 34e94e0a regression guard: a project with NO configured repo_path must
    # never fall back to writing into the server's own checkout
    # (Path(__file__).parent.parent) — that fallback is exactly how one
    # project's generate_handoff() clobbered a totally different project's
    # committed sprint_guard hooks with a foreign project_id.
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    real_hooks_dir = Path(handoff_module.__file__).parent.parent / ".claude" / "hooks"
    sh_path = real_hooks_dir / "sprint_guard.sh"
    before = sh_path.read_text(encoding="utf-8") if sh_path.exists() else None

    async def _run():
        db = await db_module.init_db(":memory:")
        p = await db_module.create_project(db, "sg-no-repo-path")
        await handoff_module._write_sprint_guard_hooks(db, p["id"])
        return p["id"]

    project_id = asyncio.run(_run())

    after = sh_path.read_text(encoding="utf-8") if sh_path.exists() else None
    assert before == after                 # server's own hooks left untouched
    if after is not None:
        assert project_id not in after     # foreign project_id never leaked in


# ---------------------------------------------------------------------------
# b4ce3274 — bounded stop-override retry ceiling on /sprint/pending_count.
# ---------------------------------------------------------------------------

from meridian.routes import sprint as sprint_routes  # noqa: E402


def _seed_project_with_pending(client, name):
    db = client.app.state.db
    p = asyncio.run(db_module.create_project(db, name))
    asyncio.run(db_module.add_sprint_item(db, p["id"], "v1", "todo one"))
    return p["id"]


def _reset_ceiling_state(monkeypatch, ceiling):
    """Clear the module's per-session counters and pin the ceiling env var."""
    with sprint_routes._stop_override_lock:
        sprint_routes._stop_override_counts.clear()
    monkeypatch.setenv("MERIDIAN_STOP_OVERRIDE_CEILING", str(ceiling))


def test_stop_override_below_ceiling_forces_continuation(client, monkeypatch):
    # Below N, every call still reports pending>0 so the guard keeps blocking
    # (byte-for-byte the pre-b4ce3274 behaviour) and flags stopped_at_ceiling False.
    _reset_ceiling_state(monkeypatch, ceiling=3)
    pid = _seed_project_with_pending(client, "ceiling-below")
    sid = "sess-below-1"

    for expected_count in (1, 2, 3):
        r = client.get(
            f"/projects/{pid}/sprint/pending_count", params={"session_id": sid}
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["pending_count"] == 1               # still blocks (exit 2)
        assert body["stopped_at_ceiling"] is False
        assert body["stop_override_count"] == expected_count
        assert body["stop_override_ceiling"] == 3


def test_stop_override_at_ceiling_allows_stop_and_flags(client, monkeypatch):
    # The 4th consult (count would exceed 3) flips to allowing the stop: the
    # reported pending_count clamps to 0 (guard exits 0) and stopped_at_ceiling
    # is set with a reason so a delta handoff can be produced.
    _reset_ceiling_state(monkeypatch, ceiling=3)
    pid = _seed_project_with_pending(client, "ceiling-at")
    sid = "sess-at-1"

    # Burn the 3-call budget (each still blocks).
    for _ in range(3):
        r = client.get(
            f"/projects/{pid}/sprint/pending_count", params={"session_id": sid}
        )
        assert r.json()["pending_count"] == 1

    # 4th call: ceiling reached.
    r = client.get(
        f"/projects/{pid}/sprint/pending_count", params={"session_id": sid}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pending_count"] == 0                    # guard now exits 0 (allows stop)
    assert body["stopped_at_ceiling"] is True
    assert body["actual_pending_count"] == 1             # real pending still surfaced
    assert body["stop_override_count"] == 3
    assert body["stop_override_ceiling"] == 3
    assert "ceiling" in body["reason"].lower()

    # Idempotent once hit — repeated consults keep allowing the stop.
    r2 = client.get(
        f"/projects/{pid}/sprint/pending_count", params={"session_id": sid}
    )
    b2 = r2.json()
    assert b2["pending_count"] == 0
    assert b2["stopped_at_ceiling"] is True
    assert b2["stop_override_count"] == 3                 # not incremented past the ceiling


def test_stop_override_counter_is_per_session(client, monkeypatch):
    # One session hitting the ceiling must not affect another session on the
    # SAME project — the budget is keyed per session_id.
    _reset_ceiling_state(monkeypatch, ceiling=3)
    pid = _seed_project_with_pending(client, "ceiling-per-session")
    hot, cold = "sess-hot", "sess-cold"

    # Exhaust the hot session's budget (3 blocks) then push it over the ceiling.
    for _ in range(3):
        client.get(f"/projects/{pid}/sprint/pending_count", params={"session_id": hot})
    r_hot = client.get(f"/projects/{pid}/sprint/pending_count", params={"session_id": hot})
    assert r_hot.json()["stopped_at_ceiling"] is True
    assert r_hot.json()["pending_count"] == 0

    # The cold session on the same project is unaffected — first consult still
    # blocks (pending>0) and is nowhere near its own ceiling.
    r_cold = client.get(f"/projects/{pid}/sprint/pending_count", params={"session_id": cold})
    cold_body = r_cold.json()
    assert cold_body["pending_count"] == 1
    assert cold_body["stopped_at_ceiling"] is False
    assert cold_body["stop_override_count"] == 1


def test_stop_override_reset_when_no_pending(client, monkeypatch):
    # Once all items are done, pending_count is 0 and the per-session budget is
    # cleared so a session id reused for later work starts fresh.
    _reset_ceiling_state(monkeypatch, ceiling=3)
    db = client.app.state.db
    p = asyncio.run(db_module.create_project(db, "ceiling-reset"))
    item = asyncio.run(db_module.add_sprint_item(db, p["id"], "v1", "todo one"))
    pid, sid = p["id"], "sess-reset"

    # Consume some budget while it's pending.
    client.get(f"/projects/{pid}/sprint/pending_count", params={"session_id": sid})
    client.get(f"/projects/{pid}/sprint/pending_count", params={"session_id": sid})
    assert sprint_routes._stop_override_counts.get(sid) == 2

    # Complete the only item → pending 0 → counter reset.
    asyncio.run(db_module.complete_sprint_item(db, pid, item["id"]))
    r = client.get(f"/projects/{pid}/sprint/pending_count", params={"session_id": sid})
    assert r.json()["pending_count"] == 0
    assert sid not in sprint_routes._stop_override_counts


def test_stop_override_ceiling_env_default_and_override(monkeypatch):
    # Default is 3; a valid positive env value wins; junk / non-positive values
    # fall back to the default rather than disabling the guard.
    monkeypatch.delenv("MERIDIAN_STOP_OVERRIDE_CEILING", raising=False)
    assert sprint_routes._stop_override_ceiling() == 3
    monkeypatch.setenv("MERIDIAN_STOP_OVERRIDE_CEILING", "5")
    assert sprint_routes._stop_override_ceiling() == 5
    monkeypatch.setenv("MERIDIAN_STOP_OVERRIDE_CEILING", "0")
    assert sprint_routes._stop_override_ceiling() == 3   # non-positive → default
    monkeypatch.setenv("MERIDIAN_STOP_OVERRIDE_CEILING", "nope")
    assert sprint_routes._stop_override_ceiling() == 3   # unparseable → default
