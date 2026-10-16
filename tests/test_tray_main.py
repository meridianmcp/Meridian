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
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

import meridian
from meridian import tray_main
from meridian import zotero_plugin_install as zpi

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
    choose_tex = mock.Mock(return_value="C:/papers/draft paper.tex")
    monkeypatch.setattr(tray_main, "_choose_tex_file", choose_tex)
    choose_overleaf_id = mock.Mock(side_effect=["project_123", "", None])
    monkeypatch.setattr(tray_main, "_choose_overleaf_project_id", choose_overleaf_id)
    latex_launch = mock.Mock()
    monkeypatch.setattr(tray_main, "_launch_meridian_latex_cli", latex_launch)
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
            "Compile a LaTeX file locally…",
            "Catalog local sessions", "Artifact capture commands", "Restart",
            "Automatic tunnel recovery", "Quit",
        ):
            callbacks[label](fake_icon, None)
        callbacks["Compile a LaTeX file locally…"](fake_icon, None)
        callbacks["Compile a LaTeX file locally…"](fake_icon, None)
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
    assert choose_tex.call_args_list == [
        mock.call("Choose a LaTeX source file to compile locally", parent=parent),
        mock.call("Choose a LaTeX source file to compile locally", parent=parent),
        mock.call("Choose a LaTeX source file to compile locally", parent=parent),
    ]
    assert choose_overleaf_id.call_args_list == [
        mock.call(parent=parent), mock.call(parent=parent), mock.call(parent=parent),
    ]
    assert latex_launch.call_args_list == [
        mock.call("compile", "C:/papers/draft paper.tex", "--project-id=project_123", parent=parent),
        mock.call("compile", "C:/papers/draft paper.tex", parent=parent),
    ]
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


def test_run_tray_dispatches_enable_picker_after_runner_lock_is_released(monkeypatch):
    fake_pystray, fake_icon = _fake_pystray_and_pil(monkeypatch)
    fake_runner = mock.MagicMock()
    fake_runner.start.return_value = _fake_status(tray_main.LocalMcpState.READY)
    fake_tunnel_runner = mock.MagicMock(command=None)
    fake_tunnel_runner.status.return_value.child.state = mock.sentinel.not_running

    def no_saved_command(*, reason):
        assert reason == "manual tunnel enable"
        raise ValueError("no prior tunnel command")

    fake_tunnel_runner.restart.side_effect = no_saved_command
    monkeypatch.setattr(tray_main, "_build_runner", lambda: fake_runner)
    monkeypatch.setattr(tray_main, "_build_tunnel_runner", lambda: fake_tunnel_runner)
    monkeypatch.setattr(tray_main, "_hosted_tunnel_status", lambda: {
        "state": "unknown",
        "detail": "not checked",
        "last_error": None,
        "base_url": "https://usemeridian.us",
        "diagnostics_url": "https://usemeridian.us/tunnel/diagnostics/tenant-123",
    })
    monkeypatch.setattr(tray_main, "_choose_project_root", mock.Mock(return_value="C:/work/project"))
    monkeypatch.setattr(tray_main.webbrowser, "open", mock.Mock())
    monkeypatch.setattr(tray_main.sys, "platform", "win32")

    watchdogs = []
    original_watchdog_init = tray_main._TunnelWatchdog.__init__

    def capture_watchdog(watchdog, *args, **kwargs):
        original_watchdog_init(watchdog, *args, **kwargs)
        watchdogs.append(watchdog)

    monkeypatch.setattr(tray_main._TunnelWatchdog, "__init__", capture_watchdog)
    real_thread = tray_main.threading.Thread
    asynchronous_ui_threads = []

    class SplitThread:
        def __init__(self, *, target, daemon, name=None, args=()):
            self.target = target
            self.daemon = daemon
            self.name = name
            self.args = args

        def start(self):
            if self.name == "meridian-tunnel-watchdog":
                return
            if getattr(self.target, "__name__", None) == "_start":
                # Keep runner work deterministic, while letting the UI dispatch
                # itself run on a real asynchronous thread below.
                self.target(*self.args)
                return

            assert watchdogs
            assert not watchdogs[0]._runner_operation_lock.locked(), (
                "UI picker callback was launched while a runner operation held its lock"
            )
            thread = real_thread(
                target=self.target,
                args=self.args,
                daemon=self.daemon,
                name=self.name,
            )
            asynchronous_ui_threads.append(thread)
            thread.start()

    monkeypatch.setattr(tray_main.threading, "Thread", SplitThread)

    def click_enable(*_args):
        callbacks = {
            call.args[0]: call.args[1]
            for call in fake_pystray.MenuItem.call_args_list
            if isinstance(call.args[0], str)
        }
        callbacks["Enable tunnel…"](fake_icon, None)

    fake_icon.run.side_effect = click_enable
    assert tray_main._run_tray() == 0

    for thread in asynchronous_ui_threads:
        thread.join(3)
        assert not thread.is_alive(), "asynchronous UI picker callback did not finish"

    tray_main._choose_project_root.assert_called_once_with(
        "Choose a local project to share through the Meridian tunnel", parent=None,
    )
    fake_tunnel_runner.restart.assert_called_once_with(reason="manual tunnel enable")
    fake_tunnel_runner.start.assert_called_once_with()


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


def test_choose_tex_file_filters_tex_sources_and_supports_cancel(monkeypatch):
    fake_tkinter = mock.MagicMock()
    fake_filedialog = mock.MagicMock()
    fake_tkinter.filedialog = fake_filedialog
    fake_filedialog.askopenfilename.side_effect = ["C:/papers/draft paper.tex", ""]
    monkeypatch.setitem(sys.modules, "tkinter", fake_tkinter)
    monkeypatch.setitem(sys.modules, "tkinter.filedialog", fake_filedialog)

    assert tray_main._choose_tex_file("Choose source") == "C:/papers/draft paper.tex"
    assert tray_main._choose_tex_file("Choose source") is None
    assert fake_filedialog.askopenfilename.call_args_list == [
        mock.call(
            parent=fake_tkinter.Tk.return_value,
            title="Choose source",
            filetypes=(("LaTeX source files", "*.tex"), ("All files", "*.*")),
        ),
        mock.call(
            parent=fake_tkinter.Tk.return_value,
            title="Choose source",
            filetypes=(("LaTeX source files", "*.tex"), ("All files", "*.*")),
        ),
    ]
    assert fake_tkinter.Tk.return_value.destroy.call_count == 2


def test_choose_tex_file_rejects_non_tex_selection(monkeypatch):
    import pytest

    fake_tkinter = mock.MagicMock()
    fake_filedialog = mock.MagicMock()
    fake_tkinter.filedialog = fake_filedialog
    fake_filedialog.askopenfilename.return_value = "C:/papers/references.bib"
    monkeypatch.setitem(sys.modules, "tkinter", fake_tkinter)
    monkeypatch.setitem(sys.modules, "tkinter.filedialog", fake_filedialog)

    with pytest.raises(ValueError, match="Choose a .tex source file"):
        tray_main._choose_tex_file("Choose source")


def test_choose_overleaf_project_id_validates_trims_and_distinguishes_blank_from_cancel(monkeypatch):
    import pytest

    fake_tkinter = mock.MagicMock()
    fake_dialog = mock.MagicMock()
    fake_tkinter.simpledialog = fake_dialog
    monkeypatch.setitem(sys.modules, "tkinter", fake_tkinter)
    parent = mock.MagicMock()
    fake_dialog.askstring.side_effect = [" project_123 ", "   ", None]

    assert tray_main._choose_overleaf_project_id(parent=parent) == "project_123"
    assert tray_main._choose_overleaf_project_id(parent=parent) == ""
    assert tray_main._choose_overleaf_project_id(parent=parent) is None
    assert fake_dialog.askstring.call_count == 3
    assert all(call.kwargs["parent"] is parent for call in fake_dialog.askstring.call_args_list)
    assert "Leave blank to compile without linking" in fake_dialog.askstring.call_args.args[1]
    assert "Cancel to cancel the compile" in fake_dialog.askstring.call_args.args[1]

    fake_dialog.askstring.side_effect = None
    fake_dialog.askstring.return_value = "../invalid"
    with pytest.raises(ValueError, match="valid Overleaf project ID"):
        tray_main._choose_overleaf_project_id(parent=parent)


