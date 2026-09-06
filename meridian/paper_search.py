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
        # 995e27a5 — follow_redirects so a future http->https (or mirror) 301 is
        # honoured instead of silently parsing a redirect body to zero results.
        async with _httpx.AsyncClient(timeout=15.0, follow_redirects=True) as http:
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
