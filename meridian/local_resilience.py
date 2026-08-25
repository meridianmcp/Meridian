"""Local lifecycle hygiene: durable temp-run manifests, process ownership/
cleanup, bounded quotas, and disk-only prestage placement (c7ef8ff7, MDE-9 P1).

Problem
-------
Several parts of this codebase run risky LOCAL operations that can be
interrupted mid-flight by a crash of the owning process itself (not just a
single call timing out, which existing code already handles well -- e.g.
``extensions/meridian-docs/meridian_docs/render_gate.py``'s own bounded
Word-COM/soffice timeout-and-terminate watchdogs): a Word COM render, a
LibreOffice conversion, a docx draft/prestage write. When the WHOLE host
process dies (killed, OOM, power loss) before its own in-call cleanup ever
runs, three things can be left behind with nothing watching them:

  1. A leftover OS temp directory (e.g. ``render_gate``'s own
     ``tempfile.TemporaryDirectory(prefix="meridian_render_gate_")`` --
     its finalizer never runs on a hard process kill).
  2. A leftover OWNED child process (WINWORD.EXE / soffice.bin) that the
     crashed process would otherwise have terminated itself.
  3. A prestage/draft artifact whose write was interrupted mid-way, with no
     record of whether it's safe to retry or needs to be quarantined.

This module is the general-purpose, dependency-light layer that makes those
three recoverable ON THE NEXT PROCESS START ("restart scavenging"), plus two
related local-hygiene primitives this item's acceptance criteria call out:
bounded disk quotas that degrade VISIBLY rather than silently, and a
disk-only prestage guard so a draft/temp artifact can never be silently
placed under a cloud-sync folder (OneDrive) where a partial/torn write
could sync mid-write or a draft could leak into a shared cloud folder.

Design, matching this codebase's established conventions rather than
inventing new ones
------------------------------------------------------------------------
  * **Fail-closed by default, injected callables for anything dangerous** --
    the exact pattern ``meridian/worktree_cleanup.py``'s quarantine trio
    (``build_quarantine_manifest`` / ``quarantine_temp_outputs`` /
    ``purge_quarantined_output``) already established: an ``ownership_check``
    / ``kill_fn`` is never assumed, and omitting one means "refuse", never
    "guess". This module reuses ``worktree_cleanup``'s quarantine primitives
    directly for interrupted-run cleanup rather than re-implementing them.
  * **PID liveness via ``os.kill(pid, 0)``** -- the same catch tuple
    ``worktree_cleanup._pid_is_alive`` / ``meridian/tunnel_client.py`` /
    ``meridian/orphan_reaper.py`` already use, so this repo has ONE
    liveness-check convention.
  * **Durable JSON ledger, atomic ``os.replace`` write** -- the same idiom
    ``extensions/meridian-outputs/meridian_outputs/annotate.py`` /
    ``fingerprint.py`` and ``extensions/meridian-docs/meridian_docs
    /render_gate.py``'s new render-receipt ledger (1e6150ef) already use.
  * **No hard cross-package import.** ``meridian-outputs`` and
    ``meridian-docs`` are standalone, separately-installable packages with
    their OWN ``pyproject.toml`` and zero dependency on this ``meridian``
    core package (see their own module docstrings -- "no hosted call is
    made", "fully local"). This module therefore never imports either
    extension; anything reused across that boundary (e.g. ``render_gate``'s
    ``tempfile.TemporaryDirectory`` prefix) is a duck-typed, documented
    STRING CONVENTION passed as a parameter with a matching default, never
    an import.
  * **Local paths never enter shared capability-manifest state.**
    ``meridian/capability_manifest.py`` already rejects an absolute local
    path at manifest-write time (``normalize_capability`` /
    ``normalize_manifest``, via its own ``_check_no_secrets_or_local_paths``).
    This module's own state (temp-run manifests, quota reports, cleanup
    receipts -- all of which legitimately contain real local paths) is
    stored ONLY in a local JSON ledger under a caller-chosen local directory
    -- never fed into ``set_capability_manifest``/
    ``db.set_project_capability_manifest``. :func:`summarize_for_capability_
    manifest` is the one function in this module that DOES build a
    manifest-shaped dict; it contains no paths by construction and is
    additionally self-validated through the real
    ``capability_manifest.normalize_capability`` validator before being
    returned, so a future edit that accidentally added a path would fail
    loudly (``CapabilityManifestError``) rather than silently leak one.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from . import capability_manifest, worktree_cleanup

__all__ = [
    "LocalResilienceError",
    "RUN_STARTED",
    "RUN_COMPLETED",
    "RUN_FAILED",
    "RUN_RESOLVED_RESUME",
    "RUN_RESOLVED_QUARANTINE",
    "RUN_STATES",
    "is_onedrive_path",
    "assert_disk_only_prestage_path",
    "check_local_quota",
    "enforce_local_quota",
    "start_temp_run",
    "complete_temp_run",
    "fail_temp_run",
    "get_temp_run",
    "list_temp_runs",
    "scan_interrupted_runs",
    "resolve_interrupted_run",
    "terminate_owned_process",
    "reap_stale_render_tempdirs",
    "list_cleanup_receipts",
    "summarize_for_capability_manifest",
]

RUN_STARTED = "started"
RUN_COMPLETED = "completed"
RUN_FAILED = "failed"
RUN_RESOLVED_RESUME = "resolved_resume"
RUN_RESOLVED_QUARANTINE = "resolved_quarantine"
RUN_STATES = (
    RUN_STARTED, RUN_COMPLETED, RUN_FAILED, RUN_RESOLVED_RESUME, RUN_RESOLVED_QUARANTINE,
)


class LocalResilienceError(ValueError):
    """Raised for fail-closed input errors in this module (invalid state
    names, missing required arguments). Mirrors the "one exception type per
    module" convention already used by ``artifact_registry.RegistryError``/
    ``research_evidence.EnvelopeValidationError`` -- callers catch one thing.
    """


# ---------------------------------------------------------------------------
# Disk-only prestage placement -- OneDrive must never receive temporary or
# draft artifacts.
# ---------------------------------------------------------------------------

# Environment variables Windows/OneDrive itself sets for a signed-in
# account's sync root(s) -- checked first since they're authoritative for
# THIS machine's actual configured sync folder(s), not a filename guess.
_ONEDRIVE_ENV_VARS: tuple[str, ...] = ("OneDriveCommercial", "OneDriveConsumer", "OneDrive")

# Fallback signal when the env vars aren't set in THIS process's environment
# (e.g. a service account, a CI runner, or a path passed in from a different
# session that had OneDrive configured) -- a literal "OneDrive" path segment
# is still a strong, if secondary, signal. Case-insensitive on Windows via
# os.path.normcase; matched as a whole path segment so e.g.
# "C:/Projects/OneDriveExporter" (not actually inside OneDrive) is NOT a
# false positive.
_ONEDRIVE_SEGMENT_RE = re.compile(r"(?i)^onedrive([ _-].*)?$")


def _onedrive_roots() -> list[str]:
    roots: list[str] = []
    for var in _ONEDRIVE_ENV_VARS:
        value = os.environ.get(var)
        if value and value.strip():
            roots.append(value.strip())
    return roots


def is_onedrive_path(path: str) -> bool:
    """Best-effort: ``True`` iff ``path`` resolves under a known OneDrive
    sync root, or contains a path segment that is itself named like a
    OneDrive folder.

    Two independent signals, either sufficient on its own:
      1. ``path`` is (or is under) one of this process's configured
         ``OneDrive*`` environment variable roots -- authoritative for THIS
         machine.
      2. ANY path segment matches ``OneDrive``/``OneDrive - <tenant>``
         case-insensitively -- catches a OneDrive root passed in from a
         different context (a path string authored on/for a machine whose
         env vars this process doesn't have), at the cost of being a
         heuristic rather than a certainty.

    Never raises: an unresolvable path is treated as NOT OneDrive (fails
    open for this READ-ONLY detector -- the fail-CLOSED behavior lives in
    :func:`assert_disk_only_prestage_path`, which refuses to proceed when
    the check itself couldn't be completed confidently, not here).
    """
    if not path or not str(path).strip():
        return False
    try:
        normalized = os.path.normcase(os.path.abspath(str(path)))
    except (OSError, ValueError):
        normalized = os.path.normcase(str(path))

    for root in _onedrive_roots():
        try:
            root_norm = os.path.normcase(os.path.abspath(root))
        except (OSError, ValueError):
            root_norm = os.path.normcase(root)
        if normalized == root_norm or normalized.startswith(root_norm + os.sep):
            return True

    parts = re.split(r"[\\/]+", str(path))
    return any(_ONEDRIVE_SEGMENT_RE.match(part) for part in parts if part)


def assert_disk_only_prestage_path(path: str) -> dict[str, Any]:
    """Fail-closed guard for a prestage/draft/temp artifact's destination.

    Args:
      path:  The path a caller is about to write a temporary or draft
             artifact to (a file path, or a directory it will live under).

    Returns:
      ``{"allowed": bool, "path": path, "reason": str | None}``.
      ``allowed=False`` whenever ``path`` is empty/blank, OR resolves as a
      OneDrive path per :func:`is_onedrive_path` -- with an explicit,
      actionable ``reason`` either way. Never raises.
    """
    if not path or not str(path).strip():
        return {
            "allowed": False, "path": path,
            "reason": "path is required -- refusing to guess a prestage destination",
        }
    if is_onedrive_path(path):
        return {
            "allowed": False, "path": path,
            "reason": (
                f"{path!r} resolves under a OneDrive-synced location -- "
                "temporary and draft artifacts must never be placed there "
                "(a partial/torn write can sync mid-write, and a draft can "
                "leak into a shared cloud folder). Choose a local-disk-only "
                "path instead."
            ),
        }
    return {"allowed": True, "path": path, "reason": None}


# ---------------------------------------------------------------------------
# Bounded quotas -- exhaustion must degrade VISIBLY, never silently.
# ---------------------------------------------------------------------------

def _walk_size(root: str) -> tuple[int, int, list[str]]:
    """Returns ``(total_bytes, file_count, unreadable_paths)``. Never raises
    -- a single unreadable file/dir is skipped and recorded, the walk
    continues."""
    total = 0
    count = 0
    unreadable: list[str] = []
    for dirpath, _dirnames, filenames in os.walk(root, onerror=lambda e: unreadable.append(str(e))):
        for name in filenames:
            fpath = os.path.join(dirpath, name)
            try:
                total += os.path.getsize(fpath)
                count += 1
            except OSError:
                unreadable.append(fpath)
    return total, count, unreadable


def check_local_quota(
    root: str, *, max_bytes: int, max_files: int | None = None,
) -> dict[str, Any]:
    """Read-only quota REPORT for a local prestage/temp/cache directory tree.

    Args:
      root:        Directory to measure (recursive).
      max_bytes:    The configured byte budget for ``root``.
      max_files:    Optional configured file-count budget.

    Returns:
      ``{"root", "exists", "used_bytes", "used_files", "max_bytes",
      "max_files", "exceeded", "reason", "unreadable_count"}``.
      ``exceeded`` is ``True`` when ``used_bytes > max_bytes`` OR (when
      ``max_files`` is given) ``used_files > max_files``. A missing
      ``root`` reports ``exists=False``, ``used_bytes=0``, ``exceeded=
      False`` (nothing is using any quota yet -- not an error). Never
      raises: this is a pure read, so it fails OPEN on an unreadable root
      (reports what it could measure, plus ``unreadable_count`` > 0) rather
      than refusing to answer -- the fail-CLOSED behavior belongs to
      :func:`enforce_local_quota`, the WRITE-GATING wrapper below.
    """
    if not root or not os.path.isdir(root):
        return {
            "root": root, "exists": False, "used_bytes": 0, "used_files": 0,
            "max_bytes": max_bytes, "max_files": max_files, "exceeded": False,
            "reason": None, "unreadable_count": 0,
        }
    used_bytes, used_files, unreadable = _walk_size(root)
    exceeded = used_bytes > max_bytes or (max_files is not None and used_files > max_files)
    reason = None
    if exceeded:
        reason = (
            f"local quota exceeded for {root!r}: {used_bytes} bytes "
            f"(budget {max_bytes}), {used_files} files"
            + (f" (budget {max_files})" if max_files is not None else "")
        )
    return {
        "root": root, "exists": True, "used_bytes": used_bytes, "used_files": used_files,
        "max_bytes": max_bytes, "max_files": max_files, "exceeded": exceeded,
        "reason": reason, "unreadable_count": len(unreadable),
    }


def enforce_local_quota(
    root: str, *, max_bytes: int, max_files: int | None = None,
) -> dict[str, Any]:
    """WRITE-GATING wrapper around :func:`check_local_quota`: call this
    BEFORE writing a new prestage/draft/temp file under ``root``.

    Returns the same fields as :func:`check_local_quota` plus ``allowed``
    (``not exceeded``) -- ``allowed=False`` means "do not write; the quota
    is exhausted" and is the explicit, VISIBLE degraded-state signal this
    item's acceptance criteria require (never a silent drop, never letting
    disk fill up unbounded). Never raises.
    """
    status = check_local_quota(root, max_bytes=max_bytes, max_files=max_files)
    return {**status, "allowed": not status["exceeded"]}


# ---------------------------------------------------------------------------
# Durable JSON ledger (temp-run manifests + cleanup receipts), atomic write.
# ---------------------------------------------------------------------------

_MANIFEST_FILENAME = "local_resilience_runs.json"
_write_lock = threading.Lock()


def _manifest_path(manifest_dir: str) -> str:
    return os.path.join(manifest_dir, _MANIFEST_FILENAME)


def _read_ledger(manifest_dir: str) -> dict[str, Any]:
    path = _manifest_path(manifest_dir)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {"runs": {}, "receipts": []}
    if not isinstance(data, dict):
        return {"runs": {}, "receipts": []}
    data.setdefault("runs", {})
    data.setdefault("receipts", [])
    if not isinstance(data["runs"], dict):
        data["runs"] = {}
    if not isinstance(data["receipts"], list):
        data["receipts"] = []
    return data


def _write_ledger(manifest_dir: str, data: dict[str, Any]) -> None:
    os.makedirs(manifest_dir, exist_ok=True)
    path = _manifest_path(manifest_dir)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
    os.replace(tmp, path)  # atomic on both POSIX and Windows


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append_receipt(manifest_dir: str, receipt: dict[str, Any]) -> dict[str, Any]:
    """Append one AUDITABLE cleanup/lifecycle receipt to the same ledger a
    run's own manifest lives in -- every action this module takes on a run
    (start/complete/fail/resolve/kill/quarantine) is durably recorded here,
    never only reflected in an in-memory return value."""
    receipt = {"receipt_id": str(uuid.uuid4()), "at": _utcnow_iso(), "at_epoch": time.time(), **receipt}
    with _write_lock:
        data = _read_ledger(manifest_dir)
        data["receipts"].append(receipt)
        _write_ledger(manifest_dir, data)
    return receipt


def list_cleanup_receipts(manifest_dir: str, run_id: str | None = None) -> list[dict[str, Any]]:
    """All auditable receipts recorded in ``manifest_dir``'s ledger, oldest
    first, optionally filtered to one ``run_id``. ``[]`` if nothing has
    been recorded yet. Never raises."""
    data = _read_ledger(manifest_dir)
    rows = list(data["receipts"])
    if run_id is not None:
        rows = [r for r in rows if r.get("run_id") == run_id]
    return sorted(rows, key=lambda r: r.get("at_epoch") or 0.0)


# ---------------------------------------------------------------------------
# Temp-run manifests + restart scavenging.
# ---------------------------------------------------------------------------

def start_temp_run(
    manifest_dir: str,
    *,
    kind: str,
    owner_pid: int,
    resumable: bool,
    process_name: str | None = None,
    temp_paths: "list[str] | None" = None,
    metadata: "dict[str, Any] | None" = None,
) -> dict[str, Any]:
    """Durably record that a risky local operation is STARTING, before it
    actually starts -- so a crash mid-operation is detectable on the next
    process start via :func:`scan_interrupted_runs`.

    Args:
      manifest_dir:   Local directory the run ledger lives under (created if
                      needed). Never call :func:`assert_disk_only_prestage_
                      path` FOR you -- pass a local-disk path; callers that
                      want the guard enforced should check it themselves
                      first, since this function's own job is just durable
                      recording, not policy.
      kind:            Free-text operation kind, e.g. ``"word_com_render"``,
                      ``"soffice_render"``, ``"docx_draft_write"``.
      owner_pid:       This run's owning process id (``os.getpid()`` in the
                      common case).
      resumable:       Whether re-attempting THIS SAME logical operation
                      from scratch is safe/idempotent if it turns out to
                      have been interrupted (e.g. a read-only render is
                      always resumable; a partially-written draft usually
                      is NOT). Drives :func:`resolve_interrupted_run`'s
                      deterministic resume-vs-quarantine outcome.
      process_name:    Optional name of a CHILD process this run itself
                      spawns and owns (e.g. ``"WINWORD.EXE"``) -- recorded
                      so a crash-orphaned child can be identified and
                      cleaned by :func:`resolve_interrupted_run` /
                      :func:`terminate_owned_process`.
      temp_paths:      Optional list of temp/draft file or directory paths
                      this run is responsible for.
      metadata:        Opaque caller-supplied extra fields.

    Returns:
      The stored run record: ``run_id``, ``kind``, ``owner_pid``,
      ``resumable``, ``process_name``, ``temp_paths``, ``metadata``,
      ``status`` (``"started"``), ``started_at``/``started_at_epoch``,
      ``ended_at``/``ended_at_epoch`` (``None`` until completed/failed/
      resolved).

    Raises:
      LocalResilienceError: ``kind`` or ``owner_pid`` missing/invalid.
    """
    if not kind or not str(kind).strip():
        raise LocalResilienceError("start_temp_run: kind is required")
    try:
        owner_pid = int(owner_pid)
    except (TypeError, ValueError) as exc:
        raise LocalResilienceError(f"start_temp_run: invalid owner_pid {owner_pid!r}") from exc

    now = time.time()
    run: dict[str, Any] = {
        "run_id": str(uuid.uuid4()),
        "kind": str(kind).strip(),
        "owner_pid": owner_pid,
        "resumable": bool(resumable),
        "process_name": process_name,
        "temp_paths": list(temp_paths or []),
        "metadata": dict(metadata or {}),
        "status": RUN_STARTED,
        "started_at": _utcnow_iso(),
        "started_at_epoch": now,
        "ended_at": None,
        "ended_at_epoch": None,
    }
    with _write_lock:
        data = _read_ledger(manifest_dir)
        data["runs"][run["run_id"]] = run
        _write_ledger(manifest_dir, data)
    _append_receipt(manifest_dir, {"run_id": run["run_id"], "action": "started", "kind": run["kind"]})
    return run


def _end_temp_run(manifest_dir: str, run_id: str, *, status: str, detail: str | None) -> dict[str, Any]:
    with _write_lock:
        data = _read_ledger(manifest_dir)
        run = data["runs"].get(run_id)
        if run is None:
            raise LocalResilienceError(f"no temp run with id {run_id!r} in {manifest_dir!r}")
        now = time.time()
        run["status"] = status
        run["ended_at"] = _utcnow_iso()
        run["ended_at_epoch"] = now
        if detail:
            run["end_detail"] = detail
        data["runs"][run_id] = run
        _write_ledger(manifest_dir, data)
    _append_receipt(manifest_dir, {"run_id": run_id, "action": status, "detail": detail})
    return run


def complete_temp_run(manifest_dir: str, run_id: str, *, detail: str | None = None) -> dict[str, Any]:
    """Mark a run COMPLETED (succeeded normally) -- the ordinary happy path,
    called once the risky operation finishes cleanly. Raises
    :class:`LocalResilienceError` if ``run_id`` is unknown."""
    return _end_temp_run(manifest_dir, run_id, status=RUN_COMPLETED, detail=detail)


def fail_temp_run(manifest_dir: str, run_id: str, *, detail: str | None = None) -> dict[str, Any]:
    """Mark a run FAILED (the operation ran to completion but reported a
    real failure, as opposed to being interrupted by a crash -- the caller
    is still alive and already knows the outcome). Raises
    :class:`LocalResilienceError` if ``run_id`` is unknown."""
    return _end_temp_run(manifest_dir, run_id, status=RUN_FAILED, detail=detail)


def get_temp_run(manifest_dir: str, run_id: str) -> "dict[str, Any] | None":
    data = _read_ledger(manifest_dir)
    run = data["runs"].get(run_id)
    return dict(run) if run is not None else None


def list_temp_runs(manifest_dir: str, *, status: str | None = None) -> list[dict[str, Any]]:
    """All runs on file, newest-started first, optionally filtered by
    ``status``. ``[]`` if the ledger doesn't exist yet. Never raises."""
    data = _read_ledger(manifest_dir)
    rows = list(data["runs"].values())
    if status is not None:
        rows = [r for r in rows if r.get("status") == status]
    return sorted(rows, key=lambda r: r.get("started_at_epoch") or 0.0, reverse=True)


