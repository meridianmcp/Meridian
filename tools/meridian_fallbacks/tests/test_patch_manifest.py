"""Tests for tools/meridian_fallbacks/patch_manifest.py."""
from __future__ import annotations

import os

import pytest

from tools.meridian_fallbacks.patch_manifest import (
    PATCH_MANIFEST_SCHEMA_VERSION,
    PatchManifest,
    PatchManifestError,
)
from tools.meridian_fallbacks.safe_ooxml_writer import compute_sha256


# ---------------------------------------------------------------------------
# create / create_from_file
# ---------------------------------------------------------------------------


def test_create_generates_fresh_ids_and_draft_status():
    m1 = PatchManifest.create("target.docx")
    m2 = PatchManifest.create("target.docx")
    assert m1.manifest_id != m2.manifest_id
    assert m1.status == "draft"
    assert m1.base_sha256 is None
    assert m1.operations == []
    assert m1.schema_version == PATCH_MANIFEST_SCHEMA_VERSION


def test_create_from_file_no_existing_file_has_no_base_hash(tmp_path):
    manifest = PatchManifest.create_from_file(tmp_path / "missing.docx")
    assert manifest.base_sha256 is None


def test_create_from_file_existing_file_records_hash(docx_path):
    manifest = PatchManifest.create_from_file(docx_path)
    assert manifest.base_sha256 == compute_sha256(docx_path.read_bytes())


# ---------------------------------------------------------------------------
# add_operation
# ---------------------------------------------------------------------------


def test_add_operation_records_hash_and_size():
    manifest = PatchManifest.create("target.docx")
    op = manifest.add_operation(
        "replace_part", "word/document.xml", "swap body", payload=b"hello world"
    )
    assert op.payload_sha256 == compute_sha256(b"hello world")
    assert op.payload_size == len(b"hello world")
    assert manifest.operations == [op]


def test_add_operation_without_payload_has_no_hash():
    manifest = PatchManifest.create("target.docx")
    op = manifest.add_operation("custom", "word/other.xml", "no payload op")
    assert op.payload_sha256 is None
    assert op.payload_size is None


def test_add_operation_rejects_unknown_kind():
    manifest = PatchManifest.create("target.docx")
    with pytest.raises(PatchManifestError, match="unknown operation kind"):
        manifest.add_operation("delete_everything", "word/document.xml", "nope")


def test_add_operation_requires_target_part():
    manifest = PatchManifest.create("target.docx")
    with pytest.raises(PatchManifestError, match="target_part is required"):
        manifest.add_operation("replace_part", "", "nope")


def test_add_operation_after_applied_raises():
    manifest = PatchManifest.create("target.docx")
    manifest.mark_applied()
    with pytest.raises(PatchManifestError, match="status 'applied'"):
        manifest.add_operation("replace_part", "word/document.xml", "too late")


def test_add_operation_after_aborted_raises():
    manifest = PatchManifest.create("target.docx")
    manifest.mark_aborted("some reason")
    with pytest.raises(PatchManifestError, match="status 'aborted'"):
        manifest.add_operation("replace_part", "word/document.xml", "too late")


# ---------------------------------------------------------------------------
# mark_applied / mark_aborted
# ---------------------------------------------------------------------------


def test_mark_applied_sets_status_and_timestamp():
    manifest = PatchManifest.create("target.docx")
    assert manifest.applied_at is None
    manifest.mark_applied()
    assert manifest.status == "applied"
    assert manifest.applied_at is not None


def test_mark_aborted_sets_status_and_reason():
    manifest = PatchManifest.create("target.docx")
    manifest.mark_aborted("payload hash mismatch")
    assert manifest.status == "aborted"
    assert manifest.aborted_reason == "payload hash mismatch"


# ---------------------------------------------------------------------------
# verify_base_unchanged
# ---------------------------------------------------------------------------


def test_verify_base_unchanged_none_base_is_trivially_true(tmp_path):
    manifest = PatchManifest.create(tmp_path / "whatever.docx", base_sha256=None)
    assert manifest.verify_base_unchanged() is True


