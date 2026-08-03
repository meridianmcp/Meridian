"""Tests for sprint item 76dde31f (665 follow-up) — typed per-item
tool_requirements contract for executor handoffs.

Mirrors tests/test_capability_manifest.py's structure and rigor for the new
meridian.tool_requirements module. Covers:

1. meridian.tool_requirements — schema validation/normalization, secrets and
   machine-local absolute path rejection (reusing capability_manifest's own
   check), deterministic ordering and content hash, the
   required_or_preferred/fallback risk classification, and the legacy
   required_tool read-time compatibility bridge.
2. meridian.db.sprint_items — add_sprint_item / patch_sprint_item / get
   round trip, malformed-input rejection, clearing semantics.
3. meridian.capability_contract.extract_tool_requirements — typed extraction
   shared between the batch /goal XML clause and the structured contract.
4. meridian.handoff — the <tool_requirements> XML clause (batch /goal and
   build_item_briefing) and its byte-for-byte parity with the structured
   capability contract's item_tool_requirements section.
5. MCP tool surface — add_sprint_item / update_sprint_item / get_sprint_items
   round trip tool_requirements end-to-end, including deterministic rejection
   of malformed input (mirrors test_capability_manifest.py's MCP coverage).
"""
from __future__ import annotations

import json as _json

import pytest

from meridian import capability_contract as cc
from meridian import db as db_module
from meridian import handoff as handoff_module
from meridian import tool_requirements as tr


