"""Local outputs indexer adapter -- wave-1 stopgap.

Wraps the core indexing logic (ported from meridian/outputs_indexer.py) and
runs it FULLY LOCALLY: no hosted call, no network dependency whatsoever.  The
hosted-aware smart-routing layer (item 1365e01a) is deliberately OUT OF SCOPE
here.

Security requirements (non-negotiable, tested):
  1. Secret-file exclusion -- :func:`is_secret_path` filters out .env*, *.key,
     *secret*, *credential*, config.*, *.pem, *.p12, *.pfx, *.jks, *.keystore,
     and other common secret-bearing patterns BEFORE any file is walked into the
     FTS index.  The filter is applied in :func:`_iter_safe_output_files` which
     replaces the plain os.walk used in the original.
  2. File locking -- :class:`IndexFileLock` provides an exclusive write lock
     (threading.Lock + optional portalocker for cross-process safety) so
     concurrent index writes can never corrupt the cache.
  3. Deterministic output -- path lists are always sorted (stable sort over
     absolute paths); dict/set iteration is avoided in any place where result
     ordering is observable.
  4. .gitignore safety -- :func:`ensure_gitignored` auto-adds the index cache
     directory to the nearest .gitignore on first use, so users never
     accidentally commit the cache.
"""
from __future__ import annotations

import concurrent.futures
import csv as csv_mod
import fnmatch
import hashlib
import io
import json
import logging
import os
import posixpath
import re
import threading
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass
from typing import Any, Callable

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Security: secret-file exclusion filter (requirement 1)
# ---------------------------------------------------------------------------

# Filename patterns (fnmatch-style, case-insensitive) that are NEVER indexed.
# Applied to the BASENAME only so they match regardless of directory depth.
_SECRET_BASENAME_PATTERNS: tuple[str, ...] = (
    ".env",
    ".env.*",
    "*.env",
    "*.key",
    "*.pem",
    "*.p12",
    "*.pfx",
    "*.jks",
    "*.keystore",
    "*.crt",
    "*.cer",
    "*.der",
    "id_rsa",
    "id_rsa.*",
    "id_dsa",
    "id_dsa.*",
    "id_ecdsa",
    "id_ecdsa.*",
    "id_ed25519",
    "id_ed25519.*",
    "*secret*",
    "*secrets*",
    "*credential*",
    "*credentials*",
    "*password*",
    "*passwd*",
    # "token" alone or prefixed by common credential adjectives -- NOT compound
    # output-analysis words like "token_counts".  We match the specific patterns
    # that are realistic secret file names.
    "token",
    "token.*",
    "*_token",
    "*_token.*",
    "*api*token*",
    "*auth*token*",
    "*access*token*",
    "*bearer*token*",
    "*refresh*token*",
    "*apikey*",
    "*api_key*",
    "*auth_key*",
    "*access_key*",
    "*private_key*",
    "*.htpasswd",
    "*.netrc",
    "netrc",
    ".netrc",
    "config.ini",
    "config.cfg",
    "config.conf",
    "config.yaml",
    "config.yml",
    "config.toml",
    "config.json",
    "settings.ini",
    "settings.cfg",
    "settings.conf",
    "settings.yaml",
    "settings.yml",
    "settings.toml",
    "settings.json",
    "secrets.yaml",
    "secrets.yml",
    "secrets.toml",
    "secrets.json",
    "*.tfvars",
    "terraform.tfstate",
    "terraform.tfstate.backup",
    "*.vault",
    "vault.yaml",
    "vault.yml",
)

# Lowercase compiled cache for fast matching (populated once on first call).
_SECRET_PATTERNS_LOWER: tuple[str, ...] = tuple(
    p.lower() for p in _SECRET_BASENAME_PATTERNS
)


def is_secret_path(path: str) -> bool:
    """Return True if ``path`` matches any secret-file exclusion pattern.

    Only the BASENAME is checked (case-insensitive fnmatch).  This is the
    authoritative filter applied before ANY file content is read or indexed.
    It is deliberately conservative: false positives (a legitimate output file
    named ``token_counts.csv``) are rare and the user can rename; false
    negatives (accidentally indexing a .env file) are a security incident.

    This function is tested exhaustively in the package test suite.
    """
    base_lower = os.path.basename(path).lower()
    for pattern in _SECRET_PATTERNS_LOWER:
        if fnmatch.fnmatch(base_lower, pattern):
            return True
    return False


def _iter_safe_output_files(outputs_dir: str) -> list[str]:
    """Walk ``outputs_dir`` recursively, returning regular files that pass the
    secret-file exclusion filter, sorted for deterministic ordering.

    MERIDIAN_NOTES.md files are included (they are picked up for annotation
    ingestion in rebuild, not FTS content rows -- same behaviour as the
    original).  Hidden directories (names starting with ``.``) are pruned to
    avoid walking .git/.env directories.
    """
    found: list[str] = []
    for root, dirs, files in os.walk(outputs_dir):
        # Prune hidden directories in-place so os.walk doesn't descend them.
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for fn in sorted(files):
            p = os.path.join(root, fn)
            if is_secret_path(p):
                _log.debug("outputs_local: skipping secret-pattern file %r", p)
                continue
            found.append(p)
    return found


# ---------------------------------------------------------------------------
# .gitignore safety (requirement 4)
# ---------------------------------------------------------------------------

def ensure_gitignored(path: str) -> None:
    """Ensure ``path`` (a directory) is listed in the nearest .gitignore.

    Searches upward from ``path`` for a ``.gitignore`` file.  If one is found
    and already contains an entry that covers ``path`` (exact or glob), this
    is a no-op.  Otherwise the entry is appended.  If no .gitignore is found,
    one is created in the same directory as ``path``.

    The entry written is the directory basename followed by '/' (a gitignore
    directory pattern), anchored to the location of the .gitignore file.  For
    example, if ``path`` is ``/project/.meridian-outputs-cache``, the entry
    ``/.meridian-outputs-cache/`` is appended to ``/project/.gitignore``.

    Errors are logged and swallowed -- a .gitignore failure must never abort
    the indexing workflow.
    """
    try:
        path = os.path.abspath(path)
        dirname = os.path.dirname(path)
        basename = os.path.basename(path)
        # Look for an existing .gitignore in the parent directory ONLY (do not
        # walk up past the immediate parent -- walking up into the repo root
        # would find the repo's own .gitignore and append the cache dir to it,
        # which is wrong when the cache lives outside the repo).  If none is
        # found in the parent, create one there.
        gi_path: str | None = None
        candidate = os.path.join(dirname, ".gitignore")
        if os.path.isfile(candidate):
            gi_path = candidate
        if gi_path is None:
            # Create one next to the cache directory.
            gi_path = os.path.join(dirname, ".gitignore")
        # Check whether an existing entry already covers this directory.
        existing = ""
        if os.path.isfile(gi_path):
            with open(gi_path, "r", encoding="utf-8", errors="replace") as fh:
                existing = fh.read()
        # Match the bare name and common gitignore patterns for it.
        patterns_to_check = (
            basename,
            basename + "/",
            "/" + basename,
            "/" + basename + "/",
            "*/" + basename,
            "*/" + basename + "/",
        )
        for pat in patterns_to_check:
            for line in existing.splitlines():
                if line.strip() == pat.strip():
                    return  # already covered
        # Compute the relative path from the .gitignore's directory.
        gi_dir = os.path.dirname(gi_path)
        try:
            rel = os.path.relpath(path, gi_dir)
        except ValueError:
            # relpath fails across drives on Windows.
            rel = basename
        entry = rel.replace("\\", "/") + "/"
        # Anchor with a leading slash when the cache is directly in the same
        # directory as the .gitignore (avoids matching nested dirs of that name).
        if "/" not in rel:
            entry = "/" + entry
        trailer = "\n" if existing and not existing.endswith("\n") else ""
        with open(gi_path, "a", encoding="utf-8") as fh:
            fh.write(f"{trailer}{entry}\n")
        _log.debug("outputs_local: added %r to %r", entry, gi_path)
    except Exception:  # noqa: BLE001 -- never abort on gitignore failure
        _log.debug("ensure_gitignored failed for %r", path, exc_info=True)


