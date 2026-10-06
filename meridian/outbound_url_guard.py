"""Outbound-URL validation for caller-supplied endpoints (SSRF guard).

Some routes let a caller name the host the *server* will connect to (for
example the BYOK ``base_url`` of ``POST /projects/{id}/decisions/consolidate``).
In hosted mode that is a server-side request forgery primitive: the request
originates inside the operator's network, so loopback, private ranges and the
cloud metadata address are reachable from there but not from the caller.

This module is the single reusable validator for that situation.
:func:`validate_outbound_url` returns a normalised base URL or raises
:class:`OutboundURLRejected`; nothing else should open a connection to a URL
the caller chose without going through it.

Policy
------
* Hosted (``hosted=True``): ``https`` only; every address the host resolves to
  must be globally routable.  Loopback, private (RFC 1918 / ULA / shared
  100.64/10), link-local (including 169.254.169.254), unspecified, multicast,
  reserved and IPv6 transition forms (IPv4-mapped, NAT64, 6to4, Teredo,
  IPv4-compatible) are refused.  Internal-looking names (``localhost``,
  ``*.internal``, ``*.local``, ``*.flycast``, single-label names) and numeric
  hosts that are not dotted-quad literals are refused before any lookup.
  ``MERIDIAN_OUTBOUND_ALLOW_PRIVATE`` is never honoured in hosted mode.
* Self-hosted / local (``hosted=False``): people legitimately point this at a
  model server on their own machine, so loopback is allowed over http or
  https.  Link-local / metadata, unspecified, multicast, reserved and
  transition forms stay refused.  Private LAN addresses need an explicit
  opt-in (``MERIDIAN_OUTBOUND_ALLOW_PRIVATE=1``).  Plain http is only allowed
  for targets that are all loopback / opted-in private; anything public must
  be https.
* Both modes: no credentials, query string or fragment in the URL, no control
  characters, whitespace, backslashes or non-ASCII, a valid non-zero port.

Error messages are fixed strings.  They never echo the URL or what it
resolved to, and "does not resolve" and "resolves somewhere forbidden" give
the same answer, so the validator is not a DNS / network-topology oracle.

Residual risk (deliberate, documented)
--------------------------------------
The check resolves the host and the HTTP client resolves it again when it
connects.  A hostile DNS server that answers differently between the two
(DNS rebinding) can still steer that second connection.  Closing it fully
needs connection-time address pinning (connect to the validated IP while
keeping the original Host / SNI), which is not done here.  Hosted callers
mitigate the rest by never following redirects and by ignoring environment
proxies.
"""
from __future__ import annotations

import asyncio
import ipaddress
import os
import re
import socket
from urllib.parse import urlsplit, urlunsplit

__all__ = [
    "ALLOW_PRIVATE_ENV",
    "OutboundURLRejected",
    "avalidate_outbound_url",
    "validate_outbound_url",
]

#: Self-hosted opt-in that additionally permits private LAN targets.  Ignored
#: in hosted mode.
ALLOW_PRIVATE_ENV = "MERIDIAN_OUTBOUND_ALLOW_PRIVATE"

_MAX_URL_LEN = 2048

_MSG_HOST = "base_url host is not permitted"
_MSG_SHAPE = "base_url must be a plain http(s) URL"
_MSG_HTTPS = "base_url must use https"
_MSG_PARTS = "base_url must not contain credentials, a query string or a fragment"

_HOSTED_BLOCKED_NAMES = frozenset({"localhost"})
_HOSTED_BLOCKED_SUFFIXES = (
    ".localhost", ".local", ".internal", ".localdomain", ".home.arpa", ".lan", ".flycast",
)
_NAME_RE = re.compile(r"^[a-z0-9_]([a-z0-9_-]*[a-z0-9_])?(\.[a-z0-9_]([a-z0-9_-]*[a-z0-9_])?)*$")
#: A final label that is purely numeric or 0x-hex is a legacy numeric host form
#: (``127.1``, ``2130706433``, ``0x7f.1``), never a real top-level domain.
_NUMERIC_TLD_RE = re.compile(r"^(?:[0-9]+|0x[0-9a-f]*)$")

_BLOCKED_V4 = (ipaddress.ip_network("0.0.0.0/8"),)
_BLOCKED_V6 = tuple(ipaddress.ip_network(n) for n in (
    "::/96",           # IPv4-compatible (deprecated); also :: and ::1
    "64:ff9b::/96",    # NAT64 well-known prefix
    "64:ff9b:1::/48",  # NAT64 local-use prefix
    "2002::/16",       # 6to4
    "2001::/32",       # Teredo
    "fec0::/10",       # deprecated site-local
))


class OutboundURLRejected(ValueError):
    """The URL must not be connected to.  The message is a fixed generic string."""


