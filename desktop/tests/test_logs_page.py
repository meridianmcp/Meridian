"""Tests for the Logs tab (meridian_desktop/ui/logs_page.py), with pytest-qt.

The desktop log reads real temp files through desktop_log.read_desktop_log_tail, with the environment
passed in, so the real LOCALAPPDATA is never read. The tunnel log reads real temp files through
log_tail.read_tunnel_log_tail, or a fake read. The runner read runs the real RunnerClient against a
fake spawn, or a fake read. Nothing here starts a process, opens a socket, reads the real home
directory or reads the keychain. Every log file is under pytest's temp directory. Fake secrets carry
a fake marker and match no live token. Offscreen Qt, from conftest.
"""

import gc
import json
import subprocess
import threading
import weakref
from pathlib import Path

import shiboken6
from PySide6.QtCore import Qt, QThreadPool, QTimer
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QCheckBox,
    QComboBox,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QToolButton,
    QWidget,
)

from meridian_desktop.desktop_log import desktop_log_path, read_desktop_log_tail
from meridian_desktop.log_tail import (
    DEFAULT_MAX_BYTES,
    NO_LOG_YET_TEXT,
    STATUS_NO_LOG_YET,
    STATUS_OK,
    STATUS_UNREADABLE,
    UNREADABLE_TEXT,
    LogTail,
    read_tunnel_log_tail,
)
from meridian_desktop.runner_client import (
    ERR_NOT_AVAILABLE,
    ERR_TIMEOUT,
    MODULE,
    NOT_AVAILABLE_MESSAGE,
    RunnerClient,
    RunnerResult,
)
from meridian_desktop.ui.logs_page import (
    DESKTOP_EMPTY_TEXT,
    DESKTOP_MISSING_TEXT,
    DESKTOP_UNREADABLE_TEXT,
    EMPTY_TUNNEL_TEXT,
    INTRO_SHOWN_TEXT,
    INTRO_UNREAD_TEXT,
    NO_LOG_TAIL_TEXT,
    REFRESH_BUTTON_TEXT,
    RUNNER_RETRY_TEXT,
    START_TUNNEL_TEXT,
    RUNNER_EMPTY_TEXT,
    RUNNER_NOT_AVAILABLE_PREFIX,
    RUNNER_NOT_CONNECTED_TEXT,
    TUNNEL_MISSING_TEXT,
    TUNNEL_UNREADABLE_TEXT,
    LogsPage,
    runner_log_text,
    runner_tail_not_configured,
)

from _shared_delivery import emit, recording_deliveries, visible_state

FAKE_QUERY_TOKEN = "fake-query-0001"
FAKE_BEARER = "sk_meridian_fake-bearer-0002"
FAKE_BARE = "sk_meridian_fake-bare-0003"
FAKE_RUNNER = "sk_meridian_fake-runner-0004"
FAKE_PASSWORD = "fake-password-0005"
FAKE_API_KEY = "fake-api-key-0006"
SECRETS = (FAKE_QUERY_TOKEN, FAKE_BEARER, FAKE_BARE, FAKE_RUNNER, FAKE_PASSWORD, FAKE_API_KEY)
# Every input control except the one Refresh button, which the button test checks by name. The line edit and the combo box
# are not listed here: the page has one of each, the control pane's search and launch picker, and the read-only test
# checks that they are only those two. Neither one writes a log. They only redraw the control log already read.
WRITE_CONTROLS = (
    QToolButton,
    QCheckBox,
    QRadioButton,
    QAbstractSpinBox,
)
# Words a clear, delete, rotate or truncate control would carry, and the runner's mutating verbs.
FORBIDDEN_BUTTON_WORDS = ("clear", "delete", "rotate", "truncate", "remove", "start", "stop", "restart")
BY_MOUSE = Qt.TextInteractionFlag.TextSelectableByMouse
BY_KEYBOARD = Qt.TextInteractionFlag.TextSelectableByKeyboard


class Recorder:
    """A fake read. It records each call and the thread it ran on, and returns a fixed value or raises.

    ``values`` (a list) is used one item per call, in order, when given. If ``gate`` is set, the
    call waits on it first, so a test can hold a read open.
    """

    def __init__(self, value=None, error=None, gate=None, values=None):
        self.value = value
        self.error = error
        self.gate = gate
        self.values = list(values) if values is not None else None
        self.calls = 0
        self.threads = []

    def __call__(self, *args):
        self.calls += 1
        self.threads.append(threading.current_thread())
        if self.gate is not None:
            self.gate.wait(5)
        if self.error is not None:
            raise self.error
        if self.values is not None:
            return self.values.pop(0)
        return self.value


