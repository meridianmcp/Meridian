"""Cross-platform PID liveness checks must never signal a Windows console."""

from __future__ import annotations

import os
import sys
import ctypes

import pytest

from meridian import pid_probe
from meridian.pid_probe import pid_is_alive


class _FakeProbe:
    def __init__(self, *, handle=42, exit_code=259, error=0):
        self.handle = handle
        self.exit_code = exit_code
        self.error = error
        self.closed: list[int] = []

    def open_process(self, pid: int):
        return self.handle

    def get_exit_code(self, handle: int):
        return self.exit_code

    def get_last_error(self):
        return self.error

    def close_handle(self, handle: int):
        self.closed.append(handle)
        return True


def test_nonpositive_pid_is_dead_without_probing(monkeypatch):
    monkeypatch.setattr(os, "kill", lambda *_args: pytest.fail("unexpected PID probe"))
    assert pid_is_alive(0, platform="posix") is False
    assert pid_is_alive(-1, platform="win32") is False


def test_posix_liveness_uses_signal_zero_and_handles_dead_or_denied(monkeypatch):
    calls: list[tuple[int, int]] = []

    def alive(pid: int, signal: int):
        calls.append((pid, signal))

    monkeypatch.setattr(os, "kill", alive)
    assert pid_is_alive(123, platform="linux") is True
    assert calls == [(123, 0)]

    def dead(_pid: int, _signal: int):
        raise ProcessLookupError()

    monkeypatch.setattr(os, "kill", dead)
    assert pid_is_alive(123, platform="linux") is False

    def denied(_pid: int, _signal: int):
        raise PermissionError()

    monkeypatch.setattr(os, "kill", denied)
    assert pid_is_alive(123, platform="linux") is True

    def os_error(_pid: int, _signal: int):
        raise OSError("invalid pid")

    monkeypatch.setattr(os, "kill", os_error)
    assert pid_is_alive(123, platform="linux") is False


def test_windows_live_process_reads_exit_code_and_closes_handle():
    probe = _FakeProbe(exit_code=259)
    assert pid_is_alive(123, platform="win32", win32_probe_loader=lambda: probe) is True
    assert probe.closed == [42]


def test_windows_exited_process_is_dead_even_when_handle_opens():
    probe = _FakeProbe(exit_code=0)
    assert pid_is_alive(123, platform="win32", win32_probe_loader=lambda: probe) is False
    assert probe.closed == [42]


@pytest.mark.parametrize(("error", "expected"), [(87, False), (5, True), (1234, True)])
def test_windows_open_failure_is_conservative(error: int, expected: bool):
    probe = _FakeProbe(handle=None, error=error)
    assert pid_is_alive(123, platform="win32", win32_probe_loader=lambda: probe) is expected


def test_windows_unavailable_or_broken_probe_fails_safe_alive():
    assert pid_is_alive(123, platform="win32", win32_probe_loader=lambda: None) is True
    assert pid_is_alive(
        123,
        platform="win32",
        win32_probe_loader=lambda: (_ for _ in ()).throw(RuntimeError("loader failed")),
    ) is True


def test_windows_probe_error_and_unknown_exit_code_fail_safe_alive():
    class BrokenProbe(_FakeProbe):
        def get_exit_code(self, handle: int):
            raise OSError("query failed")

    probe = BrokenProbe()
    assert pid_is_alive(123, platform="win32", win32_probe_loader=lambda: probe) is True
    assert probe.closed == [42]

    class UnknownProbe(_FakeProbe):
        def get_exit_code(self, handle: int):
            return None

    assert pid_is_alive(123, platform="win32", win32_probe_loader=lambda: UnknownProbe()) is True


def test_win32_probe_adapter_wraps_kernel32_calls():
    class FakeKernel32:
        def OpenProcess(self, access, inherit, pid):
            assert access == 0x1000
            assert inherit is False
            assert pid == 123
            return 42

        def GetExitCodeProcess(self, handle, pointer):
            assert handle == 42
            pointer._obj.value = 259
            return 1

        def GetLastError(self):
            return 87

        def CloseHandle(self, handle):
            assert handle == 42
            return 1

    probe = pid_probe._Win32ProcessProbe(FakeKernel32())
    assert probe.open_process(123) == 42
    assert probe.get_exit_code(42) == 259
    assert probe.get_last_error() == 87
    assert probe.close_handle(42) is True


def test_win32_loader_binds_kernel32_functions(monkeypatch):
    class FakeFunction:
        def __init__(self, fn):
            self.fn = fn

        def __call__(self, *args):
            return self.fn(*args)

    class FakeKernel32:
        OpenProcess = FakeFunction(lambda *_args: 42)
        GetExitCodeProcess = FakeFunction(
            lambda _handle, pointer: (setattr(pointer._obj, "value", 259) or 1)
        )
        GetLastError = FakeFunction(lambda: 0)
        CloseHandle = FakeFunction(lambda _handle: 1)

    kernel32 = FakeKernel32()
    monkeypatch.setattr(pid_probe.sys, "platform", "win32")
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_a, **_k: kernel32, raising=False)
    probe = pid_probe._load_win32_process_probe()
    assert probe is not None
    assert pid_is_alive(123, platform="win32", win32_probe_loader=lambda: probe) is True


def test_win32_loader_is_unavailable_off_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert pid_probe._load_win32_process_probe() is None
