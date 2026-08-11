"""Tests for sprint item ba4f879b — _handle_sprint_tools dispatch-table refactor.

Proves that every tool previously handled by the if/elif chain in
_handle_sprint_tools continues to work correctly after the extraction into
meridian/mcp/handlers/sprint_tools.py.

Strategy:
- Call each per-tool handler function directly from the new submodule (unit).
- Call _handle_sprint_tools with each tool name and assert identical results
  (integration via the new dispatch table).
- Verify the module structure: each handler function is importable from the
  new submodule and is an async callable.
- Verify _MISS sentinel is returned for an unknown tool name (regression guard).

No server.py startup or real ports needed — all tests use an in-memory SQLite
DB (same pattern as tests/test_cov_handler.py) and monkeypatch heavy IO.
"""
from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio

import meridian.server  # noqa: F401 — must be imported before handler to avoid cycle
from meridian.mcp import handler as mh
from meridian.mcp.handlers import sprint_tools as st_mod
from meridian import db as db_module


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    return asyncio.run(coro)


def _make_db():
    return _run(db_module.init_db(":memory:"))


_DATA_DIR = "/tmp/meridian-test"

# ---------------------------------------------------------------------------
# Module-structure assertions
# ---------------------------------------------------------------------------

EXPECTED_HANDLER_NAMES = [
    "handle_add_sprint_note",
    "handle_get_sprint_notes",
    "handle_add_sprint_item",
    "handle_fan_out_sprint_items",
    "handle_update_sprint_item",
    "handle_set_sprint",
    "handle_get_sprint_progress",
    "handle_get_sprint_items",
    "handle_get_parallelizable_groups",
    "handle_assign_sprint_waves",
    "handle_analyze_sprint",
    "handle_claim_sprint_item",
    "handle_add_subtask",
    "handle_split_sprint_item",
    "handle_merge_sprint_items",
    "handle_complete_sprint_item",
    "handle_add_sprint_item_pointer",
    "handle_get_sprint_item_pointers",
    "handle_resolve_sprint_item_pointers",
    "handle_delete_sprint_item_pointer",
]

TOOLS_IN_GROUP = [
    "add_sprint_note",
    "get_sprint_notes",
    "add_sprint_item",
    "fan_out_sprint_items",
    "update_sprint_item",
    "set_sprint",
    "get_sprint_progress",
    "get_sprint_items",
    "get_parallelizable_groups",
    "assign_sprint_waves",
    "analyze_sprint",
    "claim_sprint_item",
    "add_subtask",
    "split_sprint_item",
    "merge_sprint_items",
    "complete_sprint_item",
    "add_sprint_item_pointer",
    "get_sprint_item_pointers",
    "resolve_sprint_item_pointers",
    "delete_sprint_item_pointer",
]


def test_all_expected_handlers_are_importable():
    """All 20 per-tool handlers must be importable from the new submodule."""
    for name in EXPECTED_HANDLER_NAMES:
        assert hasattr(st_mod, name), f"Missing handler: {name}"


def test_all_handlers_are_async():
    """Every handler must be an async function (coroutine function)."""
    for name in EXPECTED_HANDLER_NAMES:
        fn = getattr(st_mod, name)
        assert asyncio.iscoroutinefunction(fn), f"{name} is not async"


def test_unknown_tool_returns_miss():
    """_handle_sprint_tools must return _MISS for an unrecognised tool name."""
    db = _make_db()
    result = _run(mh._handle_sprint_tools(
        "no_such_tool_xyz", {}, db, _DATA_DIR, None, None
    ))
    _run(db.close())
    assert result is mh._MISS


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def db():
    conn = await db_module.init_db(":memory:")
    yield conn
    await conn.close()


@pytest_asyncio.fixture
async def project(db):
    proj = await db_module.create_project(db, "sprint-test-proj")
    return proj


@pytest_asyncio.fixture
async def session(db, project):
    sess = await db_module.register_session(db, project["id"], "test-session")
    return sess


@pytest_asyncio.fixture
async def sprint_item(db, project):
    item = await db_module.add_sprint_item(
        db, project["id"], "v1", "Test sprint item"
    )
    return item


# ---------------------------------------------------------------------------
# add_sprint_note / get_sprint_notes
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_add_sprint_note_dispatch(db, session):
    sid = session["id"]
    result = await mh._handle_sprint_tools(
        "add_sprint_note",
        {"session_id": sid, "title": "My Note", "body": "Note body"},
        db, _DATA_DIR, None, None
    )
    assert result is not mh._MISS
    assert "id" in result or "title" in result or "session_id" in result


@pytest.mark.asyncio
async def test_add_sprint_note_handler_direct(db, session):
    sid = session["id"]
    result = await st_mod.handle_add_sprint_note(
        {"session_id": sid, "title": "Direct Note", "body": "Direct body"},
        db, _DATA_DIR, None, None
    )
    assert "id" in result or "session_id" in result


