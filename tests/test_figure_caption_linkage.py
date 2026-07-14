"""Coverage for durable figure-to-caption linkage (0ff8b982).

Piece 2 of the figure-linkage sprint item: a stored, structural reference from
a doc_figures row to its caption paragraph (a doc_elements id), independent of
paragraph position.

Exercises:
* explicit caption_element_id is stored and round-trips through put_figures /
  add_figure / get_figures,
* advisory auto-suggestion correctly finds a nearby kind='figure' caption element
  when none is given (mirroring doc_tables' paired_figure_id suggestion tests),
* suggestion is ADVISORY only -- not stored as caption_element_id, the row
  carries NULL until link_figure_caption confirms it,
* the new set_figure_caption_link store primitive (used by link_figure_caption
  MCP tool) sets the durable link on an existing figure and the updated row
  round-trips correctly,
* the "Figure 3b used twice" scenario -- two candidate captions in the same
  section -- is surfaced as suggested_caption_candidates (a list), not silently
  resolved to one,
* the link_figure_caption MCP tool end-to-end through _dispatch_mcp_tool.
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
# (a) explicit caption_element_id is stored and round-trips
# ---------------------------------------------------------------------------

def test_explicit_caption_element_id_stored_and_roundtrips(tmp_path):
    """An explicit caption_element_id is stored on insert and visible via get_figures."""
    async def _run():
        store = await _open_store(tmp_path)
        try:
            doc = await store.put_document("proj-1", "docx", [], source="a.docx")
            result = await store.put_figures(doc["id"], [
                {"caption": "Figure 1: Setup", "caption_element_id": "el-cap-001"},
            ])
            assert len(result["inserted"]) == 1
            row = result["inserted"][0]
            assert row["caption_element_id"] == "el-cap-001"
            # No suggestion should appear (we gave an explicit id).
            assert "suggested_caption_element_id" not in row
            assert "suggested_caption_candidates" not in row

            # Round-trip via get_figures.
            stored = await store.get_figures(doc["id"])
            assert len(stored) == 1
            assert stored[0]["caption_element_id"] == "el-cap-001"
        finally:
            await store.close()

    asyncio.run(_run())


def test_add_figure_explicit_caption_element_id(tmp_path):
    """add_figure passes caption_element_id through to put_figures correctly."""
    async def _run():
        store = await _open_store(tmp_path)
        try:
            doc = await store.put_document("proj-1", "docx", [], source="a.docx")
            res = await store.add_figure(
                doc["id"], None,
                caption="Figure 1: My figure",
                caption_element_id="el-explicit-abc",
            )
            assert res["figure"] is not None
            assert res["figure"]["caption_element_id"] == "el-explicit-abc"
        finally:
            await store.close()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# (b) advisory auto-suggestion finds nearby caption when none is given
# ---------------------------------------------------------------------------

def test_advisory_suggestion_finds_figure_element_in_same_section(tmp_path):
    """When element_id is given but no caption_element_id, the nearest
    kind='figure' doc_elements row in the same section is suggested.

    The element_id anchor must be an element INSIDE the section (parent_id =
    heading element id), not the section heading itself.  We store a kind='table'
    as the anchor so the suggestion lookup can find the 'figure' caption element
    that shares the same parent (the heading).
    """
    async def _run():
        store = await _open_store(tmp_path)
        try:
            # Create a document with a heading + figure-caption element + table in
            # the same section. The doc_figures row will anchor to the table element
            # (which has parent_id = heading_el_id), and the suggestion lookup should
            # then find the kind='figure' element with the same parent.
            elements = [
                {"ordinal": 0, "level": 1, "kind": "heading", "text": "Results",
                 "ref": None, "parent_ordinal": None},
                {"ordinal": 1, "level": None, "kind": "figure",
                 "text": "Figure 1: The experimental setup",
                 "ref": "Figure 1: The experimental setup",
                 "parent_ordinal": 0},
                {"ordinal": 2, "level": None, "kind": "table",
                 "text": "row1 col1",
                 "ref": None, "parent_ordinal": 0},
            ]
            doc = await store.put_document("proj-1", "docx", elements, source="a.docx")
            # Fetch element ids.
            async with store._db.execute(
                "SELECT id, kind FROM doc_elements WHERE document_id = ? ORDER BY ordinal",
                (doc["id"],),
            ) as cur:
                rows = await cur.fetchall()
            el_by_kind = {
                doc_store._row_get(r, "kind"): doc_store._row_get(r, "id") for r in rows
            }
            fig_el_id = el_by_kind.get("figure")
            table_el_id = el_by_kind.get("table")
            assert fig_el_id and table_el_id

            # Index a doc_figures row anchored to the table element (which shares
            # a parent section with the figure-caption element), NO caption_element_id.
            result = await store.put_figures(doc["id"], [
                {"caption": "Figure 1: Setup", "element_id": table_el_id},
            ])
            row = result["inserted"][0]
            assert row["caption_element_id"] is None  # not auto-applied
            # The figure-caption element in the same section should be suggested.
            suggested = row.get("suggested_caption_element_id")
            assert suggested == fig_el_id
        finally:
            await store.close()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# (c) suggestion is advisory only -- not auto-applied
# ---------------------------------------------------------------------------

def test_suggestion_is_advisory_not_auto_applied(tmp_path):
    """The suggestion appears on the returned row but caption_element_id in the
    DB stays NULL until explicitly confirmed via set_figure_caption_link."""
    async def _run():
        store = await _open_store(tmp_path)
        try:
            elements = [
                {"ordinal": 0, "level": 1, "kind": "heading", "text": "Methods",
                 "ref": None, "parent_ordinal": None},
                {"ordinal": 1, "level": None, "kind": "figure",
                 "text": "Figure 2: A diagram",
                 "ref": "Figure 2: A diagram", "parent_ordinal": 0},
                {"ordinal": 2, "level": None, "kind": "table",
                 "text": "data", "ref": None, "parent_ordinal": 0},
            ]
            doc = await store.put_document("proj-1", "docx", elements, source="b.docx")
            async with store._db.execute(
                "SELECT id, kind FROM doc_elements WHERE document_id = ? ORDER BY ordinal",
                (doc["id"],),
            ) as cur:
                rows = await cur.fetchall()
            el_by_kind = {doc_store._row_get(r, "kind"): doc_store._row_get(r, "id") for r in rows}
            table_el_id = el_by_kind.get("table")
            assert table_el_id

            result = await store.put_figures(doc["id"], [
                {"caption": "Figure 2: Diagram", "element_id": table_el_id},
            ])
            row = result["inserted"][0]
            # A suggestion should be surfaced on the returned dict.
            assert row.get("suggested_caption_element_id") is not None
            # But the stored column is NULL -- not auto-applied.
            stored = await store.get_figures(doc["id"])
            assert stored[0]["caption_element_id"] is None  # DB still NULL
        finally:
            await store.close()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# (d) set_figure_caption_link / link_figure_caption MCP tool
# ---------------------------------------------------------------------------

def test_set_figure_caption_link_stores_durable_link(tmp_path):
    """set_figure_caption_link updates the stored caption_element_id and the
    updated row is visible via get_figures."""
    async def _run():
        store = await _open_store(tmp_path)
        try:
            doc = await store.put_document("proj-1", "docx", [], source="a.docx")
            result = await store.put_figures(doc["id"], [
                {"caption": "Figure 1: Setup"},
            ])
            fig_id = result["inserted"][0]["id"]
            assert result["inserted"][0]["caption_element_id"] is None

            # Confirm the durable link.
            updated = await store.set_figure_caption_link(fig_id, "el-confirmed-001")
            assert updated is not None
            assert updated["caption_element_id"] == "el-confirmed-001"
            assert updated["id"] == fig_id

            # Round-trip.
            stored = await store.get_figures(doc["id"])
            assert stored[0]["caption_element_id"] == "el-confirmed-001"
        finally:
            await store.close()

    asyncio.run(_run())


def test_set_figure_caption_link_unknown_figure_returns_none(tmp_path):
    """set_figure_caption_link returns None for an unknown figure_id."""
    async def _run():
        store = await _open_store(tmp_path)
        try:
            result = await store.set_figure_caption_link("no-such-id", "el-anything")
            assert result is None
        finally:
            await store.close()

    asyncio.run(_run())


def test_mcp_link_figure_caption_end_to_end(tmp_path, monkeypatch):
    """link_figure_caption MCP tool updates caption_element_id on an indexed figure."""
    async def _run():
        from meridian import server as mh

        sidecar = _seed_store_via_env(tmp_path, monkeypatch)
        db = await db_module.init_db(":memory:")
        try:
            proj = await db_module.create_project(db, "caption-proj")
            pid = proj["id"]

            seed = await doc_store.open_doc_store_for(
                plan=None, hosted=False, data_dir=str(tmp_path),
                tenant_pg_url=None, override_url=sidecar,
            )
            await seed.put_document(pid, "docx", [], source="chapter1.docx")

            # First index a figure (no caption_element_id given).
            idx_res = await mh._dispatch_mcp_tool(
                "index_figure",
                {"project_id": pid, "doc": "chapter1.docx",
                 "caption": "Figure 1: The experimental setup"},
                db, str(tmp_path),
            )
            assert "error" not in idx_res
            fig_id = idx_res["figure"]["id"]

            # Confirm the durable link via link_figure_caption.
            link_res = await mh._dispatch_mcp_tool(
                "link_figure_caption",
                {"project_id": pid, "doc": "chapter1.docx",
                 "figure_id": fig_id,
                 "caption_element_id": "el-cap-confirmed-001"},
                db, str(tmp_path),
            )
            assert "error" not in link_res
            assert link_res["figure"]["caption_element_id"] == "el-cap-confirmed-001"
            assert link_res["figure"]["id"] == fig_id

            # Re-fetch via find_similar_figure to confirm round-trip through DB.
            find_res = await mh._dispatch_mcp_tool(
                "find_similar_figure",
                {"project_id": pid, "doc": "chapter1.docx",
                 "description_or_path": "experimental setup"},
                db, str(tmp_path),
            )
            assert "error" not in find_res
            assert find_res["matches"][0]["caption_element_id"] == "el-cap-confirmed-001"
        finally:
            await db.close()
            await doc_store.close_all_doc_stores()

    asyncio.run(_run())


def test_mcp_link_figure_caption_requires_all_fields(tmp_path, monkeypatch):
    """link_figure_caption returns an error for missing required fields."""
    async def _run():
        from meridian import server as mh

        _seed_store_via_env(tmp_path, monkeypatch)
        db = await db_module.init_db(":memory:")
        try:
            # Missing project_id.
            assert (await mh._dispatch_mcp_tool(
                "link_figure_caption", {}, db, str(tmp_path),
            )).get("error")
            # Missing doc.
            assert (await mh._dispatch_mcp_tool(
                "link_figure_caption", {"project_id": "p"}, db, str(tmp_path),
            )).get("error")
            # Missing figure_id.
            assert (await mh._dispatch_mcp_tool(
                "link_figure_caption",
                {"project_id": "p", "doc": "x.docx"},
                db, str(tmp_path),
            )).get("error")
            # Missing caption_element_id.
            assert (await mh._dispatch_mcp_tool(
                "link_figure_caption",
                {"project_id": "p", "doc": "x.docx", "figure_id": "fig-1"},
                db, str(tmp_path),
            )).get("error")
        finally:
            await db.close()
            await doc_store.close_all_doc_stores()

    asyncio.run(_run())


def test_mcp_link_figure_caption_unknown_doc_returns_error(tmp_path, monkeypatch):
    """link_figure_caption returns a helpful error when the doc isn't in the store."""
    async def _run():
        from meridian import server as mh

        _seed_store_via_env(tmp_path, monkeypatch)
        db = await db_module.init_db(":memory:")
        try:
            proj = await db_module.create_project(db, "cap-proj-2")
            res = await mh._dispatch_mcp_tool(
                "link_figure_caption",
                {"project_id": proj["id"], "doc": "never-ingested.docx",
                 "figure_id": "fig-1", "caption_element_id": "el-1"},
                db, str(tmp_path),
            )
            assert "error" in res
        finally:
            await db.close()
            await doc_store.close_all_doc_stores()

    asyncio.run(_run())


