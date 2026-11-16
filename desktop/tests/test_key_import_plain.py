"""Import keys from a plain keys file (a .json, .env, or secrets file): the pure import (meridian_desktop/key_import.py, its
import_plain_checked) and the Import dialog the Keys block opens from its keys-import-plain button (ui/key_import_plain_dialog.py).

Every value here is fake. The keys file is a temp file, the keychain is a MemoryKeyring created for each test, the settings are
a temp file, and the Windows user variables go to a FakeUserRegistry, so nothing real is touched. The file picker and the Yes or
No question are replaced by functions, so no real dialog opens.
"""

import json
import logging
import os
from pathlib import Path

import pytest
from PySide6.QtWidgets import QCheckBox, QLabel, QPushButton, QWidget

from _fakes import FakeUserRegistry, MemoryKeyring
from meridian_desktop import keys_file
from meridian_desktop.api_keys import MAX_CUSTOM_NAMES, STATUS_NOT_SET, STATUS_SET, ApiKeys
from meridian_desktop.key_import import NOT_SAVED_TEXT, import_plain_checked
from meridian_desktop.keys_file import NOT_SUPPORTED_TEXT, read_names, skipped_summary
from meridian_desktop.runner_controls import default_log_path
from meridian_desktop.secret_store import KeyringSecretStore, SecretStoreError
from meridian_desktop.settings_store import SettingsStore
from meridian_desktop.ui import keys_section as keys_module
from meridian_desktop.ui.key_import_plain_dialog import (
    COUNT_TEXT,
    NAME_NOTE_TEXT,
    PLAIN_QUESTION_TEXT,
    PLAIN_QUESTION_TITLE,
    KeyPlainImportDialog,
    build_plain_question,
)
from meridian_desktop.ui.keys_section import (
    IMPORT_PLAIN_TEXT,
    IMPORTED_PLAIN_ONE_TEXT,
    PLAIN_CANCELLED_TEXT,
    KeysSection,
)
from meridian_desktop.user_environment import UserEnvironment

OPENAI_VALUE = "sk-fake-plain-ALPHA-0001"
ACME_VALUE = "sk-fake-plain-BRAVO-0002"
OLD_VALUE = "sk-fake-plain-OLD-0003"
BROKEN_VALUE = "sk-fake-plain-REFUSED-0004"
# An old modification time, so a write to the file cannot go unseen.
PAST_SECONDS = 1_000_000_000


@pytest.fixture
def folder(tmp_path):
    return tmp_path


@pytest.fixture
def settings_file(folder):
    return folder / "settings.json"


@pytest.fixture
def backend():
    """A keychain of its own for each test. The session-wide one is shared, so a test never uses it."""
    return MemoryKeyring()


@pytest.fixture
def registry():
    """The fake Windows user environment. An import must leave it empty."""
    return FakeUserRegistry()


class _Answer:
    """The owner's answer to the Yes or No question, and each question it was asked with."""

    def __init__(self, yes: bool) -> None:
        self.yes = yes
        self.asked: list[tuple[str, str]] = []

    def __call__(self, parent, title, text):
        self.asked.append((title, text))
        return self.yes


def _keys(settings_file: Path, backend, registry, secrets=None) -> ApiKeys:
    store = secrets if secrets is not None else KeyringSecretStore(backend=backend)
    return ApiKeys(SettingsStore(settings_file), store, user_environment=UserEnvironment(registry))


def _write_text(folder: Path, name: str, text: str) -> Path:
    """A keys file named ``name`` under folder, with ``text`` in it."""
    path = folder / "keys" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _write_json(folder: Path, content, name: str = "my-keys.json") -> Path:
    """A .json keys file. A dict is written as JSON; text is written as it is."""
    text = content if isinstance(content, str) else json.dumps(content)
    return _write_text(folder, name, text)


def _pin(path: Path) -> None:
    os.utime(path, (PAST_SECONDS, PAST_SECONDS))


def _state(path: Path) -> tuple[bytes, int]:
    """The bytes and the modification time of a file. A write changes at least one of them."""
    return path.read_bytes(), path.stat().st_mtime_ns


