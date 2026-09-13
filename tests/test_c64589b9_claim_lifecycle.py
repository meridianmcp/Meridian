"""Tests for W1-I (c64589b9) — auto-registration + claim lifecycle.

Three distinct pieces, no source proposal existed for this item (confirmed
via get_workspace_proposals/search_all — see the session handoff notes), so
this was implemented from the title plus direct code investigation:

1. **Unregistered-session auto-registration** — ``meridian/mcp/handler.py``'s
   ``log_task`` dispatch (the only place in the codebase that actually
   hard-rejected an unrecognised ``session_id``, per grep of
   "register_session"/session-existence checks across claim_sprint_item /
   complete_sprint_item / log_task) now auto-registers via the new
   ``db_module.ensure_session_registered`` instead of raising. Covered here
   at both the db-helper level and the log_task dispatch level (the
   dispatch-level regression tests for the superseded 26c38b8e hard-reject
   live in ``tests/test_core.py``, right next to the original pinned test).

2. **release_sprint_item_claim** — a brand-new capability (confirmed absent
   under this name anywhere in the codebase). Lets a LIVE session
   voluntarily give up an in_progress claim, distinct from
   reconcile_stale_claims (dead-session cleanup).

3. **transfer_sprint_item_claim** — also brand-new. Hands a LIVE in_progress
   claim to a different actor/session without ever passing through
   'pending', so no third session can race to claim it in between.

Both new capabilities are wired end-to-end (db layer, MCP schema in
mcp_tools.py, HTTP/MCP dispatch in mcp/handler.py, stdio transport in
mcp/stdio_handler.py) mirroring the exact pattern
tests/test_56e9b3c7_reconcile_stale_claims_mcp_exposure.py already
established and verifies for reconcile_stale_claims.
"""
from __future__ import annotations

import pytest

from meridian import db as db_module
from meridian.db import sprint_items as sprint_items_module
import meridian.server as srv
from meridian.mcp_tools import _MCP_TOOLS_LIST, _READ_ONLY_TOOLS


def _tool(name: str) -> dict:
    return next(t for t in _MCP_TOOLS_LIST if t["name"] == name)


# ---------------------------------------------------------------------------
# 1. ensure_session_registered (db layer)
# ---------------------------------------------------------------------------


async def test_ensure_session_registered_creates_under_exact_id(db):
    p = await db_module.create_project(db, "w1i-ensure-session-new")
    sid = "11111111-1111-1111-1111-111111111111"
    session = await db_module.ensure_session_registered(db, p["id"], sid)
    assert session["id"] == sid
    assert session["project_id"] == p["id"]
    assert session["session_type"] == "worker"
    assert session["name"] == f"auto-{sid[:8]}"

    async with db.execute("SELECT * FROM sessions WHERE id = ?", (sid,)) as cur:
        row = await cur.fetchone()
    assert db_module._row_to_dict(row) is not None


async def test_ensure_session_registered_idempotent_for_existing(db):
    p = await db_module.create_project(db, "w1i-ensure-session-existing")
    existing = await db_module.register_session(db, p["id"], "already-here", human_id="alice")
    returned = await db_module.ensure_session_registered(
        db, p["id"], existing["id"], name="ignored-name",
    )
    assert returned["id"] == existing["id"]
    assert returned["name"] == "already-here"  # untouched, NOT overwritten
    assert returned["human_id"] == "alice"


async def test_ensure_session_registered_cross_project_raises(db):
    p1 = await db_module.create_project(db, "w1i-ensure-session-p1")
    p2 = await db_module.create_project(db, "w1i-ensure-session-p2")
    existing = await db_module.register_session(db, p1["id"], "p1-owner")
    with pytest.raises(ValueError, match="different project"):
        await db_module.ensure_session_registered(db, p2["id"], existing["id"])


async def test_ensure_session_registered_custom_name_and_type(db):
    p = await db_module.create_project(db, "w1i-ensure-session-custom")
    sid = "22222222-2222-2222-2222-222222222222"
    session = await db_module.ensure_session_registered(
        db, p["id"], sid, name="explicit-name", session_type="human", human_id="bob",
    )
    assert session["name"] == "explicit-name"
    assert session["session_type"] == "human"
    assert session["human_id"] == "bob"