def test_meridian_latex_cli_command_resolves_external_node_and_windows_npm_shim(monkeypatch, tmp_path):
    import shutil
    from pathlib import Path

    checkout = tmp_path / "source checkout"
    (checkout / "meridian").mkdir(parents=True)
    monkeypatch.setattr(tray_main, "__file__", str(checkout / "meridian" / "tray_main.py"))
    monkeypatch.setattr(tray_main.sys, "frozen", False, raising=False)
    fake_home = tmp_path / "isolated home"
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    for key in ("NPM_CONFIG_PREFIX", "npm_config_prefix", "APPDATA", "NODE_PATH"):
        monkeypatch.delenv(key, raising=False)

    npm_prefix = tmp_path / "custom npm prefix"
    shim = npm_prefix / "meridian-latex.cmd"
    shim.parent.mkdir(parents=True)
    shim.write_text("npm cmd shim is only a locator", encoding="utf-8")
    cli = npm_prefix / "node_modules" / "@meridianmcp" / "mcp" / "latex" / "cli.js"
    cli.parent.mkdir(parents=True)
    cli.write_text("#!/usr/bin/env node\n", encoding="utf-8")
    node = tmp_path / "Node Runtime" / "node.exe"

    def which(name):
        if name == "node":
            return str(node)
        if name in {"meridian-latex", "meridian-latex.cmd"}:
            return str(shim)
        return None

    monkeypatch.setattr(shutil, "which", which)
    monkeypatch.setattr(tray_main.subprocess, "run", mock.Mock(return_value=mock.Mock(returncode=0, stdout="v22.12.0\n", stderr="")))
    source = str(tmp_path / "paper path" / "draft paper & notes.tex")
    expected = [str(node), str(cli.resolve()), "compile", source]

    for frozen in (False, True):
        monkeypatch.setattr(tray_main.sys, "frozen", frozen, raising=False)
        assert tray_main._meridian_latex_cli_command("compile", source) == expected

    assert tray_main.subprocess.run.call_args_list == [
        mock.call([str(node), "--version"], capture_output=True, text=True, timeout=5, check=False, shell=False),
        mock.call([str(node), "--version"], capture_output=True, text=True, timeout=5, check=False, shell=False),
    ]


def test_meridian_latex_cli_command_reports_missing_or_old_node(monkeypatch):
    import pytest
    import shutil

    monkeypatch.setattr(shutil, "which", lambda _name: None)
    with pytest.raises(FileNotFoundError, match="Node.js 22 or newer"):
        tray_main._meridian_latex_cli_command("compile", "draft.tex")

    monkeypatch.setattr(shutil, "which", lambda name: "C:/node.exe" if name == "node" else None)
    monkeypatch.setattr(tray_main.subprocess, "run", mock.Mock(return_value=mock.Mock(returncode=0, stdout="v20.11.0\n", stderr="")))
    with pytest.raises(FileNotFoundError, match="found v20.11.0"):
        tray_main._meridian_latex_cli_command("compile", "draft.tex")


def test_frozen_tray_missing_cli_message_only_offers_global_install(monkeypatch, tmp_path):
    import shutil
    from pathlib import Path

    node = tmp_path / "Node Runtime" / "node.exe"
    monkeypatch.setattr(shutil, "which", lambda name: str(node) if name == "node" else None)
    monkeypatch.setattr(tray_main.subprocess, "run", mock.Mock(return_value=mock.Mock(returncode=0, stdout="v22.12.0\n", stderr="")))
    monkeypatch.setattr(tray_main, "__file__", str(tmp_path / "tray" / "tray_main.py"))
    monkeypatch.setattr(tray_main.sys, "frozen", True, raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "isolated home")
    for key in ("NPM_CONFIG_PREFIX", "npm_config_prefix", "APPDATA", "NODE_PATH"):
        monkeypatch.delenv(key, raising=False)

    with pytest.raises(FileNotFoundError) as exc_info:
        tray_main._meridian_latex_cli_command("compile", "draft.tex")

    assert "npm install -g @meridianmcp/mcp" in str(exc_info.value)
    assert "npm run build" not in str(exc_info.value)


def test_launch_meridian_latex_cli_uses_shell_free_argv_and_reports_missing_cli(monkeypatch):
    tex_path = "C:/papers/draft paper & notes.tex"
    command = ["C:/Node Runtime/node.exe", "C:/npm/latex/cli.js", "compile", tex_path]
    resolver = mock.Mock(return_value=command)
    popen = mock.Mock()
    monkeypatch.setattr(tray_main, "_meridian_latex_cli_command", resolver)
    monkeypatch.setattr(tray_main.subprocess, "Popen", popen)
    monkeypatch.setattr(tray_main.sys, "platform", "linux")

    tray_main._launch_meridian_latex_cli("compile", tex_path)

    resolver.assert_called_once_with("compile", tex_path)
    popen.assert_called_once_with(command, creationflags=0, shell=False)

    parent = object()
    error = mock.Mock()
    monkeypatch.setattr(tray_main, "_meridian_latex_cli_command", mock.Mock(side_effect=FileNotFoundError("install Node.js 22+")))
    monkeypatch.setattr(tray_main, "_show_error_dialog", error)
    tray_main._launch_meridian_latex_cli("compile", tex_path, parent=parent)
    error.assert_called_once_with("Local LaTeX workflow unavailable", "install Node.js 22+", parent=parent)


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
    assert "Compile a LaTeX file locally…" in labels
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
    choose_tex = mock.Mock(side_effect=["C:/papers/draft paper.tex", None])
    monkeypatch.setattr(tray_main, "_choose_tex_file", choose_tex)
    launch = mock.Mock()
    monkeypatch.setattr(tray_main, "_launch_local_cli", launch)
    latex_launch = mock.Mock()
    monkeypatch.setattr(tray_main, "_launch_meridian_latex_cli", latex_launch)

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
    choose_tex = mock.Mock(side_effect=["C:/papers/draft paper.tex", None])
    monkeypatch.setattr(tray_main, "_choose_tex_file", choose_tex)
    monkeypatch.setattr(tray_main, "_choose_overleaf_project_id", lambda parent=None: "project_123")
    latex_launch = mock.Mock()
    monkeypatch.setattr(tray_main, "_launch_meridian_latex_cli", latex_launch)

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
    callbacks["Compile a LaTeX file locally…"](None, None)
    callbacks["Compile a LaTeX file locally…"](None, None)

    assert launch.call_args_list == [
        mock.call("doctor", "--repo", "C:/work/project", cwd="C:/work/project"),
        mock.call("recovery", "catalog"),
        mock.call("artifacts", "--help"),
    ]
    assert choose_tex.call_count == 2
    latex_launch.assert_called_once_with(
        "compile", "C:/papers/draft paper.tex", "--project-id=project_123", parent=None,
    )


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


@pytest.mark.parametrize(
    "diagnostics",
    [
        {},
        {"tunnel_process": {}, "slots": {}},
        {"tunnel_process": {"any_active": "false"}, "slots": {}},
        {"slots": {"filesystem": {"process_active": "false"}}},
    ],
)
def test_hosted_tunnel_status_is_unknown_without_valid_activity_evidence(monkeypatch, diagnostics):
    import json

    from meridian import tunnel_client

    monkeypatch.setattr(tunnel_client, "_resolve_base_url", lambda: "https://example.test")
    monkeypatch.setattr(tunnel_client, "_resolve_token", lambda: "test-token")
    monkeypatch.setattr(tunnel_client, "_read_cached_token", lambda _base_url: None)
    payloads = iter([{"tenant_id": "tenant-123"}, diagnostics])

    def fake_urlopen(_request, *, timeout):
        response = mock.MagicMock()
        response.read.return_value = json.dumps(next(payloads)).encode("utf-8")
        response.__enter__.return_value = response
        return response

    monkeypatch.setattr(tray_main.urllib.request, "urlopen", fake_urlopen)

    result = tray_main._hosted_tunnel_status()

    assert result["state"] == "unknown"
    assert "valid activity evidence" in result["detail"]


def test_hosted_tunnel_status_accepts_complete_legacy_slot_activity(monkeypatch):
    import json

    from meridian import tunnel_client

    monkeypatch.setattr(tunnel_client, "_resolve_base_url", lambda: "https://example.test")
    monkeypatch.setattr(tunnel_client, "_resolve_token", lambda: "test-token")
    monkeypatch.setattr(tunnel_client, "_read_cached_token", lambda _base_url: None)
    payloads = iter([
        {"tenant_id": "tenant-123"},
        {"slots": {"filesystem": {"process_active": False}}},
    ])

    def fake_urlopen(_request, *, timeout):
        response = mock.MagicMock()
        response.read.return_value = json.dumps(next(payloads)).encode("utf-8")
        response.__enter__.return_value = response
        return response

    monkeypatch.setattr(tray_main.urllib.request, "urlopen", fake_urlopen)

    assert tray_main._hosted_tunnel_status()["state"] == "disconnected"


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
    # The Zotero plugin first-run prompt must never open a real Tk window from a test.
    monkeypatch.setattr(
        tray_main, "_schedule_zotero_plugin_first_run", lambda dispatch, **kwargs: False,
    )
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
# Zotero bridge plugin: Windows-only first-run prompt, its startup guard, the
# Not-now save failure, and the menu checkbox and env-override label.
# ---------------------------------------------------------------------------

