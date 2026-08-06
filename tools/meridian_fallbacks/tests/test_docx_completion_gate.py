"""Tests for tools/meridian_fallbacks/docx_completion_gate.py (8419f55f).

Every fixture here is a small, disposable, synthetic .docx built in-memory
via ``zipfile`` -- no canonical/real document is ever touched, matching the
sprint item's "disposable DOCX fixtures only" requirement.

``docx_completion_gate`` is imported straight off its file path (via a
sys.path insertion of its containing directory), the same pattern
``tests/test_meridian_docs_equations.py`` already uses for
``extensions/meridian-docs`` -- this deliberately never assumes
``tools/meridian_fallbacks/__init__.py`` exists or is importable as a
package, since a sibling sprint item owns that file.

Word/COM safety note: this repo's CI runs on Linux, but a self-hosted
Windows dev machine can have real Word + pywin32 installed (verified true
for at least one such machine). Every test that touches the render-receipt
path therefore injects an explicit fake ``render_checker`` (or monkeypatches
the guarded ``importlib.import_module`` call inside
``_default_render_checker``) rather than ever letting the default checker
reach real Word COM automation -- exactly the discipline
``extensions/meridian-docs/tests/test_docx_render_gate.py`` already follows
for the sibling ``check_word_com_render_receipt`` helper.
"""
from __future__ import annotations

import io
import json
import os
import struct
import sys
import time
import zipfile

import pytest

_MODULE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _MODULE_DIR not in sys.path:
    sys.path.insert(0, _MODULE_DIR)

import docx_completion_gate as gate  # noqa: E402


# ---------------------------------------------------------------------------
# Fixture builders.
# ---------------------------------------------------------------------------

_CONTENT_TYPES_XML = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Default Extension="png" ContentType="image/png"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""

_ROOT_RELS_XML = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""

_DOCUMENT_XML_TEMPLATE = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
            xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml"
            xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
            xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
            xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math">
  <w:body>
{body}
    <w:sectPr/>
  </w:body>
</w:document>"""

_DOCUMENT_RELS_TEMPLATE = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
{rels}
</Relationships>"""


def _build_docx(
    tmp_path,
    name: str = "doc.docx",
    *,
    body_xml: str = "",
    document_rels_xml: "str | None" = None,
    media: "dict[str, bytes] | None" = None,
    include_content_types: bool = True,
    include_root_rels: bool = True,
    include_document_xml: bool = True,
    document_xml_override: "bytes | None" = None,
) -> str:
    path = tmp_path / name
    with zipfile.ZipFile(str(path), "w", zipfile.ZIP_DEFLATED) as zf:
        if include_content_types:
            zf.writestr("[Content_Types].xml", _CONTENT_TYPES_XML)
        if include_root_rels:
            zf.writestr("_rels/.rels", _ROOT_RELS_XML)
        if include_document_xml:
            if document_xml_override is not None:
                zf.writestr("word/document.xml", document_xml_override)
            else:
                zf.writestr(
                    "word/document.xml",
                    _DOCUMENT_XML_TEMPLATE.format(body=body_xml).encode("utf-8"),
                )
        if document_rels_xml is not None:
            zf.writestr("word/_rels/document.xml.rels", document_rels_xml.encode("utf-8"))
        for media_name, data in (media or {}).items():
            zf.writestr(
                zipfile.ZipInfo(f"word/media/{media_name}"), data, zipfile.ZIP_STORED
            )
    return str(path)


def _para(para_id: "str | None", text: str) -> str:
    attr = f' w14:paraId="{para_id}"' if para_id else ""
    return f'<w:p{attr}><w:r><w:t>{text}</w:t></w:r></w:p>'


def _caption_para(para_id: str, kind: str = "Figure") -> str:
    return (
        f'<w:p w14:paraId="{para_id}"><w:pPr><w:pStyle w:val="Caption"/></w:pPr>'
        f'<w:fldSimple w:instr=" SEQ {kind} \\* ARABIC "><w:r><w:t>1</w:t></w:r></w:fldSimple>'
        f'</w:p>'
    )


def _equation_para(para_id: str) -> str:
    return (
        f'<w:p w14:paraId="{para_id}"><m:oMath><m:r><m:t>x</m:t></m:r></m:oMath></w:p>'
    )


