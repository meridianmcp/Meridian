"""Coverage for Word equation (OMML) indexing + figure/table elements (06df6ab3).

Exercises:

* the pure LaTeX/MathML/OMML helpers (normalize_latex, latex_to_omml,
  _omml_flatten_text, _equation_similarity) without a database,
* parse_docx_equations reading real <m:oMath> straight out of a synthetic .docx
  ZIP via lxml,
* elements_from_docx_content_tree building kind='figure'/'table' doc_elements
  nested "by section" alongside headings,
* DocStructureStore's equation methods (put_equations/get_equations/
  find_similar_equations/add_equation) end to end on a local SQLite sidecar,
  including the advisory near-duplicate surface,
* reindex_document as the one orchestrator tying outline+figures/tables and
  equations together,
* the two new MCP tools (index_equation, find_similar_equation) through the
  real _dispatch_mcp_tool path.
"""
from __future__ import annotations

import asyncio
import io
import zipfile

import pytest

from meridian import doc_store
from meridian import db as db_module


# ---------------------------------------------------------------------------
# Synthetic .docx fixtures
# ---------------------------------------------------------------------------

_EQUATION_DOCUMENT_XML = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml"
    xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math">
  <w:body>
    <w:p w14:paraId="0000B001">
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Physics</w:t></w:r>
    </w:p>
    <w:p w14:paraId="0000B002">
      <w:r><w:t>Einstein: </w:t></w:r>
      <m:oMath>
        <m:r><m:t>E</m:t></m:r>
        <m:r><m:t>=</m:t></m:r>
        <m:r><m:t>m</m:t></m:r>
        <m:sSup><m:e><m:r><m:t>c</m:t></m:r></m:e><m:sup><m:r><m:t>2</m:t></m:r></m:sup></m:sSup>
      </m:oMath>
    </w:p>
    <w:p w14:paraId="0000B003">
      <w:r><w:t>Newton: </w:t></w:r>
      <m:oMath>
        <m:r><m:t>F</m:t></m:r>
        <m:r><m:t>=</m:t></m:r>
        <m:r><m:t>m</m:t></m:r>
        <m:r><m:t>a</m:t></m:r>
      </m:oMath>
    </w:p>
  </w:body>
</w:document>
"""


_FIGURE_TABLE_DOCUMENT_XML = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:p w14:paraId="0000C001">
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Results</w:t></w:r>
    </w:p>
    <w:tbl>
      <w:tr><w:tc><w:p><w:r><w:t>H1</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>H2</w:t></w:r></w:p></w:tc></w:tr>
      <w:tr><w:tc><w:p><w:r><w:t>a</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>b</w:t></w:r></w:p></w:tc></w:tr>
    </w:tbl>
    <w:p>
      <w:fldSimple w:instr="SEQ Table \\* ARABIC"><w:r><w:t>1</w:t></w:r></w:fldSimple>
      <w:r><w:t>: Results table</w:t></w:r>
    </w:p>
    <w:p>
      <w:fldSimple w:instr="SEQ Figure \\* ARABIC"><w:r><w:t>1</w:t></w:r></w:fldSimple>
      <w:r><w:t>: A diagram of the setup</w:t></w:r>
    </w:p>
    <w:p w14:paraId="0000C002">
      <w:pPr><w:pStyle w:val="Heading2"/></w:pPr>
      <w:r><w:t>Discussion</w:t></w:r>
    </w:p>
    <w:p><w:r><w:t>Plain paragraph — not persisted as an element.</w:t></w:r></w:p>
  </w:body>
</w:document>
"""


