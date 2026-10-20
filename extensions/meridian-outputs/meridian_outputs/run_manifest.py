"""meridian_outputs.run_manifest -- canonical run manifest, bounded-indexing
snapshot, and provenance reconciliation for one Outputs run.

Sprint item 37ce5537.

This module does NOT reimplement anything the sibling modules in this
package already own -- it COMPOSES them behind one run-scoped receipt:

  * :func:`fingerprint.script_content_hash` for every content hash this
    module ever computes (never a second hash scheme).
  * :func:`outputs_local.get_convergence_state` for the walk/index's own
    convergence snapshot (never a second progress tracker).
  * the artifact registry's strict reader
    (``artifact_registry._read_registry_strict``) to VALIDATE a
    caller-declared artifact id already exists (never a second artifact
    identity store).
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
import subprocess
import threading
from datetime import datetime, timezone
from typing import Any, Callable

from . import artifact_registry, environment, fingerprint, ledger_io, outputs_local, research_evidence
from .path_security import relative_reference

_log = logging.getLogger(__name__)

__all__ = [
    "RunManifestError",
    "capture_git_state",
    "start_run_manifest",
    "finalize_run_manifest",
    "get_run_manifest",
    "list_run_manifests",
    "check_input_staleness",
    "run_ids_for_output",
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

# PROV-CANONICAL (7d9b8251) -- an in_progress manifest whose updated_at (or
# created_at, if updated_at is somehow absent) is more recent than this many
# seconds ago is treated as genuinely live ("safe to poll/retry" ->
# ResolverStatus.PENDING_RETRY); older than this, it is treated as a stale,
# likely orphaned/crashed receipt (-> ResolverStatus.UNAVAILABLE, unchanged
# from the pre-PROV-CANONICAL behavior). This module has no heartbeat
# mechanism of its own (see module docstring: the ONLY durable state it
# owns is the ledger record itself) -- age-since-last-write is the sole,
# best-effort liveness signal available without inventing a second polling
# channel. Chosen conservatively generous (30 minutes) for a local
# Outputs-indexing run; a caller with a tighter/looser liveness contract
# for its own workload can still read the raw `phase`/`updated_at` fields
# directly and re-derive its own verdict -- this constant only affects the
# EvidenceRecord bridge, never the ledger itself.
_STALE_IN_PROGRESS_SECONDS = 1800.0


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

def _cache_dir(outputs_dir: str, *, create: bool = True) -> str:
    """``<outputs_dir>/.meridian-outputs-cache``, created + gitignored on a
    write. ``create=False`` (every read path) only computes the path: a read
    never creates the directory or edits a ``.gitignore``."""
    cache_dir = os.path.join(outputs_dir, _CACHE_DIRNAME)
    if not create:
        return cache_dir
    try:
        os.makedirs(cache_dir, exist_ok=True)
        outputs_local.ensure_gitignored(cache_dir)
    except OSError:
        _log.debug("run_manifest: could not create/gitignore cache dir %r", cache_dir, exc_info=True)
    return cache_dir


def _ledger_path(outputs_dir: str, *, create: bool = True) -> str:
    return os.path.join(_cache_dir(outputs_dir, create=create), _LEDGER_FILENAME)


def _read_ledger(outputs_dir: str) -> dict[str, dict[str, Any]]:
    path = _ledger_path(outputs_dir, create=False)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _read_ledger_strict(outputs_dir: str) -> dict[str, dict[str, Any]]:
    """Strict read for staleness lookups (:func:`check_input_staleness`,
    :func:`run_ids_for_output`), the writers (:func:`_read_ledger_for_write`)
    and bundle export/import: ``{}`` only for a missing ledger. Bytes that
    are not a JSON object, or a run row that is not an object, raise
    ``ledger_io.LedgerCorruptError`` -- an unreadable ledger is never read
    as "no run covers this output". :func:`_read_ledger` keeps its lenient
    behaviour for plain listings."""
    path = _ledger_path(outputs_dir, create=False)
    ledger = ledger_io.read_ledger_strict(path)
    for record in ledger.values():
        if not isinstance(record, dict):
            kind = "null" if record is None else type(record).__name__
            raise ledger_io.LedgerCorruptError(path, f"a run row is {kind}, not an object")
    return ledger


def _read_ledger_for_write(outputs_dir: str) -> dict[str, dict[str, Any]]:
    """The writers' read, taken under ``_write_lock``. Strict: a corrupt
    ledger is quarantined and ``ledger_io.LedgerCorruptError`` raised
    (``ledger_io.raise_quarantined``), so it is never read as ``{}`` and
    replaced by an entry-only ledger. Continuing from empty (as the
    provenance ledger's writer does) would also drop the per-run
    immutability check :func:`start_run_manifest` makes against the existing
    record. Any other read ``OSError`` propagates; nothing is written."""
    try:
        return _read_ledger_strict(outputs_dir)
    except ledger_io.LedgerCorruptError as exc:
        ledger_io.raise_quarantined(exc)


def _write_ledger_entry(outputs_dir: str, key: str, record: dict[str, Any]) -> None:
    """Read-modify-write one run row. The write is a unique temp file, fsync,
    then ``os.replace`` (``ledger_io.write_ledger_atomic``): a crash can never
    leave a zero-length or truncated ledger, which the strict read would
    refuse for good, and a failed write leaves the previous ledger and no temp
    file behind."""
    path = _ledger_path(outputs_dir)
    with _write_lock:
        ledger = _read_ledger_for_write(outputs_dir)
        ledger[key] = record
        ledger_io.write_ledger_atomic(path, ledger)


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
# Input-change baseline (input_stat) -- see check_input_staleness
# ---------------------------------------------------------------------------

def _resolve_ref(outputs_dir: str, raw: str) -> str:
    """Absolute real path for an input/output reference. A relative reference
    is resolved against ``outputs_dir`` (the package-wide convention, see
    ``annotate.record_provenance``), never the process's working directory."""
    p = os.fspath(raw)
    root = os.path.realpath(os.path.abspath(os.fspath(outputs_dir)))
    candidate = p if os.path.isabs(p) else os.path.join(root, p)
    return os.path.realpath(os.path.abspath(candidate))


def _input_key(outputs_dir: str, resolved: str) -> str:
    """Stable input_stat key: outputs_dir-relative (``../data/x.csv`` for a
    sibling tree), or the absolute path when it is on another volume.
    Case-normalised like the provenance ledger's keys, so two spellings of
    one file share a key."""
    rel = relative_reference(outputs_dir, resolved)
    key = rel if rel is not None else resolved
    return os.path.normcase(os.path.normpath(key)).replace("\\", "/")


def _key_path(outputs_dir: str, key: str) -> str:
    if os.path.isabs(key):
        return key
    root = os.path.realpath(os.path.abspath(os.fspath(outputs_dir)))
    return os.path.normpath(os.path.join(root, key))


def _stat_or_none(path: str) -> "os.stat_result | None":
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st if os.path.isfile(path) else None


def _snapshot_inputs(
    outputs_dir: str, input_paths: "list[str] | None",
) -> "tuple[dict[str, str | None], dict[str, dict[str, Any]]]":
    """``(file_hashes, input_stat)`` for ``input_paths``.

    ``file_hashes`` is identity: it feeds ``manifest_hash``. It is exactly
    what :func:`_hash_paths` has always produced -- keyed by the path as
    given, hashed by :func:`fingerprint.script_content_hash` of that path,
    so a relative input resolves against the process working directory.
    Computing it any other way would change ``manifest_hash`` for a run
    started before ``input_stat`` existed and make its resume fail with a
    DIFFERENT-hash :class:`RunManifestError`.

    ``input_stat`` is the staleness baseline (excluded from the hash): each
    input resolved ONCE by :func:`_resolve_ref` (a relative path against
    ``outputs_dir``, the resolver :func:`check_input_staleness` uses), and
    that one file stat-ed and hashed, under its normalised key as
    ``{exists, size, mtime_ns, sha256}``. The two hashes differ only for a
    relative input whose working-directory and ``outputs_dir`` resolutions
    are different files; staleness always uses the ``input_stat`` one. When
    both resolve to the same file, its hash is reused rather than read
    twice. Each file is stat-ed BEFORE it is hashed: if it changes in
    between, a later check sees a stat mismatch and re-hashes, so a change
    during capture can cost one extra hash but can never be missed.
    """
    file_hashes: "dict[str, str | None]" = {}
    input_stat: dict[str, dict[str, Any]] = {}
    for raw in input_paths or []:
        given = str(raw)
        resolved = _resolve_ref(outputs_dir, given)
        st = _stat_or_none(resolved)
        sha = fingerprint.script_content_hash(resolved) if st is not None else None
        same_file = st is not None and os.path.realpath(os.path.abspath(given)) == resolved
        file_hashes[given] = sha if same_file else fingerprint.script_content_hash(given)
        if st is None:
            entry: dict[str, Any] = {"exists": False, "size": None, "mtime_ns": None, "sha256": None}
        else:
            entry = {"exists": True, "size": st.st_size, "mtime_ns": st.st_mtime_ns, "sha256": sha}
        input_stat[_input_key(outputs_dir, resolved)] = entry
    return file_hashes, dict(sorted(input_stat.items()))


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
    # Per-input size/mtime_ns/sha256 baseline for check_input_staleness. The
    # input identity is already input_identity.file_hashes; a touch that
    # leaves the bytes unchanged must not turn an idempotent re-start into
    # an identity collision.
    "input_stat",
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
    over ``input_paths`` as given, unchanged since this module's first
    version so existing runs keep their identity -- see
    :func:`_snapshot_inputs`), the bounds actually in effect
    (:func:`_snapshot_bounds`), the on-disk location of every ledger this
    run can reference (this module's own, plus the provenance/fingerprint/
    artifact-registry ledgers this package already maintains), and a
    convergence-state snapshot taken right now. ``input_stat`` keeps each
    input's size, mtime_ns and hash -- a relative input resolved against
    ``outputs_dir``, never the working directory -- under an
    outputs_dir-relative key as the baseline for
    :func:`check_input_staleness`; it is excluded from ``manifest_hash``
    (see ``_HASH_EXCLUDED_KEYS``).

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
      ledger_io.LedgerCorruptError: the run-manifest ledger exists but cannot
      be parsed. Its bytes are quarantined (``quarantine_path``) and nothing
      is written: a damaged ledger is never replaced by this one record.
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

    input_hashes, input_stat = _snapshot_inputs(outputs_dir, input_paths)
    git_state = (
        capture_git_state(repo_dir, run=_git_runner)
        if repo_dir else {"head": None, "dirty_files": []}
    )
    cache_dir = _cache_dir(outputs_dir)
    # Same snapshot record_provenance stores as `reproduction`, so the run's
    # package identity and each output's record cannot drift apart. Only the
    # four long-standing fields go into package_identity: adding the
    # dependency versions here would change manifest_hash and make a resumed
    # run that was started before this change look like an identity collision.
    env = environment.capture_environment()

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
            "name": env["package_name"],
            "version": env["package_version"],
            "python_version": env["python_version"],
            "platform": env["platform"],
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
        # Baseline for check_input_staleness (excluded from the hash).
        "input_stat": input_stat,
    }
    manifest["manifest_hash"] = run_manifest_hash(manifest)
    now = _utcnow_iso()
    manifest["created_at"] = now
    manifest["updated_at"] = now

    key = manifest["run_id"]
    with _write_lock:
        ledger = _read_ledger_for_write(outputs_dir)
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
    one is looked up in the artifact registry, read once through its strict
    reader (``artifact_registry._read_registry_strict``; never re-derived
    or duplicated here), and an id that is not registered is recorded under
    ``unknown_artifact_ids`` and likewise downgrades ``"complete"`` to
    ``"partial"``. A registry that cannot be read is an error, never "not
    registered" (see Raises).

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
      ledger_io.LedgerCorruptError: the run-manifest ledger exists but cannot
      be parsed (quarantined, nothing written; see :func:`start_run_manifest`),
      or ``artifact_ids`` is non-empty and the artifact registry exists but
      cannot be parsed (nothing written; the registry is only read, so it is
      not quarantined here).
      OSError: ``artifact_ids`` is non-empty and the artifact registry cannot
      be read; nothing is written.
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
        ledger = _read_ledger_for_write(outputs_dir)
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
        # One strict registry read, only when ids were given. A registry that
        # is corrupt (LedgerCorruptError) or cannot be read (OSError) raises
        # here, before anything is written: read leniently it looked empty,
        # so every id was recorded as not found and a "partial" verdict was
        # persisted for a store failure.
        registered = (
            artifact_registry._read_registry_strict(outputs_dir)["artifacts"]
            if artifact_ids else {}
        )
        for aid in (artifact_ids or []):
            if isinstance(aid, str) and aid in registered:
                resolved_artifact_ids.append(aid)
            else:
                unknown_artifact_ids.append(aid)

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
# Input-change staleness
# ---------------------------------------------------------------------------

