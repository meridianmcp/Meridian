"""Tests for critical marks and the Windows user variable in the named API keys (meridian_desktop/api_keys.py). No Qt.

Every keychain call goes to MemoryKeyring or FailingKeyring, and every user variable write goes to FakeUserRegistry, so nothing
touches the real credential store or HKCU. FAKE_VALUE is an obviously fake value, so the leak checks can search for it.
"""

from __future__ import annotations

import json

import pytest

from _fakes import FailingKeyring, FakeUserRegistry, MemoryKeyring
from meridian_desktop.api_keys import (
    KEY_SERVICE,
    RUNPOD_ENVIRONMENT_NAME,
    ApiKeys,
    EnvironmentSet,
    KeyNameError,
    environment_name,
)
from meridian_desktop.secret_store import RUNPOD_ACCOUNT, KeyringSecretStore, SecretStoreError
from meridian_desktop.settings_store import Settings, SettingsStore
from meridian_desktop.user_environment import UserEnvironment, UserEnvironmentError

FAKE_VALUE = "sk-fake-api-keys-critical-0001"
FAKE_VALUE_TWO = "sk-fake-api-keys-critical-0002"


@pytest.fixture
def backend():
    return MemoryKeyring()


@pytest.fixture
def registry():
    return FakeUserRegistry()


@pytest.fixture
def store(settings_path):
    return SettingsStore(settings_path)


@pytest.fixture
def keys(store, backend, registry):
    return ApiKeys(store, KeyringSecretStore(backend=backend), user_environment=UserEnvironment(registry))


def _settings_json(settings_path) -> dict:
    return json.loads(settings_path.read_text(encoding="utf-8"))


# ----------------------------------------------------------- critical marks


def test_marking_a_key_critical_stores_its_listed_name_in_settings_and_never_its_value(keys, settings_path):
    keys.save("OpenAI", FAKE_VALUE)

    listed = keys.set_critical("OpenAI", True)

    assert listed == "OpenAI"
    assert _settings_json(settings_path)["critical_keys"] == ["OpenAI"]
    assert FAKE_VALUE.encode() not in settings_path.read_bytes()


def test_the_mark_is_found_by_slug_and_stored_under_the_listed_name(keys, settings_path):
    keys.save("OpenAI", FAKE_VALUE)

    keys.set_critical("openai", True)
    assert _settings_json(settings_path)["critical_keys"] == ["OpenAI"]

    keys.set_critical("OPENAI", False)
    assert "critical_keys" not in _settings_json(settings_path), "clearing the mark removes it from the file"


def test_a_critical_key_is_listed_first_and_the_rest_keep_their_order(keys):
    keys.save("OpenAI", FAKE_VALUE)
    keys.save("Anthropic", FAKE_VALUE_TWO)
    keys.save("Mistral", FAKE_VALUE)
    keys.set_critical("Mistral", True)
    keys.set_critical("Anthropic", True)

    entries = keys.entries()

    assert [entry.name for entry in entries] == ["Anthropic", "Mistral", "RunPod", "OpenAI"]
    assert [entry.critical for entry in entries] == [True, True, False, False]


def test_runpod_can_be_marked_and_then_leads_the_critical_group(keys):
    keys.save("OpenAI", FAKE_VALUE)
    keys.save("Mistral", FAKE_VALUE)
    keys.set_critical("Mistral", True)
    keys.set_critical("RunPod", True)

    assert [entry.name for entry in keys.entries()] == ["RunPod", "Mistral", "OpenAI"]


def test_clearing_a_mark_puts_the_name_back_in_its_original_place(keys):
    keys.save("OpenAI", FAKE_VALUE)
    keys.save("Anthropic", FAKE_VALUE_TWO)
    keys.set_critical("Anthropic", True)

    keys.set_critical("Anthropic", False)

    assert [entry.name for entry in keys.entries()] == ["RunPod", "OpenAI", "Anthropic"]


