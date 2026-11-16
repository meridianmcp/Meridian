"""The hosted-projects backup: one run writes every hosted project to one file, and the Projects tab states where.

Every outside call is a fake that records its name. Nothing here reaches the hosted service, the OS keychain, a real
server or a real port. Each test that writes a backup points LOCALAPPDATA at its own temp folder, so no backup is left
in the session's folder for another test to read. FAKE_TOKEN is an obviously fake value, so a leak check can search for it.
"""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from PySide6.QtWidgets import QLabel, QPushButton, QTableWidget

from meridian_desktop import hosted_client, local_client, projects_backup
from meridian_desktop.data_source import DataSources
from meridian_desktop.settings_store import Settings, SettingsStore
from meridian_desktop.ui.projects_tab import LOADING_TEXT, ProjectsTab

FAKE_TOKEN = "sk_meridian_fake-backup-0005"
LOCAL_URL = "http://127.0.0.1:7878"
HOSTED_URL = "https://hosted.test"
TAKEN = datetime(2026, 10, 24, 23, 54, 24, tzinfo=timezone.utc)
LATER = datetime(2026, 10, 25, 8, 0, 0, tzinfo=timezone.utc)
BUTTON_TEXT = "Back up hosted projects to this computer"

# The hosted reply carries fields the backup must never keep: an executor config, agent notes and a secret-like value.
HOSTED_ROWS = [
    {
        "id": "h-1",
        "name": "Hosted one",
        "created_at": "2026-03-02T17:30:00",
        "status": "parked",
        "executor_config": {"secret": "FAKE-EXEC-VALUE"},
        "agent_instructions": "FAKE-AGENT-NOTE",
    },
    {"id": "h-2", "name": "Hosted two", "created_at": "2026-04-01T09:00:00"},
]
OLD_ROWS = [{"id": "b-1", "name": "Backed up one", "created_at": "2026-02-01T00:00:00"}]


@pytest.fixture
def local_folder(tmp_path, monkeypatch) -> Path:
    """A LOCALAPPDATA of this test's own, so the backup folder is this test's and goes away with it."""
    root = tmp_path / "local-app-data"
    monkeypatch.setenv("LOCALAPPDATA", str(root))
    return root / "meridian-desktop" / "backups"


def write_backup_at(folder: Path, when: datetime, rows=OLD_ROWS) -> projects_backup.BackupWritten:
    return projects_backup.write_backup(rows, folder=folder, taken_at=when)


def write_empty_backup_at(folder: Path, when: datetime) -> Path:
    """A backup file that holds no project, as a damaged or edited file might. The write path never makes one."""
    folder.mkdir(parents=True, exist_ok=True)
    document = {"version": 1, "taken_at": when.strftime("%Y-%m-%dT%H:%M:%SZ"), "projects": []}
    path = folder / projects_backup.backup_name(when)
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


# ----------------------------------------------------------------- the backup file


def test_backup_file_holds_only_names_ids_and_dates_and_never_the_token(local_folder):
    seen: list[tuple[str, str]] = []

    def list_hosted(url, token):
        seen.append((url, token))
        return list(HOSTED_ROWS)

    written = projects_backup.backup_hosted(list_hosted, HOSTED_URL, FAKE_TOKEN, folder=local_folder, taken_at=TAKEN)

    assert seen == [(HOSTED_URL, FAKE_TOKEN)]
    data = json.loads(written.path.read_text(encoding="utf-8"))
    assert set(data) == {"version", "taken_at", "projects"}
    assert data["taken_at"] == "2026-10-24T23:54:24Z"
    assert [set(project) for project in data["projects"]] == [{"id", "name", "created_at"}] * 2
    assert data["projects"][0] == {"id": "h-1", "name": "Hosted one", "created_at": "2026-03-02T17:30:00"}
    text = written.path.read_text(encoding="utf-8")
    for secret in (FAKE_TOKEN, "FAKE-EXEC-VALUE", "FAKE-AGENT-NOTE", "executor_config", "agent_instructions", HOSTED_URL):
        assert secret not in text
    assert written.count == 2


def test_backup_is_named_by_its_utc_time_in_the_desktop_data_folder(tmp_path, monkeypatch):
    assert projects_backup.backup_name(TAKEN) == "projects-20261024T235424Z.json"

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local-app-data"))
    assert projects_backup.backup_dir() == tmp_path / "local-app-data" / "meridian-desktop" / "backups"
    # Without LOCALAPPDATA the folder sits beside settings.json in the home folder, as settings does.
    assert projects_backup.backup_dir(env={}, home=tmp_path) == tmp_path / ".meridian-desktop" / "backups"


