"""Tests for sprint item ac80aaaf — verify MCP connector provenance, tunnel
availability, scripts, and safe fallbacks before execution (v0.2.5).

Covers:
1. meridian.capability_availability — pure per-tool classification
   (available/degraded/missing/unknown) for builtin/plugin/stdio tool
   references, fail-closed behaviour for 'required' capabilities when the
   tunnel is down, degraded-ok pass-through, and fallback-chain rescue with
   structured provenance.
2. meridian.mcp.handlers.project_tools.check_capability_availability — the
   DB-aware wrapper, exercised with an explicit mocked ``live_inventory`` so
   no network/tunnel is ever touched.

None of these tests spawn a real tunnel, subprocess, or network call — every
"live" tunnel/plugin state is a plain dict constructed in-test.
"""
from __future__ import annotations

import pytest

from meridian import capability_availability as ca
from meridian import capability_manifest as cm
from meridian import db as db_module
from meridian.mcp.handlers import project_tools as pt


def _cap(**overrides):
    base = {
        "id": "code-search",
        "purpose": "find symbols/functions/classes",
        "required_tools": ["codebase__find_symbol"],
        "fallback_chain": [],
        "availability_policy": "required",
        "verification_command": None,
        "provenance": None,
    }
    base.update(overrides)
    return cm.normalize_capability(base)


