"""Local secrets broker, slice 1: vault, master-key wrap, run wrapper, audit log.

What this slice does:

* Stores named items in one vault file. Each item has a sensitivity class.
  PUBLIC and INTERNAL values sit in plaintext in the plain index part of the
  file. SECRET and SENSITIVE values are AES-256-GCM encrypted under a random
  32-byte master key. Names, groups, purposes, allowed commands and classes are
  plaintext metadata. For encrypted items they are bound into the AEAD
  associated data, so editing them on disk fails the integrity check at unlock.
  An unknown or unset class is treated as SECRET.
* Wraps the master key twice: once under a passphrase and once under a one-time
  recovery code. Each wrapping key comes from hashlib.scrypt and each wrap is
  AES-256-GCM. Both wraps live in an OS credential store behind a small adapter
  interface. The passphrase and the recovery code are never written anywhere.
  The recovery wrap protects the encrypted vault only, not PUBLIC or INTERNAL items.
* Keeps on/off switches in memory only. Every secret and group is on after
  unlock. Closing the broker clears the key and turns everything off.
* ``run_with_secret`` runs one allowed command with the value in the child's
  environment only. It returns output with the value, its base64, URL-encoded
  and hex forms replaced by a token.
* Appends JSON lines to an audit log that records names, commands and outcomes,
  never values.

SENSITIVE is the high-risk approval tier: a call on a SENSITIVE item returns
``approval_required`` and does not run. Not in this slice: the desktop approval
prompt, the desktop UI, and a Linux credential store (the adapter is a stub that
raises NotImplementedError).

Handling rule: no code path here formats or logs a secret value. Exception
messages never contain a value or a passphrase. ``RunResult`` output is redacted
before it is returned.

Limits: Python cannot reliably zero memory, so clearing the in-memory key is best
effort. Redaction covers only the listed encodings; a child that transforms the
value another way can still leak it. Once a command is allowed to run with a
secret, that command is trusted with the secret.
"""

from __future__ import annotations

import base64
import contextlib
import ctypes
import functools
import hashlib
import hmac
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

FORMAT_VERSION = 1
MASTER_KEY_BYTES = 32
PASSPHRASE_MIN_LENGTH = 12
SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 1
_SCRYPT_N_MIN = 2**14
_SCRYPT_N_MAX = 2**17
_SCRYPT_P_MAX = 4
_SCRYPT_MAXMEM = 160 * 1024 * 1024

TARGET_PASSPHRASE_WRAP = "meridian-secrets-broker/master-wrap/passphrase"
TARGET_RECOVERY_WRAP = "meridian-secrets-broker/master-wrap/recovery"
VAULT_FILE_NAME = "vault.json"
AUDIT_FILE_NAME = "audit.jsonl"
SENSITIVITY_PUBLIC = "PUBLIC"
SENSITIVITY_INTERNAL = "INTERNAL"
SENSITIVITY_SECRET = "SECRET"
SENSITIVITY_SENSITIVE = "SENSITIVE"
SENSITIVITY_CLASSES = (
    SENSITIVITY_PUBLIC,
    SENSITIVITY_INTERNAL,
    SENSITIVITY_SECRET,
    SENSITIVITY_SENSITIVE,
)
PLAIN_CLASSES = frozenset({SENSITIVITY_PUBLIC, SENSITIVITY_INTERNAL})
ENCRYPTED_CLASSES = frozenset({SENSITIVITY_SECRET, SENSITIVITY_SENSITIVE})
COMMON_PASSPHRASE_FILE = Path(__file__).resolve().parent / "secrets_data" / "common_passphrase_sha256.txt"
COMMON_PASSPHRASE_MIN_DIGESTS = 1000
REDACTED_TOKEN = b"[REDACTED]"
MIN_VALUE_BYTES = 8
MAX_VALUE_BYTES = 8192
MAX_PURPOSE_CHARS = 200
DEFAULT_TIMEOUT_SECONDS = 300.0
UNLOCK_FAILED_MESSAGE = (
    "could not unwrap the master key: wrong passphrase or recovery code, "
    "or the stored wrap is damaged"
)

