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

── Host-local broker (92aaedb7) ────────────────────────────────────────────

Each :class:`SerenaDaemonPool` instance lives inside ONE ``tunnel_client``
process. Two sibling sessions on the same machine (two separate ``meridian
--tunnel`` / ``--mcp`` invocations, e.g. two Claude Code windows) each get
their OWN pool object with no knowledge of each other — before this feature,
each would independently spawn its own Serena for the same repo_path, wasting
memory/CPU and defeating the whole point of a "shared" daemon.

Passing ``broker_dir`` (a filesystem path) turns on **host-local sharing**:

* Every spawned daemon gets a small **descriptor** file (port, pid, start
  time, config fingerprint) under ``<broker_dir>/<repo_key>/daemon.json``.
* Every pool instance currently depending on that daemon (the one that
  spawned it, or a sibling that adopted it) holds one **lease** file —
  ``<broker_dir>/<repo_key>/lease-<owner_id>.json`` — naming its own
  ``owner_id`` and OS pid. This is reference counting via the filesystem: a
  daemon is only ever terminated once its LAST live lease is released.
* :meth:`SerenaDaemonPool.get_or_spawn` tries to **adopt** an existing,
  config-matching, still-alive daemon (verified by OS pid, not a network
  probe — see the class docstring's "known limitations") before spawning a
  duplicate. A **config fingerprint** (see :func:`config_fingerprint`) blocks
  adoption across a config drift (different flags/version pin) — a mismatch
  always spawns fresh rather than silently reusing an incompatible instance.
* :meth:`SerenaDaemonPool.reap_idle` / :meth:`SerenaDaemonPool.shutdown`
  release this pool's own lease and only actually kill the process if no
  OTHER live lease remains — an idle-but-still-leased-by-a-sibling daemon is
  never pulled out from under it.
* A lease whose recording tunnel process has died (crashed without cleanup)
  is pruned lazily, by whichever pool next touches that repo_path's entry —
  this is the "crash cleanup" half of the feature.
* :func:`has_live_lease` is the piece :mod:`meridian.tunnel_client` calls
  from ``_kill_all_previously_spawned_pids`` (the once-per-startup orphan
  sweep) so that sweep does not kill a Serena daemon whose ORIGINAL spawning
  tunnel has exited but which a *different*, still-running sibling tunnel is
  currently leasing — without this, that once-per-startup sweep would kill a
  daemon still legitimately in use, which is exactly the kind of live-process
  kill this feature exists to prevent.
* :meth:`SerenaDaemonPool.diagnostics` exposes, per tracked daemon: repo_path,
  port, pid, whether THIS pool owns it (spawned it) vs. merely leases it
  (adopted from a sibling), owner_id, start_time, config_fingerprint, a
  ``health`` field, and whether it is currently quarantined.
* ``max_daemons`` bounds this pool's own memory/CPU footprint: when spawning
  a NEW (not adopted) daemon would exceed the cap, the least-recently-used
  daemon THIS pool owns is released first (an adopted/leased daemon is never
  evicted for this — it isn't this pool's resource to spend).

``broker_dir`` defaults to ``None`` (broker OFF): every existing caller and
every existing test that does not pass it keeps the original pure in-process,
zero-filesystem-I/O behaviour verbatim — host-local sharing is strictly
opt-in, so this is a purely additive, backward-compatible change.

Known limitations (documented per the sprint scope rather than left silent):

* Adoption verifies OS-pid liveness only, not an HTTP health probe of the
  Serena instance itself — a daemon that is alive-but-wedged can still be
  adopted. The tunnel relay's own per-request error handling is the existing
  backstop for a wedged backend; wiring an active probe into adoption is a
  reasonable follow-up.
* PID-liveness checks here are a plain "does this pid exist" test (see
  :func:`_default_pid_alive`), not the create_time-based PID-reuse hardening
  ``tunnel_client._is_slot_claimed_by_live_client`` uses for slot claims. On
  a long-uptime host a just-freed pid could theoretically be reassigned to an
  unrelated process before this module notices — a narrow window, and a
  caller (like ``tunnel_client``) can inject a stricter ``pid_alive``.
* Crash cleanup is lazy (triggered the next time some pool touches that
  repo_path's entry), not a background sweep — consistent with how
  ``orphan_reaper`` is hook-triggered rather than continuously running.
* Quarantine state lives in-memory, per pool instance — it is not broadcast
  to sibling pools on the same host. A broken shared daemon is independently
  avoided by each pool that notices it, rather than globally announced.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from . import process_budget as _process_budget

# Routing header: the executor's MCP client sets this to the repo it is working
# in; the server forwards it and the tunnel relay uses it to pick the daemon.
REPO_PATH_HEADER = "x-meridian-repo-path"

# e99b09e9 — local-machine opt-in escape hatch for Serena's web dashboard.
# Headless is the enforced default everywhere a Serena command is built or
# normalized (see :func:`ensure_serena_headless`); this env var is the ONE way
# to get the GUI back, and it is deliberately an environment variable rather
# than a `tunnel_plugins` config field: that config is project-shared and
# synced across every machine/tenant using it (see AGENTS.md's capability-
# manifest provenance rules — no machine-local settings belong there), so a
# "show the dashboard" toggle stored there would pop a browser tab on every
# OTHER machine sharing the config too. An env var is inherently local to the
# one machine that sets it, and — unlike a Claude Desktop / MCP config entry —
# it is never written into a generated config file (Meridian's local
# `.mcp.json` writer only ever emits `http://127.0.0.1:<port>` proxy URLs for
# the extract slot, never Serena's raw launch command).
SERENA_DASHBOARD_OPT_IN_ENV = "MERIDIAN_SERENA_DASHBOARD"


def _dashboard_opt_in() -> bool:
    """True when the local-machine dev escape hatch is set (see the env var
    docs above). Off by default — headless always wins unless explicitly
    opted out of on THIS machine."""
    return os.environ.get(SERENA_DASHBOARD_OPT_IN_ENV, "").strip().lower() in (
        "1", "true", "yes",
    )


def is_serena_command(cmd: Any) -> bool:
    """True if *cmd* looks like a Serena MCP server launch.

    Detected by the presence of both the ``serena-agent`` package marker and
    the ``start-mcp-server`` subcommand — the two tokens every Serena launch
    (the pool's own :func:`build_serena_command`, the tunnel-plugin
    registry's default, and any tenant-saved override, stale snapshot, or
    custom slot pointed at Serena) shares regardless of ``--context``/
    ``--project``/transport values. This is intentionally loose (order- and
    flag-independent) so it still recognizes a hand-edited or historical
    command that predates a later flag rename.
    """
    if not isinstance(cmd, (list, tuple)):
        return False
    tokens = [str(t) for t in cmd]
    return "serena-agent" in tokens and "start-mcp-server" in tokens


def ensure_serena_headless(cmd: Any) -> Any:
    """Force ``--open-web-dashboard false`` onto a Serena command (e99b09e9).

    This is the single canonical enforcement point for "headless by default":
    every place a Serena command is built or resolved — this module's own
    :func:`build_serena_command`, ``tunnel_plugins.SERENA_EXTRACT_COMMAND``,
    a tenant's saved/stale extract-slot override, or a custom tunnel slot
    pointed at Serena (all routed through it via
    ``tunnel_plugins.expand_command`` / ``tunnel_plugins.resolve_plugins``)
    — funnels through here before spawn. That way a missing or stale flag
    (e.g. an override saved before this flag existed, or a hand-edited
    command) can never pop a browser tab as a side effect of a tools/list,
    initialize, reconnect, or pool spawn.

    Non-Serena commands are returned unchanged (a copy, for list inputs).
    Idempotent — an already-correct command round-trips unchanged, modulo
    normalizing the flag's value to the literal ``"false"``.

    Respects :data:`SERENA_DASHBOARD_OPT_IN_ENV` — when set truthy on this
    machine, the command is left exactly as authored (dashboard mode
    preserved) so a developer can debug against the real Serena UI locally.
    """
    if not is_serena_command(cmd):
        return list(cmd) if isinstance(cmd, (list, tuple)) else cmd
    out = [str(t) for t in cmd]
    if _dashboard_opt_in():
        return out
    flag = "--open-web-dashboard"
    if flag in out:
        idx = out.index(flag)
        if idx + 1 < len(out):
            out[idx + 1] = "false"
        else:
            out.append("false")
    else:
        try:
            insert_at = out.index("start-mcp-server") + 1
        except ValueError:
            insert_at = len(out)
        out[insert_at:insert_at] = [flag, "false"]
    return out


def _command_hash(cmd: Any) -> str:
    """Short, non-reversible digest of a command for diagnostics.

    Launch diagnostics log a hash instead of the raw command so a
    tenant-customized override that happens to embed a pasted secret (e.g. an
    API key in a custom slot's command) never reaches stdout/logs.
    """
    tokens = cmd if isinstance(cmd, (list, tuple)) else []
    joined = " ".join(str(t) for t in tokens)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:12]

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

# ── host-local broker constants ─────────────────────────────────────────────

HEALTH_HEALTHY = "healthy"
HEALTH_UNHEALTHY = "unhealthy"
HEALTH_QUARANTINED = "quarantined"

# A pool stops trying to ADOPT a given repo_path's shared daemon after this
# many consecutive found-it-dead adoption failures, falling back to spawning
# (and owning) its own instance instead — prevents an endless adopt / find-it
# dead thrash loop against a broken shared entry. Cools down after
# QUARANTINE_COOLDOWN_SECONDS so a transient blip does not quarantine a
# repo_path forever.
QUARANTINE_AFTER_FAILURES = 3
QUARANTINE_COOLDOWN_SECONDS = 5 * 60


def build_serena_command(repo_path: str, port: int) -> list[str]:
    """Serena HTTP launch command for one repo on one port.

    Uses ``--transport streamable-http --port`` so Serena serves HTTP directly
    (no mcp-proxy bridge needed) and ``--project`` to scope it to ``repo_path``.
    Mirrors the flags in :data:`meridian.tunnel_plugins.SERENA_EXTRACT_COMMAND`
    (claude-code context) plus the per-instance transport/port. Routed through
    :func:`ensure_serena_headless` — the same canonical enforcement point
    every other Serena command (defaults, overrides, custom slots) goes
    through — rather than inlining the flag here, so this builder can never
    silently drift from the shared headless-by-default guarantee.
    """
    cmd = [
        "uvx", "--from", "serena-agent", "serena", "start-mcp-server",
        "--context", "claude-code",
        "--transport", "streamable-http",
        "--port", str(port),
        "--project", repo_path,
    ]
    return ensure_serena_headless(cmd)


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


def config_fingerprint(cmd: list[str]) -> str:
    """Stable short hash identifying one repo's Serena launch config.

    Deliberately independent of the *port* (each pool allocates ports
    locally, so two pools spawning the identical config for the same
    repo_path will pick different ports) — callers building the fingerprint
    for comparison purposes pass a fixed placeholder port (see
    :meth:`SerenaDaemonPool._fingerprint_for`) so only the flags/version that
    actually define "is this the same config" are hashed. Two daemons with
    matching fingerprints are safe to treat as interchangeable; a mismatch
    (different Serena version pin, different extra flags, …) means adoption
    must be refused and a fresh daemon spawned instead — config drift must
    never be silently papered over.
    """
    joined = "\x1f".join(cmd)  # unit separator avoids arg-boundary collisions
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


def default_broker_dir() -> Path:
    """Host-local, per-user directory for the shared Serena-pool broker registry.

    Mirrors ``tunnel_client._slot_claim_dir()``'s ``~/.meridian/...``
    convention — one shared location so every ``tunnel_client`` process on
    this host (regardless of which repo/session spawned it) discovers the
    same registry.
    """
    return Path.home() / ".meridian" / "serena_pool_broker"


def _repo_key(normalized_repo_path: str) -> str:
    """Filesystem-safe directory name for one repo's broker entry."""
    return hashlib.sha256(normalized_repo_path.encode("utf-8")).hexdigest()[:24]


def _read_json(path: Path) -> "dict | None":
    """Best-effort JSON read; ``None`` on any missing/corrupt/unreadable file."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    return data if isinstance(data, dict) else None


def _write_json(path: Path, data: "dict[str, Any]") -> None:
    """Best-effort JSON write; never raises (a write failure must never block
    or crash a spawn/adopt/release call)."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _remove_file(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass


def _default_pid_alive(pid: int) -> bool:
    """Best-effort liveness check for a pid this process did not spawn itself
    (used only for leased/adopted daemons and stale-lease pruning).

    Degrades to ``True`` — assume alive — when psutil is unavailable or the
    check itself fails, so a missing/broken checker can never cause this
    module to falsely reap or refuse-to-adopt a resource that may still be
    perfectly healthy. Production callers (``tunnel_client``) may inject a
    stricter, create_time-verified checker; see the module docstring's
    "known limitations" for why the default here is intentionally simple.
    """
    try:
        import psutil  # type: ignore
    except Exception:  # noqa: BLE001
        return True
    try:
        return bool(psutil.pid_exists(pid))
    except Exception:  # noqa: BLE001
        return True


def _default_terminate_by_pid(pid: int) -> None:
    """Best-effort terminate-by-pid for a daemon this pool did not itself
    spawn (no ``Popen`` handle) — e.g. this pool is the last live lessee of a
    daemon whose original spawning tunnel process has already exited.
    Mirrors ``orphan_reaper._psutil_kill``'s terminate-then-escalate pattern;
    never raises.
    """
    try:
        import psutil  # type: ignore
    except Exception:  # noqa: BLE001
        psutil = None  # type: ignore
    if psutil is not None:
        try:
            proc = psutil.Process(pid)
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                proc.kill()
        except Exception:  # noqa: BLE001
            pass
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True, check=False,
            )
        else:
            os.kill(pid, 15)  # SIGTERM
    except Exception:  # noqa: BLE001
        pass


