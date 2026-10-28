"""Shared local-only guard for routes that serve this machine's own state (spec 6.1, P1).

``require_local_request`` refuses (403, the caller's message) unless every check holds:

- the TCP peer is a loopback address;
- no forwarding header is present;
- the ``Host`` header names loopback (``localhost``, ``127.0.0.1`` or ``[::1]``, each with
  an optional port). A DNS-rebinding page sends its own host name in ``Host``, so this is
  the check that stops rebinding. ``Sec-Fetch-Site`` cannot stop it, because a rebinding
  page is same-origin with itself;
- ``Sec-Fetch-Site``, when sent, is ``none`` or ``same-origin`` (blocks cross-site requests);
- ``Origin``, when sent, is an http(s) loopback origin.

The desktop research route and the workspace overview route both call this module, so
the rule lives in one place (review B2).
"""
from __future__ import annotations

import ipaddress
import re

from fastapi import HTTPException, Request

# A request that passed through a proxy or forwarder is not a direct local call.
FORWARDED_HEADERS = ("forwarded", "x-forwarded-for", "x-forwarded-host", "x-real-ip")
_LOOPBACK_HOST_RE = re.compile(r"(?:localhost|127\.0\.0\.1|\[::1\])(?::\d{1,5})?", re.IGNORECASE)
_LOOPBACK_ORIGIN_RE = re.compile(r"https?://(?:localhost|127\.0\.0\.1|\[::1\])(?::\d{1,5})?", re.IGNORECASE)
_FETCH_SITE_ALLOWED = frozenset({"none", "same-origin"})


def is_loopback_host(ip: str) -> bool:
    """True when ``ip`` is a loopback address (IPv4-mapped IPv6 included)."""
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_loopback


def is_loopback_host_header(value: str | None) -> bool:
    """True for a ``Host`` value of localhost, 127.0.0.1 or [::1], each with an optional port."""
    return bool(value) and _LOOPBACK_HOST_RE.fullmatch(value) is not None


def is_loopback_origin(value: str | None) -> bool:
    """True for an ``Origin`` of http(s) to localhost, 127.0.0.1 or [::1], with an optional port."""
    return bool(value) and _LOOPBACK_ORIGIN_RE.fullmatch(value) is not None


async def require_local_request(request: Request, message: str) -> None:
    """Refuse (403, ``message``) anything that did not come straight from this machine."""
    client = request.client
    if client is None or not is_loopback_host(client.host):
        raise HTTPException(status_code=403, detail=message)
    if any(name in request.headers for name in FORWARDED_HEADERS):
        raise HTTPException(status_code=403, detail=message)
    if not is_loopback_host_header(request.headers.get("host")):
        raise HTTPException(status_code=403, detail=message)
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site is not None and fetch_site.strip().lower() not in _FETCH_SITE_ALLOWED:
        raise HTTPException(status_code=403, detail=message)
    origin = request.headers.get("origin")
    if origin is not None and not is_loopback_origin(origin):
        raise HTTPException(status_code=403, detail=message)
