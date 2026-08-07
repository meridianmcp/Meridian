"""Tests for sprint item 23e20656 (665 follow-up) — unified per-item
executor_contract.

Covers meridian.executor_contract:

1. Canonical shape: every field the spec requires (mode, allowed_tools,
   forbidden_tools, steps, gate_after, output_requirements,
   completion_checks, item_id/version/scope, contract_hash) is present.
2. Deterministic hashing: stable for identical live item state, changes when
   contract-relevant state changes (tool_requirements, availability,
   dependency, wave gate, artifact declaration).
3. touches_resources (scheduling-only) stays structurally separate from the
   semantic tool_requirements-derived allowed_tools/forbidden_tools.
4. Required vs preferred tools preserved distinctly; unavailable required
   tools (and rescued-by-fallback ones) surfaced correctly; non-executable
   only when a REQUIRED tool is confirmed missing with no rescue.
5. Pointer resolution/provenance carried through verbatim from
   capability_contract.extract_sprint_item_pointers (never re-derived).
6. Dependency/wave/gate state surfaced explicitly, including the
   failure_mode='continue' carve-out.
7. Completion checks (required_notes / require_verification / prospecting)
   mirror what complete_sprint_item / claim_sprint_item actually enforce.
8. JSON/XML/human projections all derive from the SAME built object.
"""
from __future__ import annotations

import json as _json

import pytest

from meridian import capability_availability as ca
from meridian import capability_contract as cc
from meridian import db as db_module
from meridian import executor_contract as ec


def _planned_new_target(uri: str = "new_module.py"):
    return {
        "uri": uri,
        "target_kind": "planned_new",
        "selector": {"type": "range", "start_line": 1, "end_line": 1},
    }


# ---------------------------------------------------------------------------
# Canonical shape.
# ---------------------------------------------------------------------------

async def test_contract_has_required_shape_for_plain_item(db):
    project = await db_module.create_project(db, "ec-shape")
    item = await db_module.add_sprint_item(db, project["id"], "v1", "Plain item")
    contract = await ec.build_executor_contract(db, project["id"], item)

    for key in (
        "schema_version", "item_id", "version", "scope", "mode",
        "allowed_tools", "forbidden_tools", "scheduling", "steps",
        "gate_after", "output_requirements", "completion_checks",
        "executable", "executable_reasons", "generated_at", "contract_hash",
    ):
        assert key in contract, key

    assert contract["item_id"] == item["id"]
    assert contract["version"] == "v1"
    assert contract["scope"]["project_id"] == project["id"]
    assert contract["mode"] in ("autonomous", "interactive")
    assert contract["allowed_tools"] == []
    assert contract["forbidden_tools"] == []
    assert contract["executable"] is True
    assert contract["executable_reasons"] == []
    # A plain item with nothing declared still gets a "finish" step.
    assert contract["steps"][-1]["kind"] == "finish"


async def test_build_executor_contract_for_item_id_returns_none_for_unknown_item(db):
    project = await db_module.create_project(db, "ec-unknown-item")
    result = await ec.build_executor_contract_for_item_id(db, project["id"], "no-such-id")
    assert result is None


async def test_build_executor_contract_for_item_id_wraps_fetch(db):
    project = await db_module.create_project(db, "ec-fetch-wrap")
    item = await db_module.add_sprint_item(db, project["id"], "v1", "Fetch me")
    contract = await ec.build_executor_contract_for_item_id(db, project["id"], item["id"])
    assert contract is not None
    assert contract["item_id"] == item["id"]


# ---------------------------------------------------------------------------
# Deterministic hashing.
# ---------------------------------------------------------------------------

async def test_contract_hash_stable_for_identical_live_state(db):
    project = await db_module.create_project(db, "ec-hash-stable")
    item = await db_module.add_sprint_item(
        db, project["id"], "v1", "Stable item",
        tool_requirements=[{
            "name": "find_symbol", "server_or_namespace": "Serena",
            "required_or_preferred": "required", "purpose": "locate target",
        }],
    )
    fresh = await db_module.get_sprint_item(db, item["id"])
    contract_a = await ec.build_executor_contract(db, project["id"], fresh)
    contract_b = await ec.build_executor_contract(db, project["id"], fresh)
    # generated_at differs (real wall-clock), but the canonical serialization
    # and hash must be byte-identical for identical underlying state.
    assert ec.serialize_executor_contract(contract_a) == ec.serialize_executor_contract(contract_b)
    assert contract_a["contract_hash"] == contract_b["contract_hash"]


async def test_serialize_excludes_generated_at_and_contract_hash():
    contract = {
        "schema_version": 1, "item_id": "x", "generated_at": "2026-01-01T00:00:00+00:00",
        "contract_hash": "should-not-appear",
    }
    serialized = ec.serialize_executor_contract(contract)
    assert "2026-01-01T00:00:00" not in serialized
    assert "should-not-appear" not in serialized
    parsed = _json.loads(serialized)
    assert "generated_at" not in parsed
    assert "contract_hash" not in parsed


async def test_contract_hash_changes_when_tool_requirements_change(db):
    project = await db_module.create_project(db, "ec-hash-toolreq")
    item = await db_module.add_sprint_item(db, project["id"], "v1", "Item")
    before = await ec.build_executor_contract(db, project["id"], item)

    await db_module.patch_sprint_item(
        db, project["id"], item["id"],
        tool_requirements=[{
            "name": "find_symbol", "server_or_namespace": "Serena",
            "required_or_preferred": "required", "purpose": "locate target",
        }],
    )
    after_item = await db_module.get_sprint_item(db, item["id"])
    after = await ec.build_executor_contract(db, project["id"], after_item)
    assert before["contract_hash"] != after["contract_hash"]


async def test_contract_hash_changes_when_availability_changes(db):
    project = await db_module.create_project(db, "ec-hash-avail")
    item = await db_module.add_sprint_item(
        db, project["id"], "v1", "Item",
        tool_requirements=[{
            "name": "generate_handoff", "server_or_namespace": "meridian",
            "required_or_preferred": "required", "purpose": "hand off",
        }],
    )
    inv_missing = {"tunnel_reachable": False, "builtin_tools": set(), "plugins": {}, "stdio_registry": {}}
    inv_available = {"tunnel_reachable": False, "builtin_tools": {"generate_handoff"}, "plugins": {}, "stdio_registry": {}}
    contract_missing = await ec.build_executor_contract(db, project["id"], item, tool_inventory=inv_missing)
    contract_available = await ec.build_executor_contract(db, project["id"], item, tool_inventory=inv_available)
    assert contract_missing["contract_hash"] != contract_available["contract_hash"]


