"""6f7ce9d6 -- sprint-item pointer targets and deletes are project-scoped.

Audit (turf/citations 2026-09-27): the sprint ITEM is project-scoped, but

* ``finding_id`` / ``node_id`` pointer targets resolved by BARE id
  (``db.get_project_note(db, id)`` / ``DocStructureStore.get_element_by_id(id)``),
  so a pointer on a project-A item could read project B's note or document
  element; and
* ``delete_sprint_item_pointer`` deleted ``WHERE id = ?`` with no ``project_id``
  (NEW-1), while add / get / resolve / relocate all take a project.

These tests pin: a cross-project id resolves exactly like an unknown id (same
reason, no body, no confirmation the foreign row exists), and a delete with the
wrong project_id removes nothing.
"""
from __future__ import annotations

import asyncio
import json

import pytest
import pytest_asyncio

from meridian import db as db_module
from meridian import doc_store
from meridian import pointers as pointers_module
from meridian.pointers import resolve_pointer

_FOREIGN_BODY = "TOP-SECRET-PROJECT-B-BODY-6f7ce9d6"
_FOREIGN_ELEMENT_TEXT = "PROJECT-B-PRIVATE-HEADING-6f7ce9d6"


@pytest_asyncio.fixture
async def close_doc_stores():
    """``resolve_sprint_item_pointers`` opens (and caches) the tier-resolved
    doc_store sidecar under ``data_dir``; close it after the test so its
    aiosqlite worker thread doesn't outlive pytest and hang interpreter exit."""
    yield
    await doc_store.close_all_doc_stores()



async def _two_projects(db):
    a = await db_module.create_project(db, "scope-proj-a")
    b = await db_module.create_project(db, "scope-proj-b")
    return a, b


async def _foreign_finding(db, project_id: str) -> str:
    finding = await db_module.save_finding(
        db, project_id, f"finding in B\n{_FOREIGN_BODY}", source_type="experiment",
    )
    return finding["note"]["id"]


def _finding_ptr(note_id: str, **extra) -> dict:
    return {
        "source_type": "experiment",
        "targets": [{"uri": f"finding:{note_id}",
                     "selector": {"type": "finding_id", "id": note_id}}],
        **extra,
    }


# ---------------------------------------------------------------------------
# db.get_project_note -- scoped lookup
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_project_note_scoped_lookup(db):
    a, b = await _two_projects(db)
    note_b = await db_module.add_project_note(db, b["id"], "b-note", _FOREIGN_BODY)

    # Owning project sees it; a different project gets None, like a missing id.
    assert (await db_module.get_project_note(db, note_b["id"], project_id=b["id"]))["id"] == note_b["id"]
    assert await db_module.get_project_note(db, note_b["id"], project_id=a["id"]) is None
    assert await db_module.get_project_note(db, "no-such-note", project_id=a["id"]) is None
    # An empty-string scope is a scoped lookup that matches nothing -- never unscoped.
    assert await db_module.get_project_note(db, note_b["id"], project_id="") is None
    # Legacy unscoped form (internal callers that just wrote / already own the row).
    assert (await db_module.get_project_note(db, note_b["id"]))["id"] == note_b["id"]


# ---------------------------------------------------------------------------
# add_sprint_item_pointer -- the item must belong to the supplied project
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mcp_add_pointer_rejects_foreign_sprint_item(db, tmp_path):
    """A project-A write cannot attach a pointer to project B's sprint item."""
    from meridian import server as srv

    a, b = await _two_projects(db)
    item_b = await db_module.add_sprint_item(db, b["id"], "v1", "B item")

    result = await srv._dispatch_mcp_tool(
        "add_sprint_item_pointer",
        {
            "project_id": a["id"],
            "sprint_item_id": item_b["id"],
            "source_type": "code",
            "targets": [{
                "uri": "src/private.py",
                "selector": {"type": "range", "start_line": 1, "end_line": 2},
            }],
        },
        db,
        str(tmp_path),
    )

    assert "error" in result
    assert await db_module.get_sprint_item_pointers(db, item_b["id"]) == []


