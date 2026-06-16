"""Tests for the `meridian --tunnel` client (meridian/tunnel_client.py).

Covers config resolution, URL building, the npx/proxy command construction,
and the request-relay framing (via an httpx MockTransport). Network and
subprocess orchestration in run_tunnel() is not exercised here.
"""
from __future__ import annotations

import asyncio
import base64
import json

import httpx
import pytest

from meridian import tunnel_client as tc


# ---------------------------------------------------------------------------
# Token resolution
# ---------------------------------------------------------------------------

def test_resolve_token_prefers_arg(monkeypatch):
    monkeypatch.setenv("MERIDIAN_API_KEY", "sk_meridian_env")
    assert tc._resolve_token("sk_meridian_arg") == "sk_meridian_arg"


def test_resolve_token_env_precedence(monkeypatch):
    monkeypatch.delenv("MERIDIAN_API_KEY", raising=False)
    monkeypatch.setenv("BEARER_TOKEN", "sk_meridian_bearer")
    assert tc._resolve_token() == "sk_meridian_bearer"

    monkeypatch.setenv("MERIDIAN_API_KEY", "sk_meridian_apikey")
    assert tc._resolve_token() == "sk_meridian_apikey"


def test_resolve_token_strips_bearer_prefix(monkeypatch):
    monkeypatch.delenv("MERIDIAN_API_KEY", raising=False)
    monkeypatch.delenv("BEARER_TOKEN", raising=False)
    assert tc._resolve_token("Bearer sk_meridian_x") == "sk_meridian_x"


def test_resolve_token_empty_when_unset(monkeypatch):
    monkeypatch.delenv("MERIDIAN_API_KEY", raising=False)
    monkeypatch.delenv("BEARER_TOKEN", raising=False)
    assert tc._resolve_token() == ""


# ---------------------------------------------------------------------------
# Base URL resolution
# ---------------------------------------------------------------------------

def test_resolve_base_url_default(monkeypatch):
    monkeypatch.delenv("MERIDIAN_URL", raising=False)
    assert tc._resolve_base_url() == tc.DEFAULT_BASE_URL


def test_resolve_base_url_arg_overrides_env(monkeypatch):
    monkeypatch.setenv("MERIDIAN_URL", "https://env.example.com")
    assert tc._resolve_base_url("https://arg.example.com/") == "https://arg.example.com"


def test_resolve_base_url_strips_trailing_slash(monkeypatch):
    monkeypatch.setenv("MERIDIAN_URL", "https://x.example.com/")
    assert tc._resolve_base_url() == "https://x.example.com"


# ---------------------------------------------------------------------------
# URL building
# ---------------------------------------------------------------------------

def test_ws_url_https_to_wss():
    url = tc._ws_url("https://usemeridian.us", "tenant-123", "sk_tok")
    assert url.startswith("wss://usemeridian.us/tunnel/tenant-123?token=")
    assert "token=sk_tok" in url


def test_ws_url_http_to_ws():
    url = tc._ws_url("http://localhost:7878", "t1", "tok")
    assert url.startswith("ws://localhost:7878/tunnel/t1?token=")


def test_ws_url_quotes_token():
    url = tc._ws_url("https://x", "t", "a/b+c d")
    # '/', '+', and space must be percent-encoded
    assert "a%2Fb%2Bc%20d" in url


def test_permanent_url_targets_mcp_transport_endpoint():
    # Must point at the /mcp transport, NOT the proxy root (which 404s).
    assert (
        tc._permanent_url("https://usemeridian.us/", "abc")
        == "https://usemeridian.us/fs/mcp/abc/mcp"
    )


def test_sse_url_targets_sse_endpoint():
    assert (
        tc._sse_url("https://usemeridian.us", "abc")
        == "https://usemeridian.us/fs/mcp/abc/sse"
    )


# ---------------------------------------------------------------------------
# npx + proxy command
# ---------------------------------------------------------------------------

def test_find_npx_returns_nonempty_string():
    assert isinstance(tc._find_npx(), str)
    assert tc._find_npx()


def test_build_proxy_command_structure():
    cmd = tc._build_proxy_command("npx", "/repo", port=9000)
    assert cmd[0] == "npx"
    assert "mcp-proxy" in cmd
    assert "--port" in cmd
    assert "9000" in cmd
    # The separator + the wrapped filesystem server must be present.
    assert "--" in cmd
    assert "@modelcontextprotocol/server-filesystem" in cmd
    assert cmd[-1] == "/repo"
    # mcp-proxy comes before the separator; filesystem server after it.
    sep = cmd.index("--")
    assert "mcp-proxy" in cmd[:sep]
    assert "@modelcontextprotocol/server-filesystem" in cmd[sep:]
    # Inner command is bare npx (resolved by mcp-proxy / the shell), not a path.
    assert cmd[sep + 1] == "npx"


def test_build_proxy_command_uses_shell_on_windows(monkeypatch):
    # --shell is required on Windows (Node refuses to spawn .cmd without it);
    # omitted elsewhere to avoid unescaped shell arg concatenation.
    monkeypatch.setattr(tc.sys, "platform", "win32")
    assert "--shell" in tc._build_proxy_command("npx.cmd", "C:/repo")
    monkeypatch.setattr(tc.sys, "platform", "linux")
    assert "--shell" not in tc._build_proxy_command("npx", "/repo")


# ---------------------------------------------------------------------------
# Request relay
# ---------------------------------------------------------------------------

def _relay(msg, handler):
    """Run _relay_request against a mock local proxy defined by `handler`."""
    async def _inner():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            return await tc._relay_request(client, "http://127.0.0.1:8808", msg)

    return asyncio.run(_inner())


def test_relay_request_success_roundtrip():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["body"] = request.content
        return httpx.Response(200, json={"ok": True})

    msg = {
        "type": "request",
        "id": "req-1",
        "method": "POST",
        "path": "/message",
        "query": "sessionId=42",
        "headers": {"content-type": "application/json", "host": "stale-host"},
        "body": base64.b64encode(b'{"hello":1}').decode(),
    }
    resp = _relay(msg, handler)

    assert resp["type"] == "response"
    assert resp["id"] == "req-1"
    assert resp["status"] == 200
    decoded = json.loads(base64.b64decode(resp["body"]))
    assert decoded == {"ok": True}
    # query was appended, body forwarded, stale Host dropped.
    assert captured["url"] == "http://127.0.0.1:8808/message?sessionId=42"
    assert captured["method"] == "POST"
    assert captured["body"] == b'{"hello":1}'


def test_relay_request_empty_body_when_no_content():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(204)

    msg = {"type": "request", "id": "r2", "method": "GET", "path": "/"}
    resp = _relay(msg, handler)
    assert resp["status"] == 204
    assert resp["body"] == ""


def test_relay_request_local_failure_returns_502():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    msg = {"type": "request", "id": "r3", "method": "GET", "path": "/sse"}
    resp = _relay(msg, handler)
    assert resp["type"] == "response"
    assert resp["id"] == "r3"
    assert resp["status"] == 502
    err = json.loads(base64.b64decode(resp["body"]))
    assert "local proxy error" in err["error"]


def test_relay_request_drops_host_header():
    seen_headers = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.update(request.headers)
        return httpx.Response(200)

    msg = {
        "type": "request", "id": "r4", "method": "GET", "path": "/",
        "headers": {"Host": "evil.example.com", "x-keep": "yes"},
    }
    _relay(msg, handler)
    # httpx sets Host to the real target, never the forwarded stale value.
    assert seen_headers.get("host") == "127.0.0.1:8808"
    assert seen_headers.get("x-keep") == "yes"
