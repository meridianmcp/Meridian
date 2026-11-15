"""Services tab: a read-only look at the local server, the LaTeX engine, the runner and the slot ports.

This is Qt code and sits in ui/, so the boundary check allows it. The page makes no request,
starts no timer and polls nothing when it is built or shown. It checks only when Check services
is pressed, and the checks run on the thread pool, so the UI thread never blocks. The four checks:

- the local server health: one GET of /health at the local_url setting (local_client.probe_health);
- the LaTeX engine: GET /health, then GET /extension-version, at the engine_url setting
  (latex_client.probe_engine);
- the runner status: one read-only runner call (runner_client passes only status and tail-log,
  never a mutating verb);
- the slot ports: a connect-only listening check on 127.0.0.1 (slot_ports.probe_slot_ports).

The first three each get one row: the state (running, not running, not Meridian, or could not
check), the port the check uses, what the check saw, when it last ran, and one sentence on how to
start it. The rows only report. They never start, stop or restart anything.

The page has one button, and it runs those checks. It has no start, stop, restart, preflight,
doctor or run-script control. Every label is plain text (PlainText labels and plain table cells),
so a value from a probe is never read as HTML.

The runner check reads not available in this build, because the desktop has no setting for the
core's interpreter or runner scope yet. Its row says what is missing, and starts nothing.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from PySide6.QtCore import QRunnable, Qt, QThreadPool, Slot
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from meridian_desktop import latex_client, local_client
from meridian_desktop.latex_client import PROBE_TIMEOUT, EngineStatus
from meridian_desktop.runner_client import ERR_NOT_AVAILABLE, NOT_AVAILABLE_MESSAGE, RunnerResult
from meridian_desktop.settings_store import SettingsStore
from meridian_desktop.slot_ports import PROBE_HOST, SlotPortResult, probe_slot_ports
from meridian_desktop.ui.delivery import Delivered, Delivery, shared_delivery
from meridian_desktop.ui.text import enable_table_copy, make_selectable

HEALTH_TIMEOUT = 2.0
NOT_CHECKED = "not checked yet"
CHECK_BUTTON_TEXT = "Check services"
CHECKING_TEXT = "checking..."
UNCHECKED_TEXT = (
    "Press Check services to look at the local server, the LaTeX engine, the runner and the slot "
    "ports. Nothing is checked until then, and nothing here starts or stops a service."
)
FAILED_TEXT = "The {} check failed unexpectedly ({})."
NO_RESULT_TEXT = "The check gave no result."
PORTS_NOTE = "A listening port only shows that something accepts a connection. It does not show that Meridian is healthy."
PORT_COLUMNS = ("Slot", "Port", "Listening state")
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

# The four state words a row can show. A check that could not get an answer is "could not check".
STATE_RUNNING = "running"
STATE_NOT_RUNNING = "not running"
STATE_NOT_MERIDIAN = "not Meridian"
STATE_COULD_NOT_CHECK = "could not check"

LOCAL_TITLE = "Local server health"
ENGINE_TITLE = "LaTeX engine"
RUNNER_TITLE = "Runner"
FAILURE_LABELS = {
    "health": "local server health",
    "engine": "LaTeX engine",
    "runner": "runner",
    "ports": "slot port",
}

# Local server. The port comes from the Local URL setting.
LOCAL_SEEN = {
    STATE_RUNNING: "Port {port} is listening, and Meridian answered its health check.",
    STATE_NOT_RUNNING: "Port {port} is not listening, so no local server is running there.",
    STATE_NOT_MERIDIAN: "Port {port} is listening, but the answer is not Meridian's health check.",
    STATE_COULD_NOT_CHECK: "Port {port} could not be checked.",
}
LOCAL_START = {
    STATE_RUNNING: "it is already running, so there is nothing to start.",
    STATE_NOT_RUNNING: "start Meridian on this computer, then press Check services.",
    STATE_NOT_MERIDIAN: "stop the other program on port {port}, or change the Local URL in Settings, then press Check services.",
    STATE_COULD_NOT_CHECK: "check that Meridian is running on this computer, then press Check services.",
}
LOCAL_START_UNCHECKED = "start Meridian on this computer if it is not running, then press Check services."

# LaTeX engine. The port comes from the engine address in the desktop settings.
ENGINE_SEEN = {
    STATE_RUNNING: "Port {port} is listening, and the engine answered both its health and extension-version checks.",
    STATE_NOT_RUNNING: "Port {port} is not listening, so no LaTeX engine is running there.",
    STATE_NOT_MERIDIAN: "Port {port} is listening, but the answer is not the LaTeX engine.",
}
ENGINE_START = {
    STATE_RUNNING: "it is already running, so there is nothing to start.",
    STATE_NOT_RUNNING: "start the LaTeX engine you installed on this computer so it listens on port {port}, then press Check services.",
    STATE_NOT_MERIDIAN: "stop the other program on port {port}, then start the LaTeX engine you installed and press Check services.",
    STATE_COULD_NOT_CHECK: "check that the LaTeX engine you installed is running on this computer, then press Check services.",
}
ENGINE_START_LOOPBACK = (
    "change the engine address in the desktop settings to one on this computer, "
    "such as http://127.0.0.1:8471, then press Check services."
)
ENGINE_START_UNCHECKED = "start the LaTeX engine you installed on this computer if it is not running, then press Check services."

# Runner. The runner check runs one status command and names no port.
RUNNER_PORT = "none (the runner check runs a status command)"
RUNNER_NOT_CONFIGURED = (
    "The desktop has no setting for the core's Python or runner scope yet, so it cannot check the runner."
)
RUNNER_NO_CHILD_STATE = "The runner reply did not include a child state."
RUNNER_CHILD_MAX_LENGTH = 40
RUNNER_STOPPED_STATES = ("stopped", "not_running", "exited")
RUNNER_START = {
    STATE_RUNNING: "it is already running, so there is nothing to start.",
    STATE_NOT_RUNNING: "run python -m meridian.local_runner start --scope <your scope> from a terminal in the Meridian core folder.",
    STATE_COULD_NOT_CHECK: "run python -m meridian.local_runner status --scope <your scope> from a terminal in the Meridian core folder to see what is wrong.",
}
RUNNER_START_UNAVAILABLE = (
    "in a terminal in the Meridian core folder, run python -m meridian.local_runner status --scope <your scope> "
    "to see it, or start --scope <your scope> to start it."
)
RUNNER_START_UNCHECKED = (
    "run python -m meridian.local_runner start --scope <your scope> from a terminal in the Meridian core folder "
    "if it is not running, then press Check services."
)

_LOCAL_STATES = {
    local_client.HEALTH_RUNNING: STATE_RUNNING,
    local_client.HEALTH_NOT_RUNNING: STATE_NOT_RUNNING,
    local_client.HEALTH_NOT_MERIDIAN: STATE_NOT_MERIDIAN,
}
_ENGINE_STATES = {
    latex_client.ENGINE_CONNECTED: STATE_RUNNING,
    latex_client.ENGINE_NOT_RUNNING: STATE_NOT_RUNNING,
    latex_client.ENGINE_NOT_ANSWERING: STATE_NOT_MERIDIAN,
}


@dataclass(frozen=True)
class HealthFacts:
    """The health check's result. The version and git SHA are None when the probe does not report them."""

    state: str
    engine_version: str | None = None
    git_sha: str | None = None


