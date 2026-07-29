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
# 300a063d — target_kind: existing | planned_new
# ---------------------------------------------------------------------------

def test_target_kind_omitted_defaults_to_existing_unchecked():
    """Backward compat: a target with no target_kind key at all normalizes to
    'existing' in the returned shape but is NEVER filesystem-checked — a fake
    placeholder path like 'a.py' (the shape every pre-300a063d pointer/test
    uses) must keep validating exactly as before."""
    ptr = validate_pointer({
        "source_type": "code",
        "targets": [{"uri": "a.py", "selector": {"type": "range",
                     "start_line": 1, "end_line": 2}}],
    })
    assert ptr["targets"][0]["target_kind"] == "existing"


def test_target_kind_existing_real_path_passes(tmp_path):
    """target_kind='existing' explicitly declared on a REAL path validates fine,
    and the checker actually ran (proven by the missing-path counterpart below)."""
    real_file = tmp_path / "real_module.py"
    real_file.write_text("# real file\n")
    ptr = validate_pointer({
        "source_type": "code",
        "targets": [{"uri": str(real_file), "target_kind": "existing",
                     "selector": {"type": "range", "start_line": 1, "end_line": 1}}],
    })
    assert ptr["targets"][0]["target_kind"] == "existing"
    assert ptr["targets"][0]["uri"] == str(real_file)


def test_target_kind_existing_missing_path_rejected(tmp_path):
    """target_kind='existing' explicitly declared on a path that does NOT exist
    is rejected — this is the core gap 300a063d closes: a planned-new-file item
    can no longer masquerade as verified, existing-code prospecting."""
    missing = tmp_path / "does_not_exist.py"
    with pytest.raises(PointerValidationError, match="target_kind='existing'"):
        validate_pointer({
            "source_type": "code",
            "targets": [{"uri": str(missing), "target_kind": "existing",
                         "selector": {"type": "range", "start_line": 1, "end_line": 1}}],
        })


def test_target_kind_planned_new_missing_path_allowed(tmp_path):
    """target_kind='planned_new' explicitly allows a nonexistent path — the file
    hasn't been created yet, and that's the whole point of declaring it planned."""
    missing = tmp_path / "not_created_yet.py"
    ptr = validate_pointer({
        "source_type": "code",
        "targets": [{"uri": str(missing), "target_kind": "planned_new",
                     "selector": {"type": "range", "start_line": 1, "end_line": 1}}],
    })
    assert ptr["targets"][0]["target_kind"] == "planned_new"
    assert ptr["targets"][0]["uri"] == str(missing)


def test_target_kind_invalid_value_rejected():
    with pytest.raises(PointerValidationError, match="target_kind"):
        validate_pointer({
            "source_type": "code",
            "targets": [{"uri": "a.py", "target_kind": "bogus",
                         "selector": {"type": "range", "start_line": 1, "end_line": 1}}],
        })


def test_target_kind_existing_skips_check_for_non_local_uri_schemes():
    """target_kind='existing' on a non-local-path uri (zotero:/doc:/finding:/a URL)
    is NOT filesystem-checked — those schemes have their own existence semantics,
    resolved elsewhere (resolve_pointer), not a local disk check."""
    for uri, selector in [
        ("zotero:", {"type": "zotero_key", "key": "ABCD1234"}),
        ("doc:1", {"type": "node_id", "id": "el-1"}),
        ("finding:xyz", {"type": "finding_id", "id": "note-1"}),
        ("https://example.com/a", {"type": "text_quote", "exact": "x"}),
    ]:
        ptr = validate_pointer({
            "source_type": "x",
            "targets": [{"uri": uri, "target_kind": "existing", "selector": selector}],
        })
        assert ptr["targets"][0]["target_kind"] == "existing"


