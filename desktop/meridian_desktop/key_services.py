"""Known services for the named API keys: what each key is for, where it is made, and an optional check.

Pure Python: stdlib urllib only, no Qt import.

A registry entry names a service, says in one line what its key is for, and says where the key is made. The page is
plain text, shown as written. Nothing is fetched to build it.

A service may carry a check: one GET to that provider's own https host, which shows whether the saved key is accepted.
The check runs only when the owner presses Check on a key row. It reads the key for that one request, from the keychain
or from the keys file the owner chose (keys_file.py), sends it to the provider host and nowhere else, and reports one of
the fixed messages below. The key is never returned,
stored past the call, or put in a message. A failure is reported by its kind alone, and no exception is chained, so no
traceback holds the request headers.

The transport is injectable. It takes the request and a timeout in seconds, and returns the HTTP status. Tests pass a
fake, so they never reach the network. The default transport is urllib, with no redirects followed.
"""

from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

from meridian_desktop import __version__
from meridian_desktop.api_keys import KEY_SERVICE, account_for, slug
from meridian_desktop.keys_file import KeysFileError, read_value
from meridian_desktop.secret_store import SecretStore

CHECK_TIMEOUT = 10.0
USER_AGENT = f"MeridianDesktop/{__version__} (key check)"

WORKS_TEXT = "Works."
REJECTED_TEXT = "Rejected by {service}. Check the key."
UNREACHABLE_TEXT = "Could not reach {service}."
NO_KEY_TEXT = "No key is saved for {name}."
NO_CHECK_TEXT = "No check yet for this service."
FILE_UNREADABLE_TEXT = "The keys file could not be read. Choose the file again."
NOT_IN_FILE_TEXT = "{name} is not in the keys file."

# The statuses that mean the provider refused the key. Any other status, and any failure, is "could not reach".
REFUSED_STATUSES = (401, 403)


@dataclass(frozen=True)
class ServiceCheck:
    """The one GET a check sends: its URL (https), the header that carries the key, and any fixed headers it needs."""

    url: str
    key_header: str
    key_prefix: str = ""
    extra_headers: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class KeyService:
    """One known service. ``create_page`` is text only. ``check`` is None for a service that has no check yet."""

    name: str
    description: str
    create_page: str
    check: ServiceCheck | None = None


SERVICES: tuple[KeyService, ...] = (
    KeyService(
        name="OpenAI",
        description="The key for the OpenAI API, used by tools that call OpenAI models.",
        create_page="platform.openai.com, under API keys",
        check=ServiceCheck("https://api.openai.com/v1/models", "Authorization", "Bearer "),
    ),
    KeyService(
        name="Anthropic",
        description="The key for the Anthropic API, used by tools that call Claude models.",
        create_page="the Anthropic Console, under API keys",
        check=ServiceCheck(
            "https://api.anthropic.com/v1/models",
            "x-api-key",
            "",
            (("anthropic-version", "2023-06-01"),),
        ),
    ),
    KeyService(
        name="GitHub",
        description="A token for the GitHub API, used by tools that read repositories and account details.",
        create_page="github.com, under Settings, Developer settings, Personal access tokens",
        check=ServiceCheck("https://api.github.com/user", "Authorization", "Bearer "),
    ),
)

# A transport takes the request and a timeout in seconds, and returns the HTTP status. It raises for a failure to reach
# the host. A timeout is an OSError, so it is reported like any other failure to reach the host.
Transport = Callable[[urllib.request.Request, float], int]


class _Refused(Exception):
    """A check may not be sent: its URL is not https. Raised before any request, and never shown."""


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Return no follow-up request, so urllib raises HTTPError for a 3xx reply and nothing is followed."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None


def _urllib_transport(request: urllib.request.Request, timeout: float) -> int:
    """The real transport: one GET through urllib, no redirects, and the body is not read. Return the status."""
    opener = urllib.request.build_opener(_NoRedirects())
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        # urllib raises for a non-2xx status and for the refused redirect. Only the status is kept.
        exc.close()
        return exc.code


