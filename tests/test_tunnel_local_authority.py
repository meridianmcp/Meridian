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
import sys
from pathlib import Path

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
        "allow_tunnel_writes": False, "enable_shell_slot": False,
        "custom_plugins": {}, "builtin_commands": {},
    }


def test_only_a_literal_true_turns_a_switch_on(local_home):
    _write_policy({"allow_tunnel_writes": "yes", "enable_shell_slot": 1})
    policy = tc._load_local_policy()
    assert policy["allow_tunnel_writes"] is False
    assert policy["enable_shell_slot"] is False


def test_literal_true_turns_the_switches_on(local_home):
    _write_policy({"allow_tunnel_writes": True, "enable_shell_slot": True})
    policy = tc._load_local_policy()
    assert policy["allow_tunnel_writes"] is True
    assert policy["enable_shell_slot"] is True


def test_malformed_policy_falls_back_to_defaults(local_home):
    _write_policy("not json{")
    assert tc._load_local_policy()["allow_tunnel_writes"] is False


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


def _refused(msg, label="fs"):
    refusal = tc._guard_request(msg, label, tc._LOCKED_POLICY)
    return refusal[0] if refusal else None


@pytest.mark.parametrize("name", [
    "write_file", "edit_file", "create_directory", "move_file",
    "filesystem__write_file", "Filesystem: write_file", "Filesystem: move_file",
])
def test_write_tool_call_is_refused(name):
    denial = _refused(_request_msg(_call(name)))
    assert denial is not None
    assert denial["status"] == 403
    assert denial["id"] == "r1"
    body = _denial_body(denial)
    assert body["id"] == 7
    assert body["error"]["code"] == -32001


@pytest.mark.parametrize("name", ["read_text_file", "list_directory", "search_files", "Filesystem: read_text_file"])
def test_read_tool_call_passes(name):
    assert _refused(_request_msg(_call(name))) is None


def test_batch_with_one_write_is_refused():
    batch = [_call("read_text_file", 1), _call("write_file", 2)]
    denial = _refused(_request_msg(batch))
    assert denial is not None
    assert _denial_body(denial)["error"]["code"] == -32001


def test_unparseable_body_naming_a_write_tool_is_refused():
    body = base64.b64encode(b'{"method": "tools/call", "params": {"name": "write_file"').decode()
    denial = _refused({"id": "r2", "body": body})
    assert denial is not None
    assert denial["status"] == 403


def test_unparseable_body_is_refused_even_when_it_names_nothing():
    # A body that is not JSON cannot be checked, so it is refused whole: escapes, gzip and other encodings included.
    body = base64.b64encode(b"not json at all").decode()
    assert _refused({"id": "r3", "body": body}) is not None


def test_request_without_body_passes():
    assert _refused({"id": "r4", "method": "GET", "path": "/mcp"}) is None


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


# ---------------------------------------------------------------------------
# programs and writes that run through the other slots
# ---------------------------------------------------------------------------

def test_exec_slots_are_the_programs_that_run_on_this_machine():
    assert tc.EXEC_SLOT_NAMES == frozenset({"desktop-commander", "mcp-debugger", "meridian-latex"})


@pytest.mark.parametrize("slot", sorted(tc.EXEC_SLOT_NAMES))
def test_exec_slot_stays_off_without_the_local_switch(local_home, slot):
    plugins = resolve_plugins(None, detected_slots=[])
    for p in plugins:
        if p["name"] == slot:
            p["enabled"] = True
    enforced = tc._enforce_local_slot_policy(plugins, tc._load_local_policy())
    entry = next(p for p in enforced if p["name"] == slot)
    assert entry["enabled"] is False


def test_exec_slot_starts_only_when_the_local_policy_enables_it(local_home):
    _write_policy({"enable_shell_slot": True})
    plugins = resolve_plugins(None, detected_slots=[])
    for p in plugins:
        if p["name"] == "mcp-debugger":
            p["enabled"] = True
    enforced = tc._enforce_local_slot_policy(plugins, tc._load_local_policy())
    entry = next(p for p in enforced if p["name"] == "mcp-debugger")
    assert entry["enabled"] is True


