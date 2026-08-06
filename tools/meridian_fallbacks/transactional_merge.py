"""transactional_merge.py -- apply a :class:`~patch_manifest.PatchManifest`'s
operations against its target .docx as ONE all-or-nothing transaction, on
top of ``safe_ooxml_writer.SafeOoxmlWriter``'s validate-before-commit write.

Part of ``tools/meridian_fallbacks`` -- see ``capability_manifest.json``.

Transaction model
------------------
1. **Staleness check.** If the manifest recorded a ``base_sha256``, it is
   compared against the target's CURRENT on-disk hash. A mismatch means the
   file changed since the manifest was authored/reviewed, and the merge
   refuses to proceed (:class:`MergeConflictError`) unless the caller
   explicitly passes ``allow_stale_base=True``.
2. **In-memory apply.** Every operation is applied, in order, to an
   in-memory ``{part_name: bytes}`` mapping -- never to disk. Each
   operation's payload (supplied via the ``payloads`` argument, keyed by
   ``op_id``) is hash-verified against the operation's recorded
   ``payload_sha256`` before it is used, so a manifest can never be
   "applied" against substituted bytes.
3. **All-or-nothing.** If ANY operation raises, the whole in-memory mapping
   is discarded and NOTHING is written to disk -- the target file is left
   exactly as it was. This is what makes the transaction atomic even though
   the underlying write (:meth:`SafeOoxmlWriter.write_parts`) only happens
   once, at the very end, after every operation has already succeeded.
4. **Commit.** The fully-patched in-memory mapping is handed to
   :meth:`SafeOoxmlWriter.write_parts`, which itself validates the result
   BEFORE ever touching the real target path (see that module's docstring).
   A ``dry_run=True`` call performs steps 1-3 plus the same validation, but
   skips this step entirely -- nothing is written and no backup is created.

``dry_run=True`` is a pure preview: it NEVER mutates ``manifest.status``,
success or failure, specifically so a failed preview (a bad payload, a
stale base) can be fixed and re-previewed without first having to build a
brand-new manifest. Only a real (non-dry-run) call ever transitions a
manifest out of ``"draft"`` -- to ``"applied"`` on success or ``"aborted"``
(with a recorded reason) on any failure.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .patch_manifest import PatchManifest, PatchOperation
from .safe_image_insert import compute_image_insert_parts
from .safe_ooxml_writer import (
    REQUIRED_PARTS,
    DocxValidationReport,
    DocxWriteError,
    SafeOoxmlWriter,
    WriteResult,
    build_zip_bytes,
    compute_sha256,
    verify_zip_bytes,
)

Applier = Callable[[dict[str, bytes], PatchOperation, "bytes | None"], dict[str, bytes]]


class TransactionError(Exception):
    """Raised when an operation cannot be applied. Never leaves the target
    file modified -- see module docstring's "all-or-nothing" guarantee."""


class MergeConflictError(TransactionError):
    """Raised when the manifest's recorded ``base_sha256`` no longer matches
    the target file's current content, and ``allow_stale_base`` was not
    passed."""


@dataclass
class MergeResult:
    """Outcome of :func:`apply_patch_manifest`."""

    manifest_id: str
    success: bool
    applied_operation_ids: list[str]
    skipped_operation_ids: list[str]
    backup_path: str | None
    final_sha256: str | None
    validation: DocxValidationReport | None
    error: str | None
    dry_run: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest_id": self.manifest_id,
            "success": self.success,
            "applied_operation_ids": list(self.applied_operation_ids),
            "skipped_operation_ids": list(self.skipped_operation_ids),
            "backup_path": self.backup_path,
            "final_sha256": self.final_sha256,
            "validation": self.validation.to_dict() if self.validation else None,
            "error": self.error,
            "dry_run": self.dry_run,
        }


def _apply_replace_part(
    parts: dict[str, bytes], op: PatchOperation, payload: "bytes | None"
) -> dict[str, bytes]:
    if payload is None:
        raise TransactionError(
            f"operation {op.op_id} (replace_part) requires a payload but none was supplied"
        )
    new_parts = dict(parts)
    new_parts[op.target_part] = payload
    return new_parts


def _apply_insert_image(
    parts: dict[str, bytes], op: PatchOperation, payload: "bytes | None"
) -> dict[str, bytes]:
    if payload is None:
        raise TransactionError(
            f"operation {op.op_id} (insert_image) requires image payload bytes but none was supplied"
        )
    meta = op.metadata or {}
    image_ext = meta.get("image_ext")
    if not image_ext:
        raise TransactionError(f"operation {op.op_id} (insert_image) metadata must include 'image_ext'")
    result = compute_image_insert_parts(
        parts,
        payload,
        image_ext=image_ext,
        anchor_text=meta.get("anchor_text"),
        paragraph_index=meta.get("paragraph_index"),
        width_emu=meta.get("width_emu", 914400),
        height_emu=meta.get("height_emu", 914400),
        drawing_name=meta.get("drawing_name", "Picture"),
    )
    return result.parts


DEFAULT_APPLIERS: dict[str, Applier] = {
    "replace_part": _apply_replace_part,
    "insert_image": _apply_insert_image,
}


def _empty_result(manifest: PatchManifest, *, error: str, dry_run: bool) -> MergeResult:
    return MergeResult(
        manifest_id=manifest.manifest_id,
        success=False,
        applied_operation_ids=[],
        skipped_operation_ids=[op.op_id for op in manifest.operations],
        backup_path=None,
        final_sha256=None,
        validation=None,
        error=error,
        dry_run=dry_run,
    )


