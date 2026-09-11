"""Tests for sprint item cdd0ef6c — cross-client session recovery registry.

Investigation trigger: a Claude Remote Control (RC) bridge id with no
environment_id was not resumable (``remote-control --session-id`` rejected
it). This module verifies:

  1. host-local-vs-hosted security separation (no bridge id / local path /
     argv / environment id ever reaches the hosted DB row, even smuggled
     inside ``metadata``),
  2. the RESCUE-D regression itself (a remote_control bridge id alone must
     never be reported resumable),
  3. stale/dead/unknown liveness classification,
  4. recovery continuation re-deriving the LIVE board + active file claims
     rather than replaying a stored /goal body, and
  5. the MCP tool surface end to end.
"""
from __future__ import annotations

import pytest

from meridian import db as db_module
from meridian import session_recovery as model
from meridian.db import session_recovery as recovery_db
import meridian.mcp_tools as mcp_tools
import meridian.server as server


async def _session(db, prefix: str):
    project = await db_module.create_project(db, prefix)
    session = await db_module.register_session(db, project["id"], f"{prefix}-session")
    return project, session


# ---------------------------------------------------------------------------
# Model layer: host-local vs hosted separation
# ---------------------------------------------------------------------------

def test_hosted_text_rejects_secrets_and_local_paths():
    with pytest.raises(model.SessionRecoveryError, match="Refusing to persist"):
        model.validate_hosted_text(
            "bearer sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            field="last_checkpoint_ref",
        )
    with pytest.raises(model.SessionRecoveryError, match="secret-shaped"):
        model.validate_hosted_text("api_key: abcdef123456", field="detail")
    with pytest.raises(model.SessionRecoveryError, match="machine-local absolute"):
        model.validate_hosted_text("see C:\\Users\\adam\\notes.txt", field="last_handoff_ref")
    with pytest.raises(ValueError):
        model.validate_hosted_text("read /home/adam/.claude/session.json", field="detail")


def test_hosted_metadata_rejects_smuggled_local_only_identity_keys():
    """RESCUE-D core guarantee: a caller cannot launder a bridge id / local
    transcript path / environment id / argv into hosted metadata."""
    for key in sorted(model.LOCAL_ONLY_IDENTITY_KEYS):
        with pytest.raises(model.SessionRecoveryError, match="host-local sensitive"):
            model.validate_hosted_metadata({key: "cse_abcdef123456"})
    # Nested placement is caught too.
    with pytest.raises(model.SessionRecoveryError, match="host-local sensitive"):
        model.validate_hosted_metadata({"note": {"bridge_id": "cse_nested"}})
    # An ordinary, safe metadata object passes through unchanged.
    safe = {"note": "resumed after a laptop restart", "count": 3}
    assert model.validate_hosted_metadata(safe) == safe


def test_hosted_metadata_rejects_non_json_and_oversized():
    with pytest.raises(model.SessionRecoveryError, match="non-JSON value"):
        model.validate_hosted_metadata({"bad": object()})
    with pytest.raises(model.SessionRecoveryError, match="must be an object"):
        model.validate_hosted_metadata(["not", "an", "object"])
    with pytest.raises(model.SessionRecoveryError, match="exceeds"):
        model.validate_hosted_metadata({"large": ["x" * 4_000] * 6})


# ---------------------------------------------------------------------------
# Resume recipes — the RESCUE-D regression itself
# ---------------------------------------------------------------------------

def test_remote_control_bridge_id_alone_is_never_resumable():
    """The exact RESCUE-D failure shape: a cse_... bridge id with no
    environment_id must come back blocked, never a working recipe."""
    recipe, reason = model.build_resume_recipe(
        "remote_control", "claude_code", {"bridge_id": "cse_9f8e7d6c5b4a"},
    )
    assert recipe is None
    assert "environment_id" in reason


def test_remote_control_with_environment_id_is_resumable():
    recipe, reason = model.build_resume_recipe(
        "remote_control", "claude_code",
        {"bridge_id": "cse_9f8e7d6c5b4a", "environment_id": "env-42"},
    )
    assert reason is None
    assert "env-42" in recipe
    assert "cse_9f8e7d6c5b4a" in recipe


