"""Tests for the tunnel-auth sprint-item group worked together as one
cohesive change (they touch the same subsystem and cross-reference each
other):

  * 5fe96405 -- per-slot tunnel MCP entries all 401 (missing auth header +
    missing WWW-Authenticate discovery on the slot 401).
  * c7604ed7 -- tunnel client retries forever on auth-rejection close codes
    (4401/4403) instead of surfacing them; evicted (4000) needs a clearer
    message; cached-token lifetime now matches the device token's 90 days.
  * e37187f3 -- multi-machine tunnel: per-host token labels (so a second
    machine's install doesn't revoke the first machine's token) and
    per-host WS socket coexistence (so two machines don't evict each
    other), plus device_codes pruning.

Everything here is unit/integration-level against the actual modules --
no real network, subprocess, or live tunnel. New file (rather than adding
to the existing per-module test files) so this parallel-work session's
tests can't collide with another session's uncommitted edits to those
files, per this repo's own parallel-session git hygiene rule.
"""
from __future__ import annotations

import asyncio
import json
import time
import types
from unittest.mock import AsyncMock

import pytest

import meridian.server as srv  # import first -- avoids handler/server import cycle
from meridian import db as db_module
from meridian import tunnel_client as tc
from meridian.routes import oauth as oa
from meridian.routes import tunnel as tn


# ---------------------------------------------------------------------------
# 5fe96405 -- _tunnel_mcp_entries: slot entries now carry the bearer token
# ---------------------------------------------------------------------------

def test_slot_mcp_entries_carry_auth_header_when_token_provided():
    entries = tc._tunnel_mcp_entries("https://usemeridian.us", "tid-1", token="sk_meridian_x")
    for key in ("filesystem", "codebase-memory", "serena"):
        assert entries[key]["headers"] == {"Authorization": "Bearer sk_meridian_x"}
    # The pre-existing 'meridian' entry is unaffected.
    assert entries["meridian"]["headers"] == {"Authorization": "Bearer sk_meridian_x"}


def test_slot_mcp_entries_omit_headers_without_token():
    for tok in (None, ""):
        entries = tc._tunnel_mcp_entries("https://usemeridian.us", "tid-1", token=tok)
        for key in ("filesystem", "codebase-memory", "serena"):
            assert "headers" not in entries[key]
        assert "meridian" not in entries


def test_slot_mcp_entries_headers_are_independent_dicts_per_slot():
    # Mutating one slot's headers dict must never leak into another's.
    entries = tc._tunnel_mcp_entries("https://x", "t", token="tok")
    entries["filesystem"]["headers"]["X-Extra"] = "1"
    assert "X-Extra" not in entries["codebase-memory"]["headers"]
    assert "X-Extra" not in entries["serena"]["headers"]


def test_slot_mcp_entries_urls_unchanged():
    # The URL shape itself is untouched by the auth-header fix.
    entries = tc._tunnel_mcp_entries("https://usemeridian.us", "tid-123", token="tok")
    assert entries["filesystem"]["url"] == "https://usemeridian.us/fs/mcp/tid-123/mcp"
    assert entries["codebase-memory"]["url"] == "https://usemeridian.us/code/mcp/tid-123/mcp"
    assert entries["serena"]["url"] == "https://usemeridian.us/extract/mcp/tid-123/mcp"


# ---------------------------------------------------------------------------
# 5fe96405 -- _authorize_tunnel_proxy_caller: the slot 401 now carries
# WWW-Authenticate so an OAuth-capable client can discover where to auth.
# ---------------------------------------------------------------------------

def test_authorize_tunnel_proxy_caller_401_carries_www_authenticate(monkeypatch):
    async def _none(request, **kw):
        return None

    monkeypatch.setattr(tn, "_get_tenant_from_request", _none)
    req = types.SimpleNamespace(base_url="https://usemeridian.us/")
    resp = asyncio.run(tn._authorize_tunnel_proxy_caller("tid-1", req))

    assert resp is not None
    assert resp.status_code == 401
    www = resp.headers.get("www-authenticate")
    assert www is not None
    assert 'error="invalid_token"' in www
    assert "https://usemeridian.us/.well-known/oauth-protected-resource" in www


