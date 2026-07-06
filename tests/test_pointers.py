"""Coverage for the GENERIC POINTER PRIMITIVE (2976e168).

Exercises, end to end and in isolation:

* the pointer MODEL + validation (valid pointers round-trip; malformed selectors
  are rejected; subSelector nesting validates recursively),
* JSON serialize/deserialize of the ``targets`` column,
* the DB helpers (add/get/delete round-trip on ``sprint_item_pointers``),
* the ONE resolver — each selector.type (range/symbol/node_id/zotero_key)
  dispatches correctly with STUBBED code-graph / doc_store / Zotero seams (no
  network, no live Zotero); unresolvable → guarded ``{resolved: False}``; range
  returns as-is; a subSelector narrows the outer resolution,
* the three MCP tools through the real ``_dispatch_mcp_tool`` path.
"""
from __future__ import annotations

import pytest

from meridian import db as db_module
from meridian import pointers as pointers_module
from meridian.pointers import (
    PointerValidationError,
    validate_pointer,
    serialize_targets,
    deserialize_targets,
    resolve_pointer,
)


# ---------------------------------------------------------------------------
# Model + validation (pure)
# ---------------------------------------------------------------------------

def test_validate_pointer_range_round_trips():
    ptr = {
        "source_type": "code",
        "targets": [
            {"uri": "meridian/server.py",
             "selector": {"type": "range", "start_line": 10, "start_char": 0,
                          "end_line": 20, "end_char": 5}},
        ],
        "label": "the lifespan",
    }
    normalized = validate_pointer(ptr)
    assert normalized["source_type"] == "code"
    assert normalized["label"] == "the lifespan"
    sel = normalized["targets"][0]["selector"]
    assert sel["type"] == "range"
    assert sel["start_line"] == 10 and sel["end_line"] == 20


def test_validate_pointer_symbol_node_zotero_variants():
    ptr = validate_pointer({
        "source_type": "code",
        "targets": [
            {"uri": "a.py", "selector": {"type": "symbol", "qualified_name": "a.b.c"}},
            {"uri": "doc:1", "selector": {"type": "node_id", "id": "el-1"}},
            {"uri": "zotero:", "selector": {"type": "zotero_key", "key": "ABCD1234"}},
        ],
    })
    kinds = [t["selector"]["type"] for t in ptr["targets"]]
    assert kinds == ["symbol", "node_id", "zotero_key"]
    assert ptr["targets"][0]["selector"]["qualified_name"] == "a.b.c"
    assert "label" not in ptr  # omitted label stays absent


def test_validate_pointer_subselector_nesting():
    ptr = validate_pointer({
        "source_type": "code",
        "targets": [{
            "uri": "a.py",
            "selector": {
                "type": "symbol", "qualified_name": "a.b.func",
                "subSelector": {"type": "range", "start_line": 3, "end_line": 4},
            },
        }],
    })
    sel = ptr["targets"][0]["selector"]
    assert sel["subSelector"]["type"] == "range"
    assert sel["subSelector"]["start_line"] == 3


def test_validate_pointer_target_level_subselector_folds_into_selector():
    """A subSelector placed as a peer of selector (W3C shape) folds in."""
    ptr = validate_pointer({
        "source_type": "code",
        "targets": [{
            "uri": "a.py",
            "selector": {"type": "symbol", "qualified_name": "a.b.func"},
            "subSelector": {"type": "range", "start_line": 1, "end_line": 2},
        }],
    })
    assert ptr["targets"][0]["selector"]["subSelector"]["type"] == "range"


@pytest.mark.parametrize("bad", [
    {"source_type": "", "targets": [{"uri": "a", "selector": {"type": "range", "start_line": 1, "end_line": 2}}]},
    {"source_type": "code", "targets": []},
    {"source_type": "code", "targets": [{"uri": "a", "selector": {"type": "bogus"}}]},
    {"source_type": "code", "targets": [{"uri": "", "selector": {"type": "range", "start_line": 1, "end_line": 2}}]},
    {"source_type": "code", "targets": [{"uri": "a", "selector": {"type": "range", "start_line": "x", "end_line": 2}}]},
    {"source_type": "code", "targets": [{"uri": "a", "selector": {"type": "symbol"}}]},
    {"source_type": "code", "targets": [{"uri": "a", "selector": {"type": "node_id", "id": ""}}]},
    {"source_type": "code", "targets": [{"uri": "a", "selector": {"type": "zotero_key"}}]},
    {"source_type": "code", "targets": [{"uri": "a", "selector": {"type": "symbol", "qualified_name": "x", "subSelector": {"type": "nope"}}}]},
    {"source_type": "code", "targets": "not-a-list"},
    "not-an-object",
])
def test_validate_pointer_rejects_malformed(bad):
    with pytest.raises(PointerValidationError):
        validate_pointer(bad)


