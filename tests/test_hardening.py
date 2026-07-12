"""Coverage for the resilience primitives (e5f96adf / 1d021501).

`run_in_bulkhead` must (a) return a blocking function's result normally, (b) fail
fast with a typed `HeavyToolTimeout` when the call exceeds its deadline, and — the
whole point of the bulkhead — (c) keep a hung/saturated bulkhead from starving the
default `asyncio.to_thread` executor that unrelated work relies on.
"""
from __future__ import annotations

import asyncio
import threading
import time

import pytest

from meridian import hardening as h


@pytest.mark.asyncio
async def test_run_in_bulkhead_returns_result_normally():
    def _work(a, b, *, c):
        return a + b + c

    assert await h.run_in_bulkhead(_work, 1, 2, c=3, label="add") == 6


@pytest.mark.asyncio
async def test_run_in_bulkhead_times_out_fast():
    def _slow():
        time.sleep(5)
        return "never"

    t0 = time.monotonic()
    with pytest.raises(h.HeavyToolTimeout) as ei:
        await h.run_in_bulkhead(_slow, timeout=0.2, label="slow-walk")
    elapsed = time.monotonic() - t0
    assert elapsed < 2.0  # failed fast, did NOT wait the full 5s
    assert ei.value.label == "slow-walk"
    assert ei.value.timeout == 0.2


@pytest.mark.asyncio
async def test_bulkhead_saturation_does_not_starve_default_executor(monkeypatch):
    """The core bulkhead guarantee: even with every bulkhead worker hung, an
    unrelated `asyncio.to_thread` call (default executor) still runs promptly."""
    monkeypatch.setattr(h, "_BULKHEAD_MAX_WORKERS", 2)
    h._reset_for_tests()
    release = threading.Event()

    def _block():
        release.wait(timeout=5)
        return "done"

    hung: list = []
    try:
        # Saturate the 2-worker bulkhead with blocking tasks.
        hung = [
            asyncio.ensure_future(
                h.run_in_bulkhead(_block, timeout=10, label="hung")
            )
            for _ in range(2)
        ]
        await asyncio.sleep(0.15)  # let both occupy a worker

        # A further bulkhead call can't get a worker within its budget → fast-fail
        # (proves the deadline covers queue-wait, not just execution).
        with pytest.raises(h.HeavyToolTimeout):
            await h.run_in_bulkhead(_block, timeout=0.3, label="queued")

        # …yet the DEFAULT executor is completely unaffected — unrelated work runs.
        got = await asyncio.wait_for(
            asyncio.to_thread(lambda: "unrelated-ok"), timeout=1.0
        )
        assert got == "unrelated-ok"
    finally:
        release.set()
        if hung:
            await asyncio.gather(*hung, return_exceptions=True)
        h._reset_for_tests()


@pytest.mark.asyncio
async def test_run_in_bulkhead_propagates_the_functions_own_error():
    def _boom():
        raise ValueError("kaboom")

    with pytest.raises(ValueError, match="kaboom"):
        await h.run_in_bulkhead(_boom, label="boom")


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("MERIDIAN_HEAVY_TOOL_TIMEOUT", "12.5")
    monkeypatch.setenv("MERIDIAN_BULKHEAD_WORKERS", "7")
    assert h._env_float("MERIDIAN_HEAVY_TOOL_TIMEOUT", 90.0) == 12.5
    assert h._env_int("MERIDIAN_BULKHEAD_WORKERS", 4) == 7
    # bad / non-positive values fall back to the default
    monkeypatch.setenv("MERIDIAN_HEAVY_TOOL_TIMEOUT", "-3")
    monkeypatch.setenv("MERIDIAN_BULKHEAD_WORKERS", "notanint")
    assert h._env_float("MERIDIAN_HEAVY_TOOL_TIMEOUT", 90.0) == 90.0
    assert h._env_int("MERIDIAN_BULKHEAD_WORKERS", 4) == 4