_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
_GROUP_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_WIN_ABSOLUTE_RE = re.compile(r"(?:[A-Za-z]:[\\/]|\\\\[^\\/]+[\\/][^\\/]+[\\/])")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")

# Variables that change how a child process loads code. A stored value must not
# be able to redirect an allowed command this way, so these names are refused.
_RESERVED_ENV_NAMES = frozenset(
    {
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "COMSPEC",
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONSTARTUP",
        "PYTHONINSPECT",
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
        "DYLD_INSERT_LIBRARIES",
        "DYLD_LIBRARY_PATH",
        "NODE_OPTIONS",
        "BASH_ENV",
        "ENV",
        "IFS",
    }
)

# Common passphrases are checked by SHA-256 digest against a bundled file. The file
# holds digests only, never plaintext. See the header of that file for its source.
def effective_sensitivity(value: object) -> str:
    """Return the class to apply. An unknown or unset class is treated as SECRET."""
    if isinstance(value, str) and value in SENSITIVITY_CLASSES:
        return value
    return SENSITIVITY_SECRET


@functools.lru_cache(maxsize=1)
def _common_passphrase_digests() -> frozenset[str]:
    """SHA-256 digests of the common-passphrase list, read once from the bundled file.

    A missing or malformed file raises BrokerError, so a broken list refuses
    passphrases instead of allowing every one.
    """
    try:
        text = COMMON_PASSPHRASE_FILE.read_text(encoding="ascii")
    except (OSError, UnicodeDecodeError):
        raise BrokerError("common-passphrase list is unavailable") from None
    digests: set[str] = set()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if not _DIGEST_RE.fullmatch(line):
            raise BrokerError("common-passphrase list is malformed")
        digests.add(line)
    if len(digests) < COMMON_PASSPHRASE_MIN_DIGESTS:
        raise BrokerError("common-passphrase list is malformed")
    return frozenset(digests)


class BrokerError(Exception):
    """Base class. Messages never include a secret value or a passphrase."""


class InvalidInput(BrokerError):
    pass


class BrokerLocked(BrokerError):
    pass


class NotInitialized(BrokerError):
    pass


class AlreadyInitialized(BrokerError):
    pass


class UnlockFailed(BrokerError):
    pass


class PassphraseRejected(BrokerError):
    pass


class UnknownSecret(BrokerError):
    pass


class SecretDisabled(BrokerError):
    pass


class CommandNotAllowed(BrokerError):
    pass


class IntegrityError(BrokerError):
    pass


class AuditUnavailable(BrokerError):
    pass


class CredentialStoreError(BrokerError):
    pass


class CredentialStore(Protocol):
    """OS credential-store interface: text in, text out, keyed by target name."""

    def read(self, target: str) -> str | None: ...

    def write(self, target: str, text: str) -> None: ...


class LinuxCredentialStoreStub:
    """STUB, NOT IMPLEMENTED. Placeholder for a Linux credential-store adapter.

    Constructing it raises NotImplementedError. Real Linux support (for example
    libsecret) is a later slice.
    """

    def __init__(self) -> None:
        raise NotImplementedError("Linux credential store adapter is not implemented in slice 1")


if sys.platform == "win32":

    class _CREDENTIALW(ctypes.Structure):
        _fields_ = [
            ("Flags", ctypes.c_uint32),
            ("Type", ctypes.c_uint32),
            ("TargetName", ctypes.c_wchar_p),
            ("Comment", ctypes.c_wchar_p),
            ("LastWritten", ctypes.c_uint64),
            ("CredentialBlobSize", ctypes.c_uint32),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
            ("Persist", ctypes.c_uint32),
            ("AttributeCount", ctypes.c_uint32),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", ctypes.c_wchar_p),
            ("UserName", ctypes.c_wchar_p),
        ]


