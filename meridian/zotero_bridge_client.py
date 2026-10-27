"""Meridian-side client for the Zotero Bridge local write API (extensions/zotero-bridge).

The bridge listens on 127.0.0.1:23125. Each request needs a bearer token, and each write also needs a grant key that a
person approves at the Zotero desktop. This client builds the write requests, holds one grant key, and runs the
duplicate checks, which read Zotero's own local API on 127.0.0.1:23119. It opens no socket, and it reads no file or
environment variable on its own. The caller injects two callables:

- ``transport(method, path, headers, body) -> (status, json_body)`` sends one request to the write API. The transport
  adds the Host and Authorization headers, so the bearer token never reaches this module. The client sends only the
  ``Meridian-Write-Grant`` and ``Content-Type`` headers.
- ``reader(path) -> (status, json_body)`` sends one GET to Zotero's local API. ``path`` includes the query string, for
  example ``/api/users/0/items/<key>/children?format=json&limit=100&start=0``.

A client built with fakes cannot reach the network. That is the convention in meridian/research/providers/runpod.py.

Grants. The client asks for a grant under the app name "Meridian" when it holds no live key. It asks again only after a
write comes back 403 grant_required. It polls the grant a bounded number of times. A denied, expired or revoked grant,
a refused request, or a grant that is never answered raises GrantError. Nothing is retried in a loop.

Errors. Every failure raises ZoteroBridgeError or a subclass, carrying the bridge's error code. Messages are scrubbed of
the grant key and of token-shaped strings. A transport or reader exception is replaced by a fixed message, so its text,
which may hold the token, is never kept, and the exception chain is cleared.

Duplicates. ``attach_pdf`` returns "already_attached", and ``add_child_note`` returns "already_present", without
writing, when the item already has a PDF or a child note already holds the marker. ``ensure_collection`` and
``ensure_collection_chain`` reuse a collection with the same exact name under the same parent, and refuse, with no
write, when more than one matches. ``create_webpage`` returns "already_present" when every collection it names
already holds a webpage with the same exact URL. When only some do, it raises "partial_match" with no write.
Otherwise it creates the webpage and returns the new key.
"""

from __future__ import annotations

import base64
import os
import re
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import urlsplit

from meridian.exception_chain import raise_unchained
from meridian.secret_redaction import redact

Transport = Callable[[str, str, dict[str, str], Any], tuple[int, Any]]
Reader = Callable[[str], tuple[int, Any]]

APP_NAME = "Meridian"
IMPORT_LIMIT_BYTES = 24 * 1024 * 1024
BATCH_LIMIT = 200
ALREADY_ATTACHED = "already_attached"
ALREADY_PRESENT = "already_present"
ALREADY_EXISTS = "already_exists"
CREATED = "created"
DEFAULT_GRANT_POLLS = 300
DEFAULT_GRANT_POLL_SECONDS = 1.0
CHILD_PAGE_SIZE = 100
CHILD_PAGE_LIMIT = 50
COLLECTION_PAGE_SIZE = 100
COLLECTION_PAGE_LIMIT = 100
ITEM_PAGE_SIZE = 100
ITEM_PAGE_LIMIT = 100

_LOCAL_USER_ID = "0"
_PDF_MAGIC = b"%PDF-"
_KEY = re.compile(r"[A-Za-z0-9]{8}")
_GRANT_ID = re.compile(r"[0-9a-f]{32}")
_MARKER = re.compile(r"[A-Za-z0-9_.-]{8,128}")
_TOKEN_SHAPE = re.compile(r"sk_meridian_[A-Za-z0-9_-]+")
_GRANT_KEY_SHAPE = re.compile(r"\b[0-9a-f]{64}\b")


