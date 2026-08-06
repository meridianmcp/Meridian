"""safe_ooxml_writer.py -- atomic, validated, backed-up writes for OOXML zip
containers (.docx/.dotx and, incidentally, any other Open Packaging
Conventions zip -- .pptx/.xlsx share the same container format, though this
module and package are scoped to .docx per proposal
1abedabe-2f82-40e5-a320-3b32d550cc40).

Part of ``tools/meridian_fallbacks`` -- see ``capability_manifest.json`` in
this package for the package-level contract (version, source hashes,
supported operations, limitations, related proposals). This module has ZERO
dependency on the Meridian server, its DB, or any MCP tool: it is meant to
keep working when none of those are reachable ("fallback" tools, see
AGENTS.md's capability-manifest / fallback-chain section, 649e095f). It
also has no third-party dependency (stdlib ``zipfile`` +
``xml.etree.ElementTree`` only) -- ``python-docx`` is not a dependency of
this repo's pixi environment and this package must not require adding one.

Design summary
---------------
Every write goes through the same three-phase discipline:

1. **Build in memory.** Assemble the complete new zip (all parts, changed
   and unchanged) as bytes via :func:`build_zip_bytes`, never as a partial
   in-place zip mutation -- ``zipfile`` has no safe in-place edit mode;
   re-opening an existing archive in append mode can silently duplicate or
   shadow entries instead of replacing them.
2. **Validate before committing.** Round-trip the just-built bytes back
   through :func:`verify_zip_bytes` (real zipfile CRC check, required-part
   presence check, well-formed-XML check on ``word/document.xml``) BEFORE
   any disk write touches the real target path.
3. **Atomic replace, with a backup.** Write the validated bytes to a temp
   file in the SAME directory as the target (so ``os.replace`` is atomic on
   the same filesystem/volume, including Windows), flush + ``fsync`` it,
   snapshot the previous target to a timestamped backup, then
   ``os.replace`` the temp file over the target. A failure at any step
   before the final ``os.replace`` leaves the original target byte-for-byte
   untouched -- ``write_parts`` either fully succeeds or raises without
   having modified ``target_path`` at all.

Nothing here mutates ``word/document.xml`` by fully re-parsing and
re-serializing the whole document tree with ``xml.etree.ElementTree`` --
that round trip is what forces the sophisticated namespace-preservation
machinery in ``extensions/meridian-docs/meridian_docs/docs_intel.py``
(``_save_docx_xml_stdlib`` et al -- see that module for exactly why: ET
silently drops unreferenced root-level ``xmlns:*`` declarations and can
renumber namespace prefixes on reserialization). Callers that need to touch
``document.xml`` content (see ``safe_image_insert.py`` in this same
package) instead splice new bytes into the EXISTING part bytes at a located
byte offset, leaving every other byte -- and every namespace declaration
this module never even looks at -- untouched. Documented as a limitation in
``capability_manifest.json``: this buys safety on the ordinary "add a run
here" edits this fallback package targets, at the cost of NOT being a
general document.xml editor.
"""
from __future__ import annotations

import hashlib
import io
import os
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

# Minimal set of parts a well-formed .docx package must contain. Callers
# performing narrower operations (e.g. tests exercising just the zip-level
# machinery) may pass a smaller/empty ``required_parts`` tuple explicitly --
# the default is deliberately the real-world minimum, not merely "some XML
# in a zip".
REQUIRED_PARTS: tuple[str, ...] = (
    "[Content_Types].xml",
    "_rels/.rels",
    "word/document.xml",
)

_XML_PART_TO_VALIDATE = "word/document.xml"


class DocxWriteError(Exception):
    """Base class for every error this module raises."""


class DocxValidationError(DocxWriteError):
    """Raised when a just-built (not-yet-committed) zip fails validation.

    Carries the full :class:`DocxValidationReport` on ``.report`` so a
    caller (e.g. ``transactional_merge.py``) can inspect exactly what
    failed without re-parsing anything.
    """

    def __init__(self, message: str, report: "DocxValidationReport") -> None:
        super().__init__(message)
        self.report = report


