"""Tests for sprint item 0bec79a7 (PROFILE-5) — the MCP-tool + REST-route
surface over the already-built layered hosted_default/workspace/user/
project/session profile contract (meridian.profile_contract,
meridian.db.profile_layers — see tests/test_profile_layers.py for the
storage/resolution layer itself, which is unchanged by this item).

Covers:

1. meridian.db.profile_layers — the two new helpers this item adds:
   list_profile_layers (read-only enumeration, optional scope_type filter)
   and clone_profile_layer (copy fields/reset_fields/provenance through the
   same validation path as any other write; rejects cloning from an empty
   source).
2. meridian.mcp.handlers.project_tools — the 8 new MCP handlers
   (list/get/save/clone/activate/reset_profile_layer,
   get_profile_layer_revisions, get_effective_profile), invoked directly
   the same way tests/test_core.py invokes handle_start_session: await
   handle_xxx(args, db=db, data_dir=..., tenant=None, _mcp_tenant_id=None).
3. meridian/routes/settings.py — the new top-level REST router, exercised
   via the shared ``client`` TestClient fixture.
"""
from __future__ import annotations

import pytest

from meridian import db as db_module
from meridian import profile_contract as pc


# ---------------------------------------------------------------------------
# meridian.db.profile_layers — list_profile_layers / clone_profile_layer.
# ---------------------------------------------------------------------------

async def test_list_profile_layers_empty_table_returns_empty_list(db):
    assert await db_module.list_profile_layers(db) == []


async def test_list_profile_layers_returns_every_persisted_row(db):
    await db_module.set_profile_layer(db, "workspace", "singleton", fields={"auto_worktrees": 1})
    await db_module.set_profile_layer(db, "hosted_default", "global", fields={"max_pinned_decisions": 30})
    layers = await db_module.list_profile_layers(db)
    scopes = {(layer["scope_type"], layer["scope_id"]) for layer in layers}
    assert scopes == {("workspace", "singleton"), ("hosted_default", "global")}
    # deterministic ordering: scope_type, scope_id.
    assert [layer["scope_type"] for layer in layers] == ["hosted_default", "workspace"]


async def test_list_profile_layers_filters_by_scope_type(db):
    await db_module.set_profile_layer(db, "workspace", "singleton", fields={"auto_worktrees": 1})
    await db_module.set_profile_layer(db, "user", "alice", fields={"max_pinned_decisions": 15})
    layers = await db_module.list_profile_layers(db, "user")
    assert len(layers) == 1
    assert layers[0]["scope_type"] == "user"
    assert layers[0]["scope_id"] == "alice"


async def test_list_profile_layers_rejects_bad_scope_type(db):
    with pytest.raises(pc.ProfileContractError):
        await db_module.list_profile_layers(db, "planet")


async def test_clone_profile_layer_copies_fields_through_validation_path(db):
    await db_module.set_profile_layer(
        db, "hosted_default", "global",
        fields={"tool_priority_map": {"code_search": "grep"}},
        reset_fields=[], provenance={"source": "AGENTS.md"},
    )
    cloned = await db_module.clone_profile_layer(
        db, "hosted_default", "global", "hosted_default", "global-v2",
    )
    assert cloned["fields"] == {"tool_priority_map": {"code_search": "grep"}}
    assert cloned["provenance"] == {"source": "AGENTS.md"}
    assert cloned["revision"] == 1
    # clone target is independent of the source scope_id.
    source_still = await db_module.get_profile_layer(db, "hosted_default", "global")
    assert source_still["revision"] == 1


async def test_clone_profile_layer_into_hosted_default_lands_in_draft_not_source_lifecycle(db):
    await db_module.set_profile_layer(db, "hosted_default", "global", fields={"max_pinned_decisions": 20})
    await db_module.transition_hosted_default_lifecycle(db, "global", "active")
    cloned = await db_module.clone_profile_layer(
        db, "hosted_default", "global", "hosted_default", "global-clone",
    )
    # source is "active"; a fresh clone must NOT inherit that -- it starts
    # in "draft" exactly like any other first-ever hosted_default write.
    assert cloned["lifecycle_state"] == "draft"


async def test_clone_profile_layer_rejects_empty_source(db):
    with pytest.raises(pc.ProfileContractError, match="does not exist"):
        await db_module.clone_profile_layer(db, "workspace", "never-set", "workspace", "target")


