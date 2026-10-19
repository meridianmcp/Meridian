"""Local authority over the tunnel: the hosted side picks which plugins run and their ports, and nothing else.

Covers:
  - The hosted config can never supply a command or environment (built-in or custom).
  - Custom plugins run only with a command named in this machine's local policy.
  - The shell slot (desktop-commander) starts only when the local policy enables it.
  - Filesystem write tools are refused over the tunnel unless the local policy allows them, including when the
    tool name carries a relay prefix.
  - The bearer token rides in the Authorization header, not in the WebSocket URL.
  - run_verification is answered by a background task, so the message loop keeps reading.
  - The filesystem server is pinned, and the denylist covers GitHub CLI and gcloud config.
"""
from __future__ import annotations

import asyncio
import base64
import json

import pytest

import websockets

from meridian import tunnel_client as tc
from meridian.tunnel_plugins import resolve_custom_plugins, resolve_plugins


@pytest.fixture
def local_home(tmp_path, monkeypatch):
    """Point the home folder, APPDATA, and the local-runner state dir at tmp_path."""
    home = tmp_path / "home"
    home.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    appdata = tmp_path / "AppData" / "Roaming"
    appdata.mkdir(parents=True)
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setenv("MERIDIAN_LOCAL_RUNNER_STATE_DIR", str(state))
    return home


def _write_policy(data) -> None:
    from meridian.local_runner import default_state_dir
    path = default_state_dir() / tc.LOCAL_POLICY_FILENAME
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")


# ---------------------------------------------------------------------------
# local policy file
# ---------------------------------------------------------------------------

def test_missing_policy_is_locked_down(local_home):
    policy = tc._load_local_policy()
    assert policy == {
        "allow_fs_writes": False, "enable_shell_slot": False,
        "custom_plugins": {}, "builtin_commands": {},
    }


def test_only_a_literal_true_turns_a_switch_on(local_home):
    _write_policy({"allow_fs_writes": "yes", "enable_shell_slot": 1})
    policy = tc._load_local_policy()
    assert policy["allow_fs_writes"] is False
    assert policy["enable_shell_slot"] is False


def test_literal_true_turns_the_switches_on(local_home):
    _write_policy({"allow_fs_writes": True, "enable_shell_slot": True})
    policy = tc._load_local_policy()
    assert policy["allow_fs_writes"] is True
    assert policy["enable_shell_slot"] is True


def test_malformed_policy_falls_back_to_defaults(local_home):
    _write_policy("not json{")
    assert tc._load_local_policy()["allow_fs_writes"] is False


# ---------------------------------------------------------------------------
# the hosted config can never supply a command or environment
# ---------------------------------------------------------------------------

def test_server_commands_and_env_are_stripped_from_builtin_entries(local_home):
    policy = tc._load_local_policy()
    server = [{
        "name": "filesystem", "enabled": True, "port": 8808,
        "command": ["sh", "-c", "evil-from-server"], "env": {"EVIL": "1"},
    }]
    out = tc._server_config_without_commands(server, policy)
    assert len(out) == 1
    assert out[0]["enabled"] is True
    assert out[0]["port"] == 8808
    assert "command" not in out[0]
    assert "env" not in out[0]
    assert "evil-from-server" not in json.dumps(out)


def test_builtin_command_comes_only_from_the_local_policy(local_home):
    _write_policy({"builtin_commands": {"filesystem": {"command": ["my-local-fs"], "env": {"K": "v"}}}})
    policy = tc._load_local_policy()
    server = [{"name": "filesystem", "enabled": True, "command": ["sh", "-c", "hostile"]}]
    out = tc._server_config_without_commands(server, policy)
    assert out[0]["command"] == ["my-local-fs"]
    assert out[0]["env"] == {"K": "v"}
    assert "hostile" not in json.dumps(out)


def test_custom_plugin_without_a_local_command_is_dropped(local_home, capsys):
    policy = tc._load_local_policy()
    server = [{"name": "rogue", "port": 9001, "enabled": True, "command": ["sh", "-c", "curl x"]}]
    out = tc._server_config_without_commands(server, policy)
    assert out == []
    assert resolve_custom_plugins(out) == []
    assert "will not start" in capsys.readouterr().out


def test_custom_plugin_runs_with_the_command_from_the_local_policy(local_home):
    _write_policy({"custom_plugins": {"zoterox": {"command": ["node", "server.js"], "env": {"A": "b"}}}})
    policy = tc._load_local_policy()
    server = [{"name": "zoterox", "port": 9100, "enabled": True, "command": ["hostile"]}]
    out = tc._server_config_without_commands(server, policy)
    plugins = resolve_custom_plugins(out)
    assert len(plugins) == 1
    assert plugins[0]["command"] == ["node", "server.js"]
    assert plugins[0]["env"] == {"A": "b"}
    assert plugins[0]["port"] == 9100
    assert "hostile" not in json.dumps(out)


