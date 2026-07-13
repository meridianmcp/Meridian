"""Coverage for the SEMANTIC table index (2622182d).

This is the table parallel of the figure index (c623e648, see
``test_index_figure.py``): a self-contained ``doc_tables`` table with
difflib-based advisory near-duplicate detection on a normalized caption. Unlike
doc_figures, doc_tables has no image file asset -- instead carrying a
table_index and an optional paired_figure_id. It is complementary to -- not a
duplicate of -- the structural ``kind='table'`` doc_elements placement.

Exercises:

* the pure ``normalize_table_caption`` / ``_table_similarity`` helpers without a DB,
* DocStructureStore's table methods (put_tables / get_tables /
  find_similar_tables / add_table) end to end on a local SQLite sidecar,
  including the advisory near-duplicate surface AND the paired_figure_id
  auto-suggestion behavior,
* the two new MCP tools (index_table, find_similar_table) through the real
  _dispatch_mcp_tool path.
"""
from __future__ import annotations

import asyncio

import pytest

from meridian import doc_store
from meridian import db as db_module


# ---------------------------------------------------------------------------
# Pure helpers — no database
# ---------------------------------------------------------------------------

def test_normalize_table_caption_strips_label_lowercases_and_collapses_whitespace():
    # Leading "Table N:" auto-number label is stripped; case folded; whitespace collapsed.
    assert doc_store.normalize_table_caption("Table 3: Summary of Results") == "summary of results"
    assert doc_store.normalize_table_caption("Tbl. 12 - Block   diagram\nof the ADC") == "block diagram of the adc"
    # A caption that merely CONTAINS 'table' mid-string is not mangled.
    assert doc_store.normalize_table_caption("A table of contents") == "a table of contents"
    # None / blank yield the empty key.
    assert doc_store.normalize_table_caption(None) == ""
    assert doc_store.normalize_table_caption("   ") == ""


def test_table_similarity_bounds():
    assert doc_store._table_similarity("", "x") == 0.0
    assert doc_store._table_similarity("abc", "abc") == 1.0
    assert 0.0 < doc_store._table_similarity("the setup", "the setups") < 1.0


# ---------------------------------------------------------------------------
# DocStructureStore table methods — full round-trip on a local sidecar
# ---------------------------------------------------------------------------

async def _open_store(tmp_path) -> doc_store.DocStructureStore:
    conn = await db_module.init_db(str(tmp_path / "doc_structure.db"))
    store = doc_store.DocStructureStore(conn)
    await store.ensure_schema()
    return store


def test_put_tables_inserts_and_get_tables_orders_by_ordinal(tmp_path):
    async def _run():
        store = await _open_store(tmp_path)
        try:
            doc = await store.put_document("proj-1", "docx", [], source="a.docx")
            result = await store.put_tables(doc["id"], [
                {"caption": "Table 1: Summary of results", "table_index": 1,
                 "semantic_label": "results"},
                {"caption": "Table 2: Comparison of methods", "table_index": 2},
            ])
            assert len(result["inserted"]) == 2
            assert result["near_duplicates"] == []

            stored = await store.get_tables(doc["id"])
            assert [t["ordinal"] for t in stored] == [0, 1]
            assert stored[0]["semantic_label"] == "results"
            assert stored[0]["normalized_caption"] == "summary of results"
            assert stored[0]["table_index"] == 1
        finally:
            await store.close()

    asyncio.run(_run())


def test_put_tables_surfaces_near_duplicate_caption_without_skipping_insert(tmp_path):
    """A near-duplicate caption is STILL inserted (never silently dropped) but
    flagged via near_duplicates (2622182d dedup contract, mirroring figures)."""
    async def _run():
        store = await _open_store(tmp_path)
        try:
            doc = await store.put_document("proj-1", "docx", [], source="a.docx")
            await store.put_tables(doc["id"], [
                {"caption": "Table 1: Summary of results"},
            ])
            result = await store.put_tables(doc["id"], [
                {"caption": "Table 2: Summary of results."},  # same prose
            ])
            assert len(result["inserted"]) == 1  # still inserted
            assert len(result["near_duplicates"]) == 1
            dup = result["near_duplicates"][0]
            assert dup["table_id"] == result["inserted"][0]["id"]
            assert dup["score"] >= 0.85

            all_tables = await store.get_tables(doc["id"])
            assert len(all_tables) == 2  # both rows persist
        finally:
            await store.close()

    asyncio.run(_run())


def test_put_tables_dedupes_within_same_batch(tmp_path):
    """Two near-identical captions in the SAME put_tables call also dedupe
    against each other (the batch's own earlier rows), not just prior stores."""
    async def _run():
        store = await _open_store(tmp_path)
        try:
            doc = await store.put_document("proj-1", "docx", [], source="a.docx")
            result = await store.put_tables(doc["id"], [
                {"caption": "Comparison of receiver architectures"},
                {"caption": "Comparison of receiver architectures"},
            ])
            assert len(result["inserted"]) == 2
            assert len(result["near_duplicates"]) == 1
            assert result["near_duplicates"][0]["matched_id"] == result["inserted"][0]["id"]
        finally:
            await store.close()

    asyncio.run(_run())


