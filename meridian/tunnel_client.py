"""Client side of the Pro permanent tunnel (`meridian --tunnel`).

Run once on the machine that holds your repo::

    meridian --tunnel

It:
  1. Reads your API token + server URL from CLI args or the environment.
  2. Calls ``GET /me`` to resolve your ``tenant_id`` and confirm a Pro plan.
  3. Spawns a local ``mcp-proxy`` wrapping ``@modelcontextprotocol/server-filesystem``
     pointed at your repo, listening on ``127.0.0.1:8808``.
  4. Opens a persistent WebSocket to ``wss://<server>/tunnel/{tenant_id}`` and
     relays every proxied request to the local proxy, returning the response.
  5. Auto-reconnects with exponential backoff if the socket drops.
  6. Prints the permanent URL to paste into claude.ai once:
     ``https://<server>/fs/mcp/{tenant_id}``

The server side lives in ``meridian/routes/tunnel.py`` — the framing here
mirrors the protocol documented there.
"""
from __future__ import annotations

from typing import Any

import asyncio
import base64
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import __version__
from . import serena_pool as _serena_pool
from .serena_pool import SerenaDaemonPool, SERENA_POOL_BASE_PORT

DEFAULT_BASE_URL = "https://usemeridian.us"
DEFAULT_PROXY_PORT = 8808
DEFAULT_CODE_PROXY_PORT = 8809
DEFAULT_EXTRACT_PROXY_PORT = 8810
# Server's proxy timeout is 30s (_PROXY_TIMEOUT); stay just under it so a slow
# local response surfaces as our error rather than the server's tunnel timeout.
_LOCAL_REQUEST_TIMEOUT = 28.0
_MAX_BACKOFF = 30.0
# Crash isolation: how many times the watchdog relaunches a slot's proxy that
# keeps exiting (e.g. ENOENT on a missing binary) before giving up on that slot.
_WATCHDOG_MAX_RETRIES = 5
# Lazy spawn: how long (seconds) a slot may stay idle before auto-kill.
# 3649a61a — don't spawn at startup; kill after 30min of no requests.
_IDLE_KILL_SECONDS = 30 * 60
# a3410a9c — once a core slot is marked unhealthy, how often (seconds) the
# background re-probe retries spawning + tools/list to auto-recover the slot
# (e.g. the missing npx/binary became available). On success the client tells
# the server to re-advertise the slot's tools.
_SLOT_REPROBE_INTERVAL = 60.0

# 089a936a — the DEFAULT first-spawn pre-flight probe budget (attempts, delay):
# attempts=2 × delay=3s with a 10s per-attempt httpx timeout ≈ up to ~23s. Fine
# for slots whose launcher is already on disk.
_PREFLIGHT_BUDGET_DEFAULT: "tuple[int, float]" = (2, 3.0)
# 089a936a — a LARGER first-spawn budget for "cold-fetch" slots whose very first
# spawn triggers a network cold download (e.g. Desktop Commander:
# `npx -y @wonderwhy-er/desktop-commander@latest`, or the office slots'
# `uvx docx-mcp` / `uvx powerpoint-mcp`) that can exceed the default ~23s.
# attempts=4 × delay=5s with the 10s per-attempt timeout ≈ up to ~55s.
# This applies ONLY to the first-spawn pre-flight, NOT the 60s background reprobe
# (which stays attempts=1).
_PREFLIGHT_BUDGET_COLD_FETCH: "tuple[int, float]" = (4, 5.0)
# 089a936a / 24b6cb5d — slots that download their inner server on first launch and
# so need the larger cold-fetch pre-flight budget above. dc = Desktop Commander
# (npx). ppt/word = the Office slots (`uvx powerpoint-mcp` / `uvx docx-mcp`): their
# first spawn triggers a uvx download of the same order as DC's npx fetch, so they
# were failing the standard ~23s budget on a cold cache (live symptom:
# "tunnel:ppt: pre-flight health check FAILED"). Give them the same extended budget.
_COLD_FETCH_SLOTS: "frozenset[str]" = frozenset({"dc", "ppt", "word"})


# ---------------------------------------------------------------------------
# Version check (4bde9437) — nudge an upgrade when the local tunnel binary is
# behind the deployed server. Pure + fail-safe: never raises, so a bad/absent
# version string can never block the tunnel from starting.
# ---------------------------------------------------------------------------

def _version_tuple(v: str) -> tuple[int, ...]:
    """Parse a dotted version into a comparable int tuple.

    Each dot-segment contributes its leading run of digits (so ``0.1.6`` →
    ``(0, 1, 6)`` and ``0.2.0rc1`` → ``(0, 2, 0)``). Parsing stops at the first
    segment with no leading digit, and any wholly-unparseable input yields the
    empty tuple — which callers treat as "unknown, don't nag".
    """
    parts: list[int] = []
    for seg in str(v).strip().split("."):
        digits = ""
        for ch in seg:
            if ch.isdigit():
                digits += ch
            else:
                break
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def _update_notice(local: str, server: str) -> str | None:
    """Return a one-line upgrade notice iff ``server`` is newer than ``local``.

    Returns ``None`` (no nag) when either version is missing/unparseable or when
    the local version is already >= the server's. Never raises — the whole point
    is that a version check must never be able to abort ``run_tunnel``.
    """
    try:
        lt = _version_tuple(local)
        st = _version_tuple(server)
        if lt and st and st > lt:
            return (
                f"  meridian update available: server {server} > local {local} "
                "— upgrade: uv tool install meridian-server --upgrade"
            )
    except Exception:  # noqa: BLE001 — informational only, never block startup
        return None
    return None


# 23ba76a2 — tunnel self-update policy. The SAFE DEFAULT is notify-then-explicit-
# confirm ('ask'), never a silent full-auto update while a session may be live (that
# risks corrupting in-progress work). Tiers: off (no notice) | warn (notice only,
# the legacy behaviour) | ask (notice + require a keypress before updating) |
# full-auto (update without asking — opt-in only, never the default).
_VALID_UPDATE_MODES = ("off", "warn", "ask", "full-auto")


def _resolve_update_mode() -> str:
    """Return the tunnel update mode, read live from ``MERIDIAN_TUNNEL_UPDATE_MODE``.

    Unknown/blank values fall back to ``'ask'`` (the safe notify-then-confirm default)
    rather than to any auto-update, so a typo can never silently enable full-auto.
    """
    raw = str(os.environ.get("MERIDIAN_TUNNEL_UPDATE_MODE", "") or "").strip().lower()
    return raw if raw in _VALID_UPDATE_MODES else "ask"


def _update_action(mode: str, local: str, server: str, is_tty: bool) -> str:
    """Decide what the update nudge should DO — pure + testable, no I/O.

    Returns one of:
      * ``'none'``    — no newer version (or mode ``off``): do nothing.
      * ``'notify'``  — print the upgrade notice only.
      * ``'confirm'`` — print the notice AND ask for an explicit keypress before updating.
      * ``'auto'``    — update without asking (mode ``full-auto`` only).

    ``ask`` degrades to ``notify`` when stdin isn't a TTY: a backgrounded/daemonized
    tunnel can't be prompted, so it must never block or hang waiting on input.
    """
    if not _update_notice(local, server):
        return "none"  # not newer / missing / unparseable
    m = mode if mode in _VALID_UPDATE_MODES else "ask"
    if m == "off":
        return "none"
    if m == "warn":
        return "notify"
    if m == "full-auto":
        return "auto"
    return "confirm" if is_tty else "notify"  # 'ask'


def _perform_self_update() -> bool:
    """Best-effort in-place upgrade via whatever installed meridian (uv, then pipx).

    Installs the new version — which takes effect on the NEXT launch; we never hot-swap
    the running process mid-session. Fully guarded; returns True only on a clean exit,
    and always leaves the tunnel able to continue on the current version.
    """
    import shutil  # noqa: PLC0415
    import subprocess  # noqa: PLC0415

    if shutil.which("uv"):
        cmd = ["uv", "tool", "install", "meridian-server", "--upgrade"]
    elif shutil.which("pipx"):
        cmd = ["pipx", "upgrade", "meridian-server"]
    else:
        print(
            "  no uv/pipx found — upgrade manually: uv tool install meridian-server --upgrade",
            flush=True,
        )
        return False
    print(f"  updating: {' '.join(cmd)}", flush=True)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except Exception as exc:  # noqa: BLE001 — an update must never crash the tunnel
        print(f"  update failed to run ({exc}); upgrade manually.", flush=True)
        return False
    if proc.returncode == 0:
        print("  updated — restart the tunnel to run the new version.", flush=True)
        return True
    _err = (proc.stderr or proc.stdout or "").strip()[:200]
    print(f"  update exited {proc.returncode}: {_err}", flush=True)
    return False


# ---------------------------------------------------------------------------
# SlotProxy — lazy-spawn subprocess manager (3649a61a)
# ---------------------------------------------------------------------------

def _port_is_open(port: int, host: str = "127.0.0.1", timeout: float = 0.25) -> bool:
    """Return True iff a TCP connection to ``host:port`` succeeds right now.

    A quick, blocking-but-brief socket connect used as a liveness signal for a
    slot whose *launcher* process handle has exited but whose real server child
    is still listening (c8e6b61c). The classic case is Windows Desktop Commander:
    the tracked ``cmd /c npx …`` Popen returns as soon as ``cmd`` finishes, while
    ``npx`` has already handed off to a detached MCP-server grandchild that keeps
    the port bound. A dead launcher handle then makes ``poll()`` report
    not-running, so the slot respawns on every single request ("spawning proxy …
    on port 8813" repeating). Checking the port breaks that loop.

    Pure + fail-safe: never raises. A refused connection (server down) or any
    socket error returns False; only a completed connect returns True. The short
    default timeout keeps this cheap enough to call on the request hot path — a
    loopback connect to an open OR refused port returns effectively instantly;
    the timeout only bounds the rare case of a filtered/hung port.
    """
    import socket  # noqa: PLC0415 — stdlib, local import keeps module import light

    try:
        port = int(port)
    except (TypeError, ValueError):
        return False
    if port <= 0:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False
    except Exception:  # noqa: BLE001 — liveness probe must never raise
        return False


class SlotProxy:
    """Manages one tunnel slot's mcp-proxy subprocess with lazy spawning.

    The proxy is NOT started when the ``SlotProxy`` is created — it is spawned
    on the first incoming request (``ensure_running``) and automatically killed
    after ``_IDLE_KILL_SECONDS`` of no activity. A ``_proc_watchdog``-compatible
    holder dict is exposed as ``.holder`` so the existing watchdog logic can
    relaunch the process if it crashes while active.

    Thread-safety: all methods are called from the same asyncio event loop.
    The ``asyncio.Lock`` only guards the startup race (two concurrent requests
    arriving before the process is up).

    Args:
        cmd:   Full command for subprocess.Popen.
        port:  The port the proxy listens on (``http://127.0.0.1:<port>``).
        label: Human-readable slot name for log messages.
        env:   Optional env dict for Popen; ``None`` inherits the parent env.
    """

    def __init__(
        self,
        cmd: "list[str]",
        port: int,
        label: str,
        env: "dict | None" = None,
    ) -> None:
        self.cmd = cmd
        self.port = port
        self.label = label
        self.env = env
        self._proc: "subprocess.Popen | None" = None
        self._last_used: float = 0.0
        self._lock = asyncio.Lock()
        # Holder dict for _proc_watchdog compatibility.
        self.holder: dict = {
            "proc": None,
            "cmd": cmd,
            "env": env,
            "label": label,
        }

    @property
    def is_running(self) -> bool:
        """True if this slot is serving — its launcher process OR its port is live.

        c8e6b61c — a slot is considered running when EITHER:

        * the tracked launcher ``Popen`` is still alive (``poll()`` is None), OR
        * the launcher handle has exited but the server it started is still
          listening on ``self.port`` (a quick loopback socket connect succeeds).

        The second clause fixes the Desktop-Commander respawn storm on Windows:
        DC is launched as ``cmd /c npx -y @wonderwhy-er/desktop-commander@latest``,
        and ``cmd /c`` returns the instant npx hands the real MCP server off to a
        detached grandchild. Relying on the launcher ``poll()`` alone then reports
        not-running while the server is very much up, so ``ensure_running`` respawns
        it on every request. Falling back to a port check keeps a live-but-detached
        slot counted as running. Cross-platform: on POSIX (and the direct-spawn
        path) the launcher handle is normally the server itself, so the first
        clause matches and the port check is never reached.

        The port check is only consulted when the launcher handle is gone/dead, so
        the healthy hot path (proc alive) stays a cheap ``poll()`` with no socket
        call.
        """
        if self._proc is not None and self._proc.poll() is None:
            return True
        # Launcher handle absent or exited — the real server may still be
        # listening (Windows cmd/c + detached npx grandchild). Only probe the
        # port once we've actually attempted a spawn, so a never-started slot
        # (or one that was explicitly killed) doesn't get falsely revived by an
        # unrelated process that happens to hold the port.
        if self._proc is None:
            return False
        return _port_is_open(self.port)

    def touch(self) -> None:
        """Record the current time as the last-used timestamp."""
        self._last_used = time.monotonic()

    def idle_seconds(self) -> float:
        """Seconds since the last request (or since epoch if never used)."""
        if self._last_used == 0.0:
            return 0.0
        return time.monotonic() - self._last_used

    async def ensure_running(self) -> None:
        """Spawn the proxy subprocess if it is not already running.

        Uses an asyncio.Lock to avoid double-spawning under concurrent first
        requests. Prints a startup message so the user can see when the lazy
        spawn fires. Non-blocking if the process is already alive.
        """
        if self.is_running:
            return
        async with self._lock:
            # Re-check inside the lock — another coroutine may have spawned it
            # while we were waiting.
            if self.is_running:
                return
            print(
                f"tunnel:{self.label}: spawning proxy (first request / after idle kill) "
                f"on port {self.port}",
                flush=True,
            )
            try:
                self._proc = subprocess.Popen(
                    self.cmd, env=self.env, **_spawn_kwargs()
                )
                self.holder["proc"] = self._proc
            except Exception as exc:  # noqa: BLE001
                print(
                    f"tunnel:{self.label}: failed to spawn proxy: {exc}",
                    file=sys.stderr, flush=True,
                )
                self._proc = None
                self.holder["proc"] = None
                return
            # e75f4fc4 — poll real readiness instead of a blind fixed sleep.
            # A blind asyncio.sleep(1.0) was long enough for a warm restart but
            # not for a genuinely cold spawn (post-idle-kill npx/uvx package
            # resolution + server boot), which can legitimately take several
            # seconds. That gap let the FIRST request after a cold spawn hit an
            # unready proxy and either fail immediately or hang until
            # mcp-proxy's own internal ~60s timeout (-32001) — a separate,
            # external-library timeout Meridian's 1s pause did nothing to
            # protect against. _probe_slot_health already exists and is proven
            # for exactly this "is the proxy actually answering yet" check (it
            # backs the reactive post-timeout watchdog); reuse it here instead
            # of inventing a new mechanism. attempts=6/delay=3.0 gives up to
            # ~15-18s of real polling — generous for a slow cold resolve while
            # staying comfortably under the 28s caller-side request timeout
            # referenced below, so a slow-but-eventually-successful spawn is
            # still usually caught before the caller's own request fires.
            healthy = await _probe_slot_health(self.port, attempts=6, delay=3.0)
            if not healthy:
                # Don't raise — the existing behavior never raised here either.
                # A caller's actual request will now get an honest connection
                # error rather than a silently-assumed-ready slot; the request-
                # timeout watchdog (3bde892a) recovers from there. Just make the
                # miss visible in the tunnel log for diagnosis.
                print(
                    f"tunnel:{self.label}: proxy did not answer tools/list within "
                    "the cold-spawn readiness window — a real request may still "
                    "fail; the request-timeout watchdog will retry if so",
                    flush=True,
                )
            self.touch()

    def kill(self) -> None:
        """Terminate the proxy process (best-effort, no-op if not running)."""
        _terminate_proc_tree(self._proc)
        self._proc = None
        self.holder["proc"] = None

    def sync_holder(self) -> None:
        """Sync the holder's proc reference (watchdog may have replaced it)."""
        if self.holder.get("proc") is not self._proc:
            self._proc = self.holder.get("proc")


async def _idle_killer(proxy: "SlotProxy", idle_seconds: float = _IDLE_KILL_SECONDS) -> None:
    """Periodically check if a slot's proxy has been idle too long and kill it.

    Runs forever (until cancelled). Checks every ``idle_seconds / 6`` (5 min
    for the default 30min window) so we don't over-check. Only kills when
    ``proxy.is_running`` AND the idle window has been exceeded. The proxy is
    re-spawned automatically on the next incoming request via ``ensure_running``.
    """
    poll_interval = max(60.0, idle_seconds / 6)
    while True:
        await asyncio.sleep(poll_interval)
        proxy.sync_holder()
        if proxy.is_running and proxy.idle_seconds() > idle_seconds:
            print(
                f"tunnel:{proxy.label}: idle for >{idle_seconds / 60:.0f}min — "
                "killing proxy to free resources (will restart on next request)",
                flush=True,
            )
            proxy.kill()


async def _probe_slot_health(
    port: int, *, attempts: int = 2, delay: float = 3.0
) -> bool:
    """Pre-flight a freshly-spawned slot proxy with a ``tools/list`` JSON-RPC.

    POSTs to ``http://127.0.0.1:{port}/mcp`` and returns True when the proxy
    answers with an HTTP 2xx (it is alive and serving MCP). Retries up to
    *attempts* times with *delay* seconds between tries — a proxy can accept the
    Popen but still be binding its port for a second or two. (d71ba2e7)
    """
    import httpx

    payload = {"jsonrpc": "2.0", "id": "preflight", "method": "tools/list", "params": {}}
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    for attempt in range(max(1, attempts)):
        try:
            async with httpx.AsyncClient(timeout=10.0) as c:
                r = await c.post(
                    f"http://127.0.0.1:{port}/mcp", json=payload, headers=headers
                )
            if r.status_code < 400:
                return True
        except Exception:  # noqa: BLE001 — connection refused / not up yet
            pass
        if attempt + 1 < max(1, attempts):
            await asyncio.sleep(delay)
    return False


