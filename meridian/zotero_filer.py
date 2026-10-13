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
* PDFs are attached only on the desktop backend, only when --pdf-dir is given,
  and only to items this run filed and verified. Zotero must approve the app
  once in its own dialog. The approval key stays in memory and travels only in
  request headers.
* --attach-existing attaches PDFs only to items already in the collection. It
  files nothing, needs --pdf-dir, and uses the same single approval.
* --attach-existing with --backend web attaches through the Zotero Web API with
  the key from the environment. It never calls the local API or the approval step.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
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
_ATTACH_APP_NAME = "Meridian Zotero filer"
_AUTHORIZE_URL = "http://127.0.0.1:23119/api/local/authorize"
_AUTHORIZE_TIMEOUT_S = 120.0  # a person must answer Zotero's approval dialog, so this waits longer
_ATTACH_BATCH_SIZE = 25
_ITEM_KEY_RE = re.compile(r"[A-Za-z0-9]{8}")
_PDF_MAGIC = b"%PDF-"
_PDF_CONTENT_TYPE = "application/pdf"
_MAX_PAGES = 500

# A transport performs one HTTP request and returns (status, body_text).
# Signature: transport(method, url, body_bytes_or_None, headers_dict).
Transport = Callable[[str, str, bytes | None, dict[str, str]], tuple[int, str]]

# A header-aware transport also returns the response headers as a dict.
# Signature: transport(method, url, body, headers, timeout=...) -> (status, text, headers).
HeaderTransport = Callable[..., tuple[int, str, dict[str, str]]]

_ENTRY_START_RE = re.compile(r"@([A-Za-z]+)\s*([{(])")
_FIELD_START_RE = re.compile(r"[\s,]*([A-Za-z][A-Za-z0-9_.:\-]*)\s*=\s*")
_BARE_VALUE_RE = re.compile(r"[^\s,#{}\"]+")
_SPACE_RE = re.compile(r"\s*")
_AND_RE = re.compile(r"\s+and\s+")
_SKIPPED_ENTRY_TYPES = frozenset({"comment", "string", "preamble"})


class ZoteroError(RuntimeError):
    """Zotero was unreachable, answered with an error, or sent unusable data."""


_ERRORS = (ZoteroError, OSError, http.client.HTTPException)


class _StripKeyOnRedirect(urllib.request.HTTPRedirectHandler):
    """Follows redirects without sending the Zotero API key to the redirect target.

    Zotero answers a file download with a redirect to the storage host. urllib
    copies request headers onto the redirected request, so without this the key
    would reach that host.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None:
            for name in [name for name in redirected.headers if name.lower() == "zotero-api-key"]:
                del redirected.headers[name]
        return redirected


# Loopback only: never route the Zotero connector through a system proxy. Redirects
# are followed, but the Zotero API key is never sent to a redirect target.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _StripKeyOnRedirect())


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


def _urllib_transport_with_headers(method, url, body, headers, timeout=_TIMEOUT_S):
    """Like _urllib_transport, but also returns the response headers as a dict.

    HTTP error statuses are returned, not raised. Connection errors propagate as OSError.
    """
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace"), dict(response.headers.items())
    except urllib.error.HTTPError as exc:
        response_headers = dict(exc.headers.items()) if exc.headers is not None else {}
        return exc.code, exc.read().decode("utf-8", "replace"), response_headers


def _resolve(transport):
    """Return the injected transport, or the real one when none was given."""
    return _urllib_transport if transport is None else transport


def _resolve_header(transport):
    """Return the injected header-aware transport, or the real one when none was given."""
    return _urllib_transport_with_headers if transport is None else transport


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
    pdf_dir: str | os.PathLike | None = None,
    header_transport: HeaderTransport | None = None,
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
    h. When pdf_dir is set and every sent item verified, attach <citekey>.pdf
       from pdf_dir to each item (see attach_pdfs). A dry run only plans this.

    Returns {"status", "collection", "sent", "added", "skipped_duplicates"}.
    status is one of: ok, dry_run, no_collection, input_error, zotero_error,
    selection_failed, selection_changed, save_failed, verify_mismatch.
    On a dry run, "sent" is the number of items that would be filed.
    When pdf_dir is set and the attach step or the dry-run plan ran, the result
    also has an "attachments" dict. With pdf_dir unset the result has no such key.
    """
    transport = _resolve(transport)
    if isinstance(paths, (str, os.PathLike)):
        paths = [paths]
    existing: set[str] = set()
    sent_items: list[dict[str, Any]] = []
    filed: list[tuple[dict[str, str], dict[str, Any]]] = []  # (bib entry, item), aligned with items
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
            filed.append((entry, item))
        log(f"{len(items)} new item(s) for '{collection_name}'; {skipped} already present.")

        # c. Dry run stops here, before anything is written.
        if dry_run:
            for item in items:
                log(f"  would file: {item['title'] or item.get('DOI', '')}")
            dry = _result("dry_run", collection_name, sent=len(items), skipped=skipped)
            if pdf_dir is not None:
                dry["attachments"] = plan_pdf_attachments(filed, pdf_dir)
            return dry
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
        result = _result(status, collection_name, sent=len(sent_items), added=added, skipped=skipped)

        # h. Attach PDFs only after a clean, fully verified run. Items are aligned with filed.
        if pdf_dir is not None and status == "ok" and added > 0:
            result["attachments"] = attach_pdfs(
                filed[:len(sent_items)], key, pdf_dir,
                transport=transport, header_transport=header_transport, log=log,
            )
        return result
    except _ERRORS as exc:
        log(f"Zotero error: {exc}")
        return _result("zotero_error", collection_name, sent=len(sent_items), skipped=skipped)


