"""Tests for sprint item de4d4293 — deterministic per-item tool routing and
verification contracts in starter (and every other) handoff mode.

Confirmed gap this item closes: a generate_handoff(mode="starter") block
carried item ids and generic executor rules but NOTHING telling an executor
WHICH tool routes any given item — an item never gets a per-item contract
unless a planner happened to set an explicit tool_requirements/required_tool
pin. This file covers the fix:

* meridian.executor_contract — a NEW, compact, deterministic routing-hint
  extraction (infer_default_routing_category / build_routing_hint /
  build_routing_summary / routing_summary_hash), built on TOP of the
  existing canonical tool_requirements read (never a second, parallel
  per-item contract mechanism — see that module's own docstring).
* meridian.handoff._build_executor_routing_clause — the compact
  ``<executor_routing>`` /goal-text projection, wired into
  _build_quick_start_goal (the SAME function underlying every handoff
  mode), scoped to the CLAIMABLE batch only (never the full pending
  backlog) so it stays bounded.
* meridian.capability_contract.build_capability_contract — the structured
  JSON twin (``item_routing_summary`` / ``item_routing_summary_hash``), plus
  a hard cap + truncation note on the pre-existing, previously-unbounded
  ``item_executor_contracts`` field (23e20656) — the exact size regression
  this item's brief warned against repeating.
* An explicit regression guard: a realistic 10+ item paste-ready /goal
  block stays a few KB, not tens of KB.
"""
from __future__ import annotations

import re

import pytest

from meridian import capability_contract as cc
from meridian import db as db_module
from meridian import executor_contract as ec
from meridian import handoff as handoff_module
import meridian.server  # noqa: F401 — load the server before handler to avoid its import cycle
from meridian.mcp import handler as mcp_handler


_GOAL_TOKEN_RE = re.compile(r"<goal_token>[^<]*</goal_token>")


def _strip_goal_token(content: str) -> str:
    return _GOAL_TOKEN_RE.sub("<goal_token>STRIPPED</goal_token>", content)


def _extract_tag_body(content: str, tag: str) -> str:
    open_start = content.rindex(f"<{tag}")
    open_end = content.index(">", open_start) + 1
    end = content.index(f"</{tag}>", open_end)
    return content[open_end:end]


# ---------------------------------------------------------------------------
# executor_contract.infer_default_routing_category — built-in lookup table
# ---------------------------------------------------------------------------


def test_infer_default_routing_orchestration():
    hint = ec.infer_default_routing_category({"title": "Claim sprint items for wave 2"})
    assert hint["routing_category"] == "orchestration"
    assert hint["server_or_namespace"] == "meridian"
    assert hint["name"] == "claim_sprint_item"
    assert hint["required_or_preferred"] == "preferred"


def test_infer_default_routing_code_investigation():
    hint = ec.infer_default_routing_category({"title": "Investigate the parser regression"})
    assert hint["routing_category"] == "code_investigation"
    assert hint["server_or_namespace"] == "Serena"
    assert hint["name"] == "find_symbol"


def test_infer_default_routing_handoff():
    hint = ec.infer_default_routing_category({"title": "Fix generate_handoff mode parity"})
    assert hint["routing_category"] == "handoff"
    assert hint["name"] == "generate_handoff"


def test_infer_default_routing_tunnel_verification():
    hint = ec.infer_default_routing_category({"title": "Run tunnel verification before deploy gate"})
    assert hint["routing_category"] == "tunnel_verification"
    assert hint["name"] == "run_verification"


def test_infer_default_routing_docx():
    hint = ec.infer_default_routing_category({"title": "Fix a DOCX region claim bug"})
    assert hint["routing_category"] == "docx"
    assert hint["server_or_namespace"] == "meridian-docs"


def test_infer_default_routing_no_match_returns_none():
    assert ec.infer_default_routing_category({"title": "Bump a version number"}) is None
    assert ec.infer_default_routing_category({"title": "", "notes": ""}) is None
    assert ec.infer_default_routing_category({}) is None


def test_infer_default_routing_searches_notes_too():
    hint = ec.infer_default_routing_category(
        {"title": "Generic title", "notes": "Needs to claim sprint items first"}
    )
    assert hint["routing_category"] == "orchestration"


def test_infer_default_routing_first_match_wins_deterministically():
    """A title matching TWO categories' keywords always resolves to the
    table-order-first one, every time — never ambiguous / order-dependent."""
    item = {"title": "Claim sprint item, then investigate the fallout"}
    hint1 = ec.infer_default_routing_category(item)
    hint2 = ec.infer_default_routing_category(item)
    assert hint1 == hint2
    assert hint1["routing_category"] == "orchestration"  # orchestration sorts first in the table


