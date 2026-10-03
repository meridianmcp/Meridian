"""4e4c3817 -- focused tests for meridian/tray_main.py.

Covers the pure-logic surface that doesn't require an actual Windows GUI
session: server-command resolution (frozen vs. unfrozen self-relaunch),
environment merging (the real subprocess.Popen env=-replaces-not-merges
hazard this module explicitly guards against), the health probe, and the
--run-server flag dispatch. Does NOT attempt to drive a real pystray tray
icon or tkinter window -- those need an actual display/Windows session, not
a CI test runner; the dialog functions are exercised only for their
LocalRunner-facing logic (via monkeypatched tkinter), never a real GUI loop.

507e55de -- also carries the tray/GUI installer's PRE-SHIP VALIDATION
CHECKLIST (see ``TestTraySpecPreShipConsistency`` below and the standalone
subprocess regression test at the bottom of this file). Those are a
deliberately different kind of test from the unit tests above: instead of
mocking tray_main.py's collaborators, they check the REAL, as-shipped
``meridian-tray.spec`` / ``meridian/static/meridian-tray.ico`` files on disk
for the specific properties a broken pre-ship build has historically failed
on (see the spec file's own comments: a missing ``meridian/static`` datas
entry 500'd the bundled server, a missing ``meridian/templates`` entry
500'd `GET /`). A drift here -- e.g. tray_main.py growing a new third-party
import that the spec's ``hiddenimports`` doesn't know about -- fails a fast
pytest run instead of surfacing as a broken exe after a real PyInstaller
build.
"""
from __future__ import annotations

import ast
import io
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

import meridian
from meridian import tray_main

_REPO_ROOT = Path(__file__).resolve().parent.parent
_TRAY_SPEC_PATH = _REPO_ROOT / "meridian-tray.spec"
_TRAY_MAIN_PATH = _REPO_ROOT / "meridian" / "tray_main.py"
_ICO_PATH = _REPO_ROOT / "meridian" / "static" / "meridian-tray.ico"


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


def test_server_env_unfrozen_never_sets_meipass2(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    env = tray_main._server_env()
    assert "_MEIPASS2" not in env


def test_server_env_frozen_propagates_meipass2(monkeypatch, tmp_path):
    """2026-09-28 review finding #17: the frozen --run-server child reuses
    the TRAY's own already-extracted onefile directory via PyInstaller's own
    _MEIPASS2 mechanism, instead of independently re-extracting itself."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    env = tray_main._server_env()
    assert env["_MEIPASS2"] == str(tmp_path)


def test_server_env_frozen_without_meipass_sets_nothing(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    env = tray_main._server_env()
    assert "_MEIPASS2" not in env


# ---------------------------------------------------------------------------
# _cold_start_timeout -- frozen-aware default + env override
# (2026-09-28 review finding #17)
# ---------------------------------------------------------------------------


def test_cold_start_timeout_unfrozen_default(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.delenv("MERIDIAN_TRAY_COLD_START_TIMEOUT", raising=False)
    assert tray_main._cold_start_timeout() == tray_main._DEFAULT_COLD_START_TIMEOUT_SECONDS


def test_cold_start_timeout_frozen_gets_a_larger_default(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.delenv("MERIDIAN_TRAY_COLD_START_TIMEOUT", raising=False)
    assert tray_main._cold_start_timeout() == tray_main._FROZEN_COLD_START_TIMEOUT_SECONDS
    assert tray_main._FROZEN_COLD_START_TIMEOUT_SECONDS > tray_main._DEFAULT_COLD_START_TIMEOUT_SECONDS


def test_cold_start_timeout_env_override_wins(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("MERIDIAN_TRAY_COLD_START_TIMEOUT", "45")
    assert tray_main._cold_start_timeout() == 45.0


def test_cold_start_timeout_invalid_override_falls_back(monkeypatch, caplog):
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setenv("MERIDIAN_TRAY_COLD_START_TIMEOUT", "not-a-number")
    with caplog.at_level("WARNING"):
        result = tray_main._cold_start_timeout()
    assert result == tray_main._DEFAULT_COLD_START_TIMEOUT_SECONDS
    assert any("invalid" in r.message.lower() for r in caplog.records)


def test_build_runner_passes_cold_start_timeout(monkeypatch):
    monkeypatch.setattr(tray_main, "_cold_start_timeout", lambda: 42.0)
    runner = tray_main._build_runner()
    assert runner.cold_start_timeout == 42.0


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
# _pid_owns_listening_port / _expected_pid / _health_probe(expected_pid=...)
# -- health-probe identity binding (2026-09-28 review finding #13)
# ---------------------------------------------------------------------------


class _FakeConn:
    def __init__(self, status, port):
        self.status = status
        self.laddr = mock.MagicMock(port=port)


class _FakePsutilProcess:
    def __init__(self, pid, conns):
        self.pid = pid
        self._conns = conns

    def net_connections(self, kind="inet"):
        return self._conns


def _install_fake_psutil(monkeypatch, conns_by_pid):
    fake_psutil = mock.MagicMock()
    fake_psutil.CONN_LISTEN = "LISTEN"

    def _process(pid):
        if pid not in conns_by_pid:
            raise LookupError(f"no such pid {pid}")
        return _FakePsutilProcess(pid, conns_by_pid[pid])

    fake_psutil.Process = _process
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)
    return fake_psutil


def test_pid_owns_listening_port_true_when_pid_listens_on_port(monkeypatch):
    fake_psutil = _install_fake_psutil(monkeypatch, {123: [_FakeConn("LISTEN", 7878)]})
    assert tray_main._pid_owns_listening_port(123, 7878) is True


def test_pid_owns_listening_port_false_when_different_pid_holds_it(monkeypatch):
    _install_fake_psutil(monkeypatch, {123: [_FakeConn("LISTEN", 9999)]})
    assert tray_main._pid_owns_listening_port(123, 7878) is False


def test_pid_owns_listening_port_false_when_pid_has_no_listening_conn(monkeypatch):
    _install_fake_psutil(monkeypatch, {123: []})
    assert tray_main._pid_owns_listening_port(123, 7878) is False


def test_pid_owns_listening_port_degrades_true_when_psutil_unavailable(monkeypatch):
    monkeypatch.setitem(sys.modules, "psutil", None)
    # A completely unrelated process on the port would normally make this
    # False, but with no way to verify, this NARROWING check must never
    # invent a failure the HTTP response itself didn't report.
    assert tray_main._pid_owns_listening_port(123, 7878) is True


def test_pid_owns_listening_port_degrades_true_when_pid_already_exited(monkeypatch):
    _install_fake_psutil(monkeypatch, {})  # 123 not in the fake process table at all
    assert tray_main._pid_owns_listening_port(123, 7878) is True


def test_pid_owns_listening_port_falls_back_to_connections_on_older_psutil(monkeypatch):
    """psutil>=5.9 (this repo's own pin) may predate net_connections()
    (added in psutil>=6.0) -- must fall back to the older connections()
    name rather than crashing."""
    fake_psutil = mock.MagicMock()
    fake_psutil.CONN_LISTEN = "LISTEN"

    class _OldStyleProcess:
        net_connections = None  # deliberately absent -- see hasattr check

        def __init__(self, pid):
            self.pid = pid

        def connections(self, kind="inet"):
            return [_FakeConn("LISTEN", 7878)]

    del _OldStyleProcess.net_connections  # simulate a version that never had it at all
    fake_psutil.Process = _OldStyleProcess
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)
    assert tray_main._pid_owns_listening_port(123, 7878) is True


def test_expected_pid_prefers_live_handle():
    runner = mock.MagicMock()
    runner._live_handle = mock.MagicMock(pid=111)
    assert tray_main._expected_pid(runner) == 111
    runner._load_record.assert_not_called()


def test_expected_pid_falls_back_to_persisted_record(monkeypatch):
    runner = mock.MagicMock()
    runner._live_handle = None
    runner._load_record.return_value = mock.MagicMock(pid=222)
    assert tray_main._expected_pid(runner) == 222


def test_expected_pid_none_when_neither_available():
    runner = mock.MagicMock()
    runner._live_handle = None
    runner._load_record.return_value = None
    assert tray_main._expected_pid(runner) is None


def test_health_probe_cross_checks_pid_when_supplied(monkeypatch):
    monkeypatch.setattr(tray_main.urllib.request, "urlopen", lambda url, timeout: _FakeResponse(200))
    _install_fake_psutil(monkeypatch, {123: [_FakeConn("LISTEN", tray_main._default_port())]})
    assert tray_main._health_probe(123) is True


def test_health_probe_fails_when_pid_does_not_own_the_port(monkeypatch):
    """The core fix: a 200 from /health is no longer sufficient on its own
    when a different local process won the race to bind the port first."""
    monkeypatch.setattr(tray_main.urllib.request, "urlopen", lambda url, timeout: _FakeResponse(200))
    _install_fake_psutil(monkeypatch, {123: [_FakeConn("LISTEN", 9999)]})  # wrong port
    assert tray_main._health_probe(123) is False


def test_health_probe_skips_pid_check_when_http_already_failed(monkeypatch):
    """No point cross-checking identity against a server that isn't even
    responding -- and this must not touch psutil at all in that case."""
    monkeypatch.setattr(tray_main.urllib.request, "urlopen", lambda url, timeout: _FakeResponse(503))
    psutil_calls = []
    monkeypatch.setitem(
        sys.modules, "psutil",
        mock.MagicMock(Process=lambda pid: psutil_calls.append(pid) or mock.MagicMock()),
    )
    assert tray_main._health_probe(123) is False
    assert psutil_calls == []


def test_build_runner_wires_a_pid_cross_checking_health_probe(monkeypatch):
    """_build_runner's health_probe must be a closure bound to the SAME
    runner it returns, not the bare module-level _health_probe -- otherwise
    there is nothing for _expected_pid to read the live handle from."""
    monkeypatch.setattr(tray_main.urllib.request, "urlopen", lambda url, timeout: _FakeResponse(200))
    runner = tray_main._build_runner()
    assert runner.health_probe is not tray_main._health_probe
    _install_fake_psutil(monkeypatch, {})  # no live handle yet -- degrades to HTTP-only via None pid
    assert runner.health_probe() is True


# ---------------------------------------------------------------------------
# _icon_image_path -- frozen (bundled under _MEIPASS) vs. source (static/)
# ---------------------------------------------------------------------------


def test_icon_image_path_unfrozen_resolves_under_static(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    path = tray_main._icon_image_path()
    assert path.name == "meridian-tray.ico"
    assert path.parent.name == "static"


def test_icon_image_path_frozen_resolves_under_meipass(monkeypatch, tmp_path):
    """2026-09-28 review finding #23: resolves under meridian/static/ (the
    SAME sub-path the unfrozen branch uses, and the ONLY place
    meridian-tray.spec's datas= now bundles the icon -- see that file's own
    comment), not the bundle root -- there is no separate root-level copy
    to resolve against any more."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    path = tray_main._icon_image_path()
    assert path == tmp_path / "meridian" / "static" / "meridian-tray.ico"


# ---------------------------------------------------------------------------
# _sweep_stale_runtime_extractions -- orphaned onefile _MEI* dir cleanup
# (2026-09-28 review finding #5/#22, secondary part)
# ---------------------------------------------------------------------------


def test_sweep_stale_extractions_noop_when_not_frozen(monkeypatch, tmp_path):
    monkeypatch.delattr(sys, "frozen", raising=False)
    (tmp_path / "_MEI12345").mkdir()
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "_MEIcurrent"), raising=False)
    tray_main._sweep_stale_runtime_extractions()  # must not raise, must not touch tmp_path
    assert (tmp_path / "_MEI12345").exists()


