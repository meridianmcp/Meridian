"""Tests for the Connections block on the Settings tab (meridian_desktop/ui/connections_section.py), with pytest-qt.

The settings file is in pytest's temp dir, every program lookup is a fake, and every removal question is an injected fake, so no
dialog opens, nothing on this computer is read or started, and no URL is opened. Offscreen Qt, from conftest.
"""

from __future__ import annotations

import json

from PySide6.QtWidgets import QCheckBox, QComboBox, QLabel, QLineEdit, QPushButton, QWidget

from meridian_desktop.settings_store import SettingsStore
from meridian_desktop.ui.connections_section import (
    ADDED_TEXT,
    CANCELLED_TEXT,
    OPT_IN_OFF_TEXT,
    OPT_IN_ON_TEXT,
    REMOVE_BODY,
    REMOVE_TITLE,
    REMOVED_TEXT,
    ConnectionsSection,
)

FOUND = {"notes-cli": "C:/tools/notes-cli.exe"}


def finder(found: dict[str, str]):
    def resolve(name, fallbacks=()):
        return found.get(name)

    return resolve


class Asker:
    """A confirmation that records what it was asked and answers with a fixed value."""

    def __init__(self, answer: bool) -> None:
        self.answer = answer
        self.asked: list[tuple[str, str]] = []

    def __call__(self, parent, title, text) -> bool:
        self.asked.append((title, text))
        return self.answer


def _section(qtbot, path, *, answer=True, found=FOUND) -> tuple[ConnectionsSection, SettingsStore, Asker]:
    store = SettingsStore(path)
    asker = Asker(answer)
    section = ConnectionsSection(store, resolve=finder(found), confirm=asker)
    qtbot.addWidget(section)
    return section, store, asker


def _widget(section, kind, name):
    widget = section.findChild(kind, name)
    assert widget is not None, name
    return widget


def _fill_stdio(section, *, name="notes", command="notes-cli", args="--serve") -> None:
    _widget(section, QLineEdit, "connections-new-name").setText(name)
    _widget(section, QLineEdit, "connections-new-command").setText(command)
    _widget(section, QLineEdit, "connections-new-args").setText(args)


def _message(section) -> str:
    return _widget(section, QLabel, "connections-message").text()


def _saved(store) -> tuple:
    return store.load().settings.custom_connectors


# ------------------------------------------------------------------ showing the list

def test_an_empty_list_says_so_and_a_new_connector_form_is_shown(qtbot, tmp_path):
    section, _, _ = _section(qtbot, tmp_path / "settings.json")
    assert not _widget(section, QLabel, "connections-empty").isHidden()
    assert _widget(section, QLineEdit, "connections-new-name") is not None


def test_the_tunnel_opt_in_is_off_by_default(qtbot, tmp_path):
    section, _, _ = _section(qtbot, tmp_path / "settings.json")
    assert _widget(section, QCheckBox, "connections-tunnel-opt-in").isChecked() is False


def test_switching_the_transport_shows_the_url_field_and_hides_the_command_fields(qtbot, tmp_path):
    section, _, _ = _section(qtbot, tmp_path / "settings.json")
    transport = _widget(section, QComboBox, "connections-new-transport")
    transport.setCurrentIndex(transport.findData("http"))
    assert _widget(section, QLineEdit, "connections-new-command").parentWidget().isHidden()
    assert not _widget(section, QLineEdit, "connections-new-url").parentWidget().isHidden()


# ------------------------------------------------------------------ add

def test_adding_a_command_saves_it_and_lists_it(qtbot, tmp_path):
    section, store, _ = _section(qtbot, tmp_path / "settings.json")
    _fill_stdio(section)
    _widget(section, QPushButton, "connections-add").click()
    saved = _saved(store)
    assert saved == (
        {"name": "notes", "kind": "copy", "transport": "stdio", "enabled": True, "tunnel_exposed": False,
         "command": "notes-cli", "args": ["--serve"]},
    )
    assert _message(section) == ADDED_TEXT.format(name="notes")
    assert _widget(section, QWidget, "connections-row-0") is not None
    assert _widget(section, QLabel, "connections-row-0-name").text() == "notes"
    assert _widget(section, QLineEdit, "connections-new-name").text() == ""


def test_an_unsafe_name_is_refused_with_a_message_and_nothing_is_saved(qtbot, tmp_path):
    section, store, _ = _section(qtbot, tmp_path / "settings.json")
    _fill_stdio(section, name="bad name;rm")
    _widget(section, QPushButton, "connections-add").click()
    assert "letter" in _message(section)
    assert _saved(store) == ()


def test_a_command_with_a_shell_character_is_refused_and_nothing_is_saved(qtbot, tmp_path):
    section, store, _ = _section(qtbot, tmp_path / "settings.json")
    _fill_stdio(section, command="notes-cli;calc")
    _widget(section, QPushButton, "connections-add").click()
    assert "shell" in _message(section)
    assert _saved(store) == ()


def test_a_plain_http_url_is_refused_and_an_https_url_is_saved(qtbot, tmp_path):
    section, store, _ = _section(qtbot, tmp_path / "settings.json")
    transport = _widget(section, QComboBox, "connections-new-transport")
    transport.setCurrentIndex(transport.findData("http"))
    _widget(section, QLineEdit, "connections-new-name").setText("remote")
    _widget(section, QLineEdit, "connections-new-url").setText("http://example.com/mcp")
    _widget(section, QPushButton, "connections-add").click()
    assert "https://" in _message(section)
    assert _saved(store) == ()

    _widget(section, QLineEdit, "connections-new-url").setText("https://example.com/mcp")
    _widget(section, QPushButton, "connections-add").click()
    assert _saved(store)[0]["url"] == "https://example.com/mcp"


