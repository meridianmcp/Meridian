"""Tests for meridian/zotero_bridge_client.py.

Every test injects a fake transport (the write API) and a fake reader (Zotero's local API), so nothing here opens a
socket, reads a real Zotero library, or raises a real grant prompt. The fake bridge follows the grant states described
in extensions/zotero-bridge/README.md. It is not Zotero, and it proves nothing about the live bridge.
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from meridian import zotero_bridge_client as zbc
from meridian.zotero_bridge_client import (
    ALREADY_ATTACHED,
    ALREADY_PRESENT,
    BatchRolledBackError,
    GrantError,
    ZoteroBridgeClient,
    ZoteroBridgeError,
)

START = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)
GRANT_ID = "0f" * 16  # 32 hex characters, the shape the bridge issues


def _err(code: str, message: str = "refused") -> dict[str, Any]:
    return {"error": {"code": code, "message": message}}


class FakeClock:
    def __init__(self) -> None:
        self.current = START

    def now(self) -> datetime:
        return self.current

    def advance(self, **kwargs: float) -> None:
        self.current += timedelta(**kwargs)


class FakeBridge:
    """Answers the grant routes and the write routes, and records every request it receives.

    ``grant_states`` are the answers to successive GET /v1/grants/{id} polls; the last one repeats. ``forced`` holds
    (method, path-prefix, reply) entries that answer the next matching authorised write, once each.
    """

    def __init__(self, grant_states: tuple[str, ...] = ("granted",)) -> None:
        self.calls: list[dict[str, Any]] = []
        self.grant_states = list(grant_states)
        self.expires_at = START + timedelta(hours=1)
        self.valid_keys: set[str] = set()
        self.issued = 0
        self.post_grant_reply: tuple[int, Any] | None = None
        self.poll_reply: tuple[int, Any] | None = None
        self.forced: list[tuple[str, str, tuple[int, Any]]] = []
        self._states: list[str] = []

    def __call__(self, method: str, path: str, headers: dict[str, str], body: Any) -> tuple[int, Any]:
        self.calls.append({"method": method, "path": path, "headers": dict(headers), "body": body})
        if method == "POST" and path == "/v1/grants":
            if self.post_grant_reply is not None:
                return self.post_grant_reply
            self._states = list(self.grant_states)
            return 202, {"grant_id": GRANT_ID, "status": "pending"}
        if path.startswith("/v1/grants/"):
            return self._grant_route(method)
        if headers.get("Meridian-Write-Grant") not in self.valid_keys:
            return 403, _err("grant_required", "Writes need a grant from the desktop.")
        for index, (forced_method, prefix, reply) in enumerate(self.forced):
            if forced_method == method and path.startswith(prefix):
                del self.forced[index]
                return reply
        if method == "POST" and path == "/v1/batch":
            return 200, {"results": [{"key": "BATCH001"} for _ in body["operations"]]}
        return self._default_write(method, path)

    def _grant_route(self, method: str) -> tuple[int, Any]:
        if method == "DELETE":
            return 200, {"grant_id": GRANT_ID, "status": "revoked"}
        if self.poll_reply is not None:
            return self.poll_reply
        state = self._states.pop(0) if len(self._states) > 1 else self._states[0]
        if state != "granted":
            return 200, {"grant_id": GRANT_ID, "status": state}
        self.issued += 1
        key = f"{self.issued:064x}"
        self.valid_keys.add(key)
        expires = self.expires_at.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        return 200, {"grant_id": GRANT_ID, "status": "granted", "expires_at": expires, "write_key": key}

    @staticmethod
    def _default_write(method: str, path: str) -> tuple[int, Any]:
        if method == "POST" and path == "/v1/collections":
            return 201, {"key": "COLL0001", "version": 0}
        if method == "POST" and path == "/v1/items":
            return 201, {"key": "WEB00001", "version": 0}
        if method == "POST" and path.endswith("/attachments"):
            return 201, {"key": "ATT00001", "version": 0}
        if method == "POST" and path.endswith("/notes"):
            return 201, {"key": "NOTE0001", "version": 0}
        return 200, {"key": path.rsplit("/", 1)[-1], "version": 0}


class FakeReader:
    """Serves children from a dict keyed by parent item key, honouring the start and limit query parameters."""

    def __init__(self, children: dict[str, list[dict[str, Any]]] | None = None, status: int = 200) -> None:
        self.children = dict(children or {})
        self.status = status
        self.paths: list[str] = []

    def __call__(self, path: str) -> tuple[int, Any]:
        self.paths.append(path)
        if self.status != 200:
            return self.status, {"error": {"code": "local_api_disabled", "message": "Local API is not enabled"}}
        base, _, query = path.partition("?")
        params = dict(part.split("=", 1) for part in query.split("&") if "=" in part)
        parent = base.split("/")[-2]
        rows = self.children.get(parent, [])
        start = int(params.get("start", 0))
        limit = int(params.get("limit", 25))
        return 200, rows[start : start + limit]


def pdf_child(key: str = "PDF00001") -> dict[str, Any]:
    return {
        "key": key,
        "version": 1,
        "data": {"key": key, "itemType": "attachment", "contentType": "application/pdf", "filename": "paper.pdf"},
    }


def note_child(key: str, note: str) -> dict[str, Any]:
    return {"key": key, "version": 1, "data": {"key": key, "itemType": "note", "note": note}}


def write_pdf(path: Any, size: int = 2048) -> Any:
    """A file that starts with %PDF- and is ``size`` bytes long. The tail is zero-filled, so large sizes stay cheap."""
    with open(path, "wb") as handle:
        handle.write(b"%PDF-1.7\n")
        handle.truncate(size)
    return path


def make_client(
    bridge: FakeBridge, reader: FakeReader | None = None, clock: FakeClock | None = None, **kwargs: Any
) -> tuple[ZoteroBridgeClient, list[float]]:
    clock = clock or FakeClock()
    sleeps: list[float] = []
    client = ZoteroBridgeClient(
        transport=bridge,
        reader=reader or FakeReader(),
        now=clock.now,
        sleep=sleeps.append,
        **kwargs,
    )
    return client, sleeps


def grant_requests(bridge: FakeBridge) -> list[dict[str, Any]]:
    return [c for c in bridge.calls if c["method"] == "POST" and c["path"] == "/v1/grants"]


def writes_of(bridge: FakeBridge) -> list[dict[str, Any]]:
    """Every call that is not a grant route, so grant requests and polls are left out."""
    return [c for c in bridge.calls if c["path"] != "/v1/grants" and not c["path"].startswith("/v1/grants/")]


# ---------------------------------------------------------------------------------------------
# Grant lifecycle
# ---------------------------------------------------------------------------------------------


def test_three_writes_share_one_grant_request():
    bridge = FakeBridge()
    client, _ = make_client(bridge)

    client.create_collection("Papers")
    client.create_webpage("A page", "https://example.org/a")
    client.move_item("ITEM0001", add_collections=["COLL0001"])

    assert len(grant_requests(bridge)) == 1
    writes = writes_of(bridge)
    assert len(writes) == 3
    assert {c["headers"]["Meridian-Write-Grant"] for c in writes} == {f"{1:064x}"}


def test_grant_required_403_triggers_one_re_request_with_constant_app_name():
    bridge = FakeBridge()
    client, _ = make_client(bridge)
    client.create_collection("First")

    bridge.valid_keys.clear()  # the bridge forgets the key, as it does after a restart
    assert client.create_collection("Second") == "COLL0001"

    posts = grant_requests(bridge)
    assert len(posts) == 2
    assert [post["body"] for post in posts] == [{"app_name": "Meridian"}, {"app_name": "Meridian"}]
    assert len(writes_of(bridge)) == 3  # first write, the refused attempt, and the retry
    assert writes_of(bridge)[-1]["headers"]["Meridian-Write-Grant"] == f"{2:064x}"


def test_second_grant_required_stops_without_another_request():
    bridge = FakeBridge()
    bridge.forced = [
        ("POST", "/v1/collections", (403, _err("grant_required"))),
        ("POST", "/v1/collections", (403, _err("grant_required"))),
    ]
    client, _ = make_client(bridge)

    with pytest.raises(GrantError) as info:
        client.create_collection("Stuck")

    assert info.value.code == "grant_required"
    assert len(grant_requests(bridge)) == 2
    assert len(writes_of(bridge)) == 2


@pytest.mark.parametrize(
    ("outcome", "code"),
    [("denied", "grant_denied"), ("cooldown", "grant_cooldown"), ("poll_gone", "not_found")],
)
def test_denied_or_refused_grant_is_reported_and_nothing_is_written(outcome: str, code: str):
    bridge = FakeBridge()
    if outcome == "denied":
        bridge.grant_states = ["pending", "denied"]
    elif outcome == "cooldown":
        bridge.post_grant_reply = (429, _err("grant_cooldown", "That request was refused. Wait a few minutes."))
    else:
        bridge.poll_reply = (404, _err("not_found", "No grant has that id."))
    client, _ = make_client(bridge)

    with pytest.raises(GrantError) as info:
        client.create_collection("Nope")

    assert info.value.code == code
    assert len(grant_requests(bridge)) == 1
    assert writes_of(bridge) == []


def test_expired_grant_is_replaced_from_expires_at_without_a_403():
    bridge = FakeBridge()
    clock = FakeClock()
    client, _ = make_client(bridge, clock=clock)
    client.create_collection("Before expiry")

    clock.advance(hours=2)  # past the expires_at the bridge reported one hour out
    client.create_collection("After expiry")

    assert len(grant_requests(bridge)) == 2
    assert len(writes_of(bridge)) == 2  # no refused write, so the 403 path was not needed


def test_unanswered_grant_is_ended_and_reported():
    bridge = FakeBridge(grant_states=("pending",))
    client, sleeps = make_client(bridge, grant_polls=3)

    with pytest.raises(GrantError) as info:
        client.create_collection("Slow")

    assert info.value.code == "grant_timeout"
    polls = [c for c in bridge.calls if c["method"] == "GET"]
    assert len(polls) == 3
    assert bridge.calls[-1]["method"] == "DELETE"
    assert writes_of(bridge) == []
    assert sleeps == [1.0, 1.0]  # one wait between each pair of checks, none after the last


# ---------------------------------------------------------------------------------------------
# Collections and item moves
# ---------------------------------------------------------------------------------------------


def test_create_subcollection_sends_parent_key():
    bridge = FakeBridge()
    client, _ = make_client(bridge)

    assert client.create_collection("Child", parent_key="PARENT01") == "COLL0001"

    call = writes_of(bridge)[0]
    assert call["method"] == "POST"
    assert call["path"] == "/v1/collections"
    assert call["body"] == {"name": "Child", "parent_key": "PARENT01"}


def test_move_collection_to_top_sends_null_and_move_under_sends_the_key():
    bridge = FakeBridge()
    client, _ = make_client(bridge)

    client.move_collection("CHILD001", None)
    client.move_collection("CHILD001", "PARENT01")

    to_top, under = writes_of(bridge)
    assert to_top["method"] == "PATCH"
    assert to_top["path"] == "/v1/collections/CHILD001"
    assert to_top["body"] == {"parent_key": None}
    assert under["body"] == {"parent_key": "PARENT01"}


def test_move_item_copies_removes_and_moves_collections():
    bridge = FakeBridge()
    client, _ = make_client(bridge)

    client.move_item("ITEM0001", add_collections=["COLLB001"])  # copy
    client.move_item("ITEM0001", remove_collections=["COLLA001"])  # remove
    client.move_item("ITEM0001", add_collections=["COLLB001"], remove_collections=["COLLA001"])  # move

    copy, remove, both = writes_of(bridge)
    assert copy["path"] == "/v1/items/ITEM0001"
    assert copy["body"] == {"add_collections": ["COLLB001"]}
    assert remove["body"] == {"remove_collections": ["COLLA001"]}
    assert both["body"] == {"add_collections": ["COLLB001"], "remove_collections": ["COLLA001"]}
    with pytest.raises(ZoteroBridgeError):
        client.move_item("ITEM0001")  # nothing to change is refused before any request


# ---------------------------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------------------------


def test_attach_pdf_already_attached_returns_without_any_write(tmp_path):
    pdf = write_pdf(tmp_path / "paper.pdf")
    reader = FakeReader(children={"ITEM0001": [pdf_child()]})
    bridge = FakeBridge()
    client, _ = make_client(bridge, reader)

    assert client.attach_pdf("ITEM0001", str(pdf)) == ALREADY_ATTACHED
    assert bridge.calls == []  # no grant request and no write
    assert reader.paths[0].startswith("/api/users/0/items/ITEM0001/children")


def test_attach_pdf_under_the_limit_imports_base64_data(tmp_path):
    pdf = write_pdf(tmp_path / "paper.pdf", size=4096)
    bridge = FakeBridge()
    client, _ = make_client(bridge)

    assert client.attach_pdf("ITEM0001", str(pdf), title="Paper") == "ATT00001"

    call = writes_of(bridge)[0]
    body = call["body"]
    assert call["path"] == "/v1/items/ITEM0001/attachments"
    assert body["mode"] == "imported_file"
    assert body["filename"] == "paper.pdf"
    assert body["title"] == "Paper"
    assert "path" not in body
    assert base64.b64decode(body["data_base64"]) == pdf.read_bytes()


def test_attach_pdf_above_the_limit_links_by_absolute_path(tmp_path):
    big = write_pdf(tmp_path / "big.pdf", size=zbc.IMPORT_LIMIT_BYTES + 1)
    bridge = FakeBridge()
    client, _ = make_client(bridge)

    client.attach_pdf("ITEM0001", str(big))

    body = writes_of(bridge)[0]["body"]
    assert body["mode"] == "linked_file"
    assert body["path"] == str(big)
    assert "data_base64" not in body


def test_bridge_linking_refusal_is_raised_not_swallowed(tmp_path):
    big = write_pdf(tmp_path / "big.pdf", size=zbc.IMPORT_LIMIT_BYTES + 1)
    bridge = FakeBridge()
    bridge.forced = [
        ("POST", "/v1/items/ITEM0001/attachments", (422, _err("linking_refused", "Outside the allowed folders."))),
    ]
    client, _ = make_client(bridge)

    with pytest.raises(ZoteroBridgeError) as info:
        client.attach_pdf("ITEM0001", str(big))

    assert info.value.code == "linking_refused"
    assert info.value.status == 422


def test_non_pdf_file_is_refused_before_any_write(tmp_path):
    text = tmp_path / "notes.pdf"
    text.write_bytes(b"Hello, this is not a PDF")
    bridge = FakeBridge()
    client, _ = make_client(bridge)

    with pytest.raises(ZoteroBridgeError) as info:
        client.attach_pdf("ITEM0001", str(text))

    assert info.value.code == "not_pdf"
    assert bridge.calls == []


def test_reader_failure_stops_the_attach_before_any_write(tmp_path):
    pdf = write_pdf(tmp_path / "paper.pdf")
    bridge = FakeBridge()
    client, _ = make_client(bridge, FakeReader(status=403))

    with pytest.raises(ZoteroBridgeError) as info:
        client.attach_pdf("ITEM0001", str(pdf))

    assert info.value.code == "reader_failed"
    assert bridge.calls == []


# ---------------------------------------------------------------------------------------------
# Webpages and child notes
# ---------------------------------------------------------------------------------------------


def test_create_webpage_sends_url_and_collections():
    bridge = FakeBridge()
    client, _ = make_client(bridge)

    key = client.create_webpage("A page", "https://example.org/paper", parent_collection_keys=["COLL0001"])

    assert key == "WEB00001"
    call = writes_of(bridge)[0]
    assert call["path"] == "/v1/items"
    assert call["body"] == {
        "item_type": "webpage",
        "fields": {"title": "A page", "url": "https://example.org/paper"},
        "collection_keys": ["COLL0001"],
    }
    with pytest.raises(ZoteroBridgeError):
        client.create_webpage("Not web", "javascript:alert(1)")


def test_child_note_is_skipped_when_the_marker_is_present_and_written_otherwise():
    marker = "meridian-digest-0001"
    already = FakeReader(
        children={"ITEM0001": [note_child("NOTE0009", f'<div data-meridian-dedup="{marker}">old</div>')]}
    )
    bridge = FakeBridge()
    client, _ = make_client(bridge, already)

    assert client.add_child_note("ITEM0001", "<p>Summary</p>", marker) == ALREADY_PRESENT
    assert bridge.calls == []

    unrelated = FakeReader(children={"ITEM0001": [note_child("NOTE0009", "<p>unrelated</p>")]})
    bridge2 = FakeBridge()
    client2, _ = make_client(bridge2, unrelated)

    assert client2.add_child_note("ITEM0001", "<p>Summary</p>", marker) == "NOTE0001"
    call = writes_of(bridge2)[0]
    assert call["path"] == "/v1/items/ITEM0001/notes"
    assert marker in call["body"]["html"]
    assert "<p>Summary</p>" in call["body"]["html"]


# ---------------------------------------------------------------------------------------------
# Batches
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/v1/items/PARENT01/attachments", "/v1/tags/rename", "/v1/import/bibtex"])
def test_batch_refuses_attachments_tag_rename_and_bibtex_before_sending(path: str):
    bridge = FakeBridge()
    client, _ = make_client(bridge)
    operations = [
        {"method": "POST", "path": "/v1/collections", "body": {"name": "fine"}},
        {"method": "POST", "path": path, "body": {}},
    ]

    with pytest.raises(ZoteroBridgeError) as info:
        client.batch(operations)

    assert info.value.code == "not_batchable"
    assert bridge.calls == []


def test_batch_rollback_reports_failed_index_and_is_sent_once():
    bridge = FakeBridge()
    rollback = {
        "error": {
            "code": "batch_rolled_back",
            "message": "Operation 1 failed, so nothing in the batch was written.",
            "details": {"failed_index": 1, "error": {"code": "not_found", "message": "No such item."}},
        }
    }
    bridge.forced = [("POST", "/v1/batch", (422, rollback))]
    client, _ = make_client(bridge)
    operations = [
        {"method": "POST", "path": "/v1/collections", "body": {"name": "A"}},
        {"method": "PATCH", "path": "/v1/items/MISSING1", "body": {"fields": {"title": "x"}}},
    ]

    with pytest.raises(BatchRolledBackError) as info:
        client.batch(operations)

    assert info.value.failed_index == 1
    assert info.value.code == "batch_rolled_back"
    assert info.value.inner_code == "not_found"
    assert [c["path"] for c in writes_of(bridge)] == ["/v1/batch"]


def test_batch_size_limit_and_success_results():
    bridge = FakeBridge()
    client, _ = make_client(bridge)
    one = {"method": "POST", "path": "/v1/collections", "body": {"name": "A"}}

    with pytest.raises(ZoteroBridgeError):
        client.batch([one] * (zbc.BATCH_LIMIT + 1))
    assert bridge.calls == []

    assert len(client.batch([one] * zbc.BATCH_LIMIT)) == zbc.BATCH_LIMIT


# ---------------------------------------------------------------------------------------------
# Secret hygiene
# ---------------------------------------------------------------------------------------------


def test_errors_never_carry_the_grant_key_or_a_token():
    bridge = FakeBridge()
    client, _ = make_client(bridge)
    client.create_collection("Warm up")
    issued = next(iter(bridge.valid_keys))
    token = "sk_meridian_" + "A" * 40
    echo = {"error": {"code": "invalid_fields", "message": f"echoed {issued} and {token}"}}
    bridge.forced = [("POST", "/v1/collections", (422, echo))]

    with pytest.raises(ZoteroBridgeError) as info:
        client.create_collection("Again")

    assert issued not in str(info.value)
    assert token not in str(info.value)

    def leaky_transport(method: str, path: str, headers: dict[str, str], body: Any) -> tuple[int, Any]:
        raise RuntimeError(f"Authorization: Bearer {token}")

    leaky = ZoteroBridgeClient(transport=leaky_transport, reader=FakeReader(), sleep=lambda _s: None)
    with pytest.raises(ZoteroBridgeError) as leak_info:
        leaky.create_collection("X")

    assert token not in str(leak_info.value)
    assert leak_info.value.__context__ is None
    assert leak_info.value.__cause__ is None