class WindowsCredentialManager:
    """Windows Credential Manager through advapi32 CredWriteW and CredReadW.

    Follows the design's conventions: generic credential (Type=1),
    CRED_PERSIST_LOCAL_MACHINE (Persist=2), and text stored as a UTF-16LE blob.
    tools/import_secrets.ps1, named as the reference, is not in this checkout, so
    the layout follows the Win32 CREDENTIALW definition instead.

    The test suite never runs this class against the real store. It needs a manual
    round trip with a throwaway target name, which is not part of slice 1.
    """

    CRED_TYPE_GENERIC = 1
    CRED_PERSIST_LOCAL_MACHINE = 2
    ERROR_NOT_FOUND = 1168
    MAX_BLOB_BYTES = 2560

    def __init__(self) -> None:  # pragma: no cover - Windows only, real store
        if sys.platform != "win32":
            raise NotImplementedError("WindowsCredentialManager requires Windows")
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        self._write_fn = advapi32.CredWriteW
        self._write_fn.argtypes = [ctypes.POINTER(_CREDENTIALW), ctypes.c_uint32]
        self._write_fn.restype = ctypes.c_int
        self._read_fn = advapi32.CredReadW
        self._read_fn.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.POINTER(_CREDENTIALW)),
        ]
        self._read_fn.restype = ctypes.c_int
        self._free_fn = advapi32.CredFree
        self._free_fn.argtypes = [ctypes.c_void_p]
        self._free_fn.restype = None

    def write(self, target: str, text: str) -> None:  # pragma: no cover - real store
        blob = text.encode("utf-16-le")
        if len(blob) > self.MAX_BLOB_BYTES:
            raise CredentialStoreError("credential blob is too large")
        buffer = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
        cred = _CREDENTIALW()
        cred.Type = self.CRED_TYPE_GENERIC
        cred.TargetName = target
        cred.CredentialBlobSize = len(blob)
        cred.CredentialBlob = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
        cred.Persist = self.CRED_PERSIST_LOCAL_MACHINE
        if not self._write_fn(ctypes.byref(cred), 0):
            raise CredentialStoreError(f"CredWriteW failed (winerror {ctypes.get_last_error()})")

    def read(self, target: str) -> str | None:  # pragma: no cover - real store
        out = ctypes.POINTER(_CREDENTIALW)()
        if not self._read_fn(target, self.CRED_TYPE_GENERIC, 0, ctypes.byref(out)):
            code = ctypes.get_last_error()
            if code == self.ERROR_NOT_FOUND:
                return None
            raise CredentialStoreError(f"CredReadW failed (winerror {code})")
        try:
            size = int(out.contents.CredentialBlobSize)
            address = ctypes.cast(out.contents.CredentialBlob, ctypes.c_void_p).value
            raw = ctypes.string_at(address, size) if size and address else b""
        finally:
            self._free_fn(ctypes.cast(out, ctypes.c_void_p))
        return raw.decode("utf-16-le")


def default_credential_store() -> CredentialStore:
    """Return the OS credential store for this platform. Never called by tests."""
    if sys.platform == "win32":
        return WindowsCredentialManager()
    return LinuxCredentialStoreStub()  # type: ignore[return-value]  # raises


def default_vault_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "meridian" / "secrets"
    xdg = os.environ.get("XDG_DATA_HOME")
    base_path = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base_path / "meridian" / "secrets"