@dataclass(frozen=True)
class ServicesOutcome:
    """One pass of the checks. A check that raised is None here, and its class name is in ``failures``.

    ``checked_at`` holds, per check key, the local time the check finished, as text.
    """

    local_url: str
    engine_url: str = ""
    health: HealthFacts | None = None
    engine: EngineStatus | None = None
    runner: RunnerResult | None = None
    ports: tuple[SlotPortResult, ...] | None = None
    failures: dict[str, str] = field(default_factory=dict)
    checked_at: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ServiceRow:
    """One row of the Services page. Every field is plain text, shown as it is.

    ``version`` is empty unless the probe reported a version, and then the page shows it.
    """

    title: str
    state: str
    port: str
    seen: str
    how_to_start: str
    version: str = ""


def probe_health_facts(base_url: str, timeout: float = HEALTH_TIMEOUT) -> HealthFacts:
    """The local server's state from local_client.probe_health.

    That probe returns the state only. It does not read the version or git SHA from the /health
    body, so both stay None until the health probe reports them.
    """
    return HealthFacts(state=local_client.probe_health(base_url, timeout))


def probe_engine_status(base_url: str, timeout: float = PROBE_TIMEOUT) -> EngineStatus:
    """The LaTeX engine's state from latex_client.probe_engine. Read at call time, so a test can replace it."""
    return latex_client.probe_engine(base_url, timeout)


