"""patch_manifest.py -- durable, JSON-serializable record of PLANNED patches
against one .docx target, authored and reviewable BEFORE anything is
written (see ``transactional_merge.py`` in this same package for the apply
step).

Part of ``tools/meridian_fallbacks`` -- see ``capability_manifest.json``.

Design intent, straight from proposal 1abedabe-2f82-40e5-a320-3b32d550cc40
("do not depend on Serena memories; source-of-truth is tracked code plus
durable pointers"): a :class:`PatchManifest` IS the durable pointer. It
never embeds large binary payloads inline -- only a sha256 + byte size per
operation -- so a manifest stays small enough to read, diff, and (if a
caller chooses) commit to a repo as an audit trail of what was planned and,
once applied, what actually landed. The real bytes are supplied separately
at apply time (``transactional_merge.apply_patch_manifest``'s ``payloads``
argument) and verified against the recorded hash before use, so a manifest
can never be silently "applied" against payload bytes that were substituted
after the manifest was authored and (presumably) reviewed.
"""
from __future__ import annotations

import contextlib
import json
import os
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .safe_ooxml_writer import compute_sha256

PATCH_MANIFEST_SCHEMA_VERSION = 1

# "replace_part": overwrite (or create) one named part with new bytes.
# "insert_image": insert an image as an inline drawing run (see
#   safe_image_insert.compute_image_insert_parts) -- metadata carries
#   image_ext/anchor_text/paragraph_index/width_emu/height_emu/drawing_name;
#   the image bytes themselves travel as the operation's payload.
# "custom": anything a caller registers its own applier for in
#   transactional_merge.apply_patch_manifest(appliers=...) -- kept generic
#   here so this module never has to change when a new operation kind is
#   invented downstream (e.g. by output_provenance_gate/docx_completion_gate).
ALLOWED_KINDS = frozenset({"replace_part", "insert_image", "custom"})


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class PatchManifestError(Exception):
    """Raised for any manifest construction, mutation, or (de)serialization
    error."""


@dataclass
class PatchOperation:
    """One planned, individually-verifiable edit against a single OOXML part."""

    op_id: str
    kind: str
    target_part: str
    description: str
    payload_sha256: str | None = None
    payload_size: int | None = None
    created_at: str = field(default_factory=_utcnow_iso)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "op_id": self.op_id,
            "kind": self.kind,
            "target_part": self.target_part,
            "description": self.description,
            "payload_sha256": self.payload_sha256,
            "payload_size": self.payload_size,
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PatchOperation":
        try:
            return cls(
                op_id=data["op_id"],
                kind=data["kind"],
                target_part=data["target_part"],
                description=data.get("description", ""),
                payload_sha256=data.get("payload_sha256"),
                payload_size=data.get("payload_size"),
                created_at=data.get("created_at", _utcnow_iso()),
                metadata=dict(data.get("metadata") or {}),
            )
        except KeyError as exc:
            raise PatchManifestError(f"patch operation missing required field: {exc}") from exc


