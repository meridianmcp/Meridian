"""Client setup on the Settings page: the Meridian core checkout, its Python, and one row per client.

Each row shows the client's settings file, its state, and Apply and Remove buttons. Check setup runs
the core's plan and changes nothing. Apply and Remove first run a dry run, so the confirmation dialog
names the exact file, the exact change and the exact command. Only a Yes runs the change, and the
core makes a backup first. The result line then shows the backup path and whether a restart is needed.

Every command runs on the thread pool, so the window stays responsive. The core checkout and Python
are typed or chosen here and saved through settings_store, which refuses a path that does not exist.
The top of the section says what the desktop found on this computer (ui/runner_actions.py, RunnerActions.find_core),
with Find it again, and Choose the Meridian core folder when nothing usable was found. The owner does not have to type
either path. The runner and the dialogs are injected, so the tests never start the real command or open a real dialog.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import QRunnable, Qt, QThreadPool, Signal, Slot
from PySide6.QtWidgets import (
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from meridian_desktop.client_setup_runner import (
    CLIENTS,
    IN_PLACE,
    KIND_FAILED,
    LABELS,
    MISSING,
    NOT_SET_CHECKOUT_TEXT,
    NOT_SET_PYTHON_TEXT,
    STATE_TEXT,
    CheckResult,
    ClientChange,
    ClientRow,
    ClientSetupRunner,
    CommandResult,
    message_with_detail,
    names_text,
    unchecked_rows,
)
from meridian_desktop.core_locator import LocatedCore
from meridian_desktop.settings_store import CORE_CHECKOUT_KEY, CORE_PYTHON_KEY, SettingsStore
from meridian_desktop.ui.delivery import Delivered, Delivery, shared_delivery
from meridian_desktop.ui.runner_actions import RunnerActions
from meridian_desktop.ui.text import make_selectable

HEADING_TEXT = "Client setup"
FIND_AGAIN_TEXT = "Find it again"
CHOOSE_CORE_TEXT = "Choose the Meridian core folder"
LOCATING_TEXT = "Looking for the Meridian core on this computer. This takes a few seconds."
NOT_LOOKED_TEXT = "The Meridian core has not been looked for yet in this run."
FOUND_TEXT = "Meridian core found on this computer: {checkout}. Its Python: {python}."
NOT_FOUND_TEXT = (
    "The Meridian core was not found. {reason} Choose the Meridian core folder, "
    "or press Find it again after you move or install it."
)
INTRO_TEXT = (
    "Writes Meridian's local tools into each client's own settings file. Check setup changes nothing. "
    "Apply and Remove ask first, and a backup of the file is made before anything is written."
)
CHECKOUT_LABEL = "Meridian core checkout:"
PYTHON_LABEL = "Python for Meridian core:"
BROWSE_TEXT = "Browse..."
SAVE_TEXT = "Save"
CHECK_TEXT = "Check setup"
APPLY_TEXT = "Apply"
REMOVE_TEXT = "Remove"
CHECKOUT_DIALOG_TITLE = "Choose the folder that holds the meridian package"
PYTHON_DIALOG_TITLE = "Choose the Python that can import meridian"
PYTHON_FILE_FILTER = "Python program (python.exe python python3);;All files (*)"
YES_TEXT = "Yes"
NO_TEXT = "No"
APPLY_DIALOG_TITLE = "Apply Meridian's entries?"
REMOVE_DIALOG_TITLE = "Remove Meridian's entries?"
DEFAULT_STATUS_TEXT = "Press Check setup to read each client's settings file. Nothing is written."
CHECKING_TEXT = "Checking the clients' settings files. Nothing is written."
CHECKOUT_SAVED_TEXT = "Meridian core checkout saved."
LAUNCH_STARTS_OFF_TEXT = (
    "The Zotero and local server switches are now off, because the core setup changed. "
    "Turn them on again to confirm the new setup."
)
PYTHON_SAVED_TEXT = "Python for Meridian core saved."
NOT_WRITTEN_TEXT = "Not saved: the settings file could not be written."
CANCELLED_TEXT = "Cancelled. Nothing was changed."
ALREADY_IN_PLACE_TEXT = "{label} is already in place. Nothing was changed."
NOTHING_TO_REMOVE_TEXT = "No Meridian entries are in {path}. Nothing was changed."
WRITTEN_TEXT = "Written: {path}."
REMOVED_TEXT = "Removed Meridian's entries from {path}."
NO_BACKUP_TEXT = "No backup was made: the file did not exist before this change."
BACKUP_TEXT = "Backup saved at: {backup}"
RESTART_TEXT = "Restart {label} to load the change."
NO_FILE_TEXT = "No settings file is written for this client yet."
PATH_UNKNOWN_TEXT = "File: not known until Check setup reads it."
FILE_PREFIX = "File: "
REASON_NOT_SET = "Set the Meridian core checkout and Python first, then save them."
REASON_NOT_COVERED = "The Meridian core does not cover this client yet."
REASON_IN_PLACE = "Already in place. Nothing to apply."
REASON_MISSING = "There are no Meridian entries to remove: the file is missing."
TOOLTIP_CHECK_FIRST = "Press Check setup first."

_CHECK = "check"
_PREVIEW = "preview"
_CHANGE = "change"

FileChooser = Callable[[QWidget, str], str]
FolderChooser = Callable[[QWidget, str], str]
Confirm = Callable[[QWidget, str, str], bool]


def confirmation_text(mode: str, label: str, change: ClientChange, command: str) -> str:
    """The confirmation dialog's text: the exact file, the exact change, the exact command, and what is left alone."""
    if mode == "apply":
        heading = f"Apply Meridian's entries to {label}?"
    else:
        heading = f"Remove Meridian's entries from {label}?"
    return "\n".join(
        [
            heading,
            f"File: {change.path}",
            f"Change: {names_text(change)}",
            "Entries that Meridian does not own are left alone. A backup of the file is made first.",
            f"Restart {label} after the change to load it.",
            f"Command: {command}",
        ]
    )


