"""Logs view under Settings, Diagnostics: a read-only look at the desktop, control, tunnel and runner logs.

This is Qt code and sits in ui/, so the boundary check allows it. The page reads nothing when it
is built. It reads once, on its first show (the Diagnostics group shows it), and after that it reads
only when Refresh is pressed. It starts no timer and polls nothing. The reads run on the thread pool,
so the UI thread never blocks. The reads are:

- the desktop log: the bounded, redacted tail that desktop_log.read_desktop_log_tail returns;
- the control log: the bounded, redacted tail that runner_controls.read_control_log_tail returns, one line per Start or Stop.
  Its lines are grouped by launch of the local server (log_launches.py). A launch picker and a search over the shown
  lines only redraw the text already read, so they read nothing;
- the tunnel log: the bounded, redacted tail that log_tail.read_tunnel_log_tail returns;
- the runner log: the tail-log reply of the runner client. runner_client passes only the read
  verbs, so no mutating verb is ever run from this page.

Every line of every pane goes through desktop_log.redact_text, so no source can show a raw secret.

The page has one button, Refresh. It has no clear, delete, rotate or truncate control, and it never
writes to any log. Every pane is read-only plain text (QPlainTextEdit with setPlainText), and every
label is PlainText, so a log line is never read as HTML. Labels and panes can be selected to copy.

When a log is missing or empty, its pane says which process writes it and what to run to produce
it. The desktop has no setting for the core's interpreter or runner scope, so the runner pane says
the desktop cannot read the runner from here, and it says to press Refresh again or check Settings. It starts
no process.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from PySide6.QtCore import QRunnable, Qt, QThreadPool, Slot
from PySide6.QtGui import QShowEvent
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget

from meridian_desktop.desktop_log import read_desktop_log_tail, redact_text
from meridian_desktop.log_launches import GroupedLog, group_by_launch, launch_view
from meridian_desktop.log_tail import (
    STATUS_NO_LOG_YET,
    STATUS_OK,
    LogTail,
    read_tunnel_log_tail,
)
from meridian_desktop.runner_client import ERR_NOT_AVAILABLE, NOT_AVAILABLE_MESSAGE, RunnerResult
from meridian_desktop.runner_controls import read_control_log_tail
from meridian_desktop.ui.delivery import Delivered, Delivery, shared_delivery
from meridian_desktop.ui.text import make_selectable

REFRESH_BUTTON_TEXT = "Refresh"
INTRO_UNREAD_TEXT = "Nothing is read until this view is first shown. Then each log is read once. Press Refresh to read them again."
INTRO_READING_TEXT = "Reading the logs..."
INTRO_SHOWN_TEXT = "Each log is shown read-only, with secret-shaped values redacted. Press Refresh to read them again."
DESKTOP_HEADING = "Desktop log:"
CONTROL_HEADING = "Control log:"
TUNNEL_HEADING = "Tunnel log:"
RUNNER_HEADING = "Runner log (tail-log):"
READING_TEXT = "reading..."
FAILED_TEXT = "The {} check failed unexpectedly ({})."
NO_RESULT = "NoResult"

# Nothing here yet. Each pane shows this one plain line while it holds no text.
NOTHING_HERE_TEXT = "Nothing here yet."
# The owner starts the tunnel from the Tunnel panel in Settings (Start asks first). No terminal command is typed here.
START_TUNNEL_TEXT = "Start the tunnel from the Tunnel panel in Settings, then press Refresh here."
# The launch picker's first entry, which shows every launch of the local server at once.
ALL_LAUNCHES_TEXT = "All launches"
CONTROL_SEARCH_PLACEHOLDER = "Search the shown lines"
# The control log gets one line for each Start or Stop of the local server or the tunnel.
START_CONTROL_TEXT = (
    "Press Start or Stop in Local server and startup, or in the Tunnel panel in Settings, then press Refresh here."
)

DESKTOP_MISSING_TEXT = (
    "No log yet. The Meridian Desktop app writes this file each time it starts. "
    "Close the app, open it again, then press Refresh."
)
DESKTOP_EMPTY_TEXT = (
    "The desktop log is empty. The Meridian Desktop app writes to it each time it starts. "
    "Close the app, open it again, then press Refresh."
)
DESKTOP_UNREADABLE_TEXT = "The desktop log exists but could not be read."
TUNNEL_MISSING_TEXT = f"No log yet. The Meridian core's tunnel writes this file while it runs. {START_TUNNEL_TEXT}"
EMPTY_TUNNEL_TEXT = f"The tunnel log is empty. The Meridian core's tunnel writes to it while it runs. {START_TUNNEL_TEXT}"
TUNNEL_UNREADABLE_TEXT = "The tunnel log exists but could not be read."
CONTROL_MISSING_TEXT = (
    f"No log yet. The desktop adds one line here for each Start or Stop, and for each refused entry in the keys file. "
    f"{START_CONTROL_TEXT}"
)
CONTROL_EMPTY_TEXT = (
    f"The control log is empty. The desktop adds one line here for each Start or Stop, and for each refused entry in the "
    f"keys file. {START_CONTROL_TEXT}"
)
CONTROL_UNREADABLE_TEXT = "The control log exists but could not be read."
RUNNER_EMPTY_TEXT = f"No log yet. The Meridian core's runner writes this log when it starts the tunnel. {START_TUNNEL_TEXT}"
RUNNER_NOT_AVAILABLE_PREFIX = "Runner log not available."
RUNNER_NOT_CONNECTED_TEXT = (
    "The desktop has no setting for the Meridian core's Python or runner scope, so it cannot read the runner log."
)
RUNNER_RETRY_TEXT = "Press Refresh to read it again. If it keeps failing, check the Meridian core folder and Python in Settings."
NO_LOG_TAIL_TEXT = "The runner reply did not include a log tail."


@dataclass(frozen=True)
class LogsOutcome:
    """One pass of the three reads. A read that raised is None here, and its class name is in ``failures``."""

    tunnel: LogTail | None = None
    runner: RunnerResult | None = None
    failures: dict[str, str] = field(default_factory=dict)
    desktop: LogTail | None = None
    control: LogTail | None = None


@dataclass(frozen=True)
class LogText:
    """The text one file pane shows when its log is missing, empty, or cannot be read."""

    missing: str
    empty: str
    unreadable: str


DESKTOP_TEXT = LogText(DESKTOP_MISSING_TEXT, DESKTOP_EMPTY_TEXT, DESKTOP_UNREADABLE_TEXT)
TUNNEL_TEXT = LogText(TUNNEL_MISSING_TEXT, EMPTY_TUNNEL_TEXT, TUNNEL_UNREADABLE_TEXT)
CONTROL_TEXT = LogText(CONTROL_MISSING_TEXT, CONTROL_EMPTY_TEXT, CONTROL_UNREADABLE_TEXT)


def runner_tail_not_configured() -> RunnerResult:
    """The runner read for this build. The desktop sets no core interpreter or runner scope,
    so no runner command is started. The result is the runner client's own not_available result."""
    return RunnerResult(
        verb="tail-log", ok=False, error_code=ERR_NOT_AVAILABLE, error_message=NOT_AVAILABLE_MESSAGE
    )