def test_the_message_says_the_count_the_day_and_the_file_it_was_saved_to(local_folder):
    written = write_backup_at(local_folder, TAKEN, rows=HOSTED_ROWS)

    assert written.count == 2
    assert written.message == f"Backed up 2 projects on 24 October 2026. Saved to: {written.path}"
    assert projects_backup.backed_up_text(1, TAKEN) == "Backed up 1 project on 24 October 2026."


def test_the_destination_line_names_the_backups_folder(local_folder):
    assert projects_backup.destination_text(local_folder) == f"Backups are saved to: {local_folder}"


def test_one_run_writes_every_hosted_project_and_reads_the_hosted_list_once(local_folder):
    reads: list[str] = []

    def list_hosted(url, token):
        reads.append(url)
        return list(HOSTED_ROWS)

    written = projects_backup.backup_hosted(list_hosted, HOSTED_URL, FAKE_TOKEN, folder=local_folder, taken_at=TAKEN)

    assert reads == [HOSTED_URL]
    assert [path.name for path in local_folder.iterdir()] == [written.path.name]
    ids = [project["id"] for project in json.loads(written.path.read_text(encoding="utf-8"))["projects"]]
    assert ids == ["h-1", "h-2"]


def test_the_newest_five_backups_are_kept_and_older_ones_are_removed(local_folder):
    # Seven writes, a day apart: the last five stay, and the two oldest are removed.
    times = [TAKEN + timedelta(days=day) for day in range(7)]
    for when in times:
        write_backup_at(local_folder, when)

    names = sorted(path.name for path in local_folder.iterdir())
    assert names == sorted(projects_backup.backup_name(when) for when in times[-5:])


@pytest.mark.parametrize(
    "answer",
    [[], [{"name": "no id"}, {"id": "   ", "name": "blank id"}, "not a row"]],
    ids=["empty-list", "no-usable-id"],
)
def test_an_empty_hosted_answer_writes_nothing_and_keeps_the_good_backup(local_folder, answer):
    good = write_backup_at(local_folder, TAKEN)

    with pytest.raises(projects_backup.NoUsableProjects):
        projects_backup.backup_hosted(lambda url, token: answer, HOSTED_URL, FAKE_TOKEN, folder=local_folder,
                                      taken_at=LATER)

    assert [path.name for path in local_folder.iterdir()] == [good.path.name]


def test_a_failed_hosted_read_writes_nothing_and_keeps_the_newest_backup(local_folder):
    kept = write_backup_at(local_folder, TAKEN)

    def unreachable(url, token):
        raise hosted_client.HostedUnreachable()

    with pytest.raises(hosted_client.HostedError):
        projects_backup.backup_hosted(unreachable, HOSTED_URL, FAKE_TOKEN, folder=local_folder, taken_at=LATER)

    assert [path.name for path in local_folder.iterdir()] == [kept.path.name]


def test_retention_keeps_the_newest_backup_with_projects_behind_five_newer_empty_ones(local_folder):
    good = write_backup_at(local_folder, TAKEN)
    for hours in range(1, 6):
        write_empty_backup_at(local_folder, TAKEN + timedelta(hours=hours))

    projects_backup.prune_backups(local_folder)

    names = sorted(path.name for path in local_folder.iterdir())
    assert good.path.name in names
    assert len(names) == 6


def test_a_second_backup_in_the_same_second_never_replaces_the_first(local_folder):
    first = write_backup_at(local_folder, TAKEN, rows=OLD_ROWS)
    second = write_backup_at(local_folder, TAKEN, rows=HOSTED_ROWS)

    assert first.path != second.path
    assert json.loads(first.path.read_text(encoding="utf-8"))["projects"] == [
        {"id": "b-1", "name": "Backed up one", "created_at": "2026-02-01T00:00:00"}
    ]
    assert len(json.loads(second.path.read_text(encoding="utf-8"))["projects"]) == 2


def test_an_empty_answer_makes_no_backups_folder(tmp_path):
    folder = tmp_path / "never-made"

    with pytest.raises(projects_backup.NoUsableProjects):
        projects_backup.write_backup([], folder=folder, taken_at=TAKEN)

    assert not folder.exists()


# ----------------------------------------------------------------- the Projects tab


def _sources(*, token: str | None = FAKE_TOKEN, list_hosted=None) -> DataSources:
    """Every outside read is a fake. The local server is not running and there is no database file."""
    return DataSources(
        probe=lambda url: local_client.HEALTH_NOT_RUNNING,
        read_server=lambda url: [],
        read_file=lambda path: [],
        get_token=lambda: token,
        list_hosted=list_hosted or (lambda url, saved: list(HOSTED_ROWS)),
    )


def _tab(qtbot, settings_path, sources):
    """A Projects tab that has finished its first load. Returns the tab and its status label."""
    SettingsStore(settings_path).save(Settings(mode="local", hosted_url=HOSTED_URL, local_url=LOCAL_URL))
    tab = ProjectsTab(SettingsStore(settings_path), sources)
    qtbot.addWidget(tab)
    tab.refresh()
    status = tab.findChild(QLabel, "projects-status")
    qtbot.waitUntil(lambda: status.text() != LOADING_TEXT, timeout=5000)
    return tab, status


