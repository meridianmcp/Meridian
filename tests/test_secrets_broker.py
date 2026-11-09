"""Tests for the local secrets broker, slice 1.

Every credential here is a fake value. Each test injects InMemoryCredentialStore,
so the OS credential store is never touched.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import urllib.parse
from pathlib import Path

import pytest

from meridian import secrets_broker as sb

PASSPHRASE = "FAKE-passphrase-123"
NEW_PASSPHRASE = "FAKE-new-passphrase-456"
WRONG_PASSPHRASE = "FAKE-wrong-passphrase-9"
VALUE = "FAKE-VALUE-123"
SPECIAL_VALUE = "FAKE/VALUE+123=ab"
NAME = "FAKE_API_KEY"
GROUP = "fake-group"
PURPOSE = "fake test key"
PY = sys.executable


class InMemoryCredentialStore:
    """Fake credential store that lives in process memory only."""

    def __init__(self) -> None:
        self.items: dict[str, str] = {}

    def read(self, target: str) -> str | None:
        return self.items.get(target)

    def write(self, target: str, text: str) -> None:
        self.items[target] = text


def _match_code(name: str, expected: str) -> str:
    """Child program that prints 'match' only if its env var equals expected."""
    return (
        "import os, sys\n"
        f"sys.stdout.write('match' if os.environ[{name!r}] == {expected!r} else 'mismatch')\n"
    )


def _put(
    broker: sb.SecretsBroker,
    *,
    name: str = NAME,
    value: str = VALUE,
    group: str = GROUP,
    risk: str = "routine",
    allowed: list[str] | None = None,
) -> None:
    broker.put_secret(
        name,
        value,
        group=group,
        purpose=PURPOSE,
        allowed_commands=allowed if allowed is not None else [PY],
        risk=risk,
    )


def _flip_first_byte(b64_text: str) -> str:
    raw = bytearray(base64.b64decode(b64_text))
    raw[0] ^= 0x01
    return base64.b64encode(bytes(raw)).decode("ascii")


@pytest.fixture
def store() -> InMemoryCredentialStore:
    return InMemoryCredentialStore()


@pytest.fixture
def vault_dir(tmp_path: Path) -> Path:
    return tmp_path / "vault"


@pytest.fixture
def broker(vault_dir: Path, store: InMemoryCredentialStore):
    instance = sb.SecretsBroker(vault_dir, store)
    yield instance
    instance.close()


# -- wrap, unwrap, recovery -------------------------------------------------


def test_wrap_unwrap_round_trip_and_child_receives_value(vault_dir, store):
    first = sb.SecretsBroker(vault_dir, store)
    first.initialize(PASSPHRASE)
    _put(first)
    first.close()

    second = sb.SecretsBroker(vault_dir, store)
    second.unlock(PASSPHRASE)
    try:
        result = second.run_with_secret(NAME, [PY, "-c", _match_code(NAME, VALUE)])
    finally:
        second.close()
    assert result.status == "completed"
    assert result.exit_code == 0
    assert result.stdout == "match"


def test_wrong_passphrase_is_rejected_without_echoing_it(broker):
    broker.initialize(PASSPHRASE)
    broker.close()
    with pytest.raises(sb.UnlockFailed) as info:
        broker.unlock(WRONG_PASSPHRASE)
    assert WRONG_PASSPHRASE not in str(info.value)
    assert PASSPHRASE not in str(info.value)
    assert not broker.is_unlocked()


def test_recovery_code_unwraps_master_key(vault_dir, store):
    first = sb.SecretsBroker(vault_dir, store)
    recovery = first.initialize(PASSPHRASE)
    _put(first)
    first.close()

    second = sb.SecretsBroker(vault_dir, store)
    # Loose typing (lowercase, no dashes, spaces) must still normalise.
    second.unlock_with_recovery(" " + recovery.replace("-", "").lower() + " ")
    try:
        result = second.run_with_secret(NAME, [PY, "-c", _match_code(NAME, VALUE)])
    finally:
        second.close()
    assert result.stdout == "match"


def test_wrong_recovery_code_is_rejected(broker):
    broker.initialize(PASSPHRASE)
    broker.close()
    with pytest.raises(sb.UnlockFailed):
        broker.unlock_with_recovery("AAAA-" * 7 + "AAAA")
    assert not broker.is_unlocked()


def test_recovery_code_is_shown_once_and_its_text_is_stored_nowhere(broker, store, vault_dir):
    recovery = broker.initialize(PASSPHRASE)
    _put(broker)
    broker.close()
    stored_text = json.dumps(store.items) + (vault_dir / sb.VAULT_FILE_NAME).read_text(encoding="utf-8")
    assert recovery not in stored_text
    assert recovery.replace("-", "") not in stored_text


def test_passphrase_change_rewraps_without_reencrypting_values(broker, store, vault_dir):
    recovery = broker.initialize(PASSPHRASE)
    _put(broker)
    vault_path = vault_dir / sb.VAULT_FILE_NAME
    vault_before = json.loads(vault_path.read_text(encoding="utf-8"))
    passphrase_wrap_before = store.items[sb.TARGET_PASSPHRASE_WRAP]
    recovery_wrap_before = store.items[sb.TARGET_RECOVERY_WRAP]
    broker.close()

    broker.change_passphrase(PASSPHRASE, NEW_PASSPHRASE)

    vault_after = json.loads(vault_path.read_text(encoding="utf-8"))
    assert vault_after["secrets"] == vault_before["secrets"]  # same nonce and ciphertext
    assert store.items[sb.TARGET_PASSPHRASE_WRAP] != passphrase_wrap_before
    assert store.items[sb.TARGET_RECOVERY_WRAP] == recovery_wrap_before

    with pytest.raises(sb.UnlockFailed):
        broker.unlock(PASSPHRASE)
    broker.unlock(NEW_PASSPHRASE)
    assert broker.run_with_secret(NAME, [PY, "-c", _match_code(NAME, VALUE)]).stdout == "match"
    broker.close()

    broker.unlock_with_recovery(recovery)  # recovery code still works after the change
    assert broker.is_unlocked()
    broker.close()


def test_change_passphrase_requires_the_current_passphrase(broker, store):
    broker.initialize(PASSPHRASE)
    before = store.items[sb.TARGET_PASSPHRASE_WRAP]
    with pytest.raises(sb.UnlockFailed):
        broker.change_passphrase(WRONG_PASSPHRASE, NEW_PASSPHRASE)
    assert store.items[sb.TARGET_PASSPHRASE_WRAP] == before


def test_passphrase_minimum_length_and_common_list_are_enforced(broker, store):
    with pytest.raises(sb.PassphraseRejected):
        broker.initialize("FAKE-tiny-1")  # 11 characters
    with pytest.raises(sb.PassphraseRejected):
        broker.initialize("password1234")  # 12 characters, on the common list
    with pytest.raises(sb.PassphraseRejected):
        broker.initialize("  PassWord1234 ")  # 12 once stripped, on the common list
    assert not broker.is_initialized()
    assert store.items == {}
    broker.initialize("FAKE-pass-12")  # exactly 12 characters is accepted
    assert broker.is_initialized()


def test_common_passphrase_list_entries_meet_the_minimum_length():
    assert all(len(entry) >= sb.PASSPHRASE_MIN_LENGTH for entry in sb._COMMON_PASSPHRASES)


def test_change_passphrase_rejects_a_short_new_passphrase(broker, store):
    broker.initialize(PASSPHRASE)
    before = store.items[sb.TARGET_PASSPHRASE_WRAP]
    with pytest.raises(sb.PassphraseRejected):
        broker.change_passphrase(PASSPHRASE, "FAKE-tiny-1")
    assert store.items[sb.TARGET_PASSPHRASE_WRAP] == before


def test_initialize_twice_is_refused(broker):
    broker.initialize(PASSPHRASE)
    with pytest.raises(sb.AlreadyInitialized):
        broker.initialize(PASSPHRASE)


def test_initialize_refuses_to_replace_a_vault_whose_wraps_are_missing(vault_dir, store):
    first = sb.SecretsBroker(vault_dir, store)
    first.initialize(PASSPHRASE)
    _put(first)
    first.close()
    lost = sb.SecretsBroker(vault_dir, InMemoryCredentialStore())
    with pytest.raises(sb.AlreadyInitialized):
        lost.initialize(PASSPHRASE)
    assert not lost.is_initialized()


# -- switches ---------------------------------------------------------------


def test_switched_off_secret_is_refused_and_switch_on_allows_it(broker):
    broker.initialize(PASSPHRASE)
    _put(broker)
    broker.set_enabled(False, name=NAME)
    with pytest.raises(sb.SecretDisabled):
        broker.run_with_secret(NAME, [PY, "-c", "pass"])
    broker.set_enabled(True, name=NAME)
    assert broker.run_with_secret(NAME, [PY, "-c", "pass"]).exit_code == 0


def test_switched_off_group_refuses_every_secret_in_it(broker):
    broker.initialize(PASSPHRASE)
    _put(broker)
    broker.set_enabled(False, group=GROUP)
    assert broker.is_enabled(NAME) is False
    with pytest.raises(sb.SecretDisabled):
        broker.run_with_secret(NAME, [PY, "-c", "pass"])
    broker.set_enabled(True, group=GROUP)
    assert broker.is_enabled(NAME) is True


def test_switch_takes_exactly_one_target_and_must_exist(broker):
    broker.initialize(PASSPHRASE)
    _put(broker)
    with pytest.raises(sb.InvalidInput):
        broker.set_enabled(False)
    with pytest.raises(sb.InvalidInput):
        broker.set_enabled(False, name=NAME, group=GROUP)
    with pytest.raises(sb.UnknownSecret):
        broker.set_enabled(False, name="FAKE_MISSING_KEY")
    with pytest.raises(sb.UnknownSecret):
        broker.set_enabled(False, group="no-such-group")


def test_switches_default_on_at_unlock_and_everything_is_off_when_closed(vault_dir, store):
    first = sb.SecretsBroker(vault_dir, store)
    first.initialize(PASSPHRASE)
    _put(first)
    first.set_enabled(False, name=NAME)
    first.close()
    assert first.is_enabled(NAME) is False  # closed means off
    first.unlock(PASSPHRASE)
    assert first.is_enabled(NAME) is True  # default on at each unlock
    first.close()


def test_switch_state_is_not_written_to_disk(broker, store, vault_dir):
    broker.initialize(PASSPHRASE)
    _put(broker)
    vault_bytes = (vault_dir / sb.VAULT_FILE_NAME).read_bytes()
    store_before = dict(store.items)
    broker.set_enabled(False, name=NAME)
    broker.close()
    assert (vault_dir / sb.VAULT_FILE_NAME).read_bytes() == vault_bytes
    assert store.items == store_before


def test_set_enabled_requires_the_broker_to_be_unlocked(broker):
    broker.initialize(PASSPHRASE)
    _put(broker)
    broker.close()
    with pytest.raises(sb.BrokerLocked):
        broker.set_enabled(False, name=NAME)


# -- run wrapper ------------------------------------------------------------


def test_command_not_on_the_allowlist_is_refused(broker, vault_dir):
    broker.initialize(PASSPHRASE)
    _put(broker, allowed=[PY])
    with pytest.raises(sb.CommandNotAllowed):
        broker.run_with_secret(NAME, [str(vault_dir / "not-allowed-tool"), "-c", "pass"])
    sibling = os.path.join(os.path.dirname(PY), "other-python")
    with pytest.raises(sb.CommandNotAllowed):
        broker.run_with_secret(NAME, [sibling, "-c", "pass"])
    with pytest.raises(sb.CommandNotAllowed):
        broker.run_with_secret(NAME, ["python", "-c", "pass"])  # relative, not absolute


def test_run_refused_while_locked(broker):
    broker.initialize(PASSPHRASE)
    _put(broker)
    broker.close()
    with pytest.raises(sb.BrokerLocked):
        broker.run_with_secret(NAME, [PY, "-c", "pass"])


def test_run_rejects_malformed_argv(broker):
    broker.initialize(PASSPHRASE)
    _put(broker)
    with pytest.raises(sb.InvalidInput):
        broker.run_with_secret(NAME, [])
    with pytest.raises(sb.InvalidInput):
        broker.run_with_secret(NAME, PY)  # a bare string, not a list


def test_high_risk_secret_needs_approval_and_does_not_run(broker, tmp_path):
    broker.initialize(PASSPHRASE)
    _put(broker, risk="high")
    marker = tmp_path / "ran.txt"
    result = broker.run_with_secret(NAME, [PY, "-c", f"open({str(marker)!r}, 'w').close()"])
    assert result.status == "approval_required"
    assert result.exit_code is None
    assert not marker.exists()


def test_put_secret_defaults_to_high_risk(broker):
    broker.initialize(PASSPHRASE)
    broker.put_secret(NAME, VALUE, group=GROUP, purpose=PURPOSE, allowed_commands=[PY])
    assert broker.run_with_secret(NAME, [PY, "-c", "pass"]).status == "approval_required"


def test_put_secret_rejects_an_unknown_risk_tier(broker):
    broker.initialize(PASSPHRASE)
    with pytest.raises(sb.InvalidInput):
        _put(broker, risk="medium")


def test_value_reaches_only_the_child_environment(broker, monkeypatch):
    monkeypatch.delenv(NAME, raising=False)
    broker.initialize(PASSPHRASE)
    _put(broker)
    result = broker.run_with_secret(NAME, [PY, "-c", _match_code(NAME, VALUE)])
    assert result.stdout == "match"
    assert NAME not in os.environ


def test_timeout_returns_timed_out_status(broker):
    broker.initialize(PASSPHRASE)
    _put(broker)
    result = broker.run_with_secret(
        NAME, [PY, "-c", "import time; time.sleep(30)"], timeout=1.0
    )
    assert result.status == "timed_out"
    assert result.exit_code is None


def test_missing_executable_raises_broker_error_without_the_value(broker, vault_dir):
    broker.initialize(PASSPHRASE)
    missing = str(vault_dir / "no-such-binary.exe")
    _put(broker, allowed=[missing])
    with pytest.raises(sb.BrokerError) as info:
        broker.run_with_secret(NAME, [missing])
    assert VALUE not in str(info.value)


# -- redaction --------------------------------------------------------------


def test_redact_removes_raw_base64_url_and_hex_forms():
    value = SPECIAL_VALUE.encode("utf-8")
    b64 = base64.b64encode(value)
    quoted = urllib.parse.quote(SPECIAL_VALUE, safe="").encode("ascii")
    quoted_lower = quoted.replace(b"%2F", b"%2f").replace(b"%2B", b"%2b").replace(b"%3D", b"%3d")
    hex_form = value.hex().encode("ascii")
    data = b"\n".join([value, b64, quoted, quoted_lower, hex_form, b"keep me"])
    out = sb.redact(data, value)
    for form in (value, b64, quoted, quoted_lower, hex_form):
        assert form not in out
    assert b"FAKE" not in out
    assert out.count(sb.REDACTED_TOKEN) == 5
    assert b"keep me" in out


def test_run_output_is_redacted_in_raw_base64_and_url_forms(broker):
    broker.initialize(PASSPHRASE)
    _put(broker, value=SPECIAL_VALUE)
    code = (
        "import base64, os, sys, urllib.parse as u\n"
        f"v = os.environ[{NAME!r}]\n"
        "print(v)\n"
        "print(base64.b64encode(v.encode()).decode())\n"
        "print(u.quote(v, safe=''))\n"
        "sys.stderr.write(v)\n"
    )
    result = broker.run_with_secret(NAME, [PY, "-c", code])
    assert result.exit_code == 0
    assert result.stdout.count("[REDACTED]") == 3
    assert result.stderr == "[REDACTED]"
    for leaked in (
        SPECIAL_VALUE,
        base64.b64encode(SPECIAL_VALUE.encode("utf-8")).decode("ascii"),
        urllib.parse.quote(SPECIAL_VALUE, safe=""),
    ):
        assert leaked not in result.stdout


# -- audit, storage, integrity --------------------------------------------


def test_audit_log_records_names_and_outcomes_never_values(broker, vault_dir):
    broker.initialize(PASSPHRASE)
    _put(broker, value=SPECIAL_VALUE)
    broker.run_with_secret(NAME, [PY, "-c", "print('ok')"])
    with pytest.raises(sb.CommandNotAllowed):
        broker.run_with_secret(NAME, ["python", "-c", "pass"])
    text = (vault_dir / sb.AUDIT_FILE_NAME).read_text(encoding="utf-8")
    entries = [json.loads(line) for line in text.splitlines()]
    assert [entry["outcome"] for entry in entries] == [
        "started",
        "completed",
        "refused_command_not_allowed",
    ]
    assert entries[1]["exit_code"] == 0
    assert all({"ts", "secret", "command", "outcome"} <= set(entry) for entry in entries)
    assert all(entry["secret"] == NAME for entry in entries)
    for leaked in (
        SPECIAL_VALUE,
        base64.b64encode(SPECIAL_VALUE.encode("utf-8")).decode("ascii"),
        urllib.parse.quote(SPECIAL_VALUE, safe=""),
        PASSPHRASE,
    ):
        assert leaked not in text


def test_audit_failure_blocks_the_run(broker, tmp_path, monkeypatch):
    broker.initialize(PASSPHRASE)
    _put(broker)
    marker = tmp_path / "ran.txt"

    def failing_append(path, line):
        raise OSError("disk full")

    monkeypatch.setattr(sb, "_append_audit_line", failing_append)
    with pytest.raises(sb.AuditUnavailable):
        broker.run_with_secret(NAME, [PY, "-c", f"open({str(marker)!r}, 'w').close()"])
    assert not marker.exists()


def test_vault_file_holds_no_plaintext_value_or_passphrase(broker, vault_dir):
    broker.initialize(PASSPHRASE)
    _put(broker, value=SPECIAL_VALUE)
    raw = (vault_dir / sb.VAULT_FILE_NAME).read_bytes()
    assert SPECIAL_VALUE.encode("utf-8") not in raw
    assert base64.b64encode(SPECIAL_VALUE.encode("utf-8")) not in raw
    assert PASSPHRASE.encode("utf-8") not in raw


def test_list_names_returns_names_groups_and_purposes_only(broker):
    broker.initialize(PASSPHRASE)
    _put(broker, name="FAKE_ZETA_KEY", group="g2")
    _put(broker, name="FAKE_ALPHA_KEY", group="g1")
    broker.close()
    listing = broker.list_names()
    assert listing == [
        {"name": "FAKE_ALPHA_KEY", "group": "g1", "purpose": PURPOSE},
        {"name": "FAKE_ZETA_KEY", "group": "g2", "purpose": PURPOSE},
    ]
    assert VALUE not in json.dumps(listing)


@pytest.mark.parametrize("bad_name", ["bad name", "1STARTS_WITH_DIGIT", "trailing\n", "", "A" * 129])
def test_invalid_secret_names_are_rejected(broker, bad_name):
    broker.initialize(PASSPHRASE)
    with pytest.raises(sb.InvalidInput):
        _put(broker, name=bad_name)


@pytest.mark.parametrize("reserved", ["PATH", "pythonpath", "LD_PRELOAD"])
def test_reserved_loader_variable_names_are_rejected(broker, reserved):
    broker.initialize(PASSPHRASE)
    with pytest.raises(sb.InvalidInput):
        _put(broker, name=reserved)


def test_allowed_commands_must_be_absolute_paths(broker):
    broker.initialize(PASSPHRASE)
    with pytest.raises(sb.InvalidInput):
        broker.put_secret(NAME, VALUE, group=GROUP, purpose=PURPOSE, allowed_commands=["python"])
    with pytest.raises(sb.InvalidInput):
        broker.put_secret(NAME, VALUE, group=GROUP, purpose=PURPOSE, allowed_commands=[])


def test_values_must_be_at_least_eight_bytes(broker):
    broker.initialize(PASSPHRASE)
    with pytest.raises(sb.InvalidInput):
        _put(broker, value="short")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda record: record.__setitem__("risk", "routine"),
        lambda record: record.__setitem__("allowed_commands", record["allowed_commands"] + ["/usr/bin/evil"]),
        lambda record: record.__setitem__("group", "other-group"),
        lambda record: record.__setitem__("ct", _flip_first_byte(record["ct"])),
    ],
    ids=["risk", "allowlist", "group", "ciphertext"],
)
def test_tampering_with_a_record_fails_the_integrity_check(vault_dir, store, mutate):
    first = sb.SecretsBroker(vault_dir, store)
    first.initialize(PASSPHRASE)
    _put(first, risk="high")
    first.close()
    path = vault_dir / sb.VAULT_FILE_NAME
    doc = json.loads(path.read_text(encoding="utf-8"))
    mutate(doc["secrets"][NAME])
    path.write_text(json.dumps(doc), encoding="utf-8")

    second = sb.SecretsBroker(vault_dir, store)
    with pytest.raises(sb.IntegrityError) as info:
        second.unlock(PASSPHRASE)
    assert NAME in str(info.value)
    assert VALUE not in str(info.value)
    assert not second.is_unlocked()


# -- credential store adapters ----------------------------------------------


def test_linux_credential_store_is_an_unimplemented_stub():
    with pytest.raises(NotImplementedError):
        sb.LinuxCredentialStoreStub()


@pytest.mark.skipif(sys.platform == "win32", reason="non-Windows default store")
def test_default_store_on_non_windows_is_the_stub():
    with pytest.raises(NotImplementedError):
        sb.default_credential_store()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows default store")
def test_default_store_on_windows_constructs_without_touching_the_store():
    # Constructing the adapter only loads advapi32; it does not read or write.
    store = sb.default_credential_store()
    assert isinstance(store, sb.WindowsCredentialManager)
