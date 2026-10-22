"""Per-project use mode: the rights gate's stored input.

Covers ``db.set_project_use_mode`` / ``get_project_use_mode`` and the strict
validation behind them, the audit row each change writes, the rights gate reading
the STORED mode (``rights_override.authorize_image_storage``), the MCP tool
``set_project_use_mode``, and the HTTP ``PATCH /projects/{id}/settings`` field.

The ``db`` fixture is SQLite locally and Postgres in CI (TEST_DATABASE_URL), so
the database tests run on both backends. The one test that needs a database
with no CHECK constraint builds its own SQLite connection.
"""
from __future__ import annotations

import json
import uuid

import aiosqlite
import pytest
from pydantic import ValidationError

import meridian.server  # noqa: F401 — must be imported before handler to avoid cycle
from meridian import db as db_module
from meridian.db.migrations import _migrate_add_column_if_missing
from meridian.mcp import handler as mh
from meridian.mcp.handlers import project_tools as pt_mod
from meridian.mcp_tools import (
    _DESTRUCTIVE_TOOLS,
    _MCP_TOOLS_LIST,
    _READ_ONLY_TOOLS,
    _TOOL_CATEGORY,
    _TOOL_ROLE_RELEVANCE,
    _TOOL_WORKFLOW_TIER,
)
from meridian.models import ProjectSettingsPatch
from meridian.rights_override import authorize_image_storage, request_rights_override
from meridian.rights_policy import (
    COMMERCIALIZED,
    LINK_ONLY,
    RIGHTS_OVERRIDE_EVENT_TYPE,
    STANDARD_PUBLICATION,
    RightsPolicyError,
)

_DATA_DIR = "/tmp/meridian-test"
_YES = "Yes — approve this override"
_REASON = "permission letter on file"
_EVENT = "project_use_mode_changed"


async def _project(db) -> str:
    """A new project with a unique name (names are unique per database)."""
    project = await db_module.create_project(db, f"use-mode-{uuid.uuid4().hex[:10]}")
    return project["id"]


async def _audit_rows(db, project_id: str):
    return await db_module.get_action_audit_log(
        db, project_id=project_id, event_type=_EVENT,
    )


async def _rights_audit_rows(db, project_id: str):
    return await db_module.get_action_audit_log(
        db, project_id=project_id, event_type=RIGHTS_OVERRIDE_EVENT_TYPE,
    )


async def _count(db, sql: str, params: tuple) -> int:
    async with db.execute(sql, params) as cur:
        row = await cur.fetchone()
    return row["c"] if isinstance(row, dict) else row[0]


async def _hitl_count(db, project_id: str) -> int:
    return await _count(
        db, "SELECT COUNT(*) AS c FROM hitl_requests WHERE project_id = ?", (project_id,),
    )


# ---------------------------------------------------------------------------
# Default, set, and read back
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_default_use_mode_is_standard_publication(db):
    pid = await _project(db)
    assert await db_module.get_project_use_mode(db, pid) == STANDARD_PUBLICATION
    settings = await db_module.get_project_settings(db, pid)
    assert settings["use_mode"] == STANDARD_PUBLICATION


@pytest.mark.asyncio
async def test_set_commercialized_then_read_it_back(db):
    pid = await _project(db)
    updated = await db_module.set_project_use_mode(db, pid, COMMERCIALIZED, actor="test-human")
    assert updated["id"] == pid
    assert updated["use_mode"] == COMMERCIALIZED
    assert await db_module.get_project_use_mode(db, pid) == COMMERCIALIZED
    assert (await db_module.get_project_settings(db, pid))["use_mode"] == COMMERCIALIZED


@pytest.mark.asyncio
async def test_get_project_use_mode_for_a_missing_project_raises(db):
    with pytest.raises(ValueError, match="project not found"):
        await db_module.get_project_use_mode(db, "no-such-project")


def test_validate_use_mode_accepts_exactly_the_two_modes():
    assert db_module.validate_use_mode(STANDARD_PUBLICATION) == STANDARD_PUBLICATION
    assert db_module.validate_use_mode(COMMERCIALIZED) == COMMERCIALIZED


