"""Tests for named API keys (meridian_desktop/api_keys.py). Pure Python, no Qt.

Every keychain call goes to MemoryKeyring, a fake backend, so nothing touches the real credential store. FAKE_VALUE is an
obviously fake value, so a leak check can search for it. The settings file is in pytest's temp directory.
"""

from __future__ import annotations

import json

import pytest

from _fakes import FailingKeyring, MemoryKeyring
from meridian_desktop.api_keys import (
    BUILT_IN_NAMES,
    KEY_SERVICE,
    MAX_CUSTOM_NAMES,
    STATUS_NOT_SET,
    STATUS_SET,
    STATUS_UNREADABLE,
    ApiKeys,
    KeyEntry,
    KeyNameError,
    account_for,
    clean_name,
    slug,
)
from meridian_desktop.secret_store import RUNPOD_ACCOUNT, RUNPOD_SERVICE, KeyringSecretStore, SecretStoreError
from meridian_desktop.settings_store import Settings, SettingsStore

FAKE_VALUE = "sk-fake-api-keys-0001"
FAKE_VALUE_TWO = "sk-fake-api-keys-0002"


@pytest.fixture
def backend():
    return MemoryKeyring()


@pytest.fixture
def store(settings_path):
    return SettingsStore(settings_path)


@pytest.fixture
def keys(store, backend):
    return ApiKeys(store, KeyringSecretStore(backend=backend))


def _names_in_file(settings_path) -> list:
    return json.loads(settings_path.read_text(encoding="utf-8")).get("key_names", [])


# ----------------------------------------------------------- accounts and names


def test_the_service_is_the_desktop_service_the_runpod_key_uses():
    assert KEY_SERVICE == RUNPOD_SERVICE == "meridian-desktop"


def test_runpod_keeps_the_account_the_core_reads():
    assert RUNPOD_ACCOUNT == "runpod-api-key"
    assert account_for("RunPod") == RUNPOD_ACCOUNT
    assert account_for("runpod") == RUNPOD_ACCOUNT
    assert account_for("  RUNPOD  ") == RUNPOD_ACCOUNT
    assert account_for("Run Pod") == "key-run-pod", "a different name is a different key"


def test_a_custom_name_gets_its_own_account():
    assert account_for("OpenAI") == "key-openai"
    assert account_for("Anthropic") == "key-anthropic"
    assert account_for("Open AI") == "key-open-ai"
    assert account_for("my.key_2") == "key-my-key-2"


def test_a_custom_account_never_collides_with_the_runpod_or_token_accounts():
    accounts = {account_for(name) for name in ("OpenAI", "Anthropic", "hosted bearer token", "runpod api key")}
    assert RUNPOD_ACCOUNT not in accounts
    assert "hosted-bearer-token" not in accounts


@pytest.mark.parametrize("text", ["", "   ", "bad/name", "name;drop", "-leading", "x" * 41, "emoji-☃"])
def test_a_bad_name_is_refused_with_plain_words(text):
    with pytest.raises(KeyNameError) as caught:
        clean_name(text)
    assert str(caught.value)


def test_a_name_is_cleaned_of_extra_spaces():
    assert clean_name("  Open   AI  ") == "Open AI"
    assert slug("Open AI!") == "open-ai"


# ----------------------------------------------------------- keychain round trip


def test_save_stores_the_value_under_the_name_account(keys, backend):
    keys.save("OpenAI", FAKE_VALUE)

    assert backend.snapshot() == {(KEY_SERVICE, "key-openai"): FAKE_VALUE}
    assert keys.status("OpenAI") == STATUS_SET


def test_remove_deletes_the_value_and_the_name(keys, backend, store, settings_path):
    keys.save("OpenAI", FAKE_VALUE)

    removed = keys.remove("OpenAI")

    assert removed is True
    assert backend.snapshot() == {}
    assert keys.status("OpenAI") == STATUS_NOT_SET
    assert "OpenAI" not in keys.names()
    assert "key_names" not in json.loads(settings_path.read_text(encoding="utf-8"))


def test_remove_of_a_listed_name_with_no_value_reports_nothing_removed(store, backend):
    store.save(Settings(key_names=("OpenAI",)))
    keys = ApiKeys(store, KeyringSecretStore(backend=backend))

    assert keys.remove("OpenAI") is False
    assert backend.snapshot() == {}
    assert keys.names() == ["RunPod"], "the name leaves the list when it is removed"


def test_removing_a_name_twice_says_it_is_not_listed(keys):
    keys.save("OpenAI", FAKE_VALUE)
    keys.remove("OpenAI")

    with pytest.raises(KeyNameError):
        keys.remove("OpenAI")


def test_replace_keeps_the_listing_and_changes_only_the_value(keys, backend):
    keys.save("OpenAI", FAKE_VALUE)

    listed = keys.save("openai", FAKE_VALUE_TWO)

    assert listed == "OpenAI", "the same slug keeps the name as it was first listed"
    assert keys.names() == ["RunPod", "OpenAI"]
    assert backend.snapshot() == {(KEY_SERVICE, "key-openai"): FAKE_VALUE_TWO}


