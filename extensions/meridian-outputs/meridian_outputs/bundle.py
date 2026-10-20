"""meridian_outputs.bundle -- one-file backup/export of the Outputs ledgers.

Gap set C, "backup-export".

Problem
-------
An outputs tree's provenance state lives in four separate JSON sidecars
under ``<outputs_dir>/.meridian-outputs-cache/``: ``provenance_ledger.json``
(:mod:`annotate`), ``fingerprint_ledger.json`` (:mod:`fingerprint`),
``run_manifest_ledger.json`` (:mod:`run_manifest`) and
``artifact_registry.json`` (:mod:`artifact_registry`). Nothing gathered them
into one file, checksummed them, or restored them with a verification step,
so a backup was "copy the directory and hope".

What this module adds
---------------------
* :func:`export_bundle` -- loads the four ledgers through their existing
  module readers (``annotate._read_ledger``, ``fingerprint._read_ledger``,
  ``run_manifest._read_ledger``, ``artifact_registry._read_registry``,
  looked up by name at call time) and writes ONE JSON envelope::

      {"format": "meridian-outputs-bundle", "schema_version": 1,
       "digest_algorithm": "sha256", "exported_at": ..., "package_version": ...,
       "members": {<name>: {"sha256": ..., "size": ..., "payload": ...}},
       "bundle_digest": ...}

  Each member's ``sha256``/``size`` are over the payload's canonical JSON
  (``sort_keys=True``, compact separators, ASCII). ``bundle_digest`` is the
  sha256 of the canonical JSON of ``{"format", "schema_version", "members":
  [[name, sha256], ...] sorted by name}`` -- binding every member digest to
  its member name, so swapping two members is detected too.
  ``exported_at``/``package_version`` are informational and are NOT covered
  by the digest, so two exports of identical state have the same
  ``bundle_digest``. The file is written to a temp file in the target
  directory, flushed, fsynced, then ``os.replace``d into place.

* :func:`verify_bundle` -- recomputes every digest and reports
  ``verified``, ``mismatches``, ``missing``, ``unexpected``, ``malformed``
  and ``errors``. Any problem means ``verified=False``.

* :func:`import_bundle` -- verifies first and refuses on any failure,
  checks the hash-algorithm versions, plans a per-key merge into the target
  ledgers, refuses on conflicts unless ``overwrite=True``, and writes
  nothing unless ``dry_run=False``.

Members and what is deliberately left out
-----------------------------------------
* ``index_meta`` (fifth member): the exporting code's index hash-algorithm
  version (``outputs_local._HASH_ALGO_VERSION``) and the ledgers' digest
  algorithm/version (``fingerprint._DIGEST_ALGORITHM``/``_DIGEST_VERSION``).
  It is read from the code, not from ``index.duckdb``: opening the DuckDB
  file would contend with a live read-write connection (in this process or
  another) and would make the digest depend on whether an index happens to
  be open. The stored value can only lag the code constant (it is upgraded
  on the index's next connect), and the index itself is not in the bundle.
* ``index.duckdb`` is excluded: it is rebuildable from the corpus and can be
  gigabytes.
* Registry ``local_paths[*].local_only_path`` values are replaced with
  ``None`` -- the machine-local absolute path is stripped, the key stays,
  because ``artifact_registry.verify_artifact_hash`` reads that key directly
  and would raise ``KeyError`` on an imported registry if it were deleted.
  ``basename``/``host``/``first_seen``/``last_seen`` are kept, matching
  ``artifact_registry.strip_local_metadata``.
* Run-manifest records are exported verbatim. Their ``ledger_locations`` and
  ``repo_identity.repo_dir`` are absolute paths by that module's design, and
  they feed each manifest's identity ``manifest_hash``, so rewriting them
  would invalidate the record.

Integrity, not authenticity
---------------------------
sha256 checksums detect corruption and accidental edits. They do not prove
who wrote a bundle: anyone able to edit the file can recompute the digests.
An HMAC would need key management this package does not have.

Merge and conflict rules (import)
---------------------------------
* Dict ledgers (provenance, fingerprint, run manifest) merge per key: a key
  only in the bundle is added, an identical key is unchanged, a key whose
  value differs is a conflict. Keys only in the target are always kept.
* Registry artifacts merge per ``artifact_id``, compared without
  ``local_paths`` (sightings are machine-local state that belongs to the
  target). On ``overwrite`` the bundle's record replaces the target's but the
  target keeps its own ``local_paths``. Registry edges merge per
  ``(artifact_id, source_locator, relation)`` -- the same idempotency key
  ``bind_source_edge`` uses -- compared on ``metadata`` only.
* Any conflict refuses the whole import unless ``overwrite=True``.
* A target ledger that exists but is not valid JSON refuses the import:
  merging into it through a reader that maps corruption to ``{}`` would
  silently wipe it.
* Each ledger write runs inside ``ledger_io.ledger_transaction`` on that
  ledger's path -- the cross-process lock the provenance and fingerprint
  writers (``record_provenance``, ``tag_output``) commit under -- and, for
  the registry and run manifest, whose writers still lock in-process only,
  also inside that module's ``_write_lock``. Under the lock the import
  re-checks the file's ``(mtime_ns, size)`` signature and re-reads the
  ledger; a ledger that changed after the plan read it stops the import
  with ``reason="concurrent_modification"``, and no writer can commit
  between that check and the replace. Registry and run-manifest writers in
  another process are not excluded until those modules adopt
  ``ledger_io``.

Paths
-----
``bundle_path`` must resolve inside ``outputs_dir``
(:func:`path_security._require_within`, symlink-aware). Inside
``.meridian-outputs-cache`` only the ``bundles/`` subdirectory may hold a
bundle: everything else there is live state (the ledgers, ``index.duckdb``,
the ``*.lock`` files the index and ledger writers hold, the derived cache).
A ``:`` anywhere but a drive letter is refused, since on Windows it names an
alternate data stream of another file. Export also refuses to replace an
existing file unless ``overwrite=True``. A relative ``bundle_path`` is
relative to ``outputs_dir``. The default, ``.meridian-outputs-cache/bundles/
meridian-outputs-bundle.json``, sits in a dot-directory, which the outputs
walk skips, so the bundle is never indexed as an output itself. Results
report the bundle path relative to ``outputs_dir``, never absolute.

NO hosted call is made anywhere in this module.
"""
from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from typing import Any, Iterator

