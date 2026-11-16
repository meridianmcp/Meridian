"""Tests for the Services tab (meridian_desktop/ui/services_page.py), with pytest-qt.

The checks are fakes here, except where a test says otherwise. The runner test runs the real
RunnerClient against a fake spawn. The slot port test runs the real probe_slot_ports against a
fake connect check. The local server and LaTeX engine tests that look at refused and answered
requests run the real probes against a fake opener (local_client._opener), so no socket opens and
no request leaves this process. Nothing starts a process or reads the keychain. The settings file
is a temp file. Offscreen Qt, from conftest.
"""

import gc
import itertools
import subprocess
import threading
import urllib.error
import urllib.parse
import weakref
from datetime import datetime, timedelta

import pytest
import shiboken6
from PySide6.QtCore import Qt, QThreadPool, QTimer
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QCheckBox,
    QComboBox,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QTableWidget,
    QToolButton,
    QWidget,
)

from meridian_desktop import latex_client, local_client
from meridian_desktop.latex_client import (
    ENGINE_CONNECTED,
    ENGINE_ERROR,
    ENGINE_NOT_ANSWERING,
    ENGINE_NOT_RUNNING,
    EngineStatus,
)
from meridian_desktop.runner_client import ERR_NOT_AVAILABLE, MUTATING_VERBS, MODULE, RunnerClient, RunnerResult
from meridian_desktop.settings_store import Settings, SettingsStore
from meridian_desktop.slot_ports import PER_PORT_TIMEOUT, PROBE_HOST, SLOT_PORTS, SlotPortResult, probe_slot_ports
from meridian_desktop.ui import services_page
from meridian_desktop.ui.services_page import (
    CHECK_BUTTON_TEXT,
    ENGINE_TITLE,
    LOCAL_TITLE,
    NOT_CHECKED,
    RUNNER_TITLE,
    STATE_COULD_NOT_CHECK,
    STATE_NOT_MERIDIAN,
    STATE_NOT_RUNNING,
    STATE_RUNNING,
    UNCHECKED_TEXT,
    HealthFacts,
    ServicesPage,
    engine_row,
    local_server_row,
    runner_row,
)

LOOPBACK_URL = "http://127.0.0.1:7999"
from _shared_delivery import emit, recording_deliveries, visible_state

LOCAL_PORT = "7878"  # the default Local URL
ENGINE_PORT = "8471"  # the default engine address
FIXED_NOW = datetime(2026, 10, 19, 14, 3, 22)
# Every input control except the one check button, which the button test checks by name.
WRITE_CONTROLS = (
    QToolButton,
    QCheckBox,
    QRadioButton,
    QLineEdit,
    QComboBox,
    QAbstractSpinBox,
)
ENGINE_UP = EngineStatus(ENGINE_CONNECTED, latex_client.CONNECTED_MESSAGE, engine_version="1.4.0", extension_version="0.9.2")


class Recorder:
    """A fake check. It records each call and the thread it ran on, and returns a fixed value or raises.

    If ``gate`` is set, the call waits on it first, so a test can hold a check open.
    """

    def __init__(self, value=None, error=None, gate=None):
        self.value = value
        self.error = error
        self.gate = gate
        self.calls = []
        self.threads = []

    def __call__(self, *args):
        self.calls.append(args)
        self.threads.append(threading.current_thread())
        if self.gate is not None:
            self.gate.wait(5)
        if self.error is not None:
            raise self.error
        return self.value


class _FakeReply:
    """What the fake opener hands back: a status and a body, read once, like a real response."""

    def __init__(self, status, body):
        self.status = status
        self.headers = {}
        self._body = body
        self._sent = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read1(self, _size):
        if self._sent:
            return b""
        self._sent = True
        return self._body


