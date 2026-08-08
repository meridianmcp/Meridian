"""Deterministic tunnel connection lifecycle state machine (39c8cf2c).

Motivating incident: a `claude rc --permission-mode bypassPermissions`
session reported "Connected" almost immediately, then Remote Control
disconnected and the terminal session stopped responding — while the OLD
wrapper process remained alive holding its own, separate live connection.
That incident happened in a different codebase (the Claude Code CLI's own
Remote Control feature is not part of this repository — confirmed by
searching this tree for "remote_control"/"bridge_regist"/session-id patterns
before writing this module; none exist here). What IS actionable in this
repo is Meridian's OWN tunnel wrapper (``meridian/tunnel_client.py``), which
turns out to share the exact same architectural gap:

* ``_run_connection`` / ``_run_connection_lazy`` / ``_run_extract_pool_connection``
  each print "connected" the INSTANT ``websockets.connect()`` returns —
  before a single frame has ever been exchanged. The server
  (``meridian/routes/tunnel.py:tunnel_ws`` et al.) accepts the socket, THEN
  authenticates, and closes immediately (code 4401/4403) on a bad token or
  disallowed plan. A client hitting that path reports "connected" and then,
  a few milliseconds later, is torn down — the exact "reported Connected...
  then disconnected" pattern from the incident, just against Meridian's own
  relay instead of Claude's.
* There was no first-class, machine-readable notion of "this tunnel slot is
  actually ready" anywhere — only stderr print statements. Nothing else in
  the process (diagnostics, a future supervisor, a test) could ask "is slot
  X ready right now" without scraping logs.

This module gives the tunnel client that missing vocabulary:

* :class:`LifecycleState` / :class:`TunnelLifecycle` — an explicit,
  introspectable state machine per tunnel slot (``fs``, ``code``, ``extract``,
  each office/custom slot label, ...). ``READY`` is a distinct state from
  ``WS_OPEN``: a slot transitions through ``WS_OPEN`` the instant the socket
  handshake completes, but only reaches ``READY`` once the connection is
  confirmed usable (see :class:`ReadinessGate` below) — this is the
  "MCP-connected versus Remote-Control-ready" distinction the sprint notes
  ask for, applied to Meridian's own tunnel instead of Claude's RC feature.
* :class:`ReadinessGate` + :func:`start_grace_timer` / :func:`stop_grace_timer`
  — the actual readiness-confirmation mechanism. A connection is declared
  ready the moment EITHER (a) the first inbound frame arrives, or (b) the
  socket has stayed open for ``DEFAULT_READY_GRACE_SECONDS`` without being
  closed — whichever happens first. (a) confirms readiness immediately on a
  busy connection; (b) avoids waiting for the server's up-to-~120s idle-ping
  cadence on a healthy-but-quiet one. Gating strictly on "first message
  received" alone would have reproduced the exact 120s-worst-case problem
  this module exists to avoid — see the module-level constant's docstring.
* :class:`TunnelNeverReadyError` — raised by the connection functions when a
  socket opens and then ends (clean close, no exception) WITHOUT ever
  reaching ready. Before this fix, that case returned normally, which made
  ``_reconnect_loop``/``_reconnect_loop_lazy`` treat it as a *successful*
  connection attempt and reset backoff to zero — i.e. a server that keeps
  rejecting the connection immediately (bad token, disallowed plan, mid-
  incident outage) would busy-loop reconnecting with NO backoff at all. This
  is the "a clear failure state (not a silent hang)" acceptance criterion.

Scope note: this module is intentionally transport-agnostic pure Python
(state machine + two small asyncio helpers) with no import of
``tunnel_client`` — it has zero risk of circular imports and is fully unit-
testable without a real WebSocket. See ``tests/test_tunnel_lifecycle.py``.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable


class LifecycleState(str, Enum):
    """One tunnel slot's connection lifecycle. Deliberately a flat, total
    order of "what's happening right now" rather than a nested state tree —
    every transition method on :class:`TunnelLifecycle` maps to exactly one
    of these, and `.value` is what a diagnostics/JSON surface would show."""

    COLD = "cold"  # never connected yet
    CONNECTING = "connecting"  # a connect attempt is in flight
    WS_OPEN = "ws_open"  # transport handshake succeeded; not yet confirmed usable
    READY = "ready"  # confirmed usable — first frame OR grace window elapsed
    RECONNECTING = "reconnecting"  # a prior connection ended; backing off before retry
    NEVER_READY = "never_ready"  # opened then closed before ever reaching READY
    CLIENT_LOST = "client_lost"  # a genuine, exception-raising disconnect
    STOPPED = "stopped"  # graceful, intentional shutdown — terminal


# 39c8cf2c — how long a freshly-opened socket is given to either receive its
# first frame or simply stay open before being declared READY. Chosen well
# above realistic false-positive noise (an immediate server-side auth/plan
# rejection closes within milliseconds, not seconds) and well below anything
# a human would notice as "slow to connect". Deliberately NOT keyed to the
# server's own ~120s idle-ping cadence (meridian/routes/tunnel.py's
# `asyncio.wait_for(ws.receive_json(), timeout=120.0)`) — gating readiness on
# "received at least one frame" alone would leave a healthy-but-quiet
# connection sitting in WS_OPEN for up to two minutes, which is a regression,
# not a fix.
DEFAULT_READY_GRACE_SECONDS = 2.0

# Bounded ring buffer of recent transitions kept per lifecycle, for a
# diagnostics surface to show "how did we get here" without unbounded memory
# growth over a long-lived tunnel process.
_MAX_HISTORY = 20


@dataclass(frozen=True)
class Transition:
    state: LifecycleState
    at: float
    detail: str = ""


@dataclass
class TunnelLifecycle:
    """Introspectable connection state for ONE tunnel slot (e.g. "fs",
    "code", "word"). Pure bookkeeping — callers (``tunnel_client.py``) drive
    the transitions; this class never touches a socket or a clock other than
    the injectable ``clock`` (defaults to ``time.monotonic``, matching every
    other timing primitive in ``tunnel_client.py``)."""

    label: str
    clock: Callable[[], float] = time.monotonic
    state: LifecycleState = LifecycleState.COLD
    ready_since: "float | None" = None
    last_transition_at: "float | None" = None
    history: "list[Transition]" = field(default_factory=list)

    def _transition(self, state: LifecycleState, detail: str = "") -> None:
        now = self.clock()
        self.state = state
        self.last_transition_at = now
        self.history.append(Transition(state, now, detail))
        if len(self.history) > _MAX_HISTORY:
            del self.history[: len(self.history) - _MAX_HISTORY]
        if state is not LifecycleState.READY:
            self.ready_since = None

    def mark_connecting(self, detail: str = "") -> None:
        self._transition(LifecycleState.CONNECTING, detail)

    def mark_ws_open(self, detail: str = "") -> None:
        self._transition(LifecycleState.WS_OPEN, detail)

    def mark_ready(self, detail: str = "") -> None:
        self._transition(LifecycleState.READY, detail)
        self.ready_since = self.last_transition_at

    def mark_reconnecting(self, detail: str = "") -> None:
        self._transition(LifecycleState.RECONNECTING, detail)

    def mark_never_ready(self, detail: str = "") -> None:
        self._transition(LifecycleState.NEVER_READY, detail)

    def mark_client_lost(self, detail: str = "") -> None:
        self._transition(LifecycleState.CLIENT_LOST, detail)

    def mark_stopped(self, detail: str = "") -> None:
        self._transition(LifecycleState.STOPPED, detail)

    @property
    def is_ready(self) -> bool:
        return self.state is LifecycleState.READY

    def snapshot(self) -> "dict[str, Any]":
        """Machine-readable current state — the "first-class, machine-
        readable executability flag" shape the capability-manifest design
        contract (see AGENTS.md) asks lifecycle-style state to have, applied
        here to one tunnel slot instead of a whole handoff."""
        return {
            "label": self.label,
            "state": self.state.value,
            "is_ready": self.is_ready,
            "ready_since": self.ready_since,
            "last_transition_at": self.last_transition_at,
            "history": [
                {"state": t.state.value, "at": t.at, "detail": t.detail}
                for t in self.history
            ],
        }


class TunnelNeverReadyError(RuntimeError):
    """A connection opened (``websockets.connect()`` returned) and then
    ended — cleanly, with no other exception — before ever reaching READY.

    Raising this (instead of returning normally, which was the pre-39c8cf2c
    behavior) makes ``_reconnect_loop``/``_reconnect_loop_lazy`` apply their
    normal exponential backoff to this case exactly like any other failed
    attempt, instead of resetting backoff to zero and busy-looping against a
    server that keeps rejecting the connection immediately.
    """

    def __init__(self, label: str) -> None:
        self.label = label
        super().__init__(
            f"tunnel:{label}: connection closed before ever becoming ready "
            "(no frame was ever received and the readiness grace window did "
            "not elapse while the socket was open) — treating this as a "
            "failed connection attempt, not a successful one."
        )


class ReadinessGate:
    """Idempotent "has READY been announced yet for this one connection
    attempt" flag. Both the grace-timer task and the message loop call
    :meth:`announce`; only the FIRST caller gets ``True`` back (and should
    act on it — print/log/transition state), every subsequent call is a
    cheap no-op. This is what makes the "first message OR grace window,
    whichever is first" race safe: both sides can fire concurrently without
    double-announcing.
    """

    __slots__ = ("_announced",)

    def __init__(self) -> None:
        self._announced = False

    @property
    def announced(self) -> bool:
        return self._announced

    def announce(self) -> bool:
        if self._announced:
            return False
        self._announced = True
        return True


def start_grace_timer(
    announce: Callable[[], None], grace_seconds: float = DEFAULT_READY_GRACE_SECONDS,
) -> "asyncio.Task":
    """Start a background task that calls *announce* once *grace_seconds*
    elapse. *announce* is expected to be idempotent (e.g. wraps a
    :class:`ReadinessGate`) since the caller's own message loop may also call
    it independently. Cancelling the returned task before it fires is a
    normal, expected outcome (readiness was announced some other way first)
    and never raises or logs — see :func:`stop_grace_timer`.
    """

    async def _timer() -> None:
        try:
            await asyncio.sleep(grace_seconds)
        except asyncio.CancelledError:
            return
        announce()

    return asyncio.ensure_future(_timer())


async def stop_grace_timer(task: "asyncio.Task | None") -> None:
    """Cancel *task* (a :func:`start_grace_timer` result) and await it so the
    cancellation is fully processed before the caller proceeds — never
    raises, mirroring every other best-effort cleanup helper in
    ``tunnel_client.py`` (e.g. ``_release_owned_process_lease``)."""
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:  # noqa: BLE001 — cleanup must never raise
        pass


# ---------------------------------------------------------------------------
# Process-wide registry — one TunnelLifecycle per slot label, so a
# diagnostics surface (or a test) can ask "what's the state of the fs slot
# right now" without threading an object through every call site. Wiring a
# live diagnostics/MCP surface on top of this registry is explicit follow-up
# scope (see the sprint item's report) — the registry itself is ready to be
# read from one today.
# ---------------------------------------------------------------------------

_registry: "dict[str, TunnelLifecycle]" = {}


def get_lifecycle(label: str) -> TunnelLifecycle:
    """Lazily-constructed, process-wide lifecycle tracker for *label*."""
    lc = _registry.get(label)
    if lc is None:
        lc = TunnelLifecycle(label=label)
        _registry[label] = lc
    return lc


def reset_registry() -> None:
    """Test seam: drop every tracked lifecycle so tests don't leak state
    across test functions in the same process."""
    _registry.clear()


def snapshot_all() -> "dict[str, dict[str, Any]]":
    """Machine-readable snapshot of every tracked slot's current lifecycle —
    the shape a future ``get_tunnel_diagnostics`` wiring would surface."""
    return {label: lc.snapshot() for label, lc in _registry.items()}