def _image_para(para_id: str, rid: str) -> str:
    return f'<w:p w14:paraId="{para_id}"><w:r><w:drawing><a:blip r:embed="{rid}"/></w:drawing></w:r></w:p>'


def _table(rows: "list[list[str | None]]") -> str:
    trs = []
    for row in rows:
        tcs = "".join(f"<w:tc>{_para(pid, 'cell')}</w:tc>" for pid in row)
        trs.append(f"<w:tr>{tcs}</w:tr>")
    return f"<w:tbl>{''.join(trs)}</w:tbl>"


def _corrupt_stored_entry(raw: bytes, entry_name: str) -> bytes:
    """Flip one byte inside a ZIP_STORED entry's data (leaving the recorded
    CRC-32 in the local header untouched), producing a genuine CRC mismatch
    on read -- the same defect class :func:`check_zip_integrity` exists to
    catch."""
    buf = bytearray(raw)
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        info = zf.getinfo(entry_name)
        assert info.compress_type == zipfile.ZIP_STORED, "corruption helper requires ZIP_STORED"
    offset = info.header_offset
    fname_len, extra_len = struct.unpack("<HH", buf[offset + 26 : offset + 30])
    data_start = offset + 30 + fname_len + extra_len
    buf[data_start] ^= 0xFF
    return bytes(buf)


_VALID_BODY = "\n".join(
    [
        _para("P0000001", "Introduction"),
        _para("P0000002", "Body text."),
    ]
)


# ---------------------------------------------------------------------------
# check_zip_integrity
# ---------------------------------------------------------------------------

def test_check_zip_integrity_valid_docx_ok(tmp_path):
    path = _build_docx(tmp_path, body_xml=_VALID_BODY)
    raw = open(path, "rb").read()

    result = gate.check_zip_integrity(raw)

    assert result["ok"] is True
    assert result["bad_crc_entries"] == []
    assert result["malformed_xml_entries"] == []
    assert result["missing_required_parts"] == []
    assert result["entry_count"] >= 3


def test_check_zip_integrity_detects_bad_crc(tmp_path):
    path = _build_docx(tmp_path, body_xml=_VALID_BODY, media={"image1.png": b"real-png-bytes"})
    raw = open(path, "rb").read()
    corrupted = _corrupt_stored_entry(raw, "word/media/image1.png")

    result = gate.check_zip_integrity(corrupted)

    assert result["ok"] is False
    assert any(e["name"] == "word/media/image1.png" for e in result["bad_crc_entries"])


def test_check_zip_integrity_detects_malformed_xml(tmp_path):
    path = _build_docx(
        tmp_path, document_xml_override=b"<w:document><w:body></w:document>"
    )
    raw = open(path, "rb").read()

    result = gate.check_zip_integrity(raw)

    assert result["ok"] is False
    assert any(e["name"] == "word/document.xml" for e in result["malformed_xml_entries"])


def test_check_zip_integrity_detects_missing_required_parts(tmp_path):
    path = _build_docx(tmp_path, body_xml=_VALID_BODY, include_root_rels=False)
    raw = open(path, "rb").read()

    result = gate.check_zip_integrity(raw)

    assert result["ok"] is False
    assert "_rels/.rels" in result["missing_required_parts"]


def test_check_zip_integrity_bad_zip_bytes_returns_error_not_raise():
    result = gate.check_zip_integrity(b"this is not a zip file at all")

    assert result["ok"] is False
    assert result["error"] is not None


# ---------------------------------------------------------------------------
# enumerate_para_ids
# ---------------------------------------------------------------------------

def test_enumerate_para_ids_body_only():
    xml = _DOCUMENT_XML_TEMPLATE.format(body=_VALID_BODY).encode("utf-8")

    result = gate.enumerate_para_ids(xml)

    assert result["body_paragraph_count"] == 2
    assert result["table_cell_paragraph_count"] == 0
    assert result["body_missing_para_id_count"] == 0
    assert result["duplicate_para_ids"] == []
    assert result["total_unique_para_ids"] == 2


def test_enumerate_para_ids_separates_table_cell_paragraphs():
    body = _para("P0000001", "before table") + _table(
        [["T0000001", "T0000002"], ["T0000003", None]]
    )
    xml = _DOCUMENT_XML_TEMPLATE.format(body=body).encode("utf-8")

    result = gate.enumerate_para_ids(xml)

    assert result["body_paragraph_count"] == 1
    assert result["table_cell_paragraph_count"] == 4
    assert result["table_cell_missing_para_id_count"] == 1
    assert result["body_missing_para_id_count"] == 0