# ---------------------------------------------------------------------------
# PDF attachments (desktop backend only). A filed entry gets DIR/<citekey>.pdf
# as a linked file under its item. Zotero 10 shows the person an approval
# dialog the first time, through /api/local/authorize. That is one request per
# run and it is never retried. The returned key lives only in memory, travels
# only in request headers, and is never logged, printed or written to a file.
# ---------------------------------------------------------------------------

_AUTH_MESSAGES = {
    "not_approved": "Zotero did not approve the PDF attach request. No PDFs were attached.",
    "rate_limited": "Zotero rate-limited the PDF attach request. Nothing was retried; try again later.",
    "auth_error": "The PDF attach request to Zotero failed before it was answered. No PDFs were attached.",
}


def _pdf_for_entry(entry, pdf_dir):
    """Return DIR/<citekey>.pdf when that file exists and starts with %PDF-, else None.

    A citekey that is not a plain file name (empty, a dot segment, or containing a
    path separator or a colon) never resolves, so it cannot point outside DIR.
    """
    citekey = entry.get("citekey") or ""
    if not citekey or citekey in (".", "..") or ":" in citekey or os.path.basename(citekey) != citekey:
        return None
    path = os.path.join(os.fspath(pdf_dir), citekey + ".pdf")
    try:
        with open(path, "rb") as handle:
            head = handle.read(len(_PDF_MAGIC))
    except OSError:
        return None
    return path if head == _PDF_MAGIC else None


def plan_pdf_attachments(filed, pdf_dir) -> dict[str, Any]:
    """Dry-run plan: which filed entries would get a PDF. Reads local files only."""
    planned: list[str] = []
    no_pdf: list[str] = []
    for entry, _item in filed:
        citekey = entry.get("citekey") or ""
        (planned if _pdf_for_entry(entry, pdf_dir) else no_pdf).append(citekey)
    return {"status": "dry_run", "planned": planned, "no_pdf": no_pdf}


def _get_all_pages(transport, base_url):
    """GET every page of a Local API list endpoint. base_url carries no limit or start."""
    rows: list[Any] = []
    start = 0
    for _ in range(_MAX_PAGES):
        url = f"{base_url}{'&' if '?' in base_url else '?'}limit={_PAGE_SIZE}"
        if start:
            url += f"&start={start}"
        page = _get_json(transport, url)
        if not isinstance(page, list):
            raise ZoteroError("unexpected response while listing items")
        rows.extend(page)
        if len(page) < _PAGE_SIZE:
            return rows
        start += len(page)
    raise ZoteroError("item listing did not end within the page limit")


def _collection_item_records(collection_key, transport):
    """Return (item key, normalised DOI, normalised title) for each top-level item in the collection.

    Attachments, notes and annotations are skipped, as in existing_keys.
    """
    _check_collection_key(collection_key)
    records: list[tuple[str, str, str]] = []
    for obj in _get_all_pages(transport, f"{LOCAL_API}/collections/{collection_key}/items?format=json"):
        data = obj.get("data") if isinstance(obj, dict) else None
        if not isinstance(data, dict) or data.get("itemType") in _NON_TOP_LEVEL_TYPES:
            continue
        ident = str((obj.get("key") or data.get("key") or ""))
        if not _ITEM_KEY_RE.fullmatch(ident):
            continue
        records.append((ident, _norm_doi(data.get("DOI")), _norm_title(data.get("title"))))
    return records


def _collection_item_index(collection_key, transport):
    """Map each dedupe key (DOI or lowercase title) to the item key that carries it."""
    index: dict[str, str] = {}
    for ident, norm_doi, norm_title in _collection_item_records(collection_key, transport):
        for key in _keys_for(norm_doi, norm_title):
            index.setdefault(key, ident)
    return index


def _match_existing_item(norm_doi, norm_title, records):
    """Return the item key an entry matches among records, or None.

    A shared DOI always matches. A shared title matches only when the two DOIs do
    not conflict, that is when at least one side has no DOI. Two different papers
    that share a title are therefore never confused.
    """
    for ident, item_doi, _item_title in records:
        if norm_doi and item_doi == norm_doi:
            return ident
    for ident, item_doi, item_title in records:
        if norm_title and item_title == norm_title and (not norm_doi or not item_doi):
            return ident
    return None


def _has_pdf_child(item_key, transport):
    """True when the item already has a child whose contentType is application/pdf."""
    for obj in _get_all_pages(transport, f"{LOCAL_API}/items/{item_key}/children?format=json"):
        data = obj.get("data") if isinstance(obj, dict) else None
        if isinstance(data, dict) and str(data.get("contentType") or "").lower() == _PDF_CONTENT_TYPE:
            return True
    return False


def _header_value(headers, name):
    """Case-insensitive lookup in a response-header dict. None when absent."""
    wanted = name.lower()
    for key, value in (headers or {}).items():
        if str(key).lower() == wanted:
            return str(value)
    return None


def _probe_server_id(header_transport):
    """One full GET to the local API. Returns its Zotero-Server-ID response header, or None."""
    _status, _text, response_headers = header_transport(
        "GET", f"{LOCAL_API}/collections?limit=1", None, {_ALLOWED_HEADER: "1"}, timeout=_TIMEOUT_S,
    )
    return _header_value(response_headers, "Zotero-Server-ID") or None


