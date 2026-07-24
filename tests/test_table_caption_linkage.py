"""Coverage for durable table-to-caption linkage (42d398a5).

The table analogue of test_figure_caption_linkage.py (0ff8b982): a stored,
structural reference from a doc_tables row to its caption paragraph (a
doc_elements id), independent of paragraph position.

Exercises:
* the new set_table_caption_link store primitive (used by link_table_caption
  MCP tool) sets the durable link on an existing table and the updated row
  round-trips correctly,
* set_table_caption_link returns None for an unknown / blank table_id,
* the caption_element_id column migration is idempotent and detectable via
  _column_exists on both a fresh schema and a pre-existing one,
* the link_table_caption MCP tool end-to-end through _dispatch_mcp_tool
  (success, missing-args errors, doc-not-found, table-not-found),
* registration parity: link_table_caption is wired everywhere
  link_figure_caption is (mcp_tools.py category dicts + example usage +
  schema, and the notes_decisions dispatch table).
"""
from __future__ import annotations

import asyncio

import pytest

from meridian import doc_store
from meridian import db as db_module


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _open_store(tmp_path) -> doc_store.DocStructureStore:
    conn = await db_module.init_db(str(tmp_path / "doc_structure.db"))
    store = doc_store.DocStructureStore(conn)
    await store.ensure_schema()
    return store


def _seed_store_via_env(tmp_path, monkeypatch):
    sidecar = str(tmp_path / "mcp_doc_structure.db")
    monkeypatch.setenv("MERIDIAN_DOC_STORE_URL", sidecar)
    doc_store._reset_doc_store_cache()
    return sidecar


# ---------------------------------------------------------------------------
# (a) set_table_caption_link store primitive
# ---------------------------------------------------------------------------

def test_set_table_caption_link_stores_durable_link(tmp_path):
    """set_table_caption_link updates the stored caption_element_id and the
    updated row is visible via get_tables."""
    async def _run():
        store = await _open_store(tmp_path)
        try:
            doc = await store.put_document("proj-1", "docx", [], source="a.docx")
            result = await store.put_tables(doc["id"], [
                {"caption": "Table 1: Results"},
            ])
            tbl_id = result["inserted"][0]["id"]
            assert result["inserted"][0]["caption_element_id"] is None

            # Confirm the durable link.
            updated = await store.set_table_caption_link(tbl_id, "el-confirmed-001")
            assert updated is not None
            assert updated["caption_element_id"] == "el-confirmed-001"
            assert updated["id"] == tbl_id

            # Round-trip.
            stored = await store.get_tables(doc["id"])
            assert stored[0]["caption_element_id"] == "el-confirmed-001"
        finally:
            await store.close()

    asyncio.run(_run())


def test_set_table_caption_link_unknown_table_returns_none(tmp_path):
    """set_table_caption_link returns None for an unknown table_id."""
    async def _run():
        store = await _open_store(tmp_path)
        try:
            result = await store.set_table_caption_link("no-such-id", "el-anything")
            assert result is None
        finally:
            await store.close()

    asyncio.run(_run())


def test_set_table_caption_link_blank_table_id_returns_none(tmp_path):
    """set_table_caption_link returns None for a blank/invalid table_id
    (mirrors set_figure_caption_link's guard)."""
    async def _run():
        store = await _open_store(tmp_path)
        try:
            assert await store.set_table_caption_link("", "el-anything") is None
            assert await store.set_table_caption_link("   ", "el-anything") is None
            assert await store.set_table_caption_link(None, "el-anything") is None  # type: ignore[arg-type]
        finally:
            await store.close()

    asyncio.run(_run())


def test_set_table_caption_link_updates_existing_link(tmp_path):
    """A second call re-links (corrects) an already-confirmed table row."""
    async def _run():
        store = await _open_store(tmp_path)
        try:
            doc = await store.put_document("proj-1", "docx", [], source="a.docx")
            result = await store.put_tables(doc["id"], [
                {"caption": "Table 2: Ablation"},
            ])
            tbl_id = result["inserted"][0]["id"]

            first = await store.set_table_caption_link(tbl_id, "el-first")
            assert first["caption_element_id"] == "el-first"

            corrected = await store.set_table_caption_link(tbl_id, "el-corrected")
            assert corrected["caption_element_id"] == "el-corrected"

            stored = await store.get_tables(doc["id"])
            assert stored[0]["caption_element_id"] == "el-corrected"
        finally:
            await store.close()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# (b) caption_element_id migration
