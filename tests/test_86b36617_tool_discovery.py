"""Tests for sprint item 86b36617 — compile per-item tool_requirements into
executor discovery and enforce pre-edit code-intelligence receipts.

Covers meridian.tool_discovery (new module) plus its two integration points:

1. compile_discovery_request — the COMPILER: tool_requirements -> an actual
   ToolSearch-style discovery request (select:/keyword queries, batched per
   server_or_namespace).
2. classify_requirement_state / build_tool_discovery_state — required/
   preferred availability tracking + fallback telemetry, with EXPLICIT
   degraded/fail-closed states (never silently "proceed").
3. verify_pre_edit_receipt — the pre-edit semantic-search RECEIPT gate,
   distinct from meridian.code_intel_receipt's completion-time,
   capability-manifest-opt-in gate. Proves the "exposed-but-unused" failure
   mode (9c8336c4) is REJECTED, not silently accepted.
4. run_targeted_tests — exit-code-SAFE targeted-test orchestration: the real
   process exit code is propagated, never masked by an intermediate pipe
   stage.
5. Integration: meridian.executor_contract.build_executor_contract embeds a
   `tool_discovery` field (schema_version bumped 1 -> 2); meridian.handoff's
   build_item_briefing renders a `<tool_discovery_request>` clause compiled
   from the SAME tool_requirements the existing `<tool_requirements>` clause
   already renders.

On a52216e2 / cf2f5db8 (this item's own acceptance criteria cite these as
"reproductions [that] become visible, deterministic failures"): both are
real, already-completed, UNRELATED prior sprint items (a52216e2 —
meridian-outputs IndexFileLock lease/lock work; cf2f5db8 — DOCX
mc:Ignorable/mc:MustUnderstand namespace-prefix write-safety gate). Neither
item's own history (see their task-log entries) records anything about
whether codebase-memory/Serena prospecting was used before editing, so there
is no literal historical transcript to replay as a fixture here — fabricating
one would misrepresent what actually happened in those sessions. What IS
verifiable and is tested below (test_repro_a52216e2_* / test_repro_cf2f5db8_*)
is the GENERIC failure shape this item's gate exists to catch, replayed in
each of those two items' own domains (outputs/lease, docx/namespace-write):
an item whose tool_requirements REQUIRE a codebase-memory/Serena tool, with
no receipt on file, is rejected by verify_pre_edit_receipt — a check that
did not exist anywhere in the codebase before this item.
"""
from __future__ import annotations

import ast
import asyncio
import json as _json
import sys

import pytest

from meridian import code_intel_receipt as cir
from meridian import db as db_module
from meridian import executor_contract as ec
from meridian import handoff as handoff_module
from meridian import tool_discovery as td


