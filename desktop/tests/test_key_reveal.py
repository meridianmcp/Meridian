"""Reveal, Hide value and Copy value on a keychain card, the clipboard clear after 30 seconds, and ApiKeys.read_value.

A fake keychain (tests/_fakes.py MemoryKeyring) holds the values, a temp folder holds the settings, and a fake clipboard
stands in for the system clipboard, so nothing real is touched. Every value here is fake. The timers are driven by calling
their handlers directly, or by emitting their timeout, so no test waits 30 seconds.
"""

import logging

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QLabel, QLineEdit, QPushButton

from _fakes import MemoryKeyring
from meridian_desktop.api_keys import ApiKeys, KeyNameError
from meridian_desktop.secret_store import KeyringSecretStore
from meridian_desktop.settings_store import SettingsStore
from meridian_desktop.ui import keys_section
from meridian_desktop.ui.keys_section import KeysSection

VALUE = "sk-fake-reveal-ALPHA-0001"
OTHER = "sk-fake-reveal-BRAVO-0002"


class FakeClipboard:
    """Stands in for the system clipboard. It keeps the text in memory and nothing else."""

    def __init__(self):
        self._text = ""

    def setText(self, text):
        self._text = text

    def text(self):
        return self._text

    def clear(self):
        self._text = ""


class FlakyKeyring(MemoryKeyring):
    """MemoryKeyring whose reads fail once ``fail_reads`` is set. The read error holds a fake value on purpose."""

    fail_reads = False

    def get_password(self, service, username):
        if self.fail_reads:
            raise RuntimeError(f"read refused while holding {VALUE}")
        return super().get_password(service, username)


@pytest.fixture
def folder(tmp_path):
    return tmp_path


@pytest.fixture
def clipboard(monkeypatch):
    fake = FakeClipboard()
    monkeypatch.setattr(keys_section, "_system_clipboard", lambda: fake)
    return fake


def _keys(folder, backend=None):
    backend = backend if backend is not None else MemoryKeyring()
    settings = SettingsStore(folder / "settings.json")
    return ApiKeys(settings, KeyringSecretStore(backend=backend)), backend


def _section(qtbot, keys, runpod_card=None):
    section = KeysSection(keys, runpod_card)
    qtbot.addWidget(section)
    qtbot.waitUntil(lambda: not section.loading, timeout=5000)
    return section


def _open_details(section, key):
    section.findChild(QPushButton, f"keys-entry-{key}-details-toggle").click()


def _button(section, name):
    return section.findChild(QPushButton, name)


def _field(section, name):
    return section.findChild(QLineEdit, name)


def _timer(section, name):
    return section.findChild(QTimer, name)


def _message(section):
    return section.findChild(QLabel, "keys-message").text()


def _all_text(section):
    """Every label, button and line of the block. A revealed value must appear in none of them."""
    texts = [label.text() for label in section.findChildren(QLabel)]
    texts += [button.text() for button in section.findChildren(QPushButton)]
    texts += [field.text() for field in section.findChildren(QLineEdit)]
    return "\n".join(texts)


# ----------------------------------------------------------------- Reveal and Hide


def test_the_value_is_masked_until_reveal_is_pressed(qtbot, folder):
    keys, _ = _keys(folder)
    keys.save("OpenAI", VALUE)
    section = _section(qtbot, keys)
    _open_details(section, "openai")

    shown = _field(section, "keys-entry-openai-revealed")
    assert shown is not None
    assert shown.text() == ""
    assert shown.isHidden()
    assert _button(section, "keys-entry-openai-hide").isHidden()
    assert VALUE not in _all_text(section), "nothing reads or displays a value when the block is built"


def test_reveal_shows_the_value_in_a_read_only_field_and_leaves_the_write_box_empty(qtbot, folder):
    keys, _ = _keys(folder)
    keys.save("OpenAI", VALUE)
    section = _section(qtbot, keys)
    _open_details(section, "openai")

    _button(section, "keys-entry-openai-reveal").click()

    shown = _field(section, "keys-entry-openai-revealed")
    assert shown.text() == VALUE
    assert not shown.isHidden()
    assert shown.isReadOnly()
    assert shown.echoMode() == QLineEdit.EchoMode.Normal
    assert not _button(section, "keys-entry-openai-hide").isHidden()
    assert _field(section, "keys-entry-openai-value").text() == "", "the write-only box is never filled from the keychain"


