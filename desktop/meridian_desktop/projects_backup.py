"""Backup of the hosted projects to this computer. Pure Python: no Qt import.

A backup is one JSON file, ``projects-<UTC time>Z.json``, in the ``backups`` folder beside settings.json: that is
%LOCALAPPDATA%\\meridian-desktop\\backups, or ~/.meridian-desktop/backups when LOCALAPPDATA is unset. It holds only each
project's id, name and created_at, and the time it was taken. It never holds the token, the hosted URL, or any other
field of the hosted reply.

backup_hosted is the one write, and it runs in one pass. It reads the whole hosted project list through the list
function it is given (the app passes the hosted client's GET /projects), and writes one file that holds every project
in that list. A failed read writes nothing, so a failure never removes a backup. A read that gives no usable project
(an empty list, or no row with an id) writes nothing either, and raises NoUsableProjects: an empty answer never stands
in for a backup that holds projects. After a successful write the newest BACKUPS_KEPT backup files are kept, and so is
the newest backup that holds projects, even when newer files hold none (prune_backups). Older backup files are removed.

Nothing in this module sends anything to the hosted service, and nothing reads a backup back.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from meridian_desktop.settings_store import default_settings_path

BACKUP_DIR_NAME = "backups"
BACKUP_VERSION = 1
# After a write, the newest this many backup files are kept, by name. The newest backup that holds projects is kept too.
BACKUPS_KEPT = 5
# The file name, and the time inside it, both in UTC. The time is fixed width, so names sort oldest to newest.
_NAME = re.compile(r"projects-(\d{8}T\d{6})Z\.json")
_NAME_TIME = "%Y%m%dT%H%M%S"
_JSON_TIME = "%Y-%m-%dT%H:%M:%SZ"
# The only fields a backup keeps for a project.
KEPT_FIELDS = ("id", "name", "created_at")

_MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)

NO_TOKEN_TEXT = (
    "No hosted token is saved, so there are no hosted projects to back up. "
    "Paste your hosted token in Settings and press Save token."
)
NO_PROJECTS_TEXT = "Hosted Meridian returned no projects, so no backup was written. Earlier backups are kept."

ListHosted = Callable[[str, str], list[dict[str, Any]]]  # (hosted URL, token) -> the hosted client's project rows


@dataclass(frozen=True)
class BackupWritten:
    """A backup that was written: its file, how many projects it holds, its time, and the status line for it."""

    path: Path
    count: int
    taken_at: datetime
    message: str


class NoUsableProjects(Exception):
    """The rows hold no project with a usable id, so there is nothing to back up. Nothing is written or removed.

    The message is NO_PROJECTS_TEXT, the status line the tab shows for it.
    """

    def __init__(self) -> None:
        super().__init__(NO_PROJECTS_TEXT)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(when: datetime) -> datetime:
    """``when`` as an aware UTC time. A naive time is taken as UTC, so no local zone is guessed."""
    if when.tzinfo is None:
        return when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc)


def day_text(when: datetime) -> str:
    """The UTC day in plain words, such as 24 October 2026."""
    utc = _utc(when)
    return f"{utc.day} {_MONTHS[utc.month - 1]} {utc.year}"


def backup_dir(env: Mapping[str, str] | None = None, home: Path | None = None) -> Path:
    """The backups folder: beside settings.json, so it follows the same LOCALAPPDATA rule as the settings file."""
    return default_settings_path(env, home).parent / BACKUP_DIR_NAME


def backup_name(taken_at: datetime) -> str:
    return "projects-" + _utc(taken_at).strftime(_NAME_TIME) + "Z.json"


def backed_up_text(count: int, taken_at: datetime) -> str:
    noun = "project" if count == 1 else "projects"
    return f"Backed up {count} {noun} on {day_text(taken_at)}."


NEVER_BACKED_UP_TEXT = "Never backed up"


def backup_time_text(when: datetime) -> str:
    """The UTC time in plain words for the table, such as 2 Nov 2026 08:25 UTC."""
    utc = _utc(when)
    return f"{utc.day} {_MONTHS[utc.month - 1][:3]} {utc.year} {utc:%H:%M} UTC"


def destination_text(folder: Path) -> str:
    """The line that states where backups are written, shown before a backup is run."""
    return f"Backups are saved to: {folder}"


def _text_or_none(value: Any) -> str | None:
    if value is None or isinstance(value, str):
        return value
    return str(value)


def _kept_row(row: Any) -> dict[str, Any] | None:
    """The KEPT_FIELDS of one project row, or None when the row has no usable id."""
    if not isinstance(row, Mapping):
        return None
    pid = row.get("id")
    if not isinstance(pid, str) or not pid.strip():
        return None
    return {"id": pid, "name": _text_or_none(row.get("name")), "created_at": _text_or_none(row.get("created_at"))}


def write_backup(rows: list[Any], *, folder: Path, taken_at: datetime) -> BackupWritten:
    """Write the rows as the newest backup in ``folder``, then prune the backup files (prune_backups).

    Only the KEPT_FIELDS of each row go in, and a row with no usable id is left out. When no row is left, nothing is
    written or removed, and NoUsableProjects is raised. The message names the file the backup was saved to.
    """
    taken = _utc(taken_at)
    projects = [kept for kept in (_kept_row(row) for row in rows) if kept is not None]
    if not projects:
        raise NoUsableProjects()
    folder.mkdir(parents=True, exist_ok=True)
    # A backup never replaces an existing file: a second write in the same UTC second takes the next free second's name.
    while (folder / backup_name(taken)).exists():
        taken = taken + timedelta(seconds=1)
    document = {"version": BACKUP_VERSION, "taken_at": taken.strftime(_JSON_TIME), "projects": projects}
    path = folder / backup_name(taken)
    temp = folder / (path.name + ".tmp")
    temp.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)
    prune_backups(folder, keep=path)
    message = f"{backed_up_text(len(projects), taken)} Saved to: {path}"
    return BackupWritten(path, len(projects), taken, message)


def backup_hosted(
    list_hosted: ListHosted,
    hosted_url: str,
    token: str,
    *,
    folder: Path,
    taken_at: datetime,
    only_ids: Collection[str] | None = None,
) -> BackupWritten:
    """Read the hosted project list with ``list_hosted`` and write it as the newest backup, in one run.

    ``only_ids`` limits the backup to those project ids. None means every project in the list. A failed read raises
    before anything is written. An answer with no usable project raises NoUsableProjects, and writes and removes
    nothing. The token is passed to ``list_hosted`` and goes nowhere else.
    """
    rows = list_hosted(hosted_url, token)
    if only_ids is not None:
        rows = [row for row in rows if isinstance(row, Mapping) and row.get("id") in only_ids]
    return write_backup(rows, folder=folder, taken_at=taken_at)


def last_backed_up(folder: Path) -> dict[str, datetime]:
    """The newest backup time that each project id appears in, read from the backup files in ``folder``. Reads only.

    Files are read newest first, so the first time seen for an id is its newest backup. A file that cannot be read
    is skipped, and the rest are still read.
    """
    found: dict[str, datetime] = {}
    for path in _backup_files(folder):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            taken = datetime.strptime(document["taken_at"], _JSON_TIME).replace(tzinfo=timezone.utc)
            rows = document["projects"]
        except (OSError, ValueError, KeyError, TypeError, RecursionError):
            continue
        if not isinstance(rows, list):
            continue
        for row in rows:
            kept = _kept_row(row)
            if kept is not None and kept["id"] not in found:
                found[kept["id"]] = taken
    return found


def _backup_files(folder: Path) -> list[Path]:
    """The backup files in ``folder``, newest first. Only names this module writes are listed.

    Each entry is checked on its own. An entry that cannot be stat'ed is skipped, and the rest of the folder is still listed.
    """
    try:
        entries = list(folder.iterdir())
    except OSError:
        return []
    found: list[Path] = []
    for path in entries:
        if not _NAME.fullmatch(path.name):
            continue
        try:
            if path.is_file():
                found.append(path)
        except OSError:
            continue
    return sorted(found, key=lambda path: path.name, reverse=True)


def _holds_projects(path: Path) -> bool:
    """True when the backup in ``path`` parses and holds at least one project with a usable id."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        projects = document["projects"]
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        # A file nested past the parser's limit raises RecursionError. It holds no project, so it is not the one to keep.
        return False
    if not isinstance(projects, list):
        return False
    return any(_kept_row(row) is not None for row in projects)