@pytest.mark.parametrize("label,name", [
    ("extract", "execute_shell_command"),
    ("extract", "replace_symbol_body"),
    ("docs", "write_section"),
    ("outputs", "register_artifact"),
    ("word", "save_document"),
    ("code", "index_repository"),
])
def test_tunnel_write_or_exec_tool_is_refused_by_default(label, name):
    assert _refused(_request_msg(_call(name)), label=label) is not None


def test_serena_shell_passes_only_with_the_local_shell_switch():
    msg = _request_msg(_call("execute_shell_command"))
    assert tc._guard_request(msg, "extract", {**tc._LOCKED_POLICY, "enable_shell_slot": True}) is None
    assert tc._guard_request(msg, "extract", tc._LOCKED_POLICY) is not None


def test_docs_write_passes_only_with_the_local_tunnel_write_switch():
    msg = _request_msg(_call("write_section"))
    assert tc._guard_request(msg, "docs", {**tc._LOCKED_POLICY, "allow_tunnel_writes": True}) is None
    assert tc._guard_request(msg, "docs", tc._LOCKED_POLICY) is not None


def _read_request(path: str) -> dict:
    return _request_msg({"jsonrpc": "2.0", "id": 8, "method": "tools/call",
                         "params": {"name": "read_text_file", "arguments": {"path": path}}})


@pytest.mark.parametrize("path", [".env", "config/.env.local", "certs/server.pem", "id_rsa", ".git/config"])
def test_read_of_a_key_or_credential_file_is_refused(path):
    assert _refused(_read_request(path)) is not None


@pytest.mark.parametrize("path", [".env.example", "docs/readme.md"])
def test_read_of_an_ordinary_or_template_file_passes(path):
    assert _refused(_read_request(path)) is None


def test_resource_read_of_a_key_file_is_refused():
    msg = _request_msg({"jsonrpc": "2.0", "id": 5, "method": "resources/read",
                        "params": {"uri": "file:///C:/repo/certs/server.key"}})
    assert _refused(msg) is not None


def test_relayed_body_has_a_meridian_token_redacted():
    text = '{"result": {"text": "token sk_meridian_abcdefghijklmnop0123 here"}}'
    resp = {"type": "response", "id": "r", "status": 200, "body": base64.b64encode(text.encode()).decode()}
    out = base64.b64decode(tc._redact_response(resp)["body"]).decode()
    assert "sk_meridian_abcdefghijklmnop0123" not in out
    assert "redacted by the Meridian tunnel" in out


def test_relayed_body_without_secrets_passes_unchanged():
    body = base64.b64encode(b'{"result": {"text": "hello"}}').decode()
    resp = {"type": "response", "id": "r", "status": 200, "body": body}
    assert tc._redact_response(resp) is resp


@pytest.mark.parametrize("root", [
    "C:\\Users\\me\\Docs & calc",
    "C:\\Users\\me\\Projects C:\\Users\\me\\.ssh",
    "C:\\Users\\me\\a|b",
    "C:\\Users\\me\\100%",
    "C:\\Users\\me\\Notes\nx",
])
def test_windows_root_with_a_space_or_cmd_character_is_refused(root):
    assert tc._unsafe_windows_root(root, platform="win32") is True


def test_windows_plain_root_is_served():
    assert tc._unsafe_windows_root("C:\\Users\\me\\Documents\\Notes", platform="win32") is False


def test_posix_root_with_a_space_is_not_refused_for_that():
    assert tc._unsafe_windows_root("/home/me/My Notes", platform="linux") is False


@pytest.mark.skipif(sys.platform != "win32", reason="the root check reads the real platform")
def test_served_roots_drop_a_windows_root_that_would_smuggle_a_folder(local_home):
    smuggled = str(local_home / "Projects") + " " + str(local_home / ".ssh")
    assert tc._allowed_served_roots([smuggled, str(local_home / "Notes")]) == [str(local_home / "Notes")]