_ZOTERO_ENV = "MERIDIAN_ZOTERO_PLUGIN_AUTOINSTALL"


def _pretend_windows(monkeypatch):
    monkeypatch.setattr(zpi, "_platform", lambda: "win32")


def test_first_run_prompt_is_scheduled_on_windows_while_undecided(monkeypatch, tmp_path):
    _pretend_windows(monkeypatch)
    queued = []
    assert tray_main._schedule_zotero_plugin_first_run(queued.append, path=tmp_path / "zotero_plugin.json") is True
    assert len(queued) == 1


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_first_run_prompt_is_never_scheduled_off_windows(monkeypatch, tmp_path, platform):
    monkeypatch.setattr(zpi, "_platform", lambda: platform)
    queued = []
    assert tray_main._schedule_zotero_plugin_first_run(queued.append, path=tmp_path / "zotero_plugin.json") is False
    assert queued == []


def test_first_run_prompt_is_not_scheduled_once_decided(monkeypatch, tmp_path):
    _pretend_windows(monkeypatch)
    path = tmp_path / "zotero_plugin.json"
    zpi.save_settings(path, {"autoinstall": False, "decided": True})
    queued = []
    assert tray_main._schedule_zotero_plugin_first_run(queued.append, path=path) is False
    assert queued == []


def test_unreadable_settings_leave_the_prompt_pending(monkeypatch, tmp_path):
    _pretend_windows(monkeypatch)
    path = tmp_path / "zotero_plugin.json"
    path.write_text("{broken", encoding="utf-8")
    queued = []
    assert tray_main._schedule_zotero_plugin_first_run(queued.append, path=path) is True
    assert len(queued) == 1


def test_run_tray_keeps_starting_when_the_first_run_prompt_cannot_be_scheduled(monkeypatch, caplog):
    _, fake_icon = _fake_pystray_and_pil(monkeypatch)

    def _broken(dispatch, **kwargs):
        raise RuntimeError("prompt scheduling broke")

    monkeypatch.setattr(tray_main, "_schedule_zotero_plugin_first_run", _broken)
    fake_runner = mock.MagicMock()
    fake_runner.start.return_value = _fake_status(tray_main.LocalMcpState.READY)
    monkeypatch.setattr(tray_main, "_build_runner", lambda: fake_runner)
    monkeypatch.setattr(tray_main.webbrowser, "open", lambda url: None)

    with caplog.at_level("WARNING"):
        rc = tray_main._run_tray()

    assert rc == 0
    fake_icon.run.assert_called_once()
    assert "prompt scheduling broke" in caplog.text


@pytest.mark.parametrize("answer", [True, False])
def test_failed_save_after_the_prompt_logs_a_warning_and_keeps_it_pending(
    monkeypatch, tmp_path, caplog, answer,
):
    monkeypatch.setattr(tray_main, "_ask_zotero_plugin_first_run", lambda parent=None: answer)
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, so the settings folder cannot be created", encoding="utf-8")
    path = blocker / "zotero_plugin.json"

    with caplog.at_level("WARNING"):
        result = tray_main._run_zotero_plugin_first_run(path=path)

    assert result is None
    assert any(
        record.levelname == "WARNING" and "zotero" in record.getMessage().lower()
        for record in caplog.records
    )
    assert tray_main.zotero_plugin_first_run_pending(tray_main.zotero_plugin.load_settings(path)) is True


def test_menu_checkbox_shows_the_saved_setting():
    assert tray_main.zotero_plugin_autoinstall_checked({"autoinstall": True, "decided": True}) is True
    assert tray_main.zotero_plugin_autoinstall_checked({"autoinstall": False, "decided": True}) is False


def test_menu_checkbox_ignores_the_env_override_and_the_label_names_it(monkeypatch):
    monkeypatch.setenv(_ZOTERO_ENV, "1")
    assert tray_main.zotero_plugin_autoinstall_checked({"autoinstall": False, "decided": True}) is False
    label = tray_main.zotero_plugin_autoinstall_label(os.environ)
    assert "overrides" in label and _ZOTERO_ENV in label


def test_menu_label_is_plain_when_no_recognised_override_is_set():
    plain = tray_main.zotero_plugin_autoinstall_label({})
    assert _ZOTERO_ENV not in plain
    assert tray_main.zotero_plugin_autoinstall_label({_ZOTERO_ENV: "maybe"}) == plain
    assert tray_main.zotero_plugin_env_override_notice({_ZOTERO_ENV: "maybe"}) is None


def test_env_override_notice_says_the_variable_overrides_the_menu():
    notice = tray_main.zotero_plugin_env_override_notice({_ZOTERO_ENV: "0"})
    assert notice is not None
    assert _ZOTERO_ENV in notice and "overrides" in notice


def test_toggle_flips_the_saved_choice_and_records_a_decision(tmp_path):
    path = tmp_path / "zotero_plugin.json"
    zpi.save_settings(path, {"autoinstall": True, "decided": False})
    updated = tray_main._toggle_zotero_plugin_preference(path)
    assert updated == {"autoinstall": False, "decided": True, "close_zotero_for_install": True}
    assert zpi.load_settings(path) == updated


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
                            if isinstance(elt, ast.Starred):
                                # `*_zotero_bridge_datas()` is computed at build time and
                                # has no literal form. test_zotero_plugin_install.py execs
                                # the spec and checks those computed entries directly.
                                continue
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


def test_tunnel_watchdog_disable_supersedes_an_inflight_restart():
    import threading

    now = [100.0]
    entered_restart = threading.Event()
    release_restart = threading.Event()

    def restart(*, reason):
        entered_restart.set()
        assert release_restart.wait(3), "test did not release the blocked restart"

    runner = mock.Mock()
    runner.restart.side_effect = restart
    probe = mock.Mock(return_value={"state": "disconnected", "detail": "no active socket"})
    watchdog = tray_main._TunnelWatchdog(runner, status_probe=probe, clock=lambda: now[0])
    watchdog.arm()
    watchdog.tick()
    now[0] += tray_main._TUNNEL_WATCHDOG_INTERVAL_SECONDS

    recovery = threading.Thread(target=watchdog.tick)
    recovery.start()
    try:
        assert entered_restart.wait(2), "watchdog did not begin its recovery restart"
        disarm_finished = threading.Event()

        def disable_recovery():
            watchdog.disarm()
            disarm_finished.set()

        disable = threading.Thread(target=disable_recovery)
        disable.start()
        assert disarm_finished.wait(1), "disarm blocked behind LocalRunner.restart"
    finally:
        release_restart.set()
        recovery.join(3)
        if "disable" in locals():
            disable.join(3)

    assert not recovery.is_alive()
    assert not disable.is_alive()
    runner.restart.assert_called_once()
    runner.stop.assert_called_once_with()
    assert "waiting for the tunnel to be enabled" in watchdog.status_text


def test_tunnel_watchdog_restart_coalesces_with_pending_manual_reconnect():
    import threading

    now = [100.0]
    entered_restart = threading.Event()
    release_restart = threading.Event()
    child_running = threading.Event()

    def restart(*, reason):
        entered_restart.set()
        assert release_restart.wait(3), "test did not release the blocked restart"
        child_running.set()

    runner = mock.Mock()
    runner.restart.side_effect = restart
    probe = mock.Mock(return_value={"state": "disconnected", "detail": "no active socket"})
    watchdog = tray_main._TunnelWatchdog(runner, status_probe=probe, clock=lambda: now[0])
    watchdog.arm()
    watchdog.tick()
    now[0] += tray_main._TUNNEL_WATCHDOG_INTERVAL_SECONDS

    recovery = threading.Thread(target=watchdog.tick)
    recovery.start()
    manual_action_started = threading.Event()
    manual_action_entered = threading.Event()
    reused_restart = []

    try:
        assert entered_restart.wait(2), "watchdog did not begin its recovery restart"
        action_generation, starting_sequence = watchdog.begin_manual_action()

        def manual_reconnect():
            manual_action_started.set()

            def run_manual_action():
                manual_action_entered.set()
                reused = watchdog.manual_action_can_reuse_restart(
                    action_generation,
                    starting_sequence,
                    child_running=child_running.is_set(),
                )
                reused_restart.append(reused)
                if not reused:
                    runner.restart(reason="manual tunnel reconnect")
                watchdog.finish_manual_action(
                    action_generation, succeeded=True, reset_budget=True,
                )

            watchdog.serialize_runner_operation(run_manual_action)

        manual = threading.Thread(target=manual_reconnect)
        manual.start()
        assert manual_action_started.wait(1)
        assert not manual_action_entered.wait(0.1), "manual operation overlapped watchdog recovery"
    finally:
        release_restart.set()
        recovery.join(3)
        if "manual" in locals():
            manual.join(3)

    assert not recovery.is_alive()
    assert not manual.is_alive()
    assert reused_restart == [True]
    runner.restart.assert_called_once()
    runner.stop.assert_not_called()


