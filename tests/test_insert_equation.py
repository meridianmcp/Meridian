"""Coverage for insert_equation — direct OMML write-back into a .docx (51a595e7).

Exercises:

* the reusable docx-write helpers (_load_docx_xml / _find_paragraph_by_id /
  _save_docx_xml / _insert_omath_at_position) without a database,
* DocStructureStore.insert_equation end to end on a local SQLite sidecar: the
  equation is physically written into the source .docx AND the equation index
  resyncs, across the append / before / after positions and the LaTeX-inline vs
  existing-equation-id resolution paths,
* resync_document_equations as a standalone targeted resync,
* the bad-para_id / unresolvable-equation / unknown-doc / missing-file error
  paths (the file is never mutated on a resolution failure),
* the insert_equation MCP tool through the real _dispatch_mcp_tool path,
  including its required-arg validation.
"""
from __future__ import annotations

import asyncio
import io
import zipfile

import pytest

from lxml import etree as LET

from meridian import doc_store
from meridian import db as db_module


# ---------------------------------------------------------------------------
# Synthetic .docx fixture — a body with three id-addressable paragraphs, one of
# which (0000B002) already carries an inline equation.
# ---------------------------------------------------------------------------

_DOCX_XML = """<?xml version="1.0" encoding="UTF-8"?>
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
      <w:r><w:t>A paragraph with no equation yet.</w:t></w:r>
    </w:p>
  </w:body>
</w:document>
"""

# A paragraph with NO w14:paraId — must be addressable by its synthesized
# 'p{index}' id (index counts every <w:p> in document order).
_DOCX_XML_NO_PARAID = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math">
  <w:body>
    <w:p><w:r><w:t>First.</w:t></w:r></w:p>
    <w:p><w:r><w:t>Second.</w:t></w:r></w:p>
  </w:body>
</w:document>
"""


def _zip_docx(xml: str, *, extra: dict[str, str] | None = None) -> bytes:
    """Build a minimal .docx ZIP. ``extra`` adds sibling parts we assert survive."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", xml)
        for name, content in (extra or {}).items():
            zf.writestr(name, content)
    return buf.getvalue()


def _write_docx(tmp_path, xml: str, name: str = "sample.docx", **kw) -> str:
    path = tmp_path / name
    path.write_bytes(_zip_docx(xml, **kw))
    return str(path)


def _read_document_xml(path: str) -> bytes:
    with zipfile.ZipFile(path) as zf:
        with zf.open("word/document.xml") as fh:
            return fh.read()


def _count_omath(path: str) -> int:
    root = LET.fromstring(_read_document_xml(path))
    m_omath = f"{{{doc_store._OMML_NS}}}oMath"
    return sum(1 for _ in root.iter(m_omath))


def _count_paragraphs(path: str) -> int:
    root = LET.fromstring(_read_document_xml(path))
    w_p = f"{{{doc_store._DOCX_W_NS}}}p"
    return sum(1 for _ in root.iter(w_p))


# ---------------------------------------------------------------------------
# Pure helpers — _load_docx_xml / _find_paragraph_by_id / _save_docx_xml
# ---------------------------------------------------------------------------

def test_load_docx_xml_from_bytes_and_path(tmp_path):
    data = _zip_docx(_DOCX_XML)
    raw, root = doc_store._load_docx_xml(data)
    assert isinstance(raw, bytes)
    assert doc_store._local_tag(root) == "document"

    path = _write_docx(tmp_path, _DOCX_XML)
    raw2, root2 = doc_store._load_docx_xml(path)
    assert isinstance(raw2, bytes)
    assert doc_store._local_tag(root2) == "document"


def test_find_paragraph_by_paraid_and_synthetic_id():
    _, root = doc_store._load_docx_xml(_zip_docx(_DOCX_XML))
    p = doc_store._find_paragraph_by_id(root, "0000B002")
    assert p is not None
    assert "Einstein" in "".join(p.itertext())
    # Unknown / blank ids resolve to None.
    assert doc_store._find_paragraph_by_id(root, "nope") is None
    assert doc_store._find_paragraph_by_id(root, "") is None
    assert doc_store._find_paragraph_by_id(root, None) is None

    # A paragraph without a w14:paraId is addressable by its 'p{index}' id.
    _, root2 = doc_store._load_docx_xml(_zip_docx(_DOCX_XML_NO_PARAID))
    p0 = doc_store._find_paragraph_by_id(root2, "p0")
    p1 = doc_store._find_paragraph_by_id(root2, "p1")
    assert "First." in "".join(p0.itertext())
    assert "Second." in "".join(p1.itertext())


