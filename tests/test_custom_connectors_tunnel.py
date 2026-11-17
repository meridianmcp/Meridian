"""Tests for the owner's custom connectors over the tunnel (meridian/custom_connectors.py).

Offline throughout: no WebSocket, no process, no network. The tunnel is stubbed
the same way tests/test_mcp_tool_groups.py stubs it, and the client relay gets
a fake HTTP client. The checks that matter most are the opt-in gate (off means
nothing is listed or callable), the native-name guard, the tool-group filter,
https-only URLs, and that no secret value reaches a log, a print or a response.
"""
from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path

import pytest

import meridian.server  # noqa: F401 — import first to avoid handler/server import cycle
from meridian import custom_connectors as cc
from meridian import tunnel_client as tc
from meridian.mcp import handler as mh
from meridian.mcp import tool_groups as tg
from meridian.routes import tunnel as tn

SECRET = "sk-SECRET-VALUE-9f3c"
TENANT = "tenant-custom-1"


def _write_settings(tmp_path: Path, data) -> Path:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _entry(**overrides) -> dict:
    base = {
        "name": "notes",
        "kind": "copy",
        "transport": "stdio",
        "command": "C:/tools/notes.exe",
        "args": ["--serve"],
        "enabled": True,
        "tunnel_exposed": True,
    }
    base.update(overrides)
    return base


