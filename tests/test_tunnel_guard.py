"""The tunnel guard: writes, exec tools, and key or credential files refused per slot, and secrets redacted.

Covers:
  - Writes are refused on every slot by default, and allowed only with allow_tunnel_writes in the local policy.
  - Exec tools (Serena's shell tool) and the exec slots need enable_shell_slot.
  - Key and credential files are refused on every read tool, including through a symlink on the fs slot.
  - Ordinary files, templates, and read tools pass.
  - Credential-looking values are redacted from responses, in JSON and SSE bodies.
  - The write and exec sets stay in step with the servers that declare the tools.
"""
from __future__ import annotations

import asyncio
import base64
import json
import re
from pathlib import Path

import pytest

from meridian import tunnel_client as tc

_REPO = Path(__file__).resolve().parents[1]


def _request_msg(payload) -> dict:
    body = base64.b64encode(json.dumps(payload).encode()).decode()
    return {"type": "request", "id": "r1", "method": "POST", "path": "/mcp", "body": body}


def _call(name: str, arguments: dict | None = None, call_id: int = 7) -> dict:
    return {"jsonrpc": "2.0", "id": call_id, "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}}}


def _refused(msg, label="fs", policy=None):
    refusal = tc._guard_request(msg, label, policy or tc._LOCKED_POLICY)
    return refusal[0] if refusal else None


def _denial_message(denial: dict) -> str:
    return json.loads(base64.b64decode(denial["body"]))["error"]["message"]


# ---------------------------------------------------------------------------
# writes and exec tools
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("label,tool", [
    ("word", "add_paragraph"), ("word", "save_document"), ("docs", "write_section"),
    ("docs", "insert_image"), ("outputs", "register_artifact"), ("outputs", "tag_output"),
    ("extract", "replace_symbol_body"), ("extract", "create_text_file"), ("code", "index_repository"),
    ("fs", "move_file"), (None, "write_section"),
])
def test_write_tools_are_refused_per_slot_by_default(label, tool):
    denial = _refused(_request_msg(_call(tool)), label=label)
    assert denial is not None
    assert denial["status"] == 403
    assert "allow_tunnel_writes" in _denial_message(denial)


@pytest.mark.parametrize("label,tool", [
    ("word", "add_paragraph"), ("docs", "write_section"), ("outputs", "register_artifact"),
    ("extract", "replace_symbol_body"), ("fs", "write_file"),
])
def test_write_tools_pass_when_the_local_policy_allows_writes(label, tool):
    policy = dict(tc._LOCKED_POLICY, allow_tunnel_writes=True)
    assert _refused(_request_msg(_call(tool)), label=label, policy=policy) is None


@pytest.mark.parametrize("label,tool", [
    ("docs", "get_document_review"), ("docs", "search_paragraphs"), ("outputs", "get_provenance"),
    ("fs", "read_text_file"), ("fs", "directory_tree"), ("extract", "find_symbol"), ("code", "search_graph"),
])
def test_read_tools_pass_by_default(label, tool):
    assert _refused(_request_msg(_call(tool)), label=label) is None


def test_serena_shell_tool_is_refused_unless_the_shell_switch_is_on():
    msg = _request_msg(_call("execute_shell_command", {"command": "whoami"}))
    assert _refused(msg, label="extract") is not None
    policy = dict(tc._LOCKED_POLICY, enable_shell_slot=True)
    assert _refused(msg, label="extract", policy=policy) is None


def test_exec_slots_stay_off_until_the_shell_switch_is_on(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("MERIDIAN_LOCAL_RUNNER_STATE_DIR", str(state))
    plugins = [{"name": name, "enabled": True} for name in ("mcp-debugger", "meridian-latex", "desktop-commander")]
    off = tc._enforce_local_slot_policy(plugins, tc._load_local_policy())
    assert all(p["enabled"] is False for p in off)
    (state / tc.LOCAL_POLICY_FILENAME).write_text(json.dumps({"enable_shell_slot": True}), encoding="utf-8")
    on = tc._enforce_local_slot_policy(plugins, tc._load_local_policy())
    assert all(p["enabled"] is True for p in on)


def test_refused_request_never_reaches_the_local_proxy():
    class _Client:
        called = False

        async def request(self, *args, **kwargs):
            self.called = True
            raise AssertionError("the local proxy must not be contacted for a refused request")

    client = _Client()
    msg = _request_msg(_call("write_file", {"path": "x", "content": "y"}))
    resp = asyncio.run(tc._relay_request(client, "http://127.0.0.1:1", msg, label="fs", policy=tc._LOCKED_POLICY))
    assert resp["status"] == 403
    assert client.called is False


# ---------------------------------------------------------------------------
# key and credential files
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", [
    ".env", ".env.local", ".env.production", "config/.env", "C:/repo/.env",
    "id_rsa", "id_ed25519", "deploy.pem", "server.key", "keys/client.p12",
    ".npmrc", ".netrc", "repo/.git/config", "secrets.json", "local_policy.json",
    "api_token", "verification_commands.json",
])
@pytest.mark.parametrize("tool", ["read_text_file", "read_file", "read_media_file"])
def test_key_and_credential_files_are_refused_on_every_read_tool(path, tool):
    denial = _refused(_request_msg(_call(tool, {"path": path})), label="fs")
    assert denial is not None, (tool, path)
    assert denial["status"] == 403