async def test_contract_hash_changes_when_dependency_state_changes(db):
    project = await db_module.create_project(db, "ec-hash-dep")
    parent = await db_module.add_sprint_item(db, project["id"], "v1", "Parent")
    child = await db_module.add_sprint_item(
        db, project["id"], "v1", "Child", depends_on=parent["id"],
    )
    contract_blocked = await ec.build_executor_contract(db, project["id"], child)
    assert contract_blocked["executable"] is False

    await db_module.complete_sprint_item(db, project["id"], parent["id"])
    fresh_child = await db_module.get_sprint_item(db, child["id"])
    contract_unblocked = await ec.build_executor_contract(db, project["id"], fresh_child)
    assert contract_unblocked["executable"] is True
    assert contract_blocked["contract_hash"] != contract_unblocked["contract_hash"]


# ---------------------------------------------------------------------------
# touches_resources (scheduling-only) vs tool_requirements (semantic).
# ---------------------------------------------------------------------------

async def test_scheduling_touches_resources_kept_separate_from_tool_requirements(db):
    project = await db_module.create_project(db, "ec-scheduling-split")
    item = await db_module.add_sprint_item(
        db, project["id"], "v1", "Touches + tools",
        touches_resources=["file:meridian/server.py"],
        tool_requirements=[{
            "name": "find_symbol", "server_or_namespace": "Serena",
            "required_or_preferred": "required", "purpose": "locate target",
            "fallback": ["grep"],
        }],
    )
    contract = await ec.build_executor_contract(db, project["id"], item)
    assert contract["scheduling"]["touches_resources"] == ["file:meridian/server.py"]
    # The scheduling resource id must never leak into the tool-instruction lists.
    tool_names = {t["name"] for t in contract["allowed_tools"]}
    assert "file:meridian/server.py" not in tool_names
    assert "find_symbol" in tool_names
    # And the tool name must never leak into scheduling.
    assert "find_symbol" not in contract["scheduling"]["touches_resources"]


# ---------------------------------------------------------------------------
# Required vs preferred; availability; forbidden_tools.
# ---------------------------------------------------------------------------

async def test_required_and_preferred_tools_both_preserved_distinctly(db):
    project = await db_module.create_project(db, "ec-req-pref")
    item = await db_module.add_sprint_item(
        db, project["id"], "v1", "Mixed tools",
        tool_requirements=[
            {
                "name": "find_symbol", "server_or_namespace": "Serena",
                "required_or_preferred": "required", "purpose": "locate target",
            },
            {
                "name": "paper_search", "server_or_namespace": "meridian",
                "required_or_preferred": "preferred", "purpose": "check prior art",
            },
        ],
    )
    contract = await ec.build_executor_contract(db, project["id"], item)
    by_name = {t["name"]: t for t in contract["allowed_tools"]}
    assert by_name["find_symbol"]["required_or_preferred"] == "required"
    assert by_name["paper_search"]["required_or_preferred"] == "preferred"
    # Neither is confirmed missing (no live inventory injected) -> forbidden empty.
    assert contract["forbidden_tools"] == []
    assert contract["executable"] is True


async def test_required_tool_confirmed_missing_is_forbidden_and_non_executable(db):
    project = await db_module.create_project(db, "ec-required-missing")
    item = await db_module.add_sprint_item(
        db, project["id"], "v1", "Needs a dead tool",
        tool_requirements=[{
            "name": "find_symbol", "server_or_namespace": "Serena",
            "required_or_preferred": "required", "purpose": "locate target",
        }],
    )
    inventory = {
        "tunnel_reachable": True,
        "builtin_tools": set(),
        "plugins": {"Serena": {"enabled": False, "invocable": False, "tools": set()}},
        "stdio_registry": {},
    }
    contract = await ec.build_executor_contract(db, project["id"], item, tool_inventory=inventory)
    assert contract["forbidden_tools"] == [
        {"name": "find_symbol", "server_or_namespace": "Serena",
         "reason": "required tool unavailable; no fallback declared"},
    ]
    assert contract["executable"] is False
    assert any("missing_required_tools" in r for r in contract["executable_reasons"])
    assert "Serena: find_symbol" in contract["executable_reasons"][0]


async def test_preferred_tool_confirmed_missing_never_forces_non_executable(db):
    project = await db_module.create_project(db, "ec-preferred-missing")
    item = await db_module.add_sprint_item(
        db, project["id"], "v1", "Prefers a dead tool",
        tool_requirements=[{
            "name": "find_symbol", "server_or_namespace": "Serena",
            "required_or_preferred": "preferred", "purpose": "locate target",
        }],
    )
    inventory = {
        "tunnel_reachable": True,
        "builtin_tools": set(),
        "plugins": {"Serena": {"enabled": False, "invocable": False, "tools": set()}},
        "stdio_registry": {},
    }
    contract = await ec.build_executor_contract(db, project["id"], item, tool_inventory=inventory)
    assert contract["forbidden_tools"] == []
    assert contract["executable"] is True
    assert contract["allowed_tools"][0]["availability_status"] == ca.STATUS_MISSING


async def test_required_tool_unknown_availability_never_forces_non_executable(db):
    """An unrecognized tool ref (no live inventory info either way) must
    degrade to 'unknown', not be treated as confirmed-missing — mirrors
    capability_contract's own tested rule for manifest capabilities."""
    project = await db_module.create_project(db, "ec-unknown-avail")
    item = await db_module.add_sprint_item(
        db, project["id"], "v1", "Needs an unrecognized tool",
        tool_requirements=[{
            "name": "find_symbol", "server_or_namespace": "Serena",
            "required_or_preferred": "required", "purpose": "locate target",
        }],
    )
    contract = await ec.build_executor_contract(
        db, project["id"], item,
        tool_inventory={"tunnel_reachable": False, "builtin_tools": set(), "plugins": {}, "stdio_registry": {}},
    )
    assert contract["allowed_tools"][0]["availability_status"] == ca.STATUS_UNKNOWN
    assert contract["forbidden_tools"] == []
    assert contract["executable"] is True


