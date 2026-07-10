"""967bb99b — TOC / LOF / SEQ structured extraction + deterministic regeneration.

The base docx parser (a62e5b4f) *detected* field codes and flagged TOC/SEQ as
``needs_refresh`` but produced no structured representation of these field-driven
structures and could not regenerate their entry list. This suite covers the new
layer in ``docparse.docs_intel``:

* ``parse_field_switches`` — decode ``TOC \\o "1-3" \\h`` / ``SEQ Figure`` etc.
* ``regenerate_toc`` — rebuild the TOC entry list from live headings (level-scoped).
* ``regenerate_list_of_figures`` — renumber SEQ captions 1..N.
* ``document_field_structures`` — the classify + regenerate entry point.
* cached-result capture on complex/simple fields (purely additive to the parser).

All tests build a synthetic in-memory .docx (a ZIP with one word/document.xml) —
no third-party dependency, no network, no server, no sleeps.
"""
from __future__ import annotations

import io
import zipfile

from docparse import docs_intel


_NS = (
    'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
    'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml"'
)

# A document with: a TOC field (\o "1-3"), a table-of-figures TOC (\c "Figure"),
# three heading levels, and three SEQ-Figure captions (one inside a table) so the
# renumber walks the full content tree. SEQ #2's cached result is a STALE "5" to
# prove regeneration ignores baked-in numbers.
_DOCX_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<w:document {_NS}>
  <w:body>
    <w:p w14:paraId="A0000001">
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Contents</w:t></w:r>
    </w:p>
    <w:p w14:paraId="A0000002">
      <w:fldSimple w:instr=" TOC \\o &quot;1-3&quot; \\h \\z \\u ">
        <w:r><w:t>stale cached toc line</w:t></w:r>
      </w:fldSimple>
    </w:p>
    <w:p w14:paraId="A0000003">
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>List of Figures</w:t></w:r>
    </w:p>
    <w:p w14:paraId="A0000004">
      <w:r><w:fldChar w:fldCharType="begin"/></w:r>
      <w:r><w:instrText> TOC \\c &quot;Figure&quot; \\h </w:instrText></w:r>
      <w:r><w:fldChar w:fldCharType="separate"/></w:r>
      <w:r><w:t>Figure 1 old .... 4</w:t></w:r>
      <w:r><w:fldChar w:fldCharType="end"/></w:r>
    </w:p>
    <w:p w14:paraId="A0000005">
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Introduction</w:t></w:r>
    </w:p>
    <w:p w14:paraId="A0000006">
      <w:pPr><w:pStyle w:val="Heading2"/></w:pPr>
      <w:r><w:t>Background</w:t></w:r>
    </w:p>
    <w:p w14:paraId="A0000007">
      <w:pPr><w:pStyle w:val="Heading4"/></w:pPr>
      <w:r><w:t>Deep Detail (level 4, excluded by 1-3)</w:t></w:r>
    </w:p>
    <w:p w14:paraId="A0000008">
      <w:r><w:t>Figure </w:t></w:r>
      <w:r><w:fldChar w:fldCharType="begin"/></w:r>
      <w:r><w:instrText> SEQ Figure \\* ARABIC </w:instrText></w:r>
      <w:r><w:fldChar w:fldCharType="separate"/></w:r>
      <w:r><w:t>1</w:t></w:r>
      <w:r><w:fldChar w:fldCharType="end"/></w:r>
      <w:r><w:t>: first figure caption</w:t></w:r>
    </w:p>
    <w:p w14:paraId="A0000009">
      <w:r><w:t>Figure </w:t></w:r>
      <w:r><w:fldChar w:fldCharType="begin"/></w:r>
      <w:r><w:instrText> SEQ Figure \\* ARABIC </w:instrText></w:r>
      <w:r><w:fldChar w:fldCharType="separate"/></w:r>
      <w:r><w:t>5</w:t></w:r>
      <w:r><w:fldChar w:fldCharType="end"/></w:r>
      <w:r><w:t>: second figure caption (stale cached number)</w:t></w:r>
    </w:p>
    <w:tbl>
      <w:tr>
        <w:tc>
          <w:p w14:paraId="A000000A">
            <w:r><w:t>Figure </w:t></w:r>
            <w:r><w:fldChar w:fldCharType="begin"/></w:r>
            <w:r><w:instrText> SEQ Figure \\* ARABIC </w:instrText></w:r>
            <w:r><w:fldChar w:fldCharType="end"/></w:r>
            <w:r><w:t>: third figure, inside a table</w:t></w:r>
          </w:p>
        </w:tc>
      </w:tr>
    </w:tbl>
  </w:body>