def compute_sha256(data: bytes) -> str:
    """Return the hex sha256 digest of ``data``."""
    return hashlib.sha256(data).hexdigest()


@dataclass
class DocxValidationReport:
    """Result of validating a candidate (or committed) .docx zip's bytes."""

    valid: bool
    errors: list[str]
    parts: list[str]
    byte_size: int
    sha256: str

    def to_dict(self) -> dict:
        return {
            "valid": self.valid,
            "errors": list(self.errors),
            "parts": list(self.parts),
            "byte_size": self.byte_size,
            "sha256": self.sha256,
        }


def verify_zip_bytes(
    data: bytes,
    *,
    required_parts: tuple[str, ...] = REQUIRED_PARTS,
) -> DocxValidationReport:
    """Validate ``data`` as an OOXML (.docx) zip container.

    Checks performed, all recorded as human-readable strings in
    ``report.errors`` (never raises -- callers decide whether a failed
    report is fatal):

    1. The bytes open as a real zip archive at all.
    2. ``zipfile.testzip()`` reports no CRC-corrupt entry.
    3. Every name in ``required_parts`` is present in the archive.
    4. If ``word/document.xml`` is present, it parses as well-formed XML
       (``xml.etree.ElementTree.fromstring``) -- this is a well-formedness
       check only, NOT full OOXML schema validation.
    """
    errors: list[str] = []
    parts: list[str] = []

    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        errors.append(f"not a valid zip archive: {exc}")
        return DocxValidationReport(
            valid=False,
            errors=errors,
            parts=parts,
            byte_size=len(data),
            sha256=compute_sha256(data),
        )

    with zf:
        parts = sorted(zf.namelist())

        bad_entry = zf.testzip()
        if bad_entry is not None:
            errors.append(f"corrupt zip entry (CRC mismatch): {bad_entry}")

        for required in required_parts:
            if required not in zf.namelist():
                errors.append(f"missing required part: {required}")

        if _XML_PART_TO_VALIDATE in zf.namelist():
            try:
                ET.fromstring(zf.read(_XML_PART_TO_VALIDATE))
            except ET.ParseError as exc:
                errors.append(f"{_XML_PART_TO_VALIDATE} is not well-formed XML: {exc}")

    return DocxValidationReport(
        valid=not errors,
        errors=errors,
        parts=parts,
        byte_size=len(data),
        sha256=compute_sha256(data),
    )


def read_parts_from_bytes(data: bytes) -> dict[str, bytes]:
    """Return ``{part_name: raw_bytes}`` for every entry in the zip ``data``.

    Raises :class:`DocxWriteError` if ``data`` is not a readable zip.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            return {name: zf.read(name) for name in zf.namelist()}
    except zipfile.BadZipFile as exc:
        raise DocxWriteError(f"not a valid zip archive: {exc}") from exc


def build_zip_bytes(
    parts: dict[str, bytes],
    *,
    compression: int = zipfile.ZIP_DEFLATED,
) -> bytes:
    """Serialize ``parts`` (``{part_name: raw_bytes}``) into a fresh in-memory
    zip archive and return its bytes. Parts are written in sorted-name order
    for reproducibility. Pure function -- never touches disk.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression) as zf:
        for name in sorted(parts.keys()):
            zf.writestr(name, parts[name])
    return buf.getvalue()


@dataclass
class WriteResult:
    """Outcome of a committed (non-dry-run) :meth:`SafeOoxmlWriter.write_parts`
    or :meth:`SafeOoxmlWriter.restore_backup` call."""

    path: str
    backup_path: str | None
    validation: DocxValidationReport
    byte_size: int
    sha256: str

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "backup_path": self.backup_path,
            "validation": self.validation.to_dict(),
            "byte_size": self.byte_size,
            "sha256": self.sha256,
        }


