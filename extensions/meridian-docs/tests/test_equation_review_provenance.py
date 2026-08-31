from __future__ import annotations

import hashlib
import json
from pathlib import Path

from meridian_docs.equation_review_provenance import validate_equation_review_manifest


def _write(path: Path, content: bytes) -> str:
    path.write_bytes(content)
    return hashlib.sha256(content).hexdigest()


def _manifest(tmp_path: Path) -> Path:
    baseline = tmp_path / "baseline.docx"
    candidate = tmp_path / "candidate.docx"
    decision = tmp_path / "decision.docx"
    artifact = tmp_path / "proposal.docx"
    payload = {
        "artifact": str(artifact),
        "artifact_sha256": _write(artifact, b"proposal"),
        "status": "valid_v60_replaces_invalid_v59",
        "baseline_role": {"path": str(baseline), "sha256": _write(baseline, b"baseline")},
        "candidate_role": {"path": str(candidate), "sha256": _write(candidate, b"candidate")},
        "decision_sources": [{"path": str(decision), "sha256": _write(decision, b"decision")}],
        "decision_ids": ["EQ-021-R-Depth-restore"],
        "rules": ["Before is canonical, never a prior proposal."],
    }
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    return manifest


def test_validates_distinct_hashed_roles(tmp_path: Path) -> None:
    result = validate_equation_review_manifest(_manifest(tmp_path))
    assert result["status"] == "valid"
    assert not result["errors"]


def test_rejects_stale_candidate_hash(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    Path(payload["candidate_role"]["path"]).write_bytes(b"changed")
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    result = validate_equation_review_manifest(manifest)
    assert result["status"] == "invalid"
    assert any("candidate_role.sha256" in error for error in result["errors"])


def test_rejects_same_baseline_and_candidate_path(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["candidate_role"] = payload["baseline_role"]
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    result = validate_equation_review_manifest(manifest)
    assert result["status"] == "invalid"
    assert any("same file" in error for error in result["errors"])