def has_live_lease(
    broker_dir: Path,
    daemon_pid: int,
    pid_alive: "Callable[[int], bool] | None" = None,
) -> bool:
    """True if some broker entry under *broker_dir* both (a) records
    *daemon_pid* as its daemon's own OS pid, and (b) currently has at least
    one lease file naming a tunnel_pid that is still alive.

    This is what :func:`meridian.tunnel_client._kill_all_previously_spawned_pids`
    (the once-per-startup orphan sweep) consults before killing an entry
    labeled ``"extract"``: that sweep's own liveness check only looks at
    whether the ORIGINAL SPAWNING tunnel is still alive, which is exactly
    wrong for a shared daemon — the whole point of the broker is that a
    daemon can outlive the tunnel that spawned it, as long as some OTHER
    still-running sibling tunnel currently leases it. Returns ``False`` (and
    never raises) on any I/O error, missing directory, or when no entry
    matches — the safe default for a caller not wired to use this broker.
    """
    checker = pid_alive or _default_pid_alive
    try:
        if not broker_dir.is_dir():
            return False
        for key_dir in broker_dir.iterdir():
            if not key_dir.is_dir():
                continue
            descriptor = _read_json(key_dir / "daemon.json")
            if not descriptor or descriptor.get("pid") != daemon_pid:
                continue
            for lease_file in key_dir.glob("lease-*.json"):
                data = _read_json(lease_file)
                if not data:
                    continue
                tpid = data.get("tunnel_pid")
                if isinstance(tpid, int) and checker(tpid):
                    return True
            return False  # descriptor matched this pid, but no live lease
    except Exception:  # noqa: BLE001 — best-effort, never raise
        return False
    return False