def test_enumerate_para_ids_flags_duplicates_across_table_and_body():
    body = _para("DUPLICATE_ID", "body para") + _table([["DUPLICATE_ID"]])
    xml = _DOCUMENT_XML_TEMPLATE.format(body=body).encode("utf-8")

    result = gate.enumerate_para_ids(xml)

    assert result["duplicate_para_ids"] == ["DUPLICATE_ID"]
    assert result["total_unique_para_ids"] == 1


def test_enumerate_para_ids_missing_para_id_counted_not_treated_as_duplicate():
    body = _para(None, "no id 1") + _para(None, "no id 2")
    xml = _DOCUMENT_XML_TEMPLATE.format(body=body).encode("utf-8")

    result = gate.enumerate_para_ids(xml)

    assert result["body_missing_para_id_count"] == 2
    assert result["duplicate_para_ids"] == []


def test_enumerate_para_ids_raises_parse_error_on_malformed_xml():
    with pytest.raises(Exception):
        gate.enumerate_para_ids(b"<w:document><unclosed>")


# ---------------------------------------------------------------------------
# check_relationship_reachability
# ---------------------------------------------------------------------------

def test_relationship_reachability_valid_image_ok(tmp_path):
    body = _image_para("IMG0000001", "rId2")
    rels = _DOCUMENT_RELS_TEMPLATE.format(
        rels='<Relationship Id="rId2" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
        'Target="media/image1.png"/>'
    )
    path = _build_docx(
        tmp_path, body_xml=body, document_rels_xml=rels, media={"image1.png": b"png-data"}
    )
    raw = open(path, "rb").read()

    result = gate.check_relationship_reachability(raw)

    assert result["ok"] is True
    assert result["unresolved_relationship_targets"] == []
    assert result["unresolved_rids"] == []
    assert result["orphaned_media"] == []


def test_relationship_reachability_unresolved_target(tmp_path):
    rels = _DOCUMENT_RELS_TEMPLATE.format(
        rels='<Relationship Id="rId2" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
        'Target="media/does_not_exist.png"/>'
    )
    path = _build_docx(tmp_path, body_xml=_VALID_BODY, document_rels_xml=rels)
    raw = open(path, "rb").read()

    result = gate.check_relationship_reachability(raw)

    assert result["ok"] is False
    assert len(result["unresolved_relationship_targets"]) == 1
    assert result["unresolved_relationship_targets"][0]["resolved"] == "word/media/does_not_exist.png"


def test_relationship_reachability_unresolved_rid(tmp_path):
    # The document references rId99, but no relationship named rId99 exists.
    body = _image_para("IMG0000001", "rId99")
    rels = _DOCUMENT_RELS_TEMPLATE.format(rels="")
    path = _build_docx(tmp_path, body_xml=body, document_rels_xml=rels)
    raw = open(path, "rb").read()

    result = gate.check_relationship_reachability(raw)

    assert result["ok"] is False
    assert any(e["rid"] == "rId99" for e in result["unresolved_rids"])


def test_relationship_reachability_orphaned_media(tmp_path):
    # image1.png exists on disk but nothing references it.
    rels = _DOCUMENT_RELS_TEMPLATE.format(rels="")
    path = _build_docx(
        tmp_path,
        body_xml=_VALID_BODY,
        document_rels_xml=rels,
        media={"image1.png": b"png-data"},
    )
    raw = open(path, "rb").read()

    result = gate.check_relationship_reachability(raw)

    assert result["ok"] is False
    assert "word/media/image1.png" in result["orphaned_media"]


def test_relationship_reachability_external_target_not_flagged(tmp_path):
    body = '<w:p><w:hyperlink r:id="rId5"><w:r><w:t>link</w:t></w:r></w:hyperlink></w:p>'
    rels = _DOCUMENT_RELS_TEMPLATE.format(
        rels='<Relationship Id="rId5" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" '
        'Target="https://example.com/" TargetMode="External"/>'
    )
    path = _build_docx(tmp_path, body_xml=body, document_rels_xml=rels)
    raw = open(path, "rb").read()

    result = gate.check_relationship_reachability(raw)

    assert result["ok"] is True
    assert result["unresolved_relationship_targets"] == []
    assert result["unresolved_rids"] == []


