"""Pinned verification commands and the served-root denylist (tunnel client).

Covers the tunnel's remote-shell exposure fix:
  - run_verification runs only the command pinned in this machine's pins file,
    and the server supplies nothing but the project id.
  - A served root that is, contains, or sits inside a credential or token
    folder is refused by every path that serves a root.
"""
from __future__ import annotations

import inspect
import json
import os
from pathlib import Path

import pytest

from meridian import tunnel_client as tc
from meridian.local_runner import default_state_dir
from meridian.routes import tunnel as tun

PROJECT = "5787cc92-ba7d-4788-b17c-28ab7938b839"
PROXY_TOKEN = tc.FS_SERVER_PACKAGE


@pytest.fixture
def local_home(tmp_path, monkeypatch):
    """Point the home folder, APPDATA and the local-runner state dir at tmp_path so no real folder is read."""
    home = tmp_path / "home"
    home.mkdir()
    (tmp_path / "state").mkdir()
    appdata = tmp_path / "AppData" / "Roaming"
    appdata.mkdir(parents=True)
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setenv("MERIDIAN_LOCAL_RUNNER_STATE_DIR", str(tmp_path / "state"))
    return home


def _write_pins(data) -> None:
    path = default_state_dir() / tc.VERIFICATION_PINS_FILENAME
    text = data if isinstance(data, str) else json.dumps(data)
    path.write_text(text, encoding="utf-8")


async def _must_not_run(cmd, cwd):
    raise AssertionError(f"a command must not run here, got {cmd!r}")


# ---------------------------------------------------------------------------
# run_verification: only the locally pinned command runs
# ---------------------------------------------------------------------------

def test_run_verification_takes_only_the_project_id():
    """The server cannot name a command: the handler has no command parameter."""
    params = list(inspect.signature(tc._handle_run_verification).parameters)
    assert params == ["project_id"]


def test_pins_file_lives_in_the_local_runner_state_dir(local_home):
    assert tc._verification_pins_path() == default_state_dir() / tc.VERIFICATION_PINS_FILENAME


@pytest.mark.asyncio
async def test_run_verification_runs_only_the_pinned_command(local_home, monkeypatch):
    _write_pins({PROJECT: {"cmd": "pixi run test", "cwd": str(local_home)}})
    calls: list[tuple] = []

    async def _fake_run(cmd, cwd):
        calls.append((cmd, cwd))
        return {
            "status": "ok", "exit_code": 0, "passed": 3, "failed": 0,
            "stdout_tail": "3 passed", "stderr_tail": "",
        }

    monkeypatch.setattr(tc, "_handle_run_cmd", _fake_run)
    result = await tc._handle_run_verification(PROJECT)

    assert calls == [("pixi run test", str(local_home))]
    assert result["status"] == "ok"
    assert result["exit_code"] == 0
    assert result["cmd"] == "pixi run test"


@pytest.mark.asyncio
async def test_run_verification_without_a_pin_reports_not_configured(local_home, monkeypatch):
    monkeypatch.setattr(tc, "_handle_run_cmd", _must_not_run)
    result = await tc._handle_run_verification(PROJECT)
    assert result["status"] == "not_configured"
    assert result["exit_code"] is None


@pytest.mark.asyncio
async def test_run_verification_ignores_a_pin_for_another_project(local_home, monkeypatch):
    _write_pins({"00000000-0000-0000-0000-000000000000": {"cmd": "pytest"}})
    monkeypatch.setattr(tc, "_handle_run_cmd", _must_not_run)
    result = await tc._handle_run_verification(PROJECT)
    assert result["status"] == "not_configured"


@pytest.mark.asyncio
async def test_run_verification_with_empty_project_id_reports_not_configured(local_home, monkeypatch):
    _write_pins({PROJECT: {"cmd": "pytest"}})
    monkeypatch.setattr(tc, "_handle_run_cmd", _must_not_run)
    result = await tc._handle_run_verification("")
    assert result["status"] == "not_configured"


def test_valid_pin_without_cwd_loads_with_no_cwd(local_home):
    _write_pins({PROJECT: {"cmd": "  pytest -q  "}})
    assert tc._load_verification_pin(PROJECT) == {"cmd": "pytest -q", "cwd": None}


@pytest.mark.parametrize("raw", [
    "not json{",
    "[]",
    json.dumps({PROJECT: "pytest"}),
    json.dumps({PROJECT: {"cmd": ""}}),
    json.dumps({PROJECT: {"cmd": 5}}),
    json.dumps({PROJECT: {"cmd": "x" * 2001}}),
    json.dumps({PROJECT: {"cmd": "pytest", "cwd": 7}}),
])
def test_malformed_pins_file_pins_nothing(local_home, raw):
    _write_pins(raw)
    assert tc._load_verification_pin(PROJECT) is None


def test_missing_pins_file_pins_nothing(local_home):
    assert tc._load_verification_pin(PROJECT) is None


# ---------------------------------------------------------------------------
# served-root denylist: credential and token folders are never served
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("rel", [
    ".meridian",
    ".meridian/local_runner",
    ".ssh",
    ".ssh/keys",
    ".gnupg",
    ".aws/credentials",
    ".kube",
])
def test_home_credential_folders_are_denied(local_home, rel):
    assert tc._is_denied_root(str(local_home / rel))


def test_state_dir_is_denied(local_home):
    assert tc._is_denied_root(str(default_state_dir()))


