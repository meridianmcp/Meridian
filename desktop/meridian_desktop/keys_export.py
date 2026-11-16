"""The names-only export of the Keys block: one JSON file that lists each key name and its classification, and no value.

The file is EXPORT_NAME (meridian-keys-backup.json) in the backups folder beside settings.json, the same folder that
projects_backup uses. It holds the names the Keys block lists: the keychain names, then the names from the keys file the
owner chose that the keychain does not also hold. Each row has the name, its source and its classification
(keys_file.is_classified). No value is read or written here, so a value cannot reach the file.

The write goes to a temp file first and then replaces the earlier export, so a failed or interrupted write never leaves a
half-written export behind. Pure Python: no Qt import.
"""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path

from meridian_desktop.api_keys import slug
from meridian_desktop.keys_file import is_classified

EXPORT_NAME = "meridian-keys-backup.json"
EXPORT_VERSION = 1
SOURCE_KEYCHAIN = "keychain"
SOURCE_KEYS_FILE = "keys_file"
CLASSIFIED = "classified"
UNCLASSIFIED = "unclassified"
_JSON_TIME = "%Y-%m-%dT%H:%M:%SZ"


def export_rows(keychain_names: Iterable[str], file_names: Iterable[str]) -> list[dict[str, str]]:
    """One row for each name: the keychain names first, then each file name the keychain does not also hold. Names only."""
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for source, names in ((SOURCE_KEYCHAIN, keychain_names), (SOURCE_KEYS_FILE, file_names)):
        for name in names:
            key = slug(name)
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                {
                    "name": name,
                    "source": source,
                    "classification": CLASSIFIED if is_classified(name) else UNCLASSIFIED,
                }
            )
    return rows


def export_document(keychain_names: Iterable[str], file_names: Iterable[str], *, taken_at: datetime) -> dict:
    """The whole export: its version, when it was taken in UTC, and the rows. It holds no value."""
    return {
        "version": EXPORT_VERSION,
        "exported_at": taken_at.astimezone(timezone.utc).strftime(_JSON_TIME),
        "keys": export_rows(keychain_names, file_names),
    }


def write_names_export(
    folder: Path,
    keychain_names: Iterable[str],
    file_names: Iterable[str],
    *,
    taken_at: datetime,
) -> Path:
    """Write the names-only export as EXPORT_NAME in ``folder``, replacing an earlier export. Return its path.

    The text goes to a temp file first, which then replaces the export in one step. Raise OSError when the folder or the
    file cannot be written; the temp file is removed then, and an earlier export is left as it was.
    """
    document = export_document(keychain_names, file_names, taken_at=taken_at)
    text = json.dumps(document, ensure_ascii=False, indent=2)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / EXPORT_NAME
    temp = folder / (EXPORT_NAME + ".tmp")
    try:
        # Flush and sync the temp file before the replace, as settings_store.save does, so the export on disk is whole
        # before it takes the name of the earlier one.
        with temp.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp, path)
    except OSError:
        with contextlib.suppress(OSError):
            temp.unlink(missing_ok=True)
        raise
    return path


__all__ = [
    "CLASSIFIED",
    "EXPORT_NAME",
    "SOURCE_KEYCHAIN",
    "SOURCE_KEYS_FILE",
    "UNCLASSIFIED",
    "export_document",
    "export_rows",
    "write_names_export",
]
