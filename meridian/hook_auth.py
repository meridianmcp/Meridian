"""Shared, validated bearer headers for hooks that call the Meridian API."""
from __future__ import annotations

import os
import re
from collections.abc import Mapping

_BEARER_TOKEN_RE = re.compile(r"^[A-Za-z0-9._~+/-]+=*$")


def bearer_headers_from_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return an Authorization header from the local hook environment.

    ``MERIDIAN_TOKEN`` takes precedence over ``BEARER_TOKEN``. Invalid values
    are ignored so untrusted environment data cannot inject extra HTTP headers.
    The token is never added to a URL, command line, or log message.
    """
    source = os.environ if environ is None else environ
    primary = (source.get("MERIDIAN_TOKEN") or "").strip()
    token = primary or (source.get("BEARER_TOKEN") or "").strip()
    if not token or not _BEARER_TOKEN_RE.fullmatch(token):
        return {}
    return {"Authorization": f"Bearer {token}"}