def _classify(ip: "ipaddress.IPv4Address | ipaddress.IPv6Address") -> str:
    """Return ``blocked`` | ``loopback`` | ``private`` | ``global`` for one address."""
    if isinstance(ip, ipaddress.IPv6Address):
        mapped = ip.ipv4_mapped
        if mapped is not None:
            ip = mapped
        elif not (ip.is_loopback or ip.is_unspecified) and any(
            ip in net for net in _BLOCKED_V6
        ):
            # ::1 and :: sit inside ::/96 but are classified by the generic
            # rules below (loopback / unspecified); everything else in the
            # transition ranges is refused outright.
            return "blocked"
    if isinstance(ip, ipaddress.IPv4Address) and any(ip in net for net in _BLOCKED_V4):
        return "blocked"
    if ip.is_loopback:  # before is_reserved: Python files ::1 under the reserved ::/8
        return "loopback"
    if ip.is_unspecified or ip.is_multicast or ip.is_link_local or ip.is_reserved:
        return "blocked"
    if ip.is_global:
        return "global"
    return "private"


def _resolve(host: str, port: int) -> "list[ipaddress.IPv4Address | ipaddress.IPv6Address]":
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        raise OutboundURLRejected(_MSG_HOST) from None
    addrs = []
    for info in infos:
        try:
            addrs.append(ipaddress.ip_address(str(info[4][0]).split("%", 1)[0]))
        except ValueError:
            raise OutboundURLRejected(_MSG_HOST) from None
    if not addrs:
        raise OutboundURLRejected(_MSG_HOST)
    return addrs


def _allow_private(hosted: bool) -> bool:
    if hosted:
        return False
    return os.environ.get(ALLOW_PRIVATE_ENV, "").strip().lower() in ("1", "true", "yes")


def validate_outbound_url(url: str, *, hosted: bool) -> str:
    """Validate ``url`` for a server-originated request and return it normalised.

    The returned value has a lower-cased scheme and authority, no trailing
    slash, no query and no fragment, so ``f"{result}/v1/..."`` is safe to build.
    Raises :class:`OutboundURLRejected` otherwise.  Performs a blocking DNS
    lookup for names; use :func:`avalidate_outbound_url` from async code.
    """
    if not isinstance(url, str):
        raise OutboundURLRejected(_MSG_SHAPE)
    if (
        not url or len(url) > _MAX_URL_LEN or not url.isascii() or "\\" in url
        or any(ord(ch) <= 0x20 or ord(ch) == 0x7F for ch in url)
    ):
        raise OutboundURLRejected(_MSG_SHAPE)
    try:
        parts = urlsplit(url)
        port = parts.port
        host = parts.hostname
    except ValueError:
        raise OutboundURLRejected(_MSG_SHAPE) from None
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not host:
        raise OutboundURLRejected(_MSG_SHAPE)
    if "@" in parts.netloc or "?" in url or "#" in url:
        raise OutboundURLRejected(_MSG_PARTS)
    if port == 0:
        raise OutboundURLRejected(_MSG_SHAPE)
    if hosted and scheme != "https":
        raise OutboundURLRejected(_MSG_HTTPS)

    host = host.lower()
    if "%" in host:  # IPv6 zone ids / percent-encoded hosts have no place in an API URL
        raise OutboundURLRejected(_MSG_HOST)
    literal = None
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        host = host.rstrip(".")
        if not _NAME_RE.match(host):
            raise OutboundURLRejected(_MSG_HOST) from None
        if hosted:
            if (
                "." not in host or host in _HOSTED_BLOCKED_NAMES
                or host.endswith(_HOSTED_BLOCKED_SUFFIXES)
                or _NUMERIC_TLD_RE.match(host.rsplit(".", 1)[-1])
            ):
                raise OutboundURLRejected(_MSG_HOST) from None

    effective_port = port or (443 if scheme == "https" else 80)
    addrs = [literal] if literal is not None else _resolve(host, effective_port)
    classes = {_classify(ip) for ip in addrs}
    allow_private = _allow_private(hosted)

    if "blocked" in classes:
        raise OutboundURLRejected(_MSG_HOST)
    if hosted:
        if classes != {"global"}:
            raise OutboundURLRejected(_MSG_HOST)
    else:
        if "private" in classes and not allow_private:
            raise OutboundURLRejected(_MSG_HOST)
        if scheme == "http" and "global" in classes:
            raise OutboundURLRejected(_MSG_HTTPS)

    authority = parts.netloc.lower()
    return urlunsplit((scheme, authority, parts.path.rstrip("/"), "", ""))


async def avalidate_outbound_url(url: str, *, hosted: bool) -> str:
    """Async wrapper: runs :func:`validate_outbound_url` off the event loop."""
    return await asyncio.to_thread(validate_outbound_url, url, hosted=hosted)
