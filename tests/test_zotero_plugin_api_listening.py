"""The install reports whether the local write API is listening, and says when Zotero must restart.

The API starts when Zotero starts, from the profile's prefs. A repair made while Zotero is running does not
start it, so the install must say so rather than report success silently. The probe uses loopback sockets only.
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from meridian import zotero_bridge_api as bridge
from meridian import zotero_plugin_install as zpi


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_api_listening_is_false_when_nothing_is_on_the_port(monkeypatch):
    monkeypatch.setattr(bridge, "API_PORT", _free_port())
    assert zpi._api_listening() is False


def test_api_listening_is_true_when_something_accepts_on_the_port(monkeypatch):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        monkeypatch.setattr(bridge, "API_PORT", server.getsockname()[1])
        assert zpi._api_listening() is True


def test_print_api_state_asks_for_a_restart_when_the_api_is_not_listening(capsys):
    zpi._print_api_state({"api_listening": False})
    out = capsys.readouterr().out
    assert "not running yet" in out
    assert "restart it" in out


@pytest.mark.parametrize("result", [{"api_listening": True}, {}])
def test_print_api_state_is_silent_when_listening_or_unknown(capsys, result):
    zpi._print_api_state(result)
    assert capsys.readouterr().out == ""


def test_an_unchanged_install_prints_the_restart_hint_when_the_api_is_off(capsys):
    zpi._print_install_status(
        {"status": "unchanged", "path": "C:/x.xpi", "repaired": ["api_prefs"], "api_listening": False},
        Path("C:/x.xpi"),
        Path("C:/profile"),
    )
    assert "restart it" in capsys.readouterr().out


def test_an_installed_plugin_prints_the_restart_hint_when_the_api_is_off(capsys):
    zpi._print_install_status(
        {"status": "installed", "path": "C:/x.xpi", "api_listening": False},
        Path("C:/x.xpi"),
        Path("C:/profile"),
    )
    assert "restart it" in capsys.readouterr().out