def _lines(text: str) -> list[str]:
    """The lines of ``text``, with line endings removed. A final newline does not add an empty line."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return [line[:-1] if line.endswith("\r") else line for line in lines]


def runner_log_text(result: RunnerResult) -> str:
    """The runner's log tail as the pane shows it, or the reason it is not available.

    Each line goes through redact_text, the same pass every other pane gets. An empty tail is the
    runner's "no log yet". A failed read or a reply with no text tail says why, and gives the
    retry text.
    """
    if not result.ok:
        parts = [RUNNER_NOT_AVAILABLE_PREFIX, result.error_message]
        if result.error_code == ERR_NOT_AVAILABLE:
            parts.append(RUNNER_NOT_CONNECTED_TEXT)
        parts.append(RUNNER_RETRY_TEXT)
        return " ".join(parts)
    tail = (result.payload or {}).get("log_tail")
    if not isinstance(tail, str):
        return f"{RUNNER_NOT_AVAILABLE_PREFIX} {NO_LOG_TAIL_TEXT} {RUNNER_RETRY_TEXT}"
    if not tail:
        return RUNNER_EMPTY_TEXT
    return "\n".join(redact_text(line) for line in _lines(tail))


def log_pane_text(tail: LogTail, texts: LogText) -> str:
    """A file log's pane text: its redacted lines, newest first, or the fixed text for its missing, empty or unreadable state.

    The file is in time order, oldest first, so the lines are reversed here. The most recent line is at the top.
    """
    if tail.status == STATUS_OK:
        if not tail.lines:
            return texts.empty
        return "\n".join(redact_text(line) for line in reversed(tail.lines))
    if tail.status == STATUS_NO_LOG_YET:
        return texts.missing
    return texts.unreadable


def _guarded(name: str, failures: dict[str, str], work: Callable[[], Any]) -> Any:
    """Run one read. If it raises, record only its class name, and return None so the other reads still run."""
    try:
        return work()
    except Exception as exc:  # noqa: BLE001 - one failed read must not hide the others
        failures[name] = exc.__class__.__name__
        return None


def _heading(parent: QWidget, name: str, text: str) -> QLabel:
    label = QLabel(text, parent)
    label.setObjectName(name)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return make_selectable(label)


def _pane(parent: QWidget, name: str) -> QPlainTextEdit:
    pane = QPlainTextEdit(parent)
    pane.setPlaceholderText(NOTHING_HERE_TEXT)
    pane.setObjectName(name)
    pane.setReadOnly(True)
    make_selectable(pane)  # after setReadOnly, which otherwise allows mouse selection only
    pane.setUndoRedoEnabled(False)
    return pane


@dataclass(frozen=True)
class _Finished:
    """One finished pass, as its job hands it back. ``failure`` is the class name of an exception that got past the guards."""

    generation: int
    outcome: LogsOutcome | None
    failure: str | None


class _Reads:
    """The three reads the Logs tab runs on the worker thread, through the readers it was given.

    It holds no widget. A pass still running when the page is deleted therefore never keeps the page alive, and never
    touches it. The page holds one of these and hands its ``collect`` method to the thread pool.
    """

    def __init__(
        self,
        desktop_log: Callable[[], LogTail],
        tunnel_log: Callable[[], LogTail],
        runner: Callable[[], RunnerResult],
        control_log: Callable[[], LogTail],
    ) -> None:
        self._desktop_log = desktop_log
        self._tunnel_log = tunnel_log
        self._runner = runner
        self._control_log = control_log

    def collect(self) -> LogsOutcome:
        """Run the reads in order. Runs on a worker thread, and never raises for one failed read."""
        failures: dict[str, str] = {}
        desktop = _guarded("desktop", failures, self._desktop_log)
        control = _guarded("control", failures, self._control_log)
        tunnel = _guarded("tunnel", failures, self._tunnel_log)
        runner = _guarded("runner", failures, self._runner)
        return LogsOutcome(tunnel=tunnel, runner=runner, failures=failures, desktop=desktop, control=control)


class _ReadJob(QRunnable):
    """Run one pass of the reads on the thread pool and deliver a _Finished to the shared delivery object.

    The job holds only the work, the owner token, the generation and the shared delivery object. It never holds the
    page. Any exception that gets past the per-read guards is reduced to its class name here, so a raw exception
    never reaches the UI thread.
    """

    def __init__(self, generation: int, work: Callable[[], LogsOutcome], owner: object, delivery: Delivery) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._generation = generation
        self._work = work
        self._owner = owner
        self._delivery = delivery

    def run(self) -> None:
        outcome: LogsOutcome | None = None
        failure: str | None = None
        try:
            outcome = self._work()
        except Exception as exc:  # noqa: BLE001 - a worker must never raise into the pool
            failure = exc.__class__.__name__
        self._delivery.delivered.emit(Delivered(self._owner, _Finished(self._generation, outcome, failure)))


class LogsPage(QWidget):
    """The Logs view: one Refresh button and four log panes (desktop, control, tunnel, runner).

    The reads are injected. The app passes the defaults above. Tests pass fakes or temp files.
    """

    def __init__(
        self,
        *,
        desktop_log: Callable[[], LogTail] = read_desktop_log_tail,
        tunnel_log: Callable[[], LogTail] = read_tunnel_log_tail,
        runner: Callable[[], RunnerResult] = runner_tail_not_configured,
        control_log: Callable[[], LogTail] = read_control_log_tail,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("logs-page")
        self._reads = _Reads(desktop_log, tunnel_log, runner, control_log)
        self._busy = False
        # The first show starts one pass. Later shows start none, so only Refresh reads again.
        self._read_on_first_show = True
        # The control log as last read, grouped by launch. None when the control pane shows no grouped lines.
        self._control_grouped: GroupedLog | None = None
        # Identifies this page's passes. A plain object, so a job can hold it without holding the page.
        self._owner = object()
        self._generation = 0
        shared_delivery().delivered.connect(self._on_delivered)
        self._build()

    @property
    def busy(self) -> bool:
        return self._busy

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 - Qt's name for the override
        """Read once, on the first show. A later show reads nothing, and only Refresh reads again."""
        super().showEvent(event)
        if self._read_on_first_show:
            self._read_on_first_show = False
            self._start_pass()

    # ----------------------------------------------------------- layout

    def _build(self) -> None:
        layout = QVBoxLayout(self)

        self._intro = _heading(self, "logs-intro", INTRO_UNREAD_TEXT)
        layout.addWidget(self._intro)

        self._desktop_heading = _heading(self, "logs-desktop-heading", f"{DESKTOP_HEADING} not read yet.")
        layout.addWidget(self._desktop_heading)
        self._desktop_text = _pane(self, "logs-desktop-text")
        layout.addWidget(self._desktop_text, 1)

        self._control_heading = _heading(self, "logs-control-heading", f"{CONTROL_HEADING} not read yet.")
        layout.addWidget(self._control_heading)
        # Says which rule grouped the lines below, once the control log has been read.
        self._control_rule = _heading(self, "logs-control-rule", "")
        layout.addWidget(self._control_rule)
        launch_row = QHBoxLayout()
        self._control_pick = QComboBox(self)
        self._control_pick.setObjectName("logs-control-launch")
        self._control_pick.setAccessibleName("Launch")
        self._control_pick.setEnabled(False)
        self._control_search = QLineEdit(self)
        self._control_search.setObjectName("logs-control-search")
        self._control_search.setPlaceholderText(CONTROL_SEARCH_PLACEHOLDER)
        self._control_search.setAccessibleName(CONTROL_SEARCH_PLACEHOLDER)
        self._control_search.setEnabled(False)
        launch_row.addWidget(self._control_pick, 1)
        launch_row.addWidget(self._control_search, 2)
        layout.addLayout(launch_row)
        # Both controls only redraw the control log already read. Neither one starts a read.
        self._control_pick.currentIndexChanged.connect(self._render_control)
        self._control_search.textChanged.connect(self._render_control)
        self._control_text = _pane(self, "logs-control-text")
        layout.addWidget(self._control_text, 1)

        self._tunnel_heading = _heading(self, "logs-tunnel-heading", f"{TUNNEL_HEADING} not read yet.")
        layout.addWidget(self._tunnel_heading)
        self._tunnel_text = _pane(self, "logs-tunnel-text")
        layout.addWidget(self._tunnel_text, 1)

        self._runner_heading = _heading(self, "logs-runner-heading", f"{RUNNER_HEADING} not read yet.")
        layout.addWidget(self._runner_heading)
        self._runner_text = _pane(self, "logs-runner-text")
        layout.addWidget(self._runner_text, 1)

        self._refresh_button = QPushButton(REFRESH_BUTTON_TEXT, self)
        self._refresh_button.setObjectName("logs-refresh")
        layout.addWidget(self._refresh_button)

        self._refresh_button.clicked.connect(self._on_refresh_clicked)

    # ----------------------------------------------------------- reads

    @Slot()
    def _on_refresh_clicked(self) -> None:
        self._start_pass()

    def _start_pass(self) -> None:
        """Start one pass of the reads on the thread pool. A pass already running is not started again."""
        if self._busy:
            return
        self._busy = True
        self._generation += 1
        generation = self._generation
        self._refresh_button.setEnabled(False)
        self._show_reading()
        # The job holds the reads, the owner token, the generation and the shared delivery object. It never holds the page.
        QThreadPool.globalInstance().start(_ReadJob(generation, self._reads.collect, self._owner, shared_delivery()))

    @Slot(object)
    def _on_delivered(self, item: object) -> None:
        # Runs on the GUI thread. A result for another page is dropped here, and so is one from an earlier pass.
        if not isinstance(item, Delivered) or item.owner is not self._owner:
            return
        finished = item.value
        if not isinstance(finished, _Finished) or finished.generation != self._generation:
            return
        self._busy = False
        self._refresh_button.setEnabled(True)
        if finished.failure is not None or not isinstance(finished.outcome, LogsOutcome):
            self._show_all_failed(finished.failure or NO_RESULT)
            return
        self._show(finished.outcome)

    # ----------------------------------------------------------- display

    def _show_reading(self) -> None:
        self._intro.setText(INTRO_READING_TEXT)
        self._desktop_heading.setText(f"{DESKTOP_HEADING} {READING_TEXT}")
        self._control_heading.setText(f"{CONTROL_HEADING} {READING_TEXT}")
        self._tunnel_heading.setText(f"{TUNNEL_HEADING} {READING_TEXT}")
        self._runner_heading.setText(f"{RUNNER_HEADING} {READING_TEXT}")
        self._desktop_text.setPlainText("")
        self._clear_control_view()
        self._tunnel_text.setPlainText("")
        self._runner_text.setPlainText("")

    def _show_all_failed(self, failure: str) -> None:
        text = FAILED_TEXT.format("logs", failure)
        self._intro.setText(INTRO_SHOWN_TEXT)
        self._desktop_heading.setText(DESKTOP_HEADING)
        self._control_heading.setText(CONTROL_HEADING)
        self._tunnel_heading.setText(TUNNEL_HEADING)
        self._runner_heading.setText(RUNNER_HEADING)
        self._desktop_text.setPlainText(text)
        self._clear_control_view()
        self._control_text.setPlainText(text)
        self._tunnel_text.setPlainText(text)
        self._runner_text.setPlainText(text)

    def _show(self, outcome: LogsOutcome) -> None:
        self._intro.setText(INTRO_SHOWN_TEXT)
        self._show_file_pane(
            self._desktop_heading,
            self._desktop_text,
            title=DESKTOP_HEADING,
            label="desktop log",
            tail=outcome.desktop,
            failure=outcome.failures.get("desktop", NO_RESULT),
            texts=DESKTOP_TEXT,
        )
        self._show_control(outcome.control, outcome.failures.get("control", NO_RESULT))
        self._show_file_pane(
            self._tunnel_heading,
            self._tunnel_text,
            title=TUNNEL_HEADING,
            label="tunnel log",
            tail=outcome.tunnel,
            failure=outcome.failures.get("tunnel", NO_RESULT),
            texts=TUNNEL_TEXT,
        )
        self._show_runner(outcome)

    def _show_control(self, tail: object, failure: str) -> None:
        """The control pane. Its lines are grouped by launch when it has any. Otherwise it shows the same fixed text as before."""
        if not isinstance(tail, LogTail):
            self._clear_control_view()
            self._control_heading.setText(CONTROL_HEADING)
            self._control_text.setPlainText(FAILED_TEXT.format("control log", failure))
            return
        self._control_heading.setText(f"{CONTROL_HEADING} {tail.path}")
        if tail.status != STATUS_OK or not tail.lines:
            self._clear_control_view()
            self._control_text.setPlainText(log_pane_text(tail, CONTROL_TEXT))
            return
        grouped = group_by_launch([redact_text(line) for line in tail.lines])
        self._control_grouped = grouped
        self._control_rule.setText(grouped.rule_text)
        self._fill_control_picker(grouped)
        self._render_control()

    def _clear_control_view(self) -> None:
        """Forget the grouped control log, clear its pane, and turn the launch picker and search off."""
        self._control_grouped = None
        self._control_rule.setText("")
        self._control_pick.blockSignals(True)
        self._control_pick.clear()
        self._control_pick.blockSignals(False)
        self._control_pick.setEnabled(False)
        self._control_search.setEnabled(False)
        self._control_text.setPlainText("")

    def _fill_control_picker(self, grouped: GroupedLog) -> None:
        """Offer the launch picker only when there is more than one group. Its first entry shows every launch."""
        picker = self._control_pick
        picker.blockSignals(True)
        picker.clear()
        many = len(grouped.groups) > 1
        if many:
            picker.addItem(ALL_LAUNCHES_TEXT, None)
            for group in grouped.groups:
                picker.addItem(group.label, group.key)
        picker.setCurrentIndex(0)
        picker.blockSignals(False)
        picker.setEnabled(many)
        self._control_search.setEnabled(True)

    def _render_control(self, *_args: object) -> None:
        """Draw the grouped control log for the chosen launch and the search text. It redraws what was read and reads nothing."""
        grouped = self._control_grouped
        if grouped is None:
            return
        selected = self._control_pick.currentData()
        self._control_text.setPlainText(
            launch_view(grouped, selected=selected, query=self._control_search.text(), newest_first=True)
        )

    def _show_file_pane(
        self,
        heading: QLabel,
        pane: QPlainTextEdit,
        *,
        title: str,
        label: str,
        tail: object,
        failure: str,
        texts: LogText,
    ) -> None:
        if not isinstance(tail, LogTail):
            heading.setText(title)
            pane.setPlainText(FAILED_TEXT.format(label, failure))
            return
        heading.setText(f"{title} {tail.path}")
        pane.setPlainText(log_pane_text(tail, texts))

    def _show_runner(self, outcome: LogsOutcome) -> None:
        result = outcome.runner
        self._runner_heading.setText(RUNNER_HEADING)
        if not isinstance(result, RunnerResult):
            self._runner_text.setPlainText(
                FAILED_TEXT.format("runner log", outcome.failures.get("runner", NO_RESULT))
            )
            return
        self._runner_text.setPlainText(runner_log_text(result))


__all__ = [
    "DESKTOP_MISSING_TEXT",
    "EMPTY_TUNNEL_TEXT",
    "LogText",
    "LogsOutcome",
    "LogsPage",
    "REFRESH_BUTTON_TEXT",
    "RUNNER_RETRY_TEXT",
    "RUNNER_EMPTY_TEXT",
    "RUNNER_NOT_AVAILABLE_PREFIX",
    "RUNNER_NOT_CONNECTED_TEXT",
    "TUNNEL_MISSING_TEXT",
    "log_pane_text",
    "runner_log_text",
    "runner_tail_not_configured",
]
