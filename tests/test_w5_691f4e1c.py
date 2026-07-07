"""691f4e1c — add/update-time prospecting persists a DURABLE, queryable pointer.

Refinement of 926bf221 / a8550238: previously add_sprint_item / update_sprint_item
only populated ``touches_resources`` + surfaced a one-shot ``code_context`` hint listing
the ``search_graph`` / ``find_symbol`` calls the caller SHOULD run — nothing durable was
persisted, so the prospected guess was lost if unused. This item upgrades the write path
to also persist a real ``symbol``-source sprint-item pointer (retrievable later via
``get_sprint_item_pointers`` / resolvable through the tunnel), built tunnel-free from the
item's declared symbols.

Boundary (04a15d3f): the server can't reach the code index (it's behind the executor's
tunnel), so it can't run ``search_graph`` to resolve an exact line ``range`` at write
time. The tunnel-free equivalent is a W3C ``symbol`` selector carrying the declared
``qualified_name``, best-matched against the graph LATER at resolve time. A pure ``file:``
touch with no declared symbol yields no durable pointer (fabricating one would be
misleading) — only the inline hint ships for it.

Unit/mock-only: drives the pure helpers directly + the dispatcher against an in-memory
aiosqlite DB (same pattern as tests/test_cov_handler.py). No tunnel, no network.
"""
from __future__ import annotations

import asyncio

import meridian.server  # noqa: F401 — import first to avoid handler/server import cycle
from meridian import handoff as ho
from meridian.mcp import handler as mh


def _run(coro):
    return asyncio.run(coro)


def _make_db():
    import meridian.db as db_module

    return _run(db_module.init_db(":memory:"))


# ---------------------------------------------------------------------------
# handoff.build_declared_symbol_targets — the shared, tunnel-free target builder
# ---------------------------------------------------------------------------

def test_build_declared_symbol_targets_from_symbol_scope():
    """A ``symbol:<path>::<name>`` id yields a symbol selector whose qualified_name is
    the bare symbol name and whose uri is the declared file."""
    targets = ho.build_declared_symbol_targets(
        {"touches_resources": ["symbol:meridian/db/__init__.py::create_project"]}
    )
    assert targets == [
        {
            "uri": "file:meridian/db/__init__.py",
            "selector": {"type": "symbol", "qualified_name": "create_project"},
        }
    ]


def test_build_declared_symbol_targets_pairs_bare_symbol_with_declared_file():
    """A bare ``symbol:<name>`` (no ``::`` scope) is paired with the first declared
    file as its uri, and stores the whole tail as the qualified_name."""
    targets = ho.build_declared_symbol_targets(
        {"touches_resources": ["file:meridian/handoff.py", "symbol:_prospect_query"]}
    )
    assert targets == [
        {
            "uri": "file:meridian/handoff.py",
            "selector": {"type": "symbol", "qualified_name": "_prospect_query"},
        }
    ]


def test_build_declared_symbol_targets_json_string_and_inferred_prefix():
    """Tolerates the JSON-string storage shape and strips the ``inferred:`` marker."""
    targets = ho.build_declared_symbol_targets(
        {"touches_resources": '["inferred:symbol:meridian/server.py::app"]'}
    )
    assert targets == [
        {
            "uri": "file:meridian/server.py",
            "selector": {"type": "symbol", "qualified_name": "app"},
        }
    ]


def test_build_declared_symbol_targets_dedupes_repeated_symbol():
    targets = ho.build_declared_symbol_targets(
        {"touches_resources": [
            "symbol:a.py::foo",
            "symbol:b.py::foo",  # same bare name → deduped
        ]}
    )
    assert len(targets) == 1
    assert targets[0]["selector"]["qualified_name"] == "foo"


def test_build_declared_symbol_targets_file_only_yields_nothing():
    """BOUNDARY: a pure file touch has no exact symbol location tunnel-free, so no
    durable pointer is fabricated for it (only the inline hint ships)."""
    assert ho.build_declared_symbol_targets(
        {"touches_resources": ["file:meridian/db/__init__.py"]}
    ) == []
    assert ho.build_declared_symbol_targets(
        {"touches_resources": ["inferred:file:meridian/server.py"]}
    ) == []


def test_build_declared_symbol_targets_empty_and_malformed_never_raise():
    assert ho.build_declared_symbol_targets({}) == []
    assert ho.build_declared_symbol_targets({"touches_resources": None}) == []
    assert ho.build_declared_symbol_targets({"touches_resources": "not json ["}) == []
    assert ho.build_declared_symbol_targets({"touches_resources": ["symbol:"]}) == []


# ---------------------------------------------------------------------------
# handler._prospected_pointer_targets — delegates to the shared builder
# ---------------------------------------------------------------------------

def test_prospected_pointer_targets_delegates_to_handoff():
    item = {"touches_resources": ["symbol:meridian/handoff.py::_prospect_query"]}
    assert mh._prospected_pointer_targets(item) == ho.build_declared_symbol_targets(item)
    # File-only → no targets (same boundary as the shared builder).
    assert mh._prospected_pointer_targets(
        {"touches_resources": ["file:meridian/handoff.py"]}
    ) == []


# ---------------------------------------------------------------------------
# handler._persist_prospected_pointer — guards
# ---------------------------------------------------------------------------

