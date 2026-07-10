"""DOCX citation-marker extraction (75d2196d).

Proves ``docparse.docs_intel`` (re-exported as ``meridian.docs_intel``) extracts
in-text citation markers from a .docx the way ``latex_intel`` surfaces LaTeX
``\\cite`` markers, and that ``meridian.doc_store.elements_from_docx_outline``
maps those markers onto ``kind='citation'`` store elements.

Two marker families are covered:

* **Reference-manager field codes** — Zotero (``ADDIN ZOTERO_ITEM
  CSL_CITATION {json}``) and Mendeley (``ADDIN CSL_CITATION {json}``) complex
  Word fields, including the run-split ``instrText`` and the ``fldSimple`` form.
* **Footnote / endnote references** — ``<w:footnoteReference>`` /
  ``<w:endnoteReference>``.

Everything is a synthetic in-memory .docx (a ZIP with a single
``word/document.xml``) so no third-party dependency (python-docx / lxml) is
needed — matching the existing ``tests/test_docs_intel.py`` convention.
"""
from __future__ import annotations

import io
import json
import zipfile
from xml.sax.saxutils import escape

from meridian import docs_intel
from meridian.doc_store import elements_from_docx_outline


# --- synthetic-docx builders -----------------------------------------------

_HEADER = (
    '<w:document '
    'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
    'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">'
)


def _docx_bytes(body_xml: str) -> bytes:
    """Wrap a ``<w:body>`` fragment into a minimal in-memory .docx."""
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"{_HEADER}<w:body>{body_xml}</w:body></w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", document_xml)
    return buf.getvalue()


def _heading(text: str, level: int = 1, para_id: str | None = None) -> str:
    pid = f' w14:paraId="{para_id}"' if para_id else ""
    return (
        f"<w:p{pid}><w:pPr><w:pStyle w:val=\"Heading{level}\"/></w:pPr>"
        f"<w:r><w:t>{escape(text)}</w:t></w:r></w:p>"
    )


def _complex_field_para(instruction: str, para_id: str, display: str = "(cite)") -> str:
    """A paragraph carrying one complex Word field (begin/instrText/sep/end).

    The instruction is split across two ``w:instrText`` runs to prove the parser
    concatenates run-split instructions (exactly how Word writes long CSL JSON).
    """
    mid = len(instruction) // 2
    part1, part2 = instruction[:mid], instruction[mid:]
    return (
        f'<w:p w14:paraId="{para_id}">'
        '<w:r><w:t>Body text </w:t></w:r>'
        '<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
        f"<w:r><w:instrText>{escape(part1)}</w:instrText></w:r>"
        f"<w:r><w:instrText>{escape(part2)}</w:instrText></w:r>"
        '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
        f"<w:r><w:t>{escape(display)}</w:t></w:r>"
        '<w:r><w:fldChar w:fldCharType="end"/></w:r>'
        "</w:p>"
    )


def _simple_field_para(instruction: str, para_id: str) -> str:
    return (
        f'<w:p w14:paraId="{para_id}">'
        f'<w:fldSimple w:instr="{escape(instruction, {chr(34): "&quot;"})}">'
        "<w:r><w:t>[1]</w:t></w:r></w:fldSimple></w:p>"
    )


def _note_ref_para(para_id: str, footnote_id: str, endnote_id: str) -> str:
    return (
        f'<w:p w14:paraId="{para_id}">'
        "<w:r><w:t>Claim.</w:t></w:r>"
        f'<w:r><w:footnoteReference w:id="{footnote_id}"/></w:r>'
        f'<w:r><w:endnoteReference w:id="{endnote_id}"/></w:r>'
        "</w:p>"
    )


# Realistic Zotero + Mendeley field instructions.
_ZOTERO_INSTR = "ADDIN ZOTERO_ITEM CSL_CITATION " + json.dumps(
    {
        "citationItems": [
            {
                "id": 1,
                "uris": ["http://zotero.org/users/1/items/ABCD1234"],
                "itemData": {
                    "id": "doe2020",
                    "DOI": "10.1000/xyz123",
                    "author": [{"family": "Doe", "given": "Jane"}],
                    "issued": {"date-parts": [[2020]]},
                },
            },
            {
                "id": 2,
                "uris": ["http://zotero.org/users/1/items/EFGH5678"],
                "itemData": {
                    "id": "roe2019",
                    "author": [{"family": "Roe"}],
                    "issued": {"date-parts": [[2019]]},
                },
            },
        ]
    }
)

