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

9dc630de adds two more sources. Crossref (keyless) is the DOI registry itself: weaker than
OpenAlex/Semantic Scholar for topical discovery (its relevance ranking is metadata matching),
but authoritative for DOI resolution and venue metadata -- journal name, ISSN, publisher,
work type -- which is what matters when targeting a specific journal. CORE aggregates
open-access full text; unlike every other source here it needs an API key (an
unauthenticated probe got HTTP 429 on its very first request), so :func:`core_search` fails
closed with an explicit error when ``CORE_API_KEY`` is unset rather than degrading to a
different source.

454bdee5 makes ``arxiv_search`` survive hosted egress. From the hosted (Fly.io) server,
arXiv's export API answers HTTP 406 to a request that returns 200 from a residential
machine with the same URL and User-Agent, so this is an egress/IP refusal, not a
request-format bug, and no header change fixes it. ``arxiv_search`` still tries arXiv
first (the happy path is unchanged), and when arXiv refuses (403/406), stays rate-limited
or erroring after backoff (429/5xx), is unreachable (a transport error), or answers with a
body that is not an Atom feed, it falls back to OpenAlex restricted to works with an arXiv
location, then to Semantic Scholar papers carrying an arXiv id. Fallback rows keep the
arXiv row shape (``arxiv_id`` included, so watchlist dedup still works), and the result
gains ``fallback_source``, ``warning`` and ``sources_tried``. Crossref is deliberately not a
fallback: arXiv DOIs (10.48550/arXiv.*) are registered with DataCite, not Crossref, so
Crossref cannot return arXiv ids. ``openalex_search`` now also goes through
:func:`_fetch_with_backoff`, advertises a mailto in its User-Agent, and sends
``OPENALEX_API_KEY`` (when set) as a bearer header: OpenAlex budgets anonymous use per IP
(a live 2026-09-27 probe showed a 1000-credit daily budget with a search costing 10
credits, and anonymous search being throttled with ``Retry-After: ~30`` under load), so
on a shared hosted IP a free key, not a mailto, is what actually stops the 429s.
"""
from __future__ import annotations

import asyncio
import email.utils
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any

# 995e27a5 — arXiv's export API now 301-redirects http -> https. httpx does NOT
# follow redirects by default and raise_for_status() ignores 3xx, so the redirect
# body parsed to zero results and every arXiv query silently failed. Request https
# directly (and follow redirects defensively, below).
_ARXIV_API = "https://export.arxiv.org/api/query"
_ATOM = "{http://www.w3.org/2005/Atom}"
_OPENALEX_API = "https://api.openalex.org/works"
# 2e51a41a — Semantic Scholar (keyless, 100 req/min unauthenticated)
_S2_PAPER_API = "https://api.semanticscholar.org/graph/v1/paper/search"
_S2_AUTHOR_API = "https://api.semanticscholar.org/graph/v1/author/search"
# NCBI E-utilities (keyless for basic access; tool+email put requests in the polite pool)
_PUBMED_ESEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
_PUBMED_EFETCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
_NCBI_TOOL = "Meridian"
_NCBI_EMAIL = "research@usemeridian.us"
# 9dc630de — Crossref REST (keyless; a mailto puts requests in the polite pool) and CORE v3
# (key required). CORE's bare path 301-redirects to the trailing-slash form.
_CROSSREF_API = "https://api.crossref.org/works"
_CORE_API = "https://api.core.ac.uk/v3/search/works/"
_CONTACT_EMAIL = "research@usemeridian.us"
_POLITE_USER_AGENT = f"Meridian/paper_search (research routing; mailto:{_CONTACT_EMAIL})"
_RETRY_DELAYS = (0.5, 1.5)  # waits [s] before attempt 2 and attempt 3; no wait before 1
# 454bdee5 — a Retry-After longer than this means "not within this tool call": fail fast
# (so a fallback source can answer) instead of sleeping, or retrying early for nothing.
_MAX_RETRY_AFTER = 5.0
# Statuses worth retrying for the sources routed through _fetch_with_backoff with an
# explicit retry set (arXiv, OpenAlex, the arXiv fallbacks). Crossref/CORE keep the
# original (429, 503) default.
_TRANSIENT_STATUSES = frozenset({429, 500, 502, 503, 504})
# arXiv answers these to requests it refuses outright (hosted egress got 406 on
# 2026-09-26); retrying cannot help, so they go straight to the fallback chain.
_ARXIV_REFUSED_STATUSES = frozenset({403, 406})
# Wall-clock cap [s] on arXiv's retry loop: no retry starts once elapsed + wait would
# pass it. arXiv's Varnish was seen (2026-09-27) holding a request ~60s before a 503.
_ARXIV_RETRY_BUDGET = 20.0
# OpenAlex's id for the arXiv repository source ("arXiv (Cornell University)",
# type=repository, host I205783295), verified live 2026-09-27 via GET /sources/S4306400194.
_OPENALEX_ARXIV_SOURCE = "S4306400194"
_MARKUP_TAG_RE = re.compile(r"<[^>]+>")


def parse_arxiv_atom(xml_text: str, limit: int = 10) -> list[dict[str, Any]]:
    """Parse an arXiv Atom feed into a list of paper dicts. Never raises — malformed or
    empty XML degrades to ``[]``. Each result:
    ``{arxiv_id, title, authors, summary, published, updated, url, pdf_url}``.
    """
    return _parse_arxiv_feed(xml_text, limit) or []


def _parse_arxiv_feed(xml_text: str, limit: int = 10) -> list[dict[str, Any]] | None:
    """Like :func:`parse_arxiv_atom`, but ``None`` when the body is not an Atom feed at all.

    That distinction is what lets ``arxiv_search`` tell "arXiv found nothing" (an empty
    feed, ``[]``) from "something other than arXiv's API answered" (an HTML block or
    challenge page, a proxy error body served with a 200), which is a reason to fall back.
    """
    try:
        root = ET.fromstring(xml_text or "")
    except Exception:  # noqa: BLE001 — a bad feed must never crash a tool call
        return None
    if root.tag != f"{_ATOM}feed":
        return None
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

    454bdee5 — arXiv is always tried first, with the same request as before. If arXiv
    refuses (403/406), is still rate-limited or failing after backoff (429/5xx), cannot be
    reached, or answers with something that is not an Atom feed, the search falls back to
    OpenAlex and then Semantic Scholar (see :func:`_arxiv_fallback_search`). A fallback
    answer has the same rows plus ``fallback_source``, ``warning`` and ``sources_tried``;
    if every source fails, the result is the usual ``{error, query}`` plus
    ``sources_tried``. Any other failure (e.g. a 400) is returned as an error without a
    fallback, as before.
    """
    q = (query or "").strip()
    if not q:
        return {"error": "query is required"}
    n = max(1, min(int(limit or 10), 50))
    sort = "lastUpdatedDate" if _wants_date_sort(sort_by) else "relevance"
    params = {
        "search_query": f"all:{q}",
        "start": "0",
        "max_results": str(n),
        "sortBy": sort,
        "sortOrder": "descending",
    }
    import httpx as _httpx  # noqa: PLC0415 — match the handler's inline-httpx pattern
    try:
        # 995e27a5 — follow_redirects so a future http->https (or mirror) 301 is
        # honoured instead of silently parsing a redirect body to zero results.
        async with _httpx.AsyncClient(timeout=15.0, follow_redirects=True) as http:
            resp = await _fetch_with_backoff(
                http, _ARXIV_API, params,
                {"User-Agent": "Meridian/paper_search (research routing)"},
                retry_statuses=_TRANSIENT_STATUSES,
                # arXiv's Varnish has been seen holding a request ~60s before a 503;
                # never spend more than this retrying when a fallback can answer.
                budget=_ARXIV_RETRY_BUDGET,
            )
            results = _parse_arxiv_feed(resp.text, n)
    except Exception as exc:  # noqa: BLE001 — degrade, never crash the tool call
        reason = _arxiv_unreachable_reason(exc)
        if reason is None:
            return {"error": f"arxiv search failed: {exc}", "query": q}
        return await _arxiv_fallback_search(q, n, sort_by, reason)
    if results is None:
        return await _arxiv_fallback_search(q, n, sort_by, "response was not an Atom feed")
    return {"query": q, "count": len(results), "results": results}


