"""The Critical vault block of the Keys section: create or unlock the password-encrypted vault, lock it, reveal each unlocked
critical key, and copy a critical keychain key into the vault. Qt code, so it sits in ui/.

The vault itself is meridian_desktop/key_vault.py. This block never keeps the password. It is read from the field, used once
and cleared. A value is shown only while the vault is open and the owner has pressed Reveal for that key. Lock hides every
value. A key copied into the vault keeps its keychain copy, so Check and the other places that read the keychain still work.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget

from meridian_desktop import key_vault
from meridian_desktop.api_keys import slug
from meridian_desktop.key_vault import Vault, VaultError
from meridian_desktop.ui.text import make_selectable

HEADING_TEXT = "Critical vault"
INTRO_TEXT = (
    "Critical keys can be kept in one file that a password encrypts. The vault starts locked. Reveal shows a value only "
    "while the vault is open, and Lock hides every value again. A forgotten password cannot be recovered, so those keys "
    "would have to be entered again from their source. Copying a key into the vault leaves its keychain copy in place."
)
NO_VAULT_TEXT = f"No vault yet. Choose a password of at least {key_vault.MIN_PASSWORD_LENGTH} characters, then create one."
LOCKED_TEXT = "Locked. Enter the password to unlock the vault."
OPEN_ONE_TEXT = "Unlocked. 1 key is in the vault."
OPEN_MANY_TEXT = "Unlocked. {count} keys are in the vault."
MISMATCH_TEXT = "The two passwords do not match. No vault was created."
PASSWORD_FIRST_TEXT = "Type the vault password first."
CREATED_TEXT = "Created the vault and unlocked it. Its file holds no plain text."
UNLOCKED_TEXT = "Unlocked the vault."
LOCKED_NOW_TEXT = "Locked. Every value is hidden."
NO_KEYCHAIN_TEXT = "No keychain value is saved for {name}, so there is nothing to copy."
COPIED_TEXT = "Copied {name} into the vault. Its keychain copy is unchanged."
IN_VAULT_TEXT = "In the vault."
NOT_IN_VAULT_TEXT = "Not in the vault yet."
KEYCHAIN_ONLY_TEXT = "Only in the keychain."
NONE_CRITICAL_TEXT = "No critical keys yet. Mark a key critical to list it here."
REVEAL_TEXT = "Reveal"
HIDE_TEXT = "Hide"
COPY_TEXT = "Copy into vault"
CREATE_TEXT = "Create vault"
UNLOCK_TEXT = "Unlock"
LOCK_TEXT = "Lock"
PASSWORD_PLACEHOLDER = "Vault password"
CONFIRM_PLACEHOLDER = "Type the password again"


def _label(parent: QWidget, name: str, text: str = "") -> QLabel:
    label = QLabel(text, parent)
    label.setObjectName(name)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return make_selectable(label)


def _password_field(parent: QWidget, name: str, placeholder: str) -> QLineEdit:
    field = QLineEdit(parent)
    field.setObjectName(name)
    field.setEchoMode(QLineEdit.EchoMode.Password)
    field.setPlaceholderText(placeholder)
    return field


class KeyVaultBox(QWidget):
    """The vault block. ``critical_names`` lists the critical keys. ``read_keychain`` gives a keychain value, or None.

    The block holds at most one open vault, which it closes on Lock. Each refresh shows the state, and the critical keys with
    what can be done for each one.
    """

    def __init__(
        self,
        path: Path,
        critical_names: Callable[[], list[str]],
        read_keychain: Callable[[str], str | None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("keyvault")
        self._path = path
        self._critical_names = critical_names
        self._read_keychain = read_keychain
        self._vault: Vault | None = None
        self._rows: list[QWidget] = []

        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(6)
        heading = _label(self, "keyvault-heading", HEADING_TEXT)
        font = heading.font()
        font.setBold(True)
        heading.setFont(font)
        column.addWidget(heading)
        column.addWidget(_label(self, "keyvault-intro", INTRO_TEXT))
        self._status = _label(self, "keyvault-status")
        column.addWidget(self._status)

        controls = QHBoxLayout()
        self._password = _password_field(self, "keyvault-password", PASSWORD_PLACEHOLDER)
        self._confirm = _password_field(self, "keyvault-password-confirm", CONFIRM_PLACEHOLDER)
        self._create = QPushButton(CREATE_TEXT, self)
        self._create.setObjectName("keyvault-create")
        self._unlock = QPushButton(UNLOCK_TEXT, self)
        self._unlock.setObjectName("keyvault-unlock")
        self._lock = QPushButton(LOCK_TEXT, self)
        self._lock.setObjectName("keyvault-lock")
        controls.addWidget(self._password, 1)
        controls.addWidget(self._confirm, 1)
        controls.addWidget(self._create)
        controls.addWidget(self._unlock)
        controls.addWidget(self._lock)
        column.addLayout(controls)

        self._list = QVBoxLayout()
        self._list.setSpacing(4)
        column.addLayout(self._list)
        self._none = _label(self, "keyvault-none", NONE_CRITICAL_TEXT)
        column.addWidget(self._none)

        self._create.clicked.connect(self._on_create)
        self._unlock.clicked.connect(self._on_unlock)
        self._lock.clicked.connect(self._on_lock)
        self._password.returnPressed.connect(self._on_unlock)
        self.refresh()

    # ----------------------------------------------------------- state on screen

    def _is_open(self) -> bool:
        return self._vault is not None and self._vault.is_open

    def refresh(self, message: str | None = None) -> None:
        """Show the state: no vault, locked, or open, and each critical key with what can be done for it.

        ``message`` replaces the state line for one action. Without it the state line says which state the vault is in.
        """
        has_vault = key_vault.exists(self._path)
        is_open = self._is_open()
        self._password.setVisible(not is_open)
        self._confirm.setVisible(not has_vault and not is_open)
        self._create.setVisible(not has_vault and not is_open)
        self._unlock.setVisible(has_vault and not is_open)
        self._lock.setVisible(is_open)
        if message is None:
            if is_open:
                count = len(self._vault.names()) if self._vault is not None else 0
                message = OPEN_ONE_TEXT if count == 1 else OPEN_MANY_TEXT.format(count=count)
            else:
                message = LOCKED_TEXT if has_vault else NO_VAULT_TEXT
        self._status.setText(message)
        self._rebuild_rows()

    def _rebuild_rows(self) -> None:
        for row in self._rows:
            row.hide()
            row.setParent(None)
            row.deleteLater()
        self._rows = []
        while self._list.count():
            self._list.takeAt(0)

        is_open = self._is_open()
        in_vault = set(self._vault.names()) if is_open and self._vault is not None else set()
        names = sorted(set(self._critical_names()) | in_vault)
        self._none.setVisible(not names)
        for name in names:
            self._add_row(name, is_open, name in in_vault)

    def _add_row(self, name: str, is_open: bool, in_vault: bool) -> None:
        key = slug(name)
        frame = QFrame(self)
        frame.setObjectName(f"keyvault-row-{key}")
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        row = QVBoxLayout(frame)
        head = QHBoxLayout()
        title = _label(frame, f"keyvault-name-{key}", name)
        font = title.font()
        font.setBold(True)
        title.setFont(font)
        head.addWidget(title)
        if is_open:
            state = IN_VAULT_TEXT if in_vault else NOT_IN_VAULT_TEXT
        else:
            state = LOCKED_TEXT
        head.addWidget(_label(frame, f"keyvault-state-{key}", state), 1)
        row.addLayout(head)

        if is_open and in_vault:
            value = QLineEdit(frame)
            value.setObjectName(f"keyvault-value-{key}")
            value.setReadOnly(True)
            value.setVisible(False)
            reveal = QPushButton(REVEAL_TEXT, frame)
            reveal.setObjectName(f"keyvault-reveal-{key}")
            reveal.clicked.connect(lambda *_, n=name, v=value, b=reveal: self._toggle_reveal(n, v, b))
            head.addWidget(reveal)
            row.addWidget(value)
        elif is_open:
            copy = QPushButton(COPY_TEXT, frame)
            copy.setObjectName(f"keyvault-copy-{key}")
            copy.clicked.connect(lambda *_, n=name: self._on_copy(n))
            head.addWidget(copy)

        self._list.addWidget(frame)
        self._rows.append(frame)

    # ----------------------------------------------------------- actions

    def _clear_passwords(self) -> None:
        self._password.clear()
        self._confirm.clear()

    def _on_create(self) -> None:
        password = self._password.text()
        confirm = self._confirm.text()
        self._clear_passwords()
        if password != confirm:
            self.refresh(MISMATCH_TEXT)
            return
        try:
            self._vault = key_vault.create(self._path, password)
        except VaultError as exc:
            self._vault = None
            self.refresh(str(exc))
            return
        self.refresh(CREATED_TEXT)

    def _on_unlock(self) -> None:
        password = self._password.text()
        self._password.clear()
        if not password:
            self.refresh(PASSWORD_FIRST_TEXT)
            return
        try:
            self._vault = key_vault.unlock(self._path, password)
        except VaultError as exc:
            self._vault = None
            self.refresh(str(exc))
            return
        self.refresh(UNLOCKED_TEXT)

    def _on_lock(self) -> None:
        if self._vault is not None:
            self._vault.lock()
        self._vault = None
        self.refresh(LOCKED_NOW_TEXT)

    def _toggle_reveal(self, name: str, value: QLineEdit, button: QPushButton) -> None:
        """Show the value of one key, or hide it again. The text is set only while it is shown, and cleared when it is hidden."""
        if not value.isHidden():
            value.clear()
            value.setVisible(False)
            button.setText(REVEAL_TEXT)
            return
        if not self._is_open() or self._vault is None:
            self.refresh(LOCKED_TEXT)
            return
        text = self._vault.get(name)
        if text is None:
            self.refresh(NOT_IN_VAULT_TEXT)
            return
        value.setText(text)
        value.setVisible(True)
        button.setText(HIDE_TEXT)

    def _on_copy(self, name: str) -> None:
        if not self._is_open() or self._vault is None:
            self.refresh(LOCKED_TEXT)
            return
        try:
            value = self._read_keychain(name)
        except Exception:  # noqa: BLE001 - a keychain that cannot be read is shown as no value, never as a crash
            value = None
        if not value:
            self.refresh(NO_KEYCHAIN_TEXT.format(name=name))
            return
        self._vault.put(name, value)
        try:
            self._vault.save()
        except VaultError as exc:
            self._vault.remove(name)
            self.refresh(str(exc))
            return
        self.refresh(COPIED_TEXT.format(name=name))


__all__ = ["KeyVaultBox"]