def _read_spy(monkeypatch) -> list[str]:
    """Record the name of each value read from a keys file, and pass each read through to the real reader."""
    reads: list[str] = []
    real = keys_file.read_value

    def spy(path, name):
        reads.append(name)
        return real(path, name)

    monkeypatch.setattr(keys_file, "read_value", spy)
    return reads


def _all_text(widget: QWidget) -> str:
    """Every label, button and checkbox of a widget. A value must appear in none of them."""
    parts = [label.text() for label in widget.findChildren(QLabel)]
    parts += [button.text() for button in widget.findChildren(QPushButton)]
    parts += [box.text() for box in widget.findChildren(QCheckBox)]
    return "\n".join(parts)


def _section(qtbot, settings_file: Path, backend, registry, *, choose, confirm) -> KeysSection:
    """A Keys block over the given keychain. ``choose`` is the file picker fake and ``confirm`` the question fake."""
    # The Windows-variable box starts unticked, and this stub declines any question, so these tests keep the registry empty
    # unless they say otherwise.
    section = KeysSection(
        _keys(settings_file, backend, registry),
        None,
        choose_plain=choose,
        confirm_plain=confirm,
        confirm_plain_env=lambda *_: False,
    )
    qtbot.addWidget(section)
    qtbot.waitUntil(lambda: not section.loading, timeout=5000)
    return section


def _open_plain(section: KeysSection) -> KeyPlainImportDialog:
    """Press the keys-import-plain button and return the dialog it opens, or None when no dialog opens."""
    section.findChild(QPushButton, "keys-import-plain").click()
    return section.findChild(KeyPlainImportDialog, "keys-plain-import-dialog")


def _press_import(dialog: KeyPlainImportDialog) -> None:
    dialog.findChild(QPushButton, "keys-plain-import-go").click()


def _fresh_keys(settings_file: Path, backend, registry) -> ApiKeys:
    """The keys as a later session reads them: the same keychain, settings and fake registry."""
    return _keys(settings_file, backend, registry)


# ----------------------------------------------------------------- the pure import


def test_a_json_file_imports_its_ticked_names_in_file_order(folder, settings_file, backend, registry):
    path = _write_json(folder, {"OpenAI": OPENAI_VALUE, "Acme Lab": ACME_VALUE})
    keys = _keys(settings_file, backend, registry)

    result = import_plain_checked(keys, path, read_names(path).names, ["OpenAI", "Acme Lab"], replace=False)

    assert result.imported == ("OpenAI", "Acme Lab")
    assert result.kept == () and result.refused == () and result.skipped == ()
    assert keys.read_value("OpenAI") == OPENAI_VALUE
    assert keys.read_value("Acme Lab") == ACME_VALUE
    assert keys.status("Acme Lab") == STATUS_SET
    assert registry.writes == []


@pytest.mark.parametrize("name", [".env", ".env.local", "service.env", "app.secrets"])
def test_a_dotenv_or_secrets_file_imports_its_ticked_names(name, folder, settings_file, backend, registry):
    path = _write_text(folder, name, f'OpenAI={OPENAI_VALUE}\nexport Acme_Lab="{ACME_VALUE}"\n')
    keys = _keys(settings_file, backend, registry)

    result = import_plain_checked(keys, path, read_names(path).names, ["OpenAI", "Acme_Lab"], replace=False)

    assert result.imported == ("OpenAI", "Acme_Lab")
    assert keys.read_value("OpenAI") == OPENAI_VALUE
    assert keys.read_value("Acme_Lab") == ACME_VALUE
    assert registry.writes == []


def test_an_unticked_name_is_skipped_and_its_value_is_never_read(folder, settings_file, backend, registry, monkeypatch):
    path = _write_json(folder, {"OpenAI": OPENAI_VALUE, "Acme Lab": ACME_VALUE})
    reads = _read_spy(monkeypatch)
    keys = _keys(settings_file, backend, registry)

    result = import_plain_checked(keys, path, read_names(path).names, ["OpenAI"], replace=False)

    assert result.imported == ("OpenAI",)
    assert result.skipped == ("Acme Lab",)
    assert keys.status("Acme Lab") == STATUS_NOT_SET
    assert reads == ["OpenAI"]


