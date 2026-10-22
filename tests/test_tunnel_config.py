"""Tests for workstation-only credential storage."""
from __future__ import annotations

import sys
import types

import pytest

from meridian import tunnel_config


def _install_keyring(monkeypatch, backend_type):
    stored = {}
    backend = backend_type()
    module = types.ModuleType("keyring")
    module.get_keyring = lambda: backend
    module.set_password = lambda service, account, value: stored.__setitem__((service, account), value)
    module.get_password = lambda service, account: stored.get((service, account))
    module.delete_password = lambda service, account: stored.pop((service, account), None)
    monkeypatch.setitem(sys.modules, "keyring", module)
    return stored


def test_zotero_api_key_round_trips_through_native_windows_vault(monkeypatch):
    class WinVaultKeyring:
        pass

    WinVaultKeyring.__module__ = "keyring.backends.Windows"
    stored = _install_keyring(monkeypatch, WinVaultKeyring)

    tunnel_config.set_zotero_api_key("  local-test-key  ")
    assert tunnel_config.get_zotero_api_key() == "local-test-key"
    assert set(stored.values()) == {"local-test-key"}
    assert tunnel_config.delete_zotero_api_key() is True
    assert tunnel_config.get_zotero_api_key() is None
    assert tunnel_config.delete_zotero_api_key() is False


def test_zotero_api_key_fails_closed_on_non_native_backend(monkeypatch):
    class PlaintextKeyring:
        pass

    PlaintextKeyring.__module__ = "keyrings.alt.file"
    _install_keyring(monkeypatch, PlaintextKeyring)

    assert tunnel_config.get_zotero_api_key() is None
    with pytest.raises(tunnel_config.ZoteroCredentialStoreUnavailable):
        tunnel_config.set_zotero_api_key("local-test-key")


def test_zotero_api_key_rejects_empty_input(monkeypatch):
    class WinVaultKeyring:
        pass

    WinVaultKeyring.__module__ = "keyring.backends.Windows"
    _install_keyring(monkeypatch, WinVaultKeyring)

    with pytest.raises(ValueError):
        tunnel_config.set_zotero_api_key("  ")