def test_validate_pointer_range_rejects_bool_as_int():
    """A bool is not a valid line number even though bool is an int subclass."""
    with pytest.raises(PointerValidationError):
        validate_pointer({
            "source_type": "code",
            "targets": [{"uri": "a", "selector": {"type": "range", "start_line": True, "end_line": 2}}],
        })


# ---------------------------------------------------------------------------
# Serialize / deserialize the JSON targets column
# ---------------------------------------------------------------------------

def test_serialize_deserialize_targets_round_trip():
    targets = [
        {"uri": "a.py", "selector": {"type": "symbol", "qualified_name": "a.b"}},
        {"uri": "b.py", "selector": {"type": "range", "start_line": 1, "end_line": 2}},
    ]
    raw = serialize_targets(targets)
    assert isinstance(raw, str)
    assert deserialize_targets(raw) == targets


def test_deserialize_targets_tolerant_of_garbage():
    assert deserialize_targets(None) == []
    assert deserialize_targets("") == []
    assert deserialize_targets("{not json") == []
    assert deserialize_targets("{}") == []  # decoded but not a list
    already = [{"uri": "a", "selector": {"type": "range", "start_line": 1, "end_line": 1}}]
    assert deserialize_targets(already) == already


# ---------------------------------------------------------------------------
# DB helpers (add/get/delete round-trip)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_db_pointer_add_get_delete_round_trip(db):
    p = await db_module.create_project(db, "ptr-proj")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "some item")

    stored = await db_module.add_sprint_item_pointer(
        db, p["id"], item["id"], "code",
        [{"uri": "meridian/server.py",
          "selector": {"type": "symbol", "qualified_name": "meridian.server.foo"}}],
        label="the foo",
    )
    assert stored["source_type"] == "code"
    assert stored["label"] == "the foo"
    assert stored["sprint_item_id"] == item["id"]
    # targets deserialized back into a list of dicts.
    assert isinstance(stored["targets"], list)
    assert stored["targets"][0]["selector"]["qualified_name"] == "meridian.server.foo"

    got = await db_module.get_sprint_item_pointers(db, item["id"])
    assert len(got) == 1
    assert got[0]["id"] == stored["id"]
    assert got[0]["targets"] == stored["targets"]

    removed = await db_module.delete_sprint_item_pointer(db, stored["id"])
    assert removed is True
    assert await db_module.get_sprint_item_pointers(db, item["id"]) == []
    # Deleting again is a no-op returning False.
    assert await db_module.delete_sprint_item_pointer(db, stored["id"]) is False


@pytest.mark.asyncio
async def test_db_pointer_rejects_malformed_before_write(db):
    p = await db_module.create_project(db, "ptr-bad")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "item")
    with pytest.raises(ValueError):
        await db_module.add_sprint_item_pointer(
            db, p["id"], item["id"], "code",
            [{"uri": "a", "selector": {"type": "bogus"}}],
        )
    # Nothing was persisted.
    assert await db_module.get_sprint_item_pointers(db, item["id"]) == []


@pytest.mark.asyncio
async def test_db_pointer_multi_target_ordering(db):
    p = await db_module.create_project(db, "ptr-multi")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "item")
    a = await db_module.add_sprint_item_pointer(
        db, p["id"], item["id"], "code",
        [{"uri": "a", "selector": {"type": "range", "start_line": 1, "end_line": 1}}])
    b = await db_module.add_sprint_item_pointer(
        db, p["id"], item["id"], "docs",
        [{"uri": "b", "selector": {"type": "node_id", "id": "n1"}}])
    got = await db_module.get_sprint_item_pointers(db, item["id"])
    # Both pointers are returned; get_sprint_item_pointers orders by
    # (created_at, id) — a stable, deterministic order (created_at is
    # second-granularity so same-call inserts tie and fall back to id).
    assert len(got) == 2
    assert {g["id"] for g in got} == {a["id"], b["id"]}
    ids = [g["id"] for g in got]
    assert ids == sorted(ids)  # deterministic id tiebreak within a tied second


# ---------------------------------------------------------------------------
# The resolver — dispatch by selector.type (stubbed seams, never network)
# ---------------------------------------------------------------------------

async def _stub_symbol_resolver(_db, _pid, qn, _lim):
    if qn == "found.symbol":
        return [{"qualified_name": "found.symbol", "file": "found.py", "kind": "function"}]
    return []


async def _stub_node_resolver(element_id):
    if element_id == "el-1":
        return {"element": {"id": "el-1", "kind": "heading", "text": "Intro"},
                "document": {"id": "doc-1", "title": "Thesis"}}
    return None


