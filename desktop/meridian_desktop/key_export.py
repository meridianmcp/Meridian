"""Export every saved key for migration: an encrypted vault file (the default) or a plaintext JSON file. Pure Python: no Qt.

Both formats hold each listed key that has a keychain value, and no other key. The encrypted file is the one key_vault.py
writes, so Import keys from a vault file (key_import.py) reads it back with the same passphrase. The plaintext file is JSON
with each name and its value in readable form. Nothing in this app reads it back, and nothing here protects it.

A target is refused when its folder does not exist, when the folder or one of its parents holds a ``.git`` entry (git is never
run), when the name is a folder, or when a file already exists and the caller did not confirm replacing it. Every check runs
before a keychain value is read. A file is written to a temporary name beside the target and then moved over it, so a failed
write leaves the old file in place.

Only counts are logged. No name, value, passphrase or path is put in a log line, and every refusal is a fixed sentence, so
no value can reach the screen through one.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from meridian_desktop import key_vault
from meridian_desktop.api_keys import STATUS_SET, ApiKeys
from meridian_desktop.key_vault import MIN_PASSWORD_LENGTH, VaultError

log = logging.getLogger(__name__)

KIND_ENCRYPTED = "encrypted"
KIND_PLAINTEXT = "plaintext"
# The word the owner must type before a plaintext file is written. The match is exact: case and spaces count.
PLAINTEXT_CONFIRMATION = "PLAINTEXT"
GIT_ENTRY = ".git"
TEMP_SUFFIX = ".export-tmp"
PLAINTEXT_FORMAT = "meridian-keys-plaintext"
PLAINTEXT_VERSION = 1
PLAINTEXT_NOTE = "Every value in this file is readable. Anyone who can open the file can read every key."
# The names the save dialog starts with. They are plain, so the owner can tell the two files apart at a glance.
DEFAULT_FILE_NAMES: dict[str, str] = {
    KIND_ENCRYPTED: "meridian-keys-export-encrypted.json",
    KIND_PLAINTEXT: "meridian-keys-export-plaintext.json",
}

UNKNOWN_KIND_TEXT = "Choose either the encrypted or the plaintext format. Nothing was written."
PASSPHRASE_TEXT = f"Use a passphrase of at least {MIN_PASSWORD_LENGTH} characters. Nothing was written."
NOT_CONFIRMED_TEXT = "A plaintext file needs the word PLAINTEXT typed to confirm. Nothing was written."
NO_FOLDER_TEXT = "The folder for that file does not exist. Choose a folder that exists. Nothing was written."
GIT_TEXT = "That folder is inside a git repository, so nothing was written there. Choose a folder outside any repository."
CHECK_FAILED_TEXT = "The folder could not be checked for a git repository, so nothing was written."
IS_FOLDER_TEXT = "That name is a folder, so nothing was written. Choose a file name."
EXISTS_TEXT = "A file with that name already exists, so it was not replaced. Confirm replacing it to write over it."
NONE_TEXT = "No key has a saved value, so there is nothing to export. Nothing was written."
UNREADABLE_TEXT = "The keychain could not be read for one key, so nothing was exported. Nothing was written."
NOT_WRITTEN_TEXT = "The file could not be written. Nothing was exported, and any older file was kept."


class ExportError(Exception):
    """An export that was refused or failed. The message is one fixed sentence and holds no value, name or passphrase."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


class TargetExistsError(ExportError):
    """The target file already exists and the caller did not confirm replacing it. Nothing was written."""


@dataclass(frozen=True)
class ExportResult:
    """Where an export was written, how many keys it holds, and which format it is in. It holds no value."""

    path: Path
    count: int
    kind: str


def count_saved(keys: ApiKeys) -> int:
    """How many listed keys have a saved keychain value. Only the status is read, so no value is held here."""
    return sum(1 for name in keys.names() if keys.status(name) == STATUS_SET)


def inside_git_repository(folder: Path) -> bool:
    """True when ``folder`` or one of its parents holds a ``.git`` entry, as a folder or as a file. Git is never run.

    A failure to look at a folder raises OSError, which export_keys turns into a refusal.
    """
    start = folder.resolve()
    return any((candidate / GIT_ENTRY).exists() for candidate in (start, *start.parents))


