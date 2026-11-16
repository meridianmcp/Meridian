"""Filing a save into the project's Zotero collection: the collection key on each item, the refusal of a bridge that cannot
file into a collection, and the save worker passing the key only when a project has a folder.

Every request goes to a fake bridge that records it. No test reaches 127.0.0.1:23125. Offscreen Qt, no network.
"""

import datetime as dt
import json
import socket
import threading
from pathlib import Path

import pytest

from meridian_desktop import zotero_bridge, zotero_journal, zotero_scope
from meridian_desktop.ui import zotero_panel
from meridian_desktop.zotero_bridge import BRIDGE_OFF, ERROR, SAVED, ReadingItem, SaveResult, save_items
from meridian_desktop.zotero_client import Reply, ZoteroError

TOKEN = "zb-test-token-7f3c"
WRITE_KEY = "zb-write-key-9a1d"
GRANT_ID = "grant-placement-1"
NOW = dt.datetime(2026, 11, 10, 9, 0, tzinfo=dt.timezone.utc)
EXPIRES = NOW + dt.timedelta(hours=1)
PROJECT_COLLECTION = "ABCD2345"
ITEMS = [ReadingItem("First paper", "https://example.org/one"), ReadingItem("Second paper", "https://example.org/two")]


def _json(payload) -> bytes:
    return json.dumps(payload).encode("utf-8")


def _token_file(tmp_path: Path) -> Path:
    path = tmp_path / "zotero_bridge" / "api_token"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(TOKEN + "\n", encoding="utf-8")
    return path


def _clock():
    return lambda: NOW


class PlacementBridge:
    """Answers status with ``version`` (None means no plugin_version field), grants at once, and records each request."""

    def __init__(self, version="0.1.6", status=200):
        self.version = version
        self.status = status
        self.calls: list[tuple[str, str, dict, bytes | None]] = []
        self.written = 0

    def __call__(self, method, path, headers, body, timeout):
        self.calls.append((method, path, dict(headers), body))
        if method == "GET" and path == "/v1/status":
            payload = {} if self.version is None else {"plugin_version": self.version}
            return Reply(self.status, _json(payload))
        if method == "POST" and path == "/v1/grants":
            return Reply(202, _json({"grant_id": GRANT_ID, "status": "granted"}))
        if method == "GET" and path == f"/v1/grants/{GRANT_ID}":
            payload = {"grant_id": GRANT_ID, "status": "granted", "write_key": WRITE_KEY}
            payload["expires_at"] = EXPIRES.isoformat().replace("+00:00", "Z")
            return Reply(200, _json(payload))
        if method == "DELETE" and path == f"/v1/grants/{GRANT_ID}":
            return Reply(200, _json({"grant_id": GRANT_ID, "status": "revoked"}))
        if method == "POST" and path == "/v1/items":
            self.written += 1
            return Reply(201, _json({"key": f"ITEM{self.written:04d}"}))
        raise AssertionError(f"unexpected request {method} {path}")

    def item_bodies(self) -> list[dict]:
        return [json.loads(body) for method, path, _, body in self.calls if method == "POST" and path == "/v1/items"]

    def posts(self) -> list[str]:
        return [path for method, path, _, _ in self.calls if method == "POST"]


def test_a_collection_key_files_each_item_into_that_collection(tmp_path):
    bridge = PlacementBridge()
    result = save_items(
        ITEMS, transport=bridge, token_path=_token_file(tmp_path), now=_clock(), collection_key=PROJECT_COLLECTION
    )
    assert result.state == SAVED
    bodies = bridge.item_bodies()
    assert [body["collection_keys"] for body in bodies] == [[PROJECT_COLLECTION], [PROJECT_COLLECTION]]
    assert [body["fields"]["title"] for body in bodies] == ["First paper", "Second paper"]


def test_without_a_collection_key_the_item_body_has_no_placement(tmp_path):
    bridge = PlacementBridge()
    result = save_items(ITEMS, transport=bridge, token_path=_token_file(tmp_path), now=_clock())
    assert result.state == SAVED
    assert all("collection_keys" not in body for body in bridge.item_bodies())


