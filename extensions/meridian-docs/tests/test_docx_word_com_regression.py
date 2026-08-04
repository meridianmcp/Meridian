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

from meridian_docs import docs_intel, server


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
