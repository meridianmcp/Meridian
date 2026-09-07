"""Tests for tools/meridian_fallbacks/transactional_merge.py."""
from __future__ import annotations

from pathlib import Path

import pytest

import tools.meridian_fallbacks.transactional_merge as transactional_merge
from tools.meridian_fallbacks.figure_slot_manifest import (
    MANIFEST_COMPLETE,
    MANIFEST_CONTRADICTORY,
    MANIFEST_INCOMPLETE,
)
from tools.meridian_fallbacks.patch_manifest import PatchManifest
from tools.meridian_fallbacks.safe_ooxml_writer import read_parts_from_bytes
from tools.meridian_fallbacks.transactional_merge import (
    MergeConflictError,
    MergeResult,
    TransactionError,
    apply_patch_manifest,
    promote,
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


# ---------------------------------------------------------------------------
# promote() -- the "no asset promotion without a complete slot manifest"
# enforcement point (sprint item 5cc3d745, "W31-C"). The end-to-end
# behavioral coverage (a real apply, a real refusal that leaves the on-disk
# file untouched, dry_run forwarding) lives alongside its consumer,
# figure_slot_manifest, in tests/test_figure_slot_manifest.py -- exactly the
# way this module's own sibling functions (apply_patch_manifest, rollback)
# are tested here rather than there. What belongs HERE, next to
# apply_patch_manifest's own tests, is the white-box proof that promote()
# does not merely produce the same observable outcome as a refusal would,
# but never calls apply_patch_manifest AT ALL on a failing verdict, and
# forwards every other keyword argument to it VERBATIM on a passing one --
# both proven with a monkeypatched spy rather than inferred from a side
# effect apply_patch_manifest happens to cause.
# ---------------------------------------------------------------------------


def _never_call(*args, **kwargs):  # pragma: no cover - only ever installed to prove it is NOT called
    raise AssertionError(
        f"apply_patch_manifest must not be called when promote() refuses a manifest "
        f"(got called with args={args!r}, kwargs={kwargs!r})"
    )


@pytest.mark.parametrize("verdict", [MANIFEST_INCOMPLETE, MANIFEST_CONTRADICTORY])
def test_promote_never_invokes_apply_patch_manifest_on_a_failing_verdict(
    monkeypatch, docx_path, verdict
):
    monkeypatch.setattr(transactional_merge, "apply_patch_manifest", _never_call)

    manifest = PatchManifest.create_from_file(docx_path)
    op = manifest.add_operation(
        "replace_part", "word/document.xml", "should never apply", payload=b"<doc/>"
    )
    reconciliation = {"verdict": verdict, "reasons": [f"synthetic {verdict} for this test"]}

    result = promote(manifest, reconciliation, payloads={op.op_id: b"<doc/>"})

    # _never_call would have raised (failing the test) had promote() actually
    # called through -- reaching this line at all is part of the proof.
    assert result.success is False
    assert verdict in result.error
    assert result.applied_operation_ids == []
    assert manifest.status == "draft"


def test_promote_never_invokes_apply_patch_manifest_when_reconciliation_lacks_a_verdict(
    monkeypatch, docx_path
):
    """A reconciliation-shaped mapping missing the 'verdict' key entirely
    must be treated the same as any other non-MANIFEST_COMPLETE value --
    still refused, still without ever calling through."""
    monkeypatch.setattr(transactional_merge, "apply_patch_manifest", _never_call)

    manifest = PatchManifest.create_from_file(docx_path)
    manifest.add_operation("replace_part", "word/document.xml", "no verdict key", payload=b"<doc/>")

    result = promote(manifest, {})

    assert result.success is False
    assert manifest.status == "draft"


def test_promote_forwards_every_kwarg_verbatim_when_verdict_is_complete(monkeypatch, docx_path):
    """Once the verdict check passes, promote() must be a pure pass-through:
    the exact manifest instance and every other keyword argument, unmodified,
    reach apply_patch_manifest, and promote() returns whatever
    apply_patch_manifest returned with no post-processing."""
    captured: dict[str, object] = {}
    sentinel_result = MergeResult(
        manifest_id="sentinel-manifest-id",
        success=True,
        applied_operation_ids=["op-1"],
        skipped_operation_ids=[],
        backup_path="/tmp/sentinel-backup",
        final_sha256="deadbeef",
        validation=None,
        error=None,
        dry_run=True,
    )

    def spy(manifest_arg, *, payloads, writer, appliers, allow_stale_base, dry_run, required_parts):
        captured["manifest"] = manifest_arg
        captured["payloads"] = payloads
        captured["writer"] = writer
        captured["appliers"] = appliers
        captured["allow_stale_base"] = allow_stale_base
        captured["dry_run"] = dry_run
        captured["required_parts"] = required_parts
        return sentinel_result

    monkeypatch.setattr(transactional_merge, "apply_patch_manifest", spy)

    manifest = PatchManifest.create_from_file(docx_path)
    op = manifest.add_operation("replace_part", "word/document.xml", "x", payload=b"<doc/>")
    reconciliation = {"verdict": MANIFEST_COMPLETE, "reasons": ["all slots classified"]}
    sentinel_writer = object()
    sentinel_appliers = {"custom": lambda parts, operation, payload: parts}
    sentinel_required_parts = ("word/document.xml",)

    result = promote(
        manifest,
        reconciliation,
        payloads={op.op_id: b"<doc/>"},
        writer=sentinel_writer,
        appliers=sentinel_appliers,
        allow_stale_base=True,
        dry_run=True,
        required_parts=sentinel_required_parts,
    )

    # promote() returns apply_patch_manifest's result completely unmodified.
    assert result is sentinel_result
    assert captured["manifest"] is manifest
    assert captured["payloads"] == {op.op_id: b"<doc/>"}
    assert captured["writer"] is sentinel_writer
    assert captured["appliers"] is sentinel_appliers
    assert captured["allow_stale_base"] is True
    assert captured["dry_run"] is True
    assert captured["required_parts"] == sentinel_required_parts


def test_promote_with_default_kwargs_forwards_the_same_defaults_apply_patch_manifest_declares(
    monkeypatch, docx_path
):
    """A caller who calls promote() with no optional kwargs at all must see
    IDENTICAL behavior to calling apply_patch_manifest directly with no
    optional kwargs -- i.e. promote() must not silently substitute its own
    different defaults for payloads/writer/appliers/allow_stale_base/
    dry_run/required_parts."""
    captured: dict[str, object] = {}

    def spy(manifest_arg, *, payloads, writer, appliers, allow_stale_base, dry_run, required_parts):
        captured["payloads"] = payloads
        captured["writer"] = writer
        captured["appliers"] = appliers
        captured["allow_stale_base"] = allow_stale_base
        captured["dry_run"] = dry_run
        captured["required_parts"] = required_parts
        return "sentinel-return-value"

    monkeypatch.setattr(transactional_merge, "apply_patch_manifest", spy)

    manifest = PatchManifest.create_from_file(docx_path)
    manifest.add_operation("replace_part", "word/document.xml", "x", payload=b"<doc/>")
    reconciliation = {"verdict": MANIFEST_COMPLETE, "reasons": []}

    result = promote(manifest, reconciliation)

    assert result == "sentinel-return-value"
    assert captured["payloads"] is None
    assert captured["writer"] is None
    assert captured["appliers"] is None
    assert captured["allow_stale_base"] is False
    assert captured["dry_run"] is False
    from tools.meridian_fallbacks.safe_ooxml_writer import REQUIRED_PARTS

    assert captured["required_parts"] == REQUIRED_PARTS


def test_promote_type_errors_on_non_mapping_reconciliation_without_calling_through(
    monkeypatch, docx_path
):
    monkeypatch.setattr(transactional_merge, "apply_patch_manifest", _never_call)

    manifest = PatchManifest.create_from_file(docx_path)
    manifest.add_operation("replace_part", "word/document.xml", "x", payload=b"<doc/>")

    with pytest.raises(TypeError, match="mapping"):
        promote(manifest, ["not", "a", "mapping"])
    with pytest.raises(TypeError):
        promote(manifest, None)
    with pytest.raises(TypeError):
        promote(manifest, "manifest_complete")
