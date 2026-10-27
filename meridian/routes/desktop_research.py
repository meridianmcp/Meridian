"""Desktop Search tab: read-only research search over the local server.

``GET /desktop/research/search?q=<text>&sources=<comma list>&limit=<int>``

Served only to loopback clients (127.0.0.1 / ::1). One query fans out to the existing
search functions in :mod:`meridian.paper_search` and :mod:`meridian.github_search`; each
source's answer is normalized to one result shape and returned on its own, so a source
that fails never hides the others.

Read-only: nothing is written to the database and nothing is captured. Every ``reason``
is a fixed plain sentence. Raw exception text, upstream URLs and credentials are never
echoed back, and a source URL that carries credentials or a key parameter is dropped.
"""
from __future__ import annotations

import asyncio
import ipaddress
import re
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from .. import github_search as _github
from .. import paper_search as _papers

router = APIRouter()

DEFAULT_SOURCES: tuple[str, ...] = ("openalex", "semantic_scholar", "arxiv", "github_repos")
KNOWN_SOURCES: tuple[str, ...] = DEFAULT_SOURCES + ("github_code",)
MAX_QUERY_CHARS = 300
DEFAULT_LIMIT = 10
MAX_LIMIT = 25
MAX_SOURCES_PER_REQUEST = 10
MAX_ABSTRACT_CHARS = 400
MAX_AUTHORS = 50
# Upper bound on one source, on top of the per-call retry budgets inside the search
# functions, so one slow upstream cannot hold the whole tab open.
SOURCE_TIMEOUT_SECONDS = 45.0

_LABELS: dict[str, str] = {
    "openalex": "OpenAlex",
    "semantic_scholar": "Semantic Scholar",
    "arxiv": "arXiv",
    "github_repos": "GitHub",
    "github_code": "GitHub",
}
# Identifier fields per source, first non-empty wins (the result ``id``).
_ID_FIELDS: dict[str, tuple[str, ...]] = {
    "openalex": ("openalex_id", "doi", "url"),
    "semantic_scholar": ("s2_id", "doi", "url"),
    "arxiv": ("arxiv_id", "url"),
    "github_repos": ("repo", "title"),
    "github_code": ("url", "sha"),
}
# A request that passed through a proxy or forwarder is not a direct local call.
_FORWARDED_HEADERS = ("forwarded", "x-forwarded-for", "x-forwarded-host", "x-real-ip")
_YEAR_RE = re.compile(r"^\s*((?:19|20)\d{2})(?!\d)")
_SECRET_PARAM_RE = re.compile(
    r"(?i)(?:^|[&;])(?:api[_-]?key|apikey|key|token|access[_-]?token|secret|auth)="
)
_URL_RE = re.compile(r"https?://\S+")


