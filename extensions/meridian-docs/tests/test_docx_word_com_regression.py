"""Tests for the tri-state real-render canary wired into insert_equation_local,
insert_caption, and insert_highlighted_note (016015e1).

check_render_capability (render_gate.py) was implemented (93cd9798) and
already wired into insert_figure_block / merge_draft_into_canonical (ddd79188)
and set_page_header / set_page_footer / highlight_document_matches /
insert_word_comment (5bab074, "W2-C") -- but had NO production caller from
insert_equation_local, insert_caption, or insert_highlighted_note. Those three
writers relied on structural reparse only (the manifest/well-formedness checks
built into _atomic_write_docx_bytes, plus this item's own new caller-level
positive content verification -- see _verify_equation_write /
_verify_caption_write / _verify_note_write in docs_intel.py), which is
insufficient: live Word/COM can reject a document (fail to actually open or
render it) while structural verification alone reports success.

This file exercises the SAME tri-state gate contract
(rendered / unavailable-with-reason / failed) that
test_docx_write_envelope.py already exercises for the W2-C writers, applied
to the three writers wired up by this item:

  - rendered: write stands, render evidence attached to the payload.
  - failed: docx_path is restored from the pre-write backup and an error is
    returned -- never reported as verified.
  - unavailable-with-reason: fails closed by default (restore + error) for
    canonical/production promotion; allow_degraded_render=True + a
    non-empty degraded_render_reason is the only audited opt-in, and even
    then render_verified stays False / render_degraded is stamped onto the
    result.

Also covers: the promotion lock + CAS-safe restore + caller-level structural
verification (which did not exist for these three writers before this item)
never mutates the canonical staging DOCX on a verification failure, and the
server.py MCP wrappers thread allow_degraded_render/degraded_render_reason
through correctly for all three tools.
"""
from __future__ import annotations

import zipfile
import xml.etree.ElementTree as ET

import pytest

from meridian_docs import docs_intel, render_gate, server


_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W14 = "http://schemas.microsoft.com/office/word/2010/wordml"
_M = "http://schemas.openxmlformats.org/officeDocument/2006/math"

_NS = f'xmlns:w="{_W}" xmlns:w14="{_W14}" xmlns:m="{_M}"'

_DOCUMENT_XML = f'''<?xml version="1.0" encoding="UTF-8"?>
<w:document {_NS}>
  <w:body>
    <w:p w14:paraId="P0000001">
      <w:r><w:t>Anchor paragraph.</w:t></w:r>
    </w:p>
    <w:p w14:paraId="P0000002">
      <w:r><w:t>Second paragraph.</w:t></w:r>
      <m:oMath><m:r><m:t>x</m:t></m:r></m:oMath>
    </w:p>
    <w:sectPr/>
  </w:body>
</w:document>
'''

_SIMPLE_OMATH = f'<m:oMath xmlns:m="{_M}"><m:r><m:t>z</m:t></m:r></m:oMath>'


def _write_docx(tmp_path, name="doc.docx") -> str:
    path = str(tmp_path / name)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", _DOCUMENT_XML)
    return path


def _q(ns: str, tag: str) -> str:
    return f"{{{ns}}}{tag}"


def _read_document_xml(path: str) -> bytes:
    with zipfile.ZipFile(path) as zf:
        return zf.read("word/document.xml")


# ---------------------------------------------------------------------------
# insert_caption
# ---------------------------------------------------------------------------


def test_insert_caption_rendered_reports_render_evidence(tmp_path, monkeypatch):
    path = _write_docx(tmp_path)
    monkeypatch.setattr(
        docs_intel.render_gate, "check_render_capability",
        lambda p, **kwargs: {
            "status": "rendered", "backend": "libreoffice-soffice",
            "detail": {"converted_via": "soffice"},
        },
    )

    result = docs_intel.insert_caption(path, "P0000001", "Figure", "A test figure")

    assert result["status"] == "inserted"
    assert result["render_status"] == "rendered"
    assert result["render_verified"] is True
    assert result["render_backend"] == "libreoffice-soffice"