def test_queued_watchdog_restart_is_superseded_by_manual_reconnect():
    import threading

    class GatedRunnerOperationLock:
        """Hold a queued watchdog request outside the lock until manual wins."""

        def __init__(self):
            self._lock = threading.Lock()
            self.watchdog_waiting = threading.Event()
            self.allow_watchdog = threading.Event()
            self.gate_timed_out = threading.Event()
            self.acquisition_order = []

        def __enter__(self):
            name = threading.current_thread().name
            if name == "queued-watchdog-tick":
                self.watchdog_waiting.set()
                if not self.allow_watchdog.wait(3):
                    self.gate_timed_out.set()
            self._lock.acquire()
            self.acquisition_order.append(name)
            return self

        def __exit__(self, *_exc_info):
            self._lock.release()

    now = [100.0]
    runner = mock.Mock()
    probe = mock.Mock(return_value={"state": "disconnected", "detail": "no active socket"})
    watchdog = tray_main._TunnelWatchdog(runner, status_probe=probe, clock=lambda: now[0])
    operation_lock = GatedRunnerOperationLock()
    watchdog._runner_operation_lock = operation_lock
    watchdog.arm()
    watchdog.tick()
    now[0] += tray_main._TUNNEL_WATCHDOG_INTERVAL_SECONDS

    tick_result = []
    queued_tick = threading.Thread(
        target=lambda: tick_result.append(watchdog.tick()),
        name="queued-watchdog-tick",
    )
    queued_tick.start()
    manual = None
    try:
        assert operation_lock.watchdog_waiting.wait(2), "watchdog did not queue its restart request"
        action_generation, starting_sequence = watchdog.begin_manual_action()
        manual_finished = threading.Event()

        def run_manual_reconnect():
            def restart_manually():
                if not watchdog.manual_action_can_reuse_restart(
                    action_generation,
                    starting_sequence,
                    child_running=False,
                ):
                    runner.restart(reason="manual tunnel reconnect")
                watchdog.finish_manual_action(
                    action_generation, succeeded=True, reset_budget=True,
                )
                manual_finished.set()

            watchdog.serialize_runner_operation(restart_manually)

        manual = threading.Thread(target=run_manual_reconnect, name="manual-reconnect")
        manual.start()
        assert manual_finished.wait(2), "manual reconnect did not acquire the runner lock first"
        operation_lock.allow_watchdog.set()
    finally:
        operation_lock.allow_watchdog.set()
        queued_tick.join(3)
        if manual is not None:
            manual.join(3)

    assert not operation_lock.gate_timed_out.is_set()
    assert not queued_tick.is_alive()
    assert manual is not None and not manual.is_alive()
    assert operation_lock.acquisition_order == ["manual-reconnect", "queued-watchdog-tick"]
    assert tick_result == [None]
    runner.restart.assert_called_once_with(reason="manual tunnel reconnect")


def test_manual_action_during_slow_probe_discards_stale_disconnected_result():
    import threading

    probe_started = threading.Event()
    release_probe = threading.Event()
    probe_calls = [0]

    def status_probe():
        probe_calls[0] += 1
        if probe_calls[0] == 2:
            probe_started.set()
            assert release_probe.wait(3), "test did not release the slow diagnostics probe"
        return {"state": "disconnected", "detail": "stale no active socket"}

    def restart(*, reason):
        if reason == "manual tunnel reconnect":
            raise RuntimeError("manual reconnect failed")

    runner = mock.Mock()
    runner.restart.side_effect = restart
    watchdog = tray_main._TunnelWatchdog(runner, status_probe=status_probe)
    watchdog.arm()
    assert watchdog.tick() is None
    assert watchdog._consecutive_failures == 1

    tick_result = []
    slow_tick = threading.Thread(target=lambda: tick_result.append(watchdog.tick()))
    slow_tick.start()
    manual = None
    try:
        assert probe_started.wait(2), "watchdog did not enter the slow diagnostics probe"
        action_generation, starting_sequence = watchdog.begin_manual_action()
        manual_finished = threading.Event()

        def run_manual_reconnect():
            def restart_manually():
                assert not watchdog.manual_action_can_reuse_restart(
                    action_generation,
                    starting_sequence,
                    child_running=False,
                )
                try:
                    runner.restart(reason="manual tunnel reconnect")
                except RuntimeError:
                    watchdog.finish_manual_action(action_generation, succeeded=False)
                else:
                    watchdog.finish_manual_action(action_generation, succeeded=True)
                finally:
                    manual_finished.set()

            watchdog.serialize_runner_operation(restart_manually)

        manual = threading.Thread(target=run_manual_reconnect)
        manual.start()
        assert manual_finished.wait(2), "manual reconnect did not finish during the probe"
    finally:
        release_probe.set()
        slow_tick.join(3)
        if manual is not None:
            manual.join(3)

    assert not slow_tick.is_alive()
    assert manual is not None and not manual.is_alive()
    assert tick_result == [None]
    assert watchdog._consecutive_failures == 1
    runner.restart.assert_called_once_with(reason="manual tunnel reconnect")


# ---------------------------------------------------------------------------
# Zotero bridge plugin: first-run prompt and autoinstall menu toggle.
# Pure logic is tested directly. The Tk prompt is never shown: its wrapper is
# only exercised with tkinter blocked (the "no GUI" path).
# ---------------------------------------------------------------------------

_ZOTERO_PLUGIN_TOGGLE_LABEL = "Auto-install Zotero bridge plugin"
_ZOTERO_PLUGIN_PROMPT = (
    "Install the Zotero bridge plugin now? Meridian will close Zotero if it is open, install the "
    "plugin into Zotero's extensions folder, and restart Zotero if Meridian closed it. "
    "Zotero's own add-on list can remove it later."
)
_ZOTERO_PLUGIN_DOCS = _REPO_ROOT / "docs" / "zotero-plugin-install.md"


def test_first_run_prompt_wording_and_buttons_are_the_agreed_ones():
    assert tray_main.ZOTERO_PLUGIN_PROMPT_MESSAGE == _ZOTERO_PLUGIN_PROMPT
    assert tray_main.ZOTERO_PLUGIN_INSTALL_BUTTON == "Install (recommended)"
    assert tray_main.ZOTERO_PLUGIN_NOT_NOW_BUTTON == "Not now"


def test_docs_quote_the_prompt_the_tray_shows_word_for_word():
    docs = _ZOTERO_PLUGIN_DOCS.read_text(encoding="utf-8")
    assert "> " + tray_main.ZOTERO_PLUGIN_PROMPT_MESSAGE in docs
    assert "when it starts Zotero" not in docs


def test_docs_name_the_default_button_the_menu_items_and_the_save_failure_case():
    # The guide's default wording and heading must agree with what the tray shows.
    docs = _ZOTERO_PLUGIN_DOCS.read_text(encoding="utf-8")
    assert "defaults to **" + tray_main.ZOTERO_PLUGIN_INSTALL_BUTTON + "**" in docs
    assert "**" + tray_main.ZOTERO_PLUGIN_NOT_NOW_BUTTON + "**" in docs
    assert "**" + tray_main.ZOTERO_PLUGIN_INSTALL_NOW_LABEL + "**" in docs
    assert "**" + tray_main.ZOTERO_PLUGIN_MENU_LABEL + "**" in docs
    assert "## 3. Automatic install (Windows)" in docs
    assert "Your choice could not be saved" in docs


@pytest.mark.parametrize(
    ("settings", "pending"),
    [
        ({"autoinstall": True, "decided": False}, True),
        ({"autoinstall": False, "decided": True}, False),
    ],
)
def test_first_run_is_pending_only_until_the_user_answers(settings, pending):
    assert tray_main.zotero_plugin_first_run_pending(settings) is pending


def test_install_answer_enables_autoinstall_and_marks_the_choice_made():
    updated = tray_main.zotero_plugin_settings_after_answer(
        {"autoinstall": False, "decided": False}, True,
    )
    assert updated == {"autoinstall": True, "decided": True, "close_zotero_for_install": True}


def test_not_now_answer_disables_autoinstall_and_marks_the_choice_made():
    updated = tray_main.zotero_plugin_settings_after_answer(
        {"autoinstall": True, "decided": False}, False,
    )
    assert updated == {"autoinstall": False, "decided": True, "close_zotero_for_install": True}