async def _report_slot_health(
    ws, label: str, healthy: bool, *, reason: str | None = None, detail: str | None = None
) -> None:
    """Send a ``plugin_status`` control message up the slot's WebSocket so the
    server can suppress (or restore) this slot's tools. Best-effort. (d71ba2e7)

    9a8645c1 — an unhealthy report may carry a ``reason`` (e.g. ``access_denied``)
    and a human-readable ``detail`` so the dashboard shows an actionable warning
    instead of a silent dead dot."""
    msg: dict = {"type": "plugin_status", "slot": label, "healthy": healthy}
    if reason:
        msg["reason"] = reason
    if detail:
        msg["detail"] = detail
    try:
        await ws.send(json.dumps(msg))
    except Exception:  # noqa: BLE001 — never let health reporting break the relay
        pass


# 9a8645c1 — actionable hint shown when Serena (the extract slot) can't start.
_SERENA_ACCESS_DENIED_HINT = (
    "Serena could not read the repo (access denied). Point the tunnel at a "
    "SPECIFIC repo dir, not a parent like Documents — a parent often contains "
    "broken Windows junctions (e.g. 'My Music') that Serena trips on. Use "
    "`meridian --tunnel --repo C:\\path\\to\\your-repo` (the first --repo path is "
    "the active repo)."
)


def _classify_serena_failure(err: object) -> "tuple[str, str] | None":
    """Classify a Serena spawn/startup failure from an exception or stderr text.

    Returns ``(reason, detail)`` — ``("access_denied", <hint>)`` for a
    PermissionError / WinError 5 / 'access is denied' signature, else None so the
    caller falls back to a generic reason. Pure + unit-tested. (9a8645c1)"""
    if isinstance(err, PermissionError):
        return ("access_denied", _SERENA_ACCESS_DENIED_HINT)
    text = (str(err) or "").lower()
    if not text:
        return None
    if ("winerror 5" in text or "access is denied" in text
            or "permissionerror" in text or "errno 13" in text):
        return ("access_denied", _SERENA_ACCESS_DENIED_HINT)
    return None


# 089a936a — human-readable hint per slot when its first-spawn pre-flight probe
# fails (the proxy accepted the Popen but never answered a `tools/list`). The
# dc/office hint is specific (cold npx download); everything else gets a generic
# "proxy didn't respond" message. Keyed on the slot label used by the tunnel.
_DC_PREFLIGHT_HINT = (
    "Desktop Commander did not respond on port {port} — if it isn't installed, "
    "add it via the dashboard (npx -y @wonderwhy-er/desktop-commander@latest); "
    "the first launch can take ~a minute to download."
)
_OFFICE_PREFLIGHT_HINTS = {
    "ppt": (
        "The PowerPoint slot did not respond on port {port} — its launcher may "
        "not be installed yet; the first launch can take a while to download."
    ),
    "word": (
        "The Word slot did not respond on port {port} — its launcher may not be "
        "installed yet; the first launch can take a while to download."
    ),
}


def _preflight_failure_hint(label: str, port: int) -> "tuple[str, str]":
    """Classify a first-spawn pre-flight probe failure into ``(reason, detail)``.

    The proxy Popen-succeeded but never answered ``tools/list``. ``reason`` is a
    short snake-case code consistent with :func:`_classify_serena_failure` /
    ``_mark_unhealthy`` (which use e.g. ``access_denied``); here it is
    ``unreachable``. ``detail`` is a human, slot-aware hint — specific for the
    dc/office cold-fetch slots, generic otherwise. Pure + unit-tested. (089a936a)"""
    reason = "unreachable"
    if label == "dc":
        return (reason, _DC_PREFLIGHT_HINT.format(port=port))
    office = _OFFICE_PREFLIGHT_HINTS.get(label)
    if office is not None:
        return (reason, office.format(port=port))
    return (
        reason,
        f"The {label} slot's proxy did not respond on port {port} — its tools "
        "are suppressed until it comes up (auto-retried in the background).",
    )


async def _reprobe_once(proxy, probe) -> bool:
    """One slot-recovery attempt (a898710a). Ensure the proxy is running, probe
    its health, and — critically — if it is running but UNHEALTHY (a persistent
    slot like Desktop Commander whose inner MCP server died while the parent
    ``cmd /c npx`` stays alive, so ``ensure_running()`` is a no-op) force a
    kill + respawn and re-probe. Without this a stuck persistent slot never
    recovers. Returns True when healthy. ``probe`` is an async fn(port) -> bool.
    """
    if not proxy.is_running:
        await proxy.ensure_running()
    healthy = proxy.is_running and await probe(proxy.port)
    if not healthy and proxy.is_running:
        proxy.kill()
        await proxy.ensure_running()
        healthy = proxy.is_running and await probe(proxy.port)
    return healthy


def _relay_timed_out(resp: object) -> bool:
    """True iff a ``_relay_request`` result carries the private timeout marker.

    3bde892a — ``_relay_request`` tags a genuine ``httpx.TimeoutException`` with a
    private ``_timed_out`` key (a connection-refused / bad-response 502 does NOT
    get it). This reader keeps the ``dict``/key check in one place; it never
    raises on a non-dict/None result (a missing key ⇒ not a timeout)."""
    return bool(isinstance(resp, dict) and resp.get("_timed_out"))


async def _kill_on_request_timeout(proxy) -> None:
    """Force-kill a SlotProxy after a request-level timeout so the NEXT request
    re-triggers ``ensure_running()`` (3bde892a).

    The bug this closes: a request that times out against a freshly-lazy-spawned
    slot (mcp-proxy's ``createServer()`` hung on the MCP ``initialize`` handshake
    with no internal timeout) is reported as a 502, but the mcp-proxy process is
    left alive — and because :meth:`SlotProxy.is_running` falls back to a port
    check, a zombie proxy still bound to the port keeps ``is_running`` True, so
    ``ensure_running()`` stays a no-op and the slot is silently dead until the
    whole tunnel restarts. Killing it here reuses the same kill+respawn recovery
    a failed health probe already gets via :func:`_reprobe_once`: the slot is
    torn down now and re-spawned on the next request. Best-effort — a kill that
    itself raises must never break the relay loop.

    Scoped so it only fires on an *actual* timeout (the caller gates on
    :func:`_relay_timed_out`), never on a normal slow-but-successful request."""
    try:
        if proxy.is_running:
            print(
                f"tunnel:{getattr(proxy, 'label', '?')}: request timed out against "
                f"the slot on port {getattr(proxy, 'port', '?')} — force-killing the "
                "proxy so the next request respawns it (watchdog)",
                file=sys.stderr, flush=True,
            )
            proxy.kill()
    except Exception:  # noqa: BLE001 — recovery must never break the relay loop
        pass


async def _preflight_slot(
    ws,
    port: int,
    label: str,
    *,
    attempts: int | None = None,
    delay: float | None = None,
) -> bool:
    """Probe a slot after its first spawn; report unhealthy on failure. Returns
    the health result so callers can log it. (d71ba2e7)

    089a936a — two robustness additions, both scoped to the failure path (the
    HEALTHY path is unchanged):

    * On failure, report a ``reason``/``detail`` (via :func:`_preflight_failure_hint`)
      instead of a bare unhealthy dot, so the dashboard shows an actionable hint
      — matching what the Serena slot already does with
      :func:`_classify_serena_failure`.
    * ``attempts``/``delay`` override the pre-flight probe budget so cold-fetch
      slots (dc/office, which npx-download on first launch) can be given a longer
      first-spawn budget. When omitted, the default budget is derived from the
      slot label: cold-fetch slots use ``_PREFLIGHT_BUDGET_COLD_FETCH``, all
      others ``_PREFLIGHT_BUDGET_DEFAULT``.
    """
    if attempts is None or delay is None:
        _def_attempts, _def_delay = (
            _PREFLIGHT_BUDGET_COLD_FETCH
            if label in _COLD_FETCH_SLOTS
            else _PREFLIGHT_BUDGET_DEFAULT
        )
        if attempts is None:
            attempts = _def_attempts
        if delay is None:
            delay = _def_delay
    healthy = await _probe_slot_health(port, attempts=attempts, delay=delay)
    if not healthy:
        reason, detail = _preflight_failure_hint(label, port)
        print(
            f"tunnel:{label}: pre-flight health check FAILED on port {port} — "
            f"marking slot unhealthy (its tools will be suppressed): {detail}",
            file=sys.stderr, flush=True,
        )
        await _report_slot_health(ws, label, False, reason=reason, detail=detail)
    return healthy


async def _run_connection_lazy(
    ws_url: str,
    proxy: "SlotProxy",
    label: str = "fs",
    tool_prefix: str | None = None,
    known_repo_paths: "list[str] | None" = None,
) -> None:
    """Hold one WebSocket session open with lazy proxy spawning.

    Like ``_run_connection`` but uses a ``SlotProxy`` rather than a pre-spawned
    port number.  The proxy is started on the first incoming ``request`` message
    and its idle timer is reset on every request.

    Proxy startup failures surface as HTTP 503 (not 502) so the server-side
    relay returns a clear error rather than timing out, and the client retries
    at the next request naturally.

    *known_repo_paths* are trusted root anchors: if a path-not-allowed error
    falls under one of these roots the proxy is automatically expanded and the
    request retried once (b9d1b606).
    """
    import httpx
    import websockets

    _known: list[str] = list(known_repo_paths or [])
    # d71ba2e7 — one pre-flight health check per connection, fired right after
    # the first lazy spawn so a proxy that Popen-succeeds but never serves is
    # reported unhealthy instead of silently 503-ing on every tool call.
    # a3410a9c — track consecutive spawn failures and, once they exhaust the
    # retry budget (or the pre-flight fails), mark the slot unhealthy and start a
    # background re-probe that auto-recovers it when the proxy serves again.
    _preflight_done = False
    _spawn_failures = 0
    _unhealthy = False
    _reprobe_task: "asyncio.Task | None" = None

    async def _reprobe() -> None:
        """Background recovery: retry spawn + tools/list every
        _SLOT_REPROBE_INTERVAL; on the first healthy probe, re-advertise."""
        nonlocal _unhealthy, _reprobe_task
        while True:
            await asyncio.sleep(_SLOT_REPROBE_INTERVAL)
            try:
                # a898710a — _reprobe_once force-restarts a persistent slot that's
                # alive-but-unhealthy (parent process up, inner MCP server dead).
                if await _reprobe_once(
                    proxy, lambda port: _probe_slot_health(port, attempts=1)
                ):
                    _unhealthy = False
                    _reprobe_task = None
                    await _report_slot_health(ws, label, True)
                    print(
                        f"tunnel:{label}: slot recovered — re-advertising tools",
                        flush=True,
                    )
                    return
            except Exception:  # noqa: BLE001 — keep retrying until cancelled
                pass

    async def _mark_unhealthy(*, already_reported: bool = False) -> None:
        """Suppress this slot's tools and begin background recovery. Idempotent."""
        nonlocal _unhealthy, _reprobe_task
        if _unhealthy:
            return
        _unhealthy = True
        if not already_reported:
            await _report_slot_health(ws, label, False)
        if _reprobe_task is None:
            _reprobe_task = asyncio.ensure_future(_reprobe())

    try:
        async with websockets.connect(ws_url, max_size=None, ping_interval=20) as ws:
            print(f"tunnel:{label}: connected (lazy mode)", flush=True)
            local_base = f"http://127.0.0.1:{proxy.port}"
            async with httpx.AsyncClient() as http_client:
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except (ValueError, TypeError):
                        continue
                    if not isinstance(msg, dict):
                        continue
                    if msg.get("type") == "ping":
                        continue
                    if msg.get("type") == "add_fs_roots":
                        # Control message: expand the filesystem proxy's allowed dirs.
                        new_roots: list[str] = msg.get("roots") or []
                        updated_cmd, changed = _add_fs_roots_to_cmd(proxy.cmd, new_roots)
                        if changed:
                            proxy.cmd = updated_cmd
                            if proxy.is_running:
                                proxy.kill()
                                await proxy.ensure_running()
                            print(
                                f"tunnel:{label}: expanded allowed dirs → {new_roots}",
                                flush=True,
                            )
                        continue
                    if msg.get("type") == "set_fs_roots":
                        # live-fs-roots — full-list REPLACE: rebuild the fs proxy's
                        # allowed dirs to EXACTLY these roots (a removal shrinks the
                        # served set, which add_fs_roots cannot do), then respawn.
                        want_roots: list[str] = msg.get("roots") or []
                        updated_cmd, changed = _set_fs_roots_on_cmd(proxy.cmd, want_roots)
                        if changed:
                            proxy.cmd = updated_cmd
                            if proxy.is_running:
                                proxy.kill()
                                await proxy.ensure_running()
                            served = [
                                d for d in (_normalize_path_arg(r) for r in want_roots) if d
                            ]
                            print(
                                f"tunnel:{label}: set allowed dirs → {served}",
                                flush=True,
                            )
                        continue
                    if msg.get("type") == "run_cmd":
                        # 0e973e52 — run_verification: spawn test_cmd as a real local
                        # process and send the structured result back to the server.
                        # Only the FS slot handles this; other slots ignore it.
                        req_id = msg.get("id", "")
                        cmd_val = msg.get("cmd") or ""
                        cwd_val = (msg.get("cwd") or "").strip() or None
                        run_result = await _handle_run_cmd(cmd_val, cwd_val)
                        run_result["type"] = "run_cmd_result"
                        run_result["id"] = req_id
                        await ws.send(json.dumps(run_result))
                        continue
                    if msg.get("type") == "request":
                        # Lazy spawn: bring the proxy up if it died or was idle-killed.
                        _was_running = proxy.is_running
                        if not proxy.is_running:
                            await proxy.ensure_running()
                        if not proxy.is_running:
                            # Spawn failed — count it; escalate to unhealthy once
                            # the slot exhausts its retry budget (a3410a9c).
                            _spawn_failures += 1
                            if _spawn_failures > _WATCHDOG_MAX_RETRIES:
                                await _mark_unhealthy()
                            # Return 503 so the server doesn't time out.
                            req_id = msg.get("id")
                            err = json.dumps({"error": "local proxy not available"}).encode()
                            await ws.send(json.dumps({
                                "type": "response",
                                "id": req_id,
                                "status": 503,
                                "headers": {"content-type": "application/json"},
                                "body": base64.b64encode(err).decode(),
                            }))
                            continue
                        _spawn_failures = 0
                        # Pre-flight the proxy once, the first time we bring it up.
                        if not _preflight_done and not _was_running:
                            _preflight_done = True
                            if not await _preflight_slot(ws, proxy.port, label):
                                # _preflight_slot already reported unhealthy; just
                                # start the background re-probe.
                                await _mark_unhealthy(already_reported=True)
                        proxy.touch()
                        resp = await _relay_request(
                            http_client, local_base, msg, tool_prefix=tool_prefix
                        )
                        # Auto-add: if access was denied and the path falls under
                        # a known repo_path, silently expand and retry once (b9d1b606).
                        denied_path = _extract_denied_path(resp)
                        if denied_path and _known:
                            try:
                                dp = Path(denied_path).resolve()
                            except Exception:
                                dp = None
                            if dp is not None:
                                anchor = next(
                                    (
                                        r for r in _known
                                        if _is_subpath(dp, Path(r))
                                    ),
                                    None,
                                )
                                if anchor:
                                    updated_cmd, changed = _add_fs_roots_to_cmd(
                                        proxy.cmd, [anchor]
                                    )
                                    if changed:
                                        proxy.cmd = updated_cmd
                                        proxy.kill()
                                        await proxy.ensure_running()
                                        if proxy.is_running:
                                            print(
                                                f"tunnel:{label}: auto-added {anchor!r} "
                                                f"(denied: {denied_path!r})",
                                                flush=True,
                                            )
                                            resp = await _relay_request(
                                                http_client, local_base, msg,
                                                tool_prefix=tool_prefix,
                                            )
                        # 3bde892a — request-timeout watchdog. If the relay to a
                        # freshly-lazy-spawned slot TIMED OUT (mcp-proxy hung on
                        # the MCP initialize handshake — no internal timeout in
                        # createServer()), the proxy may be a zombie still holding
                        # the port, which keeps is_running True and makes
                        # ensure_running() a no-op forever. Force-kill it now so
                        # the NEXT request respawns it — the same kill+respawn
                        # recovery a failed health probe gets via _reprobe_once.
                        # Only fires on an actual timeout, never a slow-but-OK req.
                        if _relay_timed_out(resp):
                            resp.pop("_timed_out", None)  # never send on the wire
                            await _kill_on_request_timeout(proxy)
                        await ws.send(json.dumps(resp))
    finally:
        # Stop the background re-probe when the connection closes so it doesn't
        # outlive the WebSocket it reports on (a3410a9c).
        if _reprobe_task is not None:
            _reprobe_task.cancel()


async def _reconnect_loop_lazy(
    ws_url: str,
    proxy: "SlotProxy",
    label: str,
    tool_prefix: str | None = None,
    known_repo_paths: "list[str] | None" = None,
) -> None:
    """Keep one lazy-spawn tunnel alive, reconnecting with exponential backoff."""
    backoff = 1.0
    while True:
        try:
            await _run_connection_lazy(
                ws_url, proxy, label,
                tool_prefix=tool_prefix,
                known_repo_paths=known_repo_paths,
            )
            backoff = 1.0
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(
                f"tunnel:{label}: disconnected ({exc}); reconnecting in {backoff:.0f}s",
                file=sys.stderr,
                flush=True,
            )
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, _MAX_BACKOFF)


