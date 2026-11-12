"""Tests for meridian_inventree.write_params and write_tools. No test touches the network.

Main flows use a real PlanStore under pytest's tmp_path, so the stored hash, approval and
running checks are the ones production runs. Refusals on plan files use files written to disk
the same way, so run_plan is tested against what it actually reloads.
"""

from __future__ import annotations

import copy
import json
import re
import urllib.parse
import uuid
from collections.abc import Callable, Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from meridian_inventree.audit import OP_DONE, OP_STARTED, AuditLog
from meridian_inventree.client import InvenTreeClient, InvenTreeError
from meridian_inventree.plans import PlanStore, compute_plan_hash
from meridian_inventree.write_params import WriteValidationError, as_wire
from meridian_inventree.write_tools import (
    AUDIT_AFTER_NOTE,
    KEY_UNRESOLVED_NOTE,
    MAX_OPERATIONS,
    OPERATION_NAMES,
    PlanRefused,
    check_write_version,
    execute,
    preview,
    run_plan,
)

BASE = "https://inventree.example.com"
TOKEN = "tok_9f8e7d6c5b4a3210"
RANGE: tuple[str, ...] = ("1.2.",)  # the supported version range these tests run under
Timeline = list[tuple[Any, ...]]

_DETAIL = re.compile(r"^/api/(part/category|part|stock/location|stock)/(\d+)/$")
_PART_PATH = re.compile(r"^/api/part/(\d+)/$")
_NOT_FOUND = (404, b'{"detail": "not found"}')
_STOCK_ACTIONS = frozenset({"/api/stock/transfer/", "/api/stock/add/", "/api/stock/remove/"})


