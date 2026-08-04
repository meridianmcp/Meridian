"""3c4ed79d -- portable owned-process lifecycle backends.

Covers:
1. ``OwnedProcessHandle`` -- to_dict/from_dict round trip.
2. ``verify_handle_live`` -- PID-reuse guard, psutil injected/unavailable.
3. ``PosixProcessGroupBackend`` -- spawn (start_new_session), close
   (graceful SIGTERM -> forced SIGKILL escalation, idempotence, PID-reuse
   guard) -- all OS calls mocked, never touches real processes.
4. ``enable_child_subreaper`` -- injectable libc, never touches the real
   prctl syscall.
5. ``Win32JobAPI`` / ``WindowsJobObjectBackend`` -- fake kernel32 double
   (never touches real ``ctypes.WinDLL``, which doesn't exist off Windows),
   including the no-breakaway limit-flags assertion.
6. ``get_default_backend`` -- platform selection.
"""
from __future__ import annotations

import sys
import types

import pytest

from meridian import process_lifecycle as pl


# ---------------------------------------------------------------------------
# 1. OwnedProcessHandle -- to_dict / from_dict round trip
# ---------------------------------------------------------------------------


def test_owned_process_handle_to_dict_from_dict_round_trip():
    handle = pl.OwnedProcessHandle(
        run_id="abc123",
        pid=42,
        executable="node",
        cwd="/repo",
        cmdline=["node", "server.js"],
        create_time=100.5,
        group_id=42,
        job_id=None,
        closed=False,
    )
    data = handle.to_dict()
    assert "popen" not in data
    restored = pl.OwnedProcessHandle.from_dict(data)
    assert restored.run_id == "abc123"
    assert restored.pid == 42
    assert restored.executable == "node"
    assert restored.cwd == "/repo"
    assert restored.cmdline == ["node", "server.js"]
    assert restored.create_time == 100.5
    assert restored.group_id == 42
    assert restored.job_id is None
    assert restored.closed is False
    assert restored.popen is None


def test_owned_process_handle_from_dict_defaults_missing_fields():
    restored = pl.OwnedProcessHandle.from_dict({"run_id": "x", "pid": 1})
    assert restored.executable == ""
    assert restored.cwd is None
    assert restored.cmdline == []
    assert restored.create_time is None
    assert restored.closed is False


def test_new_run_id_unique():
    assert pl.new_run_id() != pl.new_run_id()


# ---------------------------------------------------------------------------
# 2. verify_handle_live -- PID-reuse guard
# ---------------------------------------------------------------------------


def _handle(pid=1, create_time=None):
    return pl.OwnedProcessHandle(
        run_id="r", pid=pid, executable="x", cwd=None, cmdline=["x"], create_time=create_time,
    )


def test_verify_handle_live_no_create_time_defaults_true():
    assert pl.verify_handle_live(_handle(create_time=None)) is True


def test_verify_handle_live_psutil_unavailable_defaults_true(monkeypatch):
    monkeypatch.setitem(sys.modules, "psutil", None)
    assert pl.verify_handle_live(_handle(create_time=10.0)) is True


def test_verify_handle_live_matching_create_time(monkeypatch):
    fake_psutil = types.ModuleType("psutil")

    class _P:
        def __init__(self, pid):
            self.pid = pid

        def create_time(self):
            return 100.0

    fake_psutil.Process = _P
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)
    assert pl.verify_handle_live(_handle(create_time=100.05)) is True  # within 1s tolerance


def test_verify_handle_live_mismatched_create_time(monkeypatch):
    fake_psutil = types.ModuleType("psutil")

    class _P:
        def __init__(self, pid):
            self.pid = pid

        def create_time(self):
            return 999.0

    fake_psutil.Process = _P
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)
    assert pl.verify_handle_live(_handle(create_time=100.0)) is False


def test_verify_handle_live_process_gone(monkeypatch):
    fake_psutil = types.ModuleType("psutil")

    class _P:
        def __init__(self, pid):
            raise RuntimeError("no such process")

    fake_psutil.Process = _P
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)
    assert pl.verify_handle_live(_handle(create_time=100.0)) is False