def result_text(mode: str, label: str, change: ClientChange) -> str:
    """The result line after a real change: the file, the backup path, and a restart note when needed."""
    lines = [
        (REMOVED_TEXT if mode == "remove" else WRITTEN_TEXT).format(path=change.path),
        BACKUP_TEXT.format(backup=change.backup) if change.backup else NO_BACKUP_TEXT,
    ]
    if change.restart_required:
        lines.append(RESTART_TEXT.format(label=label))
    return "\n".join(lines)


def no_change_text(mode: str, label: str, path: str) -> str:
    """The result line when the core reports that there was nothing to change."""
    if mode == "remove":
        return NOTHING_TO_REMOVE_TEXT.format(path=path)
    return ALREADY_IN_PLACE_TEXT.format(label=label)


def _can_apply(row: ClientRow, configured: bool) -> bool:
    return configured and row.covered and row.state != IN_PLACE


def _can_remove(row: ClientRow, configured: bool) -> bool:
    return configured and row.covered and row.state != MISSING


def _reason(row: ClientRow, configured: bool, mode: str) -> str:
    """The tooltip for a disabled button. Empty when the button is enabled."""
    if not configured:
        return REASON_NOT_SET
    if not row.covered:
        return REASON_NOT_COVERED
    if mode == "apply" and row.state == IN_PLACE:
        return REASON_IN_PLACE
    if mode == "remove" and row.state == MISSING:
        return REASON_MISSING
    return ""


def _label(parent: QWidget, name: str, text: str = "") -> QLabel:
    label = QLabel(text, parent)
    label.setObjectName(name)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return make_selectable(label)


def _button(parent: QWidget, name: str, text: str) -> QPushButton:
    button = QPushButton(text, parent)
    button.setObjectName(name)
    return button