def _http_entry(name: str = "remote", url: str = "https://example.com/mcp", **overrides) -> dict:
    base = {
        "name": name,
        "kind": "linked",
        "transport": "http",
        "url": url,
        "enabled": True,
        "tunnel_exposed": True,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Config: the opt-in, the per-entry flags, and the desktop's checks
# ---------------------------------------------------------------------------

def test_opt_in_is_off_by_default_and_nothing_else_is_read(tmp_path):
    path = _write_settings(tmp_path, {"custom_connectors": [_entry()]})
    config = cc.load_config(path)
    assert config.opt_in is False
    assert config.connectors == ()


@pytest.mark.parametrize("value", ["true", 1, "yes", None, [True]])
def test_opt_in_must_be_literally_true(tmp_path, value):
    path = _write_settings(tmp_path, {"custom_tunnel_opt_in": value, "custom_connectors": [_entry()]})
    config = cc.load_config(path)
    assert config.opt_in is False
    assert config.connectors == ()


def test_missing_settings_file_means_opt_in_off(tmp_path):
    config = cc.load_config(tmp_path / "does-not-exist.json")
    assert config == cc.CustomConnectorConfig(opt_in=False)


def test_unreadable_json_serves_nothing_and_does_not_echo_it(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(f'{{"custom_tunnel_opt_in": true, "token": "{SECRET}"', encoding="utf-8")
    config = cc.load_config(path)
    assert config.opt_in is False
    assert config.connectors == ()
    assert SECRET not in " ".join(config.notes)


def test_only_enabled_and_tunnel_exposed_entries_are_served(tmp_path):
    path = _write_settings(tmp_path, {
        "custom_tunnel_opt_in": True,
        "custom_connectors": [
            _entry(name="on-both"),
            _entry(name="not-exposed", tunnel_exposed=False),
            _entry(name="disabled", enabled=False),
            _entry(name="neither", enabled=False, tunnel_exposed=False),
        ],
    })
    config = cc.load_config(path)
    assert [c.name for c in config.connectors] == ["on-both"]


def test_enabled_must_be_literal_true(tmp_path):
    path = _write_settings(tmp_path, {
        "custom_tunnel_opt_in": True,
        "custom_connectors": [_entry(name="truthy", enabled="yes")],
    })
    assert cc.load_config(path).connectors == ()


def test_http_non_https_url_is_refused(tmp_path):
    secret_url = "http://example.com/mcp?key=" + SECRET
    path = _write_settings(tmp_path, {
        "custom_tunnel_opt_in": True,
        "custom_connectors": [_http_entry(url=secret_url)],
    })
    config = cc.load_config(path)
    assert config.connectors == ()
    assert any("remote" in note and "not served" in note for note in config.notes)
    assert SECRET not in " ".join(config.notes)


@pytest.mark.parametrize("url", [
    "ftp://example.com/mcp",
    "file:///etc/passwd",
    "https:///no-host",
    "https://user:pass@example.com/mcp",
    "https://example.com/mcp with space",
])
def test_bad_urls_are_refused(url):
    with pytest.raises(cc.ConnectorRefused):
        cc.connector_from_entry(_http_entry(url=url))


@pytest.mark.parametrize("url", [
    "https://example.com/mcp",
    "http://127.0.0.1:8900/mcp",
    "http://localhost:8900/mcp",
    "http://[::1]:8900/mcp",
])
def test_https_or_loopback_http_is_allowed(url):
    connector = cc.connector_from_entry(_http_entry(url=url))
    assert connector.url == url


def test_http_to_a_remote_host_is_refused_even_on_a_loopback_lookalike():
    with pytest.raises(cc.ConnectorRefused):
        cc.connector_from_entry(_http_entry(url="http://127.0.0.1.example.com/mcp"))


@pytest.mark.parametrize("command", [
    "notes.exe; rm -rf /",
    "notes.exe | tee x",
    "notes.exe $(whoami)",
    "notes.exe `id`",
    "notes (x86).exe",
    "notes.exe > out",
])
def test_shell_metacharacters_in_command_are_refused(command):
    with pytest.raises(cc.ConnectorRefused):
        cc.connector_from_entry(_entry(command=command))


def test_shell_metacharacters_in_an_argument_are_refused():
    with pytest.raises(cc.ConnectorRefused):
        cc.connector_from_entry(_entry(args=["--token", "a;b"]))


@pytest.mark.parametrize("name", ["p0", "P1", "p3", "my_tools", "1abc", "has space", "x" * 41, ""])
def test_names_the_tunnel_cannot_use_are_refused(name):
    with pytest.raises(cc.ConnectorRefused):
        cc.connector_from_entry(_entry(name=name))


def test_duplicate_names_keep_the_first_entry(tmp_path):
    path = _write_settings(tmp_path, {
        "custom_tunnel_opt_in": True,
        "custom_connectors": [_entry(name="Notes", args=["first"]), _entry(name="notes", args=["second"])],
    })
    config = cc.load_config(path)
    assert [c.name for c in config.connectors] == ["Notes"]
    assert config.connectors[0].args == ("first",)
    assert any("same name" in note for note in config.notes)


def test_headers_field_is_never_read_and_never_reprd():
    entry = _http_entry()
    entry["headers"] = {"Authorization": f"Bearer {SECRET}"}
    connector = cc.connector_from_entry(entry)
    assert not hasattr(connector, "headers")
    assert SECRET not in repr(connector)
    assert SECRET not in json.dumps(connector.__dict__, default=str)


def test_repr_hides_command_args_and_url():
    stdio = cc.connector_from_entry(_entry(args=[f"--key={SECRET}"]))
    http = cc.connector_from_entry(_http_entry(url=f"https://example.com/mcp?key={SECRET}"))
    assert SECRET not in repr(stdio)
    assert SECRET not in repr(http)


def test_an_entry_that_is_not_an_object_is_noted_not_served(tmp_path):
    path = _write_settings(tmp_path, {"custom_tunnel_opt_in": True, "custom_connectors": ["junk", _entry()]})
    config = cc.load_config(path)
    assert [c.name for c in config.connectors] == ["notes"]
    assert any("not a connector object" in note for note in config.notes)


def test_settings_path_follows_the_desktop_rule(tmp_path):
    assert cc.settings_path({"LOCALAPPDATA": "C:/L"}) == Path("C:/L") / "meridian-desktop" / "settings.json"
    assert cc.settings_path({}, home=tmp_path) == tmp_path / ".meridian-desktop" / "settings.json"


# ---------------------------------------------------------------------------
# Names and the announcement
# ---------------------------------------------------------------------------

def test_advertised_tool_name_is_connector_prefixed():
    assert cc.custom_tool_name("notes", "search") == "custom-notes__search"
    assert cc.custom_tool_name("notes", "x" * 80) is None


@pytest.mark.parametrize("name, shaped", [
    ("custom-notes__search", True),
    ("custom-p0__probe", True),
    ("custom-notes__", False),
    ("custom-notes_x__search", False),
    ("log_task", False),
    ("unknown__thing", False),
    ("", False),
    (None, False),
])
def test_custom_tool_name_shape(name, shaped):
    assert cc.is_custom_connector_tool_name(name) is shaped


def test_announcement_needs_a_literal_opt_in_and_checked_names():
    assert cc.announced_names({"opt_in": False, "connectors": [{"name": "notes"}]}) == ()
    assert cc.announced_names({"opt_in": "true", "connectors": [{"name": "notes"}]}) == ()
    names = cc.announced_names({
        "opt_in": True,
        "connectors": [{"name": "notes"}, {"name": "p0"}, {"name": "bad_name"}, {"name": "NOTES"}, "junk"],
    })
    assert names == ("notes",)


# ---------------------------------------------------------------------------
# Tool-group filter: custom tools sit in the existing 'custom' group
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tool, group", [
    ("custom-notes__search", "custom"),
    ("custom-p0__probe", "custom"),
    ("custom-notes_x__search", None),
    ("unknown__thing", None),
    ("log_task", None),
])
def test_custom_connector_tools_group_as_custom(tool, group):
    assert tg.group_of_tool(tool) == group


