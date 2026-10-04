"""Platform-correct, non-signalling process liveness probes.

On Windows, ``os.kill(pid, 0)`` is not a harmless existence check: Python
maps signal zero to ``GenerateConsoleCtrlEvent(CTRL_C_EVENT, pid)``. Use
``OpenProcess``/``GetExitCodeProcess`` there so liveness checks cannot send a
console control event to the caller or another attached process group.
"""

from __future__ import annotations

import os
import sys
from typing import Any, Callable

_ERROR_INVALID_PARAMETER = 87
_ERROR_ACCESS_DENIED = 5
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259


class _Win32ProcessProbe:
    """Injectable wrapper around the three kernel32 calls needed here."""

    def __init__(self, kernel32: Any):
        self._kernel32 = kernel32

    def open_process(self, pid: int) -> int | None:
        handle = self._kernel32.OpenProcess(
            _PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid)
        )
        return int(handle) if handle else None

    def get_exit_code(self, handle: int) -> int | None:
        import ctypes  # noqa: PLC0415 -- Windows-only

        code = ctypes.c_uint32(0)
        ok = self._kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        return int(code.value) if ok else None

    def get_last_error(self) -> int:
        return int(self._kernel32.GetLastError())

    def close_handle(self, handle: int) -> bool:
        return bool(self._kernel32.CloseHandle(handle))


def _load_win32_process_probe() -> _Win32ProcessProbe | None:
    """Load a properly-prototyped kernel32 probe, or report it unavailable."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes  # noqa: PLC0415 -- Windows-only

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        kernel32.GetExitCodeProcess.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        kernel32.GetLastError.argtypes = []
        kernel32.GetLastError.restype = ctypes.c_uint32
        return _Win32ProcessProbe(kernel32)
    except Exception:  # noqa: BLE001 -- inability to probe must fail safe
        return None


def _win32_pid_is_alive(
    pid: int,
    probe_loader: Callable[[], _Win32ProcessProbe | None] | None = None,
) -> bool:
    """Return false only when Windows confirms that the PID has exited."""
    try:
        probe = (probe_loader or _load_win32_process_probe)()
    except Exception:  # noqa: BLE001
        probe = None
    if probe is None:
        return True

    try:
        handle = probe.open_process(pid)
    except Exception:  # noqa: BLE001
        return True
    if handle:
        try:
            exit_code = probe.get_exit_code(handle)
        except Exception:  # noqa: BLE001
            exit_code = None
        try:
            probe.close_handle(handle)
        except Exception:  # noqa: BLE001
            pass
        return exit_code is None or exit_code == _STILL_ACTIVE

    try:
        error = probe.get_last_error()
    except Exception:  # noqa: BLE001
        return True
    if error == _ERROR_INVALID_PARAMETER:
        return False
    if error == _ERROR_ACCESS_DENIED:
        return True
    return True


def pid_is_alive(
    pid: int,
    *,
    platform: str | None = None,
    win32_probe_loader: Callable[[], _Win32ProcessProbe | None] | None = None,
) -> bool:
    """Check a PID without sending signals on Windows.

    Unknown Windows probe failures are treated as alive so callers that gate
    cleanup do not delete resources owned by a process they could not inspect.
    POSIX keeps the conventional signal-zero existence probe.
    """
    if pid <= 0:
        return False
    current_platform = platform or sys.platform
    if current_platform == "win32":
        return _win32_pid_is_alive(pid, probe_loader=win32_probe_loader)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True
