"""Tell the owner when this window's code, or the local server it started, has changed on disk. Pure Python: no Qt.

RestartWatch takes a baseline of the meridian_desktop modules loaded so far: the package files, the package __init__
files, and __main__ when it sits in the package folder. The executable is added when the app is frozen. Each check
stats those same files again. A file with a different size or modification time counts as changed, and so does a file
that is gone. Any other OSError keeps the previous stamp, because a locked or half-written file is not a removed one.
A new file on disk is ignored, because the running code never read it.

On the GUI thread a check stats the tracked files. For a server this window started, it also makes one process query and
spends at most MAX_ENTRIES_PER_TICK steps on folders and files that were unreadable when the start was confirmed. No file
contents are read, so a check is cheap enough for a UI timer. The first walk of the server's folder runs on the thread
that confirmed the start, with no bound.

Limit: modules are recorded at the first check after the watch is built, not at import. A module that changes between
its own import and that first check is not caught. A module loaded later is recorded at the first check that sees it,
and the same limit applies to it.

The server case needs a process to watch. note_server_started is called on the thread that ran the start, with the PID
that a successful local start reported. It reads the process creation time, then takes a snapshot of every .py file
under the core checkout's meridian folder: size and modification time. Each check stats only those files. A file that
changed or is gone means the running server predates it. The snapshot is taken when the start is confirmed, so an edit
made between the process starting and that moment is not seen. A .py file added after the snapshot is not seen either.
A file or folder that cannot be read when the start is confirmed stays pending. Each check tries it again, and its first
readable stamp becomes its baseline, so a change made before that stamp is not seen. A pending file that is missing for a
moment stays pending too, so it is baselined again when it comes back.

The watch drops the server only when the reader reports it GONE: the process has exited, or its PID now names another
process. A creation time that cannot be read right now does not drop it. The server is then recorded with an unknown
start time, and the next check reads the start time again. Until it is known, a reused PID cannot be ruled out. Access
denied on a PID this window started also counts as GONE, because the same user started that process. Creation times are
read through Windows, so the server case is off on other platforms. A server that was already running before the owner
pressed Start is not watched. This window does not know when that process started.

Restart never happens here. The window only says that a restart is needed.
"""

from __future__ import annotations

import os
import re
import sys
import threading
from collections import deque
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

PACKAGE = "meridian_desktop"
# The core package that the runner imports. The server watch looks at its .py files only.
SERVER_PACKAGE = "meridian"
# The most names the banner lists. The rest are counted.
MAX_NAMES = 3
# The most steps one GUI-thread refresh may spend: folder entries listed, plus pending files stat'd.
MAX_ENTRIES_PER_TICK = 200
# Windows FILETIME ticks (100 ns) from 1601-01-01 to the Unix epoch.
_EPOCH_TICKS = 116_444_736_000_000_000
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_ERROR_ACCESS_DENIED = 5
_ERROR_INVALID_PARAMETER = 87
_SEPARATORS = re.compile(r"[\\/]+")


class ProcessGone:
    """What a process reader returns when the process is not running, or the PID cannot name a process."""

    def __repr__(self) -> str:
        return "GONE"


GONE = ProcessGone()


@dataclass(frozen=True)
class FileStamp:
    """The two numbers a check compares. A file is changed when either one differs."""

    mtime_ns: int
    size: int


@dataclass(frozen=True)
class Restart:
    """What changed. ``code`` holds the display names of changed or removed code files.

    ``server`` is True when the server this window started is stale.
    """

    code: tuple[str, ...] = ()
    server: bool = False

    @property
    def needed(self) -> bool:
        return bool(self.code) or self.server


@dataclass(frozen=True)
class _Server:
    pid: int
    # None until the creation time has been read. Only GONE drops the record.
    started_ns: int | None
    files: ServerFiles


def process_started_ns(pid: int) -> int | None | ProcessGone:
    """The creation time of a process in Unix nanoseconds, GONE, or None when it cannot be read right now.

    GONE covers a PID that cannot name a process, a process whose exit time is already set (even if this program still
    holds a handle to it), and access denied. This window queries only the PIDs it started, as the same user, so access
    denied means another user's process has taken the PID. None covers any other query failure, and every platform
    except Windows. The Windows calls go through ctypes, so the app needs no new package.
    """
    if pid <= 0:
        return GONE
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        if ctypes.get_last_error() in (_ERROR_INVALID_PARAMETER, _ERROR_ACCESS_DENIED):
            return GONE
        return None
    try:
        created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        ok = kernel32.GetProcessTimes(
            handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user),
        )
        if not ok:
            return None
        if exited.dwLowDateTime or exited.dwHighDateTime:
            return GONE
        ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
    finally:
        kernel32.CloseHandle(handle)
    return (ticks - _EPOCH_TICKS) * 100


