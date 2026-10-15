"""Tests for meridian.zotero_connect: the user-initiated Zotero connect flow.

Almost every test uses fake transports and an in-memory approval store. The real
transport, the real probe opener and the real OS keyring are replaced by guards
that record any use, and the fixture fails the test at teardown if one was reached.
No test opens a socket, and none reaches Zotero, a real credential store or any
host off this machine.

The redirect tests run the real redirect handlers on top of a fake Zotero
(RedirectingZotero), installed at the bottom of a real opener, so every hop is
checked and no request leaves the process. The keyring guard is autouse: a test
that omits both the env fixture and store= still cannot reach the real keyring.
"""

from __future__ import annotations

import ast
import email.message
import io
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.response
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from meridian import tunnel_config
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


@pytest.fixture(autouse=True)
def no_real_keyring(monkeypatch):
    """Every test, with or without env or store=: the native OS keyring is replaced by a recording guard.

    The guard raises ZoteroCredentialStoreUnavailable, which the approval store reads as "no store".
    Teardown fails the test if anything reached it. A test that patches _native_keyring itself
    overrides the guard for that test only.
    """
    reached = []

    def guarded_keyring():
        reached.append("native keyring")
        raise ZoteroCredentialStoreUnavailable("test: no real OS keyring")

    monkeypatch.setattr(zc, "_native_keyring", guarded_keyring)
    monkeypatch.setattr(tunnel_config, "_native_keyring", guarded_keyring)
    yield reached
    assert reached == [], "the real OS keyring was reached"


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
def test_a_rejected_key_is_kept_and_never_triggers_a_new_approval(env, monkeypatch, code):
    # A 403 is handled separately: it may also mean the local API is switched off (see the F2 tests below).
    # The write path keeps the key, as the filer does. Only connect() replaces it, and only disconnect() removes it.
    env.store.key = ISSUED_KEY

    def boom(*args, **kwargs):
        raise AssertionError("a rejected key must never call connect()")

    monkeypatch.setattr(zc, "connect", boom)
    transport = Scripted((code, "{}"))
    result = zc.write_with_stored_approval(_write_request(), transport=transport)
    assert result["status"] == "rejected"
    assert result["message"] == zc.REJECTED_LINE
    assert env.store.key == ISSUED_KEY
    assert env.store.delete_calls == 0
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


def _route_client(monkeypatch, *, hosted=False, client_host="127.0.0.1", connect_env="1"):
    """Build a test client for the route. ``connect_env=None`` leaves the switch unset."""
    monkeypatch.setattr(zotero_route, "_hosted_mode", lambda: hosted)
    if connect_env is None:
        monkeypatch.delenv(ROUTE_ENV, raising=False)
    else:
        monkeypatch.setenv(ROUTE_ENV, connect_env)
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


# Hosted tier: connect is refused outright, for every caller. A hosted server's Zotero approval is
# process-wide and shared by every tenant, so no tenant may register it.

def test_route_hosted_refuses_an_anonymous_caller_with_403_and_runs_no_connect(env, monkeypatch):
    calls = _recording_connect(monkeypatch, {"status": "connected", "message": "ok"})
    client = _route_client(monkeypatch, hosted=True)
    response = client.post("/zotero/connect", json={"confirm": True})
    assert response.status_code == 403
    assert response.json()["error"] == zotero_route._HOSTED_REFUSAL
    assert calls == []


def test_route_runs_connect_once_for_a_confirmed_self_hosted_loopback_caller(env, monkeypatch):
    calls = _recording_connect(monkeypatch, {"status": "connected", "message": "ok"})
    client = _route_client(monkeypatch, hosted=False)
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


def test_403_with_a_successful_probe_keeps_the_key_and_reports_rejected(env):
    env.store.key = ISSUED_KEY
    transport = Scripted((403, "denied"), (200, "[]"))
    result = zc.write_with_stored_approval(_write_request(), transport=transport)
    assert result["status"] == "rejected"
    assert result["http_status"] == 403
    assert env.store.key == ISSUED_KEY
    assert env.store.delete_calls == 0
    assert [call["method"] for call in transport.calls] == ["POST", "GET"]


def test_403_whose_probe_cannot_reach_zotero_keeps_the_key(env):
    env.store.key = ISSUED_KEY
    transport = Scripted((403, "denied"), ConnectionRefusedError("refused"))
    result = zc.write_with_stored_approval(_write_request(), transport=transport)
    assert result["status"] == "unreachable"
    assert env.store.key == ISSUED_KEY
    assert env.store.delete_calls == 0