async def test_clone_profile_layer_across_scope_types_respects_target_allowed_layers(db):
    # session-scoped executor_config.repo_path is allowed at session/project
    # but not workspace -- cloning it onto a workspace target must still be
    # rejected, same as a direct set_profile_layer write would be.
    await db_module.set_profile_layer(
        db, "session", "sess-1", fields={"executor_config.repo_path": "C:\\repo"},
    )
    with pytest.raises(pc.ProfileContractError, match="not writable at layer"):
        await db_module.clone_profile_layer(db, "session", "sess-1", "workspace", "singleton")


# ---------------------------------------------------------------------------
# MCP handlers — meridian.mcp.handlers.project_tools.
# ---------------------------------------------------------------------------

async def test_handle_list_profile_layers_happy_path(db):
    from meridian.mcp.handlers.project_tools import handle_list_profile_layers

    await db_module.set_profile_layer(db, "workspace", "singleton", fields={"auto_worktrees": 1})
    await db_module.set_profile_layer(db, "user", "alice", fields={"max_pinned_decisions": 10})

    result = await handle_list_profile_layers(
        {}, db=db, data_dir="/tmp", tenant=None, _mcp_tenant_id=None,
    )
    assert len(result) == 2

    filtered = await handle_list_profile_layers(
        {"scope_type": "user"}, db=db, data_dir="/tmp", tenant=None, _mcp_tenant_id=None,
    )
    assert len(filtered) == 1
    assert filtered[0]["scope_type"] == "user"


async def test_handle_get_profile_layer_happy_path_and_missing_args(db):
    from meridian.mcp.handlers.project_tools import handle_get_profile_layer

    await db_module.set_profile_layer(db, "hosted_default", "global", fields={"max_pinned_decisions": 25})
    ok = await handle_get_profile_layer(
        {"scope_type": "hosted_default", "scope_id": "global"},
        db=db, data_dir="/tmp", tenant=None, _mcp_tenant_id=None,
    )
    assert ok["fields"]["max_pinned_decisions"] == 25

    missing_type = await handle_get_profile_layer(
        {"scope_id": "global"}, db=db, data_dir="/tmp", tenant=None, _mcp_tenant_id=None,
    )
    assert "error" in missing_type

    missing_id = await handle_get_profile_layer(
        {"scope_type": "hosted_default"}, db=db, data_dir="/tmp", tenant=None, _mcp_tenant_id=None,
    )
    assert "error" in missing_id


async def test_handle_save_profile_layer_happy_path(db):
    from meridian.mcp.handlers.project_tools import handle_save_profile_layer

    result = await handle_save_profile_layer(
        {
            "scope_type": "workspace", "scope_id": "singleton",
            "fields": {"tool_priority_map": {"docs": "meridian-docs"}},
            "provenance": {"source": "test"},
        },
        db=db, data_dir="/tmp", tenant=None, _mcp_tenant_id=None,
    )
    assert result["fields"] == {"tool_priority_map": {"docs": "meridian-docs"}}
    assert result["revision"] == 1


async def test_handle_save_profile_layer_missing_args(db):
    from meridian.mcp.handlers.project_tools import handle_save_profile_layer

    missing_type = await handle_save_profile_layer(
        {"scope_id": "singleton"}, db=db, data_dir="/tmp", tenant=None, _mcp_tenant_id=None,
    )
    assert "error" in missing_type

    missing_id = await handle_save_profile_layer(
        {"scope_type": "workspace"}, db=db, data_dir="/tmp", tenant=None, _mcp_tenant_id=None,
    )
    assert "error" in missing_id


async def test_handle_save_profile_layer_stale_revision_returns_structured_error(db):
    from meridian.mcp.handlers.project_tools import handle_save_profile_layer

    await handle_save_profile_layer(
        {"scope_type": "workspace", "scope_id": "singleton", "fields": {"auto_worktrees": 1}},
        db=db, data_dir="/tmp", tenant=None, _mcp_tenant_id=None,
    )
    result = await handle_save_profile_layer(
        {
            "scope_type": "workspace", "scope_id": "singleton",
            "fields": {"auto_worktrees": 0}, "expected_revision": 99,
        },
        db=db, data_dir="/tmp", tenant=None, _mcp_tenant_id=None,
    )
    assert result["code"] == "STALE_REVISION"
    assert result["current_revision"] == 1
    assert "error" in result


async def test_handle_clone_profile_layer_happy_path(db):
    from meridian.mcp.handlers.project_tools import handle_clone_profile_layer

    await db_module.set_profile_layer(db, "hosted_default", "global", fields={"max_pinned_decisions": 40})
    result = await handle_clone_profile_layer(
        {
            "source_scope_type": "hosted_default", "source_scope_id": "global",
            "target_scope_type": "hosted_default", "target_scope_id": "global-v2",
        },
        db=db, data_dir="/tmp", tenant=None, _mcp_tenant_id=None,
    )
    assert result["fields"]["max_pinned_decisions"] == 40
    assert result["lifecycle_state"] == "draft"


