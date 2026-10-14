"""Tests for meridian.zotero_connect: the user-initiated Zotero connect flow.

Every test uses fake transports and an in-memory approval store. The real
transport, the real probe opener and the real OS keyring are replaced by guards
that record any use, and the fixture fails the test at teardown if one was reached.
No test touches Zotero, the network or a credential store.
"""

from __future__ import annotations

import http.client
import http.server
import io
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from meridian import zotero_connect as zc
from meridian import zotero_filer as zf
from meridian.routes import zotero as zotero_route
from meridian.tunnel_config import ZoteroCredentialStoreUnavailable

# Fake approval key. It exists only inside these tests.
ISSUED_KEY = "FAKE-APPROVAL-KEY-FOR-TESTS-0001"

AUTHORIZE = "http://127.0.0.1:23119/api/local/authorize"
PROBE = "http://127.0.0.1:23119/api/users/0/items?limit=1"
SAVE_URL = "http://127.0.0.1:23119/connector/saveItems"

COLLECTIONS = zf.LOCAL_API + "/collections"
ITEMS = zf.LOCAL_API + "/collections/ABCD1234/items"
SELECTED = zf.CONNECTOR + "/getSelectedCollection"
SAVE = zf.CONNECTOR + "/saveItems"

BIB = (
    "@article{one2020,\n"
    "  author = {Lin, Ada},\n"
    "  title = {Paper One},\n"
    "  journal = {Journal of Tests},\n"
    "  year = {2020},\n"
    "  doi = {10.1000/one},\n"
    "}\n"
)


class MemoryStore:
    """In-memory stand-in for the OS keyring approval store."""

    def __init__(self, key=None):
        self.key = key
        self.set_calls = 0
        self.delete_calls = 0
        self.raise_on_set = None

    def get(self):
        return self.key

    def set(self, key):
        if self.raise_on_set is not None:
            raise self.raise_on_set
        self.key = key
        self.set_calls += 1

    def delete(self):
        self.delete_calls += 1
        had = self.key is not None
        self.key = None
        return had


