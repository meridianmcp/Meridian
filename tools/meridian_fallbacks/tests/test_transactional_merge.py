"""Tests for tools/meridian_fallbacks/transactional_merge.py."""
from __future__ import annotations

from pathlib import Path

import pytest

from tools.meridian_fallbacks.patch_manifest import PatchManifest
from tools.meridian_fallbacks.safe_ooxml_writer import read_parts_from_bytes
from tools.meridian_fallbacks.transactional_merge import (
    MergeConflictError,
    TransactionError,
    apply_patch_manifest,
    rollback,
)

from .conftest import make_document_xml


# ---------------------------------------------------------------------------
# replace_part
# ---------------------------------------------------------------------------


def test_replace_part_success_writes_and_marks_applied(docx_path):
    original_bytes = docx_path.read_bytes()
    manifest = PatchManifest.create_from_file(docx_path)
    new_doc_xml = make_document_xml(["Changed!"])
    op = manifest.add_operation("replace_part", "word/document.xml", "swap body", payload=new_doc_xml)

    result = apply_patch_manifest(manifest, payloads={op.op_id: new_doc_xml})

    assert result.success is True
    assert result.applied_operation_ids == [op.op_id]
    assert manifest.status == "applied"
    assert result.backup_path is not None
    assert Path(result.backup_path).read_bytes() == original_bytes

    on_disk = read_parts_from_bytes(docx_path.read_bytes())
    assert on_disk["word/document.xml"] == new_doc_xml


def test_missing_payload_aborts_without_writing(docx_path):
    original_bytes = docx_path.read_bytes()
    manifest = PatchManifest.create_from_file(docx_path)
    manifest.add_operation("replace_part", "word/document.xml", "swap body", payload=b"data")

    result = apply_patch_manifest(manifest, payloads={})

    assert result.success is False
    assert "no payload" in result.error
    assert manifest.status == "aborted"
    assert docx_path.read_bytes() == original_bytes


def test_payload_hash_mismatch_aborts_without_writing(docx_path):
    original_bytes = docx_path.read_bytes()
    manifest = PatchManifest.create_from_file(docx_path)
    op = manifest.add_operation(
        "replace_part", "word/document.xml", "swap body", payload=b"reviewed-bytes"
    )

    result = apply_patch_manifest(manifest, payloads={op.op_id: b"different-bytes-entirely"})

    assert result.success is False
    assert "hash mismatch" in result.error
    assert manifest.status == "aborted"
    assert docx_path.read_bytes() == original_bytes


def test_unknown_operation_kind_at_apply_time(docx_path):
    # 'custom' is a legal PatchOperation kind, but apply_patch_manifest has
    # no default applier registered for it unless the caller supplies one.
    manifest = PatchManifest.create_from_file(docx_path)
    manifest.add_operation("custom", "word/whatever.xml", "needs a custom applier")

    result = apply_patch_manifest(manifest)

    assert result.success is False
    assert "no applier registered" in result.error
    assert manifest.status == "aborted"


def test_custom_applier_is_used_when_supplied(docx_path):
    manifest = PatchManifest.create_from_file(docx_path)
    op = manifest.add_operation(
        "custom", "word/custom.xml", "add a custom part", payload=b"<custom/>"
    )

    def add_custom_part(parts, operation, payload):
        new_parts = dict(parts)
        new_parts[operation.target_part] = payload
        return new_parts

    result = apply_patch_manifest(
        manifest,
        payloads={op.op_id: b"<custom/>"},
        appliers={"custom": add_custom_part},
    )

    assert result.success is True
    on_disk = read_parts_from_bytes(docx_path.read_bytes())
    assert on_disk["word/custom.xml"] == b"<custom/>"


# ---------------------------------------------------------------------------
# insert_image operation kind
# ---------------------------------------------------------------------------


def test_insert_image_operation_success(docx_path, fake_image_bytes):
    manifest = PatchManifest.create_from_file(docx_path)
    op = manifest.add_operation(
        "insert_image",
        "word/document.xml",
        "insert a picture",
        payload=fake_image_bytes,
        metadata={"image_ext": "png"},
    )

    result = apply_patch_manifest(manifest, payloads={op.op_id: fake_image_bytes})

    assert result.success is True
    on_disk = read_parts_from_bytes(docx_path.read_bytes())
    assert on_disk["word/media/image1.png"] == fake_image_bytes


