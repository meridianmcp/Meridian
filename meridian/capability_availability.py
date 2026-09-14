"""Capability availability verification (ac80aaaf).

Given a normalized capability entry (see :mod:`meridian.capability_manifest`
-- ``id``, ``required_tools``, ``fallback_chain``, ``availability_policy``)
this module classifies each declared tool against a *live inventory* snapshot
of the current MCP connector/tunnel state, and rolls those per-tool
classifications up into an overall capability status plus, when a fallback
had to fire, structured provenance describing which one and why.

This module is pure: no DB, no network, no subprocess. It never executes a
declared stdio command to "test" it -- for a ``stdio:<id>`` tool reference it
only validates the declared identity (command shape + a content hash) against
an allowed-launcher pattern, never shells out. Building the live inventory
from real tenant/tunnel state (an async, I/O-bound job) is a separate concern
layered on top -- see
:func:`meridian.mcp.handlers.project_tools.check_capability_availability`.

Tool-reference conventions (a ``required_tools``/``fallback_chain`` entry):

* ``"<plugin_name_or_slot_display_name>__<tool_name>"`` -- an MCP tool served
  by a tunnel plugin, e.g. ``"filesystem__read_file"``,
  ``"codebase__find_symbol"`` (matches the slot-prefixed names the tunnel
  bridge and ``list_plugins``/``get_plugin_details`` already advertise -- see
  ``routes/tunnel.py``'s ``SLOT_DISPLAY_NAMES``).
* ``"<plugin_name_or_slot_display_name>"`` (no ``__``) -- the plugin as a
  whole, without pinning to one specific tool name.
* a bare name matching a native, always-in-process Meridian MCP tool (from
  :func:`meridian.tool_manifest.build_tool_manifest`) -- these never depend
  on the tunnel and are always ``available`` when recognized.
* ``"stdio:<id>"`` -- a local command/script launched directly (not via a
  hosted MCP server). Verified via a caller-supplied ``stdio_registry``
  entry's declared command + content hash, never executed.

Status values: ``available`` | ``degraded`` | ``missing`` | ``unknown``.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

STATUS_AVAILABLE = "available"
STATUS_DEGRADED = "degraded"
STATUS_MISSING = "missing"
STATUS_UNKNOWN = "unknown"

VALID_STATUSES = frozenset({STATUS_AVAILABLE, STATUS_DEGRADED, STATUS_MISSING, STATUS_UNKNOWN})

# Worse-is-higher so `max(..., key=_STATUS_RANK.__getitem__)` finds the worst.
_STATUS_RANK = {STATUS_AVAILABLE: 0, STATUS_DEGRADED: 1, STATUS_UNKNOWN: 2, STATUS_MISSING: 3}

# Launchers a declared stdio command is allowed to start with. Deliberately
# conservative: an unrecognized launcher (or a bare machine-local absolute
# path) is refused rather than trusted, since we never execute the command to
# find out what it actually does.
_ALLOWED_STDIO_LAUNCHERS = frozenset({
    "uvx", "npx", "pixi", "python", "python3", "node", "cmd",
})

_STDIO_PREFIX = "stdio:"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_allowed_stdio_command(command: Any) -> bool:
    """True when *command* (a token list) starts with a recognized launcher.

    This is a pure shape/allowlist check -- it never inspects the filesystem
    or spawns anything. A bare local script path (no launcher token) is
    rejected: without executing it there is no safe way to confirm its
    identity, so it must be wrapped in a recognized launcher to be trusted.
    """
    if not isinstance(command, (list, tuple)) or not command:
        return False
    if not all(isinstance(tok, str) and tok for tok in command):
        return False
    return command[0].strip().lower() in _ALLOWED_STDIO_LAUNCHERS


def stdio_identity_hash(command: Any) -> str:
    """Stable sha256 of a stdio command's canonical JSON token list.

    Used to detect drift between a capability's declared ``config_hash`` and
    the command currently on file for that stdio identity -- never used to
    execute anything.
    """
    canonical = json.dumps(list(command or []), separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _unconfirmed_status(policy: str) -> str:
    """The status for a tunnel-backed tool that cannot be confirmed live.

    Fail-closed for every policy except ``degraded_ok``: a capability marked
    ``required`` (or ``optional``) never gets reported as available when the
    tunnel/slot backing it can't actually be confirmed right now.
    ``degraded_ok`` explicitly permits proceeding in a reduced/read-only mode,
    so it downgrades the same situation to ``degraded`` instead of ``missing``.
    """
    return STATUS_DEGRADED if policy == "degraded_ok" else STATUS_MISSING


def _classify_stdio(tool_ref: str, inventory: dict[str, Any]) -> dict[str, Any]:
    stdio_id = tool_ref[len(_STDIO_PREFIX):].strip()
    registry = inventory.get("stdio_registry") or {}
    entry = registry.get(stdio_id)
    if not isinstance(entry, dict):
        return {
            "tool": tool_ref, "kind": "stdio", "status": STATUS_UNKNOWN,
            "detail": (
                f"no declared stdio identity registered for '{stdio_id}'; "
                "cannot verify a local command/script without one."
            ),
        }
    command = entry.get("command")
    if not is_allowed_stdio_command(command):
        return {
            "tool": tool_ref, "kind": "stdio", "status": STATUS_MISSING,
            "detail": (
                f"declared stdio command for '{stdio_id}' does not match an allowed "
                "launcher pattern; refusing to trust an unrecognized local command "
                "without executing it."
            ),
        }
    declared_hash = entry.get("config_hash")
    computed_hash = stdio_identity_hash(command)
    if declared_hash and declared_hash != computed_hash:
        return {
            "tool": tool_ref, "kind": "stdio", "status": STATUS_MISSING,
            "detail": (
                f"config hash mismatch for stdio identity '{stdio_id}' -- the declared "
                "identity does not match its recorded hash (possible drift or tamper)."
            ),
        }
    return {
        "tool": tool_ref, "kind": "stdio", "status": STATUS_AVAILABLE,
        "detail": (
            f"stdio identity '{stdio_id}' verified via its declared command/config "
            "hash (not executed)."
        ),
    }


def classify_tool(tool_ref: str, inventory: dict[str, Any], *, policy: str = "required") -> dict[str, Any]:
    """Classify one ``required_tools``/``fallback_chain`` entry.

    Returns ``{"tool", "kind", "status", "detail"}``. ``inventory`` is the
    live-inventory snapshot shape documented at module level: ``{
    "tunnel_reachable": bool, "builtin_tools": set[str],
    "plugins": {name: {"enabled": bool, "invocable": bool, "tools": set[str]}},
    "stdio_registry": {id: {"command": [...], "config_hash": str|None}} }``.
    """
    ref = (tool_ref or "").strip()
    if not ref:
        return {"tool": tool_ref, "kind": "unrecognized", "status": STATUS_UNKNOWN,
                "detail": "empty tool reference"}
    if ref.startswith(_STDIO_PREFIX):
        return _classify_stdio(ref, inventory)

    builtin_tools = inventory.get("builtin_tools") or set()
    if ref in builtin_tools:
        return {
            "tool": ref, "kind": "builtin", "status": STATUS_AVAILABLE,
            "detail": "native Meridian MCP tool; always available in-process, independent of the tunnel.",
        }

    plugins = inventory.get("plugins") or {}
    if "__" in ref:
        plugin_name, _, bare_tool = ref.partition("__")
    else:
        plugin_name, bare_tool = ref, None

    plugin_entry = plugins.get(plugin_name)
    if plugin_entry is None:
        return {
            "tool": ref, "kind": "unrecognized", "status": STATUS_UNKNOWN,
            "detail": (
                f"'{plugin_name}' is not a known Meridian plugin/slot and not a native "
                "tool; cannot classify without a recognized identity."
            ),
        }

    if not inventory.get("tunnel_reachable"):
        status = _unconfirmed_status(policy)
        return {
            "tool": ref, "kind": "plugin", "status": status,
            "detail": (
                f"tunnel is not connected; '{plugin_name}' cannot be verified live "
                f"(resolved status: {status})."
            ),
        }
    if not plugin_entry.get("enabled", False):
        status = _unconfirmed_status(policy)
        return {
            "tool": ref, "kind": "plugin", "status": status,
            "detail": f"plugin '{plugin_name}' is not enabled in this tenant's tunnel configuration.",
        }
    if not plugin_entry.get("invocable", False):
        status = _unconfirmed_status(policy)
        return {
            "tool": ref, "kind": "plugin", "status": status,
            "detail": f"plugin '{plugin_name}' is enabled but not currently invocable (slot inactive or still starting).",
        }

    known_tools = plugin_entry.get("tools") or set()
    if bare_tool is None:
        return {
            "tool": ref, "kind": "plugin", "status": STATUS_AVAILABLE,
            "detail": f"plugin '{plugin_name}' is enabled and invocable.",
        }
    if bare_tool in known_tools:
        return {
            "tool": ref, "kind": "plugin", "status": STATUS_AVAILABLE,
            "detail": f"'{bare_tool}' confirmed live on plugin '{plugin_name}'.",
        }
    return {
        "tool": ref, "kind": "plugin", "status": STATUS_UNKNOWN,
        "detail": (
            f"plugin '{plugin_name}' is live, but '{bare_tool}' was not seen in its "
            "current tool listing -- may be a naming mismatch or a tool this slot "
            "doesn't expose."
        ),
    }


def evaluate_capability_availability(capability: dict[str, Any], inventory: dict[str, Any]) -> dict[str, Any]:
    """Roll up per-tool classifications into one capability availability verdict.

    *capability* should already be normalized (see
    :func:`meridian.capability_manifest.normalize_capability`). Returns::

        {
            "capability_id": str,
            "availability_policy": str,
            "status": "available"|"degraded"|"missing"|"unknown",
            "required_tools": [ {tool, kind, status, detail}, ... ],
            "fallback_used": None | {
                "capability_id", "policy", "failed_tool", "failed_status",
                "failed_detail", "fallback_tool", "fallback_status", "recorded_at",
            },
        }

    Fail-closed contract: for ``availability_policy: "required"`` (and
    ``"optional"``), a tunnel-backed tool that can't be confirmed live is
    reported ``missing`` -- this function never silently reports
    ``available`` when it could not actually verify the tool. For
    ``"degraded_ok"``, the same situation is reported ``degraded`` instead,
    so a caller may proceed in a reduced/read-only mode.

    When a required tool isn't fully available, each ``fallback_chain`` entry
    is tried in order; the first that classifies as ``available`` or
    ``degraded`` "rescues" the capability (overall status becomes
    ``degraded`` -- proceeding, but on a fallback, not the primary tool) and
    the rescue is recorded in ``fallback_used`` as auditable provenance (which
    fallback fired, why, and when). If no fallback rescues, the worst
    required-tool status passes through unchanged.
    """
    policy = capability.get("availability_policy") or "required"
    required_tools = capability.get("required_tools") or []
    fallback_chain = capability.get("fallback_chain") or []

    required_statuses = [classify_tool(t, inventory, policy=policy) for t in required_tools]
    if not required_statuses:
        return {
            "capability_id": capability.get("id"),
            "availability_policy": policy,
            "status": STATUS_UNKNOWN,
            "required_tools": [],
            "fallback_used": None,
        }

    worst = max(required_statuses, key=lambda s: _STATUS_RANK[s["status"]])
    fallback_used: dict[str, Any] | None = None

    if worst["status"] == STATUS_AVAILABLE:
        overall = STATUS_AVAILABLE
    else:
        rescue: dict[str, Any] | None = None
        for fb in fallback_chain:
            fb_status = classify_tool(fb, inventory, policy=policy)
            if fb_status["status"] in (STATUS_AVAILABLE, STATUS_DEGRADED):
                rescue = fb_status
                break
        if rescue is not None:
            overall = STATUS_DEGRADED
            fallback_used = {
                "capability_id": capability.get("id"),
                "policy": policy,
                "failed_tool": worst["tool"],
                "failed_status": worst["status"],
                "failed_detail": worst["detail"],
                "fallback_tool": rescue["tool"],
                "fallback_status": rescue["status"],
                "recorded_at": _utc_now_iso(),
            }
        else:
            overall = worst["status"]

    return {
        "capability_id": capability.get("id"),
        "availability_policy": policy,
        "status": overall,
        "required_tools": required_statuses,
        "fallback_used": fallback_used,
    }


def evaluate_manifest_availability(
    manifest_capabilities: list[dict[str, Any]], inventory: dict[str, Any],
) -> list[dict[str, Any]]:
    """:func:`evaluate_capability_availability` applied to a whole manifest's capabilities list."""
    return [evaluate_capability_availability(c, inventory) for c in (manifest_capabilities or [])]


