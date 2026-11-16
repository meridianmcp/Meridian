"""The Keys block on the Keys tab (ui/keys_tab.py): RunPod, a search box, one card per saved name, a keys file control, and an
add form. Qt code.

The RunPod entry is the RunPod panel's own card (ui/runpod_panel.py), so its checks, its pod list and its key row work as
they did before. Every other name is a card of its own: its status, a password box for a new value, Save value and Remove.
A name is added with the form at the foot of the block. The search box above the list shows the cards whose name, known
service description, group label or subgroup label contains its text, ignoring case. It never searches values or statuses.

Each card, RunPod's included, has a Mark critical box and a Set as Windows user variable button. A critical name is listed
first and shows a Critical badge. The mark is saved in settings by name. Setting a user variable asks first, and then writes
the saved value to the Windows user environment.

Values go to the keychain through api_keys, and this block never writes one to the settings file. A value is read back only
when the owner presses Reveal on one card. It then shows in a read-only field, and it is masked again after 30 seconds or at
once with Hide value. Copy value puts it on the clipboard, which is cleared 30 seconds later if it still holds that value.
No value is read at startup or into a list. A status is read only to test whether a value is set. A failed read shows as
unreadable. Remove asks first, because the saved value is deleted from the keychain.

A keys file the owner chose lists more names (keys_file.py). Each one is a card marked From file, with no value box: its
value stays in the file. Each refresh opens the file and reads every entry, value included, to check it. Those values are
held only for that check and then dropped: only the names are kept and listed. A value is sent only when its Check is
pressed, for that one request. The app never writes the file. Remove on
such a name only takes it off the list for this session, and says the file is unchanged. A name that the keychain also holds
shows once, as the keychain
name. Each other imported name is set automatically: critical when it is a known service or a built-in name, non-critical
otherwise. The list groups them under those two headings, and a critical name shows the Critical badge. A known-service or
built-in name that is imported is marked critical in settings at the import, so its keychain card keeps the badge its file
card showed. Any other imported name is not marked.

A name that matches a known service (key_services) also shows what its key is for and where it is made. When that
service has a check, its card has a Check button. The check runs only when the owner presses it, on the thread pool,
and shows one fixed message. The form can pick a service, which fills in the name. Other leaves the name as typed.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Callable
from dataclasses import dataclass, replace
from itertools import count
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QTimer, Slot
from PySide6.QtGui import QClipboard, QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from meridian_desktop.api_keys import (
    BUILT_IN_NAMES,
    RUNPOD_NAME,
    STATUS_NOT_SET,
    STATUS_SET,
    STATUS_UNREADABLE,
    ApiKeys,
    KeyNameError,
    KEY_SERVICE,
    account_for,
    slug,
)
from meridian_desktop.key_services import (
    NO_CHECK_TEXT,
    SERVICES,
    UNREACHABLE_TEXT,
    KeyService,
    Transport,
    check_saved_key,
    service_for,
)
from meridian_desktop.key_vault import VAULT_FILE_NAME
from meridian_desktop.keys_export import write_names_export
from meridian_desktop.keys_file import (
    NOT_SUPPORTED_TEXT,
    FileNames,
    file_format,
    is_classified,
    log_listing,
    read_names,
    skipped_summary,
    split_by_classification,
)
from meridian_desktop.secret_store import SecretStore, SecretStoreError
from meridian_desktop.settings_store import SettingsStore
from meridian_desktop.ui.confirm import ask_yes_no
from meridian_desktop.ui.delivery import Delivered, shared_delivery
from meridian_desktop.ui.key_export_dialog import KeyExportDialog, PathChooser
from meridian_desktop.ui.key_import_dialog import KeyImportDialog
from meridian_desktop.ui.key_import_plain_dialog import (
    PLAIN_QUESTION_TEXT,
    PLAIN_QUESTION_TITLE,
    KeyPlainImportDialog,
    ask_plain_import,
)
from meridian_desktop.ui.key_vault_box import REVEAL_TEXT, KeyVaultBox
from meridian_desktop.ui.runner_actions import submit
from meridian_desktop.projects_backup import BACKUP_DIR_NAME, utc_now
from meridian_desktop.ui.text import make_selectable
from meridian_desktop.user_environment import UserEnvironmentError

INTRO_TEXT = (
    "Keys are kept in the OS keychain, one entry for each name. This list shows each name and whether a value is set. "
    "A value is never shown here. Type a new value to replace one."
)
ADD_HEADING_TEXT = "Add a key"
NAME_PLACEHOLDER = "Name, for example OpenAI"
VALUE_PLACEHOLDER = "Key value"
NEW_VALUE_PLACEHOLDER = "New value, to replace the saved one"
SEARCH_PLACEHOLDER = "Search keys"
# Shown when no name matches the search. It does not repeat the search text, so nothing the owner typed is echoed back.
SEARCH_EMPTY_TEXT = "No key name matches the search. Clear the search to see every name."
ADD_TEXT = "Add key"
SAVE_VALUE_TEXT = "Save value"
REMOVE_TEXT = "Remove"
REMOVE_TITLE = "Remove this key?"
REMOVE_BODY = (
    "The saved value for {name} is deleted from the keychain. A Windows user variable set for it is not changed, "
    "and it stays until you remove it in Windows environment settings. {listing} This cannot be undone."
)
REMOVE_LISTING = "The name also leaves the list."
CANCELLED_TEXT = "Cancelled. Nothing was removed."
STATUS_TEXT = {
    STATUS_SET: "A value is set.",
    STATUS_NOT_SET: "No value is set.",
    STATUS_UNREADABLE: "The keychain could not be read, so the state is not known.",
}
ADDED_TEXT = "Saved {name}. Its value is in the keychain and is not shown."
REPLACED_TEXT = "Saved the new value for {name}. It is not shown."
NO_VALUE_TEXT = "Type the new value first, then press Save value."
REMOVED_TEXT = "Removed {name} and its saved value."
REMOVED_NO_VALUE_TEXT = "Removed {name} from the list. No value was saved for it."
NOT_REMOVED_TEXT = "Not removed. {detail}"
# A removal that failed on the disk or in the keychain. The message names no path and no value.
NOT_REMOVED_OS_TEXT = "Not removed. The key could not be deleted or its name could not be written. Try again."
# The group label on each keychain card, and the editor that changes it. A label is text the owner typed, never a value.
GROUP_TEXT = "Group: {group}"
GROUP_PLACEHOLDER = "Group, for example AI"
GROUP_SAVE_TEXT = "Save group"
GROUP_SAVED_TEXT = "{name} is in the group {group}."
# The subgroup line on each keychain card, and the editor that changes it. Keys that only work as a set share one subgroup.
SUBGROUP_TEXT = "Subgroup: {subgroup}"
# Shown in place of a subgroup name on a card with no subgroup. The search never matches this text, only the label itself.
NO_SUBGROUP_TEXT = "none"
SUBGROUP_PLACEHOLDER = "Subgroup, for example AWS keys"
SUBGROUP_SAVE_TEXT = "Save subgroup"
SUBGROUP_SAVED_TEXT = "{name} is in the subgroup {subgroup}."
SUBGROUP_CLEARED_TEXT = "{name} is no longer in a subgroup."
NAMES_NOT_READ_TEXT = "The saved key names could not be read. Reopen Settings to try again."
NOT_WRITTEN_TEXT = "Not saved: the settings file could not be written."
RUNPOD_HEADING_TEXT = RUNPOD_NAME
CRITICAL_BADGE_TEXT = "Critical"
CRITICAL_BOX_TEXT = "Mark critical"
CRITICAL_TEXT = "{name} is marked critical. It is listed first."
UNCRITICAL_TEXT = "{name} is no longer marked critical."
USER_ENV_TEXT = "Set as Windows user variable"
USER_ENV_TITLE = "Set this key as a Windows user variable?"
# The question names the variable and says where the value goes. It never shows the value.
USER_ENV_BODY = (
    "The saved value for {name} is written to your Windows user environment as {variable}. Programs you start after this "
    "can read it. The value is not shown here. It stays there until you remove it in Windows environment settings."
)
USER_ENV_CANCELLED_TEXT = "Cancelled. Nothing was written to your Windows user environment."
USER_ENV_NO_VALUE_TEXT = "No value is saved for {name} yet. Save a value first, then set it as a Windows user variable."
USER_ENV_SET_TEXT = (
    "Set {variable} in your Windows user environment. The value is not shown. "
    "Programs already open do not see it until they restart."
)
USER_ENV_UNANNOUNCED_TEXT = "Windows did not confirm the change to running programs, so sign out and back in if one does not see it."
USER_ENV_NOT_SET_TEXT = "Not set. {detail}"
USER_ENV_OS_TEXT = "Not set. The Windows user environment could not be written. Try again."
# The service list in the form. Its services come from the registry, and Other keeps the free name.
SERVICE_LABEL_TEXT = "Service"
OTHER_CHOICE = "Other"
SERVICE_CHOICES = (*(service.name for service in SERVICES), OTHER_CHOICE)
# A known service's card: what the key is for, where it is made, and the check.
CREATE_TEXT = "Create the key at {page}."
CHECK_TEXT = "Check"
SHOW_DETAILS_TEXT = "Details"
HIDE_DETAILS_TEXT = "Hide"
CHECKING_TEXT = "Checking {service}..."
# The keys file: its control, what it says about the file, and the names it lists.
FILE_HEADING_TEXT = "Load keys from a file"
FILE_INTRO_TEXT = (
    "Pick a .json, .env or secrets file. A .json file holds each key as a name and its value, for example "
    '{"OpenAI": "..."}. A .env file holds one NAME=value line for each key. The values stay in that file. Showing the names '
    "here never copies a value to the keychain or to settings, and the app never writes the file. Each refresh opens the file "
    "and reads every entry, value included, but keeps only the names. A value is sent only when you press Check on its card, "
    "for that one request."
)
CHOOSE_FILE_TEXT = "Choose file"
STOP_FILE_TEXT = "Stop using this file"
NO_FILE_TEXT = "No keys file is chosen."
FILE_PATH_TEXT = "Keys file: {name}"
FILE_LISTED_ONE_TEXT = "The file lists 1 key name. Its value stays in the file."
FILE_LISTED_MANY_TEXT = "The file lists {count} key names. Their values stay in the file."
FILE_STOPPED_TEXT = "Stopped using the keys file. The file itself is unchanged."
FILE_CHOOSER_TITLE = "Choose a keys file"
# The patterns match the names keys_file.file_format accepts: ".env.*" is a prefix, so "production.env.local" is not offered.
FILE_CHOOSER_FILTER = (
    "Keys files (*.json *.env .env.* *.secrets *.secret secrets.*);;"
    "JSON files (*.json);;"
    "Env and secrets files (*.env .env.* *.secrets *.secret secrets.*)"
)
FROM_FILE_NOTE = "From file"
REMOVED_FROM_FILE_TEXT = "Removed {name} from the list for this session. The keys file is unchanged."
# The two groups of From file names, set automatically. A name is critical when it is a known service (key_services)
# or a built-in name (api_keys), and non-critical otherwise. No mark is saved for these names, so the rule decides.
CLASSIFIED_HEADING_TEXT = "Critical: a known service or built-in name. Set automatically."
UNCLASSIFIED_HEADING_TEXT = "Non-critical: any other name. Set automatically."
# The names-only export. The file it writes holds names and classifications, and no value.
EXPORT_TEXT = "Export key names"
EXPORTED_TEXT = "Saved the key names and their classification to {path}. No value is in the file."
EXPORT_FAILED_TEXT = "Not saved: the key names could not be written. Try again."
# Import keys from a vault file: the button, its file picker, and what the list says after an import. The dialog itself is
# ui/key_import_dialog.py. The Windows user variables are never written by an import.
IMPORT_VAULT_TEXT = "Import keys from a vault file"
IMPORT_CHOOSER_TITLE = "Choose a vault file to import"
IMPORT_CHOOSER_FILTER = "Key vault files (*.json);;All files (*)"
IMPORTED_SECTION_ONE_TEXT = "Imported 1 key from the vault file into the keychain. Windows user variables were not changed."
IMPORTED_SECTION_MANY_TEXT = "Imported {count} keys from the vault file into the keychain. Windows user variables were not changed."
# Import keys from a plain file: the button, its file picker, the Yes or No question before a file is read, and what the list says
# after an import. The dialog itself is ui/key_import_plain_dialog.py. A plain file is never written, and no Windows user variable
# is written by this import.
IMPORT_PLAIN_TEXT = "Import keys from a plain file"
IMPORT_PLAIN_CHOOSER_TITLE = "Choose a keys file to import"
PLAIN_CANCELLED_TEXT = "Cancelled. Nothing was read or written."
IMPORTED_PLAIN_ONE_TEXT = (
    "Imported 1 key from the keys file into the keychain. The file and the Windows user variables were not changed."
)
IMPORTED_PLAIN_MANY_TEXT = (
    "Imported {count} keys from the keys file into the keychain. The file and the Windows user variables were not changed."
)
# The Export keys control: an encrypted vault file (the default) or a plaintext file, written only to a path the owner picks.
# The dialog is ui/key_export_dialog.py. Nothing is exported from the list or at startup.
EXPORT_KEYS_TEXT = "Export keys"
EXPORTED_KEYS_ONE_TEXT = "Exported 1 key to a file. The keychain and the Windows user variables were not changed."
EXPORTED_KEYS_MANY_TEXT = "Exported {count} keys to a file. The keychain and the Windows user variables were not changed."
# The Reveal, Hide value and Copy value controls of a keychain name. A shown value is masked after REVEAL_SECONDS, and a copied
# value is cleared from the clipboard after CLIPBOARD_SECONDS, but only while the clipboard still holds that value.
REVEAL_SECONDS = 30
CLIPBOARD_SECONDS = 30
HIDE_VALUE_TEXT = "Hide value"
COPY_VALUE_TEXT = "Copy value"
REVEAL_SHOWN_TEXT = "Showing the value of {name} for {seconds} seconds. It is hidden again after that, or with Hide value."
REVEAL_HIDDEN_TEXT = "The value of {name} is hidden again."
REVEAL_NO_VALUE_TEXT = "No value is saved for {name}, so there is nothing to show."
REVEAL_NOT_READ_TEXT = "The keychain could not be read, so no value is shown."
COPIED_VALUE_TEXT = "Copied the value of {name} to the clipboard. It is cleared in {seconds} seconds if it still holds it."
COPY_NO_VALUE_TEXT = "No value is saved for {name}, so nothing was copied."
COPY_NOT_READ_TEXT = "The keychain could not be read, so nothing was copied."

# A confirmation: (parent, title, text) -> True only when the owner clicks Yes.
Confirm = Callable[[QWidget, str, str], bool]
# A file dialog: (parent) -> the path the owner picked, or "" when the owner cancelled.
FileChooser = Callable[[QWidget], str]


def _service_text(name: str) -> str:
    """The casefolded description of the known service that ``name`` matches, or "" for a name no service matches.

    Only the registry's own description is searched. A key value and a key status are never read here.
    """
    service = service_for(name)
    return service.description.casefold() if service is not None else ""


def _subgroup_key(label: str) -> str:
    """The identity of a subgroup: its label, casefolded, so "AWS keys" and "aws keys" are one subgroup."""
    return label.casefold()


def _system_clipboard() -> QClipboard:
    """The clipboard the Copy value control writes to. Tests replace this, so the real clipboard is never touched."""
    return QGuiApplication.clipboard()


def _digest(value: str) -> bytes:
    """A digest of a copied value. The section keeps this for the clipboard check, so it never keeps the value itself."""
    return hashlib.sha256(value.encode("utf-8")).digest()


def _details_toggle(frame: QFrame, key: str) -> QPushButton:
    """The Details button of a card. It is checkable, so its state says whether the details are open."""
    toggle = QPushButton(SHOW_DETAILS_TEXT, frame)
    toggle.setObjectName(f"keys-entry-{key}-details-toggle")
    toggle.setCheckable(True)
    return toggle


def _details_panel(frame: QFrame, key: str) -> tuple[QFrame, QVBoxLayout]:
    """The hidden part of a card: the service lines, the value row, the marks and the check. Returned with its layout."""
    panel = QFrame(frame)
    panel.setObjectName(f"keys-entry-{key}-details")
    panel.setVisible(False)
    layout = QVBoxLayout(panel)
    layout.setContentsMargins(0, 0, 0, 0)
    return panel, layout


def _connect_details(toggle: QPushButton, panel: QFrame) -> None:
    """Show or hide the details with the Details button, and name the button for what it does next."""

    def set_shown(shown: bool) -> None:
        panel.setVisible(shown)
        toggle.setText(HIDE_DETAILS_TEXT if shown else SHOW_DETAILS_TEXT)

    toggle.toggled.connect(set_shown)


def _label(parent: QWidget, name: str, text: str = "") -> QLabel:
    label = QLabel(text, parent)
    label.setObjectName(name)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return make_selectable(label)


def _bold(label: QLabel) -> QLabel:
    font = label.font()
    font.setBold(True)
    label.setFont(font)
    return label


def _ask_remove(parent: QWidget, title: str, text: str) -> bool:
    """The Yes or No question before a key is removed. No is the default."""
    return ask_yes_no(parent, title, text)


def _ask_user_env(parent: QWidget, title: str, text: str) -> bool:
    """The Yes or No before a value is written to the Windows user environment. No is the default."""
    return ask_yes_no(parent, title, text)


@dataclass
class _Mark:
    """The Critical badge, the Mark critical box and the user-variable button of one name. RunPod has one too."""

    badge: QLabel
    critical: QCheckBox
    user_env: QPushButton


def _show_mark(mark: _Mark, critical: bool) -> None:
    """Show the badge and set the box to the saved mark. The box's own signal does not run again for this."""
    mark.badge.setVisible(critical)
    mark.critical.blockSignals(True)
    mark.critical.setChecked(critical)
    mark.critical.blockSignals(False)