def prune_backups(folder: Path, keep: Path | None = None) -> None:
    """Remove the backup files that retention does not keep, and leave every other file as it is.

    Kept: the newest BACKUPS_KEPT backup files by name, the newest backup that holds projects (even when newer files
    hold none), and ``keep``, the file just written. A file that cannot be removed is left as it is.
    """
    files = _backup_files(folder)  # newest first
    kept = set(files[:BACKUPS_KEPT])
    newest_with_projects = next((path for path in files if _holds_projects(path)), None)
    if newest_with_projects is not None:
        kept.add(newest_with_projects)
    if keep is not None:
        kept.add(keep)
    for path in files:
        if path not in kept:
            try:
                path.unlink()
            except OSError:
                pass


__all__ = [
    "BACKUP_DIR_NAME",
    "BACKUPS_KEPT",
    "KEPT_FIELDS",
    "NEVER_BACKED_UP_TEXT",
    "NO_PROJECTS_TEXT",
    "NO_TOKEN_TEXT",
    "BackupWritten",
    "NoUsableProjects",
    "backed_up_text",
    "backup_dir",
    "backup_hosted",
    "backup_name",
    "backup_time_text",
    "day_text",
    "destination_text",
    "last_backed_up",
    "prune_backups",
    "utc_now",
    "write_backup",
]
