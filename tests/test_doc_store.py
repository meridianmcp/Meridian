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


# A .tex with in-text citations (fefb596a): one resolvable (knuth1984 -> a real
# \bibitem) and one dangling (missing_key -> no matching bib entry).
_CITE_TEX = r"""
\documentclass{article}
\begin{document}
\section{Introduction}
As shown \cite{knuth1984} and also \citep{missing_key}.
\section{Results}
Confirmed by \citet{lamport1994}.
\begin{thebibliography}{9}
\bibitem{knuth1984} Donald Knuth. The TeXbook. 1984.
\bibitem{lamport1994} Leslie Lamport. LaTeX. 1994.
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


def test_elements_from_latex_analysis_emits_citation_elements(tmp_path):
    """fefb596a: citation markers become kind='citation' elements whose
    parent_ordinal points at the enclosing section's heading element."""
    from meridian.latex_intel import analyze_latex
    analysis = analyze_latex(_CITE_TEX)
    elements = doc_store.elements_from_latex_analysis(analysis)
    by_kind: dict[str, list] = {}
    for e in elements:
        by_kind.setdefault(e["kind"], []).append(e)

    headings = by_kind["section"]
    citations = by_kind["citation"]
    biblio = by_kind["bibliography"]

    # Two sections: Introduction (ordinal 0), Results (ordinal 1).
    assert [h["text"] for h in headings] == ["Introduction", "Results"]
    intro_ord = headings[0]["ordinal"]
    results_ord = headings[1]["ordinal"]

    # Three citation elements: text = raw marker, ref = key, level None.
    assert {c["ref"] for c in citations} == {"knuth1984", "missing_key", "lamport1994"}
    assert all(c["level"] is None for c in citations)
    assert all(c["text"].startswith("\\cite") for c in citations)
    by_ref = {c["ref"]: c for c in citations}
    # knuth1984 + missing_key are in the Introduction section.
    assert by_ref["knuth1984"]["parent_ordinal"] == intro_ord
    assert by_ref["missing_key"]["parent_ordinal"] == intro_ord
    # lamport1994 is in the Results section.
    assert by_ref["lamport1994"]["parent_ordinal"] == results_ord

    # Bibliography still emitted (flat roots), keyed by citation key.
    assert {b["ref"] for b in biblio} == {"knuth1984", "lamport1994"}


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
# doc_edges — intra-document citation graph (fefb596a)
# ---------------------------------------------------------------------------

def test_put_document_materializes_resolved_and_dangling_citation_edges(tmp_path):
    """A citation whose key matches a local bib entry yields a resolved 'cites'
    edge; a citation with no matching bib entry stays a dangling element (no
    edge)."""
    async def _run():
        from meridian.latex_intel import analyze_latex
        store = await _open_store(tmp_path)
        try:
            analysis = analyze_latex(_CITE_TEX)
            elements = doc_store.elements_from_latex_analysis(analysis)
            doc = await store.put_document(
                "proj-1", "latex", elements, source="paper.tex",
            )

            edges = await store.get_edges("proj-1", document_id=doc["id"])
            # Two resolvable citations (knuth1984, lamport1994) -> two edges.
            # missing_key is dangling -> no edge.
            assert len(edges) == 2
            by_ref = {e["target_ref"]: e for e in edges}
            assert set(by_ref) == {"knuth1984", "lamport1994"}
            for edge in edges:
                assert edge["edge_kind"] == "cites"
                assert edge["target_kind"] == "bibentry"
                assert edge["target_element_id"] is not None
                assert edge["resolved_at"] is not None
                assert edge["target_document_id"] is None
                assert edge["project_id"] == "proj-1"

            # The dangling citation element is still stored (honest), just edge-less.
            struct = await store.get_structure("proj-1", "paper.tex")
            cite_refs = {
                e["ref"] for e in struct["elements"] if e["kind"] == "citation"
            }
            assert cite_refs == {"knuth1984", "missing_key", "lamport1994"}

            # Each edge's source element is the matching citation element; the
            # target element is the bibliography element with the same key.
            els_by_id = {e["id"]: e for e in struct["elements"]}
            for ref, edge in by_ref.items():
                src_el = els_by_id[edge["source_element_id"]]
                tgt_el = els_by_id[edge["target_element_id"]]
                assert src_el["kind"] == "citation" and src_el["ref"] == ref
                assert tgt_el["kind"] == "bibliography" and tgt_el["ref"] == ref

            # get_edges source_element_id filter narrows to one edge.
            knuth_src = next(
                e["id"] for e in struct["elements"]
                if e["kind"] == "citation" and e["ref"] == "knuth1984"
            )
            one = await store.get_edges("proj-1", source_element_id=knuth_src)
            assert len(one) == 1 and one[0]["target_ref"] == "knuth1984"
        finally:
            await store.close()

    asyncio.run(_run())


