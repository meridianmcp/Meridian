"""Deterministic, read-only compile receipts for LaTeX snapshots.

The receipt deliberately records *what was compiled*, not just whether a PDF
exists.  It is an evidence boundary for later Docs/Outputs integration:
included TeX and bibliography files are resolved recursively, every input is
hashed, and a missing compiler or incomplete PDF evidence can never be called
``passed``.

This module does not invoke a compiler, render a document, write sidecars, or
mutate a source tree.  A caller that has already run a compiler supplies the
log, toolchain metadata, and PDF path to :func:`build_latex_compile_receipt`.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping


RECEIPT_VERSION = 1
VALID_STATUSES = frozenset({"passed", "partial", "not_supplied", "stale", "blocked", "degraded", "unavailable"})
VALID_COMPILER_STATUSES = frozenset({"available", "unavailable", "unknown"})
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_INPUT_RE = re.compile(r"\\(?:input|include)\s*\{([^}]*)\}")
_BIB_RE = re.compile(r"\\(?:bibliography|addbibresource)\s*\{([^}]*)\}")


class LatexReceiptError(ValueError):
    """Raised when a receipt would make an unverifiable claim."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalise_path(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path))).resolve()


def _portable_locator(locator: str) -> bool:
    if locator.startswith("<external>/"):
        return True
    path = Path(locator)
    return bool(locator) and "\\" not in locator and not path.is_absolute() and ".." not in path.parts


def _snapshot_path(snapshot_root: Path, locator: str) -> Path | None:
    if not _portable_locator(locator) or locator.startswith("<external>/"):
        return None
    root = snapshot_root.resolve()
    candidate = (root / Path(locator)).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


def _inside_snapshot(path: Path, snapshot_root: Path) -> bool:
    try:
        path.resolve().relative_to(snapshot_root.resolve())
    except ValueError:
        return False
    return True


