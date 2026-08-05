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
import socket
import subprocess
import threading
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass, replace
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
    on_error: Callable[[str, OSError], None] | None = None,
):
    """Generator yielding regular files under ``outputs_dir`` that pass the
    secret-file exclusion filter AND the (optional) user exclude-pattern
    list, in deterministic (sorted-directories, sorted-files) order -- the
    same order :func:`_iter_safe_output_files` has always returned.

    ``on_error`` (item 6af1518d, requirement 1 -- convergence state's "last
    error" field): optional callback invoked as ``on_error(dir_path, exc)``
    whenever a directory can't be listed (permission denied, removed mid-walk,
    etc.). Purely observational -- the walk always continues past the failed
    directory exactly as before (best-effort, never aborts); this only lets a
    caller (:class:`_ResumableFileWalk` / :class:`OutputsFtsIndex`) SEE that
    it happened instead of the failure being silently swallowed at DEBUG log
    level with no caller-visible signal at all. Optional and additive --
    omitting it reproduces the exact prior behaviour.

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
        except OSError as exc:
            if on_error is not None:
                try:
                    on_error(root, exc)
                except Exception:  # noqa: BLE001 -- never let a caller's
                    # observer callback break the walk itself.
                    _log.debug(
                        "_walk_safe_output_files: on_error callback raised "
                        "for %r", root, exc_info=True,
                    )
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
    #
    # 3535b9ad -- this was hardcoded with no override, unlike sibling knobs
    # (MERIDIAN_OUTPUTS_MAX_WORKERS, MERIDIAN_OUTPUTS_EXCLUDE_PATTERNS). It was
    # sized conservatively because Phase 2's DB write used to cost ~8ms/row --
    # a big batch meant a proportionally huge, budget-eating write. That
    # constraint is now gone (pyarrow bulk insert, 4f78e70: 2000 rows write in
    # ~0.11s), and Phase 1/the walk are both also now dramatically faster
    # (c73c0dd7, 7fee82e, 4972a6d), so the ORIGINAL reason for capping at
    # exactly 2000 no longer applies at the same weight.
    #
    # 1bce8c41 -- FOLLOW-UP: 3535b9ad shipped configurability but left the
    # DEFAULT at 2000, so even a caller supplying a generous `max_seconds`
    # budget still stopped every drain() at 2000 files, wasting the rest of
    # its own time budget instead of walking closer to the full tree per
    # call (confirmed live against db4bb64b's real 244k-file convergence
    # run). The walk should be TIME-primary by default, not count-primary --
    # every drain() call already checks `deadline` on every single path
    # pulled (see drain() below), so raising this default doesn't weaken that
    # bound at all; it just stops an arbitrary, far-too-small count cap from
    # firing first. Bumped to an "effectively unbounded" default so the walk
    # only ever stops on `deadline` (or true exhaustion) by default, while an
    # explicit constructor arg or MERIDIAN_OUTPUTS_MAX_BATCH env var (see
    # _resolve_max_batch) still lets anyone who deliberately wants a real
    # count cap set one.
    #
    # IMPORTANT: this constant is a WALK-only concern -- this class's own
    # per-drain() cap, i.e. raw DISCOVERY capacity (how many paths a single
    # drain() call may pull off the filesystem generator). It must NOT also
    # become the DB write-chunk size (_WRITE_CHUNK) -- OutputsFtsIndex.__init__
    # resolves that SEPARATELY via its own `_WRITE_CHUNK_DEFAULT`/
    # `_resolve_write_chunk`, specifically so bumping this default doesn't
    # silently turn every DB write into one giant, unchunked SQL statement.
    #
    # b85394bd -- FOLLOW-UP to 1bce8c41: an intervening perf change (47a1c53)
    # quietly dropped this back down to 4_096 and repurposed
    # OutputsFtsIndex._adaptive_batch_limit() (memory/commit-pressure aware,
    # designed to bound ANALYSIS/write intake, see that method's docstring)
    # as rebuild()'s walk-drain cap too -- reintroducing exactly the
    # count-primary-not-time-primary regression 1bce8c41 fixed, silently:
    # every rebuild() call capped discovery at whatever the adaptive
    # controller sized for ANALYSIS, confirmed live as a flat 4_096-path
    # ceiling per call on a large root (2026-07-31). The two concerns are
    # separate: this constant governs DISCOVERY capacity only (bounded by
    # `deadline`, effectively unbounded by default per 1bce8c41's original
    # rationale, still restated below); `_adaptive_batch_limit()` governs how
    # much of the discovered backlog Phase 1/2 take on in a single call (see
    # rebuild()'s `analysis_limit`, resolved independently). Restored to
    # 1bce8c41's "effectively unbounded" default so discovery itself is
    # time-primary again; an explicit constructor arg or
    # MERIDIAN_OUTPUTS_MAX_BATCH env var still caps it for anyone who
    # deliberately wants a real count cap.
    _MAX_BATCH = 1_000_000_000
    _MAX_BATCH_ENV_VAR = "MERIDIAN_OUTPUTS_MAX_BATCH"

    @classmethod
    def _resolve_max_batch(cls, explicit: int | None) -> int:
        """Precedence: explicit constructor arg > env var > class default."""
        if explicit is not None:
            if explicit >= 1:
                return explicit
            _log.warning(
                "_ResumableFileWalk: max_batch=%r must be >= 1 -- falling "
                "back to default (%d)", explicit, cls._MAX_BATCH,
            )
            return cls._MAX_BATCH
        raw = os.environ.get(cls._MAX_BATCH_ENV_VAR)
        if raw is None or not raw.strip():
            return cls._MAX_BATCH
        try:
            value = int(raw.strip())
        except ValueError:
            _log.warning(
                "%s=%r is not a valid integer -- falling back to default (%d)",
                cls._MAX_BATCH_ENV_VAR, raw, cls._MAX_BATCH,
            )
            return cls._MAX_BATCH
        if value < 1:
            _log.warning(
                "%s=%r must be >= 1 -- falling back to default (%d)",
                cls._MAX_BATCH_ENV_VAR, raw, cls._MAX_BATCH,
            )
            return cls._MAX_BATCH
        return value

    def __init__(
        self, outputs_dir: str, *, exclude_patterns: tuple[str, ...] = (),
        max_batch: int | None = None,
        on_error: Callable[[str, OSError], None] | None = None,
    ) -> None:
        self._iterator = _walk_safe_output_files(
            outputs_dir, exclude_patterns=exclude_patterns, on_error=on_error,
        )
        self.exhausted = False
        self.max_batch = self._resolve_max_batch(max_batch)

    def drain(self, deadline: float | None) -> list[str]:
        """Pull paths until the walk is exhausted, ``deadline`` passes, or
        ``self.max_batch`` paths have been collected -- whichever comes first.

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
            if len(found) >= self.max_batch:
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
# File locking (requirement 2) + process-aware single-writer lease (a52216e2)
# ---------------------------------------------------------------------------
#
# a52216e2 -- "Wave 10D found no confirmed orphan to kill: the lock was held
# by one of several live concurrent server processes." Investigating that
# incident surfaced the REAL bug: this environment does not have `portalocker`
# installed (it is an optional, lazily-imported dependency -- see the
# original docstring below), so on a real deployment without it, the
# cross-process branch below used to be a complete no-op: `except ImportError:
# pass`. Two separate Meridian server processes each running IndexFileLock
# against the SAME on-disk db_path therefore had ZERO real mutual exclusion --
# only the in-process threading.Lock protected against contention WITHIN one
# process. Worse, a genuine (non-ImportError) acquisition failure was caught
# and silently logged at DEBUG, then treated as "acquired" -- so ANY failure
# to acquire the cross-process lock, for ANY reason, silently left ownership
# non-deterministic. Neither gap was ever surfaced anywhere a caller could see.
#
# This section closes both gaps:
#   1. A dependency-free, deterministic fallback lock (`os.O_CREAT |
#      os.O_EXCL` -- atomic file creation, same guarantee on POSIX and
#      Windows) is used automatically whenever portalocker is unavailable, so
#      REAL cross-process exclusivity now always exists, with or without the
#      optional dependency.
#   2. A genuine acquisition failure now RAISES IndexLockAcquireError instead
#      of being silently swallowed -- see that class's docstring.
#   3. Because the fallback lock's FILE (unlike portalocker's OS-level lock)
#      is not auto-removed if its owner process crashes, every lock now
#      carries a small lease (owner pid/hostname/session_id/started_at/
#      heartbeat_at) written into the lock file, and read_index_lock_owner()
#      lets any caller distinguish a lock held by a live, active owner from
#      one left behind by a dead one -- WITHOUT ever touching the recorded
#      process. Reclaiming a stale LOCK FILE and killing the process that
#      (legitimately or not) still holds it are two entirely different
#      things; this module only ever does the former.

# Per-canonical-path threading locks (in-process).
_THREADING_LOCKS: dict[str, threading.Lock] = {}
_THREADING_LOCKS_META = threading.Lock()


def _get_thread_lock(canonical: str) -> threading.Lock:
    with _THREADING_LOCKS_META:
        if canonical not in _THREADING_LOCKS:
            _THREADING_LOCKS[canonical] = threading.Lock()
        return _THREADING_LOCKS[canonical]


# How long a heartbeat may go silent before a recorded lock owner is treated
# as stale (only relevant when the owner's pid liveness can't be confirmed --
# e.g. a cross-host lease, or no psutil/ctypes check available). Overridable
# for tests / unusually slow environments.
_LOCK_STALE_SECONDS_DEFAULT = 120.0
_LOCK_STALE_SECONDS_ENV_VAR = "MERIDIAN_OUTPUTS_LOCK_STALE_SECONDS"

# How long the atomic-create fallback lock waits on a confirmed-ACTIVE (not
# stale) owner before giving up and raising IndexLockAcquireError. Only
# applies to the fallback mechanism -- the optional portalocker path keeps
# its historical indefinite-block-by-default behaviour unless a caller
# explicitly passes a timeout, so an environment where portalocker IS
# installed sees no behaviour change from this item.
_LOCK_TIMEOUT_SECONDS_DEFAULT = 60.0
_LOCK_TIMEOUT_SECONDS_ENV_VAR = "MERIDIAN_OUTPUTS_LOCK_TIMEOUT_SECONDS"


def _resolve_lock_stale_seconds() -> float:
    raw = os.environ.get(_LOCK_STALE_SECONDS_ENV_VAR)
    if raw is None or not raw.strip():
        return _LOCK_STALE_SECONDS_DEFAULT
    try:
        value = float(raw.strip())
    except ValueError:
        _log.warning(
            "%s=%r is not a valid number -- falling back to default (%.0fs)",
            _LOCK_STALE_SECONDS_ENV_VAR, raw, _LOCK_STALE_SECONDS_DEFAULT,
        )
        return _LOCK_STALE_SECONDS_DEFAULT
    if value <= 0:
        _log.warning(
            "%s=%r must be > 0 -- falling back to default (%.0fs)",
            _LOCK_STALE_SECONDS_ENV_VAR, raw, _LOCK_STALE_SECONDS_DEFAULT,
        )
        return _LOCK_STALE_SECONDS_DEFAULT
    return value


def _resolve_lock_timeout_seconds() -> float | None:
    raw = os.environ.get(_LOCK_TIMEOUT_SECONDS_ENV_VAR)
    if raw is None or not raw.strip():
        return _LOCK_TIMEOUT_SECONDS_DEFAULT
    stripped = raw.strip()
    if stripped.lower() in ("0", "none", "unbounded", "infinite"):
        return None  # explicit opt-in to block indefinitely, like portalocker
    try:
        value = float(stripped)
    except ValueError:
        _log.warning(
            "%s=%r is not a valid number -- falling back to default (%.0fs)",
            _LOCK_TIMEOUT_SECONDS_ENV_VAR, raw, _LOCK_TIMEOUT_SECONDS_DEFAULT,
        )
        return _LOCK_TIMEOUT_SECONDS_DEFAULT
    if value <= 0:
        return None
    return value


def _current_hostname() -> str:
    try:
        return socket.gethostname()
    except Exception:  # noqa: BLE001 -- diagnostics only, never fatal
        return "unknown-host"


def _pid_alive(pid: Any) -> bool | None:
    """Best-effort, NEVER-terminating liveness check for ``pid`` on THIS host.

    Returns ``True``/``False`` when determinable, ``None`` when genuinely
    indeterminate (no usable check available) -- callers must treat ``None``
    as "unknown", never as "dead". This function only ever OBSERVES process
    state; it never signals, terminates, or otherwise touches the process
    (see the module-level note above -- reclaiming a stale lock FILE and
    killing the process that owns it are different things, and this function
    is only ever used for the former).
    """
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        import psutil  # noqa: PLC0415 -- optional, already used elsewhere here
    except ImportError:
        psutil = None  # type: ignore[assignment]
    if psutil is not None:
        try:
            return bool(psutil.pid_exists(pid))
        except Exception:  # noqa: BLE001
            _log.debug("_pid_alive: psutil check failed for pid=%r", pid, exc_info=True)
    if os.name == "posix":
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # exists, just not ours to signal
        except OSError:
            return None
    # Windows without psutil -- best-effort via OpenProcess + GetExitCodeProcess
    # (never terminates anything: PROCESS_QUERY_LIMITED_INFORMATION is a
    # read-only query right).
    #
    # a52216e2 -- OpenProcess succeeding is NOT sufficient on its own: Windows
    # keeps a process's kernel object (and its pid) alive for as long as ANY
    # handle to it remains open ANYWHERE (e.g. this test's own subprocess.
    # Popen object still holding its creation handle), even after the process
    # has actually exited -- confirmed live via a real crashed-child test that
    # OpenProcess(pid) kept succeeding well after Popen.wait() had already
    # returned a real exit code. GetExitCodeProcess on that SAME handle
    # distinguishes "kernel object exists" from "process is actually still
    # running" (STILL_ACTIVE == 259) and is the correct check.
    try:
        import ctypes
        import ctypes.wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        ERROR_ACCESS_DENIED = 5
        STILL_ACTIVE = 259
        handle = ctypes.windll.kernel32.OpenProcess(  # type: ignore[attr-defined]
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid,
        )
        if not handle:
            if ctypes.get_last_error() == ERROR_ACCESS_DENIED:
                return True  # exists, just not ours to query fully
            return False
        try:
            exit_code = ctypes.wintypes.DWORD()
            ok = ctypes.windll.kernel32.GetExitCodeProcess(  # type: ignore[attr-defined]
                handle, ctypes.byref(exit_code),
            )
            if not ok:
                return None  # indeterminate -- never guess "dead"
            return exit_code.value == STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return None


class IndexLockAcquireError(Exception):
    """Raised when :class:`IndexFileLock` could not obtain REAL cross-process
    exclusivity for a persistent (non-``:memory:``) ``db_path`` (a52216e2).

    Deliberately distinct from silently proceeding without a lock: earlier
    revisions of this class caught every acquisition failure at DEBUG level
    and continued as if the lock had been acquired, which meant two
    concurrent Meridian server processes could both believe they held
    exclusive write access whenever the cross-process layer failed for any
    reason. That silent-success behaviour is exactly the non-determinism
    a52216e2 was opened to close, so this is raised loudly instead --
    mirroring :class:`TantivyLockConflict`'s precedent (9a18a2b2) of naming
    ONE specific, expected failure mode rather than letting it vanish into a
    broad ``except Exception`` at some unrelated call site.

    ``owner`` (optional): the :class:`IndexLockOwner` snapshot describing who
    held the lock when acquisition gave up, when known -- lets a caller
    surface *why* (pid/session/heartbeat age) instead of just "busy".
    """

    def __init__(self, message: str, *, owner: "IndexLockOwner | None" = None) -> None:
        super().__init__(message)
        self.owner = owner