def runner_not_configured() -> RunnerResult:
    """The runner check for this build. The desktop sets no core interpreter or runner scope,
    so no runner command is started. The result is the runner client's own not_available result."""
    return RunnerResult(
        verb="status", ok=False, error_code=ERR_NOT_AVAILABLE, error_message=NOT_AVAILABLE_MESSAGE
    )


def _could_not_check(title: str, port: str, seen: str, how_to_start: str) -> ServiceRow:
    return ServiceRow(title, STATE_COULD_NOT_CHECK, port, seen, how_to_start)


def _local_version(facts: HealthFacts) -> str:
    """The version and SHA, only for the parts the probe reported. Empty when it reported neither."""
    parts = []
    if facts.engine_version:
        parts.append(f"Engine version: {facts.engine_version}.")
    if facts.git_sha:
        parts.append(f"Git SHA: {facts.git_sha}.")
    return " ".join(parts)


def local_server_row(base_url: str, facts: HealthFacts | None, failure: str = "") -> ServiceRow:
    """The local server row, from one health probe result. ``failure`` is the sentence used when there is none."""
    port = latex_client.port_text(base_url)
    if facts is None:
        return _could_not_check(LOCAL_TITLE, port, failure or NO_RESULT_TEXT, LOCAL_START[STATE_COULD_NOT_CHECK])
    state = _LOCAL_STATES.get(facts.state, STATE_COULD_NOT_CHECK)
    if state == STATE_COULD_NOT_CHECK:
        return _could_not_check(
            LOCAL_TITLE, port, LOCAL_SEEN[state].format(port=port), LOCAL_START[state].format(port=port)
        )
    return ServiceRow(
        LOCAL_TITLE,
        state,
        port,
        LOCAL_SEEN[state].format(port=port),
        LOCAL_START[state].format(port=port),
        version=_local_version(facts),
    )


def engine_row(base_url: str, status: EngineStatus | None, failure: str = "") -> ServiceRow:
    """The LaTeX engine row, from one probe result. A could-not-check row shows the probe's own fixed text."""
    port = latex_client.port_text(base_url)
    if status is None:
        return _could_not_check(ENGINE_TITLE, port, failure or NO_RESULT_TEXT, ENGINE_START[STATE_COULD_NOT_CHECK])
    state = _ENGINE_STATES.get(status.state)
    if state is None:
        start = (
            ENGINE_START_LOOPBACK
            if status.message == latex_client.NOT_LOOPBACK_MESSAGE
            else ENGINE_START[STATE_COULD_NOT_CHECK]
        )
        return _could_not_check(ENGINE_TITLE, port, status.message or NO_RESULT_TEXT, start)
    return ServiceRow(
        ENGINE_TITLE,
        state,
        port,
        ENGINE_SEEN[state].format(port=port),
        ENGINE_START[state].format(port=port),
    )


def _child_state(result: RunnerResult) -> str | None:
    child = (result.payload or {}).get("child")
    state = child.get("state") if isinstance(child, dict) else None
    return state if isinstance(state, str) and state else None


def runner_row(result: RunnerResult | None, failure: str = "") -> ServiceRow:
    """The runner row, from one runner status call. Only a child state of running reads as running."""
    if result is None:
        return _could_not_check(RUNNER_TITLE, RUNNER_PORT, failure or NO_RESULT_TEXT, RUNNER_START[STATE_COULD_NOT_CHECK])
    if not result.ok:
        if result.error_code == ERR_NOT_AVAILABLE:
            return _could_not_check(RUNNER_TITLE, RUNNER_PORT, RUNNER_NOT_CONFIGURED, RUNNER_START_UNAVAILABLE)
        seen = result.error_message or NO_RESULT_TEXT
        return _could_not_check(RUNNER_TITLE, RUNNER_PORT, seen, RUNNER_START[STATE_COULD_NOT_CHECK])
    child_state = _child_state(result)
    if child_state is None:
        return _could_not_check(RUNNER_TITLE, RUNNER_PORT, RUNNER_NO_CHILD_STATE, RUNNER_START[STATE_COULD_NOT_CHECK])
    if child_state == "running":
        seen = "The runner reports its child process as running."
        return ServiceRow(RUNNER_TITLE, STATE_RUNNING, RUNNER_PORT, seen, RUNNER_START[STATE_RUNNING])
    if child_state in RUNNER_STOPPED_STATES:
        seen = f"The runner reports its child process as {child_state}."
        return ServiceRow(RUNNER_TITLE, STATE_NOT_RUNNING, RUNNER_PORT, seen, RUNNER_START[STATE_NOT_RUNNING])
    # A state the desktop does not read is never shown as running (local-trust contract, rule 7).
    shown = child_state if len(child_state) <= RUNNER_CHILD_MAX_LENGTH else "an unexpected state"
    seen = f"The runner reports its child state as {shown}, which the desktop does not read."
    return _could_not_check(RUNNER_TITLE, RUNNER_PORT, seen, RUNNER_START[STATE_COULD_NOT_CHECK])