def _zip_docx(xml: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", xml)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Pure helpers — normalize_latex / latex_to_omml / _omml_flatten_text
# ---------------------------------------------------------------------------

def test_normalize_latex_strips_delimiters_and_all_whitespace():
    assert doc_store.normalize_latex("  $x^2 +   y^2$  ") == "x^2+y^2"
    assert doc_store.normalize_latex(r"\(a+b\)") == "a+b"
    assert doc_store.normalize_latex(r"\[a+b\]") == "a+b"
    assert doc_store.normalize_latex("$$a+b$$") == "a+b"
    assert doc_store.normalize_latex("bare x") == "barex"
    assert doc_store.normalize_latex(None) == ""
    assert doc_store.normalize_latex("   ") == ""
    # Whitespace is semantically insignificant in LaTeX — these normalize identically.
    assert doc_store.normalize_latex("E=mc^2") == doc_store.normalize_latex("E = m c^2")


def test_latex_to_omml_converts_common_constructs():
    omml = doc_store.latex_to_omml(r"\frac{a}{b}")
    assert omml is not None
    assert "<m:f>" in omml and "<m:num>" in omml and "<m:den>" in omml

    omml_sup = doc_store.latex_to_omml("x^2")
    assert "<m:sSup>" in omml_sup

    omml_sub = doc_store.latex_to_omml("x_1")
    assert "<m:sSub>" in omml_sub

    omml_sqrt = doc_store.latex_to_omml(r"\sqrt{x}")
    assert "<m:rad>" in omml_sqrt

    # Round-trips through the flattening helper for the fuzzy-dedup key.
    flat = doc_store._omml_flatten_text(omml)
    assert flat == "ab"


def test_latex_to_omml_returns_none_on_blank_or_unconvertible_input():
    assert doc_store.latex_to_omml("") is None
    assert doc_store.latex_to_omml(None) is None
    assert doc_store.latex_to_omml("   ") is None


def test_latex_to_omml_guarded_against_latex2mathml_failure(monkeypatch):
    """A latex2mathml exception must degrade to None, never raise."""
    import latex2mathml.converter as l2m

    def _boom(_latex):
        raise RuntimeError("bad latex")

    monkeypatch.setattr(l2m, "convert", _boom)
    assert doc_store.latex_to_omml("x^2") is None


def test_mathml_unrecognized_construct_degrades_to_literal_text():
    """An mtable (matrices) has no dedicated mapping — it must still convert
    (as a flattened literal run) rather than raising."""
    from lxml import etree as LET

    mathml = (
        '<math xmlns="http://www.w3.org/1998/Math/MathML">'
        "<mtable><mtr><mtd><mn>1</mn></mtd></mtr></mtable>"
        "</math>"
    )
    root = LET.fromstring(mathml.encode("utf-8"))
    omath = doc_store._mathml_to_omml(root)
    out = LET.tostring(omath, encoding="unicode")
    assert "<m:oMath" in out
    assert "1" in doc_store._omml_flatten_text(out)


def test_omml_flatten_text_handles_blank_and_malformed_input():
    assert doc_store._omml_flatten_text(None) == ""
    assert doc_store._omml_flatten_text("") == ""
    assert doc_store._omml_flatten_text("<not-xml") == ""


# ---------------------------------------------------------------------------
# parse_docx_equations — reading <m:oMath> via lxml (never a PDF round-trip)
# ---------------------------------------------------------------------------

def test_parse_docx_equations_reads_real_omml():
    data = _zip_docx(_EQUATION_DOCUMENT_XML)
    equations = doc_store.parse_docx_equations(data)
    assert len(equations) == 2
    assert equations[0]["ordinal"] == 0
    assert equations[0]["element_id"] == "0000B002"
    assert doc_store._omml_flatten_text(equations[0]["omml_raw"]) == "E=mc2"
    assert equations[1]["element_id"] == "0000B003"
    assert doc_store._omml_flatten_text(equations[1]["omml_raw"]) == "F=ma"


def test_parse_docx_equations_empty_document_is_empty():
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body></w:body></w:document>"
    )
    assert doc_store.parse_docx_equations(_zip_docx(xml)) == []


# ---------------------------------------------------------------------------
# elements_from_docx_content_tree — figures/tables as doc_elements
# ---------------------------------------------------------------------------

def test_elements_from_docx_content_tree_figures_tables_and_headings():
    from docparse.docs_intel import document_content_tree

    data = _zip_docx(_FIGURE_TABLE_DOCUMENT_XML)
    tree = document_content_tree(data)
    elements = doc_store.elements_from_docx_content_tree(tree)

    kinds = [e["kind"] for e in elements]
    assert kinds == ["heading", "table", "figure", "heading"]

    by_kind = {}
    for e in elements:
        by_kind.setdefault(e["kind"], []).append(e)

    heading0 = by_kind["heading"][0]
    assert heading0["text"] == "Results"
    assert heading0["parent_ordinal"] is None

    table = by_kind["table"][0]
    assert table["text"] == "H1 | H2\na | b"
    assert table["ref"] == "1: Results table"
    assert table["parent_ordinal"] == heading0["ordinal"]

    figure = by_kind["figure"][0]
    assert figure["ref"] == "1: A diagram of the setup"
    assert figure["parent_ordinal"] == heading0["ordinal"]

    heading1 = by_kind["heading"][1]
    assert heading1["text"] == "Discussion"
    # Discussion is a Heading2 — nests UNDER the enclosing Heading1 (Results).
    assert heading1["parent_ordinal"] == heading0["ordinal"]

    # The plain trailing paragraph is NOT persisted as an element.
    assert len(elements) == 4


