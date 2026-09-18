"""4e4c3817 -- focused tests for meridian/tray_main.py.

Covers the pure-logic surface that doesn't require an actual Windows GUI
session: server-command resolution (frozen vs. unfrozen self-relaunch),
environment merging (the real subprocess.Popen env=-replaces-not-merges
hazard this module explicitly guards against), the health probe, and the
--run-server flag dispatch. Does NOT attempt to drive a real pystray tray
icon or tkinter window -- those need an actual display/Windows session, not
a CI test runner; the dialog functions are exercised only for their
LocalRunner-facing logic (via monkeypatched tkinter), never a real GUI loop.
"""
from __future__ import annotations

import io
import os
import sys
from unittest import mock

import pytest

import meridian
from meridian import tray_main


# ---------------------------------------------------------------------------
# _server_command -- frozen self-relaunch vs. unfrozen direct entry point
# ---------------------------------------------------------------------------


def test_server_command_unfrozen_uses_python_dash_m_meridian(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert tray_main._server_command() == [sys.executable, "-m", "meridian"]


def test_server_command_frozen_self_relaunches_with_flag(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\fake\meridian-tray.exe", raising=False)
    assert tray_main._server_command() == [r"C:\fake\meridian-tray.exe", "--run-server"]


# ---------------------------------------------------------------------------
# _server_env -- must be the FULL environment plus one addition, never a
# bare replacement (subprocess.Popen(env=...) replaces, doesn't merge).
# ---------------------------------------------------------------------------


def test_server_env_preserves_existing_vars_and_adds_frozen_mode(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("SOME_OTHER_VAR", "keep-me")
    env = tray_main._server_env()
    assert env["PATH"] == "/usr/bin:/bin"
    assert env["SOME_OTHER_VAR"] == "keep-me"
    assert env["MERIDIAN_FROZEN_MODE"] == "server"


def test_server_env_returns_a_copy_not_the_real_os_environ(monkeypatch):
    monkeypatch.setenv("SHOULD_NOT_LEAK", "1")
    env = tray_main._server_env()
    env["SHOULD_NOT_LEAK"] = "mutated"
    assert os.environ["SHOULD_NOT_LEAK"] == "1"


# ---------------------------------------------------------------------------
# _default_port / _dashboard_url
# ---------------------------------------------------------------------------


def test_default_port_falls_back_to_7878(monkeypatch):
    monkeypatch.delenv("MERIDIAN_PORT", raising=False)
    assert tray_main._default_port() == 7878


def test_default_port_respects_env_override(monkeypatch):
    monkeypatch.setenv("MERIDIAN_PORT", "9999")
    assert tray_main._default_port() == 9999


def test_dashboard_url_uses_the_resolved_port(monkeypatch):
    monkeypatch.setenv("MERIDIAN_PORT", "8123")
    assert tray_main._dashboard_url() == "http://127.0.0.1:8123/"


# ---------------------------------------------------------------------------
# _health_probe -- a real HTTP GET against /health, never a bare port check.
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def test_health_probe_true_on_2xx(monkeypatch):
    monkeypatch.setattr(
        tray_main.urllib.request, "urlopen", lambda url, timeout: _FakeResponse(200)
    )
    assert tray_main._health_probe() is True


def test_health_probe_false_on_non_2xx(monkeypatch):
    monkeypatch.setattr(
        tray_main.urllib.request, "urlopen", lambda url, timeout: _FakeResponse(503)
    )
    assert tray_main._health_probe() is False


def test_health_probe_false_on_connection_error(monkeypatch):
    def _raise(url, timeout):
        raise tray_main.urllib.error.URLError("connection refused")

    monkeypatch.setattr(tray_main.urllib.request, "urlopen", _raise)
    assert tray_main._health_probe() is False


def test_health_probe_false_on_os_error_never_raises(monkeypatch):
    def _raise(url, timeout):
        raise OSError("network unreachable")

    monkeypatch.setattr(tray_main.urllib.request, "urlopen", _raise)
    assert tray_main._health_probe() is False


# ---------------------------------------------------------------------------
# _icon_image_path -- frozen (bundled under _MEIPASS) vs. source (static/)
# ---------------------------------------------------------------------------


def test_icon_image_path_unfrozen_resolves_under_static(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    path = tray_main._icon_image_path()
    assert path.name == "meridian-tray.ico"
    assert path.parent.name == "static"


def test_icon_image_path_frozen_resolves_under_meipass(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    path = tray_main._icon_image_path()
    assert path == tmp_path / "meridian-tray.ico"


# ---------------------------------------------------------------------------
# main() -- --run-server dispatch vs. normal tray launch
# ---------------------------------------------------------------------------


def test_main_run_server_flag_dispatches_to_meridian_entry_and_sets_frozen_mode(monkeypatch):
    monkeypatch.delenv("MERIDIAN_FROZEN_MODE", raising=False)
    called_with = {}

    fake_entry = mock.MagicMock()

    def _fake_main(argv):
        called_with["argv"] = argv
        called_with["frozen_mode"] = os.environ.get("MERIDIAN_FROZEN_MODE")
        return 0

    fake_entry.main = _fake_main
    # Patch BOTH sys.modules and the real `meridian` package's own
    # `__main__` attribute. `from . import __main__` (tray_main.main's
    # dispatch) resolves via attribute lookup on the already-imported
    # `meridian` package object FIRST (CPython's `_handle_fromlist`) and
    # only falls back to sys.modules if that attribute is not yet set --
    # so if any earlier-running test in this process (or this xdist worker)
    # already did a real `import meridian.__main__`, patching sys.modules
    # alone silently does nothing and this test's dispatch call falls
    # through to the REAL entry point, which starts a real, indefinitely
    # -running Uvicorn server inside the test process (confirmed live: this
    # is exactly what caused CI's tray-installer test runs to hang at ~99%
    # instead of finishing -- reproduced locally by importing
    # meridian.__main__ for real before running this test with only the
    # sys.modules patch). Same class of hazard the tkinter dialog tests
    # below already guard against via `fake_tkinter.messagebox = ...`.
    monkeypatch.setitem(sys.modules, "meridian.__main__", fake_entry)
    monkeypatch.setattr(meridian, "__main__", fake_entry, raising=False)

    rc = tray_main.main(["--run-server"])
    assert rc == 0
    assert called_with["argv"] == []
    assert called_with["frozen_mode"] == "server"


def test_main_without_flag_runs_the_tray(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(tray_main, "_run_tray", lambda: sentinel)
    assert tray_main.main([]) is sentinel


def test_run_server_flag_is_hidden_from_help(capsys):
    with pytest.raises(SystemExit):
        tray_main.main(["--help"])
    out = capsys.readouterr().out
    assert "--run-server" not in out


# ---------------------------------------------------------------------------
# Dialog helpers -- exercised against a real (offscreen) LocalRunner status,
# with tkinter itself monkeypatched out so this never needs a real display.
# ---------------------------------------------------------------------------


def test_show_status_dialog_reads_runner_status_without_raising(monkeypatch):
    fake_tkinter = mock.MagicMock()
    fake_messagebox = mock.MagicMock()
    fake_tkinter.Tk.return_value = mock.MagicMock()
    # `from tkinter import messagebox` resolves via attribute access on the
    # already-imported `tkinter` module object -- must be wired explicitly,
    # or Mock auto-attribute creation silently hands back a DIFFERENT mock
    # than the one this test asserts against.
    fake_tkinter.messagebox = fake_messagebox
    monkeypatch.setitem(sys.modules, "tkinter", fake_tkinter)
    monkeypatch.setitem(sys.modules, "tkinter.messagebox", fake_messagebox)

    runner = mock.MagicMock()
    status = mock.MagicMock()
    status.child.state.value = "running"
    status.child.pid = 1234
    status.child.uptime_seconds = 42.0
    status.local_mcp.state.value = "ready"
    status.local_mcp.detail = "health probe reported ready"
    status.warnings = ()
    runner.status.return_value = status

    tray_main._show_status_dialog(runner)
    assert fake_messagebox.showinfo.called
    title, message = fake_messagebox.showinfo.call_args[0][:2]
    assert "running" in message
    assert "1234" in message