# ---------------------------------------------------------------------------
# executor_contract.build_routing_hint — explicit tool_requirements always
# wins over the inferred default; both sourced through the SAME canonical
# read build_executor_contract itself uses (never a second derivation).
# ---------------------------------------------------------------------------


def test_build_routing_hint_prefers_explicit_tool_requirements():
    item = {
        "id": "item-1",
        "title": "Investigate the parser regression",  # would infer code_investigation
        "tool_requirements": [{
            "name": "run_verification", "server_or_namespace": "meridian",
            "required_or_preferred": "required", "purpose": "explicit override",
        }],
    }
    hint = ec.build_routing_hint(item)
    assert hint["source"] == "explicit"
    assert hint["name"] == "run_verification"
    assert hint["required_or_preferred"] == "required"


def test_build_routing_hint_falls_back_to_inferred():
    item = {"id": "item-2", "title": "Investigate the parser regression"}
    hint = ec.build_routing_hint(item)
    assert hint["source"] == "inferred"
    assert hint["name"] == "find_symbol"


def test_build_routing_hint_honours_legacy_required_tool_pin():
    item = {"id": "item-3", "title": "Anything at all", "required_tool": "Serena: replace_symbol_body"}
    hint = ec.build_routing_hint(item)
    assert hint["source"] == "explicit"
    assert hint["server_or_namespace"] == "Serena"
    assert hint["name"] == "replace_symbol_body"


def test_build_routing_hint_none_without_id():
    assert ec.build_routing_hint({"title": "Investigate the parser regression"}) is None


def test_build_routing_hint_none_when_nothing_resolves():
    assert ec.build_routing_hint({"id": "item-4", "title": "Bump a version number"}) is None


# ---------------------------------------------------------------------------
# executor_contract.build_routing_summary / routing_summary_hash
# ---------------------------------------------------------------------------


def test_build_routing_summary_sorted_and_filtered():
    items = [
        {"id": "zzz", "title": "Investigate the parser regression"},
        {"id": "aaa", "title": "Claim sprint items for wave 2"},
        {"id": "mmm", "title": "Bump a version number"},  # no hint -> excluded
        "not-a-dict",  # skipped, never raises
    ]
    summary = ec.build_routing_summary(items)
    assert [h["item_id"] for h in summary] == ["aaa", "zzz"]


def test_build_routing_summary_empty_for_no_items():
    assert ec.build_routing_summary([]) == []


def test_routing_summary_hash_stable_and_sensitive():
    items = [{"id": "a", "title": "Claim sprint items for wave 2"}]
    summary_a = ec.build_routing_summary(items)
    summary_b = ec.build_routing_summary(list(items))
    assert ec.routing_summary_hash(summary_a) == ec.routing_summary_hash(summary_b)

    changed = [{"id": "a", "title": "Investigate the parser regression"}]
    summary_c = ec.build_routing_summary(changed)
    assert ec.routing_summary_hash(summary_a) != ec.routing_summary_hash(summary_c)


# ---------------------------------------------------------------------------
# handoff._build_executor_routing_clause — pure /goal-text renderer
# ---------------------------------------------------------------------------


def test_build_executor_routing_clause_empty_for_no_items():
    assert handoff_module._build_executor_routing_clause([]) == ""


def test_build_executor_routing_clause_empty_when_nothing_resolves():
    items = [{"id": "x", "title": "Bump a version number"}]
    assert handoff_module._build_executor_routing_clause(items) == ""


def test_build_executor_routing_clause_embeds_hash_matching_independent_call():
    items = [
        {"id": "item-1", "title": "Investigate the parser regression"},
        {"id": "item-2", "title": "Claim sprint items for wave 2"},
    ]
    clause = handoff_module._build_executor_routing_clause(items)
    assert clause.startswith("\n<executor_routing hash=\"")
    assert clause.endswith("</executor_routing>")

    expected_summary = ec.build_routing_summary(items)
    expected_hash = ec.routing_summary_hash(expected_summary)
    m = re.search(r'hash="([0-9a-f]+)"', clause)
    assert m is not None
    assert m.group(1) == expected_hash
    for h in expected_summary:
        assert h["item_id"] in clause


