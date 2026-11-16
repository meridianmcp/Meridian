"""Zotero tab: whether Zotero is installed, running and answering, a Start Zotero button, and a library search.

This is Qt code and sits in ui/, so the boundary check allows it. The tab makes no request when it
is built. It checks Zotero when first shown and when Refresh is pressed. Each check, each search and
the start action run on the thread pool, so the UI never blocks. Zotero starts only after the owner
picks Start Zotero in a dialog whose default button is Cancel. Results are plain text (PlainText
labels and plain table cells), so Zotero's text is never read as HTML. The results table is read-only
and has the shared Copy action (Ctrl+C or the right-click menu). The save section at the foot sends lines of
reading-list items through the Meridian Zotero bridge. The bridge check runs with the other checks, and Save runs on the
thread pool as well. A line whose outcome is unknown is held back until the person checks Zotero and presses I checked Zotero.
The bridge section checks the installed bridge at start and when this tab first opens, only while the owner's update-check
switch is on. It never installs by itself, and it asks the owner before a forced close of Zotero unless the owner has opted in.
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PySide6.QtCore import QRunnable, Qt, QThreadPool, QTimer, Slot
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from meridian_desktop import zotero_bridge, zotero_bridge_install, zotero_client, zotero_journal, zotero_scope
from meridian_desktop.settings_store import SettingsUnreadable, licence_accepted_for
from meridian_desktop.ui.delivery import Delivered, shared_delivery
from meridian_desktop.ui.text import enable_table_copy, make_selectable
from meridian_desktop.zotero_client import (
    API_DISABLED,
    API_NOT_RUNNING,
    API_ON,
    SEARCH_LIMIT,
    ZoteroError,
    ZoteroItem,
    ZoteroStatus,
    command_line,
)

INTRO_TEXT = (
    "Zotero is a separate app that runs on this computer. Searches and status read its local API on 127.0.0.1:23119. "
    "Saves go through the Meridian Zotero bridge on 127.0.0.1:23125, which is the only thing that writes."
)
NOT_CHECKED_TEXT = "Zotero: not checked yet. Press Refresh to check."
CHECKING_TEXT = "Checking Zotero..."
CHECK_FAILED_TEXT = "The check failed unexpectedly ({})."
INSTALLED_TEXT = "Installed: yes, at {}"
NOT_INSTALLED_TEXT = (
    "Installed: not found in the usual install folders. Install Zotero from zotero.org, then press Refresh."
)
PROCESS_RUNNING_TEXT = "Zotero process: running"
PROCESS_STOPPED_TEXT = "Zotero process: not running"
PROCESS_UNKNOWN_TEXT = "Zotero process: could not be checked on this computer"
API_TEXT = {
    API_ON: "Local API: on",
    API_DISABLED: "Local API: off (Zotero has it turned off)",
    API_NOT_RUNNING: "Local API: not answering (Zotero is not running)",
}
API_ERROR_TEXT = "Local API: could not be checked"
START_HINT_READY = (
    "Start Zotero opens Zotero on this computer after you confirm. The desktop changes no Zotero setting."
)
START_HINT_RUNNING = (
    "Zotero is already running, so there is nothing to start. If the local API is off, "
    "turn it on in Zotero under Settings, then Advanced."
)
START_HINT_NOT_INSTALLED = (
    "Zotero is not installed in a usual folder, so the desktop cannot start it. "
    "Install Zotero from zotero.org, then press Refresh."
)
START_CANCELLED_TEXT = "Start cancelled. Nothing was started and no file was written."
STARTING_TEXT = "Starting Zotero..."
API_READY_TEXT = "Local API answering: yes"
API_NOT_READY_TEXT = "Local API answering: not yet. Zotero can take a little while to open. Press Refresh in a moment."
SEARCH_PLACEHOLDER = "Search your Zotero library by title, author or year"
SEARCH_HINT = "Type a search term, then press Search or Enter."
BLANK_TEXT = "Type a search term first. Nothing was sent to Zotero."
SEARCHING_TEXT = "Searching your Zotero library..."
NO_RESULTS_TEXT = "No items in your Zotero library match \"{}\"."
RESULTS_TEXT = "{} result(s) for \"{}\"."
LIMIT_TEXT = " Showing the first {}; narrow the search to see fewer."
RESULT_COLUMNS = ("Title", "Authors", "Year", "Zotero key")
SCOPE_HEADING = "Project scope: the Zotero collection each project searches"
SCOPE_INTRO = (
    "Pick a project, then a library and a collection in your Zotero. Searches for that project read only that "
    "collection. A project with no collection searches your whole library. Saves go into the project's collection "
    "when it is in My Library and the bridge plugin is 0.1.4 or newer. Group collections cannot take saves yet."
)
SCOPE_PROJECT_LABEL = "Project"
SCOPE_LIBRARY_LABEL = "Library"
SCOPE_COLLECTION_LABEL = "Collection"
SCOPE_NO_PROJECTS_TEXT = "No projects were found, so no collection can be set."
SCOPE_NO_SOURCE_TEXT = "Projects are not available in this window, so no collection can be set."
SCOPE_LOADING_TEXT = "Loading..."
SCOPE_NO_LIBRARY_TEXT = "Pick a library first."
SCOPE_NO_COLLECTIONS_TEXT = "This library has no collections."
SCOPE_SAVE_TEXT = "Use this collection"
SCOPE_CLEAR_TEXT = "Clear collection"
SCOPE_RELOAD_TEXT = "Reload libraries"
SCOPE_NEED_TEXT = "Pick a project, a library and a collection first. Nothing was saved."
SCOPE_SAVED_TEXT = "Saved. Searches for {} now read the collection '{}' in {}."
SCOPE_CLEARED_TEXT = "Cleared. Searches for {} now read the whole library."
SCOPE_NO_PROJECT_TEXT = "Pick a project to see or set its collection."
SCOPE_CURRENT_BOUND = "{} searches the collection '{}' in {}."
SCOPE_CURRENT_UNBOUND = "{} searches your whole library. No collection is set."
SCOPE_SEARCH_IN = " Searched the collection '{}' in {} for this project."
SCOPE_SEARCH_WHOLE = " This project has no collection set, so the search covered your whole library."
SCOPE_SEARCHING_TEXT = "Searching the collection '{}' in {}..."
LIBRARIES_FAILED_TEXT = "The Zotero libraries could not be read. Press Reload libraries to try again."
COLLECTIONS_FAILED_TEXT = "The collections could not be read."
NOT_SAVED_TEXT = "Not saved: the scope file could not be written ({})."
SAVE_HEADING = "Save to Zotero"
SAVE_INTRO = (
    "One item per line, as Title | URL. The items go into your Zotero library through the Meridian Zotero bridge. "
    "Nothing is deleted."
)
SAVE_PLACEHOLDER = "Title | https://example.org/page"
SAVE_BUTTON_TEXT = "Save to Zotero"
SAVING_TEXT = "Saving to Zotero through the Meridian bridge..."
SAVE_NO_RESULT_TEXT = (
    "The save ended without a result, so every line is held back. Check Zotero before you save again."
)
SAVE_HELD_TEXT = "Save is off until you check Zotero for the held lines and press I checked Zotero."
HELD_HEADING = (
    "Held back: these may already be in Zotero, so they are not retried. Check Zotero, then press I checked Zotero."
)
HELD_BUTTON_TEXT = "I checked Zotero"
HELD_RELEASED_TEXT = "Released. Nothing was retried."
STILL_WRITING_TEXT = (
    "Released the lines Zotero confirmed. Lines still being written in another window stay held until they finish."
)
BRIDGE_CHECKING_TEXT = "Bridge: checking..."
JOURNAL_LOADING_TEXT = "The save journal is still loading. Save is off until it is read."
JOURNAL_CHECK_FAILED_TEXT = "The save journal could not be checked, so saving is off."
WRITING_ELSEWHERE_TEXT = (
    "Another window is still writing a line, so it stays held until that write ends. "
    "If no other window is open, restart the desktop."
)
# A busy journal is tried again after this long, so a window that opened while another held the lock recovers.
JOURNAL_RETRY_MS = 2000
# The kinds of read the panel runs. Each kind has at most one read in flight.
STATUS = "status"
SEARCH = "search"
ACTION = "action"
BRIDGE = "bridge"
SAVE = "save"
JOURNAL = "journal"
CLEAR = "clear"
PROJECTS = "projects"
LIBRARIES = "libraries"
COLLECTIONS = "collections"
BRIDGE_INSTALL_STATE = "bridge_install_state"
BRIDGE_LICENCE = "bridge_licence"
BRIDGE_INSTALL = "bridge_install"
BRIDGE_UPDATE = "bridge_update"

# The Zotero bridge install section. Every text names no path: the token file is referred to, never shown.
BRIDGE_HEADING = "Zotero bridge"
BRIDGE_INTRO = (
    "The Meridian Zotero bridge is the plugin that saves reading-list items into Zotero. Meridian Desktop carries a signed "
    "copy of it and installs or updates it in your Zotero profile. Zotero must be closed for the install."
)
INSTALL_CHECKING_TEXT = "Zotero bridge: checking..."
BRIDGE_CHECK_FAILED_TEXT = "Zotero bridge: could not be checked. Press Refresh to try again."
BRIDGE_NO_BUNDLE_TEXT = "No bundled bridge in this build."
BRIDGE_DAMAGED_TEXT = "The bridge record in this build is damaged. Rebuild Meridian Desktop with the signed bridge file."
BRIDGE_NO_PROFILE_TEXT = "Zotero's default profile was not found. Open Zotero once, then press Refresh."
BRIDGE_UNREADABLE_TEXT = (
    "Zotero's extensions.json could not be read, so the installed bridge version is not known. Nothing will be written."
)
BRIDGE_NOT_INSTALLED_TEXT = "Zotero bridge: not installed. The bundled version is {}."
BRIDGE_OLDER_TEXT = "Zotero bridge: installed {}, older than the bundled {}."
BRIDGE_UNKNOWN_TEXT = "Zotero bridge: the installed version could not be read. The bundled version is {}."
BRIDGE_CURRENT_TEXT = "Zotero bridge: installed {}, up to date."
BRIDGE_NEWER_TEXT = "Zotero bridge: installed {}, newer than the bundled {}. Nothing will be changed."
BRIDGE_REG_NOT_LISTED = (
    "Zotero does not list the bridge yet. After the install, start Zotero and turn the bridge on once in Tools > Plugins."
)
BRIDGE_REG_DISABLED = "Zotero has the bridge turned off. The install does not change that: turn it on in Tools > Plugins."
BRIDGE_REG_BLOCKED = "Zotero blocks the bridge. The install does not change that."
LICENCE_NEEDED_TEXT = "Read and accept the licence notice before the bridge can be installed."
LICENCE_NO_STORE_TEXT = "Settings are not available in this window, so the licence acceptance cannot be saved here."
INSTALL_BUTTON_TEXT = "Install Zotero bridge"
UPDATE_BUTTON_TEXT = "Update Zotero bridge"
LICENCE_BUTTON_TEXT = "Read the licence notice"
LICENCE_TITLE = "Zotero bridge licence"
LICENCE_ACCEPT_TEXT = "Accept"
LICENCE_ACCEPTED_TEXT = "Licence accepted. You can install the bridge now."
LICENCE_DECLINED_TEXT = "Licence not accepted. Nothing was installed and nothing was saved."
LICENCE_SAVE_FAILED_TEXT = "The licence acceptance could not be saved ({}). Nothing was installed."
INSTALL_TITLE = "Install Zotero bridge"
INSTALL_CONFIRM_BUTTON = "Install"
INSTALL_CANCELLED_TEXT = "Install cancelled. Nothing was written and Zotero was not closed."
INSTALLING_TEXT = "Installing the Zotero bridge..."
INSTALL_NO_RESULT_TEXT = "The install ended without a result, so nothing is known to have been written. Press Refresh to check."
INSTALL_FAILED_TEXT = "The install stopped with an error, so nothing is known to have been written. Press Refresh to check."
FORCING_TEXT = "Forcing Zotero to close now, as you chose. This takes up to about 10 seconds."
# The update check. The switch is saved on this tab, and the check runs only while the switch is on.
UPDATE_CHECK_TEXT = "Check for bridge updates when Meridian starts or this tab opens"
UPDATE_AVAILABLE_TEXT = (
    "A newer Zotero bridge ({bundled}) is bundled in this build than the one installed ({installed}). "
    "It can be installed below. Nothing has been installed."
)
UPDATE_CHECK_NOT_SAVED_TEXT = "The update-check switch could not be saved ({}). Nothing was changed."
# The force-close question, and the opt-in its checkbox stores.
FORCE_TITLE = "Force close Zotero"
FORCE_QUESTION_TEXT = "Zotero did not close. Force close it now? Unsaved Zotero work may be lost."
FORCE_YES_TEXT = "Yes"
FORCE_NO_TEXT = "No"
FORCE_REMEMBER_TEXT = "Force close next time"
FORCE_NOT_SAVED_TEXT = "The choice to force close next time could not be saved ({}). Nothing was closed or written."
NOTHING_CLOSED_TEXT = "Nothing was closed or written."


def _label(parent: QWidget, name: str, text: str) -> QLabel:
    label = QLabel(text, parent)
    label.setObjectName(name)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return make_selectable(label)


def process_text(running: bool | None) -> str:
    if running is True:
        return PROCESS_RUNNING_TEXT
    if running is False:
        return PROCESS_STOPPED_TEXT
    return PROCESS_UNKNOWN_TEXT


def api_text(state: str) -> str:
    return API_TEXT.get(state, API_ERROR_TEXT)


def results_text(query: str, count: int) -> str:
    """The line above the results. It names the query and says when the list is cut at the limit."""
    if count == 0:
        return NO_RESULTS_TEXT.format(query)
    text = RESULTS_TEXT.format(count, query)
    if count >= SEARCH_LIMIT:
        text += LIMIT_TEXT.format(SEARCH_LIMIT)
    return text


def start_dialog(parent: QWidget | None, command: str, folder: str) -> tuple[QMessageBox, QPushButton, QPushButton]:
    """The Start Zotero confirmation, built but not shown. Its default button is Cancel, so Enter does not start Zotero.

    The command and the working folder are shown as plain text, so the owner sees exactly what will run.
    """
    box = QMessageBox(parent)
    box.setWindowTitle("Start Zotero")
    box.setIcon(QMessageBox.Icon.Question)
    box.setTextFormat(Qt.TextFormat.PlainText)
    box.setText("Start Zotero on this computer?")
    box.setInformativeText(
        f"The desktop will run this command:\n\n{command}\n\n"
        f"Working folder: {folder}\n\n"
        "Zotero opens in its own window. The desktop changes no Zotero setting."
    )
    start = box.addButton("Start Zotero", QMessageBox.ButtonRole.AcceptRole)
    cancel = box.addButton(QMessageBox.StandardButton.Cancel)
    box.setDefaultButton(cancel)
    box.setEscapeButton(cancel)
    for child in box.findChildren(QLabel):
        child.setTextFormat(Qt.TextFormat.PlainText)
        make_selectable(child)
    return box, start, cancel


def ask_start(parent: QWidget | None, command: str, folder: str) -> bool:
    """Show the confirmation. True only when Start Zotero is chosen. Closing the dialog is a no."""
    box, start, _cancel = start_dialog(parent, command, folder)
    box.exec()
    return box.clickedButton() is start


@dataclass(frozen=True)
class _Done:
    """One finished read: its kind, the number it was started with, and the (result, failure_text) its job made."""

    kind: str
    generation: int
    payload: tuple


def _launch(client: Any, exe: str) -> tuple[str, bool]:
    """Start Zotero, then ask the local API once. Runs on a worker thread, and only after a yes."""
    line = client.start_zotero(exe)
    check = client.probe_api()
    return line, check.state == API_ON


def ask_licence(parent: QWidget | None, notice: str) -> bool:
    """The Zotero bridge licence notice in a dialog. True only when Accept is chosen. Closing the dialog is a no."""
    box = QMessageBox(parent)
    box.setWindowTitle(LICENCE_TITLE)
    box.setIcon(QMessageBox.Icon.Information)
    box.setTextFormat(Qt.TextFormat.PlainText)
    box.setText(notice)
    accept = box.addButton(LICENCE_ACCEPT_TEXT, QMessageBox.ButtonRole.AcceptRole)
    cancel = box.addButton(QMessageBox.StandardButton.Cancel)
    box.setDefaultButton(cancel)
    box.setEscapeButton(cancel)
    for child in box.findChildren(QLabel):
        child.setTextFormat(Qt.TextFormat.PlainText)
        make_selectable(child)
    box.exec()
    return box.clickedButton() is accept


def ask_bridge_install(parent: QWidget | None, text: str) -> bool:
    """The install confirmation: what will be written, and that Zotero must close. True only when Install is chosen."""
    box = QMessageBox(parent)
    box.setWindowTitle(INSTALL_TITLE)
    box.setIcon(QMessageBox.Icon.Question)
    box.setTextFormat(Qt.TextFormat.PlainText)
    box.setText(text)
    install = box.addButton(INSTALL_CONFIRM_BUTTON, QMessageBox.ButtonRole.AcceptRole)
    cancel = box.addButton(QMessageBox.StandardButton.Cancel)
    box.setDefaultButton(cancel)
    box.setEscapeButton(cancel)
    for child in box.findChildren(QLabel):
        child.setTextFormat(Qt.TextFormat.PlainText)
        make_selectable(child)
    box.exec()
    return box.clickedButton() is install


def ask_force_close(parent: QWidget | None) -> tuple[bool, bool]:
    """The force-close question. Returns (force, remember). ``force`` is True only for Yes, and ``remember`` only for Yes with
    "Force close next time" ticked. Closing the dialog, or No, is no, and No is the default button."""
    box = QMessageBox(parent)
    box.setWindowTitle(FORCE_TITLE)
    box.setIcon(QMessageBox.Icon.Warning)
    box.setTextFormat(Qt.TextFormat.PlainText)
    box.setText(FORCE_QUESTION_TEXT)
    remember = QCheckBox(FORCE_REMEMBER_TEXT, box)
    box.setCheckBox(remember)
    yes = box.addButton(FORCE_YES_TEXT, QMessageBox.ButtonRole.YesRole)
    no = box.addButton(FORCE_NO_TEXT, QMessageBox.ButtonRole.NoRole)
    box.setDefaultButton(no)
    box.setEscapeButton(no)
    for child in box.findChildren(QLabel):
        child.setTextFormat(Qt.TextFormat.PlainText)
        make_selectable(child)
    box.exec()
    forced = box.clickedButton() is yes
    return forced, forced and remember.isChecked()


def bridge_state_text(check: "zotero_bridge_install.BridgeCheck | None") -> str:
    """The one-line bridge state for the panel, with the registration note when Zotero needs the owner to act. Pure."""
    if check is None:
        return INSTALL_CHECKING_TEXT
    state = check.state
    if state == zotero_bridge_install.STATE_NO_BUNDLE:
        return BRIDGE_NO_BUNDLE_TEXT
    if state == zotero_bridge_install.STATE_DAMAGED:
        return BRIDGE_DAMAGED_TEXT
    if state == zotero_bridge_install.STATE_NO_PROFILE:
        return BRIDGE_NO_PROFILE_TEXT
    if state == zotero_bridge_install.STATE_UNREADABLE:
        return BRIDGE_UNREADABLE_TEXT
    bundled = check.bundled_version or ""
    installed = check.installed_version or ""
    if state == zotero_bridge_install.STATE_NOT_INSTALLED:
        text = BRIDGE_NOT_INSTALLED_TEXT.format(bundled)
    elif state == zotero_bridge_install.STATE_OLDER:
        text = BRIDGE_OLDER_TEXT.format(installed, bundled)
    elif state == zotero_bridge_install.STATE_UNKNOWN:
        text = BRIDGE_UNKNOWN_TEXT.format(bundled)
    elif state == zotero_bridge_install.STATE_CURRENT:
        text = BRIDGE_CURRENT_TEXT.format(installed)
    elif state == zotero_bridge_install.STATE_NEWER:
        text = BRIDGE_NEWER_TEXT.format(installed, bundled)
    else:
        return BRIDGE_CHECK_FAILED_TEXT
    note = {
        zotero_bridge_install.REG_NOT_LISTED: BRIDGE_REG_NOT_LISTED,
        zotero_bridge_install.REG_DISABLED: BRIDGE_REG_DISABLED,
        zotero_bridge_install.REG_BLOCKED: BRIDGE_REG_BLOCKED,
    }.get(check.registration, "")
    return f"{text} {note}" if note else text


def bridge_install_confirm_text(check: "zotero_bridge_install.BridgeCheck") -> str:
    """What the install confirmation says will be written, and that Zotero must close. Names no path. Pure."""
    update = check.state in (zotero_bridge_install.STATE_OLDER, zotero_bridge_install.STATE_UNKNOWN)
    action = "Update" if update else "Install"
    return (
        f"{action} the Zotero bridge {check.bundled_version} in your Zotero profile.\n\n"
        "Meridian will write:\n"
        "  - the bridge plugin file, in the profile's extensions folder;\n"
        "  - three lines in the profile's user.js: the scan setting, the bridge's local port, and the token file's location;\n"
        "  - the bridge's token file, readable only by your Windows account, if it does not exist yet.\n\n"
        "Zotero must close first. Meridian asks Zotero to close and waits up to 60 seconds. If Zotero does not close, "
        "nothing is written. Meridian then asks you before it forces Zotero to close, which can lose unsaved Zotero work, "
        "unless you have ticked Force close next time. Meridian does not start Zotero again.\n\n"
        "This build does not turn the bridge on in Zotero: if Zotero does not list it, turn it on once in Tools > Plugins.\n\n"
        f"{action} now?"
    )


def _licence_saved(store: Any) -> bool:
    """Whether the settings file records an acceptance of the notice shown now. Reads only. Runs on a worker thread. Never raises.

    An acceptance counts only with the SHA-256 of the current notice text beside it. An acceptance of an older notice, or one
    with no digest, is not accepted, so the owner is asked to read and accept the notice again.
    """
    if store is None:
        return False
    try:
        return licence_accepted_for(store.load().settings, zotero_bridge_install.LICENCE_NOTICE_SHA256)
    except Exception:  # noqa: BLE001 -- an unreadable settings file means the licence is not accepted
        return False


def _read_install_state(installer: Any, store: Any) -> tuple:
    """The bridge check and the licence acceptance, read together on a worker thread. Writes nothing."""
    return installer.check(), _licence_saved(store)


def _save_licence(store: Any) -> tuple:
    """Record the licence acceptance in the settings file. Runs on a worker thread. Returns (saved, plain reason)."""
    try:
        store.set_licence_accepted(True, notice_sha256=zotero_bridge_install.LICENCE_NOTICE_SHA256)
    except (SettingsUnreadable, ValueError) as exc:
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001 -- an OSError text can name a path, so only the class is shown
        return False, exc.__class__.__name__
    return True, ""


@dataclass(frozen=True)
class _InstallDone:
    """What one install run decided, on the worker thread. ``result`` is the installer's result, or None when the run stopped
    before the installer, with the reason in ``note``. ``force_allowed`` is whether a forced close was allowed for the run."""

    result: Any
    force_allowed: bool
    note: str = ""


def _force_close_allowed(store: Any) -> bool:
    """Whether the owner has opted in to a forced close. Reads only. Runs on a worker thread. Never raises."""
    if store is None:
        return False
    try:
        return bool(store.load().settings.zotero_force_close_allowed)
    except Exception:  # noqa: BLE001 -- an unreadable settings file means no opt-in
        return False


def _update_checks_on(store: Any) -> bool:
    """Whether the owner has turned the bridge update check on. Reads only. Never raises."""
    if store is None:
        return False
    try:
        return bool(store.load().settings.zotero_update_checks)
    except Exception:  # noqa: BLE001 -- an unreadable settings file means the check stays off
        return False


def _run_install(installer: Any, store: Any, *, force_now: bool = False) -> _InstallDone:
    """The install, run on a worker thread after a yes. The installer reads the licence itself and refuses without it.

    A forced close is allowed when the owner has opted in, or when this run is the owner's yes to the force-close question.
    Only that yes skips the normal close window (``skip_graceful``). An opt-in alone still waits the window first.
    """
    force = force_now or _force_close_allowed(store)
    result = installer.install(allow_close=True, allow_force_close=force, skip_graceful=force_now)
    return _InstallDone(result, force)


def _run_forced_install(installer: Any, store: Any, remember: bool) -> _InstallDone:
    """The install run after the owner's yes to the force-close question. With "Force close next time" ticked, the opt-in is
    stored first. If it cannot be stored, the install does not run, so nothing is closed or written."""
    if remember:
        try:
            store.set_zotero_force_close_allowed(True)
        except SettingsUnreadable as exc:
            return _InstallDone(None, False, f"{exc} {NOTHING_CLOSED_TEXT}")
        except Exception as exc:  # noqa: BLE001 -- an OSError text can name a path, so only the class is shown
            return _InstallDone(None, False, FORCE_NOT_SAVED_TEXT.format(exc.__class__.__name__))
    return _run_install(installer, store, force_now=True)


def _run_update_check(installer: Any, store: Any) -> Any:
    """The read-only bridge check for the update line, run on a worker thread. It runs only while the owner's switch is on.

    Returns None when the switch is off, so nothing is checked. Otherwise it returns the installer's read-only check.
    """
    if not _update_checks_on(store):
        return None
    return installer.check_for_update()


class _Job(QRunnable):
    """Run ``work`` on the thread pool and deliver (result, failure_text).

    A ZoteroError keeps its fixed message. Any other exception is reduced to its class name, so
    no traceback reaches the UI.
    """

    def __init__(self, work: Callable[[], Any], deliver: Callable[[tuple], None]) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._work = work
        self._deliver = deliver

    def run(self) -> None:
        result: Any = None
        failure: str | None = None
        try:
            result = self._work()
        except ZoteroError as exc:
            failure = exc.message
        except Exception as exc:  # noqa: BLE001 - a worker must never raise into the pool
            failure = CHECK_FAILED_TEXT.format(exc.__class__.__name__)
        try:
            self._deliver((result, failure))
        except RuntimeError:
            pass  # the shared delivery object is gone, for example at shutdown


class _JournalProgress:
    """Writes each item's outcome to the journal as it happens. It runs on the save's worker thread.

    An item is recorded as writing before its POST, and as landed, unknown or refused once its outcome is known. A quit in
    between leaves the item writing, and the next launch holds it.
    """

    def __init__(self, journal: zotero_journal.SaveJournal, lines: list[str]) -> None:
        self._journal = journal
        self._lines = lines
        self._ids: dict[int, str] = {}

    def started(self, index: int) -> None:
        self._ids[index] = self._journal.begin(self._lines[index])

    def finished(self, index: int, outcome: str) -> None:
        self._journal.finish(self._ids[index], outcome)


@dataclass(frozen=True)
class _JournalState:
    """What a read or a release of the journal found: the open journal, the held entries if the read finished, and any
    problem. ``held_lines`` names the lines a backup holds, when the journal cannot be read."""

    journal: zotero_journal.SaveJournal | None
    held: tuple[zotero_journal.JournalEntry, ...] | None
    problem: str | None = None
    busy: bool = False
    held_lines: tuple[str, ...] = ()


@dataclass(frozen=True)
class _SaveDone:
    """What a save run decided, on the worker thread: the result text, how much of the box to keep, the held entries once
    the run has written them, and any journal problem."""

    message: str
    keep_from: int
    held: tuple[zotero_journal.JournalEntry, ...] | None
    problem: str | None = None
    busy: bool = False


def _journal_state(path: Path, journal: zotero_journal.SaveJournal | None) -> _JournalState:
    """Open the journal if it is not open yet, and read what is held. Runs on a worker thread."""
    try:
        if journal is None:
            journal = zotero_journal.SaveJournal(path, read_timeout=zotero_journal.UI_READ_TIMEOUT_SECONDS)
        held = journal.held()
    except zotero_journal.JournalError as exc:
        return _JournalState(journal, None, exc.message, exc.busy, exc.held_lines)
    return _JournalState(journal, tuple(held))


def _clear_held(journal: zotero_journal.SaveJournal, entry_ids: list[str]) -> _JournalState:
    """Release the unknown entries the person checked, then read what is still held. Runs on a worker thread."""
    try:
        journal.clear(entry_ids)
        held = journal.held()
    except zotero_journal.JournalError as exc:
        return _JournalState(journal, None, exc.message, exc.busy, exc.held_lines)
    return _JournalState(journal, tuple(held))


def _hold_line(journal: zotero_journal.SaveJournal, line: str) -> tuple[str | None, bool]:
    """Make sure ``line`` is held. Returns the problem and whether it was busy; both empty when the line is held."""
    try:
        if any(entry.line == line for entry in journal.held()):
            return None, False
        journal.record_unknown([line])
    except zotero_journal.JournalError as exc:
        return exc.message, exc.busy
    return None, False


def _hold_lines(journal: zotero_journal.SaveJournal, lines: list[str]) -> tuple[str | None, bool]:
    """Hold every line of a save that produced no result. Returns the problem and whether it was busy."""
    try:
        journal.record_unknown(lines)
    except zotero_journal.JournalError as exc:
        return exc.message, exc.busy
    return None, False


def _record_save(journal: zotero_journal.SaveJournal, lines: list[str], result: Any) -> _SaveDone:
    """Hold what the save left unknown, then read what is held. A line leaves the box only when its journal entry was
    written, so a line whose entry failed stays in the box and is not posted again."""
    problem, busy = None, False
    if result is None:
        problem, busy = _hold_lines(journal, lines)
        keep_from = 0 if problem is not None else len(lines)
        base = SAVE_NO_RESULT_TEXT
    elif result.state == zotero_bridge.UNKNOWN and result.saved < len(lines):
        problem, busy = _hold_line(journal, lines[result.saved])
        keep_from = result.saved if problem is not None else result.saved + 1
        base = result.message
    else:
        keep_from = result.saved
        base = result.message
    message = base if problem is None else f"{problem} {base}"
    try:
        held = tuple(journal.held())
    except zotero_journal.JournalError as exc:
        return _SaveDone(message, keep_from, None, problem or exc.message, busy if problem else exc.busy)
    return _SaveDone(message, keep_from, held, problem, busy)


GROUP_FOLDER_TEXT = (
    "Nothing was saved. This project's folder is in a group library, and the Zotero bridge files items only into "
    "My Library. Choose a folder in My Library on the Zotero tab, then try again."
)
FOLDER_GONE_TEXT = (
    "Nothing was saved. This project's folder is no longer in the Zotero scope file, so the save would not go there. "
    "Choose the folder again on the Zotero tab, or clear it there to save without one, then try again."
)


SCOPE_UNREADABLE_SAVE_TEXT = (
    "Nothing was saved. The Zotero scope file could not be read, so the save cannot tell which folder this project "
    "uses. Fix or move that file in the desktop's settings folder, then try again."
)
SCOPE_SKIPPED_SAVE_TEXT = (
    "Nothing was saved. This project's folder entry in the Zotero scope file is not valid, so the save cannot tell "
    "where it goes. Choose the folder again on the Zotero tab, then try again."
)


def _save_placement(
    current: zotero_scope.ScopeRead,
    bound: frozenset[str] | set[str],
    project_id: str,
) -> tuple[str | None, str]:
    """(the collection key a save files into, a refusal). A project with no folder gives (None, "").

    ``current`` is the scope file read at save time, and it decides where a save goes. ``bound`` holds every project this
    window has seen the file bind, or hold an unusable entry for, and only its own clear removes one. A save refuses when
    the file is unreadable, since it cannot tell whether the project has a folder. It refuses when the file's entry for
    the project is not valid. It refuses when a bound project is no longer bound in the file, so nothing goes to the
    bridge's default collection by mistake. A group folder refuses, since the bridge files items only into My Library.
    """
    if current.unreadable:
        return None, SCOPE_UNREADABLE_SAVE_TEXT
    if not project_id:
        return None, ""
    if project_id in current.skipped:
        return None, SCOPE_SKIPPED_SAVE_TEXT
    scope = current.scopes.get(project_id)
    if scope is None:
        return (None, FOLDER_GONE_TEXT) if project_id in bound else (None, "")
    if scope.library_type != zotero_scope.LIBRARY_USER:
        return None, GROUP_FOLDER_TEXT
    return scope.collection_key, ""


def _run_save(
    journal: zotero_journal.SaveJournal,
    bridge: Any,
    items: list,
    lines: list[str],
    collection_key: str | None = None,
) -> _SaveDone:
    """One save, on a worker thread: the guard, the bridge, and the journal entries for what it did.

    The guard reads the journal under its lock, so a window whose list is out of date still refuses a line another window
    is writing. _JournalProgress writes each item's entry as the item starts and as its outcome is known.
    """
    try:
        held = journal.held()
    except zotero_journal.JournalError as exc:
        return _SaveDone(exc.message, 0, None, exc.message, exc.busy)
    if held:
        return _SaveDone(SAVE_HELD_TEXT, 0, tuple(held))
    progress = _JournalProgress(journal, lines)
    try:
        # A project with a folder files its saves there. Without one the bridge's own choice stands, as before.
        placed = {} if collection_key is None else {"collection_key": collection_key}
        result = bridge.save_items(items, progress=progress, **placed)
    except Exception:  # noqa: BLE001 - a save that raises has no result, so every line is held back
        result = None
    return _record_save(journal, lines, result)


class ZoteroPanel(QWidget):
    """The Zotero tab: status, Start Zotero behind a confirmation, and a read-only library search."""

    def __init__(
        self,
        client: Any = zotero_client,
        confirm: Callable[[QWidget, str, str], bool] | None = None,
        parent: QWidget | None = None,
        bridge: Any = zotero_bridge,
        journal_path: Path | None = None,
        projects: Callable[[], Sequence[tuple[str, str, str]]] | None = None,
        scopes_path: Path | None = None,
        bridge_installer: Any = None,
        settings: Any = None,
        licence_dialog: Callable[[QWidget, str], bool] | None = None,
        confirm_install: Callable[[QWidget, str], bool] | None = None,
        confirm_force_close: Callable[[QWidget], tuple[bool, bool]] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("zotero-panel")
        self._client = client
        self._bridge = bridge
        # The Meridian projects as (id, name, parent id), read on a worker. None means this window has no project list.
        self._projects_source = projects
        # Which collection each project searches. Read once here, then kept in step with the file by every save and clear.
        self._scopes_path = zotero_scope.default_path() if scopes_path is None else scopes_path
        self._scope_read = zotero_scope.read_scopes(self._scopes_path)
        # The projects this window has seen bound, or with an entry the file could not use. A save for one of them that the
        # file no longer binds is refused until this window chooses or clears its folder. Only a clear removes a project.
        self._bound_projects: set[str] = set()
        self._learn_bindings(self._scope_read)
        self._libraries: list[zotero_client.ZoteroLibrary] = []
        self._collections_by_key: dict[str, str] = {}
        self._pending_collection_key = ""
        self._pending_scope_note = ""
        self._action_text = ""
        self._load_texts: dict[str, str] = {}
        self._scope_loaded_once = False
        self._saving = False
        self._pending_lines: list[str] = []
        # The save journal. It is opened on a worker thread, so until it is open and read, Save is off.
        self._journal: zotero_journal.SaveJournal | None = None
        self._journal_path = zotero_journal.default_path() if journal_path is None else journal_path
        self._journal_loaded = False
        # A journal failure that stays for the rest of this run, so Save stays off. A busy journal is tried again.
        self._journal_problem: str | None = None
        self._journal_busy: str | None = None
        self._journal_retry = QTimer(self)
        self._journal_retry.setSingleShot(True)
        self._journal_retry.timeout.connect(self._start_journal_read)
        # The held entries from the last read that finished. A read that fails keeps them on screen.
        self._held_entries: list[zotero_journal.JournalEntry] = []
        self._confirm = ask_start if confirm is None else confirm
        # The Zotero bridge install. The installer reads and installs; the settings store keeps the licence acceptance and the
        # force-close opt-in. The installer reads the licence from the same store, so an install without one is refused.
        # Without a settings store the licence cannot be saved, so the install stays off in this window.
        self._settings = settings
        if bridge_installer is None:
            self._installer = zotero_bridge_install.ZoteroBridgeInstaller.for_this_build(settings=settings)
        else:
            self._installer = bridge_installer
        self._ask_licence = ask_licence if licence_dialog is None else licence_dialog
        self._ask_install = ask_bridge_install if confirm_install is None else confirm_install
        self._ask_force = ask_force_close if confirm_force_close is None else confirm_force_close
        self._bridge_check: zotero_bridge_install.BridgeCheck | None = None
        self._licence_accepted = False
        self._bridge_busy = False
        self._exe: str | None = None
        self._running: bool | None = None
        self._checking = False
        self._searching = False
        self._acting = False
        self._probed_on_show = False
        self._pending_query = ""
        # Identifies this panel's reads. A plain object, so a job can hold it without holding the panel.
        self._owner = object()
        # Each read takes the next number, and each kind keeps the number of its latest read. A result from an older
        # read of the same kind is dropped. Reads of different kinds never supersede each other.
        self._generation = 0
        self._latest: dict[str, int] = {}
        shared_delivery().delivered.connect(self._on_delivered)
        self._build()
        self._start_journal_read()
        # The bridge update check runs once per start, and again when this tab first opens. It does nothing while the switch
        # is off.
        self._start_update_check()

    # ----------------------------------------------------------- layout

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.addWidget(_label(self, "zotero-intro", INTRO_TEXT))

        self._installed = _label(self, "zotero-installed", NOT_CHECKED_TEXT)
        self._process = _label(self, "zotero-process", "")
        self._api = _label(self, "zotero-api", "")
        self._api_detail = _label(self, "zotero-api-detail", "")
        self._start_hint = _label(self, "zotero-start-hint", "")
        for label in (self._installed, self._process, self._api, self._api_detail, self._start_hint):
            layout.addWidget(label)

        actions = QHBoxLayout()
        self._refresh_button = QPushButton("Refresh", self)
        self._refresh_button.setObjectName("zotero-refresh")
        self._start_button = QPushButton("Start Zotero...", self)
        self._start_button.setObjectName("zotero-start")
        self._start_button.setEnabled(False)
        actions.addWidget(self._refresh_button)
        actions.addWidget(self._start_button)
        actions.addStretch(1)
        layout.addLayout(actions)

        self._start_result = _label(self, "zotero-start-result", "")
        layout.addWidget(self._start_result)

        self._build_bridge(layout)
        self._build_scope(layout)

        search_row = QHBoxLayout()
        self._search_input = QLineEdit(self)
        self._search_input.setObjectName("zotero-search-input")
        self._search_input.setPlaceholderText(SEARCH_PLACEHOLDER)
        self._search_button = QPushButton("Search", self)
        self._search_button.setObjectName("zotero-search-button")
        search_row.addWidget(self._search_input, 1)
        search_row.addWidget(self._search_button)
        layout.addLayout(search_row)

        self._search_status = _label(self, "zotero-search-status", SEARCH_HINT)
        layout.addWidget(self._search_status)

        self._results = QTableWidget(0, len(RESULT_COLUMNS), self)
        self._results.setObjectName("zotero-results")
        self._results.setHorizontalHeaderLabels(list(RESULT_COLUMNS))
        self._results.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._results.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        self._results.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._results.verticalHeader().setVisible(False)
        self._results.horizontalHeader().setStretchLastSection(True)
        enable_table_copy(self._results)
        layout.addWidget(self._results, 1)

        save_box = QVBoxLayout()
        save_box.addWidget(_label(self, "zotero-save-heading", SAVE_HEADING))
        save_box.addWidget(_label(self, "zotero-save-intro", SAVE_INTRO))
        self._save_input = QPlainTextEdit(self)
        self._save_input.setObjectName("zotero-save-input")
        self._save_input.setPlaceholderText(SAVE_PLACEHOLDER)
        self._save_input.setMinimumHeight(90)
        save_box.addWidget(self._save_input)
        save_row = QHBoxLayout()
        self._save_button = QPushButton(SAVE_BUTTON_TEXT, self)
        self._save_button.setObjectName("zotero-save-button")
        self._bridge_state = _label(self, "zotero-bridge-state", BRIDGE_CHECKING_TEXT)
        save_row.addWidget(self._save_button)
        save_row.addWidget(self._bridge_state, 1)
        save_box.addLayout(save_row)
        self._save_result = _label(self, "zotero-save-result", "")
        save_box.addWidget(self._save_result)
        self._held_heading = _label(self, "zotero-held-heading", HELD_HEADING)
        self._held_lines = QPlainTextEdit(self)
        self._held_lines.setObjectName("zotero-held-lines")
        self._held_lines.setReadOnly(True)
        make_selectable(self._held_lines)
        self._held_lines.setMinimumHeight(60)
        self._held_note = _label(self, "zotero-held-note", "")
        self._held_button = QPushButton(HELD_BUTTON_TEXT, self)
        self._held_button.setObjectName("zotero-held-checked")
        for widget in (self._held_heading, self._held_lines, self._held_note, self._held_button):
            save_box.addWidget(widget)
            widget.setVisible(False)
        layout.addLayout(save_box)

        self._refresh_button.clicked.connect(self._on_refresh_clicked)
        self._start_button.clicked.connect(self._on_start_clicked)
        self._search_button.clicked.connect(self._on_search_clicked)
        self._search_input.returnPressed.connect(self._on_search_clicked)
        self._save_button.clicked.connect(self._on_save_clicked)
        self._held_button.clicked.connect(self._on_held_checked_clicked)

    # ----------------------------------------------------------- project scope

    def _build_bridge(self, layout: QVBoxLayout) -> None:
        layout.addWidget(_label(self, "zotero-bridge-heading", BRIDGE_HEADING))
        layout.addWidget(_label(self, "zotero-bridge-intro", BRIDGE_INTRO))
        # The update check's switch and its line sit above the install state, so "below" in the line points at the buttons.
        self._update_checks = QCheckBox(UPDATE_CHECK_TEXT, self)
        self._update_checks.setObjectName("zotero-update-checks")
        self._update_checks.setChecked(_update_checks_on(self._settings))
        self._update_checks.setEnabled(self._settings is not None)
        self._update_line = _label(self, "zotero-bridge-update-line", "")
        self._update_line.setVisible(False)
        layout.addWidget(self._update_checks)
        layout.addWidget(self._update_line)
        self._update_checks.toggled.connect(self._on_update_checks_toggled)
        self._bridge_install_state = _label(self, "zotero-bridge-install-state", INSTALL_CHECKING_TEXT)
        self._bridge_licence_note = _label(self, "zotero-bridge-licence-note", "")
        self._bridge_licence_note.setVisible(False)
        layout.addWidget(self._bridge_install_state)
        layout.addWidget(self._bridge_licence_note)
        actions = QHBoxLayout()
        self._bridge_licence_button = QPushButton(LICENCE_BUTTON_TEXT, self)
        self._bridge_licence_button.setObjectName("zotero-bridge-licence")
        self._bridge_install_button = QPushButton(INSTALL_BUTTON_TEXT, self)
        self._bridge_install_button.setObjectName("zotero-bridge-install")
        for button in (self._bridge_licence_button, self._bridge_install_button):
            button.setVisible(False)
            actions.addWidget(button)
        actions.addStretch(1)
        layout.addLayout(actions)
        self._bridge_install_result = _label(self, "zotero-bridge-install-result", "")
        layout.addWidget(self._bridge_install_result)
        self._bridge_licence_button.clicked.connect(self._on_bridge_licence_clicked)
        self._bridge_install_button.clicked.connect(self._on_bridge_install_clicked)

    def _build_scope(self, layout: QVBoxLayout) -> None:
        layout.addWidget(_label(self, "zotero-scope-heading", SCOPE_HEADING))
        layout.addWidget(_label(self, "zotero-scope-intro", SCOPE_INTRO))
        grid = QGridLayout()
        grid.setColumnStretch(1, 1)
        self._scope_project = QComboBox(self)
        self._scope_project.setObjectName("zotero-scope-project")
        self._scope_library = QComboBox(self)
        self._scope_library.setObjectName("zotero-scope-library")
        self._scope_collection = QComboBox(self)
        self._scope_collection.setObjectName("zotero-scope-collection")
        rows = (
            (SCOPE_PROJECT_LABEL, self._scope_project),
            (SCOPE_LIBRARY_LABEL, self._scope_library),
            (SCOPE_COLLECTION_LABEL, self._scope_collection),
        )
        for row, (caption, combo) in enumerate(rows):
            grid.addWidget(_label(self, f"zotero-scope-caption-{row}", caption), row, 0)
            grid.addWidget(combo, row, 1)
        layout.addLayout(grid)

        actions = QHBoxLayout()
        self._scope_save = QPushButton(SCOPE_SAVE_TEXT, self)
        self._scope_save.setObjectName("zotero-scope-save")
        self._scope_clear = QPushButton(SCOPE_CLEAR_TEXT, self)
        self._scope_clear.setObjectName("zotero-scope-clear")
        self._scope_reload = QPushButton(SCOPE_RELOAD_TEXT, self)
        self._scope_reload.setObjectName("zotero-scope-reload")
        for button in (self._scope_save, self._scope_clear, self._scope_reload):
            actions.addWidget(button)
        actions.addStretch(1)
        layout.addLayout(actions)

        self._scope_current = _label(self, "zotero-scope-current", SCOPE_NO_PROJECT_TEXT)
        self._scope_note = _label(self, "zotero-scope-note", "")
        self._scope_note.setVisible(False)
        self._scope_status = _label(self, "zotero-scope-status", "")
        self._scope_status.setVisible(False)
        for widget in (self._scope_current, self._scope_note, self._scope_status):
            layout.addWidget(widget)

        self._scope_project.currentIndexChanged.connect(self._on_scope_project_changed)
        self._scope_library.currentIndexChanged.connect(self._on_scope_library_changed)
        self._scope_save.clicked.connect(self._on_scope_save_clicked)
        self._scope_clear.clicked.connect(self._on_scope_clear_clicked)
        self._scope_reload.clicked.connect(self._load_libraries)
        self._scope_library.setEnabled(False)
        self._scope_collection.setEnabled(False)

    def _scope_message(self, text: str) -> None:
        """An action's message (save, clear, a refused save). It shows above the load messages until the next change."""
        self._action_text = text
        self._render_scope_status()

    def _set_load_text(self, source: str, text: str) -> None:
        """One load's message (projects, libraries or collections). Each load keeps its own, so one cannot erase another."""
        self._load_texts[source] = text
        self._render_scope_status()

    def _render_scope_status(self) -> None:
        text = self._action_text or " ".join(item for item in self._load_texts.values() if item)
        self._scope_status.setText(text)
        self._scope_status.setVisible(bool(text))

    def _selected_project(self) -> str:
        value = self._scope_project.currentData()
        return value if isinstance(value, str) else ""

    def _scope_for(self, project_id: str) -> zotero_scope.ProjectScope | None:
        return self._scope_read.scopes.get(project_id) if project_id else None

    def _learn_bindings(self, read: zotero_scope.ScopeRead) -> None:
        """Remember every project the file binds or holds an unusable entry for. Only a clear forgets one."""
        self._bound_projects.update(read.scopes)
        self._bound_projects.update(read.skipped)

    def _current_library(self) -> zotero_client.ZoteroLibrary | None:
        key = self._scope_library.currentData()
        if not isinstance(key, str):
            return None
        for library in self._libraries:
            if f"{library.library_type}:{library.library_id}" == key:
                return library
        return None

    def _select_library(self, library_type: str, library_id: str) -> None:
        index = self._scope_library.findData(f"{library_type}:{library_id}")
        if index < 0:
            return
        self._scope_library.blockSignals(True)  # a binding shown on selection must not start a second load
        self._scope_library.setCurrentIndex(index)
        self._scope_library.blockSignals(False)

    def _update_current_label(self) -> None:
        project_id = self._selected_project()
        if not project_id:
            self._scope_current.setText(SCOPE_NO_PROJECT_TEXT)
            return
        name = self._scope_project.currentText()
        scope = self._scope_for(project_id)
        if scope is None:
            self._scope_current.setText(SCOPE_CURRENT_UNBOUND.format(name))
        else:
            self._scope_current.setText(SCOPE_CURRENT_BOUND.format(name, scope.collection_name, scope.library_name))

    def _load_projects(self) -> None:
        if self._projects_source is None:
            self._set_load_text("projects", SCOPE_NO_SOURCE_TEXT)
            return
        self._start_read(PROJECTS, self._projects_source)

    def _load_libraries(self) -> None:
        client = self._client
        self._set_load_text("libraries", SCOPE_LOADING_TEXT)
        self._start_read(LIBRARIES, lambda: client.list_libraries())

    def _load_collections(self) -> None:
        library = self._current_library()
        self._collections_by_key = {}
        self._scope_collection.setEnabled(False)
        self._scope_collection.blockSignals(True)
        self._scope_collection.clear()
        self._scope_collection.blockSignals(False)
        if library is None:
            self._scope_collection.addItem(SCOPE_NO_LIBRARY_TEXT)
            return
        self._scope_collection.addItem(SCOPE_LOADING_TEXT)
        client = self._client
        self._start_read(COLLECTIONS, lambda: client.list_collections(library))

    def _on_projects_ready(self, payload: tuple) -> None:
        result, failure = payload
        if failure is not None:
            self._set_load_text("projects", failure)
            return
        triples = [item for item in (result or []) if isinstance(item, tuple) and len(item) == 3]
        ordered = sorted(triples, key=lambda item: (item[1] or item[0]).lower())
        chosen = self._selected_project()
        self._scope_project.blockSignals(True)
        self._scope_project.clear()
        for project_id, name, _parent in ordered:
            self._scope_project.addItem(name or project_id, project_id)
        index = self._scope_project.findData(chosen) if chosen else -1
        self._scope_project.setCurrentIndex(index if index >= 0 else 0)
        self._scope_project.blockSignals(False)
        self._set_load_text("projects", "" if ordered else SCOPE_NO_PROJECTS_TEXT)
        self._on_scope_project_changed(self._scope_project.currentIndex())

    def _on_libraries_ready(self, payload: tuple) -> None:
        result, failure = payload
        if failure is not None or not isinstance(result, tuple) or len(result) != 2:
            self._libraries = []
            self._scope_library.blockSignals(True)
            self._scope_library.clear()
            self._scope_library.blockSignals(False)
            self._scope_library.setEnabled(False)
            self._scope_note.setVisible(False)
            self._set_load_text("libraries", failure or LIBRARIES_FAILED_TEXT)
            self._load_collections()
            return
        libraries, note = result
        self._libraries = list(libraries)
        chosen = self._scope_library.currentData()
        self._scope_library.blockSignals(True)
        self._scope_library.clear()
        for library in self._libraries:
            self._scope_library.addItem(library.name, f"{library.library_type}:{library.library_id}")
        index = self._scope_library.findData(chosen) if isinstance(chosen, str) else -1
        self._scope_library.setCurrentIndex(index if index >= 0 else 0)
        self._scope_library.blockSignals(False)
        self._scope_library.setEnabled(bool(self._libraries))
        self._scope_note.setText(note)
        self._scope_note.setVisible(bool(note))
        self._set_load_text("libraries", "")
        self._on_scope_project_changed(0)

    def _on_collections_ready(self, payload: tuple) -> None:
        result, failure = payload
        self._scope_collection.blockSignals(True)
        self._scope_collection.clear()
        self._collections_by_key = {}
        if failure is not None:
            self._scope_collection.addItem(COLLECTIONS_FAILED_TEXT)
            self._scope_collection.blockSignals(False)
            self._set_load_text("collections", failure)
            return
        rows = zotero_client.collection_rows(list(result or []))
        if not rows:
            self._scope_collection.addItem(SCOPE_NO_COLLECTIONS_TEXT)
            self._scope_collection.blockSignals(False)
            return
        for collection, depth in rows:
            self._collections_by_key[collection.key] = collection.name
            self._scope_collection.addItem("    " * depth + collection.name, collection.key)
        wanted = self._pending_collection_key
        self._pending_collection_key = ""
        index = self._scope_collection.findData(wanted) if wanted else -1
        if index >= 0:
            self._scope_collection.setCurrentIndex(index)
        self._scope_collection.setEnabled(True)
        self._scope_collection.blockSignals(False)
        self._set_load_text("collections", "")

    def _on_scope_project_changed(self, _index: int = 0) -> None:
        project_id = self._selected_project()
        self._scope_message("")  # an earlier save or clear message is about another project now
        self._update_current_label()
        scope = self._scope_for(project_id)
        if scope is not None:
            self._select_library(scope.library_type, scope.library_id)
            self._pending_collection_key = scope.collection_key
        else:
            self._pending_collection_key = ""
        self._load_collections()

    def _on_scope_library_changed(self, _index: int = 0) -> None:
        self._pending_collection_key = ""
        self._load_collections()

    def _on_scope_save_clicked(self) -> None:
        project_id = self._selected_project()
        library = self._current_library()
        key = self._scope_collection.currentData()
        if not project_id or library is None or not isinstance(key, str) or not self._scope_collection.isEnabled():
            self._scope_message(SCOPE_NEED_TEXT)
            return
        name = self._collections_by_key.get(key, "")
        try:
            scope = zotero_scope.ProjectScope(
                project_id=project_id,
                library_type=library.library_type,
                library_id=library.library_id,
                library_name=library.name,
                collection_key=key,
                collection_name=name,
            )
            read = zotero_scope.save_scope(scope, self._scopes_path)
        except zotero_scope.ScopeError as exc:
            self._scope_message(exc.message)
            return
        except OSError as exc:
            self._scope_message(NOT_SAVED_TEXT.format(exc.__class__.__name__))
            return
        self._scope_read = read
        self._learn_bindings(read)
        message = SCOPE_SAVED_TEXT.format(self._scope_project.currentText(), name, library.name)
        self._scope_message(f"{message} {read.warning}" if read.warning else message)
        self._update_current_label()

    def _on_scope_clear_clicked(self) -> None:
        project_id = self._selected_project()
        if not project_id:
            self._scope_message(SCOPE_NO_PROJECT_TEXT)
            return
        try:
            read = zotero_scope.clear_scope(project_id, self._scopes_path)
        except zotero_scope.ScopeError as exc:
            self._scope_message(exc.message)
            return
        except OSError as exc:
            self._scope_message(NOT_SAVED_TEXT.format(exc.__class__.__name__))
            return
        self._scope_read = read
        self._learn_bindings(read)
        self._bound_projects.discard(project_id)
        message = SCOPE_CLEARED_TEXT.format(self._scope_project.currentText())
        self._scope_message(f"{message} {read.warning}" if read.warning else message)
        self._update_current_label()

    # ----------------------------------------------------------- status

    def showEvent(self, event: Any) -> None:  # noqa: N802 - Qt naming
        super().showEvent(event)
        if not self._probed_on_show:
            self._probed_on_show = True
            self._refresh()
            self._load_projects()
            self._load_libraries()
            self._start_update_check()

    @Slot()
    def _on_refresh_clicked(self) -> None:
        self._refresh()

    def _refresh(self) -> None:
        if self._checking:
            return
        self._checking = True
        self._refresh_button.setEnabled(False)
        self._start_button.setEnabled(False)
        self._installed.setText(CHECKING_TEXT)
        self._process.setText("")
        self._api.setText("")
        self._api_detail.setText("")
        self._start_hint.setText("")
        self._start_read(STATUS, self._client.status)
        self._start_bridge_check()
        self._start_install_check()

    def _start_bridge_check(self) -> None:
        self._bridge_state.setText(BRIDGE_CHECKING_TEXT)
        self._start_read(BRIDGE, self._bridge.bridge_status)

    def _start_read(self, kind: str, work: Callable[[], Any]) -> None:
        """Start one read on the thread pool. Its result comes back to _on_delivered, tagged with kind and number."""
        self._generation += 1
        generation = self._generation
        self._latest[kind] = generation
        # The deliver closure captures only the owner, the kind, the number and the delivery object, never the panel.
        owner = self._owner
        delivery = shared_delivery()

        def deliver(payload: tuple) -> None:
            delivery.delivered.emit(Delivered(owner, _Done(kind, generation, payload)))

        QThreadPool.globalInstance().start(_Job(work, deliver))

    @Slot(object)
    def _on_delivered(self, item: object) -> None:
        # Runs on the GUI thread. A result for another panel is dropped here, and so is one a newer read of its kind
        # has superseded.
        if not isinstance(item, Delivered) or item.owner is not self._owner:
            return
        done = item.value
        if not isinstance(done, _Done) or done.generation != self._latest.get(done.kind):
            return
        if done.kind == STATUS:
            self._on_status_ready(done.payload)
        elif done.kind == SEARCH:
            self._on_search_ready(done.payload)
        elif done.kind == ACTION:
            self._on_action_ready(done.payload)
        elif done.kind == BRIDGE:
            self._on_bridge_ready(done.payload)
        elif done.kind == SAVE:
            self._on_save_ready(done.payload)
        elif done.kind == JOURNAL:
            self._on_journal_ready(done.payload)
        elif done.kind == CLEAR:
            self._on_clear_ready(done.payload)
        elif done.kind == PROJECTS:
            self._on_projects_ready(done.payload)
        elif done.kind == LIBRARIES:
            self._on_libraries_ready(done.payload)
        elif done.kind == COLLECTIONS:
            self._on_collections_ready(done.payload)
        elif done.kind == BRIDGE_INSTALL_STATE:
            self._on_install_state_ready(done.payload)
        elif done.kind == BRIDGE_LICENCE:
            self._on_licence_ready(done.payload)
        elif done.kind == BRIDGE_INSTALL:
            self._on_install_ready(done.payload)
        elif done.kind == BRIDGE_UPDATE:
            self._on_update_ready(done.payload)

    def _on_status_ready(self, payload: tuple) -> None:
        result, failure = payload
        self._checking = False
        self._refresh_button.setEnabled(True)
        if failure is not None or not isinstance(result, ZoteroStatus):
            self._exe = None
            self._running = None
            self._installed.setText(NOT_CHECKED_TEXT)
            self._process.setText("")
            self._api.setText(API_ERROR_TEXT)
            self._api_detail.setText(failure or CHECK_FAILED_TEXT.format("NoResult"))
            self._start_hint.setText("")
            self._start_button.setEnabled(False)
            return
        self._show_status(result)

    def _show_status(self, status: ZoteroStatus) -> None:
        self._exe = status.exe
        self._running = status.process_running
        self._installed.setText(NOT_INSTALLED_TEXT if status.exe is None else INSTALLED_TEXT.format(status.exe))
        self._process.setText(process_text(status.process_running))
        self._api.setText(api_text(status.api.state))
        self._api_detail.setText(status.api.message)
        self._update_start_button()

    def _update_start_button(self) -> None:
        if self._exe is None:
            hint, enabled = START_HINT_NOT_INSTALLED, False
        elif self._running is True:
            hint, enabled = START_HINT_RUNNING, False
        else:
            hint, enabled = START_HINT_READY, True
        self._start_hint.setText(hint)
        self._start_button.setEnabled(enabled and not self._acting)

    # ----------------------------------------------------------- start

    @Slot()
    def _on_start_clicked(self) -> None:
        exe = self._exe
        if exe is None or self._acting or self._running is True:
            return
        command = command_line(exe)
        folder = str(Path(exe).parent)
        if not self._confirm(self, command, folder):
            self._start_result.setText(START_CANCELLED_TEXT)
            return
        self._acting = True
        self._start_button.setEnabled(False)
        self._start_result.setText(STARTING_TEXT)
        self._start_read(ACTION, functools.partial(_launch, self._client, exe))

    def _on_action_ready(self, payload: tuple) -> None:
        result, failure = payload
        self._acting = False
        if failure is not None or not isinstance(result, tuple):
            self._start_result.setText(failure or CHECK_FAILED_TEXT.format("NoResult"))
            self._update_start_button()
            return
        line, ready = result
        ready_text = API_READY_TEXT if ready else API_NOT_READY_TEXT
        self._start_result.setText(f"{line}\n{ready_text}")
        self._refresh()

    # ----------------------------------------------------------- search

    @Slot()
    def _on_search_clicked(self) -> None:
        query = self._search_input.text().strip()
        if not query:
            self._search_status.setText(BLANK_TEXT)
            return
        if self._searching:
            return
        self._searching = True
        self._pending_query = query
        self._search_button.setEnabled(False)
        self._results.setRowCount(0)
        project_id = self._selected_project()
        scope = self._scope_for(project_id)
        if scope is None:
            # With a project picked but no collection set, the note says so; with no project picked, nothing is added.
            self._pending_scope_note = SCOPE_SEARCH_WHOLE if project_id else ""
            self._search_status.setText(SEARCHING_TEXT)
            self._start_read(SEARCH, functools.partial(self._client.search_items, query))
            return
        client = self._client
        library = zotero_client.ZoteroLibrary(scope.library_type, scope.library_id, scope.library_name)
        key = scope.collection_key
        self._pending_scope_note = SCOPE_SEARCH_IN.format(scope.collection_name, scope.library_name)
        self._search_status.setText(SCOPE_SEARCHING_TEXT.format(scope.collection_name, scope.library_name))
        self._start_read(SEARCH, lambda: client.search_collection_items(query, library, key))

    def _on_search_ready(self, payload: tuple) -> None:
        result, failure = payload
        self._searching = False
        self._search_button.setEnabled(True)
        if failure is not None:
            self._results.setRowCount(0)
            self._search_status.setText(failure)
            return
        items: list[ZoteroItem] = list(result or [])
        self._fill_results(items)
        self._search_status.setText(results_text(self._pending_query, len(items)) + self._pending_scope_note)

    def _fill_results(self, items: list[ZoteroItem]) -> None:
        self._results.setRowCount(len(items))
        for row, item in enumerate(items):
            for column, value in enumerate((item.title, item.authors, item.year, item.key)):
                cell = QTableWidgetItem(value)
                cell.setFlags(cell.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self._results.setItem(row, column, cell)

    # ----------------------------------------------------------- bridge install

    def _start_install_check(self) -> None:
        """Read the bridge state and the licence acceptance on the thread pool. Writes nothing."""
        self._start_read(BRIDGE_INSTALL_STATE, functools.partial(_read_install_state, self._installer, self._settings))

    def _on_install_state_ready(self, payload: tuple) -> None:
        result, failure = payload
        if failure is not None or not isinstance(result, tuple) or len(result) != 2:
            self._bridge_check = None
            self._licence_accepted = False
            self._bridge_install_state.setText(BRIDGE_CHECK_FAILED_TEXT)
            self._bridge_install_button.setVisible(False)
            self._bridge_licence_button.setVisible(False)
            self._bridge_licence_note.setVisible(False)
            return
        check, accepted = result
        self._bridge_check = check
        self._licence_accepted = bool(accepted)
        self._render_bridge()

    def _render_bridge(self) -> None:
        """Show the bridge state, and the buttons that state allows. Install needs the licence accepted and no install running."""
        check = self._bridge_check
        self._bridge_install_state.setText(bridge_state_text(check))
        state = check.state if check is not None else None
        installable = state in (
            zotero_bridge_install.STATE_NOT_INSTALLED,
            zotero_bridge_install.STATE_OLDER,
            zotero_bridge_install.STATE_UNKNOWN,
        )
        self._bridge_install_button.setText(
            UPDATE_BUTTON_TEXT
            if state in (zotero_bridge_install.STATE_OLDER, zotero_bridge_install.STATE_UNKNOWN)
            else INSTALL_BUTTON_TEXT
        )
        self._bridge_install_button.setVisible(installable)
        self._bridge_install_button.setEnabled(installable and self._licence_accepted and not self._bridge_busy)
        licence_needed = installable and not self._licence_accepted
        self._bridge_licence_button.setVisible(licence_needed)
        self._bridge_licence_button.setEnabled(licence_needed and not self._bridge_busy)
        note = ""
        if licence_needed:
            note = LICENCE_NEEDED_TEXT if self._settings is not None else LICENCE_NO_STORE_TEXT
        self._bridge_licence_note.setText(note)
        self._bridge_licence_note.setVisible(bool(note))

    @Slot()
    def _on_bridge_licence_clicked(self) -> None:
        if self._bridge_busy or self._licence_accepted:
            return
        if self._settings is None:
            self._bridge_install_result.setText(LICENCE_NO_STORE_TEXT)
            return
        if not self._ask_licence(self, zotero_bridge_install.LICENCE_NOTICE_TEXT):
            self._bridge_install_result.setText(LICENCE_DECLINED_TEXT)
            return
        self._bridge_busy = True
        self._render_bridge()
        self._start_read(BRIDGE_LICENCE, functools.partial(_save_licence, self._settings))

    def _on_licence_ready(self, payload: tuple) -> None:
        result, failure = payload
        self._bridge_busy = False
        if failure is not None or not isinstance(result, tuple) or len(result) != 2:
            self._bridge_install_result.setText(LICENCE_SAVE_FAILED_TEXT.format(failure or "NoResult"))
        else:
            saved, reason = result
            if saved:
                self._licence_accepted = True
                self._bridge_install_result.setText(LICENCE_ACCEPTED_TEXT)
                # A state read that started before this save may still be running, and it would report the licence as not
                # accepted. Starting the check again makes that older read superseded, so _on_delivered drops it.
                self._start_install_check()
            else:
                self._bridge_install_result.setText(LICENCE_SAVE_FAILED_TEXT.format(reason))
        self._render_bridge()

    @Slot()
    def _on_bridge_install_clicked(self) -> None:
        check = self._bridge_check
        if self._bridge_busy or check is None or not self._licence_accepted:
            return
        if not self._ask_install(self, bridge_install_confirm_text(check)):
            self._bridge_install_result.setText(INSTALL_CANCELLED_TEXT)
            return
        self._bridge_busy = True
        self._render_bridge()
        self._bridge_install_result.setText(INSTALLING_TEXT)
        self._start_read(BRIDGE_INSTALL, functools.partial(_run_install, self._installer, self._settings))

    def _on_install_ready(self, payload: tuple) -> None:
        result, failure = payload
        self._bridge_busy = False
        done = result if isinstance(result, _InstallDone) else None
        install = done.result if done is not None else None
        ask_force = False
        if failure is not None:
            # The job reduces an exception to a check-failed text, which is wrong for an install. Show the install's own text.
            self._bridge_install_result.setText(INSTALL_FAILED_TEXT)
        elif done is None:
            self._bridge_install_result.setText(INSTALL_NO_RESULT_TEXT)
        elif install is None:
            # The run stopped before the installer, for example because the force-close opt-in could not be stored.
            self._bridge_install_result.setText(done.note)
        elif not isinstance(install, zotero_bridge_install.InstallResult):
            self._bridge_install_result.setText(INSTALL_NO_RESULT_TEXT)
        else:
            self._bridge_install_result.setText(install.message)
            # The owner is asked only when a forced close could still help and the opt-in is not already on.
            ask_force = install.can_force_close and not done.force_allowed
            if install.status == zotero_bridge_install.ST_INSTALLED:
                # The bundled bridge is now in place, so no update is pending. Zotero's extensions.json can still list the old
                # version until Zotero starts again, so the line is hidden here, not recomputed from that older entry.
                self._update_line.setVisible(False)
        # The state is read again, so the buttons follow what is now on disk.
        self._bridge_check = None
        self._render_bridge()
        self._start_install_check()
        if ask_force:
            self._offer_force_close()

    def _offer_force_close(self) -> None:
        """Ask whether to force Zotero closed. Yes runs the install again, with a forced close allowed and the normal close window
        skipped, since the owner has just said yes. No writes nothing.

        "Force close next time" is stored first, by the worker, when it is ticked with Yes.
        """
        force, remember = self._ask_force(self)
        if not force:
            return
        self._bridge_busy = True
        self._render_bridge()
        self._bridge_install_result.setText(FORCING_TEXT)
        self._start_read(BRIDGE_INSTALL, functools.partial(_run_forced_install, self._installer, self._settings, remember))

    # ----------------------------------------------------------- bridge update check

    def _start_update_check(self) -> None:
        """The bridge update check, on the thread pool. It runs only while the owner's switch is on, and it only reads."""
        self._start_read(BRIDGE_UPDATE, functools.partial(_run_update_check, self._installer, self._settings))

    def _on_update_ready(self, payload: tuple) -> None:
        """Show the line when the installed bridge is older than the bundled one. Any other outcome, a failed check included,
        shows nothing."""
        result, failure = payload
        newer = (
            failure is None
            and isinstance(result, zotero_bridge_install.BridgeCheck)
            and result.state == zotero_bridge_install.STATE_OLDER
        )
        if newer:
            self._update_line.setText(
                UPDATE_AVAILABLE_TEXT.format(bundled=result.bundled_version or "", installed=result.installed_version or "")
            )
        self._update_line.setVisible(newer)

    def _on_update_checks_toggled(self, checked: bool) -> None:
        """Save the update-check switch. Turning it off hides the line. A save that fails puts the switch back and says why."""
        if not checked:
            self._update_line.setVisible(False)
        if self._settings is None:
            return
        try:
            self._settings.set_zotero_update_checks(bool(checked))
        except SettingsUnreadable as exc:
            self._revert_update_checks(not checked, str(exc))
        except Exception as exc:  # noqa: BLE001 -- an OSError text can name a path, so only the class is shown
            self._revert_update_checks(not checked, UPDATE_CHECK_NOT_SAVED_TEXT.format(exc.__class__.__name__))

    def _revert_update_checks(self, checked: bool, text: str) -> None:
        self._update_checks.blockSignals(True)
        self._update_checks.setChecked(checked)
        self._update_checks.blockSignals(False)
        self._bridge_install_result.setText(text)

    # ----------------------------------------------------------- bridge and save

    def _on_bridge_ready(self, payload: tuple) -> None:
        result, failure = payload
        if failure is not None or not isinstance(result, zotero_bridge.BridgeStatus):
            self._bridge_state.setText(failure or CHECK_FAILED_TEXT.format("NoResult"))
            return
        self._bridge_state.setText(result.message)

    # ----------------------------------------------------------- journal

    def _start_journal_read(self) -> None:
        """Open the journal if it is not open yet, and read what is held. The read runs on a worker thread."""
        self._start_read(JOURNAL, functools.partial(_journal_state, self._journal_path, self._journal))

    def _on_journal_ready(self, payload: tuple) -> None:
        state, failure = payload
        if failure is not None or not isinstance(state, _JournalState):
            state = _JournalState(self._journal, None, failure or JOURNAL_CHECK_FAILED_TEXT)
        self._apply_journal_state(state)

    def _apply_journal_state(self, state: _JournalState) -> None:
        """Show what a read of the journal found. A read that did not finish keeps the held entries of the last one that did."""
        self._journal_loaded = True
        if state.journal is not None:
            self._journal = state.journal
        if state.problem is None:
            self._journal_busy = None
        else:
            self._note_journal_problem(state.problem, state.busy)
            self._save_result.setText(state.problem)
        if state.held is not None:
            self._show_held(state.held)
        elif state.held_lines and self._journal is None:
            # The journal cannot be read, so the lines its backup holds are shown. Nothing can release them from here.
            self._show_held_lines(state.held_lines, can_clear=False)
        self._update_save_enabled()

    def _note_journal_problem(self, problem: str, busy: bool) -> None:
        """A busy journal is tried again in a moment. Any other problem stays for the rest of this run."""
        if busy:
            self._journal_busy = problem
            self._journal_retry.start(JOURNAL_RETRY_MS)
        else:
            self._journal_problem = problem
        self._update_save_enabled()

    def _show_held(self, entries: Sequence[zotero_journal.JournalEntry]) -> None:
        """Show the held lines, read from the journal on disk. They survive quitting, so they show again on every launch."""
        self._held_entries = list(entries)
        self._show_held_lines([entry.line for entry in self._held_entries], can_clear=bool(self._held_entries))
        writing = any(entry.outcome == zotero_journal.WRITING for entry in self._held_entries)
        self._held_note.setText(WRITING_ELSEWHERE_TEXT if writing else "")
        self._held_note.setVisible(writing)
        self._update_save_enabled()

    def _show_held_lines(self, lines: Sequence[str], *, can_clear: bool) -> None:
        self._held_lines.setPlainText("\n".join(lines))
        shown = bool(lines)
        self._held_heading.setVisible(shown)
        self._held_lines.setVisible(shown)
        self._held_button.setVisible(shown and can_clear)

    def _update_save_enabled(self) -> None:
        """Save is off while the journal is not open or cannot be used, while it is busy, while any line is held, and while
        a save is running."""
        self._save_button.setEnabled(
            self._journal is not None
            and self._journal_problem is None
            and self._journal_busy is None
            and not self._held_entries
            and not self._saving
        )

    @Slot()
    def _on_held_checked_clicked(self) -> None:
        if self._journal is None:
            return
        entry_ids = [entry.id for entry in self._held_entries]
        self._start_read(CLEAR, functools.partial(_clear_held, self._journal, entry_ids))

    def _on_clear_ready(self, payload: tuple) -> None:
        state, failure = payload
        if failure is not None or not isinstance(state, _JournalState):
            state = _JournalState(self._journal, None, failure or JOURNAL_CHECK_FAILED_TEXT)
        self._apply_journal_state(state)
        if state.problem is None:
            still_writing = any(entry.outcome == zotero_journal.WRITING for entry in state.held or ())
            self._save_result.setText(STILL_WRITING_TEXT if still_writing else HELD_RELEASED_TEXT)

    @Slot()
    def _on_save_clicked(self) -> None:
        if self._saving:
            return
        if self._journal is None:
            self._save_result.setText(self._journal_problem or self._journal_busy or JOURNAL_LOADING_TEXT)
            return
        if self._journal_problem is not None:
            self._save_result.setText(self._journal_problem)
            return
        text = self._save_input.toPlainText()
        try:
            items = self._bridge.parse_lines(text)
        except zotero_bridge.BridgeInputError as exc:
            self._save_result.setText(exc.message)
            return
        # parse_lines makes one item per non-blank line, in order, so these lines line up with the items.
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        # The file decides where a save goes. This window's copy and its label follow the file, and the projects the file
        # binds are remembered, so a later save still notices a folder that has gone.
        current = zotero_scope.read_scopes(self._scopes_path)
        self._scope_read = current
        self._learn_bindings(current)
        self._update_current_label()
        collection_key, refusal = _save_placement(current, self._bound_projects, self._selected_project())
        if refusal:
            self._save_result.setText(refusal)
            return
        self._pending_lines = lines
        self._saving = True
        self._save_input.setReadOnly(True)
        self._update_save_enabled()
        self._save_result.setText(SAVING_TEXT)
        # The guard, the bridge and every journal write run on the worker, so the window never waits on the journal lock.
        self._start_read(
            SAVE,
            functools.partial(_run_save, self._journal, self._bridge, items, lines, collection_key),
        )

    def _on_save_ready(self, payload: tuple) -> None:
        done, failure = payload
        lines, self._pending_lines = self._pending_lines, []
        self._saving = False
        self._save_input.setReadOnly(False)
        if failure is not None or not isinstance(done, _SaveDone):
            # The save ended without a result the worker could record. Every line stays in the box, and the journal is read
            # again, so that whatever it did record is held.
            self._save_input.setPlainText("\n".join(lines))
            self._save_result.setText(SAVE_NO_RESULT_TEXT)
            self._start_journal_read()
            self._update_save_enabled()
            self._start_bridge_check()
            return
        # The lines before keep_from landed, or their journal entry was written, so they leave the box. The rest stay.
        self._save_input.setPlainText("\n".join(lines[done.keep_from :]))
        self._save_result.setText(done.message)
        if done.problem is not None:
            self._note_journal_problem(done.problem, done.busy)
        if done.held is not None:
            self._show_held(done.held)
        self._update_save_enabled()
        self._start_bridge_check()


__all__ = [
    "API_TEXT",
    "CHECKING_TEXT",
    "NOT_CHECKED_TEXT",
    "START_CANCELLED_TEXT",
    "ZoteroPanel",
    "api_text",
    "ask_start",
    "process_text",
    "results_text",
    "start_dialog",
]
