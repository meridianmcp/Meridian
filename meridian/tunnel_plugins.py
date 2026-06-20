"""Tunnel plugin registry — per-tenant config for what `meridian --tunnel` spawns.

Three built-in transport slots map to the server's fixed proxy routes
(`/fs`, `/code`, `/extract` in ``routes/tunnel.py``). A per-tenant
``tunnel_plugins`` config (JSON stored on the tenants table) overrides what runs
behind each slot: enable/disable it, swap the command (e.g. code-intel from
``codebase-memory-mcp`` to ``codegraph``), change the port, or set
``description_overrides``. Swapping a plugin's command is therefore a pure DB
change — no client or server code change, and no redeploy.

This is the **3-slot model** (see the pinned ARCHITECTURAL decision): the server
keeps its three fixed transports; the registry drives the *client's* spawn list
plus the dashboard UI and the bridge's description rewriting. It does NOT add
new server routes per arbitrary plugin.

The module is pure (no DB, no subprocess, no I/O) so it is fully unit-tested.
Both the tunnel client (``tunnel_client.run_tunnel``) and the dashboard consume
``resolve_plugins`` / ``active_plugins``.
"""
from __future__ import annotations

from typing import Any

# slot = the fixed server transport a plugin rides on. The three built-ins each
# own one slot; that mapping is immutable (a config override can't move a
# built-in to another slot, which would collide with the server routes).
SLOTS = ("fs", "code", "extract")

DEFAULT_FS_PORT = 8808
DEFAULT_CODE_PORT = 8809
DEFAULT_EXTRACT_PORT = 8810

# Ordered: filesystem first (the always-on base), then the two code plugins.
BUILTIN_PLUGINS: list[dict[str, Any]] = [
    {
        "name": "filesystem",
        "slot": "fs",
        "port": DEFAULT_FS_PORT,
        "url_prefix": "/fs",
        "enabled": True,
        "builtin": True,
        # None → the client uses its platform-aware default builder for this slot.
        "command": None,
        "description": "Filesystem MCP (@modelcontextprotocol/server-filesystem)",
        "description_overrides": {},
    },
    {
        "name": "code-intel",
        "slot": "code",
        "port": DEFAULT_CODE_PORT,
        "url_prefix": "/code",
        "enabled": True,
        "builtin": True,
        "command": None,  # default: codebase-memory-mcp (auto-installed)
        "description": "Code intelligence graph (codebase-memory-mcp)",
        "description_overrides": {},
    },
    {
        "name": "code-extractor",
        "slot": "extract",
        "port": DEFAULT_EXTRACT_PORT,
        "url_prefix": "/extract",
        "enabled": True,
        "builtin": True,
        "command": None,  # default: uvx mcp-server-code-extractor
        "description": "Symbol extractor (mcp-server-code-extractor)",
        "description_overrides": {},
    },
]

# Editable per-slot fields that a tenant override may set.
_OVERRIDABLE = ("enabled", "command", "port", "description", "description_overrides")


def builtin_names() -> tuple[str, ...]:
    """Names of the three built-in plugins, in display order."""
    return tuple(p["name"] for p in BUILTIN_PLUGINS)


def _coerce_command(value: Any) -> list[str] | None:
    """Normalize a command override to a non-empty ``list[str]`` or ``None``.

    Accepts a shell-style string (split on whitespace) or a list/tuple of
    tokens. Anything else (or an empty result) yields ``None`` so the client
    falls back to its default builder for that slot.
    """
    if value is None:
        return None
    if isinstance(value, str):
        parts = value.split()
        return parts or None
    if isinstance(value, (list, tuple)):
        parts = [str(x).strip() for x in value if str(x).strip()]
        return parts or None
    return None


def normalize_plugins_config(raw: Any) -> dict[str, dict]:
    """Validate stored config into ``{plugin_name: override_dict}``.

    Accepts either a dict keyed by plugin name (``{"code-intel": {...}}``) or a
    list of ``{"name": ..., ...}`` dicts (the dashboard form's shape). Unknown
    plugin names are kept (so the config round-trips) but only built-in names
    take effect in :func:`resolve_plugins`. Malformed input yields ``{}``.
    """
    if not raw:
        return {}
    items: list[dict] = []
    if isinstance(raw, dict):
        for name, ov in raw.items():
            if isinstance(ov, dict):
                items.append({"name": name, **ov})
            elif ov is None:
                items.append({"name": name})
    elif isinstance(raw, list):
        items = [x for x in raw if isinstance(x, dict) and x.get("name")]
    else:
        return {}

    out: dict[str, dict] = {}
    for it in items:
        name = str(it.get("name") or "").strip()
        if not name:
            continue
        ov: dict[str, Any] = {}
        if "enabled" in it:
            ov["enabled"] = bool(it["enabled"])
        cmd = _coerce_command(it.get("command"))
        if cmd is not None:
            ov["command"] = cmd
        if isinstance(it.get("port"), int) and not isinstance(it.get("port"), bool):
            ov["port"] = it["port"]
        if isinstance(it.get("description"), str) and it["description"].strip():
            ov["description"] = it["description"].strip()
        dov = it.get("description_overrides")
        if isinstance(dov, dict):
            ov["description_overrides"] = {
                str(k): str(v) for k, v in dov.items() if str(k)
            }
        out[name] = ov
    return out


def resolve_plugins(raw_config: Any) -> list[dict]:
    """Merge per-tenant overrides over the built-in defaults.

    Returns the three built-in plugin descriptors in order, each with overrides
    applied (``enabled``, ``command``, ``port``, ``description``,
    ``description_overrides``). The ``slot``/``url_prefix`` of a built-in are
    fixed and never moved by config. An empty/absent config returns the
    defaults verbatim — so existing tunnels behave identically.
    """
    overrides = normalize_plugins_config(raw_config)
    resolved: list[dict] = []
    for base in BUILTIN_PLUGINS:
        ov = overrides.get(base["name"], {})
        merged = dict(base)
        for key in _OVERRIDABLE:
            if key in ov:
                merged[key] = ov[key]
        # slot / url_prefix are immutable for built-ins.
        merged["slot"] = base["slot"]
        merged["url_prefix"] = base["url_prefix"]
        resolved.append(merged)
    return resolved


def active_plugins(raw_config: Any) -> list[dict]:
    """:func:`resolve_plugins` filtered to the enabled entries."""
    return [p for p in resolve_plugins(raw_config) if p.get("enabled")]


def plugin_by_slot(raw_config: Any, slot: str) -> dict | None:
    """Return the resolved plugin riding ``slot`` (fs/code/extract), or None."""
    for p in resolve_plugins(raw_config):
        if p["slot"] == slot:
            return p
    return None