# ---------------------------------------------------------------------------
# the shell slot
# ---------------------------------------------------------------------------

def test_shell_slot_stays_off_without_local_opt_in(local_home):
    plugins = resolve_plugins(None, detected_slots=[])
    for p in plugins:
        if p["name"] == tc.SHELL_SLOT_NAME:
            p["enabled"] = True
    enforced = tc._enforce_local_slot_policy(plugins, tc._load_local_policy())
    shell = next(p for p in enforced if p["name"] == tc.SHELL_SLOT_NAME)
    assert shell["enabled"] is False


def test_shell_slot_starts_only_when_the_local_policy_enables_it(local_home):
    _write_policy({"enable_shell_slot": True})
    plugins = resolve_plugins(None, detected_slots=[])
    for p in plugins:
        if p["name"] == tc.SHELL_SLOT_NAME:
            p["enabled"] = True
    enforced = tc._enforce_local_slot_policy(plugins, tc._load_local_policy())
    shell = next(p for p in enforced if p["name"] == tc.SHELL_SLOT_NAME)
    assert shell["enabled"] is True


def test_hosted_config_cannot_enable_the_shell_slot(local_home):
    server = [{"name": tc.SHELL_SLOT_NAME, "enabled": True}]
    server_cfg = tc._server_config_without_commands(server, tc._load_local_policy())
    plugins = resolve_plugins(server_cfg, detected_slots=[])
    enforced = tc._enforce_local_slot_policy(plugins, tc._load_local_policy())
    shell = next(p for p in enforced if p["name"] == tc.SHELL_SLOT_NAME)
    assert shell["enabled"] is False


# ---------------------------------------------------------------------------
# filesystem writes over the tunnel
# ---------------------------------------------------------------------------

def _request_msg(payload) -> dict:
    body = base64.b64encode(json.dumps(payload).encode()).decode()
    return {"type": "request", "id": "r1", "method": "POST", "path": "/mcp", "body": body}


def _call(name: str, call_id: int = 7) -> dict:
    return {"jsonrpc": "2.0", "id": call_id, "method": "tools/call",
            "params": {"name": name, "arguments": {"path": "x", "content": "y"}}}


def _denial_body(denial: dict) -> dict:
    return json.loads(base64.b64decode(denial["body"]))


@pytest.mark.parametrize("name", [
    "write_file", "edit_file", "create_directory", "move_file",
    "filesystem__write_file", "Filesystem: write_file", "Filesystem: move_file",
])
def test_write_tool_call_is_refused(name):
    denial = tc._fs_write_denial(_request_msg(_call(name)))
    assert denial is not None
    assert denial["status"] == 403
    assert denial["id"] == "r1"
    body = _denial_body(denial)
    assert body["id"] == 7
    assert body["error"]["code"] == -32001


@pytest.mark.parametrize("name", ["read_text_file", "list_directory", "search_files", "Filesystem: read_text_file"])
def test_read_tool_call_passes(name):
    assert tc._fs_write_denial(_request_msg(_call(name))) is None


def test_batch_with_one_write_is_refused():
    batch = [_call("read_text_file", 1), _call("write_file", 2)]
    denial = tc._fs_write_denial(_request_msg(batch))
    assert denial is not None
    assert _denial_body(denial)["error"]["code"] == -32001


def test_unparseable_body_naming_a_write_tool_is_refused():
    body = base64.b64encode(b'{"method": "tools/call", "params": {"name": "write_file"').decode()
    denial = tc._fs_write_denial({"id": "r2", "body": body})
    assert denial is not None
    assert denial["status"] == 403


def test_unparseable_body_naming_nothing_passes():
    body = base64.b64encode(b"not json at all").decode()
    assert tc._fs_write_denial({"id": "r3", "body": body}) is None


def test_request_without_body_passes():
    assert tc._fs_write_denial({"id": "r4", "method": "GET", "path": "/mcp"}) is None


def test_write_tool_set_is_the_documented_one():
    # Guards against an accidental edit: these are the server's file-changing tools.
    assert tc.FS_WRITE_TOOLS == frozenset({"write_file", "edit_file", "create_directory", "move_file"})


# ---------------------------------------------------------------------------
# the bearer token is a header, never part of the URL
# ---------------------------------------------------------------------------

def test_tunnel_url_carries_no_token_and_the_token_in_the_header(local_home):
    url = tc._ws_url("https://usemeridian.us", "tenant-1", "sk_meridian_secret123")
    assert "sk_meridian" not in url
    assert "token=" not in url
    assert url.auth_headers == {"Authorization": "Bearer sk_meridian_secret123"}
    assert isinstance(url, str)


@pytest.mark.parametrize("builder", [tc._ws_code_url, tc._ws_extract_url])
def test_slot_urls_carry_no_token(local_home, builder):
    url = builder("https://usemeridian.us", "tenant-1", "sk_meridian_secret123")
    assert "sk_meridian" not in url
    assert "token=" not in url
    assert url.auth_headers == {"Authorization": "Bearer sk_meridian_secret123"}


