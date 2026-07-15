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

from pathlib import Path
from typing import Any

# 1b3a2c23 — meridian-docs lives in the local extensions/ directory; it is NOT
# published to PyPI, so `uvx meridian-docs` (bare package name) fails to install.
# Use `uvx --from <local-path> meridian-docs-mcp` to run from the checked-out source.
# 58a044c7 — entry-point renamed to "meridian-docs-mcp" (not "meridian-docs"):
# when command name == package name, uvx looks up the command as a PyPI package
# after installing from --from, failing with "not found in the package registry".
# Path(__file__).parent.parent is the repo root (meridian/ → repo root).
_MERIDIAN_DOCS_LOCAL_PATH: str = str(
    Path(__file__).parent.parent / "extensions" / "meridian-docs"
)

# slot = the fixed server transport a plugin rides on. Each built-in owns one
# slot; that mapping is immutable (a config override can't move a built-in to
# another slot, which would collide with the server routes).
SLOTS = ("fs", "code", "extract", "ppt", "word", "dc", "docs", "zotero")

DEFAULT_FS_PORT = 8808
DEFAULT_CODE_PORT = 8809
DEFAULT_EXTRACT_PORT = 8810
DEFAULT_PPT_PORT = 8811
DEFAULT_WORD_PORT = 8812
DEFAULT_DC_PORT = 8813
# 9665538a — the meridian-docs slot: the extracted stdlib-only OOXML (DOCX)
# parser living at extensions/meridian-docs and launched as an MCP server via
# `uvx --from <local-path> meridian-docs-mcp`. Distinct from the `word` slot
# (docx-mcp, authoring): this is fast, dependency-free document *intelligence*
# — outline/parse/index/search. (1b3a2c23: NOT on PyPI; spawn from local path.)
# Port 8818 sits just after the dc slot (8813) and the 4 pre-allocated custom
# slots (8814-8817), and below the custom auto-assign start (8820) — see
# CUSTOM_SLOT_PORTS / _CUSTOM_PORT_START.
DEFAULT_DOCS_PORT = 8818
# 39c117b1 — zotero-mcp slot: citation/reference-manager resolution against the
# user's LOCAL Zotero API (`uvx zotero-mcp`, env ZOTERO_LOCAL=true), bridged the
# same automatic way as docx-mcp/meridian-docs. Port 8819 sits between docs
# (8818) and the custom auto-assign start (8820).
DEFAULT_ZOTERO_PORT = 8819

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
        # ba02a1f7 — swapped word-mcp-live -> docx-mcp-server. 5b065c2e —
        # swapped docx-mcp-server -> docx-mcp: the `uvx docx-mcp` package spawns a
        # real MCP stdio server (self-reports name "FinalCompleteDocxProcessor"
        # v3.4.3) and is the tool Adam selected for the word slot in the Document-
        # Intelligence arch plan. Both are uvx-installable + cross-platform; docx-mcp
        # is python-docx-based (tradeoff vs the more-mature docx-mcp-server 0.7.4
        # recorded in a pinned decision). A tenant who saved either OLD default as an
        # override is flagged stale via previous_defaults below (cc904bfe badge).
        "command": ["uvx", "docx-mcp"],
        "previous_defaults": [["uvx", "docx-mcp-server"], ["uvx", "word-mcp-live"]],
        "env": {"MCP_AUTHOR": "Adam", "MCP_AUTHOR_INITIALS": "AC"},
        # docx-mcp exposes bare tool names (create_document, ...) — no self-prefix.
        "prefix": None,
        "session_mode": "stateless",
        "description": "Word / DOCX authoring (docx-mcp)",
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
    {
        # 9665538a — meridian-docs: the extracted stdlib-only OOXML doc parser
        # (extensions/meridian-docs), launched via `uvx --from <local-path>
        # meridian-docs-mcp`. Opt-in like the Office slots. Complements the `word`
        # slot (docx-mcp authoring) with read-only document intelligence.
        # 1b3a2c23 — NOT published to PyPI; spawn from the local extensions/
        # meridian-docs directory via `uvx --from` so this works out-of-the-box
        # without a separate PyPI publish step.
        # 58a044c7 — entry-point is "meridian-docs-mcp" (not "meridian-docs"): the
        # name-match between package and command caused uvx to attempt a PyPI lookup
        # for the command after installing from the local path, failing with
        # "meridian-docs was not found in the package registry".
        "name": "meridian-docs",
        "slot": "docs",
        "port": DEFAULT_DOCS_PORT,
        "url_prefix": "/docs",
        "enabled": False,
        "builtin": True,
        "core": False,
        # 58a044c7 — entry-point renamed to "meridian-docs-mcp" (distinct from the
        # package name "meridian-docs") to prevent uvx from treating the trailing
        # command argument as a PyPI package lookup. See extensions/meridian-docs/
        # pyproject.toml for the full rationale.
        "command": ["uvx", "--from", _MERIDIAN_DOCS_LOCAL_PATH, "meridian-docs-mcp"],
        "env": {},
        # meridian-docs exposes bare tool names (document_outline, parse_document,
        # …) — no self-prefix, so the server bridge namespaces them via
        # SLOT_DISPLAY_NAMES ("docs" → "meridian-docs__document_outline").
        "prefix": None,
        "session_mode": "stateless",
        "description": "Document intelligence — DOCX outline/parse/index/search (meridian-docs)",
        "description_overrides": {},
    },
    {
        # 39c117b1 — zotero-mcp: citation / reference-manager resolution against
        # the user's LOCAL Zotero API, launched via `uvx zotero-mcp` with
        # ZOTERO_LOCAL=true. A core/default bundled slot (same tier as docx-mcp /
        # meridian-docs), NOT tunnel-proxying Meridian's own hand-rolled
        # zotero_client — so a thesis in a .docx gets real Zotero resolution the
        # same automatic way as the other Office slots.
        "name": "zotero-mcp",
        "slot": "zotero",
        "port": DEFAULT_ZOTERO_PORT,
        "url_prefix": "/zotero",
        "enabled": False,
        "builtin": True,
        "core": False,
        "command": ["uvx", "zotero-mcp"],
        "env": {"ZOTERO_LOCAL": "true"},
        # zotero-mcp exposes bare tool names — no self-prefix, so the server
        # bridge namespaces them via SLOT_DISPLAY_NAMES ("zotero" → "zotero-mcp__…").
        "prefix": None,
        "session_mode": "stateless",
        "description": "Citation / reference-manager resolution against the local Zotero API (zotero-mcp)",
        "description_overrides": {},
    },
]