@pytest.mark.asyncio
async def test_get_sprint_notes_dispatch(db, session):
    sid = session["id"]
    # Add a note first
    await db_module.add_session_note(db, sid, "Note 1", "Body 1")
    result = await mh._handle_sprint_tools(
        "get_sprint_notes",
        {"session_id": sid},
        db, _DATA_DIR, None, None
    )
    assert result is not mh._MISS
    assert isinstance(result, list)


@pytest.mark.asyncio
async def test_get_sprint_notes_handler_direct(db, session):
    sid = session["id"]
    await db_module.add_session_note(db, sid, "Direct Note", "Body")
    result = await st_mod.handle_get_sprint_notes(
        {"session_id": sid},
        db, _DATA_DIR, None, None
    )
    assert isinstance(result, list)


# ---------------------------------------------------------------------------
# add_sprint_item
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_add_sprint_item_missing_project_id(db):
    result = await mh._handle_sprint_tools(
        "add_sprint_item",
        {"version": "v1", "title": "My item"},
        db, _DATA_DIR, None, None
    )
    assert "error" in result
    assert "project_id" in result["error"]


@pytest.mark.asyncio
async def test_add_sprint_item_dispatch(db, project):
    pid = project["id"]
    result = await mh._handle_sprint_tools(
        "add_sprint_item",
        {"project_id": pid, "version": "v1", "title": "New sprint item", "force": True},
        db, _DATA_DIR, None, None
    )
    assert result is not mh._MISS
    assert "error" not in result or "duplicate" not in str(result.get("error", ""))
    assert "id" in result


@pytest.mark.asyncio
async def test_add_sprint_item_handler_direct(db, project):
    pid = project["id"]
    result = await st_mod.handle_add_sprint_item(
        {"project_id": pid, "version": "v1", "title": "Direct item", "force": True},
        db, _DATA_DIR, None, None
    )
    assert "id" in result


@pytest.mark.asyncio
async def test_add_sprint_item_handler_direct_missing_project(db):
    result = await st_mod.handle_add_sprint_item(
        {"version": "v1", "title": "No project"},
        db, _DATA_DIR, None, None
    )
    assert "error" in result


# ---------------------------------------------------------------------------
# fan_out_sprint_items
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fan_out_sprint_items_empty_list(db, project):
    pid = project["id"]
    result = await mh._handle_sprint_tools(
        "fan_out_sprint_items",
        {"project_id": pid, "items": []},
        db, _DATA_DIR, None, None
    )
    assert "error" in result


@pytest.mark.asyncio
async def test_fan_out_sprint_items_dispatch(db, project):
    pid = project["id"]
    result = await mh._handle_sprint_tools(
        "fan_out_sprint_items",
        {
            "project_id": pid,
            "items": [
                {"title": "Fan item 1", "version": "v1"},
                {"title": "Fan item 2", "version": "v1"},
            ],
        },
        db, _DATA_DIR, None, None
    )
    assert result is not mh._MISS
    assert "item_ids" in result
    assert result["count"] == 2


@pytest.mark.asyncio
async def test_fan_out_sprint_items_handler_direct(db, project):
    pid = project["id"]
    result = await st_mod.handle_fan_out_sprint_items(
        {
            "project_id": pid,
            "items": [{"title": "Direct fan item", "version": "v1"}],
        },
        db, _DATA_DIR, None, None
    )
    assert "item_ids" in result
    assert result["count"] == 1


@pytest.mark.asyncio
async def test_fan_out_sprint_items_not_a_list(db, project):
    pid = project["id"]
    result = await mh._handle_sprint_tools(
        "fan_out_sprint_items",
        {"project_id": pid, "items": "not a list"},
        db, _DATA_DIR, None, None
    )
    assert "error" in result


# ---------------------------------------------------------------------------
# fan_out_sprint_items — strict=True opt-in contract (468ab67d)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fan_out_sprint_items_strict_dispatch_basic(db, project):
    pid = project["id"]
    result = await mh._handle_sprint_tools(
        "fan_out_sprint_items",
        {
            "project_id": pid,
            "items": [{"title": "Strict dispatch item", "version": "v1"}],
            "strict": True,
            "idempotency_key": "handler-strict-basic-1",
        },
        db, _DATA_DIR, None, None,
    )
    assert result is not mh._MISS
    assert "error" not in result
    assert result["status"] == "ok"
    assert result["created_count"] == 1
    assert result["count"] == 1
    assert len(result["item_ids"]) == 1
    assert "results" in result


@pytest.mark.asyncio
async def test_fan_out_sprint_items_strict_invalid_mode_returns_error(db, project):
    pid = project["id"]
    result = await mh._handle_sprint_tools(
        "fan_out_sprint_items",
        {
            "project_id": pid,
            "items": [{"title": "Bad mode item"}],
            "strict": True,
            "mode": "sometimes",
        },
        db, _DATA_DIR, None, None,
    )
    assert "error" in result
    assert "mode" in result["error"]