def test_authorize_tunnel_proxy_caller_returns_none_when_caller_owns_tenant(monkeypatch):
    async def fake_get_tenant(request, **kw):
        return {"id": "tid-1"}

    monkeypatch.setattr(tn, "_get_tenant_from_request", fake_get_tenant)
    req = types.SimpleNamespace(base_url="https://usemeridian.us/")
    resp = asyncio.run(tn._authorize_tunnel_proxy_caller("tid-1", req))
    assert resp is None


def test_authorize_tunnel_proxy_caller_401_when_caller_owns_different_tenant(monkeypatch):
    async def fake_get_tenant(request, **kw):
        return {"id": "some-other-tenant"}

    monkeypatch.setattr(tn, "_get_tenant_from_request", fake_get_tenant)
    req = types.SimpleNamespace(base_url="https://usemeridian.us/")
    resp = asyncio.run(tn._authorize_tunnel_proxy_caller("tid-1", req))
    assert resp is not None
    assert resp.status_code == 401
    assert resp.headers.get("www-authenticate") is not None


# ---------------------------------------------------------------------------
# c7604ed7 -- close-code handling in the reconnect loops
# ---------------------------------------------------------------------------

import websockets.exceptions as _wse
import websockets.frames as _wsf


def _closed_exc(code: int, reason: str = "x"):
    return _wse.ConnectionClosedError(_wsf.Close(code, reason), None)


def test_close_code_of_reads_the_websockets_close_code():
    assert tc._close_code_of(_closed_exc(4401)) == 4401
    assert tc._close_code_of(_closed_exc(4403)) == 4403
    assert tc._close_code_of(_closed_exc(4000)) == 4000
    assert tc._close_code_of(_wse.ConnectionClosedError(None, None)) is None
    assert tc._close_code_of(RuntimeError("dropped")) is None


@pytest.mark.parametrize("code", [4401, 4403])
def test_reconnect_loop_lazy_raises_and_invalidates_token_on_auth_rejected_close(monkeypatch, code):
    async def fake_run_connection_lazy(ws_url, proxy, label, tool_prefix=None, known_repo_paths=None):
        raise _closed_exc(code)

    invalidated = []
    monkeypatch.setattr(tc, "_run_connection_lazy", fake_run_connection_lazy)
    monkeypatch.setattr(tc, "_invalidate_cached_token", lambda base_url: invalidated.append(base_url))

    proxy = tc.SlotProxy(["x"], 8808, "fs")
    with pytest.raises(tc.TunnelAuthRejectedError):
        asyncio.run(tc._reconnect_loop_lazy("wss://x/tunnel/t", proxy, "fs", base_url="https://x"))
    assert invalidated == ["https://x"]


def test_reconnect_loop_lazy_auth_rejected_without_base_url_still_raises(monkeypatch):
    """base_url is optional -- omitting it must not swallow the rejection,
    just skip the cache-invalidation side effect."""
    async def fake_run_connection_lazy(ws_url, proxy, label, tool_prefix=None, known_repo_paths=None):
        raise _closed_exc(4401)

    def fail_if_called(base_url):
        raise AssertionError("must not be called without a base_url")

    monkeypatch.setattr(tc, "_run_connection_lazy", fake_run_connection_lazy)
    monkeypatch.setattr(tc, "_invalidate_cached_token", fail_if_called)

    proxy = tc.SlotProxy(["x"], 8808, "fs")
    with pytest.raises(tc.TunnelAuthRejectedError):
        asyncio.run(tc._reconnect_loop_lazy("wss://x/tunnel/t", proxy, "fs"))


def test_reconnect_loop_lazy_evicted_close_backs_off_instead_of_raising(monkeypatch):
    """4000 (evicted) must NOT raise -- usually a harmless same-host restart
    -- but must still climb backoff like any other real disconnect, and the
    loop must eventually be interruptible (no infinite retry that can never
    be cancelled)."""
    attempts = {"n": 0}

    async def fake_run_connection_lazy(ws_url, proxy, label, tool_prefix=None, known_repo_paths=None):
        attempts["n"] += 1
        if attempts["n"] <= 2:
            raise _closed_exc(4000)
        raise asyncio.CancelledError

    sleeps: list[float] = []

    async def fake_sleep(n):
        sleeps.append(n)

    monkeypatch.setattr(tc, "_run_connection_lazy", fake_run_connection_lazy)
    monkeypatch.setattr(tc.asyncio, "sleep", fake_sleep)

    proxy = tc.SlotProxy(["x"], 8808, "fs")
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(tc._reconnect_loop_lazy("wss://x/tunnel/t", proxy, "fs"))
    assert attempts["n"] == 3
    assert sleeps == [1.0, 2.0]  # normal exponential backoff, not reset/tight


