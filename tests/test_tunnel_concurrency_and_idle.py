"""Tunnel request concurrency, the idle keepalive window, and header-only WebSocket auth.

Covers:
  - A slow request does not hold up a later request on the same socket. A sequential loop would deadlock here and time
    out, so the test fails instead of hanging.
  - The keepalive refreshes only tunnels that have relayed a request inside the idle window.
  - A WebSocket that sends its token in the URL gets no token. Only the Authorization header counts.
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
from types import SimpleNamespace

import pytest

from meridian import tunnel_client as tc
from meridian.routes import tunnel as tun


class _StubProxy:
    """The parts of SlotProxy the request loop touches, with the process always up."""

    port = 8808
    cmd = ["npx", "-y", "mcp-proxy@6.7.20", "--port", "8808"]
    is_running = True

    def touch(self) -> None:
        return None

    async def ensure_running(self) -> None:
        return None

    def kill(self) -> None:
        return None


def _empty_result_body() -> str:
    return base64.b64encode(b"{}").decode()


def test_a_slow_request_does_not_hold_up_a_later_request(monkeypatch):
    import httpx as _httpx
    import websockets as _ws

    async def scenario():
        release_a = asyncio.Event()
        sent: list[str] = []

        class FakeWS:
            def __init__(self):
                self._msgs = [
                    json.dumps({"type": "request", "id": "A", "method": "GET", "path": "/mcp", "body": None}),
                    json.dumps({"type": "request", "id": "B", "method": "GET", "path": "/mcp", "body": None}),
                ]

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return None

            def __aiter__(self):
                return self

            async def __anext__(self):
                if not self._msgs:
                    raise StopAsyncIteration
                await asyncio.sleep(0)
                return self._msgs.pop(0)

            async def send(self, data):
                payload = json.loads(data)
                sent.append(payload["id"])
                if payload["id"] == "B":
                    release_a.set()  # B's answer is out, so A may finish

        class FakeHttp:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return None

        async def fake_relay(http_client, local_base, msg, *, tool_prefix=None):
            if msg["id"] == "A":
                await release_a.wait()
            return {"type": "response", "id": msg["id"], "status": 200, "headers": {}, "body": _empty_result_body()}

        monkeypatch.setattr(_ws, "connect", lambda url, **kw: FakeWS())
        monkeypatch.setattr(_httpx, "AsyncClient", lambda *a, **k: FakeHttp())
        monkeypatch.setattr(tc, "_relay_request_unchecked", fake_relay)
        await tc._run_connection_lazy("wss://x/tunnel/t", _StubProxy(), "fs")
        return sent

    sent = asyncio.run(asyncio.wait_for(scenario(), timeout=30))
    assert sent == ["B", "A"], sent


# ---------------------------------------------------------------------------
# idle keepalive window
# ---------------------------------------------------------------------------

def test_keepalive_refreshes_only_tunnels_that_relayed_a_request_recently(monkeypatch):
    import meridian._deps as deps

    touched: list[str] = []

    async def fake_touch(conn):
        touched.append(conn)
        return [f"session-{conn}"]

    monkeypatch.setattr(tun, "active_tunnel_tenant_ids", lambda: {"busy", "idle", "never"})
    monkeypatch.setattr(deps, "_tenant_db_cache", {"busy": "conn-busy", "idle": "conn-idle", "never": "conn-never"})
    monkeypatch.setattr(tun.db_module, "touch_latest_active_session", fake_touch)
    now = time.monotonic()
    monkeypatch.setattr(tun, "_tunnel_last_request_at", {"busy": now, "idle": now - 10 * tun.TUNNEL_IDLE_KEEPALIVE_TTL_S})

    refreshed = asyncio.run(tun.keepalive_tunnel_sessions(app=SimpleNamespace()))

    assert touched == ["conn-busy"]
    assert refreshed == ["session-conn-busy"]


def test_relaying_a_request_marks_the_tenant_active(monkeypatch):
    monkeypatch.setattr(tun, "_tunnel_last_request_at", {})
    tun._mark_inflight("tenant-x", "fs", 1)
    try:
        assert "tenant-x" in tun._tunnel_last_request_at
        assert time.monotonic() - tun._tunnel_last_request_at["tenant-x"] < 5
    finally:
        tun._mark_inflight("tenant-x", "fs", -1)


# ---------------------------------------------------------------------------
# header-only WebSocket auth
# ---------------------------------------------------------------------------

def _ws_with(headers: dict, query: dict):
    return SimpleNamespace(headers=headers, query_params=query)


def test_token_in_the_authorization_header_is_accepted():
    token, legacy = tun._extract_ws_auth_token(_ws_with({"authorization": "Bearer sk_meridian_abc"}, {}))
    assert token == "Bearer sk_meridian_abc"
    assert legacy is False


def test_token_in_the_url_is_not_accepted(caplog):
    with caplog.at_level("WARNING"):
        token, legacy = tun._extract_ws_auth_token(_ws_with({}, {"token": "sk_meridian_leaked"}))
    assert token == ""
    assert legacy is False
    assert "sk_meridian_leaked" not in caplog.text
