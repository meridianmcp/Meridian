"""meridian_outputs.run_manifest -- canonical run manifest, bounded-indexing
snapshot, and provenance reconciliation for one Outputs run.

Sprint item 37ce5537.

This module does NOT reimplement anything the sibling modules in this
package already own -- it COMPOSES them behind one run-scoped receipt:

  * :func:`fingerprint.script_content_hash` for every content hash this
    module ever computes (never a second hash scheme).
  * :func:`outputs_local.get_convergence_state` for the walk/index's own
    convergence snapshot (never a second progress tracker).
  * :func:`artifact_registry.get_artifact` to VALIDATE a caller-declared
    artifact id already exists (never a second artifact identity store).
  * :class:`research_evidence.EvidenceRecord` (kind ``RUN`` -- a slot that
    module's own ``EvidenceKind`` enum already reserved but nothing in this
    codebase ever populated) so a run manifest is representable inside the
    SAME lossless JSON/XML envelope every other provenance answer in this
    package already uses -- no second codec, no second Markdown-vs-source-
    of-truth story.

What IS new here: one more atomic-JSON ledger --
``<outputs_dir>/.meridian-outputs-cache/run_manifest_ledger.json`` -- keyed
by ``run_id``, following the exact same convention ``annotate.py``/
``fingerprint.py``/``artifact_registry.py`` already established (same cache
dir, same ``os.replace`` atomic write, same in-process ``RLock``, same
"never raises on I/O, degrade instead" discipline). This is deliberately
the ONLY new piece of durable state this module owns -- every other field
in a run-manifest record is either a caller-supplied identity input or a
REFERENCE (a path, a hash, an id, a ledger location) into state that
already lives somewhere else in this package. No per-path provenance data,
convergence bookkeeping, or artifact record is ever copied into this
ledger wholesale.

Package-boundary note (mirrors ``annotate.py``/``provenance_status.py``):
this package cannot import ``meridian.executor_contract`` (see
``pixi.toml``'s ``52cbe5d8`` note and this package's own module
docstrings) -- it is tested straight off ``sys.path`` as a standalone
extension. :func:`capture_git_state` below is therefore a deliberate,
best-effort LOCAL reimplementation of
``meridian.executor_contract.capture_git_state`` -- kept in exact lockstep
by RETURN SHAPE (``{"head", "dirty_files"}``), never by import, so a caller
that already has an ``executor_contract``-produced manifest can compare
``git_state`` dicts directly. Likewise ``run_manifest_hash`` mirrors
``executor_contract.execution_manifest_hash``'s "sha256 over canonical JSON,
excluding wall-clock/hash/lifecycle fields" discipline -- see
``_HASH_EXCLUDED_KEYS`` for the one deliberate difference (this manifest
carries mutable LIFECYCLE fields -- ``phase``/``output_identity``/
``artifact_ids``/convergence snapshots -- that ``executor_contract``'s own,
fully-static manifest does not, so the exclusion set is broader here; see
that constant's own comment).

A caller that already has an externally-built ``executor_contract``
execution-manifest aggregation can cross-reference it via
``external_manifest_hash`` (stored, never re-derived) and by feeding the
SAME aggregation to :func:`provenance_status.get_manifest_backed_provenance_status`
/ :func:`outputs_local.register_execution_manifest_outputs` directly --
this module does not duplicate that cross-referencing logic either.

NO hosted call is made anywhere in this module -- fully local, matching the
rest of the ``meridian_outputs`` package.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import subprocess
import sys
import threading
from datetime import datetime, timezone
from typing import Any, Callable

from . import __version__ as _PACKAGE_VERSION
from . import artifact_registry, fingerprint, outputs_local, research_evidence

_log = logging.getLogger(__name__)

__all__ = [
    "RunManifestError",
    "capture_git_state",
    "start_run_manifest",
    "finalize_run_manifest",
    "get_run_manifest",
    "list_run_manifests",
    "run_manifest_hash",
    "check_run_manifest_immutable",
    "run_manifest_to_evidence_record",
    "build_run_manifest_envelope",
]

_CACHE_DIRNAME = ".meridian-outputs-cache"
_LEDGER_FILENAME = "run_manifest_ledger.json"
_SCHEMA_VERSION = 1

# In-process only (not cross-process) -- see module docstring: this ledger
# follows the SAME lower-stakes, mostly-single-writer sidecar convention
# annotate.py/fingerprint.py already established. RLock (not Lock): several
# functions below do their own read-modify-write and may be called reentrantly
# from within a caller already holding this lock via a wrapping helper --
# mirrors annotate.py's own fa600e42 fix for the identical hazard.
_write_lock = threading.RLock()

_VALID_START_PHASE = "in_progress"
_VALID_FINAL_STATUSES = ("complete", "failed", "partial")


class RunManifestError(ValueError):
    """Raised for a structurally-invalid run-manifest call: a missing
    required argument, or a same-``run_id``-different-hash identity
    collision. Mirrors this package's other modules' dedicated-exception
    convention (``artifact_registry.RegistryError``,
    ``research_evidence.EnvelopeValidationError``) rather than letting a
    bare ``ValueError``/``KeyError`` escape."""


# ---------------------------------------------------------------------------
# Ledger location + atomic read/write (same convention as annotate.py)
# ---------------------------------------------------------------------------

def _cache_dir(outputs_dir: str) -> str:
    cache_dir = os.path.join(outputs_dir, _CACHE_DIRNAME)
    try:
        os.makedirs(cache_dir, exist_ok=True)
        outputs_local.ensure_gitignored(cache_dir)
    except OSError:
        _log.debug("run_manifest: could not create/gitignore cache dir %r", cache_dir, exc_info=True)
    return cache_dir


def _ledger_path(outputs_dir: str) -> str:
    return os.path.join(_cache_dir(outputs_dir), _LEDGER_FILENAME)


def _read_ledger(outputs_dir: str) -> dict[str, dict[str, Any]]:
    path = _ledger_path(outputs_dir)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_ledger_entry(outputs_dir: str, key: str, record: dict[str, Any]) -> None:
    path = _ledger_path(outputs_dir)
    with _write_lock:
        ledger = _read_ledger(outputs_dir)
        ledger[key] = record
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(ledger, fh, indent=2, sort_keys=True)
        os.replace(tmp, path)  # atomic on both POSIX and Windows


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Best-effort local git-state capture (see module docstring: cannot import
# meridian.executor_contract.capture_git_state across the package boundary).
# ---------------------------------------------------------------------------

def capture_git_state(
    repo_dir: str, *, run: "Callable[[list[str]], str | None] | None" = None,
) -> dict[str, Any]:
    """Best-effort ``{"head": <sha>|None, "dirty_files": [...]}`` for
    ``repo_dir``.

    ``run`` is an injectable ``argv -> stdout|None`` seam (tests stub it; no
    real git binary/repo required to exercise this function) -- same seam
    shape as ``meridian.executor_contract.capture_git_state``, which this
    function otherwise mirrors byte-for-byte in behavior (never in import).
    ANY failure (not a repo, git missing, timeout, non-zero exit) degrades to
    ``{"head": None, "dirty_files": []}`` -- a best-effort identity
    fingerprint, never a hard error blocking manifest construction. Never
    raises.
    """
    def _default_run(argv: list[str]) -> "str | None":
        try:
            result = subprocess.run(
                argv, cwd=repo_dir, capture_output=True, text=True,
                timeout=10, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        return result.stdout

    runner = run or _default_run
    head_out = runner(["git", "rev-parse", "HEAD"])
    head = head_out.strip() if head_out else None
    status_out = runner(["git", "status", "--porcelain"])
    dirty_files: list[str] = []
    if status_out:
        for line in status_out.splitlines():
            line = line.rstrip("\n")
            if len(line) > 3:
                dirty_files.append(line[3:].strip())
    return {"head": head, "dirty_files": sorted(set(f for f in dirty_files if f))}


# ---------------------------------------------------------------------------
# Canonical hashing (same discipline as meridian.executor_contract:
# sort_keys, compact separators, no wall-clock fields inside the payload)
# ---------------------------------------------------------------------------

def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _hash_paths(paths: "list[str] | None") -> "dict[str, str | None]":
    """sha256 of each path's CURRENT on-disk bytes, keyed by the path as
    given, via :func:`fingerprint.script_content_hash` -- the SAME hasher
    this package already uses everywhere else (never a second scheme).
    ``None`` for a missing/unreadable path is a valid state (e.g. a
    not-yet-produced output), never an error. Never raises."""
    out: "dict[str, str | None]" = {}
    for raw in paths or []:
        p = str(raw)
        out[p] = fingerprint.script_content_hash(p)
    return out


def _file_set_hash(hashes: "dict[str, str | None]") -> str:
    """ONE deterministic sha256 over a whole ``{path: hash}`` set -- dict
    keys are sorted before hashing, so caller insertion order never
    matters. Mirrors ``meridian.executor_contract.aggregate_file_set_hash``
    exactly (independently implemented, package boundary -- see module
    docstring)."""
    return hashlib.sha256(_canonical_json(dict(sorted(hashes.items()))).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Bounds snapshot -- "what limits were ACTUALLY in effect for this run"
# ---------------------------------------------------------------------------

def _snapshot_bounds() -> dict[str, Any]:
    """Best-effort snapshot of the walk/index bounds actually in effect
    right now (max worker count, walk drain cap, adaptive batch/fts/write
    thresholds, Tantivy writer heap, DuckDB memory limit) -- reads
    ``outputs_local``'s OWN resolution helpers (module-private, same
    package, same convention ``annotate.py`` already relies on for
    ``outputs_local.file_fingerprint``) rather than re-deriving the
    precedence rules a second time.

    Every field degrades independently to ``None`` on any lookup failure
    (e.g. a future refactor renames/removes one of these internals) --
    never raises, never blocks manifest construction on a bounds field
    that could not be read.
    """
    bounds: dict[str, Any] = {
        "max_workers": None,
        "max_batch": None,
        "adaptive_max_batch": None,
        "adaptive_max_fts_seconds": None,
        "adaptive_max_write_seconds": None,
        "tantivy_heap_bytes": None,
        "duckdb_memory_limit_bytes": None,
    }
    try:
        bounds["max_workers"] = outputs_local._resolve_max_workers(None)
    except Exception:  # noqa: BLE001 -- best-effort snapshot only
        _log.debug("run_manifest: could not resolve max_workers", exc_info=True)
    try:
        bounds["max_batch"] = outputs_local._ResumableFileWalk._resolve_max_batch(None)
    except Exception:  # noqa: BLE001
        _log.debug("run_manifest: could not resolve max_batch", exc_info=True)
    try:
        bounds["adaptive_max_batch"] = outputs_local.OutputsFtsIndex._ADAPTIVE_MAX_BATCH
        bounds["adaptive_max_fts_seconds"] = outputs_local.OutputsFtsIndex._ADAPTIVE_MAX_FTS_SECONDS
        bounds["adaptive_max_write_seconds"] = outputs_local.OutputsFtsIndex._ADAPTIVE_MAX_WRITE_SECONDS
    except Exception:  # noqa: BLE001
        _log.debug("run_manifest: could not read adaptive thresholds", exc_info=True)
    try:
        tantivy_heap = outputs_local._default_tantivy_heap_bytes()
        bounds["tantivy_heap_bytes"] = tantivy_heap
        bounds["duckdb_memory_limit_bytes"] = outputs_local._resolve_duckdb_memory_limit_bytes(None, tantivy_heap)
    except Exception:  # noqa: BLE001
        _log.debug("run_manifest: could not resolve memory bounds", exc_info=True)
    return bounds


def _safe_convergence(outputs_dir: str) -> "dict[str, Any] | None":
    """Best-effort :func:`outputs_local.get_convergence_state` snapshot --
    ``None`` (not an error) when it cannot be read (e.g. ``outputs_dir``
    does not exist yet at manifest-start time). Never triggers a rebuild,
    never raises."""
    try:
        state = outputs_local.get_convergence_state(outputs_dir)
    except Exception:  # noqa: BLE001
        _log.debug("run_manifest: could not read convergence state", exc_info=True)
        return None
    if isinstance(state, dict) and "error" in state:
        return None
    return state


# ---------------------------------------------------------------------------
# Identity hash -- deliberately excludes every LIFECYCLE/OUTCOME field, not
# just wall-clock fields, because (unlike executor_contract's fully-static
# manifest) a record here is mutated in place across start -> finalize.
# Excluding these keeps manifest_hash a stable IDENTITY fingerprint for the
# run's whole lifetime: finalize_run_manifest never changes it.
# ---------------------------------------------------------------------------

_HASH_EXCLUDED_KEYS = (
    "created_at",
    "updated_at",
    "manifest_hash",
    "phase",
    "output_identity",
    "artifact_ids",
    "unknown_artifact_ids",
    "convergence_at_start",
    "convergence_at_finish",
    "status_reason",
    # bd5b8d79-style live-system observation, not a caller-declared identity
    # input: _snapshot_bounds()'s duckdb_memory_limit_bytes is derived from
    # psutil.virtual_memory().available, checked FRESH at call time -- on a
    # busy machine this can differ between two calls microseconds apart with
    # otherwise IDENTICAL identity inputs (confirmed live: this is exactly
    # what broke the first version of this module's own
    # test_deterministic_hash_same_inputs). "bounds" as a whole is excluded
    # (not just this one field) so a future bounds field with the same
    # live-observation character doesn't silently reintroduce the same
    # class of nondeterminism -- it is recorded for audit on every call, but
    # never treated as part of what makes two runs "the same identity".
    "bounds",
)


def run_manifest_hash(manifest: dict[str, Any]) -> str:
    """Stable sha256 over ``manifest``'s DETERMINISTIC identity fields --
    excludes wall-clock timestamps and every mutable lifecycle/outcome field
    (see ``_HASH_EXCLUDED_KEYS``). Two calls to :func:`start_run_manifest`
    with identical identity inputs on unchanged repo/runtime state always
    produce the same hash (the "deterministic repeated manifest" acceptance
    criterion); :func:`finalize_run_manifest` never changes it."""
    payload = {k: v for k, v in manifest.items() if k not in _HASH_EXCLUDED_KEYS}
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def check_run_manifest_immutable(
    existing_manifest: "dict[str, Any] | None", new_manifest: dict[str, Any],
) -> "tuple[bool, str | None]":
    """Fail-closed precondition, mirroring
    ``meridian.executor_contract.check_manifest_immutable`` exactly (see
    that function's docstring -- reimplemented locally per this module's
    package-boundary note, not imported).

    Returns ``(True, None)`` when there is no existing manifest yet, or the
    existing one's ``manifest_hash`` matches ``new_manifest``'s (an
    idempotent re-call is always safe). Returns ``(False, reason)`` when a
    DIFFERENT manifest already exists for this ``run_id`` -- a caller must
    never silently overwrite it; a genuinely different run needs a NEW
    ``run_id``.
    """
    if existing_manifest is None:
        return True, None
    if existing_manifest.get("run_id") != new_manifest.get("run_id"):
        return False, (
            "existing_manifest is for a different run_id -- "
            "check_run_manifest_immutable must be called with the prior "
            "manifest for the SAME run, not an unrelated one"
        )
    existing_scope = existing_manifest.get("scope") or {}
    new_scope = new_manifest.get("scope") or {}
    if (
        existing_scope.get("project_id") != new_scope.get("project_id")
        or existing_scope.get("version") != new_scope.get("version")
    ):
        return False, (
            "existing_manifest is for a different project_id/version scope "
            "under the SAME run_id -- this should never happen; investigate "
            "before proceeding"
        )
    if existing_manifest.get("manifest_hash") == new_manifest.get("manifest_hash"):
        return True, None
    return False, (
        f"a run manifest already exists for run_id={new_manifest.get('run_id')!r} "
        f"with a DIFFERENT hash ({existing_manifest.get('manifest_hash')!r} != "
        f"{new_manifest.get('manifest_hash')!r}) -- run manifests are immutable "
        "per run; use a NEW run_id instead of mutating this one"
    )


# ---------------------------------------------------------------------------
# Lifecycle: start (in_progress, resumable partial receipt) -> finalize
# ---------------------------------------------------------------------------

def start_run_manifest(
    outputs_dir: str,
    *,
    run_id: str,
    command_name: str,
    command_args: "dict[str, Any] | None" = None,
    project_id: "str | None" = None,
    version: "str | None" = None,
    sprint_item_id: "str | None" = None,
    repo_dir: "str | None" = None,
    input_paths: "list[str] | None" = None,
    expected_counts: "dict[str, int] | None" = None,
    allow_partial: bool = False,
    external_manifest_hash: "str | None" = None,
    _git_runner: "Callable[[list[str]], str | None] | None" = None,
) -> dict[str, Any]:
    """Persist an in-progress run-manifest receipt for ``run_id``.

    Binds, in ONE place: project/repo identity (``project_id``/``version``/
    ``sprint_item_id`` plus a best-effort ``capture_git_state`` of
    ``repo_dir``), package/tool version (``meridian_outputs.__version__``,
    the interpreter version, platform), command identity (``command_name``
    + ``command_args``), input hashes (:func:`fingerprint.script_content_hash`
    over ``input_paths``), the bounds actually in effect
    (:func:`_snapshot_bounds`), the on-disk location of every ledger this
    run can reference (this module's own, plus the provenance/fingerprint/
    artifact-registry ledgers this package already maintains), and a
    convergence-state snapshot taken right now.

    Idempotent: calling this again with identical identity inputs (same
    ``run_id`` and everything :func:`run_manifest_hash` covers) returns the
    ALREADY-PERSISTED record unchanged -- including a prior ``finalize``'s
    outcome, if one already ran -- rather than clobbering it with a fresh
    ``in_progress`` skeleton.

    Raises:
      RunManifestError: ``outputs_dir``/``run_id``/``command_name`` missing,
      an ``expected_counts`` value that isn't a non-negative int, or a
      manifest already exists for this ``run_id`` with a DIFFERENT identity
      hash (see :func:`check_run_manifest_immutable`) -- fails closed rather
      than silently overwriting a different run's identity.
    """
    if not outputs_dir or not str(outputs_dir).strip():
        raise RunManifestError("outputs_dir is required")
    if not run_id or not str(run_id).strip():
        raise RunManifestError("run_id is required")
    if not command_name or not str(command_name).strip():
        raise RunManifestError("command_name is required")

    expected_counts_normalized: dict[str, int] = {}
    for k, v in (expected_counts or {}).items():
        if not isinstance(v, int) or isinstance(v, bool) or v < 0:
            raise RunManifestError(
                f"expected_counts[{k!r}] must be a non-negative int, got {v!r}"
            )
        expected_counts_normalized[str(k)] = v

    input_hashes = _hash_paths(input_paths)
    git_state = (
        capture_git_state(repo_dir, run=_git_runner)
        if repo_dir else {"head": None, "dirty_files": []}
    )
    cache_dir = _cache_dir(outputs_dir)

    manifest: dict[str, Any] = {
        "schema_version": _SCHEMA_VERSION,
        "run_id": str(run_id).strip(),
        "scope": {
            "project_id": project_id,
            "version": version,
            "sprint_item_id": sprint_item_id,
        },
        "command_identity": {
            "tool_name": str(command_name).strip(),
            "args": dict(command_args or {}),
        },
        "package_identity": {
            "name": "meridian_outputs",
            "version": _PACKAGE_VERSION,
            "python_version": sys.version.split()[0],
            "platform": platform.platform(),
        },
        "repo_identity": {"repo_dir": repo_dir, **git_state},
        "input_identity": {
            "file_hashes": dict(sorted(input_hashes.items())),
            "file_set_hash": _file_set_hash(input_hashes),
        },
        "bounds": _snapshot_bounds(),
        "ledger_locations": {
            "run_manifest": _ledger_path(outputs_dir),
            "provenance_ledger": os.path.join(cache_dir, "provenance_ledger.json"),
            "fingerprint_ledger": os.path.join(cache_dir, "fingerprint_ledger.json"),
            "artifact_registry": os.path.join(cache_dir, "artifact_registry.json"),
        },
        "expected_counts": expected_counts_normalized,
        "output_schema_domains": None,
        "allow_partial": bool(allow_partial),
        "external_manifest_hash": external_manifest_hash,
        # -- everything below is lifecycle/outcome state, excluded from the
        # identity hash (see _HASH_EXCLUDED_KEYS) --
        "phase": _VALID_START_PHASE,
        "output_identity": None,
        "artifact_ids": [],
        "unknown_artifact_ids": [],
        "status_reason": None,
        "convergence_at_start": _safe_convergence(outputs_dir),
        "convergence_at_finish": None,
    }
    manifest["manifest_hash"] = run_manifest_hash(manifest)
    now = _utcnow_iso()
    manifest["created_at"] = now
    manifest["updated_at"] = now

    key = manifest["run_id"]
    with _write_lock:
        ledger = _read_ledger(outputs_dir)
        existing = ledger.get(key)
        ok, reason = check_run_manifest_immutable(existing, manifest)
        if not ok:
            raise RunManifestError(reason)
        if existing is not None and existing.get("manifest_hash") == manifest["manifest_hash"]:
            return dict(existing)
        _write_ledger_entry(outputs_dir, key, manifest)
    return dict(manifest)


def finalize_run_manifest(
    outputs_dir: str,
    run_id: str,
    *,
    output_paths: "list[str] | None" = None,
    artifact_ids: "list[str] | None" = None,
    status: str = "complete",
    reason: "str | None" = None,
) -> dict[str, Any]:
    """Bind exact output hashes and artifact-id references to an already-
    started run manifest, and mark its final (or partial/interrupted)
    phase.

    Fail-closed exact output binding: every path in ``output_paths`` is
    RE-HASHED right now via :func:`fingerprint.script_content_hash` --
    never trusts a caller-declared hash. A path that is missing/unreadable
    at finalize time is recorded under ``output_identity.missing_or_unreadable``
    and downgrades an attempted ``status="complete"`` to ``"partial"``
    automatically (never silently reported as a clean, fully-verified
    run). The same fail-closed treatment applies to ``artifact_ids``: each
    one is checked against :func:`artifact_registry.get_artifact` (never
    re-derived or duplicated here) and an id that does not resolve is
    recorded under ``unknown_artifact_ids`` and likewise downgrades
    ``"complete"`` to ``"partial"``.

    Args:
      outputs_dir:    Absolute path to the outputs directory.
      run_id:          The run started via :func:`start_run_manifest`.
      output_paths:    Exact output file paths this run produced.
      artifact_ids:    Public artifact ids (from
                      ``artifact_registry.register_artifact``) this run is
                      claiming credit for -- referenced, never duplicated.
      status:          ``"complete"`` (default), ``"failed"``, or
                      ``"partial"`` -- the caller's own intended verdict,
                      subject to the fail-closed downgrade above.
      reason:          Optional human-readable explanation, preserved
                      alongside any auto-generated fail-closed reason.

    Returns:
      The updated manifest record. ``manifest_hash`` is UNCHANGED from
      :func:`start_run_manifest`'s value -- finalize only ever touches
      lifecycle/outcome fields (see ``_HASH_EXCLUDED_KEYS``), never the
      identity payload.

    Raises:
      RunManifestError: ``outputs_dir``/``run_id`` missing, ``status`` not
      one of the valid values, or no manifest was ever started for this
      ``run_id`` -- finalize never creates a fresh identity on its own.
    """
    if not outputs_dir or not str(outputs_dir).strip():
        raise RunManifestError("outputs_dir is required")
    key = str(run_id).strip() if run_id else ""
    if not key:
        raise RunManifestError("run_id is required")
    if status not in _VALID_FINAL_STATUSES:
        raise RunManifestError(
            f"status must be one of {_VALID_FINAL_STATUSES}, got {status!r}"
        )

    with _write_lock:
        ledger = _read_ledger(outputs_dir)
        existing = ledger.get(key)
        if existing is None:
            raise RunManifestError(
                f"no run manifest found for run_id={run_id!r} under "
                f"{outputs_dir!r} -- call start_run_manifest first (finalize "
                "never creates a fresh identity)"
            )

        output_hashes = _hash_paths(output_paths)
        missing = sorted(p for p, h in output_hashes.items() if h is None)
        output_identity = {
            "file_hashes": dict(sorted(output_hashes.items())),
            "file_set_hash": _file_set_hash(output_hashes),
            "missing_or_unreadable": missing,
        }

        resolved_artifact_ids: list[str] = []
        unknown_artifact_ids: list[str] = []
        for aid in (artifact_ids or []):
            try:
                rec = artifact_registry.get_artifact(outputs_dir, aid)
            except Exception:  # noqa: BLE001 -- treat a lookup failure like "unknown"
                _log.debug("run_manifest: get_artifact failed for %r", aid, exc_info=True)
                rec = None
            if rec is None:
                unknown_artifact_ids.append(aid)
            else:
                resolved_artifact_ids.append(aid)

        effective_status = status
        problems: list[str] = []
        if missing:
            problems.append(f"{len(missing)} output path(s) missing/unreadable at finalize time")
        if unknown_artifact_ids:
            problems.append(
                f"{len(unknown_artifact_ids)} artifact_id(s) not found in the registry: "
                f"{sorted(unknown_artifact_ids)}"
            )
        if problems and effective_status == "complete":
            effective_status = "partial"

        status_reason = reason
        if problems:
            auto_reason = "; ".join(problems)
            status_reason = auto_reason if not status_reason else f"{status_reason} ({auto_reason})"

        updated = dict(existing)
        updated["phase"] = effective_status
        updated["output_identity"] = output_identity
        updated["artifact_ids"] = sorted(set(resolved_artifact_ids))
        updated["unknown_artifact_ids"] = sorted(set(unknown_artifact_ids))
        updated["status_reason"] = status_reason
        updated["convergence_at_finish"] = _safe_convergence(outputs_dir)
        updated["updated_at"] = _utcnow_iso()
        # manifest_hash is an IDENTITY hash fixed at start_run_manifest time
        # -- deliberately untouched here (see _HASH_EXCLUDED_KEYS).
        _write_ledger_entry(outputs_dir, key, updated)
    return dict(updated)


def get_run_manifest(outputs_dir: str, run_id: str) -> "dict[str, Any] | None":
    """Look up the current run-manifest record for ``run_id`` (whatever
    phase it's currently in -- ``in_progress``/``complete``/``failed``/
    ``partial``). ``None`` if nothing was ever started for this id."""
    key = str(run_id).strip() if run_id else ""
    if not key:
        return None
    ledger = _read_ledger(outputs_dir)
    rec = ledger.get(key)
    return dict(rec) if rec is not None else None


def list_run_manifests(outputs_dir: str) -> "list[dict[str, Any]]":
    """Every run-manifest record ever started under ``outputs_dir``, sorted
    by ``run_id`` for deterministic output (matching this package's
    existing no-hidden-dict-order convention)."""
    ledger = _read_ledger(outputs_dir)
    return [dict(ledger[k]) for k in sorted(ledger)]


# ---------------------------------------------------------------------------
# RUN-kind EvidenceRecord bridge (the previously-unused EvidenceKind.RUN slot)
# ---------------------------------------------------------------------------

def run_manifest_to_evidence_record(manifest: dict[str, Any]) -> "research_evidence.EvidenceRecord":
    """Represent one run-manifest ledger record as a ``RUN``-kind
    :class:`research_evidence.EvidenceRecord` -- the slot that module's own
    ``EvidenceKind`` enum already reserved (see its docstring) but nothing
    in this codebase ever populated until now. No new envelope/codec is
    introduced: JSON/XML round-trip, canonical hashing, and the
    Markdown-is-only-a-projection rule all come free via the existing
    envelope machinery once wrapped this way.

    Resolver-status mapping (mirrors ``provenance_status``'s own explicit
    "never let a caller infer status from field presence" discipline):
      * ``phase == "complete"`` AND no missing outputs AND no unknown
        artifact ids -> ``VERIFIED``.
      * ``phase == "in_progress"`` -> ``UNAVAILABLE`` (not yet finalized --
        the resumable partial-receipt case).
      * ``phase == "failed"`` -> ``DEGRADED``.
      * ``phase == "partial"`` (or any forward-compatible unknown phase) ->
        ``AMBIGUOUS``.

    The record is marked ``partial=True`` for every case except a clean
    ``complete`` verdict -- a run manifest that hasn't finished, or that
    finished with unresolved outputs/artifacts, is never presented as
    authoritative. The full manifest dict is preserved verbatim under
    ``attributes`` (structured fields, not a Markdown flattening) so the
    envelope stays the lossless source of truth.
    """
    phase = manifest.get("phase")
    output_identity = manifest.get("output_identity") or {}
    has_missing_outputs = bool(output_identity.get("missing_or_unreadable"))
    has_unknown_artifacts = bool(manifest.get("unknown_artifact_ids"))

    if phase == "complete" and not has_missing_outputs and not has_unknown_artifacts:
        resolver = research_evidence.ResolverState(
            status=research_evidence.ResolverStatus.VERIFIED,
            confidence=0.95,
            reason="run manifest finalized with all declared outputs/artifacts verified",
        )
    elif phase == _VALID_START_PHASE:
        resolver = research_evidence.ResolverState(
            status=research_evidence.ResolverStatus.UNAVAILABLE,
            confidence=0.2,
            reason="run manifest not yet finalized -- interrupted/still-running receipt",
        )
    elif phase == "failed":
        resolver = research_evidence.ResolverState(
            status=research_evidence.ResolverStatus.DEGRADED,
            confidence=0.1,
            reason=manifest.get("status_reason") or "run reported failed",
        )
    else:  # "partial" or an unrecognised forward-compatible phase
        resolver = research_evidence.ResolverState(
            status=research_evidence.ResolverStatus.AMBIGUOUS,
            confidence=0.4,
            reason=manifest.get("status_reason")
            or f"run manifest phase={phase!r} is not a fully-verified complete run",
        )

    partial = not (phase == "complete" and not has_missing_outputs and not has_unknown_artifacts)
    partial_reason = None
    if partial:
        partial_reason = (
            manifest.get("status_reason")
            or f"run manifest phase={phase!r} is not a fully-verified complete run"
        )

    created_at = manifest.get("created_at") or _utcnow_iso()
    updated_at = manifest.get("updated_at") or created_at
    locator = (manifest.get("ledger_locations") or {}).get("run_manifest") or ""

    return research_evidence.EvidenceRecord(
        identity=research_evidence.EvidenceIdentity(
            id=f"run:{manifest.get('run_id')}",
            kind=research_evidence.EvidenceKind.RUN,
            locator=locator,
            label=(manifest.get("command_identity") or {}).get("tool_name"),
        ),
        timestamps=research_evidence.EvidenceTimestamps(
            observed_at=created_at, updated_at=updated_at,
        ),
        resolver=resolver,
        hashes=[
            research_evidence.EvidenceHash(
                algorithm="sha256", value=manifest.get("manifest_hash") or "",
            ),
        ],
        partial=partial,
        partial_reason=partial_reason,
        attributes=dict(manifest),
    )


def build_run_manifest_envelope(
    outputs_dir: str,
    run_id: str,
    *,
    envelope_id: "str | None" = None,
    generated_at: "str | None" = None,
) -> "research_evidence.ProvenanceEnvelope":
    """Build one lossless :class:`research_evidence.ProvenanceEnvelope`
    containing exactly one ``RUN``-kind record for ``run_id`` (via
    :func:`run_manifest_to_evidence_record`).

    Raises:
      RunManifestError: no manifest exists for ``run_id`` under
      ``outputs_dir``.
    """
    manifest = get_run_manifest(outputs_dir, run_id)
    if manifest is None:
        raise RunManifestError(
            f"no run manifest found for run_id={run_id!r} under {outputs_dir!r}"
        )
    record = run_manifest_to_evidence_record(manifest)
    return research_evidence.build_envelope(
        records=[record],
        envelope_id=envelope_id,
        generated_at=generated_at,
        partial=record.partial,
        partial_reason=record.partial_reason,
    )