def test_save_docx_xml_preserves_other_parts(tmp_path):
    path = _write_docx(
        tmp_path, _DOCX_XML, extra={"word/styles.xml": "<styles/>", "keep.txt": "hi"}
    )
    raw, root = doc_store._load_docx_xml(path)
    # Mutate: drop the existing oMath so we can observe the rewrite.
    m_omath = f"{{{doc_store._OMML_NS}}}oMath"
    for el in list(root.iter(m_omath)):
        el.getparent().remove(el)
    doc_store._save_docx_xml(raw, root, path)

    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        assert {"word/document.xml", "word/styles.xml", "keep.txt"} <= names
        assert zf.read("word/styles.xml") == b"<styles/>"
        assert zf.read("keep.txt") == b"hi"
    assert _count_omath(path) == 0


def test_insert_omath_at_position_before_after_wraps_in_paragraph():
    _, root = doc_store._load_docx_xml(_zip_docx(_DOCX_XML))
    para = doc_store._find_paragraph_by_id(root, "0000B003")
    omath = LET.fromstring(
        f'<m:oMath xmlns:m="{doc_store._OMML_NS}"><m:r><m:t>x</m:t></m:r></m:oMath>'.encode()
    )
    body = para.getparent()
    before_index = list(body).index(para)
    doc_store._insert_omath_at_position(para, omath, "before")
    # A brand-new <w:p> now sits immediately before the target paragraph.
    new_index = list(body).index(para)
    assert new_index == before_index + 1
    sibling = list(body)[before_index]
    assert doc_store._local_tag(sibling) == "p"
    assert sibling.find(f"{{{doc_store._OMML_NS}}}oMath") is not None


# ---------------------------------------------------------------------------
# DocStructureStore.insert_equation — full round-trip on a local sidecar
# ---------------------------------------------------------------------------

async def _open_store(tmp_path) -> doc_store.DocStructureStore:
    conn = await db_module.init_db(str(tmp_path / "doc_structure.db"))
    store = doc_store.DocStructureStore(conn)
    await store.ensure_schema()
    return store


async def _seed_document(store, tmp_path, xml=_DOCX_XML, name="sample.docx"):
    """Reindex a real .docx so it has a source path AND a stored equation row."""
    docx_path = _write_docx(tmp_path, xml, name=name)
    await store.reindex_document("proj-1", docx_path, source=docx_path)
    return docx_path


def test_insert_equation_latex_append_writes_file_and_resyncs(tmp_path):
    async def _run():
        store = await _open_store(tmp_path)
        try:
            docx_path = await _seed_document(store, tmp_path)
            assert _count_omath(docx_path) == 1  # the seeded E=mc^2

            res = await store.insert_equation(
                "proj-1", docx_path, "0000B003", "a^2 + b^2 = c^2",
                position="append",
            )
            assert "error" not in res
            assert res["position"] == "append"
            assert res["para_id"] == "0000B003"
            assert "<m:oMath" in res["omml"]

            # The equation is physically in the file now (2 oMath total).
            assert _count_omath(docx_path) == 2
            # ...and inside paragraph 0000B003 specifically (append = inline).
            _, root = doc_store._load_docx_xml(docx_path)
            p = doc_store._find_paragraph_by_id(root, "0000B003")
            assert p.find(f"{{{doc_store._OMML_NS}}}oMath") is not None

            # The sidecar index resynced from the modified file: 2 equations.
            doc_row = await store.get_document("proj-1", docx_path)
            eqs = await store.get_equations(doc_row["id"])
            assert len(eqs) == 2
            assert res["resync"]["inserted"]  # resync reported inserts
        finally:
            await store.close()

    asyncio.run(_run())


def test_insert_equation_before_and_after_add_display_paragraphs(tmp_path):
    async def _run():
        store = await _open_store(tmp_path)
        try:
            docx_path = await _seed_document(store, tmp_path)
            base_paras = _count_paragraphs(docx_path)

            res_before = await store.insert_equation(
                "proj-1", docx_path, "0000B003", r"\frac{a}{b}", position="before",
            )
            assert "error" not in res_before
            assert _count_paragraphs(docx_path) == base_paras + 1

            res_after = await store.insert_equation(
                "proj-1", docx_path, "0000B003", "y = 2x", position="after",
            )
            assert "error" not in res_after
            assert _count_paragraphs(docx_path) == base_paras + 2

            # All three equations (seed + 2) are indexed after the two resyncs.
            doc_row = await store.get_document("proj-1", docx_path)
            eqs = await store.get_equations(doc_row["id"])
            assert len(eqs) == 3
        finally:
            await store.close()

    asyncio.run(_run())