# ---------------------------------------------------------------------------
# finding_id targets
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_finding_pointer_cross_project_via_mcp_resolves_not_found(
    db, tmp_path, close_doc_stores,
):
    """The exact audit scenario, end to end through the real MCP dispatch: a
    project-A pointer whose finding_id belongs to project B must NOT resolve."""
    from meridian import server as srv

    a, b = await _two_projects(db)
    item_a = await db_module.add_sprint_item(db, a["id"], "v1", "A item")
    foreign_note_id = await _foreign_finding(db, b["id"])

    await srv._dispatch_mcp_tool(
        "add_sprint_item_pointer",
        {"project_id": a["id"], "sprint_item_id": item_a["id"],
         "source_type": "experiment",
         "targets": [
             {"uri": f"finding:{foreign_note_id}",
              "selector": {"type": "finding_id", "id": foreign_note_id}},
             {"uri": "finding:does-not-exist",
              "selector": {"type": "finding_id", "id": "does-not-exist"}},
         ]},
        db, str(tmp_path),
    )
    resolved = await srv._dispatch_mcp_tool(
        "resolve_sprint_item_pointers",
        {"project_id": a["id"], "sprint_item_id": item_a["id"]},
        db, str(tmp_path),
    )
    foreign, unknown = resolved["pointers"][0]["targets"]
    assert foreign["resolved"] is False
    assert "artifact" not in foreign
    # Indistinguishable from an id that simply doesn't exist: same reason, and
    # the whole payload leaks neither the body nor the foreign project's id.
    assert foreign["reason"] == unknown["reason"]
    blob = json.dumps(resolved, default=str)
    assert _FOREIGN_BODY not in blob
    assert b["id"] not in blob


@pytest.mark.asyncio
async def test_finding_pointer_same_project_still_resolves(
    db, tmp_path, close_doc_stores,
):
    """Positive control: scoping must not break the legitimate same-project case."""
    from meridian import server as srv

    a, _b = await _two_projects(db)
    item_a = await db_module.add_sprint_item(db, a["id"], "v1", "A item")
    own = await db_module.save_finding(
        db, a["id"], "finding in A\nown-body-marker", source_type="experiment",
    )
    note_id = own["note"]["id"]
    await srv._dispatch_mcp_tool(
        "add_sprint_item_pointer",
        {"project_id": a["id"], "sprint_item_id": item_a["id"],
         "source_type": "experiment",
         "targets": [{"uri": f"finding:{note_id}",
                      "selector": {"type": "finding_id", "id": note_id}}]},
        db, str(tmp_path),
    )
    resolved = await srv._dispatch_mcp_tool(
        "resolve_sprint_item_pointers",
        {"project_id": a["id"], "sprint_item_id": item_a["id"]},
        db, str(tmp_path),
    )
    target = resolved["pointers"][0]["targets"][0]
    assert target["resolved"] is True
    assert target["artifact"]["id"] == note_id
    assert "own-body-marker" in target["artifact"]["body"]


@pytest.mark.asyncio
async def test_default_finding_resolver_is_scoped_to_the_pointers_own_project(db):
    """resolve_pointer's DEFAULT finding resolver (no injected seam): the scope
    is the caller's project_id, else the stored pointer row's own project_id."""
    a, b = await _two_projects(db)
    note_b = await _foreign_finding(db, b["id"])

    # Explicit project_id (project A) -> B's note is invisible.
    out = await resolve_pointer(db, _finding_ptr(note_b), project_id=a["id"])
    t = out["targets"][0]
    assert t["resolved"] is False and "artifact" not in t
    assert _FOREIGN_BODY not in json.dumps(out, default=str)

    # No explicit scope: the pointer row's own project_id is used -> still A.
    out = await resolve_pointer(db, _finding_ptr(note_b, project_id=a["id"]))
    assert out["targets"][0]["resolved"] is False

    # ...and the owning project (B) does resolve it.
    out = await resolve_pointer(db, _finding_ptr(note_b), project_id=b["id"])
    assert out["targets"][0]["resolved"] is True
    assert _FOREIGN_BODY in out["targets"][0]["artifact"]["body"]
    out = await resolve_pointer(db, _finding_ptr(note_b, project_id=b["id"]))
    assert out["targets"][0]["resolved"] is True