def test_custom_group_off_refuses_custom_connector_tools():
    f = tg.parse_tool_filter({"groups": {"custom": False}})
    assert f.disabled_reason("custom-notes__search") is not None
    assert f.disabled_reason("log_task") is None


# ---------------------------------------------------------------------------
# Server: announcement, listing, and calls (tunnel stubbed)
# ---------------------------------------------------------------------------

class FakeConnectors:
    """Stands in for routes/tunnel._tunnel_jsonrpc: one fake tools/list and tools/call per label."""

    def __init__(self, tools_by_label: dict[str, list[dict]], call_result: dict | None = None,
                 call_error: dict | None = None) -> None:
        self.tools_by_label = tools_by_label
        self.call_result = call_result
        self.call_error = call_error
        self.calls: list[tuple[str, str, dict]] = []

    async def __call__(self, tenant_id, label, method, params, repo_path=None):
        self.calls.append((label, method, params or {}))
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": "x", "result": {"tools": self.tools_by_label.get(label, [])}}
        if self.call_error is not None:
            return {"jsonrpc": "2.0", "id": "x", "error": self.call_error}
        return {"jsonrpc": "2.0", "id": "x", "result": self.call_result or {"content": [{"type": "text", "text": "ok"}]}}


@pytest.fixture(autouse=True)
def _clean_cx_state(monkeypatch):
    for registry in (tn._tunnel_cx_sockets, tn._tenant_custom_connectors, tn._tunnel_custom_tool_routes, tn._tunnel_tool_routes):
        registry.clear()
    monkeypatch.setattr(tn, "has_active_tunnel", lambda tid: True)
    yield
    for registry in (tn._tunnel_cx_sockets, tn._tenant_custom_connectors, tn._tunnel_custom_tool_routes, tn._tunnel_tool_routes):
        registry.clear()


def _connect_cx(tenant_id: str = TENANT) -> object:
    ws = object()
    tn._tunnel_cx_sockets[tenant_id] = ws
    return ws


def _announce(ws, names=("notes",), opt_in=True) -> None:
    tn._record_custom_connector_announcement(TENANT, ws, {
        "type": "custom_connectors",
        "opt_in": opt_in,
        "connectors": [{"name": n, "transport": "stdio"} for n in names],
    })


def _notes_tools() -> list[dict]:
    return [{"name": "search", "description": "Search notes"}, {"name": "log_task", "description": "Shadow attempt"}]