def test_insert_caption_render_failed_restores_and_errors(tmp_path, monkeypatch):
    path = _write_docx(tmp_path)
    before = _read_document_xml(path)

    monkeypatch.setattr(
        docs_intel.render_gate, "check_render_capability",
        lambda p, **kwargs: {"status": "failed", "reason": "soffice crashed"},
    )

    result = docs_intel.insert_caption(path, "P0000001", "Figure", "A test figure")

    assert "error" in result
    assert result["render_status"] == "failed"
    assert result["file_restored"] is True
    assert _read_document_xml(path) == before, (
        "a real render failure must restore the file to its pre-write content, "
        "never leave the caption (promoted) version in place"
    )


def test_insert_caption_render_unavailable_fails_closed_by_default(tmp_path, monkeypatch):
    path = _write_docx(tmp_path)
    before = _read_document_xml(path)

    monkeypatch.setattr(
        docs_intel.render_gate, "check_render_capability",
        lambda p, **kwargs: {
            "status": "unavailable-with-reason",
            "reason": "no render backend available in this environment",
        },
    )

    result = docs_intel.insert_caption(path, "P0000001", "Figure", "A test figure")

    assert "error" in result
    assert result["render_status"] == "unavailable-with-reason"
    assert result["file_restored"] is True
    assert _read_document_xml(path) == before


def test_insert_caption_degrades_with_audited_override(tmp_path, monkeypatch):
    path = _write_docx(tmp_path)

    monkeypatch.setattr(
        docs_intel.render_gate, "check_render_capability",
        lambda p, **kwargs: {
            "status": "unavailable-with-reason",
            "reason": "no render backend available in this environment",
        },
    )

    result = docs_intel.insert_caption(
        path, "P0000001", "Figure", "A test figure",
        allow_degraded_render=True,
        degraded_render_reason="CI sandbox has no LibreOffice/Word installed",
    )

    assert result["status"] == "inserted"
    assert result["render_status"] == "unavailable-with-reason"
    assert result["render_verified"] is False
    assert result["render_degraded"] is True
    assert result["degraded_render_reason"] == "CI sandbox has no LibreOffice/Word installed"
    # The caption really did land -- degraded acceptance keeps the write.
    xml = _read_document_xml(path).decode("utf-8")
    assert "A test figure" in xml


def test_insert_caption_allow_degraded_render_requires_non_empty_reason(tmp_path):
    path = _write_docx(tmp_path)
    before = _read_document_xml(path)

    result = docs_intel.insert_caption(
        path, "P0000001", "Figure", "A test figure", allow_degraded_render=True,
    )

    assert "error" in result
    assert "degraded_render_reason" in result["error"]
    assert _read_document_xml(path) == before


def test_insert_caption_server_wrapper_threads_degraded_render_params(tmp_path, monkeypatch):
    path = _write_docx(tmp_path)
    monkeypatch.setattr(
        docs_intel.render_gate, "check_render_capability",
        lambda p, **kwargs: {"status": "unavailable-with-reason", "reason": "no backend"},
    )

    result = server.insert_caption(
        path, "P0000001", "Figure", "A test figure",
        allow_degraded_render=True,
        degraded_render_reason="no backend in test env",
    )

    assert result["status"] == "inserted"
    assert result["render_verified"] is False
    assert result["render_degraded"] is True


def test_insert_caption_structural_verification_failure_restores_and_errors(tmp_path, monkeypatch):
    """A caller-level content mismatch (the ACTUAL positive-content check
    this item adds) must fail closed BEFORE the render gate even runs --
    never reported as verified just because the render backend happened to
    say 'rendered'."""
    path = _write_docx(tmp_path)
    before = _read_document_xml(path)
    render_calls = {"n": 0}

    def _spy(p, **kwargs):
        render_calls["n"] += 1
        return {"status": "rendered", "backend": "test-stub", "detail": {}}

    monkeypatch.setattr(docs_intel.render_gate, "check_render_capability", _spy)
    monkeypatch.setattr(
        docs_intel, "_verify_caption_write",
        lambda *a, **kw: {"error": "post-write verification failed: simulated mismatch"},
    )

    result = docs_intel.insert_caption(path, "P0000001", "Figure", "A test figure")

    assert "error" in result
    assert result["file_restored"] is True
    assert _read_document_xml(path) == before
    # The render gate must never even be consulted once structural
    # verification has already failed.
    assert render_calls["n"] == 0


