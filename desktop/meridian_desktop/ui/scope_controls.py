"""Start, Stop and Restart for one runner scope: the tunnel on the Home card and the Tunnel tab, the local server in Settings. Qt code.

Every button asks the owner first, in a dialog that names the exact command, the scope and the working folder
(rule 2). Cancel changes nothing. The call runs on the thread pool, and its result comes back through the shared
delivery object with this widget's owner token and a generation number, so another widget's result, or an older
call's, is dropped.

Restart is a stop, then a start, and the owner is asked before each one (rule 9). A stop that is not confirmed
ends the restart, so nothing is started after a stop the runner did not confirm.

The buttons are enabled only when the gate says so. For the tunnel the gate also needs tunnel_control_enabled
(rule 10). Both gates need the Meridian core checkout and its Python to be set. The core is looked for at start, and
again by "Find it again" in Settings. The gate's text is shown when the buttons are off, so the owner sees why, and a
button next to the text does the one thing that turns them on: Choose the Meridian core folder, or Turn on the tunnel
controls. The buttons read the setup again whenever RunnerActions.coreChanged is emitted. After each finished call,
after_action runs on the GUI thread, so the owner can read the state again.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from meridian_desktop.runner_controls import (
    SCOPE_LABELS,
    START,
    STOP,
    ControlResult,
    confirmation_text,
)
from meridian_desktop.ui.delivery import Delivered, shared_delivery
from meridian_desktop.ui.runner_actions import CoreSetup, RunnerActions, submit
from meridian_desktop.ui.text import make_selectable

RESTART = "restart"
# Every control a card can offer. A card may offer only some: the Home tunnel card offers Start alone.
ALL_CONTROLS = (START, STOP, RESTART)
TUNNEL_OFF_TEXT = (
    "The tunnel controls are off. Turn them on with the button here. Each one still asks first, "
    "and the desktop never starts or stops the tunnel by itself."
)
CORE_NOT_SET_TEXT = (
    "The Meridian core was not found on this computer, so these buttons are off. "
    "Choose the Meridian core folder, and the desktop finds its Python."
)
# The button that does the one thing that turns the controls on, for each gate note that has one.
CHOOSE_CORE_BUTTON = "Choose the Meridian core folder"
TURN_ON_TUNNEL_BUTTON = "Turn on the tunnel controls"
FIX_CHOOSE_CORE = "choose-core"
FIX_TURN_ON_TUNNEL = "turn-on-tunnel"
FIX_BY_NOTE = {
    CORE_NOT_SET_TEXT: (FIX_CHOOSE_CORE, CHOOSE_CORE_BUTTON),
    TUNNEL_OFF_TEXT: (FIX_TURN_ON_TUNNEL, TURN_ON_TUNNEL_BUTTON),
}
BUTTON_TEXT = {START: "Start {label}", STOP: "Stop {label}", RESTART: "Restart {label}"}
QUESTION_TEXT = {START: "Start the {label}?", STOP: "Stop the {label}?"}
RESTART_STOP_QUESTION = "Restart the {label}: stop it first?"
RESTART_START_QUESTION = "Restart the {label}: start it now?"
CONFIRM_TEXT = {START: "Start", STOP: "Stop"}
CANCELLED_TEXT = {
    START: "Start cancelled. Nothing was started.",
    STOP: "Stop cancelled. Nothing was stopped.",
    RESTART: "Restart cancelled. Nothing was stopped or started.",
}
BUSY_TEXT = {START: "Starting the {label}...", STOP: "Stopping the {label}..."}
RESTART_BUSY_TEXT = "Restarting the {label}: stopping it first..."
START_CANCELLED_TEXT = "Start cancelled. The {label} is stopped."
RESTART_STOPPED_TEXT = "Restart stopped: the stop was not confirmed, so nothing was started."
RESTART_BLOCKED_TEXT = (
    "Restart stopped before the start, so nothing was started. The controls are now off, or the core setup changed."
)
ACTION_FAILED_TEXT = "The {label} action could not be completed ({failure})."
BUSY_OTHER_TEXT = "The {label} is busy with another action, so nothing was started or stopped. Try again when it finishes."

Gate = Callable[[CoreSetup], tuple[bool, str]]


def tunnel_gate(core: CoreSetup) -> tuple[bool, str]:
    """The tunnel buttons are on only when tunnel_control_enabled is true and the core is set. Otherwise the text says why."""
    if not core.tunnel_control_enabled:
        return False, TUNNEL_OFF_TEXT
    if not core.core_ready:
        return False, CORE_NOT_SET_TEXT
    return True, ""


def local_gate(core: CoreSetup) -> tuple[bool, str]:
    """The local buttons are on only when the core checkout and its Python are set."""
    if not core.core_ready:
        return False, CORE_NOT_SET_TEXT
    return True, ""


def _label(parent: QWidget, name: str, text: str = "") -> QLabel:
    label = QLabel(text, parent)
    label.setObjectName(name)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return make_selectable(label)


class ScopeControls(QWidget):
    """The Start, Stop and Restart buttons for one scope, with the gate note and the last result. It runs one call at a time.

    ``actionFinished`` is emitted on the GUI thread after each finished call. The owner connects to it, so the owner
    can read the state again. A signal, not a callback, keeps this widget from holding its owner in memory.
    """

    actionFinished = Signal()

    def __init__(
        self,
        scope: str,
        actions: RunnerActions,
        gate: Gate,
        *,
        prefix: str,
        parent: QWidget | None = None,
        offered: tuple[str, ...] = ALL_CONTROLS,
    ) -> None:
        super().__init__(parent)
        self.setObjectName(f"{prefix}-controls")
        self._scope = scope
        self._label = SCOPE_LABELS[scope]
        self._actions = actions
        self._gate = gate
        self._busy = False
        self._enabled_by_gate = False
        # The button that turns the controls on, when the gate is off: FIX_CHOOSE_CORE, FIX_TURN_ON_TUNNEL, or None.
        self._fix_kind: str | None = None
        # The call in flight: (python, working folder, "start" when a restart still has its start to do, or None).
        self._pending: tuple[str, str, str | None] | None = None
        # Identifies this widget's calls. A plain object, so a job can hold it without holding the widget.
        self._owner = object()
        self._generation = 0
        shared_delivery().delivered.connect(self._on_delivered)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        # Only the offered controls get a button, so a card that offers Start alone holds no Stop or Restart button.
        self._buttons: dict[str, QPushButton] = {}
        for kind in ALL_CONTROLS:
            if kind in offered:
                button = QPushButton(BUTTON_TEXT[kind].format(label=self._label), self)
                button.setObjectName(f"{prefix}-{kind}")
                self._buttons[kind] = button
        # The one-step fix. It is shown only while the gate is off, next to the greyed controls, and says what it does.
        self._fix = QPushButton(self)
        self._fix.setObjectName(f"{prefix}-fix")
        self._fix.setVisible(False)
        for button in (*self._buttons.values(), self._fix):
            row.addWidget(button)
        row.addStretch(1)
        layout.addLayout(row)
        self._note = _label(self, f"{prefix}-control-note")
        self._result = _label(self, f"{prefix}-control-result")
        self._result.setVisible(False)
        layout.addWidget(self._note)
        layout.addWidget(self._result)

        handlers = {START: self._on_start_clicked, STOP: self._on_stop_clicked, RESTART: self._on_restart_clicked}
        for kind, button in self._buttons.items():
            button.clicked.connect(handlers[kind])
        self._fix.clicked.connect(self._on_fix_clicked)
        # The core search and a chosen core folder change the gate, so the buttons read the setup again then.
        actions.coreChanged.connect(self.refresh_enabled)
        # Another tab or card may hold this scope's one call, so the buttons read that again when it changes.
        actions.scopeBusyChanged.connect(self._set_buttons)
        self.refresh_enabled()

    # ----------------------------------------------------------- state

    def refresh_enabled(self) -> None:
        """Read the saved setup again and set the buttons, the gate note and the fix button. Starts nothing."""
        enabled, note = self._gate(self._actions.core())
        self._enabled_by_gate = enabled
        self._note.setText(note)
        self._note.setVisible(bool(note))
        fix = FIX_BY_NOTE.get(note) if not enabled else None
        self._fix_kind = fix[0] if fix else None
        self._fix.setText(fix[1] if fix else "")
        self._set_buttons()

    def _set_buttons(self) -> None:
        on = self._enabled_by_gate and not self._busy and not self._actions.scope_busy_by_other(self._scope, self._owner)
        for button in self._buttons.values():
            button.setEnabled(on)
        self._fix.setVisible(self._fix_kind is not None)
        self._fix.setEnabled(self._fix_kind is not None and not self._busy)

    def _show(self, text: str) -> None:
        self._result.setText(text)
        self._result.setVisible(bool(text))

    def _finish(self) -> None:
        # The whole action is over, so this widget gives the scope back. A restart kept it through its start too.
        self._actions.end_scope_call(self._scope, self._owner)
        self._set_buttons()
        self.actionFinished.emit()

    def _start_allowed(self, python: str, cwd: str) -> bool:
        """True when the second step of a restart may start with these values.

        The gate must be on now, and the core setup must be the one the owner was shown.
        """
        core = self._actions.core()
        enabled, _note = self._gate(core)
        return enabled and (core.python, core.cwd) == (python, cwd)

    def _stop_restart_before_start(self, stop_text: str) -> None:
        """The restart ends after its stop: nothing starts, and the buttons read the gate again."""
        self._show(f"{stop_text} {RESTART_BLOCKED_TEXT}")
        self.refresh_enabled()
        self._finish()

    # ----------------------------------------------------------- asking and running

    @Slot()
    def _on_start_clicked(self) -> None:
        self._ask_and_run(START, restart=False)

    @Slot()
    def _on_stop_clicked(self) -> None:
        self._ask_and_run(STOP, restart=False)

    @Slot()
    def _on_restart_clicked(self) -> None:
        self._ask_and_run(STOP, restart=True)

    @Slot()
    def _on_fix_clicked(self) -> None:
        """Do the one thing the gate note names: choose the core folder, or turn the tunnel controls on. Starts nothing."""
        if self._busy or self._fix_kind is None:
            return
        if self._fix_kind == FIX_CHOOSE_CORE:
            message = self._actions.choose_core_folder(self)
        else:
            message = self._actions.enable_tunnel_controls()
        self._show(message)
        self.refresh_enabled()

    def _ask_and_run(self, subcommand: str, *, restart: bool) -> None:
        if self._busy:
            return
        core = self._actions.core()
        enabled, _note = self._gate(core)
        if not enabled:
            # The buttons are off, so nothing is asked and nothing runs. The gate note already says why.
            self.refresh_enabled()
            return
        if self._actions.scope_busy_by_other(self._scope, self._owner):
            # Another tab or card is running this scope's call, so nothing is asked and nothing runs.
            self._show(BUSY_OTHER_TEXT.format(label=self._label))
            return
        first = STOP if restart else subcommand
        if restart:
            question = RESTART_STOP_QUESTION.format(label=self._label)
            cancelled = CANCELLED_TEXT[RESTART]
        else:
            question = QUESTION_TEXT[subcommand].format(label=self._label)
            cancelled = CANCELLED_TEXT[subcommand]
        body = confirmation_text(core.python, first, self._scope, core.cwd)
        if not self._actions.ask(self, question, body, CONFIRM_TEXT[first]):
            self._show(cancelled)
            return
        self._submit(first, core.python, core.cwd, then=START if restart else None, restart=restart)

    def _submit(self, subcommand: str, python: str, cwd: str, *, then: str | None, restart: bool = False) -> None:
        # Claim the scope first. A restart already holds it from its stop, so the claim simply succeeds again.
        if not self._actions.begin_scope_call(self._scope, self._owner):
            self._show(BUSY_OTHER_TEXT.format(label=self._label))
            self._finish()
            return
        self._busy = True
        self._pending = (python, cwd, then)
        self._generation += 1
        generation = self._generation
        if restart:
            self._show(RESTART_BUSY_TEXT.format(label=self._label))
        else:
            self._show(BUSY_TEXT[subcommand].format(label=self._label))
        self._set_buttons()
        control = self._actions.control(python, cwd)
        scope = self._scope
        call = control.start if subcommand == START else control.stop
        submit(self._owner, generation, lambda: call(scope))

    @Slot(object)
    def _on_delivered(self, item: object) -> None:
        # Runs on the GUI thread. A result for another widget, an older call, or a call that is no longer pending is dropped.
        if not isinstance(item, Delivered) or item.owner is not self._owner:
            return
        generation, (result, failure) = item.value
        if generation != self._generation or not self._busy or self._pending is None:
            return
        python, cwd, then = self._pending
        self._busy = False
        self._pending = None
        if failure is not None or not isinstance(result, ControlResult):
            self._show(ACTION_FAILED_TEXT.format(label=self._label, failure=failure or "no result"))
            self._finish()
            return
        if then == START:
            if not result.confirmed:
                self._show(f"{result.message} {RESTART_STOPPED_TEXT}")
                self._finish()
                return
            # The gate is checked again before the start is asked, so a switch turned off after the stop is not asked.
            if not self._start_allowed(python, cwd):
                self._stop_restart_before_start(result.message)
                return
            # The stop is confirmed, so the owner is asked before the start, as rule 9 requires.
            body = confirmation_text(python, START, self._scope, cwd)
            question = RESTART_START_QUESTION.format(label=self._label)
            if not self._actions.ask(self, question, body, CONFIRM_TEXT[START]):
                self._show(f"{result.message} {START_CANCELLED_TEXT.format(label=self._label)}")
                self._finish()
                return
            # Checked once more after the dialog, just before the start runs. It uses the values the dialog showed.
            if not self._start_allowed(python, cwd):
                self._stop_restart_before_start(result.message)
                return
            self._submit(START, python, cwd, then=None)
            return
        self._show(result.message)
        self._finish()


__all__ = [
    "CORE_NOT_SET_TEXT",
    "RESTART",
    "TUNNEL_OFF_TEXT",
    "ScopeControls",
    "local_gate",
    "tunnel_gate",
]