class SerenaDaemon:
    """One Serena process serving a single repo over streamable-http.

    ``owned`` is True when THIS pool instance holds the ``Popen`` handle (it
    spawned the process); False when this daemon was **adopted** from a
    sibling pool's broker descriptor — in that case ``proc`` is ``None`` and
    liveness/termination go through ``external_pid`` instead (see
    :attr:`is_alive` and :attr:`pid`).
    """

    __slots__ = (
        "repo_path", "port", "proc", "last_used",
        "owned", "owner_id", "external_pid", "start_time",
        "config_fingerprint", "health", "_pid_alive_fn",
    )

    def __init__(
        self,
        repo_path: str,
        port: int,
        proc: Any,
        last_used: float,
        *,
        owned: bool = True,
        owner_id: str = "",
        external_pid: "int | None" = None,
        start_time: "float | None" = None,
        config_fingerprint: str = "",
        health: str = HEALTH_HEALTHY,
        pid_alive_fn: "Callable[[int], bool] | None" = None,
    ):
        self.repo_path = repo_path
        self.port = port
        self.proc = proc
        self.last_used = last_used
        self.owned = owned
        self.owner_id = owner_id
        self.external_pid = external_pid
        self.start_time = start_time if start_time is not None else last_used
        self.config_fingerprint = config_fingerprint
        self.health = health
        self._pid_alive_fn = pid_alive_fn

    @property
    def is_alive(self) -> bool:
        """True if the underlying process is still running.

        For an owned daemon this is the original ``proc.poll()`` check. For
        an adopted (not-owned) daemon there is no local ``Popen`` handle, so
        liveness is checked via the recorded ``external_pid`` and the
        injected ``pid_alive_fn`` (defaults to "assume alive" — see
        :func:`_default_pid_alive` — so a missing checker never falsely
        reaps a daemon another pool still depends on).
        """
        if self.proc is not None:
            try:
                return self.proc.poll() is None
            except Exception:  # noqa: BLE001
                return False
        if self.external_pid is None:
            return False
        if self._pid_alive_fn is None:
            return True
        try:
            return bool(self._pid_alive_fn(self.external_pid))
        except Exception:  # noqa: BLE001
            return False

    @property
    def pid(self) -> "int | None":
        """This daemon's OS pid — from our own ``Popen`` if we spawned it,
        else the recorded external pid for an adopted/leased daemon."""
        if self.proc is not None:
            return getattr(self.proc, "pid", None)
        return self.external_pid

    def touch(self, now: float) -> None:
        """Mark the daemon as just-used (resets its idle timer)."""
        self.last_used = now

    def idle_seconds(self, now: float) -> float:
        """Seconds since the daemon last served a request."""
        return max(0.0, now - self.last_used)


