"""MCP parity checks for scoped member capabilities."""
from __future__ import annotations

import pytest

import meridian.server  # noqa: F401 -- load server before handler to avoid its import cycle
from meridian import db as db_module
from meridian import member_policy
from meridian.mcp import handler as mcp_handler
from meridian.mcp_tools import MCP_MEMBER_POLICY_OPERATIONS


PROJECT = "11111111-1111-4111-8111-111111111111"
PARENT = "22222222-2222-4222-8222-222222222222"
CHILD = "33333333-3333-4333-8333-333333333333"


def _scoped(projects: tuple[str, ...], *caps: str) -> member_policy.ScopedProjectIds:
    return member_policy.ScopedProjectIds(
        projects,
        policy=member_policy.EffectivePolicy(
            caps=frozenset(caps),
            role=member_policy.ROLE_MEMBER,
        ),
    )


async def _identity_resolver(db, args, scoped):
    return dict(args)


async def _allow_scope(name, args, db, scoped, **kwargs):
    return None


def _project_row(project_id: str, parent_id: str | None = None, children=()):
    return {
        "id": project_id,
        "name": project_id,
        "parent_project_id": parent_id,
        "parent_exists": parent_id is None or parent_id == PARENT,
        "status": "active",
        "child_ids": list(children),
    }


@pytest.mark.asyncio
async def test_direct_mcp_project_write_uses_the_attached_policy(monkeypatch, tmp_path):
    monkeypatch.setattr(mcp_handler, "_resolve_project_reference", _identity_resolver)
    monkeypatch.setattr(mcp_handler.scope_guard, "enforce_scoped_call", _allow_scope)
    scoped = _scoped((PROJECT,), member_policy.WRITE_WORKSPACE_LEVEL)

    with pytest.raises(member_policy.MemberPolicyDenied):
        await mcp_handler._dispatch_mcp_tool(
            "rename_project",
            {"project_id": PROJECT, "name": "renamed"},
            object(),
            str(tmp_path),
            scoped_project_ids=scoped,
        )


@pytest.mark.asyncio
async def test_hosted_plain_list_fails_closed_for_direct_mutation(monkeypatch, tmp_path):
    monkeypatch.setattr(member_policy, "_is_hosted", lambda: True)
    monkeypatch.setattr(mcp_handler, "_resolve_project_reference", _identity_resolver)
    monkeypatch.setattr(mcp_handler.scope_guard, "enforce_scoped_call", _allow_scope)

    with pytest.raises(member_policy.MemberPolicyUnavailable):
        await mcp_handler._dispatch_mcp_tool(
            "rename_project",
            {"project_id": PROJECT, "name": "renamed"},
            object(),
            str(tmp_path),
            scoped_project_ids=[PROJECT],
        )


@pytest.mark.asyncio
async def test_parent_change_uses_hierarchy_capabilities(monkeypatch, tmp_path):
    monkeypatch.setattr(mcp_handler, "_resolve_project_reference", _identity_resolver)
    monkeypatch.setattr(mcp_handler.scope_guard, "enforce_scoped_call", _allow_scope)
    rows = {
        PROJECT: _project_row(PROJECT),
        PARENT: _project_row(PARENT),
    }

    async def family(db, ids):
        return {project_id: rows[project_id] for project_id in ids}

    monkeypatch.setattr(db_module, "get_project_family", family)
    scoped = _scoped((PROJECT, PARENT), member_policy.WRITE_PROJECT)

    with pytest.raises(member_policy.MemberPolicyDenied):
        await mcp_handler._authorize_scoped_mcp_call(
            "set_parent_project",
            {"project_id": PROJECT, "parent_project_id": PARENT},
            object(),
            str(tmp_path),
            None,
            scoped,
        )