class _FakeOpener:
    """Stands in for the loopback opener. Each request is recorded by URL.

    ``refuse`` makes every request fail the way a closed loopback port does. ``routes`` maps a
    path to (status, body), or to an exception that is raised. Other paths answer 404.
    """

    def __init__(self, routes=None, refuse=False):
        self.routes = routes or {}
        self.refuse = refuse
        self.urls = []

    def open(self, request, timeout=None):
        self.urls.append(request.full_url)
        if self.refuse:
            raise urllib.error.URLError(ConnectionRefusedError(111, "connection refused"))
        path = urllib.parse.urlsplit(request.full_url).path
        answer = self.routes.get(path, (404, b"{}"))
        if isinstance(answer, BaseException):
            raise answer
        status, body = answer
        return _FakeReply(status, body)


def _runner_ok(state="running"):
    return RunnerResult(verb="status", ok=True, payload={"scope": "scope-a", "child": {"state": state}}, exit_code=0)


def _ports(listening=("fs", "code")):
    return [SlotPortResult(slot, port, slot in listening) for slot, port in SLOT_PORTS]


def _ticking_clock(start=FIXED_NOW):
    """A clock that moves one second on each call, so each check gets its own time."""
    ticks = itertools.count()
    return lambda: start + timedelta(seconds=next(ticks))


def _page(qtbot, settings_path, *, health=None, engine=None, runner=None, ports=None, clock=None):
    page = ServicesPage(
        SettingsStore(settings_path),
        health=health if health is not None else Recorder(HealthFacts(state="running")),
        engine=engine if engine is not None else Recorder(ENGINE_UP),
        runner=runner if runner is not None else Recorder(_runner_ok()),
        ports=ports if ports is not None else Recorder(_ports()),
        clock=clock if clock is not None else (lambda: FIXED_NOW),
    )
    qtbot.addWidget(page)
    return page


def _press(qtbot, page):
    """Press Check services and wait until the checks finish and the button is enabled again."""
    button = page.findChild(QPushButton, "services-check")
    button.click()
    qtbot.waitUntil(lambda: not page.busy and button.isEnabled(), timeout=5000)


def _text(page, name):
    return page.findChild(QLabel, name).text()


def _all_label_text(page):
    return "\n".join(label.text() for label in page.findChildren(QLabel))


# ----------------------------------------------------------- the row wording, without Qt


@pytest.mark.parametrize(
    "probe_state, state, seen",
    [
        (local_client.HEALTH_RUNNING, STATE_RUNNING,
         "Port 7878 is listening, and Meridian answered its health check."),
        (local_client.HEALTH_NOT_RUNNING, STATE_NOT_RUNNING,
         "Port 7878 is not listening, so no local server is running there."),
        (local_client.HEALTH_NOT_MERIDIAN, STATE_NOT_MERIDIAN,
         "Port 7878 is listening, but the answer is not Meridian's health check."),
        (local_client.HEALTH_ERROR, STATE_COULD_NOT_CHECK,
         "Port 7878 could not be checked."),
    ],
)
def test_local_server_row_wording_for_each_probe_state(probe_state, state, seen):
    row = local_server_row("http://127.0.0.1:7878", HealthFacts(state=probe_state))

    assert row.title == LOCAL_TITLE
    assert row.state == state
    assert row.port == LOCAL_PORT
    assert row.seen == seen
    assert row.how_to_start  # every state has one plain sentence on how to start it


def test_local_server_not_listening_says_so_and_how_to_start():
    row = local_server_row("http://127.0.0.1:7878", HealthFacts(state=local_client.HEALTH_NOT_RUNNING))

    assert "not listening" in row.seen
    assert row.how_to_start == "start Meridian on this computer, then press Check services."


def test_local_server_row_without_facts_says_it_could_not_check():
    row = local_server_row("http://127.0.0.1:7878", None, "The local server health check failed unexpectedly (ValueError).")

    assert row.state == STATE_COULD_NOT_CHECK
    assert row.seen == "The local server health check failed unexpectedly (ValueError)."


def test_local_server_version_is_shown_only_for_the_parts_reported():
    reported = local_server_row("http://127.0.0.1:7878", HealthFacts("running", "1.4.2", "a555660ef312"))
    unreported = local_server_row("http://127.0.0.1:7878", HealthFacts("running"))

    assert reported.version == "Engine version: 1.4.2. Git SHA: a555660ef312."
    assert unreported.version == ""


