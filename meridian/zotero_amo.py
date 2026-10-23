"""Mozilla AMO API client for the Zotero bridge: the JWT sign-in and a read-only fetch of an approved version.

* ``make_jwt`` builds the HS256 token that AMO takes as ``Authorization: JWT <token>``. The API key is the
  issuer (``iss``) and the API secret is the signing key. Standard library only. The token is never logged.
* ``fetch_approved_version`` reads one version of the add-on by its number. When Mozilla has approved and
  signed that version, it downloads the file. It only reads. It never calls an upload or sign endpoint.
* ``urllib_transport`` is the one place that opens a connection. It sends GET requests only, refuses a
  redirect to another host, and has a fixed timeout.

What the AMO docs say (https://mozilla.github.io/addons-server/topics/api/auth.html and
https://mozilla.github.io/addons-server/topics/api/addons.html, read through a summarising fetch):

* JWT: algorithm HS256, ``iss`` = the API key, ``iat`` and ``exp`` in UTC seconds with ``exp`` at most five minutes
  after ``iat``, a unique ``jti``, and the header ``Authorization: JWT <token>``.
* Version detail: ``GET /api/v5/addons/addon/<id|slug|guid>/versions/<number>/``. A version number with no dot is
  read as an id unless it is prefixed with ``v``. The ``file`` object has ``status`` (``public`` = approved,
  ``disabled``, ``unreviewed`` = awaiting review), ``is_mozilla_signed_extension`` (the signed indicator), ``url``
  (the absolute download URL), ``size`` (bytes) and ``hash``.
* Upload detail (``GET /api/v5/addons/upload/<uuid>/``) requires authentication. It is not called here.

UNVERIFIED from the docs: whether the download URL needs the Authorization header (the docs do not say), so the
header is sent to that URL only when it is on the same https host as the API; the exact form of ``file.hash``
(it is checked only when it reads ``sha256:<64 hex>``); and whether an unlisted, signed version reports
``status`` ``public``. Each of these is isolated in one function below.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Mapping
from urllib.parse import quote, urlsplit

API_KEY_ENV = "WEB_EXT_API_KEY"
API_SECRET_ENV = "WEB_EXT_API_SECRET"
API_BASE = "https://addons.mozilla.org/api/v5/"
JWT_LIFETIME_S = 300
REQUEST_TIMEOUT_S = 60.0
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
USER_AGENT = "meridian-zotero-sign"
PUBLIC_FILE_STATUS = "public"
SIGNED_FIELD = "is_mozilla_signed_extension"

# FetchResult.status values.
FETCHED = "fetched"
NOT_APPROVED = "not_approved_yet"
NO_CREDENTIALS = "no_credentials"
FAILED = "failed"

NO_CREDENTIALS_MESSAGE = (
    f"Mozilla API credentials are missing. Set {API_KEY_ENV} and {API_SECRET_ENV} as user environment variables. "
    "Nothing was fetched or stored."
)
NOT_APPROVED_MESSAGE = "Mozilla has not approved and signed this version yet. Nothing was fetched or stored."
UNREACHABLE_MESSAGE = "Mozilla could not be reached, or the connection failed. Nothing was fetched or stored."
UNEXPECTED_MESSAGE = "Mozilla's answer for this version was not in the expected form. Nothing was fetched or stored."
OFF_HOST_MESSAGE = (
    "Mozilla gave a download address outside the add-on API host, so the file was not downloaded. Nothing was stored."
)
SIZE_MESSAGE = "The downloaded file is not the size Mozilla reported. Nothing was fetched or stored."
HASH_MESSAGE = "The downloaded file is not the sha256 Mozilla reported. Nothing was fetched or stored."
TOO_LARGE_MESSAGE = "The downloaded file is larger than expected. Nothing was fetched or stored."

_SHA256_PREFIX = "sha256:"
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class FetchResult:
    """What ``fetch_approved_version`` reports. ``data`` is set only when ``status`` is ``FETCHED``."""

    status: str
    data: "bytes | None" = None
    message: "str | None" = None


# ---------------------------------------------------------------------------
# Credentials and the JWT
# ---------------------------------------------------------------------------


def read_credentials(env: "Mapping[str, str] | None") -> "tuple[str, str] | None":
    """The API key and secret, stripped, or None when either is missing or empty. ``env`` None means os.environ."""
    source = os.environ if env is None else env
    key = str(source.get(API_KEY_ENV) or "").strip()
    secret = str(source.get(API_SECRET_ENV) or "").strip()
    return (key, secret) if key and secret else None


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _compact_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode("ascii")


def make_jwt(key: str, secret: str, now: float) -> str:
    """An HS256 JWT for AMO: ``iss`` is the key, ``iat`` is ``now``, ``exp`` is five minutes later, ``jti`` is new.

    Raises ValueError when the key or the secret is empty. The secret is never part of the token.
    """
    if not key or not secret:
        raise ValueError("the AMO API key and secret must both be set")
    issued = int(now)
    header = {"alg": "HS256", "typ": "JWT"}
    claims = {"iss": key, "iat": issued, "exp": issued + JWT_LIFETIME_S, "jti": secrets.token_hex(16)}
    signing_input = f"{_b64url(_compact_json(header))}.{_b64url(_compact_json(claims))}"
    signature = hmac.new(secret.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256).digest()
    return f"{signing_input}.{_b64url(signature)}"


def _request_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"JWT {token}", "Accept": "application/json", "User-Agent": USER_AGENT}


def _download_headers(token: str) -> dict[str, str]:
    """The headers for the file download: the same token, but no JSON Accept header on a binary file.

    UNVERIFIED: the docs do not say whether the download needs the Authorization header. It is sent here, and only to
    the API host (see ``_is_api_host``).
    """
    return {"Authorization": f"JWT {token}", "User-Agent": USER_AGENT}


# ---------------------------------------------------------------------------
# The version read: its URL, and the checks on what it returns
# ---------------------------------------------------------------------------


def _version_segment(version: str) -> str:
    """A dotted version is read as a version number. A version with no dot needs the ``v`` prefix (AMO docs)."""
    return version if "." in version else f"v{version}"


def version_detail_url(addon_id: str, version: str) -> str:
    """The URL that reads ``version`` of the add-on named by ``addon_id`` (its GUID, escaped for the path)."""
    guid = quote(addon_id, safe="")
    segment = quote(_version_segment(version), safe="")
    return f"{API_BASE}addons/addon/{guid}/versions/{segment}/"


def _is_api_host(url: str) -> bool:
    """True when ``url`` is https on the same host as the API. Only that host ever receives the token."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    api = urlsplit(API_BASE)
    return parts.scheme == api.scheme and parts.netloc.lower() == api.netloc.lower()