def _action_row(mark: _Mark) -> QHBoxLayout:
    row = QHBoxLayout()
    row.addWidget(mark.critical)
    row.addStretch(1)
    row.addWidget(mark.user_env)
    return row


def _ask_keys_file(parent: QWidget) -> str:
    """The file dialog that picks a keys file. It opens on the owner's home folder. An empty answer means it was cancelled."""
    path, _filter = QFileDialog.getOpenFileName(parent, FILE_CHOOSER_TITLE, str(Path.home()), FILE_CHOOSER_FILTER)
    return path


def _ask_vault_file(parent: QWidget) -> str:
    """The file dialog that picks a vault file to import. It opens on the owner's home folder. "" means it was cancelled."""
    path, _filter = QFileDialog.getOpenFileName(parent, IMPORT_CHOOSER_TITLE, str(Path.home()), IMPORT_CHOOSER_FILTER)
    return path


def _ask_plain_file(parent: QWidget) -> str:
    """The file dialog that picks a keys file to import once. It offers the kinds keys_file reads. "" means it was cancelled."""
    path, _filter = QFileDialog.getOpenFileName(
        parent, IMPORT_PLAIN_CHOOSER_TITLE, str(Path.home()), FILE_CHOOSER_FILTER
    )
    return path


def _listed_text(total: int) -> str:
    """What the file status says about the names the file lists."""
    if total == 1:
        return FILE_LISTED_ONE_TEXT
    return FILE_LISTED_MANY_TEXT.format(count=total)