</w:document>
"""


def _docx(xml: str = _DOCX_XML) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", xml)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# parse_field_switches
# ---------------------------------------------------------------------------

def test_parse_field_switches_toc_range_and_flags():
    parsed = docs_intel.parse_field_switches('TOC \\o "1-3" \\h \\z \\u')
    assert parsed["field_type"] == "TOC"
    assert parsed["args"] == []
    # \o carries an arg; \h \z \u are bare flags.
    assert parsed["switches"]["o"] == "1-3"
    assert parsed["switches"]["h"] is True
    assert parsed["switches"]["z"] is True
    assert parsed["switches"]["u"] is True


def test_parse_field_switches_seq_positional_arg():
    parsed = docs_intel.parse_field_switches("SEQ Figure \\* ARABIC")
    assert parsed["field_type"] == "SEQ"
    assert parsed["args"] == ["Figure"]
    # \* takes ARABIC as its arg.
    assert parsed["switches"]["*"] == "ARABIC"


def test_parse_field_switches_empty_is_safe():
    parsed = docs_intel.parse_field_switches("")
    assert parsed == {"field_type": None, "args": [], "switches": {}}


def test_tokenizer_keeps_quoted_spans_whole():
    toks = docs_intel._tokenize_field_instruction('TOC \\t "Caption,1" \\h')
    assert toks == ["TOC", "\\t", "Caption,1", "\\h"]


# ---------------------------------------------------------------------------
# cached-result capture (additive to the base parser)
# ---------------------------------------------------------------------------

def test_complex_field_captures_cached_result():
    paras = docs_intel.parse_docx(_docx())
    seq_paras = [
        p for p in paras
        if any(f["field_type"] == "SEQ" for f in p["fields"])
    ]
    # First two SEQ captions live in body paragraphs (the third is in a table).
    cached = [
        f["cached_result"]
        for p in seq_paras for f in p["fields"] if f["field_type"] == "SEQ"
    ]
    assert cached == ["1", "5"]  # stale rendered numbers, captured verbatim


def test_simple_field_captures_cached_result():
    paras = docs_intel.parse_docx(_docx())
    toc = [f for p in paras for f in p["fields"] if f["field_type"] == "TOC"]
    # fldSimple TOC keeps its cached child-run text.
    simple = [f for f in toc if f["cached_result"] == "stale cached toc line"]
    assert len(simple) == 1
    # Existing keys are untouched (additive).
    assert simple[0]["needs_refresh"] is True
    assert simple[0]["kind"] == "field"


# ---------------------------------------------------------------------------
# regenerate_toc
# ---------------------------------------------------------------------------

def test_regenerate_toc_scoped_to_levels_1_to_3():
    out = docs_intel.regenerate_toc(_docx(), level_range=(1, 3))
    assert out["structure_kind"] == "toc"
    assert out["level_range"] == [1, 3]
    texts = [e["text"] for e in out["entries"]]
    # Heading4 ("Deep Detail") is excluded; the rest are in document order.
    assert "Deep Detail (level 4, excluded by 1-3)" not in texts
    assert texts == [
        "Contents", "List of Figures", "Introduction", "Background",
    ]
    # Page numbers are honestly None (no layout engine).
    assert all(e["page"] is None for e in out["entries"])
    # Anchors are real paraIds.
    assert out["entries"][0]["para_id"] == "A0000001"


def test_regenerate_toc_unbounded_includes_all_levels():
    out = docs_intel.regenerate_toc(_docx())
    assert out["level_range"] is None
    # Heading4 is now included.
    assert any(e["level"] == 4 for e in out["entries"])
    assert out["entry_count"] == 5


# ---------------------------------------------------------------------------
# regenerate_list_of_figures
# ---------------------------------------------------------------------------

def test_regenerate_lof_renumbers_and_finds_table_caption():
    out = docs_intel.regenerate_list_of_figures(_docx(), seq_label="Figure")
    assert out["structure_kind"] == "list_of_figures"
    assert out["seq_label"] == "Figure"
    assert out["entry_count"] == 3
    # Deterministic 1..N renumber in document order — NOT the stale cached "5".
    assert [e["number"] for e in out["entries"]] == [1, 2, 3]
    # Stale cached number is preserved for diffing, not trusted for numbering.
    assert out["entries"][1]["cached_number"] == "5"
    # The third caption lives inside a table and is still found.
    assert "inside a table" in out["entries"][2]["text"]
    assert out["entries"][2]["para_id"] == "A000000A"
    assert all(e["page"] is None for e in out["entries"])


def test_regenerate_lof_unknown_label_is_empty_not_error():
    out = docs_intel.regenerate_list_of_figures(_docx(), seq_label="Table")
    assert out["entry_count"] == 0
    assert out["entries"] == []


# ---------------------------------------------------------------------------
# document_field_structures — the entry point
# ---------------------------------------------------------------------------

def test_document_field_structures_classifies_toc_and_lof():
    res = docs_intel.document_field_structures(_docx())
    assert res["has_toc"] is True
    assert res["has_list_of_figures"] is True

    kinds = {s["structure_kind"] for s in res["structures"]}
    assert kinds == {"toc", "list_of_figures"}

    toc = next(s for s in res["structures"] if s["structure_kind"] == "toc")
    assert toc["para_id"] == "A0000002"
    assert toc["level_range"] == [1, 3]
    assert toc["seq_label"] is None
    # Regenerated entry list is attached and level-scoped.
    assert toc["regenerated"]["entry_count"] == 4

    lof = next(
        s for s in res["structures"] if s["structure_kind"] == "list_of_figures"
    )
    assert lof["para_id"] == "A0000004"
    assert lof["seq_label"] == "Figure"
    assert lof["regenerated"]["entry_count"] == 3
    assert [e["number"] for e in lof["regenerated"]["entries"]] == [1, 2, 3]


def test_document_field_structures_inventories_seq_counters():
    res = docs_intel.document_field_structures(_docx())
    fig = next(c for c in res["seq_counters"] if c["label"] == "Figure")
    assert fig["occurrences"] == 3
    # Cached numbers: "1", "5", then None (the table caption had no separator).
    assert fig["cached_numbers"] == ["1", "5", None]


def test_document_field_structures_reports_honest_boundary():
    res = docs_intel.document_field_structures(_docx())
    low = res["boundary"].lower()
    assert "page numbers are none" in low
    assert "layout engine" in low and "out of scope" in low


def test_document_field_structures_empty_doc_is_clean():
    empty = f'<?xml version="1.0"?><w:document {_NS}><w:body></w:body></w:document>'
    res = docs_intel.document_field_structures(_docx(empty))
    assert res["has_toc"] is False
    assert res["has_list_of_figures"] is False
    assert res["structures"] == []
    assert res["seq_counters"] == []


def test_shim_reexports_new_symbols():
    # meridian.docs_intel is a compat shim over docparse.docs_intel; the new
    # public functions must be reachable through it too.
    from meridian import docs_intel as shim

    assert hasattr(shim, "document_field_structures")
    assert hasattr(shim, "regenerate_toc")
    assert hasattr(shim, "regenerate_list_of_figures")
    assert hasattr(shim, "parse_field_switches")
    out = shim.regenerate_toc(_docx(), level_range=(1, 1))
    assert all(e["level"] == 1 for e in out["entries"])
