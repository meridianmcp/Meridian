"""Tests for docs_intel.locate_anchor / locate_anchors (2271789f).

A read-only, fresh-snapshot deterministic anchor locator: resolves a query
(section_path, section_text, caption_label, text, para_id) against sections,
paragraphs, captions, tables (incl. cell text), and equations, returning a
stable target_para_id, document_order, quoted_text, a normalized preview,
bookmark/REF status, and an explicit ambiguity/candidate list. See the
module-level comment above docs_intel.locate_anchor for the full contract.

All tests are pure Python (stdlib + pytest) against synthetic in-memory
.docx bytes written to tmp_path -- no mcp, no network, never mutates the
source file.
"""
from __future__ import annotations

import io
import re
import zipfile

import pytest

from meridian_docs import docs_intel


_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W14 = "http://schemas.microsoft.com/office/word/2010/wordml"
_M = "http://schemas.openxmlformats.org/officeDocument/2006/math"

_SIMPLE_OMATH = (
    f'<m:oMath xmlns:m="{_M}">'
    "<m:r><m:t>E</m:t></m:r><m:r><m:t>=</m:t></m:r><m:r><m:t>mc2</m:t></m:r>"
    "</m:oMath>"
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


# ---------------------------------------------------------------------------
# One document exercising every anchor kind:
#   - headings with and without an explicit "N.N.N" numbering prefix
#     (explicit_number vs. computed_path resolution)
#   - a table (with header + data rows) immediately followed by its
#     SEQ Table caption (Table 1), bookmarked and REF'd from Conclusion
#   - a figure caption (Figure 1), bookmarked but never referenced
#   - a standalone equation (OMML only -- no <w:t> text)
# ---------------------------------------------------------------------------
_DOC_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="{_W}"
    xmlns:w14="{_W14}">
  <w:body>
    <w:p w14:paraId="H0000001">
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Introduction</w:t></w:r>
    </w:p>
    <w:p w14:paraId="P0000001">
      <w:r><w:t>Intro body paragraph.</w:t></w:r>
    </w:p>
    <w:p w14:paraId="H0000002">
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Methods</w:t></w:r>
    </w:p>
    <w:p w14:paraId="H0000003">
      <w:pPr><w:pStyle w:val="Heading2"/></w:pPr>
      <w:r><w:t>Data Collection</w:t></w:r>
    </w:p>
    <w:p w14:paraId="P0000003">
      <w:r><w:t>Data collection body paragraph.</w:t></w:r>
    </w:p>
    <w:p w14:paraId="H0000004">
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Results</w:t></w:r>
    </w:p>
    <w:p w14:paraId="H0000005">
      <w:pPr><w:pStyle w:val="Heading2"/></w:pPr>
      <w:r><w:t>Overview</w:t></w:r>
    </w:p>
    <w:p w14:paraId="F0000001">
      <w:pPr><w:pStyle w:val="Caption"/></w:pPr>
      <w:bookmarkStart w:id="0" w:name="_Ref300000001"/>
      <w:r><w:t xml:space="preserve">Figure </w:t></w:r>
      <w:fldSimple w:instr="SEQ Figure \\* ARABIC"><w:r><w:t>1</w:t></w:r></w:fldSimple>
      <w:bookmarkEnd w:id="0"/>
      <w:r><w:t xml:space="preserve">. Overview diagram.</w:t></w:r>
    </w:p>
    <w:p w14:paraId="H0000006">
      <w:pPr><w:pStyle w:val="Heading2"/></w:pPr>
      <w:r><w:t>Detailed Analysis</w:t></w:r>
    </w:p>
    <w:p w14:paraId="H0000007">
      <w:pPr><w:pStyle w:val="Heading3"/></w:pPr>
      <w:r><w:t xml:space="preserve">3.2.4 Threshold Table</w:t></w:r>
    </w:p>
    <w:p w14:paraId="P0000007">
      <w:r><w:t>Threshold analysis before and after calibration.</w:t></w:r>
    </w:p>
    <w:tbl>
      <w:tr>
        <w:tc><w:p><w:r><w:t>Metric</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>Before (%)</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>After (%)</w:t></w:r></w:p></w:tc>
      </w:tr>
      <w:tr>
        <w:tc><w:p><w:r><w:t>Threshold</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>12</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>45</w:t></w:r></w:p></w:tc>
      </w:tr>
      <w:tr>
        <w:tc><w:p><w:r><w:t>Recovery</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>30</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>60</w:t></w:r></w:p></w:tc>
      </w:tr>
    </w:tbl>
    <w:p w14:paraId="T0000001">
      <w:pPr><w:pStyle w:val="Caption"/></w:pPr>
      <w:bookmarkStart w:id="1" w:name="_Ref300000002"/>
      <w:r><w:t xml:space="preserve">Table </w:t></w:r>
      <w:fldSimple w:instr="SEQ Table \\* ARABIC"><w:r><w:t>1</w:t></w:r></w:fldSimple>
      <w:bookmarkEnd w:id="1"/>
      <w:r><w:t xml:space="preserve">. Threshold comparison before and after calibration.</w:t></w:r>
    </w:p>
    <w:p w14:paraId="EQ0000001">
      {_SIMPLE_OMATH}
    </w:p>
    <w:p w14:paraId="H0000008">
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Conclusion</w:t></w:r>
    </w:p>
    <w:p w14:paraId="R0000001">
      <w:r><w:t xml:space="preserve">See </w:t></w:r>
      <w:r><w:fldChar w:fldCharType="begin"/></w:r>
      <w:r><w:instrText xml:space="preserve"> REF _Ref300000002 \\h </w:instrText></w:r>
      <w:r><w:fldChar w:fldCharType="separate"/></w:r>
      <w:r><w:t xml:space="preserve">Table 1</w:t></w:r>
      <w:r><w:fldChar w:fldCharType="end"/></w:r>
      <w:r><w:t xml:space="preserve"> for details.</w:t></w:r>
    </w:p>
    <w:p w14:paraId="P0000008">
      <w:r><w:t xml:space="preserve">Conclusion body paragraph with enough words to test the preview truncation logic near the boundary right here.</w:t></w:r>
    </w:p>
    <w:sectPr/>
  </w:body>
</w:document>
"""


@pytest.fixture()
def doc_path(tmp_path):
    return _write_docx(tmp_path, _DOC_XML)


# ---------------------------------------------------------------------------
# Section resolution: explicit numbering vs. positional fallback
# ---------------------------------------------------------------------------

def test_locate_anchor_by_explicit_section_number(doc_path):
    result = docs_intel.locate_anchor(doc_path, {"section_path": "3.2.4"})
    assert result["status"] == "resolved"
    assert result["element_type"] == "heading"
    assert result["target_para_id"] == "H0000007"
    assert result["section_path"] == "3.2.4"
    assert result["heading_para_id"] == "H0000007"
    assert result["quoted_text"] == "3.2.4 Threshold Table"
    assert result["candidates"] == []


def test_locate_anchor_by_computed_positional_path(doc_path):
    # "Data Collection" carries no explicit numbering in its own text; it
    # resolves via the positional counters instead (2nd H1's 1st H2 -> 2.1).
    result = docs_intel.locate_anchor(doc_path, {"section_path": "2.1"})
    assert result["status"] == "resolved"
    assert result["target_para_id"] == "H0000003"
    assert result["quoted_text"] == "Data Collection"


def test_locate_anchor_section_text_match(doc_path):
    result = docs_intel.locate_anchor(doc_path, {"section_text": "overview"})
    assert result["status"] == "resolved"
    assert result["target_para_id"] == "H0000005"


def test_locate_anchor_unknown_section_path_not_found(doc_path):
    result = docs_intel.locate_anchor(doc_path, {"section_path": "9.9.9"})
    assert result["status"] == "not_found"
    assert result["candidates"] == []


# ---------------------------------------------------------------------------
# Table location: section + a table label, then a Ctrl+F-style text query
# scoped to what the section/caption resolved to.
# ---------------------------------------------------------------------------

def test_locate_anchor_section_plus_text_finds_unique_table_cell(doc_path):
    result = docs_intel.locate_anchor(
        doc_path, {"section_path": "3.2.4", "text": "12"}
    )
    assert result["status"] == "resolved"
    assert result["element_type"] == "table_cell"
    assert result["quoted_text"] == "12"
    assert re.match(r"^tbl\d+:r1:c1$", result["target_para_id"])
    assert result["word_search_locator"] == "12"
    # Table cells are synthetic ids -- never a real paragraph, so bookmark
    # lookup is explicitly not attempted rather than silently guessed.
    assert result["bookmark_exists"] is False
    assert result["ref_status"]["checked"] is False


def test_locate_anchor_section_plus_ambiguous_text(doc_path):
    # "Threshold" appears in the heading, the body paragraph, the table
    # header cell, and the table caption -- all within the 3.2.4 subtree.
    result = docs_intel.locate_anchor(
        doc_path, {"section_path": "3.2.4", "text": "Threshold"}
    )
    assert result["status"] == "ambiguous"
    assert result["candidate_count"] >= 4
    for candidate in result["candidates"]:
        assert "target_para_id" in candidate
        assert "leading_text_preview" in candidate
        assert "element_type" in candidate


def test_locate_anchor_caption_label_table(doc_path):
    result = docs_intel.locate_anchor(doc_path, {"caption_label": "Table 1"})
    assert result["status"] == "resolved"
    assert result["target_para_id"] == "T0000001"
    assert result["element_type"] == "table_caption"
    # The caption is bookmarked AND something (Conclusion's REF field)
    # actually points at it.
    assert result["bookmark_exists"] is True
    assert result["ref_status"]["checked"] is True
    assert result["ref_status"]["reference_count"] == 1
    assert result["ref_status"]["bookmark_names"] == ["_Ref300000002"]
    assert result["ref_status"]["references"][0]["para_id"] == "R0000001"


def test_locate_anchor_caption_label_figure_has_no_incoming_ref(doc_path):
    result = docs_intel.locate_anchor(doc_path, {"caption_label": "Figure 1"})
    assert result["status"] == "resolved"
    assert result["target_para_id"] == "F0000001"
    # Bookmarked, but nothing references it -- bookmark_exists and
    # ref_status.reference_count are independent signals.
    assert result["bookmark_exists"] is True
    assert result["ref_status"]["reference_count"] == 0


def test_locate_anchor_caption_label_plus_text_scopes_to_its_table(doc_path):
    result = docs_intel.locate_anchor(
        doc_path, {"caption_label": "Table 1", "text": "60"}
    )
    assert result["status"] == "resolved"
    assert result["element_type"] == "table_cell"
    assert result["quoted_text"] == "60"


def test_locate_anchor_unknown_caption_label_not_found(doc_path):
    result = docs_intel.locate_anchor(doc_path, {"caption_label": "Table 99"})
    assert result["status"] == "not_found"


# ---------------------------------------------------------------------------
# Direct para_id lookup, including round-tripping a synthetic table-cell id
# returned by an earlier query.
# ---------------------------------------------------------------------------

def test_locate_anchor_para_id_direct_heading(doc_path):
    result = docs_intel.locate_anchor(doc_path, {"para_id": "H0000001"})
    assert result["status"] == "resolved"
    assert result["quoted_text"] == "Introduction"
    assert result["leading_text_preview"] == "Introduction"
    assert result["first_words"] == "Introduction"
    assert result["bookmark_exists"] is False
    assert result["ref_status"]["checked"] is True


def test_locate_anchor_para_id_round_trips_table_cell_id(doc_path):
    first = docs_intel.locate_anchor(doc_path, {"section_path": "3.2.4", "text": "45"})
    assert first["status"] == "resolved"
    cell_id = first["target_para_id"]

    second = docs_intel.locate_anchor(doc_path, {"para_id": cell_id})
    assert second["status"] == "resolved"
    assert second["target_para_id"] == cell_id
    assert second["quoted_text"] == "45"


def test_locate_anchor_para_id_not_found(doc_path):
    result = docs_intel.locate_anchor(doc_path, {"para_id": "NOPE"})
    assert result["status"] == "not_found"


# ---------------------------------------------------------------------------
# Unscoped Ctrl+F text search, including the equation fallback.
# ---------------------------------------------------------------------------

def test_locate_anchor_unscoped_text_is_ambiguous_across_whole_document(doc_path):
    result = docs_intel.locate_anchor(doc_path, {"text": "Table"})
    assert result["status"] == "ambiguous"


def test_locate_anchor_unscoped_text_element_types_filters_to_one(doc_path):
    result = docs_intel.locate_anchor(
        doc_path, {"text": "Table", "element_types": ["heading"]}
    )
    assert result["status"] == "resolved"
    assert result["target_para_id"] == "H0000007"


def test_locate_anchor_equation_reachable_via_flat_text(doc_path):
    # The equation paragraph carries no <w:t> text at all (OMML only), so it
    # is only findable through the equation flat_text fallback.
    result = docs_intel.locate_anchor(doc_path, {"text": "E=mc2"})
    assert result["status"] == "resolved"
    assert result["element_type"] == "equation"
    assert result["target_para_id"] == "EQ0000001"
    assert result["quoted_text"] == "E=mc2"


def test_locate_anchor_text_not_found(doc_path):
    result = docs_intel.locate_anchor(doc_path, {"text": "zzz_absent_zzz"})
    assert result["status"] == "not_found"


# ---------------------------------------------------------------------------
# Preview normalization
# ---------------------------------------------------------------------------

def test_locate_anchor_preview_truncates_to_twelve_words(doc_path):
    result = docs_intel.locate_anchor(doc_path, {"para_id": "P0000008"})
    assert result["status"] == "resolved"
    preview = result["leading_text_preview"]
    assert preview.endswith("...")
    assert len(preview[:-3].split(" ")) == 12
    # quoted_text is verbatim -- normalization never touches the source.
    assert result["quoted_text"].startswith("Conclusion body paragraph")


# ---------------------------------------------------------------------------
# Staleness binding
# ---------------------------------------------------------------------------

def test_locate_anchor_expected_fingerprint_mismatch_is_stale(doc_path):
    result = docs_intel.locate_anchor(
        doc_path,
        {"para_id": "H0000001", "expected_source_fingerprint": "0" * 64},
    )
    assert result["status"] == "stale"
    assert result["reason"] == "source_fingerprint_mismatch"


def test_locate_anchor_expected_fingerprint_match_resolves(doc_path):
    first = docs_intel.locate_anchor(doc_path, {"para_id": "H0000001"})
    second = docs_intel.locate_anchor(
        doc_path,
        {
            "para_id": "H0000001",
            "expected_source_fingerprint": first["source_fingerprint"],
        },
    )
    assert second["status"] == "resolved"
    assert second["source_fingerprint"] == first["source_fingerprint"]


# ---------------------------------------------------------------------------
# Read-only invariant + error paths
# ---------------------------------------------------------------------------

def test_locate_anchor_never_mutates_document(doc_path):
    before = open(doc_path, "rb").read()
    docs_intel.locate_anchor(doc_path, {"section_path": "3.2.4", "text": "12"})
    docs_intel.locate_anchor(doc_path, {"caption_label": "Table 1"})
    after = open(doc_path, "rb").read()
    assert before == after


def test_locate_anchor_missing_file_returns_error():
    result = docs_intel.locate_anchor("C:/nonexistent/path/doc.docx", {"para_id": "X"})
    assert "error" in result


def test_locate_anchor_empty_query_returns_error(doc_path):
    result = docs_intel.locate_anchor(doc_path, {})
    assert "error" in result


def test_locate_anchor_query_with_no_recognised_key_is_not_found(doc_path):
    result = docs_intel.locate_anchor(doc_path, {"element_types": ["heading"]})
    assert result["status"] == "not_found"


# ---------------------------------------------------------------------------
# locate_anchors: multiple queries, one fresh-index pass, order preserved
# ---------------------------------------------------------------------------

def test_locate_anchors_preserves_order_and_shares_fingerprint(doc_path):
    single = docs_intel.locate_anchor(doc_path, {"para_id": "H0000001"})
    batch = docs_intel.locate_anchors(
        doc_path,
        [
            {"para_id": "H0000001"},
            {"caption_label": "Table 1"},
            {"section_path": "3.2.4", "text": "45"},
        ],
    )
    assert batch["query_count"] == 3
    assert batch["source_fingerprint"] == single["source_fingerprint"]
    results = batch["results"]
    assert len(results) == 3
    assert results[0]["target_para_id"] == "H0000001"
    assert results[1]["target_para_id"] == "T0000001"
    assert results[2]["element_type"] == "table_cell"
    for r in results:
        assert r["source_fingerprint"] == batch["source_fingerprint"]


def test_locate_anchors_resolves_independently_per_query(doc_path):
    # One bad query in the batch must not affect the others.
    batch = docs_intel.locate_anchors(
        doc_path,
        [
            {"para_id": "NOPE"},
            {"para_id": "H0000002"},
        ],
    )
    assert batch["results"][0]["status"] == "not_found"
    assert batch["results"][1]["status"] == "resolved"
    assert batch["results"][1]["target_para_id"] == "H0000002"


def test_locate_anchors_missing_file_returns_error():
    result = docs_intel.locate_anchors(
        "C:/nonexistent/path/doc.docx", [{"para_id": "X"}]
    )
    assert "error" in result


def test_locate_anchors_empty_queries_returns_error(doc_path):
    result = docs_intel.locate_anchors(doc_path, [])
    assert "error" in result
