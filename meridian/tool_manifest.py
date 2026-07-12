"""On-demand tool-manifest discovery (142808f3).

The MCP-standard way to tell a client the tool set changed is
``notifications/tools/list_changed``, but client support for it is unreliable
(Claude Desktop notably does not honour it). When a session suspects its tool
schema went stale — "I nuked the schema and things broke" — it needs a plain,
always-works tool CALL it can invoke to re-discover Meridian's built-in tools,
bypassing the notification path entirely.

``build_tool_manifest`` is a pure function over the server's tool list so it is
trivial to unit-test; the handler passes ``_MCP_TOOLS_LIST`` in and, best-effort,
re-fires the list_changed signal so a client that *does* honour it also re-lists.
"""
from __future__ import annotations

from typing import Any


def _first_sentence(text: str | None) -> str:
    """First sentence of a tool description — enough to identify it, cheap on tokens."""
    s = " ".join((text or "").split())
    if not s:
        return ""
    # Cut at the first sentence-ending period followed by a space, else first 160 chars.
    for i in range(len(s) - 1):
        if s[i] == "." and s[i + 1] == " ":
            return s[: i + 1]
    return s if len(s) <= 160 else s[:157].rstrip() + "..."


def build_tool_manifest(tools: list[dict[str, Any]]) -> dict[str, Any]:
    """Compact, self-describing manifest of the built-in Meridian MCP tools.

    Returns ``{count, tools: [{name, summary}], note}`` — small enough to return
    on every call. Deterministic order (as declared) so diffs are legible.
    """
    entries: list[dict[str, str]] = []
    seen: set[str] = set()
    for t in tools or []:
        name = t.get("name") if isinstance(t, dict) else None
        if not name or name in seen:
            continue
        seen.add(name)
        entries.append({"name": name, "summary": _first_sentence(t.get("description"))})
    return {
        "count": len(entries),
        "tools": entries,
        "note": (
            "Full built-in Meridian tool set. If a tool you expected is missing "
            "from your client's tool list, it is almost certainly a stale client "
            "schema (deferred/tool-search loading), not a removed tool — the names "
            "here are authoritative. Re-issue tools/list or reconnect to surface it."
        ),
    }