async def test_required_tool_rescued_by_fallback_stays_executable(db):
    """Reuses ac80aaaf's real fallback-rescue algorithm (via
    evaluate_capability_availability) rather than reimplementing it: a
    required tool that fails to classify, but whose declared fallback DOES
    classify as available, rescues the capability to 'degraded' — never
    'missing' — so it must not land in forbidden_tools."""
    project = await db_module.create_project(db, "ec-fallback-rescue")
    item = await db_module.add_sprint_item(
        db, project["id"], "v1", "Has a working fallback",
        tool_requirements=[{
            "name": "find_symbol", "server_or_namespace": "Serena",
            "required_or_preferred": "required", "purpose": "locate target",
            "fallback": ["grep_search"],
        }],
    )
    inventory = {
        "tunnel_reachable": False,
        "builtin_tools": {"grep_search"},
        "plugins": {},
        "stdio_registry": {},
    }
    contract = await ec.build_executor_contract(db, project["id"], item, tool_inventory=inventory)
    assert contract["forbidden_tools"] == []
    assert contract["executable"] is True
    entry = contract["allowed_tools"][0]
    assert entry["availability_status"] == ca.STATUS_DEGRADED
    assert entry["fallback_used"] is not None
    assert entry["fallback_used"]["fallback_tool"] == "grep_search"


async def test_tool_availability_checker_override_is_used_verbatim(db):
    project = await db_module.create_project(db, "ec-checker-override")
    item = await db_module.add_sprint_item(
        db, project["id"], "v1", "Custom checker",
        tool_requirements=[{
            "name": "find_symbol", "server_or_namespace": "Serena",
            "required_or_preferred": "required", "purpose": "locate target",
        }],
    )

    def _checker(requirements):
        return {
            ("Serena", "find_symbol"): {"status": "missing", "fallback_used": None},
        }

    contract = await ec.build_executor_contract(
        db, project["id"], item, tool_availability_checker=_checker,
    )
    assert contract["forbidden_tools"]
    assert contract["executable"] is False


async def test_tool_availability_checker_exception_degrades_gracefully(db):
    project = await db_module.create_project(db, "ec-checker-crash")
    item = await db_module.add_sprint_item(
        db, project["id"], "v1", "Crashy checker",
        tool_requirements=[{
            "name": "find_symbol", "server_or_namespace": "Serena",
            "required_or_preferred": "required", "purpose": "locate target",
        }],
    )

    def _bad_checker(_requirements):
        raise RuntimeError("boom")

    contract = await ec.build_executor_contract(
        db, project["id"], item, tool_availability_checker=_bad_checker,
    )
    assert contract["allowed_tools"][0]["availability_status"] == ca.STATUS_UNKNOWN
    assert contract["forbidden_tools"] == []
    assert contract["executable"] is True


# ---------------------------------------------------------------------------
# Pointer resolution / provenance — reused verbatim from capability_contract.
# ---------------------------------------------------------------------------

async def test_contract_pointers_match_capability_contract_extraction(db):
    project = await db_module.create_project(db, "ec-pointers-match")
    item = await db_module.add_sprint_item(
        db, project["id"], "v1", "Has a pointer",
        touches_resources=["file:new_module.py"],
    )
    await db_module.add_sprint_item_pointer(
        db, project["id"], item["id"], "code", [_planned_new_target()],
    )
    fresh = await db_module.get_sprint_item(db, item["id"])
    contract = await ec.build_executor_contract(db, project["id"], fresh)

    expected = await cc.extract_sprint_item_pointers(db, project["id"], [fresh])
    assert contract["pointers"] == (expected[0] if expected else None)
    assert contract["pointers"] is not None
    assert contract["pointers"]["pointers"][0]["targets"][0]["status"] == "planned"


async def test_unprospected_resources_block_executability(db):
    project = await db_module.create_project(db, "ec-unprospected")
    item = await db_module.add_sprint_item(
        db, project["id"], "v1", "Declares resources, no pointer evidence",
        touches_resources=["file:ghost.py"],
    )
    contract = await ec.build_executor_contract(db, project["id"], item)
    assert contract["completion_checks"]["prospecting"]["declares_resources"] is True
    assert contract["completion_checks"]["prospecting"]["has_pointer_evidence"] is False
    assert contract["completion_checks"]["prospecting"]["prospected"] is False
    assert contract["executable"] is False
    assert "unprospected_resources" in contract["executable_reasons"]
    assert any(s["kind"] == "blocked" for s in contract["steps"])


async def test_prospect_bypass_restores_executability(db):
    project = await db_module.create_project(db, "ec-prospect-bypass")
    item = await db_module.add_sprint_item(
        db, project["id"], "v1", "Bypassed",
        touches_resources=["file:ghost.py"], prospect_bypass=True,
    )
    contract = await ec.build_executor_contract(db, project["id"], item)
    assert contract["completion_checks"]["prospecting"]["prospected"] is True
    assert contract["executable"] is True


# ---------------------------------------------------------------------------
# output_requirements — reused verbatim from artifact_declaration.
# ---------------------------------------------------------------------------

async def test_output_requirements_mirrors_artifact_declaration(db):
    project = await db_module.create_project(db, "ec-output-reqs")
    item = await db_module.add_sprint_item(db, project["id"], "v1", "Produces a figure")
    planned_output = {
        "source_type": "code",
        "targets": [_planned_new_target("figures/output.png")],
        "provenance_required": True,
    }
    await db_module.patch_sprint_item(
        db, project["id"], item["id"],
        artifact_kind="figure", planned_output=planned_output,
        artifact_policy={"artifact_pointer_check": "strict"},
    )
    fresh = await db_module.get_sprint_item(db, item["id"])
    contract = await ec.build_executor_contract(db, project["id"], fresh)

    assert contract["output_requirements"]["artifact_kind"] == "figure"
    assert contract["output_requirements"]["declared"] is True
    assert contract["output_requirements"]["planned_output"]["provenance_required"] is True
    assert contract["output_requirements"]["policy"]["artifact_pointer_check"] == "strict"
    assert any(s["kind"] == "output_write" for s in contract["steps"])


