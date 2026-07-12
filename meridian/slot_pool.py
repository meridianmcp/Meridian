"""Elastic backend pool for a single stateless tunnel slot (39aae23f).

Each of the tunnel's fixed transport slots (fs/code/extract/ppt/word/docs/zotero)
rides ONE server-side route (see ``routes/tunnel.py``) that never changes. For a
``session_mode="stateless"`` slot every POST is handled independently (mcp-proxy's
``--stateless`` flag), so nothing stops us from load-balancing that single route
across **N inner backend copies** running client-side — each an identical
``mcp-proxy``-wrapped inner server on its own local port. More copies = more
concurrent in-flight requests for a hot slot, without adding any server route.

This module is the pure pool primitive: a data structure of backend copies plus
the dispatch (least-busy / round-robin) and elastic-sizing (min/max, idle
scale-down) logic. It deliberately does NOT open sockets, spawn real processes,
or touch the WebSocket relay — the process spawner, the port allocator, and the
clock are all injected, exactly like :class:`meridian.serena_pool.SerenaDaemonPool`.
That keeps the whole thing unit-testable with fakes and no subprocesses.

Scope (per the sprint item):
  * ``session_mode="stateless"`` slots only. ``persistent`` slots (Desktop
    Commander) keep a single copy — pooling identical copies would fork their
    per-session terminal state — and are handled elsewhere.
  * Elastic min=1 / max=2 to start, with idle scale-down of the extra copies.
  * The server route per slot stays FIXED; the fan-out is entirely client-side.

Wiring this pool into ``tunnel_client._run_connection_lazy`` (dispatch each WS
``request`` to ``acquire()``'s copy instead of a single :class:`SlotProxy`) is a
follow-up — this module is the tested building block that wiring will consume.
"""
from __future__ import annotations

import time
from typing import Any, Callable

# Elastic defaults (per the sprint item): start conservative — one always-on
# copy, burst to a second under load, and never below/above these.
DEFAULT_MIN_COPIES = 1
DEFAULT_MAX_COPIES = 2

# An extra (above-min) copy untouched for this long is torn down by
# :meth:`SlotPool.reap_idle`. Mirrors the tunnel's 30-min idle window.
IDLE_SCALE_DOWN_SECONDS = 30 * 60  # 30 minutes

# Dispatch strategies.
DISPATCH_LEAST_BUSY = "least_busy"
DISPATCH_ROUND_ROBIN = "round_robin"
_VALID_DISPATCH = (DISPATCH_LEAST_BUSY, DISPATCH_ROUND_ROBIN)


class BackendCopy:
    """One inner backend copy of a slot: a proxied inner server on a local port.

    Tracks the process handle (opaque — the pool never introspects it beyond a
    ``poll()`` liveness check), the port it listens on, an in-flight request
    counter (for least-busy dispatch), and a last-used timestamp (for idle
    scale-down). ``inflight`` is incremented by :meth:`SlotPool.acquire` and
    decremented by :meth:`SlotPool.release`.
    """

    __slots__ = ("port", "proc", "inflight", "last_used", "total_served")

    def __init__(self, port: int, proc: Any, last_used: float) -> None:
        self.port = port
        self.proc = proc
        self.inflight = 0
        self.last_used = last_used
        # Cumulative requests dispatched here (diagnostics / round-robin fairness).
        self.total_served = 0

    @property
    def is_alive(self) -> bool:
        """True if the underlying process handle is still running."""
        if self.proc is None:
            return False
        try:
            return self.proc.poll() is None
        except Exception:  # noqa: BLE001 — a bad handle counts as dead, never raises
            return False

    def touch(self, now: float) -> None:
        """Reset the idle timer (called when a request is dispatched here)."""
        self.last_used = now

    def idle_seconds(self, now: float) -> float:
        """Seconds since this copy last served a request (never negative)."""
        return max(0.0, now - self.last_used)


