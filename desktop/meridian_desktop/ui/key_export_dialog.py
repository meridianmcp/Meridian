"""The Export keys dialog: choose an encrypted vault file (the default) or a plaintext JSON file, and write every saved key to
one file the owner picks. Qt code, so it sits in ui/.

The encrypted file takes a passphrase typed twice. Its Choose where to save button stays disabled until both fields match and
are long enough. The plaintext file opens a second dialog first: it says the file holds every key in readable form, and its
write button stays disabled until the owner types PLAINTEXT exactly. The writes go through key_export.export_keys, which runs
every refusal (a folder inside a git repository, an existing file, a missing confirmation) before it reads a value.

The passphrase is read from its fields, used once and cleared after a write or when the dialog closes. No value is put in a
widget. A file that already exists is replaced only when the owner answers Yes to a question that defaults to No.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from meridian_desktop.api_keys import ApiKeys
from meridian_desktop.key_export import (
    DEFAULT_FILE_NAMES,
    KIND_ENCRYPTED,
    KIND_PLAINTEXT,
    PLAINTEXT_CONFIRMATION,
    ExportError,
    ExportResult,
    TargetExistsError,
    count_saved,
    export_keys,
)
from meridian_desktop.key_vault import MIN_PASSWORD_LENGTH
from meridian_desktop.ui.confirm import ask_yes_no
from meridian_desktop.ui.text import make_selectable

TITLE_TEXT = "Export keys for migration"
INTRO_TEXT = (
    "This writes every key that has a saved value in this computer's keychain to one file, so you can move the keys somewhere "
    "else. The keychain and the Windows user variables are not changed. Choose a format."
)
ENCRYPTED_CHOICE_TEXT = "Encrypted vault file (recommended). Opened with a passphrase you choose now. Import it with Import keys from a vault file."
PLAINTEXT_CHOICE_TEXT = "Plaintext JSON file. Every key is readable in the file. Use it only when an encrypted file cannot be used."
PASSPHRASE_PLACEHOLDER = f"Passphrase, at least {MIN_PASSWORD_LENGTH} characters"
PASSPHRASE_AGAIN_PLACEHOLDER = "Type the passphrase again"
PASSPHRASE_NOTE_TEXT = (
    "This passphrase is the only way to open the file. It is not saved anywhere, and it cannot be recovered if it is lost."
)
SHORT_HINT_TEXT = f"Use a passphrase of at least {MIN_PASSWORD_LENGTH} characters."
RETYPE_HINT_TEXT = "Type the passphrase again to confirm it."
MISMATCH_HINT_TEXT = "The two passphrases do not match yet."
COUNT_ONE_TEXT = "1 key has a saved value and will be exported."
COUNT_MANY_TEXT = "{count} keys have a saved value and will be exported."
COUNT_NONE_TEXT = "No key has a saved value, so there is nothing to export."
CHOOSE_TEXT = "Choose where to save"
CONTINUE_PLAINTEXT_TEXT = "Continue to plaintext confirmation"
CLOSE_TEXT = "Close"
SAVE_TITLE = "Save the key export as"
SAVE_FILTER = "Key export files (*.json);;All files (*)"
CANCELLED_TEXT = "Cancelled. Nothing was written."
REPLACE_TITLE = "Replace the existing file?"
REPLACE_BODY = "A file named {name} already exists in that folder. Replacing it cannot be undone. Choose No to keep it."
KEPT_TEXT = "The existing file was kept. Nothing was written."
DONE_ENCRYPTED_TEXT = (
    "Exported {count} keys to {path}. The file is encrypted, and you need its passphrase to import it. Keep the file and the "
    "passphrase private."
)
DONE_PLAINTEXT_TEXT = "Wrote {count} keys in plain text to {path}. Anyone who can open that file can read every key."
PLAINTEXT_TITLE = "Confirm a plaintext export"
PLAINTEXT_WARNING_TEXT = (
    "This file holds {count} keys in readable form. Anyone who opens it can read every value. The file is not encrypted, and "
    "nothing in this app protects it once it is written. Type PLAINTEXT below to confirm."
)
PLAINTEXT_PLACEHOLDER = "Type PLAINTEXT"
WRITE_PLAINTEXT_TEXT = "Write plaintext file"
CANCEL_TEXT = "Cancel"

# A file picker: (parent, default file name) -> the path the owner picked, or "" when the owner cancelled.
PathChooser = Callable[[QWidget, str], str]
# A Yes or No question: (parent, title, text) -> True only when the owner clicks Yes.
Confirm = Callable[[QWidget, str, str], bool]


def _label(parent: QWidget, name: str, text: str = "") -> QLabel:
    label = QLabel(text, parent)
    label.setObjectName(name)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return make_selectable(label)


def _count_text(count: int) -> str:
    if count == 0:
        return COUNT_NONE_TEXT
    if count == 1:
        return COUNT_ONE_TEXT
    return COUNT_MANY_TEXT.format(count=count)


def _ask_export_path(parent: QWidget, default_name: str) -> str:
    """The save dialog for an export. It opens on the owner's home folder. It does not ask about overwriting: the export
    dialog asks that itself, with the default answer No, so the owner sees one question and not two.
    """
    path, _filter = QFileDialog.getSaveFileName(
        parent,
        SAVE_TITLE,
        str(Path.home() / default_name),
        SAVE_FILTER,
        "",
        QFileDialog.Option.DontConfirmOverwrite,
    )
    return path


class PlaintextConfirmDialog(QDialog):
    """The second step of a plaintext export. Its write button stays disabled until PLAINTEXT is typed exactly.

    ``confirmed`` carries the typed text. The export still checks it, so the gate does not depend on this dialog alone.
    """

    confirmed = Signal(str)

    def __init__(self, count: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("keys-export-plaintext-dialog")
        self.setWindowTitle(PLAINTEXT_TITLE)
        self.setModal(True)
        column = QVBoxLayout(self)
        column.addWidget(_label(self, "keys-export-plaintext-warning", PLAINTEXT_WARNING_TEXT.format(count=count)))
        self._typed = QLineEdit(self)
        self._typed.setObjectName("keys-export-plaintext-confirm")
        self._typed.setPlaceholderText(PLAINTEXT_PLACEHOLDER)
        column.addWidget(self._typed)
        buttons = QHBoxLayout()
        self._write = QPushButton(WRITE_PLAINTEXT_TEXT, self)
        self._write.setObjectName("keys-export-plaintext-write")
        self._write.setEnabled(False)
        self._cancel = QPushButton(CANCEL_TEXT, self)
        self._cancel.setObjectName("keys-export-plaintext-cancel")
        buttons.addStretch(1)
        buttons.addWidget(self._write)
        buttons.addWidget(self._cancel)
        column.addLayout(buttons)
        self._typed.textChanged.connect(self._on_typed)
        self._write.clicked.connect(self._on_write)
        self._cancel.clicked.connect(lambda *_: self.reject())

    @Slot(str)
    def _on_typed(self, text: str) -> None:
        self._write.setEnabled(text == PLAINTEXT_CONFIRMATION)

    @Slot()
    def _on_write(self) -> None:
        typed = self._typed.text()
        if typed != PLAINTEXT_CONFIRMATION:
            return
        self.confirmed.emit(typed)
        self.accept()


class KeyExportDialog(QDialog):
    """Write every saved key to one file the owner picks, in the chosen format. ``exported`` carries how many keys were written."""

    exported = Signal(int)

    def __init__(
        self,
        keys: ApiKeys,
        parent: QWidget | None = None,
        *,
        choose_path: PathChooser | None = None,
        confirm: Confirm | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("keys-export-dialog")
        self.setWindowTitle(TITLE_TEXT)
        self.setModal(True)
        self._keys = keys
        # None means the real save dialog and the real Yes or No question. Tests pass their own.
        self._choose_path = choose_path if choose_path is not None else _ask_export_path
        self._confirm = confirm if confirm is not None else ask_yes_no
        self._count = count_saved(keys)
        self._plaintext_dialog: PlaintextConfirmDialog | None = None
        self._build()
        self._refresh_state()

    def _build(self) -> None:
        column = QVBoxLayout(self)
        column.addWidget(_label(self, "keys-export-intro", INTRO_TEXT))
        self._count_label = _label(self, "keys-export-count", _count_text(self._count))
        column.addWidget(self._count_label)

        self._encrypted = QRadioButton(ENCRYPTED_CHOICE_TEXT, self)
        self._encrypted.setObjectName("keys-export-format-encrypted")
        self._encrypted.setChecked(True)
        self._plaintext = QRadioButton(PLAINTEXT_CHOICE_TEXT, self)
        self._plaintext.setObjectName("keys-export-format-plaintext")
        column.addWidget(self._encrypted)
        column.addWidget(self._plaintext)

        self._passphrase = QLineEdit(self)
        self._passphrase.setObjectName("keys-export-passphrase")
        self._passphrase.setEchoMode(QLineEdit.EchoMode.Password)
        self._passphrase.setPlaceholderText(PASSPHRASE_PLACEHOLDER)
        self._passphrase_again = QLineEdit(self)
        self._passphrase_again.setObjectName("keys-export-passphrase-confirm")
        self._passphrase_again.setEchoMode(QLineEdit.EchoMode.Password)
        self._passphrase_again.setPlaceholderText(PASSPHRASE_AGAIN_PLACEHOLDER)
        self._passphrase_note = _label(self, "keys-export-passphrase-note", PASSPHRASE_NOTE_TEXT)
        column.addWidget(self._passphrase)
        column.addWidget(self._passphrase_again)
        column.addWidget(self._passphrase_note)

        self._hint = _label(self, "keys-export-hint")
        column.addWidget(self._hint)
        self._status = _label(self, "keys-export-status")
        column.addWidget(self._status)

        buttons = QHBoxLayout()
        self._go = QPushButton(CHOOSE_TEXT, self)
        self._go.setObjectName("keys-export-go")
        self._close = QPushButton(CLOSE_TEXT, self)
        self._close.setObjectName("keys-export-close")
        buttons.addStretch(1)
        buttons.addWidget(self._go)
        buttons.addWidget(self._close)
        column.addLayout(buttons)

        self._encrypted.toggled.connect(lambda *_: self._refresh_state())
        self._passphrase.textChanged.connect(lambda *_: self._refresh_state())
        self._passphrase_again.textChanged.connect(lambda *_: self._refresh_state())
        self._go.clicked.connect(self._on_go)
        self._close.clicked.connect(lambda *_: self.reject())

    # ----------------------------------------------------------- state on screen

    def _refresh_state(self) -> None:
        """Show the passphrase fields only for the encrypted format, and enable Go only when its inputs are ready."""
        encrypted = self._encrypted.isChecked()
        for widget in (self._passphrase, self._passphrase_again, self._passphrase_note):
            widget.setVisible(encrypted)
        if not encrypted:
            self._hint.setText("")
            self._go.setText(CONTINUE_PLAINTEXT_TEXT)
            self._go.setEnabled(self._count > 0)
            return
        self._go.setText(CHOOSE_TEXT)
        first = self._passphrase.text()
        again = self._passphrase_again.text()
        hint = ""
        if first and len(first) < MIN_PASSWORD_LENGTH:
            hint = SHORT_HINT_TEXT
        elif len(first) >= MIN_PASSWORD_LENGTH and first != again:
            hint = MISMATCH_HINT_TEXT if again else RETYPE_HINT_TEXT
        self._hint.setText(hint)
        ready = len(first) >= MIN_PASSWORD_LENGTH and first == again
        self._go.setEnabled(ready and self._count > 0)

    def _clear_passphrase(self) -> None:
        self._passphrase.clear()
        self._passphrase_again.clear()

    # ----------------------------------------------------------- actions

    @Slot()
    def _on_go(self) -> None:
        if self._plaintext.isChecked():
            self._open_plaintext_confirm()
            return
        self._export(KIND_ENCRYPTED, passphrase=self._passphrase.text(), confirmation="")

    def _open_plaintext_confirm(self) -> None:
        """Open the plaintext confirmation. It is opened with open(), never with exec(), like the other dialogs here."""
        previous = self._plaintext_dialog
        if previous is not None:
            previous.deleteLater()
        dialog = PlaintextConfirmDialog(self._count, self)
        dialog.confirmed.connect(self._on_plaintext_confirmed)
        self._plaintext_dialog = dialog
        dialog.open()

    @Slot(str)
    def _on_plaintext_confirmed(self, typed: str) -> None:
        self._export(KIND_PLAINTEXT, passphrase="", confirmation=typed)

    def _export(self, kind: str, *, passphrase: str, confirmation: str) -> None:
        """Ask for a path, then write. A file that exists is replaced only when the owner says Yes to the question."""
        chosen = self._choose_path(self, DEFAULT_FILE_NAMES[kind])
        if not chosen:
            self._status.setText(CANCELLED_TEXT)
            return
        target = Path(chosen)
        try:
            result = export_keys(self._keys, target, kind, passphrase=passphrase, confirmation=confirmation)
        except TargetExistsError:
            if not self._confirm(self, REPLACE_TITLE, REPLACE_BODY.format(name=target.name)):
                self._status.setText(KEPT_TEXT)
                return
            try:
                result = export_keys(
                    self._keys, target, kind, passphrase=passphrase, confirmation=confirmation, overwrite=True
                )
            except ExportError as exc:
                self._status.setText(str(exc))
                return
        except ExportError as exc:
            self._status.setText(str(exc))
            return
        self._show_done(result)

    def _show_done(self, result: ExportResult) -> None:
        template = DONE_PLAINTEXT_TEXT if result.kind == KIND_PLAINTEXT else DONE_ENCRYPTED_TEXT
        self._status.setText(template.format(count=result.count, path=str(result.path)))
        self._clear_passphrase()
        self.exported.emit(result.count)

    def done(self, result: int) -> None:
        """Close the dialog. However it closes, the passphrase fields are cleared first."""
        self._clear_passphrase()
        super().done(result)


__all__ = ["KeyExportDialog", "PathChooser", "PlaintextConfirmDialog"]
