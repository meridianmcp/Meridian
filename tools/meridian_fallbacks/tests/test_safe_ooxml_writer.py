"""Tests for tools/meridian_fallbacks/safe_ooxml_writer.py."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from tools.meridian_fallbacks.safe_ooxml_writer import (
    DocxValidationError,
    DocxWriteError,
    SafeOoxmlWriter,
    build_zip_bytes,
    compute_sha256,
    read_parts_from_bytes,
    verify_zip_bytes,
)

from .conftest import make_document_xml, make_minimal_docx_parts, zip_parts


# ---------------------------------------------------------------------------
# compute_sha256
# ---------------------------------------------------------------------------


def test_compute_sha256_deterministic_and_sensitive():
    a = compute_sha256(b"hello world")
    b = compute_sha256(b"hello world")
    c = compute_sha256(b"hello world!")
    assert a == b
    assert a != c
    assert len(a) == 64  # hex sha256


# ---------------------------------------------------------------------------
# build_zip_bytes / read_parts_from_bytes roundtrip
# ---------------------------------------------------------------------------


def test_build_zip_bytes_roundtrips_through_read_parts_from_bytes(minimal_docx_parts):
    data = build_zip_bytes(minimal_docx_parts)
    roundtripped = read_parts_from_bytes(data)
    assert roundtripped == minimal_docx_parts


def test_read_parts_from_bytes_rejects_non_zip():
    with pytest.raises(DocxWriteError, match="not a valid zip archive"):
        read_parts_from_bytes(b"this is definitely not a zip file")


# ---------------------------------------------------------------------------
# verify_zip_bytes
# ---------------------------------------------------------------------------


def test_verify_zip_bytes_valid_minimal_docx(minimal_docx_parts):
    data = zip_parts(minimal_docx_parts)
    report = verify_zip_bytes(data)
    assert report.valid is True
    assert report.errors == []
    assert "word/document.xml" in report.parts
    assert report.byte_size == len(data)
    assert report.sha256 == compute_sha256(data)


def test_verify_zip_bytes_not_a_zip():
    report = verify_zip_bytes(b"not a zip at all")
    assert report.valid is False
    assert any("not a valid zip archive" in e for e in report.errors)
    assert report.parts == []


def test_verify_zip_bytes_missing_required_parts():
    data = zip_parts({"word/document.xml": make_document_xml()})
    report = verify_zip_bytes(data)
    assert report.valid is False
    assert any("[Content_Types].xml" in e for e in report.errors)
    assert any("_rels/.rels" in e for e in report.errors)


def test_verify_zip_bytes_malformed_document_xml(minimal_docx_parts):
    parts = dict(minimal_docx_parts)
    parts["word/document.xml"] = b"<w:document><w:body><w:p>unclosed"
    data = zip_parts(parts)
    report = verify_zip_bytes(data)
    assert report.valid is False
    assert any("not well-formed XML" in e for e in report.errors)


def test_verify_zip_bytes_custom_required_parts_empty_tuple():
    # An empty required_parts tuple means "just validate it's a real zip".
    data = zip_parts({"anything.txt": b"hello"})
    report = verify_zip_bytes(data, required_parts=())
    assert report.valid is True


# ---------------------------------------------------------------------------
# SafeOoxmlWriter.read_parts
# ---------------------------------------------------------------------------


def test_read_parts_missing_file_raises(tmp_path):
    writer = SafeOoxmlWriter(tmp_path / "missing.docx")
    assert writer.exists() is False
    with pytest.raises(DocxWriteError, match="does not exist"):
        writer.read_parts()


def test_read_parts_returns_all_parts(docx_path):
    writer = SafeOoxmlWriter(docx_path)
    parts = writer.read_parts()
    assert set(parts) == set(make_minimal_docx_parts())


# ---------------------------------------------------------------------------
# SafeOoxmlWriter.write_parts
# ---------------------------------------------------------------------------


def test_write_parts_creates_new_file_with_no_backup(tmp_path, minimal_docx_parts):
    target = tmp_path / "brand_new.docx"
    writer = SafeOoxmlWriter(target)
    assert not target.exists()

    result = writer.write_parts(minimal_docx_parts)

    assert target.exists()
    assert result.backup_path is None  # nothing existed to back up
    assert result.validation.valid is True
    assert read_parts_from_bytes(target.read_bytes()) == minimal_docx_parts


def test_write_parts_backs_up_previous_content_on_overwrite(docx_path, minimal_docx_parts):
    original_bytes = docx_path.read_bytes()
    writer = SafeOoxmlWriter(docx_path)

    new_parts = dict(minimal_docx_parts)
    new_parts["word/document.xml"] = make_document_xml(["Changed body."])
    result = writer.write_parts(new_parts)

    assert result.backup_path is not None
    backup_path = Path(result.backup_path)
    assert backup_path.is_file()
    assert backup_path.read_bytes() == original_bytes
    assert docx_path.read_bytes() != original_bytes
    assert read_parts_from_bytes(docx_path.read_bytes())["word/document.xml"] == new_parts["word/document.xml"]


def test_write_parts_validation_failure_leaves_target_untouched(docx_path):
    original_bytes = docx_path.read_bytes()
    writer = SafeOoxmlWriter(docx_path)

    broken_parts = {"word/document.xml": b"<not-well-formed"}
    with pytest.raises(DocxValidationError) as excinfo:
        writer.write_parts(broken_parts)

    assert excinfo.value.report.valid is False
    # Target must be byte-for-byte untouched, and no backup should have been
    # created (validation runs strictly BEFORE the backup step).
    assert docx_path.read_bytes() == original_bytes
    leftover_backups = list(docx_path.parent.glob("*.bak-*"))
    assert leftover_backups == []


def test_write_parts_can_skip_validation(tmp_path):
    # validate=False is an explicit opt-out for callers that have already
    # validated (or intentionally want to write something this module's
    # narrow checks would reject, e.g. a non-.docx OPC zip in a test).
    target = tmp_path / "no_validate.docx"
    writer = SafeOoxmlWriter(target)
    result = writer.write_parts({"whatever.txt": b"not a docx at all"}, validate=False)
    assert target.exists()
    assert result.validation.valid is False  # report is still computed and returned


# ---------------------------------------------------------------------------
# SafeOoxmlWriter.restore_backup
# ---------------------------------------------------------------------------


def test_restore_backup_roundtrip(docx_path, minimal_docx_parts):
    original_bytes = docx_path.read_bytes()
    writer = SafeOoxmlWriter(docx_path)

    new_parts = dict(minimal_docx_parts)
    new_parts["word/document.xml"] = make_document_xml(["v2"])
    write_result = writer.write_parts(new_parts)
    assert docx_path.read_bytes() != original_bytes

    restore_result = writer.restore_backup(write_result.backup_path)

    assert docx_path.read_bytes() == original_bytes
    # The restore itself backed up the pre-restore (v2) state.
    assert restore_result.backup_path is not None
    pre_restore_backup_bytes = Path(restore_result.backup_path).read_bytes()
    assert verify_zip_bytes(pre_restore_backup_bytes).valid
    assert (
        read_parts_from_bytes(pre_restore_backup_bytes)["word/document.xml"]
        == new_parts["word/document.xml"]
    )


def test_restore_backup_missing_file_raises(tmp_path, docx_path):
    writer = SafeOoxmlWriter(docx_path)
    with pytest.raises(DocxWriteError, match="backup does not exist"):
        writer.restore_backup(tmp_path / "nope.docx")


# ---------------------------------------------------------------------------
# Atomicity of the low-level replace step
# ---------------------------------------------------------------------------


def test_atomic_replace_cleans_up_temp_file_on_replace_failure(tmp_path, monkeypatch):
    target = tmp_path / "x.docx"
    writer = SafeOoxmlWriter(target)

    def boom(_src, _dst):
        raise OSError("simulated os.replace failure")

    monkeypatch.setattr(os, "replace", boom)

    with pytest.raises(OSError, match="simulated os.replace failure"):
        writer._atomic_replace(b"some bytes")

    assert not target.exists()
    leftover_temp_files = [p for p in tmp_path.iterdir() if p.name.startswith(".x.docx.")]
    assert leftover_temp_files == []
