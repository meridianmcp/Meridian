"""File research items into ONE named Zotero collection, safely.

Standard library only. Talks to the Zotero desktop app over its loopback
connector and local API (127.0.0.1:23119). The desktop path needs no API keys.
The Web API fallback reads ZOTERO_USER_ID and ZOTERO_API_KEY from the
environment, and only when the desktop app cannot be reached.

Safety rules enforced here:

* Zotero's connector saves into whatever collection the user has selected in
  the UI, so that selection is the only signal trusted for where items land.
  The selection is checked by exact name before the first save and again
  after every batch.
* The target collection is matched by exact name. A missing or ambiguous name
  stops the run. Nothing is matched fuzzily.
* Steps run in order and the run stops at the first failed step. Nothing is
  written unless the collection exists, the selection matches, and the run is
  not a dry run.
* Items already in the collection (matched by DOI or lowercase title) are
  skipped.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Callable

from meridian import zotero_launcher

CONNECTOR = "http://127.0.0.1:23119/connector"
LOCAL_API = "http://127.0.0.1:23119/api/users/0"
SAVE_URI = "https://meridian.local/research"

_ALLOWED_HEADER = "Zotero-Allowed-Request"
_TIMEOUT_S = 10.0
_PAGE_SIZE = 100
_BATCH_SIZE = 25
_COLLECTION_KEY_RE = re.compile(r"[A-Za-z0-9]{8}")
_DOI_PREFIX_RE = re.compile(r"^(?:https?://)?(?:dx\.)?doi\.org/", re.IGNORECASE)
_ARXIV_ID_RE = re.compile(r"[A-Za-z0-9./_-]{1,64}")
_NON_TOP_LEVEL_TYPES = frozenset({"attachment", "note", "annotation"})

# A transport performs one HTTP request and returns (status, body_text).
# Signature: transport(method, url, body_bytes_or_None, headers_dict).
Transport = Callable[[str, str, bytes | None, dict[str, str]], tuple[int, str]]

_ENTRY_START_RE = re.compile(r"@([A-Za-z]+)\s*([{(])")
_FIELD_START_RE = re.compile(r"[\s,]*([A-Za-z][A-Za-z0-9_.:\-]*)\s*=\s*")
_BARE_VALUE_RE = re.compile(r"[^\s,#{}\"]+")
_SPACE_RE = re.compile(r"\s*")
_AND_RE = re.compile(r"\s+and\s+")
_SKIPPED_ENTRY_TYPES = frozenset({"comment", "string", "preamble"})


class ZoteroError(RuntimeError):
    """Zotero was unreachable, answered with an error, or sent unusable data."""


_ERRORS = (ZoteroError, OSError, http.client.HTTPException)

# Loopback only: never route the Zotero connector through a system proxy.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _urllib_transport(method, url, body, headers):
    """Default transport: one real HTTP request to the loopback Zotero server.

    HTTP error statuses are returned, not raised. Connection errors propagate
    as OSError and are handled by the callers.
    """
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with _OPENER.open(request, timeout=_TIMEOUT_S) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def _resolve(transport):
    """Return the injected transport, or the real one when none was given."""
    return _urllib_transport if transport is None else transport


def _send(transport, method, url, payload=None):
    """Send one request. JSON payloads get a Content-Type header. Returns (status, text)."""
    headers = {_ALLOWED_HEADER: "1"}
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    return transport(method, url, body, headers)


def _decode_json(status, text, what):
    """Parse a 2xx JSON body, or raise ZoteroError."""
    if not 200 <= status < 300:
        raise ZoteroError(f"{what} failed with HTTP {status}")
    try:
        return json.loads(text)
    except ValueError as exc:
        raise ZoteroError(f"{what} returned data that is not JSON") from exc


def _get_json(transport, url):
    """GET a URL and return the decoded JSON body."""
    status, text = _send(transport, "GET", url)
    return _decode_json(status, text, f"GET {url}")


def _check_collection_key(key):
    """Reject anything that is not a Zotero collection key before it reaches a URL."""
    if not _COLLECTION_KEY_RE.fullmatch(key or ""):
        raise ZoteroError(f"invalid Zotero collection key: {key!r}")
    return key


def _norm_doi(value):
    """Lowercase DOI with any doi.org prefix removed. Empty string when absent."""
    return _DOI_PREFIX_RE.sub("", (value or "").strip()).lower()


def _norm_title(value):
    """Lowercase title with whitespace collapsed. Empty string when absent."""
    return re.sub(r"\s+", " ", (value or "")).strip().lower()


def _keys_for(doi, title):
    """Dedupe keys for one item: its normalized DOI and normalized title."""
    keys = set()
    norm_doi = _norm_doi(doi)
    norm_title = _norm_title(title)
    if norm_doi:
        keys.add(norm_doi)
    if norm_title:
        keys.add(norm_title)
    return keys


def _item_keys(item):
    """Dedupe keys for a connector-format item."""
    return _keys_for(item.get("DOI"), item.get("title"))


def list_collections(transport: Transport | None = None) -> list[dict[str, str]]:
    """Return every collection in the local library as {key, name} dicts.

    Follows pagination: a full page (100 items) triggers another request.
    Raises ZoteroError if the local API is disabled or returns bad data.
    """
    transport = _resolve(transport)
    collections: list[dict[str, str]] = []
    seen_keys: set[str] = set()
    start = 0
    while True:
        url = f"{LOCAL_API}/collections?limit={_PAGE_SIZE}"
        if start:
            url += f"&start={start}"
        page = _get_json(transport, url)
        if not isinstance(page, list):
            raise ZoteroError("unexpected response while listing collections")
        fresh = 0
        for obj in page:
            if not isinstance(obj, dict):
                continue
            data = obj.get("data") if isinstance(obj.get("data"), dict) else obj
            key = obj.get("key") or data.get("key")
            if not key or key in seen_keys:
                continue
            seen_keys.add(key)
            fresh += 1
            collections.append({"key": key, "name": data.get("name") or ""})
        if len(page) < _PAGE_SIZE or fresh == 0:
            break
        start += len(page)
    return collections


def find_collection_key(name: str, transport: Transport | None = None) -> str | None:
    """Return the key of the collection whose name equals name exactly.

    Returns None when no collection has that exact name. Never falls back to a
    fuzzy match. Raises ZoteroError when two collections share the name, since
    the name alone cannot tell them apart.
    """
    matches = [c["key"] for c in list_collections(transport) if c["name"] == name]
    if not matches:
        return None
    if len(matches) > 1:
        raise ZoteroError(f"more than one Zotero collection is named '{name}'; rename one of them")
    return matches[0]


def selected_collection(transport: Transport | None = None) -> dict[str, Any]:
    """Return the collection currently selected in the Zotero UI.

    The result has keys id, name and targets. This is the only signal trusted
    for where saveItems will write.
    """
    transport = _resolve(transport)
    status, text = _send(transport, "POST", CONNECTOR + "/getSelectedCollection", {})
    data = _decode_json(status, text, "getSelectedCollection")
    if not isinstance(data, dict):
        raise ZoteroError("getSelectedCollection returned an unexpected shape")
    return {"id": data.get("id"), "name": data.get("name"), "targets": data.get("targets") or []}


def _is_windows() -> bool:
    """True when running on Windows. Split out so tests can exercise both branches."""
    return sys.platform == "win32"


def open_collection_in_zotero(key: str) -> bool:
    """Ask the desktop Zotero to select a collection, via a zotero:// link.

    Runs only on Windows, and only for a well-formed 8-character key. Returns
    True when the link was handed to the OS, False otherwise.
    """
    if not _is_windows() or not _COLLECTION_KEY_RE.fullmatch(key or ""):
        return False
    try:
        os.startfile("zotero://select/library/collections/" + key)  # type: ignore[attr-defined]
    except OSError:
        return False
    return True


def _selection_matches(name: str, transport: Transport) -> bool:
    """True when the UI selection is exactly the named collection right now."""
    return selected_collection(transport).get("name") == name


def wait_for_selection(
    name: str,
    transport: Transport | None = None,
    timeout_s: float = 20,
    poll_s: float = 1.0,
    sleep: Callable[[float], Any] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> bool:
    """Poll the UI selection until it shows the named collection.

    Returns True as soon as the selection matches, or False after timeout_s.
    Connection errors during polling count as "not yet" and polling continues.
    """
    transport = _resolve(transport)
    deadline = clock() + timeout_s
    while True:
        try:
            if _selection_matches(name, transport):
                return True
        except _ERRORS:
            pass
        if clock() >= deadline:
            return False
        sleep(poll_s)


def save_items(items: list[dict[str, Any]], transport: Transport | None = None) -> int | None:
    """Save connector-format items into Zotero's current selection.

    Returns the HTTP status when Zotero accepts the batch (2xx). Returns None on
    any HTTP error status or connection failure.
    """
    transport = _resolve(transport)
    payload = {"sessionID": uuid.uuid4().hex, "uri": SAVE_URI, "items": items}
    try:
        status, _ = _send(transport, "POST", CONNECTOR + "/saveItems", payload)
    except _ERRORS:
        return None
    return status if 200 <= status < 300 else None


def _clean(value: str, keep_braces: bool = False) -> str:
    """Tidy a BibTeX field value: unescape common LaTeX, drop braces, collapse spaces."""
    value = re.sub(r"\\([&%$#_])", r"\1", value)
    if not keep_braces:
        value = value.replace("{", "").replace("}", "")
    return re.sub(r"\s+", " ", value).strip()


def _find_close(text: str, open_idx: int) -> int:
    """Index of the delimiter that closes text[open_idx] ('{' or '('), or -1."""
    opener = text[open_idx]
    closer = "}" if opener == "{" else ")"
    depth = 0
    i = open_idx
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _read_braced(text: str, i: int) -> tuple[str, int]:
    """Read a {...} value starting at text[i]. Returns (inner_text, next_index)."""
    close = _find_close(text, i)
    if close < 0:
        return text[i + 1:], len(text)
    return text[i + 1:close], close + 1


def _read_quoted(text: str, i: int) -> tuple[str, int]:
    """Read a "..." value starting at text[i]. Braces inside it are counted."""
    depth = 0
    j = i + 1
    n = len(text)
    while j < n:
        ch = text[j]
        if ch == "\\":
            j += 2
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth = max(depth - 1, 0)
        elif ch == '"' and depth == 0:
            return text[i + 1:j], j + 1
        j += 1
    return text[i + 1:], n


def _parse_fields(text: str) -> dict[str, str]:
    """Parse 'name = value' pairs from an entry body. Names are lowercased.

    Values may be {braced} (nested braces allowed), "quoted", or bare tokens
    such as 2020. Values joined with # are concatenated. Trailing commas and
    stray text are skipped. The first occurrence of a name wins.
    """
    fields: dict[str, str] = {}
    n = len(text)
    i = 0
    while i < n:
        m = _FIELD_START_RE.match(text, i)
        if m is None:
            comma = text.find(",", i)
            i = n if comma < 0 else comma + 1
            continue
        name = m.group(1).lower()
        i = m.end()
        pieces: list[str] = []
        while True:
            i = _SPACE_RE.match(text, i).end()
            if i >= n:
                break
            ch = text[i]
            if ch == "{":
                piece, i = _read_braced(text, i)
            elif ch == '"':
                piece, i = _read_quoted(text, i)
            else:
                bare = _BARE_VALUE_RE.match(text, i)
                if bare is None:
                    break
                piece, i = bare.group(0), bare.end()
            pieces.append(piece)
            j = _SPACE_RE.match(text, i).end()
            if j < n and text[j] == "#":
                i = j + 1
                continue
            break
        fields.setdefault(name, "".join(pieces))
    return fields


def _entry_from_body(entrytype: str, body: str) -> dict[str, str]:
    """Build one parsed entry from the text between an entry's outer delimiters."""
    citekey, _, rest = body.partition(",")
    fields = _parse_fields(rest)
    entry = {"citekey": citekey.strip(), "entrytype": entrytype}
    for name in ("title", "author", "year", "doi", "url", "journal",
                 "booktitle", "eprint", "note", "abstract"):
        # Braces are kept on author so corporate names like {Google DeepMind}
        # can be told apart from "First Last" names later.
        entry[name] = _clean(fields.get(name, ""), keep_braces=(name == "author"))
    return entry