def _set_message(label: QLabel, text: str) -> None:
    """Show a message line, or hide the line when the text is empty."""
    label.setText(text)
    label.setVisible(bool(text))


def _choose_checkout_folder(parent: QWidget, start_dir: str) -> str:
    """Ask for a folder. Return its path, or "" when the dialog is cancelled."""
    return QFileDialog.getExistingDirectory(parent, CHECKOUT_DIALOG_TITLE, start_dir)


def _choose_python_file(parent: QWidget, start_dir: str) -> str:
    """Ask for a Python program. Return its path, or "" when the dialog is cancelled."""
    path, _selected_filter = QFileDialog.getOpenFileName(parent, PYTHON_DIALOG_TITLE, start_dir, PYTHON_FILE_FILTER)
    return path


def _ask_yes_no(parent: QWidget, title: str, text: str) -> bool:
    """A modal Yes or No. No is the default and Escape, and only a click on Yes returns True."""
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Question)
    box.setWindowTitle(title)
    box.setTextFormat(Qt.TextFormat.PlainText)
    box.setText(text)
    yes = box.addButton(YES_TEXT, QMessageBox.ButtonRole.YesRole)
    no = box.addButton(NO_TEXT, QMessageBox.ButtonRole.NoRole)
    box.setDefaultButton(no)
    box.setEscapeButton(no)
    box.exec()
    return box.clickedButton() is yes


@dataclass(frozen=True)
class _Pending:
    """What a background command was started for. It is handed back with the result."""

    purpose: str
    client: str
    mode: str


@dataclass(frozen=True)
class _Done:
    """One finished command, as its job hands it back: its generation, what it was started for, and its result."""

    generation: int
    pending: _Pending
    payload: object


class _CommandJob(QRunnable):
    """Run one command on the thread pool and deliver a _Done to the shared delivery object.

    The job holds only the command, the owner token, the generation and the shared delivery object. It never holds the
    section.
    """

    def __init__(
        self,
        generation: int,
        pending: _Pending,
        work: Callable[[], object],
        owner: object,
        delivery: Delivery,
    ) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._generation = generation
        self._pending = pending
        self._work = work
        self._owner = owner
        self._delivery = delivery

    def run(self) -> None:
        try:
            payload = self._work()
        except Exception as exc:  # noqa: BLE001 - a worker must never raise into the pool
            # The class name only: an exception's text could hold a path or a value.
            text = f"The Meridian setup step failed ({exc.__class__.__name__})."
            if self._pending.purpose == _CHECK:
                payload = CheckResult(rows=unchecked_rows(), message=text)
            else:
                payload = CommandResult(False, KIND_FAILED, text)
        self._delivery.delivered.emit(Delivered(self._owner, _Done(self._generation, self._pending, payload)))


@dataclass
class _RowWidgets:
    state: QLabel
    path: QLabel
    detail: QLabel
    apply: QPushButton
    remove: QPushButton