async def _run_extract_pool_connection(
    ws_url: str,
    pool: "SerenaDaemonPool",
    default_repo_path: str,
    label: str = "extract",
    tool_prefix: str | None = None,
) -> None:
    """Hold one WebSocket open for the code-extractor slot, routed via the pool.

    64650cb4 — unlike :func:`_run_connection_lazy` (one fixed proxy port) each
    incoming request is routed to the Serena daemon for the caller's repo_path:
    the ``X-Meridian-Repo-Path`` header picks the repo (falling back to the
    tunnel's ``default_repo_path``) and the matching daemon is spawned on demand.
    Spawn/route failures come back as HTTP 503 so the server doesn't time out.
    """
    import httpx
    import websockets

    # d71ba2e7 — one pre-flight per connection after the first daemon spawn.
    # a3410a9c — escalate a dead extract slot to unhealthy and background-reprobe.
    _preflight_done = False
    _unhealthy = False
    _reprobe_task: "asyncio.Task | None" = None

    async def _reprobe(ws) -> None:
        """Retry spawning the default-repo daemon every _SLOT_REPROBE_INTERVAL;
        re-advertise the extract slot once one comes up healthy. (a3410a9c)"""
        nonlocal _unhealthy, _reprobe_task
        target = pool.default_repo_path or default_repo_path
        while True:
            await asyncio.sleep(_SLOT_REPROBE_INTERVAL)
            try:
                d = pool.get_or_spawn(target)
                if d is not None and d.is_alive and await _probe_slot_health(d.port, attempts=1):
                    _unhealthy = False
                    _reprobe_task = None
                    await _report_slot_health(ws, label, True)
                    print(
                        f"tunnel:{label}: slot recovered — re-advertising tools",
                        flush=True,
                    )
                    return
            except Exception:  # noqa: BLE001 — keep retrying until cancelled
                pass

    async def _mark_unhealthy(
        ws, *, already_reported: bool = False,
        reason: str | None = None, detail: str | None = None,
    ) -> None:
        nonlocal _unhealthy, _reprobe_task
        if _unhealthy:
            return
        _unhealthy = True
        if not already_reported:
            await _report_slot_health(ws, label, False, reason=reason, detail=detail)
        if _reprobe_task is None:
            _reprobe_task = asyncio.ensure_future(_reprobe(ws))

    try:
        async with websockets.connect(ws_url, max_size=None, ping_interval=20) as ws:
            print(f"tunnel:{label}: connected (Serena daemon pool)", flush=True)
            async with httpx.AsyncClient() as http_client:
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except (ValueError, TypeError):
                        continue
                    if not isinstance(msg, dict):
                        continue
                    if msg.get("type") == "ping":
                        continue
                    if msg.get("type") == "set_active_repo":
                        new_path = str(msg.get("repo_path") or "").strip()
                        if new_path:
                            pool.default_repo_path = pool._normalize(new_path)
                            print(
                                f"tunnel:extract: active repo → {pool.default_repo_path}",
                                flush=True,
                            )
                        continue
                    if msg.get("type") != "request":
                        continue
                    req_id = msg.get("id")
                    repo_path = _serena_pool.resolve_repo_path(
                        msg.get("headers") or {}, pool.default_repo_path or default_repo_path
                    )
                    daemon = None
                    _spawn_exc: object = None
                    try:
                        daemon = pool.get_or_spawn(repo_path)
                    except Exception as exc:  # noqa: BLE001 — spawn failure → 503
                        _spawn_exc = exc
                        print(
                            f"tunnel:{label}: failed to start Serena for {repo_path}: {exc}",
                            file=sys.stderr, flush=True,
                        )
                    if daemon is None or not daemon.is_alive:
                        # First-spawn failure → mark unhealthy (suppress tools)
                        # and start the background re-probe (d71ba2e7 + a3410a9c).
                        # 9a8645c1 — classify access-denied (broken-junction scans)
                        # so the dashboard shows an actionable warning + bad path.
                        if not _preflight_done:
                            _preflight_done = True
                            _cls = _classify_serena_failure(_spawn_exc) if _spawn_exc else None
                            if _cls is not None:
                                _reason, _detail = _cls
                            else:
                                _reason, _detail = ("extract_unavailable", _SERENA_ACCESS_DENIED_HINT)
                            print(f"tunnel:{label}: {_detail} (repo: {repo_path})",
                                  file=sys.stderr, flush=True)
                            await _mark_unhealthy(ws, reason=_reason, detail=_detail)
                        err = json.dumps(
                            {"error": f"Serena daemon for {repo_path} not available"}
                        ).encode()
                        await ws.send(json.dumps({
                            "type": "response",
                            "id": req_id,
                            "status": 503,
                            "headers": {"content-type": "application/json"},
                            "body": base64.b64encode(err).decode(),
                        }))
                        continue
                    # First successful daemon — pre-flight its MCP port once.
                    if not _preflight_done:
                        _preflight_done = True
                        if not await _preflight_slot(ws, daemon.port, label):
                            await _mark_unhealthy(ws, already_reported=True)
                    resp = await _relay_request(
                        http_client, f"http://127.0.0.1:{daemon.port}", msg,
                        tool_prefix=tool_prefix,
                    )
                    # 3bde892a — the private timeout marker is an in-process
                    # signal only; strip it so it never leaks onto the wire. (The
                    # extract slot recovers stuck daemons via its own pool reap +
                    # reprobe, so no kill hook is wired here.)
                    if isinstance(resp, dict):
                        resp.pop("_timed_out", None)
                    await ws.send(json.dumps(resp))
    finally:
        if _reprobe_task is not None:
            _reprobe_task.cancel()


async def _reconnect_loop_extract_pool(
    ws_url: str,
    pool: "SerenaDaemonPool",
    default_repo_path: str,
    label: str = "extract",
    tool_prefix: str | None = None,
) -> None:
    """Keep the pooled code-extractor tunnel alive, reconnecting with backoff."""
    backoff = 1.0
    while True:
        try:
            await _run_extract_pool_connection(
                ws_url, pool, default_repo_path, label, tool_prefix=tool_prefix
            )
            backoff = 1.0
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(
                f"tunnel:{label}: disconnected ({exc}); reconnecting in {backoff:.0f}s",
                file=sys.stderr,
                flush=True,
            )
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, _MAX_BACKOFF)


async def _pool_idle_reaper(
    pool: "SerenaDaemonPool", idle_seconds: float = _IDLE_KILL_SECONDS
) -> None:
    """Periodically reap Serena daemons idle past the TTL (mirrors _idle_killer)."""
    poll_interval = max(60.0, idle_seconds / 6)
    while True:
        await asyncio.sleep(poll_interval)
        reaped = pool.reap_idle()
        for repo_path in reaped:
            print(
                f"tunnel:extract: Serena for {repo_path} idle >"
                f"{idle_seconds / 60:.0f}min — killed (restarts on next request)",
                flush=True,
            )


def _spawn_kwargs() -> dict:
    """Extra ``subprocess.Popen`` kwargs for spawning a proxy child.

    On Windows, ``CREATE_NEW_PROCESS_GROUP`` puts each proxy in its own group so a
    console Ctrl+C (``CTRL_C_EVENT``, broadcast to the whole foreground group) is
    NOT delivered to the children. That broadcast is what makes the cmd.exe shims
    behind ``npx``/``mcp-proxy`` pop "Terminate batch job (Y/N)?" and hang the
    terminal on shutdown. We tear the children down explicitly instead (see
    :func:`_terminate_proc_tree`). No-op on POSIX.
    """
    if sys.platform == "win32":
        # CREATE_NEW_PROCESS_GROUP (0x00000200) is defined by the stdlib only on
        # Windows. Reference it via getattr with the literal fallback so the win32
        # branch is also evaluable on a non-Windows CI host (where a test may
        # monkeypatch sys.platform to "win32"); on real Windows getattr returns the
        # genuine constant.
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)}
    return {}


def _plugin_spawn_env(env: object) -> "dict[str, str] | None":
    """Merge a plugin's optional ``env`` overrides over the parent process env
    for ``subprocess.Popen``. Returns ``None`` (inherit the parent env) when
    there is nothing valid to override. Keys/values are coerced to str and blank
    keys dropped. Shared by the Office and custom local-plugin spawns (194a7776
    — a local Zotero MCP needs ``ZOTERO_LOCAL=true``)."""
    if not isinstance(env, dict) or not env:
        return None
    coerced = {str(k): str(v) for k, v in env.items() if str(k)}
    if not coerced:
        return None
    return {**os.environ, **coerced}


# 2b04a361 — force UTF-8 stdio for the spawned Office-slot MCP servers (ppt/word/
# dc). These are third-party Python MCP servers (docx-mcp on the `word` slot,
# powerpoint-mcp on `ppt`) that run their OWN logging. On Windows the child
# Python inherits the console's legacy cp1252 encoding for sys.stdout/stderr, so
# the moment the server logs a non-ASCII message — e.g. a docx-mcp log line with
# Chinese characters — its logger raises UnicodeEncodeError and the slot crashes.
# This is the third instance of the known Windows-encoding failure class (after
# hitl_guard's em-dash and install.ps1's UTF-16). We fix it at the spawn site by
# telling the child Python to encode its own stdio as UTF-8 with the ``replace``
# error handler (PYTHONIOENCODING=utf-8:replace — the documented
# ``<encoding>:<errorhandler>`` form) and to run in UTF-8 mode (PYTHONUTF8=1,
# belt-and-suspenders). The ``:replace`` handler guarantees that a stray byte that
# still can't be encoded is substituted rather than raising — so a bad log line
# can never crash the child's logging OR take the slot down. (The launcher's own
# ``_force_utf8_io`` only exports plain ``utf-8`` with the default *strict* handler,
# so it does NOT cover an unencodable byte; we harden the office children here.)
# We do NOT pipe the child's stdout/stderr (they inherit the console fds), so this
# env-level fix is the correct and minimal lever: it configures the child's own
# encoding, not a launcher-side decode.
_UTF8_STDIO_ENV = {"PYTHONIOENCODING": "utf-8:replace", "PYTHONUTF8": "1"}


def _office_slot_spawn_env(env: object) -> "dict[str, str]":
    """Spawn env for an Office slot (ppt/word/dc), with UTF-8 stdio forced.

    Starts from :func:`_plugin_spawn_env` (the plugin's optional ``env`` merged
    over the parent process env) and layers the UTF-8 stdio vars on top so the
    spawned third-party Python MCP server writes UTF-8 to stdout/stderr regardless
    of the parent console's legacy code page (2b04a361 — fixes docx-mcp's own
    logger crashing on non-ASCII log lines under Windows cp1252). Unlike
    :func:`_plugin_spawn_env` this NEVER returns ``None``: even a slot with no
    plugin env still needs the encoding override, so we always materialise a full
    env dict (parent env + the UTF-8 vars). A plugin ``env`` key that collides with
    one of the UTF-8 vars is intentionally overridden — correct stdio encoding is
    non-negotiable for these Python servers.
    """
    base = _plugin_spawn_env(env)
    merged = dict(base) if base is not None else dict(os.environ)
    merged.update(_UTF8_STDIO_ENV)
    return merged


def _terminate_proc_tree(proc: "subprocess.Popen | None") -> None:
    """Stop a spawned proxy *and its whole child tree*. Best-effort; never raises.

    On Windows ``proc.terminate()`` only kills the direct child (the mcp-proxy
    launcher), orphaning the node/cmd grandchildren — and, combined with a console
    Ctrl+C, leaving the "Terminate batch job (Y/N)?" prompt. ``taskkill /F /T``
    kills the entire tree by PID. On POSIX a terminate→wait→kill escalation is
    enough.
    """
    if proc is None:
        return
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True, check=False,
            )
        except Exception:  # noqa: BLE001 — fall back to terminate below
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001
                pass
        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
        return
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


def _kill_stale_port_occupant(port: int, label: str) -> None:
    """Kill whatever process is already listening on ``port``, if any (44892730).

    Root cause: each ``SlotProxy`` starts life with ``_proc = None``, and its
    ``is_running`` check deliberately skips the port probe in that state
    (c8e6b61c — so a never-started slot isn't falsely "revived" by an unrelated
    process that happens to be squatting on the port). That's correct for a
    genuinely unrelated process, but wrong for the actual common case behind
    the confirmed duplicate-process bug: a PRIOR GENERATION of this same
    ``meridian --tunnel`` invocation whose child subprocess survived the parent
    exiting (crash, Ctrl+C race, or a restart that didn't wait for full
    teardown) and is still bound to the slot's port. The new generation's fresh
    ``SlotProxy`` has no handle to that orphan, so ``ensure_running`` spawns a
    second live process for the same logical slot.

    Calling this once per slot at tunnel startup, before the first
    ``ensure_running``, closes that gap directly: if anything is already
    listening on the slot's port, kill it first so the fresh spawn gets a
    clean port. Best-effort and silent on any failure (missing psutil,
    permission error, etc.) — this must never block tunnel startup.

    Note: this intentionally does NOT attempt the fuller per-client
    session-to-process liveness/routing distinction the underlying item
    describes (whether an existing process is genuinely still serving another
    live, active client vs. orphaned) — that needs the tunnel to track client
    identity per process instance, which does not exist anywhere in this
    module today and is real, separate follow-up work. This fix only handles
    the narrower, confirmed trigger: leftover processes from a previous
    tunnel-client generation.
    """
    try:
        import psutil  # type: ignore
    except Exception:  # noqa: BLE001 - psutil unavailable; nothing we can do
        return
    try:
        for conn in psutil.net_connections(kind="inet"):
            if (
                conn.status == psutil.CONN_LISTEN
                and conn.laddr
                and conn.laddr.port == port
                and conn.pid
            ):
                pid = conn.pid
                print(
                    f"tunnel:{label}: killing stale prior-generation process "
                    f"(pid {pid}) still bound to port {port} before spawning fresh",
                    flush=True,
                )
                if sys.platform == "win32":
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(pid)],
                        capture_output=True, check=False,
                    )
                else:
                    try:
                        stale = psutil.Process(pid)
                        stale.terminate()
                        stale.wait(timeout=5)
                    except Exception:  # noqa: BLE001
                        try:
                            psutil.Process(pid).kill()
                        except Exception:  # noqa: BLE001
                            pass
    except Exception:  # noqa: BLE001 - best-effort, never block tunnel startup
        pass


def _dc_default_command() -> list[str]:
    """Default Desktop Commander launcher, shell-wrapped per OS.

    DC ships only as an npm package, so the launcher is
    ``npx -y @wonderwhy-er/desktop-commander@latest``. On Windows ``npx`` is the
    extension-less shim: spawned directly by mcp-proxy (no ``--shell``, since the
    first token isn't a ``.cmd``/``.bat`` path) it raises ``ENOENT`` and the
    watchdog spin-loops. Wrapping in ``cmd /c`` makes mcp-proxy spawn the real
    ``cmd.exe``, which resolves ``npx`` via ``PATHEXT``. POSIX needs no wrapper.
    """
    base = ["npx", "-y", "@wonderwhy-er/desktop-commander@latest"]
    if sys.platform == "win32":
        return ["cmd", "/c", *base]
    return base


def _office_slot_command(slot: str, plugin: "dict | None") -> "list[str] | None":
    """Resolve the runnable inner command for an office slot (ppt/word/dc).

    Returns the command token list, or ``None`` when the slot has no runnable
    command. ``dc`` falls back to :func:`_dc_default_command` when its plugin
    leaves ``command`` unset (its launcher is spawned via npx, not a stored
    command); ``ppt``/``word`` have no such fallback, so a missing/empty command
    yields ``None``. Pure — no I/O — so both :func:`run_tunnel` and the unit
    tests share one source of truth. (0dfb107e)
    """
    cmd = (plugin or {}).get("command")
    if cmd is None and slot == "dc":
        cmd = _dc_default_command()
    return cmd or None


def _office_slot_warning(slot: str, human: str, plugin: "dict | None") -> "str | None":
    """Return a startup WARNING line iff an office slot is misconfigured (0dfb107e).

    The bug this closes: an office slot that is ENABLED but has no runnable
    command (a stored override that coerced to empty) was silently ``continue``-d
    in :func:`run_tunnel`, so the slot vanished from the entire startup log with
    ZERO warning — leaving the operator no way to see WHY an expected slot was
    absent. The filesystem slot already warns on an analogous
    "configured-but-won't-be-served" case (``_unservable_roots``); this mirrors
    that pattern for the office slots.

    Returns:
      * ``None`` when the slot is DISABLED (off-by-default, opt-in — silence is
        correct: it is not an "expected" slot) OR when it is enabled AND has a
        runnable command (healthy — no warning).
      * a one-line WARNING string (destined for stderr) when the slot is enabled
        but has no runnable command.

    Pure — no I/O — so it is unit-testable with no server/port/network.
    """
    plugin = plugin or {}
    if not plugin.get("enabled", False):
        return None
    if _office_slot_command(slot, plugin):
        return None
    return (
        f"  {human.lower():<16}WARNING enabled but no command configured "
        "— slot will NOT be served (check its tunnel_plugins command)."
    )


def _force_utf8_io() -> None:
    """Make stdio UTF-8 so the tunnel's Unicode status output can't crash.

    Windows consoles default to cp1252; printing the URLs, ✓ marks, and box
    characters in the startup banner would raise ``UnicodeEncodeError`` and kill
    the tunnel. Setting ``PYTHONIOENCODING`` propagates UTF-8 to the spawned
    proxy children, and reconfiguring the live streams fixes the already-started
    parent process (the env var alone is read only at interpreter startup).
    """
    os.environ["PYTHONIOENCODING"] = "utf-8"
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 — non-TextIOWrapper streams (e.g. captured in tests)
            pass


# ---------------------------------------------------------------------------
# Config resolution (pure — unit tested)
# ---------------------------------------------------------------------------

def _resolve_token(arg_token: str | None = None) -> str:
    """Resolve the API token: CLI arg > MERIDIAN_API_KEY > BEARER_TOKEN.

    A leading ``Bearer `` prefix (e.g. copied from a header) is stripped.
    """
    token = (
        arg_token
        or os.environ.get("MERIDIAN_API_KEY")
        or os.environ.get("BEARER_TOKEN")
        or ""
    ).strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    return token


def _resolve_base_url(arg_url: str | None = None) -> str:
    """Resolve the server base URL: CLI arg > MERIDIAN_URL > default."""
    url = (arg_url or os.environ.get("MERIDIAN_URL") or DEFAULT_BASE_URL).strip()
    return url.rstrip("/")


# ---------------------------------------------------------------------------
# Token cache — ~/.meridian/config.json (30-day expiry, per base_url)
# ---------------------------------------------------------------------------

def _config_path() -> Path:
    return Path.home() / ".meridian" / "config.json"


# ---------------------------------------------------------------------------
# Package cache locations — shown once on first tunnel run (a887155d)
# ---------------------------------------------------------------------------

