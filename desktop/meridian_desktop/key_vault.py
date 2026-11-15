"""The keys vault: the critical keys' values in one file, encrypted with a password. Pure Python apart from AES-GCM.

The vault is keys-vault.json, beside settings.json. Its file holds only a format version, the scrypt salt and parameters,
a fresh nonce for each save, and the ciphertext. The plaintext is one JSON object of name to value, and it is never written
to disk. The key is scrypt(password, salt), 32 bytes, from the standard library. AES-256-GCM encrypts the plaintext, and
the header (version, scrypt parameters and salt) is the authenticated data, so changing the header fails the same way as
changing the ciphertext.

Nothing is read at start. The vault opens only when the owner enters the password, and it stays open in memory until Lock
or the end of the session. A forgotten password cannot be recovered: nothing else holds the key, so the keys must be
entered again from their source. A wrong password and a changed file both raise VaultError with one fixed sentence. Neither
message holds the password or any value.

Every save writes a temporary file, flushes it to disk, and replaces the vault with it, so a failed save leaves the old file.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

VAULT_FILE_NAME = "keys-vault.json"
VERSION = 1
MIN_PASSWORD_LENGTH = 12
SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_MAXMEM = 64 * 1024 * 1024
KEY_BYTES = 32
SALT_BYTES = 16
NONCE_BYTES = 12

NOT_FOUND_TEXT = "No vault exists yet. Create one first."
ALREADY_EXISTS_TEXT = "A vault already exists here, so it was not replaced."
SHORT_PASSWORD_TEXT = f"Use a password of at least {MIN_PASSWORD_LENGTH} characters."
WRONG_PASSWORD_TEXT = "That password does not open the vault, or the vault file was changed."
DAMAGED_TEXT = "The vault file could not be read. It was not changed."
SAVE_FAILED_TEXT = "The vault could not be saved. The file on disk was not changed."


class VaultError(Exception):
    """A vault that cannot be created, opened or saved. The message is one fixed sentence and holds no secret."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


@dataclass
class Vault:
    """An open vault. The derived key and the entries live only in memory. ``lock`` drops both."""

    path: Path
    _salt: bytes
    _key: bytes | None
    _entries: dict[str, str] = field(default_factory=dict)

    @property
    def is_open(self) -> bool:
        return self._key is not None

    def names(self) -> list[str]:
        """The names in the vault, in sorted order. Never a value."""
        return sorted(self._entries)

    def get(self, name: str) -> str | None:
        """The value stored under ``name``, or None. Only an open vault has values."""
        return self._entries.get(name)

    def put(self, name: str, value: str) -> None:
        """Set a value in memory. Call ``save`` to write it to the file."""
        self._require_open()
        self._entries[name] = value

    def remove(self, name: str) -> bool:
        """Drop a value in memory. Call ``save`` to write the change."""
        self._require_open()
        return self._entries.pop(name, None) is not None

    def save(self) -> None:
        """Write the open vault to its file with a fresh nonce, atomically."""
        self._require_open()
        try:
            _write(self.path, self._salt, self._key, self._entries)
        except OSError as exc:
            raise VaultError(SAVE_FAILED_TEXT) from exc

    def lock(self) -> None:
        """Forget the key and every value. The file is not changed."""
        self._key = None
        self._entries = {}

    def _require_open(self) -> None:
        if self._key is None:
            raise VaultError(WRONG_PASSWORD_TEXT)


def exists(path: Path) -> bool:
    return path.is_file()


def create(path: Path, password: str) -> Vault:
    """Create an empty vault at ``path``, protected by ``password``. An existing vault is never replaced."""
    if exists(path):
        raise VaultError(ALREADY_EXISTS_TEXT)
    _check_password(password)
    salt = secrets.token_bytes(SALT_BYTES)
    key = _derive_key(password, salt)
    vault = Vault(path=path, _salt=salt, _key=key, _entries={})
    try:
        _write(path, salt, key, {})
    except OSError as exc:
        raise VaultError(SAVE_FAILED_TEXT) from exc
    return vault


def unlock(path: Path, password: str) -> Vault:
    """Open the vault at ``path`` with ``password``. A wrong password or a changed file raises VaultError."""
    if not exists(path):
        raise VaultError(NOT_FOUND_TEXT)
    document = _read_document(path)
    salt = _decode(document, "salt")
    key = _derive_key(password, salt)
    header = _header_bytes(document["kdf"], salt)
    try:
        plaintext = AESGCM(key).decrypt(_decode(document, "nonce"), _decode(document, "ciphertext"), header)
    except InvalidTag as exc:
        raise VaultError(WRONG_PASSWORD_TEXT) from exc
    entries = _parse_entries(plaintext)
    return Vault(path=path, _salt=salt, _key=key, _entries=entries)


def _check_password(password: str) -> None:
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
        raise VaultError(SHORT_PASSWORD_TEXT)


def _derive_key(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        maxmem=SCRYPT_MAXMEM,
        dklen=KEY_BYTES,
    )


def _kdf_block() -> dict[str, Any]:
    return {"name": "scrypt", "n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P}


def _header_bytes(kdf: Mapping[str, Any], salt: bytes) -> bytes:
    """The authenticated header: the version, the scrypt parameters and the salt, in one fixed text form."""
    return json.dumps(
        {"version": VERSION, "kdf": dict(kdf), "salt": base64.b64encode(salt).decode("ascii")},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _write(path: Path, salt: bytes, key: bytes | None, entries: Mapping[str, str]) -> None:
    if key is None:
        raise VaultError(WRONG_PASSWORD_TEXT)
    kdf = _kdf_block()
    nonce = secrets.token_bytes(NONCE_BYTES)
    plaintext = json.dumps(dict(entries), sort_keys=True, ensure_ascii=False).encode("utf-8")
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, _header_bytes(kdf, salt))
    document = {
        "version": VERSION,
        "kdf": kdf,
        "salt": base64.b64encode(salt).decode("ascii"),
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def _read_document(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise VaultError(DAMAGED_TEXT) from exc
    if not isinstance(document, dict) or document.get("version") != VERSION:
        raise VaultError(DAMAGED_TEXT)
    kdf = document.get("kdf")
    if not isinstance(kdf, dict) or kdf != _kdf_block():
        raise VaultError(DAMAGED_TEXT)
    for field_name in ("salt", "nonce", "ciphertext"):
        if not isinstance(document.get(field_name), str):
            raise VaultError(DAMAGED_TEXT)
    return document


def _decode(document: Mapping[str, Any], field_name: str) -> bytes:
    try:
        return base64.b64decode(document[field_name], validate=True)
    except (KeyError, ValueError, TypeError) as exc:
        raise VaultError(DAMAGED_TEXT) from exc


def _parse_entries(plaintext: bytes) -> dict[str, str]:
    try:
        entries = json.loads(plaintext.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise VaultError(DAMAGED_TEXT) from exc
    if not isinstance(entries, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in entries.items()):
        raise VaultError(DAMAGED_TEXT)
    return dict(entries)


__all__ = [
    "ALREADY_EXISTS_TEXT",
    "DAMAGED_TEXT",
    "MIN_PASSWORD_LENGTH",
    "NOT_FOUND_TEXT",
    "SAVE_FAILED_TEXT",
    "SHORT_PASSWORD_TEXT",
    "VAULT_FILE_NAME",
    "WRONG_PASSWORD_TEXT",
    "Vault",
    "VaultError",
    "create",
    "exists",
    "unlock",
]