def _store_of(keys: ApiKeys) -> SecretStore:
    """The keychain store that ``keys`` reads its names' values from. A Check reads its one value through it.

    ApiKeys keeps the store private and returns no value, so this is the one place the block reads the attribute.
    The value a Check reads goes only to its request, and it is never kept or shown.
    """
    return keys._secrets  # noqa: SLF001 - the store the saved names are read from; ApiKeys itself returns no value


def _settings_of(keys: ApiKeys) -> SettingsStore:
    """The settings store that ``keys`` keeps its names in. The keys file path is one more key in that store.

    ApiKeys keeps the store private, as it keeps the keychain store private, so this is the one place the block reads it.
    """
    return keys._settings  # noqa: SLF001 - the store the names are listed from; the keys file path is saved in it too


@dataclass
class _Card:
    """One listed name's card and the widgets the section reads and changes.

    A keychain card has a value box and Save value. A From file card has no value box, because its value stays in the file.
    A known service's card also has a Check.
    """

    frame: QFrame
    status: QLabel
    value: QLineEdit | None
    # The Critical badge, the critical box and the user-variable button. None on a From file card, which has no keychain value.
    mark: _Mark | None = None
    check: QPushButton | None = None
    check_result: QLabel | None = None
    # The subgroup line of a keychain card, which the layout and the search read. None on a From file card.
    subgroup: QLabel | None = None
    from_file: bool = False


@dataclass
class _Reveal:
    """The Reveal, Hide value and Copy value controls of one keychain name, with the field its value is shown in.

    ``timer`` masks that field again after REVEAL_SECONDS. Reveal, Copy value and the timer's handler are wired by the section.
    """

    shown: QLineEdit
    hide: QPushButton
    timer: QTimer