def _package_cache_locations() -> dict[str, str]:
    """Where npx (Node MCPs) and uvx (Python MCPs) land downloaded packages.

    Derived from the package managers' env vars with per-OS defaults — no
    subprocess is spawned, so this never slows tunnel startup. Best-effort:
    the paths are the documented defaults, accurate unless the user overrode
    them outside the standard env vars.
    """
    home = Path.home()
    local = os.environ.get("LOCALAPPDATA")
    win = sys.platform == "win32"

    npm_cache = os.environ.get("npm_config_cache")
    if not npm_cache:
        npm_cache = str(Path(local) / "npm-cache") if (win and local) else str(home / ".npm")

    uv_cache = os.environ.get("UV_CACHE_DIR")
    if not uv_cache:
        uv_cache = str(Path(local) / "uv" / "cache") if (win and local) else str(home / ".cache" / "uv")

    uv_tools = os.environ.get("UV_TOOL_DIR")
    if not uv_tools:
        uv_tools = str(Path(local) / "uv" / "tools") if (win and local) else str(home / ".local" / "share" / "uv" / "tools")

    return {
        "npx": str(Path(npm_cache) / "_npx"),
        "uvx": uv_cache,
        "uv_tools": uv_tools,
    }


def _cache_locations_marker() -> Path:
    """Marker whose existence means cache locations were already shown once."""
    return Path.home() / ".meridian" / ".cache_locations_shown"


def _print_package_cache_locations(*, force: bool = False) -> bool:
    """Print where npx/uvx cache downloaded MCP servers — once, on first run.

    A marker file under ``~/.meridian`` suppresses the banner on later runs so
    repeat starts stay terse. Returns True if it printed. Best-effort: any FS
    error is swallowed (we'd just print again next time) and never blocks
    startup. (a887155d)
    """
    marker = _cache_locations_marker()
    if not force:
        try:
            if marker.exists():
                return False
        except OSError:
            pass
    loc = _package_cache_locations()
    print("  Package caches (first run — where downloaded MCP servers land):", flush=True)
    print(f"    npx (Node MCPs):   {loc['npx']}", flush=True)
    print(f"    uvx (Python MCPs): {loc['uvx']}", flush=True)
    print(f"    uv tools:          {loc['uv_tools']}", flush=True)
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("1", encoding="utf-8")
    except OSError:
        pass
    return True


def _read_cached_token(base_url: str) -> str | None:
    """Return a cached tunnel token for *base_url* if present and unexpired."""
    path = _config_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        entry = data.get("tunnel_token", {})
        if entry.get("base_url") != base_url:
            return None
        if time.time() > entry.get("expires_at", 0):
            return None
        return entry.get("token") or None
    except Exception:
        return None


def _write_cached_token(base_url: str, token: str) -> None:
    """Persist *token* to ``~/.meridian/config.json`` with a 30-day expiry."""
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    data["tunnel_token"] = {
        "token": token,
        "base_url": base_url,
        "expires_at": int(time.time()) + 30 * 24 * 3600,
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)
    try:
        path.chmod(0o600)
    except Exception:
        pass


async def _browser_auth_flow(base_url: str) -> str:
    """Open a browser to authorize tunnel access; poll until approved or timeout.

    Returns the raw API token on success, or an empty string on failure/cancel.
    The token is never written to shell history or passed via the process list —
    it lives only in memory until the caller caches it to disk.
    """
    import uuid as _uuid
    import webbrowser

    device_code = str(_uuid.uuid4())
    connect_url = f"{base_url}/auth/tunnel-connect?device_code={device_code}"
    poll_url = f"{base_url}/auth/tunnel-poll"

    print("", flush=True)
    print("No API token found. Opening browser to authorize tunnel access.", flush=True)
    print(f"  {connect_url}", flush=True)
    print("Waiting for authorization — press Ctrl-C to cancel (10 min timeout).", flush=True)
    print("", flush=True)

    try:
        webbrowser.open(connect_url)
    except Exception:
        pass  # headless servers — user can open the URL manually

    import httpx
    deadline = time.monotonic() + 600
    async with httpx.AsyncClient(timeout=10.0) as client:
        while time.monotonic() < deadline:
            try:
                r = await client.get(poll_url, params={"device_code": device_code})
                if r.status_code == 200:
                    data = r.json()
                    if data.get("status") == "complete":
                        token = data.get("token", "")
                        if token:
                            print("  authorized.", flush=True)
                            return token
                elif r.status_code == 404:
                    print("error: device code expired — please try again.", file=sys.stderr)
                    return ""
            except Exception:
                pass
            await asyncio.sleep(3)

    print("error: tunnel auth timed out (10 min). Run again to retry.", file=sys.stderr)
    return ""


def _ws_url(base_url: str, tenant_id: str, token: str) -> str:
    """Build the tunnel WebSocket URL with the token as a query param.

    The token is passed via ``?token=`` (which the server accepts) rather than
    an Authorization header so we don't depend on the ``websockets`` version's
    header kwarg name (``extra_headers`` vs ``additional_headers``).
    """
    base = base_url.rstrip("/")
    if base.startswith("https://"):
        ws_base = "wss://" + base[len("https://"):]
    elif base.startswith("http://"):
        ws_base = "ws://" + base[len("http://"):]
    else:
        ws_base = base
    from urllib.parse import quote
    return f"{ws_base}/tunnel/{tenant_id}?token={quote(token, safe='')}"


def _permanent_url(base_url: str, tenant_id: str) -> str:
    """The URL the user adds to claude.ai once.

    Points at the mcp-proxy **Streamable HTTP** transport (`/mcp`), NOT the
    proxy root: mcp-proxy serves transports at `/mcp` (streamable) and `/sse`
    (SSE) and returns 404 for `/`. The server route `/fs/mcp/{tenant_id}/{rest}`
    relays the `/mcp` suffix straight to the local proxy.
    """
    return f"{base_url.rstrip('/')}/fs/mcp/{tenant_id}/mcp"


def _sse_url(base_url: str, tenant_id: str) -> str:
    """SSE-transport variant of the permanent URL, for older MCP clients."""
    return f"{base_url.rstrip('/')}/fs/mcp/{tenant_id}/sse"


def _ws_code_url(base_url: str, tenant_id: str, token: str) -> str:
    """Build the codebase-memory-mcp tunnel WebSocket URL."""
    base = base_url.rstrip("/")
    if base.startswith("https://"):
        ws_base = "wss://" + base[len("https://"):]
    elif base.startswith("http://"):
        ws_base = "ws://" + base[len("http://"):]
    else:
        ws_base = base
    from urllib.parse import quote
    return f"{ws_base}/tunnel-code/{tenant_id}?token={quote(token, safe='')}"


def _permanent_code_url(base_url: str, tenant_id: str) -> str:
    """The URL for codebase-memory-mcp — add to claude.ai once."""
    return f"{base_url.rstrip('/')}/code/mcp/{tenant_id}/mcp"


def _ws_extract_url(base_url: str, tenant_id: str, token: str) -> str:
    """Build the mcp-server-code-extractor tunnel WebSocket URL."""
    base = base_url.rstrip("/")
    if base.startswith("https://"):
        ws_base = "wss://" + base[len("https://"):]
    elif base.startswith("http://"):
        ws_base = "ws://" + base[len("http://"):]
    else:
        ws_base = base
    from urllib.parse import quote
    return f"{ws_base}/tunnel-extract/{tenant_id}?token={quote(token, safe='')}"


def _permanent_extract_url(base_url: str, tenant_id: str) -> str:
    """The URL for mcp-server-code-extractor — add to claude.ai once."""
    return f"{base_url.rstrip('/')}/extract/mcp/{tenant_id}/mcp"


def _ws_office_url(base_url: str, tenant_id: str, token: str, slot: str) -> str:
    """Build the WebSocket URL for an Office tunnel slot (ppt/word)."""
    base = base_url.rstrip("/")
    if base.startswith("https://"):
        ws_base = "wss://" + base[len("https://"):]
    elif base.startswith("http://"):
        ws_base = "ws://" + base[len("http://"):]
    else:
        ws_base = base
    from urllib.parse import quote
    return f"{ws_base}/tunnel-{slot}/{tenant_id}?token={quote(token, safe='')}"


def _permanent_office_url(base_url: str, tenant_id: str, slot: str) -> str:
    """The permanent URL for an Office tunnel slot (ppt/word) — add to claude.ai once."""
    return f"{base_url.rstrip('/')}/{slot}/mcp/{tenant_id}/mcp"


def _find_uvx() -> "str | None":
    """Locate the ``uvx`` launcher (uv's ephemeral-tool runner)."""
    found = shutil.which("uvx")
    if found:
        return found
    # uv's standalone installer drops uvx in ~/.local/bin on every platform.
    name = "uvx.exe" if sys.platform == "win32" else "uvx"
    candidate = Path.home() / ".local" / "bin" / name
    if candidate.exists():
        return str(candidate)
    return None


def _resolve_extractor_inner_cmd() -> "list[str] | None":
    """Resolve the launcher for mcp-server-code-extractor (a **PyPI** package).

    It is published on PyPI, not npm. Preferred: ``uvx mcp-server-code-extractor``
    (zero install, ephemeral). Fallback: pip-install the package into the current
    interpreter's environment and run it as ``python -m code_extractor`` (the
    module is ``code_extractor``). Returns the inner-command token list, or None
    if neither path is available.
    """
    uvx = _find_uvx()
    if uvx:
        return [uvx, "mcp-server-code-extractor"]
    # Fallback: ensure the package is importable in this env, then run as a module.
    import importlib.util
    if importlib.util.find_spec("code_extractor") is None:
        print(
            "  code-extractor: uvx not found — pip installing mcp-server-code-extractor...",
            flush=True,
        )
        try:
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "mcp-server-code-extractor"],
                check=True,
            )
        except Exception as exc:  # noqa: BLE001
            print(
                f"  warning: could not pip install mcp-server-code-extractor: {exc}",
                file=sys.stderr, flush=True,
            )
            return None
    return [sys.executable, "-m", "code_extractor"]


def _build_extractor_proxy_command(
    npx: str, inner_cmd: list[str], port: int = DEFAULT_EXTRACT_PROXY_PORT
) -> list[str]:
    """Wrap *inner_cmd* (the code-extractor launcher) in mcp-proxy on *port*.

    mcp-server-code-extractor is a PyPI package, so *inner_cmd* is the resolved
    Python launcher — e.g. ``[uvx, "mcp-server-code-extractor"]`` or
    ``[python, "-m", "code_extractor"]`` (see :func:`_resolve_extractor_inner_cmd`).
    The OUTER launcher is still ``npx`` because mcp-proxy itself is an npm tool.

    On Windows, ``--shell`` is added only when the inner launcher is a ``.cmd``/
    ``.bat`` shim (Node 24's CVE-2024-27980 mitigation blocks direct ``.cmd``
    spawns). ``uvx.exe`` / ``python.exe`` are real executables that spawn directly
    and preserve support for paths with spaces, so no shell is needed for them.
    """
    cmd = [npx, "-y", "mcp-proxy", "--port", str(port),
           "--server", "stream", "--stateless"]
    if sys.platform == "win32" and inner_cmd and inner_cmd[0].lower().endswith((".cmd", ".bat")):
        cmd.append("--shell")
    cmd += ["--", *inner_cmd]
    return cmd


def _managed_bin_dir() -> "Path":
    """~/.meridian/bin — where auto-downloaded binaries are installed."""
    return Path.home() / ".meridian" / "bin"


def _win_shell_safe_path(path: str) -> str:
    """Make *path* safe to pass as a single token through mcp-proxy ``--shell``
    on Windows (89bc72c4).

    mcp-proxy's ``--shell`` hands the inner command to ``cmd.exe``, which splits
    unquoted arguments on spaces. A binary path with a space — e.g. the
    auto-downloaded ``C:\\Users\\John Smith\\.meridian\\bin\\codebase-memory-mcp.exe``
    — is therefore truncated at the first space, so ``cmd.exe`` tries to run
    ``C:\\Users\\John`` and fails with WinError 3, "The system cannot find the
    path specified". This is why the code-intel slot failed to launch while the
    filesystem / Office / extractor slots (whose inner token is a bare npm
    package name or a space-free path) came up clean.

    Fix: when the path contains a space, resolve it to its 8.3 short-name form
    (``GetShortPathNameW``), which is space-free and understood by ``cmd.exe``.
    This preserves the required ``--shell`` spawn for native ``.exe`` binaries
    (mcp-proxy/Node can't always spawn them directly) *and* for ``.cmd`` shims.

    Fail-soft: on any non-Windows platform, a space-free path, a nonexistent
    path, or a volume with 8.3 name generation disabled (short-name lookup
    returns empty / raises), the original *path* is returned unchanged — the
    caller is no worse off than before this guard existed.
    """
    if sys.platform != "win32" or not path or " " not in path:
        return path
    try:
        import ctypes  # Windows-only stdlib; imported lazily so non-win32 is untouched.

        _GetShortPathNameW = ctypes.windll.kernel32.GetShortPathNameW  # type: ignore[attr-defined]
        buf = ctypes.create_unicode_buffer(512)
        n = _GetShortPathNameW(path, buf, len(buf))
        # n == 0 → failure (path missing / 8.3 disabled); n > len → buffer too small.
        if 0 < n < len(buf):
            short = buf.value
            if short and " " not in short:
                return short
    except Exception:  # noqa: BLE001 — any failure: fall back to the original path.
        pass
    return path


def _find_codebase_memory_mcp() -> str | None:
    """Return path to codebase-memory-mcp, checking PATH, npm global, then managed dir.

    ``shutil.which`` honours PATHEXT, so on Windows it already resolves the npm
    shim ``codebase-memory-mcp.cmd`` when ``%APPDATA%\\npm`` is on PATH. The
    explicit npm-global probe is a fallback for when it is not.
    """
    found = shutil.which("codebase-memory-mcp")
    if found:
        return found
    # Windows npm global install: %APPDATA%\npm\codebase-memory-mcp.cmd
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            shim = Path(appdata) / "npm" / "codebase-memory-mcp.cmd"
            if shim.exists():
                return str(shim)
    name = "codebase-memory-mcp.exe" if sys.platform == "win32" else "codebase-memory-mcp"
    managed = _managed_bin_dir() / name
    if managed.exists():
        return str(managed)
    return None


def _pick_release_asset(assets: list[dict]) -> "dict | None":
    """Pick the best GitHub release asset for the current platform and arch.

    Hard-excludes assets for other platforms before scoring so an arch-only
    match can never cause a cross-platform download (e.g. darwin-amd64 on
    Windows when no windows asset is present).
    """
    import platform as _platform

    machine = _platform.machine().lower()
    is_arm = machine in ("arm64", "aarch64")

    if sys.platform == "win32":
        os_kws = ["win", "windows"]
        os_exclude = ["linux", "darwin", "macos", "mac", "apple"]
    elif sys.platform == "darwin":
        os_kws = ["darwin", "macos", "mac", "apple"]
        # "win" is a substring of "darwin" — never use it as an exclusion keyword here
        os_exclude = ["linux", "windows", "msvc"]
    else:
        os_kws = ["linux"]
        os_exclude = ["darwin", "macos", "mac", "apple", "windows", "win"]

    arch_kws = ["aarch64", "arm64"] if is_arm else ["x86_64", "amd64", "x64"]

    def _score(name: str) -> int:
        n = name.lower()
        # Hard-exclude wrong-platform assets — never download a binary that
        # won't run on this OS, even if the arch matches.
        if any(kw in n for kw in os_exclude):
            return -100
        s = 0
        for kw in os_kws:
            if kw in n:
                s += 10
                break
        for kw in arch_kws:
            if kw in n:
                s += 5
                break
        if sys.platform == "win32" and n.endswith(".exe"):
            s += 3
        elif sys.platform != "win32" and not any(n.endswith(e) for e in (".exe", ".zip", ".tar.gz", ".tgz")):
            s += 1
        if any(n.endswith(e) for e in (".tar.gz", ".tgz", ".zip")):
            s -= 5
        return s

    candidates = [
        (a, _score(a["name"]))
        for a in assets
        if a.get("name") and a.get("browser_download_url")
    ]
    candidates.sort(key=lambda x: x[1], reverse=True)
    if not candidates or candidates[0][1] <= 0:
        return None
    return candidates[0][0]


async def _download_codebase_memory_mcp() -> "str | None":
    """Download the latest codebase-memory-mcp release for this platform.

    Saves to ~/.meridian/bin/ and makes the file executable. Returns the path
    on success, None on failure (error printed to stderr).
    """
    import httpx

    bin_dir = _managed_bin_dir()
    bin_dir.mkdir(parents=True, exist_ok=True)
    bin_name = "codebase-memory-mcp.exe" if sys.platform == "win32" else "codebase-memory-mcp"
    dest = bin_dir / bin_name

    print("  code-intel: codebase-memory-mcp not found — downloading from GitHub...", flush=True)

    api_url = "https://api.github.com/repos/DeusData/codebase-memory-mcp/releases/latest"
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            r = await client.get(api_url, headers={"Accept": "application/vnd.github+json"})
            r.raise_for_status()
            release = r.json()

        assets = release.get("assets", [])
        asset = _pick_release_asset(assets)
        if asset is None:
            print(
                "  code-intel: no suitable binary found in the GitHub release — "
                "install codebase-memory-mcp manually and re-run `meridian --tunnel`.",
                file=sys.stderr, flush=True,
            )
            return None

        version = release.get("tag_name", "unknown")
        print(f"  code-intel: downloading {asset['name']} ({version})...", flush=True)

        async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
            r = await client.get(asset["browser_download_url"])
            r.raise_for_status()
            dest.write_bytes(r.content)

        # Sanity-check: a real binary should be well over 1 MB. A redirect page
        # or partial download will be tiny — reject it so we don't silently cache
        # a broken file that produces "path not found" on every tunnel start.
        if dest.stat().st_size < 1_000_000:
            dest.unlink(missing_ok=True)
            print(
                f"  code-intel: downloaded file is too small ({dest.stat().st_size if dest.exists() else 0} bytes) "
                "— likely a corrupt download. Try: npm install -g codebase-memory-mcp",
                file=sys.stderr, flush=True,
            )
            return None

        if sys.platform != "win32":
            dest.chmod(dest.stat().st_mode | 0o111)  # make executable

        print(f"  code-intel: installed to {dest}", flush=True)
        return str(dest)
    except Exception as exc:
        print(f"  code-intel: download failed ({exc})", file=sys.stderr, flush=True)
        return None


