"""Export every saved key for migration: the pure export (meridian_desktop/key_export.py) and the Export keys dialog that the
Keys tab opens from its keys-export button (ui/key_export_dialog.py).

Every value and passphrase here is fake. The export files are temp files, the keychain is a MemoryKeyring created for each
test, the settings are a temp file, and the Windows user variables go to a FakeUserRegistry, so nothing real is touched. The
save dialog and the replace question are replaced by functions, so no real dialog opens. No test runs git: the git check
only looks for a ``.git`` entry, and the test fakes one with a folder or a file.
"""

import json
import subprocess

import pytest
from PySide6.QtWidgets import QLabel, QLineEdit, QPushButton, QRadioButton

from _fakes import FakeUserRegistry, MemoryKeyring
from meridian_desktop import key_vault
from meridian_desktop.api_keys import KEY_SERVICE, ApiKeys, account_for
from meridian_desktop.key_export import (
    GIT_TEXT,
    KIND_ENCRYPTED,
    KIND_PLAINTEXT,
    NONE_TEXT,
    NOT_CONFIRMED_TEXT,
    PLAINTEXT_CONFIRMATION,
    UNREADABLE_TEXT,
    ExportError,
    TargetExistsError,
    count_saved,
    export_keys,
    inside_git_repository,
)
from meridian_desktop.key_import import import_checked
from meridian_desktop.secret_store import KeyringSecretStore
from meridian_desktop.settings_store import SettingsStore
from meridian_desktop.ui.key_export_dialog import (
    KEPT_TEXT,
    MISMATCH_HINT_TEXT,
    REPLACE_TITLE,
    SHORT_HINT_TEXT,
    KeyExportDialog,
    PlaintextConfirmDialog,
)
from meridian_desktop.ui.keys_section import KeysSection
from meridian_desktop.user_environment import UserEnvironment

PASSPHRASE = "fake-export-pass-0001"
OTHER_PASSPHRASE = "fake-export-pass-0002"
SHORT_PASSPHRASE = "fake-short"
OPENAI_VALUE = "sk-fake-export-ALPHA-0001"
ANTHROPIC_VALUE = "sk-fake-export-BRAVO-0002"
ACME_VALUE = "sk-fake-export-CHARLIE-0003"
RUNPOD_VALUE = "sk-fake-export-DELTA-0004"
STALE_VALUE = "sk-fake-export-ECHO-0005"
OLDER_FILE = b"an older file that must stay as it is"


@pytest.fixture
def folder(tmp_path):
    return tmp_path


@pytest.fixture
def backend():
    """A keychain of its own for each test. The session-wide one is shared, so a test never uses it."""
    return MemoryKeyring()


@pytest.fixture
def registry():
    """The fake Windows user environment. An export must leave it empty."""
    return FakeUserRegistry()


def _keys(folder, backend, registry):
    settings = SettingsStore(folder / "settings.json")
    return ApiKeys(settings, KeyringSecretStore(backend=backend), user_environment=UserEnvironment(registry))


def _saved_keys(folder, backend, registry):
    """Three keys with values: two custom names and one built-in-style custom name. Their values are fake."""
    keys = _keys(folder, backend, registry)
    keys.save("OpenAI", OPENAI_VALUE)
    keys.save("Anthropic", ANTHROPIC_VALUE)
    keys.save("Acme Lab", ACME_VALUE)
    return keys


def _out_folder(folder):
    out = folder / "out"
    out.mkdir(exist_ok=True)
    return out


def _section(qtbot, keys, choose_path, confirm=None):
    section = KeysSection(keys, None, choose_export_path=choose_path, confirm_export=confirm)
    qtbot.addWidget(section)
    qtbot.waitUntil(lambda: not section.loading, timeout=5000)
    return section


def _open_export(section):
    section.findChild(QPushButton, "keys-export").click()
    dialog = section.findChild(KeyExportDialog, "keys-export-dialog")
    assert dialog is not None
    return dialog


def _fill_passphrase(dialog, first, again=None):
    dialog.findChild(QLineEdit, "keys-export-passphrase").setText(first)
    dialog.findChild(QLineEdit, "keys-export-passphrase-confirm").setText(first if again is None else again)


def _status(dialog):
    return dialog.findChild(QLabel, "keys-export-status").text()


# ----------------------------------------------------------------- the pure export


