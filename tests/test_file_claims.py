"""Tests for the get_file_claims read-only MCP tool + DB helper."""
from __future__ import annotations

from meridian import db as db_module
import meridian.server as srv


async def test_get_file_claims_reports_whole_file_lock(db):
    """get_file_claims returns the active lock with the holder's session name."""
    p = await db_module.create_project(db, "file-claims-test")
    session = await db_module.register_session(db, p["id"], "editor-session")
    await db_module.claim_file(db, "meridian/server.py", session["id"])

    result = await db_module.get_file_claims(db, "meridian/server.py")
    assert result["file_path"] == "meridian/server.py"
    assert result["file_lock"] is not None
    assert result["file_lock"]["session_id"] == session["id"]
    assert result["file_lock"]["session_name"] == "editor-session"
    assert result["symbol_claims"] == []



async def test_get_file_claims_empty_when_unclaimed(db):
    """No lock + no symbol claims → file_lock None, symbol_claims []."""
    result = await db_module.get_file_claims(db, "meridian/never_claimed.py")
    assert result["file_lock"] is None
    assert result["symbol_claims"] == []



async def test_get_file_claims_mcp_dispatch(db):
    """The MCP tool dispatches to the DB helper and returns its payload."""
    p = await db_module.create_project(db, "file-claims-mcp")
    session = await db_module.register_session(db, p["id"], "mcp-session")
    await db_module.claim_file(db, "meridian/db/__init__.py", session["id"])

    result = await srv._dispatch_mcp_tool(
        "get_file_claims",
        {"file_path": "meridian/db/__init__.py"},
        db,
        "/tmp",
    )
    assert result["file_lock"]["session_id"] == session["id"]
    assert "symbol_claims" in result
