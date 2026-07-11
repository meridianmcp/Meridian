"""Resilience primitives for MCP tool handlers — timeouts + bulkhead isolation.

Anchored in the real 5116078b incident (e5f96adf / 1d021501): ``search_outputs``
hung silently for the full ~4-minute client timeout with zero partial response,
and while it hung, logically-unrelated tools (desktop-commander, meridian-code)
appeared unreachable too. Two converging root causes, two converging fixes here:

1. **No deadline** — a potentially-unbounded filesystem walk / DuckDB build / OOXML
   parse had no internal or external time budget, so it blocked until the *client*
   gave up. :func:`run_in_bulkhead` wraps the call in ``asyncio.wait_for`` so it
   fails fast with a clear error instead ("a 503 in 5ms beats a 200 in 30s").

2. **Shared executor** — every ``asyncio.to_thread`` call shares ONE default
   ``ThreadPoolExecutor`` (``min(32, cpu+4)`` workers). Enough concurrently-hung
   heavy calls exhaust it, and then *every* other ``to_thread`` caller — including
   unrelated tunnel-adjacent work — queues behind them. That is the bulkhead
   anti-pattern ("a single slow backend exhausts the shared pool, blocking healthy
   services"). :data:`_bulkhead_executor` is a SEPARATE, small, bounded pool
   dedicated to heavy filesystem/CPU tools, so a hung walk can only ever consume a
   bulkhead slot — the default executor that everything else relies on stays free.

A hung OS call can't be force-killed mid-flight (a Python thread limitation), so a
timed-out call's thread keeps running until it returns on its own — but it does so
*inside the bulkhead*, where it can't starve the rest of the process. That is the
whole point of the compartment: a breach floods one bulkhead, not the vessel.
"""
from __future__ import annotations

import asyncio
import functools
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, TypeVar

_log = logging.getLogger(__name__)

_T = TypeVar("_T")


def _env_float(name: str, default: float) -> float:
    """Read a positive float from the environment, falling back on any bad value."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return default
    return val if val > 0 else default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return default
    return val if val > 0 else default


# Default backstop deadline for a heavy in-process tool. Comfortably above a
# normal cold index/parse, but WELL under the ~4-minute client timeout the
# 5116078b hang ran into — the server should fail fast on its own terms, not wait
# for the client to give up. Override per-call or via MERIDIAN_HEAVY_TOOL_TIMEOUT.
HEAVY_TOOL_TIMEOUT_SECONDS = _env_float("MERIDIAN_HEAVY_TOOL_TIMEOUT", 90.0)

# Size of the dedicated bulkhead pool. Small on purpose: it is a compartment, not
# the main thread pool. Enough for real heavy-tool concurrency, few enough that it
# can never be the thing that exhausts the machine. Override via
# MERIDIAN_BULKHEAD_WORKERS.
_BULKHEAD_MAX_WORKERS = _env_int("MERIDIAN_BULKHEAD_WORKERS", 4)

_executor_lock = threading.Lock()
_bulkhead_executor: ThreadPoolExecutor | None = None


class HeavyToolTimeout(TimeoutError):
    """A heavy in-process tool exceeded its wall-clock budget.

    Carries the ``label`` and ``timeout`` so a handler can turn it into a clear,
    fast tool-result error instead of hanging until the client times out.
    """

    def __init__(self, label: str, timeout: float) -> None:
        self.label = label
        self.timeout = timeout
        super().__init__(f"{label} exceeded its {timeout:g}s deadline and was abandoned")


def _get_bulkhead_executor() -> ThreadPoolExecutor:
    """Lazily create the process-wide bulkhead pool (thread-safe singleton)."""
    global _bulkhead_executor
    with _executor_lock:
        if _bulkhead_executor is None:
            _bulkhead_executor = ThreadPoolExecutor(
                max_workers=_BULKHEAD_MAX_WORKERS,
                thread_name_prefix="meridian-bulkhead",
            )
        return _bulkhead_executor


async def run_in_bulkhead(
    func: Callable[..., _T],
    *args: Any,
    timeout: float | None = None,
    label: str = "heavy-tool",
    **kwargs: Any,
) -> _T:
    """Run a blocking, filesystem/CPU-heavy ``func`` in the bulkhead pool under a
    hard wall-clock deadline.

    Isolation (1d021501): ``func`` runs in :data:`_bulkhead_executor`, NOT the
    default ``asyncio.to_thread`` executor — so even a fully-hung call can only
    occupy a bulkhead slot and never starves unrelated ``to_thread`` work.

    Deadline (e5f96adf): the await is bounded by ``asyncio.wait_for``. The budget
    covers BOTH queue-wait (all bulkhead workers busy) and execution, so a caller
    can never block indefinitely. On expiry raises :class:`HeavyToolTimeout` — the
    handler turns that into a fast, explicit error result. The underlying thread is
    left to finish on its own (it cannot be force-killed) but is contained to the
    bulkhead.

    ``timeout`` defaults to :data:`HEAVY_TOOL_TIMEOUT_SECONDS`.
    """
    budget = HEAVY_TOOL_TIMEOUT_SECONDS if timeout is None else timeout
    loop = asyncio.get_running_loop()
    call = functools.partial(func, *args, **kwargs)
    fut = loop.run_in_executor(_get_bulkhead_executor(), call)
    try:
        return await asyncio.wait_for(fut, timeout=budget)
    except (asyncio.TimeoutError, TimeoutError) as exc:
        _log.warning("bulkhead deadline hit: %s after %gs", label, budget)
        # Best-effort cancel; a running thread ignores it but the compartment
        # contains it. Re-raise as a typed error the handler can render cleanly.
        fut.cancel()
        raise HeavyToolTimeout(label, budget) from exc


def _reset_for_tests() -> None:
    """Tear down the bulkhead pool (test isolation only)."""
    global _bulkhead_executor
    with _executor_lock:
        if _bulkhead_executor is not None:
            _bulkhead_executor.shutdown(wait=False, cancel_futures=True)
            _bulkhead_executor = None
