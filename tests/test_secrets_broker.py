"""Tests for the local secrets broker, slices 1 and 2.

Every credential here is a fake value. Each test injects InMemoryCredentialStore,
so the OS credential store is never touched.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
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
PUBLIC_VALUE = "FAKE-public-1.2.3"
INTERNAL_VALUE = "FAKE-internal-port-8080"
# Replaced at authoring time with one bundled common-list entry (see test body).
COMMON_LIST_CANDIDATE = "000000000000"


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
    sensitivity: str = "SECRET",
    allowed: list[str] | None = None,
) -> None:
    broker.put_secret(
        name,
        value,
        group=group,
        purpose=PURPOSE,
        allowed_commands=allowed if allowed is not None else [PY],
        sensitivity=sensitivity,
    )


def _record(vault_dir: Path, name: str) -> dict:
    return json.loads((vault_dir / sb.VAULT_FILE_NAME).read_text(encoding="utf-8"))["secrets"][name]


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


def test_passphrase_of_eleven_characters_is_rejected(broker, store):
    assert len("FAKE-tiny-1") == 11
    with pytest.raises(sb.PassphraseRejected):
        broker.initialize("FAKE-tiny-1")
    assert not broker.is_initialized()
    assert store.items == {}


def test_passphrase_of_twelve_characters_is_accepted(broker):
    broker.initialize("FAKE-pass-12")  # exactly 12 characters
    assert broker.is_initialized()


def test_passphrase_on_the_common_list_is_rejected_without_echoing_it(broker, store):
    digest = hashlib.sha256(COMMON_LIST_CANDIDATE.encode("utf-8")).hexdigest()
    assert digest in sb._common_passphrase_digests()  # the candidate is a bundled list entry
    with pytest.raises(sb.PassphraseRejected) as info:
        broker.initialize(COMMON_LIST_CANDIDATE)
    assert COMMON_LIST_CANDIDATE not in str(info.value)
    with pytest.raises(sb.PassphraseRejected):
        broker.initialize("  " + COMMON_LIST_CANDIDATE.upper() + "  ")  # normalised before the check
    assert not broker.is_initialized()
    assert store.items == {}


def test_common_passphrase_file_holds_digests_only():
    lines = [
        line
        for line in sb.COMMON_PASSPHRASE_FILE.read_text(encoding="ascii").splitlines()
        if line and not line.startswith("#")
    ]
    assert all(sb._DIGEST_RE.fullmatch(line) for line in lines)
    assert len(lines) == len(set(lines))
    assert len(sb._common_passphrase_digests()) == len(lines) >= sb.COMMON_PASSPHRASE_MIN_DIGESTS


def test_missing_common_list_refuses_passphrases_instead_of_allowing_them(broker, store, monkeypatch, tmp_path):
    monkeypatch.setattr(sb, "COMMON_PASSPHRASE_FILE", tmp_path / "absent.txt")
    sb._common_passphrase_digests.cache_clear()
    try:
        with pytest.raises(sb.BrokerError):
            broker.initialize(PASSPHRASE)
        assert store.items == {}
    finally:
        sb._common_passphrase_digests.cache_clear()


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


def test_sensitive_item_needs_approval_and_does_not_run(broker, tmp_path):
    broker.initialize(PASSPHRASE)
    _put(broker, sensitivity="SENSITIVE")
    marker = tmp_path / "ran.txt"
    result = broker.run_with_secret(NAME, [PY, "-c", f"open({str(marker)!r}, 'w').close()"])
    assert result.status == "approval_required"
    assert result.exit_code is None
    assert not marker.exists()


def test_secret_item_runs_without_approval(broker):
    broker.initialize(PASSPHRASE)
    _put(broker, sensitivity="SECRET")
    assert broker.run_with_secret(NAME, [PY, "-c", "pass"]).exit_code == 0


def test_put_without_a_class_is_a_type_error(broker):
    broker.initialize(PASSPHRASE)
    with pytest.raises(TypeError):
        broker.put_secret(NAME, VALUE, group=GROUP, purpose=PURPOSE, allowed_commands=[PY])
    assert broker.list_names() == []


def test_plain_item_runs_with_its_value_and_the_output_is_redacted(broker):
    broker.initialize(PASSPHRASE)
    _put(broker, name="FAKE_PUBLIC_ITEM", value=PUBLIC_VALUE, sensitivity="PUBLIC")
    check = broker.run_with_secret(
        "FAKE_PUBLIC_ITEM", [PY, "-c", _match_code("FAKE_PUBLIC_ITEM", PUBLIC_VALUE)]
    )
    assert check.status == "completed"
    assert check.stdout == "match"
    echo = broker.run_with_secret(
        "FAKE_PUBLIC_ITEM",
        [PY, "-c", "import os; print(os.environ['FAKE_PUBLIC_ITEM'])"],
    )
    assert PUBLIC_VALUE not in echo.stdout
    assert echo.stdout.strip() == "[REDACTED]"


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


# -- approver for SENSITIVE items (slice 2) ---------------------------------


class _RecordingApprover:
    """Approver stand-in: records each request and returns (or raises) a fixed answer."""

    def __init__(self, answer: object) -> None:
        self.answer = answer
        self.requests: list[sb.ApprovalRequest] = []

    def __call__(self, request: sb.ApprovalRequest) -> object:
        self.requests.append(request)
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


def _audit_entries(vault_dir: Path) -> tuple[str, list[dict]]:
    text = (vault_dir / sb.AUDIT_FILE_NAME).read_text(encoding="utf-8")
    return text, [json.loads(line) for line in text.splitlines()]


def _marker_code(marker: Path) -> str:
    return f"open({str(marker)!r}, 'w').close()"


def test_without_an_approver_a_sensitive_run_is_unchanged(broker, vault_dir, tmp_path):
    broker.initialize(PASSPHRASE)
    _put(broker, sensitivity="SENSITIVE")
    marker = tmp_path / "ran.txt"
    result = broker.run_with_secret(NAME, [PY, "-c", _marker_code(marker)], approver=None)
    assert result.status == "approval_required"
    assert not marker.exists()
    _, entries = _audit_entries(vault_dir)
    assert [entry["outcome"] for entry in entries] == ["approval_required"]


def test_approved_sensitive_run_executes_once_and_logs_approved_then_run(broker, vault_dir, tmp_path):
    broker.initialize(PASSPHRASE)
    _put(broker, sensitivity="SENSITIVE", value=SPECIAL_VALUE)
    marker = tmp_path / "ran.txt"
    argv = [PY, "-c", _marker_code(marker)]
    approver = _RecordingApprover(True)
    result = broker.run_with_secret(NAME, argv, approver=approver, client="fake client")
    assert result.status == "completed"
    assert result.exit_code == 0
    assert marker.exists()
    assert approver.requests == [
        sb.ApprovalRequest(
            name=NAME,
            purpose=PURPOSE,
            group=GROUP,
            command=PY,
            argv=tuple(argv),
            client="fake client",
        )
    ]
    text, entries = _audit_entries(vault_dir)
    assert [entry["outcome"] for entry in entries] == ["approved", "started", "completed"]
    assert entries[0]["client"] == "fake client"
    assert "client" not in entries[1]
    for leaked in (SPECIAL_VALUE, base64.b64encode(SPECIAL_VALUE.encode("utf-8")).decode("ascii")):
        assert leaked not in text


def test_denied_sensitive_run_does_not_run_and_logs_one_denied_line(broker, vault_dir, tmp_path):
    broker.initialize(PASSPHRASE)
    _put(broker, sensitivity="SENSITIVE")
    marker = tmp_path / "ran.txt"
    approver = _RecordingApprover(False)
    result = broker.run_with_secret(
        NAME, [PY, "-c", _marker_code(marker)], approver=approver, client="fake client"
    )
    assert result.status == "denied"
    assert result.exit_code is None
    assert result.stdout == "" and result.stderr == ""
    assert not marker.exists()
    assert len(approver.requests) == 1
    _, entries = _audit_entries(vault_dir)
    assert [entry["outcome"] for entry in entries] == ["denied"]
    assert entries[0]["secret"] == NAME
    assert entries[0]["client"] == "fake client"


@pytest.mark.parametrize(
    "answer",
    ["yes", 1, None, RuntimeError("FAKE prompt failure")],
    ids=["string", "integer", "none", "raises"],
)
def test_anything_but_an_exact_true_denies(broker, vault_dir, tmp_path, answer):
    broker.initialize(PASSPHRASE)
    _put(broker, sensitivity="SENSITIVE")
    marker = tmp_path / "ran.txt"
    result = broker.run_with_secret(NAME, [PY, "-c", _marker_code(marker)], approver=_RecordingApprover(answer))
    assert result.status == "denied"
    assert not marker.exists()
    _, entries = _audit_entries(vault_dir)
    assert [entry["outcome"] for entry in entries] == ["denied"]


def test_approved_sensitive_output_is_still_redacted(broker):
    broker.initialize(PASSPHRASE)
    _put(broker, value=SPECIAL_VALUE, sensitivity="SENSITIVE")
    code = f"import os; print(os.environ[{NAME!r}])"
    result = broker.run_with_secret(NAME, [PY, "-c", code], approver=_RecordingApprover(True))
    assert result.status == "completed"
    assert result.stdout.strip() == "[REDACTED]"


def test_approver_is_not_asked_when_an_earlier_check_refuses(broker, tmp_path):
    broker.initialize(PASSPHRASE)
    _put(broker, sensitivity="SENSITIVE")
    approver = _RecordingApprover(True)
    with pytest.raises(sb.CommandNotAllowed):
        broker.run_with_secret(NAME, ["python", "-c", "pass"], approver=approver)
    broker.set_enabled(False, name=NAME)
    with pytest.raises(sb.SecretDisabled):
        broker.run_with_secret(NAME, [PY, "-c", "pass"], approver=approver)
    assert approver.requests == []


def test_approver_is_not_asked_about_secret_or_plain_items(broker):
    broker.initialize(PASSPHRASE)
    _put(broker, name="FAKE_SECRET_ITEM", sensitivity="SECRET")
    _put(broker, name="FAKE_PLAIN_ITEM2", value=PUBLIC_VALUE, sensitivity="PUBLIC")
    approver = _RecordingApprover(False)
    assert broker.run_with_secret("FAKE_SECRET_ITEM", [PY, "-c", "pass"], approver=approver).status == "completed"
    assert broker.run_with_secret("FAKE_PLAIN_ITEM2", [PY, "-c", "pass"], approver=approver).status == "completed"
    assert approver.requests == []


def test_the_value_is_used_only_after_approval(broker, monkeypatch):
    """The integrity pass decrypts and discards before any approval; the value is used after one.

    Events are recorded in order. Before the approver, the only decrypt is the integrity
    pass. After a denial nothing else is decrypted. After an approval the value is
    decrypted once more, for use in the child's environment.
    """
    broker.initialize(PASSPHRASE)
    _put(broker, sensitivity="SENSITIVE")
    events: list[str] = []
    real_decrypt = sb._decrypt_record

    def spy(master: bytes, name: str, record: dict) -> bytes:
        events.append(f"decrypt:{name}")
        return real_decrypt(master, name, record)

    def approver_answering(answer: bool):
        def ask(request: sb.ApprovalRequest) -> bool:
            events.append("approver")
            return answer

        return ask

    monkeypatch.setattr(sb, "_decrypt_record", spy)
    denied = broker.run_with_secret(NAME, [PY, "-c", "pass"], approver=approver_answering(False))
    assert denied.status == "denied"
    assert events == [f"decrypt:{NAME}", "approver"]
    events.clear()
    approved = broker.run_with_secret(NAME, [PY, "-c", "pass"], approver=approver_answering(True))
    assert approved.status == "completed"
    assert events == [f"decrypt:{NAME}", "approver", f"decrypt:{NAME}"]


def test_approval_audit_failure_blocks_the_run(broker, tmp_path, monkeypatch):
    broker.initialize(PASSPHRASE)
    _put(broker, sensitivity="SENSITIVE")
    marker = tmp_path / "ran.txt"

    def failing_append(path, line):
        raise OSError("disk full")

    monkeypatch.setattr(sb, "_append_audit_line", failing_append)
    with pytest.raises(sb.AuditUnavailable):
        broker.run_with_secret(NAME, [PY, "-c", _marker_code(marker)], approver=_RecordingApprover(True))
    assert not marker.exists()


@pytest.mark.parametrize("client", ["", "   ", "x" * 201, "bad\x00client", 7])
def test_client_label_is_validated(broker, client):
    broker.initialize(PASSPHRASE)
    _put(broker, sensitivity="SENSITIVE")
    with pytest.raises(sb.InvalidInput):
        broker.run_with_secret(NAME, [PY, "-c", "pass"], approver=_RecordingApprover(True), client=client)


def test_the_approval_request_has_metadata_fields_only():
    fields = {field.name for field in dataclasses.fields(sb.ApprovalRequest)}
    assert fields == {"name", "purpose", "group", "command", "argv", "client"}


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


def test_list_names_returns_name_group_purpose_and_class_never_a_value(broker):
    broker.initialize(PASSPHRASE)
    _put(broker, name="FAKE_ZETA_KEY", group="g2")
    _put(broker, name="FAKE_ALPHA_KEY", group="g1", sensitivity="SENSITIVE")
    _put(broker, name="FAKE_PUBLIC_ITEM", value=PUBLIC_VALUE, group="g3", sensitivity="PUBLIC")
    broker.close()
    listing = broker.list_names()  # works while locked
    assert listing == [
        {"name": "FAKE_ALPHA_KEY", "group": "g1", "purpose": PURPOSE, "class": "SENSITIVE"},
        {"name": "FAKE_PUBLIC_ITEM", "group": "g3", "purpose": PURPOSE, "class": "PUBLIC"},
        {"name": "FAKE_ZETA_KEY", "group": "g2", "purpose": PURPOSE, "class": "SECRET"},
    ]
    dumped = json.dumps(listing)
    assert VALUE not in dumped
    assert PUBLIC_VALUE not in dumped


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
        broker.put_secret(
            NAME, VALUE, group=GROUP, purpose=PURPOSE, allowed_commands=["python"], sensitivity="SECRET"
        )
    with pytest.raises(sb.InvalidInput):
        broker.put_secret(
            NAME, VALUE, group=GROUP, purpose=PURPOSE, allowed_commands=[], sensitivity="SECRET"
        )


def test_values_must_be_at_least_eight_bytes(broker):
    broker.initialize(PASSPHRASE)
    with pytest.raises(sb.InvalidInput):
        _put(broker, value="short")


# -- value floor: 8 bytes for every class ------------------------------------
#
# Slice 2 proposed a 1-byte floor for PUBLIC and INTERNAL. It is not adopted: the
# 8-byte floor holds for every class, because a 1-byte value would redact ordinary output.


@pytest.mark.parametrize("cls", ["PUBLIC", "INTERNAL", "SECRET", "SENSITIVE"])
def test_a_one_byte_value_is_refused_for_every_class(broker, cls):
    broker.initialize(PASSPHRASE)
    with pytest.raises(sb.InvalidInput):
        _put(broker, name="FAKE_ONE_BYTE", value="x", sensitivity=cls)
    assert broker.list_names() == []


@pytest.mark.parametrize("cls", ["PUBLIC", "INTERNAL"])
def test_an_empty_plain_value_is_still_refused(broker, cls):
    broker.initialize(PASSPHRASE)
    with pytest.raises(sb.InvalidInput):
        _put(broker, name="FAKE_EMPTY_ITEM", value="", sensitivity=cls)
    assert broker.list_names() == []


@pytest.mark.parametrize("cls", ["PUBLIC", "INTERNAL", "SECRET", "SENSITIVE"])
def test_a_seven_byte_value_is_refused_for_every_class(broker, cls):
    broker.initialize(PASSPHRASE)
    with pytest.raises(sb.InvalidInput):
        _put(broker, name="FAKE_SHORT_ITEM", value="1234567", sensitivity=cls)  # 7 bytes
    assert broker.list_names() == []


@pytest.mark.parametrize("cls", ["PUBLIC", "INTERNAL", "SECRET", "SENSITIVE"])
def test_an_eight_byte_value_is_accepted_for_every_class(broker, cls):
    broker.initialize(PASSPHRASE)
    _put(broker, name="FAKE_EIGHT_ITEM", value="12345678", sensitivity=cls)
    assert [row["name"] for row in broker.list_names()] == ["FAKE_EIGHT_ITEM"]


def test_the_floor_counts_utf8_bytes_not_characters(broker):
    broker.initialize(PASSPHRASE)
    with pytest.raises(sb.InvalidInput):
        _put(broker, name="FAKE_UTF8_SHORT", value="ééé", sensitivity="SECRET")  # 3 chars, 6 bytes
    _put(broker, name="FAKE_UTF8_OK", value="éééé", sensitivity="SECRET")  # 4 chars, 8 bytes
    assert [row["name"] for row in broker.list_names()] == ["FAKE_UTF8_OK"]


@pytest.mark.parametrize("cls", ["PUBLIC", "INTERNAL", "SECRET", "SENSITIVE"])
def test_the_maximum_stays_8192_bytes_for_every_class(broker, cls):
    broker.initialize(PASSPHRASE)
    _put(broker, name="FAKE_MAX_ITEM", value="a" * sb.MAX_VALUE_BYTES, sensitivity=cls)
    with pytest.raises(sb.InvalidInput):
        _put(broker, name="FAKE_OVER_ITEM", value="a" * (sb.MAX_VALUE_BYTES + 1), sensitivity=cls)


def test_encode_value_has_one_floor_for_every_class():
    assert sb._encode_value("12345678") == b"12345678"
    with pytest.raises(sb.InvalidInput):
        sb._encode_value("1234567")
    with pytest.raises(sb.InvalidInput):
        sb._encode_value("1234567\x00")  # 8 bytes, but a NUL is refused
    with pytest.raises(sb.InvalidInput):
        sb._encode_value(123)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda record: record.__setitem__("allowed_commands", record["allowed_commands"] + ["/usr/bin/evil"]),
        lambda record: record.__setitem__("group", "other-group"),
        lambda record: record.__setitem__("ct", _flip_first_byte(record["ct"])),
    ],
    ids=["allowlist", "group", "ciphertext"],
)
def test_tampering_with_a_record_fails_the_integrity_check(vault_dir, store, mutate):
    first = sb.SecretsBroker(vault_dir, store)
    first.initialize(PASSPHRASE)
    _put(first, sensitivity="SENSITIVE")
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


# -- sensitivity classes ----------------------------------------------------


@pytest.mark.parametrize("cls", ["PUBLIC", "INTERNAL"])
def test_public_and_internal_values_are_stored_unencrypted(broker, vault_dir, cls):
    broker.initialize(PASSPHRASE)
    value = PUBLIC_VALUE if cls == "PUBLIC" else INTERNAL_VALUE
    _put(broker, name="FAKE_PLAIN_ITEM", value=value, sensitivity=cls)
    record = _record(vault_dir, "FAKE_PLAIN_ITEM")
    assert record["class"] == cls
    assert record["value"] == value
    assert "nonce" not in record and "ct" not in record
    assert value.encode("utf-8") in (vault_dir / sb.VAULT_FILE_NAME).read_bytes()


@pytest.mark.parametrize("cls", ["SECRET", "SENSITIVE"])
def test_secret_and_sensitive_values_are_encrypted(broker, vault_dir, cls):
    broker.initialize(PASSPHRASE)
    _put(broker, name="FAKE_ENC_ITEM", value=SPECIAL_VALUE, sensitivity=cls)
    record = _record(vault_dir, "FAKE_ENC_ITEM")
    assert record["class"] == cls
    assert "value" not in record
    assert isinstance(record["nonce"], str) and isinstance(record["ct"], str)
    raw = (vault_dir / sb.VAULT_FILE_NAME).read_bytes()
    assert SPECIAL_VALUE.encode("utf-8") not in raw
    assert base64.b64encode(SPECIAL_VALUE.encode("utf-8")) not in raw


@pytest.mark.parametrize("unset", [None, "", "bogus", "public", "Sensitive"])
def test_unknown_or_unset_class_is_refused_and_writes_nothing(broker, vault_dir, unset):
    broker.initialize(PASSPHRASE)
    with pytest.raises(sb.InvalidInput):
        _put(broker, name="FAKE_UNSET_ITEM", value=SPECIAL_VALUE, sensitivity=unset)
    assert not (vault_dir / sb.VAULT_FILE_NAME).exists()
    assert broker.list_names() == []


def test_plain_entry_with_an_unknown_class_is_refused(broker, vault_dir):
    broker.initialize(PASSPHRASE)
    _put(broker)
    path = vault_dir / sb.VAULT_FILE_NAME
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["secrets"][NAME] = {
        "group": GROUP,
        "purpose": PURPOSE,
        "allowed_commands": [PY],
        "class": "bogus",
        "value": VALUE,
    }
    path.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(sb.BrokerError) as info:
        broker.list_names()
    assert NAME in str(info.value)
    assert VALUE not in str(info.value)


@pytest.mark.parametrize(
    "old_class, new_class, expected",
    [
        ("SECRET", "SENSITIVE", sb.IntegrityError),
        ("SENSITIVE", "SECRET", sb.IntegrityError),
        ("SECRET", "bogus", sb.BrokerError),
        ("SECRET", "INTERNAL", sb.BrokerError),
        ("SECRET", "PUBLIC", sb.BrokerError),
    ],
)
def test_changing_a_class_fails_verification(vault_dir, store, old_class, new_class, expected):
    first = sb.SecretsBroker(vault_dir, store)
    first.initialize(PASSPHRASE)
    _put(first, sensitivity=old_class)
    first.close()
    path = vault_dir / sb.VAULT_FILE_NAME
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["secrets"][NAME]["class"] = new_class
    path.write_text(json.dumps(doc), encoding="utf-8")
    second = sb.SecretsBroker(vault_dir, store)
    with pytest.raises(expected) as info:
        second.unlock(PASSPHRASE)
    assert VALUE not in str(info.value)
    assert not second.is_unlocked()


# -- index tag: plain items and the record set -------------------------------


def _edit_vault(vault_dir: Path, mutate) -> None:
    path = vault_dir / sb.VAULT_FILE_NAME
    doc = json.loads(path.read_text(encoding="utf-8"))
    mutate(doc)
    path.write_text(json.dumps(doc), encoding="utf-8")


def _plain_setup(vault_dir: Path, store: InMemoryCredentialStore) -> None:
    first = sb.SecretsBroker(vault_dir, store)
    first.initialize(PASSPHRASE)
    _put(first, name="FAKE_PUBLIC_ITEM", value=PUBLIC_VALUE, sensitivity="PUBLIC")
    _put(first, name=NAME, value=VALUE, sensitivity="SECRET")
    first.close()


def test_editing_a_plain_value_fails_the_index_check_at_unlock(vault_dir, store):
    _plain_setup(vault_dir, store)
    _edit_vault(
        vault_dir,
        lambda doc: doc["secrets"]["FAKE_PUBLIC_ITEM"].__setitem__("value", "FAKE-public-9.9.9"),
    )
    second = sb.SecretsBroker(vault_dir, store)
    with pytest.raises(sb.IntegrityError) as info:
        second.unlock(PASSPHRASE)
    assert "FAKE-public-9.9.9" not in str(info.value)
    assert not second.is_unlocked()


def test_an_item_added_by_hand_fails_the_index_check_at_unlock(vault_dir, store):
    _plain_setup(vault_dir, store)
    injected = {
        "group": GROUP,
        "purpose": PURPOSE,
        "allowed_commands": [PY],
        "class": "PUBLIC",
        "value": "FAKE-injected",
    }
    _edit_vault(vault_dir, lambda doc: doc["secrets"].__setitem__("FAKE_INJECTED", injected))
    second = sb.SecretsBroker(vault_dir, store)
    with pytest.raises(sb.IntegrityError):
        second.unlock(PASSPHRASE)
    assert not second.is_unlocked()


def test_an_item_removed_by_hand_fails_the_index_check_at_unlock(vault_dir, store):
    _plain_setup(vault_dir, store)
    _edit_vault(vault_dir, lambda doc: doc["secrets"].pop("FAKE_PUBLIC_ITEM"))
    second = sb.SecretsBroker(vault_dir, store)
    with pytest.raises(sb.IntegrityError):
        second.unlock(PASSPHRASE)
    assert not second.is_unlocked()


def test_a_vault_without_an_index_tag_is_refused(vault_dir, store):
    _plain_setup(vault_dir, store)
    _edit_vault(vault_dir, lambda doc: doc.pop("index_tag"))
    second = sb.SecretsBroker(vault_dir, store)
    with pytest.raises(sb.IntegrityError):
        second.unlock(PASSPHRASE)
    assert not second.is_unlocked()


def test_put_refuses_to_re_tag_a_vault_edited_after_unlock(vault_dir, store):
    _plain_setup(vault_dir, store)
    broker = sb.SecretsBroker(vault_dir, store)
    broker.unlock(PASSPHRASE)
    try:
        _edit_vault(
            vault_dir,
            lambda doc: doc["secrets"]["FAKE_PUBLIC_ITEM"].__setitem__("value", "FAKE-public-9.9.9"),
        )
        path = vault_dir / sb.VAULT_FILE_NAME
        edited = path.read_bytes()
        with pytest.raises(sb.IntegrityError):
            _put(broker, name="FAKE_NEW_ITEM", value=VALUE, sensitivity="SECRET")
        assert path.read_bytes() == edited  # nothing was written
    finally:
        broker.close()


def test_run_refuses_a_plain_item_edited_after_unlock(vault_dir, store, tmp_path):
    _plain_setup(vault_dir, store)
    broker = sb.SecretsBroker(vault_dir, store)
    broker.unlock(PASSPHRASE)
    marker = tmp_path / "ran.txt"
    try:
        _edit_vault(
            vault_dir,
            lambda doc: doc["secrets"]["FAKE_PUBLIC_ITEM"].__setitem__("value", "FAKE-public-9.9.9"),
        )
        with pytest.raises(sb.IntegrityError):
            broker.run_with_secret(
                "FAKE_PUBLIC_ITEM", [PY, "-c", f"open({str(marker)!r}, 'w').close()"]
            )
        assert not marker.exists()
    finally:
        broker.close()


def test_recovery_wrap_covers_the_encrypted_vault_only(vault_dir, store):
    first = sb.SecretsBroker(vault_dir, store)
    recovery = first.initialize(PASSPHRASE)
    _put(first, name="FAKE_ENC_ITEM", value=SPECIAL_VALUE)
    _put(first, name="FAKE_PUBLIC_ITEM", value=PUBLIC_VALUE, sensitivity="PUBLIC")
    first.close()
    names = {"FAKE_ENC_ITEM", "FAKE_PUBLIC_ITEM"}

    locked = sb.SecretsBroker(vault_dir, store)
    assert {row["name"] for row in locked.list_names()} == names  # no key or wrap needed

    original_wrap = store.items[sb.TARGET_RECOVERY_WRAP]
    store.items[sb.TARGET_RECOVERY_WRAP] = "{}"  # damage the recovery wrap
    with pytest.raises(sb.UnlockFailed):
        locked.unlock_with_recovery(recovery)
    assert {row["name"] for row in locked.list_names()} == names  # plain metadata unaffected

    store.items[sb.TARGET_RECOVERY_WRAP] = original_wrap
    locked.unlock_with_recovery(recovery)
    enc = locked.run_with_secret(
        "FAKE_ENC_ITEM", [PY, "-c", _match_code("FAKE_ENC_ITEM", SPECIAL_VALUE)]
    )
    plain = locked.run_with_secret(
        "FAKE_PUBLIC_ITEM", [PY, "-c", _match_code("FAKE_PUBLIC_ITEM", PUBLIC_VALUE)]
    )
    assert enc.stdout == "match"
    assert plain.stdout == "match"
    locked.close()


def test_audit_log_never_contains_a_plain_or_encrypted_value(broker, vault_dir):
    broker.initialize(PASSPHRASE)
    _put(broker, name="FAKE_PUBLIC_ITEM", value=PUBLIC_VALUE, sensitivity="PUBLIC")
    _put(broker, name="FAKE_SENSITIVE_ITEM", value=SPECIAL_VALUE, sensitivity="SENSITIVE")
    _put(broker, name="FAKE_ENC_ITEM", value=VALUE)
    broker.run_with_secret("FAKE_PUBLIC_ITEM", [PY, "-c", "print('ok')"])
    broker.run_with_secret("FAKE_SENSITIVE_ITEM", [PY, "-c", "pass"])
    broker.run_with_secret("FAKE_ENC_ITEM", [PY, "-c", "pass"])
    text = (vault_dir / sb.AUDIT_FILE_NAME).read_text(encoding="utf-8")
    outcomes = [json.loads(line)["outcome"] for line in text.splitlines()]
    assert outcomes == ["started", "completed", "approval_required", "started", "completed"]
    for leaked in (
        PUBLIC_VALUE,
        SPECIAL_VALUE,
        VALUE,
        base64.b64encode(SPECIAL_VALUE.encode("utf-8")).decode("ascii"),
        PASSPHRASE,
    ):
        assert leaked not in text


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
