"""Minimal, mockable SSH command execution for the durable Remote Task
primitive v1 (W1-E, item 32d3d5de).

This is the ONE integration point with the real ``ssh`` binary in the whole
feature. :mod:`meridian.db.remote_tasks` never calls a subprocess directly --
it always goes through an ``ssh_runner`` callable matching
:func:`run_ssh_command`'s signature, defaulting to :func:`run_ssh_command`
itself but overridable per-call. That is what lets
``tests/test_remote_tasks.py`` exercise the full launch/status state machine
(all five status outcomes) via a fake runner, with no real SSH server
involved -- exactly what the sprint item asks for.

WINDOWS / SELECTOREVENTLOOP NOTE (read before "simplifying" this to
``asyncio.create_subprocess_exec``)
--------------------------------------------------------------------------
``meridian/__main__.py`` deliberately forces ``SelectorEventLoop`` on Windows
for both ``--mcp`` and server modes (psycopg3 requires it; see that module's
own comment block). ``SelectorEventLoop`` does NOT implement asyncio's
subprocess transport on Windows at all -- ``asyncio.create_subprocess_exec``/
``_shell`` raise a bare ``NotImplementedError`` there (this is the exact,
already-documented reason ``--tunnel`` mode is carved out of that
SelectorEventLoop override in ``__main__.py``). A self-hosted Windows
executor session is a completely ordinary way to run this MCP server, so
this module cannot assume asyncio subprocess support is available.

The fix is not "use ProactorEventLoop" (that would break psycopg3 for the
whole process) -- it's to never put a real subprocess on THIS event loop at
all. :func:`run_ssh_command` therefore runs a plain, synchronous
``subprocess.run`` inside a worker thread via ``asyncio.to_thread``: the OS
still manages the child process exactly as it would for any blocking
subprocess call, and none of it touches asyncio's subprocess transport, so
it works identically under Selector and Proactor loops (and on Linux/macOS,
where none of this applies but the thread hop is harmless).
"""
from __future__ import annotations

import asyncio
import subprocess
from dataclasses import dataclass
from typing import Protocol

DEFAULT_LAUNCH_TIMEOUT_SECONDS = 15.0
DEFAULT_STATUS_TIMEOUT_SECONDS = 15.0

# BatchMode=yes: never block on an interactive password/passphrase prompt --
#   fail fast instead if key-based auth doesn't just work.
# ConnectTimeout=10: bound the TCP+handshake phase specifically, separate
#   from the overall per-call timeout enforced in Python below.
# StrictHostKeyChecking=accept-new: silently trust a NEW host key (no
#   interactive prompt this automation could never answer) while still
#   REJECTING a CHANGED key for an already-known host -- MITM detection is
#   kept, the one-time "is this really the host" prompt is not.
DEFAULT_SSH_OPTIONS: tuple[str, ...] = (
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=10",
    "-o", "StrictHostKeyChecking=accept-new",
)

# ssh(1): "the ssh client itself" (as opposed to the remote command) exiting
# with 255 is documented to mean connection/setup failed before the remote
# command ever ran -- distinct from the remote command's own exit code
# (which can legitimately BE 255, but that's ssh passing through a real
# remote result, not ssh's own failure code).
_SSH_CONNECT_FAILURE_RETURNCODE = 255


@dataclass(frozen=True)
class SSHResult:
    """Outcome of one short-lived SSH command execution.

    ``connected`` is the load-bearing field callers branch on: True means
    the remote command actually ran (whatever its own ``returncode`` is --
    a nonzero remote command result is still a successful SSH round trip);
    False means the CONNECTION itself never usably completed (unreachable
    host, auth failure, DNS failure, local timeout, or ``ssh`` missing) --
    ``returncode``/``stdout``/``stderr`` may be empty/None in that case, and
    ``error`` names why.
    """

    connected: bool
    returncode: "int | None"
    stdout: str
    stderr: str
    error: "str | None" = None


class SSHRunner(Protocol):
    """Shape every ``ssh_runner`` argument throughout
    :mod:`meridian.db.remote_tasks` must match -- documents the injectable
    seam tests substitute a fake implementation into."""

    async def __call__(
        self, host: str, remote_command: str, *, timeout: float
    ) -> SSHResult: ...


async def run_ssh_command(
    host: str, remote_command: str, *, timeout: float = DEFAULT_LAUNCH_TIMEOUT_SECONDS
) -> SSHResult:
    """Run ``remote_command`` on ``host`` over a fresh, short-lived SSH
    connection and return its outcome. Never raises for an ordinary
    connection/timeout failure -- those are reported via
    ``SSHResult(connected=False, ...)`` so callers never need a try/except
    around this to implement the "connection-lost" status branch.

    See the module docstring for why this runs ``subprocess.run`` in a
    worker thread rather than using asyncio's own subprocess transport.
    """

    def _blocking_run() -> SSHResult:
        argv = ["ssh", *DEFAULT_SSH_OPTIONS, host, remote_command]
        try:
            completed = subprocess.run(  # noqa: S603 -- argv list, no shell=True
                argv, capture_output=True, timeout=timeout, text=True,
            )
        except subprocess.TimeoutExpired:
            return SSHResult(
                connected=False, returncode=None, stdout="", stderr="",
                error="timeout",
            )
        except FileNotFoundError as exc:
            return SSHResult(
                connected=False, returncode=None, stdout="", stderr="",
                error=f"ssh binary not found: {exc}",
            )
        except OSError as exc:  # pragma: no cover -- platform-dependent spawn failure
            return SSHResult(
                connected=False, returncode=None, stdout="", stderr="",
                error=f"failed to spawn ssh: {exc}",
            )
        if completed.returncode == _SSH_CONNECT_FAILURE_RETURNCODE:
            return SSHResult(
                connected=False, returncode=completed.returncode,
                stdout=completed.stdout or "", stderr=completed.stderr or "",
                error="ssh_connect_failed",
            )
        return SSHResult(
            connected=True, returncode=completed.returncode,
            stdout=completed.stdout or "", stderr=completed.stderr or "",
        )

    return await asyncio.to_thread(_blocking_run)