# ---------------------------------------------------------------------------
# insert_equation_local
# ---------------------------------------------------------------------------


def test_insert_equation_append_rendered_reports_render_evidence(tmp_path, monkeypatch):
    path = _write_docx(tmp_path)
    monkeypatch.setattr(
        docs_intel.render_gate, "check_render_capability",
        lambda p, **kwargs: {"status": "rendered", "backend": "word-com", "detail": {}},
    )

    result = docs_intel.insert_equation_local(path, "P0000001", _SIMPLE_OMATH, "append")

    assert result["status"] == "inserted"
    assert result["render_status"] == "rendered"
    assert result["render_verified"] is True


def test_insert_equation_before_after_rendered_reports_render_evidence(tmp_path, monkeypatch):
    path = _write_docx(tmp_path)
    monkeypatch.setattr(
        docs_intel.render_gate, "check_render_capability",
        lambda p, **kwargs: {"status": "rendered", "backend": "word-com", "detail": {}},
    )

    result = docs_intel.insert_equation_local(path, "P0000001", _SIMPLE_OMATH, "after")

    assert result["status"] == "inserted"
    assert result["render_status"] == "rendered"
    assert result["render_verified"] is True


def test_insert_equation_render_failed_restores_and_errors(tmp_path, monkeypatch):
    path = _write_docx(tmp_path)
    before = _read_document_xml(path)

    monkeypatch.setattr(
        docs_intel.render_gate, "check_render_capability",
        lambda p, **kwargs: {"status": "failed", "reason": "word COM crashed"},
    )

    result = docs_intel.insert_equation_local(path, "P0000001", _SIMPLE_OMATH, "before")

    assert "error" in result
    assert result["render_status"] == "failed"
    assert result["file_restored"] is True
    assert _read_document_xml(path) == before


def test_insert_equation_render_unavailable_fails_closed_by_default(tmp_path, monkeypatch):
    path = _write_docx(tmp_path)
    before = _read_document_xml(path)

    monkeypatch.setattr(
        docs_intel.render_gate, "check_render_capability",
        lambda p, **kwargs: {
            "status": "unavailable-with-reason",
            "reason": "no render backend available in this environment",
        },
    )

    result = docs_intel.insert_equation_local(path, "P0000001", _SIMPLE_OMATH, "append")

    assert "error" in result
    assert result["render_status"] == "unavailable-with-reason"
    assert result["file_restored"] is True
    assert _read_document_xml(path) == before


def test_insert_equation_degrades_with_audited_override(tmp_path, monkeypatch):
    path = _write_docx(tmp_path)

    monkeypatch.setattr(
        docs_intel.render_gate, "check_render_capability",
        lambda p, **kwargs: {
            "status": "unavailable-with-reason",
            "reason": "no render backend available in this environment",
        },
    )

    result = docs_intel.insert_equation_local(
        path, "P0000001", _SIMPLE_OMATH, "after",
        allow_degraded_render=True,
        degraded_render_reason="CI sandbox has no LibreOffice/Word installed",
    )

    assert result["status"] == "inserted"
    assert result["render_verified"] is False
    assert result["render_degraded"] is True
    assert result["degraded_render_reason"] == "CI sandbox has no LibreOffice/Word installed"


def test_insert_equation_allow_degraded_render_requires_non_empty_reason(tmp_path):
    path = _write_docx(tmp_path)
    before = _read_document_xml(path)

    result = docs_intel.insert_equation_local(
        path, "P0000001", _SIMPLE_OMATH, "after", allow_degraded_render=True,
    )

    assert "error" in result
    assert "degraded_render_reason" in result["error"]
    assert _read_document_xml(path) == before


def test_insert_equation_server_wrapper_threads_degraded_render_params(tmp_path, monkeypatch):
    path = _write_docx(tmp_path)
    monkeypatch.setattr(
        docs_intel.render_gate, "check_render_capability",
        lambda p, **kwargs: {"status": "unavailable-with-reason", "reason": "no backend"},
    )

    result = server.insert_equation(
        path, "P0000001", _SIMPLE_OMATH, "append",
        allow_degraded_render=True,
        degraded_render_reason="no backend in test env",
    )

    assert result["status"] == "inserted"
    assert result["render_verified"] is False
    assert result["render_degraded"] is True


