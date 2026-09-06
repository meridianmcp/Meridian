"""c0d2356d — Stop-hook sprint guard: pending-count helper + endpoint + the
generate_handoff hook-writer."""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

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


# ---------------------------------------------------------------------------
# 273287cb — user-creatable hooks: generalizes past sprint_guard.sh/.ps1 (the
# only hook Meridian auto-writes) so a project can define its own arbitrary
# PreToolUse/PostToolUse/Stop hooks, injected the same way by generate_handoff.
# ---------------------------------------------------------------------------

def test_add_custom_hook_derives_slug_and_defaults():
    async def _run():
        db = await db_module.init_db(":memory:")
        p = await db_module.create_project(db, "hooks-crud")
        hook = await db_module.add_custom_hook(
            db, p["id"], "No Secrets!", "PreToolUse", "exit 0", matcher="Read|Bash",
        )
        return hook

    hook = asyncio.run(_run())
    assert hook["slug"] == "no_secrets"
    assert hook["event"] == "PreToolUse"
    assert hook["blocking"] == 1          # default: real exit-code semantics
    assert hook["enabled"] == 1
    assert hook["script_ps1"] is None


def test_add_custom_hook_rejects_reserved_sprint_guard_name():
    async def _run():
        db = await db_module.init_db(":memory:")
        p = await db_module.create_project(db, "hooks-reserved")
        with pytest.raises(ValueError, match="reserved"):
            await db_module.add_custom_hook(db, p["id"], "sprint_guard", "Stop", "exit 0")

    asyncio.run(_run())


def test_add_custom_hook_rejects_duplicate_slug_and_bad_event():
    async def _run():
        db = await db_module.init_db(":memory:")
        p = await db_module.create_project(db, "hooks-dupe")
        await db_module.add_custom_hook(db, p["id"], "My Hook", "PreToolUse", "exit 0")
        with pytest.raises(ValueError, match="already exists"):
            await db_module.add_custom_hook(db, p["id"], "my hook", "PostToolUse", "exit 0")
        with pytest.raises(ValueError, match="event must be"):
            await db_module.add_custom_hook(db, p["id"], "another", "Bogus", "exit 0")

    asyncio.run(_run())


def test_get_custom_hooks_filters_by_event_and_enabled():
    async def _run():
        db = await db_module.init_db(":memory:")
        p = await db_module.create_project(db, "hooks-filter")
        await db_module.add_custom_hook(db, p["id"], "one", "PreToolUse", "exit 0")
        await db_module.add_custom_hook(db, p["id"], "two", "Stop", "exit 0", enabled=False)
        all_hooks = await db_module.get_custom_hooks(db, p["id"])
        pre_only = await db_module.get_custom_hooks(db, p["id"], event="PreToolUse")
        enabled_only = await db_module.get_custom_hooks(db, p["id"], enabled_only=True)
        return all_hooks, pre_only, enabled_only

    all_hooks, pre_only, enabled_only = asyncio.run(_run())
    assert len(all_hooks) == 2
    assert [h["slug"] for h in pre_only] == ["one"]
    assert [h["slug"] for h in enabled_only] == ["one"]


def test_delete_custom_hook_idempotent():
    async def _run():
        db = await db_module.init_db(":memory:")
        p = await db_module.create_project(db, "hooks-delete")
        hook = await db_module.add_custom_hook(db, p["id"], "one", "Stop", "exit 0")
        first = await db_module.delete_custom_hook(db, p["id"], hook["id"])
        second = await db_module.delete_custom_hook(db, p["id"], hook["id"])
        return first, second

    first, second = asyncio.run(_run())
    assert first is True
    assert second is False


def test_update_custom_hook_rename_re_derives_slug():
    async def _run():
        db = await db_module.init_db(":memory:")
        p = await db_module.create_project(db, "hooks-update")
        hook = await db_module.add_custom_hook(db, p["id"], "one", "Stop", "exit 0")
        updated = await db_module.update_custom_hook(
            db, p["id"], hook["id"], name="renamed hook", blocking=False,
        )
        return updated

    updated = asyncio.run(_run())
    assert updated["slug"] == "renamed_hook"
    assert updated["blocking"] == 0


