"""b4f4627f — owned-process Stop cleanup: optional, dashboard-toggleable,
artifact-safe.

Covers the pieces added for this item on top of the existing 273287cb
custom-hooks infra and f7084ed0's orphan_reaper-specific dashboard toggle:

* the previously-missing ``update_custom_hook`` MCP tool (enable/disable/edit
  path for ANY user-defined hook, not just orphan_reaper's bespoke route),
* immediate artifact-removal on disable via that tool
  (``handoff.remove_custom_hook_artifacts`` / ``custom_hook_artifact_filenames``),
* ``handoff._write_custom_hooks`` converging (removing stale files for)
  disabled hooks on every ``generate_handoff``, not just skipping them,
* a generic ``stop_hook_active`` infinite-retrigger guard baked into every
  rendered Stop-event custom hook (previously only sprint_guard had this),
* the same guard, plus a same-session in-flight guard, on the ``/hooks/stop``
  route itself.
"""
from __future__ import annotations

import asyncio

import pytest

from meridian import db as db_module
from meridian import handoff as handoff_module


# ---------------------------------------------------------------------------
# 1. _render_custom_hook_files — generic stop_hook_active guard
# ---------------------------------------------------------------------------


def test_render_stop_hook_blocking_gets_stop_hook_active_guard():
    hook = {
        "slug": "my_stop_hook", "name": "My Stop Hook", "event": "Stop",
        "script_sh": "echo did-cleanup", "script_ps1": "Write-Output did-cleanup",
        "blocking": 1,
    }
    out = handoff_module._render_custom_hook_files(hook)
    sh = out["my_stop_hook.sh"]
    ps1 = out["my_stop_hook.ps1"]
    assert "stop_hook_active" in sh
    assert 'exit 0' in sh
    assert "echo did-cleanup" in sh                 # original body preserved
    assert "MERIDIAN_HOOK_STDIN" in sh               # payload re-exposed

    assert "stop_hook_active" in ps1
    assert "Write-Output did-cleanup" in ps1
    assert "MERIDIAN_HOOK_STDIN" in ps1


def test_render_stop_hook_advisory_gets_stop_hook_active_guard():
    hook = {
        "slug": "advisory_stop", "name": "Advisory Stop", "event": "Stop",
        "script_sh": "exit 2", "blocking": 0,
    }
    out = handoff_module._render_custom_hook_files(hook)
    sh = out["advisory_stop.sh"]
    assert "stop_hook_active" in sh
    # guard must come before the advisory downgrade wrapper, not inside it.
    assert sh.index("stop_hook_active") < sh.index("_meridian_hook_rc")


def test_render_non_stop_hook_unaffected_by_stop_guard():
    # PreToolUse/PostToolUse hooks never see stop_hook_active — rendering must
    # be byte-for-byte identical to pre-b4f4627f behavior (blocking=True).
    hook = {
        "slug": "pre_hook", "name": "Pre Hook", "event": "PreToolUse",
        "script_sh": "echo blocked; exit 2", "matcher": "Bash", "blocking": 1,
    }
    out = handoff_module._render_custom_hook_files(hook)
    sh = out["pre_hook.sh"]
    assert "stop_hook_active" not in sh
    assert "MERIDIAN_HOOK_STDIN" not in sh
    assert sh.strip().endswith("echo blocked; exit 2")


# ---------------------------------------------------------------------------
# 2. custom_hook_artifact_filenames / remove_custom_hook_artifacts
# ---------------------------------------------------------------------------


def test_custom_hook_artifact_filenames_fixed_superset():
    assert handoff_module.custom_hook_artifact_filenames("foo") == (
        "foo.sh", "foo.ps1", "foo_body.ps1",
    )


def test_remove_custom_hook_artifacts_deletes_present_files(tmp_path):
    hooks_dir = tmp_path / ".claude" / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "thing.sh").write_text("# stub", encoding="utf-8")
    (hooks_dir / "thing.ps1").write_text("# stub", encoding="utf-8")
    # thing_body.ps1 deliberately absent — removal must not error on a
    # missing file, and must not report it as removed.

    removed = handoff_module.remove_custom_hook_artifacts(hooks_dir, "thing")
    assert set(removed) == {"thing.sh", "thing.ps1"}
    assert not (hooks_dir / "thing.sh").exists()
    assert not (hooks_dir / "thing.ps1").exists()


def test_remove_custom_hook_artifacts_empty_slug_is_noop(tmp_path):
    assert handoff_module.remove_custom_hook_artifacts(tmp_path, "") == []


def test_remove_custom_hook_artifacts_missing_dir_is_noop(tmp_path):
    assert handoff_module.remove_custom_hook_artifacts(tmp_path / "nope", "thing") == []


# ---------------------------------------------------------------------------
# 3. _write_custom_hooks converges disabled hooks (removes stale files)
# ---------------------------------------------------------------------------


