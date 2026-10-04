"""7273d2fa — Zotero search submodule, keyless-where-possible.

811881c6/f65f6111 (see :mod:`meridian.paper_search`) established a per-source
function pattern for the Research Module: a ``<source>_search`` async network
call plus a pure, never-raising ``parse_<source>_*`` helper that is fully
unit-testable without hitting the network, both normalized to the same
``{query, count, results}`` top-level shape. :mod:`meridian.social_search`
and :mod:`meridian.github_search` both follow it; this module is the fourth
sibling and the first to search a USER'S OWN reference-manager library rather
than a public corpus.

Zotero's Web API v3 (https://www.zotero.org/support/dev/web_api/v3/start)
allows public groups without credentials, while private user libraries need
an API key. This hosted search intentionally accepts no key argument and does
not read server environment variables: private libraries should be reached
through the user's local Zotero MCP connection, whose key is stored in the
workstation's OS credential store and injected only into that local process.

Zotero API version 3 is pinned explicitly via the ``Zotero-API-Version``
header, matching the API's own recommendation to always pin a version.
"""
from __future__ import annotations

from typing import Any

_ZOTERO_API_BASE = "https://api.zotero.org"
_ZOTERO_API_VERSION = "3"


def _zotero_web_url(library_type: str, library_id: str, item_key: str) -> str:
    """Best-effort web-UI permalink for a Zotero item, used only as a
    fallback when the API response's own ``links.alternate.href`` is
    missing. Never raises -- an empty/odd input degrades to ``""``.
    """
    if not (library_id and item_key):
        return ""
    plural = "groups" if library_type == "group" else "users"
    return f"https://www.zotero.org/{plural}/{library_id}/items/{item_key}"


def _format_creator(creator: dict[str, Any]) -> str:
    """Zotero creators are either {firstName, lastName} (two-field name) or
    {name} (single-field, e.g. an organization). Never raises.
    """
    if not isinstance(creator, dict):
        return ""
    single = str(creator.get("name") or "").strip()
    if single:
        return single
    first = str(creator.get("firstName") or "").strip()
    last = str(creator.get("lastName") or "").strip()
    return " ".join(p for p in (first, last) if p)


def parse_zotero_items(payload: Any, library_type: str, library_id: str, limit: int = 10) -> list[dict[str, Any]]:
    """Parse a Zotero Web API v3 ``/items`` JSON payload into a list of item
    dicts. Never raises -- a malformed or empty payload degrades to ``[]``.

    Each result: ``{zotero_key, title, authors, summary, published, updated,
    url, item_type, tags}`` -- the same overall shape (title/authors/summary/
    published/updated/url) that :func:`meridian.paper_search.parse_arxiv_atom`
    and :func:`meridian.social_search.parse_hn_hits` return, plus Zotero-
    specific ``zotero_key``/``item_type``/``tags``.

    ``payload`` is the decoded JSON array of item objects Zotero's API
    returns; anything else (a dict, ``None``, a malformed row) is tolerated
    and skipped rather than raising.
    """
    if not isinstance(payload, (list, tuple)):
        return []
    out: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        data = item.get("data")
        if not isinstance(data, dict):
            continue
        item_type = str(data.get("itemType") or "").strip()
        if item_type in ("attachment", "note"):
            continue  # not a citable reference -- skip attachments/standalone notes
        key = str(data.get("key") or "").strip()
        title = " ".join(str(data.get("title") or "").split())
        creators = data.get("creators")
        authors = (
            [name for c in creators if (name := _format_creator(c))]
            if isinstance(creators, list)
            else []
        )
        tags_raw = data.get("tags")
        tags = (
            [str(t.get("tag") or "").strip() for t in tags_raw if isinstance(t, dict) and t.get("tag")]
            if isinstance(tags_raw, list)
            else []
        )
        alternate_url = ""
        links = item.get("links")
        if isinstance(links, dict):
            alternate = links.get("alternate")
            if isinstance(alternate, dict):
                alternate_url = str(alternate.get("href") or "").strip()
        url = str(data.get("url") or "").strip() or alternate_url or _zotero_web_url(library_type, library_id, key)
        out.append({
            "zotero_key": key,
            "title": title,
            "authors": authors,
            "summary": str(data.get("abstractNote") or "").strip(),
            "published": str(data.get("date") or "").strip(),
            "updated": str(data.get("dateModified") or "").strip(),
            "url": url,
            "item_type": item_type,
            "tags": tags,
        })
        if len(out) >= limit:
            break
    return out


async def zotero_search(
    query: str,
    library_type: str = "user",
    library_id: str = "",
    limit: int = 10,
    sort_by: str = "relevance",
) -> dict[str, Any]:
    """Search a Zotero library (Web API v3) and return ``{query, count,
    results:[...]}`` -- the same top-level shape as
    :func:`meridian.paper_search.arxiv_search`/:func:`meridian.social_search.hn_search`.

    ``library_type``: ``'user'`` (default, a personal library) or ``'group'``.
    ``library_id``: the numeric Zotero userID or groupID -- required; a
    public GROUP library needs no key, a private USER library does.
    Hosted tool path: deliberately keyless. A private user library returns a
    useful error directing the user to the locally configured Zotero MCP slot.
    ``sort_by``: ``'relevance'`` (default, Zotero's own result ordering) or
    ``'date'`` (most recently added first).

    Never raises -- a missing query/library_id returns ``{error}`` and any
    network/parse failure degrades to ``{error, query}`` so a research call
    can't crash the MCP handler.
    """
    q = (query or "").strip()
    if not q:
        return {"error": "query is required"}
    lib_id = (library_id or "").strip()
    if not lib_id:
        return {"error": "library_id is required (a Zotero userID or groupID)"}
    lib_type = "group" if str(library_type or "user").strip().lower() == "group" else "user"
    n = max(1, min(int(limit or 10), 50))
    plural = "groups" if lib_type == "group" else "users"
    endpoint = f"{_ZOTERO_API_BASE}/{plural}/{lib_id}/items"
    params: dict[str, str] = {
        "q": q,
        "qmode": "everything",
        "itemType": "-attachment",
        "limit": str(n),
    }
    if str(sort_by).lower() in ("date", "recent", "newest"):
        params["sort"] = "date"
        params["direction"] = "desc"
    headers = {
        "Zotero-API-Version": _ZOTERO_API_VERSION,
        "User-Agent": "Meridian/zotero_search (research routing)",
    }
    import httpx as _httpx  # noqa: PLC0415 — match paper_search's inline-httpx pattern
    try:
        async with _httpx.AsyncClient(timeout=15.0) as http:
            resp = await http.get(endpoint, params=params, headers=headers)
            resp.raise_for_status()
            results = parse_zotero_items(resp.json(), lib_type, lib_id, n)
    except _httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        if status == 403:
            return {"error": "zotero search failed: 403 Forbidden — this library is private; use your locally configured Zotero MCP connection", "query": q}
        if status == 404:
            return {"error": f"zotero search failed: 404 Not Found — no such {lib_type} library {lib_id!r}", "query": q}
        return {"error": f"zotero search failed: {exc}", "query": q}
    except Exception as exc:  # noqa: BLE001 — degrade, never crash the tool call
        return {"error": f"zotero search failed: {exc}", "query": q}
    return {"query": q, "count": len(results), "results": results}
