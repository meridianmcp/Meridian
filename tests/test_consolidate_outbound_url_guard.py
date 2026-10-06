"""Outbound-URL guard for ``POST /projects/{id}/decisions/consolidate``.

The route accepts a caller-supplied ``base_url`` and ``api_key`` and posts to
that host.  These tests pin the SECURE behaviour:

* hosted mode: only an authenticated tenant may reach the route (a forged
  ``meridian_demo`` cookie must not), and the target must be https, free of
  credentials and resolve ONLY to public addresses;
* local / self-hosted mode: a loopback target (a local model server) keeps
  working, link-local / metadata / unspecified / multicast / reserved targets
  are refused, plain http is only for loopback, and a private LAN target needs
  an explicit opt-in;
* everywhere: redirects are never followed, env proxies are ignored in hosted
  mode, and upstream response bodies / exception text are never reflected.

Everything is local and synthetic: throwaway loopback HTTP servers, inert
canary strings, a patched ``socket.getaddrinfo`` that never touches real DNS,
and a fake ``httpx.AsyncClient`` wherever a real connection is not needed.
"""
from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import re
import socket
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import httpx
import pytest

from meridian import _deps
from meridian import db as db_module

CANARY = "CANARY-inert-upstream-7f3a"
PUBLIC_HOST = "api.public-llm.test"
_OK_DECISIONS = [{"title": "Merged", "category": "TECHNICAL", "body": "m"}]


# ---------------------------------------------------------------------------
# Helpers: DNS, fake httpx client, throwaway loopback upstream
# ---------------------------------------------------------------------------

_LEGACY_NUMERIC = re.compile(r"^[0-9a-fx.]+$")


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch):
    """No test in this module may reach real DNS.  Names in ``table`` resolve to
    the listed addresses; IP literals and legacy numeric forms go to the real
    (non-network) parser; anything else fails to resolve."""
    table: dict[str, list[str]] = {
        "localhost": ["127.0.0.1", "::1"],
        PUBLIC_HOST: ["8.8.8.8"],
    }
    real = socket.getaddrinfo

    def _gai(host, port, family=0, type=0, proto=0, flags=0):
        if isinstance(host, bytes):
            host = host.decode()
        key = (host or "").lower().rstrip(".")
        if key in table:
            out = []
            for ip in table[key]:
                if ":" in ip:
                    out.append((socket.AF_INET6, socket.SOCK_STREAM, 6, "", (ip, port or 0, 0, 0)))
                else:
                    out.append((socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port or 0)))
            return out
        if _is_ip_literal(key) or _LEGACY_NUMERIC.match(key):
            return real(host, port, family, type, proto, flags)
        raise socket.gaierror(socket.EAI_NONAME, "synthetic: no DNS in tests")

    monkeypatch.setattr(socket, "getaddrinfo", _gai)
    return table


def _install_fake_httpx(monkeypatch, *, raises=None, payload=None):
    """Replace ``httpx.AsyncClient``; record constructor kwargs and POST urls."""
    rec = SimpleNamespace(calls=[], client_kwargs=[])
    ok_payload = payload if payload is not None else {
        "choices": [{"message": {"content": json.dumps({"decisions": _OK_DECISIONS})}}]
    }

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return ok_payload

    class _Client:
        def __init__(self, *a, **k):
            rec.client_kwargs.append(k)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **k):
            rec.calls.append(url)
            if raises is not None:
                raise raises
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    return rec


@contextlib.contextmanager
def _upstream(*, status=200, body=b"", headers=None):
    """Loopback-only HTTP server that records every request it receives."""
    hits: list[tuple[str, str]] = []

    class _Handler(BaseHTTPRequestHandler):
        def _serve(self):
            length = int(self.headers.get("content-length") or 0)
            if length:
                self.rfile.read(length)
            hits.append((self.command, self.path))
            self.send_response(status)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = _serve

        def log_message(self, *a):  # keep test output quiet
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}", hits
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)


def _openai_ok_body(decisions=None) -> bytes:
    inner = json.dumps({"decisions": decisions if decisions is not None else _OK_DECISIONS})
    return json.dumps({"choices": [{"message": {"content": inner}}]}).encode()