@dataclass
class IndexLockOwner:
    """Read-only snapshot of who currently holds -- or last held -- the
    single-writer lock for one ``db_path`` (a52216e2). Produced by
    :func:`read_index_lock_owner`, which never acquires the real lock and
    never signals/terminates any process -- this is diagnostics data only.

    ``is_stale`` means the recorded owner is safe to treat as ABANDONED (dead
    pid on this host, or a silent heartbeat past the configured threshold when
    liveness can't be checked directly) -- i.e. the lock FILE may be safely
    reclaimed. It is never a signal to kill anything: a stale reading only
    ever justifies deleting the leftover lock file, exactly like a crashed
    process's OS-level lock would already have been released automatically
    had portalocker's lock been in use instead.
    """

    db_path: str
    lock_path: str | None
    held: bool
    pid: int | None = None
    hostname: str | None = None
    session_id: str | None = None
    started_at: float | None = None
    heartbeat_at: float | None = None
    age_seconds: float | None = None
    lock_mode: str | None = None
    pid_alive: bool | None = None
    is_stale: bool = False
    stale_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _write_lease_to_handle(
    fh: Any, *, pid: int, hostname: str, session_id: str | None,
    started_at: float, heartbeat_at: float, lock_mode: str,
) -> None:
    """Overwrite ``fh`` (already positioned at a lock file this caller owns)
    with the current lease payload. Best-effort by design -- a lease-write
    failure must never take down the caller that just acquired the real
    lock; see the callers' own ``try/except`` wrapping.

    Deliberately does NOT ``os.fsync`` -- the lease is diagnostics-only
    (read_index_lock_owner never trusts it for the actual exclusivity
    guarantee, only the atomic-create/portalocker primitive does that), and
    on the SAME live machine a plain ``flush()`` is already immediately
    visible to any other process reading the file through the shared OS page
    cache. ``fsync`` would only buy durability across a hard crash/power-loss
    of a value nobody reads across restarts, at a real, measured per-call
    cost (called on every lock acquire AND every heartbeat -- see a52216e2's
    investigation notes for the tight-rebuild-budget regression this caused
    when it was here)."""
    payload = json.dumps({
        "pid": pid, "hostname": hostname, "session_id": session_id,
        "started_at": started_at, "heartbeat_at": heartbeat_at,
        "lock_mode": lock_mode,
    })
    fh.seek(0)
    fh.write(payload)
    fh.truncate()
    fh.flush()