def _request_authorization(header_transport, server_id):
    """Make the single authorize request, which opens Zotero's approval dialog.

    Returns (outcome, key, retry_after). outcome is approved, not_approved,
    rate_limited or auth_error. key is set only when approved. A 429 is never retried.
    """
    body = json.dumps({"appName": _ATTACH_APP_NAME}).encode("utf-8")
    headers = {_ALLOWED_HEADER: "1", "Zotero-Server-ID": server_id, "Content-Type": "application/json"}
    try:
        status, text, response_headers = header_transport(
            "POST", _AUTHORIZE_URL, body, headers, timeout=_AUTHORIZE_TIMEOUT_S,
        )
    except _ERRORS:
        return "auth_error", None, None
    if status == 429:
        return "rate_limited", None, _header_value(response_headers, "Retry-After")
    if not 200 <= status < 300:
        return "not_approved", None, None
    try:
        data = json.loads(text)
    except ValueError:
        return "not_approved", None, None
    key = data.get("key") if isinstance(data, dict) else None
    if isinstance(key, str) and key:
        return "approved", key, None
    return "not_approved", None, None


def _attachment_body(parent_key, citekey, pdf_path):
    """One Local API attachment object: a linked file under parent_key, with forward-slash paths."""
    return {
        "itemType": "attachment",
        "linkMode": "linked_file",
        "parentItem": parent_key,
        "path": os.path.abspath(pdf_path).replace("\\", "/"),
        "title": f"{citekey}.pdf",
        "contentType": _PDF_CONTENT_TYPE,
    }


def _batch_outcomes(count, text):
    """Return one bool per batch position: True only when Zotero lists it under success or successful.

    A position listed under failed, a position not listed at all, or a body that
    is not a JSON object all count as not attached.
    """
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if not isinstance(data, dict):
        return [False] * count
    ok: set[str] = set()
    for bucket in ("success", "successful"):
        part = data.get(bucket)
        if isinstance(part, dict):
            ok |= {str(position) for position in part}
    bad: set[str] = set()
    failed = data.get("failed")
    if isinstance(failed, dict):
        bad = {str(position) for position in failed}
    return [str(position) in ok and str(position) not in bad for position in range(count)]


def _write_attachments(pending, header_transport, summary, log):
    """Authorize once, then write the linked-file attachments in batches. Updates summary in place."""
    server_id = _probe_server_id(header_transport)
    if server_id is None:
        summary["status"] = "no_server_id"
        log("Zotero did not report a server id, so it cannot approve the PDF attach. No PDFs were attached.")
        return
    outcome, key, retry_after = _request_authorization(header_transport, server_id)
    if outcome != "approved":
        summary["status"] = outcome
        if retry_after is not None:
            summary["retry_after"] = retry_after
        log(_AUTH_MESSAGES[outcome])
        return
    for start in range(0, len(pending), _ATTACH_BATCH_SIZE):
        batch = pending[start:start + _ATTACH_BATCH_SIZE]
        body = json.dumps(
            [_attachment_body(parent, citekey, path) for citekey, path, parent in batch]
        ).encode("utf-8")
        headers = {
            _ALLOWED_HEADER: "1",
            "Zotero-Server-ID": server_id,
            "Zotero-API-Key": key,
            "Content-Type": "application/json",
        }
        status, text, response_headers = header_transport(
            "POST", f"{LOCAL_API}/items", body, headers, timeout=_TIMEOUT_S,
        )
        if status == 429:
            summary["status"] = "rate_limited"
            summary["retry_after"] = _header_value(response_headers, "Retry-After")
            log("Zotero rate-limited the PDF attach write. Stopped; nothing was retried.")
            return
        if not 200 <= status < 300:
            summary["status"] = "attach_failed"
            log(f"Zotero refused a PDF attach batch with HTTP {status}. Stopped.")
            return
        for (citekey, _path, _parent), attached in zip(batch, _batch_outcomes(len(batch), text)):
            if attached:
                summary["attached"].append(citekey)
                log(f"Attached PDF for '{citekey}'.")
            else:
                summary["failed"].append(citekey)
                log(f"Zotero did not attach the PDF for '{citekey}'.")


def attach_pdfs(filed, collection_key, pdf_dir, transport=None, header_transport=None, log=print) -> dict[str, Any]:
    """Attach each filed entry's local PDF to its item and return a per-item summary.

    filed is a list of (bib entry, connector item) pairs for items this run filed
    and verified. For each entry the step finds its PDF, finds the item key,
    skips items that already have a PDF child, makes one authorize request if
    anything is left, and writes the attachments in batches.

    Returns {"status", "attached", "already_has_pdf", "no_pdf", "failed"} with
    lists of citekeys. status is ok, not_approved, rate_limited, auth_error,
    no_server_id, zotero_error or attach_failed. Any refusal stops the step
    at once. Every filed entry that was not attached is listed under failed.
    """
    transport = _resolve(transport)
    header_transport = _resolve_header(header_transport)
    summary: dict[str, Any] = {
        "status": "ok", "attached": [], "already_has_pdf": [], "no_pdf": [], "failed": [],
    }
    candidates: list[tuple[str, str, dict[str, Any]]] = []
    for entry, item in filed:
        citekey = entry.get("citekey") or ""
        path = _pdf_for_entry(entry, pdf_dir)
        if path is None:
            summary["no_pdf"].append(citekey)
            log(f"Skipped PDF for '{citekey}': no usable {citekey}.pdf in the PDF folder.")
        else:
            candidates.append((citekey, path, item))

    pending: list[tuple[str, str, str]] = []
    try:
        if candidates:
            index = _collection_item_index(collection_key, transport)
            for citekey, path, item in candidates:
                parent = next((index[key] for key in sorted(_item_keys(item)) if key in index), None)
                if parent is None:
                    summary["failed"].append(citekey)
                    log(f"Could not find '{citekey}' in the collection, so its PDF was not attached.")
                elif _has_pdf_child(parent, transport):
                    summary["already_has_pdf"].append(citekey)
                    log(f"Skipped PDF for '{citekey}': the item already has a PDF.")
                else:
                    pending.append((citekey, path, parent))
            if pending:
                _write_attachments(pending, header_transport, summary, log)
    except _ERRORS as exc:
        log(f"Zotero error during the PDF attach: {exc}")
        summary["status"] = "zotero_error"

    # Anything that was not classified above, or was not attached, counts as failed.
    settled = set(summary["attached"]) | set(summary["already_has_pdf"]) | set(summary["no_pdf"]) | set(summary["failed"])
    summary["failed"] += [citekey for citekey, _path, _item in candidates if citekey not in settled]
    if summary["status"] == "ok" and summary["failed"]:
        summary["status"] = "attach_failed"
    return summary