def test_office_slot_url_carries_no_token(local_home):
    url = tc._ws_office_url("https://usemeridian.us", "tenant-1", "sk_meridian_secret123", "ppt")
    assert "sk_meridian" not in url
    assert "token=" not in url
    assert url.auth_headers == {"Authorization": "Bearer sk_meridian_secret123"}


def test_connect_kwargs_use_additional_headers_on_websockets_14_style_signature(monkeypatch, local_home):
    seen = {}

    def fake_connect(uri, *, additional_headers=None, ping_interval=None):
        seen["called"] = True
        return None

    monkeypatch.setattr(websockets, "connect", fake_connect)
    url = tc._ws_url("https://usemeridian.us", "t", "sk_meridian_x")
    kwargs = tc._ws_connect_kwargs(url)
    assert kwargs == {"additional_headers": {"Authorization": "Bearer sk_meridian_x"}}


def test_connect_kwargs_use_extra_headers_on_older_websockets_signature(monkeypatch, local_home):
    def fake_connect(uri, *, extra_headers=None, ping_interval=None):
        return None

    monkeypatch.setattr(websockets, "connect", fake_connect)
    url = tc._ws_url("https://usemeridian.us", "t", "sk_meridian_x")
    assert tc._ws_connect_kwargs(url) == {"extra_headers": {"Authorization": "Bearer sk_meridian_x"}}


def test_connect_kwargs_empty_for_a_plain_string_url():
    assert tc._ws_connect_kwargs("wss://usemeridian.us/tunnel/t") == {}


# ---------------------------------------------------------------------------
# run_verification is answered by a background task
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_answer_run_verification_sends_the_result_and_does_not_block_the_caller(monkeypatch):
    release = asyncio.Event()

    async def slow_verification(project_id):
        await release.wait()
        return {
            "status": "ok", "exit_code": 0, "passed": None, "failed": None,
            "stdout_tail": "", "stderr_tail": "", "cmd": "pytest",
        }

    sent: list[dict] = []

    class _WS:
        async def send(self, data):
            sent.append(json.loads(data))

    monkeypatch.setattr(tc, "_handle_run_verification", slow_verification)
    task = asyncio.ensure_future(tc._answer_run_verification(_WS(), "rv1", "proj-1"))
    await asyncio.sleep(0)
    assert not task.done(), "the answer must still be running; the message loop is not waiting on it"
    release.set()
    await task
    assert sent == [{
        "status": "ok", "exit_code": 0, "passed": None, "failed": None,
        "stdout_tail": "", "stderr_tail": "", "cmd": "pytest",
        "type": "run_cmd_result", "id": "rv1",
    }]


@pytest.mark.asyncio
async def test_answer_run_verification_swallows_a_closed_socket(monkeypatch):
    async def quick(project_id):
        return {"status": "ok", "exit_code": 0}

    class _ClosedWS:
        async def send(self, data):
            raise ConnectionError("socket closed while the command ran")

    monkeypatch.setattr(tc, "_handle_run_verification", quick)
    await tc._answer_run_verification(_ClosedWS(), "rv2", "proj-2")


# ---------------------------------------------------------------------------
# pinned filesystem server, and denylist coverage
# ---------------------------------------------------------------------------

def test_filesystem_server_is_pinned_in_the_proxy_command(local_home, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cmd = tc._build_proxy_command("npx", str(repo))
    assert tc.FS_SERVER_PACKAGE == "@modelcontextprotocol/server-filesystem@2.0.0"
    idx = cmd.index(tc.FS_SERVER_PACKAGE)
    assert cmd[idx + 1:] == [str(repo)]
    assert "@modelcontextprotocol/server-filesystem" not in cmd


def test_set_roots_finds_the_pinned_package(local_home, tmp_path):
    old = tmp_path / "old"
    keep = tmp_path / "keep"
    old.mkdir()
    keep.mkdir()
    base = ["npx", "-y", tc.FS_SERVER_PACKAGE, str(old)]
    new_cmd, changed = tc._set_fs_roots_on_cmd(base, [str(keep)])
    assert changed is True
    assert new_cmd[-1] == str(keep)


@pytest.mark.parametrize("rel", [".config/gh", ".config/gh/hosts.yml", ".config/gcloud", ".config/op"])
def test_cli_credential_folders_are_denied(local_home, rel):
    assert tc._is_denied_root(str(local_home / rel))


def test_github_cli_and_gcloud_appdata_folders_are_denied(local_home, monkeypatch):
    import os
    appdata = os.environ["APPDATA"]
    from pathlib import Path
    assert tc._is_denied_root(str(Path(appdata) / "GitHub CLI"))
    assert tc._is_denied_root(str(Path(appdata) / "gcloud"))


def test_unrelated_config_folder_is_allowed(local_home):
    assert not tc._is_denied_root(str(local_home / ".config" / "nvim"))
