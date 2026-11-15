"""The Settings tab's runner block: the local server's state and controls, and the two launch-time switches. Qt code.

The block has three parts, in this order:

- The local server's state, from one read-only runner status call for the local scope. The call runs on the thread
  pool when the tab is shown, and the block says "checking" until it answers. An unknown state is never shown as running.
- Start, Stop and Restart for the local scope (ui/scope_controls.py). They are on when the Meridian core checkout and
  its Python are set, and each one asks the owner first.
- Three switches: zotero_autostart and local_server_autostart (amendments A to C of the contract), and
  tunnel_autostart (amendment G). Turning one on shows the same confirmation dialog, and Cancel leaves it off. Confirming it saves the switch with the exact values the
  dialog showed (the Zotero program, or the core Python and checkout), and the launch start uses only those. Turning
  one off saves at once and asks nothing.

The block starts nothing when it is built. The switch states are read from the saved settings, with signals blocked,
so setting a box never asks the owner by accident.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Slot
from PySide6.QtGui import QShowEvent
from PySide6.QtWidgets import QCheckBox, QLabel, QVBoxLayout, QWidget

from meridian_desktop.launch_starts import TUNNEL_MODES
from meridian_desktop.runner_controls import SCOPE_LOCAL, SCOPE_TUNNEL, START, ControlResult, confirmation_text
from meridian_desktop.ui.delivery import Delivered, shared_delivery
from meridian_desktop.ui.runner_actions import RunnerActions, submit
from meridian_desktop.ui.scope_controls import CORE_NOT_SET_TEXT, ScopeControls, local_gate
from meridian_desktop.ui.text import make_selectable
from meridian_desktop.zotero_client import command_line

HEADING_TEXT = "Local server and launch-time starts"
NOT_CHECKED_TEXT = "Local server: not checked yet. It is checked when this tab is shown."
CHECKING_TEXT = "Local server: checking..."
NO_CORE_TEXT = "Local server: not checked, because the Meridian core is not set up on this computer."
STATUS_FAILED_TEXT = "Local server: could not be checked. It is not shown as running."
ZOTERO_CHECK_TEXT = "Start Zotero when the desktop opens, only when nothing answers on 127.0.0.1:23119"
LOCAL_CHECK_TEXT = "Start the local server when the desktop opens, only when the runner reports it not running"
ZOTERO_QUESTION = "Start Zotero when the desktop opens?"
ZOTERO_BODY_EXTRA = (
    "Zotero starts once per desktop launch, and only when nothing answers on 127.0.0.1:23119. "
    "The desktop never restarts Zotero."
)
LOCAL_QUESTION = "Start the local server when the desktop opens?"
LOCAL_BODY_EXTRA = (
    "The local server starts once per desktop launch, and only when the runner reports it not running and "
    "nothing answers at the local URL."
)
ZOTERO_NOT_INSTALLED_TEXT = (
    "Not turned on. Zotero is not installed in a usual folder, so the desktop cannot start it when it opens."
)
ZOTERO_CANCELLED_TEXT = "Not turned on. Zotero will not start when the desktop opens."
LOCAL_CANCELLED_TEXT = "Not turned on. The local server will not start when the desktop opens."
LOCAL_NOT_SET_TEXT = "Not turned on. " + CORE_NOT_SET_TEXT
ZOTERO_ON_TEXT = "Saved. Zotero will start when the desktop opens, if nothing answers on 127.0.0.1:23119."
LOCAL_ON_TEXT = "Saved. The local server will start when the desktop opens, if the runner reports it not running."
SWITCH_OFF_TEXT = "Saved. It will not start when the desktop opens."
NOT_SAVED_TEXT = "Not saved: the settings file could not be written."
TUNNEL_CHECK_TEXT = (
    "Start the hosted tunnel when the desktop opens, only in Hosted mode and only when the runner "
    "reports it not running"
)
TUNNEL_QUESTION = "Start the hosted tunnel when the desktop opens?"
TUNNEL_BODY_EXTRA = (
    "The tunnel starts once per desktop launch, only in Hosted mode, and only when the runner reports it "
    "not running. While it runs, claude.ai can reach the working folder shown above. It starts again on every desktop "
    "launch with no further question. Turn this switch off to stop that."
)
TUNNEL_MODE_NOT_SET_TEXT = "Not turned on. Set the desktop to Hosted mode first."
TUNNEL_CONTROLS_OFF_TEXT = "Not turned on. The tunnel buttons are off. Turn them on first."
TUNNEL_CANCELLED_TEXT = "Not turned on. The tunnel will not start when the desktop opens."
TUNNEL_ON_TEXT = (
    "Saved. The tunnel will start when the desktop opens, if the desktop is in Hosted mode and the "
    "runner reports it not running."
)


def _label(parent: QWidget, name: str, text: str = "") -> QLabel:
    label = QLabel(text, parent)
    label.setObjectName(name)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return make_selectable(label)


class RunnerSection(QWidget):
    """The local server's state and controls, and the two launch-time switches. It holds no widget of another tab."""

    def __init__(self, actions: RunnerActions, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("runner-section")
        self._actions = actions
        # Identifies this block's state reads. A plain object, so a job can hold it without holding the block.
        self._owner = object()
        self._generation = 0
        self._reading = False
        shared_delivery().delivered.connect(self._on_delivered)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(_label(self, "runner-heading", HEADING_TEXT))
        self._local_state = _label(self, "runner-local-state", NOT_CHECKED_TEXT)
        layout.addWidget(self._local_state)

        self._controls = ScopeControls(SCOPE_LOCAL, actions, local_gate, prefix="local", parent=self)
        self._controls.actionFinished.connect(self._after_local_action)
        actions.coreChanged.connect(self._on_core_changed)
        layout.addWidget(self._controls)

        self._zotero_box = QCheckBox(ZOTERO_CHECK_TEXT, self)
        self._zotero_box.setObjectName("settings-zotero-autostart")
        self._local_box = QCheckBox(LOCAL_CHECK_TEXT, self)
        self._local_box.setObjectName("settings-local-autostart")
        self._tunnel_box = QCheckBox(TUNNEL_CHECK_TEXT, self)
        self._tunnel_box.setObjectName("settings-tunnel-autostart")
        layout.addWidget(self._zotero_box)
        layout.addWidget(self._local_box)
        layout.addWidget(self._tunnel_box)

        self._message = _label(self, "runner-autostart-message")
        self._message.setVisible(False)
        layout.addWidget(self._message)

        self._zotero_box.clicked.connect(self._on_zotero_clicked)
        self._local_box.clicked.connect(self._on_local_clicked)
        self._tunnel_box.clicked.connect(self._on_tunnel_clicked)
        self.refresh_flags()

    # ----------------------------------------------------------- state

    def refresh_flags(self) -> None:
        """Show the two saved switches. Signals are blocked, so showing them never asks the owner."""
        core = self._actions.core()
        for box, on in (
            (self._zotero_box, core.zotero_autostart),
            (self._local_box, core.local_server_autostart),
            (self._tunnel_box, core.tunnel_autostart),
        ):
            box.blockSignals(True)
            box.setChecked(on)
            box.blockSignals(False)

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 - Qt naming
        super().showEvent(event)
        self._controls.refresh_enabled()
        self.refresh_local_state()

    def refresh_local_state(self) -> None:
        """One read-only status call for the local scope on the thread pool. A call already running is not doubled."""
        core = self._actions.core()
        if not core.core_ready:
            self._local_state.setText(NO_CORE_TEXT)
            return
        if self._reading:
            return
        self._reading = True
        self._generation += 1
        generation = self._generation
        self._local_state.setText(CHECKING_TEXT)
        control = self._actions.control(core.python, core.cwd)
        submit(self._owner, generation, lambda: control.status(SCOPE_LOCAL))

    @Slot()
    def _after_local_action(self) -> None:
        """After a local Start, Stop or Restart finishes, read the state again. An older read's result is dropped."""
        self._reading = False
        self.refresh_local_state()

    @Slot()
    def _on_core_changed(self) -> None:
        """The core setup changed. The switches are read again, and the local state too once the search has finished.

        The state is read whether or not this block is shown. The search at start finishes while Settings is still hidden,
        so this read is what puts the local server's answer on the line before the owner opens Settings.
        """
        self.refresh_flags()
        if self._actions.locating:
            return
        self._reading = False
        self._generation += 1
        self.refresh_local_state()

    @Slot(object)
    def _on_delivered(self, item: object) -> None:
        # Runs on the GUI thread. A result for another block, or an older read, is dropped here.
        if not isinstance(item, Delivered) or item.owner is not self._owner:
            return
        generation, (result, failure) = item.value
        if generation != self._generation:
            return
        self._reading = False
        if failure is not None or not isinstance(result, ControlResult):
            self._local_state.setText(STATUS_FAILED_TEXT)
            return
        self._local_state.setText(result.message)

    # ----------------------------------------------------------- switches

    def _set_message(self, text: str) -> None:
        self._message.setText(text)
        self._message.setVisible(bool(text))

    def _revert(self, box: QCheckBox, text: str) -> None:
        """Put a switch back to its saved state after Cancel or a refusal. Signals are blocked, so nothing is asked twice."""
        box.blockSignals(True)
        box.setChecked(not box.isChecked())
        box.blockSignals(False)
        self._set_message(text)

    def _save(
        self,
        name: str,
        value: bool,
        box: QCheckBox,
        on_text: str,
        *,
        confirmed: dict[str, str] | None = None,
    ) -> None:
        try:
            self._actions.set_flag(name, value, confirmed=confirmed)
        except (OSError, ValueError):
            self._revert(box, NOT_SAVED_TEXT)
            return
        self._set_message(on_text if value else SWITCH_OFF_TEXT)

    @Slot(bool)
    def _on_zotero_clicked(self, checked: bool) -> None:
        if not checked:
            self._save("zotero_autostart", False, self._zotero_box, ZOTERO_ON_TEXT)
            return
        exe = self._actions.find_zotero()
        if exe is None:
            self._revert(self._zotero_box, ZOTERO_NOT_INSTALLED_TEXT)
            return
        folder = _folder_of(exe)
        body = (
            "The desktop will run this command:\n\n"
            f"{command_line(exe)}\n\n"
            "Scope: zotero, once at launch\n"
            f"Working folder: {folder}\n\n"
            f"{ZOTERO_BODY_EXTRA}"
        )
        if not self._actions.ask(self, ZOTERO_QUESTION, body, "Turn on"):
            self._revert(self._zotero_box, ZOTERO_CANCELLED_TEXT)
            return
        # The exact path the dialog named is stored, so the launch start runs that program and no other.
        self._save(
            "zotero_autostart", True, self._zotero_box, ZOTERO_ON_TEXT,
            confirmed={"confirmed_zotero_exe": exe},
        )

    @Slot(bool)
    def _on_local_clicked(self, checked: bool) -> None:
        if not checked:
            self._save("local_server_autostart", False, self._local_box, LOCAL_ON_TEXT)
            return
        core = self._actions.core()
        if not core.core_ready:
            self._revert(self._local_box, LOCAL_NOT_SET_TEXT)
            return
        body = confirmation_text(core.python, START, SCOPE_LOCAL, core.cwd) + f"\n\n{LOCAL_BODY_EXTRA}"
        if not self._actions.ask(self, LOCAL_QUESTION, body, "Turn on"):
            self._revert(self._local_box, LOCAL_CANCELLED_TEXT)
            return
        # The exact Python and folder the dialog showed are stored. The launch start uses these, not the later setup.
        self._save(
            "local_server_autostart", True, self._local_box, LOCAL_ON_TEXT,
            confirmed={"confirmed_core_python": core.python, "confirmed_core_checkout": core.cwd},
        )

    @Slot(bool)
    def _on_tunnel_clicked(self, checked: bool) -> None:
        if not checked:
            self._save("tunnel_autostart", False, self._tunnel_box, TUNNEL_ON_TEXT)
            return
        core = self._actions.core()
        if core.mode not in TUNNEL_MODES:
            self._revert(self._tunnel_box, TUNNEL_MODE_NOT_SET_TEXT)
            return
        if not core.core_ready:
            self._revert(self._tunnel_box, LOCAL_NOT_SET_TEXT)
            return
        if not core.tunnel_control_enabled:
            self._revert(self._tunnel_box, TUNNEL_CONTROLS_OFF_TEXT)
            return
        body = confirmation_text(core.python, START, SCOPE_TUNNEL, core.cwd) + f"\n\n{TUNNEL_BODY_EXTRA}"
        if not self._actions.ask(self, TUNNEL_QUESTION, body, "Turn on"):
            self._revert(self._tunnel_box, TUNNEL_CANCELLED_TEXT)
            return
        # The exact Python and folder the dialog showed are stored. The launch start uses these, not the later setup.
        self._save(
            "tunnel_autostart", True, self._tunnel_box, TUNNEL_ON_TEXT,
            confirmed={"confirmed_core_python": core.python, "confirmed_core_checkout": core.cwd},
        )


def _folder_of(exe: str) -> str:
    """The folder that holds the executable, as the dialog names it."""
    return str(Path(exe).parent)


__all__ = [
    "HEADING_TEXT",
    "LOCAL_CHECK_TEXT",
    "NOT_CHECKED_TEXT",
    "RunnerSection",
    "ZOTERO_CHECK_TEXT",
]
