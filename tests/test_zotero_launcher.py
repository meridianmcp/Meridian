"""Tests for meridian.zotero_launcher, the Zotero desktop launcher core.

Nothing here starts Zotero, opens a socket or touches the real Startup folder.
Every process, network, clock and sleep dependency is injected or monkeypatched.
An autouse fixture makes any unmocked launch or probe fail closed, and points
APPDATA at a temp directory.
"""
from __future__ import annotations

import json
import os
import runpy
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from meridian import zotero_launcher as zl
from meridian import zotero_plugin_install as zpi

# Captured at import time, before the autouse fixture swaps in fakes.
_REAL_IS_WINDOWS = zl._is_windows
_REAL_PROGRAM_FILES = zl._PROGRAM_FILES_CANDIDATES

_ENV_OVERRIDE = "MERIDIAN_ZOTERO_EXE"
_STARTUP_NAME = "meridian-zotero.cmd"


class _Refused:
    """Stands in for the probe opener: behaves like Zotero being down."""

    def open(self, url, timeout=None):
        raise ConnectionRefusedError("probe blocked in tests")


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    def _blocked(*args, **kwargs):
        raise RuntimeError("real process launch is blocked in tests")

    monkeypatch.setattr(zl, "_PROBE_OPENER", _Refused())
    monkeypatch.setattr(zl.subprocess, "Popen", _blocked)
    monkeypatch.setattr(zl.subprocess, "run", _blocked)
    monkeypatch.setattr(zl, "_is_windows", lambda: False)
    monkeypatch.setattr(zl, "_PROGRAM_FILES_CANDIDATES", ())
    monkeypatch.delenv(_ENV_OVERRIDE, raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(zl, "_is_windows", lambda: True)


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"MZ")
    return path


# find_zotero_executable


def test_default_program_files_candidates_match_the_standard_install_paths():
    assert _REAL_PROGRAM_FILES == (
        r"C:\Program Files\Zotero\zotero.exe",
        r"C:\Program Files (x86)\Zotero\zotero.exe",
    )


def test_find_prefers_existing_env_override(tmp_path, monkeypatch):
    override = _touch(tmp_path / "custom" / "zotero.exe")
    pf = _touch(tmp_path / "pf" / "zotero.exe")
    local = _touch(tmp_path / "local" / "Zotero" / "zotero.exe")
    monkeypatch.setattr(zl, "_PROGRAM_FILES_CANDIDATES", (str(pf),))
    env = {_ENV_OVERRIDE: str(override), "LOCALAPPDATA": str(tmp_path / "local")}
    assert zl.find_zotero_executable(env) == override
    assert local.is_file()


def test_find_skips_env_override_that_is_not_a_file(tmp_path, monkeypatch):
    pf = _touch(tmp_path / "pf" / "zotero.exe")
    monkeypatch.setattr(zl, "_PROGRAM_FILES_CANDIDATES", (str(pf),))
    env = {_ENV_OVERRIDE: str(tmp_path / "missing" / "zotero.exe")}
    assert zl.find_zotero_executable(env) == pf


def test_find_ignores_blank_env_override(tmp_path, monkeypatch):
    pf = _touch(tmp_path / "pf" / "zotero.exe")
    monkeypatch.setattr(zl, "_PROGRAM_FILES_CANDIDATES", (str(pf),))
    assert zl.find_zotero_executable({_ENV_OVERRIDE: "   "}) == pf


def test_find_takes_first_existing_program_files_location(tmp_path, monkeypatch):
    first = _touch(tmp_path / "pf" / "zotero.exe")
    second = _touch(tmp_path / "pf86" / "zotero.exe")
    monkeypatch.setattr(zl, "_PROGRAM_FILES_CANDIDATES", (str(first), str(second)))
    assert zl.find_zotero_executable({}) == first


def test_find_falls_through_to_second_program_files_location(tmp_path, monkeypatch):
    second = _touch(tmp_path / "pf86" / "zotero.exe")
    monkeypatch.setattr(
        zl, "_PROGRAM_FILES_CANDIDATES", (str(tmp_path / "pf" / "zotero.exe"), str(second))
    )
    assert zl.find_zotero_executable({}) == second


