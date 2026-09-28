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
    monkeypatch.setitem(sys.modules, "meridian.__main__", fake_entry)

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


# ---------------------------------------------------------------------------
# _run_tray -- auto-open the dashboard on launch (owner feedback 2026-09-27:
# a bare tray icon gave zero visible feedback on launch). pystray/PIL are
# faked via sys.modules, same technique the tkinter dialog tests above use --
# never a real GUI loop; icon.run() is a mock, not an actual blocking call.
# ---------------------------------------------------------------------------


def _fake_pystray_and_pil(monkeypatch):
    """Install fake pystray/PIL modules; return (fake_pystray, fake_icon)
    so a test can assert icon.run() was reached (proving _run_tray got all
    the way through, not just bailed early)."""
    fake_icon_instance = mock.MagicMock()
    fake_pystray = mock.MagicMock()
    fake_pystray.Icon.return_value = fake_icon_instance
    monkeypatch.setitem(sys.modules, "pystray", fake_pystray)

    fake_image_module = mock.MagicMock()
    fake_image_module.open.return_value = mock.MagicMock()
    fake_PIL = mock.MagicMock()
    fake_PIL.Image = fake_image_module
    monkeypatch.setitem(sys.modules, "PIL", fake_PIL)
    monkeypatch.setitem(sys.modules, "PIL.Image", fake_image_module)
    return fake_pystray, fake_icon_instance


def _fake_status(state):
    """Build a MagicMock shaped enough like RunnerStatus for _run_tray's
    ``status.local_mcp.state`` / ``.detail`` reads."""
    status = mock.MagicMock()
    status.local_mcp.state = state
    status.local_mcp.detail = f"local mcp state: {state}"
    return status


def test_run_tray_opens_dashboard_after_fresh_start(monkeypatch):
    _fake_pystray_and_pil(monkeypatch)
    fake_runner = mock.MagicMock()
    fake_runner.start.return_value = _fake_status(tray_main.LocalMcpState.READY)
    monkeypatch.setattr(tray_main, "_build_runner", lambda: fake_runner)
    opened = []
    monkeypatch.setattr(tray_main.webbrowser, "open", opened.append)

    rc = tray_main._run_tray()

    assert rc == 0
    assert opened == [tray_main._dashboard_url()]
    fake_runner.start.assert_called_once()


def test_run_tray_opens_dashboard_when_attaching_to_already_running(monkeypatch):
    _fake_pystray_and_pil(monkeypatch)
    fake_runner = mock.MagicMock()
    # RunnerAlreadyRunningError(scope, record) -- record only needs .pid/.run_id
    # for the exception's own message formatting, so a MagicMock is enough.
    fake_runner.start.side_effect = tray_main.RunnerAlreadyRunningError(
        "meridian-tray", mock.MagicMock()
    )
    fake_runner.status.return_value = _fake_status(tray_main.LocalMcpState.READY)
    monkeypatch.setattr(tray_main, "_build_runner", lambda: fake_runner)
    opened = []
    monkeypatch.setattr(tray_main.webbrowser, "open", opened.append)

    rc = tray_main._run_tray()

    assert rc == 0
    assert opened == [tray_main._dashboard_url()]
    fake_runner.status.assert_called_once()


def test_run_tray_browser_open_failure_does_not_crash_or_skip_the_icon(monkeypatch):
    _, fake_icon_instance = _fake_pystray_and_pil(monkeypatch)
    fake_runner = mock.MagicMock()
    fake_runner.start.return_value = _fake_status(tray_main.LocalMcpState.READY)
    monkeypatch.setattr(tray_main, "_build_runner", lambda: fake_runner)

    def _raise(url):
        raise OSError("no default browser configured")

    monkeypatch.setattr(tray_main.webbrowser, "open", _raise)

    rc = tray_main._run_tray()

    assert rc == 0
    # A failed browser-open must not prevent the tray icon itself from
    # starting -- icon.run() still has to be reached.
    fake_icon_instance.run.assert_called_once()


def test_run_tray_does_not_auto_open_when_server_fails_to_start(monkeypatch):
    _fake_pystray_and_pil(monkeypatch)
    fake_runner = mock.MagicMock()
    fake_runner.start.side_effect = RuntimeError("boom")
    monkeypatch.setattr(tray_main, "_build_runner", lambda: fake_runner)
    opened = []
    monkeypatch.setattr(tray_main.webbrowser, "open", opened.append)
    dialog_calls = []
    monkeypatch.setattr(
        tray_main, "_show_error_dialog", lambda title, msg: dialog_calls.append((title, msg))
    )

    rc = tray_main._run_tray()

    assert rc == 1
    assert dialog_calls == [("Meridian failed to start", "boom")]
    # A genuine startup failure must NOT auto-open a browser onto a server
    # that isn't actually there.
    assert opened == []


def test_run_tray_does_not_auto_open_on_cold_start_timeout(monkeypatch):
    """Regression test for 2026-09-28 review finding #14/#18: start() can
    report a non-ready state via a NORMAL RETURN (RunnerStatus with
    local_mcp.state == COLD_START_TIMEOUT/FAILED), not just by raising --
    the previous code discarded start()'s return value entirely and always
    opened the browser. This is the case test_run_tray_does_not_auto_open_
    when_server_fails_to_start (a raising start()) does NOT cover."""
    _fake_pystray_and_pil(monkeypatch)
    fake_runner = mock.MagicMock()
    fake_runner.start.return_value = _fake_status(tray_main.LocalMcpState.COLD_START_TIMEOUT)
    monkeypatch.setattr(tray_main, "_build_runner", lambda: fake_runner)
    opened = []
    monkeypatch.setattr(tray_main.webbrowser, "open", opened.append)
    dialog_calls = []
    monkeypatch.setattr(
        tray_main, "_show_error_dialog", lambda title, msg: dialog_calls.append((title, msg))
    )

    rc = tray_main._run_tray()

    assert rc == 0  # the tray icon still starts -- this is a degrade, not a crash
    assert opened == []
    assert dialog_calls  # the human gets told, instead of a silently dead browser tab


def test_run_tray_does_not_auto_open_when_attaching_to_unhealthy_existing_run(monkeypatch):
    """Regression test for finding #15: 'already running' means the prior
    record's pid is alive, not that the server is healthy -- attaching must
    re-check status() before opening a browser."""
    _fake_pystray_and_pil(monkeypatch)
    fake_runner = mock.MagicMock()
    fake_runner.start.side_effect = tray_main.RunnerAlreadyRunningError(
        "meridian-tray", mock.MagicMock()
    )
    fake_runner.status.return_value = _fake_status(tray_main.LocalMcpState.FAILED)
    monkeypatch.setattr(tray_main, "_build_runner", lambda: fake_runner)
    opened = []
    monkeypatch.setattr(tray_main.webbrowser, "open", opened.append)

    rc = tray_main._run_tray()

    assert rc == 0
    assert opened == []


def test_run_tray_opens_dashboard_when_local_mcp_not_configured(monkeypatch):
    """A health_probe-less runner (local_mcp state NOT_CONFIGURED, never
    READY) must still auto-open -- there's nothing to have timed out."""
    _fake_pystray_and_pil(monkeypatch)
    fake_runner = mock.MagicMock()
    fake_runner.start.return_value = _fake_status(tray_main.LocalMcpState.NOT_CONFIGURED)
    monkeypatch.setattr(tray_main, "_build_runner", lambda: fake_runner)
    opened = []
    monkeypatch.setattr(tray_main.webbrowser, "open", opened.append)

    rc = tray_main._run_tray()

    assert rc == 0
    assert opened == [tray_main._dashboard_url()]