def test_insert_equation_structural_verification_failure_restores_and_errors(tmp_path, monkeypatch):
    path = _write_docx(tmp_path)
    before = _read_document_xml(path)
    render_calls = {"n": 0}

    def _spy(p, **kwargs):
        render_calls["n"] += 1
        return {"status": "rendered", "backend": "test-stub", "detail": {}}

    monkeypatch.setattr(docs_intel.render_gate, "check_render_capability", _spy)
    monkeypatch.setattr(
        docs_intel, "_verify_equation_write",
        lambda *a, **kw: {"error": "post-write verification failed: simulated mismatch"},
    )

    result = docs_intel.insert_equation_local(path, "P0000001", _SIMPLE_OMATH, "after")

    assert "error" in result
    assert result["file_restored"] is True
    assert _read_document_xml(path) == before
    assert render_calls["n"] == 0


# ---------------------------------------------------------------------------
# insert_highlighted_note (mode="inline")
# ---------------------------------------------------------------------------


def test_insert_highlighted_note_inline_rendered_reports_render_evidence(tmp_path, monkeypatch):
    path = _write_docx(tmp_path)
    monkeypatch.setattr(
        docs_intel.render_gate, "check_render_capability",
        lambda p, **kwargs: {
            "status": "rendered", "backend": "libreoffice-soffice", "detail": {},
        },
    )

    result = docs_intel.insert_highlighted_note(path, "Please review.", "P0000001")

    assert result["status"] == "inserted"
    assert result["mode"] == "inline"
    assert result["render_status"] == "rendered"
    assert result["render_verified"] is True


def test_insert_highlighted_note_inline_render_failed_restores_and_errors(tmp_path, monkeypatch):
    path = _write_docx(tmp_path)
    before = _read_document_xml(path)

    monkeypatch.setattr(
        docs_intel.render_gate, "check_render_capability",
        lambda p, **kwargs: {"status": "failed", "reason": "soffice crashed"},
    )

    result = docs_intel.insert_highlighted_note(path, "Please review.", "P0000001")

    assert "error" in result
    assert result["render_status"] == "failed"
    assert result["file_restored"] is True
    assert _read_document_xml(path) == before


def test_insert_highlighted_note_inline_render_unavailable_fails_closed_by_default(tmp_path, monkeypatch):
    path = _write_docx(tmp_path)
    before = _read_document_xml(path)

    monkeypatch.setattr(
        docs_intel.render_gate, "check_render_capability",
        lambda p, **kwargs: {
            "status": "unavailable-with-reason",
            "reason": "no render backend available in this environment",
        },
    )

    result = docs_intel.insert_highlighted_note(path, "Please review.", "P0000001")

    assert "error" in result
    assert result["render_status"] == "unavailable-with-reason"
    assert result["file_restored"] is True
    assert _read_document_xml(path) == before


def test_insert_highlighted_note_inline_degrades_with_audited_override(tmp_path, monkeypatch):
    path = _write_docx(tmp_path)

    monkeypatch.setattr(
        docs_intel.render_gate, "check_render_capability",
        lambda p, **kwargs: {
            "status": "unavailable-with-reason",
            "reason": "no render backend available in this environment",
        },
    )

    result = docs_intel.insert_highlighted_note(
        path, "Please review.", "P0000001",
        allow_degraded_render=True,
        degraded_render_reason="CI sandbox has no LibreOffice/Word installed",
    )

    assert result["status"] == "inserted"
    assert result["render_verified"] is False
    assert result["render_degraded"] is True
    assert result["degraded_render_reason"] == "CI sandbox has no LibreOffice/Word installed"


def test_insert_highlighted_note_allow_degraded_render_requires_non_empty_reason(tmp_path):
    path = _write_docx(tmp_path)
    before = _read_document_xml(path)

    result = docs_intel.insert_highlighted_note(
        path, "Please review.", "P0000001", allow_degraded_render=True,
    )

    assert "error" in result
    assert "degraded_render_reason" in result["error"]
    assert _read_document_xml(path) == before


