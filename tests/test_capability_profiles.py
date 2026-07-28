"""Tests for sprint item 02038afe — persist capability profiles with explicit
inheritance and per-item overrides (v0.2.5).

Builds on 649e095f (meridian.capability_manifest / project_capabilities —
see tests/test_capability_manifest.py). Covers:

1. meridian.capability_profile — pure scope/disable/provenance validation and
   the merge_layers algorithm (override precedence, conflict detection,
   disable semantics).
2. meridian.db — get/set/clear_capability_profile round trip for a single
   scope, and get_effective_capability_profile's multi-layer resolution
   (workspace -> user -> project -> sprint_version -> item), including
   SQLite/Postgres parity via the ``anydb`` fixture.
3. MCP tool surface — set_capability_profile / clear_capability_profile /
   get_effective_capability_profile registration and end-to-end dispatch.
"""
from __future__ import annotations

import json as _json

import pytest

from meridian import capability_manifest as cm
from meridian import capability_profile as cp
from meridian import db as db_module


def _valid_capability(**overrides):
    base = {
        "id": "code-search",
        "purpose": "find symbols/functions/classes",
        "required_tools": ["Serena: find_symbol"],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# capability_profile — pure validation (no DB).
# ---------------------------------------------------------------------------

def test_normalize_scope_type_accepts_all_valid_values():
    for scope_type in cp.SCOPE_TYPES:
        assert cp.normalize_scope_type(scope_type) == scope_type
        assert cp.normalize_scope_type(scope_type.upper()) == scope_type


def test_normalize_scope_type_rejects_unknown_value():
    with pytest.raises(cp.CapabilityProfileError, match="scope_type must be one of"):
        cp.normalize_scope_type("planet")


def test_normalize_scope_type_rejects_non_string():
    with pytest.raises(cp.CapabilityProfileError, match="scope_type must be one of"):
        cp.normalize_scope_type(None)


def test_capability_profile_error_is_a_capability_manifest_error():
    """Callers that already catch CapabilityManifestError (existing
    get/set_capability_manifest handlers) keep working if extended to cover
    profiles too — CapabilityProfileError subclasses it."""
    assert issubclass(cp.CapabilityProfileError, cm.CapabilityManifestError)


def test_normalize_scope_id_rejects_empty():
    with pytest.raises(cp.CapabilityProfileError, match="scope_id"):
        cp.normalize_scope_id("")
    with pytest.raises(cp.CapabilityProfileError, match="scope_id"):
        cp.normalize_scope_id("   ")


def test_normalize_disabled_capability_ids_none_and_empty():
    assert cp.normalize_disabled_capability_ids(None) == []
    assert cp.normalize_disabled_capability_ids([]) == []


def test_normalize_disabled_capability_ids_dedupes_and_sorts():
    assert cp.normalize_disabled_capability_ids(["zebra", "alpha", "alpha"]) == ["alpha", "zebra"]


def test_normalize_disabled_capability_ids_rejects_non_list():
    with pytest.raises(cp.CapabilityProfileError, match="disabled_capability_ids"):
        cp.normalize_disabled_capability_ids("not-a-list")


def test_normalize_disabled_capability_ids_rejects_non_string_entries():
    with pytest.raises(cp.CapabilityProfileError, match="disabled_capability_ids"):
        cp.normalize_disabled_capability_ids([1, 2])


def test_normalize_provenance_none_and_valid_dict():
    assert cp.normalize_provenance(None) is None
    prov = {"source": "AGENTS.md", "observed_at": "2026-07-28T00:00:00Z"}
    assert cp.normalize_provenance(prov) == prov


def test_normalize_provenance_rejects_non_dict():
    with pytest.raises(cp.CapabilityProfileError, match="provenance must be an object"):
        cp.normalize_provenance("a string")


def test_normalize_provenance_rejects_secret_shaped_value():
    with pytest.raises(cp.CapabilityProfileError, match="secret-shaped"):
        cp.normalize_provenance({"source": "postgresql://user:hunter2@host/db"})


def test_normalize_provenance_rejects_machine_local_absolute_path():
    with pytest.raises(cp.CapabilityProfileError, match="machine-local absolute path"):
        cp.normalize_provenance({"source": r"C:\Users\adam\repo\config.toml"})


# ---------------------------------------------------------------------------
# capability_profile.merge_layers — the inheritance/override/disable algorithm.
# ---------------------------------------------------------------------------

def _layer(name, capabilities=None, disabled=None):
    return {"layer": name, "capabilities": capabilities or [], "disabled_capability_ids": disabled or []}


def test_merge_layers_empty():
    effective, sources, overrides, disabled_log = cp.merge_layers([])
    assert effective == []
    assert sources == {}
    assert overrides == []
    assert disabled_log == []


def test_merge_layers_single_layer_no_conflict():
    cap = cm.normalize_capability(_valid_capability())
    effective, sources, overrides, disabled_log = cp.merge_layers([_layer("project", [cap])])
    assert effective == [cap]
    assert sources == {"code-search": "project"}
    assert overrides == []
    assert disabled_log == []


def test_merge_layers_more_specific_layer_wins_and_is_flagged_conflict():
    workspace_cap = cm.normalize_capability(_valid_capability(required_tools=["grep"]))
    project_cap = cm.normalize_capability(_valid_capability(required_tools=["Serena: find_symbol"]))
    effective, sources, overrides, disabled_log = cp.merge_layers([
        _layer("workspace", [workspace_cap]),
        _layer("project", [project_cap]),
    ])
    assert effective == [project_cap]
    assert sources == {"code-search": "project"}
    assert len(overrides) == 1
    ov = overrides[0]
    assert ov["capability_id"] == "code-search"
    assert ov["from_layer"] == "workspace"
    assert ov["to_layer"] == "project"
    assert ov["conflict"] is True
    assert ov["previous"] == workspace_cap
    assert ov["new"] == project_cap
    assert disabled_log == []


def test_merge_layers_availability_policy_mismatch_is_also_a_conflict():
    workspace_cap = cm.normalize_capability(_valid_capability(availability_policy="required"))
    project_cap = cm.normalize_capability(_valid_capability(availability_policy="optional"))
    _, _, overrides, _ = cp.merge_layers([
        _layer("workspace", [workspace_cap]),
        _layer("project", [project_cap]),
    ])
    assert overrides[0]["conflict"] is True


def test_merge_layers_compatible_refinement_is_not_a_conflict():
    """Same required_tools/availability_policy, different fallback_chain —
    a compatible refinement, not a conflict."""
    workspace_cap = cm.normalize_capability(_valid_capability(fallback_chain=["grep"]))
    project_cap = cm.normalize_capability(_valid_capability(fallback_chain=["grep", "search_code_semantic"]))
    _, _, overrides, _ = cp.merge_layers([
        _layer("workspace", [workspace_cap]),
        _layer("project", [project_cap]),
    ])
    assert len(overrides) == 1
    assert overrides[0]["conflict"] is False


def test_merge_layers_identical_redeclaration_is_recorded_but_not_a_conflict():
    cap = cm.normalize_capability(_valid_capability())
    _, _, overrides, _ = cp.merge_layers([
        _layer("workspace", [cap]),
        _layer("project", [cap]),
    ])
    assert len(overrides) == 1
    assert overrides[0]["conflict"] is False


def test_merge_layers_disable_removes_inherited_capability():
    cap = cm.normalize_capability(_valid_capability())
    effective, sources, overrides, disabled_log = cp.merge_layers([
        _layer("workspace", [cap]),
        _layer("project", disabled=["code-search"]),
    ])
    assert effective == []
    assert sources == {}
    assert overrides == []
    assert len(disabled_log) == 1
    assert disabled_log[0] == {
        "capability_id": "code-search",
        "disabled_by_layer": "project",
        "previously_declared_by_layer": "workspace",
    }


def test_merge_layers_noop_disable_of_never_declared_id_is_not_logged():
    effective, sources, overrides, disabled_log = cp.merge_layers([
        _layer("project", disabled=["never-declared"]),
    ])
    assert effective == []
    assert disabled_log == []


def test_merge_layers_same_layer_can_disable_and_redeclare_same_id():
    """A layer that both disables an inherited id and declares its own
    capability for that id: the layer's own declaration wins (declaring is
    stronger intent than disabling)."""
    workspace_cap = cm.normalize_capability(_valid_capability(required_tools=["grep"]))
    project_cap = cm.normalize_capability(_valid_capability(required_tools=["Serena: find_symbol"]))
    effective, sources, _, _ = cp.merge_layers([
        _layer("workspace", [workspace_cap]),
        _layer("project", [project_cap], disabled=["code-search"]),
    ])
    assert effective == [project_cap]
    assert sources == {"code-search": "project"}


def test_merge_layers_sorted_by_id():
    zebra = cm.normalize_capability(_valid_capability(id="zebra"))
    alpha = cm.normalize_capability(_valid_capability(id="alpha"))
    effective, _, _, _ = cp.merge_layers([_layer("project", [zebra, alpha])])
    assert [c["id"] for c in effective] == ["alpha", "zebra"]


# ---------------------------------------------------------------------------
# DB layer — get/set/clear_capability_profile (single scope).
# ---------------------------------------------------------------------------

async def test_get_capability_profile_empty_for_new_scope(db):
    result = await db_module.get_capability_profile(db, "project", "some-project-id")
    assert result["capabilities"] == []
    assert result["disabled_capability_ids"] == []
    assert result["manifest_version"] == cm.MANIFEST_SCHEMA_VERSION
    assert result["provenance"] is None
    assert result["updated_at"] is None
    assert result["manifest_hash"] == cm.manifest_hash([])


async def test_get_capability_profile_rejects_bad_scope_type(db):
    with pytest.raises(cp.CapabilityProfileError):
        await db_module.get_capability_profile(db, "planet", "x")


async def test_set_capability_profile_round_trip(db):
    saved = await db_module.set_capability_profile(
        db, "project", "proj-1",
        capabilities=[_valid_capability(id="b"), _valid_capability(id="a")],
        disabled_capability_ids=["legacy-tool"],
        provenance={"source": "AGENTS.md"},
    )
    assert [c["id"] for c in saved["capabilities"]] == ["a", "b"]
    assert saved["disabled_capability_ids"] == ["legacy-tool"]
    assert saved["provenance"] == {"source": "AGENTS.md"}
    assert saved["updated_at"] is not None

    fetched = await db_module.get_capability_profile(db, "project", "proj-1")
    assert fetched["capabilities"] == saved["capabilities"]
    assert fetched["disabled_capability_ids"] == saved["disabled_capability_ids"]
    assert fetched["provenance"] == saved["provenance"]
    assert fetched["manifest_hash"] == saved["manifest_hash"]


async def test_set_capability_profile_overwrites_wholesale(db):
    await db_module.set_capability_profile(db, "project", "proj-2", capabilities=[_valid_capability(id="first")])
    second = await db_module.set_capability_profile(db, "project", "proj-2", capabilities=[_valid_capability(id="second")])
    assert [c["id"] for c in second["capabilities"]] == ["second"]


async def test_set_capability_profile_rejects_malformed_capability(db):
    with pytest.raises(cm.CapabilityManifestError):
        await db_module.set_capability_profile(
            db, "project", "proj-3",
            capabilities=[_valid_capability(availability_policy="bogus")],
        )
    fetched = await db_module.get_capability_profile(db, "project", "proj-3")
    assert fetched["capabilities"] == []


async def test_set_capability_profile_rejects_malformed_disabled_ids(db):
    with pytest.raises(cp.CapabilityProfileError):
        await db_module.set_capability_profile(db, "project", "proj-4", disabled_capability_ids=[1, 2])


async def test_set_capability_profile_rejects_unsafe_provenance(db):
    with pytest.raises(cp.CapabilityProfileError, match="secret-shaped"):
        await db_module.set_capability_profile(
            db, "project", "proj-5", provenance={"source": "postgresql://u:p@host/db"}
        )


async def test_clear_capability_profile_removes_row(db):
    await db_module.set_capability_profile(db, "item", "item-1", capabilities=[_valid_capability()])
    cleared = await db_module.clear_capability_profile(db, "item", "item-1")
    assert cleared["capabilities"] == []
    assert cleared["disabled_capability_ids"] == []
    assert cleared["updated_at"] is None


async def test_clear_capability_profile_idempotent_on_empty_scope(db):
    cleared = await db_module.clear_capability_profile(db, "item", "never-set-item")
    assert cleared["capabilities"] == []


# ---------------------------------------------------------------------------
# DB layer — get_effective_capability_profile (multi-layer resolution).
# ---------------------------------------------------------------------------

async def test_get_effective_capability_profile_unknown_project_raises(db):
    with pytest.raises(ValueError, match="unknown project"):
        await db_module.get_effective_capability_profile(db, "does-not-exist")


async def test_get_effective_capability_profile_project_layer_only(db):
    project = await db_module.create_project(db, "cap-profile-project-only")
    await db_module.set_capability_profile(
        db, "project", project["id"], capabilities=[_valid_capability()]
    )
    result = await db_module.get_effective_capability_profile(db, project["id"])
    assert [c["id"] for c in result["capabilities"]] == ["code-search"]
    assert result["capability_sources"] == {"code-search": "project"}
    assert result["layers_applied"] == ["project"]
    assert result["overrides"] == []
    assert result["disabled"] == []
    assert result["sprint_item_id"] is None
    assert result["sprint_version"] is None


async def test_get_effective_capability_profile_inheritance_order_project_overrides_workspace(db):
    project = await db_module.create_project(db, "cap-profile-inherit")
    await db_module.set_capability_profile(
        db, "workspace", "singleton",
        capabilities=[_valid_capability(required_tools=["grep"])],
    )
    await db_module.set_capability_profile(
        db, "project", project["id"],
        capabilities=[_valid_capability(required_tools=["Serena: find_symbol"])],
    )
    result = await db_module.get_effective_capability_profile(db, project["id"])
    assert result["capabilities"][0]["required_tools"] == ["Serena: find_symbol"]
    assert result["capability_sources"] == {"code-search": "project"}
    assert result["layers_applied"] == ["workspace", "project"]
    assert len(result["overrides"]) == 1
    assert result["overrides"][0]["conflict"] is True
    assert result["overrides"][0]["from_layer"] == "workspace"
    assert result["overrides"][0]["to_layer"] == "project"


async def test_get_effective_capability_profile_disable_semantics(db):
    project = await db_module.create_project(db, "cap-profile-disable")
    await db_module.set_capability_profile(
        db, "workspace", "singleton", capabilities=[_valid_capability()]
    )
    await db_module.set_capability_profile(
        db, "project", project["id"], disabled_capability_ids=["code-search"]
    )
    result = await db_module.get_effective_capability_profile(db, project["id"])
    assert result["capabilities"] == []
    assert result["disabled"] == [{
        "capability_id": "code-search",
        "disabled_by_layer": "project",
        "previously_declared_by_layer": "workspace",
    }]


async def test_get_effective_capability_profile_user_layer_only_when_scope_id_given(db):
    project = await db_module.create_project(db, "cap-profile-user")
    await db_module.set_capability_profile(
        db, "user", "alice", capabilities=[_valid_capability(id="user-cap")]
    )
    without_user = await db_module.get_effective_capability_profile(db, project["id"])
    assert without_user["capabilities"] == []

    with_user = await db_module.get_effective_capability_profile(
        db, project["id"], user_scope_id="alice"
    )
    assert [c["id"] for c in with_user["capabilities"]] == ["user-cap"]
    assert with_user["layers_applied"] == ["user"]


async def test_get_effective_capability_profile_item_and_sprint_version_layers(db):
    project = await db_module.create_project(db, "cap-profile-item")
    item = await db_module.add_sprint_item(db, project["id"], "v1", "Some sprint item")
    version_scope_id = f"{project['id']}:{item['version']}"

    await db_module.set_capability_profile(
        db, "project", project["id"],
        capabilities=[_valid_capability(required_tools=["grep"])],
    )
    await db_module.set_capability_profile(
        db, "sprint_version", version_scope_id,
        capabilities=[_valid_capability(required_tools=["search_code_semantic"])],
    )
    await db_module.set_capability_profile(
        db, "item", item["id"],
        capabilities=[_valid_capability(required_tools=["Serena: find_symbol"])],
    )

    result = await db_module.get_effective_capability_profile(db, project["id"], item["id"])
    assert result["sprint_version"] == item["version"]
    assert result["sprint_item_id"] == item["id"]
    assert result["capabilities"][0]["required_tools"] == ["Serena: find_symbol"]
    assert result["capability_sources"] == {"code-search": "item"}
    assert result["layers_applied"] == ["project", "sprint_version", "item"]
    # project -> sprint_version -> item: two successive overrides recorded.
    assert len(result["overrides"]) == 2
    assert [o["to_layer"] for o in result["overrides"]] == ["sprint_version", "item"]


async def test_get_effective_capability_profile_unknown_sprint_item_raises(db):
    project = await db_module.create_project(db, "cap-profile-bad-item")
    with pytest.raises(ValueError, match="unknown sprint item"):
        await db_module.get_effective_capability_profile(db, project["id"], "does-not-exist")


async def test_get_effective_capability_profile_item_from_other_project_raises(db):
    project_a = await db_module.create_project(db, "cap-profile-proj-a")
    project_b = await db_module.create_project(db, "cap-profile-proj-b")
    item = await db_module.add_sprint_item(db, project_b["id"], "v1", "Belongs to B")
    with pytest.raises(ValueError, match="does not belong to project"):
        await db_module.get_effective_capability_profile(db, project_a["id"], item["id"])


async def test_capability_profile_cross_backend_parity(anydb):
    """SQLite and Postgres persist and resolve the effective profile identically."""
    project = await db_module.create_project(anydb, "cap-profile-parity")
    await db_module.set_capability_profile(
        anydb, "workspace", "singleton", capabilities=[_valid_capability(required_tools=["grep"])]
    )
    await db_module.set_capability_profile(
        anydb, "project", project["id"],
        capabilities=[_valid_capability(required_tools=["Serena: find_symbol"])],
    )
    result = await db_module.get_effective_capability_profile(anydb, project["id"])
    assert [c["id"] for c in result["capabilities"]] == ["code-search"]
    assert result["capabilities"][0]["required_tools"] == ["Serena: find_symbol"]
    assert result["layers_applied"] == ["workspace", "project"]
    assert result["manifest_hash"] == cm.manifest_hash(result["capabilities"])


# ---------------------------------------------------------------------------
# MCP tool surface — registration + end-to-end dispatch.
# ---------------------------------------------------------------------------

def test_capability_profile_tools_registered():
    from meridian import mcp_tools

    names = {t["name"] for t in mcp_tools._MCP_TOOLS_LIST}
    assert "set_capability_profile" in names
    assert "clear_capability_profile" in names
    assert "get_effective_capability_profile" in names
    for tool_name in ("set_capability_profile", "clear_capability_profile", "get_effective_capability_profile"):
        assert mcp_tools._TOOL_CATEGORY[tool_name] == "config"


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


def test_mcp_set_capability_profile_then_get_effective_round_trip(client):
    pid = client.post("/projects", json={"name": "mcp-cap-profile-round-trip"}).json()["id"]
    set_result = _result(_mcp_call(client, "set_capability_profile", {
        "scope_type": "project",
        "scope_id": pid,
        "capabilities": [_valid_capability()],
    }))
    assert set_result["capabilities"][0]["id"] == "code-search"

    effective = _result(_mcp_call(client, "get_effective_capability_profile", {"project_id": pid}))
    assert effective["capabilities"] == set_result["capabilities"]
    assert effective["capability_sources"] == {"code-search": "project"}


def test_mcp_set_capability_profile_rejects_malformed_input(client):
    result = _result(_mcp_call(client, "set_capability_profile", {
        "scope_type": "project",
        "scope_id": "some-id",
        "capabilities": [{"id": "bad", "purpose": "missing required_tools"}],
    }))
    assert "error" in result
    assert "required_tools" in result["error"]


def test_mcp_set_capability_profile_rejects_bad_scope_type(client):
    result = _result(_mcp_call(client, "set_capability_profile", {
        "scope_type": "planet",
        "scope_id": "some-id",
    }))
    assert "error" in result


def test_mcp_set_capability_profile_requires_scope_fields(client):
    result = _result(_mcp_call(client, "set_capability_profile", {"scope_id": "x"}))
    assert "error" in result
    result2 = _result(_mcp_call(client, "set_capability_profile", {"scope_type": "project"}))
    assert "error" in result2


def test_mcp_clear_capability_profile_removes_layer(client):
    pid = client.post("/projects", json={"name": "mcp-cap-profile-clear"}).json()["id"]
    _mcp_call(client, "set_capability_profile", {
        "scope_type": "project", "scope_id": pid, "capabilities": [_valid_capability()],
    })
    cleared = _result(_mcp_call(client, "clear_capability_profile", {
        "scope_type": "project", "scope_id": pid,
    }))
    assert cleared["capabilities"] == []

    effective = _result(_mcp_call(client, "get_effective_capability_profile", {"project_id": pid}))
    assert effective["capabilities"] == []


def test_mcp_get_effective_capability_profile_requires_project_id(client):
    result = _result(_mcp_call(client, "get_effective_capability_profile", {}))
    assert "error" in result


def test_mcp_get_effective_capability_profile_unknown_project(client):
    result = _result(_mcp_call(client, "get_effective_capability_profile", {"project_id": "does-not-exist"}))
    assert "error" in result