async def _ensure_codebase_memory_mcp() -> "str | None":
    """Return path to codebase-memory-mcp, auto-downloading if not already installed."""
    found = _find_codebase_memory_mcp()
    if found:
        return found
    return await _download_codebase_memory_mcp()


def _build_code_proxy_command(
    npx: str, binary: str, port: int = DEFAULT_CODE_PROXY_PORT
) -> list[str]:
    """Build the mcp-proxy command wrapping codebase-memory-mcp on the given port.

    ``binary`` may be a native executable (the auto-downloaded ``.exe`` in
    ``~/.meridian/bin``) or an npm shim (``codebase-memory-mcp.cmd`` from a global
    npm install). On Windows a ``.cmd``/``.bat`` shim must be spawned through a
    shell (``--shell``) — mcp-proxy's direct spawn hits EINVAL under Node 24's
    CVE-2024-27980 mitigation. A real ``.exe`` spawns directly (and preserves
    support for paths with spaces), so ``--shell`` is added only for shims.

    89bc72c4 — under ``--shell`` cmd.exe splits args on spaces, so a *binary*
    path containing a space (e.g. a user whose home is ``C:\\Users\\John Smith``)
    was truncated and spawned as ``C:\\Users\\John`` → WinError 3, "The system
    cannot find the path specified". That was the isolated cause of the code-intel
    slot failing to launch while the space-free / bare-package slots came up clean.
    We now resolve the binary to its 8.3 short-name form when it contains a space
    (:func:`_win_shell_safe_path`), which is space-safe for cmd.exe.
    """
    cmd = [npx, "-y", "mcp-proxy", "--port", str(port),
           "--server", "stream", "--stateless"]
    if sys.platform == "win32":
        # Always add --shell on Windows: mcp-proxy (Node.js) cannot spawn .exe
        # binaries directly in some environments (missing DLL PATH, Node spawn
        # restrictions). cmd.exe handles resolution reliably. Mirrors the FS slot's
        # unconditional --shell. A space in the binary path would be split by
        # cmd.exe, so make it shell-safe (8.3 short path); .cmd shims need shell
        # anyway per Node 24 CVE-2024-27980. (89bc72c4)
        cmd.append("--shell")
        binary = _win_shell_safe_path(binary)
    cmd += ["--", binary]
    return cmd


def _build_proxy_for_inner(
    npx: str, inner_cmd: list[str], port: int, *, stateless: bool = True
) -> list[str]:
    """Wrap an arbitrary *inner_cmd* in mcp-proxy on *port*.

    Used for tenant plugin **command overrides** (e.g. swapping code-intel from
    codebase-memory-mcp to ``codegraph``). The default built-in slots keep their
    dedicated builders (:func:`_build_proxy_command`, :func:`_build_code_proxy_command`,
    :func:`_build_extractor_proxy_command`) so their exact, well-tested behaviour
    is preserved; this generic wrapper only applies when a slot's command is
    overridden via the plugin registry.

    On Windows ``--shell`` is added only when the inner launcher is a ``.cmd``/
    ``.bat`` shim (Node 24 CVE-2024-27980 mitigation), matching the other builders.

    *stateless* (4ea1b9d5) — when False, ``--stateless`` is omitted so the inner
    server keeps state across requests. Used for ``session_mode: "persistent"``
    slots like Desktop Commander, whose terminal sessions must survive between
    calls; ``--stateless`` would reset them on every POST.
    """
    cmd = [npx, "-y", "mcp-proxy", "--port", str(port), "--server", "stream"]
    if stateless:
        cmd.append("--stateless")
    if sys.platform == "win32" and inner_cmd and inner_cmd[0].lower().endswith((".cmd", ".bat")):
        cmd.append("--shell")
    cmd += ["--", *inner_cmd]
    return cmd


def _check_node() -> bool:
    """True when a usable Node.js (``node`` + ``npx``) is on PATH.

    Every tunnel slot runs its inner server through ``npx mcp-proxy``, so Node is
    a hard prerequisite — without it mcp-proxy can't start and each slot would
    silently 503. (d1c528f5)
    """
    return bool(shutil.which("node")) and bool(
        shutil.which("npx") or shutil.which("npx.cmd")
    )


def _install_node_via_fnm() -> bool:
    """Best-effort install of Node LTS via fnm (Fast Node Manager).

    fnm needs no admin rights and is cross-platform, so it's the least-friction
    way to provision Node for a user who only has Python/pixi. fnm itself is
    installed first when missing (winget/scoop on Windows, the official
    ``curl | bash`` script on POSIX). Every step is best-effort: all failures are
    swallowed and the function returns whether ``_check_node`` passes afterward,
    so the caller can fall back to a clear error. (d1c528f5)
    """
    fnm = shutil.which("fnm")
    try:
        if fnm is None:
            if sys.platform == "win32":
                if shutil.which("winget"):
                    installer = ["winget", "install", "-e", "--id", "Schniz.fnm",
                                 "--accept-source-agreements", "--accept-package-agreements"]
                elif shutil.which("scoop"):
                    installer = ["scoop", "install", "fnm"]
                else:
                    return False
                subprocess.run(installer, check=False, timeout=600)
            else:
                if not shutil.which("bash") or not shutil.which("curl"):
                    return False
                subprocess.run(
                    "curl -fsSL https://fnm.vercel.app/install | bash",
                    shell=True, check=False, timeout=600,
                )
            fnm = shutil.which("fnm")
            if fnm is None:
                return False
        # Install + select Node LTS. fnm installs into its own dir; reflect the
        # resulting node path on this process's PATH so the spawned proxies see it.
        subprocess.run([fnm, "install", "--lts"], check=False, timeout=600)
        subprocess.run([fnm, "use", "lts-latest"], check=False, timeout=120)
        try:
            out = subprocess.run(
                [fnm, "exec", "--using=lts-latest", "node", "-e",
                 "process.stdout.write(process.execPath)"],
                check=False, capture_output=True, text=True, timeout=120,
            )
            node_path = (out.stdout or "").strip()
            if node_path:
                bindir = str(Path(node_path).parent)
                if bindir and bindir not in os.environ.get("PATH", ""):
                    os.environ["PATH"] = bindir + os.pathsep + os.environ.get("PATH", "")
        except Exception:  # noqa: BLE001 — PATH reflect is best-effort
            pass
        return _check_node()
    except Exception:  # noqa: BLE001 — install is best-effort; caller degrades
        return False


def _ensure_node(auto_install: bool) -> bool:
    """Ensure Node.js is available for the npx-based proxies.

    Returns True when Node is usable. When it's missing: auto-install via fnm if
    *auto_install* is set, otherwise print a clear, actionable error and return
    False so the caller disables the npm-dependent slots instead of handing
    mcp-proxy a broken npx path. (d1c528f5)
    """
    if _check_node():
        return True
    if auto_install:
        print(
            "meridian tunnel: Node.js not found — installing Node LTS via fnm "
            "(no admin required)…",
            flush=True,
        )
        if _install_node_via_fnm():
            print("meridian tunnel: Node.js installed and on PATH.", flush=True)
            return True
        print(
            "meridian tunnel: automatic Node.js install failed.",
            file=sys.stderr, flush=True,
        )
    print(
        "meridian tunnel: Node.js (node + npx) is required to run the tunnel "
        "proxies but was not found on PATH.\n"
        "  Fix: install Node LTS (https://nodejs.org) or fnm "
        "(https://github.com/Schniz/fnm), then restart the tunnel.\n"
        "  Or enable Settings → Tunnel → \"Auto-install Node\" (or set "
        "MERIDIAN_AUTO_INSTALL_NODE=1) to let Meridian install it for you.",
        file=sys.stderr, flush=True,
    )
    return False


def _find_npx() -> str:
    """Locate the npx launcher.

    On Windows a bare ``npx`` resolves to the extension-less shell shim, which
    fails when spawned without a shell (``[WinError 193]``). We need the full
    path to ``npx.cmd``. Fall back to the standard npm global location.
    """
    if sys.platform == "win32":
        found = shutil.which("npx.cmd") or shutil.which("npx")
        if found:
            return found
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            candidate = Path(appdata) / "npm" / "npx.cmd"
            if candidate.exists():
                return str(candidate)
        return "npx.cmd"
    return shutil.which("npx") or "npx"


def _normalize_path_arg(path: str) -> str:
    """Strip surrounding whitespace and matched surrounding quotes from a user-
    supplied path (e.g. a pasted '"C:\\Users\\me\\My Docs"'). Repeated/mixed
    wrapping is unwound; interior characters (incl. spaces) are untouched.
    Idempotent — a clean path is returned unchanged.

    path-quote-strip: only MATCHED surrounding single/double quotes are removed
    — never a lone quote, never an interior character."""
    s = (path or "").strip()
    while len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        s = s[1:-1].strip()
    return s


def _unservable_roots(roots: "list[str] | None") -> "list[tuple[str, str]]":
    """59c0e609 — configured filesystem_roots the connector will SILENTLY not
    serve, each with a reason, so the caller can WARN instead of the user seeing
    an unexplained "2 of 3 dirs" after a tunnel restart.

    The Python pipeline passes every configured root through correctly (verified);
    the drop happens at the inner ``@modelcontextprotocol/server-filesystem``
    process, which two ways silently omits a dir: (1) the directory does not exist
    at spawn time (e.g. a renamed folder, or a OneDrive/cloud dir that is
    dehydrated/offline), and (2) on Windows, mcp-proxy runs with ``--shell`` and
    concatenates args unescaped, so a path containing a SPACE is mis-parsed (see
    :func:`_build_proxy_command`). Pure/deterministic for testing — it only reads
    the filesystem via ``os.path.isdir``; the caller does the printing."""
    out: list[tuple[str, str]] = []
    for r in roots or []:
        r = _normalize_path_arg(r)
        if not r:
            continue
        if not os.path.isdir(r):
            out.append((r, "does not exist / not a directory at tunnel start"))
        elif sys.platform == "win32" and " " in r:
            out.append((r, "contains a space — not served on Windows (mcp-proxy --shell limitation)"))
    return out


def _build_proxy_command(
    npx: str, repo_path: str, port: int = DEFAULT_PROXY_PORT,
    roots: "list[str] | None" = None,
) -> list[str]:
    """Build the ``mcp-proxy`` command that wraps the filesystem MCP server.

    Roughly::

        npx -y mcp-proxy [--shell] --port <port> -- \
            npx -y @modelcontextprotocol/server-filesystem <dir1> [<dir2> ...]

    The directories served are *roots* when provided (the tenant's configured
    ``executor_config.filesystem_roots`` — supports multi-root setups like
    ``C:\\Users\\me\\Documents`` AND ``D:\\Projects``), otherwise the single
    *repo_path* (the home-directory default — unchanged behaviour).

    The OUTER ``npx`` is the resolved launcher (full ``npx.cmd`` path on Windows,
    so Python's ``subprocess`` can start it). The INNER command is bare ``npx``,
    resolved by mcp-proxy.

    On Windows we pass ``--shell`` so mcp-proxy spawns the inner ``npx`` through
    cmd.exe. Without it, mcp-proxy's direct spawn fails two ways on modern Node:
    bare ``npx`` → ENOENT, and a full ``npx.cmd`` path → EINVAL (Node 24's
    CVE-2024-27980 mitigation refuses to spawn ``.cmd``/``.bat`` without a shell).

    Note: with ``--shell`` mcp-proxy concatenates args unescaped, so a
    directory containing spaces is not yet supported on Windows.
    """
    dirs = [_normalize_path_arg(r) for r in (roots or []) if _normalize_path_arg(r)] or [repo_path]
    # --server stream: serve only Streamable HTTP (/mcp), no SSE.
    # --stateless: each POST is handled independently — required for the
    #   tunnel relay's one-shot request/response model (no persistent SSE pipe).
    cmd = [npx, "-y", "mcp-proxy", "--port", str(port),
           "--server", "stream", "--stateless"]
    if sys.platform == "win32":
        cmd.append("--shell")
    cmd += ["--", "npx", "-y", "@modelcontextprotocol/server-filesystem", *dirs]
    return cmd


def _is_subpath(child: "Path", parent: "Path") -> bool:
    """Return True if *child* is under *parent* (both resolved)."""
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _parse_test_counts(text: str) -> "tuple[int | None, int | None]":
    """0e973e52 — parse passed/failed counts from common test runner output.

    Recognises pytest-style summary lines ("5 passed", "2 failed") and
    pixi/cargo/go patterns. Returns (passed, failed) — both None when the text
    carries no recognisable summary. Never raises.
    """
    import re  # noqa: PLC0415
    if not text:
        return None, None
    try:
        # pytest: "5 passed, 2 failed in 3.14s" or "3 passed" or "0 failed"
        passed: "int | None" = None
        failed: "int | None" = None
        pm = re.search(r"(\d+)\s+passed", text)
        if pm:
            passed = int(pm.group(1))
        fm = re.search(r"(\d+)\s+failed", text)
        if fm:
            failed = int(fm.group(1))
        return passed, failed
    except Exception:  # noqa: BLE001
        return None, None


async def _handle_run_cmd(cmd: "str | list", cwd: "str | None") -> dict:
    """0e973e52 — spawn *cmd* as a local process and return structured results.

    Accepts a shell command string (run via the platform shell) or a list of
    tokens (run directly). Captures stdout+stderr with a per-call hard cap of
    16 KiB each (tail-trimmed so the most informative end is preserved). Limits
    the output so a verbose test suite never floods the tunnel with megabytes.

    Returns a dict with keys: exit_code (int), passed (int|None),
    failed (int|None), stdout_tail (str), stderr_tail (str), status (str).
    """
    import asyncio as _asyncio  # noqa: PLC0415
    import shlex as _shlex  # noqa: PLC0415

    _TAIL_BYTES = 16_384

    if not cmd:
        return {
            "status": "error",
            "message": "cmd is empty",
            "exit_code": None,
            "passed": None,
            "failed": None,
            "stdout_tail": "",
            "stderr_tail": "",
        }

    try:
        if isinstance(cmd, list):
            proc = await _asyncio.create_subprocess_exec(
                *cmd,
                stdout=_asyncio.subprocess.PIPE,
                stderr=_asyncio.subprocess.PIPE,
                cwd=cwd or None,
            )
        else:
            proc = await _asyncio.create_subprocess_shell(
                str(cmd),
                stdout=_asyncio.subprocess.PIPE,
                stderr=_asyncio.subprocess.PIPE,
                cwd=cwd or None,
            )
        stdout_b, stderr_b = await proc.communicate()
        exit_code: int = proc.returncode if proc.returncode is not None else -1

        stdout_str = stdout_b[-_TAIL_BYTES:].decode("utf-8", errors="replace") if stdout_b else ""
        stderr_str = stderr_b[-_TAIL_BYTES:].decode("utf-8", errors="replace") if stderr_b else ""

        combined = stdout_str + "\n" + stderr_str
        passed, failed = _parse_test_counts(combined)

        return {
            "status": "ok",
            "exit_code": exit_code,
            "passed": passed,
            "failed": failed,
            "stdout_tail": stdout_str,
            "stderr_tail": stderr_str,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "error",
            "message": str(exc),
            "exit_code": None,
            "passed": None,
            "failed": None,
            "stdout_tail": "",
            "stderr_tail": "",
        }


def _add_fs_roots_to_cmd(cmd: "list[str]", new_roots: "list[str]") -> "tuple[list[str], bool]":
    """Append *new_roots* to an existing ``_build_proxy_command`` result.

    Finds the ``@modelcontextprotocol/server-filesystem`` token in *cmd* and
    appends any roots that are not already present.  Returns ``(updated_cmd,
    changed)`` — if nothing was added *changed* is ``False`` and *cmd* is
    returned unchanged.
    """
    try:
        idx = cmd.index("@modelcontextprotocol/server-filesystem")
    except ValueError:
        return cmd, False
    existing: set[str] = set(cmd[idx + 1:])
    to_add = [
        nr for nr in (_normalize_path_arg(r) for r in new_roots)
        if nr and nr not in existing
    ]
    if not to_add:
        return cmd, False
    return cmd[:idx + 1] + list(cmd[idx + 1:]) + to_add, True


def _set_fs_roots_on_cmd(cmd: "list[str]", roots: "list[str]") -> "tuple[list[str], bool]":
    """live-fs-roots — REPLACE the served dirs in a ``_build_proxy_command`` result
    with EXACTLY *roots* (normalized, deduped, order preserved).

    Unlike :func:`_add_fs_roots_to_cmd` (which only appends), this rebuilds the
    trailing dir list after the ``@modelcontextprotocol/server-filesystem`` token
    so a removal shrinks the served set. Returns ``(updated_cmd, changed)`` —
    *changed* is ``False`` (and *cmd* returned unchanged) when the token is absent
    or the requested dirs already match the current ones. An empty/all-blank
    *roots* list is a no-op (``changed=False``): the inner filesystem server needs
    at least one dir, so we never strip it down to zero.
    """
    try:
        idx = cmd.index("@modelcontextprotocol/server-filesystem")
    except ValueError:
        return cmd, False
    new_dirs: list[str] = []
    for r in roots:
        nr = _normalize_path_arg(r)
        if nr and nr not in new_dirs:
            new_dirs.append(nr)
    if not new_dirs:
        # Refuse to leave the filesystem server with zero dirs.
        return cmd, False
    if list(cmd[idx + 1:]) == new_dirs:
        return cmd, False
    return cmd[:idx + 1] + new_dirs, True


def _extract_denied_path(resp: dict) -> "str | None":
    """Parse the denied path from a filesystem MCP 'access denied' response.

    Looks for ``"Access denied - path outside allowed directories: <path>"``
    inside the base64-encoded response body.  Returns the path string if found,
    ``None`` otherwise.
    """
    import re
    body_b64 = resp.get("body") or ""
    try:
        body = base64.b64decode(body_b64).decode("utf-8", errors="replace")
    except Exception:
        return None
    m = re.search(r"Access denied - path outside allowed directories:\s*(.+?)(?:\"|}|$)", body)
    if m:
        return m.group(1).strip().rstrip('"').rstrip("'").rstrip("\\")
    return None


