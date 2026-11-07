"""Local workspace overview (phase 1, read-only): builder tests. Route tests are in test_workspace_overview_route.py.

Every fixture is a temp directory. Repos, index DBs, settings, MCP files, guard
config and audit lines are written under ``tmp_path``. The real home directory,
the real guard dir and the real ``meridian.toml`` are never read: HOME, USERPROFILE
and the toml connection lookup are redirected in an autouse fixture. ``shutil.which``
is stubbed so the runtime checks depend only on the files the test writes.

Raw SQL in this file uses ``?`` placeholders. The aiosqlite ``db`` fixture needs
``?`` on SQLite, and ``meridian/pg_adapter.py`` rewrites ``?`` to ``%s`` on Postgres.
"""
from __future__ import annotations

import asyncio
import builtins
import json
import os
import re
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from meridian import cbm_registry as reg
from meridian import code_intel_receipt as receipts
from meridian import db as dbm
from meridian import guard_core as gc
from meridian import hook_settings_merge as hsm
from meridian import setup_bundle as bundle
from meridian import toml_config
from meridian import workspace_overview as wo

NOW = 1_790_452_800  # 2026-09-26T20:00:00Z, the same anchor test_cbm_registry.py uses
PATH = "/workspace/overview"
LOOPBACK = ("127.0.0.1", 50000)
DAY = 86400
SENTINEL_TOKEN = "sk_meridian_TESTVALUE_0001"
SENTINEL_PW = "SENTINEL_PW_0001"


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_machine(tmp_path, monkeypatch):
    """No real home, no real meridian.toml, no real `which` lookups."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(toml_config, "get_active_db_url", lambda: (None, "local"))
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: f"/usr/bin/{name}")
    monkeypatch.setattr(wo.socket, "gethostname", lambda: "laptop.corp.local")
    return home


@pytest.fixture
def gdir(tmp_path) -> Path:
    guard = tmp_path / "guard"
    guard.mkdir()
    return guard


@pytest.fixture
def cache(tmp_path) -> Path:
    c = tmp_path / "cbm-cache"
    c.mkdir()
    return c


@pytest.fixture
def env(gdir, _isolated_machine) -> dict:
    return {
        "MERIDIAN_GUARD_DIR": str(gdir),
        "HOME": str(_isolated_machine),
        "USERPROFILE": str(_isolated_machine),
    }


def _ts(epoch: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(epoch))


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def make_repo(base: Path, name: str, *, git: bool = True) -> Path:
    path = base / name
    path.mkdir(parents=True, exist_ok=True)
    if git:
        (path / ".git").mkdir(exist_ok=True)
    return path


def make_index(
    cache: Path, name: str, root: Path, *, indexed: float = NOW - 3600,
    nodes: int = 5, files: int = 3, mtime: float | None = None,
) -> Path:
    """A codebase-memory index DB with the same schema as test_cbm_registry.make_db."""
    db = cache / f"{name}.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("CREATE TABLE projects (name TEXT PRIMARY KEY, indexed_at TEXT, root_path TEXT)")
        conn.execute("CREATE TABLE nodes (id INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT, name TEXT)")
        conn.execute("CREATE TABLE file_hashes (project TEXT, rel_path TEXT, sha256 TEXT, mtime_ns INTEGER, size INTEGER)")
        for i in range(files):
            conn.execute("INSERT INTO file_hashes VALUES (?, ?, 'x', 0, 0)", (name, f"f{i}.py"))
        conn.execute("INSERT INTO projects VALUES (?, ?, ?)", (name, _iso(indexed), str(root).replace("\\", "/")))
        for i in range(nodes):
            conn.execute("INSERT INTO nodes (project, name) VALUES (?, ?)", (name, f"n{i}"))
        conn.commit()
    finally:
        conn.close()
    stamp = indexed if mtime is None else mtime
    os.utime(db, (stamp, stamp))
    return db


def write_snapshot(cache: Path, gdir: Path, env: dict) -> dict:
    snap = reg.build_snapshot(str(cache), env=env, now=NOW)
    reg.write_snapshot_atomic(str(gdir / "snapshot.json"), snap)
    return snap


def install_guard(repo: Path, mode: str = "enforce", *, skip: tuple[str, ...] = ()) -> None:
    """Write the .claude/settings.json the installer would write, plus the shim files it names."""
    hooks: dict[str, list] = {}
    for event, matcher, hook in hsm.desired_entries(scope="project", mode=mode, shell="bash"):
        command = hook["command"]
        if any(f'/{shim}.sh"' in command for shim in skip):
            continue
        hooks.setdefault(event, []).append({"matcher": matcher, "hooks": [hook]})
    (repo / ".claude" / "hooks").mkdir(parents=True, exist_ok=True)
    for shim in ("meridian_guard", "meridian_guard_post", "meridian_guard_brief"):
        (repo / ".claude" / "hooks" / f"{shim}.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (repo / ".claude" / "hooks" / "meridian_guard.awk").write_text("", encoding="utf-8")
    (repo / ".claude" / "settings.json").write_text(json.dumps({"hooks": hooks}), encoding="utf-8")


def record_install(gdir: Path, repo: Path, mode: str, *, installed_at: str = "2026-09-26T19:00:00Z") -> None:
    """Write a config.json install record through the installer's own planner (test-only use)."""
    key = hsm._install_key("project", repo)
    record = {
        "scope": "project", "repo": str(repo), "mode": mode, "shell": "bash",
        "project_id": "pid-should-not-leak", "project_id_source": "test",
        "copied_files": ["copied_should_not_leak.sh"], "installed_at": installed_at,
    }
    op = hsm._config_op(gdir, key, record)
    gdir.mkdir(parents=True, exist_ok=True)
    op.path.write_text(op.content, encoding="utf-8")


async def add_project(db, name: str, repo_paths: list[dict] | None = None, *, status: str | None = None) -> dict:
    proj = await dbm.create_project(db, name)
    await dbm.set_executor_config(db, proj["id"], {"repo_paths": repo_paths or []})
    if status is not None:
        await db.execute("UPDATE projects SET status = ? WHERE id = ?", (status, proj["id"]))
        await db.commit()
    return proj


async def add_session(db, project_id: str, name: str, *, status: str = "active", last_seen: float = NOW - 60) -> dict:
    session = await dbm.register_session(db, project_id, name)
    await db.execute(
        "UPDATE sessions SET status = ?, last_seen = ? WHERE id = ?",
        (status, _ts(last_seen), session["id"]),
    )
    await db.commit()
    return session


async def overview(db, env: dict, now: float = NOW) -> dict:
    doc = await wo.build_overview(db, env=env, now=now)
    assert_full_document(doc)
    return doc


def assert_full_document(doc: dict) -> None:
    assert list(doc.keys()) == ["schema", "generated_at", "runtime", "repos", "sessions", "projects", "gaps", "errors"]
    assert doc["schema"] == "meridian-workspace-overview/1"
    assert set(doc["runtime"].keys()) == {"version", "hostname", "location", "deployment", "database", "guard", "tunnel"}
    assert set(doc["repos"].keys()) == {"snapshot", "items", "total", "truncated", "limit", "scan"}
    assert set(doc["sessions"].keys()) == {"live_window_s", "items", "total", "truncated", "totals"}
    assert set(doc["projects"].keys()) == {"items", "recent_window_s"}
    for gap in doc["gaps"]:
        assert set(gap.keys()) == {"code", "message", "source"}
    for error in doc["errors"]:
        assert set(error.keys()) == {"section", "code"}


def repo_by_name(doc: dict, name: str) -> dict:
    matches = [item for item in doc["repos"]["items"] if item["name"] == name]
    assert len(matches) == 1, f"expected one repo named {name!r}"
    return matches[0]


def gap_codes(doc: dict) -> set[str]:
    return {gap["code"] for gap in doc["gaps"]}


def error_pairs(doc: dict) -> set[tuple[str, str]]:
    return {(e["section"], e["code"]) for e in doc["errors"]}


# ---------------------------------------------------------------------------
# Snapshot classifier (spec 3.7)
# ---------------------------------------------------------------------------


async def test_missing_snapshot_is_an_empty_state_not_an_error(db, env):
    doc = await overview(db, env)
    assert doc["repos"]["snapshot"]["error"] == "missing"
    assert doc["repos"]["snapshot"]["present"] is False
    assert doc["repos"]["snapshot"]["rows_total"] is None
    assert doc["repos"]["items"] == []
    assert doc["errors"] == []


async def test_corrupt_snapshot_json_is_corrupt(db, env, gdir):
    (gdir / "snapshot.json").write_text("{not json", encoding="utf-8")
    doc = await overview(db, env)
    assert doc["repos"]["snapshot"]["error"] == "corrupt"
    assert doc["repos"]["snapshot"]["present"] is True
    assert ("repos", "snapshot_unreadable") in error_pairs(doc)


async def test_wrong_schema_is_corrupt(db, env, gdir):
    (gdir / "snapshot.json").write_text(json.dumps({"schema": "other/1", "rows": []}), encoding="utf-8")
    doc = await overview(db, env)
    assert doc["repos"]["snapshot"]["error"] == "corrupt"
    assert doc["repos"]["snapshot"]["schema"] == "other/1"


async def test_rows_that_are_not_a_list_is_corrupt(db, env, gdir):
    (gdir / "snapshot.json").write_text(
        json.dumps({"schema": reg.SNAPSHOT_SCHEMA, "rows": "nope"}), encoding="utf-8",
    )
    doc = await overview(db, env)
    assert doc["repos"]["snapshot"]["error"] == "corrupt"


async def test_snapshot_that_is_not_an_object_is_corrupt(db, env, gdir):
    (gdir / "snapshot.json").write_text("[1, 2, 3]", encoding="utf-8")
    doc = await overview(db, env)
    assert doc["repos"]["snapshot"]["error"] == "corrupt"


async def test_permission_error_on_snapshot_is_unreadable(db, env, gdir, monkeypatch):
    (gdir / "snapshot.json").write_text("{}", encoding="utf-8")
    real_open = builtins.open

    def guarded_open(file, *args, **kwargs):
        if str(file).replace("\\", "/").endswith("/snapshot.json"):
            raise PermissionError("denied")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)
    doc = await overview(db, env)
    assert doc["repos"]["snapshot"]["error"] == "unreadable"
    assert ("repos", "snapshot_unreadable") in error_pairs(doc)


async def test_directory_at_snapshot_path_is_unreadable(db, env, gdir):
    (gdir / "snapshot.json").mkdir()
    doc = await overview(db, env)
    assert doc["repos"]["snapshot"]["error"] == "unreadable"


async def test_snapshot_over_the_cap_is_unreadable(db, env, gdir, cache, monkeypatch):
    write_snapshot(cache, gdir, env)
    monkeypatch.setattr(wo, "SNAPSHOT_MAX_BYTES", 16)
    doc = await overview(db, env)
    assert doc["repos"]["snapshot"]["error"] == "unreadable"


async def test_valid_snapshot_reports_built_age_stats_and_rows(db, env, gdir, cache, tmp_path):
    repo = make_repo(tmp_path, "solo")
    make_index(cache, "solo", repo)
    write_snapshot(cache, gdir, env)
    doc = await overview(db, env)
    snap = doc["repos"]["snapshot"]
    assert snap["error"] is None
    assert snap["schema"] == reg.SNAPSHOT_SCHEMA
    assert snap["built_at"] == NOW
    assert snap["built_age_s"] == 0.0
    assert snap["rows_total"] == 1
    assert snap["stats"]["read"] == 1
    assert snap["stale_after_days"] == 7
    assert repo_by_name(doc, "solo")["codebase_memory"]["index"]["snapshot_state"] == "ok"


async def test_missing_snapshot_marks_index_state_unknown(db, env, tmp_path):
    repo = make_repo(tmp_path, "lonely")
    await add_project(db, "lonely", [{"cwd": str(repo), "hostname": ""}])
    doc = await overview(db, env)
    item = repo_by_name(doc, "lonely")
    assert item["codebase_memory"]["index"]["snapshot_state"] == "missing"
    assert item["codebase_memory"]["index"]["mode"] == "none"


