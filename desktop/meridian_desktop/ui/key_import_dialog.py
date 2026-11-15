"""The Import keys from a vault file dialog: unlock one vault file with its password, tick the names to bring in, and save them
to the keychain. Qt code, so it sits in ui/.

The password is read from its field, used once and cleared. The vault is open in memory only while the dialog is open after
a correct password, and it is locked again after each import and when the dialog closes. No value is put in a widget. The
keychain writes go through key_import.import_checked, which uses ApiKeys.save, so the Windows user variables are never
written here.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtWidgets import QCheckBox, QDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget

from meridian_desktop import key_vault
from meridian_desktop.api_keys import ApiKeys, slug
from meridian_desktop.key_import import ImportResult, import_checked, is_already_saved
from meridian_desktop.key_vault import Vault, VaultError
from meridian_desktop.ui.text import make_selectable

TITLE_TEXT = "Import keys from a vault file"
INTRO_TEXT = (
    "Use this to bring keys in from a vault file this app made, for example after you move to another computer. Enter the "
    "vault file password, then tick the names to import. Each value goes into this computer's keychain."
)
FILE_TEXT = "Vault file: {name}"
PASSWORD_PLACEHOLDER = "Vault file password"
UNLOCK_TEXT = "Unlock"
IMPORT_TEXT = "Import checked keys"
CLOSE_TEXT = "Close"
REPLACE_TEXT = "Replace existing keys with the values in the vault file"
PASSWORD_FIRST_TEXT = "Type the vault file password first."
EMPTY_VAULT_TEXT = "This vault file holds no keys, so there is nothing to import."
UNLOCKED_TEXT = "Unlocked. The password was not kept. Tick the names to import, then press Import checked keys."
SAVED_NOTE_TEXT = "A value is already saved for this name, so it is kept unless Replace existing keys is ticked."
LOCKED_AGAIN_TEXT = "The vault is locked again and the password was not kept."
NONE_IMPORTED_TEXT = "No key was imported."
IMPORTED_ONE_TEXT = "Imported 1 key into the keychain."
IMPORTED_MANY_TEXT = "Imported {count} keys into the keychain."
KEPT_ONE_TEXT = "Kept 1 existing key. Tick Replace existing keys to overwrite it."
KEPT_MANY_TEXT = "Kept {count} existing keys. Tick Replace existing keys to overwrite them."
SKIPPED_TEXT = "Not ticked, so not imported: {count}."
REFUSED_TEXT = "Not imported, {name}: {reason}"
UNCHANGED_TEXT = "Windows user variables were not changed, and the vault file was not changed."


def _label(parent: QWidget, name: str, text: str = "") -> QLabel:
    label = QLabel(text, parent)
    label.setObjectName(name)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return make_selectable(label)


def _summary(result: ImportResult) -> str:
    """What the import did, in plain words. It names the refused keys and their fixed reasons, never a value."""
    lines: list[str] = []
    if not result.imported:
        lines.append(NONE_IMPORTED_TEXT)
    elif len(result.imported) == 1:
        lines.append(IMPORTED_ONE_TEXT)
    else:
        lines.append(IMPORTED_MANY_TEXT.format(count=len(result.imported)))
    if len(result.kept) == 1:
        lines.append(KEPT_ONE_TEXT)
    elif result.kept:
        lines.append(KEPT_MANY_TEXT.format(count=len(result.kept)))
    if result.skipped:
        lines.append(SKIPPED_TEXT.format(count=len(result.skipped)))
    for refusal in result.refused:
        lines.append(REFUSED_TEXT.format(name=refusal.name, reason=refusal.reason))
    lines.append(UNCHANGED_TEXT)
    return "\n".join(lines)


class KeyImportDialog(QDialog):
    """Unlock one vault file and import the ticked names. ``imported`` carries how many keys were saved to the keychain."""

    imported = Signal(int)

    def __init__(self, path: Path, keys: ApiKeys, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("keys-import-dialog")
        self.setWindowTitle(TITLE_TEXT)
        self.setModal(True)
        self._path = path
        self._keys = keys
        self._vault: Vault | None = None
        # The ticked-name boxes of the open vault, by vault name. The widgets listed in _name_widgets are removed on lock.
        self._boxes: dict[str, QCheckBox] = {}
        self._name_widgets: list[QWidget] = []
        self._build()
        self._show_locked()

    def _build(self) -> None:
        column = QVBoxLayout(self)
        column.addWidget(_label(self, "keys-import-intro", INTRO_TEXT))
        self._file = _label(self, "keys-import-file", FILE_TEXT.format(name=self._path.name))
        self._file.setToolTip(str(self._path))
        column.addWidget(self._file)

        row = QHBoxLayout()
        self._password = QLineEdit(self)
        self._password.setObjectName("keys-import-password")
        self._password.setEchoMode(QLineEdit.EchoMode.Password)
        self._password.setPlaceholderText(PASSWORD_PLACEHOLDER)
        self._unlock = QPushButton(UNLOCK_TEXT, self)
        self._unlock.setObjectName("keys-import-unlock")
        row.addWidget(self._password, 1)
        row.addWidget(self._unlock)
        column.addLayout(row)

        self._status = _label(self, "keys-import-status")
        column.addWidget(self._status)

        self._names = QVBoxLayout()
        self._names.setSpacing(4)
        column.addLayout(self._names)

        self._replace = QCheckBox(REPLACE_TEXT, self)
        self._replace.setObjectName("keys-import-replace")
        self._replace.setChecked(False)
        column.addWidget(self._replace)

        self._result = _label(self, "keys-import-result")
        column.addWidget(self._result)

        buttons = QHBoxLayout()
        self._go = QPushButton(IMPORT_TEXT, self)
        self._go.setObjectName("keys-import-go")
        self._close = QPushButton(CLOSE_TEXT, self)
        self._close.setObjectName("keys-import-close")
        buttons.addStretch(1)
        buttons.addWidget(self._go)
        buttons.addWidget(self._close)
        column.addLayout(buttons)

        self._unlock.clicked.connect(self._on_unlock)
        self._password.returnPressed.connect(self._on_unlock)
        self._go.clicked.connect(self._on_import)
        self._close.clicked.connect(lambda *_: self.reject())

    # ----------------------------------------------------------- state on screen

    def _show_locked(self) -> None:
        self._password.setVisible(True)
        self._unlock.setVisible(True)
        self._replace.setVisible(False)
        self._go.setEnabled(False)

    def _show_unlocked(self) -> None:
        self._password.setVisible(False)
        self._unlock.setVisible(False)
        self._replace.setVisible(True)
        self._go.setEnabled(True)

    def _show_names(self, vault: Vault) -> None:
        """One ticked box for each name in the vault, and a note on each name that already has a saved value."""
        self._clear_names()
        for name in vault.names():
            key = slug(name)
            box = QCheckBox(name, self)
            box.setObjectName(f"keys-import-name-{key}")
            box.setChecked(True)
            box.setVisible(True)
            self._names.addWidget(box)
            self._boxes[name] = box
            self._name_widgets.append(box)
            if is_already_saved(self._keys, name):
                note = _label(self, f"keys-import-note-{key}", SAVED_NOTE_TEXT)
                note.setVisible(True)
                self._names.addWidget(note)
                self._name_widgets.append(note)

    def _clear_names(self) -> None:
        for widget in self._name_widgets:
            self._names.removeWidget(widget)
            widget.hide()
            widget.setParent(None)
            widget.deleteLater()
        self._name_widgets = []
        self._boxes = {}

    def _set_status(self, text: str) -> None:
        self._status.setText(text)

    # ----------------------------------------------------------- actions

    def _lock_vault(self) -> None:
        """Forget the open vault and its values, hide every name, and clear the password and the Replace choice."""
        if self._vault is not None:
            self._vault.lock()
        self._vault = None
        self._clear_names()
        self._replace.setChecked(False)
        self._password.clear()

    @Slot()
    def _on_unlock(self) -> None:
        password = self._password.text()
        self._password.clear()
        if not password:
            self._set_status(PASSWORD_FIRST_TEXT)
            return
        try:
            vault = key_vault.unlock(self._path, password)
        except VaultError as exc:
            # A wrong password or a changed file writes nothing. The message is the vault's own fixed sentence.
            self._set_status(str(exc))
            return
        if not vault.names():
            vault.lock()
            self._set_status(EMPTY_VAULT_TEXT)
            return
        self._vault = vault
        self._show_names(vault)
        self._show_unlocked()
        self._set_status(UNLOCKED_TEXT)

    @Slot()
    def _on_import(self) -> None:
        if self._vault is None:
            return
        ticked = [name for name, box in self._boxes.items() if box.isChecked()]
        try:
            result = import_checked(self._keys, self._vault, ticked, replace=self._replace.isChecked())
        finally:
            self._lock_vault()
            self._show_locked()
        self._result.setText(_summary(result))
        self._set_status(LOCKED_AGAIN_TEXT)
        if result.imported:
            self.imported.emit(len(result.imported))

    def done(self, result: int) -> None:
        """Close the dialog. However it closes, the vault is locked first, so no value stays in memory."""
        self._lock_vault()
        super().done(result)


__all__ = ["KeyImportDialog"]