# ---------------------------------------------------------------------------
# count_equations_and_captions
# ---------------------------------------------------------------------------

def test_count_equations_and_captions_zero_on_plain_doc():
    xml = _DOCUMENT_XML_TEMPLATE.format(body=_VALID_BODY).encode("utf-8")

    result = gate.count_equations_and_captions(xml)

    assert result["equation_count"] == 0
    assert result["caption_style_paragraph_count"] == 0
    assert result["seq_field_count"] == 0


def test_count_equations_and_captions_counts_equations():
    body = _equation_para("EQ0000001") + _equation_para("EQ0000002")
    xml = _DOCUMENT_XML_TEMPLATE.format(body=body).encode("utf-8")

    result = gate.count_equations_and_captions(xml)

    assert result["equation_count"] == 2


def test_count_equations_and_captions_counts_caption_style_and_seq_field():
    body = _caption_para("CAP0000001", "Figure") + _caption_para("CAP0000002", "Table")
    xml = _DOCUMENT_XML_TEMPLATE.format(body=body).encode("utf-8")

    result = gate.count_equations_and_captions(xml)

    assert result["caption_style_paragraph_count"] == 2
    assert result["seq_field_count"] == 2


def test_count_equations_and_captions_complex_field_instr_text():
    body = (
        '<w:p w14:paraId="CAP0000003"><w:pPr><w:pStyle w:val="Caption"/></w:pPr>'
        '<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
        '<w:r><w:instrText xml:space="preserve"> SEQ Table \\* ARABIC </w:instrText></w:r>'
        '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
        '<w:r><w:t>1</w:t></w:r>'
        '<w:r><w:fldChar w:fldCharType="end"/></w:r>'
        '</w:p>'
    )
    xml = _DOCUMENT_XML_TEMPLATE.format(body=body).encode("utf-8")

    result = gate.count_equations_and_captions(xml)

    assert result["seq_field_count"] == 1
    assert result["caption_style_paragraph_count"] == 1


# ---------------------------------------------------------------------------
# check_required_text
# ---------------------------------------------------------------------------

def test_check_required_text_all_present():
    xml = _DOCUMENT_XML_TEMPLATE.format(body=_VALID_BODY).encode("utf-8")

    result = gate.check_required_text(xml, required_texts=["Introduction", "Body"])

    assert result["ok"] is True
    assert result["missing_required_texts"] == []


def test_check_required_text_reports_missing():
    xml = _DOCUMENT_XML_TEMPLATE.format(body=_VALID_BODY).encode("utf-8")

    result = gate.check_required_text(xml, required_texts=["Introduction", "Nonexistent Text"])

    assert result["ok"] is False
    assert result["missing_required_texts"] == ["Nonexistent Text"]


def test_check_required_text_locator_match():
    xml = _DOCUMENT_XML_TEMPLATE.format(body=_VALID_BODY).encode("utf-8")

    result = gate.check_required_text(
        xml, required_locators=[("P0000001", "Introduction")]
    )

    assert result["ok"] is True
    assert result["missing_required_locators"] == []


def test_check_required_text_locator_para_id_not_found():
    xml = _DOCUMENT_XML_TEMPLATE.format(body=_VALID_BODY).encode("utf-8")

    result = gate.check_required_text(
        xml, required_locators=[("DOES_NOT_EXIST", "whatever")]
    )

    assert result["ok"] is False
    assert result["missing_required_locators"][0]["reason"] == "paraId not found in document"


def test_check_required_text_locator_text_mismatch():
    xml = _DOCUMENT_XML_TEMPLATE.format(body=_VALID_BODY).encode("utf-8")

    result = gate.check_required_text(
        xml, required_locators=[("P0000001", "This is not the actual text")]
    )

    assert result["ok"] is False
    reason = result["missing_required_locators"][0]["reason"]
    assert "does not contain expected substring" in reason


# ---------------------------------------------------------------------------
# check_stale_source
# ---------------------------------------------------------------------------