# Editable per-slot fields that a tenant override may set.
# 39aae23f — ``pool`` is the elastic backend-copy config for a stateless slot
# (min/max copies to load-balance behind the slot's single fixed route). It only
# takes effect for ``session_mode="stateless"`` slots — see :func:`slot_pool_config`.
_OVERRIDABLE = ("enabled", "command", "port", "description", "description_overrides", "env", "pool")

# 39aae23f — elastic-pool defaults for a stateless slot. Start conservative: one
# always-on copy, burst to a second under load. Kept in sync with
# meridian.slot_pool.DEFAULT_MIN_COPIES / DEFAULT_MAX_COPIES (that module owns the
# runtime pool; this is the config-layer default so an unset config means "1 copy,
# burst to 2" without importing the pool module here).
DEFAULT_POOL_MIN_COPIES = 1
DEFAULT_POOL_MAX_COPIES = 2


def builtin_names() -> tuple[str, ...]:
    """Names of the three built-in plugins, in display order."""
    return tuple(p["name"] for p in BUILTIN_PLUGINS)


# a8a54fe9 — the general "bundle the known plugin tools as first-class built-ins"
# catalog: a single declarative source of truth naming every MCP plugin tool
# Meridian knows how to run, its install runtime, the built-in slot it rides (or
# None if it is not yet a first-class built-in), and whether it is bundled today.
#
# Why a catalog and not more slots? The server exposes a FIXED set of transport
# slots (fs/code/extract/ppt/word/dc + the 4 pre-allocated custom p0-p3 — see
# routes/tunnel.py). A built-in plugin can only exist for a slot that already has
# a dedicated server route; adding a brand-new built-in slot is a cross-file
# server change, deliberately out of scope for this pure registry module (see the
# module docstring: "It does NOT add new server routes per arbitrary plugin").
#
# So this catalog's job is to make the bundling *state* explicit and machine-
# readable — the dashboard / docs can enumerate "every known plugin tool and
# which are first-class built-ins vs. still pending" — without pretending to wire
# a slot that has no server route. Each not-yet-bundled entry carries the sprint
# item that owns its wiring, so this catalog documents the gap rather than
# duplicating those items' implementation:
#
#   - ``meridian-docs`` (the extracted stdlib-only OOXML fast parser) → owned by
#     item 9665538a (wire it as its own command alongside docx-mcp on the word
#     slot, or its own slot). It is NOT the same tool as ``docx-mcp`` (the editor
#     that already rides the word slot); it is the read/parse layer.
#   - ``zotero-mcp`` (citation / reference-manager resolution against the local
#     Zotero API) → owned by item 39c117b1 (needs tunnel-proxying like the other
#     slots, or an honest hosted-mode error). No server route exists for it yet.
#
# ``runtime`` names the launcher a bundling would use (``uvx`` for PyPI tools,
# ``npx`` for npm tools, ``binary`` for the auto-downloaded native code-intel
# binary), so a future bundling item and the dashboard agree on prereqs.
KNOWN_PLUGIN_TOOLS: list[dict[str, Any]] = [
    {
        "name": "filesystem",
        "package": "@modelcontextprotocol/server-filesystem",
        "runtime": "npx",
        "slot": "fs",
        "bundled": True,
        "owner_item": None,
        "description": "Filesystem MCP (read/write files in the served repo).",
    },
    {
        "name": "code-intel",
        "package": "codebase-memory-mcp",
        "runtime": "binary",
        "slot": "code",
        "bundled": True,
        "owner_item": None,
        "description": "Code-intelligence graph (codebase-memory-mcp).",
    },
    {
        "name": "code-extractor",
        "package": "serena-agent",
        "runtime": "uvx",
        "slot": "extract",
        "bundled": True,
        "owner_item": None,
        "description": "Symbol-level code intelligence (Serena LSP).",
    },
    {
        "name": "powerpoint",
        "package": "powerpoint-mcp",
        "runtime": "uvx",
        "slot": "ppt",
        "bundled": True,
        "owner_item": None,
        "description": "PowerPoint authoring (powerpoint-mcp).",
    },
    {
        "name": "word",
        "package": "docx-mcp",
        "runtime": "uvx",
        "slot": "word",
        "bundled": True,
        "owner_item": None,
        "description": "Word / DOCX authoring/editing (docx-mcp).",
    },
    {
        "name": "desktop-commander",
        "package": "@wonderwhy-er/desktop-commander",
        "runtime": "npx",
        "slot": "dc",
        "bundled": True,
        "owner_item": None,
        "description": "Desktop Commander — system tools, terminal (local only).",
    },
    {
        "name": "meridian-docs",
        "package": "meridian-docs",
        "runtime": "uvx",
        # 9665538a SHIPPED this as a first-class built-in on its own `docs` slot
        # (server route + WS relay in routes/tunnel.py). Now bundled — distinct
        # from docx-mcp (the editor on the word slot); this is the read layer.
        "slot": "docs",
        "bundled": True,
        "owner_item": None,
        "description": (
            "Standalone OOXML/DOCX fast parser (the read layer extracted from "
            "Meridian) — distinct from docx-mcp, which is the editor."
        ),
    },
    {
        "name": "zotero-mcp",
        "package": "zotero-mcp",
        "runtime": "uvx",
        # 39c117b1 SHIPPED this as a first-class built-in on its own `zotero` slot
        # (server route + WS relay in routes/tunnel.py), env ZOTERO_LOCAL=true.
        "slot": "zotero",
        "bundled": True,
        "owner_item": None,
        "description": (
            "Citation / reference-manager resolution against the local Zotero API."
        ),
    },
    {
        # 88dbb675 — Context7 (by Upstash): general-purpose library/framework docs MCP.
        # Indexes React, Tailwind, Next.js, and thousands of other libraries so agents
        # get up-to-date API docs without web search. Complements paper_search (academic)
        # and the GitHub source (code/issues) by covering framework documentation.
        #
        # Connection options (no API key needed for the free tier):
        #   Remote (preferred): npx -y mcp-remote https://mcp.context7.com/mcp
        #   Local stdio:        npx -y @upstash/context7-mcp
        #   Optional env:       CONTEXT7_API_KEY — free key from context7.com/dashboard
        #                       unlocks higher rate limits; omit for basic free-tier use.
        #
        # NOT a tunnel built-in: Context7 is a remote-first MCP with no local binary,
        # so it has no dedicated server route (no slot). Wire it as a custom plugin in
        # the Meridian dashboard → Tunnel → Add Plugin, or add it directly to your
        # .mcp.json / claude_desktop_config.json. See AGENTS.md for connection examples.
        "name": "context7",
        "package": "@upstash/context7-mcp",
        "runtime": "npx",
        "slot": None,  # no dedicated tunnel slot — wire as a custom plugin or direct MCP
        "bundled": False,
        "owner_item": "88dbb675",
        "description": (
            "Library/framework docs MCP (Context7 by Upstash) — up-to-date React, "
            "Tailwind, Next.js, and thousands of other library docs for AI agents. "
            "Use as a custom tunnel plugin (npx @upstash/context7-mcp) or connect "
            "directly via the remote endpoint https://mcp.context7.com/mcp. "
            "Free tier requires no API key; generate one at context7.com/dashboard "
            "for higher rate limits."
        ),
    },
]