def test_find_uses_localappdata_last(tmp_path, monkeypatch):
    local = _touch(tmp_path / "local" / "Zotero" / "zotero.exe")
    monkeypatch.setattr(zl, "_PROGRAM_FILES_CANDIDATES", (str(tmp_path / "nope.exe"),))
    assert zl.find_zotero_executable({"LOCALAPPDATA": str(tmp_path / "local")}) == local


def test_find_returns_none_when_nothing_exists(tmp_path, monkeypatch):
    monkeypatch.setattr(zl, "_PROGRAM_FILES_CANDIDATES", (str(tmp_path / "nope.exe"),))
    assert zl.find_zotero_executable({"LOCALAPPDATA": str(tmp_path / "absent")}) is None


def test_find_without_localappdata_or_override_does_not_crash(monkeypatch):
    monkeypatch.setattr(zl, "_PROGRAM_FILES_CANDIDATES", ())
    assert zl.find_zotero_executable({}) is None


def test_find_reads_os_environ_when_env_is_none(monkeypatch, tmp_path):
    exe = _touch(tmp_path / "from-environ" / "zotero.exe")
    monkeypatch.setenv(_ENV_OVERRIDE, str(exe))
    assert zl.find_zotero_executable() == exe


# is_zotero_process_running


def test_running_check_is_false_off_windows_and_never_calls_runner():
    calls = []

    def runner():
        calls.append(1)
        return "zotero.exe  1234 Console 1 100 K"

    assert zl.is_zotero_process_running(runner) is False
    assert calls == []


@pytest.mark.parametrize(
    "output, expected",
    [
        ("zotero.exe                   4312 Console                    1    210,004 K\n", True),
        ("INFO: No tasks are running which match the specified criteria.\n", False),
        ("notzotero.exe                  99 Console                    1     10 K\n", False),
        ("", False),
        ("   \n\nZOTERO.EXE 77 Services 0 5 K", True),
    ],
)
def test_running_check_parses_tasklist_output(windows, output, expected):
    assert zl.is_zotero_process_running(lambda: output) is expected


def test_running_check_swallows_runner_exception(windows):
    def boom():
        raise OSError("tasklist unavailable")

    assert zl.is_zotero_process_running(boom) is False


def test_running_check_treats_none_output_as_not_running(windows):
    assert zl.is_zotero_process_running(lambda: None) is False


