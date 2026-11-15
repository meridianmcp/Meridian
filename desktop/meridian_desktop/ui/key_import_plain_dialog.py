"""The Import keys from a plain file dialog, and the Yes or No question that comes before a plain keys file is read. Qt code, so it
sits in ui/.

The owner picks a keys file in the Keys block and answers Yes to the question first. Only then is the file's list of names read
(keys_file.read_names), and this dialog shows one ticked box for each name. A value is read from the file one name at a time,
when Import checked keys is pressed, through key_import.import_plain_checked, and it goes only to ApiKeys.save. The file is
never written. The Windows user variables box starts unticked. If it is ticked, each imported key is also set as a Windows
user variable, after one confirmation. A known-service or built-in name that is imported is marked critical, as its file card
showed it, and any other name is not. Closing the dialog writes nothing.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from meridian_desktop.api_keys import ApiKeys, KeyNameError, slug
from meridian_desktop.key_import import ImportResult, import_plain_checked, is_already_saved
from meridian_desktop.keys_file import FileNames, is_classified, skipped_summary
from meridian_desktop.secret_store import SecretStoreError
from meridian_desktop.ui.confirm import NO_TEXT, YES_TEXT, ask_yes_no
from meridian_desktop.ui.text import make_selectable
from meridian_desktop.user_environment import UserEnvironmentError

# The question asked before a plain keys file is read. It says the file holds the keys in readable form, that the app will not
# change the file, and that imported keys go into the keychain only. No is the default.
PLAIN_QUESTION_TITLE = "Import keys from this file?"
PLAIN_QUESTION_TEXT = (
    "The keys in {name} are held in readable form, so anyone who can open the file can read them. This app will not change "
    "the file. Keys you import go into this computer's keychain, and you can also set them as Windows user variables. "
    "Read the file now?"
)
YES_OBJECT_NAME = "keys-plain-import-confirm-yes"
NO_OBJECT_NAME = "keys-plain-import-confirm-no"

TITLE_TEXT = "Import keys from a plain file"
INTRO_TEXT = (
    "Tick the names to import from this keys file. Each value is read from the file when you press Import checked keys, and "
    "goes into this computer's keychain. The file itself is not changed."
)
FILE_TEXT = "Keys file: {name}"
NAME_NOTE_TEXT = "A value is already saved for this name, so it is kept unless Replace existing keys is ticked."
REPLACE_TEXT = "Replace existing keys with the values in the file"
IMPORT_TEXT = "Import checked keys"
CLOSE_TEXT = "Close"
COUNT_TEXT = "Ticked {ticked} of {total} names."
NONE_IMPORTED_TEXT = "No key was imported."
IMPORTED_ONE_TEXT = "Imported 1 key into the keychain."
IMPORTED_MANY_TEXT = "Imported {count} keys into the keychain."
KEPT_ONE_TEXT = "Kept 1 existing key. Tick Replace existing keys to overwrite it."
KEPT_MANY_TEXT = "Kept {count} existing keys. Tick Replace existing keys to overwrite them."
SKIPPED_TEXT = "Not ticked, so not imported: {count}."
REFUSED_TEXT = "Not imported, {name}: {reason}"
UNCHANGED_TEXT = "Windows user variables were not changed, and the keys file was not changed."
FILE_UNCHANGED_TEXT = "The keys file was not changed."
ENV_TEXT = "Also set each imported key as a Windows user variable, so programs you run as this user can read it"
ENV_CONFIRM_TITLE = "Set Windows user variables?"
ENV_CONFIRM_TEXT = (
    "Write {count} Windows user variable(s): {names}. Their values are stored in plain text in your Windows user "
    "environment, and programs that are already running will not see them until they restart. Continue?"
)
ENV_SET_TEXT = "Set as Windows user variables: {names}. Restart programs you have open to see them."
ENV_SKIPPED_TEXT = "No Windows user variable was set."
ENV_REFUSED_TEXT = "Not set as a Windows user variable, {name}: {reason}"
ENV_READ_FAILED_TEXT = "the saved value could not be read"
ENV_WRITE_FAILED_TEXT = "the Windows user environment could not be written"
CRITICAL_NOT_SAVED_TEXT = "Not marked critical, {name}: the settings file could not be written."


def _label(parent: QWidget, name: str, text: str = "") -> QLabel:
    label = QLabel(text, parent)
    label.setObjectName(name)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return make_selectable(label)


def build_plain_question(parent: QWidget, title: str, text: str) -> QMessageBox:
    """The Yes or No question before a plain keys file is read. No is the default, and Escape means No.

    The buttons carry the object names keys-plain-import-confirm-yes and keys-plain-import-confirm-no. The box is returned
    unshown, so a test can read its default button without running it.
    """
    box = QMessageBox(parent)
    box.setObjectName("keys-plain-import-confirm")
    box.setIcon(QMessageBox.Icon.Question)
    box.setWindowTitle(title)
    box.setTextFormat(Qt.TextFormat.PlainText)
    box.setText(text)
    yes = box.addButton(YES_TEXT, QMessageBox.ButtonRole.YesRole)
    yes.setObjectName(YES_OBJECT_NAME)
    no = box.addButton(NO_TEXT, QMessageBox.ButtonRole.NoRole)
    no.setObjectName(NO_OBJECT_NAME)
    box.setDefaultButton(no)
    box.setEscapeButton(no)
    return box


def ask_plain_import(parent: QWidget, title: str, text: str) -> bool:
    """A modal Yes or No before a plain keys file is read. Only a click on Yes returns True. No is the default."""
    box = build_plain_question(parent, title, text)
    box.exec()
    answered_yes = box.clickedButton() is box.findChild(QPushButton, YES_OBJECT_NAME)
    box.deleteLater()
    return answered_yes


def _summary(result: ImportResult, environment: list[str] | None = None, notes: list[str] | None = None) -> str:
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
    if notes:
        lines.extend(notes)
    if environment:
        lines.extend(environment)
        lines.append(FILE_UNCHANGED_TEXT)
    else:
        lines.append(UNCHANGED_TEXT)
    return "\n".join(lines)


class KeyPlainImportDialog(QDialog):
    """Tick the names of one plain keys file and import them. ``imported`` carries how many keys were saved to the keychain."""

    imported = Signal(int)

    def __init__(
        self,
        path: Path,
        listing: FileNames,
        keys: ApiKeys,
        parent: QWidget | None = None,
        confirm_environment: Callable[[QWidget, str, str], bool] | None = None,
    ) -> None:
        super().__init__(parent)
        self._confirm_environment = confirm_environment
        self.setObjectName("keys-plain-import-dialog")
        self.setWindowTitle(TITLE_TEXT)
        self.setModal(True)
        self._path = path
        self._listing = listing
        self._keys = keys
        # The ticked-name boxes, by name in the file's order. They are replaced by each import.
        self._boxes: dict[str, QCheckBox] = {}
        self._name_widgets: list[QWidget] = []
        self._build()
        self._show_names()

    def _build(self) -> None:
        column = QVBoxLayout(self)
        column.addWidget(_label(self, "keys-plain-import-intro", INTRO_TEXT))
        file_label = _label(self, "keys-plain-import-file", FILE_TEXT.format(name=self._path.name))
        file_label.setToolTip(str(self._path))
        column.addWidget(file_label)

        # A file refused as a whole says why here, and then no name is listed. Otherwise the skipped entries are listed by place.
        self._problem = _label(self, "keys-plain-import-problem", self._listing.problem or "")
        self._problem.setVisible(self._listing.problem is not None)
        column.addWidget(self._problem)
        skipped = skipped_summary(self._listing) or ""
        self._skipped = _label(self, "keys-plain-import-skipped", skipped)
        self._skipped.setVisible(bool(skipped))
        column.addWidget(self._skipped)

        self._names = QVBoxLayout()
        self._names.setSpacing(4)
        column.addLayout(self._names)

        self._count = _label(self, "keys-plain-import-count")
        column.addWidget(self._count)

        self._replace = QCheckBox(REPLACE_TEXT, self)
        self._replace.setObjectName("keys-plain-import-replace")
        self._replace.setChecked(False)
        column.addWidget(self._replace)

        self._env = QCheckBox(ENV_TEXT, self)
        self._env.setObjectName("keys-plain-import-env")
        # Off by default: a Windows user variable is written only when the owner ticks this box.
        self._env.setChecked(False)
        column.addWidget(self._env)

        self._result = _label(self, "keys-plain-import-result")
        column.addWidget(self._result)

        buttons = QHBoxLayout()
        self._go = QPushButton(IMPORT_TEXT, self)
        self._go.setObjectName("keys-plain-import-go")
        self._close = QPushButton(CLOSE_TEXT, self)
        self._close.setObjectName("keys-plain-import-close")
        buttons.addStretch(1)
        buttons.addWidget(self._go)
        buttons.addWidget(self._close)
        column.addLayout(buttons)

        self._go.clicked.connect(self._on_import)
        self._close.clicked.connect(lambda *_: self.reject())

    # ----------------------------------------------------------- state on screen

    def _show_names(self, kept_ticks: set[str] | None = None) -> None:
        """One box for each listed name, and a note on each name that already has a saved value.

        Every box starts ticked, unless ``kept_ticks`` is given: then only the names in it are ticked, so an import
        leaves the owner's own ticks as they were.
        """
        self._clear_names()
        for name in self._listing.names:
            key = slug(name)
            box = QCheckBox(name, self)
            box.setObjectName(f"keys-plain-import-name-{key}")
            box.setChecked(True if kept_ticks is None else name in kept_ticks)
            box.toggled.connect(lambda *_: self._update_count())
            self._names.addWidget(box)
            self._boxes[name] = box
            self._name_widgets.append(box)
            if is_already_saved(self._keys, name):
                note = _label(self, f"keys-plain-import-note-{key}", NAME_NOTE_TEXT)
                self._names.addWidget(note)
                self._name_widgets.append(note)
        self._update_count()

    def _clear_names(self) -> None:
        for widget in self._name_widgets:
            self._names.removeWidget(widget)
            widget.hide()
            widget.setParent(None)
            widget.deleteLater()
        self._name_widgets = []
        self._boxes = {}

    def _update_count(self) -> None:
        """The count line and the Import button: Import is offered only while at least one name is ticked."""
        total = len(self._listing.names)
        ticked = sum(1 for box in self._boxes.values() if box.isChecked())
        self._count.setText(COUNT_TEXT.format(ticked=ticked, total=total) if total else "")
        self._count.setVisible(bool(total))
        self._go.setEnabled(ticked > 0)

    # ----------------------------------------------------------- actions

    def _set_environment(self, names: tuple[str, ...]) -> list[str]:
        """Set each imported name as a Windows user variable, after one confirmation. Returns the lines to show.

        Nothing here sees a value: ApiKeys.set_user_environment reads the saved value and writes it, and never returns it.
        """
        ask = self._confirm_environment if self._confirm_environment is not None else ask_yes_no
        if not ask(self, ENV_CONFIRM_TITLE, ENV_CONFIRM_TEXT.format(count=len(names), names=", ".join(names))):
            return [ENV_SKIPPED_TEXT]
        lines: list[str] = []
        written: list[str] = []
        for name in names:
            try:
                self._keys.set_user_environment(name)
            except KeyNameError as exc:
                lines.append(ENV_REFUSED_TEXT.format(name=name, reason=str(exc)))
            except SecretStoreError:
                lines.append(ENV_REFUSED_TEXT.format(name=name, reason=ENV_READ_FAILED_TEXT))
            except UserEnvironmentError:
                lines.append(ENV_REFUSED_TEXT.format(name=name, reason=ENV_WRITE_FAILED_TEXT))
            else:
                written.append(name)
        if written:
            lines.insert(0, ENV_SET_TEXT.format(names=", ".join(written)))
        return lines

    def _mark_critical(self, names: tuple[str, ...]) -> list[str]:
        """Mark each imported known-service or built-in name critical, as its file card showed it. Return a line for each
        mark that could not be saved. The mark is saved by name, so the keychain card keeps its badge after the import.
        """
        notes: list[str] = []
        for name in names:
            if not is_classified(name):
                continue
            try:
                self._keys.set_critical(name, True)
            except (KeyNameError, OSError):
                notes.append(CRITICAL_NOT_SAVED_TEXT.format(name=name))
        return notes

    @Slot()
    def _on_import(self) -> None:
        ticked = [name for name, box in self._boxes.items() if box.isChecked()]
        result = import_plain_checked(
            self._keys,
            self._path,
            self._listing.names,
            ticked,
            replace=self._replace.isChecked(),
        )
        notes = self._mark_critical(result.imported)
        environment = self._set_environment(result.imported) if self._env.isChecked() and result.imported else []
        self._result.setText(_summary(result, environment, notes))
        # Replace goes back to off after every import, so a second press cannot overwrite a value unless it is ticked again.
        self._replace.setChecked(False)
        # The owner's ticks stay as they were. Names are not ticked again.
        self._show_names(kept_ticks=set(ticked))
        if result.imported:
            self.imported.emit(len(result.imported))


__all__ = [
    "KeyPlainImportDialog",
    "PLAIN_QUESTION_TEXT",
    "PLAIN_QUESTION_TITLE",
    "ask_plain_import",
    "build_plain_question",
]