def known_plugin_tools() -> list[dict[str, Any]]:
    """a8a54fe9 — the known-plugin-tools catalog (fresh copies, safe to mutate).

    A stable, ordered list of every MCP plugin tool Meridian knows how to run,
    each tagged with its ``slot`` (or ``None``), ``bundled`` state, and — for the
    not-yet-bundled ones — the ``owner_item`` sprint id that owns its wiring.
    Returns shallow copies so callers can annotate entries without mutating the
    module-level catalog.
    """
    return [dict(t) for t in KNOWN_PLUGIN_TOOLS]


def bundled_plugin_tools() -> list[dict[str, Any]]:
    """The subset of :func:`known_plugin_tools` already shipped as built-in slots.

    Invariant (enforced by tests): a tool is ``bundled`` iff it rides a real
    built-in slot — i.e. its ``slot`` is one of :data:`SLOTS` and its ``name``
    matches a :func:`builtin_names` entry. So this is exactly the set of plugin
    tools a user gets for free, no manual MCP wiring.
    """
    return [t for t in known_plugin_tools() if t["bundled"]]


def unbundled_plugin_tools() -> list[dict[str, Any]]:
    """Known plugin tools NOT yet first-class built-ins (the remaining gap).

    Each carries a non-null ``owner_item`` naming the sprint item that owns its
    bundling, so this method reports precisely which parts of the general
    "bundle everything" gap still belong to a dedicated item (meridian-docs →
    9665538a, zotero-mcp → 39c117b1) rather than silently pretending they ship.
    """
    return [t for t in known_plugin_tools() if not t["bundled"]]


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
        # 39aae23f — per-slot elastic-pool override. Accept an int ("pool up to N
        # copies" shorthand) or a {"enabled"?, "min"/"min_copies", "max"/
        # "max_copies"} dict; normalize into a canonical dict here so downstream
        # readers (slot_pool_config) get a stable shape. bool is not a size.
        pool = _normalize_pool_override(it.get("pool"))
        if pool is not None:
            ov["pool"] = pool
        out[name] = ov
    return out