async def _fetch_filesystem_roots(
    base_url: str, token: str
) -> "tuple[list[str], list[str], str, list[str]]":
    """GET /tunnel/filesystem-roots — the dirs the fs connector may serve.

    Returns ``(filesystem_roots, known_repo_paths, serena_repo_path,
    codebase_code_dirs)``.  ``filesystem_roots`` are the explicit allowed dirs
    (unioned ``executor_config.filesystem_roots`` across projects);
    ``known_repo_paths`` are the implicit trust anchors
    (``executor_config.repo_path`` per project) used for silent auto-add.

    b970fe07 — ``serena_repo_path`` is the first non-empty
    ``executor_config.serena_repo_path`` (Serena's default ``--project``) and
    ``codebase_code_dirs`` the deduped union of ``executor_config.codebase_code_dirs``
    (the code-intel slot's auto-index dirs). Fall-back-safe: the caller applies
    each only when the matching CLI flag is absent.

    All fields degrade to empty (``[]`` / ``""``) on any error so the caller
    falls back to today's defaults (home dir / cwd / no auto-index).
    """
    import httpx

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(
                f"{base_url}/tunnel/filesystem-roots",
                headers={"Authorization": f"Bearer {token}"},
            )
            if r.status_code == 200:
                data = r.json() or {}
                roots = data.get("filesystem_roots") or []
                known = data.get("known_repo_paths") or []
                serena_repo = data.get("serena_repo_path") or ""
                code_dirs = data.get("codebase_code_dirs") or []
                return (
                    [_normalize_path_arg(x) for x in roots if isinstance(x, str) and _normalize_path_arg(x)],
                    [_normalize_path_arg(x) for x in known if isinstance(x, str) and _normalize_path_arg(x)],
                    _normalize_path_arg(serena_repo) if isinstance(serena_repo, str) else "",
                    [_normalize_path_arg(x) for x in code_dirs if isinstance(x, str) and _normalize_path_arg(x)],
                )
    except Exception:  # noqa: BLE001 — network/parse error → defaults
        pass
    return [], [], "", []


# ---------------------------------------------------------------------------
# Auto-index helper (calls index_repository on codebase-memory-mcp proxy)
# ---------------------------------------------------------------------------

async def _index_code_dir(port: int, code_dir: str) -> None:
    """Wait for the code-intel proxy to start, then call index_repository on code_dir.

    Uses Streamable HTTP (MCP 2025-03-26): mcp-proxy handles the stdio lifecycle
    per POST, so a direct tools/call is sufficient — no client-side initialize needed.
    Failures are non-fatal (logged to stderr, tunnel continues).
    """
    import httpx

    local = f"http://127.0.0.1:{port}/mcp"
    probe = {"jsonrpc": "2.0", "id": "probe", "method": "tools/list", "params": {}}

    # Poll until the proxy is accepting connections (up to 60s).
    for _ in range(60):
        try:
            async with httpx.AsyncClient(timeout=2.0) as c:
                r = await c.post(local, json=probe,
                                 headers={"Content-Type": "application/json"})
                if r.status_code < 500:
                    break
        except Exception:
            pass
        await asyncio.sleep(1.0)
    else:
        print(
            f"  code-intel: proxy not ready after 60s — skipping auto-index of {code_dir}",
            file=sys.stderr, flush=True,
        )
        return

    payload = {
        "jsonrpc": "2.0",
        "id": "idx",
        "method": "tools/call",
        "params": {"name": "index_repository", "arguments": {"path": code_dir}},
    }
    try:
        async with httpx.AsyncClient(timeout=300.0) as c:
            r = await c.post(local, json=payload,
                             headers={"Content-Type": "application/json"})
        if r.status_code < 400:
            print(f"  code-intel: indexed {code_dir}", flush=True)
        else:
            print(
                f"  code-intel: index returned HTTP {r.status_code} for {code_dir}",
                file=sys.stderr, flush=True,
            )
    except Exception as exc:
        print(f"  code-intel: index failed for {code_dir}: {exc}",
              file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Tool-name prefixing (b4455202) — pure, unit-tested
# ---------------------------------------------------------------------------

def _prefix_tool_name(name: Any, prefix: str) -> Any:
    """Prepend ``"<prefix>__"`` to a tool name, never double-prefixing.

    Returns *name* unchanged if it is not a string or already carries the prefix.
    """
    if not isinstance(name, str):
        return name
    marker = f"{prefix}__"
    if name.startswith(marker):
        return name
    return marker + name


def _prefix_tools_in_jsonrpc(obj: Any, prefix: str) -> bool:
    """In-place: prefix every ``result.tools[*].name`` in a JSON-RPC object.

    Returns True if *obj* was a tools/list-shaped response (``result.tools`` is a
    list) and at least one entry was inspected, else False (so callers can skip
    re-serialising untouched bodies). Tolerant of malformed shapes.
    """
    if not isinstance(obj, dict):
        return False
    result = obj.get("result")
    if not isinstance(result, dict):
        return False
    tools = result.get("tools")
    if not isinstance(tools, list):
        return False
    for tool in tools:
        if isinstance(tool, dict) and "name" in tool:
            tool["name"] = _prefix_tool_name(tool["name"], prefix)
    return True


def _apply_tool_prefix(body: bytes, prefix: str | None) -> bytes:
    """Rewrite tool names in a relayed ``tools/list`` response body.

    *body* is the raw HTTP response content from the local mcp-proxy. mcp-proxy's
    Streamable HTTP transport returns either a bare JSON-RPC object or an SSE
    stream (``event: message\\n data: {json}\\n\\n``); both are handled. Only
    ``tools/list``-shaped JSON-RPC payloads (``result.tools``) are touched —
    every other body (tools/call results, initialize, errors, non-JSON) is
    returned byte-for-byte unchanged. Never raises: any parse failure degrades to
    the original body. A falsy/empty *prefix* is a no-op.
    """
    if not prefix or not body:
        return body
    # Cheap guard: only bodies that could carry a tools list are worth parsing.
    if b'"tools"' not in body:
        return body
    try:
        text = body.decode("utf-8")
    except (UnicodeDecodeError, AttributeError):
        return body

    # Plain JSON-RPC object (no SSE framing).
    stripped = text.lstrip()
    if stripped.startswith("{"):
        try:
            obj = json.loads(text)
        except ValueError:
            return body
        if _prefix_tools_in_jsonrpc(obj, prefix):
            return json.dumps(obj).encode("utf-8")
        return body

    # SSE framing: rewrite each ``data:`` line's JSON payload, preserve the rest
    # (event:/id:/comment lines, blank separators, CRLF or LF) verbatim.
    changed = False
    out_lines: list[str] = []
    for line in text.splitlines(keepends=True):
        bare = line.rstrip("\r\n")
        if bare.startswith("data:"):
            payload = bare[5:]
            lead = payload[: len(payload) - len(payload.lstrip())]
            data = payload.strip()
            if data:
                try:
                    obj = json.loads(data)
                except ValueError:
                    out_lines.append(line)
                    continue
                if _prefix_tools_in_jsonrpc(obj, prefix):
                    newline = line[len(bare):]  # original "\n"/"\r\n"/"" suffix
                    out_lines.append(f"data:{lead}{json.dumps(obj)}{newline}")
                    changed = True
                    continue
        out_lines.append(line)
    if not changed:
        return body
    return "".join(out_lines).encode("utf-8")


# ---------------------------------------------------------------------------
# Request relay (mostly pure — unit tested with httpx MockTransport)
# ---------------------------------------------------------------------------

async def _relay_request(
    http_client, local_base: str, msg: dict, *, tool_prefix: str | None = None
) -> dict:
    """Proxy one server ``request`` message to the local mcp-proxy.

    Returns a ``response`` message (same correlation id) with a base64 body,
    matching the protocol in ``routes/tunnel.py``. Local failures come back as
    a 502 so the server resolves its pending future instead of timing out.

    ``tool_prefix`` (b4455202) — when set, ``tools/list`` response bodies get
    each tool name prefixed (e.g. ``read_file`` → ``Filesystem: read_file``).
    Non-tools/list responses pass through untouched.
    """
    req_id = msg.get("id")
    method = (msg.get("method") or "GET").upper()
    path = msg.get("path") or "/"
    query = msg.get("query") or ""
    headers = dict(msg.get("headers") or {})
    # Drop any stale Host — httpx sets it from the local target.
    headers = {k: v for k, v in headers.items() if k.lower() != "host"}
    body_b64 = msg.get("body")
    body = base64.b64decode(body_b64) if body_b64 else None

    url = local_base.rstrip("/") + path
    if query:
        url += ("&" if "?" in url else "?") + query.lstrip("?")

    try:
        resp = await http_client.request(
            method, url, headers=headers, content=body,
            timeout=_LOCAL_REQUEST_TIMEOUT,
        )
        resp_body = resp.content or b""
        resp_headers = dict(resp.headers)
        if tool_prefix and resp_body:
            new_body = _apply_tool_prefix(resp_body, tool_prefix)
            if new_body != resp_body:
                resp_body = new_body
                # The rewrite changes the byte length — drop the now-stale
                # Content-Length so the server frames the new body correctly.
                resp_headers = {
                    k: v for k, v in resp_headers.items()
                    if k.lower() != "content-length"
                }
        return {
            "type": "response",
            "id": req_id,
            "status": resp.status_code,
            "headers": resp_headers,
            "body": base64.b64encode(resp_body).decode() if resp_body else "",
        }
    except Exception as exc:  # local proxy down / timeout / bad response
        err = json.dumps({"error": f"local proxy error: {exc}"}).encode()
        out = {
            "type": "response",
            "id": req_id,
            "status": 502,
            "headers": {"content-type": "application/json"},
            "body": base64.b64encode(err).decode(),
        }
        # 3bde892a — tag a *timeout* distinctly from any other local failure so
        # the lazy-spawn caller can recover the slot (kill+respawn) rather than
        # just report a 502 and leave a possibly-zombie mcp-proxy holding the
        # port. This ``_timed_out`` key is a private, in-process signal only:
        # callers MUST pop it before sending ``out`` on the wire (it is not part
        # of the tunnel response protocol). A connection-refused / bad-response
        # error is NOT a timeout and does not set it.
        try:
            import httpx  # noqa: PLC0415 — already imported by callers; local keeps module light
            if isinstance(exc, httpx.TimeoutException):
                out["_timed_out"] = True
        except Exception:  # noqa: BLE001 — never let the tag break the relay
            pass
        return out


# ---------------------------------------------------------------------------
# Networking
# ---------------------------------------------------------------------------

async def _fetch_me(base_url: str, token: str) -> dict:
    """GET /me and return the JSON body (raises on transport/HTTP error).

    8660d701 — sends this machine's hostname (X-Meridian-Hostname) so the server
    resolves THIS machine's tunnel plugin config (per-machine, since different
    machines have different software installed). Best-effort: a missing hostname
    just falls back to the per-tenant default config server-side.
    """
    import httpx
    import socket

    try:
        _hostname = socket.gethostname() or ""
    except Exception:  # noqa: BLE001
        _hostname = ""
    headers = {"Authorization": f"Bearer {token}"}
    if _hostname:
        headers["X-Meridian-Hostname"] = _hostname
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(f"{base_url}/me", headers=headers)
        r.raise_for_status()
        return r.json()


async def _run_connection(
    ws_url: str, port: int, label: str = "fs", tool_prefix: str | None = None
) -> None:
    """Hold one WebSocket session open, relaying requests until it drops."""
    import httpx
    import websockets

    local_base = f"http://127.0.0.1:{port}"
    async with websockets.connect(ws_url, max_size=None, ping_interval=20) as ws:
        print(f"tunnel:{label}: connected", flush=True)
        async with httpx.AsyncClient() as http_client:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                if not isinstance(msg, dict):
                    continue
                if msg.get("type") == "ping":
                    continue
                if msg.get("type") == "request":
                    resp = await _relay_request(
                        http_client, local_base, msg, tool_prefix=tool_prefix
                    )
                    await ws.send(json.dumps(resp))


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

async def _reconnect_loop(
    ws_url: str, port: int, label: str, tool_prefix: str | None = None
) -> None:
    """Keep one tunnel alive, reconnecting with exponential backoff."""
    backoff = 1.0
    while True:
        try:
            await _run_connection(ws_url, port, label, tool_prefix=tool_prefix)
            backoff = 1.0
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(
                f"tunnel:{label}: disconnected ({exc}); reconnecting in {backoff:.0f}s",
                file=sys.stderr,
                flush=True,
            )
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, _MAX_BACKOFF)


async def _proc_watchdog(
    holder: dict, poll_interval: float = 3.0, max_retries: int = _WATCHDOG_MAX_RETRIES
) -> None:
    """Relaunch a slot's local subprocess if it dies — with bounded retries.

    ``holder`` is a mutable dict ``{"proc", "cmd", "env", "label"}``. Each tick
    polls ``holder["proc"].poll()``; if the process has exited it is re-spawned
    from ``holder["cmd"]`` (with ``holder["env"]``) and stored back into the
    holder. Without this, a crashed local proxy leaves 127.0.0.1:880x dead — the
    WebSocket reconnects to the server but the tools silently vanish.

    Crash isolation: a proxy whose command is broken (e.g. ENOENT on a missing
    binary) used to be relaunched every tick forever, spamming the terminal. Now
    consecutive failures back off exponentially (capped at ``_MAX_BACKOFF``) and
    the watchdog gives up after ``max_retries`` straight failures, leaving that
    one slot down without taking the tunnel — or the terminal — down with it. A
    proxy that comes back healthy resets the counter, so an occasional crash
    still recovers. Runs until cancelled or it gives up on the slot.
    """
    label = holder.get("label", "?")
    failures = 0
    backoff = poll_interval
    while True:
        await asyncio.sleep(backoff)
        proc = holder.get("proc")
        if proc is None or proc.poll() is None:
            if proc is not None:  # a healthy tick clears the crash streak
                failures = 0
                backoff = poll_interval
            continue
        failures += 1
        if failures > max_retries:
            print(
                f"tunnel:{label}: local proxy keeps exiting (code {proc.returncode}); "
                f"giving up after {max_retries} retries — fix the command and restart "
                f"the tunnel.",
                file=sys.stderr, flush=True,
            )
            return
        print(
            f"tunnel:{label}: local proxy exited (code {proc.returncode}); relaunching "
            f"(attempt {failures}/{max_retries})",
            file=sys.stderr, flush=True,
        )
        try:
            holder["proc"] = subprocess.Popen(
                holder["cmd"], env=holder.get("env"), **_spawn_kwargs()
            )
        except Exception as exc:  # noqa: BLE001 — count it, back off, may still recover
            print(f"tunnel:{label}: relaunch failed: {exc}", file=sys.stderr, flush=True)
        backoff = min(max(backoff, poll_interval) * 2, _MAX_BACKOFF)


# ---------------------------------------------------------------------------
# Local MCP config auto-update (.mcp.json / .cursor/mcp.json) — pure, unit tested
# ---------------------------------------------------------------------------

# ef162c28 — Connector keys were historically named by transport SLOT
# (meridian-fs / meridian-code / meridian-extractor). They now derive from the
# PLUGIN behind each slot so the .mcp.json names read as what the connector
# actually is: filesystem / codebase-memory / serena. The static slot→name map
# below is the canonical source. We deliberately do NOT derive these from the
# resolved plugin's ``name`` field: the plugin names are "filesystem" /
# "code-intel" / "code-extractor", which would give the wrong labels for the
# code and extract slots (codebase-memory-mcp rides "code"; Serena rides
# "extract"). A hardcoded slot→name map is therefore the cleanest option and
# avoids threading the plugin registry through these pure helpers. Known
# limitation: a tenant who overrides the extract slot's command (e.g. back to
# mcp-server-code-extractor) still gets the connector name "serena" — the name
# tracks the slot's default implementation, not the live override.
_TUNNEL_MCP_SLOT_NAMES = {
    "fs": "filesystem",
    "code": "codebase-memory",
    "extract": "serena",
}

# The connector keys the CURRENT code injects (new, plugin-derived names).
TUNNEL_MCP_KEYS = tuple(_TUNNEL_MCP_SLOT_NAMES.values())

# ef162c28 — the legacy slot-named keys older tunnel builds wrote. Kept so the
# migration on inject (drop stale duplicates) and the restore/cleanup path
# (remove our entries on tunnel stop) both cover the old names as well as the
# new ones. Existing users must not accumulate both meridian-fs AND filesystem
# pointing at the same relay URL.
_LEGACY_TUNNEL_MCP_KEYS = ("meridian-fs", "meridian-code", "meridian-extractor")

# Keys that are unambiguously ours by NAME alone: the legacy slot names and the
# custom-plugin prefix. No user names a server ``meridian-fs`` /
# ``meridian-custom-*``, so seeing one is proof we wrote it.
#
# The NEW built-in names (``filesystem`` / ``codebase-memory`` / ``serena``) are
# deliberately NOT in this set: a user may legitimately run their own server
# called ``filesystem``. Such a key counts as ours ONLY when its URL also points
# at our relay (see :func:`_is_our_mcp_entry`); otherwise it's the user's and we
# suffix ours around it.
_OWN_TUNNEL_MCP_KEYS_BY_NAME = frozenset(_LEGACY_TUNNEL_MCP_KEYS)


def _is_our_mcp_entry(key: str, value: object, base_url: str, tenant_id: str) -> bool:
    """Whether an existing ``mcpServers`` entry was written by this tunnel.

    ef162c28 — ours if the key is a legacy slot name or a ``meridian-custom-*``
    key (unambiguous by name), OR the entry's ``url`` points at this tenant's
    relay/permanent tunnel URL (a connector a prior run wrote — even under a
    new-name key like ``filesystem`` or a collision-suffixed key). A key that is
    merely NAMED like one of our new built-ins but whose URL is NOT ours is the
    USER's own server and must never be clobbered.
    """
    if key in _OWN_TUNNEL_MCP_KEYS_BY_NAME or key.startswith("meridian-custom-"):
        return True
    url = value.get("url") if isinstance(value, dict) else None
    if not isinstance(url, str):
        return False
    ours = {
        _permanent_url(base_url, tenant_id),
        _permanent_code_url(base_url, tenant_id),
        _permanent_extract_url(base_url, tenant_id),
    }
    return url in ours