def _wants_date_sort(sort_by: Any) -> bool:
    return str(sort_by).lower() in ("date", "recent", "newest")


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

    454bdee5 — sends ``mailto`` as a query parameter AND in the User-Agent (the Crossref
    pattern), retries 429/5xx through :func:`_fetch_with_backoff`, and uses
    ``OPENALEX_API_KEY`` when it is set (see :func:`_openalex_get_works`). A 429 that
    outlasts the backoff says how to raise the budget.
    """
    q = (query or "").strip()
    if not q:
        return {"error": "query is required"}
    n = max(1, min(int(limit or 10), 50))
    api_key = _openalex_api_key()
    try:
        payload = await _openalex_get_works(q, n, sort_by, api_key=api_key)
        results = parse_openalex_works(payload, n)
    except Exception as exc:  # noqa: BLE001 — degrade, never crash the tool call
        message = f"openalex search failed: {exc}"
        if _http_status(exc) == 429 and not api_key:
            message += (
                " -- OpenAlex budgets anonymous search per IP; set OPENALEX_API_KEY "
                "(free key: https://openalex.org/rest-api) for a higher limit"
            )
        return {"error": _redact(message, api_key), "query": q}
    return {"query": q, "count": len(results), "results": results}


def _openalex_api_key() -> str:
    return os.environ.get("OPENALEX_API_KEY", "").strip()


async def _openalex_get_works(
    q: str, n: int, sort_by: Any, *, api_key: str = "", filter_expr: str = ""
) -> Any:
    """GET OpenAlex ``/works?search=`` and return the decoded JSON; raises on failure.

    Shared by :func:`openalex_search` and the arXiv fallback. ``mailto`` goes in both the
    query string (OpenAlex's documented polite-pool parameter) and the User-Agent. The API
    key, when there is one, goes only in an ``Authorization: Bearer`` header (documented as
    equivalent to ``api_key=``), never the query string, because httpx puts the full
    request URL into its exception messages.
    """
    params = {"search": q, "per-page": str(n), "mailto": _CONTACT_EMAIL}
    if _wants_date_sort(sort_by):
        params["sort"] = "publication_date:desc"
    if filter_expr:
        params["filter"] = filter_expr
    headers = {"User-Agent": _POLITE_USER_AGENT}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    import httpx as _httpx  # noqa: PLC0415 — match the handler's inline-httpx pattern
    async with _httpx.AsyncClient(timeout=15.0, follow_redirects=True) as http:
        resp = await _fetch_with_backoff(
            http, _OPENALEX_API, params, headers, retry_statuses=_TRANSIENT_STATUSES,
        )
        return resp.json()


# ---------------------------------------------------------------------------
# 2e51a41a — Semantic Scholar paper search + author lookup + PubMed
# ---------------------------------------------------------------------------

def parse_semantic_scholar_papers(payload: Any, limit: int = 10) -> list[dict[str, Any]]:
    """Parse a Semantic Scholar ``/paper/search`` JSON payload into normalized paper dicts.

    Never raises — a malformed or empty payload degrades to ``[]``. Each result:
    ``{s2_id, title, authors, summary, published, updated, url, pdf_url,
    citation_count, doi, tldr}`` — ``summary`` is ``abstract`` if present, falling
    back to ``tldr.text``; ``url`` is the open-access PDF URL if available, else
    the S2 paper page.
    """
    if isinstance(payload, dict):
        papers = payload.get("data")
    else:
        papers = payload
    if not isinstance(papers, (list, tuple)):
        return []
    out: list[dict[str, Any]] = []
    for paper in papers:
        if not isinstance(paper, dict):
            continue
        paper_id = str(paper.get("paperId") or "").strip()
        title = " ".join(str(paper.get("title") or "").split())
        authors = [
            str(a.get("name") or "").strip()
            for a in (paper.get("authors") or [])
            if isinstance(a, dict) and (a.get("name") or "").strip()
        ]
        abstract = (paper.get("abstract") or "").strip()
        tldr_obj = paper.get("tldr")
        tldr_text = ""
        if isinstance(tldr_obj, dict):
            tldr_text = (tldr_obj.get("text") or "").strip()
        summary = abstract or tldr_text
        year = paper.get("year")
        published = str(year) if year is not None else ""
        oap = paper.get("openAccessPdf")
        pdf_url = ""
        if isinstance(oap, dict):
            pdf_url = (oap.get("url") or "").strip()
        url = pdf_url or (
            f"https://www.semanticscholar.org/paper/{paper_id}" if paper_id else ""
        )
        citation_count = paper.get("citationCount")
        citation_count = citation_count if isinstance(citation_count, int) else 0
        external_ids = paper.get("externalIds")
        doi = ""
        if isinstance(external_ids, dict):
            doi = str(external_ids.get("DOI") or "").strip()
        out.append({
            "s2_id": paper_id,
            "title": title,
            "authors": authors,
            "summary": summary,
            "published": published,
            "updated": "",
            "url": url,
            "pdf_url": pdf_url,
            "citation_count": citation_count,
            "doi": doi,
            "tldr": tldr_text,
        })
        if len(out) >= limit:
            break
    return out


async def semantic_scholar_search(
    query: str, limit: int = 10, sort_by: str = "relevance"
) -> dict[str, Any]:
    """Search Semantic Scholar (keyless) and return ``{query, count, results:[...]}``.

    Results share the ``title/authors/summary/published/updated/url/pdf_url`` base
    shape with :func:`arxiv_search` / :func:`openalex_search` and extend it with
    ``s2_id``, ``citation_count``, ``doi``, and ``tldr``.  ``sort_by`` is accepted
    for API-shape consistency but S2's ``/paper/search`` endpoint does not expose a
    date sort — it always returns by relevance.  Never raises — degrades to
    ``{error, query}`` on any failure.
    """
    q = (query or "").strip()
    if not q:
        return {"error": "query is required"}
    n = max(1, min(int(limit or 10), 50))
    params = {
        "query": q,
        "limit": str(n),
        "fields": "title,authors,abstract,year,citationCount,tldr,openAccessPdf,externalIds",
    }
    import httpx as _httpx  # noqa: PLC0415 — match inline-httpx pattern
    try:
        async with _httpx.AsyncClient(timeout=15.0, follow_redirects=True) as http:
            resp = await http.get(
                _S2_PAPER_API, params=params,
                headers={"User-Agent": "Meridian/paper_search (research routing)"},
            )
            resp.raise_for_status()
            results = parse_semantic_scholar_papers(resp.json(), n)
    except Exception as exc:  # noqa: BLE001 — degrade, never crash the tool call
        return {"error": f"semantic scholar search failed: {exc}", "query": q}
    return {"query": q, "count": len(results), "results": results}


async def author_search(name: str, limit: int = 5) -> dict[str, Any]:
    """Look up an author's publication record via Semantic Scholar's author endpoint.

    Resolves the author's actual profile by name rather than running a text-based
    paper search, preventing attribution errors where fuzzy matching says "person X
    co-authored paper Y" when it was actually person Z.  Returns
    ``{query, count, results:[{author_id, name, affiliations, paper_count,
    citation_count, papers:[{title, year, doi}]}]}``.  Never raises — degrades to
    ``{error, query}`` on any failure.
    """
    name_q = (name or "").strip()
    if not name_q:
        return {"error": "name is required", "query": name_q}
    n = max(1, min(int(limit or 5), 50))
    params = {
        "query": name_q,
        "fields": "name,affiliations,paperCount,citationCount,papers.title,papers.year,papers.externalIds",
        "limit": str(n),
    }
    import httpx as _httpx  # noqa: PLC0415
    try:
        async with _httpx.AsyncClient(timeout=15.0, follow_redirects=True) as http:
            resp = await http.get(
                _S2_AUTHOR_API, params=params,
                headers={"User-Agent": "Meridian/paper_search (research routing)"},
            )
            resp.raise_for_status()
            payload = resp.json()
    except Exception as exc:  # noqa: BLE001
        return {"error": f"author search failed: {exc}", "query": name_q}

    authors_raw = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(authors_raw, (list, tuple)):
        authors_raw = []
    results: list[dict[str, Any]] = []
    for author in authors_raw:
        if not isinstance(author, dict):
            continue
        author_id = str(author.get("authorId") or "").strip()
        author_name = str(author.get("name") or "").strip()
        affiliations_raw = author.get("affiliations")
        affiliations = (
            [str(a).strip() for a in affiliations_raw if a]
            if isinstance(affiliations_raw, (list, tuple))
            else []
        )
        paper_count = author.get("paperCount")
        paper_count = paper_count if isinstance(paper_count, int) else 0
        citation_count = author.get("citationCount")
        citation_count = citation_count if isinstance(citation_count, int) else 0
        papers_raw = author.get("papers")
        papers: list[dict[str, Any]] = []
        if isinstance(papers_raw, (list, tuple)):
            for p in papers_raw:
                if not isinstance(p, dict):
                    continue
                p_year = p.get("year")
                ext_ids = p.get("externalIds")
                papers.append({
                    "title": str(p.get("title") or "").strip(),
                    "year": p_year if isinstance(p_year, int) else None,
                    "doi": str((ext_ids or {}).get("DOI") or "").strip()
                    if isinstance(ext_ids, dict)
                    else "",
                })
        results.append({
            "author_id": author_id,
            "name": author_name,
            "affiliations": affiliations,
            "paper_count": paper_count,
            "citation_count": citation_count,
            "papers": papers,
        })
    return {"query": name_q, "count": len(results), "results": results}


def parse_pubmed_articles(xml_text: str, limit: int = 10) -> list[dict[str, Any]]:
    """Parse a PubMed efetch XML response (rettype=abstract) into normalized paper dicts.

    Never raises — a malformed or empty payload degrades to ``[]``. Each result:
    ``{pmid, title, authors, summary, published, updated, url, pdf_url, doi}``.
    ``summary`` is the full abstract (concatenated if structured); ``url`` is the
    canonical PubMed article page; ``pdf_url`` is always ``""`` (PubMed does not
    provide direct PDF links).
    """
    try:
        root = ET.fromstring(xml_text or "")
    except Exception:  # noqa: BLE001
        return []
    out: list[dict[str, Any]] = []
    for article in root.findall(".//PubmedArticle"):
        try:
            citation = article.find("MedlineCitation")
            if citation is None:
                continue
            pmid_el = citation.find("PMID")
            pmid = pmid_el.text.strip() if pmid_el is not None and pmid_el.text else ""

            article_el = citation.find("Article")
            if article_el is None:
                continue

            title_el = article_el.find("ArticleTitle")
            title = " ".join((title_el.text or "").split()) if title_el is not None else ""

            # Abstract — may be structured (multiple AbstractText elements with labels)
            abstract_parts: list[str] = []
            abstract_el = article_el.find("Abstract")
            if abstract_el is not None:
                for at in abstract_el.findall("AbstractText"):
                    part = (at.text or "").strip()
                    if part:
                        abstract_parts.append(part)
            summary = " ".join(abstract_parts)

            # Authors
            authors: list[str] = []
            author_list = article_el.find("AuthorList")
            if author_list is not None:
                for auth in author_list.findall("Author"):
                    last = (auth.findtext("LastName") or "").strip()
                    fore = (auth.findtext("ForeName") or auth.findtext("Initials") or "").strip()
                    if last:
                        authors.append(f"{fore} {last}".strip() if fore else last)

            # Publication date — prefer structured Year/Month/Day, fall back to MedlineDate
            published = ""
            pub_date = article_el.find(".//PubDate")
            if pub_date is not None:
                year = (pub_date.findtext("Year") or "").strip()
                month = (pub_date.findtext("Month") or "").strip()
                day = (pub_date.findtext("Day") or "").strip()
                if year and month and day:
                    published = f"{year}-{month}-{day}"
                elif year and month:
                    published = f"{year}-{month}"
                elif year:
                    published = year
                else:
                    published = (pub_date.findtext("MedlineDate") or "").strip()

            # DOI from ArticleIdList in PubmedData
            doi = ""
            pubmed_data = article.find("PubmedData")
            if pubmed_data is not None:
                for aid in pubmed_data.findall(".//ArticleId"):
                    if aid.get("IdType") == "doi":
                        doi = (aid.text or "").strip()
                        break

            out.append({
                "pmid": pmid,
                "title": title,
                "authors": authors,
                "summary": summary,
                "published": published,
                "updated": "",
                "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else "",
                "pdf_url": "",
                "doi": doi,
            })
        except Exception:  # noqa: BLE001 — a bad article must never crash parsing
            continue
        if len(out) >= limit:
            break
    return out


async def pubmed_search(
    query: str, limit: int = 10, sort_by: str = "relevance"
) -> dict[str, Any]:
    """Search PubMed via NCBI E-utilities (keyless) and return ``{query, count, results}``.

    Uses a two-step approach: ``esearch`` returns PMIDs, ``efetch`` fetches article
    XML which is parsed by :func:`parse_pubmed_articles`.  PubMed is the authoritative
    index for biomedical, agricultural, plant-pathology, and disease-mechanism papers —
    a complement to arXiv (CS/physics-leaning) and OpenAlex (cross-discipline journal
    works).  ``sort_by='date'`` adds ``sort=pub+date`` to the esearch call.  Never
    raises — degrades to ``{error, query}`` on any failure.
    """
    q = (query or "").strip()
    if not q:
        return {"error": "query is required"}
    n = max(1, min(int(limit or 10), 50))
    esearch_params: dict[str, str] = {
        "db": "pubmed",
        "term": q,
        "retmax": str(n),
        "retmode": "json",
        "tool": _NCBI_TOOL,
        "email": _NCBI_EMAIL,
    }
    if str(sort_by).lower() in ("date", "recent", "newest"):
        esearch_params["sort"] = "pub+date"
    import httpx as _httpx  # noqa: PLC0415
    try:
        async with _httpx.AsyncClient(timeout=15.0, follow_redirects=True) as http:
            esearch_resp = await http.get(
                _PUBMED_ESEARCH, params=esearch_params,
                headers={"User-Agent": "Meridian/paper_search (research routing)"},
            )
            esearch_resp.raise_for_status()
            esearch_data = esearch_resp.json()
            id_list: list[str] = (
                esearch_data.get("esearchresult", {}).get("idlist") or []
            )
            if not id_list:
                return {"query": q, "count": 0, "results": []}
            efetch_params: dict[str, str] = {
                "db": "pubmed",
                "id": ",".join(str(i) for i in id_list),
                "rettype": "abstract",
                "retmode": "xml",
                "tool": _NCBI_TOOL,
                "email": _NCBI_EMAIL,
            }
            efetch_resp = await http.get(
                _PUBMED_EFETCH, params=efetch_params,
                headers={"User-Agent": "Meridian/paper_search (research routing)"},
            )
            efetch_resp.raise_for_status()
            results = parse_pubmed_articles(efetch_resp.text, n)
    except Exception as exc:  # noqa: BLE001 — degrade, never crash the tool call
        return {"error": f"pubmed search failed: {exc}", "query": q}
    return {"query": q, "count": len(results), "results": results}


# ---------------------------------------------------------------------------
# 9dc630de — Crossref + CORE
# ---------------------------------------------------------------------------

async def _fetch_with_backoff(
    http: Any,
    url: str,
    params: dict[str, str],
    headers: dict[str, str],
    *,
    retry_statuses: frozenset[int] | tuple[int, ...] = (429, 503),
    budget: float | None = None,
) -> Any:
    """HTTP GET with backoff on ``retry_statuses`` (default 429/503); max 3 attempts.

    Same contract as the helpers in github_search.py/social_search.py (each research
    module keeps its own copy): no delay before the first attempt, and any other HTTP
    error, or a transport error, propagates immediately so the caller degrades it without
    retrying. 454bdee5 extensions, all no-ops for existing callers:

    - ``retry_statuses`` widens the retried set (arXiv/OpenAlex pass 429 + 5xx).
    - A ``Retry-After`` header is honoured: the wait is the larger of it and the scheduled
      delay, and one above ``_MAX_RETRY_AFTER`` ends the loop at once (the server has said
      an early retry will fail; a live OpenAlex 429 asked for ~30s).
    - ``budget`` [s] stops retrying once elapsed time plus the next wait would exceed it.
    """
    import httpx as _httpx  # noqa: PLC0415 — match the handler's inline-httpx pattern
    started = time.monotonic()
    delay = 0.0
    for attempt in range(len(_RETRY_DELAYS) + 1):
        if attempt > 0:
            await asyncio.sleep(delay)
        try:
            resp = await http.get(url, params=params, headers=headers)
            resp.raise_for_status()
            return resp
        except _httpx.HTTPStatusError as exc:
            if exc.response.status_code not in retry_statuses or attempt == len(_RETRY_DELAYS):
                raise
            retry_after = _retry_after_seconds(exc.response)
            if retry_after is not None and retry_after > _MAX_RETRY_AFTER:
                raise
            delay = max(_RETRY_DELAYS[attempt], retry_after or 0.0)
            if budget is not None and time.monotonic() - started + delay > budget:
                raise
    raise AssertionError("unreachable: the last attempt returns or raises")  # pragma: no cover


def _retry_after_seconds(response: Any) -> float | None:
    """Seconds a ``Retry-After`` header asks for (delta-seconds or HTTP-date), else None."""
    headers = getattr(response, "headers", None)
    raw = headers.get("Retry-After") if headers is not None else None
    if not raw:
        return None
    raw = str(raw).strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        return None
    if when is None:  # pragma: no cover — older Pythons returned None instead of raising
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


def _http_status(exc: BaseException) -> int | None:
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return status if isinstance(status, int) else None


def _redact(text: str, secret: str) -> str:
    return text.replace(secret, "***") if secret else text


_BLOCK_TAG_RE = re.compile(r"</?(?:jats:)?(?:p|title|sec|list|list-item|br)\b[^>]*>", re.IGNORECASE)
_JATS_TITLE_RE = re.compile(r"<(?:jats:)?title\b[^>]*>.*?</(?:jats:)?title>", re.IGNORECASE | re.DOTALL)


def _strip_markup(text: Any, *, drop_titles: bool = False) -> str:
    """Remove JATS/HTML markup and collapse whitespace; non-strings degrade to ``""``.

    Crossref titles carry inline tags (``<scp>DNA</scp>``, ``H<sub>2</sub>O``) that must
    vanish without inserting spaces, while abstracts carry block tags (``<jats:p>``)
    that must become paragraph breaks. ``drop_titles`` removes an abstract's own
    ``<jats:title>Abstract</jats:title>`` heading.
    """
    if not isinstance(text, str):
        return ""
    if drop_titles:
        text = _JATS_TITLE_RE.sub(" ", text)
    text = _BLOCK_TAG_RE.sub(" ", text)
    text = _MARKUP_TAG_RE.sub("", text)
    return " ".join(text.split())


def _crossref_date(item: dict[str, Any], *keys: str) -> str:
    """First usable ``date-parts`` among ``keys`` as ``YYYY[-MM[-DD]]``, else ``""``.

    Crossref pads unknown components with ``null`` (e.g. ``[[2021, null]]``) and some
    records carry an empty ``[[null]]``; both stop at the last known component.
    """
    for key in keys:
        block = item.get(key)
        if not isinstance(block, dict):
            continue
        parts = block.get("date-parts")
        if not (isinstance(parts, list) and parts and isinstance(parts[0], list)):
            continue
        fields: list[str] = []
        for i, part in enumerate(parts[0][:3]):
            if isinstance(part, bool):
                break
            try:
                value = int(part)
            except (TypeError, ValueError):
                break
            fields.append(str(value) if i == 0 else f"{value:02d}")
        if fields:
            return "-".join(fields)
    return ""


def parse_crossref_works(payload: Any, limit: int = 10) -> list[dict[str, Any]]:
    """Parse a Crossref ``/works`` JSON payload into normalized paper dicts.

    Never raises — a malformed or empty payload degrades to ``[]``. Each result:
    ``{doi, title, authors, summary, published, updated, url, pdf_url, venue, issn,
    publisher, type, citation_count}``. ``venue`` is the first ``container-title``
    (journal or proceedings name); ``pdf_url`` is only set when Crossref lists a link
    whose content type is ``application/pdf``. Accepts the full response
    (``{"message": {"items": [...]}}``), the ``message`` object, or a bare item list.
    """
    items: Any = None
    if isinstance(payload, dict):
        message = payload.get("message")
        items = message.get("items") if isinstance(message, dict) else payload.get("items")
    elif isinstance(payload, (list, tuple)):
        items = payload
    if not isinstance(items, (list, tuple)):
        return []
    out: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        titles = item.get("title")
        title = _strip_markup(titles[0] if isinstance(titles, list) and titles else titles)
        authors: list[str] = []
        for author in item.get("author") or []:
            if not isinstance(author, dict):
                continue
            given = str(author.get("given") or "").strip()
            family = str(author.get("family") or "").strip()
            name = " ".join(p for p in (given, family) if p) or str(author.get("name") or "").strip()
            if name:
                authors.append(name)
        containers = item.get("container-title")
        venue = _strip_markup(
            containers[0] if isinstance(containers, list) and containers else containers
        )
        raw_issn = item.get("ISSN")
        issn = (
            [str(x).strip() for x in raw_issn if str(x).strip()]
            if isinstance(raw_issn, list)
            else []
        )
        doi = str(item.get("DOI") or "").strip()
        pdf_url = ""
        for link in item.get("link") or []:
            if (
                isinstance(link, dict)
                and str(link.get("content-type") or "").lower() == "application/pdf"
            ):
                pdf_url = str(link.get("URL") or "").strip()
                if pdf_url:
                    break
        citation_count = item.get("is-referenced-by-count")
        out.append({
            "doi": doi,
            "title": title,
            "authors": authors,
            "summary": _strip_markup(item.get("abstract"), drop_titles=True),
            "published": _crossref_date(
                item, "published", "issued", "published-print", "published-online"
            ),
            "updated": _crossref_date(item, "deposited"),
            "url": str(item.get("URL") or "").strip() or (f"https://doi.org/{doi}" if doi else ""),
            "pdf_url": pdf_url,
            "venue": venue,
            "issn": issn,
            "publisher": str(item.get("publisher") or "").strip(),
            "type": str(item.get("type") or "").strip(),
            "citation_count": (
                citation_count
                if isinstance(citation_count, int) and not isinstance(citation_count, bool)
                else 0
            ),
        })
        if len(out) >= limit:
            break
    return out


async def crossref_search(
    query: str, limit: int = 10, sort_by: str = "relevance"
) -> dict[str, Any]:
    """Search Crossref (keyless) and return ``{query, count, results:[...]}``.

    Rows share the ``title/authors/summary/published/updated/url/pdf_url`` base shape
    with the other sources and add ``doi``, ``venue``, ``issn``, ``publisher``, ``type``
    and ``citation_count``. ``sort_by='date'`` sorts by publication date, newest first.
    Retries 429/503 with backoff; never raises — degrades to ``{error, query}``.
    """
    q = (query or "").strip()
    if not q:
        return {"error": "query is required"}
    n = max(1, min(int(limit or 10), 50))
    params = {"query": q, "rows": str(n), "mailto": _CONTACT_EMAIL}
    if str(sort_by).lower() in ("date", "recent", "newest"):
        params["sort"] = "published"
        params["order"] = "desc"
    headers = {"User-Agent": f"Meridian/paper_search (research routing; mailto:{_CONTACT_EMAIL})"}
    import httpx as _httpx  # noqa: PLC0415 — match the handler's inline-httpx pattern
    try:
        async with _httpx.AsyncClient(timeout=15.0, follow_redirects=True) as http:
            resp = await _fetch_with_backoff(http, _CROSSREF_API, params, headers)
            results = parse_crossref_works(resp.json(), n)
    except Exception as exc:  # noqa: BLE001 — degrade, never crash the tool call
        return {"error": f"crossref search failed: {exc}", "query": q}
    return {"query": q, "count": len(results), "results": results}


def parse_core_works(payload: Any, limit: int = 10) -> list[dict[str, Any]]:
    """Parse a CORE v3 ``/search/works`` JSON payload into normalized paper dicts.

    Never raises — a malformed or empty payload degrades to ``[]``. Each result:
    ``{core_id, title, authors, summary, published, updated, url, pdf_url, doi, venue,
    publisher, has_full_text}``. CORE's ``fullText`` field can be an entire paper, so it
    is deliberately NOT copied into the result; ``has_full_text`` says whether one
    exists and ``pdf_url`` (CORE's ``downloadUrl``) is where to fetch it.
    """
    works = payload.get("results") if isinstance(payload, dict) else payload
    if not isinstance(works, (list, tuple)):
        return []
    out: list[dict[str, Any]] = []
    for work in works:
        if not isinstance(work, dict):
            continue
        core_id = str(work.get("id") or "").strip()
        authors = [
            " ".join(str(a.get("name") or "").split())
            for a in (work.get("authors") or [])
            if isinstance(a, dict) and str(a.get("name") or "").strip()
        ]
        published = str(work.get("publishedDate") or "").strip()[:10]
        if not published and work.get("yearPublished"):
            published = str(work.get("yearPublished")).strip()
        display_url = ""
        for link in work.get("links") or []:
            if isinstance(link, dict) and link.get("type") == "display":
                display_url = str(link.get("url") or "").strip()
                if display_url:
                    break
        venue = ""
        for journal in work.get("journals") or []:
            if isinstance(journal, dict) and str(journal.get("title") or "").strip():
                venue = " ".join(str(journal["title"]).split())
                break
        full_text = work.get("fullText")
        out.append({
            "core_id": core_id,
            "title": " ".join(str(work.get("title") or "").split()),
            "authors": authors,
            "summary": " ".join(str(work.get("abstract") or "").split()),
            "published": published,
            "updated": str(work.get("updatedDate") or "").strip()[:10],
            "url": display_url or (f"https://core.ac.uk/works/{core_id}" if core_id else ""),
            "pdf_url": str(work.get("downloadUrl") or "").strip(),
            "doi": str(work.get("doi") or "").strip(),
            "venue": venue,
            "publisher": str(work.get("publisher") or "").strip(),
            "has_full_text": isinstance(full_text, str) and bool(full_text.strip()),
        })
        if len(out) >= limit:
            break
    return out


async def core_search(
    query: str, limit: int = 10, sort_by: str = "relevance", api_key: str | None = None
) -> dict[str, Any]:
    """Search CORE's open-access corpus and return ``{query, count, results:[...]}``.

    Needs an API key (free registration): ``api_key`` if given, else the
    ``CORE_API_KEY`` environment variable. With no key it returns an explicit error
    WITHOUT making a request — never a silent fallback to another source. The key is
    sent only as an ``Authorization: Bearer`` header (never a query parameter, which
    would leak it into exception messages) and is redacted from any error text.
    ``sort_by`` is accepted for API-shape consistency but not applied: CORE's date-sort
    parameter could not be verified without a key, so results are always by relevance.
    Retries 429/503 with backoff; never raises — degrades to ``{error, query}``.
    """
    q = (query or "").strip()
    if not q:
        return {"error": "query is required"}
    key = (api_key if api_key is not None else os.environ.get("CORE_API_KEY", "")).strip()
    if not key:
        return {
            "error": (
                "CORE requires an API key: set CORE_API_KEY (free registration at "
                "https://core.ac.uk/services/api). Unauthenticated requests are rate-"
                "limited to failure (HTTP 429)."
            ),
            "query": q,
        }
    n = max(1, min(int(limit or 10), 50))
    headers = {
        "Authorization": f"Bearer {key}",
        "User-Agent": f"Meridian/paper_search (research routing; mailto:{_CONTACT_EMAIL})",
    }
    import httpx as _httpx  # noqa: PLC0415 — match the handler's inline-httpx pattern
    try:
        async with _httpx.AsyncClient(timeout=20.0, follow_redirects=True) as http:
            resp = await _fetch_with_backoff(http, _CORE_API, {"q": q, "limit": str(n)}, headers)
            results = parse_core_works(resp.json(), n)
    except Exception as exc:  # noqa: BLE001 — degrade, never crash the tool call
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status in (401, 403):
            message = f"CORE rejected the API key (HTTP {status}) -- check CORE_API_KEY"
        else:
            message = f"core search failed: {exc}"
        return {"error": message.replace(key, "***"), "query": q}
    return {"query": q, "count": len(results), "results": results}


# ---------------------------------------------------------------------------
# 454bdee5 — arXiv fallback for when arXiv refuses or cannot be reached
# ---------------------------------------------------------------------------

# An arXiv identifier, optionally versioned: new style YYMM.NNNN[N] (2007 on) or old style
# archive[.SC]/YYMMNNN (e.g. hep-th/9901001, math.GT/0309136).
_ARXIV_ID = r"(?:\d{4}\.\d{4,5}|[a-z][a-z\-]*(?:\.[a-z]{2})?/\d{7})(?:v\d+)?(?!\d)"
# Where an arXiv id shows up in OpenAlex data, most faithful first: the OAI-PMH location
# id ("pmh:oai:arXiv.org:<id>", original case), an arxiv.org abs/pdf URL (a pdf URL may
# end in ".pdf", which the id pattern stops before), then arXiv's DataCite DOI
# ("10.48550/arxiv.<id>"; OpenAlex lowercases DOIs, so this one comes last).
_ARXIV_ID_PATTERNS = (
    re.compile(rf"oai:arxiv\.org:({_ARXIV_ID})", re.IGNORECASE),
    re.compile(rf"arxiv\.org/(?:abs|pdf)/({_ARXIV_ID})", re.IGNORECASE),
    re.compile(rf"10\.48550/arxiv\.({_ARXIV_ID})", re.IGNORECASE),
)


def _arxiv_row(
    arxiv_id: str, title: str, authors: list[str], summary: str, published: str
) -> dict[str, Any]:
    """A fallback row in exactly the :func:`parse_arxiv_atom` shape.

    ``url``/``pdf_url`` are rebuilt from the id so they always point at arXiv itself.
    ``updated`` stays blank: arXiv's "updated" is the latest version's date, and no
    fallback source carries it (OpenAlex's ``updated_date`` is when OpenAlex last touched
    its own record, which would be actively misleading here).
    """
    return {
        "arxiv_id": arxiv_id,
        "title": title,
        "authors": authors,
        "summary": summary,
        "published": published,
        "updated": "",
        "url": f"https://arxiv.org/abs/{arxiv_id}",
        "pdf_url": f"https://arxiv.org/pdf/{arxiv_id}",
    }


def _arxiv_id_from_openalex_work(work: dict[str, Any]) -> str:
    """The arXiv id of an OpenAlex work, or ``""`` when none can be found.

    A preprint-only work carries it in its DOI and primary location. A work that was also
    published elsewhere is a single merged OpenAlex record whose ``doi`` is the journal's,
    so the arXiv id is only in one of its ``locations`` — hence every location is checked.
    """
    candidates: list[Any] = []
    primary = work.get("primary_location")
    locations = work.get("locations")
    for loc in [primary, *(locations if isinstance(locations, (list, tuple)) else [])]:
        if isinstance(loc, dict):
            candidates.extend(loc.get(key) for key in ("id", "landing_page_url", "pdf_url"))
    candidates.append(work.get("doi"))
    ids = work.get("ids")
    if isinstance(ids, dict):
        candidates.append(ids.get("doi"))
    for pattern in _ARXIV_ID_PATTERNS:
        for candidate in candidates:
            if isinstance(candidate, str):
                match = pattern.search(candidate)
                if match:
                    return match.group(1)
    return ""


def parse_openalex_arxiv_works(payload: Any, limit: int = 10) -> list[dict[str, Any]]:
    """Parse an OpenAlex ``/works`` payload into arXiv-shaped rows (see :func:`_arxiv_row`).

    Works with no recoverable arXiv id are dropped, and so are repeats of an id already
    returned (OpenAlex occasionally keeps a preprint and its published version as two
    works). Never raises; a malformed payload degrades to ``[]``.
    """
    works = payload.get("results") if isinstance(payload, dict) else payload
    if not isinstance(works, (list, tuple)):
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for work in works:
        if not isinstance(work, dict):
            continue
        arxiv_id = _arxiv_id_from_openalex_work(work)
        if not arxiv_id or arxiv_id in seen:
            continue
        seen.add(arxiv_id)
        base = parse_openalex_works([work], 1)[0]
        out.append(_arxiv_row(
            arxiv_id, base["title"], base["authors"], base["summary"], base["published"],
        ))
        if len(out) >= limit:
            break
    return out


def parse_semantic_scholar_arxiv_papers(payload: Any, limit: int = 10) -> list[dict[str, Any]]:
    """Parse a Semantic Scholar ``/paper/search`` payload into arXiv-shaped rows.

    Only papers whose ``externalIds`` carry an ``ArXiv`` id are kept. Never raises; a
    malformed payload degrades to ``[]``.
    """
    papers = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(papers, (list, tuple)):
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for paper in papers:
        if not isinstance(paper, dict):
            continue
        external_ids = paper.get("externalIds")
        arxiv_id = (
            str(external_ids.get("ArXiv") or "").strip() if isinstance(external_ids, dict) else ""
        )
        if not arxiv_id or arxiv_id in seen:
            continue
        seen.add(arxiv_id)
        authors = [
            str(a.get("name") or "").strip()
            for a in (paper.get("authors") or [])
            if isinstance(a, dict) and str(a.get("name") or "").strip()
        ]
        year = paper.get("year")
        published = str(paper.get("publicationDate") or "").strip() or (
            str(year) if isinstance(year, int) and not isinstance(year, bool) else ""
        )
        out.append(_arxiv_row(
            arxiv_id,
            " ".join(str(paper.get("title") or "").split()),
            authors,
            " ".join(str(paper.get("abstract") or "").split()),
            published,
        ))
        if len(out) >= limit:
            break
    return out


async def _openalex_arxiv_rows(q: str, n: int, sort_by: Any) -> list[dict[str, Any]]:
    """OpenAlex search restricted to works with an arXiv location; raises on failure.

    ``locations.source.id`` rather than ``primary_location.source.id``: the latter only
    matches preprint-only works and misses every arXiv paper that was later published
    (verified live 2026-09-27: 23,829 such works for 2023 alone).
    """
    payload = await _openalex_get_works(
        q, n, sort_by,
        api_key=_openalex_api_key(),
        filter_expr=f"locations.source.id:{_OPENALEX_ARXIV_SOURCE}",
    )
    return parse_openalex_arxiv_works(payload, n)


async def _semantic_scholar_arxiv_rows(q: str, n: int, sort_by: Any) -> list[dict[str, Any]]:
    """Semantic Scholar search keeping only papers with an arXiv id; raises on failure.

    Asks for twice ``n`` (S2's cap is 100) because non-arXiv papers are dropped after the
    fact: ``/paper/search`` has no arXiv filter. S2 has no date sort, so ``sort_by`` is
    accepted for signature parity only.
    """
    params = {
        "query": q,
        "limit": str(min(100, n * 2)),
        "fields": "title,authors,abstract,year,publicationDate,externalIds",
    }
    import httpx as _httpx  # noqa: PLC0415 — match the handler's inline-httpx pattern
    async with _httpx.AsyncClient(timeout=15.0, follow_redirects=True) as http:
        resp = await _fetch_with_backoff(
            http, _S2_PAPER_API, params, {"User-Agent": _POLITE_USER_AGENT},
            retry_statuses=_TRANSIENT_STATUSES,
        )
        return parse_semantic_scholar_arxiv_papers(resp.json(), n)


def _describe_failure(exc: BaseException) -> str:
    """One line for an error message: ``HTTP 406 Not Acceptable`` or ``ConnectError: ...``."""
    status = _http_status(exc)
    if status is not None:
        phrase = str(getattr(getattr(exc, "response", None), "reason_phrase", "") or "")
        return f"HTTP {status} {phrase}".strip()
    text = " ".join(str(exc).split())
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def _arxiv_unreachable_reason(exc: BaseException) -> str | None:
    """Why arXiv could not answer, when that warrants a fallback; ``None`` otherwise.

    Fallback-worthy: a refusal (403/406), a status that outlasted the backoff (429/5xx),
    or any request-level failure (connect/read timeout, DNS, TLS, redirect loop). Anything
    else — a 400, or a bug — keeps the pre-454bdee5 behavior of an error with no fallback,
    so a malformed request is never masked by another source's results.
    """
    import httpx as _httpx  # noqa: PLC0415 — match the handler's inline-httpx pattern
    if isinstance(exc, _httpx.HTTPStatusError):
        status = exc.response.status_code
        if status in _ARXIV_REFUSED_STATUSES or status in _TRANSIENT_STATUSES or status >= 500:
            return _describe_failure(exc)
        return None
    if isinstance(exc, _httpx.RequestError):
        return _describe_failure(exc)
    return None


async def _arxiv_fallback_search(
    q: str, n: int, sort_by: Any, arxiv_reason: str
) -> dict[str, Any]:
    """Answer an arXiv query from OpenAlex, then Semantic Scholar; never raises.

    The first source that answers wins, even with zero rows (an empty answer is still an
    answer). Rows keep the arXiv shape, including ``arxiv_id``, which research watchlists
    use as the dedup key. When every source fails, the result is the usual
    ``{error, query}`` with ``sources_tried`` added.
    """
    fallbacks = (
        ("openalex", "OpenAlex", _openalex_arxiv_rows),
        ("semantic_scholar", "Semantic Scholar", _semantic_scholar_arxiv_rows),
    )
    tried = ["arxiv"]
    failures: list[str] = []
    for name, label, fetch in fallbacks:
        tried.append(name)
        try:
            rows = await fetch(q, n, sort_by)
        except Exception as exc:  # noqa: BLE001 — degrade, never crash the tool call
            detail = _describe_failure(exc)
            if name == "openalex" and _http_status(exc) == 429 and not _openalex_api_key():
                detail += " (anonymous budget; set OPENALEX_API_KEY)"
            failures.append(f"{name}: {_redact(detail, _openalex_api_key())}")
            continue
        warning = (
            f"arXiv was unreachable from this server ({arxiv_reason}), so these results "
            f"come from {label}'s index of arXiv papers: ranking and coverage differ from "
            "arXiv's own search, the newest submissions may be missing, and 'updated' is "
            "blank."
        )
        if name == "semantic_scholar" and _wants_date_sort(sort_by):
            warning += " Semantic Scholar cannot sort by date, so they are in relevance order."
        if failures:
            warning += " Also unavailable: " + "; ".join(failures) + "."
        return {
            "query": q,
            "count": len(rows),
            "results": rows,
            "fallback_source": name,
            "warning": warning,
            "sources_tried": tried,
        }
    return {
        "error": (
            f"arxiv search failed: arXiv was unreachable from this server ({arxiv_reason}) "
            f"and every fallback failed too ({'; '.join(failures)})"
        ),
        "query": q,
        "sources_tried": tried,
    }