def test_persist_pointer_skips_when_not_prospected():
    db = _make_db()
    assert _run(mh._persist_prospected_pointer(
        db, "p", {"id": "i", "touches_resources": ["symbol:a.py::foo"]}, "no_targets"
    )) is None
    assert _run(mh._persist_prospected_pointer(db, "p", None, "prospected")) is None
    # Missing id / project → no persist.
    assert _run(mh._persist_prospected_pointer(
        db, "p", {"touches_resources": ["symbol:a.py::foo"]}, "prospected"
    )) is None


def test_persist_pointer_skips_file_only_item():
    """A prospected file-only item has no durable symbol target → nothing persisted."""
    db = _make_db()
    import meridian.db as db_module
    proj = _run(db_module.create_project(db, "p"))
    item = _run(db_module.add_sprint_item(
        db, proj["id"], "v1", "file item",
        touches_resources=["file:meridian/server.py"],
    ))
    assert _run(mh._persist_prospected_pointer(
        db, proj["id"], item, "prospected"
    )) is None
    assert _run(db_module.get_sprint_item_pointers(db, item["id"])) == []


# ---------------------------------------------------------------------------
# add_sprint_item — end-to-end: persists a durable, retrievable pointer
# ---------------------------------------------------------------------------

def test_add_sprint_item_persists_durable_symbol_pointer():
    db = _make_db()
    import meridian.db as db_module
    proj = _run(db_module.create_project(db, "prospect-persist-add"))
    out = _run(mh._dispatch_mcp_tool("add_sprint_item", {
        "project_id": proj["id"], "version": "v1",
        "title": "Refactor create_project",
        "touches_resources": ["symbol:meridian/db/__init__.py::create_project"],
        "force": True,  # skip the drift guard for determinism
    }, db, "/tmp"))
    # The inline hint + status are still surfaced (926bf221 / a8550238 unchanged).
    assert out.get("prospecting_status") == "prospected"
    # 691f4e1c — AND a durable pointer is now persisted and echoed back.
    ptr = out.get("prospected_pointer")
    assert ptr and ptr.get("source_type") == "symbol"
    # It is genuinely retrievable later (not a one-shot hint).
    stored = _run(db_module.get_sprint_item_pointers(db, out["id"]))
    assert len(stored) == 1
    sel = stored[0]["targets"][0]["selector"]
    assert sel["type"] == "symbol"
    assert sel["qualified_name"] == "create_project"
    assert stored[0]["targets"][0]["uri"] == "file:meridian/db/__init__.py"


def test_add_sprint_item_file_only_persists_no_pointer_but_keeps_hint():
    """BOUNDARY: a file-only item still gets the inline code_context hint, but no
    durable pointer is fabricated (no exact symbol location tunnel-free)."""
    db = _make_db()
    import meridian.db as db_module
    proj = _run(db_module.create_project(db, "prospect-file-only"))
    out = _run(mh._dispatch_mcp_tool("add_sprint_item", {
        "project_id": proj["id"], "version": "v1",
        "title": "Touch the server module",
        "touches_resources": ["file:meridian/server.py"],
        "force": True,
    }, db, "/tmp"))
    assert out.get("prospecting_status") == "prospected"
    assert "meridian/server.py" in out.get("code_context", {}).get("files", [])
    assert "prospected_pointer" not in out
    assert _run(db_module.get_sprint_item_pointers(db, out["id"])) == []


def test_add_sprint_item_manual_persists_no_pointer():
    db = _make_db()
    import meridian.db as db_module
    proj = _run(db_module.create_project(db, "prospect-manual"))
    out = _run(mh._dispatch_mcp_tool("add_sprint_item", {
        "project_id": proj["id"], "version": "v1",
        "title": "MANUAL (Adam): form an LLC",
        "milestone_type": "human",
        "force": True,
    }, db, "/tmp"))
    assert out.get("prospecting_status") == "skipped_manual"
    assert "prospected_pointer" not in out
    assert _run(db_module.get_sprint_item_pointers(db, out["id"])) == []


# ---------------------------------------------------------------------------
# update_sprint_item — persists on update + never stacks duplicate pointers
# ---------------------------------------------------------------------------

def test_update_sprint_item_persists_pointer_and_no_duplicate_on_rerun():
    db = _make_db()
    import meridian.db as db_module
    proj = _run(db_module.create_project(db, "prospect-persist-upd"))
    item = _run(db_module.add_sprint_item(db, proj["id"], "v1", "Some item"))
    # First update declares a real symbol → persists a durable pointer.
    out = _run(mh._dispatch_mcp_tool("update_sprint_item", {
        "project_id": proj["id"], "item_id": item["id"],
        "touches_resources": ["symbol:meridian/handoff.py::_prospect_query"],
    }, db, "/tmp"))
    assert out.get("prospecting_status") == "prospected"
    assert out.get("prospected_pointer", {}).get("source_type") == "symbol"
    assert len(_run(db_module.get_sprint_item_pointers(db, item["id"]))) == 1
    # Re-running update must NOT stack a second pointer (idempotent-ish).
    out2 = _run(mh._dispatch_mcp_tool("update_sprint_item", {
        "project_id": proj["id"], "item_id": item["id"],
        "notes": "tweak the notes",
        "touches_resources": ["symbol:meridian/handoff.py::_prospect_query"],
    }, db, "/tmp"))
    assert "prospected_pointer" not in out2  # already durable — not re-created
    assert len(_run(db_module.get_sprint_item_pointers(db, item["id"]))) == 1