def test_check_stale_source_no_signals_not_stale(tmp_path):
    path = _build_docx(tmp_path, body_xml=_VALID_BODY)

    result = gate.check_stale_source(path)

    assert result["ok"] is True
    assert result["stale"] is False


def test_check_stale_source_missing_docx_path_is_stale(tmp_path):
    result = gate.check_stale_source(str(tmp_path / "does_not_exist.docx"))

    assert result["ok"] is False
    assert result["stale"] is True
    assert "could not stat docx_path" in result["reasons"][0]


def test_check_stale_source_source_newer_than_docx_is_stale(tmp_path):
    docx_path = _build_docx(tmp_path, body_xml=_VALID_BODY)
    source_path = tmp_path / "source.md"
    source_path.write_text("source content")

    old_time = time.time() - 3600
    os.utime(docx_path, (old_time, old_time))

    result = gate.check_stale_source(docx_path, source_path=str(source_path))

    assert result["stale"] is True
    assert any("not newer than source_path" in r for r in result["reasons"])


def test_check_stale_source_docx_newer_than_source_not_stale(tmp_path):
    source_path = tmp_path / "source.md"
    source_path.write_text("source content")
    old_time = time.time() - 3600
    os.utime(source_path, (old_time, old_time))
    docx_path = _build_docx(tmp_path, body_xml=_VALID_BODY)

    result = gate.check_stale_source(docx_path, source_path=str(source_path))

    assert result["stale"] is False


def test_check_stale_source_sha256_mismatch_is_stale(tmp_path):
    source_path = tmp_path / "source.md"
    source_path.write_text("original content")
    docx_path = _build_docx(tmp_path, body_xml=_VALID_BODY)

    result = gate.check_stale_source(
        docx_path, source_path=str(source_path), expected_source_sha256="0" * 64
    )

    assert result["stale"] is True
    assert any("does not match expected_source_sha256" in r for r in result["reasons"])


def test_check_stale_source_sha256_match_not_stale(tmp_path):
    import hashlib

    source_path = tmp_path / "source.md"
    source_path.write_text("original content")
    expected = hashlib.sha256(source_path.read_bytes()).hexdigest()
    docx_path = _build_docx(tmp_path, body_xml=_VALID_BODY)

    result = gate.check_stale_source(
        docx_path, source_path=str(source_path), expected_source_sha256=expected
    )

    assert result["stale"] is False


def test_check_stale_source_expected_min_mtime_future_is_stale(tmp_path):
    docx_path = _build_docx(tmp_path, body_xml=_VALID_BODY)
    future = time.time() + 3600

    result = gate.check_stale_source(docx_path, expected_min_mtime=future)

    assert result["stale"] is True
    assert any("expected_min_mtime" in r for r in result["reasons"])


# ---------------------------------------------------------------------------
# run_completion_gate -- integration.
# ---------------------------------------------------------------------------

def _fake_render_checker(status: str, **extra):
    def _checker(docx_path: str):
        return {"status": status, **extra}

    return _checker


def test_run_completion_gate_fully_valid_docx_is_ready(tmp_path):
    body = _para("P0000001", "Hello world") + _caption_para("CAP0000001") + _equation_para(
        "EQ0000001"
    )
    path = _build_docx(tmp_path, body_xml=body)
    requirements = gate.CompletionRequirements(
        required_texts=["Hello world"],
        required_locators=[("P0000001", "Hello")],
        min_equation_count=1,
        min_caption_count=1,
    )

    report = gate.run_completion_gate(
        path,
        requirements,
        render_checker=_fake_render_checker(gate.RENDER_VERIFIED, backend="word-com"),
    )

    assert report["ready"] is True, report["reasons"]
    assert report["reasons"] == []
    assert report["zip_integrity"]["ok"] is True
    assert report["render_report"]["status"] == gate.RENDER_VERIFIED


def test_run_completion_gate_missing_docx_path(tmp_path):
    report = gate.run_completion_gate(str(tmp_path / "nope.docx"))

    assert report["ready"] is False
    assert any("does not exist" in r for r in report["reasons"])


def test_run_completion_gate_empty_docx_path():
    report = gate.run_completion_gate("")

    assert report["ready"] is False
    assert "docx_path must be a non-empty string" in report["reasons"]