def test_build_quick_start_goal_scopes_routing_clause_to_claimable_batch():
    """An item excluded from the claimable batch (backburner track) must NOT
    appear in <executor_routing>, even though it appears elsewhere in the
    /goal text via its own exclusion note."""
    items = [
        {"id": "claimable-1", "title": "Investigate the parser regression"},
        {
            "id": "backburner-1", "title": "Claim sprint items for wave 2",
            "track": "backburner",
        },
    ]
    out = handoff_module._build_quick_start_goal(items)
    routing_body = _extract_tag_body(out, "executor_routing")
    assert "claimable-1" in routing_body
    assert "backburner-1" not in routing_body


# ---------------------------------------------------------------------------
# End-to-end generate_handoff — the actual "confirmed handoff gap" fix.
# Criterion 1: starter/full/delta/goal all emit the SAME routing data for
# the same item (reusing executor_contract.py, never forking it).
# ---------------------------------------------------------------------------


async def test_starter_mode_carries_executor_routing_clause(db, tmp_path):
    p = await db_module.create_project(db, "starter-routing-gap")
    item = await db_module.add_sprint_item(
        db, p["id"], "v1", "Investigate the parser regression",
    )
    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="starter",
    )
    assert "<executor_routing" in content
    body = _extract_tag_body(content, "executor_routing")
    assert item["id"] in body
    assert "Serena: find_symbol" in body


@pytest.mark.parametrize("mode", ["starter", "goal", "full", "delta"])
async def test_executor_routing_clause_present_and_identical_across_modes(db, tmp_path, mode):
    p = await db_module.create_project(db, f"routing-mode-parity-{mode}")
    item = await db_module.add_sprint_item(
        db, p["id"], "v1", "Claim sprint items for wave 2",
    )
    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode=mode,
    )
    assert "<executor_routing" in content
    body = _extract_tag_body(content, "executor_routing")
    assert item["id"] in body
    assert "meridian: claim_sprint_item" in body


async def test_explicit_tool_requirements_surface_in_starter_routing_clause(db, tmp_path):
    """An item WITH an explicit tool_requirements pin routes to that tool,
    not the inferred default, even in starter mode's compact text."""
    p = await db_module.create_project(db, "starter-routing-explicit")
    item = await db_module.add_sprint_item(
        db, p["id"], "v1", "Investigate the parser regression",
        tool_requirements=[{
            "name": "replace_symbol_body", "server_or_namespace": "Serena",
            "required_or_preferred": "required", "purpose": "apply the fix",
        }],
    )
    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="starter",
    )
    body = _extract_tag_body(content, "executor_routing")
    assert f"{item['id']}: Serena: replace_symbol_body (required/explicit)" in body


async def test_executor_routing_clause_deterministic_modulo_token(db, tmp_path):
    p = await db_module.create_project(db, "routing-determinism")
    await db_module.add_sprint_item(db, p["id"], "v1", "Investigate the parser regression")
    _, content_a, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="starter",
    )
    _, content_b, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="starter",
    )
    assert _strip_goal_token(content_a) == _strip_goal_token(content_b)


# ---------------------------------------------------------------------------
# capability_contract JSON twin — item_routing_summary / hash, and the
# bounded (capped) item_executor_contracts field.
# ---------------------------------------------------------------------------


async def test_capability_contract_item_routing_summary_matches_direct_call(db):
    p = await db_module.create_project(db, "cc-routing-summary")
    item = await db_module.add_sprint_item(
        db, p["id"], "v1", "Investigate the parser regression",
    )
    items = [dict(item)]
    contract = await cc.build_capability_contract(db, p["id"], items=items)
    expected = ec.build_routing_summary(items)
    assert contract["item_routing_summary"] == expected
    assert contract["item_routing_summary_hash"] == ec.routing_summary_hash(expected)


async def test_capability_contract_item_routing_summary_empty_project(db):
    p = await db_module.create_project(db, "cc-routing-summary-empty")
    contract = await cc.build_capability_contract(db, p["id"])
    assert contract["item_routing_summary"] == []
    assert contract["item_routing_summary_hash"] == ec.routing_summary_hash([])


async def test_capability_contract_routing_summary_hash_matches_goal_text_clause(db, tmp_path):
    """Parity proof (mirrors the 9c6cac08 XML/JSON parity tests): with no
    claimable-batch exclusions in play, the /goal text's <executor_routing>
    hash for the requested version equals an INDEPENDENT
    build_routing_summary/routing_summary_hash call over the same live
    pending items — the hash genuinely proves the compact prose matches the
    canonical extraction, not just "looks similar"."""
    p = await db_module.create_project(db, "cc-routing-hash-parity")
    await db_module.add_sprint_item(db, p["id"], "v1", "Claim sprint items for wave 2")
    await db_module.add_sprint_item(db, p["id"], "v1", "Investigate the parser regression")

    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="goal", version="v1",
    )
    m = re.search(r'<executor_routing hash="([0-9a-f]+)"', content)
    assert m is not None
    embedded_hash = m.group(1)

    live_items = await db_module.get_sprint_items(db, p["id"], version="v1")
    expected_summary = ec.build_routing_summary(live_items)
    assert embedded_hash == ec.routing_summary_hash(expected_summary)