def test_write_sprint_guard_hooks_also_writes_enabled_custom_hooks(tmp_path):
    # Blocking hooks are written byte-for-byte (both .sh and .ps1 when
    # script_ps1 is provided); disabled hooks are skipped entirely; the
    # sprint_guard files are still written unchanged alongside them.
    async def _run():
        db = await db_module.init_db(":memory:")
        await db_module.add_custom_hook(
            db, "proj-hooks-1", "Hard Block", "PreToolUse", "echo blocked; exit 2",
            script_ps1="Write-Output blocked; exit 2", matcher="Bash", blocking=True,
        )
        await db_module.add_custom_hook(
            db, "proj-hooks-1", "Disabled One", "Stop", "exit 0", enabled=False,
        )
        await handoff_module._write_sprint_guard_hooks(db, "proj-hooks-1", root=tmp_path)

    asyncio.run(_run())
    hooks_dir = tmp_path / ".claude" / "hooks"
    assert (hooks_dir / "sprint_guard.sh").exists()
    assert (hooks_dir / "sprint_guard.ps1").exists()
    sh = (hooks_dir / "hard_block.sh").read_text(encoding="utf-8")
    ps1 = (hooks_dir / "hard_block.ps1").read_text(encoding="utf-8")
    assert sh.strip().endswith("echo blocked; exit 2")   # written verbatim (blocking=True)
    assert ps1.strip().endswith("exit 2")
    assert not (hooks_dir / "disabled_one.sh").exists()  # disabled hooks are skipped


def test_write_sprint_guard_hooks_advisory_hook_downgrades_exit_2(tmp_path):
    # blocking=False: the .sh is wrapped in a subshell that downgrades a would-be
    # exit 2 to exit 1 (advisory, never hard-blocks); the .ps1 gets a wrapper +
    # a companion _body.ps1 (PowerShell's `exit` can't be intercepted in-process,
    # so the body has to run as a genuine child process to isolate its exit code).
    async def _run():
        db = await db_module.init_db(":memory:")
        await db_module.add_custom_hook(
            db, "proj-hooks-2", "Style Nudge", "PostToolUse", "echo nudge; exit 2",
            script_ps1="Write-Output nudge; exit 2", matcher="Edit|Write", blocking=False,
        )
        await handoff_module._write_sprint_guard_hooks(db, "proj-hooks-2", root=tmp_path)

    asyncio.run(_run())
    hooks_dir = tmp_path / ".claude" / "hooks"
    sh = (hooks_dir / "style_nudge.sh").read_text(encoding="utf-8")
    assert "(\necho nudge; exit 2\n)" in sh
    assert 'exit "$_meridian_hook_rc"' in sh
    assert "exit 1" in sh                                 # the downgrade path

    ps1 = (hooks_dir / "style_nudge.ps1").read_text(encoding="utf-8")
    body = (hooks_dir / "style_nudge_body.ps1").read_text(encoding="utf-8")
    assert "style_nudge_body.ps1" in ps1
    assert "$LASTEXITCODE" in ps1
    assert body.strip().endswith("exit 2")


def test_write_sprint_guard_hooks_never_writes_reserved_slug_files(tmp_path):
    # Defense-in-depth: even if a row somehow bypassed add_custom_hook's
    # reserved-name check, the writer must never let a custom hook shadow the
    # real sprint_guard files.
    async def _run():
        db = await db_module.init_db(":memory:")
        p = await db_module.create_project(db, "hooks-reserved-write")
        # Insert directly to bypass the db-layer guard and prove the writer's
        # own defense-in-depth skip. A distinctive marker in the bypass row's
        # script makes it unmistakable if it ever leaked into sprint_guard.sh.
        await db.execute(
            "INSERT INTO custom_hooks (id, project_id, name, slug, event, "
            "script_sh, blocking, enabled) VALUES (?, ?, 'x', 'sprint_guard', "
            "'Stop', 'echo MERIDIAN_BYPASS_MARKER_9f3a', 1, 1)",
            ("bypass-id", p["id"]),
        )
        await db.commit()
        await handoff_module._write_sprint_guard_hooks(db, p["id"], root=tmp_path)
        return p["id"]

    asyncio.run(_run())
    sh = (tmp_path / ".claude" / "hooks" / "sprint_guard.sh").read_text(encoding="utf-8")
    assert "MERIDIAN_BYPASS_MARKER_9f3a" not in sh  # bypass row never leaked in
    assert "pending_count" in sh                    # still the real sprint_guard body


# ---------------------------------------------------------------------------
# 34f76536 -- template/checked-in-file parity (the a03c0eeb drift this fixes)
# ---------------------------------------------------------------------------


def test_sprint_guard_templates_have_feature_parity():
    """a03c0eeb landed the worktree-sweep addition directly in the checked-in
    sprint_guard.ps1 (commit 9a6cc6b8) without ever touching handoff.py's
    generator templates, so POSIX silently never got the feature and any
    future generate_handoff() call would have regressed the Windows hook
    back to pre-a03c0eeb content. Assert both platform templates carry the
    same feature markers so a future single-platform addition to ONE
    template without the other fails the suite immediately."""
    sh = handoff_module._SPRINT_GUARD_SH
    ps1 = handoff_module._SPRINT_GUARD_PS1
    markers = (
        "c0d2356d", "b4ce3274", "e2e1b682", "a03c0eeb",
        "stop_hook_active", "pending_count", "verification_pending_count",
        "worktrees/sweep",
    )
    for marker in markers:
        assert marker in sh, f"{marker!r} in _SPRINT_GUARD_PS1 but missing from _SPRINT_GUARD_SH"
        assert marker in ps1, f"{marker!r} in _SPRINT_GUARD_SH but missing from _SPRINT_GUARD_PS1"