@pytest.mark.parametrize(
    "status, state, seen",
    [
        (ENGINE_UP, STATE_RUNNING,
         "Port 8471 is listening, and the engine answered both its health and extension-version checks."),
        (EngineStatus(ENGINE_NOT_RUNNING, latex_client.NOT_RUNNING_MESSAGE), STATE_NOT_RUNNING,
         "Port 8471 is not listening, so no LaTeX engine is running there."),
        (EngineStatus(ENGINE_NOT_ANSWERING, latex_client.NOT_ANSWERING_MESSAGE), STATE_NOT_MERIDIAN,
         "Port 8471 is listening, but the answer is not the LaTeX engine."),
        (EngineStatus(ENGINE_ERROR, latex_client.TIMEOUT_MESSAGE), STATE_COULD_NOT_CHECK,
         latex_client.TIMEOUT_MESSAGE),
    ],
)
def test_engine_row_wording_for_each_probe_state(status, state, seen):
    row = engine_row("http://127.0.0.1:8471", status)

    assert row.title == ENGINE_TITLE
    assert row.state == state
    assert row.port == ENGINE_PORT
    assert row.seen == seen
    assert row.how_to_start


def test_engine_could_not_check_with_a_loopback_error_asks_for_a_loopback_address():
    status = EngineStatus(ENGINE_ERROR, latex_client.NOT_LOOPBACK_MESSAGE)

    row = engine_row("http://example.com:8471", status)

    assert row.state == STATE_COULD_NOT_CHECK
    assert row.seen == latex_client.NOT_LOOPBACK_MESSAGE
    assert "127.0.0.1" in row.how_to_start
    assert "example.com" not in row.how_to_start and "example.com" not in row.seen


@pytest.mark.parametrize(
    "result, state, seen",
    [
        (_runner_ok("running"), STATE_RUNNING, "The runner reports its child process as running."),
        (_runner_ok("stopped"), STATE_NOT_RUNNING, "The runner reports its child process as stopped."),
        (_runner_ok("Running"), STATE_COULD_NOT_CHECK,
         "The runner reports its child state as Running, which the desktop does not read."),
        (RunnerResult(verb="status", ok=True, payload={"scope": "s"}, exit_code=0), STATE_COULD_NOT_CHECK,
         "The runner reply did not include a child state."),
        (RunnerResult(verb="status", ok=False, error_code=ERR_NOT_AVAILABLE, error_message="x"), STATE_COULD_NOT_CHECK,
         services_page.RUNNER_NOT_CONFIGURED),
        (RunnerResult(verb="status", ok=False, error_code="nonzero_exit", error_message="The runner command exited with code 3.",
                      exit_code=3), STATE_COULD_NOT_CHECK, "The runner command exited with code 3."),
    ],
)
def test_runner_row_wording_for_each_result(result, state, seen):
    row = runner_row(result)

    assert row.title == RUNNER_TITLE
    assert row.state == state
    assert row.seen == seen
    assert row.port == services_page.RUNNER_PORT


def test_runner_never_reads_as_running_unless_its_child_says_so():
    """Contract rule 7: a stopped, unknown or missing state is never displayed as running."""
    for child in ("starting", "unknown", "RUNNING", "x" * 200):
        row = runner_row(_runner_ok(child))
        assert row.state != STATE_RUNNING, child


def test_runner_not_available_says_what_is_missing_and_what_to_do():
    row = runner_row(services_page.runner_not_configured())

    assert row.seen == services_page.RUNNER_NOT_CONFIGURED
    assert "no setting" in row.seen
    assert "python -m meridian.local_runner" in row.how_to_start


# ----------------------------------------------------------- the page, with fakes


