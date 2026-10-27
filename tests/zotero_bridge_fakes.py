"""In-memory stand-ins for one Zotero library and its local bridge, shared by the Zotero bridge test modules.

FakeLibrary holds the collections, items and children. LibraryReader serves them the way Zotero's local API does for
the client's read paths, and LibraryBridge applies writes to them behind a grant. Nothing here opens a socket, reads a
token or raises a grant prompt. It follows Zotero's JSON shape only as far as the client reads it. It is not Zotero, and
it proves nothing about the live bridge.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from meridian.zotero_bridge_client import ZoteroBridgeClient

START = datetime(2026, 10, 27, 12, 0, 0, tzinfo=timezone.utc)
GRANT_ID = "0f" * 16
URL = "https://example.org/pricing"


class FakeLibrary:
    """One library: collections by key, items by key with the collection keys they sit in, and children by parent key.

    ``created`` records the kind of every row a write creates, so a test can count what a rerun added.
    """

    def __init__(self) -> None:
        self.collections: dict[str, dict[str, Any]] = {}
        self.items: dict[str, dict[str, Any]] = {}
        self.created: list[str] = []
        self._serial = 0

    def new_key(self) -> str:
        self._serial += 1
        return f"NEW{self._serial:05d}"

    def seed_collection(self, name: str, parent_key: str | None = None) -> str:
        key = self.new_key()
        self.collections[key] = {"name": name, "parent_key": parent_key}
        return key

    def seed_item(self, item_type: str, url: str, collection_keys: list[str]) -> str:
        key = self.new_key()
        self.items[key] = {"itemType": item_type, "url": url, "collections": list(collection_keys), "children": []}
        return key

    def count(self, kind: str) -> int:
        return self.created.count(kind)

    def collection_rows(self) -> list[dict[str, Any]]:
        return [
            {
                "key": key,
                "version": 1,
                "data": {"key": key, "name": c["name"], "parentCollection": c["parent_key"] or False},
            }
            for key, c in self.collections.items()
        ]

    def collection_item_rows(self, collection_key: str) -> list[dict[str, Any]]:
        return [
            {
                "key": key,
                "version": 1,
                "data": {"key": key, "itemType": item["itemType"], "url": item["url"], "title": "A title"},
            }
            for key, item in self.items.items()
            if collection_key in item["collections"]
        ]

    def children_rows(self, parent_key: str) -> list[dict[str, Any]]:
        item = self.items.get(parent_key)
        return list(item["children"]) if item else []


class LibraryReader:
    """Serves the library the way Zotero's local API does for the client's paths. ``fail_status`` fails every read."""

    def __init__(self, library: FakeLibrary, fail_status: int | None = None) -> None:
        self.library = library
        self.fail_status = fail_status
        self.paths: list[str] = []

    def __call__(self, path: str) -> tuple[int, Any]:
        self.paths.append(path)
        if self.fail_status is not None:
            return self.fail_status, {"error": {"code": "local_api_disabled", "message": "The local API is off."}}
        base, _, query = path.partition("?")
        params = dict(part.split("=", 1) for part in query.split("&") if "=" in part)
        start, limit = int(params.get("start", 0)), int(params.get("limit", 25))
        parts = base.split("/")
        if base == "/api/users/0/collections":
            rows = self.library.collection_rows()
        elif parts[-1] == "items" and parts[-3] == "collections":
            rows = self.library.collection_item_rows(parts[-2])
        elif parts[-1] == "children":
            rows = self.library.children_rows(parts[-2])
        else:
            return 404, {"error": {"code": "not_found", "message": "No such route."}}
        return 200, rows[start : start + limit]


class LibraryBridge:
    """The write API. It answers the grant routes, refuses writes without the live key, and applies writes to the library."""

    def __init__(self, library: FakeLibrary) -> None:
        self.library = library
        self.calls: list[tuple[str, str, Any]] = []
        self.live_key: str | None = None
        self._issued = 0

    def __call__(self, method: str, path: str, headers: dict[str, str], body: Any) -> tuple[int, Any]:
        self.calls.append((method, path, body))
        if method == "POST" and path == "/v1/grants":
            return 202, {"grant_id": GRANT_ID, "status": "pending"}
        if path == f"/v1/grants/{GRANT_ID}":
            if method == "DELETE":
                return 200, {"grant_id": GRANT_ID, "status": "revoked"}
            self._issued += 1
            self.live_key = f"{self._issued:064x}"
            expires = (START + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
            return 200, {"grant_id": GRANT_ID, "status": "granted", "write_key": self.live_key, "expires_at": expires}
        if self.live_key is None or headers.get("Meridian-Write-Grant") != self.live_key:
            return 403, {"error": {"code": "grant_required", "message": "Writes need a grant from the desktop."}}
        return self._apply(method, path, body)

    def _apply(self, method: str, path: str, body: Any) -> tuple[int, Any]:
        library = self.library
        if method == "POST" and path == "/v1/collections":
            key = library.new_key()
            library.collections[key] = {"name": body["name"], "parent_key": body.get("parent_key")}
            library.created.append("collection")
            return 201, {"key": key, "version": 0}
        if method == "POST" and path == "/v1/items":
            key = library.new_key()
            library.items[key] = {
                "itemType": body["item_type"],
                "url": body["fields"].get("url"),
                "collections": list(body.get("collection_keys", [])),
                "children": [],
            }
            library.created.append("item")
            return 201, {"key": key, "version": 0}
        if method == "POST" and path.endswith("/attachments"):
            fields = {"contentType": "application/pdf", "filename": body.get("filename", "linked.pdf")}
            return self._add_child(path.split("/")[3], "attachment", fields, "attachment")
        if method == "POST" and path.endswith("/notes"):
            return self._add_child(path.split("/")[3], "note", {"note": body["html"]}, "note")
        return 200, {}

    def _add_child(self, parent_key: str, item_type: str, fields: dict[str, Any], kind: str) -> tuple[int, Any]:
        key = self.library.new_key()
        child = {"key": key, "version": 0, "data": {"key": key, "itemType": item_type, **fields}}
        self.library.items[parent_key]["children"].append(child)
        self.library.created.append(kind)
        return 201, {"key": key, "version": 0}

    def write_calls(self) -> list[tuple[str, str, Any]]:
        return [c for c in self.calls if c[1] != "/v1/grants" and not c[1].startswith("/v1/grants/")]


def make(library: FakeLibrary, reader: LibraryReader | Any = None) -> tuple[ZoteroBridgeClient, LibraryBridge, Any]:
    bridge = LibraryBridge(library)
    reader = reader if reader is not None else LibraryReader(library)
    client = ZoteroBridgeClient(
        transport=bridge, reader=reader, now=lambda: START, sleep=lambda seconds: None
    )
    return client, bridge, reader
