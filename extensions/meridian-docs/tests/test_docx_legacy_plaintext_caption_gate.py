"""Tests for the docs-intel-jcshm-linter-gap-cleanup-20260918 whole-document
gate on _legacy_plaintext_caption_findings -- the "legacy_plaintext_caption"
false-positive fix.

Before this fix, ANY paragraph matching "Figure N"/"Table N" with no SEQ
field was flagged, regardless of whether the document uses native
(SEQ-field) captions ANYWHERE. That is correct for a document that is
genuinely MISSING some caption migrations (mixed convention), but a pure
false positive for a document whose own deliberate house style never uses
SEQ fields at all -- confirmed on this project's own real manuscript/SI,
where the same 5 plaintext-caption findings fired harmlessly on every
single build_document_review pass all project and were manually
re-verified as a non-issue 3+ separate times.

The fix: before the per-record loop, check whether ``records`` contains ANY
record classified as a native (SEQ-field) figure_caption/table_caption. Zero
native captions anywhere -> return [] immediately (this document's own
house style, not a defect). One or more native captions exist -> run the
existing per-paragraph detection unchanged (the real mixed-convention case
must still be caught).

Covers:
  - Zero native captions -> [].
  - Mixed native + plaintext -> still flags only the plaintext one(s).
  - A native TABLE caption alone still satisfies the gate for a plaintext
    FIGURE caption elsewhere (and vice versa) -- the gate is document-wide,
    not per-kind.
  - build_document_review's "caption" category reflects the same gating.

All tests use synthetic .docx bytes built inline -- no real files, no
network. Mirrors test_docx_caption_style_audit.py's fixture conventions.
"""
from __future__ import annotations

import io
import os
import sys
import zipfile

import pytest

_EXT_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _EXT_PATH not in sys.path:
    sys.path.insert(0, _EXT_PATH)

from meridian_docs import docs_intel  # noqa: E402

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W14 = "http://schemas.microsoft.com/office/word/2010/wordml"
_NS_HEADER = f'xmlns:w="{_W}" xmlns:w14="{_W14}"'