@dataclass
class PatchManifest:
    """A draft (or applied/aborted) set of :class:`PatchOperation` entries
    targeting one .docx file, plus the sha256 the target had when the
    manifest was authored (``base_sha256``) -- the staleness fingerprint
    ``transactional_merge.apply_patch_manifest`` checks before applying
    anything."""

    manifest_id: str
    target_docx_path: str
    base_sha256: str | None
    created_at: str
    schema_version: int = PATCH_MANIFEST_SCHEMA_VERSION
    operations: list[PatchOperation] = field(default_factory=list)
    status: str = "draft"
    applied_at: str | None = None
    aborted_reason: str | None = None
    notes: str = ""

    @classmethod
    def create(
        cls,
        target_docx_path: str | Path,
        *,
        base_sha256: str | None = None,
        notes: str = "",
    ) -> "PatchManifest":
        return cls(
            manifest_id=uuid.uuid4().hex,
            target_docx_path=str(target_docx_path),
            base_sha256=base_sha256,
            created_at=_utcnow_iso(),
            notes=notes,
        )

    @classmethod
    def create_from_file(cls, target_docx_path: str | Path, *, notes: str = "") -> "PatchManifest":
        """Same as :meth:`create`, but computes ``base_sha256`` from the
        CURRENT bytes on disk (``None`` if the target does not exist yet --
        e.g. a manifest whose first operation will create the file)."""
        path = Path(target_docx_path)
        base = compute_sha256(path.read_bytes()) if path.is_file() else None
        return cls.create(target_docx_path, base_sha256=base, notes=notes)

    def add_operation(
        self,
        kind: str,
        target_part: str,
        description: str,
        *,
        payload: bytes | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> PatchOperation:
        """Append a new operation to this manifest. Only legal on a
        ``status == "draft"`` manifest -- an applied or aborted manifest is
        a closed historical record, not something to keep editing."""
        if self.status != "draft":
            raise PatchManifestError(
                f"cannot add an operation to a manifest with status {self.status!r} "
                "(only 'draft' manifests are mutable)"
            )
        if kind not in ALLOWED_KINDS:
            raise PatchManifestError(f"unknown operation kind {kind!r}; allowed: {sorted(ALLOWED_KINDS)}")
        if not target_part:
            raise PatchManifestError("target_part is required")

        op = PatchOperation(
            op_id=uuid.uuid4().hex,
            kind=kind,
            target_part=target_part,
            description=description,
            payload_sha256=compute_sha256(payload) if payload is not None else None,
            payload_size=len(payload) if payload is not None else None,
            metadata=dict(metadata or {}),
        )
        self.operations.append(op)
        return op

    def mark_applied(self) -> None:
        self.status = "applied"
        self.applied_at = _utcnow_iso()

    def mark_aborted(self, reason: str) -> None:
        self.status = "aborted"
        self.aborted_reason = reason

    def verify_base_unchanged(
        self,
        current_path: str | Path | None = None,
        *,
        current_bytes: bytes | None = None,
    ) -> bool:
        """True when the recorded ``base_sha256`` matches the file's CURRENT
        hash. A manifest with no recorded ``base_sha256`` (``None``) has
        nothing to compare against and is trivially considered unchanged --
        that is a deliberate "unknown base" state, not an assertion that the
        file is empty or missing.
        """
        if self.base_sha256 is None:
            return True
        if current_bytes is None:
            path = Path(current_path if current_path is not None else self.target_docx_path)
            if not path.is_file():
                return False
            current_bytes = path.read_bytes()
        return compute_sha256(current_bytes) == self.base_sha256

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "manifest_id": self.manifest_id,
            "target_docx_path": self.target_docx_path,
            "base_sha256": self.base_sha256,
            "created_at": self.created_at,
            "status": self.status,
            "applied_at": self.applied_at,
            "aborted_reason": self.aborted_reason,
            "notes": self.notes,
            "operations": [op.to_dict() for op in self.operations],
        }

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PatchManifest":
        if not isinstance(data, dict):
            raise PatchManifestError("manifest data must be a dict")
        schema_version = data.get("schema_version")
        if schema_version != PATCH_MANIFEST_SCHEMA_VERSION:
            raise PatchManifestError(
                f"unsupported patch manifest schema_version {schema_version!r}; "
                f"expected {PATCH_MANIFEST_SCHEMA_VERSION}"
            )
        try:
            operations = [PatchOperation.from_dict(op) for op in data.get("operations", [])]
            return cls(
                manifest_id=data["manifest_id"],
                target_docx_path=data["target_docx_path"],
                base_sha256=data.get("base_sha256"),
                created_at=data["created_at"],
                schema_version=schema_version,
                operations=operations,
                status=data.get("status", "draft"),
                applied_at=data.get("applied_at"),
                aborted_reason=data.get("aborted_reason"),
                notes=data.get("notes", ""),
            )
        except KeyError as exc:
            raise PatchManifestError(f"manifest data missing required field: {exc}") from exc

    @classmethod
    def from_json(cls, text: str) -> "PatchManifest":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise PatchManifestError(f"invalid JSON: {exc}") from exc
        return cls.from_dict(data)

    def save(self, path: str | Path) -> None:
        """Atomically write this manifest as JSON to ``path`` (temp file in
        the same directory + fsync + ``os.replace``, mirroring
        ``safe_ooxml_writer.SafeOoxmlWriter``'s discipline for the same
        reason: a crash mid-write must never leave a truncated manifest)."""
        path = Path(path)
        parent = path.parent if str(path.parent) else Path(".")
        parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(parent), prefix=f".{path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(self.to_json())
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.remove(tmp_name)
            raise

    @classmethod
    def load(cls, path: str | Path) -> "PatchManifest":
        return cls.from_json(Path(path).read_text(encoding="utf-8"))