def test_an_encrypted_export_imports_back_through_the_vault_import(folder, backend, registry):
    keys = _saved_keys(folder, backend, registry)
    target = _out_folder(folder) / "keys-export.json"

    result = export_keys(keys, target, KIND_ENCRYPTED, passphrase=PASSPHRASE)

    assert result.count == 3
    assert result.kind == KIND_ENCRYPTED
    assert result.path == target
    fresh_folder = folder / "fresh"
    fresh_folder.mkdir()
    fresh_registry = FakeUserRegistry()
    fresh_keys = _keys(fresh_folder, MemoryKeyring(), fresh_registry)
    vault = key_vault.unlock(target, PASSPHRASE)
    imported = import_checked(fresh_keys, vault, vault.names(), replace=False)
    assert sorted(imported.imported) == ["Acme Lab", "Anthropic", "OpenAI"]
    assert fresh_keys.read_value("OpenAI") == OPENAI_VALUE
    assert fresh_keys.read_value("Anthropic") == ANTHROPIC_VALUE
    assert fresh_keys.read_value("Acme Lab") == ACME_VALUE
    assert fresh_registry.writes == [] and registry.writes == []


def test_the_encrypted_file_holds_no_key_value_or_name_in_plain_bytes(folder, backend, registry):
    keys = _saved_keys(folder, backend, registry)
    target = _out_folder(folder) / "keys-export.json"

    export_keys(keys, target, KIND_ENCRYPTED, passphrase=PASSPHRASE)

    data = target.read_bytes()
    for secret in (OPENAI_VALUE, ANTHROPIC_VALUE, ACME_VALUE, PASSPHRASE):
        assert secret.encode("utf-8") not in data
    for name in ("OpenAI", "Anthropic", "Acme Lab"):
        assert name.encode("utf-8") not in data
    # The file is the vault format: exactly the five fields, and the values only inside the ciphertext.
    assert sorted(json.loads(data.decode("utf-8"))) == ["ciphertext", "kdf", "nonce", "salt", "version"]


def test_a_plaintext_export_is_refused_without_the_typed_confirmation(folder, backend, registry):
    keys = _saved_keys(folder, backend, registry)
    target = _out_folder(folder) / "plain.json"

    for confirmation in ("", "plaintext", "PLAINTEXT ", " PLAINTEXT", "PLAIN TEXT", "yes"):
        with pytest.raises(ExportError) as caught:
            export_keys(keys, target, KIND_PLAINTEXT, confirmation=confirmation)
        assert str(caught.value) == NOT_CONFIRMED_TEXT
    assert not target.exists()

    result = export_keys(keys, target, KIND_PLAINTEXT, confirmation=PLAINTEXT_CONFIRMATION)

    assert result.count == 3
    document = json.loads(target.read_text(encoding="utf-8"))
    pairs = {item["name"]: item["value"] for item in document["keys"]}
    assert pairs == {"OpenAI": OPENAI_VALUE, "Anthropic": ANTHROPIC_VALUE, "Acme Lab": ACME_VALUE}


def test_a_folder_inside_a_git_repository_is_refused_and_git_is_never_run(folder, backend, registry, monkeypatch):
    keys = _saved_keys(folder, backend, registry)

    def git_must_not_run(*args, **kwargs):
        raise AssertionError("the export must not run git")

    monkeypatch.setattr(subprocess, "run", git_must_not_run)
    monkeypatch.setattr(subprocess, "Popen", git_must_not_run)
    repo = folder / "repo"
    (repo / ".git").mkdir(parents=True)
    nested = repo / "notes" / "deep"
    nested.mkdir(parents=True)

    for kind, kwargs in (
        (KIND_ENCRYPTED, {"passphrase": PASSPHRASE}),
        (KIND_PLAINTEXT, {"confirmation": PLAINTEXT_CONFIRMATION}),
    ):
        target = nested / f"export-{kind}.json"
        with pytest.raises(ExportError) as caught:
            export_keys(keys, target, kind, **kwargs)
        assert str(caught.value) == GIT_TEXT
        assert not target.exists()
    assert inside_git_repository(nested)


def test_a_git_entry_that_is_a_file_counts_and_a_folder_without_one_is_allowed(folder, backend, registry):
    keys = _saved_keys(folder, backend, registry)
    worktree = folder / "worktree"
    worktree.mkdir()
    (worktree / ".git").write_text("gitdir: fake-location\n", encoding="utf-8")

    with pytest.raises(ExportError) as caught:
        export_keys(keys, worktree / "keys.json", KIND_PLAINTEXT, confirmation=PLAINTEXT_CONFIRMATION)
    assert str(caught.value) == GIT_TEXT

    result = export_keys(keys, _out_folder(folder) / "keys.json", KIND_PLAINTEXT, confirmation=PLAINTEXT_CONFIRMATION)

    assert result.count == 3


