"""The newest-first view of a grouped log. Pure Python: the lines are given, nothing is read from disk."""

from meridian_desktop.log_launches import group_by_launch, launch_view

LINES = [
    "2026-11-10T10:00:00+00:00 start scope=local outcome=confirmed pid=1",
    "a line in launch one",
    "2026-11-10T12:00:00+00:00 start scope=local outcome=failed pid=not reported",
    "a line in launch two",
]


def test_newest_first_puts_the_latest_launch_at_the_top_and_its_newest_line_first():
    text = launch_view(group_by_launch(LINES), selected=None, newest_first=True)

    assert text.index("Launch 2 of 2") < text.index("Launch 1 of 2")
    assert text.index("a line in launch two") < text.index("2026-11-10T12:00:00+00:00 start")


def test_the_default_view_keeps_the_oldest_launch_first():
    text = launch_view(group_by_launch(LINES), selected=None)

    assert text.index("Launch 1 of 2") < text.index("Launch 2 of 2")


def test_a_log_with_no_launch_line_and_no_time_shows_its_newest_line_first():
    text = launch_view(group_by_launch(["one", "two"]), selected=None, newest_first=True)

    assert text == "two\none"