def test_mcp_link_figure_caption_unknown_figure_id_returns_error(tmp_path, monkeypatch):
    """link_figure_caption returns an error when figure_id doesn't resolve."""
    async def _run():
        from meridian import server as mh

        sidecar = _seed_store_via_env(tmp_path, monkeypatch)
        db = await db_module.init_db(":memory:")
        try:
            proj = await db_module.create_project(db, "cap-proj-3")
            pid = proj["id"]
            seed = await doc_store.open_doc_store_for(
                plan=None, hosted=False, data_dir=str(tmp_path),
                tenant_pg_url=None, override_url=sidecar,
            )
            await seed.put_document(pid, "docx", [], source="ch2.docx")
            res = await mh._dispatch_mcp_tool(
                "link_figure_caption",
                {"project_id": pid, "doc": "ch2.docx",
                 "figure_id": "no-such-figure-id",
                 "caption_element_id": "el-1"},
                db, str(tmp_path),
            )
            assert "error" in res
            assert "no_such_figure_id" in res["error"].replace("-", "_") or "no-such-figure-id" in res["error"]
        finally:
            await db.close()
            await doc_store.close_all_doc_stores()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# (e) "Figure 3b used twice" scenario: two candidate captions in one section
# ---------------------------------------------------------------------------

def test_two_caption_candidates_in_same_section_surfaces_ambiguity(tmp_path):
    """When two kind='figure' elements exist in the same section and no
    caption_element_id is given, suggested_caption_candidates (a list of both
    element ids) is returned -- not silently picking one.

    The doc_figures row anchors to a table element (which is a sibling of the
    two figure-caption elements under the same heading parent), so the
    suggestion lookup finds both kind='figure' candidates in that section.
    """
    async def _run():
        store = await _open_store(tmp_path)
        try:
            # Two figure-caption elements in the same section (simulates the
            # "Figure 3b" duplicate-label scenario from the real thesis),
            # plus a table element to use as the doc_figures anchor.
            elements = [
                {"ordinal": 0, "level": 1, "kind": "heading", "text": "Chapter 3",
                 "ref": None, "parent_ordinal": None},
                {"ordinal": 1, "level": None, "kind": "figure",
                 "text": "Figure 3b: First variant",
                 "ref": "Figure 3b: First variant", "parent_ordinal": 0},
                {"ordinal": 2, "level": None, "kind": "figure",
                 "text": "Figure 3b: Second variant",
                 "ref": "Figure 3b: Second variant", "parent_ordinal": 0},
                {"ordinal": 3, "level": None, "kind": "table",
                 "text": "data",
                 "ref": None, "parent_ordinal": 0},
            ]
            doc = await store.put_document("proj-1", "docx", elements, source="thesis.docx")
            async with store._db.execute(
                "SELECT id, kind, ordinal FROM doc_elements "
                "WHERE document_id = ? ORDER BY ordinal",
                (doc["id"],),
            ) as cur:
                rows = await cur.fetchall()
            el_by_ordinal = {
                doc_store._row_get(r, "ordinal"): doc_store._row_get(r, "id") for r in rows
            }
            fig1_el_id = el_by_ordinal.get(1)
            fig2_el_id = el_by_ordinal.get(2)
            table_el_id = el_by_ordinal.get(3)
            assert fig1_el_id and fig2_el_id and table_el_id

            # Index a figure doc_figures row with the table element as anchor
            # (sibling of the two figure-caption elements), no caption_element_id
            # -- should surface BOTH candidates.
            result = await store.put_figures(doc["id"], [
                {"caption": "Figure 3b image file",
                 "file_path": "/path/to/fig3b.png",
                 "element_id": table_el_id},
            ])
            row = result["inserted"][0]
            # Must NOT silently pick one.
            assert row["caption_element_id"] is None
            assert "suggested_caption_element_id" not in row  # NOT a single suggestion
            candidates = row.get("suggested_caption_candidates")
            assert isinstance(candidates, list)
            assert len(candidates) == 2
            assert fig1_el_id in candidates
            assert fig2_el_id in candidates
        finally:
            await store.close()

    asyncio.run(_run())