async def test_ensure_session_registered_rejects_bad_session_type(db):
    p = await db_module.create_project(db, "w1i-ensure-session-badtype")
    with pytest.raises(ValueError, match="invalid session_type"):
        await db_module.ensure_session_registered(
            db, p["id"], "33333333-3333-3333-3333-333333333333", session_type="robot",
        )


# ---------------------------------------------------------------------------
# 2. release_sprint_item_claim (db layer)
# ---------------------------------------------------------------------------


async def test_release_claim_success_clears_actor_and_claimed_at(db):
    p = await db_module.create_project(db, "w1i-release-success")
    item = await db_module.add_sprint_item(
        db, p["id"], "v1", "release me",
        touches_resources=["file:w1i_release_me.py"], prospect_bypass=True,
    )
    owner = await db_module.register_session(db, p["id"], "release-owner")
    await db_module.claim_sprint_item(db, p["id"], item["id"], actor=owner["id"])
    await db_module.claim_file(db, "w1i_release_me.py", owner["id"])

    result = await db_module.release_sprint_item_claim(
        db, p["id"], item["id"], owner["id"], reason="wrong scope",
    )
    assert result["prior_actor"] == owner["id"]
    assert result["released_resources"] == ["file:w1i_release_me.py"]
    assert result["item"]["status"] == "pending"
    assert result["item"]["actor"] is None
    assert result["item"]["claimed_at"] is None

    # f007e59e — both columns cleared, not just status.
    fresh = await db_module.get_sprint_item(db, item["id"])
    assert fresh["status"] == "pending"
    assert fresh["actor"] is None
    assert fresh["claimed_at"] is None

    claims = await db_module.get_file_claims(db, "w1i_release_me.py")
    assert claims["file_lock"] is None

    audit = await db_module.get_action_audit_log(
        db, project_id=p["id"],
        event_type=sprint_items_module.SPRINT_ITEM_CLAIM_RELEASED_AUDIT_EVENT,
    )
    assert len(audit) == 1
    assert item["id"] in audit[0]["detail"]


async def test_release_claim_not_in_progress_is_blocked(db):
    p = await db_module.create_project(db, "w1i-release-not-in-progress")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "never claimed")
    result = await db_module.release_sprint_item_claim(db, p["id"], item["id"], "some-session")
    assert result["blocked"] is True
    assert result["error"] == "NOT_IN_PROGRESS"


async def test_release_claim_wrong_owner_refused_without_force(db):
    p = await db_module.create_project(db, "w1i-release-wrong-owner")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "owned by someone else")
    owner = await db_module.register_session(db, p["id"], "real-owner")
    intruder = await db_module.register_session(db, p["id"], "intruder")
    await db_module.claim_sprint_item(db, p["id"], item["id"], actor=owner["id"])

    result = await db_module.release_sprint_item_claim(db, p["id"], item["id"], intruder["id"])
    assert result["blocked"] is True
    assert result["error"] == "NOT_CLAIM_OWNER"
    assert result["actor"] == owner["id"]

    unchanged = await db_module.get_sprint_item(db, item["id"])
    assert unchanged["status"] == "in_progress"
    assert unchanged["actor"] == owner["id"]


async def test_release_claim_symbol_resource_released(db):
    p = await db_module.create_project(db, "w1i-release-symbol")
    item = await db_module.add_sprint_item(
        db, p["id"], "v1", "release a symbol resource",
        touches_resources=["symbol:w1i_release_sym.py::bar"], prospect_bypass=True,
    )
    owner = await db_module.register_session(db, p["id"], "release-symbol-owner")
    await db_module.claim_sprint_item(db, p["id"], item["id"], actor=owner["id"])
    claimed = await db_module.claim_symbol(
        db, owner["id"], "w1i_release_sym.py", "bar", "def bar():\n    pass\n",
    )
    assert claimed.get("claimed") is not False

    result = await db_module.release_sprint_item_claim(db, p["id"], item["id"], owner["id"])
    assert result["released_resources"] == ["symbol:w1i_release_sym.py::bar"]
    remaining = await db_module.get_symbol_claims(db, "w1i_release_sym.py")
    assert remaining == []


