"""meridian_outputs.ledger_io -- multi-writer- and crash-safe JSON ledger I/O.

The provenance ledger (:mod:`annotate`) and the fingerprint ledger
(:mod:`fingerprint`) are small JSON objects that several writers update by
read-modify-write. Before this module each ledger had three gaps:

* **In-process lock only.** A ``threading`` lock serialised writers inside one
  process, but two processes (two MCP servers, or a script running beside a
  server) could both read the old ledger and the later ``os.replace`` silently
  dropped the earlier writer's entry. Both also shared one fixed ``.tmp`` name,
  so they could interleave bytes in the same temporary file.
* **Corrupt reads looked empty.** A truncated or garbled ledger read as
  ``{}``, so the next write replaced it with a one-entry ledger and every
  prior entry vanished without a trace, and lock-free readers reported
  "nothing stale" for a ledger they could not parse.
* **No fsync.** A crash right after ``os.replace`` could leave a zero-length
  ledger, which the empty-read rule then turned into a wipe.

This module closes all three, reusing the package's existing cross-process
primitive rather than adding a new one:

* :func:`ledger_transaction` holds :class:`outputs_local.IndexFileLock` on the
  ledger path (lock file ``<ledger>.lock`` beside it, inside the gitignored
  cache directory) for the whole read-modify-write. Contention fails closed:
  :class:`outputs_local.IndexLockAcquireError` propagates when the lock does
  not become free within its timeout. Every process takes the same
  atomic-create lease file whatever is installed; a lease left by a dead
  owner is reclaimed as stale.
* :func:`read_ledger_strict` returns ``{}`` only for a missing ledger. Bytes
  that are not a JSON object raise :class:`LedgerCorruptError`; they are never
  read as empty.
* Inside a transaction, :meth:`LedgerTransaction.load` copies corrupt bytes to
  ``<ledger>.corrupt-<sha256 prefix>`` (quarantine), logs a warning and
  continues from an empty ledger, so the writer is not blocked and the
  original bytes stay recoverable. The name comes from the bytes, so retries
  over the same corrupt content reuse one copy. If the quarantine copy cannot
  be written, the error propagates and nothing is replaced.
* :func:`raise_quarantined` is the fail-closed variant for writers whose
  existing records carry identity a write is checked against (the run
  manifest, the artifact registry): the corrupt bytes are quarantined the
  same way, then :class:`LedgerCorruptError` is raised and nothing is
  written.
* :func:`write_ledger_atomic` writes a unique temporary file in the ledger's
  own directory, flushes it, calls ``os.fsync`` on it, then moves it over the
  ledger with ``os.replace``. On any failure the temporary file is removed and
  the previous ledger is left byte-for-byte unchanged.

Readers stay lock-free: ``os.replace`` is atomic, so a reader always sees a
complete old or a complete new ledger. On Windows a reader that opens the
file while another process is replacing it can hit a transient sharing
violation; both the read and the replace retry that briefly.

Only the provenance and fingerprint ledgers use this module so far. The run
manifest, artifact registry and derived-cache manifest have the same shape
and can adopt it in a follow-up.

NO hosted call is made anywhere in this module -- fully local, matching the
rest of the ``meridian_outputs`` package.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import time
from contextlib import contextmanager
from typing import Any, Callable, Iterator, NoReturn

from . import outputs_local

_log = logging.getLogger(__name__)

__all__ = [
    "LedgerCorruptError",
    "LedgerTransaction",
    "ledger_transaction",
    "parse_ledger_bytes",
    "quarantine_ledger",
    "raise_quarantined",
    "read_ledger_strict",
    "write_ledger_atomic",
]

# Windows reports a sharing violation as PermissionError when one process
# opens or replaces a file that another process holds open (no
# FILE_SHARE_DELETE). It is transient for a ledger, whose writers hold it for
# milliseconds, so reads and replaces retry it a bounded number of times.
_SHARING_RETRIES = 20
_SHARING_RETRY_SLEEP_SECONDS = 0.025


class LedgerCorruptError(ValueError):
    """A ledger file exists but does not hold a JSON object.

    ``path`` is the ledger path; ``reason`` says what was wrong;
    ``quarantine_path`` is set when the bytes were copied aside. The message
    names only the ledger's basename, so a machine-local absolute path never
    reaches an API error.
    """

    def __init__(
        self, path: str, reason: str, *, quarantine_path: str | None = None,
    ) -> None:
        self.path = path
        self.reason = reason
        self.quarantine_path = quarantine_path
        super().__init__(f"ledger {os.path.basename(path)!r} is corrupt: {reason}")


def _retry_sharing(op: Callable[[], Any]) -> Any:
    """Run ``op``, retrying a Windows sharing violation (PermissionError)."""
    for attempt in range(_SHARING_RETRIES):
        try:
            return op()
        except PermissionError:
            if attempt == _SHARING_RETRIES - 1:
                raise
            time.sleep(_SHARING_RETRY_SLEEP_SECONDS)
    raise AssertionError("unreachable")  # pragma: no cover


def _read_bytes(path: str) -> bytes | None:
    """The ledger's bytes, or ``None`` when it does not exist."""
    def _op() -> bytes:
        with open(path, "rb") as fh:
            return fh.read()

    try:
        return _retry_sharing(_op)
    except FileNotFoundError:
        return None


