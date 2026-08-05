"""Tests for meridian_docs.docs_intel.set_page_header / set_page_footer (f1185012).

FEAT: no page header/footer support at all, previously. python-docx, the
Google Docs API + UI, and Apryse all treat header/footer as first-class,
table-stakes DOCX functionality.

This is a genuinely SEPARATE write path from every other write-back function
in docs_intel.py: it is the first one that has to add brand-new OOXML parts
(word/header<N>.xml / word/footer<N>.xml), a new relationship in
word/_rels/document.xml.rels, and a new content-type override in
[Content_Types].xml -- rather than only ever rewriting the one, already-
existing word/document.xml part in place. These tests build throwaway .docx
files (both a hand-built minimal one with NO supporting parts, and a more
"real" one that already has [Content_Types].xml / .rels the way actual Word
output does), add a header/footer, and verify the resulting XML/relationships/
content-types are structurally correct and the document still re-parses
correctly with this module's own tooling (python-docx is not available in
this environment, so round-trip verification is via re-parsing our own
written XML directly, per the sprint scope).
"""
from __future__ import annotations

import io
import zipfile

import pytest
import xml.etree.ElementTree as ET

from meridian_docs import docs_intel


@pytest.fixture(autouse=True)
def _default_render_capability(monkeypatch):
    """ddd79188 follow-up -- set_page_header/set_page_footer now invoke the
    real render-capability gate (render_gate.check_render_capability) AFTER
    the structural write is staged, verified, and promoted. Every test in
    this file exercises STRUCTURAL correctness and must not depend on -- or
    be slowed/blocked by -- whichever render backends (LibreOffice, Word COM)
    happen to be installed on the machine running the suite. Stub a
    successful 'rendered' result by default, mirroring
    test_19be1551_insert_figure_block.py's own fixture of the same name.
    """
    monkeypatch.setattr(
        docs_intel.render_gate,
        "check_render_capability",
        lambda docx_path, **kwargs: {
            "status": "rendered",
            "backend": "test-stub",
            "detail": {"stub": True},
        },
    )


_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
_PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"


def _q(ns: str, tag: str) -> str:
    return f"{{{ns}}}{tag}"


_MINIMAL_DOCUMENT_XML = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:p w14:paraId="P0000001">
      <w:r><w:t>Hello world.</w:t></w:r>
    </w:p>
    <w:sectPr>
      <w:pgSz w:w="12240" w:h="15840"/>
    </w:sectPr>
  </w:body>
</w:document>
"""


def _write_minimal_docx(tmp_path, name: str = "minimal.docx") -> str:
    """A docx with ONLY word/document.xml -- no [Content_Types].xml, no
    word/_rels/document.xml.rels at all. Exercises the from-scratch
    scaffolding fallback path."""
    path = str(tmp_path / name)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", _MINIMAL_DOCUMENT_XML)
    with open(path, "wb") as fh:
        fh.write(buf.getvalue())
    return path


_REAL_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
</Types>
"""

_REAL_DOCUMENT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>
"""

_MINIMAL_STYLES_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>
"""


def _write_real_docx(tmp_path, name: str = "real.docx") -> str:
    """A docx with a realistic (Word-shaped) [Content_Types].xml and
    word/_rels/document.xml.rels already populated with an UNRELATED
    relationship (styles.xml) -- proves existing entries survive untouched."""
    path = str(tmp_path / name)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", _MINIMAL_DOCUMENT_XML)
        zf.writestr("[Content_Types].xml", _REAL_CONTENT_TYPES)
        zf.writestr("word/_rels/document.xml.rels", _REAL_DOCUMENT_RELS)
        zf.writestr("word/styles.xml", _MINIMAL_STYLES_XML)
    with open(path, "wb") as fh:
        fh.write(buf.getvalue())
    return path


def _read_part(path: str, part_name: str) -> bytes:
    with zipfile.ZipFile(path) as zf:
        return zf.read(part_name)


def _namelist(path: str) -> list[str]:
    with zipfile.ZipFile(path) as zf:
        return zf.namelist()


# ---------------------------------------------------------------------------
# Header on a from-scratch minimal docx (no [Content_Types].xml / .rels yet).
# ---------------------------------------------------------------------------