def _normalize_pool_override(raw: Any) -> "dict | None":
    """39aae23f — normalize a per-slot ``pool`` override into a canonical dict.

    Returns ``None`` when *raw* carries nothing usable (so the slot keeps the
    built-in default sizing). Otherwise returns a dict that may contain any of
    ``enabled`` (bool), ``min`` (int >= 1), ``max`` (int >= 1):

    * an ``int`` N → ``{"max": N}`` ("pool up to N copies"); N<=1 disables pooling.
    * a ``dict`` → its ``enabled`` / ``min``|``min_copies`` / ``max``|``max_copies``
      keys, ints only (``bool`` rejected — it is not a size).

    The min<=max invariant and the stateless-slot gate are applied later by
    :func:`slot_pool_config` (which also owns the numeric floor/clamp), so this
    stays a pure shape-normalizer.
    """
    if isinstance(raw, bool):
        # A bare bool toggles pooling on/off without changing sizes.
        return {"enabled": raw}
    if isinstance(raw, int):
        return {"max": raw}
    if not isinstance(raw, dict):
        return None
    out: dict[str, Any] = {}
    if "enabled" in raw:
        out["enabled"] = bool(raw["enabled"])
    lo = raw.get("min", raw.get("min_copies"))
    if isinstance(lo, int) and not isinstance(lo, bool):
        out["min"] = lo
    hi = raw.get("max", raw.get("max_copies"))
    if isinstance(hi, int) and not isinstance(hi, bool):
        out["max"] = hi
    return out or None


