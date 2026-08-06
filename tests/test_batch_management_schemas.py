"""Tests for sprint item 627187b8 -- multi-transport exposure of
``meridian.db.batch_management.execute_batch`` (86e4ae44).

Covers the schema/manifest/docs half of the acceptance criteria:

1. ``execute_batch`` is registered in ``meridian.mcp_tools._MCP_TOOLS_LIST``
   with a stable input schema: required ``project_id``/``operation``/
   ``entries``/``mode``/``idempotency_key``, and the ``operation``/``mode``
   enums match ``meridian.batch_ops``' own constants exactly (no drift
   between the schema and the code that enforces it).
2. The stdio transport advertises the IDENTICAL schema object (via
   ``_shared_tool``), not a hand-copied duplicate that can drift.
3. The tool is correctly categorised (category/role_relevance/workflow_tier)
   and is neither read-only nor destructive.
4. The connector/tool-manifest generator (``meridian.tool_manifest.
   build_tool_manifest``) picks the tool up automatically.
5. The HTTP route exists on the live FastAPI app.
6. docs/mcp-tools.md and docs/api-reference.md (regenerated via
   ``scripts/gen_docs.py``) mention the new tool/route -- the exhaustive
   byte-for-byte drift check already lives in
   ``tests/test_core.py::test_docs_mcp_tools_matches_live_tool_doc`` /
   ``test_docs_api_reference_matches_live_doc``; this file adds a narrower,
   feature-specific presence check.
7. ``meridian.batch_ops``' request-shape validation and operation ->
   entry_kind / forced-action normalization, unit-tested directly.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import pytest_asyncio

import meridian.server as server_module  # noqa: F401 — load before mcp.handler (import-cycle guard)
from meridian import batch_ops
from meridian import db as db_module
from meridian import mcp_tools
from meridian import tool_manifest as tool_manifest_module
from meridian.db import batch_management as bm

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _find_tool(name: str) -> dict:
    return next(t for t in mcp_tools._MCP_TOOLS_LIST if t["name"] == name)


# ---------------------------------------------------------------------------
# 1. MCP tool schema shape
# ---------------------------------------------------------------------------

def test_execute_batch_registered_in_mcp_tools_list():
    names = [t["name"] for t in mcp_tools._MCP_TOOLS_LIST]
    assert "execute_batch" in names
    assert names.count("execute_batch") == 1


def test_execute_batch_schema_required_fields():
    tool = _find_tool("execute_batch")
    schema = tool["inputSchema"]
    required = set(schema["required"])
    # project_id is deliberately NOT in the JSON-schema required array —
    # project_name is an accepted alternative (resolved to project_id by the
    # dispatcher before any handler runs), and the MCP low-level Server
    # validates `required` fields BEFORE that resolution happens. Listing
    # project_id as required would hard-reject a legitimate project_name-only
    # call at the protocol layer. See
    # tests/test_core.py::test_every_project_id_tool_schema_advertises_project_name
    # for the repo-wide convention this follows; project_id is still
    # effectively required at runtime via the handler's own
    # "project_id is required (or pass project_name)" check.
    assert required == {"operation", "entries", "mode", "idempotency_key"}
    props = schema["properties"]
    assert "project_id" in props
    assert "project_name" in props
    assert "alternative to project_id" in props["project_name"]["description"]
    assert set(props["operation"]["enum"]) == set(batch_ops.BATCH_OPERATIONS)
    assert set(props["mode"]["enum"]) == set(bm.BATCH_MODES)
    assert props["entries"]["type"] == "array"
    assert "idempotency_key" in props
    assert "max_entries" in props
    assert "session_id" in props


def test_execute_batch_annotations_not_readonly_not_destructive():
    tool = _find_tool("execute_batch")
    assert tool["annotations"]["readOnlyHint"] is False
    assert tool["annotations"]["destructiveHint"] is False


def test_execute_batch_category_role_tier():
    assert mcp_tools._TOOL_CATEGORY.get("execute_batch") == "sprint-management"
    assert mcp_tools._TOOL_ROLE_RELEVANCE.get("execute_batch") in ("both", "executor", "planner")
    assert mcp_tools._TOOL_WORKFLOW_TIER.get("execute_batch") == "common-support"


def test_execute_batch_has_example():
    assert "execute_batch" in mcp_tools._TOOL_EXAMPLES


# ---------------------------------------------------------------------------
# 2. stdio transport advertises the IDENTICAL schema
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stdio_tool_schema_is_the_shared_schema(db, monkeypatch):
    import mcp.types as mcp_types

    async def _return_db(*_a, **_k):
        return db

    monkeypatch.setattr(db_module, "init_db", _return_db)
    monkeypatch.setenv("MERIDIAN_DB", ":memory:")
    monkeypatch.delenv("MERIDIAN_DB_URL", raising=False)

    server, _run_stdio = server_module.build_mcp_server()
    list_handler = server.request_handlers[mcp_types.ListToolsRequest]
    listed = await list_handler(mcp_types.ListToolsRequest())
    stdio_tool = next(t for t in listed.root.tools if t.name == "execute_batch")

    canonical = _find_tool("execute_batch")
    assert stdio_tool.description == canonical["description"]
    assert stdio_tool.inputSchema == canonical["inputSchema"]


# ---------------------------------------------------------------------------
# 3. Connector / tool manifest generation
# ---------------------------------------------------------------------------

def test_connector_manifest_includes_execute_batch():
    manifest = tool_manifest_module.build_tool_manifest(mcp_tools._MCP_TOOLS_LIST)
    names = [t["name"] for t in manifest["tools"]]
    assert "execute_batch" in names
    entry = next(t for t in manifest["tools"] if t["name"] == "execute_batch")
    assert entry["summary"]  # non-empty first-sentence summary
    assert manifest["count"] == len(mcp_tools._MCP_TOOLS_LIST)


def test_tool_manifest_revision_changes_if_schema_changes():
    rev_full = tool_manifest_module.tool_manifest_revision(mcp_tools._MCP_TOOLS_LIST)
    without_batch = [t for t in mcp_tools._MCP_TOOLS_LIST if t["name"] != "execute_batch"]
    rev_without = tool_manifest_module.tool_manifest_revision(without_batch)
    assert rev_full != rev_without


# ---------------------------------------------------------------------------
# 4. HTTP route registered
# ---------------------------------------------------------------------------

def test_http_route_registered():
    from fastapi.routing import APIRoute

    matches = [
        r for r in server_module.app.routes
        if isinstance(r, APIRoute)
        and r.path == "/projects/{project_id}/sprint-batch"
        and "POST" in (r.methods or set())
    ]
    assert len(matches) == 1
    assert matches[0].endpoint.__name__ == "execute_batch_endpoint"


# ---------------------------------------------------------------------------
# 5. Docs sync — feature-specific presence check (exhaustive drift check
#    lives in tests/test_core.py's docs-matches-live-doc tests).
# ---------------------------------------------------------------------------

def test_docs_mcp_tools_mentions_execute_batch():
    content = (_REPO_ROOT / "docs" / "mcp-tools.md").read_text(encoding="utf-8")
    assert "execute_batch" in content


def test_docs_api_reference_mentions_sprint_batch_route():
    content = (_REPO_ROOT / "docs" / "api-reference.md").read_text(encoding="utf-8")
    assert "/projects/{project_id}/sprint-batch" in content


# ---------------------------------------------------------------------------
# 6. meridian.batch_ops — request-shape validation, unit-tested directly.
# ---------------------------------------------------------------------------

def test_batch_operations_mapping():
    assert batch_ops.BATCH_OPERATIONS == {
        "sprint_items": "sprint_item",
        "item_updates": "sprint_item",
        "pointers": "sprint_item_pointer",
        "notes": "sprint_note",
    }


def test_validate_batch_request_shape_ok():
    # No exception for a fully-shaped, valid request.
    batch_ops.validate_batch_request_shape({
        "operation": "sprint_items", "mode": "all_or_nothing",
        "idempotency_key": "key-1", "entries": [{"title": "x"}],
    })
    # idempotency_key explicitly null is a valid opt-out, not a missing key.
    batch_ops.validate_batch_request_shape({
        "operation": "notes", "mode": "best_effort",
        "idempotency_key": None, "entries": [],
    })


def test_validate_batch_request_shape_missing_operation():
    with pytest.raises(batch_ops.BatchRequestError, match="operation"):
        batch_ops.validate_batch_request_shape({
            "mode": "all_or_nothing", "idempotency_key": "k",
        })


def test_validate_batch_request_shape_unknown_operation():
    with pytest.raises(batch_ops.BatchRequestError, match="operation"):
        batch_ops.validate_batch_request_shape({
            "operation": "bogus", "mode": "all_or_nothing", "idempotency_key": "k",
        })


def test_validate_batch_request_shape_missing_mode():
    with pytest.raises(batch_ops.BatchRequestError, match="mode"):
        batch_ops.validate_batch_request_shape({
            "operation": "notes", "idempotency_key": "k",
        })


def test_validate_batch_request_shape_bad_mode():
    with pytest.raises(batch_ops.BatchRequestError, match="mode"):
        batch_ops.validate_batch_request_shape({
            "operation": "notes", "mode": "sometimes", "idempotency_key": "k",
        })


def test_validate_batch_request_shape_missing_idempotency_key_entirely():
    # Key genuinely absent (not None) -- this is the "forgot to think about
    # idempotency" case the acceptance criteria requires we reject.
    with pytest.raises(batch_ops.BatchRequestError, match="idempotency_key"):
        batch_ops.validate_batch_request_shape({
            "operation": "notes", "mode": "best_effort",
        })


def test_normalize_entries_forces_action_for_sprint_items():
    normalized = batch_ops._normalize_entries_for_operation(
        "sprint_items", [{"title": "a"}, {"title": "b", "action": "create"}],
    )
    assert all(e["action"] == "create" for e in normalized)


def test_normalize_entries_forces_action_for_item_updates():
    normalized = batch_ops._normalize_entries_for_operation(
        "item_updates", [{"item_id": "x", "title": "a"}],
    )
    assert normalized[0]["action"] == "update"


def test_normalize_entries_rejects_action_mismatch():
    with pytest.raises(batch_ops.BatchRequestError, match="sprint_items"):
        batch_ops._normalize_entries_for_operation(
            "sprint_items", [{"title": "a", "action": "update"}],
        )
    with pytest.raises(batch_ops.BatchRequestError, match="item_updates"):
        batch_ops._normalize_entries_for_operation(
            "item_updates", [{"item_id": "x", "action": "create"}],
        )


def test_normalize_entries_passthrough_for_pointers_and_notes():
    entries = [{"sprint_item_id": "x"}]
    assert batch_ops._normalize_entries_for_operation("pointers", entries) is entries
    assert batch_ops._normalize_entries_for_operation("notes", entries) is entries


# ---------------------------------------------------------------------------
# 7. execute_batch_operation — DB-backed correctness per operation.
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def project(db):
    return await db_module.create_project(db, "batch-schema-test-proj")


@pytest_asyncio.fixture
async def session(db, project):
    return await db_module.register_session(db, project["id"], "batch-schema-session")


@pytest.mark.asyncio
async def test_execute_batch_operation_sprint_items(db, project):
    out = await batch_ops.execute_batch_operation(
        db, project_id=project["id"], operation="sprint_items",
        entries=[{"title": "Batch-created item A"}],
        mode="all_or_nothing", idempotency_key="op-sprint-items-1",
    )
    assert out["operation"] == "sprint_items"
    assert out["entry_kind"] == "sprint_item"
    assert out["status"] == "ok"
    assert out["created_count"] == 1


@pytest.mark.asyncio
async def test_execute_batch_operation_item_updates(db, project):
    item = await db_module.add_sprint_item(db, project["id"], "v1", "Item to patch")
    out = await batch_ops.execute_batch_operation(
        db, project_id=project["id"], operation="item_updates",
        entries=[{"item_id": item["id"], "notes": "patched via batch"}],
        mode="all_or_nothing", idempotency_key="op-item-updates-1",
    )
    assert out["operation"] == "item_updates"
    assert out["status"] == "ok"
    updated = await db_module.get_sprint_item(db, item["id"])
    assert updated["notes"] == "patched via batch"


@pytest.mark.asyncio
async def test_execute_batch_operation_pointers(db, project):
    item = await db_module.add_sprint_item(db, project["id"], "v1", "Item for pointer")
    out = await batch_ops.execute_batch_operation(
        db, project_id=project["id"], operation="pointers",
        entries=[{
            "sprint_item_id": item["id"], "source_type": "code",
            "targets": [{"uri": "file:a.py",
                         "selector": {"type": "symbol", "qualified_name": "a.b"}}],
        }],
        mode="all_or_nothing", idempotency_key="op-pointers-1",
    )
    assert out["operation"] == "pointers"
    assert out["entry_kind"] == "sprint_item_pointer"
    assert out["status"] == "ok"


@pytest.mark.asyncio
async def test_execute_batch_operation_notes(db, project, session):
    out = await batch_ops.execute_batch_operation(
        db, project_id=project["id"], operation="notes",
        entries=[{"title": "Batch note", "body": "body text"}],
        mode="all_or_nothing", idempotency_key="op-notes-1",
        session_id=session["id"],
    )
    assert out["operation"] == "notes"
    assert out["entry_kind"] == "sprint_note"
    assert out["status"] == "ok"


@pytest.mark.asyncio
async def test_execute_batch_operation_unknown_operation_raises(db, project):
    with pytest.raises(batch_ops.BatchRequestError):
        await batch_ops.execute_batch_operation(
            db, project_id=project["id"], operation="not-a-real-op",
            entries=[{}], mode="all_or_nothing", idempotency_key="k",
        )
