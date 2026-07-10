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


@pytest.mark.asyncio
async def test_mcp_delete_pointer_removes_and_is_idempotent(db):
    """98c71a42 — the delete MCP tool removes a pointer and is idempotent."""
    from meridian import server as srv
    p = await db_module.create_project(db, "ptr-del")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "item")
    added = await srv._dispatch_mcp_tool(
        "add_sprint_item_pointer",
        {"project_id": p["id"], "sprint_item_id": item["id"], "source_type": "code",
         "targets": [{"uri": "a.py", "selector": {"type": "range",
                      "start_line": 1, "end_line": 2}}]},
        db, "/tmp",
    )
    ptr_id = added["id"]

    # delete the real pointer -> deleted True, and the item has no pointers left
    deleted = await srv._dispatch_mcp_tool(
        "delete_sprint_item_pointer", {"pointer_id": ptr_id}, db, "/tmp",
    )
    assert deleted == {"pointer_id": ptr_id, "deleted": True}
    listed = await srv._dispatch_mcp_tool(
        "get_sprint_item_pointers",
        {"project_id": p["id"], "sprint_item_id": item["id"]},
        db, "/tmp",
    )
    assert listed["pointers"] == []

    # deleting again is idempotent (not an error) -> deleted False
    again = await srv._dispatch_mcp_tool(
        "delete_sprint_item_pointer", {"pointer_id": ptr_id}, db, "/tmp",
    )
    assert again == {"pointer_id": ptr_id, "deleted": False}

    # a missing pointer_id is a clean error, not a crash
    err = await srv._dispatch_mcp_tool(
        "delete_sprint_item_pointer", {}, db, "/tmp",
    )
    assert "error" in err


def test_delete_pointer_tool_registered():
    """98c71a42 — delete_sprint_item_pointer is a registered, non-read-only,
    destructive-hinted MCP tool (so it is callable and correctly annotated)."""
    from meridian.mcp_tools import _MCP_TOOLS_LIST, _READ_ONLY_TOOLS, _DESTRUCTIVE_TOOLS
    names = {t["name"] for t in _MCP_TOOLS_LIST}
    assert "delete_sprint_item_pointer" in names
    assert "delete_sprint_item_pointer" not in _READ_ONLY_TOOLS
    assert "delete_sprint_item_pointer" in _DESTRUCTIVE_TOOLS


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


# ---------------------------------------------------------------------------
# S4 — text_quote (web, 1d3f6e71) + finding_id (experiment, 1f1cd4d9)
# ---------------------------------------------------------------------------

def test_validate_text_quote_and_finding_id_variants():
    ptr = validate_pointer({
        "source_type": "web",
        "targets": [{"uri": "https://example.com/a", "selector": {
            "type": "text_quote", "exact": "the cited passage",
            "prefix": "before ", "suffix": " after",
            "archived_url": "https://web.archive.org/web/2/https://example.com/a"}}],
    })
    sel = ptr["targets"][0]["selector"]
    assert sel["type"] == "text_quote"
    assert sel["exact"] == "the cited passage"
    assert sel["prefix"] == "before " and sel["suffix"] == " after"
    assert sel["archived_url"].startswith("https://web.archive.org/")

    ptr2 = validate_pointer({
        "source_type": "experiment",
        "targets": [{"uri": "finding:xyz",
                     "selector": {"type": "finding_id", "id": "note-123"}}],
    })
    assert ptr2["targets"][0]["selector"] == {"type": "finding_id", "id": "note-123"}


@pytest.mark.parametrize("bad_sel", [
    {"type": "text_quote"},                              # missing exact
    {"type": "text_quote", "exact": "   "},              # blank exact
    {"type": "text_quote", "exact": "x", "prefix": 5},   # non-str prefix
    {"type": "finding_id"},                              # missing id
    {"type": "finding_id", "id": ""},                    # empty id
])
def test_validate_rejects_bad_web_experiment_selectors(bad_sel):
    with pytest.raises(PointerValidationError):
        validate_pointer({"source_type": "x",
                          "targets": [{"uri": "u", "selector": bad_sel}]})


