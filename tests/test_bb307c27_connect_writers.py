"""bb307c27 -- meridian_connect.py Codex / Cursor writers must not damage user config.

Found by the 2026-09-27 install/tunnel audit:

- Codex: ``re.sub(r"\\[hooks\\].*?...")`` deleted the user's WHOLE ``[hooks]``
  table (their own hooks included) on every run, and MCP auth was sent as
  ``type = "http"`` + ``api_key`` -- neither of which is a documented Codex MCP
  field (Codex documents ``url`` + ``bearer_token_env_var`` / ``http_headers`` /
  ``env_http_headers``; verified against the Codex MCP docs on 2026-09-28).
- Cursor: ``<cwd>/.cursor/mcp.json`` was overwritten wholesale (dropping every
  other MCP server), held the raw token, and could sit inside a git repo.

Everything runs against temp dirs and fixture values; the real home directory,
real Codex/Cursor config and any real token are never touched.
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import types
from pathlib import Path

import pytest

_CONNECT_PY = Path(__file__).resolve().parent.parent / "scripts" / "meridian_connect.py"
_FAKE = "sk_meridian_faketoken_not_real_0000"  # noqa: S105 -- fixture value
_URL = "https://meridian.example.test"
_START = "curl -s -X POST -K \"/h/hook_auth.conf\" 'https://meridian.example.test/hooks/session-start'"
_STOP = "curl -s -X POST -K \"/h/hook_auth.conf\" 'https://meridian.example.test/hooks/stop' >/dev/null 2>&1"

tomllib = pytest.importorskip("tomllib")


def _load():
    spec = importlib.util.spec_from_file_location("meridian_connect_bb307c27", _CONNECT_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load()


USER_CONFIG = '''\
# my codex config
model = "gpt-5"

[hooks]
# my own hook -- must survive
my_hook = "echo mine"
other = ["a", "b"]

[mcp_servers.other]
command = "npx"
args = ["-y", "some-server"]

[tui]
theme = "dark"
'''


def _merge(mod, text, token=_FAKE):
    return mod._merge_codex_config(text, _URL, token, _START, _STOP)


def _subsequence(needles, haystack):
    it = iter(haystack)
    return all(any(n == h for h in it) for n in needles)


# ---------------------------------------------------------------------------
# Codex -- merge, never regex-delete
# ---------------------------------------------------------------------------

def test_codex_merge_preserves_user_hooks_and_everything_else(mod):
    out, notes = _merge(mod, USER_CONFIG)
    assert notes == []
    parsed = tomllib.loads(out)
    # the user's own [hooks] content survives ...
    assert parsed["hooks"]["my_hook"] == "echo mine"
    assert parsed["hooks"]["other"] == ["a", "b"]
    # ... Meridian's hooks were added to the SAME table ...
    assert parsed["hooks"]["session_start"] == _START
    assert parsed["hooks"]["stop"] == _STOP
    # ... and every other table / key is intact.
    assert parsed["model"] == "gpt-5"
    assert parsed["mcp_servers"]["other"] == {"command": "npx", "args": ["-y", "some-server"]}
    assert parsed["tui"] == {"theme": "dark"}
    # Every original line (comments included) is still there, in order.
    original = [ln for ln in USER_CONFIG.splitlines() if ln.strip()]
    assert _subsequence(original, out.splitlines())


def test_codex_merge_uses_documented_http_headers_not_type_or_api_key(mod):
    out, _ = _merge(mod, USER_CONFIG)
    meridian = tomllib.loads(out)["mcp_servers"]["meridian"]
    assert meridian["url"] == f"{_URL}/mcp"
    assert meridian["http_headers"] == {"Authorization": f"Bearer {_FAKE}"}
    assert "type" not in meridian and "api_key" not in meridian


def test_codex_merge_without_token_writes_no_auth(mod):
    out, _ = _merge(mod, USER_CONFIG, token="")
    meridian = tomllib.loads(out)["mcp_servers"]["meridian"]
    assert meridian == {"url": f"{_URL}/mcp"}


def test_codex_merge_is_idempotent(mod):
    once, _ = _merge(mod, USER_CONFIG)
    twice, _ = _merge(mod, once)
    assert twice == once
    assert twice.count("[mcp_servers.meridian]") == 1
    assert twice.count("session_start =") == 1


def test_codex_merge_migrates_the_legacy_meridian_block_without_touching_user_keys(mod):
    legacy = (
        USER_CONFIG
        + '\n[mcp_servers.meridian]\ntype = "http"\nurl = "https://old.example.test/mcp"\n'
        + 'api_key = "sk_meridian_old_token_fixture"\n'
    )
    # the pre-fix writer put session_start/stop in [hooks] too
    legacy = legacy.replace(
        'my_hook = "echo mine"',
        'my_hook = "echo mine"\nsession_start = "curl https://old.example.test/hooks/session-start"',
    )
    out, notes = _merge(mod, legacy)
    assert notes == []
    parsed = tomllib.loads(out)
    assert parsed["hooks"]["my_hook"] == "echo mine"
    assert parsed["hooks"]["session_start"] == _START, "Meridian's own stale hook is replaced"
    assert parsed["mcp_servers"]["meridian"]["url"] == f"{_URL}/mcp"
    assert "api_key" not in out and 'type = "http"' not in out
    assert "sk_meridian_old_token_fixture" not in out
    assert out.count("[mcp_servers.meridian") == 1


def test_codex_merge_never_overwrites_a_users_own_hook_of_the_same_name(mod):
    text = '[hooks]\nsession_start = "echo my own session hook"\n'
    out, notes = _merge(mod, text)
    parsed = tomllib.loads(out)
    assert parsed["hooks"]["session_start"] == "echo my own session hook"
    assert parsed["hooks"]["stop"] == _STOP, "the un-clashing key is still added"
    assert any("session_start" in n and "untouched" in n for n in notes)


def test_codex_merge_creates_hooks_table_when_absent(mod):
    out, _ = _merge(mod, 'model = "x"\n')
    parsed = tomllib.loads(out)
    assert parsed["model"] == "x"
    assert parsed["hooks"] == {"session_start": _START, "stop": _STOP}


def test_codex_merge_into_empty_or_missing_file(mod):
    out, _ = _merge(mod, "")
    parsed = tomllib.loads(out)
    assert parsed["mcp_servers"]["meridian"]["url"] == f"{_URL}/mcp"
    assert parsed["hooks"]["stop"] == _STOP


def test_codex_merge_ignores_header_lookalikes_inside_multiline_strings_and_arrays(mod):
    text = (
        'notes = """\n[hooks]\nnot a table\n"""\n'
        "matrix = [\n  [1, 2]\n]\n"
        "single = '''\n[mcp_servers.meridian]\n'''\n"
        '\n[hooks]\nmine = "x"\n'
    )
    out, notes = _merge(mod, text)
    assert notes == []
    parsed = tomllib.loads(out)
    assert parsed["notes"] == "[hooks]\nnot a table\n"
    assert parsed["matrix"] == [[1, 2]]
    assert parsed["single"] == "[mcp_servers.meridian]\n"
    assert parsed["hooks"]["mine"] == "x" and parsed["hooks"]["session_start"] == _START
    assert parsed["mcp_servers"]["meridian"]["url"] == f"{_URL}/mcp"


def test_codex_merge_leaves_unsafe_meridian_definitions_untouched(mod):
    text = '[mcp_servers]\nmeridian = { url = "https://x.example.test/mcp" }\n'
    out, notes = _merge(mod, text)
    parsed = tomllib.loads(out)  # still valid TOML: no duplicate table was appended
    assert parsed["mcp_servers"]["meridian"] == {"url": "https://x.example.test/mcp"}
    assert any("cannot edit safely" in n for n in notes)
    assert _FAKE not in "\n".join(notes), "warnings must never echo the token"


def test_codex_merge_preserves_crlf_line_endings(mod):
    text = USER_CONFIG.replace("\n", "\r\n")
    out, _ = _merge(mod, text)
    assert "\r\n" in out and "\n" not in out.replace("\r\n", "")
    assert tomllib.loads(out)["hooks"]["my_hook"] == "echo mine"


def test_codex_merge_escapes_hook_commands_into_valid_toml(mod):
    cmd = 'curl -d "{\\"cwd\\":\\"$PWD\\"}" \'https://meridian.example.test/hooks/stop\''
    out, _ = mod._merge_codex_config("", _URL, "", _START, cmd)
    assert tomllib.loads(out)["hooks"]["stop"] == cmd


def test_configure_codex_end_to_end_backs_up_once_and_restricts_the_token_file(mod, tmp_path, monkeypatch):
    cfg = tmp_path / ".codex" / "config.toml"
    cfg.parent.mkdir()
    cfg.write_text(USER_CONFIG, encoding="utf-8")
    restricted: list[Path] = []
    monkeypatch.setattr(mod, "_restrict_to_owner", lambda p: restricted.append(Path(p)) or True)

    ok, notes = mod._configure_codex(cfg, _URL, _FAKE, _START, _STOP)
    assert ok
    backup = cfg.with_name("config.toml.meridian-bak")
    assert backup.read_text(encoding="utf-8") == USER_CONFIG, "pristine original kept"
    assert restricted == [cfg], "the file that now holds the token must be locked down"
    parsed = tomllib.loads(cfg.read_text(encoding="utf-8"))
    assert parsed["hooks"]["my_hook"] == "echo mine"
    assert not list(cfg.parent.glob("*.meridian-tmp")), "no temp file left behind"

    # a re-run must not clobber the pristine backup with Meridian's own output
    mod._configure_codex(cfg, _URL, _FAKE, _START, _STOP)
    assert backup.read_text(encoding="utf-8") == USER_CONFIG


def test_configure_codex_leaves_an_unreadable_file_alone(mod, tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_bytes(b"\xff\xfe\x00 not valid utf-8 \xff")
    before = cfg.read_bytes()
    ok, notes = mod._configure_codex(cfg, _URL, _FAKE, _START, _STOP)
    assert ok is False and cfg.read_bytes() == before
    assert any("could not read" in n for n in notes)


def test_codex_writer_no_longer_uses_the_destructive_regex_or_undocumented_fields():
    src = _CONNECT_PY.read_text(encoding="utf-8")
    assert "_re.sub" not in src, "the regex that deleted the whole [hooks] table must be gone"
    # (the emitted output is asserted behaviourally above; these pin the old f-strings)
    assert "api_key = \"{token}\"" not in src
    assert "type = \"http\"\\n" not in src


# ---------------------------------------------------------------------------
# Cursor -- merge into the user-global file; never touch the repo folder
# ---------------------------------------------------------------------------

@pytest.fixture()
def cursor_home(mod, tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".cursor").mkdir(parents=True)
    monkeypatch.setattr(mod.Path, "home", staticmethod(lambda: home))
    monkeypatch.setattr(mod, "_restrict_to_owner", lambda p: True)
    return home


def test_cursor_merge_keeps_other_servers_and_top_level_keys(mod, cursor_home):
    path = cursor_home / ".cursor" / "mcp.json"
    original = {
        "mcpServers": {
            "github": {"url": "https://api.example.test/mcp", "headers": {"Authorization": "Bearer other"}},
            "local": {"command": "npx", "args": ["-y", "thing"]},
        },
        "someOtherKey": {"keep": True},
    }
    path.write_text(json.dumps(original), encoding="utf-8")

    ok, msg = mod._configure_cursor(_URL, _FAKE)
    assert ok, msg
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["mcpServers"]["github"] == original["mcpServers"]["github"]
    assert data["mcpServers"]["local"] == original["mcpServers"]["local"]
    assert data["someOtherKey"] == {"keep": True}
    assert data["mcpServers"]["meridian"] == {
        "url": f"{_URL}/mcp",
        "headers": {"Authorization": f"Bearer {_FAKE}"},
    }
    assert json.loads(path.with_name("mcp.json.meridian-bak").read_text(encoding="utf-8")) == original


def test_cursor_merge_replaces_only_the_meridian_entry_and_is_idempotent(mod, cursor_home):
    path = cursor_home / ".cursor" / "mcp.json"
    path.write_text(json.dumps({"mcpServers": {"meridian": {"url": "https://old.example.test/mcp"}}}), encoding="utf-8")
    assert mod._configure_cursor(_URL, "")[0]
    first = path.read_text(encoding="utf-8")
    assert json.loads(first)["mcpServers"] == {"meridian": {"url": f"{_URL}/mcp"}}
    assert mod._configure_cursor(_URL, "")[0]
    assert path.read_text(encoding="utf-8") == first


def test_cursor_merge_creates_the_file_when_missing(mod, tmp_path, monkeypatch):
    home = tmp_path / "fresh-home"
    monkeypatch.setattr(mod.Path, "home", staticmethod(lambda: home))
    monkeypatch.setattr(mod, "_restrict_to_owner", lambda p: True)
    ok, _ = mod._configure_cursor(_URL, _FAKE)
    assert ok
    data = json.loads((home / ".cursor" / "mcp.json").read_text(encoding="utf-8"))
    assert list(data["mcpServers"]) == ["meridian"]


@pytest.mark.parametrize(
    "content",
    ['{\n  // a comment\n  "mcpServers": {}\n}', "[1, 2, 3]", '{"mcpServers": []}', "{not json"],
    ids=["jsonc-comment", "not-an-object", "servers-not-an-object", "broken"],
)
def test_cursor_merge_refuses_to_clobber_a_file_it_cannot_parse(mod, cursor_home, content):
    path = cursor_home / ".cursor" / "mcp.json"
    path.write_text(content, encoding="utf-8")
    ok, msg = mod._configure_cursor(_URL, _FAKE)
    assert ok is False and "left untouched" in msg
    assert path.read_text(encoding="utf-8") == content
    assert _FAKE not in msg, "the warning must never echo the token"


def test_cursor_token_file_is_locked_down(mod, tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(mod.Path, "home", staticmethod(lambda: home))
    restricted: list[Path] = []
    monkeypatch.setattr(mod, "_restrict_to_owner", lambda p: restricted.append(Path(p)) or True)
    mod._configure_cursor(_URL, _FAKE)
    assert restricted == [home / ".cursor" / "mcp.json"]
    restricted.clear()
    mod._configure_cursor(_URL, "")  # no token -> nothing sensitive to protect
    assert restricted == []


def test_legacy_project_file_with_a_plaintext_token_is_reported_not_modified(mod, tmp_path):
    proj = tmp_path / "repo" / ".cursor"
    proj.mkdir(parents=True)
    legacy = proj / "mcp.json"
    legacy.write_text(json.dumps({"mcpServers": {"meridian": {"headers": {"Authorization": f"Bearer {_FAKE}"}}}}), encoding="utf-8")
    before = legacy.read_bytes()
    warning = mod._legacy_cursor_project_token_warning(tmp_path / "repo")
    assert warning and "plaintext" in warning and _FAKE not in warning
    assert legacy.read_bytes() == before
    assert mod._legacy_cursor_project_token_warning(tmp_path) == ""


# ---------------------------------------------------------------------------
# main() end to end: Codex + Cursor both "detected" on a temp home
# ---------------------------------------------------------------------------

@pytest.fixture()
def run_main(mod, tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".cursor").mkdir(parents=True)
    repo = tmp_path / "some-git-repo"
    (repo / ".git").mkdir(parents=True)
    for var in ("MERIDIAN_TOKEN", "MERIDIAN_API_KEY", "BEARER_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))
    monkeypatch.chdir(repo)

    def fake_http(method, url, *, token="", body=None, timeout=10):
        if url.endswith("/auth/me"):
            return {"email": "tester@example.test"}
        if url.endswith("/auth/tokens"):
            return {"token": "sk_meridian_permanent_fixture"}
        return {}

    real_which = shutil.which
    monkeypatch.setattr(mod, "_http", fake_http)
    monkeypatch.setattr(mod.shutil, "which", lambda c, *a, **k: None if c in ("claude", "codex", "cursor") else real_which(c, *a, **k))
    monkeypatch.setattr(mod.webbrowser, "open", lambda *_a, **_k: True)
    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(isatty=lambda: False))
    monkeypatch.setattr(sys, "argv", ["meridian_connect.py", "--url", _URL, "--token", "sk_meridian_cli_fixture"])
    return mod, home, repo


def test_main_merges_codex_and_cursor_and_writes_nothing_into_the_repo_folder(run_main):
    mod, home, repo = run_main
    (home / ".codex" / "config.toml").write_text(USER_CONFIG, encoding="utf-8")
    (home / ".cursor" / "mcp.json").write_text(
        json.dumps({"mcpServers": {"github": {"url": "https://api.example.test/mcp"}}}), encoding="utf-8"
    )
    assert mod.main() == 0

    codex = tomllib.loads((home / ".codex" / "config.toml").read_text(encoding="utf-8"))
    assert codex["hooks"]["my_hook"] == "echo mine"
    assert "session_start" in codex["hooks"] and "stop" in codex["hooks"]
    assert codex["mcp_servers"]["other"]["command"] == "npx"
    assert codex["mcp_servers"]["meridian"]["http_headers"] == {
        "Authorization": "Bearer sk_meridian_permanent_fixture"
    }

    cursor = json.loads((home / ".cursor" / "mcp.json").read_text(encoding="utf-8"))
    assert set(cursor["mcpServers"]) == {"github", "meridian"}

    # the current folder (a git repo) must not receive a token-bearing file
    assert not (repo / ".cursor").exists()
    leaked = [p for p in repo.rglob("*") if p.is_file() and b"sk_meridian_" in p.read_bytes()]
    assert not leaked, leaked