def test_write_custom_hooks_removes_files_when_hook_becomes_disabled(tmp_path):
    async def _run():
        db = await db_module.init_db(":memory:")
        p = await db_module.create_project(db, "hooks-converge")
        hook = await db_module.add_custom_hook(
            db, p["id"], "Cleanup", "Stop", "exit 0", blocking=True,
        )
        hooks_dir = tmp_path / ".claude" / "hooks"
        hooks_dir.mkdir(parents=True)
        # First write: hook is enabled -> file exists.
        await handoff_module._write_custom_hooks(db, p["id"], hooks_dir)
        assert (hooks_dir / "cleanup.sh").exists()
        # Disable it, then re-run the writer — must converge (remove stale file)
        # rather than silently leaving it in place.
        await db_module.update_custom_hook(db, p["id"], hook["id"], enabled=False)
        await handoff_module._write_custom_hooks(db, p["id"], hooks_dir)
        return hooks_dir

    hooks_dir = asyncio.run(_run())
    assert not (hooks_dir / "cleanup.sh").exists()


# ---------------------------------------------------------------------------
# 4. update_custom_hook MCP tool
# ---------------------------------------------------------------------------


async def test_mcp_update_custom_hook_edits_fields(db):
    from meridian import server as srv

    p = await db_module.create_project(db, "hooks-update-mcp")
    added = await srv._dispatch_mcp_tool(
        "add_custom_hook",
        {"project_id": p["id"], "name": "one", "event": "Stop", "script_sh": "exit 0"},
        db, "/tmp",
    )
    updated = await srv._dispatch_mcp_tool(
        "update_custom_hook",
        {"project_id": p["id"], "hook_id": added["id"], "name": "renamed", "blocking": False},
        db, "/tmp",
    )
    assert updated["slug"] == "renamed"
    assert updated["blocking"] == 0
    assert "error" not in updated


async def test_mcp_update_custom_hook_requires_hook_id_and_a_field(db):
    from meridian import server as srv

    p = await db_module.create_project(db, "hooks-update-missing-args")
    no_hook_id = await srv._dispatch_mcp_tool(
        "update_custom_hook", {"project_id": p["id"], "enabled": False}, db, "/tmp",
    )
    assert "error" in no_hook_id

    added = await srv._dispatch_mcp_tool(
        "add_custom_hook",
        {"project_id": p["id"], "name": "two", "event": "Stop", "script_sh": "exit 0"},
        db, "/tmp",
    )
    no_fields = await srv._dispatch_mcp_tool(
        "update_custom_hook", {"project_id": p["id"], "hook_id": added["id"]}, db, "/tmp",
    )
    assert "error" in no_fields


async def test_mcp_update_custom_hook_unknown_hook_id_returns_error(db):
    from meridian import server as srv

    p = await db_module.create_project(db, "hooks-update-unknown")
    result = await srv._dispatch_mcp_tool(
        "update_custom_hook",
        {"project_id": p["id"], "hook_id": "no-such-hook", "enabled": False},
        db, "/tmp",
    )
    assert "error" in result


async def test_mcp_update_custom_hook_rejects_reserved_rename(db):
    from meridian import server as srv

    p = await db_module.create_project(db, "hooks-update-reserved")
    added = await srv._dispatch_mcp_tool(
        "add_custom_hook",
        {"project_id": p["id"], "name": "three", "event": "Stop", "script_sh": "exit 0"},
        db, "/tmp",
    )
    result = await srv._dispatch_mcp_tool(
        "update_custom_hook",
        {"project_id": p["id"], "hook_id": added["id"], "name": "sprint_guard"},
        db, "/tmp",
    )
    assert "error" in result


async def test_mcp_update_custom_hook_disable_removes_artifacts_immediately(db, tmp_path):
    from meridian import server as srv

    repo_dir = tmp_path / "myrepo"
    hooks_dir = repo_dir / ".claude" / "hooks"
    hooks_dir.mkdir(parents=True)

    p = await db_module.create_project(db, "hooks-update-immediate-removal")
    await db_module.set_executor_config(db, p["id"], {"repo_path": str(repo_dir)})
    added = await srv._dispatch_mcp_tool(
        "add_custom_hook",
        {"project_id": p["id"], "name": "immediate", "event": "Stop", "script_sh": "exit 0"},
        db, "/tmp",
    )
    # Simulate a prior generate_handoff having already written this hook's files.
    await handoff_module._write_custom_hooks(db, p["id"], hooks_dir)
    assert (hooks_dir / "immediate.sh").exists()

    result = await srv._dispatch_mcp_tool(
        "update_custom_hook",
        {"project_id": p["id"], "hook_id": added["id"], "enabled": False},
        db, "/tmp",
    )
    assert result["enabled"] == 0
    assert result["removed_files"] == ["immediate.sh"]
    assert not (hooks_dir / "immediate.sh").exists()


