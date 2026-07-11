"""Coverage for the SEMANTIC figure index (c623e648).

This is the figure parallel of the equation index (06df6ab3, see
``test_doc_equations.py``): a self-contained ``doc_figures`` table with
difflib-based advisory near-duplicate detection on a normalized caption, PLUS an
on-disk existence check that FLAGS (never hard-fails) a missing figure asset. It
is complementary to — not a duplicate of — the structural ``kind='figure'``
doc_elements placement.

Exercises:

* the pure ``normalize_caption`` / ``_figure_similarity`` helpers without a DB,
* DocStructureStore's figure methods (put_figures / get_figures /
  find_similar_figures / add_figure) end to end on a local SQLite sidecar,
  including the advisory near-duplicate surface AND the missing-file flag,
* the two new MCP tools (index_figure, find_similar_figure) through the real
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

def test_normalize_caption_strips_label_lowercases_and_collapses_whitespace():
    # Leading "Figure N:" auto-number label is stripped; case folded; runs of
    # whitespace collapse to one space; ends trimmed.
    assert doc_store.normalize_caption("Figure 3: The Experimental Setup") == "the experimental setup"
    assert doc_store.normalize_caption("Fig. 12 - Block   diagram\nof the ADC") == "block diagram of the adc"
    # A caption that merely CONTAINS 'figure' mid-string is not mangled.
    assert doc_store.normalize_caption("A figure of merit") == "a figure of merit"
    # None / blank yield the empty key.
    assert doc_store.normalize_caption(None) == ""
    assert doc_store.normalize_caption("   ") == ""


def test_figure_similarity_bounds():
    assert doc_store._figure_similarity("", "x") == 0.0
    assert doc_store._figure_similarity("abc", "abc") == 1.0
    assert 0.0 < doc_store._figure_similarity("the setup", "the setups") < 1.0


# ---------------------------------------------------------------------------
# DocStructureStore figure methods — full round-trip on a local sidecar
# ---------------------------------------------------------------------------

async def _open_store(tmp_path) -> doc_store.DocStructureStore:
    conn = await db_module.init_db(str(tmp_path / "doc_structure.db"))
    store = doc_store.DocStructureStore(conn)
    await store.ensure_schema()
    return store


def test_put_figures_inserts_and_get_figures_orders_by_ordinal(tmp_path):
    async def _run():
        # A real asset on disk so file_exists resolves to 1 (present).
        asset = tmp_path / "setup.png"
        asset.write_bytes(b"\x89PNG\r\n\x1a\n")

        store = await _open_store(tmp_path)
        try:
            doc = await store.put_document("proj-1", "docx", [], source="a.docx")
            result = await store.put_figures(doc["id"], [
                {"file_path": str(asset), "caption": "Figure 1: The experimental setup",
                 "semantic_label": "apparatus"},
                {"file_path": str(asset), "caption": "Figure 2: A histogram of results"},
            ])
            assert len(result["inserted"]) == 2
            assert result["near_duplicates"] == []
            assert result["missing_files"] == []  # the asset exists

            stored = await store.get_figures(doc["id"])
            assert [f["ordinal"] for f in stored] == [0, 1]
            assert stored[0]["semantic_label"] == "apparatus"
            assert stored[0]["normalized_caption"] == "the experimental setup"
            assert stored[0]["file_exists"] == 1
        finally:
            await store.close()

    asyncio.run(_run())


def test_put_figures_flags_missing_file_without_hard_failing(tmp_path):
    """A referenced file that is not on disk is FLAGGED (file_exists=0 +
    missing_files entry), never a hard failure — the figure still inserts."""
    async def _run():
        store = await _open_store(tmp_path)
        try:
            doc = await store.put_document("proj-1", "docx", [], source="a.docx")
            result = await store.put_figures(doc["id"], [
                {"file_path": str(tmp_path / "does_not_exist.png"),
                 "caption": "Figure 1: Ghost figure"},
            ])
            assert len(result["inserted"]) == 1  # still inserted
            assert len(result["missing_files"]) == 1
            assert result["missing_files"][0]["figure_id"] == result["inserted"][0]["id"]
            assert result["inserted"][0]["file_exists"] == 0
        finally:
            await store.close()

    asyncio.run(_run())


def test_put_figures_surfaces_near_duplicate_caption_without_skipping_insert(tmp_path):
    """A near-duplicate caption is STILL inserted (never silently dropped) but
    flagged via near_duplicates (c623e648 dedup contract, mirroring equations)."""
    async def _run():
        store = await _open_store(tmp_path)
        try:
            doc = await store.put_document("proj-1", "docx", [], source="a.docx")
            await store.put_figures(doc["id"], [
                {"caption": "Figure 1: The experimental setup"},
            ])
            result = await store.put_figures(doc["id"], [
                {"caption": "Figure 2: The experimental setup."},  # same prose
            ])
            assert len(result["inserted"]) == 1  # still inserted
            assert len(result["near_duplicates"]) == 1
            dup = result["near_duplicates"][0]
            assert dup["figure_id"] == result["inserted"][0]["id"]
            assert dup["score"] >= 0.85

            all_figs = await store.get_figures(doc["id"])
            assert len(all_figs) == 2  # both rows persist
        finally:
            await store.close()

    asyncio.run(_run())


def test_put_figures_dedupes_within_same_batch(tmp_path):
    """Two near-identical captions in the SAME put_figures call also dedupe
    against each other (the batch's own earlier rows), not just prior stores."""
    async def _run():
        store = await _open_store(tmp_path)
        try:
            doc = await store.put_document("proj-1", "docx", [], source="a.docx")
            result = await store.put_figures(doc["id"], [
                {"caption": "Block diagram of the receiver"},
                {"caption": "Block diagram of the receiver"},
            ])
            assert len(result["inserted"]) == 2
            assert len(result["near_duplicates"]) == 1
            assert result["near_duplicates"][0]["matched_id"] == result["inserted"][0]["id"]
        finally:
            await store.close()

    asyncio.run(_run())


def test_find_similar_figures_ranks_by_caption_and_by_path(tmp_path):
    async def _run():
        store = await _open_store(tmp_path)
        try:
            doc = await store.put_document("proj-1", "docx", [], source="a.docx")
            await store.put_figures(doc["id"], [
                {"file_path": "figures/setup.png", "caption": "Figure 1: The experimental setup"},
                {"file_path": "figures/hist.png", "caption": "Figure 2: A histogram of the results"},
                {"file_path": "figures/flow.png", "caption": "Figure 3: The control flow"},
            ])

            # By description — the setup caption ranks first.
            by_desc = await store.find_similar_figures(doc["id"], "experimental setup", limit=2)
            assert len(by_desc) == 2
            assert by_desc[0]["normalized_caption"] == "the experimental setup"
            assert by_desc[0]["score"] > by_desc[1]["score"]

            # By path — the matching file_path ranks first even with no caption words.
            by_path = await store.find_similar_figures(doc["id"], "figures/hist.png")
            assert by_path[0]["file_path"] == "figures/hist.png"
            assert by_path[0]["score"] > 0.9

            # Unknown document (no figures) yields an empty list, not an error.
            empty = await store.find_similar_figures("no-such-doc", "x")
            assert empty == []
        finally:
            await store.close()

    asyncio.run(_run())


def test_add_figure_returns_shape_and_missing_flag(tmp_path):
    async def _run():
        store = await _open_store(tmp_path)
        try:
            doc = await store.put_document("proj-1", "docx", [], source="a.docx")

            res = await store.add_figure(
                doc["id"], str(tmp_path / "absent.png"),
                caption="Figure 1: Something", semantic_label="thing",
            )
            assert res["figure"]["semantic_label"] == "thing"
            assert res["figure"]["normalized_caption"] == "something"
            assert res["near_duplicates"] == []
            assert len(res["missing_files"]) == 1  # absent.png does not exist

            # A caption-only figure (no path) is fine — no file check, no flag.
            res2 = await store.add_figure(doc["id"], None, caption="Figure 2: Caption only")
            assert res2["figure"]["file_path"] is None
            assert res2["figure"]["file_exists"] is None
            assert res2["missing_files"] == []
        finally:
            await store.close()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# MCP tools — index_figure + find_similar_figure
# ---------------------------------------------------------------------------

def _seed_store_via_env(tmp_path, monkeypatch):
    sidecar = str(tmp_path / "mcp_doc_structure.db")
    monkeypatch.setenv("MERIDIAN_DOC_STORE_URL", sidecar)
    doc_store._reset_doc_store_cache()
    return sidecar


def test_mcp_index_figure_and_find_similar_figure_round_trip(tmp_path, monkeypatch):
    async def _run():
        from meridian import server as mh

        asset = tmp_path / "setup.png"
        asset.write_bytes(b"\x89PNG\r\n\x1a\n")

        sidecar = _seed_store_via_env(tmp_path, monkeypatch)
        db = await db_module.init_db(":memory:")
        try:
            proj = await db_module.create_project(db, "fig-proj")
            pid = proj["id"]

            seed = await doc_store.open_doc_store_for(
                plan=None, hosted=False, data_dir=str(tmp_path),
                tenant_pg_url=None, override_url=sidecar,
            )
            await seed.put_document(pid, "docx", [], source="chapter1.docx")

            res = await mh._dispatch_mcp_tool(
                "index_figure",
                {"project_id": pid, "doc": "chapter1.docx",
                 "file_path": str(asset),
                 "caption": "Figure 1: The experimental setup",
                 "semantic_label": "apparatus"},
                db, str(tmp_path),
            )
            assert "error" not in res
            assert res["figure"]["semantic_label"] == "apparatus"
            assert res["near_duplicates"] == []
            assert res["missing_files"] == []  # the asset exists on disk

            find_res = await mh._dispatch_mcp_tool(
                "find_similar_figure",
                {"project_id": pid, "doc": "chapter1.docx",
                 "description_or_path": "experimental setup"},
                db, str(tmp_path),
            )
            assert "error" not in find_res
            assert find_res["matches"][0]["semantic_label"] == "apparatus"
            assert find_res["matches"][0]["score"] > 0.5

            # A near-duplicate caption is surfaced, not silently dropped.
            dup_res = await mh._dispatch_mcp_tool(
                "index_figure",
                {"project_id": pid, "doc": "chapter1.docx",
                 "caption": "Figure 2: The experimental setup"},
                db, str(tmp_path),
            )
            assert len(dup_res["near_duplicates"]) == 1
        finally:
            await db.close()
            await doc_store.close_all_doc_stores()

    asyncio.run(_run())


def test_mcp_index_figure_flags_missing_file(tmp_path, monkeypatch):
    async def _run():
        from meridian import server as mh

        sidecar = _seed_store_via_env(tmp_path, monkeypatch)
        db = await db_module.init_db(":memory:")
        try:
            proj = await db_module.create_project(db, "fig-proj-missing")
            pid = proj["id"]
            seed = await doc_store.open_doc_store_for(
                plan=None, hosted=False, data_dir=str(tmp_path),
                tenant_pg_url=None, override_url=sidecar,
            )
            await seed.put_document(pid, "docx", [], source="chapter1.docx")

            res = await mh._dispatch_mcp_tool(
                "index_figure",
                {"project_id": pid, "doc": "chapter1.docx",
                 "file_path": str(tmp_path / "nope.png"),
                 "caption": "Figure 1: Ghost"},
                db, str(tmp_path),
            )
            assert "error" not in res
            assert len(res["missing_files"]) == 1
            assert res["figure"]["file_exists"] == 0
        finally:
            await db.close()
            await doc_store.close_all_doc_stores()

    asyncio.run(_run())


def test_mcp_index_figure_requires_project_id_doc_and_payload(tmp_path, monkeypatch):
    async def _run():
        from meridian import server as mh

        _seed_store_via_env(tmp_path, monkeypatch)
        db = await db_module.init_db(":memory:")
        try:
            assert (await mh._dispatch_mcp_tool(
                "index_figure", {}, db, str(tmp_path),
            )).get("error")
            assert (await mh._dispatch_mcp_tool(
                "index_figure", {"project_id": "p"}, db, str(tmp_path),
            )).get("error")
            # doc present but neither file_path nor caption is an error.
            assert (await mh._dispatch_mcp_tool(
                "index_figure", {"project_id": "p", "doc": "x.docx"}, db, str(tmp_path),
            )).get("error")
        finally:
            await db.close()
            await doc_store.close_all_doc_stores()

    asyncio.run(_run())


def test_mcp_index_figure_unknown_doc_returns_helpful_error(tmp_path, monkeypatch):
    async def _run():
        from meridian import server as mh

        _seed_store_via_env(tmp_path, monkeypatch)
        db = await db_module.init_db(":memory:")
        try:
            proj = await db_module.create_project(db, "fig-proj-2")
            res = await mh._dispatch_mcp_tool(
                "index_figure",
                {"project_id": proj["id"], "doc": "never-ingested.docx",
                 "caption": "Figure 1"},
                db, str(tmp_path),
            )
            assert "error" in res
            assert "never-ingested.docx" in res["error"]
        finally:
            await db.close()
            await doc_store.close_all_doc_stores()

    asyncio.run(_run())


def test_mcp_find_similar_figure_unknown_doc_returns_empty_matches(tmp_path, monkeypatch):
    async def _run():
        from meridian import server as mh

        _seed_store_via_env(tmp_path, monkeypatch)
        db = await db_module.init_db(":memory:")
        try:
            proj = await db_module.create_project(db, "fig-proj-3")
            res = await mh._dispatch_mcp_tool(
                "find_similar_figure",
                {"project_id": proj["id"], "doc": "never-ingested.docx",
                 "description_or_path": "setup"},
                db, str(tmp_path),
            )
            assert "error" not in res
            assert res["matches"] == []
            assert res["document_id"] is None
        finally:
            await db.close()
            await doc_store.close_all_doc_stores()

    asyncio.run(_run())


def test_mcp_find_similar_figure_requires_project_id_doc_and_query(tmp_path, monkeypatch):
    async def _run():
        from meridian import server as mh

        _seed_store_via_env(tmp_path, monkeypatch)
        db = await db_module.init_db(":memory:")
        try:
            assert (await mh._dispatch_mcp_tool(
                "find_similar_figure", {}, db, str(tmp_path),
            )).get("error")
            assert (await mh._dispatch_mcp_tool(
                "find_similar_figure", {"project_id": "p", "doc": "x.docx"}, db, str(tmp_path),
            )).get("error")
        finally:
            await db.close()
            await doc_store.close_all_doc_stores()

    asyncio.run(_run())
