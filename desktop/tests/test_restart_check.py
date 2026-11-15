"""Tests for the restart-needed check: the baseline, the server watch, the banner text and the start hook. No Qt.

Every file is written under tmp_path and its modification time is set by hand, so nothing depends on the clock. The
watch is given explicit modules and process readers, so a test never reads the real sys.modules or starts a process.
The Windows reader is tested with a fake kernel32. Two tests read the creation time of a real process on Windows.
"""

import ctypes
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import ModuleType

import pytest

from meridian_desktop import runner_controls as rc
from meridian_desktop.restart_check import (
    GONE,
    FileStamp,
    Restart,
    RestartWatch,
    ServerFiles,
    banner_text,
    display_name,
    package_files,
    process_started_ns,
)

BASE_NS = 1_700_000_000_000_000_000
NANOS_PER_SECOND = 1_000_000_000
# NTFS keeps 100 ns steps, so an edit a nanosecond after a time is stored as that same time. Tests step by seconds.
LATER_NS = BASE_NS + 60 * NANOS_PER_SECOND
NANOS_PER_HOUR = 3600 * NANOS_PER_SECOND
# The FILETIME tick count of the Unix epoch: 100 ns ticks from 1601-01-01.
WINDOWS_EPOCH_TICKS = 116_444_736_000_000_000


def _set_mtime(path, ns):
    os.utime(path, ns=(ns, ns))


def _write(path, text="x = 1\n", mtime=BASE_NS):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    _set_mtime(path, mtime)
    return str(path)


def _module(name, file):
    module = ModuleType(name)
    module.__file__ = file
    return module


def _no_process(pid):
    return None


def _reader(alive):
    """A process reader over a dict of pid to creation time. A pid missing from the dict is GONE."""
    return lambda pid: alive.get(pid, GONE)


def _watch(modules, *, frozen=False, executable="", started_ns_of=_no_process):
    return RestartWatch(modules=modules, frozen=frozen, executable=executable, started_ns_of=started_ns_of)


def _locked_stat(path, real_stat):
    """An os.stat stand-in that fails with a sharing error for one path and passes every other call through."""

    def locked(target, *args, **kwargs):
        if os.fspath(target) == path:
            raise PermissionError(13, "locked by another process")
        return real_stat(target, *args, **kwargs)

    return locked


# ----------------------------------------------------------- the code baseline


def test_unchanged_code_raises_no_alert(tmp_path):
    path = _write(tmp_path / "meridian_desktop" / "a.py")
    watch = _watch({"meridian_desktop.a": _module("meridian_desktop.a", path)})
    assert watch.check() == Restart()
    assert not watch.check().needed


def test_changed_modification_time_is_reported_by_package_name(tmp_path):
    path = _write(tmp_path / "meridian_desktop" / "ui" / "main_window.py")
    watch = _watch({"meridian_desktop.ui.main_window": _module("meridian_desktop.ui.main_window", path)})
    _set_mtime(path, LATER_NS)
    assert watch.check() == Restart(code=("meridian_desktop/ui/main_window.py",))


def test_changed_size_is_reported_even_when_the_modification_time_is_the_same(tmp_path):
    path = _write(tmp_path / "meridian_desktop" / "a.py")
    watch = _watch({"meridian_desktop.a": _module("meridian_desktop.a", path)})
    _write(tmp_path / "meridian_desktop" / "a.py", text="x = 2222222\n", mtime=BASE_NS)
    assert watch.check().code == ("meridian_desktop/a.py",)


def test_a_deleted_code_file_is_reported(tmp_path):
    path = _write(tmp_path / "meridian_desktop" / "a.py")
    watch = _watch({"meridian_desktop.a": _module("meridian_desktop.a", path)})
    os.remove(path)
    assert watch.check() == Restart(code=("meridian_desktop/a.py",))