def test_run_completion_gate_duplicate_para_ids_blocks_ready(tmp_path):
    body = _para("DUP", "one") + _para("DUP", "two")
    path = _build_docx(tmp_path, body_xml=body)

    report = gate.run_completion_gate(
        path, render_checker=_fake_render_checker(gate.RENDER_VERIFIED)
    )

    assert report["ready"] is False
    assert any("duplicate_para_ids_found" in r for r in report["reasons"])


def test_run_completion_gate_bad_crc_blocks_ready(tmp_path):
    path = _build_docx(
        tmp_path, body_xml=_VALID_BODY, media={"image1.png": b"real-png-bytes"}
    )
    raw = open(path, "rb").read()
    corrupted = _corrupt_stored_entry(raw, "word/media/image1.png")
    with open(path, "wb") as f:
        f.write(corrupted)

    report = gate.run_completion_gate(
        path, render_checker=_fake_render_checker(gate.RENDER_VERIFIED)
    )

    assert report["ready"] is False
    assert "zip_integrity_failed" in report["reasons"]


def test_run_completion_gate_unresolved_relationship_blocks_ready(tmp_path):
    rels = _DOCUMENT_RELS_TEMPLATE.format(
        rels='<Relationship Id="rId2" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
        'Target="media/missing.png"/>'
    )
    path = _build_docx(tmp_path, body_xml=_VALID_BODY, document_rels_xml=rels)

    report = gate.run_completion_gate(
        path, render_checker=_fake_render_checker(gate.RENDER_VERIFIED)
    )

    assert report["ready"] is False
    assert "relationship_reachability_failed" in report["reasons"]


def test_run_completion_gate_required_text_missing_blocks_ready(tmp_path):
    path = _build_docx(tmp_path, body_xml=_VALID_BODY)
    requirements = gate.CompletionRequirements(required_texts=["not present anywhere"])

    report = gate.run_completion_gate(
        path, requirements, render_checker=_fake_render_checker(gate.RENDER_VERIFIED)
    )

    assert report["ready"] is False
    assert "required_text_or_locator_missing" in report["reasons"]


def test_run_completion_gate_min_equation_count_not_met(tmp_path):
    path = _build_docx(tmp_path, body_xml=_VALID_BODY)
    requirements = gate.CompletionRequirements(min_equation_count=1)

    report = gate.run_completion_gate(
        path, requirements, render_checker=_fake_render_checker(gate.RENDER_VERIFIED)
    )

    assert report["ready"] is False
    assert any("equation_count" in r for r in report["reasons"])


def test_run_completion_gate_min_caption_count_not_met(tmp_path):
    path = _build_docx(tmp_path, body_xml=_VALID_BODY)
    requirements = gate.CompletionRequirements(min_caption_count=1)

    report = gate.run_completion_gate(
        path, requirements, render_checker=_fake_render_checker(gate.RENDER_VERIFIED)
    )

    assert report["ready"] is False
    assert any("caption_style_paragraph_count" in r for r in report["reasons"])


def test_run_completion_gate_stale_source_blocks_ready(tmp_path):
    path = _build_docx(tmp_path, body_xml=_VALID_BODY)
    source_path = tmp_path / "source.md"
    source_path.write_text("source")
    old_time = time.time() - 3600
    os.utime(path, (old_time, old_time))
    requirements = gate.CompletionRequirements(source_path=str(source_path))

    report = gate.run_completion_gate(
        path, requirements, render_checker=_fake_render_checker(gate.RENDER_VERIFIED)
    )

    assert report["ready"] is False
    assert any("stale_source_refused" in r for r in report["reasons"])


def test_run_completion_gate_require_render_verified_blocks_when_unavailable(tmp_path):
    path = _build_docx(tmp_path, body_xml=_VALID_BODY)
    requirements = gate.CompletionRequirements(require_render_verified=True)

    report = gate.run_completion_gate(
        path,
        requirements,
        render_checker=_fake_render_checker(
            gate.RENDER_UNAVAILABLE, reason="non-Windows platform"
        ),
    )

    assert report["ready"] is False
    assert report["render_report"]["status"] == gate.RENDER_UNAVAILABLE
    assert any("require_render_verified" in r for r in report["reasons"])