def test_insert_highlighted_note_mode_comment_forwards_degraded_render_params(tmp_path, monkeypatch):
    """mode="comment" delegates to insert_word_comment, which already enforces
    its own render gate (5bab074/W2-C) -- confirm the two new params are
    actually forwarded, not silently dropped."""
    path = _write_docx(tmp_path)
    captured = {}
    real_insert_word_comment = docs_intel.insert_word_comment

    def _spy(*args, **kwargs):
        captured.update(kwargs)
        return real_insert_word_comment(*args, **kwargs)

    monkeypatch.setattr(docs_intel, "insert_word_comment", _spy)
    monkeypatch.setattr(
        docs_intel.render_gate, "check_render_capability",
        lambda p, **kwargs: {"status": "unavailable-with-reason", "reason": "no backend"},
    )

    result = docs_intel.insert_highlighted_note(
        path, "Please review.", "P0000001", mode="comment",
        allow_degraded_render=True,
        degraded_render_reason="no backend in test env",
    )

    assert captured.get("allow_degraded_render") is True
    assert captured.get("degraded_render_reason") == "no backend in test env"
    assert result["status"] == "inserted"
    assert result["render_verified"] is False
    assert result["render_degraded"] is True


def test_insert_highlighted_note_server_wrapper_threads_degraded_render_params(tmp_path, monkeypatch):
    path = _write_docx(tmp_path)
    monkeypatch.setattr(
        docs_intel.render_gate, "check_render_capability",
        lambda p, **kwargs: {"status": "unavailable-with-reason", "reason": "no backend"},
    )

    result = server.insert_highlighted_note(
        path, "Please review.", "P0000001",
        allow_degraded_render=True,
        degraded_render_reason="no backend in test env",
    )

    assert result["status"] == "inserted"
    assert result["render_verified"] is False
    assert result["render_degraded"] is True


def test_insert_highlighted_note_structural_verification_failure_restores_and_errors(tmp_path, monkeypatch):
    path = _write_docx(tmp_path)
    before = _read_document_xml(path)
    render_calls = {"n": 0}

    def _spy(p, **kwargs):
        render_calls["n"] += 1
        return {"status": "rendered", "backend": "test-stub", "detail": {}}

    monkeypatch.setattr(docs_intel.render_gate, "check_render_capability", _spy)
    monkeypatch.setattr(
        docs_intel, "_verify_note_write",
        lambda *a, **kw: {"error": "post-write verification failed: simulated mismatch"},
    )

    result = docs_intel.insert_highlighted_note(path, "Please review.", "P0000001")

    assert "error" in result
    assert result["file_restored"] is True
    assert _read_document_xml(path) == before
    assert render_calls["n"] == 0


# ---------------------------------------------------------------------------
# Canonical staging DOCX is never mutated by a verification/render failure.
# ---------------------------------------------------------------------------


def test_render_gate_failures_never_leave_promoted_bytes_on_disk(tmp_path, monkeypatch):
    """Cross-cutting regression: for all three writers, a render 'failed' or
    'unavailable-with-reason' status must never leave the just-promoted
    (mutated) bytes sitting on disk -- the canonical file is restored to
    exactly its pre-write content in every failure branch."""
    monkeypatch.setattr(
        docs_intel.render_gate, "check_render_capability",
        lambda p, **kwargs: {"status": "failed", "reason": "simulated render failure"},
    )

    path_caption = _write_docx(tmp_path, "caption.docx")
    before_caption = _read_document_xml(path_caption)
    r1 = docs_intel.insert_caption(path_caption, "P0000001", "Table", "A table")
    assert "error" in r1
    assert _read_document_xml(path_caption) == before_caption

    path_equation = _write_docx(tmp_path, "equation.docx")
    before_equation = _read_document_xml(path_equation)
    r2 = docs_intel.insert_equation_local(path_equation, "P0000001", _SIMPLE_OMATH, "before")
    assert "error" in r2
    assert _read_document_xml(path_equation) == before_equation

    path_note = _write_docx(tmp_path, "note.docx")
    before_note = _read_document_xml(path_note)
    r3 = docs_intel.insert_highlighted_note(path_note, "A note.", "P0000001")
    assert "error" in r3
    assert _read_document_xml(path_note) == before_note