def test_target_kind_existing_uses_injectable_path_exists_checker():
    """path_exists is an injectable seam (same pattern as symbol_resolver /
    node_resolver / citation_resolver): tests can stub it instead of touching
    the real filesystem."""
    ptr = {
        "source_type": "code",
        "targets": [{"uri": "some/fake/path.py", "target_kind": "existing",
                     "selector": {"type": "range", "start_line": 1, "end_line": 1}}],
    }
    ok = validate_pointer(ptr, path_exists=lambda _uri: True)
    assert ok["targets"][0]["target_kind"] == "existing"
    with pytest.raises(PointerValidationError):
        validate_pointer(ptr, path_exists=lambda _uri: False)


@pytest.mark.asyncio
async def test_db_pointer_target_kind_existing_missing_path_rejected(db, tmp_path):
    """DB layer: add_sprint_item_pointer rejects an explicit target_kind='existing'
    pointer at a nonexistent path BEFORE any write (mirrors
    test_db_pointer_rejects_malformed_before_write's convention)."""
    p = await db_module.create_project(db, "ptr-kind-bad")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "item")
    missing = tmp_path / "ghost.py"
    with pytest.raises(ValueError):
        await db_module.add_sprint_item_pointer(
            db, p["id"], item["id"], "code",
            [{"uri": str(missing), "target_kind": "existing",
              "selector": {"type": "range", "start_line": 1, "end_line": 1}}],
        )
    assert await db_module.get_sprint_item_pointers(db, item["id"]) == []


@pytest.mark.asyncio
async def test_db_pointer_target_kind_planned_new_missing_path_allowed(db, tmp_path):
    """DB layer: a target_kind='planned_new' pointer at a nonexistent path IS
    persisted — a planned-new-file item is real prospecting evidence too, just
    distinguishable from verified existing-code evidence."""
    p = await db_module.create_project(db, "ptr-kind-planned")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "item")
    missing = tmp_path / "new_module.py"
    stored = await db_module.add_sprint_item_pointer(
        db, p["id"], item["id"], "code",
        [{"uri": str(missing), "target_kind": "planned_new",
          "selector": {"type": "range", "start_line": 1, "end_line": 1}}],
    )
    assert stored["targets"][0]["target_kind"] == "planned_new"
    got = await db_module.get_sprint_item_pointers(db, item["id"])
    assert got[0]["targets"][0]["target_kind"] == "planned_new"