def test_hide_masks_the_value_at_once(qtbot, folder):
    keys, _ = _keys(folder)
    keys.save("OpenAI", VALUE)
    section = _section(qtbot, keys)
    _open_details(section, "openai")
    _button(section, "keys-entry-openai-reveal").click()

    _button(section, "keys-entry-openai-hide").click()

    shown = _field(section, "keys-entry-openai-revealed")
    assert shown.text() == ""
    assert shown.isHidden()
    assert _button(section, "keys-entry-openai-hide").isHidden()
    assert not _timer(section, "keys-entry-openai-reveal-timer").isActive()
    assert VALUE not in _all_text(section)


def test_the_auto_hide_handler_masks_the_value_without_waiting_thirty_seconds(qtbot, folder):
    keys, _ = _keys(folder)
    keys.save("OpenAI", VALUE)
    section = _section(qtbot, keys)
    _open_details(section, "openai")
    _button(section, "keys-entry-openai-reveal").click()
    timer = _timer(section, "keys-entry-openai-reveal-timer")
    assert timer.isActive()
    assert timer.interval() == 30_000

    section._auto_hide("OpenAI")

    shown = _field(section, "keys-entry-openai-revealed")
    assert shown.text() == ""
    assert shown.isHidden()
    assert not timer.isActive()
    assert _message(section) == keys_section.REVEAL_HIDDEN_TEXT.format(name="OpenAI")


def test_the_timer_timeout_is_wired_to_the_auto_hide(qtbot, folder):
    keys, _ = _keys(folder)
    keys.save("OpenAI", VALUE)
    section = _section(qtbot, keys)
    _open_details(section, "openai")
    _button(section, "keys-entry-openai-reveal").click()

    _timer(section, "keys-entry-openai-reveal-timer").timeout.emit()

    assert _field(section, "keys-entry-openai-revealed").isHidden()
    assert _field(section, "keys-entry-openai-revealed").text() == ""


def test_pressing_reveal_again_restarts_the_thirty_seconds(qtbot, folder):
    """Qt restarts a running timer when start() is called on it, so each press must call start() on the timer.

    The calls are counted rather than read back from QTimer.remainingTime(). On the machine this was written on that value
    read up to about a second low, even right after a fresh start, so a threshold on it could not tell a restart apart.
    """
    keys, _ = _keys(folder)
    keys.save("OpenAI", VALUE)
    section = _section(qtbot, keys)
    _open_details(section, "openai")
    reveal = _button(section, "keys-entry-openai-reveal")
    timer = _timer(section, "keys-entry-openai-reveal-timer")
    starts = []
    real_start = timer.start

    def counting_start(*args):
        starts.append(args)
        real_start(*args)

    timer.start = counting_start  # the section calls timer.start() on each press of Reveal
    reveal.click()
    assert len(starts) == 1

    reveal.click()

    assert len(starts) == 2, "a second press calls start again, which restarts the 30 seconds"
    assert timer.isActive()
    assert _field(section, "keys-entry-openai-revealed").text() == VALUE


def test_a_key_with_no_value_shows_nothing_on_reveal_or_copy(qtbot, folder, clipboard):
    keys, _ = _keys(folder)
    section = _section(qtbot, keys, runpod_card=QLabel("runpod card"))

    _button(section, "keys-entry-runpod-reveal").click()
    assert _field(section, "keys-entry-runpod-revealed").isHidden()
    assert _message(section) == keys_section.REVEAL_NO_VALUE_TEXT.format(name="RunPod")

    _button(section, "keys-entry-runpod-copy").click()
    assert clipboard.text() == ""
    assert _message(section) == keys_section.COPY_NO_VALUE_TEXT.format(name="RunPod")


def test_the_runpod_card_reveals_its_saved_value_the_same_way(qtbot, folder):
    keys, _ = _keys(folder)
    keys.save("RunPod", VALUE)
    section = _section(qtbot, keys, runpod_card=QLabel("runpod card"))

    _button(section, "keys-entry-runpod-reveal").click()

    assert _field(section, "keys-entry-runpod-revealed").text() == VALUE
    _button(section, "keys-entry-runpod-hide").click()
    assert _field(section, "keys-entry-runpod-revealed").text() == ""