from . import annotate, artifact_registry, fingerprint, ledger_io, outputs_local, run_manifest
from .path_security import _require_within, relative_output_path

__all__ = [
    "BUNDLE_FORMAT",
    "BUNDLE_SCHEMA_VERSION",
    "DEFAULT_BUNDLE_RELPATH",
    "MEMBER_NAMES",
    "export_bundle",
    "verify_bundle",
    "import_bundle",
]

BUNDLE_FORMAT = "meridian-outputs-bundle"
BUNDLE_SCHEMA_VERSION = 1
DIGEST_ALGORITHM = "sha256"
DEFAULT_BUNDLE_RELPATH = ".meridian-outputs-cache/bundles/meridian-outputs-bundle.json"
MAX_BUNDLE_BYTES = 1 << 30  # 1 GiB: refuse to json-parse anything larger
INDEX_META = "index_meta"

_CACHE_DIRNAME = ".meridian-outputs-cache"
_BUNDLES_SUBDIR = "bundles"  # the only part of the cache a bundle may live in
_LOCK_TIMEOUT_SECONDS = 30.0
_CONFLICT_LIST_CAP = 500

# member name -> (module, reader attribute, on-disk filename). The reader is
# looked up by NAME at call time, so a reader replaced on another branch (or
# monkeypatched in a test) is the one used.
_LEDGER_MEMBERS: dict[str, tuple[Any, str, str]] = {
    "artifact_registry": (artifact_registry, "_read_registry", "artifact_registry.json"),
    "fingerprint_ledger": (fingerprint, "_read_ledger", "fingerprint_ledger.json"),
    "provenance_ledger": (annotate, "_read_ledger", "provenance_ledger.json"),
    "run_manifest_ledger": (run_manifest, "_read_ledger", "run_manifest_ledger.json"),
}
MEMBER_NAMES: tuple[str, ...] = tuple(sorted([*_LEDGER_MEMBERS, INDEX_META]))

# Ledgers whose own writers serialise only on their module's in-process
# ``_write_lock`` (they have not adopted ledger_io yet); see
# _ledger_write_lock. The provenance and fingerprint writers use
# ledger_io.ledger_transaction.
_IN_PROCESS_LOCKED_MEMBERS = frozenset({"artifact_registry", "run_manifest_ledger"})

_PROTECTED_CACHE_FILES = frozenset(
    name.lower()
    for name in (
        *(filename for _, _, filename in _LEDGER_MEMBERS.values()),
        "index.duckdb",
        "index.duckdb.wal",
    )
)


# ---------------------------------------------------------------------------
# Canonical digests
# ---------------------------------------------------------------------------

def _canonical_bytes(obj: Any) -> bytes:
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("utf-8")


def _member_digest(payload: Any) -> tuple[str, int]:
    data = _canonical_bytes(payload)
    return hashlib.sha256(data).hexdigest(), len(data)


def _bundle_digest(member_digests: dict[str, str]) -> str:
    material = {
        "format": BUNDLE_FORMAT,
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "members": [[name, member_digests[name]] for name in sorted(member_digests)],
    }
    return hashlib.sha256(_canonical_bytes(material)).hexdigest()


def _index_meta() -> dict[str, Any]:
    return {
        "hash_algo_version": int(outputs_local._HASH_ALGO_VERSION),
        "ledger_digest_algorithm": fingerprint._DIGEST_ALGORITHM,
        "ledger_digest_version": int(fingerprint._DIGEST_VERSION),
        "index_included": False,
    }


