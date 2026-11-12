"""Tests for meridian_inventree.server: the fixed tool list, configuration, the write gate and wiring.

Every request goes through a fake transport. Nothing here opens a socket. The import test
runs in a child process with socket calls blocked.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import subprocess
import sys
import urllib.parse
from pathlib import Path
from typing import Any

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from meridian_inventree import read_tools, server
from meridian_inventree.server import (
    ENV_STATE,
    ENV_TOKEN,
    ENV_URL,
    ConfigError,
    build_app,
    create_server,
    default_state_dir,
    load_config,
    main,
    version_supported,
)

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
TOKEN = "tok_fixture_0123456789abcdef"
BASE_URL = "https://inventree.example.test"

READ_TOOLS = {
    "inventree_search_parts",
    "inventree_get_part",
    "inventree_list_categories",
    "inventree_search_stock",
    "inventree_get_stock_item",
    "inventree_list_locations",
    "inventree_roles",
    "inventree_check_version",
    "inventree_plan_status",
}
WRITE_TOOLS = {"inventree_propose"}
EXPECTED_TOOLS = READ_TOOLS | WRITE_TOOLS


class FakeTransport:
    """Records every request and answers from a route table. Unknown routes answer 404."""

    def __init__(self, routes: dict[tuple[str, str], tuple[int, Any]] | None = None) -> None:
        self.routes: dict[tuple[str, str], tuple[int, Any]] = routes or {}
        self.calls: list[tuple[str, str, bytes | None]] = []

    def __call__(
        self,
        method: str,
        url: str,
        headers: Any,
        body: bytes | None,
        timeout: float,
    ) -> tuple[int, bytes]:
        self.calls.append((method, url, body))
        path = urllib.parse.urlsplit(url).path
        status, payload = self.routes.get((method, path), (404, {"detail": "not found"}))
        return status, json.dumps(payload).encode("utf-8")

    @property
    def methods(self) -> set[str]:
        return {method for method, _url, _body in self.calls}


def _version_routes(version: str) -> dict[tuple[str, str], tuple[int, Any]]:
    return {("GET", "/api/"): (200, {"server": "InvenTree", "version": version, "apiVersion": 999})}


def _env(state: Path, **extra: str) -> dict[str, str]:
    env = {ENV_URL: BASE_URL, ENV_TOKEN: TOKEN, ENV_STATE: str(state)}
    env.update(extra)
    return env


def _app(tmp_path: Path, routes: dict[tuple[str, str], tuple[int, Any]] | None = None) -> tuple[Any, FakeTransport]:
    transport = FakeTransport(routes)
    return build_app(_env(tmp_path / "state"), transport=transport), transport


def _create_part(name: str, key: str | None = None) -> dict[str, Any]:
    return {"op": "create_part", "client_key": key or f"part-{name.replace(' ', '-')}", "params": {"name": name}}


def _tool_list(app: Any) -> list[Any]:
    return asyncio.run(app.mcp.list_tools())


# ---------------------------------------------------------------- tool list and registration


def test_tool_list_is_exactly_the_planned_set(tmp_path: Path) -> None:
    app, transport = _app(tmp_path)
    names = {tool.name for tool in _tool_list(app)}
    assert names == EXPECTED_TOOLS
    assert set(app.tools) == EXPECTED_TOOLS
    assert transport.calls == []  # building the server made no request


def test_no_tool_executes_or_approves(tmp_path: Path) -> None:
    app, _ = _app(tmp_path)
    names = {tool.name for tool in _tool_list(app)}
    assert "inventree_execute" not in names
    for name in names:
        for word in ("execute", "approve", "run", "apply", "commit", "delete"):
            assert word not in name, name


def test_read_tools_are_marked_read_only_and_propose_is_not(tmp_path: Path) -> None:
    app, _ = _app(tmp_path)
    tools = {tool.name: tool for tool in _tool_list(app)}
    for name in READ_TOOLS:
        assert tools[name].annotations is not None, name
        assert tools[name].annotations.readOnlyHint is True, name
    assert tools["inventree_propose"].annotations.readOnlyHint is False


def test_tool_inputs_match_the_plan(tmp_path: Path) -> None:
    app, _ = _app(tmp_path)
    tools = {tool.name: tool for tool in _tool_list(app)}
    assert "operations" in tools["inventree_propose"].inputSchema["properties"]
    assert "plan_id" in tools["inventree_plan_status"].inputSchema["properties"]
    assert "parent" in tools["inventree_list_categories"].inputSchema["properties"]
    assert "cascade" in tools["inventree_search_stock"].inputSchema["properties"]


# ---------------------------------------------------------------- configuration


def test_missing_url_is_a_clear_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as info:
        create_server({ENV_TOKEN: TOKEN, ENV_STATE: str(tmp_path)})
    assert info.value.code == "missing_url"
    assert ENV_URL in info.value.message
    assert TOKEN not in info.value.message


def test_missing_token_is_a_clear_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as info:
        create_server({ENV_URL: BASE_URL, ENV_STATE: str(tmp_path)})
    assert info.value.code == "missing_token"
    assert ENV_TOKEN in info.value.message


def test_main_reports_missing_url_on_stderr_and_exits_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    err = io.StringIO()
    served: list[Any] = []
    code = main({ENV_TOKEN: TOKEN, ENV_STATE: str(tmp_path)}, stderr=err, serve=served.append)
    assert code == server.EXIT_CONFIG
    assert ENV_URL in err.getvalue()
    assert TOKEN not in err.getvalue()
    assert served == []
    assert capsys.readouterr().out == ""  # stdout belongs to the MCP protocol


def test_token_is_hidden_from_repr_and_error_messages(tmp_path: Path) -> None:
    config = load_config(_env(tmp_path))
    assert TOKEN not in repr(config)
    with pytest.raises(ConfigError) as insecure:
        create_server(_env(tmp_path, **{ENV_URL: "http://example.test"}))
    assert insecure.value.code == "insecure_url"
    assert TOKEN not in insecure.value.message
    with pytest.raises(ConfigError) as bad_token:
        create_server(_env(tmp_path, **{ENV_TOKEN: "has space " + TOKEN}))
    assert bad_token.value.code == "bad_token"
    assert TOKEN not in bad_token.value.message


def test_state_folder_inside_the_repo_is_refused() -> None:
    if server._repo_root(PACKAGE_ROOT) is None:
        pytest.skip("not running from a git checkout")
    with pytest.raises(ConfigError) as info:
        load_config(_env(PACKAGE_ROOT / "state"))
    assert info.value.code == "state_in_repo"


def test_default_state_folder_is_under_the_user_state_directory(tmp_path: Path) -> None:
    environ = {
        ENV_URL: BASE_URL,
        ENV_TOKEN: TOKEN,
        "LOCALAPPDATA": str(tmp_path / "local"),
        "XDG_STATE_HOME": str(tmp_path / "xdg"),
    }
    expected = default_state_dir(environ)
    assert expected.parts[-2:] == ("meridian", "inventree")
    assert load_config(environ).state_dir == expected.resolve()


def test_import_reads_no_settings_and_makes_no_network_call() -> None:
    script = (
        "import socket\n"
        "def _blocked(*args, **kwargs):\n"
        "    raise RuntimeError('network blocked during import')\n"
        "socket.create_connection = _blocked\n"
        "socket.socket.connect = _blocked\n"
        "socket.socket.connect_ex = _blocked\n"
        "import meridian_inventree.server\n"
        "import meridian_inventree.__main__\n"
        "print('imported')\n"
    )
    env = {key: value for key, value in os.environ.items() if not key.startswith(("INVENTREE_", "MERIDIAN_INVENTREE_"))}
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=PACKAGE_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "imported" in result.stdout


# ---------------------------------------------------------------- startup and version


def test_version_prefix_matching() -> None:
    assert version_supported("0.17.5", ("0.17.",)) is True
    assert version_supported("0.170.1", ("0.17.",)) is False
    assert version_supported("0.17.5", ()) is False
    assert version_supported(None, ("0.17.",)) is False


def test_main_reports_the_version_and_serves_once(tmp_path: Path) -> None:
    transport = FakeTransport(_version_routes("9.9.9"))
    err = io.StringIO()
    served: list[Any] = []
    code = main(_env(tmp_path), transport=transport, stderr=err, serve=served.append)
    assert code == 0
    assert len(served) == 1
    assert "9.9.9" in err.getvalue()
    assert "refused" in err.getvalue()
    assert TOKEN not in err.getvalue()


def test_main_still_serves_when_the_version_check_fails(tmp_path: Path) -> None:
    transport = FakeTransport({("GET", "/api/"): (503, {})})
    err = io.StringIO()
    served: list[Any] = []
    code = main(_env(tmp_path), transport=transport, stderr=err, serve=served.append)
    assert code == 0
    assert len(served) == 1
    assert "version check failed" in err.getvalue()


# ---------------------------------------------------------------- write gate and propose


def test_writes_are_refused_while_no_version_range_is_set(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "SUPPORTED_VERSION_PREFIXES", ())
    app, transport = _app(tmp_path, _version_routes("0.17.5"))
    with pytest.raises(ToolError, match="version_unsupported"):
        app.tools["inventree_propose"](operations=[_create_part("Resistor 10k")])
    assert transport.methods == set()  # no request at all was sent, not even the version read
    assert list((tmp_path / "state" / "plans").glob("*.json")) == []


def test_writes_are_refused_when_the_version_cannot_be_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "SUPPORTED_VERSION_PREFIXES", ("0.17.",))
    app, transport = _app(tmp_path, {("GET", "/api/"): (503, {})})
    with pytest.raises(ToolError, match="version_unverified"):
        app.tools["inventree_propose"](operations=[_create_part("Resistor 10k")])
    assert transport.methods == {"GET"}


def test_propose_stores_a_plan_and_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "SUPPORTED_VERSION_PREFIXES", ("0.17.",))
    app, transport = _app(tmp_path, _version_routes("0.17.5"))
    result = app.tools["inventree_propose"](operations=[_create_part("Resistor 10k")])
    assert result["status"] == "proposed"
    assert result["nothing_written"] is True
    assert len(result["plan_id"]) == 32
    assert result["operations"][0]["method"] == "POST"
    assert result["operations"][0]["path"] == "/api/part/"
    assert transport.methods == {"GET"}  # no POST or PATCH was sent
    assert (tmp_path / "state" / "plans" / f"{result['plan_id']}.json").exists()
    text = json.dumps(result)
    assert "plan_hash" not in text
    assert "approved_hash" not in text


def test_plan_status_reports_state_and_hides_the_hash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "SUPPORTED_VERSION_PREFIXES", ("0.17.",))
    app, _ = _app(tmp_path, _version_routes("0.17.5"))
    plan_id = app.tools["inventree_propose"](operations=[_create_part("Resistor 10k")])["plan_id"]
    status = app.tools["inventree_plan_status"](plan_id=plan_id)
    assert status["plan_id"] == plan_id
    assert status["status"] == "proposed"
    assert status["approved_at"] is None
    assert status["executed_at"] is None
    assert "plan_hash" not in json.dumps(status)


def test_plan_status_rejects_a_malformed_id(tmp_path: Path) -> None:
    app, _ = _app(tmp_path)
    with pytest.raises(ToolError, match="invalid_plan_id"):
        app.tools["inventree_plan_status"](plan_id="../../etc/passwd")


def test_plan_status_reports_an_unknown_id(tmp_path: Path) -> None:
    app, _ = _app(tmp_path)
    with pytest.raises(ToolError, match="not_found"):
        app.tools["inventree_plan_status"](plan_id="0" * 32)


def test_unknown_operation_is_refused_before_any_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "SUPPORTED_VERSION_PREFIXES", ("0.17.",))
    app, transport = _app(tmp_path, _version_routes("0.17.5"))
    with pytest.raises(ToolError, match="unknown_operation"):
        app.tools["inventree_propose"](
            operations=[{"op": "delete_part", "client_key": "k1", "params": {"pk": 1}}],
        )
    assert transport.calls == []


def test_plan_over_fifty_operations_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "SUPPORTED_VERSION_PREFIXES", ("0.17.",))
    app, transport = _app(tmp_path, _version_routes("0.17.5"))
    operations = [_create_part(f"Part {index}", key=f"key-{index}") for index in range(51)]
    with pytest.raises(ToolError, match="too_many_operations"):
        app.tools["inventree_propose"](operations=operations)
    assert transport.calls == []


def test_empty_plan_is_refused(tmp_path: Path) -> None:
    app, transport = _app(tmp_path)
    with pytest.raises(ToolError, match="empty_plan"):
        app.tools["inventree_propose"](operations=[])
    assert transport.calls == []


def test_duplicate_client_keys_are_refused(tmp_path: Path) -> None:
    app, transport = _app(tmp_path)
    operations = [_create_part("Alpha", key="same"), _create_part("Beta", key="same")]
    with pytest.raises(ToolError, match="duplicate_client_key"):
        app.tools["inventree_propose"](operations=operations)
    assert transport.calls == []


def test_operation_with_extra_keys_is_refused(tmp_path: Path) -> None:
    app, transport = _app(tmp_path)
    operation = {"op": "create_part", "client_key": "k", "params": {"name": "A"}, "approved": True}
    with pytest.raises(ToolError, match="invalid_operation"):
        app.tools["inventree_propose"](operations=[operation])
    assert transport.calls == []


# ---------------------------------------------------------------- read tools


def test_search_parts_sends_the_search_text_and_returns_items(tmp_path: Path) -> None:
    routes = {("GET", "/api/part/"): (200, {"count": 1, "next": None, "results": [{"pk": 7, "name": "Resistor"}]})}
    app, transport = _app(tmp_path, routes)
    result = app.tools["inventree_search_parts"](query="resistor")
    assert result["items"] == [{"pk": 7, "name": "Resistor"}]
    assert result["truncated"] is False
    method, url, body = transport.calls[-1]
    assert method == "GET"
    assert "search=resistor" in url
    assert body is None


def test_limit_above_the_maximum_is_clamped_and_noted(tmp_path: Path) -> None:
    routes = {("GET", "/api/part/"): (200, {"results": [], "next": None})}
    app, _ = _app(tmp_path, routes)
    result = app.tools["inventree_search_parts"](query="", limit=500)
    assert result["limit"] == read_tools.MAX_LIMIT
    assert any("clamped" in note for note in result["notes"])


def test_get_part_returns_the_object(tmp_path: Path) -> None:
    app, _ = _app(tmp_path, {("GET", "/api/part/7/"): (200, {"pk": 7, "name": "Resistor"})})
    assert app.tools["inventree_get_part"](pk=7) == {"pk": 7, "name": "Resistor"}


def test_get_part_rejects_a_path_like_id_without_a_request(tmp_path: Path) -> None:
    app, transport = _app(tmp_path)
    with pytest.raises(ToolError, match="invalid_argument"):
        app.tools["inventree_get_part"](pk="1/../../admin")
    assert transport.calls == []


def test_roles_returns_the_list_under_roles(tmp_path: Path) -> None:
    app, _ = _app(tmp_path, {("GET", "/api/user/roles/"): (200, [{"name": "part", "view": True}])})
    assert app.tools["inventree_roles"]() == {"roles": [{"name": "part", "view": True}]}


def test_check_version_reports_writes_refused_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "SUPPORTED_VERSION_PREFIXES", ())
    app, _ = _app(tmp_path, _version_routes("0.17.5"))
    info = app.tools["inventree_check_version"]()
    assert info["reported_version"] == "0.17.5"
    assert info["writes_allowed"] is False
    assert info["supported_prefixes"] == []


def test_check_version_reports_writes_allowed_inside_the_range(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "SUPPORTED_VERSION_PREFIXES", ("0.17.",))
    app, _ = _app(tmp_path, _version_routes("0.17.5"))
    assert app.tools["inventree_check_version"]()["writes_allowed"] is True


@pytest.mark.parametrize(
    ("tool", "args", "path", "expected_query"),
    [
        ("inventree_list_categories", {"parent": 3}, "/api/part/category/", "parent=3"),
        ("inventree_search_stock", {"query": "bolt", "location": 4, "cascade": False}, "/api/stock/", "location=4"),
        ("inventree_list_locations", {"parent": 5}, "/api/stock/location/", "parent=5"),
    ],
)
def test_list_and_search_tools_hit_their_endpoints(
    tmp_path: Path, tool: str, args: dict[str, Any], path: str, expected_query: str
) -> None:
    app, transport = _app(tmp_path, {("GET", path): (200, {"results": [], "next": None})})
    result = app.tools[tool](**args)
    assert result["items"] == []
    method, url, _body = transport.calls[-1]
    assert method == "GET"
    assert urllib.parse.urlsplit(url).path == path
    assert expected_query in url


def test_get_stock_item_returns_the_object(tmp_path: Path) -> None:
    app, _ = _app(tmp_path, {("GET", "/api/stock/9/"): (200, {"pk": 9, "quantity": "5"})})
    assert app.tools["inventree_get_stock_item"](pk="9") == {"pk": 9, "quantity": "5"}


@pytest.mark.parametrize(
    ("operations", "code"),
    [
        ({"op": "create_part"}, "invalid_operation"),
        ([{"op": 5, "client_key": "k", "params": {}}], "unknown_operation"),
        ([{"op": "create_part", "client_key": "k", "params": ["Resistor"]}], "invalid_operation"),
        ([{"op": "create_part", "client_key": "   ", "params": {"name": "A"}}], "invalid_client_key"),
    ],
)
def test_malformed_operations_are_refused_before_any_request(
    tmp_path: Path, operations: Any, code: str
) -> None:
    app, transport = _app(tmp_path)
    with pytest.raises(ToolError, match=code):
        app.tools["inventree_propose"](operations=operations)
    assert transport.calls == []


def test_unexpected_errors_are_reported_without_their_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app, _ = _app(tmp_path)

    def broken(client: Any) -> dict[str, Any]:
        raise RuntimeError("secret detail " + TOKEN)

    monkeypatch.setattr(read_tools, "roles", broken)
    with pytest.raises(ToolError) as info:
        app.tools["inventree_roles"]()
    assert str(info.value).startswith("internal_error")
    assert TOKEN not in str(info.value)
    assert "secret detail" not in str(info.value)


def test_unusable_state_folder_exits_2_with_a_message(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a folder", encoding="utf-8")
    err = io.StringIO()
    served: list[Any] = []
    code = main(_env(blocker / "state"), transport=FakeTransport(), stderr=err, serve=served.append)
    assert code == server.EXIT_CONFIG
    assert "state folder" in err.getvalue()
    assert served == []


def test_module_entry_point_exits_2_when_the_url_is_missing() -> None:
    env = {key: value for key, value in os.environ.items() if not key.startswith(("INVENTREE_", "MERIDIAN_INVENTREE_"))}
    env[ENV_TOKEN] = TOKEN
    result = subprocess.run(
        [sys.executable, "-m", "meridian_inventree"],
        cwd=PACKAGE_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 2
    assert ENV_URL in result.stderr
    assert TOKEN not in result.stderr
    assert result.stdout == ""