class FakeInvenTree:
    """In-memory stand-in for the InvenTree endpoints the write ops use. Records every request."""

    def __init__(self, timeline: Timeline | None = None) -> None:
        self.timeline: Timeline = timeline if timeline is not None else []
        self.requests: list[tuple[str, str]] = []
        self.writes: list[tuple[str, str, dict[str, Any]]] = []
        self.fail_paths: dict[str, int] = {}
        self.version = "1.2.3"
        self.api_status = 200
        self.categories: dict[int, dict[str, Any]] = {3: {"pk": 3, "name": "Passives"}}
        self.locations: dict[int, dict[str, Any]] = {
            1: {"pk": 1, "name": "Shelf A", "parent": None},
            2: {"pk": 2, "name": "Bin 2", "parent": 1},
        }
        self.parts: dict[int, dict[str, Any]] = {
            7: {"pk": 7, "name": "Resistor 10k", "IPN": "R-10K", "active": True, "category": 3},
        }
        self.stock: dict[int, dict[str, Any]] = {100: {"pk": 100, "part": 7, "quantity": "10", "location": 1}}
        self._next_pk = 500

    def __call__(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> tuple[int, bytes]:
        path = urllib.parse.urlsplit(url).path
        self.requests.append((method, path))
        if method == "GET":
            self.timeline.append(("read", path))
            return self._get(path)
        payload: dict[str, Any] = json.loads(body.decode("utf-8")) if body else {}
        self.writes.append((method, path, payload))
        self.timeline.append(("write", method, path))
        forced = self.fail_paths.get(f"{method} {path}")
        if forced is not None:
            return forced, b'{"detail": "rejected"}'
        return self._write(method, path, payload)

    def _get(self, path: str) -> tuple[int, bytes]:
        if path == "/api/":
            return self.api_status, json.dumps({"version": self.version, "apiVersion": 999}).encode("utf-8")
        match = _DETAIL.match(path)
        if match is None:
            return _NOT_FOUND
        tables = {
            "part/category": self.categories,
            "part": self.parts,
            "stock/location": self.locations,
            "stock": self.stock,
        }
        row = tables[match.group(1)].get(int(match.group(2)))
        if row is None:
            return _NOT_FOUND
        return 200, json.dumps(row).encode("utf-8")

    def _write(self, method: str, path: str, payload: dict[str, Any]) -> tuple[int, bytes]:
        if method == "POST" and path == "/api/part/":
            return self._create(self.parts, payload)
        if method == "POST" and path == "/api/stock/location/":
            return self._create(self.locations, payload)
        if method == "POST" and path == "/api/stock/":
            return self._create(self.stock, payload)
        if method == "PATCH" and (match := _PART_PATH.match(path)):
            row = self.parts.get(int(match.group(1)))
            if row is None:
                return _NOT_FOUND
            row.update(payload)
            return 200, json.dumps(row).encode("utf-8")
        if method == "POST" and path in _STOCK_ACTIONS:
            # VERIFY-LIVE: the real answer shape is unconfirmed. The write path needs a usable pk, so the
            # fake answers with the stock item the request named.
            return 200, json.dumps({"pk": payload["items"][0]["pk"]}).encode("utf-8")
        return _NOT_FOUND

    def _create(self, table: dict[int, dict[str, Any]], payload: dict[str, Any]) -> tuple[int, bytes]:
        pk = self._next_pk
        self._next_pk += 1
        table[pk] = {**payload, "pk": pk}
        return 201, json.dumps({**payload, "pk": pk}).encode("utf-8")


class RecordingAudit:
    """Audit sink that can be told to fail at a given (phase, index). Shares a timeline with the transport."""

    def __init__(self, timeline: Timeline, fail_at: frozenset[tuple[str, int]] = frozenset()) -> None:
        self.events: list[dict[str, Any]] = []
        self._timeline = timeline
        self._fail_at = fail_at

    def append(self, event: dict[str, Any]) -> dict[str, Any]:
        if (event["phase"], event["index"]) in self._fail_at:
            raise OSError("audit file unavailable")
        self.events.append(dict(event))
        self._timeline.append(("audit", event["phase"], event["index"]))
        return event

    def succeeded_keys(self) -> set[str]:
        return {event["client_key"] for event in self.events if event["event"] == OP_DONE}

    def read_events(self) -> list[dict[str, Any]]:
        return [dict(event) for event in self.events]

    def unresolved_keys(self) -> set[str]:
        # The same rule as AuditLog.unresolved_keys, applied to the events recorded here.
        return AuditLog.unresolved_keys(self)  # type: ignore[arg-type]


class StatusProbe(RecordingAudit):
    """Records the plan's status on disk each time an operation starts."""

    def __init__(self, timeline: Timeline, store: PlanStore, plan_id: str) -> None:
        super().__init__(timeline)
        self.statuses: list[str] = []
        self._store = store
        self._plan_id = plan_id

    def append(self, event: dict[str, Any]) -> dict[str, Any]:
        if event["phase"] == "started":
            self.statuses.append(self._store.get(self._plan_id)["status"])
        return super().append(event)


class _ResultNotWritten(PlanStore):
    """A store whose final record cannot be written, as if the disk failed after the writes."""

    def mark_executed(self, plan_id: str, results: Any, failed: bool = False) -> dict[str, Any]:
        raise OSError("disk unavailable")


def op(name: str, params: dict[str, Any], key: str) -> dict[str, Any]:
    return {"op": name, "params": params, "client_key": key}


def previews_for(client: InvenTreeClient, operations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [preview(item["op"], item["params"], client) for item in operations]


def approved_in_store(store: PlanStore, client: InvenTreeClient, operations: list[dict[str, Any]]) -> dict[str, Any]:
    """Propose through the real store, approve with its own hash, and return the stored record."""
    proposed = store.propose(operations, previews_for(client, operations))
    store.approve(proposed["plan_id"], proposed["plan_hash"])
    return store.get(proposed["plan_id"])


def approved_plan(
    store: PlanStore,
    operations: list[dict[str, Any]],
    previews: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Propose and approve with placeholder previews, skipping live reads. For refusals that happen first."""
    prevs = previews if previews is not None else [{"before": {}, "after": {}} for _ in operations]
    proposed = store.propose(operations, prevs)
    store.approve(proposed["plan_id"], proposed["plan_hash"])
    return store.get(proposed["plan_id"])


def write_raw(
    store: PlanStore,
    operations: Any,
    previews: Any,
    *,
    plan_hash: str | None = None,
) -> str:
    """Put an approved plan file on disk without propose's checks. Returns its plan_id."""
    plan_id = uuid.uuid4().hex
    digest = plan_hash if plan_hash is not None else compute_plan_hash(operations, previews)
    record = {
        "plan_id": plan_id,
        "operations": operations,
        "previews": previews,
        "status": "approved",
        "plan_hash": digest,
        "approval": {"approved_at": "2026-01-01T00:00:00+00:00", "approved_by": "owner", "approved_hash": digest},
        "running_at": None,
        "executed_at": None,
        "results": None,
    }
    (store.root_dir / f"{plan_id}.json").write_text(json.dumps(record), encoding="utf-8")
    return plan_id


def edit_disk(store: PlanStore, plan_id: str, mutate: Callable[[dict[str, Any]], None]) -> None:
    """Change a plan file on disk without touching its hash."""
    path = store.root_dir / f"{plan_id}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    mutate(data)
    path.write_text(json.dumps(data), encoding="utf-8")


def disk_status(store: PlanStore, plan_id: str) -> str:
    return json.loads((store.root_dir / f"{plan_id}.json").read_text(encoding="utf-8"))["status"]


def paths_written(fake: FakeInvenTree) -> list[str]:
    return [path for _method, path, _body in fake.writes]


@pytest.fixture
def timeline() -> Timeline:
    return []


@pytest.fixture
def fake(timeline: Timeline) -> FakeInvenTree:
    return FakeInvenTree(timeline)


@pytest.fixture
def client(fake: FakeInvenTree) -> InvenTreeClient:
    return InvenTreeClient(BASE, TOKEN, transport=fake)


@pytest.fixture
def store(tmp_path: Path) -> PlanStore:
    # The same audit file the runs in these tests write to, so propose and approve see what runs record.
    return PlanStore(tmp_path / "plans", audit=AuditLog(tmp_path / "audit.jsonl"))


# ---------------------------------------------------------------- operation set


def test_the_six_v1_write_operations_are_registered() -> None:
    assert OPERATION_NAMES == {
        "create_part",
        "update_part",
        "create_location",
        "create_stock_item",
        "transfer_stock",
        "adjust_stock_quantity",
    }


# ---------------------------------------------------------------- validation refuses before any network call


INVALID_CASES: list[tuple[str, Any, str]] = [
    ("create_part", {}, "missing_name"),
    ("create_part", {"name": "   "}, "invalid_name"),
    ("create_part", {"name": "x" * 201}, "invalid_name"),
    ("create_part", {"name": 5}, "invalid_name"),
    ("create_part", {"name": "ok", "colour": "red"}, "unknown_field"),
    ("create_part", {"name": "ok", "category": True}, "invalid_pk"),
    ("create_part", {"name": "ok", "active": "yes"}, "invalid_field"),
    ("update_part", {"name": "x"}, "missing_field"),
    ("update_part", {"pk": 0, "name": "x"}, "invalid_pk"),
    ("update_part", {"pk": "7", "name": "x"}, "invalid_pk"),
    ("update_part", {"pk": 7}, "no_changes"),
    ("create_location", {}, "missing_name"),
    ("create_location", {"name": ""}, "invalid_name"),
    ("create_location", {"name": "ok", "parent": -1}, "invalid_pk"),
    ("create_stock_item", {"part": 7}, "missing_field"),
    ("create_stock_item", {"part": 7, "quantity": -1}, "invalid_quantity"),
    ("create_stock_item", {"part": 7, "quantity": "nan"}, "invalid_quantity"),
    ("create_stock_item", {"part": 7, "quantity": float("inf")}, "invalid_quantity"),
    ("create_stock_item", {"part": 7, "quantity": True}, "invalid_quantity"),
    ("create_stock_item", {"quantity": 1}, "missing_field"),
    ("transfer_stock", {"item": 100, "quantity": 1}, "missing_destination"),
    ("transfer_stock", {"item": 100, "quantity": 0, "destination": 2}, "invalid_quantity"),
    ("transfer_stock", {"item": 100, "quantity": 1, "destination": 2, "junk": 1}, "unknown_field"),
    ("adjust_stock_quantity", {"item": 100, "delta": 0}, "invalid_quantity"),
    ("adjust_stock_quantity", {"item": 100, "delta": "inf"}, "invalid_quantity"),
    ("adjust_stock_quantity", {"item": 100, "delta": "abc"}, "invalid_quantity"),
    ("unknown_op", {}, "unknown_op"),
    ("create_part", ["not", "a", "mapping"], "invalid_params"),
]


@pytest.mark.parametrize(("op_name", "params", "code"), INVALID_CASES)
def test_invalid_params_refused_before_any_network_call(
    fake: FakeInvenTree,
    client: InvenTreeClient,
    op_name: str,
    params: Any,
    code: str,
) -> None:
    with pytest.raises(WriteValidationError) as info:
        preview(op_name, params, client)
    assert info.value.code == code
    assert fake.requests == []


def test_name_at_the_200_character_limit_is_accepted(fake: FakeInvenTree, client: InvenTreeClient) -> None:
    result = preview("create_part", {"name": "x" * 200}, client)
    assert result["after"]["name"] == "x" * 200
    assert fake.requests == []


def test_preview_is_read_only(fake: FakeInvenTree, client: InvenTreeClient) -> None:
    preview("update_part", {"pk": 7, "name": "Resistor 4k7"}, client)
    preview("create_stock_item", {"part": 7, "quantity": 2}, client)
    assert fake.writes == []


def test_execute_refused_outside_the_plan_runner(fake: FakeInvenTree, client: InvenTreeClient) -> None:
    with pytest.raises(PlanRefused) as info:
        execute("create_location", {"name": "A"}, client, "k1")
    assert info.value.code == "execute_outside_plan"
    assert fake.requests == []


# ---------------------------------------------------------------- preview shape and live checks


def test_update_part_preview_shows_before_and_after(client: InvenTreeClient) -> None:
    result = preview("update_part", {"pk": 7, "name": "Resistor 4k7"}, client)
    assert result["before"] == {"name": "Resistor 10k"}
    assert result["after"] == {"name": "Resistor 4k7"}
    assert (result["method"], result["path"]) == ("PATCH", "/api/part/7/")


def test_create_preview_has_an_empty_before(client: InvenTreeClient) -> None:
    assert preview("create_location", {"name": "Drawer"}, client)["before"] == {}


def test_create_part_with_unknown_category_refused_at_preview(fake: FakeInvenTree, client: InvenTreeClient) -> None:
    with pytest.raises(WriteValidationError) as info:
        preview("create_part", {"name": "Film cap", "category": 99}, client)
    assert info.value.code == "missing_reference"
    assert fake.writes == []


def test_non_object_read_is_bad_response() -> None:
    client = InvenTreeClient(BASE, TOKEN, transport=lambda *_args: (200, b"[]"))
    with pytest.raises(InvenTreeError) as info:
        preview("update_part", {"pk": 7, "name": "x"}, client)
    assert info.value.code == "bad_response"


def test_unreadable_stock_quantity_is_bad_response(fake: FakeInvenTree, client: InvenTreeClient) -> None:
    fake.stock[100]["quantity"] = None
    with pytest.raises(InvenTreeError) as info:
        preview("adjust_stock_quantity", {"item": 100, "delta": 1}, client)
    assert info.value.code == "bad_response"


# ---------------------------------------------------------------- stock quantity rules


def test_adjust_that_would_go_negative_is_refused_at_preview(fake: FakeInvenTree, client: InvenTreeClient) -> None:
    with pytest.raises(WriteValidationError) as info:
        preview("adjust_stock_quantity", {"item": 100, "delta": "-11"}, client)
    assert info.value.code == "would_go_negative"
    assert fake.requests == [("GET", "/api/stock/100/")]


def test_adjust_to_exactly_zero_is_allowed(client: InvenTreeClient) -> None:
    result = preview("adjust_stock_quantity", {"item": 100, "delta": "-10"}, client)
    assert result["after"] == {"quantity": "0"}


def test_adjust_sends_add_for_positive_and_remove_for_negative(
    fake: FakeInvenTree,
    client: InvenTreeClient,
    store: PlanStore,
    timeline: Timeline,
) -> None:
    stored = approved_in_store(
        store,
        client,
        [
            op("adjust_stock_quantity", {"item": 100, "delta": "3"}, "add-1"),
            op("adjust_stock_quantity", {"item": 100, "delta": "-4"}, "remove-1"),
        ],
    )
    report = run_plan(stored, client, RecordingAudit(timeline), store=store, supported_prefixes=RANGE)
    assert report.complete
    assert fake.writes == [
        ("POST", "/api/stock/add/", {"items": [{"pk": 100, "quantity": "3"}]}),
        ("POST", "/api/stock/remove/", {"items": [{"pk": 100, "quantity": "4"}]}),
    ]


def test_negative_stock_refused_at_run_time_when_stock_fell_after_approval(
    fake: FakeInvenTree,
    client: InvenTreeClient,
    store: PlanStore,
    timeline: Timeline,
) -> None:
    stored = approved_in_store(store, client, [op("adjust_stock_quantity", {"item": 100, "delta": "-4"}, "adj-1")])
    fake.stock[100]["quantity"] = "2"
    report = run_plan(stored, client, RecordingAudit(timeline), store=store, supported_prefixes=RANGE)
    assert report.outcomes[0].status == "failed"
    # The re-read finds the stock changed since approval, so the run refuses with state_changed and sends
    # nothing. The would_go_negative check that now fails is recorded in the refusal message, not the code.
    # (The code was would_go_negative before the live-state re-read existed.)
    assert report.outcomes[0].error_code == "state_changed"
    assert fake.writes == []
    assert store.get(stored["plan_id"])["status"] == "failed"


def test_transfer_more_than_present_is_refused_at_preview(fake: FakeInvenTree, client: InvenTreeClient) -> None:
    with pytest.raises(WriteValidationError) as info:
        preview("transfer_stock", {"item": 100, "quantity": "11", "destination": 2}, client)
    assert info.value.code == "insufficient_stock"
    assert fake.writes == []


def test_transfer_to_missing_location_refused_at_preview(fake: FakeInvenTree, client: InvenTreeClient) -> None:
    with pytest.raises(WriteValidationError) as info:
        preview("transfer_stock", {"item": 100, "quantity": "1", "destination": 999}, client)
    assert info.value.code == "missing_reference"
    assert fake.writes == []


# ---------------------------------------------------------------- write bodies, through the store


def test_transfer_posts_items_and_destination(
    fake: FakeInvenTree,
    client: InvenTreeClient,
    store: PlanStore,
    timeline: Timeline,
) -> None:
    operations = [op("transfer_stock", {"item": 100, "quantity": "4", "destination": 2}, "t1")]
    stored = approved_in_store(store, client, operations)
    report = run_plan(stored, client, RecordingAudit(timeline), store=store, supported_prefixes=RANGE)
    assert report.complete
    assert fake.writes == [
        ("POST", "/api/stock/transfer/", {"items": [{"pk": 100, "quantity": "4"}], "location": 2}),
    ]


def test_create_part_checks_category_then_posts_given_fields(
    fake: FakeInvenTree,
    client: InvenTreeClient,
    store: PlanStore,
    timeline: Timeline,
) -> None:
    operations = [op("create_part", {"name": "Film cap", "category": 3, "IPN": "C-100"}, "p1")]
    stored = approved_in_store(store, client, operations)
    report = run_plan(stored, client, RecordingAudit(timeline), store=store, supported_prefixes=RANGE)
    assert ("GET", "/api/part/category/3/") in fake.requests
    assert fake.writes == [
        ("POST", "/api/part/", {"name": "Film cap", "category": 3, "IPN": "C-100", "active": True}),
    ]
    assert report.outcomes[0].pk == 500


def test_update_part_patches_only_changed_fields(
    fake: FakeInvenTree,
    client: InvenTreeClient,
    store: PlanStore,
    timeline: Timeline,
) -> None:
    stored = approved_in_store(store, client, [op("update_part", {"pk": 7, "name": "Resistor 4k7"}, "u1")])
    run_plan(stored, client, RecordingAudit(timeline), store=store, supported_prefixes=RANGE)
    assert fake.writes == [("PATCH", "/api/part/7/", {"name": "Resistor 4k7"})]


def test_update_part_can_clear_the_category(
    fake: FakeInvenTree,
    client: InvenTreeClient,
    store: PlanStore,
    timeline: Timeline,
) -> None:
    stored = approved_in_store(store, client, [op("update_part", {"pk": 7, "category": None}, "u2")])
    run_plan(stored, client, RecordingAudit(timeline), store=store, supported_prefixes=RANGE)
    assert fake.writes == [("PATCH", "/api/part/7/", {"category": None})]


def test_create_location_with_parent_checks_parent_first(
    fake: FakeInvenTree,
    client: InvenTreeClient,
    store: PlanStore,
    timeline: Timeline,
) -> None:
    stored = approved_in_store(
        store,
        client,
        [op("create_location", {"name": "Drawer 3", "parent": 1, "structural": False}, "l1")],
    )
    run_plan(stored, client, RecordingAudit(timeline), store=store, supported_prefixes=RANGE)
    assert ("GET", "/api/stock/location/1/") in fake.requests
    assert fake.writes == [("POST", "/api/stock/location/", {"name": "Drawer 3", "parent": 1, "structural": False})]


def test_create_stock_item_sends_quantity_as_a_decimal_string(
    fake: FakeInvenTree,
    client: InvenTreeClient,
    store: PlanStore,
    timeline: Timeline,
) -> None:
    stored = approved_in_store(
        store,
        client,
        [op("create_stock_item", {"part": 7, "quantity": "2.50", "location": 1, "batch": "B-7"}, "s1")],
    )
    run_plan(stored, client, RecordingAudit(timeline), store=store, supported_prefixes=RANGE)
    assert fake.writes == [
        ("POST", "/api/stock/", {"part": 7, "quantity": "2.50", "location": 1, "batch": "B-7"}),
    ]


# ---------------------------------------------------------------- run_plan: refusals with zero requests


def test_unapproved_plan_refused_with_zero_requests(
    fake: FakeInvenTree,
    client: InvenTreeClient,
    store: PlanStore,
    timeline: Timeline,
) -> None:
    operations = [op("create_location", {"name": "A"}, "k1")]
    proposed = store.propose(operations, previews_for(client, operations))
    fake.requests.clear()
    audit = RecordingAudit(timeline)
    with pytest.raises(PlanRefused) as info:
        run_plan(store.get(proposed["plan_id"]), client, audit, store=store, supported_prefixes=RANGE)
    assert info.value.code == "not_approved"
    assert fake.requests == []
    assert audit.events == []
    assert store.get(proposed["plan_id"])["status"] == "proposed"


def test_approved_status_without_an_approval_record_refused(
    fake: FakeInvenTree,
    client: InvenTreeClient,
    store: PlanStore,
) -> None:
    stored = approved_plan(store, [op("create_location", {"name": "A"}, "k1")])
    edit_disk(store, stored["plan_id"], lambda data: data.update(approval=None))
    with pytest.raises(PlanRefused) as info:
        run_plan(stored, client, RecordingAudit([]), store=store, supported_prefixes=RANGE)
    assert info.value.code == "not_approved"
    assert fake.requests == []


def test_approval_signed_for_another_hash_refused(
    fake: FakeInvenTree,
    client: InvenTreeClient,
    store: PlanStore,
) -> None:
    stored = approved_plan(store, [op("create_location", {"name": "A"}, "k1")])
    edit_disk(store, stored["plan_id"], lambda data: data["approval"].update(approved_hash="0" * 64))
    with pytest.raises(PlanRefused) as info:
        run_plan(stored, client, RecordingAudit([]), store=store, supported_prefixes=RANGE)
    assert info.value.code == "hash_mismatch"
    assert fake.requests == []


def test_plan_file_edited_after_approval_refused_with_zero_requests(
    fake: FakeInvenTree,
    client: InvenTreeClient,
    store: PlanStore,
    timeline: Timeline,
) -> None:
    stored = approved_in_store(store, client, [op("create_location", {"name": "A"}, "k1")])

    def tamper(data: dict[str, Any]) -> None:
        data["operations"][0]["params"]["name"] = "B"

    edit_disk(store, stored["plan_id"], tamper)
    fake.requests.clear()
    with pytest.raises(PlanRefused) as info:
        run_plan(stored, client, RecordingAudit(timeline), store=store, supported_prefixes=RANGE)
    assert info.value.code == "plan_tampered"
    assert fake.requests == []
    assert disk_status(store, stored["plan_id"]) == "approved"


def test_plan_rehashed_after_approval_refused_as_hash_mismatch(
    fake: FakeInvenTree,
    client: InvenTreeClient,
    store: PlanStore,
    timeline: Timeline,
) -> None:
    stored = approved_in_store(store, client, [op("create_location", {"name": "A"}, "k1")])

    def tamper_and_rehash(data: dict[str, Any]) -> None:
        data["operations"][0]["params"]["name"] = "B"
        data["plan_hash"] = compute_plan_hash(data["operations"], data["previews"])

    edit_disk(store, stored["plan_id"], tamper_and_rehash)
    fake.requests.clear()
    with pytest.raises(PlanRefused) as info:
        run_plan(stored, client, RecordingAudit(timeline), store=store, supported_prefixes=RANGE)
    assert info.value.code == "hash_mismatch"
    assert fake.requests == []


def test_plan_file_with_a_non_finite_number_is_refused_as_tampered(
    fake: FakeInvenTree,
    client: InvenTreeClient,
    store: PlanStore,
) -> None:
    # A NaN cannot be hashed. json.loads accepts it, so the store's validation refuses the file.
    plan_id = write_raw(
        store,
        [op("create_location", {"name": float("nan")}, "k1")],
        [{"before": {}, "after": {}}],
        plan_hash="0" * 64,
    )
    with pytest.raises(PlanRefused) as info:
        run_plan({"plan_id": plan_id}, client, RecordingAudit([]), store=store, supported_prefixes=RANGE)
    assert info.value.code == "plan_tampered"
    assert fake.requests == []


def test_plan_file_without_previews_is_refused_as_tampered(
    fake: FakeInvenTree,
    client: InvenTreeClient,
    store: PlanStore,
) -> None:
    plan_id = write_raw(store, [op("create_location", {"name": "A"}, "k1")], [{"before": {}, "after": {}}])
    edit_disk(store, plan_id, lambda data: data.pop("previews"))
    with pytest.raises(PlanRefused) as info:
        run_plan({"plan_id": plan_id}, client, RecordingAudit([]), store=store, supported_prefixes=RANGE)
    assert info.value.code == "plan_tampered"
    assert fake.requests == []


def test_executed_plan_cannot_run_twice(
    fake: FakeInvenTree,
    client: InvenTreeClient,
    store: PlanStore,
    timeline: Timeline,
) -> None:
    stored = approved_in_store(store, client, [op("create_location", {"name": "A"}, "k1")])
    run_plan(stored, client, RecordingAudit(timeline), store=store, supported_prefixes=RANGE)
    with pytest.raises(PlanRefused) as info:
        run_plan(store.get(stored["plan_id"]), client, RecordingAudit(timeline), store=store, supported_prefixes=RANGE)
    assert info.value.code == "already_executed"
    assert paths_written(fake) == ["/api/stock/location/"]


def test_failed_plan_is_terminal_and_cannot_be_retried(
    fake: FakeInvenTree,
    client: InvenTreeClient,
    store: PlanStore,
    timeline: Timeline,
) -> None:
    fake.fail_paths["POST /api/stock/transfer/"] = 400
    stored = approved_in_store(
        store,
        client,
        [
            op("create_location", {"name": "Drawer 4"}, "k-loc"),
            op("transfer_stock", {"item": 100, "quantity": "1", "destination": 2}, "k-xfer"),
        ],
    )
    report = run_plan(stored, client, RecordingAudit(timeline), store=store, supported_prefixes=RANGE)
    assert report.complete is False
    assert store.get(stored["plan_id"])["status"] == "failed"

    del fake.fail_paths["POST /api/stock/transfer/"]
    with pytest.raises(PlanRefused) as info:
        run_plan(store.get(stored["plan_id"]), client, RecordingAudit(timeline), store=store, supported_prefixes=RANGE)
    assert info.value.code == "already_executed"
    assert paths_written(fake).count("/api/stock/location/") == 1


def test_plan_with_an_invalid_operation_refused_before_any_write(
    fake: FakeInvenTree,
    client: InvenTreeClient,
    store: PlanStore,
) -> None:
    stored = approved_plan(
        store,
        [
            op("create_location", {"name": "ok"}, "k1"),
            op("transfer_stock", {"item": 100, "quantity": 1}, "k2"),
        ],
    )
    with pytest.raises(PlanRefused) as info:
        run_plan(stored, client, RecordingAudit([]), store=store, supported_prefixes=RANGE)
    assert info.value.code == "invalid_operation"
    assert fake.requests == []


@pytest.mark.parametrize("bad_key", ["", "   ", "k" * 201])
def test_bad_client_key_on_disk_is_refused_before_any_write(
    fake: FakeInvenTree,
    client: InvenTreeClient,
    store: PlanStore,
    bad_key: str,
) -> None:
    plan_id = write_raw(store, [op("create_location", {"name": "A"}, bad_key)], [{"before": {}, "after": {}}])
    with pytest.raises(PlanRefused) as info:
        run_plan({"plan_id": plan_id}, client, RecordingAudit([]), store=store, supported_prefixes=RANGE)
    assert info.value.code == "plan_tampered"
    assert fake.requests == []


def test_plan_over_the_operation_limit_refused(fake: FakeInvenTree, client: InvenTreeClient, store: PlanStore) -> None:
    operations = [op("create_location", {"name": f"L{index}"}, f"k{index}") for index in range(MAX_OPERATIONS + 1)]
    plan_id = write_raw(store, operations, [{"before": {}, "after": {}} for _ in operations])
    with pytest.raises(PlanRefused) as info:
        run_plan({"plan_id": plan_id}, client, RecordingAudit([]), store=store, supported_prefixes=RANGE)
    assert info.value.code == "plan_tampered"
    assert fake.requests == []


# ---------------------------------------------------------------- fix 1: run_plan trusts only the stored plan


def test_run_sends_the_stored_plan_not_an_edited_copy(
    fake: FakeInvenTree,
    client: InvenTreeClient,
    store: PlanStore,
    timeline: Timeline,
) -> None:
    stored = approved_in_store(store, client, [op("create_location", {"name": "A"}, "k1")])
    edited = copy.deepcopy(stored)
    edited["operations"][0]["params"]["name"] = "B"
    edited["previews"][0]["after"]["name"] = "B"
    edited["status"] = "executed"  # a caller cannot mark a plan as already run either
    report = run_plan(edited, client, RecordingAudit(timeline), store=store, supported_prefixes=RANGE)
    assert report.complete
    assert fake.writes == [("POST", "/api/stock/location/", {"name": "A"})]
    assert store.get(stored["plan_id"])["status"] == "executed"


# ---------------------------------------------------------------- fix 2: approved -> running before the first write


def test_plan_is_marked_running_before_the_first_write(
    fake: FakeInvenTree,
    client: InvenTreeClient,
    store: PlanStore,
    timeline: Timeline,
) -> None:
    stored = approved_in_store(store, client, [op("create_location", {"name": "A"}, "k1")])
    probe = StatusProbe(timeline, store, stored["plan_id"])
    report = run_plan(stored, client, probe, store=store, supported_prefixes=RANGE)
    assert report.complete
    assert probe.statuses == ["running"]
    assert paths_written(fake) == ["/api/stock/location/"]


def test_running_plan_is_never_run_again(
    fake: FakeInvenTree,
    client: InvenTreeClient,
    store: PlanStore,
    timeline: Timeline,
) -> None:
    stored = approved_in_store(store, client, [op("create_location", {"name": "A"}, "k1")])
    store.mark_running(stored["plan_id"])
    fake.requests.clear()
    with pytest.raises(PlanRefused) as info:
        run_plan(store.get(stored["plan_id"]), client, RecordingAudit(timeline), store=store, supported_prefixes=RANGE)
    assert info.value.code == "already_running"
    assert fake.requests == []
    assert fake.writes == []


def test_result_that_cannot_be_recorded_leaves_the_plan_running(
    fake: FakeInvenTree,
    client: InvenTreeClient,
    store: PlanStore,
    timeline: Timeline,
) -> None:
    stored = approved_in_store(store, client, [op("create_location", {"name": "A"}, "k1")])
    failing = _ResultNotWritten(store.root_dir)
    with pytest.raises(PlanRefused) as info:
        run_plan(stored, client, RecordingAudit(timeline), store=failing, supported_prefixes=RANGE)
    assert info.value.code == "result_not_recorded"
    assert paths_written(fake) == ["/api/stock/location/"]
    assert store.get(stored["plan_id"])["status"] == "running"


# ---------------------------------------------------------------- fix 3: idempotency against the audit log


def test_key_that_already_succeeded_in_an_earlier_plan_is_skipped(
    fake: FakeInvenTree,
    client: InvenTreeClient,
    store: PlanStore,
    tmp_path: Path,
) -> None:
    audit = AuditLog(tmp_path / "audit.jsonl")
    first = approved_in_store(store, client, [op("create_location", {"name": "A"}, "dup")])
    assert run_plan(first, client, audit, store=store, supported_prefixes=RANGE).complete

    second = approved_in_store(store, client, [op("create_location", {"name": "B"}, "dup")])
    report = run_plan(second, client, audit, store=store, supported_prefixes=RANGE)
    assert [outcome.status for outcome in report.outcomes] == ["skipped"]
    assert report.complete
    assert [body for _method, _path, body in fake.writes] == [{"name": "A"}]
    second_events = [event["event"] for event in audit.read_events() if event["plan_id"] == second["plan_id"]]
    assert second_events == ["op_skipped"]


def test_failed_key_is_not_skipped_by_a_later_plan(
    fake: FakeInvenTree,
    client: InvenTreeClient,
    store: PlanStore,
    tmp_path: Path,
) -> None:
    audit = AuditLog(tmp_path / "audit.jsonl")
    fake.fail_paths["POST /api/stock/location/"] = 500
    first = approved_in_store(store, client, [op("create_location", {"name": "A"}, "k1")])
    assert run_plan(first, client, audit, store=store, supported_prefixes=RANGE).complete is False

    del fake.fail_paths["POST /api/stock/location/"]
    second = approved_in_store(store, client, [op("create_location", {"name": "A"}, "k1")])
    report = run_plan(second, client, audit, store=store, supported_prefixes=RANGE)
    assert [outcome.status for outcome in report.outcomes] == ["ok"]
    assert len(fake.writes) == 2  # the failed attempt and the real one; the key was not skipped


def test_unreadable_audit_log_refuses_before_any_write(
    fake: FakeInvenTree,
    client: InvenTreeClient,
    store: PlanStore,
    timeline: Timeline,
) -> None:
    class _UnreadableAudit(RecordingAudit):
        def succeeded_keys(self) -> set[str]:
            raise ValueError("damaged line in the audit log")

    stored = approved_in_store(store, client, [op("create_location", {"name": "A"}, "k1")])
    with pytest.raises(PlanRefused) as info:
        run_plan(stored, client, _UnreadableAudit(timeline), store=store, supported_prefixes=RANGE)
    assert info.value.code == "audit_unreadable"
    assert fake.writes == []
    assert store.get(stored["plan_id"])["status"] == "approved"


def test_client_key_the_audit_would_redact_is_refused_before_any_write(
    fake: FakeInvenTree,
    client: InvenTreeClient,
    store: PlanStore,
    timeline: Timeline,
) -> None:
    # The audit log would write this key as [redacted], so the idempotency check could never find it.
    stored = approved_plan(store, [op("create_location", {"name": "A"}, "bearer abc123")])
    with pytest.raises(PlanRefused) as info:
        run_plan(stored, client, RecordingAudit(timeline), store=store, supported_prefixes=RANGE)
    assert info.value.code == "invalid_operation"
    assert fake.requests == []
    assert store.get(stored["plan_id"])["status"] == "approved"


def test_run_refuses_an_unresolved_key_without_sending_anything(
    fake: FakeInvenTree,
    client: InvenTreeClient,
    store: PlanStore,
    tmp_path: Path,
) -> None:
    # An earlier run started a write with this key and never recorded how it ended, so the write may already be applied.
    audit = AuditLog(tmp_path / "audit.jsonl")
    stored = approved_in_store(store, client, [op("create_location", {"name": "A"}, "k1")])
    audit.append({"event": OP_STARTED, "plan_id": "f" * 32, "index": 0, "client_key": "k1", "phase": "started"})
    fake.requests.clear()

    report = run_plan(stored, client, audit, store=store, supported_prefixes=RANGE)

    assert [outcome.status for outcome in report.outcomes] == ["failed"]
    assert report.outcomes[0].error_code == "key_unresolved"
    assert report.outcomes[0].note == KEY_UNRESOLVED_NOTE
    assert report.complete is False
    assert fake.writes == []  # no POST or PATCH was sent
    assert all(method == "GET" for method, _path in fake.requests)  # only the version check read InvenTree
    run_events = [event["event"] for event in audit.read_events() if event["plan_id"] == stored["plan_id"]]
    assert run_events == ["op_skipped"]  # a skipped event, not op_failed, so the refusal does not settle the key
    assert "k1" in audit.unresolved_keys()  # the key is still unresolved after the run
    current = store.get(stored["plan_id"])
    assert current["status"] == "failed"
    assert current["results"][0]["error_code"] == "key_unresolved"


def test_second_run_with_a_still_unresolved_key_is_refused_again(
    fake: FakeInvenTree,
    client: InvenTreeClient,
    store: PlanStore,
    tmp_path: Path,
) -> None:
    audit = AuditLog(tmp_path / "audit.jsonl")
    first = approved_in_store(store, client, [op("create_location", {"name": "A"}, "k1")])
    audit.append({"event": OP_STARTED, "plan_id": "f" * 32, "index": 0, "client_key": "k1", "phase": "started"})
    assert run_plan(first, client, audit, store=store, supported_prefixes=RANGE).outcomes[0].error_code == "key_unresolved"
    fake.requests.clear()

    # A second plan that holds the same key was approved before the unresolved write, so propose would refuse
    # the key itself. write_raw puts it on disk the way such an approval would have left it.
    second_id = write_raw(store, [op("create_location", {"name": "B"}, "k1")], [{"before": {}, "after": {}}])
    report = run_plan({"plan_id": second_id}, client, audit, store=store, supported_prefixes=RANGE)

    assert [outcome.error_code for outcome in report.outcomes] == ["key_unresolved"]
    assert report.complete is False
    assert fake.writes == []  # no POST was sent for the second plan either
    assert "k1" in audit.unresolved_keys()
    assert store.get(second_id)["status"] == "failed"


# ---------------------------------------------------------------- fix 4: version gate at run time


def test_run_refuses_a_version_outside_the_supported_range(
    fake: FakeInvenTree,
    client: InvenTreeClient,
    store: PlanStore,
    timeline: Timeline,
) -> None:
    stored = approved_in_store(store, client, [op("create_location", {"name": "A"}, "k1")])
    fake.version = "2.0.0"
    with pytest.raises(PlanRefused) as info:
        run_plan(stored, client, RecordingAudit(timeline), store=store, supported_prefixes=RANGE)
    assert info.value.code == "version_unsupported"
    assert fake.writes == []
    assert store.get(stored["plan_id"])["status"] == "approved"


def test_run_refuses_when_the_version_cannot_be_read(
    fake: FakeInvenTree,
    client: InvenTreeClient,
    store: PlanStore,
    timeline: Timeline,
) -> None:
    stored = approved_in_store(store, client, [op("create_location", {"name": "A"}, "k1")])
    fake.api_status = 503
    with pytest.raises(PlanRefused) as info:
        run_plan(stored, client, RecordingAudit(timeline), store=store, supported_prefixes=RANGE)
    assert info.value.code == "version_unverified"
    assert fake.writes == []
    assert store.get(stored["plan_id"])["status"] == "approved"


def test_run_with_no_supported_range_refuses_every_version(
    fake: FakeInvenTree,
    client: InvenTreeClient,
    store: PlanStore,
    timeline: Timeline,
) -> None:
    stored = approved_in_store(store, client, [op("create_location", {"name": "A"}, "k1")])
    with pytest.raises(PlanRefused) as info:
        run_plan(stored, client, RecordingAudit(timeline), store=store)
    assert info.value.code == "version_unsupported"
    assert fake.writes == []
    assert fake.requests == []  # with no range, not even the version read is sent


def test_propose_gate_with_no_version_range_sends_no_request(fake: FakeInvenTree, client: InvenTreeClient) -> None:
    # propose runs check_write_version before it previews anything. With no range set it refuses before any GET /api/.
    with pytest.raises(PlanRefused) as info:
        check_write_version(client, ())
    assert info.value.code == "version_unsupported"
    assert fake.requests == []


# ---------------------------------------------------------------- fix 5: the stored request is what is sent


def test_stored_request_is_the_request_that_is_sent(
    fake: FakeInvenTree,
    client: InvenTreeClient,
    store: PlanStore,
    timeline: Timeline,
) -> None:
    operations = [op("transfer_stock", {"item": 100, "quantity": "4", "destination": 2}, "t1")]
    stored = approved_in_store(store, client, operations)
    report = run_plan(stored, client, RecordingAudit(timeline), store=store, supported_prefixes=RANGE)
    assert report.complete
    ((method, path, body),) = fake.writes
    stored_preview = stored["previews"][0]
    assert (stored_preview["method"], stored_preview["path"], stored_preview["body"]) == (method, path, body)


def test_run_refuses_a_stored_request_that_differs_from_the_operation(
    fake: FakeInvenTree,
    client: InvenTreeClient,
    store: PlanStore,
    timeline: Timeline,
) -> None:
    operations = [op("create_location", {"name": "A"}, "k1")]
    previews = previews_for(client, operations)
    previews[0]["body"] = {"name": "Other"}  # the stored request does not match the operation
    proposed = store.propose(operations, previews)
    store.approve(proposed["plan_id"], proposed["plan_hash"])
    report = run_plan(
        store.get(proposed["plan_id"]),
        client,
        RecordingAudit(timeline),
        store=store,
        supported_prefixes=RANGE,
    )
    assert report.outcomes[0].status == "failed"
    assert report.outcomes[0].error_code == "request_mismatch"
    assert report.complete is False
    assert fake.writes == []
    assert store.get(proposed["plan_id"])["status"] == "failed"


# ---------------------------------------------------------------- run_plan: order, failure, audit


def test_run_plan_stops_at_the_first_failure_and_reports_the_rest_as_not_run(
    fake: FakeInvenTree,
    client: InvenTreeClient,
    store: PlanStore,
    timeline: Timeline,
) -> None:
    fake.fail_paths["POST /api/stock/transfer/"] = 400
    stored = approved_in_store(
        store,
        client,
        [
            op("create_location", {"name": "Drawer 4"}, "k-loc"),
            op("transfer_stock", {"item": 100, "quantity": "1", "destination": 2}, "k-xfer"),
            op("create_location", {"name": "Drawer 5"}, "k-loc2"),
            op("create_part", {"name": "Never sent"}, "k-part"),
        ],
    )
    timeline.clear()
    audit = RecordingAudit(timeline)
    report = run_plan(stored, client, audit, store=store, supported_prefixes=RANGE)

    assert [outcome.status for outcome in report.outcomes] == ["ok", "failed", "not_run", "not_run"]
    assert report.outcomes[1].error_code == "invalid"
    assert paths_written(fake) == ["/api/stock/location/", "/api/stock/transfer/"]
    assert [(event["phase"], event["index"]) for event in audit.events] == [
        ("started", 0),
        ("finished", 0),
        ("started", 1),
        ("finished", 1),
    ]
    saved = store.get(stored["plan_id"])
    assert saved["status"] == "failed"
    assert [row["status"] for row in saved["results"]] == ["ok", "failed", "not_run", "not_run"]


def test_audit_event_names_follow_the_operation_lifecycle(
    fake: FakeInvenTree,
    client: InvenTreeClient,
    store: PlanStore,
    timeline: Timeline,
) -> None:
    stored = approved_in_store(store, client, [op("create_location", {"name": "A"}, "k1")])
    audit = RecordingAudit(timeline)
    run_plan(stored, client, audit, store=store, supported_prefixes=RANGE)
    assert [event["event"] for event in audit.events] == ["op_started", "op_done"]


def test_audit_event_brackets_each_write(
    fake: FakeInvenTree,
    client: InvenTreeClient,
    store: PlanStore,
    timeline: Timeline,
) -> None:
    stored = approved_in_store(store, client, [op("create_location", {"name": "A"}, "k1")])
    timeline.clear()
    run_plan(stored, client, RecordingAudit(timeline), store=store, supported_prefixes=RANGE)
    assert timeline == [
        ("read", "/api/"),
        ("audit", "started", 0),
        ("write", "POST", "/api/stock/location/"),
        ("audit", "finished", 0),
    ]


def test_audit_failure_before_a_write_stops_without_writing(
    fake: FakeInvenTree,
    client: InvenTreeClient,
    store: PlanStore,
    timeline: Timeline,
) -> None:
    stored = approved_in_store(
        store,
        client,
        [op("create_location", {"name": "A"}, "k1"), op("create_location", {"name": "B"}, "k2")],
    )
    audit = RecordingAudit(timeline, fail_at=frozenset({("started", 0)}))
    report = run_plan(stored, client, audit, store=store, supported_prefixes=RANGE)
    assert [outcome.status for outcome in report.outcomes] == ["failed", "not_run"]
    assert report.outcomes[0].error_code == "audit_failed"
    assert fake.writes == []


def test_audit_failure_after_a_write_stops_the_plan_and_says_so(
    fake: FakeInvenTree,
    client: InvenTreeClient,
    store: PlanStore,
    timeline: Timeline,
) -> None:
    stored = approved_in_store(
        store,
        client,
        [op("create_location", {"name": "A"}, "k1"), op("create_location", {"name": "B"}, "k2")],
    )
    audit = RecordingAudit(timeline, fail_at=frozenset({("finished", 0)}))
    report = run_plan(stored, client, audit, store=store, supported_prefixes=RANGE)
    assert report.outcomes[0].status == "ok"
    assert report.outcomes[0].note == AUDIT_AFTER_NOTE
    assert [outcome.status for outcome in report.outcomes] == ["ok", "not_run"]
    assert report.complete is False
    assert paths_written(fake) == ["/api/stock/location/"]
    # The write was sent and succeeded, but its op_done is missing, so the plan needs the owner, not a retry.
    # (It was "failed" before the fix: a failed plan could be re-proposed and the write repeated.)
    assert store.get(stored["plan_id"])["status"] == "needs_reconcile"


def test_audit_failure_after_the_last_write_needs_reconcile(
    fake: FakeInvenTree,
    client: InvenTreeClient,
    store: PlanStore,
    timeline: Timeline,
) -> None:
    stored = approved_in_store(store, client, [op("create_location", {"name": "A"}, "k1")])
    audit = RecordingAudit(timeline, fail_at=frozenset({("finished", 0)}))
    report = run_plan(stored, client, audit, store=store, supported_prefixes=RANGE)
    assert report.complete is False
    assert store.get(stored["plan_id"])["status"] == "needs_reconcile"


def test_audit_and_report_never_contain_the_token(
    fake: FakeInvenTree,
    client: InvenTreeClient,
    store: PlanStore,
    timeline: Timeline,
) -> None:
    fake.fail_paths["POST /api/stock/location/"] = 500
    stored = approved_in_store(store, client, [op("create_location", {"name": "A"}, "k1")])
    audit = RecordingAudit(timeline)
    report = run_plan(stored, client, audit, store=store, supported_prefixes=RANGE)
    assert TOKEN not in json.dumps(audit.events)
    assert TOKEN not in json.dumps(report.as_dict())
    assert report.outcomes[0].error_code == "server_error"


# ---------------------------------------------------------------- fix A: a lost answer is unknown, not failed


class _AnswerLost(FakeInvenTree):
    """Applies each write the way InvenTree would, then loses the answer before the client can read it.

    "timeout" raises before any answer arrives. "too_deep_answer" returns a 2xx whose body the client
    cannot parse, because it is nested too deeply.
    """

    def __init__(self, timeline: Timeline, mode: str) -> None:
        super().__init__(timeline)
        self._mode = mode

    def __call__(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> tuple[int, bytes]:
        if method == "GET":
            return super().__call__(method, url, headers, body, timeout)
        super().__call__(method, url, headers, body, timeout)  # InvenTree applies the write
        if self._mode == "timeout":
            raise TimeoutError("timed out waiting for the answer")
        return 201, b"[" * 100_000


@pytest.mark.parametrize(("mode", "code"), [("timeout", "unreachable"), ("too_deep_answer", "bad_response")])
def test_lost_answer_is_recorded_as_unknown_and_never_retried(
    store: PlanStore,
    timeline: Timeline,
    tmp_path: Path,
    mode: str,
    code: str,
) -> None:
    lost = _AnswerLost(timeline, mode)
    client = InvenTreeClient(BASE, TOKEN, transport=lost)
    audit = AuditLog(tmp_path / "audit.jsonl")  # a real log, so the event names are the ones the owner reads
    stored = approved_in_store(
        store,
        client,
        [op("create_location", {"name": "Drawer 6"}, "k-lost"), op("create_location", {"name": "Drawer 7"}, "k-next")],
    )

    report = run_plan(stored, client, audit, store=store, supported_prefixes=RANGE)

    assert [outcome.status for outcome in report.outcomes] == ["unknown", "not_run"]
    assert report.outcomes[0].error_code == code
    assert report.complete is False
    assert paths_written(lost) == ["/api/stock/location/"]  # one write was sent; it was not retried, and op 2 never ran
    events = audit.read_events()
    assert [event["event"] for event in events] == ["op_started", "op_unknown"]
    assert events[1]["status"] == "unknown"
    assert store.get(stored["plan_id"])["status"] == "needs_reconcile"

    # Until the owner settles it by hand, the plan is refused, and no request is sent.
    lost.requests.clear()
    with pytest.raises(PlanRefused) as info:
        run_plan(store.get(stored["plan_id"]), client, audit, store=store, supported_prefixes=RANGE)
    assert info.value.code == "needs_reconcile"
    assert lost.requests == []


# ---------------------------------------------------------------- fix C: quantity cap before any wire string


def test_quantity_outside_the_wire_limits_is_refused_before_any_string_is_built(
    fake: FakeInvenTree,
    client: InvenTreeClient,
) -> None:
    # 1E+999999999 has an adjusted exponent of 999999999. Refusing it must not build a string that long,
    # so every refusal is checked by its short message, and none sends a request.
    refused = [
        ("create_stock_item", {"part": 7, "quantity": "1E+999999999"}),
        ("create_stock_item", {"part": 7, "quantity": "-1E+999999999"}),
        ("create_stock_item", {"part": 7, "quantity": "1E-999999999"}),
        ("create_stock_item", {"part": 7, "quantity": "1234567890.123456"}),  # 16 significant digits
        ("create_stock_item", {"part": 7, "quantity": "1E+10"}),  # adjusted exponent 10
        ("transfer_stock", {"item": 100, "quantity": "1E+999999999", "destination": 2}),
        ("adjust_stock_quantity", {"item": 100, "delta": "-1E+999999999"}),
    ]
    for op_name, params in refused:
        with pytest.raises(WriteValidationError) as info:
            preview(op_name, params, client)
        assert info.value.code == "invalid_quantity"
        assert len(info.value.message) < 200
    assert fake.requests == []  # refused while the parameters were checked, before any read

    with pytest.raises(WriteValidationError) as info:
        as_wire(Decimal("1E+999999999"))
    assert info.value.code == "invalid_quantity"

    # Fifteen significant digits are allowed, and the string sent keeps all of them.
    result = preview("create_stock_item", {"part": 7, "quantity": "1234567890.12345"}, client)
    assert result["body"]["quantity"] == "1234567890.12345"

    # A stock quantity that InvenTree reports outside the limits is unreadable, so it refuses the write.
    fake.stock[100]["quantity"] = "1E+999999999"
    with pytest.raises(InvenTreeError) as info:
        preview("adjust_stock_quantity", {"item": 100, "delta": 1}, client)
    assert info.value.code == "bad_response"


# ---------------------------------------------------------------- fix: gateway and malformed 2xx answers to a write


class _AnswerIs(FakeInvenTree):
    """Applies each write the way InvenTree would, then answers with a fixed status and body."""

    def __init__(self, timeline: Timeline, status: int, body: bytes) -> None:
        super().__init__(timeline)
        self._status = status
        self._body = body

    def __call__(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> tuple[int, bytes]:
        if method == "GET":
            return super().__call__(method, url, headers, body, timeout)
        super().__call__(method, url, headers, body, timeout)  # InvenTree applies the write
        return self._status, self._body


def test_gateway_answer_to_a_write_is_unknown_and_other_5xx_stays_failed(
    fake: FakeInvenTree,
    client: InvenTreeClient,
    store: PlanStore,
    timeline: Timeline,
    tmp_path: Path,
) -> None:
    audit = AuditLog(tmp_path / "audit.jsonl")
    for status in (502, 503, 504, 500):
        key = f"gateway-{status}"
        fake.fail_paths["POST /api/stock/location/"] = status
        stored = approved_in_store(store, client, [op("create_location", {"name": f"Gateway {status}"}, key)])
        report = run_plan(stored, client, audit, store=store, supported_prefixes=RANGE)
        expected_status, expected_plan = ("unknown", "needs_reconcile") if status != 500 else ("failed", "failed")
        assert report.outcomes[0].status == expected_status, status
        last = [event for event in audit.read_events() if event["client_key"] == key][-1]
        assert last["event"] == ("op_unknown" if status != 500 else "op_failed"), status
        assert store.get(stored["plan_id"])["status"] == expected_plan, status

    # One request per status was sent and none was retried. A gateway status cannot be read as success.
    assert paths_written(fake) == ["/api/stock/location/"] * 4
    assert fake.writes[0][2] == {"name": "Gateway 502"}


def test_2xx_answer_without_a_usable_primary_key_is_unknown_not_success(
    store: PlanStore,
    timeline: Timeline,
    tmp_path: Path,
) -> None:
    proxy_page = _AnswerIs(timeline, 200, b"<html><body>Sign in to continue</body></html>")
    client = InvenTreeClient(BASE, TOKEN, transport=proxy_page)
    audit = AuditLog(tmp_path / "audit.jsonl")
    stored = approved_in_store(
        store,
        client,
        [op("create_location", {"name": "Drawer 8"}, "k-html"), op("create_location", {"name": "Drawer 9"}, "k-next")],
    )

    report = run_plan(stored, client, audit, store=store, supported_prefixes=RANGE)

    assert [outcome.status for outcome in report.outcomes] == ["unknown", "not_run"]
    assert report.outcomes[0].pk is None
    assert report.complete is False
    assert paths_written(proxy_page) == ["/api/stock/location/"]  # the write was sent once, never retried
    assert [event["event"] for event in audit.read_events()] == ["op_started", "op_unknown"]
    assert store.get(stored["plan_id"])["status"] == "needs_reconcile"


# ---------------------------------------------------------------- fix: an op_done that cannot be recorded after a write


class _FailOnDone(RecordingAudit):
    """Records every event except op_done, which it refuses. Nothing else fails."""

    def append(self, event: dict[str, Any]) -> dict[str, Any]:
        if event["event"] == OP_DONE:
            raise OSError("audit file unavailable")
        return super().append(event)


def test_op_done_that_cannot_be_recorded_after_a_write_moves_the_plan_to_needs_reconcile(
    fake: FakeInvenTree,
    client: InvenTreeClient,
    store: PlanStore,
    timeline: Timeline,
) -> None:
    stored = approved_in_store(
        store,
        client,
        [op("create_location", {"name": "A"}, "k1"), op("create_location", {"name": "B"}, "k2")],
    )

    report = run_plan(stored, client, _FailOnDone(timeline), store=store, supported_prefixes=RANGE)

    assert [outcome.status for outcome in report.outcomes] == ["ok", "not_run"]
    assert report.outcomes[0].note == AUDIT_AFTER_NOTE
    assert paths_written(fake) == ["/api/stock/location/"]  # the run stopped, so the second write was never sent
    current = store.get(stored["plan_id"])
    assert current["status"] == "needs_reconcile"  # neither approved nor failed
    with pytest.raises(PlanRefused) as info:
        run_plan(current, client, _FailOnDone(timeline), store=store, supported_prefixes=RANGE)
    assert info.value.code == "needs_reconcile"
    assert paths_written(fake) == ["/api/stock/location/"]


# ---------------------------------------------------------------- fix: the live state is read again before each write


def test_changed_stock_quantity_between_approval_and_run_sends_no_post(
    fake: FakeInvenTree,
    client: InvenTreeClient,
    store: PlanStore,
    timeline: Timeline,
    tmp_path: Path,
) -> None:
    stored = approved_in_store(store, client, [op("adjust_stock_quantity", {"item": 100, "delta": "-4"}, "adj-1")])
    assert stored["previews"][0]["before"] == {"quantity": "10"}
    fake.stock[100]["quantity"] = "7"  # someone else moved stock after the owner approved "10"
    audit = AuditLog(tmp_path / "audit.jsonl")

    report = run_plan(stored, client, audit, store=store, supported_prefixes=RANGE)

    assert report.outcomes[0].status == "failed"
    assert report.outcomes[0].error_code == "state_changed"
    assert fake.writes == []  # no POST was sent
    assert [event["event"] for event in audit.read_events()] == ["op_started", "op_failed"]
    assert store.get(stored["plan_id"])["status"] == "failed"
