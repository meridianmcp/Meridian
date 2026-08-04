"""Tests for the fail-closed DOCX write envelope (W2-C).

Builds on W2-A (namespace-preserving DOCX serialization, ee4bf79) and W2-B
(unique bookmark w:id allocation, 39f7ac2), both already shipped. This item
hardens :func:`docs_intel._save_docx_with_new_parts_stdlib` -- the shared
multi-part writer behind ``set_page_header``/``set_page_footer``,
``highlight_document_matches``, and ``insert_word_comment`` -- and its
callers, in two independent ways:

1. Real XML well-formedness verification of every part a write changes
   (:func:`docs_intel._atomic_write_docx_bytes`'s new ``changed_parts``
   check). Previously a malformed-but-still-ZIP-valid write could be
   promoted and reported as success: the structural-manifest counts
   (media/style/relationship) silently treat an unparsable XML part as "0
   elements" instead of raising, and the ZIP-container check alone can't see
   inside a well-formed ZIP holding a corrupt member.
2. A real Word/COM (or LibreOffice) render-capability check
   (:func:`docs_intel._enforce_render_verification`, from ddd79188) is now
   also enforced by every ``_save_docx_with_new_parts_stdlib`` caller,
   mirroring the same gate ``insert_figure_block`` / ``merge_docx_draft``
   already use: structural verification alone can never prove a document
   actually opens in Word. Fails closed by default when no render backend is
   available; ``allow_degraded_render=True`` + a non-empty
   ``degraded_render_reason`` is the only audited opt-in.

In both cases the guiding invariant is the same: never report success, and
never leave a promoted file on disk, after a verification failure -- ``dest``
is either byte-identical to its pre-write content (never-promoted case) or
restored to it (promoted-then-failed-render case).
"""
from __future__ import annotations

import io
import zipfile
import xml.etree.ElementTree as ET

import pytest

from meridian_docs import docs_intel, server


_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W14 = "http://schemas.microsoft.com/office/word/2010/wordml"


@pytest.fixture(autouse=True)
def _default_render_capability(monkeypatch):
    """Stub a successful 'rendered' result by default so tests that are not
    specifically exercising the render-capability gate's own contract don't
    depend on -- or get blocked by -- whichever render backends happen to be
    installed on the machine running the suite. Mirrors
    test_19be1551_insert_figure_block.py's fixture of the same name."""
    monkeypatch.setattr(
        docs_intel.render_gate,
        "check_render_capability",
        lambda docx_path, **kwargs: {
            "status": "rendered",
            "backend": "test-stub",
            "detail": {"stub": True},
        },
    )


_DOCUMENT_XML = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:p w14:paraId="P0000001">
      <w:r><w:t>The quick brown fox jumps over the lazy dog.</w:t></w:r>
    </w:p>
    <w:sectPr/>
  </w:body>
