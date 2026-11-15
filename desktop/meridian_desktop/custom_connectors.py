"""The owner's own MCP connectors: the registry, its checks, and what the local cache gets. Pure Python: no Qt import.

The registry is the list ``custom_connectors`` in settings.json (settings_store.py). Each saved object is one connector the
owner added or linked::

    {"name": "notes", "kind": "copy", "transport": "stdio", "command": "C:/tools/notes.exe", "args": ["--serve"],
     "enabled": true, "tunnel_exposed": false}
    {"name": "remote", "kind": "linked", "transport": "http", "url": "https://example.com/mcp",
     "enabled": true, "tunnel_exposed": false}

``kind`` is "copy" when Meridian keeps the connector's own definition. It is "linked" when the entry points at a connector
that already exists: by its command on this computer, or by its URL. Both kinds share one list. ``transport`` is "stdio",
a command and its arguments run on this computer, or "http", a URL.

Every entry is checked when it is read, so a hand-edited file cannot hold one the form would refuse. A name is a safe
identifier. A command and its arguments hold no shell metacharacter, since they are never handed to a shell. A URL is https,
or http to a loopback host only, and holds no user name or password.

Where an enabled entry goes:
- The local cache writes an enabled stdio entry into Claude Desktop and Codex, under the name meridian-custom-<name>, when its
  command is found on this computer (local_cache.py). An http entry is not written there, because the cache writes local
  stdio servers only.
- claude.ai reaches a connector only through the tunnel bridge, and only while the tunnel is up. That needs both the
  owner's opt-in (``custom_tunnel_opt_in``, off by default) and the entry's ``tunnel_exposed`` flag (off by default). The
  bridge does not serve custom connectors yet. This module stores the two flags, and the bridge follow-up reads them.

Nothing here starts a process, opens a connection or reads a secret.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from urllib.parse import urlsplit

from meridian_desktop.local_cache import CUSTOM_PREFIX, CustomCache, Resolve, ServerSpec, find_command
from meridian_desktop.local_client import LOOPBACK_HOSTS

KIND_COPY = "copy"
KIND_LINKED = "linked"
KINDS = (KIND_COPY, KIND_LINKED)
KIND_TEXT = {KIND_COPY: "Copy", KIND_LINKED: "Linked"}
TRANSPORT_STDIO = "stdio"
TRANSPORT_HTTP = "http"
TRANSPORTS = (TRANSPORT_STDIO, TRANSPORT_HTTP)
TRANSPORT_TEXT = {TRANSPORT_STDIO: "Command", TRANSPORT_HTTP: "URL"}
MAX_NAME_LENGTH = 40
MAX_CONNECTORS = 50
MAX_ARGS = 20
MAX_TEXT_LENGTH = 300
MAX_URL_LENGTH = 2048
# The ServerSpec id for a custom connector. Built-in servers use their catalog ids, so these never clash with them.
SERVER_PREFIX = "custom:"
_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]*")
# Characters a shell reads as syntax or expansion, and quotes. A backslash and "/" stay, since Windows paths need them. "~"
# stays too, because a short 8.3 path such as PROGRA~1 holds one. Parentheses are refused, so a path such as
# "Program Files (x86)" is given in its short form.
_SHELL_CHARACTERS = frozenset(";&|<>`$(){}[]*?!^'\"%#")
_URL_BAD_CHARACTERS = frozenset("\"'<>`")


class ConnectorError(ValueError):
    """A connector is refused, or a saved entry is not usable. The text says why, in plain words, and never repeats a secret."""


@dataclass(frozen=True)
class Connector:
    """One checked connector. ``command`` and ``args`` are set for stdio, ``url`` for http."""

    name: str
    kind: str
    transport: str
    command: str = ""
    args: tuple[str, ...] = ()
    url: str = ""
    enabled: bool = True
    tunnel_exposed: bool = False

    def to_saved(self) -> dict:
        """The normalised object written to settings.json. Only the fields the transport uses are written."""
        saved: dict = {
            "name": self.name,
            "kind": self.kind,
            "transport": self.transport,
            "enabled": self.enabled,
            "tunnel_exposed": self.tunnel_exposed,
        }
        if self.transport == TRANSPORT_STDIO:
            saved["command"] = self.command
            saved["args"] = list(self.args)
        else:
            saved["url"] = self.url
        return saved


@dataclass(frozen=True)
class Row:
    """One saved entry as the Settings section shows it. ``connector`` is None when the entry is not usable, and
    ``problem`` then says why. ``index`` is the entry's position in the saved list, which is how a change finds it."""

    index: int
    name: str
    connector: Connector | None
    problem: str


def clean_name(value: object) -> str:
    """The name, stripped and checked. A name starts with a letter and holds only letters, digits, dashes and underscores."""
    if not isinstance(value, str):
        raise ConnectorError("The name must be text.")
    name = value.strip()
    if not name:
        raise ConnectorError("Type a name for the connector first, for example notes.")
    if len(name) > MAX_NAME_LENGTH:
        raise ConnectorError(f"The name is longer than {MAX_NAME_LENGTH} characters.")
    if not _NAME.fullmatch(name):
        raise ConnectorError("The name must start with a letter, and use only letters, digits, dashes and underscores.")
    return name