def _read_lease_from_path(path: str) -> dict[str, Any] | None:
    """Best-effort parse of a lock file's lease payload. ``None`` for a
    missing file, an unreadable file, or content that isn't a JSON object
    (e.g. a lock file whose owner acquired it but hasn't written its lease
    yet -- a narrow, harmless race window)."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return None
    if not text.strip():
        return None
    try:
        obj = json.loads(text)
    except Exception:  # noqa: BLE001
        return None
    return obj if isinstance(obj, dict) else None


def _probe_portalocker_held(lock_path: str, portalocker_module: Any) -> bool | None:
    """Best-effort, side-effect-minimal check: is ``lock_path`` CURRENTLY
    locked via portalocker by ANY process (including this one, via a
    different handle)? A non-blocking probe lock/unlock cycle on a fresh
    handle is the standard technique for this -- if the probe itself
    succeeds, nobody holds the real lock right now (the probe never keeps
    it). Returns ``None`` on any I/O error -- genuinely indeterminate, never
    guessed."""
    try:
        # Ensure the file exists so the probe targets the same path a real
        # acquirer would -- mirrors the original acquire() path (which always
        # opened the lock file in "w" mode on every attempt).
        with open(lock_path, "a", encoding="utf-8"):
            pass
        probe = open(lock_path, "r+", encoding="utf-8")
    except OSError:
        return None
    try:
        try:
            portalocker_module.lock(
                probe, portalocker_module.LOCK_EX | portalocker_module.LOCK_NB,
            )
        except Exception:  # noqa: BLE001 -- LockException or a platform OSError
            return True
        try:
            portalocker_module.unlock(probe)
        except Exception:  # noqa: BLE001
            pass
        return False
    finally:
        try:
            probe.close()
        except Exception:  # noqa: BLE001
            pass


def read_index_lock_owner(
    db_path: str, *, stale_seconds: float | None = None,
) -> IndexLockOwner:
    """Read-only snapshot of the single-writer lock/lease state for
    ``db_path`` (a52216e2). Never acquires the real write lock, never blocks
    on contention, and never signals/terminates any process -- safe to call
    from any read path (search, diagnostics, get_convergence_state) without
    disturbing a live writer. See :class:`IndexLockOwner` for field meanings.
    """
    if not db_path or db_path == ":memory:":
        return IndexLockOwner(db_path=db_path, lock_path=None, held=False)
    canonical = os.path.abspath(db_path)
    lock_path = canonical + ".lock"
    meta = _read_lease_from_path(lock_path)
    threshold = stale_seconds if stale_seconds is not None else _resolve_lock_stale_seconds()
    now = time.time()

    try:
        import portalocker  # noqa: PLC0415 -- optional
    except ImportError:
        held: bool | None = os.path.exists(lock_path)
    else:
        held = _probe_portalocker_held(lock_path, portalocker)

    if meta is None:
        # No lease content -- either never used, cleanly released (atomic
        # mode always removes the file on release; see IndexFileLock.release),
        # or a narrow race where the file was just created but its owner
        # hasn't written a lease yet. None of those are "stale" -- there is
        # nothing here to reclaim.
        return IndexLockOwner(
            db_path=db_path, lock_path=lock_path, held=bool(held) if held is not None else False,
        )

    pid = meta.get("pid")
    hostname = meta.get("hostname")
    session_id = meta.get("session_id")
    started_at = meta.get("started_at")
    heartbeat_at = meta.get("heartbeat_at")
    age = (
        now - heartbeat_at if isinstance(heartbeat_at, (int, float)) else None
    )

    pid_alive: bool | None = None
    if isinstance(pid, int) and hostname == _current_hostname():
        pid_alive = _pid_alive(pid)

    is_stale = False
    stale_reason: str | None = None
    if held is False:
        # OS/atomic-file-confirmed: nobody holds it right now. Leftover
        # metadata here is purely historical (portalocker mode leaves the
        # file in place after a clean release) -- nothing to reclaim.
        is_stale = False
    elif pid_alive is False:
        is_stale = True
        stale_reason = f"recorded owner pid={pid} on this host is no longer running"
    elif age is not None and age > threshold:
        is_stale = True
        stale_reason = f"heartbeat is {age:.1f}s old (> {threshold:.0f}s threshold)"

    return IndexLockOwner(
        db_path=db_path, lock_path=lock_path,
        held=bool(held) if held is not None else True,
        pid=pid, hostname=hostname, session_id=session_id,
        started_at=started_at, heartbeat_at=heartbeat_at, age_seconds=age,
        lock_mode=meta.get("lock_mode"), pid_alive=pid_alive,
        is_stale=is_stale, stale_reason=stale_reason,
    )


class IndexFileLock:
    """Exclusive write lock for one index DB file.

    Uses a per-path threading.Lock for in-process safety (always) PLUS one of
    two real cross-process mechanisms for a persistent (non-``:memory:``)
    path:

    * ``portalocker`` (when importable) -- an OS-level advisory lock, kernel-
      released automatically if the owning process dies. Unchanged from the
      historical behaviour: blocks indefinitely by default.
    * An atomic-create fallback (``os.O_CREAT | os.O_EXCL``) used whenever
      portalocker is unavailable (a52216e2) -- a real, deterministic,
      dependency-free mutual-exclusion primitive on both POSIX and Windows.
      Bounded by ``timeout`` (default :func:`_resolve_lock_timeout_seconds`)
      against a confirmed-ACTIVE owner; a confirmed-STALE owner's lock file is
      reclaimed automatically (never its process -- see
      :func:`read_index_lock_owner`).

    Both mechanisms write a small lease (owner pid/hostname/session_id/
    started_at/heartbeat_at) into the lock file once acquired, readable via
    :func:`read_index_lock_owner` without holding the lock.

    A genuine acquisition failure now raises :class:`IndexLockAcquireError`
    instead of being silently treated as success (a52216e2) -- callers that
    need to degrade gracefully (rather than let this propagate) should catch
    it explicitly, the same way :class:`TantivyLockConflict` is already
    handled at its call sites.

    Usage::

        with IndexFileLock(db_path):
            # exclusive write access to db_path
            ...
    """

    _ATOMIC_POLL_INITIAL = 0.05
    _ATOMIC_POLL_MAX = 0.5

    def __init__(
        self, db_path: str, *, session_id: str | None = None,
        timeout: float | None = None,
    ) -> None:
        self._db_path = db_path
        self._canonical = os.path.abspath(db_path) if db_path != ":memory:" else db_path
        self._thread_lock = _get_thread_lock(self._canonical)
        self._file_handle: Any = None
        self._session_id = session_id
        self._timeout = timeout
        self._lock_mode: str | None = None
        self._started_at: float | None = None
        self._last_heartbeat_write: float | None = None

    @property
    def session_id(self) -> str | None:
        return self._session_id

    def acquire(self, *, timeout: float | None = None) -> None:
        self._thread_lock.acquire()
        if self._canonical == ":memory:":
            return
        lock_path = self._canonical + ".lock"
        try:
            import portalocker  # noqa: PLC0415 -- optional
        except ImportError:
            portalocker = None  # type: ignore[assignment]

        try:
            if portalocker is not None:
                self._acquire_portalocker(portalocker, lock_path)
            else:
                self._acquire_atomic(lock_path, timeout)
        except BaseException:
            self._thread_lock.release()
            raise

        now = time.time()
        self._started_at = now
        self._write_lease(now)

    def _acquire_portalocker(self, portalocker: Any, lock_path: str) -> None:
        try:
            self._file_handle = open(lock_path, "w", encoding="utf-8")  # noqa: WPS515
            portalocker.lock(self._file_handle, portalocker.LOCK_EX)
        except Exception as exc:  # noqa: BLE001 -- a52216e2: no longer swallowed
            if self._file_handle is not None:
                try:
                    self._file_handle.close()
                except Exception:  # noqa: BLE001
                    pass
                self._file_handle = None
            _log.warning(
                "IndexFileLock: portalocker acquire failed for %r -- refusing "
                "to proceed without a real cross-process lock (a52216e2)",
                lock_path, exc_info=True,
            )
            raise IndexLockAcquireError(
                f"failed to acquire the cross-process lock at {lock_path!r}: {exc}"
            ) from exc
        self._lock_mode = "portalocker"

    def _acquire_atomic(self, lock_path: str, timeout: float | None) -> None:
        effective_timeout = (
            timeout if timeout is not None
            else self._timeout if self._timeout is not None
            else _resolve_lock_timeout_seconds()
        )
        deadline = None if effective_timeout is None else time.monotonic() + effective_timeout
        poll = self._ATOMIC_POLL_INITIAL
        while True:
            try:
                fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
            except FileExistsError:
                owner = read_index_lock_owner(self._db_path)
                if owner.is_stale:
                    try:
                        os.remove(lock_path)
                    except OSError:
                        _log.debug(
                            "IndexFileLock: lost the race reclaiming stale "
                            "lock %r -- retrying", lock_path,
                        )
                    continue
                if deadline is not None and time.monotonic() >= deadline:
                    raise IndexLockAcquireError(
                        f"index lock at {lock_path!r} is held by an active "
                        f"owner (pid={owner.pid}, session={owner.session_id!r}) "
                        f"and did not become available within "
                        f"{effective_timeout}s", owner=owner,
                    )
                time.sleep(poll)
                poll = min(poll * 1.5, self._ATOMIC_POLL_MAX)
                continue
            except OSError as exc:
                raise IndexLockAcquireError(
                    f"failed to create lock file {lock_path!r}: {exc}"
                ) from exc
            else:
                self._file_handle = os.fdopen(fd, "r+", encoding="utf-8")
                self._lock_mode = "atomic_create"
                return

    # a52216e2 -- heartbeat() is called from inside rebuild()'s write-lock
    # section on every Phase 2 pass that reaches the Tantivy commit step, so
    # it must be cheap on the common (fast, uncontended) call -- a full lease
    # rewrite on every single call measurably ate into tight rebuild budgets
    # (see the investigation notes on _write_lease_to_handle re: fsync).
    # Skipping a redundant rewrite when the lease is already fresh keeps the
    # steady-state cost near zero while still refreshing promptly whenever a
    # call is slow enough to actually need it.
    _HEARTBEAT_MIN_INTERVAL_SECONDS = 1.0

    def _write_lease(self, heartbeat_at: float) -> None:
        if self._file_handle is None:
            return
        try:
            _write_lease_to_handle(
                self._file_handle,
                pid=os.getpid(), hostname=_current_hostname(),
                session_id=self._session_id,
                started_at=self._started_at or heartbeat_at,
                heartbeat_at=heartbeat_at, lock_mode=self._lock_mode or "unknown",
            )
            self._last_heartbeat_write = heartbeat_at
        except Exception:  # noqa: BLE001 -- diagnostics-only, never fatal
            _log.debug("IndexFileLock: failed to write lease metadata", exc_info=True)

    def heartbeat(self, *, force: bool = False) -> None:
        """Refresh this lease's heartbeat timestamp (best-effort, never
        raises). Call periodically during a long-held lock so a sibling
        process's staleness check (:func:`read_index_lock_owner`) never
        mistakes a slow-but-alive writer for an abandoned one.

        A no-op (skips the write entirely) when the lease was already
        refreshed within :attr:`_HEARTBEAT_MIN_INTERVAL_SECONDS` -- pass
        ``force=True`` to bypass that and always write."""
        if self._file_handle is None:
            return
        now = time.time()
        if not force and self._last_heartbeat_write is not None:
            if now - self._last_heartbeat_write < self._HEARTBEAT_MIN_INTERVAL_SECONDS:
                return
        try:
            self._write_lease(now)
        except Exception:  # noqa: BLE001
            _log.debug("IndexFileLock.heartbeat failed", exc_info=True)

    def release(self) -> None:
        try:
            if self._file_handle is not None:
                if self._lock_mode == "portalocker":
                    try:
                        import portalocker  # noqa: PLC0415
                        portalocker.unlock(self._file_handle)
                    except Exception:  # noqa: BLE001
                        pass
                try:
                    self._file_handle.close()
                except Exception:  # noqa: BLE001
                    pass
                if self._lock_mode == "atomic_create":
                    # a52216e2 -- unlike portalocker's OS-level lock, this is
                    # a plain file: it is NOT released just by closing the
                    # handle, so a clean release must remove it explicitly to
                    # make the path available to the next acquirer. Best-
                    # effort: if this races a concurrent stale-reclaim from
                    # another process, both sides converge on the same "free"
                    # end state regardless of who wins.
                    try:
                        os.remove(self._canonical + ".lock")
                    except OSError:
                        pass
                self._file_handle = None
                self._lock_mode = None
                self._last_heartbeat_write = None
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


def _content_for_fts(
    path: str, fingerprint: FileFingerprint, *, body: str | None = None,
) -> str:
    """The text body for the FTS content column.

    ``body`` is supplied by the single-read analysis fast path when available;
    otherwise this helper preserves the existing on-demand read behavior.
    """
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
    if body is None:
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
    """One row of the persistent outputs_index table.

    edc84500 -- ``content`` (the full extracted CSV/JSON/text body used for
    FTS) is the one HEAVY field here; every other field is a small scalar.
    Once a row has been committed to DuckDB and staged for its Tantivy
    commit, :class:`OutputsFtsIndex` evicts ``content`` back to ``None`` in
    its long-lived ``_row_cache`` (see ``_apply_precomputed``/``_light_row``)
    so the cache never holds one full file body per discovered file for the
    lifetime of the process -- that unbounded growth is what caused a real
    OS-level allocator failure at ~96,000/244,191 files on a real tree. A
    ``None`` content means "not resident in memory, re-read it from the
    persistent ``outputs_index`` table by path if you actually need it"
    (see :meth:`OutputsFtsIndex.get_content`) -- it does NOT mean the row is
    stale, incomplete, or unindexed.
    """

    path: str
    content: str | None
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


def _light_row(row: OutputRow) -> OutputRow:
    """Return a copy of ``row`` with the heavy ``content`` field evicted.

    edc84500 -- this is what :class:`OutputsFtsIndex` stores in its
    long-lived ``_row_cache`` once a row has been (or is about to be)
    persisted: every lightweight field callers actually read off a cached
    row (sha256 for archival dedup, size for the size-prefilter, mtime/kind/
    csv_columns/json_keys/generating_script/is_archival/canonical_path for
    staleness + metadata bookkeeping) survives unchanged; only ``content``
    -- never read from ``_row_cache`` anywhere in this module, see
    ``search``/``resolve_output``/``get_content``, which all query DuckDB
    directly -- is dropped.
    """
    return replace(row, content=None)


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
    # Content extracted from the same read used for hashing/fingerprinting.
    # Kept only for the current bounded rebuild batch; the long-lived cache
    # still evicts it after persistence.
    content: str | None = None


def _analyse_file(
    path: str, hasher: Callable[[str], str | None], *, needs_hash: bool = True,
    stat_signature: tuple[float | None, int | None] | None = None,
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
    captured_mtime, captured_size = (
        stat_signature if stat_signature is not None else (None, None)
    )
    if not needs_hash:
        fp = file_fingerprint(path)
        if stat_signature is not None:
            size, mtime = captured_size, captured_mtime
        else:
            try:
                st = os.stat(path)
                size = st.st_size
                mtime = st.st_mtime
            except OSError:
                size = mtime = None
        return _FileAnalysis(path=path, fingerprint=fp, mtime=mtime,
                              size=size, sha256=None)

    if stat_signature is not None:
        size, mtime = captured_size, captured_mtime
    else:
        try:
            st = os.stat(path)
            size = st.st_size
            mtime = st.st_mtime
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
                fts_content = _content_for_fts(path, fp)
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
                fts_content = _content_for_fts(path, fp, body=text)
            return _FileAnalysis(path=path, fingerprint=fp, mtime=mtime,
                                  size=size, sha256=sha, content=fts_content)

    # Fallback: OSError on the single read, or a non-default hasher injected
    # (tests, or a future custom hasher) -- original two-read path, unchanged.
    fp = file_fingerprint(path)
    sha = hasher(path)
    return _FileAnalysis(path=path, fingerprint=fp, mtime=mtime, size=size, sha256=sha)


# ---------------------------------------------------------------------------
# Explicit convergence state (item 6af1518d, requirement 1)
# ---------------------------------------------------------------------------
#
# Real incident behind this: searching from the full root of a large tree vs.
# a narrow subdirectory gave inconsistent results, because the index walk
# over a huge tree is resumable/incremental and a search issued while the
# walk is still in progress can silently return a "zero hits" result
# indistinguishable from a genuine "this file doesn't exist" zero-hits
# result. `partial`/`fts_pending`/`pending_stale_count`/`db_write_error` were
# already tracked internally (see OutputsFtsIndex.rebuild) and surfaced ad
# hoc on search_outputs()'s result dict (81a0b23d, b1789c0d, 1a799e52) -- this
# dataclass is the single, explicit, structured object those fields are
# derived from, with two additions the ad hoc fields didn't cover: a scan
# boundary (how far the walk has gotten) and an expected/indexed count pair
# (progress signal), plus (via `subtree`) a convergence answer SCOPED to a
# sub-path, not just the whole outputs_dir -- see
# OutputsFtsIndex.get_convergence_state.

@dataclass(frozen=True)
class ConvergenceState:
    """A single, explicit snapshot of how converged an index (or a subtree
    of it) is, right now. See :meth:`OutputsFtsIndex.get_convergence_state`.
    """

    outputs_dir: str
    subtree: str | None
    converged: bool
    walk_complete: bool
    scan_boundary: str | None
    pending_count: int
    indexed_count: int
    expected_count: int | None
    last_error: str | None
    fts_pending: bool
    partial: bool
    # a52216e2 -- read-only single-writer lock/lease diagnostics (see
    # IndexLockOwner/read_index_lock_owner): who holds this index's write
    # lock right now (pid/hostname/session_id/started_at/heartbeat_at), and
    # whether that owner looks active or stale. None only for an in-memory
    # (":memory:") index, which has no persistent lock to report on.
    index_lock: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _subtree_scanned_past(boundary: str | None, subtree_norm: str) -> bool:
    """Best-effort heuristic: True when the walk's tracked scan boundary
    (the last path handed back by a drain() call) proves ``subtree_norm``
    can have no more undiscovered files THIS pass.

    Relies on :func:`_walk_safe_output_files`'s documented traversal order:
    deterministic, sorted, depth-first, a directory's own files before its
    subdirectories, subdirectories visited in sorted-name order -- i.e. for
    any path prefix S, every path under S is visited as one contiguous block
    before the walk moves on to whatever sorts immediately after S among its
    siblings. Once the boundary is no longer inside S (doesn't start with
    ``S + "/"`` and isn't S itself) AND sorts after S, the walk has moved
    past S's entire block for this pass -- nothing under S can still be
    queued.

    This is a documented, best-effort heuristic (plain lexicographic path
    comparison), not exact per-directory queue-depth bookkeeping -- exposing
    the walk's internal directory stack size was deliberately left out of
    scope (see _ResumableFileWalk's docstring); this heuristic answers the
    same practical question ("has this subtree's zero-hit result actually
    been confirmed, or is the walk just not there yet") without it.
    """
    if boundary is None:
        return False
    b = _normalize_output_path(boundary) or boundary.replace("\\", "/")
    s = subtree_norm.rstrip("/")
    if b == s or b.startswith(s + "/"):
        return False  # boundary is still inside the subtree -- not done yet
    return b > s


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
# a849e3d5 -- was a flat hardcoded 8 regardless of machine size. The default
# follows physical cores (not logical threads): filesystem-heavy analysis does
# not benefit from one worker per hyperthread. Explicit/env overrides remain.
_HARDCODED_MAX_WORKERS_FALLBACK = 8
_MAX_WORKERS_ENV_VAR = "MERIDIAN_OUTPUTS_MAX_WORKERS"


def _physical_core_count() -> int:
    """Return physical cores when available, falling back safely."""
    try:
        import psutil  # noqa: PLC0415
        physical = psutil.cpu_count(logical=False)
        if physical and physical >= 1:
            return physical
    except (ImportError, OSError, AttributeError):
        pass
    return os.cpu_count() or _HARDCODED_MAX_WORKERS_FALLBACK


def _default_max_workers() -> int:
    """Resolve the default Phase-1 worker cap from the environment.

    Checked fresh on every call (a cheap env lookup) rather than cached at
    import time, so a changed ``MERIDIAN_OUTPUTS_MAX_WORKERS`` takes effect
    on the next :class:`OutputsFtsIndex` construction without a process
    restart. Falls back to physical cores via ``psutil`` (or
    ``os.cpu_count()`` when unavailable), with the historical hardcoded 8 as
    the final fallback. Invalid overrides are logged rather than silently
    ignored.
    """
    _default = _physical_core_count()
    raw = os.environ.get(_MAX_WORKERS_ENV_VAR)
    if raw is None or not raw.strip():
        return _default
    try:
        value = int(raw.strip())
    except ValueError:
        _log.warning(
            "%s=%r is not a valid integer -- falling back to default (%d)",
            _MAX_WORKERS_ENV_VAR, raw, _default,
        )
        return _default
    if value < 1:
        _log.warning(
            "%s=%r must be >= 1 -- falling back to default (%d)",
            _MAX_WORKERS_ENV_VAR, raw, _default,
        )
        return _default
    return value


def _resolve_max_workers(explicit: int | None) -> int:
    """Precedence: explicit constructor arg > env var > os.cpu_count() default.

    Mirrors the precedence rule already used elsewhere in this codebase for
    other explicit-arg/env-var/default triples (e.g. project_id resolution).
    """
    if explicit is not None:
        if explicit >= 1:
            return explicit
        _default = _physical_core_count()
        _log.warning(
            "OutputsFtsIndex: max_workers=%r must be >= 1 -- falling back "
            "to default (%d)", explicit, _default,
        )
        return _default
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
    # Adaptive defaults are deliberately bounded: 4k is the safety floor,
    # while 32k was the fastest tested setting in the real SUT benchmark.
    # 64k is reserved for machines with ample headroom and is still bounded
    # to avoid turning a large root into one uninterruptible commit burst.
    _ADAPTIVE_MIN_BATCH = 4_096
    _ADAPTIVE_MAX_BATCH = 65_536
    _ADAPTIVE_HEALTHY_AVAILABLE_BYTES = 4 * 1024**3
    _ADAPTIVE_LOW_AVAILABLE_BYTES = 2 * 1024**3
    _ADAPTIVE_MAX_FTS_SECONDS = 8.0
    _ADAPTIVE_MAX_WRITE_SECONDS = 24.0
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

    # 1bce8c41 -- DB write-chunk size (INSERT/DELETE batching against
    # DuckDB, see `_WRITE_CHUNK` in rebuild()), decoupled from
    # `_ResumableFileWalk._MAX_BATCH` (the walk's own per-drain() count cap).
    # Before this, both concerns shared one constant/knob: bumping the
    # walk's default towards "effectively unbounded" (so a generous
    # `max_seconds` budget isn't wasted stopping at an arbitrary file count)
    # would ALSO have made every DB write one giant, unchunked SQL statement
    # by default -- a new unbounded-parameter-count/memory-spike risk
    # replacing the one just fixed. This keeps the write-chunk default at
    # the same tuned value (2000) the shared knob used before 1bce8c41,
    # independent of whatever the walk's own default becomes.
    _WRITE_CHUNK_DEFAULT = 2000
    _WRITE_CHUNK_ENV_VAR = "MERIDIAN_OUTPUTS_WRITE_CHUNK"

    @classmethod
    def _resolve_write_chunk(cls, explicit: int | None) -> int:
        """Precedence: explicit constructor arg > env var > class default.

        Mirrors :meth:`_ResumableFileWalk._resolve_max_batch`'s precedence
        pattern exactly, but resolves an entirely independent value/env var
        (see the ``_WRITE_CHUNK_DEFAULT`` comment above for why the two must
        not share a knob).
        """
        if explicit is not None:
            if explicit >= 1:
                return explicit
            _log.warning(
                "OutputsFtsIndex: write_chunk=%r must be >= 1 -- falling "
                "back to default (%d)", explicit, cls._WRITE_CHUNK_DEFAULT,
            )
            return cls._WRITE_CHUNK_DEFAULT
        raw = os.environ.get(cls._WRITE_CHUNK_ENV_VAR)
        if raw is None or not raw.strip():
            return cls._WRITE_CHUNK_DEFAULT
        try:
            value = int(raw.strip())
        except ValueError:
            _log.warning(
                "%s=%r is not a valid integer -- falling back to default (%d)",
                cls._WRITE_CHUNK_ENV_VAR, raw, cls._WRITE_CHUNK_DEFAULT,
            )
            return cls._WRITE_CHUNK_DEFAULT
        if value < 1:
            _log.warning(
                "%s=%r must be >= 1 -- falling back to default (%d)",
                cls._WRITE_CHUNK_ENV_VAR, raw, cls._WRITE_CHUNK_DEFAULT,
            )
            return cls._WRITE_CHUNK_DEFAULT
        return value

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
        max_batch: int | None = None,
        write_chunk: int | None = None,
        session_id: str | None = None,
    ) -> None:
        self.outputs_dir = outputs_dir
        self._db_path = db_path
        self._hasher = hasher
        # a52216e2 -- optional caller-supplied identity (e.g. a Meridian
        # session_id) attributed to this instance's write-lock lease, purely
        # for diagnostics (see lock_diagnostics()/read_index_lock_owner()).
        self.session_id = session_id
        # acac2599 -- Phase-1 ThreadPoolExecutor worker cap: explicit param >
        # MERIDIAN_OUTPUTS_MAX_WORKERS env var > hardcoded default (8, the
        # previous unconfigurable behaviour).
        self._max_workers = _resolve_max_workers(max_workers)
        # c73c0dd7 -- Tantivy writer heap_size: explicit param (bytes) >
        # MERIDIAN_OUTPUTS_TANTIVY_HEAP_MB env var > 512MB default (up from
        # tantivy's own undersized default, measured ~3x faster commits).
        self._tantivy_heap_bytes = _resolve_tantivy_heap_bytes(tantivy_heap_bytes)
        # 3535b9ad -- walk batch cap: explicit param > MERIDIAN_OUTPUTS_MAX_BATCH
        # env var > class default (1bce8c41: an effectively-unbounded default,
        # time-primary -- see _ResumableFileWalk._MAX_BATCH). This value feeds
        # ONLY the walk's own per-drain() cap and the walk-vs-analysis
        # throttle below; it no longer feeds the DB write-chunk size.
        self._max_batch = _ResumableFileWalk._resolve_max_batch(max_batch)
        self._max_batch_overridden = (
            max_batch is not None
            or bool(os.environ.get(_ResumableFileWalk._MAX_BATCH_ENV_VAR, "").strip())
        )
        self._adaptive_batch = self._initial_adaptive_batch()
        # 1bce8c41 -- DB write-chunk size: explicit param > MERIDIAN_OUTPUTS_
        # WRITE_CHUNK env var > 2000 default. Deliberately resolved
        # independently of `self._max_batch` above -- see `_WRITE_CHUNK_DEFAULT`.
        self._write_chunk = self._resolve_write_chunk(write_chunk)
        # fd4dd661 -- user-configurable gitignore-style exclude patterns:
        # explicit param > MERIDIAN_OUTPUTS_EXCLUDE_PATTERNS env var > empty
        # (unchanged default behaviour -- only secret-file exclusion applies).
        self._exclude_patterns: tuple[str, ...] = (
            tuple(exclude_patterns) if exclude_patterns is not None
            else _default_exclude_patterns()
        )
        self._write_lock = IndexFileLock(db_path, session_id=session_id)
        self._read_lock = threading.RLock()  # in-process query serialisation
        # a52216e2 -- set when the write lock could not be acquired this
        # call (IndexLockAcquireError). Reset at the top of every rebuild(),
        # mirroring last_db_write_error's contract.
        self.last_lock_error: str | None = None
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
        # Planner diagnostics: phase timings and discovery coverage from the
        # most recent rebuild.  These are intentionally lightweight and make
        # large-root bottlenecks measurable instead of inferred from a single
        # wall-clock duration.
        self.last_rebuild_metrics: dict[str, Any] = {}
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
        # ------------------------------------------------------------------
        # Explicit convergence state (item 6af1518d) -- see ConvergenceState
        # and get_convergence_state() below.
        # ------------------------------------------------------------------
        # Last path handed back by the resumable walk's drain() this pass
        # (None once a pass completes, or before the first drain of a fresh
        # pass). Used both as a raw progress signal and, via
        # _subtree_scanned_past(), to answer subtree-scoped convergence
        # queries without a second walk.
        self._scan_boundary: str | None = None
        # Most recent directory the walk could not list (permission denied,
        # removed mid-walk, etc.), if any -- see _walk_safe_output_files's
        # on_error hook. Distinct from last_db_write_error (a PERSISTENCE
        # failure); this is a DISCOVERY failure -- the walk best-effort
        # continues past it, but a subtree under an unreadable directory can
        # never converge, so this must be surfaced rather than silently
        # swallowed at DEBUG level.
        self._last_walk_error: str | None = None
        # Best current estimate of the total file count under outputs_dir --
        # set from the authoritative len(all_paths) each time a walk pass
        # completes. None until the very first pass has ever completed.
        self._expected_count: int | None = None
        # Every path ever registered via register_priority_path -- purely
        # observational (get_convergence_state doesn't currently read this),
        # kept so callers/tests can confirm a specific path really was
        # fast-pathed rather than organically discovered by the ambient walk.
        self._priority_registered: set[str] = set()
        # Set once seed_from_ancestor() has been attempted (successfully or
        # not) for this instance, so get_subtree_index() only ever tries the
        # ancestor-seeding lookup once per subtree index, not on every call.
        self._seeded_from_ancestor = False
        # ------------------------------------------------------------------
        # Durable walk/convergence state (item durability follow-up to
        # 6af1518d) -- everything above this point (_walk_state,
        # _pending_stale, _scan_boundary, _expected_count, _last_walk_error)
        # only ever lived in this Python object's memory: a process restart
        # mid-walk lost the backlog and scan progress outright, AND
        # get_convergence_state() -- which reads none of this from disk --
        # would report a freshly-restarted, never-yet-rebuilt instance as
        # `converged=True` (walk_state is None, pending_stale is empty)
        # even though the previous process's walk never finished a pass.
        # That is exactly the "silently claim completion it hasn't
        # verified" failure this fixes. _walk_pass_confirmed_complete is
        # the durable proxy for "_walk_state is not None": kept in
        # lockstep with the walk_complete local computed in rebuild()'s
        # Phase 0 every call, persisted to outputs_index_meta at the end
        # of rebuild() (see _persist_walk_state_locked), and rehydrated in
        # _connect() (see _rehydrate_walk_state_from_disk) so a fresh
        # instance -- even one that answers get_convergence_state() before
        # ever calling rebuild() itself -- reflects the last CONFIRMED
        # on-disk state rather than an optimistic empty default. Defaults
        # to True (matches the pre-existing "nothing recorded yet" ==
        # "nothing to report as incomplete" contract for a genuinely
        # brand-new index -- see TestConvergenceState::
        # test_no_walk_in_progress_means_any_subtree_converged).
        self._walk_pass_confirmed_complete = True
        # Monotonic counter, incremented each time rebuild() starts a BRAND
        # NEW walk pass (self._walk_state was None). Purely a durable
        # audit/diagnostic trail -- distinguishes "this pending_stale/
        # scan_boundary belongs to the walk pass that was running when the
        # process died" from an arbitrarily older one; nothing in the
        # convergence-state contract branches on its value.
        self._walk_epoch = 0

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
            # Durability follow-up to 6af1518d -- restore walk epoch/
            # cursor/pending-backlog/expected-count/last-error from any
            # prior process's persisted state (see
            # _persist_walk_state_locked). Deliberately NOT gated on
            # needs_full_rehash: this metadata (stat-signature backlog,
            # scan boundary, error string) is independent of the hash
            # algorithm the row content was fingerprinted with, so a
            # pending hash-algo upgrade has no bearing on whether it's
            # safe to restore.
            try:
                self._rehydrate_walk_state_from_disk()
            except Exception:  # noqa: BLE001
                _log.debug(
                    "OutputsFtsIndex._connect: walk-state rehydration failed",
                    exc_info=True,
                )
        return self._con

    def _rehydrate_cache_from_disk(self) -> None:
        """Populate ``_manifest``/``_row_cache`` from any pre-existing rows in
        the on-disk ``outputs_index`` table, so a fresh process resumes prior
        progress instead of treating every file as stale again. No-op (and
        cheap) when the table doesn't exist yet or is empty.

        edc84500 -- deliberately does NOT select the ``content`` column: the
        rows rehydrated here are, by definition, already fully persisted (we
        just read them back from disk), so there is nothing to re-commit and
        no reason to ever materialise their (potentially huge, per-file)
        text bodies into memory just to immediately evict them again. On a
        tree with hundreds of thousands of already-indexed rows this is the
        difference between a cheap metadata-only rehydration and briefly
        holding the entire tree's extracted content in memory on every
        process restart -- the same class of unbounded growth this item
        fixes for the walk/write path, just at connect() time instead of
        during rebuild().
        """
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
                "SELECT path, mtime, sha256, size, "
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
                content=None,  # edc84500 -- never resident; re-read on demand.
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

    # Keys this instance owns inside the shared, generic ``outputs_index_meta``
    # key/value table (see _ensure_schema's docstring: "kept generic so
    # future schema-version markers don't need a new table each time" --
    # this reuses that existing local DB connection/schema rather than
    # inventing a second on-disk store for walk/convergence state).
    _WALK_STATE_META_KEYS = (
        "walk_epoch",
        "walk_pass_complete",
        "walk_scan_boundary",
        "walk_expected_count",
        "walk_last_error",
        "walk_pending_stale_json",
    )

    def _persist_walk_state_locked(self, con: Any) -> None:
        """Durably record walk epoch/cursor(scan boundary)/pending backlog/
        expected count/last error into ``outputs_index_meta`` so a process
        restart can rehydrate them (:meth:`_rehydrate_walk_state_from_disk`)
        instead of silently reporting false convergence or losing
        confirmed-stale files that were never actually persisted.

        Must be called with ``self._write_lock`` already held (mirrors
        :meth:`_add_annotation_locked`'s naming convention) -- called from
        the tail of :meth:`rebuild`, after Phase 2's own write (successful
        or not; a write failure is exactly the case where the caller most
        needs the retry backlog to survive a restart). Best-effort: a
        persistence failure here must never break rebuild()'s own contract,
        so callers wrap this in their own try/except.

        <code-review fix, sprint 6b5ecdc5> -- the DELETE + 6 INSERTs below
        are wrapped in ONE explicit DuckDB transaction (BEGIN/COMMIT, with
        ROLLBACK on any failure) instead of running as 7 separate
        auto-committed statements. Previously a hard kill (crash, OOM,
        power loss) between any two of those statements left the DB
        holding a MIX of old and new keys -- e.g. a freshly-written
        ``walk_epoch`` alongside a stale ``walk_pass_complete``/
        ``walk_pending_stale_json`` that the DELETE had already removed
        but the matching INSERT never reached. On rehydration those
        missing keys silently fell back to the optimistic constructor
        defaults (``_walk_pass_confirmed_complete=True``,
        ``_pending_stale={}``), reintroducing -- via a different
        mechanism -- exactly the "silently claims completion it hasn't
        verified" bug this whole feature exists to close. No other
        multi-statement atomic pattern exists elsewhere in this file to
        match (the other delete-then-insert helpers here,
        e.g. :meth:`_write_hash_algo_version`, are single-key upserts,
        not a multi-key batch), so this introduces DuckDB's standard
        explicit-transaction SQL rather than inventing a new mechanism.
        With this, any failure -- including the process dying mid-batch,
        which never reaches COMMIT at all -- leaves the durable state
        exactly as it was BEFORE this call started: either the complete
        old set of keys, or (a genuinely first-ever persist) nothing.
        """
        self._ensure_schema(con)
        pending_json = json.dumps(
            {p: list(sig) for p, sig in self._pending_stale.items()}
        )
        values: dict[str, str | None] = {
            "walk_epoch": str(self._walk_epoch),
            "walk_pass_complete": "1" if self._walk_pass_confirmed_complete else "0",
            "walk_scan_boundary": self._scan_boundary,
            "walk_expected_count": (
                str(self._expected_count) if self._expected_count is not None
                else None
            ),
            "walk_last_error": self._last_walk_error,
            "walk_pending_stale_json": pending_json,
        }
        placeholders = ",".join("?" for _ in self._WALK_STATE_META_KEYS)
        con.execute("BEGIN TRANSACTION")
        try:
            con.execute(
                f"DELETE FROM outputs_index_meta WHERE key IN ({placeholders})",
                list(self._WALK_STATE_META_KEYS),
            )
            for key, value in values.items():
                con.execute(
                    "INSERT INTO outputs_index_meta (key, value) VALUES (?, ?)",
                    [key, value],
                )
        except BaseException:
            try:
                con.execute("ROLLBACK")
            except Exception:  # noqa: BLE001
                _log.debug(
                    "OutputsFtsIndex._persist_walk_state_locked: rollback "
                    "after a failed write also failed -- connection may be "
                    "left in an unusable transaction state", exc_info=True,
                )
            raise
        else:
            con.execute("COMMIT")

    def _rehydrate_walk_state_from_disk(self) -> None:
        """Restore walk epoch/cursor(scan boundary)/pending backlog/expected
        count/last error from a prior process's persisted state (see
        :meth:`_persist_walk_state_locked`), so a freshly-constructed
        instance -- even one whose very first call is a read-only
        :meth:`get_convergence_state` -- reflects the last CONFIRMED on-disk
        state rather than the optimistic "nothing recorded, must be fine"
        default every field otherwise starts from.

        No-op (fields keep their constructor defaults) when
        ``outputs_index_meta`` doesn't exist yet, holds none of these keys
        (a brand-new DB, or one written before this durability fix shipped),
        or can't be read -- same degrade-gracefully contract as
        :meth:`_rehydrate_cache_from_disk`.

        <code-review fix, sprint 6b5ecdc5> -- MERGES with, rather than
        overwrites, whatever this instance has already accumulated
        in-memory. This method only ever fires on this instance's FIRST
        :meth:`_connect` call -- but in the REAL production call order
        (``search_outputs()`` -> ``rebuild()`` directly, with no
        preceding ``get_convergence_state()``/``_connect()`` call), that
        first :meth:`_connect` doesn't happen until MID-Phase-2 of the
        first post-restart :meth:`rebuild` call -- i.e. AFTER Phase 0/1
        of that SAME call already mutated ``self._pending_stale`` (newly
        discovered stale paths) and unconditionally recomputed
        ``self._walk_pass_confirmed_complete`` from THIS call's own,
        just-observed walk state. A hard overwrite here used to silently
        wipe those same-call discoveries and replace them with whatever
        was persisted before the restart -- a file whose write failed on
        THIS call would vanish from the retry backlog forever instead of
        surviving for the next call (the false-convergence bug this
        feature exists to close, reintroduced via clobbered rehydration
        rather than missing persistence). Each field below is merged
        according to which of the two sources (this call's own, possibly
        absent, in-flight state vs. the persisted, possibly stale,
        cross-restart state) is more trustworthy for that field:

        * ``_pending_stale`` -- UNION. A persisted entry for a path this
          call's own Phase 0/1 hasn't rediscovered yet must still survive
          for retry; a path this call DID just re-stat is fresher and
          wins over any stale persisted signature for the same path.
        * ``_walk_pass_confirmed_complete`` -- logical AND (never widens
          False -> True, only ever narrows True -> False). Phase 0
          unconditionally recomputes this EVERY call, so by the time this
          method can run mid-rebuild(), the in-memory value already IS
          this call's own, fully current answer -- rehydration must never
          let a stale, more optimistic persisted value override a fresh,
          less optimistic one (that is precisely the danger case). When
          Phase 0 hasn't run yet this instance (the constructor default,
          True, is still in place), AND correctly reduces to "just adopt
          the persisted value", reproducing the pre-merge behaviour those
          call orders already depend on.
        * ``_scan_boundary`` / ``_last_walk_error`` / ``_expected_count``
          -- fill only if this call hasn't already produced a value
          (still ``None``); a value Phase 0/1 already set this call is by
          definition more current than anything persisted from before.
        * ``_walk_epoch`` -- take the max of the two: a purely monotonic,
          purely diagnostic counter (nothing in the convergence contract
          branches on it), so "genuinely newer" is simply "numerically
          larger".

        ``rows`` being non-empty but missing SPECIFIC keys (as opposed to
        holding none of them) means a prior process persisted SOME of the
        6 keys but not all -- only possible from a DB written by a
        pre-atomicity-fix version of this code (:meth:`_persist_walk_state_locked`
        now makes the 6-key batch atomic, so this code can no longer
        produce that shape itself going forward). A missing
        ``walk_pass_complete`` key in that situation is treated as
        persisted-False (conservative), not as "nothing to merge" --
        see the ``partial_persist`` handling below.
        """
        con = self._con
        if con is None:
            return
        try:
            placeholders = ",".join("?" for _ in self._WALK_STATE_META_KEYS)
            rows = con.execute(
                "SELECT key, value FROM outputs_index_meta "
                f"WHERE key IN ({placeholders})",
                list(self._WALK_STATE_META_KEYS),
            ).fetchall()
        except Exception:  # noqa: BLE001
            _log.debug(
                "OutputsFtsIndex._rehydrate_walk_state_from_disk: read "
                "failed", exc_info=True,
            )
            return
        if not rows:
            return
        values = {key: value for key, value in rows}
        # <code-review fix, sprint 6b5ecdc5> -- some, but not all, of the 6
        # keys were found. With the atomic persist above this can only
        # happen from a DB a pre-fix version of this code left in a
        # partially-written state -- never from this version's own writes.
        partial_persist = len(values) < len(self._WALK_STATE_META_KEYS)
        if partial_persist:
            _log.warning(
                "OutputsFtsIndex._rehydrate_walk_state_from_disk: found "
                "%d/%d walk-state keys in outputs_index_meta for %r -- "
                "this looks like a persist from before the atomic-write "
                "fix (or on-disk corruption) was interrupted partway "
                "through. Treating the walk pass as UNCONFIRMED rather "
                "than trusting the optimistic default.",
                len(values), len(self._WALK_STATE_META_KEYS), self._db_path,
            )
        # walk_epoch -- purely diagnostic monotonic counter; "genuinely
        # newer" reduces to "numerically larger" (see docstring).
        epoch_raw = values.get("walk_epoch")
        if epoch_raw is not None:
            try:
                self._walk_epoch = max(self._walk_epoch, int(epoch_raw))
            except (TypeError, ValueError):
                _log.debug(
                    "OutputsFtsIndex._rehydrate_walk_state_from_disk: "
                    "invalid walk_epoch %r", epoch_raw,
                )
        # walk_pass_confirmed_complete -- logical AND merge (see
        # docstring): never lets a persisted value turn a call-computed
        # False into True; correctly adopts the persisted value outright
        # when this call hasn't computed anything yet (in-memory still at
        # the constructor's True default).
        complete_raw = values.get("walk_pass_complete")
        if complete_raw is not None:
            persisted_complete = complete_raw == "1"
        else:
            # Missing specifically because rows is non-empty (something
            # WAS persisted) yet this particular key wasn't -- see
            # partial_persist above. Treat conservatively as "persisted
            # state says NOT confirmed complete" rather than "nothing to
            # merge", so this can only ever narrow towards False, same as
            # every other partial-persist case.
            persisted_complete = False
        self._walk_pass_confirmed_complete = (
            self._walk_pass_confirmed_complete and persisted_complete
        )
        # scan_boundary / last_walk_error / expected_count -- fill only if
        # this call hasn't already produced a value (see docstring).
        if self._scan_boundary is None:
            self._scan_boundary = values.get("walk_scan_boundary")
        if self._last_walk_error is None:
            self._last_walk_error = values.get("walk_last_error")
        if self._expected_count is None:
            expected_raw = values.get("walk_expected_count")
            if expected_raw is not None:
                try:
                    self._expected_count = int(expected_raw)
                except (TypeError, ValueError):
                    self._expected_count = None
        # pending_stale -- UNION (see docstring): persisted entries fill in
        # paths this call's own walk hasn't rediscovered yet; this call's
        # own (fresher) entries win for any path present in both.
        pending_json = values.get("walk_pending_stale_json")
        if pending_json:
            try:
                raw = json.loads(pending_json)
                persisted_pending: dict[str, tuple[float | None, int | None]] = {
                    p: (sig[0], sig[1]) for p, sig in raw.items()
                }
                merged_pending = dict(persisted_pending)
                merged_pending.update(self._pending_stale)
                self._pending_stale = merged_pending
            except Exception:  # noqa: BLE001
                _log.debug(
                    "OutputsFtsIndex._rehydrate_walk_state_from_disk: "
                    "failed to parse pending_stale_json", exc_info=True,
                )
        _log.debug(
            "OutputsFtsIndex._rehydrate_walk_state_from_disk: merged walk "
            "state (epoch=%d, pass_complete=%s, pending=%d) from disk",
            self._walk_epoch, self._walk_pass_confirmed_complete,
            len(self._pending_stale),
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

    def _initial_adaptive_batch(self) -> int:
        """Choose a conservative batch from current process memory.

        Explicit constructor/env values remain authoritative.  The optional
        psutil dependency is only consulted for the adaptive default; when it
        is unavailable we use the proven 4k floor rather than guessing.
        """
        if self._max_batch_overridden:
            return self._max_batch
        try:
            import psutil  # noqa: PLC0415
            vm = psutil.virtual_memory()
            available = int(vm.available) - self._tantivy_heap_bytes
        except (ImportError, OSError, AttributeError):
            return self._ADAPTIVE_MIN_BATCH
        if available < self._ADAPTIVE_LOW_AVAILABLE_BYTES:
            return self._ADAPTIVE_MIN_BATCH
        if available < self._ADAPTIVE_HEALTHY_AVAILABLE_BYTES:
            return self._ADAPTIVE_MIN_BATCH * 2
        # Even with ample memory, begin at 32k.  A cold 64k commit measured
        # 63.9s on the real SUT tree, so 64k is only earned after a healthy
        # 32k commit rather than paid up front on every cold index.
        return self._ADAPTIVE_MAX_BATCH // 2

    def _adaptive_batch_limit(self) -> int:
        """Adjust the default batch using memory and prior commit pressure."""
        if self._max_batch_overridden:
            return self._max_batch
        target = self._adaptive_batch
        metrics = self.last_rebuild_metrics
        if (
            float(metrics.get("fts_seconds", 0) or 0) > self._ADAPTIVE_MAX_FTS_SECONDS
            or float(metrics.get("write_seconds", 0) or 0) > self._ADAPTIVE_MAX_WRITE_SECONDS
        ):
            target = max(self._ADAPTIVE_MIN_BATCH, target // 2)
        elif metrics and (
            float(metrics.get("fts_seconds", 0) or 0) < 3.0
            and float(metrics.get("write_seconds", 0) or 0) < 8.0
        ):
            target = min(self._ADAPTIVE_MAX_BATCH, target * 2)
        self._adaptive_batch = target
        return target

    def _record_walk_error(self, dir_path: str, exc: OSError) -> None:
        """``on_error`` callback for :class:`_ResumableFileWalk` (item
        6af1518d): records the most recent directory the walk could not
        list, so :meth:`get_convergence_state` can surface it as
        ``last_error`` instead of it being silently swallowed at DEBUG
        level. Best-effort observational only -- never raises, never
        affects the walk itself continuing past the failed directory.
        """
        self._last_walk_error = f"could not list directory {dir_path!r}: {exc}"
        _log.warning(
            "OutputsFtsIndex: walk could not list directory %r: %s",
            dir_path, exc,
        )

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
        rebuild_started = time.monotonic()
        self.last_rebuild_metrics = {}
        # 1a799e52 -- reset per-call; set below if Phase 2's DB write fails.
        self.last_db_write_error = None
        # a52216e2 -- reset per-call; set below if the write lock itself
        # could not be acquired this call.
        self.last_lock_error = None
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
        walk_started = time.monotonic()
        # b85394bd -- discovery capacity and adaptive ANALYSIS capacity are
        # deliberately two different numbers now (previously both were
        # `self._adaptive_batch_limit()`, conflating "how fast can the walk
        # enumerate paths" with "how much can Phase 1/2 afford to hash/write
        # this call", and reintroducing 1bce8c41's count-primary-walk
        # regression -- see the _MAX_BATCH docstring above):
        #   walk_batch      -- discovery capacity: the walk's own per-drain()
        #                       cap (`self._max_batch`, decoupled, explicit
        #                       param/env-overridable, effectively unbounded
        #                       by default so the walk stays time-primary).
        #   analysis_limit  -- adaptive analysis capacity: memory- and
        #                       commit-pressure-aware, used ONLY to (a) gate
        #                       the discovery throttle below and (b) bound
        #                       how much of the pending-stale backlog Phase 1
        #                       takes on THIS call (see "bounded analysis
        #                       intake" further down).
        walk_batch = self._max_batch
        analysis_limit = self._adaptive_batch_limit()
        if os.path.isdir(self.outputs_dir):
            if self._walk_state is None:
                # A brand-new pass is starting (either the very first one,
                # or the one after a prior pass completed) -- bump the
                # durable epoch counter (see _persist_walk_state_locked)
                # BEFORE any paths are drained, so even a crash during this
                # very first drain() records the correct epoch on restart.
                self._walk_epoch += 1
                self._walk_state = _ResumableFileWalk(
                    self.outputs_dir, exclude_patterns=self._exclude_patterns,
                    max_batch=walk_batch, on_error=self._record_walk_error,
                )
                self._walk_accumulated = []
            else:
                # The walk persists across calls; discovery capacity is
                # static (own knob), so this only re-applies it in case a
                # constructor/env override changed between instances -- it
                # is NOT re-derived from adaptive analysis pressure.
                self._walk_state.max_batch = walk_batch
            if len(self._pending_stale) < analysis_limit:
                newly_seen = self._walk_state.drain(phase1_deadline)
                self._walk_accumulated.extend(newly_seen)
                if newly_seen:
                    # 6af1518d -- convergence state's scan boundary: how far
                    # the walk has gotten this pass, in its own deterministic
                    # sorted-DFS order (see _subtree_scanned_past).
                    self._scan_boundary = newly_seen[-1]
            walk_complete = self._walk_state.exhausted
        else:
            self._walk_state = None
            self._walk_accumulated = []
            self._pending_stale = {}
            walk_complete = True

        # Durable proxy for "_walk_state is not None" -- see the field's
        # docstring in __init__. Kept in lockstep with walk_complete on
        # every call (in-memory here; persisted at the tail of this method)
        # so get_convergence_state() reports the correct walk_complete/
        # converged answer even for a freshly-restarted process that polls
        # it before ever calling rebuild() itself.
        self._walk_pass_confirmed_complete = walk_complete

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
            # 6af1518d -- a full pass just completed: this is the new
            # authoritative "expected" total (progress-signal denominator for
            # ConvergenceState), and the scan boundary resets -- the NEXT
            # rebuild() call starts a fresh pass from the top.
            self._expected_count = len(all_paths)
            self._scan_boundary = None
        else:
            # Walk pass still in progress -- we only know about the files
            # revisited so far THIS pass, not the full tree. Optimistically
            # keep every previously-indexed path in the picture (assume still
            # present until the walk actually gets around to confirming
            # otherwise) so the reported row count and search index never
            # regress mid-pass. Removed-file detection is deferred until the
            # pass completes.
            # During an incomplete pass, preserve the cache's insertion order
            # and append only newly discovered paths. Sorting and rebuilding a
            # second set here is O(n log n) work on every continuation call,
            # even though no stale/removal decision can be made until the walk
            # completes. Deterministic sorting remains on the completed pass.
            all_paths = list(self._row_cache)
            known_paths = set(all_paths)
            all_paths.extend(p for p in self._walk_accumulated if p not in known_paths)
            removed_paths = set()

        path_set = set(all_paths)
        walk_elapsed = time.monotonic() - walk_started
        self.last_rebuild_metrics.update({
            "walk_seconds": round(walk_elapsed, 6),
            "discovered_this_call": len(newly_seen),
            "discovered_total": len(all_paths),
            "walk_complete": bool(walk_complete),
            "pending_stale_count": len(self._pending_stale),
            # b85394bd -- planner/diagnostic surface for the discovery-vs-
            # analysis split: the resolved caps THIS call actually used, and
            # whether the analysis figure came from an explicit override or
            # the memory-adaptive controller, so a caller can tell a slow
            # convergence apart from a deliberately-tuned one.
            "walk_batch_limit": walk_batch,
            "analysis_batch_limit": analysis_limit,
            "analysis_batch_source": (
                "override" if self._max_batch_overridden else "adaptive"
            ),
        })

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

        # b85394bd -- BOUNDED ANALYSIS INTAKE: `self._pending_stale` may hold
        # far more than one call should take on at once (discovery capacity
        # is now effectively unbounded per call -- see `walk_batch` above --
        # so a single fast drain() on a huge tree can add tens of thousands
        # of paths to the backlog in one shot). Only the first
        # `analysis_limit` entries (stable insertion order -- the walk's own
        # deterministic sorted-DFS discovery order, see `_scan_boundary`)
        # are taken into `stale`/`stale_sigs` and therefore into Phase 1's
        # ThreadPoolExecutor submission and Phase 2's write below; anything
        # beyond that stays in `self._pending_stale` untouched (never
        # submitted, never popped) for a later call to pick up -- this is
        # what actually prevents "the entire pending list is submitted to
        # ThreadPoolExecutor" regardless of backlog size. Deterministic:
        # the same backlog + the same analysis_limit always yields the same
        # bounded slice.
        stale_all: list[str] = list(self._pending_stale)
        stale: list[str] = stale_all[:analysis_limit]
        stale_sigs: dict[str, tuple[float | None, int | None]] = {
            p: self._pending_stale[p] for p in stale
        }
        self.last_rebuild_metrics["analysis_backlog_deferred"] = (
            len(stale_all) - len(stale)
        )

        # Parallel per-file analysis for stale paths (fingerprint + hash + stat).
        # Workers run before the write lock is taken so heavy I/O (e.g. hashing
        # a 5.9 MB file) overlaps across files.  Results are collected into a
        # dict and processed in sorted order to guarantee determinism.
        precomputed: dict[str, _FileAnalysis] = {}
        analysis_started = time.monotonic()
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
                        stat_signature=stale_sigs.get(p),
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

        self.last_rebuild_metrics["analysis_seconds"] = round(
            time.monotonic() - analysis_started, 6,
        )

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
        classify_started = time.monotonic()
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
                # b85394bd -- `path not in stale_set` used to structurally
                # guarantee a row_cache hit here (staleness was defined as
                # "manifest mismatch OR no row_cache entry", covering every
                # currently-stale path). That invariant no longer holds
                # unconditionally: `stale`/`stale_set` is now a BOUNDED
                # analysis-intake slice of `self._pending_stale` (see
                # "BOUNDED ANALYSIS INTAKE" above), so a path can be
                # genuinely stale, deferred past this call's intake cap, AND
                # never previously cached (a brand-new file) all at once --
                # `path not in stale_set` no longer implies a row_cache hit
                # for that path. `.get()` degrades this to the same safe
                # None default 5845cc6d already documents below for a
                # straggler Phase 1 didn't reach in time, rather than a
                # KeyError.
                if path not in stale_set:
                    cached_row = self._row_cache.get(path)
                    if cached_row is not None:
                        return cached_row.sha256
                    return None
                # 5845cc6d — genuinely stale but Phase 1 didn't get to it
                # before its own sub-deadline (or, per b85394bd above, was
                # deferred past this call's bounded analysis intake).  Do
                # NOT fall back to a fresh synchronous self._hasher(path)
                # call here: classify_canonical_archival has no deadline
                # check of its own, so looping through potentially thousands
                # of un-analysed stale files would blow the overall budget
                # just as badly as the original Phase-1-blocks-forever bug
                # this fix targets. None is a safe default ("not confirmed
                # archival this round") -- the file gets a proper hash once
                # its turn in a future Phase 1 comes up.
                return None

            classifications = classify_canonical_archival(
                all_paths, hasher=_cached_hasher,
            )
        self.last_rebuild_metrics["classification_seconds"] = round(
            time.monotonic() - classify_started, 6,
        )

        # ------------------------------------------------------------------
        # Phase 2: targeted write (write_lock held)
        # ------------------------------------------------------------------
        write_started = time.monotonic()
        # a52216e2 -- acquire() is split from the `with` sugar here so a
        # genuine IndexLockAcquireError (real cross-process contention, or an
        # unexpected acquisition failure) degrades this call gracefully
        # instead of propagating out of rebuild() entirely: nothing below has
        # run yet, so no in-memory/on-disk state has been touched this call --
        # the next rebuild() (this process or a sibling) simply retries.
        try:
            self._write_lock.acquire()
        except IndexLockAcquireError as _lock_exc:
            self.last_lock_error = str(_lock_exc)
            self.last_rebuild_partial = True
            _log.warning("OutputsFtsIndex.rebuild: %s", _lock_exc)
            self.last_rebuild_metrics["write_seconds"] = round(
                time.monotonic() - write_started, 6,
            )
            self.last_rebuild_metrics.update({
                "rebuild_seconds": round(time.monotonic() - rebuild_started, 6),
                "rows_returned": len(self._row_cache),
                "rows_changed": 0,
                "rows_deleted": 0,
                "partial": True,
                "fts_pending": bool(self._fts_pending),
            })
            return len(self._row_cache)
        try:
            self._ingest_meridian_notes(all_paths)
            rows, changed, paths_to_delete, new_rows = (
                self._apply_precomputed(
                    all_paths, path_set, removed_paths, stale, stale_sigs,
                    precomputed, classifications, deadline,
                )
            )
            if not walk_complete:
                # 6ba77ada -- the walk itself hasn't finished a full pass yet
                # (it's being resumed across future rebuild() calls), so the
                # index is known-incomplete regardless of whether Phase 1/2
                # themselves hit their own deadlines this call.
                self.last_rebuild_partial = True
            if self.last_rebuild_metrics.get("analysis_backlog_deferred"):
                # b85394bd -- bounded analysis intake deliberately deferred
                # part of the backlog (independent of any deadline breach --
                # `_apply_precomputed` above only sets this for a genuine
                # deadline hit, and resets it to False at its own start, so
                # this must be re-applied AFTER that call returns). Real work
                # remains queued in `self._pending_stale`; report the rebuild
                # as partial so callers (search_outputs()'s ad hoc `partial`/
                # `pending_stale_count` fields, not just the structured
                # `convergence` object which already derives independently
                # from `self._pending_stale`) keep surfacing it -- same
                # "re-invoke, don't conclude not-found" contract as every
                # other partial-rebuild cause.
                self.last_rebuild_partial = True
            # <false-convergence fix, see root-cause note at the pop below> --
            # write_confirmed tracks whether Phase 2's DB write (+ FTS commit)
            # actually succeeds. Starts True (nothing to persist, or the
            # write below succeeds) and is flipped False ONLY inside the
            # `except` block. The pending-stale pop used to run
            # unconditionally right here -- see the note at its new location
            # for why that was a real bug, not just a comment inaccuracy.
            write_confirmed = True
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
                    # 1bce8c41 -- self._write_chunk, NOT self._max_batch: the
                    # walk's own count cap and the DB write-chunk size are
                    # deliberately decoupled (see _WRITE_CHUNK_DEFAULT) so
                    # that self._max_batch's new effectively-unbounded
                    # default (time-primary walk convergence) can never
                    # silently turn this into one giant, unchunked write.
                    _WRITE_CHUNK = self._write_chunk
                    replacement_paths = {r.path for r in new_rows}
                    db_delete_paths = [
                        p for p in paths_to_delete if p not in replacement_paths
                    ]
                    if db_delete_paths:
                        for i in range(0, len(db_delete_paths), _WRITE_CHUNK):
                            chunk = db_delete_paths[i:i + _WRITE_CHUNK]
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
                                    "INSERT OR REPLACE INTO outputs_index "
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
                                    f"INSERT OR REPLACE INTO outputs_index VALUES {row_placeholders}",
                                    flat_params,
                                )
                    # 77443d83 -- stage this call's own delta for the next
                    # Tantivy commit. Accumulates (rather than overwrites)
                    # across deferred calls, so whenever _rebuild_fts() next
                    # actually runs -- here or lazily from search() -- it
                    # commits the full outstanding delta as one small Tantivy
                    # transaction, never a full re-index.
                    replacement_paths = {r.path for r in new_rows}
                    self._pending_tantivy_deletes.update(paths_to_delete)
                    self._pending_tantivy_deletes.update(replacement_paths)
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
                        # a52216e2 -- refresh the lease heartbeat before the
                        # (potentially slow, on a large delta) Tantivy commit
                        # below, so a sibling process's staleness check never
                        # mistakes a genuinely-still-working writer for an
                        # abandoned one just because the DB write above alone
                        # took a while.
                        self._write_lock.heartbeat()
                        fts_started = time.monotonic()
                        self._rebuild_fts(con)
                        self.last_rebuild_metrics["fts_seconds"] = round(
                            time.monotonic() - fts_started, 6,
                        )
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
                    write_confirmed = False
            # <false-convergence ROOT-CAUSE FIX> -- a path is only dropped from
            # the pending-stale backlog once its row is CONFIRMED persisted
            # (write_confirmed True). Before this fix, the pop ran
            # unconditionally right after _apply_precomputed -- BEFORE Phase
            # 2's DB write (the code directly above) even ran, let alone
            # succeeded. _apply_precomputed() had already updated
            # self._row_cache / self._manifest optimistically for every path
            # in new_rows (edc84500's "content evicted, everything else kept"
            # cache), so once a path was popped from _pending_stale here, a
            # DB write failure for that SAME path left it looking exactly as
            # "current" as a genuinely persisted row: Phase 1's own staleness
            # check (`self._manifest.get(p) != sig or p not in
            # self._row_cache`) would find both fields already up to date and
            # never re-flag it as stale. The file would then be silently and
            # PERMANENTLY dropped from the searchable index for the lifetime
            # of the process -- never retried, never surfaced again once this
            # call's own last_db_write_error is reset to None at the top of
            # the NEXT rebuild() call. That is exactly the false-convergence
            # failure mode flagged in this sprint item: the tree can look
            # fully converged (partial=False, pending_stale_count omitted/0)
            # while a real file is missing from search results.
            #
            # The fix: leave failed paths IN _pending_stale (they were never
            # removed to begin with -- this is a pure reordering, not a
            # re-add) so the next rebuild() call resubmits them to Phase 1/2
            # for a genuine retry. last_rebuild_partial / fts_pending are
            # deliberately NOT touched here on a write failure -- db_write_
            # error already carries this call's own signal (1a799e52,
            # unchanged contract, see TestSearchOutputsAPI::
            # test_search_outputs_surfaces_db_write_error_in_result_dict) and
            # this fix's job is only to make the NEXT call actually retry
            # instead of silently losing the file forever.
            if write_confirmed:
                for r in new_rows:
                    self._pending_stale.pop(r.path, None)
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
            # Durability follow-up to 6af1518d -- persist THIS call's final
            # walk epoch/cursor/pending-backlog/expected-count/last-error,
            # regardless of whether Phase 2's row write above succeeded.
            # Unconditional (not gated on `changed`) because a walk pass can
            # legitimately complete -- or a pending-stale backlog can
            # legitimately shrink/grow -- with zero row-level changes this
            # call (e.g. a full pass over an already-converged tree).
            # Best-effort: a persistence failure here must never break
            # rebuild()'s own return contract.
            try:
                walk_state_con = self._connect()
                self._persist_walk_state_locked(walk_state_con)
            except Exception:  # noqa: BLE001
                _log.debug(
                    "OutputsFtsIndex.rebuild: failed to persist walk state",
                    exc_info=True,
                )
            self.last_rebuild_metrics["write_seconds"] = round(
                time.monotonic() - write_started, 6,
            )
            self.last_rebuild_metrics.update({
                "rebuild_seconds": round(time.monotonic() - rebuild_started, 6),
                "rows_returned": len(rows),
                "rows_changed": len(new_rows),
                "rows_deleted": len(paths_to_delete),
                "partial": bool(self.last_rebuild_partial),
                "fts_pending": bool(self._fts_pending),
            })
            return len(rows)
        finally:
            self._write_lock.release()

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

        edc84500 -- ``new_rows`` (and therefore ``all_rows``'s inputs before
        caching) always carry REAL content: either freshly extracted this
        call, or re-read from DuckDB on demand (see the archival-metadata
        refresh loop below). ``self._row_cache`` itself only ever stores the
        light (content-evicted) copy -- see :func:`_light_row` -- so it never
        re-accumulates the unbounded, per-file text bodies that caused a real
        OS-level allocator failure at ~96,000/244,191 files on a real tree.
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
                    analysis = _analyse_file(
                        p, self._hasher, stat_signature=stale_sigs.get(p),
                    )
                except Exception:  # noqa: BLE001
                    _log.debug("_apply_precomputed: fallback _analyse_file failed for %r", p,
                                exc_info=True)
                    continue
            fp = analysis.fingerprint
            mtime = analysis.mtime
            size = analysis.size
            # During a hash-algorithm upgrade the cache is intentionally left
            # empty, even though DuckDB still contains legacy rows; those
            # rows must be replaced rather than treated as brand-new inserts.
            had_existing = p in self._row_cache or self._pending_hash_upgrade
            cls = classifications.get(p)
            row = OutputRow(
                path=p,
                content=(
                    analysis.content
                    if analysis.content is not None
                    else _content_for_fts(p, fp)
                ),
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
            # edc84500 -- `new_rows` keeps the FULL row (real content, just
            # extracted) so the imminent DB write + Tantivy commit below get
            # the real text. `_row_cache` only ever stores the light
            # (content-evicted) copy -- this is the actual eviction point:
            # content is never resident in `_row_cache` for longer than the
            # tail end of the SAME rebuild() call that produced it.
            self._row_cache[p] = _light_row(row)
            self._manifest[p] = stale_sigs.get(p, (mtime, size))
            if had_existing:
                paths_to_delete.append(p)  # replace an existing persisted row
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
                    # edc84500 -- `row` here is a CACHED (light) row, so
                    # `row.content` is normally already None (evicted after a
                    # prior commit). Re-insertion below must never write that
                    # None over the real, already-persisted content -- read
                    # it back from DuckDB first whenever it isn't resident.
                    # (A non-evicted `row.content` -- e.g. this same path was
                    # ALSO stale this call, though `p in stale_set` already
                    # excludes that -- is reused as-is, no extra read.)
                    content = (
                        row.content if row.content is not None
                        else self._fetch_content_from_db(p)
                    )
                    full_row = replace(
                        row, content=content, is_archival=new_is_archival,
                        canonical_path=new_canonical,
                    )
                    # Keep the cached entry itself light -- only the two
                    # metadata fields change; content stays evicted.
                    row.is_archival = new_is_archival
                    row.canonical_path = new_canonical
                    # This row changed -- include it in the targeted update.
                    paths_to_delete.append(p)
                    new_rows.append(full_row)
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

    # ------------------------------------------------------------------
    # Explicit convergence state (item 6af1518d, requirement 1 & 2)
    # ------------------------------------------------------------------

    def get_convergence_state(self, subtree: str | None = None) -> "ConvergenceState":
        """Return an explicit, structured convergence-state snapshot.

        Without ``subtree``, describes convergence of this index's whole
        ``outputs_dir``. With ``subtree`` (any path under ``outputs_dir``),
        additionally answers "has THIS specific sub-path been fully covered
        by the walk so far" -- the exact ambiguity behind the real incident
        motivating this item: searching the full root of a large tree vs. a
        narrow subdirectory gave inconsistent zero-hit results because there
        was no way to tell "the walk hasn't reached this subtree yet" apart
        from "this subtree is genuinely empty". See
        :func:`_subtree_scanned_past` for the scan-boundary heuristic this
        uses, and its documented limits.

        Durability follow-up to 6af1518d: connects (best-effort) before
        reading state, so a process that restarted mid-walk and calls this
        method FIRST -- before ever calling :meth:`rebuild` itself -- still
        answers from the last CONFIRMED on-disk state (see
        :meth:`_rehydrate_walk_state_from_disk`) instead of the empty,
        optimistic-looking defaults a brand-new, never-connected instance
        would otherwise report. Still read-only and safe to poll: once
        connected (a one-time cost per instance), this is a pure in-memory
        read, and never triggers a filesystem walk or any indexing.
        """
        with self._read_lock:
            try:
                self._connect()
            except Exception:  # noqa: BLE001
                _log.debug(
                    "OutputsFtsIndex.get_convergence_state: connect failed "
                    "(duckdb unavailable or db unreadable) -- reporting "
                    "in-memory state only", exc_info=True,
                )
            # _walk_pass_confirmed_complete is the durable proxy for
            # "_walk_state is not None" -- see its docstring in __init__.
            # Checking BOTH covers the in-process case (a live walk this
            # same process started) and the cross-restart case (a prior
            # process's walk that never finished, rehydrated above).
            walk_in_progress = (
                self._walk_state is not None
                or not self._walk_pass_confirmed_complete
            )
            last_error = (
                self.last_db_write_error or self._last_walk_error
                or self.last_lock_error
            )
            if subtree is None:
                pending = len(self._pending_stale)
                converged = (
                    not walk_in_progress and pending == 0
                    and not self._fts_pending and last_error is None
                )
                scope_desc = None
            else:
                sub_norm = _normalize_output_path(subtree) or str(subtree).replace("\\", "/")
                scope_desc = subtree
                subtree_scanned = (
                    not walk_in_progress
                    or _subtree_scanned_past(self._scan_boundary, sub_norm)
                )
                pending = sum(
                    1 for p in self._pending_stale
                    if (
                        (n := _normalize_output_path(p)) == sub_norm
                        or n.startswith(sub_norm + "/")
                    )
                )
                converged = (
                    subtree_scanned and pending == 0
                    and not self._fts_pending and last_error is None
                )
            return ConvergenceState(
                outputs_dir=self.outputs_dir,
                subtree=scope_desc,
                converged=converged,
                walk_complete=not walk_in_progress,
                scan_boundary=self._scan_boundary,
                pending_count=pending,
                indexed_count=len(self._row_cache),
                expected_count=self._expected_count,
                last_error=last_error,
                fts_pending=bool(self._fts_pending),
                partial=bool(self.last_rebuild_partial),
                index_lock=self.lock_diagnostics(),
            )

    def lock_diagnostics(self) -> dict[str, Any] | None:
        """Read-only snapshot of this index's write-lock/lease state
        (a52216e2). Never acquires the write lock itself -- safe to call from
        any read path (search, get_convergence_state) without blocking on, or
        disturbing, a live writer. ``None`` for an in-memory (``:memory:``)
        index, which has no persistent lock to report on.
        """
        if self._db_path == ":memory:":
            return None
        try:
            return read_index_lock_owner(self._db_path).to_dict()
        except Exception:  # noqa: BLE001 -- diagnostics must never break a caller
            _log.debug("OutputsFtsIndex.lock_diagnostics failed", exc_info=True)
            return {"db_path": self._db_path, "held": None, "error": "lock diagnostics unavailable"}

    # ------------------------------------------------------------------
    # Provenance-triggered targeted registration (item 6af1518d,
    # requirement 3)
    # ------------------------------------------------------------------

    def _build_rows_for_paths(
        self,
        paths: list[str],
        stale_sigs: dict[str, tuple[float | None, int | None]],
        precomputed: dict[str, "_FileAnalysis"],
        classifications: dict[str, ArchivalClassification],
    ) -> tuple[list[OutputRow], list[str]]:
        """Build+cache :class:`OutputRow` objects for a small, EXPLICIT path
        set. This is the row-construction slice of :meth:`_apply_precomputed`
        's stale-processing loop, factored out standalone (not shared code)
        so :meth:`index_paths` can reuse the exact same row shape without
        touching ``self.last_rebuild_partial`` or processing
        ``removed_paths``/the archival-metadata-refresh loop -- both are
        root-walk-scoped concerns that a small targeted side-channel write
        must never perturb (a provenance-triggered registration must not be
        able to flip a genuinely-in-progress root rebuild's ``partial`` flag
        back to ``False``).
        """
        new_rows: list[OutputRow] = []
        paths_to_delete: list[str] = []
        for p in sorted(paths):
            analysis = precomputed.get(p)
            if analysis is None:
                continue
            fp = analysis.fingerprint
            had_existing = p in self._row_cache or self._pending_hash_upgrade
            cls = classifications.get(p)
            row = OutputRow(
                path=p,
                content=(
                    analysis.content if analysis.content is not None
                    else _content_for_fts(p, fp)
                ),
                mtime=analysis.mtime,
                sha256=analysis.sha256,
                size=analysis.size,
                generating_script=fp.generating_script,
                kind=fp.kind,
                is_archival=bool(cls and cls.is_archival),
                canonical_path=(cls.canonical_path if cls else None),
                csv_columns=fp.csv_columns,
                json_keys=fp.json_keys,
            )
            self._row_cache[p] = _light_row(row)
            self._manifest[p] = stale_sigs.get(p, (row.mtime, row.size))
            if had_existing:
                paths_to_delete.append(p)
            new_rows.append(row)
        return new_rows, paths_to_delete

    def _write_rows_locked(
        self, con: Any, new_rows: list[OutputRow], paths_to_delete: list[str],
    ) -> None:
        """Persist ``new_rows``/``paths_to_delete`` and stage+commit the
        matching Tantivy delta. Must be called with ``self._write_lock``
        already held. Deliberately a plain per-row ``execute`` loop (not the
        pyarrow bulk-insert path ``rebuild()`` uses) -- this is only ever
        called with a small, explicit path list (a handful of
        provenance-registered files or an ancestor-index seed slice), never
        a whole-tree batch, so the bulk-insert machinery's extra complexity
        isn't warranted here.
        """
        for p in paths_to_delete:
            if p not in {r.path for r in new_rows}:
                con.execute("DELETE FROM outputs_index WHERE path = ?", [p])
        for r in new_rows:
            con.execute(
                "INSERT OR REPLACE INTO outputs_index VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    r.path, r.content, r.mtime, r.sha256, r.size,
                    r.generating_script, r.kind, r.is_archival, r.canonical_path,
                    json.dumps(r.csv_columns) if r.csv_columns else None,
                    json.dumps(r.json_keys) if r.json_keys else None,
                ],
            )
        self._pending_tantivy_deletes.update(paths_to_delete)
        self._pending_tantivy_deletes.update(r.path for r in new_rows)
        for r in new_rows:
            self._pending_tantivy_upserts[r.path] = r
        try:
            self._rebuild_fts(con)
        except Exception:  # noqa: BLE001
            _log.debug("_write_rows_locked: _rebuild_fts failed", exc_info=True)
            # Rows are already persisted in DuckDB; just defer the FTS
            # commit to the next rebuild()/search() call, same contract as
            # rebuild()'s own deadline-deferral path (b1789c0d).
            if not self._fts_built:
                self._fts_pending = True

    def index_paths(self, paths: list[str]) -> dict[str, Any]:
        """Synchronously analyse + persist a small, EXPLICIT set of paths,
        bypassing the ambient resumable walk (Phase 0) and its
        deadline/backlog-throttle machinery entirely.

        Cost is bounded by ``len(paths)``, not by ``outputs_dir``'s total
        size -- safe to call synchronously from a fast, latency-sensitive
        caller (e.g. right after ``record_provenance``) even on a huge,
        still-converging tree. Secret-pattern paths are silently skipped
        (same filter the ambient walk applies). A path that doesn't exist
        yet is queued into ``self._pending_stale`` (stat signature
        ``(None, None)``) so a future call -- once the file actually exists
        -- indexes it without the caller needing to retry explicitly.

        Returns ``{"indexed": N, "queued": N, "paths": [...]}`` -- best
        effort, never raises.
        """
        norm_paths: list[str] = []
        for p in paths:
            if not p:
                continue
            pp = os.path.normpath(p)
            if is_secret_path(pp):
                continue
            norm_paths.append(pp)
        if not norm_paths:
            return {"indexed": 0, "queued": 0, "paths": []}

        stale_sigs: dict[str, tuple[float | None, int | None]] = {}
        existing: list[str] = []
        queued = 0
        for p in norm_paths:
            try:
                st = os.stat(p)
            except OSError:
                with self._write_lock:
                    self._pending_stale.setdefault(p, (None, None))
                queued += 1
                continue
            stale_sigs[p] = (st.st_mtime, st.st_size)
            existing.append(p)

        if not existing:
            return {"indexed": 0, "queued": queued, "paths": []}

        precomputed: dict[str, _FileAnalysis] = {}
        for p in existing:
            try:
                precomputed[p] = _analyse_file(
                    p, self._hasher, needs_hash=True, stat_signature=stale_sigs[p],
                )
            except Exception:  # noqa: BLE001
                _log.debug("index_paths: _analyse_file failed for %r", p, exc_info=True)

        if not precomputed:
            return {"indexed": 0, "queued": queued, "paths": []}

        def _cached_hasher(path: str) -> str | None:
            a = precomputed.get(path)
            return a.sha256 if a else None

        classifications = classify_canonical_archival(
            sorted(precomputed), hasher=_cached_hasher,
        )

        with self._write_lock:
            con = self._connect()
            self._ensure_schema(con)
            new_rows, paths_to_delete = self._build_rows_for_paths(
                list(precomputed), stale_sigs, precomputed, classifications,
            )
            if new_rows:
                try:
                    self._write_rows_locked(con, new_rows, paths_to_delete)
                except Exception as exc:  # noqa: BLE001
                    _log.debug("index_paths: write failed", exc_info=True)
                    self.last_db_write_error = f"{type(exc).__name__}: {exc}"
                    return {"indexed": 0, "queued": queued, "paths": []}
            for p in precomputed:
                self._pending_stale.pop(p, None)

        return {
            "indexed": len(new_rows), "queued": queued,
            "paths": [r.path for r in new_rows],
        }

    def register_priority_path(self, path: str) -> dict[str, Any]:
        """Mark ``path`` as known-important (typically because provenance
        was just recorded for it via ``record_provenance``) so it becomes
        searchable promptly via :meth:`index_paths` instead of waiting for
        the ambient full-root walk to reach it -- item 6af1518d requirement
        3. Thin, intent-named wrapper around :meth:`index_paths` -- kept as
        a separate name so callers/tests can express "this is a
        provenance-triggered registration" even though the underlying
        primitive is generic.
        """
        self._priority_registered.add(os.path.normpath(path))
        return self.index_paths([path])

    # ------------------------------------------------------------------
    # Hierarchical subtree indexing (item 6af1518d, requirement 4)
    # ------------------------------------------------------------------

    def seed_from_ancestor(self, ancestor: "OutputsFtsIndex", subtree_path: str) -> int:
        """Copy the slice of ``ancestor``'s already-indexed rows that fall
        under ``subtree_path`` into THIS (subtree-scoped) index, so this
        index's own resumable walk never has to re-discover/re-hash files
        the ancestor (root, or a nearer parent subtree) already converged
        on -- "reuse a slice of" the parent index, per requirement 4.

        Best-effort and purely additive: never removes or overwrites a row
        this index already has, and never raises -- a seeding failure just
        means this index's own walk will (more slowly, but always
        correctly) rediscover the same files itself.

        Returns the number of rows copied.
        """
        self._seeded_from_ancestor = True
        prefix = os.path.normpath(os.path.abspath(subtree_path))
        try:
            with ancestor._read_lock:
                ancestor_paths = [
                    p for p in ancestor._row_cache
                    if p == prefix or p.startswith(prefix + os.sep)
                ]
            if not ancestor_paths:
                return 0
            with self._write_lock:
                con = self._connect()
                self._ensure_schema(con)
                new_rows: list[OutputRow] = []
                for p in ancestor_paths:
                    if p in self._row_cache:
                        continue  # a local write always wins
                    light = ancestor._row_cache.get(p)
                    if light is None:
                        continue
                    content = ancestor._fetch_content_from_db(p)
                    row = replace(light, content=content)
                    self._row_cache[p] = _light_row(row)
                    self._manifest[p] = ancestor._manifest.get(p, (row.mtime, row.size))
                    new_rows.append(row)
                if new_rows:
                    self._write_rows_locked(con, new_rows, [])
                return len(new_rows)
        except Exception:  # noqa: BLE001
            _log.debug(
                "seed_from_ancestor: failed seeding %r from %r",
                subtree_path, ancestor.outputs_dir, exc_info=True,
            )
            return 0

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

    def _fetch_content_from_db(self, path: str) -> str | None:
        """Read the persisted FTS ``content`` body for the EXACT (already
        stored-form) ``path`` directly from the ``outputs_index`` table.

        edc84500 -- the one and only place ``content`` should ever be read
        back once it has been evicted from ``_row_cache``. ``path`` must
        already match the DB's stored form exactly (no
        :func:`_normalize_output_path` case/slash normalisation) -- callers
        with a raw filesystem path from ``all_paths``/``_row_cache`` keys
        satisfy this directly; :meth:`get_content` (the public, user-facing
        lookup) normalises first via the platform-aware SQL that
        :meth:`resolve_output` also uses.
        """
        try:
            con = self._connect()
            self._ensure_schema(con)
            row = con.execute(
                "SELECT content FROM outputs_index WHERE path = ?", [path],
            ).fetchone()
        except Exception:  # noqa: BLE001
            _log.debug(
                "OutputsFtsIndex._fetch_content_from_db failed for %r",
                path, exc_info=True,
            )
            return None
        return row[0] if row is not None else None

    def get_content(self, file_path: str) -> str | None:
        """Return the indexed FTS content body for ``file_path``, or
        ``None`` if the path isn't indexed.

        edc84500 -- ``_row_cache`` evicts a row's ``content`` back to
        ``None`` once it has been committed (see ``_apply_precomputed``);
        this is the supported way for a caller to get the real text back
        on demand, by reading it straight from the persistent
        ``outputs_index`` table -- never read
        ``self._row_cache[path].content`` directly, it may be stale/``None``
        even for a fully-indexed, up-to-date row. Mirrors
        :meth:`resolve_output`'s platform-aware path matching (Windows:
        case/slash-insensitive; POSIX: exact).
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
                    sql = (
                        "SELECT content FROM outputs_index "
                        "WHERE lower(replace(path, '\\', '/')) = ?"
                    )
                else:
                    sql = "SELECT content FROM outputs_index WHERE path = ?"
                row = con.execute(sql, [target]).fetchone()
            except Exception:  # noqa: BLE001
                _log.debug("OutputsFtsIndex.get_content failed for %r",
                            file_path, exc_info=True)
                return None
        return row[0] if row is not None else None

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