async def test_output_requirements_absent_for_undeclared_item(db):
    project = await db_module.create_project(db, "ec-output-none")
    item = await db_module.add_sprint_item(db, project["id"], "v1", "No artifact")
    contract = await ec.build_executor_contract(db, project["id"], item)
    assert contract["output_requirements"]["artifact_kind"] is None
    assert contract["output_requirements"]["planned_output"] is None
    assert contract["output_requirements"]["declared"] is False


# ---------------------------------------------------------------------------
# Dependency / wave / gate state.
# ---------------------------------------------------------------------------

async def test_dependency_failure_mode_continue_carve_out(db):
    project = await db_module.create_project(db, "ec-dep-continue")
    parent = await db_module.add_sprint_item(db, project["id"], "v1", "Parent")
    child = await db_module.add_sprint_item(
        db, project["id"], "v1", "Child", depends_on=parent["id"], failure_mode="continue",
    )
    await db_module.fail_sprint_item(db, project["id"], parent["id"], reason="broke")
    fresh_child = await db_module.get_sprint_item(db, child["id"])
    contract = await ec.build_executor_contract(db, project["id"], fresh_child)
    assert contract["dependency"]["satisfied"] is True
    assert contract["dependency"]["blocking_item"] is None


async def test_dependency_failure_mode_stop_blocks(db):
    project = await db_module.create_project(db, "ec-dep-stop")
    parent = await db_module.add_sprint_item(db, project["id"], "v1", "Parent")
    child = await db_module.add_sprint_item(
        db, project["id"], "v1", "Child", depends_on=parent["id"], failure_mode="stop",
    )
    await db_module.fail_sprint_item(db, project["id"], parent["id"], reason="broke")
    fresh_child = await db_module.get_sprint_item(db, child["id"])
    contract = await ec.build_executor_contract(db, project["id"], fresh_child)
    assert contract["dependency"]["satisfied"] is False
    assert contract["executable"] is False


async def test_gate_after_and_gate_blocking_are_explicit(db):
    project = await db_module.create_project(db, "ec-wave-gates")
    item_w1 = await db_module.add_sprint_item(db, project["id"], "v1", "Configure ingest pipeline", wave="wave-1")
    item_w2 = await db_module.add_sprint_item(db, project["id"], "v1", "Deploy notification service", wave="wave-2")
    await db_module.configure_wave_gate(
        db, project["id"], wave_end="wave-1", actions=[{"type": "wait", "seconds": 1}],
    )

    contract_w1 = await ec.build_executor_contract(db, project["id"], item_w1)
    assert contract_w1["gate_after"] is not None
    assert contract_w1["gate_after"]["wave_end"] == "wave-1"
    assert contract_w1["gate_after"]["gate_passed"] is False
    assert contract_w1["gate_blocking"] is None
    assert contract_w1["executable"] is True

    contract_w2 = await ec.build_executor_contract(db, project["id"], item_w2)
    assert contract_w2["gate_blocking"] is not None
    assert contract_w2["gate_blocking"]["wave_end"] == "wave-1"
    assert contract_w2["executable"] is False
    assert any("wave_gate_pending:wave-1" in r for r in contract_w2["executable_reasons"])

    await db_module.complete_wave_gate(
        db, project["id"], "wave-1", {"status": "ok", "exit_code": 0},
    )
    fresh_w1 = await db_module.get_sprint_item(db, item_w1["id"])
    fresh_w2 = await db_module.get_sprint_item(db, item_w2["id"])
    contract_w1_after = await ec.build_executor_contract(db, project["id"], fresh_w1)
    contract_w2_after = await ec.build_executor_contract(db, project["id"], fresh_w2)
    assert contract_w1_after["gate_after"]["gate_passed"] is True
    assert contract_w2_after["gate_blocking"] is None
    assert contract_w2_after["executable"] is True
    assert contract_w1["contract_hash"] != contract_w1_after["contract_hash"]


# ---------------------------------------------------------------------------
# Completion checks.
# ---------------------------------------------------------------------------

async def test_required_notes_completion_check_reflects_evidence(db):
    project = await db_module.create_project(db, "ec-required-notes")
    item = await db_module.add_sprint_item(db, project["id"], "v1", "Needs notes")
    await db_module.patch_sprint_item(db, project["id"], item["id"], required_notes=True)
    fresh = await db_module.get_sprint_item(db, item["id"])
    contract = await ec.build_executor_contract(db, project["id"], fresh)
    assert contract["completion_checks"]["required_notes"] is True
    assert contract["completion_checks"]["required_notes_satisfied"] is False
    assert any(
        s["kind"] == "completion_check" and "required_notes" in s["description"]
        for s in contract["steps"]
    )

    await db_module.patch_sprint_item(db, project["id"], item["id"], notes="shipped the thing")
    fresh2 = await db_module.get_sprint_item(db, item["id"])
    contract2 = await ec.build_executor_contract(db, project["id"], fresh2)
    assert contract2["completion_checks"]["required_notes_satisfied"] is True


async def test_require_verification_completion_check_reflects_on_file_verdict(db):
    project = await db_module.create_project(db, "ec-require-verification")
    item = await db_module.add_sprint_item(db, project["id"], "v1", "Needs verification")
    await db_module.patch_sprint_item(db, project["id"], item["id"], require_verification=True)
    fresh = await db_module.get_sprint_item(db, item["id"])
    contract = await ec.build_executor_contract(db, project["id"], fresh)
    assert contract["completion_checks"]["require_verification"] is True
    assert contract["completion_checks"]["require_verification_satisfied"] is False
    assert contract["completion_checks"]["verification_on_file"] is None

    await db_module.record_sprint_item_verification(
        db, project["id"], item["id"], "verifier-session", "pass", notes="looks good",
    )
    fresh2 = await db_module.get_sprint_item(db, item["id"])
    contract2 = await ec.build_executor_contract(db, project["id"], fresh2)
    assert contract2["completion_checks"]["require_verification_satisfied"] is True
    assert contract2["completion_checks"]["verification_on_file"]["verdict"] == "pass"


# ---------------------------------------------------------------------------
# JSON/XML/human projections all derive from the SAME object.
# ---------------------------------------------------------------------------