def test_no_saved_token_means_no_backup_controls(qtbot, settings_path):
    tab, _status = _tab(qtbot, settings_path, _sources(token=None))

    assert tab.findChildren(QPushButton) == []
    assert tab.findChild(QLabel, "projects-backup-destination") is None
    assert tab.findChild(QLabel, "projects-backup-status") is None


def test_a_saved_token_shows_the_button_and_the_destination_before_any_run(qtbot, settings_path, local_folder):
    tab, _status = _tab(qtbot, settings_path, _sources())

    button = tab.findChild(QPushButton, "projects-backup-button")
    destination = tab.findChild(QLabel, "projects-backup-destination")
    assert button is not None and not button.isHidden()
    assert button.text() == BUTTON_TEXT
    assert destination is not None and not destination.isHidden()
    assert destination.text() == f"Backups are saved to: {local_folder}"
    assert list(local_folder.glob("projects-*.json")) == []


def test_the_button_runs_one_backup_and_names_the_file_it_saved(qtbot, settings_path, local_folder):
    reads: list[tuple[str, str]] = []

    def list_hosted(url, token):
        reads.append((url, token))
        return list(HOSTED_ROWS)

    tab, _status = _tab(qtbot, settings_path, _sources(list_hosted=list_hosted))
    button = tab.findChild(QPushButton, "projects-backup-button")
    backup_status = tab.findChild(QLabel, "projects-backup-status")

    button.click()
    qtbot.waitUntil(lambda: backup_status.text().startswith("Backed up"), timeout=5000)

    assert reads == [(HOSTED_URL, FAKE_TOKEN)]
    files = sorted(local_folder.glob("projects-*.json"))
    assert len(files) == 1
    assert re.fullmatch(
        r"Backed up 2 projects on \d{1,2} [A-Z][a-z]+ \d{4}\. Saved to: " + re.escape(str(files[0])),
        backup_status.text(),
    )
    text = files[0].read_text(encoding="utf-8")
    assert FAKE_TOKEN not in text and FAKE_TOKEN not in backup_status.text()
    assert "FAKE-EXEC-VALUE" not in text
    assert button.isEnabled()


def test_an_empty_hosted_answer_says_so_and_keeps_the_earlier_backup(qtbot, settings_path, local_folder):
    good = write_backup_at(local_folder, TAKEN)
    tab, _status = _tab(qtbot, settings_path, _sources(list_hosted=lambda url, token: []))
    button = tab.findChild(QPushButton, "projects-backup-button")
    backup_status = tab.findChild(QLabel, "projects-backup-status")

    button.click()
    qtbot.waitUntil(lambda: backup_status.text() == projects_backup.NO_PROJECTS_TEXT, timeout=5000)

    assert [path.name for path in local_folder.iterdir()] == [good.path.name]
    assert button.isEnabled()


def test_a_hosted_failure_is_named_in_plain_words_and_leaks_no_token(qtbot, settings_path, local_folder):
    def unreachable(url, token):
        raise hosted_client.HostedUnreachable()

    tab, _status = _tab(qtbot, settings_path, _sources(list_hosted=unreachable))
    button = tab.findChild(QPushButton, "projects-backup-button")
    backup_status = tab.findChild(QLabel, "projects-backup-status")

    button.click()
    qtbot.waitUntil(lambda: backup_status.text().startswith("Projects could not be backed up"), timeout=5000)

    assert FAKE_TOKEN not in backup_status.text()
    assert list(local_folder.glob("projects-*.json")) == []
    assert button.isEnabled()


def test_a_press_while_a_backup_runs_starts_no_second_run(qtbot, settings_path, local_folder):
    release = threading.Event()
    reads: list[str] = []

    def slow_list(url, token):
        reads.append(url)
        release.wait(timeout=5)
        return list(HOSTED_ROWS)

    tab, _status = _tab(qtbot, settings_path, _sources(list_hosted=slow_list))
    button = tab.findChild(QPushButton, "projects-backup-button")
    backup_status = tab.findChild(QLabel, "projects-backup-status")

    button.click()
    assert not button.isEnabled()
    assert backup_status.text() == "Backing up hosted projects..."
    tab._start_backup()  # a second press while the first run is in flight
    release.set()
    qtbot.waitUntil(lambda: backup_status.text().startswith("Backed up"), timeout=5000)

    assert reads == [HOSTED_URL]
    assert len(list(local_folder.glob("projects-*.json"))) == 1
    assert button.isEnabled()
    assert tab.findChild(QTableWidget, "projects-table") is not None