def test_a_new_file_on_disk_is_ignored(tmp_path):
    path = _write(tmp_path / "meridian_desktop" / "a.py")
    watch = _watch({"meridian_desktop.a": _module("meridian_desktop.a", path)})
    _write(tmp_path / "meridian_desktop" / "brand_new.py")
    assert watch.check() == Restart()


def test_a_module_imported_after_the_baseline_is_watched_from_its_first_check(tmp_path):
    _write(tmp_path / "meridian_desktop" / "a.py")
    modules = {}
    watch = _watch(modules)
    late = _write(tmp_path / "meridian_desktop" / "late.py")
    modules["meridian_desktop.late"] = _module("meridian_desktop.late", late)
    assert watch.check() == Restart()
    _set_mtime(late, LATER_NS)
    assert watch.check().code == ("meridian_desktop/late.py",)


def test_a_transient_stat_error_keeps_the_previous_stamp(tmp_path, monkeypatch):
    path = _write(tmp_path / "meridian_desktop" / "a.py")
    watch = _watch({"meridian_desktop.a": _module("meridian_desktop.a", path)})
    real_stat = os.stat
    monkeypatch.setattr(os, "stat", _locked_stat(path, real_stat))
    assert watch.check() == Restart()
    monkeypatch.setattr(os, "stat", real_stat)
    _set_mtime(path, LATER_NS)
    assert watch.check().code == ("meridian_desktop/a.py",)


def test_a_module_unreadable_at_the_baseline_is_taken_on_a_later_check(tmp_path, monkeypatch):
    path = _write(tmp_path / "meridian_desktop" / "a.py")
    real_stat = os.stat
    monkeypatch.setattr(os, "stat", _locked_stat(path, real_stat))
    watch = _watch({"meridian_desktop.a": _module("meridian_desktop.a", path)})
    monkeypatch.setattr(os, "stat", real_stat)
    assert watch.check() == Restart()
    _set_mtime(path, LATER_NS)
    assert watch.check().code == ("meridian_desktop/a.py",)


def test_package_files_take_the_package_modules_and_their_init_files_only():
    modules = {
        "meridian_desktop": _module("meridian_desktop", "/x/meridian_desktop/__init__.py"),
        "meridian_desktop.ui.main_window": _module(
            "meridian_desktop.ui.main_window", "/x/meridian_desktop/ui/main_window.py"
        ),
        "meridian_desktopx": _module("meridian_desktopx", "/x/other.py"),
        "json": _module("json", "/lib/json/__init__.py"),
        "meridian_desktop.no_file": ModuleType("meridian_desktop.no_file"),
        "meridian_desktop.none": None,
    }
    assert package_files(modules) == {
        "/x/meridian_desktop/__init__.py",
        "/x/meridian_desktop/ui/main_window.py",
    }


def test_package_files_include_main_only_when_it_sits_in_the_package_folder():
    inside = {"__main__": _module("__main__", "C:\\app\\meridian_desktop\\__main__.py")}
    assert package_files(inside) == {"C:\\app\\meridian_desktop\\__main__.py"}
    outside = {"__main__": _module("__main__", "C:\\tools\\pytest\\__main__.py")}
    assert package_files(outside) == set()


# ----------------------------------------------------------- frozen build


def test_a_frozen_build_watches_its_executable(tmp_path):
    exe = _write(tmp_path / "Meridian.exe", "MZ", mtime=BASE_NS)
    watch = _watch({}, frozen=True, executable=exe)
    _set_mtime(exe, LATER_NS)
    assert watch.check() == Restart(code=("Meridian.exe",))


def test_a_source_run_does_not_watch_the_python_executable(tmp_path):
    exe = _write(tmp_path / "python.exe", "MZ", mtime=BASE_NS)
    watch = _watch({}, frozen=False, executable=exe)
    _set_mtime(exe, LATER_NS)
    assert watch.check() == Restart()