async def test_handle_clone_profile_layer_rejects_empty_source(db):
    from meridian.mcp.handlers.project_tools import handle_clone_profile_layer

    result = await handle_clone_profile_layer(
        {
            "source_scope_type": "workspace", "source_scope_id": "never-set",
            "target_scope_type": "workspace", "target_scope_id": "target",
        },
        db=db, data_dir="/tmp", tenant=None, _mcp_tenant_id=None,
    )
    assert "error" in result


async def test_handle_clone_profile_layer_missing_args(db):
    from meridian.mcp.handlers.project_tools import handle_clone_profile_layer

    result = await handle_clone_profile_layer(
        {"source_scope_type": "workspace", "source_scope_id": "x"},
        db=db, data_dir="/tmp", tenant=None, _mcp_tenant_id=None,
    )
    assert "error" in result


async def test_handle_activate_profile_layer_happy_path(db):
    from meridian.mcp.handlers.project_tools import handle_activate_profile_layer

    await db_module.set_profile_layer(db, "hosted_default", "global", fields={"max_pinned_decisions": 20})
    result = await handle_activate_profile_layer(
        {"scope_id": "global"}, db=db, data_dir="/tmp", tenant=None, _mcp_tenant_id=None,
    )
    assert result["lifecycle_state"] == "active"


async def test_handle_activate_profile_layer_missing_scope_id(db):
    from meridian.mcp.handlers.project_tools import handle_activate_profile_layer

    result = await handle_activate_profile_layer(
        {}, db=db, data_dir="/tmp", tenant=None, _mcp_tenant_id=None,
    )
    assert "error" in result


async def test_handle_reset_profile_layer_happy_path(db):
    from meridian.mcp.handlers.project_tools import handle_reset_profile_layer

    await db_module.set_profile_layer(db, "user", "alice", fields={"max_pinned_decisions": 15})
    result = await handle_reset_profile_layer(
        {"scope_type": "user", "scope_id": "alice"},
        db=db, data_dir="/tmp", tenant=None, _mcp_tenant_id=None,
    )
    assert result["fields"] == {}
    assert result["revision"] == 0


async def test_handle_reset_profile_layer_missing_args(db):
    from meridian.mcp.handlers.project_tools import handle_reset_profile_layer

    result = await handle_reset_profile_layer(
        {"scope_type": "user"}, db=db, data_dir="/tmp", tenant=None, _mcp_tenant_id=None,
    )
    assert "error" in result


async def test_handle_get_profile_layer_revisions_happy_path(db):
    from meridian.mcp.handlers.project_tools import handle_get_profile_layer_revisions

    await db_module.set_profile_layer(db, "hosted_default", "global", fields={"max_pinned_decisions": 20})
    await db_module.transition_hosted_default_lifecycle(db, "global", "active")
    result = await handle_get_profile_layer_revisions(
        {"scope_id": "global"}, db=db, data_dir="/tmp", tenant=None, _mcp_tenant_id=None,
    )
    assert [r["revision"] for r in result] == [2, 1]

    limited = await handle_get_profile_layer_revisions(
        {"scope_id": "global", "limit": 1}, db=db, data_dir="/tmp", tenant=None, _mcp_tenant_id=None,
    )
    assert len(limited) == 1


async def test_handle_get_profile_layer_revisions_missing_scope_id(db):
    from meridian.mcp.handlers.project_tools import handle_get_profile_layer_revisions

    result = await handle_get_profile_layer_revisions(
        {}, db=db, data_dir="/tmp", tenant=None, _mcp_tenant_id=None,
    )
    assert "error" in result


async def test_handle_get_effective_profile_happy_path(db):
    from meridian.mcp.handlers.project_tools import handle_get_effective_profile

    project = await db_module.create_project(db, "profile5-effective-happy")
    result = await handle_get_effective_profile(
        {"project_id": project["id"]}, db=db, data_dir="/tmp", tenant=None, _mcp_tenant_id=None,
    )
    assert result["project_id"] == project["id"]
    assert result["fields"]["max_pinned_decisions"] == 20


async def test_handle_get_effective_profile_missing_project_id(db):
    from meridian.mcp.handlers.project_tools import handle_get_effective_profile

    result = await handle_get_effective_profile(
        {}, db=db, data_dir="/tmp", tenant=None, _mcp_tenant_id=None,
    )
    assert "error" in result


async def test_handle_get_effective_profile_unknown_project_id(db):
    from meridian.mcp.handlers.project_tools import handle_get_effective_profile

    result = await handle_get_effective_profile(
        {"project_id": "does-not-exist"}, db=db, data_dir="/tmp", tenant=None, _mcp_tenant_id=None,
    )
    assert "error" in result