@pytest.mark.asyncio
async def test_default_finding_resolver_fails_closed_with_no_owning_project(db):
    """No project_id argument AND none on the pointer row: nothing to scope by,
    so the default resolver must NOT fall back to the old bare-id lookup."""
    _a, b = await _two_projects(db)
    note_b = await _foreign_finding(db, b["id"])
    out = await resolve_pointer(db, _finding_ptr(note_b))
    t = out["targets"][0]
    assert t["resolved"] is False
    assert _FOREIGN_BODY not in json.dumps(out, default=str)


@pytest.mark.asyncio
async def test_injected_finding_resolver_foreign_row_is_rejected():
    """Defense in depth: whatever resolver is injected, a returned note that
    self-identifies a different project never surfaces."""
    ptr = _finding_ptr("n1")

    async def foreign(_id):
        return {"id": _id, "project_id": "proj-B", "title": "t", "body": _FOREIGN_BODY}

    out = await resolve_pointer(None, ptr, project_id="proj-A", finding_resolver=foreign)
    t = out["targets"][0]
    assert t["resolved"] is False
    assert t["reason"] == "no finding artifact with that id"
    assert _FOREIGN_BODY not in json.dumps(out, default=str)

    async def own(_id):
        return {"id": _id, "project_id": "proj-A", "title": "t", "body": "mine"}

    assert (await resolve_pointer(
        None, ptr, project_id="proj-A", finding_resolver=own,
    ))["targets"][0]["resolved"] is True

    # A stub that carries no project identity is unverifiable here and keeps
    # working (legacy seam contract) -- the in-repo seams are all scoped.
    async def bare(_id):
        return {"id": _id, "title": "t", "body": "mine"}

    assert (await resolve_pointer(
        None, ptr, project_id="proj-A", finding_resolver=bare,
    ))["targets"][0]["resolved"] is True


@pytest.mark.asyncio
async def test_finding_subselector_is_scoped_too(db):
    """A subSelector resolves through the same dispatch, so a foreign
    finding_id hidden in a subSelector is rejected as well."""
    a, b = await _two_projects(db)
    note_b = await _foreign_finding(db, b["id"])
    ptr = {"source_type": "code", "targets": [{
        "uri": "a.py",
        "selector": {"type": "range", "start_line": 1, "end_line": 2,
                     "subSelector": {"type": "finding_id", "id": note_b}},
    }]}
    out = await resolve_pointer(db, ptr, project_id=a["id"])
    sub = out["targets"][0]["subResolved"]
    assert sub["resolved"] is False
    assert _FOREIGN_BODY not in json.dumps(out, default=str)


# ---------------------------------------------------------------------------
# node_id targets
# ---------------------------------------------------------------------------

async def _store_with_foreign_element(project_b: str):
    """A doc_store sidecar holding ONE document + element owned by project B."""
    conn = await db_module.init_db(":memory:")
    store = doc_store.DocStructureStore(conn)
    await store.ensure_schema()
    doc = await store.put_document(
        project_b, "docx",
        [{"ordinal": 0, "level": 1, "kind": "heading",
          "text": _FOREIGN_ELEMENT_TEXT, "ref": "p1", "parent_ordinal": None}],
        source="b-private.docx", title="B private doc",
    )
    struct = await store.get_structure(project_b, "b-private.docx")
    return store, doc, struct["elements"][0]["id"]