async def test_capability_contract_attached_uniformly_includes_routing_fields_in_starter(db, tmp_path):
    """The MCP dispatch layer attaches capability_contract to EVERY mode
    uniformly (existing behaviour) — this asserts the NEW routing fields
    ride along for mode='starter' specifically, not just 'goal'/'full'."""
    p = await db_module.create_project(db, "mcp-routing-starter")
    item = await db_module.add_sprint_item(
        db, p["id"], "v1", "Investigate the parser regression",
    )
    result = await mcp_handler._handle_task_tools(
        "generate_handoff", {"project_id": p["id"], "mode": "starter"},
        db, str(tmp_path), tenant=None, _mcp_tenant_id=None,
    )
    cc_result = result["capability_contract"]
    assert "item_routing_summary" in cc_result
    assert "item_executor_contracts_truncated" in cc_result
    by_id = {h["item_id"]: h for h in cc_result["item_routing_summary"]}
    assert item["id"] in by_id
    assert by_id[item["id"]]["source"] == "inferred"


async def test_item_executor_contracts_capped_deterministically(db):
    p = await db_module.create_project(db, "cc-executor-contracts-cap")
    ids = []
    for i in range(5):
        it = await db_module.add_sprint_item(
            db, p["id"], "v1", f"pending item {i}", force=True,
        )
        ids.append(it["id"])
    ids.sort()

    contract = await cc.build_capability_contract(db, p["id"], max_executor_contracts=2)
    assert len(contract["item_executor_contracts"]) == 2
    assert contract["item_executor_contracts_truncated"] == {
        "truncated": True, "total_candidates": 5, "included": 2,
    }
    # Deterministic subset: the two lowest sprint-item ids, every time.
    assert [e["item_id"] for e in contract["item_executor_contracts"]] == ids[:2]

    contract_b = await cc.build_capability_contract(db, p["id"], max_executor_contracts=2)
    assert (
        [e["item_id"] for e in contract_b["item_executor_contracts"]]
        == [e["item_id"] for e in contract["item_executor_contracts"]]
    )


async def test_item_executor_contracts_not_truncated_under_cap(db):
    p = await db_module.create_project(db, "cc-executor-contracts-under-cap")
    await db_module.add_sprint_item(db, p["id"], "v1", "Solo item")
    contract = await cc.build_capability_contract(db, p["id"])
    assert contract["item_executor_contracts_truncated"] == {
        "truncated": False, "total_candidates": 1, "included": 1,
    }


# ---------------------------------------------------------------------------
# THE regression guard this item's brief explicitly required: a realistic
# 10+ item paste-ready /goal block stays a few KB, never tens of KB — this
# is what a version WITHOUT the size-discipline constraints would have
# blown through (see capability_contract.build_capability_contract's own
# docstring for the 95KB+/37-69-item incident this guards against).
# ---------------------------------------------------------------------------


async def test_realistic_multiitem_starter_goal_text_stays_well_under_tens_of_kb(db, tmp_path):
    p = await db_module.create_project(db, "size-bound-regression")
    titles = [
        "Investigate the parser regression",
        "Refactor the payments module for clarity",
        "Claim sprint items for the current wave",
        "Generate handoff for mode parity coverage",
        "Run tunnel verification before the deploy gate",
        "Fix a DOCX region claim bug in the report writer",
        "Trace the root cause of the intermittent crash",
        "Understand the auth flow before touching it",
        "Explore the codebase for dead code paths",
        "Audit the migration guard for correctness",
        "Bump a version number in the changelog",
        "Tidy up an unrelated helper function",
    ]
    for t in titles:
        await db_module.add_sprint_item(db, p["id"], "v1", t, force=True)

    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="starter",
    )
    # A few KB, comfortably — NOT the tens-of-KB (or 95KB+) a per-item
    # contract inlined unconditionally for every item would have produced.
    assert len(content) < 10_000, (
        f"starter /goal text is {len(content)} chars for a {len(titles)}-item "
        "scope — expected well under 10KB"
    )
    assert "<executor_routing" in content
