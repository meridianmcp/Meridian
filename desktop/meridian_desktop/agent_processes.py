"""Agent programs running on this computer, listed read-only. Pure Python apart from the Windows calls.

A process is an agent when its executable is named claude or codex, with or without .exe, in any case. Every such
process is listed, helpers included, so no process is hidden from the owner. Command lines are never read, so nothing
typed on a command line reaches a result.

Three reads, each read-only:
- snapshot_processes: the process ids, parent ids and executable names, from the Windows toolhelp snapshot.
- read_working_folder: the working folder of one 64-bit process, from that process's own parameters block. It needs
  PROCESS_QUERY_INFORMATION and PROCESS_VM_READ, and it writes nothing. It is "" when the folder cannot be read: a
  32-bit process, a process this user cannot read, a process that has gone, or any non-Windows platform.
- restart_check.process_started_ns: the start time of one process, the reader the restart check already uses.

Nothing here stops, suspends, signals or writes to a process. A failure becomes a fixed text result, so a raw
exception never reaches the window.
"""

from __future__ import annotations

import ntpath
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from meridian_desktop.restart_check import GONE, ProcessGone, process_started_ns

# The executable names that are agent programs, without .exe and in lower case, and the label each one shows.
AGENT_PROGRAMS = {"claude": "Claude", "codex": "Codex"}
STATE_OK = "ok"
STATE_UNSUPPORTED = "unsupported"
STATE_ERROR = "error"
UNSUPPORTED_TEXT = "Listing agent programs is only available on Windows for now."
READ_FAILED_TEXT = "The running programs could not be listed. Try Refresh again in a moment."
# The text the window shows in a cell that has no value: the folder could not be read, or no known folder holds it.
NOT_READ_TEXT = "Not read"
NO_MATCH_TEXT = "No match"

_EXE_SUFFIX = ".exe"
_PATH_SEPARATOR = "\\"
_TH32CS_SNAPPROCESS = 0x00000002
_PROCESS_QUERY_INFORMATION = 0x0400
_PROCESS_VM_READ = 0x0010
_PROCESS_BASIC_INFORMATION = 0  # the NtQueryInformationProcess class that returns the PEB address
# Offsets for a 64-bit process. A 32-bit process is not read.
_PEB_PROCESS_PARAMETERS = 0x20  # PEB.ProcessParameters
_PARAMS_CURRENT_DIRECTORY = 0x38  # RTL_USER_PROCESS_PARAMETERS.CurrentDirectory, a CURDIR
_CURDIR_LENGTH = slice(0, 2)  # its DosPath UNICODE_STRING: Length, then Buffer at offset 8
_CURDIR_BUFFER = slice(8, 16)
_MAX_FOLDER_CHARS = 1024


@dataclass(frozen=True)
class ProcessEntry:
    """One process from the snapshot: its id, its parent's id, and its executable name as Windows reports it."""

    pid: int
    parent_pid: int
    exe_name: str


@dataclass(frozen=True)
class AgentProcess:
    """One agent program. ``started_ns`` is None when the start time cannot be read. ``folder`` is "" when the working
    folder cannot be read. ``project`` is "" when no known project folder holds that folder."""

    program: str
    pid: int
    started_ns: int | None
    folder: str
    project: str


@dataclass(frozen=True)
class AgentListing:
    """The outcome of one listing. ``state`` is STATE_OK, STATE_UNSUPPORTED or STATE_ERROR. ``message`` is fixed text
    for the two failure states, and ``agents`` is set only for STATE_OK."""

    state: str
    message: str = ""
    agents: tuple[AgentProcess, ...] = ()


# ----------------------------------------------------------------- pure helpers


def program_key(exe_name: str) -> str:
    """The executable name in lower case without .exe, so "Claude.exe" and "claude" compare equal."""
    text = exe_name.strip().lower()
    if text.endswith(_EXE_SUFFIX):
        text = text[: -len(_EXE_SUFFIX)]
    return text