# ---------------------------------------------------------------------------

def test_table_caption_element_id_migration_on_existing_db(tmp_path):
    """ensure_schema is idempotent and the doc_tables table carries
    caption_element_id (the ALTER TABLE migration path is a no-op once the
    column is present, and safe to call repeatedly)."""
    async def _run():
        conn = await db_module.init_db(str(tmp_path / "migration_test.db"))
        store = doc_store.DocStructureStore(conn)
        await store.ensure_schema()
        # A second call must not raise (idempotent ALTER TABLE guard).
        await store.ensure_schema()
        # Column must be present.
        assert await store._column_exists("doc_tables", "caption_element_id")
        await store.close()

    asyncio.run(_run())


def test_column_exists_detects_table_caption_element_id(tmp_path):
    """_column_exists correctly reports True for the new column and False for
    a column that does not exist, on the doc_tables table."""
    async def _run():
        store = await _open_store(tmp_path)
        try:
            assert await store._column_exists("doc_tables", "caption_element_id") is True
            assert await store._column_exists("doc_tables", "no_such_column_xyz") is False
        finally:
            await store.close()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# (c) link_table_caption MCP tool, end to end
# ---------------------------------------------------------------------------

def test_mcp_link_table_caption_end_to_end(tmp_path, monkeypatch):
    """link_table_caption MCP tool updates caption_element_id on an indexed table."""
    async def _run():
        from meridian import server as mh

        sidecar = _seed_store_via_env(tmp_path, monkeypatch)
        db = await db_module.init_db(":memory:")
        try:
            proj = await db_module.create_project(db, "table-caption-proj")
            pid = proj["id"]

            seed = await doc_store.open_doc_store_for(
                plan=None, hosted=False, data_dir=str(tmp_path),
                tenant_pg_url=None, override_url=sidecar,
            )
            await seed.put_document(pid, "docx", [], source="chapter1.docx")

            # First index a table (no caption_element_id given).
            idx_res = await mh._dispatch_mcp_tool(
                "index_table",
                {"project_id": pid, "doc": "chapter1.docx",
                 "caption": "Table 1: Summary of experimental results"},
                db, str(tmp_path),
            )
            assert "error" not in idx_res
            tbl_id = idx_res["table"]["id"]

            # Confirm the durable link via link_table_caption.
            link_res = await mh._dispatch_mcp_tool(
                "link_table_caption",
                {"project_id": pid, "doc": "chapter1.docx",
                 "table_id": tbl_id,
                 "caption_element_id": "el-cap-confirmed-001"},
                db, str(tmp_path),
            )
            assert "error" not in link_res
            assert link_res["table"]["caption_element_id"] == "el-cap-confirmed-001"
            assert link_res["table"]["id"] == tbl_id

            # Re-fetch via find_similar_table to confirm round-trip through DB.
            find_res = await mh._dispatch_mcp_tool(
                "find_similar_table",
                {"project_id": pid, "doc": "chapter1.docx",
                 "description": "summary of experimental results"},
                db, str(tmp_path),
            )
            assert "error" not in find_res
            assert find_res["matches"][0]["caption_element_id"] == "el-cap-confirmed-001"
        finally:
            await db.close()
            await doc_store.close_all_doc_stores()

    asyncio.run(_run())


def test_mcp_link_table_caption_requires_all_fields(tmp_path, monkeypatch):
    """link_table_caption returns an error for missing required fields."""
    async def _run():
        from meridian import server as mh

        _seed_store_via_env(tmp_path, monkeypatch)
        db = await db_module.init_db(":memory:")
        try:
            # Missing project_id.
            assert (await mh._dispatch_mcp_tool(
                "link_table_caption", {}, db, str(tmp_path),
            )).get("error")
            # Missing doc.
            assert (await mh._dispatch_mcp_tool(
                "link_table_caption", {"project_id": "p"}, db, str(tmp_path),
            )).get("error")
            # Missing table_id.
            assert (await mh._dispatch_mcp_tool(
                "link_table_caption",
                {"project_id": "p", "doc": "x.docx"},
                db, str(tmp_path),
            )).get("error")
            # Missing caption_element_id.
            assert (await mh._dispatch_mcp_tool(
                "link_table_caption",
                {"project_id": "p", "doc": "x.docx", "table_id": "tbl-1"},
                db, str(tmp_path),
            )).get("error")
        finally:
            await db.close()
            await doc_store.close_all_doc_stores()

    asyncio.run(_run())