async def _stub_citation_resolver(ref):
    if ref == "zotero:GOOD":
        return {"zotero_key": "GOOD", "doi": "10.1/x", "title": "A Paper"}
    return None


@pytest.mark.asyncio
async def test_resolve_range_returns_location_as_is():
    ptr = {
        "source_type": "code",
        "targets": [{"uri": "a.py", "selector": {
            "type": "range", "start_line": 5, "start_char": 0,
            "end_line": 9, "end_char": 4}}],
    }
    out = await resolve_pointer(None, ptr, project_id="pid",
                                symbol_resolver=_stub_symbol_resolver,
                                node_resolver=_stub_node_resolver,
                                citation_resolver=_stub_citation_resolver)
    t = out["targets"][0]
    assert t["resolved"] is True
    assert t["selector_type"] == "range"
    assert t["uri"] == "a.py"
    assert t["range"] == {"start_line": 5, "start_char": 0, "end_line": 9, "end_char": 4}


@pytest.mark.asyncio
async def test_resolve_symbol_hits_and_misses():
    ptr = {
        "source_type": "code",
        "targets": [
            {"uri": "a.py", "selector": {"type": "symbol", "qualified_name": "found.symbol"}},
            {"uri": "b.py", "selector": {"type": "symbol", "qualified_name": "missing.symbol"}},
        ],
    }
    out = await resolve_pointer(None, ptr, project_id="pid",
                                symbol_resolver=_stub_symbol_resolver,
                                citation_resolver=_stub_citation_resolver)
    hit, miss = out["targets"]
    assert hit["resolved"] is True and hit["file"] == "found.py"
    assert miss["resolved"] is False
    assert "reason" in miss


@pytest.mark.asyncio
async def test_resolve_node_id_hit_and_miss():
    ptr = {
        "source_type": "docs",
        "targets": [
            {"uri": "doc:1", "selector": {"type": "node_id", "id": "el-1"}},
            {"uri": "doc:1", "selector": {"type": "node_id", "id": "nope"}},
        ],
    }
    out = await resolve_pointer(None, ptr, project_id="pid",
                                symbol_resolver=_stub_symbol_resolver,
                                node_resolver=_stub_node_resolver,
                                citation_resolver=_stub_citation_resolver)
    hit, miss = out["targets"]
    assert hit["resolved"] is True
    assert hit["element"]["text"] == "Intro"
    assert hit["document"]["title"] == "Thesis"
    assert miss["resolved"] is False


@pytest.mark.asyncio
async def test_resolve_node_id_no_store_is_unresolved():
    ptr = {"source_type": "docs",
           "targets": [{"uri": "d", "selector": {"type": "node_id", "id": "el-1"}}]}
    # node_resolver defaults to None → cannot resolve, but never raises.
    out = await resolve_pointer(None, ptr, project_id="pid",
                                symbol_resolver=_stub_symbol_resolver,
                                citation_resolver=_stub_citation_resolver)
    assert out["targets"][0]["resolved"] is False
    assert "no doc_store" in out["targets"][0]["reason"]


@pytest.mark.asyncio
async def test_resolve_zotero_key_hit_and_miss():
    ptr = {
        "source_type": "citation",
        "targets": [
            {"uri": "z", "selector": {"type": "zotero_key", "key": "GOOD"}},
            {"uri": "z", "selector": {"type": "zotero_key", "key": "BAD"}},
        ],
    }
    out = await resolve_pointer(None, ptr, project_id="pid",
                                symbol_resolver=_stub_symbol_resolver,
                                citation_resolver=_stub_citation_resolver)
    hit, miss = out["targets"]
    assert hit["resolved"] is True and hit["item"]["zotero_key"] == "GOOD"
    assert miss["resolved"] is False


@pytest.mark.asyncio
async def test_resolve_subselector_narrows_outer():
    """symbol + range subSelector = 'these lines, within this function'."""
    ptr = {
        "source_type": "code",
        "targets": [{
            "uri": "a.py",
            "selector": {
                "type": "symbol", "qualified_name": "found.symbol",
                "subSelector": {"type": "range", "start_line": 12, "end_line": 15},
            },
        }],
    }
    out = await resolve_pointer(None, ptr, project_id="pid",
                                symbol_resolver=_stub_symbol_resolver,
                                citation_resolver=_stub_citation_resolver)
    t = out["targets"][0]
    assert t["resolved"] is True  # outer symbol resolved
    assert t["subResolved"]["resolved"] is True
    assert t["narrowed_range"] == {"start_line": 12, "end_line": 15}


@pytest.mark.asyncio
async def test_resolve_never_raises_on_malformed_target():
    ptr = {"source_type": "code", "targets": ["not-a-dict",
           {"uri": "a", "selector": "not-a-dict"}]}
    out = await resolve_pointer(None, ptr, project_id="pid",
                                symbol_resolver=_stub_symbol_resolver,
                                citation_resolver=_stub_citation_resolver)
    assert all(t["resolved"] is False for t in out["targets"])


