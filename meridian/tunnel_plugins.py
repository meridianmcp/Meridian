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

# slot = the fixed server transport a plugin rides on. Each built-in owns one
# slot; that mapping is immutable (a config override can't move a built-in to
# another slot, which would collide with the server routes).
SLOTS = ("fs", "code", "extract", "ppt", "word", "dc")

DEFAULT_FS_PORT = 8808
DEFAULT_CODE_PORT = 8809
DEFAULT_EXTRACT_PORT = 8810
DEFAULT_PPT_PORT = 8811
DEFAULT_WORD_PORT = 8812
DEFAULT_DC_PORT = 8813

# The code-extractor slot's default launcher: Serena (LSP-based symbol tools —
# find_symbol / replace_symbol_body, etc.), run ephemerally via uvx. The
# ``{repo_path}`` placeholder is expanded to the tunnel's working directory at
# spawn time (see :func:`expand_command`) — Serena needs ``--project`` to load
# the right repo. Swapping this default is a pure-data change here, no redeploy.
SERENA_EXTRACT_COMMAND: list[str] = [
    "uvx", "serena", "start-mcp-server",
    "--context", "ide-assistant",
    "--project", "{repo_path}",
]

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
        # default: Serena (LSP symbol tools); {repo_path} expanded at spawn time.
        "command": list(SERENA_EXTRACT_COMMAND),
        "description": "Symbol-level code intelligence (Serena LSP)",
        "description_overrides": {},
    },
    {
        "name": "powerpoint",
        "slot": "ppt",
        "port": DEFAULT_PPT_PORT,
        "url_prefix": "/ppt",
        # Office plugins are opt-in: off by default, enabled from the dashboard.
        "enabled": False,
        "builtin": True,
        "command": ["uvx", "powerpoint-mcp"],
        "env": {},
        "description": "PowerPoint authoring (powerpoint-mcp)",
        "description_overrides": {},
    },
    {
        "name": "word",
        "slot": "word",
        "port": DEFAULT_WORD_PORT,
        "url_prefix": "/word",
        "enabled": False,
        "builtin": True,
        "command": ["uvx", "word-mcp-live"],
        "env": {"MCP_AUTHOR": "Adam", "MCP_AUTHOR_INITIALS": "AC"},
        "description": "Word authoring (word-mcp-live)",
        "description_overrides": {},
    },
    {
        "name": "desktop-commander",
        "slot": "dc",
        "port": DEFAULT_DC_PORT,
        "url_prefix": "/dc",
        "enabled": False,
        "builtin": True,
        "command": None,  # spawned via npx @wonderwhy-er/desktop-commander@latest
        "description": "Desktop Commander — system tools, file access, terminal (local only)",
        "description_overrides": {},
    },
]

# Editable per-slot fields that a tenant override may set.
_OVERRIDABLE = ("enabled", "command", "port", "description", "description_overrides", "env")


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


def expand_command(value: Any, *, repo_path: str | None = None) -> list[str] | None:
    """Coerce *value* to a command token list and expand template variables.

    Supports ``{repo_path}`` → *repo_path* (the tunnel's working directory) so a
    plugin command can target the active repo — e.g. Serena's
    ``--project {repo_path}`` (see :data:`SERENA_EXTRACT_COMMAND`). Unknown
    ``{...}`` placeholders are left untouched. Accepts the same shapes as
    :func:`_coerce_command`; ``None``/empty in yields ``None``. Returns a fresh
    list, so module-level command constants are never mutated.
    """
    cmd = _coerce_command(value)
    if cmd is None:
        return None
    rp = repo_path or ""
    return [tok.replace("{repo_path}", rp) for tok in cmd]


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
        env = it.get("env")
        if isinstance(env, dict):
            ov["env"] = {str(k): str(v) for k, v in env.items() if str(k)}
        out[name] = ov
    return out


def resolve_plugins(raw_config: Any, detected_slots: Any = frozenset()) -> list[dict]:
    """Merge per-tenant overrides over the built-in defaults.

    Returns the built-in plugin descriptors in order, each with overrides applied
    (``enabled``, ``command``, ``port``, ``description``, ``description_overrides``,
    ``env``). The ``slot``/``url_prefix`` of a built-in are fixed and never moved
    by config. An empty/absent config returns the defaults verbatim — so existing
    tunnels behave identically.

    ``detected_slots`` are slots whose backing binary was found on the local
    machine (see :func:`detect_office_binaries`). A detected slot is auto-enabled
    **only when the user did not explicitly set ``enabled``** for it, so an
    explicit ``enabled: false`` in the config always wins.
    """
    overrides = normalize_plugins_config(raw_config)
    resolved: list[dict] = []
    for base in BUILTIN_PLUGINS:
        ov = overrides.get(base["name"], {})
        merged = dict(base)
        for key in _OVERRIDABLE:
            if key in ov:
                merged[key] = ov[key]
        # Auto-enable a detected slot unless the user explicitly chose enabled.
        if base["slot"] in detected_slots and "enabled" not in ov:
            merged["enabled"] = True
        # slot / url_prefix are immutable for built-ins.
        merged["slot"] = base["slot"]
        merged["url_prefix"] = base["url_prefix"]
        resolved.append(merged)
    return resolved


# Office slots auto-enable when their MCP launcher is on PATH (sprint 6c2b3562).
OFFICE_BINARIES = {"ppt": "powerpoint-mcp", "word": "word-mcp-live"}


def detect_office_binaries(which: Any = None) -> set[str]:
    """Return the Office slots (``ppt``/``word``) whose binary is on PATH.

    ``which`` defaults to :func:`shutil.which`; injectable for tests.
    """
    if which is None:
        import shutil
        which = shutil.which
    return {slot for slot, binary in OFFICE_BINARIES.items() if which(binary)}


def active_plugins(raw_config: Any, detected_slots: Any = frozenset()) -> list[dict]:
    """:func:`resolve_plugins` filtered to the enabled entries."""
    return [p for p in resolve_plugins(raw_config, detected_slots) if p.get("enabled")]


def plugin_by_slot(raw_config: Any, slot: str) -> dict | None:
    """Return the resolved plugin riding ``slot`` (fs/code/extract), or None."""
    for p in resolve_plugins(raw_config):
        if p["slot"] == slot:
            return p
    return None