def parse_ledger_bytes(path: str, raw: bytes) -> dict[str, Any]:
    """Parse ledger bytes, raising :class:`LedgerCorruptError` unless they
    decode to a JSON object. A zero-length file is corrupt, not empty: it is
    what a crash between create and write leaves behind."""
    if not raw.strip():
        raise LedgerCorruptError(path, "file is empty")
    try:
        data = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError:
        raise LedgerCorruptError(path, "file is not valid UTF-8") from None
    except ValueError as exc:
        raise LedgerCorruptError(path, f"file is not valid JSON ({exc})") from None
    if not isinstance(data, dict):
        raise LedgerCorruptError(
            path, f"top-level JSON value is {type(data).__name__}, not an object",
        )
    return data


def read_ledger_strict(path: str) -> dict[str, Any]:
    """Read a ledger without ever mistaking damage for emptiness.

    Returns ``{}`` only when ``path`` does not exist. Raises
    :class:`LedgerCorruptError` for bytes that are not a JSON object, and
    re-raises any other ``OSError`` (after retrying a transient Windows
    sharing violation). Takes no lock: safe inside :func:`ledger_transaction`
    and safe for lock-free readers.
    """
    raw = _read_bytes(path)
    if raw is None:
        return {}
    return parse_ledger_bytes(path, raw)


# Hex digits of the content sha256 in a quarantine name. A prefix keeps the
# name about as long as the old UTC stamp (Windows MAX_PATH); a prefix
# collision is harmless because an existing file is reused only when its
# bytes are identical.
_QUARANTINE_DIGEST_CHARS = 16


def _quarantine_name(path: str, raw: bytes) -> str:
    digest = hashlib.sha256(raw).hexdigest()[:_QUARANTINE_DIGEST_CHARS]
    return f"{path}.corrupt-{digest}"


def quarantine_ledger(path: str, raw: bytes | None = None) -> str | None:
    """Copy ``path``'s bytes to ``<path>.corrupt-<sha256 prefix>``.

    ``raw`` is the bytes to copy. A caller whose strict parse rejected bytes
    passes exactly those, so the copy is what failed even if the file was
    replaced since; without ``raw`` the file's current bytes are read.
    The name comes from the bytes, so the same corrupt content always maps
    to one quarantine file: when every commit after a quarantine fails, a
    retry reuses the existing copy instead of adding another full-size one.
    The copy is created exclusively and fsynced; an existing file is reused
    only when its bytes are identical, and is never overwritten (different
    bytes under the same name get a ``-<n>`` suffix). Returns the quarantine
    path, or ``None`` when ``raw`` is not given and ``path`` no longer
    exists. Raises ``OSError`` when the copy cannot be written; callers must
    then leave the ledger alone.
    """
    if raw is None:
        raw = _read_bytes(path)
        if raw is None:
            return None
    base = _quarantine_name(path, raw)
    candidate = base
    for suffix in range(1, 1000):
        try:
            fd = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0))
        except FileExistsError:
            if _read_bytes(candidate) == raw:
                return candidate  # these exact bytes are already quarantined
            candidate = f"{base}-{suffix}"
            continue
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(raw)
                fh.flush()
                os.fsync(fh.fileno())
        except BaseException:
            try:
                os.remove(candidate)
            except OSError:
                pass
            raise
        return candidate
    raise FileExistsError(f"no free quarantine name for {os.path.basename(path)!r}")