def test_elements_from_docx_content_tree_empty_tree_is_empty():
    assert doc_store.elements_from_docx_content_tree({}) == []
    assert doc_store.elements_from_docx_content_tree({"blocks": []}) == []


# ---------------------------------------------------------------------------
# DocStructureStore equation methods — full round-trip on a local sidecar
# ---------------------------------------------------------------------------

async def _open_store(tmp_path) -> doc_store.DocStructureStore:
    conn = await db_module.init_db(str(tmp_path / "doc_structure.db"))
    store = doc_store.DocStructureStore(conn)
    await store.ensure_schema()
    return store


def test_put_equations_inserts_and_get_equations_orders_by_ordinal(tmp_path):
    async def _run():
        store = await _open_store(tmp_path)
        try:
            doc = await store.put_document("proj-1", "docx", [], source="a.docx")
            result = await store.put_equations(doc["id"], [
                {"latex": "x^2 + y^2 = z^2", "semantic_label": "Pythagoras"},
                {"latex": r"\frac{a}{b}"},
            ])
            assert len(result["inserted"]) == 2
            assert result["near_duplicates"] == []

            stored = await store.get_equations(doc["id"])
            assert [e["ordinal"] for e in stored] == [0, 1]
            assert stored[0]["semantic_label"] == "Pythagoras"
            assert stored[0]["latex_normalized"] == "x^2+y^2=z^2"
            assert "<m:sSup>" in stored[0]["omml_raw"]
        finally:
            await store.close()

    asyncio.run(_run())


def test_put_equations_surfaces_near_duplicate_without_skipping_insert(tmp_path):
    """A near-duplicate LaTeX spelling is STILL inserted (never silently
    dropped) but flagged via near_duplicates (06df6ab3 dedup contract)."""
    async def _run():
        store = await _open_store(tmp_path)
        try:
            doc = await store.put_document("proj-1", "docx", [], source="a.docx")
            await store.put_equations(doc["id"], [{"latex": "E=mc^2"}])
            result = await store.put_equations(doc["id"], [{"latex": "E = m c^2"}])

            assert len(result["inserted"]) == 1  # still inserted
            assert len(result["near_duplicates"]) == 1
            dup = result["near_duplicates"][0]
            assert dup["equation_id"] == result["inserted"][0]["id"]
            assert dup["score"] >= 0.85

            all_eqs = await store.get_equations(doc["id"])
            assert len(all_eqs) == 2  # both rows persist
        finally:
            await store.close()

    asyncio.run(_run())


def test_put_equations_dedupes_within_same_batch(tmp_path):
    """Two near-identical equations in the SAME put_equations call also dedupe
    against each other (the batch's own earlier rows), not just prior stores."""
    async def _run():
        store = await _open_store(tmp_path)
        try:
            doc = await store.put_document("proj-1", "docx", [], source="a.docx")
            result = await store.put_equations(doc["id"], [
                {"latex": "a^2 + b^2 = c^2"},
                {"latex": "a^2+b^2=c^2"},
            ])
            assert len(result["inserted"]) == 2
            assert len(result["near_duplicates"]) == 1
            assert result["near_duplicates"][0]["matched_id"] == result["inserted"][0]["id"]
        finally:
            await store.close()

    asyncio.run(_run())


def test_put_equations_from_omml_raw_uses_flattened_text_as_dedup_key(tmp_path):
    async def _run():
        store = await _open_store(tmp_path)
        try:
            doc = await store.put_document("proj-1", "docx", [], source="a.docx")
            omml = '<m:oMath xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math"><m:r><m:t>E</m:t></m:r><m:r><m:t>=</m:t></m:r><m:r><m:t>mc2</m:t></m:r></m:oMath>'
            result = await store.put_equations(doc["id"], [{"omml_raw": omml}])
            assert result["inserted"][0]["latex_normalized"] == "E=mc2"
            assert result["inserted"][0]["omml_raw"] == omml
        finally:
            await store.close()

    asyncio.run(_run())