def _failure_sentence(key: str, failures: dict[str, str]) -> str:
    return FAILED_TEXT.format(FAILURE_LABELS[key], failures.get(key, "NoResult"))


def _stamp(clock: Callable[[], datetime]) -> str:
    return clock().strftime(TIME_FORMAT)


def _guarded(
    name: str,
    failures: dict[str, str],
    stamps: dict[str, str],
    clock: Callable[[], datetime],
    work: Callable[[], Any],
) -> Any:
    """Run one check, record when it finished, and return its value. If it raises, record only its
    class name, and return None so the other checks still run."""
    try:
        value = work()
    except Exception as exc:  # noqa: BLE001 - one failed check must not hide the others
        failures[name] = exc.__class__.__name__
        value = None
    stamps[name] = _stamp(clock)
    return value


def _label(parent: QWidget, name: str, text: str) -> QLabel:
    label = QLabel(text, parent)
    label.setObjectName(name)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return make_selectable(label)


@dataclass(frozen=True)
class _Finished:
    """One finished pass, as its job hands it back. ``failure`` is the class name of an exception that got past the guards."""

    generation: int
    outcome: ServicesOutcome | None
    failure: str | None


class _Checks:
    """The checks the Services page runs on the worker thread, through the settings store and the injected checks.

    It holds no widget. A pass still running when the page is deleted therefore never keeps the page alive, and never
    touches it. The page holds one of these and hands its ``collect`` method to the thread pool.
    """

    def __init__(
        self,
        settings_store: SettingsStore,
        health: Callable[[str, float], HealthFacts],
        engine: Callable[[str, float], EngineStatus],
        runner: Callable[[], RunnerResult],
        ports: Callable[[], Sequence[SlotPortResult]],
        clock: Callable[[], datetime],
    ) -> None:
        self._settings = settings_store
        self._health = health
        self._engine = engine
        self._runner = runner
        self._ports = ports
        self._clock = clock

    def collect(self) -> ServicesOutcome:
        """Run the checks in order. Runs on a worker thread, and never raises for one failed check."""
        settings = self._settings.load().settings
        local_url, engine_url = settings.local_url, settings.engine_url
        failures: dict[str, str] = {}
        stamps: dict[str, str] = {}

        def run(name: str, work: Callable[[], Any]) -> Any:
            return _guarded(name, failures, stamps, self._clock, work)

        health = run("health", lambda: self._health(local_url, HEALTH_TIMEOUT))
        engine = run("engine", lambda: self._engine(engine_url, PROBE_TIMEOUT))
        runner = run("runner", self._runner)
        ports_list = run("ports", lambda: tuple(self._ports()))
        return ServicesOutcome(
            local_url=local_url,
            engine_url=engine_url,
            health=health,
            engine=engine,
            runner=runner,
            ports=ports_list,
            failures=failures,
            checked_at=stamps,
        )


class _ChecksJob(QRunnable):
    """Run one pass of the checks on the thread pool and deliver a _Finished to the shared delivery object.

    The job holds only the work, the owner token, the generation and the shared delivery object. It never holds the
    page. Any exception that gets past the per-check guards is reduced to its class name here, so a raw exception
    never reaches the UI thread.
    """

    def __init__(self, generation: int, work: Callable[[], ServicesOutcome], owner: object, delivery: Delivery) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._generation = generation
        self._work = work
        self._owner = owner
        self._delivery = delivery

    def run(self) -> None:
        outcome: ServicesOutcome | None = None
        failure: str | None = None
        try:
            outcome = self._work()
        except Exception as exc:  # noqa: BLE001 - a worker must never raise into the pool
            failure = exc.__class__.__name__
        self._delivery.delivered.emit(Delivered(self._owner, _Finished(self._generation, outcome, failure)))