# ---------------------------------------------------------------------------
# File locking (requirement 2)
# ---------------------------------------------------------------------------

# Per-canonical-path threading locks (in-process).
_THREADING_LOCKS: dict[str, threading.Lock] = {}
_THREADING_LOCKS_META = threading.Lock()


def _get_thread_lock(canonical: str) -> threading.Lock:
    with _THREADING_LOCKS_META:
        if canonical not in _THREADING_LOCKS:
            _THREADING_LOCKS[canonical] = threading.Lock()
        return _THREADING_LOCKS[canonical]


class IndexFileLock:
    """Exclusive write lock for one index DB file.

    Uses a per-path threading.Lock for in-process safety (always) plus
    portalocker for cross-process safety (when available).  If portalocker is
    absent the cross-process layer is skipped gracefully -- the in-process
    layer still prevents concurrent writes within one Python process.

    Usage::

        with IndexFileLock(db_path):
            # exclusive write access to db_path
            ...
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._canonical = os.path.abspath(db_path) if db_path != ":memory:" else db_path
        self._thread_lock = _get_thread_lock(self._canonical)
        self._file_handle: Any = None

    def acquire(self) -> None:
        self._thread_lock.acquire()
        if self._canonical == ":memory:":
            return
        try:
            import portalocker  # noqa: PLC0415 -- optional
            lock_path = self._canonical + ".lock"
            self._file_handle = open(lock_path, "w", encoding="utf-8")  # noqa: WPS515
            portalocker.lock(self._file_handle, portalocker.LOCK_EX)
        except ImportError:
            pass  # portalocker absent -- in-process lock only
        except Exception:  # noqa: BLE001
            _log.debug("IndexFileLock: portalocker acquire failed", exc_info=True)
            if self._file_handle is not None:
                try:
                    self._file_handle.close()
                except Exception:  # noqa: BLE001
                    pass
                self._file_handle = None

    def release(self) -> None:
        try:
            if self._file_handle is not None:
                try:
                    import portalocker  # noqa: PLC0415
                    portalocker.unlock(self._file_handle)
                except Exception:  # noqa: BLE001
                    pass
                try:
                    self._file_handle.close()
                except Exception:  # noqa: BLE001
                    pass
                self._file_handle = None
        finally:
            self._thread_lock.release()

    def __enter__(self) -> "IndexFileLock":
        self.acquire()
        return self

    def __exit__(self, *_: Any) -> None:
        self.release()


# ---------------------------------------------------------------------------
# Core indexing helpers (ported from meridian/outputs_indexer.py)
# ---------------------------------------------------------------------------

_MAX_CONTENT_CHARS = 200_000
_ARCHIVAL_SUFFIX_RE = re.compile(r"_old(?:_\d+)?$", re.IGNORECASE)

_TEXT_CONTENT_SUFFIXES: frozenset[str] = frozenset({".csv", ".json"})
_METADATA_ONLY_SUFFIXES: frozenset[str] = frozenset({".npy"})

_SCRIPT_HINT_KEYS: tuple[str, ...] = (
    "generating_script", "generated_by", "source_script", "script",
    "producer", "producer_script", "generator",
)
_SCRIPT_HINT_RE = re.compile(
    r"(?:generated\s+by|produced\s+by|source[:=])\s*['\"]?"
    r"([\w./\\-]+\.(?:py|R|jl|ipynb|sh|m))",
    re.IGNORECASE,
)

DEFAULT_REBUILD_BUDGET_SECONDS = 130.0
# 5845cc6d — Phase 1 (parallel hashing) gets at most this fraction of the
# overall budget, measured from rebuild()'s own start (not from when Phase 1
# begins). On a cold multi-hundred-thousand-file tree the walk + staleness
# stat loop alone can consume a large chunk of the budget before Phase 1 even
# starts; without its own sub-deadline, Phase 1 would then consume ALL of
# whatever remained, leaving Phase 2 zero time to persist anything it
# computed -- every call would make zero forward progress, forever. Splitting
# the budget guarantees Phase 2 always gets a real (roughly equal) share to
# write whatever Phase 1 managed to finish, so every rebuild() call commits
# some real progress instead of only the last one (if it ever arrives).
_PHASE1_BUDGET_FRACTION = 0.5
MERIDIAN_NOTES_FILENAME = "MERIDIAN_NOTES.md"


@dataclass
class NpyMetadata:
    """Shape/dtype/size/mtime for one .npy file -- no array content ever read."""

    path: str
    shape: tuple[int, ...] | None
    dtype: str | None
    size_bytes: int | None
    modified_at: float | None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def npy_metadata(path: str) -> NpyMetadata:
    """Read header metadata from a .npy file without loading the full array."""
    try:
        st = os.stat(path)
        size_bytes: int | None = st.st_size
        modified_at: float | None = st.st_mtime
    except OSError as exc:
        return NpyMetadata(
            path=path, shape=None, dtype=None,
            size_bytes=None, modified_at=None, error=str(exc),
        )
    try:
        import numpy  # noqa: PLC0415 -- optional
        arr = numpy.load(path, mmap_mode="r")
        try:
            shape = tuple(int(d) for d in arr.shape)
            dtype = str(arr.dtype)
        finally:
            del arr
        return NpyMetadata(path=path, shape=shape, dtype=dtype,
                           size_bytes=size_bytes, modified_at=modified_at)
    except Exception as exc:  # noqa: BLE001
        _log.debug("npy_metadata failed for %r", path, exc_info=True)
        return NpyMetadata(path=path, shape=None, dtype=None,
                           size_bytes=size_bytes, modified_at=modified_at,
                           error=str(exc))


def _normalize_output_path(path: Any) -> str:
    """Canonical cross-platform path for equality matching."""
    if not isinstance(path, str):
        return ""
    s = path.strip()
    if not s:
        return ""
    try:
        s = os.path.abspath(s)
    except (OSError, ValueError):
        pass
    return os.path.normcase(os.path.normpath(s)).replace("\\", "/")


def _classify_suffix(path: str) -> str:
    suffix = os.path.splitext(path)[1].lower()
    if suffix in _TEXT_CONTENT_SUFFIXES:
        return "text_content"
    if suffix in _METADATA_ONLY_SUFFIXES:
        return "metadata_only"
    return "binary_metadata"


def _read_text_capped(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read(_MAX_CONTENT_CHARS)


def _sha256_file(path: str) -> str | None:
    try:
        h = hashlib.sha256()
        with open(path, "rb", buffering=0) as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _infer_generating_script_from_text(text: str) -> str | None:
    m = _SCRIPT_HINT_RE.search(text)
    return m.group(1) if m else None


def _infer_generating_script_from_obj(obj: Any) -> str | None:
    if isinstance(obj, dict):
        for key in _SCRIPT_HINT_KEYS:
            val = obj.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return None


def _extract_csv(text: str) -> tuple[list[str] | None, str | None]:
    columns: list[str] | None = None
    try:
        reader = csv_mod.reader(io.StringIO(text))
        for row in reader:
            columns = [c.strip() for c in row]
            break
    except Exception:  # noqa: BLE001
        columns = None
    return columns, _infer_generating_script_from_text(text)


def _extract_json(text: str) -> tuple[list[str] | None, str | None]:
    keys: list[str] | None = None
    script: str | None = None
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            keys = [str(k) for k in obj.keys()]
        script = _infer_generating_script_from_obj(obj)
    except Exception:  # noqa: BLE001
        keys = None
    if script is None:
        script = _infer_generating_script_from_text(text)
    return keys, script


@dataclass
class FileFingerprint:
    """Cheap content-derived signature for one output file."""

    path: str
    kind: str
    csv_columns: list[str] | None = None
    json_keys: list[str] | None = None
    generating_script: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def file_fingerprint(path: str) -> FileFingerprint:
    """Compute a cheap FileFingerprint for one output file."""
    kind = _classify_suffix(path)
    if kind != "text_content":
        return FileFingerprint(path=path, kind=kind)
    try:
        text = _read_text_capped(path)
    except OSError:
        return FileFingerprint(path=path, kind=kind)
    suffix = os.path.splitext(path)[1].lower()
    if suffix == ".csv":
        columns, script = _extract_csv(text)
        return FileFingerprint(path=path, kind=kind,
                               csv_columns=columns, generating_script=script)
    keys, script = _extract_json(text)
    return FileFingerprint(path=path, kind=kind,
                           json_keys=keys, generating_script=script)


def _content_for_fts(path: str, fingerprint: FileFingerprint) -> str:
    """The text body for the FTS content column."""
    terms: list[str] = [os.path.basename(path)]
    if fingerprint.csv_columns:
        terms.extend(fingerprint.csv_columns)
    if fingerprint.json_keys:
        terms.extend(fingerprint.json_keys)
    if fingerprint.generating_script:
        terms.append(fingerprint.generating_script)
    header = " ".join(terms)
    if fingerprint.kind != "text_content":
        return header
    try:
        body = _read_text_capped(path)
    except OSError:
        body = ""
    return f"{header}\n{body}"


def archival_candidate(path: str) -> bool:
    """Stage-1 filename heuristic: is this a candidate archival copy?"""
    base = os.path.basename(path)
    stem = os.path.splitext(base)[0]
    if base.startswith("_"):
        return True
    return bool(_ARCHIVAL_SUFFIX_RE.search(stem))


def _canonical_name(path: str) -> str:
    """Derive the canonical (non-archival) twin name for ``path``.

    Uses ``posixpath`` (not ``os.path``) so the result is always
    forward-slash-separated regardless of host OS -- these are logical
    output-tree paths, not real filesystem paths, and the "deterministic
    output" security requirement means the same input must produce the
    same string on Windows and POSIX alike.
    """
    directory = posixpath.dirname(path)
    base = posixpath.basename(path)
    stem, ext = posixpath.splitext(base)
    stem = _ARCHIVAL_SUFFIX_RE.sub("", stem)
    if base.startswith("_"):
        stem = stem.lstrip("_")
    return posixpath.join(directory, f"{stem}{ext}") if directory else f"{stem}{ext}"


@dataclass
class ArchivalClassification:
    path: str
    is_archival: bool
    canonical_path: str | None = None
    reason: str = ""


def classify_canonical_archival(
    paths: list[str], *, hasher: Callable[[str], str | None] = _sha256_file,
) -> dict[str, ArchivalClassification]:
    """Two-stage canonical-vs-archival classification.  Deterministic output
    order: keys follow the input ``paths`` list order (caller sorts them)."""
    path_set = set(paths)
    _hash_cache: dict[str, str | None] = {}

    def _h(p: str) -> str | None:
        if p not in _hash_cache:
            _hash_cache[p] = hasher(p)
        return _hash_cache[p]

    out: dict[str, ArchivalClassification] = {}
    for path in paths:
        if not archival_candidate(path):
            out[path] = ArchivalClassification(
                path=path, is_archival=False,
                reason="not a name-pattern candidate",
            )
            continue
        twin = _canonical_name(path)
        if twin == path or twin not in path_set:
            out[path] = ArchivalClassification(
                path=path, is_archival=False,
                reason="archival name pattern but no canonical twin present",
            )
            continue
        cand_hash = _h(path)
        twin_hash = _h(twin)
        if cand_hash is not None and twin_hash is not None and cand_hash == twin_hash:
            out[path] = ArchivalClassification(
                path=path, is_archival=True, canonical_path=twin,
                reason="SHA-256 identical to canonical twin",
            )
        else:
            out[path] = ArchivalClassification(
                path=path, is_archival=False, canonical_path=twin,
                reason="archival name pattern but content DIFFERS from twin",
            )
    return out


@dataclass
class OutputRow:
    """One row of the persistent outputs_index table."""

    path: str
    content: str
    mtime: float | None
    sha256: str | None
    size: int | None
    generating_script: str | None
    kind: str = ""
    is_archival: bool = False
    canonical_path: str | None = None
    csv_columns: list[str] | None = None
    json_keys: list[str] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_output_rows(
    outputs_dir: str, *, hasher: Callable[[str], str | None] = _sha256_file,
) -> list[OutputRow]:
    """Walk ``outputs_dir`` with secret-file exclusion and build OutputRows."""
    paths = _iter_safe_output_files(outputs_dir)
    classifications = classify_canonical_archival(paths, hasher=hasher)
    rows: list[OutputRow] = []
    for path in paths:
        fp = file_fingerprint(path)
        try:
            st = os.stat(path)
            size: int | None = st.st_size
            mtime: float | None = st.st_mtime
        except OSError:
            size = mtime = None
        cls = classifications.get(path)
        rows.append(OutputRow(
            path=path,
            content=_content_for_fts(path, fp),
            mtime=mtime,
            sha256=hasher(path),
            size=size,
            generating_script=fp.generating_script,
            kind=fp.kind,
            is_archival=bool(cls and cls.is_archival),
            canonical_path=(cls.canonical_path if cls else None),
            csv_columns=fp.csv_columns,
            json_keys=fp.json_keys,
        ))
    return rows


# ---------------------------------------------------------------------------
# Per-file pre-analysis result (used by parallel rebuild pipeline)
# ---------------------------------------------------------------------------

@dataclass
class _FileAnalysis:
    """Pre-computed, read-only analysis for one stale output file.

    Populated by worker threads (before the write lock is taken) so that the
    locked DB-write path can build an :class:`OutputRow` without doing any I/O.
    All fields are safe to read from any thread; no DB access is performed.
    """

    path: str
    fingerprint: FileFingerprint
    mtime: float | None
    size: int | None
    sha256: str | None


def _analyse_file(path: str, hasher: Callable[[str], str | None]) -> "_FileAnalysis":
    """Read-only per-file analysis: stat + fingerprint + hash.

    Designed to run in a :class:`concurrent.futures.ThreadPoolExecutor` worker.
    Pure I/O + CPU with no shared mutable state -- safe to call concurrently
    for different paths.  The GIL is released during the file-read portions,
    so hashing a large file (e.g. a 5.9 MB sweep_results.json) overlaps with
    hashing and parsing other files.
    """
    fp = file_fingerprint(path)
    try:
        st = os.stat(path)
        size: int | None = st.st_size
        mtime: float | None = st.st_mtime
    except OSError:
        size = mtime = None
    sha = hasher(path)
    return _FileAnalysis(path=path, fingerprint=fp, mtime=mtime, size=size, sha256=sha)


# ---------------------------------------------------------------------------
# Persistent DuckDB FTS index with write locking
# ---------------------------------------------------------------------------

_MAX_CACHED_INDEXES = 32
_index_cache_lock = threading.Lock()
_index_cache: OrderedDict[str, "OutputsFtsIndex"] = OrderedDict()


class OutputsFtsIndex:
    """Persistent DuckDB FTS index over a local outputs directory.

    Same logic as the main-repo OutputsFtsIndex, but:
    - file walks use :func:`_iter_safe_output_files` (secret exclusion)
    - DuckDB write operations are serialised through :class:`IndexFileLock`
    - connection is not guarded by a separate threading.RLock (the
      IndexFileLock covers both cross-thread and cross-process exclusion)
    """

    _COLUMNS = (
        "path", "content", "mtime", "sha256", "size", "generating_script",
        "kind", "is_archival", "canonical_path", "csv_columns", "json_keys",
    )

    def __init__(
        self,
        outputs_dir: str,
        *,
        db_path: str = ":memory:",
        connection: Any = None,
        hasher: Callable[[str], str | None] = _sha256_file,
    ) -> None:
        self.outputs_dir = outputs_dir
        self._db_path = db_path
        self._hasher = hasher
        self._write_lock = IndexFileLock(db_path)
        self._read_lock = threading.RLock()  # in-process query serialisation
        self._con = connection
        self._owns_con = connection is None
        self._fts_built = False
        self._manifest: dict[str, tuple[float | None, int | None]] = {}
        self._row_cache: dict[str, OutputRow] = {}
        self.last_rebuild_partial = False

    def _connect(self) -> Any:
        if self._con is None:
            import duckdb  # noqa: PLC0415
            self._con = duckdb.connect(self._db_path)
            # d9c76caa follow-up -- a fresh instance always assumed
            # _fts_built started False, forcing a full (expensive, non-
            # incremental) FTS rebuild on its very first call even when a
            # working FTS index already exists on disk from a PRIOR
            # process's successful rebuild (0c1a4349 made the index persist
            # across restarts, but this in-memory flag didn't know that --
            # every fresh MCP server spawn paid the full-table-rebuild tax
            # again regardless). Detect an existing FTS schema so a fresh
            # process reuses it (possibly slightly stale -- same trade-off
            # as the deadline-based skip in rebuild()) instead of always
            # rebuilding from scratch on process restart.
            try:
                existing = self._con.execute(
                    "SELECT 1 FROM information_schema.schemata "
                    "WHERE schema_name = 'fts_main_outputs_index'"
                ).fetchone()
                if existing is not None:
                    self._fts_built = True
            except Exception:  # noqa: BLE001
                _log.debug(
                    "OutputsFtsIndex._connect: FTS schema probe failed",
                    exc_info=True,
                )
        return self._con

    def _ensure_schema(self, con: Any) -> None:
        con.execute(
            "CREATE TABLE IF NOT EXISTS outputs_index ("
            "path VARCHAR PRIMARY KEY, content VARCHAR, mtime DOUBLE, "
            "sha256 VARCHAR, size BIGINT, generating_script VARCHAR, "
            "kind VARCHAR, is_archival BOOLEAN, canonical_path VARCHAR, "
            "csv_columns VARCHAR, json_keys VARCHAR)"
        )
        con.execute(
            "CREATE TABLE IF NOT EXISTS annotations ("
            "path VARCHAR NOT NULL, note VARCHAR NOT NULL, "
            "run_params_json VARCHAR, "
            "created_at DOUBLE NOT NULL, updated_at DOUBLE NOT NULL, "
            "source VARCHAR NOT NULL DEFAULT 'tool')"
        )

    def add_annotation(
        self,
        path: str,
        note: str,
        *,
        run_params: dict[str, Any] | None = None,
        source: str = "tool",
    ) -> dict[str, Any]:
        """Upsert an annotation.  Thread-safe via write lock."""
        with self._write_lock:
            return self._add_annotation_locked(
                path, note, run_params=run_params, source=source,
            )

    def _add_annotation_locked(
        self,
        path: str,
        note: str,
        *,
        run_params: dict[str, Any] | None = None,
        source: str = "tool",
    ) -> dict[str, Any]:
        """Same as :meth:`add_annotation` but assumes ``self._write_lock`` is
        ALREADY held by the caller (e.g. :meth:`rebuild`).  ``IndexFileLock``
        is not reentrant, so calling ``add_annotation`` from inside a
        ``with self._write_lock:`` block deadlocks -- callers that already
        hold the lock must use this instead."""
        now = time.time()
        run_params_json = json.dumps(run_params) if run_params else None
        try:
            con = self._connect()
            self._ensure_schema(con)
            con.execute(
                "DELETE FROM annotations WHERE path = ? AND source = ?",
                [path, source],
            )
            con.execute(
                "INSERT INTO annotations (path, note, run_params_json, "
                "created_at, updated_at, source) VALUES (?, ?, ?, ?, ?, ?)",
                [path, note, run_params_json, now, now, source],
            )
        except Exception as exc:  # noqa: BLE001
            _log.debug("add_annotation failed for %r", path, exc_info=True)
            return {"error": str(exc)}
        return {
            "path": path, "note": note, "run_params": run_params,
            "created_at": now, "updated_at": now, "source": source,
        }

    def get_annotations_for_path(self, path: str) -> list[dict[str, Any]]:
        """Return annotations for ``path`` and all ancestor directories."""
        target = _normalize_output_path(path)
        if not target:
            return []
        candidates: set[str] = {target}
        parts = target.split("/")
        for i in range(1, len(parts)):
            parent = "/".join(parts[:i])
            if parent:
                candidates.add(parent)
        with self._read_lock:
            try:
                con = self._connect()
                self._ensure_schema(con)
                relation = con.execute(
                    "SELECT path, note, run_params_json, created_at, "
                    "updated_at, source FROM annotations"
                )
                columns = [c[0] for c in relation.description]
                fetched = relation.fetchall()
            except Exception:  # noqa: BLE001
                _log.debug("get_annotations_for_path failed for %r", path,
                            exc_info=True)
                return []
        rows: list[dict[str, Any]] = []
        for row in fetched:
            rec = dict(zip(columns, row))
            norm = _normalize_output_path(rec.get("path") or "")
            if norm in candidates:
                rows.append({
                    "path": rec["path"],
                    "note": rec.get("note"),
                    "run_params": (
                        json.loads(rec["run_params_json"])
                        if rec.get("run_params_json") else None
                    ),
                    "created_at": rec.get("created_at"),
                    "updated_at": rec.get("updated_at"),
                    "source": rec.get("source"),
                })
        rows.sort(key=lambda r: (
            _normalize_output_path(r.get("path") or "") != target,
            -(r.get("updated_at") or 0),
        ))
        return rows

    def _ingest_meridian_notes(self, paths: list[str]) -> int:
        """Only ever called from within :meth:`rebuild`'s ``with self._write_lock:``
        block -- uses ``_add_annotation_locked`` (not ``add_annotation``) to avoid
        re-acquiring the non-reentrant write lock."""
        ingested = 0
        for p in paths:
            if os.path.basename(p) != MERIDIAN_NOTES_FILENAME:
                continue
            directory = os.path.dirname(p)
            try:
                with open(p, "r", encoding="utf-8", errors="replace") as fh:
                    text = fh.read(_MAX_CONTENT_CHARS)
            except OSError:
                _log.debug("_ingest_meridian_notes: could not read %r", p,
                            exc_info=True)
                continue
            if not text.strip():
                continue
            self._add_annotation_locked(directory, text, source=MERIDIAN_NOTES_FILENAME)
            ingested += 1
        return ingested

    def rebuild(
        self, *, max_seconds: float | None = DEFAULT_REBUILD_BUDGET_SECONDS,
    ) -> int:
        """Incrementally rebuild the table + FTS index.  Returns row count.

        Performance design (two-phase):

        Phase 1 -- parallel analysis, NO lock held:
          * Walk files and determine which are stale (mtime/size changed or
            absent from cache).
          * Run :func:`_analyse_file` for every stale path concurrently via a
            :class:`~concurrent.futures.ThreadPoolExecutor`.  Each worker does
            only read-only I/O (stat + read + hash) -- no DB access, no shared
            mutable state.  Results are collected and sorted by path for
            deterministic ordering.  Bounded by its own sub-deadline
            (``_PHASE1_BUDGET_FRACTION`` of ``max_seconds``, 5845cc6d) so it
            can never consume the whole budget and leave Phase 2 nothing to
            persist -- every call is guaranteed a real chance to commit
            whatever Phase 1 managed to finish, not just the first call that
            happens to complete before the deadline.
          * Run :func:`classify_canonical_archival` on all paths (also
            read-only) once the worker results are in.

        Phase 2 -- targeted write, write_lock held:
          * Update ``_manifest`` / ``_row_cache`` from the pre-computed results.
          * DELETE only the specific paths that were removed or changed.
          * INSERT only the new/changed rows as a single batch via
            ``executemany`` (much faster than one ``execute`` per row).
          * Rebuild the FTS index.

        The ``IndexFileLock`` (``self._write_lock``) is NOT reentrant.
        ``_ingest_meridian_notes`` -- called inside the lock -- must therefore
        use ``_add_annotation_locked`` (not ``add_annotation``).  That
        invariant is preserved here.
        """
        deadline = (None if max_seconds is None
                    else time.monotonic() + max_seconds)
        # 5845cc6d — Phase 1 gets its own, earlier sub-deadline so it can never
        # consume the entire budget and starve Phase 2 of time to persist
        # anything. Computed from the same start point as `deadline` (not from
        # when Phase 1 begins) so it also implicitly accounts for time already
        # spent on the walk + staleness-stat pass below.
        phase1_deadline = (None if max_seconds is None
                            else time.monotonic() + max_seconds * _PHASE1_BUDGET_FRACTION)

        # ------------------------------------------------------------------
        # Phase 1: read-only pre-analysis (no lock, may run in parallel)
        # ------------------------------------------------------------------
        all_paths: list[str] = (
            _iter_safe_output_files(self.outputs_dir)
            if os.path.isdir(self.outputs_dir) else []
        )

        path_set = set(all_paths)

        # Determine which paths are removed or stale (mtime/size changed).
        # We read _manifest without the lock here.  _manifest is only mutated
        # inside the write lock (Phase 2), so reading it here is safe as long
        # as no concurrent rebuild() is running -- which IndexFileLock prevents.
        removed_paths: set[str] = set(self._manifest) - path_set
        stale: list[str] = []
        stale_sigs: dict[str, tuple[float | None, int | None]] = {}
        for p in all_paths:
            try:
                st = os.stat(p)
                sig: tuple[float | None, int | None] = (st.st_mtime, st.st_size)
            except OSError:
                sig = (None, None)
            if self._manifest.get(p) != sig or p not in self._row_cache:
                stale.append(p)
                stale_sigs[p] = sig

        # Parallel per-file analysis for stale paths (fingerprint + hash + stat).
        # Workers run before the write lock is taken so heavy I/O (e.g. hashing
        # a 5.9 MB file) overlaps across files.  Results are collected into a
        # dict and processed in sorted order to guarantee determinism.
        precomputed: dict[str, _FileAnalysis] = {}
        if stale:
            # Cap workers at 8 to avoid spawning hundreds of threads for a huge
            # outputs tree; I/O is the bottleneck, not CPU count.
            max_workers = min(8, len(stale))
            # Not a `with` block: on a deadline breach we need
            # shutdown(wait=False, cancel_futures=True) so still-queued work is
            # dropped immediately. A `with` block's default __exit__ calls
            # shutdown(wait=True), which would block until every submitted
            # future finishes -- silently defeating the deadline check below on
            # a large/cold tree (this was the actual bug: Phase 1 had no
            # deadline check at all and always ran to completion).
            pool = concurrent.futures.ThreadPoolExecutor(
                max_workers=max_workers,
                thread_name_prefix="meridian_outputs_analyse",
            )
            phase1_deadline_hit = False
            try:
                futures = {
                    pool.submit(_analyse_file, p, self._hasher): p
                    for p in stale
                }
                for fut in concurrent.futures.as_completed(futures):
                    if phase1_deadline is not None and time.monotonic() > phase1_deadline:
                        phase1_deadline_hit = True
                        break
                    try:
                        analysis = fut.result()
                        precomputed[analysis.path] = analysis
                    except Exception:  # noqa: BLE001
                        p = futures[fut]
                        _log.debug("_analyse_file failed for %r", p, exc_info=True)
            finally:
                if phase1_deadline_hit:
                    _log.debug(
                        "rebuild: Phase 1 budget exceeded with %d/%d files "
                        "analysed; cancelling remaining work",
                        len(precomputed), len(stale),
                    )
                    pool.shutdown(wait=False, cancel_futures=True)
                else:
                    pool.shutdown(wait=True)

        # classify_canonical_archival needs all paths (not just stale ones) to
        # detect archival twins correctly.  It is read-only, so it runs here
        # before the lock.  Only computed when there are stale or removed paths
        # (i.e. when something actually changed) to avoid unnecessary hashing.
        classifications: dict[str, ArchivalClassification] = {}
        if stale or removed_paths:
            # Reuse hashes already computed by workers where possible so
            # classify_canonical_archival doesn't read the same file twice.
            _precomp_hashes: dict[str, str | None] = {
                p: a.sha256 for p, a in precomputed.items()
            }

            stale_set = set(stale)

            def _cached_hasher(path: str) -> str | None:
                if path in _precomp_hashes:
                    return _precomp_hashes[path]
                # Non-stale (mtime/size unchanged) archival candidates already
                # have a known-good hash from a prior rebuild -- reuse it
                # instead of re-hashing the file from disk again this run.
                # (`path not in stale_set` structurally guarantees a row_cache
                # hit here: staleness is defined as "manifest mismatch OR no
                # row_cache entry", so a non-stale path always has one.)
                if path not in stale_set:
                    return self._row_cache[path].sha256
                # 5845cc6d — genuinely stale but Phase 1 didn't get to it
                # before its own sub-deadline. Do NOT fall back to a fresh
                # synchronous self._hasher(path) call here: classify_canonical
                # _archival has no deadline check of its own, so looping
                # through potentially thousands of un-analysed stale files
                # would blow the overall budget just as badly as the original
                # Phase-1-blocks-forever bug this fix targets. None is a safe
                # default ("not confirmed archival this round") -- the file
                # gets a proper hash once its turn in a future Phase 1 comes up.
                return None

            classifications = classify_canonical_archival(
                all_paths, hasher=_cached_hasher,
            )

        # ------------------------------------------------------------------
        # Phase 2: targeted write (write_lock held)
        # ------------------------------------------------------------------
        with self._write_lock:
            self._ingest_meridian_notes(all_paths)
            rows, changed, paths_to_delete, new_rows = (
                self._apply_precomputed(
                    all_paths, path_set, removed_paths, stale, stale_sigs,
                    precomputed, classifications, deadline,
                )
            )
            if changed:
                try:
                    con = self._connect()
                    self._ensure_schema(con)
                    # Delete only the paths that were removed or re-computed.
                    if paths_to_delete:
                        con.executemany(
                            "DELETE FROM outputs_index WHERE path = ?",
                            [[p] for p in paths_to_delete],
                        )
                    # Batch-insert only the new/changed rows.
                    if new_rows:
                        con.executemany(
                            "INSERT INTO outputs_index VALUES "
                            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            [
                                [
                                    r.path, r.content, r.mtime, r.sha256, r.size,
                                    r.generating_script, r.kind, r.is_archival,
                                    r.canonical_path,
                                    json.dumps(r.csv_columns) if r.csv_columns else None,
                                    json.dumps(r.json_keys) if r.json_keys else None,
                                ]
                                for r in new_rows
                            ],
                        )
                    # d9c76caa follow-up -- _rebuild_fts() is a FULL,
                    # non-incremental rebuild (DuckDB has no incremental FTS
                    # support in the installed version, per b8314850) whose
                    # cost grows with the TOTAL row count and has no deadline
                    # check of its own. On a huge cold tree this alone can
                    # push the overall rebuild() call well past its budget
                    # even after Phase 1/Phase 2 correctly stop on time (the
                    # real-MCP-path validation for this item showed exactly
                    # that: search_outputs still exceeded the ~4min external
                    # ceiling with Phase 1/2 fixed). Skip the rebuild when the
                    # deadline has already passed AND a usable FTS index
                    # already exists from a prior successful rebuild --
                    # search() only rebuilds FTS itself when none exists yet
                    # (_fts_built is False), so skipping here never leaves
                    # search() with nothing to query, only slightly-stale
                    # results (missing this round's newest rows) until a
                    # future call with enough remaining budget completes a
                    # fresh one.
                    skip_fts = (
                        self._fts_built
                        and deadline is not None
                        and time.monotonic() > deadline
                    )
                    if skip_fts:
                        self.last_rebuild_partial = True
                    else:
                        self._rebuild_fts(con)
                except Exception:  # noqa: BLE001
                    _log.debug("OutputsFtsIndex.rebuild failed", exc_info=True)
            return len(rows)

    def _apply_precomputed(
        self,
        all_paths: list[str],
        path_set: set[str],
        removed_paths: set[str],
        stale: list[str],
        stale_sigs: dict[str, tuple[float | None, int | None]],
        precomputed: dict[str, "_FileAnalysis"],
        classifications: dict[str, ArchivalClassification],
        deadline: float | None,
    ) -> tuple[list[OutputRow], bool, list[str], list[OutputRow]]:
        """Apply pre-computed per-file analysis to the in-memory cache.

        Called from inside :meth:`rebuild`'s ``with self._write_lock:`` block.
        Returns ``(all_rows, changed, paths_to_delete, new_rows)`` where:
        - ``all_rows`` -- the full current row list for all indexed paths
        - ``changed``  -- True if any DB write is needed
        - ``paths_to_delete`` -- paths to DELETE from the DB (removed + stale)
        - ``new_rows`` -- OutputRow objects to INSERT (stale paths with fresh data)

        The split between ``paths_to_delete`` and ``new_rows`` enables the
        targeted delete + batched insert that replaces the old DELETE-all /
        reinsert-all pattern.
        """
        self.last_rebuild_partial = False
        changed = False
        paths_to_delete: list[str] = []
        stale_set = set(stale)

        # Apply removals.
        for p in sorted(removed_paths):  # sorted for determinism
            self._manifest.pop(p, None)
            self._row_cache.pop(p, None)
            paths_to_delete.append(p)
            changed = True

        # Apply stale results from precomputed analysis, honouring the deadline.
        # Process in sorted path order so that partial (deadline-expired) rebuilds
        # produce a deterministic prefix.
        new_rows: list[OutputRow] = []
        for p in sorted(stale):
            if deadline is not None and time.monotonic() > deadline:
                self.last_rebuild_partial = True
                break
            analysis = precomputed.get(p)
            if analysis is None:
                # Worker failed for this path; fall back to synchronous analysis.
                try:
                    analysis = _analyse_file(p, self._hasher)
                except Exception:  # noqa: BLE001
                    _log.debug("_apply_precomputed: fallback _analyse_file failed for %r", p,
                                exc_info=True)
                    continue
            fp = analysis.fingerprint
            mtime = analysis.mtime
            size = analysis.size
            cls = classifications.get(p)
            row = OutputRow(
                path=p,
                content=_content_for_fts(p, fp),
                mtime=mtime,
                sha256=analysis.sha256,
                size=size,
                generating_script=fp.generating_script,
                kind=fp.kind,
                is_archival=bool(cls and cls.is_archival),
                canonical_path=(cls.canonical_path if cls else None),
                csv_columns=fp.csv_columns,
                json_keys=fp.json_keys,
            )
            self._row_cache[p] = row
            self._manifest[p] = stale_sigs.get(p, (mtime, size))
            paths_to_delete.append(p)  # delete old row (if any) before reinserting
            new_rows.append(row)
            changed = True

        # Update archival metadata on non-stale cached rows when the archival
        # classification changed (e.g. a twin file was added or removed).
        if classifications:
            for p in all_paths:
                if p in stale_set:
                    continue
                row = self._row_cache.get(p)
                if row is None:
                    continue
                cls = classifications.get(p)
                new_is_archival = bool(cls and cls.is_archival)
                new_canonical = cls.canonical_path if cls else None
                if (row.is_archival != new_is_archival
                        or row.canonical_path != new_canonical):
                    row.is_archival = new_is_archival
                    row.canonical_path = new_canonical
                    # This row changed -- include it in the targeted update.
                    paths_to_delete.append(p)
                    new_rows.append(row)
                    changed = True

        all_rows = [self._row_cache[p] for p in all_paths if p in self._row_cache]
        return all_rows, changed, paths_to_delete, new_rows

    def invalidate(self, path: str) -> None:
        """Force ``path`` to be re-hashed on next rebuild."""
        with self._write_lock:
            self._row_cache.pop(path, None)

    def _rebuild_fts(self, con: Any) -> None:
        con.execute("INSTALL fts")
        con.execute("LOAD fts")
        con.execute(
            "PRAGMA create_fts_index("
            "'outputs_index', 'path', 'content', "
            "stemmer = 'porter', stopwords = 'none', overwrite = 1)"
        )
        self._fts_built = True

    def search(
        self, query: str, *, limit: int = 10, include_archival: bool = True,
    ) -> list[dict[str, Any]]:
        """BM25 search over the FTS index.  Best-effort: errors yield [].

        Filtering (``bm25 IS NOT NULL``), ordering (``ORDER BY bm25 DESC``), and
        row-count limiting (``LIMIT``) are all pushed into the SQL query so that
        only the top-N matching rows are transferred from DuckDB to Python.  A
        Python sort is still applied afterwards to honour the archival score
        penalty (archival rows get bm25 * 0.5) without adding complexity to the
        SQL.
        """
        q = (query or "").strip()
        if not q:
            return []
        safe_limit = max(1, int(limit))
        with self._read_lock:
            try:
                con = self._connect()
                self._ensure_schema(con)
                if not self._fts_built:
                    self._rebuild_fts(con)
                sql = (
                    "SELECT path, content, mtime, sha256, size, generating_script, "
                    "kind, is_archival, canonical_path, csv_columns, json_keys, "
                    "fts_main_outputs_index.match_bm25(path, ?) AS bm25 "
                    "FROM outputs_index "
                    "WHERE fts_main_outputs_index.match_bm25(path, ?) IS NOT NULL "
                    "ORDER BY bm25 DESC "
                    "LIMIT ?"
                )
                relation = con.execute(sql, [q, q, safe_limit])
                columns = [c[0] for c in relation.description]
                fetched = relation.fetchall()
            except Exception:  # noqa: BLE001
                _log.debug("OutputsFtsIndex.search failed", exc_info=True)
                return []
        hits: list[dict[str, Any]] = []
        for row in fetched:
            rec = dict(zip(columns, row))
            bm25 = rec.get("bm25")
            if bm25 is None:
                continue
            is_arch = bool(rec.get("is_archival"))
            if is_arch and not include_archival:
                continue
            score = float(bm25) * (0.5 if is_arch else 1.0)
            hits.append({
                "path": rec["path"],
                "score": score,
                "bm25": float(bm25),
                "is_archival": is_arch,
                "canonical_path": rec.get("canonical_path"),
                "kind": rec.get("kind"),
                "generating_script": rec.get("generating_script"),
                "csv_columns": (
                    json.loads(rec["csv_columns"]) if rec.get("csv_columns") else None
                ),
                "json_keys": (
                    json.loads(rec["json_keys"]) if rec.get("json_keys") else None
                ),
                "size": rec.get("size"),
                "mtime": rec.get("mtime"),
            })
        hits.sort(key=lambda h: h["score"], reverse=True)
        return hits

    def resolve_output(self, file_path: str) -> dict[str, Any] | None:
        """Exact-match lookup of an output row by file path.

        The WHERE clause is pushed into SQL to avoid a full-table scan.
        ``_normalize_output_path`` lowercases and forward-slash-normalizes on
        Windows (``os.path.normcase`` + backslash replacement), so on Windows we
        apply the same transformation in SQL:
        ``lower(replace(path, '\\', '/')) = ?`` with the already-normalised
        target.  On POSIX the stored paths already use forward slashes and are
        case-sensitive, so a plain equality check suffices.
        """
        target = _normalize_output_path(file_path)
        if not target:
            return None
        import sys as _sys
        with self._read_lock:
            try:
                con = self._connect()
                self._ensure_schema(con)
                if _sys.platform == "win32":
                    # stored paths have backslashes + mixed case; target is
                    # already forward-slash + lowercase from normcase.
                    sql = (
                        "SELECT path, content, mtime, sha256, size, generating_script, "
                        "kind, is_archival, canonical_path, csv_columns, json_keys "
                        "FROM outputs_index "
                        "WHERE lower(replace(path, '\\', '/')) = ?"
                    )
                else:
                    sql = (
                        "SELECT path, content, mtime, sha256, size, generating_script, "
                        "kind, is_archival, canonical_path, csv_columns, json_keys "
                        "FROM outputs_index "
                        "WHERE path = ?"
                    )
                relation = con.execute(sql, [target])
                columns = [c[0] for c in relation.description]
                row = relation.fetchone()
            except Exception:  # noqa: BLE001
                _log.debug("OutputsFtsIndex.resolve_output failed",
                            exc_info=True)
                return None
        if row is None:
            return None
        rec = dict(zip(columns, row))
        return {
            "path": rec["path"],
            "generating_script": rec.get("generating_script"),
            "is_archival": bool(rec.get("is_archival")),
            "canonical_path": rec.get("canonical_path"),
            "sha256": rec.get("sha256"),
            "kind": rec.get("kind"),
            "size": rec.get("size"),
            "mtime": rec.get("mtime"),
            "csv_columns": (
                json.loads(rec["csv_columns"]) if rec.get("csv_columns") else None
            ),
            "json_keys": (
                json.loads(rec["json_keys"]) if rec.get("json_keys") else None
            ),
        }

    def close(self) -> None:
        with self._write_lock:
            if self._owns_con and self._con is not None:
                try:
                    self._con.close()
                except Exception:  # noqa: BLE001
                    _log.debug("OutputsFtsIndex.close failed", exc_info=True)
            if self._owns_con:
                self._con = None
                self._fts_built = False


# ---------------------------------------------------------------------------
# Module-level stateless API (what server.py's MCP tools call)
# ---------------------------------------------------------------------------

def _cache_key(outputs_dir: str) -> str:
    return _normalize_output_path(outputs_dir) or os.path.abspath(str(outputs_dir))


def _resolve_index_db_path(outputs_dir: str) -> str:
    """Return the on-disk DuckDB path for ``outputs_dir``'s index cache.

    The cache lives at ``<outputs_dir>/.meridian-outputs-cache/index.duckdb``
    so it travels with the data it indexes and stays out of unrelated
    directories.  :func:`ensure_gitignored` is called on the cache directory
    so it never gets committed.  On any failure to create the directory
    (permissions, read-only tree, etc.) this falls back to ``:memory:`` so a
    single bad outputs_dir degrades to the old (non-persistent) behaviour
    instead of raising.
    """
    cache_dir = os.path.join(outputs_dir, ".meridian-outputs-cache")
    try:
        os.makedirs(cache_dir, exist_ok=True)
        ensure_gitignored(cache_dir)
    except OSError:
        _log.debug(
            "_resolve_index_db_path: could not create cache dir %r, "
            "falling back to :memory:", cache_dir, exc_info=True,
        )
        return ":memory:"
    return os.path.join(cache_dir, "index.duckdb")


def _get_cached_index(outputs_dir: str) -> OutputsFtsIndex:
    """Look up (or create) the cached OutputsFtsIndex for a directory."""
    key = _cache_key(outputs_dir)
    with _index_cache_lock:
        idx = _index_cache.pop(key, None)
        if idx is None:
            idx = OutputsFtsIndex(outputs_dir, db_path=_resolve_index_db_path(outputs_dir))
        _index_cache[key] = idx
        while len(_index_cache) > _MAX_CACHED_INDEXES:
            _, evicted = _index_cache.popitem(last=False)
            evicted.close()
        return idx


def search_outputs(
    outputs_dir: str,
    query: str,
    *,
    limit: int = 10,
    include_archival: bool = True,
    max_seconds: float | None = DEFAULT_REBUILD_BUDGET_SECONDS,
) -> dict[str, Any]:
    """BM25 search over a local outputs tree.

    Fully local -- no hosted call.  Reuses a cached incremental
    :class:`OutputsFtsIndex` per ``outputs_dir``.  Secret files are never
    indexed (filtered by :func:`is_secret_path` before any file is walked).
    """
    result: dict[str, Any] = {
        "outputs_dir": outputs_dir,
        "query": query,
        "hits": [],
        "total_indexed": 0,
    }
    if not query or not str(query).strip():
        result["error"] = "query is required"
        return result
    if not os.path.isdir(outputs_dir):
        result["error"] = f"outputs_dir does not exist: {outputs_dir}"
        return result
    index = _get_cached_index(outputs_dir)
    result["total_indexed"] = index.rebuild(max_seconds=max_seconds)
    hits = index.search(query, limit=limit, include_archival=include_archival)
    for hit in hits:
        hit["annotations"] = index.get_annotations_for_path(hit["path"])
    result["hits"] = hits
    if index.last_rebuild_partial:
        result["partial"] = True
    return result


def annotate_outputs(
    outputs_dir: str,
    path: str,
    note: str,
    *,
    run_params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Add/update an annotation for ``path`` in the outputs tree.

    Fully local -- no hosted call.
    """
    if not outputs_dir or not str(outputs_dir).strip():
        return {"error": "outputs_dir is required"}
    if not path or not str(path).strip():
        return {"error": "path is required"}
    if not note or not str(note).strip():
        return {"error": "note is required"}
    index = _get_cached_index(outputs_dir)
    return index.add_annotation(path, note, run_params=run_params, source="tool")


def classify_outputs(
    paths: list[str],
) -> dict[str, Any]:
    """Classify a list of file paths as canonical or archival.

    Fully local -- no hosted call.  Wraps :func:`classify_canonical_archival`.
    Returns a list of classification records (path, is_archival, canonical_path,
    reason) in stable sorted order.
    """
    sorted_paths = sorted(str(p) for p in paths if p)
    results = classify_canonical_archival(sorted_paths)
    return {
        "total": len(sorted_paths),
        "classifications": [
            {
                "path": c.path,
                "is_archival": c.is_archival,
                "canonical_path": c.canonical_path,
                "reason": c.reason,
            }
            for c in (results[p] for p in sorted_paths)
        ],
    }


def resolve_figure_output(
    outputs_dir: str, file_path: str,
) -> dict[str, Any] | None:
    """Exact-path lookup for a figure's source output file.

    Fully local -- no hosted call.
    """
    if not file_path or not str(file_path).strip():
        return None
    if not os.path.isdir(outputs_dir):
        return None
    index = _get_cached_index(outputs_dir)
    index.rebuild()
    return index.resolve_output(file_path)