class ClientSetupSection(QWidget):
    """The client setup block for the Settings page. One section, one row per client.

    ``coreChanged`` is emitted after a core path is saved, or after the core search changes the setup, so the runner
    block on the same tab shows its switches and its local state again. The boxes follow the saved paths, except a box the
    owner has typed into, which is left as it is.
    """

    coreChanged = Signal()

    def __init__(
        self,
        settings_store: SettingsStore,
        parent: QWidget | None = None,
        *,
        runner: ClientSetupRunner | None = None,
        choose_folder: FolderChooser = _choose_checkout_folder,
        choose_python: FileChooser = _choose_python_file,
        confirm: Confirm = _ask_yes_no,
        actions: RunnerActions | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("client-setup-section")
        self._store = settings_store
        self._runner = runner if runner is not None else ClientSetupRunner(settings_store)
        self._choose_folder = choose_folder
        self._choose_python = choose_python
        self._confirm = confirm
        self._actions = actions if actions is not None else RunnerActions(settings_store)
        # The saved values the two boxes last showed. A box that still shows one follows the saved value; any other is typed.
        self._shown_checkout = ""
        self._shown_python = ""
        self._rows: dict[str, ClientRow] = {row.client: row for row in unchecked_rows()}
        self._widgets: dict[str, _RowWidgets] = {}
        self._busy = False
        self._configured = False
        self._missing = ""
        self._last_status = DEFAULT_STATUS_TEXT
        # Identifies this section's commands. A plain object, so a job can hold it without holding the section.
        self._owner = object()
        self._generation = 0
        shared_delivery().delivered.connect(self._on_delivered)
        self._actions.coreChanged.connect(self._on_core_changed)
        self._build()
        self.refresh()
        self._render_rows()

    # ----------------------------------------------------------- layout

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(_label(self, "client-setup-heading", HEADING_TEXT))
        layout.addWidget(_label(self, "client-setup-intro", INTRO_TEXT))

        # What the search found, with the two buttons that search again or let the owner choose the folder.
        self._finder_status = _label(self, "client-setup-finder-status")
        layout.addWidget(self._finder_status)
        finder_row = QHBoxLayout()
        self._find_again = _button(self, "client-setup-find-again", FIND_AGAIN_TEXT)
        self._choose_core = _button(self, "client-setup-choose-core", CHOOSE_CORE_TEXT)
        finder_row.addWidget(self._find_again)
        finder_row.addWidget(self._choose_core)
        finder_row.addStretch(1)
        layout.addLayout(finder_row)
        # The two path rows share one grid: the caption column takes the width of its longest caption, and the path box
        # takes the rest. A caption never wraps, so "Meridian core checkout:" is always whole.
        paths = QGridLayout()
        paths.setContentsMargins(0, 0, 0, 0)
        paths.setHorizontalSpacing(8)
        paths.setVerticalSpacing(6)
        paths.setColumnStretch(1, 1)

        self._checkout_input = QLineEdit(self)
        self._checkout_input.setObjectName("client-setup-checkout")
        self._browse_checkout = _button(self, "client-setup-browse-checkout", BROWSE_TEXT)
        self._save_checkout = _button(self, "client-setup-save-checkout", SAVE_TEXT)
        self._path_row(
            paths,
            0,
            _label(self, "client-setup-checkout-label", CHECKOUT_LABEL),
            self._checkout_input,
            self._browse_checkout,
            self._save_checkout,
        )

        self._python_input = QLineEdit(self)
        self._python_input.setObjectName("client-setup-python")
        self._browse_python = _button(self, "client-setup-browse-python", BROWSE_TEXT)
        self._save_python = _button(self, "client-setup-save-python", SAVE_TEXT)
        self._path_row(
            paths,
            1,
            _label(self, "client-setup-python-label", PYTHON_LABEL),
            self._python_input,
            self._browse_python,
            self._save_python,
        )
        layout.addLayout(paths)

        self._settings_message = _label(self, "client-setup-settings-message")
        _set_message(self._settings_message, "")
        layout.addWidget(self._settings_message)

        check_row = QHBoxLayout()
        self._check_button = _button(self, "client-setup-check", CHECK_TEXT)
        check_row.addWidget(self._check_button)
        check_row.addStretch(1)
        layout.addLayout(check_row)

        self._status = _label(self, "client-setup-status")
        layout.addWidget(self._status)

        for client, label in CLIENTS:
            layout.addLayout(self._client_block(client, label))

        self._result = _label(self, "client-setup-result")
        _set_message(self._result, "")
        layout.addWidget(self._result)

        self._browse_checkout.clicked.connect(self._on_browse_checkout)
        self._browse_python.clicked.connect(self._on_browse_python)
        self._save_checkout.clicked.connect(self._on_save_checkout)
        self._save_python.clicked.connect(self._on_save_python)
        self._check_button.clicked.connect(self._on_check_clicked)
        self._find_again.clicked.connect(self._on_find_again_clicked)
        self._choose_core.clicked.connect(self._on_choose_core_clicked)

    def _path_row(
        self, grid: QGridLayout, row: int, label: QLabel, line: QLineEdit, browse: QPushButton, save: QPushButton
    ) -> None:
        """One path row in the shared grid: caption, path box, Browse and Save. The caption never wraps."""
        label.setWordWrap(False)
        grid.addWidget(label, row, 0)
        grid.addWidget(line, row, 1)
        grid.addWidget(browse, row, 2)
        grid.addWidget(save, row, 3)

    def _client_block(self, client: str, label: str) -> QVBoxLayout:
        block = QVBoxLayout()
        head = QHBoxLayout()
        head.addWidget(_label(self, f"client-setup-{client}-name", label))
        state = _label(self, f"client-setup-{client}-state")
        head.addWidget(state, 1)
        block.addLayout(head)
        path = _label(self, f"client-setup-{client}-path")
        block.addWidget(path)
        detail = _label(self, f"client-setup-{client}-detail")
        _set_message(detail, "")
        block.addWidget(detail)

        buttons = QHBoxLayout()
        apply_button = _button(self, f"client-setup-{client}-apply", APPLY_TEXT)
        remove_button = _button(self, f"client-setup-{client}-remove", REMOVE_TEXT)
        buttons.addWidget(apply_button)
        buttons.addWidget(remove_button)
        buttons.addStretch(1)
        block.addLayout(buttons)

        self._widgets[client] = _RowWidgets(state, path, detail, apply_button, remove_button)
        apply_button.clicked.connect(lambda *_, c=client: self._start_preview(c, "apply"))
        remove_button.clicked.connect(lambda *_, c=client: self._start_preview(c, "remove"))
        return block

    # ----------------------------------------------------------- state on screen

    def refresh(self) -> None:
        """Show the saved core checkout and Python in the boxes, then update the status and the buttons."""
        settings = self._store.load().settings
        self._checkout_input.setText(settings.core_checkout)
        self._python_input.setText(settings.core_python)
        self._shown_checkout = settings.core_checkout
        self._shown_python = settings.core_python
        self._recompute()
        self._render_finder()

    def _follow_saved_boxes(self) -> None:
        """Move a box to a new saved path, unless the owner has typed into it since it last showed a saved path."""
        settings = self._store.load().settings
        if self._checkout_input.text().strip() == self._shown_checkout:
            self._checkout_input.setText(settings.core_checkout)
            self._shown_checkout = settings.core_checkout
        if self._python_input.text().strip() == self._shown_python:
            self._python_input.setText(settings.core_python)
            self._shown_python = settings.core_python

    def _render_finder(self) -> None:
        """Say what the search found on this computer, in plain words."""
        located: LocatedCore | None = self._actions.located
        if self._actions.locating:
            text = LOCATING_TEXT
        elif located is None:
            text = NOT_LOOKED_TEXT
        elif located.found:
            text = FOUND_TEXT.format(checkout=located.checkout, python=located.python)
        else:
            text = NOT_FOUND_TEXT.format(reason=located.reason)
        _set_message(self._finder_status, text)

    def _choose_core_needed(self) -> bool:
        """The Choose button shows while the setup is not usable: the search found nothing, or the saved setup is not set."""
        located = self._actions.located
        if located is not None and not located.found:
            return True
        return not self._actions.core().core_ready

    def _recompute(self) -> None:
        """Say plainly what is not set yet, and turn the buttons on or off. The boxes are left as they are."""
        settings = self._store.load().settings
        if not settings.core_checkout:
            self._missing = NOT_SET_CHECKOUT_TEXT
        elif not settings.core_python:
            self._missing = NOT_SET_PYTHON_TEXT
        else:
            self._missing = ""
        self._configured = not self._missing
        self._refresh_status()
        self._apply_enablement()

    def _refresh_status(self) -> None:
        _set_message(self._status, self._missing or self._last_status)

    def _render_rows(self) -> None:
        for client, _name in CLIENTS:
            row = self._rows[client]
            widgets = self._widgets[client]
            widgets.state.setText(STATE_TEXT[row.state])
            if not row.covered:
                path_text = NO_FILE_TEXT
            elif row.path:
                path_text = FILE_PREFIX + row.path
            else:
                path_text = PATH_UNKNOWN_TEXT
            widgets.path.setText(path_text)
            _set_message(widgets.detail, row.detail)

    def _apply_enablement(self) -> None:
        busy = self._busy
        for widget in (
            self._checkout_input,
            self._python_input,
            self._browse_checkout,
            self._save_checkout,
            self._browse_python,
            self._save_python,
        ):
            widget.setEnabled(not busy)
        self._check_button.setEnabled(not busy)
        searching = self._actions.locating
        self._find_again.setEnabled(not busy and not searching)
        self._choose_core.setEnabled(not busy and not searching)
        self._choose_core.setVisible(self._choose_core_needed())
        for client, _name in CLIENTS:
            row = self._rows[client]
            widgets = self._widgets[client]
            widgets.apply.setEnabled(not busy and _can_apply(row, self._configured))
            widgets.remove.setEnabled(not busy and _can_remove(row, self._configured))
            widgets.apply.setToolTip(_reason(row, self._configured, "apply"))
            widgets.remove.setToolTip(_reason(row, self._configured, "remove"))

    # ----------------------------------------------------------- saving the core paths

    @Slot()
    def _on_save_checkout(self) -> None:
        self._save_core(CORE_CHECKOUT_KEY, self._checkout_input.text(), CHECKOUT_SAVED_TEXT)

    @Slot()
    def _on_save_python(self) -> None:
        self._save_core(CORE_PYTHON_KEY, self._python_input.text(), PYTHON_SAVED_TEXT)

    def _save_core(self, key: str, value: str, saved_text: str) -> None:
        before = self._store.load().settings
        try:
            saved = self._store.set_core_path(key, value)
        except ValueError as exc:
            # The store's text is fixed wording that names the path it refused, and nothing was written.
            _set_message(self._settings_message, str(exc))
            return
        except OSError:
            _set_message(self._settings_message, NOT_WRITTEN_TEXT)
            return
        # The box that was just saved now shows a saved path, so a later search may move it.
        if key == CORE_CHECKOUT_KEY:
            self._shown_checkout = saved.core_checkout
        else:
            self._shown_python = saved.core_python
        # A saved core path turns both launch-time switches off (contract amendment C), and the status line says so.
        turned_off = before.zotero_autostart or before.local_server_autostart or before.tunnel_autostart
        _set_message(self._settings_message, f"{saved_text} {LAUNCH_STARTS_OFF_TEXT}" if turned_off else saved_text)
        self._recompute()  # the other box keeps what the user typed, saved or not
        self.coreChanged.emit()  # the runner block on this tab shows the switches as they are saved now
        self._actions.notify_core_changed()  # the Tunnel and local controls read the saved setup again

    @Slot()
    def _on_find_again_clicked(self) -> None:
        """Search this computer again. The search runs on the thread pool, and its result comes back through _on_core_changed."""
        self._actions.find_core()

    @Slot()
    def _on_choose_core_clicked(self) -> None:
        _set_message(self._settings_message, self._actions.choose_core_folder(self))

    @Slot()
    def _on_core_changed(self) -> None:
        """The core search finished or started, or the core folder changed. Show the search result and the saved paths."""
        self._follow_saved_boxes()
        self._recompute()
        self._render_finder()
        self.coreChanged.emit()  # the runner block on this tab reads its switches and local state again

    @Slot()
    def _on_browse_checkout(self) -> None:
        chosen = self._choose_folder(self, self._checkout_input.text().strip())
        if chosen:
            self._checkout_input.setText(chosen)  # not saved until Save is pressed

    @Slot()
    def _on_browse_python(self) -> None:
        chosen = self._choose_python(self, self._python_input.text().strip())
        if chosen:
            self._python_input.setText(chosen)  # not saved until Save is pressed

    # ----------------------------------------------------------- background commands

    def _start(self, pending: _Pending, work: Callable[[], object]) -> None:
        self._busy = True
        self._generation += 1
        self._apply_enablement()
        # The job holds the command, the owner token, the generation and the shared delivery object. It never holds the section.
        QThreadPool.globalInstance().start(_CommandJob(self._generation, pending, work, self._owner, shared_delivery()))

    @Slot()
    def _on_check_clicked(self) -> None:
        _set_message(self._status, CHECKING_TEXT)
        self._start(_Pending(_CHECK, "", ""), self._runner.check)

    def _start_preview(self, client: str, mode: str) -> None:
        """Dry run first. The dialog then shows exactly what a Yes would write."""
        runner = self._runner
        self._start(_Pending(_PREVIEW, client, mode), lambda: runner.preview(client, mode))

    @Slot(object)
    def _on_delivered(self, item: object) -> None:
        # Runs on the GUI thread. A result for another section is dropped here, and so is one from an earlier command.
        if not isinstance(item, Delivered) or item.owner is not self._owner:
            return
        done = item.value
        if not isinstance(done, _Done) or done.generation != self._generation:
            return
        pending, payload = done.pending, done.payload
        self._busy = False
        self._apply_enablement()
        if pending.purpose == _CHECK:
            self._show_check(payload)
        elif pending.purpose == _PREVIEW:
            self._after_preview(pending, payload)
        else:
            self._after_change(pending, payload)

    def _show_check(self, result: object) -> None:
        assert isinstance(result, CheckResult)
        for row in result.rows:
            self._rows[row.client] = row
        self._render_rows()
        self._last_status = message_with_detail(result.message, result.detail) or DEFAULT_STATUS_TEXT
        self._refresh_status()
        self._apply_enablement()

    def _after_preview(self, pending: _Pending, result: object) -> None:
        assert isinstance(result, CommandResult)
        label = LABELS.get(pending.client, pending.client)
        if not result.ok:
            _set_message(self._result, message_with_detail(result.message, result.detail))
            return
        change = result.changes[0]
        if change.action == "unchanged":
            _set_message(self._result, no_change_text(pending.mode, label, change.path))
            return
        command = self._runner.describe_command(pending.client, pending.mode)
        title = APPLY_DIALOG_TITLE if pending.mode == "apply" else REMOVE_DIALOG_TITLE
        if not self._confirm(self, title, confirmation_text(pending.mode, label, change, command)):
            _set_message(self._result, CANCELLED_TEXT)
            return
        runner = self._runner
        self._start(
            _Pending(_CHANGE, pending.client, pending.mode),
            lambda: runner.run_change(pending.client, pending.mode),
        )

    def _after_change(self, pending: _Pending, result: object) -> None:
        assert isinstance(result, CommandResult)
        label = LABELS.get(pending.client, pending.client)
        if not result.ok:
            _set_message(self._result, message_with_detail(result.message, result.detail))
            return
        change = result.changes[0]
        if change.action == "unchanged":
            _set_message(self._result, no_change_text(pending.mode, label, change.path))
        else:
            _set_message(self._result, result_text(pending.mode, label, change))
        # Read the file again, so the row shows its new state.
        _set_message(self._status, CHECKING_TEXT)
        self._start(_Pending(_CHECK, "", ""), self._runner.check)


__all__ = [
    "ClientSetupSection",
    "confirmation_text",
    "no_change_text",
    "result_text",
]