def test_the_frozen_flag_and_executable_are_read_when_the_watch_is_built(tmp_path, monkeypatch):
    exe = _write(tmp_path / "Meridian.exe", "MZ", mtime=BASE_NS)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", exe)
    watch = RestartWatch(modules={}, started_ns_of=_no_process)  # no frozen or executable argument
    _set_mtime(exe, LATER_NS)
    assert watch.check() == Restart(code=("Meridian.exe",))


def test_without_the_frozen_flag_the_executable_is_not_watched(tmp_path, monkeypatch):
    exe = _write(tmp_path / "python.exe", "MZ", mtime=BASE_NS)
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(sys, "executable", exe)
    watch = RestartWatch(modules={}, started_ns_of=_no_process)  # no frozen or executable argument
    _set_mtime(exe, LATER_NS)
    assert watch.check() == Restart()


# ----------------------------------------------------------- display names and banner text


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("C:\\repo\\meridian_desktop\\ui\\main_window.py", "meridian_desktop/ui/main_window.py"),
        ("/x/meridian_desktop/__init__.py", "meridian_desktop/__init__.py"),
        ("C:/meridian_desktop/old/meridian_desktop/x.py", "meridian_desktop/x.py"),
        ("C:\\Program Files\\Meridian\\Meridian.exe", "Meridian.exe"),
    ],
)
def test_display_name_starts_at_the_package_folder(path, expected):
    assert display_name(path) == expected


def test_banner_text_is_empty_when_nothing_changed():
    assert banner_text(Restart()) == ""


def test_banner_text_names_one_changed_file():
    assert banner_text(Restart(code=("meridian_desktop/a.py",))) == (
        "Restart needed: meridian_desktop/a.py changed on disk since this window opened."
    )


def test_banner_text_names_three_files_and_counts_the_rest():
    names = ("meridian_desktop/a.py", "meridian_desktop/b.py", "meridian_desktop/c.py", "meridian_desktop/d.py")
    assert banner_text(Restart(code=names)) == (
        "Restart needed: meridian_desktop/a.py, meridian_desktop/b.py, meridian_desktop/c.py and 1 more "
        "changed on disk since this window opened."
    )


def test_banner_text_for_the_server_alone():
    assert banner_text(Restart(server=True)) == (
        "Restart needed: the local Meridian server changed on disk since it started."
    )


def test_banner_text_for_code_and_the_server_together():
    text = banner_text(Restart(code=("meridian_desktop/a.py",), server=True))
    assert text == (
        "Restart needed: meridian_desktop/a.py changed on disk since this window opened. "
        "The local Meridian server changed on disk since it started."
    )


# ----------------------------------------------------------- the local server watch


def _server_setup(tmp_path, *, pid=4321, started=BASE_NS + 10 * NANOS_PER_SECOND):
    """A checkout with one server file written before the server started, and a process reader that knows the pid."""
    checkout = tmp_path / "core"
    source = _write(checkout / "meridian" / "server.py", mtime=BASE_NS)
    alive = {pid: started}
    return checkout, source, alive


def test_a_server_file_edited_after_the_server_started_marks_it_stale(tmp_path):
    checkout, source, alive = _server_setup(tmp_path)
    watch = _watch({}, started_ns_of=_reader(alive))
    watch.note_server_started(4321, str(checkout))
    assert not watch.check().server
    _set_mtime(source, alive[4321] + NANOS_PER_SECOND)
    assert watch.check() == Restart(server=True)


def test_a_file_with_the_same_size_and_an_older_timestamp_is_caught(tmp_path):
    checkout, source, alive = _server_setup(tmp_path)
    watch = _watch({}, started_ns_of=_reader(alive))
    watch.note_server_started(4321, str(checkout))
    # Same size, and a timestamp from before the server started: a replacement that a newer-than-start check misses.
    _write(source, text="x = 9\n", mtime=BASE_NS - 60 * NANOS_PER_SECOND)
    assert watch.check() == Restart(server=True)