# ---------------------------------------------------------------------------
# Strict validation: a bad value raises, changes nothing, and is never coerced
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [
    "commercial", "Commercialized", " commercialized ", "academic", "standard",
    "", None, 1, True, ["commercialized"],
])
async def test_invalid_use_mode_raises_and_changes_nothing(db, bad):
    pid = await _project(db)
    with pytest.raises(RightsPolicyError) as exc:
        await db_module.set_project_use_mode(db, pid, bad, actor="test-human")
    assert exc.value.code == "USE_MODE_INVALID"
    assert isinstance(exc.value, ValueError)
    assert await db_module.get_project_use_mode(db, pid) == STANDARD_PUBLICATION
    assert await _audit_rows(db, pid) == []


@pytest.mark.asyncio
async def test_setting_a_missing_project_raises_and_writes_nothing(db):
    with pytest.raises(ValueError, match="project not found"):
        await db_module.set_project_use_mode(db, "no-such-project", COMMERCIALIZED, actor="test-human")
    assert await _audit_rows(db, "no-such-project") == []


# ---------------------------------------------------------------------------
# Audit: one row per change, with old and new values
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_each_change_writes_one_audit_row_with_old_and_new(db):
    pid = await _project(db)
    await db_module.set_project_use_mode(
        db, pid, COMMERCIALIZED, actor="test-human", reason="client contract",
    )
    rows = await _audit_rows(db, pid)
    assert len(rows) == 1
    row = rows[0]
    detail = json.loads(row["detail"])
    assert row["event_type"] == _EVENT == db_module.PROJECT_USE_MODE_EVENT_TYPE
    assert row["actor"] == "test-human"
    assert detail == {
        "old": STANDARD_PUBLICATION,
        "new": COMMERCIALIZED,
        "reason": "client contract",
    }

    await db_module.set_project_use_mode(db, pid, STANDARD_PUBLICATION, actor="test-human")
    rows = await _audit_rows(db, pid)
    assert len(rows) == 2
    pairs = {(json.loads(r["detail"])["old"], json.loads(r["detail"])["new"]) for r in rows}
    assert pairs == {
        (STANDARD_PUBLICATION, COMMERCIALIZED),
        (COMMERCIALIZED, STANDARD_PUBLICATION),
    }


@pytest.mark.asyncio
async def test_failed_audit_write_restores_the_old_mode(db, monkeypatch):
    """The mode and its audit row never disagree: if the audit row cannot be
    written, the old mode is restored and the error propagates."""
    pid = await _project(db)

    async def audit_store_down(*_args, **_kwargs):
        raise RuntimeError("audit store unavailable")

    monkeypatch.setattr(db_module, "record_action_audit_event", audit_store_down)
    with pytest.raises(RuntimeError, match="audit store unavailable"):
        await db_module.set_project_use_mode(db, pid, COMMERCIALIZED, actor="test-human")
    assert await db_module.get_project_use_mode(db, pid) == STANDARD_PUBLICATION


# ---------------------------------------------------------------------------
# Python guard where the database has no CHECK (upgraded databases)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_python_guard_rejects_a_bad_value_where_the_database_has_no_check():
    """An upgraded database gains use_mode through ALTER TABLE, which carries no
    CHECK, so the database itself accepts any text. The Python guard is then the
    only guard, and the gate fails closed on a stored value it does not know."""
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    try:
        await conn.execute(
            "CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE)"
        )
        await conn.execute("INSERT INTO projects (id, name) VALUES ('legacy', 'legacy-project')")
        await conn.commit()
        await _migrate_add_column_if_missing(
            conn, "projects", "use_mode", "TEXT NOT NULL DEFAULT 'standard_publication'",
        )
        # The existing row backfills to the default.
        assert await db_module.get_project_use_mode(conn, "legacy") == STANDARD_PUBLICATION

        # With no CHECK the database accepts a bad value outright.
        await conn.execute("UPDATE projects SET use_mode = 'bogus' WHERE id = 'legacy'")
        await conn.commit()

        # The setter refuses a bad value and leaves the stored value as it was.
        with pytest.raises(RightsPolicyError) as exc:
            await db_module.set_project_use_mode(conn, "legacy", "academic", actor="test-human")
        assert exc.value.code == "USE_MODE_INVALID"
        assert await db_module.get_project_use_mode(conn, "legacy") == "bogus"

        # The gate fails closed on the unknown stored value: it applies the
        # commercialized gate, so restricted bytes are refused with no HITL.
        d = await authorize_image_storage(conn, "legacy", "CC BY-NC 4.0", subject_id="fig-1")
        assert d.allowed is False
        assert d.tier == LINK_ONLY
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# The rights gate reads the STORED mode
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stored_commercialized_refuses_link_only_bytes_with_no_hitl(db):
    pid = await _project(db)
    await db_module.set_project_use_mode(db, pid, COMMERCIALIZED, actor="test-human")
    d = await authorize_image_storage(db, pid, "CC BY-NC 4.0", subject_id="fig-1")
    assert d.allowed is False
    assert d.use_mode == COMMERCIALIZED
    assert d.tier == LINK_ONLY
    assert await _hitl_count(db, pid) == 0
    assert await _rights_audit_rows(db, pid) == []