def stamp(path: str) -> FileStamp | None:
    """The stamp of one file, or None when the file is gone. Only stat is called.

    Any other OSError is raised. The caller then keeps the stamp it already has.
    """
    try:
        info = os.stat(path)
    except FileNotFoundError:
        return None
    return FileStamp(info.st_mtime_ns, info.st_size)


def package_files(modules: Mapping[str, Any]) -> set[str]:
    """The ``__file__`` of every meridian_desktop module in ``modules``, __init__ files included.

    The app's __main__ is included when it sits in the package folder, which is the case under ``python -m``.
    """
    files: set[str] = set()
    for name, module in list(modules.items()):
        file = getattr(module, "__file__", None)
        if not isinstance(file, str) or not file:
            continue
        if name == PACKAGE or name.startswith(PACKAGE + "."):
            files.add(file)
        elif name == "__main__" and _is_package_main(file):
            files.add(file)
    return files


def _is_package_main(file: str) -> bool:
    segments = [part for part in _SEPARATORS.split(file) if part]
    return len(segments) >= 2 and segments[-1] == "__main__.py" and segments[-2] == PACKAGE


class ServerFiles:
    """The core .py files a server was started from.

    ``stamps`` holds the baseline of every file that has been read. A file that cannot be stat'd, or that is missing for
    a moment, is pending: it waits in a queue and is tried again, in turn, on each refresh. The first readable stamp
    becomes its baseline. A folder that cannot be listed is pending in the same way. Its entries join as pending when it
    is listed, so the files inside are baselined on a later tick.

    A refresh spends at most MAX_ENTRIES_PER_TICK steps, counting folder entries listed and pending files stat'd. It
    resumes where it stopped on the next tick, so a large folder never stalls the GUI thread. The first pass, made when
    the object is built, has no bound because it runs on the thread that confirmed the start.
    """

    def __init__(self, root: Path) -> None:
        self.stamps: dict[str, FileStamp] = {}
        self._pending: deque[str] = deque()
        self._pending_set: set[str] = set()
        self._folders: deque[str] = deque()
        # A folder being listed across ticks, with its open listing. None when no folder is half read.
        self._scan: tuple[str, Iterator[os.DirEntry[str]]] | None = None

        def folder_failed(error: OSError) -> None:
            if error.filename:
                self._folders.append(str(error.filename))

        for dirpath, _dirs, files in os.walk(root, onerror=folder_failed):
            for name in files:
                if name.endswith(".py"):
                    self._add_pending(os.path.join(dirpath, name))
        self._work(None)

    def refresh(self) -> None:
        """Spend up to MAX_ENTRIES_PER_TICK steps on the pending work. Called on the GUI thread."""
        self._work(MAX_ENTRIES_PER_TICK)

    def tracked(self) -> set[str]:
        """Every .py path this object knows about, baselined or pending."""
        return set(self.stamps) | self._pending_set

    def changed(self) -> bool:
        """True when a file with a baseline is gone or differs from it. Pending and unreadable files are not changes."""
        for path, before in self.stamps.items():
            try:
                now = stamp(path)
            except OSError:
                continue
            if now != before:
                return True
        return False

    def _add_pending(self, path: str) -> None:
        if path not in self.stamps and path not in self._pending_set:
            self._pending.append(path)
            self._pending_set.add(path)

    def _work(self, budget: int | None) -> None:
        """Spend ``budget`` steps on the folders and the pending files. None means no bound."""
        left = budget
        while self._scan is not None or self._folders:
            if left is not None and left <= 0:
                break
            if self._scan is None:
                folder = self._folders.popleft()
                try:
                    self._scan = (folder, os.scandir(folder))
                except OSError:
                    self._folders.append(folder)
                    break
            left, ended = self._list(left)
            if not ended:
                break
        self._stat_pending(left)

    def _list(self, left: int | None) -> tuple[int | None, bool]:
        """Read entries from the open folder. Returns the steps left and whether the folder is finished."""
        folder, entries = self._scan
        while left is None or left > 0:
            try:
                entry = next(entries)
            except StopIteration:
                entries.close()
                self._scan = None
                return left, True
            except OSError:
                entries.close()
                self._scan = None
                self._folders.append(folder)
                return left, False
            if left is not None:
                left -= 1
            self._classify(entry)
        return left, False

    def _classify(self, entry: os.DirEntry[str]) -> None:
        try:
            is_folder = entry.is_dir(follow_symlinks=False)
        except OSError:
            is_folder = False
        # os.walk does not follow links either, so a link cannot make the scan loop.
        if is_folder:
            self._folders.append(entry.path)
        elif entry.name.endswith(".py"):
            self._add_pending(entry.path)

    def _stat_pending(self, left: int | None) -> None:
        """Stat up to ``left`` pending files, in turn. A file that is still unreadable or missing goes to the back."""
        tries = len(self._pending) if left is None else min(len(self._pending), max(left, 0))
        for _ in range(tries):
            path = self._pending.popleft()
            try:
                now = stamp(path)
            except OSError:
                self._pending.append(path)
                continue
            if now is None:
                self._pending.append(path)
                continue
            self._pending_set.discard(path)
            self.stamps[path] = now