def test_put_tables_explicit_paired_figure_id_is_stored(tmp_path):
    """An explicit paired_figure_id is stored as-is without suggestion lookup."""
    async def _run():
        store = await _open_store(tmp_path)
        try:
            doc = await store.put_document("proj-1", "docx", [], source="a.docx")
            result = await store.put_tables(doc["id"], [
                {"caption": "Table 1: Results", "paired_figure_id": "fig-abc-123"},
            ])
            assert result["inserted"][0]["paired_figure_id"] == "fig-abc-123"
            # No suggested_figure_id when paired_figure_id is given explicitly.
            assert "suggested_figure_id" not in result["inserted"][0]
        finally:
            await store.close()

    asyncio.run(_run())


def test_put_tables_advisory_figure_suggestion_via_element_id(tmp_path):
    """When element_id is given and a figure exists in the same section, a
    suggested_figure_id is surfaced (advisory -- not auto-applied)."""
    async def _run():
        store = await _open_store(tmp_path)
        try:
            # Store a document with a heading + figure element in the same section.
            elements = [
                {"ordinal": 0, "level": 1, "kind": "heading", "text": "Results",
                 "ref": None, "parent_ordinal": None},
                {"ordinal": 1, "level": None, "kind": "figure", "text": "Figure 1",
                 "ref": "Figure 1: Setup", "parent_ordinal": 0},
                {"ordinal": 2, "level": None, "kind": "table", "text": "Table 1",
                 "ref": "Table 1: Summary", "parent_ordinal": 0},
            ]
            doc = await store.put_document("proj-1", "docx", elements, source="a.docx")
            # Fetch the element ids.
            stored_els = await store._db.execute(
                "SELECT id, kind, parent_id FROM doc_elements WHERE document_id = ? ORDER BY ordinal",
                (doc["id"],),
            )
            rows = await stored_els.fetchall()
            el_by_kind = {doc_store._row_get(r, "kind"): doc_store._row_get(r, "id") for r in rows}
            fig_el_id = el_by_kind.get("figure")
            tbl_el_id = el_by_kind.get("table")
            assert fig_el_id and tbl_el_id

            # Index the table with its element_id but no paired_figure_id.
            result = await store.put_tables(doc["id"], [
                {"caption": "Table 1: Summary", "element_id": tbl_el_id},
            ])
            assert result["inserted"][0]["paired_figure_id"] is None
            # The figure in the same section should be suggested.
            assert result["inserted"][0].get("suggested_figure_id") == fig_el_id
        finally:
            await store.close()

    asyncio.run(_run())


def test_find_similar_tables_ranks_by_caption(tmp_path):
    async def _run():
        store = await _open_store(tmp_path)
        try:
            doc = await store.put_document("proj-1", "docx", [], source="a.docx")
            await store.put_tables(doc["id"], [
                {"caption": "Table 1: Summary of experimental results", "table_index": 1},
                {"caption": "Table 2: Comparison of baseline methods", "table_index": 2},
                {"caption": "Table 3: Statistical test outputs", "table_index": 3},
            ])

            # By description — the results caption ranks first.
            by_desc = await store.find_similar_tables(doc["id"], "experimental results", limit=2)
            assert len(by_desc) == 2
            assert by_desc[0]["normalized_caption"] == "summary of experimental results"
            assert by_desc[0]["score"] > by_desc[1]["score"]

            # Unknown document (no tables) yields an empty list, not an error.
            empty = await store.find_similar_tables("no-such-doc", "x")
            assert empty == []
        finally:
            await store.close()

    asyncio.run(_run())


def test_add_table_returns_shape(tmp_path):
    async def _run():
        store = await _open_store(tmp_path)
        try:
            doc = await store.put_document("proj-1", "docx", [], source="a.docx")

            res = await store.add_table(
                doc["id"], 1,
                caption="Table 1: Something", semantic_label="thing",
            )
            assert res["table"]["semantic_label"] == "thing"
            assert res["table"]["normalized_caption"] == "something"
            assert res["table"]["table_index"] == 1
            assert res["near_duplicates"] == []

            # A caption-only table (no table_index) is fine.
            res2 = await store.add_table(doc["id"], None, caption="Table 2: Caption only")
            assert res2["table"]["table_index"] is None
            assert res2["table"]["normalized_caption"] == "caption only"
        finally:
            await store.close()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# MCP tools — index_table + find_similar_table
# ---------------------------------------------------------------------------

def _seed_store_via_env(tmp_path, monkeypatch):
    sidecar = str(tmp_path / "mcp_doc_structure.db")
    monkeypatch.setenv("MERIDIAN_DOC_STORE_URL", sidecar)
    doc_store._reset_doc_store_cache()
    return sidecar


