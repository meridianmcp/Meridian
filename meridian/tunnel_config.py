"""Local OS-keyring access for workstation-only tunnel credentials.

This module intentionally keeps Zotero API keys out of project settings,
environment files, and hosted Meridian state.  The tunnel client reads the key
from the active native OS credential store and exposes it only to the local
Zotero MCP child process.
"""
from __future__ import annotations

import logging
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

_LOG = logging.getLogger(__name__)
_SERVICE = "Meridian"
_ZOTERO_ACCOUNT = "zotero-api-key"
_NATIVE_BACKENDS = {
    ("keyring.backends.windows", "winvaultkeyring"),
    ("keyring.backends.macos", "keyring"),
    ("keyring.backends.secretservice", "keyring"),
    ("keyring.backends.kwallet", "dbuskeyring"),
    ("keyring.backends.kwallet", "kwallet4"),
    ("keyring.backends.kwallet", "kwallet5"),
}
_COLLECTION_KEY_RE = re.compile(r"^[A-Za-z0-9]{1,64}$")
_LIBRARY_ID_RE = re.compile(r"^\d{1,20}$")


class ZoteroCredentialStoreUnavailable(RuntimeError):
    """Raised when no supported native OS credential store is available."""


def _native_keyring() -> Any:
    """Return keyring only when its selected backend is a native OS vault."""
    try:
        import keyring  # noqa: PLC0415 — optional until local credentials are used
    except ImportError as exc:
        raise ZoteroCredentialStoreUnavailable(
            "Install Meridian with its keyring dependency to manage Zotero credentials."
        ) from exc

    backend = keyring.get_keyring()
    backend_type = type(backend)
    identity = (backend_type.__module__.casefold(), backend_type.__name__.casefold())
    if identity not in _NATIVE_BACKENDS:
        raise ZoteroCredentialStoreUnavailable(
            "No supported native OS credential store is available."
        )
    return keyring


def get_zotero_api_key() -> str | None:
    """Read the local Zotero API key, or return ``None`` when unavailable."""
    try:
        keyring = _native_keyring()
        value = keyring.get_password(_SERVICE, _ZOTERO_ACCOUNT)
    except ZoteroCredentialStoreUnavailable as exc:
        _LOG.debug("Local Zotero credential unavailable: %s", exc)
        return None
    except Exception:  # noqa: BLE001 — public/group Zotero use remains available
        _LOG.warning("Could not read the local Zotero credential from the OS vault.")
        return None
    return value.strip() if isinstance(value, str) and value.strip() else None


def set_zotero_api_key(api_key: str) -> None:
    """Store a private-library key in the native OS vault; never log its value."""
    if not isinstance(api_key, str) or not api_key.strip():
        raise ValueError("A non-empty Zotero API key is required.")
    keyring = _native_keyring()
    try:
        keyring.set_password(_SERVICE, _ZOTERO_ACCOUNT, api_key.strip())
    except Exception as exc:  # noqa: BLE001 — surface storage failure to setup UI
        raise ZoteroCredentialStoreUnavailable(
            "Could not store the Zotero credential in the native OS vault."
        ) from exc


def delete_zotero_api_key() -> bool:
    """Remove the local Zotero API key; return False when none was stored."""
    keyring = _native_keyring()
    try:
        if keyring.get_password(_SERVICE, _ZOTERO_ACCOUNT) is None:
            return False
        keyring.delete_password(_SERVICE, _ZOTERO_ACCOUNT)
    except Exception as exc:  # noqa: BLE001 — surface storage failure to setup UI
        raise ZoteroCredentialStoreUnavailable(
            "Could not remove the Zotero credential from the native OS vault."
        ) from exc
    return True


def _zotero_preferences_path() -> Path:
    """Return the user-local, non-secret Zotero preferences path."""
    return Path.home() / ".meridian" / "zotero.json"


def _read_zotero_preferences() -> dict[str, Any]:
    try:
        data = json.loads(_zotero_preferences_path().read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def get_zotero_library_id() -> str | None:
    """Return the optional Zotero Web API user id stored on this machine."""
    value = _read_zotero_preferences().get("library_id")
    if isinstance(value, (str, int)) and _LIBRARY_ID_RE.fullmatch(str(value).strip()):
        return str(value).strip()
    return None


def get_zotero_collection_keys() -> list[str]:
    """Return the selected local collection keys, or [] for whole-library scope."""
    values = _read_zotero_preferences().get("collection_keys")
    if not isinstance(values, list):
        return []
    return list(dict.fromkeys(
        value.strip().upper()
        for value in values
        if isinstance(value, str) and _COLLECTION_KEY_RE.fullmatch(value.strip())
    ))


def set_zotero_preferences(
    *, library_id: str | int | None, collection_keys: list[str] | tuple[str, ...]
) -> None:
    """Persist non-secret Zotero scope locally with an atomic replacement.

    The API key is intentionally never written here; it remains in the native
    OS credential vault via :func:`set_zotero_api_key`.
    """
    if library_id is None or not str(library_id).strip():
        normalized_library_id = None
    else:
        normalized_library_id = str(library_id).strip()
        if not _LIBRARY_ID_RE.fullmatch(normalized_library_id):
            raise ValueError("Zotero user ID must contain only digits.")

    if not isinstance(collection_keys, (list, tuple)):
        raise ValueError("Zotero collection keys must be a list.")
    normalized_keys: list[str] = []
    for value in collection_keys:
        if not isinstance(value, str) or not _COLLECTION_KEY_RE.fullmatch(value.strip()):
            raise ValueError("A Zotero collection key is invalid.")
        key = value.strip().upper()
        if key not in normalized_keys:
            normalized_keys.append(key)

    path = _zotero_preferences_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:  # best-effort ACL hardening; Windows uses its user profile ACL
        pass
    payload = {
        "library_id": normalized_library_id,
        "collection_keys": normalized_keys,
    }
    temp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=".zotero-", suffix=".tmp", delete=False,
        ) as fh:
            temp_path = fh.name
            json.dump(payload, fh, sort_keys=True, indent=2)
            fh.write("\n")
        try:
            os.chmod(temp_path, 0o600)
        except OSError:  # best-effort on platforms without POSIX mode bits
            pass
        os.replace(temp_path, path)
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.unlink(temp_path)
            except OSError:
                pass
