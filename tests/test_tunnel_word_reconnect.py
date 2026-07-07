"""9250d89e — automated connect / kill / reconnect coverage for the WORD tunnel slot.

Before leaning on the docx word slot for thesis-critical work, prove the client
survives a dropped socket and comes back, rather than assuming "it installs =
it works". The word slot runs through the exact same client path the tunnel wires
for it: run_tunnel() mounts each office slot via
``_reconnect_loop_lazy(ws_office, oproxy, slot=...)`` (tunnel_client.py ~L3100),
and the slot's socket URL comes from ``_ws_office_url(..., "word")``.

The existing test_cov_tunnel_client suite exercises the lazy reconnect loop only
for the ``fs`` slot and only for a single drop. These tests target the word slot
specifically and the full resilience story:

* a drop is retried (connect -> kill -> reconnect), repeatedly;
* backoff grows exponentially across *consecutive* failures and RESETS to 1s
  after a healthy reconnect;
* backoff is capped at _MAX_BACKOFF so a long outage can't blow up the delay;
* the word slot's WebSocket URL routes to ``/tunnel-word/`` with a quoted token.
"""
from __future__ import annotations

import asyncio

import pytest

from meridian import tunnel_client as tc


def _word_proxy() -> "tc.SlotProxy":
    # The real default launcher for the word slot (5b065c2e). Port is irrelevant
    # to the reconnect loop but kept realistic.
    return tc.SlotProxy(["uvx", "docx-mcp"], 8811, "word")


def test_word_slot_reconnects_after_drop_with_backoff_reset(monkeypatch):
    """connect(ok) -> drop -> drop -> reconnect(ok) -> drop: every attempt targets
    the word slot, the loop never dies on a drop, and backoff resets after the
    healthy reconnect instead of climbing forever."""
    calls: list[str] = []
    sleeps: list[float] = []
    # None == a connection that ran then closed cleanly; Exception == a socket drop.
    script = iter([None, RuntimeError("drop"), RuntimeError("drop"), None, RuntimeError("drop")])

    async def fake_conn(ws_url, proxy, label, tool_prefix=None, known_repo_paths=None):
        calls.append(label)
        try:
            behavior = next(script)
        except StopIteration:
            behavior = None
        if isinstance(behavior, Exception):
            raise behavior
        return None

    async def fake_sleep(delay):
        sleeps.append(delay)
        if len(sleeps) >= 5:  # stop after the sequence we care about
            raise asyncio.CancelledError

    monkeypatch.setattr(tc, "_run_connection_lazy", fake_conn)
    monkeypatch.setattr(tc.asyncio, "sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(tc._reconnect_loop_lazy("wss://x/tunnel-word/t", _word_proxy(), "word"))

    # It kept reconnecting across drops — five attempts, all for the word slot.
    assert calls == ["word"] * 5
    # 1s after ok, then 2s,4s across the two consecutive drops, RESET to 1s after
    # the healthy reconnect, then 2s on the next drop. The reset is the point.
    assert sleeps == [1.0, 2.0, 4.0, 1.0, 2.0]


def test_word_slot_backoff_caps_at_max(monkeypatch):
    """A sustained outage (only drops) grows backoff 1,2,4,8,16 then pins at
    _MAX_BACKOFF — the delay never runs away."""
    sleeps: list[float] = []

    async def always_drop(ws_url, proxy, label, tool_prefix=None, known_repo_paths=None):
        raise ConnectionError("socket closed")

    async def fake_sleep(delay):
        sleeps.append(delay)
        if len(sleeps) >= 7:
            raise asyncio.CancelledError

    monkeypatch.setattr(tc, "_run_connection_lazy", always_drop)
    monkeypatch.setattr(tc.asyncio, "sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(tc._reconnect_loop_lazy("wss://x/tunnel-word/t", _word_proxy(), "word"))

    assert sleeps == [1.0, 2.0, 4.0, 8.0, 16.0, tc._MAX_BACKOFF, tc._MAX_BACKOFF]
    # Monotonic non-decreasing and never above the cap.
    assert all(b <= tc._MAX_BACKOFF for b in sleeps)
    assert sleeps == sorted(sleeps)


def test_word_slot_cancel_propagates_cleanly(monkeypatch):
    """A CancelledError from the connection (Ctrl+C / shutdown) propagates out of
    the word-slot loop instead of being swallowed as a reconnectable drop."""
    async def cancel_immediately(ws_url, proxy, label, tool_prefix=None, known_repo_paths=None):
        raise asyncio.CancelledError

    monkeypatch.setattr(tc, "_run_connection_lazy", cancel_immediately)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(tc._reconnect_loop_lazy("wss://x/tunnel-word/t", _word_proxy(), "word"))


def test_word_slot_ws_url_routes_to_tunnel_word():
    """The word slot's socket URL must hit the /tunnel-word/ route with the token
    URL-encoded (this is the URL _reconnect_loop_lazy is handed for the word slot)."""
    url = tc._ws_office_url("https://usemeridian.us", "tenant-42", "sk/with+special=", "word")
    assert url.startswith("wss://usemeridian.us/tunnel-word/tenant-42?token=")
    # token is percent-encoded (no raw / + = leaking into the query string)
    assert "sk/with+special=" not in url
    assert "sk%2Fwith%2Bspecial%3D" in url
    # http base downgrades to ws:// (local dev), still the word route.
    local = tc._ws_office_url("http://127.0.0.1:8000", "t", "tok", "word")
    assert local.startswith("ws://127.0.0.1:8000/tunnel-word/t?token=tok")