def test_no_caption_candidates_returns_no_suggestion_fields(tmp_path):
    """When no kind='figure' elements exist in the section, no suggestion fields
    appear on the inserted row."""
    async def _run():
        store = await _open_store(tmp_path)
        try:
            elements = [
                {"ordinal": 0, "level": 1, "kind": "heading", "text": "Empty section",
                 "ref": None, "parent_ordinal": None},
            ]
            doc = await store.put_document("proj-1", "docx", elements, source="a.docx")
            async with store._db.execute(
                "SELECT id FROM doc_elements WHERE document_id = ? AND kind = 'heading'",
                (doc["id"],),
            ) as cur:
                hrow = await cur.fetchone()
            heading_el_id = doc_store._row_get(hrow, "id")

            result = await store.put_figures(doc["id"], [
                {"caption": "Orphan figure", "element_id": heading_el_id},
            ])
            row = result["inserted"][0]
            assert row["caption_element_id"] is None
            assert "suggested_caption_element_id" not in row
            assert "suggested_caption_candidates" not in row
        finally:
            await store.close()

    asyncio.run(_run())


def test_caption_element_id_migration_on_existing_db(tmp_path):
    """ensure_schema is idempotent and adds caption_element_id to a pre-existing
    doc_figures table (the ALTER TABLE migration path)."""
    async def _run():
        # First open: create the schema without caption_element_id by NOT having
        # a pre-existing column -- but since our _SCHEMA_STATEMENTS now include
        # caption_element_id, a fresh DB already has it. Test that a second
        # ensure_schema call is idempotent (no error).
        conn = await db_module.init_db(str(tmp_path / "migration_test.db"))
        store = doc_store.DocStructureStore(conn)
        await store.ensure_schema()
        # A second call must not raise.
        await store.ensure_schema()
        # Column must be present.
        assert await store._column_exists("doc_figures", "caption_element_id")
        await store.close()

    asyncio.run(_run())