def test_doc_store_get_element_by_id_is_project_scoped():
    async def _run():
        store, doc, eid = await _store_with_foreign_element("proj-B")
        try:
            # Owning project resolves it, with its document header.
            own = await store.get_element_by_id(eid, project_id="proj-B")
            assert own["element"]["text"] == _FOREIGN_ELEMENT_TEXT
            assert own["document"]["id"] == doc["id"]
            assert own["document"]["project_id"] == "proj-B"
            # Another project gets None -- same as an unknown id.
            assert await store.get_element_by_id(eid, project_id="proj-A") is None
            assert await store.get_element_by_id("nope", project_id="proj-A") is None
            # Empty-string scope matches nothing (fails closed), never unscoped.
            assert await store.get_element_by_id(eid, project_id="") is None
            # Legacy unscoped read is unchanged for callers that own the id.
            legacy = await store.get_element_by_id(eid)
            assert legacy["element"]["id"] == eid
        finally:
            await store.close()

    asyncio.run(_run())


def test_node_pointer_cross_project_via_mcp_resolves_not_found(monkeypatch):
    """A project-A pointer whose node_id is project B's doc element must NOT
    resolve through the real MCP handler (the handler's node_resolver is scoped)."""
    from meridian import server as srv
    from meridian.mcp import handler as mh

    async def _run():
        db = await db_module.init_db(":memory:")
        a = await db_module.create_project(db, "node-scope-a")
        b = await db_module.create_project(db, "node-scope-b")
        store, _doc, foreign_eid = await _store_with_foreign_element(b["id"])
        try:
            async def _fake_store(_db, _data_dir, _tenant):
                return store

            monkeypatch.setattr(mh, "_resolve_ingest_doc_store", _fake_store)

            item_a = await db_module.add_sprint_item(db, a["id"], "v1", "A item")
            item_b = await db_module.add_sprint_item(db, b["id"], "v1", "B item")
            for item, proj in ((item_a, a), (item_b, b)):
                await srv._dispatch_mcp_tool(
                    "add_sprint_item_pointer",
                    {"project_id": proj["id"], "sprint_item_id": item["id"],
                     "source_type": "docs",
                     "targets": [{"uri": "doc:b-private",
                                  "selector": {"type": "node_id", "id": foreign_eid}}]},
                    db, "/tmp",
                )

            # Project A: rejected exactly like an unknown id, nothing leaked.
            res_a = await srv._dispatch_mcp_tool(
                "resolve_sprint_item_pointers",
                {"project_id": a["id"], "sprint_item_id": item_a["id"]}, db, "/tmp",
            )
            t = res_a["pointers"][0]["targets"][0]
            assert t["resolved"] is False
            assert t["reason"] == "no element with that id"
            assert "element" not in t and "document" not in t
            blob = json.dumps(res_a, default=str)
            assert _FOREIGN_ELEMENT_TEXT not in blob
            assert "B private doc" not in blob

            # Positive control: project B resolving its own element still works.
            res_b = await srv._dispatch_mcp_tool(
                "resolve_sprint_item_pointers",
                {"project_id": b["id"], "sprint_item_id": item_b["id"]}, db, "/tmp",
            )
            tb = res_b["pointers"][0]["targets"][0]
            assert tb["resolved"] is True
            assert tb["element"]["text"] == _FOREIGN_ELEMENT_TEXT
        finally:
            await store.close()
            await db.close()

    asyncio.run(_run())


@pytest.mark.asyncio
async def test_injected_node_resolver_foreign_document_is_rejected():
    """Defense in depth for node_id: a resolved row whose document names a
    different project never surfaces, whatever resolver was injected."""
    ptr = {"source_type": "docs", "targets": [
        {"uri": "doc:1", "selector": {"type": "node_id", "id": "el-9"}}]}

    async def foreign(_id):
        return {"element": {"id": _id, "text": _FOREIGN_ELEMENT_TEXT},
                "document": {"id": "d", "project_id": "proj-B", "title": "B doc"}}

    out = await resolve_pointer(None, ptr, project_id="proj-A", node_resolver=foreign)
    t = out["targets"][0]
    assert t["resolved"] is False
    assert t["reason"] == "no element with that id"
    assert _FOREIGN_ELEMENT_TEXT not in json.dumps(out, default=str)

    async def own(_id):
        return {"element": {"id": _id, "text": "mine"},
                "document": {"id": "d", "project_id": "proj-A", "title": "A doc"}}

    assert (await resolve_pointer(
        None, ptr, project_id="proj-A", node_resolver=own,
    ))["targets"][0]["resolved"] is True