def _declared_sha256(value: object) -> "str | None":
    """The sha256 that Mozilla reports for the file, when it reports one as ``sha256:<64 hex>``. Otherwise None.

    UNVERIFIED: the docs do not give the exact form of ``file.hash``. Any other form is left to the content checks.
    """
    if not isinstance(value, str) or not value.startswith(_SHA256_PREFIX):
        return None
    digest = value[len(_SHA256_PREFIX):].lower()
    return digest if _SHA256_HEX.fullmatch(digest) else None


def _json_object(body: bytes) -> "dict[str, Any] | None":
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def _http_message(status: object) -> str:
    return f"Mozilla answered with HTTP {status}. Nothing was fetched or stored."


def _failed(message: str) -> FetchResult:
    return FetchResult(status=FAILED, message=message)


def _download(
    run: Callable[[str, Mapping[str, str]], tuple[int, bytes]], url: str, headers: Mapping[str, str],
) -> "tuple[int, bytes] | None":
    """The download, or None when the connection failed. The transport's own error text is never kept."""
    try:
        return run(url, headers)
    except Exception:  # noqa: BLE001 -- a failed call is a failed result; its text is never kept
        return None


# ---------------------------------------------------------------------------
# fetch_approved_version: read the version, and download it only when Mozilla has approved and signed it
# ---------------------------------------------------------------------------