def check_passphrase(passphrase: object) -> None:
    """Enforce the passphrase policy. Never includes the passphrase in an error."""
    if not isinstance(passphrase, str):
        raise InvalidInput("passphrase must be a string")
    try:
        passphrase.encode("utf-8")
    except UnicodeEncodeError:
        raise PassphraseRejected("passphrase must be valid UTF-8 text") from None
    if len(passphrase.strip()) < PASSPHRASE_MIN_LENGTH:
        raise PassphraseRejected(f"passphrase must be at least {PASSPHRASE_MIN_LENGTH} characters")
    digest = hashlib.sha256(passphrase.strip().lower().encode("utf-8")).hexdigest()
    if digest in _common_passphrase_digests():
        raise PassphraseRejected("passphrase is on the common-password list")


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(text: object) -> bytes:
    if not isinstance(text, str):
        raise TypeError("expected base64 text")
    return base64.b64decode(text.encode("ascii"), validate=True)


def _scrypt_key(secret: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        secret.encode("utf-8"), salt=salt, n=n, r=r, p=p, maxmem=_SCRYPT_MAXMEM, dklen=32
    )


def _scrypt_params_ok(n: int, r: int, p: int) -> bool:
    return (
        _SCRYPT_N_MIN <= n <= _SCRYPT_N_MAX
        and (n & (n - 1)) == 0
        and r == SCRYPT_R
        and 1 <= p <= _SCRYPT_P_MAX
    )


def _wrap_aad(kind: str) -> bytes:
    return f"meridian-secrets-broker/master-wrap/v{FORMAT_VERSION}/{kind}".encode("ascii")


def _wrap_master_key(master_key: bytes, secret: str, kind: str) -> str:
    salt = secrets.token_bytes(16)
    nonce = secrets.token_bytes(12)
    kek = _scrypt_key(secret, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
    ciphertext = AESGCM(kek).encrypt(nonce, master_key, _wrap_aad(kind))
    doc = {
        "v": FORMAT_VERSION,
        "kind": kind,
        "kdf": "scrypt",
        "n": SCRYPT_N,
        "r": SCRYPT_R,
        "p": SCRYPT_P,
        "salt": _b64(salt),
        "nonce": _b64(nonce),
        "ct": _b64(ciphertext),
    }
    return json.dumps(doc, sort_keys=True, separators=(",", ":"))


def _unwrap_master_key(wrap_text: str, secret: str, kind: str) -> bytes:
    try:
        doc = json.loads(wrap_text)
        if (
            not isinstance(doc, dict)
            or doc.get("v") != FORMAT_VERSION
            or doc.get("kind") != kind
            or doc.get("kdf") != "scrypt"
        ):
            raise ValueError("unexpected wrap header")
        n, r, p = int(doc["n"]), int(doc["r"]), int(doc["p"])
        if not _scrypt_params_ok(n, r, p):
            raise ValueError("scrypt parameters out of range")
        kek = _scrypt_key(secret, _unb64(doc["salt"]), n, r, p)
        master = AESGCM(kek).decrypt(_unb64(doc["nonce"]), _unb64(doc["ct"]), _wrap_aad(kind))
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, InvalidTag):
        raise UnlockFailed(UNLOCK_FAILED_MESSAGE) from None
    if len(master) != MASTER_KEY_BYTES:
        raise UnlockFailed(UNLOCK_FAILED_MESSAGE)
    return master


def _new_recovery_code() -> str:
    """160 random bits as 8 groups of 4 base32 characters (no padding needed)."""
    raw = base64.b32encode(secrets.token_bytes(20)).decode("ascii")
    return "-".join(raw[i : i + 4] for i in range(0, len(raw), 4))


def _normalize_recovery_code(code: object) -> str:
    if not isinstance(code, str):
        raise InvalidInput("recovery code must be a string")
    return re.sub(r"[\s-]", "", code).upper()


def _check_name(name: object) -> str:
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        raise InvalidInput("secret names must match [A-Za-z_][A-Za-z0-9_]{0,127}")
    if name.upper() in _RESERVED_ENV_NAMES:
        raise InvalidInput(f"'{name}' is a reserved environment variable name")
    return name