def resolve_pool_size(
    raw: Any,
    *,
    default_min: int = DEFAULT_MIN_COPIES,
    default_max: int = DEFAULT_MAX_COPIES,
) -> "tuple[int, int]":
    """Coerce a per-slot pool config into a sane ``(min_copies, max_copies)`` pair.

    Accepts the shapes a stored ``tunnel_plugins`` slot override might carry:

    * ``None`` / missing / garbage → ``(default_min, default_max)``.
    * an ``int`` N → ``(1, N)`` — "pool up to N copies" shorthand.
    * a ``dict`` with ``min``/``max`` (or ``min_copies``/``max_copies``) keys.

    Guarantees ``1 <= min <= max``: min floors at 1 (a stateless slot always has
    at least one copy), max is raised to min if a config inverts them, and
    non-int / bool values fall back to the defaults. Pure — no I/O.
    """
    def _as_int(value: Any, fallback: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            return fallback
        return value

    lo = default_min
    hi = default_max
    if isinstance(raw, bool):
        pass  # bool is not a valid size spec — keep defaults
    elif isinstance(raw, int):
        # "pool up to N" shorthand: min 1, max N.
        hi = raw
        lo = 1
    elif isinstance(raw, dict):
        lo = _as_int(raw.get("min", raw.get("min_copies")), default_min)
        hi = _as_int(raw.get("max", raw.get("max_copies")), default_max)

    lo = max(1, lo)
    hi = max(1, hi)
    if hi < lo:
        hi = lo
    return lo, hi


class SlotPool:
    """An elastic pool of identical backend copies behind one stateless slot.

    Copies are added lazily: :meth:`acquire` spawns the first copy on demand and
    bursts up to ``max_copies`` while existing copies are busy, then hands each
    request to the least-busy (or next round-robin) live copy. :meth:`release`
    marks a request done. :meth:`reap_idle` scales the pool back down to
    ``min_copies`` by tearing down extra copies idle past the TTL.

    Everything external is injected so the pool is fully unit-testable with no
    real subprocess, socket, or clock:

    Args:
        command_builder: ``(port) -> list[str]`` building one copy's launch
            command (the ``mcp-proxy``-wrapped inner server on ``port``). The pool
            never runs it directly; it is handed to ``spawn``.
        spawn: ``(cmd) -> proc`` returning an object with a ``poll()`` method
            (defaults to ``subprocess.Popen``).
        terminate: ``(proc) -> None`` tearing a copy down (defaults to a
            best-effort terminate→kill).
        now: ``() -> float`` monotonic clock (defaults to ``time.monotonic``).
        base_port: first port; successive copies take the next free port.
        min_copies / max_copies: elastic bounds (see :func:`resolve_pool_size`).
        dispatch: ``"least_busy"`` (default) or ``"round_robin"``.
        idle_scale_down_seconds: idle TTL for reaping ABOVE-min copies.
    """

    def __init__(
        self,
        command_builder: Callable[[int], list[str]],
        *,
        base_port: int,
        spawn: Callable[[list[str]], Any] | None = None,
        terminate: Callable[[Any], None] | None = None,
        now: Callable[[], float] = time.monotonic,
        min_copies: int = DEFAULT_MIN_COPIES,
        max_copies: int = DEFAULT_MAX_COPIES,
        dispatch: str = DISPATCH_LEAST_BUSY,
        idle_scale_down_seconds: float = IDLE_SCALE_DOWN_SECONDS,
        label: str = "slot",
    ) -> None:
        self._command_builder = command_builder
        self.base_port = base_port
        self._spawn = spawn if spawn is not None else _default_spawn
        self._terminate = terminate if terminate is not None else _default_terminate
        self._now = now
        # Normalize the bounds so a caller can't invert or zero them.
        self.min_copies = max(1, int(min_copies))
        self.max_copies = max(self.min_copies, int(max_copies))
        self.dispatch = dispatch if dispatch in _VALID_DISPATCH else DISPATCH_LEAST_BUSY
        self.idle_scale_down_seconds = idle_scale_down_seconds
        self.label = label
        self._copies: list[BackendCopy] = []
        # Monotonic cursor for round-robin dispatch.
        self._rr_cursor = 0

    # ── introspection ────────────────────────────────────────────────────────

    def __len__(self) -> int:
        return len(self._copies)

    @property
    def copies(self) -> "list[BackendCopy]":
        """Live view of the current backend copies (do not mutate)."""
        return self._copies

    @property
    def ports(self) -> "list[int]":
        """Ports of the current copies, in creation order."""
        return [c.port for c in self._copies]

    @property
    def total_inflight(self) -> int:
        """Sum of in-flight requests across all copies."""
        return sum(c.inflight for c in self._copies)

    def _live_copies(self) -> "list[BackendCopy]":
        """Copies whose process is still alive (prunes dead handles first)."""
        alive = [c for c in self._copies if c.is_alive]
        if len(alive) != len(self._copies):
            self._copies = alive
        return alive

    def _next_port(self) -> int:
        """Lowest ``base_port + k`` not currently bound by a live copy."""
        used = {c.port for c in self._copies}
        port = self.base_port
        while port in used:
            port += 1
        return port

    # ── elastic sizing ───────────────────────────────────────────────────────

    def _spawn_copy(self) -> BackendCopy:
        """Spawn one new backend copy on the next free port and register it."""
        port = self._next_port()
        cmd = self._command_builder(port)
        proc = self._spawn(cmd)
        copy = BackendCopy(port, proc, self._now())
        self._copies.append(copy)
        return copy

    def ensure_min(self) -> None:
        """Bring the pool up to ``min_copies`` live copies (spawns as needed).

        Idempotent: prunes dead copies first, then tops up. A stateless slot's
        pool keeps at least ``min_copies`` (default 1) warm; the burst copies
        above min are added on demand by :meth:`acquire` and reaped when idle.
        """
        live = self._live_copies()
        while len(live) < self.min_copies:
            self._spawn_copy()
            live = self._live_copies()

    def _should_burst(self, live: "list[BackendCopy]") -> bool:
        """True if we should spawn another copy: room under max AND all busy.

        We only burst when every existing live copy already has an in-flight
        request — i.e. there is no idle capacity to absorb the new request. This
        keeps the pool at its minimum under light load and only grows under real
        contention.
        """
        if len(live) >= self.max_copies:
            return False
        if not live:
            return True
        return all(c.inflight > 0 for c in live)

    def _select(self, live: "list[BackendCopy]") -> BackendCopy:
        """Pick a copy to serve the next request per the dispatch strategy."""
        if self.dispatch == DISPATCH_ROUND_ROBIN:
            # Advance the cursor over the current live set; total_served keeps the
            # distribution fair even as copies are added/removed.
            copy = live[self._rr_cursor % len(live)]
            self._rr_cursor += 1
            return copy
        # least_busy: fewest in-flight wins; ties break on fewest total served
        # (spread load evenly) then lowest port (stable/deterministic).
        return min(live, key=lambda c: (c.inflight, c.total_served, c.port))

    # ── dispatch ─────────────────────────────────────────────────────────────

    def acquire(self) -> BackendCopy:
        """Reserve a backend copy for one request; returns the chosen copy.

        Semantics:
          1. Ensure at least ``min_copies`` live copies exist.
          2. If every live copy is busy and we're under ``max_copies``, spawn a
             burst copy so the new request gets fresh capacity.
          3. Select a copy (least-busy or round-robin), increment its in-flight
             counter, bump its served count, and touch its idle timer.

        The caller MUST pair every ``acquire()`` with a :meth:`release` (use
        try/finally at the call site) so the in-flight counter stays accurate.
        """
        self.ensure_min()
        live = self._live_copies()
        if self._should_burst(live):
            self._spawn_copy()
            live = self._live_copies()
        copy = self._select(live)
        copy.inflight += 1
        copy.total_served += 1
        copy.touch(self._now())
        return copy

    def release(self, copy: BackendCopy) -> None:
        """Mark one request on ``copy`` as finished (decrements in-flight).

        Floors at zero so a double-release (or releasing an already-idle copy)
        can never drive the counter negative and corrupt least-busy dispatch.
        Also refreshes the idle timer — a copy that just finished work is "warm".
        """
        if copy is None:
            return
        if copy.inflight > 0:
            copy.inflight -= 1
        copy.touch(self._now())

    # ── scale-down / teardown ────────────────────────────────────────────────

    def reap_idle(self, now: float | None = None) -> "list[int]":
        """Tear down ABOVE-``min`` copies idle past the TTL. Returns reaped ports.

        Never reaps below ``min_copies``, never reaps a copy with in-flight
        requests, and reaps most-idle-first so a burst that's no longer needed
        collapses back to the minimum. Idempotent; safe to call on a timer.
        """
        when = self._now() if now is None else now
        live = self._live_copies()
        # Candidates: above-min, idle past TTL, and not currently serving.
        removable = [
            c for c in live
            if c.inflight == 0 and c.idle_seconds(when) >= self.idle_scale_down_seconds
        ]
        # Most-idle first, and only as many as keeps us at/above min.
        removable.sort(key=lambda c: c.idle_seconds(when), reverse=True)
        budget = len(live) - self.min_copies
        reaped: list[int] = []
        for copy in removable:
            if budget <= 0:
                break
            self._terminate(copy.proc)
            self._copies.remove(copy)
            reaped.append(copy.port)
            budget -= 1
        return reaped

    def shutdown(self) -> None:
        """Terminate every copy (tunnel exit). Idempotent."""
        for copy in list(self._copies):
            self._terminate(copy.proc)
        self._copies.clear()


def _default_spawn(cmd: list[str]) -> Any:
    """Default copy spawner — ``subprocess.Popen`` (imported lazily)."""
    import subprocess  # noqa: PLC0415 — keep module import light + injectable

    return subprocess.Popen(cmd)


def _default_terminate(proc: Any) -> None:
    """Best-effort terminate→kill of a backend copy process; never raises."""
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