@pytest.mark.asyncio
async def test_bulk_delete_requires_the_union_of_target_hierarchy_caps(monkeypatch, tmp_path):
    monkeypatch.setattr(mcp_handler, "_resolve_project_reference", _identity_resolver)
    monkeypatch.setattr(mcp_handler.scope_guard, "enforce_scoped_call", _allow_scope)
    rows = {
        PARENT: _project_row(PARENT, children=(CHILD,)),
        CHILD: _project_row(CHILD, PARENT),
    }

    async def family(db, ids):
        return {project_id: rows[project_id] for project_id in ids}

    monkeypatch.setattr(db_module, "get_project_family", family)
    scoped = _scoped(
        (PARENT, CHILD),
        member_policy.WRITE_PROJECT,
        member_policy.DELETE_PROJECT,
    )

    with pytest.raises(member_policy.MemberPolicyDenied):
        await mcp_handler._authorize_scoped_mcp_call(
            "delete_projects_batch",
            {"project_ids": [PARENT, CHILD]},
            object(),
            str(tmp_path),
            None,
            scoped,
        )


@pytest.mark.asyncio
async def test_bulk_project_ids_are_checked_against_member_scope(monkeypatch, tmp_path):
    monkeypatch.setattr(mcp_handler, "_resolve_project_reference", _identity_resolver)
    monkeypatch.setattr(mcp_handler.scope_guard, "enforce_scoped_call", _allow_scope)

    async def should_not_load_family(db, ids):
        raise AssertionError("scope refusal must precede hierarchy lookup")

    monkeypatch.setattr(db_module, "get_project_family", should_not_load_family)
    scoped = _scoped(
        (PARENT,),
        member_policy.WRITE_PROJECT,
        member_policy.DELETE_PROJECT,
        member_policy.DELETE_SUBPROJECT,
    )

    with pytest.raises(ValueError, match="outside your access scope"):
        await mcp_handler._authorize_scoped_mcp_call(
            "delete_projects_batch",
            {"project_ids": [PARENT, CHILD]},
            object(),
            str(tmp_path),
            None,
            scoped,
        )


@pytest.mark.asyncio
async def test_workspace_write_requires_workspace_capability(monkeypatch, tmp_path):
    monkeypatch.setattr(mcp_handler, "_resolve_project_reference", _identity_resolver)
    monkeypatch.setattr(mcp_handler.scope_guard, "enforce_scoped_call", _allow_scope)
    scoped = _scoped((PROJECT,), member_policy.WRITE_PROJECT)

    with pytest.raises(member_policy.MemberPolicyDenied):
        await mcp_handler._authorize_scoped_mcp_call(
            "add_workspace_note",
            {"title": "shared", "body": "workspace note"},
            object(),
            str(tmp_path),
            None,
            scoped,
        )


@pytest.mark.asyncio
async def test_merge_and_archive_use_catalog_helpers(monkeypatch, tmp_path):
    monkeypatch.setattr(mcp_handler, "_resolve_project_reference", _identity_resolver)
    monkeypatch.setattr(mcp_handler.scope_guard, "enforce_scoped_call", _allow_scope)
    rows = {
        PARENT: _project_row(PARENT),
        CHILD: _project_row(CHILD, PARENT),
    }

    async def family(db, ids):
        return {project_id: rows[project_id] for project_id in ids}

    monkeypatch.setattr(db_module, "get_project_family", family)
    scoped = _scoped((PARENT, CHILD), member_policy.WRITE_PROJECT)
    for name, args in (
        ("merge_project", {"source_project_id": CHILD, "target_project_id": PARENT}),
        ("archive_project", {"project_id": PARENT}),
    ):
        with pytest.raises(member_policy.MemberPolicyDenied):
            await mcp_handler._authorize_scoped_mcp_call(
                name, args, object(), str(tmp_path), None, scoped,
            )


@pytest.mark.asyncio
async def test_tunnel_scope_guard_runs_before_forwarding(monkeypatch, tmp_path):
    monkeypatch.setattr(mcp_handler, "_resolve_project_reference", _identity_resolver)
    seen = []

    async def deny_scope(name, args, db, scoped, **kwargs):
        seen.append((name, args))
        raise ValueError("project is outside your access scope")

    monkeypatch.setattr(mcp_handler.scope_guard, "enforce_scoped_call", deny_scope)
    monkeypatch.setattr(mcp_handler, "_meridian_tool_names", lambda: frozenset())
    result = await mcp_handler._handle_mcp_request(
        {
            "id": 7,
            "method": "tools/call",
            "params": {
                "name": "plugin__update_project",
                "arguments": {"project_id": PROJECT},
            },
        },
        object(),
        str(tmp_path),
        tenant={"id": "tenant"},
        scoped_project_ids=_scoped((PROJECT,), member_policy.WRITE_PROJECT),
    )

    assert seen and result["error"]["message"] == "project is outside your access scope"