def test_no_answer_leaves_the_settings_undecided():
    original = {"autoinstall": True, "decided": False}
    assert tray_main.zotero_plugin_settings_after_answer(original, None) == original


def test_menu_toggle_flips_autoinstall_and_counts_as_a_decision():
    on = {"autoinstall": True, "decided": False}
    off = tray_main.toggle_zotero_plugin_autoinstall(on)
    assert off == {"autoinstall": False, "decided": True, "close_zotero_for_install": True}
    assert tray_main.toggle_zotero_plugin_autoinstall(off) == {
        "autoinstall": True, "decided": True, "close_zotero_for_install": True,
    }


def test_menu_checkmark_shows_the_saved_setting_not_the_env_override(monkeypatch):
    # The checkmark is the saved choice. An env override is named in the menu label instead.
    monkeypatch.setenv("MERIDIAN_ZOTERO_PLUGIN_AUTOINSTALL", "0")
    assert tray_main.zotero_plugin_autoinstall_checked({"autoinstall": True, "decided": True}) is True
    assert tray_main.zotero_plugin_autoinstall_checked({"autoinstall": False, "decided": True}) is False


def test_undecided_first_run_is_scheduled_off_the_tray_thread(monkeypatch, tmp_path):
    _pretend_windows(monkeypatch)
    path = tmp_path / "zotero_plugin.json"
    zpi.save_settings(path, {"autoinstall": True, "decided": False})
    queued = []
    assert tray_main._schedule_zotero_plugin_first_run(queued.append, path=path) is True
    assert len(queued) == 1
    assert callable(queued[0])


def test_decided_settings_do_not_schedule_the_first_run_prompt(tmp_path):
    path = tmp_path / "zotero_plugin.json"
    zpi.save_settings(path, {"autoinstall": False, "decided": True})
    queued = []
    assert tray_main._schedule_zotero_plugin_first_run(queued.append, path=path) is False
    assert queued == []


def test_install_answer_is_saved_as_the_user_choice(monkeypatch, tmp_path):
    _pretend_windows(monkeypatch)
    path = tmp_path / "zotero_plugin.json"
    zpi.save_settings(path, {"autoinstall": True, "decided": False})
    monkeypatch.setattr(tray_main, "_ask_zotero_plugin_first_run", lambda parent=None: True)
    queued = []
    tray_main._schedule_zotero_plugin_first_run(queued.append, path=path)
    queued[0]()
    assert zpi.load_settings(path) == {"autoinstall": True, "decided": True, "close_zotero_for_install": True}


def test_not_now_answer_is_saved_as_opt_out(monkeypatch, tmp_path):
    _pretend_windows(monkeypatch)
    path = tmp_path / "zotero_plugin.json"
    zpi.save_settings(path, {"autoinstall": True, "decided": False})
    monkeypatch.setattr(tray_main, "_ask_zotero_plugin_first_run", lambda parent=None: False)
    queued = []
    tray_main._schedule_zotero_plugin_first_run(queued.append, path=path)
    queued[0]()
    assert zpi.load_settings(path) == {"autoinstall": False, "decided": True, "close_zotero_for_install": True}


def test_missing_gui_leaves_the_choice_undecided_and_never_raises(monkeypatch, tmp_path):
    _pretend_windows(monkeypatch)
    path = tmp_path / "zotero_plugin.json"
    zpi.save_settings(path, {"autoinstall": True, "decided": False})
    monkeypatch.setitem(sys.modules, "tkinter", None)  # any "import tkinter" now raises ImportError
    queued = []
    tray_main._schedule_zotero_plugin_first_run(queued.append, path=path)
    queued[0]()  # must not raise, must not block
    assert zpi.load_settings(path) == {"autoinstall": True, "decided": False, "close_zotero_for_install": True}


def test_menu_toggle_persists_the_new_preference(tmp_path):
    # Auto-install defaults to off, so the first toggle from an unanswered file turns it on.
    path = tmp_path / "zotero_plugin.json"
    first = tray_main._toggle_zotero_plugin_preference(path)
    assert first == {"autoinstall": True, "decided": True, "close_zotero_for_install": True}
    assert zpi.load_settings(path) == first


def test_run_tray_schedules_the_first_run_prompt_and_exposes_the_plugin_toggle(monkeypatch):
    # conftest pins MERIDIAN_ZOTERO_PLUGIN_AUTOINSTALL to "0"; clear it so the plain label is shown.
    monkeypatch.delenv("MERIDIAN_ZOTERO_PLUGIN_AUTOINSTALL", raising=False)
    fake_pystray, fake_icon_instance = _fake_pystray_and_pil(monkeypatch)
    runner = mock.MagicMock()
    runner.start.return_value = _fake_status(tray_main.LocalMcpState.READY)
    monkeypatch.setattr(tray_main, "_build_runner", lambda: runner)
    monkeypatch.setattr(tray_main.webbrowser, "open", lambda _url: True)
    scheduled = []
    monkeypatch.setattr(
        tray_main,
        "_schedule_zotero_plugin_first_run",
        lambda dispatch, **kwargs: scheduled.append(kwargs) or False,
    )

    assert tray_main._run_tray() == 0

    assert len(scheduled) == 1
    # The plugin toggle's label is dynamic (it names the env override when set), so resolve it.
    labels = [
        call.args[0] if isinstance(call.args[0], str) else call.args[0](None)
        for call in fake_pystray.MenuItem.call_args_list
    ]
    assert _ZOTERO_PLUGIN_TOGGLE_LABEL in labels
    assert "Zotero connection…" in labels
    fake_icon_instance.run.assert_called_once()


# ---------------------------------------------------------------------------
# Zotero bridge plugin: the Install answer installs now, the "Install Zotero
# plugin now" menu item, the toggle's override notice and the info dialog.
# Every install is faked and every dialog is recorded, so no profile, process
# list or Tk window is touched.
# ---------------------------------------------------------------------------

# Captured at import, before the autouse fixture below swaps the dialog out.
_REAL_SHOW_INFO_DIALOG = tray_main._show_info_dialog
_REPORT_TITLE = "Zotero bridge plugin"
_INSTALL_NOW_HINT = 'Close Zotero, then choose "Install Zotero plugin now" from the Meridian tray menu.'


@pytest.fixture(autouse=True)
def zotero_faked(monkeypatch):
    """No test in this file installs into a real Zotero profile or opens a real dialog."""
    recorded = SimpleNamespace(dialogs=[], errors=[])

    def _fake_install():
        return {"status": "no_profile", "path": None, "error": None}

    def _fake_info(title, message, parent=None):
        recorded.dialogs.append((title, message))

    def _fake_error(title, message, parent=None):
        recorded.errors.append((title, message))

    monkeypatch.setattr(zpi, "install_default_profile", _fake_install)
    # The install now checks the process state first, so fake it too: no test here reads the real process list.
    monkeypatch.setattr(zpi, "zotero_process_state", lambda runner=None: False)
    monkeypatch.setattr(tray_main, "_show_info_dialog", _fake_info)
    monkeypatch.setattr(tray_main, "_show_error_dialog", _fake_error)
    return recorded


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (
            {"status": "installed", "path": "C:/Zotero/extensions/a.xpi", "error": None},
            "Installed the Zotero bridge plugin to C:/Zotero/extensions/a.xpi. Start Zotero to use it.",
        ),
        (
            {"status": "unchanged", "path": "C:/Zotero/extensions/a.xpi", "error": None},
            "The Zotero bridge plugin is already up to date at C:/Zotero/extensions/a.xpi.",
        ),
        (
            {"status": "refused_running", "path": None, "error": None},
            "Zotero is running, so nothing was installed. " + _INSTALL_NOW_HINT,
        ),
        (
            {"status": "no_profile", "path": None, "error": None},
            "No Zotero profile was found, so nothing was installed. Open Zotero once so it creates "
            'a profile, close it, then choose "Install Zotero plugin now" from the Meridian tray menu.',
        ),
        (
            {"status": "refused_not_profile", "path": None, "error": "C:/p is not a Zotero profile."},
            "Nothing was installed: C:/p is not a Zotero profile.",
        ),
        (
            {"status": "failed", "path": None, "error": "disk full"},
            "The install failed: disk full",
        ),
        (
            {"status": "check_failed", "path": None, "error": "tasklist exited with code 1"},
            "Meridian could not check whether Zotero is running, so nothing was installed. "
            'Make sure Zotero is closed, then choose "Install Zotero plugin now" from the Meridian tray menu.',
        ),
    ],
)
def test_install_result_text_names_each_outcome(result, expected):
    assert tray_main.zotero_plugin_install_text(result) == expected