def test_a_duplicate_name_is_refused(qtbot, tmp_path):
    section, store, _ = _section(qtbot, tmp_path / "settings.json")
    _fill_stdio(section)
    _widget(section, QPushButton, "connections-add").click()
    _fill_stdio(section, name="NOTES")
    _widget(section, QPushButton, "connections-add").click()
    assert "already exists" in _message(section)
    assert len(_saved(store)) == 1


def test_a_linked_command_that_is_not_on_this_computer_is_refused(qtbot, tmp_path):
    section, store, _ = _section(qtbot, tmp_path / "settings.json", found={})
    kind = _widget(section, QComboBox, "connections-new-kind")
    kind.setCurrentIndex(kind.findData("linked"))
    _fill_stdio(section, command="notes-cli")
    _widget(section, QPushButton, "connections-add").click()
    assert "was not found on this computer" in _message(section)
    assert _saved(store) == ()


def test_adding_is_refused_while_the_settings_file_has_a_problem_and_the_file_is_left_alone(qtbot, tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{not json", encoding="utf-8")
    section, _, _ = _section(qtbot, path)
    _fill_stdio(section)
    _widget(section, QPushButton, "connections-add").click()
    assert "has a problem" in _message(section)
    assert path.read_text(encoding="utf-8") == "{not json"


# ------------------------------------------------------------------ switches

def test_switching_a_connector_off_saves_it_and_on_again_restores_it(qtbot, tmp_path):
    section, store, _ = _section(qtbot, tmp_path / "settings.json")
    _fill_stdio(section)
    _widget(section, QPushButton, "connections-add").click()

    _widget(section, QCheckBox, "connections-row-0-enabled").setChecked(False)
    assert _saved(store)[0]["enabled"] is False
    assert "Off." in _widget(section, QLabel, "connections-row-0-status").text()

    _widget(section, QCheckBox, "connections-row-0-enabled").setChecked(True)
    assert _saved(store)[0]["enabled"] is True


def test_the_tunnel_flag_saves_and_its_status_says_the_opt_in_is_off(qtbot, tmp_path):
    section, store, _ = _section(qtbot, tmp_path / "settings.json")
    _fill_stdio(section)
    _widget(section, QPushButton, "connections-add").click()

    _widget(section, QCheckBox, "connections-row-0-tunnel").setChecked(True)
    assert _saved(store)[0]["tunnel_exposed"] is True
    assert "Tunnel: off, because the tunnel opt-in is off." in _widget(section, QLabel, "connections-row-0-status").text()


def test_the_tunnel_opt_in_saves_and_shows_its_plain_status(qtbot, tmp_path):
    section, store, _ = _section(qtbot, tmp_path / "settings.json")
    opt_in = _widget(section, QCheckBox, "connections-tunnel-opt-in")
    opt_in.setChecked(True)
    assert store.load().settings.custom_tunnel_opt_in is True
    assert _message(section) == OPT_IN_ON_TEXT
    opt_in.setChecked(False)
    assert store.load().settings.custom_tunnel_opt_in is False
    assert _message(section) == OPT_IN_OFF_TEXT


# ------------------------------------------------------------------ remove

def test_remove_asks_first_and_a_no_keeps_the_connector(qtbot, tmp_path):
    section, store, asker = _section(qtbot, tmp_path / "settings.json", answer=False)
    _fill_stdio(section)
    _widget(section, QPushButton, "connections-add").click()

    _widget(section, QPushButton, "connections-row-0-remove").click()
    assert len(asker.asked) == 1
    title, text = asker.asked[0]
    assert title == REMOVE_TITLE
    assert text == REMOVE_BODY.format(name="notes")
    assert len(_saved(store)) == 1
    assert _message(section) == CANCELLED_TEXT


def test_remove_on_yes_takes_the_connector_out_of_the_saved_list(qtbot, tmp_path):
    section, store, asker = _section(qtbot, tmp_path / "settings.json", answer=True)
    _fill_stdio(section)
    _widget(section, QPushButton, "connections-add").click()

    _widget(section, QPushButton, "connections-row-0-remove").click()
    assert len(asker.asked) == 1
    assert _saved(store) == ()
    assert _message(section) == REMOVED_TEXT.format(name="notes")
    assert section.findChild(QWidget, "connections-row-0") is None


# ------------------------------------------------------------------ saved entries that are not usable

def test_a_saved_entry_that_is_not_usable_shows_why_and_can_only_be_removed(qtbot, tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps({"custom_connectors": [{"name": "x;y", "kind": "copy", "transport": "stdio", "command": "a", "args": []}]}),
        encoding="utf-8",
    )
    section, store, _ = _section(qtbot, path)
    assert _widget(section, QLabel, "connections-row-0-status").text().startswith("Not used:")
    assert _widget(section, QCheckBox, "connections-row-0-enabled").isEnabled() is False
    assert _widget(section, QCheckBox, "connections-row-0-tunnel").isEnabled() is False
    _widget(section, QPushButton, "connections-row-0-remove").click()
    assert _saved(store) == ()