def export_keys(
    keys: ApiKeys,
    target: Path,
    kind: str,
    *,
    passphrase: str = "",
    confirmation: str = "",
    overwrite: bool = False,
) -> ExportResult:
    """Write every listed key that has a keychain value to ``target``, in the format ``kind``.

    An encrypted export needs a ``passphrase`` of at least MIN_PASSWORD_LENGTH characters. A plaintext export needs
    ``confirmation`` to be exactly PLAINTEXT_CONFIRMATION. An existing file raises TargetExistsError unless ``overwrite`` is
    True. A refusal raises ExportError with a fixed sentence, and nothing is written. Raises nothing else.
    """
    if kind not in (KIND_ENCRYPTED, KIND_PLAINTEXT):
        raise ExportError(UNKNOWN_KIND_TEXT)
    if kind == KIND_PLAINTEXT and confirmation != PLAINTEXT_CONFIRMATION:
        raise ExportError(NOT_CONFIRMED_TEXT)
    if kind == KIND_ENCRYPTED:
        _check_passphrase(passphrase)
    path = Path(target)
    _check_target(path, overwrite=overwrite)
    values = _saved_values(keys)
    if not values:
        raise ExportError(NONE_TEXT)
    if kind == KIND_ENCRYPTED:
        _write_vault(path, values, passphrase, overwrite=overwrite)
    else:
        _write_plaintext(path, values, overwrite=overwrite)
    log.info("Key export: %d keys written as %s.", len(values), kind)
    return ExportResult(path, len(values), kind)


def _check_passphrase(passphrase: object) -> None:
    if not isinstance(passphrase, str) or len(passphrase) < MIN_PASSWORD_LENGTH:
        raise ExportError(PASSPHRASE_TEXT)


def _check_target(path: Path, *, overwrite: bool) -> None:
    """Refuse a target that cannot be written safely. Every check here runs before any keychain value is read."""
    try:
        if not path.parent.is_dir():
            raise ExportError(NO_FOLDER_TEXT)
        if inside_git_repository(path.parent):
            raise ExportError(GIT_TEXT)
        if path.is_dir():
            raise ExportError(IS_FOLDER_TEXT)
        if path.exists() and not overwrite:
            raise TargetExistsError(EXISTS_TEXT)
    except OSError:
        raise ExportError(CHECK_FAILED_TEXT) from None


def _saved_values(keys: ApiKeys) -> dict[str, str]:
    """Each listed name with a keychain value, and its value. A name with no value is left out.

    A keychain read that fails stops the whole export, so the file never holds fewer keys than the keychain does.
    """
    values: dict[str, str] = {}
    for name in keys.names():
        try:
            value = keys.read_value(name)
        except Exception:  # noqa: BLE001 - the step failed and its text may hold detail, so it is never shown or chained
            raise ExportError(UNREADABLE_TEXT) from None
        if value:
            values[name] = value
    return values


def _temp_path(target: Path) -> Path:
    return target.with_name(target.name + TEMP_SUFFIX)


def _remove_quietly(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        return


def _move_into_place(temp: Path, target: Path, *, overwrite: bool) -> None:
    """Move the finished temporary file over the target. An overwrite the owner did not confirm is refused here too,
    because a file can appear at the target after the first check, while the keys are being read."""
    if not overwrite and target.exists():
        raise TargetExistsError(EXISTS_TEXT)
    os.replace(temp, target)


def _write_vault(target: Path, values: dict[str, str], passphrase: str, *, overwrite: bool) -> None:
    """Write the encrypted file with the vault code, to a temporary name, then move it over the target."""
    temp = _temp_path(target)
    _remove_quietly(temp)
    try:
        vault = key_vault.create(temp, passphrase)
        for name, value in values.items():
            vault.put(name, value)
        vault.save()
        vault.lock()
        _move_into_place(temp, target, overwrite=overwrite)
    except TargetExistsError:
        _remove_quietly(temp)
        raise
    except (VaultError, OSError):
        _remove_quietly(temp)
        _remove_quietly(temp.with_name(temp.name + ".tmp"))
        raise ExportError(NOT_WRITTEN_TEXT) from None


def _write_plaintext(target: Path, values: dict[str, str], *, overwrite: bool) -> None:
    """Write the plaintext JSON file to a temporary name, then move it over the target."""
    document: dict[str, Any] = {
        "format": PLAINTEXT_FORMAT,
        "version": PLAINTEXT_VERSION,
        "note": PLAINTEXT_NOTE,
        "keys": [{"name": name, "value": value} for name, value in values.items()],
    }
    temp = _temp_path(target)
    _remove_quietly(temp)
    moved = False
    try:
        with open(temp, "x", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        _move_into_place(temp, target, overwrite=overwrite)
        moved = True
    except TargetExistsError:
        raise
    except OSError:
        raise ExportError(NOT_WRITTEN_TEXT) from None
    finally:
        if not moved:
            _remove_quietly(temp)


__all__ = [
    "DEFAULT_FILE_NAMES",
    "EXISTS_TEXT",
    "ExportError",
    "ExportResult",
    "GIT_TEXT",
    "KIND_ENCRYPTED",
    "KIND_PLAINTEXT",
    "NONE_TEXT",
    "NOT_CONFIRMED_TEXT",
    "PLAINTEXT_CONFIRMATION",
    "TargetExistsError",
    "UNREADABLE_TEXT",
    "count_saved",
    "export_keys",
    "inside_git_repository",
]
