"""Relaunching the desktop app: the command for a frozen build and a source run, a start that fails, and the startup
check of the new copy. No process is started: the child is a fake with poll().
"""

from meridian_desktop.app_relaunch import (
    ALIVE,
    EXITED,
    EXITED_TEXT,
    FAILED_START_TEXT,
    RUNNING,
    StartupWatch,
    relaunch_plan,
    start_new_copy,
)


class FakeChild:
    """Stands in for a Popen object. poll() returns the codes in order and repeats the last one."""

    def __init__(self, *codes):
        self._codes = list(codes)

    def poll(self):
        if len(self._codes) > 1:
            return self._codes.pop(0)
        return self._codes[0]


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def test_a_frozen_build_relaunches_its_own_executable_with_the_same_arguments():
    plan = relaunch_plan(True, "C:/Apps/Meridian.exe", ["--flag"], "C:/Apps")
    assert plan.argv == ("C:/Apps/Meridian.exe", "--flag")
    assert plan.cwd == "C:/Apps"


def test_a_source_run_relaunches_the_package_with_the_same_arguments():
    plan = relaunch_plan(False, "C:/py/python.exe", ["--flag"], "C:/desktop")
    assert plan.argv == ("C:/py/python.exe", "-m", "meridian_desktop", "--flag")
    assert plan.cwd == "C:/desktop"


def test_a_successful_start_returns_the_child_and_is_detached_on_windows():
    seen = {}
    child = FakeChild(None)

    def fake_popen(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return child

    plan = relaunch_plan(False, "py", [], "C:/desktop")
    assert start_new_copy(plan, popen=fake_popen, platform="win32") == (child, None)
    assert seen["argv"] == ["py", "-m", "meridian_desktop"]
    assert seen["kwargs"]["cwd"] == "C:/desktop"
    assert seen["kwargs"]["creationflags"] != 0


def test_a_failed_start_says_only_the_error_class_and_keeps_the_window_open():
    def failing(argv, **kwargs):
        raise OSError("C:/secret/path/is/wrong")

    child, text = start_new_copy(
        relaunch_plan(True, "C:/Apps/Meridian.exe", [], "C:/Apps"), popen=failing, platform="win32"
    )
    assert child is None
    assert text == FAILED_START_TEXT.format("OSError")
    assert "secret" not in text


def test_a_copy_that_keeps_running_is_alive_only_after_the_hold():
    clock = FakeClock()
    watch = StartupWatch(FakeChild(None), hold_seconds=3.0, clock=clock)
    assert watch.check() == (RUNNING, "")
    clock.now += 2.9
    assert watch.check() == (RUNNING, "")
    clock.now += 0.1
    assert watch.check() == (ALIVE, "")


def test_a_copy_that_exits_while_starting_reports_its_exit_code_only():
    clock = FakeClock()
    watch = StartupWatch(FakeChild(None, 1), hold_seconds=3.0, clock=clock)
    assert watch.check() == (RUNNING, "")
    state, text = watch.check()
    assert state == EXITED
    assert text == EXITED_TEXT.format(1)
    assert "exit code 1" in text


def test_an_exit_after_the_hold_still_counts_as_an_exit():
    clock = FakeClock()
    watch = StartupWatch(FakeChild(2), hold_seconds=3.0, clock=clock)
    clock.now += 10
    assert watch.check() == (EXITED, EXITED_TEXT.format(2))