# ---------------------------------------------------------------------------
# Paths and files
# ---------------------------------------------------------------------------

def _cache_dir(outputs_dir: str) -> str:
    return os.path.join(outputs_dir, _CACHE_DIRNAME)


def _ledger_file(outputs_dir: str, filename: str) -> str:
    return os.path.join(_cache_dir(outputs_dir), filename)


def _inside(path: str, root: str) -> bool:
    """``path`` is ``root`` or below it, compared case-insensitively.

    Case-insensitive on every platform: on a case-insensitive filesystem
    (Windows, default macOS) ``.MERIDIAN-OUTPUTS-CACHE`` is the cache, and on
    a case-sensitive one refusing a look-alike directory costs nothing.
    """
    path_key, root_key = os.path.normcase(path).lower(), os.path.normcase(root).lower()
    try:
        return os.path.commonpath((root_key, path_key)) == root_key
    except ValueError:  # different drives
        return False


def _stream_separator_error(bundle_path: Any) -> str | None:
    """Refuse a ``:`` outside the drive letter: on Windows ``name:stream``
    writes an alternate data stream of ``name``, an existing file."""
    if not isinstance(bundle_path, (str, os.PathLike)):
        return None  # _require_within reports the type problem
    raw = os.fsdecode(os.fspath(bundle_path))
    if ":" in os.path.splitdrive(raw)[1]:
        return "bundle_path rejected: ':' is only allowed in a drive letter"
    return None


def _resolve_bundle_path(outputs_dir: str, bundle_path: Any) -> tuple[str | None, str | None]:
    """``(real_path, None)`` or ``(None, error)``; never leaks an absolute path."""
    stream_error = _stream_separator_error(bundle_path)
    if stream_error:
        return None, stream_error
    try:
        real = _require_within(outputs_dir, bundle_path)
    except ValueError as exc:
        return None, f"bundle_path rejected: {exc}"
    cache_real = os.path.realpath(os.path.join(os.path.realpath(outputs_dir), _CACHE_DIRNAME))
    if _inside(real, cache_real):
        if (
            os.path.normcase(os.path.dirname(real)).lower() == os.path.normcase(cache_real).lower()
            and os.path.basename(real).lower() in _PROTECTED_CACHE_FILES
        ):
            return None, "bundle_path rejected: it names a live ledger or index file"
        # Not realpath'd on purpose: ``real`` is already fully resolved, so a
        # bundles/ link back into the cache cannot pass this check.
        bundles_dir = os.path.join(cache_real, _BUNDLES_SUBDIR)
        if not _inside(real, bundles_dir) or _inside(bundles_dir, real):
            return None, (
                f"bundle_path rejected: inside {_CACHE_DIRNAME} only the "
                f"{_BUNDLES_SUBDIR}/ subdirectory may hold a bundle (the rest "
                "is live ledger, index, lock and derived-cache state)"
            )
    if os.path.isdir(real):
        return None, "bundle_path rejected: it names a directory"
    return real, None


def _display_path(outputs_dir: str, real: str) -> str:
    try:
        return relative_output_path(outputs_dir, real)
    except ValueError:
        return os.path.basename(real)


def _ledger_file_problem(path: str) -> str | None:
    """``None`` if the ledger file is absent or a valid JSON object."""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f"unreadable ({type(exc).__name__})"
    if st.st_size == 0:
        return "file is empty (zero bytes)"
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        return f"not valid JSON ({type(exc).__name__})"
    if not isinstance(data, dict):
        return "top level is not a JSON object"
    return None


def _file_signature(path: str) -> tuple[int, int] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size


def _atomic_write_json(path: str, obj: Any, *, indent: int | None) -> None:
    """Temp file in the same directory, fsync, ``os.replace``, directory
    fsync; the temp file is removed on any failure so nothing half-written
    is left behind. The replace retries a transient Windows sharing
    violation (a lock-free reader holding the target open) the same way
    ``ledger_io.write_ledger_atomic`` does."""
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".bundle-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, indent=indent, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        ledger_io._retry_sharing(lambda: os.replace(tmp, path))
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    ledger_io._fsync_directory(directory)


def _ensure_cache_dir(outputs_dir: str) -> None:
    cache = _cache_dir(outputs_dir)
    os.makedirs(cache, exist_ok=True)
    try:
        outputs_local.ensure_gitignored(cache)
    except OSError:
        pass


