"""6d1abc98 — GitHub search submodule, keyless (GitHub's public REST search API).

811881c6/f65f6111/d58000c6 (see :mod:`meridian.paper_search` / :mod:`meridian.social_search`)
established a per-source function pattern for the Research Module: a
``<source>_search`` async network call plus a pure, never-raising
``parse_<source>_*`` helper that is fully unit-testable without hitting the
network, both normalized to the same ``{query, count, results}`` top-level shape.

This module is the ``github_search`` sibling: external prior-art / competitive-repo
research, distinct from ``search_code`` (which only searches the CALLING project's
own connected repo, not the wider GitHub ecosystem). Two keyless endpoints, mirroring
paper_search's two-source shape:

- ``github_code_search`` — GitHub's Code Search API (``/search/code``): finds actual
  usage of a symbol/pattern/API across public repos.
- ``github_repo_search`` — GitHub's Repository Search API (``/search/repositories``):
  finds competitor/prior-art repositories by topic/description/stars.

Both are keyless (no PAT required for public-repo search) — matching the exact
keyless constraint paper_search.py/social_search.py are built around. GitHub rate-
limits unauthenticated search requests fairly aggressively; no explicit backoff/
retry is implemented here, matching the existing keyless-family convention of
degrading to ``{error}`` on any exception rather than retrying.
"""
from __future__ import annotations

from typing import Any

_GITHUB_CODE_API = "https://api.github.com/search/code"
_GITHUB_REPO_API = "https://api.github.com/search/repositories"
_ACCEPT_HEADER = "application/vnd.github+json"


def parse_github_code_items(payload: Any, limit: int = 10) -> list[dict[str, Any]]:
    """Parse a GitHub ``/search/code`` JSON payload into a list of result dicts.

    Never raises — a malformed or empty payload degrades to ``[]``. Each result:
    ``{path, repo, authors, summary, published, updated, url, sha, score}`` — the
    same overall shape (title/authors/summary/published/url — here ``path`` stands
    in for ``title`` since a code hit is a file, not a titled document) that
    :func:`meridian.paper_search.parse_arxiv_atom` and
    :func:`meridian.social_search.parse_hn_hits` return.

    ``payload`` is the decoded JSON object (``{"items": [...]}``); passing the raw
    list of items is also accepted.
    """
    if isinstance(payload, dict):
        items = payload.get("items")
    else:
        items = payload
    if not isinstance(items, (list, tuple)):
        return []
    out: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or item.get("name") or "").strip()
        repository = item.get("repository")
        repository = repository if isinstance(repository, dict) else {}
        repo_full_name = str(repository.get("full_name") or "").strip()
        owner = repository.get("owner")
        owner_login = ""
        if isinstance(owner, dict):
            owner_login = str(owner.get("login") or "").strip()
        elif repo_full_name:
            owner_login = repo_full_name.split("/", 1)[0]
        html_url = str(item.get("html_url") or "").strip()
        sha = str(item.get("sha") or "").strip()
        score = item.get("score")
        score = score if isinstance(score, (int, float)) else 0
        out.append({
            "path": path,
            "title": path,
            "repo": repo_full_name,
            "authors": [owner_login] if owner_login else [],
            "summary": "",
            "published": "",
            "updated": "",
            "url": html_url,
            "sha": sha,
            "score": score,
        })
        if len(out) >= limit:
            break
    return out


def parse_github_repo_items(payload: Any, limit: int = 10) -> list[dict[str, Any]]:
    """Parse a GitHub ``/search/repositories`` JSON payload into a list of result
    dicts, normalized to the SAME overall shape as :func:`parse_github_code_items`.

    Never raises — a malformed or empty payload degrades to ``[]``. Each result:
    ``{title, authors, summary, published, updated, url, repo, stars, forks,
    language, score}``.

    ``payload`` is the decoded JSON object (``{"items": [...]}``); passing the raw
    list of items is also accepted.
    """
    if isinstance(payload, dict):
        items = payload.get("items")
    else:
        items = payload
    if not isinstance(items, (list, tuple)):
        return []
    out: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        full_name = str(item.get("full_name") or item.get("name") or "").strip()
        owner = item.get("owner")
        owner_login = ""
        if isinstance(owner, dict):
            owner_login = str(owner.get("login") or "").strip()
        elif full_name:
            owner_login = full_name.split("/", 1)[0]
        description = " ".join(str(item.get("description") or "").split())
        html_url = str(item.get("html_url") or "").strip()
        stars = item.get("stargazers_count")
        stars = stars if isinstance(stars, int) else 0
        forks = item.get("forks_count")
        forks = forks if isinstance(forks, int) else 0
        language = str(item.get("language") or "").strip() if item.get("language") else ""
        score = item.get("score")
        score = score if isinstance(score, (int, float)) else 0
        out.append({
            "title": full_name,
            "repo": full_name,
            "authors": [owner_login] if owner_login else [],
            "summary": description,
            "published": str(item.get("created_at") or "").strip(),
            "updated": str(item.get("updated_at") or item.get("pushed_at") or "").strip(),
            "url": html_url,
            "stars": stars,
            "forks": forks,
            "language": language,
            "score": score,
        })
        if len(out) >= limit:
            break
    return out