def test_401_keeps_the_key_and_reports_rejected_without_probing(env):
    env.store.key = ISSUED_KEY
    transport = Scripted((401, "no"))
    assert zc.write_with_stored_approval(_write_request(), transport=transport)["status"] == "rejected"
    assert env.store.key == ISSUED_KEY
    assert env.store.delete_calls == 0
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
# self-hosted route. uvicorn (--proxy-headers) rewrites the client address from
# X-Forwarded-For when the peer is trusted, and a trusted Docker gateway peer can carry
# a spoofed header, so the loopback check alone can be spoofed. The Dockerfile trust
# list is tested in test_zotero_dockerfile_proxy_trust.py.

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


def test_hosted_connect_route_refuses_a_request_behind_the_platform_proxy_with_403(env, monkeypatch):
    # Hosted traffic arrives through the platform proxy with forwarding headers. The tier refusal
    # does not depend on them: the request is refused the same way with or without them.
    calls = _recording_connect(monkeypatch, {"status": "connected", "message": "ok"})
    client = _route_client(monkeypatch, hosted=True, client_host="10.0.0.5")
    response = client.post(
        "/zotero/connect",
        json={"confirm": True},
        headers={"X-Forwarded-For": "203.0.113.7", "X-Forwarded-Proto": "https", "X-Real-IP": "203.0.113.7"},
    )
    assert response.status_code == 403
    assert response.json()["error"] == zotero_route._HOSTED_REFUSAL
    assert calls == []


# D2: the self-hosted route is off by default. Only MERIDIAN_ZOTERO_CONNECT_ENABLED
# set to 1, true or yes turns it on. The hosted tier refuses the route whatever the switch says.

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


@pytest.mark.parametrize("connect_env", [None, "0", "no", "1", "true", "yes"])
def test_hosted_connect_route_is_403_whatever_the_self_hosted_switch_says(env, monkeypatch, connect_env):
    calls = _recording_connect(monkeypatch, {"status": "connected", "message": "ok"})
    client = _route_client(monkeypatch, hosted=True, connect_env=connect_env)
    response = client.post("/zotero/connect", json={"confirm": True})
    assert response.status_code == 403
    assert response.json()["error"] == zotero_route._HOSTED_REFUSAL
    assert calls == []


# ---------------------------------------------------------------------------
# send_with_stored_approval(): the filer's approval-carrying write. Never connect().
# ---------------------------------------------------------------------------

ATTACH_WRITE = zf.LOCAL_API + "/items"


