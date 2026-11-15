"""Start a new copy of this desktop app so it runs the code now on disk. Pure Python: no Qt import.

The new copy starts first. The caller closes this window only after two checks pass: the operating system started the
process, and StartupWatch then saw that process still running STARTUP_HOLD_SECONDS later. Only "the process has not
exited" is checked. Nothing checks that the new copy's window opened or that it works. A copy that exits while starting
(an import error, for example) keeps this window open, and the owner sees its exit code, never a path. A frozen build
relaunches its own executable; a source run relaunches the package with the same arguments from the folder that holds it.
"""

from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

FAILED_START_TEXT = "The new copy could not start ({}). This window stays open."
EXITED_TEXT = "The new copy stopped while starting (exit code {}). This window stays open."
# How long the new copy must stay running before this window closes, and how often the caller polls it.
STARTUP_HOLD_SECONDS = 3.0
POLL_INTERVAL_MS = 250

RUNNING = "running"
ALIVE = "alive"
EXITED = "exited"


@dataclass(frozen=True)
class RelaunchPlan:
    """The command for the new copy, and the folder to start it in."""

    argv: tuple[str, ...]
    cwd: str


def package_parent() -> str:
    """The folder that holds the meridian_desktop package, so a source run can import it."""
    return str(Path(__file__).resolve().parents[1])


def relaunch_plan(frozen: bool, executable: str, argv: Sequence[str], folder: str) -> RelaunchPlan:
    """A frozen build runs its own executable again. A source run runs the package with the same arguments."""
    if frozen:
        return RelaunchPlan((executable, *argv), folder)
    return RelaunchPlan((executable, "-m", "meridian_desktop", *argv), folder)


def start_new_copy(
    plan: RelaunchPlan,
    popen: Callable[..., Any] = subprocess.Popen,
    platform: str = sys.platform,
) -> tuple[Any, str | None]:
    """Start the new copy detached from this window. Returns (child, None) on success, or (None, failure text) with only
    the error class name, so no path or message from the system is shown. The child has poll(), as Popen does."""
    flags = 0
    if platform == "win32":
        flags = getattr(subprocess, "DETACHED_PROCESS", 0x00000008) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200
        )
    try:
        child = popen(list(plan.argv), cwd=plan.cwd, creationflags=flags, close_fds=True)
    except OSError as exc:
        return None, FAILED_START_TEXT.format(exc.__class__.__name__)
    return child, None


class StartupWatch:
    """Watches a started copy until it has stayed running for hold_seconds. The caller polls check() on a timer.

    check() returns (RUNNING, "") while the hold has not passed, (ALIVE, "") once the copy is still running after it, or
    (EXITED, text) as soon as the copy has exited. The text names the exit code only.
    """

    def __init__(
        self,
        child: Any,
        hold_seconds: float = STARTUP_HOLD_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._child = child
        self._clock = clock
        self._deadline = clock() + hold_seconds

    def check(self) -> tuple[str, str]:
        code = self._child.poll()
        if code is not None:
            return EXITED, EXITED_TEXT.format(int(code))
        if self._clock() >= self._deadline:
            return ALIVE, ""
        return RUNNING, ""


__all__ = [
    "ALIVE",
    "EXITED",
    "EXITED_TEXT",
    "FAILED_START_TEXT",
    "POLL_INTERVAL_MS",
    "RUNNING",
    "STARTUP_HOLD_SECONDS",
    "RelaunchPlan",
    "StartupWatch",
    "package_parent",
    "relaunch_plan",
    "start_new_copy",
]
