"""The Zotero save journal: the outcome of each item write, kept on disk so it survives quitting. Stdlib and ctypes only, no Qt.

Each entry holds one line of the reading list and an outcome. An item's entry is written as "writing" before its write
starts, and changed to landed, unknown or refused once the outcome is known. An entry that is still writing is held, and
so is an entry that is unknown, until the person checks Zotero and clears it. A writing entry also records the process
that is writing it, by its id and start time. Once that process has exited, its write can no longer finish, so the entry
reads as unknown and can be cleared. A writing entry whose process is still running is never cleared, because another
window may still be writing it. Only the line text, an id, an outcome, a time, and for a writing entry the owner's
process id and start time are stored. Never a token, a grant key or a response body.

Several windows, and several processes, may use the journal at once. So every operation runs under an exclusive lock on
a sidecar lock file. Inside the lock it re-reads the journal from disk, applies one change, and writes the result.
Nothing writes from a copy that was read earlier. Before the journal is replaced, its new contents are first written to
a last-good backup. A journal that is missing or cannot be parsed, after the backup has been written once, is unreadable,
and its message names the lines the backup holds. A lock that another process holds is waited for, up to the timeout of
the operation. Only a lock error that means "held elsewhere" is waited for. Any other lock error is raised at once, with
its real cause.
"""

from __future__ import annotations

import datetime as dt
import errno
import functools
import json
import os
import tempfile
import time
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

from meridian_desktop.settings_store import _replace_with_retry, default_settings_path

JOURNAL_NAME = "zotero_saves.json"
BACKUP_SUFFIX = ".bak"
LOCK_SUFFIX = ".lock"
JOURNAL_VERSION = 1
MAX_ENTRIES = 500
# How long a write waits for another window's lock, and how long a read that gates the window waits.
LOCK_TIMEOUT_SECONDS = 10.0
UI_READ_TIMEOUT_SECONDS = 0.5
LOCK_POLL_SECONDS = 0.02
WRITING = "writing"
LANDED = "landed"
UNKNOWN = "unknown"
REFUSED = "refused"
OUTCOMES = (WRITING, LANDED, UNKNOWN, REFUSED)
HELD_OUTCOMES = (WRITING, UNKNOWN)
WRITE_FAILED_TEXT = (
    "The Zotero save journal could not be written, so nothing more was saved. Check Zotero before you save again."
)
LOCK_TIMEOUT_TEXT = "The journal is busy in another window. Try again in a moment."
LOCK_FAILED_TEXT = "The Zotero save journal could not be locked, so saving is off."
LINE_HELD_TEXT = (
    "That line is already held, or is being written in another window, so it was not sent. Check Zotero before you "
    "save it again."
)
NO_ENTRY_TEXT = "The Zotero save journal has no entry for that write, so its outcome was not recorded."
_FIELDS = ("id", "line", "outcome", "at")
_STORED = (*_FIELDS, "owner_pid", "owner_started")
# The lock errors that mean another process holds the lock, so the wait goes on: EACCES on Windows, EAGAIN on POSIX.
_CONTENTION_ERRNOS = frozenset({errno.EACCES, errno.EAGAIN, errno.EDEADLK})
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
_ERROR_INVALID_PARAMETER = 87


def journal_unreadable_text(path: Path, held_lines: Sequence[str] = ()) -> str:
    """The message when the journal cannot be read. It names the held lines the backup holds, when there are any."""
    message = (
        "The Zotero save journal could not be read, so saving is off. Check Zotero before moving or fixing "
        f"{path}, then restart the desktop."
    )
    if held_lines:
        message += " These lines may already be in Zotero and stay held: " + "; ".join(held_lines) + "."
    return message