# ---------------------------------------------------------------------------
# review fixes: extract-pool credential, Serena roots, key-file names, slots with no write list
# ---------------------------------------------------------------------------

def test_extract_pool_connect_sends_the_bearer_header(local_home, monkeypatch):
    captured: dict = {}

    def fake_connect(url, **kwargs):
        captured.update(kwargs)
        raise RuntimeError("stop after the connect call")

    monkeypatch.setattr(websockets, "connect", fake_connect)
    ws_url = tc._authed_url("wss://usemeridian.us/tunnel-extract/tenant-1", "sk_meridian_secret123")
    pool = type("Pool", (), {"default_repo_path": None, "_normalize": staticmethod(lambda p: p)})()
    with pytest.raises(RuntimeError, match="stop after"):
        asyncio.run(tc._run_extract_pool_connection(ws_url, pool, str(local_home), label="extract"))
    headers = captured.get("additional_headers") or captured.get("extra_headers") or {}
    assert headers == {"Authorization": "Bearer sk_meridian_secret123"}


@pytest.mark.parametrize("root_of", [
    lambda home: "",
    lambda home: "relative/project",
    lambda home: str(home),
    lambda home: str(home / ".ssh"),
    lambda home: str(Path(home).anchor),
])
def test_serena_root_refuses_home_drive_root_and_credential_folders(local_home, root_of):
    assert tc._serena_root_refusal(root_of(local_home)) is not None


def test_serena_root_accepts_a_project_folder(local_home):
    assert tc._serena_root_refusal(str(local_home / "proj")) is None


@pytest.mark.skipif(sys.platform != "win32", reason="drive-letter spellings only exist on Windows")
@pytest.mark.parametrize("spelling", [
    "\\\\?\\{ssh}",
    "\\\\localhost\\{admin}\\{rest}",
    "\\\\?\\UNC\\localhost\\{admin}\\{rest}",
])
def test_denylist_sees_through_extended_length_and_admin_share_spellings(local_home, spelling):
    ssh = local_home / ".ssh"
    drive = Path(local_home).drive.rstrip(":")
    rest = str(ssh)[len(Path(local_home).anchor):]
    assert tc._is_denied_root(spelling.format(ssh=str(ssh), admin=f"{drive}$", rest=rest))


def test_windows_path_text_leaves_other_paths_alone():
    assert tc._windows_path_text("/home/me/notes") == "/home/me/notes"
    assert tc._windows_path_text("C:\\Users\\me\\notes") == "C:\\Users\\me\\notes"


@pytest.mark.parametrize("name", ["id_rsa.", "id_rsa::$DATA", ".env.", "server.key.", "server.pem::$DATA"])
def test_key_file_names_with_a_trailing_dot_or_stream_are_refused(name):
    assert _refused(_read_request(name)) is not None


@pytest.mark.parametrize("name", [".env.example.", "docs.md."])
def test_template_names_with_a_trailing_dot_still_pass(name):
    assert _refused(_read_request(name)) is None


def test_alternate_data_stream_on_any_file_is_refused():
    assert _refused(_read_request("docs.md::$DATA")) is not None


@pytest.mark.parametrize("label,name", [("ppt", "add_slide"), ("zotero", "search_items")])
def test_unlisted_slot_calls_are_refused_by_default(label, name):
    assert _refused(_request_msg(_call(name)), label=label) is not None


def test_unlisted_slot_calls_pass_when_the_local_policy_allows_writes():
    msg = _request_msg(_call("add_slide"))
    assert tc._guard_request(msg, "ppt", {**tc._LOCKED_POLICY, "allow_tunnel_writes": True}) is None