</w:document>
"""


def _write_docx(tmp_path, name="doc.docx") -> str:
    path = str(tmp_path / name)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", _DOCUMENT_XML)
    return path


def _q(ns: str, tag: str) -> str:
    return f"{{{ns}}}{tag}"


# ---------------------------------------------------------------------------
# Core fix: real XML well-formedness verification of changed parts.
# ---------------------------------------------------------------------------


def test_save_docx_with_new_parts_stdlib_rejects_malformed_xml_part(tmp_path):
    path = _write_docx(tmp_path)
    with open(path, "rb") as fh:
        raw = fh.read()
    original_bytes = raw

    updated_parts = {
        # A genuinely new part with malformed XML -- word/document.xml
        # itself is left out of updated_parts entirely (unchanged).
        "[Content_Types].xml": b"<Types><Override PartName=/no/closing/quote",
    }

    with pytest.raises(docs_intel.DocxWriteVerificationError) as excinfo:
        docs_intel._save_docx_with_new_parts_stdlib(raw, updated_parts, path)

    manifest = excinfo.value.manifest
    assert "[Content_Types].xml" in manifest.get("xml_parse_errors", {})

    # dest was NEVER promoted -- the whole point of stage-before-promote.
    with open(path, "rb") as fh:
        assert fh.read() == original_bytes
    assert not (tmp_path / "doc.docx.bak").exists()


def test_save_docx_with_new_parts_stdlib_promotes_well_formed_write(tmp_path):
    path = _write_docx(tmp_path)
    with open(path, "rb") as fh:
        raw = fh.read()

    new_document = (
        b'<?xml version="1.0" encoding="UTF-8"?>\n'
        + b'<w:document xmlns:w="' + _W.encode() + b'" '
        + b'xmlns:w14="' + _W14.encode() + b'">'
        + b'<w:body><w:p w14:paraId="P0000002"><w:r><w:t>Edited.</w:t></w:r></w:p>'
        + b'<w:sectPr/></w:body></w:document>'
    )
    docs_intel._save_docx_with_new_parts_stdlib(
        raw, {"word/document.xml": new_document}, path
    )

    with zipfile.ZipFile(path) as zf:
        root = ET.fromstring(zf.read("word/document.xml"))
    texts = [t.text for t in root.iter(_q(_W, "t"))]
    assert texts == ["Edited."]


def test_save_docx_with_new_parts_stdlib_missing_part_after_stage_fails_closed(tmp_path, monkeypatch):
    """Defensive edge case: if a caller claims a part changed but the staged
    archive doesn't actually contain it (a bug in the caller's own ZIP
    repackaging), that must also fail closed instead of silently promoting a
    write that doesn't match what was claimed."""
    path = _write_docx(tmp_path)
    with open(path, "rb") as fh:
        raw = fh.read()
    original_bytes = raw

    real_atomic_write = docs_intel._atomic_write_docx_bytes

    def _lie_about_changed_parts(payload, dest, **kwargs):
        kwargs = dict(kwargs)
        changed = dict(kwargs.get("changed_parts") or {})
        changed["word/does-not-exist.xml"] = b"<a/>"
        kwargs["changed_parts"] = changed
        return real_atomic_write(payload, dest, **kwargs)

    monkeypatch.setattr(docs_intel, "_atomic_write_docx_bytes", _lie_about_changed_parts)

    with pytest.raises(docs_intel.DocxWriteVerificationError) as excinfo:
        docs_intel._save_docx_with_new_parts_stdlib(
            raw, {"word/document.xml": _DOCUMENT_XML.encode("utf-8")}, path
        )

    assert "word/does-not-exist.xml" in excinfo.value.manifest.get("xml_parse_errors", {})
    with open(path, "rb") as fh:
        assert fh.read() == original_bytes


# ---------------------------------------------------------------------------
# Render-capability gate wiring: highlight_document_matches.
# ---------------------------------------------------------------------------


def test_highlight_document_matches_rendered_reports_render_evidence(tmp_path, monkeypatch):
    path = _write_docx(tmp_path)
    monkeypatch.setattr(
        docs_intel.render_gate, "check_render_capability",
        lambda p, **kwargs: {
            "status": "rendered", "backend": "libreoffice-soffice",
            "detail": {"converted_via": "soffice"},
        },
    )

    result = docs_intel.highlight_document_matches(path, "quick brown fox")

    assert result["status"] == "highlighted"
    assert result["render_status"] == "rendered"
    assert result["render_verified"] is True
    assert result["render_backend"] == "libreoffice-soffice"


def test_highlight_document_matches_render_failed_restores_and_errors(tmp_path, monkeypatch):
    path = _write_docx(tmp_path)
    with open(path, "rb") as fh:
        original_bytes = fh.read()

    monkeypatch.setattr(
        docs_intel.render_gate, "check_render_capability",
        lambda p, **kwargs: {"status": "failed", "reason": "soffice crashed"},
    )

    result = docs_intel.highlight_document_matches(path, "quick brown fox")

    assert "error" in result
    assert result["render_status"] == "failed"
    assert result["file_restored"] is True
    with open(path, "rb") as fh:
        assert fh.read() == original_bytes, (
            "a real render failure must restore the file to its pre-write "
            "content, never leave the highlighted (promoted) version in place"
        )