@pytest.mark.asyncio
async def test_stored_standard_stores_link_only_bytes_by_default(db):
    pid = await _project(db)
    d = await authorize_image_storage(db, pid, "CC BY-NC 4.0", subject_id="fig-1")
    assert d.allowed is True
    assert d.use_mode == STANDARD_PUBLICATION


@pytest.mark.asyncio
async def test_explicit_standard_cannot_unlock_a_stored_commercialized_project(db):
    pid = await _project(db)
    await db_module.set_project_use_mode(db, pid, COMMERCIALIZED, actor="test-human")
    d = await authorize_image_storage(
        db, pid, "CC BY-NC 4.0", subject_id="fig-1", use_mode=STANDARD_PUBLICATION,
    )
    assert d.allowed is False
    assert d.use_mode == COMMERCIALIZED


@pytest.mark.asyncio
async def test_explicit_commercialized_refuses_on_a_stored_standard_project(db):
    pid = await _project(db)
    d = await authorize_image_storage(
        db, pid, "CC BY-NC 4.0", subject_id="fig-1", use_mode=COMMERCIALIZED,
    )
    assert d.allowed is False
    assert d.use_mode == COMMERCIALIZED


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ["CC0", "CC BY-NC 4.0"])
async def test_missing_project_is_refused_and_stores_nothing(db, raw):
    missing = "no-such-project"
    with pytest.raises(RightsPolicyError) as exc:
        await authorize_image_storage(db, missing, raw, subject_id="fig-1")
    assert exc.value.code == "PROJECT_NOT_FOUND"
    assert await _hitl_count(db, missing) == 0
    assert await _rights_audit_rows(db, missing) == []


@pytest.mark.asyncio
async def test_missing_project_is_refused_before_an_approval_is_spent(db):
    pid = await _project(db)
    hitl = await request_rights_override(
        db, pid, subject_id="fig-1", reason=_REASON, description="CC BY-NC figure",
    )
    await db_module.answer_hitl_request(db, hitl["id"], _YES, answered_by="adam")
    with pytest.raises(RightsPolicyError) as exc:
        await authorize_image_storage(
            db, "no-such-project", "CC BY-NC 4.0", subject_id="fig-1",
            hitl_id=hitl["id"], reason=_REASON,
        )
    assert exc.value.code == "PROJECT_NOT_FOUND"
    # The approval was not spent: the real, now commercialized project still accepts it.
    await db_module.set_project_use_mode(db, pid, COMMERCIALIZED, actor="test-human")
    d = await authorize_image_storage(
        db, pid, "CC BY-NC 4.0", subject_id="fig-1", hitl_id=hitl["id"], reason=_REASON,
    )
    assert d.allowed is True
    assert d.override_used is True


# ---------------------------------------------------------------------------
# Settings: the update path and the Pydantic body
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_update_project_settings_routes_use_mode_through_the_audited_setter(db):
    pid = await _project(db)
    settings = await db_module.update_project_settings(
        db, pid, use_mode=COMMERCIALIZED, use_mode_actor="test-human",
    )
    assert settings["use_mode"] == COMMERCIALIZED
    rows = await _audit_rows(db, pid)
    assert len(rows) == 1
    assert rows[0]["actor"] == "test-human"