def test_mcp_link_table_caption_unknown_doc_returns_error(tmp_path, monkeypatch):
    """link_table_caption returns a helpful error when the doc isn't in the store."""
    async def _run():
        from meridian import server as mh

        _seed_store_via_env(tmp_path, monkeypatch)
        db = await db_module.init_db(":memory:")
        try:
            proj = await db_module.create_project(db, "table-cap-proj-2")
            res = await mh._dispatch_mcp_tool(
                "link_table_caption",
                {"project_id": proj["id"], "doc": "never-ingested.docx",
                 "table_id": "tbl-1", "caption_element_id": "el-1"},
                db, str(tmp_path),
            )
            assert "error" in res
        finally:
            await db.close()
            await doc_store.close_all_doc_stores()

    asyncio.run(_run())


def test_mcp_link_table_caption_unknown_table_id_returns_error(tmp_path, monkeypatch):
    """link_table_caption returns an error (suggesting find_similar_table) when
    table_id doesn't resolve."""
    async def _run():
        from meridian import server as mh

        sidecar = _seed_store_via_env(tmp_path, monkeypatch)
        db = await db_module.init_db(":memory:")
        try:
            proj = await db_module.create_project(db, "table-cap-proj-3")
            pid = proj["id"]
            seed = await doc_store.open_doc_store_for(
                plan=None, hosted=False, data_dir=str(tmp_path),
                tenant_pg_url=None, override_url=sidecar,
            )
            await seed.put_document(pid, "docx", [], source="ch2.docx")
            res = await mh._dispatch_mcp_tool(
                "link_table_caption",
                {"project_id": pid, "doc": "ch2.docx",
                 "table_id": "no-such-table-id",
                 "caption_element_id": "el-1"},
                db, str(tmp_path),
            )
            assert "error" in res
            assert "find_similar_table" in res["error"]
            assert "no-such-table-id" in res["error"]
        finally:
            await db.close()
            await doc_store.close_all_doc_stores()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# (d) registration parity: link_table_caption everywhere link_figure_caption is
# ---------------------------------------------------------------------------

def test_link_table_caption_registered_alongside_link_figure_caption():
    """42d398a5 — link_table_caption must be registered in every place
    link_figure_caption is: the tool schema list, the example-usage dict, and
    each of the three category-mapping dicts (docx / executor /
    maintenance-only). A tool half-registered in some dicts but not others is
    a real bug class in this codebase (see sprint item 42d398a5)."""
    from meridian import mcp_tools

    schema_names = {t["name"] for t in mcp_tools._MCP_TOOLS_LIST}
    assert "link_figure_caption" in schema_names
    assert "link_table_caption" in schema_names

    assert "link_figure_caption" in mcp_tools._TOOL_EXAMPLES
    assert "link_table_caption" in mcp_tools._TOOL_EXAMPLES

    # _TOOL_CATEGORY ("docx"), _TOOL_ROLE_RELEVANCE ("executor"), and
    # _TOOL_WORKFLOW_TIER ("maintenance-only") are the three dicts the sprint
    # item calls out by their value, not their variable name.
    category_dicts = [
        mcp_tools._TOOL_CATEGORY,
        mcp_tools._TOOL_ROLE_RELEVANCE,
        mcp_tools._TOOL_WORKFLOW_TIER,
    ]
    for mapping in category_dicts:
        has_figure = "link_figure_caption" in mapping
        has_table = "link_table_caption" in mapping
        assert has_figure == has_table, (
            "link_table_caption registration diverges from link_figure_caption "
            f"in {mapping!r}"
        )
        if has_figure:
            assert mapping["link_figure_caption"] == mapping["link_table_caption"]


def test_link_table_caption_dispatch_handler_registered():
    """link_table_caption dispatches through the same mechanism as
    link_figure_caption in meridian.mcp.handler."""
    from meridian.mcp.handlers import notes_decisions as nd_mod

    assert hasattr(nd_mod, "handle_link_table_caption")
    assert asyncio.iscoroutinefunction(nd_mod.handle_link_table_caption)
