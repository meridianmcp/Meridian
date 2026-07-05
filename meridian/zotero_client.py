"""Zotero LOCAL-API citation resolver — fefb596a.

The intra-document citation graph (commit a1e6c71) links a ``kind='citation'``
marker to a matching ``kind='bibliography'`` entry in the *same* document. This
module adds the **cross-document** hop the sprint item names: resolving an
in-text citation marker (a BibTeX citekey, a DOI, or an explicit Zotero item
key) to a canonical **Zotero item** via Zotero's LOCAL HTTP API.

Zotero exposes a local server (bundled with the desktop app) that speaks the
same JSON schema as the Zotero Web API v3, on ``http://127.0.0.1:23119/api`` by
default. The single-user local library is user id ``0``. The two endpoints we
use:

* ``GET /users/0/items/<itemKey>?format=json`` → one item object
  ``{key, version, library, data:{DOI, title, itemType, ...}}``.
* ``GET /users/0/items?q=<term>&qmode=everything&format=json&limit=N`` → a JSON
  array of item objects matching a free-text search.

**Guard contract (non-negotiable).** The local API is frequently *unavailable*:
Zotero may be closed, or the local API may be disabled (it returns HTTP 403
``"Local API is not enabled"`` in that state). Every failure mode — 403, 404, a
connection error, a timeout, a malformed body — is treated as *"unresolved"* and
returns ``None``. :func:`resolve_citation_ref` **never raises**. That keeps the
opt-in cross-document pass (:meth:`meridian.doc_store.DocStructureStore.resolve_zotero_edges`)
safe to run against a machine with no Zotero at all.

Reference-shape dispatch (:func:`_normalize_ref`):

* ``"doi:10.1000/xyz"`` or a bare string that *looks like* a DOI
  (:func:`_looks_like_doi`) → a ``q=`` search whose results are filtered to the
  item whose ``data.DOI`` matches case-insensitively. DOIs are the reliable key.
* ``"zotero:ABCD1234"`` → a direct ``GET /items/ABCD1234`` (the value is a
  Zotero item key).
* anything else (a bare BibTeX citekey like ``knuth1984``) → a best-effort
  ``q=<citekey>`` free-text search, returning the top hit if any. **Without
  Better BibTeX installed Zotero does not index the citekey itself**, so this is
  fuzzy and may miss (BBT is not assumed here); a DOI or ``zotero:`` ref is
  always preferred when available.

The client uses ``httpx`` (already a dependency; injectable for tests).
"""
from __future__ import annotations

import os
import re
import logging
from typing import Any

_log = logging.getLogger(__name__)


# Default base URL for Zotero's local API. Overridable via env so an operator can
# point at a non-default port or a proxy. The trailing ``/api`` is part of the
# Zotero local-server layout (``http://127.0.0.1:23119/api/...``).
_DEFAULT_BASE_URL = "http://127.0.0.1:23119/api"
_BASE_URL_ENV = "MERIDIAN_ZOTERO_API_URL"

# Local single-user library id (Zotero local API convention).
_LOCAL_USER_ID = "0"

# Per-request timeout — the local server answers instantly when up; when it is
# down we want to fail fast (connect refused is immediate, but a hung port
# should not stall the whole resolve pass). Mirrors the tunnel health probe.
_REQUEST_TIMEOUT = 10.0

# How many search hits to request for a q= lookup. Small: a DOI match needs only
# a handful of candidates, and a citekey search returns the most-relevant first.
_SEARCH_LIMIT = 25

# A DOI is ``10.<registrant>/<suffix>``: registrant is 4+ digits, suffix is any
# non-space run. Deliberately permissive on the suffix (DOIs allow a wide char
# set) but anchored so it matches a *whole* bare ref, not a substring.
_DOI_RE = re.compile(r"^10\.\d{4,}/\S+$")


def _base_url(base_url: str | None) -> str:
    """Resolve the effective base URL: explicit arg → env → built-in default."""
    if base_url and base_url.strip():
        return base_url.strip().rstrip("/")
    env = os.environ.get(_BASE_URL_ENV)
    if env and env.strip():
        return env.strip().rstrip("/")
    return _DEFAULT_BASE_URL