def test_reconnect_loop_and_extract_pool_share_the_same_auth_rejected_handling(monkeypatch):
    """The other two reconnect loops (_reconnect_loop, used for the legacy
    non-lazy path, and _reconnect_loop_extract_pool, used for pooled Serena)
    must apply the identical close-code handling."""
    async def fake_run_connection(ws_url, port, label, tool_prefix=None):
        raise _closed_exc(4403)

    monkeypatch.setattr(tc, "_run_connection", fake_run_connection)
    with pytest.raises(tc.TunnelAuthRejectedError):
        asyncio.run(tc._reconnect_loop("wss://x", 8808, "fs"))

    async def fake_run_extract_pool_connection(ws_url, pool, repo_path, label, tool_prefix=None):
        raise _closed_exc(4401)

    monkeypatch.setattr(tc, "_run_extract_pool_connection", fake_run_extract_pool_connection)
    with pytest.raises(tc.TunnelAuthRejectedError):
        asyncio.run(tc._reconnect_loop_extract_pool("wss://x", object(), "/repo", "extract"))


# ---------------------------------------------------------------------------
# c7604ed7 -- cached tunnel-token lifetime alignment + invalidation
# ---------------------------------------------------------------------------

def test_write_cached_token_uses_90_day_expiry_matching_the_device_token(monkeypatch, tmp_path):
    cfg = tmp_path / "config.json"
    monkeypatch.setattr(tc, "_config_path", lambda: cfg)
    before = time.time()
    tc._write_cached_token("https://x", "sk_tok")
    data = json.loads(cfg.read_text(encoding="utf-8"))
    expires_at = data["tunnel_token"]["expires_at"]
    delta_days = (expires_at - before) / 86400.0
    assert 89.9 < delta_days < 90.1, f"expected ~90 day expiry, got {delta_days:.2f} days"


def test_invalidate_cached_token_removes_matching_entry(monkeypatch, tmp_path):
    cfg = tmp_path / "config.json"
    monkeypatch.setattr(tc, "_config_path", lambda: cfg)
    tc._write_cached_token("https://x", "sk_tok")
    assert tc._read_cached_token("https://x") == "sk_tok"

    tc._invalidate_cached_token("https://x")
    assert tc._read_cached_token("https://x") is None
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert "tunnel_token" not in data


def test_invalidate_cached_token_ignores_a_different_servers_cached_token(monkeypatch, tmp_path):
    cfg = tmp_path / "config.json"
    monkeypatch.setattr(tc, "_config_path", lambda: cfg)
    tc._write_cached_token("https://x", "sk_tok")

    tc._invalidate_cached_token("https://other-server")
    assert tc._read_cached_token("https://x") == "sk_tok"


def test_invalidate_cached_token_tolerates_missing_config_file(monkeypatch, tmp_path):
    monkeypatch.setattr(tc, "_config_path", lambda: tmp_path / "does-not-exist.json")
    tc._invalidate_cached_token("https://x")  # must not raise


# ---------------------------------------------------------------------------
# e37187f3 -- WS URL builders carry &host= (client side)
# ---------------------------------------------------------------------------

def test_ws_url_builders_include_host_param_when_hostname_available(monkeypatch):
    monkeypatch.setattr(tc, "_local_host_id", lambda: "my-machine")
    assert "&host=my-machine" in tc._ws_url("https://x", "t", "tok")
    assert "&host=my-machine" in tc._ws_code_url("https://x", "t", "tok")
    assert "&host=my-machine" in tc._ws_extract_url("https://x", "t", "tok")
    assert "&host=my-machine" in tc._ws_office_url("https://x", "t", "tok", "ppt")