def _valid_requirement(**overrides):
    base = {
        "name": "find_symbol",
        "server_or_namespace": "Serena",
        "required_or_preferred": "required",
        "purpose": "locate the target function before editing",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# tool_requirements — schema validation/normalization (pure, no DB).
# ---------------------------------------------------------------------------

def test_normalize_tool_requirement_accepts_minimal_valid_entry():
    normalized = tr.normalize_tool_requirement(_valid_requirement())
    assert normalized["name"] == "find_symbol"
    assert normalized["server_or_namespace"] == "Serena"
    assert normalized["required_or_preferred"] == "required"
    assert normalized["purpose"] == "locate the target function before editing"
    assert normalized["call_template"] is None
    assert normalized["fallback"] == []
    assert normalized["availability_check"] is None
    assert normalized["verification"] is None


def test_normalize_tool_requirement_accepts_full_entry():
    raw = _valid_requirement(
        required_or_preferred="preferred",
        call_template="find_symbol(name_path='Foo/bar')",
        fallback=["search_code_semantic", "grep"],
        availability_check="tools/list contains 'Serena: find_symbol'",
        verification="the returned symbol's file matches the item's touches_resources",
    )
    normalized = tr.normalize_tool_requirement(raw)
    assert normalized["required_or_preferred"] == "preferred"
    assert normalized["call_template"] == "find_symbol(name_path='Foo/bar')"
    assert normalized["fallback"] == ["search_code_semantic", "grep"]
    assert normalized["availability_check"] == "tools/list contains 'Serena: find_symbol'"
    assert normalized["verification"] == (
        "the returned symbol's file matches the item's touches_resources"
    )


def test_normalize_tool_requirement_accepts_single_string_fallback():
    normalized = tr.normalize_tool_requirement(_valid_requirement(fallback="grep"))
    assert normalized["fallback"] == ["grep"]


@pytest.mark.parametrize(
    "field", ["name", "server_or_namespace", "required_or_preferred", "purpose"]
)
def test_normalize_tool_requirement_rejects_missing_required_field(field):
    raw = _valid_requirement()
    del raw[field]
    with pytest.raises(tr.ToolRequirementError, match=field):
        tr.normalize_tool_requirement(raw)


def test_normalize_tool_requirement_rejects_unknown_field():
    raw = _valid_requirement(unexpected_field="nope")
    with pytest.raises(tr.ToolRequirementError, match="unknown tool_requirements field"):
        tr.normalize_tool_requirement(raw)


def test_normalize_tool_requirement_rejects_non_object():
    with pytest.raises(tr.ToolRequirementError, match="must be an object"):
        tr.normalize_tool_requirement("not-a-dict")


def test_normalize_tool_requirement_rejects_bad_required_or_preferred():
    with pytest.raises(tr.ToolRequirementError, match="required_or_preferred"):
        tr.normalize_tool_requirement(_valid_requirement(required_or_preferred="sometimes"))


def test_normalize_tool_requirement_rejects_secret_shaped_value():
    with pytest.raises(tr.ToolRequirementError, match="secret-shaped"):
        tr.normalize_tool_requirement(
            _valid_requirement(call_template="curl -H 'Authorization: Bearer sk-abcdefghij1234567890'")
        )


@pytest.mark.parametrize("bad_path", ["C:\\Users\\alice\\secrets.env", "/home/alice/.ssh/id_rsa"])
def test_normalize_tool_requirement_rejects_machine_local_absolute_path(bad_path):
    with pytest.raises(tr.ToolRequirementError, match="machine-local absolute path"):
        tr.normalize_tool_requirement(_valid_requirement(availability_check=bad_path))


def test_normalize_tool_requirements_empty_and_none():
    assert tr.normalize_tool_requirements(None) == []
    assert tr.normalize_tool_requirements([]) == []


def test_normalize_tool_requirements_rejects_non_list():
    with pytest.raises(tr.ToolRequirementError, match="must be a list"):
        tr.normalize_tool_requirements("not-a-list")


def test_normalize_tool_requirements_rejects_duplicate_name_and_namespace():
    raw = [_valid_requirement(), _valid_requirement(purpose="a second, different reason")]
    with pytest.raises(tr.ToolRequirementError, match="duplicate tool_requirements entry"):
        tr.normalize_tool_requirements(raw)


def test_normalize_tool_requirements_allows_same_name_different_namespace():
    raw = [
        _valid_requirement(server_or_namespace="Serena"),
        _valid_requirement(server_or_namespace="Filesystem"),
    ]
    normalized = tr.normalize_tool_requirements(raw)
    assert len(normalized) == 2


def test_normalize_tool_requirements_deterministic_ordering():
    raw = [
        _valid_requirement(name="zeta", server_or_namespace="Zserver"),
        _valid_requirement(name="alpha", server_or_namespace="Aserver"),
    ]
    normalized = tr.normalize_tool_requirements(raw)
    assert [(r["server_or_namespace"], r["name"]) for r in normalized] == [
        ("Aserver", "alpha"), ("Zserver", "zeta"),
    ]


def test_tool_requirements_hash_stable_across_input_order():
    a = [_valid_requirement(name="a", server_or_namespace="S"), _valid_requirement(name="b", server_or_namespace="S")]
    b = [_valid_requirement(name="b", server_or_namespace="S"), _valid_requirement(name="a", server_or_namespace="S")]
    assert tr.tool_requirements_hash(tr.normalize_tool_requirements(a)) == \
        tr.tool_requirements_hash(tr.normalize_tool_requirements(b))


def test_tool_requirements_hash_changes_with_content():
    a = tr.normalize_tool_requirements([_valid_requirement()])
    b = tr.normalize_tool_requirements([_valid_requirement(purpose="a totally different reason")])
    assert tr.tool_requirements_hash(a) != tr.tool_requirements_hash(b)


def test_has_tool_requirements():
    assert tr.has_tool_requirements([]) is False
    assert tr.has_tool_requirements(None) is False
    assert tr.has_tool_requirements([_valid_requirement()]) is True


# ---------------------------------------------------------------------------
# requirement_risk_class — required-tool-unavailable vs fallback, explicit.
# ---------------------------------------------------------------------------

def test_risk_class_required_with_no_fallback_is_hard_block():
    req = tr.normalize_tool_requirement(_valid_requirement())
    assert tr.requirement_risk_class(req) == "hard_block"


def test_risk_class_required_with_fallback_has_fallback():
    req = tr.normalize_tool_requirement(_valid_requirement(fallback=["grep"]))
    assert tr.requirement_risk_class(req) == "has_fallback"


def test_risk_class_preferred_is_always_soft_even_with_fallback():
    req = tr.normalize_tool_requirement(
        _valid_requirement(required_or_preferred="preferred", fallback=["grep"])
    )
    assert tr.requirement_risk_class(req) == "soft"
    req2 = tr.normalize_tool_requirement(_valid_requirement(required_or_preferred="preferred"))
    assert tr.requirement_risk_class(req2) == "soft"


# ---------------------------------------------------------------------------
# legacy required_tool compatibility bridge — structured field is canonical;
# legacy required_tool is a read-time fallback ONLY when structured is empty.
# ---------------------------------------------------------------------------

def test_legacy_required_tool_as_requirement_splits_on_colon():
    synthesized = tr.legacy_required_tool_as_requirement("Serena: replace_symbol_body")
    assert synthesized["server_or_namespace"] == "Serena"
    assert synthesized["name"] == "replace_symbol_body"
    assert synthesized["required_or_preferred"] == "required"
    assert synthesized["fallback"] == []


def test_legacy_required_tool_as_requirement_no_colon_uses_legacy_namespace():
    synthesized = tr.legacy_required_tool_as_requirement("meridian__patch_file")
    assert synthesized["server_or_namespace"] == "legacy"
    assert synthesized["name"] == "meridian__patch_file"


def test_effective_tool_requirements_structured_wins_over_legacy():
    item = {
        "tool_requirements": _json.dumps([_valid_requirement(name="a", server_or_namespace="S")]),
        "required_tool": "Serena: replace_symbol_body",
    }
    effective = tr.effective_tool_requirements(item)
    assert len(effective) == 1
    assert effective[0]["name"] == "a"


def test_effective_tool_requirements_falls_back_to_legacy_when_structured_empty():
    item = {"tool_requirements": None, "required_tool": "Serena: replace_symbol_body"}
    effective = tr.effective_tool_requirements(item)
    assert len(effective) == 1
    assert effective[0]["name"] == "replace_symbol_body"
    assert effective[0]["server_or_namespace"] == "Serena"


def test_effective_tool_requirements_empty_when_neither_set():
    assert tr.effective_tool_requirements({}) == []


def test_parse_tool_requirements_malformed_json_degrades_to_empty():
    assert tr.parse_tool_requirements("{not valid json") == []
    assert tr.parse_tool_requirements("null") == []
    assert tr.parse_tool_requirements('"a string, not a list"') == []


def test_serialize_tool_requirements_raises_on_malformed_input():
    with pytest.raises(tr.ToolRequirementError):
        tr.serialize_tool_requirements([{"name": "x"}])  # missing required fields


def test_serialize_then_parse_round_trip():
    normalized_in = [_valid_requirement()]
    stored = tr.serialize_tool_requirements(normalized_in)
    assert stored is not None
    parsed = tr.parse_tool_requirements(stored)
    assert parsed == tr.normalize_tool_requirements(normalized_in)


def test_serialize_tool_requirements_empty_returns_none():
    assert tr.serialize_tool_requirements(None) is None
    assert tr.serialize_tool_requirements([]) is None


# ---------------------------------------------------------------------------
# meridian.db.sprint_items — add/update/get persistence round trip.
# ---------------------------------------------------------------------------

async def test_add_sprint_item_persists_tool_requirements(db):
    project = await db_module.create_project(db, "tool-reqs-add")
    item = await db_module.add_sprint_item(
        db, project["id"], "v1", "Refactor auth module",
        tool_requirements=[_valid_requirement()],
    )
    assert "error" not in item
    fetched = await db_module.get_sprint_item(db, item["id"])
    parsed = tr.parse_tool_requirements(fetched["tool_requirements"])
    assert len(parsed) == 1
    assert parsed[0]["name"] == "find_symbol"


async def test_add_sprint_item_rejects_malformed_tool_requirements(db):
    project = await db_module.create_project(db, "tool-reqs-add-bad")
    with pytest.raises(ValueError):
        await db_module.add_sprint_item(
            db, project["id"], "v1", "Refactor auth module",
            tool_requirements=[{"name": "x"}],  # missing required fields
        )


async def test_add_sprint_item_without_tool_requirements_leaves_column_null(db):
    project = await db_module.create_project(db, "tool-reqs-add-none")
    item = await db_module.add_sprint_item(db, project["id"], "v1", "An ordinary item")
    fetched = await db_module.get_sprint_item(db, item["id"])
    assert fetched.get("tool_requirements") is None


async def test_patch_sprint_item_sets_tool_requirements(db):
    project = await db_module.create_project(db, "tool-reqs-patch")
    item = await db_module.add_sprint_item(db, project["id"], "v1", "An ordinary item")
    updated = await db_module.patch_sprint_item(
        db, project["id"], item["id"], tool_requirements=[_valid_requirement()],
    )
    parsed = tr.parse_tool_requirements(updated["tool_requirements"])
    assert len(parsed) == 1


async def test_patch_sprint_item_clears_tool_requirements(db):
    project = await db_module.create_project(db, "tool-reqs-patch-clear")
    item = await db_module.add_sprint_item(
        db, project["id"], "v1", "Item with a requirement",
        tool_requirements=[_valid_requirement()],
    )
    updated = await db_module.patch_sprint_item(
        db, project["id"], item["id"], tool_requirements=[],
    )
    assert updated["tool_requirements"] is None


async def test_patch_sprint_item_omitting_key_leaves_unchanged(db):
    project = await db_module.create_project(db, "tool-reqs-patch-omit")
    item = await db_module.add_sprint_item(
        db, project["id"], "v1", "Item with a requirement",
        tool_requirements=[_valid_requirement()],
    )
    updated = await db_module.patch_sprint_item(
        db, project["id"], item["id"], title="Renamed",
    )
    parsed = tr.parse_tool_requirements(updated["tool_requirements"])
    assert len(parsed) == 1


async def test_patch_sprint_item_rejects_malformed_tool_requirements(db):
    project = await db_module.create_project(db, "tool-reqs-patch-bad")
    item = await db_module.add_sprint_item(db, project["id"], "v1", "An ordinary item")
    with pytest.raises(ValueError):
        await db_module.patch_sprint_item(
            db, project["id"], item["id"],
            tool_requirements=[{"name": "x", "server_or_namespace": "S",
                                 "required_or_preferred": "extremely-required", "purpose": "p"}],
        )


# ---------------------------------------------------------------------------
# capability_contract.extract_tool_requirements — pure typed extraction.
# ---------------------------------------------------------------------------

def test_extract_tool_requirements_pure_data():
    items = [
        {"id": "item1", "tool_requirements": _json.dumps([_valid_requirement()])},
        {"id": "item2"},  # nothing declared
        {"id": "item3", "required_tool": "Serena: find_symbol"},  # legacy fallback
        {"tool_requirements": _json.dumps([_valid_requirement()])},  # no id — skipped
    ]
    extracted = cc.extract_tool_requirements(items)
    ids = {e["item_id"] for e in extracted}
    assert ids == {"item1", "item3"}
    item3 = next(e for e in extracted if e["item_id"] == "item3")
    assert item3["requirements"][0]["name"] == "find_symbol"


def test_extract_tool_requirements_structured_wins_over_legacy_per_item():
    items = [{
        "id": "item1",
        "tool_requirements": _json.dumps([_valid_requirement(name="structured_tool")]),
        "required_tool": "Serena: legacy_tool",
    }]
    extracted = cc.extract_tool_requirements(items)
    assert len(extracted) == 1
    assert extracted[0]["requirements"][0]["name"] == "structured_tool"


def test_extract_tool_requirements_empty_for_empty_items():
    assert cc.extract_tool_requirements([]) == []


# ---------------------------------------------------------------------------
# handoff._build_tool_requirements_clause / build_item_briefing rendering.
# ---------------------------------------------------------------------------

def test_build_tool_requirements_clause_empty_for_no_requirements():
    assert handoff_module._build_tool_requirements_clause([]) == ""
    assert handoff_module._build_tool_requirements_clause([{"id": "x"}]) == ""


def test_build_tool_requirements_clause_embeds_canonical_json():
    items = [{"id": "aaa111", "tool_requirements": _json.dumps([_valid_requirement()])}]
    out = handoff_module._build_tool_requirements_clause(items)
    assert out.startswith("\n<tool_requirements>")
    assert out.endswith("</tool_requirements>")
    body = out[len("\n<tool_requirements>"):-len("</tool_requirements>")]
    embedded = _json.loads(body)
    assert embedded == cc.extract_tool_requirements(items)


def test_build_item_briefing_includes_tool_requirements_clause():
    item = {
        "id": "item-uuid",
        "title": "Refactor auth",
        "tool_requirements": _json.dumps([_valid_requirement()]),
    }
    briefing = handoff_module.build_item_briefing(item)
    assert "<tool_requirements>" in briefing
    start = briefing.index("<tool_requirements>") + len("<tool_requirements>")
    end = briefing.index("</tool_requirements>")
    embedded = _json.loads(briefing[start:end])
    assert embedded[0]["name"] == "find_symbol"


def test_build_item_briefing_falls_back_to_legacy_required_tool():
    item = {"id": "item-uuid", "title": "Refactor auth", "required_tool": "Serena: find_symbol"}
    briefing = handoff_module.build_item_briefing(item)
    assert "<tool_requirements>" in briefing
    assert "<required_tool>" in briefing  # legacy clause still renders too


def test_build_item_briefing_no_tool_requirements_tag_when_nothing_declared():
    item = {"id": "item-uuid", "title": "An ordinary item"}
    briefing = handoff_module.build_item_briefing(item)
    assert "<tool_requirements>" not in briefing


# ---------------------------------------------------------------------------
# XML/JSON parity — generate_handoff's <tool_requirements> clause must carry
# IDENTICAL typed data to capability_contract's item_tool_requirements.
# ---------------------------------------------------------------------------

async def test_generate_handoff_xml_and_contract_carry_identical_tool_requirements(db, tmp_path):
    project = await db_module.create_project(db, "tool-reqs-parity")
    await db_module.add_sprint_item(
        db, project["id"], "v1", "Refactor auth module",
        tool_requirements=[_valid_requirement()],
    )
    await db_module.add_sprint_item(
        db, project["id"], "v1", "Fix a bug with a legacy pin",
        required_tool="Serena: replace_symbol_body",
    )
    _, content, _ = await handoff_module.generate_handoff(
        db, project["id"], str(tmp_path), mode="goal", skip_ai_summary=True,
    )
    assert "<tool_requirements>" in content
    start = content.index("<tool_requirements>") + len("<tool_requirements>")
    end = content.index("</tool_requirements>")
    xml_typed = _json.loads(content[start:end])

    contract = await cc.build_capability_contract(db, project["id"])
    assert contract["item_tool_requirements"] == xml_typed
    assert len(xml_typed) == 2


async def test_build_capability_contract_item_tool_requirements_empty_project(db):
    project = await db_module.create_project(db, "tool-reqs-empty-contract")
    contract = await cc.build_capability_contract(db, project["id"])
    assert contract["item_tool_requirements"] == []


async def test_build_capability_contract_accepts_explicit_items_override(db):
    project = await db_module.create_project(db, "tool-reqs-explicit-items")
    items = [{"id": "explicit-1", "status": "pending",
              "tool_requirements": _json.dumps([_valid_requirement()])}]
    contract = await cc.build_capability_contract(db, project["id"], items=items)
    assert contract["item_tool_requirements"] == cc.extract_tool_requirements(items)


# ---------------------------------------------------------------------------
# MCP tool surface — add_sprint_item / update_sprint_item / get_sprint_items
# round trip tool_requirements end-to-end.
# ---------------------------------------------------------------------------

def _mcp_call(client, name, arguments):
    r = client.post("/mcp/sse", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    })
    assert r.status_code == 200
    return r.json()