_STALE_INPUT_STATUSES = ("changed", "missing", "appeared", "unreadable", "unverifiable")


def _classify_input(
    path: str, base: dict[str, Any], *, strict: bool,
) -> "tuple[str, str, bool]":
    """``(status, reason, hashed)`` for one input against its baseline."""
    st = _stat_or_none(path)
    if not base.get("exists"):
        if st is None:
            return "absent", "absent at run start and still absent", False
        return "appeared", "absent at run start, present now", False
    if st is None:
        return "missing", "present at run start, missing now", False
    if (
        not strict
        and base.get("size") is not None
        and base.get("size") == st.st_size
        and base.get("mtime_ns") == st.st_mtime_ns
    ):
        return "unchanged", "size and mtime unchanged since run start (not re-hashed)", False
    current = fingerprint.script_content_hash(path)
    recorded = base.get("sha256")
    if current is None:
        return "unreadable", "exists but cannot be read now", True
    if recorded is None:
        return "unverifiable", "no content hash was captured at run start", True
    if current != recorded:
        return "changed", "content hash differs from the hash recorded at run start", True
    if strict:
        return "unchanged", "content hash unchanged (strict re-hash)", True
    return "unchanged", "size/mtime changed but content hash unchanged", True


def check_input_staleness(
    outputs_dir: str, run_id: str, *, strict: bool = False,
) -> dict[str, Any]:
    """Have any of ``run_id``'s inputs changed since the run started?

    Compares every input recorded by :func:`start_run_manifest` with the file
    on disk now. Stat first: an input whose size and mtime_ns both match the
    start-time baseline is reported unchanged WITHOUT being read, so a sweep
    over a large tree costs one ``stat`` per input. Only a stat mismatch (or
    ``strict=True``) re-hashes, through the same
    :func:`fingerprint.script_content_hash` used at start. Consequence of the
    default: an edit that keeps both size and mtime_ns (for example a tool
    that restores the timestamp) is missed; ``strict=True`` hashes every
    input and catches it.

    Each input gets a status: ``unchanged``, ``absent`` (absent at start and
    still absent), or one of the stale statuses ``changed``, ``missing``,
    ``appeared`` (absent at start, present now), ``unreadable`` and
    ``unverifiable`` (no start-time hash). Inputs are keyed relative to
    ``outputs_dir`` (absolute when on another volume); a relative input path
    is resolved against ``outputs_dir``, and compared with the
    ``input_stat`` hash :func:`start_run_manifest` took of that same file.
    A manifest written before ``input_stat`` existed has no stat baseline,
    so every input is re-hashed against ``input_identity.file_hashes``
    (``baseline="legacy_hash_only"``), each resolved against ``outputs_dir``.
    Those identity hashes are of the path as given, so a relative input
    started from a working directory other than ``outputs_dir`` can report
    ``changed``/``appeared`` there -- fail closed, never missed.

    Returns ``{run_id, strict, baseline, stale, reason, inputs, stale_inputs,
    stale_outputs, hashed_count}``. ``stale_outputs`` lists the run's
    finalized output paths (as recorded) when any input is stale, else
    ``[]``; it is also ``[]`` for a run that was never finalized.

    Raises:
      RunManifestError: missing ``outputs_dir``/``run_id``, or no manifest
      was ever started for ``run_id``.
      ledger_io.LedgerCorruptError: the run-manifest ledger exists but
      cannot be parsed (strict read) -- never reported as "no manifest".
    """
    if not outputs_dir or not str(outputs_dir).strip():
        raise RunManifestError("outputs_dir is required")
    key = str(run_id).strip() if run_id else ""
    manifest = _read_ledger_strict(outputs_dir).get(key) if key else None
    if manifest is None:
        raise RunManifestError(f"no run manifest found for run_id={run_id!r}")

    baseline = manifest.get("input_stat")
    mode = "stat_and_hash"
    if not isinstance(baseline, dict):
        mode = "legacy_hash_only"
        baseline = {}
        hashes = (manifest.get("input_identity") or {}).get("file_hashes") or {}
        for given, sha in hashes.items():
            key = _input_key(outputs_dir, _resolve_ref(outputs_dir, given))
            baseline[key] = {"exists": sha is not None, "size": None, "mtime_ns": None, "sha256": sha}

    inputs: list[dict[str, Any]] = []
    hashed_count = 0
    for key in sorted(baseline):
        base = baseline[key] if isinstance(baseline[key], dict) else {}
        status, reason, hashed = _classify_input(
            _key_path(outputs_dir, key), base, strict=strict,
        )
        hashed_count += int(hashed)
        inputs.append({"input": key, "status": status, "reason": reason})

    stale_inputs = [i["input"] for i in inputs if i["status"] in _STALE_INPUT_STATUSES]
    outputs = sorted(((manifest.get("output_identity") or {}).get("file_hashes") or {}))
    stale = bool(stale_inputs)
    if not inputs:
        reason = "run recorded no inputs"
    elif stale:
        reason = (
            f"{len(stale_inputs)} of {len(inputs)} input(s) changed since run start: "
            + ", ".join(f"{i['input']} ({i['status']})" for i in inputs if i["input"] in stale_inputs)
        )
    else:
        reason = f"all {len(inputs)} input(s) unchanged since run start"
    return {
        "run_id": manifest.get("run_id"),
        "strict": bool(strict),
        "baseline": mode,
        "stale": stale,
        "reason": reason,
        "inputs": inputs,
        "stale_inputs": stale_inputs,
        "stale_outputs": outputs if stale else [],
        "hashed_count": hashed_count,
    }