def test_a_name_that_is_not_listed_cannot_be_marked_and_nothing_is_written(keys, settings_path):
    with pytest.raises(KeyNameError):
        keys.set_critical("Nope", True)

    assert not settings_path.exists(), "a refused mark writes nothing at all"


def test_a_settings_write_that_fails_leaves_the_marks_as_they_were(keys, settings_path, monkeypatch):
    keys.save("OpenAI", FAKE_VALUE)

    def refuse(self, settings):
        raise OSError("disk full")

    monkeypatch.setattr(SettingsStore, "save", refuse)
    with pytest.raises(OSError):
        keys.set_critical("OpenAI", True)

    assert "critical_keys" not in _settings_json(settings_path)


def test_marking_over_an_unreadable_settings_file_keeps_a_copy_of_it_first(keys, settings_path):
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text("{not json", encoding="utf-8")

    keys.set_critical("RunPod", True)

    backup = settings_path.with_name(settings_path.name + ".unreadable.bak")
    assert backup.read_text(encoding="utf-8") == "{not json", "the unreadable file is kept before it is replaced"
    assert _settings_json(settings_path)["critical_keys"] == ["RunPod"]


def test_removing_a_custom_key_also_drops_its_mark(keys, settings_path):
    keys.save("OpenAI", FAKE_VALUE)
    keys.save("Anthropic", FAKE_VALUE_TWO)
    keys.set_critical("OpenAI", True)
    keys.set_critical("Anthropic", True)

    keys.remove("OpenAI")

    settings = _settings_json(settings_path)
    assert settings["critical_keys"] == ["Anthropic"], "only the removed name's mark leaves the file"
    assert "OpenAI" not in settings["key_names"]


def test_removing_runpod_keeps_its_listing_and_its_mark(keys, settings_path):
    keys.save("RunPod", FAKE_VALUE)
    keys.set_critical("RunPod", True)

    keys.remove("RunPod")

    assert [entry.name for entry in keys.entries()] == ["RunPod"]
    assert keys.entries()[0].critical is True
    assert _settings_json(settings_path)["critical_keys"] == ["RunPod"]


def test_a_malformed_marks_value_reads_as_no_marks(keys, settings_path):
    keys.save("OpenAI", FAKE_VALUE)
    data = _settings_json(settings_path)
    data["critical_keys"] = "OpenAI"
    settings_path.write_text(json.dumps(data), encoding="utf-8")

    assert all(entry.critical is False for entry in keys.entries())


def test_a_mark_on_a_name_that_is_no_longer_listed_matches_nothing(keys, store):
    store.save(Settings(critical_keys=("Ghost",)))

    assert [entry.name for entry in keys.entries()] == ["RunPod"]
    assert all(entry.critical is False for entry in keys.entries())


# ----------------------------------------------------------- variable names


@pytest.mark.parametrize(
    ("name", "variable"),
    [("OpenAI", "OPENAI"), ("Open AI", "OPEN_AI"), ("my.key", "MY_KEY"), ("alpha-beta_2", "ALPHA_BETA_2")],
)
def test_a_key_name_becomes_its_variable_in_capitals_with_dashes_as_underscores(name, variable):
    assert environment_name(name) == variable


def test_runpod_is_set_as_runpod_api_key_under_any_spelling():
    assert environment_name("RunPod") == RUNPOD_ENVIRONMENT_NAME == "RUNPOD_API_KEY"
    assert environment_name("runpod") == "RUNPOD_API_KEY"


@pytest.mark.parametrize("name", ["Path", "TEMP", "2FA"])
def test_a_name_windows_cannot_take_as_a_variable_is_refused(name):
    with pytest.raises(KeyNameError):
        environment_name(name)


def test_the_variable_name_of_an_unlisted_key_is_refused(keys):
    with pytest.raises(KeyNameError):
        keys.environment_name("Nope")


def test_the_variable_name_of_a_listed_key_is_given_before_anything_is_written(keys, registry):
    keys.save("OpenAI", FAKE_VALUE)

    assert keys.environment_name("openai") == "OPENAI"
    assert registry.writes == []


