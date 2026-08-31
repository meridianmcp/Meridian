"""Fail-closed provenance checks for equation review packets.

Equation review packets have three deliberately different document roles:

* ``baseline_role`` is the read-only source used for every Before panel;
* ``candidate_role`` is the exact draft being reviewed;
* ``decision_sources`` record the packets that supplied approved decisions.

This module is intentionally read-only.  It validates hashes and role
separation; it does not edit a DOCX, refresh fields, render pages, or infer
scientific meaning.  A packet with a missing file, a stale hash, or an
ambiguous baseline/candidate binding is invalid rather than merely warned.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


class EquationReviewProvenanceError(ValueError):
    """Raised only for malformed validator inputs, not for a bad manifest."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _binding_errors(binding: Any, label: str) -> list[str]:
    if not isinstance(binding, dict):
        return [f"{label} must be an object"]
    errors: list[str] = []
    path = binding.get("path")
    expected = binding.get("sha256")
    if not isinstance(path, str) or not path.strip():
        errors.append(f"{label}.path is missing")
    if not isinstance(expected, str) or len(expected) != 64:
        errors.append(f"{label}.sha256 is missing or malformed")
    return errors


def validate_equation_review_manifest(
    manifest_path: str | Path,
    *,
    require_artifact: bool = True,
) -> dict[str, Any]:
    """Validate a generated equation-review manifest without changing files.

    The return value is always JSON-safe.  ``status`` is ``"valid"`` only if
    every required role exists, all referenced files exist, all hashes match,
    the baseline and candidate are different files, and an artifact hash is
    present/matching when ``require_artifact`` is true.
    """
    manifest_file = Path(manifest_path)
    result: dict[str, Any] = {
        "manifest_path": str(manifest_file),
        "status": "invalid",
        "errors": [],
        "warnings": [],
        "verified": [],
    }
    if not manifest_file.is_file():
        result["errors"].append("manifest does not exist")
        return result
    try:
        payload = json.loads(manifest_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        result["errors"].append(f"manifest is unreadable: {exc}")
        return result
    if not isinstance(payload, dict):
        result["errors"].append("manifest root must be an object")
        return result
    if payload.get("status") not in {"valid", "valid_v60_replaces_invalid_v59"}:
        result["errors"].append("manifest status is not a valid review-packet status")

    baseline = payload.get("baseline_role")
    candidate = payload.get("candidate_role")
    result["errors"].extend(_binding_errors(baseline, "baseline_role"))
    result["errors"].extend(_binding_errors(candidate, "candidate_role"))
    if isinstance(baseline, dict) and isinstance(candidate, dict):
        baseline_path = str(baseline.get("path", ""))
        candidate_path = str(candidate.get("path", ""))
        if baseline_path and candidate_path and Path(baseline_path).resolve() == Path(candidate_path).resolve():
            result["errors"].append("baseline_role and candidate_role resolve to the same file")

    checked_paths: set[Path] = set()
    for label, binding in (("baseline_role", baseline), ("candidate_role", candidate)):
        if not isinstance(binding, dict) or not isinstance(binding.get("path"), str):
            continue
        path = Path(binding["path"])
        if not path.is_file():
            result["errors"].append(f"{label}.path does not exist: {path}")
            continue
        try:
            actual = _sha256(path)
        except OSError as exc:
            result["errors"].append(f"{label} cannot be hashed: {exc}")
            continue
        checked_paths.add(path.resolve())
        if actual.casefold() != str(binding.get("sha256", "")).casefold():
            result["errors"].append(f"{label}.sha256 does not match the file on disk")
        else:
            result["verified"].append(label)

    for index, binding in enumerate(payload.get("decision_sources", [])):
        label = f"decision_sources[{index}]"
        result["errors"].extend(_binding_errors(binding, label))
        if not isinstance(binding, dict) or not isinstance(binding.get("path"), str):
            continue
        path = Path(binding["path"])
        if not path.is_file():
            result["errors"].append(f"{label}.path does not exist: {path}")
            continue
        try:
            actual = _sha256(path)
        except OSError as exc:
            result["errors"].append(f"{label} cannot be hashed: {exc}")
            continue
        if actual.casefold() != str(binding.get("sha256", "")).casefold():
            result["errors"].append(f"{label}.sha256 does not match the file on disk")
        else:
            result["verified"].append(label)

    artifact_path = payload.get("artifact")
    artifact_hash = payload.get("artifact_sha256")
    if require_artifact:
        if not isinstance(artifact_path, str) or not artifact_path.strip():
            result["errors"].append("artifact path is missing")
        elif not isinstance(artifact_hash, str) or len(artifact_hash) != 64:
            result["errors"].append("artifact_sha256 is missing or malformed")
        else:
            path = Path(artifact_path)
            if not path.is_file():
                result["errors"].append(f"artifact does not exist: {path}")
            else:
                actual = _sha256(path)
                if actual.casefold() != artifact_hash.casefold():
                    result["errors"].append("artifact_sha256 does not match the artifact on disk")
                else:
                    result["verified"].append("artifact")
    else:
        result["warnings"].append("artifact existence/hash was not required")

    if not isinstance(payload.get("decision_ids"), list) or not payload.get("decision_ids"):
        result["errors"].append("decision_ids must identify the decisions carried into the packet")
    if not isinstance(payload.get("rules"), list) or not payload.get("rules"):
        result["errors"].append("rules must identify the baseline/current/proposed binding rules")
    result["status"] = "valid" if not result["errors"] else "invalid"
    return result

