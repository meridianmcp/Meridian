"""Audit log tests. The file only grows, never holds credentials, and survives concurrent writers."""

from __future__ import annotations

import datetime
import json
import threading
from pathlib import Path

import pytest

from meridian_inventree.audit import REDACTED, AuditLog


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "audit" / "inventree.jsonl"


def test_append_writes_one_json_line_with_a_utc_timestamp(path: Path) -> None:
    log = AuditLog(path)
    log.append({"event": "op_started", "plan_id": "abc", "client_key": "k1"})
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["event"] == "op_started"
    assert record["plan_id"] == "abc"
    parsed = datetime.datetime.fromisoformat(record["ts"])
    assert parsed.utcoffset() == datetime.timedelta(0)


def test_caller_cannot_set_the_timestamp(path: Path) -> None:
    log = AuditLog(path)
    old = "1999-01-01T00:00:00+00:00"
    log.append({"ts": old, "event": "forged"})
    record = log.read_events()[0]
    assert record["ts"] != old
    assert record["event"] == "forged"


def test_audit_file_only_grows(path: Path) -> None:
    log = AuditLog(path)
    log.append({"event": "one"})
    log.append({"event": "two"})
    before = path.read_bytes()
    log.append({"event": "three"})
    after = path.read_bytes()
    assert len(after) > len(before)
    assert after.startswith(before)
    assert [event["event"] for event in log.read_events()] == ["one", "two", "three"]


def test_existing_content_is_never_truncated(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"event":"external"}\n', encoding="utf-8")
    log = AuditLog(path)
    log.append({"event": "next"})
    assert path.read_text(encoding="utf-8").startswith('{"event":"external"}\n')
    assert [event["event"] for event in log.read_events()] == ["external", "next"]


def test_credentials_are_never_written(path: Path) -> None:
    log = AuditLog(path)
    log.append(
        {
            "event": "bridge",
            "authorization": "Token abc123SECRET",
            "api_token": "abc999",
            "note": "used sk_meridian_TESTSECRET123 and Token zzz777 here",
            "nested": {"items": [{"password": "hunter2"}]},
        }
    )
    text = path.read_text(encoding="utf-8")
    for leaked in ["abc123SECRET", "abc999", "sk_meridian_TESTSECRET123", "zzz777", "hunter2"]:
        assert leaked not in text
    assert REDACTED in text
    record = log.read_events()[0]
    assert record["event"] == "bridge"
    assert record["authorization"] == REDACTED


def test_scrub_leaves_ordinary_values_alone(path: Path) -> None:
    log = AuditLog(path)
    log.append({"event": "plan_approved", "plan_hash": "a" * 64, "client_key": "key-1", "count": 3})
    record = log.read_events()[0]
    assert record["plan_hash"] == "a" * 64
    assert record["client_key"] == "key-1"
    assert record["count"] == 3


def test_non_dict_events_are_refused_and_no_file_is_made(path: Path) -> None:
    log = AuditLog(path)
    with pytest.raises(TypeError):
        log.append(["not", "a", "dict"])  # type: ignore[arg-type]
    assert not path.exists()


def test_each_line_is_plain_ascii(path: Path) -> None:
    log = AuditLog(path)
    log.append({"event": "x", "note": "café   done"})
    raw = path.read_bytes()
    raw.decode("ascii")
    assert raw.count(b"\n") == 1


def test_missing_parent_folders_are_created(tmp_path: Path) -> None:
    deep = tmp_path / "a" / "b" / "audit.jsonl"
    AuditLog(deep).append({"event": "x"})
    assert deep.exists()


def test_reading_a_missing_log_returns_no_events(path: Path) -> None:
    assert AuditLog(path).read_events() == []


def test_succeeded_keys_lists_only_keys_with_an_op_done_event(path: Path) -> None:
    log = AuditLog(path)
    log.append({"event": "op_started", "client_key": "k1"})
    log.append({"event": "op_done", "client_key": "k1"})
    log.append({"event": "op_started", "client_key": "k2"})
    log.append({"event": "op_failed", "client_key": "k2"})
    log.append({"event": "op_done", "client_key": "k3"})
    log.append({"event": "op_skipped", "client_key": "k4"})
    assert log.succeeded_keys() == {"k1", "k3"}


def test_succeeded_keys_is_empty_without_a_log(path: Path) -> None:
    assert AuditLog(path).succeeded_keys() == set()


def test_succeeded_keys_refuses_an_unreadable_log(path: Path) -> None:
    # A damaged line must raise, so the caller can refuse the run instead of guessing.
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"event":"op_done","client_key":"k1"}\nnot json\n', encoding="utf-8")
    with pytest.raises(ValueError):
        AuditLog(path).succeeded_keys()


def test_started_without_settling_event_is_unresolved(path: Path) -> None:
    # A start with nothing after it means the final write may have gone out, so the key stays unresolved.
    log = AuditLog(path)
    log.append({"event": "op_started", "client_key": "k1"})
    assert "k1" in log.unresolved_keys()


def test_started_then_automatic_failure_is_settled(path: Path) -> None:
    # The request was never sent after a definite start, so an automatic failure settles the key.
    log = AuditLog(path)
    log.append({"event": "op_started", "client_key": "k1"})
    log.append({"event": "op_failed", "client_key": "k1"})
    assert "k1" not in log.unresolved_keys()


def test_unknown_then_automatic_failure_stays_unresolved(path: Path) -> None:
    # The earlier request may still have been applied, so an automatic failure cannot clear an unknown outcome.
    log = AuditLog(path)
    log.append({"event": "op_unknown", "client_key": "k1"})
    log.append({"event": "op_failed", "client_key": "k1"})
    assert "k1" in log.unresolved_keys()


def test_unknown_then_manual_failure_resolves(path: Path) -> None:
    # The owner's hand-written failure record settles the write that never reached InvenTree.
    log = AuditLog(path)
    log.append({"event": "op_unknown", "client_key": "k1"})
    log.append({"event": "op_failed", "client_key": "k1", "manual": True})
    assert "k1" not in log.unresolved_keys()


def test_unknown_then_done_resolves(path: Path) -> None:
    log = AuditLog(path)
    log.append({"event": "op_unknown", "client_key": "k1"})
    log.append({"event": "op_done", "client_key": "k1"})
    assert "k1" not in log.unresolved_keys()


def test_concurrent_writers_keep_every_line(path: Path) -> None:
    log = AuditLog(path)

    def worker(number: int) -> None:
        for index in range(25):
            log.append({"event": "tick", "worker": number, "index": index})

    threads = [threading.Thread(target=worker, args=(number,)) for number in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    events = log.read_events()
    assert len(events) == 8 * 25
    assert {(event["worker"], event["index"]) for event in events} == {
        (number, index) for number in range(8) for index in range(25)
    }
