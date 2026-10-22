"""Local provider-session catalog and provenance-linked recovery packs.

The catalog reads filenames and filesystem metadata only. Transcript bytes are
opened only by :func:`hash_selected_jsonl_lines` after a caller selects one
session and a bounded range. Packs are written to the local application data
directory and are never sent to Meridian by this module.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from .secret_redaction import check_for_secrets


CATALOG_SCHEMA_VERSION = 1
PACK_SCHEMA_VERSION = 1
DEFAULT_SCAN_LIMIT = 100_000
MAX_SCAN_LIMIT = 500_000
DEFAULT_CATALOG_LIMIT = 200
MAX_CATALOG_LIMIT = 2_000
MAX_SELECTED_RANGE_BYTES = 128 * 1024
MAX_RANGE_SCAN_BYTES = 16 * 1024 * 1024
MAX_PACK_BYTES = 32 * 1024
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_CODEX_ROLLOUT_RE = re.compile(
    r"^rollout-.*-([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$"
)


class ProviderSessionError(ValueError):
    """A selected local session or context pack failed validation."""


@dataclass(frozen=True)
class ProviderSession:
    provider: str
    surface: str
    session_id: str
    source_path: str
    source_root: str
    size_bytes: int
    modified_at: str
    resume_argv: tuple[str, ...]
    resume_status: str
    modified_at_ns: int | None = None
    source_file_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["resume_argv"] = list(self.resume_argv)
        data["recovery_outcomes"] = {
            "found": True,
            "read": False,
            "provider_native_resume": self.resume_status,
            "reconstructable": "not_built",
            "restorable": "not_provided",
        }
        return data


def _utc_timestamp(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


def _default_roots() -> tuple[Path, Path]:
    home = Path.home()
    claude = Path(os.environ.get("CLAUDE_CONFIG_DIR", home / ".claude")).expanduser()
    codex = Path(os.environ.get("CODEX_HOME", home / ".codex")).expanduser()
    return claude, codex


def _runtime_status(executable: str) -> str:
    return "candidate_unverified" if shutil.which(executable) else "runtime_missing"


def _session_from_file(
    *, provider: str, surface: str, session_id: str, path: Path, root: Path,
    resume_argv: tuple[str, ...], runtime: str,
) -> ProviderSession | None:
    try:
        info = path.stat(follow_symlinks=False)
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_size < 0:
        return None
    return ProviderSession(
        provider=provider,
        surface=surface,
        session_id=session_id,
        source_path=str(path.resolve()),
        source_root=str(root.resolve()),
        size_bytes=info.st_size,
        modified_at=_utc_timestamp(info.st_mtime),
        resume_argv=resume_argv,
        resume_status=_runtime_status(runtime),
        modified_at_ns=info.st_mtime_ns,
        source_file_id=f"{info.st_dev:x}:{info.st_ino:x}",
    )


def _scan_claude_code(root: Path, scan_limit: int) -> tuple[list[ProviderSession], dict[str, Any]]:
    sessions: list[ProviderSession] = []
    project_root = root / "projects"
    scanned = 0
    errors: list[str] = []
    truncated = False
    try:
        project_entries = os.scandir(project_root)
    except FileNotFoundError:
        return sessions, {
            "provider": "claude_code", "root": str(project_root), "available": False,
            "scanned_entries": 0, "candidate_count": 0, "truncated": False,
            "errors": [], "detail": "Claude Code projects directory was not found.",
        }
    except OSError as exc:
        return sessions, {
            "provider": "claude_code", "root": str(project_root), "available": True,
            "scanned_entries": 0, "candidate_count": 0, "truncated": False,
            "errors": [f"{type(exc).__name__}: could not list project directories"],
        }
    with project_entries:
        for project_entry in project_entries:
            scanned += 1
            if scanned > scan_limit:
                truncated = True
                break
            try:
                if not project_entry.is_dir(follow_symlinks=False):
                    continue
                entries = os.scandir(project_entry.path)
            except OSError:
                errors.append("Could not read one Claude Code project directory.")
                continue
            with entries:
                for entry in entries:
                    scanned += 1
                    if scanned > scan_limit:
                        truncated = True
                        break
                    if not entry.name.lower().endswith(".jsonl"):
                        continue
                    session_id = entry.name[:-6]
                    if not _UUID_RE.fullmatch(session_id):
                        continue
                    try:
                        is_file = entry.is_file(follow_symlinks=False)
                        path = Path(entry.path)
                    except OSError:
                        continue
                    if not is_file:
                        continue
                    session = _session_from_file(
                        provider="claude_code", surface="cli", session_id=session_id,
                        path=path, root=root, resume_argv=("claude", "--resume", session_id),
                        runtime="claude",
                    )
                    if session:
                        sessions.append(session)
                if truncated:
                    break
    return sessions, {
        "provider": "claude_code", "root": str(project_root.resolve()), "available": True,
        "scanned_entries": min(scanned, scan_limit), "candidate_count": len(sessions),
        "truncated": truncated, "errors": errors,
        "detail": "Filesystem metadata only; no transcript contents were read.",
    }


def _codex_session_id(filename: str) -> str | None:
    if not filename.lower().endswith(".jsonl"):
        return None
    match = _CODEX_ROLLOUT_RE.fullmatch(filename[:-6])
    return match.group(1).lower() if match else None


def _scan_codex_cli(root: Path, scan_limit: int) -> tuple[list[ProviderSession], dict[str, Any]]:
    sessions: list[ProviderSession] = []
    scanned = 0
    errors: list[str] = []
    truncated = False
    present_roots = [root / "sessions", root / "archived_sessions"]
    found_root = False
    def on_walk_error(_error: OSError) -> None:
        errors.append("Could not read part of the Codex CLI session tree.")

    for base in present_roots:
        if not base.is_dir():
            continue
        found_root = True
        for current, dirs, files in os.walk(base, topdown=True, onerror=on_walk_error, followlinks=False):
            dirs[:] = sorted(d for d in dirs if not (Path(current) / d).is_symlink())
            scanned += len(dirs)
            if scanned >= scan_limit:
                truncated = True
                break
            for filename in files:
                scanned += 1
                if scanned > scan_limit:
                    truncated = True
                    break
                session_id = _codex_session_id(filename)
                if not session_id:
                    continue
                path = Path(current) / filename
                session = _session_from_file(
                    provider="codex_cli", surface="cli", session_id=session_id,
                    path=path, root=root, resume_argv=("codex", "resume", session_id),
                    runtime="codex",
                )
                if session:
                    sessions.append(session)
            if truncated:
                break
        if truncated:
            break
    return sessions, {
        "provider": "codex_cli", "root": str(root.resolve()), "available": found_root,
        "scanned_entries": min(scanned, scan_limit), "candidate_count": len(sessions),
        "truncated": truncated, "errors": errors,
        "detail": (
            "Filesystem metadata only; session_index.jsonl and rollout contents were not read."
            if found_root else "Codex CLI sessions and archived_sessions directories were not found."
        ),
    }


def catalog_local_sessions(
    *,
    claude_config_dir: Path | str | None = None,
    codex_home: Path | str | None = None,
    limit: int = DEFAULT_CATALOG_LIMIT,
    scan_limit: int = DEFAULT_SCAN_LIMIT,
    clock: Any = None,
) -> dict[str, Any]:
    """Catalog local Claude Code and Codex CLI sessions from metadata only.

    The traversal and result count are bounded. The adapter intentionally does
    not read session indexes or transcript content, infer titles, or claim a
    native resume has been smoke-tested.
    """
    if not 1 <= limit <= MAX_CATALOG_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_CATALOG_LIMIT}")
    if not 1 <= scan_limit <= MAX_SCAN_LIMIT:
        raise ValueError(f"scan_limit must be between 1 and {MAX_SCAN_LIMIT}")
    default_claude, default_codex = _default_roots()
    claude_root = Path(claude_config_dir).expanduser() if claude_config_dir is not None else default_claude
    codex_root = Path(codex_home).expanduser() if codex_home is not None else default_codex
    claude_sessions, claude_scan = _scan_claude_code(claude_root, scan_limit)
    codex_sessions, codex_scan = _scan_codex_cli(codex_root, scan_limit)
    all_sessions = sorted(
        claude_sessions + codex_sessions,
        key=lambda session: (session.modified_at, session.provider, session.session_id),
        reverse=True,
    )
    result_sessions = all_sessions[:limit]
    return {
        "schema_version": CATALOG_SCHEMA_VERSION,
        "generated_at": _utc_timestamp((clock or time.time)()),
        "complete": not any(scan["truncated"] or scan["errors"] for scan in (claude_scan, codex_scan)),
        "limit": limit,
        "scan_limit_per_provider": scan_limit,
        "total_candidates": len(all_sessions),
        "returned_count": len(result_sessions),
        "providers": [claude_scan, codex_scan],
        "not_cataloged_surfaces": [
            {
                "provider": "claude_agent_sdk",
                "status": "host_adapter_required",
                "detail": "Use the installed Agent SDK list/read/resume interface; this filesystem catalog does not launch SDK code.",
            },
            {
                "provider": "codex_app",
                "status": "app_server_or_host_required",
                "detail": "Use Codex app-server or host thread tools; app threads are not inferred from local CLI rollout files.",
            },
            {
                "provider": "chatgpt",
                "status": "provider_ui_or_user_export",
                "detail": "Account chats are not treated as local CLI sessions or importable archives.",
            },
            {
                "provider": "claude_web",
                "status": "provider_ui_or_user_export",
                "detail": "Account chats are not treated as Claude Code CLI sessions or importable archives.",
            },
        ],
        "sessions": [session.as_dict() for session in result_sessions],
    }


def hash_selected_jsonl_lines(
    session: ProviderSession,
    start_line: int,
    end_line: int,
    *,
    max_selected_bytes: int = MAX_SELECTED_RANGE_BYTES,
    max_scan_bytes: int = MAX_RANGE_SCAN_BYTES,
) -> dict[str, Any]:
    """Hash a selected 1-based JSONL line range without returning its content.

    Reads only the chosen source file and only after selection. A bounded scan
    from the beginning avoids unbounded work on multi-gigabyte provider logs;
    if the range falls outside the scan budget, use a provider-supported
    message/turn selector in a future versioned adapter.
    """
    _validate_provider_session(session)
    if start_line < 1 or end_line < start_line:
        raise ProviderSessionError("line range must be 1-based and end_line >= start_line")
    path = Path(session.source_path)
    try:
        before = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise ProviderSessionError("selected provider session file is unavailable") from exc
    if not stat.S_ISREG(before.st_mode):
        raise ProviderSessionError("selected provider session is no longer a regular file")
    if (
        before.st_size != session.size_bytes
        or _utc_timestamp(before.st_mtime) != session.modified_at
        or (session.modified_at_ns is not None and before.st_mtime_ns != session.modified_at_ns)
        or (session.source_file_id is not None and f"{before.st_dev:x}:{before.st_ino:x}" != session.source_file_id)
    ):
        raise ProviderSessionError("provider session changed since cataloging; refresh the catalog before selecting a range")
    selected = bytearray()
    line_number = 0
    scanned = 0
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            handle = os.fdopen(descriptor, "rb")
        except BaseException:
            os.close(descriptor)
            raise
        with handle:
            opened = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_size != session.size_bytes
                or (session.modified_at_ns is not None and opened.st_mtime_ns != session.modified_at_ns)
                or (session.source_file_id is not None and f"{opened.st_dev:x}:{opened.st_ino:x}" != session.source_file_id)
            ):
                raise ProviderSessionError("provider session changed while opening the selected range")
            while line_number < end_line:
                remaining_scan = max_scan_bytes - scanned
                if remaining_scan <= 0:
                    raise ProviderSessionError("line range exceeds the bounded scan budget; use a provider-native selector")
                line = handle.readline(remaining_scan + 1)
                if not line:
                    break
                scanned += len(line)
                if scanned > max_scan_bytes:
                    raise ProviderSessionError("line range exceeds the bounded scan budget; use a provider-native selector")
                line_number += 1
                if line_number >= start_line:
                    selected.extend(line)
                    if len(selected) > max_selected_bytes:
                        raise ProviderSessionError("selected range exceeds the byte limit")
            after_open = os.fstat(handle.fileno())
        after = path.stat(follow_symlinks=False)
    except ProviderSessionError:
        raise
    except OSError as exc:
        raise ProviderSessionError("could not read the selected provider session range") from exc
    if line_number < end_line:
        raise ProviderSessionError("selected end line does not exist")
    if (
        after_open.st_size != before.st_size
        or after_open.st_mtime_ns != before.st_mtime_ns
        or not stat.S_ISREG(after.st_mode)
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
        or (session.source_file_id is not None and f"{after.st_dev:x}:{after.st_ino:x}" != session.source_file_id)
    ):
        raise ProviderSessionError("provider session changed while the selected range was read")
    return {
        "kind": "line_range",
        "start": start_line,
        "end": end_line,
        "selector_quality": "coarse",
        "sha256": hashlib.sha256(selected).hexdigest(),
        "selected_bytes": len(selected),
        "source_mtime_utc": session.modified_at,
        "content_returned": False,
    }


def _validate_pack_text(value: Any, field: str, *, required: bool = False) -> str | None:
    if value is None:
        if required:
            raise ProviderSessionError(f"{field} is required")
        return None
    if not isinstance(value, str):
        raise ProviderSessionError(f"{field} must be a string")
    text = value.strip()
    if required and not text:
        raise ProviderSessionError(f"{field} is required")
    if len(text) > 4_000:
        raise ProviderSessionError(f"{field} exceeds the 4000-character limit")
    if text:
        try:
            check_for_secrets(text, context=f"local recovery pack {field}")
        except ValueError as exc:
            raise ProviderSessionError(str(exc)) from exc
    return text or None


def _validate_provider_session(session: ProviderSession) -> None:
    if not isinstance(session, ProviderSession):
        raise ProviderSessionError("a cataloged provider session is required")
    if session.provider not in {"claude_code", "codex_cli"} or session.surface != "cli":
        raise ProviderSessionError("provider session is not from a supported local CLI surface")
    if not _UUID_RE.fullmatch(session.session_id):
        raise ProviderSessionError("provider session id is invalid")
    source = Path(session.source_path)
    root = Path(session.source_root)
    if not source.is_absolute() or not root.is_absolute():
        raise ProviderSessionError("provider session paths must be absolute local paths")
    try:
        source.resolve(strict=False).relative_to(root.resolve(strict=False))
    except (OSError, ValueError) as exc:
        raise ProviderSessionError("provider session path is outside its configured local root") from exc


def _compact_task_state(task_state: dict[str, Any]) -> dict[str, Any]:
    fields = ("objective", "current_step", "next_action", "recent_transcript")
    result: dict[str, Any] = {}
    for field in fields:
        row = task_state.get(field)
        if isinstance(row, dict):
            text = _validate_pack_text(
                row.get("text"), f"task.{field}",
                required=field in ("objective", "next_action"),
            )
            refs = row.get("source_refs", [])
        else:
            text = _validate_pack_text(
                row, f"task.{field}", required=field in ("objective", "next_action"),
            )
            refs = []
        if refs is None:
            refs = []
        if not isinstance(refs, list) or any(not isinstance(ref, str) for ref in refs):
            raise ProviderSessionError(f"task.{field}.source_refs must be a list of source-ref ids")
        if len(refs) > 32:
            raise ProviderSessionError(f"task.{field}.source_refs exceeds the 32-reference limit")
        result[field] = {"text": text, "source_refs": refs}
    for field in ("constraints", "decisions", "failed_approaches", "recency_tail"):
        raw = task_state.get(field, [])
        if not isinstance(raw, list):
            raise ProviderSessionError(f"task.{field} must be a list")
        if len(raw) > 32:
            raise ProviderSessionError(f"task.{field} exceeds the 32-entry limit")
        rows = []
        for index, entry in enumerate(raw):
            if isinstance(entry, str):
                text, refs = entry, []
            elif isinstance(entry, dict):
                text, refs = entry.get("text") or entry.get("summary"), entry.get("source_refs", [])
            else:
                raise ProviderSessionError(f"task.{field}[{index}] must be text or an object")
            clean = _validate_pack_text(text, f"task.{field}[{index}]", required=True)
            if not isinstance(refs, list) or any(not isinstance(ref, str) for ref in refs):
                raise ProviderSessionError(f"task.{field}[{index}].source_refs must be a list")
            if len(refs) > 32:
                raise ProviderSessionError(f"task.{field}[{index}].source_refs exceeds the 32-reference limit")
            rows.append({"text": clean, "source_refs": refs})
        result[field] = rows
    ledger = task_state.get("command_error_ledger", [])
    if not isinstance(ledger, list) or len(ledger) > 32:
        raise ProviderSessionError("task.command_error_ledger must be a list of at most 32 entries")
    clean_ledger = []
    for index, entry in enumerate(ledger):
        if not isinstance(entry, dict):
            raise ProviderSessionError(f"task.command_error_ledger[{index}] must be an object")
        row = {}
        for key in ("command", "result", "error", "observed_at"):
            value = entry.get(key)
            if value is not None:
                row[key] = _validate_pack_text(
                    value, f"task.command_error_ledger[{index}].{key}", required=True,
                )
        refs = entry.get("source_refs", [])
        if not isinstance(refs, list) or any(not isinstance(ref, str) for ref in refs):
            raise ProviderSessionError(f"task.command_error_ledger[{index}].source_refs must be a list")
        if len(refs) > 32:
            raise ProviderSessionError(f"task.command_error_ledger[{index}].source_refs exceeds the 32-reference limit")
        row["source_refs"] = refs
        clean_ledger.append(row)
    result["command_error_ledger"] = clean_ledger
    return result


def _compact_meridian_recovery(
    response: dict[str, Any] | None,
    *,
    project_id: str,
    sprint_version: str,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Extract a small live board snapshot from get_session_recovery output."""
    if response is None:
        return {"available": False, "reason": "live Meridian recovery state was not supplied"}, None
    if not isinstance(response, dict):
        raise ProviderSessionError("meridian_recovery must be a get_session_recovery result object")
    recovery = response.get("recovery")
    continuation = response.get("continuation")
    if not isinstance(recovery, dict) or not isinstance(continuation, dict):
        return {"available": False, "reason": "the supplied Meridian recovery response has no live continuation"}, None
    record_project = recovery.get("project_id")
    if record_project is not None and str(record_project) != project_id:
        raise ProviderSessionError("Meridian recovery response belongs to a different project")
    record_version = recovery.get("sprint_version")
    if record_version is not None and str(record_version) != sprint_version:
        raise ProviderSessionError("Meridian recovery response has a different sprint version")
    board = continuation.get("board")
    if not isinstance(board, dict):
        return {"available": False, "reason": "live board snapshot is unavailable"}, None
    raw_items = board.get("items") or []
    if not isinstance(raw_items, list):
        raise ProviderSessionError("Meridian continuation board.items must be a list")
    items = []
    for index, item in enumerate(raw_items[:100]):
        if not isinstance(item, dict):
            continue
        item_id = _validate_pack_text(item.get("id"), f"meridian.board.items[{index}].id")
        title = _validate_pack_text(item.get("title") or item.get("description"), f"meridian.board.items[{index}].title")
        status = _validate_pack_text(item.get("status"), f"meridian.board.items[{index}].status")
        items.append({"id": item_id, "title": title, "status": status or "unknown"})
    claims = []
    raw_claims = continuation.get("active_file_claims") or []
    if isinstance(raw_claims, list):
        for claim in raw_claims[:100]:
            if not isinstance(claim, dict):
                continue
            claims.append({
                "file_path": _validate_pack_text(claim.get("file_path"), "meridian.active_file_claims.file_path"),
                "mode": _validate_pack_text(claim.get("mode"), "meridian.active_file_claims.mode"),
                "expires_at": _validate_pack_text(claim.get("expires_at"), "meridian.active_file_claims.expires_at"),
            })
    revision_hash = _validate_pack_text(board.get("revision_hash"), "meridian.board.revision_hash")
    blocker_summary = board.get("blocker_summary")
    if blocker_summary is not None:
        blocker_summary = _validate_pack_text(str(blocker_summary), "meridian.board.blocker_summary")
    state = {
        "available": True,
        "version_filter": _validate_pack_text(str(board.get("version_filter") or sprint_version), "meridian.board.version_filter"),
        "item_count": int(board.get("item_count") or len(raw_items)),
        "revision_hash": revision_hash,
        "blocker_summary": blocker_summary,
        "items": items,
        "items_truncated": len(raw_items) > len(items),
        "active_file_claims": claims,
        "claims_truncated": isinstance(raw_claims, list) and len(raw_claims) > len(claims),
        "observed_at": _utc_timestamp(time.time()),
    }
    source_ref = {
        "id": "meridian-live-board",
        "uri": f"meridian://project/{project_id}/sprint/{sprint_version}",
        "kind": "live-board-snapshot",
        "revision_hash": revision_hash,
    }
    return state, source_ref