class SafeOoxmlWriter:
    """Validate-then-atomically-replace writer for a single .docx target path.

    Every write is: build the full candidate zip in memory -> validate it ->
    back up whatever currently exists at ``target_path`` -> atomically
    replace ``target_path`` with the validated bytes. Nothing partial is
    ever left on disk: either the whole operation lands, or ``target_path``
    is exactly as it was before the call.
    """

    def __init__(self, target_path: str | Path, *, backup_dir: str | Path | None = None) -> None:
        self.target_path = Path(target_path)
        self.backup_dir = Path(backup_dir) if backup_dir is not None else self.target_path.parent

    def exists(self) -> bool:
        return self.target_path.is_file()

    def read_parts(self) -> dict[str, bytes]:
        """Read every part of the CURRENT on-disk target into memory.

        Raises :class:`DocxWriteError` if the target does not exist or is
        not a readable zip.
        """
        if not self.exists():
            raise DocxWriteError(f"target docx does not exist: {self.target_path}")
        data = self.target_path.read_bytes()
        return read_parts_from_bytes(data)

    def write_parts(
        self,
        parts: dict[str, bytes],
        *,
        validate: bool = True,
        required_parts: tuple[str, ...] = REQUIRED_PARTS,
    ) -> WriteResult:
        """Build, validate, back up, and atomically commit ``parts`` as the
        new content of ``target_path``.

        Raises :class:`DocxValidationError` (with ``.report`` populated) if
        ``validate`` is True and the candidate bytes fail validation -- in
        that case ``target_path`` is left completely untouched (the
        candidate is never written to disk, let alone committed).
        """
        candidate = build_zip_bytes(parts)
        report = verify_zip_bytes(candidate, required_parts=required_parts)
        if validate and not report.valid:
            raise DocxValidationError(
                f"refusing to write {self.target_path}: "
                f"{'; '.join(report.errors) or 'unknown validation failure'}",
                report,
            )

        backup_path = self._backup_existing()
        self._atomic_replace(candidate)

        return WriteResult(
            path=str(self.target_path),
            backup_path=str(backup_path) if backup_path else None,
            validation=report,
            byte_size=len(candidate),
            sha256=report.sha256,
        )

    def restore_backup(self, backup_path: str | Path) -> WriteResult:
        """Atomically restore ``backup_path`` back over ``target_path``.

        The CURRENT ``target_path`` content is itself backed up first (same
        as any other write), so a restore is itself reversible -- restoring
        never permanently discards whatever was in place beforehand.
        """
        backup_path = Path(backup_path)
        if not backup_path.is_file():
            raise DocxWriteError(f"backup does not exist: {backup_path}")
        data = backup_path.read_bytes()
        report = verify_zip_bytes(data)
        pre_restore_backup = self._backup_existing()
        self._atomic_replace(data)
        return WriteResult(
            path=str(self.target_path),
            backup_path=str(pre_restore_backup) if pre_restore_backup else None,
            validation=report,
            byte_size=len(data),
            sha256=report.sha256,
        )

    def _backup_existing(self) -> Path | None:
        """Snapshot the CURRENT on-disk target to a timestamped backup file
        alongside ``backup_dir``. Returns ``None`` (no-op) when the target
        does not yet exist -- there is nothing to back up on a first write.
        """
        if not self.exists():
            return None
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup_path = self.backup_dir / f"{self.target_path.stem}.bak-{stamp}{self.target_path.suffix}"
        shutil.copy2(self.target_path, backup_path)
        return backup_path

    def _atomic_replace(self, data: bytes) -> None:
        """Write ``data`` to a temp file in ``target_path``'s own directory,
        fsync it, then ``os.replace`` it over ``target_path``. ``os.replace``
        is atomic on the same filesystem/volume on every platform this repo
        targets, including Windows (unlike ``os.rename``, which fails on
        Windows when the destination already exists).
        """
        self.target_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self.target_path.parent),
            prefix=f".{self.target_path.name}.",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, self.target_path)
        except BaseException:
            try:
                os.remove(tmp_name)
            except OSError:
                pass
            raise