class HeaderScripted:
    """Header-aware fake: each call returns or raises the next outcome, and records the call."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def __call__(self, method, url, body, headers, timeout=None):
        self.calls.append({"method": method, "url": url, "body": body, "headers": dict(headers), "timeout": timeout})
        if not self.outcomes:
            raise AssertionError(f"unexpected request: {method} {url}")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def test_stored_write_without_an_approval_sends_nothing_and_never_connects(env, monkeypatch):
    env.store.key = None
    monkeypatch.setattr(zc, "connect", lambda **kwargs: pytest.fail("connect() must never run from a write"))
    header = HeaderScripted()
    result = zc.send_with_stored_approval("POST", ATTACH_WRITE, [{}], header_transport=header)
    assert result == {"status": "needs_connection", "message": zc._MESSAGES["needs_connection"]}
    assert header.calls == []


def test_stored_write_sends_the_key_only_in_authorization_and_returns_the_reply_body(env, monkeypatch):
    env.store.key = ISSUED_KEY
    monkeypatch.setattr(zc, "connect", lambda **kwargs: pytest.fail("connect() must never run from a write"))
    header = HeaderScripted((200, '{"success": {"0": "A1"}}', {}))
    result = zc.send_with_stored_approval(
        "POST", ATTACH_WRITE, [{"a": 1}], headers={"Zotero-Server-ID": "S1"}, header_transport=header,
    )
    assert result == {"status": "written", "http_status": 200, "text": '{"success": {"0": "A1"}}'}
    call = header.calls[0]
    assert call["headers"]["Authorization"] == f"Bearer {ISSUED_KEY}"
    assert call["headers"]["Zotero-Server-ID"] == "S1"
    assert "Zotero-API-Key" not in call["headers"]
    assert ISSUED_KEY not in json.dumps(result)


def test_stored_write_429_is_rate_limited_with_retry_after_and_keeps_the_key(env):
    env.store.key = ISSUED_KEY
    header = HeaderScripted((429, "slow", {"Retry-After": "30"}))
    result = zc.send_with_stored_approval("POST", ATTACH_WRITE, [], header_transport=header)
    assert result["status"] == "rate_limited"
    assert result["retry_after"] == "30"
    assert env.store.key == ISSUED_KEY
    assert env.store.delete_calls == 0


def test_stored_write_refuses_a_non_loopback_url_before_sending(env):
    env.store.key = ISSUED_KEY
    header = HeaderScripted()
    result = zc.send_with_stored_approval("POST", "https://example.test/items", [], header_transport=header)
    assert result["status"] == "error"
    assert header.calls == []


def test_stored_write_403_with_a_failing_probe_keeps_the_key(env):
    env.store.key = ISSUED_KEY
    header = HeaderScripted((403, "no", {}), ConnectionRefusedError("down"))
    result = zc.send_with_stored_approval("POST", ATTACH_WRITE, [], header_transport=header)
    assert result["status"] == "unreachable"
    assert env.store.key == ISSUED_KEY
    assert env.store.delete_calls == 0
    assert all(call["method"] == "GET" for call in header.calls[1:])


# ---------------------------------------------------------------------------
# The filer path keeps a rejected key. Only connect() replaces it. The stored
# senders refuse the authorize path before any request is sent.
# ---------------------------------------------------------------------------

def test_filer_send_401_keeps_the_key_and_says_running_connect_again_replaces_it(env, monkeypatch):
    env.store.key = ISSUED_KEY
    monkeypatch.setattr(zc, "connect", lambda **kwargs: pytest.fail("connect() must never run from a send"))
    header = HeaderScripted((401, "no", {}))
    result = zc.send_with_stored_approval("POST", ATTACH_WRITE, [{}], header_transport=header)
    assert result["status"] == "rejected"
    assert result["message"] == (
        "Zotero rejected the stored approval. Running 'python -m meridian.zotero_connect connect' again replaces it."
    )
    assert env.store.key == ISSUED_KEY
    assert env.store.delete_calls == 0
    assert len(header.calls) == 1
    assert ISSUED_KEY not in json.dumps(result)


def test_filer_send_403_with_a_reachable_probe_keeps_the_key(env, monkeypatch):
    env.store.key = ISSUED_KEY
    monkeypatch.setattr(zc, "connect", lambda **kwargs: pytest.fail("connect() must never run from a send"))
    header = HeaderScripted((403, "denied", {}), (200, "[]", {}))
    result = zc.send_with_stored_approval("POST", ATTACH_WRITE, [{}], header_transport=header)
    assert result["status"] == "rejected"
    assert "Running 'python -m meridian.zotero_connect connect' again replaces it." in result["message"]
    assert env.store.key == ISSUED_KEY
    assert env.store.delete_calls == 0
    assert [call["method"] for call in header.calls] == ["POST", "GET"]
    assert "Authorization" not in header.calls[1]["headers"]
    assert ISSUED_KEY not in json.dumps(result)


AUTHORIZE_VARIANTS = [
    AUTHORIZE,
    AUTHORIZE + "/",
    "http://127.0.0.1:23119/api/local/AUTHORIZE",
    "http://127.0.0.1:23119/api/local%2Fauthorize",
    "http://127.0.0.1:23119/api/users/0/../../local/authorize",
    "http://127.0.0.1:23119//api//local//authorize",
    "http://localhost:23119/api/local/authorize?appName=Meridian",
]


@pytest.mark.parametrize("url", AUTHORIZE_VARIANTS)
def test_write_with_stored_approval_refuses_the_authorize_path_before_any_request(env, monkeypatch, url):
    env.store.key = ISSUED_KEY
    monkeypatch.setattr(zc, "connect", lambda **kwargs: pytest.fail("connect() must never run from a write"))
    transport = Scripted()
    result = zc.write_with_stored_approval({"method": "POST", "url": url, "payload": {}}, transport=transport)
    assert result["status"] == "error"
    assert transport.calls == []
    assert env.store.key == ISSUED_KEY
    assert env.store.delete_calls == 0
    assert ISSUED_KEY not in json.dumps(result)


@pytest.mark.parametrize("url", AUTHORIZE_VARIANTS)
def test_send_with_stored_approval_refuses_the_authorize_path_before_any_request(env, monkeypatch, url):
    env.store.key = ISSUED_KEY
    monkeypatch.setattr(zc, "connect", lambda **kwargs: pytest.fail("connect() must never run from a send"))
    header = HeaderScripted()
    result = zc.send_with_stored_approval("POST", url, {"appName": "Meridian"}, header_transport=header)
    assert result["status"] == "error"
    assert header.calls == []
    assert env.store.key == ISSUED_KEY
    assert env.store.delete_calls == 0
    assert ISSUED_KEY not in json.dumps(result)


def test_authorize_path_is_refused_even_with_no_stored_approval(env, monkeypatch):
    monkeypatch.setattr(zc, "connect", lambda **kwargs: pytest.fail("connect() must never run from a send"))
    header = HeaderScripted()
    transport = Scripted()
    assert zc.send_with_stored_approval("POST", AUTHORIZE, {}, header_transport=header)["status"] == "error"
    assert zc.write_with_stored_approval({"method": "POST", "url": AUTHORIZE}, transport=transport)["status"] == "error"
    assert header.calls == []
    assert transport.calls == []


def test_an_unparseable_url_is_refused_before_any_request_so_the_key_never_leaves(env, monkeypatch):
    env.store.key = ISSUED_KEY
    monkeypatch.setattr(zc, "connect", lambda **kwargs: pytest.fail("connect() must never run from a sender"))
    bad = "http://[::1/api/local/items"
    transport = Scripted()
    header = HeaderScripted()
    write = zc.write_with_stored_approval({"method": "POST", "url": bad, "payload": {}}, transport=transport)
    assert write["status"] == "error"
    assert zc.send_with_stored_approval("POST", bad, {}, header_transport=header)["status"] == "error"
    assert transport.calls == []
    assert header.calls == []
    assert env.store.key == ISSUED_KEY
    assert env.store.delete_calls == 0


# ---------------------------------------------------------------------------
# Blocking review fixes. No redirect reaches the authorize endpoint, with or
# without a key. The write path keeps a rejected key. No approval-free probe
# targets the authorize endpoint. Only disconnect() removes the key.
# ---------------------------------------------------------------------------

# The real transport, captured before the env fixture swaps in the guarded one.
_REAL_DEFAULT_TRANSPORT = zc._default_transport

ATTACH_PATH = "/api/users/0/items"
PROBE_PATH = "/api/users/0/items"
COLLECTIONS_PATH = "/api/users/0/collections"
SAVE_PATH = "/connector/saveItems"

# Redirect targets that reach the authorize endpoint in a spelling a server could accept.
REDIRECT_TARGETS = [
    AUTHORIZE,
    AUTHORIZE + "/",
    "http://127.0.0.1:23119/api/local/authorize;x",
    "http://127.0.0.1:23119/api/local;x/authorize",
    "http://127.0.0.1:23119/api/local/authorize%3Fx",
    "http://127.0.0.1:23119/api/local\\authorize",
    "http://localhost:23119/api/local/AUTHORIZE?appName=Meridian",
]


class RedirectingZotero(urllib.request.BaseHandler):
    """A fake Zotero installed at the bottom of a real opener. It records every request it receives.

    ``redirects`` maps a request path to a Location. A request for a mapped path gets a 302 to that
    Location. Any other request gets a 200 with an empty JSON list. The handler runs ahead of urllib's
    own HTTP handler, so no socket is opened, and the real redirect handlers run on top of it.
    """

    handler_order = 100

    def __init__(self, redirects=None):
        self.redirects = dict(redirects or {})
        self.seen = []

    def http_open(self, req):
        parts = urllib.parse.urlsplit(req.full_url)
        self.seen.append({
            "method": req.get_method(), "url": req.full_url, "path": parts.path, "headers": dict(req.header_items()),
        })
        location = self.redirects.get(parts.path)
        headers = email.message.Message()
        if location is None:
            code, body = 200, b"[]"
            headers["Content-Type"] = "application/json"
        else:
            code, body = 302, b""
            headers["Location"] = location
        response = urllib.response.addinfourl(io.BytesIO(body), headers, req.full_url, code)
        response.msg = "OK" if location is None else "Found"
        return response


def _zotero_opener(guard, zotero):
    """An opener built the way the real transports build theirs, with the fake Zotero at the bottom."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), guard, zotero)