def test_no_announcement_lists_nothing_and_asks_nothing(monkeypatch):
    fake = FakeConnectors({"cx:notes": _notes_tools()})
    monkeypatch.setattr(tn, "_tunnel_jsonrpc", fake)
    _connect_cx()
    assert asyncio.run(tn.list_custom_connector_tools(TENANT)) == []
    assert fake.calls == []


def test_opt_in_false_announcement_records_nothing(monkeypatch):
    fake = FakeConnectors({"cx:notes": _notes_tools()})
    monkeypatch.setattr(tn, "_tunnel_jsonrpc", fake)
    ws = _connect_cx()
    _announce(ws, opt_in=False)
    assert TENANT not in tn._tenant_custom_connectors
    assert asyncio.run(tn.list_custom_connector_tools(TENANT)) == []
    assert fake.calls == []


def test_opt_in_true_lists_connector_tools_under_the_custom_name(monkeypatch):
    fake = FakeConnectors({"cx:notes": [{"name": "search", "description": "Search notes"}]})
    monkeypatch.setattr(tn, "_tunnel_jsonrpc", fake)
    ws = _connect_cx()
    _announce(ws)
    tools = asyncio.run(tn.list_custom_connector_tools(TENANT))
    assert [t["name"] for t in tools] == ["custom-notes__search"]
    assert tools[0]["description"] == "Search notes"
    assert ("cx:notes", "tools/list", {}) in fake.calls


def test_a_native_name_is_never_shadowed(monkeypatch):
    fake = FakeConnectors({"cx:notes": _notes_tools()})
    monkeypatch.setattr(tn, "_tunnel_jsonrpc", fake)
    ws = _connect_cx()
    _announce(ws)
    tools = asyncio.run(tn.list_custom_connector_tools(TENANT, reserved_names={"log_task"}))
    names = [t["name"] for t in tools]
    assert "log_task" not in names
    assert "custom-notes__log_task" in names


def test_reserved_names_are_excluded_from_the_list(monkeypatch):
    fake = FakeConnectors({"cx:notes": _notes_tools()})
    monkeypatch.setattr(tn, "_tunnel_jsonrpc", fake)
    ws = _connect_cx()
    _announce(ws)
    tools = asyncio.run(tn.list_custom_connector_tools(TENANT, reserved_names={"custom-notes__search"}))
    assert [t["name"] for t in tools] == ["custom-notes__log_task"]


def test_call_is_forwarded_to_the_named_connector(monkeypatch):
    fake = FakeConnectors({"cx:notes": [{"name": "search"}]})
    monkeypatch.setattr(tn, "_tunnel_jsonrpc", fake)
    ws = _connect_cx()
    _announce(ws)
    result = asyncio.run(tn.call_custom_connector_tool(TENANT, "custom-notes__search", {"q": "x"}))
    assert result["content"][0]["text"] == "ok"
    assert ("cx:notes", "tools/call", {"name": "search", "arguments": {"q": "x"}}) in fake.calls


def test_call_without_an_announcement_is_refused_and_never_forwarded(monkeypatch):
    fake = FakeConnectors({"cx:notes": [{"name": "search"}]})
    monkeypatch.setattr(tn, "_tunnel_jsonrpc", fake)
    _connect_cx()
    with pytest.raises(tn.CustomConnectorUnavailable):
        asyncio.run(tn.call_custom_connector_tool(TENANT, "custom-notes__search", {}))
    assert fake.calls == []


def test_opt_out_after_listing_makes_the_tool_uncallable(monkeypatch):
    fake = FakeConnectors({"cx:notes": [{"name": "search"}]})
    monkeypatch.setattr(tn, "_tunnel_jsonrpc", fake)
    ws = _connect_cx()
    _announce(ws)
    asyncio.run(tn.list_custom_connector_tools(TENANT))
    _announce(ws, opt_in=False)
    with pytest.raises(tn.CustomConnectorUnavailable):
        asyncio.run(tn.call_custom_connector_tool(TENANT, "custom-notes__search", {}))
    assert all(label != "cx:notes" or method != "tools/call" for label, method, _ in fake.calls)