def test_default_runner_queries_tasklist_with_image_filter(windows, monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(argv, 0, stdout="zotero.exe  12 Console 1 5 K\n")

    monkeypatch.setattr(zl.subprocess, "run", fake_run)
    assert zl.is_zotero_process_running() is True
    assert seen["argv"] == ["tasklist", "/FI", "IMAGENAME eq zotero.exe", "/NH"]
    assert seen["kwargs"]["capture_output"] is True
    assert seen["kwargs"]["text"] is True


def test_default_runner_handles_missing_stdout(windows, monkeypatch):
    monkeypatch.setattr(
        zl.subprocess,
        "run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 0, stdout=None),
    )
    assert zl.is_zotero_process_running() is False


# zotero_api_alive


class _Resp:
    def __init__(self, status=None):
        self.status = status
        self.closed = False

    def close(self):
        self.closed = True


class _Recorder:
    """Injected opener: records each call, then returns or raises."""

    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    def __call__(self, url, timeout=None):
        self.calls.append((url, timeout))
        if self.error is not None:
            raise self.error
        return self.result


def test_probe_url_is_the_local_library_one_item_request():
    assert zl._PROBE_URL == "http://127.0.0.1:23119/api/users/0/items?limit=1"


def test_probe_true_on_http_200_and_closes_response():
    resp = _Resp(200)
    rec = _Recorder(result=resp)
    assert zl.zotero_api_alive(timeout=0.5, opener=rec) is True
    assert rec.calls == [(zl._PROBE_URL, 0.5)]
    assert resp.closed is True


@pytest.mark.parametrize("status", [204, 403, 404, 500])
def test_probe_false_on_non_200(status):
    resp = _Resp(status)
    assert zl.zotero_api_alive(opener=_Recorder(result=resp)) is False
    assert resp.closed is True


def test_probe_false_on_connection_error():
    rec = _Recorder(error=ConnectionRefusedError("refused"))
    assert zl.zotero_api_alive(opener=rec) is False


def test_probe_false_on_urllib_http_error_500():
    err = urllib.error.HTTPError(zl._PROBE_URL, 500, "boom", None, None)
    assert zl.zotero_api_alive(opener=_Recorder(error=err)) is False


def test_probe_reads_getcode_when_status_attribute_missing():
    class _CodeOnly:
        def getcode(self):
            return 200

    assert zl.zotero_api_alive(opener=_Recorder(result=_CodeOnly())) is True


def test_probe_false_when_response_has_no_status_at_all():
    assert zl.zotero_api_alive(opener=_Recorder(result=object())) is False


def test_probe_default_opener_is_used_when_none_injected(monkeypatch):
    rec = _Recorder(result=_Resp(200))

    class _Holder:
        open = rec

    monkeypatch.setattr(zl, "_PROBE_OPENER", _Holder())
    assert zl.zotero_api_alive() is True
    assert rec.calls == [(zl._PROBE_URL, 2.0)]


def test_probe_opener_ignores_configured_http_proxy(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:8080")
    monkeypatch.setenv("http_proxy", "http://proxy.invalid:8080")

    # Control: a default opener does pick up the env proxy, so the check below is meaningful.
    control = urllib.request.build_opener()
    assert any(
        isinstance(h, urllib.request.ProxyHandler) for h in control.handle_open.get("http", [])
    )

    opener = zl._build_probe_opener()
    assert not any(
        isinstance(h, urllib.request.ProxyHandler) for h in opener.handle_open.get("http", [])
    )


def test_is_windows_reflects_sys_platform(monkeypatch):
    real_is_windows = _REAL_IS_WINDOWS
    monkeypatch.setattr(sys, "platform", "win32")
    assert real_is_windows() is True
    monkeypatch.setattr(sys, "platform", "linux")
    assert real_is_windows() is False


# start_zotero


class _Clock:
    """Fake clock: sleep() advances now() so the poll loop needs no real waiting."""

    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def _scripted_alive(monkeypatch, results):
    it = iter(results)
    monkeypatch.setattr(zl, "zotero_api_alive", lambda *a, **k: next(it))


def _fixed_find(monkeypatch, exe):
    monkeypatch.setattr(zl, "find_zotero_executable", lambda env=None: exe)


def test_start_already_running_does_not_launch(monkeypatch, tmp_path):
    exe = tmp_path / "zotero.exe"
    monkeypatch.setattr(zl, "zotero_api_alive", lambda *a, **k: True)
    _fixed_find(monkeypatch, exe)
    launched = []
    result = zl.start_zotero(launcher=launched.append)
    assert result == {"action": "already_running", "exe": str(exe), "api_ready": True}
    assert launched == []


def test_start_already_running_reports_no_exe_when_not_found(monkeypatch):
    monkeypatch.setattr(zl, "zotero_api_alive", lambda *a, **k: True)
    _fixed_find(monkeypatch, None)
    result = zl.start_zotero(launcher=lambda exe: pytest.fail("must not launch"))
    assert result == {"action": "already_running", "exe": None, "api_ready": True}


def test_start_not_installed_does_not_launch(monkeypatch):
    monkeypatch.setattr(zl, "zotero_api_alive", lambda *a, **k: False)
    _fixed_find(monkeypatch, None)
    launched = []
    result = zl.start_zotero(launcher=launched.append)
    assert result == {"action": "not_installed", "exe": None, "api_ready": False}
    assert launched == []


def test_start_launches_then_waits_until_api_ready(monkeypatch, tmp_path):
    exe = tmp_path / "zotero.exe"
    # Results: pre-launch check, poll 1 (down), poll 2 (down), poll 3 (up).
    _scripted_alive(monkeypatch, [False, False, False, True])
    _fixed_find(monkeypatch, exe)
    timer = _Clock()
    launched = []
    result = zl.start_zotero(
        timeout_s=60,
        poll_s=2,
        launcher=launched.append,
        sleep=timer.sleep,
        clock=timer.clock,
    )
    assert result == {"action": "started", "exe": str(exe), "api_ready": True}
    assert launched == [exe]
    assert timer.sleeps == [2, 2]


def test_start_times_out_when_api_never_answers(monkeypatch, tmp_path):
    exe = tmp_path / "zotero.exe"
    monkeypatch.setattr(zl, "zotero_api_alive", lambda *a, **k: False)
    _fixed_find(monkeypatch, exe)
    timer = _Clock()
    launched = []
    result = zl.start_zotero(
        timeout_s=10,
        poll_s=2,
        launcher=launched.append,
        sleep=timer.sleep,
        clock=timer.clock,
    )
    assert result == {"action": "timeout", "exe": str(exe), "api_ready": False}
    assert launched == [exe]
    assert timer.sleeps == [2, 2, 2, 2, 2]


def test_start_uses_detached_launcher_by_default(monkeypatch, tmp_path):
    exe = tmp_path / "zotero.exe"
    _scripted_alive(monkeypatch, [False, True])
    _fixed_find(monkeypatch, exe)
    launched = []
    monkeypatch.setattr(zl, "_launch_detached", launched.append)
    timer = _Clock()
    result = zl.start_zotero(sleep=timer.sleep, clock=timer.clock)
    assert result["action"] == "started"
    assert launched == [exe]


def test_start_propagates_launch_failure(monkeypatch, tmp_path):
    _fixed_find(monkeypatch, tmp_path / "zotero.exe")
    monkeypatch.setattr(zl, "zotero_api_alive", lambda *a, **k: False)

    def fail(exe):
        raise PermissionError("access denied")

    with pytest.raises(PermissionError):
        zl.start_zotero(launcher=fail)


def test_launch_detached_uses_windows_detach_flags(windows, monkeypatch, tmp_path):
    exe = tmp_path / "zotero.exe"
    seen = {}

    def fake_popen(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(zl.subprocess, "Popen", fake_popen)
    zl._launch_detached(exe)
    assert seen["argv"] == [str(exe)]
    kwargs = seen["kwargs"]
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["stdout"] == subprocess.DEVNULL
    assert kwargs["stderr"] == subprocess.DEVNULL
    assert kwargs["close_fds"] is True
    assert kwargs["creationflags"] == 0x00000008 | 0x00000200


def test_launch_detached_sets_no_creationflags_off_windows(monkeypatch, tmp_path):
    seen = {}

    def fake_popen(argv, **kwargs):
        seen["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(zl.subprocess, "Popen", fake_popen)
    zl._launch_detached(tmp_path / "zotero.exe")
    assert "creationflags" not in seen["kwargs"]
    assert seen["kwargs"]["close_fds"] is True


# status


def test_status_reports_installed_and_running(monkeypatch, tmp_path):
    exe = tmp_path / "zotero.exe"
    _fixed_find(monkeypatch, exe)
    monkeypatch.setattr(zl, "is_zotero_process_running", lambda runner=None: True)
    monkeypatch.setattr(zl, "zotero_api_alive", lambda *a, **k: False)
    assert zl.status() == {
        "installed": True,
        "exe": str(exe),
        "process_running": True,
        "api_ready": False,
    }


def test_status_reports_not_installed(monkeypatch):
    _fixed_find(monkeypatch, None)
    monkeypatch.setattr(zl, "is_zotero_process_running", lambda runner=None: False)
    monkeypatch.setattr(zl, "zotero_api_alive", lambda *a, **k: False)
    assert zl.status() == {
        "installed": False,
        "exe": None,
        "process_running": False,
        "api_ready": False,
    }


# autostart plan, install and remove


def test_plan_describes_entry_and_writes_nothing(tmp_path):
    startup = tmp_path / "startup"
    repo = tmp_path / "repo"
    plan = zl.autostart_plan(
        startup_dir=str(startup),
        python_exe=r"C:\Py\python.exe",
        repo_dir=str(repo),
    )
    assert plan["path"] == os.path.join(str(startup), _STARTUP_NAME)
    assert f'cd /d "{repo}"' in plan["content"]
    assert r'"C:\Py\python.exe" -m meridian.zotero_launcher start' in plan["content"]
    assert not startup.exists()


def test_plan_escapes_percent_signs_for_cmd(tmp_path):
    plan = zl.autostart_plan(
        startup_dir=str(tmp_path),
        python_exe="py%1.exe",
        repo_dir="C:\\50%off",
    )
    assert "py%%1.exe" in plan["content"]
    assert "C:\\50%%off" in plan["content"]


def test_plan_defaults_use_appdata_interpreter_and_repo_root(tmp_path):
    plan = zl.autostart_plan()
    expected = os.path.join(
        str(tmp_path / "roaming"),
        "Microsoft",
        "Windows",
        "Start Menu",
        "Programs",
        "Startup",
        _STARTUP_NAME,
    )
    assert plan["path"] == expected
    assert sys.executable in plan["content"]
    assert (Path(zl._repo_root()) / "meridian" / "zotero_launcher.py").is_file()


def test_default_startup_dir_falls_back_to_home_when_appdata_unset(monkeypatch, tmp_path):
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    assert zl._default_startup_dir() == os.path.join(
        str(tmp_path),
        "AppData",
        "Roaming",
        "Microsoft",
        "Windows",
        "Start Menu",
        "Programs",
        "Startup",
    )


def test_install_without_confirm_only_plans(tmp_path):
    startup = tmp_path / "Startup"
    result = zl.autostart_install(
        False,
        startup_dir=str(startup),
        python_exe="py.exe",
        repo_dir=str(tmp_path / "repo"),
    )
    assert result["written"] is False
    assert set(result) == {"path", "content", "written"}
    assert not startup.exists()


def test_install_treats_non_true_confirm_as_no(tmp_path):
    result = zl.autostart_install(None, startup_dir=str(tmp_path), python_exe="py.exe")
    assert result["written"] is False
    assert not (tmp_path / _STARTUP_NAME).exists()


def test_install_with_confirm_writes_cmd_and_creates_folder(tmp_path):
    startup = tmp_path / "Startup" / "nested"
    result = zl.autostart_install(
        True,
        startup_dir=str(startup),
        python_exe="py.exe",
        repo_dir=str(tmp_path / "repo"),
    )
    target = startup / _STARTUP_NAME
    assert result["written"] is True
    raw = target.read_bytes()
    assert raw == result["content"].encode("utf-8")
    assert b"\r\n" in raw
    assert b"meridian.zotero_launcher start" in raw


def test_remove_without_confirm_keeps_file(tmp_path):
    target = tmp_path / _STARTUP_NAME
    target.write_text("keep me", encoding="utf-8")
    result = zl.autostart_remove(False, startup_dir=str(tmp_path))
    assert result == {
        "path": os.path.join(str(tmp_path), _STARTUP_NAME),
        "existed": True,
        "removed": False,
    }
    assert target.read_text(encoding="utf-8") == "keep me"


def test_remove_with_confirm_deletes_file(tmp_path):
    target = tmp_path / _STARTUP_NAME
    target.write_text("bye", encoding="utf-8")
    result = zl.autostart_remove(True, startup_dir=str(tmp_path))
    assert result == {
        "path": os.path.join(str(tmp_path), _STARTUP_NAME),
        "existed": True,
        "removed": True,
    }
    assert not target.exists()


def test_remove_with_confirm_when_missing_reports_not_removed(tmp_path):
    result = zl.autostart_remove(True, startup_dir=str(tmp_path))
    assert result["existed"] is False
    assert result["removed"] is False


# main (CLI)


def _json_out(capsys):
    return json.loads(capsys.readouterr().out)


def test_main_status_prints_snapshot_and_exits_zero(monkeypatch, capsys, tmp_path):
    exe = tmp_path / "zotero.exe"
    _fixed_find(monkeypatch, exe)
    monkeypatch.setattr(zl, "is_zotero_process_running", lambda runner=None: True)
    monkeypatch.setattr(zl, "zotero_api_alive", lambda *a, **k: False)
    assert zl.main(["status"]) == 0
    assert _json_out(capsys) == {
        "installed": True,
        "exe": str(exe),
        "process_running": True,
        "api_ready": False,
    }


def test_main_status_when_not_installed_still_exits_zero(monkeypatch, capsys):
    _fixed_find(monkeypatch, None)
    monkeypatch.setattr(zl, "is_zotero_process_running", lambda runner=None: False)
    monkeypatch.setattr(zl, "zotero_api_alive", lambda *a, **k: False)
    assert zl.main(["status"]) == 0
    assert _json_out(capsys)["installed"] is False


def test_main_autostart_plan_prints_plan_without_writing(capsys, tmp_path):
    assert zl.main(["autostart-plan"]) == 0
    out = _json_out(capsys)
    assert set(out) == {"path", "content"}
    assert out["path"].endswith(_STARTUP_NAME)
    assert not (tmp_path / "roaming").exists()


def test_main_autostart_install_without_yes_changes_nothing(capsys, tmp_path):
    assert zl.main(["autostart-install"]) == 0
    out = _json_out(capsys)
    assert out["written"] is False
    assert not Path(out["path"]).exists()


def test_main_autostart_install_with_yes_writes_into_startup(capsys):
    assert zl.main(["autostart-install", "--yes"]) == 0
    out = _json_out(capsys)
    assert out["written"] is True
    assert Path(out["path"]).is_file()


def test_main_autostart_remove_needs_yes_to_delete(capsys):
    zl.autostart_install(True)
    capsys.readouterr()
    installed_path = zl.autostart_plan()["path"]

    assert zl.main(["autostart-remove"]) == 0
    first = _json_out(capsys)
    assert first["removed"] is False
    assert Path(installed_path).is_file()

    assert zl.main(["autostart-remove", "--yes"]) == 0
    second = _json_out(capsys)
    assert second["removed"] is True
    assert not Path(installed_path).exists()


@pytest.mark.parametrize(
    "action, api_ready, expected_code",
    [
        ("already_running", True, 0),
        ("started", True, 0),
        ("not_installed", False, 1),
        ("timeout", False, 1),
    ],
)
def test_main_start_exit_codes(monkeypatch, capsys, action, api_ready, expected_code):
    seen = {}

    def fake_start(timeout_s=60.0, **kwargs):
        seen["timeout_s"] = timeout_s
        return {"action": action, "exe": None, "api_ready": api_ready}

    monkeypatch.setattr(zl, "start_zotero", fake_start)
    assert zl.main(["start", "--timeout", "12.5"]) == expected_code
    assert seen["timeout_s"] == 12.5
    assert _json_out(capsys) == {"action": action, "exe": None, "api_ready": api_ready}


def test_main_start_defaults_to_sixty_second_timeout(monkeypatch, capsys):
    seen = {}

    def fake_start(timeout_s=None, **kwargs):
        seen["timeout_s"] = timeout_s
        return {"action": "started", "exe": None, "api_ready": True}

    monkeypatch.setattr(zl, "start_zotero", fake_start)
    assert zl.main(["start"]) == 0
    assert seen["timeout_s"] == 60.0


def test_main_start_reports_launch_failure_as_error(monkeypatch, capsys):
    def boom(**kwargs):
        raise PermissionError("access denied")

    monkeypatch.setattr(zl, "start_zotero", boom)
    assert zl.main(["start"]) == 1
    out = _json_out(capsys)
    assert out["action"] == "error"
    assert "access denied" in out["error"]


def test_main_without_subcommand_is_a_usage_error(capsys):
    with pytest.raises(SystemExit) as exc:
        zl.main([])
    assert exc.value.code == 2


def test_main_unknown_subcommand_is_a_usage_error(capsys):
    with pytest.raises(SystemExit) as exc:
        zl.main(["bogus"])
    assert exc.value.code == 2


def test_module_entrypoint_exits_with_main_result(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["zotero_launcher", "autostart-plan"])
    monkeypatch.delitem(sys.modules, "meridian.zotero_launcher", raising=False)
    with pytest.raises(SystemExit) as exc:
        runpy.run_module("meridian.zotero_launcher", run_name="__main__")
    assert exc.value.code == 0
    assert "content" in json.loads(capsys.readouterr().out)


# ---------------------------------------------------------------------------
# start_zotero -- Zotero bridge plugin install before launch
# ---------------------------------------------------------------------------

_XPI_NAME = "meridian-zotero@usemeridian.us.xpi"


def _zotero_profile(tmp_path, monkeypatch, *, running=False):
    """A fake Zotero data root with one default profile, reachable only through env."""
    data_root = tmp_path / "ZoteroRoot"
    profile = data_root / "Profiles" / "abc.default"
    profile.mkdir(parents=True)
    (data_root / "profiles.ini").write_text(
        "[Profile0]\nName=default\nIsRelative=1\nPath=Profiles/abc.default\nDefault=1\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MERIDIAN_ZOTERO_DATA_DIR", str(data_root))
    monkeypatch.setattr(zpi, "zotero_is_running", lambda runner=None: running)
    return profile


def _start_recording_xpi_state(monkeypatch, tmp_path, *, api_answers=(False, True)):
    """Run start_zotero and record whether the plugin file exists at the moment of launch."""
    exe = tmp_path / "zotero.exe"
    _scripted_alive(monkeypatch, list(api_answers))
    _fixed_find(monkeypatch, exe)
    seen_at_launch = []

    def _launcher(path):
        seen_at_launch.append(path)

    result = zl.start_zotero(
        timeout_s=10,
        poll_s=1,
        launcher=_launcher,
        sleep=lambda seconds: None,
        clock=lambda: 0.0,
    )
    return result, exe, seen_at_launch


def test_start_installs_the_plugin_before_launching_when_enabled(monkeypatch, tmp_path):
    profile = _zotero_profile(tmp_path, monkeypatch)
    monkeypatch.setenv("MERIDIAN_ZOTERO_PLUGIN_AUTOINSTALL", "1")
    target = profile / "extensions" / _XPI_NAME
    installed_at_launch = []
    exe = tmp_path / "zotero.exe"
    _scripted_alive(monkeypatch, [False, True])
    _fixed_find(monkeypatch, exe)

    result = zl.start_zotero(
        timeout_s=10,
        poll_s=1,
        launcher=lambda path: installed_at_launch.append(target.is_file()),
        sleep=lambda seconds: None,
        clock=lambda: 0.0,
    )

    assert result == {"action": "started", "exe": str(exe), "api_ready": True}
    assert installed_at_launch == [True]


def test_start_skips_the_plugin_when_autoinstall_is_disabled_by_env(monkeypatch, tmp_path):
    profile = _zotero_profile(tmp_path, monkeypatch)
    monkeypatch.setenv("MERIDIAN_ZOTERO_PLUGIN_AUTOINSTALL", "0")
    result, _exe, launched = _start_recording_xpi_state(monkeypatch, tmp_path)
    assert result["action"] == "started"
    assert len(launched) == 1
    assert not (profile / "extensions" / _XPI_NAME).exists()


def test_start_respects_the_saved_opt_out_when_there_is_no_env_override(monkeypatch, tmp_path):
    profile = _zotero_profile(tmp_path, monkeypatch)
    monkeypatch.delenv("MERIDIAN_ZOTERO_PLUGIN_AUTOINSTALL", raising=False)
    zpi.save_settings(zpi.settings_path(), {"autoinstall": False, "decided": True})
    result, _exe, launched = _start_recording_xpi_state(monkeypatch, tmp_path)
    assert result["action"] == "started"
    assert len(launched) == 1
    assert not (profile / "extensions" / _XPI_NAME).exists()


def test_plugin_install_exception_never_blocks_the_launch(monkeypatch, tmp_path, caplog):
    _zotero_profile(tmp_path, monkeypatch)
    monkeypatch.setenv("MERIDIAN_ZOTERO_PLUGIN_AUTOINSTALL", "1")

    def _explode(*args, **kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(zpi, "install_into_profile", _explode)
    with caplog.at_level("WARNING"):
        result, exe, launched = _start_recording_xpi_state(monkeypatch, tmp_path)
    assert result["action"] == "started"
    assert launched == [exe]
    assert "disk on fire" in caplog.text


def test_plugin_build_exception_never_blocks_the_launch(monkeypatch, tmp_path):
    _zotero_profile(tmp_path, monkeypatch)
    monkeypatch.setenv("MERIDIAN_ZOTERO_PLUGIN_AUTOINSTALL", "1")

    def _missing_source(*args, **kwargs):
        raise FileNotFoundError("extensions/zotero-bridge is not bundled")

    monkeypatch.setattr(zpi, "build_xpi", _missing_source)
    result, exe, launched = _start_recording_xpi_state(monkeypatch, tmp_path)
    assert result["action"] == "started"
    assert launched == [exe]


def test_plugin_is_never_installed_while_zotero_is_running(monkeypatch, tmp_path):
    profile = _zotero_profile(tmp_path, monkeypatch, running=True)
    monkeypatch.setenv("MERIDIAN_ZOTERO_PLUGIN_AUTOINSTALL", "1")
    result, exe, launched = _start_recording_xpi_state(monkeypatch, tmp_path)
    assert result["action"] == "started"
    assert launched == [exe]
    assert not (profile / "extensions" / _XPI_NAME).exists()