def attach_existing_files(
    paths,
    collection_name: str,
    pdf_dir: str | os.PathLike,
    transport: Transport | None = None,
    header_transport: HeaderTransport | None = None,
    dry_run: bool = False,
    log: Callable[[str], Any] = print,
) -> dict[str, Any]:
    """Attach PDFs to entries that are already in the target collection (--attach-existing).

    Nothing is filed. Each .bib entry is matched to an item already in the
    collection by DOI, or by normalised title when the DOIs do not conflict. An
    entry with no match is not touched.
    A matched entry gets DIR/<citekey>.pdf as a linked file when the file starts
    with %PDF- and the item has no application/pdf child yet.

    A dry run stops after planning, so it never authorizes or writes. A live run
    makes one authorize request when anything is planned, then writes the planned
    attachments through _write_attachments. A refusal is reported and never retried.

    Returns {"status", "collection", "mode", "attachments"}. The attachments dict
    holds lists of citekeys: planned, attached, already_has_pdf, no_pdf, failed,
    unmatched and duplicate_in_bib. status is dry_run, ok, no_collection,
    input_error, zotero_error, not_approved, rate_limited, auth_error,
    no_server_id or attach_failed.
    """
    transport = _resolve(transport)
    header_transport = _resolve_header(header_transport)
    if isinstance(paths, (str, os.PathLike)):
        paths = [paths]
    summary: dict[str, Any] = {
        "status": "dry_run" if dry_run else "ok",
        "planned": [], "attached": [], "already_has_pdf": [], "no_pdf": [],
        "failed": [], "unmatched": [], "duplicate_in_bib": [],
    }
    candidates: list[tuple[str, str, str]] = []  # (citekey, pdf path, parent item key)

    def finish(status: str) -> dict[str, Any]:
        summary["status"] = status
        return {"status": status, "collection": collection_name, "mode": "attach_existing", "attachments": summary}

    try:
        key = find_collection_key(collection_name, transport)
        if key is None:
            log(
                f"Create the collection '{collection_name}' in Zotero first "
                "(right-click My Library > New Collection)."
            )
            return finish("no_collection")
        try:
            entries = _load_bib_entries(paths)
        except (OSError, ValueError) as exc:
            log(f"Could not read the BibTeX input: {exc}")
            return finish("input_error")

        records = _collection_item_records(key, transport)
        claimed: set[str] = set()  # one PDF per existing item per run
        for entry in entries:
            citekey = entry.get("citekey") or ""
            item = bib_to_connector_item(entry)
            parent = _match_existing_item(_norm_doi(item.get("DOI")), _norm_title(item.get("title")), records)
            if parent is None:
                summary["unmatched"].append(citekey)
                continue
            if parent in claimed:
                summary["duplicate_in_bib"].append(citekey)
                continue
            path = _pdf_for_entry(entry, pdf_dir)
            if path is None:
                summary["no_pdf"].append(citekey)
                log(f"Skipped PDF for '{citekey}': no usable {citekey}.pdf in the PDF folder.")
            elif _has_pdf_child(parent, transport):
                summary["already_has_pdf"].append(citekey)
                log(f"Skipped PDF for '{citekey}': the item already has a PDF.")
            else:
                claimed.add(parent)  # claimed only once the entry is planned, never before its PDF check
                candidates.append((citekey, path, parent))
                summary["planned"].append(citekey)
        if summary["unmatched"]:
            log(f"Not in '{collection_name}', so not touched: {', '.join(summary['unmatched'])}")

        if dry_run:
            for citekey in summary["planned"]:
                log(f"Would attach PDF for '{citekey}' to its existing item.")
            return finish("dry_run")
        if candidates:
            _write_attachments(candidates, header_transport, summary, log)
    except _ERRORS as exc:
        log(f"Zotero error during the PDF attach: {exc}")
        summary["status"] = "zotero_error"

    # Anything planned that was not attached counts as failed, as in attach_pdfs.
    attached = set(summary["attached"])
    summary["failed"] += [citekey for citekey, _path, _parent in candidates if citekey not in attached]
    if summary["status"] == "ok" and summary["failed"]:
        summary["status"] = "attach_failed"
    return finish(summary["status"])


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


# ---------------------------------------------------------------------------
# Web API attach path (--attach-existing --backend web). PDFs are uploaded with
# the user's own API key from the environment. This path never calls the local
# API and never calls the desktop approval step. The key is sent only in the
# Zotero-API-Key header, and the storage upload gets no Zotero headers at all.
# ---------------------------------------------------------------------------