def _get_cached_index(
    outputs_dir: str, *, session_id: str | None = None,
) -> OutputsFtsIndex:
    """Look up (or create) the cached OutputsFtsIndex for a directory.

    ``session_id`` (a52216e2, optional): attributed to the index's write-lock
    lease for diagnostics (see ``lock_diagnostics()``/``read_index_lock_
    owner()``). Only applied when a NEW instance is created for this
    directory -- an already-cached instance keeps whatever session_id it was
    first constructed with, since the lease identity should stay stable for
    the lifetime of one process's index rather than flapping between callers.
    """
    key = _cache_key(outputs_dir)
    with _index_cache_lock:
        idx = _index_cache.pop(key, None)
        if idx is None:
            idx = OutputsFtsIndex(
                outputs_dir, db_path=_resolve_index_db_path(outputs_dir),
                session_id=session_id,
            )
        _index_cache[key] = idx
        while len(_index_cache) > _MAX_CACHED_INDEXES:
            _, evicted = _index_cache.popitem(last=False)
            evicted.close()
        return idx


def get_convergence_state(
    outputs_dir: str, subtree: str | None = None,
) -> dict[str, Any]:
    """Module-level convenience wrapper: explicit convergence state (item
    6af1518d requirement 1) for the SAME cached :class:`OutputsFtsIndex`
    instance ``search_outputs``/``annotate_outputs`` use for this
    ``outputs_dir``. Does NOT trigger a rebuild -- purely reads current
    state, so it's cheap and safe to poll.

    ``subtree`` (optional): scope the answer to a sub-path, not just the
    whole ``outputs_dir`` -- see :meth:`OutputsFtsIndex.get_convergence_state`.

    Returns ``{"error": ...}`` if ``outputs_dir`` doesn't exist; otherwise
    the :class:`ConvergenceState` as a dict -- including ``index_lock``
    (a52216e2): who currently holds this index's write lock (pid/hostname/
    session_id/started_at/heartbeat_at) and whether that owner looks active
    or stale. Never triggers any indexing and never disturbs a live writer.
    """
    if not outputs_dir or not os.path.isdir(outputs_dir):
        return {"error": f"outputs_dir does not exist: {outputs_dir}"}
    index = _get_cached_index(outputs_dir)
    return index.get_convergence_state(subtree=subtree).to_dict()


