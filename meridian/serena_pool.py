"""Serena daemon pool — one Serena HTTP instance per repo_path (64650cb4).

The tunnel's code-extractor slot runs Serena (LSP symbol tools). Historically a
single Serena was started with one ``--project <repo_path>``, so an executor
session that reached into a *different* repo (e.g. a meridian-build session
calling ``find_declaration`` on Masters_Thesis) hit Serena's "outside configured
workspaces" error.

This module replaces that single instance with a **pool keyed by repo_path**.
Each distinct repo gets its own Serena process started on demand with
``--transport streamable-http --port <N> --project <repo_path>``; the tunnel
relay routes each request to the daemon matching the caller's repo_path (carried
in the ``X-Meridian-Repo-Path`` request header, falling back to the tunnel's
default repo). Daemons idle longer than :data:`IDLE_KILL_SECONDS` are reaped.

The pool is pure-Python and fully unit-testable: process spawning and the clock
are injected (``spawn`` / ``now``) so tests never start a real Serena.
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any, Callable

# Routing header: the executor's MCP client sets this to the repo it is working
# in; the server forwards it and the tunnel relay uses it to pick the daemon.
REPO_PATH_HEADER = "x-meridian-repo-path"

# Serena HTTP daemons are allocated sequential ports starting here.
#
# a1a870d5 (2026-07-19) — this was 8820, IDENTICAL to
# ``tunnel_plugins.DEFAULT_OUTPUTS_PORT``. The original comment's intent
# ("chosen above the office-plugin ports 8811-8813 so it never collides with a
# fixed slot") broke silently as later work grew that fixed range upward:
# meridian-docs (8818), zotero (8819), outputs (8820), and debug (8821) each
# claimed "the next free port above the current max" in turn, and 8820
# eventually landed exactly on this pool's base port. Picking anything just
# above the fixed range is fragile for the same reason — a future slot can
# repeat the pattern.
#
# 8700 instead sits BELOW ``tunnel_plugins.DEFAULT_FS_PORT`` (8808, the lowest
# fixed port the tunnel-plugin catalog declares), with 100+ ports of headroom
# for this pool's sequential per-repo allocation (:meth:`SerenaDaemonPool.
# _next_port`). Every built-in slot added since the catalog's inception has
# grown the fixed range upward from 8808, never downward, so this range does
# not need to be re-bumped every time a new built-in slot is added — unlike
# ``tunnel_plugins._CUSTOM_PORT_START``, which historically has been bumped
# repeatedly for exactly that reason (see its own comment history). See
# :func:`scripts.tunnel_smoke_test.check_port_collisions` for the permanent
# regression check across every declared port in the codebase.
SERENA_POOL_BASE_PORT = 8700

# A daemon untouched for this long is killed by :meth:`SerenaDaemonPool.reap_idle`.
IDLE_KILL_SECONDS = 30 * 60  # 30 minutes


def build_serena_command(repo_path: str, port: int) -> list[str]:
    """Serena HTTP launch command for one repo on one port.

    Uses ``--transport streamable-http --port`` so Serena serves HTTP directly
    (no mcp-proxy bridge needed) and ``--project`` to scope it to ``repo_path``.
    Mirrors the flags in :data:`meridian.tunnel_plugins.SERENA_EXTRACT_COMMAND`
    (headless, claude-code context) plus the per-instance transport/port.
    """
    return [
        "uvx", "--from", "serena-agent", "serena", "start-mcp-server",
        "--context", "claude-code",
        "--open-web-dashboard", "false",
        "--transport", "streamable-http",
        "--port", str(port),
        "--project", repo_path,
    ]


def resolve_repo_path(headers: dict[str, Any] | None, default_repo_path: str) -> str:
    """Pick the repo_path for a request from its headers, else the tunnel default.

    Header lookup is case-insensitive (HTTP header casing is not preserved
    uniformly across the relay). A blank/missing header falls back to
    ``default_repo_path`` so existing single-repo callers keep working.
    """
    if headers:
        for key, value in headers.items():
            if str(key).lower() == REPO_PATH_HEADER:
                candidate = str(value or "").strip()
                if candidate:
                    return candidate
    return default_repo_path


class SerenaDaemon:
    """One Serena process serving a single repo over streamable-http."""

    __slots__ = ("repo_path", "port", "proc", "last_used")

    def __init__(self, repo_path: str, port: int, proc: Any, last_used: float):
        self.repo_path = repo_path
        self.port = port
        self.proc = proc
        self.last_used = last_used

    @property
    def is_alive(self) -> bool:
        """True if the underlying process is still running."""
        if self.proc is None:
            return False
        try:
            return self.proc.poll() is None
        except Exception:  # noqa: BLE001
            return False

    def touch(self, now: float) -> None:
        """Mark the daemon as just-used (resets its idle timer)."""
        self.last_used = now

    def idle_seconds(self, now: float) -> float:
        """Seconds since the daemon last served a request."""
        return max(0.0, now - self.last_used)


class SerenaDaemonPool:
    """A pool of Serena daemons keyed by normalized repo_path.

    Daemons are spawned lazily by :meth:`get_or_spawn` and torn down either by
    :meth:`reap_idle` (idle TTL) or :meth:`shutdown` (tunnel exit). ``spawn`` and
    ``now`` are injectable for tests; in production they default to
    ``subprocess.Popen`` and ``time.monotonic``.
    """

    def __init__(
        self,
        *,
        default_repo_path: str | None = None,
        base_port: int = SERENA_POOL_BASE_PORT,
        idle_kill_seconds: float = IDLE_KILL_SECONDS,
        command_builder: Callable[[str, int], list[str]] = build_serena_command,
        spawn: Callable[[list[str]], Any] | None = None,
        now: Callable[[], float] = time.monotonic,
        terminate: Callable[[Any], None] | None = None,
    ) -> None:
        self.default_repo_path = (
            self._normalize(default_repo_path) if default_repo_path else None
        )
        self.base_port = base_port
        self.idle_kill_seconds = idle_kill_seconds
        self._command_builder = command_builder
        self._spawn = spawn if spawn is not None else (lambda cmd: subprocess.Popen(cmd))
        self._now = now
        self._terminate = terminate if terminate is not None else _default_terminate
        self._daemons: dict[str, SerenaDaemon] = {}

    @staticmethod
    def _normalize(repo_path: str) -> str:
        """Canonicalize a repo_path so two spellings map to one daemon."""
        try:
            return str(Path(repo_path).expanduser().resolve())
        except Exception:  # noqa: BLE001 — non-filesystem-ish key, use as-is
            return str(repo_path).strip()

    def _next_port(self) -> int:
        """Lowest base_port+offset not currently bound by a live daemon."""
        used = {d.port for d in self._daemons.values()}
        port = self.base_port
        while port in used:
            port += 1
        return port

    def get_or_spawn(self, repo_path: str) -> SerenaDaemon:
        """Return the live daemon for ``repo_path``, spawning one if needed.

        Re-spawns transparently if a previously-registered daemon has died. The
        returned daemon's idle timer is reset.
        """
        key = self._normalize(repo_path)
        now = self._now()
        existing = self._daemons.get(key)
        if existing is not None and existing.is_alive:
            existing.touch(now)
            return existing
        # Missing or dead → spawn fresh (reuse the dead daemon's port if any).
        port = existing.port if existing is not None else self._next_port()
        cmd = self._command_builder(key, port)
        proc = self._spawn(cmd)
        daemon = SerenaDaemon(key, port, proc, now)
        self._daemons[key] = daemon
        return daemon

    def daemon_for(self, repo_path: str) -> SerenaDaemon | None:
        """Return the registered daemon for ``repo_path`` without spawning."""
        return self._daemons.get(self._normalize(repo_path))

    def reap_idle(self, now: float | None = None) -> list[str]:
        """Terminate daemons idle longer than the TTL. Returns reaped repo_paths."""
        when = self._now() if now is None else now
        reaped: list[str] = []
        for key, daemon in list(self._daemons.items()):
            if daemon.idle_seconds(when) >= self.idle_kill_seconds:
                self._terminate(daemon.proc)
                del self._daemons[key]
                reaped.append(key)
        return reaped

    def shutdown(self) -> None:
        """Terminate every daemon (tunnel exit)."""
        for daemon in list(self._daemons.values()):
            self._terminate(daemon.proc)
        self._daemons.clear()

    def repo_paths(self) -> list[str]:
        """Normalized repo_paths with a registered daemon."""
        return list(self._daemons.keys())

    def __len__(self) -> int:
        return len(self._daemons)


def _default_terminate(proc: Any) -> None:
    """Best-effort terminate→kill of a daemon process; never raises."""
    if proc is None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