def _pid_is_alive(pid: int) -> bool:
    """Same catch tuple as ``worktree_cleanup._pid_is_alive`` /
    ``orphan_reaper`` -- ONE liveness-check convention across this repo."""
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    return True


def scan_interrupted_runs(
    manifest_dir: str, *, pid_alive: "Callable[[int], bool] | None" = None,
) -> dict[str, Any]:
    """RESTART SCAVENGING, read-only: every run still ``"started"`` whose
    ``owner_pid`` is no longer alive is a run that was interrupted by its
    owning process dying (crash/kill/power-loss) rather than reaching its
    own ``complete_temp_run``/``fail_temp_run`` call.

    Pure detection -- never mutates the ledger or touches any file/process
    beyond a liveness probe. Call this once at process/service startup;
    feed each result into :func:`resolve_interrupted_run` to act on it.

    Args:
      manifest_dir:  The same directory :func:`start_temp_run` was called
                    against.
      pid_alive:      Injectable liveness check (for tests); defaults to a
                    real ``os.kill(pid, 0)`` probe.

    Returns:
      ``{"manifest_dir", "checked", "interrupted": [...run dicts...]}``.
    """
    alive = pid_alive or _pid_is_alive
    started = list_temp_runs(manifest_dir, status=RUN_STARTED)
    interrupted = [run for run in started if not alive(run["owner_pid"])]
    return {"manifest_dir": manifest_dir, "checked": len(started), "interrupted": interrupted}