@contextlib.contextmanager
def _ledger_write_lock(name: str, module: Any, path: str) -> Iterator[None]:
    """Hold the locks that ledger ``name``'s own writers serialise on.

    Always ``ledger_io.ledger_transaction`` on the ledger path: the
    cross-process lock ``record_provenance`` and ``tag_output`` commit
    under. For the members in :data:`_IN_PROCESS_LOCKED_MEMBERS`, whose
    writers still use only their module's in-process ``_write_lock``, that
    lock is taken too, inside the transaction (those writers never take the
    ledger transaction, so the order cannot deadlock). ``annotate._write_lock``
    is deliberately NOT taken: it guards only that module's read cache, and
    annotate's writers take it inside their own transaction.
    """
    with ledger_io.ledger_transaction(path):
        if name not in _IN_PROCESS_LOCKED_MEMBERS:
            yield
            return
        lock = module._write_lock
        if not lock.acquire(timeout=_LOCK_TIMEOUT_SECONDS):
            raise TimeoutError("ledger write lock is busy")
        try:
            yield
        finally:
            lock.release()


def _invalidate_annotate_cache(path: str) -> None:
    cache = getattr(annotate, "_ledger_read_cache", None)
    if not isinstance(cache, dict):
        return
    target = os.path.normcase(os.path.realpath(path))
    for key in list(cache):
        with contextlib.suppress(OSError, TypeError, ValueError):
            if os.path.normcase(os.path.realpath(key)) == target:
                cache.pop(key, None)


# ---------------------------------------------------------------------------
# Payload helpers
# ---------------------------------------------------------------------------

def _strip_registry(registry: dict[str, Any]) -> tuple[dict[str, Any], int]:
    clean = copy.deepcopy(registry)
    stripped = 0
    for record in (clean.get("artifacts") or {}).values():
        if not isinstance(record, dict):
            continue
        for sighting in record.get("local_paths") or []:
            if isinstance(sighting, dict) and sighting.get("local_only_path") is not None:
                sighting["local_only_path"] = None
                stripped += 1
    return clean, stripped


def _entry_count(name: str, payload: Any) -> int:
    if name == "artifact_registry" and isinstance(payload, dict):
        return len(payload.get("artifacts") or {}) + len(payload.get("edges") or [])
    if name == INDEX_META:
        return 1
    return len(payload) if isinstance(payload, dict) else 0


def _str_or_none(value: Any) -> bool:
    return value is None or isinstance(value, str)


# Row-level checks. Whoever edits a bundle can recompute its digests, so
# verify must also reject rows the ledger readers would crash on after an
# import: fingerprint.check_staleness passes script_path to os.fspath, the
# registry sorts local_paths by last_seen and edges by artifact_id/
# source_locator/relation, and reads every sighting with .get. Messages
# never quote a ledger key, since legacy keys can be absolute paths.

def _fingerprint_row_error(row: dict[str, Any]) -> str | None:
    for field_name in ("script_path", "script_hash"):
        if not _str_or_none(row.get(field_name)):
            return f"a fingerprint_ledger entry's {field_name} is not a string or null"
    digest = row.get("content_digest")
    if isinstance(digest, dict):
        digest = digest.get("digest")
    if not _str_or_none(digest):
        return "a fingerprint_ledger entry's content_digest is not a string, null or digest object"
    return None


def _artifact_record_error(record: dict[str, Any]) -> str | None:
    if not isinstance(record.get("artifact_id"), str):
        return "an artifact_registry record's artifact_id is not a string"
    sightings = record.get("local_paths", [])
    if not isinstance(sightings, list) or not all(isinstance(s, dict) for s in sightings):
        return "an artifact_registry record's local_paths is not a list of objects"
    for sighting in sightings:
        for field_name in ("local_only_path", "last_seen"):
            if not _str_or_none(sighting.get(field_name)):
                return (
                    f"an artifact_registry record's local_paths {field_name} "
                    "is not a string or null"
                )
    return None


def _edge_error(edge: dict[str, Any]) -> str | None:
    for field_name in ("artifact_id", "source_locator", "relation"):
        if not isinstance(edge.get(field_name), str):
            return f"an artifact_registry edge's {field_name} is not a string"
    if not isinstance(edge.get("metadata") or {}, dict):
        return "an artifact_registry edge's metadata is not an object"
    return None