def test_services_tab_shows_one_row_per_service(qtbot, settings_path):
    SettingsStore(settings_path).save(Settings(local_url=LOOPBACK_URL))
    health = Recorder(HealthFacts(state="running", engine_version="1.4.2", git_sha="a555660ef312"))
    page = _page(qtbot, settings_path, health=health, runner=Recorder(_runner_ok("running")), ports=Recorder(_ports()))

    _press(qtbot, page)

    assert _text(page, "services-health") == "Local server health: running"
    assert _text(page, "services-health-port") == "Port checked: 7999"
    assert _text(page, "services-health-seen") == "Port 7999 is listening, and Meridian answered its health check."
    assert _text(page, "services-health-version") == "Engine version: 1.4.2. Git SHA: a555660ef312."
    assert page.findChild(QLabel, "services-health-version").isHidden() is False
    assert _text(page, "services-health-checked") == "Last checked: 2026-10-19 14:03:22"
    assert _text(page, "services-health-start") == "How to start: it is already running, so there is nothing to start."

    assert _text(page, "services-engine") == "LaTeX engine: running"
    assert _text(page, "services-engine-port") == f"Port checked: {ENGINE_PORT}"
    assert _text(page, "services-engine-checked") == "Last checked: 2026-10-19 14:03:22"

    assert _text(page, "services-runner") == "Runner: running"
    assert _text(page, "services-runner-port") == f"Port checked: {services_page.RUNNER_PORT}"
    assert _text(page, "services-runner-seen") == "The runner reports its child process as running."

    table = page.findChild(QTableWidget, "services-ports-table")
    assert table.rowCount() == 15
    rows = [[table.item(row, column).text() for column in range(3)] for row in range(table.rowCount())]
    assert rows[0] == ["fs", "8808", "listening"]
    assert rows[1] == ["code", "8809", "listening"]
    assert rows[2] == ["extract", "8810", "not listening"]
    assert [row[1] for row in rows] == [str(port) for _slot, port in SLOT_PORTS]


@pytest.mark.parametrize("state", ["running", "not_running", "not_meridian", "error"])
def test_local_server_state_is_shown_in_words(qtbot, settings_path, state):
    SettingsStore(settings_path).save(Settings(local_url=LOOPBACK_URL))
    page = _page(qtbot, settings_path, health=Recorder(HealthFacts(state=state)))

    _press(qtbot, page)

    expected = {
        "running": "running",
        "not_running": "not running",
        "not_meridian": "not Meridian",
        "error": "could not check",
    }[state]
    assert _text(page, "services-health") == f"Local server health: {expected}"
    assert "not_running" not in _all_label_text(page) and "not_meridian" not in _all_label_text(page)


def test_checks_use_the_settings_addresses(qtbot, settings_path):
    SettingsStore(settings_path).save(Settings(local_url=LOOPBACK_URL, engine_url="http://127.0.0.1:9999"))
    health = Recorder(HealthFacts(state="running"))
    engine = Recorder(ENGINE_UP)
    page = _page(qtbot, settings_path, health=health, engine=engine)

    _press(qtbot, page)

    assert health.calls == [(LOOPBACK_URL, services_page.HEALTH_TIMEOUT)]
    assert engine.calls == [("http://127.0.0.1:9999", latex_client.PROBE_TIMEOUT)]
    assert _text(page, "services-engine-port") == "Port checked: 9999"


def test_each_row_shows_the_time_its_own_check_finished(qtbot, settings_path):
    page = _page(qtbot, settings_path, clock=_ticking_clock())

    _press(qtbot, page)

    assert _text(page, "services-health-checked") == "Last checked: 2026-10-19 14:03:22"
    assert _text(page, "services-engine-checked") == "Last checked: 2026-10-19 14:03:23"
    assert _text(page, "services-runner-checked") == "Last checked: 2026-10-19 14:03:24"


def test_unchecked_rows_say_so_and_still_say_how_to_start(qtbot, settings_path):
    page = _page(qtbot, settings_path)

    assert _text(page, "services-intro") == UNCHECKED_TEXT
    assert _text(page, "services-health") == "Local server health: not checked yet"
    assert _text(page, "services-engine") == "LaTeX engine: not checked yet"
    assert _text(page, "services-runner") == "Runner: not checked yet"
    assert _text(page, "services-health-checked") == f"Last checked: {NOT_CHECKED}"
    assert _text(page, "services-engine-start").startswith("How to start: start the LaTeX engine")
    assert _text(page, "services-runner-start").startswith("How to start: run python -m meridian.local_runner start")
    assert page.findChild(QLabel, "services-health-version").isHidden()