# ---------------------------------------------------------------------------
# Index facts (spec 3.5)
# ---------------------------------------------------------------------------


async def test_old_index_is_not_fresh_and_recent_index_is_fresh(db, env, gdir, cache, tmp_path):
    old = make_repo(tmp_path, "old-index")
    new = make_repo(tmp_path, "new-index")
    make_index(cache, "old-index", old, indexed=NOW - 8 * DAY, mtime=NOW - 8 * DAY)
    make_index(cache, "new-index", new, indexed=NOW - DAY, mtime=NOW - DAY)
    write_snapshot(cache, gdir, env)
    await add_project(db, "old-index", [{"cwd": str(old), "hostname": ""}])
    await add_project(db, "new-index", [{"cwd": str(new), "hostname": ""}])
    doc = await overview(db, env)
    old_index = repo_by_name(doc, "old-index")["codebase_memory"]["index"]
    new_index = repo_by_name(doc, "new-index")["codebase_memory"]["index"]
    assert old_index["fresh"] is False
    assert old_index["age_days"] > 7
    assert new_index["fresh"] is True
    assert new_index["present"] is True


async def test_resolve_reports_own_pin_canonical_ancestor_and_none(db, env, gdir, cache, tmp_path):
    own = make_repo(tmp_path, "own")
    make_index(cache, "own", own)

    pinned = make_repo(tmp_path, "pinned")
    elsewhere = make_repo(tmp_path, "elsewhere", git=False)
    make_index(cache, "pinned-index", elsewhere)
    (gdir / "pins.json").write_text(json.dumps({str(pinned): "pinned-index"}), encoding="utf-8")

    canon = make_repo(tmp_path, "canon")
    make_index(cache, "canon", canon)
    linked = make_repo(tmp_path, "linked", git=False)
    worktrees = canon / ".git" / "worktrees" / "linked"
    worktrees.mkdir(parents=True)
    (worktrees / "commondir").write_text("../..\n", encoding="utf-8")
    (linked / ".git").write_text(f"gitdir: {worktrees.as_posix()}\n", encoding="utf-8")

    parent = make_repo(tmp_path, "parent", git=False)
    make_index(cache, "parent", parent)
    child = parent / "child"
    child.mkdir()

    make_repo(tmp_path, "lonely")

    write_snapshot(cache, gdir, env)
    for name, path in [("own", own), ("pinned", pinned), ("linked", linked), ("child", child), ("lonely", tmp_path / "lonely")]:
        await add_project(db, f"proj-{name}", [{"cwd": str(path), "hostname": ""}])

    doc = await overview(db, env)
    modes = {item["name"]: item["codebase_memory"]["index"]["mode"] for item in doc["repos"]["items"]}
    assert modes["own"] == "own"
    assert modes["pinned"] == "pin"
    assert modes["linked"] == "canonical"
    assert modes["child"] == "ancestor"
    assert modes["lonely"] == "none"
    assert repo_by_name(doc, "linked")["canonical"]["linked"] is True
    assert repo_by_name(doc, "linked")["canonical"]["canonical_key"] == reg.path_key(reg.norm_path(str(canon), msys=True))


async def test_partial_index_when_nodes_are_below_files(db, env, gdir, cache, tmp_path):
    repo = make_repo(tmp_path, "partial")
    make_index(cache, "partial", repo, nodes=2, files=5)
    write_snapshot(cache, gdir, env)
    await add_project(db, "partial", [{"cwd": str(repo), "hostname": ""}])
    doc = await overview(db, env)
    assert repo_by_name(doc, "partial")["codebase_memory"]["index"]["partial"] is True


async def test_present_false_with_fresh_true_after_the_db_is_deleted(db, env, gdir, cache, tmp_path):
    repo = make_repo(tmp_path, "deleted-db")
    db_file = make_index(cache, "deleted-db", repo, indexed=NOW - DAY, mtime=NOW - DAY)
    write_snapshot(cache, gdir, env)
    db_file.unlink()
    await add_project(db, "deleted-db", [{"cwd": str(repo), "hostname": ""}])
    doc = await overview(db, env)
    index = repo_by_name(doc, "deleted-db")["codebase_memory"]["index"]
    assert index["present"] is False
    assert index["fresh"] is True
    assert "index_nodes_zero_ambiguous" not in gap_codes(doc)


async def test_covered_dirs_unknown_gives_null_coverage(db, env, gdir, cache, tmp_path):
    repo = make_repo(tmp_path, "uncovered")
    make_index(cache, "uncovered", repo)
    snap = write_snapshot(cache, gdir, env)
    for row in snap["rows"]:
        row["covered_dirs"] = None
    reg.write_snapshot_atomic(str(gdir / "snapshot.json"), snap)
    await add_project(db, "uncovered", [{"cwd": str(repo), "hostname": ""}])
    doc = await overview(db, env)
    assert repo_by_name(doc, "uncovered")["codebase_memory"]["index"]["covers_repo_root"] is None


async def test_zero_nodes_raises_the_ambiguity_gap(db, env, gdir, cache, tmp_path):
    repo = make_repo(tmp_path, "zero")
    make_index(cache, "zero", repo, nodes=0, files=0)
    write_snapshot(cache, gdir, env)
    await add_project(db, "zero", [{"cwd": str(repo), "hostname": ""}])
    doc = await overview(db, env)
    assert "index_nodes_zero_ambiguous" in gap_codes(doc)


# ---------------------------------------------------------------------------
# Settings, hooks and guard state (spec 2.2, 3.5)
# ---------------------------------------------------------------------------


async def test_enforce_and_advisory_installs_parse_differently(db, env, gdir, tmp_path):
    enforce = make_repo(tmp_path, "enforce-repo")
    advisory = make_repo(tmp_path, "advisory-repo")
    install_guard(enforce, "enforce")
    install_guard(advisory, "advisory")
    await add_project(db, "enforce-repo", [{"cwd": str(enforce), "hostname": ""}])
    await add_project(db, "advisory-repo", [{"cwd": str(advisory), "hostname": ""}])
    doc = await overview(db, env)
    strong = repo_by_name(doc, "enforce-repo")["guard"]
    weak = repo_by_name(doc, "advisory-repo")["guard"]
    assert strong["settings_state"] == "present"
    assert strong["registered"] is True
    assert strong["installed_mode"] == "enforce"
    assert strong["components"] == ["brief", "post", "pre"]
    assert strong["complete"] is True
    assert strong["scope"] == "project"
    assert strong["runtime_ok"] is True
    assert weak["installed_mode"] == "advisory"


async def test_malformed_settings_is_unreadable_with_error(db, env, tmp_path):
    repo = make_repo(tmp_path, "bad-settings")
    (repo / ".claude").mkdir()
    (repo / ".claude" / "settings.json").write_text("{oops", encoding="utf-8")
    await add_project(db, "bad-settings", [{"cwd": str(repo), "hostname": ""}])
    doc = await overview(db, env)
    guard = repo_by_name(doc, "bad-settings")["guard"]
    assert guard["settings_state"] == "unreadable"
    assert guard["registered"] is None
    assert ("repos", "settings_unreadable") in error_pairs(doc)


async def test_settings_that_parse_to_a_non_object_is_unreadable(db, env, tmp_path):
    repo = make_repo(tmp_path, "list-settings")
    (repo / ".claude").mkdir()
    (repo / ".claude" / "settings.json").write_text("[1, 2]", encoding="utf-8")
    await add_project(db, "list-settings", [{"cwd": str(repo), "hostname": ""}])
    doc = await overview(db, env)
    assert repo_by_name(doc, "list-settings")["guard"]["settings_state"] == "unreadable"
    assert ("repos", "settings_unreadable") in error_pairs(doc)


async def test_settings_over_the_cap_is_unreadable(db, env, tmp_path, monkeypatch):
    repo = make_repo(tmp_path, "huge-settings")
    install_guard(repo, "enforce")
    monkeypatch.setattr(wo, "SETTINGS_MAX_BYTES", 16)
    await add_project(db, "huge-settings", [{"cwd": str(repo), "hostname": ""}])
    doc = await overview(db, env)
    assert repo_by_name(doc, "huge-settings")["guard"]["settings_state"] == "unreadable"


async def test_settings_path_that_is_a_directory_is_unreadable(db, env, tmp_path):
    repo = make_repo(tmp_path, "dir-settings")
    (repo / ".claude" / "settings.json").mkdir(parents=True)
    await add_project(db, "dir-settings", [{"cwd": str(repo), "hostname": ""}])
    doc = await overview(db, env)
    assert repo_by_name(doc, "dir-settings")["guard"]["settings_state"] == "unreadable"


async def test_repo_without_settings_is_absent_not_registered(db, env, tmp_path):
    repo = make_repo(tmp_path, "no-settings")
    await add_project(db, "no-settings", [{"cwd": str(repo), "hostname": ""}])
    doc = await overview(db, env)
    guard = repo_by_name(doc, "no-settings")["guard"]
    assert guard["settings_state"] == "absent"
    assert guard["registered"] is False
    assert guard["complete"] is False


async def test_pre_hook_absent_is_never_complete(db, env, tmp_path):
    repo = make_repo(tmp_path, "no-pre")
    install_guard(repo, "enforce", skip=("meridian_guard",))
    await add_project(db, "no-pre", [{"cwd": str(repo), "hostname": ""}])
    doc = await overview(db, env)
    guard = repo_by_name(doc, "no-pre")["guard"]
    assert "pre" not in guard["components"]
    assert guard["components"] == ["brief", "post"]
    assert guard["complete"] is False