def test_run_completion_gate_require_render_verified_passes_when_verified(tmp_path):
    path = _build_docx(tmp_path, body_xml=_VALID_BODY)
    requirements = gate.CompletionRequirements(require_render_verified=True)

    report = gate.run_completion_gate(
        path,
        requirements,
        render_checker=_fake_render_checker(gate.RENDER_VERIFIED, backend="word-com"),
    )

    assert report["ready"] is True
    assert report["render_report"]["status"] == gate.RENDER_VERIFIED


def test_run_completion_gate_skip_render_check_is_unverified_not_success(tmp_path):
    path = _build_docx(tmp_path, body_xml=_VALID_BODY)

    report = gate.run_completion_gate(path, skip_render_check=True)

    assert report["render_report"]["status"] == gate.RENDER_UNVERIFIED
    # Not requiring verified render, so this alone must not block readiness.
    assert report["ready"] is True


def test_run_completion_gate_render_checker_invalid_result_becomes_unverified(tmp_path):
    path = _build_docx(tmp_path, body_xml=_VALID_BODY)

    report = gate.run_completion_gate(
        path, render_checker=lambda docx_path: {"status": "not-a-real-status"}
    )

    assert report["render_report"]["status"] == gate.RENDER_UNVERIFIED


def test_run_completion_gate_render_checker_raising_becomes_failed(tmp_path):
    path = _build_docx(tmp_path, body_xml=_VALID_BODY)

    def _boom(docx_path):
        raise RuntimeError("com error simulation")

    report = gate.run_completion_gate(path, render_checker=_boom)

    assert report["render_report"]["status"] == gate.RENDER_FAILED
    assert "com error simulation" in report["render_report"]["reason"]


def test_run_completion_gate_never_raises_on_malformed_document_xml(tmp_path):
    path = _build_docx(tmp_path, document_xml_override=b"<w:document><unclosed>")

    report = gate.run_completion_gate(
        path, render_checker=_fake_render_checker(gate.RENDER_VERIFIED)
    )

    assert report["ready"] is False
    assert report["para_id_report"] is None
    assert report["equation_caption_report"] is None


def test_run_completion_gate_reports_full_structural_data_even_when_ready(tmp_path):
    path = _build_docx(tmp_path, body_xml=_VALID_BODY)

    report = gate.run_completion_gate(
        path, render_checker=_fake_render_checker(gate.RENDER_VERIFIED)
    )

    assert report["ready"] is True
    assert report["para_id_report"]["body_paragraph_count"] == 2
    assert report["equation_caption_report"]["equation_count"] == 0
    assert report["required_text_report"]["ok"] is True
    assert report["stale_source_report"]["stale"] is False


# ---------------------------------------------------------------------------
# _default_render_checker -- guarded optional import of meridian_docs.
# ---------------------------------------------------------------------------

def test_default_render_checker_meridian_docs_not_importable(tmp_path, monkeypatch):
    path = _build_docx(tmp_path, body_xml=_VALID_BODY)

    def _raise(name):
        raise ModuleNotFoundError(f"No module named {name!r}")

    monkeypatch.setattr(gate.importlib, "import_module", _raise)

    result = gate._default_render_checker(path)

    assert result["status"] == gate.RENDER_UNAVAILABLE
    assert "not importable" in result["reason"]


def test_default_render_checker_maps_rendered_to_verified(tmp_path, monkeypatch):
    path = _build_docx(tmp_path, body_xml=_VALID_BODY)

    class _FakeModule:
        @staticmethod
        def check_word_com_render_receipt(docx_path):
            return {"status": "rendered", "backend": "word-com", "detail": {"ok": True}}

    monkeypatch.setattr(
        gate.importlib, "import_module", lambda name: _FakeModule()
    )

    result = gate._default_render_checker(path)

    assert result["status"] == gate.RENDER_VERIFIED
    assert result["backend"] == "word-com"
    assert result["source_status"] == "rendered"


def test_default_render_checker_maps_unavailable_with_reason(tmp_path, monkeypatch):
    path = _build_docx(tmp_path, body_xml=_VALID_BODY)

    class _FakeModule:
        @staticmethod
        def check_word_com_render_receipt(docx_path):
            return {"status": "unavailable-with-reason", "reason": "non-Windows platform"}

    monkeypatch.setattr(gate.importlib, "import_module", lambda name: _FakeModule())

    result = gate._default_render_checker(path)

    assert result["status"] == gate.RENDER_UNAVAILABLE
    assert result["reason"] == "non-Windows platform"