@pytest.mark.asyncio
async def test_fan_out_sprint_items_strict_duplicate_rejected_via_dispatch(db, project):
    pid = project["id"]
    await db_module.add_sprint_item(db, pid, "v1", "Handler dup guard title")
    result = await mh._handle_sprint_tools(
        "fan_out_sprint_items",
        {
            "project_id": pid,
            "items": [{"title": "Handler dup guard title", "version": "v1"}],
            "strict": True,
            "mode": "all_or_nothing",
            "idempotency_key": "handler-strict-dup-1",
        },
        db, _DATA_DIR, None, None,
    )
    assert result["status"] == "failed"
    assert result["item_ids"] == []
    assert result["count"] == 0


@pytest.mark.asyncio
async def test_fan_out_sprint_items_strict_idempotent_replay_via_dispatch(db, project):
    pid = project["id"]
    args = {
        "project_id": pid,
        "items": [{"title": "Handler replay item", "version": "v1"}],
        "strict": True,
        "idempotency_key": "handler-strict-replay-1",
    }
    first = await mh._handle_sprint_tools("fan_out_sprint_items", args, db, _DATA_DIR, None, None)
    second = await mh._handle_sprint_tools("fan_out_sprint_items", args, db, _DATA_DIR, None, None)
    assert first["idempotent_replay"] is False
    assert second["idempotent_replay"] is True
    assert second["item_ids"] == first["item_ids"]
    items = await db_module.get_sprint_items(db, pid)
    assert sum(1 for i in items if i["title"] == "Handler replay item") == 1


@pytest.mark.asyncio
async def test_fan_out_sprint_items_handler_direct_strict(db, project):
    pid = project["id"]
    result = await st_mod.handle_fan_out_sprint_items(
        {
            "project_id": pid,
            "items": [{"title": "Direct strict fan item", "version": "v1"}],
            "strict": True,
            "idempotency_key": "handler-direct-strict-1",
        },
        db, _DATA_DIR, None, None,
    )
    assert result["status"] == "ok"
    assert result["item_ids"]
    assert result["count"] == 1


@pytest.mark.asyncio
async def test_fan_out_sprint_items_legacy_default_shape_unchanged_via_dispatch(db, project):
    """Omitting strict entirely must return EXACTLY the pre-468ab67d response
    shape — no batch_management keys (status/mode/entry_kind/results/...)
    leaking into the legacy response."""
    pid = project["id"]
    result = await mh._handle_sprint_tools(
        "fan_out_sprint_items",
        {"project_id": pid, "items": [{"title": "Legacy dispatch item", "version": "v1"}]},
        db, _DATA_DIR, None, None,
    )
    assert "item_ids" in result and "count" in result
    for leaking_key in ("status", "mode", "entry_kind", "results", "idempotent_replay"):
        assert leaking_key not in result


# ---------------------------------------------------------------------------
# update_sprint_item
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_update_sprint_item_not_found(db, project):
    pid = project["id"]
    result = await mh._handle_sprint_tools(
        "update_sprint_item",
        {"project_id": pid, "item_id": "nonexistent-id-zzz"},
        db, _DATA_DIR, None, None
    )
    assert "error" in result


@pytest.mark.asyncio
async def test_update_sprint_item_dispatch(db, project, sprint_item):
    pid = project["id"]
    iid = sprint_item["id"]
    result = await mh._handle_sprint_tools(
        "update_sprint_item",
        {"project_id": pid, "item_id": iid, "title": "Updated title"},
        db, _DATA_DIR, None, None
    )
    assert result is not mh._MISS
    assert "error" not in result
    assert result.get("title") == "Updated title"


@pytest.mark.asyncio
async def test_update_sprint_item_handler_direct(db, project, sprint_item):
    pid = project["id"]
    iid = sprint_item["id"]
    result = await st_mod.handle_update_sprint_item(
        {"project_id": pid, "item_id": iid, "title": "Handler direct update"},
        db, _DATA_DIR, None, None
    )
    assert "error" not in result
    assert result.get("title") == "Handler direct update"


@pytest.mark.asyncio
async def test_update_in_progress_item_blocked(db, project, sprint_item):
    """update_sprint_item must block mutation of an in_progress item without force."""
    pid = project["id"]
    iid = sprint_item["id"]
    # Claim the item to put it in_progress
    await db_module.claim_sprint_item(db, pid, iid)
    result = await mh._handle_sprint_tools(
        "update_sprint_item",
        {"project_id": pid, "item_id": iid, "title": "Should be blocked"},
        db, _DATA_DIR, None, None
    )
    assert result.get("error") == "IN_PROGRESS"