def register_priority_path(outputs_dir: str, path: str) -> dict[str, Any]:
    """Module-level convenience wrapper: provenance-triggered targeted
    registration (item 6af1518d requirement 3), using the SAME cached
    :class:`OutputsFtsIndex` instance a subsequent ``search_outputs`` call
    for this ``outputs_dir`` will use -- so a path registered here is
    genuinely reflected in the very next search, not a throwaway side index.

    Intended caller: ``annotate.record_provenance``, right after it
    successfully records a provenance entry for ``path`` -- so a
    provenance-known path becomes searchable promptly instead of waiting
    for the ambient full-root walk to reach it.
    """
    if not outputs_dir or not os.path.isdir(outputs_dir):
        return {"registered": False, "reason": "outputs_dir does not exist"}
    if not path or not str(path).strip():
        return {"registered": False, "reason": "path is required"}
    index = _get_cached_index(outputs_dir)
    result = index.register_priority_path(path)
    result["registered"] = True
    return result


def register_output_paths(outputs_dir: str, paths: list[str]) -> dict[str, Any]:
    """Module-level convenience wrapper: bulk-register a small, EXPLICIT
    list of exactly-known output paths (item b85394bd), using the SAME
    cached :class:`OutputsFtsIndex` instance a subsequent ``search_outputs``
    call for this ``outputs_dir`` will use -- so paths registered here are
    genuinely reflected in the very next search, not a throwaway side index.

    Distinct from :func:`register_priority_path` (single path, thin
    provenance-triggered wrapper): this is the direct, general-purpose entry
    point for a caller that already knows the exact file list it wants
    indexed right now -- e.g. a build/pipeline step announcing "these N
    files are the outputs I just produced" -- without waiting on, or
    depending on the timing of, the ambient full-root walk (Phase 0) to
    discover them on its own schedule. Delegates straight to
    :meth:`OutputsFtsIndex.index_paths`, which bypasses Phase 0's
    resumable-walk/backlog-throttle machinery entirely: cost is bounded by
    ``len(paths)``, not ``outputs_dir``'s total size, so this stays cheap
    and synchronous even against a huge, still-converging tree.

    Args:
      outputs_dir:  Absolute path to the outputs directory.
      paths:        Exact output file paths to register/index now.

    Returns:
      ``{"registered": True, "indexed": N, "queued": N, "paths": [...]}``
      on success (``indexed`` -- rows written this call; ``queued`` -- paths
      that don't exist on disk YET, retried automatically on a later call
      once they do), or ``{"registered": False, "reason": ...}`` when
      ``outputs_dir``/``paths`` are missing. Best effort, never raises.
    """
    if not outputs_dir or not os.path.isdir(outputs_dir):
        return {"registered": False, "reason": "outputs_dir does not exist"}
    if not paths:
        return {"registered": False, "reason": "paths is required"}
    index = _get_cached_index(outputs_dir)
    for p in paths:
        if p and str(p).strip():
            index._priority_registered.add(os.path.normpath(p))
    result = index.index_paths(paths)
    result["registered"] = True
    return result