def test_an_existing_name_is_kept_unless_replace_is_ticked_and_its_file_value_is_not_read(
    folder, settings_file, backend, registry, monkeypatch
):
    path = _write_json(folder, {"OpenAI": OPENAI_VALUE})
    keys = _keys(settings_file, backend, registry)
    keys.save("OpenAI", OLD_VALUE)
    reads = _read_spy(monkeypatch)
    names = read_names(path).names

    kept = import_plain_checked(keys, path, names, ["OpenAI"], replace=False)

    assert kept.kept == ("OpenAI",) and kept.imported == ()
    assert keys.read_value("OpenAI") == OLD_VALUE
    assert reads == [], "a kept name's value is not read from the file"

    replaced = import_plain_checked(keys, path, names, ["OpenAI"], replace=True)

    assert replaced.imported == ("OpenAI",) and replaced.kept == ()
    assert keys.read_value("OpenAI") == OPENAI_VALUE
    assert reads == ["OpenAI"]


def test_a_refused_entry_gives_its_keys_file_reason_and_its_value_never_appears(folder, settings_file, backend, registry):
    path = _write_text(folder, "keys.env", f'OpenAI={OPENAI_VALUE}\nBROKEN="{BROKEN_VALUE}\n')
    listing = read_names(path)
    keys = _keys(settings_file, backend, registry)

    result = import_plain_checked(keys, path, listing.names, listing.names, replace=False)

    assert listing.names == ("OpenAI",)
    assert [(item.where, item.reason) for item in listing.refused] == [("line 2", "value has no closing quote")]
    assert result.imported == ("OpenAI",)
    rendered = "\n".join([repr(listing), repr(result), skipped_summary(listing) or ""])
    assert BROKEN_VALUE not in rendered


def test_a_name_apikeys_refuses_is_refused_as_written_and_nothing_is_saved(folder, settings_file, backend, registry):
    keys = _keys(settings_file, backend, registry)
    for index in range(MAX_CUSTOM_NAMES):
        keys.save(f"Seed {index:02d}", OLD_VALUE)
    path = _write_json(folder, {"Newcomer": ACME_VALUE})

    result = import_plain_checked(keys, path, read_names(path).names, ["Newcomer"], replace=False)

    assert result.imported == ()
    assert [(item.name, item.reason) for item in result.refused] == [
        ("Newcomer", f"The list holds at most {MAX_CUSTOM_NAMES} custom names. Remove one first.")
    ]
    assert keys.status("Newcomer") == STATUS_NOT_SET
    assert ACME_VALUE not in repr(result)


class _FailingWriteStore(KeyringSecretStore):
    """A keychain whose write step fails. Its sentence names no value."""

    def set_secret(self, service, account, value):
        raise SecretStoreError("the keychain write step failed")


def test_a_failed_keychain_write_is_refused_with_its_own_sentence(folder, settings_file, backend, registry):
    path = _write_json(folder, {"OpenAI": OPENAI_VALUE})
    keys = _keys(settings_file, backend, registry, secrets=_FailingWriteStore(backend=backend))

    result = import_plain_checked(keys, path, read_names(path).names, ["OpenAI"], replace=False)

    assert result.imported == ()
    assert [(item.name, item.reason) for item in result.refused] == [("OpenAI", NOT_SAVED_TEXT)]
    assert keys.status("OpenAI") == STATUS_NOT_SET
    assert OPENAI_VALUE not in repr(result)


# ----------------------------------------------------------------- the question and the picker