def _tunnel_mcp_entries(
    base_url: str, tenant_id: str, custom: "list[dict] | None" = None,
) -> dict[str, dict]:
    """The HTTP MCP connector entries pointing at this tenant's tunnel.

    ef162c28 — the three built-in slots are keyed by the plugin behind each slot
    (``filesystem`` / ``codebase-memory`` / ``serena``, see
    :data:`_TUNNEL_MCP_SLOT_NAMES`), pointing at the hosted relay URLs
    (claude.ai-reachable). *custom* is the list of running user-defined plugins
    (``{"name", "port"}``); each gets an entry keyed ``meridian-custom-<name>``
    pointing at its LOCAL mcp-proxy (``http://127.0.0.1:<port>/mcp``) — they are
    LOCAL-ONLY and have no server route, so a co-located client reaches them
    directly, not via the relay.

    Collision handling and legacy-key migration happen in
    :func:`_inject_mcp_entries`, which sees the existing config; this function is
    a pure name→url map and stays trivially unit-testable.
    """
    entries = {
        _TUNNEL_MCP_SLOT_NAMES["fs"]: {"type": "http", "url": _permanent_url(base_url, tenant_id)},
        _TUNNEL_MCP_SLOT_NAMES["code"]: {"type": "http", "url": _permanent_code_url(base_url, tenant_id)},
        _TUNNEL_MCP_SLOT_NAMES["extract"]: {"type": "http", "url": _permanent_extract_url(base_url, tenant_id)},
    }
    for cp in custom or []:
        name = cp.get("name")
        port = cp.get("port")
        if not name or not isinstance(port, int):
            continue
        entries[f"meridian-custom-{name}"] = {
            "type": "http", "url": f"http://127.0.0.1:{port}/mcp",
        }
    return entries


def _mcp_json_paths(cwd: "str | Path") -> list["Path"]:
    """MCP config files to update: `.mcp.json` always, `.cursor/mcp.json` if present.

    `.mcp.json` (Claude Code) is created if absent — that is the whole point of
    auto-update. `.cursor/mcp.json` is only touched when it already exists so we
    never create Cursor config for non-Cursor users.
    """
    cwd = Path(cwd)
    paths = [cwd / ".mcp.json"]
    cursor = cwd / ".cursor" / "mcp.json"
    if cursor.exists():
        paths.append(cursor)
    return paths


def _unique_mcp_key(desired: str, taken: "set[str]") -> str:
    """A connector key not already in *taken*, derived from *desired*.

    ef162c28 — on collision with a user's own server we append ``-meridian``,
    then ``-meridian-2``, ``-meridian-3`` … so we never clobber their entry.
    """
    if desired not in taken:
        return desired
    candidate = f"{desired}-meridian"
    n = 2
    while candidate in taken:
        candidate = f"{desired}-meridian-{n}"
        n += 1
    return candidate


def _inject_mcp_entries(
    text: "str | None",
    entries: dict[str, dict],
    base_url: str = "",
    tenant_id: str = "",
) -> str:
    """Merge *entries* under ``mcpServers`` in an existing `.mcp.json` body.

    *text* is the current file content (``None``/empty for a new file). Existing
    servers and other top-level keys are preserved. Returns the new file text.

    ef162c28 — three behaviours layered on the plain merge:

    * **Legacy migration:** any of our *legacy* slot-named keys
      (``meridian-fs`` / ``meridian-code`` / ``meridian-extractor``) already in
      the file are removed before we write the new plugin-named entries, so
      resuming users don't accumulate stale duplicates pointing at the same URL.
      Our *new* keys are simply overwritten with the current URL.
    * **Collision handling:** if the file has a key matching one of our incoming
      built-in names (e.g. the user runs their OWN ``filesystem`` server) that
      is NOT ours, we write under a suffixed key (``filesystem-meridian``, …)
      and leave the user's entry untouched.
    * Custom (``meridian-custom-*``) keys are always ours — overwritten in place.

    *base_url*/*tenant_id* enable ours-vs-theirs detection by URL (a connector a
    prior run wrote under a suffixed key). Both default to "" for callers that
    only merge already-unique entries (e.g. simple unit tests).
    """
    data = {}
    if text:
        try:
            data = json.loads(text)
        except Exception:  # noqa: BLE001 — malformed config: start clean rather than crash
            data = {}
    if not isinstance(data, dict):
        data = {}
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}

    # 1. Migration + de-dup: drop every entry that is OURS (legacy slot names,
    #    prior new-name writes, custom keys, or any URL pointing at our relay).
    #    We rebuild them fresh below, so this prevents stale duplicates and lets
    #    collision detection see only the USER's remaining servers.
    for key in list(servers):
        if _is_our_mcp_entry(key, servers.get(key), base_url, tenant_id):
            del servers[key]

    # 2. Merge our entries, suffixing on collision with a user-owned key.
    for desired, entry in entries.items():
        key = _unique_mcp_key(desired, set(servers))
        servers[key] = entry

    data["mcpServers"] = servers
    return json.dumps(data, indent=2) + "\n"


def _install_mcp_json(
    cwd: "str | Path", base_url: str, tenant_id: str,
    custom: "list[dict] | None" = None,
) -> list[tuple["Path", "str | None"]]:
    """Inject tunnel connector entries into local MCP config files.

    *custom* is the list of running user-defined plugins (``{"name", "port"}``);
    each gets a LOCAL ``http://127.0.0.1:<port>/mcp`` connector entry alongside the
    built-in relay entries (see :func:`_tunnel_mcp_entries`).

    Returns a list of ``(path, original_text_or_None)`` snapshots for restore.
    ``original_text_or_None`` is ``None`` when we created the file. Failures on
    any single file are reported and skipped — never fatal to the tunnel.
    """
    entries = _tunnel_mcp_entries(base_url, tenant_id, custom)
    snapshots: list[tuple[Path, str | None]] = []
    for path in _mcp_json_paths(cwd):
        existed = path.exists()
        original = path.read_text(encoding="utf-8") if existed else None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                _inject_mcp_entries(original, entries, base_url, tenant_id),
                encoding="utf-8",
            )
            snapshots.append((path, original))
        except Exception as exc:  # noqa: BLE001
            print(f"  warning: could not update {path}: {exc}", file=sys.stderr, flush=True)
    return snapshots


def _strip_our_mcp_entries(
    text: "str | None", base_url: str = "", tenant_id: str = "",
) -> "str | None":
    """Remove every connector entry that is OURS from a `.mcp.json` body.

    ef162c28 — used on restore to guarantee neither the LEGACY slot names
    (``meridian-fs`` / ``meridian-code`` / ``meridian-extractor``) nor the NEW
    plugin names (``filesystem`` / ``codebase-memory`` / ``serena``), nor any
    ``meridian-custom-*`` key, nor a suffixed key whose URL points at our relay,
    survives after the tunnel stops.

    Returns the re-serialized cleaned text WHEN something of ours was removed, or
    ``None`` when there was nothing of ours to strip (``None``/unparseable input,
    or a body that already contained none of our keys). ``None`` tells the caller
    to keep the original text byte-for-byte — so a user's file we never touched
    is restored exactly, not reformatted by ``json.dumps``.
    """
    if not text:
        return None
    try:
        data = json.loads(text)
    except Exception:  # noqa: BLE001 — malformed → keep original verbatim
        return None
    if not isinstance(data, dict):
        return None
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        return None
    removed = False
    for key in list(servers):
        if _is_our_mcp_entry(key, servers.get(key), base_url, tenant_id):
            del servers[key]
            removed = True
    if not removed:
        return None
    data["mcpServers"] = servers
    return json.dumps(data, indent=2) + "\n"


def _restore_mcp_json(
    snapshots: list[tuple["Path", "str | None"]],
    base_url: str = "",
    tenant_id: str = "",
) -> None:
    """Undo :func:`_install_mcp_json`: restore originals, delete files we created.

    ef162c28 — before restoring an original that we merged into, strip any of
    OUR connector entries (legacy + new + custom) from it. Normally the original
    had none of ours, so :func:`_strip_our_mcp_entries` returns ``None`` and we
    write the original text BYTE-FOR-BYTE (no json.dumps reformatting). But if a
    prior tunnel crashed and left stale ``meridian-fs``/etc. entries in the file
    we snapshotted, those are stripped so we never resurrect them. Files we
    CREATED (original is ``None``) are deleted outright.
    """
    for path, original in snapshots:
        try:
            if original is None:
                path.unlink(missing_ok=True)
            else:
                cleaned = _strip_our_mcp_entries(original, base_url, tenant_id)
                path.write_text(
                    cleaned if cleaned is not None else original, encoding="utf-8"
                )
        except Exception:  # noqa: BLE001 — best-effort cleanup
            pass


