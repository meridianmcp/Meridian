"""Tests for the critical marks and the Windows user variable on the Keys block (pytest-qt, offscreen Qt from conftest).

The keychain is a MemoryKeyring, the Windows user environment is a FakeUserRegistry, and every question is an injected
fake, so nothing touches the real credential store, HKCU, the network or a dialog. FAKE_VALUE is an obviously fake value,
so the leak checks can search for it.
"""

from __future__ import annotations

import json

from PySide6.QtWidgets import QCheckBox, QFrame, QLabel, QLineEdit, QPushButton, QWidget

from _fakes import FakeUserRegistry, MemoryKeyring
from meridian_desktop.api_keys import KEY_SERVICE, ApiKeys
from meridian_desktop.secret_store import KeyringSecretStore
from meridian_desktop.settings_store import Settings, SettingsStore
from meridian_desktop.ui.keys_section import (
    CRITICAL_TEXT,
    NOT_WRITTEN_TEXT,
    UNCRITICAL_TEXT,
    USER_ENV_CANCELLED_TEXT,
    USER_ENV_NO_VALUE_TEXT,
    USER_ENV_SET_TEXT,
    USER_ENV_TITLE,
    USER_ENV_UNANNOUNCED_TEXT,
    KeysSection,
)
from meridian_desktop.user_environment import UserEnvironment

FAKE_VALUE = "sk-fake-keys-critical-ui-0001"


class Asked:
    """An injected Yes or No. It records every question, and answers with ``answer``."""

    def __init__(self, answer: bool) -> None:
        self.answer = answer
        self.calls: list[tuple[str, str]] = []

    def __call__(self, parent, title, text) -> bool:
        self.calls.append((title, text))
        return self.answer


def _section(qtbot, settings_path, backend, registry, *, confirm=None, runpod=False, names=("OpenAI", "Anthropic")):
    SettingsStore(settings_path).save(Settings(key_names=tuple(names)))
    keys = ApiKeys(SettingsStore(settings_path), KeyringSecretStore(backend=backend), user_environment=UserEnvironment(registry))
    runpod_card = QWidget() if runpod else None
    section = KeysSection(keys, runpod_card, confirm=confirm)
    qtbot.addWidget(section)
    qtbot.waitUntil(lambda: not section.loading, timeout=5000)
    return section


def _give(backend, account: str, value: str = FAKE_VALUE) -> None:
    backend.set_password(KEY_SERVICE, account, value)


def _order(section) -> list[str]:
    layout = section._entry_layout
    widgets = [layout.itemAt(index).widget() for index in range(layout.count())]
    return [widget.objectName() for widget in widgets if widget is not None]


def _box(section, key: str) -> QCheckBox:
    return section.findChild(QCheckBox, f"keys-entry-{key}-critical")


def _badge(section, key: str) -> QLabel:
    return section.findChild(QLabel, f"keys-entry-{key}-badge")


def _user_env_button(section, key: str) -> QPushButton:
    return section.findChild(QPushButton, f"keys-entry-{key}-user-env")


def _all_text(widget) -> str:
    """Every label, button and placeholder on the widget. The typed values in the password boxes are not included."""
    parts = [label.text() for label in widget.findChildren(QLabel)]
    parts += [button.text() for button in widget.findChildren(QPushButton)]
    parts += [box.text() for box in widget.findChildren(QCheckBox)]
    parts += [line.placeholderText() for line in widget.findChildren(QLineEdit)]
    return "\n".join(parts)


# ----------------------------------------------------------- the critical mark


def test_marking_a_key_critical_shows_the_badge_and_saves_the_mark_in_settings(qtbot, settings_path):
    backend = MemoryKeyring()
    registry = FakeUserRegistry()
    _give(backend, "key-openai")
    section = _section(qtbot, settings_path, backend, registry)
    assert not _badge(section, "openai").isVisibleTo(section)

    _box(section, "openai").setChecked(True)

    assert _badge(section, "openai").isVisibleTo(section)
    assert _box(section, "openai").isChecked()
    assert json.loads(settings_path.read_text(encoding="utf-8"))["critical_keys"] == ["OpenAI"]
    assert FAKE_VALUE.encode() not in settings_path.read_bytes(), "the mark is saved with the name, never the value"
    assert section._message.text() == CRITICAL_TEXT.format(name="OpenAI")


def test_a_critical_key_is_listed_first_and_the_rest_keep_their_order(qtbot, settings_path):
    backend = MemoryKeyring()
    section = _section(qtbot, settings_path, backend, FakeUserRegistry(), names=("OpenAI", "Anthropic", "Mistral"))
    assert _order(section) == ["keys-entry-openai", "keys-entry-anthropic", "keys-entry-mistral"]

    _box(section, "mistral").setChecked(True)

    assert _order(section) == ["keys-entry-mistral", "keys-entry-openai", "keys-entry-anthropic"]


def test_runpod_moves_to_the_top_of_the_critical_group_when_it_is_marked(qtbot, settings_path):
    backend = MemoryKeyring()
    section = _section(qtbot, settings_path, backend, FakeUserRegistry(), runpod=True)
    assert _order(section) == ["keys-entry-runpod", "keys-entry-openai", "keys-entry-anthropic"]

    _box(section, "anthropic").setChecked(True)

    assert _order(section) == ["keys-entry-anthropic", "keys-entry-runpod", "keys-entry-openai"]