def _git_state(repo_root: Path | None) -> dict[str, Any]:
    if repo_root is None:
        return {"repo_root": None, "git_head": None, "worktree_state": "unknown", "changed_paths": []}
    repo = repo_root.expanduser().resolve()
    try:
        head = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--show-toplevel", "--verify", "HEAD"],
            capture_output=True, text=True, timeout=3, check=True,
        ).stdout.splitlines()
        status = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain=v1", "--untracked-files=normal"],
            capture_output=True, text=True, timeout=3, check=True,
        ).stdout.splitlines()
    except (OSError, subprocess.SubprocessError):
        return {"repo_root": str(repo), "git_head": None, "worktree_state": "unknown", "changed_paths": []}
    root = head[0] if len(head) >= 1 else str(repo)
    commit = head[-1] if head else None
    paths = [line[3:] for line in status[:200] if len(line) >= 4]
    return {
        "repo_root": root,
        "git_head": commit,
        "worktree_state": "dirty" if status else "clean",
        "changed_paths": paths,
        "changed_paths_truncated": len(status) > len(paths),
    }


def _canonical_hash(payload: dict[str, Any]) -> str:
    unsigned = {key: value for key, value in payload.items() if key != "integrity"}
    raw = json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def build_local_context_pack(
    session: ProviderSession,
    *,
    project_id: str,
    sprint_version: str,
    task_state: dict[str, Any],
    meridian_recovery: dict[str, Any] | None = None,
    transcript_range: dict[str, Any] | None = None,
    artifact_refs: list[dict[str, Any]] | None = None,
    repo_root: Path | None = None,
    clock: Any = None,
) -> dict[str, Any]:
    """Build a compact, local-only reconstruction pack for one selected session.

    ``task_state`` must contain approved compact summaries and optional Meridian
    pointers. Raw transcripts are never included. Each text assertion may link
    to explicit ``source_refs``; a selected transcript range is represented by
    its locator, selector quality, and optional hash only.
    """
    _validate_provider_session(session)
    project_id = _validate_pack_text(project_id, "project_id", required=True) or ""
    sprint_version = _validate_pack_text(sprint_version, "sprint_version", required=True) or ""
    if not isinstance(task_state, dict):
        raise ProviderSessionError("task_state must be an object of compact, approved summaries")
    task = _compact_task_state(task_state)
    meridian_state, meridian_source_ref = _compact_meridian_recovery(
        meridian_recovery, project_id=project_id, sprint_version=sprint_version,
    )
    if transcript_range is not None:
        if not isinstance(transcript_range, dict):
            raise ProviderSessionError("transcript_range must be an object")
        kind = transcript_range.get("kind")
        quality = transcript_range.get("selector_quality")
        if kind not in {"line_range", "turn_id", "event_id", "timestamp_window"}:
            raise ProviderSessionError("transcript range kind is unsupported")
        if quality not in {"exact", "coarse", "unavailable"}:
            raise ProviderSessionError("transcript range selector_quality is unsupported")
        range_data = {
            "kind": kind,
            "start": _validate_pack_text(str(transcript_range.get("start", "")), "transcript_range.start", required=True),
            "end": _validate_pack_text(str(transcript_range.get("end", "")), "transcript_range.end", required=True),
            "selector_quality": quality,
            "sha256": transcript_range.get("sha256"),
        }
        if range_data["sha256"] is not None and not re.fullmatch(r"[0-9a-f]{64}", str(range_data["sha256"])):
            raise ProviderSessionError("transcript range sha256 must be a 64-character lowercase hex digest")
    else:
        range_data = {"kind": "none", "start": None, "end": None, "selector_quality": "unavailable", "sha256": None}
    refs = [{
        "id": "provider-range-0",
        "provider": session.provider,
        "native_session_id": session.session_id,
        "source_locator": session.source_path,
        "source_sha256": None,
        "source_size_bytes": session.size_bytes,
        "source_mtime_utc": session.modified_at,
        "selected_range_sha256": range_data["sha256"],
        "range": range_data,
        "captured_at": _utc_timestamp((clock or time.time)()),
        "resolution": "verified" if range_data["sha256"] else "coarse",
    }]
    if meridian_source_ref:
        refs.append(meridian_source_ref)
    task_source_refs = task_state.get("source_refs", [])
    if not isinstance(task_source_refs, list) or len(task_source_refs) > 64:
        raise ProviderSessionError("task.source_refs must be a list of at most 64 items")
    for ref in task_source_refs:
        if not isinstance(ref, dict):
            raise ProviderSessionError("source_refs entries must be objects")
        ref_id = _validate_pack_text(ref.get("id"), "source_refs.id", required=True)
        uri = _validate_pack_text(ref.get("uri"), "source_refs.uri", required=True)
        refs.append({"id": ref_id, "uri": uri, "kind": _validate_pack_text(ref.get("kind"), "source_refs.kind")})
    known_ref_ids = {str(ref.get("id")) for ref in refs if ref.get("id") is not None}
    for field, entry in task.items():
        rows = entry if isinstance(entry, list) else [entry]
        for row in rows:
            for ref_id in row.get("source_refs", []):
                if ref_id not in known_ref_ids:
                    raise ProviderSessionError(f"task.{field} references unknown source ref {ref_id!r}")
    artifacts = artifact_refs or []
    if not isinstance(artifacts, list) or len(artifacts) > 100:
        raise ProviderSessionError("artifact_refs must be a list of at most 100 items")
    clean_artifacts = []
    for index, ref in enumerate(artifacts):
        if not isinstance(ref, dict):
            raise ProviderSessionError(f"artifact_refs[{index}] must be an object")
        uri = _validate_pack_text(ref.get("uri"), f"artifact_refs[{index}].uri", required=True)
        digest = ref.get("sha256")
        if digest is not None and not re.fullmatch(r"[0-9a-f]{64}", str(digest)):
            raise ProviderSessionError(f"artifact_refs[{index}].sha256 is invalid")
        status = _validate_pack_text(ref.get("status", "unverified"), f"artifact_refs[{index}].status", required=True)
        clean_artifacts.append({"uri": uri, "sha256": digest, "status": status})
    now = _utc_timestamp((clock or time.time)())
    task_ready = bool(task["objective"]["text"] and task["next_action"]["text"])
    recent_transcript_ready = bool(task["recent_transcript"]["text"])
    if recent_transcript_ready and range_data["kind"] != "none" and "provider-range-0" not in task["recent_transcript"]["source_refs"]:
        raise ProviderSessionError("task.recent_transcript must reference provider-range-0 when a transcript range is selected")
    git_state = _git_state(repo_root)
    git_state_complete = bool(git_state.get("git_head")) and not git_state.get("changed_paths_truncated", False)
    reconstruction_ready = (
        task_ready
        and recent_transcript_ready
        and range_data["kind"] != "none"
        and meridian_state.get("available", False)
        and not meridian_state.get("items_truncated", False)
        and not meridian_state.get("claims_truncated", False)
        and git_state_complete
    )
    pack: dict[str, Any] = {
        "schema_version": PACK_SCHEMA_VERSION,
        "pack_id": uuid.uuid4().hex,
        "generated_at": now,
        "project": {"project_id": project_id, "sprint_version": sprint_version},
        "source": {
            "provider": session.provider,
            "surface": session.surface,
            "native_session_id": session.session_id,
            "client_version": None,
            "local_root_ref": None,
            "source_locator": session.source_path,
            "source_sha256": None,
            "source_size_bytes": session.size_bytes,
            "source_mtime_utc": session.modified_at,
            "selected_range_sha256": range_data["sha256"],
            "adapter": {"id": f"{session.provider}-metadata", "version": str(CATALOG_SCHEMA_VERSION)},
            "range": range_data,
        },
        "task": task,
        "verified_state": {
            "observed_at": now,
            **git_state,
            "meridian": meridian_state,
            "artifact_refs": clean_artifacts,
            "source_refs": ["provider-range-0"],
        },
        "source_refs": refs,
        "coverage": {
            "complete": (
                reconstruction_ready
            ),
            "truncated": bool(
                meridian_state.get("items_truncated", False)
                or meridian_state.get("claims_truncated", False)
                or git_state.get("changed_paths_truncated", False)
            ),
            "omissions": (
                ([] if range_data["kind"] != "none" else ["no provider transcript range selected"])
                + ([] if recent_transcript_ready else ["no compact summary of the selected local transcript range was supplied"])
                + ([] if meridian_state.get("available", False) else ["live Meridian board state was not supplied or was unavailable"])
                + (["live Meridian board or active claims exceeded the pack summary limit"] if meridian_state.get("items_truncated", False) or meridian_state.get("claims_truncated", False) else [])
                + ([] if git_state_complete else ["repository head or complete changed-path state was unavailable"])
            ),
            "redaction": "caller_reviewed",
        },
        "recovery_outcomes": {
            "found": True,
            "read": range_data["sha256"] is not None,
            "provider_native_resume": session.resume_status,
            "reconstructable": "ready" if reconstruction_ready else "incomplete",
            "restorable": "not_proven",
        },
    }
    pack["integrity"] = {"canonical_sha256": _canonical_hash(pack)}
    encoded = json.dumps(pack, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_PACK_BYTES:
        raise ProviderSessionError(f"context pack exceeds {MAX_PACK_BYTES} bytes")
    return pack


def _safe_component(value: str) -> str:
    component = re.sub(r"[^A-Za-z0-9_.-]", "_", value.strip())[:100]
    return "project" if component in {"", ".", ".."} else component


def write_local_context_pack(data_dir: Path | str, pack: dict[str, Any]) -> Path:
    """Atomically persist one pack under local application state."""
    if not isinstance(pack, dict) or pack.get("schema_version") != PACK_SCHEMA_VERSION:
        raise ProviderSessionError("unsupported local context-pack schema")
    integrity = pack.get("integrity")
    expected = integrity.get("canonical_sha256") if isinstance(integrity, dict) else None
    if not isinstance(expected, str) or expected != _canonical_hash(pack):
        raise ProviderSessionError("context-pack integrity hash does not match")
    encoded = json.dumps(pack, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    if len(encoded) > MAX_PACK_BYTES:
        raise ProviderSessionError(f"context pack exceeds {MAX_PACK_BYTES} bytes")
    project = _safe_component(str(pack.get("project", {}).get("project_id", "project")))
    pack_id = str(pack.get("pack_id", ""))
    if not re.fullmatch(r"[0-9a-f]{32}", pack_id):
        raise ProviderSessionError("context pack id is invalid")
    directory = Path(data_dir) / "session_recovery" / "packs" / project
    path = directory / f"{pack_id}.json"
    temporary: Path | None = None
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name == "posix":
            directory.chmod(0o700)
        with tempfile.NamedTemporaryFile("wb", dir=directory, prefix=".pack-", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(encoded)
            handle.write(b"\n")
        os.replace(temporary, path)
    except OSError as exc:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        raise ProviderSessionError("could not write the local context pack") from exc
    return path


def read_local_context_pack(path: Path | str) -> dict[str, Any] | None:
    """Read and verify a local pack; malformed or modified packs return None."""
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != PACK_SCHEMA_VERSION
            or not isinstance(value.get("integrity"), dict)
            or value["integrity"].get("canonical_sha256") != _canonical_hash(value)
    ):
        return None
    return value


def _resolve_local_data_dir() -> Path:
    from .local_runner import default_state_dir

    return default_state_dir()


def cli_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="meridian recovery", description="Local provider-session catalog and recovery-pack tools.")
    commands = parser.add_subparsers(dest="command", required=True)
    catalog_parser = commands.add_parser("catalog", help="List local Claude Code and Codex CLI sessions by metadata only.")
    catalog_parser.add_argument("--claude-root", type=Path)
    catalog_parser.add_argument("--codex-home", type=Path)
    catalog_parser.add_argument("--limit", type=int, default=DEFAULT_CATALOG_LIMIT)
    catalog_parser.add_argument("--scan-limit", type=int, default=DEFAULT_SCAN_LIMIT)
    pack_parser = commands.add_parser("pack", help="Build a local-only pack for one selected provider session.")
    pack_parser.add_argument("--provider", choices=("claude_code", "codex_cli"), required=True)
    pack_parser.add_argument("--session-id", required=True)
    pack_parser.add_argument("--project-id", required=True)
    pack_parser.add_argument("--sprint-version", required=True)
    pack_parser.add_argument("--context-file", type=Path, required=True, help="JSON containing compact approved task summaries and source refs; do not pass raw transcripts.")
    pack_parser.add_argument("--repo-root", type=Path)
    pack_parser.add_argument("--claude-root", type=Path)
    pack_parser.add_argument("--codex-home", type=Path)
    pack_parser.add_argument("--catalog-limit", type=int, default=MAX_CATALOG_LIMIT, help="Maximum sessions to inspect while selecting the pack source.")
    pack_parser.add_argument("--data-dir", type=Path, help="Local app-state directory (defaults to Meridian's state directory).")
    pack_parser.add_argument("--start-line", type=int)
    pack_parser.add_argument("--end-line", type=int)
    args = parser.parse_args(argv)
    try:
        catalog_limit = args.limit if args.command == "catalog" else args.catalog_limit
        catalog = catalog_local_sessions(
            claude_config_dir=args.claude_root,
            codex_home=args.codex_home,
            limit=catalog_limit,
            scan_limit=getattr(args, "scan_limit", DEFAULT_SCAN_LIMIT),
        )
        if args.command == "catalog":
            print(json.dumps(catalog, indent=2, sort_keys=True))
            return 0
        if (args.start_line is None) != (args.end_line is None):
            parser.error("--start-line and --end-line must be provided together")
        session_data = next((
            row for row in catalog["sessions"]
            if row["provider"] == args.provider and row["session_id"] == args.session_id
        ), None)
        if session_data is None:
            print(json.dumps({"error": "selected provider session was not found in the bounded catalog"}))
            return 1
        session = ProviderSession(
            provider=session_data["provider"], surface=session_data["surface"],
            session_id=session_data["session_id"], source_path=session_data["source_path"],
            source_root=session_data["source_root"], size_bytes=session_data["size_bytes"],
            modified_at=session_data["modified_at"], resume_argv=tuple(session_data["resume_argv"]),
            resume_status=session_data["resume_status"],
            modified_at_ns=session_data.get("modified_at_ns"),
            source_file_id=session_data.get("source_file_id"),
        )
        context_payload = json.loads(args.context_file.read_text(encoding="utf-8"))
        if not isinstance(context_payload, dict):
            raise ProviderSessionError("context file must contain a JSON object")
        task_state = context_payload.get("task", context_payload)
        if not isinstance(task_state, dict):
            raise ProviderSessionError("context file task field must be a JSON object")
        transcript_range = None
        if args.start_line is not None:
            transcript_range = hash_selected_jsonl_lines(session, args.start_line, args.end_line)
        pack = build_local_context_pack(
            session,
            project_id=args.project_id,
            sprint_version=args.sprint_version,
            task_state=task_state,
            meridian_recovery=context_payload.get("meridian_recovery"),
            transcript_range=transcript_range,
            artifact_refs=context_payload.get("artifact_refs", []),
            repo_root=args.repo_root,
        )
        output_path = write_local_context_pack(args.data_dir or _resolve_local_data_dir(), pack)
        print(json.dumps({
            "pack_id": pack["pack_id"], "path": str(output_path),
            "sha256": pack["integrity"]["canonical_sha256"],
            "recovery_outcomes": pack["recovery_outcomes"],
            "stored_locally": True,
        }, indent=2, sort_keys=True))
        return 0
    except (OSError, ValueError, ProviderSessionError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(cli_main())