async def test_release_claim_wrong_owner_succeeds_with_force(db):
    p = await db_module.create_project(db, "w1i-release-force")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "force released")
    owner = await db_module.register_session(db, p["id"], "real-owner-2")
    intruder = await db_module.register_session(db, p["id"], "forcer")
    await db_module.claim_sprint_item(db, p["id"], item["id"], actor=owner["id"])

    result = await db_module.release_sprint_item_claim(
        db, p["id"], item["id"], intruder["id"], force=True,
    )
    assert "blocked" not in result
    assert result["prior_actor"] == owner["id"]
    fresh = await db_module.get_sprint_item(db, item["id"])
    assert fresh["status"] == "pending"
    assert fresh["actor"] is None


async def test_release_claim_unknown_item_returns_none(db):
    p = await db_module.create_project(db, "w1i-release-unknown-item")
    result = await db_module.release_sprint_item_claim(db, p["id"], "does-not-exist", "sess")
    assert result is None


async def test_release_claim_wrong_project_returns_none(db):
    p1 = await db_module.create_project(db, "w1i-release-wrong-proj-1")
    p2 = await db_module.create_project(db, "w1i-release-wrong-proj-2")
    item = await db_module.add_sprint_item(db, p1["id"], "v1", "belongs to p1")
    result = await db_module.release_sprint_item_claim(db, p2["id"], item["id"], "sess")
    assert result is None


async def test_release_claim_requires_session_id(db):
    p = await db_module.create_project(db, "w1i-release-requires-session")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "no session given")
    with pytest.raises(ValueError, match="session_id is required"):
        await db_module.release_sprint_item_claim(db, p["id"], item["id"], "")


async def test_release_claim_race_lost_reports_structured_block(db, monkeypatch):
    p = await db_module.create_project(db, "w1i-release-race")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "raced away")
    owner = await db_module.register_session(db, p["id"], "race-owner")
    await db_module.claim_sprint_item(db, p["id"], item["id"], actor=owner["id"])

    async def _fake_transition(*_a, **_k):
        return None

    monkeypatch.setattr(sprint_items_module, "_transition_status", _fake_transition)
    result = await db_module.release_sprint_item_claim(db, p["id"], item["id"], owner["id"])
    assert result["blocked"] is True
    assert result["error"] == "RACE_LOST"


# ---------------------------------------------------------------------------
# 3. transfer_sprint_item_claim (db layer)
# ---------------------------------------------------------------------------


async def test_transfer_claim_success_moves_actor_and_file_lock(db):
    p = await db_module.create_project(db, "w1i-transfer-success")
    item = await db_module.add_sprint_item(
        db, p["id"], "v1", "transfer me",
        touches_resources=["file:w1i_transfer_me.py"], prospect_bypass=True,
    )
    from_sess = await db_module.register_session(db, p["id"], "transfer-from")
    to_sess = await db_module.register_session(db, p["id"], "transfer-to")
    await db_module.claim_sprint_item(db, p["id"], item["id"], actor=from_sess["id"])
    await db_module.claim_file(db, "w1i_transfer_me.py", from_sess["id"])

    result = await db_module.transfer_sprint_item_claim(
        db, p["id"], item["id"], from_sess["id"], to_sess["id"],
        to_session_id=to_sess["id"], reason="handing off",
    )
    assert result["prior_actor"] == from_sess["id"]
    assert result["new_actor"] == to_sess["id"]
    assert result["transferred_resources"] == ["file:w1i_transfer_me.py"]
    assert result["item"]["status"] == "in_progress"
    assert result["item"]["actor"] == to_sess["id"]

    claims = await db_module.get_file_claims(db, "w1i_transfer_me.py")
    assert claims["file_lock"]["session_id"] == to_sess["id"]

    audit = await db_module.get_action_audit_log(
        db, project_id=p["id"],
        event_type=sprint_items_module.SPRINT_ITEM_CLAIM_TRANSFERRED_AUDIT_EVENT,
    )
    assert len(audit) == 1


async def test_transfer_claim_without_to_session_id_only_releases_lock(db):
    p = await db_module.create_project(db, "w1i-transfer-no-to-session")
    item = await db_module.add_sprint_item(
        db, p["id"], "v1", "transfer to a human name",
        touches_resources=["file:w1i_transfer_human.py"], prospect_bypass=True,
    )
    from_sess = await db_module.register_session(db, p["id"], "transfer-human-from")
    await db_module.claim_sprint_item(db, p["id"], item["id"], actor=from_sess["id"])
    await db_module.claim_file(db, "w1i_transfer_human.py", from_sess["id"])

    result = await db_module.transfer_sprint_item_claim(
        db, p["id"], item["id"], from_sess["id"], "adam",
    )
    assert result["new_actor"] == "adam"
    assert result["transferred_resources"] == []
    assert result["released_only_resources"] == ["file:w1i_transfer_human.py"]

    claims = await db_module.get_file_claims(db, "w1i_transfer_human.py")
    assert claims["file_lock"] is None
    fresh = await db_module.get_sprint_item(db, item["id"])
    assert fresh["status"] == "in_progress"
    assert fresh["actor"] == "adam"