def test_a_size_change_with_the_same_timestamp_is_caught(tmp_path):
    checkout, source, alive = _server_setup(tmp_path)
    watch = _watch({}, started_ns_of=_reader(alive))
    watch.note_server_started(4321, str(checkout))
    _write(source, text="x = 2222222\n", mtime=BASE_NS)
    assert watch.check() == Restart(server=True)


def test_a_non_python_file_under_the_server_package_does_not_mark_it_stale(tmp_path):
    # The file exists before the start, so the snapshot would hold it if the .py filter were missing. Changing it after
    # the start must then mark the server stale. It does not, so the filter is what keeps it out.
    checkout, _source, alive = _server_setup(tmp_path)
    page = _write(checkout / "meridian" / "static" / "page.js", mtime=BASE_NS)
    watch = _watch({}, started_ns_of=_reader(alive))
    watch.note_server_started(4321, str(checkout))
    _set_mtime(page, LATER_NS)
    assert not watch.check().server


def test_a_python_file_added_after_the_start_is_not_seen(tmp_path):
    # The snapshot is taken when the start is confirmed, so a file that appears later is outside it. This pins the limit.
    checkout, _source, alive = _server_setup(tmp_path)
    watch = _watch({}, started_ns_of=_reader(alive))
    watch.note_server_started(4321, str(checkout))
    _write(checkout / "meridian" / "new_module.py", mtime=LATER_NS)
    assert not watch.check().server


def test_the_server_watch_drops_the_server_when_its_process_is_gone(tmp_path):
    checkout, source, alive = _server_setup(tmp_path)
    started = alive[4321]
    watch = _watch({}, started_ns_of=_reader(alive))
    watch.note_server_started(4321, str(checkout))
    _set_mtime(source, started + NANOS_PER_SECOND)
    assert watch.check().server
    alive[4321] = GONE
    assert not watch.check().server
    # The same process back under the same pid is not watched again. The owner has to start it from the window.
    alive[4321] = started
    assert not watch.check().server


def test_the_server_watch_drops_the_server_when_its_pid_is_reused(tmp_path):
    checkout, source, alive = _server_setup(tmp_path)
    watch = _watch({}, started_ns_of=_reader(alive))
    watch.note_server_started(4321, str(checkout))
    _set_mtime(source, alive[4321] + NANOS_PER_SECOND)
    alive[4321] = alive[4321] + 999  # the same pid now belongs to a process with another creation time
    assert not watch.check().server


def test_an_unreadable_creation_time_keeps_the_watch_and_it_is_read_again(tmp_path):
    checkout, source, alive = _server_setup(tmp_path)
    started = alive[4321]
    reading = {"value": started}
    watch = _watch({}, started_ns_of=lambda pid: reading["value"])
    watch.note_server_started(4321, str(checkout))
    reading["value"] = None  # the query fails on this tick
    _set_mtime(source, started + NANOS_PER_SECOND)
    assert watch.check().server
    reading["value"] = started  # readable again, and the server is still watched
    assert watch.check().server


def test_a_server_whose_start_time_cannot_be_read_at_the_start_is_recorded_and_read_again(tmp_path):
    checkout, source, _alive = _server_setup(tmp_path)
    reading = {"value": None}
    watch = _watch({}, started_ns_of=lambda pid: reading["value"])
    watch.note_server_started(4321, str(checkout))
    _set_mtime(source, LATER_NS)
    assert watch.check().server  # recorded anyway, so the changed file is reported
    reading["value"] = GONE
    assert not watch.check().server  # only GONE drops it


def test_a_start_time_learned_after_the_start_catches_a_reused_pid(tmp_path):
    checkout, source, _alive = _server_setup(tmp_path)
    started = BASE_NS + 10 * NANOS_PER_SECOND
    reading = {"value": None}
    watch = _watch({}, started_ns_of=lambda pid: reading["value"])
    watch.note_server_started(4321, str(checkout))
    reading["value"] = started  # the start time is readable from now on
    assert not watch.check().server
    _set_mtime(source, LATER_NS)
    # Still watched once the start time is known: the edit is reported before any PID reuse is simulated.
    assert watch.check() == Restart(server=True)
    reading["value"] = started + 999  # only now does the PID belong to another process
    assert not watch.check().server


