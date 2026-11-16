"""Hosted bearer token storage for the desktop. Pure Python: no Qt import.

The hosted Meridian bearer token (format in AGENTS.md) lives in the OS keychain and
never in settings.json. It is kept through secret_store, under service
``meridian-desktop``. One store holds every token the desktop keeps. Each token has its
own account name, so the hosted token and another token (a Google refresh token, for
example) share the store without sharing an entry.

Contract:
- get_token returns None when nothing is stored. It also returns None when the backend
  fails, so a failed read reads as "no token" and never raises.
- set_token and delete_token raise TokenStoreError when the backend fails. Nothing is
  silently dropped on a write.
- No function here writes a file. The token is in the keychain only.

Errors never carry a token value. The message names the operation and the error class.
Exceptions are not chained, so no traceback holds the value.
"""

from __future__ import annotations

import re

from meridian_desktop.secret_store import KeyringSecretStore, SecretStore, SecretStoreError

TOKEN_SERVICE = "meridian-desktop"
HOSTED_TOKEN_ACCOUNT = "hosted-bearer-token"

# An account is a short lowercase name. It is a label, never a secret.
_ACCOUNT = re.compile(r"[a-z][a-z0-9-]{0,63}")


class TokenStoreError(SecretStoreError):
    """A token could not be saved or removed. The message holds no token text."""


def _account(account: object) -> str:
    if not isinstance(account, str) or not _ACCOUNT.fullmatch(account):
        raise TokenStoreError("account must be a lowercase name of letters, digits and hyphens")
    return account


def _store(store: SecretStore | None) -> SecretStore:
    return store if store is not None else KeyringSecretStore()


def _describe(exc: BaseException) -> str:
    """Value-free text for a failed store call. SecretStoreError text names only the step and class."""
    if isinstance(exc, SecretStoreError):
        return str(exc)
    return f"keyring call failed ({exc.__class__.__name__})"


def get_token(account: str = HOSTED_TOKEN_ACCOUNT, *, store: SecretStore | None = None) -> str | None:
    """Return the stored token, or None when nothing is stored or the backend fails."""
    name = _account(account)
    try:
        return _store(store).get_secret(TOKEN_SERVICE, name)
    except Exception:  # noqa: BLE001 - a backend failure reads as "no token"; the error is dropped
        return None


def has_token(account: str = HOSTED_TOKEN_ACCOUNT, *, store: SecretStore | None = None) -> bool:
    """True when a token is saved. The keychain API has no existence test, so the value is read here only to test it.

    The value is dropped at once. Nothing is returned, shown or logged, and a failed read is False, as get_token gives.
    """
    return bool(get_token(account, store=store))


def set_token(token: str, account: str = HOSTED_TOKEN_ACCOUNT, *, store: SecretStore | None = None) -> None:
    """Store the token in the keychain. Raise TokenStoreError when the save fails."""
    name = _account(account)
    try:
        _store(store).set_secret(TOKEN_SERVICE, name, token)
    except Exception as exc:  # noqa: BLE001 - every failure is reported by class, never by its text
        detail = _describe(exc)
    else:
        return
    raise TokenStoreError(f"token could not be saved: {detail}") from None


def delete_token(account: str = HOSTED_TOKEN_ACCOUNT, *, store: SecretStore | None = None) -> bool:
    """Remove the token. Return False when nothing was stored, True when it was removed.

    Raise TokenStoreError when the backend fails, so a caller never believes a token was
    removed when it was not.
    """
    name = _account(account)
    try:
        return _store(store).delete_secret(TOKEN_SERVICE, name)
    except Exception as exc:  # noqa: BLE001 - every failure is reported by class, never by its text
        detail = _describe(exc)
    raise TokenStoreError(f"token could not be removed: {detail}") from None


__all__ = [
    "HOSTED_TOKEN_ACCOUNT",
    "TOKEN_SERVICE",
    "TokenStoreError",
    "delete_token",
    "get_token",
    "has_token",
    "set_token",
]