def slot_pool_config(plugin: Any) -> "dict":
    """39aae23f — the effective elastic-pool config for one resolved slot plugin.

    Returns ``{"enabled": bool, "min": int, "max": int}``. Pooling load-balances
    N identical inner backend copies behind the slot's single FIXED server route
    (see :mod:`meridian.slot_pool`), so it is only ever offered for
    ``session_mode="stateless"`` slots — a ``persistent`` slot (Desktop Commander)
    always resolves to ``enabled=False, min=1, max=1`` regardless of any override,
    because forking identical copies would split its per-session state.

    For a stateless slot:

    * With no ``pool`` override, pooling is ON with the built-in defaults
      (:data:`DEFAULT_POOL_MIN_COPIES` / :data:`DEFAULT_POOL_MAX_COPIES` = 1/2) —
      an unset config still gets elastic burst-to-2.
    * An override's ``min``/``max`` clamp to ``1 <= min <= max`` (min floors at 1;
      max is raised to min if inverted).
    * ``enabled: false`` (or a resolved ``max <= 1``) collapses the slot to a
      single copy (``enabled=False, min=1, max=1``) — i.e. today's behaviour.

    Pure — no I/O. ``plugin`` is a resolved plugin dict (from
    :func:`resolve_plugins`); a non-dict yields the single-copy default.
    """
    single = {"enabled": False, "min": 1, "max": 1}
    if not isinstance(plugin, dict):
        return single
    # Only stateless slots may pool. Default missing session_mode to "stateless"
    # so a resolved built-in without the key (shouldn't happen) still behaves.
    if plugin.get("session_mode", "stateless") != "stateless":
        return single

    ov = plugin.get("pool")
    lo = DEFAULT_POOL_MIN_COPIES
    hi = DEFAULT_POOL_MAX_COPIES
    enabled_override: bool | None = None
    if isinstance(ov, dict):
        if "enabled" in ov:
            enabled_override = bool(ov["enabled"])
        if isinstance(ov.get("min"), int) and not isinstance(ov.get("min"), bool):
            lo = ov["min"]
        if isinstance(ov.get("max"), int) and not isinstance(ov.get("max"), bool):
            hi = ov["max"]
    elif isinstance(ov, int) and not isinstance(ov, bool):
        hi = ov

    lo = max(1, lo)
    hi = max(1, hi)
    if hi < lo:
        hi = lo
    # Pooling is meaningful only when we can run >1 copy. An explicit enabled=False
    # (or a size that never exceeds one copy) collapses to the single-copy default.
    enabled = (hi > 1) if enabled_override is None else (enabled_override and hi > 1)
    if not enabled:
        return single
    return {"enabled": True, "min": lo, "max": hi}