def test_a_server_that_is_already_gone_at_the_start_is_not_watched(tmp_path):
    checkout, source, _alive = _server_setup(tmp_path)
    watch = _watch({}, started_ns_of=lambda pid: GONE)
    watch.note_server_started(4321, str(checkout))
    _set_mtime(source, LATER_NS)
    assert not watch.check().server


def test_no_checkout_means_no_server_is_watched(tmp_path, monkeypatch):
    # A .py file sits in the working folder's meridian package. Without the empty-checkout guard the watch would list it.
    monkeypatch.chdir(tmp_path)
    source = _write(tmp_path / "meridian" / "server.py", mtime=BASE_NS)
    alive = {4321: BASE_NS + 10 * NANOS_PER_SECOND}
    watch = _watch({}, started_ns_of=_reader(alive))
    watch.note_server_started(4321, "")
    _set_mtime(source, LATER_NS)
    assert not watch.check().server


def test_clearing_a_gone_server_does_not_drop_a_newer_start(tmp_path):
    checkout, source, alive = _server_setup(tmp_path)
    started = alive[4321]
    alive[5555] = started
    state = {"replace": False}
    watch = None

    def reader(pid):
        if pid == 4321 and state["replace"]:
            # While the old server is being checked, a new start is confirmed and replaces it.
            watch.note_server_started(5555, str(checkout))
            return GONE
        return alive.get(pid, GONE)

    watch = _watch({}, started_ns_of=reader)
    watch.note_server_started(4321, str(checkout))
    state["replace"] = True
    assert not watch.check().server
    _set_mtime(source, started + NANOS_PER_SECOND)
    assert watch.check().server  # the newer start is still watched


class _PausingWatch(RestartWatch):
    """A watch whose clear of the server record can pause, so another thread can start a replacement inside a check.

    ``pause_clear`` is called once, just before the first clear of the record to None. The record itself is stored in
    the instance dict, so the property below does not recurse.
    """

    def __init__(self, *args, **kwargs):
        self.pause_clear = None
        super().__init__(*args, **kwargs)

    @property
    def _server(self):
        return self.__dict__.get("_server_record")

    @_server.setter
    def _server(self, value):
        pause = self.pause_clear
        if value is None and pause is not None:
            self.pause_clear = None
            pause()
        self.__dict__["_server_record"] = value


