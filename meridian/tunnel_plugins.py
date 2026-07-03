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

# 8fb69d54 — 4 pre-allocated custom slots (p0-p3) on ports 8814-8817 so a custom
# plugin bound to a slot gets a real server route (/tunnel-p0 … /tunnel-p3) and
# appears in the claude.ai connector (closes ecf5b8c6). The server-side slot
# routes/registries live in routes/tunnel.py (_CUSTOM_SLOTS).
CUSTOM_SLOT_PORTS = {"p0": 8814, "p1": 8815, "p2": 8816, "p3": 8817}

# The code-extractor slot's default launcher: Serena (LSP-based symbol tools —
# find_symbol / replace_symbol_body, etc.), run ephemerally via uvx. The
# ``{repo_path}`` placeholder is expanded to the tunnel's working directory at
# spawn time (see :func:`expand_command`) — Serena needs ``--project`` to load
# the right repo. Swapping this default is a pure-data change here, no redeploy.
#
# The distribution is published on PyPI as ``serena-agent`` (the bare ``serena``
# project ships no ``serena`` console script), so we pin ``uvx --from
# serena-agent`` and invoke the ``serena`` entrypoint it provides.
SERENA_EXTRACT_COMMAND: list[str] = [
    "uvx", "--from", "serena-agent", "serena", "start-mcp-server",
    "--context", "claude-code",
    # Don't pop a browser tab to Serena's web dashboard on every tunnel (re)start
    # — the tunnel runs Serena headless behind the proxy. ``--open-web-dashboard
    # false`` is Serena's documented flag and overrides the user's global
    # serena_config.yml, so it applies whether or not they already have one (the
    # legacy native ``gui_log_window`` already defaults off). (a39c4a99)
    "--open-web-dashboard", "false",
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
        # Core tools are always-on and have no enable toggle in the dashboard;
        # plugins (core=False) are opt-in. (b2a60de7)
        "core": True,
        # None → the client uses its platform-aware default builder for this slot.
        "command": None,
        # Tool-name display prefix the client relay prepends to this slot's
        # tools/list entries. MUST stay None: the server-side bridge
        # (routes/tunnel.py list_tunnel_tools) already namespaces every slot's
        # tools via SLOT_DISPLAY_NAMES (fs → "filesystem__read_file"). Setting a
        # client prefix too produces double-prefixed names like
        # "filesystem__Filesystem__read_file" in the claude.ai connector. (49905647)
        "prefix": None,
        # 4ea1b9d5 — "stateless" slots ride mcp-proxy's --stateless flag (each
        # POST handled independently, the default for the one-shot tunnel relay).
        # "persistent" slots (e.g. Desktop Commander) keep a stateful inner
        # process across requests and so omit --stateless + skip the idle-killer.
        "session_mode": "stateless",
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
        "core": True,
        "command": None,  # default: codebase-memory-mcp (auto-installed)
        # codebase-memory-mcp already self-prefixes its tools — leave empty.
        "prefix": None,
        "session_mode": "stateless",
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
        "core": True,
        # default: Serena (LSP symbol tools); {repo_path} expanded at spawn time.
        "command": list(SERENA_EXTRACT_COMMAND),
        # cc904bfe — historical defaults this slot has shipped. A saved override
        # matching one of these (but not the current default) is a stale copy of
        # an old default → the dashboard shows a "newer default available" badge.
        # The extract slot defaulted to `uvx mcp-server-code-extractor` (command
        # None → that builder) before Serena (commit 1428f1b).
        "previous_defaults": [["uvx", "mcp-server-code-extractor"]],
        # MUST stay None — the server bridge namespaces extract-slot tools as
        # "extractor__find_symbol" via SLOT_DISPLAY_NAMES. A client prefix here
        # would double them to "extractor__Serena__find_symbol". (49905647)
        "prefix": None,
        "session_mode": "stateless",
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
        "core": False,
        "command": ["uvx", "powerpoint-mcp"],
        "env": {},
        # powerpoint-mcp self-prefixes its tools — leave empty to avoid doubling.
        "prefix": None,
        "session_mode": "stateless",
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
        "core": False,
        "command": ["uvx", "--from", "word-mcp-live", "word_mcp_server.exe"],
        "env": {"MCP_AUTHOR": "Adam", "MCP_AUTHOR_INITIALS": "AC"},
        # word-mcp-live self-prefixes its tools — leave empty.
        "prefix": None,
        "session_mode": "stateless",
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
        "core": False,
        "command": None,  # spawned via npx @wonderwhy-er/desktop-commander@latest
        # Desktop Commander self-prefixes its tools — leave empty.
        "prefix": None,
        # 4ea1b9d5 — DC runs stateful terminal sessions: persistent so the inner
        # process survives across requests (no --stateless, no idle-kill).
        "session_mode": "persistent",
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


def _iter_plugin_items(raw: Any) -> list[dict]:
    """Normalize a stored config into an ordered list of ``{"name", ...}`` dicts.

    Accepts the dict-keyed form (``{"code-intel": {...}}``) or the list form (the
    dashboard's shape). Order is preserved so first-occurrence dedup is possible
    (see :func:`resolve_custom_plugins`). Non-dict / garbage input yields ``[]``.
    """
    if not raw:
        return []
    items: list[dict] = []
    if isinstance(raw, dict):
        for name, ov in raw.items():
            if isinstance(ov, dict):
                items.append({"name": name, **ov})
            elif ov is None:
                items.append({"name": name})
    elif isinstance(raw, list):
        items = [x for x in raw if isinstance(x, dict) and x.get("name")]
    return items


def normalize_plugins_config(raw: Any) -> dict[str, dict]:
    """Validate stored config into ``{plugin_name: override_dict}``.

    Accepts either a dict keyed by plugin name (``{"code-intel": {...}}``) or a
    list of ``{"name": ..., ...}`` dicts (the dashboard form's shape). Unknown
    plugin names are kept (so the config round-trips) but only built-in names
    take effect in :func:`resolve_plugins`. Malformed input yields ``{}``.
    """
    out: dict[str, dict] = {}
    for it in _iter_plugin_items(raw):
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


# Built-in default ports — a custom plugin may not reuse one, or its local proxy
# would collide with the built-in slot riding that port (see resolve_custom_plugins).
_BUILTIN_DEFAULT_PORTS = frozenset({
    DEFAULT_FS_PORT, DEFAULT_CODE_PORT, DEFAULT_EXTRACT_PORT,
    DEFAULT_PPT_PORT, DEFAULT_WORD_PORT, DEFAULT_DC_PORT,
})


def resolve_custom_plugins(raw_config: Any) -> list[dict]:
    """Resolve user-defined (non-built-in) plugins from the stored config.

    A custom plugin is a config entry whose ``name`` is **not** a built-in name
    (see :func:`builtin_names`), carrying its own ``command`` and a local
    ``port``. Unlike the built-ins these are **LOCAL-ONLY** (pinned architectural
    decision): they ride a local mcp-proxy port + the local ``.mcp.json`` and
    have no server route — they never appear in the claude.ai connector. The
    tunnel client spawns each enabled one behind ``_build_proxy_for_inner`` and
    points the local MCP config at its ``http://127.0.0.1:<port>`` proxy.

    Returns validated descriptors
    ``{"name", "command" (list[str]), "port" (int), "enabled" (bool),
    "builtin": False, "custom": True}``. Invalid entries are dropped silently
    (they simply don't run); validation rules:

    - ``name``: non-empty (stripped), not a built-in name, unique (first wins).
    - ``command``: coerced via :func:`_coerce_command`; must be non-empty. The
      ``{repo_path}`` template is left intact here — expansion happens in the
      client at spawn time via :func:`expand_command`.
    - ``port``: a real ``int`` (``bool`` rejected), in 1024–65535, and not one of
      the built-in default ports (8808–8813) so a custom proxy can't collide
      with a built-in slot.

    Pure: no I/O, no subprocess. An empty/absent/garbage config yields ``[]``.
    """
    builtins = set(builtin_names())
    out: list[dict] = []
    seen: set[str] = set()
    # Walk the raw items in their original order (not via normalize_plugins_config,
    # whose dict collapse would make the *last* duplicate win) so dedup is first-wins.
    for it in _iter_plugin_items(raw_config):
        name = str(it.get("name") or "").strip()
        if not name or name in builtins or name in seen:
            continue
        cmd = _coerce_command(it.get("command"))
        if not cmd:
            continue
        port = it.get("port")
        if not isinstance(port, int) or isinstance(port, bool):
            continue
        if not (1024 <= port <= 65535) or port in _BUILTIN_DEFAULT_PORTS:
            continue
        seen.add(name)
        out.append({
            "name": name,
            "command": cmd,
            "port": port,
            "enabled": bool(it.get("enabled", True)),
            "builtin": False,
            "custom": True,
        })
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
        # cc904bfe — flag a stale custom command override: the tenant saved a
        # `command` that matches a *previous* built-in default for this slot (so
        # it was a copy of the old default, now superseded by a new one). The
        # tunnel still runs the override, but the dashboard surfaces a "newer
        # default available" badge so the user can opt back into the new default.
        # A genuinely-custom command (not in previous_defaults) is left untouched.
        ov_cmd = ov.get("command")
        prev_defaults = base.get("previous_defaults") or []
        if (ov_cmd and ov_cmd != base.get("command")
                and any(ov_cmd == pd for pd in prev_defaults)):
            merged["stale_override"] = True
            merged["newer_default_command"] = base.get("command")
            merged["newer_default_label"] = base.get("description")
        merged.pop("previous_defaults", None)  # internal — don't leak to clients
        resolved.append(merged)
    return resolved


def parse_plugins_by_host(raw: Any) -> dict[str, Any]:
    """8660d701 — parse ``tenants.tunnel_plugins_by_host`` into ``{hostname: config}``.

    Tolerant of None / empty / malformed JSON / non-dict input (all → ``{}``), so a
    junk value never breaks tunnel resolution.
    """
    import json
    val: Any = raw
    if isinstance(raw, str):
        if not raw.strip():
            return {}
        try:
            val = json.loads(raw)
        except Exception:  # noqa: BLE001
            return {}
    if not isinstance(val, dict):
        return {}
    return {str(k): v for k, v in val.items() if str(k)}


def select_host_config(default_config: Any, by_host_raw: Any, hostname: str | None) -> Any:
    """8660d701 — the effective tunnel-plugins config for one machine.

    Returns the machine's per-host config when ``hostname`` has an entry in
    ``by_host_raw`` (``tenants.tunnel_plugins_by_host``); otherwise the per-tenant
    default ``default_config`` (already-parsed ``tunnel_plugins``). This is how a
    machine running ``meridian --tunnel`` gets its own config while existing
    single-machine tenants keep working unchanged.
    """
    if hostname:
        by_host = parse_plugins_by_host(by_host_raw)
        if hostname in by_host:
            return by_host[hostname]
    return default_config


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