def _seen(zotero):
    return [(call["method"], call["path"]) for call in zotero.seen]


# F4: a request that carries the approval never follows a redirect off loopback.
# The fake Zotero sits under the real guard, so every hop is checked and no socket opens.

F4_BASE = "http://127.0.0.1:23119/api"
F4_OFF_LOOPBACK = "http://203.0.113.10:9/collect"


def test_a_redirect_off_loopback_raises_and_sends_no_approval_to_the_target(monkeypatch):
    zotero = RedirectingZotero({"/api/start": F4_OFF_LOOPBACK})
    monkeypatch.setattr(zc, "_TRANSPORT_OPENER", _zotero_opener(zc._LoopbackRedirectGuard(), zotero))
    with pytest.raises(Exception) as excinfo:
        _REAL_DEFAULT_TRANSPORT(2.0)("POST", F4_BASE + "/start", b"{}", {"Authorization": f"Bearer {ISSUED_KEY}"})
    assert type(excinfo.value).__name__ == "_RedirectRefused"
    assert ISSUED_KEY not in str(excinfo.value)
    assert _seen(zotero) == [("POST", "/api/start")]
    assert zotero.seen[0]["headers"]["Authorization"] == f"Bearer {ISSUED_KEY}"
    assert all(urllib.parse.urlsplit(call["url"]).hostname == "127.0.0.1" for call in zotero.seen)