def terminate_owned_process(
    pid: int,
    *,
    expected_name: "str | None" = None,
    process_name_for_pid: "Callable[[int], str | None] | None" = None,
    kill_signal: int = signal.SIGTERM,
) -> dict[str, Any]:
    """Best-effort, IDENTITY-CHECKED termination of exactly ONE owned
    process -- never a sweep. Mirrors
    ``render_gate._terminate_owned_process``'s exact primitive
    (``os.kill(pid, signal.SIGTERM)``), generalized with an optional
    name-verification step so a caller with a way to look up a live
    process's name (e.g. via ``psutil``, injected -- this module has NO
    hard ``psutil`` dependency) can refuse to touch a pid that has been
    reused by the OS for an unrelated process since it was recorded --
    the same PID-reuse hazard ``orphan_reaper.py``'s own identity-matching
    guards against.

    Args:
      pid:                    The process id to terminate.
      expected_name:           If given (and ``process_name_for_pid`` is
                              also given), the CURRENT process at ``pid``
                              must resolve to this name (case-insensitive
                              substring match) or termination is refused
                              (``"identity_mismatch"``) rather than risking
                              killing an unrelated, PID-reused process.
      process_name_for_pid:    Injectable ``pid -> name|None`` lookup. When
                              omitted, no name verification is possible and
                              this function falls back to a pid-liveness-only
                              guard (matching ``worktree_cleanup``'s own
                              fail-open-on-absent-data posture for PID
                              checks -- only a KNOWN mismatch blocks, never
                              an unknowable one).
      kill_signal:              Defaults to ``SIGTERM`` (graceful); pass
                              ``SIGKILL`` for a forced kill.

    Returns:
      ``{"pid", "terminated": bool, "reason": str}``. Never raises.
    """
    if not _pid_is_alive(pid):
        return {"pid": pid, "terminated": False, "reason": "already_gone"}
    if expected_name and process_name_for_pid is not None:
        try:
            current_name = process_name_for_pid(pid)
        except Exception as exc:  # noqa: BLE001 -- a raising lookup must not crash the caller
            current_name = None
            _lookup_error = str(exc)
        else:
            _lookup_error = None
        if current_name is not None and expected_name.lower() not in current_name.lower():
            return {
                "pid": pid, "terminated": False, "reason": "identity_mismatch",
                "detail": f"expected name containing {expected_name!r}, found {current_name!r}",
            }
        if current_name is None and _lookup_error:
            return {
                "pid": pid, "terminated": False, "reason": "identity_unknown",
                "detail": f"process_name_for_pid raised: {_lookup_error}",
            }
    try:
        os.kill(pid, kill_signal)
    except OSError as exc:
        return {"pid": pid, "terminated": False, "reason": f"kill failed: {exc}"}
    return {"pid": pid, "terminated": True, "reason": "signalled"}