def test_zotero_appdata_folder_is_denied(local_home):
    appdata = Path(os.environ["APPDATA"])
    assert tc._is_denied_root(str(appdata / "Zotero"))
    assert tc._is_denied_root(str(appdata / "Zotero" / "storage"))


@pytest.mark.parametrize("rel", ["", ".."])
def test_ancestor_of_a_credential_folder_is_denied(local_home, rel):
    """A root that CONTAINS a token folder would serve it just as a root inside it does."""
    root = (local_home / rel).resolve()
    assert tc._is_denied_root(str(root))


@pytest.mark.parametrize("rel", [".meridian-notes", ".ssh-backup", "projects/repo"])
def test_sibling_with_a_matching_prefix_is_allowed(local_home, rel):
    """Component-wise containment: '.meridian-notes' is not inside '.meridian'."""
    assert not tc._is_denied_root(str(local_home / rel))


def test_denied_root_check_fails_closed(local_home, monkeypatch):
    def _broken():
        raise OSError("cannot resolve credential folders")

    monkeypatch.setattr(tc, "_denied_root_folders", _broken)
    assert tc._is_denied_root(str(local_home / "projects" / "repo")) is True


def test_allowed_served_roots_drops_denied_and_keeps_allowed(local_home):
    allowed = str(local_home / "projects" / "repo")
    denied = str(local_home / ".ssh")
    assert tc._allowed_served_roots([denied, allowed]) == [allowed]


# ---------------------------------------------------------------------------
# every path that serves a root applies the denylist
# ---------------------------------------------------------------------------

def test_build_proxy_command_serves_no_denied_root(local_home):
    allowed = local_home / "projects" / "repo"
    allowed.mkdir(parents=True)
    denied = local_home / ".ssh"
    denied.mkdir()
    cmd = tc._build_proxy_command(
        "npx", str(local_home / "fallback"), roots=[str(allowed), str(denied)],
    )
    assert cmd[cmd.index(PROXY_TOKEN) + 1:] == [str(allowed)]


def test_build_proxy_command_refuses_a_denied_repo_path_fallback(local_home):
    cmd = tc._build_proxy_command("npx", str(local_home / ".meridian"))
    assert cmd[cmd.index(PROXY_TOKEN) + 1:] == []


def test_build_proxy_command_serves_an_allowed_repo_path_fallback(local_home):
    repo = local_home / "repo"
    repo.mkdir()
    cmd = tc._build_proxy_command("npx", str(repo))
    assert cmd[cmd.index(PROXY_TOKEN) + 1:] == [str(repo)]


def test_add_fs_roots_skips_denied_roots(local_home):
    allowed = local_home / "projects"
    allowed.mkdir()
    base = ["npx", PROXY_TOKEN, str(allowed)]
    new_cmd, changed = tc._add_fs_roots_to_cmd(
        base, [str(local_home / ".ssh"), str(local_home / ".meridian")],
    )
    assert changed is False
    assert new_cmd == base


def test_add_fs_roots_appends_only_the_allowed_new_root(local_home):
    kept = local_home / "projects"
    extra = local_home / "extra"
    kept.mkdir()
    extra.mkdir()
    base = ["npx", PROXY_TOKEN, str(kept)]
    new_cmd, changed = tc._add_fs_roots_to_cmd(
        base, [str(local_home / ".ssh"), str(extra)],
    )
    assert changed is True
    assert new_cmd == ["npx", PROXY_TOKEN, str(kept), str(extra)]


def test_set_fs_roots_replaces_with_allowed_roots_only(local_home):
    old = local_home / "old"
    keep = local_home / "keep"
    old.mkdir()
    keep.mkdir()
    base = ["npx", PROXY_TOKEN, str(old)]
    new_cmd, changed = tc._set_fs_roots_on_cmd(
        base, [str(keep), str(local_home / ".gnupg")],
    )
    assert changed is True
    assert new_cmd == ["npx", PROXY_TOKEN, str(keep)]


def test_set_fs_roots_with_only_denied_roots_leaves_the_command_unchanged(local_home):
    old = local_home / "old"
    old.mkdir()
    base = ["npx", PROXY_TOKEN, str(old)]
    new_cmd, changed = tc._set_fs_roots_on_cmd(base, [str(local_home / ".ssh")])
    assert changed is False
    assert new_cmd == base


# ---------------------------------------------------------------------------
# send_verification_control: the wire message carries the project id only
# ---------------------------------------------------------------------------

class _RespondingWS:
    """Records each sent payload and answers it the way the local client would."""

    def __init__(self, reply: dict):
        self.sent: list[dict] = []
        self._reply = reply

    async def send_json(self, payload):
        self.sent.append(payload)
        fut = tun._pending_run_cmd_reqs[payload["id"]]
        fut.set_result({**self._reply, "id": payload["id"]})


@pytest.mark.asyncio
async def test_send_verification_control_sends_only_the_project_id(monkeypatch):
    ws = _RespondingWS({
        "status": "ok", "exit_code": 0, "passed": None, "failed": None,
        "stdout_tail": "", "stderr_tail": "", "cmd": "pixi run test",
    })
    monkeypatch.setitem(tun._tunnel_sockets, "tenant-msg", ws)

    result = await tun.send_verification_control("tenant-msg", PROJECT)

    assert len(ws.sent) == 1
    message = ws.sent[0]
    assert message["type"] == "run_verification"
    assert message["project_id"] == PROJECT
    assert set(message) == {"type", "id", "project_id"}, "no command or cwd may be sent"
    assert result["status"] == "ok"
    assert result["cmd"] == "pixi run test"
    assert message["id"] not in tun._pending_run_cmd_reqs
