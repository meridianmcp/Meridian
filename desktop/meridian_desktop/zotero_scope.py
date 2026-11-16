"""Which Zotero library and collection each Meridian project searches. Kept on this computer only.

A Zotero collection key only means something in the Zotero that issued it, so the binding is a desktop file beside the
settings file. It is never sent to the server, and the server never reads it. A project with no entry searches the whole
library. Nothing here writes to Zotero.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from meridian_desktop.settings_store import _replace_with_retry, default_settings_path

SCOPE_FILE_NAME = "zotero_project_scopes.json"
BACKUP_SUFFIX = ".unreadable.bak"
FILE_VERSION = 1
MAX_PROJECT_ID = 200
MAX_NAME = 300
LIBRARY_USER = "user"
LIBRARY_GROUP = "group"
UNREADABLE_TEXT = "The Zotero scope file could not be read, so nothing was saved. A copy of it was kept beside it."
UNREADABLE_NO_COPY_TEXT = "The Zotero scope file could not be read, and no copy could be kept. Nothing was saved."
INVALID_TEXT = "That project, library or collection is not valid. Nothing was saved."
SKIPPED_TEXT = (
    "{} saved scope(s) in the Zotero scope file are not valid, so they are not used. They stay in the file as they were, "
    "so fix them by hand."
)
_LIBRARY_ID = re.compile(r"[0-9]{1,12}")
_COLLECTION_KEY = re.compile(r"[A-Z0-9]{8}")


class ScopeError(Exception):
    """A scope that cannot be saved, or a file that cannot be written safely. ``message`` is the owner's text."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class ProjectScope:
    """One project's binding: the library (the user library, or a group) and the collection inside it."""

    project_id: str
    library_type: str
    library_id: str
    library_name: str
    collection_key: str
    collection_name: str

    def __post_init__(self) -> None:
        if not _valid(self):
            raise ScopeError(INVALID_TEXT)


def _valid(scope: ProjectScope) -> bool:
    """Every field must be the right type and shape. A user library is always id "0"; a group id is digits."""
    if not isinstance(scope.project_id, str) or not scope.project_id or len(scope.project_id) > MAX_PROJECT_ID:
        return False
    if scope.project_id != scope.project_id.strip():
        return False
    if scope.library_type == LIBRARY_USER:
        library_ok = scope.library_id == "0"
    elif scope.library_type == LIBRARY_GROUP:
        library_ok = isinstance(scope.library_id, str) and _LIBRARY_ID.fullmatch(scope.library_id) is not None
    else:
        library_ok = False
    if not library_ok:
        return False
    if not isinstance(scope.collection_key, str) or _COLLECTION_KEY.fullmatch(scope.collection_key) is None:
        return False
    names = (scope.library_name, scope.collection_name)
    return all(isinstance(name, str) and len(name) <= MAX_NAME for name in names)


def default_path() -> Path:
    """The scope file beside the desktop's settings file, in the desktop state folder."""
    return default_settings_path().parent / SCOPE_FILE_NAME


@dataclass(frozen=True)
class ScopeRead:
    """What the file held. ``warning`` is the owner's text when entries are not valid or the file could not be read.

    ``skipped`` names the projects whose entries are not valid. The file still says those projects have a folder, so a
    save for one of them is refused rather than sent to the bridge's default collection. ``raw_skipped`` holds those
    entries exactly as the file has them, so a write can put them back.
    """

    scopes: dict[str, ProjectScope]
    warning: str
    unreadable: bool
    skipped: frozenset[str] = frozenset()
    raw_skipped: dict[str, Any] = field(default_factory=dict)


def _from_entry(project_id: str, entry: Any) -> ProjectScope | None:
    if not isinstance(entry, dict):
        return None
    try:
        return ProjectScope(
            project_id=project_id,
            library_type=entry.get("library_type"),
            library_id=entry.get("library_id"),
            library_name=entry.get("library_name"),
            collection_key=entry.get("collection_key"),
            collection_name=entry.get("collection_name"),
        )
    except ScopeError:
        return None