def test_set_page_header_from_scratch_scaffolding(tmp_path):
    path = _write_minimal_docx(tmp_path)

    result = docs_intel.set_page_header(path, "Meridian Thesis Draft")
    assert result["status"] == "set"
    assert result["kind"] == "header"
    assert result["type"] == "default"
    assert result["part_name"] == "word/header1.xml"
    assert result["relationship_id"] == "rId1"

    names = _namelist(path)
    assert "word/header1.xml" in names
    assert "[Content_Types].xml" in names
    assert "word/_rels/document.xml.rels" in names

    # Header part content is structurally correct: <w:hdr> wrapping one
    # paragraph/run with the exact text.
    hdr_root = ET.fromstring(_read_part(path, "word/header1.xml"))
    assert hdr_root.tag == _q(_W, "hdr")
    texts = [t.text for t in hdr_root.iter(_q(_W, "t"))]
    assert texts == ["Meridian Thesis Draft"]

    # Content-types override registered correctly.
    ct_root = ET.fromstring(_read_part(path, "[Content_Types].xml"))
    overrides = {
        o.get("PartName"): o.get("ContentType")
        for o in ct_root.iter(_q(_PKG_CT_NS, "Override"))
    }
    assert overrides["/word/header1.xml"] == (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml"
    )

    # Relationship registered correctly.
    rels_root = ET.fromstring(_read_part(path, "word/_rels/document.xml.rels"))
    rels = {
        r.get("Id"): (r.get("Type"), r.get("Target"))
        for r in rels_root.iter(_q(_PKG_REL_NS, "Relationship"))
    }
    assert rels["rId1"] == (
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/header",
        "header1.xml",
    )

    # sectPr wired with the headerReference pointing at that relationship.
    doc_root = ET.fromstring(_read_part(path, "word/document.xml"))
    body = doc_root.find(_q(_W, "body"))
    sectpr = body.find(_q(_W, "sectPr"))
    href = sectpr.find(_q(_W, "headerReference"))
    assert href is not None
    assert href.get(_q(_W, "type")) == "default"
    assert href.get(_q(_R_NS, "id")) == "rId1"

    # The original paragraph content is untouched.
    body_texts = [t.text for t in body.iter(_q(_W, "t")) if t.text]
    assert "Hello world." in body_texts

    # The document still re-parses cleanly with this module's own tooling.
    paras = docs_intel.parse_docx(path)
    assert any(p["text"] == "Hello world." for p in paras)


def test_set_page_footer_from_scratch_scaffolding(tmp_path):
    path = _write_minimal_docx(tmp_path)

    result = docs_intel.set_page_footer(path, "Page footer text")
    assert result["status"] == "set"
    assert result["kind"] == "footer"
    assert result["part_name"] == "word/footer1.xml"

    ftr_root = ET.fromstring(_read_part(path, "word/footer1.xml"))
    assert ftr_root.tag == _q(_W, "ftr")
    texts = [t.text for t in ftr_root.iter(_q(_W, "t"))]
    assert texts == ["Page footer text"]

    doc_root = ET.fromstring(_read_part(path, "word/document.xml"))
    sectpr = doc_root.find(_q(_W, "body")).find(_q(_W, "sectPr"))
    fref = sectpr.find(_q(_W, "footerReference"))
    assert fref is not None
    assert fref.get(_q(_W, "type")) == "default"


# ---------------------------------------------------------------------------
# Header + footer together -- schema ordering + relationship id allocation.
# ---------------------------------------------------------------------------


def test_header_then_footer_schema_order_and_rel_ids(tmp_path):
    path = _write_minimal_docx(tmp_path)

    r1 = docs_intel.set_page_header(path, "Header text")
    r2 = docs_intel.set_page_footer(path, "Footer text")

    assert r1["relationship_id"] == "rId1"
    assert r2["relationship_id"] == "rId2"  # allocated past rId1, no collision

    doc_root = ET.fromstring(_read_part(path, "word/document.xml"))
    sectpr = doc_root.find(_q(_W, "body")).find(_q(_W, "sectPr"))
    child_tags = [c.tag.rsplit("}", 1)[-1] for c in sectpr]
    # headerReference must precede footerReference per CT_SectPr schema order,
    # and both must precede the pre-existing pgSz element.
    assert child_tags.index("headerReference") < child_tags.index("footerReference")
    assert child_tags.index("footerReference") < child_tags.index("pgSz")

    # pgSz (pre-existing sectPr content) is untouched.
    pgsz = sectpr.find(_q(_W, "pgSz"))
    assert pgsz.get(_q(_W, "w")) == "12240"


# ---------------------------------------------------------------------------
# "Set" semantics: calling twice with the same type overwrites in place.
# ---------------------------------------------------------------------------


def test_set_page_header_twice_same_type_overwrites_in_place(tmp_path):
    path = _write_minimal_docx(tmp_path)

    first = docs_intel.set_page_header(path, "Draft v1")
    second = docs_intel.set_page_header(path, "Draft v2 -- final")

    # Same part, same relationship id, same relationship count -- no orphaned
    # duplicate part/relationship/override from the second call.
    assert second["part_name"] == first["part_name"] == "word/header1.xml"
    assert second["relationship_id"] == first["relationship_id"] == "rId1"

    hdr_root = ET.fromstring(_read_part(path, "word/header1.xml"))
    texts = [t.text for t in hdr_root.iter(_q(_W, "t"))]
    assert texts == ["Draft v2 -- final"]

    rels_root = ET.fromstring(_read_part(path, "word/_rels/document.xml.rels"))
    rel_ids = [r.get("Id") for r in rels_root.iter(_q(_PKG_REL_NS, "Relationship"))]
    assert rel_ids == ["rId1"]  # exactly one relationship, not two

    ct_root = ET.fromstring(_read_part(path, "[Content_Types].xml"))
    overrides = [o.get("PartName") for o in ct_root.iter(_q(_PKG_CT_NS, "Override"))]
    assert overrides.count("/word/header1.xml") == 1  # not duplicated

    doc_root = ET.fromstring(_read_part(path, "word/document.xml"))
    sectpr = doc_root.find(_q(_W, "body")).find(_q(_W, "sectPr"))
    href_count = len(sectpr.findall(_q(_W, "headerReference")))
    assert href_count == 1  # not duplicated