# Built-in default ports — a custom plugin may not reuse one, or its local proxy
# would collide with the built-in slot riding that port (see resolve_custom_plugins).
_BUILTIN_DEFAULT_PORTS = frozenset({
    DEFAULT_FS_PORT, DEFAULT_CODE_PORT, DEFAULT_EXTRACT_PORT,
    DEFAULT_PPT_PORT, DEFAULT_WORD_PORT, DEFAULT_DC_PORT, DEFAULT_DOCS_PORT,
    DEFAULT_ZOTERO_PORT,
})

# 9811d04c — first port a freshly-added custom plugin (from the browse "Add"
# button) is auto-assigned when the caller supplies no port. Starts just above
# the built-in default range (8808–8817, incl. the 4 pre-allocated custom
# slots), so an auto-assigned port never collides with a built-in slot.
_CUSTOM_PORT_START = 8820

# 9811d04c — the built-in *slot* names (fs/code/extract/ppt/word/dc). A custom
# plugin's name must not collide with a built-in slot name (task rule) nor with a
# built-in plugin's display name (filesystem/code-intel/… — see builtin_names),
# since either is a slot override rather than a genuine custom plugin.
_RESERVED_CUSTOM_NAMES = frozenset(SLOTS)

# Safe custom-plugin name charset: letters, digits, dash, underscore, dot. The
# name becomes an ``.mcp.json`` key (``meridian-custom-<name>``) and part of a
# local proxy identity, so we keep it to a conservative, shell/JSON-safe set.
import re as _re
_CUSTOM_NAME_RE = _re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def is_reserved_custom_name(name: Any) -> bool:
    """9811d04c — True if *name* collides with a built-in slot or plugin name.

    A custom plugin may not be named like a built-in slot (fs/code/extract/ppt/
    word/dc) or a built-in plugin (filesystem/code-intel/…): such a name is a slot
    override, never a stand-alone custom plugin, and ``resolve_custom_plugins``
    would silently drop it. Case-insensitive on the stripped value.
    """
    n = str(name or "").strip().lower()
    if not n:
        return False
    return n in _RESERVED_CUSTOM_NAMES or n in {b.lower() for b in builtin_names()}