def test_a_socket_that_is_not_connected_lists_nothing(monkeypatch):
    fake = FakeConnectors({"cx:notes": [{"name": "search"}]})
    monkeypatch.setattr(tn, "_tunnel_jsonrpc", fake)
    ws = _connect_cx()
    _announce(ws)
    tn._tunnel_cx_sockets.pop(TENANT)
    assert asyncio.run(tn.list_custom_connector_tools(TENANT)) == []


def test_the_announcement_is_cleared_only_by_its_own_socket(monkeypatch):
    fake = FakeConnectors({"cx:notes": [{"name": "search"}]})
    monkeypatch.setattr(tn, "_tunnel_jsonrpc", fake)
    ws_one = _connect_cx()
    _announce(ws_one)
    other = object()
    tn._clear_custom_connector_announcement(TENANT, other)
    assert TENANT in tn._tenant_custom_connectors
    tn._clear_custom_connector_announcement(TENANT, ws_one)
    assert TENANT not in tn._tenant_custom_connectors


def test_connector_error_text_is_not_echoed_to_the_caller(monkeypatch):
    fake = FakeConnectors({"cx:notes": [{"name": "search"}]}, call_error={"code": -1, "message": f"leaked {SECRET}"})
    monkeypatch.setattr(tn, "_tunnel_jsonrpc", fake)
    ws = _connect_cx()
    _announce(ws)
    with pytest.raises(tn.CustomConnectorUnavailable) as info:
        asyncio.run(tn.call_custom_connector_tool(TENANT, "custom-notes__search", {}))
    assert SECRET not in str(info.value)
    assert "notes" in str(info.value)


def test_tunnel_jsonrpc_names_the_connector_in_a_header(monkeypatch):
    from starlette.responses import Response

    captured: dict = {}

    async def fake_do_proxy(tenant_id, method, path, query, headers, body_bytes, sockets, pending, label, **kw):
        captured.setdefault("headers", []).append((label, dict(headers)))
        payload = {"jsonrpc": "2.0", "id": "x", "result": {"tools": []}}
        return Response(content=json.dumps(payload), status_code=200, media_type="application/json")

    monkeypatch.setattr(tn, "_do_proxy", fake_do_proxy)
    tn._tunnel_cx_sockets[TENANT] = object()
    asyncio.run(tn._tunnel_jsonrpc(TENANT, "cx:notes", "tools/list", {}))
    label, headers = captured["headers"][0]
    assert label == "cx:notes"
    assert headers["x-meridian-custom-connector"] == "notes"


def test_tunnel_jsonrpc_adds_no_connector_header_for_slot_labels(monkeypatch):
    from starlette.responses import Response

    captured: dict = {}

    async def fake_do_proxy(tenant_id, method, path, query, headers, body_bytes, sockets, pending, label, **kw):
        captured["headers"] = dict(headers)
        return Response(content=json.dumps({"jsonrpc": "2.0", "id": "x", "result": {}}), status_code=200,
                        media_type="application/json")

    monkeypatch.setattr(tn, "_do_proxy", fake_do_proxy)
    tn._tunnel_sockets[TENANT] = object()
    try:
        asyncio.run(tn._tunnel_jsonrpc(TENANT, "fs", "tools/list", {}))
    finally:
        tn._tunnel_sockets.pop(TENANT, None)
    assert "x-meridian-custom-connector" not in captured["headers"]


# ---------------------------------------------------------------------------
# Handler: tools/list and tools/call (tunnel stubbed)
# ---------------------------------------------------------------------------

def _tenant(filter_value=None) -> dict:
    tenant = {"id": TENANT, "plan": "pro"}
    if filter_value is not None:
        tenant["tunnel_tool_filter"] = filter_value
    return tenant


def _list(monkeypatch, tenant):
    async def no_slot_tools(tid, reserved):
        return []

    monkeypatch.setattr(tn, "list_tunnel_tools", no_slot_tools)
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    return asyncio.run(mh._handle_mcp_request(body, db=None, data_dir="/tmp", tenant=tenant))


