"""Regression test for 827b6bdc.

BUG: a .docx's native w14:paraId attribute is SUPPOSED to be unique per
paragraph, but nothing in this codebase actually verified that. The reference
regression case: two distinct <w:p> elements both carrying
w14:paraId="6BDC5378" (a malformed / hand-edited / merged document).
document_content_tree's _build_synth_id_map only synthesizes stable ids for
paragraphs LACKING a native paraId -- it did nothing for paragraphs that DO
have one, so two paragraphs sharing a native id silently got the same
para_id in the content tree, with zero detection or warning.

FIX (this vendored copy): document_content_tree now also calls
_find_duplicate_native_para_ids(body) and surfaces the result as a new
``duplicate_para_ids`` key -- read-only reporting, never a silent mutation.
index_docx_structure passes this straight through in its own summary so a
local-indexing caller learns about the ambiguity too.

These tests exercise the VENDORED copy directly (meridian_docs ships its own
independent copy of document_content_tree -- see _vendored_content_tree.py's
module docstring for why) so a drift between it and the canonical
packages/docparse/docparse/docs_intel.py implementation would show up here,
not just in the canonical test suite.
"""
from __future__ import annotations

import io
import zipfile

import pytest

from meridian_docs import docs_intel
from meridian_docs._vendored_content_tree import (
    document_content_tree,
    _find_duplicate_native_para_ids,
)


def _make_docx_bytes(xml: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", xml)
    return buf.getvalue()


def _write_docx(tmp_path, xml: str, name: str = "doc.docx") -> str:
    path = str(tmp_path / name)
    with open(path, "wb") as fh:
        fh.write(_make_docx_bytes(xml))
    return path


# The reference regression case named by this item: two distinct paragraphs
# both carrying w14:paraId="6BDC5378".
_DUPLICATE_PARA_ID_XML = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:p w14:paraId="10000001">
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Chapter One</w:t></w:r>
    </w:p>
    <w:p w14:paraId="6BDC5378">
      <w:r><w:t>First paragraph, original owner of 6BDC5378.</w:t></w:r>
    </w:p>
    <w:p w14:paraId="AAAA9999">
      <w:r><w:t>An unrelated, uniquely-identified paragraph in between.</w:t></w:r>
    </w:p>
    <w:p w14:paraId="6BDC5378">
      <w:r><w:t>Second, unrelated paragraph that WRONGLY shares 6BDC5378.</w:t></w:r>
    </w:p>
  </w:body>
</w:document>
"""


def test_vendored_document_content_tree_reports_duplicate_native_para_id():
    tree = document_content_tree(_make_docx_bytes(_DUPLICATE_PARA_ID_XML))
    dups = tree["duplicate_para_ids"]
    assert len(dups) == 1
    entry = dups[0]
    assert entry["para_id"] == "6BDC5378"
    assert entry["occurrence_count"] == 2
    # Body-order index + a text snippet from each duplicate -- enough to
    # actually locate the problem, not just "duplicate found".
    assert [o["index"] for o in entry["occurrences"]] == [1, 3]
    assert "First paragraph" in entry["occurrences"][0]["text"]
    assert "Second, unrelated paragraph" in entry["occurrences"][1]["text"]
    # Both paragraphs are still present in blocks -- surfaced, not collapsed.
    dup_blocks = [b for b in tree["blocks"] if b.get("para_id") == "6BDC5378"]
    assert len(dup_blocks) == 2
    assert dup_blocks[0]["text"] != dup_blocks[1]["text"]


def test_vendored_document_content_tree_no_duplicates_reports_empty_list():
    clean_xml = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:p w14:paraId="10000001"><w:r><w:t>Unique one.</w:t></w:r></w:p>
    <w:p w14:paraId="10000002"><w:r><w:t>Unique two.</w:t></w:r></w:p>
  </w:body>
</w:document>
"""
    tree = document_content_tree(_make_docx_bytes(clean_xml))
    assert tree["duplicate_para_ids"] == []


def test_vendored_find_duplicate_native_para_ids_matches_canonical_shape():
    """The vendored helper's output shape must match the canonical
    docparse.docs_intel._find_duplicate_native_para_ids exactly -- this is
    what index_docx_structure and document_content_tree both rely on.

    docparse is a SEPARATE package this extension deliberately does not
    depend on at runtime (see _vendored_content_tree.py's module docstring
    -- uvx isolated installs don't reliably resolve it); this cross-check
    only runs when it happens to also be on the path (e.g. this monorepo's
    own dev/test environment), and is skipped rather than failing a
    standalone install of this extension where it is legitimately absent.
    """
    try:
        from docparse.docs_intel import (
            _find_duplicate_native_para_ids as canonical_impl,
        )
    except ImportError:
        pytest.skip("docparse not installed in this environment (expected for a standalone meridian-docs install)")
    import xml.etree.ElementTree as ET

    root = ET.fromstring(_DUPLICATE_PARA_ID_XML)
    body = root.find("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}body")

    vendored_result = _find_duplicate_native_para_ids(body)
    canonical_result = canonical_impl(body)
    assert vendored_result == canonical_result


def test_index_docx_structure_surfaces_duplicate_para_ids(tmp_path):
    """index_docx_structure -- the extension's own local-only structural
    indexer -- passes document_content_tree's duplicate report straight
    through in its summary, READ-ONLY (it never renumbers to "fix" it)."""
    path = _write_docx(tmp_path, _DUPLICATE_PARA_ID_XML, name="dup.docx")
    before = open(path, "rb").read()

    index_db_path = str(tmp_path / "structure_index.sqlite3")
    summary = docs_intel.index_docx_structure(path, index_db_path)

    assert summary["duplicate_para_ids"]
    assert len(summary["duplicate_para_ids"]) == 1
    assert summary["duplicate_para_ids"][0]["para_id"] == "6BDC5378"
    assert summary["duplicate_para_ids"][0]["occurrence_count"] == 2
    # Read-only: indexing never mutates the source .docx to resolve this.
    assert open(path, "rb").read() == before
    # Indexing still completes normally -- a duplicate is reported, not fatal.
    assert summary["complete"] is True
    assert summary["heading_count"] == 1


def test_index_docx_structure_no_duplicates_reports_empty_list(tmp_path):
    clean_xml = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:p w14:paraId="10000001"><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Only Heading</w:t></w:r></w:p>
    <w:p w14:paraId="10000002"><w:r><w:t>Only paragraph.</w:t></w:r></w:p>
  </w:body>
</w:document>
"""
    path = _write_docx(tmp_path, clean_xml, name="clean.docx")
    index_db_path = str(tmp_path / "structure_index.sqlite3")
    summary = docs_intel.index_docx_structure(path, index_db_path)
    assert summary["duplicate_para_ids"] == []