def test_find_similar_equations_ranks_by_score_and_respects_limit(tmp_path):
    async def _run():
        store = await _open_store(tmp_path)
        try:
            doc = await store.put_document("proj-1", "docx", [], source="a.docx")
            await store.put_equations(doc["id"], [
                {"latex": "E=mc^2"},
                {"latex": r"\frac{a}{b}"},
                {"latex": "F=ma"},
            ])
            matches = await store.find_similar_equations(doc["id"], "E = m c^2", limit=2)
            assert len(matches) == 2
            assert matches[0]["latex_normalized"] == "E=mc^2"
            assert matches[0]["score"] > matches[1]["score"]

            # Unknown document (no equations) yields an empty list, not an error.
            empty = await store.find_similar_equations("no-such-doc", "x")
            assert empty == []
        finally:
            await store.close()

    asyncio.run(_run())


def test_add_equation_detects_omml_vs_latex_and_returns_shape(tmp_path):
    async def _run():
        store = await _open_store(tmp_path)
        try:
            doc = await store.put_document("proj-1", "docx", [], source="a.docx")

            latex_result = await store.add_equation(doc["id"], "x^2", semantic_label="square")
            assert latex_result["equation"]["latex_normalized"] == "x^2"
            assert latex_result["equation"]["semantic_label"] == "square"
            assert latex_result["near_duplicates"] == []

            omml = '<m:oMath xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math"><m:r><m:t>y^2</m:t></m:r></m:oMath>'
            omml_result = await store.add_equation(doc["id"], omml)
            assert omml_result["equation"]["omml_raw"] == omml
        finally:
            await store.close()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# reindex_document — the one orchestrator entry point
# ---------------------------------------------------------------------------

def test_reindex_document_ties_outline_figures_tables_and_equations_together(tmp_path):
    async def _run():
        docx_path = tmp_path / "sample.docx"
        docx_path.write_bytes(_zip_docx(_EQUATION_DOCUMENT_XML))

        store = await _open_store(tmp_path)
        try:
            result = await store.reindex_document(
                "proj-1", str(docx_path), source="sample.docx", title="Sample",
            )
            assert result["document"]["source"] == "sample.docx"
            assert result["document"]["doc_type"] == "docx"
            assert result["elements_count"] == 1  # one heading ("Physics")
            assert len(result["equations"]["inserted"]) == 2

            struct = await store.get_structure("proj-1", "sample.docx")
            assert [e["text"] for e in struct["elements"]] == ["Physics"]

            eqs = await store.get_equations(result["document"]["id"])
            assert [e["element_id"] for e in eqs] == ["0000B002", "0000B003"]
        finally:
            await store.close()

    asyncio.run(_run())


def test_reindex_document_upsert_replaces_elements_but_equations_accumulate(tmp_path):
    """Re-running reindex_document on the SAME source upserts doc/elements (like
    put_document always has) while put_equations keeps its own additive/dedup
    contract for the equations pass."""
    async def _run():
        docx_path = tmp_path / "sample.docx"
        docx_path.write_bytes(_zip_docx(_EQUATION_DOCUMENT_XML))

        store = await _open_store(tmp_path)
        try:
            first = await store.reindex_document("proj-1", str(docx_path), source="sample.docx")
            second = await store.reindex_document("proj-1", str(docx_path), source="sample.docx")
            assert second["document"]["id"] == first["document"]["id"]

            docs = await store.list_documents("proj-1")
            assert len(docs) == 1  # no duplicate document row

            # Equations re-parsed on the second pass are near-duplicates of the
            # first pass's rows (same source docx) — surfaced, not silently lost.
            assert len(second["equations"]["near_duplicates"]) == 2
        finally:
            await store.close()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# MCP tools — index_equation + find_similar_equation
# ---------------------------------------------------------------------------

def _seed_store_via_env(tmp_path, monkeypatch):
    sidecar = str(tmp_path / "mcp_doc_structure.db")
    monkeypatch.setenv("MERIDIAN_DOC_STORE_URL", sidecar)
    doc_store._reset_doc_store_cache()
    return sidecar