def test_stdio_resume_recipe_requires_local_session_id():
    recipe, reason = model.build_resume_recipe("stdio", "claude_code", {})
    assert recipe is None
    assert "local transcript" in reason

    recipe, reason = model.build_resume_recipe(
        "stdio", "claude_code", {"local_session_id": "conv-abc"},
    )
    assert reason is None
    assert recipe == "claude --resume conv-abc"


def test_tunnel_transport_has_no_resume_recipe():
    recipe, reason = model.build_resume_recipe("tunnel", None, {"argv": []})
    assert recipe is None
    assert "reconnect-based" in reason


def test_explicit_argv_wins_over_template():
    recipe, reason = model.build_resume_recipe(
        "remote_control", "cursor", {"argv": ["cursor", "--resume-bridge", "cse_x"]},
    )
    assert reason is None
    assert recipe == "cursor --resume-bridge cse_x"


def test_unknown_transport_is_blocked():
    recipe, reason = model.build_resume_recipe("unknown", None, {})
    assert recipe is None
    assert "no known resume recipe" in reason


# ---------------------------------------------------------------------------
# Liveness classification
# ---------------------------------------------------------------------------

def test_classify_liveness_states():
    from datetime import datetime, timedelta, timezone

    now = datetime(2026, 9, 10, 12, 0, 0, tzinfo=timezone.utc)
    fresh = (now - timedelta(minutes=1)).isoformat()
    stale = (now - timedelta(minutes=30)).isoformat()
    dead = (now - timedelta(hours=8)).isoformat()

    assert model.classify_liveness(fresh, "active", now=now) == "resumable"
    assert model.classify_liveness(stale, "active", now=now) == "stale"
    assert model.classify_liveness(dead, "active", now=now) == "dead"
    assert model.classify_liveness(None, "active", now=now) == "unknown"
    assert model.classify_liveness(fresh, "crashed", now=now) == "dead"
    assert model.classify_liveness(fresh, "ended", now=now) == "dead"
    assert model.classify_liveness("not-a-timestamp", "active", now=now) == "unknown"


# ---------------------------------------------------------------------------
# Host-local snapshot
# ---------------------------------------------------------------------------

def test_local_snapshot_roundtrip_and_atomic_failure(tmp_path):
    result = model.write_local_recovery_snapshot(
        tmp_path, "proj-1",
        {"ref-1": {"bridge_id": "cse_x", "environment_id": "env-1", "resume_recipe": "r"}},
    )
    assert result["ok"] is True
    payload = model.read_local_recovery_snapshot(tmp_path, "proj-1")
    assert payload["records"]["ref-1"]["bridge_id"] == "cse_x"
    assert model.read_local_recovery_snapshot(tmp_path, "missing-project") is None

    data_dir_file = tmp_path / "not-a-dir"
    data_dir_file.write_text("x", encoding="utf-8")
    failure = model.write_local_recovery_snapshot(data_dir_file, "proj-2", {})
    assert failure["ok"] is False


@pytest.mark.asyncio
async def test_sqlite_migration_is_idempotent(db):
    from meridian.db import migrations as _mig

    await _mig._migrate_session_recovery_registry(db)
    await _mig._migrate_session_recovery_registry(db)
    async with db.execute(
        "SELECT COUNT(*) AS n FROM session_recovery_registry"
    ) as cur:
        row = await cur.fetchone()
    assert row is not None


# ---------------------------------------------------------------------------
# DB layer
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_register_upserts_and_refreshes_heartbeat(db):
    project, session = await _session(db, "recovery-upsert")
    first = await recovery_db.register_session_recovery(
        db, project["id"], session["id"],
        transport="stdio", client_type="claude-code", verified_resumable=True,
    )
    assert first["liveness"] == "resumable"
    assert first["verified_resumable"] is True
    assert first["metadata"] == {}

    second = await recovery_db.register_session_recovery(
        db, project["id"], session["id"],
        transport="remote_control", lifecycle_status="idle",
        verified_resumable=False, sprint_version="v1",
    )
    # Same row (upsert), not a duplicate.
    assert second["id"] == first["id"]
    assert second["transport"] == "remote_control"
    assert second["sprint_version"] == "v1"
    assert second["last_heartbeat_at"] >= first["last_heartbeat_at"]

    rows = await recovery_db.list_resumable_sessions(db, project["id"], include_dead=True)
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_register_never_persists_local_only_fields_even_via_db_layer(db):
    """No DB-layer parameter exists to accept bridge_id/environment_id/argv/
    local_transcript_path at all -- the hosted row has no such column."""
    project, session = await _session(db, "recovery-no-leak")
    record = await recovery_db.register_session_recovery(
        db, project["id"], session["id"], transport="remote_control",
        verified_resumable=True,
    )
    assert set(record) >= {
        "id", "project_id", "meridian_session_id", "local_ref_id", "transport",
        "lifecycle_status", "verified_resumable", "last_heartbeat_at", "metadata",
    }
    for forbidden in model.LOCAL_ONLY_IDENTITY_KEYS:
        assert forbidden not in record