def test_insert_equation_reuses_existing_equation_id(tmp_path):
    async def _run():
        store = await _open_store(tmp_path)
        try:
            docx_path = await _seed_document(store, tmp_path)
            doc_row = await store.get_document("proj-1", docx_path)
            seeded = await store.get_equations(doc_row["id"])
            assert len(seeded) == 1
            existing_id = seeded[0]["id"]
            existing_omml = seeded[0]["omml_raw"]

            res = await store.insert_equation(
                "proj-1", docx_path, "0000B003", existing_id, position="append",
            )
            assert "error" not in res
            # The written OMML is the stored equation's OMML (flattened text E=mc2).
            assert doc_store._omml_flatten_text(res["omml"]) == \
                doc_store._omml_flatten_text(existing_omml)
            assert _count_omath(docx_path) == 2
        finally:
            await store.close()

    asyncio.run(_run())


def test_insert_equation_raw_omml_payload(tmp_path):
    async def _run():
        store = await _open_store(tmp_path)
        try:
            docx_path = await _seed_document(store, tmp_path)
            omml = (
                f'<m:oMath xmlns:m="{doc_store._OMML_NS}">'
                "<m:r><m:t>z</m:t></m:r></m:oMath>"
            )
            res = await store.insert_equation(
                "proj-1", docx_path, "0000B003", omml, position="append",
            )
            assert "error" not in res
            assert "z" in doc_store._omml_flatten_text(res["omml"])
            assert _count_omath(docx_path) == 2
        finally:
            await store.close()

    asyncio.run(_run())


def test_insert_equation_bad_para_id_does_not_mutate_file(tmp_path):
    async def _run():
        store = await _open_store(tmp_path)
        try:
            docx_path = await _seed_document(store, tmp_path)
            before = _read_document_xml(docx_path)
            res = await store.insert_equation(
                "proj-1", docx_path, "does-not-exist", "x^2",
            )
            assert "error" in res
            assert "does-not-exist" in res["error"]
            # File is byte-for-byte unchanged — fail-before-write.
            assert _read_document_xml(docx_path) == before
        finally:
            await store.close()

    asyncio.run(_run())


def test_insert_equation_unresolvable_latex_errors_before_write(tmp_path):
    async def _run():
        store = await _open_store(tmp_path)
        try:
            docx_path = await _seed_document(store, tmp_path)
            before = _read_document_xml(docx_path)
            # Empty LaTeX -> latex_to_omml None -> unresolvable, file untouched.
            res = await store.insert_equation(
                "proj-1", docx_path, "0000B003", "   ",
            )
            assert "error" in res
            assert _read_document_xml(docx_path) == before
        finally:
            await store.close()

    asyncio.run(_run())


def test_insert_equation_unknown_doc_and_bad_position(tmp_path):
    async def _run():
        store = await _open_store(tmp_path)
        try:
            # Unknown source (never stored).
            res = await store.insert_equation(
                "proj-1", "never.docx", "p0", "x^2",
            )
            assert "error" in res
            assert "never.docx" in res["error"]

            # Bad position value is rejected up front.
            docx_path = await _seed_document(store, tmp_path)
            res2 = await store.insert_equation(
                "proj-1", docx_path, "0000B002", "x^2", position="sideways",
            )
            assert "error" in res2
            assert "position" in res2["error"]
        finally:
            await store.close()

    asyncio.run(_run())


def test_insert_equation_malformed_omml_payload_errors_before_write(tmp_path):
    """A '<'-prefixed payload that is NOT valid XML is caught (never crashes,
    never mutates the file)."""
    async def _run():
        store = await _open_store(tmp_path)
        try:
            docx_path = await _seed_document(store, tmp_path)
            before = _read_document_xml(docx_path)
            res = await store.insert_equation(
                "proj-1", docx_path, "0000B003", "<m:oMath><unclosed",
            )
            assert "error" in res
            assert "not valid XML" in res["error"]
            assert _read_document_xml(docx_path) == before
        finally:
            await store.close()

    asyncio.run(_run())


def test_insert_equation_missing_file_on_disk(tmp_path):
    async def _run():
        store = await _open_store(tmp_path)
        try:
            docx_path = await _seed_document(store, tmp_path)
            # Remove the file after it was indexed; the write-back must error.
            import os
            os.remove(docx_path)
            res = await store.insert_equation(
                "proj-1", docx_path, "0000B002", "x^2",
            )
            assert "error" in res
            assert "not found" in res["error"]
        finally:
            await store.close()

    asyncio.run(_run())