async def test_projections_derive_from_same_object_not_three_implementations(db):
    project = await db_module.create_project(db, "ec-projections")
    item = await db_module.add_sprint_item(
        db, project["id"], "v1", "Projected item",
        tool_requirements=[{
            "name": "find_symbol", "server_or_namespace": "Serena",
            "required_or_preferred": "required", "purpose": "locate target",
        }],
        wave="wave-1",
    )
    await db_module.configure_wave_gate(
        db, project["id"], wave_end="wave-1", actions=[{"type": "wait", "seconds": 1}],
    )
    fresh = await db_module.get_sprint_item(db, item["id"])
    contract = await ec.build_executor_contract(db, project["id"], fresh)

    as_json = ec.to_json(contract)
    as_xml = ec.render_xml_clause(contract)
    as_text = ec.render_text(contract)

    parsed = _json.loads(as_json)
    assert parsed["item_id"] == contract["item_id"] == item["id"]
    assert parsed["contract_hash"] == contract["contract_hash"]

    # Every projection must agree on the SAME item_id, mode, and tool name —
    # pulled from the identical built object, not independently re-derived.
    assert item["id"] in as_xml
    assert item["id"] in as_text
    assert "find_symbol" in as_xml
    assert "find_symbol" in as_text
    assert contract["mode"] in as_text
    assert f'contract_hash="{contract["contract_hash"]}"' in as_xml
    assert contract["gate_after"]["wave_end"] in as_xml
    assert contract["gate_after"]["wave_end"] in as_text


def test_render_xml_clause_pure_over_static_contract():
    """No DB / no re-fetch — render_xml_clause is a pure function of an
    already-built contract dict."""
    contract = {
        "item_id": "abc123", "mode": "autonomous", "executable": False,
        "contract_hash": "deadbeef",
        "executable_reasons": ["missing_required_tools:Serena: find_symbol"],
        "allowed_tools": [{
            "name": "find_symbol", "server_or_namespace": "Serena",
            "required_or_preferred": "required", "availability_status": "missing",
        }],
        "forbidden_tools": [{"name": "find_symbol", "server_or_namespace": "Serena"}],
        "steps": [{"order": 1, "kind": "finish", "description": "Call complete_sprint_item."}],
        "gate_after": None,
    }
    xml = ec.render_xml_clause(contract)
    assert xml.startswith("<executor_contract")
    assert xml.endswith("</executor_contract>")
    assert "Serena: find_symbol" in xml
    assert "missing_required_tools" in xml


def test_render_text_pure_over_static_contract():
    contract = {
        "item_id": "abc123", "version": "v1", "mode": "autonomous",
        "executable": True, "executable_reasons": [],
        "allowed_tools": [], "forbidden_tools": [],
        "steps": [{"order": 1, "kind": "finish", "description": "Call complete_sprint_item."}],
        "gate_after": None,
    }
    text = ec.render_text(contract)
    assert "abc123" in text
    assert "1. Call complete_sprint_item." in text


# ===========================================================================
# 3b3020ac — hash-pinned scientific execution manifests, per-worker
# completion records, and fail-closed aggregation.
#
# All of this is DB-free and synchronous (see the module section docstring
# in meridian/executor_contract.py) — no `db` fixture needed.
# ===========================================================================

def _base_manifest_kwargs(**overrides):
    kwargs = dict(
        project_id="proj-a",
        version="v1",
        run_id="run-1",
        runner_name="thesis-runner",
        expected_counts={"images": 3, "branches": 2},
        output_schema={
            "version": "1.0",
            "domains": {
                "images": {"kind": "raw", "fields": ["path"]},
                "branch_metrics": {"kind": "dse", "fields": ["branch_id"]},
            },
        },
        config_fingerprint={"solver": "adam", "lr": 0.01},
        python_version="3.12.4",
        pixi_env_fingerprint="pixi-hash-abc",
    )
    kwargs.update(overrides)
    return kwargs


def _stub_hasher(mapping):
    """Injectable hash_files seam: returns a fixed {path: hash} dict for
    every path requested, defaulting missing entries to a stable sha256-like
    stand-in so tests never touch the real filesystem."""
    def _hash(paths):
        return {p: mapping.get(p, "0" * 64) for p in paths}
    return _hash


# ---------------------------------------------------------------------------
# Manifest construction, hashing, immutability, secret/path screening.
# ---------------------------------------------------------------------------

