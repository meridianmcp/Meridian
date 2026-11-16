"""First-run dialog: connection mode, local address and start-with-Windows.

Shown once, before the main window, when the settings file does not exist yet. ui/app.py asks
needs_first_run() and calls run_first_run(). It is never shown under --smoke, and callers that
pass first_run=False never see it. Closing the dialog without Save keeps the defaults, so the
question is asked once, not on every start.

The helpers that decide what gets saved (validate_local_url, settings_from_choices,
save_first_run) need no window, so the tests check them directly.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, replace
from urllib.parse import urlsplit

from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from meridian_desktop import install as inst
from meridian_desktop.settings_store import DEFAULT_HOSTED_URL, DEFAULT_LOCAL_URL, VALID_MODES, Settings, SettingsStore
from meridian_desktop.ui.text import make_selectable

_ADDRESS_HINT = "for example http://127.0.0.1:7878"
_PORT_PROBLEM = f"The local address needs a port from 1 to 65535, {_ADDRESS_HINT}."


@dataclass(frozen=True)
class FirstRunOutcome:
    """What was saved, and a notice in plain words when part of the choice could not be applied."""

    settings: Settings
    notice: str | None = None


def needs_first_run(store: SettingsStore, *, smoke: bool) -> bool:
    """True for a real start with no settings file yet. Never under --smoke."""
    return not smoke and not store.path.exists()


def validate_local_url(text: str) -> str | None:
    """None when ``text`` is an http or https address with a host and a usable port. Otherwise, what is wrong."""
    value = text.strip()
    if not value:
        return f"Enter the local address, {_ADDRESS_HINT}."
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        return _PORT_PROBLEM
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return f"The local address must start with http:// or https:// and name a host, {_ADDRESS_HINT}."
    if port is not None and not 1 <= port <= 65535:
        return _PORT_PROBLEM
    return None


def settings_from_choices(current: Settings, *, mode: str, local_url: str, autostart: bool) -> Settings:
    """``current`` with the three answers applied. Every other setting is kept as it is."""
    if mode not in VALID_MODES:
        raise ValueError(f"mode must be one of {VALID_MODES}, got {mode!r}")
    # The owner answered the question, so the mode is explicit and a later launch keeps it.
    return replace(current, mode=mode, mode_explicit=True, local_url=local_url.strip(), autostart=autostart)


def save_first_run(
    store: SettingsStore,
    chosen: Settings,
    *,
    env: Mapping[str, str] | None = None,
    registry: inst.RegistryBackend | None = None,
) -> FirstRunOutcome:
    """Save the answers, then add or remove the start-with-Windows entry. Never raises for a failed step."""
    environment = os.environ if env is None else env
    try:
        store.save(chosen)
    except OSError as exc:
        return FirstRunOutcome(
            chosen,
            f"Meridian Desktop could not save your choices ({exc.strerror or 'unknown error'}). "
            "It will use the defaults and ask again next time.",
        )
    try:
        inst.set_autostart(chosen.autostart, environment, registry=registry)
    except inst.InstallError as exc:
        return FirstRunOutcome(chosen, str(exc))
    except OSError as exc:
        return FirstRunOutcome(
            chosen,
            f"Start with Windows could not be set ({exc.strerror or 'unknown error'}). Your other choices are saved.",
        )
    return FirstRunOutcome(chosen)


class FirstRunDialog(QDialog):
    """Asks the connection mode, the local address and start-with-Windows. ``saved`` is True only after a valid Save."""

    def __init__(self, current: Settings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._current = current
        self.saved = False
        self.setWindowTitle("Welcome to Meridian Desktop")
        self.setModal(True)
        self.setMinimumWidth(480)

        intro = make_selectable(QLabel("Choose how Meridian Desktop connects. You can change this later in Settings."))
        intro.setWordWrap(True)

        self.local_button = QRadioButton("Local: Meridian runs on this computer (recommended)")
        self.hosted_button = QRadioButton(f"Hosted: connect to {DEFAULT_HOSTED_URL}")
        self._mode_group = QButtonGroup(self)
        self._mode_group.addButton(self.local_button)
        self._mode_group.addButton(self.hosted_button)
        if current.mode == "hosted":
            self.hosted_button.setChecked(True)
        else:
            self.local_button.setChecked(True)
        # Both buttons are connected, so the field follows the choice whichever one changes last.
        self.local_button.toggled.connect(self._sync_address_field)
        self.hosted_button.toggled.connect(self._sync_address_field)

        address_label = make_selectable(QLabel("Local address (used when Local is selected):"))
        self.address_field = QLineEdit(current.local_url)
        self.address_field.setPlaceholderText(DEFAULT_LOCAL_URL)
        self.address_field.setEnabled(current.mode != "hosted")
        address_hint = make_selectable(QLabel("Leave it as it is unless Meridian listens on another address."))
        address_hint.setWordWrap(True)

        self.error_label = make_selectable(QLabel())
        self.error_label.setWordWrap(True)
        self.error_label.setVisible(False)

        self.autostart_box = QCheckBox("Start Meridian Desktop when I sign in to Windows")
        self.autostart_box.setChecked(current.autostart)
        autostart_hint = make_selectable(QLabel("Off by default. Turn it on only if you want Meridian Desktop running after sign-in."))
        autostart_hint.setWordWrap(True)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        for widget in (
            intro,
            self.local_button,
            self.hosted_button,
            address_label,
            self.address_field,
            address_hint,
            self.error_label,
            self.autostart_box,
            autostart_hint,
            buttons,
        ):
            layout.addWidget(widget)

    def _sync_address_field(self, *_args: object) -> None:
        self.address_field.setEnabled(self.local_button.isChecked())

    def settings(self) -> Settings:
        """The settings the dialog would save now. Hosted keeps the saved local address unchanged."""
        if self.hosted_button.isChecked():
            return settings_from_choices(
                self._current,
                mode="hosted",
                local_url=self._current.local_url,
                autostart=self.autostart_box.isChecked(),
            )
        return settings_from_choices(
            self._current,
            mode="local",
            local_url=self.address_field.text(),
            autostart=self.autostart_box.isChecked(),
        )

    def accept(self) -> None:
        if self.local_button.isChecked():
            problem = validate_local_url(self.address_field.text())
            if problem is not None:
                self.error_label.setText(problem)
                self.error_label.setVisible(True)
                self.address_field.setFocus()
                return
        self.saved = True
        super().accept()


def run_first_run(
    store: SettingsStore,
    *,
    env: Mapping[str, str] | None = None,
    registry: inst.RegistryBackend | None = None,
    parent: QWidget | None = None,
) -> FirstRunOutcome:
    """Show the dialog, then save and apply the answers. Closing without Save keeps the defaults, with autostart off."""
    current = store.load().settings
    dialog = FirstRunDialog(current, parent)
    dialog.exec()
    chosen = dialog.settings() if dialog.saved else replace(current, autostart=False)
    return save_first_run(store, chosen, env=env, registry=registry)