def _req(**overrides):
    base = {
        "name": "find_symbol",
        "server_or_namespace": "Serena",
        "required_or_preferred": "required",
        "purpose": "locate the target symbol before editing",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# 1. compile_discovery_request — the compiler.
# ---------------------------------------------------------------------------

def test_compile_discovery_request_produces_select_and_keyword_queries():
    item = {"id": "item-1", "tool_requirements": [_req()]}
    request = td.compile_discovery_request(item)
    assert request["schema_version"] == td.TOOL_DISCOVERY_SCHEMA_VERSION
    assert request["item_id"] == "item-1"
    assert len(request["requested"]) == 1
    entry = request["requested"][0]
    assert entry["name"] == "find_symbol"
    assert entry["server_or_namespace"] == "Serena"
    assert entry["query"] == "select:find_symbol"
    assert entry["keyword_query"] == "Serena find_symbol"
    assert entry["required_or_preferred"] == "required"


def test_compile_discovery_request_batches_by_server():
    item = {
        "id": "item-2",
        "tool_requirements": [
            _req(name="find_symbol"),
            _req(name="find_referencing_symbols"),
            _req(name="search_graph", server_or_namespace="codebase-memory-mcp"),
        ],
    }
    request = td.compile_discovery_request(item)
    batches = {b["server_or_namespace"]: b for b in request["batched_queries"]}
    assert set(batches["Serena"]["names"]) == {"find_symbol", "find_referencing_symbols"}
    assert batches["Serena"]["query"] == "select:find_symbol,find_referencing_symbols"
    assert batches["codebase-memory-mcp"]["query"] == "select:search_graph"


def test_compile_discovery_request_empty_for_item_with_no_requirements():
    request = td.compile_discovery_request({"id": "item-3"})
    assert request["requested"] == []
    assert request["batched_queries"] == []


def test_compile_discovery_request_uses_legacy_required_tool_bridge():
    item = {"id": "item-4", "required_tool": "Serena: find_symbol"}
    request = td.compile_discovery_request(item)
    assert len(request["requested"]) == 1
    assert request["requested"][0]["name"] == "find_symbol"
    assert request["requested"][0]["server_or_namespace"] == "Serena"


def test_compile_discovery_request_structured_field_wins_over_legacy():
    item = {
        "id": "item-5",
        "required_tool": "Serena: find_symbol",
        "tool_requirements": [_req(name="search_graph", server_or_namespace="codebase-memory-mcp")],
    }
    request = td.compile_discovery_request(item)
    names = {e["name"] for e in request["requested"]}
    assert names == {"search_graph"}


def test_compile_discovery_request_rejects_non_dict_item():
    with pytest.raises(TypeError):
        td.compile_discovery_request("not-a-dict")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 2. classify_requirement_state — explicit degraded/fail-closed states.
# ---------------------------------------------------------------------------

def test_classify_available_is_ok():
    result = td.classify_requirement_state(_req(), {"status": "available", "fallback_used": None})
    assert result["state"] == "ok"


def test_classify_required_missing_no_fallback_is_fail_closed():
    result = td.classify_requirement_state(_req(), {"status": "missing", "fallback_used": None})
    assert result["state"] == "fail_closed"


def test_classify_preferred_missing_is_soft_unavailable_never_blocking():
    result = td.classify_requirement_state(
        _req(required_or_preferred="preferred"), {"status": "missing", "fallback_used": None},
    )
    assert result["state"] == "soft_unavailable"


def test_classify_degraded_with_rescue_is_explicit_degraded_fallback():
    avail = {
        "status": "degraded",
        "fallback_used": {
            "fallback_tool": "codebase-memory-mcp__search_graph",
            "failed_tool": "Serena__find_symbol",
        },
    }
    result = td.classify_requirement_state(
        _req(fallback=["codebase-memory-mcp: search_graph"]), avail,
    )
    assert result["state"] == "degraded_fallback"
    assert result["fallback_used"] == "codebase-memory-mcp__search_graph"


def test_classify_required_unknown_with_no_fallback_is_fail_closed():
    """hard_block risk class (required + no declared fallback): an 'unknown'
    (can't-confirm) status still fails closed — there is nothing to fall
    back to if the primary tool turns out to be genuinely absent."""
    result = td.classify_requirement_state(_req(), {"status": "unknown", "fallback_used": None})
    assert result["state"] == "fail_closed"


def test_classify_required_unknown_with_declared_fallback_is_soft():
    """has_fallback risk class: an 'unknown' primary with a documented
    fallback chain is NOT forced into fail_closed -- mirrors
    executor_contract's own 'unknown is never a hard block' convention."""
    result = td.classify_requirement_state(
        _req(fallback=["codebase-memory-mcp: search_graph"]),
        {"status": "unknown", "fallback_used": None},
    )
    assert result["state"] == "soft_unavailable"


def test_classify_missing_availability_entry_degrades_to_unknown_shape():
    result = td.classify_requirement_state(_req(), None)
    assert result["state"] == "fail_closed"
    assert result["status"] == "unknown"


# ---------------------------------------------------------------------------
# 3. verify_pre_edit_receipt — the receipt gate.
# ---------------------------------------------------------------------------

async def test_receipt_gate_not_applicable_with_no_tool_requirements(db):
    project = await db_module.create_project(db, "td-gate-none")
    item = await db_module.add_sprint_item(db, project["id"], "v1", "Plain item")
    check = await td.verify_pre_edit_receipt(db, project["id"], item)
    assert check["applicable"] is False
    assert check["ok"] is True


async def test_receipt_gate_not_applicable_when_code_intel_tool_only_preferred(db):
    project = await db_module.create_project(db, "td-gate-preferred")
    item = await db_module.add_sprint_item(
        db, project["id"], "v1", "Nice to have prospecting",
        tool_requirements=[_req(required_or_preferred="preferred")],
    )
    check = await td.verify_pre_edit_receipt(db, project["id"], item)
    assert check["applicable"] is False


async def test_receipt_gate_not_applicable_for_non_code_intel_required_tool(db):
    """A REQUIRED tool that isn't a codebase-memory/Serena prospecting tool
    (e.g. a docx tool) never triggers this gate -- it's scoped specifically
    to search_graph/get_code_snippet/find_symbol/find_referencing_symbols
    (and the rest of code_intel_receipt.CODE_INTEL_RECEIPT_TOOLS)."""
    project = await db_module.create_project(db, "td-gate-non-code-intel")
    item = await db_module.add_sprint_item(
        db, project["id"], "v1", "Insert a citation",
        tool_requirements=[_req(
            name="insert_citation", server_or_namespace="meridian-docs",
            purpose="add a bibliography entry",
        )],
    )
    check = await td.verify_pre_edit_receipt(db, project["id"], item)
    assert check["applicable"] is False


async def test_receipt_gate_rejects_when_required_and_no_receipt_on_file(db):
    """THE core gate behavior: exposed (compiled into the discovery request,
    would resolve via ToolSearch) but never actually called -> rejected, not
    silently accepted."""
    project = await db_module.create_project(db, "td-gate-missing")
    item = await db_module.add_sprint_item(
        db, project["id"], "v1", "Refactor the auth module",
        tool_requirements=[_req()],
    )
    claimed = await db_module.claim_sprint_item(db, project["id"], item["id"])
    check = await td.verify_pre_edit_receipt(db, project["id"], claimed)
    assert check["applicable"] is True
    assert check["ok"] is False
    assert check["code"] == "TOOL_DISCOVERY_RECEIPT_MISSING"
    assert check["exposed"] is True
    assert check["actually_called"] is False
    assert check["receipt"] is None


async def test_receipt_gate_passes_once_a_genuine_receipt_is_recorded(db):
    project = await db_module.create_project(db, "td-gate-present")
    item = await db_module.add_sprint_item(
        db, project["id"], "v1", "Refactor the billing module",
        tool_requirements=[_req()],
    )
    claimed = await db_module.claim_sprint_item(db, project["id"], item["id"])

    # Structural receipt write -- mirrors what the server's own tool-dispatch
    # code does the instant a real search_graph/find_symbol call lands (see
    # code_intel_receipt.py's module docstring): never a self-report.
    await cir.record_prospect_receipt(
        db, tenant_id=None, project_id=project["id"], session_id="exec-session-1",
        tool_name="find_symbol", query="AuthRouter",
    )

    check = await td.verify_pre_edit_receipt(db, project["id"], claimed)
    assert check["applicable"] is True
    assert check["ok"] is True
    assert check["exposed"] is True
    assert check["actually_called"] is True
    assert check["receipt"] is not None


async def test_receipt_gate_9c8336c4_exposed_but_unused_is_rejected_even_when_available(db):
    """9c8336c4: a tool that was made available via ToolSearch (resolves as
    'available' in the availability classification) but never actually
    invoked must still be REJECTED by the receipt gate -- availability is
    not a substitute for a receipt."""
    project = await db_module.create_project(db, "td-gate-9c8336c4")
    item = await db_module.add_sprint_item(
        db, project["id"], "v1", "Rename a widely-used symbol",
        tool_requirements=[_req()],
    )
    claimed = await db_module.claim_sprint_item(db, project["id"], item["id"])

    requirements = [_req()]
    # Simulate: ToolSearch successfully exposed the tool (it resolves as
    # fully "available") -- but the executor never actually called it.
    availability_by_key = {("Serena", "find_symbol"): {"status": "available", "fallback_used": None}}

    state = await td.build_tool_discovery_state(
        db, project["id"], claimed, availability_by_key=availability_by_key,
    )
    assert state["selected"][0]["state"] == "ok"  # exposed, resolvable
    assert state["receipt"]["exposed"] is True
    assert state["receipt"]["actually_called"] is False
    assert state["receipt"]["ok"] is False  # ... but still rejected
    assert state["executable"] is False
    assert "TOOL_DISCOVERY_RECEIPT_MISSING" in state["executable_reasons"]


async def test_receipt_gate_ignores_stale_receipt_from_before_this_claim(db):
    """A receipt recorded BEFORE this item was (re-)claimed does not count
    as evidence for the CURRENT claim -- mirrors code_intel_receipt.py's own
    freshness discipline exactly (same `since` semantics)."""
    project = await db_module.create_project(db, "td-gate-stale")
    item = await db_module.add_sprint_item(
        db, project["id"], "v1", "Touch the payments module",
        tool_requirements=[_req()],
    )
    # A receipt from a stale, earlier pass -- recorded before this claim.
    await cir.record_prospect_receipt(
        db, tenant_id=None, project_id=project["id"], session_id="stale-session",
        tool_name="find_symbol", query="old query",
    )
    claimed = await db_module.claim_sprint_item(db, project["id"], item["id"])
    # Force `since` far in the future relative to the stale receipt so this
    # test is deterministic regardless of real wall-clock timing precision.
    check = await td.verify_pre_edit_receipt(
        db, project["id"], claimed, since="2099-01-01 00:00:00",
    )
    assert check["applicable"] is True
    assert check["ok"] is False
    assert check["actually_called"] is False


# ---------------------------------------------------------------------------
# a52216e2 / cf2f5db8 — see module docstring for why these are GENERIC
# domain replays rather than literal historical transcripts.
# ---------------------------------------------------------------------------

async def test_repro_a52216e2_outputs_lease_domain_receiptless_completion_rejected(db):
    """a52216e2-style item (meridian-outputs IndexFileLock lease/lock work):
    a REQUIRED find_symbol prospecting requirement with NO receipt on file
    is a visible, deterministic failure under this item's gate -- before
    86b36617, nothing in the codebase checked this at all in this domain."""
    project = await db_module.create_project(db, "repro-a52216e2")
    item = await db_module.add_sprint_item(
        db, project["id"], "v1",
        "Implement process-aware single-writer lease/lock for the "
        "meridian-outputs local index (IndexFileLock)",
        tool_requirements=[_req(purpose="locate existing IndexFileLock call sites")],
    )
    claimed = await db_module.claim_sprint_item(db, project["id"], item["id"])
    check = await td.verify_pre_edit_receipt(db, project["id"], claimed)
    assert check["applicable"] is True
    assert check["ok"] is False
    assert check["code"] == "TOOL_DISCOVERY_RECEIPT_MISSING"


async def test_repro_cf2f5db8_docx_namespace_domain_receiptless_completion_rejected(db):
    """cf2f5db8-style item (DOCX mc:Ignorable/mc:MustUnderstand
    namespace-prefix write-safety gate): same generic failure shape, in an
    entirely different (docx write-integrity) domain -- proving the gate is
    domain-agnostic, not something wired only for code-intel-flavored work."""
    project = await db_module.create_project(db, "repro-cf2f5db8")
    item = await db_module.add_sprint_item(
        db, project["id"], "v1",
        "Add a fail-closed mc:Ignorable/mc:MustUnderstand namespace-prefix "
        "fidelity gate to docs_intel.py's _atomic_write_docx_bytes",
        tool_requirements=[_req(purpose="locate _atomic_write_docx_bytes call sites")],
    )
    claimed = await db_module.claim_sprint_item(db, project["id"], item["id"])
    check = await td.verify_pre_edit_receipt(db, project["id"], claimed)
    assert check["applicable"] is True
    assert check["ok"] is False
    assert check["code"] == "TOOL_DISCOVERY_RECEIPT_MISSING"

    # And the positive control: once a genuine receipt IS recorded, the same
    # domain-agnostic gate passes -- proving this isn't a fixed "always
    # reject" stub, only "reject an actually-missing receipt".
    await cir.record_prospect_receipt(
        db, tenant_id=None, project_id=project["id"], session_id="exec-session-2",
        tool_name="find_symbol", query="_atomic_write_docx_bytes",
    )
    check2 = await td.verify_pre_edit_receipt(db, project["id"], claimed)
    assert check2["ok"] is True
    assert check2["actually_called"] is True


# ---------------------------------------------------------------------------
# 4. build_tool_discovery_state — composition + stable field names.
# ---------------------------------------------------------------------------

async def test_discovery_state_has_all_six_stable_fields(db):
    project = await db_module.create_project(db, "td-state-shape")
    item = await db_module.add_sprint_item(
        db, project["id"], "v1", "Locate and fix a bug",
        tool_requirements=[_req()],
    )
    state = await td.build_tool_discovery_state(db, project["id"], item)
    for key in ("requested", "selected", "first_call", "availability", "fallback", "receipt"):
        assert key in state, key
    assert state["schema_version"] == td.TOOL_DISCOVERY_SCHEMA_VERSION
    assert state["item_id"] == item["id"]


async def test_discovery_state_selected_reflects_fallback_rescue(db):
    project = await db_module.create_project(db, "td-state-fallback")
    item = await db_module.add_sprint_item(
        db, project["id"], "v1", "Trace a call graph",
        tool_requirements=[_req(fallback=["codebase-memory-mcp: search_graph"])],
    )
    availability_by_key = {
        ("Serena", "find_symbol"): {
            "status": "degraded",
            "fallback_used": {
                "fallback_tool": "codebase-memory-mcp__search_graph",
                "failed_tool": "Serena__find_symbol",
            },
        },
    }
    state = await td.build_tool_discovery_state(
        db, project["id"], item, availability_by_key=availability_by_key,
    )
    assert state["selected"][0]["selected_tool"] == "codebase-memory-mcp__search_graph"
    assert state["selected"][0]["source"] == "fallback"
    assert state["selected"][0]["state"] == "degraded_fallback"

    fb = state["fallback"][0]
    assert fb["declared"] == ["codebase-memory-mcp: search_graph"]
    assert fb["used"] == "codebase-memory-mcp__search_graph"
    assert fb["rescued"] is True

    assert "Serena: find_symbol" in state["availability"]["degraded"]


async def test_discovery_state_fail_closed_is_explicit_not_silent(db):
    project = await db_module.create_project(db, "td-state-fail-closed")
    item = await db_module.add_sprint_item(
        db, project["id"], "v1", "Do something requiring an absent tool",
        tool_requirements=[_req(name="totally_missing_tool")],
    )
    availability_by_key = {("Serena", "totally_missing_tool"): {"status": "missing", "fallback_used": None}}
    state = await td.build_tool_discovery_state(
        db, project["id"], item, availability_by_key=availability_by_key,
    )
    assert state["executable"] is False
    assert any("fail_closed_tools" in r for r in state["executable_reasons"])
    fail_closed_entries = [e for e in state["degraded_or_fail_closed"] if e["state"] == "fail_closed"]
    assert len(fail_closed_entries) == 1
    assert fail_closed_entries[0]["name"] == "totally_missing_tool"
    assert "Serena: totally_missing_tool" in state["availability"]["missing"]


async def test_discovery_state_first_call_populated_from_receipt(db):
    project = await db_module.create_project(db, "td-state-first-call")
    item = await db_module.add_sprint_item(
        db, project["id"], "v1", "Prospect before editing",
        tool_requirements=[_req()],
    )
    claimed = await db_module.claim_sprint_item(db, project["id"], item["id"])
    await cir.record_prospect_receipt(
        db, tenant_id=None, project_id=project["id"], session_id="exec-session-3",
        tool_name="find_symbol", query="PaymentProcessor",
    )
    state = await td.build_tool_discovery_state(db, project["id"], claimed)
    assert state["first_call"] is not None
    assert state["first_call"]["tool"] == "find_symbol"
    assert state["first_call"]["at"] is not None


async def test_discovery_state_deterministic_for_identical_state(db):
    """Building the state twice for identical underlying data (no receipt,
    no live inventory change in between) must be byte-identical when
    serialized -- same discipline as executor_contract's own hash-stability
    guarantee."""
    project = await db_module.create_project(db, "td-state-deterministic")
    item = await db_module.add_sprint_item(
        db, project["id"], "v1", "Stable state check",
        tool_requirements=[_req()],
    )
    availability_by_key = {("Serena", "find_symbol"): {"status": "unknown", "fallback_used": None}}
    state_a = await td.build_tool_discovery_state(
        db, project["id"], item, availability_by_key=availability_by_key,
    )
    state_b = await td.build_tool_discovery_state(
        db, project["id"], item, availability_by_key=availability_by_key,
    )
    assert _json.dumps(state_a, sort_keys=True) == _json.dumps(state_b, sort_keys=True)


# ---------------------------------------------------------------------------
# 5. Integration: executor_contract.build_executor_contract.
# ---------------------------------------------------------------------------

async def test_executor_contract_embeds_tool_discovery_field(db):
    project = await db_module.create_project(db, "ec-td-embed")
    item = await db_module.add_sprint_item(
        db, project["id"], "v1", "Item with prospecting requirement",
        tool_requirements=[_req()],
    )
    fresh = await db_module.get_sprint_item(db, item["id"])
    contract = await ec.build_executor_contract(db, project["id"], fresh)
    assert "tool_discovery" in contract
    assert contract["schema_version"] == 2
    assert contract["tool_discovery"]["requested"][0]["name"] == "find_symbol"
    # The pre-existing top-level executable/executable_reasons stay
    # UNCHANGED by the new discovery-side receipt gate -- that gate is
    # surfaced separately (see executor_contract.py's inline comment) so
    # this item's discovery/compilation work never silently changes
    # claim/complete-time blocking behavior owned by code_intel_receipt.py.
    assert contract["executable"] is True


async def test_executor_contract_tool_discovery_reuses_same_availability_as_allowed_tools(db):
    """The SAME availability_by_key feeds both allowed_tools/forbidden_tools
    AND tool_discovery -- they can never disagree about a given tool's
    status."""
    project = await db_module.create_project(db, "ec-td-shared-availability")
    item = await db_module.add_sprint_item(
        db, project["id"], "v1", "Missing tool item",
        tool_requirements=[_req(name="definitely_not_a_real_tool")],
    )
    fresh = await db_module.get_sprint_item(db, item["id"])

    def _checker(requirements):
        return {
            ("Serena", "definitely_not_a_real_tool"): {"status": "missing", "fallback_used": None},
        }

    contract = await ec.build_executor_contract(db, project["id"], fresh, tool_availability_checker=_checker)
    assert contract["forbidden_tools"][0]["name"] == "definitely_not_a_real_tool"
    assert contract["tool_discovery"]["availability"]["missing"] == ["Serena: definitely_not_a_real_tool"]


async def test_executor_contract_hash_stable_with_tool_discovery_present(db):
    project = await db_module.create_project(db, "ec-td-hash-stable")
    item = await db_module.add_sprint_item(
        db, project["id"], "v1", "Stable with discovery",
        tool_requirements=[_req()],
    )
    fresh = await db_module.get_sprint_item(db, item["id"])
    contract_a = await ec.build_executor_contract(db, project["id"], fresh)
    contract_b = await ec.build_executor_contract(db, project["id"], fresh)
    assert ec.serialize_executor_contract(contract_a) == ec.serialize_executor_contract(contract_b)
    assert contract_a["contract_hash"] == contract_b["contract_hash"]


# ---------------------------------------------------------------------------
# 5b. Integration: handoff.build_item_briefing.
# ---------------------------------------------------------------------------

def _extract_clause(briefing: str, tag: str):
    open_tag, close_tag = f"<{tag}>", f"</{tag}>"
    if open_tag not in briefing:
        return None
    start = briefing.index(open_tag) + len(open_tag)
    end = briefing.index(close_tag)
    return _json.loads(briefing[start:end])


def test_build_item_briefing_renders_tool_discovery_request_clause():
    item = {
        "id": "item-briefing-1",
        "title": "Refactor auth",
        "tool_requirements": _json.dumps([_req()]),
    }
    briefing = handoff_module.build_item_briefing(item)
    embedded = _extract_clause(briefing, "tool_discovery_request")
    assert embedded is not None
    assert embedded["requested"][0]["query"] == "select:find_symbol"
    assert embedded["batched_queries"][0]["query"] == "select:find_symbol"


def test_build_item_briefing_omits_tool_discovery_request_when_nothing_declared():
    item = {"id": "item-briefing-2", "title": "Update the README"}
    briefing = handoff_module.build_item_briefing(item)
    assert "<tool_discovery_request>" not in briefing


# ---------------------------------------------------------------------------
# 6. run_targeted_tests — exit-code-safe orchestration.
# ---------------------------------------------------------------------------

@pytest.mark.subprocess_isolated
async def test_run_targeted_tests_propagates_real_nonzero_exit_code():
    """List-exec form: the REAL exit code of the target process, unmasked."""
    result = await td.run_targeted_tests(
        [sys.executable, "-c", "import sys; sys.exit(3)"],
    )
    assert result["status"] == "ok"
    assert result["exit_code"] == 3


@pytest.mark.subprocess_isolated
async def test_run_targeted_tests_propagates_real_zero_exit_code():
    result = await td.run_targeted_tests(
        [sys.executable, "-c", "import sys; sys.exit(0)"],
    )
    assert result["status"] == "ok"
    assert result["exit_code"] == 0


@pytest.mark.subprocess_isolated
async def test_run_targeted_tests_parses_pytest_style_pass_fail_counts():
    result = await td.run_targeted_tests([
        sys.executable, "-c",
        "print('5 passed, 2 failed in 1.23s')",
    ])
    assert result["exit_code"] == 0
    assert result["passed"] == 5
    assert result["failed"] == 2


async def test_run_targeted_tests_empty_cmd_is_a_clean_error():
    """No subprocess_isolated marker: `cmd` is empty, so `run_targeted_tests`
    short-circuits before ever spawning a process (see its `if not cmd:`
    guard in meridian/tool_discovery.py) -- nothing here is xdist-contention
    sensitive."""
    result = await td.run_targeted_tests([])
    assert result["status"] == "error"
    assert result["exit_code"] is None


@pytest.mark.subprocess_isolated
async def test_run_targeted_tests_timeout_kills_process_and_reports_status():
    result = await td.run_targeted_tests(
        [sys.executable, "-c", "import time; time.sleep(5)"],
        timeout=0.2,
    )
    assert result["status"] == "timeout"
    assert result["exit_code"] is None


@pytest.mark.subprocess_isolated
async def test_run_targeted_tests_shell_pipe_can_mask_exit_code_list_form_cannot():
    """Reproduces the EXACT root-cause class this acceptance criterion names
    ('not something masked by a pipe/tail'): a shell string that pipes the
    real command's output through a second stage reports the LAST stage's
    exit status, not the real target's. The list-exec form of the SAME
    underlying command does not have this problem -- see
    test_run_targeted_tests_propagates_real_nonzero_exit_code above.
    """
    import os

    py = sys.executable
    if sys.platform == "win32":
        # cmd.exe's `find` mirrors POSIX `tail`/`grep` for this purpose: its
        # own exit status (0 = found a match, 1 = did not) replaces the
        # piped command's real exit code as the shell's reported status.
        #
        # Use an UNAMBIGUOUS path to the real Windows find.exe rather than
        # a bare `find` -- on a dev machine with Git for Windows installed,
        # `C:\Program Files\Git\usr\bin\find.exe` (GNU findutils) resolves
        # ahead of `C:\Windows\System32\find.exe` on PATH. GNU find given
        # `/c ""` interprets `/c` as a search path (Git Bash's own path
        # translation maps it to the C: drive root) and silently launches a
        # full recursive scan of the entire drive instead of erroring or
        # counting lines -- reproduced directly: it does not deadlock, it
        # is a genuinely slow, unbounded, wrong operation that can run for
        # hours, which is why this test previously appeared to hang rather
        # than fail fast.
        _system_root = os.environ.get("SystemRoot", r"C:\Windows")
        _win_find = os.path.join(_system_root, "System32", "find.exe")
        shell_cmd = f'"{py}" -c "import sys; sys.exit(3)" | "{_win_find}" /c ""'
    else:
        shell_cmd = f'"{py}" -c "import sys; sys.exit(3)" | tail -n 5'

    masked = await td.run_targeted_tests(shell_cmd)
    unmasked = await td.run_targeted_tests([py, "-c", "import sys; sys.exit(3)"])

    assert unmasked["exit_code"] == 3
    # The masked (shell-pipe) form's exit code must NOT equal the real
    # target's exit code -- demonstrating exactly why the list-exec form is
    # the one this function documents as giving the safety guarantee.
    assert masked["exit_code"] != 3


# ---------------------------------------------------------------------------
# Regression: CI run 31289808800 -- Ruff F821 "undefined name `datetime`" at
# tool_discovery.py:909 and :965 (validate_discovery_override's and
# apply_discovery_override's `now: "datetime | None" = None` parameter).
# Both functions only ever bound `datetime` *locally*, inside their own
# bodies (`from datetime import datetime as _datetime, timezone as
# _timezone`), never at module scope -- so the quoted forward-ref annotation
# genuinely had nothing to resolve against for static analysis, even though
# `from __future__ import annotations` meant it never broke at runtime. Fix
# was a `if TYPE_CHECKING: from datetime import datetime` module-level
# import, which Ruff/mypy resolve annotation forward-refs against without
# adding a real runtime import. This test parses the module's own source so
# it fails on the original bug and needs no `ruff` install (not in the
# default pixi env; see tests/test_w5_736d300e_ruff_blocking.py).
# ---------------------------------------------------------------------------

def test_tool_discovery_datetime_annotation_resolves_at_module_scope():
    def _binds_datetime(node: ast.stmt) -> bool:
        if isinstance(node, ast.Import):
            return any(
                alias.name == "datetime" and (alias.asname or alias.name) == "datetime"
                for alias in node.names
            )
        if isinstance(node, ast.ImportFrom):
            return node.module == "datetime" and any(
                alias.name == "datetime" and (alias.asname or alias.name) == "datetime"
                for alias in node.names
            )
        return False

    def _is_type_checking_guard(test: ast.expr) -> bool:
        return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
            isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
        )

    with open(td.__file__, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=td.__file__)

    bound_at_module_scope = any(_binds_datetime(node) for node in tree.body)
    bound_under_type_checking = any(
        _binds_datetime(inner)
        for node in tree.body
        if isinstance(node, ast.If) and _is_type_checking_guard(node.test)
        for inner in node.body
    )

    assert bound_at_module_scope or bound_under_type_checking, (
        "meridian/tool_discovery.py's `now: \"datetime | None\"` forward-ref "
        "annotations (validate_discovery_override / apply_discovery_override) "
        "reference `datetime`, but nothing binds that name at module scope or "
        "inside an `if TYPE_CHECKING:` guard -- this is the exact CI run "
        "31289808800 Ruff F821 regression (undefined name 'datetime')."
    )

    # The two annotated call sites themselves still exist and still use the
    # bare `datetime` forward-ref -- guards against this test silently going
    # stale if the annotation is later rewritten to something else entirely.
    assert 'now: "datetime | None" = None' in open(td.__file__, encoding="utf-8").read()