def _looks_like_pdf(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(5) == b"%PDF-"
    except OSError:
        return False


def _locator(path: Path, snapshot_root: Path) -> str:
    """Return a portable path relative to the snapshot root when possible."""

    try:
        return path.relative_to(snapshot_root).as_posix()
    except ValueError:
        # Keep external inputs auditable without leaking a machine-local path.
        return f"<external>/{path.name}"


def _candidate_path(name: str, base_dir: Path, suffix: str) -> Path:
    candidate = Path(name.strip())
    if not str(candidate).lower().endswith(suffix):
        candidate = Path(str(candidate) + suffix)
    return _normalise_path(candidate if candidate.is_absolute() else base_dir / candidate)


def _iter_dependencies(root_tex: Path) -> tuple[list[Path], list[Path], list[dict[str, str]]]:
    """Resolve TeX and bibliography dependencies without executing TeX."""

    snapshot_root = root_tex.parent.resolve()
    inputs: list[Path] = []
    bibliography: list[Path] = []
    unresolved: list[dict[str, str]] = []
    seen: set[Path] = set()
    pending = [root_tex]

    while pending:
        current = pending.pop(0)
        current = _normalise_path(current)
        if current in seen or not current.is_file():
            continue
        seen.add(current)
        try:
            source = current.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        for match in _INPUT_RE.finditer(source):
            name = match.group(1).strip()
            if not name:
                continue
            path = _candidate_path(name, current.parent, ".tex")
            if not _inside_snapshot(path, snapshot_root):
                unresolved.append(
                    {"from": _locator(current, snapshot_root), "kind": "external", "name": Path(name).name}
                )
                continue
            if path.is_file() and path not in inputs and path != root_tex:
                inputs.append(path)
                pending.append(path)
            elif not path.is_file():
                unresolved.append({"from": _locator(current, snapshot_root), "kind": "input", "name": name})

        for match in _BIB_RE.finditer(source):
            for name in match.group(1).split(","):
                name = name.strip()
                if not name:
                    continue
                path = _candidate_path(name, current.parent, ".bib")
                if not _inside_snapshot(path, snapshot_root):
                    unresolved.append(
                        {"from": _locator(current, snapshot_root), "kind": "external", "name": Path(name).name}
                    )
                    continue
                if path.is_file() and path not in bibliography:
                    bibliography.append(path)
                elif not path.is_file():
                    unresolved.append({"from": _locator(current, snapshot_root), "kind": "bibliography", "name": name})

    return (
        sorted(inputs, key=os.fspath),
        sorted(bibliography, key=os.fspath),
        sorted(unresolved, key=lambda item: (item["from"], item["kind"], item["name"])),
    )


def _file_record(path: Path, snapshot_root: Path) -> dict[str, Any]:
    return {
        "locator": _locator(path, snapshot_root),
        "sha256": _sha256_file(path),
        "size_bytes": path.stat().st_size,
    }


def _validate_hash(value: str | None, field_name: str) -> None:
    if value is not None and not _SHA256_RE.fullmatch(value):
        raise LatexReceiptError(f"{field_name} must be a lowercase SHA-256 digest")


def _tuple_records(value: Iterable[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    records = []
    for record in value:
        if not isinstance(record, Mapping):
            raise LatexReceiptError("file records must be objects")
        locator = record.get("locator")
        digest = record.get("sha256")
        if not isinstance(locator, str) or not locator:
            raise LatexReceiptError("file record locator must be a non-empty string")
        if not _portable_locator(locator):
            raise LatexReceiptError("file record locator must be portable and confined")
        if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
            raise LatexReceiptError("file record sha256 must be a lowercase SHA-256 digest")
        normalized = {"locator": locator, "sha256": digest}
        if "size_bytes" in record:
            size = record["size_bytes"]
            if not isinstance(size, int) or size < 0:
                raise LatexReceiptError("file record size_bytes must be a non-negative integer")
            normalized["size_bytes"] = size
        records.append(normalized)
    return tuple(sorted(records, key=lambda item: item["locator"]))


@dataclass(frozen=True)
class LatexCompileReceipt:
    """Immutable receipt with deterministic JSON/XML serialization."""

    status: str
    root_locator: str
    root_sha256: str
    input_files: tuple[dict[str, Any], ...] = ()
    bibliography_files: tuple[dict[str, Any], ...] = ()
    unresolved_dependencies: tuple[dict[str, str], ...] = ()
    engine: str | None = None
    compiler_status: str = "unknown"
    toolchain_versions: Mapping[str, str] = field(default_factory=dict)
    command: str | None = None
    compiler_log: str | None = None
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    pdf_locator: str | None = None
    pdf_sha256: str | None = None
    pdf_size_bytes: int | None = None
    page_count: int | None = None
    profile: Mapping[str, Any] | None = None
    equation_manifest_sha256: str | None = None
    reference_manifest_sha256: str | None = None
    unknown_fields: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in VALID_STATUSES:
            raise LatexReceiptError(f"unsupported receipt status: {self.status!r}")
        if not isinstance(self.root_locator, str) or not self.root_locator:
            raise LatexReceiptError("root_locator must be a non-empty string")
        _validate_hash(self.root_sha256, "root_sha256")
        if self.compiler_status not in VALID_COMPILER_STATUSES:
            raise LatexReceiptError(f"unsupported compiler status: {self.compiler_status!r}")
        if self.status == "passed":
            if self.compiler_status != "available":
                raise LatexReceiptError("an unavailable or unknown compiler cannot produce a passed receipt")
            if (
                self.errors
                or not self.pdf_locator
                or not self.pdf_sha256
                or self.pdf_size_bytes is None
                or self.page_count is None
                or not self.engine
                or not self.command
                or self.compiler_log is None
                or not self.toolchain_versions
                or self.unresolved_dependencies
            ):
                raise LatexReceiptError(
                    "a passed receipt requires complete compiler, dependency, PDF, and log evidence"
                )
        if not _portable_locator(self.root_locator):
            raise LatexReceiptError("root_locator must be portable and confined")
        if self.pdf_locator is not None and not _portable_locator(self.pdf_locator):
            raise LatexReceiptError("pdf_locator must be portable and confined")
        if self.pdf_size_bytes is not None and (not isinstance(self.pdf_size_bytes, int) or self.pdf_size_bytes < 0):
            raise LatexReceiptError("pdf_size_bytes must be a non-negative integer")
        if self.page_count is not None and (not isinstance(self.page_count, int) or self.page_count < 1):
            raise LatexReceiptError("page_count must be a positive integer when supplied")
        _validate_hash(self.pdf_sha256, "pdf_sha256")
        _validate_hash(self.equation_manifest_sha256, "equation_manifest_sha256")
        _validate_hash(self.reference_manifest_sha256, "reference_manifest_sha256")
        object.__setattr__(self, "input_files", _tuple_records(self.input_files))
        object.__setattr__(self, "bibliography_files", _tuple_records(self.bibliography_files))
        unresolved = []
        for item in self.unresolved_dependencies:
            if not isinstance(item, Mapping) or not all(
                isinstance(item.get(key), str) and item.get(key) for key in ("from", "kind", "name")
            ):
                raise LatexReceiptError("unresolved dependency records require from, kind, and name")
            unresolved.append({key: item[key] for key in ("from", "kind", "name")})
        object.__setattr__(
            self,
            "unresolved_dependencies",
            tuple(sorted(unresolved, key=lambda item: (item["from"], item["kind"], item["name"]))),
        )
        object.__setattr__(self, "toolchain_versions", dict(sorted(self.toolchain_versions.items())))
        object.__setattr__(self, "warnings", tuple(str(item) for item in self.warnings))
        object.__setattr__(self, "errors", tuple(str(item) for item in self.errors))
        object.__setattr__(self, "unknown_fields", dict(self.unknown_fields))

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "receipt_version": RECEIPT_VERSION,
            "status": self.status,
            "root_locator": self.root_locator,
            "root_sha256": self.root_sha256,
            "input_files": list(self.input_files),
            "bibliography_files": list(self.bibliography_files),
            "unresolved_dependencies": list(self.unresolved_dependencies),
            "engine": self.engine,
            "compiler_status": self.compiler_status,
            "toolchain_versions": dict(self.toolchain_versions),
            "command": self.command,
            "compiler_log": self.compiler_log,
            "warnings": list(self.warnings),
            "errors": list(self.errors),
            "pdf_locator": self.pdf_locator,
            "pdf_sha256": self.pdf_sha256,
            "pdf_size_bytes": self.pdf_size_bytes,
            "page_count": self.page_count,
            "profile": self.profile,
            "equation_manifest_sha256": self.equation_manifest_sha256,
            "reference_manifest_sha256": self.reference_manifest_sha256,
        }
        result.update(self.unknown_fields)
        return result

    def canonical_json(self) -> str:
        return _canonical_json(self.to_dict())

    def digest(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    def to_xml(self) -> str:
        root = ET.Element("latex_compile_receipt", {"receipt_version": str(RECEIPT_VERSION), "status": self.status})
        payload = ET.SubElement(root, "payload", {"encoding": "canonical-json"})
        payload.text = self.canonical_json()
        return ET.tostring(root, encoding="unicode", short_empty_elements=True)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "LatexCompileReceipt":
        if not isinstance(value, Mapping):
            raise LatexReceiptError("receipt must be an object")
        version = value.get("receipt_version", RECEIPT_VERSION)
        if version != RECEIPT_VERSION:
            raise LatexReceiptError(f"unsupported receipt_version: {version!r}")
        known = {
            "receipt_version",
            "status",
            "root_locator",
            "root_sha256",
            "input_files",
            "bibliography_files",
            "unresolved_dependencies",
            "engine",
            "compiler_status",
            "toolchain_versions",
            "command",
            "compiler_log",
            "warnings",
            "errors",
            "pdf_locator",
            "pdf_sha256",
            "pdf_size_bytes",
            "page_count",
            "profile",
            "equation_manifest_sha256",
            "reference_manifest_sha256",
        }
        kwargs = {key: value.get(key) for key in known if key != "receipt_version" and key in value}
        kwargs.setdefault("input_files", ())
        kwargs.setdefault("bibliography_files", ())
        kwargs.setdefault("unresolved_dependencies", ())
        kwargs.setdefault("toolchain_versions", {})
        kwargs.setdefault("warnings", ())
        kwargs.setdefault("errors", ())
        kwargs["unknown_fields"] = {key: item for key, item in value.items() if key not in known}
        return cls(**kwargs)

    @classmethod
    def from_xml(cls, value: str) -> "LatexCompileReceipt":
        try:
            root = ET.fromstring(value)
            if root.tag != "latex_compile_receipt":
                raise LatexReceiptError("invalid receipt XML root")
            payload = root.find("payload")
            if payload is None or payload.text is None:
                raise LatexReceiptError("receipt XML has no canonical JSON payload")
            if payload.get("encoding") != "canonical-json":
                raise LatexReceiptError("receipt XML payload encoding is not canonical-json")
            data = json.loads(payload.text)
        except (ET.ParseError, json.JSONDecodeError) as exc:
            raise LatexReceiptError(f"invalid receipt XML: {exc}") from exc
        if root.get("receipt_version") != str(RECEIPT_VERSION) or root.get("status") != data.get("status"):
            raise LatexReceiptError("receipt XML metadata does not match payload")
        return cls.from_dict(data)

    def verify_current_files(self, snapshot_root: str | os.PathLike[str]) -> dict[str, Any]:
        """Compare recorded relative input hashes with the current snapshot."""

        root = _normalise_path(Path(snapshot_root))
        records = [{"locator": self.root_locator, "sha256": self.root_sha256}]
        records.extend(self.input_files)
        records.extend(self.bibliography_files)
        reasons: list[str] = []
        for record in records:
            locator = str(record["locator"])
            if locator.startswith("<external>/"):
                reasons.append(f"unverifiable external input: {locator}")
                continue
            path = _snapshot_path(root, locator)
            if path is None:
                reasons.append(f"unverifiable locator: {locator}")
                continue
            if not path.is_file():
                reasons.append(f"missing input: {locator}")
                continue
            actual = _sha256_file(path)
            if actual != record["sha256"]:
                reasons.append(f"hash mismatch: {locator}")
        reasons.extend(
            f"unresolved dependency: {item['kind']} {item['name']} from {item['from']}"
            for item in self.unresolved_dependencies
        )
        if self.pdf_locator is not None:
            if self.pdf_locator.startswith("<external>/"):
                reasons.append(f"unverifiable PDF: {self.pdf_locator}")
            else:
                pdf_path = _snapshot_path(root, self.pdf_locator)
                if pdf_path is None or not pdf_path.is_file():
                    reasons.append(f"missing PDF: {self.pdf_locator}")
                else:
                    if not _looks_like_pdf(pdf_path):
                        reasons.append(f"invalid PDF signature: {self.pdf_locator}")
                    if self.pdf_sha256 and _sha256_file(pdf_path) != self.pdf_sha256:
                        reasons.append(f"PDF hash mismatch: {self.pdf_locator}")
                    if self.pdf_size_bytes is not None and pdf_path.stat().st_size != self.pdf_size_bytes:
                        reasons.append(f"PDF size mismatch: {self.pdf_locator}")
        elif self.status == "passed":
            reasons.append("passed receipt has no PDF locator")
        return {"stale": bool(reasons), "reasons": reasons}


def build_latex_compile_receipt(
    root_tex: str | os.PathLike[str],
    *,
    status: str = "unavailable",
    compiler_status: str = "unknown",
    engine: str | None = None,
    toolchain_versions: Mapping[str, str] | None = None,
    command: str | None = None,
    compiler_log: str | None = None,
    warnings: Iterable[str] = (),
    errors: Iterable[str] = (),
    pdf_path: str | os.PathLike[str] | None = None,
    page_count: int | None = None,
    profile: Mapping[str, Any] | None = None,
    equation_manifest_sha256: str | None = None,
    reference_manifest_sha256: str | None = None,
) -> LatexCompileReceipt:
    """Build a receipt from an existing local snapshot without compiling it."""

    root = _normalise_path(Path(root_tex))
    if not root.is_file():
        raise LatexReceiptError(f"root TeX file does not exist: {root}")
    snapshot_root = root.parent
    inputs, bibliography, unresolved = _iter_dependencies(root)
    pdf = _normalise_path(Path(pdf_path)) if pdf_path is not None else None
    if pdf is not None:
        if pdf.parent != snapshot_root and _snapshot_path(snapshot_root, _locator(pdf, snapshot_root)) is None:
            raise LatexReceiptError("pdf_path must be inside the TeX snapshot")
        if pdf.is_file() and not _looks_like_pdf(pdf):
            raise LatexReceiptError("pdf_path does not contain a PDF")
    pdf_exists = pdf is not None and pdf.is_file()
    pdf_hash = _sha256_file(pdf) if pdf_exists and pdf is not None else None
    pdf_size = pdf.stat().st_size if pdf_exists and pdf is not None else None
    if status == "passed" and compiler_status != "available":
        raise LatexReceiptError("passed requires an available compiler")
    if status == "passed" and (
        not pdf_exists
        or page_count is None
        or not engine
        or not command
        or compiler_log is None
        or not toolchain_versions
    ):
        raise LatexReceiptError("passed requires complete compiler, PDF, and log evidence")
    if status == "passed" and unresolved:
        raise LatexReceiptError("passed requires every TeX and bibliography dependency to resolve")
    return LatexCompileReceipt(
        status=status,
        root_locator=_locator(root, snapshot_root),
        root_sha256=_sha256_file(root),
        input_files=tuple(_file_record(path, snapshot_root) for path in inputs),
        bibliography_files=tuple(_file_record(path, snapshot_root) for path in bibliography),
        unresolved_dependencies=tuple(unresolved),
        engine=engine,
        compiler_status=compiler_status,
        toolchain_versions=toolchain_versions or {},
        command=command,
        compiler_log=compiler_log,
        warnings=tuple(warnings),
        errors=tuple(errors),
        pdf_locator=_locator(pdf, snapshot_root) if pdf is not None else None,
        pdf_sha256=pdf_hash,
        pdf_size_bytes=pdf_size,
        page_count=page_count,
        profile=profile,
        equation_manifest_sha256=equation_manifest_sha256,
        reference_manifest_sha256=reference_manifest_sha256,
    )


__all__ = [
    "LatexCompileReceipt",
    "LatexReceiptError",
    "RECEIPT_VERSION",
    "VALID_STATUSES",
    "build_latex_compile_receipt",
]