def _quarantine_or_raise(
    path: str, exc: LedgerCorruptError, raw: bytes | None = None,
) -> str | None:
    """Quarantine ``path`` (corrupt for ``exc.reason``; ``raw`` as in
    :func:`quarantine_ledger`); if the copy fails, raise a
    :class:`LedgerCorruptError` saying so (the ledger is left untouched)."""
    try:
        return quarantine_ledger(path, raw)
    except OSError as copy_exc:
        raise LedgerCorruptError(
            path,
            f"{exc.reason}; quarantine copy failed ({copy_exc.__class__.__name__}), "
            "ledger left untouched",
        ) from copy_exc


def raise_quarantined(exc: LedgerCorruptError, raw: bytes | None = None) -> NoReturn:
    """Fail closed on a corrupt ledger: quarantine it, then raise.

    For a writer that must not rebuild the ledger from empty the way
    :meth:`LedgerTransaction.load` does -- one whose existing records carry
    identity the write is checked against (a run manifest's per-run
    immutability, an artifact's per-id content hash), so continuing from
    ``{}`` would drop both the records and the check. The bytes are copied
    to ``<ledger>.corrupt-<sha256 prefix>`` exactly as ``load`` does, a
    warning is logged, and :class:`LedgerCorruptError` is raised with
    ``quarantine_path`` set. Nothing is written over the ledger, so the
    caller's write never happens.

    ``raw`` should be the exact bytes whose parse raised ``exc``. Those are
    what gets copied, so the quarantine holds what failed even when the
    file was replaced after the parse (these writers do not hold the
    cross-process ledger lock). Without ``raw`` the file is read again.
    """
    quarantined = _quarantine_or_raise(exc.path, exc, raw)
    _log.warning(
        "ledger %r is corrupt (%s); original bytes quarantined to %r, "
        "refusing to write over it",
        os.path.basename(exc.path), exc.reason,
        os.path.basename(quarantined) if quarantined else None,
    )
    raise LedgerCorruptError(
        exc.path,
        f"{exc.reason}; original bytes quarantined to "
        f"{os.path.basename(quarantined) if quarantined else None!r}, ledger left untouched",
        quarantine_path=quarantined,
    ) from exc


def _fsync_directory(directory: str) -> None:
    """Persist a rename on POSIX. Windows cannot open a directory this way,
    and NTFS journals the rename itself, so this is a no-op there."""
    if os.name != "posix":
        return
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


# Random bytes in a temporary file's name: "<ledger basename>.<8 hex>.tmp".
# Four bytes keep the name exactly as long as the tempfile.mkstemp name it
# replaced ("<basename>.<8 chars>.tmp"); a longer token pushed a cache
# directory near the Windows MAX_PATH limit (no long-path support) over it,
# and every ledger write there failed. O_EXCL still refuses a clash.
_TEMP_TOKEN_BYTES = 4
# A temporary file is renamed or removed within moments of its creation; one
# older than this is what a crash between create and os.replace left behind.
_STALE_TEMP_SECONDS = 3600.0


def _sweep_stale_temps(directory: str, basename: str) -> None:
    """Remove ``basename``'s leftover temporary files in ``directory``.

    Only names :func:`write_ledger_atomic` itself creates for this ledger
    (``<basename>.<8 hex>.tmp`` exactly, compared case-insensitively where the
    filesystem is) and only regular files whose mtime is older than
    :data:`_STALE_TEMP_SECONDS`, so a temporary file another writer is still
    filling is never touched. Best-effort: a file that cannot be listed,
    stat-ed or removed is left for a later write, and nothing is raised."""
    pattern = re.compile(
        re.escape(os.path.normcase(basename))
        + r"\.[0-9a-f]{%d}\.tmp" % (2 * _TEMP_TOKEN_BYTES)
    )
    cutoff = time.time() - _STALE_TEMP_SECONDS
    try:
        with os.scandir(directory) as entries:
            stale = [
                entry.path for entry in entries
                if pattern.fullmatch(os.path.normcase(entry.name))
                and entry.is_file(follow_symlinks=False)
                and entry.stat(follow_symlinks=False).st_mtime < cutoff
            ]
    except OSError:
        return
    for leftover in stale:
        try:
            os.remove(leftover)
        except OSError:
            pass