def test_highlight_document_matches_render_unavailable_fails_closed_by_default(tmp_path, monkeypatch):
    path = _write_docx(tmp_path)
    with open(path, "rb") as fh:
        original_bytes = fh.read()

    monkeypatch.setattr(
        docs_intel.render_gate, "check_render_capability",
        lambda p, **kwargs: {
            "status": "unavailable-with-reason",
            "reason": "no render backend available in this environment",
        },
    )

    result = docs_intel.highlight_document_matches(path, "quick brown fox")

    assert "error" in result
    assert result["render_status"] == "unavailable-with-reason"
    assert result["file_restored"] is True
    with open(path, "rb") as fh:
        assert fh.read() == original_bytes


def test_highlight_document_matches_degrades_with_audited_override(tmp_path, monkeypatch):
    path = _write_docx(tmp_path)

    monkeypatch.setattr(
        docs_intel.render_gate, "check_render_capability",
        lambda p, **kwargs: {
            "status": "unavailable-with-reason",
            "reason": "no render backend available in this environment",
        },
    )

    result = docs_intel.highlight_document_matches(
        path, "quick brown fox",
        allow_degraded_render=True,
        degraded_render_reason="CI sandbox has no LibreOffice/Word installed",
    )

    assert result["status"] == "highlighted"
    assert result["render_status"] == "unavailable-with-reason"
    assert result["render_verified"] is False
    assert result["render_degraded"] is True
    assert result["degraded_render_reason"] == "CI sandbox has no LibreOffice/Word installed"


def test_highlight_document_matches_allow_degraded_render_requires_non_empty_reason(tmp_path):
    path = _write_docx(tmp_path)
    with open(path, "rb") as fh:
        original_bytes = fh.read()

    result = docs_intel.highlight_document_matches(
        path, "quick brown fox", allow_degraded_render=True,
    )

    assert "error" in result
    assert "degraded_render_reason" in result["error"]
    with open(path, "rb") as fh:
        assert fh.read() == original_bytes


def test_highlight_document_server_wrapper_threads_degraded_render_params(tmp_path, monkeypatch):
    path = _write_docx(tmp_path)
    monkeypatch.setattr(
        docs_intel.render_gate, "check_render_capability",
        lambda p, **kwargs: {"status": "unavailable-with-reason", "reason": "no backend"},
    )

    result = server.highlight_document(
        path, "quick brown fox",
        allow_degraded_render=True,
        degraded_render_reason="no backend in test env",
    )

    assert result["status"] == "highlighted"
    assert result["render_verified"] is False
    assert result["render_degraded"] is True


# ---------------------------------------------------------------------------
# Render-capability gate wiring: insert_word_comment.
# ---------------------------------------------------------------------------


def test_insert_word_comment_rendered_reports_render_evidence(tmp_path, monkeypatch):
    path = _write_docx(tmp_path)
    monkeypatch.setattr(
        docs_intel.render_gate, "check_render_capability",
        lambda p, **kwargs: {"status": "rendered", "backend": "word-com", "detail": {}},
    )

    result = docs_intel.insert_word_comment(path, "Please review.", "P0000001")

    assert result["status"] == "inserted"
    assert result["render_status"] == "rendered"
    assert result["render_verified"] is True


def test_insert_word_comment_render_failed_restores_and_errors(tmp_path, monkeypatch):
    path = _write_docx(tmp_path)
    with open(path, "rb") as fh:
        original_bytes = fh.read()

    monkeypatch.setattr(
        docs_intel.render_gate, "check_render_capability",
        lambda p, **kwargs: {"status": "failed", "reason": "word COM crashed"},
    )

    result = docs_intel.insert_word_comment(path, "Please review.", "P0000001")

    assert "error" in result
    assert result["render_status"] == "failed"
    assert result["file_restored"] is True
    with open(path, "rb") as fh:
        assert fh.read() == original_bytes