def test_ws_url_builders_omit_host_param_when_hostname_unavailable(monkeypatch):
    monkeypatch.setattr(tc, "_local_host_id", lambda: "")
    assert "host=" not in tc._ws_url("https://x", "t", "tok")
    assert "host=" not in tc._ws_code_url("https://x", "t", "tok")
    assert "host=" not in tc._ws_extract_url("https://x", "t", "tok")
    assert "host=" not in tc._ws_office_url("https://x", "t", "tok", "ppt")


def test_local_host_id_tolerates_gethostname_failure(monkeypatch):
    import socket as _socket

    def boom():
        raise OSError("no hostname")

    monkeypatch.setattr(_socket, "gethostname", boom)
    assert tc._local_host_id() == ""


# ---------------------------------------------------------------------------
# e37187f3 -- per-host WS socket coexistence registry (routes/tunnel.py)
# ---------------------------------------------------------------------------

class _FakeWSHost:
    """Minimal stand-in exposing only .query_params, for _tunnel_ws_host_id."""

    def __init__(self, host=None):
        self.query_params = {"host": host} if host else {}


def test_tunnel_ws_host_id_reads_query_param_or_falls_back_to_unknown():
    assert tn._tunnel_ws_host_id(_FakeWSHost("laptop-a")) == "laptop-a"
    assert tn._tunnel_ws_host_id(_FakeWSHost(None)) == "unknown"


@pytest.fixture(autouse=True)
def _clean_multi_host_registry():
    tn._tunnel_sockets_by_host.clear()
    yield
    tn._tunnel_sockets_by_host.clear()


def test_register_multi_host_socket_different_hosts_coexist():
    """The core e37187f3 fix: two DIFFERENT hosts connecting for the same
    tenant+slot must NOT evict each other."""
    sockets: dict = {}
    ws_a, ws_b = object(), object()

    evicted_a = tn._register_tunnel_socket_multi_host("tid-1", "fs", "host-a", ws_a, sockets)
    assert evicted_a is None
    evicted_b = tn._register_tunnel_socket_multi_host("tid-1", "fs", "host-b", ws_b, sockets)
    assert evicted_b is None  # <-- host B must NOT evict host A

    # The single "active routing" pointer every existing caller reads just
    # follows whichever host connected most recently (unchanged semantics
    # for callers) -- but BOTH sockets are still tracked and BOTH stay open
    # (neither was ever told to close).
    assert sockets["tid-1"] is ws_b
    assert tn._tunnel_sockets_by_host[("tid-1", "fs")]["host-a"] is ws_a
    assert tn._tunnel_sockets_by_host[("tid-1", "fs")]["host-b"] is ws_b


def test_register_multi_host_socket_same_host_reconnect_still_evicts():
    """A reconnect from the SAME host (e.g. local binary restarted) still
    evicts its own PRIOR connection -- the original, legitimate behavior
    the pre-fix code comment described."""
    sockets: dict = {}
    ws_old, ws_new = object(), object()

    tn._register_tunnel_socket_multi_host("tid-1", "fs", "host-a", ws_old, sockets)
    evicted = tn._register_tunnel_socket_multi_host("tid-1", "fs", "host-a", ws_new, sockets)
    assert evicted is ws_old
    assert sockets["tid-1"] is ws_new
    assert tn._tunnel_sockets_by_host[("tid-1", "fs")]["host-a"] is ws_new


def test_register_multi_host_socket_scoped_per_slot_not_just_per_host():
    """_serve_tunnel_ws is ONE function shared by 7 different slots
    (ppt/word/dc/docs/zotero/outputs/debug); the SAME host legitimately
    holds one live connection per slot at once. Neither must be mistaken
    for a same-host reconnect on the OTHER slot."""
    ppt_sockets: dict = {}
    word_sockets: dict = {}
    ws_ppt, ws_word = object(), object()

    evicted_ppt = tn._register_tunnel_socket_multi_host("tid-1", "ppt", "host-a", ws_ppt, ppt_sockets)
    evicted_word = tn._register_tunnel_socket_multi_host("tid-1", "word", "host-a", ws_word, word_sockets)
    assert evicted_ppt is None
    assert evicted_word is None
    assert ppt_sockets["tid-1"] is ws_ppt
    assert word_sockets["tid-1"] is ws_word