def test_a_replacement_started_while_a_check_clears_the_old_record_survives_the_check(tmp_path):
    """A check clears the old record, and a new start for another PID lands inside that clear.

    The check reads the old PID, which its reader reports GONE. Just before it clears the record, it pauses. The new
    start runs on another thread, released by a barrier, and replaces the record during the pause. Then the check
    finishes its clear. The replacement must survive. Without the lock around the clear, the clear stores None after
    the replacement and drops it. With the lock, the start waits for the check, so the pause times out and the
    replacement is stored after the clear.
    """
    checkout, source, alive = _server_setup(tmp_path)
    alive[5555] = alive[4321]
    retired = {"value": False}

    def reader(pid):
        if pid == 4321 and retired["value"]:
            return GONE
        return alive.get(pid, GONE)

    watch = _PausingWatch(modules={}, frozen=False, executable="", started_ns_of=reader)
    watch.note_server_started(4321, str(checkout))
    retired["value"] = True
    in_clear = threading.Event()
    replaced = threading.Event()

    def pause():
        in_clear.set()
        replaced.wait(timeout=2)  # with the lock, the start below waits for this check, so this times out

    watch.pause_clear = pause
    barrier = threading.Barrier(2)
    errors = []

    def check_side():
        try:
            barrier.wait(timeout=30)
            watch.check()
        except Exception as error:  # the test reports any failure from the other thread
            errors.append(error)

    def start_side():
        try:
            barrier.wait(timeout=30)
            in_clear.wait(timeout=10)
            watch.note_server_started(5555, str(checkout))
            replaced.set()
        except Exception as error:
            errors.append(error)

    threads = [threading.Thread(target=check_side), threading.Thread(target=start_side)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not any(thread.is_alive() for thread in threads), "a thread hung"
    assert errors == []
    _set_mtime(source, alive[5555] + NANOS_PER_SECOND)
    assert watch.check().server, "the replacement start was dropped by the check that was clearing the old record"


def test_server_files_lists_python_files_only_and_a_missing_folder_is_empty(tmp_path):
    root = tmp_path / "meridian"
    _write(root / "a.py", mtime=BASE_NS)
    _write(root / "sub" / "b.py", mtime=LATER_NS)
    _write(root / "data.json", mtime=LATER_NS + NANOS_PER_HOUR)
    files = ServerFiles(root)
    assert sorted(os.path.basename(path) for path in files.stamps) == ["a.py", "b.py"]
    b_path = os.path.join(str(root), "sub", "b.py")
    assert files.stamps[b_path] == FileStamp(LATER_NS, os.stat(b_path).st_size)
    assert ServerFiles(tmp_path / "missing").stamps == {}


def test_a_server_file_that_cannot_be_read_at_the_start_gets_its_baseline_on_a_later_check(tmp_path, monkeypatch):
    checkout, source, alive = _server_setup(tmp_path)
    real_stat = os.stat
    monkeypatch.setattr(os, "stat", _locked_stat(source, real_stat))
    watch = _watch({}, started_ns_of=_reader(alive))
    watch.note_server_started(4321, str(checkout))
    monkeypatch.setattr(os, "stat", real_stat)
    assert not watch.check().server  # the first readable stamp becomes its baseline
    _set_mtime(source, alive[4321] + NANOS_PER_SECOND)
    assert watch.check() == Restart(server=True)


def test_an_unlistable_server_folder_is_listed_on_a_later_check_and_its_files_join(tmp_path, monkeypatch):
    checkout, _source, alive = _server_setup(tmp_path)
    folder = os.path.join(str(checkout / "meridian"), "sub")
    sub_file = _write(Path(folder) / "c.py", mtime=BASE_NS)
    real_scandir = os.scandir

    def denied(path="."):
        if os.path.normcase(os.path.normpath(os.fspath(path))) == os.path.normcase(os.path.normpath(folder)):
            raise PermissionError(13, "denied", folder)
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", denied)
    watch = _watch({}, started_ns_of=_reader(alive))
    watch.note_server_started(4321, str(checkout))
    monkeypatch.setattr(os, "scandir", real_scandir)
    assert not watch.check().server  # the folder is listed now, and its c.py gets a baseline
    _set_mtime(sub_file, alive[4321] + NANOS_PER_SECOND)
    assert watch.check() == Restart(server=True)


def test_a_pending_server_file_missing_for_a_moment_is_baselined_again_and_its_later_edit_is_seen(tmp_path, monkeypatch):
    checkout, source, alive = _server_setup(tmp_path)
    real_stat = os.stat
    monkeypatch.setattr(os, "stat", _locked_stat(source, real_stat))
    watch = _watch({}, started_ns_of=_reader(alive))
    watch.note_server_started(4321, str(checkout))  # the file cannot be read at the start, so it is pending
    monkeypatch.setattr(os, "stat", real_stat)
    os.remove(source)  # missing for a moment, as an editor's save can leave it
    assert not watch.check().server  # still pending, not dropped
    _write(source, mtime=BASE_NS)  # back again, and baselined on this check
    assert not watch.check().server
    _set_mtime(source, alive[4321] + NANOS_PER_SECOND)  # the later edit must be seen
    assert watch.check() == Restart(server=True)


def test_a_newly_readable_folder_is_listed_a_bounded_number_of_steps_per_tick(tmp_path, monkeypatch):
    root = tmp_path / "meridian"
    folder = os.path.join(str(root), "sub")
    for index in range(10):
        _write(Path(folder) / f"m{index}.py", mtime=BASE_NS)
    real_scandir = os.scandir

    def denied(path="."):
        if os.path.normcase(os.path.normpath(os.fspath(path))) == os.path.normcase(os.path.normpath(folder)):
            raise PermissionError(13, "denied", folder)
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", denied)
    files = ServerFiles(root)
    monkeypatch.setattr(os, "scandir", real_scandir)
    monkeypatch.setattr("meridian_desktop.restart_check.MAX_ENTRIES_PER_TICK", 3)

    def in_folder():
        return {path for path in files.tracked() if os.path.dirname(path) == folder}

    files.refresh()
    assert len(in_folder()) <= 3  # one tick lists at most the cap
    ticks = 1
    while len(in_folder()) < 10 and ticks < 20:
        files.refresh()
        ticks += 1
    assert len(in_folder()) == 10
    assert ticks > 1  # the folder needed more than one tick, so the cap was really in force


def test_a_confirmed_stop_does_not_reach_the_watch(tmp_path):
    calls = []
    # The stop reply names the pid it stopped, so a guard that wrongly sends STOP to the hook would have something to send.
    spawn = _FakeSpawn(_done({"scope": "local", "child": {"state": "stopped", "pid": 4321}}))
    assert _control(tmp_path, spawn, calls).stop("local").outcome == rc.CONFIRMED
    assert calls == []


# ----------------------------------------------------------- the Windows process reader, through a fake kernel32


class _FakeKernel32:
    """Stands in for ctypes.WinDLL("kernel32"). OpenProcess, GetProcessTimes and CloseHandle return what the test sets."""

    def __init__(self, *, handle=0x1234, times_ok=True, created=0, exited=0):
        self.handle = handle
        self.times_ok = times_ok
        self.created = created  # FILETIME ticks, 100 ns since 1601
        self.exited = exited
        self.closed = []

        def open_process(access, inherit, pid):
            return self.handle

        def get_process_times(handle, created_ref, exited_ref, kernel_ref, user_ref):
            if not self.times_ok:
                return 0
            _set_filetime(created_ref, self.created)
            _set_filetime(exited_ref, self.exited)
            return 1

        def close_handle(handle):
            self.closed.append(handle)
            return 1

        self.OpenProcess = open_process
        self.GetProcessTimes = get_process_times
        self.CloseHandle = close_handle


def _set_filetime(byref_arg, ticks):
    # ctypes.byref returns an argument holder. Its target is the FILETIME structure the code passed in.
    target = byref_arg._obj
    target.dwLowDateTime = ticks & 0xFFFFFFFF
    target.dwHighDateTime = ticks >> 32


@pytest.fixture
def windows_api(monkeypatch):
    """Makes process_started_ns take its Windows path, with a fake kernel32 and a chosen last error."""

    def install(fake, last_error=0):
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(ctypes, "WinDLL", lambda name, **kwargs: fake, raising=False)
        monkeypatch.setattr(ctypes, "get_last_error", lambda: last_error, raising=False)

    return install


def test_a_failed_open_for_another_reason_is_unreadable(windows_api):
    windows_api(_FakeKernel32(handle=0), last_error=6)  # ERROR_INVALID_HANDLE
    assert process_started_ns(4321) is None


def test_access_denied_on_the_pid_of_a_started_server_means_it_was_reused(windows_api):
    windows_api(_FakeKernel32(handle=0), last_error=5)  # ERROR_ACCESS_DENIED
    assert process_started_ns(4321) is GONE


def test_an_invalid_pid_is_gone(windows_api):
    windows_api(_FakeKernel32(handle=0), last_error=87)  # ERROR_INVALID_PARAMETER
    assert process_started_ns(4321) is GONE


def test_a_failed_creation_time_query_is_unreadable_and_the_handle_is_closed(windows_api):
    fake = _FakeKernel32(times_ok=False)
    windows_api(fake)
    assert process_started_ns(4321) is None
    assert fake.closed == [0x1234]


def test_a_set_exit_time_is_gone_and_the_handle_is_closed(windows_api):
    fake = _FakeKernel32(exited=123)
    windows_api(fake)
    assert process_started_ns(4321) is GONE
    assert fake.closed == [0x1234]


def test_a_creation_time_is_read_as_unix_nanoseconds(windows_api):
    windows_api(_FakeKernel32(created=WINDOWS_EPOCH_TICKS + 100_000_000))  # ten seconds after the Unix epoch
    assert process_started_ns(4321) == 10 * NANOS_PER_SECOND


# ----------------------------------------------------------- the real process creation time


@pytest.mark.skipif(sys.platform != "win32", reason="the creation time is read through Windows")
def test_process_started_ns_reads_the_creation_time_of_this_process():
    started = process_started_ns(os.getpid())
    assert isinstance(started, int)
    age_ns = time.time_ns() - started
    assert 0 < age_ns < 24 * NANOS_PER_HOUR


@pytest.mark.skipif(sys.platform != "win32", reason="the creation time is read through Windows")
def test_process_started_ns_is_gone_for_a_pid_that_names_no_process():
    assert process_started_ns(0x7FFFFFF0) is GONE


@pytest.mark.skipif(sys.platform != "win32", reason="the creation time is read through Windows")
def test_process_started_ns_is_gone_for_a_process_that_has_exited_but_is_still_open_here():
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait(timeout=60)
    # Popen still holds the process handle, so the query can open the process. Its exit time is set, so it is gone.
    assert process_started_ns(child.pid) is GONE


def test_process_started_ns_is_none_off_windows_and_gone_for_pid_zero(monkeypatch):
    assert process_started_ns(0) is GONE
    monkeypatch.setattr(sys, "platform", "linux")
    assert process_started_ns(os.getpid()) is None


# ----------------------------------------------------------- the runner's start hook


class _FakeSpawn:
    def __init__(self, *replies):
        self.replies = list(replies)

    def __call__(self, argv, **kwargs):
        return self.replies.pop(0)


def _done(payload, code=0):
    return subprocess.CompletedProcess(args=[], returncode=code, stdout=json.dumps(payload).encode("utf-8"), stderr=b"")


def _control(tmp_path, spawn, calls):
    return rc.RunnerControl(
        sys.executable,
        str(tmp_path),
        spawn=spawn,
        log_path=tmp_path / "control.log",
        on_started=lambda pid, cwd: calls.append((pid, cwd)),
    )


def test_a_confirmed_local_start_hands_its_pid_and_checkout_to_the_watch(tmp_path):
    calls = []
    spawn = _FakeSpawn(_done({"scope": "local", "child": {"state": "running", "pid": 4321}}))
    result = _control(tmp_path, spawn, calls).start("local")
    assert result.outcome == rc.CONFIRMED
    assert calls == [(4321, str(tmp_path))]


def test_a_tunnel_start_does_not_reach_the_watch(tmp_path):
    calls = []
    spawn = _FakeSpawn(_done({"scope": "tunnel", "child": {"state": "running", "pid": 5555}}))
    assert _control(tmp_path, spawn, calls).start("tunnel").outcome == rc.CONFIRMED
    assert calls == []


def test_a_local_start_that_is_not_running_does_not_reach_the_watch(tmp_path):
    calls = []
    spawn = _FakeSpawn(_done({"scope": "local", "child": {"state": "crashed", "pid": 4321}}))
    assert _control(tmp_path, spawn, calls).start("local").outcome == rc.FAILED
    assert calls == []


def test_a_status_call_does_not_reach_the_watch(tmp_path):
    calls = []
    spawn = _FakeSpawn(_done({"scope": "local", "child": {"state": "running", "pid": 4321}}))
    _control(tmp_path, spawn, calls).status("local")
    assert calls == []