async def test_mcp_update_custom_hook_disable_without_repo_path_removes_nothing(db):
    from meridian import server as srv

    p = await db_module.create_project(db, "hooks-update-no-repo")
    added = await srv._dispatch_mcp_tool(
        "add_custom_hook",
        {"project_id": p["id"], "name": "norepo", "event": "Stop", "script_sh": "exit 0"},
        db, "/tmp",
    )
    result = await srv._dispatch_mcp_tool(
        "update_custom_hook",
        {"project_id": p["id"], "hook_id": added["id"], "enabled": False},
        db, "/tmp",
    )
    assert result["enabled"] == 0
    assert "removed_files" not in result  # nothing to report when no repo_path is set


async def test_mcp_update_custom_hook_enabling_never_removes_files(db, tmp_path):
    # Only a True -> False transition triggers removal; True -> True (no-op)
    # or False -> True must never touch the filesystem.
    from meridian import server as srv

    repo_dir = tmp_path / "myrepo2"
    hooks_dir = repo_dir / ".claude" / "hooks"
    hooks_dir.mkdir(parents=True)

    p = await db_module.create_project(db, "hooks-update-enable-noop")
    await db_module.set_executor_config(db, p["id"], {"repo_path": str(repo_dir)})
    added = await srv._dispatch_mcp_tool(
        "add_custom_hook",
        {"project_id": p["id"], "name": "stay-on", "event": "Stop", "script_sh": "exit 0",
         "enabled": False},
        db, "/tmp",
    )
    result = await srv._dispatch_mcp_tool(
        "update_custom_hook",
        {"project_id": p["id"], "hook_id": added["id"], "enabled": True},
        db, "/tmp",
    )
    assert result["enabled"] == 1
    assert "removed_files" not in result


def test_update_custom_hook_tool_registered():
    from meridian.mcp_tools import (
        _MCP_TOOLS_LIST, _READ_ONLY_TOOLS, _DESTRUCTIVE_TOOLS, _TOOL_CATEGORY,
    )
    names = {t["name"] for t in _MCP_TOOLS_LIST}
    assert "update_custom_hook" in names
    assert "update_custom_hook" not in _READ_ONLY_TOOLS
    assert "update_custom_hook" not in _DESTRUCTIVE_TOOLS
    assert _TOOL_CATEGORY.get("update_custom_hook") == "config"


# ---------------------------------------------------------------------------
# 5. /hooks/stop route guards (stop_hook_active + same-session in-flight)
# ---------------------------------------------------------------------------


def test_hooks_stop_stop_hook_active_short_circuits(client, monkeypatch):
    called = {"n": 0}

    async def _boom(*a, **kw):
        called["n"] += 1
        raise AssertionError("generate_handoff must not be called when stop_hook_active")

    monkeypatch.setattr("meridian.handoff.generate_handoff", _boom)

    project = client.post("/projects", json={"name": "stop-active-proj"}).json()
    r = client.post(
        "/hooks/stop",
        json={"project_id": project["id"], "stop_hook_active": True},
    )
    assert r.status_code == 200
    body = r.json()
    assert body == {"ok": True, "handoff": None, "reason": "stop_hook_active"}
    assert called["n"] == 0


def test_hooks_stop_in_flight_guard_skips_concurrent_same_session(client, monkeypatch):
    from meridian import server as srv

    async def _boom(*a, **kw):
        raise AssertionError("generate_handoff must not be called while already in-flight")

    monkeypatch.setattr("meridian.handoff.generate_handoff", _boom)

    project = client.post("/projects", json={"name": "stop-inflight-proj"}).json()
    start = client.post("/hooks/session-start", json={"project_id": project["id"]}).json()
    ctx = start["hookSpecificOutput"]["additionalContext"]
    session_id = None
    for line in ctx.splitlines():
        if line.startswith("SESSION ID:"):
            session_id = line.split(":", 1)[1].strip()
            break
    assert session_id

    srv._STOP_HOOK_INFLIGHT.add(session_id)
    try:
        r = client.post(
            "/hooks/stop",
            json={"project_id": project["id"], "session_id": session_id},
        )
    finally:
        srv._STOP_HOOK_INFLIGHT.discard(session_id)

    assert r.status_code == 200
    body = r.json()
    assert body == {"ok": True, "handoff": None, "reason": "already_in_progress"}


def test_hooks_stop_clears_in_flight_guard_after_completion(client, monkeypatch):
    from meridian import server as srv

    async def _fake_handoff(db, project_id, output_dir, *, mode="full", session_id=None, **kw):
        return ("/tmp/handoff-delta.md", "# delta handoff\n", False)

    monkeypatch.setattr("meridian.handoff.generate_handoff", _fake_handoff)

    project = client.post("/projects", json={"name": "stop-inflight-clears"}).json()
    start = client.post("/hooks/session-start", json={"project_id": project["id"]}).json()
    ctx = start["hookSpecificOutput"]["additionalContext"]
    session_id = None
    for line in ctx.splitlines():
        if line.startswith("SESSION ID:"):
            session_id = line.split(":", 1)[1].strip()
            break
    assert session_id

    r = client.post(
        "/hooks/stop",
        json={"project_id": project["id"], "session_id": session_id},
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True
    # Guard must be released after a successful run, not left set forever.
    assert session_id not in srv._STOP_HOOK_INFLIGHT