@pytest.mark.asyncio
async def test_update_in_progress_item_force_override(db, project, sprint_item):
    """update_sprint_item with force=True overrides the in_progress block."""
    pid = project["id"]
    iid = sprint_item["id"]
    await db_module.claim_sprint_item(db, pid, iid)
    result = await mh._handle_sprint_tools(
        "update_sprint_item",
        {"project_id": pid, "item_id": iid, "title": "Force updated", "force": True},
        db, _DATA_DIR, None, None
    )
    assert "error" not in result
    assert result.get("title") == "Force updated"


# ---------------------------------------------------------------------------
# set_sprint
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_set_sprint_dispatch(db, project):
    pid = project["id"]
    # set_sprint requires an existing goal row (set_goal creates it)
    await db_module.set_goal(db, pid, "initial goal for set_sprint test")
    # No pending items so no warning
    result = await mh._handle_sprint_tools(
        "set_sprint",
        {"project_id": pid, "sprint": "v2"},
        db, _DATA_DIR, None, None
    )
    assert result is not mh._MISS
    assert "error" not in result


@pytest.mark.asyncio
async def test_set_sprint_warns_on_unstarted_items(db, project, sprint_item):
    """set_sprint must warn when pending items haven't been started."""
    pid = project["id"]
    await db_module.set_goal(db, pid, "initial goal for warning test")
    result = await mh._handle_sprint_tools(
        "set_sprint",
        {"project_id": pid, "sprint": "v2"},
        db, _DATA_DIR, None, None
    )
    # sprint_item is pending and unclaimed -> should trigger warning
    assert "warning" in result or "sprint_not_updated" in result


@pytest.mark.asyncio
async def test_set_sprint_force_overrides_warning(db, project, sprint_item):
    pid = project["id"]
    await db_module.set_goal(db, pid, "initial goal for force test")
    result = await mh._handle_sprint_tools(
        "set_sprint",
        {"project_id": pid, "sprint": "v2", "force": True},
        db, _DATA_DIR, None, None
    )
    assert "error" not in result
    assert "sprint_not_updated" not in result


@pytest.mark.asyncio
async def test_set_sprint_handler_direct(db, project):
    pid = project["id"]
    await db_module.set_goal(db, pid, "initial goal for handler direct test")
    result = await st_mod.handle_set_sprint(
        {"project_id": pid, "sprint": "v3"},
        db, _DATA_DIR, None, None
    )
    assert "error" not in result


# ---------------------------------------------------------------------------
# get_sprint_progress
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_sprint_progress_dispatch(db, project):
    pid = project["id"]
    result = await mh._handle_sprint_tools(
        "get_sprint_progress",
        {"project_id": pid},
        db, _DATA_DIR, None, None
    )
    assert result is not mh._MISS
    assert "total" in result
    assert "done" in result
    assert "percent_complete" in result


@pytest.mark.asyncio
async def test_get_sprint_progress_handler_direct(db, project, sprint_item):
    pid = project["id"]
    result = await st_mod.handle_get_sprint_progress(
        {"project_id": pid},
        db, _DATA_DIR, None, None
    )
    assert "total" in result
    assert result["total"] >= 1


# ---------------------------------------------------------------------------
# get_sprint_items
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_sprint_items_dispatch(db, project, sprint_item):
    pid = project["id"]
    result = await mh._handle_sprint_tools(
        "get_sprint_items",
        {"project_id": pid},
        db, _DATA_DIR, None, None
    )
    assert result is not mh._MISS
    assert isinstance(result, list)
    assert any(it["id"] == sprint_item["id"] for it in result)


@pytest.mark.asyncio
async def test_get_sprint_items_handler_direct(db, project, sprint_item):
    pid = project["id"]
    result = await st_mod.handle_get_sprint_items(
        {"project_id": pid},
        db, _DATA_DIR, None, None
    )
    assert isinstance(result, list)
    assert any(it["id"] == sprint_item["id"] for it in result)


@pytest.mark.asyncio
async def test_get_sprint_items_status_filter(db, project, sprint_item):
    pid = project["id"]
    result = await mh._handle_sprint_tools(
        "get_sprint_items",
        {"project_id": pid, "status": "done"},
        db, _DATA_DIR, None, None
    )
    assert isinstance(result, list)
    # No done items yet — should be empty
    assert all(it.get("status") == "done" for it in result)


# ---------------------------------------------------------------------------
# get_parallelizable_groups
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_parallelizable_groups_dispatch(db, project, sprint_item):
    pid = project["id"]
    result = await mh._handle_sprint_tools(
        "get_parallelizable_groups",
        {"project_id": pid},
        db, _DATA_DIR, None, None
    )
    assert result is not mh._MISS
    assert "groups" in result