class TestBuildExecutionManifest:
    def test_manifest_has_required_shape(self):
        manifest = ec.build_execution_manifest(**_base_manifest_kwargs())
        for key in (
            "schema_version", "run_id", "scope", "lineage", "runner_identity",
            "input_identity", "git_state", "runtime_fingerprint",
            "config_fingerprint", "expected_counts", "output_schema",
            "allow_partial", "created_at", "manifest_hash",
        ):
            assert key in manifest, key
        assert manifest["scope"] == {"project_id": "proj-a", "version": "v1"}
        assert manifest["run_id"] == "run-1"
        assert manifest["output_schema"]["domains"]["images"]["kind"] == "raw"
        assert manifest["output_schema"]["domains"]["branch_metrics"]["kind"] == "dse"

    def test_manifest_requires_core_identity_fields(self):
        for missing in ("project_id", "version", "run_id", "runner_name"):
            kwargs = _base_manifest_kwargs()
            kwargs[missing] = ""
            with pytest.raises(ec.ExecutionManifestError):
                ec.build_execution_manifest(**kwargs)

    def test_manifest_hash_deterministic_for_identical_inputs(self):
        """Idempotent rerun: building the SAME manifest twice (identical
        inputs) yields byte-identical hashes — the caller can safely re-plan
        the same run without minting spurious 'different' manifests."""
        m1 = ec.build_execution_manifest(**_base_manifest_kwargs())
        m2 = ec.build_execution_manifest(**_base_manifest_kwargs())
        assert m1["manifest_hash"] == m2["manifest_hash"]
        # created_at is wall-clock and MUST NOT be part of the hash.
        assert ec.execution_manifest_hash(m1) == ec.execution_manifest_hash(m2)

    def test_manifest_hash_changes_when_inputs_change(self):
        m1 = ec.build_execution_manifest(**_base_manifest_kwargs())
        m2 = ec.build_execution_manifest(**_base_manifest_kwargs(config_fingerprint={"solver": "sgd"}))
        assert m1["manifest_hash"] != m2["manifest_hash"]

    def test_two_project_version_isolation(self):
        """Two manifests differing ONLY in project_id/version get different
        hashes and different scopes — a run in one project/version can never
        be mistaken for a run in another, even with identical run_id and
        otherwise-identical content."""
        m_proj_a = ec.build_execution_manifest(**_base_manifest_kwargs(project_id="proj-a", version="v1"))
        m_proj_b = ec.build_execution_manifest(**_base_manifest_kwargs(project_id="proj-b", version="v1"))
        m_v2 = ec.build_execution_manifest(**_base_manifest_kwargs(project_id="proj-a", version="v2"))
        assert m_proj_a["manifest_hash"] != m_proj_b["manifest_hash"]
        assert m_proj_a["manifest_hash"] != m_v2["manifest_hash"]
        assert m_proj_a["scope"] != m_proj_b["scope"]
        assert m_proj_a["scope"] != m_v2["scope"]

    def test_runner_and_input_hashes_use_injected_hasher(self):
        hasher = _stub_hasher({"runner.py": "a" * 64, "input.csv": "b" * 64})
        manifest = ec.build_execution_manifest(
            **_base_manifest_kwargs(
                runner_source_paths=["runner.py"],
                input_paths=["input.csv"],
                hash_files=hasher,
            )
        )
        assert manifest["runner_identity"]["source_hashes"] == {"runner.py": "a" * 64}
        assert manifest["input_identity"]["file_hashes"] == {"input.csv": "b" * 64}
        assert manifest["runner_identity"]["source_set_hash"]
        assert manifest["input_identity"]["file_set_hash"]

    def test_git_state_and_lineage_carried_through(self):
        manifest = ec.build_execution_manifest(
            **_base_manifest_kwargs(
                sprint_item_id="item-123",
                decision_ids=["dec-2", "dec-1"],
                git_head="deadbeef" * 5,
                git_dirty_files=["b.py", "a.py"],
            )
        )
        assert manifest["lineage"]["sprint_item_id"] == "item-123"
        assert manifest["lineage"]["decision_ids"] == ["dec-1", "dec-2"]
        assert manifest["git_state"]["head"] == "deadbeef" * 5
        assert manifest["git_state"]["dirty_files"] == ["a.py", "b.py"]

    def test_secret_shaped_config_fingerprint_rejected(self):
        with pytest.raises(ec.ExecutionManifestError):
            ec.build_execution_manifest(
                **_base_manifest_kwargs(
                    config_fingerprint={"token": "sk-abcdefghij1234567890"}
                )
            )

    def test_absolute_local_path_in_runtime_fingerprint_rejected(self):
        with pytest.raises(ec.ExecutionManifestError):
            ec.build_execution_manifest(
                **_base_manifest_kwargs(
                    runtime_fingerprint_extra={"venv_path": r"C:\Users\alice\venv"}
                )
            )

    def test_input_and_runner_paths_are_not_secret_screened(self):
        """File-path fields (runner_source_paths / input_paths) are
        LEGITIMATELY real paths — they must never be rejected by the
        secret/local-path screen that only applies to the fingerprint
        sub-objects."""
        manifest = ec.build_execution_manifest(
            **_base_manifest_kwargs(
                runner_source_paths=[r"C:\Users\alice\thesis\runner.py"],
                input_paths=[r"C:\Users\alice\thesis\data.csv"],
                hash_files=lambda paths: {p: None for p in paths},
            )
        )
        assert r"C:\Users\alice\thesis\runner.py" in manifest["runner_identity"]["source_hashes"]

    def test_expected_counts_must_be_non_negative_ints(self):
        with pytest.raises(ec.ExecutionManifestError):
            ec.build_execution_manifest(**_base_manifest_kwargs(expected_counts={"images": -1}))
        with pytest.raises(ec.ExecutionManifestError):
            ec.build_execution_manifest(**_base_manifest_kwargs(expected_counts={"images": "3"}))


class TestOutputSchemaDomainTagging:
    def test_domain_missing_kind_rejected(self):
        with pytest.raises(ec.ExecutionManifestError):
            ec.build_execution_manifest(
                **_base_manifest_kwargs(
                    output_schema={"version": "1.0", "domains": {"images": {}}}
                )
            )

    def test_domain_unknown_kind_rejected(self):
        with pytest.raises(ec.ExecutionManifestError):
            ec.build_execution_manifest(
                **_base_manifest_kwargs(
                    output_schema={
                        "version": "1.0",
                        "domains": {"images": {"kind": "processed"}},
                    }
                )
            )

    def test_no_domains_declared_is_valid_backward_compat_state(self):
        manifest = ec.build_execution_manifest(**_base_manifest_kwargs(output_schema=None))
        assert manifest["output_schema"] == {"version": None, "domains": {}}


class TestManifestImmutability:
    def test_first_write_always_safe(self):
        m = ec.build_execution_manifest(**_base_manifest_kwargs())
        ok, reason = ec.check_manifest_immutable(None, m)
        assert ok is True
        assert reason is None

    def test_idempotent_rewrite_of_identical_manifest_is_safe(self):
        m1 = ec.build_execution_manifest(**_base_manifest_kwargs())
        m2 = ec.build_execution_manifest(**_base_manifest_kwargs())
        ok, reason = ec.check_manifest_immutable(m1, m2)
        assert ok is True
        assert reason is None

    def test_mutating_an_existing_run_is_refused(self):
        m1 = ec.build_execution_manifest(**_base_manifest_kwargs())
        m2 = ec.build_execution_manifest(**_base_manifest_kwargs(config_fingerprint={"solver": "sgd"}))
        ok, reason = ec.check_manifest_immutable(m1, m2)
        assert ok is False
        assert "different hash" in reason.lower() or "different" in reason.lower()

    def test_unrelated_scope_is_rejected(self):
        m1 = ec.build_execution_manifest(**_base_manifest_kwargs(project_id="proj-a"))
        m2 = ec.build_execution_manifest(**_base_manifest_kwargs(project_id="proj-b"))
        ok, reason = ec.check_manifest_immutable(m1, m2)
        assert ok is False
        assert "scope" in reason.lower()


# ---------------------------------------------------------------------------
# Per-worker completion records + skip-existing.
# ---------------------------------------------------------------------------