def test_insert_word_comment_allow_degraded_render_requires_non_empty_reason(tmp_path):
    path = _write_docx(tmp_path)
    with open(path, "rb") as fh:
        original_bytes = fh.read()

    result = docs_intel.insert_word_comment(
        path, "Please review.", "P0000001", allow_degraded_render=True,
    )

    assert "error" in result
    assert "degraded_render_reason" in result["error"]
    with open(path, "rb") as fh:
        assert fh.read() == original_bytes


# ---------------------------------------------------------------------------
# Render-capability gate wiring: set_page_header / set_page_footer.
# ---------------------------------------------------------------------------


def test_set_page_header_rendered_reports_render_evidence(tmp_path, monkeypatch):
    path = _write_docx(tmp_path)
    monkeypatch.setattr(
        docs_intel.render_gate, "check_render_capability",
        lambda p, **kwargs: {"status": "rendered", "backend": "test-stub", "detail": {}},
    )

    result = docs_intel.set_page_header(path, "Meridian Draft")

    assert result["status"] == "set"
    assert result["render_status"] == "rendered"
    assert result["render_verified"] is True


def test_set_page_footer_render_unavailable_fails_closed_by_default(tmp_path, monkeypatch):
    path = _write_docx(tmp_path)
    with open(path, "rb") as fh:
        original_bytes = fh.read()

    monkeypatch.setattr(
        docs_intel.render_gate, "check_render_capability",
        lambda p, **kwargs: {
            "status": "unavailable-with-reason",
            "reason": "no render backend available in this environment",
        },
    )

    result = docs_intel.set_page_footer(path, "Page footer text")

    assert "error" in result
    assert result["render_status"] == "unavailable-with-reason"
    assert result["file_restored"] is True
    with open(path, "rb") as fh:
        assert fh.read() == original_bytes


def test_set_page_header_degrades_with_audited_override(tmp_path, monkeypatch):
    path = _write_docx(tmp_path)

    monkeypatch.setattr(
        docs_intel.render_gate, "check_render_capability",
        lambda p, **kwargs: {
            "status": "unavailable-with-reason",
            "reason": "no render backend available in this environment",
        },
    )

    result = docs_intel.set_page_header(
        path, "Meridian Draft",
        allow_degraded_render=True,
        degraded_render_reason="CI sandbox has no LibreOffice/Word installed",
    )

    assert result["status"] == "set"
    assert result["render_verified"] is False
    assert result["render_degraded"] is True


def test_set_page_header_allow_degraded_render_requires_non_empty_reason(tmp_path):
    path = _write_docx(tmp_path)
    with open(path, "rb") as fh:
        original_bytes = fh.read()

    result = docs_intel.set_page_header(path, "Meridian Draft", allow_degraded_render=True)

    assert "error" in result
    assert "degraded_render_reason" in result["error"]
    with open(path, "rb") as fh:
        assert fh.read() == original_bytes


# ---------------------------------------------------------------------------
# Structural correctness is still independently enforced -- passing the
# render gate never substitutes for it.
# ---------------------------------------------------------------------------


def test_render_gate_never_bypasses_structural_media_style_protection(tmp_path, monkeypatch):
    """A staged write that drops media/style parts must still be rejected by
    the existing structural-manifest gate even when the render backend would
    happily report 'rendered' -- render verification is layered ON TOP of
    structural verification, never a replacement for it."""
    path = str(tmp_path / "doc.docx")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", _DOCUMENT_XML)
        archive.writestr(
            "word/media/image1.png", b"\x89PNG\r\n\x1a\nfakepngbytes"
        )
    with open(path, "rb") as fh:
        raw = fh.read()
    original_bytes = raw

    # A hostile/buggy caller that repackages the ZIP WITHOUT the media part.
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(raw)) as src:
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
            for info in src.infolist():
                if info.filename.startswith("word/media/"):
                    continue
                dst.writestr(info, src.read(info.filename))

    with pytest.raises(docs_intel.DocxWriteVerificationError):
        docs_intel._atomic_write_docx_bytes(
            out.getvalue(),
            path,
            pre_manifest=docs_intel._docx_structural_manifest(raw),
            protected_keys=("media_count", "style_count"),
        )

    with open(path, "rb") as fh:
        assert fh.read() == original_bytes
