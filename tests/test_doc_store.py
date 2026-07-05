"""Coverage for the tiered document-structure store (9ee6d2ec).

Exercises the pure tier resolver, the element mappers from real docs_intel /
latex_intel parse output, the full DocStructureStore round-trip on a local
SQLite sidecar (put/get/list/delete + upsert-by-source + parent edges), and the
best-effort/guarded wiring (ingest survives a store failure; the lifespan
tolerates a store-open failure).
"""
from __future__ import annotations

import asyncio
import io
import os
import zipfile

import pytest

from meridian import doc_store
from meridian import db as db_module


# ---------------------------------------------------------------------------
# Synthetic fixtures (a real .docx ZIP + a real .tex string)
# ---------------------------------------------------------------------------

_DOCUMENT_XML = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:p w14:paraId="0000A001">
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Introduction</w:t></w:r>
    </w:p>
    <w:p w14:paraId="0000A002">
      <w:pPr><w:pStyle w:val="Heading2"/></w:pPr>
      <w:r><w:t>Design</w:t></w:r>
    </w:p>
    <w:p w14:paraId="0000A003">
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Results</w:t></w:r>
    </w:p>
  </w:body>
</w:document>
"""

_SAMPLE_TEX = r"""
\documentclass{article}
\begin{document}
\section{Introduction}
\subsection{Design}
\section{Results}
\begin{thebibliography}{9}
\bibitem{knuth1984} Donald Knuth. The TeXbook. Addison-Wesley, 1984.
\end{thebibliography}
\end{document}
"""


def _synthetic_docx() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", _DOCUMENT_XML)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# resolve_doc_store_target — pure tier logic (no DB opened)
# ---------------------------------------------------------------------------

def test_resolve_target_pro_with_pg_url_uses_cloud_pg():
    url = "postgresql://user:pw@host/db"
    target, label = doc_store.resolve_doc_store_target(
        plan="pro", hosted=True, data_dir="/data",
        tenant_pg_url=url, override_url=None,
    )
    assert target == url
    assert label == "cloud_pg"


def test_resolve_target_admin_with_pg_url_uses_cloud_pg():
    url = "postgres://user:pw@host/db"
    target, label = doc_store.resolve_doc_store_target(
        plan="admin", hosted=True, data_dir="/data",
        tenant_pg_url=url, override_url=None,
    )
    assert (target, label) == (url, "cloud_pg")


def test_resolve_target_free_uses_local_sidecar():
    target, label = doc_store.resolve_doc_store_target(
        plan="free", hosted=True, data_dir=os.path.join("x", "data"),
        tenant_pg_url="postgresql://user:pw@host/db", override_url=None,
    )
    assert label == "local_sqlite"
    assert target.endswith("doc_structure.db")
    assert os.path.dirname(target) == os.path.join("x", "data")


def test_resolve_target_standard_uses_local_sidecar():
    target, label = doc_store.resolve_doc_store_target(
        plan="standard", hosted=True, data_dir="/data",
        tenant_pg_url="postgresql://user:pw@host/db", override_url=None,
    )
    assert label == "local_sqlite"


def test_resolve_target_selfhosted_pro_without_pg_url_uses_sidecar():
    # pro plan but NO cloud pg url available (self-hosted) -> local sidecar.
    target, label = doc_store.resolve_doc_store_target(
        plan="pro", hosted=False, data_dir="/data",
        tenant_pg_url=None, override_url=None,
    )
    assert label == "local_sqlite"


def test_resolve_target_pro_with_non_pg_url_uses_sidecar():
    # A sqlite path in tenant_pg_url is not a pg url -> falls back to sidecar.
    target, label = doc_store.resolve_doc_store_target(
        plan="pro", hosted=True, data_dir="/data",
        tenant_pg_url="/some/local.db", override_url=None,
    )
    assert label == "local_sqlite"


def test_resolve_target_selfhosted_pro_with_pg_url_uses_sidecar():
    # Self-hosted (hosted=False) has no billing tier: even a pro plan WITH a real
    # cloud pg url must NOT route to the cloud backend -> local sidecar. Guards
    # the `hosted` gate on the cloud_pg branch.
    target, label = doc_store.resolve_doc_store_target(
        plan="pro", hosted=False, data_dir="/data",
        tenant_pg_url="postgresql://user:pw@host/db", override_url=None,
    )
    assert label == "local_sqlite"


def test_resolve_target_override_always_wins():
    override = "postgresql://override/db"
    # Even for a free plan with no pg url, the override wins.
    target, label = doc_store.resolve_doc_store_target(
        plan="free", hosted=False, data_dir="/data",
        tenant_pg_url=None, override_url=override,
    )
    assert (target, label) == (override, "override")
    # And it also beats a would-be cloud_pg selection.
    target2, label2 = doc_store.resolve_doc_store_target(
        plan="pro", hosted=True, data_dir="/data",
        tenant_pg_url="postgresql://tenant/db", override_url=override,
    )
    assert (target2, label2) == (override, "override")


# ---------------------------------------------------------------------------
# Element mappers — real parse output -> nested elements
# ---------------------------------------------------------------------------

def test_elements_from_docx_outline_nests_by_heading_level():
    from meridian.docs_intel import document_outline
    outline = document_outline(_synthetic_docx())
    elements = doc_store.elements_from_docx_outline(outline)
    # Three headings: H1 Introduction, H2 Design (child of Introduction), H1 Results.
    assert [e["text"] for e in elements] == ["Introduction", "Design", "Results"]
    assert [e["kind"] for e in elements] == ["heading", "heading", "heading"]
    assert [e["level"] for e in elements] == [1, 2, 1]
    assert [e["ref"] for e in elements] == ["0000A001", "0000A002", "0000A003"]
    # Parent edges by ordinal: Introduction(root), Design->Introduction(0), Results(root).
    assert elements[0]["parent_ordinal"] is None
    assert elements[1]["parent_ordinal"] == 0
    assert elements[2]["parent_ordinal"] is None


def test_elements_from_latex_analysis_headings_and_bibliography():
    from meridian.latex_intel import analyze_latex
    analysis = analyze_latex(_SAMPLE_TEX)
    elements = doc_store.elements_from_latex_analysis(analysis)
    headings = [e for e in elements if e["kind"] != "bibliography"]
    biblio = [e for e in elements if e["kind"] == "bibliography"]
    assert [e["text"] for e in headings] == ["Introduction", "Design", "Results"]
    assert [e["kind"] for e in headings] == ["section", "subsection", "section"]
    # Design (subsection, level 3) nests under Introduction (section, level 2).
    assert headings[1]["parent_ordinal"] == 0
    assert headings[2]["parent_ordinal"] is None
    # Bibliography appended as a flat root element keyed by citation key.
    assert len(biblio) == 1
    assert biblio[0]["ref"] == "knuth1984"
    assert biblio[0]["parent_ordinal"] is None


def test_elements_from_empty_outline_is_empty():
    assert doc_store.elements_from_docx_outline({}) == []
    assert doc_store.elements_from_latex_analysis({}) == []


# ---------------------------------------------------------------------------
# DocStructureStore round-trip on a local SQLite sidecar
# ---------------------------------------------------------------------------

async def _open_store(tmp_path) -> doc_store.DocStructureStore:
    conn = await db_module.init_db(str(tmp_path / "doc_structure.db"))
    store = doc_store.DocStructureStore(conn)
    await store.ensure_schema()
    return store


def test_store_put_get_structure_roundtrip(tmp_path):
    async def _run():
        store = await _open_store(tmp_path)
        try:
            elements = [
                {"ordinal": 0, "level": 1, "kind": "heading", "text": "Intro",
                 "ref": "p1", "parent_ordinal": None},
                {"ordinal": 1, "level": 2, "kind": "heading", "text": "Design",
                 "ref": "p2", "parent_ordinal": 0},
                {"ordinal": 2, "level": 1, "kind": "heading", "text": "Results",
                 "ref": "p3", "parent_ordinal": None},
            ]
            doc = await store.put_document(
                "proj-1", "docx", elements, source="a.docx", title="A",
            )
            assert doc["element_count"] == 3
            assert doc["doc_type"] == "docx"
            assert doc["source"] == "a.docx"
            assert doc["content_hash"]

            struct = await store.get_structure("proj-1", "a.docx")
            assert struct is not None
            els = struct["elements"]
            # Ordered by ordinal.
            assert [e["ordinal"] for e in els] == [0, 1, 2]
            assert [e["text"] for e in els] == ["Intro", "Design", "Results"]
            # Real stored parent edge: Design.parent_id == Intro.id.
            by_text = {e["text"]: e for e in els}
            assert by_text["Design"]["parent_id"] == by_text["Intro"]["id"]
            assert by_text["Intro"]["parent_id"] is None
            assert by_text["Results"]["parent_id"] is None
        finally:
            await store.close()

    asyncio.run(_run())


def test_store_upsert_by_source_replaces_elements(tmp_path):
    async def _run():
        store = await _open_store(tmp_path)
        try:
            v1 = [
                {"ordinal": 0, "level": 1, "kind": "heading", "text": "Old",
                 "ref": "p1", "parent_ordinal": None},
                {"ordinal": 1, "level": 1, "kind": "heading", "text": "Old2",
                 "ref": "p2", "parent_ordinal": None},
            ]
            doc1 = await store.put_document("proj-1", "docx", v1, source="a.docx")
            # Re-store the same source with a different structure.
            v2 = [
                {"ordinal": 0, "level": 1, "kind": "heading", "text": "New",
                 "ref": "p9", "parent_ordinal": None},
            ]
            doc2 = await store.put_document("proj-1", "docx", v2, source="a.docx")

            # Exactly one document for the source (no duplicate).
            docs = await store.list_documents("proj-1")
            assert len(docs) == 1
            assert docs[0]["element_count"] == 1
            # Upsert keeps the same document id (stable identity).
            assert doc2["id"] == doc1["id"]

            struct = await store.get_structure("proj-1", "a.docx")
            assert [e["text"] for e in struct["elements"]] == ["New"]
        finally:
            await store.close()

    asyncio.run(_run())


def test_store_anonymous_puts_do_not_merge(tmp_path):
    async def _run():
        store = await _open_store(tmp_path)
        try:
            e = [{"ordinal": 0, "level": 1, "kind": "heading", "text": "X",
                  "ref": None, "parent_ordinal": None}]
            d1 = await store.put_document("proj-1", "docx", e, source=None)
            d2 = await store.put_document("proj-1", "docx", e, source=None)
            assert d1["id"] != d2["id"]
            docs = await store.list_documents("proj-1")
            assert len(docs) == 2
        finally:
            await store.close()

    asyncio.run(_run())


def test_store_list_and_delete_and_missing(tmp_path):
    async def _run():
        store = await _open_store(tmp_path)
        try:
            e = [{"ordinal": 0, "level": 1, "kind": "heading", "text": "X",
                  "ref": "p1", "parent_ordinal": None}]
            await store.put_document("proj-1", "docx", e, source="a.docx")
            await store.put_document("proj-1", "latex", e, source="b.tex")

            docs = await store.list_documents("proj-1")
            assert {d["source"] for d in docs} == {"a.docx", "b.tex"}

            # get on missing -> None.
            assert await store.get_document("proj-1", "nope.docx") is None
            assert await store.get_structure("proj-1", "nope.docx") is None

            # delete removes the doc + its elements.
            assert await store.delete_document("proj-1", "a.docx") is True
            assert await store.get_document("proj-1", "a.docx") is None
            assert await store.delete_document("proj-1", "a.docx") is False
            remaining = await store.list_documents("proj-1")
            assert [d["source"] for d in remaining] == ["b.tex"]
        finally:
            await store.close()

    asyncio.run(_run())


def test_store_persists_real_docx_outline_end_to_end(tmp_path):
    async def _run():
        from meridian.docs_intel import document_outline
        store = await _open_store(tmp_path)
        try:
            outline = document_outline(_synthetic_docx())
            elements = doc_store.elements_from_docx_outline(outline)
            doc = await store.put_document(
                "proj-1", "docx", elements, source="thesis.docx",
            )
            assert doc["element_count"] == 3
            struct = await store.get_structure("proj-1", "thesis.docx")
            by_text = {e["text"]: e for e in struct["elements"]}
            # Design (H2) is stored under Introduction (H1).
            assert by_text["Design"]["parent_id"] == by_text["Introduction"]["id"]
        finally:
            await store.close()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# open_doc_store_for — factory + cache
# ---------------------------------------------------------------------------

def test_open_doc_store_for_caches_by_target(tmp_path, monkeypatch):
    async def _run():
        doc_store._reset_doc_store_cache()
        try:
            s1 = await doc_store.open_doc_store_for(
                plan="free", hosted=False, data_dir=str(tmp_path),
                tenant_pg_url=None, override_url=None,
            )
            s2 = await doc_store.open_doc_store_for(
                plan="free", hosted=False, data_dir=str(tmp_path),
                tenant_pg_url=None, override_url=None,
            )
            # Same resolved target -> same cached store instance.
            assert s1 is s2
            # It works end-to-end.
            e = [{"ordinal": 0, "level": 1, "kind": "heading", "text": "H",
                  "ref": None, "parent_ordinal": None}]
            await s1.put_document("p", "docx", e, source="x.docx")
            assert (await s2.get_document("p", "x.docx"))["element_count"] == 1
        finally:
            await doc_store.close_all_doc_stores()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Guarded wiring — ingest survives a store failure; lifespan tolerates open fail
# ---------------------------------------------------------------------------

def test_ingest_succeeds_even_if_structure_store_raises(tmp_path, monkeypatch):
    """The docx STRUCTURE persistence is best-effort: if the store blows up, the
    flat-note ingest must still succeed and return normally."""
    async def _run():
        from meridian import server as mh
        from meridian import doc_store as ds_mod

        db = await db_module.init_db(":memory:")
        try:
            proj = await db_module.create_project(db, "ingest-guard")
            docx_path = tmp_path / "chapter.docx"
            docx_path.write_bytes(_synthetic_docx())

            # Force the structure store to explode on open.
            async def _boom(**_kwargs):
                raise RuntimeError("store backend down")

            monkeypatch.setattr(ds_mod, "open_doc_store_for", _boom)
            monkeypatch.setattr(mh.app.state, "doc_store", None, raising=False)

            res = await mh._dispatch_mcp_tool(
                "ingest_document",
                {"project_id": proj["id"], "file_path": str(docx_path),
                 "title": "Chapter"},
                db, str(tmp_path),
            )
            # Ingest still produced the kind='document' note — no error surfaced.
            assert "error" not in res
            assert res.get("id")
        finally:
            await db.close()

    asyncio.run(_run())


def test_ingest_persists_structure_into_default_store(tmp_path, monkeypatch):
    """Happy path: ingesting a .docx also persists its structure so it is later
    retrievable via the store."""
    async def _run():
        from meridian import server as mh
        from meridian import doc_store as ds_mod

        ds_mod._reset_doc_store_cache()
        monkeypatch.delenv("MERIDIAN_DOC_STORE_URL", raising=False)
        db = await db_module.init_db(":memory:")
        try:
            proj = await db_module.create_project(db, "ingest-persist")
            docx_path = tmp_path / "chapter.docx"
            docx_path.write_bytes(_synthetic_docx())

            res = await mh._dispatch_mcp_tool(
                "ingest_document",
                {"project_id": proj["id"], "file_path": str(docx_path),
                 "title": "Chapter"},
                db, str(tmp_path),
            )
            assert "error" not in res

            # The structure landed in the tier-resolved (local sidecar) store.
            store = await ds_mod.open_doc_store_for(
                plan=None, hosted=False, data_dir=str(tmp_path),
                tenant_pg_url=None, override_url=None,
            )
            struct = await store.get_structure(proj["id"], str(docx_path))
            assert struct is not None
            assert [e["text"] for e in struct["elements"]] == [
                "Introduction", "Design", "Results",
            ]
        finally:
            await db.close()
            await ds_mod.close_all_doc_stores()

    asyncio.run(_run())


def test_ingest_skips_structure_for_plain_text(tmp_path, monkeypatch):
    """A non-docx/tex ingest (plain content) must not touch the structure store."""
    async def _run():
        from meridian import server as mh
        from meridian import doc_store as ds_mod

        called = {"n": 0}

        async def _tracker(**_kwargs):
            called["n"] += 1
            raise AssertionError("should not resolve a store for plain text")

        monkeypatch.setattr(ds_mod, "open_doc_store_for", _tracker)
        db = await db_module.init_db(":memory:")
        try:
            proj = await db_module.create_project(db, "ingest-plain")
            res = await mh._dispatch_mcp_tool(
                "ingest_document",
                {"project_id": proj["id"], "content": "hello world",
                 "title": "note", "source": "s://x"},
                db, str(tmp_path),
            )
            assert "error" not in res
            assert called["n"] == 0
        finally:
            await db.close()

    asyncio.run(_run())


def test_lifespan_tolerates_doc_store_open_failure(tmp_path, monkeypatch):
    """If the default store cannot open at startup, the server still boots and
    app.state.doc_store is None (no crash)."""
    import importlib
    from fastapi.testclient import TestClient
    from meridian import doc_store as ds_mod

    async def _boom(**_kwargs):
        raise RuntimeError("cannot open sidecar")

    monkeypatch.setenv("MERIDIAN_DB", ":memory:")
    monkeypatch.setenv("MERIDIAN_DB_URL", "")
    monkeypatch.setenv("MERIDIAN_DEMO_DB_URL", "")
    monkeypatch.setenv("MERIDIAN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MERIDIAN_GOAL_MD", str(tmp_path / "GOAL.md"))
    monkeypatch.setenv("MERIDIAN_MD_ROOT", str(tmp_path))
    monkeypatch.setenv("MERIDIAN_SKIP_DEMO", "1")
    monkeypatch.setattr(ds_mod, "open_doc_store_for", _boom)

    # Fresh import so the lifespan picks up the env vars cleanly (mirrors the
    # conftest `client` fixture) — avoids stale global cache bleed from prior tests.
    import meridian.server as server_module
    server_module = importlib.reload(server_module)

    with TestClient(server_module.app) as client:
        assert getattr(client.app.state, "doc_store", "MISSING") is None
        # Server is otherwise healthy.
        assert client.get("/health").status_code == 200