def _inventory(**overrides):
    base = {
        "tunnel_reachable": True,
        "builtin_tools": {"start_session", "log_task"},
        "plugins": {
            "codebase": {"enabled": True, "invocable": True, "tools": {"find_symbol", "search_graph"}},
            "code-intel": {"enabled": True, "invocable": True, "tools": {"find_symbol", "search_graph"}},
            "filesystem": {"enabled": True, "invocable": True, "tools": {"read_file", "write_file"}},
        },
        "stdio_registry": {},
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# classify_tool — builtin tools (always available, tunnel-independent)
# ---------------------------------------------------------------------------

def test_classify_builtin_tool_always_available_even_with_tunnel_down():
    inv = _inventory(tunnel_reachable=False)
    result = ca.classify_tool("start_session", inv, policy="required")
    assert result["status"] == ca.STATUS_AVAILABLE
    assert result["kind"] == "builtin"


# ---------------------------------------------------------------------------
# classify_tool — plugin tools: available / degraded / missing / unknown
# ---------------------------------------------------------------------------

def test_classify_plugin_tool_available_when_live_and_confirmed():
    inv = _inventory()
    result = ca.classify_tool("codebase__find_symbol", inv, policy="required")
    assert result["status"] == ca.STATUS_AVAILABLE
    assert result["kind"] == "plugin"


def test_classify_plugin_whole_available_without_bare_tool_name():
    inv = _inventory()
    result = ca.classify_tool("filesystem", inv, policy="required")
    assert result["status"] == ca.STATUS_AVAILABLE


def test_classify_plugin_tool_unknown_when_not_seen_in_live_listing():
    inv = _inventory()
    result = ca.classify_tool("codebase__delete_everything", inv, policy="required")
    assert result["status"] == ca.STATUS_UNKNOWN


def test_classify_unrecognized_plugin_name_is_unknown():
    inv = _inventory()
    result = ca.classify_tool("totally-made-up-plugin__whatever", inv, policy="required")
    assert result["status"] == ca.STATUS_UNKNOWN
    assert result["kind"] == "unrecognized"


def test_classify_plugin_tool_missing_when_tunnel_down_and_policy_required():
    inv = _inventory(tunnel_reachable=False)
    result = ca.classify_tool("codebase__find_symbol", inv, policy="required")
    assert result["status"] == ca.STATUS_MISSING
    assert "tunnel is not connected" in result["detail"]


def test_classify_plugin_tool_missing_when_tunnel_down_and_policy_optional():
    inv = _inventory(tunnel_reachable=False)
    result = ca.classify_tool("codebase__find_symbol", inv, policy="optional")
    assert result["status"] == ca.STATUS_MISSING


def test_classify_plugin_tool_degraded_when_tunnel_down_and_policy_degraded_ok():
    inv = _inventory(tunnel_reachable=False)
    result = ca.classify_tool("codebase__find_symbol", inv, policy="degraded_ok")
    assert result["status"] == ca.STATUS_DEGRADED


def test_classify_plugin_not_enabled_is_unconfirmed_status():
    inv = _inventory(plugins={"codebase": {"enabled": False, "invocable": False, "tools": set()}})
    required = ca.classify_tool("codebase__find_symbol", inv, policy="required")
    degraded_ok = ca.classify_tool("codebase__find_symbol", inv, policy="degraded_ok")
    assert required["status"] == ca.STATUS_MISSING
    assert degraded_ok["status"] == ca.STATUS_DEGRADED


def test_classify_plugin_enabled_but_not_invocable_is_unconfirmed_status():
    inv = _inventory(plugins={"codebase": {"enabled": True, "invocable": False, "tools": set()}})
    required = ca.classify_tool("codebase__find_symbol", inv, policy="required")
    degraded_ok = ca.classify_tool("codebase__find_symbol", inv, policy="degraded_ok")
    assert required["status"] == ca.STATUS_MISSING
    assert degraded_ok["status"] == ca.STATUS_DEGRADED


def test_classify_empty_tool_ref_is_unknown():
    result = ca.classify_tool("", _inventory())
    assert result["status"] == ca.STATUS_UNKNOWN


# ---------------------------------------------------------------------------
# stdio identity validation — never executes anything
# ---------------------------------------------------------------------------

def test_is_allowed_stdio_command_accepts_known_launchers():
    assert ca.is_allowed_stdio_command(["uvx", "pytest"]) is True
    assert ca.is_allowed_stdio_command(["npx", "-y", "some-pkg"]) is True
    assert ca.is_allowed_stdio_command(["pixi", "run", "test"]) is True


def test_is_allowed_stdio_command_rejects_bare_path():
    assert ca.is_allowed_stdio_command([r"C:\Users\adam\repo\tool.exe"]) is False
    assert ca.is_allowed_stdio_command(["/home/adam/.local/bin/tool"]) is False


def test_is_allowed_stdio_command_rejects_empty_or_malformed():
    assert ca.is_allowed_stdio_command([]) is False
    assert ca.is_allowed_stdio_command(None) is False
    assert ca.is_allowed_stdio_command("uvx pytest") is False  # not a list
    assert ca.is_allowed_stdio_command(["uvx", 123]) is False


def test_stdio_identity_hash_deterministic():
    h1 = ca.stdio_identity_hash(["uvx", "pytest"])
    h2 = ca.stdio_identity_hash(["uvx", "pytest"])
    h3 = ca.stdio_identity_hash(["uvx", "pytest", "-k", "x"])
    assert h1 == h2
    assert h1 != h3


def test_classify_stdio_tool_available_when_registered_and_hash_matches():
    command = ["pixi", "run", "python", "verify.py"]
    inv = _inventory(stdio_registry={
        "verify": {"command": command, "config_hash": ca.stdio_identity_hash(command)},
    })
    result = ca.classify_tool("stdio:verify", inv)
    assert result["status"] == ca.STATUS_AVAILABLE
    assert result["kind"] == "stdio"


def test_classify_stdio_tool_unknown_when_not_registered():
    result = ca.classify_tool("stdio:never-declared", _inventory())
    assert result["status"] == ca.STATUS_UNKNOWN


def test_classify_stdio_tool_missing_when_command_not_allowed_pattern():
    inv = _inventory(stdio_registry={
        "shady": {"command": [r"C:\Users\adam\secret-tool.exe"], "config_hash": None},
    })
    result = ca.classify_tool("stdio:shady", inv)
    assert result["status"] == ca.STATUS_MISSING
    assert "allowed launcher pattern" in result["detail"]


def test_classify_stdio_tool_missing_on_config_hash_mismatch():
    command = ["uvx", "some-tool"]
    inv = _inventory(stdio_registry={
        "drifted": {"command": command, "config_hash": "not-the-real-hash"},
    })
    result = ca.classify_tool("stdio:drifted", inv)
    assert result["status"] == ca.STATUS_MISSING
    assert "hash mismatch" in result["detail"]


def test_classify_stdio_never_executes_command(monkeypatch):
    """Defense-in-depth: even a maliciously-shaped registry entry must never
    trigger a subprocess spawn. Patch subprocess.Popen/os.system to explode if
    called, then classify a stdio ref and confirm neither fires."""
    import os
    import subprocess

    def _boom(*a, **kw):
        raise AssertionError("stdio classification must never execute anything")

    monkeypatch.setattr(subprocess, "Popen", _boom)
    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(os, "system", _boom)

    command = ["uvx", "some-tool"]
    inv = _inventory(stdio_registry={
        "safe": {"command": command, "config_hash": ca.stdio_identity_hash(command)},
    })
    result = ca.classify_tool("stdio:safe", inv)
    assert result["status"] == ca.STATUS_AVAILABLE


# ---------------------------------------------------------------------------
# evaluate_capability_availability — overall rollup + fail-closed contract
# ---------------------------------------------------------------------------

def test_evaluate_available_when_all_required_tools_confirmed():
    cap = _cap()
    result = ca.evaluate_capability_availability(cap, _inventory())
    assert result["status"] == ca.STATUS_AVAILABLE
    assert result["fallback_used"] is None
    assert len(result["required_tools"]) == 1


def test_evaluate_fail_closed_missing_for_required_when_tunnel_down_no_fallback():
    cap = _cap(availability_policy="required")
    result = ca.evaluate_capability_availability(cap, _inventory(tunnel_reachable=False))
    assert result["status"] == ca.STATUS_MISSING
    assert result["fallback_used"] is None


def test_evaluate_degraded_ok_passes_through_as_degraded_when_tunnel_down():
    cap = _cap(availability_policy="degraded_ok")
    result = ca.evaluate_capability_availability(cap, _inventory(tunnel_reachable=False))
    assert result["status"] == ca.STATUS_DEGRADED


def test_evaluate_optional_policy_still_reports_missing_when_tunnel_down():
    """'optional' is not the same as 'degraded_ok' -- it does not soften the
    per-tool classification, it only means the caller isn't obligated to block
    on the capability. The classifier reports the honest status either way."""
    cap = _cap(availability_policy="optional")
    result = ca.evaluate_capability_availability(cap, _inventory(tunnel_reachable=False))
    assert result["status"] == ca.STATUS_MISSING


def test_evaluate_fallback_rescues_and_records_provenance():
    cap = _cap(
        required_tools=["codebase__delete_everything"],  # unknown -> won't be available
        fallback_chain=["filesystem__read_file"],
        availability_policy="required",
    )
    result = ca.evaluate_capability_availability(cap, _inventory())
    assert result["status"] == ca.STATUS_DEGRADED
    fb = result["fallback_used"]
    assert fb is not None
    assert fb["capability_id"] == "code-search"
    assert fb["failed_tool"] == "codebase__delete_everything"
    assert fb["fallback_tool"] == "filesystem__read_file"
    assert fb["fallback_status"] == ca.STATUS_AVAILABLE
    assert "recorded_at" in fb and fb["recorded_at"]


def test_evaluate_fallback_chain_tried_in_order_first_rescuer_wins():
    cap = _cap(
        required_tools=["codebase__delete_everything"],
        fallback_chain=["also-unknown__thing", "filesystem__read_file", "codebase__find_symbol"],
        availability_policy="required",
    )
    result = ca.evaluate_capability_availability(cap, _inventory())
    assert result["fallback_used"]["fallback_tool"] == "filesystem__read_file"


def test_evaluate_no_fallback_rescue_stays_missing_for_required():
    cap = _cap(
        required_tools=["codebase__delete_everything"],
        fallback_chain=["also-unknown__thing"],
        availability_policy="required",
    )
    result = ca.evaluate_capability_availability(cap, _inventory(tunnel_reachable=False))
    assert result["status"] == ca.STATUS_MISSING
    assert result["fallback_used"] is None


def test_evaluate_fallback_not_needed_when_primary_available():
    cap = _cap(fallback_chain=["filesystem__read_file"])
    result = ca.evaluate_capability_availability(cap, _inventory())
    assert result["status"] == ca.STATUS_AVAILABLE
    assert result["fallback_used"] is None


def test_evaluate_manifest_availability_applies_to_each_capability():
    caps = [_cap(id="a"), _cap(id="b", required_tools=["start_session"])]
    results = ca.evaluate_manifest_availability(caps, _inventory())
    assert [r["capability_id"] for r in results] == ["a", "b"]
    assert all(r["status"] == ca.STATUS_AVAILABLE for r in results)


def test_evaluate_manifest_availability_empty_list():
    assert ca.evaluate_manifest_availability([], _inventory()) == []


# ---------------------------------------------------------------------------
# check_capability_availability — DB-aware wrapper, mocked live_inventory
# (no network/tunnel touched: live_inventory is passed explicitly).
# ---------------------------------------------------------------------------

async def test_check_capability_availability_empty_manifest_returns_empty_list(db):
    project = await db_module.create_project(db, "cap-avail-empty")
    result = await pt.check_capability_availability(
        db, project["id"], live_inventory=_inventory(),
    )
    assert result == []


async def test_check_capability_availability_evaluates_persisted_manifest(db):
    project = await db_module.create_project(db, "cap-avail-basic")
    await db_module.set_project_capability_manifest(db, project["id"], [_cap()])
    result = await pt.check_capability_availability(
        db, project["id"], live_inventory=_inventory(),
    )
    assert len(result) == 1
    assert result[0]["capability_id"] == "code-search"
    assert result[0]["status"] == ca.STATUS_AVAILABLE


async def test_check_capability_availability_filters_by_capability_id(db):
    project = await db_module.create_project(db, "cap-avail-filter")
    await db_module.set_project_capability_manifest(
        db, project["id"], [_cap(id="a"), _cap(id="b", required_tools=["start_session"])],
    )
    result = await pt.check_capability_availability(
        db, project["id"], capability_id="b", live_inventory=_inventory(),
    )
    assert len(result) == 1
    assert result[0]["capability_id"] == "b"


async def test_check_capability_availability_fail_closed_when_tunnel_down(db):
    project = await db_module.create_project(db, "cap-avail-tunnel-down")
    await db_module.set_project_capability_manifest(db, project["id"], [_cap()])
    result = await pt.check_capability_availability(
        db, project["id"], live_inventory=_inventory(tunnel_reachable=False),
    )
    assert result[0]["status"] == ca.STATUS_MISSING


async def test_check_capability_availability_degraded_ok_when_tunnel_down(db):
    project = await db_module.create_project(db, "cap-avail-degraded-ok")
    await db_module.set_project_capability_manifest(
        db, project["id"], [_cap(availability_policy="degraded_ok")],
    )
    result = await pt.check_capability_availability(
        db, project["id"], live_inventory=_inventory(tunnel_reachable=False),
    )
    assert result[0]["status"] == ca.STATUS_DEGRADED


# ---------------------------------------------------------------------------
# _build_live_inventory — real (non-network) shape checks against a tenant
# with no active tunnel. Never spawns a real tunnel or hits the network:
# has_active_tunnel/tenant_owner_instance are pure in-memory lookups that are
# empty/False for a tenant id that was never connected.
# ---------------------------------------------------------------------------

async def test_build_live_inventory_no_tenant_is_unreachable_with_builtins_only():
    inv = await pt._build_live_inventory(None)
    assert inv["tunnel_reachable"] is False
    assert isinstance(inv["builtin_tools"], set) and len(inv["builtin_tools"]) > 0
    assert inv["stdio_registry"] == {}


async def test_build_live_inventory_unconnected_tenant_reports_unreachable():
    inv = await pt._build_live_inventory({"id": "never-connected-tenant-xyz", "tunnel_plugins": None})
    assert inv["tunnel_reachable"] is False
    # Built-in plugin slots are still enumerated (from resolve_plugins' defaults)
    # even though nothing is currently invocable.
    assert "filesystem" in inv["plugins"]
    assert inv["plugins"]["filesystem"]["invocable"] is False


async def test_check_capability_availability_derives_inventory_when_not_supplied(db):
    """Without an explicit live_inventory, the wrapper derives one via
    _build_live_inventory -- for a tenant with no live tunnel this must still
    resolve cleanly (fail-closed missing for a 'required' capability), not
    raise, proving the derivation path itself is exercised end-to-end without
    any real network/tunnel."""
    project = await db_module.create_project(db, "cap-avail-derived")
    await db_module.set_project_capability_manifest(db, project["id"], [_cap()])
    result = await pt.check_capability_availability(
        db, project["id"], tenant={"id": "never-connected-tenant-abc", "tunnel_plugins": None},
    )
    assert len(result) == 1
    assert result[0]["status"] == ca.STATUS_MISSING