def _call(monkeypatch, tenant, name, args=None):
    body = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": name, "arguments": args or {}}}
    return asyncio.run(mh._handle_mcp_request(body, db=None, data_dir="/tmp", tenant=tenant))


def test_tools_list_shows_custom_tools_only_when_announced(monkeypatch):
    fake = FakeConnectors({"cx:notes": [{"name": "search"}]})
    monkeypatch.setattr(tn, "_tunnel_jsonrpc", fake)
    resp = _list(monkeypatch, _tenant())
    assert "custom-notes__search" not in {t["name"] for t in resp["result"]["tools"]}

    _announce(_connect_cx())
    resp = _list(monkeypatch, _tenant())
    names = {t["name"] for t in resp["result"]["tools"]}
    assert "custom-notes__search" in names
    assert "log_task" in names  # native tools are untouched


def test_tools_list_hides_a_custom_tool_whose_group_is_off(monkeypatch):
    fake = FakeConnectors({"cx:notes": [{"name": "search"}]})
    monkeypatch.setattr(tn, "_tunnel_jsonrpc", fake)
    _announce(_connect_cx())
    resp = _list(monkeypatch, _tenant(json.dumps({"groups": {"custom": False}})))
    names = {t["name"] for t in resp["result"]["tools"]}
    assert "custom-notes__search" not in names
    assert "log_task" in names


def test_tools_call_forwards_an_announced_custom_tool(monkeypatch):
    fake = FakeConnectors({"cx:notes": [{"name": "search"}]})
    monkeypatch.setattr(tn, "_tunnel_jsonrpc", fake)
    _announce(_connect_cx())
    resp = _call(monkeypatch, _tenant(), "custom-notes__search", {"q": "x"})
    assert resp["result"]["content"][0]["text"] == "ok"
    assert ("cx:notes", "tools/call", {"name": "search", "arguments": {"q": "x"}}) in fake.calls


def test_tools_call_refuses_a_disabled_custom_tool_without_forwarding(monkeypatch):
    fake = FakeConnectors({"cx:notes": [{"name": "search"}]})
    monkeypatch.setattr(tn, "_tunnel_jsonrpc", fake)
    _announce(_connect_cx())
    tenant = _tenant(json.dumps({"tools": {"custom-notes__search": False}}))
    resp = _call(monkeypatch, tenant, "custom-notes__search")
    assert resp["error"]["code"] == -32603
    assert "custom-notes__search" in resp["error"]["message"]
    assert "disabled" in resp["error"]["message"]
    assert not any(method == "tools/call" for _, method, _ in fake.calls)


def test_tools_call_refuses_a_custom_tool_when_the_owner_has_not_opted_in(monkeypatch):
    fake = FakeConnectors({"cx:notes": [{"name": "search"}]})
    monkeypatch.setattr(tn, "_tunnel_jsonrpc", fake)
    _connect_cx()  # socket up, but no opted-in announcement
    resp = _call(monkeypatch, _tenant(), "custom-notes__search")
    assert resp["error"]["code"] == -32603
    assert "not available" in resp["error"]["message"]
    assert fake.calls == []


def test_tools_call_never_routes_a_native_name_to_a_connector(monkeypatch):
    fake = FakeConnectors({"cx:notes": [{"name": "log_task"}]})
    monkeypatch.setattr(tn, "_tunnel_jsonrpc", fake)
    _announce(_connect_cx())
    # log_task with no db is a native call; the connector is never asked to run it.
    _call(monkeypatch, _tenant(), "log_task", {})
    assert not any(label.startswith("cx:") and method == "tools/call" for label, method, _ in fake.calls)


# ---------------------------------------------------------------------------
# Client: the relay, the spawn step, and what it prints
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status: int = 200, body: bytes = b"{}", headers: dict | None = None) -> None:
        self.status_code = status
        self.content = body
        self.headers = headers or {"content-type": "application/json", "x-other": "dropped"}