def test_runpod_is_built_in_and_its_save_writes_the_runpod_account(keys, backend, settings_path):
    keys.save("runpod", FAKE_VALUE)

    assert backend.snapshot() == {(RUNPOD_SERVICE, RUNPOD_ACCOUNT): FAKE_VALUE}
    assert keys.names() == ["RunPod"]
    # A built-in name is never written to the list of custom names
    assert not settings_path.exists(), "saving RunPod writes no custom name to the settings file"


def test_runpod_cannot_be_removed_from_the_list(keys):
    keys.save("RunPod", FAKE_VALUE)

    keys.remove("RunPod")

    assert keys.names() == list(BUILT_IN_NAMES), "RunPod stays listed, so its card is always there"
    assert keys.status("RunPod") == STATUS_NOT_SET


# ----------------------------------------------------------- names listed, values never returned


def test_entries_carry_name_status_and_built_in_flag_only(keys):
    keys.save("OpenAI", FAKE_VALUE)

    entries = keys.entries()

    assert [entry.name for entry in entries] == ["RunPod", "OpenAI"]
    assert all(isinstance(entry, KeyEntry) for entry in entries)
    assert [entry.built_in for entry in entries] == [True, False]
    assert {entry.status for entry in entries} == {STATUS_NOT_SET, STATUS_SET}
    for entry in entries:
        assert set(vars(entry)) == {"name", "status", "built_in", "critical"}, "a KeyEntry holds no value field"
        assert FAKE_VALUE not in repr(entry)


def test_names_are_saved_in_settings_and_values_never_are(keys, settings_path):
    keys.save("OpenAI", FAKE_VALUE)
    keys.save("Anthropic", FAKE_VALUE_TWO)

    assert _names_in_file(settings_path) == ["OpenAI", "Anthropic"]
    raw = settings_path.read_bytes()
    assert FAKE_VALUE.encode() not in raw
    assert FAKE_VALUE_TWO.encode() not in raw


def test_a_value_is_never_in_the_names_listing_or_any_entry(keys):
    keys.save("OpenAI", FAKE_VALUE)

    listing = repr(keys.names()) + repr(keys.entries())

    assert FAKE_VALUE not in listing


def test_a_status_read_returns_only_a_state_word(keys):
    keys.save("OpenAI", FAKE_VALUE)

    assert keys.status("OpenAI") in (STATUS_SET, STATUS_NOT_SET, STATUS_UNREADABLE)
    assert keys.status("OpenAI") == STATUS_SET


def test_a_name_with_the_same_slug_as_a_listed_one_is_one_key(keys, backend):
    keys.save("Open AI", FAKE_VALUE)
    keys.save("open-ai", FAKE_VALUE_TWO)

    assert keys.names() == ["RunPod", "Open AI"]
    assert backend.snapshot() == {(KEY_SERVICE, "key-open-ai"): FAKE_VALUE_TWO}


def test_the_custom_name_list_is_capped(keys):
    for index in range(MAX_CUSTOM_NAMES):
        keys.save(f"Key {index}", FAKE_VALUE)

    with pytest.raises(KeyNameError):
        keys.save("One too many", FAKE_VALUE)
    assert len(keys.names()) == len(BUILT_IN_NAMES) + MAX_CUSTOM_NAMES


# ----------------------------------------------------------- refusals write nothing


@pytest.mark.parametrize("name", ["", "   ", "bad/name"])
def test_a_refused_name_writes_nothing(keys, backend, settings_path, name):
    with pytest.raises(KeyNameError):
        keys.save(name, FAKE_VALUE)

    assert backend.snapshot() == {}
    assert not settings_path.exists()


@pytest.mark.parametrize("value", ["", "   "])
def test_an_empty_value_is_refused_and_nothing_is_written(keys, backend, settings_path, value):
    with pytest.raises(ValueError):
        keys.save("OpenAI", value)

    assert backend.snapshot() == {}
    assert not settings_path.exists()


def test_a_failed_name_write_removes_the_value_it_just_wrote(backend, settings_path, store, monkeypatch):
    keys = ApiKeys(store, KeyringSecretStore(backend=backend))

    def refuse(settings):
        raise OSError("disk full")

    monkeypatch.setattr(store, "save", refuse)

    with pytest.raises(OSError):
        keys.save("OpenAI", FAKE_VALUE)

    assert backend.snapshot() == {}, "no value is left behind without its name"


def test_a_keychain_failure_is_reported_without_the_value(settings_path, store):
    keys = ApiKeys(store, KeyringSecretStore(backend=FailingKeyring(FAKE_VALUE)))

    with pytest.raises(SecretStoreError) as caught:
        keys.save("OpenAI", FAKE_VALUE)

    assert FAKE_VALUE not in str(caught.value)
    assert "keyring save failed (ValueError)" in str(caught.value)


def test_a_failed_read_shows_as_unreadable_not_as_not_set(settings_path, store):
    keys = ApiKeys(store, KeyringSecretStore(backend=FailingKeyring(FAKE_VALUE)))

    assert keys.status("OpenAI") == STATUS_UNREADABLE
    assert FAKE_VALUE not in repr(keys.entries())


def test_a_saved_name_that_is_not_valid_is_left_out_of_the_list(store, backend, settings_path):
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps({"key_names": ["OpenAI", "bad/name", "RunPod", "OpenAI"]}), encoding="utf-8")
    keys = ApiKeys(store, KeyringSecretStore(backend=backend))

    assert keys.names() == ["RunPod", "OpenAI"]