def test_set_page_header_different_types_coexist(tmp_path):
    path = _write_minimal_docx(tmp_path)

    docs_intel.set_page_header(path, "Default header", header_type="default")
    docs_intel.set_page_header(path, "First-page header", header_type="first")

    doc_root = ET.fromstring(_read_part(path, "word/document.xml"))
    sectpr = doc_root.find(_q(_W, "body")).find(_q(_W, "sectPr"))
    refs = {
        h.get(_q(_W, "type")): h.get(_q(_R_NS, "id"))
        for h in sectpr.findall(_q(_W, "headerReference"))
    }
    assert set(refs) == {"default", "first"}
    assert refs["default"] != refs["first"]

    names = _namelist(path)
    assert "word/header1.xml" in names
    assert "word/header2.xml" in names


# ---------------------------------------------------------------------------
# A "real" (Word-shaped) docx: existing content-types/rels entries survive.
# ---------------------------------------------------------------------------


def test_set_page_header_preserves_existing_real_docx_parts(tmp_path):
    path = _write_real_docx(tmp_path)

    result = docs_intel.set_page_header(path, "Header on a real docx")
    assert result["status"] == "set"
    # styles.xml already held rId1 -- the new relationship must not collide.
    assert result["relationship_id"] == "rId2"

    rels_root = ET.fromstring(_read_part(path, "word/_rels/document.xml.rels"))
    rels = {r.get("Id"): r.get("Target") for r in rels_root.iter(_q(_PKG_REL_NS, "Relationship"))}
    assert rels["rId1"] == "styles.xml"  # untouched
    assert rels["rId2"] == "header1.xml"

    ct_root = ET.fromstring(_read_part(path, "[Content_Types].xml"))
    overrides = {
        o.get("PartName"): o.get("ContentType")
        for o in ct_root.iter(_q(_PKG_CT_NS, "Override"))
    }
    assert overrides["/word/styles.xml"] == (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"
    )
    assert overrides["/word/document.xml"] == (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
    )
    assert overrides["/word/header1.xml"] == (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml"
    )

    # styles.xml part itself is byte-for-byte untouched.
    assert _read_part(path, "word/styles.xml") == _MINIMAL_STYLES_XML.encode("utf-8")

    # The whole archive is still a structurally valid ZIP that Python itself
    # can re-open and every part re-parses as well-formed XML.
    with zipfile.ZipFile(path) as zf:
        bad = zf.testzip()
        assert bad is None
        for name in zf.namelist():
            if name.endswith(".xml") or name.endswith(".rels"):
                ET.fromstring(zf.read(name))  # raises ET.ParseError if malformed


# ---------------------------------------------------------------------------
# Error handling.
# ---------------------------------------------------------------------------


def test_set_page_header_rejects_empty_text(tmp_path):
    path = _write_minimal_docx(tmp_path)
    result = docs_intel.set_page_header(path, "   ")
    assert "error" in result


def test_set_page_header_rejects_invalid_type(tmp_path):
    path = _write_minimal_docx(tmp_path)
    result = docs_intel.set_page_header(path, "Header", header_type="bogus")
    assert "error" in result


def test_set_page_footer_rejects_invalid_type(tmp_path):
    path = _write_minimal_docx(tmp_path)
    result = docs_intel.set_page_footer(path, "Footer", footer_type="bogus")
    assert "error" in result


def test_set_page_header_missing_file():
    result = docs_intel.set_page_header("/no/such/path.docx", "Header")
    assert "error" in result


def test_set_page_header_invalidates_sidecar_mtime(tmp_path):
    path = _write_minimal_docx(tmp_path)
    index_db_path = str(tmp_path / "sidecar.sqlite3")
    docs_intel.index_docx(path, index_db_path)

    conn = docs_intel._connect(index_db_path)
    try:
        row = conn.execute(
            "SELECT value FROM docx_index_meta WHERE key = 'source_mtime'"
        ).fetchone()
        assert row is not None and row[0] is not None
    finally:
        conn.close()

    docs_intel.set_page_header(path, "Header", index_db_path=index_db_path)

    conn = docs_intel._connect(index_db_path)
    try:
        row = conn.execute(
            "SELECT value FROM docx_index_meta WHERE key = 'source_mtime'"
        ).fetchone()
        assert row is not None and row[0] is None
    finally:
        conn.close()