def summarize_availability(evaluated: list[dict[str, Any]]) -> dict[str, Any]:
    """Bucket :func:`evaluate_manifest_availability`'s per-capability verdicts
    into the ``{available, missing, degraded}`` capability-id-list shape
    ``capability_contract.build_capability_contract``'s ``AvailabilityChecker``
    interface expects (an ``availability_checker`` callable passed to that
    function, or returned by :func:`check_local_first_availability` below).

    Fail-closed, policy-aware: a capability whose live status could not
    actually be confirmed (``STATUS_UNKNOWN`` -- an unrecognized tool
    reference, a naming mismatch, or the degenerate empty-``required_tools``
    case) is NEVER silently reported as available. It is bucketed the same
    way an unconfirmed tunnel-backed tool already is: ``missing`` for a
    ``required``/``optional`` capability (fail-closed), ``degraded`` for a
    ``degraded_ok`` capability (fail-open into a reduced/read-only mode, per
    that policy's documented contract).

    Deterministic: each bucket is a sorted list of capability ids, never
    input order, so two evaluations of the identical underlying state
    summarize byte-identically.
    """
    available: list[str] = []
    missing: list[str] = []
    degraded: list[str] = []
    for entry in evaluated or []:
        if not isinstance(entry, dict):
            continue
        cap_id = entry.get("capability_id")
        if not cap_id:
            continue
        status = entry.get("status")
        policy = entry.get("availability_policy") or "required"
        if status == STATUS_AVAILABLE:
            available.append(cap_id)
        elif status == STATUS_DEGRADED:
            degraded.append(cap_id)
        elif status == STATUS_MISSING:
            missing.append(cap_id)
        else:
            # STATUS_UNKNOWN (or any status this adapter doesn't recognize
            # yet) -- fail closed rather than silently dropping the
            # capability out of every bucket.
            if policy == "degraded_ok":
                degraded.append(cap_id)
            else:
                missing.append(cap_id)
    return {
        "available": sorted(available),
        "missing": sorted(missing),
        "degraded": sorted(degraded),
    }