def _result(resp):
    assert resp.get("result") is not None, resp
    return _json.loads(resp["result"]["content"][0]["text"])


def test_tool_requirements_tools_use_shared_schema():
    from meridian import mcp_tools

    add_item = next(t for t in mcp_tools._MCP_TOOLS_LIST if t["name"] == "add_sprint_item")
    upd_item = next(t for t in mcp_tools._MCP_TOOLS_LIST if t["name"] == "update_sprint_item")
    assert "tool_requirements" in add_item["inputSchema"]["properties"]
    assert "tool_requirements" in upd_item["inputSchema"]["properties"]
    # Shared constant — never two independently-maintained schema fragments.
    assert (
        add_item["inputSchema"]["properties"]["tool_requirements"]
        is upd_item["inputSchema"]["properties"]["tool_requirements"]
    )


def test_mcp_add_sprint_item_persists_tool_requirements(client):
    pid = client.post("/projects", json={"name": "mcp-tool-reqs-add"}).json()["id"]
    result = _result(_mcp_call(client, "add_sprint_item", {
        "project_id": pid, "version": "v1", "title": "Refactor auth module",
        "tool_requirements": [_valid_requirement()],
    }))
    assert "error" not in result
    item_id = result["id"]

    fetched = _result(_mcp_call(client, "get_sprint_items", {"project_id": pid}))
    item = next(it for it in fetched if it["id"] == item_id)
    parsed = tr.parse_tool_requirements(item["tool_requirements"])
    assert len(parsed) == 1
    assert parsed[0]["name"] == "find_symbol"