@pytest.mark.parametrize("path", [
    ".env.example", ".env.sample", "README.md", "src/app.py", "docs/notes.txt", "id_rsa.pub",
])
def test_ordinary_and_template_files_pass(path):
    assert _refused(_request_msg(_call("read_text_file", {"path": path})), label="fs") is None


def test_read_multiple_files_refuses_when_any_path_is_a_key_file():
    msg = _request_msg(_call("read_multiple_files", {"paths": ["README.md", "config/.env"]}))
    denial = _refused(msg, label="fs")
    assert denial is not None
    assert "credential" in _denial_message(denial)


def test_refusal_message_names_the_file_not_the_full_path():
    denial = _refused(_request_msg(_call("read_text_file", {"path": "/home/me/secret/server.key"})), label="fs")
    message = _denial_message(denial)
    assert "server.key" in message
    assert "/home/me" not in message


def test_symlink_to_a_key_file_is_refused_on_the_fs_slot(tmp_path):
    key = tmp_path / ".env"
    key.write_text("TOKEN=x", encoding="utf-8")
    link = tmp_path / "notes.txt"
    try:
        link.symlink_to(key)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks need privileges on this machine")
    denial = _refused(_request_msg(_call("read_text_file", {"path": str(link)})), label="fs")
    assert denial is not None


def test_resources_read_of_a_key_file_is_refused():
    msg = _request_msg({"jsonrpc": "2.0", "id": 9, "method": "resources/read",
                        "params": {"uri": "file:///repo/config/.env"}})
    assert _refused(msg, label="extract") is not None


def test_unparseable_request_naming_a_key_file_fails_closed():
    body = base64.b64encode(b'{"method": "tools/call", "params": {"arguments": {"path": ".env"').decode()
    assert _refused({"id": "r9", "body": body}, label="fs") is not None


def test_unparseable_request_naming_nothing_passes_the_guard():
    body = base64.b64encode(b"<<<not json>>>").decode()
    assert _refused({"id": "r10", "body": body}, label="fs") is None


# ---------------------------------------------------------------------------
# redaction of credential-looking values
# ---------------------------------------------------------------------------

PEM = "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA0abc\n-----END RSA PRIVATE KEY-----"


def _body_resp(text: str) -> dict:
    return {"type": "response", "id": "r", "status": 200, "headers": {},
            "body": base64.b64encode(text.encode("utf-8")).decode()}


def _decoded(resp: dict) -> str:
    return base64.b64decode(resp["body"]).decode("utf-8")


