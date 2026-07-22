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
import shutil
import subprocess
import threading
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
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

# e8a2f710 -- ONE precompiled regex alternation instead of iterating
# fnmatch.fnmatch() once per pattern (~80 patterns) for every file. Measured
# live against a real 16,000-file tree: the sequential-fnmatch loop cost
# ~2.36s; this single combined regex does the identical check in ~0.20s
# (~12x faster), verified to produce byte-identical results across the full
# 66k-file tree. fnmatch.translate() already anchors each alternative (ends
# in \Z), so joining N translated patterns with "|" and doing one .match()
# is equivalent to trying each pattern in turn -- not an approximation.
_SECRET_PATTERN_RE = re.compile(
    "|".join(fnmatch.translate(p) for p in _SECRET_PATTERNS_LOWER),
    re.IGNORECASE,
)


def is_secret_path(path: str) -> bool:
    """Return True if ``path`` matches any secret-file exclusion pattern.

    Only the BASENAME is checked (case-insensitive). This is the
    authoritative filter applied before ANY file content is read or indexed.
    It is deliberately conservative: false positives (a legitimate output file
    named ``token_counts.csv``) are rare and the user can rename; false
    negatives (accidentally indexing a .env file) are a security incident.

    e8a2f710 -- backed by one precompiled regex alternation (built once at
    import time from the same _SECRET_PATTERNS_LOWER list this always used)
    rather than looping fnmatch.fnmatch() per pattern -- ~12x faster on real
    trees, verified to produce identical results.

    This function is tested exhaustively in the package test suite.
    """
    return _SECRET_PATTERN_RE.match(os.path.basename(path)) is not None



# ---------------------------------------------------------------------------
# fd4dd661 -- user-configurable exclude patterns (gitignore-style, v1)
# ---------------------------------------------------------------------------
#
# Scope decision (documented per fd4dd661's own "your call on scope" note):
# v1 is a PRAGMATIC fnmatch-based glob list, not a full gitignore-spec
# parser.  Supported:
#   - A bare glob (e.g. "*.tmp", "big_sweep_*") matches the file/directory
#     BASENAME at any depth.
#   - A glob containing "/" (e.g. "cache/generated") matches against the
#     path RELATIVE to outputs_dir (forward-slash separated, regardless of
#     host OS -- same normalisation :func:`_canonical_name` already uses).
#   - A trailing "/" (e.g. "node_modules/") marks a DIRECTORY pattern: the
#     directory (and everything under it) is PRUNED from the walk entirely
#     -- not just filtered file-by-file -- which is what actually lets a
#     huge never-useful subtree be skipped cheaply. Matched the same way
#     (basename or relative path) with the trailing slash stripped first.
# Deliberately NOT supported in v1 (kept simple, documented rather than
# silently wrong): gitignore negation ("!pattern"), and per-directory
# .gitignore file discovery (this is one explicit pattern list, not scattered
# across the tree). Note fnmatch's "*" already matches across "/" boundaries
# (it has no filesystem awareness), which is MORE permissive than real
# gitignore's single "*" -- e.g. "cache/*" here also matches
# "cache/sub/file.txt", not just direct children. Use a trailing "/" pattern
# instead when a whole subtree should be pruned.
_EXCLUDE_PATTERNS_ENV_VAR = "MERIDIAN_OUTPUTS_EXCLUDE_PATTERNS"


def _default_exclude_patterns() -> tuple[str, ...]:
    """Resolve the default exclude-pattern list from the environment.

    Comma- or newline-separated glob list in
    ``MERIDIAN_OUTPUTS_EXCLUDE_PATTERNS``, e.g.
    ``"node_modules/,*.tmp,big_sweep_output/"``.  Empty/unset -> no patterns
    (unchanged default behaviour: only secret-file exclusion applies).
    Re-read on every call (cheap) rather than cached at import time, so a
    changed env var takes effect on the next :class:`OutputsFtsIndex`
    construction without a process restart -- same pattern as
    :func:`_default_max_workers` (acac2599).
    """
    raw = os.environ.get(_EXCLUDE_PATTERNS_ENV_VAR, "")
    if not raw.strip():
        return ()
    parts = re.split(r"[,\n]", raw)
    return tuple(p.strip() for p in parts if p.strip())


def _matches_exclude_pattern(
    basename: str, rel_path: str, patterns: tuple[str, ...],
) -> bool:
    """True if ``basename`` or ``rel_path`` (forward-slash, relative to
    outputs_dir) matches any glob in ``patterns``.  See the module-level
    comment above for the exact (deliberately simple, v1) matching rules.
    """
    for pat in patterns:
        p = pat.strip()
        if not p:
            continue
        core = p[:-1] if p.endswith("/") else p
        if not core:
            continue
        if fnmatch.fnmatch(basename, core) or fnmatch.fnmatch(rel_path, core):
            return True
    return False


def _walk_safe_output_files(
    outputs_dir: str, *, exclude_patterns: tuple[str, ...] = (),
):
    """Generator yielding regular files under ``outputs_dir`` that pass the
    secret-file exclusion filter AND the (optional) user exclude-pattern
    list, in deterministic (sorted-directories, sorted-files) order -- the
    same order :func:`_iter_safe_output_files` has always returned.

    This is a plain generator, which means pulling from it can be paused
    (simply stop calling ``next()``) and resumed later exactly where it left
    off, at zero extra cost.  That property is what makes
    :class:`_ResumableFileWalk` possible (6ba77ada) -- see its docstring.

    MERIDIAN_NOTES.md files are included (they are picked up for annotation
    ingestion in rebuild, not FTS content rows -- same behaviour as the
    original).  Hidden directories (names starting with ``.``) are pruned to
    avoid walking .git/.env directories.  ``exclude_patterns`` (fd4dd661) --
    directories matching a pattern are pruned the same way hidden
    directories are (never descended into); files matching a pattern are
    skipped, same as a secret-path match.

    e8a2f710 -- iterative os.scandir()-based stack traversal instead of
    os.walk(). os.walk() carries real per-directory overhead beyond the
    raw syscalls (generator bookkeeping, an extra is_dir() re-check for
    entries it already classified). Measured live against a real 66k-file
    tree: this walker (combined with the fast secret-pattern regex above)
    completes the full tree in ~5.6s vs ~17.3s for the previous
    os.walk()-based version (~3.1x faster) -- verified byte-for-byte
    identical output ordering against the old implementation before this
    replaced it. Traversal order is preserved exactly: a LIFO stack pops
    the most-recently-pushed directory first, and subdirectories are
    pushed in REVERSE sorted order so they pop out in forward sorted
    order -- the same sorted, depth-first, current-dir-files-before-
    subdirs order os.walk()'s own sorted-dirs recursion always produced.
    """
    stack: list[str] = [outputs_dir]
    while stack:
        root = stack.pop()
        try:
            with os.scandir(root) as it:
                entries = list(it)
        except OSError:
            continue
        dir_entries: list[os.DirEntry] = []
        file_entries: list[os.DirEntry] = []
        for entry in entries:
            try:
                entry_is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                continue
            if entry_is_dir:
                if entry.name.startswith("."):
                    continue
                if exclude_patterns:
                    rel = os.path.relpath(
                        entry.path, outputs_dir
                    ).replace("\\", "/")
                    if _matches_exclude_pattern(entry.name, rel, exclude_patterns):
                        _log.debug(
                            "outputs_local: pruning user-excluded dir %r",
                            entry.path,
                        )
                        continue
                dir_entries.append(entry)
            else:
                file_entries.append(entry)
        dir_entries.sort(key=lambda e: e.name)
        file_entries.sort(key=lambda e: e.name)
        stack.extend(e.path for e in reversed(dir_entries))
        for entry in file_entries:
            p = entry.path
            if is_secret_path(p):
                _log.debug("outputs_local: skipping secret-pattern file %r", p)
                continue
            if exclude_patterns:
                rel = os.path.relpath(p, outputs_dir).replace("\\", "/")
                if _matches_exclude_pattern(entry.name, rel, exclude_patterns):
                    _log.debug("outputs_local: skipping user-excluded file %r", p)
                    continue
            yield p


def _iter_safe_output_files(
    outputs_dir: str, *, exclude_patterns: tuple[str, ...] = (),
) -> list[str]:
    """Walk ``outputs_dir`` recursively, returning regular files that pass the
    secret-file exclusion filter (and the optional ``exclude_patterns``
    user list, fd4dd661), sorted for deterministic ordering.

    This blocks until the walk completes -- fine for a one-shot, small-tree
    caller (e.g. :func:`build_output_rows`), but NOT deadline-aware: on a
    large tree (tens of thousands of files) this call alone can take far
    longer than any reasonable budget.  :meth:`OutputsFtsIndex.rebuild` does
    NOT call this directly for that reason -- it uses
    :class:`_ResumableFileWalk` instead, which bounds each walk increment by
    ``rebuild()``'s own deadline (6ba77ada).
    """
    return list(
        _walk_safe_output_files(outputs_dir, exclude_patterns=exclude_patterns)
    )


class _ResumableFileWalk:
    """Deadline-bounded, resumable wrapper around :func:`_walk_safe_output_files`.

    6ba77ada -- the plain, blocking :func:`_iter_safe_output_files` walk has
    zero deadline awareness: on a large tree it can by itself take far
    longer than :meth:`OutputsFtsIndex.rebuild`'s entire ``max_seconds``
    budget, starving Phase 1/Phase 2 of any chance to run at all (confirmed
    live: ~11s to walk a 70,000-file tree vs. the 5s default budget -- every
    call returned 0 rows, forever, because by the time the walk finished both
    Phase 1's own sub-deadline, 5845cc6d, AND the overall deadline had
    already passed). :meth:`drain` pulls paths from the underlying generator
    only until ``deadline`` (a ``time.monotonic()`` value) is reached, so a
    single call can never consume more than its allotted share of the
    budget. Because the wrapped object is a Python generator, pausing and
    resuming it costs nothing extra: the next :meth:`drain` call continues
    exactly where the previous one stopped, so a full walk of a huge tree is
    amortised across as many ``rebuild()`` calls as it needs instead of
    blocking the first one.
    """

    # 6ba77ada -- caps how many paths a single drain() can hand back, in
    # addition to the time-based deadline. Enumerating a directory entry is
    # cheap enough that a deadline-bound drain() can hand back tens of
    # thousands of paths in one go on a fast disk -- far more than Phase 1/2
    # (real per-file I/O: stat + read + hash + DB write) can realistically
    # analyse and persist within the SAME call's remaining budget. Since the
    # walk only resumes once `self._pending_stale` (the backlog this batch
    # feeds) is empty (see rebuild()'s Phase 0 throttle), an oversized batch
    # here directly becomes an oversized backlog that then takes many calls
    # to work down -- not because the walk itself is slow, but because it
    # was allowed to run so far ahead of the analysis/write stages. Capping
    # the batch keeps each throttle cycle's backlog proportionate to what
    # Phase 1/2 can plausibly clear in a comparable amount of time.
    _MAX_BATCH = 2000

    def __init__(
        self, outputs_dir: str, *, exclude_patterns: tuple[str, ...] = (),
    ) -> None:
        self._iterator = _walk_safe_output_files(
            outputs_dir, exclude_patterns=exclude_patterns,
        )
        self.exhausted = False

    def drain(self, deadline: float | None) -> list[str]:
        """Pull paths until the walk is exhausted, ``deadline`` passes, or
        ``_MAX_BATCH`` paths have been collected -- whichever comes first.

        Always pulls at least one path per call (if any remain) before
        checking the deadline, so a single ``drain()`` call can never spin
        forever without making any progress even when ``deadline`` is
        already in the past.
        """
        found: list[str] = []
        if self.exhausted:
            return found
        for path in self._iterator:
            found.append(path)
            if len(found) >= self._MAX_BATCH:
                return found
            if deadline is not None and time.monotonic() > deadline:
                return found
        self.exhausted = True
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
# e8a2f710 -- cap how much of a file's content the script-hint regex actually
# scans. Provenance comments/headers this pattern targets ("generated by
# X.py", "source: X.py") are a header/near-top convention when they exist at
# all -- there's no realistic case for one appearing deep inside a 200,000-
# char JSON/CSV body. Measured live on a real 2239-file text-content batch:
# unbounded search (up to _MAX_CONTENT_CHARS=200,000 chars/file) cost 2.844s;
# bounded to the first 8192 chars cost 0.265s (~10.7x faster) with IDENTICAL
# results on every file (0 real files in that batch used this phrase pattern
# at all, at any position -- confirmed by diffing full-text vs bounded
# output, not assumed). This was the single largest piece of Phase 1's CPU
# cost once hashing (already fast, xxhash) and JSON/CSV parsing themselves
# were isolated and ruled out.
_SCRIPT_HINT_SEARCH_CHARS = 8192

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