@pytest.mark.asyncio
async def test_resolve_text_quote_present_drift_and_guarded():
    ptr = {"source_type": "web", "targets": [{"uri": "https://x/a", "selector": {
        "type": "text_quote", "exact": "the cited passage",
        "archived_url": "https://web.archive.org/web/2/https://x/a"}}]}

    async def present(_uri): return "... the cited passage lives here ..."
    hit = (await resolve_pointer(None, ptr, web_fetcher=present))["targets"][0]
    assert hit["resolved"] is True and hit["found"] is True and hit["drift"] is False
    assert hit["archived_url"].startswith("https://web.archive.org/")

    async def changed(_uri): return "totally different content now"
    drift = (await resolve_pointer(None, ptr, web_fetcher=changed))["targets"][0]
    assert drift["resolved"] is True and drift["found"] is False and drift["drift"] is True

    async def nothing(_uri): return None
    n = (await resolve_pointer(None, ptr, web_fetcher=nothing))["targets"][0]
    assert n["resolved"] is False

    async def boom(_uri): raise RuntimeError("network")
    g = (await resolve_pointer(None, ptr, web_fetcher=boom))["targets"][0]
    assert g["resolved"] is False  # guarded — never raises
    assert g["archived_url"].startswith("https://web.archive.org/")  # echoed even unresolved


@pytest.mark.asyncio
async def test_resolve_text_quote_prefix_suffix_disambiguation():
    ptr = {"source_type": "web", "targets": [{"uri": "https://x/a", "selector": {
        "type": "text_quote", "exact": "bank", "prefix": "river ", "suffix": " side"}}]}
    async def right_context(_uri): return "walking along the river bank side at dusk"
    async def wrong_context(_uri): return "i deposited cash at the bank downtown"
    ok = (await resolve_pointer(None, ptr, web_fetcher=right_context))["targets"][0]
    assert ok["found"] is True
    miss = (await resolve_pointer(None, ptr, web_fetcher=wrong_context))["targets"][0]
    assert miss["found"] is False and miss["drift"] is True


@pytest.mark.asyncio
async def test_resolve_finding_id_hit_miss_and_guarded():
    ptr = {"source_type": "experiment", "targets": [
        {"uri": "finding:note-1", "selector": {"type": "finding_id", "id": "note-1"}},
        {"uri": "finding:nope", "selector": {"type": "finding_id", "id": "nope"}},
    ]}

    async def finder(_id):
        return ({"id": "note-1", "title": "Finding: exp run",
                 "body": "input=X\noutput=Y\nparams={'lr':0.1}"} if _id == "note-1" else None)
    hit, miss = (await resolve_pointer(None, ptr, finding_resolver=finder))["targets"]
    assert hit["resolved"] is True and hit["artifact"]["title"].startswith("Finding:")
    assert miss["resolved"] is False

    async def boom(_id): raise RuntimeError("db down")
    g = (await resolve_pointer(None, ptr, finding_resolver=boom))["targets"][0]
    assert g["resolved"] is False  # guarded


@pytest.mark.asyncio
async def test_web_archive_save_page_now_and_fetcher():
    from meridian import web_archive

    class _Resp:
        def __init__(self, headers=None, text=None):
            self.headers = headers or {}
            self.text = text

    async def post_ok(_url):
        return _Resp(headers={"Content-Location": "/web/20260706010101/https://example.com/a"})
    res = await web_archive.save_page_now("https://example.com/a", http_post=post_ok)
    assert res["archived_url"] == "https://web.archive.org/web/20260706010101/https://example.com/a"
    assert res["archived_at"]

    async def post_no_header(_url): return _Resp(headers={})
    res2 = await web_archive.save_page_now("https://example.com/a", http_post=post_no_header)
    assert res2["archived_url"] == "https://web.archive.org/web/2/https://example.com/a"

    async def post_boom(_url): raise RuntimeError("net")
    assert "error" in await web_archive.save_page_now("https://x", http_post=post_boom)

    assert web_archive.wayback_latest_url("https://x/a") == "https://web.archive.org/web/2/https://x/a"

    async def get_ok(_uri): return _Resp(text="page body")
    assert await web_archive.default_web_fetcher("https://x", http_get=get_ok) == "page body"

    async def get_boom(_uri): raise RuntimeError("net")
    assert await web_archive.default_web_fetcher("https://x", http_get=get_boom) is None


@pytest.mark.asyncio
async def test_mcp_web_pointer_archives_at_citation_time(db, monkeypatch):
    """1d3f6e71 — creating a source_type='web' text_quote pointer archives the URL
    at citation time (Save-Page-Now, stubbed) and stores the snapshot on the target."""
    from meridian import server as srv
    from meridian import web_archive

    async def _fake_spn(url, **_kw):
        return {"archived_url": f"https://web.archive.org/web/20260706/{url}",
                "archived_at": "2026-07-06 00:00:00"}
    monkeypatch.setattr(web_archive, "save_page_now", _fake_spn)

    p = await db_module.create_project(db, "web-ptr")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "cite a web source")
    added = await srv._dispatch_mcp_tool(
        "add_sprint_item_pointer",
        {"project_id": p["id"], "sprint_item_id": item["id"], "source_type": "web",
         "targets": [{"uri": "https://example.com/paper",
                      "selector": {"type": "text_quote", "exact": "a key claim"}}]},
        db, "/tmp",
    )
    sel = added["targets"][0]["selector"]
    assert sel["archived_url"] == "https://web.archive.org/web/20260706/https://example.com/paper"
    assert sel["archived_at"] == "2026-07-06 00:00:00"


