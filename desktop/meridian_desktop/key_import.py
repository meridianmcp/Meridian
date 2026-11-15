"""Import keys from a password-protected vault file into this computer's keychain. Pure Python: no Qt import.

The vault file is the one key_vault.py writes. Its values reach the keychain only through ApiKeys.save, and only for the
names the owner ticked, so nothing else is written. A name that already has a value is kept unless the caller asks to
replace it. This module never calls set_user_environment, so the Windows user variables are not written here.

Only counts are logged. No name, value or password is put in a log line, and a refusal reason is a fixed sentence, so
no value can reach the screen through one.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass

from meridian_desktop.api_keys import STATUS_NOT_SET, ApiKeys, KeyNameError, clean_name, slug
from meridian_desktop.key_vault import Vault
from meridian_desktop.secret_store import MAX_SECRET_LENGTH, SecretStoreError

log = logging.getLogger(__name__)

DUPLICATE_TEXT = "The same key appears twice in the vault, and one of its names was already imported in this run."
NO_VALUE_TEXT = "The vault holds no value for this name, so nothing was imported."
VALUE_REFUSED_TEXT = f"The value is empty or longer than {MAX_SECRET_LENGTH} characters, so this key was not imported."
NOT_SAVED_TEXT = "The keychain could not be written, so this key was not imported."


@dataclass(frozen=True)
class Refusal:
    """A vault name that was not imported, and the fixed sentence that says why."""

    name: str
    reason: str


@dataclass(frozen=True)
class ImportResult:
    """What an import did, by vault name. ``imported`` holds each name as the keychain list shows it."""

    imported: tuple[str, ...] = ()
    kept: tuple[str, ...] = ()
    refused: tuple[Refusal, ...] = ()
    skipped: tuple[str, ...] = ()


def is_already_saved(keys: ApiKeys, name: str) -> bool:
    """True when a value is already saved for this name, or the keychain cannot be read for it. Never a value.

    A name that cannot be a key name is False here, and import_checked refuses it.
    """
    try:
        cleaned = clean_name(name)
    except KeyNameError:
        return False
    return keys.status(cleaned) != STATUS_NOT_SET


def import_checked(keys: ApiKeys, vault: Vault, checked: Iterable[str], *, replace: bool) -> ImportResult:
    """Save the ticked names of an open vault to the keychain, in the vault's own order.

    An unticked name is skipped. A name that already has a value is kept, unless ``replace`` is True. A name that cannot be a
    key name is refused, and so is a second vault name for the same key. A keychain write that fails is refused with a fixed
    sentence. The vault is not changed by this call, and it stays open for the caller to lock.
    """
    ticked = set(checked)
    imported: list[str] = []
    kept: list[str] = []
    refused: list[Refusal] = []
    skipped: list[str] = []
    seen: set[str] = set()
    for name in vault.names():
        if name not in ticked:
            skipped.append(name)
            continue
        try:
            cleaned = clean_name(name)
        except KeyNameError as exc:
            refused.append(Refusal(name, str(exc)))
            continue
        key = slug(cleaned)
        if key in seen:
            refused.append(Refusal(name, DUPLICATE_TEXT))
            continue
        seen.add(key)
        if not replace and is_already_saved(keys, cleaned):
            kept.append(name)
            continue
        value = vault.get(name)
        if not value or not value.strip():
            refused.append(Refusal(name, NO_VALUE_TEXT))
            continue
        try:
            listed = keys.save(cleaned, value)
        except KeyNameError as exc:
            refused.append(Refusal(name, str(exc)))
        except ValueError:
            refused.append(Refusal(name, VALUE_REFUSED_TEXT))
        except (OSError, SecretStoreError):
            refused.append(Refusal(name, NOT_SAVED_TEXT))
        else:
            imported.append(listed)
    log.info(
        "Key vault import: %d saved, %d kept, %d refused, %d not ticked.",
        len(imported),
        len(kept),
        len(refused),
        len(skipped),
    )
    return ImportResult(tuple(imported), tuple(kept), tuple(refused), tuple(skipped))


__all__ = [
    "DUPLICATE_TEXT",
    "ImportResult",
    "NOT_SAVED_TEXT",
    "NO_VALUE_TEXT",
    "Refusal",
    "VALUE_REFUSED_TEXT",
    "import_checked",
    "is_already_saved",
]