def run_ids_for_output(
    outputs_dir: str, path: str, *, record_run_id: "str | None" = None,
) -> "list[str]":
    """Run ids whose manifest covers output ``path``: every run whose
    finalized ``output_identity`` lists it (relative paths resolved against
    ``outputs_dir``), plus ``record_run_id`` (a provenance record's
    ``run_id``) when that run exists. Sorted; one strict ledger read:
    raises ``ledger_io.LedgerCorruptError`` for an unreadable ledger rather
    than returning ``[]`` ("no run covers it")."""
    if not path or not str(path).strip():
        return []
    target = os.path.normcase(_resolve_ref(outputs_dir, str(path)))
    found: set[str] = set()
    ledger = _read_ledger_strict(outputs_dir)
    for manifest in (ledger[k] for k in sorted(ledger)):
        rid = manifest.get("run_id")
        if not isinstance(rid, str):
            continue
        if record_run_id is not None and rid == record_run_id:
            found.add(rid)
            continue
        outputs = (manifest.get("output_identity") or {}).get("file_hashes") or {}
        if any(os.path.normcase(_resolve_ref(outputs_dir, str(p))) == target for p in outputs):
            found.add(rid)
    return sorted(found)


# ---------------------------------------------------------------------------
# RUN-kind EvidenceRecord bridge (the previously-unused EvidenceKind.RUN slot)
# ---------------------------------------------------------------------------

