"""The names-only export: one JSON file of key names and their classification, and no value in it."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from PySide6.QtWidgets import QLabel, QPushButton

from _fakes import MemoryKeyring
from meridian_desktop import keys_export
from meridian_desktop.api_keys import ApiKeys
from meridian_desktop.keys_export import (
    CLASSIFIED,
    EXPORT_NAME,
    SOURCE_KEYCHAIN,
    SOURCE_KEYS_FILE,
    UNCLASSIFIED,
    export_rows,
    write_names_export,
)
from meridian_desktop.secret_store import KeyringSecretStore
from meridian_desktop.settings_store import SettingsStore
from meridian_desktop.ui import keys_section as keys_module
from meridian_desktop.ui.keys_section import EXPORT_FAILED_TEXT, EXPORTED_TEXT, KeysSection

FAKE_VALUE = "sk-fake-export-value-0001"
FAKE_FILE_VALUE = "sk-fake-export-value-0002"
WHEN = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)


def _export_path(settings_path: Path) -> Path:
    """The export is beside settings.json, in the backups folder that projects_backup uses."""
    return settings_path.parent / "backups" / EXPORT_NAME


def _section(qtbot, settings_path: Path, backend, choose=None):
    keys = ApiKeys(SettingsStore(settings_path), KeyringSecretStore(backend=backend))
    section = KeysSection(keys, choose_file=choose)
    qtbot.addWidget(section)
    qtbot.waitUntil(lambda: not section.loading, timeout=5000)
    return section, keys


def _write_keys_file(tmp_path: Path, content: dict) -> str:
    folder = tmp_path / "keys"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "my-keys.json"
    path.write_text(json.dumps(content), encoding="utf-8")
    return str(path)


def test_the_rows_hold_each_name_its_source_and_its_classification_once():
    rows = export_rows(["RunPod", "Personal"], ["Personal", "OpenAI"])

    assert rows == [
        {"name": "RunPod", "source": SOURCE_KEYCHAIN, "classification": CLASSIFIED},
        {"name": "Personal", "source": SOURCE_KEYCHAIN, "classification": UNCLASSIFIED},
        {"name": "OpenAI", "source": SOURCE_KEYS_FILE, "classification": CLASSIFIED},
    ], "a file name the keychain also holds shows once, as the keychain name"


def test_the_export_is_one_json_document_with_its_version_time_and_rows(tmp_path):
    path = write_names_export(tmp_path, ["RunPod"], ["OpenAI"], taken_at=WHEN)

    assert path == tmp_path / EXPORT_NAME
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document == {
        "version": 1,
        "exported_at": "2026-11-01T12:00:00Z",
        "keys": [
            {"name": "RunPod", "source": SOURCE_KEYCHAIN, "classification": CLASSIFIED},
            {"name": "OpenAI", "source": SOURCE_KEYS_FILE, "classification": CLASSIFIED},
        ],
    }


def test_a_second_export_replaces_the_first_and_leaves_no_temp_file(tmp_path):
    write_names_export(tmp_path, ["RunPod", "Old"], [], taken_at=WHEN)

    path = write_names_export(tmp_path, ["RunPod", "New"], [], taken_at=WHEN)

    names = [row["name"] for row in json.loads(path.read_text(encoding="utf-8"))["keys"]]
    assert names == ["RunPod", "New"]
    assert sorted(child.name for child in tmp_path.iterdir()) == [EXPORT_NAME]


def test_the_temp_file_is_flushed_and_synced_before_it_replaces_the_export(tmp_path, monkeypatch):
    # The whole export must be on disk at the moment of the sync, and the sync must come before the replace. The fake
    # sync reads the temp file as it is then: a missing flush would leave it short, and the assertion would fail there.
    temp = tmp_path / (EXPORT_NAME + ".tmp")
    document = keys_export.export_document(["RunPod"], ["OpenAI"], taken_at=WHEN)
    expected = json.dumps(document, ensure_ascii=False, indent=2)
    events: list[str] = []
    real_fsync = keys_export.os.fsync
    real_replace = keys_export.os.replace

    def recording_fsync(fd):
        events.append("fsync")
        assert temp.read_text(encoding="utf-8") == expected, "the whole export is written and flushed before the sync"
        real_fsync(fd)

    def recording_replace(src, dst):
        events.append("replace")
        real_replace(src, dst)

    monkeypatch.setattr(keys_export.os, "fsync", recording_fsync)
    monkeypatch.setattr(keys_export.os, "replace", recording_replace)

    path = write_names_export(tmp_path, ["RunPod"], ["OpenAI"], taken_at=WHEN)

    assert events == ["fsync", "replace"], "the sync comes before the replace, once each"
    assert path.read_text(encoding="utf-8") == expected


def test_a_failed_replace_keeps_the_earlier_export_and_removes_the_temp_file(tmp_path, monkeypatch):
    path = write_names_export(tmp_path, ["RunPod", "First"], [], taken_at=WHEN)
    before = path.read_bytes()

    def refuse(src, dst):
        raise OSError("refused for the test")

    monkeypatch.setattr(keys_export.os, "replace", refuse)
    with pytest.raises(OSError):
        write_names_export(tmp_path, ["RunPod", "Second"], [], taken_at=WHEN)

    assert path.read_bytes() == before, "the earlier export is left as it was"
    assert sorted(child.name for child in tmp_path.iterdir()) == [EXPORT_NAME]


def test_the_export_button_writes_names_and_no_value_beside_settings(qtbot, settings_path, tmp_path):
    backend = MemoryKeyring()
    path = _write_keys_file(tmp_path, {"OpenAI": FAKE_FILE_VALUE})
    section, keys = _section(qtbot, settings_path, backend, choose=lambda parent: path)
    keys.save("Personal", FAKE_VALUE)
    section.refresh()
    section.findChild(QPushButton, "keys-file-choose").click()

    section.findChild(QPushButton, "keys-export-names").click()

    text = _export_path(settings_path).read_text(encoding="utf-8")
    assert json.loads(text)["keys"] == [
        {"name": "RunPod", "source": SOURCE_KEYCHAIN, "classification": CLASSIFIED},
        {"name": "Personal", "source": SOURCE_KEYCHAIN, "classification": UNCLASSIFIED},
        {"name": "OpenAI", "source": SOURCE_KEYS_FILE, "classification": CLASSIFIED},
    ]
    assert FAKE_VALUE not in text, "no value of a keychain name is in the export"
    assert FAKE_FILE_VALUE not in text, "no value of a file name is in the export"
    shown = section.findChild(QLabel, "keys-message").text()
    assert shown == EXPORTED_TEXT.format(path=_export_path(settings_path))
    assert FAKE_VALUE not in shown and FAKE_FILE_VALUE not in shown


def test_the_export_button_says_so_when_the_names_cannot_be_written(qtbot, settings_path, monkeypatch):
    section, _keys = _section(qtbot, settings_path, MemoryKeyring())

    def refuse(folder, names, file_names, *, taken_at):
        raise OSError("refused for the test")

    monkeypatch.setattr(keys_module, "write_names_export", refuse)
    section.findChild(QPushButton, "keys-export-names").click()

    assert section.findChild(QLabel, "keys-message").text() == EXPORT_FAILED_TEXT
    assert not _export_path(settings_path).exists()