def test_put_document_case_insensitive_key_match(tmp_path):
    """Citation key matching a bib entry is case-insensitive exact."""
    async def _run():
        store = await _open_store(tmp_path)
        try:
            elements = [
                {"ordinal": 0, "level": 2, "kind": "section", "text": "S",
                 "ref": None, "parent_ordinal": None},
                {"ordinal": 1, "level": None, "kind": "citation",
                 "text": r"\cite{Knuth1984}", "ref": "Knuth1984",
                 "parent_ordinal": 0},
                {"ordinal": 2, "level": None, "kind": "bibliography",
                 "text": "Knuth", "ref": "knuth1984", "parent_ordinal": None},
            ]
            doc = await store.put_document(
                "proj-1", "latex", elements, source="c.tex",
            )
            edges = await store.get_edges("proj-1", document_id=doc["id"])
            assert len(edges) == 1
            assert edges[0]["target_ref"] == "Knuth1984"
            assert edges[0]["resolved_at"] is not None
        finally:
            await store.close()

    asyncio.run(_run())


def test_put_document_upsert_replaces_edges_no_orphans(tmp_path):
    """Re-storing the same source replaces edges — no duplicate/orphan rows."""
    async def _run():
        from meridian.latex_intel import analyze_latex
        store = await _open_store(tmp_path)
        try:
            elements = doc_store.elements_from_latex_analysis(
                analyze_latex(_CITE_TEX)
            )
            doc1 = await store.put_document(
                "proj-1", "latex", elements, source="paper.tex",
            )
            edges1 = await store.get_edges("proj-1", document_id=doc1["id"])
            assert len(edges1) == 2

            # Re-store the SAME source (upsert). Edges must be replaced, not
            # accumulated, and no orphan rows pointing at deleted elements remain.
            doc2 = await store.put_document(
                "proj-1", "latex", elements, source="paper.tex",
            )
            assert doc2["id"] == doc1["id"]

            # Exactly one document; exactly two edges (no duplicates).
            assert len(await store.list_documents("proj-1")) == 1
            edges2 = await store.get_edges("proj-1", document_id=doc2["id"])
            assert len(edges2) == 2

            # Every remaining edge's source element still exists (no orphans).
            struct = await store.get_structure("proj-1", "paper.tex")
            live_ids = {e["id"] for e in struct["elements"]}
            all_edges = await store.get_edges("proj-1")
            assert len(all_edges) == 2
            assert all(e["source_element_id"] in live_ids for e in all_edges)
        finally:
            await store.close()

    asyncio.run(_run())


def test_delete_document_removes_edges(tmp_path):
    """Deleting a document leaves no orphan edges."""
    async def _run():
        from meridian.latex_intel import analyze_latex
        store = await _open_store(tmp_path)
        try:
            elements = doc_store.elements_from_latex_analysis(
                analyze_latex(_CITE_TEX)
            )
            await store.put_document(
                "proj-1", "latex", elements, source="paper.tex",
            )
            assert len(await store.get_edges("proj-1")) == 2
            assert await store.delete_document("proj-1", "paper.tex") is True
            assert await store.get_edges("proj-1") == []
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