async def test_handle_get_effective_profile_resolves_project_name(db):
    from meridian.mcp.handlers.project_tools import handle_get_effective_profile

    project = await db_module.create_project(db, "profile5-effective-by-name")
    result = await handle_get_effective_profile(
        {"project_name": "profile5-effective-by-name"},
        db=db, data_dir="/tmp", tenant=None, _mcp_tenant_id=None,
    )
    assert result["project_id"] == project["id"]


# ---------------------------------------------------------------------------
# REST routes — meridian/routes/settings.py, via the shared `client` fixture.
# ---------------------------------------------------------------------------

def test_rest_profile_layers_save_get_list_reset_round_trip(client):
    r = client.put(
        "/profile-layers/workspace/singleton",
        json={"fields": {"auto_worktrees": 1}},
    )
    assert r.status_code == 200
    saved = r.json()
    assert saved["fields"] == {"auto_worktrees": 1}
    assert saved["revision"] == 1

    r = client.get("/profile-layers/workspace/singleton")
    assert r.status_code == 200
    assert r.json()["fields"] == {"auto_worktrees": 1}

    r = client.get("/profile-layers")
    assert r.status_code == 200
    assert any(
        layer["scope_type"] == "workspace" and layer["scope_id"] == "singleton"
        for layer in r.json()
    )

    r = client.get("/profile-layers", params={"scope_type": "workspace"})
    assert r.status_code == 200
    assert all(layer["scope_type"] == "workspace" for layer in r.json())

    r = client.delete("/profile-layers/workspace/singleton")
    assert r.status_code == 200  # 200, not 204 -- returns the post-reset empty-layer dict
    assert r.json()["fields"] == {}


def test_rest_save_profile_layer_stale_revision_returns_409(client):
    client.put("/profile-layers/workspace/singleton", json={"fields": {"auto_worktrees": 1}})
    r = client.put(
        "/profile-layers/workspace/singleton",
        json={"fields": {"auto_worktrees": 0}, "expected_revision": 99},
    )
    assert r.status_code == 409


def test_rest_profile_layer_clone(client):
    client.put(
        "/profile-layers/hosted_default/global",
        json={"fields": {"max_pinned_decisions": 40}},
    )
    r = client.post(
        "/profile-layers/hosted_default/global/clone",
        json={"target_scope_type": "hosted_default", "target_scope_id": "global-v2"},
    )
    assert r.status_code == 200
    cloned = r.json()
    assert cloned["fields"]["max_pinned_decisions"] == 40
    assert cloned["lifecycle_state"] == "draft"


def test_rest_profile_layer_clone_missing_target_returns_400(client):
    client.put("/profile-layers/hosted_default/global", json={"fields": {"max_pinned_decisions": 40}})
    r = client.post("/profile-layers/hosted_default/global/clone", json={})
    assert r.status_code == 400


def test_rest_profile_layer_clone_empty_source_returns_400(client):
    r = client.post(
        "/profile-layers/workspace/never-set/clone",
        json={"target_scope_type": "workspace", "target_scope_id": "target"},
    )
    assert r.status_code == 400


def test_rest_profile_layer_activate(client):
    client.put("/profile-layers/hosted_default/global", json={"fields": {"max_pinned_decisions": 20}})
    r = client.post("/profile-layers/global/activate", json={})
    assert r.status_code == 200
    assert r.json()["lifecycle_state"] == "active"


def test_rest_profile_layer_revisions_not_shadowed_by_get_scope_route(client):
    """Regression coverage for the routing-order fix: GET
    /profile-layers/{scope_id}/revisions must resolve to the revisions
    route, not be swallowed by GET /profile-layers/{scope_type}/{scope_id}
    treating 'revisions' as a literal scope_id."""
    client.put("/profile-layers/hosted_default/global", json={"fields": {"max_pinned_decisions": 20}})
    client.post("/profile-layers/global/activate", json={})
    r = client.get("/profile-layers/global/revisions")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list)
    assert [row["revision"] for row in body] == [2, 1]


def test_rest_effective_profile_route(client):
    project = client.post("/projects", json={"name": "profile5-rest-effective"}).json()
    r = client.get(f"/projects/{project['id']}/effective-profile")
    assert r.status_code == 200
    body = r.json()
    assert body["project_id"] == project["id"]
    assert body["fields"]["max_pinned_decisions"] == 20


def test_rest_effective_profile_route_unknown_project_returns_404(client):
    r = client.get("/projects/does-not-exist/effective-profile")
    assert r.status_code == 404
