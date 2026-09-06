"""Hash-bound, read-only evidence envelope for document analysis.

This module is intentionally dependency-light.  It accepts the existing Docs,
workspace, Outputs, and render result mappings instead of importing those
systems at runtime, which keeps the bridge optional and avoids a mandatory
Docs-to-Outputs dependency cycle.  The source content hash is the join key;
registry identity, legacy status, and evidence status remain separate facts.
"""

from __future__ import annotations

import hashlib
import json
import os
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping


ANALYSIS_RECEIPT_VERSION = 1
ANALYSIS_STATUSES = frozenset({"pass", "partial", "not_supplied", "stale", "blocked", "degraded", "unavailable"})
RELATION_TYPES = frozenset(
    {"derived_from", "contains", "references", "supersedes", "validated_by", "rendered_from", "evidence_for"}
)
_RENDERED_STATUS = "rendered"


class AnalysisReceiptError(ValueError):
    """Raised when an evidence envelope would make an unsafe claim."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _copy_json(value: Any, field_name: str) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise AnalysisReceiptError(f"{field_name} must be JSON-compatible") from exc


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _normalize_source_hash(value: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
        raise AnalysisReceiptError("source_sha256 must be a lowercase SHA-256 digest")
    return value


def _normalize_status(value: str, field_name: str = "status") -> str:
    if value not in ANALYSIS_STATUSES:
        raise AnalysisReceiptError(f"unsupported {field_name}: {value!r}")
    return value


def _component_status(value: Mapping[str, Any] | None, source_sha256: str) -> dict[str, Any]:
    """Normalize an existing result while preserving missing/partial evidence."""

    if value is None:
        return {"status": "not_supplied", "data": None, "reasons": ["component not supplied"]}
    if not isinstance(value, Mapping):
        return {"status": "blocked", "data": None, "reasons": ["component is not an object"]}
    copied = dict(value)
    explicit_status = copied.get("status")
    if explicit_status == _RENDERED_STATUS:
        explicit_status = "pass"
    if explicit_status is None:
        if copied.get("valid") is True or copied.get("ok") is True:
            explicit_status = "pass"
        elif copied.get("valid") is False or copied.get("ok") is False:
            explicit_status = "blocked"
        else:
            explicit_status = "partial"
    try:
        status = _normalize_status(str(explicit_status))
    except AnalysisReceiptError:
        status = "blocked"
    reasons = [str(item) for item in copied.get("reasons", [])] if isinstance(copied.get("reasons", []), list) else []
    observed_hash = next(
        (
            copied.get(key)
            for key in (
                "source_sha256",
                "source_fingerprint",
                "source_docx_sha256",
                "document_fingerprint",
                "input_sha256",
            )
            if copied.get(key)
        ),
        None,
    )
    if observed_hash is not None and observed_hash != source_sha256:
        status = "stale"
        reasons.append("component source hash does not match receipt source hash")
    elif observed_hash is None and status == "pass":
        status = "partial"
        reasons.append("component has no source hash binding")
    copied["status"] = status
    if reasons:
        copied["reasons"] = list(dict.fromkeys(reasons))
    return copied


def _normalize_relations(value: Iterable[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    relations: list[dict[str, Any]] = []
    for relation in value:
        if not isinstance(relation, Mapping):
            raise AnalysisReceiptError("relations must contain objects")
        relation_type = relation.get("type")
        if relation_type not in RELATION_TYPES:
            raise AnalysisReceiptError(f"unsupported relation type: {relation_type!r}")
        if not isinstance(relation.get("from"), str) or not isinstance(relation.get("to"), str):
            raise AnalysisReceiptError("relations require string from and to identifiers")
        relations.append({key: relation[key] for key in sorted(relation)})
    return tuple(sorted(relations, key=_canonical_json))


@dataclass(frozen=True)
class AnalysisReceipt:
    """Immutable aggregate of separately labeled document evidence."""

    source_locator: str
    source_sha256: str
    status: str
    components: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    artifacts: tuple[dict[str, Any], ...] = ()
    relations: tuple[dict[str, Any], ...] = ()
    profile: Mapping[str, Any] | None = None
    unknown_fields: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.source_locator, str) or not self.source_locator:
            raise AnalysisReceiptError("source_locator must be a non-empty portable locator")
        if "\\" in self.source_locator or os.path.isabs(self.source_locator) or ".." in Path(self.source_locator).parts:
            raise AnalysisReceiptError("source_locator must be portable and non-absolute")
        _normalize_source_hash(self.source_sha256)
        _normalize_status(self.status)
        normalized_components: dict[str, Mapping[str, Any]] = {}
        for key, value in (self.components or {}).items():
            if not isinstance(key, str) or not key:
                raise AnalysisReceiptError("component names must be non-empty strings")
            if not isinstance(value, Mapping):
                raise AnalysisReceiptError("component values must be objects")
            normalized_components[key] = _component_status(value, self.source_sha256)
        object.__setattr__(self, "components", dict(sorted(normalized_components.items())))
        object.__setattr__(self, "relations", _normalize_relations(self.relations))
        known_fields = {
            "receipt_version",
            "source_locator",
            "source_sha256",
            "status",
            "components",
            "artifacts",
            "relations",
            "profile",
        }
        unknown_fields = self.unknown_fields or {}
        if set(unknown_fields).intersection(known_fields):
            raise AnalysisReceiptError("unknown_fields cannot overwrite canonical fields")
        object.__setattr__(self, "artifacts", tuple(_copy_json(item, "artifacts") for item in self.artifacts))
        object.__setattr__(self, "profile", _copy_json(self.profile, "profile") if self.profile is not None else None)
        object.__setattr__(self, "unknown_fields", _copy_json(unknown_fields, "unknown_fields"))

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "receipt_version": ANALYSIS_RECEIPT_VERSION,
            "source_locator": self.source_locator,
            "source_sha256": self.source_sha256,
            "status": self.status,
            "components": _copy_json(self.components, "components"),
            "artifacts": list(self.artifacts),
            "relations": list(self.relations),
            "profile": self.profile,
        }
        result.update(self.unknown_fields)
        return result

    def canonical_json(self) -> str:
        return _canonical_json(self.to_dict())

    def digest(self) -> str:
        return _sha256(self.canonical_json().encode("utf-8"))

    def to_xml(self) -> str:
        root = ET.Element(
            "analysis_receipt",
            {"receipt_version": str(ANALYSIS_RECEIPT_VERSION), "status": self.status},
        )
        payload = ET.SubElement(root, "payload", {"encoding": "canonical-json"})
        payload.text = self.canonical_json()
        return ET.tostring(root, encoding="unicode", short_empty_elements=True)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "AnalysisReceipt":
        if not isinstance(value, Mapping):
            raise AnalysisReceiptError("analysis receipt must be an object")
        if value.get("receipt_version", ANALYSIS_RECEIPT_VERSION) != ANALYSIS_RECEIPT_VERSION:
            raise AnalysisReceiptError("unsupported analysis receipt version")
        known = {
            "receipt_version",
            "source_locator",
            "source_sha256",
            "status",
            "components",
            "artifacts",
            "relations",
            "profile",
        }
        kwargs = {key: value[key] for key in known if key != "receipt_version" and key in value}
        kwargs.setdefault("components", {})
        kwargs.setdefault("artifacts", ())
        kwargs.setdefault("relations", ())
        kwargs["unknown_fields"] = {key: item for key, item in value.items() if key not in known}
        return cls(**kwargs)

    @classmethod
    def from_xml(cls, value: str) -> "AnalysisReceipt":
        try:
            root = ET.fromstring(value)
            if root.tag != "analysis_receipt":
                raise AnalysisReceiptError("invalid analysis receipt XML root")
            payload = root.find("payload")
            if payload is None or payload.text is None:
                raise AnalysisReceiptError("analysis receipt XML has no JSON payload")
            if payload.get("encoding") != "canonical-json":
                raise AnalysisReceiptError("analysis receipt XML payload encoding is not canonical-json")
            data = json.loads(payload.text)
        except (ET.ParseError, json.JSONDecodeError) as exc:
            raise AnalysisReceiptError(f"invalid analysis receipt XML: {exc}") from exc
        if root.get("receipt_version") != str(ANALYSIS_RECEIPT_VERSION) or root.get("status") != data.get("status"):
            raise AnalysisReceiptError("analysis receipt XML metadata does not match payload")
        return cls.from_dict(data)

    def verify_source(self, source: bytes | bytearray | str | os.PathLike[str]) -> dict[str, Any]:
        """Verify current bytes and return stale/valid evidence without writing."""

        if isinstance(source, (str, os.PathLike)) and Path(source).is_file():
            current = Path(source).read_bytes()
        elif isinstance(source, str):
            current = source.encode("utf-8")
        else:
            current = bytes(source)
        actual = _sha256(current)
        return {"valid": actual == self.source_sha256, "stale": actual != self.source_sha256, "actual_sha256": actual}


def build_analysis_receipt(
    source: bytes | bytearray | str | os.PathLike[str],
    *,
    source_locator: str | None = None,
    equation_graph: Mapping[str, Any] | None = None,
    notation_audit: Mapping[str, Any] | None = None,
    integrity: Mapping[str, Any] | None = None,
    workspace_lineage: Mapping[str, Any] | None = None,
    outputs_evidence: Mapping[str, Any] | None = None,
    render_receipt: Mapping[str, Any] | None = None,
    artifacts: Iterable[Mapping[str, Any]] = (),
    relations: Iterable[Mapping[str, Any]] = (),
    profile: Mapping[str, Any] | None = None,
) -> AnalysisReceipt:
    """Compose existing result mappings into a hash-bound receipt."""

    if isinstance(source, (str, os.PathLike)) and Path(source).is_file():
        path = Path(source)
        raw = path.read_bytes()
        locator = source_locator or path.name
    elif isinstance(source, str):
        if Path(source).suffix.casefold() in {".docx", ".tex", ".pdf"}:
            raise AnalysisReceiptError("source path does not exist; pass raw bytes for content")
        raw = source.encode("utf-8")
        locator = source_locator or "<raw>"
    else:
        raw = bytes(source)
        locator = source_locator or "<bytes>"
    if "\\" in locator or os.path.isabs(locator) or ".." in Path(locator).parts:
        locator = Path(locator).name
    source_sha256 = _sha256(raw)
    components = {
        "equation_graph": _component_status(equation_graph, source_sha256),
        "notation_audit": _component_status(notation_audit, source_sha256),
        "integrity": _component_status(integrity, source_sha256),
        "workspace_lineage": _component_status(workspace_lineage, source_sha256),
        "outputs_evidence": _component_status(outputs_evidence, source_sha256),
        "render_receipt": _component_status(render_receipt, source_sha256),
    }
    supplied = [value["status"] for value in components.values() if value["status"] != "not_supplied"]
    if not supplied:
        status = "not_supplied"
    elif any(value in {"blocked", "stale"} for value in supplied):
        status = "blocked" if "blocked" in supplied else "stale"
    elif any(value in {"partial", "degraded", "unavailable"} for value in supplied):
        status = "partial"
    else:
        status = "pass"
    return AnalysisReceipt(
        source_locator=locator,
        source_sha256=source_sha256,
        status=status,
        components=components,
        artifacts=tuple(dict(item) for item in artifacts),
        relations=tuple(dict(item) for item in relations),
        profile=profile,
    )


def build_docx_analysis_receipt(
    document_path: str | os.PathLike[str],
    *,
    notation_manifest: Mapping[str, Any] | None = None,
    workspace_lineage: Mapping[str, Any] | None = None,
    outputs_evidence: Mapping[str, Any] | None = None,
    render_receipt: Mapping[str, Any] | None = None,
    profile: Mapping[str, Any] | None = None,
) -> AnalysisReceipt:
    """Run the existing read-only DOCX analyzers and bind their evidence.

    This is the concrete Docs-side adapter.  It imports only sibling readers,
    never the Outputs registry, and does not render, persist, or modify the
    DOCX.  Outputs/workspace/render evidence remains caller-supplied so the
    join stays explicit and staged.
    """

    path = Path(document_path)
    if not path.is_file():
        raise AnalysisReceiptError(f"document path does not exist: {path}")
    from . import docs_intel, equation_graph, nomenclature, ooxml_integrity, notation_audit

    raw = path.read_bytes()
    source_sha256 = _sha256(raw)
    manifest = dict(notation_manifest) if notation_manifest is not None else None
    graph = equation_graph.build_equation_graph(str(path), manifest)
    notation = notation_audit.audit_equation_notation(str(path), manifest) if manifest is not None else None
    package = ooxml_integrity.validate_docx_package(str(path))
    structural = docs_intel.audit_equation_integrity(str(path))
    nomenclature_result = nomenclature.lint_nomenclature(str(path), manifest) if manifest is not None else None

    graph = dict(graph)
    graph.setdefault("source_fingerprint", source_sha256)
    graph["status"] = "blocked" if graph.get("error_type") else "pass"
    package = dict(package)
    package["source_fingerprint"] = source_sha256
    package["status"] = "pass" if package.get("ok") is True else "blocked"
    structural = dict(structural)
    structural["source_fingerprint"] = source_sha256
    structural["status"] = "pass" if structural.get("finding_count", 0) == 0 else "blocked"
    if notation is not None:
        notation = dict(notation)
        notation["source_fingerprint"] = source_sha256
        notation["status"] = "pass" if notation.get("valid") is True else "blocked"
    if nomenclature_result is not None:
        nomenclature_result = dict(nomenclature_result)
        nomenclature_result["source_fingerprint"] = source_sha256
        nomenclature_result["status"] = "pass" if nomenclature_result.get("valid") is True else "blocked"

    receipt = build_analysis_receipt(
        raw,
        source_locator=path.name,
        equation_graph=graph,
        notation_audit=notation,
        integrity={
            "package": package,
            "structural": structural,
            "source_fingerprint": source_sha256,
            "status": "pass" if package["status"] == "pass" and structural["status"] == "pass" else "blocked",
        },
        workspace_lineage=workspace_lineage,
        outputs_evidence=outputs_evidence,
        render_receipt=render_receipt,
        profile=profile,
    )
    components = dict(receipt.components)
    if nomenclature_result is not None:
        components["nomenclature_audit"] = _component_status(nomenclature_result, source_sha256)
    return AnalysisReceipt(
        source_locator=receipt.source_locator,
        source_sha256=receipt.source_sha256,
        status=receipt.status,
        components=components,
        artifacts=receipt.artifacts,
        relations=receipt.relations,
        profile=receipt.profile,
        unknown_fields=receipt.unknown_fields,
    )


def assert_source_hash(receipt: AnalysisReceipt, expected_source_sha256: str) -> None:
    """Reject a receipt whose content join key is not the expected hash."""

    if receipt.source_sha256 != _normalize_source_hash(expected_source_sha256):
        raise AnalysisReceiptError("source hash mismatch")


def project_registry_evidence(
    registry_record: Mapping[str, Any] | None,
    *,
    legacy_status: Mapping[str, Any] | None = None,
    source_sha256: str | None = None,
) -> dict[str, Any]:
    """Project registry facts without collapsing legacy status into success."""

    if registry_record is None:
        return {"status": "not_supplied", "registry_identity": None, "source_edges": [], "legacy_status": legacy_status}
    if not isinstance(registry_record, Mapping):
        return {"status": "blocked", "registry_identity": None, "source_edges": [], "legacy_status": legacy_status}
    record = dict(registry_record)
    identity = record.get("artifact_id", record.get("id"))
    edges = record.get("source_edges", record.get("source_edge", []))
    if isinstance(edges, Mapping):
        edges = [dict(edges)]
    elif not isinstance(edges, list):
        edges = []
    status = str(record.get("status", "partial"))
    if status not in ANALYSIS_STATUSES:
        status = "partial"
    observed = record.get("source_sha256") or record.get("source_fingerprint")
    if source_sha256 and observed and observed != source_sha256:
        status = "stale"
    elif status == "pass" and (identity is None or legacy_status is None or not observed):
        # A missing legacy/index fact is not evidence that the registry is bad,
        # but it is not a complete hash-bound cross-system success either.
        status = "partial"
    return {
        "status": status,
        "registry_identity": identity,
        "source_edges": [dict(edge) if isinstance(edge, Mapping) else edge for edge in edges],
        "legacy_status": dict(legacy_status) if isinstance(legacy_status, Mapping) else legacy_status,
        "registry_record": record,
    }


def bind_render_receipt(
    render_receipt: Mapping[str, Any] | None,
    analysis_receipt: AnalysisReceipt,
    *,
    equation_manifest_sha256: str | None = None,
    notation_manifest_sha256: str | None = None,
) -> dict[str, Any]:
    """Return a copied render receipt with explicit analysis freshness facts."""

    bound = dict(render_receipt or {})
    observed = bound.get("source_sha256") or bound.get("source_docx_sha256") or bound.get("source_fingerprint")
    status = str(bound.get("status", "not_supplied")) if bound else "not_supplied"
    if status == _RENDERED_STATUS:
        status = "pass"
    if status not in ANALYSIS_STATUSES:
        status = "partial"
    reasons: list[str] = []
    if observed and observed != analysis_receipt.source_sha256:
        status = "stale"
        reasons.append("render receipt source hash does not match analysis source hash")
    elif not observed and status == "pass":
        status = "partial"
        reasons.append("render receipt has no source hash binding")
    binding = {
        "source_sha256": analysis_receipt.source_sha256,
        "analysis_receipt_sha256": analysis_receipt.digest(),
        "equation_manifest_sha256": equation_manifest_sha256,
        "notation_manifest_sha256": notation_manifest_sha256,
        "status": "pass" if not reasons else "stale",
    }
    bound["analysis_binding"] = binding
    bound["status"] = status
    if reasons:
        bound["reasons"] = reasons
    return bound


def evaluate_analysis_gate(
    receipt: AnalysisReceipt,
    *,
    required_components: Iterable[str] = ("equation_graph", "notation_audit", "integrity"),
    allow_degraded: bool = False,
) -> dict[str, Any]:
    """Evaluate a read-only promotion gate over separately labeled evidence."""

    required = tuple(dict.fromkeys(str(item) for item in required_components))
    reasons: list[str] = []
    statuses: dict[str, str] = {}
    for name in required:
        component = receipt.components.get(name)
        status = str(component.get("status", "not_supplied")) if component else "not_supplied"
        statuses[name] = status
        if status not in ANALYSIS_STATUSES:
            reasons.append(f"required component {name} has unknown status: {status}")
        elif status == "not_supplied":
            reasons.append(f"required component not supplied: {name}")
        elif status in {"stale", "blocked"}:
            reasons.append(f"required component {name} is {status}")
        elif status in {"partial", "degraded", "unavailable"} and not allow_degraded:
            reasons.append(f"required component {name} is {status}")
    allowed = not reasons and receipt.status == "pass"
    return {
        "allowed": allowed,
        "status": "pass" if allowed else "blocked",
        "receipt_status": receipt.status,
        "required_components": list(required),
        "component_statuses": statuses,
        "reasons": reasons,
    }


__all__ = [
    "ANALYSIS_RECEIPT_VERSION",
    "ANALYSIS_STATUSES",
    "RELATION_TYPES",
    "AnalysisReceipt",
    "AnalysisReceiptError",
    "assert_source_hash",
    "bind_render_receipt",
    "build_analysis_receipt",
    "build_docx_analysis_receipt",
    "evaluate_analysis_gate",
    "project_registry_evidence",
]