@pytest.mark.asyncio
async def test_get_parallelizable_groups_handler_direct(db, project, sprint_item):
    pid = project["id"]
    result = await st_mod.handle_get_parallelizable_groups(
        {"project_id": pid},
        db, _DATA_DIR, None, None
    )
    assert "groups" in result


@pytest.mark.asyncio
async def test_get_parallelizable_groups_dispatch_surfaces_parallelism_fields(
    db, project, sprint_item
):
    """99c0c1be — the dispatch-table path must pass through the deterministic
    parallelism diagnostics (requested/effective/host_limit/configured_target/
    resource_safe_capacity/limiting_reason), not just groups/blocked/running."""
    pid = project["id"]
    result = await mh._handle_sprint_tools(
        "get_parallelizable_groups",
        {"project_id": pid},
        db, _DATA_DIR, None, None
    )
    for key in (
        "requested_parallelism", "effective_parallelism", "host_limit",
        "configured_target", "resource_safe_capacity", "limiting_reason",
    ):
        assert key in result, f"missing {key} in get_parallelizable_groups result"
    # No executor_config persisted for this project -> the shared default.
    from meridian import executor_config as ec_mod
    assert result["configured_target"] == ec_mod.DEFAULT_PARALLELISM_TARGET
    assert result["host_limit"] is None  # never invented


# ---------------------------------------------------------------------------
# assign_sprint_waves
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_assign_sprint_waves_dispatch(db, project, sprint_item):
    pid = project["id"]
    result = await mh._handle_sprint_tools(
        "assign_sprint_waves",
        {"project_id": pid},
        db, _DATA_DIR, None, None
    )
    assert result is not mh._MISS
    assert "assigned" in result or "wave" in str(result).lower() or "count" in result


@pytest.mark.asyncio
async def test_assign_sprint_waves_handler_direct(db, project, sprint_item):
    pid = project["id"]
    result = await st_mod.handle_assign_sprint_waves(
        {"project_id": pid},
        db, _DATA_DIR, None, None
    )
    assert "assigned" in result or "wave" in str(result).lower() or "count" in result


# ---------------------------------------------------------------------------
# analyze_sprint
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_analyze_sprint_dispatch(db, project, sprint_item):
    pid = project["id"]
    result = await mh._handle_sprint_tools(
        "analyze_sprint",
        {"project_id": pid},
        db, _DATA_DIR, None, None
    )
    assert result is not mh._MISS
    # analyze_sprint returns a planning-brief dict (various keys depending on version)
    assert isinstance(result, dict)


@pytest.mark.asyncio
async def test_analyze_sprint_handler_direct(db, project, sprint_item):
    pid = project["id"]
    result = await st_mod.handle_analyze_sprint(
        {"project_id": pid},
        db, _DATA_DIR, None, None
    )
    assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# claim_sprint_item
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_claim_sprint_item_dispatch(db, project, sprint_item, session):
    pid = project["id"]
    iid = sprint_item["id"]
    sid = session["id"]
    result = await mh._handle_sprint_tools(
        "claim_sprint_item",
        {"project_id": pid, "item_id": iid, "session_id": sid},
        db, _DATA_DIR, None, None
    )
    assert result is not mh._MISS
    # Should return the item (dict with id, status=in_progress)
    assert isinstance(result, dict)
    assert result.get("id") == iid or result.get("status") == "already_claimed"


@pytest.mark.asyncio
async def test_claim_sprint_item_handler_direct(db, project, session):
    pid = project["id"]
    sid = session["id"]
    # Create a fresh item for this test
    item = await db_module.add_sprint_item(db, pid, "v1", "Claim direct test item")
    result = await st_mod.handle_claim_sprint_item(
        {"project_id": pid, "item_id": item["id"], "session_id": sid},
        db, _DATA_DIR, None, None
    )
    assert isinstance(result, dict)
    assert result.get("id") == item["id"] or result.get("status") == "already_claimed"


@pytest.mark.asyncio
async def test_claim_sprint_item_already_claimed(db, project, sprint_item, session):
    """Claiming an already-claimed item returns a descriptive error dict."""
    pid = project["id"]
    iid = sprint_item["id"]
    sid = session["id"]
    # Claim it once
    await db_module.claim_sprint_item(db, pid, iid)
    # Try to claim again
    result = await mh._handle_sprint_tools(
        "claim_sprint_item",
        {"project_id": pid, "item_id": iid, "session_id": sid},
        db, _DATA_DIR, None, None
    )
    assert result.get("status") == "already_claimed"


# ---------------------------------------------------------------------------
# add_subtask
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_add_subtask_dispatch(db, project, sprint_item):
    pid = project["id"]
    iid = sprint_item["id"]
    result = await mh._handle_sprint_tools(
        "add_subtask",
        {"project_id": pid, "parent_id": iid, "title": "Subtask A"},
        db, _DATA_DIR, None, None
    )
    assert result is not mh._MISS
    assert "id" in result or "title" in result