def _find_ancestor_cached_index(subtree_dir: str) -> "OutputsFtsIndex | None":
    """Best-effort lookup of an already-cached :class:`OutputsFtsIndex`
    whose ``outputs_dir`` is an ANCESTOR of (or equal to) ``subtree_dir`` --
    i.e. a root/parent index that may already have indexed some or all of
    ``subtree_dir``'s files. Picks the DEEPEST (most specific) ancestor
    present in the cache, so a subtree-of-a-subtree prefers the nearer
    parent's data over the ultimate root's. Returns ``None`` if no such
    index is currently cached (nothing to reuse -- not an error).
    """
    target = os.path.normpath(os.path.abspath(subtree_dir))
    best: OutputsFtsIndex | None = None
    best_len = -1
    with _index_cache_lock:
        candidates = list(_index_cache.values())
    for idx in candidates:
        cand = os.path.normpath(os.path.abspath(idx.outputs_dir))
        if cand == target:
            continue
        try:
            common = os.path.commonpath([cand, target])
        except ValueError:
            continue  # different drives on Windows, etc.
        if common == cand and len(cand) > best_len:
            best = idx
            best_len = len(cand)
    return best


def get_subtree_index(root_outputs_dir: str, subtree_path: str) -> OutputsFtsIndex:
    """Hierarchical subtree indexing (item 6af1518d requirement 4): return a
    persistent, INDEPENDENTLY-converging :class:`OutputsFtsIndex` scoped to
    ``subtree_path``, seeded from a slice of any already-cached ancestor
    index's rows so a subtree already partly/fully covered by a prior
    root-level (or nearer-parent) rebuild doesn't pay a full re-walk/re-hash
    just because the query is now scoped narrower.

    ``subtree_path`` must be ``root_outputs_dir`` itself or a path
    underneath it -- raises ``ValueError`` otherwise (a "subtree" outside
    its claimed root is a caller bug, not a degrade-gracefully case).

    The returned index is the SAME cached instance ``search_outputs(subtree_
    path, ...)`` would use (both go through :func:`_get_cached_index`), so
    calling this before a search under the narrower path is a pure
    optimisation -- searching the subtree directly without ever calling this
    still works, just without the ancestor-seeding speedup.
    """
    root_norm = os.path.normpath(os.path.abspath(root_outputs_dir))
    sub_norm = os.path.normpath(os.path.abspath(subtree_path))
    if sub_norm != root_norm:
        try:
            common = os.path.commonpath([root_norm, sub_norm])
        except ValueError as exc:
            raise ValueError(
                f"{subtree_path!r} is not under {root_outputs_dir!r}"
            ) from exc
        if common != root_norm:
            raise ValueError(
                f"{subtree_path!r} is not under {root_outputs_dir!r}"
            )

    index = _get_cached_index(subtree_path)
    if not index._row_cache and not index._seeded_from_ancestor:
        ancestor = _find_ancestor_cached_index(subtree_path)
        if ancestor is not None:
            index.seed_from_ancestor(ancestor, subtree_path)
    return index