def test_a_keychain_that_cannot_be_read_shows_no_value_and_no_error_text(qtbot, folder, clipboard):
    backend = FlakyKeyring()
    keys, _ = _keys(folder, backend)
    keys.save("OpenAI", VALUE)
    section = _section(qtbot, keys)
    _open_details(section, "openai")
    backend.fail_reads = True

    _button(section, "keys-entry-openai-reveal").click()
    assert _field(section, "keys-entry-openai-revealed").isHidden()
    assert _message(section) == keys_section.REVEAL_NOT_READ_TEXT

    _button(section, "keys-entry-openai-copy").click()
    assert clipboard.text() == ""
    assert _message(section) == keys_section.COPY_NOT_READ_TEXT
    assert VALUE not in _all_text(section)


# ----------------------------------------------------------------- Copy value and the clipboard


def test_copy_value_puts_the_value_on_the_clipboard_and_starts_the_clear_timer(qtbot, folder, clipboard):
    keys, _ = _keys(folder)
    keys.save("OpenAI", VALUE)
    section = _section(qtbot, keys)
    _open_details(section, "openai")

    _button(section, "keys-entry-openai-copy").click()

    assert clipboard.text() == VALUE
    clear = _timer(section, "keys-clipboard-timer")
    assert clear.isActive()
    assert clear.interval() == 30_000
    assert _message(section) == keys_section.COPIED_VALUE_TEXT.format(
        name="OpenAI", seconds=keys_section.CLIPBOARD_SECONDS
    )


def test_the_clipboard_is_cleared_when_it_still_holds_the_copied_value(qtbot, folder, clipboard):
    keys, _ = _keys(folder)
    keys.save("OpenAI", VALUE)
    section = _section(qtbot, keys)
    _open_details(section, "openai")
    _button(section, "keys-entry-openai-copy").click()

    section._clear_copied_value()

    assert clipboard.text() == ""


def test_the_clipboard_is_left_alone_when_it_no_longer_holds_the_copied_value(qtbot, folder, clipboard):
    keys, _ = _keys(folder)
    keys.save("OpenAI", VALUE)
    section = _section(qtbot, keys)
    _open_details(section, "openai")
    _button(section, "keys-entry-openai-copy").click()
    clipboard.setText(OTHER)

    section._clear_copied_value()

    assert clipboard.text() == OTHER, "text the owner copied since then is not cleared"


def test_the_clear_timer_timeout_is_wired_to_the_clear(qtbot, folder, clipboard):
    keys, _ = _keys(folder)
    keys.save("OpenAI", VALUE)
    section = _section(qtbot, keys)
    _open_details(section, "openai")
    _button(section, "keys-entry-openai-copy").click()

    _timer(section, "keys-clipboard-timer").timeout.emit()

    assert clipboard.text() == ""


# ----------------------------------------------------------------- ApiKeys.read_value


def test_read_value_returns_the_saved_value_and_none_for_an_unset_key(folder):
    keys, _ = _keys(folder)
    keys.save("OpenAI", VALUE)

    assert keys.read_value("OpenAI") == VALUE
    assert keys.read_value("RunPod") is None, "RunPod is listed, and no value is saved for it"


def test_read_value_refuses_a_name_that_is_not_listed(folder):
    keys, _ = _keys(folder)

    with pytest.raises(KeyNameError):
        keys.read_value("Never Listed")


# ----------------------------------------------------------------- the settings file and the log


def test_the_settings_file_and_the_log_never_hold_the_value(qtbot, folder, clipboard, caplog):
    caplog.set_level(logging.DEBUG)
    keys, _ = _keys(folder)
    keys.save("OpenAI", VALUE)
    section = _section(qtbot, keys)
    _open_details(section, "openai")
    _button(section, "keys-entry-openai-reveal").click()
    _button(section, "keys-entry-openai-copy").click()
    section._clear_copied_value()
    _button(section, "keys-entry-openai-hide").click()

    settings = folder / "settings.json"
    assert settings.is_file()
    written = [path for path in folder.rglob("*") if path.is_file()]
    assert settings in written
    for path in written:
        assert VALUE.encode("utf-8") not in path.read_bytes(), f"{path.name} holds the value"
    assert VALUE not in caplog.text