def test_every_redirect_hop_is_checked_not_only_the_first(monkeypatch):
    zotero = RedirectingZotero({"/api/start": F4_BASE + "/hop", "/api/hop": F4_OFF_LOOPBACK})
    monkeypatch.setattr(zc, "_TRANSPORT_OPENER", _zotero_opener(zc._LoopbackRedirectGuard(), zotero))
    with pytest.raises(Exception) as excinfo:
        _REAL_DEFAULT_TRANSPORT(2.0)("POST", F4_BASE + "/start", b"{}", {"Authorization": f"Bearer {ISSUED_KEY}"})
    assert type(excinfo.value).__name__ == "_RedirectRefused"
    assert [call["path"] for call in zotero.seen] == ["/api/start", "/api/hop"]
    assert all(urllib.parse.urlsplit(call["url"]).hostname == "127.0.0.1" for call in zotero.seen)


def test_a_write_redirected_off_loopback_keeps_the_key_and_reports_error(env, monkeypatch):
    env.store.key = ISSUED_KEY
    zotero = RedirectingZotero({"/api/start": F4_OFF_LOOPBACK})
    monkeypatch.setattr(zc, "_TRANSPORT_OPENER", _zotero_opener(zc._LoopbackRedirectGuard(), zotero))
    result = zc.write_with_stored_approval(
        {"method": "POST", "url": F4_BASE + "/start", "payload": {}},
        transport=_REAL_DEFAULT_TRANSPORT(2.0), store=env.store,
    )
    assert result["status"] == "error"
    assert _seen(zotero) == [("POST", "/api/start")]
    assert env.store.key == ISSUED_KEY
    assert env.store.delete_calls == 0
    assert ISSUED_KEY not in json.dumps(result)


@pytest.mark.parametrize("target", REDIRECT_TARGETS)
def test_a_write_redirected_to_the_authorize_endpoint_is_never_followed(env, monkeypatch, target):
    env.store.key = ISSUED_KEY
    zotero = RedirectingZotero({ATTACH_PATH: target})
    monkeypatch.setattr(zc, "_TRANSPORT_OPENER", _zotero_opener(zc._LoopbackRedirectGuard(), zotero))
    result = zc.write_with_stored_approval(
        {"method": "POST", "url": zf.LOCAL_API + "/items", "payload": [{"a": 1}]},
        transport=_REAL_DEFAULT_TRANSPORT(2.0),
    )
    assert result["status"] == "error"
    assert _seen(zotero) == [("POST", ATTACH_PATH)]
    assert env.store.key == ISSUED_KEY
    assert ISSUED_KEY not in json.dumps(result)


@pytest.mark.parametrize("target", REDIRECT_TARGETS)
def test_a_filer_send_redirected_to_the_authorize_endpoint_is_never_followed(env, monkeypatch, target):
    env.store.key = ISSUED_KEY
    zotero = RedirectingZotero({ATTACH_PATH: target})
    monkeypatch.setattr(zf, "_OPENER", _zotero_opener(zf._StripKeyOnRedirect(), zotero))
    result = zc.send_with_stored_approval(
        "POST", zf.LOCAL_API + "/items", [{"a": 1}], header_transport=zf._urllib_transport_with_headers,
    )
    assert result["status"] == "error"
    assert _seen(zotero) == [("POST", ATTACH_PATH)]
    assert env.store.key == ISSUED_KEY
    assert ISSUED_KEY not in json.dumps(result)


@pytest.mark.parametrize("target", REDIRECT_TARGETS)
def test_a_connector_save_redirected_to_the_authorize_endpoint_is_never_followed(env, monkeypatch, target):
    # The connector save carries no approval. The authorize endpoint is still never reached through it.
    env.store.key = ISSUED_KEY
    zotero = RedirectingZotero({SAVE_PATH: target})
    monkeypatch.setattr(zf, "_OPENER", _zotero_opener(zf._StripKeyOnRedirect(), zotero))
    result = zf.save_items([{"title": "T"}])
    assert result["status"] == "error"
    assert _seen(zotero) == [("POST", SAVE_PATH)]
    assert env.store.key == ISSUED_KEY


@pytest.mark.parametrize("target", REDIRECT_TARGETS)
def test_a_status_probe_redirected_to_the_authorize_endpoint_is_never_followed(env, target):
    zotero = RedirectingZotero({PROBE_PATH: target})
    result = zc.status(opener=_zotero_opener(zc._LoopbackRedirectGuard(), zotero).open)
    assert result["status"] == "unreachable"
    assert _seen(zotero) == [("GET", PROBE_PATH)]


@pytest.mark.parametrize("target", REDIRECT_TARGETS)
def test_the_filer_server_id_probe_redirected_to_the_authorize_endpoint_is_never_followed(env, monkeypatch, target):
    zotero = RedirectingZotero({COLLECTIONS_PATH: target})
    monkeypatch.setattr(zf, "_OPENER", _zotero_opener(zf._StripKeyOnRedirect(), zotero))
    assert zf._probe_server_id(zf._urllib_transport_with_headers) is None
    assert _seen(zotero) == [("GET", COLLECTIONS_PATH)]