def test_default_render_checker_maps_failed(tmp_path, monkeypatch):
    path = _build_docx(tmp_path, body_xml=_VALID_BODY)

    class _FakeModule:
        @staticmethod
        def check_word_com_render_receipt(docx_path):
            return {"status": "failed", "reason": "com error"}

    monkeypatch.setattr(gate.importlib, "import_module", lambda name: _FakeModule())

    result = gate._default_render_checker(path)

    assert result["status"] == gate.RENDER_FAILED


def test_default_render_checker_missing_attribute_is_unavailable(tmp_path, monkeypatch):
    path = _build_docx(tmp_path, body_xml=_VALID_BODY)

    class _FakeModule:
        pass

    monkeypatch.setattr(gate.importlib, "import_module", lambda name: _FakeModule())

    result = gate._default_render_checker(path)

    assert result["status"] == gate.RENDER_UNAVAILABLE


def test_default_render_checker_raising_checker_is_failed(tmp_path, monkeypatch):
    path = _build_docx(tmp_path, body_xml=_VALID_BODY)

    class _FakeModule:
        @staticmethod
        def check_word_com_render_receipt(docx_path):
            raise RuntimeError("boom")

    monkeypatch.setattr(gate.importlib, "import_module", lambda name: _FakeModule())

    result = gate._default_render_checker(path)

    assert result["status"] == gate.RENDER_FAILED
    assert "boom" in result["reason"]


# ---------------------------------------------------------------------------
# CLI (main).
# ---------------------------------------------------------------------------

def test_cli_main_ready_exits_zero_and_prints_json(tmp_path, capsys):
    path = _build_docx(tmp_path, body_xml=_VALID_BODY)

    exit_code = gate.main([path, "--skip-render-check"])

    assert exit_code == 0
    captured = json.loads(capsys.readouterr().out)
    assert captured["ready"] is True


def test_cli_main_not_ready_exits_one(tmp_path, capsys):
    path = _build_docx(tmp_path, body_xml=_VALID_BODY)

    exit_code = gate.main(
        [path, "--skip-render-check", "--required-text", "not present anywhere"]
    )

    assert exit_code == 1
    captured = json.loads(capsys.readouterr().out)
    assert captured["ready"] is False


def test_cli_main_required_locator_parsing(tmp_path, capsys):
    path = _build_docx(tmp_path, body_xml=_VALID_BODY)

    exit_code = gate.main(
        [path, "--skip-render-check", "--required-locator", "P0000001::Introduction"]
    )

    assert exit_code == 0


def test_cli_main_rejects_malformed_locator(tmp_path):
    path = _build_docx(tmp_path, body_xml=_VALID_BODY)

    with pytest.raises(SystemExit):
        gate.main([path, "--required-locator", "no-double-colon-here"])


# ---------------------------------------------------------------------------
# Module-level invariants.
# ---------------------------------------------------------------------------

def test_render_statuses_constant():
    assert gate.RENDER_STATUSES == (
        gate.RENDER_VERIFIED,
        gate.RENDER_UNVERIFIED,
        gate.RENDER_UNAVAILABLE,
        gate.RENDER_FAILED,
    )
    assert gate.RENDER_VERIFIED == "render_verified"
    assert gate.RENDER_UNVERIFIED == "render_unverified"
    assert gate.RENDER_UNAVAILABLE == "render_unavailable"
    assert gate.RENDER_FAILED == "render_failed"


def test_gate_schema_version_is_stable_int():
    assert isinstance(gate.GATE_SCHEMA_VERSION, int)
    assert gate.GATE_SCHEMA_VERSION == 1


def test_legacy_alias_points_at_run_completion_gate():
    assert gate._codex_docx_completion_gate is gate.run_completion_gate


def test_module_has_no_hard_dependency_on_package_init():
    """This module must be importable standalone (as done at the top of this
    file, off its own file path) without tools/meridian_fallbacks/__init__.py
    existing or being importable. Asserting on `gate`'s own identity (rather
    than global `sys.modules` membership, which a sibling test file importing
    the real `tools.meridian_fallbacks` package in the same pytest session can
    legitimately populate) is what actually proves this module itself was
    loaded as a bare top-level module, not as a package submodule."""
    assert gate.__name__ == "docx_completion_gate"
    assert not gate.__package__