@pytest.mark.asyncio
async def test_db_pointer_target_kind_existing_real_path_allowed(db, tmp_path):
    """DB layer: a target_kind='existing' pointer at a REAL path is persisted."""
    p = await db_module.create_project(db, "ptr-kind-real")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "item")
    real_file = tmp_path / "present.py"
    real_file.write_text("# present\n")
    stored = await db_module.add_sprint_item_pointer(
        db, p["id"], item["id"], "code",
        [{"uri": str(real_file), "target_kind": "existing",
          "selector": {"type": "range", "start_line": 1, "end_line": 1}}],
    )
    assert stored["targets"][0]["target_kind"] == "existing"


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
async def test_mcp_resolve_pointers_reaches_live_graph_via_tenant(db, monkeypatch):
    """653579c5 regression — resolve_sprint_item_pointers must resolve a symbol
    target via the SAME live tunnel-connected graph prospect_symbol reaches,
    not just the (production-empty) codebase_graph_entities snapshot.

    Before the fix, ``tenant`` was accepted by the handler but never threaded
    into symbol resolution at all, so this scenario (empty snapshot, live
    graph has the answer) always returned {resolved: False} even with an
    active code tunnel for this exact tenant/project.
    """
    import meridian.routes.tunnel as _tunnel_mod
    from meridian import server as srv

    p = await db_module.create_project(db, "ptr-live-graph")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "wire the primitive")

    # The local cached snapshot has NOTHING for this project (matches
    # production reality: nothing ever populates codebase_graph_entities).
    async def _empty_snapshot(_db, _pid, query, limit=10):
        return []
    monkeypatch.setattr(db_module, "search_graph_entities", _empty_snapshot)

    # But a live code tunnel for this tenant resolves the symbol instantly.
    async def _fake_call_tunnel(tid, name, args, **kw):
        if name == "codebase__search_graph":
            return {"content": [{"type": "text", "text":
                '{"results": [{"qualified_name": "meridian.server.mcp_tools_doc", '
                '"file": "meridian/server.py"}]}'}]}
        raise AssertionError(f"unexpected tunnel tool: {name}")
    monkeypatch.setattr(_tunnel_mod, "call_tunnel_tool", _fake_call_tunnel)
    monkeypatch.setattr(_tunnel_mod, "has_active_tunnel", lambda tid: True)

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

    fake_tenant = {"id": "tenant-live-graph"}
    resolved = await srv._dispatch_mcp_tool(
        "resolve_sprint_item_pointers",
        {"project_id": p["id"], "sprint_item_id": item["id"]},
        db, "/tmp",
        tenant=fake_tenant,
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


# ---------------------------------------------------------------------------
# 3196ba0e — fail-closed artifact readiness verification (b730 follow-up)
#
# verify_target_readiness / verify_pointer_readiness answer the COMPLETION-
# time question "is this target genuinely ready?" — distinct from
# validate_pointer's opt-in, WRITE-time target_kind='existing' check above.
# meridian-outputs (figure_resolver / provenance_getter) is a separate
# package not importable from core, so these tests stub those seams exactly
# like _stub_symbol_resolver / _stub_node_resolver / _stub_citation_resolver
# do for resolve_pointer's own seams.
# ---------------------------------------------------------------------------

from meridian.pointers import verify_target_readiness, verify_pointer_readiness


@pytest.mark.asyncio
async def test_readiness_missing_uri_reported_explicitly():
    out = await verify_target_readiness({"target_kind": "existing"})
    assert out["ready"] is False
    assert out["status"] == "missing_uri"


@pytest.mark.asyncio
async def test_readiness_non_local_uri_skipped_not_faked_ready():
    """A zotero:/doc:/finding:/URL uri is out of scope for a filesystem
    readiness check — reported ready (skipped), never silently checked."""
    out = await verify_target_readiness({"uri": "zotero:ABCD1234", "target_kind": "existing"})
    assert out["ready"] is True
    assert out["status"] == "skipped"


@pytest.mark.asyncio
async def test_readiness_target_kind_omitted_defaults_to_existing(tmp_path):
    missing = tmp_path / "nope.py"
    out = await verify_target_readiness({"uri": str(missing)})
    assert out["target_kind"] == "existing"
    assert out["ready"] is False
    assert out["status"] == "missing"


@pytest.mark.asyncio
async def test_readiness_injectable_path_and_dir_checkers():
    """path_exists / is_dir are injectable seams, same pattern validate_pointer
    already uses for path_exists — tests never need to touch a real filesystem."""
    calls = []

    def _exists(uri):
        calls.append(("exists", uri))
        return True

    def _isdir(uri):
        calls.append(("isdir", uri))
        return False

    out = await verify_target_readiness(
        {"uri": "fake/path.csv", "target_kind": "existing"},
        path_exists=_exists, is_dir=_isdir,
    )
    assert out["ready"] is True
    assert ("exists", "fake/path.csv") in calls
    assert ("isdir", "fake/path.csv") in calls


# -- existing: file present / missing / is-a-directory -----------------------


@pytest.mark.asyncio
async def test_readiness_existing_file_present_no_resolver(tmp_path):
    """existing + file present + no figure_resolver -> ready, but explicitly
    'unresolved' (meridian-outputs unavailable) — never faked as canonical."""
    real = tmp_path / "results.csv"
    real.write_text("a,b\n1,2\n")
    out = await verify_target_readiness({"uri": str(real), "target_kind": "existing"})
    assert out["ready"] is True
    assert out["status"] == "unresolved"


@pytest.mark.asyncio
async def test_readiness_existing_missing_file(tmp_path):
    missing = tmp_path / "nope.csv"
    out = await verify_target_readiness({"uri": str(missing), "target_kind": "existing"})
    assert out["ready"] is False
    assert out["status"] == "missing"


@pytest.mark.asyncio
async def test_readiness_existing_path_is_a_directory(tmp_path):
    out = await verify_target_readiness({"uri": str(tmp_path), "target_kind": "existing"})
    assert out["ready"] is False
    assert out["status"] == "is_directory"


@pytest.mark.asyncio
async def test_readiness_planned_new_path_is_a_directory_before_provenance(tmp_path):
    """A planned_new target naming an existing DIRECTORY is rejected before
    provenance is even consulted."""
    async def _prov(_outputs_dir, _path):
        raise AssertionError("provenance_getter must not be called for a directory")

    out = await verify_target_readiness(
        {"uri": str(tmp_path), "target_kind": "planned_new"}, provenance_getter=_prov,
    )
    assert out["ready"] is False
    assert out["status"] == "is_directory"


# -- planned_new: creation + provenance registration --------------------------


@pytest.mark.asyncio
async def test_readiness_planned_new_not_created_yet(tmp_path):
    """Naming a future path is never enough on its own (the sprint spec's
    core requirement for this item)."""
    future = tmp_path / "not_written_yet.png"
    out = await verify_target_readiness({"uri": str(future), "target_kind": "planned_new"})
    assert out["ready"] is False
    assert out["status"] == "not_created"


@pytest.mark.asyncio
async def test_readiness_planned_new_before_record_provenance(tmp_path):
    """File was created, but record_provenance was never called for it — an
    in-memory ledger stub mirrors extensions/meridian-outputs' annotate.py
    record_provenance/get_provenance contract (path -> record dict | None)."""
    made = tmp_path / "figure_1.png"
    made.write_bytes(b"\x89PNG\r\n")
    ledger: dict = {}

    async def _get_provenance(_outputs_dir, path):
        return ledger.get(path)

    out = await verify_target_readiness(
        {"uri": str(made), "target_kind": "planned_new"}, provenance_getter=_get_provenance,
    )
    assert out["ready"] is False
    assert out["status"] == "provenance_missing"


@pytest.mark.asyncio
async def test_readiness_planned_new_after_record_provenance(tmp_path):
    """Once a provenance record exists for the same path, the SAME target
    flips to ready — mirroring record_provenance's real upsert-then-
    get_provenance round trip."""
    made = tmp_path / "figure_1.png"
    made.write_bytes(b"\x89PNG\r\n")
    ledger: dict = {}

    async def _get_provenance(_outputs_dir, path):
        return ledger.get(path)

    before = await verify_target_readiness(
        {"uri": str(made), "target_kind": "planned_new"}, provenance_getter=_get_provenance,
    )
    assert before["ready"] is False

    # Simulate record_provenance(outputs_dir, made, ...) having been called.
    ledger[str(made)] = {
        "path": str(made), "generating_script": "plot_results.py", "recorded_at": 1234.0,
    }

    after = await verify_target_readiness(
        {"uri": str(made), "target_kind": "planned_new"}, provenance_getter=_get_provenance,
    )
    assert after["ready"] is True
    assert after["status"] == "ready"
    assert after["provenance"]["generating_script"] == "plot_results.py"


@pytest.mark.asyncio
async def test_readiness_planned_new_provenance_getter_unavailable(tmp_path):
    """No provenance_getter wired at all (meridian-outputs unavailable) must
    degrade explicitly to ready=False — never silently pass."""
    made = tmp_path / "table_2.csv"
    made.write_text("x,y\n1,2\n")
    out = await verify_target_readiness({"uri": str(made), "target_kind": "planned_new"})
    assert out["ready"] is False
    assert out["status"] == "provenance_unavailable"


@pytest.mark.asyncio
async def test_readiness_planned_new_provenance_getter_raises_degrades(tmp_path):
    """A provenance_getter that raises (tool present but unreachable) must
    never be silently converted into success."""
    made = tmp_path / "table_3.csv"
    made.write_text("x,y\n1,2\n")

    async def _boom(_outputs_dir, _path):
        raise RuntimeError("meridian-outputs tunnel down")

    out = await verify_target_readiness(
        {"uri": str(made), "target_kind": "planned_new"}, provenance_getter=_boom,
    )
    assert out["ready"] is False
    assert out["status"] == "provenance_check_failed"
    assert "tunnel down" in out["reason"]


# -- existing: canonical vs archival vs ambiguous resolution ------------------


@pytest.mark.asyncio
async def test_readiness_existing_canonical_vs_archival_resolution(tmp_path):
    """canonical (non-archival) vs archival/stale classification is recorded,
    but BOTH stay ready=True — archival is deprioritized evidence, not a
    second gate (mirrors OutputsFtsIndex.search's own never-hard-exclude
    policy for archival rows)."""
    canon = tmp_path / "run.csv"
    canon.write_text("a,b\n1,2\n")
    stale = tmp_path / "run_old.csv"
    stale.write_text("a,b\n1,2\n")

    async def _resolver_canonical(_outputs_dir, file_path):
        return {"path": file_path, "is_archival": False, "canonical_path": None}

    async def _resolver_archival(_outputs_dir, file_path):
        return {"path": file_path, "is_archival": True, "canonical_path": str(canon)}

    canon_out = await verify_target_readiness(
        {"uri": str(canon), "target_kind": "existing"}, figure_resolver=_resolver_canonical,
    )
    assert canon_out["ready"] is True
    assert canon_out["status"] == "canonical"

    stale_out = await verify_target_readiness(
        {"uri": str(stale), "target_kind": "existing"}, figure_resolver=_resolver_archival,
    )
    assert stale_out["ready"] is True
    assert stale_out["status"] == "archival"
    assert stale_out["resolved"]["canonical_path"] == str(canon)


@pytest.mark.asyncio
async def test_readiness_existing_ambiguous_basename_resolution(tmp_path):
    """Multiple same-basename candidates (the meridian-outputs extension's
    relocation-tolerant basename-fallback tier) are surfaced as ambiguous,
    not silently collapsed to canonical."""
    figure = tmp_path / "plot.png"
    figure.write_bytes(b"\x89PNG\r\n")

    async def _resolver_ambiguous(_outputs_dir, file_path):
        return {"path": file_path, "is_archival": False,
                "match_type": "basename", "candidate_count": 3}

    out = await verify_target_readiness(
        {"uri": str(figure), "target_kind": "existing"}, figure_resolver=_resolver_ambiguous,
    )
    assert out["ready"] is True
    assert out["status"] == "ambiguous"
    assert out["resolved"]["candidate_count"] == 3


@pytest.mark.asyncio
async def test_readiness_existing_meridian_outputs_unavailable_no_resolver(tmp_path):
    """No figure_resolver at all — the tool genuinely unavailable. File
    presence still satisfies readiness, but status must say 'unresolved',
    never 'canonical' (never fake success for an unreachable check)."""
    real = tmp_path / "output.npy"
    real.write_bytes(b"\x93NUMPY")
    out = await verify_target_readiness({"uri": str(real), "target_kind": "existing"})
    assert out["ready"] is True
    assert out["status"] == "unresolved"
    assert "unavailable" in out["reason"]


@pytest.mark.asyncio
async def test_readiness_existing_meridian_outputs_resolver_raises_degrades(tmp_path):
    """A figure_resolver that raises (tool present but unreachable) degrades
    explicitly rather than silently reporting canonical."""
    real = tmp_path / "output.json"
    real.write_text("{}")

    async def _boom(_outputs_dir, _path):
        raise RuntimeError("outputs tunnel timeout")

    out = await verify_target_readiness(
        {"uri": str(real), "target_kind": "existing"}, figure_resolver=_boom,
    )
    assert out["ready"] is True
    assert out["status"] == "degraded"
    assert "timeout" in out["reason"]


@pytest.mark.asyncio
async def test_readiness_default_figure_resolver_wraps_outputs_indexer(tmp_path):
    """The core-local default figure_resolver (used when a caller wants real
    resolution without injecting a stub) really does reuse
    outputs_indexer.resolve_figure_output rather than duplicating resolution
    policy — proven end-to-end against a real (tiny) outputs tree."""
    outputs_dir = tmp_path / "outputs"
    outputs_dir.mkdir()
    csv_path = outputs_dir / "metrics.csv"
    csv_path.write_text("epoch,loss\n1,0.5\n")

    resolver = pointers_module._default_figure_resolver()
    out = await verify_target_readiness(
        {"uri": str(csv_path), "target_kind": "existing"},
        outputs_dir=str(outputs_dir), figure_resolver=resolver,
    )
    assert out["ready"] is True
    assert out["status"] == "canonical"
    assert out["resolved"]["path"] == str(csv_path)


# -- pointer-level wrapper -----------------------------------------------------


@pytest.mark.asyncio
async def test_readiness_pointer_level_requires_every_target_ready(tmp_path):
    good = tmp_path / "good.csv"
    good.write_text("a\n1\n")
    missing = tmp_path / "missing.csv"

    ptr = {
        "source_type": "experiment",
        "label": "run artifacts",
        "targets": [
            {"uri": str(good), "target_kind": "existing"},
            {"uri": str(missing), "target_kind": "existing"},
        ],
    }
    out = await verify_pointer_readiness(ptr)
    assert out["ready"] is False
    assert out["label"] == "run artifacts"
    assert out["targets"][0]["ready"] is True
    assert out["targets"][1]["ready"] is False
    assert out["targets"][1]["status"] == "missing"


@pytest.mark.asyncio
async def test_readiness_pointer_level_all_ready_when_every_target_passes(tmp_path):
    a = tmp_path / "a.csv"
    a.write_text("x\n")
    b = tmp_path / "b.png"
    b.write_bytes(b"\x89PNG")
    ptr = {"source_type": "experiment", "targets": [
        {"uri": str(a), "target_kind": "existing"},
        {"uri": str(b), "target_kind": "existing"},
    ]}
    out = await verify_pointer_readiness(ptr)
    assert out["ready"] is True
    assert all(t["ready"] for t in out["targets"])


@pytest.mark.asyncio
async def test_readiness_pointer_level_empty_targets_never_vacuously_ready():
    out = await verify_pointer_readiness({"source_type": "experiment", "targets": []})
    assert out["ready"] is False
    assert out["targets"] == []


@pytest.mark.asyncio
async def test_readiness_pointer_level_malformed_target_never_raises():
    out = await verify_pointer_readiness({"source_type": "x", "targets": ["not-a-dict"]})
    assert out["ready"] is False
    assert out["targets"][0]["status"] == "malformed_target"


# ---------------------------------------------------------------------------
# 88f82c15 (b730 follow-up) — evaluate_artifact_pointer_policy: the warn/
# strict POLICY evaluator that runs at handoff-ANNOTATION time, distinct
# from (and built on top of) verify_target_readiness/verify_pointer_readiness
# above (a completion-time, per-target, I/O-backed check) and the 5fd9d2fd
# classifier (meridian.artifact_classification.classify_artifact_work, which
# this evaluator reuses rather than duplicating).
#
# Covers: off/warn/strict mode behavior, every insufficiency reason code
# (bare docx / directory / generic tool reference / unsupported type /
# missing entirely), the "cannot self-declare out of the check" invariant,
# and the false-positive exception (a genuinely document_only/caption_only
# item with a bare/insufficient pointer must NEVER warn).
# ---------------------------------------------------------------------------

import json as _json

from meridian.pointers import evaluate_artifact_pointer_policy


_STRICT_POLICY = _json.dumps({"artifact_pointer_check": "strict"})
_WARN_POLICY = _json.dumps({"artifact_pointer_check": "warn"})
_OFF_POLICY = _json.dumps({"artifact_pointer_check": "off"})


def _figure_item(**overrides):
    item = {"id": "art-1", "title": "Insert a new ablation chart figure into the results"}
    item.update(overrides)
    return item


# --- required result shape -------------------------------------------------

def test_evaluate_artifact_pointer_policy_always_returns_required_fields():
    """Each result must include: item id, classification, policy, warning
    code, required remediation, and affected pointer ids."""
    result = evaluate_artifact_pointer_policy(_figure_item())
    for key in (
        "item_id", "classification", "policy",
        "warning_code", "required_remediation", "affected_pointer_ids",
    ):
        assert key in result
    assert result["item_id"] == "art-1"
    assert isinstance(result["classification"], dict)
    assert isinstance(result["policy"], dict)


def test_evaluate_artifact_pointer_policy_never_raises_on_malformed_item():
    result = evaluate_artifact_pointer_policy(None)  # type: ignore[arg-type]
    assert result["warning_code"] is None
    assert result["ready"] is True
    result2 = evaluate_artifact_pointer_policy({})
    assert result2["warning_code"] is None


# --- default policy is warn -------------------------------------------------

def test_default_artifact_pointer_check_is_warn_when_undeclared():
    result = evaluate_artifact_pointer_policy(_figure_item())
    assert result["policy"]["artifact_pointer_check"] == "warn"
    assert result["warning_code"] == "missing_pointer"
    assert result["ready"] is True  # warn mode never blocks


# --- not artifact-sensitive: never warns, regardless of policy -------------

def test_not_sensitive_item_never_warns_even_under_strict():
    item = _figure_item(
        title="Renumber figure captions after Figure 4 was deleted",
        artifact_policy=_STRICT_POLICY,
    )
    result = evaluate_artifact_pointer_policy(item)
    assert result["classification"]["classification"] == "caption_only"
    assert result["classification"]["is_artifact_sensitive"] is False
    assert result["warning_code"] is None
    assert result["ready"] is True


def test_false_positive_document_only_declared_kind_with_bare_pointer_never_warns():
    """A genuinely document_only item (declared kind wins, per 5fd9d2fd) with
    a bare .docx pointer must NOT warn, even under strict policy."""
    item = _figure_item(
        title="Insert a new ablation chart figure",  # figure-sounding title
        artifact_kind="document_only",  # explicit override — genuinely document_only
        touches_resources=_json.dumps(["file:outputs/report.docx"]),
        artifact_policy=_STRICT_POLICY,
    )
    result = evaluate_artifact_pointer_policy(item)
    assert result["classification"]["classification"] == "document_only"
    assert result["classification"]["rule"] == "declared_artifact_kind"
    assert result["warning_code"] is None
    assert result["ready"] is True


def test_false_positive_fallback_caption_only_with_bare_pointer_never_warns():
    item = _figure_item(
        title="Renumber figure captions",
        touches_resources=_json.dumps(["file:outputs/report.docx"]),
        artifact_policy=_STRICT_POLICY,
    )
    result = evaluate_artifact_pointer_policy(item)
    assert result["classification"]["classification"] == "caption_only"
    assert result["warning_code"] is None


# --- a figure/table item cannot self-declare its way out --------------------

def test_allow_document_only_override_does_not_bypass_a_sensitive_verdict():
    """policy.allow_document_only_override is NOT consulted to flip a
    genuinely sensitive (figure/table) classification to safe — only the
    classifier's own verdict (declared kind, or fallback evidence) can do
    that. A figure/table item cannot self-declare its way out of the check."""
    item = _figure_item(
        artifact_policy=_json.dumps({
            "artifact_pointer_check": "strict",
            "allow_document_only_override": True,
        }),
    )
    result = evaluate_artifact_pointer_policy(item)
    assert result["classification"]["is_artifact_sensitive"] is True
    assert result["warning_code"] == "missing_pointer"
    assert result["ready"] is False


# --- insufficiency reason codes ---------------------------------------------

def test_insufficient_bare_docx_pointer_warns_with_specific_code():
    item = _figure_item(
        touches_resources=_json.dumps(["file:outputs/report.docx"]),
        artifact_policy=_WARN_POLICY,
    )
    result = evaluate_artifact_pointer_policy(item)
    assert result["warning_code"] == "insufficient_pointer_bare_docx"
    assert result["required_remediation"]
    assert "docx" in result["required_remediation"].lower()
    assert result["ready"] is True  # warn mode


def test_insufficient_directory_pointer_warns_with_specific_code():
    item = _figure_item(
        touches_resources=_json.dumps(["file:outputs/figures/"]),
        artifact_policy=_WARN_POLICY,
    )
    result = evaluate_artifact_pointer_policy(item)
    assert result["warning_code"] == "insufficient_pointer_directory"


def test_insufficient_generic_tool_reference_warns_and_names_pointer_id():
    item = _figure_item(
        pointer_records=[{
            "id": "ptr-abc123",
            "source_type": "code",
            "targets": [{"uri": "mcp_tool:search_outputs"}],
        }],
        artifact_policy=_WARN_POLICY,
    )
    result = evaluate_artifact_pointer_policy(item)
    assert result["warning_code"] == "insufficient_pointer_generic_reference"
    assert result["affected_pointer_ids"] == ["ptr-abc123"]


def test_insufficient_unsupported_extension_warns_with_specific_code():
    item = _figure_item(
        touches_resources=_json.dumps(["file:outputs/figures/notes.txt"]),
        artifact_policy=_WARN_POLICY,
    )
    result = evaluate_artifact_pointer_policy(item)
    assert result["warning_code"] == "insufficient_pointer_unsupported_type"


def test_missing_pointer_entirely_uses_missing_pointer_code():
    item = _figure_item(artifact_policy=_WARN_POLICY)
    result = evaluate_artifact_pointer_policy(item)
    assert result["warning_code"] == "missing_pointer"
    assert result["affected_pointer_ids"] == []


def test_concrete_evidence_never_warns_even_under_strict():
    item = _figure_item(
        touches_resources=_json.dumps(["file:outputs/figures/ablation.png"]),
        artifact_policy=_STRICT_POLICY,
    )
    result = evaluate_artifact_pointer_policy(item)
    assert result["warning_code"] is None
    assert result["ready"] is True


# --- off/warn/strict mode matrix --------------------------------------------

def test_warn_mode_emits_warning_but_stays_ready():
    item = _figure_item(artifact_policy=_WARN_POLICY)
    result = evaluate_artifact_pointer_policy(item)
    assert result["warning_code"] is not None
    assert result["ready"] is True


def test_strict_mode_emits_warning_and_is_not_ready():
    item = _figure_item(artifact_policy=_STRICT_POLICY)
    result = evaluate_artifact_pointer_policy(item)
    assert result["warning_code"] is not None
    assert result["ready"] is False


def test_off_mode_suppresses_warning_but_preserves_classification_and_policy():
    """off mode: the policy warning is suppressed while raw declarations
    (classification + effective policy) are still preserved."""
    item = _figure_item(artifact_policy=_OFF_POLICY)
    result = evaluate_artifact_pointer_policy(item)
    assert result["warning_code"] is None
    assert result["required_remediation"] is None
    assert result["affected_pointer_ids"] == []
    assert result["ready"] is True
    # "raw declarations... preserved" — the real classification/policy are
    # NOT replaced with empty/unknown placeholders just because checking is off.
    assert result["classification"]["classification"] == "figure"
    assert result["classification"]["is_artifact_sensitive"] is True
    assert result["policy"]["artifact_pointer_check"] == "off"


def test_off_mode_with_insufficient_pointer_also_suppresses():
    item = _figure_item(
        touches_resources=_json.dumps(["file:outputs/report.docx"]),
        artifact_policy=_OFF_POLICY,
    )
    result = evaluate_artifact_pointer_policy(item)
    assert result["warning_code"] is None
    assert result["ready"] is True
