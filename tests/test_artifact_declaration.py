"""Tests for sprint item 2f9cb288 (b7308039 / 665 follow-up) — typed, persisted
artifact_kind / planned_output / policy declaration contract.

Mirrors tests/test_tool_requirements.py's structure and rigor for the new
meridian.artifact_declaration module. Covers:

1. meridian.artifact_declaration — schema validation/normalization for all
   three fields, reuse of meridian.pointers.validate_pointer for
   planned_output, secrets and machine-local absolute path rejection
   (reusing capability_manifest's own check), serialize/parse round trip,
   and the effective_* accessors' backward-compatible defaults.
2. meridian.db.sprint_items — add_sprint_item / patch_sprint_item / get
   round trip, malformed-input rejection, clearing semantics, and a
   backward-compatible legacy item (added before this feature existed /
   with none of these fields declared).
3. meridian.handoff — the <artifact_declaration> clause in
   build_item_briefing.
4. MCP tool surface — add_sprint_item / update_sprint_item / get_sprint_items
   round trip the three fields end-to-end, including deterministic
   rejection of malformed input.
"""
from __future__ import annotations

import json as _json

import pytest

from meridian import artifact_declaration as ad
from meridian import db as db_module
from meridian import handoff as handoff_module


def _valid_planned_output(**overrides):
    base = {
        "source_type": "code",
        "targets": [
            {
                "uri": "outputs/figures/ablation.png",
                "selector": {"type": "range", "start_line": 1, "end_line": 1},
                "target_kind": "planned_new",
            }
        ],
        "label": "ablation figure",
        "provenance_required": True,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# artifact_declaration — schema validation/normalization (pure, no DB).
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kind", ["document_only", "figure", "table"])
def test_normalize_artifact_kind_accepts_known_values(kind):
    assert ad.normalize_artifact_kind(kind) == kind


def test_normalize_artifact_kind_case_and_whitespace_tolerant():
    assert ad.normalize_artifact_kind("  Figure  ") == "figure"


def test_normalize_artifact_kind_rejects_unknown_value():
    with pytest.raises(ad.ArtifactDeclarationError, match="artifact_kind"):
        ad.normalize_artifact_kind("blueprint")


def test_normalize_artifact_kind_rejects_non_string():
    with pytest.raises(ad.ArtifactDeclarationError):
        ad.normalize_artifact_kind(123)
    with pytest.raises(ad.ArtifactDeclarationError):
        ad.normalize_artifact_kind("")


def test_parse_artifact_kind_lenient_on_read():
    assert ad.parse_artifact_kind("figure") == "figure"
    assert ad.parse_artifact_kind(None) is None
    assert ad.parse_artifact_kind("") is None
    assert ad.parse_artifact_kind(123) is None


def test_normalize_planned_output_none_passes_through():
    assert ad.normalize_planned_output(None) is None


def test_normalize_planned_output_accepts_valid_pointer():
    normalized = ad.normalize_planned_output(_valid_planned_output())
    assert normalized["source_type"] == "code"
    assert normalized["label"] == "ablation figure"
    assert normalized["provenance_required"] is True
    target = normalized["targets"][0]
    assert target["uri"] == "outputs/figures/ablation.png"
    assert target["target_kind"] == "planned_new"
    assert target["selector"]["type"] == "range"


def test_normalize_planned_output_defaults_provenance_required_false():
    raw = _valid_planned_output()
    del raw["provenance_required"]
    normalized = ad.normalize_planned_output(raw)
    assert normalized["provenance_required"] is False


def test_normalize_planned_output_rejects_non_object():
    with pytest.raises(ad.ArtifactDeclarationError, match="must be an object"):
        ad.normalize_planned_output("not-a-dict")


def test_normalize_planned_output_rejects_unknown_top_field():
    raw = _valid_planned_output(unexpected_field="nope")
    with pytest.raises(ad.ArtifactDeclarationError, match="unknown planned_output field"):
        ad.normalize_planned_output(raw)


def test_normalize_planned_output_rejects_non_bool_provenance_required():
    raw = _valid_planned_output(provenance_required="yes")
    with pytest.raises(ad.ArtifactDeclarationError, match="provenance_required"):
        ad.normalize_planned_output(raw)


def test_normalize_planned_output_reuses_pointer_validation_for_malformed_targets():
    """Malformed pointer shapes reject via meridian.pointers.validate_pointer
    (NOT reimplemented) — a missing targets array is a pointer-level error."""
    raw = _valid_planned_output()
    raw["targets"] = []
    with pytest.raises(ad.ArtifactDeclarationError, match="planned_output:"):
        ad.normalize_planned_output(raw)


def test_normalize_planned_output_rejects_bad_selector_type():
    raw = _valid_planned_output()
    raw["targets"] = [{"uri": "x.png", "selector": {"type": "not_a_real_type"}}]
    with pytest.raises(ad.ArtifactDeclarationError):
        ad.normalize_planned_output(raw)


def test_normalize_planned_output_rejects_secret_shaped_label():
    raw = _valid_planned_output(label="curl -H 'Authorization: Bearer sk-abcdefghij1234567890'")
    with pytest.raises(ad.ArtifactDeclarationError, match="secret-shaped"):
        ad.normalize_planned_output(raw)


@pytest.mark.parametrize("bad_uri", ["C:\\Users\\alice\\outputs\\fig.png", "/home/alice/outputs/fig.png"])
def test_normalize_planned_output_rejects_machine_local_absolute_uri(bad_uri):
    raw = _valid_planned_output()
    raw["targets"] = [{"uri": bad_uri, "selector": {"type": "range", "start_line": 1, "end_line": 1}}]
    with pytest.raises(ad.ArtifactDeclarationError, match="machine-local absolute path"):
        ad.normalize_planned_output(raw)


def test_normalize_planned_output_never_infers_from_generic_pointer():
    """Do not silently infer a planned output — an empty/absent declaration
    stays None; there is no inference helper in this module at all."""
    assert ad.normalize_planned_output(None) is None
    assert ad.effective_planned_output({}) is None


def test_serialize_then_parse_planned_output_round_trip():
    normalized_in = _valid_planned_output()
    stored = ad.serialize_planned_output(normalized_in)
    assert stored is not None
    parsed = ad.parse_planned_output(stored)
    assert parsed == ad.normalize_planned_output(normalized_in)


def test_serialize_planned_output_empty_returns_none():
    assert ad.serialize_planned_output(None) is None


def test_serialize_planned_output_raises_on_malformed_input():
    with pytest.raises(ad.ArtifactDeclarationError):
        ad.serialize_planned_output({"source_type": "code"})  # missing targets


def test_parse_planned_output_malformed_json_degrades_to_none():
    assert ad.parse_planned_output("{not valid json") is None
    assert ad.parse_planned_output("null") is None
    assert ad.parse_planned_output('"a string, not an object"') is None


# ---------------------------------------------------------------------------
# policy — artifact_pointer_check + guard flags.
# ---------------------------------------------------------------------------

def test_normalize_artifact_policy_none_passes_through():
    assert ad.normalize_artifact_policy(None) is None


def test_normalize_artifact_policy_fills_defaults():
    normalized = ad.normalize_artifact_policy({})
    assert normalized == {
        "artifact_pointer_check": "warn",
        "require_exact_figure_output_pointer": False,
        "require_exact_table_output_pointer": False,
        "allow_document_only_override": False,
    }


@pytest.mark.parametrize("level", ["off", "warn", "strict"])
def test_normalize_artifact_policy_accepts_valid_levels(level):
    normalized = ad.normalize_artifact_policy({"artifact_pointer_check": level})
    assert normalized["artifact_pointer_check"] == level


def test_normalize_artifact_policy_rejects_bad_check_level():
    with pytest.raises(ad.ArtifactDeclarationError, match="artifact_pointer_check"):
        ad.normalize_artifact_policy({"artifact_pointer_check": "sometimes"})


def test_normalize_artifact_policy_rejects_unknown_field():
    with pytest.raises(ad.ArtifactDeclarationError, match="unknown policy field"):
        ad.normalize_artifact_policy({"artifact_pointer_check": "warn", "nope": True})


def test_normalize_artifact_policy_rejects_non_bool_guard_flag():
    with pytest.raises(ad.ArtifactDeclarationError, match="require_exact_figure_output_pointer"):
        ad.normalize_artifact_policy({"require_exact_figure_output_pointer": "yes"})


def test_normalize_artifact_policy_rejects_non_object():
    with pytest.raises(ad.ArtifactDeclarationError, match="must be an object"):
        ad.normalize_artifact_policy("not-a-dict")


def test_serialize_then_parse_artifact_policy_round_trip():
    normalized_in = {"artifact_pointer_check": "strict", "require_exact_table_output_pointer": True}
    stored = ad.serialize_artifact_policy(normalized_in)
    assert stored is not None
    parsed = ad.parse_artifact_policy(stored)
    assert parsed == ad.normalize_artifact_policy(normalized_in)


def test_serialize_artifact_policy_empty_returns_none():
    assert ad.serialize_artifact_policy(None) is None


# ---------------------------------------------------------------------------
# effective_* accessors — backward compatibility: absent == unknown/default.
# ---------------------------------------------------------------------------

def test_effective_artifact_kind_absent_is_none():
    assert ad.effective_artifact_kind({}) is None
    assert ad.effective_artifact_kind({"artifact_kind": None}) is None


def test_effective_artifact_kind_reads_stored_value():
    assert ad.effective_artifact_kind({"artifact_kind": "table"}) == "table"


def test_effective_planned_output_absent_is_none():
    assert ad.effective_planned_output({}) is None


def test_effective_planned_output_reads_stored_json():
    stored = ad.serialize_planned_output(_valid_planned_output())
    item = {"planned_output": stored}
    assert ad.effective_planned_output(item)["source_type"] == "code"


def test_effective_artifact_policy_absent_is_project_default_warn():
    """Backward compatibility (b0d42ef6-style contract): absent is 'unknown',
    never a specific enforcement level — the project default is warn, not
    off and not strict."""
    effective = ad.effective_artifact_policy({})
    assert effective["artifact_pointer_check"] == "warn"
    assert effective["require_exact_figure_output_pointer"] is False
    assert effective["require_exact_table_output_pointer"] is False
    assert effective["allow_document_only_override"] is False


def test_effective_artifact_policy_merges_partial_declaration_over_default():
    stored = ad.serialize_artifact_policy({"artifact_pointer_check": "strict"})
    effective = ad.effective_artifact_policy({"artifact_policy": stored})
    assert effective["artifact_pointer_check"] == "strict"
    # Fields not declared still fall back to the default (False).
    assert effective["require_exact_figure_output_pointer"] is False


def test_has_artifact_declaration():
    assert ad.has_artifact_declaration({}) is False
    assert ad.has_artifact_declaration({"artifact_kind": "figure"}) is True
    assert ad.has_artifact_declaration({"planned_output": "{}"}) is True
    assert ad.has_artifact_declaration({"artifact_policy": "{}"}) is True


# ---------------------------------------------------------------------------
# meridian.db.sprint_items — add/update/get persistence round trip.
# ---------------------------------------------------------------------------

async def test_add_sprint_item_persists_artifact_declaration(db):
    project = await db_module.create_project(db, "artifact-decl-add")
    item = await db_module.add_sprint_item(
        db, project["id"], "v1", "Produce the ablation figure",
        artifact_kind="figure",
        planned_output=_valid_planned_output(),
        artifact_policy={"artifact_pointer_check": "strict"},
    )
    assert "error" not in item
    fetched = await db_module.get_sprint_item(db, item["id"])
    assert fetched["artifact_kind"] == "figure"
    planned = ad.parse_planned_output(fetched["planned_output"])
    assert planned["label"] == "ablation figure"
    policy = ad.parse_artifact_policy(fetched["artifact_policy"])
    assert policy["artifact_pointer_check"] == "strict"


async def test_add_sprint_item_rejects_malformed_artifact_kind(db):
    project = await db_module.create_project(db, "artifact-decl-add-badkind")
    with pytest.raises(ValueError):
        await db_module.add_sprint_item(
            db, project["id"], "v1", "Bad kind item", artifact_kind="blueprint",
        )


async def test_add_sprint_item_rejects_malformed_planned_output(db):
    project = await db_module.create_project(db, "artifact-decl-add-badptr")
    with pytest.raises(ValueError):
        await db_module.add_sprint_item(
            db, project["id"], "v1", "Bad pointer item",
            planned_output={"source_type": "code"},  # missing targets
        )


async def test_add_sprint_item_rejects_malformed_policy(db):
    project = await db_module.create_project(db, "artifact-decl-add-badpolicy")
    with pytest.raises(ValueError):
        await db_module.add_sprint_item(
            db, project["id"], "v1", "Bad policy item",
            artifact_policy={"artifact_pointer_check": "sometimes"},
        )


async def test_patch_sprint_item_sets_artifact_declaration(db):
    project = await db_module.create_project(db, "artifact-decl-patch")
    item = await db_module.add_sprint_item(db, project["id"], "v1", "An ordinary item")
    updated = await db_module.patch_sprint_item(
        db, project["id"], item["id"],
        artifact_kind="table",
        planned_output=_valid_planned_output(source_type="docs"),
        artifact_policy={"require_exact_table_output_pointer": True},
    )
    assert updated["artifact_kind"] == "table"
    assert ad.parse_planned_output(updated["planned_output"])["source_type"] == "docs"
    assert ad.parse_artifact_policy(updated["artifact_policy"])["require_exact_table_output_pointer"] is True


async def test_patch_sprint_item_clears_artifact_declaration(db):
    project = await db_module.create_project(db, "artifact-decl-patch-clear")
    item = await db_module.add_sprint_item(
        db, project["id"], "v1", "Item with declarations",
        artifact_kind="figure",
        planned_output=_valid_planned_output(),
        artifact_policy={"artifact_pointer_check": "strict"},
    )
    updated = await db_module.patch_sprint_item(
        db, project["id"], item["id"],
        artifact_kind="", planned_output=None, artifact_policy=None,
    )
    assert updated["artifact_kind"] is None
    assert updated["planned_output"] is None
    assert updated["artifact_policy"] is None
    # Cleared policy reads back as the project default, not an error.
    assert ad.effective_artifact_policy(updated)["artifact_pointer_check"] == "warn"


async def test_patch_sprint_item_omitting_keys_leaves_unchanged(db):
    project = await db_module.create_project(db, "artifact-decl-patch-omit")
    item = await db_module.add_sprint_item(
        db, project["id"], "v1", "Item with a kind", artifact_kind="table",
    )
    updated = await db_module.patch_sprint_item(
        db, project["id"], item["id"], title="Renamed",
    )
    assert updated["artifact_kind"] == "table"


async def test_patch_sprint_item_rejects_malformed_planned_output(db):
    project = await db_module.create_project(db, "artifact-decl-patch-bad")
    item = await db_module.add_sprint_item(db, project["id"], "v1", "An ordinary item")
    with pytest.raises(ValueError):
        await db_module.patch_sprint_item(
            db, project["id"], item["id"],
            planned_output={"source_type": "code", "targets": []},
        )


# ---------------------------------------------------------------------------
# Backward compatibility — a legacy item declaring NONE of these fields.
# ---------------------------------------------------------------------------

async def test_legacy_item_without_artifact_declaration_reads_as_unknown(db):
    """An item added before this feature existed (or that simply never
    declares artifact metadata) round-trips through get_sprint_item /
    get_sprint_items with all three columns NULL, and the effective_*
    accessors report 'unknown' / the project default — never an error,
    never a guessed value."""
    project = await db_module.create_project(db, "artifact-decl-legacy")
    legacy = await db_module.add_sprint_item(db, project["id"], "v1", "A plain legacy item")
    fetched = await db_module.get_sprint_item(db, legacy["id"])
    assert fetched["artifact_kind"] is None
    assert fetched["planned_output"] is None
    assert fetched["artifact_policy"] is None
    assert ad.effective_artifact_kind(fetched) is None
    assert ad.effective_planned_output(fetched) is None
    assert ad.effective_artifact_policy(fetched) == ad.default_artifact_policy()

    all_items = await db_module.get_sprint_items(db, project["id"])
    found = next(it for it in all_items if it["id"] == legacy["id"])
    assert found["artifact_kind"] is None


# ---------------------------------------------------------------------------
# handoff.build_item_briefing — <artifact_declaration> clause.
# ---------------------------------------------------------------------------

def test_build_item_briefing_includes_artifact_declaration_clause():
    item = {
        "id": "item-uuid",
        "title": "Produce the ablation figure",
        "artifact_kind": "figure",
        "planned_output": _json.dumps(_valid_planned_output()),
        "artifact_policy": _json.dumps({"artifact_pointer_check": "strict"}),
    }
    briefing = handoff_module.build_item_briefing(item)
    assert "<artifact_declaration>" in briefing
    start = briefing.index("<artifact_declaration>") + len("<artifact_declaration>")
    end = briefing.index("</artifact_declaration>")
    embedded = _json.loads(briefing[start:end])
    assert embedded["artifact_kind"] == "figure"
    assert embedded["planned_output"]["source_type"] == "code"
    # Policy is the EFFECTIVE (merged) policy, not the raw declared fragment.
    assert embedded["policy"]["artifact_pointer_check"] == "strict"
    assert embedded["policy"]["require_exact_figure_output_pointer"] is False


def test_build_item_briefing_no_artifact_declaration_tag_when_nothing_declared():
    item = {"id": "item-uuid", "title": "An ordinary item"}
    briefing = handoff_module.build_item_briefing(item)
    assert "<artifact_declaration>" not in briefing


def test_build_item_briefing_artifact_kind_only_still_renders_clause():
    item = {"id": "item-uuid", "title": "Kind only", "artifact_kind": "document_only"}
    briefing = handoff_module.build_item_briefing(item)
    assert "<artifact_declaration>" in briefing
    start = briefing.index("<artifact_declaration>") + len("<artifact_declaration>")
    end = briefing.index("</artifact_declaration>")
    embedded = _json.loads(briefing[start:end])
    assert embedded["artifact_kind"] == "document_only"
    assert embedded["planned_output"] is None
    # No per-item policy declared — falls back to the project default.
    assert embedded["policy"]["artifact_pointer_check"] == "warn"


# ---------------------------------------------------------------------------
# MCP tool surface — add_sprint_item / update_sprint_item / get_sprint_items
# round trip artifact declarations end-to-end.
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


def test_artifact_declaration_tools_use_shared_schema():
    from meridian import mcp_tools

    add_item = next(t for t in mcp_tools._MCP_TOOLS_LIST if t["name"] == "add_sprint_item")
    upd_item = next(t for t in mcp_tools._MCP_TOOLS_LIST if t["name"] == "update_sprint_item")
    for field in ("artifact_kind", "planned_output", "policy"):
        assert field in add_item["inputSchema"]["properties"]
        assert field in upd_item["inputSchema"]["properties"]
        # Shared constant — never two independently-maintained schema fragments.
        assert (
            add_item["inputSchema"]["properties"][field]
            is upd_item["inputSchema"]["properties"][field]
        )


def test_mcp_add_sprint_item_persists_artifact_declaration(client):
    pid = client.post("/projects", json={"name": "mcp-artifact-decl-add"}).json()["id"]
    result = _result(_mcp_call(client, "add_sprint_item", {
        "project_id": pid, "version": "v1", "title": "Produce the ablation figure",
        "artifact_kind": "figure",
        "planned_output": _valid_planned_output(),
        "policy": {"artifact_pointer_check": "strict"},
    }))
    assert "error" not in result
    item_id = result["id"]

    fetched = _result(_mcp_call(client, "get_sprint_items", {"project_id": pid}))
    item = next(it for it in fetched if it["id"] == item_id)
    assert item["artifact_kind"] == "figure"
    assert ad.parse_planned_output(item["planned_output"])["label"] == "ablation figure"
    assert ad.parse_artifact_policy(item["artifact_policy"])["artifact_pointer_check"] == "strict"


def test_mcp_add_sprint_item_rejects_malformed_artifact_kind(client):
    pid = client.post("/projects", json={"name": "mcp-artifact-decl-badkind"}).json()["id"]
    result = _result(_mcp_call(client, "add_sprint_item", {
        "project_id": pid, "version": "v1", "title": "Bad kind item",
        "artifact_kind": "blueprint",
    }))
    assert "error" in result
    assert "artifact_kind" in result["error"]


def test_mcp_update_sprint_item_sets_then_clears_artifact_declaration(client):
    pid = client.post("/projects", json={"name": "mcp-artifact-decl-update"}).json()["id"]
    added = _result(_mcp_call(client, "add_sprint_item", {
        "project_id": pid, "version": "v1", "title": "An ordinary item",
    }))
    item_id = added["id"]

    set_result = _result(_mcp_call(client, "update_sprint_item", {
        "project_id": pid, "item_id": item_id,
        "artifact_kind": "table",
        "planned_output": _valid_planned_output(source_type="docs"),
        "policy": {"require_exact_table_output_pointer": True},
    }))
    assert "error" not in set_result
    assert set_result["artifact_kind"] == "table"

    cleared = _result(_mcp_call(client, "update_sprint_item", {
        "project_id": pid, "item_id": item_id,
        "artifact_kind": "", "planned_output": None, "policy": None,
    }))
    assert cleared["artifact_kind"] is None
    assert cleared["planned_output"] is None
    assert cleared["artifact_policy"] is None


def test_mcp_update_sprint_item_rejects_secret_shaped_planned_output(client):
    pid = client.post("/projects", json={"name": "mcp-artifact-decl-secret"}).json()["id"]
    added = _result(_mcp_call(client, "add_sprint_item", {
        "project_id": pid, "version": "v1", "title": "An ordinary item",
    }))
    result = _result(_mcp_call(client, "update_sprint_item", {
        "project_id": pid, "item_id": added["id"],
        "planned_output": _valid_planned_output(
            label="curl -H 'Authorization: Bearer sk-abcdefghij1234567890'"
        ),
    }))
    assert "error" in result
    assert "secret-shaped" in result["error"]