def write_ledger_atomic(path: str, data: dict[str, Any]) -> None:
    """Replace ``path`` with ``data`` as JSON, atomically and durably.

    Writes a uniquely named temporary file (``<ledger>.<8 hex>.tmp``) in the
    same directory, created exclusively (``O_EXCL``) so two writers can never
    share one, flushes it, calls ``os.fsync`` on it, then moves it over
    ``path`` with ``os.replace``. The file is created with mode ``0o666``
    and the process umask applied, as ``open(path, "w")`` would, so the
    ledger stays readable to the other accounts or containers the umask
    allows (``tempfile.mkstemp`` would force ``0o600``). On any failure the
    temporary file is removed and the previous ledger is untouched.
    ``TypeError`` from a non-JSON-serialisable value is raised before
    anything is renamed. A crash between create and replace still leaves the
    temporary file behind, so each write first removes this ledger's
    temporary files older than an hour (:func:`_sweep_stale_temps`).
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    basename = os.path.basename(path)
    _sweep_stale_temps(directory, basename)
    tmp = os.path.join(
        directory, f"{basename}.{secrets.token_hex(_TEMP_TOKEN_BYTES)}.tmp",
    )
    fd = os.open(
        tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o666,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        _retry_sharing(lambda: os.replace(tmp, path))
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    _fsync_directory(directory)


class LedgerTransaction:
    """Handle yielded by :func:`ledger_transaction`.

    ``load`` reads the ledger under the lock (quarantining corrupt bytes),
    ``commit`` writes it back atomically. ``quarantined_path`` is set when
    ``load`` had to quarantine.
    """

    def __init__(self, path: str, lock: Any = None) -> None:
        self.path = path
        self.quarantined_path: str | None = None
        self.committed = False
        self._lock = lock

    def load(
        self, loader: Callable[[str], dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Read the ledger with ``loader`` (default :func:`read_ledger_strict`).

        ``loader`` must not take the ledger lock itself: the lock behind
        :func:`ledger_transaction` is not reentrant. A corrupt ledger is
        quarantined and an empty dict returned, so the caller can rebuild it;
        if the quarantine copy fails, the original :class:`LedgerCorruptError`
        propagates and nothing is written.
        """
        read = loader or read_ledger_strict
        try:
            return read(self.path)
        except LedgerCorruptError as exc:
            quarantined = _quarantine_or_raise(self.path, exc)
            self.quarantined_path = quarantined
            _log.warning(
                "ledger %r is corrupt (%s); original bytes quarantined to %r, "
                "continuing from an empty ledger",
                os.path.basename(self.path), exc.reason,
                os.path.basename(quarantined) if quarantined else None,
            )
            return {}

    def commit(self, data: dict[str, Any]) -> None:
        # Refresh the lease before the write so a waiter never judges this
        # holder stale (heartbeat older than the 120 s threshold) during the
        # commit and reclaims the lock mid-write. The commit phase is only
        # the atomic write below; callers keep their own work before it.
        # Remaining limit: work between acquire and commit that by itself
        # outlasts the threshold can still be reclaimed before this runs
        # (on POSIX; on Windows the open lock file cannot be removed).
        if self._lock is not None:
            self._lock.heartbeat(force=True)
        write_ledger_atomic(self.path, data)
        self.committed = True


@contextmanager
def ledger_transaction(
    path: str, *, timeout: float | None = None,
) -> Iterator[LedgerTransaction]:
    """Hold the cross-process ledger lock for one read-modify-write.

    Usage::

        with ledger_transaction(ledger_path) as txn:
            ledger = txn.load()
            ledger[key] = record
            txn.commit(ledger)

    The lock is :class:`outputs_local.IndexFileLock` on ``path`` (lock file
    ``<path>.lock``). ``timeout`` bounds the atomic-create fallback's wait;
    ``None`` uses ``IndexFileLock``'s own default
    (``MERIDIAN_OUTPUTS_LOCK_TIMEOUT_SECONDS``, 60 s). Raises
    :class:`outputs_local.IndexLockAcquireError` when the lock stays busy.
    The in-process part of the lock is not reentrant: never open a second
    transaction on the same path from inside one.
    """
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    lock = outputs_local.IndexFileLock(path, timeout=timeout)
    lock.acquire()
    try:
        yield LedgerTransaction(path, lock)
    finally:
        lock.release()