@pytest.mark.asyncio
async def test_project_and_session_scope_enforced(db):
    project, session = await _session(db, "recovery-scope-a")
    other_project, other_session = await _session(db, "recovery-scope-b")
    await recovery_db.register_session_recovery(
        db, project["id"], session["id"], transport="stdio",
    )
    assert await recovery_db.get_session_recovery(
        db, other_project["id"], session_id=session["id"]
    ) is None
    with pytest.raises(ValueError, match="does not belong"):
        await recovery_db.register_session_recovery(
            db, project["id"], other_session["id"], transport="stdio",
        )


@pytest.mark.asyncio
async def test_list_resumable_sessions_excludes_dead_by_default(db):
    project, alive = await _session(db, "recovery-list-alive")
    crashed = await db_module.register_session(db, project["id"], "recovery-list-crashed-session")
    await recovery_db.register_session_recovery(
        db, project["id"], alive["id"], transport="stdio", lifecycle_status="active",
    )
    await recovery_db.register_session_recovery(
        db, project["id"], crashed["id"], transport="stdio", lifecycle_status="crashed",
    )
    live_only = await recovery_db.list_resumable_sessions(db, project["id"])
    assert {r["meridian_session_id"] for r in live_only} == {alive["id"]}

    everything = await recovery_db.list_resumable_sessions(db, project["id"], include_dead=True)
    assert {r["meridian_session_id"] for r in everything} == {alive["id"], crashed["id"]}
    dead_row = next(r for r in everything if r["meridian_session_id"] == crashed["id"])
    assert dead_row["liveness"] == "dead"


@pytest.mark.asyncio
async def test_recovery_continuation_rederives_live_board_and_active_claims(db):
    """Recovery continuation must reflect ground truth, not a stale /goal:
    an item added AFTER registration still shows up, and a file this
    session still holds a write lock on is surfaced (never released)."""
    project, session = await _session(db, "recovery-continuation")
    await recovery_db.register_session_recovery(
        db, project["id"], session["id"], transport="stdio", sprint_version="v1",
    )
    item = await db_module.add_sprint_item(db, project["id"], "v1", "Do the thing")
    from meridian.db import locks as locks_module
    await locks_module.claim_file(db, "meridian/example.py", session["id"])

    continuation = await recovery_db.build_recovery_continuation(db, project["id"], session["id"])
    item_ids = {it["id"] for it in continuation["board"]["items"]}
    assert item["id"] in item_ids
    assert "meridian/example.py" in continuation["active_file_claims"]
    assert "do not replay" in continuation["guidance"].lower() or "re-derives" in continuation["guidance"].lower()

    with pytest.raises(ValueError, match="no session recovery record"):
        await recovery_db.build_recovery_continuation(db, project["id"], "not-a-session")


# ---------------------------------------------------------------------------
# MCP surface
# ---------------------------------------------------------------------------

def test_mcp_tools_advertise_session_recovery_surface():
    names = {tool["name"] for tool in mcp_tools._MCP_TOOLS_LIST}
    assert {
        "register_session_recovery", "list_resumable_sessions", "get_session_recovery",
    } <= names
    read_only = set(mcp_tools._READ_ONLY_TOOLS)
    assert {"list_resumable_sessions", "get_session_recovery"} <= read_only
    assert "register_session_recovery" not in read_only