def test_not_listening_comes_from_a_refused_socket_for_both_addresses(qtbot, settings_path, monkeypatch):
    """Real probes, a fake opener that refuses every connection. No socket opens."""
    opener = _FakeOpener(refuse=True)
    monkeypatch.setattr(local_client, "_opener", lambda: opener)
    page = ServicesPage(
        SettingsStore(settings_path),
        runner=Recorder(_runner_ok()),
        ports=Recorder(_ports()),
        clock=lambda: FIXED_NOW,
    )
    qtbot.addWidget(page)

    _press(qtbot, page)

    assert opener.urls == [
        f"http://127.0.0.1:{LOCAL_PORT}/health",
        f"http://127.0.0.1:{ENGINE_PORT}/health",
    ]
    assert _text(page, "services-health") == "Local server health: not running"
    assert _text(page, "services-health-seen") == "Port 7878 is not listening, so no local server is running there."
    assert _text(page, "services-engine") == "LaTeX engine: not running"
    assert _text(page, "services-engine-seen") == "Port 8471 is not listening, so no LaTeX engine is running there."
    assert _text(page, "services-engine-start") == (
        "How to start: start the LaTeX engine you installed on this computer so it listens on port 8471, "
        "then press Check services."
    )


def test_http_answers_decide_running_and_not_meridian(qtbot, settings_path, monkeypatch):
    """Real probes against fake HTTP answers.

    The local server's /health body is Meridian's, so that row is running. Both addresses answer
    /health with a JSON object, but the engine's /extension-version is missing, so the port that
    answers is not the LaTeX engine, and that row reads not Meridian.
    """
    meridian_health = b'{"service": "meridian", "status": "ok"}'
    opener = _FakeOpener(routes={"/health": (200, meridian_health)})
    monkeypatch.setattr(local_client, "_opener", lambda: opener)
    page = ServicesPage(SettingsStore(settings_path), runner=Recorder(_runner_ok()), ports=Recorder(_ports()), clock=lambda: FIXED_NOW)
    qtbot.addWidget(page)

    _press(qtbot, page)

    assert _text(page, "services-health") == "Local server health: running"
    assert _text(page, "services-engine") == "LaTeX engine: not Meridian"
    assert _text(page, "services-engine-seen") == "Port 8471 is listening, but the answer is not the LaTeX engine."


def test_a_timed_out_probe_reads_could_not_check(qtbot, settings_path, monkeypatch):
    opener = _FakeOpener(routes={"/health": TimeoutError("slow")})
    monkeypatch.setattr(local_client, "_opener", lambda: opener)
    page = ServicesPage(SettingsStore(settings_path), runner=Recorder(_runner_ok()), ports=Recorder(_ports()), clock=lambda: FIXED_NOW)
    qtbot.addWidget(page)

    _press(qtbot, page)

    assert _text(page, "services-health") == "Local server health: could not check"
    assert _text(page, "services-health-seen") == "Port 7878 could not be checked."
    assert _text(page, "services-engine") == "LaTeX engine: could not check"
    assert _text(page, "services-engine-seen") == latex_client.TIMEOUT_MESSAGE


def test_default_health_check_sends_nothing_to_a_non_loopback_address(monkeypatch):
    def no_request(*args, **kwargs):
        raise AssertionError("a request was opened")

    monkeypatch.setattr(local_client, "_opener", no_request)

    facts = services_page.probe_health_facts("http://example.com:7878")

    assert facts == HealthFacts(state="error")


def test_services_page_has_no_mutating_controls(qtbot, settings_path):
    page = _page(qtbot, settings_path)

    buttons = page.findChildren(QPushButton)
    assert [button.objectName() for button in buttons] == ["services-check"]
    assert buttons[0].text() == CHECK_BUTTON_TEXT
    for verb in MUTATING_VERBS:
        assert verb not in buttons[0].text().lower()
    for widget_type in WRITE_CONTROLS:
        assert page.findChildren(widget_type) == [], widget_type.__name__

    _press(qtbot, page)

    assert [button.objectName() for button in page.findChildren(QPushButton)] == ["services-check"]
    assert page.findChildren(QTimer) == []