def resolve_interrupted_run(
    manifest_dir: str,
    run_id: str,
    *,
    quarantine_root: "str | None" = None,
    ownership_check: "Callable[[str], dict[str, Any]] | None" = None,
    process_name_for_pid: "Callable[[int], str | None] | None" = None,
) -> dict[str, Any]:
    """Deterministic resolution for ONE run :func:`scan_interrupted_runs`
    found interrupted -- the crash-recovery counterpart to a normal
    ``complete_temp_run``/``fail_temp_run``.

    Two independent steps, both best-effort and both recorded as auditable
    receipts regardless of outcome:

      1. **Process cleanup.** If the run recorded ``process_name`` (a child
         process it owned), attempt to terminate it via
         :func:`terminate_owned_process` -- identity-checked when
         ``process_name_for_pid`` is supplied, pid-liveness-guarded either
         way. A run with no ``process_name`` skips this step entirely
         (nothing to clean).
      2. **Temp-artifact disposition.** Deterministic, based on the run's
         OWN ``resumable`` flag recorded at :func:`start_temp_run` time --
         never a heuristic:

           - ``resumable=True`` -- resolved as ``"resolved_resume"``: the
             operation is safe to simply retry from scratch; ``temp_paths``
             are left untouched (a resumable operation's own next attempt
             is expected to overwrite/regenerate them).
           - ``resumable=False`` -- resolved as ``"resolved_quarantine"``:
             ``temp_paths`` are handed to
             ``worktree_cleanup.build_quarantine_manifest`` +
             ``worktree_cleanup.quarantine_temp_outputs`` (the SAME
             reversible archive-move primitives that module already
             provides -- never re-implemented here), gated by the SAME
             ``ownership_check`` fail-closed contract: omitting it means
             nothing is quarantined (recorded as
             ``quarantine_skipped_no_ownership_check``), never a silent
             guess.

    The run is ALWAYS marked resolved in the ledger (one of
    :data:`RUN_RESOLVED_RESUME` / :data:`RUN_RESOLVED_QUARANTINE|`) so a
    repeated scavenging pass never re-triggers the same resolution forever.

    Returns:
      ``{"run_id", "action" ("resume"|"quarantine"), "process_cleanup":
      {...} | None, "quarantine_result": {...} | None, "run": <updated run
      dict>}`` -- ``run["status"]`` is one of :data:`RUN_RESOLVED_RESUME` /
      :data:`RUN_RESOLVED_QUARANTINE`.

    Raises:
      LocalResilienceError: ``run_id`` is unknown, or the run is not
      currently ``"started"`` (only an interrupted-but-unresolved run can
      be resolved -- resolving an already-resolved or still-live run is
      refused rather than silently re-running cleanup).
    """
    run = get_temp_run(manifest_dir, run_id)
    if run is None:
        raise LocalResilienceError(f"no temp run with id {run_id!r} in {manifest_dir!r}")
    if run["status"] != RUN_STARTED:
        raise LocalResilienceError(
            f"run {run_id!r} is status={run['status']!r}, not {RUN_STARTED!r} -- "
            "only an interrupted, unresolved run can be resolved"
        )

    process_cleanup: "dict[str, Any] | None" = None
    if run.get("process_name"):
        process_cleanup = terminate_owned_process(
            run["owner_pid"], expected_name=run["process_name"],
            process_name_for_pid=process_name_for_pid,
        )
        _append_receipt(manifest_dir, {
            "run_id": run_id, "action": "process_cleanup", "result": process_cleanup,
        })

    quarantine_result: "dict[str, Any] | None" = None
    if run["resumable"]:
        action = "resume"
        resolved_status = RUN_RESOLVED_RESUME
    else:
        action = "quarantine"
        resolved_status = RUN_RESOLVED_QUARANTINE
        temp_paths = run.get("temp_paths") or []
        if temp_paths and quarantine_root and ownership_check is not None:
            manifest = worktree_cleanup.build_quarantine_manifest(
                temp_paths, archive_root=quarantine_root, ownership_check=ownership_check,
            )
            quarantine_result = worktree_cleanup.quarantine_temp_outputs(manifest)
        else:
            quarantine_result = {
                "moved_count": 0, "skipped_count": len(temp_paths),
                "reason": (
                    "quarantine_skipped_no_ownership_check" if temp_paths
                    else "no_temp_paths_recorded"
                ),
            }
        _append_receipt(manifest_dir, {
            "run_id": run_id, "action": "quarantine", "result": quarantine_result,
        })

    updated = _end_temp_run(manifest_dir, run_id, status=resolved_status, detail=f"restart scavenging: {action}")
    return {
        "run_id": run_id, "action": action, "process_cleanup": process_cleanup,
        "quarantine_result": quarantine_result, "run": updated,
    }


