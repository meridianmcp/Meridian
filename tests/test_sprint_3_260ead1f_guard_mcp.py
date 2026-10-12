"""Tests for sprint item 260ead1f -- optional symbol-lock guard for research runs,
exercised through the MCP dispatch path (guard_mode off / warn / strict).

The model and DB layers are covered in tests/test_research_run.py. This file pins
the MCP surface: guard_mode must reach start_research_run, an unknown guard_mode
must fail closed (no run row), a read-only run must never take a write lock, and
malformed claim payloads must fail open with the verdict visible on the run.
"""
from __future__ import annotations

import pytest

from meridian import db as db_module
from meridian.db import research_runs as run_db
import meridian.server as server


async def _session(db, prefix: str):
    project = await db_module.create_project(db, prefix)
    session = await db_module.register_session(db, project["id"], f"{prefix}-session")
    return project, session


async def _dispatch_start(db, tmp_path, project_id, session_id, **extra):
    args = {
        "project_id": project_id, "session_id": session_id,
        "mode": "isolated_write", "repository_id": "meridian-repo@worktree:wf-260ead1f",
        "allowed_paths": ["meridian/scratch/guard_mcp.py"], "turn_budget": 10,
        "is_isolated_worktree": True,
    }
    args.update(extra)
    return await server._dispatch_mcp_tool("start_research_run", args, db, str(tmp_path))


@pytest.mark.asyncio
async def test_mcp_start_without_guard_mode_is_off_and_ignores_a_real_conflict(db, tmp_path):
    """Zero-behavior-change default over MCP: an isolated_write run starts even when
    another session already write-claims its path, and the guard is never evaluated."""
    project, holder = await _session(db, "mcp-guard-off-holder")
    await db_module.claim_file(db, "meridian/scratch/guard_mcp.py", holder["id"])

    project2, runner = await _session(db, "mcp-guard-off-runner")
    result = await _dispatch_start(db, tmp_path, project2["id"], runner["id"])

    assert "error" not in result
    assert result["run"]["status"] == "active"
    assert result["run"]["guard"]["checked"] is False
    assert result["run"]["guard"]["guard_mode"] == "off"


@pytest.mark.asyncio
async def test_mcp_start_warn_surfaces_the_conflict_and_still_starts(db, tmp_path):
    project, holder = await _session(db, "mcp-guard-warn-holder")
    await db_module.claim_file(db, "meridian/scratch/guard_mcp.py", holder["id"])

    project2, runner = await _session(db, "mcp-guard-warn-runner")
    result = await _dispatch_start(
        db, tmp_path, project2["id"], runner["id"], guard_mode="warn",
    )

    assert "error" not in result
    assert result["run"]["status"] == "active"
    guard = result["run"]["guard"]
    assert guard["checked"] is True
    assert guard["allow"] is True
    assert len(guard["conflicts"]) == 1
    conflict = guard["conflicts"][0]
    assert conflict["file_path"] == "meridian/scratch/guard_mcp.py"
    assert conflict["reason"] == "write_locked"
    assert conflict["holder"] == holder["id"]


@pytest.mark.asyncio
async def test_mcp_start_strict_blocks_a_whole_file_conflict_and_creates_no_run(db, tmp_path):
    project, holder = await _session(db, "mcp-guard-strict-holder")
    await db_module.claim_file(db, "meridian/scratch/guard_mcp.py", holder["id"])

    project2, runner = await _session(db, "mcp-guard-strict-runner")
    before = await run_db.list_research_runs(db, project2["id"], include_terminal=True)
    result = await _dispatch_start(
        db, tmp_path, project2["id"], runner["id"], guard_mode="strict",
    )

    assert "run" not in result
    assert "guard_mode='strict' blocked" in result["error"]
    assert "write_locked" in result["error"]
    after = await run_db.list_research_runs(db, project2["id"], include_terminal=True)
    assert after == before  # strict refusal leaves no partial row


@pytest.mark.asyncio
async def test_mcp_start_strict_allows_a_clear_path(db, tmp_path):
    project, runner = await _session(db, "mcp-guard-clear")
    result = await _dispatch_start(
        db, tmp_path, project["id"], runner["id"], guard_mode="strict",
    )

    assert "error" not in result
    assert result["run"]["status"] == "active"
    assert result["run"]["guard"]["checked"] is True
    assert result["run"]["guard"]["allow"] is True
    assert result["run"]["guard"]["conflicts"] == []


@pytest.mark.asyncio
async def test_mcp_start_rejects_an_unknown_guard_mode_without_creating_a_run(db, tmp_path):
    """An unrecognised guard_mode must fail closed, never silently fall back to off."""
    project, runner = await _session(db, "mcp-guard-unknown")
    before = await run_db.list_research_runs(db, project["id"], include_terminal=True)
    result = await _dispatch_start(
        db, tmp_path, project["id"], runner["id"], guard_mode="block",
    )

    assert "run" not in result
    assert "guard_mode must be one of" in result["error"]
    after = await run_db.list_research_runs(db, project["id"], include_terminal=True)
    assert after == before


@pytest.mark.asyncio
async def test_mcp_read_only_run_never_takes_a_write_lock_even_under_strict(db, tmp_path):
    """A read_only run is evaluated in claim_guard's read mode: another session's
    SYMBOL claim must not block it, and starting the run must not add any claim of
    its own on the path."""
    path = "meridian/scratch/guard_mcp_readonly.py"
    _, holder = await _session(db, "mcp-guard-ro-holder")
    claimed = await db_module.claim_symbol(
        db, holder["id"], path, "edit_caption", "def edit_caption():\n    pass\n",
    )
    assert claimed["claimed"] is True

    project, runner = await _session(db, "mcp-guard-ro-runner")
    result = await _dispatch_start(
        db, tmp_path, project["id"], runner["id"], mode="read_only",
        allowed_paths=[path], is_isolated_worktree=False, guard_mode="strict",
    )

    assert "error" not in result
    guard = result["run"]["guard"]
    assert guard["allow"] is True
    assert guard["conflicts"] == []
    assert guard["results"][0]["claim_mode"] == "read"

    claims = await db_module.get_file_claims(db, path, project["id"])
    assert claims["file_lock"] is None  # the read-only run took no write lock
    assert len(claims["symbol_claims"]) == 1  # only the holder's original symbol claim
    assert claims["symbol_claims"][0]["session_id"] == holder["id"]


@pytest.mark.asyncio
async def test_mcp_strict_fails_open_on_malformed_claim_payload(db, tmp_path, monkeypatch):
    """Malformed claim data must degrade to allow, and that verdict must be visible on
    the returned run (reason='no-claim-data'), never a silent block or a crash."""
    async def _malformed_claims(*_args, **_kwargs):
        return "not-a-dict"

    monkeypatch.setattr(db_module, "get_file_claims", _malformed_claims)

    project, runner = await _session(db, "mcp-guard-malformed")
    result = await _dispatch_start(
        db, tmp_path, project["id"], runner["id"], guard_mode="strict",
    )

    assert "error" not in result
    assert result["run"]["status"] == "active"
    guard = result["run"]["guard"]
    assert guard["allow"] is True
    assert guard["conflicts"] == []
    assert guard["results"][0]["reason"] == "no-claim-data"
    assert guard["results"][0]["holder"] is None