def _manifest():
    return ec.build_execution_manifest(**_base_manifest_kwargs())


def _complete_record(manifest, worker_id="w1", **overrides):
    kwargs = dict(
        manifest=manifest, worker_id=worker_id, status=ec.WORKER_STATUS_COMPLETE,
        input_hash="in-1", config_hash="cfg-1", expected_keys=["k1", "k2"],
        row_counts={"rows": 10}, output_hashes={f"{worker_id}.png": "a" * 64},
        domain="images",
    )
    kwargs.update(overrides)
    return ec.build_worker_completion_record(**kwargs)


class TestWorkerCompletionRecords:
    def test_build_and_validate_round_trip(self):
        manifest = _manifest()
        record = _complete_record(manifest)
        assert record["manifest_hash"] == manifest["manifest_hash"]
        assert record["record_hash"]
        valid, err = ec.validate_worker_completion_record(record)
        assert valid is True
        assert err is None

    def test_invalid_status_rejected(self):
        manifest = _manifest()
        with pytest.raises(ec.ExecutionManifestError):
            ec.build_worker_completion_record(manifest=manifest, worker_id="w1", status="done")

    def test_invalid_output_hash_rejected(self):
        manifest = _manifest()
        with pytest.raises(ec.ExecutionManifestError):
            ec.build_worker_completion_record(
                manifest=manifest, worker_id="w1", status=ec.WORKER_STATUS_COMPLETE,
                output_hashes={"a.png": "not-a-hash"},
            )

    def test_structural_validation_rejects_malformed_record(self):
        valid, err = ec.validate_worker_completion_record({"worker_id": "w1"})
        assert valid is False
        assert "missing required field" in err

        valid2, err2 = ec.validate_worker_completion_record("not-a-dict")
        assert valid2 is False
        assert err2


class TestSkipExisting:
    def test_matching_hashes_allows_skip(self):
        manifest = _manifest()
        record = _complete_record(manifest)
        ok, reason = ec.check_skip_existing_worker(
            manifest, record, current_input_hash="in-1", current_config_hash="cfg-1",
        )
        assert ok is True
        assert "safe to skip" in reason

    def test_stale_manifest_hash_refuses_skip(self):
        """Hash mismatch: an existing record was built against a DIFFERENT
        manifest (e.g. config changed since the worker last ran) — skip is
        refused even though the worker_id/status otherwise look fine."""
        manifest_old = ec.build_execution_manifest(**_base_manifest_kwargs(config_fingerprint={"solver": "sgd"}))
        manifest_new = ec.build_execution_manifest(**_base_manifest_kwargs(config_fingerprint={"solver": "adam"}))
        stale_record = _complete_record(manifest_old)
        ok, reason = ec.check_skip_existing_worker(
            manifest_new, stale_record, current_input_hash="in-1", current_config_hash="cfg-1",
        )
        assert ok is False
        assert "manifest_hash" in reason

    def test_stale_input_hash_refuses_skip(self):
        manifest = _manifest()
        record = _complete_record(manifest, input_hash="in-1")
        ok, reason = ec.check_skip_existing_worker(
            manifest, record, current_input_hash="in-DIFFERENT", current_config_hash="cfg-1",
        )
        assert ok is False
        assert "input_hash" in reason

    def test_stale_config_hash_refuses_skip(self):
        manifest = _manifest()
        record = _complete_record(manifest, config_hash="cfg-1")
        ok, reason = ec.check_skip_existing_worker(
            manifest, record, current_input_hash="in-1", current_config_hash="cfg-DIFFERENT",
        )
        assert ok is False
        assert "config_hash" in reason

    def test_non_complete_status_refuses_skip(self):
        manifest = _manifest()
        record = _complete_record(manifest, status=ec.WORKER_STATUS_FAILED, output_hashes={}, row_counts={})
        ok, reason = ec.check_skip_existing_worker(
            manifest, record, current_input_hash="in-1", current_config_hash="cfg-1",
        )
        assert ok is False
        assert "complete" in reason

    def test_no_existing_record_refuses_skip(self):
        manifest = _manifest()
        ok, reason = ec.check_skip_existing_worker(manifest, None)
        assert ok is False
        assert "nothing to skip" in reason

    def test_structurally_invalid_existing_record_refuses_skip(self):
        manifest = _manifest()
        ok, reason = ec.check_skip_existing_worker(manifest, {"worker_id": "w1"})
        assert ok is False
        assert "structural validation" in reason


# ---------------------------------------------------------------------------
# Fail-closed aggregation.
# ---------------------------------------------------------------------------