def service_for(name: str) -> KeyService | None:
    """The registry entry whose name has the same slug as ``name``, or None when no service matches."""
    key = slug(name)
    for service in SERVICES:
        if slug(service.name) == key:
            return service
    return None


def _saved_value(secrets: SecretStore, name: str) -> str | None:
    """The saved value for ``name``, or None when none is saved or it cannot be read. It is used for one request only."""
    try:
        value = secrets.get_secret(KEY_SERVICE, account_for(name))
    except Exception:  # noqa: BLE001 - a failed read has no value to send, and its text is never used
        return None
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def _sendable(key: str) -> bool:
    """True when the key can go in an HTTP header: ASCII, printable, and with no space or line break in it."""
    return key.isascii() and key.isprintable() and not any(ch.isspace() for ch in key)


def _request_for(check: ServiceCheck, key: str) -> urllib.request.Request:
    """The GET for one check. Raise _Refused when the URL is not https, so the key never goes over plain http."""
    parts = urllib.parse.urlsplit(check.url)
    if parts.scheme != "https" or not parts.hostname:
        raise _Refused()
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT, check.key_header: check.key_prefix + key}
    headers.update(dict(check.extra_headers))
    return urllib.request.Request(check.url, method="GET", headers=headers)


def _verdict(service: KeyService, check: ServiceCheck, key: str, transport: Transport, timeout: float) -> str:
    """Send the one GET and turn its outcome into one of the fixed messages. Nothing from the request leaves here."""
    try:
        status = transport(_request_for(check, key), timeout)
    except Exception:  # noqa: BLE001 - any failure is reported by its kind alone; no exception text or chain is kept
        return UNREACHABLE_TEXT.format(service=service.name)
    if isinstance(status, int) and 200 <= status < 300:
        return WORKS_TEXT
    if status in REFUSED_STATUSES:
        return REJECTED_TEXT.format(service=service.name)
    return UNREACHABLE_TEXT.format(service=service.name)


def check_saved_key(
    service: KeyService,
    name: str,
    secrets: SecretStore,
    *,
    transport: Transport | None = None,
    timeout: float = CHECK_TIMEOUT,
    keys_file: str | None = None,
) -> str:
    """Check the key for ``name`` against ``service``. Return one of the fixed messages, and never the key.

    The key comes from the keychain, or, when ``keys_file`` names the keys file the owner chose, from that file (see
    keys_file.py). A service without a check returns NO_CHECK_TEXT and reads nothing. A name with no saved value returns
    NO_KEY_TEXT and sends nothing. A file that is missing or refused returns FILE_UNREADABLE_TEXT, and a name the file
    does not hold returns NOT_IN_FILE_TEXT; both send nothing. A value that cannot go in a header is refused without a
    request. ``transport`` is injectable, and the default is urllib.
    """
    check = service.check
    if check is None:
        return NO_CHECK_TEXT
    if keys_file is None:
        key = _saved_value(secrets, name)
        if key is None:
            return NO_KEY_TEXT.format(name=name)
    else:
        try:
            key = read_value(keys_file, name)
        except KeysFileError as exc:
            # A skipped entry has a plain message of its own. Any other refused or missing file gets the one file message.
            return str(exc) if exc.entry_skipped else FILE_UNREADABLE_TEXT
        except Exception:  # noqa: BLE001 - a refused or missing file is reported by one fixed message, never its text
            return FILE_UNREADABLE_TEXT
        if key is None:
            return NOT_IN_FILE_TEXT.format(name=name)
    if not _sendable(key):
        return REJECTED_TEXT.format(service=service.name)
    return _verdict(service, check, key, transport if transport is not None else _urllib_transport, timeout)


__all__ = [
    "CHECK_TIMEOUT",
    "FILE_UNREADABLE_TEXT",
    "NOT_IN_FILE_TEXT",
    "NO_CHECK_TEXT",
    "NO_KEY_TEXT",
    "REJECTED_TEXT",
    "SERVICES",
    "UNREACHABLE_TEXT",
    "WORKS_TEXT",
    "KeyService",
    "ServiceCheck",
    "Transport",
    "check_saved_key",
    "service_for",
]