async def test_transfer_claim_symbol_resource_released_not_reclaimed(db):
    p = await db_module.create_project(db, "w1i-transfer-symbol")
    item = await db_module.add_sprint_item(
        db, p["id"], "v1", "transfer with a symbol resource",
        touches_resources=["symbol:w1i_sym.py::foo"], prospect_bypass=True,
    )
    from_sess = await db_module.register_session(db, p["id"], "transfer-symbol-from")
    to_sess = await db_module.register_session(db, p["id"], "transfer-symbol-to")
    await db_module.claim_sprint_item(db, p["id"], item["id"], actor=from_sess["id"])
    claimed = await db_module.claim_symbol(
        db, from_sess["id"], "w1i_sym.py", "foo", "def foo():\n    pass\n",
    )
    assert claimed.get("claimed") is not False

    result = await db_module.transfer_sprint_item_claim(
        db, p["id"], item["id"], from_sess["id"], to_sess["id"],
        to_session_id=to_sess["id"],
    )
    assert result["transferred_resources"] == []
    assert result["released_only_resources"] == ["symbol:w1i_sym.py::foo"]
    remaining = await db_module.get_symbol_claims(db, "w1i_sym.py")
    assert remaining == []


async def test_transfer_claim_not_in_progress_is_blocked(db):
    p = await db_module.create_project(db, "w1i-transfer-not-in-progress")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "never claimed for transfer")
    result = await db_module.transfer_sprint_item_claim(
        db, p["id"], item["id"], "some-session", "other-actor",
    )
    assert result["blocked"] is True
    assert result["error"] == "NOT_IN_PROGRESS"


async def test_transfer_claim_wrong_owner_refused_without_force(db):
    p = await db_module.create_project(db, "w1i-transfer-wrong-owner")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "not yours to transfer")
    owner = await db_module.register_session(db, p["id"], "transfer-real-owner")
    intruder = await db_module.register_session(db, p["id"], "transfer-intruder")
    await db_module.claim_sprint_item(db, p["id"], item["id"], actor=owner["id"])

    result = await db_module.transfer_sprint_item_claim(
        db, p["id"], item["id"], intruder["id"], "someone-else",
    )
    assert result["blocked"] is True
    assert result["error"] == "NOT_CLAIM_OWNER"

    unchanged = await db_module.get_sprint_item(db, item["id"])
    assert unchanged["actor"] == owner["id"]


async def test_transfer_claim_same_actor_is_blocked(db):
    p = await db_module.create_project(db, "w1i-transfer-same-actor")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "already yours")
    owner = await db_module.register_session(db, p["id"], "transfer-self-owner")
    await db_module.claim_sprint_item(db, p["id"], item["id"], actor=owner["id"])

    result = await db_module.transfer_sprint_item_claim(
        db, p["id"], item["id"], owner["id"], owner["id"],
    )
    assert result["blocked"] is True
    assert result["error"] == "SAME_ACTOR"


async def test_transfer_claim_unknown_item_returns_none(db):
    p = await db_module.create_project(db, "w1i-transfer-unknown-item")
    result = await db_module.transfer_sprint_item_claim(
        db, p["id"], "does-not-exist", "sess", "actor",
    )
    assert result is None


async def test_transfer_claim_requires_to_actor_and_from_session(db):
    p = await db_module.create_project(db, "w1i-transfer-requires-args")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "missing args")
    with pytest.raises(ValueError, match="to_actor is required"):
        await db_module.transfer_sprint_item_claim(db, p["id"], item["id"], "sess", "")
    with pytest.raises(ValueError, match="from_session_id is required"):
        await db_module.transfer_sprint_item_claim(db, p["id"], item["id"], "", "actor")