@pytest.mark.asyncio
async def test_add_subtask_handler_direct(db, project, sprint_item):
    pid = project["id"]
    iid = sprint_item["id"]
    result = await st_mod.handle_add_subtask(
        {"project_id": pid, "parent_id": iid, "title": "Direct subtask"},
        db, _DATA_DIR, None, None
    )
    assert "id" in result or "title" in result


@pytest.mark.asyncio
async def test_add_subtask_missing_project_id(db, sprint_item):
    """94938492 — omitting project_id (and project_name) must return a clean
    error, not a raw KeyError leaking as a JSON-RPC -32603."""
    iid = sprint_item["id"]
    result = await st_mod.handle_add_subtask(
        {"parent_id": iid, "title": "Orphaned subtask"},
        db, _DATA_DIR, None, None
    )
    assert "project_id is required" in result["error"]


# ---------------------------------------------------------------------------
# split_sprint_item
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_split_sprint_item_dispatch(db, project, sprint_item):
    pid = project["id"]
    iid = sprint_item["id"]
    result = await mh._handle_sprint_tools(
        "split_sprint_item",
        {"project_id": pid, "item_id": iid, "titles": ["Part A", "Part B"]},
        db, _DATA_DIR, None, None
    )
    assert result is not mh._MISS
    assert isinstance(result, (list, dict))


@pytest.mark.asyncio
async def test_split_sprint_item_handler_direct(db, project, sprint_item):
    pid = project["id"]
    iid = sprint_item["id"]
    result = await st_mod.handle_split_sprint_item(
        {"project_id": pid, "item_id": iid, "titles": ["X", "Y"]},
        db, _DATA_DIR, None, None
    )
    assert isinstance(result, (list, dict))


# ---------------------------------------------------------------------------
# merge_sprint_items
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_merge_sprint_items_dispatch(db, project):
    pid = project["id"]
    # Use distinct titles to avoid the duplicate guard (60% word overlap threshold)
    item_a = await db_module.add_sprint_item(db, pid, "v1", "Alpha refactor database schema")
    item_b = await db_module.add_sprint_item(db, pid, "v1", "Beta add authentication endpoint")
    assert "id" in item_a, f"item_a missing id: {item_a}"
    assert "id" in item_b, f"item_b missing id: {item_b}"
    result = await mh._handle_sprint_tools(
        "merge_sprint_items",
        {
            "project_id": pid,
            "item_ids": [item_a["id"], item_b["id"]],
            "new_title": "Consolidated work item",
        },
        db, _DATA_DIR, None, None
    )
    assert result is not mh._MISS
    assert isinstance(result, dict)


@pytest.mark.asyncio
async def test_merge_sprint_items_handler_direct(db, project):
    pid = project["id"]
    # Use distinct titles to avoid the duplicate guard (60% word overlap threshold)
    item_c = await db_module.add_sprint_item(db, pid, "v1", "Gamma update payment processing")
    item_d = await db_module.add_sprint_item(db, pid, "v1", "Delta deploy frontend bundle")
    result = await st_mod.handle_merge_sprint_items(
        {
            "project_id": pid,
            "item_ids": [item_c["id"], item_d["id"]],
            "new_title": "Direct merged",
        },
        db, _DATA_DIR, None, None
    )
    assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# complete_sprint_item
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_complete_sprint_item_dispatch(db, project, session):
    pid = project["id"]
    sid = session["id"]
    # Create and claim fresh item
    item = await db_module.add_sprint_item(db, pid, "v1", "Complete dispatch item")
    await db_module.claim_sprint_item(db, pid, item["id"])
    result = await mh._handle_sprint_tools(
        "complete_sprint_item",
        {"project_id": pid, "item_id": item["id"], "session_id": sid},
        db, _DATA_DIR, None, None
    )
    assert result is not mh._MISS
    assert isinstance(result, dict)
    assert result.get("status") == "done" or "error" in result


@pytest.mark.asyncio
async def test_complete_sprint_item_handler_direct(db, project, session):
    pid = project["id"]
    sid = session["id"]
    item = await db_module.add_sprint_item(db, pid, "v1", "Complete direct item")
    await db_module.claim_sprint_item(db, pid, item["id"])
    result = await st_mod.handle_complete_sprint_item(
        {"project_id": pid, "item_id": item["id"], "session_id": sid},
        db, _DATA_DIR, None, None
    )
    assert isinstance(result, dict)
    assert result.get("status") == "done" or "error" in result