# ---------------------------------------------------------------------------
# delete_sprint_item_pointer -- project_id required and enforced (NEW-1)
# ---------------------------------------------------------------------------

async def _pointer_in(db, project, item):
    return await db_module.add_sprint_item_pointer(
        db, project["id"], item["id"], "code",
        [{"uri": "a.py", "selector": {"type": "range", "start_line": 1, "end_line": 2}}],
    )


@pytest.mark.asyncio
async def test_db_delete_pointer_wrong_project_deletes_nothing(db):
    a, b = await _two_projects(db)
    item_a = await db_module.add_sprint_item(db, a["id"], "v1", "A item")
    ptr = await _pointer_in(db, a, item_a)

    # Caller scoped to B: reported exactly like a nonexistent id, row untouched.
    assert await db_module.delete_sprint_item_pointer(db, b["id"], ptr["id"]) is False
    assert [p["id"] for p in await db_module.get_sprint_item_pointers(db, item_a["id"])] == [ptr["id"]]

    # The owning project deletes it.
    assert await db_module.delete_sprint_item_pointer(db, a["id"], ptr["id"]) is True
    assert await db_module.get_sprint_item_pointers(db, item_a["id"]) == []


@pytest.mark.asyncio
async def test_db_delete_pointer_requires_project_id_argument(db):
    """A stale two-argument call must fail loudly, never run unscoped."""
    a, _b = await _two_projects(db)
    item_a = await db_module.add_sprint_item(db, a["id"], "v1", "A item")
    ptr = await _pointer_in(db, a, item_a)
    with pytest.raises(TypeError):
        await db_module.delete_sprint_item_pointer(db, ptr["id"])  # type: ignore[call-arg]
    assert len(await db_module.get_sprint_item_pointers(db, item_a["id"])) == 1


@pytest.mark.asyncio
async def test_mcp_delete_pointer_wrong_project_deletes_nothing(db):
    from meridian import server as srv

    a, b = await _two_projects(db)
    item_a = await db_module.add_sprint_item(db, a["id"], "v1", "A item")
    ptr = await _pointer_in(db, a, item_a)

    wrong = await srv._dispatch_mcp_tool(
        "delete_sprint_item_pointer",
        {"project_id": b["id"], "pointer_id": ptr["id"]}, db, "/tmp",
    )
    assert wrong == {"pointer_id": ptr["id"], "deleted": False}
    # ...identical to the response for an id that never existed.
    missing = await srv._dispatch_mcp_tool(
        "delete_sprint_item_pointer",
        {"project_id": b["id"], "pointer_id": "no-such-pointer"}, db, "/tmp",
    )
    assert missing == {"pointer_id": "no-such-pointer", "deleted": False}
    assert len(await db_module.get_sprint_item_pointers(db, item_a["id"])) == 1

    ok = await srv._dispatch_mcp_tool(
        "delete_sprint_item_pointer",
        {"project_id": a["id"], "pointer_id": ptr["id"]}, db, "/tmp",
    )
    assert ok == {"pointer_id": ptr["id"], "deleted": True}
    assert await db_module.get_sprint_item_pointers(db, item_a["id"]) == []


@pytest.mark.asyncio
async def test_mcp_delete_pointer_requires_project_id(db):
    from meridian import server as srv

    a, _b = await _two_projects(db)
    item_a = await db_module.add_sprint_item(db, a["id"], "v1", "A item")
    ptr = await _pointer_in(db, a, item_a)

    res = await srv._dispatch_mcp_tool(
        "delete_sprint_item_pointer", {"pointer_id": ptr["id"]}, db, "/tmp",
    )
    assert "error" in res and "project_id" in res["error"]
    assert "deleted" not in res
    assert len(await db_module.get_sprint_item_pointers(db, item_a["id"])) == 1


