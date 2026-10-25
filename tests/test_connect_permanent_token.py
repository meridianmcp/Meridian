"""The hosted installer fails closed when it cannot mint the permanent token.

scripts/meridian_connect.py exchanges the token it was given for a permanent
``hooks-installer`` token via POST /auth/tokens. Before this guard, a failed
POST fell through and the 10-minute browser install token was written to
``~/.meridian/hook_auth.conf`` and registered with Claude Code. Hooks and MCP
then started returning 401 once it expired, with nothing to explain why.

Every value here is a fixture. Nothing reads the real home directory, runs a
real installer step, or contacts a server.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
import types
from pathlib import Path

import pytest

_CONNECT_PY = Path(__file__).resolve().parent.parent / "scripts" / "meridian_connect.py"
_URL = "https://meridian.example.test"
_INSTALL = "sk_meridian_install_fixture"  # noqa: S105 -- fixture value
_PERMANENT = "sk_meridian_permanent_fixture"  # noqa: S105 -- fixture value
_ENV_NAMES = ("MERIDIAN_TOKEN", "MERIDIAN_API_KEY", "BEARER_TOKEN", "CLAUDE_CONFIG_DIR")


def _load():
    spec = importlib.util.spec_from_file_location("meridian_connect_permanent_token", _CONNECT_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeNet:
    """Stands in for ``_http``. ``mint`` is what POST /auth/tokens returns."""

    def __init__(self, mint):
        self.mint = mint
        self.calls: list[tuple[str, str]] = []

    def __call__(self, method, url, *, token="", body=None, timeout=10):
        self.calls.append((method, url))
        if url.endswith("/auth/me"):
            return {"email": "tester@example.test"}
        if url.endswith("/auth/tokens"):
            return self.mint
        return {}


def _prepare(tmp_path, monkeypatch, mint, *, claude=False):
    home = tmp_path / "home"
    (home / "AppData" / "Roaming").mkdir(parents=True)
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))
    monkeypatch.chdir(tmp_path)

    mod = _load()
    net = _FakeNet(mint)
    real_which = shutil.which

    def fake_which(cmd, *a, **k):
        if cmd == "claude" and claude:
            return "C:/fake/bin/claude"
        if cmd in ("claude", "codex", "cursor"):
            return None
        return real_which(cmd, *a, **k)

    monkeypatch.setattr(mod, "_http", net)
    monkeypatch.setattr(mod.shutil, "which", fake_which)
    monkeypatch.setattr(mod.webbrowser, "open", lambda *_a, **_k: True)
    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(isatty=lambda: False))
    monkeypatch.setenv("MERIDIAN_TOKEN", _INSTALL)
    monkeypatch.setattr(sys, "argv", ["meridian_connect.py", "--url", _URL])
    return home, mod


def _written_files(home: Path) -> list[Path]:
    return [p for p in home.rglob("*") if p.is_file()]


@pytest.mark.parametrize(
    "mint",
    [None, {}, {"id": "row-1", "label": "hooks-installer"}],
    ids=["request-failed", "empty-body", "no-token-field"],
)
def test_failed_permanent_token_mint_exits_before_writing_anything(tmp_path, monkeypatch, capsys, mint):
    home, mod = _prepare(tmp_path, monkeypatch, mint, claude=True)
    registered: list[str] = []
    monkeypatch.setattr(
        mod, "_register_claude_mcp", lambda url, tok, cli: registered.append(tok) or (True, []),
    )

    rc = mod.main()

    captured = capsys.readouterr()
    assert rc == 1
    assert _written_files(home) == [], "no hook, MCP or auth config may be written with the install token"
    assert not (home / ".meridian" / "hook_auth.conf").exists()
    assert registered == [], "the MCP registration must not receive the install token"
    assert _INSTALL not in captured.out + captured.err, "the token must never be echoed"
    assert "could not create a permanent Meridian token" in captured.err


def test_successful_mint_persists_and_registers_the_permanent_token(tmp_path, monkeypatch):
    home, mod = _prepare(tmp_path, monkeypatch, {"token": _PERMANENT}, claude=True)
    registered: list[str] = []
    monkeypatch.setattr(
        mod, "_register_claude_mcp", lambda url, tok, cli: registered.append(tok) or (True, []),
    )

    assert mod.main() == 0

    cfg = (home / ".meridian" / "hook_auth.conf").read_text(encoding="utf-8")
    assert _PERMANENT in cfg
    assert _INSTALL not in cfg
    assert registered == [_PERMANENT]