@pytest.mark.asyncio
async def test_policy_lookup_failure_returns_service_unavailable_before_tunnel(monkeypatch, tmp_path):
    monkeypatch.setattr(mcp_handler, "_resolve_project_reference", _identity_resolver)
    monkeypatch.setattr(mcp_handler.scope_guard, "enforce_scoped_call", _allow_scope)

    async def failed_family(db, ids):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(db_module, "get_project_family", failed_family)
    monkeypatch.setattr(mcp_handler, "_meridian_tool_names", lambda: frozenset())
    result = await mcp_handler._handle_mcp_request(
        {
            "id": 8,
            "method": "tools/call",
            "params": {
                "name": "delete_projects_batch",
                "arguments": {"project_ids": [PROJECT]},
            },
        },
        object(),
        str(tmp_path),
        tenant={"id": "tenant"},
        scoped_project_ids=_scoped((PROJECT,), member_policy.WRITE_PROJECT),
    )

    assert result["error"]["code"] == -32000
    assert result["error"]["data"]["status_code"] == 503
    assert result["error"]["data"]["code"] == "MEMBER_POLICY_UNAVAILABLE"


@pytest.mark.asyncio
async def test_tunnel_archive_alias_keeps_archive_capability_requirements(monkeypatch, tmp_path):
    monkeypatch.setattr(mcp_handler, "_resolve_project_reference", _identity_resolver)
    monkeypatch.setattr(mcp_handler.scope_guard, "enforce_scoped_call", _allow_scope)

    async def family(db, ids):
        return {PROJECT: _project_row(PROJECT)}

    monkeypatch.setattr(db_module, "get_project_family", family)
    scoped = _scoped((PROJECT,), member_policy.WRITE_PROJECT)

    with pytest.raises(member_policy.MemberPolicyDenied):
        await mcp_handler._authorize_scoped_mcp_call(
            "tenant__archive_project_alias",
            {"project_id": PROJECT},
            object(),
            str(tmp_path),
            None,
            scoped,
            tunnel_candidate=True,
        )


@pytest.mark.asyncio
async def test_tunnel_status_id_alias_stays_inside_member_scope(monkeypatch, tmp_path):
    monkeypatch.setattr(mcp_handler, "_resolve_project_reference", _identity_resolver)
    monkeypatch.setattr(mcp_handler.scope_guard, "enforce_scoped_call", _allow_scope)

    async def should_not_load_family(db, ids):
        raise AssertionError("out-of-scope id must be refused before policy lookup")

    monkeypatch.setattr(db_module, "get_project_family", should_not_load_family)

    with pytest.raises(ValueError, match="outside your access scope"):
        await mcp_handler._authorize_scoped_mcp_call(
            "tenant__set_project_status",
            {"id": CHILD, "status": "archived"},
            object(),
            str(tmp_path),
            None,
            _scoped((PROJECT,), member_policy.WRITE_PROJECT, member_policy.DELETE_PROJECT),
            tunnel_candidate=True,
        )


def test_mcp_policy_registry_covers_project_hierarchy_operations():
    assert MCP_MEMBER_POLICY_OPERATIONS["create_project"] == "project_create"
    assert MCP_MEMBER_POLICY_OPERATIONS["set_parent_project"] == "project_reparent"
    assert MCP_MEMBER_POLICY_OPERATIONS["merge_project"] == "project_merge"
    assert MCP_MEMBER_POLICY_OPERATIONS["delete_projects_batch"] == "project_delete"
    assert MCP_MEMBER_POLICY_OPERATIONS["archive_project"] == "project_status"
