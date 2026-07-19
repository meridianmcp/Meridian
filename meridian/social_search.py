"""d58000c6 — social media search submodule, keyless (Hacker News via Algolia).

811881c6/f65f6111 (see :mod:`meridian.paper_search`) established a per-source
function pattern for the Research Module: a ``<source>_search`` async network call
plus a pure, never-raising ``parse_<source>_*`` helper that is fully unit-testable
without hitting the network, both normalized to the same ``{query, count, results}``
top-level shape.

This module is the first "additional optional search submodule beyond the core
Research Module" (b66c9168 / refiled as d58000c6): a social-media / public-discussion
search source. Hacker News was chosen over Twitter/X or Reddit because its Algolia-
powered search API (https://hn.algolia.com/api) is keyless and requires no OAuth or
paid tier — matching the exact keyless constraint paper_search.py is built around.
Twitter/X and Reddit's search APIs both require authenticated app credentials and
were explicitly out of scope for this item.

Only ``story`` items are searched (not raw comments) so results read like
discussion/link submissions rather than isolated comment fragments — closer in
spirit to a "paper" result than a mid-thread reply would be.
"""
from __future__ import annotations

import html
import re
from typing import Any

# Algolia's HN Search API. ``/search`` ranks by relevance; ``/search_by_date`` is
# the same index sorted chronologically (used for sort_by="date").
_HN_API = "https://hn.algolia.com/api/v1/search"
_HN_API_BY_DATE = "https://hn.algolia.com/api/v1/search_by_date"

_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    """Strip HTML tags from HN's ``story_text``/``comment_text`` and normalize
    whitespace. Never raises — anything unexpected degrades to ``""``.
    """
    if not text:
        return ""
    try:
        cleaned = _TAG_RE.sub(" ", text)
        cleaned = html.unescape(cleaned)
        return " ".join(cleaned.split())
    except Exception:  # noqa: BLE001 — a bad payload must never crash a tool call
        return ""


def _hn_permalink(object_id: str) -> str:
    return f"https://news.ycombinator.com/item?id={object_id}" if object_id else ""


def parse_hn_hits(payload: Any, limit: int = 10) -> list[dict[str, Any]]:
    """Parse an Algolia HN Search API JSON payload into a list of item dicts.

    Never raises — a malformed or empty payload degrades to ``[]``. Each result:
    ``{hn_id, title, authors, summary, published, updated, url, discussion_url,
    points, num_comments}`` — the same overall shape (title/authors/summary/
    published/updated/url) that :func:`meridian.paper_search.parse_arxiv_atom` and
    :func:`meridian.paper_search.parse_openalex_works` return, plus HN-specific
    ``discussion_url``/``points``/``num_comments``.

    ``payload`` is the decoded JSON object (``{"hits": [...]}``); passing the raw
    list of hits is also accepted.
    """
    if isinstance(payload, dict):
        hits = payload.get("hits")
    else:
        hits = payload
    if not isinstance(hits, (list, tuple)):
        return []
    out: list[dict[str, Any]] = []
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        object_id = str(hit.get("objectID") or "").strip()
        author = str(hit.get("author") or "").strip()
        title = " ".join(str(hit.get("title") or hit.get("story_title") or "").split())
        raw_text = hit.get("story_text") or hit.get("comment_text") or ""
        created_at = str(hit.get("created_at") or "").strip()
        url = str(hit.get("url") or hit.get("story_url") or "").strip()
        discussion_url = _hn_permalink(object_id)
        points = hit.get("points")
        points = points if isinstance(points, int) else 0
        num_comments = hit.get("num_comments")
        num_comments = num_comments if isinstance(num_comments, int) else 0
        out.append({
            "hn_id": object_id,
            "title": title,
            "authors": [author] if author else [],
            "summary": _strip_html(str(raw_text)),
            "published": created_at,
            "updated": created_at,
            "url": url or discussion_url,
            "discussion_url": discussion_url,
            "points": points,
            "num_comments": num_comments,
        })
        if len(out) >= limit:
            break
    return out


async def hn_search(
    query: str, limit: int = 10, sort_by: str = "relevance"
) -> dict[str, Any]:
    """Search Hacker News (keyless, via the Algolia HN Search API) and return
    ``{query, count, results:[...]}`` — the same top-level shape as
    :func:`meridian.paper_search.arxiv_search`/``openalex_search``.

    ``sort_by``: ``'relevance'`` (default, Algolia's ranking) or ``'date'`` (most
    recently submitted first, via the API's ``search_by_date`` endpoint).
    Never raises — an empty query returns ``{error}`` and any network/parse failure
    degrades to ``{error, query}`` so a research call can't crash the MCP handler.
    """
    q = (query or "").strip()
    if not q:
        return {"error": "query is required"}
    n = max(1, min(int(limit or 10), 50))
    endpoint = _HN_API_BY_DATE if str(sort_by).lower() in ("date", "recent", "newest") else _HN_API
    params = {
        "query": q,
        "hitsPerPage": str(n),
        "tags": "story",
    }
    import httpx as _httpx  # noqa: PLC0415 — match paper_search's inline-httpx pattern
    try:
        async with _httpx.AsyncClient(timeout=15.0) as http:
            resp = await http.get(
                endpoint, params=params,
                headers={"User-Agent": "Meridian/social_search (research routing)"},
            )
            resp.raise_for_status()
            results = parse_hn_hits(resp.json(), n)
    except Exception as exc:  # noqa: BLE001 — degrade, never crash the tool call
        return {"error": f"hn search failed: {exc}", "query": q}
    return {"query": q, "count": len(results), "results": results}
