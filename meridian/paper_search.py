"""811881c6 — real arXiv paper search.  f65f6111 — plus keyless OpenAlex.

The RESEARCH ROUTING PROTOCOL (agent_defaults.py) tells agents to "use the paper-search
MCP first when it is in your tool list (arXiv / Semantic Scholar-style lookup)" — but no
such callable tool existed, so an agent that followed the instruction hit "unknown tool".
This wires a REAL one: arXiv's export API is keyless (no secret plumbing) and returns an
Atom feed, which we parse into structured paper records. Web search + GitHub search can
follow the same template later; arXiv is first because it's the keyless, specifically-
needed piece.

f65f6111 adds OpenAlex as a SECOND keyless source (https://api.openalex.org/works) so the
tool isn't preprint-only — OpenAlex indexes published journal/conference works across every
discipline. Its ``/works?search=`` endpoint returns JSON, which we normalize to the SAME
result shape ``arxiv_search`` returns so a caller (and capture_research_finding) can treat
both sources uniformly.

Pure parsing (:func:`parse_arxiv_atom`, :func:`parse_openalex_works`) is separated from the
network calls (:func:`arxiv_search`, :func:`openalex_search`) so both can be unit-tested
deterministically without hitting the network.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any

_ARXIV_API = "http://export.arxiv.org/api/query"
_ATOM = "{http://www.w3.org/2005/Atom}"
_OPENALEX_API = "https://api.openalex.org/works"


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


def _openalex_abstract(inverted_index: Any) -> str:
    """Reconstruct an abstract from OpenAlex's ``abstract_inverted_index``.

    OpenAlex stores abstracts as ``{word: [positions...]}`` (an inverted index) rather
    than plain text. We invert it back into a linear string. Never raises — anything
    unexpected (missing/None/malformed) degrades to ``""``.
    """
    if not isinstance(inverted_index, dict):
        return ""
    try:
        positioned: list[tuple[int, str]] = []
        for word, positions in inverted_index.items():
            if not isinstance(positions, (list, tuple)):
                continue
            for pos in positions:
                if isinstance(pos, int):
                    positioned.append((pos, str(word)))
        positioned.sort(key=lambda pw: pw[0])
        return " ".join(word for _, word in positioned)
    except Exception:  # noqa: BLE001 — a bad abstract must never crash a tool call
        return ""


def parse_openalex_works(payload: Any, limit: int = 10) -> list[dict[str, Any]]:
    """Parse an OpenAlex ``/works`` JSON payload into a list of paper dicts, normalized to
    the SAME shape ``parse_arxiv_atom`` returns. Never raises — a malformed or empty
    payload degrades to ``[]``. Each result:
    ``{openalex_id, title, authors, summary, published, updated, url, pdf_url, doi}``.

    ``payload`` is the decoded JSON object (``{"results": [...]}``); passing the raw list
    of works is also accepted.
    """
    if isinstance(payload, dict):
        works = payload.get("results")
    else:
        works = payload
    if not isinstance(works, (list, tuple)):
        return []
    out: list[dict[str, Any]] = []
    for work in works:
        if not isinstance(work, dict):
            continue
        authors = []
        for authorship in work.get("authorships") or []:
            if not isinstance(authorship, dict):
                continue
            author = authorship.get("author")
            name = (author.get("display_name") if isinstance(author, dict) else "") or ""
            name = name.strip()
            if name:
                authors.append(name)
        primary = work.get("primary_location")
        primary = primary if isinstance(primary, dict) else {}
        landing = (primary.get("landing_page_url") or "").strip()
        pdf_url = (primary.get("pdf_url") or "").strip()
        raw_id = (work.get("id") or "").strip()  # e.g. https://openalex.org/W2741809807
        doi = (work.get("doi") or "").strip()  # e.g. https://doi.org/10.7717/peerj.4375
        title = " ".join(str(work.get("title") or work.get("display_name") or "").split())
        published = (work.get("publication_date") or "").strip()
        updated = (work.get("updated_date") or "").strip()
        out.append({
            "openalex_id": raw_id.rsplit("/", 1)[-1] if raw_id else "",
            "title": title,
            "authors": authors,
            "summary": _openalex_abstract(work.get("abstract_inverted_index")),
            "published": published,
            "updated": updated,
            "url": landing or doi or raw_id,
            "pdf_url": pdf_url,
            "doi": doi,
        })
        if len(out) >= limit:
            break
    return out


async def openalex_search(
    query: str, limit: int = 10, sort_by: str = "relevance"
) -> dict[str, Any]:
    """Search OpenAlex and return ``{query, count, results:[...]}`` in the SAME shape as
    :func:`arxiv_search`.

    ``sort_by``: ``'relevance'`` (default) or ``'date'`` (most-recent publication first).
    Never raises — an empty query returns ``{error}`` and any network/parse failure
    degrades to ``{error, query}`` so a research call can't crash the MCP handler. Mirrors
    ``arxiv_search`` exactly (keyless, best-effort, non-raising).
    """
    q = (query or "").strip()
    if not q:
        return {"error": "query is required"}
    n = max(1, min(int(limit or 10), 50))
    params = {
        "search": q,
        "per-page": str(n),
        # A mailto puts the request in OpenAlex's faster "polite pool" (keyless still).
        "mailto": "research@usemeridian.us",
    }
    if str(sort_by).lower() in ("date", "recent", "newest"):
        params["sort"] = "publication_date:desc"
    import httpx as _httpx  # noqa: PLC0415 — match the handler's inline-httpx pattern
    try:
        async with _httpx.AsyncClient(timeout=15.0) as http:
            resp = await http.get(
                _OPENALEX_API, params=params,
                headers={"User-Agent": "Meridian/paper_search (research routing)"},
            )
            resp.raise_for_status()
            results = parse_openalex_works(resp.json(), n)
    except Exception as exc:  # noqa: BLE001 — degrade, never crash the tool call
        return {"error": f"openalex search failed: {exc}", "query": q}
    return {"query": q, "count": len(results), "results": results}