def _looks_like_doi(s: Any) -> bool:
    """True if ``s`` is a bare DOI string (``10.NNNN/....``).

    Accepts only the canonical bare form (no ``doi:`` / ``https://doi.org/``
    prefix — those are stripped by :func:`_normalize_ref` first). Non-strings and
    blanks are False.
    """
    if not isinstance(s, str):
        return False
    return bool(_DOI_RE.match(s.strip()))


def _normalize_ref(ref: Any) -> tuple[str, str] | None:
    """Classify a citation ref into ``(kind, value)`` or ``None``.

    ``kind`` is one of:

    * ``"doi"`` — ``value`` is the bare DOI (``10.x/y``). Recognised from a
      ``doi:`` prefix, a ``https?://(dx.)?doi.org/`` URL, or a bare DOI-shaped
      string.
    * ``"zotero"`` — ``value`` is a Zotero item key. Recognised from a
      ``zotero:`` prefix (the key is uppercased-alnum by Zotero; we pass it
      through verbatim after trimming).
    * ``"citekey"`` — ``value`` is a bare BibTeX citation key (the fallback for
      anything non-empty that is not a DOI or ``zotero:`` ref).

    Returns ``None`` for a non-string or blank ref (nothing to resolve).
    """
    if not isinstance(ref, str):
        return None
    r = ref.strip()
    if not r:
        return None

    lowered = r.lower()

    # Explicit DOI prefix.
    if lowered.startswith("doi:"):
        doi = r[4:].strip()
        return ("doi", doi) if doi else None

    # DOI URL forms (https://doi.org/10.x, http://dx.doi.org/10.x).
    doi_url = re.match(r"^https?://(?:dx\.)?doi\.org/(.+)$", r, re.IGNORECASE)
    if doi_url:
        doi = doi_url.group(1).strip()
        return ("doi", doi) if doi else None

    # Explicit Zotero item-key prefix.
    if lowered.startswith("zotero:"):
        key = r[7:].strip()
        return ("zotero", key) if key else None

    # Bare DOI-shaped string.
    if _looks_like_doi(r):
        return ("doi", r)

    # Fallback: treat as a BibTeX citekey for a best-effort text search.
    return ("citekey", r)


def _normalize_item(item: Any) -> dict[str, Any] | None:
    """Project a raw Zotero item object into our normalized resolver dict.

    A Zotero item is ``{key, version, library, data:{DOI, title, itemType, ...}}``.
    Returns ``{"zotero_key", "doi", "title", "item_type"}`` (``doi`` is ``None``
    when the item carries no DOI or a blank one), or ``None`` if ``item`` has no
    usable key (not a resolvable Zotero item).
    """
    if not isinstance(item, dict):
        return None
    data = item.get("data")
    data = data if isinstance(data, dict) else {}
    key = item.get("key") or data.get("key")
    if not isinstance(key, str) or not key.strip():
        return None
    doi_raw = data.get("DOI")
    doi = doi_raw.strip() if isinstance(doi_raw, str) and doi_raw.strip() else None
    title_raw = data.get("title")
    title = title_raw if isinstance(title_raw, str) else None
    item_type_raw = data.get("itemType")
    item_type = item_type_raw if isinstance(item_type_raw, str) else None
    return {
        "zotero_key": key.strip(),
        "doi": doi,
        "title": title,
        "item_type": item_type,
    }


async def _get_json(client: Any, url: str, params: dict[str, Any] | None) -> Any:
    """GET ``url`` and return parsed JSON, or ``None`` on ANY failure.

    Any non-2xx status (incl. 403 "Local API is not enabled" and 404), any
    transport error (connection refused / timeout), and any body that is not
    valid JSON all collapse to ``None``. This is the single choke-point that
    makes the whole client never-raises.
    """
    try:
        resp = await client.get(url, params=params)
    except Exception:  # noqa: BLE001 — connection refused / timeout / DNS / etc.
        _log.debug("zotero request failed: %s", url, exc_info=True)
        return None
    try:
        status = resp.status_code
    except Exception:  # noqa: BLE001 — a stub/mocked response missing the attr
        return None
    if status < 200 or status >= 300:
        # 403 (local API disabled), 404 (unknown item), 5xx, ... → unresolved.
        _log.debug("zotero non-2xx %s for %s", status, url)
        return None
    try:
        return resp.json()
    except Exception:  # noqa: BLE001 — malformed / empty body
        _log.debug("zotero response not JSON for %s", url, exc_info=True)
        return None