def test_verify_base_unchanged_matches_current_file(docx_path):
    manifest = PatchManifest.create_from_file(docx_path)
    assert manifest.verify_base_unchanged() is True


def test_verify_base_unchanged_detects_drift(docx_path):
    manifest = PatchManifest.create_from_file(docx_path)
    docx_path.write_bytes(b"completely different content now")
    assert manifest.verify_base_unchanged() is False


def test_verify_base_unchanged_missing_file_is_false(docx_path):
    manifest = PatchManifest.create_from_file(docx_path)
    os.remove(docx_path)
    assert manifest.verify_base_unchanged() is False


def test_verify_base_unchanged_accepts_explicit_bytes(docx_path):
    manifest = PatchManifest.create_from_file(docx_path)
    assert manifest.verify_base_unchanged(current_bytes=docx_path.read_bytes()) is True
    assert manifest.verify_base_unchanged(current_bytes=b"different") is False


# ---------------------------------------------------------------------------
# to_dict / from_dict and to_json / from_json roundtrips
# ---------------------------------------------------------------------------


def test_to_dict_from_dict_roundtrip():
    manifest = PatchManifest.create("target.docx", notes="review before applying")
    manifest.add_operation("replace_part", "word/document.xml", "op one", payload=b"aaa")
    manifest.add_operation("insert_image", "word/document.xml", "op two", metadata={"image_ext": "png"})

    restored = PatchManifest.from_dict(manifest.to_dict())

    assert restored.manifest_id == manifest.manifest_id
    assert restored.target_docx_path == manifest.target_docx_path
    assert restored.notes == manifest.notes
    assert [op.to_dict() for op in restored.operations] == [op.to_dict() for op in manifest.operations]


def test_to_json_from_json_roundtrip():
    manifest = PatchManifest.create("target.docx")
    manifest.add_operation("replace_part", "word/document.xml", "op one", payload=b"aaa")

    restored = PatchManifest.from_json(manifest.to_json())

    assert restored.to_dict() == manifest.to_dict()


def test_from_dict_rejects_wrong_schema_version():
    data = PatchManifest.create("target.docx").to_dict()
    data["schema_version"] = 999
    with pytest.raises(PatchManifestError, match="unsupported patch manifest schema_version"):
        PatchManifest.from_dict(data)


def test_from_dict_rejects_non_dict():
    with pytest.raises(PatchManifestError, match="must be a dict"):
        PatchManifest.from_dict(["not", "a", "dict"])  # type: ignore[arg-type]


def test_from_dict_missing_required_field_raises():
    data = PatchManifest.create("target.docx").to_dict()
    del data["manifest_id"]
    with pytest.raises(PatchManifestError, match="missing required field"):
        PatchManifest.from_dict(data)


def test_from_json_rejects_invalid_json():
    with pytest.raises(PatchManifestError, match="invalid JSON"):
        PatchManifest.from_json("{not valid json")


# ---------------------------------------------------------------------------
# save / load
# ---------------------------------------------------------------------------


def test_save_and_load_roundtrip(tmp_path):
    manifest = PatchManifest.create("target.docx")
    manifest.add_operation("replace_part", "word/document.xml", "op one", payload=b"aaa")
    path = tmp_path / "manifest.json"

    manifest.save(path)
    loaded = PatchManifest.load(path)

    assert loaded.to_dict() == manifest.to_dict()


def test_save_is_atomic_and_leaves_no_temp_file_on_failure(tmp_path, monkeypatch):
    manifest = PatchManifest.create("target.docx")
    path = tmp_path / "manifest.json"
    path.write_text('{"pre-existing": true}', encoding="utf-8")

    def boom(_src, _dst):
        raise OSError("simulated os.replace failure")

    monkeypatch.setattr(os, "replace", boom)

    with pytest.raises(OSError, match="simulated os.replace failure"):
        manifest.save(path)

    # Original file is untouched, and no leftover temp file remains.
    assert path.read_text(encoding="utf-8") == '{"pre-existing": true}'
    leftover = [p for p in tmp_path.iterdir() if p.name.startswith(".manifest.json.")]
    assert leftover == []