async def run_tunnel(
    *,
    token: str | None = None,
    base_url: str | None = None,
    repo_path: str | None = None,
    extra_fs_roots: list[str] | None = None,
    port: int = DEFAULT_PROXY_PORT,
    code_port: int = DEFAULT_CODE_PROXY_PORT,
    extract_port: int = DEFAULT_EXTRACT_PROXY_PORT,
    code_dirs: list[str] | None = None,
) -> int:
    """Resolve config, start local proxies, and keep all tunnels up.

    The tenant's tunnel plugin registry (from ``/me``, 3-slot model) decides what
    runs behind each of the three transport slots — filesystem (*port*),
    code-intel (*code_port*, auto-installing codebase-memory-mcp when its command
    is not overridden), and code-extractor (*extract_port*). Each slot can be
    disabled, given a command override, or assigned a different port via the
    per-tenant config; an empty config reproduces the built-in defaults exactly.
    Blocks until interrupted (Ctrl-C). Returns a process exit code.

    If *code_dirs* is provided, calls ``index_repository`` on each path via the
    code-intel proxy after it starts — so the first session has a fully indexed
    codebase without any manual tool call.
    """
    _force_utf8_io()
    # Resolve base_url first — it's the cache key for the stored token.
    base_url = _resolve_base_url(base_url)
    # b970fe07 — remember whether the caller explicitly set repo_path / code_dirs
    # on the CLI *before* we default repo_path to cwd. When a flag is absent, a
    # dashboard-configured value (serena_repo_path / codebase_code_dirs, fetched
    # below) may fill it in; when a flag IS present, the CLI always wins and the
    # config is ignored — so an unset config reproduces today's exact behaviour.
    _repo_path_from_cli = bool(repo_path)
    _code_dirs_from_cli = bool(code_dirs)
    repo_path = str(Path(repo_path or Path.cwd()).resolve())

    # Token priority: --token / env vars > cached token > browser auth flow.
    # The --token flag is kept for CI/scripted use so the raw value is never in
    # shell history; the env-var path likewise keeps it out of `ps` output.
    resolved_token = _resolve_token(token)
    _from_cache = False
    _browser_authed = False
    if not resolved_token:
        resolved_token = _read_cached_token(base_url) or ""
        _from_cache = bool(resolved_token)
    if not resolved_token:
        resolved_token = await _browser_auth_flow(base_url)
        _browser_authed = bool(resolved_token)
    if not resolved_token:
        print("error: tunnel auth cancelled or timed out.", file=sys.stderr)
        return 2
    token = resolved_token

    # 1. Resolve tenant_id + plan from /me.
    try:
        me = await _fetch_me(base_url, token)
    except Exception as exc:
        # Cached token may be revoked — retry via browser flow once, then give up.
        if _from_cache and not _browser_authed:
            print("cached token rejected — re-authenticating...", file=sys.stderr, flush=True)
            token = await _browser_auth_flow(base_url)
            _browser_authed = bool(token)
            if not token:
                print("error: tunnel auth cancelled.", file=sys.stderr)
                return 2
            try:
                me = await _fetch_me(base_url, token)
            except Exception as exc2:
                print(f"error: could not reach {base_url}/me: {exc2}", file=sys.stderr)
                return 1
        else:
            print(f"error: could not reach {base_url}/me: {exc}", file=sys.stderr)
            return 1

    # Cache the token only after /me confirms it works.
    if _browser_authed:
        _write_cached_token(base_url, token)

    tenant_id = me.get("tenant_id")
    if not tenant_id:
        print(
            "error: /me returned no tenant_id — is this a hosted account with "
            "a valid token?",
            file=sys.stderr,
        )
        return 1

    plan = me.get("plan") or "free"
    if plan not in ("pro", "admin") and not me.get("is_internal"):
        print(
            f"error: the tunnel is a Pro feature; your plan is '{plan}'. "
            "Upgrade at " + base_url + "/pricing",
            file=sys.stderr,
        )
        return 1

    # Resolve the tenant's tunnel plugin registry (3-slot model). The server's
    # /me response returns the already-resolved list (defaults + per-tenant
    # overrides); fall back to built-in defaults for older servers. Each slot
    # may be disabled, given a command override, or assigned a different port.
    from .tunnel_plugins import (
        resolve_plugins, resolve_custom_plugins, detect_office_binaries,
        expand_command, SERENA_EXTRACT_COMMAND,
    )
    # Auto-enable Office slots whose launcher is installed on this machine, unless
    # the user explicitly configured them. Resolve locally from the raw config
    # (tunnel_plugins_config) so binary-detection applies; fall back to the
    # server-resolved list for older servers that don't send the raw config.
    detected = detect_office_binaries()
    if "tunnel_plugins_config" in me:
        plugins = resolve_plugins(me.get("tunnel_plugins_config"), detected_slots=detected)
    else:
        plugins = me.get("tunnel_plugins") or resolve_plugins(None, detected_slots=detected)
    by_slot = {p.get("slot"): p for p in plugins if isinstance(p, dict)}
    fs_plugin = by_slot.get("fs") or {}
    code_plugin = by_slot.get("code") or {}
    extract_plugin = by_slot.get("extract") or {}
    ppt_plugin = by_slot.get("ppt") or {}
    word_plugin = by_slot.get("word") or {}
    dc_plugin = by_slot.get("dc") or {}
    # Per-slot effective ports (override > the run_tunnel arg default).
    fs_port = int(fs_plugin.get("port") or port)
    code_port = int(code_plugin.get("port") or code_port)
    extract_port = int(extract_plugin.get("port") or extract_port)
    ppt_port = int(ppt_plugin.get("port") or 8811)
    word_port = int(word_plugin.get("port") or 8812)
    dc_port = int(dc_plugin.get("port") or 8813)

    # Filesystem connector roots (executor_config.filesystem_roots, unioned across
    # the tenant's projects). Empty → fall back to the home dir (repo_path).
    # known_repo_paths are trust anchors for silent auto-add (b9d1b606).
    # b970fe07 — also fetch the dashboard-configured Serena default repo path and
    # code-intel index dirs (extract/code slots), consumed fall-back-safe below.
    fs_roots, known_repo_paths, cfg_serena_repo_path, cfg_code_dirs = (
        await _fetch_filesystem_roots(base_url, token)
    )
    # b970fe07 — Serena's default --project. The CLI --repo always wins; only when
    # it was absent (repo_path defaulted to cwd) does a configured serena_repo_path
    # take over. Unset config → serena_repo_path stays == repo_path (today's cwd
    # default, unchanged behaviour).
    serena_repo_path = repo_path
    if not _repo_path_from_cli and cfg_serena_repo_path:
        cfg_serena_repo_path = _normalize_path_arg(cfg_serena_repo_path)
        try:
            serena_repo_path = str(Path(cfg_serena_repo_path).resolve())
            print(
                f"  code-extractor:    Serena default repo from dashboard config: "
                f"{serena_repo_path}",
                flush=True,
            )
        except Exception:  # noqa: BLE001 — bad config path → keep the cwd default
            serena_repo_path = repo_path
    # b970fe07 — code-intel auto-index dirs. --code-dir always wins; only when it
    # was absent does the configured codebase_code_dirs fill in. Unset config →
    # code_dirs stays as passed (today's behaviour: no auto-index unless flagged).
    if not _code_dirs_from_cli and cfg_code_dirs:
        code_dirs = list(cfg_code_dirs)
        print(
            f"  code-intel:        auto-index dirs from dashboard config: "
            f"{', '.join(code_dirs)}",
            flush=True,
        )
    # cbbd0eb4 — extra --repo paths (after the first) become additional fs roots,
    # resolved + deduped onto the front so the connector serves exactly the dirs
    # the user named (and the active repo_path) — not a broad parent dir.
    if extra_fs_roots:
        _resolved_extra: list[str] = []
        for _r in extra_fs_roots:
            try:
                _rp = str(Path(_r).resolve())
            except Exception:  # noqa: BLE001
                continue
            if _rp and _rp not in _resolved_extra:
                _resolved_extra.append(_rp)
        if _resolved_extra:
            _union = list(dict.fromkeys([*fs_roots, *_resolved_extra]))
            fs_roots = _union
            # The extra roots are also trust anchors for silent auto-add.
            known_repo_paths = list(dict.fromkeys([*known_repo_paths, *_resolved_extra]))

    # 1c. Node.js gate (d1c528f5) — every slot runs its inner server through
    #     ``npx mcp-proxy``, so without Node the proxies can't start. Auto-install
    #     via fnm when the workspace opts in (or MERIDIAN_AUTO_INSTALL_NODE=1);
    #     otherwise print a clear error and stop rather than spawning broken
    #     proxies that silently 503 on first call.
    _auto_install_node = bool(me.get("auto_install_node_deps")) or (
        os.environ.get("MERIDIAN_AUTO_INSTALL_NODE", "").strip().lower()
        in ("1", "true", "yes")
    )
    if not _ensure_node(_auto_install_node):
        return 1

    # 2. Build commands for all enabled slots. Processes are NOT spawned here —
    #    lazy spawning (3649a61a) defers each proxy until its first incoming
    #    request, then auto-kills after _IDLE_KILL_SECONDS of idle time.
    npx = _find_npx()
    # SlotProxy objects (one per enabled slot) replace the old proc_holders list.
    slot_proxies: list[SlotProxy] = []
    # Track which built-in slots have a registered SlotProxy (for URL printing).
    proxy_fs: SlotProxy | None = None
    proxy_code: SlotProxy | None = None
    proxy_extract: SlotProxy | None = None
    serena_pool: SerenaDaemonPool | None = None  # 64650cb4 — default-Serena extract
    office_proxies: dict[str, SlotProxy] = {}
    office_ports = {"ppt": ppt_port, "word": word_port, "dc": dc_port}
    # 4ea1b9d5 — slots whose session_mode is "persistent" (e.g. Desktop
    # Commander): they keep a stateful inner process, so they skip the
    # idle-killer that would otherwise tear the session down after 30min.
    persistent_slots: set[str] = set()

    if fs_plugin.get("enabled", True):
        fs_override = fs_plugin.get("command")
        cmd_fs = (
            _build_proxy_for_inner(npx, list(fs_override), fs_port)
            if fs_override else _build_proxy_command(npx, repo_path, fs_port, roots=fs_roots)
        )
        _served = ", ".join(fs_roots) if fs_roots else repo_path
        print(f"meridian tunnel: serving {_served}", flush=True)
        # 59c0e609 — surface roots the inner filesystem server will silently drop
        # (missing dir / Windows space-in-path) so a "served 2 of 3" is diagnosable.
        for _r, _why in _unservable_roots(fs_roots if fs_override is None else None):
            print(
                f"  filesystem: WARNING configured root will NOT be served — {_r} ({_why})",
                flush=True, file=sys.stderr,
            )
        print(f"  filesystem:        lazy-spawn on port {fs_port}", flush=True)
        proxy_fs = SlotProxy(cmd_fs, fs_port, "fs")
        slot_proxies.append(proxy_fs)
    else:
        print("  filesystem:        disabled (tunnel_plugins config)", flush=True)

    # 3. Code-intel slot (slot "code"). Resolve the command (including auto-
    #    install of codebase-memory-mcp) at startup so we fail fast if it's
    #    unavailable, but defer the actual Popen until first request.
    if not code_plugin.get("enabled", True):
        print("  code-intel:        disabled (tunnel_plugins config)", flush=True)
    elif code_plugin.get("command"):
        cmd_code = _build_proxy_for_inner(npx, list(code_plugin["command"]), code_port)
        print(f"  code-intel:        lazy-spawn on port {code_port} (custom command)", flush=True)
        proxy_code = SlotProxy(cmd_code, code_port, "code")
        slot_proxies.append(proxy_code)
    else:
        code_binary = await _ensure_codebase_memory_mcp()
        if code_binary is not None:
            managed_bin = str(_managed_bin_dir())
            if managed_bin not in os.environ.get("PATH", ""):
                os.environ["PATH"] = managed_bin + os.pathsep + os.environ.get("PATH", "")
            cmd_code = _build_code_proxy_command(npx, code_binary, code_port)
            # 89bc72c4 — the command builder makes a spaced binary path safe for
            # cmd.exe via its 8.3 short name. If the resolved inner token STILL
            # contains a space (8.3 generation disabled on the volume), the slot
            # would hit WinError 3 on spawn — warn so it's diagnosable rather than
            # a silent dead dot. Mirrors the FS slot's _unservable_roots warning.
            if sys.platform == "win32" and " " in cmd_code[-1]:
                print(
                    "  code-intel: WARNING binary path contains a space and no 8.3 "
                    f"short name was available — {cmd_code[-1]!r}. The slot may fail "
                    "to launch (cmd.exe splits on spaces). Install codebase-memory-mcp "
                    "under a space-free path, or enable 8.3 name generation.",
                    flush=True, file=sys.stderr,
                )
            print(f"  code-intel:        lazy-spawn on port {code_port}", flush=True)
            proxy_code = SlotProxy(cmd_code, code_port, "code")
            slot_proxies.append(proxy_code)
        else:
            print(
                "  code-intel:        not available (codebase-memory-mcp could not be installed)",
                flush=True,
            )

    # 4. Code-extractor slot. Same lazy pattern — command resolved now, process
    #    deferred to first request.
    if not extract_plugin.get("enabled", True):
        print("  code-extractor:    disabled (tunnel_plugins config)", flush=True)
    else:
        ext_raw = extract_plugin.get("command")
        if ext_raw == SERENA_EXTRACT_COMMAND:
            # 64650cb4 — default Serena: a per-repo_path daemon pool instead of a
            # single fixed --project instance, so executor sessions touching other
            # repos no longer hit Serena's "outside configured workspaces" error.
            serena_pool = SerenaDaemonPool(
                # b970fe07 — dashboard serena_repo_path fills this in when --repo
                # was not passed; otherwise it's the CLI repo_path (== cwd default).
                default_repo_path=serena_repo_path,
                spawn=lambda cmd: subprocess.Popen(cmd, **_spawn_kwargs()),
            )
            print(
                f"  code-extractor:    Serena daemon pool (lazy, per repo_path) "
                f"from port {SERENA_POOL_BASE_PORT}",
                flush=True,
            )
        elif (ext_override := expand_command(ext_raw, repo_path=repo_path)):
            cmd_extract = _build_proxy_for_inner(npx, list(ext_override), extract_port)
            print(f"  code-extractor:    lazy-spawn on port {extract_port} (custom command)", flush=True)
            proxy_extract = SlotProxy(cmd_extract, extract_port, "extract")
            slot_proxies.append(proxy_extract)
        else:
            extractor_inner = _resolve_extractor_inner_cmd()
            if extractor_inner is not None:
                cmd_extract = _build_extractor_proxy_command(npx, extractor_inner, extract_port)
                print(f"  code-extractor:    lazy-spawn on port {extract_port}", flush=True)
                proxy_extract = SlotProxy(cmd_extract, extract_port, "extract")
                slot_proxies.append(proxy_extract)
            else:
                print(
                    "  code-extractor:    not available (uvx missing and pip install failed)",
                    flush=True,
                )

    # 4b. Office MCP slots (ppt/word/dc). Off by default; enabled via dashboard.
    for slot, plugin, human in (("ppt", ppt_plugin, "PowerPoint"),
                                ("word", word_plugin, "Word"),
                                ("dc", dc_plugin, "Desktop Commander")):
        if not plugin.get("enabled", False):
            continue
        cmd = _office_slot_command(slot, plugin)
        if not cmd:
            # 0dfb107e — the slot is ENABLED but has no runnable command (a
            # misconfigured override that coerced to empty). Previously we
            # `continue`d silently, so the slot vanished from the whole startup
            # log with ZERO warning — undiagnosable. Mirror the filesystem
            # missing-root WARNING and the code-intel "not available" line so an
            # operator can see WHY an expected slot is absent.
            _warn = _office_slot_warning(slot, human, plugin)
            if _warn:
                print(_warn, flush=True, file=sys.stderr)
            continue
        oport = office_ports[slot]
        # 2b04a361 — Office slots spawn third-party Python MCP servers (docx-mcp,
        # powerpoint-mcp) that log non-ASCII; force UTF-8 stdio in the child env
        # so their own loggers can't crash on Windows' cp1252 console encoding.
        spawn_env = _office_slot_spawn_env(plugin.get("env"))
        # 4ea1b9d5 — persistent slots omit --stateless so their inner process
        # keeps state across requests (DC terminal sessions).
        _persistent = plugin.get("session_mode") == "persistent"
        cmd_office = _build_proxy_for_inner(npx, list(cmd), oport, stateless=not _persistent)
        mode_note = " (persistent)" if _persistent else ""
        print(f"  {human.lower():<16}lazy-spawn on port {oport}{mode_note}", flush=True)
        op = SlotProxy(cmd_office, oport, slot, env=spawn_env)
        office_proxies[slot] = op
        slot_proxies.append(op)
        if _persistent:
            persistent_slots.add(slot)

    # 44892730 — before anything spawns, clear any stale prior-generation
    # process still bound to each registered slot's port (see
    # _kill_stale_port_occupant's docstring for the full root-cause). Every
    # slot registered above is lazy-spawn (ensure_running fires on first
    # request), so this always runs well before any of them actually launch.
    for _sp in slot_proxies:
        _kill_stale_port_occupant(_sp.port, _sp.label)

    # 4c. Custom plugins (LOCAL-ONLY) — still eagerly spawned because they serve
    #     the local .mcp.json directly without a server relay route.
    custom_plugins = resolve_custom_plugins(me.get("tunnel_plugins_config"))
    running_custom: list[dict] = []
    # proc_holders is kept for custom plugins (eager, no lazy-spawn).
    proc_holders: list[dict] = []
    for cp in custom_plugins:
        if not cp.get("enabled", True):
            continue
        cname = cp["name"]
        cport = cp["port"]
        cmd_inner = expand_command(cp["command"], repo_path=repo_path)
        if not cmd_inner:
            continue
        cmd_custom = _build_proxy_for_inner(npx, list(cmd_inner), cport)
        # 194a7776 — merge the plugin's optional env over the parent env at spawn
        # (e.g. a local Zotero MCP needs ZOTERO_LOCAL=true). None inherits parent.
        spawn_env = _plugin_spawn_env(cp.get("env"))
        # 44892730 — custom plugins spawn eagerly (unlike the lazy SlotProxy
        # slots above), so the same stale-prior-generation-process gap applies
        # here too, right before the Popen below.
        _kill_stale_port_occupant(cport, f"custom:{cname}")
        print(f"  custom:{cname:<9}http://127.0.0.1:{cport}", flush=True)
        try:
            proc_custom = subprocess.Popen(cmd_custom, env=spawn_env, **_spawn_kwargs())
            proc_holders.append({"proc": proc_custom, "cmd": cmd_custom, "env": spawn_env, "label": f"custom:{cname}"})
            running_custom.append({"name": cname, "port": cport})
        except Exception as exc:
            print(f"  warning: could not start custom plugin {cname!r}: {exc}", file=sys.stderr)

    # 5. Print permanent URLs.
    print("", flush=True)
    # Best-effort update nudge: the server reports its version in /me (already
    # fetched above). Fully fail-open — a missing/unparseable version, or any
    # exception, silently skips the notice and never blocks the tunnel.
    try:
        _srv_ver = me.get("server_version")
        if _srv_ver:
            # 23ba76a2 — branch on the update mode (default 'ask' = notify-then-confirm).
            _action = _update_action(
                _resolve_update_mode(), __version__, str(_srv_ver), sys.stdin.isatty()
            )
            if _action != "none":
                _notice = _update_notice(__version__, str(_srv_ver))
                if _notice:
                    print(_notice, flush=True)
                if _action == "confirm":
                    try:
                        _ans = input("  Update now? [y/N] ").strip().lower()
                    except (EOFError, KeyboardInterrupt):
                        _ans = ""
                    if _ans in ("y", "yes"):
                        _perform_self_update()
                    else:
                        print("  skipped — continuing on the current version.", flush=True)
                elif _action == "auto":
                    print("  update mode is full-auto — updating now.", flush=True)
                    _perform_self_update()
    except Exception:  # noqa: BLE001 — informational only, never block startup
        pass
    print("  Tunnel URLs (for Cursor / non-claude.ai clients only):", flush=True)
    if proxy_fs is not None:
        print(f"    Filesystem:      {_permanent_url(base_url, tenant_id)}", flush=True)
    if proxy_code is not None:
        print(f"    Code Intel:      {_permanent_code_url(base_url, tenant_id)}", flush=True)
    if proxy_extract is not None or serena_pool is not None:
        print(f"    Code Extractor:  {_permanent_extract_url(base_url, tenant_id)}", flush=True)
    if "ppt" in office_proxies:
        print(f"    PowerPoint:      {_permanent_office_url(base_url, tenant_id, 'ppt')}", flush=True)
    if "word" in office_proxies:
        print(f"    Word:            {_permanent_office_url(base_url, tenant_id, 'word')}", flush=True)
    print(f"  claude.ai: all tools appear under your Meridian connector automatically.", flush=True)
    if proxy_fs is not None:
        print(f"  (SSE clients: {_sse_url(base_url, tenant_id)})", flush=True)
    print(f"  Proxies start on first use; idle for >{_IDLE_KILL_SECONDS // 60}min → auto-kill + restart.", flush=True)
    # First-run orientation: where npx/uvx drop downloaded MCP packages.
    try:
        _print_package_cache_locations()
    except Exception:  # noqa: BLE001 — informational only, never block startup
        pass
    print("", flush=True)

    # 5b. Auto-update local MCP client config.
    mcp_snapshots: list[tuple[Path, str | None]] = []
    try:
        mcp_snapshots = _install_mcp_json(Path.cwd(), base_url, tenant_id, running_custom)
    except Exception as exc:  # noqa: BLE001
        print(f"  warning: could not update local MCP config: {exc}", file=sys.stderr, flush=True)
    if mcp_snapshots:
        for path, _orig in mcp_snapshots:
            print(f"  Updated MCP config: {path}", flush=True)
        _custom_conns = "".join(
            f", meridian-custom-{cp['name']}" for cp in running_custom
        )
        # ef162c28 — connectors are now named by the plugin behind each slot
        # (filesystem / codebase-memory / serena) rather than by transport slot.
        _builtin_conns = ", ".join(TUNNEL_MCP_KEYS)
        print(
            f"    added connectors: {_builtin_conns}"
            f"{_custom_conns} (removed on Ctrl+C)",
            flush=True,
        )
        print(
            "    Other clients (e.g. Cursor → .cursor/mcp.json): add the three URLs above manually.",
            flush=True,
        )
        print("", flush=True)

    # 6. Auto-index code dirs via code-intel proxy (fire-and-forget background
    #    tasks). With lazy spawn we need to ensure the proxy is up first.
    index_tasks: list[asyncio.Task] = []
    if proxy_code is not None and code_dirs:
        async def _lazy_index(proxy: SlotProxy, code_dir: str) -> None:
            await proxy.ensure_running()
            if proxy.is_running:
                await _index_code_dir(proxy.port, code_dir)
        for _d in (code_dirs or []):
            _d = _normalize_path_arg(_d)
            if not _d:
                continue
            index_tasks.append(
                asyncio.ensure_future(_lazy_index(proxy_code, str(Path(_d).resolve())))
            )

    # 7. Run lazy reconnect loops — one per enabled slot. Each loop also gets an
    #    idle-killer coroutine that auto-kills the proxy after _IDLE_KILL_SECONDS.
    # b4455202 — per-slot tool-name display prefix (e.g. "Filesystem", "Serena")
    # the relay prepends to that slot's tools/list. Slots whose inner server
    # already self-prefixes carry prefix=None (no-op).
    slot_prefixes = {
        s: (by_slot.get(s) or {}).get("prefix")
        for s in ("fs", "code", "extract", "ppt", "word", "dc")
    }
    ws_fs = _ws_url(base_url, tenant_id, token)
    ws_code = _ws_code_url(base_url, tenant_id, token)
    ws_extract = _ws_extract_url(base_url, tenant_id, token)
    tasks: list[asyncio.Task] = []
    if proxy_fs is not None:
        tasks.append(asyncio.ensure_future(
            _reconnect_loop_lazy(
                ws_fs, proxy_fs, "fs",
                tool_prefix=slot_prefixes.get("fs"),
                known_repo_paths=known_repo_paths,
            )
        ))
        tasks.append(asyncio.ensure_future(_idle_killer(proxy_fs)))
    if proxy_code is not None:
        tasks.append(asyncio.ensure_future(
            _reconnect_loop_lazy(ws_code, proxy_code, "code", tool_prefix=slot_prefixes.get("code"))
        ))
        tasks.append(asyncio.ensure_future(_idle_killer(proxy_code)))
    if proxy_extract is not None:
        tasks.append(asyncio.ensure_future(
            _reconnect_loop_lazy(ws_extract, proxy_extract, "extract", tool_prefix=slot_prefixes.get("extract"))
        ))
        tasks.append(asyncio.ensure_future(_idle_killer(proxy_extract)))
    elif serena_pool is not None:
        # 64650cb4 — pooled Serena: per-repo routing + idle reaper instead of a
        # single SlotProxy + idle-killer.
        tasks.append(asyncio.ensure_future(
            _reconnect_loop_extract_pool(
                # b970fe07 — match the pool's default_repo_path (serena_repo_path,
                # which is the CLI repo_path unless a dashboard config overrode it).
                ws_extract, serena_pool, serena_repo_path, "extract",
                tool_prefix=slot_prefixes.get("extract"),
            )
        ))
        tasks.append(asyncio.ensure_future(_pool_idle_reaper(serena_pool)))
    for slot, oproxy in office_proxies.items():
        ws_office = _ws_office_url(base_url, tenant_id, token, slot)
        tasks.append(asyncio.ensure_future(
            _reconnect_loop_lazy(ws_office, oproxy, slot, tool_prefix=slot_prefixes.get(slot))
        ))
        # 4ea1b9d5 — persistent slots keep their inner process alive; don't
        # attach the idle-killer that would reset their session after 30min.
        if slot not in persistent_slots:
            tasks.append(asyncio.ensure_future(_idle_killer(oproxy)))
    # Custom plugins (eager) still use the regular reconnect + watchdog.
    for holder in proc_holders:
        tasks.append(asyncio.ensure_future(_proc_watchdog(holder)))
    if not tasks and not running_custom:
        print("error: no tunnel plugins enabled — nothing to serve.", file=sys.stderr)
        return 1

    # Install signal handlers for clean Ctrl+C shutdown.
    loop = asyncio.get_event_loop()

    def _request_stop(_signum=None, _frame=None):
        for t in tasks:
            loop.call_soon_threadsafe(t.cancel)

    installed_signals: list[tuple] = []
    _sigs = [signal.SIGINT]
    if sys.platform == "win32" and hasattr(signal, "SIGBREAK"):
        _sigs.append(signal.SIGBREAK)
    for _sig in _sigs:
        try:
            installed_signals.append((_sig, signal.getsignal(_sig)))
            signal.signal(_sig, _request_stop)
        except (ValueError, OSError):  # not the main thread / unsupported signal
            pass

    try:
        await asyncio.gather(*tasks)
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\ntunnel: shutting down", flush=True)
        return 0
    finally:
        # Restore original signal handlers.
        for _sig, _prev in installed_signals:
            try:
                signal.signal(_sig, _prev)
            except (ValueError, OSError):  # noqa: PERF203
                pass
        # Restore local MCP config before killing processes.
        if mcp_snapshots:
            _restore_mcp_json(mcp_snapshots, base_url, tenant_id)
            print("  Restored local MCP config (removed tunnel connectors).", flush=True)
        for t in tasks + index_tasks:
            t.cancel()
        # Kill all lazy slot proxies (only those actually running at shutdown).
        for sp in slot_proxies:
            sp.kill()
        # 64650cb4 — tear down every pooled Serena daemon.
        if serena_pool is not None:
            serena_pool.shutdown()
        # Kill custom (eager) plugin processes.
        for holder in proc_holders:
            _terminate_proc_tree(holder.get("proc"))
    return 0