@pytest.mark.asyncio
async def test_resolve_symbol_resolver_exception_is_guarded():
    async def _boom(*_a, **_k):
        raise RuntimeError("graph exploded")
    ptr = {"source_type": "code",
           "targets": [{"uri": "a", "selector": {"type": "symbol", "qualified_name": "x"}}]}
    out = await resolve_pointer(None, ptr, project_id="pid",
                                symbol_resolver=_boom,
                                citation_resolver=_stub_citation_resolver)
    assert out["targets"][0]["resolved"] is False


# ---------------------------------------------------------------------------
# The three MCP tools via the real dispatch path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mcp_pointer_tools_add_get_resolve(db, monkeypatch):
    from meridian import server as srv

    p = await db_module.create_project(db, "ptr-mcp")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "wire the primitive")

    # Stub the real code-graph search so the symbol resolves deterministically
    # without indexing a repo.
    async def _fake_search(_db, _pid, query, limit=10):
        return [{"qualified_name": "meridian.server.mcp_tools_doc",
                 "file": "meridian/server.py", "kind": "function"}]
    monkeypatch.setattr(db_module, "search_graph_entities", _fake_search)

    # add
    added = await srv._dispatch_mcp_tool(
        "add_sprint_item_pointer",
        {"project_id": p["id"], "sprint_item_id": item["id"], "source_type": "code",
         "targets": [{"uri": "meridian/server.py",
                      "selector": {"type": "symbol",
                                   "qualified_name": "meridian.server.mcp_tools_doc"}}],
         "label": "doc generator"},
        db, "/tmp",
    )
    assert added["label"] == "doc generator"
    ptr_id = added["id"]

    # get
    listed = await srv._dispatch_mcp_tool(
        "get_sprint_item_pointers",
        {"project_id": p["id"], "sprint_item_id": item["id"]},
        db, "/tmp",
    )
    assert listed["sprint_item_id"] == item["id"]
    assert len(listed["pointers"]) == 1
    assert listed["pointers"][0]["id"] == ptr_id

    # resolve
    resolved = await srv._dispatch_mcp_tool(
        "resolve_sprint_item_pointers",
        {"project_id": p["id"], "sprint_item_id": item["id"]},
        db, "/tmp",
    )
    assert len(resolved["pointers"]) == 1
    target = resolved["pointers"][0]["targets"][0]
    assert target["resolved"] is True
    assert target["file"] == "meridian/server.py"


@pytest.mark.asyncio
async def test_mcp_add_pointer_malformed_returns_error(db):
    from meridian import server as srv
    p = await db_module.create_project(db, "ptr-mcp-bad")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "item")
    result = await srv._dispatch_mcp_tool(
        "add_sprint_item_pointer",
        {"project_id": p["id"], "sprint_item_id": item["id"], "source_type": "code",
         "targets": [{"uri": "a", "selector": {"type": "bogus"}}]},
        db, "/tmp",
    )
    assert "error" in result


@pytest.mark.asyncio
async def test_mcp_pointer_tools_by_project_name(db):
    """Project-scoped pointer tools resolve via project_name (no project_id)."""
    from meridian import server as srv
    p = await db_module.create_project(db, "ptr-by-name")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "item")
    added = await srv._dispatch_mcp_tool(
        "add_sprint_item_pointer",
        {"project_name": "ptr-by-name", "sprint_item_id": item["id"],
         "source_type": "code",
         "targets": [{"uri": "a.py", "selector": {"type": "range",
                      "start_line": 1, "end_line": 2}}]},
        db, "/tmp",
    )
    assert added["project_id"] == p["id"]


# ---------------------------------------------------------------------------
# MCP tool-list + schema membership
# ---------------------------------------------------------------------------

def test_pointer_tools_in_mcp_tools_list():
    from meridian.mcp_tools import _MCP_TOOLS_LIST
    names = {t["name"] for t in _MCP_TOOLS_LIST}
    assert {"add_sprint_item_pointer", "get_sprint_item_pointers",
            "resolve_sprint_item_pointers"} <= names


def test_pointer_tools_do_not_require_project_id():
    from meridian.mcp_tools import _MCP_TOOLS_LIST
    by_name = {t["name"]: t for t in _MCP_TOOLS_LIST}
    for name in ("add_sprint_item_pointer", "get_sprint_item_pointers",
                 "resolve_sprint_item_pointers"):
        schema = by_name[name]["inputSchema"]
        assert "project_name" in schema["properties"], name
        assert "project_id" not in (schema.get("required") or []), name