@pytest.mark.parametrize("target", REDIRECT_TARGETS)
def test_both_redirect_handlers_refuse_the_authorize_endpoint_with_or_without_a_key(target):
    plain = urllib.request.Request(SAVE_URL, data=b"[]", method="POST")
    keyed = urllib.request.Request(
        zf.LOCAL_API + "/items", data=b"[]", headers={"Authorization": f"Bearer {ISSUED_KEY}"}, method="POST",
    )
    for handler in (zc._LoopbackRedirectGuard(), zf._StripKeyOnRedirect()):
        assert handler.redirect_request(plain, None, 302, "Found", {}, target) is None
        assert handler.redirect_request(keyed, None, 302, "Found", {}, target) is None


@pytest.mark.parametrize(
    "url",
    [
        AUTHORIZE,
        AUTHORIZE + "/",
        "http://127.0.0.1:23119/api/local/authorize;x",
        "http://127.0.0.1:23119/api/local/authorize;",
        "http://127.0.0.1:23119/api/local;x/authorize",
        "http://127.0.0.1:23119/api/local;/authorize",
        "http://127.0.0.1:23119/api/local%3Bx/authorize",
        "http://127.0.0.1:23119/api/local/authorize%3Fx",
        "http://127.0.0.1:23119/api/local/authorize%3fx",
        "http://127.0.0.1:23119/api/local/authorize%23x",
        "http://127.0.0.1:23119/api/local/authorize?x=1",
        "http://127.0.0.1:23119/api/local/authorize#frag",
        "http://127.0.0.1:23119/api/local\\authorize",
        "http://127.0.0.1:23119/api\\local\\authorize",
        "http://127.0.0.1:23119/api/local%5Cauthorize",
        "http://127.0.0.1:23119/api/local%5cauthorize",
        "http://127.0.0.1:23119/api/local/authorize%00x",
        "http://127.0.0.1:23119/api/local/auth%0Aorize",
        "http://127.0.0.1:23119/api/local/x/../authorize",
        "http://127.0.0.1:23119/api/local/authorize/..",
        "http://127.0.0.1:23119/api/local/authorize/",
        "http://127.0.0.1:23119//api//local//authorize",
        "http://127.0.0.1:23119/api/users/0/../../local/authorize",
        "http://localhost:23119/api/local/authorize?appName=Meridian",
    ],
)
def test_targets_authorize_refuses_every_spelling_of_the_endpoint(url):
    assert zc._targets_authorize(url) is True


def _encoded_rounds(text, rounds):
    for _ in range(rounds):
        text = urllib.parse.quote(text, safe="")
    return text


@pytest.mark.parametrize("rounds", [5, 6, 8])
def test_targets_authorize_decodes_deeper_than_the_old_four_rounds(rounds):
    assert zc._targets_authorize("http://127.0.0.1:23119/api/" + _encoded_rounds("local/authorize", rounds)) is True


@pytest.mark.parametrize("path", ["local/authorize", "local/items"])
def test_a_path_still_encoded_past_the_decode_depth_is_refused(path):
    # Fail closed: a path that does not finish decoding inside the depth limit is refused, never guessed at.
    assert zc._targets_authorize("http://127.0.0.1:23119/api/" + _encoded_rounds(path, 9)) is True


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:23119/api/users/0/items?limit=1",
        "http://127.0.0.1:23119/connector/saveItems",
        "http://127.0.0.1:23119/api/local/status",
        "http://127.0.0.1:23119/api/local/authorized",
        "http://127.0.0.1:23119/api/local/authorize-me",
        "http://127.0.0.1:23119/api/users/0/items/local",
        "http://127.0.0.1:23119/",
    ],
)
def test_targets_authorize_allows_the_other_local_api_paths(url):
    assert zc._targets_authorize(url) is False


def test_status_never_probes_a_base_url_that_targets_the_authorize_endpoint(env, monkeypatch):
    monkeypatch.setenv("MERIDIAN_ZOTERO_API_URL", "http://127.0.0.1:23119/api/local/authorize")
    opener = probe_opener()
    assert zc.status(opener=opener)["status"] == "unreachable"
    assert opener.calls == []


def test_the_write_path_probe_never_targets_the_authorize_endpoint(env, monkeypatch):
    env.store.key = ISSUED_KEY
    monkeypatch.setenv("MERIDIAN_ZOTERO_API_URL", "http://127.0.0.1:23119/api/local/authorize")
    transport = Scripted((403, "denied"))
    result = zc.write_with_stored_approval(_write_request(), transport=transport)
    assert result["status"] == "error"
    assert [call["method"] for call in transport.calls] == ["POST"]
    assert env.store.key == ISSUED_KEY


def test_only_disconnect_removes_the_stored_approval():
    """The store's delete() is reached from disconnect() alone. connect() replaces the key and never removes it."""
    tree = ast.parse(Path(zc.__file__).read_text(encoding="utf-8"), filename=zc.__file__)
    callers = set()
    for func in ast.walk(tree):
        if not isinstance(func, ast.FunctionDef):
            continue
        for node in ast.walk(func):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("delete", "delete_password")
            ):
                callers.add(func.name)
    assert callers == {"delete", "disconnect"}