def _is_loopback_host(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_loopback


async def _require_loopback(request: Request) -> None:
    """Refuse anything that did not reach this process directly from the same machine."""
    client = request.client
    if (
        client is None
        or not _is_loopback_host(client.host)
        or any(name in request.headers for name in _FORWARDED_HEADERS)
    ):
        raise HTTPException(
            status_code=403,
            detail="The desktop research search is only available from this machine.",
        )


def _checked_query(raw: str | None) -> str:
    if raw is None or len(raw) > MAX_QUERY_CHARS or not raw.strip():
        raise HTTPException(
            status_code=422,
            detail=f"q is required: 1 to {MAX_QUERY_CHARS} characters.",
        )
    return raw.strip()


def _requested_sources(raw: str | None) -> list[str]:
    names: list[str] = []
    for part in (raw or "").split(","):
        name = part.strip().lower()[:64]
        if name and name not in names:
            names.append(name)
        if len(names) >= MAX_SOURCES_PER_REQUEST:
            break
    return names or list(DEFAULT_SOURCES)


def _capped_limit(raw: int) -> int:
    return max(1, min(int(raw), MAX_LIMIT))


def _text(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split())


def _abstract(value: Any) -> str:
    text = _text(value)
    if len(text) <= MAX_ABSTRACT_CHARS:
        return text
    return text[: MAX_ABSTRACT_CHARS - 3].rstrip() + "..."


def _year(value: Any) -> int | None:
    match = _YEAR_RE.match(str(value or ""))
    return int(match.group(1)) if match else None


def _authors(values: Any) -> list[str]:
    if not isinstance(values, (list, tuple)):
        return []
    names = [_text(value) for value in values]
    return [name for name in names if name][:MAX_AUTHORS]


def _safe_url(value: Any) -> str:
    """An https URL with no userinfo and no key-like query parameter, else ``""``.

    Plain http links are upgraded to https (the contract's ``url`` is https). Anything
    with credentials, or a key/token/secret query parameter, is dropped entirely.
    """
    try:
        parts = urlsplit(str(value or "").strip())
        host = (parts.hostname or "").lower()
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https") or not host:
        return ""
    if parts.username is not None or parts.password is not None:
        return ""
    if _SECRET_PARAM_RE.search(parts.query):
        return ""
    if parts.scheme == "http":
        parts = parts._replace(scheme="https")
    return parts.geturl()


def _normalize(source: str, row: Any) -> dict[str, Any] | None:
    """One upstream row in the contract shape, or ``None`` when it has no id and no title."""
    if not isinstance(row, dict):
        return None
    if source == "github_code":
        repo = _text(row.get("repo"))
        path = _text(row.get("path") or row.get("title"))
        title = f"{repo}: {path}" if repo and path else (path or repo)
    else:
        title = _text(row.get("title"))
    ident = ""
    for field in _ID_FIELDS[source]:
        ident = _text(row.get(field))
        if ident:
            break
    if not ident and not title:
        return None
    return {
        "id": ident,
        "title": title,
        "authors": _authors(row.get("authors")),
        "year": _year(row.get("published")),
        "url": _safe_url(row.get("url")),
        "source": source,
        "abstract": _abstract(row.get("summary")),
    }


def _failed(reason: str) -> dict[str, Any]:
    return {"ok": False, "reason": reason, "results": []}


def _plain_reason(source: str, detail: str) -> str:
    """Map an upstream error message to one fixed sentence. The message is never echoed."""
    label = _LABELS[source]
    text = _URL_RE.sub(" ", detail).lower()
    if "timed out" in text or "timeout" in text or "latency budget" in text:
        return f"{label} did not answer in time; try again shortly"
    if (
        re.search(r"\b429\b", text)
        or "rate limit" in text
        or "too many requests" in text
        or "budget" in text
    ):
        if source.startswith("github"):
            return "GitHub rate limit reached for public search; try again in a minute"
        return f"{label} rate limit reached; try again in a minute"
    if source.startswith("github") and re.search(r"\b403\b", text):
        return "GitHub rate limit reached for public search; try again in a minute"
    if re.search(r"\b40[36]\b", text):
        return f"{label} refused the request from this server"
    return f"{label} search failed; the source did not answer"


def _from_payload(source: str, payload: Any, limit: int) -> dict[str, Any]:
    label = _LABELS[source]
    if not isinstance(payload, dict):
        return _failed(f"{label} search failed; the source did not answer")
    if payload.get("error"):
        return _failed(_plain_reason(source, str(payload.get("error"))))
    rows = payload.get("results")
    if not isinstance(rows, list):
        return _failed(f"{label} search failed; the source did not answer")
    results: list[dict[str, Any]] = []
    for row in rows:
        try:
            item = _normalize(source, row)
        except Exception:  # noqa: BLE001 — one malformed row must not sink the source
            item = None
        if item is not None:
            results.append(item)
        if len(results) >= limit:
            break
    reason = ""
    fallback = payload.get("fallback_source")
    if fallback:
        index = _LABELS.get(str(fallback), "another index")
        reason = (
            "arXiv was unreachable from this server; these results come from "
            f"{index}'s index of arXiv papers"
        )
    return {"ok": True, "reason": reason, "results": results}


async def _call_source(source: str, query: str, limit: int) -> Any:
    if source == "openalex":
        return await _papers.openalex_search(query, limit)
    if source == "semantic_scholar":
        return await _papers.semantic_scholar_search(query, limit)
    if source == "arxiv":
        return await _papers.arxiv_search(query, limit)
    if source == "github_repos":
        return await _github.github_repo_search(query, limit)
    return await _github.github_code_search(query, limit)


async def _run_source(source: str, query: str, limit: int) -> dict[str, Any]:
    if source not in KNOWN_SOURCES:
        return _failed("unknown source")
    try:
        payload = await asyncio.wait_for(
            _call_source(source, query, limit), timeout=SOURCE_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        return _failed(f"{_LABELS[source]} did not answer in time; try again shortly")
    except Exception:  # noqa: BLE001 — a failing source must never take the others down
        return _failed(f"{_LABELS[source]} search failed; the source did not answer")
    try:
        return _from_payload(source, payload, limit)
    except Exception:  # noqa: BLE001 — never let a normalization bug leak exception text
        return _failed(f"{_LABELS[source]} search failed; the source did not answer")


@router.get(
    "/desktop/research/search",
    dependencies=[Depends(_require_loopback)],
)
async def desktop_research_search(
    q: str | None = Query(default=None, description="Search text, 1 to 300 characters."),
    sources: str | None = Query(
        default=None,
        description=(
            "Comma list of openalex, semantic_scholar, arxiv, github_repos, github_code. "
            "Default: openalex,semantic_scholar,arxiv,github_repos."
        ),
    ),
    limit: int = Query(default=DEFAULT_LIMIT, description="Results per source, 1 to 25."),
) -> dict[str, Any]:
    """Desktop Search tab: read-only research search across the named sources (loopback only)."""
    query = _checked_query(q)
    names = _requested_sources(sources)
    per_source = _capped_limit(limit)
    answers = await asyncio.gather(*(_run_source(name, query, per_source) for name in names))
    return {"query": query, "sources": dict(zip(names, answers))}