@pytest.mark.parametrize("version", ["0.1.4", "0.1.5", "0.2.0", "1.0.0"])
def test_a_bridge_at_or_after_0_1_4_files_into_the_collection(tmp_path, version):
    bridge = PlacementBridge(version=version)
    result = save_items(
        ITEMS, transport=bridge, token_path=_token_file(tmp_path), now=_clock(), collection_key=PROJECT_COLLECTION
    )
    assert result.state == SAVED


@pytest.mark.parametrize(
    ("version", "message"),
    [
        ("0.1.3", zotero_bridge.PLACEMENT_OLD_TEXT),
        ("0.0.9", zotero_bridge.PLACEMENT_OLD_TEXT),
        (None, zotero_bridge.PLACEMENT_UNKNOWN_TEXT),
        ("", zotero_bridge.PLACEMENT_UNKNOWN_TEXT),
        ("soon", zotero_bridge.PLACEMENT_UNKNOWN_TEXT),
        (14, zotero_bridge.PLACEMENT_UNKNOWN_TEXT),
    ],
)
def test_a_bridge_that_cannot_file_into_a_collection_is_refused_before_any_grant(tmp_path, version, message):
    bridge = PlacementBridge(version=version)
    result = save_items(
        ITEMS, transport=bridge, token_path=_token_file(tmp_path), now=_clock(), collection_key=PROJECT_COLLECTION
    )
    assert result.state == ERROR
    assert result.message == message
    assert result.saved == 0
    # Only the status read was sent: no grant was asked for and no item was written.
    assert bridge.posts() == []


def test_a_status_reply_that_is_not_200_is_refused_as_unknown(tmp_path):
    bridge = PlacementBridge(status=503)
    result = save_items(
        ITEMS, transport=bridge, token_path=_token_file(tmp_path), now=_clock(), collection_key=PROJECT_COLLECTION
    )
    assert result.state == ERROR
    assert result.message == zotero_bridge.PLACEMENT_UNKNOWN_TEXT
    assert bridge.posts() == []


@pytest.mark.parametrize("key", ["abcd2345", "ABC2345", "ABCD23456", "ABCD-234", " ABCD2345", ""])
def test_a_malformed_collection_key_is_refused_with_no_request(tmp_path, key):
    bridge = PlacementBridge()
    result = save_items(ITEMS, transport=bridge, token_path=_token_file(tmp_path), now=_clock(), collection_key=key)
    assert result.state == ERROR
    assert result.message == zotero_bridge.COLLECTION_KEY_TEXT
    assert bridge.calls == []


class _RecordingSaveBridge:
    """Stands in for the bridge in the panel's save worker and records the keyword arguments it was given."""

    def __init__(self) -> None:
        self.kwargs: dict | None = None

    def save_items(self, items, progress=None, **kwargs):
        self.kwargs = kwargs
        return SaveResult(ERROR, 0, "Nothing was saved.")


def test_the_save_worker_files_into_the_project_folder_only_when_it_has_one(tmp_path):
    from meridian_desktop.ui.zotero_panel import _run_save

    journal = zotero_journal.SaveJournal(tmp_path / "journal.json")
    lines = ["First paper | https://example.org/one"]
    scoped = _RecordingSaveBridge()
    _run_save(journal, scoped, ITEMS[:1], lines, PROJECT_COLLECTION)
    assert scoped.kwargs == {"collection_key": PROJECT_COLLECTION}
    unscoped = _RecordingSaveBridge()
    _run_save(journal, unscoped, ITEMS[:1], lines)
    assert unscoped.kwargs == {}


class StatusFailingBridge(PlacementBridge):
    """The status read fails the way ``failure`` says: a ZoteroError is raised, or the reply is returned as given."""

    def __init__(self, failure) -> None:
        super().__init__()
        self.failure = failure

    def __call__(self, method, path, headers, body, timeout):
        if method == "GET" and path == "/v1/status":
            self.calls.append((method, path, dict(headers), body))
            if isinstance(self.failure, ZoteroError):
                raise self.failure
            return self.failure
        return super().__call__(method, path, headers, body, timeout)