class FakeClock:
    """Clock that moves only when a test moves it."""

    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class Scripted:
    """Fake transport. Returns or raises the next scripted outcome and records each call."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def __call__(self, method, url, body, headers):
        self.calls.append({"method": method, "url": url, "body": body, "headers": dict(headers)})
        if not self.outcomes:
            raise AssertionError(f"unexpected request: {method} {url}")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class FakeResponse:
    """Stand-in for a urllib response from the local API probe."""

    def __init__(self, status=200, headers=None, body=b"[]"):
        self.status = status
        self.headers = headers or {}
        self._body = body
        self.closed = False

    def read(self):
        return self._body

    def close(self):
        self.closed = True


def probe_opener(*outcomes):
    """Fake probe opener. Records each Request it receives."""
    calls = []

    def opener(request, timeout):
        calls.append(request)
        outcome = outcomes[len(calls) - 1]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    opener.calls = calls
    return opener


class Routed:
    """Route-table transport for the filer flow. Handlers return (status, payload)."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, method, url, body, headers):
        self.calls.append({"method": method, "url": url, "body": body, "headers": dict(headers)})
        handler = self.routes.get((method, url.split("?", 1)[0]))
        if handler is None:
            raise AssertionError(f"unexpected request: {method} {url}")
        status, payload = handler(url, body) if callable(handler) else handler
        return status, json.dumps(payload)

    def posts(self):
        return [call for call in self.calls if call["method"] == "POST"]


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Isolate every test: in-memory approval store, tmp state file, guarded network."""
    monkeypatch.delenv("MERIDIAN_ZOTERO_API_URL", raising=False)
    store = MemoryStore()
    state = SimpleNamespace(
        store=store,
        state_path=tmp_path / "zotero-connect.json",
        default_calls=[],
        probe_calls=[],
    )
    monkeypatch.setattr(zc, "KeyringApprovalStore", lambda: store)
    monkeypatch.setattr(zc, "_state_path", lambda: state.state_path)
    monkeypatch.setattr(zc, "_IN_PROCESS_LAST_ATTEMPT", None)

    def guarded_transport(timeout):
        def send(method, url, body, headers):
            state.default_calls.append((method, url))
            raise ConnectionRefusedError("no real transport in tests")
        return send

    class GuardedOpener:
        def open(self, request, timeout):
            state.probe_calls.append(request)
            raise ConnectionRefusedError("no real probe in tests")

    monkeypatch.setattr(zc, "_default_transport", guarded_transport)
    monkeypatch.setattr(zc, "_PROBE_OPENER", GuardedOpener())
    yield state
    assert state.default_calls == [], "the real transport was reached"
    assert state.probe_calls == [], "the real probe opener was reached"


@pytest.fixture
def bib_file(tmp_path):
    path = tmp_path / "refs.bib"
    path.write_text(BIB, encoding="utf-8")
    return str(path)


def _write_request(payload=None):
    return {"method": "POST", "url": SAVE_URL, "payload": {"items": [] if payload is None else payload}}


# ---------------------------------------------------------------------------
# status(): read-only, never POSTs
# ---------------------------------------------------------------------------

def test_status_is_a_plain_get_and_reports_not_connected(env):
    opener = probe_opener(FakeResponse(200, {"Zotero-Server-ID": "srv-1"}, b"[]"))
    result = zc.status(opener=opener)
    assert result["status"] == "not_connected"
    assert len(opener.calls) == 1
    request = opener.calls[0]
    assert isinstance(request, urllib.request.Request)
    assert request.get_method() == "GET"
    assert request.data is None
    assert request.full_url == PROBE


def test_status_reports_connected_when_an_approval_is_stored(env):
    env.store.key = ISSUED_KEY
    opener = probe_opener(FakeResponse(200, {"Zotero-Server-ID": "srv-1"}, b"[]"))
    assert zc.status(opener=opener)["status"] == "connected"


@pytest.mark.parametrize(
    "outcome, expected",
    [
        (ConnectionRefusedError("refused"), "unreachable"),
        (urllib.error.URLError("refused"), "unreachable"),
        (urllib.error.HTTPError(PROBE, 403, "Forbidden", {}, None), "local_api_disabled"),
        (FakeResponse(200, {}, b"[]"), "unreachable"),
        (FakeResponse(500, {"Zotero-Server-ID": "srv-1"}, b""), "unreachable"),
    ],
)
def test_status_classifies_the_local_api(env, outcome, expected):
    assert zc.status(opener=probe_opener(outcome))["status"] == expected


# ---------------------------------------------------------------------------
# connect(): the only POST to the authorize endpoint
# ---------------------------------------------------------------------------

def test_connect_posts_exactly_once_to_the_authorize_endpoint(env):
    transport = Scripted((200, json.dumps({"key": ISSUED_KEY})))
    zc.connect(transport=transport)
    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == AUTHORIZE
    assert json.loads(call["body"]) == {"appName": "Meridian"}


def test_connect_approved_stores_the_key_and_reports_connected(env):
    result = zc.connect(transport=Scripted((200, json.dumps({"key": ISSUED_KEY}))))
    assert result["status"] == "connected"
    assert env.store.key == ISSUED_KEY
    assert env.store.set_calls == 1
    assert ISSUED_KEY not in json.dumps(result)


@pytest.mark.parametrize(
    "outcome, expected",
    [
        ((403, "denied by user"), "denied"),
        ((403, "Local API is not enabled"), "local_api_disabled"),
        ((429, "slow down"), "rate_limited"),
        ((500, "boom"), "error"),
        ((404, "no such endpoint"), "error"),
        ((200, json.dumps({"approved": True})), "error"),
        ((200, "not json"), "error"),
        (TimeoutError("read timed out"), "timeout"),
        (urllib.error.URLError(TimeoutError("connect timed out")), "timeout"),
        (ConnectionRefusedError("refused"), "unreachable"),
        (ValueError("odd transport failure"), "error"),
        ((200, "[1]"), "error"),
    ],
)
def test_connect_failures_store_nothing(env, outcome, expected):
    result = zc.connect(transport=Scripted(outcome))
    assert result["status"] == expected
    assert env.store.key is None
    assert env.store.set_calls == 0


def test_connect_refuses_a_non_loopback_base_url_without_posting(env):
    transport = Scripted()
    result = zc.connect(base_url="https://zotero.example.com/api", transport=transport)
    assert result["status"] == "error"
    assert transport.calls == []
    assert env.store.key is None


def test_connect_approved_but_store_unavailable_returns_status_without_the_key(env):
    env.store.raise_on_set = ZoteroCredentialStoreUnavailable("no vault")
    result = zc.connect(transport=Scripted((200, json.dumps({"key": ISSUED_KEY}))))
    assert result["status"] == "store_unavailable"
    assert ISSUED_KEY not in json.dumps(result)
    assert env.store.key is None


def test_connect_allows_only_one_call_per_sixty_seconds(env):
    assert zc.CONNECT_MIN_INTERVAL_S == 60
    clock = FakeClock(1000.0)
    assert zc.connect(transport=Scripted((403, "denied")), clock=clock)["status"] == "denied"

    clock.now += 30
    second = Scripted((200, json.dumps({"key": ISSUED_KEY})))
    blocked = zc.connect(transport=second, clock=clock)
    assert blocked["status"] == "cooldown"
    assert blocked["retry_after_s"] == 30
    assert second.calls == []
    assert env.store.key is None

    clock.now += 30  # now 60 seconds after the first call
    assert zc.connect(transport=second, clock=clock)["status"] == "connected"
    assert len(second.calls) == 1


def test_a_timed_out_attempt_still_starts_the_cooldown(env):
    clock = FakeClock(0.0)
    assert zc.connect(transport=Scripted(TimeoutError("slow")), clock=clock)["status"] == "timeout"
    blocked = zc.connect(transport=Scripted((200, json.dumps({"key": ISSUED_KEY}))), clock=clock)
    assert blocked["status"] == "cooldown"
    assert env.store.key is None


def test_cooldown_survives_a_new_process_through_the_state_file(env):
    clock = FakeClock(500.0)
    zc.connect(transport=Scripted((403, "denied")), clock=clock)
    assert env.state_path.is_file()
    zc._IN_PROCESS_LAST_ATTEMPT = None  # a fresh process has no in-memory record
    clock.now = 510.0
    later = Scripted()
    assert zc.connect(transport=later, clock=clock)["status"] == "cooldown"
    assert later.calls == []


# ---------------------------------------------------------------------------
# write_with_stored_approval(): stored key only, never connect()
# ---------------------------------------------------------------------------

def test_write_without_a_stored_approval_sends_nothing_and_never_connects(env, monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("a write must never call connect()")

    monkeypatch.setattr(zc, "connect", boom)
    transport = Scripted()
    result = zc.write_with_stored_approval(_write_request(), transport=transport)
    assert result["status"] == "needs_connection"
    assert transport.calls == []


def test_write_sends_the_key_only_in_the_authorization_header(env):
    env.store.key = ISSUED_KEY
    transport = Scripted((201, "{}"))
    result = zc.write_with_stored_approval(_write_request([1]), transport=transport)
    assert result == {"status": "written", "http_status": 201}
    call = transport.calls[0]
    assert call["url"] == SAVE_URL
    assert call["headers"]["Authorization"] == f"Bearer {ISSUED_KEY}"
    assert ISSUED_KEY not in (call["body"] or b"").decode("utf-8")
    assert ISSUED_KEY not in json.dumps(result)


@pytest.mark.parametrize("code", [401])
def test_a_rejected_key_is_deleted_and_never_triggers_a_new_approval(env, monkeypatch, code):
    # A 403 is handled separately: it may also mean the local API is switched off (see the F2 tests below).
    env.store.key = ISSUED_KEY

    def boom(*args, **kwargs):
        raise AssertionError("a rejected key must never call connect()")

    monkeypatch.setattr(zc, "connect", boom)
    transport = Scripted((code, "{}"))
    result = zc.write_with_stored_approval(_write_request(), transport=transport)
    assert result["status"] == "needs_connection"
    assert env.store.key is None
    assert len(transport.calls) == 1
    assert all(call["url"] != AUTHORIZE for call in transport.calls)


def test_a_non_loopback_url_is_refused_before_the_key_is_sent(env):
    env.store.key = ISSUED_KEY
    transport = Scripted()
    request = {"method": "POST", "url": "https://attacker.example.com/collect", "payload": {}}
    result = zc.write_with_stored_approval(request, transport=transport)
    assert result["status"] == "error"
    assert transport.calls == []
    assert env.store.key == ISSUED_KEY


def test_an_unreachable_zotero_keeps_the_stored_key(env):
    env.store.key = ISSUED_KEY
    result = zc.write_with_stored_approval(_write_request(), transport=Scripted(ConnectionRefusedError("refused")))
    assert result["status"] == "unreachable"
    assert env.store.key == ISSUED_KEY


# ---------------------------------------------------------------------------
# disconnect()
# ---------------------------------------------------------------------------

def test_disconnect_removes_the_key_and_says_only_zotero_can_revoke_it(env):
    env.store.key = ISSUED_KEY
    result = zc.disconnect()
    assert env.store.key is None
    assert result["removed"] is True
    assert "Advanced" in result["message"]
    assert "clear" in result["message"].lower()
    assert ISSUED_KEY not in json.dumps(result)


def test_disconnect_with_nothing_stored_still_returns_the_revocation_text(env):
    result = zc.disconnect()
    assert result["removed"] is False
    assert "Advanced" in result["message"]


# ---------------------------------------------------------------------------
# The key never appears in a result, a log or an error string
# ---------------------------------------------------------------------------

def test_the_key_appears_in_no_result_log_or_error_string(env, caplog):
    caplog.set_level(logging.DEBUG)
    results = []
    results.append(zc.connect(transport=Scripted((200, json.dumps({"key": ISSUED_KEY}))), clock=FakeClock(0.0)))
    env.store.key = ISSUED_KEY
    # Zotero echoes the key back in an error body and the transport error names it.
    results.append(zc.write_with_stored_approval(
        _write_request(), transport=Scripted((500, f"bad key {ISSUED_KEY}")),
    ))
    results.append(zc.write_with_stored_approval(
        _write_request(), transport=Scripted(ConnectionRefusedError(f"refused {ISSUED_KEY}")),
    ))
    results.append(zc.connect(
        transport=Scripted(TimeoutError(f"slow {ISSUED_KEY}")), clock=FakeClock(120.0),
    ))
    results.append(zc.write_with_stored_approval(_write_request(), transport=Scripted((401, "no"))))
    results.append(zc.status(opener=probe_opener(ConnectionRefusedError(f"x {ISSUED_KEY}"))))
    results.append(zc.disconnect())
    serialized = json.dumps(results)
    assert ISSUED_KEY not in serialized
    assert ISSUED_KEY not in caplog.text
    assert ISSUED_KEY not in str(results)


# ---------------------------------------------------------------------------
# The filer's live path uses the stored approval and never authorizes
# ---------------------------------------------------------------------------

def _filer_routes(on_save=None):
    saved = {"done": False}

    def items(url, body):
        if not saved["done"]:
            return 200, []
        return 200, [{"key": "NEW00001", "data": {
            "key": "NEW00001", "itemType": "journalArticle", "DOI": "10.1000/one", "title": "Paper One",
        }}]

    def save(url, body):
        saved["done"] = True
        return (201, {}) if on_save is None else on_save(url, body)

    return {
        ("GET", COLLECTIONS): (200, [{"key": "ABCD1234", "data": {"key": "ABCD1234", "name": "Research"}}]),
        ("GET", ITEMS): items,
        ("POST", SELECTED): (200, {"id": "C1", "name": "Research", "targets": []}),
        ("POST", SAVE): save,
    }


@pytest.fixture
def live_filer(monkeypatch):
    """Stop the filer from opening the Zotero UI, so the tests stay on fake transports."""
    opened = []

    def record_open(key):
        opened.append(key)
        return False

    monkeypatch.setattr(zf, "open_collection_in_zotero", record_open)
    return SimpleNamespace(opened=opened)


def test_live_run_without_a_stored_approval_stops_before_any_write_or_authorize(env, bib_file, live_filer):
    transport = Routed(_filer_routes())
    messages = []
    result = zf.file_bib_files([bib_file], "Research", transport=transport, log=messages.append)
    assert result["status"] == "needs_connection"
    assert sum("Connect Meridian to Zotero first" in line for line in messages) == 1
    assert transport.posts() == []
    assert all("/local/authorize" not in call["url"] for call in transport.calls)
    assert live_filer.opened == []


def test_cli_live_run_without_an_approval_exits_two_with_one_connect_line(env, bib_file, live_filer, monkeypatch, capsys):
    transport = Routed(_filer_routes())
    monkeypatch.setattr(zf, "_urllib_transport", transport)
    code = zf.main(["file", "--collection", "Research", "--bib", bib_file, "--backend", "desktop"])
    captured = capsys.readouterr()
    assert code == 2
    connect_lines = [line for line in captured.err.splitlines() if "Connect Meridian to Zotero first" in line]
    assert len(connect_lines) == 1
    assert json.loads(captured.out)["status"] == "needs_connection"
    assert transport.posts() == []


def test_live_run_with_a_stored_approval_never_sends_the_key_to_the_connector(env, bib_file, live_filer):
    env.store.key = ISSUED_KEY
    transport = Routed(_filer_routes())
    messages = []
    result = zf.file_bib_files([bib_file], "Research", transport=transport, log=messages.append)
    assert result["status"] == "ok"
    save_calls = [call for call in transport.calls if call["url"] == SAVE]
    assert len(save_calls) == 1
    assert "Authorization" not in save_calls[0]["headers"]
    assert "Zotero-API-Key" not in save_calls[0]["headers"]
    assert ISSUED_KEY not in (save_calls[0]["body"] or b"").decode("utf-8")
    assert ISSUED_KEY not in json.dumps(result)
    assert ISSUED_KEY not in "\n".join(messages)


def test_a_connector_rejection_mid_run_keeps_the_stored_approval(env, bib_file, live_filer):
    env.store.key = ISSUED_KEY
    transport = Routed(_filer_routes(on_save=lambda url, body: (401, {})))
    messages = []
    result = zf.file_bib_files([bib_file], "Research", transport=transport, log=messages.append)
    assert result["status"] == "save_failed"
    assert env.store.key == ISSUED_KEY
    assert env.store.delete_calls == 0


def test_a_dry_run_never_asks_for_an_approval(env, bib_file, live_filer):
    transport = Routed(_filer_routes())
    result = zf.file_bib_files([bib_file], "Research", transport=transport, dry_run=True, log=lambda m: None)
    assert result["status"] == "dry_run"
    assert transport.posts() == []


# ---------------------------------------------------------------------------
# The connect route: logged-in caller, explicit confirm, one connect() per call
# ---------------------------------------------------------------------------

# The self-hosted route is off unless this variable is 1, true or yes (D2).
ROUTE_ENV = "MERIDIAN_ZOTERO_CONNECT_ENABLED"


def _route_client(monkeypatch, *, hosted=False, tenant=None, client_host="127.0.0.1", connect_env="1"):
    """Build a test client for the route. ``connect_env=None`` leaves the switch unset."""
    monkeypatch.setattr(zotero_route, "_hosted_mode", lambda: hosted)
    if connect_env is None:
        monkeypatch.delenv(ROUTE_ENV, raising=False)
    else:
        monkeypatch.setenv(ROUTE_ENV, connect_env)

    async def _tenant(request):
        return tenant

    monkeypatch.setattr(zotero_route, "_get_tenant_from_request", _tenant)
    app = FastAPI()
    app.include_router(zotero_route.router)
    return TestClient(app, client=(client_host, 50000))


def _recording_connect(monkeypatch, result):
    calls = []

    def fake(**kwargs):
        calls.append(kwargs)
        return dict(result)

    monkeypatch.setattr(zc, "connect", fake)
    return calls


def test_route_requires_an_explicit_confirm_and_json_body(env, monkeypatch):
    calls = _recording_connect(monkeypatch, {"status": "connected", "message": "ok"})
    client = _route_client(monkeypatch)
    assert client.post("/zotero/connect", json={}).status_code == 400
    assert client.post("/zotero/connect", data="confirm=true").status_code == 400
    assert calls == []


def test_route_hosted_without_a_logged_in_user_is_401(env, monkeypatch):
    calls = _recording_connect(monkeypatch, {"status": "connected", "message": "ok"})
    client = _route_client(monkeypatch, hosted=True, tenant=None)
    assert client.post("/zotero/connect", json={"confirm": True}).status_code == 401
    assert calls == []


def test_route_runs_connect_once_for_a_confirmed_logged_in_user(env, monkeypatch):
    calls = _recording_connect(monkeypatch, {"status": "connected", "message": "ok"})
    client = _route_client(monkeypatch, hosted=True, tenant={"id": "tenant-1"})
    response = client.post("/zotero/connect", json={"confirm": True})
    assert response.status_code == 200
    assert response.json()["status"] == "connected"
    assert len(calls) == 1


def test_route_maps_the_cooldown_to_429_with_retry_after(env, monkeypatch):
    _recording_connect(monkeypatch, {"status": "cooldown", "retry_after_s": 42, "message": "wait"})
    client = _route_client(monkeypatch)
    response = client.post("/zotero/connect", json={"confirm": True})
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "42"


def test_route_is_post_only(env, monkeypatch):
    client = _route_client(monkeypatch)
    assert client.get("/zotero/connect").status_code == 405


# ---------------------------------------------------------------------------
# The user-run command
# ---------------------------------------------------------------------------

def test_cli_connect_runs_connect_once_and_exits_nonzero_when_not_connected(env, monkeypatch, capsys):
    calls = _recording_connect(monkeypatch, {"status": "denied", "message": "no"})
    assert zc.main(["connect"]) == 1
    assert len(calls) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "denied"


def test_cli_status_and_disconnect_print_json_and_exit_zero(env, monkeypatch, capsys):
    monkeypatch.setattr(zc, "status", lambda **kwargs: {"status": "not_connected", "message": "m"})
    assert zc.main(["status"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "not_connected"
    env.store.key = ISSUED_KEY
    assert zc.main(["disconnect"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["removed"] is True
    assert ISSUED_KEY not in json.dumps(out)
    assert env.store.key is None


# ---------------------------------------------------------------------------
# Storage, transport and state-file edges
# ---------------------------------------------------------------------------

class FakeKeyring:
    """Stands in for a native keyring module. ``fail`` makes every call raise, like a locked vault."""

    def __init__(self, fail=False):
        self.values = {}
        self.fail = fail

    def get_password(self, service, account):
        if self.fail:
            raise RuntimeError("vault locked")
        return self.values.get((service, account))

    def set_password(self, service, account, value):
        if self.fail:
            raise RuntimeError("vault locked")
        self.values[(service, account)] = value

    def delete_password(self, service, account):
        if self.fail:
            raise RuntimeError("vault locked")
        del self.values[(service, account)]


def test_keyring_store_round_trips_under_meridians_service(monkeypatch):
    ring = FakeKeyring()
    monkeypatch.setattr(zc, "_native_keyring", lambda: ring)
    store = zc.KeyringApprovalStore()
    store.set(ISSUED_KEY)
    assert store.get() == ISSUED_KEY
    assert ("Meridian", "zotero-local-approval") in ring.values
    assert store.delete() is True
    assert store.get() is None
    assert store.delete() is False


def test_keyring_store_reads_as_not_connected_when_no_native_store_exists(monkeypatch):
    def unavailable():
        raise ZoteroCredentialStoreUnavailable("no vault")

    monkeypatch.setattr(zc, "_native_keyring", unavailable)
    store = zc.KeyringApprovalStore()
    assert store.get() is None
    assert store.delete() is False
    with pytest.raises(ZoteroCredentialStoreUnavailable):
        store.set(ISSUED_KEY)


def test_keyring_store_failures_surface_without_the_key(monkeypatch):
    monkeypatch.setattr(zc, "_native_keyring", lambda: FakeKeyring(fail=True))
    store = zc.KeyringApprovalStore()
    assert store.get() is None
    with pytest.raises(ZoteroCredentialStoreUnavailable) as excinfo:
        store.set(ISSUED_KEY)
    assert ISSUED_KEY not in str(excinfo.value)
    with pytest.raises(ZoteroCredentialStoreUnavailable):
        store.delete()


class _Ctx:
    def __init__(self, response):
        self.response = response

    def __enter__(self):
        return self.response

    def __exit__(self, *exc):
        return False


def test_default_transport_returns_status_and_body_and_http_errors_as_values(monkeypatch):
    monkeypatch.setattr(
        zc, "_TRANSPORT_OPENER",
        SimpleNamespace(open=lambda request, timeout: _Ctx(FakeResponse(201, {}, b'{"ok": 1}'))),
    )
    assert zc._default_transport(5.0)("POST", AUTHORIZE, b"{}", {}) == (201, '{"ok": 1}')

    def refuse(request, timeout):
        raise urllib.error.HTTPError(AUTHORIZE, 403, "Forbidden", {}, io.BytesIO(b"denied by user"))

    monkeypatch.setattr(zc, "_TRANSPORT_OPENER", SimpleNamespace(open=refuse))
    assert zc._default_transport(5.0)("POST", AUTHORIZE, b"{}", {}) == (403, "denied by user")


def test_status_probe_branches(env):
    server_error = urllib.error.HTTPError(PROBE, 500, "boom", {}, None)
    assert zc.status(opener=probe_opener(server_error))["status"] == "unreachable"

    class CodeOnly:
        headers = {"Zotero-Server-ID": "srv-1"}

        def getcode(self):
            return 200

        def read(self):
            return b"[]"

        def close(self):
            pass

    assert zc.status(opener=probe_opener(CodeOnly()))["status"] == "not_connected"

    class BrokenBody(FakeResponse):
        def read(self):
            raise OSError("connection reset")

    assert zc.status(opener=probe_opener(BrokenBody(200, {"Zotero-Server-ID": "x"})))["status"] == "unreachable"
    assert zc.status(opener=probe_opener(FakeResponse(403, {"Zotero-Server-ID": "x"}, b"")))["status"] == "local_api_disabled"
    assert zc.status(base_url="https://zotero.example.com/api", opener=probe_opener())["status"] == "unreachable"


@pytest.mark.parametrize("content", ["{not json", json.dumps({"last_connect_at": True}), json.dumps(["x"])])
def test_an_unreadable_state_file_never_blocks_connect(env, content):
    env.state_path.write_text(content, encoding="utf-8")
    result = zc.connect(transport=Scripted((403, "denied")), clock=FakeClock(0.0))
    assert result["status"] == "denied"


def test_a_future_dated_record_is_ignored(env):
    env.state_path.write_text(json.dumps({"last_connect_at": 9999.0}), encoding="utf-8")
    result = zc.connect(transport=Scripted((403, "denied")), clock=FakeClock(0.0))
    assert result["status"] == "denied"


def test_an_unwritable_state_file_still_applies_the_in_process_limit(env, monkeypatch, tmp_path):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(zc, "_state_path", lambda: blocker / "zotero-connect.json")
    assert zc.connect(transport=Scripted((403, "denied")), clock=FakeClock(0.0))["status"] == "denied"
    blocked = zc.connect(transport=Scripted(), clock=FakeClock(10.0))
    assert blocked["status"] == "cooldown"


def test_a_write_without_a_payload_sends_no_body(env):
    env.store.key = ISSUED_KEY
    transport = Scripted((200, "{}"))
    result = zc.write_with_stored_approval({"method": "GET", "url": SAVE_URL}, transport=transport)
    assert result == {"status": "written", "http_status": 200}
    assert transport.calls[0]["body"] is None
    assert "Content-Type" not in transport.calls[0]["headers"]


# ---------------------------------------------------------------------------
# Review fixes F1-F5
# ---------------------------------------------------------------------------

class Constant:
    """Thread-safe fake transport that answers every call with one outcome."""

    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = []
        self._lock = threading.Lock()

    def __call__(self, method, url, body, headers):
        with self._lock:
            self.calls.append({"method": method, "url": url, "body": body, "headers": dict(headers)})
        return self.outcome


# F1: the cooldown read-check-write is atomic across threads and processes.

def test_two_concurrent_connect_calls_one_passes_and_one_gets_the_cooldown(env, monkeypatch):
    real_read = zc._read_last_attempt

    def slow_read(path):
        time.sleep(0.2)  # widen the read-check-write window: an unlocked check lets both calls through
        return real_read(path)

    monkeypatch.setattr(zc, "_read_last_attempt", slow_read)
    transport = Constant((200, json.dumps({"key": ISSUED_KEY})))
    clock = FakeClock(1000.0)
    barrier = threading.Barrier(2)
    statuses = []

    def worker():
        barrier.wait()
        statuses.append(zc.connect(transport=transport, clock=clock)["status"])

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert sorted(statuses) == ["connected", "cooldown"]
    assert len(transport.calls) == 1
    assert env.store.set_calls == 1


def test_a_cooldown_lock_held_by_another_process_blocks_connect(env, monkeypatch):
    monkeypatch.setattr(zc, "_LOCK_TIMEOUT_S", 0.2)
    transport = Scripted()
    with zc._file_lock(zc._lock_path(env.state_path), 1.0) as held:
        assert held is True
        result = zc.connect(transport=transport, clock=FakeClock(0.0))
    assert result["status"] == "cooldown"
    assert transport.calls == []
    assert env.store.key is None


# F2: a 403 from the local write path is probed before anything is forgotten.

def test_403_with_a_failing_probe_keeps_the_key_and_reports_local_api_disabled(env):
    env.store.key = ISSUED_KEY
    transport = Scripted((403, "Forbidden"), (403, "Forbidden"))
    result = zc.write_with_stored_approval(_write_request(), transport=transport)
    assert result["status"] == "local_api_disabled"
    assert env.store.key == ISSUED_KEY
    assert env.store.delete_calls == 0
    assert [call["method"] for call in transport.calls] == ["POST", "GET"]
    assert transport.calls[1]["url"] == PROBE
    assert "Authorization" not in transport.calls[1]["headers"]
    assert ISSUED_KEY not in json.dumps(result)


def test_403_whose_probe_reports_the_local_api_not_enabled_keeps_the_key(env):
    env.store.key = ISSUED_KEY
    transport = Scripted((403, "denied"), (403, "Local API is not enabled"))
    assert zc.write_with_stored_approval(_write_request(), transport=transport)["status"] == "local_api_disabled"
    assert env.store.key == ISSUED_KEY
    assert env.store.delete_calls == 0


def test_403_with_a_successful_probe_deletes_the_key(env):
    env.store.key = ISSUED_KEY
    transport = Scripted((403, "denied"), (200, "[]"))
    result = zc.write_with_stored_approval(_write_request(), transport=transport)
    assert result["status"] == "needs_connection"
    assert env.store.key is None
    assert env.store.delete_calls == 1
    assert [call["method"] for call in transport.calls] == ["POST", "GET"]


def test_403_whose_probe_cannot_reach_zotero_keeps_the_key(env):
    env.store.key = ISSUED_KEY
    transport = Scripted((403, "denied"), ConnectionRefusedError("refused"))
    result = zc.write_with_stored_approval(_write_request(), transport=transport)
    assert result["status"] == "unreachable"
    assert env.store.key == ISSUED_KEY
    assert env.store.delete_calls == 0


def test_401_deletes_the_key_without_probing(env):
    env.store.key = ISSUED_KEY
    transport = Scripted((401, "no"))
    assert zc.write_with_stored_approval(_write_request(), transport=transport)["status"] == "needs_connection"
    assert env.store.key is None
    assert [call["method"] for call in transport.calls] == ["POST"]


# F3: the connector save path never sends the approval and never touches the store.

@pytest.mark.parametrize("outcome", [(401, "no"), (403, "denied"), (500, "boom")])
def test_connector_rejection_leaves_the_stored_approval_intact(env, monkeypatch, outcome):
    env.store.key = ISSUED_KEY

    def no_store(*args, **kwargs):
        raise AssertionError("the connector save path must never open the approval store")

    monkeypatch.setattr(zc, "KeyringApprovalStore", no_store)
    result = zf.save_items([{"title": "T"}], Scripted(outcome))
    assert result["status"] == "error"
    assert env.store.key == ISSUED_KEY
    assert env.store.delete_calls == 0


def test_connector_requests_carry_no_approval_or_api_key_header(env):
    env.store.key = ISSUED_KEY
    transport = Scripted((201, "{}"))
    assert zf.save_items([{"title": "T"}], transport) == {"status": "written", "http_status": 201}
    headers = transport.calls[0]["headers"]
    assert "Authorization" not in headers
    assert "Zotero-API-Key" not in headers
    assert headers["Zotero-Allowed-Request"] == "1"
    assert ISSUED_KEY not in (transport.calls[0]["body"] or b"").decode("utf-8")


# F4: a request that carries the approval never follows a redirect off loopback.

class _ServerState:
    """A real loopback HTTP server. Paths listed in routes answer with a 302 to the mapped location."""

    def __init__(self):
        self.seen = []
        self.routes = {}
        self.base = ""


@pytest.fixture
def loopback_server():
    state = _ServerState()

    class Handler(http.server.BaseHTTPRequestHandler):
        def _answer(self):
            state.seen.append((self.path, self.headers.get("Authorization")))
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            target = state.routes.get(self.path)
            if target is None:
                body = b"{}"
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(302)
                self.send_header("Location", target)
                self.send_header("Content-Length", "0")
                self.end_headers()

        do_GET = _answer
        do_POST = _answer

        def log_message(self, format, *args):  # noqa: A002 - matches the base class signature
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def no_offloopback_connections(monkeypatch):
    """Record, and refuse, any connection the real HTTP client opens to a non-loopback host."""
    attempts = []
    real_connect = http.client.HTTPConnection.connect

    def guarded_connect(self):
        if self.host not in ("127.0.0.1", "localhost", "::1"):
            attempts.append(self.host)
            raise ConnectionRefusedError("test: no connection off loopback")
        return real_connect(self)

    monkeypatch.setattr(http.client.HTTPConnection, "connect", guarded_connect)
    return attempts


def test_a_redirect_off_loopback_raises_and_sends_no_approval_to_the_target(loopback_server, no_offloopback_connections):
    loopback_server.routes["/start"] = "http://203.0.113.10:9/collect"
    send = zc._default_transport(2.0)
    with pytest.raises(Exception) as excinfo:
        send("POST", loopback_server.base + "/start", b"{}", {"Authorization": f"Bearer {ISSUED_KEY}"})
    assert no_offloopback_connections == []
    assert loopback_server.seen == [("/start", f"Bearer {ISSUED_KEY}")]
    assert type(excinfo.value).__name__ == "_RedirectRefused"
    assert ISSUED_KEY not in str(excinfo.value)


def test_every_redirect_hop_is_checked_not_only_the_first(loopback_server, no_offloopback_connections):
    loopback_server.routes["/start"] = loopback_server.base + "/hop"
    loopback_server.routes["/hop"] = "http://203.0.113.10:9/collect"
    send = zc._default_transport(2.0)
    with pytest.raises(Exception) as excinfo:
        send("POST", loopback_server.base + "/start", b"{}", {"Authorization": f"Bearer {ISSUED_KEY}"})
    assert [path for path, _auth in loopback_server.seen] == ["/start", "/hop"]
    assert no_offloopback_connections == []
    assert type(excinfo.value).__name__ == "_RedirectRefused"


def test_a_write_redirected_off_loopback_keeps_the_key_and_reports_error(loopback_server, no_offloopback_connections):
    loopback_server.routes["/start"] = "http://203.0.113.10:9/collect"
    store = MemoryStore(ISSUED_KEY)
    result = zc.write_with_stored_approval(
        {"method": "POST", "url": loopback_server.base + "/start", "payload": {}}, store=store,
    )
    assert result["status"] == "error"
    assert store.key == ISSUED_KEY
    assert store.delete_calls == 0
    assert no_offloopback_connections == []
    assert ISSUED_KEY not in json.dumps(result)


# F5: the self-hosted connect route accepts only loopback clients.

@pytest.mark.parametrize("client_host", ["203.0.113.9", "192.168.1.20", "10.0.0.5"])
def test_self_hosted_connect_route_rejects_a_non_loopback_client(env, monkeypatch, client_host):
    calls = _recording_connect(monkeypatch, {"status": "connected", "message": "ok"})
    client = _route_client(monkeypatch, client_host=client_host)
    response = client.post("/zotero/connect", json={"confirm": True})
    assert response.status_code == 403
    assert calls == []


@pytest.mark.parametrize("client_host", ["127.0.0.1", "::1"])
def test_self_hosted_connect_route_accepts_loopback_clients(env, monkeypatch, client_host):
    calls = _recording_connect(monkeypatch, {"status": "connected", "message": "ok"})
    client = _route_client(monkeypatch, client_host=client_host)
    assert client.post("/zotero/connect", json={"confirm": True}).status_code == 200
    assert len(calls) == 1


# Forwarded-header refusal: a request relayed by a proxy is not a local use of the
# self-hosted route. uvicorn (--proxy-headers --forwarded-allow-ips=*) rewrites the
# client address from X-Forwarded-For, so the loopback check alone can be spoofed.

@pytest.mark.parametrize("client_host", ["127.0.0.1", "::1"])
def test_self_hosted_connect_route_refuses_a_loopback_request_with_x_forwarded_for(env, monkeypatch, client_host):
    calls = _recording_connect(monkeypatch, {"status": "connected", "message": "ok"})
    client = _route_client(monkeypatch, client_host=client_host)
    response = client.post(
        "/zotero/connect", json={"confirm": True}, headers={"X-Forwarded-For": "127.0.0.1"},
    )
    assert response.status_code == 403
    assert calls == []


@pytest.mark.parametrize("client_host", ["127.0.0.1", "::1"])
def test_self_hosted_connect_route_refuses_a_loopback_request_with_x_real_ip(env, monkeypatch, client_host):
    calls = _recording_connect(monkeypatch, {"status": "connected", "message": "ok"})
    client = _route_client(monkeypatch, client_host=client_host)
    response = client.post("/zotero/connect", json={"confirm": True}, headers={"X-Real-IP": "127.0.0.1"})
    assert response.status_code == 403
    assert calls == []


@pytest.mark.parametrize(
    "headers",
    [
        {"X-Forwarded-Host": "meridian.example"},
        {"X-Forwarded-Proto": "https"},
        {"Forwarded": "for=127.0.0.1;proto=https"},
        {"x-forwarded-for": "127.0.0.1"},
    ],
)
def test_self_hosted_connect_route_refuses_any_other_forwarding_header(env, monkeypatch, headers):
    calls = _recording_connect(monkeypatch, {"status": "connected", "message": "ok"})
    client = _route_client(monkeypatch, client_host="127.0.0.1")
    response = client.post("/zotero/connect", json={"confirm": True}, headers=headers)
    assert response.status_code == 403
    assert calls == []


@pytest.mark.parametrize("client_host", ["127.0.0.1", "::1"])
def test_self_hosted_plain_loopback_request_still_reaches_connect(env, monkeypatch, client_host):
    calls = _recording_connect(monkeypatch, {"status": "connected", "message": "ok"})
    client = _route_client(monkeypatch, client_host=client_host)
    response = client.post("/zotero/connect", json={"confirm": True})
    assert response.status_code == 200
    assert len(calls) == 1


@pytest.mark.parametrize("client_host", ["203.0.113.9", "192.168.1.20", "10.0.0.5"])
def test_self_hosted_connect_route_refuses_a_non_loopback_request_even_when_it_claims_loopback(
    env, monkeypatch, client_host,
):
    calls = _recording_connect(monkeypatch, {"status": "connected", "message": "ok"})
    client = _route_client(monkeypatch, client_host=client_host)
    response = client.post(
        "/zotero/connect", json={"confirm": True}, headers={"X-Forwarded-For": "127.0.0.1"},
    )
    assert response.status_code == 403
    assert calls == []


def test_hosted_connect_route_accepts_a_logged_in_request_that_carries_forwarding_headers(env, monkeypatch):
    # Hosted traffic always arrives through the platform proxy, so forwarding headers
    # are normal there and must not be refused.
    calls = _recording_connect(monkeypatch, {"status": "connected", "message": "ok"})
    client = _route_client(monkeypatch, hosted=True, tenant={"id": "tenant-1"}, client_host="10.0.0.5")
    response = client.post(
        "/zotero/connect",
        json={"confirm": True},
        headers={"X-Forwarded-For": "203.0.113.7", "X-Forwarded-Proto": "https", "X-Real-IP": "203.0.113.7"},
    )
    assert response.status_code == 200
    assert len(calls) == 1


def test_hosted_connect_route_still_requires_a_logged_in_user_with_forwarding_headers(env, monkeypatch):
    calls = _recording_connect(monkeypatch, {"status": "connected", "message": "ok"})
    client = _route_client(monkeypatch, hosted=True, tenant=None, client_host="10.0.0.5")
    response = client.post(
        "/zotero/connect", json={"confirm": True}, headers={"X-Forwarded-For": "203.0.113.7"},
    )
    assert response.status_code == 401
    assert calls == []


# D2: the self-hosted route is off by default. Only MERIDIAN_ZOTERO_CONNECT_ENABLED
# set to 1, true or yes turns it on, and hosted mode ignores the switch.

@pytest.mark.parametrize("client_host", ["127.0.0.1", "::1"])
def test_self_hosted_connect_route_is_off_when_the_switch_is_unset(env, monkeypatch, client_host):
    calls = _recording_connect(monkeypatch, {"status": "connected", "message": "ok"})
    client = _route_client(monkeypatch, client_host=client_host, connect_env=None)
    response = client.post("/zotero/connect", json={"confirm": True})
    assert response.status_code == 403
    assert ROUTE_ENV in response.json()["error"]
    assert calls == []


@pytest.mark.parametrize("value", ["", "0", "false", "no", "on", "enabled"])
def test_self_hosted_connect_route_stays_off_for_any_other_value(env, monkeypatch, value):
    calls = _recording_connect(monkeypatch, {"status": "connected", "message": "ok"})
    client = _route_client(monkeypatch, connect_env=value)
    response = client.post("/zotero/connect", json={"confirm": True})
    assert response.status_code == 403
    assert calls == []


@pytest.mark.parametrize("value", ["1", "true", "yes", "TRUE", " Yes "])
def test_self_hosted_connect_route_proceeds_when_the_switch_is_set(env, monkeypatch, value):
    calls = _recording_connect(monkeypatch, {"status": "connected", "message": "ok"})
    client = _route_client(monkeypatch, connect_env=value)
    response = client.post("/zotero/connect", json={"confirm": True})
    assert response.status_code == 200
    assert len(calls) == 1


def test_enabled_route_still_refuses_a_non_loopback_client(env, monkeypatch):
    calls = _recording_connect(monkeypatch, {"status": "connected", "message": "ok"})
    client = _route_client(monkeypatch, connect_env="1", client_host="203.0.113.9")
    response = client.post("/zotero/connect", json={"confirm": True})
    assert response.status_code == 403
    assert response.json()["error"] == "connect runs only from this machine"
    assert calls == []


def test_enabled_route_still_requires_an_explicit_confirm(env, monkeypatch):
    calls = _recording_connect(monkeypatch, {"status": "connected", "message": "ok"})
    client = _route_client(monkeypatch, connect_env="1")
    assert client.post("/zotero/connect", json={}).status_code == 400
    assert calls == []


def test_hosted_connect_route_ignores_the_switch_when_it_is_unset(env, monkeypatch):
    calls = _recording_connect(monkeypatch, {"status": "connected", "message": "ok"})
    client = _route_client(monkeypatch, hosted=True, tenant={"id": "tenant-1"}, connect_env=None)
    response = client.post("/zotero/connect", json={"confirm": True})
    assert response.status_code == 200
    assert len(calls) == 1


@pytest.mark.parametrize("value", ["0", "no"])
def test_hosted_connect_route_is_not_switched_off_by_a_false_value(env, monkeypatch, value):
    calls = _recording_connect(monkeypatch, {"status": "connected", "message": "ok"})
    client = _route_client(monkeypatch, hosted=True, tenant={"id": "tenant-1"}, connect_env=value)
    response = client.post("/zotero/connect", json={"confirm": True})
    assert response.status_code == 200
    assert len(calls) == 1


def test_hosted_unauthenticated_request_is_still_401_when_the_switch_is_unset(env, monkeypatch):
    calls = _recording_connect(monkeypatch, {"status": "connected", "message": "ok"})
    client = _route_client(monkeypatch, hosted=True, tenant=None, connect_env=None)
    assert client.post("/zotero/connect", json={"confirm": True}).status_code == 401
    assert calls == []