def _in_progress_age_seconds(manifest: dict[str, Any]) -> "float | None":
    """Best-effort seconds since ``manifest["updated_at"]`` (falls back to
    ``created_at`` if that's somehow absent). ``None`` when neither
    timestamp is present or parseable -- callers must treat that as "cannot
    confirm liveness" (conservatively: NOT alive), never as "assume alive".
    Never raises."""
    raw = manifest.get("updated_at") or manifest.get("created_at")
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - ts).total_seconds())


def run_manifest_to_evidence_record(manifest: dict[str, Any]) -> "research_evidence.EvidenceRecord":
    """Represent one run-manifest ledger record as a ``RUN``-kind
    :class:`research_evidence.EvidenceRecord` -- the slot that module's own
    ``EvidenceKind`` enum already reserved (see its docstring) but nothing
    in this codebase ever populated until now. No new envelope/codec is
    introduced: JSON/XML round-trip, canonical hashing, and the
    Markdown-is-only-a-projection rule all come free via the existing
    envelope machinery once wrapped this way.

    Resolver-status mapping (mirrors ``provenance_status``'s own explicit
    "never let a caller infer status from field presence" discipline).
    PROV-CANONICAL (7d9b8251) revised the ``in_progress``/``failed``
    branches below -- see :class:`research_evidence.ResolverStatus`'s own
    docstring for why ``PENDING_RETRY``/``FAILED`` were added:

      * ``phase == "complete"`` AND no missing outputs AND no unknown
        artifact ids -> ``VERIFIED``.
      * ``phase == "in_progress"`` AND last updated within
        :data:`_STALE_IN_PROGRESS_SECONDS` -> ``PENDING_RETRY`` (genuinely
        still executing -- safe to poll/retry, distinct from a dead one).
      * ``phase == "in_progress"`` but stale (older than the threshold, or
        no parseable timestamp at all -- fail-closed, never assumed alive)
        -> ``UNAVAILABLE`` (likely an orphaned/crashed run).
      * ``phase == "failed"`` -> ``FAILED`` (a first-class terminal-failure
        signal -- PRE-PROV-CANONICAL this was ``DEGRADED``, which conflated
        "confirmed dead" with "still usable but imperfect").
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
        age = _in_progress_age_seconds(manifest)
        if age is not None and age <= _STALE_IN_PROGRESS_SECONDS:
            resolver = research_evidence.ResolverState(
                status=research_evidence.ResolverStatus.PENDING_RETRY,
                confidence=0.3,
                reason=(
                    "run manifest still in_progress and recently updated "
                    f"({age:.0f}s ago) -- likely actively executing, safe to "
                    "poll/retry rather than treat as dead"
                ),
            )
        else:
            staleness = (
                f"last updated {age:.0f}s ago" if age is not None
                else "updated_at/created_at missing or unparseable"
            )
            resolver = research_evidence.ResolverState(
                status=research_evidence.ResolverStatus.UNAVAILABLE,
                confidence=0.15,
                reason=(
                    f"run manifest in_progress but stale ({staleness}) -- "
                    "likely an orphaned/crashed run, not actively executing"
                ),
            )
    elif phase == "failed":
        resolver = research_evidence.ResolverState(
            status=research_evidence.ResolverStatus.FAILED,
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