# ---------------------------------------------------------------------------
# Real backend / structural-validation-fallback regression coverage (W2-D,
# 9a817fce). Everything above this line drives check_render_capability via a
# monkeypatched stand-in so the tri-state CONTRACT can be tested
# deterministically regardless of the machine -- but that leaves a real gap:
# none of it proves the ACTUAL _word_com_render / _soffice_render backends
# produce correct results against a genuinely Word-authored (full-part)
# .docx package, and this whole file never ran in CI at all before this item
# (extensions/meridian-docs is not wired into pixi.toml -- see the new
# `meridian-docs` job in .github/workflows/test.yml).
#
# This section drives the REAL (unmocked) render_gate.check_render_capability
# against a full-fidelity fixture built with every part a genuine Word save
# produces ([Content_Types].xml, both .rels parts, styles/fontTable/settings/
# webSettings/theme -- not just the bare word/document.xml the tri-state-
# contract tests above use, which is sufficient for a monkeypatched backend
# but not something a real backend is guaranteed to open cleanly).
#
# Exactly one of two branches is exercised on any given machine, and both are
# asserted -- this never silently skips just because the current machine
# lacks a render backend:
#
#   - a real backend IS available (Word COM on a Windows box with Word
#     installed, or LibreOffice via `soffice` -- CI installs
#     libreoffice-writer for this) -> render_status == "rendered" with real
#     backend evidence, exercising the genuine automation path end to end.
#   - no backend is available (this sandbox; a bare Windows box with no
#     Office; any runner without soffice) -> render fails closed by default
#     (never silently "verified"); the STRUCTURAL VALIDATION FALLBACK the
#     item title calls for is exercised explicitly via
#     allow_degraded_render=True and an independent, from-scratch structural
#     integrity check on the resulting .docx (valid ZIP, well-formed XML in
#     every part, every required OOXML part present, and the write's content
#     genuinely landed) -- real evidence that does not depend on Word or
#     LibreOffice being installed anywhere.
# ---------------------------------------------------------------------------


_CONTENT_TYPES_XML = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
  <Override PartName="/word/fontTable.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.fontTable+xml"/>
  <Override PartName="/word/settings.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml"/>
  <Override PartName="/word/webSettings.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.webSettings+xml"/>
  <Override PartName="/word/theme/theme1.xml" ContentType="application/vnd.openxmlformats-officedocument.theme+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
  <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
</Types>
'''

_ROOT_RELS_XML = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>
'''

_DOCUMENT_RELS_XML = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/fontTable" Target="fontTable.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/webSettings" Target="webSettings.xml"/>
  <Relationship Id="rId4" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/settings" Target="settings.xml"/>
  <Relationship Id="rId5" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme" Target="theme/theme1.xml"/>
</Relationships>
'''

_STYLES_XML = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:docDefaults/>
  <w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>
</w:styles>
'''

_FONT_TABLE_XML = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:fonts xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:font w:name="Calibri"/>
</w:fonts>
'''

_SETTINGS_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<w:settings xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>\n'
)

_WEB_SETTINGS_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<w:webSettings xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>\n'
)

_THEME_XML = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" name="Office Theme">
  <a:themeElements>
    <a:clrScheme name="Office"/>
    <a:fontScheme name="Office"/>
    <a:fmtScheme name="Office"/>
  </a:themeElements>
</a:theme>
'''

_CORE_PROPS_XML = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/">
  <dc:title>Meridian W2-D regression fixture</dc:title>
</cp:coreProperties>
'''

_APP_PROPS_XML = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">
  <Application>Meridian test fixture</Application>
</Properties>
'''

_REQUIRED_OOXML_PARTS = ("[Content_Types].xml", "_rels/.rels", "word/document.xml")