# ---------------------------------------------------------------------------
# 3. PosixProcessGroupBackend
# ---------------------------------------------------------------------------


class _FakeProc:
    def __init__(self, pid):
        self.pid = pid
        self.wait_calls = 0

    def wait(self, timeout=None):
        self.wait_calls += 1
        return 0


def test_posix_backend_spawn_uses_new_session(monkeypatch):
    captured = {}

    def fake_popen(cmd, env=None, cwd=None, **kwargs):
        captured["cmd"] = cmd
        captured["cwd"] = cwd
        captured["kwargs"] = kwargs
        return _FakeProc(4242)

    monkeypatch.setattr(pl.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(pl, "_safe_create_time", lambda pid: 1.5)

    backend = pl.PosixProcessGroupBackend()
    handle = backend.spawn(["echo", "hi"], cwd="/tmp")

    assert captured["kwargs"].get("start_new_session") is True
    assert handle.pid == 4242
    assert handle.group_id == 4242
    assert handle.cmdline == ["echo", "hi"]
    assert handle.cwd == "/tmp"
    assert handle.create_time == 1.5
    assert isinstance(handle.popen, _FakeProc)


def test_posix_backend_adopt_does_not_repopen(monkeypatch):
    proc = _FakeProc(77)
    backend = pl.PosixProcessGroupBackend()
    handle = backend.adopt(proc, cmd=["node", "x.js"], cwd="/repo")
    assert handle.pid == 77
    assert handle.group_id == 77
    assert handle.popen is proc


def test_posix_backend_close_idempotent(monkeypatch):
    calls = []
    # Patched on the pl module itself (not os.killpg) -- os.killpg/signal.SIGKILL
    # don't exist at all on Windows, so pl resolves them once at import time
    # via getattr(..., fallback) into pl._killpg/_SIGTERM/_SIGKILL. Patching
    # those module-level names keeps this test runnable on a Windows dev box.
    monkeypatch.setattr(pl, "_killpg", lambda pgid, sig: calls.append((pgid, sig)))
    backend = pl.PosixProcessGroupBackend()
    handle = _handle(pid=9)
    handle.group_id = 9
    handle.closed = True
    ok = backend.close(handle)
    assert ok is True
    assert calls == []  # never signals an already-closed handle


def test_posix_backend_close_skips_when_pid_reused(monkeypatch):
    calls = []
    # Patched on the pl module itself (not os.killpg) -- os.killpg/signal.SIGKILL
    # don't exist at all on Windows, so pl resolves them once at import time
    # via getattr(..., fallback) into pl._killpg/_SIGTERM/_SIGKILL. Patching
    # those module-level names keeps this test runnable on a Windows dev box.
    monkeypatch.setattr(pl, "_killpg", lambda pgid, sig: calls.append((pgid, sig)))
    monkeypatch.setattr(pl, "verify_handle_live", lambda handle: False)
    backend = pl.PosixProcessGroupBackend()
    handle = _handle(pid=11, create_time=123.0)
    handle.group_id = 11
    ok = backend.close(handle)
    assert ok is True
    assert handle.closed is True
    assert calls == []


def test_posix_backend_close_graceful_sigterm_only(monkeypatch):
    calls = []
    # Patched on the pl module itself (not os.killpg) -- os.killpg/signal.SIGKILL
    # don't exist at all on Windows, so pl resolves them once at import time
    # via getattr(..., fallback) into pl._killpg/_SIGTERM/_SIGKILL. Patching
    # those module-level names keeps this test runnable on a Windows dev box.
    monkeypatch.setattr(pl, "_killpg", lambda pgid, sig: calls.append((pgid, sig)))
    backend = pl.PosixProcessGroupBackend()
    proc = _FakeProc(21)  # wait() succeeds immediately -- clean SIGTERM exit
    handle = pl.OwnedProcessHandle(
        run_id="r", pid=21, executable="x", cwd=None, cmdline=["x"], group_id=21, popen=proc,
    )
    ok = backend.close(handle, grace_seconds=1.0)
    assert ok is True
    assert handle.closed is True
    assert calls == [(21, pl._SIGTERM)]  # no escalation needed


def test_posix_backend_close_escalates_to_sigkill(monkeypatch):
    calls = []
    # Patched on the pl module itself (not os.killpg) -- os.killpg/signal.SIGKILL
    # don't exist at all on Windows, so pl resolves them once at import time
    # via getattr(..., fallback) into pl._killpg/_SIGTERM/_SIGKILL. Patching
    # those module-level names keeps this test runnable on a Windows dev box.
    monkeypatch.setattr(pl, "_killpg", lambda pgid, sig: calls.append((pgid, sig)))
    # First _group_alive check (after the SIGTERM grace window) reports
    # still-alive -> escalate. Second (after SIGKILL) reports dead.
    counter = {"n": 0}

    def fake_alive(pgid):
        counter["n"] += 1
        return counter["n"] == 1

    monkeypatch.setattr(pl.PosixProcessGroupBackend, "_group_alive", staticmethod(fake_alive))
    backend = pl.PosixProcessGroupBackend()
    # No popen attached -> goes through the deadline-poll path; grace_seconds=0
    # makes the while loop's condition false immediately (deterministic, no
    # real sleep needed) so _group_alive is called exactly once per phase.
    handle = pl.OwnedProcessHandle(
        run_id="r", pid=7, executable="x", cwd=None, cmdline=["x"], group_id=7, popen=None,
    )
    ok = backend.close(handle, grace_seconds=0)
    assert ok is True
    assert handle.closed is True
    assert calls == [(7, pl._SIGTERM), (7, pl._SIGKILL)]


def test_posix_backend_close_process_lookup_error_is_success(monkeypatch):
    def raise_lookup(pgid, sig):
        raise ProcessLookupError("gone")

    monkeypatch.setattr(pl, "_killpg", raise_lookup)
    backend = pl.PosixProcessGroupBackend()
    handle = pl.OwnedProcessHandle(
        run_id="r", pid=13, executable="x", cwd=None, cmdline=["x"], group_id=13, popen=None,
    )
    ok = backend.close(handle)
    assert ok is True
    assert handle.closed is True


# ---------------------------------------------------------------------------
# 4. enable_child_subreaper -- injectable libc, never touches real prctl
# ---------------------------------------------------------------------------


def test_enable_child_subreaper_noop_on_non_linux(monkeypatch):
    monkeypatch.setattr(pl.sys, "platform", "win32")
    assert pl.enable_child_subreaper() is False


def test_enable_child_subreaper_calls_prctl_via_injected_libc(monkeypatch):
    monkeypatch.setattr(pl.sys, "platform", "linux")
    calls = []

    class _FakeLibc:
        def prctl(self, *args):
            calls.append(args)
            return 0

    ok = pl.enable_child_subreaper(libc_loader=lambda: _FakeLibc())
    assert ok is True
    assert calls == [(36, 1, 0, 0, 0)]


def test_enable_child_subreaper_degrades_on_loader_failure(monkeypatch):
    monkeypatch.setattr(pl.sys, "platform", "linux")

    def bad_loader():
        raise RuntimeError("no libc")

    assert pl.enable_child_subreaper(libc_loader=bad_loader) is False


def test_enable_child_subreaper_false_on_nonzero_prctl_result(monkeypatch):
    monkeypatch.setattr(pl.sys, "platform", "linux")

    class _FailingLibc:
        def prctl(self, *args):
            return -1

    assert pl.enable_child_subreaper(libc_loader=lambda: _FailingLibc()) is False


# ---------------------------------------------------------------------------
# 5. Win32JobAPI / WindowsJobObjectBackend -- fake kernel32, no real ctypes.WinDLL
# ---------------------------------------------------------------------------


class _FakeKernel32:
    """Records every call; CreateJobObjectW/OpenProcess hand back
    incrementing fake handles so tests can assert wiring without any real
    Windows API."""

    def __init__(self):
        self.calls = []
        self._next_handle = 1000

    def _handle(self):
        self._next_handle += 1
        return self._next_handle

    def CreateJobObjectW(self, sec, name):
        h = self._handle()
        self.calls.append(("CreateJobObjectW", h))
        return h

    def SetInformationJobObject(self, job_handle, info_class, info_ptr, info_size):
        info = ctypes_cast_extended_limit(info_ptr)
        self.calls.append(("SetInformationJobObject", job_handle, info.BasicLimitInformation.LimitFlags))
        return 1

    def OpenProcess(self, access, inherit, pid):
        h = self._handle()
        self.calls.append(("OpenProcess", access, pid, h))
        return h

    def AssignProcessToJobObject(self, job_handle, process_handle):
        self.calls.append(("AssignProcessToJobObject", job_handle, process_handle))
        return 1

    def TerminateJobObject(self, job_handle, exit_code):
        self.calls.append(("TerminateJobObject", job_handle, exit_code))
        return 1

    def CloseHandle(self, handle):
        self.calls.append(("CloseHandle", handle))
        return 1


def ctypes_cast_extended_limit(info_ptr):
    import ctypes

    return ctypes.cast(
        info_ptr, ctypes.POINTER(pl._JOBOBJECT_EXTENDED_LIMIT_INFORMATION)
    ).contents


def test_win32_job_api_sets_only_kill_on_close_flag_no_breakaway():
    kernel32 = _FakeKernel32()
    api = pl.Win32JobAPI(kernel32)
    job = api.create_job()
    ok = api.set_kill_on_close(job)
    assert ok is True
    set_call = next(c for c in kernel32.calls if c[0] == "SetInformationJobObject")
    limit_flags = set_call[2]
    assert limit_flags == pl._JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    assert limit_flags & pl._JOB_OBJECT_LIMIT_BREAKAWAY_OK == 0
    assert limit_flags & pl._JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK == 0


def test_win32_job_api_open_process_and_assign():
    kernel32 = _FakeKernel32()
    api = pl.Win32JobAPI(kernel32)
    job = api.create_job()
    proc_handle = api.open_process(555)
    assert api.assign_process(job, proc_handle) is True
    assert ("AssignProcessToJobObject", job, proc_handle) in kernel32.calls


def test_load_win32_job_api_returns_none_off_windows():
    # Real (non-monkeypatched) platform check -- never touches ctypes.WinDLL
    # unless sys.platform is genuinely "win32".
    if sys.platform != "win32":
        assert pl._load_win32_job_api() is None


def test_windows_backend_spawn_assigns_job(monkeypatch):
    def fake_popen(cmd, env=None, cwd=None, **kwargs):
        assert kwargs.get("creationflags") == 0x00000200
        return _FakeProc(555)

    monkeypatch.setattr(pl.subprocess, "Popen", fake_popen)
    fake_api = _FakeKernel32()
    backend = pl.WindowsJobObjectBackend(api_loader=lambda: pl.Win32JobAPI(fake_api))

    handle = backend.spawn(["node", "server.js"])

    assert handle.pid == 555
    assert handle.job_id is not None
    kinds = [c[0] for c in fake_api.calls]
    assert kinds == [
        "CreateJobObjectW", "SetInformationJobObject", "OpenProcess", "AssignProcessToJobObject",
    ]


def test_windows_backend_spawn_degrades_without_api(monkeypatch):
    monkeypatch.setattr(pl.subprocess, "Popen", lambda cmd, env=None, cwd=None, **kw: _FakeProc(777))
    backend = pl.WindowsJobObjectBackend(api_loader=lambda: None)
    handle = backend.spawn(["node"])
    assert handle.job_id is None  # degraded, taskkill-only teardown
    assert handle.pid == 777


def test_windows_backend_adopt_assigns_existing_process(monkeypatch):
    fake_api = _FakeKernel32()
    backend = pl.WindowsJobObjectBackend(api_loader=lambda: pl.Win32JobAPI(fake_api))
    proc = _FakeProc(321)
    handle = backend.adopt(proc, cmd=["node", "x.js"])
    assert handle.pid == 321
    assert handle.job_id is not None


def test_windows_backend_close_terminates_job_and_taskkills(monkeypatch):
    run_calls = []
    monkeypatch.setattr(pl.subprocess, "run", lambda argv, **kw: run_calls.append(argv))
    fake_api = _FakeKernel32()
    backend = pl.WindowsJobObjectBackend(api_loader=lambda: pl.Win32JobAPI(fake_api))
    handle = pl.OwnedProcessHandle(
        run_id="r", pid=555, executable="node", cwd=None, cmdline=["node"], job_id=100,
    )
    ok = backend.close(handle)
    assert ok is True
    assert handle.closed is True
    assert ("TerminateJobObject", 100, 1) in fake_api.calls
    assert run_calls == [["taskkill", "/F", "/T", "/PID", "555"]]


def test_windows_backend_close_idempotent(monkeypatch):
    run_calls = []
    monkeypatch.setattr(pl.subprocess, "run", lambda argv, **kw: run_calls.append(argv))
    backend = pl.WindowsJobObjectBackend(api_loader=lambda: None)
    handle = pl.OwnedProcessHandle(
        run_id="r", pid=1, executable="x", cwd=None, cmdline=["x"], closed=True,
    )
    ok = backend.close(handle)
    assert ok is True
    assert run_calls == []


def test_windows_backend_close_without_job_still_taskkills(monkeypatch):
    run_calls = []
    monkeypatch.setattr(pl.subprocess, "run", lambda argv, **kw: run_calls.append(argv))
    backend = pl.WindowsJobObjectBackend(api_loader=lambda: None)
    handle = pl.OwnedProcessHandle(
        run_id="r", pid=42, executable="x", cwd=None, cmdline=["x"],
    )
    ok = backend.close(handle)
    assert ok is True
    assert run_calls == [["taskkill", "/F", "/T", "/PID", "42"]]


def test_windows_backend_close_skips_when_pid_reused(monkeypatch):
    run_calls = []
    monkeypatch.setattr(pl.subprocess, "run", lambda argv, **kw: run_calls.append(argv))
    monkeypatch.setattr(pl, "verify_handle_live", lambda handle: False)
    backend = pl.WindowsJobObjectBackend(api_loader=lambda: None)
    handle = pl.OwnedProcessHandle(
        run_id="r", pid=1, executable="x", cwd=None, cmdline=["x"], create_time=1.0,
    )
    ok = backend.close(handle)
    assert ok is True
    assert handle.closed is True
    assert run_calls == []  # skipped entirely -- PID may have been reused


def test_assign_to_job_failure_leaves_job_id_none(monkeypatch):
    class _FailingAPI:
        def create_job(self):
            return 42

        def set_kill_on_close(self, job_handle):
            return False  # simulate SetInformationJobObject failing

        def close_handle(self, handle):
            return True

    monkeypatch.setattr(pl.subprocess, "Popen", lambda cmd, env=None, cwd=None, **kw: _FakeProc(9))
    backend = pl.WindowsJobObjectBackend(api_loader=lambda: _FailingAPI())
    handle = backend.spawn(["node"])
    assert handle.job_id is None


# ---------------------------------------------------------------------------
# 6. get_default_backend -- platform selection
# ---------------------------------------------------------------------------


def test_get_default_backend_windows(monkeypatch):
    monkeypatch.setattr(pl.sys, "platform", "win32")
    backend = pl.get_default_backend()
    assert isinstance(backend, pl.WindowsJobObjectBackend)


def test_get_default_backend_posix(monkeypatch):
    monkeypatch.setattr(pl.sys, "platform", "linux")
    backend = pl.get_default_backend()
    assert isinstance(backend, pl.PosixProcessGroupBackend)