@pytest.mark.parametrize(
    ("failure", "state", "message"),
    [
        (
            ZoteroError("not_running", zotero_bridge.NOT_ANSWERING_TEXT),
            BRIDGE_OFF,
            zotero_bridge.NOT_ANSWERING_TEXT,
        ),
        (
            ZoteroError("unreachable", zotero_bridge.UNREACHABLE_TEXT),
            ERROR,
            zotero_bridge.UNREACHABLE_TEXT,
        ),
        (
            ZoteroError("timeout", zotero_bridge.TIMEOUT_TEXT),
            ERROR,
            zotero_bridge.PLAIN_TIMEOUT_TEXT,
        ),
        (Reply(401, _json({})), ERROR, zotero_bridge.TOKEN_REFUSED_TEXT),
    ],
)
def test_a_failed_status_read_names_the_real_problem_and_sends_no_grant(tmp_path, failure, state, message):
    bridge = StatusFailingBridge(failure)
    result = save_items(
        ITEMS, transport=bridge, token_path=_token_file(tmp_path), now=_clock(), collection_key=PROJECT_COLLECTION
    )
    assert result.state == state
    assert result.message == message
    assert result.saved == 0
    assert bridge.posts() == []


def test_a_status_read_that_never_answers_gives_the_plain_timeout_through_the_real_transport(tmp_path, monkeypatch):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    held: list[socket.socket] = []

    def take_connection_and_never_answer() -> None:
        try:
            conn, _ = listener.accept()
        except OSError:
            return
        held.append(conn)

    thread = threading.Thread(target=take_connection_and_never_answer, daemon=True)
    thread.start()
    monkeypatch.setattr(zotero_bridge, "TIMEOUT", 0.3)
    try:
        transport = zotero_bridge.bridge_transport(f"http://127.0.0.1:{listener.getsockname()[1]}")
        result = save_items(
            ITEMS, transport=transport, token_path=_token_file(tmp_path), now=_clock(), collection_key=PROJECT_COLLECTION
        )
    finally:
        listener.close()
        for conn in held:
            conn.close()
        thread.join(timeout=5)
    assert result.state == ERROR
    assert result.message == zotero_bridge.PLAIN_TIMEOUT_TEXT
    assert result.saved == 0


@pytest.mark.parametrize("version", ["0.1.5-beta", "0.1.5-rc1", "0.2.0-alpha.1"])
def test_a_prerelease_above_0_1_4_files_into_the_collection(tmp_path, version):
    bridge = PlacementBridge(version=version)
    result = save_items(
        ITEMS, transport=bridge, token_path=_token_file(tmp_path), now=_clock(), collection_key=PROJECT_COLLECTION
    )
    assert result.state == SAVED


def test_a_prerelease_of_0_1_4_sorts_before_0_1_4_and_is_refused(tmp_path):
    bridge = PlacementBridge(version="0.1.4-rc1")
    result = save_items(
        ITEMS, transport=bridge, token_path=_token_file(tmp_path), now=_clock(), collection_key=PROJECT_COLLECTION
    )
    assert result.state == ERROR
    assert result.message == zotero_bridge.PLACEMENT_OLD_TEXT
    assert bridge.posts() == []


def _scope(project_id, *, library_type="user", library_id="0", collection_key=PROJECT_COLLECTION):
    return zotero_scope.ProjectScope(
        project_id=project_id,
        library_type=library_type,
        library_id=library_id,
        library_name="My Library" if library_type == zotero_scope.LIBRARY_USER else "Group library",
        collection_key=collection_key,
        collection_name="Notes",
    )


def _read(scopes=(), warning="", unreadable=False, skipped=()):
    return zotero_scope.ScopeRead(
        scopes={scope.project_id: scope for scope in scopes},
        warning=warning,
        unreadable=unreadable,
        skipped=frozenset(skipped),
    )


def test_a_save_for_a_project_with_no_folder_is_not_placed_and_not_refused():
    assert zotero_panel._save_placement(_read(), set(), "") == (None, "")
    assert zotero_panel._save_placement(_read(), set(), "P1") == (None, "")


def test_the_folder_in_the_file_is_the_key_a_save_files_into():
    current = _read([_scope("P1")])
    assert zotero_panel._save_placement(current, {"P1"}, "P1") == (PROJECT_COLLECTION, "")


def test_a_folder_changed_by_another_window_is_the_one_in_the_file():
    # The key differs from the default, so this fails if placement reads any copy other than the file's.
    current = _read([_scope("P1", collection_key="EFGH6789")])
    assert zotero_panel._save_placement(current, {"P1"}, "P1") == ("EFGH6789", "")