def test_check_failed_is_a_known_install_status_and_is_not_rewritten_as_failed():
    result = tray_main.zotero_plugin_install_now_result(
        lambda: {"status": "check_failed", "path": None, "error": "tasklist exited with code 1"},
    )
    assert result["status"] == "check_failed"


@pytest.mark.parametrize(
    "status", ["installed", "unchanged", "refused_running", "check_failed", "no_profile", "failed"],
)
def test_first_run_install_text_starts_by_saying_the_choice_was_saved(status):
    text = tray_main.zotero_plugin_install_text({"status": status, "path": "P", "error": "E"}, first_run=True)
    assert text.startswith("Your choice was saved. ")


def test_first_run_refusal_while_zotero_runs_records_the_choice_and_names_the_menu_item():
    text = tray_main.zotero_plugin_install_text({"status": "refused_running", "path": None}, first_run=True)
    assert text == "Your choice was saved. Zotero is running, so nothing was installed. " + _INSTALL_NOW_HINT


def test_an_install_that_raises_becomes_a_failed_result_and_never_raises():
    def _boom():
        raise PermissionError("access denied")

    result = tray_main.zotero_plugin_install_now_result(_boom)
    assert result["status"] == "failed"
    assert "access denied" in result["error"]


@pytest.mark.parametrize("answer", [None, "installed", ["installed"], {"status": "exploded"}])
def test_an_unrecognised_install_answer_is_reported_as_failed(answer):
    assert tray_main.zotero_plugin_install_now_result(lambda: answer)["status"] == "failed"


def test_install_message_runs_the_install_once_and_returns_its_text():
    calls = []

    def _install():
        calls.append("install")
        return {"status": "refused_running", "path": None}

    text = tray_main.zotero_plugin_install_message(_install)
    assert calls == ["install"]
    assert text == "Zotero is running, so nothing was installed. " + _INSTALL_NOW_HINT


def test_install_message_never_raises_even_when_the_install_does():
    def _boom():
        raise RuntimeError("tasklist exploded")

    assert tray_main.zotero_plugin_install_message(_boom) == "The install failed: tasklist exploded"


def test_install_answer_installs_now_and_reports_the_result(monkeypatch, tmp_path, zotero_faked):
    monkeypatch.delenv(_ZOTERO_ENV, raising=False)  # conftest pins it to "0"; this test wants no override
    path = tmp_path / "zotero_plugin.json"
    monkeypatch.setattr(tray_main, "_ask_zotero_plugin_first_run", lambda parent=None: True)
    monkeypatch.setattr(
        zpi, "install_default_profile", lambda: {"status": "installed", "path": "P", "error": None},
    )

    result = tray_main._run_zotero_plugin_first_run(path=path)

    assert result == {"autoinstall": True, "decided": True, "close_zotero_for_install": True}
    assert zpi.load_settings(path) == {"autoinstall": True, "decided": True, "close_zotero_for_install": True}
    assert zotero_faked.dialogs == [
        (_REPORT_TITLE, "Your choice was saved. Installed the Zotero bridge plugin to P. Start Zotero to use it."),
    ]


def test_install_answer_while_zotero_runs_records_the_choice_and_says_what_to_do(
    monkeypatch, tmp_path, zotero_faked,
):
    monkeypatch.delenv(_ZOTERO_ENV, raising=False)
    path = tmp_path / "zotero_plugin.json"
    monkeypatch.setattr(tray_main, "_ask_zotero_plugin_first_run", lambda parent=None: True)
    monkeypatch.setattr(
        zpi, "install_default_profile", lambda: {"status": "refused_running", "path": None, "error": None},
    )

    tray_main._run_zotero_plugin_first_run(path=path)

    assert zpi.load_settings(path) == {"autoinstall": True, "decided": True, "close_zotero_for_install": True}
    assert zotero_faked.dialogs == [
        (_REPORT_TITLE, "Your choice was saved. Zotero is running, so nothing was installed. " + _INSTALL_NOW_HINT),
    ]


def test_install_answer_survives_an_install_that_raises(monkeypatch, tmp_path, zotero_faked):
    monkeypatch.delenv(_ZOTERO_ENV, raising=False)
    path = tmp_path / "zotero_plugin.json"
    monkeypatch.setattr(tray_main, "_ask_zotero_plugin_first_run", lambda parent=None: True)

    def _boom():
        raise RuntimeError("tasklist exploded")

    monkeypatch.setattr(zpi, "install_default_profile", _boom)

    result = tray_main._run_zotero_plugin_first_run(path=path)

    assert result == {"autoinstall": True, "decided": True, "close_zotero_for_install": True}
    assert zotero_faked.dialogs == [
        (_REPORT_TITLE, "Your choice was saved. The install failed: tasklist exploded"),
    ]


_ENV_BLOCKED_TEXT = (
    "Your choice was saved. MERIDIAN_ZOTERO_PLUGIN_AUTOINSTALL turns automatic installs off, "
    'so nothing was installed now. Choose "Install Zotero plugin now" in the Meridian tray menu to install it.'
)


@pytest.mark.parametrize("raw", ["0", "false", "NO", " off "])
def test_install_answer_saves_the_choice_but_installs_nothing_when_the_env_var_turns_automatic_installs_off(
    monkeypatch, tmp_path, zotero_faked, raw,
):
    # The Install answer is an automatic install, so the env var blocks it. The choice is still saved.
    monkeypatch.setenv(_ZOTERO_ENV, raw)
    path = tmp_path / "zotero_plugin.json"
    calls = []
    monkeypatch.setattr(tray_main, "_ask_zotero_plugin_first_run", lambda parent=None: True)
    monkeypatch.setattr(
        zpi, "install_default_profile", lambda: calls.append("install") or {"status": "installed", "path": "P"},
    )

    result = tray_main._run_zotero_plugin_first_run(path=path)

    assert calls == []
    assert result == {"autoinstall": True, "decided": True, "close_zotero_for_install": True}
    assert zpi.load_settings(path) == {"autoinstall": True, "decided": True, "close_zotero_for_install": True}
    assert zotero_faked.dialogs == [(_REPORT_TITLE, _ENV_BLOCKED_TEXT)]


@pytest.mark.parametrize("raw", ["1", "yes"])
def test_install_answer_still_installs_when_the_env_var_turns_automatic_installs_on(
    monkeypatch, tmp_path, zotero_faked, raw,
):
    monkeypatch.setenv(_ZOTERO_ENV, raw)
    path = tmp_path / "zotero_plugin.json"
    monkeypatch.setattr(tray_main, "_ask_zotero_plugin_first_run", lambda parent=None: True)
    monkeypatch.setattr(
        zpi, "install_default_profile", lambda: {"status": "installed", "path": "P", "error": None},
    )

    tray_main._run_zotero_plugin_first_run(path=path)

    assert zotero_faked.dialogs == [
        (_REPORT_TITLE, "Your choice was saved. Installed the Zotero bridge plugin to P. Start Zotero to use it."),
    ]


def test_install_now_menu_item_runs_even_when_the_env_var_turns_automatic_installs_off(
    monkeypatch, zotero_faked,
):
    # The menu item is an explicit request, so the env var does not stop it.
    monkeypatch.setenv(_ZOTERO_ENV, "0")
    monkeypatch.setattr(
        zpi, "install_default_profile", lambda: {"status": "unchanged", "path": "P", "error": None},
    )
    queued = []

    tray_main._install_zotero_plugin_now_and_report(queued.append)
    queued[0]()

    assert zotero_faked.dialogs == [(_REPORT_TITLE, "The Zotero bridge plugin is already up to date at P.")]


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        (
            True,
            "Meridian could not save your Zotero plugin choice: read-only settings folder. "
            "Nothing was installed, and the prompt will ask again on the next launch. "
            'To install now, choose "Install Zotero plugin now" from the Meridian tray menu.',
        ),
        (
            False,
            "Meridian could not save your Zotero plugin choice: read-only settings folder. "
            "Nothing was installed, and the prompt will ask again on the next launch.",
        ),
    ],
)
def test_first_run_save_failure_is_reported_and_nothing_is_installed(
    monkeypatch, tmp_path, zotero_faked, answer, expected,
):
    path = tmp_path / "zotero_plugin.json"
    calls = []
    monkeypatch.setattr(tray_main, "_ask_zotero_plugin_first_run", lambda parent=None: answer)
    monkeypatch.setattr(zpi, "install_default_profile", lambda: calls.append("install") or {"status": "installed"})

    def _cannot_save(target, settings):
        raise OSError("read-only settings folder")

    monkeypatch.setattr(zpi, "save_settings", _cannot_save)

    assert tray_main._run_zotero_plugin_first_run(path=path) is None
    assert calls == []
    assert zotero_faked.dialogs == []
    assert zotero_faked.errors == [(_REPORT_TITLE, expected)]


