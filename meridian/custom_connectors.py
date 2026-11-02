"""The owner's custom MCP connectors, as the tunnel bridge reads them.

Pure Python: no network, no process start, no Qt. The desktop app
(ajc3xc/meridian-desktop, meridian_desktop/custom_connectors.py) writes the
registry as the ``custom_connectors`` list in ``settings.json`` and the opt-in
as ``custom_tunnel_opt_in``. This module reads that same file on the same
machine and keeps only what the tunnel may serve.

Rules, in order:

- The opt-in ``custom_tunnel_opt_in`` must be literally ``true``. It is off by
  default, and when it is off nothing else in the file is read.
- An entry is used only when both ``enabled`` and ``tunnel_exposed`` are
  literally ``true``.
- Each entry is checked the way the desktop checks it. The tunnel is stricter
  on names: letters, digits and dashes only, because the tool name is
  ``custom-<name>__<tool>`` and an underscore would make that split ambiguous.
  The names ``p0``..``p3`` are refused, since they are the owner's plugin slots.
- An HTTP entry is used with NO auth headers. A ``headers`` key, if an entry has
  one, is never read.

Nothing here returns or logs a command, an argument or a URL in an error text:
those can carry a secret. Errors name the connector and the reason only.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

SETTINGS_FILE_NAME = "settings.json"
KEY_CONNECTORS = "custom_connectors"
KEY_OPT_IN = "custom_tunnel_opt_in"

TRANSPORT_STDIO = "stdio"
TRANSPORT_HTTP = "http"

MAX_CONNECTORS = 50
MAX_ARGS = 20
MAX_TEXT_LENGTH = 300
MAX_URL_LENGTH = 2048

# Tool names advertised over the tunnel are ``custom-<connector>__<tool>``.
TOOL_PREFIX = "custom-"
TOOL_SEPARATOR = "__"
# claude.ai tool names are limited to this character set and length.
_TOOL_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")
# Letters, digits, dashes. No underscore, so ``__`` in a tool name is unambiguous.
CONNECTOR_NAME = re.compile(r"[A-Za-z][A-Za-z0-9-]{0,39}")
RESERVED_NAMES = frozenset({"p0", "p1", "p2", "p3"})
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

# Characters a shell reads as syntax or expansion, and quotes. Mirrors the desktop.
_SHELL_CHARACTERS = frozenset(";&|<>`$(){}[]*?!^'\"%#")
_URL_BAD_CHARACTERS = frozenset("\"'<>`")


class ConnectorRefused(ValueError):
    """An entry is not served. The text says why, and never repeats a value."""


@dataclass(frozen=True)
class CustomConnector:
    """One connector the tunnel may serve. Command, args and URL are hidden from repr."""

    name: str
    transport: str
    command: str = field(default="", repr=False)
    args: tuple[str, ...] = field(default=(), repr=False)
    url: str = field(default="", repr=False)


@dataclass(frozen=True)
class CustomConnectorConfig:
    """What the tunnel client may serve. ``notes`` are plain sentences with no values."""

    opt_in: bool
    connectors: tuple[CustomConnector, ...] = ()
    notes: tuple[str, ...] = ()


def settings_path(env: Mapping[str, str] | None = None, home: Path | None = None) -> Path:
    """The desktop settings file: %LOCALAPPDATA%\\meridian-desktop\\settings.json, or
    ~/.meridian-desktop/settings.json when LOCALAPPDATA is unset."""
    environ = os.environ if env is None else env
    local_app_data = environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "meridian-desktop" / SETTINGS_FILE_NAME
    base = home if home is not None else Path.home()
    return base / ".meridian-desktop" / SETTINGS_FILE_NAME


def _clean_name(value: object) -> str:
    if not isinstance(value, str):
        raise ConnectorRefused("the name must be text")
    name = value.strip()
    if not name:
        raise ConnectorRefused("the name is empty")
    if not CONNECTOR_NAME.fullmatch(name):
        raise ConnectorRefused(
            "the tunnel needs a name of letters, digits and dashes, starting with a letter"
        )
    if name.lower() in RESERVED_NAMES:
        raise ConnectorRefused("that name is reserved for a plugin slot")
    return name


def _clean_text(value: object, what: str) -> str:
    if not isinstance(value, str):
        raise ConnectorRefused(f"the {what} must be text")
    text = value.strip()
    if not text:
        raise ConnectorRefused(f"the {what} is empty")
    if len(text) > MAX_TEXT_LENGTH:
        raise ConnectorRefused(f"the {what} is longer than {MAX_TEXT_LENGTH} characters")
    if any(ch in _SHELL_CHARACTERS or ord(ch) < 32 for ch in text):
        raise ConnectorRefused(f"the {what} holds a character a shell reads as syntax")
    return text


def _clean_args(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ConnectorRefused("the arguments must be a list")
    if len(value) > MAX_ARGS:
        raise ConnectorRefused(f"there are more than {MAX_ARGS} arguments")
    return tuple(_clean_text(item, "argument") for item in value)


def _clean_url(value: object) -> str:
    if not isinstance(value, str):
        raise ConnectorRefused("the URL must be text")
    url = value.strip()
    if not url:
        raise ConnectorRefused("the URL is empty")
    if len(url) > MAX_URL_LENGTH:
        raise ConnectorRefused(f"the URL is longer than {MAX_URL_LENGTH} characters")
    if any(ch.isspace() or ord(ch) < 32 or ch in _URL_BAD_CHARACTERS for ch in url):
        raise ConnectorRefused("the URL holds a character a URL cannot have")
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        parts.port  # noqa: B018 - raises ValueError for a port out of range
    except ValueError as exc:
        raise ConnectorRefused("the URL is not valid") from exc
    if parts.username is not None or parts.password is not None:
        raise ConnectorRefused("the URL holds a user name or password")
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not host:
        raise ConnectorRefused("the URL must be https, or http to a loopback host")
    if scheme == "http" and host not in LOOPBACK_HOSTS:
        raise ConnectorRefused("http is allowed only for a loopback host")
    return url


def connector_from_entry(item: Mapping) -> CustomConnector:
    """A connector from one saved object. Raises ConnectorRefused, with a plain reason."""
    name = _clean_name(item.get("name"))
    kind = item.get("kind", "copy")
    if kind not in ("copy", "linked"):
        raise ConnectorRefused("the kind must be copy or linked")
    transport = item.get("transport")
    if transport == TRANSPORT_STDIO:
        return CustomConnector(
            name=name,
            transport=TRANSPORT_STDIO,
            command=_clean_text(item.get("command"), "command"),
            args=_clean_args(item.get("args")),
        )
    if transport == TRANSPORT_HTTP:
        return CustomConnector(name=name, transport=TRANSPORT_HTTP, url=_clean_url(item.get("url")))
    raise ConnectorRefused("the transport must be stdio or http")


def load_config(path: Path | None = None) -> CustomConnectorConfig:
    """Read the owner's opt-in and the usable connectors. Never raises on a bad file."""
    target = path if path is not None else settings_path()
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return CustomConnectorConfig(opt_in=False)
    except (OSError, UnicodeDecodeError):
        return CustomConnectorConfig(False, notes=("settings.json could not be read; no custom connectors are served.",))
    try:
        data = json.loads(text)
    except ValueError:
        return CustomConnectorConfig(False, notes=("settings.json is not valid JSON; no custom connectors are served.",))
    if not isinstance(data, dict) or data.get(KEY_OPT_IN) is not True:
        return CustomConnectorConfig(opt_in=False)

    items = data.get(KEY_CONNECTORS)
    if items is None:
        return CustomConnectorConfig(opt_in=True)
    if not isinstance(items, list):
        return CustomConnectorConfig(True, notes=("custom_connectors must be a list; none are served.",))

    connectors: list[CustomConnector] = []
    notes: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(items[:MAX_CONNECTORS]):
        if not isinstance(item, Mapping):
            notes.append(f"entry {index + 1} is not a connector object, so it is not served.")
            continue
        if item.get("enabled") is not True or item.get("tunnel_exposed") is not True:
            continue
        label = item.get("name") if isinstance(item.get("name"), str) and CONNECTOR_NAME.fullmatch(item["name"]) else f"entry {index + 1}"
        try:
            connector = connector_from_entry(item)
        except ConnectorRefused as exc:
            notes.append(f"connector {label} is not served: {exc}.")
            continue
        key = connector.name.lower()
        if key in seen:
            notes.append(f"connector {label} is not served: another connector has the same name.")
            continue
        seen.add(key)
        connectors.append(connector)
    if len(items) > MAX_CONNECTORS:
        notes.append(f"only the first {MAX_CONNECTORS} connectors are considered.")
    return CustomConnectorConfig(True, tuple(connectors), tuple(notes))


