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
6. ``Win32ConsoleAPI`` / graceful CTRL_BREAK shutdown (2026-09-28 review
   finding #5/#22) -- fake kernel32/user32 doubles for the opt-in
   ensure_console_for_graceful_shutdown path, plus one real, non-mocked,
   Windows-only end-to-end integration test proving the full pipeline
   (AllocConsole + console inheritance + GenerateConsoleCtrlEvent + the
   target's own SetConsoleCtrlHandler) genuinely delivers CTRL_BREAK.
7. ``get_default_backend`` -- platform selection + console-flag passthrough.
"""
from __future__ import annotations

import sys
import time
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


@pytest.mark.skipif(
    sys.platform not in ("linux", "linux2"),
    reason="zombie-reap semantics require a real POSIX process table (/proc)",
)
def test_posix_backend_close_without_popen_reaps_real_child_no_zombie_left():
    """Deliberate exception to this file's own 'all OS calls mocked' rule
    (module docstring, section 3): zombie-reaping cannot be verified with a
    mock, only with a real process. Regression test for a real bug found on
    Linux CI 2026-09-08: close() on a handle with no ``popen`` attached (the
    exact shape LocalRunner.stop() builds when it is a DIFFERENT Python
    object than the one that originally spawned the child -- e.g. a fresh
    LocalRunner reconstructed from an on-disk record in a later CLI
    invocation) went through the signal-then-poll path in
    ``_signal_and_wait`` without ever calling ``Popen.wait()``/``os.waitpid``.
    A signalled child nobody waits on becomes a zombie -- still fully
    present for ``kill(pid, 0)`` / ``psutil.pid_exists()`` (POSIX keeps a
    zombie's PID entry until reaped) -- so ``close()`` kept reporting the
    process as alive (or, worse, silently leaked a zombie once the poll
    window elapsed) instead of confirming a clean stop.
    """
    import os
    import subprocess

    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
    )
    pid = proc.pid
    try:
        handle = pl.OwnedProcessHandle(
            run_id="r", pid=pid, executable=sys.executable, cwd=None,
            cmdline=[sys.executable], group_id=pid, popen=None,
        )
        backend = pl.PosixProcessGroupBackend()
        ok = backend.close(handle, grace_seconds=3.0)
        assert ok is True
        # Fully reaped, not merely signalled-but-zombied: /proc/<pid> must
        # be gone entirely (a zombie's /proc/<pid> entry persists, with
        # State: Z, until its parent reaps it).
        assert not os.path.exists(f"/proc/{pid}")
    finally:
        # Best-effort cleanup in case the assertion above ever fails again --
        # never leak a zombie into the rest of the test session.
        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            pass


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
        self._jobs_by_name = {}

    def _handle(self):
        self._next_handle += 1
        return self._next_handle

    def CreateJobObjectW(self, sec, name):
        h = self._handle()
        self.calls.append(("CreateJobObjectW", h, name))
        if name:
            self._jobs_by_name[name] = h
        return h

    def OpenJobObjectW(self, access, inherit, name):
        h = self._jobs_by_name.get(name)
        self.calls.append(("OpenJobObjectW", access, name, h))
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
    # CloseHandle on the OpenProcess handle is expected even on success --
    # it's only needed transiently to make the Assign call, never for the
    # job's continued lifetime (2026-09-28 handle-leak fix).
    assert kinds == [
        "CreateJobObjectW", "SetInformationJobObject", "OpenProcess", "AssignProcessToJobObject",
        "CloseHandle",
    ]
    proc_handle_closed = next(c for c in fake_api.calls if c[0] == "CloseHandle")
    open_process_call = next(c for c in fake_api.calls if c[0] == "OpenProcess")
    assert proc_handle_closed[1] == open_process_call[3]  # closed exactly the proc handle, not the job
def test_windows_backend_spawn_skips_kill_on_close_when_disabled(monkeypatch):
    """d397bb71: kill_on_job_close=False must never call
    SetInformationJobObject at all, but the job is still created and the
    child still assigned to it -- explicit TerminateJobObject-based teardown
    later is unaffected either way (see WindowsJobObjectBackend's own
    docstring)."""
    monkeypatch.setattr(pl.subprocess, "Popen", lambda cmd, env=None, cwd=None, **kw: _FakeProc(555))
    fake_api = _FakeKernel32()
    backend = pl.WindowsJobObjectBackend(
        api_loader=lambda: pl.Win32JobAPI(fake_api), kill_on_job_close=False,
    )

    handle = backend.spawn(["node", "server.js"])

    assert handle.job_id is not None
    assert handle.job_name is not None
    kinds = [c[0] for c in fake_api.calls]
    assert "SetInformationJobObject" not in kinds
    assert kinds == [
        "CreateJobObjectW", "OpenProcess", "AssignProcessToJobObject", "CloseHandle",
    ]


def test_windows_backend_spawn_sets_kill_on_close_by_default(monkeypatch):
    """The default (no kill_on_job_close passed) must be unchanged from the
    pre-d397bb71 behavior -- SetInformationJobObject still runs."""
    monkeypatch.setattr(pl.subprocess, "Popen", lambda cmd, env=None, cwd=None, **kw: _FakeProc(556))
    fake_api = _FakeKernel32()
    backend = pl.WindowsJobObjectBackend(api_loader=lambda: pl.Win32JobAPI(fake_api))

    handle = backend.spawn(["node", "server.js"])

    assert handle.job_id is not None
    kinds = [c[0] for c in fake_api.calls]
    assert "SetInformationJobObject" in kinds


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
    # Also doubles as the regression case for the create_time=None-but-job-
    # terminated branch of the 2026-09-28 PID-reuse fix: this handle never
    # captures create_time, yet taskkill still fires because job_terminated
    # is True -- the job's own AssignProcessToJobObject already bound to
    # this exact pid at spawn time, which counts as identity confirmation.
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


def test_windows_backend_close_skips_taskkill_when_identity_and_job_both_unverifiable(monkeypatch):
    """Regression test for the 2026-09-28 PID-reuse fix: when create_time
    was never captured (fast-crash race in adopt()) AND no Job Object was
    ever assigned (api unavailable, or OpenProcess/AssignProcessToJobObject
    failed on the already-exited child), close() has zero identity
    confirmation left -- it must NOT taskkill by bare PID, since Windows
    may have since recycled that PID for an unrelated process. This
    replaces the old test of the same shape, which asserted the unsafe
    behavior this fix removes."""
    run_calls = []
    monkeypatch.setattr(pl.subprocess, "run", lambda argv, **kw: run_calls.append(argv))
    backend = pl.WindowsJobObjectBackend(api_loader=lambda: None)
    handle = pl.OwnedProcessHandle(
        run_id="r", pid=42, executable="x", cwd=None, cmdline=["x"],
    )
    ok = backend.close(handle)
    assert ok is True
    assert handle.closed is True
    assert run_calls == []  # unverifiable identity, no job -- refuse the destructive path


def test_windows_backend_close_taskkills_when_create_time_verified_without_job(monkeypatch):
    """The other half of the gate: a captured create_time is itself enough
    confirmation to fall back to taskkill, even with no job assigned. Uses
    a fake psutil (matching test_verify_handle_live_matching_create_time's
    pattern) so verify_handle_live does a REAL matching comparison rather
    than hitting its own no-psutil/mismatch defaults, which would either
    mask or short-circuit the branch this test targets."""
    fake_psutil = types.ModuleType("psutil")

    class _P:
        def __init__(self, pid):
            self.pid = pid

        def create_time(self):
            return 100.0

    fake_psutil.Process = _P
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)
    run_calls = []
    monkeypatch.setattr(pl.subprocess, "run", lambda argv, **kw: run_calls.append(argv))
    backend = pl.WindowsJobObjectBackend(api_loader=lambda: None)
    handle = pl.OwnedProcessHandle(
        run_id="r", pid=42, executable="x", cwd=None, cmdline=["x"], create_time=100.0,
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


def test_job_object_name_is_deterministic_per_run_id():
    assert pl._job_object_name("abc123") == pl._job_object_name("abc123")
    assert pl._job_object_name("abc123") != pl._job_object_name("xyz789")
    assert pl._job_object_name("abc123").startswith("Local\\")


def test_windows_backend_spawn_names_the_job_from_run_id(monkeypatch):
    """2026-09-28 review finding #12: every NEW job is created with a
    deterministic, run_id-derived NAME (not anonymous) so a LATER, DIFFERENT
    process can reopen it."""
    def fake_popen(cmd, env=None, cwd=None, **kwargs):
        return _FakeProc(555)

    monkeypatch.setattr(pl.subprocess, "Popen", fake_popen)
    fake_api = _FakeKernel32()
    backend = pl.WindowsJobObjectBackend(api_loader=lambda: pl.Win32JobAPI(fake_api))

    handle = backend.spawn(["node", "server.js"])

    assert handle.job_name == pl._job_object_name(handle.run_id)
    create_call = next(c for c in fake_api.calls if c[0] == "CreateJobObjectW")
    assert create_call[2] == handle.job_name


def test_assign_to_job_failure_leaves_job_name_none(monkeypatch):
    class _FailingAPI:
        def create_job(self, name=None):
            return 42

        def set_kill_on_close(self, job_handle):
            return False

        def close_handle(self, handle):
            return True

    monkeypatch.setattr(pl.subprocess, "Popen", lambda cmd, env=None, cwd=None, **kw: _FakeProc(9))
    backend = pl.WindowsJobObjectBackend(api_loader=lambda: _FailingAPI())
    handle = backend.spawn(["node"])
    assert handle.job_id is None
    assert handle.job_name is None


def _fake_psutil_matching(create_time_value):
    """Fake psutil module whose Process(pid).create_time() always matches
    *create_time_value* -- makes verify_handle_live() do a REAL matching
    comparison (True) instead of hitting a genuine (and, on this dev
    machine, failing) lookup against a fabricated PID that doesn't
    correspond to a real running process. Mirrors
    test_verify_handle_live_matching_create_time's own pattern."""
    fake_psutil = types.ModuleType("psutil")

    class _P:
        def __init__(self, pid):
            self.pid = pid

        def create_time(self):
            return create_time_value

    fake_psutil.Process = _P
    return fake_psutil


def test_close_reopens_job_by_name_for_a_cross_process_handle(monkeypatch):
    """The core fix: a handle reconstructed from a PERSISTED record (no
    live popen -- exactly RunnerRecord.as_owned_handle()'s shape from a
    standalone `python -m meridian.local_runner stop` invocation in a
    DIFFERENT process than the one that spawned the child) must reopen the
    job by NAME rather than trusting the stale job_id HANDLE INT, which
    means nothing (and could even collide with an unrelated handle) outside
    the ORIGINAL process's own handle table."""
    monkeypatch.setitem(sys.modules, "psutil", _fake_psutil_matching(100.0))
    run_calls = []
    monkeypatch.setattr(pl.subprocess, "run", lambda argv, **kw: run_calls.append(argv))
    fake_api = _FakeKernel32()
    # Simulate the job having been created (with a name) by a DIFFERENT,
    # now-gone process -- fake_api's own _jobs_by_name registry stands in
    # for "the kernel still has this named job object alive".
    fake_api._jobs_by_name["Local\\meridian-job-run-xyz"] = 777
    backend = pl.WindowsJobObjectBackend(api_loader=lambda: pl.Win32JobAPI(fake_api))
    # job_id is a bogus/stale int (e.g. reused by something unrelated in
    # THIS process) -- must never be used directly when job_name is present.
    handle = pl.OwnedProcessHandle(
        run_id="run-xyz", pid=555, executable="node", cwd=None, cmdline=["node"],
        job_id=999999, job_name="Local\\meridian-job-run-xyz", create_time=100.0,
    )
    ok = backend.close(handle)
    assert ok is True
    assert ("OpenJobObjectW", pl._JOB_OBJECT_TERMINATE, "Local\\meridian-job-run-xyz", 777) in fake_api.calls
    assert ("TerminateJobObject", 777, 1) in fake_api.calls
    # The bogus job_id (999999) must NEVER have been passed to TerminateJobObject.
    assert not any(c[0] == "TerminateJobObject" and c[1] == 999999 for c in fake_api.calls)


def test_close_falls_back_to_raw_job_id_for_legacy_handle_with_no_job_name(monkeypatch):
    """A handle with no job_name at all (predates this fix) keeps the exact
    pre-existing behavior -- direct use of the raw job_id value."""
    run_calls = []
    monkeypatch.setattr(pl.subprocess, "run", lambda argv, **kw: run_calls.append(argv))
    fake_api = _FakeKernel32()
    backend = pl.WindowsJobObjectBackend(api_loader=lambda: pl.Win32JobAPI(fake_api))
    handle = pl.OwnedProcessHandle(
        run_id="r", pid=555, executable="node", cwd=None, cmdline=["node"], job_id=100,
    )
    ok = backend.close(handle)
    assert ok is True
    assert ("TerminateJobObject", 100, 1) in fake_api.calls
    assert not any(c[0] == "OpenJobObjectW" for c in fake_api.calls)


def test_close_job_name_reopen_returns_none_when_job_already_gone(monkeypatch):
    """The named job no longer exists (already terminated/closed elsewhere)
    -- close() must not crash, and falls through to the taskkill fallback
    for final cleanup."""
    monkeypatch.setitem(sys.modules, "psutil", _fake_psutil_matching(1.0))
    run_calls = []
    monkeypatch.setattr(pl.subprocess, "run", lambda argv, **kw: run_calls.append(argv))
    fake_api = _FakeKernel32()  # empty _jobs_by_name -- OpenJobObjectW returns None
    backend = pl.WindowsJobObjectBackend(api_loader=lambda: pl.Win32JobAPI(fake_api))
    handle = pl.OwnedProcessHandle(
        run_id="r", pid=42, executable="x", cwd=None, cmdline=["x"],
        job_name="Local\\meridian-job-r", create_time=1.0,
    )
    ok = backend.close(handle)
    assert ok is True
    assert not any(c[0] == "TerminateJobObject" for c in fake_api.calls)
    assert run_calls == [["taskkill", "/F", "/T", "/PID", "42"]]  # create_time confirms identity


def test_owned_process_handle_job_name_round_trips_through_to_dict():
    handle = pl.OwnedProcessHandle(
        run_id="r", pid=1, executable="x", cwd=None, cmdline=["x"], job_name="Local\\meridian-job-r",
    )
    data = handle.to_dict()
    assert data["job_name"] == "Local\\meridian-job-r"
    restored = pl.OwnedProcessHandle.from_dict(data)
    assert restored.job_name == "Local\\meridian-job-r"


def test_assign_to_job_failure_closes_both_job_and_proc_handles(monkeypatch):
    """Regression test for the 2026-09-28 handle-leak fix: when
    AssignProcessToJobObject itself fails (job created, proc_handle
    opened, but assignment fails), both the job handle and the process
    handle must be explicitly closed -- previously NEITHER was closed on
    this path (nor on the success path), leaking one kernel32 HANDLE per
    spawn."""
    fake_api = _FakeKernel32()
    fake_api.AssignProcessToJobObject = lambda job_handle, process_handle: (
        fake_api.calls.append(("AssignProcessToJobObject", job_handle, process_handle)) or 0
    )
    monkeypatch.setattr(pl.subprocess, "Popen", lambda cmd, env=None, cwd=None, **kw: _FakeProc(9))
    backend = pl.WindowsJobObjectBackend(api_loader=lambda: pl.Win32JobAPI(fake_api))
    handle = backend.spawn(["node"])
    assert handle.job_id is None
    close_calls = [c[1] for c in fake_api.calls if c[0] == "CloseHandle"]
    job_handle = next(c[1] for c in fake_api.calls if c[0] == "CreateJobObjectW")
    proc_handle = next(c[3] for c in fake_api.calls if c[0] == "OpenProcess")
    assert job_handle in close_calls
    assert proc_handle in close_calls


# ---------------------------------------------------------------------------
# 6. Graceful CTRL_BREAK shutdown before the forceful path (2026-09-28
#    review finding #5/#22) -- opt-in via ensure_console_for_graceful_shutdown,
#    default False (every pre-existing caller/test above is unaffected).
# ---------------------------------------------------------------------------


class _FakeConsoleKernel32:
    """Fake kernel32 double for pl.Win32ConsoleAPI -- never touches a real
    ctypes.WinDLL. `alloc_result`/`alloc_error` and `ctrl_break_result`
    control AllocConsole/GenerateConsoleCtrlEvent outcomes per test."""

    def __init__(self, *, alloc_result=1, alloc_error=0, ctrl_break_result=1):
        self.calls = []
        self.alloc_result = alloc_result
        self.alloc_error = alloc_error
        self.ctrl_break_result = ctrl_break_result
        self._console_window = 0

    def GetConsoleWindow(self):
        return self._console_window

    def AllocConsole(self):
        self.calls.append("AllocConsole")
        if self.alloc_result:
            self._console_window = 999
        return self.alloc_result

    def GenerateConsoleCtrlEvent(self, ctrl_type, pid):
        self.calls.append(("GenerateConsoleCtrlEvent", ctrl_type, pid))
        return self.ctrl_break_result


class _FakeUser32:
    def __init__(self):
        self.calls = []

    def ShowWindow(self, hwnd, cmd):
        self.calls.append(("ShowWindow", hwnd, cmd))
        return 1


def _fake_console_api(**kwargs):
    kernel32 = _FakeConsoleKernel32(**kwargs)
    api = pl.Win32ConsoleAPI(kernel32, _FakeUser32(), get_last_error=lambda: kernel32.alloc_error)
    return api, kernel32


def test_win32_console_api_alloc_then_hide():
    api, kernel32 = _fake_console_api()
    assert api.get_console_window() == 0
    assert api.alloc_console() is True
    api.hide_console_window()
    assert api.get_console_window() == 999


def test_ensure_console_noop_when_flag_not_set():
    """Default backend (the pre-existing behavior for every OTHER caller,
    e.g. tunnel_client.py) never touches the console API at all."""
    api, kernel32 = _fake_console_api()
    backend = pl.WindowsJobObjectBackend(api_loader=lambda: None, console_api_loader=lambda: api)
    backend._ensure_console()
    assert kernel32.calls == []


def test_ensure_console_calls_alloc_exactly_once_across_multiple_spawns(monkeypatch):
    """Attempted at most once per backend INSTANCE, not once per spawn()."""
    api, kernel32 = _fake_console_api()
    monkeypatch.setattr(pl.subprocess, "Popen", lambda cmd, env=None, cwd=None, **kw: _FakeProc(1))
    backend = pl.WindowsJobObjectBackend(
        api_loader=lambda: None, ensure_console_for_graceful_shutdown=True, console_api_loader=lambda: api,
    )
    backend.spawn(["node"])
    backend.spawn(["node"])
    assert kernel32.calls.count("AllocConsole") == 1


def test_ensure_console_skips_warning_on_access_denied(caplog):
    """ERROR_ACCESS_DENIED means a console already exists -- benign, no
    warning (confirmed empirically: GetConsoleWindow() is unreliable under a
    ConPTY-backed terminal, so AllocConsole's own return code is the
    authority, not a pre-check)."""
    api, kernel32 = _fake_console_api(alloc_result=0, alloc_error=pl._ERROR_ACCESS_DENIED)
    backend = pl.WindowsJobObjectBackend(
        api_loader=lambda: None, ensure_console_for_graceful_shutdown=True, console_api_loader=lambda: api,
    )
    with caplog.at_level("WARNING"):
        backend._ensure_console()
    assert not any("AllocConsole failed" in r.message for r in caplog.records)


def test_ensure_console_warns_on_genuine_failure(caplog):
    api, kernel32 = _fake_console_api(alloc_result=0, alloc_error=1234)
    backend = pl.WindowsJobObjectBackend(
        api_loader=lambda: None, ensure_console_for_graceful_shutdown=True, console_api_loader=lambda: api,
    )
    with caplog.at_level("WARNING"):
        backend._ensure_console()
    assert any("AllocConsole failed" in r.message for r in caplog.records)


def test_close_graceful_ctrl_break_skips_the_forceful_path_entirely(monkeypatch):
    """When CTRL_BREAK is delivered AND the process exits within
    grace_seconds, TerminateJobObject/taskkill must never run at all --
    "escalating to the forceful path only on timeout"."""
    run_calls = []
    monkeypatch.setattr(pl.subprocess, "run", lambda argv, **kw: run_calls.append(argv))
    api, kernel32 = _fake_console_api(ctrl_break_result=1)
    fake_job_api = _FakeKernel32()
    backend = pl.WindowsJobObjectBackend(
        api_loader=lambda: pl.Win32JobAPI(fake_job_api),
        ensure_console_for_graceful_shutdown=True, console_api_loader=lambda: api,
    )
    proc = _FakeProc(42)  # wait() succeeds immediately -- clean CTRL_BREAK exit
    handle = pl.OwnedProcessHandle(
        run_id="r", pid=42, executable="x", cwd=None, cmdline=["x"], job_id=100, popen=proc,
    )
    ok = backend.close(handle, grace_seconds=1.0)
    assert ok is True
    assert handle.closed is True
    assert ("GenerateConsoleCtrlEvent", pl._CTRL_BREAK_EVENT, 42) in kernel32.calls
    assert run_calls == []  # forceful path never ran
    assert not any(c[0] == "TerminateJobObject" for c in fake_job_api.calls)


def test_close_falls_through_to_forceful_when_ctrl_break_send_fails(monkeypatch):
    run_calls = []
    monkeypatch.setattr(pl.subprocess, "run", lambda argv, **kw: run_calls.append(argv))
    api, kernel32 = _fake_console_api(ctrl_break_result=0)  # GenerateConsoleCtrlEvent fails
    fake_job_api = _FakeKernel32()
    backend = pl.WindowsJobObjectBackend(
        api_loader=lambda: pl.Win32JobAPI(fake_job_api),
        ensure_console_for_graceful_shutdown=True, console_api_loader=lambda: api,
    )
    handle = pl.OwnedProcessHandle(
        run_id="r", pid=42, executable="x", cwd=None, cmdline=["x"], job_id=100,
    )
    ok = backend.close(handle)
    assert ok is True
    assert ("TerminateJobObject", 100, 1) in fake_job_api.calls
    assert run_calls == [["taskkill", "/F", "/T", "/PID", "42"]]


def test_close_falls_through_to_forceful_when_ctrl_break_times_out(monkeypatch):
    """CTRL_BREAK is delivered (sent=True) but the process never actually
    exits within grace_seconds -- must still escalate to the forceful path,
    not report a false success."""
    run_calls = []
    monkeypatch.setattr(pl.subprocess, "run", lambda argv, **kw: run_calls.append(argv))
    api, kernel32 = _fake_console_api(ctrl_break_result=1)
    fake_job_api = _FakeKernel32()
    backend = pl.WindowsJobObjectBackend(
        api_loader=lambda: pl.Win32JobAPI(fake_job_api),
        ensure_console_for_graceful_shutdown=True, console_api_loader=lambda: api,
    )

    class _NeverExitsProc(_FakeProc):
        def wait(self, timeout=None):
            raise __import__("subprocess").TimeoutExpired(cmd="x", timeout=timeout)

    handle = pl.OwnedProcessHandle(
        run_id="r", pid=42, executable="x", cwd=None, cmdline=["x"], job_id=100,
        popen=_NeverExitsProc(42),
    )
    ok = backend.close(handle, grace_seconds=0.1)
    assert ok is True
    assert ("TerminateJobObject", 100, 1) in fake_job_api.calls
    assert run_calls == [["taskkill", "/F", "/T", "/PID", "42"]]


def test_close_graceful_path_is_a_noop_when_flag_not_set(monkeypatch):
    """Every EXISTING test in this file constructs WindowsJobObjectBackend
    without ensure_console_for_graceful_shutdown -- this documents (and
    locks in) that default=False means the console API is never even
    loaded, let alone called, from close()."""
    run_calls = []
    monkeypatch.setattr(pl.subprocess, "run", lambda argv, **kw: run_calls.append(argv))
    console_loader_calls = []

    def _tracking_console_loader():
        console_loader_calls.append(1)
        return None

    fake_job_api = _FakeKernel32()
    backend = pl.WindowsJobObjectBackend(
        api_loader=lambda: pl.Win32JobAPI(fake_job_api), console_api_loader=_tracking_console_loader,
    )
    handle = pl.OwnedProcessHandle(
        run_id="r", pid=42, executable="x", cwd=None, cmdline=["x"], job_id=100,
    )
    backend.close(handle)
    assert console_loader_calls == []  # _attempt_graceful_ctrl_break short-circuited before loading


def test_pid_confirmed_gone_uses_psutil_when_available(monkeypatch):
    fake_psutil = types.ModuleType("psutil")
    fake_psutil.pid_exists = lambda pid: pid != 999
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)
    assert pl._pid_confirmed_gone(999) is True
    assert pl._pid_confirmed_gone(1) is False


def test_pid_confirmed_gone_false_when_psutil_unavailable(monkeypatch):
    monkeypatch.setitem(sys.modules, "psutil", None)
    assert pl._pid_confirmed_gone(999) is False  # unverifiable -- never claim "gone"


@pytest.mark.skipif(
    sys.platform != "win32", reason="CTRL_BREAK/console/Job Object mechanics are Windows-only",
)
def test_real_ctrl_break_graceful_shutdown_end_to_end(tmp_path):
    """Deliberate exception to this file's own 'all OS calls mocked' rule
    (see module docstring): a REAL, non-mocked integration test, because the
    2026-09-28 investigation's whole point was that fakes can't prove
    GenerateConsoleCtrlEvent is actually DELIVERABLE from a genuinely
    console-less caller -- only a real Windows process can. Spawns a real
    child via WindowsJobObjectBackend(ensure_console_for_graceful_shutdown=
    True) and verifies the full pipeline -- AllocConsole + the child
    inheriting that console + GenerateConsoleCtrlEvent + the child's own
    SetConsoleCtrlHandler -- genuinely delivers CTRL_BREAK end to end. This
    is the same mechanism manually confirmed during triage (see
    WindowsJobObjectBackend's own docstring) and the reason the real
    production --run-server child (uvicorn, which maps SIGBREAK to its own
    graceful handle_exit on Windows) can shut down cleanly instead of being
    torn out mid-run by TerminateJobObject.
    """
    marker_path = tmp_path / "graceful_marker.txt"
    ready_path = tmp_path / "graceful_ready.txt"
    child_script = tmp_path / "graceful_child.py"
    child_script.write_text(
        "import ctypes, os, time\n"
        "HANDLER = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint32)\n"
        "def handler(ctrl_type):\n"
        f"    open(r'{marker_path}', 'w').write(str(ctrl_type))\n"
        "    os._exit(0)\n"  # a console-ctrl handler runs on its OWN OS
        # thread -- sys.exit() there would only end that thread, not the
        # process; os._exit() is the correct, thread-safe way to actually
        # terminate here, exactly what a real graceful-shutdown handler
        # (e.g. uvicorn's) does after finishing its own cleanup.
        "_h = HANDLER(handler)\n"
        "ctypes.WinDLL('kernel32', use_last_error=True).SetConsoleCtrlHandler(_h, True)\n"
        # Explicit readiness signal -- polled below instead of a blind sleep,
        # so this test cannot flake on host-load timing (the same class of
        # subprocess-timing flake already documented elsewhere in this repo's
        # test suite): the CTRL_BREAK send is never attempted until the
        # handler is DEMONSTRABLY registered, not just "probably by now".
        f"open(r'{ready_path}', 'w').write('ready')\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    backend = pl.WindowsJobObjectBackend(ensure_console_for_graceful_shutdown=True)
    handle = backend.spawn([sys.executable, str(child_script)])
    try:
        deadline = time.monotonic() + 10.0
        while not ready_path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready_path.exists(), "child never signaled its handler was registered"

        ok = backend.close(handle, grace_seconds=5.0)
        assert ok is True
        assert marker_path.exists(), (
            "the child's own CTRL_BREAK handler never fired -- graceful "
            "shutdown was not delivered end-to-end"
        )
        assert marker_path.read_text().strip() == str(pl._CTRL_BREAK_EVENT)
    finally:
        if handle.popen is not None and handle.popen.poll() is None:
            handle.popen.kill()


# ---------------------------------------------------------------------------
# 7. get_default_backend -- platform selection + console-flag passthrough
# ---------------------------------------------------------------------------


def test_get_default_backend_windows(monkeypatch):
    monkeypatch.setattr(pl.sys, "platform", "win32")
    backend = pl.get_default_backend()
    assert isinstance(backend, pl.WindowsJobObjectBackend)


def test_get_default_backend_posix(monkeypatch):
    monkeypatch.setattr(pl.sys, "platform", "linux")
    backend = pl.get_default_backend()
    assert isinstance(backend, pl.PosixProcessGroupBackend)


def test_get_default_backend_console_flag_defaults_false(monkeypatch):
    monkeypatch.setattr(pl.sys, "platform", "win32")
    backend = pl.get_default_backend()
    assert backend._ensure_console_for_graceful_shutdown is False


def test_get_default_backend_console_flag_passthrough(monkeypatch):
    monkeypatch.setattr(pl.sys, "platform", "win32")
    backend = pl.get_default_backend(ensure_console_for_graceful_shutdown=True)
    assert backend._ensure_console_for_graceful_shutdown is True


def test_get_default_backend_console_flag_ignored_on_posix(monkeypatch):
    monkeypatch.setattr(pl.sys, "platform", "linux")
    backend = pl.get_default_backend(ensure_console_for_graceful_shutdown=True)
    assert isinstance(backend, pl.PosixProcessGroupBackend)  # no crash, no such attribute needed


def test_get_default_backend_kill_on_job_close_defaults_true(monkeypatch):
    monkeypatch.setattr(pl.sys, "platform", "win32")
    backend = pl.get_default_backend()
    assert backend._kill_on_job_close is True


def test_get_default_backend_kill_on_job_close_passthrough_false(monkeypatch):
    """d397bb71: a detached/fire-and-forget caller opts out via this
    passthrough."""
    monkeypatch.setattr(pl.sys, "platform", "win32")
    backend = pl.get_default_backend(kill_on_job_close=False)
    assert backend._kill_on_job_close is False


def test_get_default_backend_kill_on_job_close_ignored_on_posix(monkeypatch):
    monkeypatch.setattr(pl.sys, "platform", "linux")
    backend = pl.get_default_backend(kill_on_job_close=False)
    assert isinstance(backend, pl.PosixProcessGroupBackend)  # no crash, no such attribute needed
