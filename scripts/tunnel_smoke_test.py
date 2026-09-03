#!/usr/bin/env python
"""Tunnel smoke-test harness (sprint item 3dac9efb, design note 6271aa81).

Adam's explicit ask (2026-07-19, after a night of manual tunnel restart/test/
report cycles): a real, permanent, committed script that starts the tunnel
itself, speaks real MCP to every slot, records structured pass/fail, and
drives the fix-redeploy-retest loop autonomously -- "ouroboros until it's all
fixed for good without my constant intervention manually."

Run it directly::

    pixi run python scripts/tunnel_smoke_test.py --single-cycle
    pixi run python scripts/tunnel_smoke_test.py --max-cycles 5 --auto-fix

or import it as a library (every function below is independently testable --
see tests/test_tunnel_smoke_test.py).

WHAT TRANSPORT THIS USES, AND WHY (read before changing it)
-------------------------------------------------------------------------
Adam hand-prototyped two patterns tonight, both proven working:

  1. Direct stdio JSON-RPC to a slot's INNER MCP server (initialize ->
     notifications/initialized -> tools/list), confirmed against meridian-docs.
  2. Direct HTTP POST to an already-WARM local mcp-proxy port (127.0.0.1:88xx).

This harness generalizes pattern #1 to every slot as its PRIMARY, authoritative
transport (:class:`StdioMcpClient`): it resolves the exact command
``meridian --tunnel`` would use for a slot (via ``meridian.tunnel_plugins`` /
a few ``meridian.tunnel_client`` helpers, reused rather than re-implemented so
there is zero drift risk against the real thing) and speaks raw MCP JSON-RPC
directly to that inner server's stdio -- no ``mcp-proxy``, no WebSocket relay,
no tenant/token/network round trip required for the core loop.

Why not pattern #2 (hit the local mcp-proxy port) as the default? Because
``mcp-proxy`` for a slot is only spawned lazily, from inside
``tunnel_client.SlotProxy.ensure_running`` -- which only fires when the
*hosted relay* forwards a real WebSocket "request" message from
usemeridian.us. There is no local, in-process way to trigger that spawn
without either (a) reimplementing large parts of ``run_tunnel`` here (drift
risk), or (b) driving traffic through the real hosted URLs with a live
token/tenant (a hard network dependency this harness's CORE loop should not
require). Pattern #2 is still exercised, but only opportunistically: if a
slot's proxy port already happens to be open (warmed by real prior usage),
:func:`test_slot` does not additionally probe it -- see the coverage matrix
below for exactly what is and is not exercised.

SAFETY: a live tunnel may already be running on this machine
-------------------------------------------------------------------------
Building this on 2026-07-19 turned up ``~/.meridian/tunnel.log`` actively
being appended to by Adam's own overnight ``meridian --tunnel`` session --
i.e. a second, *independent*, real MCP client is commonly running on the same
box this harness runs on. Two consequences, both load-bearing:

  * The STDIO-direct transport above never binds a TCP port and never touches
    the hosted WebSocket relay, so it is safe to run at any time, alongside a
    live session, with zero collision risk. This is the DEFAULT.
  * Actually starting a competing ``meridian --tunnel`` subprocess (needed
    only for full-stack boot validation / the empirical auth-reuse check /
    the best-effort cascading-disconnect correlation -- see
    :class:`TunnelSubprocess`) is gated OFF by default
    (``--with-tunnel-boot`` to opt in) and, even then, refuses to run at all
    if :func:`detect_live_peer_ports` finds another live tunnel's claim on a
    port this run would need -- mirroring the exact peer-aware guard
    ``tunnel_client._kill_stale_port_occupant`` already uses in production
    (reused here directly, not re-implemented, via a fresh per-run
    ``client_id``). The hosted relay is believed to key each transport slot's
    WebSocket by tenant_id alone (single active connection per slot per
    tenant -- see ``routes/tunnel.py``'s ``_tunnel_*_sockets`` dicts), so a
    second real tunnel client for the SAME tenant could silently replace /
    disconnect a live human session's connection. This harness must never
    risk that -- matching the same "never disrupt the human's active
    session" principle behind the hooks.ps1 hard rule in CLAUDE.md.

FAILURE-CATEGORY COVERAGE MATRIX (design note 6271aa81's 7 predicted classes)
-------------------------------------------------------------------------
 1. Cold-spawn readiness too short   -- FULL. Cycle 1 spawns a cold-fetch slot
    twice (cold, then warm) and compares timing; see ``build_slot_specs`` /
    ``COLD_FETCH_SLOTS`` / ``COLD_SPAWN_WARN_THRESHOLD_S``.
 2. Multi-instance pooling contention -- BEST-EFFORT, local-only. Real elastic
    pooling is server-side (``meridian.slot_pool`` bursts 1->2 *local* copies
    under load, routed by the hosted relay); this harness cannot drive that
    concurrency from outside the relay. What IS real: static analysis
    confirmed a second, entirely independent pool -- Serena's per-repo daemon
    pool -- allocated its FIRST daemon on ``SERENA_POOL_BASE_PORT``, which was
    IDENTICAL to ``DEFAULT_OUTPUTS_PORT`` (both 8820). FIXED by a1a870d5
    (2026-07-19): ``SERENA_POOL_BASE_PORT`` moved to 8700, below every fixed
    port the tunnel-plugin catalog declares. See :func:`check_port_collisions`
    -- now a general, permanent regression check across every declared port in
    the codebase (not just this one historical pair), so it would have caught
    this exact bug and stays green going forward.
 3. Fly-instance / routing mismatch  -- WEAK, local-only. ``test_slot``
    repeats its functional call a few seconds apart and flags an inconsistent
    pass/fail (``repeat_consistent``); a genuinely Fly-routing-caused flap
    cannot be reproduced by a local stdio child (there is no Fly routing in
    that path) -- this check mainly catches slot-local flakiness.
 4. AV/Defender TAR_ENTRY_ERROR      -- FULL. :func:`classify_captured_output`
    detects the signature in ANY captured stdout/stderr; is treated as a
    HARD STOP everywhere in this file -- never retried, never auto-fixed.
 5. Registry-resolution mystery      -- FULL. Signature-detected the same way;
    eligible for the opt-in scoped-cache-clear auto-fixer (reuses
    ``tunnel_client._scoped_cache_clear`` -- the exact mechanism already
    proven safe in production, not a new destructive routine).
 6. Cascading cross-slot disconnect  -- BEST-EFFORT, ``--with-tunnel-boot``
    only. Independent per-slot stdio children share no event loop, so they
    structurally cannot reproduce a shared-event-loop stall; when the real
    tunnel subprocess IS started, :func:`detect_cascading_disconnect`
    correlates its own timestamped log lines for exactly this pattern.
 7. Auth/token re-issuance           -- FULL, and checked FIRST (see
    ``main()``). A read-only, zero-risk cache inspection
    (:func:`predict_needs_browser_auth`) always runs; the full empirical
    double-start check runs only under ``--with-tunnel-boot``.

Two additional REAL findings this file's own construction turned up (not
predicted by the design note) are documented and permanently checked by
:func:`check_client_wires_all_catalog_slots` and :func:`check_port_collisions`
below -- see their docstrings.

ABSOLUTE HARD BOUNDARY: this harness never attempts to modify Windows
Defender / antivirus exclusions or any other security setting, under any
flag, automatically or otherwise. A detected AV signature is ALWAYS reported,
never worked around.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from meridian.tunnel_plugins import (  # noqa: E402
    SERENA_EXTRACT_COMMAND,
    SLOTS,
    detect_office_binaries,
    expand_command,
    resolve_plugins,
)
from meridian.tunnel_client import (  # noqa: E402
    DEFAULT_BASE_URL,
    _config_path as _tc_config_path,
    _dc_default_command,
    _ensure_codebase_memory_mcp,
    _find_npx,
    _find_uvx,
    _is_slot_claimed_by_live_client,
    _kill_stale_port_occupant,
    _office_slot_command,
    _plugin_spawn_env,
    _port_is_open,
    _read_cached_token,
    _scoped_cache_clear,
    _spawn_kwargs,
)
from meridian.serena_pool import SERENA_POOL_BASE_PORT

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Slots the design note calls out as always-tested. "outputs" is included
# because it is directly testable via stdio regardless of client wiring state.
CORE_SLOTS: tuple[str, ...] = ("fs", "code", "extract")
DEFAULT_TESTED_SLOTS: tuple[str, ...] = (
    "fs", "code", "extract", "ppt", "word", "dc", "docs", "outputs",
)
# "mcp-debugger and zotero only if enabled" (design note) -- opt-in via --include-optional.
OPTIONAL_SLOTS: tuple[str, ...] = ("zotero", "debug")

# 2026-07-19 finding (this file's own construction): at the time this harness
# was first built, these catalog slots had NO wiring at all in
# tunnel_client.run_tunnel -- no SlotProxy was ever created for them, so even
# a tenant who enabled them via the dashboard got nothing when they ran
# `meridian --tunnel`. FIXED by 12afe021 (both slots now share the office-
# family SlotProxy + reconnect-loop wiring); kept as an empty tuple -- rather
# than deleted -- so a future regression that un-wires either slot has an
# obvious place to land the two names back into, and
# check_client_wires_all_catalog_slots (below) independently re-verifies the
# real source on every run regardless of this constant's value.
KNOWN_UNWIRED_SLOTS: tuple[str, ...] = ()

# Slots whose FIRST spawn triggers a genuine cold network/venv build (mirrors
# tunnel_client._COLD_FETCH_SLOTS, duplicated here as a constant rather than
# imported since tunnel_client's set is keyed by its own internal slot labels
# for a slightly different purpose -- the two are expected to stay in sync).
# 12afe021 -- "debug" added alongside "outputs": now that both are actually
# wired and spawned, "debug" (npx -y @debugmcp/mcp-debugger) is a first-install
# cold fetch exactly like "dc"'s npx launch.
#
# 2026-07-20 finding: "code" is deliberately NOT in tunnel_client._COLD_FETCH_SLOTS
# (production's SlotProxy reuse_existing=True usually adopts an already-warm
# instance instead of needing a cold-build budget), but THIS harness always
# spawns a brand-new codebase-memory-mcp process of its own -- see
# _harness_code_intel_env() below for why it can't just reuse production's
# cache dir. A genuinely cold first-time index build of this repo measured
# >30s (STDIO_INIT_TIMEOUT_S) in a real run. Deliberately DIVERGES from
# tunnel_client._COLD_FETCH_SLOTS here for that reason.
COLD_FETCH_SLOTS: frozenset[str] = frozenset(
    {"dc", "ppt", "word", "docs", "zotero", "outputs", "debug", "code"}
)

# Roughly mirrors tunnel_client._PREFLIGHT_BUDGET_COLD_FETCH (4 attempts * 5s
# delay + processing => on the order of a minute). A cold spawn slower than
# this is worth a finding even if it eventually succeeds.
COLD_SPAWN_WARN_THRESHOLD_S = 90.0

STDIO_INIT_TIMEOUT_S = 30.0
STDIO_LIST_TIMEOUT_S = 30.0
STDIO_CALL_TIMEOUT_S = 30.0
STDIO_COLD_SPAWN_TIMEOUT_S = 150.0  # docs/outputs cold uvx build can take 30-150s
REPEAT_CALL_DELAY_S = 3.0  # category-3 check: same call, a few seconds apart

STARTUP_CONFIRM_RE = re.compile(r"meridian tunnel: serving")
STARTUP_SETTLE_WINDOW_S = 8.0
STARTUP_HARD_TIMEOUT_S = 90.0

# "passes once" != "fixed" (edge case #3 in the design note).
CONSECUTIVE_PASSES_REQUIRED = 2
DEFAULT_MAX_CYCLES = 5
# Cap on wall-clock waiting for a server-side deploy to go green (edge case #4).
DEPLOY_WAIT_TIMEOUT_S = 15 * 60

# Failure-signature strings, captured verbatim from real observed output.
AV_SIGNATURE = "TAR_ENTRY_ERROR"
REGISTRY_SIGNATURE = "was not found in the package registry"
BROWSER_AUTH_SIGNATURE = "No API token found. Opening browser"

# All ports the client catalog knows about -- used for the pre-flight stale-
# port sweep and the live-peer-session safety guard.
ALL_KNOWN_PORTS: tuple[int, ...] = (8808, 8809, 8810, 8811, 8812, 8813, 8818, 8819, 8820, 8821)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Pure JSON-RPC message builders (unit-tested directly, no I/O)
# ---------------------------------------------------------------------------

MCP_PROTOCOL_VERSION = "2025-03-26"


def build_initialize_request(req_id: int = 1) -> dict:
    """The MCP handshake's opening request. Every real MCP stdio client sends
    this first; a server that never answers it is a hard connectivity fail."""
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "method": "initialize",
        "params": {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "meridian-tunnel-smoke-test", "version": "1.0"},
        },
    }


def build_initialized_notification() -> dict:
    """The handshake-completing notification (no ``id`` -- notifications get
    no response, per JSON-RPC 2.0)."""
    return {"jsonrpc": "2.0", "method": "notifications/initialized"}


def build_tools_list_request(req_id: int = 2) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "method": "tools/list", "params": {}}


def build_tools_call_request(name: str, arguments: dict, req_id: int = 3) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments or {}},
    }


def parse_jsonrpc_message(line: str) -> "dict | None":
    """Parse one line of captured stdout as a JSON-RPC message.

    Returns ``None`` (never raises) for blank lines, non-JSON lines (some
    inner servers occasionally emit a stray banner line on stdout instead of
    stderr), or JSON that doesn't decode to an object -- callers should skip
    and keep reading rather than treat this as fatal.
    """
    text = (line or "").strip()
    if not text:
        return None
    try:
        msg = json.loads(text)
    except (ValueError, TypeError):
        return None
    return msg if isinstance(msg, dict) else None


# ---------------------------------------------------------------------------
# Generic functional-tool selection (pure, unit-tested)
# ---------------------------------------------------------------------------

def select_functional_tool(
    tools: "list[dict]",
    preferred: "list[tuple[str, dict]] | None" = None,
) -> "tuple[str, dict] | None":
    """Pick ONE tool + arguments for a real (not just tools/list) functional
    call -- "not just existence-check" per the design note.

    *preferred* is an ordered list of ``(tool_name, arguments)`` candidates a
    caller is confident about (e.g. the filesystem slot's well-documented
    ``list_directory``); the first one present in *tools* wins. This exists
    because third-party MCP servers' exact tool names/schemas are outside
    this repo's control and can drift across package versions -- hardcoding
    a guess for every slot would be brittle. Falling back to the FIRST tool
    with no required schema fields is the robust, version-agnostic default:
    a tool nobody needs to pass arguments to is always safe to call as a real
    smoke check.

    Returns ``None`` when neither a preferred match nor any zero-required
    tool exists (every declared tool needs arguments this harness cannot
    safely guess) -- callers treat that as "functional call skipped", not a
    hard failure, since tools/list itself still succeeded.
    """
    by_name = {t.get("name"): t for t in tools if isinstance(t, dict) and t.get("name")}
    if preferred:
        for name, args in preferred:
            if name in by_name:
                return name, args
    for t in tools:
        if not isinstance(t, dict) or not t.get("name"):
            continue
        schema = t.get("inputSchema") or {}
        required = schema.get("required") or []
        if not required:
            return t["name"], {}
    return None


# ---------------------------------------------------------------------------
# Failure-signature classification (pure, unit-tested)
# ---------------------------------------------------------------------------

def classify_captured_output(text: "str | None") -> "str | None":
    """Classify captured process output against the known failure signatures.

    Order matters: AV interference is checked first because it is the
    hardest hard-stop (never retriable by this harness, ever); a
    registry-resolution failure is checked next (retriable via a scoped
    cache clear); the browser-auth signature last (only ever relevant when
    scanning a real tunnel subprocess's boot log, never a slot's stdio
    output, but classified the same way for a single source of truth).
    Returns ``None`` for anything else -- including empty/``None`` input.
    """
    if not text:
        return None
    if AV_SIGNATURE in text:
        return "av_interference"
    if REGISTRY_SIGNATURE in text:
        return "registry_resolution"
    if BROWSER_AUTH_SIGNATURE in text:
        return "browser_auth_required"
    return None


# ---------------------------------------------------------------------------
# Static regression checks (pure, real findings against the actual repo)
# ---------------------------------------------------------------------------

def check_client_wires_all_catalog_slots(
    tunnel_client_source: str,
    catalog_slots: "Iterable[str]",
    *,
    core_slots: "Iterable[str]" = CORE_SLOTS,
) -> "list[str]":
    """Return catalog slot names that ``tunnel_client.py``'s source never
    references at all -- i.e. slots declared in
    ``tunnel_plugins.BUILTIN_PLUGINS`` that ``run_tunnel`` has no code path
    for whatsoever, so even an explicit dashboard ``enabled: true`` produces
    nothing (no ``SlotProxy``, no reconnect loop, no printed status line).

    This is a real gap discovered while building this harness (2026-07-19):
    at the time, ``outputs`` and ``debug`` were BOTH in this state -- the
    server side already had full WS routes for them (``routes/tunnel.py``'s
    ``_tunnel_outputs_sockets`` / ``@router.websocket("/tunnel-outputs/{tenant_id}")``),
    and ``tunnel_plugins.BUILTIN_PLUGINS`` fully described their commands/ports,
    but the CLIENT never connected. FIXED by 12afe021 -- both slots now join
    the office-family loop in ``run_tunnel`` alongside ppt/word/dc/docs/zotero,
    so this check should report an empty list against the real source going
    forward (see ``test_check_client_wires_all_catalog_slots_real_source_matches_known_gap``
    in tests/test_tunnel_smoke_test.py, which asserts exactly that). The check
    itself is kept as a permanent regression guard: any future slot added to
    ``BUILTIN_PLUGINS`` without matching client wiring will be caught the same
    way. A crude source-text-membership check (rather than e.g. import-time
    introspection of ``run_tunnel``'s bytecode) is deliberate: it is cheap, has
    zero import-time side effects, and is exactly as precise as this needs to
    be -- every wired slot's short code appears as a quoted literal somewhere
    in the office-slot loop or an explicit ``by_slot.get(...)`` block; an
    unwired one appears nowhere.

    Pure string operation -- *tunnel_client_source* is passed in (rather than
    read from disk here) so this stays fully unit-testable against synthetic
    fixtures with zero filesystem dependency; callers doing a REAL check pass
    ``Path(tunnel_client.__file__).read_text()``.
    """
    missing: list[str] = []
    core = set(core_slots)
    for slot in catalog_slots:
        if slot in core:
            continue
        if f'"{slot}"' not in tunnel_client_source and f"'{slot}'" not in tunnel_client_source:
            missing.append(slot)
    return missing


def check_port_collisions() -> "list[str]":
    """Return human-readable descriptions of any STATIC port collision between
    every fixed port this codebase declares.

    This is a general regression check over the FULL set of statically
    declared ports -- every ``tunnel_plugins.DEFAULT_*_PORT`` (fs/code/
    extract/ppt/word/dc/docs/zotero/outputs/debug), every pre-allocated
    ``tunnel_plugins.CUSTOM_SLOT_PORTS`` entry (p0-p3), and
    ``serena_pool.SERENA_POOL_BASE_PORT`` -- not just one hardcoded pair. Two
    constants naming the same port number means whatever they gate would
    contend for the same TCP port if both were simultaneously active.

    Real finding this check is permanent regression coverage for (2026-07-19,
    fixed by a1a870d5): ``meridian.serena_pool.SERENA_POOL_BASE_PORT`` was
    8820 -- the port the code-extractor slot's default Serena daemon pool
    allocates FIRST (see ``SerenaDaemonPool._next_port``, which starts at
    ``base_port`` and increments only when that port is already held by
    another live daemon in the SAME pool). ``tunnel_plugins.DEFAULT_OUTPUTS_PORT``
    was also 8820. Since the code-extractor slot defaults to the Serena pool
    (``run_tunnel`` only skips it when a tenant overrides the extract slot's
    command), a tenant with the default extract slot AND the outputs slot
    both active would have Serena's first-repo daemon and meridian-outputs
    contending for the same TCP port. 12afe021 wired the outputs slot into
    ``run_tunnel`` (see :func:`check_client_wires_all_catalog_slots`), which
    turned this from a dormant static finding into a live collision the moment
    a tenant enables both slots -- fixed separately by moving
    ``SERENA_POOL_BASE_PORT`` off 8820 (a1a870d5). This was independent of,
    and additional to, the design note's predicted "multi-instance pooling
    contention" category -- it was a static allocation bug, not a runtime
    race. Checking every declared port (not just that one pair) means the
    same class of bug -- any two fixed ports landing on the same number --
    is caught going forward, regardless of which two constants collide next.
    """
    from meridian.tunnel_plugins import (
        CUSTOM_SLOT_PORTS,
        DEFAULT_CODE_PORT,
        DEFAULT_DC_PORT,
        DEFAULT_DEBUG_PORT,
        DEFAULT_DOCS_PORT,
        DEFAULT_EXTRACT_PORT,
        DEFAULT_FS_PORT,
        DEFAULT_OUTPUTS_PORT,
        DEFAULT_PPT_PORT,
        DEFAULT_WORD_PORT,
        DEFAULT_ZOTERO_PORT,
    )

    declared: "dict[str, int]" = {
        "tunnel_plugins.DEFAULT_FS_PORT": DEFAULT_FS_PORT,
        "tunnel_plugins.DEFAULT_CODE_PORT": DEFAULT_CODE_PORT,
        "tunnel_plugins.DEFAULT_EXTRACT_PORT": DEFAULT_EXTRACT_PORT,
        "tunnel_plugins.DEFAULT_PPT_PORT": DEFAULT_PPT_PORT,
        "tunnel_plugins.DEFAULT_WORD_PORT": DEFAULT_WORD_PORT,
        "tunnel_plugins.DEFAULT_DC_PORT": DEFAULT_DC_PORT,
        "tunnel_plugins.DEFAULT_DOCS_PORT": DEFAULT_DOCS_PORT,
        "tunnel_plugins.DEFAULT_ZOTERO_PORT": DEFAULT_ZOTERO_PORT,
        "tunnel_plugins.DEFAULT_OUTPUTS_PORT": DEFAULT_OUTPUTS_PORT,
        "tunnel_plugins.DEFAULT_DEBUG_PORT": DEFAULT_DEBUG_PORT,
        "serena_pool.SERENA_POOL_BASE_PORT": SERENA_POOL_BASE_PORT,
    }
    for slot_name, port in CUSTOM_SLOT_PORTS.items():
        declared[f"tunnel_plugins.CUSTOM_SLOT_PORTS[{slot_name!r}]"] = port

    by_port: "dict[int, list[str]]" = {}
    for name, port in declared.items():
        by_port.setdefault(port, []).append(name)

    findings: list[str] = []
    for port in sorted(by_port):
        names = by_port[port]
        if len(names) > 1:
            findings.append(
                f"port {port} is declared by multiple constants: "
                f"{', '.join(sorted(names))} -- these would contend for the "
                "same TCP port if simultaneously active."
            )
    return findings


def detect_cascading_disconnect(
    log_lines: "list[tuple[float, str]]",
    *,
    window_s: float = 20.0,
) -> "list[str]":
    """Best-effort correlation for failure category #6 (cascading cross-slot
    disconnect, item 31de9cf7's class of bug) against a REAL tunnel
    subprocess's own timestamped stdout lines.

    *log_lines* is ``(seconds_since_process_start, line)`` pairs, exactly what
    :class:`TunnelSubprocess` accumulates. Flags any ``"disconnected ("``
    event for slot B occurring within *window_s* seconds of a ``"spawning
    proxy"`` event for a DIFFERENT slot A -- the observable signature of one
    slot's stuck spawn (a blocking call on the shared asyncio event loop)
    starving every other slot's WebSocket reconnect loop.

    Pure text-timestamp correlation, no I/O -- fully unit-testable with
    synthetic log lines. Independent stdio-direct slot tests (this harness's
    default transport) cannot exercise this at all, since they share no
    event loop with one another; this function is ONLY meaningful when
    applied to a real tunnel subprocess's log (``--with-tunnel-boot``).
    """
    spawn_re = re.compile(r"tunnel:(\S+?): spawning proxy")
    disc_re = re.compile(r"tunnel:(\S+?): disconnected \(")
    spawns = [(t, m.group(1)) for t, line in log_lines if (m := spawn_re.search(line))]
    discs = [(t, m.group(1)) for t, line in log_lines if (m := disc_re.search(line))]
    out: list[str] = []
    for st, sslot in spawns:
        for dt, dslot in discs:
            if dslot != sslot and abs(dt - st) <= window_s:
                out.append(
                    f"possible cascading disconnect: {dslot!r} disconnected "
                    f"{dt - st:+.1f}s relative to {sslot!r}'s spawn (window={window_s}s)"
                )
    return out


def predict_needs_browser_auth(base_url: str = DEFAULT_BASE_URL) -> bool:
    """Read-only, zero-risk pre-flight for failure category #7 -- "CHECK THIS
    FIRST" per the design note.

    Mirrors ``tunnel_client._read_cached_token`` exactly (reused directly, not
    reimplemented): returns True iff starting a real tunnel right now would
    need a fresh browser OAuth click (no cached token, or a cached token that
    is expired / for a different base_url). Never touches the network and
    never starts a subprocess -- safe to call unconditionally, every run.
    """
    return _read_cached_token(base_url) is None


# ---------------------------------------------------------------------------
# Convergence tracking (pure, unit-tested)
# ---------------------------------------------------------------------------

class ConvergenceTracker:
    """Tracks each slot's CONSECUTIVE pass streak across cycles.

    Edge case #3 in the design note: "PASSES ONCE" != "FIXED" -- a slot only
    counts as solid once it has passed :data:`CONSECUTIVE_PASSES_REQUIRED`
    times in a row, on separate cycles, with zero failures in between. Any
    failure resets that slot's streak to zero.
    """

    def __init__(self, required: int = CONSECUTIVE_PASSES_REQUIRED) -> None:
        self.required = required
        self._streaks: "dict[str, int]" = {}

    def record(self, slot: str, passed: bool) -> None:
        if passed:
            self._streaks[slot] = self._streaks.get(slot, 0) + 1
        else:
            self._streaks[slot] = 0

    def streak(self, slot: str) -> int:
        return self._streaks.get(slot, 0)

    def is_solid(self, slot: str) -> bool:
        return self.streak(slot) >= self.required

    def all_solid(self, slots: "Iterable[str]") -> bool:
        slots = list(slots)
        return bool(slots) and all(self.is_solid(s) for s in slots)

    def unsolved(self, slots: "Iterable[str]") -> "list[str]":
        return [s for s in slots if not self.is_solid(s)]


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class SlotSpec:
    slot: str
    name: str
    port: "int | None"
    cmd: "list[str]"
    env: "dict[str, str] | None"
    session_mode: str
    cold_fetch: bool
    optional: bool = False
    wired_in_client: bool = True
    skip_reason: "str | None" = None

    def to_dict(self) -> dict:
        return {
            "slot": self.slot, "name": self.name, "port": self.port,
            "cmd": self.cmd, "session_mode": self.session_mode,
            "cold_fetch": self.cold_fetch, "optional": self.optional,
            "wired_in_client": self.wired_in_client, "skip_reason": self.skip_reason,
        }


@dataclass
class SlotResult:
    slot: str
    name: str
    passed: bool
    spawn_ms: "float | None" = None
    cold: "bool | None" = None
    tools_count: "int | None" = None
    functional_tool: "str | None" = None
    functional_ok: bool = False
    repeat_consistent: "bool | None" = None
    error: "str | None" = None
    classification: "str | None" = None
    stderr_tail: "str | None" = None
    notes: "list[str]" = field(default_factory=list)
    # ba31dedf — the "shutdown" leg of the cold/warm/shutdown smoke test.
    # True only when StdioMcpClient.close() confirmed the spawned child
    # actually exited (a real receipt, never fabricated); False when it was
    # never spawned or termination could not be confirmed; None only for a
    # SlotResult built before any spawn was attempted (skip_reason /
    # no-runnable-command early returns in test_slot).
    shutdown_confirmed: "bool | None" = None

    def to_dict(self) -> dict:
        return {
            "slot": self.slot, "name": self.name, "passed": self.passed,
            "spawn_ms": self.spawn_ms, "cold": self.cold,
            "tools_count": self.tools_count, "functional_tool": self.functional_tool,
            "functional_ok": self.functional_ok, "repeat_consistent": self.repeat_consistent,
            "error": self.error, "classification": self.classification,
            "stderr_tail": self.stderr_tail, "notes": list(self.notes),
            "shutdown_confirmed": self.shutdown_confirmed,
        }


@dataclass
class Finding:
    category: str
    severity: str  # "hard-stop" | "action-needed" | "info"
    summary: str
    detail: str = ""
    slot: "str | None" = None

    def to_dict(self) -> dict:
        return {
            "category": self.category, "severity": self.severity, "slot": self.slot,
            "summary": self.summary, "detail": self.detail,
        }


@dataclass
class CycleResult:
    cycle: int
    started_at: str
    ended_at: str
    slot_results: "list[SlotResult]"
    findings: "list[Finding]"
    tunnel_boot_ok: "bool | None" = None

    def to_dict(self) -> dict:
        return {
            "cycle": self.cycle, "started_at": self.started_at, "ended_at": self.ended_at,
            "tunnel_boot_ok": self.tunnel_boot_ok,
            "slot_results": [r.to_dict() for r in self.slot_results],
            "findings": [f.to_dict() for f in self.findings],
        }

    def any_hard_stop(self) -> bool:
        return any(f.severity == "hard-stop" for f in self.findings)


async def _terminate_proc_tree_async(proc: "asyncio.subprocess.Process | None") -> None:
    """Stop a spawned child *and its whole process tree* -- async counterpart
    of ``tunnel_client._terminate_proc_tree`` (same ``taskkill /F /T`` logic,
    re-expressed for ``asyncio.subprocess.Process`` instead of a blocking
    ``subprocess.Popen``, and awaited rather than using a synchronous
    ``proc.wait(timeout=...)``).

    On Windows, ``npx``/``uvx`` invocations are launcher shims: a plain
    ``.terminate()`` kills only the immediate child (``npx.cmd`` -> ``cmd.exe``)
    and orphans the real ``node.exe``/inner-server grandchild, which then
    keeps running (and, for a persistent slot, keeps a port bound) after this
    harness believes it has cleaned up. ``taskkill /F /T /PID <pid>`` kills
    the whole tree by PID, matching the exact fix already proven for the
    tunnel client's own proxy processes. Best-effort; never raises.
    """
    if proc is None:
        return
    if sys.platform == "win32":
        try:
            kill_proc = await asyncio.create_subprocess_exec(
                "taskkill", "/F", "/T", "/PID", str(proc.pid),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            # Bounded the same way as the proc.wait() below -- taskkill
            # itself can stall (AV/Defender interference, an unkillable
            # target) and must not hang this function forever. TimeoutError
            # is an Exception subclass so it's caught by the clause below,
            # same as a spawn failure.
            await asyncio.wait_for(kill_proc.wait(), timeout=5.0)
        except Exception:  # noqa: BLE001 — fall back to terminate below
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
    else:
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=5.0)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()


# ---------------------------------------------------------------------------
# StdioMcpClient -- the harness's primary, authoritative test transport
# ---------------------------------------------------------------------------

class McpStdioError(RuntimeError):
    """Raised for any stdio-transport failure -- spawn, timeout, or protocol
    error. Always carries enough context (including a captured-stderr tail)
    to classify via :func:`classify_captured_output`."""


class StdioMcpClient:
    """Speaks real MCP JSON-RPC directly over a subprocess's stdio.

    Generalizes Adam's hand-prototyped pattern #1 (direct stdio to an inner
    MCP server, proven against meridian-docs) to every slot. Requires
    ``asyncio.create_subprocess_exec`` support, which on Windows means the
    default ``ProactorEventLoop`` (do NOT run this under a
    ``SelectorEventLoop`` policy override -- ``asyncio.run()`` with no prior
    policy change already gives Proactor on win32, which is what this file
    relies on).
    """

    def __init__(
        self,
        cmd: "list[str]",
        *,
        env: "dict[str, str] | None" = None,
        cwd: "str | None" = None,
        label: str = "?",
    ) -> None:
        self.cmd = cmd
        self.env = env
        self.cwd = cwd
        self.label = label
        self._proc: "asyncio.subprocess.Process | None" = None
        self._stderr_chunks: "list[bytes]" = []
        self._stderr_task: "asyncio.Task | None" = None
        self._next_id = 1

    async def start(self) -> None:
        merged_env = {**os.environ, **(self.env or {})}
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *self.cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=merged_env,
                cwd=self.cwd,
                **_spawn_kwargs(),
            )
        except (FileNotFoundError, OSError) as exc:
            raise McpStdioError(f"{self.label}: spawn failed: {exc}") from exc
        self._stderr_task = asyncio.ensure_future(self._drain_stderr())

    async def _drain_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        try:
            while True:
                chunk = await self._proc.stderr.read(4096)
                if not chunk:
                    break
                self._stderr_chunks.append(chunk)
        except Exception:  # noqa: BLE001 — best-effort log capture only
            pass

    @property
    def captured_stderr(self) -> str:
        return b"".join(self._stderr_chunks).decode("utf-8", errors="replace")

    async def _send(self, message: dict) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        line = (json.dumps(message) + "\n").encode("utf-8")
        self._proc.stdin.write(line)
        await self._proc.stdin.drain()

    async def _recv(self, *, timeout: float, expected_id: int) -> dict:
        """Read stdout lines until the RESPONSE to *expected_id* arrives.

        2026-07-20 finding: this used to return the first parseable JSON-RPC
        line, full stop -- with no check that it was actually a response to
        the request just sent, rather than a server-initiated NOTIFICATION
        (a message with no "id", which the MCP spec explicitly allows a
        server to send at any time, e.g. `notifications/message` logging).
        Desktop Commander does exactly this during startup: a burst of log
        notifications arrives on stdout before the real tools/list response,
        got returned as if it WERE that response (no "error" key, no
        "result" key -> `(resp.get("result") or {}).get("tools")` silently
        evaluated to `[]`), and the harness reported a clean-looking
        "0 tools, no error" instead of the real tool list. Any MCP server
        that emits notifications between a request and its response would
        silently desync every subsequent request/response pairing on this
        connection the same way -- not a Desktop-Commander-specific bug.
        Skipping anything whose "id" doesn't match what THIS call sent fixes
        it for every slot, not just this one.
        """
        assert self._proc is not None and self._proc.stdout is not None
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise McpStdioError(
                    f"{self.label}: timed out after {timeout}s waiting for a response "
                    f"(stderr tail: {self.captured_stderr[-500:]!r})"
                )
            try:
                raw = await asyncio.wait_for(self._proc.stdout.readline(), timeout=remaining)
            except asyncio.TimeoutError as exc:
                raise McpStdioError(
                    f"{self.label}: timed out after {timeout}s waiting for a response "
                    f"(stderr tail: {self.captured_stderr[-500:]!r})"
                ) from exc
            if not raw:
                raise McpStdioError(
                    f"{self.label}: process closed stdout (exit={self._proc.returncode}; "
                    f"stderr tail: {self.captured_stderr[-500:]!r})"
                )
            msg = parse_jsonrpc_message(raw.decode("utf-8", errors="replace"))
            if msg is None:
                # Non-JSON stdout line (rare banner/log noise) -- keep reading.
                continue
            if msg.get("id") != expected_id:
                # A notification (no "id") or a response to an earlier/other
                # request on this connection -- not what this call is
                # waiting for. Keep reading rather than misreport it.
                continue
            return msg

    async def initialize(self, *, timeout: float = STDIO_INIT_TIMEOUT_S) -> dict:
        req_id = self._next_id
        self._next_id += 1
        await self._send(build_initialize_request(req_id))
        resp = await self._recv(timeout=timeout, expected_id=req_id)
        await self._send(build_initialized_notification())
        return resp

    async def list_tools(self, *, timeout: float = STDIO_LIST_TIMEOUT_S) -> "list[dict]":
        req_id = self._next_id
        self._next_id += 1
        await self._send(build_tools_list_request(req_id))
        resp = await self._recv(timeout=timeout, expected_id=req_id)
        if "error" in resp:
            raise McpStdioError(f"{self.label}: tools/list error: {resp['error']}")
        return (resp.get("result") or {}).get("tools") or []

    async def call_tool(self, name: str, arguments: dict, *, timeout: float = STDIO_CALL_TIMEOUT_S) -> dict:
        req_id = self._next_id
        self._next_id += 1
        await self._send(build_tools_call_request(name, arguments, req_id))
        return await self._recv(timeout=timeout, expected_id=req_id)

    async def close(self) -> bool:
        """Tear down the spawned child. Returns a REAL shutdown receipt --
        ``True`` only when the process is CONFIRMED exited (a non-None
        ``returncode``) after termination, ``False`` when it was never
        spawned, or termination could not be confirmed (still running after
        ``_terminate_proc_tree_async``'s own bounded kill+wait). Never
        fabricated: this is exactly what ``_terminate_proc_tree_async``
        already does internally, just observed and reported instead of
        discarded (ba31dedf 'shutdown' leg of the cold/warm/shutdown smoke
        test).
        """
        if self._proc is None:
            return False
        with contextlib.suppress(Exception):
            if self._proc.stdin:
                self._proc.stdin.close()
        await _terminate_proc_tree_async(self._proc)
        shutdown_confirmed = self._proc.returncode is not None
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            # asyncio.CancelledError is a BaseException (not Exception) since
            # Python 3.8, so contextlib.suppress(Exception) alone does NOT
            # catch the CancelledError that awaiting our own just-cancelled
            # task raises -- it escaped close() uncaught and crashed the
            # whole harness (run_cycle -> run_loop -> _amain) the moment a
            # slot legitimately timed out. Standard "cancel then await, catch
            # the resulting CancelledError" idiom: this is expected fallout
            # of the .cancel() call two lines up, not an external cancellation
            # that should keep propagating.
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await self._stderr_task
        return shutdown_confirmed


# ---------------------------------------------------------------------------
# Slot command resolution -- reuses tunnel_client/tunnel_plugins as the single
# source of truth wherever they already resolve a command; only fs (a
# well-documented, stable public package) and code (via the already-tested
# auto-installer) are special-cased.
# ---------------------------------------------------------------------------

def _resolve_launcher(cmd: "list[str]") -> "list[str]":
    """Rewrite a bare ``npx``/``uvx`` first token to its directly-spawnable
    resolved path.

    ``tunnel_plugins.BUILTIN_PLUGINS`` commands use bare ``"npx"``/``"uvx"``
    because ``mcp-proxy`` (a Node child_process) resolves those from PATH
    itself, with a ``--shell`` fallback for Windows .cmd shims. This harness
    spawns commands directly via Python's ``subprocess``/``asyncio``, which
    needs the actual resolved path (matching exactly what
    ``tunnel_client._find_npx``/``_find_uvx`` already do, and are reused for
    here) -- see those functions' docstrings for the Windows ``[WinError
    193]``/``EINVAL`` history this sidesteps. Any other first token (a full
    path, ``cmd``, a native binary) is left untouched.
    """
    if not cmd:
        return list(cmd)
    out = list(cmd)
    head = out[0].lower()
    if head == "npx":
        out[0] = _find_npx()
    elif head == "uvx":
        found = _find_uvx()
        if found:
            out[0] = found
    return out


def _harness_code_intel_env() -> "dict[str, str]":
    """Dedicated ``CBM_CACHE_DIR`` for THIS harness's own code-intel spawn.

    2026-07-20 finding: build_slot_specs previously passed ``env=None`` for
    the "code" slot, so its codebase-memory-mcp spawn used the unset default
    cache location -- never the dedicated, already-warm one
    ``tunnel_client._code_intel_spawn_env()`` gives production's own slot
    (3475c72f/8e10fb80). That forced a full cold reindex of this repo on
    EVERY harness run, timing out against the non-cold-fetch 30s budget.

    Deliberately NOT the same directory as production's dedicated cache,
    either: that directory can be held open by a live ``meridian --tunnel``
    process's own code-intel slot at the exact moment this harness runs (a
    live peer tunnel is exactly the scenario this harness's own
    ``detect_live_peer_ports`` guard exists for elsewhere) -- two
    independently-spawned codebase-memory-mcp processes both holding the same
    index file open at once is the precise collision
    ``tunnel_client._code_intel_cache_dir``'s docstring documents working
    around for other external consumers. A third, harness-only location keeps
    this harness's own spawns from ever colliding with either the unset
    default or production's dedicated dir, while still staying warm/
    persistent across repeated harness runs -- only the very first-ever run
    against a fresh dir pays the real cold-build cost.
    """
    cache_dir = Path.home() / ".meridian" / "code-intel-cache-smoketest"
    env = dict(os.environ)
    if not env.get("CBM_CACHE_DIR"):
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
        except Exception:  # noqa: BLE001 — best-effort; codebase-memory-mcp
            # creates its cache dir itself on first write if this didn't.
            pass
        env["CBM_CACHE_DIR"] = str(cache_dir)
    return env


async def build_slot_specs(
    repo_path: str,
    *,
    slots: "Iterable[str]" = DEFAULT_TESTED_SLOTS,
    include_optional: bool = False,
) -> "list[SlotSpec]":
    """Resolve one :class:`SlotSpec` per requested slot.

    Sources ports/commands/env/session_mode from
    ``tunnel_plugins.resolve_plugins`` (LOCAL defaults only -- no live
    ``/me`` network call, so this never depends on tenant auth for the core
    loop) wherever a slot has an explicit catalog command, falling back to
    the same builders ``run_tunnel`` itself uses for ``fs``/``code``
    (command ``None`` in the catalog by design -- see
    ``tunnel_plugins.BUILTIN_PLUGINS``).
    """
    wanted = list(slots)
    if include_optional:
        wanted += [s for s in OPTIONAL_SLOTS if s not in wanted]

    plugins = resolve_plugins(None, detected_slots=detect_office_binaries())
    by_slot = {p["slot"]: p for p in plugins}

    specs: "list[SlotSpec]" = []
    for slot in wanted:
        plugin = by_slot.get(slot, {})
        wired = slot not in KNOWN_UNWIRED_SLOTS
        optional = slot in OPTIONAL_SLOTS
        cold = slot in COLD_FETCH_SLOTS

        if slot == "fs":
            cmd = [_find_npx(), "-y", "@modelcontextprotocol/server-filesystem", repo_path]
            specs.append(SlotSpec(slot, "filesystem", plugin.get("port", 8808), cmd,
                                   None, "stateless", cold, optional, wired))
        elif slot == "code":
            binary = await _ensure_codebase_memory_mcp()
            if binary is None:
                specs.append(SlotSpec(slot, "code-intel", plugin.get("port", 8809), [],
                                       None, "stateless", cold, optional, wired,
                                       skip_reason="codebase-memory-mcp could not be located or installed"))
                continue
            specs.append(SlotSpec(slot, "code-intel", plugin.get("port", 8809), [binary],
                                   _harness_code_intel_env(), "stateless", cold, optional, wired))
        elif slot == "extract":
            cmd = _resolve_launcher(expand_command(SERENA_EXTRACT_COMMAND, repo_path=repo_path) or [])
            specs.append(SlotSpec(slot, "code-extractor", plugin.get("port", 8810), cmd,
                                   None, "stateless", cold, optional, wired))
        elif slot in ("ppt", "word", "dc"):
            cmd = _resolve_launcher(_office_slot_command(slot, plugin) or [])
            if not cmd:
                specs.append(SlotSpec(slot, plugin.get("name", slot), plugin.get("port"), [],
                                       plugin.get("env"), plugin.get("session_mode", "stateless"),
                                       cold, optional, wired,
                                       skip_reason=f"no runnable command resolved for slot {slot!r}"))
                continue
            specs.append(SlotSpec(slot, plugin.get("name", slot), plugin.get("port"), cmd,
                                   plugin.get("env"), plugin.get("session_mode", "stateless"),
                                   cold, optional, wired))
        else:  # docs, zotero, outputs, debug -- explicit catalog commands
            cmd = _resolve_launcher(expand_command(plugin.get("command"), repo_path=repo_path) or [])
            if not cmd:
                specs.append(SlotSpec(slot, plugin.get("name", slot), plugin.get("port"), [],
                                       plugin.get("env"), plugin.get("session_mode", "stateless"),
                                       cold, optional, wired,
                                       skip_reason=f"slot {slot!r} not found in the local plugin catalog"))
                continue
            specs.append(SlotSpec(slot, plugin.get("name", slot), plugin.get("port"), cmd,
                                   plugin.get("env"), plugin.get("session_mode", "stateless"),
                                   cold, optional, wired))
    return specs


# Well-documented, version-stable preferred tools per slot. Everything else
# relies on select_functional_tool's zero-required-args fallback (see its
# docstring for why guessing third-party schemas is deliberately avoided).
def _preferred_tools(spec: SlotSpec, repo_path: str) -> "list[tuple[str, dict]] | None":
    if spec.slot == "fs":
        return [("list_directory", {"path": repo_path})]
    return None


# ---------------------------------------------------------------------------
# Per-slot functional test
# ---------------------------------------------------------------------------

async def test_slot(
    spec: SlotSpec,
    repo_path: str,
    *,
    cold: "bool | None" = None,
    repeat_check: bool = True,
    spawn_timeout: "float | None" = None,
) -> SlotResult:
    """Spawn *spec*'s inner MCP server, speak a real handshake + tools/list,
    then ONE real functional tool call (per the design note: "not just
    tools/list alone"). Returns a fully structured :class:`SlotResult` --
    never raises.
    """
    if spec.skip_reason:
        return SlotResult(spec.slot, spec.name, passed=False, cold=cold,
                           error=spec.skip_reason, notes=["skipped: no command to run"])
    if not spec.cmd:
        return SlotResult(spec.slot, spec.name, passed=False, cold=cold,
                           error="no runnable command resolved")

    timeout = spawn_timeout or (STDIO_COLD_SPAWN_TIMEOUT_S if spec.cold_fetch else STDIO_INIT_TIMEOUT_S)
    client = StdioMcpClient(spec.cmd, env=_plugin_spawn_env(spec.env), cwd=repo_path, label=spec.slot)
    t0 = time.monotonic()
    try:
        # 2026-07-20 finding (this run's own construction): every other await in
        # this function is timeout-wrapped, but client.start() (a bare
        # asyncio.create_subprocess_exec) was not -- an OS-level stall before the
        # child process even starts talking on stdio (AV on-access scan of a
        # freshly-downloaded/npx-cached binary, a first-run GUI/elevation prompt
        # from an Office-family slot, etc.) hung the entire harness indefinitely
        # with near-zero CPU, since nothing downstream ever got a chance to time
        # it out. Bounding it here means a stall like that surfaces as a normal
        # boot_failure-style SlotResult instead of an unkillable-from-inside hang.
        # NB: if create_subprocess_exec is cancelled after the OS process actually
        # exists but before the Process wrapper is returned, that child can be
        # left orphaned (an inherent asyncio limitation) -- acceptable tradeoff
        # vs. hanging the whole run forever.
        await asyncio.wait_for(client.start(), timeout=timeout)
        await asyncio.wait_for(client.initialize(timeout=timeout), timeout=timeout + 5.0)
        tools = await asyncio.wait_for(client.list_tools(timeout=timeout), timeout=timeout + 5.0)
    except (McpStdioError, asyncio.TimeoutError) as exc:
        stderr = client.captured_stderr
        text = f"{exc}\n{stderr}"
        classification = classify_captured_output(text)
        shutdown_confirmed = await client.close()
        return SlotResult(
            spec.slot, spec.name, passed=False, cold=cold, error=str(exc),
            classification=classification, stderr_tail=stderr[-1500:] if stderr else None,
            shutdown_confirmed=shutdown_confirmed,
        )

    spawn_ms = (time.monotonic() - t0) * 1000.0
    notes: "list[str]" = []
    functional_tool: "str | None" = None
    functional_ok = False
    repeat_consistent: "bool | None" = None

    picked = select_functional_tool(tools, _preferred_tools(spec, repo_path))
    if picked is None:
        notes.append(f"no zero-arg tool found among {len(tools)} tool(s) -- functional call skipped")
    else:
        functional_tool, args = picked
        try:
            r1 = await asyncio.wait_for(
                client.call_tool(functional_tool, args, timeout=STDIO_CALL_TIMEOUT_S),
                timeout=STDIO_CALL_TIMEOUT_S + 5.0,
            )
            functional_ok = "error" not in r1
            if repeat_check:
                await asyncio.sleep(REPEAT_CALL_DELAY_S)
                r2 = await asyncio.wait_for(
                    client.call_tool(functional_tool, args, timeout=STDIO_CALL_TIMEOUT_S),
                    timeout=STDIO_CALL_TIMEOUT_S + 5.0,
                )
                ok2 = "error" not in r2
                repeat_consistent = functional_ok == ok2
                if not repeat_consistent:
                    notes.append(
                        f"category-3 flag: repeat call to {functional_tool!r} gave "
                        f"inconsistent pass/fail ({functional_ok} then {ok2})"
                    )
        except (McpStdioError, asyncio.TimeoutError) as exc:
            notes.append(f"functional call to {functional_tool!r} failed: {exc}")

    stderr = client.captured_stderr
    shutdown_confirmed = await client.close()
    classification = classify_captured_output(stderr) if not functional_ok else None
    # Connectivity + a non-empty tool list is the floor; a real functional
    # call succeeding is the target. When no safe zero-arg tool exists at all,
    # a clean connect + non-empty tools/list still counts as passed (there was
    # nothing unsafe-to-guess left to prove), but functional_ok stays False so
    # callers can see the distinction rather than a misleadingly blanket PASS.
    passed = functional_ok or (picked is None and len(tools) > 0)

    return SlotResult(
        spec.slot, spec.name, passed=passed, spawn_ms=spawn_ms, cold=cold,
        tools_count=len(tools), functional_tool=functional_tool, functional_ok=functional_ok,
        repeat_consistent=repeat_consistent, classification=classification,
        stderr_tail=stderr[-1500:] if stderr else None, notes=notes,
        shutdown_confirmed=shutdown_confirmed,
    )


# ---------------------------------------------------------------------------
# Live-peer-session safety guard + stale-port sweep
# ---------------------------------------------------------------------------

def detect_live_peer_ports(ports: "Iterable[int]" = ALL_KNOWN_PORTS) -> "list[int]":
    """Return the subset of *ports* that look like they belong to a LIVE,
    independent tunnel session already running on this machine.

    A port counts as "live peer" if it is open AND either (a) its slot-claim
    file names a different, still-running tunnel PID (via
    ``tunnel_client._is_slot_claimed_by_live_client``, reused directly so
    this shares the exact PID-reuse-hardened liveness check production
    already relies on), or (b) it is simply open with no claim info at all
    (an older/manual tunnel invocation predating the claim-file feature) --
    treated conservatively as "possibly live" rather than silently ignored.
    """
    fresh_id = str(uuid.uuid4())  # guaranteed to never match a real claim
    live: "list[int]" = []
    for port in ports:
        if not _port_is_open(port):
            continue
        if _is_slot_claimed_by_live_client(port, fresh_id):
            live.append(port)
            continue
        # Open with no verifiable claim -- conservatively treat as possibly
        # live rather than assume it is safe to reclaim.
        live.append(port)
    return live


def kill_stale_ports(ports: "Iterable[int]" = ALL_KNOWN_PORTS, client_id: "str | None" = None) -> None:
    """Best-effort pre-cycle port sweep, mirroring what ``meridian --tunnel``
    already does at its own startup (``__main__.py``'s stale-port cleanup).

    Reuses ``tunnel_client._kill_stale_port_occupant`` directly (NOT a fresh
    unconditional ``os.kill``) so a port genuinely owned by a live PEER
    tunnel session is left untouched -- only a truly orphaned occupant (dead
    PID, or a claim written by client_id itself from an earlier failed
    cleanup) is cleared. *client_id* should be this harness run's own fresh
    UUID, distinct from any real session's, so the peer-liveness check in
    ``_kill_stale_port_occupant`` correctly treats every OTHER claim as a
    possible live peer to protect.
    """
    cid = client_id or str(uuid.uuid4())
    for port in ports:
        with contextlib.suppress(Exception):
            _kill_stale_port_occupant(port, f"smoke-test:{port}", current_client_id=cid)


# ---------------------------------------------------------------------------
# Real tunnel subprocess (opt-in, --with-tunnel-boot only)
# ---------------------------------------------------------------------------

DEFAULT_TUNNEL_LAUNCH_CMD: "list[str]" = ["pixi", "run", "python", "-m", "meridian"]


class TunnelSubprocess:
    """Wraps ``pixi run python -m meridian --tunnel`` as a real, monitored
    background subprocess for boot-level validation ONLY (auth reuse, the
    Node gate, live slot-enable resolution, category-6/7 log correlation).
    It is intentionally NOT used as the functional-MCP-call transport -- see
    the module docstring for why.
    """

    def __init__(
        self,
        repo_path: str,
        *,
        base_url: "str | None" = None,
        log_path: "Path | None" = None,
        launch_cmd: "list[str] | None" = None,
        extra_args: "list[str] | None" = None,
    ) -> None:
        self.repo_path = repo_path
        self.base_url = base_url
        self.log_path = log_path
        self.launch_cmd = launch_cmd or DEFAULT_TUNNEL_LAUNCH_CMD
        self.extra_args = extra_args or []
        self.proc: "asyncio.subprocess.Process | None" = None
        self.lines: "list[tuple[float, str]]" = []
        self._reader_task: "asyncio.Task | None" = None
        self._log_fh = None
        self._t0 = 0.0

    async def start(self) -> None:
        cmd = [*self.launch_cmd, "--tunnel", "--repo", self.repo_path, *self.extra_args]
        env = dict(os.environ)
        if self.base_url:
            env["MERIDIAN_URL"] = self.base_url
        if self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            self._log_fh = open(self.log_path, "w", encoding="utf-8")
        self._t0 = time.monotonic()
        self.proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=str(REPO_ROOT), env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            **_spawn_kwargs(),
        )
        self._reader_task = asyncio.ensure_future(self._read_loop())

    async def _read_loop(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        while True:
            raw = await self.proc.stdout.readline()
            if not raw:
                break
            text = raw.decode("utf-8", errors="replace").rstrip("\n")
            ts = time.monotonic() - self._t0
            self.lines.append((ts, text))
            if self._log_fh:
                self._log_fh.write(f"[{ts:8.2f}s] {text}\n")
                self._log_fh.flush()

    @property
    def captured(self) -> str:
        return "\n".join(line for _, line in self.lines)

    async def wait_for_settle(
        self,
        *,
        confirm_re: "re.Pattern" = STARTUP_CONFIRM_RE,
        settle_window: float = STARTUP_SETTLE_WINDOW_S,
        hard_timeout: float = STARTUP_HARD_TIMEOUT_S,
    ) -> str:
        """Poll for the "meridian tunnel: serving ..." confirmation line (or
        settle-window timeout), per the design note's step 2. Returns one of
        ``"confirm_line"``, ``"timeout_settle_window"``, ``"timeout_hard"``,
        or ``"exited_early"``."""
        deadline = time.monotonic() + hard_timeout
        seen_confirm_at: "float | None" = None
        while time.monotonic() < deadline:
            if self.proc is not None and self.proc.returncode is not None:
                return "exited_early"
            if seen_confirm_at is None:
                for _, line in self.lines:
                    if confirm_re.search(line):
                        seen_confirm_at = time.monotonic()
                        break
            if seen_confirm_at is not None and time.monotonic() - seen_confirm_at >= settle_window:
                return "confirm_line"
            await asyncio.sleep(0.25)
        return "timeout_settle_window" if seen_confirm_at is not None else "timeout_hard"

    async def stop(self) -> None:
        if self.proc is None:
            return
        with contextlib.suppress(ProcessLookupError):
            if sys.platform == "win32" and hasattr(signal, "CTRL_BREAK_EVENT"):
                self.proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                self.proc.send_signal(signal.SIGINT)
        try:
            # Prefer graceful shutdown: run_tunnel's own signal handler tears
            # down every SlotProxy (and its whole process tree) itself when
            # given the chance -- only escalate to a hard tree-kill below if
            # it doesn't exit in time.
            await asyncio.wait_for(self.proc.wait(), timeout=15.0)
        except asyncio.TimeoutError:
            await _terminate_proc_tree_async(self.proc)
        if self._reader_task is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._reader_task, timeout=5.0)
        if self._log_fh:
            self._log_fh.close()


# ---------------------------------------------------------------------------
# Opt-in, well-understood client-side auto-fixer
# ---------------------------------------------------------------------------

def apply_client_side_cache_clear_fix(spec: SlotSpec) -> bool:
    """The one auto-fix this harness ships: a SCOPED uvx/npx cache clear for
    *spec*'s package, so the next spawn attempt starts from a clean cache.

    Reuses ``tunnel_client._scoped_cache_clear`` -- the EXACT mechanism
    already proven safe in production (``SlotProxy.ensure_running`` /
    ``_spawn_with_cache_retry`` already apply it after every real spawn
    failure) rather than inventing new filesystem-deleting code here. Scoped
    to just that one package's cache entry (see ``_scoped_cache_clear``'s
    docstring for the exact scoping contract) -- never a global cache wipe.

    Returns True iff a clear was attempted (a recognisable uvx/npx command);
    False for anything else (e.g. the auto-installed code-intel binary),
    which this fix cannot help -- callers should not retry in that case.

    Only ever invoked when the operator explicitly opts in via ``--auto-fix``
    (see ``run_loop``) and only for the ``registry_resolution`` /
    cold-spawn-timeout classifications -- NEVER for ``av_interference`` or
    ``browser_auth_required`` (see the module's ABSOLUTE HARD BOUNDARY).
    """
    return _scoped_cache_clear(spec.cmd, spec.slot)


AUTO_FIXERS: "dict[str, Callable[[SlotSpec], bool]]" = {
    "registry_resolution": apply_client_side_cache_clear_fix,
}


# ---------------------------------------------------------------------------
# Cycle + loop orchestration
# ---------------------------------------------------------------------------

@dataclass
class RunContext:
    repo_path: str
    base_url: str = DEFAULT_BASE_URL
    slots: "tuple[str, ...]" = DEFAULT_TESTED_SLOTS
    include_optional: bool = False
    concurrency: int = 4
    with_tunnel_boot: bool = False
    force_tunnel_boot: bool = False
    log_dir: "Path | None" = None
    auto_fix: bool = False
    client_id: str = field(default_factory=lambda: str(uuid.uuid4()))


def static_findings() -> "list[Finding]":
    """Findings that don't depend on running anything -- cheap, always-on,
    permanent regression checks against the real repo."""
    findings: "list[Finding]" = []
    try:
        source = Path(__file__).resolve().parent.parent.joinpath("meridian", "tunnel_client.py").read_text(encoding="utf-8")
        missing = check_client_wires_all_catalog_slots(source, SLOTS)
        for slot in missing:
            findings.append(Finding(
                category="client_wiring_gap", severity="action-needed", slot=slot,
                summary=f"tunnel_client.run_tunnel has no code path for slot {slot!r}.",
                detail=(
                    f"Slot {slot!r} is declared in tunnel_plugins.BUILTIN_PLUGINS and the "
                    "server already has WS routes for it, but the tunnel CLIENT never "
                    "creates a SlotProxy or reconnect loop for it -- enabling it via the "
                    "dashboard has no effect until this is wired in tunnel_client.run_tunnel."
                ),
            ))
    except Exception as exc:  # noqa: BLE001 — a static check must never crash the run
        findings.append(Finding(category="self_check_error", severity="info", slot=None,
                                 summary="check_client_wires_all_catalog_slots could not run",
                                 detail=str(exc)))
    for msg in check_port_collisions():
        findings.append(Finding(category="port_collision", severity="action-needed",
                                 slot=None, summary="Static port collision in the tunnel port catalog.",
                                 detail=msg))
    return findings


async def run_cycle(ctx: RunContext, cycle_num: int, tracker: ConvergenceTracker) -> CycleResult:
    started = _utcnow_iso()
    findings: "list[Finding]" = list(static_findings())

    if predict_needs_browser_auth(ctx.base_url):
        findings.append(Finding(
            category="auth_reissuance", severity="action-needed", slot=None,
            summary="No cached tunnel token found -- the first real tunnel start this "
                    "run would need a fresh browser OAuth click.",
            detail=f"Checked {_tc_config_path()} for base_url={ctx.base_url!r}.",
        ))

    boot: "TunnelSubprocess | None" = None
    tunnel_boot_ok: "bool | None" = None
    if ctx.with_tunnel_boot:
        live_ports = detect_live_peer_ports(ALL_KNOWN_PORTS)
        if live_ports and not ctx.force_tunnel_boot:
            findings.append(Finding(
                category="live_session_guard", severity="info", slot=None,
                summary=f"Skipped starting a competing tunnel subprocess -- ports {live_ports} "
                        "look claimed by a live peer tunnel on this machine.",
                detail="Pass --force-tunnel-boot to override (not recommended while a human "
                       "session may be relying on that tunnel).",
            ))
        else:
            kill_stale_ports(ALL_KNOWN_PORTS, ctx.client_id)
            log_path = (ctx.log_dir / f"tunnel_boot_cycle{cycle_num}.log") if ctx.log_dir else None
            boot = TunnelSubprocess(ctx.repo_path, base_url=ctx.base_url, log_path=log_path)
            try:
                # Bounded the same as wait_for_settle's own hard_timeout below --
                # without this, a stall inside create_subprocess_exec itself (AV
                # on-access scan, pixi env resolution) hangs this coroutine
                # forever with no timeout machinery downstream ever getting a
                # chance to run.
                await asyncio.wait_for(boot.start(), timeout=STARTUP_HARD_TIMEOUT_S)
            except asyncio.TimeoutError:
                tunnel_boot_ok = False
                findings.append(Finding(
                    category="boot_failure", severity="action-needed", slot=None,
                    summary=f"Tunnel subprocess failed to spawn within {STARTUP_HARD_TIMEOUT_S:.0f}s.",
                    detail="create_subprocess_exec() itself never returned -- possibly an AV "
                           "on-access scan of the freshly-invoked pixi/python launcher, or pixi "
                           "environment resolution stalling.",
                ))
            else:
                settle = await boot.wait_for_settle()
                tunnel_boot_ok = settle in ("confirm_line", "timeout_settle_window")
                classification = classify_captured_output(boot.captured)
                if classification == "av_interference":
                    findings.append(Finding(category="av_interference", severity="hard-stop", slot=None,
                                             summary="TAR_ENTRY_ERROR detected in tunnel boot output.",
                                             detail="Needs Adam's one-time Defender/AV exclusion -- "
                                                    "this harness will never attempt that itself."))
                elif classification == "browser_auth_required":
                    findings.append(Finding(category="auth_reissuance", severity="action-needed", slot=None,
                                             summary="Tunnel boot required a fresh browser OAuth click.",
                                             detail="Blocks fully unattended looping past this point."))
                if not tunnel_boot_ok:
                    findings.append(Finding(category="boot_failure", severity="action-needed", slot=None,
                                             summary=f"Tunnel subprocess did not reach a settled boot state ({settle}).",
                                             detail=boot.captured[-2000:]))
                for msg in detect_cascading_disconnect(boot.lines):
                    findings.append(Finding(category="cascading_disconnect", severity="action-needed",
                                             slot=None, summary="Possible cascading cross-slot disconnect.",
                                             detail=msg))

    specs = await build_slot_specs(ctx.repo_path, slots=ctx.slots, include_optional=ctx.include_optional)
    slot_results: "list[SlotResult]" = []
    sem = asyncio.Semaphore(max(1, ctx.concurrency))

    async def _run_one(spec: SlotSpec) -> "list[SlotResult]":
        async with sem:
            do_cold_warm = cycle_num == 1 and spec.cold_fetch
            r1 = await test_slot(spec, ctx.repo_path, cold=True if do_cold_warm else None)
            tracker.record(spec.slot, r1.passed)
            if not do_cold_warm:
                return [r1]
            r2 = await test_slot(spec, ctx.repo_path, cold=False)
            tracker.record(spec.slot, r2.passed)
            if r1.spawn_ms and r2.spawn_ms and r1.spawn_ms > COLD_SPAWN_WARN_THRESHOLD_S * 1000:
                findings.append(Finding(
                    category="cold_spawn_slow", severity="action-needed", slot=spec.slot,
                    summary=f"{spec.slot}: cold spawn took {r1.spawn_ms / 1000:.1f}s "
                            f"(warm was {r2.spawn_ms / 1000:.1f}s).",
                    detail=f"Threshold is {COLD_SPAWN_WARN_THRESHOLD_S:.0f}s.",
                ))
            return [r1, r2]

    for group in await asyncio.gather(*[_run_one(s) for s in specs]):
        slot_results.extend(group)

    for r in slot_results:
        if r.classification == "av_interference":
            findings.append(Finding(category="av_interference", severity="hard-stop", slot=r.slot,
                                     summary=f"{r.slot}: TAR_ENTRY_ERROR detected during spawn.",
                                     detail="Needs Adam's one-time Defender/AV exclusion; never auto-retried."))
        elif r.classification == "registry_resolution":
            findings.append(Finding(category="registry_resolution", severity="action-needed", slot=r.slot,
                                     summary=f"{r.slot}: package-registry resolution failure.",
                                     detail=r.stderr_tail or r.error or ""))
        if r.repeat_consistent is False:
            findings.append(Finding(category="routing_mismatch", severity="action-needed", slot=r.slot,
                                     summary=f"{r.slot}: repeat functional call gave inconsistent results.",
                                     detail="Same tool + args called twice a few seconds apart; "
                                            "one succeeded and the other didn't."))

    if ctx.auto_fix:
        for r in slot_results:
            fixer = AUTO_FIXERS.get(r.classification or "")
            if fixer is None:
                continue
            spec = next((s for s in specs if s.slot == r.slot), None)
            if spec is None:
                continue
            applied = fixer(spec)
            findings.append(Finding(
                category="auto_fix_applied" if applied else "auto_fix_not_applicable",
                severity="info", slot=r.slot,
                summary=f"{r.slot}: {'applied' if applied else 'no applicable'} scoped cache-clear fix "
                        f"for classification {r.classification!r}.",
            ))

    if boot is not None:
        await boot.stop()

    return CycleResult(cycle=cycle_num, started_at=started, ended_at=_utcnow_iso(),
                        slot_results=slot_results, findings=findings, tunnel_boot_ok=tunnel_boot_ok)


async def check_auth_reuse(ctx: RunContext) -> Finding:
    """Empirical, full-cost version of failure category #7: start a real
    tunnel subprocess TWICE (kill between), scanning both boots for the
    browser-auth signature. Only ever called under ``--with-tunnel-boot``
    (starting a real subprocess twice is exactly the risky operation the
    module docstring's SAFETY section warns about) -- guarded the same way
    ``run_cycle``'s boot leg is.
    """
    if not ctx.log_dir:
        raise ValueError("check_auth_reuse requires ctx.log_dir")
    live_ports = detect_live_peer_ports(ALL_KNOWN_PORTS)
    if live_ports and not ctx.force_tunnel_boot:
        return Finding(
            category="auth_reissuance", severity="info", slot=None,
            summary="Skipped the empirical double-start auth-reuse check -- a live peer "
                    f"tunnel appears to own ports {live_ports}.",
            detail="Falling back to the read-only cache prediction only "
                   "(predict_needs_browser_auth). Pass --force-tunnel-boot to override.",
        )

    results = []
    for i in (1, 2):
        kill_stale_ports(ALL_KNOWN_PORTS, ctx.client_id)
        boot = TunnelSubprocess(ctx.repo_path, base_url=ctx.base_url,
                                 log_path=ctx.log_dir / f"auth_check_run{i}.log")
        try:
            # Same bound as run_cycle's boot leg -- a stall inside
            # create_subprocess_exec itself must not hang this coroutine (and
            # therefore the whole check) forever.
            await asyncio.wait_for(boot.start(), timeout=STARTUP_HARD_TIMEOUT_S)
        except asyncio.TimeoutError:
            return Finding(
                category="boot_failure", severity="action-needed", slot=None,
                summary=f"Tunnel subprocess (auth-reuse run {i}) failed to spawn within "
                        f"{STARTUP_HARD_TIMEOUT_S:.0f}s.",
                detail="create_subprocess_exec() itself never returned -- possibly an AV "
                       "on-access scan of the freshly-invoked pixi/python launcher, or pixi "
                       "environment resolution stalling. Auth-reuse check aborted.",
            )
        await boot.wait_for_settle()
        results.append(BROWSER_AUTH_SIGNATURE in boot.captured)
        await boot.stop()

    if any(results):
        return Finding(
            category="auth_reissuance",
            severity="hard-stop-for-full-automation" if all(results) else "action-needed",
            slot=None,
            summary="A tunnel restart required a fresh browser OAuth click.",
            detail=f"run1_needed_auth={results[0]} run2_needed_auth={results[1]}. "
                   "A fully unattended loop cannot click through a browser prompt -- "
                   "this blocks looping past the first cycle until a token is cached.",
        )
    return Finding(
        category="auth_reissuance", severity="info", slot=None,
        summary="Cached token reused across two consecutive tunnel starts -- no browser prompt.",
        detail="Confirms fully unattended looping is NOT blocked by re-auth.",
    )


async def run_loop(ctx: RunContext, *, max_cycles: int = DEFAULT_MAX_CYCLES) -> "tuple[int, list[CycleResult]]":
    """The fix-redeploy-retest driver. Returns ``(exit_code, cycles)``.

    Exit codes: ``0`` all tested slots solid (2 consecutive passes); ``1``
    non-convergence within *max_cycles*; ``2`` an AV hard-stop fired; ``3`` a
    browser-auth hard-stop fired for full automation.
    """
    tracker = ConvergenceTracker()
    tested_slots = list(ctx.slots) + (list(OPTIONAL_SLOTS) if ctx.include_optional else [])
    cycles: "list[CycleResult]" = []

    for cycle_num in range(1, max_cycles + 1):
        cycle = await run_cycle(ctx, cycle_num, tracker)
        cycles.append(cycle)

        if any(f.category == "av_interference" and f.severity == "hard-stop" for f in cycle.findings):
            return 2, cycles
        if any(f.severity == "hard-stop-for-full-automation" for f in cycle.findings):
            return 3, cycles
        if tracker.all_solid(tested_slots):
            return 0, cycles

    return 1, cycles


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: "list[str] | None" = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    p.add_argument("--repo", default=str(REPO_ROOT), help="Repo path to serve (default: this repo).")
    p.add_argument("--base-url", default=DEFAULT_BASE_URL)
    p.add_argument("--slots", default=",".join(DEFAULT_TESTED_SLOTS),
                    help="Comma-separated slot list to test.")
    p.add_argument("--include-optional", action="store_true",
                    help="Also test zotero/debug (off by default per the design note).")
    p.add_argument("--max-cycles", type=int, default=DEFAULT_MAX_CYCLES)
    p.add_argument("--single-cycle", action="store_true", help="Alias for --max-cycles 1.")
    p.add_argument("--with-tunnel-boot", action="store_true",
                    help="Also start a real `meridian --tunnel` subprocess for boot-level "
                         "validation (auth reuse, category-6 correlation). Off by default -- "
                         "see the module docstring's SAFETY section.")
    p.add_argument("--force-tunnel-boot", action="store_true",
                    help="Start a real tunnel subprocess even if a live peer session is detected. "
                         "Not recommended.")
    p.add_argument("--check-auth-reuse", action="store_true",
                    help="Run the empirical double-start category-7 check (implies --with-tunnel-boot).")
    p.add_argument("--auto-fix", action="store_true",
                    help="Apply the opt-in, well-understood client-side scoped-cache-clear fix "
                         "for registry-resolution failures. Never applied to AV/browser-auth findings.")
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument("--log-dir", default=None, help="Default: a fresh temp dir (path printed).")
    p.add_argument("--json-out", default=None, help="Default: <log-dir>/result.json.")
    return p.parse_args(argv)


async def _amain(args: argparse.Namespace) -> int:
    import tempfile

    log_dir = Path(args.log_dir) if args.log_dir else Path(tempfile.mkdtemp(prefix="tunnel_smoke_"))
    log_dir.mkdir(parents=True, exist_ok=True)
    print(f"tunnel-smoke-test: logs -> {log_dir}", flush=True)

    ctx = RunContext(
        repo_path=str(Path(args.repo).resolve()),
        base_url=args.base_url,
        slots=tuple(s.strip() for s in args.slots.split(",") if s.strip()),
        include_optional=args.include_optional,
        concurrency=args.concurrency,
        with_tunnel_boot=args.with_tunnel_boot or args.check_auth_reuse,
        force_tunnel_boot=args.force_tunnel_boot,
        log_dir=log_dir,
        auto_fix=args.auto_fix,
    )

    # Category 7 -- CHECK THIS FIRST, always, zero-risk.
    needs_auth = predict_needs_browser_auth(ctx.base_url)
    print(f"tunnel-smoke-test: cached-token check -> needs_browser_auth={needs_auth}", flush=True)
    auth_finding: "Finding | None" = None
    if args.check_auth_reuse:
        auth_finding = await check_auth_reuse(ctx)
        print(f"tunnel-smoke-test: auth-reuse check -> {auth_finding.summary}", flush=True)

    max_cycles = 1 if args.single_cycle else args.max_cycles
    exit_code, cycles = await run_loop(ctx, max_cycles=max_cycles)

    report = {
        "repo_path": ctx.repo_path,
        "base_url": ctx.base_url,
        "slots": list(ctx.slots),
        "needs_browser_auth_predicted": needs_auth,
        "auth_reuse_check": auth_finding.to_dict() if auth_finding else None,
        "exit_code": exit_code,
        "cycles": [c.to_dict() for c in cycles],
    }
    json_out = Path(args.json_out) if args.json_out else log_dir / "result.json"
    json_out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"tunnel-smoke-test: wrote {json_out}", flush=True)

    last = cycles[-1] if cycles else None
    if last:
        for r in last.slot_results:
            status = "PASS" if r.passed else "FAIL"
            print(f"  [{status}] {r.slot:10s} functional_ok={r.functional_ok} "
                  f"tools={r.tools_count} error={r.error}", flush=True)
        for f in last.findings:
            print(f"  FINDING [{f.severity}] {f.category}: {f.summary}", flush=True)
    print(f"tunnel-smoke-test: exit_code={exit_code}", flush=True)
    return exit_code


def main(argv: "list[str] | None" = None) -> int:
    args = parse_args(argv)
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    sys.exit(main())