@pytest.mark.asyncio
async def test_mcp_register_never_leaks_local_identity_into_hosted_row(db, tmp_path):
    """End-to-end MCP regression for the security-critical constraint: a
    caller-supplied local_identity (bridge_id/environment_id/argv/local
    transcript path) travels only to the host-local snapshot file, never
    into the hosted DB row this call returns."""
    project, session = await _session(db, "recovery-mcp-no-leak")
    result = await server._dispatch_mcp_tool(
        "register_session_recovery",
        {
            "project_id": project["id"],
            "session_id": session["id"],
            "transport": "remote_control",
            "client_type": "claude-code",
            "local_identity": {
                "bridge_id": "cse_deadbeef00112233",
                "environment_id": "env-77",
                "local_session_id": "conv-77",
                "local_transcript_path": "C:\\Users\\adam\\.claude\\projects\\x\\conv.jsonl",
                "argv": ["claude", "--resume", "conv-77", "--environment-id", "env-77"],
            },
        },
        db, str(tmp_path),
    )
    assert result["resume_recipe"] is not None
    assert result["recovery"]["verified_resumable"] is True
    blob = str(result["recovery"])
    assert "cse_deadbeef00112233" not in blob
    assert "env-77" not in blob
    assert "conv.jsonl" not in blob

    # The hosted row, re-read straight from the DB, carries none of it either.
    fresh = await recovery_db.get_session_recovery(db, project["id"], session_id=session["id"])
    assert "cse_deadbeef00112233" not in str(fresh)
    assert "conv.jsonl" not in str(fresh)

    # ...but the host-local snapshot DID capture it, for THIS machine's own use.
    assert result["local_snapshot"]["ok"] is True
    snapshot = model.read_local_recovery_snapshot(tmp_path, project["id"])
    local_ref = fresh["local_ref_id"]
    assert snapshot["records"][local_ref]["bridge_id"] == "cse_deadbeef00112233"


@pytest.mark.asyncio
async def test_mcp_register_rejects_bridge_id_smuggled_via_metadata(db, tmp_path):
    project, session = await _session(db, "recovery-mcp-metadata-guard")
    with pytest.raises(ValueError, match="host-local sensitive"):
        await server._dispatch_mcp_tool(
            "register_session_recovery",
            {
                "project_id": project["id"],
                "session_id": session["id"],
                "transport": "stdio",
                "metadata": {"bridge_id": "cse_smuggled"},
            },
            db, str(tmp_path),
        )


@pytest.mark.asyncio
async def test_mcp_list_and_get_session_recovery_round_trip(db, tmp_path):
    project, session = await _session(db, "recovery-mcp-roundtrip")
    await server._dispatch_mcp_tool(
        "register_session_recovery",
        {
            "project_id": project["id"], "session_id": session["id"],
            "transport": "stdio", "sprint_version": "v1",
            "local_identity": {"local_session_id": "conv-1"},
        },
        db, str(tmp_path),
    )
    listed = await server._dispatch_mcp_tool(
        "list_resumable_sessions", {"project_id": project["id"]}, db, str(tmp_path),
    )
    assert listed["count"] == 1
    assert listed["sessions"][0]["meridian_session_id"] == session["id"]

    fetched = await server._dispatch_mcp_tool(
        "get_session_recovery",
        {"project_id": project["id"], "session_id": session["id"]},
        db, str(tmp_path),
    )
    assert fetched["recovery"]["meridian_session_id"] == session["id"]
    assert fetched["resume_recipe"] == "claude --resume conv-1"
    assert fetched["continuation"]["active_file_claims"] == []

    missing = await server._dispatch_mcp_tool(
        "get_session_recovery",
        {"project_id": project["id"], "session_id": "does-not-exist"},
        db, str(tmp_path),
    )
    assert "error" in missing


@pytest.mark.asyncio
async def test_mcp_register_session_recovery_project_id_not_required_schema():
    """Enforced project-wide contract: project_id must never be required."""
    by_name = {t["name"]: t for t in mcp_tools._MCP_TOOLS_LIST}
    for name in ("register_session_recovery", "list_resumable_sessions", "get_session_recovery"):
        schema = by_name[name]["inputSchema"]
        assert "project_id" not in (schema.get("required") or [])
        assert "project_name" in schema["properties"]
