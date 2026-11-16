"""Reading the backups folder: one bad entry is skipped, and one bad file cannot break pruning.

The stat call is made to raise for one entry, and a file is nested deeper than the JSON parser allows. Each test uses its
own LOCALAPPDATA folder, so no backup is left in the session's folder for another test to read.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from meridian_desktop import projects_backup

TAKEN = datetime(2026, 10, 24, 23, 54, 24, tzinfo=timezone.utc)
HOSTED_ROWS = [
    {"id": "h-1", "name": "Hosted one", "created_at": "2026-03-02T17:30:00"},
    {"id": "h-2", "name": "Hosted two", "created_at": "2026-04-01T09:00:00"},
]
FULL_GLOB = "projects-*.json"


def test_one_unreadable_entry_is_skipped_and_the_rest_of_the_folder_is_still_listed(tmp_path, monkeypatch):
    folder = tmp_path / "b"
    for day in range(3):
        projects_backup.write_backup(HOSTED_ROWS, folder=folder, taken_at=TAKEN + timedelta(days=day))
    names = sorted(path.name for path in folder.glob(FULL_GLOB))
    unreadable = names[0]
    real_is_file = type(folder).is_file

    def stat_refused(self):
        if self.name == unreadable:
            raise PermissionError("stat refused for this one entry")
        return real_is_file(self)

    monkeypatch.setattr(type(folder), "is_file", stat_refused)

    listed = projects_backup._backup_files(folder)

    assert sorted(path.name for path in listed) == names[1:]


def test_a_deeply_nested_file_cannot_break_pruning(tmp_path):
    """The newest file is nested past the JSON parser's limit. Pruning must still finish and keep the real backups."""
    folder = tmp_path / "b"
    for day in range(3):
        projects_backup.write_backup(HOSTED_ROWS, folder=folder, taken_at=TAKEN + timedelta(days=day))
    deep = folder / projects_backup.backup_name(TAKEN + timedelta(days=10))
    deep.write_text("[" * 200_000 + "]" * 200_000, encoding="utf-8")

    projects_backup.prune_backups(folder)

    names = sorted(path.name for path in folder.iterdir())
    assert deep.name in names
    assert len(names) == 4