@pytest.mark.parametrize("answer", [False, None])
def test_answers_other_than_install_never_run_the_install(monkeypatch, tmp_path, zotero_faked, answer):
    path = tmp_path / "zotero_plugin.json"
    calls = []
    monkeypatch.setattr(tray_main, "_ask_zotero_plugin_first_run", lambda parent=None: answer)
    monkeypatch.setattr(zpi, "install_default_profile", lambda: calls.append("install") or {"status": "installed"})

    tray_main._run_zotero_plugin_first_run(path=path)

    assert calls == []
    assert zotero_faked.dialogs == []


def test_install_now_action_shows_the_result_only_once_it_is_dispatched(monkeypatch, zotero_faked):
    monkeypatch.setattr(
        zpi, "install_default_profile", lambda: {"status": "unchanged", "path": "P", "error": None},
    )
    queued = []

    tray_main._install_zotero_plugin_now_and_report(queued.append)

    assert len(queued) == 1
    assert zotero_faked.dialogs == []
    queued[0]()
    assert zotero_faked.dialogs == [(_REPORT_TITLE, "The Zotero bridge plugin is already up to date at P.")]


def test_install_now_action_logs_a_broken_dispatcher_instead_of_raising(caplog):
    def _broken(callback):
        raise RuntimeError("dispatch broke")

    with caplog.at_level("WARNING"):
        tray_main._install_zotero_plugin_now_and_report(_broken)

    assert "dispatch broke" in caplog.text


def test_a_broken_result_dialog_is_logged_and_never_raised(monkeypatch, caplog):
    def _broken(title, message, parent=None):
        raise RuntimeError("no display")

    monkeypatch.setattr(tray_main, "_show_info_dialog", _broken)
    with caplog.at_level("WARNING"):
        tray_main._report_zotero_plugin_install("text")

    assert "no display" in caplog.text


@pytest.mark.parametrize(("platform", "offered"), [("win32", True), ("linux", False)])
def test_install_now_menu_item_is_offered_only_where_the_install_is_supported(monkeypatch, platform, offered):
    monkeypatch.setattr(zpi, "_platform", lambda: platform)
    fake_pystray, _fake_icon = _fake_pystray_and_pil(monkeypatch)
    runner = mock.MagicMock()
    runner.start.return_value = _fake_status(tray_main.LocalMcpState.READY)
    monkeypatch.setattr(tray_main, "_build_runner", lambda: runner)
    monkeypatch.setattr(tray_main.webbrowser, "open", lambda _url: True)

    assert tray_main._run_tray() == 0

    labels = [
        call.args[0] for call in fake_pystray.MenuItem.call_args_list if isinstance(call.args[0], str)
    ]
    assert (tray_main.ZOTERO_PLUGIN_INSTALL_NOW_LABEL in labels) is offered


@pytest.mark.parametrize(
    ("env", "notice_expected"),
    [({_ZOTERO_ENV: "0"}, True), ({_ZOTERO_ENV: "yes"}, True), ({}, False), ({_ZOTERO_ENV: "maybe"}, False)],
)
def test_menu_toggle_saves_then_shows_the_override_notice_only_when_it_applies(zotero_faked, env, notice_expected):
    saved = []
    queued = []

    tray_main._toggle_zotero_plugin_from_menu(
        queued.append, toggle=lambda: saved.append("toggled"), env=env,
    )

    assert saved == ["toggled"]
    if not notice_expected:
        assert queued == []
        return
    assert len(queued) == 1
    queued[0]()
    assert zotero_faked.dialogs == [(_REPORT_TITLE, tray_main.zotero_plugin_env_override_notice(env))]
    assert _ZOTERO_ENV in zotero_faked.dialogs[0][1]


def test_menu_toggle_reports_a_save_failure_and_skips_the_notice(zotero_faked):
    queued = []

    def _cannot_save():
        raise OSError("read-only settings folder")

    tray_main._toggle_zotero_plugin_from_menu(queued.append, toggle=_cannot_save, env={_ZOTERO_ENV: "0"})

    assert len(queued) == 1
    queued[0]()
    assert zotero_faked.errors == [
        ("Zotero plugin preference not saved", "The setting could not be saved: read-only settings folder"),
    ]
    assert zotero_faked.dialogs == []


def _fake_tkinter(monkeypatch):
    fake_tk = mock.MagicMock(name="tkinter")
    fake_messagebox = mock.MagicMock(name="tkinter.messagebox")
    fake_tk.messagebox = fake_messagebox
    monkeypatch.setitem(sys.modules, "tkinter", fake_tk)
    monkeypatch.setitem(sys.modules, "tkinter.messagebox", fake_messagebox)
    return fake_tk, fake_messagebox


def test_info_dialog_without_a_parent_owns_a_hidden_root_and_destroys_it(monkeypatch):
    fake_tk, fake_messagebox = _fake_tkinter(monkeypatch)

    _REAL_SHOW_INFO_DIALOG(_REPORT_TITLE, "The message", parent=None)

    root = fake_tk.Tk.return_value
    root.withdraw.assert_called_once_with()
    fake_messagebox.showinfo.assert_called_once_with(_REPORT_TITLE, "The message", parent=root)
    root.destroy.assert_called_once_with()


def test_info_dialog_with_a_parent_uses_it_and_leaves_it_alone(monkeypatch):
    fake_tk, fake_messagebox = _fake_tkinter(monkeypatch)
    parent = mock.MagicMock(name="parent-root")

    _REAL_SHOW_INFO_DIALOG(_REPORT_TITLE, "The message", parent=parent)

    fake_tk.Tk.assert_not_called()
    fake_messagebox.showinfo.assert_called_once_with(_REPORT_TITLE, "The message", parent=parent)
    parent.withdraw.assert_not_called()
    parent.destroy.assert_not_called()


def _run_tray_and_collect_menu(monkeypatch):
    """Run _run_tray with fake pystray and return the fake, so tests can reach the menu callbacks."""
    fake_pystray, _fake_icon = _fake_pystray_and_pil(monkeypatch)
    runner = mock.MagicMock()
    runner.start.return_value = _fake_status(tray_main.LocalMcpState.READY)
    monkeypatch.setattr(tray_main, "_build_runner", lambda: runner)
    monkeypatch.setattr(tray_main.webbrowser, "open", lambda _url: True)
    assert tray_main._run_tray() == 0
    return fake_pystray


def _menu_callback(fake_pystray, function_name):
    for call in fake_pystray.MenuItem.call_args_list:
        if len(call.args) > 1 and getattr(call.args[1], "__name__", "") == function_name:
            return call.args[1]
    raise AssertionError(f"no menu item is wired to {function_name}")


def test_install_now_menu_callback_installs_and_shows_the_result(monkeypatch, zotero_faked):
    monkeypatch.setattr(zpi, "_platform", lambda: "win32")
    monkeypatch.setattr(
        zpi, "install_default_profile", lambda: {"status": "unchanged", "path": "P", "error": None},
    )
    fake_pystray = _run_tray_and_collect_menu(monkeypatch)

    _menu_callback(fake_pystray, "_install_zotero_plugin_now")(None, None)

    # The install runs on its own thread, so wait (bounded) for the recorded dialog.
    deadline = time.monotonic() + 5
    while not zotero_faked.dialogs and time.monotonic() < deadline:
        time.sleep(0.01)
    assert zotero_faked.dialogs == [(_REPORT_TITLE, "The Zotero bridge plugin is already up to date at P.")]


def test_toggle_menu_callback_saves_the_flipped_preference(monkeypatch, tmp_path):
    path = tmp_path / "zotero_plugin.json"
    zpi.save_settings(path, {"autoinstall": False, "decided": True})
    monkeypatch.setattr(zpi, "settings_path", lambda: path)
    monkeypatch.setattr(zpi, "_platform", lambda: "win32")
    monkeypatch.delenv(_ZOTERO_ENV, raising=False)  # no override, so no notice thread is started
    fake_pystray = _run_tray_and_collect_menu(monkeypatch)

    _menu_callback(fake_pystray, "_toggle_zotero_plugin_autoinstall")(None, None)

    assert zpi.load_settings(path) == {"autoinstall": True, "decided": True, "close_zotero_for_install": True}


# ---------------------------------------------------------------------------
# Closing Zotero for the install: prompt, messages, the close toggle
# ---------------------------------------------------------------------------

_ZOTERO_CLOSE_LABEL = "Close Zotero to install the plugin"
_CLOSE_OVERRIDE_ENV = "MERIDIAN_ZOTERO_CLOSE_ON_INSTALL"
_CLOSED_AND_RESTARTED_TAIL = " No further action is needed."