def _write_word_authored_docx(tmp_path, name: str = "word_authored.docx") -> str:
    """A full-fidelity fixture with every part a genuine Word save produces.

    Unlike ``_write_docx`` above (bare ``word/document.xml`` only -- sufficient
    for the monkeypatched tri-state-contract tests, but not something a real
    backend is guaranteed to open cleanly), this is what "Word-authored" means
    for this item: a complete, real OOXML package.
    """
    path = str(tmp_path / name)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES_XML)
        archive.writestr("_rels/.rels", _ROOT_RELS_XML)
        archive.writestr("word/document.xml", _DOCUMENT_XML)
        archive.writestr("word/_rels/document.xml.rels", _DOCUMENT_RELS_XML)
        archive.writestr("word/styles.xml", _STYLES_XML)
        archive.writestr("word/fontTable.xml", _FONT_TABLE_XML)
        archive.writestr("word/settings.xml", _SETTINGS_XML)
        archive.writestr("word/webSettings.xml", _WEB_SETTINGS_XML)
        archive.writestr("word/theme/theme1.xml", _THEME_XML)
        archive.writestr("docProps/core.xml", _CORE_PROPS_XML)
        archive.writestr("docProps/app.xml", _APP_PROPS_XML)
    return path


def _assert_structural_fallback_integrity(path: str) -> None:
    """The "structural validation fallback" the item title calls for: when no
    real render backend is available (this sandbox; a CI runner without
    LibreOffice; a bare Windows box with no Office), the write must still be
    independently verifiable as a genuinely well-formed OOXML package -- valid
    ZIP, well-formed XML in every part, and every part a real reader (Word,
    LibreOffice, or Meridian's own parse_docx) requires actually present.
    This is real, from-scratch evidence -- it does not reuse docs_intel's own
    internal _verify_*_write helpers, so it can't share a blind spot with them.
    """
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        for required in _REQUIRED_OOXML_PARTS:
            assert required in names, f"missing required OOXML part: {required}"
        for part_name in names:
            if not (part_name.endswith(".xml") or part_name.endswith(".rels")):
                continue
            raw = archive.read(part_name)
            try:
                ET.fromstring(raw)
            except ET.ParseError as exc:  # pragma: no cover -- failure path
                raise AssertionError(f"{part_name} is not well-formed XML: {exc}") from exc
    with open(path, "rb") as handle:
        raw_bytes = handle.read()
    paragraphs = docs_intel.parse_docx(raw_bytes)
    assert paragraphs, "structural fallback: parse_docx found no paragraphs"


def _probe_real_render_status(path: str) -> str:
    """Run the REAL (unmocked) render_gate.check_render_capability once
    against ``path`` to learn which of the three tri-states THIS machine
    actually produces right now, before driving a writer through it.

    Read-only: backends only read ``path`` (Word COM opens it
    ``ReadOnly=True``; soffice converts to a throwaway temp PDF), so this
    never mutates the fixture the caller is about to write into.

    Three real outcomes are possible, not two -- a naive "is a backend
    importable" check (which is all ``detect_backend`` does) is NOT the same
    as "will actually render successfully": a backend can be nominally
    *available* (e.g. ``pywin32`` is importable) yet still *fail* the
    moment a render is attempted (e.g. no Word.Application registered on
    this machine at all). Treating "available" as a guarantee of success
    would make this suite flaky on exactly that kind of half-configured
    machine, so every test below branches on the REAL observed status
    rather than assuming ``detect_backend()``'s availability implies
    ``"rendered"``.
    """
    return render_gate.check_render_capability(path)["status"]


def test_insert_caption_real_backend_or_structural_fallback(tmp_path):
    """No monkeypatch -- drives the REAL render_gate.check_render_capability
    against a full-fidelity, Word-authored fixture."""
    path = _write_word_authored_docx(tmp_path, "caption_real.docx")
    before = _read_document_xml(path)
    status = _probe_real_render_status(path)

    if status == render_gate.RENDERED:
        result = docs_intel.insert_caption(path, "P0000001", "Figure", "Real backend caption")
        assert result["status"] == "inserted"
        assert result["render_status"] == render_gate.RENDERED
        assert result["render_verified"] is True
        assert result["render_backend"] in {backend.name for backend in render_gate.KNOWN_BACKENDS}
        assert "Real backend caption" in _read_document_xml(path).decode("utf-8")
    elif status == render_gate.UNAVAILABLE_WITH_REASON:
        result = docs_intel.insert_caption(
            path, "P0000001", "Figure", "Degraded backend caption",
            allow_degraded_render=True,
            degraded_render_reason="no render backend available in this environment (structural fallback)",
        )
        assert result["status"] == "inserted"
        assert result["render_status"] == render_gate.UNAVAILABLE_WITH_REASON
        assert result["render_verified"] is False
        assert result["render_degraded"] is True
        _assert_structural_fallback_integrity(path)
        assert "Degraded backend caption" in _read_document_xml(path).decode("utf-8")
    else:
        assert status == render_gate.FAILED
        result = docs_intel.insert_caption(path, "P0000001", "Figure", "Should not land")
        assert "error" in result
        assert result["render_status"] == render_gate.FAILED
        assert result["file_restored"] is True
        assert _read_document_xml(path) == before