# ---------------------------------------------------------------------------
# Authority spoofing: a '@' or a backslash in the authority.
#
# urllib dials the raw authority, while urlsplit().hostname reads the text after the last '@'.
# So "http://evil.example\@127.0.0.1:23119/x" reports the loopback host, yet the request is
# aimed at evil.example. Every such URL is refused before the host check.
# ---------------------------------------------------------------------------

SPOOF = "http://evil.example\\@127.0.0.1:23119/x"
SPOOF_BASE = "http://evil.example\\@127.0.0.1:23119/api"
SPOOF_URLS = [
    SPOOF,
    "http://evil.example@127.0.0.1:23119/x",
    "http://evil.example%5C@127.0.0.1:23119/x",
]


def test_the_spoof_url_reads_as_loopback_to_the_old_hostname_check():
    # The bypass in one line: the hostname urlsplit reports is the loopback address.
    assert urllib.parse.urlsplit(SPOOF).hostname == "127.0.0.1"


@pytest.mark.parametrize("url", SPOOF_URLS)
def test_require_loopback_refuses_an_authority_with_an_at_sign_or_backslash(url):
    assert zc._require_loopback(url) is False


def test_require_loopback_still_accepts_the_plain_loopback_authorize_url():
    assert zc._require_loopback(AUTHORIZE) is True


@pytest.mark.parametrize("url", SPOOF_URLS)
def test_loopback_redirect_guard_refuses_an_authority_spoof_as_off_loopback(url):
    plain = urllib.request.Request(SAVE_URL, data=b"[]", method="POST")
    with pytest.raises(zc._RedirectRefused):
        zc._LoopbackRedirectGuard().redirect_request(plain, None, 302, "Found", {}, url)


@pytest.mark.parametrize("target", [AUTHORIZE, SPOOF])
def test_no_authorize_redirect_handler_stands_the_reply_for_authorize_and_spoof_targets(target):
    plain = urllib.request.Request(PROBE, method="GET")
    assert zc._NoAuthorizeRedirect().redirect_request(plain, None, 302, "Found", {}, target) is None


def test_no_authorize_redirect_handler_still_follows_an_ordinary_loopback_redirect():
    plain = urllib.request.Request(PROBE, method="GET")
    follow = zc._NoAuthorizeRedirect().redirect_request(
        plain, None, 302, "Found", {}, "http://127.0.0.1:23119/api/elsewhere",
    )
    assert isinstance(follow, urllib.request.Request)


def test_a_write_redirected_to_a_backslash_authority_is_refused_and_never_requested(env, monkeypatch):
    env.store.key = ISSUED_KEY
    zotero = RedirectingZotero({ATTACH_PATH: SPOOF})
    monkeypatch.setattr(zc, "_TRANSPORT_OPENER", _zotero_opener(zc._LoopbackRedirectGuard(), zotero))
    result = zc.write_with_stored_approval(
        {"method": "POST", "url": zf.LOCAL_API + "/items", "payload": [{"a": 1}]},
        transport=_REAL_DEFAULT_TRANSPORT(2.0),
    )
    assert result["status"] == "error"
    assert _seen(zotero) == [("POST", ATTACH_PATH)]
    assert all("evil.example" not in call["url"] for call in zotero.seen)
    assert env.store.key == ISSUED_KEY
    assert ISSUED_KEY not in json.dumps(result)


def test_a_status_probe_redirected_to_a_backslash_authority_is_not_followed(env):
    zotero = RedirectingZotero({PROBE_PATH: SPOOF})
    result = zc.status(opener=_zotero_opener(zc._LoopbackRedirectGuard(), zotero).open)
    assert result["status"] == "unreachable"
    assert _seen(zotero) == [("GET", PROBE_PATH)]


def test_a_filer_send_redirected_to_a_backslash_authority_keeps_the_key_off_the_target(env, monkeypatch):
    # The keyed filer guard would otherwise follow this hop: the spoof's host reads as loopback.
    env.store.key = ISSUED_KEY
    zotero = RedirectingZotero({ATTACH_PATH: SPOOF})
    monkeypatch.setattr(zf, "_OPENER", _zotero_opener(zf._StripKeyOnRedirect(), zotero))
    result = zc.send_with_stored_approval(
        "POST", zf.LOCAL_API + "/items", [{"a": 1}], header_transport=zf._urllib_transport_with_headers,
    )
    assert result["status"] == "error"
    assert _seen(zotero) == [("POST", ATTACH_PATH)]
    keyed = [call for call in zotero.seen if ISSUED_KEY in json.dumps(call["headers"])]
    assert [call["path"] for call in keyed] == [ATTACH_PATH]
    assert env.store.key == ISSUED_KEY