async def test_transfer_claim_race_lost_reports_structured_block(db, monkeypatch):
    p = await db_module.create_project(db, "w1i-transfer-race")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "raced away on transfer")
    owner = await db_module.register_session(db, p["id"], "transfer-race-owner")
    await db_module.claim_sprint_item(db, p["id"], item["id"], actor=owner["id"])

    async def _fake_transition(*_a, **_k):
        return None

    monkeypatch.setattr(sprint_items_module, "_transition_status", _fake_transition)
    result = await db_module.transfer_sprint_item_claim(
        db, p["id"], item["id"], owner["id"], "new-owner",
    )
    assert result["blocked"] is True
    assert result["error"] == "RACE_LOST"


# ---------------------------------------------------------------------------
# 4. MCP schema registration (mirrors test_56e9b3c7_reconcile_stale_claims_mcp_exposure.py)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool_name", ["release_sprint_item_claim", "transfer_sprint_item_claim"])
def test_tool_present_exactly_once(tool_name):
    names = [t["name"] for t in _MCP_TOOLS_LIST if t["name"] == tool_name]
    assert names == [tool_name]


@pytest.mark.parametrize("tool_name", ["release_sprint_item_claim", "transfer_sprint_item_claim"])
def test_schema_advertises_project_id_and_project_name_alternative(tool_name):
    tool = _tool(tool_name)
    props = tool["inputSchema"]["properties"]
    assert "project_id" in props
    assert "project_name" in props
    assert "alternative to project_id" in props["project_name"]["description"]


@pytest.mark.parametrize("tool_name", ["release_sprint_item_claim", "transfer_sprint_item_claim"])
def test_tool_is_not_tagged_read_only(tool_name):
    assert tool_name not in _READ_ONLY_TOOLS
    tool = _tool(tool_name)
    assert tool["annotations"]["readOnlyHint"] is False
    assert tool["annotations"]["destructiveHint"] is False
    assert tool["annotations"]["idempotentHint"] is False


@pytest.mark.parametrize("tool_name", ["release_sprint_item_claim", "transfer_sprint_item_claim"])
def test_tool_category_and_role_relevance(tool_name):
    tool = _tool(tool_name)
    assert tool["category"] == "sprint-management"
    assert tool["role_relevance"] == "executor"


def test_release_schema_requires_item_id_and_session_id():
    tool = _tool("release_sprint_item_claim")
    assert set(tool["inputSchema"]["required"]) == {"item_id", "session_id"}


def test_transfer_schema_requires_item_id_session_id_to_actor():
    tool = _tool("transfer_sprint_item_claim")
    assert set(tool["inputSchema"]["required"]) == {"item_id", "session_id", "to_actor"}


# ---------------------------------------------------------------------------
# 5. HTTP/MCP dispatch
# ---------------------------------------------------------------------------


async def test_release_sprint_item_claim_mcp_dispatch(db):
    p = await db_module.create_project(db, "w1i-mcp-dispatch-release")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "release via mcp")
    owner = await db_module.register_session(db, p["id"], "mcp-release-owner")
    await db_module.claim_sprint_item(db, p["id"], item["id"], actor=owner["id"])

    result = await srv._dispatch_mcp_tool(
        "release_sprint_item_claim",
        {"project_id": p["id"], "item_id": item["id"], "session_id": owner["id"]},
        db, "/tmp",
    )
    assert result["item"]["status"] == "pending"

    unchanged = await db_module.get_sprint_item(db, item["id"])
    assert unchanged["status"] == "pending"
    assert unchanged["actor"] is None


async def test_release_sprint_item_claim_mcp_dispatch_ownership_block(db):
    p = await db_module.create_project(db, "w1i-mcp-dispatch-release-block")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "not yours via mcp")
    owner = await db_module.register_session(db, p["id"], "mcp-release-real-owner")
    intruder = await db_module.register_session(db, p["id"], "mcp-release-intruder")
    await db_module.claim_sprint_item(db, p["id"], item["id"], actor=owner["id"])

    result = await srv._dispatch_mcp_tool(
        "release_sprint_item_claim",
        {"project_id": p["id"], "item_id": item["id"], "session_id": intruder["id"]},
        db, "/tmp",
    )
    assert result["blocked"] is True
    assert result["error"] == "NOT_CLAIM_OWNER"