def _payload_shape_error(name: str, payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return "payload is not a JSON object"
    if name == INDEX_META:
        version = payload.get("hash_algo_version")
        if isinstance(version, bool) or not isinstance(version, int):
            return "index_meta.hash_algo_version is not an integer"
        return None
    if name == "artifact_registry":
        artifacts = payload.get("artifacts")
        edges = payload.get("edges")
        if not isinstance(artifacts, dict) or not all(isinstance(v, dict) for v in artifacts.values()):
            return "artifact_registry.artifacts is not an object of records"
        if not isinstance(edges, list) or not all(isinstance(e, dict) for e in edges):
            return "artifact_registry.edges is not a list of edge objects"
        for record in artifacts.values():
            error = _artifact_record_error(record)
            if error:
                return error
        for edge in edges:
            error = _edge_error(edge)
            if error:
                return error
        return None
    # Every provenance, fingerprint and run-manifest row must be an object;
    # fingerprint rows are then checked field by field.
    if not all(isinstance(v, dict) for v in payload.values()):
        return "ledger entries are not all JSON objects"
    if name == "fingerprint_ledger":
        for row in payload.values():
            error = _fingerprint_row_error(row)
            if error:
                return error
    return None


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def export_bundle(
    outputs_dir: str, bundle_path: str | None = None, *, overwrite: bool = False,
) -> dict[str, Any]:
    """Write every Outputs ledger plus index metadata to one verified file.

    Args:
      outputs_dir:  Absolute path to the outputs directory.
      bundle_path:  Where to write, inside ``outputs_dir`` (relative paths are
                    relative to it). Defaults to
                    :data:`DEFAULT_BUNDLE_RELPATH`. Inside
                    ``.meridian-outputs-cache`` only its ``bundles/``
                    subdirectory is allowed.
      overwrite:    Replace a file that already exists at ``bundle_path``.
                    Off by default, so an export never silently replaces a
                    user's file or an earlier bundle.

    Returns:
      ``{"exported": True, "bundle_path", "bundle_digest", "members":
      {name: {"sha256", "size", "entries"}}, "local_only_paths_stripped"}``
      on success, or ``{"exported": False, "error", "errors"?}``. Refuses
      (writes nothing) when a ledger file exists but is empty, unreadable or
      not a JSON object, or when its reader raises -- a corrupt ledger is
      never exported as an empty payload.
    """
    if not outputs_dir or not str(outputs_dir).strip() or not os.path.isdir(outputs_dir):
        return {"exported": False, "error": "outputs_dir does not exist"}
    target, error = _resolve_bundle_path(outputs_dir, bundle_path or DEFAULT_BUNDLE_RELPATH)
    if target is None:
        return {"exported": False, "error": error}
    if not overwrite and os.path.lexists(target):
        return {
            "exported": False,
            "error": "bundle_path rejected: a file already exists there "
                     "(pass overwrite=True to replace it)",
        }

    payloads: dict[str, Any] = {}
    errors: list[str] = []
    stripped = 0
    for name in sorted(_LEDGER_MEMBERS):
        module, reader_name, filename = _LEDGER_MEMBERS[name]
        problem = _ledger_file_problem(_ledger_file(outputs_dir, filename))
        if problem:
            errors.append(f"{name}: {problem}")
            continue
        try:
            payload = getattr(module, reader_name)(outputs_dir)
        except Exception as exc:  # noqa: BLE001 -- reported, export refused
            errors.append(f"{name}: reader failed ({type(exc).__name__})")
            continue
        shape = _payload_shape_error(name, payload)
        if shape:
            errors.append(f"{name}: {shape}")
            continue
        if name == "artifact_registry":
            payload, stripped = _strip_registry(payload)
        payloads[name] = payload
    if errors:
        return {
            "exported": False,
            "error": "refusing to export: a ledger could not be read cleanly",
            "errors": errors,
        }
    payloads[INDEX_META] = _index_meta()

    members: dict[str, dict[str, Any]] = {}
    digests: dict[str, str] = {}
    for name in MEMBER_NAMES:
        digest, size = _member_digest(payloads[name])
        digests[name] = digest
        members[name] = {"sha256": digest, "size": size, "payload": payloads[name]}
    bundle_digest = _bundle_digest(digests)
    from . import __version__ as package_version

    envelope = {
        "format": BUNDLE_FORMAT,
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "digest_algorithm": DIGEST_ALGORITHM,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "package_version": package_version,
        "members": members,
        "bundle_digest": bundle_digest,
    }
    try:
        # The ledger readers above already created and gitignored the cache
        # dir; _atomic_write_json creates any bundle subdirectory itself.
        _atomic_write_json(target, envelope, indent=None)
    except OSError as exc:
        return {"exported": False, "error": f"failed to write bundle ({type(exc).__name__})"}

    return {
        "exported": True,
        "bundle_path": _display_path(outputs_dir, target),
        "bundle_digest": bundle_digest,
        "members": {
            name: {
                "sha256": members[name]["sha256"],
                "size": members[name]["size"],
                "entries": _entry_count(name, payloads[name]),
            }
            for name in MEMBER_NAMES
        },
        "local_only_paths_stripped": stripped,
    }


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------

def _empty_report() -> dict[str, Any]:
    return {
        "verified": False,
        "format": None,
        "schema_version": None,
        "bundle_digest": None,
        "bundle_digest_ok": False,
        "members": {},
        "mismatches": [],
        "missing": [],
        "unexpected": [],
        "malformed": [],
        "errors": [],
    }


def _load_and_verify(
    bundle_path: Any, outputs_dir: str | None, max_bytes: int,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    report = _empty_report()
    if outputs_dir is not None:
        if not outputs_dir or not str(outputs_dir).strip() or not os.path.isdir(outputs_dir):
            report["errors"].append("outputs_dir does not exist")
            return report, None
        real, error = _resolve_bundle_path(outputs_dir, bundle_path)
        if real is None:
            report["errors"].append(error)
            return report, None
    else:
        if not bundle_path or not str(bundle_path).strip():
            report["errors"].append("bundle_path is required")
            return report, None
        real = os.path.realpath(os.fspath(bundle_path))

    try:
        size = os.path.getsize(real)
    except OSError:
        report["errors"].append("bundle file not found or unreadable")
        return report, None
    if size > max_bytes:
        report["errors"].append(f"bundle is larger than the {max_bytes}-byte limit")
        return report, None
    try:
        with open(real, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        report["errors"].append(f"bundle is not valid JSON ({type(exc).__name__})")
        return report, None
    if not isinstance(data, dict):
        report["errors"].append("bundle top level is not a JSON object")
        return report, None

    report["format"] = data.get("format")
    report["schema_version"] = data.get("schema_version")
    report["bundle_digest"] = data.get("bundle_digest")
    if data.get("format") != BUNDLE_FORMAT:
        report["errors"].append("unrecognised format marker")
    schema = data.get("schema_version")
    if isinstance(schema, bool) or schema != BUNDLE_SCHEMA_VERSION:
        report["errors"].append(f"unsupported schema_version {schema!r}")
    if report["errors"]:
        return report, None

    members = data.get("members")
    if not isinstance(members, dict):
        report["errors"].append("members is not a JSON object")
        return report, None
    report["missing"] = [name for name in MEMBER_NAMES if name not in members]
    report["unexpected"] = sorted(name for name in members if name not in MEMBER_NAMES)

    computed: dict[str, str] = {}
    for name in MEMBER_NAMES:
        if name not in members:
            continue
        member = members[name]
        if not isinstance(member, dict) or "payload" not in member or not isinstance(member.get("sha256"), str):
            report["malformed"].append(name)
            continue
        digest, size = _member_digest(member["payload"])
        computed[name] = digest
        digest_ok = digest == member["sha256"] and member.get("size") == size
        shape = _payload_shape_error(name, member["payload"])
        if not digest_ok:
            report["mismatches"].append(name)
        if shape:
            report["malformed"].append(name)
        report["members"][name] = {
            "sha256": member["sha256"],
            "computed_sha256": digest,
            "size": member.get("size"),
            "computed_size": size,
            "ok": digest_ok and not shape,
        }
        if shape:
            report["members"][name]["shape_error"] = shape

    if not report["missing"] and len(computed) == len(MEMBER_NAMES):
        report["bundle_digest_ok"] = data.get("bundle_digest") == _bundle_digest(computed)
    report["verified"] = not (
        report["errors"] or report["missing"] or report["unexpected"]
        or report["malformed"] or report["mismatches"]
    ) and report["bundle_digest_ok"]
    return report, data


def verify_bundle(
    bundle_path: str,
    *,
    outputs_dir: str | None = None,
    max_bytes: int = MAX_BUNDLE_BYTES,
) -> dict[str, Any]:
    """Recompute every digest in a bundle and report what does not match.

    Args:
      bundle_path:  The bundle file. When ``outputs_dir`` is given it must
                    resolve inside it (relative paths are relative to it).
      outputs_dir:  Optional root to confine ``bundle_path`` to (the MCP tool
                    always passes it).
      max_bytes:    Refuse to parse a file larger than this.

    Returns:
      ``{"verified", "format", "schema_version", "bundle_digest",
      "bundle_digest_ok", "members": {name: {"sha256", "computed_sha256",
      "size", "computed_size", "ok"}}, "mismatches", "missing",
      "unexpected", "malformed", "errors"}``. ``verified`` is True only when
      the format marker and schema version are right, every expected member
      is present with a matching digest, size and payload shape, no
      unexpected member exists, and ``bundle_digest`` matches. Never raises
      on a bad file -- every failure is reported.
    """
    report, _data = _load_and_verify(bundle_path, outputs_dir, max_bytes)
    return report


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

def _merge_mapping(
    current: dict[str, Any],
    incoming: dict[str, Any],
    *,
    overwrite: bool,
    comparable: Any = None,
    combine: Any = None,
) -> tuple[dict[str, Any], dict[str, list[str]]]:
    view = comparable or (lambda value: value)
    merged = dict(current)
    stats: dict[str, list[str]] = {"added": [], "unchanged": [], "conflicts": [], "replaced": []}
    for key in sorted(incoming):
        value = incoming[key]
        if key not in current:
            merged[key] = value
            stats["added"].append(key)
        elif view(current[key]) == view(value):
            stats["unchanged"].append(key)
        else:
            stats["conflicts"].append(key)
            if overwrite:
                merged[key] = combine(current[key], value) if combine else value
                stats["replaced"].append(key)
    return merged, stats


def _edge_key(edge: dict[str, Any]) -> str:
    return json.dumps(
        [edge.get("artifact_id"), edge.get("source_locator"), edge.get("relation")],
        ensure_ascii=True,
    )


def _merge_registry(
    current: dict[str, Any], incoming: dict[str, Any], *, overwrite: bool,
) -> tuple[dict[str, Any], dict[str, list[str]]]:
    merged = dict(current)
    stats: dict[str, list[str]] = {"added": [], "unchanged": [], "conflicts": [], "replaced": []}

    def _absorb(prefix: str, part: dict[str, list[str]]) -> None:
        for bucket, keys in part.items():
            stats[bucket].extend(f"{prefix}{key}" for key in keys)

    # Top-level keys other than artifacts/edges (none today) merge as values.
    extra_current = {k: v for k, v in current.items() if k not in ("artifacts", "edges")}
    extra_incoming = {k: v for k, v in incoming.items() if k not in ("artifacts", "edges")}
    extra_merged, extra_stats = _merge_mapping(extra_current, extra_incoming, overwrite=overwrite)
    merged.update(extra_merged)
    _absorb("", extra_stats)

    artifacts, artifact_stats = _merge_mapping(
        dict(current.get("artifacts") or {}),
        dict(incoming.get("artifacts") or {}),
        overwrite=overwrite,
        comparable=lambda rec: {k: v for k, v in rec.items() if k != "local_paths"},
        combine=lambda cur, inc: {**inc, "local_paths": cur.get("local_paths") or []},
    )
    merged["artifacts"] = artifacts
    _absorb("artifact:", artifact_stats)

    edges = [dict(e) for e in (current.get("edges") or [])]
    position = {_edge_key(e): i for i, e in enumerate(edges)}
    for edge in incoming.get("edges") or []:
        key = _edge_key(edge)
        if key not in position:
            position[key] = len(edges)
            edges.append(dict(edge))
            stats["added"].append(f"edge:{key}")
        elif (edges[position[key]].get("metadata") or {}) == (edge.get("metadata") or {}):
            stats["unchanged"].append(f"edge:{key}")
        else:
            stats["conflicts"].append(f"edge:{key}")
            if overwrite:
                edges[position[key]] = dict(edge)
                stats["replaced"].append(f"edge:{key}")
    merged["edges"] = edges
    return merged, stats


def _summarise(stats: dict[str, list[str]]) -> dict[str, Any]:
    conflicts = stats["conflicts"]
    return {
        "added": len(stats["added"]),
        "unchanged": len(stats["unchanged"]),
        "replaced": len(stats["replaced"]),
        "conflict_count": len(conflicts),
        "conflicts": conflicts[:_CONFLICT_LIST_CAP],
        "conflicts_truncated": len(conflicts) > _CONFLICT_LIST_CAP,
    }


def _concurrent_modification_result(result: dict[str, Any]) -> dict[str, Any]:
    """Mark ``result`` as stopped because a target ledger changed after the
    plan read it, and return it.

    The import declines to write anything further, so this is a refusal:
    ``refused`` is True like every other stop, ``imported`` stays False, and
    ``written_members`` keeps whatever was already replaced. The import
    write loop's signature re-check should return through this helper
    rather than setting ``reason`` by hand.
    """
    result["refused"] = True
    result["imported"] = False
    result["reason"] = "concurrent_modification"
    return result


def import_bundle(
    bundle_path: str,
    outputs_dir: str,
    *,
    dry_run: bool = True,
    overwrite: bool = False,
    accept_hash_algo_mismatch: bool = False,
    max_bytes: int = MAX_BUNDLE_BYTES,
) -> dict[str, Any]:
    """Verify a bundle, then merge its ledgers into ``outputs_dir``.

    Order of checks, each fail-closed (a refusal writes nothing):
      1. ``bundle_path`` must resolve inside ``outputs_dir``, and the bundle
         must pass :func:`verify_bundle` in full.
      2. Hash-algorithm versions: the bundle's ``index_meta`` must match this
         code's ``outputs_local._HASH_ALGO_VERSION`` and ledger digest
         algorithm/version. A mismatch refuses unless
         ``accept_hash_algo_mismatch=True``, in which case it is flagged in
         ``hash_algo`` and the import proceeds (ledger hashes are sha256
         either way; the index version only describes the excluded index).
      3. Every target ledger that exists must be a readable JSON object.
      4. The per-key merge plan must have no conflicts, unless
         ``overwrite=True``.
      5. Nothing is written when ``dry_run`` is True (the default).

    A real import replaces each changed ledger file atomically, one after
    another; it is NOT atomic across the ledgers. If a later ledger's write
    fails or stops (``"write_failed"``, ``"concurrent_modification"``), the
    ledgers already replaced stay replaced: ``written_members`` lists exactly
    those, in write order, and the rest are untouched.

    Args:
      bundle_path:                The bundle file, inside ``outputs_dir``.
      outputs_dir:                The outputs directory to import into.
      dry_run:                    Plan only (default). Pass False to write.
      overwrite:                  Replace conflicting entries with the
                                  bundle's (registry sightings stay local).
      accept_hash_algo_mismatch:  Proceed, flagged, across a hash-version
                                  mismatch.
      max_bytes:                  Refuse to parse a larger bundle file.

    Returns:
      ``{"imported", "dry_run", "refused", "reason", "verification",
      "hash_algo", "members": {name: {"added", "unchanged", "replaced",
      "conflict_count", "conflicts", "conflicts_truncated"}},
      "would_write", "written_members"}``. ``imported`` is True only after a
      real (non-dry-run) import completed every write it needed. ``reason``
      is one of ``None``, ``"outputs_dir_missing"``,
      ``"verification_failed"``, ``"hash_algo_version_mismatch"``,
      ``"target_ledger_unreadable"``, ``"conflicts"``,
      ``"concurrent_modification"`` or ``"write_failed"``.
    """
    result: dict[str, Any] = {
        "imported": False,
        "dry_run": bool(dry_run),
        "refused": False,
        "reason": None,
        "verification": None,
        "hash_algo": None,
        "members": {},
        "would_write": [],
        "written_members": [],
    }

    def _refuse(reason: str, **extra: Any) -> dict[str, Any]:
        result["refused"] = True
        result["reason"] = reason
        result.update(extra)
        return result

    if not outputs_dir or not str(outputs_dir).strip() or not os.path.isdir(outputs_dir):
        return _refuse("outputs_dir_missing")

    report, data = _load_and_verify(bundle_path, outputs_dir, max_bytes)
    result["verification"] = report
    if not report["verified"] or data is None:
        return _refuse("verification_failed")

    members = data["members"]
    meta = members[INDEX_META]["payload"]
    local_digest = [fingerprint._DIGEST_ALGORITHM, int(fingerprint._DIGEST_VERSION)]
    bundle_digest_algo = [meta.get("ledger_digest_algorithm"), meta.get("ledger_digest_version")]
    mismatch = (
        meta.get("hash_algo_version") != int(outputs_local._HASH_ALGO_VERSION)
        or bundle_digest_algo != local_digest
    )
    result["hash_algo"] = {
        "bundle_hash_algo_version": meta.get("hash_algo_version"),
        "local_hash_algo_version": int(outputs_local._HASH_ALGO_VERSION),
        "bundle_ledger_digest": bundle_digest_algo,
        "local_ledger_digest": local_digest,
        "mismatch": mismatch,
        "accepted": bool(mismatch and accept_hash_algo_mismatch),
    }
    if mismatch and not accept_hash_algo_mismatch:
        return _refuse("hash_algo_version_mismatch")

    plan: dict[str, dict[str, Any]] = {}
    unreadable: list[str] = []
    for name in sorted(_LEDGER_MEMBERS):
        module, reader_name, filename = _LEDGER_MEMBERS[name]
        path = _ledger_file(outputs_dir, filename)
        problem = _ledger_file_problem(path)
        if problem:
            unreadable.append(f"{name}: {problem}")
            continue
        signature = _file_signature(path)
        try:
            current = getattr(module, reader_name)(outputs_dir)
        except Exception as exc:  # noqa: BLE001
            unreadable.append(f"{name}: reader failed ({type(exc).__name__})")
            continue
        incoming = members[name]["payload"]
        if name == "artifact_registry":
            merged, stats = _merge_registry(current, incoming, overwrite=overwrite)
        else:
            merged, stats = _merge_mapping(dict(current), incoming, overwrite=overwrite)
        plan[name] = {
            "module": module, "reader": reader_name, "path": path,
            "signature": signature, "base": current,
            "merged": merged, "stats": stats,
            "changed": bool(stats["added"] or stats["replaced"]),
        }
        result["members"][name] = _summarise(stats)
    if unreadable:
        return _refuse("target_ledger_unreadable", target_errors=unreadable)

    if any(p["stats"]["conflicts"] for p in plan.values()) and not overwrite:
        return _refuse("conflicts")

    result["would_write"] = [name for name in sorted(plan) if plan[name]["changed"]]
    if dry_run:
        return result

    try:
        _ensure_cache_dir(outputs_dir)
    except OSError:
        return _refuse("write_failed")
    for name in result["would_write"]:
        step = plan[name]
        try:
            # Re-check and write under the same lock the ledger's own writers
            # commit under, so no writer can commit between the check and
            # the replace. The plan was read without it: a ledger that changed
            # since (signature, or content for a same-size rewrite inside one
            # mtime tick) stops the import instead of being overwritten.
            with _ledger_write_lock(name, step["module"], step["path"]):
                if (
                    _file_signature(step["path"]) != step["signature"]
                    or getattr(step["module"], step["reader"])(outputs_dir) != step["base"]
                ):
                    return _concurrent_modification_result(result)
                _atomic_write_json(step["path"], step["merged"], indent=2)
                result["written_members"].append(name)
                if step["module"] is annotate:
                    _invalidate_annotate_cache(step["path"])
        except (OSError, TimeoutError, TypeError, ValueError, outputs_local.IndexLockAcquireError):
            result["reason"] = "write_failed"
            return result
    result["imported"] = True
    return result