def validate_custom_plugin(
    name: Any, command: Any, port: Any = None,
    *, existing_ports: Any = (), env: Any = None,
) -> "tuple[dict | None, str | None]":
    """9811d04c — validate one custom-plugin selection, returning ``(entry, error)``.

    Exactly one of the pair is non-None. On success ``entry`` is a normalized
    ``{"name", "command" (list[str]), "port" (int), "enabled": True}`` dict (plus
    an ``"env"`` dict when supplied and non-empty), ready to merge into the stored
    ``tunnel_plugins`` config; ``error`` is None. On failure ``entry`` is None and
    ``error`` is a short human-readable reason.

    Rules (mirror :func:`resolve_custom_plugins`, but *reject* instead of silently
    dropping so the add API can report why):

    - ``name``: stripped, matches :data:`_CUSTOM_NAME_RE` (letters/digits plus
      ``._-``, ≤64 chars), and not a built-in slot/plugin name.
    - ``command``: coerced via :func:`_coerce_command`; must be non-empty.
    - ``port``: when given, a real ``int`` (``bool`` rejected) in 1024–65535, not a
      built-in default port (8808–8813) and not already in ``existing_ports``. When
      omitted/None, the first free port at/after :data:`_CUSTOM_PORT_START` that
      avoids the built-in ports and ``existing_ports`` is auto-assigned.
    - ``env`` (optional): a dict → coerced to ``{str: str}`` (blank keys dropped);
      attached only when non-empty.
    """
    nm = str(name or "").strip()
    if not nm:
        return None, "name is required"
    if not _CUSTOM_NAME_RE.match(nm):
        return None, (
            "name must be 1–64 chars of letters, digits, dot, dash or underscore "
            "(and start alphanumeric)"
        )
    if is_reserved_custom_name(nm):
        return None, f"'{nm}' collides with a built-in slot/plugin name"

    cmd = _coerce_command(command)
    if not cmd:
        return None, "command is required"

    used = {p for p in existing_ports if isinstance(p, int) and not isinstance(p, bool)}
    if port is None or port == "":
        assigned: int | None = None
        for candidate in range(_CUSTOM_PORT_START, 65536):
            if candidate in _BUILTIN_DEFAULT_PORTS or candidate in used:
                continue
            assigned = candidate
            break
        if assigned is None:  # pragma: no cover — 57k-port exhaustion is unreachable
            return None, "no free port available"
        port_int = assigned
    else:
        if not isinstance(port, int) or isinstance(port, bool):
            return None, "port must be an integer"
        if not (1024 <= port <= 65535):
            return None, "port must be in 1024–65535"
        if port in _BUILTIN_DEFAULT_PORTS:
            return None, "port collides with a built-in slot (8808–8813)"
        if port in used:
            return None, f"port {port} is already used by another custom plugin"
        port_int = port

    entry: dict[str, Any] = {
        "name": nm, "command": cmd, "port": port_int, "enabled": True,
    }
    if isinstance(env, dict):
        coerced = {str(k): str(v) for k, v in env.items() if str(k)}
        if coerced:
            entry["env"] = coerced
    return entry, None


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
    "builtin": False, "custom": True}``, plus an optional ``"env"`` dict when the
    entry carries valid environment overrides. Invalid entries are dropped
    silently (they simply don't run); validation rules:

    - ``name``: non-empty (stripped), not a built-in name, unique (first wins).
    - ``command``: coerced via :func:`_coerce_command`; must be non-empty. The
      ``{repo_path}`` template is left intact here — expansion happens in the
      client at spawn time via :func:`expand_command`.
    - ``port``: a real ``int`` (``bool`` rejected), in 1024–65535, and not one of
      the built-in default ports (8808–8813) so a custom proxy can't collide
      with a built-in slot.
    - ``env`` (optional): a dict of ``{VAR: value}`` (keys/values coerced to
      str, blank keys dropped) merged into the plugin's spawn environment by the
      client — e.g. ``{"ZOTERO_LOCAL": "true"}`` for a local Zotero MCP. A
      non-dict or empty env is omitted so the descriptor shape is unchanged.

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
        descriptor: dict[str, Any] = {
            "name": name,
            "command": cmd,
            "port": port,
            "enabled": bool(it.get("enabled", True)),
            "builtin": False,
            "custom": True,
        }
        # ba02a1f7/194a7776 — carry optional per-plugin env (e.g. a local
        # Zotero MCP needs ZOTERO_LOCAL=true). Only attach when it coerces to a
        # non-empty dict, so entries without env keep the historical shape.
        env = it.get("env")
        if isinstance(env, dict):
            coerced = {str(k): str(v) for k, v in env.items() if str(k)}
            if coerced:
                descriptor["env"] = coerced
        seen.add(name)
        out.append(descriptor)
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
OFFICE_BINARIES = {"ppt": "powerpoint-mcp", "word": "docx-mcp"}


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