def search_outputs(
    outputs_dir: str,
    query: str,
    *,
    limit: int = 10,
    include_archival: bool = True,
    max_seconds: float | None = DEFAULT_REBUILD_BUDGET_SECONDS,
    subtree: str | None = None,
) -> dict[str, Any]:
    """BM25 search over a local outputs tree.

    ``subtree`` (item 6af1518d requirement 4, optional): scope indexing AND
    searching to a sub-path of ``outputs_dir`` without requiring a full
    re-walk of the root. When given, this delegates to a SEPARATE,
    independently-converging index for ``subtree`` (see
    :func:`get_subtree_index`) -- seeded from a slice of ``outputs_dir``'s
    own cached index when one already exists and has already covered some or
    all of ``subtree``, so the narrower query doesn't pay to re-hash files
    the root already indexed. The response's ``convergence``/``partial``/
    etc. fields then describe the SUBTREE's own convergence, not the whole
    root's -- giving a real, scoped answer instead of "ask the whole-root
    index and hope it's gotten there yet" (the exact inconsistency the real
    incident behind this item hit: root vs. narrow-subdirectory searches
    gave different zero-hit answers with no way to tell why).

    *** A ZERO-HIT RESULT IS NOT PROOF THE FILE/TERM DOESN'T EXIST. ***
    Always check ``partial``, ``fts_pending``, ``pending_stale_count``, and
    ``db_write_error`` on the response before concluding "not found" -- any of
    them being set means indexing is still in progress (or a write failed and
    is queued for retry), not that the search genuinely came up empty. When
    ``hits`` is empty AND the index is not fully converged, this function also
    sets ``zero_hits_warning`` (see below) as a single, hard-to-miss signal --
    but the underlying fields are the authoritative contract; do not rely on
    ``zero_hits_warning`` alone if you are inspecting fields programmatically.

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

    81a0b23d: ``total_indexed``/``total_in_index`` are both deliberately
    optimistic mid-pass (``rebuild()``'s ``all_paths`` retains every
    previously-indexed path so counts never regress while a resumable walk
    is still discovering the rest of the tree -- see
    ``_ResumableFileWalk``/Phase 0 in ``rebuild()``), so a brand-new file can
    look like it's covered by a large ``total_indexed`` when it hasn't been
    discovered yet.  When ``partial=True``, ``pending_stale_count`` gives the
    size of the confirmed-stale backlog still awaiting analysis+write
    (``OutputsFtsIndex._pending_stale``) so a caller can tell a zero-hit
    result on a mid-pass index (backlog non-empty, or the walk itself hasn't
    finished a pass) apart from a genuine miss on a fully-converged index.

    <surface-it-loudly>: when ``hits`` is empty AND at least one of
    ``partial``/``fts_pending``/``db_write_error`` is set, the response ALSO
    carries ``zero_hits_warning`` -- a human-readable string explaining that
    this specific zero-hit result must not be read as "not found" (a
    same-shaped WARNING is also logged server-side). This exists because the
    ``partial``/``fts_pending``/``pending_stale_count`` contract documented
    above, while already tracked and returned, was repeatedly missed by
    callers (frequently other AI agents) skimming only ``hits: []`` --
    exactly the misdiagnosis this docstring update and field were added to
    close.
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
    if subtree:
        if not os.path.isdir(subtree):
            result["error"] = f"subtree does not exist: {subtree}"
            return result
        try:
            index = get_subtree_index(outputs_dir, subtree)
        except ValueError as exc:
            result["error"] = str(exc)
            return result
        result["subtree"] = subtree
    else:
        index = _get_cached_index(outputs_dir)
    result["total_indexed"] = index.rebuild(max_seconds=max_seconds)
    # b1789c0d -- expose cumulative row count from the DB (which may be
    # larger than total_indexed on a partial rebuild that resumes prior work)
    # so callers can distinguish "cold tree, indexing in progress" from
    # "empty tree, nothing to find". total_in_index == len(index._row_cache)
    # because _row_cache always mirrors what is (or will be) in the DB.
    result["total_in_index"] = len(index._row_cache)
    # Planner/diagnostic surface: make discovery coverage and phase costs
    # explicit.  ``total_in_index`` is a historical row count; these fields
    # distinguish that from a completed filesystem walk and provide the
    # measurements needed to tune large-root scans.
    result["discovery"] = dict(index.last_rebuild_metrics)
    result["discovery"].setdefault(
        "walk_complete", index._walk_state is None,
    )
    result["discovery"]["row_cache_content_resident"] = False
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
        # 81a0b23d -- last_rebuild_partial was already tracked internally
        # (set whenever the resumable walk hasn't finished a pass, Phase 1
        # hit its deadline mid-backlog, or Phase 2's deadline passed before
        # the FTS commit) but nothing told the caller HOW MUCH work is left
        # queued. Mirrors the tantivy_lock_warning/9a18a2b2 precedent: make
        # real, already-tracked state visible instead of silently partial.
        # Only meaningful alongside partial=True -- a fully-converged
        # rebuild always leaves this backlog empty, so it's omitted there to
        # keep the existing full-coverage response shape unchanged.
        if index._pending_stale:
            result["pending_stale_count"] = len(index._pending_stale)
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
    if index.last_lock_error:
        # a52216e2 -- the write lock itself could not be acquired this call
        # (real cross-process contention against an active owner, or an
        # unexpected acquisition failure) -- rebuild() already degraded
        # gracefully (returned the last-known row count rather than raising),
        # but a caller silently getting a stale/empty result with no
        # indication why isn't actionable. Same precedent as
        # tantivy_lock_warning (9a18a2b2): surface it, don't hide it.
        result["index_lock_warning"] = index.last_lock_error
    # 6af1518d requirement 1 -- explicit, structured convergence state,
    # additive alongside the ad hoc partial/fts_pending/pending_stale_count
    # fields above (unchanged, for backwards compatibility). When `subtree`
    # was given, `index` is already the subtree-scoped instance, so this
    # describes the SUBTREE's own convergence, not the whole root's.
    result["convergence"] = index.get_convergence_state().to_dict()
    # e631d54f (follow-up to 6af1518d) -- a single, explicit `degraded` flag
    # mirroring `convergence["converged"]`, computed regardless of whether
    # `hits` is empty. The `zero_hits_warning` below only ever fires for a
    # ZERO-hit response; a NON-empty `hits` list from a not-yet-converged
    # index was previously indistinguishable from a complete answer unless a
    # caller cross-referenced `convergence["converged"]` itself. That gap is
    # exactly what makes a partial/stale index unsafe as an authoritative
    # pointer/provenance signal (e.g. "does an output matching X already
    # exist anywhere in the tree") -- a caller doing an existence/dedup check
    # must see `degraded=True` and treat these hits as candidates only, never
    # as proof nothing else matches.
    result["degraded"] = not result["convergence"]["converged"]
    if not hits and (
        result.get("partial") or result.get("fts_pending")
        or result.get("db_write_error")
        or not result["convergence"]["converged"]
    ):
        # <surface-it-loudly> -- b1789c0d/81a0b23d already tracked
        # partial/fts_pending/pending_stale_count/db_write_error internally,
        # and the module + tool docstrings already document the contract
        # ("an empty hits list with partial=True means... still pending").
        # In practice that was not loud enough: a caller (frequently another
        # AI agent, not a human reading this docstring) that only glances at
        # `hits: []` has repeatedly misread it as "file does not exist" even
        # though partial=True/fts_pending=True/db_write_error was sitting
        # right there in the SAME response -- the exact failure mode this
        # sprint item was opened to close. Rather than rely on a caller
        # cross-referencing three separate optional fields, add ONE
        # impossible-to-miss field on the exact response shape that triggers
        # the misread: zero hits + an incomplete/unpersisted index. Modeled
        # on the tantivy_lock_warning precedent (9a18a2b2) -- a plain string
        # that is simultaneously truthy-as-a-flag and a ready-to-relay
        # explanation for an LLM caller, needing no separate boolean.
        result["zero_hits_warning"] = (
            "search_outputs returned 0 hits, but indexing of outputs_dir has "
            "NOT finished (partial=True and/or fts_pending=True and/or "
            "db_write_error is set on this same response) -- this is NOT a "
            "reliable 'file does not exist' signal. Re-invoke search_outputs "
            "on the same outputs_dir and query to let indexing continue "
            "(see pending_stale_count for how much work remains queued), and "
            "only treat a miss as confirmed once a response comes back with "
            "no partial/fts_pending/db_write_error fields at all."
        )
        _log.warning(
            "search_outputs: zero hits for query=%r under outputs_dir=%r "
            "while the index is incomplete (partial=%s, fts_pending=%s, "
            "pending_stale_count=%s, db_write_error=%s) -- this is NOT a "
            "confirmed miss; caller should re-invoke rather than conclude "
            "the file/term doesn't exist",
            query, outputs_dir, result.get("partial"), result.get("fts_pending"),
            result.get("pending_stale_count"), result.get("db_write_error"),
        )
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


def get_indexed_output(outputs_dir: str, file_path: str) -> dict[str, Any] | None:
    """Read-only membership check: has ``file_path`` ever been discovered and
    indexed under ``outputs_dir`` -- WITHOUT triggering a rebuild (unlike
    :func:`resolve_figure_output`, which forces one first). Item bd5b8d79
    (per-file provenance authority) -- this is what lets a caller distinguish
    "this path is part of the outputs tree but has no exact provenance
    record" (a real, if unregistered, output) from "this path was never
    discovered by the walker at all".

    Delegates straight to :meth:`OutputsFtsIndex.resolve_output` (an exact-
    path SQL lookup against the persistent ``outputs_index`` table) on the
    SAME cached index instance every other tool in this module uses -- a row
    written by a prior process is visible here even before THIS process's
    own walk has rehydrated ``_row_cache``/``_manifest``, because the query
    runs directly against the on-disk DuckDB file (see ``_connect``).

    Returns:
      The resolved row (same shape as ``resolve_output``/
      ``resolve_figure_output``: path, generating_script, is_archival,
      canonical_path, sha256, kind, size, mtime, csv_columns, json_keys), or
      ``None`` if ``file_path`` has never been indexed under ``outputs_dir``
      (or ``outputs_dir``/``file_path`` are missing/invalid).
    """
    if not file_path or not str(file_path).strip():
        return None
    if not outputs_dir or not os.path.isdir(outputs_dir):
        return None
    index = _get_cached_index(outputs_dir)
    return index.resolve_output(file_path)


def get_indexed_output_status(outputs_dir: str, file_path: str) -> dict[str, Any]:
    """The authoritative-gate-safe sibling of :func:`get_indexed_output`
    (item e631d54f, follow-up to 6af1518d).

    :func:`get_indexed_output` returning ``None`` is ambiguous on a
    not-yet-converged index: it cannot distinguish "confirmed absent" from
    "the ambient walk simply hasn't reached this path yet". A caller that
    needs an AUTHORITATIVE absence answer (e.g. composing a
    provenance-status verdict, or a dedup/existence check before writing a
    new output) must consult ``degraded`` before trusting a ``None``/empty
    ``row`` here as confirmed absence -- when ``degraded`` is ``True``, the
    correct read is "unknown, index still catching up", never "absent".

    Never triggers a rebuild (same read-only contract as
    :func:`get_indexed_output`) -- this only adds the convergence context a
    caller needs to interpret the (unchanged) membership answer correctly.

    Returns:
      ``{"row": <dict|None>, "degraded": bool, "convergence": {...}}`` where
      ``row`` is identical to what :func:`get_indexed_output` returns, and
      ``convergence`` is :meth:`OutputsFtsIndex.get_convergence_state`'s
      dict (whole-``outputs_dir`` convergence -- this is a membership check
      against the full tree, not a subtree-scoped one). ``outputs_dir``/
      ``file_path`` missing or invalid returns ``row=None`` with
      ``degraded=True`` (never a confident "absent").
    """
    if not file_path or not str(file_path).strip():
        return {"row": None, "degraded": True, "convergence": None}
    if not outputs_dir or not os.path.isdir(outputs_dir):
        return {"row": None, "degraded": True, "convergence": None}
    index = _get_cached_index(outputs_dir)
    row = index.resolve_output(file_path)
    convergence = index.get_convergence_state().to_dict()
    return {
        "row": row,
        "degraded": not convergence["converged"],
        "convergence": convergence,
    }


def get_path_annotations(outputs_dir: str, path: str) -> list[dict[str, Any]]:
    """Read-only wrapper: annotations for ``path`` AND its ancestor
    directories (e.g. a directory-level ``MERIDIAN_NOTES.md`` note), read
    from the SAME cached index :func:`annotate_outputs`/``search_outputs``
    already use.

    Delegates straight to :meth:`OutputsFtsIndex.get_annotations_for_path`.
    Exposed at module level (item bd5b8d79) so a caller composing a richer
    per-path answer (``provenance_status.get_provenance_status``) can read
    directory-level fallback coverage without reaching into the class's
    instance directly. Never triggers a rebuild -- purely reads whatever
    annotations are already persisted.
    """
    if not path or not str(path).strip():
        return []
    if not outputs_dir or not os.path.isdir(outputs_dir):
        return []
    index = _get_cached_index(outputs_dir)
    return index.get_annotations_for_path(path)


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