def test_mcp_index_table_and_find_similar_table_round_trip(tmp_path, monkeypatch):
    async def _run():
        from meridian import server as mh

        sidecar = _seed_store_via_env(tmp_path, monkeypatch)
        db = await db_module.init_db(":memory:")
        try:
            proj = await db_module.create_project(db, "tbl-proj")
            pid = proj["id"]

            seed = await doc_store.open_doc_store_for(
                plan=None, hosted=False, data_dir=str(tmp_path),
                tenant_pg_url=None, override_url=sidecar,
            )
            await seed.put_document(pid, "docx", [], source="chapter1.docx")

            res = await mh._dispatch_mcp_tool(
                "index_table",
                {"project_id": pid, "doc": "chapter1.docx",
                 "table_index": 1,
                 "caption": "Table 1: Summary of experimental results",
                 "semantic_label": "results table"},
                db, str(tmp_path),
            )
            assert "error" not in res
            assert res["table"]["semantic_label"] == "results table"
            assert res["near_duplicates"] == []

            find_res = await mh._dispatch_mcp_tool(
                "find_similar_table",
                {"project_id": pid, "doc": "chapter1.docx",
                 "description": "experimental results"},
                db, str(tmp_path),
            )
            assert "error" not in find_res
            assert find_res["matches"][0]["semantic_label"] == "results table"
            assert find_res["matches"][0]["score"] > 0.5

            # A near-duplicate caption is surfaced, not silently dropped.
            dup_res = await mh._dispatch_mcp_tool(
                "index_table",
                {"project_id": pid, "doc": "chapter1.docx",
                 "caption": "Table 2: Summary of experimental results"},
                db, str(tmp_path),
            )
            assert len(dup_res["near_duplicates"]) == 1
        finally:
            await db.close()
            await doc_store.close_all_doc_stores()

    asyncio.run(_run())


def test_mcp_index_table_requires_project_id_doc_and_payload(tmp_path, monkeypatch):
    async def _run():
        from meridian import server as mh

        _seed_store_via_env(tmp_path, monkeypatch)
        db = await db_module.init_db(":memory:")
        try:
            assert (await mh._dispatch_mcp_tool(
                "index_table", {}, db, str(tmp_path),
            )).get("error")
            assert (await mh._dispatch_mcp_tool(
                "index_table", {"project_id": "p"}, db, str(tmp_path),
            )).get("error")
            # doc present but neither table_index nor caption is an error.
            assert (await mh._dispatch_mcp_tool(
                "index_table", {"project_id": "p", "doc": "x.docx"}, db, str(tmp_path),
            )).get("error")
        finally:
            await db.close()
            await doc_store.close_all_doc_stores()

    asyncio.run(_run())


def test_mcp_index_table_unknown_doc_returns_helpful_error(tmp_path, monkeypatch):
    async def _run():
        from meridian import server as mh

        _seed_store_via_env(tmp_path, monkeypatch)
        db = await db_module.init_db(":memory:")
        try:
            proj = await db_module.create_project(db, "tbl-proj-2")
            res = await mh._dispatch_mcp_tool(
                "index_table",
                {"project_id": proj["id"], "doc": "never-ingested.docx",
                 "caption": "Table 1"},
                db, str(tmp_path),
            )
            assert "error" in res
            assert "never-ingested.docx" in res["error"]
        finally:
            await db.close()
            await doc_store.close_all_doc_stores()

    asyncio.run(_run())


def test_mcp_find_similar_table_unknown_doc_returns_empty_matches(tmp_path, monkeypatch):
    async def _run():
        from meridian import server as mh

        _seed_store_via_env(tmp_path, monkeypatch)
        db = await db_module.init_db(":memory:")
        try:
            proj = await db_module.create_project(db, "tbl-proj-3")
            res = await mh._dispatch_mcp_tool(
                "find_similar_table",
                {"project_id": proj["id"], "doc": "never-ingested.docx",
                 "description": "summary"},
                db, str(tmp_path),
            )
            assert "error" not in res
            assert res["matches"] == []
            assert res["document_id"] is None
        finally:
            await db.close()
            await doc_store.close_all_doc_stores()

    asyncio.run(_run())


def test_mcp_find_similar_table_requires_project_id_doc_and_query(tmp_path, monkeypatch):
    async def _run():
        from meridian import server as mh

        _seed_store_via_env(tmp_path, monkeypatch)
        db = await db_module.init_db(":memory:")
        try:
            assert (await mh._dispatch_mcp_tool(
                "find_similar_table", {}, db, str(tmp_path),
            )).get("error")
            assert (await mh._dispatch_mcp_tool(
                "find_similar_table", {"project_id": "p", "doc": "x.docx"}, db, str(tmp_path),
            )).get("error")
        finally:
            await db.close()
            await doc_store.close_all_doc_stores()

    asyncio.run(_run())