@pytest.mark.asyncio
async def test_update_project_settings_refuses_a_bad_use_mode_before_writing_anything(db):
    pid = await _project(db)
    with pytest.raises(RightsPolicyError) as exc:
        await db_module.update_project_settings(db, pid, hitl_auto_answer=2, use_mode="academic")
    assert exc.value.code == "USE_MODE_INVALID"
    settings = await db_module.get_project_settings(db, pid)
    assert settings["hitl_auto_answer"] == 0
    assert settings["use_mode"] == STANDARD_PUBLICATION
    assert await _audit_rows(db, pid) == []


def test_patch_body_accepts_only_the_two_use_modes():
    assert ProjectSettingsPatch(use_mode="commercialized").use_mode == "commercialized"
    assert ProjectSettingsPatch(use_mode="standard_publication").use_mode == "standard_publication"
    # Omitting the field leaves the stored mode alone.
    assert ProjectSettingsPatch().use_mode is None
    for bad in ("academic", "Commercialized", ""):
        with pytest.raises(ValidationError):
            ProjectSettingsPatch(use_mode=bad)


# ---------------------------------------------------------------------------
# HTTP: PATCH /projects/{id}/settings, via the shared client fixture
# ---------------------------------------------------------------------------

def test_http_patch_sets_and_returns_use_mode(client):
    pid = client.post("/projects", json={"name": "use-mode-http-set"}).json()["id"]
    assert client.get(f"/projects/{pid}/settings").json()["use_mode"] == STANDARD_PUBLICATION
    r = client.patch(f"/projects/{pid}/settings", json={"use_mode": COMMERCIALIZED})
    assert r.status_code == 200
    assert r.json()["use_mode"] == COMMERCIALIZED
    assert client.get(f"/projects/{pid}/settings").json()["use_mode"] == COMMERCIALIZED


def test_http_patch_rejects_an_invalid_use_mode_and_changes_nothing(client):
    pid = client.post("/projects", json={"name": "use-mode-http-invalid"}).json()["id"]
    r = client.patch(f"/projects/{pid}/settings", json={"use_mode": "academic"})
    assert r.status_code == 422
    assert client.get(f"/projects/{pid}/settings").json()["use_mode"] == STANDARD_PUBLICATION


def test_http_patch_unknown_project_returns_404(client):
    r = client.patch("/projects/does-not-exist/settings", json={"use_mode": COMMERCIALIZED})
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# MCP tool: set_project_use_mode
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mcp_tool_sets_the_mode_and_audits_it_as_the_mcp_actor(db):
    pid = await _project(db)
    result = await mh._handle_project_tools(
        "set_project_use_mode",
        {"project_id": pid, "use_mode": COMMERCIALIZED, "reason": "contract"},
        db, _DATA_DIR, None, None,
    )
    assert "error" not in result
    assert result["use_mode"] == COMMERCIALIZED
    rows = await _audit_rows(db, pid)
    assert len(rows) == 1
    assert rows[0]["actor"] == "mcp:set_project_use_mode"
    assert json.loads(rows[0]["detail"])["reason"] == "contract"


@pytest.mark.asyncio
async def test_mcp_cannot_downgrade_a_commercialized_project(db):
    # A downgrade unlocks restricted images with no human in the loop, so it is
    # refused over MCP and must leave both the mode and the audit trail unchanged.
    pid = await _project(db)
    await db_module.set_project_use_mode(db, pid, COMMERCIALIZED, actor="test-human")
    before = len(await _audit_rows(db, pid))
    result = await mh._handle_project_tools(
        "set_project_use_mode",
        {"project_id": pid, "use_mode": STANDARD_PUBLICATION},
        db, _DATA_DIR, None, None,
    )
    assert "error" in result
    assert "owner" in result["error"]
    assert await db_module.get_project_use_mode(db, pid) == COMMERCIALIZED
    assert len(await _audit_rows(db, pid)) == before