def test_the_yes_or_no_question_defaults_to_no_and_says_what_it_is_for(qtbot):
    parent = QWidget()
    qtbot.addWidget(parent)

    box = build_plain_question(parent, PLAIN_QUESTION_TITLE, PLAIN_QUESTION_TEXT.format(name="keys.json"))

    assert box.defaultButton().objectName() == "keys-plain-import-confirm-no"
    assert box.escapeButton().objectName() == "keys-plain-import-confirm-no"
    assert box.findChild(QPushButton, "keys-plain-import-confirm-yes") is not None
    assert box.findChild(QPushButton, "keys-plain-import-confirm-no") is not None
    for phrase in ("readable form", "will not change the file", "keychain", "keys.json"):
        assert phrase in box.text()


def test_the_plain_picker_offers_the_kinds_the_keys_file_reader_accepts(monkeypatch):
    seen: dict[str, str] = {}

    class FakeDialog:
        @staticmethod
        def getOpenFileName(parent, title, start, filter_text):
            seen.update(title=title, filter=filter_text)
            return "", ""

    monkeypatch.setattr(keys_module, "QFileDialog", FakeDialog)

    assert keys_module._ask_plain_file(QWidget()) == ""
    assert "*.json" in seen["filter"] and "*.env" in seen["filter"] and "*.secrets" in seen["filter"]


def test_the_plain_import_button_sits_beside_the_vault_button(qtbot, folder, settings_file, backend, registry):
    section = _section(qtbot, settings_file, backend, registry, choose=lambda parent: "", confirm=_Answer(False))

    plain = section.findChild(QPushButton, "keys-import-plain")
    vault = section.findChild(QPushButton, "keys-import-vault")

    assert plain is not None and vault is not None
    assert plain.parentWidget() is vault.parentWidget()
    assert plain.text() == IMPORT_PLAIN_TEXT


# ----------------------------------------------------------------- the Yes or No gate


def test_answering_no_reads_nothing_and_writes_nothing(qtbot, folder, settings_file, backend, registry, monkeypatch):
    path = _write_json(folder, {"OpenAI": OPENAI_VALUE})
    _pin(path)
    before = _state(path)

    def must_not_read(*_args, **_kwargs):
        raise AssertionError("no name or value may be read before a Yes")

    monkeypatch.setattr(keys_module, "read_names", must_not_read)
    monkeypatch.setattr(keys_file, "read_value", must_not_read)
    answer = _Answer(yes=False)
    section = _section(qtbot, settings_file, backend, registry, choose=lambda parent: str(path), confirm=answer)

    dialog = _open_plain(section)

    assert len(answer.asked) == 1
    assert dialog is None
    assert section.findChild(QLabel, "keys-message").text() == PLAIN_CANCELLED_TEXT
    assert backend.snapshot() == {}
    assert registry.writes == [] and registry.values == {}
    assert _state(path) == before
    assert SettingsStore(settings_file).load().settings.key_names == ()


def test_a_file_of_a_kind_the_app_does_not_read_is_refused_before_any_question(
    qtbot, folder, settings_file, backend, registry
):
    path = _write_text(folder, "notes.txt", f"OpenAI={OPENAI_VALUE}")
    answer = _Answer(yes=True)
    section = _section(qtbot, settings_file, backend, registry, choose=lambda parent: str(path), confirm=answer)

    dialog = _open_plain(section)

    assert dialog is None
    assert answer.asked == [], "the question is not asked for a file the app does not read"
    assert section.findChild(QLabel, "keys-message").text() == NOT_SUPPORTED_TEXT
    assert OPENAI_VALUE not in _all_text(section)


# ----------------------------------------------------------------- the dialog