def _post(c, project_id, *, base_url=None, model="gpt-4o-mini",
          api_key="sk-test-canary", headers=None, **extra):
    body = {"api_key": api_key, "model": model}
    if base_url is not None:
        body["base_url"] = base_url
    body.update(extra)
    return c.post(f"/projects/{project_id}/decisions/consolidate",
                  json=body, headers=headers or {})


# ---------------------------------------------------------------------------
# Fixtures: local project, hosted tenant, demo DB
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _no_env_proxy(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.delenv("MERIDIAN_OUTBOUND_ALLOW_PRIVATE", raising=False)


@pytest.fixture
def local_project(client):
    """Self-hosted (MERIDIAN_HOSTED unset) client plus a project with decisions."""
    r = client.post("/projects", json={"name": f"cons-{uuid.uuid4().hex[:6]}"})
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    for i in range(2):
        r = client.post(f"/projects/{pid}/decisions-pinned",
                        json={"title": f"D{i}", "body": f"b{i}"})
        assert r.status_code == 201, r.text
    return client, pid


@pytest.fixture
def hosted(client, monkeypatch):
    """The same app switched to hosted mode (the gate and ``_db`` read the
    environment on every request)."""
    monkeypatch.setenv("MERIDIAN_HOSTED", "1")
    _deps._reset_limiter_counts()
    yield client
    _deps._reset_limiter_counts()


@pytest.fixture
def tenant(hosted):
    """An authenticated hosted tenant with its own DB and one pinned decision."""
    c = hosted
    auth_db = c.app.state.db
    t = asyncio.run(db_module.upsert_tenant(auth_db, f"t-{uuid.uuid4().hex[:8]}@example.com"))
    asyncio.run(db_module.update_tenant(auth_db, t["id"], plan="pro"))
    conn = asyncio.run(db_module.init_db(":memory:"))
    proj = asyncio.run(db_module.create_project(conn, "own-proj"))
    asyncio.run(db_module.pin_decision(conn, proj["id"], "Alpha", "alpha body", "TECHNICAL"))
    raw, _row = asyncio.run(db_module.create_api_token(auth_db, t["id"], label="t"))
    _deps._tenant_db_cache[t["id"]] = conn
    try:
        yield c, proj["id"], {"Authorization": f"Bearer {raw}"}
    finally:
        _deps._tenant_db_cache.pop(t["id"], None)
        asyncio.run(conn.close())


# ===========================================================================
# 1. Hosted: who may reach the route
# ===========================================================================

def test_hosted_anonymous_request_is_401(hosted, monkeypatch):
    rec = _install_fake_httpx(monkeypatch)
    r = _post(hosted, "00000000-0000-4000-8000-000000000000", base_url=f"https://{PUBLIC_HOST}")
    assert r.status_code == 401
    assert rec.calls == []


def test_hosted_forged_demo_cookie_cannot_trigger_outbound_call(hosted, monkeypatch):
    """The demo cookie is an unsigned '1' anyone can set.  It reaches the demo DB
    but must never be enough to make the server issue an outbound request."""
    demo = asyncio.run(db_module.init_db(":memory:"))
    try:
        proj = asyncio.run(db_module.create_project(demo, "demo-proj"))
        asyncio.run(db_module.pin_decision(demo, proj["id"], "Demo", "demo body", "TECHNICAL"))
        monkeypatch.setattr(hosted.app.state, "demo_db", demo, raising=False)
        with _upstream(body=_openai_ok_body()) as (base, hits):
            r = _post(hosted, proj["id"], base_url=base, headers={"Cookie": "meridian_demo=1"})
        assert r.status_code in (401, 403), (r.status_code, r.text[:200])
        assert hits == []
    finally:
        asyncio.run(demo.close())


def test_hosted_handler_itself_requires_a_resolved_tenant(hosted, monkeypatch):
    """Defence in depth: call the handler directly (bypassing the app-wide gate and
    the demo read-only middleware) with a forged demo cookie.  It must refuse on
    its own instead of relying on those layers."""
    from fastapi import HTTPException
    from starlette.requests import Request

    from meridian.routes.decisions import consolidate_decisions_ai

    demo = asyncio.run(db_module.init_db(":memory:"))
    try:
        proj = asyncio.run(db_module.create_project(demo, "demo-proj"))
        asyncio.run(db_module.pin_decision(demo, proj["id"], "Demo", "demo body", "TECHNICAL"))
        monkeypatch.setattr(hosted.app.state, "demo_db", demo, raising=False)
        rec = _install_fake_httpx(monkeypatch)
        scope = {
            "type": "http", "method": "POST", "path": "/x", "query_string": b"",
            "headers": [(b"cookie", b"meridian_demo=1")], "app": hosted.app,
        }
        body = {"api_key": "sk-test", "model": "gpt-4o-mini",
                "base_url": f"https://{PUBLIC_HOST}"}
        with pytest.raises(HTTPException) as ei:
            asyncio.run(consolidate_decisions_ai(proj["id"], body, Request(scope)))
        assert ei.value.status_code == 401
        assert rec.calls == []
    finally:
        asyncio.run(demo.close())


def test_hosted_tenant_reaches_route_with_a_public_target(tenant, monkeypatch):
    c, pid, auth = tenant
    rec = _install_fake_httpx(monkeypatch)
    r = _post(c, pid, base_url=f"https://{PUBLIC_HOST}/", headers=auth)
    assert r.status_code == 200, r.text
    assert r.json()["consolidated"][0]["title"] == "Merged"
    assert rec.calls == [f"https://{PUBLIC_HOST}/v1/chat/completions"]


# ===========================================================================
# 2. Hosted: the outbound target
# ===========================================================================

def test_hosted_loopback_target_is_never_contacted(tenant):
    """Reproduces the core issue with a real loopback server: in hosted mode the
    server must not connect to a caller-supplied loopback address."""
    c, pid, auth = tenant
    with _upstream(status=500, body=CANARY.encode()) as (base, hits):
        r = _post(c, pid, base_url=base, headers=auth)
    assert hits == [], f"hosted server connected to a loopback target: {hits}"
    assert r.status_code == 400, (r.status_code, r.text[:200])
    assert CANARY not in r.text


HOSTED_REJECTED_URLS = [
    # loopback, in every spelling
    "http://127.0.0.1:8000", "https://127.0.0.1", "https://127.1", "https://localhost",
    "https://LOCALHOST.", "https://[::1]", "https://[::ffff:127.0.0.1]",
    "https://[::ffff:7f00:1]", "https://[::127.0.0.1]", "https://2130706433",
    "https://0x7f.0.0.1", "https://017700000001",
    # link-local / cloud metadata
    "https://169.254.169.254", "https://169.254.169.254/latest/meta-data",
    "https://[::ffff:169.254.169.254]", "https://[fe80::1]", "http://[fd00:ec2::254]",
    # private / shared / unspecified / multicast / reserved
    "https://10.0.0.5", "https://172.16.0.1", "https://192.168.1.1", "https://100.64.0.1",
    "https://[fc00::1]", "https://[fdaa::1]", "https://0.0.0.0", "https://[::]",
    "https://224.0.0.1", "https://[ff02::1]", "https://240.0.0.1", "https://255.255.255.255",
    # embedded-IPv4 transition forms
    "https://[64:ff9b::7f00:1]", "https://[2002:7f00:1::1]",
    # internal-looking names that never reach DNS
    "https://myapp.internal", "https://svc.flycast", "https://printer.local", "https://redis",
    # structural problems
    "https://user:pw@api.public-llm.test", "https://user@api.public-llm.test",
    "https://api.public-llm.test?x=1", "https://api.public-llm.test#frag",
    "https://api.public-llm.test\\@127.0.0.1", "https://api.public-llm.test/a b",
    "https://api.public-llm.test:0", "https://api.public-llm.test:99999", "https://[::1",
    "ftp://api.public-llm.test", "file:///etc/passwd", "gopher://api.public-llm.test",
    "//api.public-llm.test", "api.public-llm.test", "https://", "https://unresolvable.test",
]


@pytest.mark.parametrize("base_url", HOSTED_REJECTED_URLS)
def test_hosted_rejects_non_public_or_malformed_base_url(tenant, monkeypatch, base_url):
    c, pid, auth = tenant
    rec = _install_fake_httpx(monkeypatch)
    r = _post(c, pid, base_url=base_url, headers=auth)
    assert r.status_code == 400, (base_url, r.status_code, r.text[:200])
    assert rec.calls == [], f"outbound call attempted for {base_url!r}"


@pytest.mark.parametrize("addresses", [
    ["10.1.2.3"], ["127.0.0.1"], ["169.254.169.254"], ["fd00::5"], ["::ffff:10.0.0.1"],
    ["8.8.8.8", "10.0.0.1"],           # mixed answer: ANY non-public address refuses
    ["2001:4860:4860::8888", "::1"],
])
def test_hosted_rejects_a_name_that_resolves_to_a_non_public_address(
        tenant, monkeypatch, fake_dns, addresses):
    c, pid, auth = tenant
    fake_dns["rebind-or-internal.test"] = addresses
    rec = _install_fake_httpx(monkeypatch)
    r = _post(c, pid, base_url="https://rebind-or-internal.test", headers=auth)
    assert r.status_code == 400, (addresses, r.status_code, r.text[:200])
    assert rec.calls == []


def test_hosted_accepts_a_name_that_resolves_only_to_public_addresses(
        tenant, monkeypatch, fake_dns):
    c, pid, auth = tenant
    fake_dns["multi.public-llm.test"] = ["8.8.8.8", "2001:4860:4860::8888"]
    rec = _install_fake_httpx(monkeypatch)
    r = _post(c, pid, base_url="https://multi.public-llm.test/api", headers=auth)
    assert r.status_code == 200, r.text
    assert rec.calls == ["https://multi.public-llm.test/api/v1/chat/completions"]


def test_hosted_requires_https(tenant, monkeypatch):
    c, pid, auth = tenant
    rec = _install_fake_httpx(monkeypatch)
    r = _post(c, pid, base_url=f"http://{PUBLIC_HOST}", headers=auth)
    assert r.status_code == 400
    assert rec.calls == []


def test_hosted_ignores_the_private_network_opt_in(tenant, monkeypatch):
    """The LAN opt-in is a self-hosted convenience; hosted mode never honours it."""
    c, pid, auth = tenant
    monkeypatch.setenv("MERIDIAN_OUTBOUND_ALLOW_PRIVATE", "1")
    rec = _install_fake_httpx(monkeypatch)
    for target in ("https://10.0.0.5", "https://127.0.0.1", "https://192.168.1.1"):
        r = _post(c, pid, base_url=target, headers=auth)
        assert r.status_code == 400, (target, r.status_code)
    assert rec.calls == []


def test_hosted_client_never_follows_redirects_or_trusts_env_proxies(tenant, monkeypatch):
    c, pid, auth = tenant
    rec = _install_fake_httpx(monkeypatch)
    r = _post(c, pid, base_url=f"https://{PUBLIC_HOST}", headers=auth)
    assert r.status_code == 200, r.text
    kwargs = rec.client_kwargs[-1]
    assert kwargs.get("follow_redirects") is False
    assert kwargs.get("trust_env") is False


def test_hosted_upstream_error_body_is_not_reflected(tenant, monkeypatch):
    c, pid, auth = tenant
    err = httpx.HTTPStatusError(
        "boom", request=httpx.Request("POST", f"https://{PUBLIC_HOST}"),
        response=httpx.Response(500, text=CANARY),
    )
    _install_fake_httpx(monkeypatch, raises=err)
    r = _post(c, pid, base_url=f"https://{PUBLIC_HOST}", headers=auth)
    assert r.status_code == 502
    assert CANARY not in r.text


def test_hosted_connection_error_detail_is_generic(tenant, monkeypatch):
    c, pid, auth = tenant
    _install_fake_httpx(monkeypatch, raises=httpx.ConnectError(f"{CANARY} internal-host:6379"))
    r = _post(c, pid, base_url=f"https://{PUBLIC_HOST}", headers=auth)
    assert r.status_code == 502
    assert CANARY not in r.text
    assert "6379" not in r.text


# ===========================================================================
# 3. Local / self-hosted mode
# ===========================================================================

def test_local_loopback_model_server_still_works(local_project):
    c, pid = local_project
    with _upstream(body=_openai_ok_body(), headers={"content-type": "application/json"}) as (
            base, hits):
        r = _post(c, pid, base_url=base)
    assert r.status_code == 200, r.text
    assert r.json()["consolidated"][0]["title"] == "Merged"
    assert r.json()["original_count"] == 2
    assert hits == [("POST", "/v1/chat/completions")]


def test_local_upstream_error_body_is_not_reflected(local_project):
    c, pid = local_project
    with _upstream(status=500, body=CANARY.encode()) as (base, hits):
        r = _post(c, pid, base_url=base)
    assert hits, "the loopback target should have been contacted in local mode"
    assert r.status_code == 502
    assert CANARY not in r.text


def test_local_redirect_is_not_followed_and_its_body_not_reflected(local_project):
    c, pid = local_project
    with _upstream(body=_openai_ok_body()) as (target_base, target_hits):
        with _upstream(status=302, body=CANARY.encode(),
                       headers={"location": f"{target_base}/v1/chat/completions"}) as (
                base, hits):
            r = _post(c, pid, base_url=base)
    assert hits, "the first hop should have been contacted"
    assert target_hits == [], "a redirect target must never be contacted"
    assert r.status_code == 502
    assert CANARY not in r.text


def test_local_client_disables_redirects(local_project, monkeypatch):
    c, pid = local_project
    rec = _install_fake_httpx(monkeypatch)
    r = _post(c, pid, base_url="http://127.0.0.1:11434")
    assert r.status_code == 200, r.text
    assert rec.client_kwargs[-1].get("follow_redirects") is False


@pytest.mark.parametrize("base_url", [
    "http://169.254.169.254", "http://169.254.169.254/latest/meta-data",
    "http://[::ffff:169.254.169.254]", "https://[fe80::1]", "http://[fd00:ec2::254]",
    "http://0.0.0.0:8080", "http://[::]", "http://224.0.0.1", "http://240.0.0.1",
    "https://user:pw@localhost", "https://localhost?x=1", "ftp://localhost",
    "http://localhost:99999",
])
def test_local_refuses_metadata_unspecified_multicast_reserved_and_malformed(
        local_project, monkeypatch, base_url):
    c, pid = local_project
    rec = _install_fake_httpx(monkeypatch)
    r = _post(c, pid, base_url=base_url)
    assert r.status_code == 400, (base_url, r.status_code, r.text[:200])
    assert rec.calls == []


@pytest.mark.parametrize("base_url", [
    "http://127.0.0.1:11434", "http://localhost:1234", "https://localhost:8443",
    "http://[::1]:8080", "http://[::ffff:127.0.0.1]:8080", f"https://{PUBLIC_HOST}",
])
def test_local_allows_loopback_and_public_https_targets(local_project, monkeypatch, base_url):
    c, pid = local_project
    rec = _install_fake_httpx(monkeypatch)
    r = _post(c, pid, base_url=base_url)
    assert r.status_code == 200, (base_url, r.status_code, r.text[:200])
    assert len(rec.calls) == 1


def test_local_plain_http_to_a_public_host_is_refused(local_project, monkeypatch):
    c, pid = local_project
    rec = _install_fake_httpx(monkeypatch)
    r = _post(c, pid, base_url=f"http://{PUBLIC_HOST}")
    assert r.status_code == 400
    assert rec.calls == []


def test_local_private_lan_target_needs_an_explicit_opt_in(local_project, monkeypatch):
    c, pid = local_project
    rec = _install_fake_httpx(monkeypatch)
    for target in ("http://192.168.1.50:11434", "http://10.0.0.7:8000", "http://[fd00::5]:8000"):
        r = _post(c, pid, base_url=target)
        assert r.status_code == 400, (target, r.status_code)
    assert rec.calls == []
    monkeypatch.setenv("MERIDIAN_OUTBOUND_ALLOW_PRIVATE", "1")
    r = _post(c, pid, base_url="http://192.168.1.50:11434")
    assert r.status_code == 200, r.text
    assert rec.calls == ["http://192.168.1.50:11434/v1/chat/completions"]
    # the opt-in widens private ranges only, never link-local / metadata
    r = _post(c, pid, base_url="http://169.254.169.254")
    assert r.status_code == 400


def test_local_name_resolving_to_metadata_address_is_refused(
        local_project, monkeypatch, fake_dns):
    c, pid = local_project
    fake_dns["sneaky.test"] = ["169.254.169.254"]
    rec = _install_fake_httpx(monkeypatch)
    r = _post(c, pid, base_url="https://sneaky.test")
    assert r.status_code == 400
    assert rec.calls == []


# ===========================================================================
# 4. Mode-independent behaviour
# ===========================================================================

def test_claude_models_ignore_base_url(local_project, monkeypatch):
    c, pid = local_project
    rec = _install_fake_httpx(monkeypatch, payload={"content": [
        {"text": json.dumps({"decisions": _OK_DECISIONS})}]})
    r = _post(c, pid, model="claude-haiku-4-5-20251001", base_url="http://169.254.169.254")
    assert r.status_code == 200, r.text
    assert rec.calls == ["https://api.anthropic.com/v1/messages"]


@pytest.mark.parametrize("extra", [
    {"base_url": 123}, {"base_url": ["https://x"]}, {"api_key": "sk\r\nX-Evil: 1"},
    {"api_key": {"a": 1}}, {"model": 5},
])
def test_non_string_or_header_unsafe_inputs_are_rejected(local_project, monkeypatch, extra):
    c, pid = local_project
    rec = _install_fake_httpx(monkeypatch)
    body = {"api_key": "sk-test", "model": "gpt-4o-mini"}
    body.update(extra)
    r = c.post(f"/projects/{pid}/decisions/consolidate", json=body)
    assert r.status_code == 400, (extra, r.status_code, r.text[:200])
    assert rec.calls == []


def test_unknown_project_is_404(client, monkeypatch):
    rec = _install_fake_httpx(monkeypatch)
    r = _post(client, "00000000-0000-4000-8000-000000000000", base_url="http://127.0.0.1:11434")
    assert r.status_code == 404
    assert rec.calls == []


def test_consolidated_output_keeps_only_the_expected_fields(local_project, monkeypatch):
    c, pid = local_project
    leaked = {"title": "T", "category": "TECHNICAL", "body": "B", "secret": CANARY}
    _install_fake_httpx(monkeypatch, payload={"choices": [{"message": {
        "content": json.dumps({"decisions": [leaked, "not-a-dict"]})}}]})
    r = _post(c, pid, base_url="http://127.0.0.1:11434")
    assert r.status_code == 200, r.text
    assert r.json()["consolidated"] == [{"title": "T", "category": "TECHNICAL", "body": "B"}]
    assert CANARY not in r.text


def test_malformed_upstream_shape_is_a_generic_502(local_project, monkeypatch):
    c, pid = local_project
    _install_fake_httpx(monkeypatch, payload={"choices": [{"message": {
        "content": json.dumps({"decisions": {"x": CANARY}})}}]})
    r = _post(c, pid, base_url="http://127.0.0.1:11434")
    assert r.status_code == 502
    assert CANARY not in r.text


# ===========================================================================
# 5. The reusable validator, directly
# ===========================================================================

def _validator():
    from meridian import outbound_url_guard as g  # noqa: PLC0415
    return g


@pytest.mark.parametrize("url", HOSTED_REJECTED_URLS)
def test_validator_hosted_rejects(url):
    g = _validator()
    with pytest.raises(g.OutboundURLRejected):
        g.validate_outbound_url(url, hosted=True)


def test_validator_hosted_accepts_public_https_and_normalises():
    g = _validator()
    assert g.validate_outbound_url(f"HTTPS://{PUBLIC_HOST}/api/", hosted=True) == \
        f"https://{PUBLIC_HOST}/api"
    assert g.validate_outbound_url("https://8.8.8.8:8443", hosted=True) == "https://8.8.8.8:8443"
    assert g.validate_outbound_url("https://[2001:4860:4860::8888]/v1", hosted=True) == \
        "https://[2001:4860:4860::8888]/v1"


def test_validator_error_messages_do_not_echo_the_url_or_resolution(fake_dns):
    g = _validator()
    fake_dns["leaky.test"] = ["10.9.8.7"]
    for url in ("https://leaky.test/secret-path", "https://unresolvable.test/secret-path"):
        with pytest.raises(g.OutboundURLRejected) as ei:
            g.validate_outbound_url(url, hosted=True)
        msg = str(ei.value)
        assert "10.9.8.7" not in msg and "secret-path" not in msg and "leaky" not in msg
    # unresolvable and non-public must be indistinguishable (no DNS oracle)
    msgs = set()
    for url in ("https://leaky.test", "https://unresolvable.test"):
        with pytest.raises(g.OutboundURLRejected) as ei:
            g.validate_outbound_url(url, hosted=True)
        msgs.add(str(ei.value))
    assert len(msgs) == 1


def test_validator_local_policy(monkeypatch):
    g = _validator()
    assert g.validate_outbound_url("http://localhost:11434/", hosted=False) == \
        "http://localhost:11434"
    assert g.validate_outbound_url("http://[::1]:8000", hosted=False) == "http://[::1]:8000"
    for bad in ("http://169.254.169.254", "http://0.0.0.0", "http://10.0.0.1",
                f"http://{PUBLIC_HOST}", "https://u:p@localhost"):
        with pytest.raises(g.OutboundURLRejected):
            g.validate_outbound_url(bad, hosted=False)
    monkeypatch.setenv("MERIDIAN_OUTBOUND_ALLOW_PRIVATE", "1")
    assert g.validate_outbound_url("http://10.0.0.1:8000", hosted=False) == "http://10.0.0.1:8000"
    with pytest.raises(g.OutboundURLRejected):
        g.validate_outbound_url("http://169.254.169.254", hosted=False)
    with pytest.raises(g.OutboundURLRejected):
        g.validate_outbound_url("http://10.0.0.1:8000", hosted=True)


@pytest.mark.parametrize("hosted", [True, False])
@pytest.mark.parametrize("url", [
    "https://-bad.test", "https://exa$mple.test", "https://a..test",
    "https://[2001:4860:4860::8888%25eth0]", "https://127.0.0.1%2e", "https://[fe80::1%25eth0]",
])
def test_validator_rejects_invalid_hostnames_and_zone_ids(url, hosted):
    g = _validator()
    with pytest.raises(g.OutboundURLRejected):
        g.validate_outbound_url(url, hosted=hosted)


@pytest.mark.parametrize("answer", [
    [],                                                       # empty answer
    [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("not-an-ip", 443))],   # garbage sockaddr
])
def test_validator_fails_closed_on_odd_resolver_answers(monkeypatch, answer):
    g = _validator()
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: answer)
    for hosted in (True, False):
        with pytest.raises(g.OutboundURLRejected):
            g.validate_outbound_url("https://odd-answer.test", hosted=hosted)


def test_validator_scope_suffix_on_resolved_address_is_ignored(monkeypatch):
    g = _validator()
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [
        (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2001:4860:4860::8888%eth0", 443, 0, 0))])
    assert g.validate_outbound_url("https://scoped.test", hosted=True) == "https://scoped.test"


def test_avalidate_outbound_url_runs_the_same_policy():
    g = _validator()
    assert asyncio.run(g.avalidate_outbound_url(f"https://{PUBLIC_HOST}/x/", hosted=True)) == \
        f"https://{PUBLIC_HOST}/x"
    with pytest.raises(g.OutboundURLRejected):
        asyncio.run(g.avalidate_outbound_url("https://127.0.0.1", hosted=True))


def test_validator_rejects_overlong_and_control_characters():
    g = _validator()
    for bad in ("https://" + "a" * 3000 + ".test", "https://api.public-llm.test/\x00",
                "https://api.public-llm.test/\n", "https://api.public-llm.test/\t"):
        with pytest.raises(g.OutboundURLRejected):
            g.validate_outbound_url(bad, hosted=True)
    with pytest.raises(g.OutboundURLRejected):
        g.validate_outbound_url(None, hosted=True)  # type: ignore[arg-type]