class TestAggregateWorkerCompletions:
    def test_full_production_when_every_worker_completes_cleanly(self):
        manifest = _manifest()
        records = [_complete_record(manifest, worker_id=f"w{i}") for i in range(1, 4)]
        agg = ec.aggregate_worker_completions(
            manifest, records, expected_worker_ids=["w1", "w2", "w3"],
        )
        assert agg["ok"] is True
        assert agg["status"] == ec.AGGREGATION_STATUS_COMPLETE
        assert agg["is_full_production"] is True
        assert agg["expected_count"] == 3
        assert agg["observed_count"] == 3
        assert agg["first_failing_worker"] is None
        assert agg["missing_workers"] == []

    def test_idempotent_rerun_same_records_same_verdict(self):
        """Aggregating the identical record set twice yields a byte-for-byte
        identical verdict (minus nothing wall-clock — this aggregation
        carries no timestamp at all) — a re-run over unchanged state must
        never flip status."""
        manifest = _manifest()
        records = [_complete_record(manifest, worker_id=f"w{i}") for i in range(1, 4)]
        agg1 = ec.aggregate_worker_completions(manifest, records, expected_worker_ids=["w1", "w2", "w3"])
        agg2 = ec.aggregate_worker_completions(manifest, records, expected_worker_ids=["w1", "w2", "w3"])
        assert agg1 == agg2

    def test_missing_worker_fails_closed(self):
        manifest = _manifest()
        records = [_complete_record(manifest, worker_id="w1")]
        agg = ec.aggregate_worker_completions(manifest, records, expected_worker_ids=["w1", "w2"])
        assert agg["ok"] is False
        assert agg["status"] == ec.AGGREGATION_STATUS_FAILED
        assert agg["missing_workers"] == ["w2"]
        assert agg["first_failing_worker"] == "w2"

    def test_empty_expected_worker_ids_never_vacuously_complete(self):
        manifest = _manifest()
        records = [_complete_record(manifest, worker_id="w1")]
        agg = ec.aggregate_worker_completions(manifest, records, expected_worker_ids=[])
        assert agg["ok"] is False
        assert agg["status"] == ec.AGGREGATION_STATUS_FAILED

    def test_duplicate_worker_submission_fails_closed(self):
        manifest = _manifest()
        records = [_complete_record(manifest, worker_id="w1"), _complete_record(manifest, worker_id="w1")]
        agg = ec.aggregate_worker_completions(manifest, records, expected_worker_ids=["w1"])
        assert agg["ok"] is False
        assert agg["duplicate_workers"] == ["w1"]
        assert agg["first_failing_worker"] == "w1"

    def test_empty_output_on_complete_status_fails_closed(self):
        manifest = _manifest()
        empty_record = ec.build_worker_completion_record(
            manifest=manifest, worker_id="w1", status=ec.WORKER_STATUS_COMPLETE,
            output_hashes={}, row_counts={}, domain="images",
        )
        agg = ec.aggregate_worker_completions(manifest, [empty_record], expected_worker_ids=["w1"])
        assert agg["ok"] is False
        assert agg["empty_output_workers"] == ["w1"]

    def test_hash_mismatched_worker_fails_closed(self):
        manifest_old = ec.build_execution_manifest(**_base_manifest_kwargs(config_fingerprint={"solver": "sgd"}))
        manifest_new = ec.build_execution_manifest(**_base_manifest_kwargs(config_fingerprint={"solver": "adam"}))
        stale_record = _complete_record(manifest_old, worker_id="w1")
        agg = ec.aggregate_worker_completions(manifest_new, [stale_record], expected_worker_ids=["w1"])
        assert agg["ok"] is False
        assert agg["hash_mismatched_workers"] == ["w1"]
        assert agg["first_failing_worker"] == "w1"

    def test_schema_confusion_domain_not_declared_fails_closed(self):
        manifest = _manifest()
        confused_record = _complete_record(manifest, worker_id="w1", domain="not_a_real_domain")
        agg = ec.aggregate_worker_completions(manifest, [confused_record], expected_worker_ids=["w1"])
        assert agg["ok"] is False
        assert agg["schema_mismatched_workers"] == ["w1"]

    def test_raw_and_dse_domains_rolled_up_separately(self):
        manifest = _manifest()
        raw_rec = _complete_record(manifest, worker_id="w1", domain="images", row_counts={"rows": 4})
        dse_rec = ec.build_worker_completion_record(
            manifest=manifest, worker_id="w2", status=ec.WORKER_STATUS_COMPLETE,
            output_hashes={"w2.json": "b" * 64}, row_counts={"branches": 7},
            domain="branch_metrics",
        )
        agg = ec.aggregate_worker_completions(manifest, [raw_rec, dse_rec], expected_worker_ids=["w1", "w2"])
        assert agg["ok"] is True
        assert agg["domains"]["images"]["kind"] == "raw"
        assert agg["domains"]["images"]["complete_workers"] == 1
        assert agg["domains"]["images"]["rows"] == 4
        assert agg["domains"]["branch_metrics"]["kind"] == "dse"
        assert agg["domains"]["branch_metrics"]["complete_workers"] == 1
        assert agg["domains"]["branch_metrics"]["rows"] == 7

    def test_partial_without_allow_partial_fails_closed(self):
        manifest = _manifest()  # allow_partial defaults to False
        partial_record = _complete_record(manifest, worker_id="w1", status=ec.WORKER_STATUS_PARTIAL)
        agg = ec.aggregate_worker_completions(manifest, [partial_record], expected_worker_ids=["w1"])
        assert agg["ok"] is False
        assert agg["status"] == ec.AGGREGATION_STATUS_FAILED
        assert agg["partial_workers"] == ["w1"]

    def test_partial_run_with_allow_partial_is_a_valid_failure_stage_subset(self):
        """Distinguishes full production data from a valid failure-stage
        subset: allow_partial=True + a genuinely-partial run with NO
        integrity violations succeeds, but explicitly NOT as full
        production."""
        manifest = ec.build_execution_manifest(**_base_manifest_kwargs(allow_partial=True))
        complete_record = _complete_record(manifest, worker_id="w1")
        agg = ec.aggregate_worker_completions(
            manifest, [complete_record], expected_worker_ids=["w1", "w2"],
        )
        assert agg["ok"] is True
        assert agg["status"] == ec.AGGREGATION_STATUS_PARTIAL
        assert agg["is_full_production"] is False
        assert agg["missing_workers"] == ["w2"]

    def test_structurally_invalid_record_fails_closed(self):
        manifest = _manifest()
        agg = ec.aggregate_worker_completions(
            manifest, [{"worker_id": "w1"}], expected_worker_ids=["w1"],
        )
        assert agg["ok"] is False
        assert agg["structurally_invalid_records"]
        assert agg["first_failing_worker"] == "w1"

    def test_deterministic_diagnostics_first_failing_worker_is_lexicographic(self):
        manifest = _manifest()
        records = [_complete_record(manifest, worker_id="w3")]
        agg = ec.aggregate_worker_completions(
            manifest, records, expected_worker_ids=["w1", "w2", "w3"],
        )
        # w1 and w2 are both missing — the deterministic pick is the
        # lexicographically-first one, stable across repeated calls.
        assert agg["first_failing_worker"] == "w1"
        agg_again = ec.aggregate_worker_completions(
            manifest, records, expected_worker_ids=["w1", "w2", "w3"],
        )
        assert agg_again["first_failing_worker"] == "w1"

    def test_summarize_execution_manifest_aggregation_is_compact(self):
        manifest = _manifest()
        records = [_complete_record(manifest, worker_id="w1")]
        agg = ec.aggregate_worker_completions(manifest, records, expected_worker_ids=["w1"])
        summary = ec.summarize_execution_manifest_aggregation(agg)
        assert summary["ok"] is True
        assert summary["manifest_hash"] == manifest["manifest_hash"]
        assert "worker_records" not in summary