def _tail_ok(lines, path="tunnel.log"):
    return LogTail(STATUS_OK, Path(path), tuple(lines), "")


def _runner_ok(log_tail):
    return RunnerResult(
        verb="tail-log", ok=True, payload={"scope": "scope-a", "log_tail": log_tail}, exit_code=0
    )


def _page(qtbot, *, tunnel, runner=None, desktop=None, control=None):
    page = LogsPage(
        desktop_log=desktop if desktop is not None else Recorder(_tail_ok([], "desktop.log")),
        tunnel_log=tunnel,
        runner=runner if runner is not None else Recorder(_runner_ok("")),
        control_log=control if control is not None else Recorder(_tail_ok([], "control.log")),
    )
    qtbot.addWidget(page)
    return page


def _press(qtbot, page):
    """Press Refresh and wait until all the reads finish and the button is enabled again."""
    button = page.findChild(QPushButton, "logs-refresh")
    button.click()
    qtbot.waitUntil(lambda: not page.busy and button.isEnabled(), timeout=5000)


def _text(page, name):
    return page.findChild(QPlainTextEdit, name).toPlainText()


def _label(page, name):
    return page.findChild(QLabel, name).text()


def _all_visible_text(page):
    """Every string a user can read on the page: labels, and the text in each pane."""
    chunks = [label.text() for label in page.findChildren(QLabel)]
    chunks += [edit.toPlainText() for edit in page.findChildren(QPlainTextEdit)]
    return "\n".join(chunks)