def test_a_connector_save_redirected_to_a_backslash_authority_is_not_followed(env, monkeypatch):
    env.store.key = ISSUED_KEY
    zotero = RedirectingZotero({SAVE_PATH: SPOOF})
    monkeypatch.setattr(zf, "_OPENER", _zotero_opener(zf._StripKeyOnRedirect(), zotero))
    result = zf.save_items([{"title": "T"}])
    assert result["status"] == "error"
    assert _seen(zotero) == [("POST", SAVE_PATH)]
    assert env.store.key == ISSUED_KEY


def test_the_filer_probe_redirected_to_a_backslash_authority_is_not_followed(env, monkeypatch):
    zotero = RedirectingZotero({COLLECTIONS_PATH: SPOOF})
    monkeypatch.setattr(zf, "_OPENER", _zotero_opener(zf._StripKeyOnRedirect(), zotero))
    assert zf._probe_server_id(zf._urllib_transport_with_headers) is None
    assert _seen(zotero) == [("GET", COLLECTIONS_PATH)]


def test_connect_refuses_a_backslash_authority_base_before_any_request(env):
    transport = Scripted()
    result = zc.connect(base_url=SPOOF_BASE, transport=transport, store=env.store, state_path=env.state_path)
    assert result["status"] == "error"
    assert transport.calls == []
    assert not env.state_path.exists()
    assert env.store.key is None


def test_status_refuses_a_backslash_authority_base_before_any_probe(env):
    opener = probe_opener()
    assert zc.status(base_url=SPOOF_BASE, opener=opener)["status"] == "unreachable"
    assert opener.calls == []


def test_write_refuses_a_backslash_authority_url_and_keeps_the_key(env):
    env.store.key = ISSUED_KEY
    transport = Scripted()
    result = zc.write_with_stored_approval(
        {"method": "POST", "url": SPOOF_BASE + "/users/0/items", "payload": {}}, transport=transport,
    )
    assert result["status"] == "error"
    assert transport.calls == []
    assert env.store.key == ISSUED_KEY
    assert ISSUED_KEY not in json.dumps(result)


def test_send_refuses_a_backslash_authority_url_and_keeps_the_key(env):
    env.store.key = ISSUED_KEY
    header = HeaderScripted()
    result = zc.send_with_stored_approval("POST", SPOOF_BASE + "/users/0/items", [], header_transport=header)
    assert result["status"] == "error"
    assert header.calls == []
    assert env.store.key == ISSUED_KEY


# The reachability probe after a 403 reads its base from MERIDIAN_ZOTERO_API_URL. That base is
# checked as a loopback URL before any request, so a spoofed base gets no probe at all.

SPOOFED_PROBE_BASES = [
    "http://203.0.113.10:23119/api",
    "http://evil.example/api",
    "http://127.0.0.1@203.0.113.10:23119/api",
    "http://evil.example\\@127.0.0.1:23119/api",
]


@pytest.mark.parametrize("base", SPOOFED_PROBE_BASES)
def test_probe_refuses_a_spoofed_base_before_any_request(env, monkeypatch, base):
    monkeypatch.setenv("MERIDIAN_ZOTERO_API_URL", base)
    transport = Scripted()
    assert zc._probe_local_api(transport) == "error"
    assert transport.calls == []


@pytest.mark.parametrize("base", SPOOFED_PROBE_BASES)
def test_write_sends_no_probe_when_the_probe_base_is_spoofed(env, monkeypatch, base):
    env.store.key = ISSUED_KEY
    monkeypatch.setenv("MERIDIAN_ZOTERO_API_URL", base)
    transport = Scripted((403, "forbidden"))
    result = zc.write_with_stored_approval(
        {"method": "POST", "url": zf.LOCAL_API + "/items", "payload": []}, transport=transport, store=env.store,
    )
    assert result == zc._result("error", http_status=403)
    assert [call["url"] for call in transport.calls] == [zf.LOCAL_API + "/items"]
    assert env.store.key == ISSUED_KEY
    assert ISSUED_KEY not in json.dumps(result)


@pytest.mark.parametrize("base", SPOOFED_PROBE_BASES)
def test_filer_send_sends_no_probe_when_the_probe_base_is_spoofed(env, monkeypatch, base):
    env.store.key = ISSUED_KEY
    monkeypatch.setenv("MERIDIAN_ZOTERO_API_URL", base)
    header = HeaderScripted((403, "forbidden", {}))
    result = zc.send_with_stored_approval("POST", zf.LOCAL_API + "/items", [], header_transport=header, store=env.store)
    assert result == zc._result("error", http_status=403)
    assert [call["url"] for call in header.calls] == [zf.LOCAL_API + "/items"]
    assert env.store.key == ISSUED_KEY
    assert ISSUED_KEY not in json.dumps(result)
