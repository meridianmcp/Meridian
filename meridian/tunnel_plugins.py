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

import hashlib
import json
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

# 469d89b4 — meridian-outputs lives in the local extensions/ directory; it is NOT
# published to PyPI, so `uvx meridian-outputs` (bare package name) fails to install.
# Use `uvx --from <local-path> meridian-outputs-mcp` to run from the checked-out source.
# The entry-point is "meridian-outputs-mcp" (NOT "meridian-outputs") for the same
# uvx name-collision reason documented in 58a044c7 above — see extensions/meridian-
# outputs/pyproject.toml [project.scripts] for the full rationale.
_MERIDIAN_OUTPUTS_LOCAL_PATH: str = str(
    Path(__file__).parent.parent / "extensions" / "meridian-outputs"
)

# slot = the fixed server transport a plugin rides on. Each built-in owns one
# slot; that mapping is immutable (a config override can't move a built-in to
# another slot, which would collide with the server routes).
SLOTS = ("fs", "code", "extract", "ppt", "word", "dc", "docs", "zotero", "outputs", "debug")

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
# (8818) and the outputs slot (8820).
DEFAULT_ZOTERO_PORT = 8819
# 469d89b4 — meridian-outputs slot: local BM25 outputs index (CSV/JSON/NPY) via
# `uvx --from <local-path> meridian-outputs-mcp`. Port 8820 sits just after
# zotero (8819) and was previously the custom auto-assign start; _CUSTOM_PORT_START
# is bumped to 8821 below so auto-assigned custom ports never collide with this slot.
DEFAULT_OUTPUTS_PORT = 8820