def test_a_json_file_imports_its_ticked_names_through_the_dialog(qtbot, folder, settings_file, backend, registry):
    path = _write_json(folder, {"OpenAI": OPENAI_VALUE, "Acme Lab": ACME_VALUE})
    _pin(path)
    before = _state(path)
    answer = _Answer(yes=True)
    section = _section(qtbot, settings_file, backend, registry, choose=lambda parent: str(path), confirm=answer)

    dialog = _open_plain(section)

    assert dialog is not None
    assert answer.asked[0][0] == PLAIN_QUESTION_TITLE
    assert path.name in answer.asked[0][1]
    assert dialog.findChild(QCheckBox, "keys-plain-import-name-openai").isChecked()
    assert dialog.findChild(QCheckBox, "keys-plain-import-name-acme-lab").isChecked()
    assert dialog.findChild(QCheckBox, "keys-plain-import-replace").isChecked() is False
    dialog.findChild(QCheckBox, "keys-plain-import-name-acme-lab").setChecked(False)
    assert dialog.findChild(QLabel, "keys-plain-import-count").text() == COUNT_TEXT.format(ticked=1, total=2)

    _press_import(dialog)

    keys = _fresh_keys(settings_file, backend, registry)
    assert keys.status("OpenAI") == STATUS_SET
    assert keys.read_value("OpenAI") == OPENAI_VALUE
    assert keys.status("Acme Lab") == STATUS_NOT_SET
    assert section.findChild(QLabel, "keys-message").text() == IMPORTED_PLAIN_ONE_TEXT
    assert "Imported 1 key" in dialog.findChild(QLabel, "keys-plain-import-result").text()
    assert registry.writes == [] and registry.values == {} and registry.announcements == 0
    assert _state(path) == before, "the source file is byte-identical and unchanged in time after the import"
    assert OPENAI_VALUE not in _all_text(dialog)


@pytest.mark.parametrize("name", [".env.local", "keys.env"])
def test_a_dotenv_file_imports_its_ticked_names_through_the_dialog(
    qtbot, folder, settings_file, backend, registry, name
):
    path = _write_text(folder, name, f"OpenAI={OPENAI_VALUE}\nAcme_Lab={ACME_VALUE}\n")
    section = _section(qtbot, settings_file, backend, registry, choose=lambda parent: str(path), confirm=_Answer(True))
    dialog = _open_plain(section)
    dialog.findChild(QCheckBox, "keys-plain-import-name-acme-lab").setChecked(False)

    _press_import(dialog)

    keys = _fresh_keys(settings_file, backend, registry)
    assert keys.read_value("OpenAI") == OPENAI_VALUE
    assert keys.status("Acme_Lab") == STATUS_NOT_SET
    assert registry.writes == []


def test_an_existing_name_is_kept_unless_replace_is_ticked_in_the_dialog(qtbot, folder, settings_file, backend, registry):
    path = _write_json(folder, {"OpenAI": OPENAI_VALUE})
    keys = _fresh_keys(settings_file, backend, registry)
    keys.save("OpenAI", OLD_VALUE)
    section = _section(qtbot, settings_file, backend, registry, choose=lambda parent: str(path), confirm=_Answer(True))
    dialog = _open_plain(section)
    assert dialog.findChild(QLabel, "keys-plain-import-note-openai").text() == NAME_NOTE_TEXT

    _press_import(dialog)

    assert keys.read_value("OpenAI") == OLD_VALUE
    assert "Kept 1 existing key" in dialog.findChild(QLabel, "keys-plain-import-result").text()

    dialog.findChild(QCheckBox, "keys-plain-import-replace").setChecked(True)
    _press_import(dialog)

    assert keys.read_value("OpenAI") == OPENAI_VALUE
    assert registry.writes == []


def test_a_refused_entry_is_reported_in_keys_file_wording_and_its_value_never_appears(
    qtbot, folder, settings_file, backend, registry
):
    path = _write_text(folder, "keys.env", f'OpenAI={OPENAI_VALUE}\nBROKEN="{BROKEN_VALUE}\n')
    section = _section(qtbot, settings_file, backend, registry, choose=lambda parent: str(path), confirm=_Answer(True))

    dialog = _open_plain(section)

    assert dialog.findChild(QLabel, "keys-plain-import-skipped").text() == (
        "Skipped 1 of 2 entries. The rest are loaded.\nline 2: value has no closing quote"
    )
    assert BROKEN_VALUE not in _all_text(dialog)
    _press_import(dialog)
    assert BROKEN_VALUE not in _all_text(dialog)
    assert BROKEN_VALUE not in settings_file.read_text(encoding="utf-8")