def apply_patch_manifest(
    manifest: PatchManifest,
    *,
    payloads: dict[str, bytes] | None = None,
    writer: SafeOoxmlWriter | None = None,
    appliers: dict[str, Applier] | None = None,
    allow_stale_base: bool = False,
    dry_run: bool = False,
    required_parts: tuple[str, ...] = REQUIRED_PARTS,
) -> MergeResult:
    """Apply every operation in ``manifest`` to its ``target_docx_path`` as
    one all-or-nothing transaction. See module docstring for the full
    contract. Mutates ``manifest.status`` in place (``"applied"`` on
    success, ``"aborted"`` with a recorded reason on failure) -- callers
    that want to retry after fixing a payload should build a fresh
    manifest rather than reuse an aborted one (mirrors
    ``PatchManifest.add_operation``'s "only draft manifests are mutable"
    rule).
    """
    if manifest.status != "draft":
        return _empty_result(
            manifest,
            error=(
                f"manifest status is {manifest.status!r}, not 'draft' -- "
                "it has already been applied or aborted"
            ),
            dry_run=dry_run,
        )

    payloads = payloads or {}
    merged_appliers: dict[str, Applier] = dict(DEFAULT_APPLIERS)
    if appliers:
        merged_appliers.update(appliers)

    target_path = manifest.target_docx_path
    writer = writer or SafeOoxmlWriter(target_path)

    if not writer.exists():
        error = f"target docx does not exist: {target_path}"
        if not dry_run:
            manifest.mark_aborted(error)
        return _empty_result(manifest, error=error, dry_run=dry_run)

    current_bytes = Path(target_path).read_bytes()
    if not manifest.verify_base_unchanged(current_bytes=current_bytes) and not allow_stale_base:
        raise MergeConflictError(
            f"manifest {manifest.manifest_id} base_sha256 {manifest.base_sha256!r} no longer "
            f"matches the current file's hash {compute_sha256(current_bytes)!r} -- the target "
            "changed since this manifest was created. Re-create the manifest against the "
            "current file, or pass allow_stale_base=True to apply anyway."
        )

    parts = writer.read_parts()
    applied: list[str] = []

    try:
        for op in manifest.operations:
            applier = merged_appliers.get(op.kind)
            if applier is None:
                raise TransactionError(
                    f"no applier registered for operation kind {op.kind!r} (op_id={op.op_id})"
                )
            payload = payloads.get(op.op_id)
            if op.payload_sha256 is not None:
                if payload is None:
                    raise TransactionError(
                        f"operation {op.op_id} recorded a payload_sha256 but no payload "
                        "bytes were supplied for it"
                    )
                actual_hash = compute_sha256(payload)
                if actual_hash != op.payload_sha256:
                    raise TransactionError(
                        f"operation {op.op_id} payload hash mismatch: manifest recorded "
                        f"{op.payload_sha256}, supplied payload hashes to {actual_hash} -- "
                        "refusing to apply a payload that does not match what was reviewed "
                        "when the manifest was authored"
                    )
            parts = applier(parts, op, payload)
            applied.append(op.op_id)
    except TransactionError as exc:
        # Nothing has been written to disk at any point in this branch --
        # `parts` was only ever mutated in memory, and is discarded here.
        # A dry run NEVER mutates manifest state (see module docstring) so
        # a failed preview can be retried after fixing the payload/manifest
        # without first having to build a brand-new manifest.
        if not dry_run:
            manifest.mark_aborted(str(exc))
        return _empty_result(manifest, error=str(exc), dry_run=dry_run)

    if dry_run:
        candidate = build_zip_bytes(parts)
        report = verify_zip_bytes(candidate, required_parts=required_parts)
        if not report.valid:
            return MergeResult(
                manifest_id=manifest.manifest_id,
                success=False,
                applied_operation_ids=applied,
                skipped_operation_ids=[],
                backup_path=None,
                final_sha256=None,
                validation=report,
                error="dry_run validation failed",
                dry_run=True,
            )
        return MergeResult(
            manifest_id=manifest.manifest_id,
            success=True,
            applied_operation_ids=applied,
            skipped_operation_ids=[],
            backup_path=None,
            final_sha256=report.sha256,
            validation=report,
            error=None,
            dry_run=True,
        )

    try:
        write_result = writer.write_parts(parts, required_parts=required_parts)
    except DocxWriteError as exc:
        manifest.mark_aborted(f"write failed: {exc}")
        return MergeResult(
            manifest_id=manifest.manifest_id,
            success=False,
            applied_operation_ids=[],
            skipped_operation_ids=[op.op_id for op in manifest.operations],
            backup_path=None,
            final_sha256=None,
            validation=getattr(exc, "report", None),
            error=str(exc),
            dry_run=False,
        )

    manifest.mark_applied()
    return MergeResult(
        manifest_id=manifest.manifest_id,
        success=True,
        applied_operation_ids=applied,
        skipped_operation_ids=[],
        backup_path=write_result.backup_path,
        final_sha256=write_result.sha256,
        validation=write_result.validation,
        error=None,
        dry_run=False,
    )


def rollback(
    manifest: PatchManifest,
    merge_result: MergeResult,
    *,
    writer: SafeOoxmlWriter | None = None,
) -> WriteResult:
    """Restore the target file to the state it was in immediately before a
    successful (non-dry-run) :func:`apply_patch_manifest` call, using the
    backup that call created."""
    if not merge_result.backup_path:
        raise TransactionError(
            "merge_result has no backup_path to roll back to (either the merge never wrote "
            "to disk, or it was a dry run)"
        )
    writer = writer or SafeOoxmlWriter(manifest.target_docx_path)
    return writer.restore_backup(merge_result.backup_path)