def test_checks_run_only_when_the_button_is_pressed(qtbot, settings_path):
    health = Recorder(HealthFacts(state="running"))
    engine = Recorder(ENGINE_UP)
    runner = Recorder(_runner_ok())
    ports = Recorder(_ports())
    page = _page(qtbot, settings_path, health=health, engine=engine, runner=runner, ports=ports)

    page.show()
    qtbot.waitExposed(page)
    qtbot.wait(200)  # give any work started on show a chance to run

    assert health.calls == []
    assert engine.calls == []
    assert runner.calls == []
    assert ports.calls == []
    assert _text(page, "services-health").endswith("not checked yet")

    _press(qtbot, page)

    assert len(health.calls) == 1
    assert len(engine.calls) == 1
    assert len(runner.calls) == 1
    assert len(ports.calls) == 1


def test_checks_run_off_the_ui_thread(qtbot, settings_path):
    health = Recorder(HealthFacts(state="running"))
    engine = Recorder(ENGINE_UP)
    runner = Recorder(_runner_ok())
    ports = Recorder(_ports())
    page = _page(qtbot, settings_path, health=health, engine=engine, runner=runner, ports=ports)

    _press(qtbot, page)

    for check in (health, engine, runner, ports):
        assert check.threads and all(thread is not threading.main_thread() for thread in check.threads)


def test_button_is_disabled_while_checks_run(qtbot, settings_path):
    gate = threading.Event()
    health = Recorder(HealthFacts(state="running"), gate=gate)
    page = _page(qtbot, settings_path, health=health)
    button = page.findChild(QPushButton, "services-check")

    button.click()

    assert page.busy
    assert not button.isEnabled()
    gate.set()
    qtbot.waitUntil(lambda: not page.busy and button.isEnabled(), timeout=5000)
    assert len(health.calls) == 1


def test_one_failed_check_does_not_hide_the_others(qtbot, settings_path):
    secret = "SECRET-IN-AN-ERROR-MESSAGE"
    page = _page(
        qtbot,
        settings_path,
        health=Recorder(HealthFacts(state="running")),
        runner=Recorder(error=RuntimeError(secret)),
        ports=Recorder(_ports()),
    )

    _press(qtbot, page)

    assert _text(page, "services-runner") == "Runner: could not check"
    assert _text(page, "services-runner-seen") == "The runner check failed unexpectedly (RuntimeError)."
    assert _text(page, "services-health") == "Local server health: running"
    assert page.findChild(QTableWidget, "services-ports-table").rowCount() == 15
    assert secret not in _all_label_text(page)


def test_runner_check_passes_only_the_status_verb(qtbot, settings_path):
    spawned = []

    def spawn(argv, **kwargs):
        spawned.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=b'{"scope": "scope-a", "child": {"state": "running"}}', stderr=b"")

    client = RunnerClient(python="core-python", spawn=spawn)
    page = _page(qtbot, settings_path, runner=lambda: client.status("scope-a"))

    _press(qtbot, page)

    assert [argv[3] for argv in spawned] == ["status"]
    assert spawned[0][:3] == ["core-python", "-m", MODULE]
    assert _text(page, "services-runner") == "Runner: running"


def test_runner_check_starts_nothing_without_an_interpreter(qtbot, settings_path):
    spawned = []

    def spawn(argv, **kwargs):
        spawned.append(argv)
        raise AssertionError("no process should start")

    client = RunnerClient(spawn=spawn)  # no interpreter configured
    page = _page(qtbot, settings_path, runner=lambda: client.status("scope-a"))

    _press(qtbot, page)

    assert spawned == []
    assert _text(page, "services-runner") == "Runner: could not check"
    assert _text(page, "services-runner-seen") == services_page.RUNNER_NOT_CONFIGURED


def test_runner_reads_not_available_when_no_interpreter_is_configured(qtbot, settings_path):
    page = ServicesPage(
        SettingsStore(settings_path),
        health=Recorder(HealthFacts(state="running")),
        engine=Recorder(ENGINE_UP),
        ports=Recorder(_ports()),
        clock=lambda: FIXED_NOW,
    )
    qtbot.addWidget(page)

    _press(qtbot, page)

    assert _text(page, "services-runner") == "Runner: could not check"
    assert _text(page, "services-runner-start").startswith("How to start: in a terminal in the Meridian core folder")