def _xxh3_file(path: str) -> str | None:
    """Fast, non-cryptographic content hash for archival-duplicate detection.

    984b237c -- swaps SHA-256 for xxHash's XXH3-128 variant on the ONE
    hasher this module uses for archival-vs-canonical duplicate detection
    (:func:`classify_canonical_archival`'s ``cand_hash == twin_hash`` byte-
    equality check).  That comparison is never a security/audit boundary --
    collision resistance against an adversary is irrelevant here, only
    "did this content change" -- so a non-cryptographic hash is the correct
    tool, and xxHash is dramatically faster than SHA-256 on real files (see
    ``TestXxh3Benchmark`` in the test suite for a live, on-box A/B
    confirmation rather than just citing published numbers). Every OTHER
    SHA-256 use in the wider Meridian codebase -- anywhere hashing serves a
    genuine security/audit purpose -- is untouched; this swap is scoped to
    ONLY this module's archival-dedup hasher.

    MIGRATION (49b97a6a, resolved): an existing DB's rows are guarded by
    ``_HASH_ALGO_VERSION`` / :func:`_check_hash_algo_version` so an upgrade
    from a pre-xxHash DB forces a one-time full re-hash rather than ever
    mixing SHA-256 and xxHash values in the same column.

    Lazily imports ``xxhash`` so the extension's hard dependency surface
    doesn't grow for callers that never touch archival classification, and
    degrades gracefully to :func:`_sha256_file` when ``xxhash`` isn't
    installed for any reason -- a missing optional dependency must never
    turn a hash-algorithm swap into a broken indexer. The returned digest
    format therefore differs depending on which path was taken (128-bit
    xxh3 hex vs. 256-bit sha256 hex); callers must treat the hash purely as
    an opaque equality token (which is exactly how
    :func:`classify_canonical_archival` already uses it) and never assume a
    fixed length or algorithm.
    """
    try:
        import xxhash  # noqa: PLC0415 -- optional, lazy
    except ImportError:
        return _sha256_file(path)
    try:
        h = xxhash.xxh3_128()
        with open(path, "rb", buffering=0) as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _infer_generating_script_from_text(text: str) -> str | None:
    m = _SCRIPT_HINT_RE.search(text[:_SCRIPT_HINT_SEARCH_CHARS])
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
    paths: list[str], *, hasher: Callable[[str], str | None] = _xxh3_file,
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
    outputs_dir: str, *, hasher: Callable[[str], str | None] = _xxh3_file,
    exclude_patterns: tuple[str, ...] = (),
) -> list[OutputRow]:
    """Walk ``outputs_dir`` with secret-file exclusion (and the optional
    user ``exclude_patterns`` list, fd4dd661) and build OutputRows."""
    paths = _iter_safe_output_files(outputs_dir, exclude_patterns=exclude_patterns)
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


def _analyse_file(
    path: str, hasher: Callable[[str], str | None], *, needs_hash: bool = True,
) -> "_FileAnalysis":
    """Read-only per-file analysis: stat + fingerprint + hash.

    Designed to run in a :class:`concurrent.futures.ThreadPoolExecutor` worker.
    Pure I/O + CPU with no shared mutable state -- safe to call concurrently
    for different paths.  The GIL is released during the file-read portions,
    so hashing a large file (e.g. a 5.9 MB sweep_results.json) overlaps with
    hashing and parsing other files.

    e1fd4182 -- fingerprinting and hashing used to each open+read the file
    separately (file_fingerprint(path) + hasher(path)). Live-benchmarked
    2.15x/53.5% time saved on a real 70k-file tree by reading the bytes
    ONCE and deriving both the hash and the capped-text fingerprint from
    that single buffer -- verified byte-for-byte identical output (hash,
    kind, csv_columns, json_keys, generating_script) across all 70,000
    files before applying. Only takes this fast path for the default
    hasher (`hasher is _sha256_file`); a custom/injected hasher (tests,
    or the future xxHash swap in 984b237c once that lands) falls back to
    the original two-read path unchanged, so the injectable-hasher
    contract other callers rely on is fully preserved.

    e1fd4182 (size-prefilter follow-up) -- ``needs_hash=False`` skips the
    hash entirely: a file whose size is unique across the whole tree
    provably cannot be a duplicate of anything classify_canonical_archival
    would ever compare it against, so computing its hash is pure waste.
    Live-benchmarked 2.82x/64.5% time saved on a real 420-file tree (399 of
    which had unique sizes) -- verified 0 mismatches on the hashes of every
    file that DID still need one. Callers decide ``needs_hash`` from a
    size-count map built once per rebuild() call, not from anything in
    this function -- it stays a pure per-path decision here.
    """
    if not needs_hash:
        fp = file_fingerprint(path)
        try:
            st = os.stat(path)
            size: int | None = st.st_size
            mtime: float | None = st.st_mtime
        except OSError:
            size = mtime = None
        return _FileAnalysis(path=path, fingerprint=fp, mtime=mtime,
                              size=size, sha256=None)

    try:
        st = os.stat(path)
        size: int | None = st.st_size
        mtime: float | None = st.st_mtime
    except OSError:
        size = mtime = None

    if hasher is _sha256_file or hasher is _xxh3_file:
        try:
            with open(path, "rb", buffering=0) as fh:
                data = fh.read()
        except OSError:
            data = None
        if data is not None:
            # 984b237c -- fast path supports either hasher on the SAME single
            # read; xxhash import failure inside _xxh3_file already degrades
            # to SHA256 gracefully for the (rare) non-fast-path callers, but
            # this fast path computes the hash directly rather than calling
            # back through hasher(path) (which would re-read) -- so it needs
            # its own lazy-import fallback, mirroring _xxh3_file's own.
            if hasher is _xxh3_file:
                try:
                    import xxhash  # noqa: PLC0415
                    h = xxhash.xxh3_128()
                except ImportError:
                    h = hashlib.sha256()
            else:
                h = hashlib.sha256()
            h.update(data)
            sha = h.hexdigest()
            kind = _classify_suffix(path)
            if kind != "text_content":
                fp = FileFingerprint(path=path, kind=kind)
            else:
                # Match _read_text_capped's exact semantics (up to
                # _MAX_CONTENT_CHARS characters). Over-read bytes (4/char
                # worst case for UTF-8) then slice the DECODED text --
                # avoids decoding a huge file unnecessarily, never cuts a
                # multi-byte char differently than the original read did.
                text = data[: _MAX_CONTENT_CHARS * 4].decode(
                    "utf-8", errors="replace"
                )[:_MAX_CONTENT_CHARS]
                suffix = os.path.splitext(path)[1].lower()
                if suffix == ".csv":
                    columns, script = _extract_csv(text)
                    fp = FileFingerprint(path=path, kind=kind,
                                         csv_columns=columns, generating_script=script)
                else:
                    keys, script = _extract_json(text)
                    fp = FileFingerprint(path=path, kind=kind,
                                         json_keys=keys, generating_script=script)
            return _FileAnalysis(path=path, fingerprint=fp, mtime=mtime,
                                  size=size, sha256=sha)

    # Fallback: OSError on the single read, or a non-default hasher injected
    # (tests, or a future custom hasher) -- original two-read path, unchanged.
    fp = file_fingerprint(path)
    sha = hasher(path)
    return _FileAnalysis(path=path, fingerprint=fp, mtime=mtime, size=size, sha256=sha)


# ---------------------------------------------------------------------------
# Persistent DuckDB FTS index with write locking
# ---------------------------------------------------------------------------

_MAX_CACHED_INDEXES = 32
_index_cache_lock = threading.Lock()
_index_cache: OrderedDict[str, "OutputsFtsIndex"] = OrderedDict()

# 49b97a6a -- bump whenever the algorithm behind outputs_index.sha256
# changes, so OutputsFtsIndex._check_hash_algo_version can detect an
# on-disk DB written under a prior algorithm and force a one-time full
# re-hash instead of ever silently mixing algorithms under one column.
# 1 = legacy SHA-256 (pre-984b237c). 2 = xxHash XXH3-128 (984b237c).
_HASH_ALGO_VERSION = 2

# ---------------------------------------------------------------------------
# acac2599 -- configurable Phase-1 ThreadPoolExecutor worker cap
# ---------------------------------------------------------------------------
_DEFAULT_MAX_WORKERS = 8
_MAX_WORKERS_ENV_VAR = "MERIDIAN_OUTPUTS_MAX_WORKERS"


def _default_max_workers() -> int:
    """Resolve the default Phase-1 worker cap from the environment.

    Checked fresh on every call (a cheap env lookup) rather than cached at
    import time, so a changed ``MERIDIAN_OUTPUTS_MAX_WORKERS`` takes effect
    on the next :class:`OutputsFtsIndex` construction without a process
    restart. Falls back to the historical hardcoded default (8) for
    anything not a positive integer, logging why so a typo'd env var is
    diagnosable instead of silently ignored.
    """
    raw = os.environ.get(_MAX_WORKERS_ENV_VAR)
    if raw is None or not raw.strip():
        return _DEFAULT_MAX_WORKERS
    try:
        value = int(raw.strip())
    except ValueError:
        _log.warning(
            "%s=%r is not a valid integer -- falling back to default (%d)",
            _MAX_WORKERS_ENV_VAR, raw, _DEFAULT_MAX_WORKERS,
        )
        return _DEFAULT_MAX_WORKERS
    if value < 1:
        _log.warning(
            "%s=%r must be >= 1 -- falling back to default (%d)",
            _MAX_WORKERS_ENV_VAR, raw, _DEFAULT_MAX_WORKERS,
        )
        return _DEFAULT_MAX_WORKERS
    return value