def display_name(path: str) -> str:
    """The name the banner shows: from the package folder down, for example meridian_desktop/ui/main_window.py."""
    segments = [part for part in _SEPARATORS.split(path) if part]
    if PACKAGE in segments:
        index = len(segments) - 1 - segments[::-1].index(PACKAGE)
        return "/".join(segments[index:])
    return segments[-1] if segments else path


def banner_text(restart: Restart) -> str:
    """The sentence the banner shows, or "" when no restart is needed."""
    if not restart.needed:
        return ""
    sentences: list[str] = []
    if restart.code:
        names = ", ".join(restart.code[:MAX_NAMES])
        extra = len(restart.code) - MAX_NAMES
        if extra > 0:
            names += f" and {extra} more"
        sentences.append(f"Restart needed: {names} changed on disk since this window opened.")
    if restart.server:
        if sentences:
            sentences.append("The local Meridian server changed on disk since it started.")
        else:
            sentences.append("Restart needed: the local Meridian server changed on disk since it started.")
    return " ".join(sentences)


class RestartWatch:
    """The baseline for this window's code, and the local server it started, if any.

    ``check`` and ``note_server_started`` may run on different threads. The server record is only read or replaced under
    the lock. A check works from one snapshot of the record, taken under the lock, and never reads it again.
    """

    def __init__(
        self,
        *,
        modules: Mapping[str, Any] | None = None,
        frozen: bool | None = None,
        executable: str | None = None,
        started_ns_of: Callable[[int], int | None | ProcessGone] = process_started_ns,
    ) -> None:
        self._modules: Mapping[str, Any] = sys.modules if modules is None else modules
        self._frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
        self._executable = sys.executable if executable is None else executable
        self._started_ns_of = started_ns_of
        self._stamps: dict[str, FileStamp] = {}
        self._lock = threading.Lock()
        self._server: _Server | None = None
        self._add_loaded()

    def _code_paths(self) -> set[str]:
        paths = package_files(self._modules)
        if self._frozen and self._executable:
            paths.add(self._executable)
        return paths

    def _add_loaded(self) -> None:
        for path in self._code_paths():
            if path in self._stamps:
                continue
            try:
                found = stamp(path)
            except OSError:
                # Unreadable right now. It is tried again on the next check, and it is not dropped.
                continue
            if found is not None:
                self._stamps[path] = found

    def note_server_started(self, pid: int, checkout: str) -> None:
        """Watch the server that a successful local start reported. Called on the thread that ran the start.

        Only GONE means nothing is watched. A creation time that cannot be read is recorded as unknown, and each later
        check reads it again.
        """
        if not checkout:
            return
        started = self._started_ns_of(pid)
        if started is GONE:
            return
        files = ServerFiles(Path(checkout) / SERVER_PACKAGE)
        server = _Server(pid, started if isinstance(started, int) else None, files)
        with self._lock:
            self._server = server

    def _replace_if_current(self, old: _Server, new: _Server | None) -> None:
        """Set the record to ``new`` only while ``old`` is still the record. A newer start is never overwritten."""
        with self._lock:
            if self._server is old:
                self._server = new

    def _server_stale(self) -> bool:
        with self._lock:
            server = self._server
        if server is None:
            return False
        state = self._started_ns_of(server.pid)
        # Every decision below uses this one snapshot. A start that replaces the record while this check runs is seen
        # by the next check, and this check never reports the record that replaced it.
        if state is GONE or (isinstance(state, int) and server.started_ns is not None and state != server.started_ns):
            # The process has exited, or its PID now belongs to another process.
            self._replace_if_current(server, None)
            return False
        if isinstance(state, int) and server.started_ns is None:
            # The start time has just been read for the first time. Record it, so a reused PID is caught from now on.
            learned = replace(server, started_ns=state)
            self._replace_if_current(server, learned)
            server = learned
        server.files.refresh()
        return server.files.changed()

    def check(self) -> Restart:
        """Compare the baseline with the disk now. Modules loaded since the last check join the baseline first."""
        self._add_loaded()
        changed: list[str] = []
        for path, before in list(self._stamps.items()):
            try:
                now = stamp(path)
            except OSError:
                # Unreadable right now. The previous stamp stays, so this is not a change.
                continue
            if now != before:
                changed.append(path)
        names = tuple(sorted(display_name(path) for path in changed))
        return Restart(code=names, server=self._server_stale())