def test_insert_equation_real_backend_or_structural_fallback(tmp_path):
    path = _write_word_authored_docx(tmp_path, "equation_real.docx")
    before = _read_document_xml(path)
    status = _probe_real_render_status(path)

    if status == render_gate.RENDERED:
        result = docs_intel.insert_equation_local(path, "P0000001", _SIMPLE_OMATH, "append")
        assert result["status"] == "inserted"
        assert result["render_status"] == render_gate.RENDERED
        assert result["render_verified"] is True
        assert result["render_backend"] in {backend.name for backend in render_gate.KNOWN_BACKENDS}
        assert b"<m:oMath" in _read_document_xml(path)
    elif status == render_gate.UNAVAILABLE_WITH_REASON:
        result = docs_intel.insert_equation_local(
            path, "P0000001", _SIMPLE_OMATH, "after",
            allow_degraded_render=True,
            degraded_render_reason="no render backend available in this environment (structural fallback)",
        )
        assert result["status"] == "inserted"
        assert result["render_status"] == render_gate.UNAVAILABLE_WITH_REASON
        assert result["render_verified"] is False
        assert result["render_degraded"] is True
        _assert_structural_fallback_integrity(path)
        assert b"<m:oMath" in _read_document_xml(path)
    else:
        assert status == render_gate.FAILED
        result = docs_intel.insert_equation_local(path, "P0000001", _SIMPLE_OMATH, "after")
        assert "error" in result
        assert result["render_status"] == render_gate.FAILED
        assert result["file_restored"] is True
        assert _read_document_xml(path) == before


def test_insert_highlighted_note_real_backend_or_structural_fallback(tmp_path):
    path = _write_word_authored_docx(tmp_path, "note_real.docx")
    before = _read_document_xml(path)
    status = _probe_real_render_status(path)

    if status == render_gate.RENDERED:
        result = docs_intel.insert_highlighted_note(path, "Real backend note.", "P0000001")
        assert result["status"] == "inserted"
        assert result["render_status"] == render_gate.RENDERED
        assert result["render_verified"] is True
        assert result["render_backend"] in {backend.name for backend in render_gate.KNOWN_BACKENDS}
        assert "Real backend note." in _read_document_xml(path).decode("utf-8")
    elif status == render_gate.UNAVAILABLE_WITH_REASON:
        result = docs_intel.insert_highlighted_note(
            path, "Degraded backend note.", "P0000001",
            allow_degraded_render=True,
            degraded_render_reason="no render backend available in this environment (structural fallback)",
        )
        assert result["status"] == "inserted"
        assert result["render_status"] == render_gate.UNAVAILABLE_WITH_REASON
        assert result["render_verified"] is False
        assert result["render_degraded"] is True
        _assert_structural_fallback_integrity(path)
        assert "Degraded backend note." in _read_document_xml(path).decode("utf-8")
    else:
        assert status == render_gate.FAILED
        result = docs_intel.insert_highlighted_note(path, "Should not land.", "P0000001")
        assert "error" in result
        assert result["render_status"] == render_gate.FAILED
        assert result["file_restored"] is True
        assert _read_document_xml(path) == before


def test_word_authored_fixture_itself_is_structurally_valid(tmp_path):
    """Sanity check on the fixture builder itself, independent of any writer
    -- guards against the fixture silently regressing into something no
    reader (real or structural-fallback) would accept."""
    path = _write_word_authored_docx(tmp_path)
    _assert_structural_fallback_integrity(path)
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
    for expected_part in (
        "word/styles.xml",
        "word/fontTable.xml",
        "word/settings.xml",
        "word/webSettings.xml",
        "word/theme/theme1.xml",
        "word/_rels/document.xml.rels",
        "docProps/core.xml",
        "docProps/app.xml",
    ):
        assert expected_part in names
