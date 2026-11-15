"""Settings states the folder that hosted-project backups are written to.

The Projects tab states the same folder next to its backup button (tests/test_projects_backup.py). Settings shows it in
the connection section, so the owner can see where backups go without opening the Projects tab. Nothing is written here:
LOCALAPPDATA points at a temp folder for the test, and the keychain is an in-memory fake.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtWidgets import QLabel

from _fakes import MemoryKeyring
from meridian_desktop import projects_backup
from meridian_desktop.secret_store import KeyringSecretStore
from meridian_desktop.settings_store import Settings, SettingsStore
from meridian_desktop.ui.settings_tab import SettingsTab

HOSTED_URL = "https://hosted.test"
LOCAL_URL = "http://127.0.0.1:7878"


@pytest.fixture
def local_folder(tmp_path, monkeypatch) -> Path:
    """A LOCALAPPDATA of this test's own, so the backup folder is this test's and goes away with it."""
    root = tmp_path / "local-app-data"
    monkeypatch.setenv("LOCALAPPDATA", str(root))
    return root / "meridian-desktop" / "backups"


def test_the_settings_connection_section_states_the_backup_folder(qtbot, settings_path, local_folder):
    SettingsStore(settings_path).save(Settings(mode="local", hosted_url=HOSTED_URL, local_url=LOCAL_URL))
    tab = SettingsTab(
        SettingsStore(settings_path),
        secrets=KeyringSecretStore(backend=MemoryKeyring()),
        list_hosted=lambda url, token: [],
    )
    qtbot.addWidget(tab)

    destination = tab.findChild(QLabel, "settings-backup-destination")

    assert destination is not None
    assert destination.text() == projects_backup.destination_text(local_folder)