def test_ingest_tex_persists_citations_and_edges_end_to_end(tmp_path, monkeypatch):
    """fefb596a end-to-end: ingesting a .tex with in-text citations persists the
    citation elements AND materializes the resolved citation->bibentry edges via
    the default (tier-resolved) store, with no MCP/handler change required."""
    async def _run():
        from meridian import server as mh
        from meridian import doc_store as ds_mod

        ds_mod._reset_doc_store_cache()
        monkeypatch.delenv("MERIDIAN_DOC_STORE_URL", raising=False)
        db = await db_module.init_db(":memory:")
        try:
            proj = await db_module.create_project(db, "ingest-cites")
            tex_path = tmp_path / "paper.tex"
            tex_path.write_text(_CITE_TEX, encoding="utf-8")

            # .tex is not server-side EXTRACTED (only .txt/.md/.docx are), so the
            # supported ingest path passes the raw source as `content` alongside a
            # `.tex` file_path — the flat note is stored from content, and the
            # structure/citation persistence re-parses the .tex on disk.
            res = await mh._dispatch_mcp_tool(
                "ingest_document",
                {"project_id": proj["id"], "file_path": str(tex_path),
                 "content": _CITE_TEX, "title": "Paper"},
                db, str(tmp_path),
            )
            assert "error" not in res

            store = await ds_mod.open_doc_store_for(
                plan=None, hosted=False, data_dir=str(tmp_path),
                tenant_pg_url=None, override_url=None,
            )
            struct = await store.get_structure(proj["id"], str(tex_path))
            assert struct is not None
            cite_refs = {
                e["ref"] for e in struct["elements"] if e["kind"] == "citation"
            }
            assert cite_refs == {"knuth1984", "missing_key", "lamport1994"}

            edges = await store.get_edges(
                proj["id"], document_id=struct["document"]["id"]
            )
            assert {e["target_ref"] for e in edges} == {"knuth1984", "lamport1994"}
            assert all(e["edge_kind"] == "cites" for e in edges)
            assert all(e["resolved_at"] is not None for e in edges)
        finally:
            await db.close()
            await ds_mod.close_all_doc_stores()

    asyncio.run(_run())


def test_ingest_tex_survives_citation_path_failure(tmp_path, monkeypatch):
    """Best-effort: if the citation-emission path raises, ingest still succeeds
    (the flat kind='document' note is produced, no error surfaced)."""
    async def _run():
        from meridian import server as mh
        from meridian import doc_store as ds_mod

        ds_mod._reset_doc_store_cache()
        monkeypatch.delenv("MERIDIAN_DOC_STORE_URL", raising=False)

        # Force the latex element mapper to explode mid-ingest.
        def _boom(_analysis):
            raise RuntimeError("citation path down")

        monkeypatch.setattr(ds_mod, "elements_from_latex_analysis", _boom)
        db = await db_module.init_db(":memory:")
        try:
            proj = await db_module.create_project(db, "ingest-cite-guard")
            tex_path = tmp_path / "paper.tex"
            tex_path.write_text(_CITE_TEX, encoding="utf-8")

            res = await mh._dispatch_mcp_tool(
                "ingest_document",
                {"project_id": proj["id"], "file_path": str(tex_path),
                 "content": _CITE_TEX, "title": "Paper"},
                db, str(tmp_path),
            )
            # Ingest still produced the note — the citation-path failure is swallowed.
            assert "error" not in res
            assert res.get("id")
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


# ---------------------------------------------------------------------------
# 2412d29d — FULL-CHAIN integration: the real docx pipeline
# (docs_intel.document_outline -> elements_from_docx_outline -> put_document ->
# get_structure) round-trips on real .docx bytes, plus the branches that bite in
# production: empty document, malformed docx, upsert-by-source, missing source.
# Derived from an ad-hoc round-trip run first (proved it works on a real docx),
# then formalized here — not written in a vacuum.
# ---------------------------------------------------------------------------