@pytest.mark.asyncio
async def test_mcp_delete_pointer_by_project_name(db):
    from meridian import server as srv

    a, _b = await _two_projects(db)
    item_a = await db_module.add_sprint_item(db, a["id"], "v1", "A item")
    ptr = await _pointer_in(db, a, item_a)
    res = await srv._dispatch_mcp_tool(
        "delete_sprint_item_pointer",
        {"project_name": "scope-proj-a", "pointer_id": ptr["id"]}, db, "/tmp",
    )
    assert res == {"pointer_id": ptr["id"], "deleted": True}


@pytest.mark.asyncio
async def test_mcp_delete_pointer_scoped_caller_cannot_reach_foreign_project(db):
    """With the project_id now required, the dispatch layer's project-scope gate
    covers this tool: a key scoped to project A can neither name project B nor
    omit the project to slip past the gate."""
    from meridian import server as srv

    a, b = await _two_projects(db)
    item_b = await db_module.add_sprint_item(db, b["id"], "v1", "B item")
    ptr_b = await _pointer_in(db, b, item_b)

    with pytest.raises(ValueError, match="outside your access scope"):
        await srv._dispatch_mcp_tool(
            "delete_sprint_item_pointer",
            {"project_id": b["id"], "pointer_id": ptr_b["id"]},
            db, "/tmp", scoped_project_ids=[a["id"]],
        )
    omitted = await srv._dispatch_mcp_tool(
        "delete_sprint_item_pointer", {"pointer_id": ptr_b["id"]},
        db, "/tmp", scoped_project_ids=[a["id"]],
    )
    assert "error" in omitted
    # Even naming A (in scope) cannot delete B's pointer -- the SQL is scoped.
    in_scope = await srv._dispatch_mcp_tool(
        "delete_sprint_item_pointer",
        {"project_id": a["id"], "pointer_id": ptr_b["id"]},
        db, "/tmp", scoped_project_ids=[a["id"]],
    )
    assert in_scope["deleted"] is False
    assert len(await db_module.get_sprint_item_pointers(db, item_b["id"])) == 1


def test_delete_pointer_tool_schema_advertises_project_scope():
    """The advertised schema and the dispatch must agree: project_id /
    project_name are accepted (the handler requires one of them)."""
    from meridian.mcp_tools import _MCP_TOOLS_LIST

    tool = next(t for t in _MCP_TOOLS_LIST if t["name"] == "delete_sprint_item_pointer")
    props = tool["inputSchema"]["properties"]
    assert {"project_id", "project_name", "pointer_id"} <= set(props)
    assert "pointer_id" in tool["inputSchema"]["required"]


@pytest.mark.asyncio
async def test_batch_rollback_compensation_still_deletes_the_pointer(db):
    """batch_management's all_or_nothing compensation calls
    delete_sprint_item_pointer; it swallows every exception, so a stale
    signature there would fail SILENTLY (pointer left behind). Pin that the
    compensation really removes the pointer -- and only within its project."""
    from meridian.db import batch_management as bm

    a, b = await _two_projects(db)
    item_a = await db_module.add_sprint_item(db, a["id"], "v1", "A item")
    ptr = await _pointer_in(db, a, item_a)

    # Wrong project: compensation is a no-op (never touches a foreign pointer).
    await bm._compensate_pointer_entry(db, b["id"], ("pointer", ptr["id"]), None)  # type: ignore[arg-type]
    assert len(await db_module.get_sprint_item_pointers(db, item_a["id"])) == 1

    await bm._compensate_pointer_entry(db, a["id"], ("pointer", ptr["id"]), None)  # type: ignore[arg-type]
    assert await db_module.get_sprint_item_pointers(db, item_a["id"]) == []


def test_module_exports_unchanged():
    """The scoping change must not drop the public surface other modules import."""
    assert callable(pointers_module.resolve_pointer)
    assert callable(db_module.get_project_note)
    assert callable(db_module.delete_sprint_item_pointer)