# ---------------------------------------------------------------------------
# ambiguous spellings are refused outright; unreadable bodies are refused whole
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", [
    "\\\\?\\C:\\Users\\me\\.ssh",
    "\\\\?\\UNC\\localhost\\C$\\Users\\me",
    "\\\\localhost\\C$\\Users\\me",
    "//localhost/C$/Users/me",
    "//?/C:/Users/me",
    "\\\\.\\C:\\Users\\me",
    "C:server.key",
    "C:",
    "C:\\Users\\me\\..\\other",
    "C:\\PROGRA~1\\x",
    "ENV~1.PRO",
    "C:\\Users\\me\\proj:stream\\x",
    "bad\x01name",
])
def test_ambiguous_path_spelling_is_refused(path):
    from meridian.repo_scope import ambiguous_path_spelling  # noqa: PLC0415
    assert ambiguous_path_spelling(path) is not None


@pytest.mark.parametrize("path", [
    "C:\\Users\\me\\proj",
    "C:/Users/me/proj",
    "/home/me/proj",
    "proj/src/main.py",
    "docs/readme.md",
])
def test_plain_path_spelling_is_accepted(path):
    from meridian.repo_scope import ambiguous_path_spelling  # noqa: PLC0415
    assert ambiguous_path_spelling(path) is None


@pytest.mark.parametrize("name", ["ENV~1.PRO", "ID_ED2~1", "C:server.key", "id_rsa\\."])
def test_tool_argument_with_an_ambiguous_spelling_is_refused(name):
    assert _refused(_read_request(name), label="extract") is not None


def test_unparseable_body_naming_a_write_tool_still_refused_on_any_slot():
    body = base64.b64encode(b'{"method": "tools/call", "params": {"name": "write_file"').decode()
    assert _refused({"id": "r9", "body": body}, label="ppt") is not None


def test_resources_read_with_an_ambiguous_uri_is_refused():
    msg = _request_msg({"jsonrpc": "2.0", "id": 6, "method": "resources/read",
                        "params": {"uri": "file://localhost/C$/Users/me/notes.txt"}})
    assert _refused(msg) is not None


# ---------------------------------------------------------------------------
# the extract loop: a caller's root header or set_active_repo cannot pick a credential folder
# ---------------------------------------------------------------------------

class _FakePool:
    default_repo_path = None

    def __init__(self):
        self.spawned: list[str] = []

    def _normalize(self, path):
        return path

    def get_or_spawn(self, repo_path, on_launch=None):
        self.spawned.append(repo_path)
        raise RuntimeError("no Serena daemon in tests")


class _FakeExtractWS:
    def __init__(self, messages):
        self._messages = list(messages)
        self.sent: list = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._messages:
            raise StopAsyncIteration
        return self._messages.pop(0)

    async def send(self, data):
        self.sent.append(data)


def _run_extract_loop(monkeypatch, messages, default_path):
    pool = _FakePool()
    ws = _FakeExtractWS(messages)
    monkeypatch.setattr(websockets, "connect", lambda url, **kw: ws)
    url = tc._authed_url("wss://usemeridian.us/tunnel-extract/tenant-1", "sk_meridian_secret123")
    asyncio.run(tc._run_extract_pool_connection(url, pool, default_path, label="extract"))
    return pool


def test_extract_loop_replaces_a_credential_folder_header_with_the_tunnel_default(local_home, monkeypatch):
    project = str(local_home / "proj")
    denied_header = {"X-Meridian-Repo-Path": str(local_home / ".ssh")}
    request = json.dumps({"type": "request", "id": "1", "method": "POST", "path": "/mcp",
                          "headers": denied_header, "body": ""})
    pool = _run_extract_loop(monkeypatch, [request], project)
    assert pool.spawned == [project]


def test_extract_loop_refuses_set_active_repo_into_a_credential_folder(local_home, monkeypatch):
    project = str(local_home / "proj")
    set_repo = json.dumps({"type": "set_active_repo", "repo_path": str(local_home / ".ssh")})
    plain = json.dumps({"type": "request", "id": "2", "method": "POST", "path": "/mcp", "headers": {}, "body": ""})
    pool = _run_extract_loop(monkeypatch, [set_repo, plain], project)
    assert pool.default_repo_path is None
    assert pool.spawned == [project]


# ---------------------------------------------------------------------------
# the refinements: path-named arguments only, mixed prefixes, credential folders, unreadable bodies
# ---------------------------------------------------------------------------

