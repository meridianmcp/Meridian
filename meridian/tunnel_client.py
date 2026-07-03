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


# ---------------------------------------------------------------------------
# SlotProxy — lazy-spawn subprocess manager (3649a61a)
# ---------------------------------------------------------------------------

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
        """True if the subprocess is alive."""
        return self._proc is not None and self._proc.poll() is None

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
            # Brief pause to let the proxy's port bind before the first request
            # hits it.  28s request timeout means a slow startup is recoverable
            # (the caller retries on the next WS message) but 1s is usually
            # enough for mcp-proxy to be ready.
            await asyncio.sleep(1.0)
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


async def _preflight_slot(ws, port: int, label: str) -> bool:
    """Probe a slot after its first spawn; report unhealthy on failure. Returns
    the health result so callers can log it. (d71ba2e7)"""
    healthy = await _probe_slot_health(port)
    if not healthy:
        print(
            f"tunnel:{label}: pre-flight health check FAILED on port {port} — "
            "marking slot unhealthy (its tools will be suppressed)",
            file=sys.stderr, flush=True,
        )
        await _report_slot_health(ws, label, False)
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
    """
    cmd = [npx, "-y", "mcp-proxy", "--port", str(port),
           "--server", "stream", "--stateless"]
    if sys.platform == "win32":
        # Always add --shell on Windows: mcp-proxy (Node.js) cannot spawn .exe
        # binaries directly in some environments (missing DLL PATH, Node spawn
        # restrictions). cmd.exe handles resolution reliably. Mirrors the FS slot's
        # unconditional --shell. Limitation: paths with spaces in them won't work
        # via --shell (cmd.exe splits on spaces); .cmd shims need shell anyway per
        # Node 24 CVE-2024-27980.
        cmd.append("--shell")
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
    dirs = [r for r in (roots or []) if r and r.strip()] or [repo_path]
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
    to_add = [r for r in new_roots if r and r.strip() and r.strip() not in existing]
    if not to_add:
        return cmd, False
    return cmd[:idx + 1] + list(cmd[idx + 1:]) + to_add, True


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
) -> "tuple[list[str], list[str]]":
    """GET /tunnel/filesystem-roots — the dirs the fs connector may serve.

    Returns ``(filesystem_roots, known_repo_paths)``.  ``filesystem_roots`` are
    the explicit allowed dirs (unioned ``executor_config.filesystem_roots`` across
    projects); ``known_repo_paths`` are the implicit trust anchors
    (``executor_config.repo_path`` per project) used for silent auto-add.
    Both lists are empty on any error so the caller falls back to the
    home-directory default.
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
                return (
                    [str(x).strip() for x in roots if isinstance(x, str) and x.strip()],
                    [str(x).strip() for x in known if isinstance(x, str) and x.strip()],
                )
    except Exception:  # noqa: BLE001 — network/parse error → defaults
        pass
    return [], []


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
        return {
            "type": "response",
            "id": req_id,
            "status": 502,
            "headers": {"content-type": "application/json"},
            "body": base64.b64encode(err).decode(),
        }


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

# Connector keys we inject; used for restore so we only touch our own entries.
TUNNEL_MCP_KEYS = ("meridian-fs", "meridian-code", "meridian-extractor")


def _tunnel_mcp_entries(
    base_url: str, tenant_id: str, custom: "list[dict] | None" = None,
) -> dict[str, dict]:
    """The HTTP MCP connector entries pointing at this tenant's tunnel.

    The three built-in slots point at the hosted relay URLs (claude.ai-reachable).
    *custom* is the list of running user-defined plugins (``{"name", "port"}``);
    each gets an entry keyed ``meridian-custom-<name>`` pointing at its LOCAL
    mcp-proxy (``http://127.0.0.1:<port>/mcp``) — they are LOCAL-ONLY and have no
    server route, so a co-located client reaches them directly, not via the relay.
    """
    entries = {
        "meridian-fs": {"type": "http", "url": _permanent_url(base_url, tenant_id)},
        "meridian-code": {"type": "http", "url": _permanent_code_url(base_url, tenant_id)},
        "meridian-extractor": {"type": "http", "url": _permanent_extract_url(base_url, tenant_id)},
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


def _inject_mcp_entries(text: "str | None", entries: dict[str, dict]) -> str:
    """Merge *entries* under ``mcpServers`` in an existing `.mcp.json` body.

    *text* is the current file content (``None``/empty for a new file). Existing
    servers and other top-level keys are preserved. Returns the new file text.
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
    servers.update(entries)
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
            path.write_text(_inject_mcp_entries(original, entries), encoding="utf-8")
            snapshots.append((path, original))
        except Exception as exc:  # noqa: BLE001
            print(f"  warning: could not update {path}: {exc}", file=sys.stderr, flush=True)
    return snapshots


def _restore_mcp_json(snapshots: list[tuple["Path", "str | None"]]) -> None:
    """Undo :func:`_install_mcp_json`: restore originals, delete files we created."""
    for path, original in snapshots:
        try:
            if original is None:
                path.unlink(missing_ok=True)
            else:
                path.write_text(original, encoding="utf-8")
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
    fs_roots, known_repo_paths = await _fetch_filesystem_roots(base_url, token)
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
                default_repo_path=repo_path,
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
        cmd = plugin.get("command")
        if cmd is None and slot == "dc":
            cmd = _dc_default_command()
        if not cmd:
            continue
        oport = office_ports[slot]
        spawn_env = _plugin_spawn_env(plugin.get("env"))
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
            _notice = _update_notice(__version__, str(_srv_ver))
            if _notice:
                print(_notice, flush=True)
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
        print(
            "    added connectors: meridian-fs, meridian-code, meridian-extractor"
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
        for d in code_dirs:
            index_tasks.append(
                asyncio.ensure_future(_lazy_index(proxy_code, str(Path(d).resolve())))
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
                ws_extract, serena_pool, repo_path, "extract",
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
            _restore_mcp_json(mcp_snapshots)
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
