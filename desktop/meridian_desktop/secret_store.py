"""Secret storage for the desktop. Pure Python: no Qt import.

The RunPod key lives in the OS keychain, through the ``keyring`` package, under
service ``meridian-desktop`` and account ``runpod-api-key``. The Meridian core
reads that same entry through its ``keyring:meridian-desktop/runpod-api-key``
credential reference. The desktop never sends the key to the Meridian server or
to RunPod.

``keyring`` is imported inside the methods, not at module import. A backend can
be injected for tests, so tests never touch the real credential store.

Errors name the operation and the exception class only. A value is never put in
an error message, and the exception is not chained, so no traceback carries a
secret.
"""

from __future__ import annotations

from typing import Any, Protocol

RUNPOD_SERVICE = "meridian-desktop"
RUNPOD_ACCOUNT = "runpod-api-key"
MAX_SECRET_LENGTH = 512


class SecretStoreError(Exception):
    """A store operation failed. The message names the operation and the exception class."""


class SecretBackend(Protocol):
    """The part of the ``keyring`` module interface that the store uses."""

    def get_password(self, service: str, username: str) -> str | None: ...

    def set_password(self, service: str, username: str, password: str) -> None: ...

    def delete_password(self, service: str, username: str) -> None: ...


class SecretStore(Protocol):
    """What the UI needs from a store. KeyringSecretStore is the real one."""

    def get_secret(self, service: str, account: str) -> str | None: ...

    def set_secret(self, service: str, account: str, value: str) -> None: ...

    def delete_secret(self, service: str, account: str) -> bool: ...


def _clean(value: Any) -> str:
    """Strip surrounding whitespace, then reject empty and over-long values."""
    if not isinstance(value, str):
        raise SecretStoreError("secret must be a string")
    cleaned = value.strip()
    if not cleaned:
        raise SecretStoreError("secret is empty")
    if len(cleaned) > MAX_SECRET_LENGTH:
        raise SecretStoreError(f"secret is longer than {MAX_SECRET_LENGTH} characters")
    return cleaned


def _call(operation: str, fn: Any, *args: Any) -> Any:
    """Run one backend call. Any failure becomes SecretStoreError with the class name only.

    The raise sits after the except block, so no exception context is attached. The
    original exception, which may carry the value, is dropped here.
    """
    try:
        return fn(*args)
    except Exception as exc:  # noqa: BLE001 - every backend failure is reported by class only
        kind = exc.__class__.__name__
    raise SecretStoreError(f"keyring {operation} failed ({kind})") from None


class KeyringSecretStore:
    """Secrets in the OS keychain through ``keyring``, or through an injected backend."""

    def __init__(self, backend: SecretBackend | None = None) -> None:
        self._backend = backend

    def _module(self) -> SecretBackend:
        if self._backend is not None:
            return self._backend
        import keyring  # lazy: the import happens on first use, not at module import

        return keyring

    def get_secret(self, service: str, account: str) -> str | None:
        return _call("read", self._module().get_password, service, account)

    def set_secret(self, service: str, account: str, value: str) -> None:
        cleaned = _clean(value)
        _call("save", self._module().set_password, service, account, cleaned)

    def delete_secret(self, service: str, account: str) -> bool:
        """Remove the entry. Return False when nothing was stored, True when it was removed."""
        backend = self._module()
        if _call("read", backend.get_password, service, account) is None:
            return False
        _call("delete", backend.delete_password, service, account)
        return True


__all__ = [
    "MAX_SECRET_LENGTH",
    "RUNPOD_ACCOUNT",
    "RUNPOD_SERVICE",
    "KeyringSecretStore",
    "SecretBackend",
    "SecretStore",
    "SecretStoreError",
]