def test_multiline_file_content_with_tilde_and_control_character_is_not_refused():
    msg = _request_msg({"jsonrpc": "2.0", "id": 11, "method": "tools/call",
                        "params": {"name": "write_file", "arguments": {
                            "path": "notes.md", "content": "line one\nsee HEAD~1\n// copyright\x01\n"}}})
    assert tc._guard_request(msg, "fs", {**tc._LOCKED_POLICY, "allow_tunnel_writes": True}) is None


def test_shell_command_with_tilde_and_dot_slash_is_not_refused_for_its_spelling():
    msg = _request_msg({"jsonrpc": "2.0", "id": 12, "method": "tools/call",
                        "params": {"name": "execute_shell_command",
                                   "arguments": {"command": "git show HEAD~1 --stat && ./gradlew test"}}})
    assert tc._guard_request(msg, "extract", {**tc._LOCKED_POLICY, "enable_shell_slot": True}) is None


def test_dot_segment_on_a_plain_folder_read_is_allowed():
    msg = _request_msg({"jsonrpc": "2.0", "id": 13, "method": "tools/call",
                        "params": {"name": "list_directory", "arguments": {"path": "."}}})
    assert _refused(msg) is None


@pytest.mark.parametrize("path", ["/\\localhost\\C$\\Users\\me", "\\/?/C:/Users/me", "//\\?\\C:\\x"])
def test_mixed_separator_prefixes_are_refused(path):
    from meridian.repo_scope import ambiguous_path_spelling  # noqa: PLC0415
    assert ambiguous_path_spelling(path) is not None


@pytest.mark.parametrize("path", [
    ".aws/credentials", ".ssh/config", "home/.kube/config",
    ".config/gh/hosts.yml", ".config/gcloud/creds.db", "x/.gnupg/random",
])
def test_files_inside_credential_folders_are_refused(path):
    assert _refused(_read_request(path)) is not None


def test_ordinary_config_folder_file_passes():
    assert _refused(_read_request(".config/nvim/init.lua")) is None


@pytest.mark.parametrize("path", ["proj/src/../secret.txt", "C:\\Users\\me\\.."])
def test_parent_segments_are_refused(path):
    assert _refused(_read_request(path)) is not None


def test_relative_served_root_is_refused():
    assert tc._allowed_served_roots(["relative/project"]) == []


def test_alternate_data_stream_directory_spelling_is_refused():
    from meridian.repo_scope import ambiguous_path_spelling  # noqa: PLC0415
    assert ambiguous_path_spelling("C:\\Users\\me\\proj:stream\\x") is not None


def test_resources_read_of_a_drive_relative_key_name_is_refused():
    msg = _request_msg({"jsonrpc": "2.0", "id": 14, "method": "resources/read",
                        "params": {"uri": "file:///C:server.key"}})
    assert _refused(msg) is not None


def test_undecodable_base64_body_is_refused():
    assert _refused({"id": "r15", "body": "!!!not base64!!!"}) is not None


def test_over_deep_json_body_is_refused_not_crashed_on():
    deep = "[" * 100000 + "]" * 100000
    body = base64.b64encode(deep.encode()).decode()
    assert _refused({"id": "r16", "body": body}) is not None


# ---------------------------------------------------------------------------
# the credential-folder boundary: credential folders are refused, project config is not
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", [".meridian/tool-routing.toml", ".docker/compose.yml", "proj/.meridian/tool-routing.toml"])
def test_project_config_in_a_meridian_or_docker_folder_still_passes(path):
    assert _refused(_read_request(path)) is None


@pytest.mark.parametrize("path", [".docker/config.json", "home/.docker/config.json"])
def test_docker_registry_config_is_refused_wherever_it_sits(path):
    assert _refused(_read_request(path)) is not None


@pytest.mark.parametrize("path", [".aws", ".ssh", "proj/.aws", "proj/.config/gh"])
def test_a_credential_folder_named_as_the_path_is_refused(path):
    # A search or listing rooted at the folder would return the files inside it, so the folder itself is refused.
    assert _refused(_read_request(path)) is not None