class ZoteroBridgeError(Exception):
    """A refusal or failure from the bridge, the reader, or this client's own input checks.

    ``code`` is the bridge's error code when the bridge answered. Client-side failures use ``invalid_input``,
    ``not_batchable``, ``not_pdf``, ``file_not_found``, ``file_unreadable``, ``transport_failed``, ``reader_failed`` or
    ``bad_reply``. ``message`` is already scrubbed of secrets.
    """

    def __init__(self, code: str, message: str, *, status: int | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.status = status


class GrantError(ZoteroBridgeError):
    """The grant was refused, denied, expired, revoked, or never answered. Nothing was written under it."""


class BatchRolledBackError(ZoteroBridgeError):
    """A batch was rolled back. ``failed_index`` names the operation that failed, and ``inner_code`` its error code."""

    def __init__(self, message: str, *, status: int, failed_index: int | None, inner_code: str | None) -> None:
        super().__init__("batch_rolled_back", message, status=status)
        self.failed_index = failed_index
        self.inner_code = inner_code


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _invalid(name: str, reason: str) -> ZoteroBridgeError:
    # The message names the field but never echoes the value.
    return ZoteroBridgeError("invalid_input", f"{name} {reason}.")


def _key(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _KEY.fullmatch(value):
        raise _invalid(name, "must be an 8-character Zotero key")
    return value


def _keys(values: Any, name: str) -> list[str]:
    if not isinstance(values, (list, tuple)):
        raise _invalid(name, "must be a list of Zotero keys")
    return [_key(value, f"{name}[{index}]") for index, value in enumerate(values)]


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _invalid(name, "must be a non-empty string")
    return value


def _http_url(value: Any) -> str:
    if isinstance(value, str) and not any(ch.isspace() for ch in value):
        try:
            parts = urlsplit(value)
        except ValueError:
            parts = None
        if parts is not None and parts.scheme in ("http", "https") and parts.netloc:
            return value
    raise _invalid("url", "must be an absolute http or https URL")


_BATCH_KEY = r"[A-Za-z0-9]{8}"
# The only routes a batch may carry, as (method, path pattern). extensions/zotero-bridge/README.md lists the same set
# under "Batches": item create, update, trash and restore; note create and update; relation add and remove; collection
# create, update, trash and restore; and adding or removing items in a collection. Every other route is refused.
# This client deliberately leaves out note create (POST /v1/items/<key>/notes), although the plugin accepts it. A note
# created in a batch skips the dedup marker check, so note creates go through add_child_note instead. The README is
# not edited to match. Item and collection creates stay on the list and are not deduplicated inside a batch.
_BATCHABLE_ROUTES: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (method, re.compile(pattern))
    for method, pattern in (
        ("POST", r"/v1/items"),
        ("PATCH", rf"/v1/items/{_BATCH_KEY}"),
        ("POST", rf"/v1/items/{_BATCH_KEY}/trash"),
        ("POST", rf"/v1/items/{_BATCH_KEY}/restore"),
        ("PATCH", rf"/v1/notes/{_BATCH_KEY}"),
        ("POST", rf"/v1/items/{_BATCH_KEY}/relations"),
        ("DELETE", rf"/v1/items/{_BATCH_KEY}/relations"),
        ("POST", r"/v1/collections"),
        ("PATCH", rf"/v1/collections/{_BATCH_KEY}"),
        ("POST", rf"/v1/collections/{_BATCH_KEY}/trash"),
        ("POST", rf"/v1/collections/{_BATCH_KEY}/restore"),
        ("POST", rf"/v1/collections/{_BATCH_KEY}/items"),
        ("DELETE", rf"/v1/collections/{_BATCH_KEY}/items"),
    )
)
_BATCH_REFUSED_ROUTE = (
    "targets an attachment, a tag rename or a BibTeX import. The bridge refuses those inside a batch, "
    "so send them on their own."
)
_BATCH_NOTE_CREATE_REFUSAL = (
    "creates a child note. Use zotero_add_child_note instead, which checks the dedup marker first, so a rerun does "
    "not add the note twice."
)


def _batch_refusal(method: str, path: str) -> str | None:
    """Why a batch may not carry this route, or None when it may. The reason names a category and never the path."""
    segments = [part for part in path.split("?", 1)[0].split("/") if part]
    if len(segments) == 4 and segments[:2] == ["v1", "items"] and segments[3] == "attachments":
        return _BATCH_REFUSED_ROUTE
    if method == "POST" and len(segments) == 4 and segments[:2] == ["v1", "items"] and segments[3] == "notes":
        return _BATCH_NOTE_CREATE_REFUSAL
    if segments in (["v1", "tags", "rename"], ["v1", "import", "bibtex"]):
        return _BATCH_REFUSED_ROUTE
    if segments[:2] == ["v1", "grants"]:
        return "targets a grant route. A grant is asked for and ended outside a batch."
    if segments == ["v1", "status"]:
        return "targets the status route. A batch carries writes only."
    if segments == ["v1", "batch"]:
        return "targets the batch route. A batch cannot hold another batch."
    if any(allowed == method and pattern.fullmatch(path) for allowed, pattern in _BATCHABLE_ROUTES):
        return None
    return (
        "is not a route a batch may carry. A batch carries item, note, relation, collection and "
        "collection-membership writes only."
    )


def _batch_entry(operation: Any, index: int) -> dict[str, Any]:
    name = f"operations[{index}]"
    if not isinstance(operation, dict):
        raise _invalid(name, "must be an object with method, path and body")
    method = operation.get("method")
    path = operation.get("path")
    body = operation.get("body")
    if not isinstance(method, str) or not method or not isinstance(path, str) or not path:
        raise _invalid(name, "needs a method and a path")
    if body is not None and not isinstance(body, dict):
        raise _invalid(name, "body must be an object")
    reason = _batch_refusal(method, path)
    if reason is not None:
        raise ZoteroBridgeError("not_batchable", f"{name} {reason}")
    entry: dict[str, Any] = {"method": method, "path": path}
    if body is not None:
        entry["body"] = body
    return entry


def _new_key(reply: Any) -> str:
    key = reply.get("key") if isinstance(reply, dict) else None
    if not isinstance(key, str) or not _KEY.fullmatch(key):
        raise ZoteroBridgeError("bad_reply", "The bridge reply did not name the new item.")
    return key


def _error_code(reply: Any) -> str | None:
    error = reply.get("error") if isinstance(reply, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    return code if isinstance(code, str) else None


def _refused_for_grant(status: int, reply: Any) -> bool:
    return status == 403 and _error_code(reply) == "grant_required"


def _parse_utc(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _is_pdf_attachment(child: dict[str, Any]) -> bool:
    data = child.get("data")
    if not isinstance(data, dict) or data.get("itemType") != "attachment":
        return False
    filename = data.get("filename")
    return data.get("contentType") == "application/pdf" or (
        isinstance(filename, str) and filename.lower().endswith(".pdf")
    )


def _is_note_with_marker(child: dict[str, Any], marker: str) -> bool:
    """True when the note carries the marker as written by add_child_note: a quoted attribute, not the bare text.

    The bare text would match a longer marker that starts with it (v1 inside v10), and a note that merely mentions the
    marker in its prose. The quoted attribute avoids both. A note whose text contains that exact attribute still
    matches, since it cannot be told apart from one add_child_note wrote.
    """
    data = child.get("data")
    if not isinstance(data, dict) or data.get("itemType") != "note":
        return False
    note = data.get("note")
    return isinstance(note, str) and f'data-meridian-dedup="{marker}"' in note


def _unreadable_collection() -> ZoteroBridgeError:
    return ZoteroBridgeError(
        "reader_failed",
        "The Zotero local API returned a collection this check cannot read, so the duplicate check did not run.",
    )


def _collection_row(row: Any) -> dict[str, Any]:
    """One collection from Zotero's local API as {key, name, parent_key}.

    Raises rather than guess at a row it cannot read. Zotero reports a top-level collection with ``parentCollection``
    set to false. Any other value must be a key.
    """
    data = row.get("data") if isinstance(row, dict) else None
    if not isinstance(data, dict):
        raise _unreadable_collection()
    key = data.get("key") or row.get("key")
    name = data.get("name")
    if not isinstance(key, str) or not _KEY.fullmatch(key) or not isinstance(name, str):
        raise _unreadable_collection()
    if "parentCollection" not in data:
        # A missing field is not the same as a top-level collection, so it is refused rather than guessed at.
        raise _unreadable_collection()
    parent = data["parentCollection"]
    if parent is False:
        parent_key = None
    elif isinstance(parent, str) and _KEY.fullmatch(parent):
        parent_key = parent
    else:
        raise _unreadable_collection()
    return {"key": key, "name": name, "parent_key": parent_key}


def _matching_keys(collections: list[dict[str, Any]], name: str, parent_key: str | None) -> list[str]:
    """Keys of the collections whose name and parent are exactly these. A top-level match has parent_key None."""
    return [row["key"] for row in collections if row["name"] == name and row["parent_key"] == parent_key]


def _ambiguous_collection() -> ZoteroBridgeError:
    return ZoteroBridgeError(
        "ambiguous_collection",
        "More than one collection has that exact name under the same parent, so nothing was written. "
        "Rename one of them, then retry.",
    )


def _unreadable_webpage() -> ZoteroBridgeError:
    return ZoteroBridgeError(
        "reader_failed",
        "The Zotero local API returned an item this check cannot read, so the duplicate check did not run.",
    )


def _unreadable_child() -> ZoteroBridgeError:
    return ZoteroBridgeError(
        "reader_failed",
        "The Zotero local API returned a child item this check cannot read, so the duplicate check did not run.",
    )


def _readable_child(row: Any) -> bool:
    """True when a child row has the data object and the itemType that the PDF and note checks read."""
    data = row.get("data") if isinstance(row, dict) else None
    return isinstance(data, dict) and isinstance(data.get("itemType"), str)


def _webpage_key(row: Any, url: str) -> str | None:
    """The key of a webpage item in ``row`` whose URL is exactly ``url``. None for a readable row of another kind.

    Raises rather than skip a row it cannot read. A skipped webpage could hold the URL, and a rerun would then create a
    second copy.
    """
    data = row.get("data") if isinstance(row, dict) else None
    if not isinstance(data, dict):
        raise _unreadable_webpage()
    item_type = data.get("itemType")
    if not isinstance(item_type, str):
        raise _unreadable_webpage()
    if item_type != "webpage":
        return None
    key = data.get("key") or row.get("key")
    page_url = data.get("url")
    if not isinstance(key, str) or not _KEY.fullmatch(key) or not isinstance(page_url, str):
        raise _unreadable_webpage()
    return key if page_url == url else None


class CollectionChain(NamedTuple):
    """A Meridian project mapped to a top-level collection, and an optional subproject under it.

    Each status is "created" or "already_exists". ``subproject_key`` and its status are None when no subproject
    was asked for.
    """

    project_key: str
    project_status: str
    subproject_key: str | None
    subproject_status: str | None


class ZoteroBridgeClient:
    """Writes to the bridge through one grant key held for the client's lifetime. Not thread-safe: use one per process."""

    def __init__(
        self,
        transport: Transport,
        reader: Reader,
        *,
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] | None = None,
        grant_polls: int = DEFAULT_GRANT_POLLS,
        grant_poll_seconds: float = DEFAULT_GRANT_POLL_SECONDS,
    ) -> None:
        if transport is None or reader is None:
            raise ValueError("ZoteroBridgeClient requires an injected transport and reader (see the module docstring)")
        if grant_polls < 1:
            raise ValueError("grant_polls must be at least 1")
        self._transport = transport
        self._reader = reader
        self._now = now or _utc_now
        self._sleep = sleep or time.sleep
        self._grant_polls = grant_polls
        self._grant_poll_seconds = grant_poll_seconds
        self._key: str | None = None
        self._expires_at: datetime | None = None
        # Every grant key this client has held. Error text is scrubbed of each one.
        self._secrets: set[str] = set()

    # ----- collections -------------------------------------------------------------------------

    def create_collection(self, name: str, parent_key: str | None = None) -> str:
        body: dict[str, Any] = {"name": _text(name, "name")}
        if parent_key is not None:
            body["parent_key"] = _key(parent_key, "parent_key")
        return _new_key(self._write("POST", "/v1/collections", body))

    def move_collection(self, key: str, parent_key: str | None) -> Any:
        """Moves a collection under ``parent_key``. ``None`` moves it to the top level."""
        key = _key(key, "key")
        parent = None if parent_key is None else _key(parent_key, "parent_key")
        return self._write("PATCH", f"/v1/collections/{key}", {"parent_key": parent})

    def list_collections(self) -> list[dict[str, Any]]:
        """Every collection in the local library as {key, name, parent_key}. parent_key is None at the top level.

        Read-only, through the reader. It fails rather than returning a partial list, and it fails on any row it
        cannot read.
        """
        rows = self._read_pages(
            f"/api/users/{_LOCAL_USER_ID}/collections",
            COLLECTION_PAGE_SIZE,
            COLLECTION_PAGE_LIMIT,
            "The library has more collections than the duplicate check reads, so it did not run.",
        )
        return [_collection_row(row) for row in rows]

    def ensure_collection(self, name: str, parent_key: str | None = None) -> tuple[str, str]:
        """Returns ``(status, key)``: ("already_exists", key) when one collection has this name under ``parent_key``,
        or ("created", key) after creating it there. ``parent_key`` None means the top level.

        Names match exactly and case-sensitively, with no trimming. More than one match raises
        ``ambiguous_collection`` with no write, because the name cannot tell them apart. A renamed collection is not
        found, so a second one is created.
        """
        name = _text(name, "name")
        parent = None if parent_key is None else _key(parent_key, "parent_key")
        matches = _matching_keys(self.list_collections(), name, parent)
        if len(matches) > 1:
            raise _ambiguous_collection()
        if matches:
            return ALREADY_EXISTS, matches[0]
        return CREATED, self.create_collection(name, parent)

    def ensure_collection_chain(self, project_name: str, subproject_name: str | None = None) -> CollectionChain:
        """Maps a Meridian project to a top-level collection, and an optional subproject to a collection under it.

        One read of the library serves both lookups, and every ambiguity is refused before any write. A subproject is
        matched by its exact name under the project's collection, so one subproject name under two projects gives two
        collections. A project with no collection yet gets a new one, and so does its subproject. A missing project
        cannot already hold a subproject, so the subproject is created without a lookup.
        """
        project = _text(project_name, "project_name")
        subproject = None if subproject_name is None else _text(subproject_name, "subproject_name")
        collections = self.list_collections()
        parents = _matching_keys(collections, project, None)
        if len(parents) > 1:
            raise _ambiguous_collection()
        existing_parent = parents[0] if parents else None
        children: list[str] = []
        if subproject is not None and existing_parent is not None:
            children = _matching_keys(collections, subproject, existing_parent)
            if len(children) > 1:
                raise _ambiguous_collection()
        if existing_parent is None:
            project_key, project_status = self.create_collection(project), CREATED
        else:
            project_key, project_status = existing_parent, ALREADY_EXISTS
        if subproject is None:
            return CollectionChain(project_key, project_status, None, None)
        if children:
            return CollectionChain(project_key, project_status, children[0], ALREADY_EXISTS)
        return CollectionChain(project_key, project_status, self.create_collection(subproject, project_key), CREATED)

    # ----- items -------------------------------------------------------------------------------

    def move_item(
        self,
        item_key: str,
        add_collections: list[str] | tuple[str, ...] = (),
        remove_collections: list[str] | tuple[str, ...] = (),
    ) -> Any:
        """Adds the item to ``add_collections`` and takes it out of ``remove_collections``. Both may be used at once."""
        item_key = _key(item_key, "item_key")
        add = _keys(list(add_collections), "add_collections")
        remove = _keys(list(remove_collections), "remove_collections")
        if not add and not remove:
            raise _invalid("collections", "need at least one collection to add or remove")
        if set(add) & set(remove):
            raise _invalid("collections", "cannot add and remove the same collection")
        body: dict[str, Any] = {}
        if add:
            body["add_collections"] = add
        if remove:
            body["remove_collections"] = remove
        return self._write("PATCH", f"/v1/items/{item_key}", body)

    def create_webpage(
        self, title: str, url: str, parent_collection_keys: list[str] | tuple[str, ...] = ()
    ) -> tuple[str, str]:
        """Creates a webpage item and returns ``(status, key)``: ("created", the new key), or ("already_present", the
        existing key) with no write.

        With collection keys, each collection is read first. When every one of them already holds a webpage item with
        exactly this URL, the existing key comes back. A URL in some of the collections but not all raises
        ``partial_match`` with no write, so a copy is neither duplicated nor silently skipped. Without collection keys
        nothing is read, so a rerun creates the item again.
        """
        fields = {"title": _text(title, "title"), "url": _http_url(url)}
        body: dict[str, Any] = {"item_type": "webpage", "fields": fields}
        collections = _keys(list(parent_collection_keys), "parent_collection_keys")
        if collections:
            found = [keys[0] for keys in self._webpage_keys_by_collection(collections, fields["url"]) if keys]
            per_collection = len(dict.fromkeys(collections))
            if found and len(found) == per_collection:
                return ALREADY_PRESENT, found[0]
            if found:
                raise ZoteroBridgeError(
                    "partial_match",
                    "Some of the collections already hold this URL and others do not, so nothing was written. "
                    "Add the existing item to the missing collections, or remove it, then retry.",
                )
            body["collection_keys"] = collections
        return CREATED, _new_key(self._write("POST", "/v1/items", body))

    def attach_pdf(self, item_key: str, path: str, title: str | None = None) -> str:
        """Attaches a PDF to ``item_key``. Returns the new attachment key, or "already_attached" with no write.

        Files of 24 MiB or less are imported as base64 data. Larger files are linked by absolute path, and the bridge
        decides whether it may link them. Its refusal is raised, not hidden.
        """
        item_key = _key(item_key, "item_key")
        if not isinstance(path, str) or not os.path.isabs(path):
            raise _invalid("path", "must be an absolute path")
        if title is not None and not isinstance(title, str):
            raise _invalid("title", "must be a string")
        if any(_is_pdf_attachment(child) for child in self._children(item_key)):
            return ALREADY_ATTACHED
        if not os.path.isfile(path):
            raise ZoteroBridgeError("file_not_found", "No file exists at that path.")
        try:
            size = os.path.getsize(path)
            if size <= IMPORT_LIMIT_BYTES:
                data: bytes | None = Path(path).read_bytes()
                head = data[: len(_PDF_MAGIC)]
            else:
                data = None
                with open(path, "rb") as handle:
                    head = handle.read(len(_PDF_MAGIC))
        except OSError:
            raise ZoteroBridgeError("file_unreadable", "The file could not be read.") from None
        if head != _PDF_MAGIC:
            raise ZoteroBridgeError("not_pdf", "Only a file that starts with %PDF- can be attached.")
        body: dict[str, Any]
        if data is not None:
            body = {
                "mode": "imported_file",
                "filename": os.path.basename(path),
                "data_base64": base64.b64encode(data).decode("ascii"),
                "content_type": "application/pdf",
            }
        else:
            body = {"mode": "linked_file", "path": path, "content_type": "application/pdf"}
        if title is not None:
            body["title"] = title
        return _new_key(self._write("POST", f"/v1/items/{item_key}/attachments", body))

    def add_child_note(self, parent_key: str, html: str, dedup_marker: str) -> str:
        """Adds a child note holding ``html`` and ``dedup_marker``. Returns "already_present" when a child already has it."""
        parent_key = _key(parent_key, "parent_key")
        if not isinstance(html, str):
            raise _invalid("html", "must be a string")
        if not isinstance(dedup_marker, str) or not _MARKER.fullmatch(dedup_marker):
            raise _invalid("dedup_marker", "must be 8 to 128 letters, digits, dots, underscores or hyphens")
        if any(_is_note_with_marker(child, dedup_marker) for child in self._children(parent_key)):
            return ALREADY_PRESENT
        wrapped = f'<div data-meridian-dedup="{dedup_marker}">{html}</div>'
        return _new_key(self._write("POST", f"/v1/items/{parent_key}/notes", {"html": wrapped}))

    # ----- batches -----------------------------------------------------------------------------

    def batch(self, operations: list[dict[str, Any]] | tuple[dict[str, Any], ...]) -> list[Any]:
        """Runs 1 to 200 operations in one bridge transaction.

        Only the routes in _BATCHABLE_ROUTES are sent. Every operation is checked first, and any other route, method or
        category is refused before anything is sent: grant routes, the status route, a nested batch, attachments, tag
        rename, BibTeX import, and any unlisted method or path. A rollback raises BatchRolledBackError with the failed
        operation's index. Nothing is applied in part. Item and collection creates are sent as written and are not
        deduplicated, so a rerun of a batch that creates them writes them again. Use create_webpage or ensure_collection
        for a create that must not repeat.
        """
        if not isinstance(operations, (list, tuple)):
            raise _invalid("operations", "must be a list")
        if not 1 <= len(operations) <= BATCH_LIMIT:
            raise _invalid("operations", f"must hold 1 to {BATCH_LIMIT} operations")
        plan = [_batch_entry(operation, index) for index, operation in enumerate(operations)]
        reply = self._write("POST", "/v1/batch", {"operations": plan})
        results = reply.get("results") if isinstance(reply, dict) else None
        if not isinstance(results, list):
            raise ZoteroBridgeError("bad_reply", "The bridge reply carried no batch results.")
        return results

    # ----- writes and grants -------------------------------------------------------------------

    def _write(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        """One write under a live grant key. Asks for a new grant once, and only after a grant_required 403."""
        key = self._ensure_key()
        status, reply = self._call(method, path, body, grant=key)
        if _refused_for_grant(status, reply):
            self._forget_key()
            key = self._acquire_grant()
            status, reply = self._call(method, path, body, grant=key)
            if _refused_for_grant(status, reply):
                self._forget_key()
                raise GrantError(
                    "grant_required",
                    "The bridge refused the write again under a new grant. Nothing was written, and it was not retried.",
                )
        if not 200 <= status < 300:
            raise self._bridge_error(status, reply)
        return reply

    def _ensure_key(self) -> str:
        if self._key is not None and self._expires_at is not None and self._now() < self._expires_at:
            return self._key
        return self._acquire_grant()

    def _forget_key(self) -> None:
        self._key = None
        self._expires_at = None

    def _acquire_grant(self) -> str:
        """Requests a grant and polls it up to ``grant_polls`` times. A granted reply carries the key, once."""
        self._forget_key()
        status, reply = self._call("POST", "/v1/grants", {"app_name": APP_NAME})
        if not 200 <= status < 300:
            raise self._grant_failure(status, reply)
        grant_id = reply.get("grant_id") if isinstance(reply, dict) else None
        if not isinstance(grant_id, str) or not _GRANT_ID.fullmatch(grant_id):
            raise GrantError("grant_bad_reply", "The bridge did not return a grant id.")
        for check in range(self._grant_polls):
            if check:
                self._sleep(self._grant_poll_seconds)
            status, reply = self._call("GET", f"/v1/grants/{grant_id}")
            if not 200 <= status < 300:
                raise self._grant_failure(status, reply)
            state = reply.get("status") if isinstance(reply, dict) else None
            if state == "pending":
                continue
            if state == "granted":
                return self._take_key(reply)
            if state in ("denied", "expired", "revoked"):
                raise GrantError(f"grant_{state}", f"The desktop answered the grant request: {state}.")
            raise GrantError("grant_bad_reply", "The grant reply had an unknown status.")
        self._end_grant(grant_id)
        raise GrantError(
            "grant_timeout",
            f"No answer at the desktop after {self._grant_polls} checks. The request was ended.",
        )

    def _grant_failure(self, status: int, reply: Any) -> GrantError:
        """A bridge refusal on a grant route, typed as GrantError so callers can catch one class for the whole grant flow."""
        error = self._bridge_error(status, reply)
        return GrantError(error.code, error.message, status=error.status)

    def _take_key(self, reply: dict[str, Any]) -> str:
        key = reply.get("write_key")
        expires_at = _parse_utc(reply.get("expires_at"))
        if not isinstance(key, str) or not key or expires_at is None:
            raise GrantError("grant_bad_reply", "The granted reply had no usable key or expiry.")
        self._key = key
        self._expires_at = expires_at
        self._secrets.add(key)
        return key

    def _end_grant(self, grant_id: str) -> None:
        try:
            self._call("DELETE", f"/v1/grants/{grant_id}")
        except ZoteroBridgeError:
            pass  # Best effort. The timeout error is the one the caller needs.

    # ----- transport, reader and error mapping -------------------------------------------------

    def _call(
        self, method: str, path: str, body: dict[str, Any] | None = None, grant: str | None = None
    ) -> tuple[int, Any]:
        headers: dict[str, str] = {}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if grant is not None:
            headers["Meridian-Write-Grant"] = grant
        failed = False
        try:
            status, reply = self._transport(method, path, headers, body)
        except Exception:  # noqa: BLE001 - the transport's text can hold the token, so none of it is kept
            failed = True
        if failed:
            raise_unchained(ZoteroBridgeError("transport_failed", "The local write API did not answer."))
        return status, reply

    def _read_list(self, path: str) -> list[Any]:
        failed = False
        try:
            status, body = self._reader(path)
        except Exception:  # noqa: BLE001 - a reader failure means the duplicate check cannot run
            failed = True
        if failed:
            raise_unchained(
                ZoteroBridgeError("reader_failed", "The Zotero local API did not answer, so the duplicate check did not run.")
            )
        if status != 200 or not isinstance(body, list):
            raise ZoteroBridgeError(
                "reader_failed",
                f"The Zotero local API answered {status}, so the duplicate check did not run.",
                status=status,
            )
        return body

    def _read_pages(self, base: str, page_size: int, page_limit: int, too_many: str) -> list[Any]:
        """Every row under ``base``, read a page at a time. It fails with ``too_many`` only when a row exists past
        ``page_limit`` full pages. A list of exactly that many rows is returned whole."""
        found: list[Any] = []
        for page in range(page_limit):
            rows = self._read_list(f"{base}?format=json&limit={page_size}&start={page * page_size}")
            found.extend(rows)
            if len(rows) < page_size:
                return found
        beyond = self._read_list(f"{base}?format=json&limit={page_size}&start={page_limit * page_size}")
        if beyond:
            raise ZoteroBridgeError("reader_failed", too_many)
        return found

    def _children(self, parent_key: str) -> list[dict[str, Any]]:
        """Every child of an item, read a page at a time. Fails rather than returning a partial list, and fails on a
        child it cannot read, since a skipped child could be the PDF or the note a rerun is checking for."""
        rows = self._read_pages(
            f"/api/users/{_LOCAL_USER_ID}/items/{parent_key}/children",
            CHILD_PAGE_SIZE,
            CHILD_PAGE_LIMIT,
            "The item has more children than the duplicate check reads, so it did not run.",
        )
        if not all(_readable_child(row) for row in rows):
            raise _unreadable_child()
        return rows

    def _webpage_keys_by_collection(self, collection_keys: list[str], url: str) -> list[list[str]]:
        """For each distinct collection, the keys of its webpage items whose URL is exactly ``url``. Reads only."""
        by_collection: list[list[str]] = []
        for collection_key in dict.fromkeys(collection_keys):
            rows = self._read_pages(
                f"/api/users/{_LOCAL_USER_ID}/collections/{collection_key}/items",
                ITEM_PAGE_SIZE,
                ITEM_PAGE_LIMIT,
                "A collection has more items than the duplicate check reads, so it did not run.",
            )
            keys = [key for key in (_webpage_key(row, url) for row in rows) if key is not None]
            by_collection.append(keys)
        return by_collection

    def _clean(self, text: Any) -> str:
        out = str(text)
        for secret in self._secrets:
            out = out.replace(secret, "[redacted]")
        out = _GRANT_KEY_SHAPE.sub("[redacted]", out)
        out = _TOKEN_SHAPE.sub("[redacted]", out)
        return redact(out)

    def _bridge_error(self, status: int, reply: Any) -> ZoteroBridgeError:
        error = reply.get("error") if isinstance(reply, dict) else None
        if not isinstance(error, dict) or not isinstance(error.get("code"), str):
            return ZoteroBridgeError(
                f"http_{status}", f"The bridge answered {status} without an error code.", status=status
            )
        code = self._clean(error["code"])
        message = self._clean(error.get("message") or "The bridge refused the request.")
        if code == "batch_rolled_back":
            details = error.get("details") if isinstance(error.get("details"), dict) else {}
            failed_index = details.get("failed_index")
            inner = details.get("error")
            inner_code = inner.get("code") if isinstance(inner, dict) else None
            return BatchRolledBackError(
                message,
                status=status,
                failed_index=failed_index if isinstance(failed_index, int) else None,
                inner_code=self._clean(inner_code) if isinstance(inner_code, str) else None,
            )
        error_class = GrantError if code.startswith("grant_") else ZoteroBridgeError
        return error_class(code, message, status=status)