def _clean_text(value: object, what: str) -> str:
    """Text for a command or an argument. Refused when empty, too long, or holding a shell metacharacter or a control character."""
    if not isinstance(value, str):
        raise ConnectorError(f"The {what} must be text.")
    text = value.strip()
    if not text:
        raise ConnectorError(f"Type the {what} first.")
    if len(text) > MAX_TEXT_LENGTH:
        raise ConnectorError(f"The {what} is longer than {MAX_TEXT_LENGTH} characters.")
    if any(ch in _SHELL_CHARACTERS or ord(ch) < 32 for ch in text):
        raise ConnectorError(
            f"The {what} holds a character a shell reads as syntax, such as ; | & < > quotes or parentheses. "
            "Use a plain path (a short 8.3 path for Program Files) and plain arguments."
        )
    return text


def clean_args(value: object) -> tuple[str, ...]:
    """The arguments as a list of plain text. Each one is checked like a command."""
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ConnectorError("The arguments must be a list.")
    if len(value) > MAX_ARGS:
        raise ConnectorError(f"There are more than {MAX_ARGS} arguments.")
    return tuple(_clean_text(item, "argument") for item in value)


def clean_url(value: object) -> str:
    """A URL that is https, or http to a loopback host. A user name or password in it is refused."""
    if not isinstance(value, str):
        raise ConnectorError("The URL must be text.")
    url = value.strip()
    if not url:
        raise ConnectorError("Type the URL first, for example https://example.com/mcp.")
    if len(url) > MAX_URL_LENGTH:
        raise ConnectorError(f"The URL is longer than {MAX_URL_LENGTH} characters.")
    if any(ch.isspace() or ord(ch) < 32 or ch in _URL_BAD_CHARACTERS for ch in url):
        raise ConnectorError("The URL holds a space, a quote or another character a URL cannot have.")
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        parts.port  # noqa: B018 - reading it raises ValueError for a port out of range
    except ValueError as exc:
        raise ConnectorError("The URL is not valid.") from exc
    if parts.username is not None or parts.password is not None:
        raise ConnectorError("The URL holds a user name or password. Leave credentials out of the URL.")
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not host:
        raise ConnectorError("The URL must start with https:// and name a host.")
    if scheme == "http" and host not in LOOPBACK_HOSTS:
        raise ConnectorError("An http URL is allowed only for 127.0.0.1, localhost or ::1. Use https:// for any other host.")
    return url


def connector_from_fields(data: Mapping) -> Connector:
    """A connector from a form or from a saved object. Every field is checked, and ConnectorError says what is wrong."""
    name = clean_name(data.get("name"))
    kind = data.get("kind", KIND_COPY)
    if kind not in KINDS:
        raise ConnectorError("The kind must be copy or linked.")
    transport = data.get("transport")
    if transport not in TRANSPORTS:
        raise ConnectorError("The transport must be stdio (a command) or http (a URL).")
    enabled = data.get("enabled", True)
    tunnel_exposed = data.get("tunnel_exposed", False)
    if not isinstance(enabled, bool) or not isinstance(tunnel_exposed, bool):
        raise ConnectorError("enabled and tunnel_exposed must be true or false.")
    if transport == TRANSPORT_STDIO:
        return Connector(
            name=name,
            kind=kind,
            transport=transport,
            command=_clean_text(data.get("command"), "command"),
            args=clean_args(data.get("args")),
            enabled=enabled,
            tunnel_exposed=tunnel_exposed,
        )
    return Connector(
        name=name,
        kind=kind,
        transport=transport,
        url=clean_url(data.get("url")),
        enabled=enabled,
        tunnel_exposed=tunnel_exposed,
    )


def _check_linked_command(connector: Connector, resolve: Resolve) -> None:
    """A linked command must be a connector that already exists on this computer. Checked when it is added."""
    if connector.kind == KIND_LINKED and connector.transport == TRANSPORT_STDIO and resolve(connector.command, ()) is None:
        raise ConnectorError(f"Not added. The linked command {connector.command} was not found on this computer.")


def read_rows(raw: Sequence) -> list[Row]:
    """Every saved entry, checked, in saved order. A name that repeats an earlier usable name is not used."""
    rows: list[Row] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        if index >= MAX_CONNECTORS:
            rows.append(Row(index, "", None, f"More than {MAX_CONNECTORS} connectors are saved, so this one is not used."))
            continue
        if not isinstance(item, Mapping):
            rows.append(Row(index, "", None, "This entry is not a connector object, so it is not used."))
            continue
        saved_name = item.get("name") if isinstance(item.get("name"), str) else ""
        try:
            connector = connector_from_fields(item)
        except ConnectorError as exc:
            rows.append(Row(index, saved_name, None, str(exc)))
            continue
        key = connector.name.lower()
        if key in seen:
            rows.append(Row(index, connector.name, None, f"Another connector is already named {connector.name}, so this one is not used."))
            continue
        seen.add(key)
        rows.append(Row(index, connector.name, connector, ""))
    return rows