def find_agents(entries: Sequence[ProcessEntry]) -> tuple[tuple[ProcessEntry, str], ...]:
    """The agent processes among ``entries``, as (entry, label) pairs in process id order.

    Every process named as an agent program is kept, helpers included, so no process is hidden from the owner.
    """
    found: list[tuple[ProcessEntry, str]] = []
    for entry in sorted(entries, key=lambda item: item.pid):
        label = AGENT_PROGRAMS.get(program_key(entry.exe_name))
        if label:
            found.append((entry, label))
    return tuple(found)


def _norm(path: str) -> str:
    """A Windows path in one form for comparison: case folded, slashes unified, no trailing separator."""
    text = path.strip()
    if not text:
        return ""
    return ntpath.normcase(ntpath.normpath(text)).rstrip(_PATH_SEPARATOR)


def project_for_folder(folder: str, folders: Mapping[str, str]) -> str:
    """The name of the known project whose folder holds ``folder``, or "" when none does.

    ``folders`` maps a project name to its folder. The longest matching folder wins, so a folder nested in another
    names its own project. Case and slash direction are ignored, as Windows ignores them. A folder that only shares a
    name prefix does not match: a folder named app2 is not inside a folder named app.
    """
    target = _norm(folder)
    if not target:
        return ""
    best = ""
    best_len = -1
    for project, root in folders.items():
        base = _norm(root)
        if not base:
            continue
        inside = target == base or target.startswith(base + _PATH_SEPARATOR)
        if inside and len(base) > best_len:
            best, best_len = project, len(base)
    return best


# ----------------------------------------------------------------- Windows reads


def snapshot_processes() -> tuple[ProcessEntry, ...]:
    """Every process on this computer from the Windows toolhelp snapshot. Read-only.

    Raises OSError when the platform is not Windows or the snapshot cannot be taken.
    """
    if sys.platform != "win32":
        raise OSError("process snapshots are read on Windows only")
    import ctypes
    from ctypes import wintypes

    class ProcessEntry32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry32W)]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry32W)]
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    snapshot = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == ctypes.c_void_p(-1).value:
        raise OSError("the process snapshot could not be taken")
    entries: list[ProcessEntry] = []
    try:
        entry = ProcessEntry32W()
        entry.dwSize = ctypes.sizeof(entry)
        more = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            entries.append(ProcessEntry(int(entry.th32ProcessID), int(entry.th32ParentProcessID), entry.szExeFile))
            more = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return tuple(entries)


def read_working_folder(pid: int) -> str:
    """The working folder of one process, or "" when it cannot be read. Only memory is read, never written.

    The folder is read only for a 64-bit process on a 64-bit Python, because the offsets are those of a 64-bit process
    block. Any failure, including a process that has exited, gives "".
    """
    if sys.platform != "win32" or sys.maxsize <= 2**32:
        return ""
    try:
        return _working_folder_windows(pid)
    except Exception:  # noqa: BLE001 - an unreadable process is "" and never a raw exception
        return ""