def test_prompt_says_the_install_closes_and_restarts_zotero():
    prompt = tray_main.ZOTERO_PLUGIN_PROMPT_MESSAGE
    assert "close Zotero if it is open" in prompt
    assert "restart Zotero" in prompt


def test_closed_and_restarted_message_names_the_result_and_the_next_step():
    text = tray_main.zotero_plugin_install_text({
        "status": "installed", "path": "P", "closed_zotero": True, "restart": "started",
    })
    assert text == (
        "Meridian closed Zotero, installed the Zotero bridge plugin to P, and restarted Zotero."
        + _CLOSED_AND_RESTARTED_TAIL
    )


def test_closed_and_restarted_message_for_a_plugin_that_was_already_up_to_date():
    text = tray_main.zotero_plugin_install_text({
        "status": "unchanged", "path": "P", "closed_zotero": True, "restart": "already_running",
    })
    assert text == (
        "Meridian closed Zotero, the Zotero bridge plugin was already up to date at P, and restarted Zotero."
        + _CLOSED_AND_RESTARTED_TAIL
    )


@pytest.mark.parametrize("restart", ["timeout", "not_installed", "error"])
def test_closed_but_not_restarted_message_says_to_start_zotero(restart):
    text = tray_main.zotero_plugin_install_text({
        "status": "installed", "path": "P", "closed_zotero": True, "restart": restart,
    })
    assert text == (
        "Meridian closed Zotero and installed the Zotero bridge plugin to P, but could not restart Zotero. "
        "Start Zotero yourself to use the plugin."
    )


def test_close_failed_message_says_zotero_did_not_close_and_to_install_again():
    text = tray_main.zotero_plugin_install_text({"status": "close_failed", "path": None, "closed_zotero": False})
    assert text == (
        "Zotero did not close, so nothing was installed. Close Zotero yourself, then choose "
        '"Install Zotero plugin now" from the Meridian tray menu.'
    )


def test_close_failed_message_is_prefixed_after_a_first_run_answer():
    text = tray_main.zotero_plugin_install_text({"status": "close_failed", "path": None}, first_run=True)
    assert text.startswith("Your choice was saved. Zotero did not close, so nothing was installed.")
    assert "Install Zotero plugin now" in text


def test_refused_running_message_still_says_to_close_zotero_and_install_now():
    text = tray_main.zotero_plugin_install_text({"status": "refused_running", "path": None})
    assert text == (
        "Zotero is running, so nothing was installed. Close Zotero, then choose "
        '"Install Zotero plugin now" from the Meridian tray menu.'
    )


def test_close_failed_is_a_known_install_status_so_the_tray_shows_its_message():
    result = tray_main.zotero_plugin_install_now_result(lambda: {"status": "close_failed", "path": None})
    assert result["status"] == "close_failed"


def test_toggle_close_flips_only_the_close_setting_and_leaves_the_first_run_undecided():
    # Touching the close choice is not an answer to the first-run prompt, so the prompt still asks.
    on = {"autoinstall": True, "decided": False, "close_zotero_for_install": True}
    off = tray_main.toggle_zotero_plugin_close(on)
    assert off == {"autoinstall": True, "decided": False, "close_zotero_for_install": False}
    assert tray_main.toggle_zotero_plugin_close(off)["close_zotero_for_install"] is True


def test_toggle_autoinstall_keeps_the_saved_close_setting():
    saved = {"autoinstall": False, "decided": True, "close_zotero_for_install": False}
    assert tray_main.toggle_zotero_plugin_autoinstall(saved) == {
        "autoinstall": True, "decided": True, "close_zotero_for_install": False,
    }


def test_close_checkmark_shows_the_saved_setting_not_the_env_override(monkeypatch):
    monkeypatch.setenv(_CLOSE_OVERRIDE_ENV, "0")
    assert tray_main.zotero_plugin_close_checked({"close_zotero_for_install": True}) is True
    assert tray_main.zotero_plugin_close_checked({"close_zotero_for_install": False}) is False


def test_close_label_is_plain_when_no_env_override_is_set():
    assert tray_main.zotero_plugin_close_label({}) == _ZOTERO_CLOSE_LABEL
    assert tray_main.ZOTERO_PLUGIN_CLOSE_MENU_LABEL == _ZOTERO_CLOSE_LABEL


def test_close_label_says_the_env_override_applies_when_it_is_set():
    label = tray_main.zotero_plugin_close_label({_CLOSE_OVERRIDE_ENV: "1"})
    assert label == _ZOTERO_CLOSE_LABEL + " (MERIDIAN_ZOTERO_CLOSE_ON_INSTALL overrides this)"


def test_close_env_notice_only_appears_for_a_recognised_value():
    assert tray_main.zotero_plugin_close_env_notice({}) is None
    assert tray_main.zotero_plugin_close_env_notice({_CLOSE_OVERRIDE_ENV: "maybe"}) is None
    notice = tray_main.zotero_plugin_close_env_notice({_CLOSE_OVERRIDE_ENV: "off"})
    assert notice is not None
    assert _CLOSE_OVERRIDE_ENV in notice and "overrides" in notice


def test_close_toggle_persists_the_new_preference(tmp_path):
    path = tmp_path / "zotero_plugin.json"
    zpi.save_settings(path, {"autoinstall": True, "decided": True, "close_zotero_for_install": True})
    updated = tray_main._toggle_zotero_plugin_close_preference(path)
    assert updated == {"autoinstall": True, "decided": True, "close_zotero_for_install": False}
    assert zpi.load_settings(path) == updated


def test_install_now_refuses_without_closing_when_the_saved_setting_is_off(monkeypatch, tmp_path):
    path = tmp_path / "zotero_plugin.json"
    zpi.save_settings(path, {"autoinstall": False, "decided": True, "close_zotero_for_install": False})
    monkeypatch.setattr(zpi, "settings_path", lambda: path)
    monkeypatch.setattr(zpi, "zotero_process_state", lambda runner=None: True)
    monkeypatch.setattr(zpi, "close_zotero_gracefully", lambda **kwargs: pytest.fail("the close must not run"))
    text = tray_main._install_zotero_plugin_now_and_report(lambda callback: None)
    assert text == tray_main.zotero_plugin_install_text({"status": "refused_running", "path": None})


def test_install_now_closes_restarts_and_reports_when_the_setting_is_on(monkeypatch, tmp_path):
    from meridian import zotero_launcher

    path = tmp_path / "zotero_plugin.json"
    zpi.save_settings(path, {"autoinstall": False, "decided": True, "close_zotero_for_install": True})
    monkeypatch.setattr(zpi, "settings_path", lambda: path)
    states = [True, False, False]
    monkeypatch.setattr(
        zpi, "zotero_process_state", lambda runner=None: states.pop(0) if len(states) > 1 else states[0],
    )
    monkeypatch.setattr(zpi, "close_zotero_gracefully", lambda **kwargs: "closed")
    monkeypatch.setattr(
        zotero_launcher, "start_zotero", lambda **kwargs: {"action": "started", "exe": "Z", "api_ready": True},
    )
    monkeypatch.setattr(
        zpi, "install_default_profile", lambda: {"status": "installed", "path": "P", "error": None},
    )
    text = tray_main._install_zotero_plugin_now_and_report(lambda callback: None)
    assert text == (
        "Meridian closed Zotero, installed the Zotero bridge plugin to P, and restarted Zotero."
        + _CLOSED_AND_RESTARTED_TAIL
    )


def test_run_tray_exposes_the_close_toggle_and_it_saves_the_flip(monkeypatch, tmp_path):
    path = tmp_path / "zotero_plugin.json"
    zpi.save_settings(path, {"autoinstall": True, "decided": True, "close_zotero_for_install": True})
    monkeypatch.setattr(zpi, "settings_path", lambda: path)
    monkeypatch.setattr(zpi, "_platform", lambda: "win32")
    monkeypatch.delenv(_ZOTERO_ENV, raising=False)  # no env override, so no notice thread is started
    monkeypatch.delenv(_CLOSE_OVERRIDE_ENV, raising=False)
    fake_pystray = _run_tray_and_collect_menu(monkeypatch)

    _menu_callback(fake_pystray, "_toggle_zotero_plugin_close")(None, None)

    assert zpi.load_settings(path) == {"autoinstall": True, "decided": True, "close_zotero_for_install": False}


def test_docs_name_the_close_toggle_label_and_the_keep_open_flag():
    docs = _ZOTERO_PLUGIN_DOCS.read_text(encoding="utf-8")
    assert "**" + tray_main.ZOTERO_PLUGIN_CLOSE_MENU_LABEL + "**" in docs
    assert "--keep-zotero-open" in docs