def test_mcp_add_sprint_item_rejects_malformed_tool_requirements(client):
    pid = client.post("/projects", json={"name": "mcp-tool-reqs-add-bad"}).json()["id"]
    result = _result(_mcp_call(client, "add_sprint_item", {
        "project_id": pid, "version": "v1", "title": "Refactor auth module",
        "tool_requirements": [{"name": "x", "purpose": "missing other required fields"}],
    }))
    assert "error" in result
    assert "server_or_namespace" in result["error"] or "required_or_preferred" in result["error"]


def test_mcp_update_sprint_item_sets_then_clears_tool_requirements(client):
    pid = client.post("/projects", json={"name": "mcp-tool-reqs-update"}).json()["id"]
    added = _result(_mcp_call(client, "add_sprint_item", {
        "project_id": pid, "version": "v1", "title": "An ordinary item",
    }))
    item_id = added["id"]

    set_result = _result(_mcp_call(client, "update_sprint_item", {
        "project_id": pid, "item_id": item_id,
        "tool_requirements": [_valid_requirement()],
    }))
    assert "error" not in set_result
    parsed = tr.parse_tool_requirements(set_result["tool_requirements"])
    assert len(parsed) == 1

    cleared = _result(_mcp_call(client, "update_sprint_item", {
        "project_id": pid, "item_id": item_id, "tool_requirements": [],
    }))
    assert cleared["tool_requirements"] is None


def test_mcp_update_sprint_item_rejects_secret_shaped_tool_requirement(client):
    pid = client.post("/projects", json={"name": "mcp-tool-reqs-secret"}).json()["id"]
    added = _result(_mcp_call(client, "add_sprint_item", {
        "project_id": pid, "version": "v1", "title": "An ordinary item",
    }))
    result = _result(_mcp_call(client, "update_sprint_item", {
        "project_id": pid, "item_id": added["id"],
        "tool_requirements": [_valid_requirement(
            call_template="curl -H 'Authorization: Bearer sk-abcdefghij1234567890'"
        )],
    }))
    assert "error" in result
    assert "secret-shaped" in result["error"]