from meridian.docs_intel import document_outline


def _docx_from_body(inner_xml: str) -> bytes:
    doc = (
        '<?xml version="1.0"?><w:document '
        'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml"><w:body>'
        + inner_xml +
        '</w:body></w:document>'
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", doc)
    return buf.getvalue()


def test_full_docx_chain_roundtrips_with_parent_edges(tmp_path):
    """The whole pipeline on real .docx bytes: parse -> map -> store -> read back,
    preserving heading order AND the structural parent edge (H2 under its H1)."""
    async def _run():
        outline = document_outline(_synthetic_docx())  # Intro(H1), Design(H2), Results(H1)
        elements = doc_store.elements_from_docx_outline(outline)
        store = await _open_store(tmp_path)
        await store.put_document("proj", "docx", elements, source="thesis.docx", title="Thesis")
        got = await store.get_structure("proj", "thesis.docx")
        assert got is not None
        texts = [e["text"] for e in got["elements"]]
        assert texts == ["Introduction", "Design", "Results"]
        by_text = {e["text"]: e for e in got["elements"]}
        # Design (H2) attaches under the preceding Introduction (H1); Results (H1) is a root.
        assert by_text["Design"]["parent_id"] == by_text["Introduction"]["id"]
        assert by_text["Results"]["parent_id"] is None
    asyncio.run(_run())


def test_docx_chain_empty_document_roundtrips(tmp_path):
    """A docx with body text but NO headings yields 0 elements; the document row
    still stores + reads back with an empty element list (not an error)."""
    async def _run():
        docx = _docx_from_body('<w:p w14:paraId="B1"><w:r><w:t>plain body, no headings</w:t></w:r></w:p>')
        outline = document_outline(docx)
        assert outline["heading_count"] == 0
        elements = doc_store.elements_from_docx_outline(outline)
        assert elements == []
        store = await _open_store(tmp_path)
        await store.put_document("proj", "docx", elements, source="empty.docx")
        got = await store.get_structure("proj", "empty.docx")
        assert got is not None and got["elements"] == []
    asyncio.run(_run())


def test_docx_chain_malformed_docx_fails_cleanly(tmp_path):
    """Malformed (non-zip) docx bytes make document_outline raise — the parse
    failure surfaces to the caller (guarded at the MCP layer, b43bab91), never a
    silent empty structure."""
    with pytest.raises(Exception):
        document_outline(b"this is not a zip file at all")


def test_docx_chain_upsert_by_source_replaces_structure(tmp_path):
    """Re-parsing the same source (a doc edited then re-ingested) upserts: stable
    document id, structure replaced — not a duplicate row nor a silent merge."""
    async def _run():
        store = await _open_store(tmp_path)
        first = doc_store.elements_from_docx_outline(document_outline(_synthetic_docx()))
        doc1 = await store.put_document("proj", "docx", first, source="paper.docx")
        # Re-ingest a shorter version of the same source.
        docx2 = _docx_from_body('<w:p w14:paraId="C1"><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>OnlyOne</w:t></w:r></w:p>')
        second = doc_store.elements_from_docx_outline(document_outline(docx2))
        doc2 = await store.put_document("proj", "docx", second, source="paper.docx")
        assert doc2["id"] == doc1["id"]  # stable id (upsert, not insert)
        got = await store.get_structure("proj", "paper.docx")
        assert [e["text"] for e in got["elements"]] == ["OnlyOne"]
    asyncio.run(_run())


def test_docx_chain_missing_source_returns_none(tmp_path):
    """get_structure for a source that was never stored returns None (not a crash),
    so a hosted/tunnel-unavailable read degrades cleanly."""
    async def _run():
        store = await _open_store(tmp_path)
        assert await store.get_structure("proj", "never-ingested.docx") is None
    asyncio.run(_run())