def _doc(body_xml: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<w:document {_NS_HEADER}>
  <w:body>
{body_xml}
  </w:body>
</w:document>"""


def _zip_docx(xml: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", xml)
    return buf.getvalue()


def _write_docx(tmp_path, xml: str, name: str = "sample.docx") -> str:
    path = tmp_path / name
    path.write_bytes(_zip_docx(xml))
    return str(path)


def _native_figure_caption(para_id: str, ref_name: str = "_Ref100000001") -> str:
    """A real native (SEQ-field) figure caption, matching the convention
    used elsewhere in this test suite (see test_docs_intel_new_primitives.py's
    _TWO_FIGURES_COLLIDING_XML)."""
    return (
        f'<w:p w14:paraId="{para_id}">'
        f'<w:pPr><w:pStyle w:val="Caption"/></w:pPr>'
        f'<w:bookmarkStart w:id="0" w:name="{ref_name}"/>'
        f'<w:r><w:t xml:space="preserve">Figure </w:t></w:r>'
        f'<w:fldSimple w:instr="SEQ Figure \\* ARABIC"><w:r><w:t>1</w:t></w:r></w:fldSimple>'
        f'<w:bookmarkEnd w:id="0"/>'
        f'<w:r><w:t xml:space="preserve">. A native caption.</w:t></w:r>'
        f"</w:p>"
    )


def _native_table_caption(para_id: str, ref_name: str = "_Ref100000002") -> str:
    return (
        f'<w:p w14:paraId="{para_id}">'
        f'<w:pPr><w:pStyle w:val="Caption"/></w:pPr>'
        f'<w:bookmarkStart w:id="0" w:name="{ref_name}"/>'
        f'<w:r><w:t xml:space="preserve">Table </w:t></w:r>'
        f'<w:fldSimple w:instr="SEQ Table \\* ARABIC"><w:r><w:t>1</w:t></w:r></w:fldSimple>'
        f'<w:bookmarkEnd w:id="0"/>'
        f'<w:r><w:t xml:space="preserve">. A native table caption.</w:t></w:r>'
        f"</w:p>"
    )


def _plaintext_figure_caption(number: int, para_id: str) -> str:
    return (
        f'<w:p w14:paraId="{para_id}">'
        f'<w:r><w:t xml:space="preserve">Figure {number}. A plaintext caption with no SEQ field.</w:t></w:r>'
        f"</w:p>"
    )


def _plaintext_table_caption(number: int, para_id: str) -> str:
    return (
        f'<w:p w14:paraId="{para_id}">'
        f'<w:r><w:t xml:space="preserve">Table {number}. A plaintext table caption with no SEQ field.</w:t></w:r>'
        f"</w:p>"
    )


def _body_para(text: str, para_id: str) -> str:
    return f'<w:p w14:paraId="{para_id}"><w:r><w:t>{text}</w:t></w:r></w:p>'


def _records(path: str) -> list[dict]:
    with open(path, "rb") as fh:
        raw = fh.read()
    records, _tree = docs_intel._iter_anchor_records(raw)
    return records


# ---------------------------------------------------------------------------
# The gate itself
# ---------------------------------------------------------------------------

def test_zero_native_captions_document_produces_no_legacy_findings(tmp_path):
    """The exact real-project false-positive scenario: a document whose own
    convention is plain literal caption numbers everywhere, never SEQ
    fields -- must NOT be flagged at all."""
    body = (
        _body_para("Some introductory text.", "00000001")
        + _plaintext_figure_caption(1, "00000002")
        + _plaintext_table_caption(1, "00000003")
    )
    path = _write_docx(tmp_path, _doc(body))
    records = _records(path)
    assert docs_intel._legacy_plaintext_caption_findings(records) == []


def test_mixed_native_and_plaintext_still_flags_only_the_plaintext_one(tmp_path):
    """The real mixed-convention case this gate must NOT break: a document
    that has at least one native SEQ-field caption elsewhere still gets its
    genuinely-missed plaintext caption flagged."""
    body = (
        _native_figure_caption("00000001")
        + _plaintext_figure_caption(2, "00000002")
    )
    path = _write_docx(tmp_path, _doc(body))
    records = _records(path)
    findings = docs_intel._legacy_plaintext_caption_findings(records)
    assert len(findings) == 1
    assert findings[0]["type"] == "legacy_plaintext_caption"
    assert findings[0]["para_id"] == "00000002"
    assert findings[0]["old_cached_number"] == "2"


def test_native_table_caption_alone_satisfies_the_gate_for_a_plaintext_figure(tmp_path):
    """The gate is DOCUMENT-WIDE, not per-kind: a native TABLE caption
    still counts as "this document uses native captions" for a plaintext
    FIGURE caption elsewhere."""
    body = (
        _native_table_caption("00000001")
        + _plaintext_figure_caption(1, "00000002")
    )
    path = _write_docx(tmp_path, _doc(body))
    records = _records(path)
    findings = docs_intel._legacy_plaintext_caption_findings(records)
    assert len(findings) == 1
    assert findings[0]["kind"] == "Figure"
    assert findings[0]["para_id"] == "00000002"


def test_multiple_plaintext_captions_all_flagged_once_gate_is_open(tmp_path):
    body = (
        _native_figure_caption("00000001")
        + _plaintext_figure_caption(2, "00000002")
        + _plaintext_table_caption(3, "00000003")
    )
    path = _write_docx(tmp_path, _doc(body))
    records = _records(path)
    findings = docs_intel._legacy_plaintext_caption_findings(records)
    assert len(findings) == 2
    kinds = {f["kind"] for f in findings}
    assert kinds == {"Figure", "Table"}


def test_non_caption_paragraphs_never_flagged_either_way(tmp_path):
    body = _body_para("Figures and tables are discussed throughout this paper.", "00000001")
    path = _write_docx(tmp_path, _doc(body))
    records = _records(path)
    assert docs_intel._legacy_plaintext_caption_findings(records) == []


# ---------------------------------------------------------------------------
# build_document_review wiring
# ---------------------------------------------------------------------------

def test_build_document_review_omits_legacy_finding_when_document_has_no_native_captions(tmp_path):
    body = _plaintext_figure_caption(1, "00000001")
    path = _write_docx(tmp_path, _doc(body))
    review = docs_intel.build_document_review(path)
    assert review["status"] == "ok"
    types = {f["type"] for f in review["findings"]}
    assert "legacy_plaintext_caption" not in types


def test_build_document_review_still_flags_legacy_caption_when_native_captions_exist(tmp_path):
    body = _native_figure_caption("00000001") + _plaintext_figure_caption(2, "00000002")
    path = _write_docx(tmp_path, _doc(body))
    review = docs_intel.build_document_review(path)
    assert review["status"] == "ok"
    legacy_findings = [f for f in review["findings"] if f["type"] == "legacy_plaintext_caption"]
    assert len(legacy_findings) == 1
    assert legacy_findings[0]["category"] == "caption"
    assert legacy_findings[0]["locator"]["status"] == "resolved"
    assert legacy_findings[0]["locator"]["target_para_id"] == "00000002"