def test_runner_error_text_is_shown_as_the_runner_client_wrote_it(qtbot, settings_path):
    def spawn(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 3, stdout=b'{"scope": "scope-a"}', stderr=b"")

    client = RunnerClient(python="core-python", spawn=spawn)
    page = _page(qtbot, settings_path, runner=lambda: client.status("scope-a"))

    _press(qtbot, page)

    assert _text(page, "services-runner") == "Runner: could not check"
    assert _text(page, "services-runner-seen") == "The runner command exited with code 3."


def test_slot_ports_come_from_the_real_probe_with_a_fake_connect(qtbot, settings_path):
    calls = []

    def fake_connect(host, port, timeout):
        calls.append((host, port, timeout))
        return port == 8820  # outputs

    page = _page(qtbot, settings_path, ports=lambda: probe_slot_ports(check=fake_connect))

    _press(qtbot, page)

    assert calls == [(PROBE_HOST, port, PER_PORT_TIMEOUT) for _slot, port in SLOT_PORTS]
    table = page.findChild(QTableWidget, "services-ports-table")
    states = {table.item(row, 0).text(): table.item(row, 2).text() for row in range(table.rowCount())}
    assert states["outputs"] == "listening"
    assert states["fs"] == "not listening"


def test_labels_are_plain_text(qtbot, settings_path):
    page = _page(
        qtbot,
        settings_path,
        health=Recorder(HealthFacts(state="running", engine_version="<b>1.4.2</b>")),
    )

    _press(qtbot, page)

    version = page.findChild(QLabel, "services-health-version")
    assert version.text() == "Engine version: <b>1.4.2</b>."
    for label in page.findChildren(QLabel):
        assert label.textFormat() == Qt.TextFormat.PlainText


def test_every_label_is_selectable(qtbot, settings_path):
    page = _page(qtbot, settings_path)

    for label in page.findChildren(QLabel):
        assert label.textInteractionFlags() & Qt.TextInteractionFlag.TextSelectableByMouse, label.objectName()


def test_no_row_shows_not_built_yet(qtbot, settings_path):
    page = _page(qtbot, settings_path)
    _press(qtbot, page)

    assert "Not built yet" not in _all_label_text(page)


FAILURE_CASES = [
    ("health", "services-health", "Local server health: could not check", "services-health-seen",
     "The local server health check failed unexpectedly (ValueError)."),
    ("engine", "services-engine", "LaTeX engine: could not check", "services-engine-seen",
     "The LaTeX engine check failed unexpectedly (ValueError)."),
    ("runner", "services-runner", "Runner: could not check", "services-runner-seen",
     "The runner check failed unexpectedly (ValueError)."),
    ("ports", "services-ports-status", None, None,
     "The slot port check failed unexpectedly (ValueError)."),
]


@pytest.mark.parametrize(("failing", "label", "state_text", "seen_name", "expected"), FAILURE_CASES)
def test_each_failed_check_shows_its_own_failure_and_the_others_still_show(
    qtbot, settings_path, failing, label, state_text, seen_name, expected
):
    secret = f"SECRET-IN-{failing}-MESSAGE"
    checks = {
        "health": Recorder(HealthFacts(state="running")),
        "engine": Recorder(ENGINE_UP),
        "runner": Recorder(_runner_ok()),
        "ports": Recorder(_ports()),
    }
    checks[failing] = Recorder(error=ValueError(secret))
    page = _page(
        qtbot,
        settings_path,
        health=checks["health"],
        engine=checks["engine"],
        runner=checks["runner"],
        ports=checks["ports"],
    )

    _press(qtbot, page)

    if seen_name is not None:
        assert _text(page, label) == state_text
        assert _text(page, seen_name) == expected
    else:
        assert _text(page, label) == expected
    assert secret not in _all_label_text(page)
    if failing != "health":
        assert _text(page, "services-health") == "Local server health: running"
    if failing != "engine":
        assert _text(page, "services-engine") == "LaTeX engine: running"
    if failing != "runner":
        assert _text(page, "services-runner") == "Runner: running"
    if failing != "ports":
        assert page.findChild(QTableWidget, "services-ports-table").rowCount() == 15