def _resolve_max_workers(explicit: int | None) -> int:
    """Precedence: explicit constructor arg > env var > hardcoded default.

    Mirrors the precedence rule already used elsewhere in this codebase for
    other explicit-arg/env-var/default triples (e.g. project_id resolution).
    """
    if explicit is not None:
        if explicit >= 1:
            return explicit
        _log.warning(
            "OutputsFtsIndex: max_workers=%r must be >= 1 -- falling back "
            "to default (%d)", explicit, _DEFAULT_MAX_WORKERS,
        )
        return _DEFAULT_MAX_WORKERS
    return _default_max_workers()


# ---------------------------------------------------------------------------
# c73c0dd7 -- configurable Tantivy writer heap_size
# ---------------------------------------------------------------------------
# Measured live against a real 16k-file batch: Tantivy's own default
# (undocumented in the Python binding, but effectively ~128MB against real
# content of ~240MB) produced 678 fragmented segments + a 4.8s reload().
# Raising heap_size to 512MB drops segments to 48 and cuts
# add+commit+reload from 8.8s to 2.8s (~3x) -- the writer flushes far less
# often against the same content volume.
_DEFAULT_TANTIVY_HEAP_BYTES = 512 * 1024 * 1024
_TANTIVY_HEAP_ENV_VAR = "MERIDIAN_OUTPUTS_TANTIVY_HEAP_MB"


def _default_tantivy_heap_bytes() -> int:
    """Resolve the default Tantivy writer heap_size (in bytes) from the
    environment, expressed in MB for readability (mirrors the max_workers
    resolution pattern above). Checked fresh on every call rather than
    cached at import time."""
    raw = os.environ.get(_TANTIVY_HEAP_ENV_VAR)
    if raw is None or not raw.strip():
        return _DEFAULT_TANTIVY_HEAP_BYTES
    try:
        value_mb = int(raw.strip())
    except ValueError:
        _log.warning(
            "%s=%r is not a valid integer (MB) -- falling back to default (%d MB)",
            _TANTIVY_HEAP_ENV_VAR, raw, _DEFAULT_TANTIVY_HEAP_BYTES // (1024 * 1024),
        )
        return _DEFAULT_TANTIVY_HEAP_BYTES
    if value_mb < 15:  # tantivy's own hard minimum is ~15MB per indexing thread
        _log.warning(
            "%s=%r must be >= 15 (MB) -- falling back to default (%d MB)",
            _TANTIVY_HEAP_ENV_VAR, raw, _DEFAULT_TANTIVY_HEAP_BYTES // (1024 * 1024),
        )
        return _DEFAULT_TANTIVY_HEAP_BYTES
    return value_mb * 1024 * 1024


def _resolve_tantivy_heap_bytes(explicit: int | None) -> int:
    """Precedence: explicit constructor arg (bytes) > env var (MB) > default."""
    if explicit is not None:
        if explicit >= 15 * 1024 * 1024:
            return explicit
        _log.warning(
            "OutputsFtsIndex: tantivy_heap_bytes=%r must be >= 15MB -- "
            "falling back to default (%d MB)", explicit,
            _DEFAULT_TANTIVY_HEAP_BYTES // (1024 * 1024),
        )
        return _DEFAULT_TANTIVY_HEAP_BYTES
    return _default_tantivy_heap_bytes()


# ---------------------------------------------------------------------------
# 9a18a2b2 -- Tantivy single-writer lock conflict handling
# ---------------------------------------------------------------------------

class TantivyLockConflict(Exception):
    """Raised when Tantivy's own single-writer lock is already held by
    another writer (same process or a different one) for this index's
    on-disk directory.

    Tantivy already ships a real lock file with a clean, identifiable
    failure (``LockBusy``) -- unlike code-intel's raw-SQLite Windows rename
    bug, there is no ambiguous partial-write corruption risk here. This
    class exists purely so :class:`OutputsFtsIndex` can recognise that ONE
    specific, expected failure mode and surface a clear, actionable message
    (see :meth:`OutputsFtsIndex._connect_tantivy`) instead of it vanishing
    into the broad ``except Exception`` handlers that already wrap every
    Tantivy call site (best-effort by design -- see
    :meth:`OutputsFtsIndex.rebuild`/:meth:`OutputsFtsIndex.search`
    docstrings). This is deliberately NOT a cross-process coordination
    mechanism -- no retry loop, no new locking primitive, just a named,
    loggable, testable failure instead of an opaque one.
    """


_TANTIVY_LOCK_MARKERS = ("lockbusy", "failed to acquire", "index lock")