def test_sweep_stale_extractions_noop_when_no_meipass(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    tray_main._sweep_stale_runtime_extractions()  # must not raise


def test_sweep_stale_extractions_removes_siblings_but_not_current(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    current = tmp_path / "_MEIcurrent"
    stale_a = tmp_path / "_MEIstale1"
    stale_b = tmp_path / "_MEIstale2"
    for d in (current, stale_a, stale_b):
        d.mkdir()
        (d / "marker.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr(sys, "_MEIPASS", str(current), raising=False)

    tray_main._sweep_stale_runtime_extractions()

    assert current.exists() and (current / "marker.txt").exists()
    assert not stale_a.exists()
    assert not stale_b.exists()


def test_sweep_stale_extractions_never_touches_non_mei_entries(monkeypatch, tmp_path):
    """The runtime_tmpdir is Meridian-owned per meridian-tray.spec, but this
    sweep is still deliberately conservative: it only ever deletes entries
    whose name starts with '_MEI' -- never a bare file, never an unrelated
    directory that happens to live alongside the extraction dirs."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    current = tmp_path / "_MEIcurrent"
    current.mkdir()
    monkeypatch.setattr(sys, "_MEIPASS", str(current), raising=False)
    unrelated_dir = tmp_path / "not-a-mei-dir"
    unrelated_dir.mkdir()
    unrelated_file = tmp_path / "_MEIsomething.txt"  # starts with _MEI but is a FILE
    unrelated_file.write_text("x", encoding="utf-8")

    tray_main._sweep_stale_runtime_extractions()

    assert unrelated_dir.exists()
    assert unrelated_file.exists()


def test_sweep_stale_extractions_degrades_on_error(monkeypatch, tmp_path):
    """A sweep failure (e.g. a sibling still locked by a concurrently
    running second tray instance) must never crash tray startup."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    current = tmp_path / "_MEIcurrent"
    current.mkdir()
    stale = tmp_path / "_MEIstale"
    stale.mkdir()
    monkeypatch.setattr(sys, "_MEIPASS", str(current), raising=False)
    monkeypatch.setattr(
        tray_main.shutil, "rmtree",
        lambda path, ignore_errors=False: (_ for _ in ()).throw(OSError("locked")),
    )

    tray_main._sweep_stale_runtime_extractions()  # must not raise


def test_run_tray_calls_sweep_before_building_runner(monkeypatch):
    _fake_pystray_and_pil(monkeypatch)
    fake_runner = mock.MagicMock()
    fake_runner.start.return_value = _fake_status(tray_main.LocalMcpState.NOT_CONFIGURED)
    monkeypatch.setattr(tray_main, "_build_runner", lambda: fake_runner)
    monkeypatch.setattr(tray_main.webbrowser, "open", lambda url: None)
    calls = []
    monkeypatch.setattr(tray_main, "_sweep_stale_runtime_extractions", lambda: calls.append(1))

    tray_main._run_tray()

    assert calls == [1]


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


def test_main_second_windows_launch_opens_dashboard_without_another_tray(monkeypatch):
    monkeypatch.setattr(
        tray_main, "_acquire_windows_tray_lock", lambda: ("already_running", None),
    )
    opened = []
    monkeypatch.setattr(tray_main.webbrowser, "open", opened.append)
    monkeypatch.setattr(
        tray_main, "_run_tray", lambda: pytest.fail("a second icon must not start"),
    )

    assert tray_main.main([]) == 0
    assert opened == [tray_main._dashboard_url()]


def test_windows_tray_instance_lock_releases_its_byte_range(monkeypatch, tmp_path):
    fake_msvcrt = mock.MagicMock()
    fake_msvcrt.LK_NBLCK = 1
    fake_msvcrt.LK_UNLCK = 2
    monkeypatch.setitem(sys.modules, "msvcrt", fake_msvcrt)
    monkeypatch.setattr(tray_main.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    state, handle = tray_main._acquire_windows_tray_lock()

    assert state == "acquired"
    assert handle is not None
    assert (tmp_path / "Meridian" / "tray-instance.lock").read_bytes() == b"\0"
    tray_main._release_windows_tray_lock(handle)
    assert fake_msvcrt.locking.call_args_list[0].args[1:] == (1, 1)
    assert fake_msvcrt.locking.call_args_list[1].args[1:] == (2, 1)
    assert handle.closed


def test_windows_tray_instance_lock_treats_a_busy_lock_as_an_existing_instance(
    monkeypatch, tmp_path,
):
    import errno

    fake_msvcrt = mock.MagicMock()
    fake_msvcrt.LK_NBLCK = 1
    fake_msvcrt.locking.side_effect = OSError(errno.EACCES, "lock is held")
    monkeypatch.setitem(sys.modules, "msvcrt", fake_msvcrt)
    monkeypatch.setattr(tray_main.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    assert tray_main._acquire_windows_tray_lock() == ("already_running", None)


def test_main_reports_single_instance_lock_errors(monkeypatch):
    monkeypatch.setattr(
        tray_main, "_acquire_windows_tray_lock", mock.Mock(side_effect=OSError("lock unavailable")),
    )
    show_error = mock.Mock()
    monkeypatch.setattr(tray_main, "_show_error_dialog", show_error)
    monkeypatch.setattr(
        tray_main, "_run_tray", lambda: pytest.fail("tray cannot start without its lock"),
    )

    assert tray_main.main([]) == 1
    show_error.assert_called_once_with(
        "Meridian tray could not start",
        "Could not establish the single-instance lock: lock unavailable",
    )


def test_run_tray_uses_the_tk_cocoa_main_loop_on_macos(monkeypatch):
    fake_pystray, fake_icon = _fake_pystray_and_pil(monkeypatch)
    fake_runner = mock.MagicMock()
    fake_runner.start.return_value = _fake_status(tray_main.LocalMcpState.READY)
    monkeypatch.setattr(tray_main, "_build_runner", lambda: fake_runner)
    monkeypatch.setattr(tray_main.webbrowser, "open", lambda _url: None)
    monkeypatch.setattr(tray_main.sys, "platform", "darwin")

    fake_root = mock.MagicMock()
    fake_tk = mock.MagicMock()
    fake_tk.Tk.return_value = fake_root
    monkeypatch.setitem(sys.modules, "tkinter", fake_tk)
    appkit = mock.MagicMock()
    cocoa_app = object()
    appkit.NSApplication.sharedApplication.return_value = cocoa_app
    monkeypatch.setitem(sys.modules, "AppKit", appkit)

    assert tray_main._run_tray() == 0

    fake_tk.Tk.assert_called_once_with()
    fake_icon.run_detached.assert_called_once_with()
    fake_icon.run.assert_not_called()
    fake_root.mainloop.assert_called_once_with()
    fake_root.destroy.assert_called_once_with()
    assert fake_pystray.Icon.call_args.kwargs["darwin_nsapplication"] is cocoa_app


def test_run_tray_cleans_up_macos_root_when_startup_is_not_ready(monkeypatch):
    fake_pystray, _ = _fake_pystray_and_pil(monkeypatch)
    fake_runner = mock.MagicMock()
    fake_runner.start.return_value = _fake_status(tray_main.LocalMcpState.COLD_START_TIMEOUT)
    monkeypatch.setattr(tray_main, "_build_runner", lambda: fake_runner)
    monkeypatch.setattr(tray_main.sys, "platform", "darwin")
    monkeypatch.setattr(tray_main, "_show_error_dialog", mock.Mock())

    fake_root = mock.MagicMock()
    fake_tk = mock.MagicMock()
    fake_tk.Tk.return_value = fake_root
    monkeypatch.setitem(sys.modules, "tkinter", fake_tk)
    appkit = mock.MagicMock()
    appkit.NSApplication.sharedApplication.return_value = object()
    monkeypatch.setitem(sys.modules, "AppKit", appkit)

    assert tray_main._run_tray() == 1

    fake_root.destroy.assert_called_once_with()
    fake_pystray.Icon.assert_not_called()


def test_run_tray_reports_startup_exception_and_closes_macos_root(monkeypatch):
    _fake_pystray_and_pil(monkeypatch)
    fake_runner = mock.MagicMock()
    fake_runner.start.side_effect = RuntimeError("startup failed")
    monkeypatch.setattr(tray_main, "_build_runner", lambda: fake_runner)
    monkeypatch.setattr(tray_main.sys, "platform", "darwin")
    show_error = mock.Mock()
    monkeypatch.setattr(tray_main, "_show_error_dialog", show_error)

    fake_root = mock.MagicMock()
    fake_tk = mock.MagicMock()
    fake_tk.Tk.return_value = fake_root
    monkeypatch.setitem(sys.modules, "tkinter", fake_tk)
    appkit = mock.MagicMock()
    appkit.NSApplication.sharedApplication.return_value = object()
    monkeypatch.setitem(sys.modules, "AppKit", appkit)

    assert tray_main._run_tray() == 1

    show_error.assert_called_once_with("Meridian failed to start", "startup failed", parent=fake_root)
    fake_root.destroy.assert_called_once_with()


def test_run_tray_closes_macos_root_when_existing_runner_status_fails(monkeypatch):
    _fake_pystray_and_pil(monkeypatch)
    fake_runner = mock.MagicMock()
    record = mock.MagicMock(pid=123, run_id="existing")
    fake_runner.start.side_effect = tray_main.RunnerAlreadyRunningError("default", record)
    fake_runner.status.return_value = _fake_status(tray_main.LocalMcpState.COLD_START_TIMEOUT)
    monkeypatch.setattr(tray_main, "_build_runner", lambda: fake_runner)
    monkeypatch.setattr(tray_main.sys, "platform", "darwin")

    fake_root = mock.MagicMock()
    fake_tk = mock.MagicMock()
    fake_tk.Tk.return_value = fake_root
    monkeypatch.setitem(sys.modules, "tkinter", fake_tk)
    appkit = mock.MagicMock()
    appkit.NSApplication.sharedApplication.return_value = object()
    monkeypatch.setitem(sys.modules, "AppKit", appkit)

    assert tray_main._run_tray() == 1

    fake_runner.status.assert_called_once_with()
    fake_root.destroy.assert_called_once_with()


def test_run_tray_falls_back_to_a_solid_icon_when_the_asset_is_missing(monkeypatch):
    fake_pystray, fake_icon = _fake_pystray_and_pil(monkeypatch)
    image_module = sys.modules["PIL.Image"]
    image_module.open.side_effect = OSError("missing image")
    fake_runner = mock.MagicMock()
    fake_runner.start.return_value = _fake_status(tray_main.LocalMcpState.READY)
    monkeypatch.setattr(tray_main, "_build_runner", lambda: fake_runner)
    monkeypatch.setattr(tray_main.webbrowser, "open", lambda _url: None)

    assert tray_main._run_tray() == 0

    image_module.new.assert_called_once_with("RGBA", (64, 64), (0, 102, 204, 255))
    fake_pystray.Icon.assert_called_once()
    fake_icon.run.assert_called_once_with()


@pytest.mark.parametrize(
    ("failure", "title"),
    [
        (tray_main.ZoteroSetupError("credential vault unavailable"), "Zotero setup unavailable"),
        (RuntimeError("unexpected setup failure"), "Zotero setup failed"),
    ],
)
def test_run_tray_reports_zotero_setup_errors(monkeypatch, failure, title):
    fake_pystray, _ = _fake_pystray_and_pil(monkeypatch)
    fake_runner = mock.MagicMock()
    fake_runner.start.return_value = _fake_status(tray_main.LocalMcpState.READY)
    monkeypatch.setattr(tray_main, "_build_runner", lambda: fake_runner)
    monkeypatch.setattr(tray_main.webbrowser, "open", lambda _url: None)
    monkeypatch.setattr(tray_main, "run_zotero_setup_dialog", mock.Mock(side_effect=failure))
    show_error = mock.Mock()
    monkeypatch.setattr(tray_main, "_show_error_dialog", show_error)

    class ImmediateThread:
        def __init__(self, *, target, daemon, name=None, args=()):
            self.target = target
            self.name = name
            self.args = args

        def start(self):
            if self.name != "meridian-tunnel-watchdog":
                self.target(*self.args)

    monkeypatch.setattr(tray_main.threading, "Thread", ImmediateThread)
    assert tray_main._run_tray() == 0
    callbacks = {
        call.args[0]: call.args[1]
        for call in fake_pystray.MenuItem.call_args_list
        if isinstance(call.args[0], str)
    }
    callbacks["Zotero connection…"](mock.MagicMock(), None)

    show_error.assert_called_once_with(title, str(failure), parent=None)


@pytest.mark.parametrize("platform", ["win32", "darwin"])
def test_run_tray_menu_actions_use_the_platform_ui_dispatcher(monkeypatch, platform):
    fake_pystray, fake_icon = _fake_pystray_and_pil(monkeypatch)
    fake_runner = mock.MagicMock()
    fake_runner.start.return_value = _fake_status(tray_main.LocalMcpState.READY)
    monkeypatch.setattr(tray_main, "_build_runner", lambda: fake_runner)
    fake_tunnel_runner = mock.MagicMock()
    monkeypatch.setattr(tray_main, "_build_tunnel_runner", lambda: fake_tunnel_runner)
    hosted_status = {
        "state": "disconnected",
        "detail": "No active hosted tunnel socket is reported.",
        "last_error": None,
        "base_url": "https://usemeridian.us",
        "diagnostics_url": "https://usemeridian.us/tunnel/diagnostics/tenant-123",
    }
    monkeypatch.setattr(tray_main, "_hosted_tunnel_status", lambda: hosted_status)
    monkeypatch.setattr(tray_main.webbrowser, "open", mock.Mock())
    monkeypatch.setattr(tray_main.sys, "platform", platform)

    parent = None
    if platform == "darwin":
        parent = mock.MagicMock()
        fake_tk = mock.MagicMock()
        fake_tk.Tk.return_value = parent
        monkeypatch.setitem(sys.modules, "tkinter", fake_tk)
        appkit = mock.MagicMock()
        appkit.NSApplication.sharedApplication.return_value = object()
        monkeypatch.setitem(sys.modules, "AppKit", appkit)

    monkeypatch.setattr(tray_main, "_show_status_dialog", mock.Mock())
    monkeypatch.setattr(tray_main, "_show_logs_window", mock.Mock())
    monkeypatch.setattr(tray_main, "_load_tunnel_watchdog_enabled", lambda: True)
    save_watchdog = mock.Mock()
    monkeypatch.setattr(tray_main, "_save_tunnel_watchdog_enabled", save_watchdog)
    monkeypatch.setattr(tray_main, "run_zotero_setup_dialog", mock.Mock())
    errors = mock.Mock()
    monkeypatch.setattr(tray_main, "_show_error_dialog", errors)
    monkeypatch.setattr(
        tray_main, "_choose_project_root",
        mock.Mock(side_effect=["C:/work/project", None, None]),
    )
    launch = mock.Mock()
    monkeypatch.setattr(tray_main, "_launch_local_cli", launch)
    fake_runner.restart.side_effect = RuntimeError("restart failed")

    class ImmediateThread:
        def __init__(self, *, target, daemon, name=None, args=()):
            self.target = target
            self.name = name
            self.args = args

        def start(self):
            if self.name != "meridian-tunnel-watchdog":
                self.target(*self.args)

    monkeypatch.setattr(tray_main.threading, "Thread", ImmediateThread)

    def click_actions(*_args):
        callbacks = {
            call.args[0]: call.args[1]
            for call in fake_pystray.MenuItem.call_args_list
            if isinstance(call.args[0], str)
        }
        for label in (
            "Open Dashboard", "Status", "View Logs", "Zotero connection…",
            "Set up a local project…", "Check a local project…",
            "Catalog local sessions", "Artifact capture commands", "Restart",
            "Automatic tunnel recovery", "Quit",
        ):
            callbacks[label](fake_icon, None)
        # A second setup-menu invocation covers the user's picker-cancel path.
        callbacks["Set up a local project…"](fake_icon, None)

    if platform == "darwin":
        fake_icon.run_detached.side_effect = click_actions
        parent.mainloop.side_effect = lambda: parent.after.call_args_list[0].args[1]()
    else:
        fake_icon.run.side_effect = click_actions

    assert tray_main._run_tray() == 0

    status_call = tray_main._show_status_dialog.call_args
    assert status_call.args == (fake_runner,)
    assert status_call.kwargs["parent"] is parent
    assert status_call.kwargs["tunnel_runner"] is fake_tunnel_runner
    assert status_call.kwargs["hosted_tunnel_status"] == hosted_status
    expected_watchdog_status = (
        "enabled; waiting for the tunnel to be enabled"
        if platform == "win32" else "disabled by user"
    )
    assert status_call.kwargs["watchdog_status"] == expected_watchdog_status
    tray_main._show_logs_window.assert_called_once_with(fake_runner, parent=parent)
    tray_main.run_zotero_setup_dialog.assert_called_once_with(parent=parent)
    assert tray_main._choose_project_root.call_count == 3
    setup = mock.call("setup", "--repo", "C:/work/project", cwd="C:/work/project")
    recovery = mock.call("recovery", "catalog")
    artifacts = mock.call("artifacts", "--help")
    assert launch.call_args_list == (
        [setup, recovery, artifacts] if platform == "win32" else [recovery, artifacts, setup]
    )
    fake_runner.restart.assert_called_once_with()
    save_watchdog.assert_called_once_with(False)
    errors.assert_called_once_with("Meridian restart failed", "restart failed", parent=parent)
    fake_runner.stop.assert_called_once_with()
    fake_icon.stop.assert_called_once_with()


def test_run_tray_exposes_hosted_tunnel_enable_reconnect_disable_and_diagnostics(monkeypatch):
    fake_pystray, fake_icon = _fake_pystray_and_pil(monkeypatch)
    fake_runner = mock.MagicMock()
    fake_runner.start.return_value = _fake_status(tray_main.LocalMcpState.READY)
    fake_tunnel_runner = mock.MagicMock(command=None)
    fake_tunnel_runner.status.return_value.child.state = mock.sentinel.not_running
    fake_tunnel_runner.restart.side_effect = [ValueError("no prior tunnel command"), None]
    monkeypatch.setattr(tray_main, "_build_runner", lambda: fake_runner)
    monkeypatch.setattr(tray_main, "_build_tunnel_runner", lambda: fake_tunnel_runner)
    monkeypatch.setattr(tray_main, "_hosted_tunnel_status", lambda: {
        "state": "connected",
        "detail": "active",
        "last_error": None,
        "base_url": "https://usemeridian.us",
        "diagnostics_url": "https://usemeridian.us/tunnel/diagnostics/tenant-123",
    })
    monkeypatch.setattr(tray_main, "_choose_project_root", mock.Mock(return_value="C:/work/project"))
    monkeypatch.setattr(tray_main.webbrowser, "open", mock.Mock())
    monkeypatch.setattr(tray_main.sys, "platform", "win32")

    class ImmediateThread:
        def __init__(self, *, target, daemon, name=None, args=()):
            self.target = target
            self.name = name
            self.args = args

        def start(self):
            if self.name != "meridian-tunnel-watchdog":
                self.target(*self.args)

    monkeypatch.setattr(tray_main.threading, "Thread", ImmediateThread)

    def click_tunnel_actions(*_args):
        callbacks = {
            call.args[0]: call.args[1]
            for call in fake_pystray.MenuItem.call_args_list
            if isinstance(call.args[0], str)
        }
        for label in (
            "Enable tunnel…", "Reconnect tunnel", "Disable tunnel",
            "Open tunnel diagnostics", "Quit",
        ):
            callbacks[label](fake_icon, None)

    fake_icon.run.side_effect = click_tunnel_actions
    assert tray_main._run_tray() == 0

    tray_main._choose_project_root.assert_called_once_with(
        "Choose a local project to share through the Meridian tunnel", parent=None,
    )
    assert fake_tunnel_runner.command == [
        sys.executable,
        "-m",
        "meridian.tunnel_main",
        "--repo",
        str(Path("C:/work/project").resolve()),
    ]
    fake_tunnel_runner.start.assert_called_once_with()
    assert fake_tunnel_runner.restart.call_args_list == [
        mock.call(reason="manual tunnel enable"),
        mock.call(reason="manual tunnel reconnect"),
    ]
    fake_tunnel_runner.stop.assert_called_once_with()
    tray_main.webbrowser.open.assert_any_call("https://usemeridian.us/tunnel/diagnostics/tenant-123")


@pytest.mark.parametrize("frozen", [False, True])
def test_tunnel_command_uses_the_supervised_entrypoint(monkeypatch, tmp_path, frozen):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(tray_main.sys, "frozen", frozen, raising=False)
    command = tray_main._tunnel_command(str(project))
    prefix = [sys.executable, "--run-tunnel"] if frozen else [
        sys.executable, "-m", "meridian.tunnel_main",
    ]
    assert command == [*prefix, "--repo", str(project.resolve())]


@pytest.mark.parametrize(
    ("argv", "forwarded"),
    [
        (["--run-tunnel", "--repo", "C:/work/project"], ["--repo", "C:/work/project"]),
        (["--_tunnel-child", "--repo", "C:/work/project"], ["--_tunnel-child", "--repo", "C:/work/project"]),
    ],
)
def test_frozen_tunnel_flags_dispatch_to_tunnel_main(monkeypatch, argv, forwarded):
    from types import SimpleNamespace

    tunnel_main = mock.Mock(return_value=7)
    monkeypatch.setitem(sys.modules, "meridian.tunnel_main", SimpleNamespace(main=tunnel_main))
    assert tray_main.main(argv) == 7
    tunnel_main.assert_called_once_with(forwarded)


def test_tk_ui_dispatcher_keeps_rescheduling_after_callback_error():
    root = mock.MagicMock()
    dispatcher = tray_main._TkUiDispatcher(root)
    failing = mock.Mock(side_effect=RuntimeError("menu action failed"))
    following = mock.Mock()
    dispatcher.submit(failing)
    dispatcher.submit(following)

    first_tick = root.after.call_args.args[1]
    with pytest.raises(RuntimeError, match="menu action failed"):
        first_tick()

    second_tick = root.after.call_args.args[1]
    second_tick()
    failing.assert_called_once()
    following.assert_called_once()


def test_local_cli_command_uses_the_tray_executable_when_frozen(monkeypatch):
    monkeypatch.setattr(tray_main.sys, "executable", "C:/Meridian/meridian-tray.exe")
    monkeypatch.setattr(tray_main.sys, "frozen", True, raising=False)

    assert tray_main._local_cli_command("recovery", "catalog") == [
        "C:/Meridian/meridian-tray.exe", "recovery", "catalog",
    ]


def test_local_cli_command_uses_current_python_in_source_mode(monkeypatch):
    monkeypatch.setattr(tray_main.sys, "executable", "C:/Python/python.exe")
    monkeypatch.setattr(tray_main.sys, "frozen", False, raising=False)

    assert tray_main._local_cli_command("doctor", "--repo", "C:/work/project") == [
        "C:/Python/python.exe", "-m", "meridian", "doctor", "--repo", "C:/work/project",
    ]


def test_launch_local_cli_uses_a_visible_windows_console(monkeypatch):
    popen = mock.Mock()
    monkeypatch.setattr(tray_main.subprocess, "Popen", popen)
    monkeypatch.setattr(tray_main.sys, "platform", "win32")
    monkeypatch.setattr(tray_main.subprocess, "CREATE_NEW_CONSOLE", 0x10, raising=False)
    monkeypatch.setattr(tray_main, "_local_cli_command", lambda *args: ["meridian", *args])

    tray_main._launch_local_cli("recovery", "catalog")

    popen.assert_called_once_with(
        ["meridian", "recovery", "catalog"], cwd=None, creationflags=0x10,
    )


def test_choose_project_root_returns_the_selected_local_path(monkeypatch):
    fake_tkinter = mock.MagicMock()
    fake_filedialog = mock.MagicMock()
    fake_tkinter.Tk.return_value = mock.MagicMock()
    fake_tkinter.filedialog = fake_filedialog
    fake_filedialog.askdirectory.return_value = "C:/work/project"
    monkeypatch.setitem(sys.modules, "tkinter", fake_tkinter)
    monkeypatch.setitem(sys.modules, "tkinter.filedialog", fake_filedialog)

    assert tray_main._choose_project_root("Choose project") == "C:/work/project"
    fake_filedialog.askdirectory.assert_called_once_with(
        parent=fake_tkinter.Tk.return_value, title="Choose project", mustexist=True,
    )
    fake_tkinter.Tk.return_value.destroy.assert_called_once_with()


def test_choose_project_root_returns_none_when_dialog_is_cancelled(monkeypatch):
    fake_tkinter = mock.MagicMock()
    fake_filedialog = mock.MagicMock()
    fake_tkinter.filedialog = fake_filedialog
    fake_filedialog.askdirectory.return_value = ""
    monkeypatch.setitem(sys.modules, "tkinter", fake_tkinter)
    monkeypatch.setitem(sys.modules, "tkinter.filedialog", fake_filedialog)

    assert tray_main._choose_project_root("Choose project") is None
    fake_tkinter.Tk.return_value.destroy.assert_called_once_with()


def test_launch_local_cli_reports_process_start_failure(monkeypatch):
    monkeypatch.setattr(tray_main, "_local_cli_command", lambda *args: ["missing", *args])
    monkeypatch.setattr(tray_main.subprocess, "Popen", mock.Mock(side_effect=OSError("not found")))
    show_error = mock.Mock()
    monkeypatch.setattr(tray_main, "_show_error_dialog", show_error)

    tray_main._launch_local_cli("doctor")

    show_error.assert_called_once_with("Meridian local tool failed to open", "not found")


def test_run_tray_exposes_local_workstation_tools(monkeypatch):
    fake_pystray, fake_icon_instance = _fake_pystray_and_pil(monkeypatch)
    runner = mock.MagicMock()
    runner.start.return_value = _fake_status(tray_main.LocalMcpState.READY)
    monkeypatch.setattr(tray_main, "_build_runner", lambda: runner)
    monkeypatch.setattr(tray_main.webbrowser, "open", lambda _url: True)

    assert tray_main._run_tray() == 0

    labels = [call.args[0] for call in fake_pystray.MenuItem.call_args_list]
    assert "Local workstation tools" in labels
    assert "Set up a local project…" in labels
    assert "Automatic tunnel recovery" in labels
    assert "Check a local project…" in labels
    assert "Catalog local sessions" in labels
    assert "Artifact capture commands" in labels
    fake_icon_instance.run.assert_called_once()


def test_local_project_setup_menu_uses_the_selected_root(monkeypatch):
    fake_pystray, _ = _fake_pystray_and_pil(monkeypatch)
    runner = mock.MagicMock()
    runner.start.return_value = _fake_status(tray_main.LocalMcpState.READY)
    monkeypatch.setattr(tray_main, "_build_runner", lambda: runner)
    monkeypatch.setattr(tray_main.webbrowser, "open", lambda _url: True)
    monkeypatch.setattr(
        tray_main, "_choose_project_root", lambda _title, parent=None: "C:/work/project",
    )
    launch = mock.Mock()
    monkeypatch.setattr(tray_main, "_launch_local_cli", launch)

    class ImmediateThread:
        def __init__(self, *, target, daemon, name=None, args=()):
            self.target = target
            self.name = name
            self.args = args

        def start(self):
            if self.name != "meridian-tunnel-watchdog":
                self.target(*self.args)

    monkeypatch.setattr(tray_main.threading, "Thread", ImmediateThread)
    tray_main._run_tray()
    setup_item = next(
        call for call in fake_pystray.MenuItem.call_args_list
        if call.args[0] == "Set up a local project…"
    )
    setup_item.args[1](None, None)

    launch.assert_called_once_with(
        "setup", "--repo", "C:/work/project", cwd="C:/work/project",
    )


def test_local_tools_launch_check_recovery_and_artifact_commands(monkeypatch):
    fake_pystray, _ = _fake_pystray_and_pil(monkeypatch)
    runner = mock.MagicMock()
    runner.start.return_value = _fake_status(tray_main.LocalMcpState.READY)
    monkeypatch.setattr(tray_main, "_build_runner", lambda: runner)
    monkeypatch.setattr(tray_main.webbrowser, "open", lambda _url: True)
    monkeypatch.setattr(
        tray_main, "_choose_project_root", lambda _title, parent=None: "C:/work/project",
    )
    launch = mock.Mock()
    monkeypatch.setattr(tray_main, "_launch_local_cli", launch)

    class ImmediateThread:
        def __init__(self, *, target, daemon, name=None, args=()):
            self.target = target
            self.name = name
            self.args = args

        def start(self):
            if self.name != "meridian-tunnel-watchdog":
                self.target(*self.args)

    monkeypatch.setattr(tray_main.threading, "Thread", ImmediateThread)
    tray_main._run_tray()
    callbacks = {
        call.args[0]: call.args[1]
        for call in fake_pystray.MenuItem.call_args_list
    }
    callbacks["Check a local project…"](None, None)
    callbacks["Catalog local sessions"](None, None)
    callbacks["Artifact capture commands"](None, None)

    assert launch.call_args_list == [
        mock.call("doctor", "--repo", "C:/work/project", cwd="C:/work/project"),
        mock.call("recovery", "catalog"),
        mock.call("artifacts", "--help"),
    ]


def test_local_project_menu_reports_picker_errors_without_stopping_tray(monkeypatch):
    fake_pystray, _ = _fake_pystray_and_pil(monkeypatch)
    runner = mock.MagicMock()
    runner.start.return_value = _fake_status(tray_main.LocalMcpState.READY)
    monkeypatch.setattr(tray_main, "_build_runner", lambda: runner)
    monkeypatch.setattr(tray_main.webbrowser, "open", lambda _url: True)
    monkeypatch.setattr(
        tray_main, "_choose_project_root", mock.Mock(side_effect=RuntimeError("dialog unavailable")),
    )
    errors = []
    monkeypatch.setattr(
        tray_main, "_show_error_dialog",
        lambda title, message, parent=None: errors.append((title, message)),
    )

    class ImmediateThread:
        def __init__(self, *, target, daemon, name=None, args=()):
            self.target = target
            self.name = name
            self.args = args

        def start(self):
            if self.name != "meridian-tunnel-watchdog":
                self.target(*self.args)

    monkeypatch.setattr(tray_main.threading, "Thread", ImmediateThread)
    assert tray_main._run_tray() == 0
    callbacks = {
        call.args[0]: call.args[1]
        for call in fake_pystray.MenuItem.call_args_list
    }
    callbacks["Set up a local project…"](None, None)
    callbacks["Check a local project…"](None, None)

    assert errors == [
        ("Meridian project setup failed", "dialog unavailable"),
        ("Meridian project check failed", "dialog unavailable"),
    ]


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

    tunnel_runner = mock.MagicMock()
    tunnel_status = mock.MagicMock()
    tunnel_status.child.state.value = "running"
    tunnel_status.child.pid = 5678
    tunnel_runner.status.return_value = tunnel_status
    hosted_status = {
        "state": "connected",
        "detail": "Hosted diagnostics report an active tunnel.",
        "last_error": "filesystem: previous proxy error",
    }

    tray_main._show_status_dialog(
        runner,
        tunnel_runner=tunnel_runner,
        hosted_tunnel_status=hosted_status,
    )
    assert fake_messagebox.showinfo.called
    title, message = fake_messagebox.showinfo.call_args[0][:2]
    assert "running" in message
    assert "1234" in message
    assert "Hosted tunnel: connected" in message
    assert "Tunnel supervisor: running" in message
    assert "Last error: filesystem: previous proxy error" in message


def test_hosted_tunnel_status_uses_the_authenticated_hosted_diagnostics(monkeypatch):
    import json

    from meridian import tunnel_client

    monkeypatch.setattr(tunnel_client, "_resolve_base_url", lambda: "https://example.test")
    monkeypatch.setattr(tunnel_client, "_resolve_token", lambda: "test-token")
    monkeypatch.setattr(tunnel_client, "_read_cached_token", lambda _base_url: None)
    payloads = iter([
        {"tenant_id": "tenant-123"},
        {
            "tunnel_process": {"any_active": True},
            "slots": {"filesystem": {"last_error": "proxy start failed"}},
        },
    ])
    requests = []

    def fake_urlopen(request, *, timeout):
        requests.append(request)
        response = mock.MagicMock()
        response.read.return_value = json.dumps(next(payloads)).encode("utf-8")
        response.__enter__.return_value = response
        return response

    monkeypatch.setattr(tray_main.urllib.request, "urlopen", fake_urlopen)
    result = tray_main._hosted_tunnel_status()

    assert result["state"] == "connected"
    assert result["last_error"] == "filesystem: proxy start failed"
    assert result["diagnostics_url"] == "https://example.test/tunnel/diagnostics/tenant-123"
    assert [request.full_url for request in requests] == [
        "https://example.test/me",
        "https://example.test/tunnel/diagnostics/tenant-123",
    ]
    assert all(request.get_header("Authorization") == "Bearer test-token" for request in requests)


def test_hosted_tunnel_status_reports_missing_auth_without_network_calls(monkeypatch):
    from meridian import tunnel_client

    monkeypatch.setattr(tunnel_client, "_resolve_base_url", lambda: "https://example.test")
    monkeypatch.setattr(tunnel_client, "_resolve_token", lambda: "")
    monkeypatch.setattr(tunnel_client, "_read_cached_token", lambda _base_url: None)
    network = mock.Mock(side_effect=AssertionError("must not contact hosted service without a token"))
    monkeypatch.setattr(tray_main.urllib.request, "urlopen", network)

    result = tray_main._hosted_tunnel_status()

    assert result["state"] == "not signed in"
    assert result["base_url"] == "https://example.test"
    network.assert_not_called()


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
        tray_main, "_show_error_dialog",
        lambda title, msg, parent=None: dialog_calls.append((title, msg))
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
        tray_main, "_show_error_dialog",
        lambda title, msg, parent=None: dialog_calls.append((title, msg))
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


def test_configure_zotero_flag_opens_setup_without_starting_tray(monkeypatch):
    calls = []
    monkeypatch.setattr(tray_main, "run_zotero_setup_dialog", lambda: calls.append("setup"))
    monkeypatch.setattr(tray_main, "_run_tray", lambda: pytest.fail("tray should not start"))

    assert tray_main.main(["--configure-zotero"]) == 0
    assert calls == ["setup"]


# ---------------------------------------------------------------------------
# Pre-ship validation checklist (507e55de) -- real-artifact consistency
# checks between meridian-tray.spec, meridian/static/meridian-tray.ico, and
# tray_main.py itself. These read the ACTUAL files on disk (never mocks),
# so they catch the class of bug a unit test mocking LocalRunner/pystray/PIL
# cannot: a PyInstaller build succeeding but shipping a broken exe because
# the spec drifted from what the module actually needs at runtime.
# ---------------------------------------------------------------------------


def _spec_call_kwargs(spec_path: Path, call_name: str, occurrence: int = 1) -> dict:
    """Statically extract literal keyword arguments from a named call (e.g.
    ``Analysis(...)`` or ``EXE(...)``) inside a PyInstaller .spec file.

    .spec files are real Python but reference names (``Analysis``, ``EXE``,
    ``PYZ``, and the pipeline variables they're chained through) that only
    exist inside PyInstaller's own exec environment -- they cannot be safely
    ``exec``'d here just to validate their shape. Parsing the AST and pulling
    only the literal (list/str/bool/None) keyword values out of the call we
    care about validates the spec's real, checked-in content without needing
    PyInstaller itself (or a full build) to do it.

    73257801 -- the spec now has TWO ``EXE(...)`` calls (macOS's onedir
    build inside ``if _IS_MACOS:``, Windows' onefile build inside the
    ``else:``). ``occurrence`` (1-based) picks which match to return when a
    call name appears more than once -- default 1 keeps every pre-existing,
    unambiguous call site (``Analysis``/``PYZ``/``COLLECT``/``BUNDLE`` each
    appear exactly once) working unchanged; the macOS branch is textually
    first, so the Windows-only ``EXE(...)`` call is occurrence=2.
    """
    tree = ast.parse(spec_path.read_text(encoding="utf-8"))
    seen = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == call_name:
            seen += 1
            if seen != occurrence:
                continue
            kwargs: dict = {}
            for kw in node.keywords:
                if kw.arg is None:
                    continue
                try:
                    kwargs[kw.arg] = ast.literal_eval(kw.value)
                except (ValueError, TypeError):
                    # 73257801 -- a list kwarg may contain a platform-
                    # conditional ternary element (e.g.
                    # `'pystray._darwin' if _IS_MACOS else 'pystray._win32'`),
                    # which isn't a literal, so a whole-list literal_eval
                    # fails even though every OTHER element is a plain
                    # literal. Fall back to evaluating element-by-element,
                    # resolving such a ternary to BOTH of its literal
                    # branches -- a static pre-ship check just needs to know
                    # a given literal is reachable somewhere in the list, not
                    # which platform's branch actually picks it at build
                    # time. A genuinely non-literal, non-list value (e.g.
                    # `cipher=block_cipher`, a bare Name reference) is still
                    # skipped entirely, unchanged from before.
                    if isinstance(kw.value, ast.List):
                        elts: list = []
                        ok = True
                        for elt in kw.value.elts:
                            try:
                                if isinstance(elt, ast.IfExp):
                                    elts.append(ast.literal_eval(elt.body))
                                    elts.append(ast.literal_eval(elt.orelse))
                                else:
                                    elts.append(ast.literal_eval(elt))
                            except (ValueError, TypeError):
                                ok = False
                                break
                        if ok:
                            kwargs[kw.arg] = elts
                    continue
            return kwargs
    raise AssertionError(f"no {call_name}(...) call found in {spec_path}")


def _tray_main_top_level_imports() -> set[str]:
    """Top-level module names tray_main.py imports anywhere in its source
    (module scope or inside a function, e.g. the lazily-imported ``pystray``/
    ``PIL`` inside ``_run_tray``) -- excludes relative imports (``from .
    local_runner import ...``, ``from . import __main__``), which are
    covered by their own explicit hiddenimports checks below instead.
    """
    tree = ast.parse(_TRAY_MAIN_PATH.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                modules.add(node.module.split(".")[0])
    return modules


class TestTraySpecPreShipConsistency:
    """Cross-checks between the real, committed meridian-tray.spec,
    meridian/static/meridian-tray.ico, and tray_main.py."""

    def test_ico_asset_exists_and_has_valid_ico_magic(self):
        assert _ICO_PATH.is_file(), f"missing tray icon asset: {_ICO_PATH}"
        header = _ICO_PATH.read_bytes()[:4]
        # ICO file format header: reserved(2)=0x0000, type(2)=0x0001.
        assert header == b"\x00\x00\x01\x00", (
            f"{_ICO_PATH} does not have a valid .ico header (got {header!r})"
        )

    def test_spec_datas_paths_all_exist_relative_to_repo_root(self):
        datas = _spec_call_kwargs(_TRAY_SPEC_PATH, "Analysis")["datas"]
        assert datas, "meridian-tray.spec Analysis(datas=...) is empty"
        for src, _dest in datas:
            path = _REPO_ROOT / src
            assert path.exists(), (
                f"meridian-tray.spec datas=... references {src!r}, which "
                f"does not exist at {path}"
            )

    def test_spec_bundles_the_real_static_and_templates_directories(self):
        # Regression check for the two documented live-build failures in the
        # spec file's own comments: a missing 'meridian/static' entry 500'd
        # the bundled server's StaticFiles mount, and a missing
        # 'meridian/templates' entry 500'd `GET /` (Jinja2Templates).
        datas_sources = {src for src, _dest in _spec_call_kwargs(_TRAY_SPEC_PATH, "Analysis")["datas"]}
        assert "meridian/static" in datas_sources
        assert "meridian/templates" in datas_sources

    def test_spec_does_not_bundle_the_icon_a_second_time_separately(self):
        # 2026-09-28 review finding #23: meridian-tray.ico used to be
        # bundled TWICE -- once via a standalone datas entry at the bundle
        # root, again as part of the whole meridian/static directory copy.
        # There must now be exactly one datas entry whose source is the
        # icon file itself (the whole-directory 'meridian/static' entry
        # still carries it, just not as a SEPARATE, redundant entry).
        datas_sources = [src for src, _dest in _spec_call_kwargs(_TRAY_SPEC_PATH, "Analysis")["datas"]]
        assert datas_sources.count("meridian/static/meridian-tray.ico") == 0
        assert datas_sources.count("meridian/static") == 1

    def test_spec_exe_icon_kwarg_points_at_the_real_ico_file(self):
        # occurrence=2: the Windows-only EXE() call (see 73257801) -- the
        # macOS EXE() sets no icon at all (BUNDLE() carries it there instead).
        icon = _spec_call_kwargs(_TRAY_SPEC_PATH, "EXE", occurrence=2).get("icon")
        assert icon == "meridian/static/meridian-tray.ico"
        assert (_REPO_ROOT / icon).is_file()

    def test_spec_exe_is_a_windowed_gui_app_not_a_console_tool(self):
        exe_kwargs = _spec_call_kwargs(_TRAY_SPEC_PATH, "EXE")
        assert exe_kwargs.get("console") is False, (
            "meridian-tray.exe is a tray/GUI app -- console=True would pop "
            "a terminal window behind the tray icon on every launch"
        )

    def test_spec_exe_name_matches_the_documented_binary_name(self):
        assert _spec_call_kwargs(_TRAY_SPEC_PATH, "EXE").get("name") == "meridian-tray"

    def test_spec_exe_is_a_single_onefile_binary(self):
        # The module docstring promises "a single binary that is BOTH the
        # tray icon and ... the real Meridian HTTP server" -- onefile=False
        # would ship a directory instead, breaking that contract silently.
        # This is the Windows shape specifically (occurrence=2, see
        # 73257801) -- macOS deliberately uses onedir+BUNDLE instead, since
        # pystray's menu-bar icon needs a real .app bundle to show at all.
        assert _spec_call_kwargs(_TRAY_SPEC_PATH, "EXE", occurrence=2).get("onefile") is True

    def test_spec_hiddenimports_has_no_duplicate_entries(self):
        hidden = _spec_call_kwargs(_TRAY_SPEC_PATH, "Analysis")["hiddenimports"]
        assert len(hidden) == len(set(hidden)), (
            f"duplicate entries in meridian-tray.spec hiddenimports: {hidden}"
        )

    @pytest.mark.parametrize(
        "required_entry",
        [
            "meridian.tray_main",  # PyInstaller's own Analysis(['meridian/tray_main.py']) entry
            "meridian.__main__",  # main()'s `from . import __main__` (--run-server dispatch)
            "meridian.server",  # the real HTTP server --run-server ultimately serves
            "meridian.local_runner",  # module-level `from .local_runner import (...)`
            "pystray._win32",  # _run_tray()'s lazily-imported `import pystray`
            "PIL",  # _run_tray()'s lazily-imported `from PIL import Image`
            "PIL.Image",
            "keyring.backends.macOS" if sys.platform == "darwin" else "keyring.backends.Windows",
        ],
    )
    def test_spec_hiddenimports_covers_tray_mains_real_dependencies(self, required_entry):
        hidden = _spec_call_kwargs(_TRAY_SPEC_PATH, "Analysis")["hiddenimports"]
        assert required_entry in hidden, (
            f"meridian-tray.spec hiddenimports is missing {required_entry!r}, "
            "which tray_main.py needs at runtime (directly, or via "
            "local_runner/server/--run-server dispatch)"
        )

    def test_spec_hiddenimports_covers_every_third_party_import_tray_main_uses(self):
        """Statically derives tray_main.py's own third-party imports (stdlib
        modules and the relative `meridian` package excluded) and asserts
        each has a matching hiddenimports entry -- so this test itself
        breaks, rather than silently drifting, the next time tray_main.py
        grows a new third-party import the spec hasn't been updated for.
        """
        third_party = _tray_main_top_level_imports() - set(sys.stdlib_module_names) - {"meridian"}
        assert third_party, "sanity check: expected at least pystray/PIL here"
        hidden = _spec_call_kwargs(_TRAY_SPEC_PATH, "Analysis")["hiddenimports"]
        for module in sorted(third_party):
            assert any(entry == module or entry.startswith(module + ".") for entry in hidden), (
                f"tray_main.py imports {module!r} but meridian-tray.spec's "
                f"hiddenimports does not declare it (or any submodule of it): {hidden}"
            )


@pytest.mark.subprocess_isolated
def test_running_tray_main_as_a_direct_script_does_not_hit_relative_import_error():
    """Regression check for the exact frozen-crash class tray_main.py's own
    module docstring documents: PyInstaller's Analysis(['meridian/tray_main.py'])
    runs this file as __main__ with __package__ unset, which historically
    crashed the frozen exe with "attempted relative import with no known
    parent package" before the module's top-of-file __package__ fixup was
    added. A real PyInstaller build isn't available in a plain pytest run,
    but invoking the script directly (not via `python -m`, which would
    already set __package__ correctly and mask the bug) reproduces the same
    "no parent package" starting condition the frozen exe hits, over a real
    subprocess -- not a mock.
    """
    result = subprocess.run(
        [sys.executable, str(_TRAY_MAIN_PATH), "--help"],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"direct-script invocation failed (rc={result.returncode}):\n{result.stderr}"
    )
    assert "attempted relative import" not in result.stderr
    assert "--run-server" not in result.stdout, (
        "the internal --run-server flag must stay hidden from --help output"
    )


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
        tray_main, "_show_error_dialog",
        lambda title, msg, parent=None: dialog_calls.append((title, msg))
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

def test_tunnel_watchdog_requires_confirmed_failures_and_exposes_restart_reason():
    now = [100.0]
    probe = mock.Mock(return_value={"state": "disconnected", "detail": "No active tunnel socket"})
    runner = mock.Mock()
    watchdog = tray_main._TunnelWatchdog(runner, status_probe=probe, clock=lambda: now[0])
    watchdog.arm()

    assert watchdog.tick() is None
    now[0] += tray_main._TUNNEL_WATCHDOG_INTERVAL_SECONDS
    reason = watchdog.tick()

    assert reason == "Hosted tunnel diagnostics reported disconnected: No active tunnel socket"
    runner.restart.assert_called_once_with(reason=reason)
    assert reason in watchdog.status_text


def test_tunnel_watchdog_ignores_indeterminate_health_and_can_be_disabled():
    probe = mock.Mock(return_value={"state": "unavailable", "detail": "hosted service unavailable"})
    runner = mock.Mock()
    watchdog = tray_main._TunnelWatchdog(runner, status_probe=probe)
    watchdog.arm()

    watchdog.tick()
    watchdog.tick()
    assert runner.restart.call_count == 0

    watchdog.set_enabled(False)
    probe.reset_mock()
    probe.return_value = {"state": "disconnected", "detail": "no socket"}
    watchdog.tick()
    probe.assert_not_called()
    assert watchdog.status_text == "disabled by user"


def test_tunnel_watchdog_caps_restart_attempts_until_manual_rearm():
    now = [0.0]
    probe = mock.Mock(return_value={"state": "disconnected", "detail": "no active socket"})
    runner = mock.Mock()
    watchdog = tray_main._TunnelWatchdog(runner, status_probe=probe, clock=lambda: now[0])
    watchdog.arm()

    for _ in range(len(tray_main._TUNNEL_WATCHDOG_BACKOFF_SECONDS)):
        assert watchdog.tick() is None
        now[0] += tray_main._TUNNEL_WATCHDOG_INTERVAL_SECONDS
        reason = watchdog.tick()
        assert reason is not None
        now[0] += tray_main._TUNNEL_WATCHDOG_INTERVAL_SECONDS

    assert runner.restart.call_count == len(tray_main._TUNNEL_WATCHDOG_BACKOFF_SECONDS)
    assert watchdog.tick() is None
    now[0] += tray_main._TUNNEL_WATCHDOG_INTERVAL_SECONDS
    paused = watchdog.tick()
    assert paused is not None and "paused" in paused
    assert runner.restart.call_count == len(tray_main._TUNNEL_WATCHDOG_BACKOFF_SECONDS)

    watchdog.arm(reset_budget=True)
    assert "paused" not in watchdog.status_text


def test_tunnel_watchdog_preference_defaults_on_and_persists(tmp_path):
    path = tmp_path / "preferences" / "tunnel-watchdog.json"

    assert tray_main._load_tunnel_watchdog_enabled(path) is True
    tray_main._save_tunnel_watchdog_enabled(False, path)
    assert tray_main._load_tunnel_watchdog_enabled(path) is False
    tray_main._save_tunnel_watchdog_enabled(True, path)
    assert tray_main._load_tunnel_watchdog_enabled(path) is True