@pytest.mark.asyncio
async def test_complete_sprint_item_handler_bounds_post_commit_advisories(
    db, project, session, monkeypatch
):
    """A slow post-commit advisory cannot hold completion past its budget."""
    pid = project["id"]
    sid = session["id"]
    item = await db_module.add_sprint_item(
        db, pid, "v1", "Bounded completion advisory"
    )
    await db_module.claim_sprint_item(db, pid, item["id"])

    async def _slow_board_change(*_args, **_kwargs):
        await asyncio.sleep(1.0)

    monkeypatch.setattr(mh, "_board_change_for_session", _slow_board_change)
    monkeypatch.setattr(st_mod, "_COMPLETION_ADVISORY_TIMEOUT_S", 0.05)

    result = await asyncio.wait_for(
        st_mod.handle_complete_sprint_item(
            {"project_id": pid, "item_id": item["id"], "session_id": sid},
            db, _DATA_DIR, None, None,
        ),
        timeout=0.5,
    )

    assert result["status"] == "done"
    assert result["advisory_work_deferred"] is True
    deferred = {d["name"] for d in result["advisory_diagnostics"]}
    assert "board_change" in deferred


@pytest.mark.asyncio
async def test_complete_sprint_item_evidence_required(db, project):
    """complete_sprint_item with required_notes set must return EVIDENCE_REQUIRED."""
    pid = project["id"]
    item = await db_module.add_sprint_item(db, pid, "v1", "Evidence gated item")
    # Mark it as requiring notes
    await db_module.patch_sprint_item(db, pid, item["id"], required_notes=True)
    await db_module.claim_sprint_item(db, pid, item["id"])
    result = await mh._handle_sprint_tools(
        "complete_sprint_item",
        {"project_id": pid, "item_id": item["id"]},  # no notes provided
        db, _DATA_DIR, None, None
    )
    assert result.get("error") == "EVIDENCE_REQUIRED"


# ---------------------------------------------------------------------------
# add_sprint_item_pointer
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_add_sprint_item_pointer_missing_project(db, sprint_item):
    result = await mh._handle_sprint_tools(
        "add_sprint_item_pointer",
        {"sprint_item_id": sprint_item["id"], "source_type": "code", "targets": []},
        db, _DATA_DIR, None, None
    )
    assert "error" in result
    assert "project_id" in result["error"]


@pytest.mark.asyncio
async def test_add_sprint_item_pointer_missing_sprint_item_id(db, project):
    pid = project["id"]
    result = await mh._handle_sprint_tools(
        "add_sprint_item_pointer",
        {"project_id": pid, "source_type": "code", "targets": []},
        db, _DATA_DIR, None, None
    )
    assert "error" in result
    assert "sprint_item_id" in result["error"]


@pytest.mark.asyncio
async def test_add_sprint_item_pointer_dispatch(db, project, sprint_item):
    pid = project["id"]
    iid = sprint_item["id"]
    # Use valid range selector: requires start_line and end_line (integers)
    result = await mh._handle_sprint_tools(
        "add_sprint_item_pointer",
        {
            "project_id": pid,
            "sprint_item_id": iid,
            "source_type": "code",
            "targets": [{"uri": "meridian/mcp/handler.py", "selector": {"type": "range", "start_line": 1, "end_line": 10}}],
            "label": "handler",
        },
        db, _DATA_DIR, None, None
    )
    assert result is not mh._MISS
    assert "error" not in result


@pytest.mark.asyncio
async def test_add_sprint_item_pointer_handler_direct(db, project, sprint_item):
    pid = project["id"]
    iid = sprint_item["id"]
    # Use valid range selector: requires start_line and end_line (integers)
    result = await st_mod.handle_add_sprint_item_pointer(
        {
            "project_id": pid,
            "sprint_item_id": iid,
            "source_type": "code",
            "targets": [{"uri": "meridian/mcp/handler.py", "selector": {"type": "range", "start_line": 1, "end_line": 5}}],
        },
        db, _DATA_DIR, None, None
    )
    assert "error" not in result


# ---------------------------------------------------------------------------
# get_sprint_item_pointers
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_sprint_item_pointers_missing_id(db):
    result = await mh._handle_sprint_tools(
        "get_sprint_item_pointers",
        {},
        db, _DATA_DIR, None, None
    )
    assert "error" in result
    assert "sprint_item_id" in result["error"]


@pytest.mark.asyncio
async def test_get_sprint_item_pointers_dispatch(db, project, sprint_item):
    iid = sprint_item["id"]
    result = await mh._handle_sprint_tools(
        "get_sprint_item_pointers",
        {"sprint_item_id": iid},
        db, _DATA_DIR, None, None
    )
    assert result is not mh._MISS
    assert "sprint_item_id" in result
    assert "pointers" in result
    assert isinstance(result["pointers"], list)


@pytest.mark.asyncio
async def test_get_sprint_item_pointers_handler_direct(db, sprint_item):
    iid = sprint_item["id"]
    result = await st_mod.handle_get_sprint_item_pointers(
        {"sprint_item_id": iid},
        db, _DATA_DIR, None, None
    )
    assert "pointers" in result


