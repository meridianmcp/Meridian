"""Coverage tests for meridian/integrations/langgraph.py — the MeridianCheckpointer.

CI-SAFE: every test injects a MOCK httpx.AsyncClient (via monkeypatching
``httpx.AsyncClient`` on the module) — nothing here ever opens a real socket.

Drives the checkpointer's internal helpers (``_ensure_session``, ``_log_task``,
``_request_hitl``) and the LangGraph BaseCheckpointSaver surface (``put`` with
and without a HITL interrupt, the None/empty get/list stubs, and the sync/async
aliases) directly via asyncio.run.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from meridian.integrations.langgraph import MeridianCheckpointer


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Mock httpx.AsyncClient — async context manager that records requests
# ---------------------------------------------------------------------------

class _MockResponse:
    def __init__(self, status_code=200, json_body=None):
        self.status_code = status_code
        self._json_body = json_body or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "boom", request=None, response=None  # type: ignore[arg-type]
            )

    def json(self):
        return self._json_body


class _MockClient:
    """Async-context-manager stand-in for httpx.AsyncClient.

    ``handler(url, headers, json)`` returns a _MockResponse (or raises to
    simulate a transport error). Every POST is recorded on ``requests``.
    Shares the request log with the factory so tests can inspect it.
    """

    def __init__(self, handler, requests):
        self._handler = handler
        self.requests = requests

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, headers=None, json=None):
        self.requests.append({"url": url, "headers": headers or {}, "json": json or {}})
        return self._handler(url, headers or {}, json or {})


def _install_mock_httpx(monkeypatch, handler):
    """Patch httpx.AsyncClient on the module so no real socket is opened.

    Returns the shared list of recorded requests.
    """
    from meridian.integrations import langgraph as lg

    requests: list[dict] = []

    def _factory(*_a, **_k):
        return _MockClient(handler, requests)

    monkeypatch.setattr(lg.httpx, "AsyncClient", _factory)
    return requests


# ---------------------------------------------------------------------------
# __init__ — token → Authorization header, url normalisation
# ---------------------------------------------------------------------------

def test_init_sets_bearer_header_and_strips_trailing_slash():
    cp = MeridianCheckpointer(
        project_id="pid", api_url="http://localhost:7878/", api_token="tok123"
    )
    assert cp.api_url == "http://localhost:7878"  # trailing slash stripped
    assert cp.headers["Authorization"] == "Bearer tok123"
    assert cp._session_id is None


def test_init_without_token_has_empty_headers():
    cp = MeridianCheckpointer(project_id="pid", api_url="http://localhost:7878")
    assert cp.headers == {}


# ---------------------------------------------------------------------------
# _ensure_session — register (73-75) + cached early-return (62)
# ---------------------------------------------------------------------------

def test_ensure_session_registers_and_returns_id(monkeypatch):
    def handler(url, headers, json):
        assert url.endswith("/sessions/register")
        assert json["project_id"] == "pid"
        assert json["name"] == "langgraph-worker"
        assert json["agent_framework"] == "langgraph"
        assert headers["Authorization"] == "Bearer tok"
        return _MockResponse(json_body={"id": "sess-42"})

    requests = _install_mock_httpx(monkeypatch, handler)
    cp = MeridianCheckpointer(project_id="pid", api_url="http://x", api_token="tok")
    sid = _run(cp._ensure_session())
    assert sid == "sess-42"
    assert cp._session_id == "sess-42"
    assert len(requests) == 1


def test_ensure_session_cached_returns_early_without_http(monkeypatch):
    """Line 62 — a set _session_id short-circuits before any HTTP call."""
    def handler(url, headers, json):  # pragma: no cover - must not be called
        raise AssertionError("HTTP must not be called when session is cached")

    _install_mock_httpx(monkeypatch, handler)
    cp = MeridianCheckpointer(project_id="pid", api_url="http://x")
    cp._session_id = "already-here"
    assert _run(cp._ensure_session()) == "already-here"


def test_ensure_session_concurrent_calls_register_exactly_once(monkeypatch):
    """Regression: two concurrent _ensure_session() calls on the SAME
    checkpointer instance (e.g. two LangGraph nodes logging around the same
    time) must not both register a session.

    Before the lock fix, the check ("if self._session_id: return") and the
    write ("self._session_id = resp.json()['id']") were separated by an
    `await http.post(...)` with nothing serializing concurrent callers —
    two callers racing a cold session both registered, and the loser's
    session_id was silently discarded, leaving an orphaned session row with
    no reference anywhere to clean it up. Same class of bug as
    _deps._open_tenant_db_by_id / doc_store.open_doc_store_for.
    """
    call_count = {"n": 0}

    async def _slow_post(url, headers=None, json=None):
        call_count["n"] += 1
        await asyncio.sleep(0.02)
        return _MockResponse(json_body={"id": f"sess-{call_count['n']}"})

    class _SlowMockClient(_MockClient):
        async def post(self, url, headers=None, json=None):
            self.requests.append({"url": url, "headers": headers or {}, "json": json or {}})
            return await _slow_post(url, headers, json)

    from meridian.integrations import langgraph as lg

    requests: list[dict] = []
    monkeypatch.setattr(lg.httpx, "AsyncClient", lambda *a, **k: _SlowMockClient(None, requests))

    cp = MeridianCheckpointer(project_id="pid", api_url="http://x", api_token="tok")

    async def _run_both():
        return await asyncio.gather(cp._ensure_session(), cp._ensure_session())

    sid1, sid2 = _run(_run_both())

    assert call_count["n"] == 1, (
        "two concurrent _ensure_session() calls must register exactly once, "
        "not race to register two sessions"
    )
    assert sid1 == sid2 == cp._session_id


def test_ensure_session_raises_for_status_on_error(monkeypatch):
    """Line 73 — raise_for_status propagates an HTTP error from register."""
    def handler(url, headers, json):
        return _MockResponse(status_code=500)

    _install_mock_httpx(monkeypatch, handler)
    cp = MeridianCheckpointer(project_id="pid", api_url="http://x")
    with pytest.raises(httpx.HTTPStatusError):
        _run(cp._ensure_session())
    # Failed register does not cache a bogus session id.
    assert cp._session_id is None


# ---------------------------------------------------------------------------
# _log_task — posts to /tasks (80-81), swallows exceptions (91-92)
# ---------------------------------------------------------------------------

def test_log_task_posts_task_with_session(monkeypatch):
    def handler(url, headers, json):
        if url.endswith("/sessions/register"):
            return _MockResponse(json_body={"id": "sess-9"})
        assert url.endswith("/tasks")
        assert json["session_id"] == "sess-9"
        assert json["project_id"] == "pid"
        assert json["description"] == "did a thing"
        assert json["status"] == "done"
        return _MockResponse(json_body={"ok": True})

    requests = _install_mock_httpx(monkeypatch, handler)
    cp = MeridianCheckpointer(project_id="pid", api_url="http://x")
    _run(cp._log_task("did a thing"))
    urls = [r["url"] for r in requests]
    assert any(u.endswith("/sessions/register") for u in urls)
    assert any(u.endswith("/tasks") for u in urls)


def test_log_task_custom_status(monkeypatch):
    captured = {}

    def handler(url, headers, json):
        if url.endswith("/sessions/register"):
            return _MockResponse(json_body={"id": "s1"})
        captured["status"] = json["status"]
        return _MockResponse(json_body={})

    _install_mock_httpx(monkeypatch, handler)
    cp = MeridianCheckpointer(project_id="pid", api_url="http://x")
    _run(cp._log_task("desc", status="in_progress"))
    assert captured["status"] == "in_progress"


def test_log_task_swallows_exceptions(monkeypatch):
    """Lines 91-92 — a transport error during logging never propagates."""
    def handler(url, headers, json):
        raise RuntimeError("network down")

    _install_mock_httpx(monkeypatch, handler)
    cp = MeridianCheckpointer(project_id="pid", api_url="http://x")
    # Must not raise.
    _run(cp._log_task("desc"))


# ---------------------------------------------------------------------------
# _request_hitl — posts blocking HITL (95-105), swallows exceptions (106-107)
# ---------------------------------------------------------------------------

def test_request_hitl_posts_blocking_question(monkeypatch):
    def handler(url, headers, json):
        assert url.endswith("/projects/pid/hitl")
        assert json["question"] == "approve?"
        assert json["context"] == "node=n step=1"
        assert json["urgency"] == "blocking"
        return _MockResponse(json_body={"id": "h1"})

    requests = _install_mock_httpx(monkeypatch, handler)
    cp = MeridianCheckpointer(project_id="pid", api_url="http://x")
    _run(cp._request_hitl("approve?", context="node=n step=1"))
    assert len(requests) == 1
    assert requests[0]["url"].endswith("/projects/pid/hitl")


def test_request_hitl_swallows_exceptions(monkeypatch):
    """Lines 106-107 — HITL posting failures never propagate."""
    def handler(url, headers, json):
        raise RuntimeError("hitl endpoint down")

    _install_mock_httpx(monkeypatch, handler)
    cp = MeridianCheckpointer(project_id="pid", api_url="http://x")
    _run(cp._request_hitl("q"))  # must not raise


# ---------------------------------------------------------------------------
# put — logs task; surfaces HITL on __interrupt__ pending send (126-127)
# ---------------------------------------------------------------------------

def test_put_logs_task_no_interrupt(monkeypatch):
    def handler(url, headers, json):
        if url.endswith("/sessions/register"):
            return _MockResponse(json_body={"id": "s"})
        return _MockResponse(json_body={})

    requests = _install_mock_httpx(monkeypatch, handler)
    cp = MeridianCheckpointer(project_id="pid", api_url="http://x")
    config = {"configurable": {"thread_id": "node-A"}}
    checkpoint = {"pending_sends": []}
    metadata = {"step": 3}
    ret = _run(cp.put(config, checkpoint, metadata, {}))
    assert ret is config  # put returns the config unchanged
    task_posts = [r for r in requests if r["url"].endswith("/tasks")]
    assert task_posts
    assert "[langgraph] node-A — step 3" == task_posts[0]["json"]["description"]
    # No HITL was raised.
    assert not any(r["url"].endswith("/hitl") for r in requests)


def test_put_surfaces_hitl_on_interrupt(monkeypatch):
    """Lines 124-130 — a pending send with __interrupt__ fires _request_hitl."""
    def handler(url, headers, json):
        if url.endswith("/sessions/register"):
            return _MockResponse(json_body={"id": "s"})
        return _MockResponse(json_body={})

    requests = _install_mock_httpx(monkeypatch, handler)
    cp = MeridianCheckpointer(project_id="pid", api_url="http://x")
    config = {"configurable": {"thread_id": "node-B"}}
    checkpoint = {
        "pending_sends": [
            "not-a-dict",  # skipped by the isinstance guard
            {"no_interrupt": True},  # dict but no __interrupt__ key
            {"__interrupt__": True, "value": "Need human sign-off"},
        ]
    }
    metadata = {"step": 7}
    _run(cp.put(config, checkpoint, metadata, {}))
    hitls = [r for r in requests if r["url"].endswith("/hitl")]
    assert len(hitls) == 1
    assert hitls[0]["json"]["question"] == "Need human sign-off"
    assert hitls[0]["json"]["context"] == "node=node-B step=7"


def test_put_interrupt_default_question_and_unknown_node(monkeypatch):
    """Interrupt dict without a value → default question; missing thread_id/step
    fall back to 'unknown-node' / '?'."""
    def handler(url, headers, json):
        if url.endswith("/sessions/register"):
            return _MockResponse(json_body={"id": "s"})
        return _MockResponse(json_body={})

    requests = _install_mock_httpx(monkeypatch, handler)
    cp = MeridianCheckpointer(project_id="pid", api_url="http://x")
    checkpoint = {"pending_sends": [{"__interrupt__": True}]}
    _run(cp.put({}, checkpoint, {}, {}))
    hitls = [r for r in requests if r["url"].endswith("/hitl")]
    assert len(hitls) == 1
    assert hitls[0]["json"]["question"] == "Graph interrupted — awaiting human input"
    assert hitls[0]["json"]["context"] == "node=unknown-node step=?"


def test_put_handles_missing_pending_sends(monkeypatch):
    """checkpoint.get('pending_sends') is None → `or []` guard yields no HITL."""
    def handler(url, headers, json):
        if url.endswith("/sessions/register"):
            return _MockResponse(json_body={"id": "s"})
        return _MockResponse(json_body={})

    requests = _install_mock_httpx(monkeypatch, handler)
    cp = MeridianCheckpointer(project_id="pid", api_url="http://x")
    _run(cp.put({"configurable": {"thread_id": "n"}}, {}, {"step": 1}, {}))
    assert not any(r["url"].endswith("/hitl") for r in requests)


# ---------------------------------------------------------------------------
# get / get_tuple / list — None / empty async-generator stubs (135, 138, 148)
# ---------------------------------------------------------------------------

def test_get_returns_none():
    cp = MeridianCheckpointer(project_id="pid", api_url="http://x")
    assert _run(cp.get({})) is None


def test_get_tuple_returns_none():
    cp = MeridianCheckpointer(project_id="pid", api_url="http://x")
    assert _run(cp.get_tuple({})) is None


def test_list_is_empty_async_generator():
    cp = MeridianCheckpointer(project_id="pid", api_url="http://x")

    async def _collect():
        return [x async for x in cp.list({}, filter=None, before=None, limit=5)]

    assert _run(_collect()) == []


# ---------------------------------------------------------------------------
# Sync + async aliases (153, 157, 160, 163, 166)
# ---------------------------------------------------------------------------

def test_put_writes_is_noop():
    cp = MeridianCheckpointer(project_id="pid", api_url="http://x")
    assert cp.put_writes("a", b="c") is None  # line 153


def test_aput_delegates_to_put(monkeypatch):
    def handler(url, headers, json):
        if url.endswith("/sessions/register"):
            return _MockResponse(json_body={"id": "s"})
        return _MockResponse(json_body={})

    _install_mock_httpx(monkeypatch, handler)
    cp = MeridianCheckpointer(project_id="pid", api_url="http://x")
    config = {"configurable": {"thread_id": "n"}}
    ret = _run(cp.aput(config, {"pending_sends": []}, {"step": 1}, {}))
    assert ret is config  # line 157 delegates to put(), which returns config


def test_aget_delegates_to_get():
    cp = MeridianCheckpointer(project_id="pid", api_url="http://x")
    assert _run(cp.aget({})) is None  # line 160


def test_aget_tuple_delegates_to_get_tuple():
    cp = MeridianCheckpointer(project_id="pid", api_url="http://x")
    assert _run(cp.aget_tuple({})) is None  # line 163


def test_alist_returns_async_iterator():
    cp = MeridianCheckpointer(project_id="pid", api_url="http://x")

    async def _collect():
        it = await cp.alist({})  # line 166 — returns the async generator
        return [x async for x in it]

    assert _run(_collect()) == []