class SerenaDaemonPool:
    """A pool of Serena daemons keyed by normalized repo_path.

    Daemons are spawned lazily by :meth:`get_or_spawn` and torn down either by
    :meth:`reap_idle` (idle TTL) or :meth:`shutdown` (tunnel exit). ``spawn``
    and ``now`` are injectable for tests; in production they default to
    ``subprocess.Popen`` and ``time.monotonic``.

    Passing ``broker_dir`` opts this pool into the host-local broker (see the
    module docstring): daemons become shareable across sibling pools on the
    same host, reference-counted via lease files so a daemon is only killed
    once its last live lessee releases it. ``broker_dir=None`` (the default)
    keeps the original pure in-process behaviour with zero filesystem I/O.
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
        broker_dir: "Path | None" = None,
        owner_id: str = "",
        pid_alive: "Callable[[int], bool] | None" = None,
        terminate_by_pid: "Callable[[int], None] | None" = None,
        self_pid: Callable[[], int] = os.getpid,
        max_daemons: "int | None" = None,
        quarantine_after_failures: int = QUARANTINE_AFTER_FAILURES,
        quarantine_cooldown_seconds: float = QUARANTINE_COOLDOWN_SECONDS,
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

        # ── host-local broker state ─────────────────────────────────────────
        self.broker_dir = broker_dir
        # An owner_id is only meaningful once the broker is enabled — but if
        # the caller enabled broker_dir without naming an owner_id, generate
        # one so two sibling pools never collide on the SAME lease filename
        # (which would make each look like the other's own already-released
        # lease and break the reference count).
        self.owner_id = owner_id or (str(uuid.uuid4()) if broker_dir is not None else "")
        self._pid_alive = pid_alive if pid_alive is not None else _default_pid_alive
        self._terminate_by_pid = (
            terminate_by_pid if terminate_by_pid is not None else _default_terminate_by_pid
        )
        self._self_pid = self_pid
        self.max_daemons = max_daemons
        self._quarantine_after_failures = quarantine_after_failures
        self._quarantine_cooldown_seconds = quarantine_cooldown_seconds
        self._adopt_failures: dict[str, int] = {}
        self._quarantine_until: dict[str, float] = {}
        # 9c8336c4 — one ProcessBudgetMonitor per repo_path key, created
        # lazily on first check_budgets() call. Only ever tracks OWNED
        # daemons (see check_budgets) — an adopted/leased daemon belongs to
        # a sibling pool's own registry, not this one's.
        self._budget_monitors: "dict[str, _process_budget.ProcessBudgetMonitor]" = {}

    @staticmethod
    def _normalize(repo_path: str) -> str:
        """Canonicalize a repo_path so two spellings map to one daemon."""
        try:
            return str(Path(repo_path).expanduser().resolve())
        except Exception:  # noqa: BLE001 — non-filesystem-ish key, use as-is
            return str(repo_path).strip()

    def _next_port(self) -> int:
        """Lowest base_port+offset not currently bound by a live daemon —
        either one THIS pool tracks, or one a sibling pool's broker
        descriptor claims (when the broker is enabled), so two independent
        pools on the same host never collide on the same port for two
        DIFFERENT repos."""
        used = {d.port for d in self._daemons.values()}
        used |= self._broker_used_ports()
        port = self.base_port
        while port in used:
            port += 1
        return port

    # ── host-local broker: filesystem plumbing ──────────────────────────────

    def _key_dir(self, key: str) -> Path:
        return self.broker_dir / _repo_key(key)  # type: ignore[operator]

    def _lease_path(self, key: str) -> Path:
        return self._key_dir(key) / f"lease-{self.owner_id}.json"

    def _lease_paths(self, key: str) -> "list[Path]":
        d = self._key_dir(key)
        if not d.is_dir():
            return []
        try:
            return list(d.glob("lease-*.json"))
        except Exception:  # noqa: BLE001
            return []

    def _read_descriptor(self, key: str) -> "dict | None":
        if self.broker_dir is None:
            return None
        return _read_json(self._key_dir(key) / "daemon.json")

    def _write_descriptor(self, key: str, daemon: SerenaDaemon) -> None:
        if self.broker_dir is None:
            return
        _write_json(self._key_dir(key) / "daemon.json", {
            "repo_path": key,
            "port": daemon.port,
            "pid": daemon.pid,
            "start_time": daemon.start_time,
            "config_fingerprint": daemon.config_fingerprint,
        })

    def _remove_descriptor(self, key: str) -> None:
        if self.broker_dir is None:
            return
        _remove_file(self._key_dir(key) / "daemon.json")

    def _write_lease(self, key: str) -> None:
        if self.broker_dir is None or not self.owner_id:
            return
        _write_json(self._lease_path(key), {
            "owner_id": self.owner_id,
            "tunnel_pid": self._self_pid(),
            "touched_at": self._now(),
        })

    def _touch_lease(self, key: str) -> None:
        self._write_lease(key)

    def _remove_lease(self, key: str) -> None:
        if self.broker_dir is None:
            return
        _remove_file(self._lease_path(key))

    def _prune_leases(self, key: str) -> None:
        """Remove lease files whose recording tunnel process has died —
        crash cleanup for a sibling that never got to release its own lease."""
        for p in self._lease_paths(key):
            data = _read_json(p)
            pid = data.get("tunnel_pid") if data else None
            if not isinstance(pid, int) or not self._pid_alive(pid):
                _remove_file(p)

    def _has_other_live_lease(self, key: str) -> bool:
        for lease_path in self._lease_paths(key):
            if lease_path == self._lease_path(key):
                continue  # our own — we've already removed it by this point
            data = _read_json(lease_path)
            if not data:
                continue
            pid = data.get("tunnel_pid")
            if isinstance(pid, int) and self._pid_alive(pid):
                return True
        return False

    def _broker_used_ports(self) -> "set[int]":
        if self.broker_dir is None or not self.broker_dir.is_dir():
            return set()
        ports: "set[int]" = set()
        try:
            for key_dir in self.broker_dir.iterdir():
                if not key_dir.is_dir():
                    continue
                descriptor = _read_json(key_dir / "daemon.json")
                if not descriptor:
                    continue
                port = descriptor.get("port")
                dpid = descriptor.get("pid")
                if isinstance(port, int) and isinstance(dpid, int) and self._pid_alive(dpid):
                    ports.add(port)
        except Exception:  # noqa: BLE001
            pass
        return ports

    def _fingerprint_for(self, key: str) -> str:
        """Config fingerprint for *key*, independent of the port a specific
        pool happens to allocate (see :func:`config_fingerprint`)."""
        return config_fingerprint(self._command_builder(key, 0))

    # ── quarantine bookkeeping ───────────────────────────────────────────────

    def _note_adopt_failure(self, key: str, now: float) -> None:
        n = self._adopt_failures.get(key, 0) + 1
        self._adopt_failures[key] = n
        if n >= self._quarantine_after_failures:
            self._quarantine_until[key] = now + self._quarantine_cooldown_seconds

    def _is_quarantined(self, key: str, now: float) -> bool:
        until = self._quarantine_until.get(key)
        if until is None:
            return False
        if now >= until:
            del self._quarantine_until[key]
            self._adopt_failures.pop(key, None)
            return False
        return True

    # ── adoption ─────────────────────────────────────────────────────────────

    def _try_adopt(self, key: str, now: float) -> "SerenaDaemon | None":
        descriptor = self._read_descriptor(key)
        if descriptor is None:
            return None
        pid = descriptor.get("pid")
        port = descriptor.get("port")
        fingerprint = descriptor.get("config_fingerprint")
        if not isinstance(pid, int) or not isinstance(port, int):
            return None
        if fingerprint != self._fingerprint_for(key):
            return None  # config drift — never adopt a mismatched instance
        if not self._pid_alive(pid):
            # Stale descriptor from a daemon that is no longer running —
            # clean it up so no sibling wastes a cycle trying to adopt it.
            self._remove_descriptor(key)
            self._prune_leases(key)
            return None
        start_time = descriptor.get("start_time")
        daemon = SerenaDaemon(
            key, port, None, now,
            owned=False, owner_id=self.owner_id, external_pid=pid,
            start_time=start_time if isinstance(start_time, (int, float)) else now,
            config_fingerprint=fingerprint or "",
            health=HEALTH_HEALTHY,
            pid_alive_fn=self._pid_alive,
        )
        self._write_lease(key)
        return daemon

    def get_or_spawn(
        self,
        repo_path: str,
        *,
        on_launch: Callable[[dict[str, Any]], None] | None = None,
    ) -> SerenaDaemon:
        """Return the live daemon for ``repo_path``, spawning one if needed.

        With no broker configured this is unchanged from the original pool:
        reuse the local daemon if alive, else spawn (re-spawning transparently
        if a previously-registered daemon has died).

        With a broker configured, a locally-dead entry first tries to ADOPT a
        still-alive, config-matching daemon a sibling pool already has
        running for this repo_path before spawning a duplicate — this is the
        actual cross-session sharing. The returned daemon's idle timer is
        reset either way.

        ``on_launch`` (e99b09e9), if given, is called with a structured
        diagnostics dict — ``repo_path``, ``port``, ``pid``, ``reused``
        (bool), ``dashboard`` (``"headless"``/``"gui"``), ``command_hash``
        (never the raw command — see :func:`_command_hash`) — on both the
        reuse and fresh-spawn paths (not on the adoption path, which is
        diagnosed separately via :meth:`diagnostics`). Optional and injected
        (same pattern as ``spawn``/``now``/``terminate``) so the pool stays
        pure and callers (the tunnel client) own how/whether diagnostics are
        logged.
        """
        key = self._normalize(repo_path)
        now = self._now()
        existing = self._daemons.get(key)
        if existing is not None and existing.is_alive:
            existing.touch(now)
            if self.broker_dir is not None:
                self._touch_lease(key)
                if not existing.owned:
                    self._adopt_failures.pop(key, None)
            if on_launch is not None:
                on_launch(self._launch_diagnostics(key, existing, reused=True))
            return existing
        if existing is not None:
            # Locally-tracked entry died: either our own owned process
            # crashed, or — for an adopted entry — the process a sibling
            # spawned went away.
            if not existing.owned:
                self._note_adopt_failure(key, now)
                self._remove_lease(key)
            del self._daemons[key]

        if self.broker_dir is not None and not self._is_quarantined(key, now):
            adopted = self._try_adopt(key, now)
            if adopted is not None:
                self._daemons[key] = adopted
                return adopted

        # Missing, dead, or nothing adoptable → spawn fresh (reuse the dead
        # local daemon's port if any).
        port = existing.port if existing is not None else self._next_port()
        cmd = self._command_builder(key, port)
        proc = self._spawn(cmd)
        daemon = SerenaDaemon(
            key, port, proc, now,
            owned=True, owner_id=self.owner_id, start_time=now,
            config_fingerprint=self._fingerprint_for(key),
            health=HEALTH_HEALTHY, pid_alive_fn=self._pid_alive,
        )
        self._daemons[key] = daemon
        if self.broker_dir is not None:
            self._write_descriptor(key, daemon)
            self._write_lease(key)
            self._enforce_max_daemons()
        elif self.max_daemons is not None:
            self._enforce_max_daemons()
        if on_launch is not None:
            on_launch(self._launch_diagnostics(key, daemon, reused=False, cmd=cmd))
        return daemon

    def _launch_diagnostics(
        self, repo_path: str, daemon: SerenaDaemon, *, reused: bool, cmd: Any = None,
    ) -> dict[str, Any]:
        """Structured launch info for ``on_launch`` (see :meth:`get_or_spawn`).

        On the reuse path no command was (re)built, so one is recomputed here
        purely for the diagnostic hash — :func:`build_serena_command`-shaped
        builders are pure/cheap, and nothing is spawned by this call.
        """
        if cmd is None:
            try:
                cmd = self._command_builder(repo_path, daemon.port)
            except Exception:  # noqa: BLE001 — diagnostics must never break spawn
                cmd = None
        return {
            "repo_path": repo_path,
            "port": daemon.port,
            "pid": getattr(daemon.proc, "pid", None),
            "reused": reused,
            "dashboard": "gui" if _dashboard_opt_in() else "headless",
            "command_hash": _command_hash(cmd),
        }

    def daemon_for(self, repo_path: str) -> SerenaDaemon | None:
        """Return the registered daemon for ``repo_path`` without spawning."""
        return self._daemons.get(self._normalize(repo_path))

    # ── bounded memory/CPU ───────────────────────────────────────────────────

    def _enforce_max_daemons(self) -> None:
        """Bound THIS pool's own owned-daemon footprint: when spawning a NEW
        (not adopted) daemon pushes the owned count past ``max_daemons``,
        release the least-recently-used owned daemon first. An adopted/leased
        daemon is never evicted here — it costs this pool nothing to keep
        tracking (no local process, no local memory beyond a small struct)."""
        if self.max_daemons is None:
            return
        owned = [d for d in self._daemons.values() if d.owned]
        while len(owned) > self.max_daemons:
            victim = min(owned, key=lambda d: d.last_used)
            del self._daemons[victim.repo_path]
            self._release_daemon(victim.repo_path, victim)
            owned.remove(victim)

    # ── idle reaping + shutdown ─────────────────────────────────────────────

    def _release_daemon(self, key: str, daemon: SerenaDaemon) -> bool:
        """Release this pool's claim on *daemon*; terminate it iff this pool
        was the last live claimant. Returns True iff this call actually
        terminated the process."""
        if self.broker_dir is None:
            # No cross-process broker configured — original single-tunnel
            # behaviour: this pool always owns what it tracks, so it always
            # kills it.
            self._terminate(daemon.proc)
            return True
        self._remove_lease(key)
        self._prune_leases(key)  # sweep crashed sibling leases while we're here
        if self._has_other_live_lease(key):
            return False  # a sibling still depends on it — leave it running
        if daemon.proc is not None:
            self._terminate(daemon.proc)
        elif daemon.pid is not None:
            self._terminate_by_pid(daemon.pid)
        self._remove_descriptor(key)
        return True

    def reap_idle(
        self,
        now: float | None = None,
        *,
        on_terminate: Callable[[dict[str, Any]], None] | None = None,
    ) -> list[str]:
        """Release daemons idle longer than the TTL. Returns the repo_paths
        this pool released (which may still be alive if a sibling pool still
        leases them — see :meth:`_release_daemon`).

        ``on_terminate`` (e99b09e9), if given, is called once per daemon this
        pool actually terminates (not one merely released while a sibling
        still leases it — see :meth:`_release_daemon`'s return value) with
        ``{repo_path, port, pid, reason: "idle_timeout"}``.
        """
        when = self._now() if now is None else now
        reaped: list[str] = []
        for key, daemon in list(self._daemons.items()):
            if daemon.idle_seconds(when) >= self.idle_kill_seconds:
                pid = getattr(daemon.proc, "pid", None)
                del self._daemons[key]
                terminated = self._release_daemon(key, daemon)
                reaped.append(key)
                if on_terminate is not None and terminated:
                    on_terminate({
                        "repo_path": key, "port": daemon.port, "pid": pid,
                        "reason": "idle_timeout",
                    })
        return reaped

    def shutdown(
        self, *, on_terminate: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        """Release every daemon this pool tracks (tunnel exit). A daemon is
        only actually terminated if no sibling pool still leases it.

        ``on_terminate`` (e99b09e9), if given, is called once per daemon this
        pool actually terminates (not one left running because a sibling
        still leases it — see :meth:`_release_daemon`'s return value) with
        ``{repo_path, port, pid, reason: "tunnel_shutdown"}``.
        """
        for key, daemon in list(self._daemons.items()):
            pid = getattr(daemon.proc, "pid", None)
            terminated = self._release_daemon(key, daemon)
            if on_terminate is not None and terminated:
                on_terminate({
                    "repo_path": key, "port": daemon.port, "pid": pid,
                    "reason": "tunnel_shutdown",
                })
        self._daemons.clear()

    def repo_paths(self) -> list[str]:
        """Normalized repo_paths with a registered daemon (owned or leased)."""
        return list(self._daemons.keys())

    # ── diagnostics ──────────────────────────────────────────────────────────

    def diagnostics(self, now: float | None = None) -> "list[dict[str, Any]]":
        """Per-daemon diagnostics: repo_path, port, pid, ownership, owner_id,
        start_time, config_fingerprint, health, quarantine state, and idle
        seconds — everything the sprint scope asks a broker to expose."""
        when = self._now() if now is None else now
        out: "list[dict[str, Any]]" = []
        for key, daemon in self._daemons.items():
            alive = daemon.is_alive
            out.append({
                "repo_path": key,
                "port": daemon.port,
                "pid": daemon.pid,
                "owned": daemon.owned,
                "owner_id": daemon.owner_id,
                "start_time": daemon.start_time,
                "config_fingerprint": daemon.config_fingerprint,
                "health": daemon.health if alive else HEALTH_UNHEALTHY,
                "quarantined": self._is_quarantined(key, when),
                "idle_seconds": daemon.idle_seconds(when),
            })
        return out

    # ── host-local memory/CPU budgets (9c8336c4) ────────────────────────────

    def check_budgets(
        self,
        budget: "_process_budget.ProcessBudget | None" = None,
        *,
        sampler: "Callable[[int], Any] | None" = None,
        on_terminate: "Callable[[dict[str, Any]], None] | None" = None,
    ) -> "list[_process_budget.BudgetReport]":
        """Sample and enforce a host-local memory/CPU budget against every
        daemon THIS pool OWNS (spawned itself).

        An adopted/leased daemon (``daemon.owned is False``) is never
        checked here — it belongs to a sibling pool's own registry and
        this pool has no OS-level authority to throttle or kill it. This is
        the "only processes proven owned ... may be throttled or
        terminated" rule from the sprint notes, enforced structurally
        rather than by a runtime check.

        Opt-in: never invoked automatically by :meth:`get_or_spawn` /
        :meth:`reap_idle` / :meth:`shutdown` — a caller (the tunnel's
        periodic loop) calls this on its own cadence. Returns one
        :class:`process_budget.BudgetReport` per checked daemon so a caller
        can log/aggregate; a daemon whose report action is ``"kill"`` is
        released through the existing broker-aware :meth:`_release_daemon`
        path (so a still-leased-by-a-sibling daemon is correctly left
        running even if THIS pool's budget considers it over-budget — the
        sibling's lease wins, matching every other release path in this
        module) and removed from local tracking.

        ``on_terminate``, if given, mirrors :meth:`reap_idle`'s own
        callback shape — called once per daemon this pool actually
        terminates, with ``{repo_path, port, pid, reason: "budget_exceeded"}``.
        """
        cfg = budget or _process_budget.load_host_budget_config()
        if not cfg.enabled:
            return []
        reports: "list[_process_budget.BudgetReport]" = []
        for key, daemon in list(self._daemons.items()):
            if not daemon.owned or daemon.pid is None:
                continue
            monitor = self._budget_monitors.get(key)
            if monitor is None:
                monitor = _process_budget.ProcessBudgetMonitor(f"serena:{key}", cfg)
                self._budget_monitors[key] = monitor
            sample = _process_budget.sample_process(daemon.pid, proc_factory=sampler)
            report = monitor.evaluate(daemon.pid, sample)
            if report.action == "kill":
                pid = daemon.pid
                port = daemon.port
                del self._daemons[key]
                terminated = self._release_daemon(key, daemon)
                monitor.record_kill_outcome(survived=not terminated)
                if terminated:
                    self._budget_monitors.pop(key, None)
                    if on_terminate is not None:
                        on_terminate({
                            "repo_path": key, "port": port, "pid": pid,
                            "reason": "budget_exceeded",
                        })
            reports.append(report)
        return reports

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