@pytest.mark.asyncio
async def test_mcp_web_pointer_archive_failure_falls_back(db, monkeypatch):
    """Archiving is best-effort: an SPN failure still creates the pointer, with the
    deterministic Wayback 'latest snapshot' URL as the archive reference."""
    from meridian import server as srv
    from meridian import web_archive

    async def _spn_fails(url, **_kw):
        return {"error": "archive request failed"}
    monkeypatch.setattr(web_archive, "save_page_now", _spn_fails)

    p = await db_module.create_project(db, "web-ptr-fallback")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "cite")
    added = await srv._dispatch_mcp_tool(
        "add_sprint_item_pointer",
        {"project_id": p["id"], "sprint_item_id": item["id"], "source_type": "web",
         "targets": [{"uri": "https://example.com/x",
                      "selector": {"type": "text_quote", "exact": "claim"}}]},
        db, "/tmp",
    )
    assert added["targets"][0]["selector"]["archived_url"] == \
        "https://web.archive.org/web/2/https://example.com/x"


@pytest.mark.asyncio
async def test_mcp_experiment_pointer_resolves_save_finding_artifact(db):
    """1f1cd4d9 — a save_finding artifact (a run log: input/output/params/timestamp)
    is addressable via a source_type='experiment' finding_id pointer and resolves
    end-to-end through the real MCP dispatch (no injected seam)."""
    from meridian import server as srv

    p = await db_module.create_project(db, "exp-ptr")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "log an experiment")
    finding = await db_module.save_finding(
        db, p["id"],
        "experiment run 7\ninput=img_042.png\noutput=mask_042.png\nparams={'thresh':0.6}",
        source_type="experiment",
    )
    note_id = finding["note"]["id"]

    await srv._dispatch_mcp_tool(
        "add_sprint_item_pointer",
        {"project_id": p["id"], "sprint_item_id": item["id"], "source_type": "experiment",
         "targets": [{"uri": f"finding:{note_id}",
                      "selector": {"type": "finding_id", "id": note_id}}],
         "label": "run 7 artifact"},
        db, "/tmp",
    )
    resolved = await srv._dispatch_mcp_tool(
        "resolve_sprint_item_pointers",
        {"project_id": p["id"], "sprint_item_id": item["id"]},
        db, "/tmp",
    )
    target = resolved["pointers"][0]["targets"][0]
    assert target["resolved"] is True
    assert target["selector_type"] == "finding_id"
    assert target["artifact"]["id"] == note_id
    assert "input=img_042.png" in target["artifact"]["body"]


# ---------------------------------------------------------------------------
# e9d72d17 — selectable (not Zotero-hardcoded) reference-manager backend
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_citation_backend_registry_select_and_fallback():
    # Zotero ships registered as the default.
    assert "zotero" in pointers_module.available_citation_backends()
    assert pointers_module.DEFAULT_CITATION_BACKEND == "zotero"

    async def _fake(ref):
        return {"zotero_key": "X", "backend": "mendeley"}

    pointers_module.register_citation_backend("mendeley", lambda: _fake)
    try:
        assert "mendeley" in pointers_module.available_citation_backends()
        # Case-insensitive selection.
        assert pointers_module.resolve_citation_backend("MENDELEY") is _fake
        # Unknown backend → default (zotero) fallback, never an error.
        assert callable(pointers_module.resolve_citation_backend("nope-not-real"))
    finally:
        pointers_module._CITATION_BACKENDS.pop("mendeley", None)


def test_register_citation_backend_rejects_empty_name():
    with pytest.raises(ValueError):
        pointers_module.register_citation_backend("  ", lambda: None)


def test_resolve_citation_backend_env_var(monkeypatch):
    async def _fake(ref):
        return None

    pointers_module.register_citation_backend("acme", lambda: _fake)
    try:
        monkeypatch.setenv("MERIDIAN_CITATION_BACKEND", "acme")
        # No explicit arg → env var selects the backend.
        assert pointers_module.resolve_citation_backend(None) is _fake
        # Explicit arg beats the env var.
        assert pointers_module.resolve_citation_backend("zotero") is not _fake
    finally:
        pointers_module._CITATION_BACKENDS.pop("acme", None)


