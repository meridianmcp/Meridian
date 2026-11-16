"""Copy-only setup guides for MCP clients the desktop does not write config for. Pure Python: no Qt import.

The guides are plain data. This module reads no file, writes no file and makes no network
call. The desktop shows the text from render_text, and the user copies it into the client
by hand.

Guide kinds:
- hosted_instructions: a hosted connector, set up in the client's own settings (ChatGPT, Grok).
- ui_only: a client configured through its own settings file or UI (Zed).
- needs_confirmation: a client whose config path has not been checked yet (Windsurf).

A guide with confirmed set to False is marked "confirm on install". Its steps, file path or
key names have not been checked against the client, so the owner confirms them before the
guide is trusted.

No guide holds a token. Each guide sends the user to the Meridian dashboard, where tokens are
created, and uses TOKEN_PLACEHOLDER wherever a token goes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from meridian_desktop.settings_store import DEFAULT_HOSTED_URL

KIND_HOSTED = "hosted_instructions"
KIND_UI_ONLY = "ui_only"
KIND_NEEDS_CONFIRMATION = "needs_confirmation"
KINDS = (KIND_HOSTED, KIND_UI_ONLY, KIND_NEEDS_CONFIRMATION)

# The public HTTPS endpoint of the hosted Meridian MCP server.
MCP_ENDPOINT = DEFAULT_HOSTED_URL.rstrip("/") + "/mcp"
TOKEN_PLACEHOLDER = "<token from the Meridian dashboard>"
CONFIRM_ON_INSTALL_LINE = (
    "Status: confirm on install. The steps, file path or key names below have not been "
    "checked against this client yet."
)

_SLUG = re.compile(r"[a-z][a-z0-9-]{0,39}")


@dataclass(frozen=True)
class ClientGuide:
    """One copy-only setup guide. ``snippet`` is text to paste, with TOKEN_PLACEHOLDER still in it."""

    client_id: str
    name: str
    kind: str
    confirmed: bool
    summary: str
    steps: tuple[str, ...]
    snippet: str | None = None
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not _SLUG.fullmatch(self.client_id):
            raise ValueError(f"client_id {self.client_id!r} is not a lowercase slug")
        if self.kind not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}, got {self.kind!r}")
        if not self.steps:
            raise ValueError(f"guide {self.client_id!r} needs at least one step")


_ZED_ENTRY = (
    '"meridian": {\n'
    f'  "url": "{MCP_ENDPOINT}",\n'
    '  "headers": {\n'
    f'    "Authorization": "{TOKEN_PLACEHOLDER}"\n'
    "  }\n"
    "}"
)

_WINDSURF_ENTRY = (
    '"meridian": {\n'
    f'  "serverUrl": "{MCP_ENDPOINT}",\n'
    '  "headers": {\n'
    f'    "Authorization": "{TOKEN_PLACEHOLDER}"\n'
    "  }\n"
    "}"
)

GUIDES: tuple[ClientGuide, ...] = (
    ClientGuide(
        client_id="chatgpt",
        name="ChatGPT",
        kind=KIND_HOSTED,
        confirmed=True,
        summary=(
            "ChatGPT connects to Meridian's hosted MCP server over public HTTPS. "
            "Nothing is installed on this computer."
        ),
        steps=(
            "Sign in to the Meridian dashboard and create a token there. Tokens are created "
            "in the dashboard, not in ChatGPT.",
            "In ChatGPT, start a new app that connects to a remote MCP server.",
            f"For the server address, enter {MCP_ENDPOINT}.",
            "Where the form asks for a token or key, enter the token from step 1.",
            "Save the app, start a new chat, and check that the Meridian tools are listed.",
        ),
        notes=(
            "ChatGPT reaches the server from the internet, so a local address such as "
            "127.0.0.1 cannot be used here.",
        ),
    ),
    ClientGuide(
        client_id="grok",
        name="Grok",
        kind=KIND_HOSTED,
        confirmed=False,
        summary=(
            "Grok connects to Meridian's hosted MCP server over public HTTPS. "
            "The connector steps have not been confirmed yet."
        ),
        steps=(
            "Sign in to the Meridian dashboard and create a token there. Tokens are created "
            "in the dashboard, not in Grok.",
            "In Grok, open the connector or tool settings and add a custom remote MCP server.",
            f"For the server address, enter {MCP_ENDPOINT}.",
            "Where the form asks for a token or key, enter the token from step 1.",
        ),
        notes=(
            "Check each screen against Grok when you install. Grok's menu names may differ.",
        ),
    ),
    ClientGuide(
        client_id="zed",
        name="Zed",
        kind=KIND_UI_ONLY,
        confirmed=False,
        summary=(
            "Zed reads context servers from its own settings. The desktop does not write "
            "Zed's settings file, so follow these steps by hand."
        ),
        steps=(
            "Sign in to the Meridian dashboard and create a token there.",
            "In Zed, open settings.json and find the context_servers object.",
            "Inside context_servers, add the entry below, keeping the other entries. "
            "Replace the placeholder with the token from step 1.",
        ),
        snippet=_ZED_ENTRY,
        notes=(
            "Zed's settings file is JSONC, which allows comments. Add the entry to the existing "
            "object rather than replacing the file.",
            "Check the key names and the header value format against Zed when you install.",
        ),
    ),
    ClientGuide(
        client_id="windsurf",
        name="Windsurf",
        kind=KIND_NEEDS_CONFIRMATION,
        confirmed=False,
        summary=(
            "Windsurf reads MCP servers from its MCP config file. The file path below has not "
            "been checked on a machine with Windsurf installed."
        ),
        steps=(
            "Sign in to the Meridian dashboard and create a token there.",
            "Open the Windsurf MCP config file at ~/.codeium/windsurf/mcp_config.json.",
            "Inside the mcpServers object, add the entry below, keeping the other entries. "
            "Replace the placeholder with the token from step 1.",
            "Restart Windsurf so it reads the file.",
        ),
        snippet=_WINDSURF_ENTRY,
        notes=(
            "Check the file path, the key names and the header value format against Windsurf "
            "when you install.",
        ),
    ),
)


def get_guide(client_id: str) -> ClientGuide | None:
    """Return the guide with this id, or None when there is none."""
    for guide in GUIDES:
        if guide.client_id == client_id:
            return guide
    return None


def render_text(guide: ClientGuide) -> str:
    """The plain text the desktop shows and the user copies. Nothing in it is markup."""
    lines = [guide.name, "", guide.summary, ""]
    if not guide.confirmed:
        lines += [CONFIRM_ON_INSTALL_LINE, ""]
    lines.append("Steps:")
    lines += [f"{number}. {step}" for number, step in enumerate(guide.steps, start=1)]
    if guide.snippet is not None:
        lines += ["", "Entry to copy (replace the placeholder first):", "", guide.snippet]
    if guide.notes:
        lines.append("")
        lines += [f"Note: {note}" for note in guide.notes]
    return "\n".join(lines) + "\n"


__all__ = [
    "CONFIRM_ON_INSTALL_LINE",
    "GUIDES",
    "KINDS",
    "KIND_HOSTED",
    "KIND_NEEDS_CONFIRMATION",
    "KIND_UI_ONLY",
    "MCP_ENDPOINT",
    "TOKEN_PLACEHOLDER",
    "ClientGuide",
    "get_guide",
    "render_text",
]