def test_checked_in_sprint_guard_hooks_match_generator_output(tmp_path, monkeypatch):
    """The committed .claude/hooks/sprint_guard.{sh,ps1} must always be
    byte-for-byte what _write_sprint_guard_hooks generates today for this
    project's real baked id -- catches template/checked-in-file drift (the
    exact a03c0eeb asymmetry this item fixes) the moment either side changes
    without the other being regenerated to match."""
    monkeypatch.delenv("MERIDIAN_URL", raising=False)  # checked-in files bake the default URL
    real_hooks_dir = Path(handoff_module.__file__).parent.parent / ".claude" / "hooks"
    checked_in_sh = real_hooks_dir / "sprint_guard.sh"
    checked_in_ps1 = real_hooks_dir / "sprint_guard.ps1"
    if not checked_in_sh.exists() or not checked_in_ps1.exists():
        pytest.skip("no checked-in .claude/hooks/sprint_guard.{sh,ps1} in this checkout")

    # Recover the baked project_id from the checked-in file rather than
    # hardcoding it, so this test tracks whichever project this repo is
    # currently wired to.
    checked_in_sh_text = checked_in_sh.read_text(encoding="utf-8")
    m = re.search(r'PROJECT_ID="([^"]+)"', checked_in_sh_text)
    assert m, "could not recover PROJECT_ID from checked-in sprint_guard.sh"
    project_id = m.group(1)

    async def _run():
        db = await db_module.init_db(":memory:")
        await handoff_module._write_sprint_guard_hooks(db, project_id, root=tmp_path)

    asyncio.run(_run())
    generated_sh = (tmp_path / ".claude" / "hooks" / "sprint_guard.sh").read_text(encoding="utf-8")
    generated_ps1 = (tmp_path / ".claude" / "hooks" / "sprint_guard.ps1").read_text(encoding="utf-8")
    assert generated_sh == checked_in_sh_text
    assert generated_ps1 == checked_in_ps1.read_text(encoding="utf-8")


async def test_mcp_custom_hook_tools_add_get_delete(db):
    from meridian import server as srv

    p = await db_module.create_project(db, "hooks-mcp")
    added = await srv._dispatch_mcp_tool(
        "add_custom_hook",
        {"project_id": p["id"], "name": "No Secrets", "event": "PreToolUse",
         "script_sh": "exit 0", "matcher": "Read|Bash"},
        db, "/tmp",
    )
    listed = await srv._dispatch_mcp_tool(
        "get_custom_hooks", {"project_id": p["id"]}, db, "/tmp",
    )
    reserved_err = await srv._dispatch_mcp_tool(
        "add_custom_hook",
        {"project_id": p["id"], "name": "sprint_guard", "event": "Stop", "script_sh": "exit 0"},
        db, "/tmp",
    )
    deleted = await srv._dispatch_mcp_tool(
        "delete_custom_hook", {"project_id": p["id"], "hook_id": added["id"]}, db, "/tmp",
    )
    deleted_again = await srv._dispatch_mcp_tool(
        "delete_custom_hook", {"project_id": p["id"], "hook_id": added["id"]}, db, "/tmp",
    )
    missing_args = await srv._dispatch_mcp_tool("add_custom_hook", {"project_id": p["id"]}, db, "/tmp")

    assert added["slug"] == "no_secrets"
    assert listed["hooks"][0]["id"] == added["id"]
    assert "error" in reserved_err
    assert deleted == {"hook_id": added["id"], "deleted": True}
    assert deleted_again == {"hook_id": added["id"], "deleted": False}
    assert "error" in missing_args


def test_custom_hook_tools_registered():
    from meridian.mcp_tools import _MCP_TOOLS_LIST, _READ_ONLY_TOOLS, _DESTRUCTIVE_TOOLS
    names = {t["name"] for t in _MCP_TOOLS_LIST}
    assert {"add_custom_hook", "get_custom_hooks", "delete_custom_hook"} <= names
    assert "get_custom_hooks" in _READ_ONLY_TOOLS
    assert "delete_custom_hook" in _DESTRUCTIVE_TOOLS
    assert "add_custom_hook" not in _READ_ONLY_TOOLS
    assert "add_custom_hook" not in _DESTRUCTIVE_TOOLS