@dataclass(frozen=True)
class _RowLabels:
    """The labels that show one row on the page."""

    title: str
    state: QLabel
    port: QLabel
    seen: QLabel
    checked: QLabel
    how_to_start: QLabel
    version: QLabel


class ServicesPage(QWidget):
    """The Services tab: one button, read-only checks of the local server, the LaTeX engine, the
    runner and the slot ports, and their results.

    The checks are injected. The app passes the defaults above. Tests pass fakes, and a clock.
    """

    def __init__(
        self,
        settings_store: SettingsStore,
        *,
        health: Callable[[str, float], HealthFacts] = probe_health_facts,
        engine: Callable[[str, float], EngineStatus] = probe_engine_status,
        runner: Callable[[], RunnerResult] = runner_not_configured,
        ports: Callable[[], Sequence[SlotPortResult]] = probe_slot_ports,
        clock: Callable[[], datetime] = datetime.now,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("services-page")
        self._checks = _Checks(settings_store, health, engine, runner, ports, clock)
        self._busy = False
        # Identifies this page's passes. A plain object, so a job can hold it without holding the page.
        self._owner = object()
        self._generation = 0
        shared_delivery().delivered.connect(self._on_delivered)
        self._build()

    @property
    def busy(self) -> bool:
        return self._busy

    # ----------------------------------------------------------- layout

    def _build(self) -> None:
        layout = QVBoxLayout(self)

        self._intro = _label(self, "services-intro", UNCHECKED_TEXT)
        layout.addWidget(self._intro)

        self._local = self._row(layout, "services-health", LOCAL_TITLE, LOCAL_START_UNCHECKED)
        self._engine_row = self._row(layout, "services-engine", ENGINE_TITLE, ENGINE_START_UNCHECKED)
        self._runner_row = self._row(layout, "services-runner", RUNNER_TITLE, RUNNER_START_UNCHECKED)

        self._ports_status = _label(self, "services-ports-status", f"Slot ports: {NOT_CHECKED}")
        layout.addWidget(self._ports_status)

        self._table = QTableWidget(0, len(PORT_COLUMNS), self)
        self._table.setObjectName("services-ports-table")
        self._table.setHorizontalHeaderLabels(list(PORT_COLUMNS))
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setStretchLastSection(True)
        enable_table_copy(self._table)
        layout.addWidget(self._table, 1)

        self._check_button = QPushButton(CHECK_BUTTON_TEXT, self)
        self._check_button.setObjectName("services-check")
        layout.addWidget(self._check_button)

        self._check_button.clicked.connect(self._on_check_clicked)

    def _row(self, layout: QVBoxLayout, prefix: str, title: str, unchecked_start: str) -> _RowLabels:
        frame = QFrame(self)
        frame.setObjectName(f"{prefix}-row")
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        block = QVBoxLayout(frame)
        labels = _RowLabels(
            title=title,
            state=_label(frame, prefix, f"{title}: {NOT_CHECKED}"),
            port=_label(frame, f"{prefix}-port", f"Port checked: {NOT_CHECKED}"),
            seen=_label(frame, f"{prefix}-seen", ""),
            checked=_label(frame, f"{prefix}-checked", f"Last checked: {NOT_CHECKED}"),
            how_to_start=_label(frame, f"{prefix}-start", f"How to start: {unchecked_start}"),
            version=_label(frame, f"{prefix}-version", ""),
        )
        labels.version.setVisible(False)
        for label in (labels.state, labels.port, labels.seen, labels.version, labels.checked, labels.how_to_start):
            block.addWidget(label)
        layout.addWidget(frame)
        return labels

    def _row_labels(self) -> tuple[_RowLabels, ...]:
        return (self._local, self._engine_row, self._runner_row)

    # ----------------------------------------------------------- checks

    @Slot()
    def _on_check_clicked(self) -> None:
        if self._busy:
            return
        self._busy = True
        self._generation += 1
        generation = self._generation
        self._check_button.setEnabled(False)
        self._show_checking()
        # The job holds the checks, the owner token, the generation and the shared delivery object. It never holds the page.
        QThreadPool.globalInstance().start(_ChecksJob(generation, self._checks.collect, self._owner, shared_delivery()))

    @Slot(object)
    def _on_delivered(self, item: object) -> None:
        # Runs on the GUI thread. A result for another page is dropped here, and so is one from an earlier pass.
        if not isinstance(item, Delivered) or item.owner is not self._owner:
            return
        finished = item.value
        if not isinstance(finished, _Finished) or finished.generation != self._generation:
            return
        self._busy = False
        self._check_button.setEnabled(True)
        if finished.failure is not None or not isinstance(finished.outcome, ServicesOutcome):
            self._show_all_failed(finished.failure or "NoResult")
            return
        self._show(finished.outcome)

    # ----------------------------------------------------------- display

    def _show_row(self, labels: _RowLabels, row: ServiceRow, checked_at: str | None) -> None:
        labels.state.setText(f"{labels.title}: {row.state}")
        labels.port.setText(f"Port checked: {row.port}")
        labels.seen.setText(row.seen)
        labels.version.setText(row.version)
        labels.version.setVisible(bool(row.version))
        labels.checked.setText(f"Last checked: {checked_at or NOT_CHECKED}")
        labels.how_to_start.setText(f"How to start: {row.how_to_start}")

    def _show_checking(self) -> None:
        for labels in self._row_labels():
            labels.state.setText(f"{labels.title}: {CHECKING_TEXT}")
            labels.seen.setText("")
            labels.version.setVisible(False)
        self._ports_status.setText(f"Slot ports: {CHECKING_TEXT}")
        self._table.setRowCount(0)

    def _show_all_failed(self, failure: str) -> None:
        text = FAILED_TEXT.format("services", failure)
        for labels in self._row_labels():
            labels.state.setText(f"{labels.title}: {STATE_COULD_NOT_CHECK}")
            labels.seen.setText(text)
            labels.version.setVisible(False)
        self._ports_status.setText(text)
        self._table.setRowCount(0)

    def _show(self, outcome: ServicesOutcome) -> None:
        failures = outcome.failures
        stamps = outcome.checked_at
        local = local_server_row(outcome.local_url, outcome.health, _failure_sentence("health", failures))
        engine = engine_row(outcome.engine_url, outcome.engine, _failure_sentence("engine", failures))
        runner = runner_row(outcome.runner, _failure_sentence("runner", failures))
        self._show_row(self._local, local, stamps.get("health"))
        self._show_row(self._engine_row, engine, stamps.get("engine"))
        self._show_row(self._runner_row, runner, stamps.get("runner"))
        self._show_ports(outcome, failures, stamps.get("ports"))

    def _show_ports(self, outcome: ServicesOutcome, failures: dict[str, str], checked_at: str | None) -> None:
        if outcome.ports is None:
            self._ports_status.setText(_failure_sentence("ports", failures))
            self._table.setRowCount(0)
            return
        self._table.setRowCount(len(outcome.ports))
        for row, result in enumerate(outcome.ports):
            for column, text in enumerate((result.slot, str(result.port), result.label)):
                self._table.setItem(row, column, QTableWidgetItem(text))
        when = f" Last checked {checked_at}." if checked_at else ""
        self._ports_status.setText(
            f"{len(outcome.ports)} slot ports checked on {PROBE_HOST}.{when} {PORTS_NOTE}"
        )


__all__ = [
    "CHECK_BUTTON_TEXT",
    "ENGINE_TITLE",
    "HEALTH_TIMEOUT",
    "LOCAL_TITLE",
    "NOT_CHECKED",
    "RUNNER_TITLE",
    "STATE_COULD_NOT_CHECK",
    "STATE_NOT_MERIDIAN",
    "STATE_NOT_RUNNING",
    "STATE_RUNNING",
    "UNCHECKED_TEXT",
    "HealthFacts",
    "ServiceRow",
    "ServicesOutcome",
    "ServicesPage",
    "engine_row",
    "local_server_row",
    "probe_engine_status",
    "probe_health_facts",
    "runner_not_configured",
    "runner_row",
]