def test_insert_equation_addresses_paragraph_without_paraid(tmp_path):
    async def _run():
        store = await _open_store(tmp_path)
        try:
            docx_path = await _seed_document(
                store, tmp_path, xml=_DOCX_XML_NO_PARAID, name="noid.docx",
            )
            res = await store.insert_equation(
                "proj-1", docx_path, "p1", "x^2", position="append",
            )
            assert "error" not in res
            _, root = doc_store._load_docx_xml(docx_path)
            p1 = doc_store._find_paragraph_by_id(root, "p1")
            assert p1.find(f"{{{doc_store._OMML_NS}}}oMath") is not None
        finally:
            await store.close()

    asyncio.run(_run())


def test_resync_document_equations_standalone(tmp_path):
    """resync drops stale rows and re-derives them from the current file."""
    async def _run():
        store = await _open_store(tmp_path)
        try:
            docx_path = await _seed_document(store, tmp_path)
            doc_row = await store.get_document("proj-1", docx_path)
            # Add an out-of-band equation row that is NOT in the file.
            await store.put_equations(doc_row["id"], [{"latex": "not-in-file"}])
            assert len(await store.get_equations(doc_row["id"])) == 2

            result = await store.resync_document_equations(doc_row["id"], docx_path)
            # After resync only the file's real single equation remains.
            eqs = await store.get_equations(doc_row["id"])
            assert len(eqs) == 1
            assert eqs[0]["element_id"] == "0000B002"
            assert len(result["inserted"]) == 1
        finally:
            await store.close()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# MCP tool — insert_equation through the real _dispatch_mcp_tool path
# ---------------------------------------------------------------------------

def _seed_store_via_env(tmp_path, monkeypatch):
    sidecar = str(tmp_path / "mcp_doc_structure.db")
    monkeypatch.setenv("MERIDIAN_DOC_STORE_URL", sidecar)
    doc_store._reset_doc_store_cache()
    return sidecar


def test_mcp_insert_equation_round_trip(tmp_path, monkeypatch):
    async def _run():
        from meridian import server as mh

        sidecar = _seed_store_via_env(tmp_path, monkeypatch)
        db = await db_module.init_db(":memory:")
        try:
            proj = await db_module.create_project(db, "eq-insert-proj")
            pid = proj["id"]

            docx_path = _write_docx(tmp_path, _DOCX_XML, name="chapter.docx")
            seed = await doc_store.open_doc_store_for(
                plan=None, hosted=False, data_dir=str(tmp_path),
                tenant_pg_url=None, override_url=sidecar,
            )
            await seed.reindex_document(pid, docx_path, source=docx_path)

            res = await mh._dispatch_mcp_tool(
                "insert_equation",
                {"project_id": pid, "doc": docx_path, "para_id": "0000B003",
                 "equation_id_or_omml": "F = ma", "position": "append"},
                db, str(tmp_path),
            )
            assert "error" not in res
            assert res["project_id"] == pid
            assert res["position"] == "append"
            assert _count_omath(docx_path) == 2
        finally:
            await db.close()
            await doc_store.close_all_doc_stores()

    asyncio.run(_run())


def test_mcp_insert_equation_requires_args(tmp_path, monkeypatch):
    async def _run():
        from meridian import server as mh

        _seed_store_via_env(tmp_path, monkeypatch)
        db = await db_module.init_db(":memory:")
        try:
            assert (await mh._dispatch_mcp_tool(
                "insert_equation", {}, db, str(tmp_path),
            )).get("error")
            assert (await mh._dispatch_mcp_tool(
                "insert_equation", {"project_id": "p"}, db, str(tmp_path),
            )).get("error")
            assert (await mh._dispatch_mcp_tool(
                "insert_equation", {"project_id": "p", "doc": "x.docx"},
                db, str(tmp_path),
            )).get("error")
            assert (await mh._dispatch_mcp_tool(
                "insert_equation",
                {"project_id": "p", "doc": "x.docx", "para_id": "p0"},
                db, str(tmp_path),
            )).get("error")
        finally:
            await db.close()
            await doc_store.close_all_doc_stores()

    asyncio.run(_run())


def test_mcp_insert_equation_unknown_doc_returns_error(tmp_path, monkeypatch):
    async def _run():
        from meridian import server as mh

        _seed_store_via_env(tmp_path, monkeypatch)
        db = await db_module.init_db(":memory:")
        try:
            proj = await db_module.create_project(db, "eq-insert-proj-2")
            res = await mh._dispatch_mcp_tool(
                "insert_equation",
                {"project_id": proj["id"], "doc": "never-ingested.docx",
                 "para_id": "p0", "equation_id_or_omml": "x^2"},
                db, str(tmp_path),
            )
            assert "error" in res
            assert "never-ingested.docx" in res["error"]
        finally:
            await db.close()
            await doc_store.close_all_doc_stores()

    asyncio.run(_run())