_WEB_KEY_ENV_NAMES = ("ZOTERO_SECOND_API_KEY", "ZOTERO_API_KEY")
_WEB_RETRY_STATUSES = frozenset({429, 503})
_WEB_RETRY_CAP_S = 120.0  # a longer Retry-After stops the run rather than blocking it
_WEB_UPLOAD_TIMEOUT_S = 120.0
_WEB_FORM_TYPE = "application/x-www-form-urlencoded"
_STORED_LINK_MODES = frozenset({"imported_file", "imported_url"})  # link modes whose file Zotero stores


class _WebStop(ZoteroError):
    """Ends a Web attach run early. status says why. The message never includes the key."""

    def __init__(self, status, message, retry_after=None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after
        self.step = "not_run"


def web_attach_credentials(env=None) -> tuple[str, str, str] | None:
    """Return (user_id, api_key, variable name) for the Web attach path, or None.

    The key comes from ZOTERO_SECOND_API_KEY when that is set, otherwise from
    ZOTERO_API_KEY. Only the variable name is returned, never the value.
    """
    source = os.environ if env is None else env
    user_id = (source.get("ZOTERO_USER_ID") or "").strip()
    if not user_id.isdigit():
        return None
    for name in _WEB_KEY_ENV_NAMES:
        value = (source.get(name) or "").strip()
        if value:
            return user_id, value, name
    return None


def _retry_seconds(value):
    """Seconds to wait for a Retry-After value. None when it is missing, not a number, or over the cap."""
    try:
        seconds = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    if not 0 <= seconds <= _WEB_RETRY_CAP_S:
        return None
    return seconds


def _json_or_none(text):
    """Decode a JSON body, or None when it is not JSON."""
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def _ok(status):
    """True for a 2xx status."""
    return 200 <= status < 300


def _created_key(data):
    """The key of the object a Web API write created at position "0", or None.

    Accepts both response shapes. success maps the position to the key. successful
    maps the position to the object.
    """
    if not isinstance(data, dict):
        return None
    key = None
    success = data.get("success")
    if isinstance(success, dict):
        key = success.get("0")
    if key is None and isinstance(data.get("successful"), dict):
        entry = data["successful"].get("0")
        if isinstance(entry, dict):
            inner = entry.get("data") if isinstance(entry.get("data"), dict) else {}
            key = entry.get("key") or inner.get("key")
    return key if isinstance(key, str) and _ITEM_KEY_RE.fullmatch(key) else None


class _WebRun:
    """Web API calls for one attach run.

    The key is held in memory and sent only in the Zotero-API-Key header. Calls to
    Zotero also send Zotero-API-Version: 3. The storage upload goes to a storage
    host, so it gets no Zotero headers at all.
    """

    def __init__(self, user_id, api_key, transport, sleep):
        self.user_id = user_id
        self._api_key = api_key
        self._transport = _resolve_header(transport)
        self._sleep = sleep

    def call(self, method, url, body=None, headers=None, zotero=True, timeout=_TIMEOUT_S):
        """One request. A 429 or 503 is retried once, after its Retry-After wait. Returns (status, text, headers)."""
        sent = dict(headers or {})
        if zotero:
            sent["Zotero-API-Version"] = "3"
            sent["Zotero-API-Key"] = self._api_key
        status, text, response_headers = self._transport(method, url, body, sent, timeout=timeout)
        if status not in _WEB_RETRY_STATUSES:
            return status, text, response_headers
        retry_after = _header_value(response_headers, "Retry-After")
        wait = _retry_seconds(retry_after)
        if wait is None:
            raise _WebStop(
                "rate_limited",
                f"Zotero answered HTTP {status} and asked for a wait this run will not make. Stopped.",
                retry_after,
            )
        self._sleep(wait)
        status, text, response_headers = self._transport(method, url, body, sent, timeout=timeout)
        if status in _WEB_RETRY_STATUSES:
            retry_after = _header_value(response_headers, "Retry-After")
            raise _WebStop(
                "rate_limited",
                f"Zotero answered HTTP {status} again after one retry. Stopped.",
                retry_after,
            )
        return status, text, response_headers

    def get_json(self, url, what):
        """GET a Web API URL and return its decoded JSON body."""
        status, text, _headers = self.call("GET", url)
        if not _ok(status):
            _web_raise(status, what)
        return _decode_json(status, text, what)

    def objects(self, base_url, what):
        """Yield every object from every page of a list endpoint. base_url carries no query string."""
        start = 0
        for _ in range(_MAX_PAGES):
            page = self.get_json(f"{base_url}?format=json&limit={_PAGE_SIZE}&start={start}", what)
            if not isinstance(page, list):
                raise ZoteroError(f"unexpected response while {what}")
            yield from page
            if len(page) < _PAGE_SIZE:
                return
            start += len(page)
        raise ZoteroError(f"{what} did not end within the page limit")

    def verify_key(self):
        """Check that the key belongs to ZOTERO_USER_ID and may write to the library and its files."""
        status, text, _headers = self.call("GET", f"{WEB_API}/keys/current")
        if not _ok(status):
            raise _WebStop("key_refused", f"Zotero refused the API key (HTTP {status}). Nothing was attached.")
        data = _json_or_none(text)
        if not isinstance(data, dict) or str(data.get("userID")) != self.user_id:
            raise _WebStop(
                "key_user_mismatch",
                "The API key belongs to a different Zotero user than ZOTERO_USER_ID. Nothing was attached.",
            )
        access = data.get("access") if isinstance(data.get("access"), dict) else {}
        user = access.get("user") if isinstance(access.get("user"), dict) else {}
        if user.get("write") is not True or user.get("files") is not True:
            raise _WebStop(
                "key_insufficient",
                "The API key needs library write and file access. Nothing was attached.",
            )


def _web_collection_key(run, name):
    """Key of the one collection named exactly name, or None. Two collections with that name stop the run."""
    keys = []
    for obj in run.objects(f"{WEB_API}/users/{run.user_id}/collections", "listing collections"):
        data = obj.get("data") if isinstance(obj, dict) else None
        if isinstance(data, dict) and data.get("name") == name and isinstance(data.get("key"), str):
            keys.append(_check_collection_key(data["key"]))
    if len(keys) > 1:
        raise _WebStop(
            "ambiguous_collection",
            f"More than one Zotero collection is named '{name}'. Nothing was attached.",
        )
    return keys[0] if keys else None


def _web_top_item_records(run, collection_key):
    """(item key, normalised DOI, normalised title) for each top-level item in the collection."""
    base = f"{WEB_API}/users/{run.user_id}/collections/{_check_collection_key(collection_key)}/items/top"
    records = []
    for obj in run.objects(base, "listing collection items"):
        data = obj.get("data") if isinstance(obj, dict) else None
        if not isinstance(data, dict) or data.get("itemType") in _NON_TOP_LEVEL_TYPES:
            continue
        ident = str(obj.get("key") or data.get("key") or "")
        if not _ITEM_KEY_RE.fullmatch(ident):
            continue
        records.append((ident, _norm_doi(data.get("DOI")), _norm_title(data.get("title"))))
    return records


def _web_child_file_stored(run, child_key):
    """True when Zotero stores the file of this attachment. HTTP 404 means it is not stored.

    The download redirects to the storage host, and the key is not sent there. Any
    other status stops the run, so an unclear answer never leads to a second child.
    """
    status, _text, _headers = run.call(
        "GET", f"{WEB_API}/users/{run.user_id}/items/{child_key}/file", timeout=_WEB_UPLOAD_TIMEOUT_S,
    )
    if status == 404:
        return False
    if not _ok(status):
        _web_raise(status, "checking a PDF attachment's file")
    return True


def _web_pdf_child_state(run, item_key):
    """(attached, orphan_key) for the PDF children of an item.

    attached is True when a PDF child has its file stored on Zotero. A PDF child
    with no stored file is an orphan: an earlier run created the empty attachment
    and its upload then failed. orphan_key is the key of the first orphan, or None.
    A linked file or linked URL has no file on Zotero, so it is never checked and
    counts as attached. A PDF child whose key cannot be checked also counts as
    attached, so nothing is uploaded beside it.
    """
    url = f"{WEB_API}/users/{run.user_id}/items/{item_key}/children"
    orphan = None
    for obj in run.objects(url, "listing item children"):
        data = obj.get("data") if isinstance(obj, dict) else None
        if not isinstance(data, dict) or str(data.get("contentType") or "").lower() != _PDF_CONTENT_TYPE:
            continue
        link_mode = data.get("linkMode")
        child_key = str(obj.get("key") or data.get("key") or "")
        if (link_mode and link_mode not in _STORED_LINK_MODES) or not _ITEM_KEY_RE.fullmatch(child_key):
            return True, None
        if _web_child_file_stored(run, child_key):
            return True, None
        if orphan is None:
            orphan = child_key
    return False, orphan


def _web_attach_entry(run, citekey, path, parent_key, orphan_key=None):
    """Steps 5a to 5d for one PDF. Returns (attached, failed_step, stop_run).

    orphan_key is an empty PDF child that an earlier run left behind. When it is
    given, the upload goes into that child and no new attachment item is created.
    failed_step names the step that failed. stop_run is True after a connection
    error, so the caller stops the run. A rate limit raises _WebStop, with its
    step set to the step that was running.
    """
    try:
        mtime_ms = int(os.stat(path).st_mtime * 1000)
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError:
        return False, "read_pdf", False
    filename = f"{citekey}.pdf"
    md5 = hashlib.md5(data).hexdigest()
    base = f"{WEB_API}/users/{run.user_id}"
    step = "create_item"
    try:
        if orphan_key is None:
            item = {
                "itemType": "attachment", "parentItem": parent_key, "linkMode": "imported_file",
                "title": filename, "filename": filename, "contentType": _PDF_CONTENT_TYPE,
                "md5": md5, "mtime": mtime_ms, "tags": [], "relations": {},
            }
            status, text, _headers = run.call(
                "POST", f"{base}/items", json.dumps([item]).encode("utf-8"), {"Content-Type": "application/json"},
            )
            att_key = _created_key(_json_or_none(text)) if _ok(status) else None
            if att_key is None:
                return False, step, False
        else:
            att_key = orphan_key

        step = "authorize"
        file_url = f"{base}/items/{att_key}/file"
        form = urllib.parse.urlencode({"md5": md5, "filename": filename, "filesize": len(data), "mtime": mtime_ms})
        status, text, _headers = run.call(
            "POST", file_url, form.encode("ascii"), {"If-None-Match": "*", "Content-Type": _WEB_FORM_TYPE},
        )
        auth = _json_or_none(text) if _ok(status) else None
        if not isinstance(auth, dict):
            return False, step, False
        if auth.get("exists") == 1:
            return True, None, False  # Zotero already stores these bytes, so there is nothing to upload
        upload_url = auth.get("url")
        prefix, suffix = auth.get("prefix"), auth.get("suffix")
        upload_key, content_type = auth.get("uploadKey"), auth.get("contentType")
        if not (
            isinstance(upload_url, str) and upload_url.startswith("https://")
            and isinstance(prefix, str) and isinstance(suffix, str)
            and isinstance(upload_key, str) and upload_key
            and isinstance(content_type, str) and content_type
        ):
            return False, step, False

        step = "upload"
        payload = prefix.encode("utf-8") + data + suffix.encode("utf-8")
        status, _text, _headers = run.call(
            "POST", upload_url, payload, {"Content-Type": content_type},
            zotero=False, timeout=_WEB_UPLOAD_TIMEOUT_S,
        )
        if not _ok(status):
            return False, step, False

        step = "register"
        status, _text, _headers = run.call(
            "POST", file_url, urllib.parse.urlencode({"upload": upload_key}).encode("ascii"),
            {"If-None-Match": "*", "Content-Type": _WEB_FORM_TYPE},
        )
        if status != 204:
            return False, step, False
        return True, None, False
    except _WebStop as exc:
        exc.step = step
        raise
    except (OSError, http.client.HTTPException):
        return False, step, True


def attach_existing_web(
    paths,
    collection_name: str,
    pdf_dir: str | os.PathLike,
    user_id: str,
    api_key: str,
    transport: HeaderTransport | None = None,
    dry_run: bool = False,
    log: Callable[[str], Any] = print,
    sleep: Callable[[float], Any] = time.sleep,
) -> dict[str, Any]:
    """Attach PDFs through the Zotero Web API to entries already in the collection.

    Matching, the skip rules and the dry-run plan match attach_existing_files. The
    local API and the desktop approval step are never used. The steps are: check
    the key, find the collection by exact name, list its top-level items, match each
    entry, skip entries whose PDF child already has its file stored, then for each
    planned PDF create the attachment item, authorize the upload, upload the bytes,
    and register. An empty PDF child left by an earlier run is reused, so the upload
    goes into it and no second child is created. A dry run stops after planning.

    Returns {"status", "collection", "mode", "attachments"}. The attachments dict
    holds citekey lists: planned, attached, already_has_pdf, no_pdf, failed, unmatched
    and duplicate_in_bib. failed_steps maps each failed citekey to the step that
    failed. status is dry_run, ok, no_collection, ambiguous_collection, input_error,
    key_refused, key_user_mismatch, key_insufficient, rate_limited, zotero_error or
    attach_failed.
    """
    if isinstance(paths, (str, os.PathLike)):
        paths = [paths]
    summary: dict[str, Any] = {
        "status": "dry_run" if dry_run else "ok",
        "planned": [], "attached": [], "already_has_pdf": [], "no_pdf": [],
        "failed": [], "failed_steps": {}, "unmatched": [], "duplicate_in_bib": [],
    }
    candidates: list[tuple[str, str, str, str | None]] = []  # (citekey, pdf path, parent item key, orphan child key)
    run = _WebRun(user_id, api_key, transport, sleep)

    def finish(status: str) -> dict[str, Any]:
        summary["status"] = status
        return {"status": status, "collection": collection_name, "mode": "attach_existing_web", "attachments": summary}

    try:
        try:
            entries = _load_bib_entries(paths)
        except (OSError, ValueError) as exc:
            log(f"Could not read the BibTeX input: {exc}")
            return finish("input_error")
        run.verify_key()
        key = _web_collection_key(run, collection_name)
        if key is None:
            log(f"Create the collection '{collection_name}' in Zotero first.")
            return finish("no_collection")
        records = _web_top_item_records(run, key)
        claimed: set[str] = set()  # one PDF per existing item per run
        for entry in entries:
            citekey = entry.get("citekey") or ""
            item = bib_to_connector_item(entry)
            parent = _match_existing_item(_norm_doi(item.get("DOI")), _norm_title(item.get("title")), records)
            if parent is None:
                summary["unmatched"].append(citekey)
                continue
            if parent in claimed:
                summary["duplicate_in_bib"].append(citekey)
                continue
            path = _pdf_for_entry(entry, pdf_dir)
            if path is None:
                summary["no_pdf"].append(citekey)
                log(f"Skipped PDF for '{citekey}': no usable {citekey}.pdf in the PDF folder.")
                continue
            has_pdf, orphan = _web_pdf_child_state(run, parent)
            if has_pdf:
                summary["already_has_pdf"].append(citekey)
                log(f"Skipped PDF for '{citekey}': the item already has a PDF.")
                continue
            claimed.add(parent)  # claimed only once the entry is planned
            candidates.append((citekey, path, parent, orphan))
            summary["planned"].append(citekey)
        if summary["unmatched"]:
            log(f"Not in '{collection_name}', so not touched: {', '.join(summary['unmatched'])}")

        if dry_run:
            for citekey in summary["planned"]:
                log(f"Would attach PDF for '{citekey}' to its existing item.")
            return finish("dry_run")
        for citekey, path, parent, orphan in candidates:
            if orphan is not None:
                log(f"Reusing the empty PDF attachment left by an earlier run for '{citekey}'.")
            try:
                attached, failed_step, stop_run = _web_attach_entry(run, citekey, path, parent, orphan)
            except _WebStop as exc:
                summary["failed"].append(citekey)
                summary["failed_steps"][citekey] = exc.step
                raise
            if attached:
                summary["attached"].append(citekey)
                log(f"Attached PDF for '{citekey}'.")
            else:
                summary["failed"].append(citekey)
                summary["failed_steps"][citekey] = failed_step
                log(f"Could not attach the PDF for '{citekey}': the {failed_step} step failed.")
            if stop_run:
                summary["status"] = "zotero_error"
                log("A connection error ended the run. The remaining PDFs were not attempted.")
                break
    except _WebStop as exc:
        log(str(exc))
        summary["status"] = exc.status
        if exc.retry_after is not None:
            summary["retry_after"] = exc.retry_after
    except _ERRORS as exc:
        log(f"Zotero error during the PDF attach: {exc}")
        summary["status"] = "zotero_error"

    # Any planned entry that was not attached counts as failed. Entries the run never reached are not_run.
    attached_set = set(summary["attached"])
    for citekey, _path, _parent, _orphan in candidates:
        if citekey not in attached_set and citekey not in summary["failed"]:
            summary["failed"].append(citekey)
            summary["failed_steps"][citekey] = "not_run"
    if summary["status"] == "ok" and summary["failed"]:
        summary["status"] = "attach_failed"
    return finish(summary["status"])


def _run_attach_existing_web(args) -> int:
    """CLI: --attach-existing with --backend web. Prints the JSON result. Exit 0 for ok or dry_run, else 2."""
    creds = web_attach_credentials()
    if creds is None:
        print(json.dumps({
            "status": "no_credentials",
            "message": (
                "Set ZOTERO_USER_ID (numeric) and an API key with library write and file access "
                "(ZOTERO_SECOND_API_KEY, or ZOTERO_API_KEY) in the environment. Nothing was attached."
            ),
        }, indent=2))
        return 2
    user_id, api_key, key_name = creds
    _stderr(f"Using the API key from {key_name}.")
    result = attach_existing_web(
        args.bib, args.collection, args.pdf_dir, user_id, api_key, dry_run=args.dry_run, log=_stderr,
    )
    print(json.dumps(result, indent=2))
    return 0 if result["status"] in ("ok", "dry_run") else 2


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
            "a real run never falls back to web; a dry run uses web only when desktop is unreachable "
            "and ZOTERO_USER_ID and ZOTERO_API_KEY are set; "
            "desktop: local connector only; web: Zotero Web API only"
        ),
    )
    file_parser.add_argument(
        "--pdf-dir",
        default=None,
        help=(
            "folder holding <citekey>.pdf files to attach to newly filed items (desktop backend only). "
            "The first real attach asks Zotero to approve Meridian once, in a dialog you must answer."
        ),
    )
    file_parser.add_argument(
        "--attach-existing",
        action="store_true",
        help=(
            "attach DIR/<citekey>.pdf to items ALREADY in the collection (matched by DOI or title) "
            "instead of filing new items. Needs --pdf-dir. With --backend web it uses the Zotero "
            "Web API and the key from the environment. Otherwise it needs the desktop backend, and "
            "Zotero asks for approval once per run, only when something is left to attach."
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


def _pick_auto_backend(dry_run: bool) -> str | None:
    """Choose the backend for --backend auto. Returns "desktop", "web" or None.

    A dry run never starts Zotero. It uses desktop when the API already answers,
    and web when it does not and usable credentials are set. Credentials are read
    only on that dry-run fallback.

    A real run uses desktop when the API answers or comes up after a start. It
    never falls back to web, so a failed start returns None and nothing is written
    through a backend the user did not choose.
    """
    if dry_run:
        if zotero_launcher.zotero_api_alive():
            return "desktop"
        return "web" if web_credentials() is not None else None
    return "desktop" if _desktop_ready() else None


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Prints the result as JSON. Exit 0 for ok or dry_run, else 2.

    With --backend auto, a Zotero that cannot be reached exits 2 with status
    zotero_unavailable and writes nothing. A real run never falls back to the Web
    API. A dry run uses the Web API only when Zotero is down and credentials are set,
    and it never starts Zotero.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "collections":
        try:
            print(json.dumps(list_collections(), indent=2))
        except _ERRORS as exc:
            print(json.dumps({"status": "zotero_error", "message": str(exc)}, indent=2))
            return 2
        return 0
    if args.attach_existing and args.pdf_dir is None:
        parser.error("--attach-existing requires --pdf-dir")
    if args.attach_existing and args.backend == "web":
        return _run_attach_existing_web(args)
    backend = args.backend
    if backend == "auto":
        backend = _pick_auto_backend(args.dry_run)
        if backend is None:
            if args.dry_run:
                _stderr("Zotero desktop is not running. Start Zotero desktop, then run this again. Nothing was filed.")
            else:
                _stderr(
                    "Zotero desktop did not come up within the wait. Start Zotero and retry, "
                    "or pass --backend web to file through the Web API. Nothing was filed."
                )
            print(json.dumps(_result("zotero_unavailable", args.collection), indent=2))
            return 2
    if args.attach_existing:
        if backend != "desktop":
            print(json.dumps({
                "status": "attach_existing_needs_desktop",
                "collection": args.collection,
                "message": (
                    "--attach-existing attaches PDFs through the Zotero desktop app. "
                    "Start Zotero desktop and retry. Nothing was attached."
                ),
            }, indent=2))
            return 2
        result = attach_existing_files(args.bib, args.collection, args.pdf_dir, dry_run=args.dry_run, log=_stderr)
    elif backend == "web":
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
        if args.pdf_dir is not None:
            result["attachments"] = {"status": "not_run", "reason": "PDF attach needs the desktop backend"}
    else:
        result = file_bib_files(
            args.bib, args.collection, dry_run=args.dry_run, log=_stderr, pdf_dir=args.pdf_dir
        )
        if args.pdf_dir is not None and "attachments" not in result:
            result["attachments"] = {"status": "not_run", "reason": f"filing status is {result['status']}"}
    print(json.dumps(result, indent=2))
    return 0 if result["status"] in ("ok", "dry_run") else 2


if __name__ == "__main__":
    raise SystemExit(main())
