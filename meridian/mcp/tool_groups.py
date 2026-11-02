"""Tool-group filter for the hosted tunnel bridge (feat/mcp-tool-groups).

The bridge in ``handler.py`` advertises every tool the tenant's live
``meridian --tunnel`` exposes, each namespaced by its connector display name
(``codebase__search_graph``, ``filesystem__read_file``, ...). This module puts
each connector into one group and lets the tenant switch a whole group, or a
single tool, off. Everything is on by default.

Native Meridian tools and GitHub tools are never in scope: a name that is not
a tunnel connector name (no ``<connector>__`` prefix from
``routes/tunnel.SLOT_DISPLAY_NAMES``) is always enabled.

Connector groups, keyed by the tunnel slot label (``routes/tunnel.py``)::

    chat-files  fs                 -> filesystem__*
    code        code               -> codebase__*          (codebase-memory-mcp)
    code        extract            -> extractor__*         (Serena, meridian-extractor)
    outputs     outputs            -> meridian-outputs__*
    docs        docs               -> meridian-docs__*
    latex       latex              -> meridian-latex__*
    debug       debug              -> mcp-debugger__*
    office      ppt, word          -> powerpoint__*, word__*
    desktop     dc                 -> desktop-commander__*
    zotero      zotero             -> zotero-mcp__*
    custom      p0..p3             -> custom-p0__* .. custom-p3__*
    custom      owner's connectors -> custom-<name>__*     (custom_connectors.py, opt-in)

Settings value: ``tenants.tunnel_tool_filter`` (JSON text, NULL = all enabled)::

    {"groups": {"debug": false}, "tools": {"codebase__trace_path": false}}

A missing key, or a ``true`` value, means enabled. A tool is enabled only when
its group is enabled and the tool itself is not switched off. Non-boolean
values and unknown group names are ignored, so a malformed value fails open to
the default (everything on). This module only reads the setting; writing it is
done through ``db.update_tenant(..., tunnel_tool_filter=...)``.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Iterable

from ..custom_connectors import is_custom_connector_prefix

# Tunnel slot label -> group. Every slot in routes/tunnel._TUNNEL_LABELS must
# appear here; tests enforce that so no tunnel tool is ungrouped.
_SLOT_GROUPS: dict[str, str] = {
    "fs": "chat-files",
    "code": "code",
    "extract": "code",
    "outputs": "outputs",
    "docs": "docs",
    "latex": "latex",
    "debug": "debug",
    "ppt": "office",
    "word": "office",
    "dc": "desktop",
    "zotero": "zotero",
    "p0": "custom",
    "p1": "custom",
    "p2": "custom",
    "p3": "custom",
}

# Ordered, de-duplicated group names.
GROUPS: tuple[str, ...] = tuple(dict.fromkeys(_SLOT_GROUPS.values()))


@lru_cache(maxsize=1)
def _connector_groups() -> dict[str, str]:
    """Connector display prefix (``codebase``) -> group (``code``).

    Built from ``routes/tunnel.SLOT_DISPLAY_NAMES``, the same map that
    ``list_tunnel_tools`` uses to prefix tool names. Imported lazily so this
    module stays importable without the routes package.
    """
    from ..routes.tunnel import SLOT_DISPLAY_NAMES  # noqa: PLC0415

    return {
        SLOT_DISPLAY_NAMES[slot]: group
        for slot, group in _SLOT_GROUPS.items()
        if slot in SLOT_DISPLAY_NAMES
    }


def group_of_tool(name: str) -> str | None:
    """The group a tunnel tool belongs to, or ``None`` if it is not a tunnel tool.

    The owner's custom connectors (``custom-<name>__<tool>``) are in the ``custom``
    group, whether or not they are currently announced. A name that is never
    announced is never listed or callable, so the group only ever narrows what is
    already on offer.
    """
    prefix, sep, _rest = name.partition("__")
    if not sep:
        return None
    group = _connector_groups().get(prefix)
    if group is not None:
        return group
    if is_custom_connector_prefix(prefix):
        return "custom"
    return None


def _flag_map(raw: Any) -> dict[str, bool]:
    if not isinstance(raw, dict):
        return {}
    return {str(k): v for k, v in raw.items() if isinstance(v, bool)}


@dataclass(frozen=True)
class ToolFilter:
    """The disabled groups and tools for one tenant. Empty means everything on."""

    disabled_groups: frozenset[str] = frozenset()
    disabled_tools: frozenset[str] = frozenset()

    def is_enabled(self, name: str) -> bool:
        group = group_of_tool(name)
        if group is None:
            return True
        return group not in self.disabled_groups and name not in self.disabled_tools

    def disabled_reason(self, name: str) -> str | None:
        """A client-facing reason when *name* is switched off, else ``None``."""
        group = group_of_tool(name)
        if group is None:
            return None
        if group in self.disabled_groups:
            return (
                f"tool '{name}' is disabled: its group '{group}' is turned off "
                "in this tenant's tunnel tool settings"
            )
        if name in self.disabled_tools:
            return f"tool '{name}' is disabled in this tenant's tunnel tool settings"
        return None

    def filter_tools(self, tools: Iterable[dict]) -> list[dict]:
        """Keep the tool dicts whose ``name`` is enabled, preserving order."""
        return [t for t in tools if self.is_enabled(str(t.get("name") or ""))]


def parse_tool_filter(raw: Any) -> ToolFilter:
    """Build a :class:`ToolFilter` from the stored setting.

    *raw* is the JSON text from ``tenants.tunnel_tool_filter``, an already-decoded
    dict, or ``None``. Anything unparseable yields the default filter.
    """
    data: Any = raw
    if isinstance(raw, (str, bytes)):
        try:
            data = json.loads(raw) if raw.strip() else None
        except (ValueError, TypeError):
            data = None
    if not isinstance(data, dict):
        return ToolFilter()
    groups = _flag_map(data.get("groups"))
    tools = _flag_map(data.get("tools"))
    return ToolFilter(
        disabled_groups=frozenset(g for g, on in groups.items() if g in GROUPS and not on),
        disabled_tools=frozenset(t for t, on in tools.items() if not on),
    )