_MENDELEY_INSTR = "ADDIN CSL_CITATION " + json.dumps(
    {
        "citationItems": [
            {"itemData": {"id": "smith2021", "ISBN": "978-0-13-468599-1"}}
        ],
        "mendeley": {"formattedCitation": "(Smith, 2021)"},
    }
)


def _rich_docx() -> bytes:
    body = (
        _heading("Introduction", 1, "H0001")
        + _complex_field_para(_ZOTERO_INSTR, "P0001")
        + _heading("Methods", 1, "H0002")
        + _simple_field_para(_MENDELEY_INSTR, "P0002")
        + _note_ref_para("P0003", "2", "5")
    )
    return _docx_bytes(body)


# --- premise: extraction exists and is exported ----------------------------

def test_parse_docx_citations_is_exported():
    # The new API is reachable through the meridian shim (not just docparse).
    assert hasattr(docs_intel, "parse_docx_citations")
    assert callable(docs_intel.parse_docx_citations)


def test_zotero_field_extracts_both_keys_run_split():
    body = _heading("Intro", 1, "H1") + _complex_field_para(_ZOTERO_INSTR, "P1")
    cites = docs_intel.parse_docx_citations(_docx_bytes(body))
    assert len(cites) == 1
    marker = cites[0]
    assert marker["kind"] == "citation"
    assert marker["source"] == "zotero"
    assert marker["para_id"] == "P1"
    # citation precedes no heading? No — heading H1 comes first, ordinal 0.
    assert marker["section_ordinal"] == 0
    # DOI wins for item 1; item 2 (no DOI/ISBN) falls back to its Zotero URI.
    assert marker["keys"] == [
        "10.1000/xyz123",
        "http://zotero.org/users/1/items/EFGH5678",
    ]
    # The raw ADDIN instruction is preserved as the marker text.
    assert "CSL_CITATION" in marker["marker_text"]


def test_mendeley_simple_field_extracts_key():
    body = _simple_field_para(_MENDELEY_INSTR, "P9")
    cites = docs_intel.parse_docx_citations(_docx_bytes(body))
    assert len(cites) == 1
    assert cites[0]["source"] == "mendeley"
    # No DOI; ISBN is the stable key.
    assert cites[0]["keys"] == ["978-0-13-468599-1"]
    # No heading precedes it.
    assert cites[0]["section_ordinal"] is None


def test_footnote_and_endnote_references_are_markers():
    body = _heading("H", 1, "H1") + _note_ref_para("P1", "7", "8")
    cites = docs_intel.parse_docx_citations(_docx_bytes(body))
    assert len(cites) == 2
    foot, end = cites
    assert foot["source"] == "footnote"
    assert foot["note_id"] == "7"
    assert foot["keys"] == ["7"]
    assert foot["marker_text"] == "[footnote 7]"
    assert end["source"] == "endnote"
    assert end["note_id"] == "8"
    assert end["keys"] == ["8"]


def test_document_order_and_section_tracking():
    cites = docs_intel.parse_docx_citations(_rich_docx())
    # zotero(1) under "Introduction"(ord 0), mendeley(1) under "Methods"(ord 1),
    # footnote + endnote(2) also under "Methods".
    assert [c["source"] for c in cites] == [
        "zotero",
        "mendeley",
        "footnote",
        "endnote",
    ]
    assert [c["section_ordinal"] for c in cites] == [0, 1, 1, 1]


def test_document_outline_surfaces_citations_additively():
    outline = docs_intel.document_outline(_rich_docx())
    # Existing keys are unchanged / still present.
    assert outline["heading_count"] == 2
    assert "headings" in outline and "fields" in outline
    # New additive keys.
    assert outline["citation_count"] == 4
    assert len(outline["citations"]) == 4
    assert outline["citations"][0]["source"] == "zotero"