async def github_code_search(
    query: str, limit: int = 10, sort_by: str = "relevance"
) -> dict[str, Any]:
    """Search GitHub code (keyless, via the public Code Search API) and return
    ``{query, count, results:[...]}`` — the same top-level shape as
    :func:`meridian.paper_search.arxiv_search`/``openalex_search`` and
    :func:`meridian.social_search.hn_search`.

    ``sort_by``: ``'relevance'`` (default, GitHub's best-match ranking) or
    ``'date'`` (most-recently-indexed first — ``sort=indexed``, the only
    alternate sort the Code Search API supports).
    Never raises — an empty query returns ``{error}`` and any network/parse
    failure degrades to ``{error, query}`` so a research call can't crash the
    MCP handler.
    """
    q = (query or "").strip()
    if not q:
        return {"error": "query is required"}
    n = max(1, min(int(limit or 10), 50))
    params = {"q": q, "per_page": str(n)}
    if str(sort_by).lower() in ("date", "recent", "newest"):
        params["sort"] = "indexed"
        params["order"] = "desc"
    import httpx as _httpx  # noqa: PLC0415 — match the handler's inline-httpx pattern
    try:
        async with _httpx.AsyncClient(timeout=15.0) as http:
            resp = await http.get(
                _GITHUB_CODE_API, params=params,
                headers={
                    "User-Agent": "Meridian/github_search (research routing)",
                    "Accept": _ACCEPT_HEADER,
                },
            )
            resp.raise_for_status()
            results = parse_github_code_items(resp.json(), n)
    except Exception as exc:  # noqa: BLE001 — degrade, never crash the tool call
        return {"error": f"github code search failed: {exc}", "query": q}
    return {"query": q, "count": len(results), "results": results}


async def github_repo_search(
    query: str, limit: int = 10, sort_by: str = "relevance"
) -> dict[str, Any]:
    """Search GitHub repositories (keyless, via the public Repository Search API)
    and return ``{query, count, results:[...]}`` in the SAME shape as
    :func:`github_code_search`.

    ``sort_by``: ``'relevance'`` (default, GitHub's best-match ranking) or
    ``'date'`` (most-recently-updated first — ``sort=updated``).
    Never raises — an empty query returns ``{error}`` and any network/parse
    failure degrades to ``{error, query}`` so a research call can't crash the
    MCP handler. Mirrors ``github_code_search`` exactly (keyless, best-effort,
    non-raising).
    """
    q = (query or "").strip()
    if not q:
        return {"error": "query is required"}
    n = max(1, min(int(limit or 10), 50))
    params = {"q": q, "per_page": str(n)}
    if str(sort_by).lower() in ("date", "recent", "newest"):
        params["sort"] = "updated"
        params["order"] = "desc"
    import httpx as _httpx  # noqa: PLC0415 — match the handler's inline-httpx pattern
    try:
        async with _httpx.AsyncClient(timeout=15.0) as http:
            resp = await http.get(
                _GITHUB_REPO_API, params=params,
                headers={
                    "User-Agent": "Meridian/github_search (research routing)",
                    "Accept": _ACCEPT_HEADER,
                },
            )
            resp.raise_for_status()
            results = parse_github_repo_items(resp.json(), n)
    except Exception as exc:  # noqa: BLE001 — degrade, never crash the tool call
        return {"error": f"github repo search failed: {exc}", "query": q}
    return {"query": q, "count": len(results), "results": results}