# ---------------------------------------------------------------------------
# Crash-recovery reaper for render_gate.py's own disposable render tempdirs.
#
# extensions/meridian-docs/meridian_docs/render_gate.py's _soffice_render /
# _word_com_render_thread / _word_com_render_isolated each open a
# tempfile.TemporaryDirectory(prefix="meridian_render_gate_") -- self-
# cleaning on a normal Python-level exception, but NOT on a hard process
# kill (the finalizer never runs). This is a duck-typed, no-import
# companion (see module docstring's "no hard cross-package import" note):
# the prefix is a documented STRING CONVENTION, not an imported constant.
# ---------------------------------------------------------------------------

RENDER_TEMPDIR_PREFIX_DEFAULT = "meridian_render_gate_"


def reap_stale_render_tempdirs(
    temp_root: "str | None" = None,
    *,
    prefix: str = RENDER_TEMPDIR_PREFIX_DEFAULT,
    max_age_seconds: float = 3600.0,
    now: "float | None" = None,
) -> dict[str, Any]:
    """Restart-scavenging sweep for orphaned render-backend temp
    directories left behind by a crashed process (see module-section
    docstring above).

    These directories are self-contained, disposable render byproducts
    (never user data -- ``render_gate`` itself always treats them as
    throwaway), so a directory matching ``prefix`` and older than
    ``max_age_seconds`` is removed directly (``shutil.rmtree``) rather than
    quarantined -- the distinctive Meridian-owned prefix IS the ownership
    signal here, the same way ``.meridian-outputs-cache`` is elsewhere in
    this codebase; no injected ``ownership_check`` is needed or requested.

    Args:
      temp_root:         Directory to scan (non-recursive -- only DIRECT
                        children matching ``prefix``). Defaults to
                        ``tempfile.gettempdir()``.
      prefix:             Directory name prefix to match. MUST match
                        ``render_gate``'s own
                        ``RENDER_TEMPDIR_PREFIX``/``tempfile.
                        TemporaryDirectory(prefix=...)`` value -- kept in
                        sync by convention (documented on both sides), not
                        by import (see module docstring).
      max_age_seconds:    Only a directory whose mtime is older than this is
                        removed -- never touches one that might still be a
                        LIVE, in-progress render (crash detection needs a
                        grace period, not an instant sweep).
      now:                Injectable current time (for tests); defaults to
                        ``time.time()``.

    Returns:
      ``{"temp_root", "prefix", "scanned", "removed": [...paths...],
      "skipped": [{"path", "reason"}]}``. Never raises: one directory's
      removal failure is recorded per-entry, the sweep continues.
    """
    import tempfile as _tempfile

    root = temp_root or _tempfile.gettempdir()
    now = time.time() if now is None else now
    removed: list[str] = []
    skipped: list[dict[str, Any]] = []
    scanned = 0

    try:
        entries = os.listdir(root)
    except OSError as exc:
        return {"temp_root": root, "prefix": prefix, "scanned": 0, "removed": [], "skipped": [
            {"path": root, "reason": f"could not list temp_root: {exc}"},
        ]}

    for name in entries:
        if not name.startswith(prefix):
            continue
        scanned += 1
        candidate = os.path.join(root, name)
        if not os.path.isdir(candidate):
            skipped.append({"path": candidate, "reason": "not_a_directory"})
            continue
        try:
            age = now - os.path.getmtime(candidate)
        except OSError as exc:
            skipped.append({"path": candidate, "reason": f"could not stat: {exc}"})
            continue
        if age < max_age_seconds:
            skipped.append({"path": candidate, "reason": f"too_recent (age={age:.1f}s)"})
            continue
        try:
            shutil.rmtree(candidate)
        except OSError as exc:
            skipped.append({"path": candidate, "reason": f"rmtree failed: {exc}"})
            continue
        removed.append(candidate)

    return {"temp_root": root, "prefix": prefix, "scanned": scanned, "removed": removed, "skipped": skipped}