def custom_tool_name(connector: str, tool: str) -> str | None:
    """The advertised name for a connector's tool, or None if it cannot be advertised."""
    name = f"{TOOL_PREFIX}{connector}{TOOL_SEPARATOR}{tool}"
    return name if _TOOL_NAME.fullmatch(name) else None


def is_custom_connector_prefix(prefix: str) -> bool:
    """True for a connector display prefix ``custom-<name>``, the part before ``__``."""
    return prefix.startswith(TOOL_PREFIX) and CONNECTOR_NAME.fullmatch(prefix[len(TOOL_PREFIX):]) is not None


def is_custom_connector_tool_name(name: object) -> bool:
    """True for a name shaped like a custom connector tool. Shape only, not an existence check."""
    if not isinstance(name, str) or TOOL_SEPARATOR not in name:
        return False
    prefix, _sep, rest = name.partition(TOOL_SEPARATOR)
    return bool(rest) and is_custom_connector_prefix(prefix)


def announced_names(message: Mapping) -> tuple[str, ...]:
    """The connector names the client announces over the tunnel, checked. Empty unless the
    announcement carries ``opt_in`` as literally true."""
    if message.get("opt_in") is not True:
        return ()
    items = message.get("connectors")
    if not isinstance(items, list):
        return ()
    names: list[str] = []
    seen: set[str] = set()
    for item in items[:MAX_CONNECTORS]:
        raw = item.get("name") if isinstance(item, Mapping) else None
        if not isinstance(raw, str) or not CONNECTOR_NAME.fullmatch(raw) or raw.lower() in RESERVED_NAMES:
            continue
        if raw.lower() in seen:
            continue
        seen.add(raw.lower())
        names.append(raw)
    return tuple(names)


__all__ = [
    "CONNECTOR_NAME",
    "CustomConnector",
    "CustomConnectorConfig",
    "ConnectorRefused",
    "MAX_CONNECTORS",
    "TRANSPORT_HTTP",
    "TRANSPORT_STDIO",
    "TOOL_PREFIX",
    "TOOL_SEPARATOR",
    "announced_names",
    "connector_from_entry",
    "custom_tool_name",
    "is_custom_connector_prefix",
    "is_custom_connector_tool_name",
    "load_config",
    "settings_path",
]