def test_an_existing_file_is_not_replaced_without_confirmation(folder, backend, registry):
    keys = _saved_keys(folder, backend, registry)
    target = _out_folder(folder) / "keys.json"
    target.write_bytes(OLDER_FILE)

    with pytest.raises(TargetExistsError):
        export_keys(keys, target, KIND_ENCRYPTED, passphrase=PASSPHRASE)
    with pytest.raises(TargetExistsError):
        export_keys(keys, target, KIND_PLAINTEXT, confirmation=PLAINTEXT_CONFIRMATION)

    assert target.read_bytes() == OLDER_FILE
    assert not (target.parent / (target.name + ".export-tmp")).exists()

    result = export_keys(keys, target, KIND_PLAINTEXT, confirmation=PLAINTEXT_CONFIRMATION, overwrite=True)

    assert result.count == 3
    assert json.loads(target.read_text(encoding="utf-8"))["format"] == "meridian-keys-plaintext"


def test_the_count_matches_the_keychain_entries_that_hold_a_value(folder, backend, registry):
    keys = _saved_keys(folder, backend, registry)
    keys.save("RunPod", RUNPOD_VALUE)
    keys.save("Stale", STALE_VALUE)
    # The name stays listed, but its keychain entry is gone, so it is not a key to export.
    backend.delete_password(KEY_SERVICE, account_for("Stale"))
    target = _out_folder(folder) / "keys.json"

    assert count_saved(keys) == 4
    result = export_keys(keys, target, KIND_PLAINTEXT, confirmation=PLAINTEXT_CONFIRMATION)

    assert result.count == len(backend.snapshot()) == 4
    document = json.loads(target.read_text(encoding="utf-8"))
    assert sorted(item["name"] for item in document["keys"]) == ["Acme Lab", "Anthropic", "OpenAI", "RunPod"]


def test_an_export_with_no_saved_value_is_refused(folder, backend, registry):
    keys = _keys(folder, backend, registry)
    target = _out_folder(folder) / "empty.json"

    with pytest.raises(ExportError) as caught:
        export_keys(keys, target, KIND_PLAINTEXT, confirmation=PLAINTEXT_CONFIRMATION)

    assert str(caught.value) == NONE_TEXT
    assert not target.exists()


def test_an_unreadable_keychain_value_stops_the_whole_export(folder, backend, registry, monkeypatch):
    keys = _saved_keys(folder, backend, registry)
    target = _out_folder(folder) / "keys.json"

    def unreadable(name):
        raise RuntimeError(f"read failed while holding {ACME_VALUE}")

    monkeypatch.setattr(keys, "read_value", unreadable)

    with pytest.raises(ExportError) as caught:
        export_keys(keys, target, KIND_PLAINTEXT, confirmation=PLAINTEXT_CONFIRMATION)

    assert str(caught.value) == UNREADABLE_TEXT
    assert ACME_VALUE not in str(caught.value)
    assert caught.value.__suppress_context__, "the failed read's text is never chained onto the refusal"
    assert not target.exists(), "a partial file is never left behind"


# ----------------------------------------------------------------- the Export keys dialog


def test_the_export_keys_button_opens_a_dialog_that_counts_the_saved_keys(qtbot, folder, backend, registry):
    keys = _saved_keys(folder, backend, registry)
    section = _section(qtbot, keys, lambda parent, default_name: str(folder / "unused.json"))

    dialog = _open_export(section)

    assert dialog.findChild(QLabel, "keys-export-count").text() == "3 keys have a saved value and will be exported."
    assert dialog.findChild(QRadioButton, "keys-export-format-encrypted").isChecked(), "encrypted is the default"
    assert dialog.findChild(QPushButton, "keys-export-go").isEnabled() is False, "no passphrase is typed yet"


def test_an_encrypted_export_is_written_once_the_passphrases_match(qtbot, folder, backend, registry):
    keys = _saved_keys(folder, backend, registry)
    target = _out_folder(folder) / "keys.json"
    suggested = []

    def choose(parent, default_name):
        suggested.append(default_name)
        return str(target)

    section = _section(qtbot, keys, choose)
    dialog = _open_export(section)
    _fill_passphrase(dialog, PASSPHRASE)
    go = dialog.findChild(QPushButton, "keys-export-go")
    assert go.isEnabled()

    go.click()

    assert target.is_file()
    assert suggested == ["meridian-keys-export-encrypted.json"]
    assert _status(dialog).startswith("Exported 3 keys to ")
    assert dialog.findChild(QLineEdit, "keys-export-passphrase").text() == "", "the passphrase is cleared after a write"
    assert key_vault.unlock(target, PASSPHRASE).names() == ["Acme Lab", "Anthropic", "OpenAI"]