def test_insert_image_operation_missing_image_ext_metadata(docx_path, fake_image_bytes):
    manifest = PatchManifest.create_from_file(docx_path)
    op = manifest.add_operation(
        "insert_image", "word/document.xml", "insert a picture", payload=fake_image_bytes
    )

    result = apply_patch_manifest(manifest, payloads={op.op_id: fake_image_bytes})

    assert result.success is False
    assert "image_ext" in result.error


# ---------------------------------------------------------------------------
# Staleness / conflict handling
# ---------------------------------------------------------------------------


def test_stale_base_raises_merge_conflict(docx_path):
    manifest = PatchManifest.create_from_file(docx_path)
    docx_path.write_bytes(b"someone else changed this file concurrently")

    with pytest.raises(MergeConflictError):
        apply_patch_manifest(manifest)


def test_allow_stale_base_bypasses_conflict(docx_path, minimal_docx_parts):
    manifest = PatchManifest.create_from_file(docx_path)
    from .conftest import zip_parts

    docx_path.write_bytes(zip_parts(minimal_docx_parts))  # still valid, but "changed"

    result = apply_patch_manifest(manifest, allow_stale_base=True)

    assert result.success is True
    assert manifest.status == "applied"


def test_target_missing_fails_and_aborts(tmp_path):
    manifest = PatchManifest.create(tmp_path / "nope.docx")
    result = apply_patch_manifest(manifest)
    assert result.success is False
    assert "does not exist" in result.error
    assert manifest.status == "aborted"


def test_reapplying_an_already_applied_manifest_is_a_no_op_failure(docx_path):
    manifest = PatchManifest.create_from_file(docx_path)
    manifest.mark_applied()

    result = apply_patch_manifest(manifest)

    assert result.success is False
    assert "already been applied" in result.error


# ---------------------------------------------------------------------------
# dry_run: never mutates manifest state, never writes to disk
# ---------------------------------------------------------------------------


def test_dry_run_does_not_write_to_disk(docx_path):
    original_bytes = docx_path.read_bytes()
    manifest = PatchManifest.create_from_file(docx_path)
    new_doc_xml = make_document_xml(["dry run body"])
    op = manifest.add_operation("replace_part", "word/document.xml", "swap body", payload=new_doc_xml)

    result = apply_patch_manifest(manifest, payloads={op.op_id: new_doc_xml}, dry_run=True)

    assert result.success is True
    assert result.dry_run is True
    assert result.backup_path is None
    assert docx_path.read_bytes() == original_bytes
    # A successful dry run never mutates manifest state -- still draft.
    assert manifest.status == "draft"


def test_dry_run_failure_does_not_abort_manifest_and_can_be_retried(docx_path):
    manifest = PatchManifest.create_from_file(docx_path)
    reviewed_payload = make_document_xml(["reviewed"])
    op = manifest.add_operation(
        "replace_part", "word/document.xml", "swap body", payload=reviewed_payload
    )

    bad_result = apply_patch_manifest(
        manifest, payloads={op.op_id: b"wrong-bytes"}, dry_run=True
    )
    assert bad_result.success is False
    assert manifest.status == "draft"  # NOT aborted -- a dry run is a pure preview

    # Because the manifest is still draft, a real apply with the correct
    # payload can proceed without rebuilding the manifest from scratch.
    good_result = apply_patch_manifest(manifest, payloads={op.op_id: reviewed_payload})
    assert good_result.success is True
    assert manifest.status == "applied"


# ---------------------------------------------------------------------------
# rollback
# ---------------------------------------------------------------------------


def test_rollback_restores_pre_apply_content(docx_path):
    original_bytes = docx_path.read_bytes()
    manifest = PatchManifest.create_from_file(docx_path)
    new_doc_xml = make_document_xml(["post-apply"])
    op = manifest.add_operation("replace_part", "word/document.xml", "swap body", payload=new_doc_xml)
    result = apply_patch_manifest(manifest, payloads={op.op_id: new_doc_xml})
    assert docx_path.read_bytes() != original_bytes

    rollback(manifest, result)

    assert docx_path.read_bytes() == original_bytes


def test_rollback_without_backup_raises(docx_path):
    manifest = PatchManifest.create_from_file(docx_path)
    new_doc_xml = make_document_xml(["x"])
    op = manifest.add_operation("replace_part", "word/document.xml", "swap body", payload=new_doc_xml)
    dry_result = apply_patch_manifest(manifest, payloads={op.op_id: new_doc_xml}, dry_run=True)

    with pytest.raises(TransactionError, match="no backup_path"):
        rollback(manifest, dry_result)