# ---------------------------------------------------------------------------
# Capability-manifest-safe summarization -- local paths never leak into
# shared, project-scoped capability manifest state.
# ---------------------------------------------------------------------------

def summarize_for_capability_manifest(
    *,
    availability_policy: str = "optional",
    verification_command: "str | None" = None,
) -> dict[str, Any]:
    """A ready-to-declare capability-manifest ENTRY for "local resilience is
    available in this environment" -- contains NO local paths, NO run/receipt
    data, by construction (see module docstring's reuse-not-reimplement
    note). Self-validated through the REAL
    ``capability_manifest.normalize_capability`` before being returned, so
    an accidental future edit that added a path-shaped field would raise
    ``capability_manifest.CapabilityManifestError`` here, loudly, rather
    than silently reach a shared manifest write.

    This function deliberately does NOT accept ``manifest_dir``,
    ``temp_root``, or any other local-path parameter -- there is nothing
    for a caller to accidentally pass through into shared state.

    Returns:
      A normalized capability dict, safe to include verbatim in a
      ``set_capability_manifest(capabilities=[...])`` call.
    """
    raw = {
        "id": "local_resilience",
        "purpose": (
            "Durable temp-run manifests, process ownership/cleanup, bounded "
            "quotas, and disk-only prestage hygiene for local operations."
        ),
        "required_tools": ["local-filesystem"],
        "fallback_chain": [],
        "availability_policy": availability_policy,
    }
    if verification_command:
        raw["verification_command"] = verification_command
    return capability_manifest.normalize_capability(raw)