def test_unregister_multi_host_socket_preserves_a_different_hosts_active_pointer():
    """When host A finally disconnects AFTER host B has taken over as the
    active routing target, A's cleanup must not clobber B's pointer."""
    sockets: dict = {}
    ws_a, ws_b = object(), object()
    tn._register_tunnel_socket_multi_host("tid-1", "fs", "host-a", ws_a, sockets)
    tn._register_tunnel_socket_multi_host("tid-1", "fs", "host-b", ws_b, sockets)
    assert sockets["tid-1"] is ws_b

    tn._unregister_tunnel_socket_multi_host("tid-1", "fs", "host-a", ws_a, sockets)
    assert sockets["tid-1"] is ws_b  # <-- still B, not cleared
    assert "host-a" not in tn._tunnel_sockets_by_host.get(("tid-1", "fs"), {})
    assert tn._tunnel_sockets_by_host[("tid-1", "fs")]["host-b"] is ws_b


def test_unregister_multi_host_socket_clears_active_pointer_when_it_was_the_last_one():
    sockets: dict = {}
    ws_a = object()
    tn._register_tunnel_socket_multi_host("tid-1", "fs", "host-a", ws_a, sockets)
    tn._unregister_tunnel_socket_multi_host("tid-1", "fs", "host-a", ws_a, sockets)
    assert "tid-1" not in sockets
    assert ("tid-1", "fs") not in tn._tunnel_sockets_by_host


# ---------------------------------------------------------------------------
# e37187f3 -- hostname sanitization (server.py)
# ---------------------------------------------------------------------------

def test_sanitize_tunnel_hostname_strips_unsafe_characters():
    assert srv._sanitize_tunnel_hostname("laptop-A.local_1") == "laptop-A.local_1"
    assert srv._sanitize_tunnel_hostname("a b/c'd\"e") == "abcde"
    assert srv._sanitize_tunnel_hostname("") == ""
    assert srv._sanitize_tunnel_hostname(None) == ""


def test_sanitize_tunnel_hostname_caps_length():
    assert len(srv._sanitize_tunnel_hostname("a" * 500)) == 200


# ---------------------------------------------------------------------------
# e37187f3 -- per-host tunnel-cli tokens (server.py tunnel_connect_authorize)
# ---------------------------------------------------------------------------

def _fake_tunnel_connect_request(db, body):
    async def _json():
        return body
    return types.SimpleNamespace(app=types.SimpleNamespace(state=types.SimpleNamespace(db=db)), json=_json)


def test_tunnel_connect_authorize_scopes_token_per_host(monkeypatch):
    """Two different hosts installing for the SAME tenant must both end up
    with a valid, independent tunnel-cli token -- neither install revokes
    the other's (the core reported bug)."""
    async def _run():
        db = await db_module.init_db(":memory:")
        tenant = await db_module.upsert_tenant(db, "a@x.com")
        monkeypatch.setattr(srv, "_hosted_mode", lambda: True)
        monkeypatch.setattr(
            srv, "_get_authenticated_tenant",
            AsyncMock(return_value=tenant),
        )
        srv._tunnel_device_codes.clear()

        r1 = await srv.tunnel_connect_authorize(
            _fake_tunnel_connect_request(db, {"device_code": "dc-1", "hostname": "laptop-a"})
        )
        r2 = await srv.tunnel_connect_authorize(
            _fake_tunnel_connect_request(db, {"device_code": "dc-2", "hostname": "desktop-b"})
        )
        tokens = await db_module.list_api_tokens(db, tenant["id"])
        return r1, r2, tokens

    r1, r2, tokens = asyncio.run(_run())
    assert r1 == {"status": "ok"}
    assert r2 == {"status": "ok"}
    labels = {t["label"] for t in tokens}
    assert labels == {"tunnel-cli:laptop-a", "tunnel-cli:desktop-b"}
    assert len(tokens) == 2, "both machines' tokens must survive"