# 121e6a27 — mcp-debugger slot: @debugmcp/mcp-debugger, a 7-language DAP
# (Debug Adapter Protocol) debugger, launched via `npx -y @debugmcp/mcp-debugger`
# (npm-published, no local clone needed — unlike the meridian-docs/meridian-outputs
# slots, which spawn from a local extensions/ checkout). Port 8821 sits just after
# outputs (8820); _CUSTOM_PORT_START is bumped to 8822 below so auto-assigned
# custom ports never collide with this slot.
DEFAULT_DEBUG_PORT = 8821

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
        #
        # e99b09e9 — also backfilled the two historical Serena command shapes
        # so a tenant who saved an override while either was the live default
        # gets the "newer default available" badge too (not just silent
        # headless enforcement via ensure_serena_headless, which already
        # covers the actual behavior regardless of this list):
        #   - pre-344dd5e: no --open-web-dashboard flag at all (would have
        #     popped the GUI dashboard on every tunnel start).
        #   - post-344dd5e / pre-744d191: headless flag present, but
        #     --context ide-assistant (deprecated name, since renamed to
        #     claude-code).
        "previous_defaults": [
            ["uvx", "mcp-server-code-extractor"],
            [
                "uvx", "--from", "serena-agent", "serena", "start-mcp-server",
                "--context", "ide-assistant",
                "--project", "{repo_path}",
            ],
            [
                "uvx", "--from", "serena-agent", "serena", "start-mcp-server",
                "--context", "ide-assistant",
                "--open-web-dashboard", "false",
                "--project", "{repo_path}",
            ],
        ],
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
        # 4b26c2ef (2026-07-29 trace) — RETIRED: resolve_plugins() forces this
        # slot's resolved `enabled` to False unconditionally — even when a
        # tenant's stored tunnel_plugins override explicitly sets
        # `enabled: true`, and even when detect_office_binaries() finds
        # docx-mcp on PATH. The descriptor is kept here (command/port/env/etc.
        # still resolve normally) purely for route/backward compatibility —
        # routes/tunnel.py still serves the fixed /word route rather than
        # 404ing an old client — it is just never spawned. Use the `docs` slot
        # (meridian-docs) for Meridian-aware DOCX work instead. See
        # migrate_retired_overrides() below for the accompanying cleanup of
        # stale per-tenant/per-host `word: {enabled: true}` overrides.
        "retired": True,
        # ba02a1f7 — swapped word-mcp-live -> docx-mcp-server. 5b065c2e —
        # swapped docx-mcp-server -> docx-mcp: the `uvx docx-mcp` package spawns a
        # real MCP stdio server (self-reports name "FinalCompleteDocxProcessor"
        # v3.4.3) and is the tool Adam selected for the word slot in the Document-
        # Intelligence arch plan. Both are uvx-installable + cross-platform; docx-mcp
        # is python-docx-based (tradeoff vs the more-mature docx-mcp-server 0.7.4
        # recorded in a pinned decision). A tenant who saved either OLD default as an
        # override is flagged stale via previous_defaults below (cc904bfe badge).
        "command": ["uvx", "docx-mcp"],
        # 92b6d977 (2026-07-23) — investigated a report that
        # replace_block_between_manual_anchors on the old docx-mcp-server
        # default returns "Start anchor ... not found" for verbatim, exact-match
        # anchor text (both Heading-1 and Normal-style paragraphs; not
        # style-specific). Confirmed and reproduced upstream in
        # office-word-mcp-server (GongRzhe/Office-Word-MCP-Server, the PyPI
        # package backing this old default; module `word_document_server`,
        # matching this repo's "docx-mcp-server" previous_defaults entry): in
        # word_document_server/utils/document_utils.py,
        # replace_block_between_manual_anchors() (and the sibling
        # replace_paragraph_block_below_header()/delete_block_under_header())
        # identify paragraph/table XML elements via `el.tag == CT_P.tag` /
        # `el.tag == CT_Tbl.tag`. Accessed on the *class* (not an instance),
        # `CT_P.tag` resolves to lxml's unbound `getset_descriptor` for
        # `_Element.tag`, not the paragraph's qualified tag string — so the
        # comparison is unconditionally False, start_idx never gets set, and
        # every anchor lookup fails regardless of paragraph style or exact-text
        # match. Not a run-split/whitespace/smart-quote issue (the run-text
        # concatenation a few lines above it is actually correct). This lives
        # entirely in the third-party PyPI package, not in this repo (fetched
        # fresh via `uvx` — nothing vendored under extensions/), so it isn't
        # patchable here; it's one more data point for why 5b065c2e already
        # moved the default off this package to docx-mcp. If
        # office-word-mcp-server is ever reconsidered, this must be fixed
        # upstream (compare against `qn("w:p")`/`qn("w:tbl")` instead) before
        # any anchor-based replace tool on it can be trusted.
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
        # c5c309cc — ``command`` is intentionally left unset here, NOT a literal
        # unpinned ``@latest`` spawn. 3db4f8d8 already pinned the real launcher to
        # a known-good version (``_DC_PINNED_VERSION``) in tunnel_client.py:
        # ``_office_slot_command()`` falls back to ``_dc_default_command()`` for
        # the ``dc`` slot whenever ``command`` is None, and that fallback resolves
        # ``npx -y @wonderwhy-er/desktop-commander@<pinned>`` (plus the Windows
        # ``cmd /c`` wrapper needed so mcp-proxy can find npx via PATHEXT).
        # Hardcoding a literal command list *here* instead would regress that
        # Windows wrapper — verified as of 2026-07-19 (npm dist-tag `latest` for
        # @wonderwhy-er/desktop-commander is 0.2.46, matching the current pin) that
        # this None + fallback pattern is correct and the pin is live; do not
        # "fix" this back to an explicit command without re-reading
        # _dc_default_command()'s docstring in tunnel_client.py first.
        "command": None,
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
        # f886d37a — root cause of a "code edits to meridian-docs don't take effect
        # until well after a tunnel restart" bug seen live 2026-07-22: `uvx --from
        # <local-path> ...` caches the built venv keyed on the resolved package, and
        # its normal cache-freshness check does not reliably catch every local-source
        # edit — so a newly-added @mcp.tool() function stayed invisible even across
        # tunnel restarts. Confirmed live against this repo's pinned `uv 0.11.11`:
        # `--reinstall`/`--reinstall-package` are silently NO-OPS for `uvx` (uv itself
        # warns "Tools cannot be reinstalled via `uvx`" and the stale build still
        # runs), and `--refresh`/`--refresh-package` also do NOT force a rebuild of a
        # `--from <local-path>` venv whose source changed. Only `--no-cache` (`-n`)
        # actually forces a from-scratch rebuild every invocation — verified with a
        # throwaway local package: editing its source and re-running with each
        # candidate flag reproduced the stale output for every flag except
        # `--no-cache`. The cost (bypassing uv's resolution/build cache on every
        # spawn, ~1-2s + re-touches dependency resolution) is accepted here because
        # this is a local-path entry that IS edited during development; it is
        # deliberately NOT applied to the PyPI-installed plugins below (docx-mcp,
        # powerpoint-mcp, zotero-mcp, mcp-debugger), which aren't locally edited and
        # would only pay the cost for no correctness benefit.
        "command": [
            "uvx", "--no-cache", "--from", _MERIDIAN_DOCS_LOCAL_PATH, "meridian-docs-mcp",
        ],
        # 4b5b1a74 — root cause of a "meridian-docs was not found in the package
        # registry" crash seen live on 2026-07-19 even though the default above is
        # already correct: unlike the `code-extractor`/`word` slots, this entry
        # never got a `previous_defaults` list when 58a044c7 renamed the command
        # (["uvx", "--from", <path>, "meridian-docs"] -> ["...", "meridian-docs-mcp"]).
        # Any tenant `tunnel_plugins` config saved BEFORE that rename still stores
        # the old, broken command; resolve_plugins() merges a stored `command`
        # override unconditionally (see _OVERRIDABLE) and its cc904bfe
        # stale-override check only fires when the override matches something in
        # `previous_defaults` — so a pre-rename docs override ran forever with zero
        # "newer default available" signal, silently reproducing the exact uvx
        # registry-resolution failure the 58a044c7 rename was meant to eliminate.
        # Backfilling the pre-rename command here (mirroring the pattern already
        # used for `code-extractor`/`word`) makes resolve_plugins() flag it via
        # `stale_override` so the dashboard badge surfaces it — the same
        # warn-don't-silently-swap contract cc904bfe already guarantees elsewhere
        # (see test_resolve_plugins_flags_stale_extract_override /
        # test_resolve_plugins_flags_stale_word_docx_mcp_server_override).
        # f886d37a — also backfill the pre-cache-fix form of the CURRENT entry-point
        # (no "--no-cache" flag): a tenant override saved before this fix was applied
        # would otherwise merge in unconditionally with zero staleness signal, exactly
        # the same silent-reproduction failure mode 4b5b1a74 already documents above.
        "previous_defaults": [
            ["uvx", "--from", _MERIDIAN_DOCS_LOCAL_PATH, "meridian-docs"],
            ["uvx", "--from", _MERIDIAN_DOCS_LOCAL_PATH, "meridian-docs-mcp"],
        ],
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
    {
        # 469d89b4 — meridian-outputs: local BM25 outputs index (CSV/JSON/NPY),
        # launched via `uvx --from <local-path> meridian-outputs-mcp`. Provides
        # search_outputs, annotate_outputs, classify_outputs, resolve_figure_output,
        # find_outputs_by_source, npy_metadata, and file_fingerprint — all
        # purely local, no hosted call.
        # 469d89b4 — NOT published to PyPI; spawn from the local extensions/
        # meridian-outputs directory via `uvx --from` so this works out-of-the-box
        # without a separate PyPI publish step.
        # The entry-point is "meridian-outputs-mcp" (NOT "meridian-outputs") for the
        # same uvx name-collision reason documented in 58a044c7: when command name ==
        # package name, uvx attempts a PyPI lookup after --from install and fails.
        "name": "meridian-outputs",
        "slot": "outputs",
        "port": DEFAULT_OUTPUTS_PORT,
        "url_prefix": "/outputs",
        "enabled": False,
        "builtin": True,
        "core": False,
        # f886d37a — "--no-cache" forces uvx to rebuild this local-path venv from
        # scratch on every spawn instead of silently reusing a stale cached build
        # from before the last code edit — see the matching comment on the
        # meridian-docs entry above for the full investigation (uv 0.11.11
        # confirmed: --reinstall/--reinstall-package are no-ops for uvx;
        # --refresh/--refresh-package don't invalidate a stale --from build either;
        # only --no-cache/-n actually does). Deliberately NOT applied to the
        # PyPI-installed plugins in this file (docx-mcp, powerpoint-mcp, zotero-mcp,
        # mcp-debugger) — those aren't edited locally, so forcing a rebuild would
        # only add spawn latency for no correctness benefit.
        "command": [
            "uvx", "--no-cache", "--from", _MERIDIAN_OUTPUTS_LOCAL_PATH, "meridian-outputs-mcp",
        ],
        # f886d37a — backfill previous_defaults with the pre-cache-fix form of the
        # current entry-point (no "--no-cache" flag) so a tenant override saved
        # before this fix was applied is flagged `stale_override` via the ordinary
        # exact-match path below, rather than silently continuing to run without
        # the cache-busting flag with zero dashboard signal.
        "previous_defaults": [
            ["uvx", "--from", _MERIDIAN_OUTPUTS_LOCAL_PATH, "meridian-outputs-mcp"],
        ],
        # ff8d1b2f — root cause of a live "search_outputs unreachable,
        # tunnel_tried=true" failure (2026-07-20): the default `--from` path above
        # was already correct (computed the same way as _MERIDIAN_DOCS_LOCAL_PATH,
        # not a hardcoded server path — that part of the original bug report did
        # not match current code). The REAL bug: this slot never got a
        # `previous_defaults` list, and unlike the docs/word/extract slots (whose
        # stale overrides are single fixed historical strings — a rename), an
        # `--from <local-path>` override going stale here is inherently
        # environment-specific: a tenant's stored command can bake in an absolute
        # checkout path from whatever machine/container it was captured on (e.g.
        # a stray "file:///C:/app/extensions/meridian-outputs" from a differently
        # rooted checkout). No fixed previous_defaults list can enumerate every
        # possible stale path. resolve_plugins() below therefore also flags any
        # override that is structurally `["uvx", "--no-cache", "--from",
        # <some-other-path>, "meridian-outputs-mcp"]` — same runtime + entry-point
        # (and, f886d37a, same "--no-cache" flag) as the current default, different
        # local path — as `stale_override`, so the dashboard badge fires instead of
        # the tunnel client silently retrying an unreachable path on every call (see
        # _is_stale_local_from_override).
        "env": {},
        # meridian-outputs exposes bare tool names (search_outputs, annotate_outputs,
        # …) — no self-prefix, so the server bridge namespaces them via
        # SLOT_DISPLAY_NAMES ("outputs" → "meridian-outputs__search_outputs").
        "prefix": None,
        "session_mode": "stateless",
        "description": "Local outputs index — BM25 search over CSV/JSON/NPY files (meridian-outputs)",
        "description_overrides": {},
    },
    {
        # 121e6a27 — mcp-debugger: @debugmcp/mcp-debugger, a 7-language DAP
        # (Debug Adapter Protocol) debugger MCP. npm-published and npx-ready — no
        # local clone or extensions/ checkout needed, unlike meridian-docs /
        # meridian-outputs. A short-term standalone `.mcp.json` entry was added to
        # the target project as a stopgap; this is the real tunnel-routed
        # built-in, following the same slot pattern as dc/docs/zotero/outputs.
        "name": "mcp-debugger",
        "slot": "debug",
        "port": DEFAULT_DEBUG_PORT,
        "url_prefix": "/debug",
        "enabled": False,
        "builtin": True,
        "core": False,
        "command": ["npx", "-y", "@debugmcp/mcp-debugger"],
        "env": {},
        # A debug session (breakpoints, call stack, step state) is stateful across
        # requests — the same reasoning as Desktop Commander's terminal sessions —
        # so this slot is "persistent" (skips --stateless + the idle-killer) rather
        # than the one-shot "stateless" relay the read-only slots use.
        "session_mode": "persistent",
        # mcp-debugger exposes bare tool names — no self-prefix, so the server
        # bridge namespaces them via SLOT_DISPLAY_NAMES ("debug" → "mcp-debugger__…").
        "prefix": None,
        "description": "Debugging — 7-language DAP debugger (mcp-debugger)",
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

# 31648740 — corrects the earlier e401221d diagnosis (that fix landed in
# orphan_reaper.py's _norm_path for an unrelated path-normalization bug; it did
# NOT touch pooling). The REAL root cause, confirmed live 2026-07-19: code-intel
# (codebase-memory-mcp.exe) has a documented third-party Windows bug where its
# on-disk index rename fails if a second copy of the same process holds the .db
# file open. The default elastic pool bursts stateless slots to 2 copies under
# load — for code-intel that means 2 codebase-memory-mcp.exe instances spawned
# in the same second, one holding the .db open while the other renames its
# index, producing the HTTP 406 seen tonight.
#
# code-intel must therefore always resolve to a single copy — the same outcome
# ``session_mode="persistent"`` gives ``dc`` — but for a DIFFERENT reason, so it
# needs its OWN hard gate rather than reuse of that mechanism: code-intel is
# genuinely stateless (no per-session state to split across copies; --stateless
# and the idle-killer both correctly apply to it), so flipping its session_mode
# to "persistent" would be semantically wrong and would have real side effects
# (disables --stateless, skips the idle-killer) that are not warranted here.
#
# This frozenset is a second, independent hard gate — mirroring the
# ``_COLD_FETCH_SLOTS``-style per-slot exemption list in tunnel_client.py —
# checked alongside the session_mode gate in :func:`slot_pool_config` so a
# tenant's ``pool`` override cannot re-enable elastic pooling for this slot and
# reintroduce the Windows file-lock bug. Keyed by ``slot`` (not ``name``) to
# match how plugin dicts are looked up elsewhere in this module.
_POOL_HARD_PINNED_SLOTS: "frozenset[str]" = frozenset({"code"})


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
        "name": "meridian-outputs",
        "package": "meridian-outputs",
        "runtime": "uvx",
        # 469d89b4 SHIPPED this as a first-class built-in on its own `outputs` slot
        # (server route + WS relay in routes/tunnel.py). Local BM25 outputs index
        # (CSV/JSON/NPY); launched from the local extensions/meridian-outputs source
        # via `uvx --from <local-path> meridian-outputs-mcp`. NOT on PyPI.
        "slot": "outputs",
        "bundled": True,
        "owner_item": None,
        "description": (
            "Local BM25 outputs index (CSV/JSON/NPY) — search_outputs, "
            "annotate_outputs, classify_outputs, resolve_figure_output, "
            "find_outputs_by_source, npy_metadata, file_fingerprint. All "
            "purely local, no hosted call."
        ),
    },
    {
        # 121e6a27 SHIPPED this as a first-class built-in on its own `debug` slot
        # (server route + WS relay in routes/tunnel.py). @debugmcp/mcp-debugger is
        # a 7-language DAP (Debug Adapter Protocol) debugger, npm-published and
        # npx-ready — no local clone/extensions checkout needed, unlike
        # meridian-docs / meridian-outputs above.
        "name": "mcp-debugger",
        "package": "@debugmcp/mcp-debugger",
        "runtime": "npx",
        "slot": "debug",
        "bundled": True,
        "owner_item": None,
        "description": (
            "7-language DAP (Debug Adapter Protocol) debugger — set breakpoints, "
            "step, inspect variables/call stacks across a live debug session."
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

    e99b09e9 — this is the funnel every non-default Serena invocation passes
    through before spawn: the extract slot's tenant-saved/stale override (see
    ``tunnel_client.run_tunnel``'s ``elif expand_command(ext_raw, ...)``
    branch) AND any custom plugin/slot (``resolve_custom_plugins`` command
    expansion at spawn time), both here and nowhere else. Routing the result
    through :func:`meridian.serena_pool.ensure_serena_headless` here — rather
    than only at the one built-in default — means a command that merely
    *looks like* Serena (matches on content, not on which slot it rides)
    always gets the headless flag forced, independent of staleness or which
    slot it's bound to. Non-Serena commands pass through byte-for-byte.
    """
    cmd = _coerce_command(value)
    if cmd is None:
        return None
    rp = repo_path or ""
    expanded = [tok.replace("{repo_path}", rp) for tok in cmd]
    from .serena_pool import ensure_serena_headless  # noqa: PLC0415 — avoid import cycle risk

    return ensure_serena_headless(expanded)


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


# 02dbd8b4 — runtime configuration generation: every settings write is stamped
# with a monotonically increasing generation number, a content hash of the
# EFFECTIVE config, and a source timestamp (see routes/tunnel.py's
# ``bump_runtime_config_generation`` / ``get_runtime_config_generation``). The
# hash lives here, next to ``normalize_plugins_config``, so it stays a pure,
# dependency-free function the tunnel client can also import without pulling
# in the server's DB/WebSocket machinery.
def config_fingerprint(raw_config: Any) -> str:
    """Stable content hash of a tunnel plugin config.

    Normalizes *raw_config* first (:func:`normalize_plugins_config`) so
    cosmetically different but semantically identical configs — list vs
    dict-keyed form, key order, an explicit ``{}`` vs ``None`` vs ``[]`` —
    fingerprint identically. This is the "effective configuration hash"
    reported alongside the runtime config generation: the dashboard, a
    connected tunnel client, and the connector cache can all compare hashes
    instead of deep-diffing JSON blobs to answer "are we looking at the same
    configuration?", and a settings write that doesn't actually change
    anything (identical hash) does not need to advance the generation counter
    or demand a restart.

    Pure — no I/O, no randomness: the same input always yields the same
    16-hex-char digest (truncated sha256; short enough to log/display, long
    enough that an accidental collision between two genuinely different
    configs is not a practical concern for this change-detection use case).
    """
    normalized = normalize_plugins_config(raw_config)
    canonical = json.dumps(normalized, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


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

    A slot may also be hard-pinned to a single copy for a reason OTHER than
    session_mode — see :data:`_POOL_HARD_PINNED_SLOTS` (31648740: code-intel's
    Windows index-rename bug when 2 copies race on the same .db file). That gate
    is checked here too, unconditionally, so a tenant ``pool`` override cannot
    re-enable elastic pooling for a hard-pinned slot.

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
    # 31648740 — second, independent hard gate: some stateless slots are still
    # pinned to a single copy for a non-session_mode reason (code-intel's
    # Windows .db-rename race under 2 concurrent copies). Checked before any
    # override is read, so a tenant ``pool`` override can never re-enable
    # elastic pooling here.
    if plugin.get("slot") in _POOL_HARD_PINNED_SLOTS:
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
    DEFAULT_ZOTERO_PORT, DEFAULT_OUTPUTS_PORT, DEFAULT_DEBUG_PORT,
})

# 9811d04c — first port a freshly-added custom plugin (from the browse "Add"
# button) is auto-assigned when the caller supplies no port. Starts just above
# the built-in default range (8808–8821, incl. the 4 pre-allocated custom
# slots, the outputs built-in at 8820, and the debug built-in at 8821), so an
# auto-assigned port never collides with a built-in slot.
# 469d89b4 — bumped from 8820 to 8821 to make room for DEFAULT_OUTPUTS_PORT.
# 121e6a27 — bumped from 8821 to 8822 to make room for DEFAULT_DEBUG_PORT.
_CUSTOM_PORT_START = 8822

# 9811d04c — the built-in *slot* names (fs/code/extract/ppt/word/dc). A custom
# plugin's name must not collide with a built-in slot name (task rule) nor with a
# built-in plugin's display name (filesystem/code-intel/… — see builtin_names),
# since either is a slot override rather than a genuine custom plugin.
#
# 45049071 — also reserve the OPTIONAL OpenAI Secure MCP Tunnel transport
# adapter's own identity (meridian.openai_tunnel_adapter). That adapter is
# NOT one of the three-slot model's built-in slots (it never appears in
# SLOTS/builtin_names — it is a separate, parallel transport, not a plugin
# behind a Meridian tunnel slot), so without this explicit reservation a
# same-named LOCAL custom plugin would silently pass
# validate_custom_plugin/resolve_custom_plugins and shadow the adapter's
# identity in any future dashboard/plugin listing.
_RESERVED_CUSTOM_NAMES = frozenset(SLOTS) | {
    "openai", "openai-tunnel", "openai_secure_mcp_tunnel",
}

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
    would silently drop it. Case-insensitive on the stripped value. Also
    reserves the separate OpenAI Secure MCP Tunnel adapter's own identity
    (45049071 — see :data:`_RESERVED_CUSTOM_NAMES`'s own comment).
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


def _is_stale_local_from_override(ov_cmd: Any, base_cmd: Any) -> bool:
    """ff8d1b2f — detect a stale ``uvx --no-cache --from <local-path> <entry-point>``
    override.

    Complements the exact-match ``previous_defaults`` list: that list only catches
    a *fixed* historical default (e.g. a one-time entry-point rename, or — f886d37a —
    the pre-cache-fix command missing the ``--no-cache`` flag). A local ``--from``
    command embeds an absolute filesystem path computed from
    ``Path(__file__).parent.parent`` at the time it was saved — if a tenant's
    stored override was captured on a different checkout/container (a different
    root than this machine currently computes), the path segment is permanently
    wrong on this machine and no fixed string list could ever enumerate every
    possible stale value.

    Returns True when both commands share the same shape
    ``["uvx", "--no-cache", "--from", <path>, <entry-point>]`` (f886d37a bumped this
    from a 4-token to a 5-token shape when the cache-busting flag was inserted) with
    the same runtime, cache flag, and entry-point but a *different* path — i.e.
    "this looks like our own default, just pointed at a different local checkout" —
    without asserting anything about paths that aren't ours to begin with (a
    genuinely custom command, e.g. a different runtime, entry-point, or missing the
    ``--no-cache`` flag entirely, is left untouched here; the missing-flag case is
    instead caught by the exact-match ``previous_defaults`` entry above).
    """
    if (
        not isinstance(ov_cmd, list) or not isinstance(base_cmd, list)
        or len(ov_cmd) != 5 or len(base_cmd) != 5
    ):
        return False
    if (
        ov_cmd[0] != base_cmd[0]
        or ov_cmd[1] != "--no-cache" or base_cmd[1] != "--no-cache"
        or ov_cmd[2] != "--from" or base_cmd[2] != "--from"
    ):
        return False
    if ov_cmd[4] != base_cmd[4]:
        return False
    return ov_cmd[3] != base_cmd[3]


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
        # 4b26c2ef — a built-in marked `"retired": True` (currently just
        # `word`/docx-mcp) is forced OFF here, last — after both the tenant
        # override merge above and the detected_slots auto-enable — so neither
        # an explicit `enabled: true` override nor detect_office_binaries()
        # finding docx-mcp on PATH can re-activate it. Everything else about
        # the resolved descriptor (command/port/env/stale_override flag/etc.)
        # still resolves normally; only `enabled` is pinned.
        if base.get("retired"):
            merged["enabled"] = False
        # slot / url_prefix are immutable for built-ins.
        merged["slot"] = base["slot"]
        merged["url_prefix"] = base["url_prefix"]
        # e99b09e9 — force headless on any resolved command that looks like a
        # Serena launch: the built-in "extract" default, a tenant override, a
        # stale snapshot from before the headless flag existed, or (since
        # detection is content-based, not slot-based) a tenant who pointed a
        # DIFFERENT built-in slot's command override at Serena. Custom
        # (non-builtin-named) plugins aren't looped over here — they're
        # normalized at spawn time by expand_command instead (see its
        # docstring). This runs BEFORE the staleness check below, which
        # compares the RAW ov_cmd / base_cmd, so normalizing here never masks
        # a genuinely stale override from the "newer default available"
        # badge. Keeps what the dashboard displays/returns as "the command"
        # already headless-safe, not just what the tunnel client resolves at
        # spawn time via expand_command.
        if isinstance(merged.get("command"), list):
            from .serena_pool import ensure_serena_headless  # noqa: PLC0415

            merged["command"] = ensure_serena_headless(merged["command"])
        # cc904bfe — flag a stale custom command override: the tenant saved a
        # `command` that matches a *previous* built-in default for this slot (so
        # it was a copy of the old default, now superseded by a new one). The
        # tunnel still runs the override, but the dashboard surfaces a "newer
        # default available" badge so the user can opt back into the new default.
        # A genuinely-custom command (not in previous_defaults) is left untouched.
        # ff8d1b2f — also flags a stale `uvx --from <local-path>` override whose
        # path doesn't match what *this* machine currently computes (see
        # _is_stale_local_from_override) — a fixed previous_defaults string list
        # can't enumerate every possible stale absolute path a tenant's stored
        # config might have baked in from a different checkout/container.
        ov_cmd = ov.get("command")
        base_cmd = base.get("command")
        prev_defaults = base.get("previous_defaults") or []
        if ov_cmd and ov_cmd != base_cmd and (
            any(ov_cmd == pd for pd in prev_defaults)
            or _is_stale_local_from_override(ov_cmd, base_cmd)
        ):
            merged["stale_override"] = True
            merged["newer_default_command"] = base_cmd
            merged["newer_default_label"] = base.get("description")
        merged.pop("previous_defaults", None)  # internal — don't leak to clients
        resolved.append(merged)
    return resolved


def retired_plugin_names() -> "frozenset[str]":
    """4b26c2ef — names of built-ins marked ``"retired": True`` in
    :data:`BUILTIN_PLUGINS` — forced permanently OFF by :func:`resolve_plugins`
    regardless of tenant override or PATH-based auto-detection. Currently just
    ``word`` (docx-mcp). Derived from the registry (not a hardcoded literal) so a
    future retirement extends :func:`migrate_retired_overrides` automatically.
    """
    return frozenset(p["name"] for p in BUILTIN_PLUGINS if p.get("retired"))


def migrate_retired_overrides(raw_config: Any) -> "tuple[Any, bool]":
    """4b26c2ef — clean a stale ``enabled: true`` override for a RETIRED built-in
    slot (currently just ``word``) out of a stored tenant/per-host config.

    :func:`resolve_plugins` already force-disables :func:`retired_plugin_names`
    unconditionally, so an old ``{"word": {"enabled": true}}`` override sitting in
    a tenant's persisted ``tunnel_plugins`` / ``tunnel_plugins_by_host`` blob is
    inert — it never takes effect — but nothing removes it, so it round-trips
    through the dashboard (and gets re-persisted) forever. This does that at the
    source:

    * Any retired-slot entry with a truthy ``enabled`` has it flipped to
      ``False`` — the entry itself is kept (not dropped), so a saved
      ``command``/``env`` survives in case the slot is ever un-retired.
    * If that flip fires and the config has no explicit ``meridian-docs`` entry
      yet, an ``{"enabled": true}`` entry for it is appended — carrying the
      tenant's original intent ("I wanted DOCX tooling on") over to the slot
      that replaces word for Meridian-aware DOCX work.
    * Anything else (no retired-slot entry present, or one already
      ``enabled: false``/unset) is returned completely unchanged.

    Returns ``(migrated_config, changed)``. ``migrated_config`` preserves the
    caller's original shape (list-of-dicts vs. dict-keyed) so it can be persisted
    back verbatim in place of the input; ``changed`` is ``False`` whenever
    nothing needed migrating, so a call site can skip a no-op DB write. Pure — no
    I/O, no subprocess.
    """
    items = _iter_plugin_items(raw_config)
    if not items:
        return raw_config, False

    retired = retired_plugin_names()
    changed = False
    out_items: list[dict] = []
    names_seen: set[str] = set()
    for it in items:
        name = str(it.get("name") or "").strip()
        names_seen.add(name)
        if name in retired and bool(it.get("enabled")):
            it = {**it, "enabled": False}
            changed = True
        out_items.append(it)

    if changed and "meridian-docs" not in names_seen:
        out_items.append({"name": "meridian-docs", "enabled": True})

    if isinstance(raw_config, list):
        return out_items, changed
    # Dict-keyed shape in -> dict-keyed shape out ({name: {..without "name"..}}).
    out_dict: dict[str, Any] = {}
    for it in out_items:
        nm = it["name"]
        out_dict[nm] = {k: v for k, v in it.items() if k != "name"}
    return out_dict, changed


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
    """:func:`resolve_plugins` filtered to the enabled entries.

    4b26c2ef — a retired built-in (currently ``word``) can never appear here:
    :func:`resolve_plugins` forces its resolved ``enabled`` to ``False``
    unconditionally before this filter ever runs, regardless of ``raw_config``
    or ``detected_slots``.
    """
    return [p for p in resolve_plugins(raw_config, detected_slots) if p.get("enabled")]


def plugin_by_slot(raw_config: Any, slot: str) -> dict | None:
    """Return the resolved plugin riding ``slot`` (fs/code/extract), or None."""
    for p in resolve_plugins(raw_config):
        if p["slot"] == slot:
            return p
    return None