def parse_bibtex(text: str) -> list[dict[str, str]]:
    """Parse BibTeX text into a list of entry dicts.

    Each dict has keys citekey, entrytype (lowercase), title, author, year, doi,
    url, journal, booktitle, eprint, note and abstract. Missing fields are "".
    @comment, @string and @preamble blocks are skipped. Entry bodies are matched
    by brace depth, so nested braces and "@" inside a value do not split entries.
    """
    entries: list[dict[str, str]] = []
    pos = 0
    while True:
        m = _ENTRY_START_RE.search(text, pos)
        if m is None:
            break
        entrytype = m.group(1).lower()
        open_idx = m.end() - 1
        close_idx = _find_close(text, open_idx)
        if close_idx < 0:
            body = text[open_idx + 1:]
            pos = len(text)
        else:
            body = text[open_idx + 1:close_idx]
            pos = close_idx + 1
        if entrytype in _SKIPPED_ENTRY_TYPES:
            continue
        entries.append(_entry_from_body(entrytype, body))
    return entries


def _split_authors(author: str) -> list[str]:
    """Split a BibTeX author list on ' and ' that sits outside braces."""
    parts: list[str] = []
    depth = 0
    start = 0
    i = 0
    while i < len(author):
        ch = author[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth = max(depth - 1, 0)
        elif depth == 0:
            m = _AND_RE.match(author, i)
            if m:
                parts.append(author[start:i])
                i = start = m.end()
                continue
        i += 1
    parts.append(author[start:])
    return [p.strip() for p in parts if p.strip()]


def _creator(name: str) -> dict[str, str]:
    """Turn one BibTeX name into a Zotero creator.

    "Last, First" and "First Last" split into two fields. Braced names, and
    names that cannot be split, use a single name field.
    """
    if "{" in name or "}" in name:
        return {"creatorType": "author", "name": _clean(name)}
    if "," in name:
        last, _, first = name.partition(",")
        if first.strip():
            return {"creatorType": "author", "firstName": first.strip(), "lastName": last.strip()}
        return {"creatorType": "author", "name": name.strip()}
    words = name.split()
    if len(words) < 2:
        return {"creatorType": "author", "name": name.strip()}
    return {"creatorType": "author", "firstName": " ".join(words[:-1]), "lastName": words[-1]}


def bib_to_connector_item(entry: dict[str, str]) -> dict[str, Any]:
    """Convert one parsed BibTeX entry into a Zotero connector item.

    Item type: article becomes journalArticle, inproceedings or conference
    becomes conferencePaper, anything else becomes document. The DOI has any
    doi.org prefix removed. When there is no url but there is an arXiv eprint,
    the url is https://arxiv.org/abs/<eprint>. extra holds "BibTeX key: <key>".
    """
    entrytype = (entry.get("entrytype") or "").lower()
    if entrytype == "article":
        item_type = "journalArticle"
    elif entrytype in ("inproceedings", "conference"):
        item_type = "conferencePaper"
    else:
        item_type = "document"

    item: dict[str, Any] = {
        "itemType": item_type,
        "title": entry.get("title", ""),
        "creators": [_creator(n) for n in _split_authors(entry.get("author", ""))],
    }
    if entry.get("year"):
        item["date"] = entry["year"]
    doi = _norm_doi(entry.get("doi", ""))
    if doi:
        item["DOI"] = _DOI_PREFIX_RE.sub("", (entry.get("doi") or "").strip())
    url = entry.get("url", "")
    eprint = (entry.get("eprint") or "").strip()
    if not url and eprint and _ARXIV_ID_RE.fullmatch(eprint):
        url = "https://arxiv.org/abs/" + eprint
    if url:
        item["url"] = url
    venue = entry.get("journal") or entry.get("booktitle") or ""
    if venue:
        item["publicationTitle"] = venue
    if entry.get("abstract"):
        item["abstractNote"] = entry["abstract"]
    item["extra"] = "BibTeX key: " + (entry.get("citekey") or "")
    return item


def existing_keys(collection_key: str, transport: Transport | None = None) -> set[str]:
    """Return lowercase DOIs and lowercase titles of items already in a collection.

    Attachments, notes and annotations are ignored, since their titles would
    falsely match papers. Follows pagination. Used for dedupe.
    """
    transport = _resolve(transport)
    _check_collection_key(collection_key)
    keys: set[str] = set()
    seen_items: set[str] = set()
    start = 0
    while True:
        url = f"{LOCAL_API}/collections/{collection_key}/items?format=json&limit={_PAGE_SIZE}"
        if start:
            url += f"&start={start}"
        page = _get_json(transport, url)
        if not isinstance(page, list):
            raise ZoteroError("unexpected response while listing collection items")
        fresh = 0
        for obj in page:
            data = obj.get("data") if isinstance(obj, dict) else None
            if not isinstance(data, dict):
                continue
            ident = obj.get("key") or data.get("key")
            if ident is not None:
                if ident in seen_items:
                    continue
                seen_items.add(ident)
            fresh += 1
            if data.get("itemType") in _NON_TOP_LEVEL_TYPES:
                continue
            keys |= _keys_for(data.get("DOI"), data.get("title"))
        if len(page) < _PAGE_SIZE or fresh == 0:
            break
        start += len(page)
    return keys


def _load_bib_entries(paths) -> list[dict[str, str]]:
    """Parse every .bib file. Non-.bib paths are refused. Raises OSError or ValueError."""
    entries: list[dict[str, str]] = []
    for path in paths:
        if os.path.splitext(str(path))[1].lower() != ".bib":
            raise ValueError(f"not a .bib file: {path}")
        with open(path, encoding="utf-8", errors="replace") as handle:
            entries.extend(parse_bibtex(handle.read()))
    return entries


def _result(status, collection, sent=0, added=0, skipped=0):
    """Build the plain result dict every file_bib_files path returns."""
    return {
        "status": status,
        "collection": collection,
        "sent": sent,
        "added": added,
        "skipped_duplicates": skipped,
    }


def file_bib_files(
    paths,
    collection_name: str,
    transport: Transport | None = None,
    dry_run: bool = False,
    log: Callable[[str], Any] = print,
) -> dict[str, Any]:
    """File every entry in the given .bib files into one named Zotero collection.

    Steps run in order and the run stops at the first failed step:

    a. Find the collection by exact name. If it is missing, write nothing.
    b. Parse the .bib files. Drop items whose DOI or title already exists in
       the collection, or repeats within this run.
    c. On a dry run, report what would be filed and stop.
    d. Open the collection in Zotero and wait until it is the UI selection.
    e. Re-check the selection right before saving.
    f. Save in batches of 25. Re-check the selection after each batch.
    g. Count how many of the sent items now appear in the collection.

    Returns {"status", "collection", "sent", "added", "skipped_duplicates"}.
    status is one of: ok, dry_run, no_collection, input_error, zotero_error,
    selection_failed, selection_changed, save_failed, verify_mismatch.
    On a dry run, "sent" is the number of items that would be filed.
    """
    transport = _resolve(transport)
    if isinstance(paths, (str, os.PathLike)):
        paths = [paths]
    existing: set[str] = set()
    sent_items: list[dict[str, Any]] = []
    skipped = 0
    try:
        # a. The collection must exist, matched by exact name.
        key = find_collection_key(collection_name, transport)
        if key is None:
            log(
                f"Create the collection '{collection_name}' in Zotero first "
                "(right-click My Library > New Collection)."
            )
            return _result("no_collection", collection_name)

        # b. Parse the input and drop anything already filed.
        try:
            entries = _load_bib_entries(paths)
        except (OSError, ValueError) as exc:
            log(f"Could not read the BibTeX input: {exc}")
            return _result("input_error", collection_name)

        existing = existing_keys(key, transport)
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        for entry in entries:
            item = bib_to_connector_item(entry)
            keys = _item_keys(item)
            if not keys:
                log(f"Skipping '{entry.get('citekey') or '?'}': it has no title or DOI.")
                continue
            if keys & existing or keys & seen:
                skipped += 1
                continue
            seen |= keys
            items.append(item)
        log(f"{len(items)} new item(s) for '{collection_name}'; {skipped} already present.")

        # c. Dry run stops here, before anything is written.
        if dry_run:
            for item in items:
                log(f"  would file: {item['title'] or item.get('DOI', '')}")
            return _result("dry_run", collection_name, sent=len(items), skipped=skipped)
        if not items:
            log("Nothing new to file.")
            return _result("ok", collection_name, skipped=skipped)

        # d. Select the collection in Zotero and wait until it shows as selected.
        if not open_collection_in_zotero(key):
            log(f"Could not open '{collection_name}' in Zotero automatically. Select it in the Zotero window.")
        if not wait_for_selection(collection_name, transport):
            log(f"Zotero did not show '{collection_name}' as selected. Nothing was saved.")
            return _result("selection_failed", collection_name, skipped=skipped)

        # e. Re-check the selection right before saving.
        if not _selection_matches(collection_name, transport):
            log(f"The selection is no longer '{collection_name}'. Nothing was saved.")
            return _result("selection_changed", collection_name, skipped=skipped)

        # f. Save in batches. Re-check the selection after each batch.
        for start in range(0, len(items), _BATCH_SIZE):
            batch = items[start:start + _BATCH_SIZE]
            # Check again immediately before each batch, so a batch can never go to a different collection.
            if not _selection_matches(collection_name, transport):
                log(f"The selection is no longer '{collection_name}' before batch {start // _BATCH_SIZE + 1}; stopping.")
                return _result(
                    "selection_changed", collection_name, sent=len(sent_items), skipped=skipped
                )
            if save_items(batch, transport) is None:
                log(f"Zotero did not accept a batch after {len(sent_items)} item(s) were saved.")
                return _result("save_failed", collection_name, sent=len(sent_items), skipped=skipped)
            sent_items.extend(batch)
            if not _selection_matches(collection_name, transport):
                log(f"The selection left '{collection_name}' after {len(sent_items)} item(s); stopping.")
                return _result(
                    "selection_changed", collection_name, sent=len(sent_items), skipped=skipped
                )

        # g. Verify: count the sent items that now appear in the collection.
        after = existing_keys(key, transport)
        new_keys = after - existing
        added = sum(1 for item in sent_items if _item_keys(item) & new_keys)
        status = "ok" if added == len(sent_items) else "verify_mismatch"
        log(f"Filed {added} of {len(sent_items)} sent item(s) into '{collection_name}'.")
        return _result(status, collection_name, sent=len(sent_items), added=added, skipped=skipped)
    except _ERRORS as exc:
        log(f"Zotero error: {exc}")
        return _result("zotero_error", collection_name, sent=len(sent_items), skipped=skipped)


# ---------------------------------------------------------------------------
# Web API backend (api.zotero.org). It can create collections and write items.
# It needs a numeric user id and a key with write access, both read from the
# environment only. The key is never printed, logged or written to a file.
# ---------------------------------------------------------------------------

WEB_API = "https://api.zotero.org"
_WEB_BATCH_SIZE = 50  # the Web API accepts at most 50 objects per write request


def web_credentials(env=None) -> tuple[str, str] | None:
    """Return (user_id, api_key) from ZOTERO_USER_ID and ZOTERO_API_KEY, or None."""
    source = os.environ if env is None else env
    user_id = (source.get("ZOTERO_USER_ID") or "").strip()
    api_key = (source.get("ZOTERO_API_KEY") or "").strip()
    if not user_id.isdigit() or not api_key:
        return None
    return user_id, api_key


def _web_send(transport, method, url, api_key, payload=None):
    """One Web API request. The key travels in a header only."""
    headers = {"Zotero-API-Version": "3", "Zotero-API-Key": api_key}
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    return transport(method, url, body, headers)


def _web_raise(status, what):
    """Raise ZoteroError for a failed Web API call. The message never includes the key."""
    if status == 403:
        raise ZoteroError(f"{what} was refused (HTTP 403). The key needs write access to this library.")
    if status == 404:
        raise ZoteroError(f"{what}: library not found (HTTP 404). Check ZOTERO_USER_ID.")
    raise ZoteroError(f"{what} failed with HTTP {status}")


def web_list_collections(user_id, api_key, transport=None) -> list[dict[str, str]]:
    """List every collection in the library through the Web API, following pagination."""
    transport = _resolve(transport)
    found: list[dict[str, str]] = []
    start = 0
    while True:
        url = f"{WEB_API}/users/{user_id}/collections?format=json&limit={_PAGE_SIZE}&start={start}"
        status, text = _web_send(transport, "GET", url, api_key)
        if not 200 <= status < 300:
            _web_raise(status, "listing collections")
        page = _decode_json(status, text, "listing collections")
        if not isinstance(page, list):
            raise ZoteroError("unexpected response while listing collections")
        for obj in page:
            data = obj.get("data") if isinstance(obj, dict) else None
            if isinstance(data, dict) and data.get("key") and data.get("name") is not None:
                found.append({"key": data["key"], "name": data["name"]})
        if len(page) < _PAGE_SIZE:
            break
        start += len(page)
    return found


def web_find_collection_key(user_id, api_key, name, transport=None) -> str | None:
    """Return the key of the collection whose name matches exactly, or None."""
    for row in web_list_collections(user_id, api_key, transport):
        if row["name"] == name:
            return _check_collection_key(row["key"])
    return None


def web_create_collection(user_id, api_key, name, transport=None) -> str:
    """Create a top-level collection through the Web API and return its key."""
    transport = _resolve(transport)
    url = f"{WEB_API}/users/{user_id}/collections"
    status, text = _web_send(transport, "POST", url, api_key, [{"name": name}])
    if not 200 <= status < 300:
        _web_raise(status, "creating the collection")
    data = _decode_json(status, text, "creating the collection")
    key = None
    if isinstance(data, dict):
        success = data.get("success")
        if isinstance(success, dict) and isinstance(success.get("0"), str):
            key = success["0"]
        if not key and isinstance(data.get("successful"), dict):
            entry = data["successful"].get("0")
            if isinstance(entry, dict):
                key = entry.get("key") or (entry.get("data") or {}).get("key")
    if not key:
        reason = data.get("failed") if isinstance(data, dict) else None
        raise ZoteroError(f"Zotero did not create the collection: {reason or 'no key returned'}")
    return _check_collection_key(key)


def web_existing_keys(user_id, api_key, collection_key, transport=None) -> set[str]:
    """Lowercase DOIs and titles of top-level items already in a collection, via the Web API."""
    transport = _resolve(transport)
    _check_collection_key(collection_key)
    keys: set[str] = set()
    start = 0
    while True:
        url = (
            f"{WEB_API}/users/{user_id}/collections/{collection_key}/items"
            f"?format=json&limit={_PAGE_SIZE}&start={start}"
        )
        status, text = _web_send(transport, "GET", url, api_key)
        if not 200 <= status < 300:
            _web_raise(status, "listing collection items")
        page = _decode_json(status, text, "listing collection items")
        if not isinstance(page, list):
            raise ZoteroError("unexpected response while listing collection items")
        for obj in page:
            data = obj.get("data") if isinstance(obj, dict) else None
            if isinstance(data, dict) and data.get("itemType") not in _NON_TOP_LEVEL_TYPES:
                keys |= _keys_for(data.get("DOI"), data.get("title"))
        if len(page) < _PAGE_SIZE:
            break
        start += len(page)
    return keys


def _schema_fix(item: dict[str, Any]) -> dict[str, Any]:
    """Map fields the Web API rejects for an item type onto valid fields.

    publicationTitle is only valid for journalArticle. For conferencePaper it
    becomes proceedingsTitle. Other types keep the venue and DOI in extra.
    """
    out = dict(item)
    item_type = out.get("itemType")
    if "publicationTitle" in out and item_type != "journalArticle":
        venue = out.pop("publicationTitle")
        if item_type == "conferencePaper":
            out["proceedingsTitle"] = venue
        else:
            out["extra"] = "\n".join(p for p in (out.get("extra", ""), "Venue: " + venue) if p)
    if item_type == "document" and "DOI" in out:
        doi = out.pop("DOI")
        out["extra"] = "\n".join(p for p in (out.get("extra", ""), "DOI: " + doi) if p)
    return out


def web_write_items(user_id, api_key, collection_key, items, transport=None) -> tuple[int, list]:
    """Write items into a collection through the Web API, in batches of 50.

    Returns (number accepted, list of rejection messages).
    """
    transport = _resolve(transport)
    _check_collection_key(collection_key)
    url = f"{WEB_API}/users/{user_id}/items"
    accepted = 0
    failures: list = []
    for start in range(0, len(items), _WEB_BATCH_SIZE):
        batch = [
            dict(_schema_fix(item), collections=[collection_key])
            for item in items[start:start + _WEB_BATCH_SIZE]
        ]
        status, text = _web_send(transport, "POST", url, api_key, batch)
        if not 200 <= status < 300:
            _web_raise(status, "writing items")
        data = _decode_json(status, text, "writing items")
        if not isinstance(data, dict):
            raise ZoteroError("unexpected response while writing items")
        ok = data.get("successful") or data.get("success") or {}
        accepted += len(ok) if isinstance(ok, dict) else 0
        bad = data.get("failed") or {}
        if isinstance(bad, dict):
            failures.extend(bad.values())
    return accepted, failures


def file_bib_files_web(
    paths,
    collection_name: str,
    user_id: str,
    api_key: str,
    transport: Transport | None = None,
    dry_run: bool = False,
    log: Callable[[str], None] = print,
) -> dict[str, Any]:
    """File .bib entries through the Web API, creating the collection when it is missing.

    Steps: parse the input, look up the collection by exact name, drop items
    already filed, then either stop (dry run), create the collection and write
    the new items, and verify the count through the Web API.
    """
    transport = _resolve(transport)
    skipped = 0
    try:
        try:
            entries = _load_bib_entries(paths)
        except (OSError, ValueError) as exc:
            log(f"Could not read the BibTeX input: {exc}")
            return _result("input_error", collection_name)

        key = web_find_collection_key(user_id, api_key, collection_name, transport)
        existing: set[str] = set()
        if key is not None:
            existing = web_existing_keys(user_id, api_key, key, transport)

        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        for entry in entries:
            item = bib_to_connector_item(entry)
            keys = _item_keys(item)
            if not keys:
                log(f"Skipping '{entry.get('citekey') or '?'}': it has no title or DOI.")
                continue
            if keys & existing or keys & seen:
                skipped += 1
                continue
            seen |= keys
            items.append(item)
        log(f"{len(items)} new item(s) for '{collection_name}'; {skipped} already present.")

        if dry_run:
            for item in items:
                log(f"  would file: {item['title'] or item.get('DOI', '')}")
            return _result("dry_run", collection_name, sent=len(items), skipped=skipped)
        if not items:
            log("Nothing new to file.")
            return _result("ok", collection_name, skipped=skipped)

        if key is None:
            key = web_create_collection(user_id, api_key, collection_name, transport)
            log(f"Created the collection '{collection_name}' through the Web API.")

        accepted, failures = web_write_items(user_id, api_key, key, items, transport)
        if failures:
            log(f"Zotero rejected {len(failures)} item(s): {failures[:3]}")

        after = web_existing_keys(user_id, api_key, key, transport)
        added = sum(1 for item in items if _item_keys(item) & (after - existing))
        status = "ok" if added == len(items) and not failures else "verify_mismatch"
        log(f"Filed {added} of {len(items)} item(s) into '{collection_name}'.")
        return _result(status, collection_name, sent=len(items), added=added, skipped=skipped)
    except _ERRORS as exc:
        log(f"Zotero error: {exc}")
        return _result("zotero_error", collection_name, skipped=skipped)


def _stderr(message: str) -> None:
    """Log to stderr so stdout carries only the JSON result."""
    print(message, file=sys.stderr)


def _build_parser() -> argparse.ArgumentParser:
    """Build the argparse CLI: 'file' files .bib entries, 'collections' lists them."""
    parser = argparse.ArgumentParser(
        prog="python -m meridian.zotero_filer",
        description="File BibTeX entries into one named Zotero collection (local API only).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    file_parser = subparsers.add_parser("file", help="file .bib entries into one collection")
    file_parser.add_argument("--collection", required=True, help="exact name of the target collection")
    file_parser.add_argument("--bib", nargs="+", required=True, help="one or more .bib file paths")
    file_parser.add_argument("--dry-run", action="store_true", help="report what would be filed, write nothing")
    file_parser.add_argument(
        "--backend",
        choices=("auto", "web", "desktop"),
        default="auto",
        help=(
            "auto (default): desktop when Zotero answers, starting Zotero if needed; "
            "web only when desktop is unreachable and ZOTERO_USER_ID and ZOTERO_API_KEY are set; "
            "desktop: local connector only; web: Zotero Web API only"
        ),
    )

    subparsers.add_parser("collections", help="list Zotero collections (read-only)")
    return parser


def _desktop_ready() -> bool:
    """True when Zotero's local API answers, starting Zotero first when it does not.

    Never reads a credential. A launch that fails with OSError counts as not ready.
    """
    if zotero_launcher.zotero_api_alive():
        return True
    try:
        started = zotero_launcher.start_zotero()
    except OSError as exc:
        _stderr(f"Could not start Zotero desktop: {exc}")
        return False
    return bool(started.get("api_ready"))


def _pick_auto_backend() -> str | None:
    """Choose the backend for --backend auto: desktop first, web only as a fallback.

    Returns "desktop" when Zotero answers, or comes up after a start. Returns
    "web" when the desktop app is unreachable and usable credentials are set.
    Returns None when neither works. Credentials are read only on the fallback.
    """
    if _desktop_ready():
        return "desktop"
    if web_credentials() is not None:
        return "web"
    return None


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Prints the result as JSON. Exit 0 for ok or dry_run, else 2.

    With --backend auto, a Zotero that cannot be reached and has no usable Web
    API fallback exits 2 with status zotero_unavailable and writes nothing.
    """
    args = _build_parser().parse_args(argv)
    if args.command == "collections":
        try:
            print(json.dumps(list_collections(), indent=2))
        except _ERRORS as exc:
            print(json.dumps({"status": "zotero_error", "message": str(exc)}, indent=2))
            return 2
        return 0
    backend = args.backend
    if backend == "auto":
        backend = _pick_auto_backend()
        if backend is None:
            _stderr(
                "Zotero desktop is not running and could not be started. "
                "Start Zotero desktop, then run this again. Nothing was filed."
            )
            print(json.dumps(_result("zotero_unavailable", args.collection), indent=2))
            return 2
    if backend == "web":
        creds = web_credentials()
        if creds is None:
            print(json.dumps({
                "status": "no_credentials",
                "message": "Set ZOTERO_USER_ID (numeric) and ZOTERO_API_KEY (write access) in the environment.",
            }, indent=2))
            return 2
        result = file_bib_files_web(
            args.bib, args.collection, creds[0], creds[1], dry_run=args.dry_run, log=_stderr
        )
    else:
        result = file_bib_files(args.bib, args.collection, dry_run=args.dry_run, log=_stderr)
    print(json.dumps(result, indent=2))
    return 0 if result["status"] in ("ok", "dry_run") else 2


if __name__ == "__main__":
    raise SystemExit(main())