def local_first_live_inventory() -> dict[str, Any]:
    """Tenant/tunnel-free live-inventory snapshot: native, always-in-process
    Meridian MCP tools only -- no plugins, no stdio identities, tunnel
    presumed unreachable (74c591b6, RESEARCH-OS WAVE B).

    This is the "local-first" floor a self-hosted or offline-tunnel session
    can always evaluate against, with zero DB/tenant/network access: a
    capability whose *entire* ``required_tools`` list is satisfied by a
    native tool -- e.g. ``research_retrieval``'s ``paper_search`` -- resolves
    to a genuine ``available`` verdict (see :func:`classify_tool`'s own
    builtin-tool branch, which is unconditional and needs no tunnel state at
    all), rather than staying permanently unverified. A capability that
    genuinely depends on a tunnel-backed plugin (Serena, codebase-memory,
    meridian-outputs, ...) still correctly resolves ``missing``/``degraded``
    here, since no tunnel is reachable from this snapshot -- this is an
    HONEST local floor, never a blanket "everything is available."

    Best-effort: any failure importing the static tool manifest degrades to
    an empty builtin-tools set (every plugin-backed capability then reports
    unrecognized/unconfirmed per policy) rather than raising.
    """
    try:
        from meridian.mcp_tools import _MCP_TOOLS_LIST  # noqa: PLC0415
        from meridian.tool_manifest import build_tool_manifest  # noqa: PLC0415

        manifest = build_tool_manifest(_MCP_TOOLS_LIST)
        builtin_tools = {
            t["name"] for t in manifest.get("tools", []) if isinstance(t, dict) and t.get("name")
        }
    except Exception:  # noqa: BLE001 — a local-first probe must never crash the caller
        builtin_tools = set()
    return {
        "tunnel_reachable": False,
        "builtin_tools": builtin_tools,
        "plugins": {},
        "stdio_registry": {},
    }