def _parse(raw: bytes) -> tuple[dict[str, ProjectScope], frozenset[str], dict[str, Any]] | None:
    """The valid scopes, the project ids of the entries that are not valid, and those entries as the file has them.

    None when the file is not a scope file at all.
    """
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(value, dict) or not isinstance(value.get("scopes"), dict):
        return None
    scopes: dict[str, ProjectScope] = {}
    skipped: dict[str, Any] = {}
    for project_id, entry in value["scopes"].items():
        scope = _from_entry(project_id, entry)
        if scope is None:
            skipped[project_id] = entry
        else:
            scopes[project_id] = scope
    return scopes, frozenset(skipped), skipped


def read_scopes(path: Path | None = None) -> ScopeRead:
    """Read the scope file. A missing file means no scopes. An unreadable file is said to be unreadable, never guessed at."""
    target = default_path() if path is None else path
    try:
        raw = target.read_bytes()
    except FileNotFoundError:
        return ScopeRead({}, "", False)
    except OSError:
        return ScopeRead({}, UNREADABLE_TEXT, True)
    parsed = _parse(raw)
    if parsed is None:
        return ScopeRead({}, UNREADABLE_TEXT, True)
    scopes, skipped, raw = parsed
    return ScopeRead(scopes, SKIPPED_TEXT.format(len(skipped)) if skipped else "", False, skipped, raw)


def _serialize(scopes: dict[str, ProjectScope], kept: dict[str, Any]) -> bytes:
    """The file's text. ``kept`` holds entries that were not valid, written back exactly as they were."""
    entries: dict[str, Any] = {
        project_id: {
            "library_type": scope.library_type,
            "library_id": scope.library_id,
            "library_name": scope.library_name,
            "collection_key": scope.collection_key,
            "collection_name": scope.collection_name,
        }
        for project_id, scope in scopes.items()
    }
    for project_id, raw in kept.items():
        entries.setdefault(project_id, raw)
    body = {"version": FILE_VERSION, "scopes": entries}
    return (json.dumps(body, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _atomic_write(target: Path, data: bytes) -> None:
    """Write beside ``target`` and replace it, so a failed write never leaves half a file."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        _replace_with_retry(temp, target)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def _keep_copy(target: Path) -> bool:
    """Copy the file aside before a write touches it. False when the copy could not be made."""
    try:
        shutil.copyfile(target, target.with_name(target.name + BACKUP_SUFFIX))
    except OSError:
        return False
    return True


def _change(
    path: Path | None,
    change: Callable[[dict[str, ProjectScope]], None],
    forget: str | None = None,
) -> ScopeRead:
    target = default_path() if path is None else path
    current = read_scopes(target)
    if current.unreadable:
        raise ScopeError(UNREADABLE_TEXT if _keep_copy(target) else UNREADABLE_NO_COPY_TEXT)
    scopes = dict(current.scopes)
    change(scopes)
    # Entries that are not valid are written back as they were, so the file still says their projects have a folder. A
    # change that binds one of those projects replaces its entry, as the person asked. A clear of one of those projects
    # removes its entry too (``forget``), since the person asked for that project to have no folder.
    kept = {
        project_id: raw
        for project_id, raw in current.raw_skipped.items()
        if project_id not in scopes and project_id != forget
    }
    _atomic_write(target, _serialize(scopes, kept))
    return ScopeRead(scopes, SKIPPED_TEXT.format(len(kept)) if kept else "", False, frozenset(kept), kept)


def save_scope(scope: ProjectScope, path: Path | None = None) -> ScopeRead:
    """Bind one project to a collection. Every other project's binding is kept as it was."""

    def put(scopes: dict[str, ProjectScope]) -> None:
        scopes[scope.project_id] = scope

    return _change(path, put)


def clear_scope(project_id: str, path: Path | None = None) -> ScopeRead:
    """Remove one project's binding, so it searches the whole library again."""

    def drop(scopes: dict[str, ProjectScope]) -> None:
        scopes.pop(project_id, None)

    return _change(path, drop, forget=project_id)
