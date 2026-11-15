"""Set a Windows user environment variable, the setx equivalent. Pure Python: no Qt import.

The value is written to HKCU\\Environment as REG_SZ, the key that setx and the Windows Environment Variables dialog write
to. Then a WM_SETTINGCHANGE message is broadcast with "Environment" as its text, so programs that listen for a change of
the environment (Explorer, a newly started terminal) read it again. A program that is already running keeps the values it
started with, so the owner is told to restart it.

``winreg`` and ``ctypes`` are imported inside the methods, not at module import. A backend can be injected for tests, so
no test writes to the real registry.

Errors name the operation and the exception class only. The value is never put in a message, and the exception is not
chained, so no traceback carries it.
"""

from __future__ import annotations

import re
from typing import Protocol

ENVIRONMENT_SUBKEY = "Environment"
HWND_BROADCAST = 0xFFFF
WM_SETTINGCHANGE = 0x001A
SMTO_ABORTIFHUNG = 0x0002
BROADCAST_TIMEOUT_MS = 5000

# Variables that Windows, the shell or Python depend on. A user value under one of these names would change how every
# program starts, so the name is refused. Windows compares names without regard to case.
RESERVED_NAMES = frozenset(
    {
        "ALLUSERSPROFILE",
        "APPDATA",
        "COMMONPROGRAMFILES",
        "COMPUTERNAME",
        "COMSPEC",
        "HOME",
        "HOMEDRIVE",
        "HOMEPATH",
        "LOCALAPPDATA",
        "LOGONSERVER",
        "NUMBER_OF_PROCESSORS",
        "OS",
        "PATH",
        "PATHEXT",
        "PROCESSOR_ARCHITECTURE",
        "PROCESSOR_IDENTIFIER",
        "PROCESSOR_LEVEL",
        "PROCESSOR_REVISION",
        "PROGRAMDATA",
        "PROGRAMFILES",
        "PROMPT",
        "PSMODULEPATH",
        "PUBLIC",
        "PYTHONHOME",
        "PYTHONPATH",
        "SESSIONNAME",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "USERDOMAIN",
        "USERNAME",
        "USERPROFILE",
        "WINDIR",
    }
)
_VARIABLE_SHAPE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")


class UserEnvironmentError(Exception):
    """A variable could not be written. The message names the operation and the exception class only."""


class RegistryBackend(Protocol):
    """What the writer needs. WindowsRegistry is the real one."""

    def write_user_value(self, name: str, value: str) -> None: ...

    def announce_change(self) -> bool: ...


def variable_name_problem(name: str) -> str | None:
    """Plain words for why ``name`` cannot be a user variable, or None when it can."""
    if not _VARIABLE_SHAPE.fullmatch(name):
        return "The variable name may use letters, digits and underscores, and it cannot start with a digit."
    if name.upper() in RESERVED_NAMES:
        return f"{name} is a Windows variable that other programs need. Rename the key so its variable is different."
    return None


class WindowsRegistry:
    """The real backend: HKCU\\Environment through winreg, and the broadcast through user32."""

    def write_user_value(self, name: str, value: str) -> None:
        import winreg  # lazy: only Windows has it

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, ENVIRONMENT_SUBKEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)

    def announce_change(self) -> bool:
        import ctypes  # lazy: ctypes.windll exists only on Windows
        from ctypes import wintypes

        result = wintypes.DWORD()
        # The text sent is the section name only. The value is never part of the message.
        sent = ctypes.windll.user32.SendMessageTimeoutW(
            HWND_BROADCAST,
            WM_SETTINGCHANGE,
            0,
            ENVIRONMENT_SUBKEY,
            SMTO_ABORTIFHUNG,
            BROADCAST_TIMEOUT_MS,
            ctypes.byref(result),
        )
        return bool(sent)


def _call(operation: str, fn, *args):
    """Run one backend call. Any failure becomes UserEnvironmentError with the class name only, and no context."""
    try:
        return fn(*args)
    except Exception as exc:  # noqa: BLE001 - every backend failure is reported by class only
        kind = exc.__class__.__name__
    raise UserEnvironmentError(f"user environment {operation} failed ({kind})") from None


class UserEnvironment:
    """Write one user variable, then announce the change. The backend is injectable."""

    def __init__(self, backend: RegistryBackend | None = None) -> None:
        self._backend = backend

    def _module(self) -> RegistryBackend:
        return self._backend if self._backend is not None else WindowsRegistry()

    def set(self, name: str, value: str) -> bool:
        """Write ``name`` = ``value`` for this user. Return True when Windows confirmed the change to running programs.

        The name is checked before anything is written, so a refused name touches nothing. The write happens first. A
        failed announcement does not undo the write, so it is reported as False and the value stays.
        """
        problem = variable_name_problem(name)
        if problem is not None:
            raise UserEnvironmentError(problem)
        if not isinstance(value, str) or not value.strip():
            raise UserEnvironmentError("The value is empty.")
        if "\x00" in value:
            raise UserEnvironmentError("The value holds a character that Windows cannot store.")
        backend = self._module()
        _call("write", backend.write_user_value, name, value)
        try:
            return bool(_call("announce", backend.announce_change))
        except UserEnvironmentError:
            return False


__all__ = [
    "RESERVED_NAMES",
    "UserEnvironment",
    "UserEnvironmentError",
    "WindowsRegistry",
    "variable_name_problem",
]