def _is_tantivy_lock_conflict(exc: BaseException) -> bool:
    """Best-effort sniff for Tantivy's single-writer lock-conflict error.

    The official ``tantivy`` PyPI bindings raise a plain ``ValueError`` (not
    a dedicated exception type) whose message contains "LockBusy" / "Failed
    to acquire index lock" (tantivy-rs's own ``LockError::LockBusy``
    variant, confirmed live against this exact bindings version). String-
    sniffing is the same pragmatic approach this module already takes for
    other optional/lazy-imported libraries that don't expose typed
    exceptions of their own.
    """
    text = str(exc).lower()
    return any(marker in text for marker in _TANTIVY_LOCK_MARKERS)


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
        hasher: Callable[[str], str | None] = _xxh3_file,
        max_workers: int | None = None,
        exclude_patterns: tuple[str, ...] | None = None,
        tantivy_heap_bytes: int | None = None,
    ) -> None:
        self.outputs_dir = outputs_dir
        self._db_path = db_path
        self._hasher = hasher
        # acac2599 -- Phase-1 ThreadPoolExecutor worker cap: explicit param >
        # MERIDIAN_OUTPUTS_MAX_WORKERS env var > hardcoded default (8, the
        # previous unconfigurable behaviour).
        self._max_workers = _resolve_max_workers(max_workers)
        # c73c0dd7 -- Tantivy writer heap_size: explicit param (bytes) >
        # MERIDIAN_OUTPUTS_TANTIVY_HEAP_MB env var > 512MB default (up from
        # tantivy's own undersized default, measured ~3x faster commits).
        self._tantivy_heap_bytes = _resolve_tantivy_heap_bytes(tantivy_heap_bytes)
        # fd4dd661 -- user-configurable gitignore-style exclude patterns:
        # explicit param > MERIDIAN_OUTPUTS_EXCLUDE_PATTERNS env var > empty
        # (unchanged default behaviour -- only secret-file exclusion applies).
        self._exclude_patterns: tuple[str, ...] = (
            tuple(exclude_patterns) if exclude_patterns is not None
            else _default_exclude_patterns()
        )
        self._write_lock = IndexFileLock(db_path)
        self._read_lock = threading.RLock()  # in-process query serialisation
        self._con = connection
        self._owns_con = connection is None
        self._fts_built = False
        # b1789c0d — set when _rebuild_fts() is deferred because the overall
        # deadline expired before Phase 2 could reach it (includes the cold/
        # first-call case where _fts_built is still False). search() uses this
        # flag to schedule a lazy FTS build on the NEXT call once some rows are
        # actually in the table -- so the caller gets REAL results (from a
        # partial but non-empty index) instead of empty hits with total_indexed=0.
        self._fts_pending = False
        self._manifest: dict[str, tuple[float | None, int | None]] = {}
        self._row_cache: dict[str, OutputRow] = {}
        self.last_rebuild_partial = False
        # 1a799e52 -- set when Phase 2's DB write raises inside rebuild()'s
        # `except Exception: _log.debug(...)` block. Previously that failure
        # was swallowed at DEBUG level only, while total_indexed/total_in_index
        # (both derived from the in-memory _row_cache, populated BEFORE the
        # write is attempted) kept reporting growing "success" -- a real
        # persistence failure looked identical to a healthy index until a
        # fresh process found nothing on disk. None means the most recent
        # rebuild() call's write (if attempted) succeeded or nothing changed.
        self.last_db_write_error: str | None = None
        # 6ba77ada -- resumable file-walk state (see _ResumableFileWalk). None
        # when no walk pass is currently in progress; set at the start of a
        # pass and cleared once that pass's walk finishes. _walk_accumulated
        # holds every path discovered so far in the CURRENT in-progress pass.
        self._walk_state: "_ResumableFileWalk | None" = None
        self._walk_accumulated: list[str] = []
        # 6ba77ada -- backlog of paths confirmed stale (by the staleness
        # check below) but not yet successfully analysed + written. Persists
        # across calls so a straggler is retried, not lost or re-detected
        # from scratch, AND doubles as the signal the walk throttle (above)
        # uses to pause further discovery until Phase 1/2 catch up.
        self._pending_stale: dict[str, tuple[float | None, int | None]] = {}
        # 77443d83 -- Tantivy replaces DuckDB's own FTS extension for the
        # search index; DuckDB (self._con) remains the metadata store (path,
        # content, mtime, sha256, ... -- see _COLUMNS) that resolve_output and
        # add_annotation still query directly.
        self._tantivy_index: Any = None
        self._tantivy_writer: Any = None
        # Rows staged for the next Tantivy commit. Populated from THIS call's
        # own paths_to_delete/new_rows in rebuild() whenever the FTS build is
        # deferred past a deadline, so search()'s lazy catch-up (or the next
        # rebuild()) commits exactly the outstanding delta -- never a full
        # re-index -- no matter how many deferrals accumulate first.
        self._pending_tantivy_deletes: set[str] = set()
        self._pending_tantivy_upserts: dict[str, OutputRow] = {}
        # 8163816e -- gates the one-time-per-process migration check that
        # backfills Tantivy from any pre-existing DuckDB rows (upgrade path).
        self._tantivy_migration_checked = False
        # 9a18a2b2 -- last Tantivy lock-conflict message (if any), surfaced
        # by search_outputs() as an actionable warning. None whenever the
        # last attempt to open/reuse the Tantivy writer succeeded (or none
        # was ever attempted yet).
        self._last_tantivy_error: str | None = None
        # 49b97a6a -- set True when _check_hash_algo_version() (called from
        # _connect()) detects this db_path's outputs_index table predates
        # the xxHash swap (984b237c) and needs a one-time full re-hash.
        # Cleared once a rebuild() call fully converges (walk complete, no
        # deadline breach, empty backlog) -- see rebuild()'s tail.
        self._pending_hash_upgrade = False

    def _connect(self) -> Any:
        if self._con is None:
            import duckdb  # noqa: PLC0415
            self._con = duckdb.connect(self._db_path)
            # 77443d83 -- a fresh instance always assumed _fts_built started
            # False. With Tantivy this is now cheap either way (_rebuild_fts
            # only ever commits its pending delta, never a full re-index), but
            # detecting an existing on-disk Tantivy index still lets a fresh
            # process recognise a prior process's committed work immediately
            # rather than reporting _fts_built=False until the next write.
            tdir = self._tantivy_dir()
            if tdir is not None:
                try:
                    import tantivy  # noqa: PLC0415
                    if tantivy.Index.exists(tdir):
                        self._fts_built = True
                except Exception:  # noqa: BLE001
                    _log.debug(
                        "OutputsFtsIndex._connect: tantivy index probe failed",
                        exc_info=True,
                    )
            # 49b97a6a -- follow-up to 984b237c's xxHash swap: a DB written
            # before the swap can hold SHA-256 values under the same
            # 'sha256' column xxHash now writes to. Rehydrating those rows
            # as "already indexed" would let them silently mix algorithms
            # with newly-hashed rows forever (archival-dedup would then
            # false-negative across that boundary -- the exact bug this
            # item fixes). Detect + flag BEFORE rehydration, since the flag
            # decides whether rehydration should even run this connect.
            needs_full_rehash = False
            try:
                needs_full_rehash = self._check_hash_algo_version(self._con)
            except Exception:  # noqa: BLE001
                _log.debug(
                    "OutputsFtsIndex._connect: hash-algo version check failed",
                    exc_info=True,
                )
            # QUICK PATCH (2026-07-16, live diagnosis) -- 0c1a4349 made the
            # outputs_index TABLE persist to disk, but _manifest/_row_cache
            # (the in-memory staleness-detection state) were never rehydrated
            # from it on a fresh process. Every process restart therefore saw
            # an empty _row_cache, making EVERY file look stale again
            # regardless of how much real work a prior process already
            # persisted to disk -- confirmed live: a 205k+-file tree's cache
            # file sat at a fixed size for hours across multiple calls,
            # because each call re-did the same early-sorted subset of files
            # from scratch instead of continuing past where the last one left
            # off. Rehydrate from the existing table (if any) so restarts
            # resume instead of restarting.
            #
            # 49b97a6a -- SKIPPED when needs_full_rehash is True: leaving
            # _manifest/_row_cache empty makes every existing row look
            # exactly as "cold" as a brand-new tree, so the already-proven
            # incremental convergence machinery below (staleness detection,
            # ThreadPoolExecutor analysis, Phase 2 write) re-analyses AND
            # re-hashes every row for real, with zero new code paths.
            if not needs_full_rehash:
                try:
                    self._rehydrate_cache_from_disk()
                except Exception:  # noqa: BLE001
                    _log.debug(
                        "OutputsFtsIndex._connect: cache rehydration failed",
                        exc_info=True,
                    )
        return self._con

    def _rehydrate_cache_from_disk(self) -> None:
        """Populate ``_manifest``/``_row_cache`` from any pre-existing rows in
        the on-disk ``outputs_index`` table, so a fresh process resumes prior
        progress instead of treating every file as stale again. No-op (and
        cheap) when the table doesn't exist yet or is empty."""
        con = self._con
        if con is None:
            return
        try:
            exists = con.execute(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_name = 'outputs_index'"
            ).fetchone()
        except Exception:  # noqa: BLE001
            return
        if exists is None:
            return
        try:
            relation = con.execute(
                "SELECT path, content, mtime, sha256, size, "
                "generating_script, kind, is_archival, canonical_path, "
                "csv_columns, json_keys FROM outputs_index"
            )
            columns = [c[0] for c in relation.description]
            fetched = relation.fetchall()
        except Exception:  # noqa: BLE001
            _log.debug(
                "OutputsFtsIndex._rehydrate_cache_from_disk: read failed",
                exc_info=True,
            )
            return
        for raw in fetched:
            rec = dict(zip(columns, raw))
            path = rec.get("path")
            if not path:
                continue
            row = OutputRow(
                path=path,
                content=rec.get("content"),
                mtime=rec.get("mtime"),
                sha256=rec.get("sha256"),
                size=rec.get("size"),
                generating_script=rec.get("generating_script"),
                kind=rec.get("kind"),
                is_archival=bool(rec.get("is_archival")),
                canonical_path=rec.get("canonical_path"),
                csv_columns=(
                    json.loads(rec["csv_columns"]) if rec.get("csv_columns") else None
                ),
                json_keys=(
                    json.loads(rec["json_keys"]) if rec.get("json_keys") else None
                ),
            )
            self._row_cache[path] = row
            self._manifest[path] = (rec.get("mtime"), rec.get("size"))
        if fetched:
            _log.debug(
                "OutputsFtsIndex._rehydrate_cache_from_disk: resumed %d "
                "cached rows from disk", len(fetched),
            )

    def _read_hash_algo_version(self, con: Any) -> int:
        """Return the ``hash_algo_version`` stored in ``outputs_index_meta``,
        or 0 if unset/unreadable (covers both "brand-new DB" and "genuinely
        pre-49b97a6a DB that never had this row at all")."""
        try:
            row = con.execute(
                "SELECT value FROM outputs_index_meta "
                "WHERE key = 'hash_algo_version'"
            ).fetchone()
        except Exception:  # noqa: BLE001
            return 0
        if row is None or row[0] is None:
            return 0
        try:
            return int(row[0])
        except (TypeError, ValueError):
            return 0

    def _write_hash_algo_version(self, con: Any, version: int) -> None:
        """Upsert ``hash_algo_version`` via delete-then-insert -- same
        pattern :meth:`_add_annotation_locked` already uses, rather than
        relying on DuckDB's ``ON CONFLICT`` support."""
        con.execute(
            "DELETE FROM outputs_index_meta WHERE key = 'hash_algo_version'"
        )
        con.execute(
            "INSERT INTO outputs_index_meta (key, value) VALUES "
            "('hash_algo_version', ?)",
            [str(version)],
        )

    def _check_hash_algo_version(self, con: Any) -> bool:
        """49b97a6a -- detect a pre-xxHash (984b237c) on-disk DB and flag it
        for a one-time full re-hash instead of silently mixing SHA-256 (old
        rows, untouched since before the upgrade) with xxHash (newly-stale
        rows) under the same ``sha256`` column -- archival-dedup would
        otherwise false-negative across that boundary forever.

        Returns True when THIS instance needs a full re-hash -- the caller
        (:meth:`_connect`) must then skip :meth:`_rehydrate_cache_from_disk`
        so every existing row looks exactly as "cold" as a brand-new tree,
        letting the ordinary incremental convergence machinery in
        :meth:`rebuild` do the actual re-hashing with no separate code path.

        A brand-new (empty) ``outputs_index`` table is NOT a legacy DB --
        there is nothing to re-hash, so the current version is written
        immediately and this returns False, matching pre-49b97a6a behaviour
        exactly for the common (fresh-DB) case.
        """
        self._ensure_schema(con)
        stored = self._read_hash_algo_version(con)
        if stored >= _HASH_ALGO_VERSION:
            return False
        if stored == 0:
            try:
                has_rows = con.execute(
                    "SELECT 1 FROM outputs_index LIMIT 1"
                ).fetchone() is not None
            except Exception:  # noqa: BLE001
                has_rows = False
            if not has_rows:
                self._write_hash_algo_version(con, _HASH_ALGO_VERSION)
                return False
        _log.info(
            "OutputsFtsIndex: hash_algo_version %d < current %d for %r -- "
            "scheduling a one-time full re-hash (upgrading archival-dedup "
            "hashing from SHA-256 to xxHash, 984b237c/49b97a6a)",
            stored, _HASH_ALGO_VERSION, self._db_path,
        )
        self._pending_hash_upgrade = True
        return True

    @staticmethod
    def _tantivy_schema() -> Any:
        import tantivy  # noqa: PLC0415
        schema_builder = tantivy.SchemaBuilder()
        # 'raw' (untokenized) so delete_documents/exact-match lookups by full
        # path work reliably -- mirrors the DuckDB table's path PRIMARY KEY.
        schema_builder.add_text_field("path", stored=True, tokenizer_name="raw")
        schema_builder.add_text_field("content", stored=False)
        return schema_builder.build()

    def _tantivy_dir(self) -> str | None:
        """Directory Tantivy persists its index segments to, a sibling of the
        DuckDB cache file. None (RAM index, no persistence) when db_path is
        ':memory:' -- mirrors _resolve_index_db_path's own degrade-gracefully
        behaviour for a directory that can't be created.

        5d0b3866 -- MUST be unique per ``db_path``, not merely per PARENT
        directory. The previous implementation used a single fixed
        ``<parent>/tantivy_index`` name, so any two distinct ``db_path``
        values sharing a parent (e.g. multiple .duckdb files dropped in the
        same tmp/ folder during testing, or any future multi-db layout)
        silently collided on ONE tantivy directory. Confirmed live: a
        second index's :meth:`_connect` would find the FIRST index's
        on-disk Tantivy segments already there via ``tantivy.Index.exists``
        and set ``_fts_built = True`` from a completely unrelated index's
        state, after which :meth:`search` queried Tantivy content that had
        nothing to do with this instance's own DuckDB rows -- returning 0
        hits for terms genuinely present in every one of the colliding
        instances' own files.

        Fixed by deriving the directory name from the full, absolute
        ``db_path`` itself: a short content hash guarantees two distinct
        ``db_path`` values NEVER collide (even after any sanitisation), and
        a sanitised filename stem is appended purely for human
        readability/debugging -- it plays no role in uniqueness.
        """
        if self._db_path == ":memory:":
            return None
        canonical = os.path.abspath(self._db_path)
        base = os.path.dirname(canonical)
        if not base:
            return None
        stem = os.path.splitext(os.path.basename(canonical))[0]
        safe_stem = re.sub(r"[^A-Za-z0-9_.-]", "_", stem) or "index"
        digest = hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:10]
        tdir = os.path.join(base, f"tantivy_index__{safe_stem}__{digest}")
        try:
            os.makedirs(tdir, exist_ok=True)
        except OSError:
            return None
        return tdir

    def _connect_tantivy(self) -> tuple[Any, Any]:
        """Lazily open (or reuse) the Tantivy index + a long-lived writer --
        same lifecycle pattern as :meth:`_connect` for the DuckDB connection.
        Tantivy allows only one live writer per index, so this instance keeps
        a single writer alive across calls rather than opening/closing one
        per commit.

        9a18a2b2 -- Tantivy enforces this with a real single-writer lock
        file: a SECOND live writer against the same directory (this process
        racing itself, or a different process/session indexing the same
        outputs_dir concurrently) makes ``index.writer()`` raise a clean,
        identifiable error (:func:`_is_tantivy_lock_conflict`) rather than
        corrupting anything. This is intentionally NOT turned into a
        cross-process coordination mechanism (no retry/backoff loop, no new
        locking primitive) -- that's explicitly out of scope here. Instead
        the conflict is recognised, logged at WARNING (not buried at DEBUG
        inside the broad ``except Exception`` blocks that already wrap
        every caller of this method -- rebuild()/search() stay best-effort
        and never crash from this), and re-raised as
        :class:`TantivyLockConflict` with an actionable message that
        :attr:`_last_tantivy_error` records for callers (search_outputs()
        surfaces it as ``result["tantivy_lock_warning"]``).
        """
        if self._tantivy_index is None:
            import tantivy  # noqa: PLC0415
            schema = self._tantivy_schema()
            tdir = self._tantivy_dir()
            index = (
                tantivy.Index(schema, path=tdir) if tdir is not None
                else tantivy.Index(schema)
            )
            try:
                # c73c0dd7 -- explicit heap_size (default 512MB, up from
                # tantivy's own undersized default): measured live on a real
                # 16k-file batch, this alone drops segment fragmentation from
                # 678 to 48 and cuts add+commit+reload from 8.8s to 2.8s (~3x).
                writer = index.writer(heap_size=self._tantivy_heap_bytes)
            except Exception as exc:  # noqa: BLE001
                if not _is_tantivy_lock_conflict(exc):
                    raise
                message = (
                    f"Tantivy index at {tdir!r} is locked by another writer "
                    "(this process or a different one is already indexing "
                    "this outputs_dir). Rows already analysed this call "
                    "remain safe in the DuckDB metadata table; the search "
                    "index will catch up automatically on a later call "
                    "once the other writer releases the lock."
                )
                self._last_tantivy_error = message
                _log.warning("OutputsFtsIndex._connect_tantivy: %s", message)
                raise TantivyLockConflict(message) from exc
            self._last_tantivy_error = None
            self._tantivy_index = index
            self._tantivy_writer = writer
        return self._tantivy_index, self._tantivy_writer

    def _migrate_duckdb_rows_to_tantivy_if_needed(
        self, con: Any, index: Any, writer: Any,
    ) -> None:
        """8163816e -- migration path for pre-Tantivy (pure-DuckDB-FTS)
        installs: their outputs_index table can already hold rows that
        predate this migration. Those rows aren't "stale" by filesystem
        mtime/size, so the ordinary incremental path in rebuild() never
        revisits them -- without this, they'd simply be invisible to
        search() forever after an upgrade.

        Converter approach (cheaper than delete-and-rebuild-clean): bulk-copy
        existing DuckDB rows straight into Tantivy once. Content is already
        sitting in the metadata table, so no filesystem re-walk/re-hash/
        re-classify is needed. delete_documents before each add makes this
        naturally idempotent -- safe to re-run after a partial/crashed
        attempt, since it's gated on Tantivy's doc count still being short of
        DuckDB's row count rather than a one-shot flag.
        """
        try:
            tantivy_count = index.searcher().num_docs
            duckdb_count = con.execute(
                "SELECT COUNT(*) FROM outputs_index"
            ).fetchone()[0]
        except Exception:  # noqa: BLE001
            _log.debug(
                "OutputsFtsIndex: tantivy migration count probe failed",
                exc_info=True,
            )
            return
        if duckdb_count == 0 or tantivy_count >= duckdb_count:
            return
        _log.info(
            "OutputsFtsIndex: migrating pre-existing DuckDB rows into "
            "Tantivy (%d row(s) in DuckDB, %d already in Tantivy)",
            duckdb_count, tantivy_count,
        )
        import tantivy  # noqa: PLC0415
        relation = con.execute("SELECT path, content FROM outputs_index")
        for path, content in relation.fetchall():
            writer.delete_documents("path", path)
            writer.add_document(tantivy.Document(path=path, content=content or ""))
        writer.commit()
        index.reload()

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
        # 49b97a6a -- small key/value metadata table; currently only holds
        # 'hash_algo_version' (see _check_hash_algo_version), kept generic
        # so future schema-version markers don't need a new table each time.
        con.execute(
            "CREATE TABLE IF NOT EXISTS outputs_index_meta ("
            "key VARCHAR PRIMARY KEY, value VARCHAR)"
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
        # 1a799e52 -- reset per-call; set below if Phase 2's DB write fails.
        self.last_db_write_error = None
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
        # Phase 0: deadline-aware, resumable file walk (6ba77ada)
        # ------------------------------------------------------------------
        # The plain, blocking _iter_safe_output_files() walk has no deadline
        # awareness of its own and can by itself take far longer than the
        # entire rebuild() budget on a huge tree (confirmed live: ~11s to
        # walk a 70k-file tree vs. the 5s default budget -- Phase 1/Phase 2
        # never got a chance to run at all, every call returning 0 rows
        # forever). The walk shares Phase 1's own sub-deadline
        # (`phase1_deadline`, computed above from the same start point as the
        # overall `deadline`) -- this matches 5845cc6d's original intent,
        # whose comment already assumed "time already spent on the walk"
        # would eat into Phase 1's share; it just assumed the walk itself was
        # bounded, which it wasn't. _ResumableFileWalk wraps a Python
        # generator, so pausing/resuming a walk across multiple rebuild()
        # calls costs nothing extra -- a full pass over a huge tree is
        # amortised across as many calls as it needs instead of blocking the
        # first one.
        # The walk is additionally THROTTLED to only discover more files
        # while the already-known backlog of confirmed-stale files still
        # waiting on Phase 1/2 (`self._pending_stale`, populated below) is
        # smaller than one walk batch. Without this, on a tree where the
        # walk can discover files far faster than Phase 1/2 can
        # analyse+persist them (which is exactly the situation this fix
        # creates), the backlog would grow without bound and every call
        # would waste most of its budget re-submitting/re-considering an
        # ever-larger backlog instead of ever shrinking it. A backlog-size
        # threshold (rather than "only when fully empty") matters for small
        # trees too: a pathologically tight first call can leave a tiny
        # (e.g. single-digit) backlog that Phase 1/2 will trivially clear
        # alongside a full batch of freshly-discovered files on the very
        # next call -- gating strictly on "empty" would instead stall
        # discovery of the REST of a small tree for an extra call for no
        # benefit, since there was never a risk of overwhelming Phase 1/2.
        newly_seen: list[str] = []
        if os.path.isdir(self.outputs_dir):
            if self._walk_state is None:
                self._walk_state = _ResumableFileWalk(
                    self.outputs_dir, exclude_patterns=self._exclude_patterns,
                )
                self._walk_accumulated = []
            if len(self._pending_stale) < _ResumableFileWalk._MAX_BATCH:
                newly_seen = self._walk_state.drain(phase1_deadline)
                self._walk_accumulated.extend(newly_seen)
            walk_complete = self._walk_state.exhausted
        else:
            self._walk_state = None
            self._walk_accumulated = []
            self._pending_stale = {}
            walk_complete = True

        if walk_complete:
            # A full pass just finished (or outputs_dir doesn't exist) -- this
            # is now the authoritative on-disk picture, so removed-file
            # detection is safe. Reset resumable state so the NEXT rebuild()
            # call starts a fresh pass and keeps catching future on-disk
            # changes.
            all_paths: list[str] = sorted(self._walk_accumulated)
            self._walk_state = None
            self._walk_accumulated = []
            removed_paths: set[str] = set(self._manifest) - set(all_paths)
            # A path that vanished from disk can never become un-stale --
            # drop it from the backlog so it isn't retried forever.
            for p in removed_paths:
                self._pending_stale.pop(p, None)
        else:
            # Walk pass still in progress -- we only know about the files
            # revisited so far THIS pass, not the full tree. Optimistically
            # keep every previously-indexed path in the picture (assume still
            # present until the walk actually gets around to confirming
            # otherwise) so the reported row count and search index never
            # regress mid-pass. Removed-file detection is deferred until the
            # pass completes.
            all_paths = sorted(set(self._row_cache) | set(self._walk_accumulated))
            removed_paths = set()

        path_set = set(all_paths)

        # ------------------------------------------------------------------
        # Phase 1: read-only pre-analysis (no lock, may run in parallel)
        # ------------------------------------------------------------------
        # Staleness (mtime/size changed) is os.stat-checked exactly once per
        # file: only for paths the walk revisited THIS call (`newly_seen`).
        # A confirmed-stale path is added to `self._pending_stale` (path ->
        # captured stat signature), which persists across calls until the
        # path is actually analysed and written -- so a straggler that
        # Phase 1/2 didn't get to before this call's own deadline is neither
        # lost nor re-stat-ed again next call; it just stays queued. This is
        # what makes the walk-throttling above safe: `self._pending_stale`
        # IS the backlog the throttle is sized against. _manifest is read
        # without the lock here; it is only mutated inside the write lock
        # (Phase 2), so reading it here is safe as long as no concurrent
        # rebuild() is running -- which IndexFileLock prevents.
        for p in newly_seen:
            try:
                st = os.stat(p)
                sig: tuple[float | None, int | None] = (st.st_mtime, st.st_size)
            except OSError:
                sig = (None, None)
            if self._manifest.get(p) != sig or p not in self._row_cache:
                self._pending_stale[p] = sig

        stale: list[str] = list(self._pending_stale)
        stale_sigs: dict[str, tuple[float | None, int | None]] = dict(self._pending_stale)

        # Parallel per-file analysis for stale paths (fingerprint + hash + stat).
        # Workers run before the write lock is taken so heavy I/O (e.g. hashing
        # a 5.9 MB file) overlaps across files.  Results are collected into a
        # dict and processed in sorted order to guarantee determinism.
        precomputed: dict[str, _FileAnalysis] = {}
        if stale:
            # e1fd4182 (size-prefilter follow-up) -- build a size -> count
            # map across ALL known paths (stale files use their freshly-
            # stat'd size from stale_sigs; already-cached files use the size
            # already sitting in _row_cache from a prior rebuild -- no extra
            # stat calls either way) so a stale file can be compared against
            # duplicates that AREN'T themselves stale this call, not just
            # other members of this batch.
            size_counts: dict[int, int] = {}
            for p in all_paths:
                sz = (
                    stale_sigs[p][1] if p in stale_sigs
                    else (self._row_cache[p].size if p in self._row_cache else None)
                )
                if sz is not None:
                    size_counts[sz] = size_counts.get(sz, 0) + 1

            def _needs_hash(p: str) -> bool:
                # 49b97a6a -- a pending hash-algo-version upgrade means every
                # stale row must be genuinely re-hashed under the new
                # algorithm (that IS the whole point of the one-time full
                # rehash pass); skipping unique-size files here would leave
                # their sha256 permanently None post-"upgrade" instead of a
                # real xxHash value, defeating 49b97a6a for exactly the
                # subset of rows the size-prefilter (e1fd4182) considers
                # cheapest to skip. Confirmed via
                # TestHashAlgoVersionUpgrade::test_legacy_db_triggers_full_rehash_on_upgrade.
                if self._pending_hash_upgrade:
                    return True
                sz = stale_sigs.get(p, (None, None))[1]
                return sz is None or size_counts.get(sz, 0) > 1

            # acac2599 -- cap workers at self._max_workers (constructor param
            # > MERIDIAN_OUTPUTS_MAX_WORKERS env var > 8, see
            # _resolve_max_workers) to avoid spawning hundreds of threads for
            # a huge outputs tree; I/O is the bottleneck, not CPU count.
            max_workers = min(self._max_workers, len(stale))
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
                    pool.submit(
                        _analyse_file, p, self._hasher, needs_hash=_needs_hash(p),
                    ): p
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
        #
        # 6ba77ada -- ALSO gated on `walk_complete`: this call has no deadline
        # check of its own and costs O(len(all_paths)). Before the resumable
        # walk, `all_paths` was always the complete tree exactly once per
        # call, so this was already an implicit per-call cost every caller
        # accepted. With a resumable walk, `all_paths` mid-pass is a
        # continuously-growing (but not yet authoritative) approximation --
        # paying this cost on every intermediate call as it grows would
        # itself become a new way to blow the budget before Phase 2 ever
        # gets to write anything (defeats the walk fix). Running it only
        # once the pass is complete (the picture is both final and at its
        # largest exactly once, not repeatedly) bounds the total cost to one
        # full-tree scan per pass instead of one per call. Rows written from
        # mid-pass stale files get a conservative is_archival=False default
        # in the meantime; the existing "update non-stale cached rows" loop
        # in _apply_precomputed corrects them once this finally runs.
        classifications: dict[str, ArchivalClassification] = {}
        if walk_complete and (stale or removed_paths):
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
            # 6ba77ada -- a path only leaves the pending-stale backlog once
            # it's actually been analysed and staged for write (`new_rows`);
            # anything in `stale` that _apply_precomputed didn't get to this
            # call (its own deadline check breaks early) simply stays queued
            # for the next call instead of being silently dropped.
            for r in new_rows:
                self._pending_stale.pop(r.path, None)
            if not walk_complete:
                # 6ba77ada -- the walk itself hasn't finished a full pass yet
                # (it's being resumed across future rebuild() calls), so the
                # index is known-incomplete regardless of whether Phase 1/2
                # themselves hit their own deadlines this call.
                self.last_rebuild_partial = True
            if changed:
                try:
                    con = self._connect()
                    self._ensure_schema(con)
                    # e8a2f710 -- prefer DuckDB's native Arrow zero-copy bulk
                    # path over any parameter-bound VALUES insert. Measured
                    # live: executemany (16.8s), a raw per-row execute()
                    # loop (15.6s), a single combined multi-row VALUES
                    # statement (10.0s), and a columnar UNNEST bind (15.7s)
                    # all land in the same order of magnitude for a 2000-row
                    # batch -- the cost tracks total (row x column) cells
                    # bound through DuckDB's SQL parameter layer almost
                    # linearly, regardless of batching strategy or storage
                    # backend (in-memory vs on-disk measured identical too).
                    # Registering a pyarrow Table and running INSERT ...
                    # SELECT * FROM it bypasses that layer entirely: same
                    # 2000-row batch measured at 0.110s (~150x faster than
                    # the original executemany), 0.047s for a 5138-row batch.
                    # Falls back to the combined-VALUES path (still ~40%
                    # faster than plain executemany) when pyarrow isn't
                    # installed, so this degrades gracefully rather than
                    # hard-requiring the new dependency.
                    _WRITE_CHUNK = _ResumableFileWalk._MAX_BATCH
                    if paths_to_delete:
                        for i in range(0, len(paths_to_delete), _WRITE_CHUNK):
                            chunk = paths_to_delete[i:i + _WRITE_CHUNK]
                            placeholders = ",".join("?" for _ in chunk)
                            con.execute(
                                f"DELETE FROM outputs_index WHERE path IN ({placeholders})",
                                chunk,
                            )
                    # Batch-insert only the new/changed rows.
                    if new_rows:
                        try:
                            import pyarrow as _pa  # noqa: PLC0415 -- optional, lazy
                        except ImportError:
                            _pa = None
                        if _pa is not None:
                            _arrow_table = _pa.table({
                                "path": [r.path for r in new_rows],
                                "content": [r.content for r in new_rows],
                                "mtime": [r.mtime for r in new_rows],
                                "sha256": [r.sha256 for r in new_rows],
                                "size": [r.size for r in new_rows],
                                "generating_script": [r.generating_script for r in new_rows],
                                "kind": [r.kind for r in new_rows],
                                "is_archival": [r.is_archival for r in new_rows],
                                "canonical_path": [r.canonical_path for r in new_rows],
                                "csv_columns": [
                                    json.dumps(r.csv_columns) if r.csv_columns else None
                                    for r in new_rows
                                ],
                                "json_keys": [
                                    json.dumps(r.json_keys) if r.json_keys else None
                                    for r in new_rows
                                ],
                            })
                            con.register("_outputs_index_bulk_insert", _arrow_table)
                            try:
                                con.execute(
                                    "INSERT INTO outputs_index "
                                    "(path, content, mtime, sha256, size, "
                                    "generating_script, kind, is_archival, "
                                    "canonical_path, csv_columns, json_keys) "
                                    "SELECT path, content, mtime, sha256, size, "
                                    "generating_script, kind, is_archival, "
                                    "canonical_path, csv_columns, json_keys "
                                    "FROM _outputs_index_bulk_insert"
                                )
                            finally:
                                con.unregister("_outputs_index_bulk_insert")
                        else:
                            for i in range(0, len(new_rows), _WRITE_CHUNK):
                                chunk_rows = new_rows[i:i + _WRITE_CHUNK]
                                row_placeholders = ",".join(
                                    "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)" for _ in chunk_rows
                                )
                                flat_params: list[Any] = []
                                for r in chunk_rows:
                                    flat_params.extend([
                                        r.path, r.content, r.mtime, r.sha256, r.size,
                                        r.generating_script, r.kind, r.is_archival,
                                        r.canonical_path,
                                        json.dumps(r.csv_columns) if r.csv_columns else None,
                                        json.dumps(r.json_keys) if r.json_keys else None,
                                    ])
                                con.execute(
                                    f"INSERT INTO outputs_index VALUES {row_placeholders}",
                                    flat_params,
                                )
                    # 77443d83 -- stage this call's own delta for the next
                    # Tantivy commit. Accumulates (rather than overwrites)
                    # across deferred calls, so whenever _rebuild_fts() next
                    # actually runs -- here or lazily from search() -- it
                    # commits the full outstanding delta as one small Tantivy
                    # transaction, never a full re-index.
                    self._pending_tantivy_deletes.update(paths_to_delete)
                    for r in new_rows:
                        self._pending_tantivy_upserts[r.path] = r
                    # b1789c0d / d9c76caa -- _rebuild_fts() has no deadline
                    # check of its own. On a huge cold tree, calling it
                    # unconditionally could push the overall rebuild() call
                    # well past its budget even after Phase 1/Phase 2 stopped
                    # on time (confirmed live against a 66k-file tree, back
                    # when _rebuild_fts() was DuckDB's full non-incremental
                    # PRAGMA create_fts_index -- both calls returned
                    # total_indexed=0 because that alone hit the ~4min
                    # external client timeout). Tantivy's delta-only commit
                    # (77443d83) makes this cost proportional to what changed
                    # rather than total row count, but the deadline gate below
                    # is kept regardless -- a large deferred delta is still a
                    # real cost worth deferring under a tight budget.
                    #
                    # d9c76caa addressed the WARM-tree case (skip when
                    # _fts_built AND deadline passed) but NOT the COLD-tree
                    # case: on a first-ever call _fts_built is always False,
                    # so the old guard never fired.
                    #
                    # Fix (b1789c0d): skip _rebuild_fts() whenever the deadline
                    # has already passed -- regardless of whether _fts_built is
                    # True or False. On the cold case (_fts_built=False) we set
                    # _fts_pending=True so search() knows to schedule a lazy
                    # FTS build on the NEXT call (once rows exist in the table).
                    # This guarantees every call makes real row-level progress
                    # AND the second call (warm rows, pending FTS) can pay the
                    # _rebuild_fts() cost with a fresh deadline, eventually
                    # returning actual BM25 results instead of forever empty hits.
                    deadline_passed = (
                        deadline is not None and time.monotonic() > deadline
                    )
                    if deadline_passed:
                        self.last_rebuild_partial = True
                        if not self._fts_built:
                            # Cold-tree case: rows were written but FTS index
                            # was never built. Flag it so search() attempts a
                            # lazy build on its next invocation.
                            self._fts_pending = True
                        # (Warm-tree case: _fts_built=True -- search() already
                        # has a working index; it will just be slightly stale
                        # until the next call with enough remaining budget.)
                    else:
                        self._fts_pending = False
                        self._rebuild_fts(con)
                except Exception as _db_write_exc:  # noqa: BLE001
                    _log.debug("OutputsFtsIndex.rebuild failed", exc_info=True)
                    # 1a799e52 -- this used to be swallowed at DEBUG level only,
                    # with total_indexed/total_in_index (both derived from the
                    # in-memory _row_cache populated BEFORE this write) still
                    # reporting growing "success". Surface it so callers can
                    # distinguish "indexed in memory, not yet confirmed on
                    # disk" from "confirmed persisted".
                    self.last_db_write_error = (
                        f"{type(_db_write_exc).__name__}: {_db_write_exc}"
                    )
            # b1789c0d: also set _fts_pending when the deadline expired before
            # any rows were written (changed=False because _apply_precomputed
            # exited immediately on a deeply-negative deadline). In that case
            # we never entered the `if changed:` block above, so the pending
            # flag was never set -- but the FTS index is still unbuilt on a
            # cold tree. Check last_rebuild_partial (set by _apply_precomputed)
            # to catch this case.
            if self.last_rebuild_partial and not self._fts_built:
                self._fts_pending = True
            # 49b97a6a -- once a hash-algo upgrade is pending (_connect()
            # found an old-version DB and skipped cache rehydration so every
            # row would look cold), only persist the new version marker once
            # THIS call has genuinely converged: the walk finished a full
            # pass, no deadline was breached, and nothing is left in the
            # stale backlog. Writing the marker any earlier would let a
            # process restart mid-upgrade rehydrate a DB that still has a
            # genuine SHA-256/xxHash mix as "fully current" -- the exact
            # silent-mix bug this item exists to prevent.
            if (self._pending_hash_upgrade and not self.last_rebuild_partial
                    and not self._pending_stale):
                try:
                    upgrade_con = self._connect()
                    self._write_hash_algo_version(upgrade_con, _HASH_ALGO_VERSION)
                    self._pending_hash_upgrade = False
                    _log.info(
                        "OutputsFtsIndex: full re-hash upgrade complete for %r",
                        self._db_path,
                    )
                except Exception:  # noqa: BLE001
                    _log.debug(
                        "OutputsFtsIndex.rebuild: failed to persist "
                        "hash_algo_version", exc_info=True,
                    )
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
        """77443d83 -- commit exactly the rows that changed as one small
        Tantivy transaction (this call's own delta, plus anything
        accumulated across a previously-deferred call), instead of DuckDB's
        full non-incremental ``PRAGMA create_fts_index`` rebuild. Tantivy's
        own background segment merge handles consolidation, not us.

        ``con`` is still accepted (rather than dropped) because the one-time
        migration check (8163816e) needs it to read pre-existing DuckDB rows,
        and because callers/tests hold a bound reference to this method and
        call it as ``_rebuild_fts(con)``.
        """
        import tantivy  # noqa: PLC0415
        index, writer = self._connect_tantivy()
        deletes, self._pending_tantivy_deletes = self._pending_tantivy_deletes, set()
        upserts, self._pending_tantivy_upserts = self._pending_tantivy_upserts, {}
        if deletes or upserts:
            for p in deletes:
                writer.delete_documents("path", p)
            for row in upserts.values():
                writer.add_document(
                    tantivy.Document(path=row.path, content=row.content or "")
                )
            writer.commit()
            index.reload()
        if not self._tantivy_migration_checked:
            self._tantivy_migration_checked = True
            self._migrate_duckdb_rows_to_tantivy_if_needed(con, index, writer)
        self._fts_built = True

    def search(
        self, query: str, *, limit: int = 10, include_archival: bool = True,
    ) -> list[dict[str, Any]]:
        """BM25 search over the FTS index.  Best-effort: errors yield [].

        a6056886 -- queries the Tantivy index (77443d83) instead of DuckDB
        FTS, but preserves the exact same external contract: the returned
        hit shape (path/score/bm25/is_archival/canonical_path/kind/
        generating_script/csv_columns/json_keys/size/mtime), the archival
        score penalty (bm25 * 0.5), and the DESC-by-score ordering are all
        unchanged. Tantivy only ever stores path+content (see
        _tantivy_schema); every other column still lives in DuckDB, so a
        match is resolved to path+bm25 first, then hydrated via a targeted
        ``WHERE path IN (...)`` lookup against the metadata table -- same
        division of labour as before (FTS engine ranks, DuckDB holds data),
        just with the ranking engine swapped out.

        b1789c0d: when _fts_pending is True (rows exist in the table from a
        prior partial rebuild but _rebuild_fts() was deferred because that
        call's deadline expired first), this method attempts a lazy FTS build
        now -- the MCP call that triggered SEARCH has a fresh budget, so paying
        _rebuild_fts()'s per-row cost here is the right moment.  Once built,
        _fts_pending is cleared and _fts_built is set so subsequent calls
        skip the build entirely (warm path).
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
                    # b1789c0d: _fts_pending means rows are ready in the table
                    # but FTS was deferred from a prior rebuild(). Attempt a
                    # lazy build now. An empty pending delta is a no-op
                    # (mirrors the old "FTS over an empty table is harmless").
                    self._fts_pending = False
                    self._rebuild_fts(con)
                index, _writer = self._connect_tantivy()
                # 52cbe5d8 -- a single Searcher snapshot must be used for BOTH
                # the query (which returns DocAddress values that are only
                # valid relative to the segment layout of the searcher that
                # produced them) and the doc() lookups below. The previous
                # code called index.searcher() a SECOND time after the query
                # to resolve each hit's path -- if Tantivy's background
                # segment-merge thread swapped in a new segment layout between
                # the two calls (a real race, observed live), the first
                # searcher's DocAddress values become invalid against the
                # second searcher and searcher.doc(addr) raises a Rust-level
                # panic (pyo3_runtime.PanicException: "index out of bounds")
                # that is NOT caught by the `except Exception` below --
                # crashing the whole call instead of the documented
                # best-effort "errors yield []" contract. Confirmed via
                # TestTantivySearchIndex::test_search_reuses_single_searcher_snapshot.
                searcher = index.searcher()
                parsed_query = index.parse_query(q, ["content"])
                tantivy_hits = searcher.search(parsed_query, safe_limit).hits
                if not tantivy_hits:
                    return []
                bm25_by_path: dict[str, float] = {}
                for score, addr in tantivy_hits:
                    path = searcher.doc(addr).get_first("path")
                    if path is not None:
                        bm25_by_path[path] = float(score)
                if not bm25_by_path:
                    return []
                placeholders = ",".join("?" for _ in bm25_by_path)
                sql = (
                    "SELECT path, content, mtime, sha256, size, generating_script, "
                    "kind, is_archival, canonical_path, csv_columns, json_keys "
                    f"FROM outputs_index WHERE path IN ({placeholders})"
                )
                relation = con.execute(sql, list(bm25_by_path.keys()))
                columns = [c[0] for c in relation.description]
                fetched = relation.fetchall()
            except Exception:  # noqa: BLE001
                _log.debug("OutputsFtsIndex.search failed", exc_info=True)
                return []
        hits: list[dict[str, Any]] = []
        for row in fetched:
            rec = dict(zip(columns, row))
            bm25 = bm25_by_path.get(rec["path"])
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
            # 77443d83 -- the Tantivy writer is always owned by this instance
            # (never passed in via the constructor, unlike `connection`), so
            # it's cleaned up unconditionally.
            if self._tantivy_writer is not None:
                try:
                    self._tantivy_writer.wait_merging_threads()
                except Exception:  # noqa: BLE001
                    _log.debug(
                        "OutputsFtsIndex.close: tantivy writer cleanup failed",
                        exc_info=True,
                    )
                self._tantivy_writer = None
            self._tantivy_index = None


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

    b1789c0d: on a cold, large tree the first call may not complete the FTS
    rebuild within its budget.  In that case ``total_indexed`` reflects how
    many rows were committed to the DB this call, ``total_in_index`` reflects
    the cumulative row count across ALL prior calls (which may be larger after
    the second call), and ``partial=True`` signals more indexing remains.
    Callers should re-invoke search_outputs to get progressively better
    results as the index builds across multiple calls.  An empty ``hits``
    list with ``partial=True`` means the FTS index has not yet been built
    (still pending) -- check ``total_in_index`` to see if rows already exist.
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
    # b1789c0d -- expose cumulative row count from the DB (which may be
    # larger than total_indexed on a partial rebuild that resumes prior work)
    # so callers can distinguish "cold tree, indexing in progress" from
    # "empty tree, nothing to find". total_in_index == len(index._row_cache)
    # because _row_cache always mirrors what is (or will be) in the DB.
    result["total_in_index"] = len(index._row_cache)
    if index.last_db_write_error:
        # 1a799e52 -- a real Phase 2 persistence failure this call. Rows may
        # still be visible in total_indexed/total_in_index (in-memory
        # row_cache), but they were NOT confirmed written to disk.
        result["db_write_error"] = index.last_db_write_error
    hits = index.search(query, limit=limit, include_archival=include_archival)
    for hit in hits:
        hit["annotations"] = index.get_annotations_for_path(hit["path"])
    result["hits"] = hits
    if index.last_rebuild_partial:
        result["partial"] = True
    if index._fts_pending:
        # Rows exist but FTS index hasn't been built yet.  Signal this so the
        # caller knows to re-invoke (the next search() call will build FTS).
        result["fts_pending"] = True
        result["partial"] = True
    if index._last_tantivy_error:
        # 9a18a2b2 -- Tantivy's single-writer lock was held by another
        # writer during this call. rebuild()/search() already degraded
        # gracefully (never raised), but a caller silently getting fewer/no
        # hits with no indication why isn't actionable -- surface the same
        # clear message OutputsFtsIndex already logged at WARNING.
        result["tantivy_lock_warning"] = index._last_tantivy_error
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


# ---------------------------------------------------------------------------
# Disposable log search: Tier 0 ripgrep scan + Tier 1 opportunistic sniffing
# ---------------------------------------------------------------------------
#
# Deliberately NOT built on OutputsFtsIndex.  Logs have no guaranteed
# structure -- rotated files, plain text, JSON-lines, syslog, mixed formats
# within the same directory -- so a persistent DuckDB/Tantivy schema would
# drift stale the moment a file rotates or a format changes, and the
# maintenance cost (staleness tracking, incremental rebuild, FTS commits)
# buys nothing for a directory that's usually far smaller than a full
# outputs/docs tree. Every call re-scans the tree fresh; there is no cache
# to invalidate and nothing to go stale.
#
#   Tier 0 -- always-on. Shells out to `rg` (ripgrep) for a sub-second
#             parallel regex scan when it's on PATH; transparently falls back
#             to an equivalent pure-Python `re` scan (same match semantics,
#             same secret-path exclusion, same budgets) when it isn't, so the
#             tool always returns real results rather than an error.
#   Tier 1 -- opportunistic, layered on the SAME scan (no second pass over
#             the files). Each Tier-0 match line is cheaply sniffed for a
#             timestamp and/or a JSON object. Matches with a sniffed signal
#             rank above plain ones (by severity, then recency). A line that
#             sniffs nothing free-falls back to Tier 0's own scan order --
#             no extra cost is paid for the miss.

_LOG_SCAN_TIMEOUT_SECONDS = 5.0
_LOG_MAX_MATCHES_PER_FILE = 200
_LOG_MAX_TOTAL_MATCHES = 2000
_LOG_LINE_PREVIEW_CHARS = 500

# Tier-1 timestamp sniffing: common log timestamp shapes. Order matters --
# the first pattern that matches a line wins, so put the least ambiguous
# shapes first.
_LOG_TS_FORMATS: tuple[tuple["re.Pattern[str]", str], ...] = (
    # ISO-8601 / RFC-3339: 2026-07-18T16:10:32.123Z, .../+05:30, ".../ ...".
    (
        re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"),
        "iso8601",
    ),
    # Common (Apache/nginx) log format: 18/Jul/2026:16:10:32 +0000
    (re.compile(r"\d{2}/[A-Za-z]{3}/\d{4}:\d{2}:\d{2}:\d{2}(?:\s[+-]\d{4})?"), "clf"),
    # syslog / journalctl short form: "Jul 18 16:10:32" -- no year in the line.
    (re.compile(r"\b[A-Z][a-z]{2}\s+\d{1,2}\s\d{2}:\d{2}:\d{2}\b"), "syslog"),
)

_LOG_TS_PARSE_FORMATS: dict[str, tuple[str, ...]] = {
    "iso8601": (
        "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S",
    ),
    "clf": ("%d/%b/%Y:%H:%M:%S %z", "%d/%b/%Y:%H:%M:%S"),
    "syslog": ("%b %d %H:%M:%S",),
}


def _sniff_timestamp(line: str) -> tuple[str | None, float | None]:
    """Tier-1: best-effort timestamp extraction. Returns (raw_text, epoch).

    Never raises -- an unparsable/ambiguous match just yields (None, None),
    which is the free Tier-0 fallback signalled by :attr:`LogMatch.tier`.
    """
    for pattern, kind in _LOG_TS_FORMATS:
        m = pattern.search(line)
        if not m:
            continue
        raw = m.group(0)
        # Normalize to something datetime.strptime's %z can parse: "Z" -> UTC
        # offset, "+05:30" -> "+0530" (strptime's %z wants no colon).
        normalized = raw.replace("Z", "+0000")
        normalized = re.sub(r"([+-]\d{2}):(\d{2})$", r"\1\2", normalized)
        for fmt in _LOG_TS_PARSE_FORMATS[kind]:
            try:
                dt = datetime.strptime(normalized, fmt)
            except ValueError:
                continue
            if kind == "syslog":
                # No year in a syslog-short timestamp -- assume "now"'s year;
                # good enough for relative recency ranking within one scan.
                dt = dt.replace(year=datetime.now(timezone.utc).year)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return raw, dt.timestamp()
    return None, None


_LOG_LEVEL_RE = re.compile(
    r"\b(FATAL|CRITICAL|ERROR|WARN(?:ING)?|INFO|DEBUG|TRACE)\b", re.IGNORECASE
)
_LOG_LEVEL_RANK: dict[str, int] = {
    "FATAL": 5, "CRITICAL": 5, "ERROR": 4, "WARN": 3, "WARNING": 3,
    "INFO": 2, "DEBUG": 1, "TRACE": 0,
}


def _sniff_json(line: str) -> dict[str, Any] | None:
    """Tier-1: best-effort JSON-object sniff for one log line.

    Tries the whole (stripped) line first -- the common "one JSON object per
    line" structured-logging convention -- then falls back to the substring
    between the first ``{`` and last ``}`` (handles a leading timestamp/prefix
    before the JSON payload, e.g. ``2026-07-18 16:10:32 {"level":"error"}``).
    Returns None (free Tier-0 fallback) on anything that doesn't parse --
    this must never cost more than a couple of cheap attempts per line.
    """
    stripped = line.strip()
    if not stripped:
        return None
    candidates: list[str] = []
    if stripped[0] == "{" and stripped[-1] == "}":
        candidates.append(stripped)
    else:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start != -1 and end > start:
            candidates.append(stripped[start:end + 1])
    for candidate in candidates:
        try:
            obj = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict):
            return obj
    return None


def _sniff_level(line: str, json_fields: dict[str, Any] | None) -> str | None:
    """Tier-1: best-effort severity-level sniff -- structured field first,
    falling back to a bare regex scan of the raw line."""
    if json_fields:
        for key in ("level", "severity", "lvl", "loglevel", "log_level"):
            val = json_fields.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip().upper()
    m = _LOG_LEVEL_RE.search(line)
    return m.group(1).upper() if m else None


@dataclass
class LogMatch:
    """One matched log line: Tier-0 position plus opportunistic Tier-1 signals."""

    path: str
    line_number: int
    line: str
    scan_order: int
    timestamp_raw: str | None = None
    timestamp_epoch: float | None = None
    level: str | None = None
    json_fields: dict[str, Any] | None = None

    @property
    def tier(self) -> int:
        """1 if any Tier-1 signal was sniffed for this line, else 0 (the
        line free-falls back to Tier-0 scan-order ranking)."""
        return 1 if (self.timestamp_epoch is not None or self.json_fields is not None) else 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "line_number": self.line_number,
            "line": self.line,
            "tier": self.tier,
            "timestamp": self.timestamp_raw,
            "timestamp_epoch": self.timestamp_epoch,
            "level": self.level,
            "json_fields": self.json_fields,
        }


def _rank_key(m: LogMatch) -> tuple[int, int, float, int]:
    """Tier-1-aware ranking key, used with ``sort(key=_rank_key, reverse=True)``.

    Matches carrying a Tier-1 signal (sniffed level and/or timestamp) sort
    above plain ones, ordered by severity then recency. A match with NO
    Tier-1 signal gets tier1_group=0 and level_rank=-1/timestamp=0.0, so
    ties within that group fall through to ``-scan_order`` -- i.e. it keeps
    exactly the order Tier 0's scan already produced it in. That's the "free
    fallback to Tier 0" the sniffing never pays extra for.
    """
    level_rank = _LOG_LEVEL_RANK.get((m.level or "").upper(), -1)
    has_ts = m.timestamp_epoch is not None
    tier1_group = (1 if level_rank >= 0 else 0) + (1 if has_ts else 0)
    return (tier1_group, level_rank, m.timestamp_epoch if has_ts else 0.0, -m.scan_order)


def _rg_binary() -> str | None:
    return shutil.which("rg")


def _run_ripgrep(
    logs_dir: str, pattern: str, *, timeout_seconds: float, max_total_matches: int,
) -> list[tuple[str, int, str]] | None:
    """Tier 0: shell out to ripgrep for a sub-second parallel scan.

    Returns a list of ``(path, line_number, line_text)`` in ripgrep's own
    match order, or None if ``rg`` isn't on PATH, the call errors out, or the
    timeout is hit -- the caller then falls back to :func:`_scan_logs_python`
    so a missing/misbehaving binary degrades gracefully instead of surfacing
    a CLI error to the MCP caller.
    """
    rg_bin = _rg_binary()
    if not rg_bin:
        return None
    cmd = [
        rg_bin, "--json", "-i", "--no-heading",
        "-m", str(_LOG_MAX_MATCHES_PER_FILE),
        "-e", pattern, logs_dir,
    ]
    try:
        proc = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired):
        _log.debug("search_logs: ripgrep invocation failed/timed out", exc_info=True)
        return None
    # rg exit code 0 == matches found, 1 == no matches (not a failure), 2 ==
    # usage/regex error -- fall back to the Python scanner for the latter so
    # a pattern ripgrep's regex engine rejects still gets a real attempt.
    if proc.returncode not in (0, 1):
        _log.debug(
            "search_logs: ripgrep exited %s: %s", proc.returncode,
            proc.stderr.decode("utf-8", "replace")[:300],
        )
        return None
    out: list[tuple[str, int, str]] = []
    for raw_line in proc.stdout.splitlines():
        if len(out) >= max_total_matches:
            break
        try:
            rec = json.loads(raw_line)
        except (ValueError, TypeError):
            continue
        if rec.get("type") != "match":
            continue
        data = rec.get("data") or {}
        path = (data.get("path") or {}).get("text")
        line_number = data.get("line_number")
        text = (data.get("lines") or {}).get("text")
        if not path or line_number is None or text is None:
            continue
        if is_secret_path(path):
            continue
        out.append((path, int(line_number), text.rstrip("\r\n")))
    return out


def _scan_logs_python(
    logs_dir: str, pattern: str, *, timeout_seconds: float,
    max_matches_per_file: int, max_total_matches: int,
) -> list[tuple[str, int, str]]:
    """Tier 0 fallback when ``rg`` isn't on PATH: pure-Python line scan.

    Same secret-path exclusion (:func:`_iter_safe_output_files`, shared with
    the outputs indexer) and per-file/total match caps as the ripgrep path,
    plus its own wall-clock deadline so a huge/cold logs tree degrades
    (returns whatever it found so far) instead of hanging the MCP call. If
    ``pattern`` isn't a valid Python regex, falls back further to a literal
    (escaped) substring match rather than raising.
    """
    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except re.error:
        regex = re.compile(re.escape(pattern), re.IGNORECASE)
    deadline = time.monotonic() + timeout_seconds
    out: list[tuple[str, int, str]] = []
    for path in _iter_safe_output_files(logs_dir):
        if len(out) >= max_total_matches or time.monotonic() > deadline:
            break
        per_file = 0
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for line_number, line in enumerate(fh, start=1):
                    if per_file >= max_matches_per_file or len(out) >= max_total_matches:
                        break
                    if time.monotonic() > deadline:
                        break
                    if regex.search(line):
                        out.append((path, line_number, line.rstrip("\r\n")))
                        per_file += 1
        except OSError:
            continue
    return out


def search_logs(
    logs_dir: str,
    query: str,
    *,
    limit: int = 20,
    timeout_seconds: float = _LOG_SCAN_TIMEOUT_SECONDS,
    max_line_chars: int = _LOG_LINE_PREVIEW_CHARS,
) -> dict[str, Any]:
    """Disposable log search: Tier 0 ripgrep scan + Tier 1 opportunistic ranking.

    Fully local, fully disposable -- unlike :class:`OutputsFtsIndex`, NO index
    is ever written to disk. Logs have no guaranteed structure (rotated
    files, plain text, JSON-lines, syslog, mixed formats side by side), so a
    persistent schema would drift stale the moment a file rotates; re-scanning
    fresh every call is the right trade-off for a tree that's usually far
    smaller than a full outputs/docs corpus.

    Tier 0 (always on): shells out to ``rg`` for a sub-second scan; falls
    back automatically to an equivalent pure-Python regex scan when ``rg``
    isn't installed. Secret-named files (``.env*``, ``*.key``, ``*secret*``,
    ``*credential*``, etc.) are never scanned -- same exclusion the outputs
    indexer uses (:func:`is_secret_path`).

    Tier 1 (opportunistic, layered on the SAME scan -- no second pass): each
    Tier-0 match line is cheaply sniffed for a timestamp and/or a JSON
    object. Matches with a sniffed signal rank above plain ones (severity,
    then recency). A line with no sniffable structure free-falls back to
    Tier 0's own scan order at no extra cost.

    Args:
      logs_dir:         Absolute path to the directory tree to search.
      query:            Search pattern. Ripgrep-flavoured regex when ``rg``
                        is available (case-insensitive); the same pattern is
                        applied as a Python ``re`` regex in the fallback
                        path, degrading further to a literal (escaped) match
                        if it isn't valid Python regex either.
      limit:            Maximum number of hits to return (default 20).
      timeout_seconds:  Wall-clock scan budget (default 5s) -- shared by both
                        the ripgrep subprocess and the Python fallback.
      max_line_chars:   Cap on returned line-text length (default 500).

    Returns:
      {logs_dir, query, hits, total_matched, engine} plus optional {error}.
      ``engine`` is ``"ripgrep"`` or ``"python-fallback"``. Each hit is a
      :meth:`LogMatch.to_dict` (path, line_number, line, tier, timestamp,
      timestamp_epoch, level, json_fields).
    """
    result: dict[str, Any] = {
        "logs_dir": logs_dir, "query": query, "hits": [], "total_matched": 0,
    }
    if not query or not str(query).strip():
        result["error"] = "query is required"
        return result
    if not os.path.isdir(logs_dir):
        result["error"] = f"logs_dir does not exist: {logs_dir}"
        return result

    raw = _run_ripgrep(
        logs_dir, query, timeout_seconds=timeout_seconds,
        max_total_matches=_LOG_MAX_TOTAL_MATCHES,
    )
    engine = "ripgrep"
    if raw is None:
        engine = "python-fallback"
        raw = _scan_logs_python(
            logs_dir, query, timeout_seconds=timeout_seconds,
            max_matches_per_file=_LOG_MAX_MATCHES_PER_FILE,
            max_total_matches=_LOG_MAX_TOTAL_MATCHES,
        )

    matches: list[LogMatch] = []
    for scan_order, (path, line_number, text) in enumerate(raw):
        json_fields = _sniff_json(text)
        ts_raw, ts_epoch = _sniff_timestamp(text)
        level = _sniff_level(text, json_fields)
        preview = text if len(text) <= max_line_chars else text[:max_line_chars] + "..."
        matches.append(LogMatch(
            path=path, line_number=line_number, line=preview,
            scan_order=scan_order, timestamp_raw=ts_raw, timestamp_epoch=ts_epoch,
            level=level, json_fields=json_fields,
        ))

    matches.sort(key=_rank_key, reverse=True)
    safe_limit = max(1, int(limit))
    result["hits"] = [m.to_dict() for m in matches[:safe_limit]]
    result["total_matched"] = len(matches)
    result["engine"] = engine
    return result