def test_the_encrypted_go_button_waits_for_a_long_enough_matching_passphrase(qtbot, folder, backend, registry):
    keys = _saved_keys(folder, backend, registry)
    target = _out_folder(folder) / "keys.json"
    section = _section(qtbot, keys, lambda parent, default_name: str(target))
    dialog = _open_export(section)
    go = dialog.findChild(QPushButton, "keys-export-go")

    _fill_passphrase(dialog, SHORT_PASSPHRASE)
    assert go.isEnabled() is False
    assert dialog.findChild(QLabel, "keys-export-hint").text() == SHORT_HINT_TEXT

    _fill_passphrase(dialog, PASSPHRASE, OTHER_PASSPHRASE)
    assert go.isEnabled() is False
    assert dialog.findChild(QLabel, "keys-export-hint").text() == MISMATCH_HINT_TEXT

    _fill_passphrase(dialog, PASSPHRASE, PASSPHRASE)
    assert go.isEnabled()
    assert not target.exists(), "nothing is written until the owner presses the button"


def test_a_plaintext_file_is_written_only_after_the_typed_word(qtbot, folder, backend, registry):
    keys = _saved_keys(folder, backend, registry)
    target = _out_folder(folder) / "plain.json"
    section = _section(qtbot, keys, lambda parent, default_name: str(target))
    dialog = _open_export(section)

    dialog.findChild(QRadioButton, "keys-export-format-plaintext").setChecked(True)
    assert dialog.findChild(QLineEdit, "keys-export-passphrase").isHidden(), "no passphrase is asked for a plaintext file"
    dialog.findChild(QPushButton, "keys-export-go").click()

    confirm = dialog.findChild(PlaintextConfirmDialog, "keys-export-plaintext-dialog")
    assert confirm is not None
    write = confirm.findChild(QPushButton, "keys-export-plaintext-write")
    typed = confirm.findChild(QLineEdit, "keys-export-plaintext-confirm")
    assert write.isEnabled() is False
    typed.setText("plaintext")
    assert write.isEnabled() is False, "the word must match exactly"
    write.click()
    assert not target.exists()

    typed.setText("PLAINTEXT")
    assert write.isEnabled()
    write.click()

    assert target.is_file()
    names = sorted(item["name"] for item in json.loads(target.read_text(encoding="utf-8"))["keys"])
    assert names == ["Acme Lab", "Anthropic", "OpenAI"]
    assert _status(dialog).startswith("Wrote 3 keys in plain text to ")


@pytest.mark.parametrize("answer", [False, True])
def test_the_dialog_keeps_an_existing_file_unless_the_owner_says_yes(qtbot, folder, backend, registry, answer):
    keys = _saved_keys(folder, backend, registry)
    target = _out_folder(folder) / "keys.json"
    target.write_bytes(OLDER_FILE)
    asked = []

    def confirm(parent, title, text):
        asked.append(title)
        return answer

    section = _section(qtbot, keys, lambda parent, default_name: str(target), confirm=confirm)
    dialog = _open_export(section)
    _fill_passphrase(dialog, PASSPHRASE)
    dialog.findChild(QPushButton, "keys-export-go").click()

    assert asked == [REPLACE_TITLE]
    if answer:
        assert target.read_bytes() != OLDER_FILE
        assert _status(dialog).startswith("Exported 3 keys to ")
    else:
        assert target.read_bytes() == OLDER_FILE
        assert _status(dialog) == KEPT_TEXT


def test_the_dialog_refuses_a_target_inside_a_git_repository(qtbot, folder, backend, registry):
    keys = _saved_keys(folder, backend, registry)
    repo = folder / "repo"
    (repo / ".git").mkdir(parents=True)
    target = repo / "keys.json"
    section = _section(qtbot, keys, lambda parent, default_name: str(target))
    dialog = _open_export(section)
    _fill_passphrase(dialog, PASSPHRASE)

    dialog.findChild(QPushButton, "keys-export-go").click()

    assert _status(dialog) == GIT_TEXT
    assert not target.exists()
