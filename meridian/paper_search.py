"""811881c6 — real arXiv paper search.

The RESEARCH ROUTING PROTOCOL (agent_defaults.py) tells agents to "use the paper-search
MCP first when it is in your tool list (arXiv / Semantic Scholar-style lookup)" — but no
such callable tool existed, so an agent that followed the instruction hit "unknown tool".
This wires a REAL one: arXiv's export API is keyless (no secret plumbing) and returns an
Atom feed, which we parse into structured paper records. Web search + GitHub search can
follow the same template later; arXiv is first because it's the keyless, specifically-
needed piece.

Pure parsing (:func:`parse_arxiv_atom`) is separated from the network call
(:func:`arxiv_search`) so it can be unit-tested deterministically without hitting arXiv.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any

_ARXIV_API = "http://export.arxiv.org/api/query"
_ATOM = "{http://www.w3.org/2005/Atom}"


def parse_arxiv_atom(xml_text: str, limit: int = 10) -> list[dict[str, Any]]:
    """Parse an arXiv Atom feed into a list of paper dicts. Never raises — malformed or
    empty XML degrades to ``[]``. Each result:
    ``{arxiv_id, title, authors, summary, published, updated, url, pdf_url}``.
    """
    try:
        root = ET.fromstring(xml_text or "")
    except Exception:  # noqa: BLE001 — a bad feed must never crash a tool call
        return []
    out: list[dict[str, Any]] = []
    for entry in root.findall(f"{_ATOM}entry"):
        def _text(tag: str) -> str:
            el = entry.find(f"{_ATOM}{tag}")
            return el.text.strip() if el is not None and el.text else ""

        raw_id = _text("id")  # e.g. http://arxiv.org/abs/2401.01234v1
        authors = [
            (a.findtext(f"{_ATOM}name") or "").strip()
            for a in entry.findall(f"{_ATOM}author")
        ]
        pdf_url, abs_url = "", raw_id
        for link in entry.findall(f"{_ATOM}link"):
            if link.get("title") == "pdf":
                pdf_url = link.get("href", "")
            elif link.get("rel") == "alternate":
                abs_url = link.get("href", abs_url)
        out.append({
            "arxiv_id": raw_id.rsplit("/", 1)[-1] if raw_id else "",
            "title": " ".join(_text("title").split()),
            "authors": [a for a in authors if a],
            "summary": " ".join(_text("summary").split()),
            "published": _text("published"),
            "updated": _text("updated"),
            "url": abs_url,
            "pdf_url": pdf_url,
        })
        if len(out) >= limit:
            break
    return out


async def arxiv_search(
    query: str, limit: int = 10, sort_by: str = "relevance"
) -> dict[str, Any]:
    """Search arXiv and return ``{query, count, results:[...]}``.

    ``sort_by``: ``'relevance'`` (default) or ``'date'`` (most-recently-updated first).
    Never raises — an empty query returns ``{error}`` and any network/parse failure
    degrades to ``{error, query}`` so a research call can't crash the MCP handler.
    """
    q = (query or "").strip()
    if not q:
        return {"error": "query is required"}
    n = max(1, min(int(limit or 10), 50))
    sort = "lastUpdatedDate" if str(sort_by).lower() in ("date", "recent", "newest") else "relevance"
    params = {
        "search_query": f"all:{q}",
        "start": "0",
        "max_results": str(n),
        "sortBy": sort,
        "sortOrder": "descending",
    }
    import httpx as _httpx  # noqa: PLC0415 — match the handler's inline-httpx pattern
    try:
        async with _httpx.AsyncClient(timeout=15.0) as http:
            resp = await http.get(
                _ARXIV_API, params=params,
                headers={"User-Agent": "Meridian/paper_search (research routing)"},
            )
            resp.raise_for_status()
            results = parse_arxiv_atom(resp.text, n)
    except Exception as exc:  # noqa: BLE001 — degrade, never crash the tool call
        return {"error": f"arxiv search failed: {exc}", "query": q}
    return {"query": q, "count": len(results), "results": results}