def _row_at(raw: Sequence, index: int) -> Row:
    if not 0 <= index < len(raw):
        raise ConnectorError("That connector is no longer in the list.")
    return read_rows(raw)[index]


def add(raw: Sequence, data: Mapping, *, resolve: Resolve = find_command) -> tuple[dict, ...]:
    """The saved list with one more connector. Raises ConnectorError, and changes nothing, when the connector is refused."""
    connector = connector_from_fields(data)
    _check_linked_command(connector, resolve)
    taken = {row.name.lower() for row in read_rows(raw) if row.name}
    if connector.name.lower() in taken:
        raise ConnectorError(f"Not added. A connector named {connector.name} already exists.")
    if len(raw) >= MAX_CONNECTORS:
        raise ConnectorError(f"Not added. The limit is {MAX_CONNECTORS} connectors.")
    return tuple(raw) + (connector.to_saved(),)


def remove(raw: Sequence, index: int) -> tuple[dict, ...]:
    """The saved list without the entry at ``index``. A saved entry that is not usable can be removed too."""
    _row_at(raw, index)
    return tuple(item for position, item in enumerate(raw) if position != index)


def set_enabled(raw: Sequence, index: int, enabled: bool) -> tuple[dict, ...]:
    """The saved list with the entry at ``index`` switched on or off."""
    return _update(raw, index, enabled=bool(enabled))


def set_tunnel_exposed(raw: Sequence, index: int, exposed: bool) -> tuple[dict, ...]:
    """The saved list with the entry at ``index`` marked, or not, for the tunnel. Nothing is served by this alone."""
    return _update(raw, index, tunnel_exposed=bool(exposed))


def _update(raw: Sequence, index: int, **changes: bool) -> tuple[dict, ...]:
    row = _row_at(raw, index)
    if row.connector is None:
        raise ConnectorError("This entry is not usable, so it cannot change. Remove it instead.")
    updated = list(raw)
    updated[index] = replace(row.connector, **changes).to_saved()
    return tuple(updated)


def status_text(row: Row, *, resolve: Resolve = find_command, tunnel_opt_in: bool) -> str:
    """The plain status line for one saved entry: whether it is written, and what the tunnel setting does to it."""
    connector = row.connector
    if connector is None:
        return f"Not used: {row.problem}"
    if not connector.enabled:
        text = "Off. Not written to Claude Desktop or Codex."
    elif connector.transport == TRANSPORT_HTTP:
        text = "On. An http connector is not written to Claude Desktop or Codex; the local cache writes stdio entries only."
    elif resolve(connector.command, ()) is None:
        text = f"Not written: {connector.command} was not found on this computer."
    else:
        text = "On. Written to Claude Desktop and Codex at the next start."
    if connector.tunnel_exposed:
        if not tunnel_opt_in:
            text += " Tunnel: off, because the tunnel opt-in is off."
        else:
            text += " Tunnel: wanted, but the tunnel bridge does not serve custom connectors yet."
    return text


def cache_entries(raw: Sequence, *, resolve: Resolve = find_command) -> CustomCache:
    """What the local cache writes for the owner's connectors: each enabled stdio entry whose command is found, and a note
    for each enabled entry left out. A disabled or removed entry is absent, so the cache removes it at the next run."""
    specs: list[ServerSpec] = []
    notes: list[str] = []
    for row in read_rows(raw):
        connector = row.connector
        if connector is None:
            notes.append(f"A custom connector is not written: {row.problem}")
            continue
        if not connector.enabled:
            continue
        if connector.transport == TRANSPORT_HTTP:
            notes.append(
                f"{CUSTOM_PREFIX}{connector.name} is not written: the local cache writes stdio entries only, "
                "and this connector is an http URL."
            )
            continue
        command = resolve(connector.command, ())
        if command is None:
            notes.append(f"{CUSTOM_PREFIX}{connector.name} is not written: {connector.command} was not found on this computer.")
            continue
        specs.append(
            ServerSpec(
                server=SERVER_PREFIX + connector.name,
                name=CUSTOM_PREFIX + connector.name,
                command=command,
                args=connector.args,
            )
        )
    return CustomCache(specs=tuple(specs), notes=tuple(notes))


__all__ = [
    "Connector",
    "ConnectorError",
    "KIND_COPY",
    "KIND_LINKED",
    "KIND_TEXT",
    "MAX_CONNECTORS",
    "MAX_NAME_LENGTH",
    "Row",
    "TRANSPORTS",
    "TRANSPORT_HTTP",
    "TRANSPORT_STDIO",
    "TRANSPORT_TEXT",
    "add",
    "cache_entries",
    "clean_name",
    "clean_url",
    "connector_from_fields",
    "read_rows",
    "remove",
    "set_enabled",
    "set_tunnel_exposed",
    "status_text",
]
