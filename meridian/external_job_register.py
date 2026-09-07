"""Shared validation and local persistence for external-job continuity.

Meridian cannot truthfully own an arbitrary RunPod, SSH, Slurm, or CI job.
This module therefore keeps the shared record provider-neutral and treats the
host-local JSON snapshot as a companion cache, not as a second authority.
"""
from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from meridian.secret_redaction import check_for_secrets

EXTERNAL_JOB_STATUSES = frozenset(
    {"queued", "running", "blocked", "unknown", "succeeded", "failed", "canceled"}
)
EXTERNAL_JOB_TERMINAL_STATUSES = frozenset({"succeeded", "failed", "canceled"})
SNAPSHOT_SCHEMA_VERSION = 1
MAX_METADATA_BYTES = 50_000

_LOCAL_PATH_RE = re.compile(
    r"(?:^|[\s=(])(?:[A-Za-z]:[\\/]|\\\\|/(?!/))"
)
_SNAPSHOT_LOCK = threading.Lock()


def utcnow_iso() -> str:
    """Return a sortable UTC timestamp with microsecond precision."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def validate_external_status(value: object) -> str:
    """Normalize and validate the small, provider-neutral status vocabulary."""
    status = value.strip().lower() if isinstance(value, str) else ""
    if status not in EXTERNAL_JOB_STATUSES:
        raise ValueError(
            "external job status must be one of "
            f"{sorted(EXTERNAL_JOB_STATUSES)}, got {value!r}"
        )
    return status


def _validate_text(
    value: object,
    *,
    field: str,
    required: bool = False,
    max_chars: int = 4_000,
    reject_local_path: bool = False,
) -> str | None:
    if value is None:
        if required:
            raise ValueError(f"{field} is required")
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    text = value.strip()
    if required and not text:
        raise ValueError(f"{field} is required")
    if len(text) > max_chars:
        raise ValueError(f"{field} exceeds the {max_chars}-character limit")
    check_for_secrets(text, context=f"external job {field}")
    if reject_local_path and _LOCAL_PATH_RE.search(text):
        raise ValueError(
            f"Refusing to persist external job {field}: machine-local absolute paths "
            "must stay in the local status snapshot"
        )
    return text or None


def validate_metadata(value: object) -> dict[str, Any]:
    """Validate JSON metadata without allowing secrets or local paths."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("external job metadata must be an object")

    def visit(node: Any, path: str = "metadata") -> None:
        if isinstance(node, str):
            _validate_text(
                node,
                field=path,
                max_chars=8_000,
                reject_local_path=True,
            )
        elif isinstance(node, dict):
            for key, child in node.items():
                if not isinstance(key, str):
                    raise ValueError("external job metadata keys must be strings")
                visit(child, f"{path}.{key}")
        elif isinstance(node, (list, tuple)):
            for index, child in enumerate(node):
                visit(child, f"{path}[{index}]")
        elif node is not None and not isinstance(node, (bool, int, float)):
            raise ValueError(f"external job {path} contains a non-JSON value")

    visit(value)
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise ValueError("external job metadata must be JSON serializable") from exc
    if len(encoded.encode("utf-8")) > MAX_METADATA_BYTES:
        raise ValueError(f"external job metadata exceeds {MAX_METADATA_BYTES} bytes")
    return dict(value)


def validate_job_identity(
    *, job_key: object, provider: object, external_id: object
) -> tuple[str, str, str]:
    key = _validate_text(
        job_key, field="job_key", required=True, max_chars=200, reject_local_path=True
    )
    provider_name = _validate_text(
        provider, field="provider", required=True, max_chars=64, reject_local_path=True
    )
    external = _validate_text(
        external_id, field="external_id", required=True, max_chars=500,
        reject_local_path=True,
    )
    assert key is not None and provider_name is not None and external is not None
    return key, provider_name.lower(), external


def validate_job_fields(
    *,
    status: object,
    phase: object = None,
    check_hint: object = None,
    resume_hint: object = None,
    resource_hint: object = None,
    next_check_at: object = None,
    detail: object = None,
    metadata: object = None,
) -> dict[str, Any]:
    return {
        "status": validate_external_status(status),
        "phase": _validate_text(phase, field="phase", max_chars=500, reject_local_path=True),
        "check_hint": _validate_text(
            check_hint, field="check_hint", max_chars=2_000, reject_local_path=True
        ),
        "resume_hint": _validate_text(
            resume_hint, field="resume_hint", max_chars=2_000, reject_local_path=True
        ),
        "resource_hint": _validate_text(
            resource_hint, field="resource_hint", max_chars=1_000, reject_local_path=True
        ),
        "next_check_at": _validate_text(
            next_check_at, field="next_check_at", max_chars=80, reject_local_path=True
        ),
        "detail": _validate_text(detail, field="detail", max_chars=4_000, reject_local_path=True),
        "metadata": validate_metadata(metadata),
    }


def _safe_component(value: str) -> str:
    component = re.sub(r"[^A-Za-z0-9_.-]", "_", value.strip())
    return component[:160] or "project"


def external_job_snapshot_path(data_dir: str | os.PathLike[str], project_id: str) -> Path:
    """Return the local-only snapshot path for one project."""
    return Path(data_dir) / "external_jobs" / f"{_safe_component(project_id)}.json"


def write_local_status_snapshot(
    data_dir: str | os.PathLike[str], project_id: str, jobs: list[dict[str, Any]]
) -> dict[str, Any]:
    """Atomically write a crash-surviving local snapshot.

    The caller deliberately receives a structured failure instead of an
    exception: the database record is authoritative and must remain usable if
    the host disk is read-only or temporarily unavailable.
    """
    path = external_job_snapshot_path(data_dir, project_id)
    payload = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "project_id": project_id,
        "generated_at": utcnow_iso(),
        "jobs": jobs,
    }
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        with _SNAPSHOT_LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
            os.replace(temporary, path)
    except (OSError, TypeError, ValueError) as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        return {"ok": False, "path": str(path), "error": str(exc)}
    return {
        "ok": True,
        "path": str(path),
        "job_count": len(jobs),
        "generated_at": payload["generated_at"],
    }


def read_local_status_snapshot(
    data_dir: str | os.PathLike[str], project_id: str
) -> dict[str, Any] | None:
    """Read a local snapshot for diagnostics; malformed/missing means None."""
    path = external_job_snapshot_path(data_dir, project_id)
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(payload, dict) or payload.get("project_id") != project_id:
        return None
    return payload


def build_log_description(action: str, job: dict[str, Any]) -> str:
    """Render the bounded, secret-checked task-log representation."""
    state = {
        "job_key": job.get("job_key"),
        "provider": job.get("provider"),
        "external_id": job.get("external_id"),
        "status": job.get("status"),
        "phase": job.get("phase"),
        "last_observed_at": job.get("last_observed_at"),
        "next_check_at": job.get("next_check_at"),
        "check_hint": job.get("check_hint"),
        "resume_hint": job.get("resume_hint"),
        "resource_hint": job.get("resource_hint"),
        "detail": job.get("detail"),
        "metadata": job.get("metadata") or {},
    }
    encoded = json.dumps(state, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    description = f"External job {action}: {encoded}"
    check_for_secrets(description, context="external job task description")
    return description[:9_500]