def _check_group(group: object) -> str:
    if not isinstance(group, str) or not _GROUP_RE.fullmatch(group):
        raise InvalidInput("group must be 1-64 characters: letters, digits, '_', '.', '-'")
    return group


def _check_purpose(purpose: object) -> str:
    if not isinstance(purpose, str) or not purpose.strip() or len(purpose) > MAX_PURPOSE_CHARS:
        raise InvalidInput(f"purpose must be 1-{MAX_PURPOSE_CHARS} characters")
    return purpose.strip()


def _encode_value(value: object) -> bytes:
    if not isinstance(value, str):
        raise InvalidInput("value must be a string")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise InvalidInput("value must be valid UTF-8 text") from None
    if not (MIN_VALUE_BYTES <= len(encoded) <= MAX_VALUE_BYTES) or b"\x00" in encoded:
        raise InvalidInput(
            f"value must be {MIN_VALUE_BYTES} to {MAX_VALUE_BYTES} bytes with no NUL"
        )
    return encoded


def _is_absolute_command(path: str) -> bool:
    if "\x00" in path:
        return False
    if os.name == "nt":
        return bool(_WIN_ABSOLUTE_RE.match(path))
    return path.startswith("/")


def _norm_path(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def _normalize_allowed(commands: object) -> list[str]:
    if not isinstance(commands, (list, tuple)) or not commands:
        raise InvalidInput("allowed_commands must be a non-empty list of absolute paths")
    normalized: list[str] = []
    for command in commands:
        if not isinstance(command, str) or not _is_absolute_command(command):
            raise InvalidInput("each allowed command must be an absolute path")
        path = os.path.normpath(command)
        if _norm_path(path) not in {_norm_path(item) for item in normalized}:
            normalized.append(path)
    return normalized


def _command_allowed(command: str, allowed: list[str]) -> bool:
    if not _is_absolute_command(command):
        return False
    wanted = _norm_path(command)
    return any(_norm_path(item) == wanted for item in allowed)


def _record_aad(name: str, record: dict) -> bytes:
    meta = {
        "v": FORMAT_VERSION,
        "name": name,
        "group": record["group"],
        "purpose": record["purpose"],
        "allowed_commands": record["allowed_commands"],
        "class": record["class"],
    }
    return b"meridian-secrets-broker/record\n" + json.dumps(
        meta, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _decrypt_record(master: bytes, name: str, record: dict) -> bytes:
    try:
        return AESGCM(master).decrypt(
            _unb64(record["nonce"]), _unb64(record["ct"]), _record_aad(name, record)
        )
    except (InvalidTag, ValueError, TypeError, KeyError, OverflowError):
        raise IntegrityError(f"secret '{name}' failed its integrity check") from None


def _check_record_shape(name: str, record: object) -> None:
    """Fail closed on any record whose fields do not match its effective class.

    PUBLIC and INTERNAL records carry a plaintext ``value`` and no ciphertext.
    Every other record, including one with an unknown class, must carry
    ciphertext and no ``value``, so a plaintext value can never sit under a
    class that should be encrypted.
    """
    ok = isinstance(record, dict)
    if ok:
        ok = all(isinstance(record.get(key), str) for key in ("group", "purpose", "class"))
        ok = ok and isinstance(record.get("allowed_commands"), list)
        ok = ok and all(isinstance(item, str) for item in record["allowed_commands"])
    if ok:
        if effective_sensitivity(record["class"]) in PLAIN_CLASSES:
            ok = record["class"] in PLAIN_CLASSES and isinstance(record.get("value"), str)
            ok = ok and "nonce" not in record and "ct" not in record
        else:
            ok = isinstance(record.get("nonce"), str) and isinstance(record.get("ct"), str)
            ok = ok and "value" not in record
    if not ok:
        raise BrokerError(f"vault record '{name}' is malformed")


def redaction_forms(value: bytes) -> set[bytes]:
    """Every encoding of ``value`` that run_with_secret removes from output."""
    text = value.decode("utf-8")
    b64 = base64.b64encode(value)
    b64url = base64.urlsafe_b64encode(value)
    quoted = urllib.parse.quote(text, safe="")
    quoted_lower_hex = re.sub(r"%[0-9A-F]{2}", lambda m: m.group(0).lower(), quoted)
    return {
        value,
        b64,
        b64.rstrip(b"="),
        b64url,
        b64url.rstrip(b"="),
        quoted.encode("ascii"),
        quoted_lower_hex.encode("ascii"),
        urllib.parse.quote_plus(text, safe="").encode("ascii"),
        value.hex().encode("ascii"),
        value.hex().upper().encode("ascii"),
    }


def redact(data: bytes, value: bytes) -> bytes:
    """Replace every encoding of ``value`` in ``data``, longest form first."""
    for form in sorted(redaction_forms(value), key=len, reverse=True):
        data = data.replace(form, REDACTED_TOKEN)
    return data


def _append_audit_line(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())


@dataclass(frozen=True)
class RunResult:
    """status is 'completed', 'timed_out' or 'approval_required'."""

    status: str
    exit_code: int | None
    stdout: str
    stderr: str


class SecretsBroker:
    """Vault plus unlock state. The OS credential store is injected, never assumed."""

    def __init__(self, vault_dir: Path | str, credential_store: CredentialStore) -> None:
        self._dir = Path(vault_dir)
        self._store = credential_store
        self._key: bytearray | None = None
        self._off_names: set[str] = set()
        self._off_groups: set[str] = set()

    # -- lifecycle ---------------------------------------------------------

    def is_initialized(self) -> bool:
        return self._store.read(TARGET_PASSPHRASE_WRAP) is not None

    def is_unlocked(self) -> bool:
        return self._key is not None

    def initialize(self, passphrase: str) -> str:
        """Create the master key. Returns the recovery code, which is shown only once."""
        check_passphrase(passphrase)
        if self.is_initialized():
            raise AlreadyInitialized("broker is already initialized")
        if self._load_vault():
            raise AlreadyInitialized(
                "vault already holds secrets but its master-key wraps are missing; "
                "refusing to create a new master key over them"
            )
        master = secrets.token_bytes(MASTER_KEY_BYTES)
        recovery_code = _new_recovery_code()
        # The passphrase wrap is written last: it is the commit point.
        self._store.write(
            TARGET_RECOVERY_WRAP,
            _wrap_master_key(master, _normalize_recovery_code(recovery_code), "recovery"),
        )
        self._store.write(
            TARGET_PASSPHRASE_WRAP, _wrap_master_key(master, passphrase, "passphrase")
        )
        self._open(master)
        return recovery_code

    def unlock(self, passphrase: str) -> None:
        if not isinstance(passphrase, str):
            raise InvalidInput("passphrase must be a string")
        wrap = self._store.read(TARGET_PASSPHRASE_WRAP)
        if wrap is None:
            raise NotInitialized("broker is not initialized")
        self._open(_unwrap_master_key(wrap, passphrase, "passphrase"))

    def unlock_with_recovery(self, recovery_code: str) -> None:
        wrap = self._store.read(TARGET_RECOVERY_WRAP)
        if wrap is None:
            raise NotInitialized("broker is not initialized")
        self._open(_unwrap_master_key(wrap, _normalize_recovery_code(recovery_code), "recovery"))

    def change_passphrase(self, old_passphrase: str, new_passphrase: str) -> None:
        """Re-wrap the master key under a new passphrase. Values are not touched."""
        check_passphrase(new_passphrase)
        wrap = self._store.read(TARGET_PASSPHRASE_WRAP)
        if wrap is None:
            raise NotInitialized("broker is not initialized")
        master = _unwrap_master_key(wrap, old_passphrase, "passphrase")
        if self._key is not None and not hmac.compare_digest(master, bytes(self._key)):
            raise IntegrityError("stored master-key wrap does not match the unlocked key")
        self._store.write(TARGET_PASSPHRASE_WRAP, _wrap_master_key(master, new_passphrase, "passphrase"))

    def close(self) -> None:
        """Clear the key (best effort) and turn every switch off."""
        if self._key is not None:
            for index in range(len(self._key)):
                self._key[index] = 0
        self._key = None
        self._off_names.clear()
        self._off_groups.clear()

    def _open(self, master: bytes) -> None:
        for name, record in self._load_vault().items():
            if effective_sensitivity(record["class"]) in ENCRYPTED_CLASSES:
                _decrypt_record(master, name, record)  # IntegrityError on any mismatch
        self.close()
        self._key = bytearray(master)

    def _require_unlocked(self) -> bytearray:
        if self._key is None:
            raise BrokerLocked("broker is locked; unlock it first")
        return self._key

    # -- secrets -----------------------------------------------------------

    def put_secret(
        self,
        name: str,
        value: str,
        *,
        group: str,
        purpose: str,
        allowed_commands: list[str],
        sensitivity: str | None = None,
    ) -> None:
        """Create or replace an item. An unknown or unset class is stored as SECRET.

        Needs an unlocked broker for every class, so the write path has one rule.
        """
        key = self._require_unlocked()
        _check_name(name)
        cls = effective_sensitivity(sensitivity)
        core = {
            "group": _check_group(group),
            "purpose": _check_purpose(purpose),
            "allowed_commands": _normalize_allowed(allowed_commands),
            "class": cls,
        }
        encoded = _encode_value(value)
        records = self._load_vault()
        if cls in PLAIN_CLASSES:
            records[name] = {**core, "value": value}
        else:
            nonce = secrets.token_bytes(12)
            ciphertext = AESGCM(bytes(key)).encrypt(nonce, encoded, _record_aad(name, core))
            records[name] = {**core, "nonce": _b64(nonce), "ct": _b64(ciphertext)}
        self._save_vault(records)

    def list_names(self) -> list[dict[str, str]]:
        """Name, group, purpose and class for every item. Never a value.

        Works while locked. Encrypted items' class is verified at unlock (it is
        bound into their AEAD data). PUBLIC and INTERNAL items have no AEAD, so
        their metadata is not integrity-checked.
        """
        return [
            {
                "name": name,
                "group": record["group"],
                "purpose": record["purpose"],
                "class": effective_sensitivity(record["class"]),
            }
            for name, record in sorted(self._load_vault().items())
        ]

    def set_enabled(
        self, enabled: bool, *, name: str | None = None, group: str | None = None
    ) -> None:
        self._require_unlocked()
        if (name is None) == (group is None):
            raise InvalidInput("pass exactly one of name or group")
        records = self._load_vault()
        if name is not None:
            _check_name(name)
            if name not in records:
                raise UnknownSecret(f"no secret named '{name}'")
            switches, key = self._off_names, name
        else:
            wanted = _check_group(group)
            if not any(record["group"] == wanted for record in records.values()):
                raise UnknownSecret(f"no secrets in group '{wanted}'")
            switches, key = self._off_groups, wanted
        if enabled:
            switches.discard(key)
        else:
            switches.add(key)

    def is_enabled(self, name: str) -> bool:
        _check_name(name)
        record = self._load_vault().get(name)
        return record is not None and self._switch_on(name, record)

    def _switch_on(self, name: str, record: dict) -> bool:
        return (
            self._key is not None
            and name not in self._off_names
            and record["group"] not in self._off_groups
        )

    # -- run wrapper -------------------------------------------------------

    def run_with_secret(
        self,
        name: str,
        argv: list[str] | tuple[str, ...],
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> RunResult:
        if not isinstance(argv, (list, tuple)) or not argv or not all(isinstance(a, str) for a in argv):
            raise InvalidInput("argv must be a non-empty list of strings")
        _check_name(name)
        command = argv[0]
        if self._key is None:
            self._audit(name, command, "refused_locked")
            raise BrokerLocked("broker is locked; unlock it first")
        record = self._load_vault().get(name)
        if record is None:
            self._audit(name, command, "refused_unknown_secret")
            raise UnknownSecret(f"no secret named '{name}'")
        if not self._switch_on(name, record):
            self._audit(name, command, "refused_switched_off")
            raise SecretDisabled(f"secret '{name}' is switched off")
        if not _command_allowed(command, record["allowed_commands"]):
            self._audit(name, command, "refused_command_not_allowed")
            raise CommandNotAllowed(f"command is not allowed for secret '{name}'")
        cls = effective_sensitivity(record["class"])
        if cls == SENSITIVITY_SENSITIVE:
            self._audit(name, command, "approval_required")
            return RunResult(status="approval_required", exit_code=None, stdout="", stderr="")

        if cls in PLAIN_CLASSES:
            value_text = record["value"]
            plaintext = value_text.encode("utf-8")
        else:
            plaintext = _decrypt_record(bytes(self._key), name, record)
            value_text = plaintext.decode("utf-8")
        self._audit(name, command, "started")  # fail closed: no audit line, no run
        env = {**os.environ, name: value_text}
        try:
            proc = subprocess.run(
                list(argv),
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            self._audit(name, command, "timed_out")
            return RunResult(
                status="timed_out",
                exit_code=None,
                stdout=redact(exc.stdout or b"", plaintext).decode("utf-8", "replace"),
                stderr=redact(exc.stderr or b"", plaintext).decode("utf-8", "replace"),
            )
        except OSError as exc:
            self._audit(name, command, "failed_to_start")
            raise BrokerError(f"could not start command ({type(exc).__name__})") from None
        self._audit(name, command, "completed", exit_code=proc.returncode)
        return RunResult(
            status="completed",
            exit_code=proc.returncode,
            stdout=redact(proc.stdout or b"", plaintext).decode("utf-8", "replace"),
            stderr=redact(proc.stderr or b"", plaintext).decode("utf-8", "replace"),
        )

    # -- storage -----------------------------------------------------------

    def _load_vault(self) -> dict[str, dict]:
        try:
            raw = (self._dir / VAULT_FILE_NAME).read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        try:
            doc = json.loads(raw)
        except ValueError:
            raise BrokerError("vault file is not valid JSON") from None
        if (
            not isinstance(doc, dict)
            or doc.get("v") != FORMAT_VERSION
            or not isinstance(doc.get("secrets"), dict)
        ):
            raise BrokerError("vault file has an unexpected format")
        for name, record in doc["secrets"].items():
            _check_record_shape(name, record)
        return doc["secrets"]

    def _save_vault(self, records: dict[str, dict]) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        text = json.dumps({"v": FORMAT_VERSION, "secrets": records}, sort_keys=True, indent=2) + "\n"
        fd, tmp_name = tempfile.mkstemp(dir=self._dir, prefix=".vault-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self._dir / VAULT_FILE_NAME)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise

    def _audit(
        self, secret: str, command: str, outcome: str, *, exit_code: int | None = None
    ) -> None:
        entry: dict[str, object] = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "secret": secret,
            "command": command,
            "outcome": outcome,
        }
        if exit_code is not None:
            entry["exit_code"] = exit_code
        try:
            _append_audit_line(self._dir / AUDIT_FILE_NAME, json.dumps(entry, sort_keys=True) + "\n")
        except OSError:
            raise AuditUnavailable("audit log could not be written; the action was not run") from None


def open_default_broker(vault_dir: Path | None = None) -> SecretsBroker:
    """Broker wired to the OS credential store. Tests must not call this."""
    return SecretsBroker(vault_dir if vault_dir is not None else default_vault_dir(), default_credential_store())