def fetch_approved_version(
    *,
    env: "Mapping[str, str] | None",
    transport: "Callable[[str, Mapping[str, str]], tuple[int, bytes]] | None",
    addon_id: str,
    version: str,
    clock: "Callable[[], float] | None" = None,
) -> FetchResult:
    """Read ``version`` of ``addon_id`` from AMO, and return its signed file when Mozilla has approved and signed it.

    * No credentials: ``NO_CREDENTIALS``, and no transport call.
    * Not approved, not signed, no download address, or no such version: ``NOT_APPROVED``.
    * Otherwise the file is downloaded with the same token, checked against the reported size and sha256 when those
      are given, and returned as ``FETCHED`` with its bytes. Anything else is ``FAILED`` with a message that holds no
      secret and no transport error text.

    The caller must still check the bytes against this source before it stores them. ``transport`` is called as
    ``transport(url, headers)`` and returns ``(status, body)``. ``transport`` None means ``urllib_transport``. Never
    raises.
    """
    creds = read_credentials(env)
    if creds is None:
        return FetchResult(status=NO_CREDENTIALS, message=NO_CREDENTIALS_MESSAGE)
    run = transport if transport is not None else urllib_transport
    try:
        now = (clock if clock is not None else time.time)()
        token = make_jwt(creds[0], creds[1], now)
        status, body = run(version_detail_url(addon_id, version), _request_headers(token))
    except Exception:  # noqa: BLE001 -- a failed call is a failed result; its text is never kept
        return _failed(UNREACHABLE_MESSAGE)
    if status == 404:
        return FetchResult(status=NOT_APPROVED, message=NOT_APPROVED_MESSAGE)
    if status != 200:
        return _failed(_http_message(status))
    record = _json_object(body)
    if record is None or record.get("version") != version:
        return _failed(UNEXPECTED_MESSAGE)
    file = record.get("file")
    if not isinstance(file, dict):
        return _failed(UNEXPECTED_MESSAGE)
    url = file.get("url")
    # Approval is the file's public status. The is_mozilla_signed_extension flag reads False for an approved
    # unlisted version (checked live), so it is not required here. The downloaded file is checked for Mozilla's
    # signature entries and against this source before anything is stored.
    approved = file.get("status") == PUBLIC_FILE_STATUS
    if not approved or not isinstance(url, str) or not url:
        return FetchResult(status=NOT_APPROVED, message=NOT_APPROVED_MESSAGE)
    if not _is_api_host(url):
        return _failed(OFF_HOST_MESSAGE)
    downloaded = _download(run, url, _download_headers(token))
    if downloaded is None:
        return _failed(UNREACHABLE_MESSAGE)
    status, data = downloaded
    if status != 200:
        return _failed(_http_message(status))
    if len(data) > MAX_RESPONSE_BYTES:
        return _failed(TOO_LARGE_MESSAGE)
    size = file.get("size")
    if isinstance(size, int) and not isinstance(size, bool) and size != len(data):
        return _failed(SIZE_MESSAGE)
    declared = _declared_sha256(file.get("hash"))
    if declared is not None and declared != hashlib.sha256(data).hexdigest():
        return _failed(HASH_MESSAGE)
    return FetchResult(status=FETCHED, data=data)


# ---------------------------------------------------------------------------
# The one transport: GET only, a fixed timeout, and no redirect to another host
# ---------------------------------------------------------------------------


class _NoCrossHostRedirect(urllib.request.HTTPRedirectHandler):
    """Follows a redirect only to the same host, so the token is never sent to another host.

    A redirect to another host is not followed. urllib then raises an HTTP error for the 3xx code, which
    ``urllib_transport`` returns as that code.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target, origin = urlsplit(newurl), urlsplit(req.full_url)
        # The scheme must match too: an https request redirected to http would send the token in cleartext.
        if target.scheme.lower() != origin.scheme.lower() or target.netloc.lower() != origin.netloc.lower():
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def urllib_transport(url: str, headers: Mapping[str, str]) -> tuple[int, bytes]:
    """GET ``url`` with ``headers`` and return ``(status, body)``. An HTTP error returns its code with no body.

    A connection failure raises OSError (or a subclass). The caller turns it into a failed result.
    """
    request = urllib.request.Request(url, headers=dict(headers), method="GET")
    opener = urllib.request.build_opener(_NoCrossHostRedirect)
    try:
        with opener.open(request, timeout=REQUEST_TIMEOUT_S) as response:
            return int(response.status), response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        return int(exc.code), b""
