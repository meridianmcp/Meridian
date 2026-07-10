"""Internet Archive helpers for the "web" pointer source_type (1d3f6e71).

A ``text_quote`` pointer at a URL is only as durable as the URL. This module adds
link-rot / content-drift resilience:

* :func:`save_page_now` — archive a URL via the Internet Archive Save-Page-Now
  API at CITATION TIME, returning the created snapshot URL.
* :func:`wayback_latest_url` — the deterministic "latest snapshot" Wayback URL for
  a page (no network); the fallback archive reference when SPN is unavailable.
* :func:`default_web_fetcher` — fetch a page's text for the drift check (the
  default ``web_fetcher`` seam of :func:`meridian.pointers.resolve_pointer`).

Everything here is BEST-EFFORT and never raises: archiving or fetching the open
web must never break pointer creation or resolution. The HTTP seams are injectable
so tests never touch the network.

06df6ab3 — :func:`default_web_fetcher` ALSO anchors ``text_quote`` against docx
paragraph text: a ``uri`` that is a local ``.docx`` path (not an ``http(s)://``
URL) is read via :func:`meridian.docs_intel.parse_docx` instead of an HTTP GET,
so the SAME ``text_quote`` selector (``exact``/``prefix``/``suffix``, drift
detection) works across web/docs/code sources with no new selector type — see
``meridian/pointers.py`` for the resolver this feeds.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

_log = logging.getLogger(__name__)

_SPN_ENDPOINT = "https://web.archive.org/save/"
_WAYBACK_PREFIX = "https://web.archive.org/web/"
_DEFAULT_TIMEOUT = 15.0


def wayback_latest_url(url: str) -> str:
    """Deterministic Wayback "latest snapshot" URL for ``url`` (no network).

    ``https://web.archive.org/web/2/<url>`` redirects to the most recent capture —
    the archive reference stored when Save-Page-Now is unavailable or times out.
    """
    return f"{_WAYBACK_PREFIX}2/{url}"


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _snapshot_url_from_headers(resp: Any) -> str | None:
    """Pull SPN's canonical capture path from the response's Content-Location."""
    try:
        headers = getattr(resp, "headers", {}) or {}
        loc = headers.get("Content-Location") or headers.get("content-location")
    except Exception:  # noqa: BLE001
        return None
    if not loc:
        return None
    return ("https://web.archive.org" + loc) if loc.startswith("/") else loc


async def save_page_now(
    url: str,
    *,
    http_post: Callable[..., Awaitable[Any]] | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Archive ``url`` via Internet Archive Save-Page-Now (best-effort, never raises).

    Returns ``{"archived_url", "archived_at"}`` on success, else ``{"error": ...}``.
    The snapshot URL is taken from the response's ``Content-Location`` header (SPN's
    canonical capture path), falling back to :func:`wayback_latest_url`. ``http_post``
    is injectable for tests; the default uses httpx with ``timeout``.
    """
    if not url or not isinstance(url, str):
        return {"error": "no url"}
    try:
        if http_post is None:
            import httpx  # noqa: PLC0415

            async with httpx.AsyncClient(
                timeout=timeout, follow_redirects=True
            ) as client:
                resp = await client.post(_SPN_ENDPOINT + url)
        else:
            resp = await http_post(_SPN_ENDPOINT + url)
    except Exception as exc:  # noqa: BLE001 — the open web is unreliable; never raise
        _log.debug("save_page_now failed for %r", url, exc_info=True)
        return {"error": f"archive request failed: {exc}"}

    archived = _snapshot_url_from_headers(resp) or wayback_latest_url(url)
    return {"archived_url": archived, "archived_at": _now_iso()}


def _looks_like_local_docx(uri: str) -> bool:
    """True for a local ``.docx`` path (NOT an ``http(s)://`` URL) — 06df6ab3."""
    if not isinstance(uri, str) or not uri.strip():
        return False
    lowered = uri.strip().lower()
    if lowered.startswith(("http://", "https://")):
        return False
    return lowered.endswith(".docx")


def _docx_paragraph_text(path: str) -> str | None:
    """Concatenated paragraph text of a local ``.docx``, newline-joined.

    The docx counterpart of an HTTP GET for :func:`default_web_fetcher`'s
    ``text_quote`` anchor check (06df6ab3): reads every paragraph via
    :func:`meridian.docs_intel.parse_docx` (stdlib-only, no PDF round-trip) and
    joins them so ``exact``/``prefix``/``suffix`` matching works the same way it
    does for a fetched web page's body text. Best-effort — returns ``None`` on
    any failure (missing file, bad zip, ...), never raises.
    """
    try:
        from .docs_intel import parse_docx  # noqa: PLC0415 — optional/lazy
    except Exception:  # noqa: BLE001
        return None
    try:
        paragraphs = parse_docx(path)
    except Exception:  # noqa: BLE001
        _log.debug("docx paragraph fetch failed for %r", path, exc_info=True)
        return None
    return "\n".join(p.get("text", "") for p in paragraphs)


async def default_web_fetcher(
    uri: str,
    *,
    http_get: Callable[..., Awaitable[Any]] | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
) -> str | None:
    """Fetch ``uri``'s text, or None on any failure (best-effort).

    The default ``web_fetcher`` seam for ``text_quote`` drift checks. A local
    ``.docx`` path is read via :func:`_docx_paragraph_text` (06df6ab3 — no
    network, no PDF round-trip); everything else is an HTTP GET as before. Never
    raises.
    """
    if not uri or not isinstance(uri, str):
        return None
    if _looks_like_local_docx(uri):
        return _docx_paragraph_text(uri)
    try:
        if http_get is None:
            import httpx  # noqa: PLC0415

            async with httpx.AsyncClient(
                timeout=timeout, follow_redirects=True
            ) as client:
                resp = await client.get(uri)
        else:
            resp = await http_get(uri)
        return getattr(resp, "text", None)
    except Exception:  # noqa: BLE001
        _log.debug("default_web_fetcher failed for %r", uri, exc_info=True)
        return None