def check_local_first_availability(capabilities: list[dict[str, Any]]) -> dict[str, Any]:
    """Explicit, opt-in, tenant-free availability check (74c591b6).

    Classifies each declared capability's tools against
    :func:`local_first_live_inventory` via :func:`evaluate_manifest_availability`,
    then buckets the result via :func:`summarize_availability` -- the exact
    ``{available, missing, degraded}`` shape
    ``capability_contract.build_capability_contract``'s ``availability_checker``
    kwarg expects, so a caller passes this straight through:
    ``build_capability_contract(db, project_id,
    availability_checker=capability_availability.check_local_first_availability)``.

    Deliberately NOT auto-discovered by
    ``capability_contract._resolve_availability``'s own guessed sibling-module
    lookup (that lookup is named ``check_availability``, not this) -- wiring
    a real check into the bare, no-context default path would change the
    documented "unknown until a caller supplies real tenant/tunnel context"
    contract every existing capability_contract test pins (see
    ``tests/test_capability_contract.py::test_contract_empty_manifest_degrades_cleanly``
    and ``::test_contract_unknown_availability_fails_closed_for_required``).
    This function is instead an explicit, honest, always-available fallback
    a caller opts into -- exactly the "local-first" floor RESEARCH-OS WAVE B
    calls for: a capability whose only required tool is native (like
    ``research_retrieval``'s ``paper_search``) genuinely resolves
    ``available`` with no tunnel/tenant at all, while a tunnel-dependent
    capability honestly reports its real (unreachable-tunnel) status rather
    than a faked success.
    """
    evaluated = evaluate_manifest_availability(capabilities, local_first_live_inventory())
    return summarize_availability(evaluated)