def test_a_folder_removed_from_the_file_refuses_the_save():
    assert zotero_panel._save_placement(_read(), {"P1"}, "P1") == (None, zotero_panel.FOLDER_GONE_TEXT)


def test_a_folder_gone_from_the_file_stays_refused_after_another_write_to_the_file():
    # The window's own save for another project rewrites the file, but this window still remembers that P1 was bound.
    after_write = _read([_scope("R")])
    assert zotero_panel._save_placement(after_write, {"P1", "R"}, "P1") == (None, zotero_panel.FOLDER_GONE_TEXT)


def test_a_project_the_window_cleared_saves_without_a_folder():
    # A clear removes the project from the window's memory, so a save for it goes without a folder, as the refusal said.
    assert zotero_panel._save_placement(_read([_scope("R")]), {"R"}, "P1") == (None, "")


def test_an_unreadable_scope_file_refuses_every_save_with_the_scope_file_text():
    unreadable = _read(warning=zotero_scope.UNREADABLE_TEXT, unreadable=True)
    assert zotero_panel._save_placement(unreadable, {"P1"}, "P1") == (
        None,
        zotero_panel.SCOPE_UNREADABLE_SAVE_TEXT,
    )
    assert zotero_panel._save_placement(unreadable, set(), "") == (None, zotero_panel.SCOPE_UNREADABLE_SAVE_TEXT)


def test_an_unreadable_scope_file_refusal_does_not_claim_a_copy_was_kept():
    key, refusal = zotero_panel._save_placement(_read(unreadable=True), set(), "")
    assert key is None
    assert refusal == zotero_panel.SCOPE_UNREADABLE_SAVE_TEXT
    assert "copy" not in refusal


def test_a_project_whose_entry_is_not_valid_in_the_file_refuses_its_saves(tmp_path):
    path = tmp_path / "zotero_project_scopes.json"
    entry = {"library_type": "user", "library_id": "0", "library_name": "My Library", "collection_name": "Notes"}
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "scopes": {
                    "P1": {**entry, "collection_key": "abcd2345"},
                    "P2": {**entry, "collection_key": PROJECT_COLLECTION},
                },
            }
        ),
        encoding="utf-8",
    )
    current = zotero_scope.read_scopes(path)
    assert current.skipped == frozenset({"P1"})
    assert set(current.scopes) == {"P2"}
    assert zotero_panel._save_placement(current, set(), "P1") == (None, zotero_panel.SCOPE_SKIPPED_SAVE_TEXT)
    assert zotero_panel._save_placement(current, set(), "P2") == (PROJECT_COLLECTION, "")


def test_a_project_whose_entry_was_not_valid_at_launch_still_refuses_at_save():
    current = _read(skipped=["P1"])
    assert zotero_panel._save_placement(current, {"P1"}, "P1") == (None, zotero_panel.SCOPE_SKIPPED_SAVE_TEXT)
    assert zotero_panel._save_placement(current, set(), "P1") == (None, zotero_panel.SCOPE_SKIPPED_SAVE_TEXT)


def test_a_write_keeps_an_invalid_entry_so_a_fresh_launch_still_refuses_its_saves(tmp_path):
    path = tmp_path / "zotero_project_scopes.json"
    entry = {"library_type": "user", "library_id": "0", "library_name": "My Library", "collection_name": "Notes"}
    path.write_text(json.dumps({"version": 1, "scopes": {"P1": {**entry, "collection_key": "abcd2345"}}}), encoding="utf-8")
    written = zotero_scope.save_scope(_scope("P3"), path)
    assert written.skipped == frozenset({"P1"})
    # A fresh read, with no memory of this window, still finds the invalid entry and refuses P1's saves.
    fresh = zotero_scope.read_scopes(path)
    assert fresh.skipped == frozenset({"P1"})
    assert set(fresh.scopes) == {"P3"}
    assert zotero_panel._save_placement(fresh, set(), "P1") == (None, zotero_panel.SCOPE_SKIPPED_SAVE_TEXT)


def test_a_group_folder_refuses_rather_than_filing_into_my_library():
    current = _read([_scope("P1", library_type=zotero_scope.LIBRARY_GROUP, library_id="123456")])
    assert zotero_panel._save_placement(current, {"P1"}, "P1") == (None, zotero_panel.GROUP_FOLDER_TEXT)