def test_private_key_block_is_redacted_inside_a_json_response():
    text = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": PEM}]}})
    out = _decoded(tc._redact_response(_body_resp(text)))
    assert "MIIEpAIBAAKCAQEA0abc" not in out
    assert "[redacted by the Meridian tunnel]" in out
    json.loads(out)  # still valid JSON


@pytest.mark.parametrize("value", [
    "sk_meridian_AbCdEfGhIjKlMnOp1234", "AKIAABCDEFGHIJKLMNOP", "ghp_" + "a" * 36,
    "xoxb-1234567890-abcdefghij", "sk-proj-" + "b" * 40,
])
def test_known_token_shapes_are_redacted(value):
    text = json.dumps({"result": {"content": [{"type": "text", "text": f"token is {value} here"}]}})
    out = _decoded(tc._redact_response(_body_resp(text)))
    assert value not in out


def test_env_style_assignment_value_is_redacted():
    text = json.dumps({"result": {"content": [{"type": "text", "text": "DATABASE_PASSWORD = hunter2hunter2"}]}})
    out = _decoded(tc._redact_response(_body_resp(text)))
    assert "hunter2hunter2" not in out


def test_sse_data_lines_are_redacted():
    payload = json.dumps({"result": {"content": [{"type": "text", "text": PEM}]}})
    text = f"event: message\ndata: {payload}\n\n"
    out = _decoded(tc._redact_response(_body_resp(text)))
    assert "MIIEpAIBAAKCAQEA0abc" not in out
    assert out.startswith("event: message")


def test_clean_response_passes_unchanged():
    resp = _body_resp(json.dumps({"result": {"content": [{"type": "text", "text": "def main(): pass"}]}}))
    assert tc._redact_response(resp) is resp


def test_binary_response_passes_unchanged():
    resp = {"type": "response", "id": "b", "status": 200, "headers": {},
            "body": base64.b64encode(bytes(range(256))).decode()}
    assert tc._redact_response(resp) is resp


# ---------------------------------------------------------------------------
# the write and exec sets stay in step with the servers that declare the tools
# ---------------------------------------------------------------------------

_READ_VERB = re.compile(
    r"^(get|list|read|find|query|inspect|check|verify|resolve|lookup|preview|compare|describe|explain|validate|lint|"
    r"outline|extract|scan|fetch|view|show|search|status|parse|export|locate|audit|plan|build_prose|document_outline|"
    r"format_reference|file_fingerprint|script_content_hash|npy_metadata|serialize|evaluate|list_render)"
)


def _declared_tools(path: Path) -> list[str]:
    import ast
    names = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for d in node.decorator_list:
                target = d.func if isinstance(d, ast.Call) else d
                if isinstance(target, ast.Attribute) and target.attr == "tool":
                    names.append(node.name)
    return names


@pytest.mark.parametrize("extension,label,writes", [
    ("extensions/meridian-outputs/meridian_outputs/server.py", "outputs", tc.OUTPUTS_WRITE_TOOLS),
    ("extensions/meridian-docs/meridian_docs/server.py", "docs", tc.DOCS_WRITE_TOOLS),
])
def test_every_declared_extension_tool_is_classified(extension, label, writes):
    """A new tool must be listed as a write, or have a read-style name. Otherwise this fails."""
    declared = _declared_tools(_REPO / extension)
    assert declared, f"no tools found in {extension}"
    unclassified = [n for n in declared if n not in writes and not _READ_VERB.match(n)]
    assert unclassified == [], f"{label} tools not classified as read or write: {unclassified}"


def test_word_and_docs_write_sets_cover_the_server_gate():
    from meridian.routes import tunnel as server_tunnel
    assert set(server_tunnel._WORD_WRITE_TOOLS) <= tc.WORD_WRITE_TOOLS
    assert set(server_tunnel._DOCS_WRITE_TOOLS.keys()) <= tc.DOCS_WRITE_TOOLS