async def _resolve_zotero_key(
    client: Any, base: str, key: str
) -> dict[str, Any] | None:
    """Resolve a ``zotero:<key>`` ref via a direct item GET."""
    url = f"{base}/users/{_LOCAL_USER_ID}/items/{key}"
    body = await _get_json(client, url, {"format": "json"})
    # The item endpoint returns a single object; some servers may wrap it in a
    # list — handle both defensively.
    if isinstance(body, list):
        body = body[0] if body else None
    return _normalize_item(body)


async def _resolve_doi(
    client: Any, base: str, doi: str
) -> dict[str, Any] | None:
    """Resolve a DOI ref: text-search, then filter to a case-insensitive DOI match."""
    url = f"{base}/users/{_LOCAL_USER_ID}/items"
    body = await _get_json(
        client,
        url,
        {
            "q": doi,
            "qmode": "everything",
            "format": "json",
            "limit": _SEARCH_LIMIT,
        },
    )
    if not isinstance(body, list):
        return None
    target = doi.strip().lower()
    for item in body:
        normalized = _normalize_item(item)
        if normalized is None:
            continue
        item_doi = normalized.get("doi")
        if isinstance(item_doi, str) and item_doi.strip().lower() == target:
            return normalized
    # No item whose DOI matches — do NOT fabricate a link from a loose text hit.
    return None


async def _resolve_citekey(
    client: Any, base: str, citekey: str
) -> dict[str, Any] | None:
    """Best-effort resolve a bare BibTeX citekey via a text search.

    Without Better BibTeX, Zotero does not index the citekey token itself, so a
    hit here is fuzzy (it matches title/creator/etc. text that happens to contain
    the citekey string). We return the TOP hit if the search yields anything,
    else ``None``. Documented as lossy — DOIs are the reliable key.
    """
    url = f"{base}/users/{_LOCAL_USER_ID}/items"
    body = await _get_json(
        client,
        url,
        {
            "q": citekey,
            "qmode": "everything",
            "format": "json",
            "limit": _SEARCH_LIMIT,
        },
    )
    if not isinstance(body, list):
        return None
    for item in body:
        normalized = _normalize_item(item)
        if normalized is not None:
            return normalized
    return None


async def resolve_citation_ref(
    ref: str,
    *,
    base_url: str | None = None,
    client: Any = None,
) -> dict[str, Any] | None:
    """Resolve a citation ref to a canonical Zotero item, or ``None``.

    Dispatches on the ref shape (:func:`_normalize_ref`):

    * a DOI (``doi:...``, a ``doi.org`` URL, or a bare ``10.x/y`` string) →
      searches the library and returns the item whose ``data.DOI`` matches
      case-insensitively.
    * ``zotero:<key>`` → a direct item GET by Zotero key.
    * a bare citekey → a best-effort text search, top hit (fuzzy without BBT).

    On a hit returns a normalized dict::

        {"zotero_key": str, "doi": str | None, "title": str | None,
         "item_type": str | None}

    Returns ``None`` when the ref is empty/unclassifiable, when Zotero is
    unreachable or its local API is disabled (HTTP 403), when the item/DOI is not
    found, or on any error. **Never raises.**

    ``base_url`` overrides the endpoint (default: ``$MERIDIAN_ZOTERO_API_URL`` or
    ``http://127.0.0.1:23119/api``). ``client`` injects a preconfigured
    ``httpx.AsyncClient`` (used by tests to mock responses); when omitted a
    short-lived client is created for the single lookup.
    """
    parsed = _normalize_ref(ref)
    if parsed is None:
        return None
    kind, value = parsed
    base = _base_url(base_url)

    async def _dispatch(c: Any) -> dict[str, Any] | None:
        if kind == "zotero":
            return await _resolve_zotero_key(c, base, value)
        if kind == "doi":
            return await _resolve_doi(c, base, value)
        return await _resolve_citekey(c, base, value)

    try:
        if client is not None:
            return await _dispatch(client)
        import httpx  # noqa: PLC0415 — optional, imported lazily like other call sites

        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as owned:
            return await _dispatch(owned)
    except Exception:  # noqa: BLE001 — belt-and-suspenders: the pass must never break
        _log.debug("zotero resolve_citation_ref failed for %r", ref, exc_info=True)
        return None
