"""Relaunching the desktop app: the command for a frozen build and a source run, and a start that fails. No process is started.
"""

from meridian_desktop.app_relaunch import FAILED_START_TEXT, relaunch_plan, start_new_copy


def test_a_frozen_build_relaunches_its_own_executable_with_the_same_arguments():
    plan = relaunch_plan(True, "C:/Apps/Meridian.exe", ["--flag"], "C:/Apps")
    assert plan.argv == ("C:/Apps/Meridian.exe", "--flag")
    assert plan.cwd == "C:/Apps"


def test_a_source_run_relaunches_the_package_with_the_same_arguments():
    plan = relaunch_plan(False, "C:/py/python.exe", ["--flag"], "C:/desktop")
    assert plan.argv == ("C:/py/python.exe", "-m", "meridian_desktop", "--flag")
    assert plan.cwd == "C:/desktop"


def test_a_successful_start_returns_none_and_is_detached_on_windows():
    seen = {}

    def fake_popen(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs

    plan = relaunch_plan(False, "py", [], "C:/desktop")
    assert start_new_copy(plan, popen=fake_popen, platform="win32") is None
    assert seen["argv"] == ["py", "-m", "meridian_desktop"]
    assert seen["kwargs"]["cwd"] == "C:/desktop"
    assert seen["kwargs"]["creationflags"] != 0


def test_a_failed_start_says_only_the_error_class_and_keeps_the_window_open():
    def failing(argv, **kwargs):
        raise OSError("C:/secret/path/is/wrong")

    text = start_new_copy(relaunch_plan(True, "C:/Apps/Meridian.exe", [], "C:/Apps"), popen=failing, platform="win32")
    assert text == FAILED_START_TEXT.format("OSError")
    assert "secret" not in text