def test_clearing_the_mark_hides_the_badge_and_puts_the_name_back(qtbot, settings_path):
    backend = MemoryKeyring()
    section = _section(qtbot, settings_path, backend, FakeUserRegistry())
    _box(section, "anthropic").setChecked(True)

    _box(section, "anthropic").setChecked(False)

    assert not _badge(section, "anthropic").isVisibleTo(section)
    assert _order(section) == ["keys-entry-openai", "keys-entry-anthropic"]
    assert section._message.text() == UNCRITICAL_TEXT.format(name="Anthropic")


def test_a_settings_write_that_fails_puts_the_box_back_and_says_so(qtbot, settings_path, monkeypatch):
    backend = MemoryKeyring()
    section = _section(qtbot, settings_path, backend, FakeUserRegistry())

    def refuse(self, settings):
        raise OSError("disk full")

    monkeypatch.setattr(SettingsStore, "save", refuse)
    _box(section, "openai").setChecked(True)

    assert not _box(section, "openai").isChecked(), "the box shows the mark the file still holds"
    assert not _badge(section, "openai").isVisibleTo(section)
    assert section._message.text() == NOT_WRITTEN_TEXT
    assert "critical_keys" not in settings_path.read_text(encoding="utf-8")


def test_the_search_still_filters_when_a_critical_key_is_listed_first(qtbot, settings_path):
    backend = MemoryKeyring()
    section = _section(qtbot, settings_path, backend, FakeUserRegistry())
    _box(section, "anthropic").setChecked(True)

    section._search.setText("open")

    assert section.findChild(QFrame, "keys-entry-openai").isVisibleTo(section)
    assert not section.findChild(QFrame, "keys-entry-anthropic").isVisibleTo(section)


def test_removing_a_critical_key_drops_its_mark_from_settings(qtbot, settings_path):
    backend = MemoryKeyring()
    _give(backend, "key-openai")
    asked = Asked(True)
    section = _section(qtbot, settings_path, backend, FakeUserRegistry(), confirm=asked)
    _box(section, "openai").setChecked(True)

    section.findChild(QPushButton, "keys-entry-openai-remove").click()

    assert len(asked.calls) == 1
    assert "critical_keys" not in settings_path.read_text(encoding="utf-8")
    assert "OpenAI" not in section._cards


# ----------------------------------------------------------- the Windows user variable


def test_the_user_variable_is_asked_first_and_declining_writes_nothing(qtbot, settings_path):
    backend = MemoryKeyring()
    _give(backend, "key-openai")
    registry = FakeUserRegistry()
    asked = Asked(False)
    section = _section(qtbot, settings_path, backend, registry, confirm=asked)

    _user_env_button(section, "openai").click()

    assert len(asked.calls) == 1
    title, text = asked.calls[0]
    assert title == USER_ENV_TITLE
    assert "OPENAI" in text, "the question names the variable"
    assert FAKE_VALUE not in text
    assert registry.writes == []
    assert section._message.text() == USER_ENV_CANCELLED_TEXT


def test_confirming_writes_the_saved_value_and_no_value_is_ever_shown(qtbot, settings_path):
    backend = MemoryKeyring()
    _give(backend, "key-openai")
    registry = FakeUserRegistry()
    asked = Asked(True)
    section = _section(qtbot, settings_path, backend, registry, confirm=asked)

    _user_env_button(section, "openai").click()

    assert registry.writes == [("OPENAI", FAKE_VALUE)]
    assert section._message.text() == USER_ENV_SET_TEXT.format(variable="OPENAI")
    assert FAKE_VALUE not in section._message.text()
    assert FAKE_VALUE not in _all_text(section)
    assert FAKE_VALUE.encode() not in settings_path.read_bytes()


def test_an_unconfirmed_broadcast_tells_the_owner_to_sign_out_and_back_in(qtbot, settings_path):
    backend = MemoryKeyring()
    _give(backend, "key-openai")
    registry = FakeUserRegistry(announced=False)
    section = _section(qtbot, settings_path, backend, registry, confirm=Asked(True))

    _user_env_button(section, "openai").click()

    assert registry.writes == [("OPENAI", FAKE_VALUE)]
    assert USER_ENV_UNANNOUNCED_TEXT in section._message.text()


def test_a_key_with_no_saved_value_is_not_offered_the_question(qtbot, settings_path):
    backend = MemoryKeyring()
    registry = FakeUserRegistry()
    asked = Asked(True)
    section = _section(qtbot, settings_path, backend, registry, confirm=asked)

    _user_env_button(section, "anthropic").click()

    assert asked.calls == []
    assert registry.writes == []
    assert section._message.text() == USER_ENV_NO_VALUE_TEXT.format(name="Anthropic")


def test_a_name_windows_cannot_take_is_refused_before_the_question(qtbot, settings_path):
    backend = MemoryKeyring()
    _give(backend, "key-path", FAKE_VALUE)
    registry = FakeUserRegistry()
    asked = Asked(True)
    section = _section(qtbot, settings_path, backend, registry, confirm=asked, names=("Path",))

    _user_env_button(section, "path").click()

    assert asked.calls == []
    assert registry.writes == []
    assert "Windows variable" in section._message.text()
    assert FAKE_VALUE not in section._message.text()


def test_runpod_is_set_as_runpod_api_key_after_the_question(qtbot, settings_path):
    backend = MemoryKeyring()
    _give(backend, "runpod-api-key")
    registry = FakeUserRegistry()
    asked = Asked(True)
    section = _section(qtbot, settings_path, backend, registry, confirm=asked, runpod=True)

    _user_env_button(section, "runpod").click()

    assert "RUNPOD_API_KEY" in asked.calls[0][1]
    assert registry.writes == [("RUNPOD_API_KEY", FAKE_VALUE)]