def _write_log(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def _tree(root):
    """Every file under ``root``: its bytes and modification time, keyed by relative path."""
    found = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            found[path.relative_to(root).as_posix()] = (path.read_bytes(), path.stat().st_mtime_ns)
    return found


def _desktop_env(tmp_path):
    return {"LOCALAPPDATA": str(tmp_path / "local")}


def test_logs_page_is_read_only_and_plain_text(qtbot):
    tunnel = Recorder(_tail_ok(["<b>bold</b> tunnel line"]))
    runner = Recorder(_runner_ok("<i>runner</i> line"))
    desktop = Recorder(_tail_ok(["<u>desktop</u> line"], "desktop.log"))
    page = _page(qtbot, tunnel=tunnel, runner=runner, desktop=desktop)

    buttons = page.findChildren(QPushButton)
    assert [button.objectName() for button in buttons] == ["logs-refresh"]
    assert buttons[0].text() == REFRESH_BUTTON_TEXT
    for word in FORBIDDEN_BUTTON_WORDS:
        assert word not in buttons[0].text().lower()
    for widget_type in WRITE_CONTROLS:
        assert page.findChildren(widget_type) == [], widget_type.__name__
    assert [edit.objectName() for edit in page.findChildren(QLineEdit)] == ["logs-control-search"]
    assert [box.objectName() for box in page.findChildren(QComboBox)] == ["logs-control-launch"]
    for pane in page.findChildren(QPlainTextEdit):
        assert pane.isReadOnly(), pane.objectName()

    _press(qtbot, page)

    assert [button.objectName() for button in page.findChildren(QPushButton)] == ["logs-refresh"]
    assert page.findChildren(QTimer) == []
    for pane in page.findChildren(QPlainTextEdit):
        assert pane.isReadOnly(), pane.objectName()
    # The markup stays as typed. Each pane holds plain text, and no label is read as HTML.
    assert _text(page, "logs-desktop-text") == "<u>desktop</u> line"
    assert _text(page, "logs-tunnel-text") == "<b>bold</b> tunnel line"
    assert _text(page, "logs-runner-text") == "<i>runner</i> line"
    for label in page.findChildren(QLabel):
        assert label.textFormat() == Qt.TextFormat.PlainText, label.objectName()


def test_nothing_is_read_before_the_first_show(qtbot):
    desktop = Recorder(_tail_ok(["line"], "desktop.log"))
    tunnel = Recorder(_tail_ok(["line"]))
    runner = Recorder(_runner_ok("line"))
    control = Recorder(_tail_ok(["line"], "control.log"))
    page = _page(qtbot, tunnel=tunnel, runner=runner, desktop=desktop, control=control)

    qtbot.wait(200)

    assert [read.calls for read in (desktop, tunnel, runner, control)] == [0, 0, 0, 0]
    assert not page.busy
    assert _label(page, "logs-desktop-heading") == "Desktop log: not read yet."
    assert _label(page, "logs-tunnel-heading") == "Tunnel log: not read yet."
    assert _label(page, "logs-runner-heading") == "Runner log (tail-log): not read yet."
    assert _text(page, "logs-desktop-text") == ""
    assert _text(page, "logs-tunnel-text") == ""
    assert _text(page, "logs-runner-text") == ""
    assert _label(page, "logs-intro") == INTRO_UNREAD_TEXT


def test_the_first_show_reads_each_log_once_and_later_shows_read_nothing(qtbot):
    desktop = Recorder(_tail_ok(["desk line"], "desktop.log"))
    tunnel = Recorder(_tail_ok(["tunnel line"]))
    runner = Recorder(_runner_ok("runner line"))
    control = Recorder(_tail_ok(["control line"], "control.log"))
    page = _page(qtbot, tunnel=tunnel, runner=runner, desktop=desktop, control=control)
    reads = (desktop, tunnel, runner, control)

    page.show()
    qtbot.waitUntil(lambda: not page.busy and page.findChild(QPushButton, "logs-refresh").isEnabled(), timeout=5000)

    assert [read.calls for read in reads] == [1, 1, 1, 1]
    assert _text(page, "logs-desktop-text") == "desk line"
    assert _text(page, "logs-tunnel-text") == "tunnel line"
    assert _text(page, "logs-runner-text") == "runner line"
    assert _text(page, "logs-control-text") == "control line"
    assert _label(page, "logs-intro") == INTRO_SHOWN_TEXT

    page.hide()
    page.show()
    qtbot.wait(100)

    assert [read.calls for read in reads] == [1, 1, 1, 1]

    _press(qtbot, page)

    assert [read.calls for read in reads] == [2, 2, 2, 2]


def test_all_three_panes_show_their_bounded_tails_and_the_runner_tail(qtbot, tmp_path):
    env = _desktop_env(tmp_path)
    desktop_path = desktop_log_path(env=env)
    tunnel_path = tmp_path / "tunnel.log"
    # Far more than the default budget, so only the end of each file is read.
    _write_log(desktop_path, "\n".join(f"desk-line-{index:07d} " + "x" * 40 for index in range(4000)) + "\n")
    _write_log(tunnel_path, "\n".join(f"tunnel-line-{index:07d} " + "x" * 40 for index in range(4000)) + "\n")
    page = _page(
        qtbot,
        desktop=lambda: read_desktop_log_tail(env=env),
        tunnel=lambda: read_tunnel_log_tail(tunnel_path),
        runner=Recorder(_runner_ok("runner-one\nrunner-two")),
    )

    _press(qtbot, page)

    desktop_shown = _text(page, "logs-desktop-text")
    assert "desk-line-0003999" in desktop_shown
    assert "desk-line-0000000" not in desktop_shown
    assert len(desktop_shown.encode("utf-8")) <= DEFAULT_MAX_BYTES
    tunnel_shown = _text(page, "logs-tunnel-text")
    assert "tunnel-line-0003999" in tunnel_shown
    assert "tunnel-line-0000000" not in tunnel_shown
    assert len(tunnel_shown.encode("utf-8")) <= DEFAULT_MAX_BYTES
    assert _text(page, "logs-runner-text") == "runner-one\nrunner-two"
    assert _label(page, "logs-desktop-heading") == f"Desktop log: {desktop_path}"
    assert _label(page, "logs-tunnel-heading") == f"Tunnel log: {tunnel_path}"


def test_desktop_pane_shows_its_redacted_tail(qtbot, tmp_path):
    env = _desktop_env(tmp_path)
    path = desktop_log_path(env=env)
    _write_log(
        path,
        f"2026-10-19T10:00:00 INFO meridian_desktop: Meridian Desktop started.\n"
        f"2026-10-19T10:00:01 WARNING meridian_desktop: connect ?token={FAKE_QUERY_TOKEN}\n"
        f"2026-10-19T10:00:02 ERROR meridian_desktop: Bearer {FAKE_BEARER}\n"
        f"2026-10-19T10:00:03 INFO meridian_desktop: password={FAKE_PASSWORD} api_key: '{FAKE_API_KEY}'\n",
    )
    page = _page(qtbot, desktop=lambda: read_desktop_log_tail(env=env), tunnel=Recorder(_tail_ok([])))

    _press(qtbot, page)

    shown = _text(page, "logs-desktop-text")
    assert "Meridian Desktop started." in shown
    assert "token=[redacted]" in shown
    assert "Bearer [redacted]" in shown
    assert "password=[redacted]" in shown
    assert "api_key: [redacted]" in shown
    for secret in SECRETS:
        assert secret not in _all_visible_text(page)
    assert _label(page, "logs-desktop-heading") == f"Desktop log: {path}"


def test_missing_desktop_log_says_which_app_writes_it_and_what_to_do(qtbot, tmp_path):
    env = _desktop_env(tmp_path)
    page = _page(qtbot, desktop=lambda: read_desktop_log_tail(env=env), tunnel=Recorder(_tail_ok([])))

    _press(qtbot, page)

    assert _text(page, "logs-desktop-text") == DESKTOP_MISSING_TEXT
    assert "The Meridian Desktop app writes this file each time it starts." in DESKTOP_MISSING_TEXT
    assert "Close the app, open it again, then press Refresh." in DESKTOP_MISSING_TEXT
    assert not (tmp_path / "local").exists()  # the read created no folder


def test_empty_desktop_log_says_it_is_empty_and_what_to_do(qtbot, tmp_path):
    env = _desktop_env(tmp_path)
    _write_log(desktop_log_path(env=env), "")
    page = _page(qtbot, desktop=lambda: read_desktop_log_tail(env=env), tunnel=Recorder(_tail_ok([])))

    _press(qtbot, page)

    assert _text(page, "logs-desktop-text") == DESKTOP_EMPTY_TEXT


def test_unreadable_desktop_log_shows_fixed_text_without_the_error(qtbot, tmp_path):
    env = _desktop_env(tmp_path)
    desktop_log_path(env=env).mkdir(parents=True)  # a folder cannot be read as a log
    page = _page(qtbot, desktop=lambda: read_desktop_log_tail(env=env), tunnel=Recorder(_tail_ok([])))

    _press(qtbot, page)

    assert _text(page, "logs-desktop-text") == DESKTOP_UNREADABLE_TEXT
    assert str(tmp_path) not in _text(page, "logs-desktop-text")


def test_missing_tunnel_log_names_the_tunnel_and_the_command(qtbot, tmp_path):
    missing = tmp_path / "never-written" / "tunnel.log"
    page = _page(qtbot, tunnel=lambda: read_tunnel_log_tail(missing))

    _press(qtbot, page)

    assert _text(page, "logs-tunnel-text") == TUNNEL_MISSING_TEXT
    assert "The Meridian core's tunnel writes this file while it runs." in TUNNEL_MISSING_TEXT
    assert START_TUNNEL_TEXT in _text(page, "logs-tunnel-text")
    assert "not available" not in _text(page, "logs-tunnel-text").lower()
    assert _label(page, "logs-tunnel-heading") == f"Tunnel log: {missing}"


def test_unreadable_tunnel_log_shows_fixed_text_without_the_error(qtbot, tmp_path):
    # A directory cannot be opened as a file, so the reader reports the log as unreadable.
    page = _page(qtbot, tunnel=lambda: read_tunnel_log_tail(tmp_path))

    _press(qtbot, page)

    assert _text(page, "logs-tunnel-text") == TUNNEL_UNREADABLE_TEXT
    assert str(tmp_path) not in _text(page, "logs-tunnel-text")


def test_empty_tunnel_log_says_it_is_empty_and_what_to_run(qtbot, tmp_path):
    path = tmp_path / "tunnel.log"
    _write_log(path, "")
    page = _page(qtbot, tunnel=lambda: read_tunnel_log_tail(path))

    _press(qtbot, page)

    assert _text(page, "logs-tunnel-text") == EMPTY_TUNNEL_TEXT
    assert START_TUNNEL_TEXT in EMPTY_TUNNEL_TEXT


def test_tunnel_log_secrets_are_redacted_on_screen(qtbot, tmp_path):
    path = tmp_path / "tunnel.log"
    _write_log(
        path,
        f"connect https://example.test/mcp?token={FAKE_QUERY_TOKEN}\n"
        f"Authorization: Bearer {FAKE_BEARER}\n"
        f"key {FAKE_BARE} in use\n",
    )
    page = _page(qtbot, tunnel=lambda: read_tunnel_log_tail(path))

    _press(qtbot, page)

    shown = _all_visible_text(page)
    for secret in SECRETS[:3]:
        assert secret not in shown
    assert "token=[redacted]" in shown
    assert "Bearer [redacted]" in shown
    assert "[redacted] in use" in shown


def test_runner_output_gets_the_same_redaction_as_the_tunnel_log(qtbot):
    runner_text = (
        f"start ?token={FAKE_QUERY_TOKEN}\n"
        f"Bearer {FAKE_BEARER}\n"
        f"{FAKE_RUNNER} is the key\n"
        "plain line\n"
    )
    page = _page(qtbot, tunnel=Recorder(_tail_ok([])), runner=Recorder(_runner_ok(runner_text)))

    _press(qtbot, page)

    shown = _text(page, "logs-runner-text")
    for secret in SECRETS:
        assert secret not in shown
    assert shown == (
        "start ?token=[redacted]\n"
        "Bearer [redacted]\n"
        "[redacted] is the key\n"
        "plain line"
    )


def test_page_redacts_a_source_that_skipped_the_reader(qtbot):
    # A source that does not go through log_tail still reaches the screen redacted.
    page = _page(
        qtbot,
        tunnel=Recorder(_tail_ok([f"raw {FAKE_BARE}"])),
        desktop=Recorder(_tail_ok([f"desk raw {FAKE_BARE}", f"password={FAKE_PASSWORD}"], "desktop.log")),
    )

    _press(qtbot, page)

    assert FAKE_BARE not in _text(page, "logs-tunnel-text")
    assert "raw [redacted]" in _text(page, "logs-tunnel-text")
    assert FAKE_BARE not in _text(page, "logs-desktop-text")
    assert FAKE_PASSWORD not in _text(page, "logs-desktop-text")


def test_runner_not_configured_reads_not_available_and_starts_nothing(qtbot):
    spawned = []

    def spawn(argv, **kwargs):
        spawned.append(argv)
        raise AssertionError("no process should start")

    client = RunnerClient(spawn=spawn)  # no interpreter configured
    page = LogsPage(
        desktop_log=Recorder(_tail_ok([], "desktop.log")),
        tunnel_log=Recorder(_tail_ok([])),
        runner=lambda: client.tail_log("scope-a"),
        control_log=Recorder(_tail_ok([], "control.log")),
    )
    qtbot.addWidget(page)

    _press(qtbot, page)

    assert spawned == []
    assert _text(page, "logs-runner-text") == (
        f"{RUNNER_NOT_AVAILABLE_PREFIX} {NOT_AVAILABLE_MESSAGE} {RUNNER_NOT_CONNECTED_TEXT} {RUNNER_RETRY_TEXT}"
    )
    assert RUNNER_RETRY_TEXT in _text(page, "logs-runner-text")


def test_default_runner_read_is_not_configured(qtbot):
    page = LogsPage(
        desktop_log=Recorder(_tail_ok([], "desktop.log")),
        tunnel_log=Recorder(_tail_ok([])),
        control_log=Recorder(_tail_ok([], "control.log")),
    )
    qtbot.addWidget(page)

    _press(qtbot, page)

    assert _text(page, "logs-runner-text") == runner_log_text(runner_tail_not_configured())
    assert _text(page, "logs-runner-text").startswith(RUNNER_NOT_AVAILABLE_PREFIX)


def test_runner_pane_passes_only_the_tail_log_verb(qtbot):
    spawned = []

    def spawn(argv, **kwargs):
        spawned.append(argv)
        body = json.dumps({"scope": "scope-a", "log_tail": "runner line"}).encode("utf-8")
        return subprocess.CompletedProcess(argv, 0, stdout=body, stderr=b"")

    client = RunnerClient(python="core-python", spawn=spawn)
    page = LogsPage(
        desktop_log=Recorder(_tail_ok([], "desktop.log")),
        tunnel_log=Recorder(_tail_ok([])),
        runner=lambda: client.tail_log("scope-a"),
        control_log=Recorder(_tail_ok([], "control.log")),
    )
    qtbot.addWidget(page)

    _press(qtbot, page)

    assert [argv[3] for argv in spawned] == ["tail-log"]
    assert spawned[0][:3] == ["core-python", "-m", MODULE]
    assert _text(page, "logs-runner-text") == "runner line"


def test_runner_empty_tail_names_the_runner_and_the_start_command(qtbot):
    page = _page(qtbot, tunnel=Recorder(_tail_ok([])), runner=Recorder(_runner_ok("")))

    _press(qtbot, page)

    assert _text(page, "logs-runner-text") == RUNNER_EMPTY_TEXT
    assert "The Meridian core's runner writes this log" in RUNNER_EMPTY_TEXT
    assert START_TUNNEL_TEXT in _text(page, "logs-runner-text")


def test_each_missing_or_empty_source_names_its_writer_and_its_command(qtbot, tmp_path):
    env = _desktop_env(tmp_path)  # no desktop.log is written
    page = _page(
        qtbot,
        desktop=lambda: read_desktop_log_tail(env=env),
        tunnel=lambda: read_tunnel_log_tail(tmp_path / "none" / "tunnel.log"),
        runner=Recorder(_runner_ok("")),
    )

    _press(qtbot, page)

    assert "The Meridian Desktop app writes this file each time it starts." in _text(page, "logs-desktop-text")
    assert START_TUNNEL_TEXT in _text(page, "logs-tunnel-text")
    assert START_TUNNEL_TEXT in _text(page, "logs-runner-text")


def test_runner_reply_without_a_log_tail_reads_not_available(qtbot):
    reply = RunnerResult(verb="tail-log", ok=True, payload={"scope": "scope-a"}, exit_code=0)
    page = _page(qtbot, tunnel=Recorder(_tail_ok([])), runner=Recorder(reply))

    _press(qtbot, page)

    assert _text(page, "logs-runner-text") == (
        f"{RUNNER_NOT_AVAILABLE_PREFIX} {NO_LOG_TAIL_TEXT} {RUNNER_RETRY_TEXT}"
    )


def test_runner_error_text_is_shown_as_the_runner_client_wrote_it(qtbot):
    def spawn(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 3, stdout=b'{"scope": "scope-a"}', stderr=b"")

    client = RunnerClient(python="core-python", spawn=spawn)
    page = LogsPage(
        desktop_log=Recorder(_tail_ok([], "desktop.log")),
        tunnel_log=Recorder(_tail_ok([])),
        runner=lambda: client.tail_log("scope-a"),
        control_log=Recorder(_tail_ok([], "control.log")),
    )
    qtbot.addWidget(page)

    _press(qtbot, page)

    assert _text(page, "logs-runner-text") == (
        f"{RUNNER_NOT_AVAILABLE_PREFIX} The runner command exited with code 3. {RUNNER_RETRY_TEXT}"
    )


def test_runner_log_text_for_a_failed_result_uses_its_fixed_message():
    not_available = RunnerResult(
        verb="tail-log", ok=False, error_code=ERR_NOT_AVAILABLE, error_message="fixed text"
    )
    timed_out = RunnerResult(verb="tail-log", ok=False, error_code=ERR_TIMEOUT, error_message="timed out text")

    assert runner_log_text(not_available) == (
        f"{RUNNER_NOT_AVAILABLE_PREFIX} fixed text {RUNNER_NOT_CONNECTED_TEXT} {RUNNER_RETRY_TEXT}"
    )
    assert runner_log_text(timed_out) == f"{RUNNER_NOT_AVAILABLE_PREFIX} timed out text {RUNNER_RETRY_TEXT}"
    assert RUNNER_NOT_CONNECTED_TEXT not in runner_log_text(timed_out)


def test_refresh_reads_again_on_each_press(qtbot):
    desktop = Recorder(values=[_tail_ok(["desk first"], "desktop.log"), _tail_ok(["desk second"], "desktop.log")])
    tunnel = Recorder(values=[_tail_ok(["first read"]), _tail_ok(["second read"])])
    runner = Recorder(values=[_runner_ok("one"), _runner_ok("two")])
    page = _page(qtbot, tunnel=tunnel, runner=runner, desktop=desktop)

    _press(qtbot, page)
    assert _text(page, "logs-tunnel-text") == "first read"
    assert _text(page, "logs-desktop-text") == "desk first"

    _press(qtbot, page)
    assert _text(page, "logs-tunnel-text") == "second read"
    assert _text(page, "logs-runner-text") == "two"
    assert _text(page, "logs-desktop-text") == "desk second"
    assert tunnel.calls == 2
    assert runner.calls == 2
    assert desktop.calls == 2


def test_reads_run_off_the_ui_thread(qtbot):
    tunnel = Recorder(_tail_ok([]))
    runner = Recorder(_runner_ok(""))
    desktop = Recorder(_tail_ok([], "desktop.log"))
    page = _page(qtbot, tunnel=tunnel, runner=runner, desktop=desktop)

    _press(qtbot, page)

    for read in (desktop, tunnel, runner):
        assert read.threads and all(thread is not threading.main_thread() for thread in read.threads)


def test_refresh_button_is_disabled_while_reading(qtbot):
    gate = threading.Event()
    tunnel = Recorder(_tail_ok([]), gate=gate)
    page = _page(qtbot, tunnel=tunnel)
    button = page.findChild(QPushButton, "logs-refresh")

    button.click()

    assert page.busy
    assert not button.isEnabled()
    gate.set()
    qtbot.waitUntil(lambda: not page.busy and button.isEnabled(), timeout=5000)
    assert tunnel.calls == 1


def test_a_pass_is_not_started_while_another_is_still_running(qtbot):
    gate = threading.Event()
    tunnel = Recorder(_tail_ok(["tunnel line"]), gate=gate)
    page = _page(qtbot, tunnel=tunnel)

    page._start_pass()
    assert page.busy
    page._start_pass()  # refused while the first pass runs, so no second read starts

    gate.set()
    qtbot.waitUntil(lambda: not page.busy, timeout=5000)
    assert tunnel.calls == 1


def test_a_failed_read_shows_its_class_name_and_the_other_panes_still_show(qtbot):
    secret = "SECRET-IN-A-READ-ERROR"
    page = _page(
        qtbot,
        tunnel=Recorder(error=RuntimeError(secret)),
        runner=Recorder(_runner_ok("runner still shows")),
        desktop=Recorder(_tail_ok(["desktop still shows"], "desktop.log")),
    )

    _press(qtbot, page)

    assert _text(page, "logs-tunnel-text") == "The tunnel log check failed unexpectedly (RuntimeError)."
    assert _text(page, "logs-runner-text") == "runner still shows"
    assert _text(page, "logs-desktop-text") == "desktop still shows"
    assert secret not in _all_visible_text(page)


def test_a_failed_desktop_read_does_not_hide_the_other_logs(qtbot):
    secret = "SECRET-DESKTOP-READ-ERROR"
    page = _page(
        qtbot,
        desktop=Recorder(error=OSError(secret)),
        tunnel=Recorder(_tail_ok(["tunnel still shows"])),
        runner=Recorder(_runner_ok("runner still shows")),
    )

    _press(qtbot, page)

    assert _text(page, "logs-desktop-text") == "The desktop log check failed unexpectedly (OSError)."
    assert _text(page, "logs-tunnel-text") == "tunnel still shows"
    assert _text(page, "logs-runner-text") == "runner still shows"
    assert secret not in _all_visible_text(page)


def test_a_failed_runner_read_does_not_hide_the_tunnel_log(qtbot):
    secret = "SECRET-RUNNER-READ-ERROR"
    page = _page(qtbot, tunnel=Recorder(_tail_ok(["tunnel still shows"])), runner=Recorder(error=OSError(secret)))

    _press(qtbot, page)

    assert _text(page, "logs-tunnel-text") == "tunnel still shows"
    assert _text(page, "logs-runner-text") == "The runner log check failed unexpectedly (OSError)."
    assert secret not in _all_visible_text(page)


def test_an_unexpected_failure_of_the_whole_pass_is_reported_without_its_text(qtbot):
    page = _page(qtbot, tunnel=Recorder(_tail_ok([])))

    def broken_pass():
        raise KeyError("SECRET-KEY-TEXT")

    page._reads.collect = broken_pass
    _press(qtbot, page)

    assert _text(page, "logs-tunnel-text") == "The logs check failed unexpectedly (KeyError)."
    assert "SECRET-KEY-TEXT" not in _all_visible_text(page)


def test_refresh_never_changes_the_logs_on_disk(qtbot, tmp_path):
    env = _desktop_env(tmp_path)
    desktop_path = desktop_log_path(env=env)
    _write_log(desktop_path, "desk-alpha\n")
    _write_log(desktop_path.with_name("desktop.log.1"), "desk-older\n")
    tunnel_path = tmp_path / "tunnel.log"
    _write_log(tunnel_path, "alpha\nbeta\n")
    _write_log(tmp_path / "tunnel.log.1", "older\n")
    before = _tree(tmp_path)
    page = _page(
        qtbot,
        desktop=lambda: read_desktop_log_tail(env=env),
        tunnel=lambda: read_tunnel_log_tail(tunnel_path),
    )

    _press(qtbot, page)
    _press(qtbot, page)

    assert _tree(tmp_path) == before
    assert "desk-alpha" in _text(page, "logs-desktop-text")
    assert "alpha" in _text(page, "logs-tunnel-text")


def test_status_values_map_to_their_fixed_text(qtbot):
    """Each status the readers can return has its own visible text, and none shows an exception."""
    cases = [
        (
            LogTail(STATUS_NO_LOG_YET, Path("x"), (), NO_LOG_YET_TEXT),
            "desktop",
            DESKTOP_MISSING_TEXT,
        ),
        (LogTail(STATUS_UNREADABLE, Path("x"), (), UNREADABLE_TEXT), "desktop", DESKTOP_UNREADABLE_TEXT),
        (LogTail(STATUS_OK, Path("x"), (), ""), "desktop", DESKTOP_EMPTY_TEXT),
        (LogTail(STATUS_NO_LOG_YET, Path("x"), (), NO_LOG_YET_TEXT), "tunnel", TUNNEL_MISSING_TEXT),
        (LogTail(STATUS_UNREADABLE, Path("x"), (), UNREADABLE_TEXT), "tunnel", TUNNEL_UNREADABLE_TEXT),
        (LogTail(STATUS_OK, Path("x"), (), ""), "tunnel", EMPTY_TUNNEL_TEXT),
    ]
    for tail, source, expected in cases:
        if source == "desktop":
            page = _page(qtbot, tunnel=Recorder(_tail_ok([])), desktop=Recorder(tail))
            _press(qtbot, page)
            shown = _text(page, "logs-desktop-text")
        else:
            page = _page(qtbot, tunnel=Recorder(tail))
            _press(qtbot, page)
            shown = _text(page, "logs-tunnel-text")
        assert shown == expected
        assert "Traceback" not in shown


def test_every_label_and_pane_can_be_selected(qtbot):
    page = _page(qtbot, tunnel=Recorder(_tail_ok(["x"])))

    _press(qtbot, page)

    assert page.findChildren(QLabel)
    for label in page.findChildren(QLabel):
        flags = label.textInteractionFlags()
        assert flags & BY_MOUSE and flags & BY_KEYBOARD, label.objectName()
    for pane in page.findChildren(QPlainTextEdit):
        flags = pane.textInteractionFlags()
        assert flags & BY_MOUSE and flags & BY_KEYBOARD, pane.objectName()


def test_no_text_on_the_page_says_not_built_yet(qtbot, tmp_path):
    page = _page(qtbot, tunnel=lambda: read_tunnel_log_tail(tmp_path / "none.log"))
    assert "not built yet" not in _all_visible_text(page).lower()

    _press(qtbot, page)

    assert "not built yet" not in _all_visible_text(page).lower()


def test_a_pass_still_running_when_the_page_is_deleted_does_not_keep_the_page(qtbot, monkeypatch):
    """A page deleted while its reads still run is freed on the GUI thread, because the pass holds no page.

    The pass used to hold the page through its bound collect method and its signal emit. When the pool ended the
    pass on a worker, the worker dropped the last reference, so the page could be freed off the GUI thread. The
    collector is off here, so only reference counting can free the page. Nothing reaches the page after it is gone.
    """
    gate = threading.Event()
    shown = []
    monkeypatch.setattr(LogsPage, "_show", lambda self, outcome: shown.append(outcome))
    monkeypatch.setattr(LogsPage, "_show_all_failed", lambda self, failure: shown.append(failure))
    tunnel = Recorder(_tail_ok(["held"]), gate=gate)

    gc.disable()
    try:
        container = QWidget()
        page = LogsPage(
            desktop_log=Recorder(_tail_ok([], "desktop.log")),
            tunnel_log=tunnel,
            runner=Recorder(_runner_ok("")),
            control_log=Recorder(_tail_ok([], "control.log")),
            parent=container,
        )
        page.findChild(QPushButton, "logs-refresh").click()
        qtbot.waitUntil(lambda: tunnel.calls == 1, timeout=5000)  # the pass has started and is held at the gate
        ref = weakref.ref(page)
        shiboken6.delete(container)  # the page's widgets are deleted on the GUI thread while the pass still runs
        del page
        assert ref() is None
        gate.set()
        assert QThreadPool.globalInstance().waitForDone(5000)
        qtbot.wait(50)  # the late result reaches the event loop, and nothing raises
        assert shown == []
    finally:
        gate.set()
        gc.enable()


# ----------------------------------------------------------- a result for another owner, or a superseded pass


def test_a_result_from_another_owner_or_from_a_superseded_pass_is_dropped(qtbot):
    """A sibling page's answer, and an answer from a pass that this page's newer pass superseded, show nothing.

    Two pages each read their own log. The page's first pass emits its Delivered. The sibling's first pass then emits one
    at the same generation, and it reaches the page too, so the page drops it by owner alone. The page reads again, and
    its first Delivered is replayed: it carries the page's own owner with a superseded generation, so it is dropped by
    generation alone. The page's second log lines are what the user sees throughout, and they are the control that the
    page's own current Delivered is accepted.
    """
    tunnel = Recorder(values=[_tail_ok(["first pass line"]), _tail_ok(["second pass line"])])
    page = _page(qtbot, tunnel=tunnel)
    sibling = _page(qtbot, tunnel=Recorder(values=[_tail_ok(["sibling pass line"])]))
    with recording_deliveries() as emitted:
        _press(qtbot, page)
        assert len(emitted) == 1
        own_first = emitted[0]
        shown_first = visible_state(page)
        _press(qtbot, sibling)
        assert len(emitted) == 2
        assert visible_state(page) == shown_first
        _press(qtbot, page)
        shown_second = visible_state(page)
        assert shown_second != shown_first
        emit(own_first)  # this page's answer from the first pass, which the second pass superseded
        qtbot.wait(20)
    assert visible_state(page) == shown_second