class KeysSection(QWidget):
    """The Keys block. The RunPod card is placed by the host; every other name gets a card made here."""

    def __init__(
        self,
        keys: ApiKeys,
        runpod_card: QWidget | None = None,
        parent: QWidget | None = None,
        *,
        confirm: Confirm | None = None,
        transport: Transport | None = None,
        choose_file: FileChooser | None = None,
        choose_vault: FileChooser | None = None,
        choose_export_path: PathChooser | None = None,
        confirm_export: Confirm | None = None,
        choose_plain: FileChooser | None = None,
        confirm_plain: Confirm | None = None,
        confirm_plain_env: Confirm | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("keys-section")
        self._keys = keys
        self._runpod_card = runpod_card
        # None means the Yes/No dialog. It is looked up when Remove is pressed, not here, so a test can swap it.
        self._confirm = confirm
        # The store a Check reads its value from, and the transport it sends with. None for the transport is urllib.
        self._store = _store_of(keys)
        self._transport = transport
        # The settings the keys file path is kept in. The file dialog is looked up here, and None means the real dialog.
        self._settings = _settings_of(keys)
        # The last listing written to the control log, with its path. The block is rebuilt on each refresh, so an
        # unchanged listing is not written again.
        self._logged_listing: tuple[str, FileNames] | None = None
        self._choose_file = choose_file if choose_file is not None else _ask_keys_file
        # The vault file picker for an import, and the import dialog it opened. None means the real file dialog.
        self._choose_vault = choose_vault if choose_vault is not None else _ask_vault_file
        self._import_dialog: KeyImportDialog | None = None
        # The Export keys dialog, and the file picker and replace question it uses. None means the real ones.
        self._export_dialog: KeyExportDialog | None = None
        self._choose_export_path = choose_export_path
        self._confirm_export = confirm_export
        # The plain keys file picker, and the Yes or No question asked before a plain file is read. None means the real ones.
        self._choose_plain = choose_plain if choose_plain is not None else _ask_plain_file
        self._confirm_plain = confirm_plain if confirm_plain is not None else ask_plain_import
        self._confirm_plain_env = confirm_plain_env
        self._plain_dialog: KeyPlainImportDialog | None = None
        # The names a From file card was removed for, by slug. This session keeps them, and the file is never changed.
        self._hidden_file_slugs: set[str] = set()
        self._cards: dict[str, _Card] = {}
        # One heading per subgroup, by its subgroup key. A heading is made once and reused. Only the subgroups with a keychain
        # card on the list are in the layout (_subgroup_in_use), and only those can show.
        self._subgroup_headings: dict[str, QLabel] = {}
        self._subgroup_in_use: set[str] = set()
        # The RunPod card's frame and its marks, when a RunPod card is hosted. The frame sits in the same list as the others,
        # so a critical RunPod is listed first, and the search filters it by its name like the others.
        self._runpod_frame: QFrame | None = None
        self._runpod_mark: _Mark | None = None
        # The first read of the listed names runs on the thread pool, so building this block never reads the keychain.
        self._owner = object()
        self._load_generation = 0
        self._loading = False
        # A Check also runs on the thread pool. Its result comes back under its own owner and token, so it is never
        # mistaken for a read of the names. The name maps to the token of the one check running for its card.
        self._check_owner = object()
        self._check_tokens = count(1)
        self._pending: dict[str, int] = {}
        shared_delivery().delivered.connect(self._on_names_loaded)
        shared_delivery().delivered.connect(self._on_check_delivered)
        # The Reveal controls of each keychain name, by name. The clipboard timer clears a copied value after
        # CLIPBOARD_SECONDS. It holds only a digest of that value, so the check can match the clipboard without the value.
        self._reveals: dict[str, _Reveal] = {}
        self._copied_digest: bytes | None = None
        self._clipboard_timer = QTimer(self)
        self._clipboard_timer.setObjectName("keys-clipboard-timer")
        self._clipboard_timer.setSingleShot(True)
        self._clipboard_timer.setInterval(CLIPBOARD_SECONDS * 1000)
        self._clipboard_timer.timeout.connect(self._clear_copied_value)
        self._build()
        self._load_names()

    @property
    def loading(self) -> bool:
        """True while the listed names are being read from the keychain on the thread pool."""
        return self._loading

    def _load_names(self) -> None:
        """Read the listed names and whether each one is set, on the thread pool. A read still running is replaced."""
        self._load_generation += 1
        self._loading = True
        # The job holds the key store, never this block, so a block freed while the read runs is never touched off the GUI thread.
        keys = self._keys
        submit(self._owner, self._load_generation, lambda: keys.entries())

    @Slot(object)
    def _on_names_loaded(self, item: object) -> None:
        # Runs on the GUI thread. A result for another section, or an older read, is dropped here.
        if not isinstance(item, Delivered) or item.owner is not self._owner:
            return
        generation, (entries, failure) = item.value
        if generation != self._load_generation:
            return
        self._loading = False
        if failure is not None:
            self._show(NAMES_NOT_READ_TEXT)
            # The keychain names stay as they were. The keys file is still read, so its names are still listed.
            self._apply_file_block(self._keychain_names())
            return
        self._apply_entries(entries)

    # ----------------------------------------------------------- layout

    def _build(self) -> None:
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(8)
        column.addWidget(_label(self, "keys-intro", INTRO_TEXT))

        self._search = QLineEdit(self)
        self._search.setObjectName("keys-search")
        self._search.setPlaceholderText(SEARCH_PLACEHOLDER)
        self._search.setAccessibleName(SEARCH_PLACEHOLDER)
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._on_search_changed)
        column.addWidget(self._search)
        self._search_empty = _label(self, "keys-search-empty", SEARCH_EMPTY_TEXT)
        self._search_empty.setVisible(False)
        column.addWidget(self._search_empty)
        # The Critical vault block sits above the list, so the critical keys and their vault are seen first.
        # The critical names as the last loaded list showed them. The vault block reads this, never the keychain.
        self._critical_cache: list[str] = []
        self._vault_box = KeyVaultBox(self._vault_path(), self._critical_names, self._read_keychain, self)
        column.addWidget(self._vault_box)

        # Every card sits in this one list, the RunPod card's included, so the critical names come first whichever they are.
        self._entry_layout = QVBoxLayout()
        self._entry_layout.setSpacing(8)
        column.addLayout(self._entry_layout)
        if self._runpod_card is not None:
            self._build_runpod_frame()
        # The two group headings of the From file cards. They join the list only while their group has a card, so the list
        # holds the keychain cards and RunPod alone otherwise.
        self._classified_heading = _bold(_label(self, "keys-file-classified-heading", CLASSIFIED_HEADING_TEXT))
        self._unclassified_heading = _bold(_label(self, "keys-file-unclassified-heading", UNCLASSIFIED_HEADING_TEXT))
        self._classified_heading.setVisible(False)
        self._unclassified_heading.setVisible(False)

        self._build_file_control(column)

        self._message = _label(self, "keys-message")
        self._message.setVisible(False)
        column.addWidget(self._message)

        column.addWidget(_bold(_label(self, "keys-add-heading", ADD_HEADING_TEXT)))
        service_row = QHBoxLayout()
        self._new_service = QComboBox(self)
        self._new_service.setObjectName("keys-new-service")
        self._new_service.setAccessibleName(SERVICE_LABEL_TEXT)
        self._new_service.addItems(list(SERVICE_CHOICES))
        self._new_service.setCurrentText(OTHER_CHOICE)
        # activated fires only for a pick the owner makes, so the name is never filled by a reset.
        self._new_service.activated.connect(self._on_service_picked)
        service_row.addWidget(_label(self, "keys-service-label", SERVICE_LABEL_TEXT))
        service_row.addWidget(self._new_service)
        service_row.addStretch(1)
        column.addLayout(service_row)
        row = QHBoxLayout()
        self._new_name = QLineEdit(self)
        self._new_name.setObjectName("keys-new-name")
        self._new_name.setPlaceholderText(NAME_PLACEHOLDER)
        self._new_value = QLineEdit(self)
        self._new_value.setObjectName("keys-new-value")
        self._new_value.setEchoMode(QLineEdit.EchoMode.Password)
        self._new_value.setPlaceholderText(VALUE_PLACEHOLDER)
        self._add_button = QPushButton(ADD_TEXT, self)
        self._add_button.setObjectName("keys-add")
        row.addWidget(self._new_name, 1)
        row.addWidget(self._new_value, 1)
        row.addWidget(self._add_button)
        column.addLayout(row)

        export_row = QHBoxLayout()
        self._export_button = QPushButton(EXPORT_TEXT, self)
        self._export_button.setObjectName("keys-export-names")
        export_row.addWidget(self._export_button)
        self._export_keys_button = QPushButton(EXPORT_KEYS_TEXT, self)
        self._export_keys_button.setObjectName("keys-export")
        export_row.addWidget(self._export_keys_button)
        self._import_button = QPushButton(IMPORT_VAULT_TEXT, self)
        self._import_button.setObjectName("keys-import-vault")
        export_row.addWidget(self._import_button)
        self._import_plain_button = QPushButton(IMPORT_PLAIN_TEXT, self)
        self._import_plain_button.setObjectName("keys-import-plain")
        export_row.addWidget(self._import_plain_button)
        export_row.addStretch(1)
        column.addLayout(export_row)
        self._export_button.clicked.connect(self._on_export_names)
        self._import_button.clicked.connect(self._on_import_vault)
        self._import_plain_button.clicked.connect(self._on_import_plain)
        self._export_keys_button.clicked.connect(self._on_export_keys)

        self._add_button.clicked.connect(self._on_add_clicked)
        self._new_value.returnPressed.connect(self._on_add_clicked)

    def _build_runpod_frame(self) -> None:
        # The RunPod card is framed like the other cards, so every name in the block looks the same.
        frame = QFrame(self)
        frame.setObjectName("keys-entry-runpod")
        self._runpod_frame = frame
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        mark = self._make_mark(frame, slug(RUNPOD_NAME), RUNPOD_NAME)
        self._runpod_mark = mark
        frame_column = QVBoxLayout(frame)
        head = QHBoxLayout()
        head.addWidget(_bold(_label(frame, "keys-runpod-name", RUNPOD_HEADING_TEXT)))
        head.addWidget(mark.badge)
        head.addStretch(1)
        frame_column.addLayout(head)
        frame_column.addWidget(self._runpod_card)
        reveal_row, reveal = self._make_reveal(frame, slug(RUNPOD_NAME), RUNPOD_NAME)
        self._reveals[RUNPOD_NAME] = reveal
        frame_column.addLayout(reveal_row)
        frame_column.addLayout(_action_row(mark))
        self._entry_layout.addWidget(frame)

    def _make_mark(self, frame: QFrame, key: str, name: str) -> _Mark:
        badge = _bold(_label(frame, f"keys-entry-{key}-badge", CRITICAL_BADGE_TEXT))
        badge.setVisible(False)
        critical = QCheckBox(CRITICAL_BOX_TEXT, frame)
        critical.setObjectName(f"keys-entry-{key}-critical")
        user_env = QPushButton(USER_ENV_TEXT, frame)
        user_env.setObjectName(f"keys-entry-{key}-user-env")
        critical.toggled.connect(lambda checked, n=name: self._on_critical_toggled(n, checked))
        user_env.clicked.connect(lambda *_, n=name: self._on_user_env(n))
        return _Mark(badge, critical, user_env)

    def _build_file_control(self, column: QVBoxLayout) -> None:
        """The Load keys from a file block: what the file is for, its path, Choose file, Stop using it, and its status."""
        column.addWidget(_bold(_label(self, "keys-file-heading", FILE_HEADING_TEXT)))
        column.addWidget(_label(self, "keys-file-intro", FILE_INTRO_TEXT))
        self._file_path = _label(self, "keys-file-path", NO_FILE_TEXT)
        column.addWidget(self._file_path)
        row = QHBoxLayout()
        self._choose_button = QPushButton(CHOOSE_FILE_TEXT, self)
        self._choose_button.setObjectName("keys-file-choose")
        self._stop_button = QPushButton(STOP_FILE_TEXT, self)
        self._stop_button.setObjectName("keys-file-stop")
        # Stop is off until a file is chosen. _apply_file_block turns it on when a path is saved.
        self._stop_button.setEnabled(False)
        row.addWidget(self._choose_button)
        row.addWidget(self._stop_button)
        row.addStretch(1)
        column.addLayout(row)
        self._file_status = _label(self, "keys-file-status")
        column.addWidget(self._file_status)
        self._set_file_status("")
        self._choose_button.clicked.connect(self._on_choose_file)
        self._stop_button.clicked.connect(self._on_stop_file)

    def _check_row(
        self,
        frame: QFrame,
        column: QVBoxLayout,
        key: str,
        name: str,
        service: KeyService | None,
    ) -> tuple[QPushButton | None, QLabel | None]:
        """The Check row of a card for a known service: its button, and the line its result is shown on. None for no service."""
        if service is None:
            return None, None
        check_row = QHBoxLayout()
        check_result = _label(frame, f"keys-entry-{key}-check-result")
        check: QPushButton | None = None
        if service.check is None:
            # A service with no check says so in place of a button, so nothing looks as if it were checked.
            check_result.setText(NO_CHECK_TEXT)
        else:
            check = QPushButton(CHECK_TEXT, frame)
            check.setObjectName(f"keys-entry-{key}-check")
            check.clicked.connect(lambda *_, n=name: self._on_check(n))
            check_row.addWidget(check)
        check_row.addWidget(check_result, 1)
        column.addLayout(check_row)
        return check, check_result

    def _make_card(self, name: str) -> _Card:
        key = slug(name)
        service = service_for(name)
        frame = QFrame(self)
        frame.setObjectName(f"keys-entry-{key}")
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        column = QVBoxLayout(frame)

        mark = self._make_mark(frame, key, name)
        head = QHBoxLayout()
        head.addWidget(_bold(_label(frame, f"keys-entry-{key}-name", name)))
        head.addWidget(mark.badge)
        status = _label(frame, f"keys-entry-{key}-status")
        head.addWidget(status, 1)
        head.addWidget(_label(frame, f"keys-entry-{key}-group", GROUP_TEXT.format(group=self._group_text(name))))
        subgroup_label = _label(frame, f"keys-entry-{key}-subgroup", self._subgroup_line(name))
        head.addWidget(subgroup_label)
        toggle = _details_toggle(frame, key)
        head.addWidget(toggle)
        column.addLayout(head)
        # One line per name. The details below stay hidden until the owner opens them with Details.
        panel, details = _details_panel(frame, key)
        column.addWidget(panel)

        if service is not None:
            # A known service says what its key is for and where it is made. Both lines are plain text.
            details.addWidget(_label(panel, f"keys-entry-{key}-description", service.description))
            details.addWidget(_label(panel, f"keys-entry-{key}-create", CREATE_TEXT.format(page=service.create_page)))

        row = QHBoxLayout()
        value = QLineEdit(panel)
        value.setObjectName(f"keys-entry-{key}-value")
        value.setEchoMode(QLineEdit.EchoMode.Password)
        value.setPlaceholderText(NEW_VALUE_PLACEHOLDER)
        save = QPushButton(SAVE_VALUE_TEXT, panel)
        save.setObjectName(f"keys-entry-{key}-save")
        remove = QPushButton(REMOVE_TEXT, panel)
        remove.setObjectName(f"keys-entry-{key}-remove")
        row.addWidget(value, 1)
        row.addWidget(save)
        row.addWidget(remove)
        details.addLayout(row)
        reveal_row, reveal = self._make_reveal(panel, key, name)
        self._reveals[name] = reveal
        details.addLayout(reveal_row)
        details.addLayout(_action_row(mark))
        # The group editor: the owner types a new label and saves it. Only the label is stored.
        group_row = QHBoxLayout()
        group_edit = QLineEdit(panel)
        group_edit.setObjectName(f"keys-entry-{key}-group-edit")
        group_edit.setPlaceholderText(GROUP_PLACEHOLDER)
        group_save = QPushButton(GROUP_SAVE_TEXT, panel)
        group_save.setObjectName(f"keys-entry-{key}-group-save")
        group_row.addWidget(group_edit, 1)
        group_row.addWidget(group_save)
        details.addLayout(group_row)
        group_save.clicked.connect(lambda *_, n=name: self._on_group_saved(n))
        # The subgroup editor: the owner types the subgroup this key belongs to, or leaves it empty to clear it. Only the label
        # is stored.
        subgroup_row = QHBoxLayout()
        subgroup_edit = QLineEdit(panel)
        subgroup_edit.setObjectName(f"keys-entry-{key}-subgroup-edit")
        subgroup_edit.setPlaceholderText(SUBGROUP_PLACEHOLDER)
        subgroup_save = QPushButton(SUBGROUP_SAVE_TEXT, panel)
        subgroup_save.setObjectName(f"keys-entry-{key}-subgroup-save")
        subgroup_row.addWidget(subgroup_edit, 1)
        subgroup_row.addWidget(subgroup_save)
        details.addLayout(subgroup_row)
        subgroup_save.clicked.connect(lambda *_, n=name: self._on_subgroup_saved(n))

        check, check_result = self._check_row(panel, details, key, name, service)

        save.clicked.connect(lambda *_, n=name: self._on_save_value(n))
        value.returnPressed.connect(lambda *_, n=name: self._on_save_value(n))
        remove.clicked.connect(lambda *_, n=name: self._on_remove(n))
        _connect_details(toggle, panel)
        return _Card(frame, status, value, mark, check, check_result, subgroup=subgroup_label)

    def _make_reveal(self, parent: QFrame, key: str, name: str) -> tuple[QHBoxLayout, _Reveal]:
        """The Reveal, Hide value and Copy value row of a keychain name, and the field its value is shown in.

        The field is read-only, empty and hidden until Reveal is pressed. Its timer masks it again after REVEAL_SECONDS.
        """
        shown = QLineEdit(parent)
        shown.setObjectName(f"keys-entry-{key}-revealed")
        shown.setReadOnly(True)
        shown.setVisible(False)
        reveal = QPushButton(REVEAL_TEXT, parent)
        reveal.setObjectName(f"keys-entry-{key}-reveal")
        hide = QPushButton(HIDE_VALUE_TEXT, parent)
        hide.setObjectName(f"keys-entry-{key}-hide")
        hide.setVisible(False)
        copy = QPushButton(COPY_VALUE_TEXT, parent)
        copy.setObjectName(f"keys-entry-{key}-copy")
        timer = QTimer(parent)
        timer.setObjectName(f"keys-entry-{key}-reveal-timer")
        timer.setSingleShot(True)
        timer.setInterval(REVEAL_SECONDS * 1000)
        row = QHBoxLayout()
        row.addWidget(reveal)
        row.addWidget(hide)
        row.addWidget(copy)
        row.addWidget(shown, 1)
        reveal.clicked.connect(lambda *_, n=name: self._on_reveal(n))
        hide.clicked.connect(lambda *_, n=name: self._mask(n))
        copy.clicked.connect(lambda *_, n=name: self._on_copy_value(n))
        timer.timeout.connect(lambda *_, n=name: self._auto_hide(n))
        return row, _Reveal(shown, hide, timer)

    def _make_file_card(self, name: str) -> _Card:
        """The card for a name the keys file lists. It has no value box: the value stays in the file and is read on Check."""
        key = slug(name)
        service = service_for(name)
        frame = QFrame(self)
        frame.setObjectName(f"keys-entry-{key}")
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        column = QVBoxLayout(frame)

        head = QHBoxLayout()
        head.addWidget(_bold(_label(frame, f"keys-entry-{key}-name", name)))
        # Set automatically: a known service or built-in name is critical, every other imported name is not.
        badge = _bold(_label(frame, f"keys-entry-{key}-badge", CRITICAL_BADGE_TEXT))
        badge.setVisible(is_classified(name))
        head.addWidget(badge)
        status = _label(frame, f"keys-entry-{key}-status")
        head.addWidget(status, 1)
        toggle = _details_toggle(frame, key)
        head.addWidget(toggle)
        column.addLayout(head)
        panel, details = _details_panel(frame, key)
        column.addWidget(panel)

        if service is not None:
            details.addWidget(_label(panel, f"keys-entry-{key}-description", service.description))
            details.addWidget(_label(panel, f"keys-entry-{key}-create", CREATE_TEXT.format(page=service.create_page)))

        row = QHBoxLayout()
        remove = QPushButton(REMOVE_TEXT, panel)
        remove.setObjectName(f"keys-entry-{key}-remove")
        row.addStretch(1)
        row.addWidget(remove)
        details.addLayout(row)

        check, check_result = self._check_row(panel, details, key, name, service)

        remove.clicked.connect(lambda *_, n=name: self._on_remove(n))
        _connect_details(toggle, panel)
        return _Card(frame, status, None, check=check, check_result=check_result, from_file=True)

    # ----------------------------------------------------------- state on screen

    def _vault_path(self) -> Path:
        """The vault file, beside settings.json: the same folder as the settings the names are kept in."""
        return self._settings.path.parent / VAULT_FILE_NAME

    def _critical_names(self) -> list[str]:
        """The names marked critical, as the last loaded list showed them. Never reads the keychain."""
        return list(self._critical_cache)

    def _set_critical_cached(self, name: str, critical: bool) -> None:
        """Keep the critical names in step with a mark the owner just changed, then show the vault block again."""
        remaining = [known for known in self._critical_cache if known != name]
        self._critical_cache = remaining + ([name] if critical else [])
        self._vault_box.refresh()

    def _read_keychain(self, name: str) -> str | None:
        """The keychain value of one name, for copying it into the vault. Only the vault box calls this."""
        return self._store.get_secret(KEY_SERVICE, account_for(name))

    def refresh(self) -> None:
        """Re-read which names are listed and whether each one is set. Values are never read into a control.

        This read runs on the GUI thread. It also supersedes a read still running, so that older read cannot bring back a
        name this read has already dropped.
        """
        self._load_generation += 1
        self._loading = False
        self._apply_entries(self._keys.entries())

    def _apply_entries(self, entries: list[Any]) -> None:
        self._critical_cache = [entry.name for entry in entries if entry.critical]
        self._vault_box.refresh()
        listed = [entry for entry in entries if not entry.built_in]
        keychain = {entry.name for entry in listed}
        for name in [name for name, card in self._cards.items() if not card.from_file and name not in keychain]:
            self._drop_card(name)
        for entry in listed:
            card = self._card_for(entry.name, from_file=False)
            card.status.setText(STATUS_TEXT[entry.status])
            if card.subgroup is not None:
                card.subgroup.setText(self._subgroup_line(entry.name))
            if card.mark is not None:
                _show_mark(card.mark, entry.critical)
        for entry in entries:
            if entry.built_in and self._runpod_mark is not None:
                _show_mark(self._runpod_mark, entry.critical)
        self._apply_file_block(keychain)
        self._reorder(entries)
        self._apply_filter()

    def _keychain_names(self) -> set[str]:
        """The names that have a keychain card now."""
        return {name for name, card in self._cards.items() if not card.from_file}

    def _card_for(self, name: str, *, from_file: bool) -> _Card:
        """The card for ``name``. A card of the other kind is dropped and made again, so one name is one kind of card."""
        card = self._cards.get(name)
        if card is not None and card.from_file != from_file:
            self._drop_card(name)
            card = None
        if card is None:
            card = self._make_file_card(name) if from_file else self._make_card(name)
            self._cards[name] = card
            self._entry_layout.addWidget(card.frame)
        return card

    def _drop_card(self, name: str) -> None:
        card = self._cards.pop(name)
        self._pending.pop(name, None)  # a check still running for a dropped card has no card to show its result on
        reveal = self._reveals.pop(name, None)
        if reveal is not None:
            reveal.timer.stop()  # a shown value leaves with its card, and its timer does not fire for a gone card
        self._entry_layout.removeWidget(card.frame)
        card.frame.hide()
        card.frame.deleteLater()

    def _drop_file_cards(self, keep: set[str]) -> None:
        """Drop each From file card whose name is not in ``keep``."""
        for name in [name for name, card in self._cards.items() if card.from_file and name not in keep]:
            self._drop_card(name)

    def _keys_file_path(self) -> str:
        """The keys file the owner chose, or "" when none is chosen or the settings file cannot be read."""
        return self._settings.load().settings.keys_file_path

    def _apply_file_block(self, keychain_names: set[str]) -> None:
        """Show the keys file's path and status, and keep one From file card for each name the file lists.

        A name the keychain also lists keeps its keychain card. A name removed for this session has no card. The file is
        read here, on this thread, for its names only: no value is kept.
        """
        path = self._keys_file_path()
        self._stop_button.setEnabled(bool(path))
        if not path:
            self._file_path.setText(NO_FILE_TEXT)
            self._file_path.setToolTip("")
            self._set_file_status("")
            self._drop_file_cards(set())
            self._lay_out_file_groups([])
            return
        self._file_path.setText(FILE_PATH_TEXT.format(name=Path(path).name))
        self._file_path.setToolTip(path)
        listing = read_names(path)
        self._log_listing(path, listing)
        if listing.problem is not None:
            self._set_file_status(listing.problem)
            self._drop_file_cards(set())
            self._lay_out_file_groups([])
            return
        note = skipped_summary(listing)
        self._set_file_status(_listed_text(len(listing.names)) + ("" if note is None else "\n" + note))
        keychain_slugs = {slug(name) for name in keychain_names}
        shown = [
            name
            for name in listing.names
            if slug(name) not in keychain_slugs and slug(name) not in self._hidden_file_slugs
        ]
        self._drop_file_cards(set(shown))
        for name in shown:
            self._card_for(name, from_file=True).status.setText(FROM_FILE_NOTE)
        self._lay_out_file_groups(shown)

    def _lay_out_file_groups(self, names: list[str]) -> None:
        """Order the From file cards: the classified names under their heading, then the unclassified under theirs.

        A heading shows only when its group has a card. The keychain cards and RunPod stay in front of both groups, because
        _reorder runs after this.
        """
        classified, unclassified = split_by_classification(names)
        for heading, group in ((self._classified_heading, classified), (self._unclassified_heading, unclassified)):
            self._entry_layout.removeWidget(heading)
            heading.setVisible(bool(group))
            if group:
                self._entry_layout.addWidget(heading)
            for name in group:
                frame = self._cards[name].frame
                self._entry_layout.removeWidget(frame)
                self._entry_layout.addWidget(frame)

    def _log_listing(self, path: str, listing: FileNames) -> None:
        """Write a listing of the keys file to the control log, unless it is the one last written for this path."""
        if self._logged_listing == (path, listing):
            return
        self._logged_listing = (path, listing)
        log_listing(self._settings.path, listing)

    def _set_file_status(self, text: str) -> None:
        """Show a line about the keys file, or hide the line when ``text`` is empty."""
        self._file_status.setText(text)
        self._file_status.setVisible(bool(text))

    def _reorder(self, entries: list[Any]) -> None:
        """Lay the cards out in the order of ``entries``, which lists the critical names first. RunPod sits where its entry is.

        The cards of one subgroup sit together under its heading. A subgroup's block goes where its first card is in
        ``entries``. Entries list the critical names first, so a block with a critical card comes before the others. A From
        file card is not in ``entries``, so it keeps its place after the keychain names.
        """
        blocks: list[list[QWidget]] = []
        block_of: dict[str, list[QWidget]] = {}
        for entry in entries:
            frame: QFrame | None
            if entry.built_in:
                frame = self._runpod_frame
            elif entry.name in self._cards and not self._cards[entry.name].from_file:
                frame = self._cards[entry.name].frame
            else:
                continue
            if frame is None:
                continue
            label = "" if entry.built_in else self._subgroup_text(entry.name)
            if not label:
                blocks.append([frame])
                continue
            key = _subgroup_key(label)
            if key not in block_of:
                block_of[key] = [self._subgroup_heading(key, label)]
                blocks.append(block_of[key])
            block_of[key].append(frame)
        sequence = [widget for block in blocks for widget in block]
        for index, widget in enumerate(sequence):
            self._entry_layout.removeWidget(widget)
            self._entry_layout.insertWidget(index, widget)
        self._subgroup_in_use = set(block_of)
        # A heading with no card on the list leaves the layout, and is hidden so that it cannot show loose.
        for key, heading in self._subgroup_headings.items():
            if key not in block_of:
                self._entry_layout.removeWidget(heading)
                heading.hide()

    def _subgroup_heading(self, key: str, label: str) -> QLabel:
        """The heading of one subgroup. It is made once and reused, so a subgroup keeps the same widget."""
        heading = self._subgroup_headings.get(key)
        if heading is None:
            heading = _bold(_label(self, f"keys-subgroup-{slug(label)}"))
            self._subgroup_headings[key] = heading
        heading.setText(label)
        return heading

    def _show(self, text: str) -> None:
        self._message.setText(text)
        self._message.setVisible(bool(text))

    # ----------------------------------------------------------- search

    @Slot(str)
    def _group_text(self, name: str) -> str:
        """The group a listed key is in, or "" for a name that is not listed (a file-only name, or RunPod's card)."""
        try:
            return self._keys.group_of(name)
        except KeyNameError:
            return ""

    def _subgroup_text(self, name: str) -> str:
        """The subgroup label a listed key is in, or "" when it has none (a file-only name, or none set)."""
        try:
            return self._keys.subgroup_of(name)
        except KeyNameError:
            return ""

    def _subgroup_line(self, name: str) -> str:
        """The line a keychain card shows for its subgroup. A key with no subgroup shows NO_SUBGROUP_TEXT."""
        return SUBGROUP_TEXT.format(subgroup=self._subgroup_text(name) or NO_SUBGROUP_TEXT)

    def _on_group_saved(self, name: str) -> None:
        """Save the group label the owner typed for a keychain card, and show it on the card."""
        key = slug(name)
        edit = self.findChild(QLineEdit, f"keys-entry-{key}-group-edit")
        try:
            group = self._keys.set_group(name, edit.text() if edit is not None else "")
        except KeyNameError as exc:
            self._show(str(exc))
            return
        except OSError:
            self._show(NOT_WRITTEN_TEXT)
            return
        if edit is not None:
            edit.clear()
        label = self.findChild(QLabel, f"keys-entry-{key}-group")
        if label is not None:
            label.setText(GROUP_TEXT.format(group=group))
        self._apply_filter()
        self._show(GROUP_SAVED_TEXT.format(name=name, group=group))

    def _on_subgroup_saved(self, name: str) -> None:
        """Save the subgroup the owner typed for a keychain card, or clear it when the box is empty, then lay out again."""
        key = slug(name)
        edit = self.findChild(QLineEdit, f"keys-entry-{key}-subgroup-edit")
        try:
            subgroup = self._keys.set_subgroup(name, edit.text() if edit is not None else "")
        except KeyNameError as exc:
            self._show(str(exc))
            return
        except OSError:
            self._show(NOT_WRITTEN_TEXT)
            return
        if edit is not None:
            edit.clear()
        # The card moves under its subgroup heading, or out from under one, so the list is laid out again.
        self.refresh()
        if subgroup:
            self._show(SUBGROUP_SAVED_TEXT.format(name=name, subgroup=subgroup))
        else:
            self._show(SUBGROUP_CLEARED_TEXT.format(name=name))

    def _on_search_changed(self, _text: str) -> None:
        self._apply_filter()

    def _apply_filter(self) -> None:
        """Show each card whose name, or the description of its known service, holds the search text, ignoring case.

        Values and statuses are never matched. Group and subgroup labels are matched too.
        """
        query = self._search.text().strip().casefold()
        frames = [(name, card.frame) for name, card in self._cards.items()]
        if self._runpod_frame is not None:
            frames.append((RUNPOD_NAME, self._runpod_frame))
        any_match = False
        # A group heading shows only while one of its From file cards is on screen.
        group_shown = {True: False, False: False}
        # A subgroup heading shows only while one of its keychain cards is on screen.
        subgroup_shown: set[str] = set()
        for name, frame in frames:
            card = self._cards.get(name)
            keychain = card is not None and not card.from_file
            subgroup = self._subgroup_text(name) if keychain else ""
            match = (
                not query
                or query in name.casefold()
                or query in _service_text(name)
                or query in self._group_text(name).casefold()
                or query in subgroup.casefold()
            )
            frame.setVisible(match)
            any_match = any_match or match
            if match and card is not None and card.from_file:
                group_shown[is_classified(name)] = True
            if match and subgroup:
                subgroup_shown.add(_subgroup_key(subgroup))
        self._classified_heading.setVisible(group_shown[True])
        self._unclassified_heading.setVisible(group_shown[False])
        for key, heading in self._subgroup_headings.items():
            heading.setVisible(key in self._subgroup_in_use and key in subgroup_shown)
        # Nothing is said while the names are still being read, because no card exists yet to match against.
        self._search_empty.setVisible(bool(query) and not any_match and not self._loading)

    # ----------------------------------------------------------- actions

    @Slot()
    def _on_add_clicked(self) -> None:
        try:
            listed = self._keys.save(self._new_name.text(), self._new_value.text())
        except (KeyNameError, ValueError) as exc:
            self._show(str(exc))  # fixed wording from api_keys; the value typed is not repeated
            return
        except SecretStoreError as exc:
            self._show(f"Not saved. {exc}")  # names the step and the class only
            return
        except OSError:
            self._show(NOT_WRITTEN_TEXT)
            return
        self._new_name.clear()
        self._new_value.clear()
        self._new_service.setCurrentText(OTHER_CHOICE)
        self._reset_check(listed)
        self.refresh()
        self._show(ADDED_TEXT.format(name=listed))

    @Slot(int)
    def _on_service_picked(self, index: int) -> None:
        """Fill the name with the service the owner picked. Other leaves the name as the owner typed it."""
        choice = self._new_service.itemText(index)
        if choice != OTHER_CHOICE:
            self._new_name.setText(choice)

    def _on_reveal(self, name: str) -> None:
        """Show the saved value of one name for REVEAL_SECONDS. A press while it is shown reads it again and restarts the time."""
        reveal = self._reveals.get(name)
        if reveal is None:
            return
        try:
            value = self._keys.read_value(name)
        except KeyNameError as exc:
            self._mask(name)
            self._show(str(exc))
            return
        except SecretStoreError:
            self._mask(name)
            self._show(REVEAL_NOT_READ_TEXT)  # names no step, class or value
            return
        if not value:
            self._mask(name)
            self._show(REVEAL_NO_VALUE_TEXT.format(name=name))
            return
        reveal.shown.setText(value)
        reveal.shown.setVisible(True)
        reveal.hide.setVisible(True)
        reveal.timer.start()  # starting a running timer restarts it, so a second press gives the full time again
        self._show(REVEAL_SHOWN_TEXT.format(name=name, seconds=REVEAL_SECONDS))

    def _mask(self, name: str) -> None:
        """Hide the value of one name at once: clear its field, hide the field and Hide value, and stop its timer."""
        reveal = self._reveals.get(name)
        if reveal is None:
            return
        reveal.timer.stop()
        reveal.shown.clear()
        reveal.shown.setVisible(False)
        reveal.hide.setVisible(False)

    def _auto_hide(self, name: str) -> None:
        """The timer's handler: mask the value of one name once its time is up, and say so."""
        if name not in self._reveals:
            return
        self._mask(name)
        self._show(REVEAL_HIDDEN_TEXT.format(name=name))

    def _on_copy_value(self, name: str) -> None:
        """Copy the saved value of one name to the clipboard. It is cleared CLIPBOARD_SECONDS later if it is still there."""
        try:
            value = self._keys.read_value(name)
        except KeyNameError as exc:
            self._show(str(exc))
            return
        except SecretStoreError:
            self._show(COPY_NOT_READ_TEXT)
            return
        if not value:
            self._show(COPY_NO_VALUE_TEXT.format(name=name))
            return
        _system_clipboard().setText(value)
        self._copied_digest = _digest(value)
        self._clipboard_timer.start()  # a later copy restarts the time, for the newest value
        self._show(COPIED_VALUE_TEXT.format(name=name, seconds=CLIPBOARD_SECONDS))

    def _clear_copied_value(self) -> None:
        """The clipboard timer's handler: clear the clipboard, but only while it still holds the value this section copied."""
        digest, self._copied_digest = self._copied_digest, None
        if digest is None:
            return
        clipboard = _system_clipboard()
        if hmac.compare_digest(_digest(clipboard.text()), digest):
            clipboard.clear()

    def _on_save_value(self, name: str) -> None:
        card = self._cards.get(name)
        if card is None or card.value is None:
            return  # a From file name has no value box: its value stays in the file
        typed = card.value.text()
        if not typed.strip():
            self._show(NO_VALUE_TEXT)
            return
        try:
            listed = self._keys.save(name, typed)
        except (KeyNameError, ValueError) as exc:
            self._show(str(exc))
            return
        except SecretStoreError as exc:
            self._show(f"Not saved. {exc}")
            return
        except OSError:
            self._show(NOT_WRITTEN_TEXT)
            return
        card.value.clear()
        self._reset_check(listed)
        self.refresh()
        self._show(REPLACED_TEXT.format(name=listed))

    def _on_critical_toggled(self, name: str, critical: bool) -> None:
        self._set_critical_cached(name, critical)
        try:
            listed = self._keys.set_critical(name, critical)
        except KeyNameError as exc:
            self.refresh()
            self._show(str(exc))
            return
        except OSError:
            # The box goes back to the mark the settings file still holds, because refresh reads it again.
            self.refresh()
            self._show(NOT_WRITTEN_TEXT)
            return
        self.refresh()
        self._show((CRITICAL_TEXT if critical else UNCRITICAL_TEXT).format(name=listed))

    def _on_user_env(self, name: str) -> None:
        if self._keys.status(name) == STATUS_NOT_SET:
            self._show(USER_ENV_NO_VALUE_TEXT.format(name=name))
            return
        try:
            variable = self._keys.environment_name(name)
        except KeyNameError as exc:
            self._show(str(exc))
            return
        body = USER_ENV_BODY.format(name=name, variable=variable)
        ask = self._confirm if self._confirm is not None else _ask_user_env
        if not ask(self, USER_ENV_TITLE, body):
            self._show(USER_ENV_CANCELLED_TEXT)
            return
        try:
            written = self._keys.set_user_environment(name)
        except KeyNameError as exc:
            self._show(str(exc))
            return
        except (SecretStoreError, UserEnvironmentError) as exc:
            self._show(USER_ENV_NOT_SET_TEXT.format(detail=exc))  # names the step and the class only
            return
        except OSError:
            self._show(USER_ENV_OS_TEXT)
            return
        text = USER_ENV_SET_TEXT.format(variable=written.variable)
        if not written.announced:
            text = f"{text} {USER_ENV_UNANNOUNCED_TEXT}"
        self._show(text)

    def _reset_check(self, name: str) -> None:
        """A saved value changed, so an older check for the name is dropped and its result cleared.

        A check still running is dropped here too, so its result, when it arrives, has no pending token to match.
        """
        self._mask(name)  # a value shown before the change is no longer the saved one, so it is hidden
        self._pending.pop(name, None)
        card = self._cards.get(name)
        if card is not None and card.check is not None and card.check_result is not None:
            card.check.setEnabled(True)
            card.check_result.setText("")

    def _on_check(self, name: str) -> None:
        """Start the owner's check of the key for ``name``. It runs on the thread pool and sends one GET.

        A keychain name's check reads its keychain value. A From file name's check reads its value from the keys file, at
        the moment the check runs. Either value goes only to that one request, and is never kept or shown.
        """
        card = self._cards.get(name)
        service = service_for(name)
        if card is None or card.check is None or card.check_result is None or service is None:
            return
        if name in self._pending:
            return  # one check at a time for a card
        token = next(self._check_tokens)
        self._pending[name] = token
        card.check.setEnabled(False)
        card.check_result.setText(CHECKING_TEXT.format(service=service.name))
        # The job holds the service, the name, the store, the transport and the file path, never this block or a widget.
        store, transport = self._store, self._transport
        if card.from_file:
            keys_file = self._keys_file_path()
            submit(
                self._check_owner,
                token,
                lambda: check_saved_key(service, name, store, transport=transport, keys_file=keys_file),
            )
        else:
            submit(self._check_owner, token, lambda: check_saved_key(service, name, store, transport=transport))

    @Slot(object)
    def _on_check_delivered(self, item: object) -> None:
        # Runs on the GUI thread. A result for another section, for a check that was reset, or for a gone card is dropped.
        if not isinstance(item, Delivered) or item.owner is not self._check_owner:
            return
        token, (message, failure) = item.value
        name = next((listed for listed, pending in self._pending.items() if pending == token), None)
        if name is None:
            return
        del self._pending[name]
        card = self._cards.get(name)
        if card is None or card.check is None or card.check_result is None:
            return
        card.check.setEnabled(True)
        if failure is None and isinstance(message, str):
            card.check_result.setText(message)
            return
        # The worker raised, so there is no message from it. Show the unreachable text, which names no value.
        service = service_for(name)
        card.check_result.setText(UNREACHABLE_TEXT.format(service=service.name if service is not None else name))

    def _on_remove(self, name: str) -> None:
        if name in BUILT_IN_NAMES:
            return  # RunPod keeps its own Remove key button, on its card
        self._mask(name)  # a shown value is hidden before the owner is asked, and stays hidden if the removal fails
        card = self._cards.get(name)
        if card is not None and card.from_file:
            # A name from the keys file leaves the list for this session only. The file is never changed by a removal.
            self._hidden_file_slugs.add(slug(name))
            self.refresh()
            self._show(REMOVED_FROM_FILE_TEXT.format(name=name))
            return
        body = REMOVE_BODY.format(name=name, listing=REMOVE_LISTING)
        ask = self._confirm if self._confirm is not None else _ask_remove
        if not ask(self, REMOVE_TITLE, body):
            self._show(CANCELLED_TEXT)
            return
        try:
            removed = self._keys.remove(name)
        except KeyNameError as exc:
            self._show(str(exc))
            return
        except SecretStoreError as exc:
            self._show(NOT_REMOVED_TEXT.format(detail=exc))
            return
        except OSError:
            # The keychain value is deleted before the names list is written, so it can already be gone here. The card is
            # read again, so it never says a value is set when the keychain no longer holds one.
            self.refresh()
            self._show(NOT_REMOVED_OS_TEXT)
            return
        self.refresh()
        self._show((REMOVED_TEXT if removed else REMOVED_NO_VALUE_TEXT).format(name=name))

    @Slot()
    def _on_choose_file(self) -> None:
        """Choose a keys file. A file that is refused is not chosen, and the path already saved stays as it was."""
        chosen = self._choose_file(self)
        if not chosen:
            return  # the owner cancelled the dialog, so nothing changed
        listing = read_names(chosen)
        self._log_listing(chosen, listing)
        if listing.problem is not None:
            self._set_file_status(listing.problem)
            return
        try:
            self._save_keys_file(chosen)
        except OSError:
            self._set_file_status(NOT_WRITTEN_TEXT)
            return
        self._hidden_file_slugs.clear()
        self.refresh()

    @Slot()
    def _on_stop_file(self) -> None:
        """Stop using the keys file. Its names leave the list. The file itself is not changed."""
        try:
            self._save_keys_file("")
        except OSError:
            self._set_file_status(NOT_WRITTEN_TEXT)
            return
        self._hidden_file_slugs.clear()
        self.refresh()
        self._show(FILE_STOPPED_TEXT)

    @Slot()
    def _on_export_names(self) -> None:
        """Write the names-only export beside settings.json: each key name and its classification, and no value."""
        path = self._keys_file_path()
        file_names = read_names(path).names if path else ()
        try:
            written = write_names_export(
                self._settings.path.parent / BACKUP_DIR_NAME,
                self._keys.names(),
                file_names,
                taken_at=utc_now(),
            )
        except OSError:
            self._show(EXPORT_FAILED_TEXT)
            return
        self._show(EXPORTED_TEXT.format(path=written))

    @Slot()
    def _on_import_vault(self) -> None:
        """Choose a vault file and open the import dialog on it. The dialog opens as a window and is never run with exec()."""
        chosen = self._choose_vault(self)
        if not chosen:
            return  # the owner cancelled the file dialog, so nothing opened
        previous = self._import_dialog
        if previous is not None:
            previous.deleteLater()
        dialog = KeyImportDialog(Path(chosen), self._keys, self)
        dialog.imported.connect(self._on_vault_imported)
        self._import_dialog = dialog
        dialog.open()

    @Slot(int)
    def _on_vault_imported(self, count: int) -> None:
        """Show the imported names in the list, and say how many keys came in. The Windows user variables are not touched."""
        self.refresh()
        self._show(IMPORTED_SECTION_ONE_TEXT if count == 1 else IMPORTED_SECTION_MANY_TEXT.format(count=count))

    @Slot()
    def _on_import_plain(self) -> None:
        """Choose a keys file, then ask the Yes or No question. Only a Yes reads the file's names, into the plain import dialog.

        A name the file's extension does not show as a keys file is refused before the file is opened. No value is read before
        the owner answers Yes, and a No or Escape reads nothing and writes nothing.
        """
        chosen = self._choose_plain(self)
        if not chosen:
            return  # the owner cancelled the file dialog, so nothing was read
        path = Path(chosen)
        if file_format(path) is None:
            self._show(NOT_SUPPORTED_TEXT)  # decided by the file name alone, so the file is not opened
            return
        if not self._confirm_plain(self, PLAIN_QUESTION_TITLE, PLAIN_QUESTION_TEXT.format(name=path.name)):
            self._show(PLAIN_CANCELLED_TEXT)
            return
        listing = read_names(path)
        previous = self._plain_dialog
        if previous is not None:
            previous.deleteLater()
        dialog = KeyPlainImportDialog(path, listing, self._keys, self, confirm_environment=self._confirm_plain_env)
        dialog.imported.connect(self._on_plain_imported)
        self._plain_dialog = dialog
        dialog.open()

    @Slot(int)
    def _on_plain_imported(self, count: int) -> None:
        """Show the imported names in the list and say how many keys came in. The file and user variables are untouched."""
        self.refresh()
        self._show(IMPORTED_PLAIN_ONE_TEXT if count == 1 else IMPORTED_PLAIN_MANY_TEXT.format(count=count))

    @Slot()
    def _on_export_keys(self) -> None:
        """Open the Export keys dialog. It writes only to a path the owner picks, and it is never run with exec()."""
        previous = self._export_dialog
        if previous is not None:
            previous.deleteLater()
        dialog = KeyExportDialog(self._keys, self, choose_path=self._choose_export_path, confirm=self._confirm_export)
        dialog.exported.connect(self._on_keys_exported)
        self._export_dialog = dialog
        dialog.open()

    @Slot(int)
    def _on_keys_exported(self, count: int) -> None:
        """Say how many keys were written. The list is not refreshed, because an export changes no keychain value."""
        self._show(EXPORTED_KEYS_ONE_TEXT if count == 1 else EXPORTED_KEYS_MANY_TEXT.format(count=count))

    def _save_keys_file(self, path: str) -> None:
        """Save the keys file path, or "" to stop using a file. Every other settings key is kept.

        Raises OSError when the settings file cannot be written.
        """
        self._settings.keep_unreadable_copy()
        current = self._settings.load().settings
        self._settings.save(replace(current, keys_file_path=path))


__all__ = [
    "ADDED_TEXT",
    "CHECKING_TEXT",
    "CHECK_TEXT",
    "CRITICAL_BADGE_TEXT",
    "CRITICAL_TEXT",
    "CREATE_TEXT",
    "FILE_LISTED_MANY_TEXT",
    "FILE_LISTED_ONE_TEXT",
    "FILE_PATH_TEXT",
    "FILE_STOPPED_TEXT",
    "FROM_FILE_NOTE",
    "KeysSection",
    "NOT_WRITTEN_TEXT",
    "NO_FILE_TEXT",
    "OTHER_CHOICE",
    "REMOVED_FROM_FILE_TEXT",
    "REMOVED_TEXT",
    "REPLACED_TEXT",
    "SEARCH_EMPTY_TEXT",
    "SEARCH_PLACEHOLDER",
    "SERVICE_CHOICES",
    "STATUS_TEXT",
    "UNCRITICAL_TEXT",
    "USER_ENV_SET_TEXT",
    "USER_ENV_TITLE",
]