@pytest.mark.asyncio
async def test_resolve_pointer_routes_through_selected_backend():
    """resolve_pointer sends zotero_key targets through the SELECTED backend when no
    explicit citation_resolver is injected — the product-level selection seam."""
    async def _mendeley(ref):
        return {"zotero_key": "M1", "title": "via mendeley"} if ref == "zotero:M1" else None

    pointers_module.register_citation_backend("mendeley", lambda: _mendeley)
    try:
        ptr = {
            "source_type": "citation",
            "targets": [{"uri": "z", "selector": {"type": "zotero_key", "key": "M1"}}],
        }
        out = await resolve_pointer(
            None, ptr, project_id="pid",
            symbol_resolver=_stub_symbol_resolver,
            citation_backend="mendeley",
        )
        t = out["targets"][0]
        assert t["resolved"] is True
        assert t["item"]["title"] == "via mendeley"
    finally:
        pointers_module._CITATION_BACKENDS.pop("mendeley", None)


# ---------------------------------------------------------------------------
# 06df6ab3 — text_quote extended to anchor docx paragraph text (one selector
# mechanism across code/docs/web, not a new selector type).
# ---------------------------------------------------------------------------

def _docx_bytes(paragraphs: list[str]) -> bytes:
    """A minimal real .docx ZIP with one <w:p> per paragraph string."""
    import io
    import zipfile

    body = "".join(
        f'<w:p><w:r><w:t xml:space="preserve">{text}</w:t></w:r></w:p>'
        for text in paragraphs
    )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", xml)
    return buf.getvalue()


def test_looks_like_local_docx_and_docx_paragraph_text(tmp_path):
    from meridian import web_archive

    assert web_archive._looks_like_local_docx("thesis/chapter1.docx") is True
    assert web_archive._looks_like_local_docx("https://x/a.docx") is False
    assert web_archive._looks_like_local_docx("https://x/a") is False
    assert web_archive._looks_like_local_docx("") is False
    assert web_archive._looks_like_local_docx(None) is False

    path = tmp_path / "sample.docx"
    path.write_bytes(_docx_bytes(["Intro paragraph.", "The cited passage lives here."]))
    text = web_archive._docx_paragraph_text(str(path))
    assert text == "Intro paragraph.\nThe cited passage lives here."

    # A missing/unreadable file degrades to None, never raises.
    assert web_archive._docx_paragraph_text(str(tmp_path / "missing.docx")) is None


@pytest.mark.asyncio
async def test_default_web_fetcher_routes_local_docx_to_paragraph_text(tmp_path):
    from meridian import web_archive

    path = tmp_path / "chapter1.docx"
    path.write_bytes(_docx_bytes(["Para one.", "Para two with the key claim."]))

    # No http_get involved for a local .docx path — it never touches the network.
    async def boom_if_called(_uri):
        raise AssertionError("HTTP fetch must not be used for a local .docx uri")

    text = await web_archive.default_web_fetcher(str(path), http_get=boom_if_called)
    assert text == "Para one.\nPara two with the key claim."

    # A plain http(s) URL still goes through the HTTP branch as before.
    class _Resp:
        def __init__(self, text):
            self.text = text

    async def get_ok(_uri):
        return _Resp("page body")

    assert await web_archive.default_web_fetcher("https://x/a.docx", http_get=get_ok) == "page body"


@pytest.mark.asyncio
async def test_resolve_text_quote_anchors_docx_paragraph_and_flags_drift(tmp_path):
    """End-to-end: a source_type='docs' text_quote pointer whose uri is a local
    .docx resolves via the SAME resolver as web text_quote — just fed docx
    paragraph text instead of a fetched page body — including drift detection
    when the passage no longer matches."""
    path = tmp_path / "chapter1.docx"
    path.write_bytes(_docx_bytes(["Intro.", "As shown by the key result in this section."]))

    ptr = {"source_type": "docs", "targets": [{"uri": str(path), "selector": {
        "type": "text_quote", "exact": "the key result"}}]}

    # No web_fetcher injected — resolve_pointer's default seam (web_archive's
    # default_web_fetcher) must route the local .docx path itself.
    out = await resolve_pointer(None, ptr)
    hit = out["targets"][0]
    assert hit["resolved"] is True
    assert hit["found"] is True
    assert hit["drift"] is False

    # A quote that no longer appears in the docx is a resolved drift, not an error.
    ptr2 = {"source_type": "docs", "targets": [{"uri": str(path), "selector": {
        "type": "text_quote", "exact": "a passage that was removed"}}]}
    out2 = await resolve_pointer(None, ptr2)
    miss = out2["targets"][0]
    assert miss["resolved"] is True
    assert miss["found"] is False
    assert miss["drift"] is True