def test_mcp_index_equation_and_find_similar_equation_round_trip(tmp_path, monkeypatch):
    async def _run():
        from meridian import server as mh

        sidecar = _seed_store_via_env(tmp_path, monkeypatch)
        db = await db_module.init_db(":memory:")
        try:
            proj = await db_module.create_project(db, "eq-proj")
            pid = proj["id"]

            seed = await doc_store.open_doc_store_for(
                plan=None, hosted=False, data_dir=str(tmp_path),
                tenant_pg_url=None, override_url=sidecar,
            )
            await seed.put_document(pid, "docx", [], source="chapter1.docx")

            res = await mh._dispatch_mcp_tool(
                "index_equation",
                {"project_id": pid, "doc": "chapter1.docx",
                 "omml_or_latex": "E=mc^2", "semantic_label": "mass-energy"},
                db, str(tmp_path),
            )
            assert "error" not in res
            assert res["equation"]["semantic_label"] == "mass-energy"
            assert res["near_duplicates"] == []

            find_res = await mh._dispatch_mcp_tool(
                "find_similar_equation",
                {"project_id": pid, "doc": "chapter1.docx", "latex": "E = m c^2"},
                db, str(tmp_path),
            )
            assert "error" not in find_res
            assert find_res["matches"][0]["semantic_label"] == "mass-energy"
            assert find_res["matches"][0]["score"] > 0.5

            # A near-duplicate insert is surfaced, not silently dropped.
            dup_res = await mh._dispatch_mcp_tool(
                "index_equation",
                {"project_id": pid, "doc": "chapter1.docx", "omml_or_latex": "E = mc^2"},
                db, str(tmp_path),
            )
            assert len(dup_res["near_duplicates"]) == 1
        finally:
            await db.close()
            await doc_store.close_all_doc_stores()

    asyncio.run(_run())


def test_mcp_index_equation_requires_project_id_doc_and_payload(tmp_path, monkeypatch):
    async def _run():
        from meridian import server as mh

        _seed_store_via_env(tmp_path, monkeypatch)
        db = await db_module.init_db(":memory:")
        try:
            assert (await mh._dispatch_mcp_tool(
                "index_equation", {}, db, str(tmp_path),
            )).get("error")
            assert (await mh._dispatch_mcp_tool(
                "index_equation", {"project_id": "p"}, db, str(tmp_path),
            )).get("error")
            assert (await mh._dispatch_mcp_tool(
                "index_equation", {"project_id": "p", "doc": "x.docx"}, db, str(tmp_path),
            )).get("error")
        finally:
            await db.close()
            await doc_store.close_all_doc_stores()

    asyncio.run(_run())


def test_mcp_index_equation_unknown_doc_returns_helpful_error(tmp_path, monkeypatch):
    async def _run():
        from meridian import server as mh

        _seed_store_via_env(tmp_path, monkeypatch)
        db = await db_module.init_db(":memory:")
        try:
            proj = await db_module.create_project(db, "eq-proj-2")
            res = await mh._dispatch_mcp_tool(
                "index_equation",
                {"project_id": proj["id"], "doc": "never-ingested.docx", "omml_or_latex": "x^2"},
                db, str(tmp_path),
            )
            assert "error" in res
            assert "never-ingested.docx" in res["error"]
        finally:
            await db.close()
            await doc_store.close_all_doc_stores()

    asyncio.run(_run())


def test_mcp_find_similar_equation_unknown_doc_returns_empty_matches(tmp_path, monkeypatch):
    async def _run():
        from meridian import server as mh

        _seed_store_via_env(tmp_path, monkeypatch)
        db = await db_module.init_db(":memory:")
        try:
            proj = await db_module.create_project(db, "eq-proj-3")
            res = await mh._dispatch_mcp_tool(
                "find_similar_equation",
                {"project_id": proj["id"], "doc": "never-ingested.docx", "latex": "x^2"},
                db, str(tmp_path),
            )
            assert "error" not in res
            assert res["matches"] == []
            assert res["document_id"] is None
        finally:
            await db.close()
            await doc_store.close_all_doc_stores()

    asyncio.run(_run())


def test_mcp_find_similar_equation_requires_project_id_doc_and_latex(tmp_path, monkeypatch):
    async def _run():
        from meridian import server as mh

        _seed_store_via_env(tmp_path, monkeypatch)
        db = await db_module.init_db(":memory:")
        try:
            assert (await mh._dispatch_mcp_tool(
                "find_similar_equation", {}, db, str(tmp_path),
            )).get("error")
            assert (await mh._dispatch_mcp_tool(
                "find_similar_equation", {"project_id": "p", "doc": "x.docx"}, db, str(tmp_path),
            )).get("error")
        finally:
            await db.close()
            await doc_store.close_all_doc_stores()

    asyncio.run(_run())