class JournalError(Exception):
    """The journal could not be read, written or locked. ``message`` is fixed text for a person to read.

    ``held_lines`` names the lines the last-good backup holds, when the journal is unreadable. ``busy`` is True only when
    another window held the lock for the whole wait. That is not a fault, so the caller may try again.
    """

    def __init__(self, message: str, held_lines: Sequence[str] = (), *, busy: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.held_lines = tuple(held_lines)
        self.busy = busy


@dataclass(frozen=True)
class JournalEntry:
    """One line of the reading list and its outcome. ``at`` is the UTC time of the last change, in ISO 8601.

    A writing entry names its owner: the process id, and that process's start time where the system gives one.
    """

    id: str
    line: str
    outcome: str
    at: str
    owner_pid: int | None = None
    owner_started: str | None = None


def default_path() -> Path:
    """The journal beside the desktop's settings file, in the desktop state folder."""
    return default_settings_path().parent / JOURNAL_NAME


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


@functools.lru_cache(maxsize=1)
def _kernel32():
    """The Windows functions the process check uses, with their types set so a handle is never truncated."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    filetime = ctypes.POINTER(wintypes.FILETIME)
    kernel32.GetProcessTimes.argtypes = (wintypes.HANDLE, filetime, filetime, filetime, filetime)
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32


def _windows_process(pid: int) -> tuple[bool, str | None]:
    """Whether process ``pid`` is running, and its start time as a token. Uses the query-limited right, which any caller
    can ask for, and reads the exit code: STILL_ACTIVE means the process has not exited."""
    import ctypes
    from ctypes import wintypes

    kernel32 = _kernel32()
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # ERROR_INVALID_PARAMETER means no process has this id. Any other failure, such as access denied, may be a live
        # process, so it counts as running, and its entry is kept.
        return ctypes.get_last_error() != _ERROR_INVALID_PARAMETER, None
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True, None
        if code.value != _STILL_ACTIVE:
            return False, None
        created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        times = (ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user))
        if not kernel32.GetProcessTimes(handle, *times):
            return True, None
        return True, str((created.dwHighDateTime << 32) | created.dwLowDateTime)
    finally:
        kernel32.CloseHandle(handle)


def _process_state(pid: int) -> tuple[bool, str | None]:
    """Whether process ``pid`` is running, and its start time as a token. The token is None where the system gives none."""
    if pid <= 0:
        return False, None
    if os.name == "nt":
        return _windows_process(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False, None
    except PermissionError:
        pass  # the process exists but belongs to another user
    return True, None


def _owner_running(entry: JournalEntry) -> bool:
    """Whether the process writing ``entry`` is still running. A writing entry with no owner cannot be shown to be live."""
    if entry.owner_pid is None:
        return False
    running, token = _process_state(entry.owner_pid)
    if not running:
        return False
    # A process id can be given to a new process, so a different start time means the owner has exited.
    return entry.owner_started is None or token is None or token == entry.owner_started


def _settled(entries: Sequence[JournalEntry]) -> list[JournalEntry]:
    """The entries as they stand now. A writing entry whose owner has exited is unknown, because its write can no longer
    finish, and the person must check Zotero before the line is released."""
    return [
        replace(entry, outcome=UNKNOWN, owner_pid=None, owner_started=None)
        if entry.outcome == WRITING and not _owner_running(entry)
        else entry
        for entry in entries
    ]


def _parse_entries(raw: bytes) -> list[JournalEntry] | None:
    """The entries in a journal's bytes, or None when they are not a journal. Never raises."""
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    entries = value.get("entries") if isinstance(value, dict) else None
    if not isinstance(entries, list):
        return None
    parsed: list[JournalEntry] = []
    for item in entries:
        if not isinstance(item, dict) or not all(isinstance(item.get(name), str) for name in _FIELDS):
            return None
        if item["outcome"] not in OUTCOMES:
            return None
        pid, started = item.get("owner_pid"), item.get("owner_started")
        if pid is not None and (isinstance(pid, bool) or not isinstance(pid, int)):
            return None
        if started is not None and not isinstance(started, str):
            return None
        parsed.append(
            JournalEntry(
                id=item["id"],
                line=item["line"],
                outcome=item["outcome"],
                at=item["at"],
                owner_pid=pid,
                owner_started=started,
            )
        )
    return parsed


def _serialize(entries: Sequence[JournalEntry]) -> bytes:
    payload = {"version": JOURNAL_VERSION, "entries": [{name: getattr(e, name) for name in _STORED} for e in entries]}
    return json.dumps(payload, indent=2).encode("utf-8")


def _pruned(entries: Sequence[JournalEntry]) -> list[JournalEntry]:
    """Keep every held entry, and the most recent of the rest, up to MAX_ENTRIES in all."""
    if len(entries) <= MAX_ENTRIES:
        return list(entries)
    held = [entry for entry in entries if entry.outcome in HELD_OUTCOMES]
    others = [entry for entry in entries if entry.outcome not in HELD_OUTCOMES]
    room = max(MAX_ENTRIES - len(held), 0)
    kept_others = {id(entry) for entry in others[len(others) - room :]} if room else set()
    return [entry for entry in entries if entry.outcome in HELD_OUTCOMES or id(entry) in kept_others]


def _atomic_write(target: Path, data: bytes) -> None:
    """Write ``data`` to a temporary file beside ``target``, then replace ``target`` with it. Retries a Windows lock."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        _replace_with_retry(temp, target)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def _lock_file(handle) -> None:
    """Take an exclusive lock on the whole lock file without waiting. Raises OSError while another holder has it."""
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(handle) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _is_contention(exc: OSError) -> bool:
    """True when a lock error means another holder has the lock, so the caller should wait and try again."""
    return exc.errno in _CONTENTION_ERRNOS


def _lock_failed(exc: OSError) -> JournalError:
    """A lock error that is not contention. The message keeps the real cause, and it is never reported as busy."""
    cause = exc.strerror or str(exc) or type(exc).__name__
    return JournalError(f"{LOCK_FAILED_TEXT} The system reported: {cause}.")


class SaveJournal:
    """The journal at ``path``. Every operation re-reads the file under the lock, so windows and processes never drop
    each other's entries.

    Writes wait up to ``lock_timeout`` for another holder of the lock. Reads wait up to ``read_timeout``, which defaults
    to ``lock_timeout``. Constructing it reads the journal, and raises JournalError if the journal is unreadable.
    """

    def __init__(
        self, path: Path, *, lock_timeout: float = LOCK_TIMEOUT_SECONDS, read_timeout: float | None = None
    ) -> None:
        self._path = Path(path)
        self._backup = self._path.with_name(self._path.name + BACKUP_SUFFIX)
        self._lock_path = self._path.with_name(self._path.name + LOCK_SUFFIX)
        self._write_timeout = lock_timeout
        self._read_timeout = lock_timeout if read_timeout is None else read_timeout
        with self._locked(self._read_timeout):
            self._read_locked()

    @property
    def path(self) -> Path:
        return self._path

    @contextmanager
    def _locked(self, timeout: float) -> Iterator[None]:
        """Hold the exclusive lock for one operation. Waits up to ``timeout`` for another holder, then raises JournalError
        with ``busy`` set. A lock error that is not contention is raised at once."""
        try:
            self._lock_path.parent.mkdir(parents=True, exist_ok=True)
            handle = open(self._lock_path, "a+b")
        except OSError as exc:
            raise _lock_failed(exc) from exc
        try:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    _lock_file(handle)
                    break
                except OSError as exc:
                    if not _is_contention(exc):
                        raise _lock_failed(exc) from exc
                    if time.monotonic() >= deadline:
                        raise JournalError(LOCK_TIMEOUT_TEXT, busy=True) from exc
                    time.sleep(LOCK_POLL_SECONDS)
            try:
                yield
            finally:
                try:
                    _unlock_file(handle)
                except OSError:
                    pass
        finally:
            handle.close()

    def _backup_held(self) -> tuple[str, ...]:
        """The lines the last-good backup holds as writing or unknown. Empty when the backup is missing or unreadable."""
        try:
            raw = self._backup.read_bytes()
        except OSError:
            return ()
        entries = _settled(_parse_entries(raw) or [])
        return tuple(entry.line for entry in entries if entry.outcome in HELD_OUTCOMES)

    def _unreadable(self) -> JournalError:
        held = self._backup_held()
        return JournalError(journal_unreadable_text(self._path, held), held)

    def _read_locked(self) -> list[JournalEntry]:
        """The journal as it is on disk now, settled. Caller holds the lock. A missing journal is empty only if no write has
        ever happened, which the backup records."""
        try:
            raw = self._path.read_bytes()
        except FileNotFoundError:
            if self._backup.exists():
                raise self._unreadable() from None
            return []
        except OSError:
            raise self._unreadable() from None
        entries = _parse_entries(raw)
        if entries is None:
            raise self._unreadable()
        return _settled(entries)

    def _write_locked(self, entries: Sequence[JournalEntry]) -> None:
        """Write the journal. The new contents go to the last-good backup first, then replace the journal. Caller holds the lock."""
        data = _serialize(_pruned(entries))
        try:
            _atomic_write(self._backup, data)
            _atomic_write(self._path, data)
        except OSError:
            raise JournalError(WRITE_FAILED_TEXT) from None

    def held(self) -> list[JournalEntry]:
        """The entries that hold a line, read from disk under the lock: writing, or unknown."""
        with self._locked(self._read_timeout):
            return [entry for entry in self._read_locked() if entry.outcome in HELD_OUTCOMES]

    def begin(self, line: str) -> str:
        """Record that the write of ``line`` is about to start, owned by this process. Returns the entry's id.

        Refused when another entry already holds the same line, so two windows cannot both post it.
        """
        owner = os.getpid()
        entry = JournalEntry(
            id=uuid.uuid4().hex,
            line=line,
            outcome=WRITING,
            at=_now(),
            owner_pid=owner,
            owner_started=_process_state(owner)[1],
        )
        with self._locked(self._write_timeout):
            entries = self._read_locked()
            if any(other.line == line and other.outcome in HELD_OUTCOMES for other in entries):
                raise JournalError(LINE_HELD_TEXT)
            self._write_locked([*entries, entry])
        return entry.id

    def finish(self, entry_id: str, outcome: str) -> None:
        """Record the outcome of a write that was begun. ``outcome`` is landed, unknown or refused."""
        if outcome not in (LANDED, UNKNOWN, REFUSED):
            raise ValueError(f"not a finished outcome: {outcome}")
        with self._locked(self._write_timeout):
            entries = self._read_locked()
            if not any(entry.id == entry_id for entry in entries):
                raise JournalError(NO_ENTRY_TEXT)
            updated = [
                replace(e, outcome=outcome, at=_now(), owner_pid=None, owner_started=None) if e.id == entry_id else e
                for e in entries
            ]
            self._write_locked(updated)

    def record_unknown(self, lines: Sequence[str]) -> None:
        """Hold lines whose outcome is not known at all, such as when a save produced no result."""
        stamp = _now()
        added = [JournalEntry(id=uuid.uuid4().hex, line=line, outcome=UNKNOWN, at=stamp) for line in lines]
        with self._locked(self._write_timeout):
            entries = self._read_locked()
            self._write_locked([*entries, *added])

    def clear(self, entry_ids: Sequence[str]) -> int:
        """Remove the given entries, but only the unknown ones. A writing entry is never cleared, because its write may
        still be in flight in another window. Returns how many were removed."""
        wanted = set(entry_ids)
        with self._locked(self._write_timeout):
            entries = self._read_locked()
            kept = [entry for entry in entries if not (entry.id in wanted and entry.outcome == UNKNOWN)]
            removed = len(entries) - len(kept)
            if removed:
                self._write_locked(kept)
        return removed


__all__ = [
    "HELD_OUTCOMES",
    "JOURNAL_NAME",
    "LANDED",
    "LOCK_FAILED_TEXT",
    "LOCK_TIMEOUT_TEXT",
    "REFUSED",
    "UI_READ_TIMEOUT_SECONDS",
    "UNKNOWN",
    "WRITING",
    "JournalEntry",
    "JournalError",
    "SaveJournal",
    "default_path",
    "journal_unreadable_text",
]