def test_non_citation_addin_field_is_ignored():
    # A TOC / bookmark ADDIN (no CSL_CITATION token) is NOT a citation.
    non_cite = (
        '<w:p w14:paraId="P1">'
        '<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
        '<w:r><w:instrText> TOC \\o "1-3" \\h </w:instrText></w:r>'
        '<w:r><w:fldChar w:fldCharType="end"/></w:r>'
        "</w:p>"
    )
    cites = docs_intel.parse_docx_citations(_docx_bytes(non_cite))
    assert cites == []
    # But document_outline still surfaces it as a *field* (unchanged behaviour).
    outline = docs_intel.document_outline(_docx_bytes(non_cite))
    assert outline["field_count"] == 1
    assert outline["citation_count"] == 0


def test_malformed_source_degrades_to_empty():
    # Not a zip / not a docx -> [] (never raises).
    assert docs_intel.parse_docx_citations(b"not a zip") == []
    assert docs_intel.parse_docx_citations("nonexistent-path.docx") == []
    # A valid zip missing word/document.xml -> [].
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("other.xml", "<x/>")
    assert docs_intel.parse_docx_citations(buf.getvalue()) == []


def test_malformed_csl_json_still_surfaces_marker_with_no_keys():
    # Broken JSON payload: the marker is still surfaced (not dropped) with keys=[]
    instr = "ADDIN ZOTERO_ITEM CSL_CITATION {not valid json"
    body = _complex_field_para(instr, "P1")
    cites = docs_intel.parse_docx_citations(_docx_bytes(body))
    assert len(cites) == 1
    assert cites[0]["source"] == "zotero"
    assert cites[0]["keys"] == []


# --- doc_store mapping: citations become kind='citation' store elements -----

def test_elements_from_docx_outline_emits_citation_elements():
    outline = docs_intel.document_outline(_rich_docx())
    elements = elements_from_docx_outline(outline)

    headings = [e for e in elements if e["kind"] == "heading"]
    citations = [e for e in elements if e["kind"] == "citation"]

    assert [h["text"] for h in headings] == ["Introduction", "Methods"]
    # zotero(2 keys) + mendeley(1) + footnote(1) + endnote(1) = 5 citation elems.
    assert len(citations) == 5

    # Ordinals are contiguous and unique across the whole element list.
    ordinals = [e["ordinal"] for e in elements]
    assert ordinals == list(range(len(elements)))

    # Each key surfaces as a citation element ref.
    refs = {c["ref"] for c in citations}
    assert "10.1000/xyz123" in refs
    assert "http://zotero.org/users/1/items/EFGH5678" in refs
    assert "978-0-13-468599-1" in refs
    assert "2" in refs  # footnote note-id (from _rich_docx)
    assert "5" in refs  # endnote note-id (from _rich_docx)


def test_citation_parent_ordinal_points_at_enclosing_heading():
    outline = docs_intel.document_outline(_rich_docx())
    elements = elements_from_docx_outline(outline)
    # ordinal 0 = "Introduction" heading, ordinal 1 = "Methods" heading.
    by_ref = {e.get("ref"): e for e in elements if e["kind"] == "citation"}
    # Zotero DOI key lived under "Introduction" (heading ordinal 0).
    assert by_ref["10.1000/xyz123"]["parent_ordinal"] == 0
    # Mendeley ISBN lived under "Methods" (heading ordinal 1).
    assert by_ref["978-0-13-468599-1"]["parent_ordinal"] == 1
    # Footnote id (note id "2" from _rich_docx) under "Methods".
    assert by_ref["2"]["parent_ordinal"] == 1


def test_keyless_marker_still_emits_one_element():
    instr = "ADDIN ZOTERO_ITEM CSL_CITATION {broken"
    outline = docs_intel.document_outline(_docx_bytes(_complex_field_para(instr, "P1")))
    elements = elements_from_docx_outline(outline)
    citations = [e for e in elements if e["kind"] == "citation"]
    assert len(citations) == 1
    assert citations[0]["ref"] is None
    assert "CSL_CITATION" in citations[0]["text"]