def _working_folder_windows(pid: int) -> str:
    import ctypes
    from ctypes import wintypes

    class ProcessBasicInformation(ctypes.Structure):
        _fields_ = [
            ("Reserved1", ctypes.c_void_p),
            ("PebBaseAddress", ctypes.c_void_p),
            ("Reserved2", ctypes.c_void_p * 2),
            ("UniqueProcessId", ctypes.c_void_p),
            ("Reserved3", ctypes.c_void_p),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll")
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.IsWow64Process.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
    kernel32.IsWow64Process.restype = wintypes.BOOL
    kernel32.ReadProcessMemory.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    kernel32.ReadProcessMemory.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    ntdll.NtQueryInformationProcess.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.ULONG,
        ctypes.POINTER(wintypes.ULONG),
    ]
    ntdll.NtQueryInformationProcess.restype = ctypes.c_long  # NTSTATUS: 0 is success

    handle = kernel32.OpenProcess(_PROCESS_QUERY_INFORMATION | _PROCESS_VM_READ, False, pid)
    if not handle:
        return ""
    try:
        is_wow64 = wintypes.BOOL()
        if not kernel32.IsWow64Process(handle, ctypes.byref(is_wow64)) or is_wow64.value:
            return ""

        def read(address: int, size: int) -> bytes:
            buffer = ctypes.create_string_buffer(size)
            count = ctypes.c_size_t(0)
            ok = kernel32.ReadProcessMemory(
                handle, address, ctypes.cast(buffer, ctypes.c_void_p), size, ctypes.byref(count)
            )
            return buffer.raw[: count.value] if ok and count.value == size else b""

        info = ProcessBasicInformation()
        returned = wintypes.ULONG(0)
        status = ntdll.NtQueryInformationProcess(
            handle, _PROCESS_BASIC_INFORMATION, ctypes.byref(info), ctypes.sizeof(info), ctypes.byref(returned)
        )
        peb = info.PebBaseAddress or 0
        if status != 0 or not peb:
            return ""
        params = int.from_bytes(read(peb + _PEB_PROCESS_PARAMETERS, 8), "little")
        if not params:
            return ""
        current = read(params + _PARAMS_CURRENT_DIRECTORY, 16)
        if len(current) != 16:
            return ""
        length = int.from_bytes(current[_CURDIR_LENGTH], "little")
        text_address = int.from_bytes(current[_CURDIR_BUFFER], "little")
        if length < 2 or length % 2 or length > 2 * _MAX_FOLDER_CHARS or not text_address:
            return ""
        raw = read(text_address, length)
        if len(raw) != length:
            return ""
        text = raw.decode("utf-16-le")
        # Windows keeps a trailing separator on the folder. It is dropped, except on a drive root such as C:\.
        if len(text) > 3 and text.endswith(_PATH_SEPARATOR):
            text = text[:-1]
        return text
    finally:
        kernel32.CloseHandle(handle)


# ----------------------------------------------------------------- one listing


def _started(started_of: Callable[[int], int | None | ProcessGone], pid: int) -> int | None | ProcessGone:
    try:
        return started_of(pid)
    except Exception:  # noqa: BLE001 - an unreadable start time is None, never a raw exception
        return None


def _folder(folder_of: Callable[[int], str], pid: int) -> str:
    try:
        found = folder_of(pid)
    except Exception:  # noqa: BLE001 - an unreadable folder is "", never a raw exception
        return ""
    return found if isinstance(found, str) else ""


def list_agent_programs(
    folders: Mapping[str, str],
    *,
    supported: bool | None = None,
    snapshot: Callable[[], Sequence[ProcessEntry]] = snapshot_processes,
    folder_of: Callable[[int], str] = read_working_folder,
    started_of: Callable[[int], int | None | ProcessGone] = process_started_ns,
) -> AgentListing:
    """List the agent programs running now, each with its working folder, start time and project. Read-only.

    ``folders`` maps a project name to its known folder. A process that exits between the snapshot and its start-time
    read is left out. A working folder or start time that cannot be read is "" or None, and the row still shows. The
    three reads are keyword arguments, so a test can give fake process data. ``supported`` defaults to Windows only.
    """
    is_supported = sys.platform == "win32" if supported is None else supported
    if not is_supported:
        return AgentListing(STATE_UNSUPPORTED, UNSUPPORTED_TEXT)
    try:
        entries = list(snapshot())
    except Exception:  # noqa: BLE001 - a raw exception never reaches the window
        return AgentListing(STATE_ERROR, READ_FAILED_TEXT)
    agents: list[AgentProcess] = []
    for entry, label in find_agents(entries):
        started = _started(started_of, entry.pid)
        if started is GONE:
            continue  # the process exited after the snapshot
        folder = _folder(folder_of, entry.pid)
        agents.append(
            AgentProcess(
                program=label,
                pid=entry.pid,
                started_ns=started if isinstance(started, int) else None,
                folder=folder,
                project=project_for_folder(folder, folders),
            )
        )
    return AgentListing(STATE_OK, "", tuple(agents))


__all__ = [
    "AGENT_PROGRAMS",
    "NOT_READ_TEXT",
    "NO_MATCH_TEXT",
    "READ_FAILED_TEXT",
    "STATE_ERROR",
    "STATE_OK",
    "STATE_UNSUPPORTED",
    "UNSUPPORTED_TEXT",
    "AgentListing",
    "AgentProcess",
    "ProcessEntry",
    "find_agents",
    "list_agent_programs",
    "program_key",
    "project_for_folder",
    "read_working_folder",
    "snapshot_processes",
]