async def test_user_scope_flag_comes_from_config(db, env, gdir, tmp_path):
    doc = await overview(db, env)
    assert doc["runtime"]["guard"]["user_scope_installed"] is None
    record_install(gdir, tmp_path / "user-probe", "enforce")
    cfg = json.loads((gdir / "config.json").read_text(encoding="utf-8"))
    cfg["installs"]["user"] = {"installed_at": "2026-09-26T19:00:00Z", "mode": "enforce"}
    (gdir / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    doc = await overview(db, env)
    assert doc["runtime"]["guard"]["user_scope_installed"] is True


async def test_malformed_config_is_null_with_config_error(db, env, gdir):
    (gdir / "config.json").write_text("{broken", encoding="utf-8")
    doc = await overview(db, env)
    assert doc["runtime"]["guard"]["user_scope_installed"] is None
    assert ("runtime", "config_unreadable") in error_pairs(doc)


async def test_config_over_the_cap_is_unreadable(db, env, gdir, monkeypatch):
    (gdir / "config.json").write_text(json.dumps({"installs": {}}), encoding="utf-8")
    monkeypatch.setattr(wo, "CONFIG_MAX_BYTES", 4)
    doc = await overview(db, env)
    assert doc["runtime"]["guard"]["user_scope_installed"] is None
    assert ("runtime", "config_unreadable") in error_pairs(doc)


async def test_install_record_drift_is_visible_and_only_two_fields_leak(db, env, gdir, tmp_path):
    repo = make_repo(tmp_path, "drift")
    install_guard(repo, "enforce")
    record_install(gdir, repo, "advisory")
    await add_project(db, "drift", [{"cwd": str(repo), "hostname": ""}])
    doc = await overview(db, env)
    guard = repo_by_name(doc, "drift")["guard"]
    assert guard["installed_mode"] == "enforce"
    assert guard["record"] == {"installed_at": "2026-09-26T19:00:00Z", "mode": "advisory"}
    assert guard["record_mode_matches"] is False
    dumped = json.dumps(doc)
    assert "copied_should_not_leak" not in dumped
    assert "pid-should-not-leak" not in dumped
    assert "copied_files" not in dumped
    assert "project_id_source" not in dumped


async def test_matching_install_record_matches(db, env, gdir, tmp_path):
    repo = make_repo(tmp_path, "match")
    install_guard(repo, "enforce")
    record_install(gdir, repo, "enforce")
    await add_project(db, "match", [{"cwd": str(repo), "hostname": ""}])
    doc = await overview(db, env)
    assert repo_by_name(doc, "match")["guard"]["record_mode_matches"] is True


async def test_guard_hooks_with_missing_scripts_report_problems(db, env, tmp_path):
    repo = make_repo(tmp_path, "missing-scripts")
    install_guard(repo, "enforce")
    (repo / ".claude" / "hooks" / "meridian_guard_brief.sh").unlink()
    await add_project(db, "missing-scripts", [{"cwd": str(repo), "hostname": ""}])
    doc = await overview(db, env)
    guard = repo_by_name(doc, "missing-scripts")["guard"]
    assert guard["runtime_ok"] is False
    assert any("brief missing" in problem for problem in guard["problems"])


async def test_guard_kill_switch_file_reports_off(db, env, gdir):
    (gdir / "guard.off").write_text("", encoding="utf-8")
    doc = await overview(db, env)
    assert doc["runtime"]["guard"]["mode"] == "off"
    assert doc["runtime"]["guard"]["mode_scope"] == "server_process"


async def test_guard_mode_env_off_reports_off(db, env):
    doc = await overview(db, env | {"MERIDIAN_GUARD": "off"})
    assert doc["runtime"]["guard"]["mode"] == "off"


async def test_disabled_rules_are_listed(db, env):
    doc = await overview(db, env | {"MERIDIAN_GUARD_DISABLE": "G3,g11"})
    assert doc["runtime"]["guard"]["disabled_rules"] == ["G11", "G3"]


# ---------------------------------------------------------------------------
# Managed codebase-memory entry (spec 3.5)
# ---------------------------------------------------------------------------


async def test_managed_entry_is_detected_by_key_and_never_echoes_values(db, env, tmp_path):
    repo = make_repo(tmp_path, "managed")
    managed_key = bundle._server_name("codebase_memory", repo)
    servers = {
        managed_key: {**bundle._node_command(["-y", "codebase-memory-mcp"], windows=False), "cwd": str(repo)},
        "other_server": {"command": "node", "args": ["x.js"], "env": {"TOKEN": SENTINEL_TOKEN}},
    }
    (repo / ".mcp.json").write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
    await add_project(db, "managed", [{"cwd": str(repo), "hostname": ""}])
    doc = await overview(db, env)
    assert repo_by_name(doc, "managed")["codebase_memory"]["managed_entry"] is True
    assert SENTINEL_TOKEN not in json.dumps(doc)


async def test_only_other_servers_is_not_managed(db, env, tmp_path):
    repo = make_repo(tmp_path, "other-only")
    (repo / ".mcp.json").write_text(json.dumps({"mcpServers": {"other_server": {"command": "node"}}}), encoding="utf-8")
    await add_project(db, "other-only", [{"cwd": str(repo), "hostname": ""}])
    doc = await overview(db, env)
    assert repo_by_name(doc, "other-only")["codebase_memory"]["managed_entry"] is False


async def test_mcp_json_that_parses_to_a_non_object_is_unreadable(db, env, tmp_path):
    repo = make_repo(tmp_path, "list-mcp")
    (repo / ".mcp.json").write_text("[1]", encoding="utf-8")
    await add_project(db, "list-mcp", [{"cwd": str(repo), "hostname": ""}])
    doc = await overview(db, env)
    assert repo_by_name(doc, "list-mcp")["codebase_memory"]["managed_entry"] is None
    assert ("repos", "mcp_unreadable") in error_pairs(doc)


async def test_mcp_without_servers_block_is_not_managed(db, env, tmp_path):
    repo = make_repo(tmp_path, "no-servers")
    (repo / ".mcp.json").write_text(json.dumps({"other": 1}), encoding="utf-8")
    await add_project(db, "no-servers", [{"cwd": str(repo), "hostname": ""}])
    doc = await overview(db, env)
    assert repo_by_name(doc, "no-servers")["codebase_memory"]["managed_entry"] is False


async def test_unreadable_mcp_json_gives_null_managed_entry(db, env, tmp_path):
    repo = make_repo(tmp_path, "bad-mcp")
    (repo / ".mcp.json").write_text("{nope", encoding="utf-8")
    await add_project(db, "bad-mcp", [{"cwd": str(repo), "hostname": ""}])
    doc = await overview(db, env)
    assert repo_by_name(doc, "bad-mcp")["codebase_memory"]["managed_entry"] is None
    assert ("repos", "mcp_unreadable") in error_pairs(doc)


async def test_mcp_json_over_the_cap_gives_null_managed_entry(db, env, tmp_path, monkeypatch):
    repo = make_repo(tmp_path, "big-mcp")
    (repo / ".mcp.json").write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    monkeypatch.setattr(wo, "MCP_MAX_BYTES", 4)
    await add_project(db, "big-mcp", [{"cwd": str(repo), "hostname": ""}])
    doc = await overview(db, env)
    assert repo_by_name(doc, "big-mcp")["codebase_memory"]["managed_entry"] is None


# ---------------------------------------------------------------------------
# Repo identity (spec 4)
# ---------------------------------------------------------------------------


def test_three_spellings_of_one_path_share_a_key():
    spellings = [r"C:\Users\x\repo", "c:/users/x/repo/", "/c/Users/x/repo"]
    keys = {reg.path_key(reg.norm_path(s, msys=True)) for s in spellings}
    assert keys == {"c:/users/x/repo"}


def test_install_key_with_drive_prefix_matches_the_repo_key(gdir):
    record = {"scope": "project", "mode": "enforce", "installed_at": "2026-09-26T19:00:00Z"}
    gdir.mkdir(parents=True, exist_ok=True)
    op = hsm._config_op(gdir, "project:C:/Users/x/repo", record)
    op.path.write_text(op.content, encoding="utf-8")
    cfg = wo._read_config(str(gdir), wo._Budget(wo.SCAN_BUDGET_BYTES, time.monotonic() + 10), [])
    assert reg.path_key(reg.norm_path("C:/Users/x/repo", msys=True)) in cfg["installs"]


async def test_cwd_inside_a_repo_maps_to_the_root_row(db, env, tmp_path):
    root = make_repo(tmp_path, "monorepo")
    src = root / "src"
    src.mkdir()
    await add_project(db, "monorepo", [{"cwd": str(src), "hostname": ""}])
    doc = await overview(db, env)
    assert len(doc["repos"]["items"]) == 1
    item = doc["repos"]["items"][0]
    assert item["key"] == reg.path_key(reg.norm_path(str(root), msys=True))
    assert reg.norm_path(str(src), msys=True) in item["source_paths"]


async def test_worktree_inside_a_subdirectory_resolves_to_the_root_key(db, env, tmp_path):
    root = make_repo(tmp_path, "wt-root")
    src = root / "src"
    src.mkdir()
    proj = await add_project(db, "wt-root", [{"cwd": str(root), "hostname": ""}])
    session = await add_session(db, proj["id"], "wt-session", status="active", last_seen=NOW - 30)
    await dbm.register_worktree(db, session["id"], proj["id"], "feat/x", str(src))
    doc = await overview(db, env)
    row = doc["sessions"]["items"][0]
    assert row["worktree"]["repo_key"] == reg.path_key(reg.norm_path(str(root), msys=True))


async def test_no_git_above_the_path_gives_the_gap_and_a_normalized_key(db, env, tmp_path):
    loose = tmp_path / "no-git-here"
    loose.mkdir()
    await add_project(db, "loose", [{"cwd": str(loose), "hostname": ""}])
    doc = await overview(db, env)
    item = repo_by_name(doc, "no-git-here")
    assert item["key"] == reg.path_key(reg.norm_path(str(loose), msys=True))
    assert item["canonical"] == {"worktree_root": None, "canonical_root": None, "linked": None, "canonical_key": None}
    assert "repo_root_unresolved" in gap_codes(doc)


async def test_repo_path_that_does_not_exist_is_a_normalized_key(db, env, tmp_path):
    ghost = tmp_path / "ghost-repo"
    await add_project(db, "ghost", [{"cwd": str(ghost), "hostname": ""}])
    doc = await overview(db, env)
    item = repo_by_name(doc, "ghost-repo")
    assert item["codebase_memory"]["managed_entry"] is False
    assert item["guard"]["settings_state"] == "absent"


async def test_legacy_repo_path_is_a_candidate_source(db, env, tmp_path):
    legacy = make_repo(tmp_path, "legacy")
    proj = await dbm.create_project(db, "legacy-proj")
    await dbm.set_executor_config(db, proj["id"], {"repo_path": str(legacy)})
    doc = await overview(db, env)
    item = repo_by_name(doc, "legacy")
    assert item["sources"] == ["project_repo_paths_legacy"]
    assert item["hostnames"] == []


async def test_legacy_repo_path_is_ignored_once_repo_paths_exists(db, env, tmp_path):
    legacy = make_repo(tmp_path, "ignored-legacy")
    proj = await dbm.create_project(db, "mixed")
    await dbm.set_executor_config(db, proj["id"], {"repo_path": str(legacy), "repo_paths": []})
    doc = await overview(db, env)
    assert doc["repos"]["items"] == []


async def test_executor_config_as_json_text_is_decoded(db, env, tmp_path):
    text_repo = make_repo(tmp_path, "text-config")
    proj = await dbm.create_project(db, "text-config")
    await db.execute(
        "UPDATE projects SET executor_config = ? WHERE id = ?",
        (json.dumps({"repo_paths": [{"cwd": str(text_repo), "hostname": ""}]}), proj["id"]),
    )
    await db.commit()
    doc = await overview(db, env)
    assert repo_by_name(doc, "text-config")["projects"] == [{"id": proj["id"], "name": "text-config"}]


async def test_malformed_executor_config_is_ignored(db, env, tmp_path):
    proj = await dbm.create_project(db, "broken-config")
    await db.execute("UPDATE projects SET executor_config = ? WHERE id = ?", ("{oops", proj["id"]))
    await db.commit()
    doc = await overview(db, env)
    assert doc["repos"]["items"] == []
    assert doc["errors"] == []


async def test_non_dict_executor_config_is_ignored(db, env, tmp_path):
    proj = await dbm.create_project(db, "list-config")
    await db.execute("UPDATE projects SET executor_config = ? WHERE id = ?", (json.dumps([1, 2]), proj["id"]))
    await db.commit()
    doc = await overview(db, env)
    assert doc["repos"]["items"] == []


async def test_repo_path_entries_that_are_not_usable_are_skipped(db, env, tmp_path):
    good = make_repo(tmp_path, "good-entry")
    proj = await dbm.create_project(db, "messy")
    await dbm.set_executor_config(db, proj["id"], {"repo_paths": [
        "not-a-dict", {"cwd": "   "}, {"cwd": 42}, {"cwd": str(good), "hostname": ""},
    ]})
    doc = await overview(db, env)
    assert [item["name"] for item in doc["repos"]["items"]] == ["good-entry"]


async def test_relative_candidate_paths_are_skipped(db, env, tmp_path):
    proj = await dbm.create_project(db, "relative")
    await dbm.set_executor_config(db, proj["id"], {"repo_paths": [{"cwd": "relative/path", "hostname": ""}]})
    doc = await overview(db, env)
    assert doc["repos"]["items"] == []


async def test_subscan_and_snapshot_rows_join_by_key(db, env, gdir, cache, tmp_path):
    repo = make_repo(tmp_path, "joined")
    make_index(cache, "joined", repo)
    write_snapshot(cache, gdir, env)
    doc = await overview(db, env)
    item = repo_by_name(doc, "joined")
    assert item["sources"] == ["index_snapshot"]
    assert item["codebase_memory"]["index"]["mode"] == "own"


async def test_install_record_adds_a_repo_candidate(db, env, gdir, tmp_path):
    repo = make_repo(tmp_path, "from-install")
    record_install(gdir, repo, "enforce")
    doc = await overview(db, env)
    item = repo_by_name(doc, "from-install")
    assert item["sources"] == ["guard_install_record"]


# ---------------------------------------------------------------------------
# Hostnames and location (spec 5.4)
# ---------------------------------------------------------------------------


async def test_recorded_hostnames_drive_location_match(db, env, tmp_path, monkeypatch):
    monkeypatch.setattr(wo, "_local_hostname", lambda: "host")
    same = make_repo(tmp_path, "same")
    empty = make_repo(tmp_path, "empty-host")
    unknown = make_repo(tmp_path, "unknown-host")
    other = make_repo(tmp_path, "other-host")
    long_name = make_repo(tmp_path, "long-host")
    proj = await dbm.create_project(db, "hosts")
    await dbm.set_executor_config(db, proj["id"], {"repo_paths": [
        {"cwd": str(same), "hostname": "HOST.corp.local"},
        {"cwd": str(empty), "hostname": ""},
        {"cwd": str(unknown), "hostname": "unknown"},
        {"cwd": str(other), "hostname": "other"},
        {"cwd": str(long_name), "hostname": "averyveryveryverylonghostname-1234567890"},
    ]})
    doc = await overview(db, env)
    assert repo_by_name(doc, "same")["location"] == {"match": "same_machine", "hostnames": ["host"]}
    assert repo_by_name(doc, "empty-host")["location"]["match"] == "unknown"
    assert repo_by_name(doc, "unknown-host")["location"]["match"] == "unknown"
    assert repo_by_name(doc, "other-host")["location"]["match"] == "other_machine"
    assert repo_by_name(doc, "long-host")["location"] == {
        "match": "other_machine", "hostnames": ["averyveryveryverylonghostname-1234567890"],
    }
    assert "hostname_unverified" in gap_codes(doc)


async def test_unknown_local_hostname_makes_every_location_unknown(db, env, tmp_path, monkeypatch):
    monkeypatch.setattr(wo, "_local_hostname", lambda: None)
    repo = make_repo(tmp_path, "nohost")
    proj = await dbm.create_project(db, "nohost")
    await dbm.set_executor_config(db, proj["id"], {"repo_paths": [{"cwd": str(repo), "hostname": "box"}]})
    doc = await overview(db, env)
    assert doc["runtime"]["hostname"] is None
    assert repo_by_name(doc, "nohost")["location"]["match"] == "unknown"


def test_short_host_and_recorded_host_rules():
    assert wo._short_host(" HOST.corp.local ") == "host"
    assert wo._recorded_host(None) is None
    assert wo._recorded_host("") is None
    assert wo._recorded_host("UNKNOWN") is None
    assert wo._recorded_host("Box.Local") == "box"


def test_local_hostname_reads_the_first_label(monkeypatch):
    monkeypatch.setattr(wo.socket, "gethostname", lambda: "LAPTOP.corp.local")
    assert wo._local_hostname() == "laptop"
    monkeypatch.setattr(wo.socket, "gethostname", lambda: "")
    assert wo._local_hostname() is None

    def boom():
        raise OSError("no name")

    monkeypatch.setattr(wo.socket, "gethostname", boom)
    assert wo._local_hostname() is None


# ---------------------------------------------------------------------------
# Guard event activity (spec 3.6)
# ---------------------------------------------------------------------------


def _audit_line(ts: int, root: str, decision: str = "deny", rule: str = "G3") -> str:
    return json.dumps({
        "ts": ts, "event": "PreToolUse", "rule": rule, "decision": decision,
        "tool": "Grep", "root": root, "session": "s1",
    })


async def test_audit_events_attach_by_repo_root_and_index_root(db, env, gdir, cache, tmp_path):
    repo = make_repo(tmp_path, "audited")
    make_index(cache, "audited", repo)
    write_snapshot(cache, gdir, env)
    await add_project(db, "audited", [{"cwd": str(repo), "hostname": ""}])
    other = make_repo(tmp_path, "unrelated")
    lines = [
        _audit_line(NOW - 500, str(repo)),
        _audit_line(NOW - 100, str(other), decision="inject"),
        _audit_line(NOW - 200, str(repo), decision="allow-escape"),
    ]
    (gdir / "audit.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
    doc = await overview(db, env)
    activity = repo_by_name(doc, "audited")["activity"]
    assert activity["last_guard_event_at"] == _iso(NOW - 200)
    assert activity["last_guard_event_reason"] is None
    assert "guard_event_root_is_index_root" in gap_codes(doc)


async def test_audit_event_on_the_index_root_counts_for_a_nested_repo(db, env, gdir, cache, tmp_path):
    # Not a git repo: a git parent would make the child resolve to it (spec 4.3), not to an ancestor index.
    parent = make_repo(tmp_path, "indexed-parent", git=False)
    make_index(cache, "indexed-parent", parent)
    child = parent / "child"
    child.mkdir()
    write_snapshot(cache, gdir, env)
    await add_project(db, "child-proj", [{"cwd": str(child), "hostname": ""}])
    (gdir / "audit.log").write_text(_audit_line(NOW - 50, str(parent)) + "\n", encoding="utf-8")
    doc = await overview(db, env)
    item = repo_by_name(doc, "child")
    assert item["codebase_memory"]["index"]["mode"] == "ancestor"
    assert item["activity"]["last_guard_event_at"] == _iso(NOW - 50)


async def test_no_audit_log_reason(db, env, tmp_path):
    repo = make_repo(tmp_path, "no-log")
    await add_project(db, "no-log", [{"cwd": str(repo), "hostname": ""}])
    doc = await overview(db, env)
    activity = repo_by_name(doc, "no-log")["activity"]
    assert activity["last_guard_event_at"] is None
    assert activity["last_guard_event_reason"] == "no audit.log in the guard dir"


async def test_audit_log_without_a_match_reason(db, env, gdir, tmp_path):
    repo = make_repo(tmp_path, "quiet")
    (gdir / "audit.log").write_text(_audit_line(NOW - 10, str(tmp_path / "elsewhere")) + "\n", encoding="utf-8")
    await add_project(db, "quiet", [{"cwd": str(repo), "hostname": ""}])
    doc = await overview(db, env)
    assert repo_by_name(doc, "quiet")["activity"]["last_guard_event_reason"] == (
        "no guard event recorded for this repo in audit.log"
    )


async def test_match_beyond_the_256_kib_tail_is_unknown(db, env, gdir, tmp_path):
    repo = make_repo(tmp_path, "buried")
    first = _audit_line(NOW - 999, str(repo)) + "\n"
    filler = (json.dumps({"ts": NOW, "root": str(tmp_path / "noise"), "rule": "G9"}) + "\n")
    count = (wo.AUDIT_TAIL_BYTES * 2) // len(filler) + 1
    (gdir / "audit.log").write_text(first + filler * count, encoding="utf-8")
    await add_project(db, "buried", [{"cwd": str(repo), "hostname": ""}])
    doc = await overview(db, env)
    activity = repo_by_name(doc, "buried")["activity"]
    assert activity["last_guard_event_at"] is None
    assert activity["last_guard_event_reason"] == "unknown: not in the last 256 KiB of audit.log"
    assert "guard_audit_tail_limited" in gap_codes(doc)


async def test_unreadable_audit_log_reports_the_error(db, env, gdir, tmp_path):
    repo = make_repo(tmp_path, "unreadable-log")
    (gdir / "audit.log").mkdir()
    await add_project(db, "unreadable-log", [{"cwd": str(repo), "hostname": ""}])
    doc = await overview(db, env)
    assert repo_by_name(doc, "unreadable-log")["activity"]["last_guard_event_reason"] == (
        "unknown: audit.log unreadable"
    )
    assert ("repos", "audit_unreadable") in error_pairs(doc)


async def test_audit_lines_with_bad_shapes_are_skipped(db, env, gdir, tmp_path):
    repo = make_repo(tmp_path, "shapes")
    lines = [
        "", "not json", "[1, 2]",
        json.dumps({"root": "", "ts": 1}),
        json.dumps({"root": str(repo), "ts": True}),
        json.dumps({"root": str(repo), "ts": "later"}),
        json.dumps({"root": 7, "ts": 1}),
        _audit_line(NOW - 30, str(repo)),
    ]
    (gdir / "audit.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
    await add_project(db, "shapes", [{"cwd": str(repo), "hostname": ""}])
    doc = await overview(db, env)
    assert repo_by_name(doc, "shapes")["activity"]["last_guard_event_at"] == _iso(NOW - 30)


async def test_non_finite_audit_timestamps_are_malformed_lines(db, env, gdir, tmp_path):
    """Spec 3.6, review R2: NaN and Infinity are not timestamps, so those audit lines are skipped."""
    repo = make_repo(tmp_path, "nonfinite-audit")
    lines = [
        json.dumps({"root": str(repo), "ts": float("nan")}),
        json.dumps({"root": str(repo), "ts": float("inf")}),
        json.dumps({"root": str(repo), "ts": float("-inf")}),
    ]
    (gdir / "audit.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
    await add_project(db, "nonfinite-audit", [{"cwd": str(repo), "hostname": ""}])
    doc = await overview(db, env)
    activity = repo_by_name(doc, "nonfinite-audit")["activity"]
    assert activity["last_guard_event_at"] is None
    assert activity["last_guard_event_reason"] == wo._AUDIT_NO_EVENT
    json.dumps(doc, allow_nan=False)


async def test_an_event_time_that_cannot_render_keeps_a_reason(db, env, gdir, tmp_path):
    """Review R2: an event whose time has no ISO form gets the fixed malformed reason, never null."""
    repo = make_repo(tmp_path, "huge-audit")
    (gdir / "audit.log").write_text(json.dumps({"root": str(repo), "ts": 10 ** 20}) + "\n", encoding="utf-8")
    await add_project(db, "huge-audit", [{"cwd": str(repo), "hostname": ""}])
    doc = await overview(db, env)
    activity = repo_by_name(doc, "huge-audit")["activity"]
    assert activity["last_guard_event_at"] is None
    assert activity["last_guard_event_reason"] == wo._AUDIT_EVENT_MALFORMED
    json.dumps(doc, allow_nan=False)


@pytest.mark.parametrize("unrenderable_first", [True, False], ids=["huge_then_valid", "valid_then_huge"])
async def test_an_unrenderable_time_never_hides_an_earlier_valid_event(
    db, env, gdir, tmp_path, unrenderable_first,
):
    """N2: a line whose time cannot render is counted only for the reason, so the valid event still shows."""
    repo = make_repo(tmp_path, "huge-plus-valid")
    huge = json.dumps({"root": str(repo), "ts": 10 ** 20})
    valid = _audit_line(NOW - 100, str(repo))
    lines = [huge, valid] if unrenderable_first else [valid, huge]
    (gdir / "audit.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
    await add_project(db, "huge-plus-valid", [{"cwd": str(repo), "hostname": ""}])
    doc = await overview(db, env)
    activity = repo_by_name(doc, "huge-plus-valid")["activity"]
    assert activity["last_guard_event_at"] == _iso(NOW - 100)
    assert activity["last_guard_event_reason"] is None
    json.dumps(doc, allow_nan=False)


async def test_an_unrenderable_event_time_keeps_a_reason_on_a_degraded_repo(db, env, gdir, tmp_path, monkeypatch):
    """Review R2, degraded path: the same rule holds when the repo's facts raised."""
    repo = make_repo(tmp_path, "degraded-audit")
    (gdir / "audit.log").write_text(json.dumps({"root": str(repo), "ts": 10 ** 20}) + "\n", encoding="utf-8")
    await add_project(db, "degraded-audit", [{"cwd": str(repo), "hostname": ""}])

    def boom(key, root, unresolved, ctx):
        raise RuntimeError("boom")

    monkeypatch.setattr(wo, "_repo_facts", boom)
    doc = await overview(db, env)
    activity = repo_by_name(doc, "degraded-audit")["activity"]
    assert activity["last_guard_event_at"] is None
    assert activity["last_guard_event_reason"] == wo._AUDIT_EVENT_MALFORMED
    json.dumps(doc, allow_nan=False)


async def test_non_finite_snapshot_rows_are_dropped_like_unreadable_rows(db, env, gdir, tmp_path):
    """Review R1: NaN or Infinity in a row's stamp or count drops that row. The snapshot stays ok."""
    repo = make_repo(tmp_path, "nonfinite-row")
    root = str(repo).replace("\\", "/")
    root_key = reg.path_key(reg.norm_path(root))
    rows = [
        {"name": "nan-row", "root": root, "root_key": root_key, "db": str(tmp_path / "a.db"),
         "indexed_epoch": float("nan"), "nodes": 5},
        {"name": "inf-row", "root": root, "root_key": root_key, "db": str(tmp_path / "b.db"),
         "indexed_epoch": 1.0, "nodes": float("inf")},
    ]
    (gdir / "snapshot.json").write_text(json.dumps({"schema": reg.SNAPSHOT_SCHEMA, "rows": rows}), encoding="utf-8")
    doc = await overview(db, env)
    assert doc["repos"]["snapshot"]["error"] is None
    assert doc["repos"]["snapshot"]["rows_total"] == 0
    json.dumps(doc, allow_nan=False)


async def test_a_non_finite_value_from_a_probe_is_null_in_the_document(db, env, gdir, cache, tmp_path, monkeypatch):
    """Review R1 backstop: a NaN from any step becomes null, so the document is always strict JSON."""
    repo = make_repo(tmp_path, "nan-age")
    make_index(cache, "nan-age", repo)
    write_snapshot(cache, gdir, env)
    await add_project(db, "nan-age", [{"cwd": str(repo), "hostname": ""}])
    real_freshness = reg.freshness

    def nan_age(row, fs, now):
        out = real_freshness(row, fs, now)
        out["age_days"] = float("nan")
        return out

    monkeypatch.setattr(reg, "freshness", nan_age)
    doc = await overview(db, env)
    assert repo_by_name(doc, "nan-age")["codebase_memory"]["index"]["age_days"] is None
    json.dumps(doc, allow_nan=False)


def test_json_safe_turns_non_finite_numbers_into_null_at_every_depth():
    value = {
        "a": float("nan"), "b": [1.5, float("inf"), {"c": -float("inf")}],
        "d": (float("nan"),), "e": 2, "f": "x", "g": True,
    }
    assert wo._json_safe(value) == {"a": None, "b": [1.5, None, {"c": None}], "d": [None], "e": 2, "f": "x", "g": True}


def test_epoch_seconds_render_as_utc_iso():
    expected = datetime.fromtimestamp(1790000000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert wo._iso(1790000000) == expected
    assert wo._iso(10 ** 20) is None


async def test_prospect_receipt_attributes_to_a_repo_within_seven_days(db, env, tmp_path, monkeypatch):
    repo = make_repo(tmp_path, "receipted")
    proj = await add_project(db, "receipted", [{"cwd": str(repo), "hostname": ""}])
    fresh = await dbm.record_action_audit_event(
        db, receipts.RECEIPT_EVENT_TYPE, project_id=proj["id"],
        detail=json.dumps({"repo_root": str(repo), "repo_source": "explicit_root_dir"}),
    )
    await db.execute("UPDATE action_audit_log SET created_at = ? WHERE id = ?", (_ts(NOW - DAY), fresh["id"]))
    await db.commit()

    spy_calls = []
    real = dbm.get_action_audit_log

    async def spy(db_arg, **kwargs):
        spy_calls.append(kwargs)
        return await real(db_arg, **kwargs)

    monkeypatch.setattr(dbm, "get_action_audit_log", spy)
    doc = await overview(db, env)
    assert repo_by_name(doc, "receipted")["activity"]["last_prospect_receipt_at"] == _iso(NOW - DAY)
    assert repo_by_name(doc, "receipted")["activity"]["receipt_window_days"] == 7
    call = spy_calls[0]
    assert call["event_type"] == receipts.RECEIPT_EVENT_TYPE
    assert call["limit"] == 500
    assert call["since"] == _ts(NOW - 7 * DAY)


async def test_receipt_older_than_seven_days_does_not_count(db, env, tmp_path):
    repo = make_repo(tmp_path, "stale-receipt")
    proj = await add_project(db, "stale-receipt", [{"cwd": str(repo), "hostname": ""}])
    old = await dbm.record_action_audit_event(
        db, receipts.RECEIPT_EVENT_TYPE, project_id=proj["id"],
        detail=json.dumps({"repo_root": str(repo), "repo_source": "explicit_root_dir"}),
    )
    await db.execute("UPDATE action_audit_log SET created_at = ? WHERE id = ?", (_ts(NOW - 9 * DAY), old["id"]))
    await db.commit()
    doc = await overview(db, env)
    assert repo_by_name(doc, "stale-receipt")["activity"]["last_prospect_receipt_at"] is None


async def test_default_root_receipts_are_not_attributed(db, env, tmp_path):
    repo = make_repo(tmp_path, "default-root")
    proj = await add_project(db, "default-root", [{"cwd": str(repo), "hostname": ""}])
    await dbm.record_action_audit_event(
        db, receipts.RECEIPT_EVENT_TYPE, project_id=proj["id"],
        detail=json.dumps({"repo_root": str(repo), "repo_source": "default_repo_root"}),
    )
    doc = await overview(db, env)
    assert repo_by_name(doc, "default-root")["activity"]["last_prospect_receipt_at"] is None
    assert "receipts_default_root_excluded" in gap_codes(doc)


async def test_session_worktree_receipts_count_and_bad_details_are_skipped(db, env, tmp_path):
    repo = make_repo(tmp_path, "wt-receipt")
    proj = await add_project(db, "wt-receipt", [{"cwd": str(repo), "hostname": ""}])
    await dbm.record_action_audit_event(
        db, receipts.RECEIPT_EVENT_TYPE, project_id=proj["id"],
        detail=json.dumps({"repo_root": str(repo), "repo_source": "session_worktree"}),
    )
    await dbm.record_action_audit_event(
        db, receipts.RECEIPT_EVENT_TYPE, project_id=proj["id"], detail="not json",
    )
    await dbm.record_action_audit_event(
        db, receipts.RECEIPT_EVENT_TYPE, project_id=proj["id"], detail=json.dumps(["list"]),
    )
    await dbm.record_action_audit_event(
        db, receipts.RECEIPT_EVENT_TYPE, project_id=proj["id"], detail=json.dumps({"repo_source": "explicit_root_dir"}),
    )
    doc = await overview(db, env)
    assert repo_by_name(doc, "wt-receipt")["activity"]["last_prospect_receipt_at"] is not None


async def test_receipt_window_truncation_sets_the_gap(db, env, tmp_path, monkeypatch):
    monkeypatch.setattr(wo, "MAX_RECEIPTS_PER_PROJECT", 2)
    repo = make_repo(tmp_path, "many-receipts")
    proj = await add_project(db, "many-receipts", [{"cwd": str(repo), "hostname": ""}])
    for _ in range(2):
        await dbm.record_action_audit_event(
            db, receipts.RECEIPT_EVENT_TYPE, project_id=proj["id"],
            detail=json.dumps({"repo_root": str(repo), "repo_source": "explicit_root_dir"}),
        )
    doc = await overview(db, env)
    assert "receipts_window_truncated" in gap_codes(doc)


async def test_receipt_query_failure_is_reported_and_other_sections_survive(db, env, tmp_path, monkeypatch):
    repo = make_repo(tmp_path, "receipt-fail")
    proj = await add_project(db, "receipt-fail", [{"cwd": str(repo), "hostname": ""}])
    await add_session(db, proj["id"], "still-here", status="active", last_seen=NOW - 30)

    async def broken(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(dbm, "get_action_audit_log", broken)
    doc = await overview(db, env)
    assert ("repos", "receipts_unavailable") in error_pairs(doc)
    assert repo_by_name(doc, "receipt-fail")["activity"]["last_prospect_receipt_at"] is None
    assert len(doc["sessions"]["items"]) == 1
    assert doc["projects"]["items"][0]["activity"] == "live"


# ---------------------------------------------------------------------------
# Sessions, projects and liveness (spec 3.4)
# ---------------------------------------------------------------------------

_VECTOR_FILE = Path(__file__).resolve().parent / "fixtures" / "workspace_live_vectors.json"
_VECTOR_DOC = json.loads(_VECTOR_FILE.read_text(encoding="utf-8"))
# (name, now, last_seen, status, expected_live, expected_age_s). The same file is read by the
# vitest parity test, so the two sides cannot drift (spec 9.4, review M4).
PARITY_VECTORS = [
    (v["name"], _VECTOR_DOC["now"], v["last_seen"], v["status"], v["expected_live"], v["expected_age_s"])
    for v in _VECTOR_DOC["vectors"]
]


def _iso_to_epoch(value: str) -> float:
    return datetime.strptime(value.replace("Z", ""), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()


@pytest.mark.parametrize("name,now_s,last_seen,status,expected_live,expected_age", PARITY_VECTORS, ids=[v[0] for v in PARITY_VECTORS])
def test_session_liveness_matches_the_shared_vectors(name, now_s, last_seen, status, expected_live, expected_age):
    now_dt = datetime.fromtimestamp(_iso_to_epoch(now_s), tz=timezone.utc)
    view = wo._session_view(
        {"id": "s", "name": "n", "status": status, "last_seen": last_seen},
        "p", "proj", now_dt, None, {},
    )
    assert view["live"] is expected_live
    assert view["age_s"] == expected_age


def test_live_window_mirrors_the_dashboard_constant():
    assert wo.LIVE_WINDOW_S * 1000 == 600000
    assert _VECTOR_DOC["live_window_s"] == wo.LIVE_WINDOW_S
    source = (Path(__file__).resolve().parents[1] / "meridian" / "static" / "dashboard-utils.ts").read_text(encoding="utf-8")
    assert re.search(r"10\s*\*\s*60\s*\*\s*1000", source)


@pytest.mark.parametrize("value,expected", [
    (None, None),
    (42, None),
    ("", None),
    ("   ", None),
    ("2026-10-27 11:50:00", datetime(2026, 10, 27, 11, 50, tzinfo=timezone.utc)),
    ("2026-10-27T11:50:00", datetime(2026, 10, 27, 11, 50, tzinfo=timezone.utc)),
    (datetime(2026, 10, 27, 11, 50), datetime(2026, 10, 27, 11, 50, tzinfo=timezone.utc)),
    (datetime(2026, 10, 27, 13, 50, tzinfo=timezone(timedelta(hours=2))), datetime(2026, 10, 27, 11, 50, tzinfo=timezone.utc)),
])
def test_parse_last_seen_forms(value, expected):
    assert wo.parse_last_seen(value) == expected


async def test_project_activity_covers_live_recent_idle_none_and_closed_only(db, env):
    live = await add_project(db, "p-live")
    await add_session(db, live["id"], "a", status="active", last_seen=NOW - 60)
    recent = await add_project(db, "p-recent")
    await add_session(db, recent["id"], "b", status="active", last_seen=NOW - 2 * 3600)
    idle = await add_project(db, "p-idle")
    await add_session(db, idle["id"], "c", status="idle", last_seen=NOW - 30 * 3600)
    await add_project(db, "p-none")
    closed = await add_project(db, "p-closed")
    await add_session(db, closed["id"], "d", status="closed", last_seen=NOW - 60)
    doc = await overview(db, env)
    activity = {p["name"]: p["activity"] for p in doc["projects"]["items"]}
    assert activity == {"p-live": "live", "p-recent": "recent", "p-idle": "idle", "p-none": "none", "p-closed": "none"}
    counts = {p["name"]: p["session_counts"] for p in doc["projects"]["items"]}
    assert counts["p-closed"] == {"total": 1, "live": 0, "status_active": 0, "status_idle": 0}
    assert all(p["activity_source"] == "sessions.last_seen" for p in doc["projects"]["items"])
    assert "project_status_manual" in gap_codes(doc)


async def test_project_status_is_reported_when_valid_and_null_otherwise(db, env, monkeypatch):
    await add_project(db, "parked-one", status="parked")
    doc = await overview(db, env)
    assert doc["projects"]["items"][0]["status"] == "parked"
    real = dbm.list_projects

    async def with_unexpected_status(db_arg):
        rows = await real(db_arg)
        for row in rows:
            row["status"] = "weird"
        return rows

    monkeypatch.setattr(dbm, "list_projects", with_unexpected_status)
    doc = await overview(db, env)
    assert doc["projects"]["items"][0]["status"] is None


async def test_closed_session_with_a_worktree_is_listed(db, env, tmp_path):
    repo = make_repo(tmp_path, "closed-wt")
    proj = await add_project(db, "closed-wt", [{"cwd": str(repo), "hostname": ""}])
    session = await add_session(db, proj["id"], "closed-with-wt", status="closed", last_seen=NOW - 60)
    await dbm.register_worktree(db, session["id"], proj["id"], "main", str(repo))
    closed_no_wt = await add_session(db, proj["id"], "closed-plain", status="closed", last_seen=NOW - 60)
    doc = await overview(db, env)
    names = [item["name"] for item in doc["sessions"]["items"]]
    assert names == ["closed-with-wt"]
    assert closed_no_wt["id"] not in [item["id"] for item in doc["sessions"]["items"]]


async def test_session_items_are_newest_first_and_carry_location_unknown(db, env):
    proj = await add_project(db, "order")
    await add_session(db, proj["id"], "older", status="active", last_seen=NOW - 120)
    await add_session(db, proj["id"], "newer", status="idle", last_seen=NOW - 5)
    doc = await overview(db, env)
    items = doc["sessions"]["items"]
    assert [item["name"] for item in items] == ["newer", "older"]
    assert all(item["location"] == "unknown" for item in items)
    assert items[1]["live"] is True
    assert items[0]["live"] is False
    assert doc["sessions"]["totals"] == {"total": 2, "live": 1, "status_active": 1, "status_idle": 1}


async def test_clock_skew_is_not_live_and_not_recent(db, env):
    proj = await add_project(db, "skewed")
    await add_session(db, proj["id"], "future", status="active", last_seen=NOW + 5)
    doc = await overview(db, env)
    item = doc["sessions"]["items"][0]
    assert item["live"] is False
    assert item["age_s"] == -5.0
    assert doc["projects"]["items"][0]["activity"] == "idle"


async def test_unparseable_last_seen_is_unknown_age(db, env):
    proj = await add_project(db, "garbled")
    session = await dbm.register_session(db, proj["id"], "garbled-session")
    await db.execute("UPDATE sessions SET last_seen = ? WHERE id = ?", ("not a time", session["id"]))
    await db.commit()
    doc = await overview(db, env)
    item = doc["sessions"]["items"][0]
    assert item["age_s"] is None
    assert item["last_seen_utc"] is None
    assert item["live"] is False


async def test_session_caps_and_totals_over_the_limit(db, env, monkeypatch):
    monkeypatch.setattr(wo, "MAX_SESSIONS", 2)
    proj = await add_project(db, "capped")
    for i in range(3):
        await add_session(db, proj["id"], f"s{i}", status="active", last_seen=NOW - 10 * (i + 1))
    doc = await overview(db, env)
    assert len(doc["sessions"]["items"]) == 2
    assert doc["sessions"]["total"] == 3
    assert doc["sessions"]["truncated"] is True
    assert doc["sessions"]["totals"]["total"] == 3


async def test_repo_cap_reports_total_and_truncation(db, env, tmp_path, monkeypatch):
    monkeypatch.setattr(wo, "MAX_REPOS", 2)
    paths = [make_repo(tmp_path, f"capped-{i}") for i in range(3)]
    await add_project(db, "many-repos", [{"cwd": str(p), "hostname": ""} for p in paths])
    doc = await overview(db, env)
    assert doc["repos"]["total"] == 3
    assert doc["repos"]["truncated"] is True
    assert doc["repos"]["limit"] == 2
    assert len(doc["repos"]["items"]) == 2


async def test_session_and_project_rows_for_a_failed_session_query_are_unknown(db, env, monkeypatch):
    good = await add_project(db, "good-sessions")
    await add_session(db, good["id"], "fine", status="active", last_seen=NOW - 30)
    await add_project(db, "bad-sessions")
    real = dbm.get_sessions

    async def flaky(db_arg, project_id, **kwargs):
        if project_id != good["id"]:
            raise RuntimeError("boom")
        return await real(db_arg, project_id, **kwargs)

    monkeypatch.setattr(dbm, "get_sessions", flaky)
    doc = await overview(db, env)
    rows = {p["name"]: p for p in doc["projects"]["items"]}
    assert rows["bad-sessions"]["session_counts"] is None
    assert rows["bad-sessions"]["activity"] is None
    assert rows["good-sessions"]["activity"] == "live"
    assert ("sessions", "db_error") in error_pairs(doc)


async def test_failed_project_list_gives_unknown_session_totals(db, env, monkeypatch):
    async def broken(db_arg):
        raise RuntimeError("boom")

    monkeypatch.setattr(dbm, "list_projects", broken)
    doc = await overview(db, env)
    assert doc["projects"]["items"] == []
    assert doc["sessions"]["total"] is None
    assert doc["sessions"]["totals"] is None
    assert ("projects", "db_error") in error_pairs(doc)


async def test_failed_worktree_query_is_reported_under_repos(db, env, monkeypatch):
    await add_project(db, "wt-fail")

    async def broken(db_arg, project_id):
        raise RuntimeError("boom")

    monkeypatch.setattr(dbm, "list_active_worktrees", broken)
    doc = await overview(db, env)
    assert ("repos", "db_error") in error_pairs(doc)


async def test_project_repo_keys_and_name_sorting(db, env, tmp_path):
    a = make_repo(tmp_path, "aaa-repo")
    b = make_repo(tmp_path, "bbb-repo")
    await add_project(db, "zeta", [{"cwd": str(b), "hostname": ""}])
    await add_project(db, "Alpha", [{"cwd": str(a), "hostname": ""}])
    doc = await overview(db, env)
    assert [p["name"] for p in doc["projects"]["items"]] == ["Alpha", "zeta"]
    assert doc["projects"]["items"][0]["repo_keys"] == [reg.path_key(reg.norm_path(str(a), msys=True))]


# ---------------------------------------------------------------------------
# Tunnel signals (spec 5.2)
# ---------------------------------------------------------------------------


def _claim_dir(home: Path) -> Path:
    path = home / ".meridian" / "slot_claims"
    path.mkdir(parents=True, exist_ok=True)
    return path


async def test_no_claims_is_unknown_and_zero(db, env):
    doc = await overview(db, env)
    tunnel = doc["runtime"]["tunnel"]
    assert tunnel["client_process"] == "unknown"
    assert tunnel["client_process_reason"] == "tunnel process not detected"
    assert tunnel["live_slot_claims"] == 0
    assert tunnel["hosted_connection"] == "unknown"
    assert "tunnel_hosted_connection_unknown" in gap_codes(doc)


async def test_live_claim_with_matching_create_time_is_running(db, env, _isolated_machine):
    import psutil

    claims = _claim_dir(_isolated_machine)
    (claims / "5555.json").write_text(json.dumps({
        "client_id": "client-secret-id", "tunnel_pid": os.getpid(),
        "create_time": psutil.Process(os.getpid()).create_time(),
    }), encoding="utf-8")
    doc = await overview(db, env)
    tunnel = doc["runtime"]["tunnel"]
    assert tunnel["client_process"] == "running"
    assert tunnel["client_process_reason"] == "tunnel process found"
    assert tunnel["live_slot_claims"] == 1
    dumped = json.dumps(doc)
    assert "client-secret-id" not in dumped
    assert '"tunnel_pid"' not in dumped


async def test_live_claim_without_create_time_uses_pid_existence(db, env, _isolated_machine):
    claims = _claim_dir(_isolated_machine)
    (claims / "5556.json").write_text(json.dumps({"client_id": "c", "tunnel_pid": os.getpid()}), encoding="utf-8")
    doc = await overview(db, env)
    assert doc["runtime"]["tunnel"]["live_slot_claims"] == 1


async def test_claim_with_a_reused_pid_is_not_live(db, env, _isolated_machine):
    claims = _claim_dir(_isolated_machine)
    (claims / "5557.json").write_text(json.dumps({"client_id": "c", "tunnel_pid": os.getpid(), "create_time": 0.0}), encoding="utf-8")
    doc = await overview(db, env)
    assert doc["runtime"]["tunnel"]["client_process"] == "unknown"
    assert doc["runtime"]["tunnel"]["live_slot_claims"] == 0


async def test_dead_pid_claim_is_unknown(db, env, _isolated_machine):
    claims = _claim_dir(_isolated_machine)
    (claims / "5558.json").write_text(json.dumps({"client_id": "c", "tunnel_pid": 2147483646}), encoding="utf-8")
    doc = await overview(db, env)
    assert doc["runtime"]["tunnel"]["client_process"] == "unknown"
    assert doc["runtime"]["tunnel"]["live_slot_claims"] == 0


async def test_claims_with_bad_pids_or_json_are_ignored(db, env, _isolated_machine):
    claims = _claim_dir(_isolated_machine)
    (claims / "a.json").write_text("{bad", encoding="utf-8")
    (claims / "b.json").write_text("[1]", encoding="utf-8")
    (claims / "c.json").write_text(json.dumps({"tunnel_pid": "12"}), encoding="utf-8")
    (claims / "d.json").write_text(json.dumps({"tunnel_pid": True}), encoding="utf-8")
    doc = await overview(db, env)
    assert doc["runtime"]["tunnel"]["live_slot_claims"] == 0


async def test_psutil_missing_gives_unknown_and_the_error(db, env, _isolated_machine, monkeypatch):
    claims = _claim_dir(_isolated_machine)
    (claims / "5559.json").write_text(json.dumps({"tunnel_pid": os.getpid()}), encoding="utf-8")
    monkeypatch.setitem(sys.modules, "psutil", None)
    doc = await overview(db, env)
    assert doc["runtime"]["tunnel"]["client_process"] == "unknown"
    assert doc["runtime"]["tunnel"]["live_slot_claims"] is None
    assert ("runtime", "psutil_missing") in error_pairs(doc)


async def test_claim_directory_that_is_a_file_is_treated_as_no_claims(db, env, _isolated_machine):
    parent = _isolated_machine / ".meridian"
    parent.mkdir(parents=True, exist_ok=True)
    (parent / "slot_claims").write_text("not a dir", encoding="utf-8")
    doc = await overview(db, env)
    assert doc["runtime"]["tunnel"]["live_slot_claims"] == 0


async def test_claim_listing_failure_is_reported(db, env, _isolated_machine, monkeypatch):
    """A listing failure is reported, never read as zero claims (review M1).

    The patch replaces the call the code makes (``os.scandir``) for the claim directory only.
    """
    claims = _claim_dir(_isolated_machine)
    (claims / "5560.json").write_text(json.dumps({"tunnel_pid": os.getpid()}), encoding="utf-8")
    denied = os.path.normcase(os.path.normpath(str(claims)))
    real_scandir = os.scandir

    def scandir(path="."):
        if os.path.normcase(os.path.normpath(os.fspath(path))) == denied:
            raise PermissionError(13, "Permission denied")
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", scandir)
    doc = await overview(db, env)
    assert doc["runtime"]["tunnel"]["live_slot_claims"] is None
    assert doc["runtime"]["tunnel"]["client_process"] == "unknown"
    assert ("runtime", "tunnel_claims_unreadable") in error_pairs(doc)
    assert "tunnel_claims_unreadable" in gap_codes(doc)


async def test_only_the_first_64_claim_files_are_read(db, env, _isolated_machine):
    claims = _claim_dir(_isolated_machine)
    for i in range(64):
        (claims / f"a{i:02d}.json").write_text(json.dumps({"tunnel_pid": 2147483646}), encoding="utf-8")
    (claims / "zz.json").write_text(json.dumps({"tunnel_pid": os.getpid()}), encoding="utf-8")
    doc = await overview(db, env)
    assert doc["runtime"]["tunnel"]["live_slot_claims"] == 0


async def test_claim_file_over_the_byte_cap_is_ignored(db, env, _isolated_machine):
    claims = _claim_dir(_isolated_machine)
    padded = {"tunnel_pid": os.getpid(), "pad": "x" * 5000}
    (claims / "big.json").write_text(json.dumps(padded), encoding="utf-8")
    doc = await overview(db, env)
    assert doc["runtime"]["tunnel"]["live_slot_claims"] == 0


async def test_claim_file_that_cannot_be_read_is_ignored(db, env, _isolated_machine):
    claims = _claim_dir(_isolated_machine)
    (claims / "dir.json").mkdir()
    doc = await overview(db, env)
    assert doc["runtime"]["tunnel"]["live_slot_claims"] == 0


# ---------------------------------------------------------------------------
# Caps, budget and off-loop scan (spec 3.8)
# ---------------------------------------------------------------------------


async def test_byte_budget_stops_the_scan_and_marks_repos_not_scanned(db, env, tmp_path, monkeypatch):
    # Tiny on purpose: one settings file (about 1 KB) must overrun it, so the second repo is skipped.
    monkeypatch.setattr(wo, "SCAN_BUDGET_BYTES", 16)
    repo_a = make_repo(tmp_path, "budget-a")
    repo_b = make_repo(tmp_path, "budget-b")
    install_guard(repo_a, "enforce")
    install_guard(repo_b, "enforce")
    await add_project(db, "budget", [
        {"cwd": str(repo_a), "hostname": ""}, {"cwd": str(repo_b), "hostname": ""},
    ])
    doc = await overview(db, env)
    scan = doc["repos"]["scan"]
    assert scan["complete"] is False
    assert scan["stopped_reason"] == "bytes"
    assert scan["budget_bytes"] == 16
    last = doc["repos"]["items"][-1]
    assert last["guard"]["settings_state"] == "not_scanned"
    assert last["guard"]["registered"] is None
    assert last["codebase_memory"]["managed_entry"] is None
    assert "scan_budget_reached" in gap_codes(doc)


async def test_time_budget_of_zero_stops_the_scan(db, env, tmp_path, monkeypatch):
    monkeypatch.setattr(wo, "SCAN_BUDGET_S", 0)
    repo = make_repo(tmp_path, "time-budget")
    install_guard(repo, "enforce")
    await add_project(db, "time-budget", [{"cwd": str(repo), "hostname": ""}])
    doc = await overview(db, env)
    assert doc["repos"]["scan"]["stopped_reason"] == "time"
    assert doc["repos"]["items"][0]["guard"]["settings_state"] == "not_scanned"


async def test_a_complete_scan_reports_no_stop(db, env, tmp_path):
    repo = make_repo(tmp_path, "complete")
    await add_project(db, "complete", [{"cwd": str(repo), "hostname": ""}])
    doc = await overview(db, env)
    assert doc["repos"]["scan"]["complete"] is True
    assert doc["repos"]["scan"]["stopped_reason"] is None
    assert "scan_budget_reached" not in gap_codes(doc)


async def test_filesystem_scan_runs_in_a_worker_thread(db, env, monkeypatch):
    calls: list[str] = []
    real = asyncio.to_thread

    async def recording(fn, *args, **kwargs):
        calls.append(fn.__name__)
        return await real(fn, *args, **kwargs)

    monkeypatch.setattr(wo.asyncio, "to_thread", recording)
    await overview(db, env)
    assert "_scan_filesystem" in calls


async def test_read_only_leaves_db_rows_and_snapshot_bytes_unchanged(db, env, gdir, cache, tmp_path, monkeypatch):
    repo = make_repo(tmp_path, "readonly")
    make_index(cache, "readonly", repo)
    write_snapshot(cache, gdir, env)
    proj = await add_project(db, "readonly", [{"cwd": str(repo), "hostname": ""}])
    await add_session(db, proj["id"], "ro", status="active", last_seen=NOW - 30)
    snapshot_before = (gdir / "snapshot.json").read_bytes()
    projects_before = await dbm.list_projects(db)
    sessions_before = await dbm.get_sessions(db, proj["id"], active_only=False)

    def refuse(*args, **kwargs):
        raise AssertionError("refresh must not be called")

    monkeypatch.setattr(reg, "refresh", refuse)
    doc = await overview(db, env)
    assert doc["repos"]["snapshot"]["error"] is None
    assert (gdir / "snapshot.json").read_bytes() == snapshot_before
    assert await dbm.list_projects(db) == projects_before
    assert await dbm.get_sessions(db, proj["id"], active_only=False) == sessions_before


async def test_secret_values_never_reach_the_response(db, env, monkeypatch):
    url = f"postgresql://user:{SENTINEL_PW}@db.example/x"
    monkeypatch.setattr(toml_config, "get_active_db_url", lambda: (url, "production-label"))
    doc = await overview(db, env | {"MERIDIAN_DB_URL": url})
    dumped = json.dumps(doc)
    assert SENTINEL_PW not in dumped
    assert "db.example" not in dumped
    assert doc["runtime"]["database"]["backend"] == "postgres"
    assert doc["runtime"]["database"]["connection_name"] == "production-label"
    assert "db_host" not in dumped
    assert "toml_path" not in dumped


async def test_memory_and_sqlite_backend_labels(db, env):
    assert (await overview(db, env | {"MERIDIAN_DB": ":memory:"}))["runtime"]["database"]["backend"] == "memory"
    assert (await overview(db, env))["runtime"]["database"]["backend"] == "sqlite"


async def test_connection_name_failure_is_null(db, env, monkeypatch):
    def boom():
        raise RuntimeError("bad toml")

    monkeypatch.setattr(toml_config, "get_active_db_url", boom)
    doc = await overview(db, env)
    assert doc["runtime"]["database"]["connection_name"] is None


async def test_no_guard_dir_at_all_still_builds(db):
    doc = await overview(db, {})
    assert doc["repos"]["snapshot"]["path"] is None
    assert doc["repos"]["snapshot"]["error"] == "missing"
    assert doc["runtime"]["guard"]["guard_dir"] is None
    assert doc["runtime"]["guard"]["user_scope_installed"] is None


async def test_guard_dir_argument_overrides_the_environment(db, tmp_path):
    custom = tmp_path / "custom-guard"
    custom.mkdir()
    doc = await wo.build_overview(db, env={}, now=NOW, guard_dir=str(custom))
    assert doc["runtime"]["guard"]["guard_dir"] == reg.norm_path(str(custom), msys=True)


async def test_unexpected_guard_mode_failure_is_unknown(db, env, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("bad sentinel")

    monkeypatch.setattr(gc, "guard_mode", boom)
    doc = await overview(db, env)
    assert doc["runtime"]["guard"]["mode"] is None
    assert doc["runtime"]["guard"]["disabled_rules"] == []


async def test_unexpected_per_repo_failure_is_contained(db, env, tmp_path, monkeypatch):
    good = make_repo(tmp_path, "contained-good")
    bad = make_repo(tmp_path, "contained-bad")
    await add_project(db, "contained", [
        {"cwd": str(good), "hostname": ""}, {"cwd": str(bad), "hostname": ""},
    ])
    real = wo._repo_facts

    def picky(key, root, unresolved, ctx):
        if root.endswith("contained-bad"):
            raise RuntimeError("boom")
        return real(key, root, unresolved, ctx)

    monkeypatch.setattr(wo, "_repo_facts", picky)
    doc = await overview(db, env)
    # Review L1: the failed repo stays in items, so repos.total reconciles with the items.
    assert [item["name"] for item in doc["repos"]["items"]] == ["contained-bad", "contained-good"]
    assert doc["repos"]["total"] == len(doc["repos"]["items"]) == 2
    failed = repo_by_name(doc, "contained-bad")
    assert failed["guard"]["settings_state"] == "unreadable"
    assert failed["codebase_memory"]["index"]["mode"] is None
    assert ("repos", "settings_unreadable") in error_pairs(doc)
    assert "repo_facts_unavailable" in gap_codes(doc)


async def test_byte_budget_trips_before_config_audit_claims_and_identity(
    db, env, gdir, cache, tmp_path, monkeypatch, _isolated_machine,
):
    """Review M2: the snapshot read spends the budget, so every later step is skipped and reports unknown."""
    monkeypatch.setattr(wo, "SCAN_BUDGET_BYTES", 16)
    repo = make_repo(tmp_path, "budget-early")
    make_index(cache, "budget-early", repo)
    write_snapshot(cache, gdir, env)
    record_install(gdir, repo, "enforce")
    (gdir / "audit.log").write_text(_audit_line(NOW, str(repo)) + "\n", encoding="utf-8")
    claims = _claim_dir(_isolated_machine)
    (claims / "9000.json").write_text(json.dumps({"tunnel_pid": os.getpid()}), encoding="utf-8")
    await add_project(db, "budget-early", [{"cwd": str(repo), "hostname": ""}])
    doc = await overview(db, env)
    assert doc["repos"]["scan"]["complete"] is False
    assert doc["repos"]["scan"]["stopped_reason"] == "bytes"
    assert doc["repos"]["snapshot"]["error"] is None  # the snapshot itself is always read first
    assert doc["runtime"]["guard"]["user_scope_installed"] is None  # config.json was not read
    assert doc["runtime"]["tunnel"]["live_slot_claims"] is None  # claims were not read
    item = repo_by_name(doc, "budget-early")  # still listed: identity is skipped, not dropped
    assert item["guard"]["settings_state"] == "not_scanned"
    assert item["codebase_memory"]["index"]["mode"] is None
    assert item["codebase_memory"]["managed_entry"] is None
    assert item["canonical"]["linked"] is None
    assert item["activity"]["last_guard_event_reason"] == wo._AUDIT_NOT_SCANNED
    assert "scan_budget_reached" in gap_codes(doc)
    assert "tunnel_claims_unreadable" not in gap_codes(doc)
    json.dumps(doc, allow_nan=False)


def _stop_time_budget_after_first_identity(monkeypatch) -> None:
    """N1 test seam: the time budget runs out right after the first identity probe.

    Identity charges no bytes. Bytes run out before identity (snapshot, config, audit, claims) or
    after the repo facts start, so a stop between two identity probes can only come from the time
    budget. The deadline is set to zero right after the probe, so the stop does not depend on how
    fast the git reads are.
    """
    made: list[wo._Budget] = []
    real_budget = wo._Budget
    real_identity = wo._identity

    class RecordingBudget(real_budget):
        def __init__(self, limit_bytes: int, deadline: float) -> None:
            super().__init__(limit_bytes, deadline)
            made.append(self)

    def identity_then_time_out(norm, fs):
        ident = real_identity(norm, fs)
        made[-1].deadline = 0.0
        return ident

    monkeypatch.setattr(wo, "_Budget", RecordingBudget)
    monkeypatch.setattr(wo, "_identity", identity_then_time_out)


async def test_subdirectory_of_a_resolved_root_is_its_own_row_after_a_time_stop(
    db, env, tmp_path, monkeypatch,
):
    """Documented limit: the budget stops after the first repo resolves, then a subdirectory of it is seen.

    After the stop, identity is not resolved. The subdirectory keys by its normalized path, so it
    is its own row and does not join the root's row. The scan returns two rows and is incomplete.
    """
    _stop_time_budget_after_first_identity(monkeypatch)
    root = make_repo(tmp_path, "budget-prefix")
    sub = root / "src"
    sub.mkdir()
    await add_project(db, "budget-prefix", [
        {"cwd": str(root), "hostname": ""},
        {"cwd": str(sub), "hostname": ""},
    ])
    doc = await overview(db, env)
    assert doc["repos"]["scan"]["complete"] is False
    assert doc["repos"]["scan"]["stopped_reason"] == "time"
    assert "scan_budget_reached" in gap_codes(doc)
    assert len(doc["repos"]["items"]) == 2
    root_item = repo_by_name(doc, "budget-prefix")
    assert root_item["path"] == reg.norm_path(str(root), msys=True)
    assert root_item["key"] == reg.path_key(reg.norm_path(str(root), msys=True))
    assert reg.norm_path(str(sub), msys=True) not in root_item["source_paths"]
    sub_item = repo_by_name(doc, "src")
    assert sub_item["key"] == reg.path_key(reg.norm_path(str(sub), msys=True))
    assert sub_item["key"] != root_item["key"]


async def test_subdirectory_listed_before_its_root_after_the_stop_keeps_two_rows(db, env, tmp_path, monkeypatch):
    """Documented limit: with no root resolved before the stop, a subdirectory cannot find its root.

    Both paths are keyed from their normalized forms, so one repo shows as two rows and the scan
    reports scan_budget_reached. Identity after a stop never probes the filesystem, so this is the
    accepted outcome.
    """
    monkeypatch.setattr(wo, "SCAN_BUDGET_S", 0)  # the stop comes before any identity probe
    root = make_repo(tmp_path, "budget-mirror")
    sub = root / "src"
    sub.mkdir()
    await add_project(db, "budget-mirror", [
        {"cwd": str(sub), "hostname": ""},
        {"cwd": str(root), "hostname": ""},
    ])
    doc = await overview(db, env)
    assert doc["repos"]["scan"]["complete"] is False
    assert doc["repos"]["scan"]["stopped_reason"] == "time"
    assert "scan_budget_reached" in gap_codes(doc)
    assert len(doc["repos"]["items"]) == 2
    sub_item = repo_by_name(doc, "src")
    assert sub_item["key"] == reg.path_key(reg.norm_path(str(sub), msys=True))
    assert repo_by_name(doc, "budget-mirror")["key"] == reg.path_key(reg.norm_path(str(root), msys=True))


def test_identity_after_the_stop_keys_each_path_by_its_normalized_form(tmp_path, monkeypatch):
    """Documented limit, unit level: once the budget stops, a subdirectory and a sibling key by their own paths.

    The root resolved before the stop keeps its root key. After the stop, identity is not resolved, so
    the subdirectory and the sibling each key by their normalized path. Neither takes the root's key.
    """
    root = make_repo(tmp_path, "unit-root")
    sub = root / "src"
    sub.mkdir()
    sibling = make_repo(tmp_path, "unit-root-sibling", git=False)
    budget = wo._Budget(wo.SCAN_BUDGET_BYTES, time.monotonic() + 10)
    real_identity = wo._identity

    def identity_then_time_out(norm, fs):
        ident = real_identity(norm, fs)
        budget.deadline = 0.0  # the time budget runs out right after the first probe
        return ident

    monkeypatch.setattr(wo, "_identity", identity_then_time_out)
    inputs = [("project_repo_paths", str(p), "p1", None) for p in (root, sub, sibling)]
    cands, _ = wo._identify_candidates(inputs, None, [], reg.RealFS(), budget)
    by_norm = {c["norm"]: c for c in cands}
    root_norm = reg.norm_path(str(root), msys=True)
    sub_norm = reg.norm_path(str(sub), msys=True)
    sibling_norm = reg.norm_path(str(sibling), msys=True)
    assert budget.stopped == "time"
    assert by_norm[root_norm]["root"] == root_norm
    assert by_norm[root_norm]["unresolved"] is False
    assert by_norm[root_norm]["key"] == reg.path_key(root_norm)
    assert by_norm[sub_norm]["root"] == sub_norm
    assert by_norm[sub_norm]["key"] == reg.path_key(sub_norm)
    assert by_norm[sub_norm]["key"] != by_norm[root_norm]["key"]
    assert by_norm[sub_norm]["unresolved"] is None
    assert by_norm[sibling_norm]["root"] == sibling_norm
    assert by_norm[sibling_norm]["key"] == reg.path_key(sibling_norm)
    assert by_norm[sibling_norm]["unresolved"] is None


async def test_nested_worktree_under_a_resolved_root_keeps_its_own_row_after_a_time_stop(
    db, env, tmp_path, monkeypatch,
):
    """Spec 4.4 under a time stop: a linked worktree nested under a resolved root keeps its own row.

    The parent checkout resolves before the stop. The linked worktree sits inside the parent's
    directory and has its own ``.git`` file, so it is a different git root. After the stop it keys by
    its own normalized path and is not merged into the parent's row. The scan is incomplete.
    """
    _stop_time_budget_after_first_identity(monkeypatch)
    parent = make_repo(tmp_path, "nested-parent")
    linked = parent / "worktrees" / "feat"
    linked.mkdir(parents=True)
    gitdir = (parent / ".git" / "worktrees" / "feat").as_posix()
    (linked / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
    await add_project(db, "nested-parent", [
        {"cwd": str(parent), "hostname": ""},
        {"cwd": str(linked), "hostname": ""},
    ])
    doc = await overview(db, env)
    assert doc["repos"]["scan"]["complete"] is False
    assert doc["repos"]["scan"]["stopped_reason"] == "time"
    assert "scan_budget_reached" in gap_codes(doc)
    assert len(doc["repos"]["items"]) == 2
    parent_item = repo_by_name(doc, "nested-parent")
    linked_item = repo_by_name(doc, "feat")
    assert parent_item["key"] == reg.path_key(reg.norm_path(str(parent), msys=True))
    assert linked_item["key"] == reg.path_key(reg.norm_path(str(linked), msys=True))
    assert linked_item["key"] != parent_item["key"]
    assert reg.norm_path(str(linked), msys=True) not in parent_item["source_paths"]


async def test_a_failing_assemble_keeps_the_runtime_and_scan_sections(db, env, tmp_path, monkeypatch):
    """Review M3: _assemble raising yields empty repos, sessions and projects, with a gap and errors."""
    repo = make_repo(tmp_path, "assemble-fail")
    install_guard(repo, "enforce")
    await add_project(db, "assemble-fail", [{"cwd": str(repo), "hostname": ""}])

    def boom(*args, **kwargs):
        raise RuntimeError("injected failure")

    monkeypatch.setattr(wo, "_assemble", boom)
    doc = await overview(db, env)
    assert doc["repos"]["items"] == []
    assert doc["sessions"]["items"] == []
    assert doc["projects"]["items"] == []
    assert doc["runtime"]["tunnel"]["hosted_connection"] == "unknown"
    assert {("repos", "section_failed"), ("sessions", "section_failed"), ("projects", "section_failed")} <= error_pairs(doc)
    assert "section_failed" in gap_codes(doc)
    json.dumps(doc, allow_nan=False)


async def test_a_failing_runtime_keeps_the_other_sections(db, env, tmp_path, monkeypatch):
    """Review M3: _runtime raising yields a runtime with unknown values and the other sections intact."""
    repo = make_repo(tmp_path, "runtime-fail")
    install_guard(repo, "enforce")
    await add_project(db, "runtime-fail", [{"cwd": str(repo), "hostname": ""}])

    def boom(*args, **kwargs):
        raise RuntimeError("injected failure")

    monkeypatch.setattr(wo, "_runtime", boom)
    doc = await overview(db, env)
    assert doc["runtime"]["location"] == "local"
    assert doc["runtime"]["guard"]["mode"] is None
    assert doc["runtime"]["tunnel"]["client_process"] == "unknown"
    assert ("runtime", "section_failed") in error_pairs(doc)
    assert "section_failed" in gap_codes(doc)
    assert repo_by_name(doc, "runtime-fail")["guard"]["registered"] is True
    json.dumps(doc, allow_nan=False)


@pytest.mark.parametrize("target", ["_scan_filesystem", "_candidate_inputs"])
async def test_a_failing_scan_or_candidate_step_returns_a_complete_document(db, env, monkeypatch, target):
    """Review M3: a failure before or during the scan is recorded as a gap, not a 500."""

    def boom(*args, **kwargs):
        raise RuntimeError("injected failure")

    monkeypatch.setattr(wo, target, boom)
    doc = await overview(db, env)
    assert ("repos", "section_failed") in error_pairs(doc)
    assert "section_failed" in gap_codes(doc)
    assert doc["repos"]["items"] == []
    json.dumps(doc, allow_nan=False)


async def test_budget_stopping_mid_claims_reports_no_partial_count(db, env, monkeypatch, _isolated_machine):
    """Review M2: a budget stop inside the claim loop leaves live_slot_claims null, never a partial count."""
    monkeypatch.setattr(wo, "SCAN_BUDGET_BYTES", 1)
    claims = _claim_dir(_isolated_machine)
    (claims / "9001.json").write_text(json.dumps({"tunnel_pid": os.getpid()}), encoding="utf-8")
    (claims / "9002.json").write_text(json.dumps({"tunnel_pid": os.getpid()}), encoding="utf-8")
    doc = await overview(db, env)
    assert doc["repos"]["scan"]["stopped_reason"] == "bytes"
    assert doc["runtime"]["tunnel"]["live_slot_claims"] is None
    assert doc["runtime"]["tunnel"]["client_process"] == "unknown"


async def test_failing_guard_dir_lookup_and_db_load_are_sections_not_500s(db, env, monkeypatch):
    """Review M3: the guard dir lookup and the project load are guarded like the other sections."""

    def boom(*args, **kwargs):
        raise RuntimeError("injected failure")

    monkeypatch.setattr(wo.reg, "guard_dir", boom)
    monkeypatch.setattr(wo, "_load_db", boom)
    doc = await overview(db, env)
    assert {("runtime", "section_failed"), ("projects", "section_failed")} <= error_pairs(doc)
    assert doc["runtime"]["guard"]["guard_dir"] is None
    assert doc["projects"]["items"] == []


# ---------------------------------------------------------------------------
# Gap codes and error codes (spec 2.5, 2.6)
# ---------------------------------------------------------------------------


async def test_always_gaps_are_present_on_an_empty_machine(db, env):
    doc = await overview(db, env)
    always = {
        "tunnel_hosted_connection_unknown", "tunnel_claim_signal_partial", "sessions_location_unknown",
        "guard_mode_server_process", "user_scope_guard_not_scanned", "repo_last_use_partial",
        "session_repo_link_partial", "project_status_manual", "project_activity_excludes_closed",
        "receipts_default_root_excluded", "receipt_root_compare_normalized",
    }
    assert always <= gap_codes(doc)
    assert not (gap_codes(doc) & {"guard_runtime_server_process", "scan_budget_reached", "hostname_unverified"})
    assert all(gap["message"] and gap["source"] for gap in doc["gaps"])


async def test_snapshot_gaps_appear_only_with_a_snapshot(db, env, gdir, cache, tmp_path):
    repo = make_repo(tmp_path, "snap-gaps")
    make_index(cache, "snap-gaps", repo)
    write_snapshot(cache, gdir, env)
    await add_project(db, "snap-gaps", [{"cwd": str(repo), "hostname": ""}])
    doc = await overview(db, env)
    assert {"snapshot_written_at_session_start", "snapshot_machine_wide"} <= gap_codes(doc)
    empty = await overview(db, env | {"MERIDIAN_GUARD_DIR": str(tmp_path / "nowhere")})
    assert "snapshot_machine_wide" not in gap_codes(empty)


async def test_registered_guard_raises_the_server_process_runtime_gap(db, env, tmp_path):
    repo = make_repo(tmp_path, "registered")
    install_guard(repo, "enforce")
    await add_project(db, "registered", [{"cwd": str(repo), "hostname": ""}])
    doc = await overview(db, env)
    assert "guard_runtime_server_process" in gap_codes(doc)


async def test_partial_settings_read_error_codes_are_deduplicated(db, env, tmp_path):
    a = make_repo(tmp_path, "dup-a")
    b = make_repo(tmp_path, "dup-b")
    for repo in (a, b):
        (repo / ".claude").mkdir()
        (repo / ".claude" / "settings.json").write_text("{bad", encoding="utf-8")
    await add_project(db, "dups", [{"cwd": str(a), "hostname": ""}, {"cwd": str(b), "hostname": ""}])
    doc = await overview(db, env)
    codes = [(e["section"], e["code"]) for e in doc["errors"]]
    assert codes.count(("repos", "settings_unreadable")) == 1


async def test_claim_that_fails_inside_psutil_is_not_live(db, env, _isolated_machine, monkeypatch):
    import psutil

    class Exploding:
        def __init__(self, pid):
            raise psutil.Error("nope")

    claims = _claim_dir(_isolated_machine)
    (claims / "explode.json").write_text(json.dumps({"tunnel_pid": os.getpid(), "create_time": 1.0}), encoding="utf-8")
    monkeypatch.setattr(psutil, "Process", Exploding)
    doc = await overview(db, env)
    assert doc["runtime"]["tunnel"]["live_slot_claims"] == 0