class _FakeHttp:
    def __init__(self, response: _FakeResponse | None = None, error: Exception | None = None) -> None:
        self.response = response or _FakeResponse()
        self.error = error
        self.calls: list[dict] = []

    async def request(self, method, url, headers=None, content=None, timeout=None, follow_redirects=None):
        self.calls.append({"method": method, "url": url, "headers": dict(headers or {}), "content": content,
                           "follow_redirects": follow_redirects})
        if self.error is not None:
            raise self.error
        return self.response


def _endpoints() -> dict:
    return {
        "notes": tc._CustomEndpoint("notes", "stdio", local_url="http://127.0.0.1:50123/mcp"),
        "remote": tc._CustomEndpoint("remote", "http", remote_url="https://example.com/mcp"),
    }


def _request_msg(name: str, body: dict | None = None, **extra) -> dict:
    payload = json.dumps(body or {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode()
    msg = {
        "type": "request",
        "id": "req-1",
        "method": "POST",
        "path": "/mcp",
        "query": "",
        "headers": {"content-type": "application/json", "accept": "application/json",
                    "Mcp-Session-Id": "sess-1", tc._CUSTOM_CONNECTOR_HEADER: name},
        "body": base64.b64encode(payload).decode(),
    }
    msg.update(extra)
    return msg


def test_relay_unknown_connector_returns_404_without_contacting_anything():
    http = _FakeHttp()
    resp = asyncio.run(tc._relay_custom_connector_request(http, _endpoints(), _request_msg("nope")))
    assert resp["status"] == 404
    assert http.calls == []


def test_relay_stdio_connector_goes_to_its_loopback_proxy_and_ignores_the_path():
    http = _FakeHttp()
    msg = _request_msg("notes", path="/somewhere/else")
    resp = asyncio.run(tc._relay_custom_connector_request(http, _endpoints(), msg))
    assert resp["status"] == 200
    assert http.calls[0]["url"] == "http://127.0.0.1:50123/mcp"


def test_relay_http_connector_goes_to_the_owners_url():
    http = _FakeHttp()
    asyncio.run(tc._relay_custom_connector_request(http, _endpoints(), _request_msg("remote")))
    assert http.calls[0]["url"] == "https://example.com/mcp"
    assert http.calls[0]["follow_redirects"] is False


def test_relay_forwards_only_the_three_allowed_headers_and_adds_no_auth():
    http = _FakeHttp()
    msg = _request_msg("remote")
    msg["headers"].update({
        "Authorization": f"Bearer {SECRET}",
        "Cookie": f"s={SECRET}",
        "x-meridian-repo-path": "/repo",
    })
    asyncio.run(tc._relay_custom_connector_request(http, _endpoints(), msg))
    forwarded = http.calls[0]["headers"]
    assert set(k.lower() for k in forwarded) <= {"content-type", "accept", "mcp-session-id", tc._CUSTOM_CONNECTOR_HEADER}
    assert "authorization" not in {k.lower() for k in forwarded}
    assert "cookie" not in {k.lower() for k in forwarded}
    assert SECRET not in json.dumps(forwarded)


def test_relay_passes_the_json_body_through_unchanged():
    http = _FakeHttp()
    body = {"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "search"}}
    asyncio.run(tc._relay_custom_connector_request(http, _endpoints(), _request_msg("notes", body)))
    assert json.loads(http.calls[0]["content"]) == body


def test_relay_refuses_a_non_post_method():
    http = _FakeHttp()
    resp = asyncio.run(tc._relay_custom_connector_request(http, _endpoints(), _request_msg("notes", method="DELETE")))
    assert resp["status"] == 405
    assert http.calls == []


def test_relay_returns_only_the_allowed_response_headers():
    http = _FakeHttp(_FakeResponse(200, b'{"ok":true}', {"content-type": "application/json",
                                                          "mcp-session-id": "sess-9", "set-cookie": "x=1"}))
    resp = asyncio.run(tc._relay_custom_connector_request(http, _endpoints(), _request_msg("notes")))
    assert resp["headers"] == {"content-type": "application/json", "mcp-session-id": "sess-9"}
    assert base64.b64decode(resp["body"]) == b'{"ok":true}'


def test_relay_failure_is_generic_and_leaks_neither_url_nor_secret(capsys):
    http = _FakeHttp(error=ConnectionError(f"could not reach https://example.com/mcp?token={SECRET}"))
    resp = asyncio.run(tc._relay_custom_connector_request(http, _endpoints(), _request_msg("remote")))
    body = base64.b64decode(resp["body"]).decode()
    assert resp["status"] == 502
    assert "remote" in body
    assert SECRET not in body
    assert "example.com" not in body
    captured = capsys.readouterr()
    assert SECRET not in captured.err and SECRET not in captured.out
    assert "ConnectionError" in captured.err


def test_relay_rejects_a_body_that_is_not_base64():
    http = _FakeHttp()
    msg = _request_msg("notes")
    msg["body"] = "@@@not-base64@@@"
    resp = asyncio.run(tc._relay_custom_connector_request(http, _endpoints(), msg))
    assert resp["status"] == 400
    assert http.calls == []


def test_custom_endpoint_repr_hides_its_urls():
    endpoint = tc._CustomEndpoint("remote", "http", remote_url=f"https://example.com/mcp?key={SECRET}")
    assert SECRET not in repr(endpoint)


def test_spawn_is_off_when_the_opt_in_is_off(monkeypatch, tmp_path, capsys):
    path = _write_settings(tmp_path, {"custom_connectors": [_entry(args=[SECRET])]})
    monkeypatch.setattr(cc, "settings_path", lambda *a, **k: path)
    spawned: list = []
    monkeypatch.setattr(tc, "_spawn_with_cache_retry", lambda *a, **k: spawned.append(a) or object())
    holders: list[dict] = []
    assert tc._spawn_custom_connector_processes("npx", holders) == {}
    assert spawned == [] and holders == []


def test_spawn_starts_stdio_and_registers_http_without_printing_values(monkeypatch, tmp_path, capsys):
    path = _write_settings(tmp_path, {
        "custom_tunnel_opt_in": True,
        "custom_connectors": [
            _entry(name="notes", args=[f"--key={SECRET}"]),
            _http_entry(name="remote", url=f"https://example.com/mcp?key={SECRET}"),
        ],
    })
    monkeypatch.setattr(cc, "settings_path", lambda *a, **k: path)
    seen: list[tuple] = []

    def fake_spawn(cmd, env, label, **kwargs):
        seen.append((cmd, env, label))
        return object()

    monkeypatch.setattr(tc, "_spawn_with_cache_retry", fake_spawn)
    holders: list[dict] = []
    endpoints = tc._spawn_custom_connector_processes("npx-fake", holders)

    assert set(endpoints) == {"notes", "remote"}
    assert endpoints["notes"].transport == "stdio"
    assert endpoints["notes"].local_url.startswith("http://127.0.0.1:") and endpoints["notes"].local_url.endswith("/mcp")
    assert endpoints["remote"].remote_url == f"https://example.com/mcp?key={SECRET}"
    assert len(seen) == 1 and seen[0][2] == "cx:notes"
    assert len(holders) == 1 and holders[0]["label"] == "cx:notes"
    captured = capsys.readouterr()
    assert SECRET not in captured.out and SECRET not in captured.err
    assert "notes" in captured.out and "remote" in captured.out


def test_spawn_refusal_message_is_shown_without_values(monkeypatch, tmp_path, capsys):
    path = _write_settings(tmp_path, {
        "custom_tunnel_opt_in": True,
        "custom_connectors": [_http_entry(url=f"http://example.com/mcp?key={SECRET}")],
    })
    monkeypatch.setattr(cc, "settings_path", lambda *a, **k: path)
    holders: list[dict] = []
    assert tc._spawn_custom_connector_processes("npx-fake", holders) == {}
    captured = capsys.readouterr()
    assert SECRET not in captured.out and SECRET not in captured.err


def test_cx_socket_url_targets_the_cx_route():
    url = tc._ws_custom_connectors_url("https://usemeridian.us", TENANT, "tok")
    assert url.startswith("wss://usemeridian.us/tunnel-cx/" + TENANT + "?token=")