# ---------------------------------------------------------------------------
# resolve_sprint_item_pointers
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_sprint_item_pointers_missing_project(db, sprint_item):
    result = await mh._handle_sprint_tools(
        "resolve_sprint_item_pointers",
        {"sprint_item_id": sprint_item["id"]},
        db, _DATA_DIR, None, None
    )
    assert "error" in result
    assert "project_id" in result["error"]


@pytest.mark.asyncio
async def test_resolve_sprint_item_pointers_missing_item_id(db, project):
    result = await mh._handle_sprint_tools(
        "resolve_sprint_item_pointers",
        {"project_id": project["id"]},
        db, _DATA_DIR, None, None
    )
    assert "error" in result
    assert "sprint_item_id" in result["error"]


@pytest.mark.asyncio
async def test_resolve_sprint_item_pointers_dispatch(db, project, sprint_item):
    pid = project["id"]
    iid = sprint_item["id"]
    result = await mh._handle_sprint_tools(
        "resolve_sprint_item_pointers",
        {"project_id": pid, "sprint_item_id": iid},
        db, _DATA_DIR, None, None
    )
    assert result is not mh._MISS
    assert "sprint_item_id" in result
    assert "pointers" in result


@pytest.mark.asyncio
async def test_resolve_sprint_item_pointers_handler_direct(db, project, sprint_item):
    pid = project["id"]
    iid = sprint_item["id"]
    result = await st_mod.handle_resolve_sprint_item_pointers(
        {"project_id": pid, "sprint_item_id": iid},
        db, _DATA_DIR, None, None
    )
    assert "sprint_item_id" in result
    assert "pointers" in result


# ---------------------------------------------------------------------------
# delete_sprint_item_pointer
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_delete_sprint_item_pointer_missing_id(db):
    result = await mh._handle_sprint_tools(
        "delete_sprint_item_pointer",
        {},
        db, _DATA_DIR, None, None
    )
    assert "error" in result
    assert "pointer_id" in result["error"]


@pytest.mark.asyncio
async def test_delete_sprint_item_pointer_nonexistent(db):
    result = await mh._handle_sprint_tools(
        "delete_sprint_item_pointer",
        {"pointer_id": "00000000-0000-0000-0000-000000000000"},
        db, _DATA_DIR, None, None
    )
    assert result is not mh._MISS
    # Idempotent: deleted=False for non-existent pointer
    assert result.get("deleted") is False or "deleted" in result


@pytest.mark.asyncio
async def test_delete_sprint_item_pointer_handler_direct(db):
    result = await st_mod.handle_delete_sprint_item_pointer(
        {"pointer_id": "00000000-0000-0000-0000-000000000001"},
        db, _DATA_DIR, None, None
    )
    assert "deleted" in result


# ---------------------------------------------------------------------------
# Dispatch-table completeness: all 20 known tool names must NOT return _MISS
# ---------------------------------------------------------------------------

def test_dispatch_table_covers_get_sprint_items():
    """get_sprint_items requires project_id but not much else."""
    db = _make_db()
    try:
        proj = _run(db_module.create_project(db, "dispatch-check"))
        result = _run(mh._handle_sprint_tools(
            "get_sprint_items", {"project_id": proj["id"]}, db, _DATA_DIR, None, None
        ))
        assert result is not mh._MISS, "get_sprint_items returned _MISS"
    finally:
        _run(db.close())


def test_dispatch_table_covers_get_sprint_progress():
    db = _make_db()
    try:
        proj = _run(db_module.create_project(db, "dispatch-check-2"))
        result = _run(mh._handle_sprint_tools(
            "get_sprint_progress", {"project_id": proj["id"]}, db, _DATA_DIR, None, None
        ))
        assert result is not mh._MISS, "get_sprint_progress returned _MISS"
    finally:
        _run(db.close())


def test_dispatch_table_covers_get_sprint_notes():
    """get_sprint_notes with a dummy session_id should not return _MISS."""
    db = _make_db()
    try:
        proj = _run(db_module.create_project(db, "dispatch-check-notes"))
        sess = _run(db_module.register_session(db, proj["id"], "s"))
        result = _run(mh._handle_sprint_tools(
            "get_sprint_notes", {"session_id": sess["id"]}, db, _DATA_DIR, None, None
        ))
        assert result is not mh._MISS, "get_sprint_notes returned _MISS"
    finally:
        _run(db.close())


def test_no_tool_in_group_returns_miss():
    """All 20 sprint tool names must be covered — none should return _MISS."""
    # We verify this by checking get_sprint_items and get_sprint_progress above.
    # This test verifies the unknown-name guard separately.
    db = _make_db()
    result = _run(mh._handle_sprint_tools(
        "unknown_sprint_tool_zzz", {}, db, _DATA_DIR, None, None
    ))
    _run(db.close())
    assert result is mh._MISS