@pytest.mark.asyncio
async def test_mcp_can_still_upgrade_a_standard_project(db):
    pid = await _project(db)
    result = await mh._handle_project_tools(
        "set_project_use_mode",
        {"project_id": pid, "use_mode": COMMERCIALIZED},
        db, _DATA_DIR, None, None,
    )
    assert result["use_mode"] == COMMERCIALIZED


@pytest.mark.asyncio
async def test_a_blank_stored_mode_fails_closed_for_image_storage(db, monkeypatch):
    # A blank stored value can only come from raw SQL. It must tighten the gate.
    pid = await _project(db)

    async def stored_blank(_db, _project_id):
        return ""

    monkeypatch.setattr(db_module, "get_project_use_mode", stored_blank)
    d = await authorize_image_storage(db, pid, "CC BY-NC 4.0", subject_id="fig-1")
    assert d.allowed is False
    assert d.tier == LINK_ONLY


@pytest.mark.asyncio
async def test_mcp_tool_resolves_the_project_by_name(db):
    project = await db_module.create_project(db, f"use-mode-{uuid.uuid4().hex[:10]}")
    result = await mh._handle_project_tools(
        "set_project_use_mode",
        {"project_name": project["name"], "use_mode": COMMERCIALIZED},
        db, _DATA_DIR, None, None,
    )
    assert result["id"] == project["id"]
    assert result["use_mode"] == COMMERCIALIZED


@pytest.mark.asyncio
async def test_mcp_tool_returns_error_for_an_unknown_project(db):
    result = await mh._handle_project_tools(
        "set_project_use_mode",
        {"project_id": "does-not-exist", "use_mode": COMMERCIALIZED},
        db, _DATA_DIR, None, None,
    )
    assert "error" in result


@pytest.mark.asyncio
async def test_mcp_tool_returns_error_for_a_bad_value_and_changes_nothing(db):
    pid = await _project(db)
    result = await mh._handle_project_tools(
        "set_project_use_mode", {"project_id": pid, "use_mode": "academic"},
        db, _DATA_DIR, None, None,
    )
    assert "error" in result
    assert await db_module.get_project_use_mode(db, pid) == STANDARD_PUBLICATION


@pytest.mark.asyncio
async def test_mcp_tool_requires_a_project_and_a_mode(db):
    pid = await _project(db)
    no_project = await mh._handle_project_tools(
        "set_project_use_mode", {"use_mode": COMMERCIALIZED}, db, _DATA_DIR, None, None,
    )
    assert "error" in no_project
    no_mode = await mh._handle_project_tools(
        "set_project_use_mode", {"project_id": pid}, db, _DATA_DIR, None, None,
    )
    assert "error" in no_mode


@pytest.mark.asyncio
async def test_mcp_handler_is_exposed_and_called_directly(db):
    pid = await _project(db)
    assert hasattr(pt_mod, "handle_set_project_use_mode")
    result = await pt_mod.handle_set_project_use_mode(
        {"project_id": pid, "use_mode": COMMERCIALIZED}, db, _DATA_DIR, None, None,
    )
    assert result["use_mode"] == COMMERCIALIZED


def test_mcp_schema_enumerates_exactly_the_two_modes():
    tool = next(t for t in _MCP_TOOLS_LIST if t["name"] == "set_project_use_mode")
    schema = tool["inputSchema"]
    assert schema["properties"]["use_mode"]["enum"] == [STANDARD_PUBLICATION, COMMERCIALIZED]
    assert schema["required"] == ["use_mode"]


def test_mcp_tool_is_classified_like_set_project_execution_mode():
    name = "set_project_use_mode"
    peer = "set_project_execution_mode"
    assert _TOOL_CATEGORY[name] == _TOOL_CATEGORY[peer] == "project"
    assert _TOOL_ROLE_RELEVANCE[name] == _TOOL_ROLE_RELEVANCE[peer]
    assert _TOOL_WORKFLOW_TIER[name] == _TOOL_WORKFLOW_TIER[peer]
    assert name not in _READ_ONLY_TOOLS
    assert name not in _DESTRUCTIVE_TOOLS