def test_tunnel_connect_authorize_same_host_reinstall_overwrites_only_its_own_token(monkeypatch):
    """Re-running the installer on ONE machine is still a legitimate
    single-slot overwrite -- must not accumulate stale tokens."""
    async def _run():
        db = await db_module.init_db(":memory:")
        tenant = await db_module.upsert_tenant(db, "a@x.com")
        monkeypatch.setattr(srv, "_hosted_mode", lambda: True)
        monkeypatch.setattr(
            srv, "_get_authenticated_tenant",
            AsyncMock(return_value=tenant),
        )
        srv._tunnel_device_codes.clear()

        await srv.tunnel_connect_authorize(
            _fake_tunnel_connect_request(db, {"device_code": "dc-1", "hostname": "laptop-a"})
        )
        await srv.tunnel_connect_authorize(
            _fake_tunnel_connect_request(db, {"device_code": "dc-2", "hostname": "laptop-a"})
        )
        return await db_module.list_api_tokens(db, tenant["id"])

    tokens = asyncio.run(_run())
    assert len(tokens) == 1
    assert tokens[0]["label"] == "tunnel-cli:laptop-a"


def test_tunnel_connect_authorize_without_hostname_falls_back_to_legacy_shared_label(monkeypatch):
    """An older client that never sends a hostname keeps the exact pre-fix
    shared-label behavior -- no crash, no forced-upgrade regression."""
    async def _run():
        db = await db_module.init_db(":memory:")
        tenant = await db_module.upsert_tenant(db, "a@x.com")
        monkeypatch.setattr(srv, "_hosted_mode", lambda: True)
        monkeypatch.setattr(
            srv, "_get_authenticated_tenant",
            AsyncMock(return_value=tenant),
        )
        srv._tunnel_device_codes.clear()

        await srv.tunnel_connect_authorize(_fake_tunnel_connect_request(db, {"device_code": "dc-1"}))
        return await db_module.list_api_tokens(db, tenant["id"])

    tokens = asyncio.run(_run())
    assert len(tokens) == 1
    assert tokens[0]["label"] == "tunnel-cli"


# ---------------------------------------------------------------------------
# e37187f3 -- device_codes pruning (routes/oauth.py)
# ---------------------------------------------------------------------------

def _fmt(dt) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def test_prune_expired_device_codes_removes_only_expired_rows():
    async def _run():
        from datetime import datetime, timezone, timedelta
        db = await db_module.init_db(":memory:")
        past = _fmt(datetime.now(timezone.utc) - timedelta(minutes=5))
        future = _fmt(datetime.now(timezone.utc) + timedelta(minutes=5))
        await db.execute(
            "INSERT INTO device_codes (device_code, user_code, expires_at) VALUES (?, ?, ?)",
            ("expired-hash", "USER-EXP1", past),
        )
        await db.execute(
            "INSERT INTO device_codes (device_code, user_code, expires_at) VALUES (?, ?, ?)",
            ("live-hash", "USER-LIVE", future),
        )
        await db.commit()
        deleted = await oa._prune_expired_device_codes(db)
        async with db.execute("SELECT device_code FROM device_codes") as cur:
            rows = await cur.fetchall()
        remaining = [r["device_code"] if hasattr(r, "keys") else r[0] for r in rows]
        return deleted, remaining

    deleted, remaining = asyncio.run(_run())
    assert deleted == 1
    assert remaining == ["live-hash"]


def test_oauth_device_endpoint_prunes_abandoned_rows_on_every_mint():
    """A device_code that was minted and simply abandoned (never polled
    again) is never deleted by any OTHER code path -- only the mint
    endpoint's own sweep catches it."""
    async def _run():
        from datetime import datetime, timezone, timedelta
        db = await db_module.init_db(":memory:")
        past = _fmt(datetime.now(timezone.utc) - timedelta(minutes=5))
        await db.execute(
            "INSERT INTO device_codes (device_code, user_code, expires_at) VALUES (?, ?, ?)",
            ("abandoned-hash", "USER-OLD1", past),
        )
        await db.commit()
        req = types.SimpleNamespace(
            app=types.SimpleNamespace(state=types.SimpleNamespace(db=db)),
            base_url="https://x/",
        )
        resp = await oa._oauth_device(req)
        async with db.execute(
            "SELECT COUNT(*) AS c FROM device_codes WHERE device_code = ?", ("abandoned-hash",)
        ) as cur:
            row = await cur.fetchone()
        remaining = row["c"] if hasattr(row, "keys") else row[0]
        return resp, remaining

    resp, remaining = asyncio.run(_run())
    assert resp.status_code == 200
    assert remaining == 0