# ----------------------------------------------------------- set as a user variable


def test_the_saved_value_is_written_to_the_user_environment_and_the_result_holds_no_value(keys, registry):
    keys.save("OpenAI", FAKE_VALUE)

    written = keys.set_user_environment("OpenAI")

    assert registry.writes == [("OPENAI", FAKE_VALUE)]
    assert written == EnvironmentSet(variable="OPENAI", announced=True)
    assert FAKE_VALUE not in repr(written)


def test_the_value_is_never_stored_in_settings_when_it_is_set_as_a_user_variable(keys, settings_path):
    keys.save("OpenAI", FAKE_VALUE)

    keys.set_user_environment("OpenAI")

    assert FAKE_VALUE.encode() not in settings_path.read_bytes()


def test_the_runpod_value_goes_to_runpod_api_key(keys, backend, registry):
    keys.save("RunPod", FAKE_VALUE)
    assert backend.get_password(KEY_SERVICE, RUNPOD_ACCOUNT) == FAKE_VALUE

    keys.set_user_environment("RunPod")

    assert registry.writes == [("RUNPOD_API_KEY", FAKE_VALUE)]


def test_the_value_of_a_name_spelt_in_another_case_is_the_one_written(keys, registry):
    keys.save("OpenAI", FAKE_VALUE)

    keys.set_user_environment("openai")

    assert registry.writes == [("OPENAI", FAKE_VALUE)]


def test_a_key_with_no_saved_value_writes_nothing(store, backend, registry):
    store.save(Settings(key_names=("OpenAI",)))
    keys = ApiKeys(store, KeyringSecretStore(backend=backend), user_environment=UserEnvironment(registry))

    with pytest.raises(KeyNameError):
        keys.set_user_environment("OpenAI")

    assert registry.writes == []


def test_an_unlisted_name_writes_nothing(keys, registry):
    with pytest.raises(KeyNameError):
        keys.set_user_environment("Nope")

    assert registry.writes == []


def test_a_keychain_that_cannot_be_read_is_reported_by_class_only_and_nothing_is_written(store, registry):
    store.save(Settings(key_names=("OpenAI",)))
    keys = ApiKeys(store, KeyringSecretStore(backend=FailingKeyring(FAKE_VALUE)), user_environment=UserEnvironment(registry))

    with pytest.raises(SecretStoreError) as caught:
        keys.set_user_environment("OpenAI")

    assert FAKE_VALUE not in str(caught.value)
    assert registry.writes == []


def test_a_registry_failure_is_reported_by_class_only_and_never_carries_the_value(store, backend):
    store.save(Settings(key_names=("OpenAI",)))
    KeyringSecretStore(backend=backend).set_secret(KEY_SERVICE, "key-openai", FAKE_VALUE)
    failing = FakeUserRegistry(fail_write=PermissionError(f"denied while holding {FAKE_VALUE}"))
    keys = ApiKeys(store, KeyringSecretStore(backend=backend), user_environment=UserEnvironment(failing))

    with pytest.raises(UserEnvironmentError) as caught:
        keys.set_user_environment("OpenAI")

    text = str(caught.value)
    assert "PermissionError" in text
    assert FAKE_VALUE not in text
    assert failing.announcements == 0


def test_an_unconfirmed_broadcast_is_reported_on_the_result_and_the_value_stays_written(store, backend):
    store.save(Settings(key_names=("OpenAI",)))
    KeyringSecretStore(backend=backend).set_secret(KEY_SERVICE, "key-openai", FAKE_VALUE)
    registry = FakeUserRegistry(announced=False)
    keys = ApiKeys(store, KeyringSecretStore(backend=backend), user_environment=UserEnvironment(registry))

    written = keys.set_user_environment("OpenAI")

    assert written.announced is False
    assert registry.writes == [("OPENAI", FAKE_VALUE)]