def test_an_unexpected_failure_of_the_whole_pass_is_reported_without_its_text(qtbot, settings_path):
    page = _page(qtbot, settings_path)

    def broken_pass():
        raise KeyError("SECRET-KEY-TEXT")

    page._checks.collect = broken_pass
    _press(qtbot, page)

    assert _text(page, "services-health") == "Local server health: could not check"
    assert _text(page, "services-health-seen") == "The services check failed unexpectedly (KeyError)."
    assert "SECRET-KEY-TEXT" not in _all_label_text(page)
    assert page.findChild(QTableWidget, "services-ports-table").rowCount() == 0


def test_a_runner_reply_without_a_child_state_reads_could_not_check(qtbot, settings_path):
    reply = RunnerResult(verb="status", ok=True, payload={"scope": "scope-a"}, exit_code=0)
    page = _page(qtbot, settings_path, runner=Recorder(reply))

    _press(qtbot, page)

    assert _text(page, "services-runner") == "Runner: could not check"
    assert _text(page, "services-runner-seen") == "The runner reply did not include a child state."


def test_a_pass_still_running_when_the_page_is_deleted_does_not_keep_the_page(qtbot, settings_path, monkeypatch):
    """A page deleted while its checks still run is freed on the GUI thread, because the pass holds no page.

    The pass used to hold the page through its bound collect method and its signal emit. When the pool ended the
    pass on a worker, the worker dropped the last reference, so the page could be freed off the GUI thread. The
    collector is off here, so only reference counting can free the page. Nothing reaches the page after it is gone.
    """
    gate = threading.Event()
    shown = []
    monkeypatch.setattr(ServicesPage, "_show", lambda self, outcome: shown.append(outcome))
    monkeypatch.setattr(ServicesPage, "_show_all_failed", lambda self, failure: shown.append(failure))
    health = Recorder(HealthFacts(state="running"), gate=gate)

    gc.disable()
    try:
        container = QWidget()
        page = ServicesPage(
            SettingsStore(settings_path),
            health=health,
            engine=Recorder(ENGINE_UP),
            runner=Recorder(_runner_ok()),
            ports=Recorder(_ports()),
            clock=lambda: FIXED_NOW,
            parent=container,
        )
        page.findChild(QPushButton, "services-check").click()
        qtbot.waitUntil(lambda: bool(health.calls), timeout=5000)  # the pass has started and is held at the gate
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


def test_a_result_from_another_owner_or_from_a_superseded_pass_is_dropped(qtbot, settings_path):
    """A sibling page's answer, and an answer from a pass that this page's newer pass superseded, show nothing.

    Two pages each keep their own checks. The page's first pass emits its Delivered. The sibling's first pass then emits
    one at the same generation, and it reaches the page too, so the page drops it by owner alone. The page checks again
    with the local server down, and its first Delivered is replayed: it carries the page's own owner with a superseded
    generation, so it is dropped by generation alone. The page's second rows are what the user sees throughout, and they
    are the control that the page's own current Delivered is accepted.
    """
    health = Recorder(HealthFacts(state="running"))
    page = _page(qtbot, settings_path, health=health)
    sibling = _page(qtbot, settings_path, health=Recorder(HealthFacts(state=local_client.HEALTH_NOT_RUNNING)))
    with recording_deliveries() as emitted:
        _press(qtbot, page)
        assert len(emitted) == 1
        own_first = emitted[0]
        shown_first = visible_state(page)
        _press(qtbot, sibling)
        assert len(emitted) == 2
        assert visible_state(page) == shown_first
        health.value = HealthFacts(state=local_client.HEALTH_NOT_RUNNING)
        _press(qtbot, page)
        shown_second = visible_state(page)
        assert shown_second != shown_first
        emit(own_first)  # this page's answer from the first pass, which the second pass superseded
        qtbot.wait(20)
    assert visible_state(page) == shown_second
