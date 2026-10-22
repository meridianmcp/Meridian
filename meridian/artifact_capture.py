"""Opt-in local capture of provider-created output files.

Provider hook payloads are treated as untrusted hints. Only configured output
roots, selected file types, and bounded regular files are accepted. Captured
bytes are stored in Meridian's project-scoped content-addressed store; a local
occurrence manifest keeps the event/run lineage and is never sent to Meridian.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from . import artifact_store
from .local_runner import default_state_dir
from .secret_redaction import check_for_secrets


SCHEMA_VERSION = 1
MAX_EVENT_BYTES = 1024 * 1024
DEFAULT_MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_FILE_BYTES = 100 * 1024 * 1024
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
DEFAULT_EXTENSIONS = (
    ".bib", ".csv", ".docx", ".html", ".jpeg", ".jpg", ".json", ".md",
    ".pdf", ".png", ".pptx", ".svg", ".tex", ".txt", ".webp", ".xlsx", ".zip",
)
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_PROJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
_HASH_RE = re.compile(r"^(?:sha256:)?[0-9a-f]{64}$")
_EXCLUDED_DIRS = {
    ".git", ".hg", ".svn", ".claude", ".codex", ".meridian", ".pixi",
    ".venv", "venv", "node_modules", "__pycache__", "site-packages", "dist-cache",
}
_SENSITIVE_NAME = re.compile(r"(?:^|[._-])(secret|credential|token|password|private[-_]?key)(?:[._-]|$)", re.I)
_CAPTURE_TOOL_NAMES = {"write", "edit", "multiedit", "notebookedit", "apply_patch"}


class ArtifactCaptureError(ValueError):
    """A selected capture, manifest, or restore operation failed closed."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _data_dir(data_dir: Path | str | None = None) -> Path:
    if data_dir is not None:
        return Path(data_dir).expanduser().resolve()
    override = os.environ.get("MERIDIAN_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return default_state_dir().expanduser().resolve()


def _project_id(value: Any) -> str:
    if not isinstance(value, str) or not _PROJECT_RE.fullmatch(value):
        raise ArtifactCaptureError("project id must be a safe, non-secret path component")
    return value


def _local_id(value: Any) -> str | None:
    return value if isinstance(value, str) and _ID_RE.fullmatch(value) else None


def _resolve_dir(value: Path | str, field: str) -> Path:
    path = Path(value).expanduser()
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ArtifactCaptureError(f"{field} must name an existing local directory") from exc
    if not resolved.is_dir():
        raise ArtifactCaptureError(f"{field} must name an existing local directory")
    return resolved


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _config_path(data_dir: Path) -> Path:
    return data_dir / "artifact_capture" / "config.json"


def _manifest_path(data_dir: Path, project_id: str) -> Path:
    return data_dir / "artifact_capture" / _project_id(project_id) / "manifest.json"


@contextmanager
def _exclusive_lock(path: Path, timeout: float = 3.0) -> Iterator[None]:
    """Short cross-process lock for local JSON manifest/config updates."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle = path.open("a+b")
    if os.name == "nt":
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
    started = time.monotonic()
    acquired = False
    try:
        while not acquired:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except (OSError, BlockingIOError):
                if time.monotonic() - started >= timeout:
                    raise ArtifactCaptureError("timed out waiting for the local artifact manifest lock")
                time.sleep(0.05)
        yield
    finally:
        if acquired:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _atomic_json(path: Path, payload: dict[str, Any], *, limit: int) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    if len(encoded) > limit:
        raise ArtifactCaptureError(f"local artifact metadata exceeds {limit} bytes")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        path.parent.chmod(0o700)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("wb", dir=path.parent, prefix=".capture-", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(encoded)
            handle.write(b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise ArtifactCaptureError("could not atomically update local artifact metadata") from exc


def _read_json(path: Path, *, limit: int) -> dict[str, Any] | None:
    try:
        if path.stat().st_size > limit:
            raise ArtifactCaptureError("local artifact metadata exceeds its size limit")
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ArtifactCaptureError("local artifact metadata is unreadable or malformed") from exc
    if not isinstance(parsed, dict):
        raise ArtifactCaptureError("local artifact metadata must be a JSON object")
    return parsed


def _load_config(data_dir: Path) -> dict[str, Any]:
    payload = _read_json(_config_path(data_dir), limit=MAX_MANIFEST_BYTES)
    if payload is None:
        return {"schema_version": SCHEMA_VERSION, "projects": {}}
    if payload.get("schema_version") != SCHEMA_VERSION or not isinstance(payload.get("projects"), dict):
        raise ArtifactCaptureError("unsupported local artifact capture config schema")
    return payload


def configure_project(
    data_dir: Path | str | None,
    *,
    project_id: str,
    project_root: Path | str,
    output_roots: list[Path | str],
    subproject: str | None = None,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    extensions: list[str] | None = None,
    outputs_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Explicitly opt one local project into capture for selected output roots."""
    data = _data_dir(data_dir)
    project_id = _project_id(project_id)
    root = _resolve_dir(project_root, "project root")
    if not output_roots:
        raise ArtifactCaptureError("at least one selected output root is required to enable capture")
    normalized_roots = []
    for raw_root in output_roots:
        output_root = _resolve_dir(raw_root, "output root")
        if not _is_within(output_root, root):
            raise ArtifactCaptureError("each output root must be inside the selected project root")
        if str(output_root) not in normalized_roots:
            normalized_roots.append(str(output_root))
    if not 1 <= max_file_bytes <= MAX_FILE_BYTES:
        raise ArtifactCaptureError(f"max_file_bytes must be between 1 and {MAX_FILE_BYTES}")
    requested_extensions = extensions or DEFAULT_EXTENSIONS
    if any(not isinstance(value, str) for value in requested_extensions):
        raise ArtifactCaptureError("extensions must be simple filename extensions such as .pdf or .docx")
    allowed = sorted({(value if value.startswith(".") else f".{value}").lower() for value in requested_extensions})
    if not allowed or any(not re.fullmatch(r"\.[a-z0-9]{1,12}", suffix) for suffix in allowed):
        raise ArtifactCaptureError("extensions must be simple filename extensions such as .pdf or .docx")
    outputs_path = _resolve_dir(outputs_dir, "Meridian Outputs directory") if outputs_dir else None
    if subproject is not None and not isinstance(subproject, str):
        raise ArtifactCaptureError("subproject must be a short text label")
    subproject_value = subproject.strip()[:200] if subproject else None
    if subproject_value:
        check_for_secrets(subproject_value, "artifact capture subproject")
    entry = {
        "enabled": True,
        "project_id": project_id,
        "project_root": str(root),
        "output_roots": normalized_roots,
        "subproject": subproject_value,
        "max_file_bytes": max_file_bytes,
        "extensions": allowed,
        "outputs_dir": str(outputs_path) if outputs_path else None,
        "configured_at": _utc_now(),
    }
    config_path = _config_path(data)
    with _exclusive_lock(config_path.with_suffix(".lock")):
        payload = _load_config(data)
        payload["projects"][project_id] = entry
        _atomic_json(config_path, payload, limit=MAX_MANIFEST_BYTES)
    return {key: value for key, value in entry.items() if key not in {"project_root", "output_roots", "outputs_dir"}} | {
        "project_root": str(root), "output_roots": normalized_roots, "outputs_dir": str(outputs_path) if outputs_path else None,
    }


def disable_project(data_dir: Path | str | None, project_id: str) -> bool:
    data = _data_dir(data_dir)
    project_id = _project_id(project_id)
    path = _config_path(data)
    with _exclusive_lock(path.with_suffix(".lock")):
        payload = _load_config(data)
        entry = payload["projects"].get(project_id)
        if not isinstance(entry, dict):
            return False
        entry["enabled"] = False
        entry["disabled_at"] = _utc_now()
        _atomic_json(path, payload, limit=MAX_MANIFEST_BYTES)
    return True


def capture_status(data_dir: Path | str | None, project_id: str) -> dict[str, Any]:
    data = _data_dir(data_dir)
    payload = _load_config(data)
    project_id = _project_id(project_id)
    entry = payload["projects"].get(project_id)
    if not isinstance(entry, dict):
        return {
            "project_id": project_id,
            "enabled": False,
            "configured": False,
            "manifest_status": "not_configured",
            "manifest_sha256": None,
            "manifest_occurrence_count": 0,
            "manifest_file_present": False,
        }
    manifest_path = _manifest_path(data, project_id)
    try:
        manifest = _manifest(data, project_id)
        if manifest_path.exists():
            manifest_bytes = manifest_path.read_bytes()
            file_present = True
        else:
            manifest_bytes = json.dumps(
                manifest, sort_keys=True, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
            file_present = False
        manifest_status = "available" if entry.get("enabled") is True else "not_configured"
        manifest_hash = _sha256(manifest_bytes)
        occurrence_count = sum(
            1
            for record in manifest.get("occurrences", {}).values()
            if isinstance(record, dict) and record.get("retention_state", "active") == "active"
        )
    except (ArtifactCaptureError, OSError, ValueError, TypeError):
        manifest_status = "degraded"
        manifest_hash = None
        occurrence_count = None
        file_present = manifest_path.exists()
    return {
        "project_id": project_id,
        "configured": True,
        **entry,
        "manifest_status": manifest_status,
        "manifest_sha256": manifest_hash,
        "manifest_occurrence_count": occurrence_count,
        "manifest_file_present": file_present,
    }


def _project_for_cwd(data_dir: Path, cwd: Any) -> dict[str, Any] | None:
    if not isinstance(cwd, str) or not cwd:
        return None
    try:
        current = Path(cwd).expanduser().resolve(strict=True)
    except OSError:
        return None
    payload = _load_config(data_dir)
    candidates = []
    for entry in payload["projects"].values():
        if not isinstance(entry, dict) or entry.get("enabled") is not True:
            continue
        try:
            root = Path(entry["project_root"]).resolve(strict=True)
        except (KeyError, OSError, TypeError):
            continue
        if _is_within(current, root):
            candidates.append((len(root.parts), entry))
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def _allowed_root(entry: dict[str, Any], candidate: Path) -> tuple[int, Path] | None:
    roots = entry.get("output_roots")
    if not isinstance(roots, list):
        return None
    found = []
    for index, raw_root in enumerate(roots):
        try:
            root = Path(raw_root).resolve(strict=True)
        except (OSError, TypeError):
            continue
        if _is_within(candidate, root):
            found.append((len(root.parts), index, root))
    if not found:
        return None
    _depth, index, root = max(found, key=lambda item: item[0])
    return index, root


def _excluded_path(path: Path, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return True
    if not relative.parts:
        return True
    if any(part.lower() in _EXCLUDED_DIRS for part in relative.parts[:-1]):
        return True
    name = relative.name
    if name.lower() in {".env", "credentials", "credentials.json", "secrets.json", "id_rsa", "id_ed25519"}:
        return True
    return bool(_SENSITIVE_NAME.search(name) or name.lower().endswith((".pem", ".key", ".p12", ".pfx")))


def _git_ignored(project_root: Path, path: Path) -> bool:
    try:
        relative = path.relative_to(project_root)
    except ValueError:
        return True
    try:
        result = subprocess.run(
            ["git", "-C", str(project_root), "check-ignore", "--quiet", "--", str(relative)],
            capture_output=True, timeout=2, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _stat_identity(info: os.stat_result) -> tuple[int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _read_stable_file(path: Path, max_bytes: int) -> tuple[bytes, os.stat_result]:
    try:
        before = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise ArtifactCaptureError("selected output file is unavailable") from exc
    if not stat.S_ISREG(before.st_mode):
        raise ArtifactCaptureError("selected output must be a regular non-symlink file")
    if before.st_size < 0 or before.st_size > max_bytes:
        raise ArtifactCaptureError(f"selected output exceeds the {max_bytes}-byte file limit")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        handle = os.fdopen(descriptor, "rb")
        descriptor = None  # fdopen owns the descriptor from here on.
        with handle:
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode) or _stat_identity(opened) != _stat_identity(before):
                raise ArtifactCaptureError("selected output changed while opening")
            raw = handle.read(max_bytes + 1)
            after_open = os.fstat(handle.fileno())
        after = path.stat(follow_symlinks=False)
    except ArtifactCaptureError:
        raise
    except OSError as exc:
        raise ArtifactCaptureError("could not read the selected output file") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if len(raw) > max_bytes or _stat_identity(after_open) != _stat_identity(before) or _stat_identity(after) != _stat_identity(before):
        raise ArtifactCaptureError("selected output changed or exceeded its limit while being read")
    return raw, before


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _manifest(data_dir: Path, project_id: str) -> dict[str, Any]:
    path = _manifest_path(data_dir, project_id)
    payload = _read_json(path, limit=MAX_MANIFEST_BYTES)
    if payload is None:
        return {"schema_version": SCHEMA_VERSION, "project_id": project_id, "occurrences": {}}
    if (
        payload.get("schema_version") != SCHEMA_VERSION
        or payload.get("project_id") != project_id
        or not isinstance(payload.get("occurrences"), dict)
    ):
        raise ArtifactCaptureError("local artifact occurrence manifest has an unsupported schema or project id")
    return payload


def _write_manifest(data_dir: Path, project_id: str, payload: dict[str, Any]) -> None:
    _atomic_json(_manifest_path(data_dir, project_id), payload, limit=MAX_MANIFEST_BYTES)


def _try_register_outputs(entry: dict[str, Any], record: dict[str, Any], source_path: Path) -> dict[str, Any]:
    outputs_dir = entry.get("outputs_dir")
    if not outputs_dir:
        return {"status": "not_configured"}
    try:
        from meridian_outputs.artifact_registry import register_artifact, verify_artifact_hash
    except ImportError:
        return {"status": "unavailable", "reason": "Meridian Outputs registry package is not installed locally"}
    try:
        registered = register_artifact(
            str(outputs_dir), "agent_output", canonical_path=str(source_path),
            expected_sha256=record["source_sha256"], generator=f"{record['provider']}:PostToolUse",
            run_id=record.get("run_id"), source_locator=record["source_relative_path"],
            role="canonical", metadata={
                "capture_occurrence_id": record["occurrence_id"],
                "project_id": record["project_id"],
                "subproject": record.get("subproject"),
                "stored_content_hash": record["content_hash"],
            },
        )
        checked = verify_artifact_hash(str(outputs_dir), registered["artifact_id"], str(source_path))
        return {"status": "verified" if checked.get("verified") else "hash_mismatch", "artifact_id": registered.get("artifact_id"), "verified": bool(checked.get("verified"))}
    except Exception as exc:  # the capture remains useful even if the optional registry is unavailable
        return {"status": "failed", "error_type": type(exc).__name__}


def capture_file(
    data_dir: Path | str | None,
    project_id: str,
    source_path: Path | str,
    *,
    source_event: dict[str, Any] | None = None,
    trigger: str = "manual_user_capture",
) -> dict[str, Any]:
    """Capture one explicitly permitted output and append its local occurrence record."""
    data = _data_dir(data_dir)
    project_id = _project_id(project_id)
    if not isinstance(trigger, str) or trigger not in {"manual_user_capture", "opt_in_post_tool_use"}:
        raise ArtifactCaptureError("capture trigger is not supported")
    config = _load_config(data)
    entry = config["projects"].get(project_id)
    if not isinstance(entry, dict) or entry.get("enabled") is not True:
        raise ArtifactCaptureError("artifact capture is disabled for this project; configure selected roots first")
    try:
        project_root = Path(entry["project_root"]).resolve(strict=True)
        candidate = Path(source_path).expanduser().resolve(strict=True)
    except (KeyError, OSError, TypeError) as exc:
        raise ArtifactCaptureError("selected output path or project root is unavailable") from exc
    selected_root = _allowed_root(entry, candidate)
    if selected_root is None:
        raise ArtifactCaptureError("selected output is outside every configured output root")
    output_root_index, output_root = selected_root
    if _excluded_path(candidate, output_root):
        raise ArtifactCaptureError("selected output is excluded by the local capture policy")
    extension = candidate.suffix.lower()
    if extension not in entry.get("extensions", DEFAULT_EXTENSIONS):
        raise ArtifactCaptureError(f"file type {extension or '(no extension)'} is not enabled for local capture")
    max_bytes = int(entry.get("max_file_bytes", DEFAULT_MAX_FILE_BYTES))
    if not 1 <= max_bytes <= MAX_FILE_BYTES:
        raise ArtifactCaptureError("stored capture size limit is invalid")
    if _git_ignored(project_root, candidate):
        raise ArtifactCaptureError("git-ignored output files are excluded from automatic capture")
    raw, info = _read_stable_file(candidate, max_bytes)
    source_event = source_event if isinstance(source_event, dict) else {}
    provider = source_event.get("provider", "manual")
    if provider not in {"claude_code", "codex_cli", "manual"}:
        provider = "manual"
    relative_project_path = candidate.relative_to(project_root).as_posix()
    relative_output_path = candidate.relative_to(output_root).as_posix()
    event_id = _local_id(source_event.get("event_id")) or uuid.uuid4().hex
    tool_name = _local_id(source_event.get("tool_name"))
    session_ref = _local_id(source_event.get("session_ref"))
    run_id = _local_id(source_event.get("run_id"))
    source_digest = _sha256(raw)
    subproject = source_event.get("subproject", entry.get("subproject"))
    if subproject is not None:
        if not isinstance(subproject, str):
            raise ArtifactCaptureError("capture subproject must be text")
        subproject = subproject.strip()[:200] or None
        if subproject:
            check_for_secrets(subproject, "artifact capture subproject")
    occurrence_material = "\0".join((project_id, event_id, relative_project_path, source_digest))
    occurrence_id = hashlib.sha256(occurrence_material.encode("utf-8")).hexdigest()[:32]
    data_dir_path = str(data)

    manifest_file = _manifest_path(data, project_id)
    with _exclusive_lock(manifest_file.with_suffix(".lock")):
        manifest = _manifest(data, project_id)
        existing = manifest["occurrences"].get(occurrence_id)
        if isinstance(existing, dict):
            return {"captured": True, "deduplicated_event": True, "occurrence": existing}
        mime_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        stored_meta = artifact_store.store_artifact(data_dir_path, project_id, raw, content_type=mime_type)
        content_hash_value = str(stored_meta.get("content_hash", ""))
        stored = artifact_store.get_artifact(data_dir_path, project_id, content_hash_value)
        if stored is None or artifact_store.content_hash(stored) != content_hash_value:
            raise ArtifactCaptureError("stored artifact failed its read-back hash verification")
        record: dict[str, Any] = {
            "occurrence_id": occurrence_id,
            "project_id": project_id,
            "subproject": subproject,
            "run_id": run_id,
            "provider": provider,
            "surface": source_event.get("surface") if isinstance(source_event.get("surface"), str) and source_event.get("surface") in {"cli", "desktop", "manual"} else ("cli" if provider != "manual" else "manual"),
            "provider_version": _local_id(source_event.get("provider_version")),
            "event_name": _local_id(source_event.get("event_name")),
            "event_id": event_id,
            "tool_name": tool_name,
            "tool_use_id": _local_id(source_event.get("tool_use_id")),
            "local_session_ref": session_ref,
            "source_relative_path": relative_project_path,
            "output_root_index": output_root_index,
            "output_relative_path": relative_output_path,
            "source_size_bytes": len(raw),
            "source_mtime_ns": info.st_mtime_ns,
            "source_sha256": source_digest,
            "content_type": mime_type,
            "content_hash": content_hash_value,
            "content_size_bytes": len(stored),
            "redacted": bool(stored_meta.get("redacted", False)),
            "captured_at": _utc_now(),
            "trigger": trigger,
            "consent": "local_project_capture_opt_in",
            "retention_state": "active",
            "capture_verification": {
                "verified": True,
                "stored_sha256": content_hash_value,
                "stored_size_bytes": len(stored),
            },
            "outputs_registry": {"status": "pending"},
        }
        registry_status = _try_register_outputs(entry, record, candidate)
        record["outputs_registry"] = registry_status
        manifest["occurrences"][occurrence_id] = record
        _write_manifest(data, project_id, manifest)
    return {"captured": True, "deduplicated_event": False, "occurrence": record}


def _event_file_paths(event: dict[str, Any]) -> list[str]:
    tool_input = event.get("tool_input")
    if not isinstance(tool_input, dict):
        return []
    paths: list[str] = []
    for key in ("file_path", "path", "filename", "notebook_path"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            paths.append(value.strip())
    if not paths and str(event.get("tool_name", "")).lower() == "apply_patch":
        patch_text = tool_input.get("command")
        if isinstance(patch_text, str):
            current: str | None = None
            for line in patch_text.splitlines():
                if line.startswith("*** Add File: ") or line.startswith("*** Update File: "):
                    if current:
                        paths.append(current)
                    current = line.split(":", 1)[1].strip()
                elif line.startswith("*** Delete File: "):
                    if current:
                        paths.append(current)
                    current = None
                elif line.startswith("*** Move to: ") and current is not None:
                    current = line.split(":", 1)[1].strip()
                elif line == "*** End Patch" and current:
                    paths.append(current)
                    current = None
            if current:
                paths.append(current)
    cwd = event.get("cwd")
    if isinstance(cwd, str) and cwd:
        base = Path(cwd).expanduser()
        paths = [str(path if Path(path).is_absolute() else base / path) for path in paths]
    return list(dict.fromkeys(paths))


def handle_hook_event(event: dict[str, Any], *, data_dir: Path | str | None = None) -> dict[str, Any]:
    """Handle a Claude/Codex PostToolUse event without persisting its raw payload."""
    if not isinstance(event, dict):
        return {"captured": False, "reason": "invalid_event"}
    event_name = event.get("hook_event_name") or event.get("event_name")
    if not isinstance(event_name, str) or event_name not in {"PostToolUse", "post_tool_use"}:
        return {"captured": False, "reason": "unsupported_event"}
    tool_name = event.get("tool_name")
    if not isinstance(tool_name, str) or tool_name.lower() not in _CAPTURE_TOOL_NAMES:
        return {"captured": False, "reason": "unsupported_tool"}
    candidates = _event_file_paths(event)
    if not candidates:
        return {"captured": False, "reason": "no_explicit_file_path"}
    data = _data_dir(data_dir)
    entry = _project_for_cwd(data, event.get("cwd"))
    if entry is None:
        return {"captured": False, "reason": "project_not_configured"}
    provider = event.get("provider")
    if not isinstance(provider, str) or provider not in {"claude_code", "codex_cli", "manual"}:
        # Codex tool events carry a turn id; Claude Code hook events carry a
        # transcript path and can otherwise have the same PostToolUse shape.
        provider = "codex_cli" if event.get("turn_id") else "claude_code"
    source_event = {
        "provider": provider,
        "surface": "cli",
        "provider_version": event.get("provider_version"),
        "event_name": event_name,
        "event_id": event.get("tool_use_id") or event.get("tool_call_id") or event.get("call_id"),
        "tool_name": tool_name,
        "tool_use_id": event.get("tool_use_id") or event.get("tool_call_id"),
        "session_ref": event.get("session_id"),
        "run_id": event.get("run_id") or event.get("turn_id"),
        "subproject": event.get("subproject"),
    }
    results = []
    for candidate in candidates[:64]:
        try:
            results.append(capture_file(
                data, entry["project_id"], candidate,
                source_event=source_event, trigger="opt_in_post_tool_use",
            ))
        except ArtifactCaptureError as exc:
            results.append({"captured": False, "reason": type(exc).__name__})
    if len(results) == 1:
        return results[0]
    return {"captured": any(row.get("captured") for row in results), "captured_count": sum(bool(row.get("captured")) for row in results), "results": results}


def hook_main(*, stdin=None, data_dir: Path | str | None = None) -> int:
    """Quiet provider-hook entry point; errors never interrupt a completed tool."""
    stream = stdin if stdin is not None else sys.stdin.buffer
    try:
        raw = stream.read(MAX_EVENT_BYTES + 1)
        if len(raw) > MAX_EVENT_BYTES:
            return 0
        event = json.loads(raw.decode("utf-8"))
        if isinstance(event, dict):
            handle_hook_event(event, data_dir=data_dir)
    except Exception:
        # The post-tool action has already happened; capture is best-effort and
        # must not replace a provider result or echo untrusted event content.
        return 0
    return 0


def list_occurrences(data_dir: Path | str | None, project_id: str) -> list[dict[str, Any]]:
    data = _data_dir(data_dir)
    project_id = _project_id(project_id)
    return sorted(
        _manifest(data, project_id)["occurrences"].values(),
        key=lambda row: (row.get("captured_at", ""), row.get("occurrence_id", "")),
        reverse=True,
    )


def _verified_blob(data: Path, project_id: str, record: dict[str, Any]) -> bytes:
    expected = record.get("content_hash")
    if not isinstance(expected, str) or not _HASH_RE.fullmatch(expected):
        raise ArtifactCaptureError("occurrence has an invalid stored-content hash")
    blob = artifact_store.get_artifact(str(data), project_id, expected)
    if blob is None or artifact_store.content_hash(blob) != expected:
        raise ArtifactCaptureError("stored artifact is missing or failed content-hash verification")
    return blob


def restore_occurrence(
    data_dir: Path | str | None,
    project_id: str,
    occurrence_id: str,
    destination: Path | str | None = None,
    *,
    overwrite: bool = False,
) -> dict[str, Any]:
    data = _data_dir(data_dir)
    project_id = _project_id(project_id)
    if not re.fullmatch(r"[0-9a-f]{32}", occurrence_id):
        raise ArtifactCaptureError("occurrence id is invalid")
    config = _load_config(data)["projects"].get(project_id)
    if not isinstance(config, dict):
        raise ArtifactCaptureError("project capture configuration is missing")
    with _exclusive_lock(_manifest_path(data, project_id).with_suffix(".lock")):
        manifest = _manifest(data, project_id)
        record = manifest["occurrences"].get(occurrence_id)
        if not isinstance(record, dict):
            raise ArtifactCaptureError("capture occurrence was not found")
        blob = _verified_blob(data, project_id, record)
        output_roots = [Path(row).resolve(strict=True) for row in config.get("output_roots", [])]
        if destination is None:
            index = int(record.get("output_root_index", -1))
            if not 0 <= index < len(output_roots):
                raise ArtifactCaptureError("original selected output root is no longer configured")
            destination_path = output_roots[index] / str(record.get("output_relative_path", ""))
        else:
            destination_path = Path(destination).expanduser()
        try:
            candidate_target = destination_path.resolve(strict=False)
        except OSError as exc:
            raise ArtifactCaptureError("restore destination cannot be resolved") from exc
        if not any(candidate_target != root and _is_within(candidate_target, root) for root in output_roots):
            raise ArtifactCaptureError("restore destination is outside every configured output root")
        candidate_target.parent.mkdir(parents=True, exist_ok=True)
        resolved_parent = candidate_target.parent.resolve(strict=True)
        resolved_target = resolved_parent / candidate_target.name
        if not any(resolved_target != root and _is_within(resolved_target, root) for root in output_roots):
            raise ArtifactCaptureError("restore destination changed outside every configured output root")
        if resolved_target.exists() and not overwrite:
            raise ArtifactCaptureError("restore destination exists; pass overwrite=true to replace it")
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile("wb", dir=resolved_parent, prefix=".restore-", suffix=".tmp", delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(blob)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, resolved_target)
        except OSError as exc:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            raise ArtifactCaptureError("could not write the verified artifact bytes to the selected destination") from exc
        restored = resolved_target.read_bytes()
        actual_hash = artifact_store.content_hash(restored)
        verified = actual_hash == record["content_hash"]
        if not verified:
            raise ArtifactCaptureError("restored file failed its post-write content-hash verification")
        relative_dest = next(resolved_target.relative_to(root).as_posix() for root in output_roots if _is_within(resolved_target, root))
        history = record.setdefault("restore_verifications", [])
        history.append({
            "verified": True,
            "destination_relative_path": relative_dest,
            "content_hash": actual_hash,
            "size_bytes": len(restored),
            "source_bytes_match": _sha256(restored) == record.get("source_sha256"),
            "restored_at": _utc_now(),
        })
        del history[:-50]
        manifest["occurrences"][occurrence_id] = record
        _write_manifest(data, project_id, manifest)
    return {"restored": True, "verified": True, "source_bytes_match": _sha256(blob) == record.get("source_sha256"), "destination": str(resolved_target), "content_hash": actual_hash}


def unlink_occurrence(data_dir: Path | str | None, project_id: str, occurrence_id: str) -> bool:
    data = _data_dir(data_dir)
    project_id = _project_id(project_id)
    with _exclusive_lock(_manifest_path(data, project_id).with_suffix(".lock")):
        manifest = _manifest(data, project_id)
        record = manifest["occurrences"].get(occurrence_id)
        if not isinstance(record, dict):
            return False
        record["retention_state"] = "unlinked"
        record["unlinked_at"] = _utc_now()
        _write_manifest(data, project_id, manifest)
    return True


def export_project(data_dir: Path | str | None, project_id: str, output_path: Path | str) -> dict[str, Any]:
    data = _data_dir(data_dir)
    project_id = _project_id(project_id)
    records = list_occurrences(data, project_id)
    active = [row for row in records if row.get("retention_state") == "active"]
    bundle_manifest = {"schema_version": SCHEMA_VERSION, "project_id": project_id, "occurrences": active}
    target = Path(output_path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("wb", dir=target.parent, prefix=".artifact-export-", suffix=".tmp", delete=False) as raw:
            temporary = Path(raw.name)
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", json.dumps(bundle_manifest, ensure_ascii=False, indent=2, sort_keys=True))
            hashes = sorted({row["content_hash"] for row in active})
            for digest in hashes:
                blob = artifact_store.get_artifact(str(data), project_id, digest)
                if blob is None or artifact_store.content_hash(blob) != digest:
                    raise ArtifactCaptureError("export refused because a referenced stored blob is missing or invalid")
                archive.writestr(f"blobs/{digest.split(':', 1)[1]}", blob)
        os.replace(temporary, target)
    except (OSError, zipfile.BadZipFile) as exc:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise ArtifactCaptureError("could not write the local project artifact export") from exc
    return {"exported": True, "path": str(target), "occurrence_count": len(active), "blob_count": len({row["content_hash"] for row in active})}


def purge_project(data_dir: Path | str | None, project_id: str, *, confirm_project_id: str) -> dict[str, Any]:
    data = _data_dir(data_dir)
    project_id = _project_id(project_id)
    if confirm_project_id != project_id:
        raise ArtifactCaptureError("purge requires --confirm-project matching the project id")
    with _exclusive_lock(_manifest_path(data, project_id).with_suffix(".lock")):
        manifest = _manifest(data, project_id)
        active = [row for row in manifest["occurrences"].values() if isinstance(row, dict) and row.get("retention_state") == "active"]
        hashes = sorted({str(row["content_hash"]) for row in active if isinstance(row.get("content_hash"), str)})
        removed_blobs = 0
        for digest in hashes:
            if artifact_store.delete_artifact(str(data), project_id, digest):
                removed_blobs += 1
        for record in active:
            record["retention_state"] = "purged"
            record["purged_at"] = _utc_now()
            record["capture_verification"]["verified"] = False
        _write_manifest(data, project_id, manifest)
    return {"purged": True, "project_id": project_id, "occurrence_count": len(active), "blob_count": removed_blobs}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="meridian artifacts", description="Opt-in local agent output capture and verified restore.")
    parser.add_argument("--data-dir", type=Path, help="Local data directory; defaults to MERIDIAN_DATA_DIR or the Meridian local state directory.")
    commands = parser.add_subparsers(dest="command", required=True)
    configure = commands.add_parser("configure", help="Enable local capture for explicitly selected output roots.")
    configure.add_argument("--project-id", required=True)
    configure.add_argument("--root", type=Path, required=True)
    configure.add_argument("--output-root", type=Path, action="append", required=True)
    configure.add_argument("--subproject")
    configure.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_FILE_BYTES)
    configure.add_argument("--extension", action="append")
    configure.add_argument("--outputs-dir", type=Path)
    disable = commands.add_parser("disable", help="Disable automatic local capture for a project.")
    disable.add_argument("--project-id", required=True)
    status = commands.add_parser("status", help="Show local capture configuration for a project.")
    status.add_argument("--project-id", required=True)
    listing = commands.add_parser("list", help="List local artifact occurrences for a project.")
    listing.add_argument("--project-id", required=True)
    capture = commands.add_parser("capture", help="Capture one explicitly selected local output file.")
    capture.add_argument("--project-id", required=True)
    capture.add_argument("--source", type=Path, required=True)
    capture.add_argument("--provider", choices=("claude_code", "codex_cli", "manual"), default="manual")
    capture.add_argument("--event-id")
    capture.add_argument("--session-ref")
    capture.add_argument("--run-id")
    capture.add_argument("--subproject")
    restore = commands.add_parser("restore", help="Restore one occurrence after verifying its stored content hash.")
    restore.add_argument("--project-id", required=True)
    restore.add_argument("--occurrence-id", required=True)
    restore.add_argument("--destination", type=Path)
    restore.add_argument("--overwrite", action="store_true")
    unlink = commands.add_parser("unlink", help="Unlink one occurrence while retaining the immutable blob.")
    unlink.add_argument("--project-id", required=True)
    unlink.add_argument("--occurrence-id", required=True)
    export = commands.add_parser("export", help="Write a local project archive containing active manifests and verified blobs.")
    export.add_argument("--project-id", required=True)
    export.add_argument("--output", type=Path, required=True)
    purge = commands.add_parser("purge", help="Purge all capture-managed blobs for one confirmed project.")
    purge.add_argument("--project-id", required=True)
    purge.add_argument("--confirm-project", required=True)
    commands.add_parser("hook", help=argparse.SUPPRESS)
    return parser


def cli_main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "hook":
            return hook_main(data_dir=args.data_dir)
        if args.command == "configure":
            result = configure_project(args.data_dir, project_id=args.project_id, project_root=args.root, output_roots=args.output_root, subproject=args.subproject, max_file_bytes=args.max_bytes, extensions=args.extension, outputs_dir=args.outputs_dir)
        elif args.command == "disable":
            result = {"disabled": disable_project(args.data_dir, args.project_id), "project_id": args.project_id}
        elif args.command == "status":
            result = capture_status(args.data_dir, args.project_id)
        elif args.command == "list":
            result = {"project_id": args.project_id, "occurrences": list_occurrences(args.data_dir, args.project_id)}
        elif args.command == "capture":
            result = capture_file(args.data_dir, args.project_id, args.source, source_event={"provider": args.provider, "event_id": args.event_id, "session_ref": args.session_ref, "run_id": args.run_id, "subproject": args.subproject})
        elif args.command == "restore":
            result = restore_occurrence(args.data_dir, args.project_id, args.occurrence_id, args.destination, overwrite=args.overwrite)
        elif args.command == "unlink":
            result = {"unlinked": unlink_occurrence(args.data_dir, args.project_id, args.occurrence_id), "occurrence_id": args.occurrence_id}
        elif args.command == "export":
            result = export_project(args.data_dir, args.project_id, args.output)
        else:
            result = purge_project(args.data_dir, args.project_id, confirm_project_id=args.confirm_project)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except (OSError, ValueError, ArtifactCaptureError, artifact_store.ArtifactStoreError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover - the packaged CLI dispatches through meridian.__main__
    raise SystemExit(cli_main())