async def test_transfer_sprint_item_claim_mcp_dispatch(db):
    p = await db_module.create_project(db, "w1i-mcp-dispatch-transfer")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "transfer via mcp")
    from_sess = await db_module.register_session(db, p["id"], "mcp-transfer-from")
    to_sess = await db_module.register_session(db, p["id"], "mcp-transfer-to")
    await db_module.claim_sprint_item(db, p["id"], item["id"], actor=from_sess["id"])

    result = await srv._dispatch_mcp_tool(
        "transfer_sprint_item_claim",
        {
            "project_id": p["id"], "item_id": item["id"],
            "session_id": from_sess["id"], "to_actor": to_sess["id"],
        },
        db, "/tmp",
    )
    assert result["new_actor"] == to_sess["id"]

    fresh = await db_module.get_sprint_item(db, item["id"])
    assert fresh["status"] == "in_progress"
    assert fresh["actor"] == to_sess["id"]


async def test_release_sprint_item_claim_mcp_dispatch_resolves_project_name(db):
    p = await db_module.create_project(db, "w1i-mcp-dispatch-release-byname")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "release by project name")
    owner = await db_module.register_session(db, p["id"], "mcp-release-byname-owner")
    await db_module.claim_sprint_item(db, p["id"], item["id"], actor=owner["id"])

    result = await srv._dispatch_mcp_tool(
        "release_sprint_item_claim",
        {
            "project_name": "w1i-mcp-dispatch-release-byname",
            "item_id": item["id"], "session_id": owner["id"],
        },
        db, "/tmp",
    )
    assert result["item"]["status"] == "pending"


async def test_release_sprint_item_claim_mcp_dispatch_missing_project_id_error(db):
    result = await srv._dispatch_mcp_tool(
        "release_sprint_item_claim",
        {"item_id": "irrelevant", "session_id": "irrelevant"},
        db, "/tmp",
    )
    assert "error" in result


async def test_transfer_sprint_item_claim_mcp_dispatch_missing_to_actor_error(db):
    p = await db_module.create_project(db, "w1i-mcp-dispatch-transfer-missing")
    result = await srv._dispatch_mcp_tool(
        "transfer_sprint_item_claim",
        {"project_id": p["id"], "item_id": "irrelevant", "session_id": "irrelevant"},
        db, "/tmp",
    )
    assert "error" in result


# ---------------------------------------------------------------------------
# 6. stdio transport parity (mirrors test_stdio_schema_matches_canonical_schema)
# ---------------------------------------------------------------------------


def _build_stdio_server(monkeypatch, db):
    import meridian.server as server_module

    async def _return_db(*_a, **_k):
        return db

    monkeypatch.setattr(db_module, "init_db", _return_db)
    monkeypatch.setenv("MERIDIAN_DB", ":memory:")
    monkeypatch.delenv("MERIDIAN_DB_URL", raising=False)
    server, _run_stdio = server_module.build_mcp_server()
    return server


async def _stdio_list_tools(server):
    import mcp.types as mcp_types

    list_handler = server.request_handlers[mcp_types.ListToolsRequest]
    listed = await list_handler(mcp_types.ListToolsRequest())
    return listed.root.tools


@pytest.mark.parametrize("tool_name", ["release_sprint_item_claim", "transfer_sprint_item_claim"])
async def test_stdio_schema_matches_canonical_schema(db, monkeypatch, tool_name):
    server = _build_stdio_server(monkeypatch, db)
    tools = await _stdio_list_tools(server)
    stdio_tool = next(t for t in tools if t.name == tool_name)

    canonical = _tool(tool_name)
    assert stdio_tool.inputSchema == canonical["inputSchema"]
    assert stdio_tool.description == canonical["description"]


async def test_stdio_call_release_sprint_item_claim_dispatches_real_data(db, monkeypatch):
    import json

    import mcp.types as mcp_types

    server = _build_stdio_server(monkeypatch, db)
    p = await db_module.create_project(db, "w1i-stdio-release")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "released via stdio")
    owner = await db_module.register_session(db, p["id"], "stdio-release-owner")
    await db_module.claim_sprint_item(db, p["id"], item["id"], actor=owner["id"])

    call_handler = server.request_handlers[mcp_types.CallToolRequest]
    called = await call_handler(
        mcp_types.CallToolRequest(
            params=mcp_types.CallToolRequestParams(
                name="release_sprint_item_claim",
                arguments={
                    "project_id": p["id"], "item_id": item["id"], "session_id": owner["id"],
                },
            )
        )
    )
    result = json.loads(called.root.content[0].text)
    assert result["item"]["status"] == "pending"