def test_a_file_refused_as_a_whole_says_why_and_offers_no_import(qtbot, folder, settings_file, backend, registry):
    path = _write_json(folder, f'["{BROKEN_VALUE}"]')
    section = _section(qtbot, settings_file, backend, registry, choose=lambda parent: str(path), confirm=_Answer(True))

    dialog = _open_plain(section)

    assert dialog.findChild(QLabel, "keys-plain-import-problem").text().startswith("Not loaded.")
    assert dialog.findChild(QCheckBox, "keys-plain-import-name-openai") is None
    assert dialog.findChild(QPushButton, "keys-plain-import-go").isEnabled() is False
    assert BROKEN_VALUE not in _all_text(dialog)


def test_closing_without_importing_writes_nothing(qtbot, folder, settings_file, backend, registry):
    path = _write_json(folder, {"OpenAI": OPENAI_VALUE, "Acme Lab": ACME_VALUE})
    _pin(path)
    before = _state(path)
    section = _section(qtbot, settings_file, backend, registry, choose=lambda parent: str(path), confirm=_Answer(True))
    dialog = _open_plain(section)
    dialog.findChild(QCheckBox, "keys-plain-import-name-openai").setChecked(False)

    dialog.findChild(QPushButton, "keys-plain-import-close").click()

    assert not dialog.isVisible()
    assert backend.snapshot() == {}
    assert registry.writes == [] and registry.values == {}
    assert SettingsStore(settings_file).load().settings.key_names == ()
    assert _state(path) == before


def test_the_settings_file_and_the_log_never_hold_a_value(qtbot, folder, settings_file, backend, registry, caplog):
    caplog.set_level(logging.DEBUG)
    path = _write_text(folder, "keys.env", f'OpenAI={OPENAI_VALUE}\nAcme_Lab={ACME_VALUE}\nBROKEN="{BROKEN_VALUE}\n')
    section = _section(qtbot, settings_file, backend, registry, choose=lambda parent: str(path), confirm=_Answer(True))
    dialog = _open_plain(section)
    _press_import(dialog)

    log = default_log_path(settings_file)
    for secret in (OPENAI_VALUE, ACME_VALUE, BROKEN_VALUE):
        assert secret not in caplog.text
        assert secret not in settings_file.read_text(encoding="utf-8")
        assert not log.exists() or secret not in log.read_text(encoding="utf-8")
    assert "Key file import" in caplog.text, "the import logs its counts"


def test_an_imported_known_service_keeps_the_critical_badge_its_file_card_showed(
    qtbot, folder, settings_file, backend, registry
):
    path = _write_json(folder, {"OpenAI": OPENAI_VALUE, "Acme Lab": ACME_VALUE})
    section = KeysSection(
        _keys(settings_file, backend, registry),
        None,
        choose_file=lambda parent: str(path),
        choose_plain=lambda parent: str(path),
        confirm_plain=_Answer(True),
        confirm_plain_env=lambda *_: False,
    )
    qtbot.addWidget(section)
    qtbot.waitUntil(lambda: not section.loading, timeout=5000)
    # The keys file is chosen in the Load keys from a file block, which lists its names as From file cards.
    section.findChild(QPushButton, "keys-file-choose").click()
    assert not section.findChild(QLabel, "keys-entry-openai-badge").isHidden(), "the file card shows the Critical badge"
    assert section.findChild(QLabel, "keys-entry-acme-lab-badge").isHidden(), "an unclassified name is not critical"

    _press_import(_open_plain(section))

    # The file card leaves the list when the name is imported, so wait until only the keychain card holds the badge.
    qtbot.waitUntil(lambda: len(section.findChildren(QLabel, "keys-entry-openai-badge")) == 1, timeout=5000)
    assert not section.findChild(QLabel, "keys-entry-openai-badge").isHidden(), "the keychain card keeps the Critical badge"
    assert section.findChild(QCheckBox, "keys-entry-openai-critical").isChecked()
    assert section.findChild(QLabel, "keys-entry-acme-lab-badge").isHidden()
    assert SettingsStore(settings_file).load().settings.critical_keys == ("OpenAI",)
