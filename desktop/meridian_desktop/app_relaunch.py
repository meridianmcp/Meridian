"""Start a new copy of this desktop app so it runs the code now on disk. Pure Python: no Qt import.

The new copy starts first. The caller closes this window only when that start succeeded, so a failed start never leaves
the owner with no app. A frozen build relaunches its own executable; a source run relaunches the package with the same
arguments from the folder that holds it.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

FAILED_START_TEXT = "The new copy could not start ({}). This window stays open."


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
) -> str | None:
    """Start the new copy detached from this window. Returns None on success, or the failure text with only the error
    class name, so no path or message from the system is shown."""
    flags = 0
    if platform == "win32":
        flags = getattr(subprocess, "DETACHED_PROCESS", 0x00000008) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200
        )
    try:
        popen(list(plan.argv), cwd=plan.cwd, creationflags=flags, close_fds=True)
    except OSError as exc:
        return FAILED_START_TEXT.format(exc.__class__.__name__)
    return None


__all__ = ["FAILED_START_TEXT", "RelaunchPlan", "package_parent", "relaunch_plan", "start_new_copy"]
