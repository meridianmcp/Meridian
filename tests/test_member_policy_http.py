"""Hosted HTTP enforcement for project-scoped member capabilities."""
from __future__ import annotations

import asyncio
import importlib
from datetime import datetime, timezone

import pytest

import meridian.server  # noqa: F401
from meridian import db as db_module


def _hosted_client(monkeypatch, tmp_path):
    monkeypatch.setenv("MERIDIAN_HOSTED", "true")
    monkeypatch.setenv("MERIDIAN_DB", ":memory:")
    monkeypatch.setenv("MERIDIAN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MERIDIAN_DB_URL", "")
    monkeypatch.setenv("MERIDIAN_DEMO_DB_URL", "")
    monkeypatch.setenv("MERIDIAN_AUTH_DB", "")
    monkeypatch.setenv("MERIDIAN_SKIP_DEMO", "1")
    monkeypatch.setenv("MERIDIAN_GOAL_MD", str(tmp_path / "GOAL.md"))
    monkeypatch.setenv("MERIDIAN_MD_ROOT", str(tmp_path))
    from fastapi.testclient import TestClient
    import meridian.server as server_module

    return TestClient(importlib.reload(server_module).app)


async def _seed_workspace(db):
    owner = await db_module.upsert_tenant(db, "member-http-owner@example.com")
    await db.execute("UPDATE tenants SET plan='admin' WHERE id=?", (owner["id"],))
    member = await db_module.upsert_tenant(db, "member-http-scoped@example.com")
    # The request path exercises member policy, not the separate free-tier
    # project-count entitlement. Give this test identity the paid entitlement
    # so the second project creation can reach the scoped capability check.
    await db.execute("UPDATE tenants SET plan='pro' WHERE id=?", (member["id"],))
    owner_token, _ = await db_module.create_api_token(db, owner["id"])
    member_token, _ = await db_module.create_api_token(db, member["id"])
    parent = (await db_module.create_project(db, "member-http-project"))["id"]
    other = (await db_module.create_project(db, "member-http-other"))["id"]
    foreign = (await db_module.create_project(db, "member-http-foreign"))["id"]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    for row_id, project_id in (("member-http-row", parent), ("member-http-row-2", other)):
        await db.execute(
            "INSERT INTO workspace_members "
            "(id, tenant_id, email, role, github_access, joined_at, project_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (row_id, owner["id"], member["email"], "admin", "none", now, project_id),
        )
    await db.commit()
    return {
        "owner_id": owner["id"], "owner_token": owner_token,
        "member_token": member_token, "project": parent, "other": other,
        "foreign": foreign,
    }


async def _seed_session_and_task(db, project_id):
    session = await db_module.register_session(db, project_id, "member-http-session")
    task = await db_module.log_task(
        db, session["id"], project_id, "member-http-task", status="done",
    )
    return session, task


async def _set_member_policy(db, owner_id, policy):
    await db_module.set_member_policy_override(
        db, owner_id, "member-http-scoped@example.com", policy,
        "member-policy-http-test",
    )


def test_scoped_http_create_delete_archive_and_bulk_paths_apply_capabilities(monkeypatch, tmp_path):
    with _hosted_client(monkeypatch, tmp_path) as client:
        setup = asyncio.run(_seed_workspace(client.app.state.db))
        member = {
            "Authorization": f"Bearer {setup['member_token']}",
            "X-Workspace-Tenant-Id": setup["owner_id"],
        }

        # Built-in create_subproject permits a child; top-level creation is a
        # workspace-level write and remains denied by default.
        child = client.post(
            "/projects", headers=member,
            json={"name": "member-http-child", "parent_project_id": setup["project"]},
        )
        assert child.status_code == 201, child.text
        denied_top_level = client.post(
            "/projects", headers=member, json={"name": "member-http-top"},
        )
        assert denied_top_level.status_code == 403, denied_top_level.text
        malformed_parent = client.post(
            "/projects", headers=member,
            json={"name": "member-http-malformed-parent", "parent_project_id": "not-a-project-id"},
        )
        assert malformed_parent.status_code == 403, malformed_parent.text

        # Re-parenting a granted top-level project also needs delete_project;
        # detaching the child needs delete_subproject + workspace-level write.
        denied_nest = client.post(
            f"/projects/{setup['other']}/parent", headers=member,
            json={"parent_project_id": setup["project"]},
        )
        assert denied_nest.status_code == 403, denied_nest.text
        denied_detach = client.post(
            f"/projects/{child.json()['id']}/parent", headers=member,
            json={"parent_project_id": None},
        )
        assert denied_detach.status_code == 403, denied_detach.text

        # Project lifecycle operations are denied by built-in defaults even
        # though this ADMIN member may otherwise write inside the granted project.
        denied_archive = client.patch(
            f"/projects/{setup['project']}/organization", headers=member,
            json={"status": "archived"},
        )
        assert denied_archive.status_code == 403, denied_archive.text
        async def _archive_for_restore_check():
            db = client.app.state.db
            await db.execute(
                "UPDATE projects SET status='archived' WHERE id=?", (setup["other"],),
            )
            await db.commit()

        asyncio.run(_archive_for_restore_check())
        denied_restore = client.patch(
            f"/projects/{setup['other']}/organization", headers=member,
            json={"status": "active"},
        )
        assert denied_restore.status_code == 403, denied_restore.text
        denied_delete = client.delete(f"/projects/{setup['project']}", headers=member)
        assert denied_delete.status_code == 403, denied_delete.text

        # Every ID in a bulk delete is scope-checked before a project lookup.
        denied_batch = client.delete(
            "/projects", headers=member,
            params=[("project_id", setup["project"]), ("project_id", setup["foreign"])],
        )
        assert denied_batch.status_code == 403, denied_batch.text


def test_member_policy_lookup_errors_fail_closed_with_http_503(monkeypatch, tmp_path):
    with _hosted_client(monkeypatch, tmp_path) as client:
        setup = asyncio.run(_seed_workspace(client.app.state.db))
        member = {
            "Authorization": f"Bearer {setup['member_token']}",
            "X-Workspace-Tenant-Id": setup["owner_id"],
        }
        from meridian import member_policy

        async def unavailable(*_args, **_kwargs):
            raise RuntimeError("auth db temporarily unavailable")

        monkeypatch.setattr(member_policy, "resolve_member_scope", unavailable)
        response = client.delete(f"/projects/{setup['project']}", headers=member)
        assert response.status_code == 503, response.text


def test_task_and_session_mutations_require_write_project(monkeypatch, tmp_path):
    with _hosted_client(monkeypatch, tmp_path) as client:
        setup = asyncio.run(_seed_workspace(client.app.state.db))
        session, task = asyncio.run(
            _seed_session_and_task(client.app.state.db, setup["project"])
        )
        member = {
            "Authorization": f"Bearer {setup['member_token']}",
            "X-Workspace-Tenant-Id": setup["owner_id"],
        }
        asyncio.run(_set_member_policy(
            client.app.state.db, setup["owner_id"], {"write_project": False},
        ))

        denied_create = client.post(
            "/tasks", headers=member,
            json={
                "project_id": setup["project"], "session_id": session["id"],
                "description": "must not be appended",
            },
        )
        denied_register = client.post(
            "/sessions/register", headers=member,
            json={"project_id": setup["project"], "name": "must not register"},
        )
        denied_patch_task = client.patch(
            f"/tasks/{task['id']}", headers=member, json={"status": "failed"},
        )
        denied_delete_task = client.delete(f"/tasks/{task['id']}", headers=member)
        denied_patch_session = client.patch(
            f"/sessions/{session['id']}", headers=member, json={"status": "idle"},
        )
        denied_close_session = client.post(
            f"/sessions/{session['id']}/close", headers=member,
        )
        denied_heartbeat = client.post(
            f"/sessions/{session['id']}/heartbeat", headers=member,
        )

        for response in (
            denied_create, denied_register, denied_patch_task, denied_delete_task,
            denied_patch_session, denied_close_session, denied_heartbeat,
        ):
            assert response.status_code == 403, response.text
            assert response.json().get("capability") == "write_project"

        saved = asyncio.run(db_module.get_task(client.app.state.db, task["id"]))
        assert saved["status"] == "done"


def test_task_session_and_profile_mutations_allow_explicit_capabilities(monkeypatch, tmp_path):
    with _hosted_client(monkeypatch, tmp_path) as client:
        setup = asyncio.run(_seed_workspace(client.app.state.db))
        session, task = asyncio.run(
            _seed_session_and_task(client.app.state.db, setup["project"])
        )
        member = {
            "Authorization": f"Bearer {setup['member_token']}",
            "X-Workspace-Tenant-Id": setup["owner_id"],
        }

        # The built-in project default allows task and session writes.
        patch_task = client.patch(
            f"/tasks/{task['id']}", headers=member, json={"status": "failed"},
        )
        assert patch_task.status_code == 200, patch_task.text
        patched_session = client.patch(
            f"/sessions/{session['id']}", headers=member, json={"status": "idle"},
        )
        assert patched_session.status_code == 200, patched_session.text
        created_task = client.post(
            "/tasks", headers=member,
            json={
                "project_id": setup["project"], "session_id": session["id"],
                "description": "allowed task write",
            },
        )
        assert created_task.status_code == 201, created_task.text

        # Workspace profile writes inherit the built-in deny; the explicit
        # workspace capability enables them while retaining project access.
        denied_workspace_profile = client.put(
            "/profile-layers/workspace/singleton", headers=member,
            json={"fields": {}},
        )
        assert denied_workspace_profile.status_code == 403
        assert denied_workspace_profile.json().get("capability") == "write_workspace_level"

        asyncio.run(_set_member_policy(
            client.app.state.db, setup["owner_id"], {
                "write_project": True, "write_workspace_level": True,
            },
        ))
        allowed_workspace_profile = client.put(
            "/profile-layers/workspace/singleton", headers=member,
            json={"fields": {}},
        )
        assert allowed_workspace_profile.status_code == 200, allowed_workspace_profile.text
        allowed_project_profile = client.put(
            f"/profile-layers/project/{setup['project']}", headers=member,
            json={"fields": {}},
        )
        assert allowed_project_profile.status_code == 200, allowed_project_profile.text


def test_malformed_empty_workspace_role_fails_closed_for_project_writes(
    monkeypatch, tmp_path,
):
    with _hosted_client(monkeypatch, tmp_path) as client:
        setup = asyncio.run(_seed_workspace(client.app.state.db))
        member = {
            "Authorization": f"Bearer {setup['member_token']}",
            "X-Workspace-Tenant-Id": setup["owner_id"],
        }

        async def _corrupt_member_role():
            await client.app.state.db.execute(
                "UPDATE workspace_members SET role='' WHERE tenant_id=? AND email=?",
                (setup["owner_id"], "member-http-scoped@example.com"),
            )
            await client.app.state.db.commit()

        asyncio.run(_corrupt_member_role())

        response = client.post(
            "/sessions/register", headers=member,
            json={"project_id": setup["project"], "name": "malformed-role-write"},
        )

        assert response.status_code == 403, response.text
        payload = response.json()
        assert payload["error"] == "forbidden"
        assert "viewer" in payload["message"]
        assert "cannot perform this action" in payload["message"]


def test_nonproject_member_policy_scope_is_checked_before_capability(monkeypatch, tmp_path):
    with _hosted_client(monkeypatch, tmp_path) as client:
        setup = asyncio.run(_seed_workspace(client.app.state.db))
        foreign_session, foreign_task = asyncio.run(
            _seed_session_and_task(client.app.state.db, setup["foreign"])
        )
        member = {
            "Authorization": f"Bearer {setup['member_token']}",
            "X-Workspace-Tenant-Id": setup["owner_id"],
        }
        from meridian import member_policy

        capability_checks = []

        def capability_would_allow(_scoped, capabilities, *, target=None):
            capability_checks.append((capabilities, target))

        monkeypatch.setattr(member_policy, "require_capabilities", capability_would_allow)
        denied_task = client.patch(
            f"/tasks/{foreign_task['id']}", headers=member, json={"status": "failed"},
        )
        denied_session = client.patch(
            f"/sessions/{foreign_session['id']}", headers=member,
            json={"status": "idle"},
        )
        assert denied_task.status_code == 403, denied_task.text
        assert denied_session.status_code == 403, denied_session.text
        assert capability_checks == []


def test_nonproject_policy_lookup_errors_fail_closed_with_http_503(monkeypatch, tmp_path):
    with _hosted_client(monkeypatch, tmp_path) as client:
        setup = asyncio.run(_seed_workspace(client.app.state.db))
        _, task = asyncio.run(
            _seed_session_and_task(client.app.state.db, setup["project"])
        )
        member = {
            "Authorization": f"Bearer {setup['member_token']}",
            "X-Workspace-Tenant-Id": setup["owner_id"],
        }
        from meridian import member_policy

        async def unavailable(*_args, **_kwargs):
            raise RuntimeError("auth db temporarily unavailable")

        monkeypatch.setattr(member_policy, "resolve_member_scope", unavailable)
        response = client.patch(
            f"/tasks/{task['id']}", headers=member, json={"status": "failed"},
        )
        assert response.status_code == 503, response.text


def test_scoped_http_reparent_uses_the_parent_that_was_authorized(monkeypatch, tmp_path):
    with _hosted_client(monkeypatch, tmp_path) as client:
        setup = asyncio.run(_seed_workspace(client.app.state.db))
        db = client.app.state.db
        child = _run(db_module.create_project(
            db, "member-http-reparent-race-child", parent_project_id=setup["project"],
        ))
        _run(_set_member_policy(
            db, setup["owner_id"], {
                "create_subproject": True,
                "delete_subproject": True,
                "delete_project": False,
            },
        ))
        member = {
            "Authorization": f"Bearer {setup['member_token']}",
            "X-Workspace-Tenant-Id": setup["owner_id"],
        }

        original_gate = server_module._member_policy_project_write_response
        raced = []

        async def authorize_then_detach(request):
            response = await original_gate(request)
            if request.method == "POST" and request.url.path == (
                f"/projects/{child['id']}/parent"
            ):
                raced.append(True)
                await db_module.set_parent_project(db, child["id"], None)
            return response

        monkeypatch.setattr(
            server_module, "_member_policy_project_write_response", authorize_then_detach,
        )
        response = client.post(
            f"/projects/{child['id']}/parent", headers=member,
            json={"parent_project_id": setup["other"]},
        )

        assert raced == [True]
        assert response.status_code == 409, response.text
        saved = _run(db_module.get_project(db, child["id"]))
        assert saved is not None
        assert saved["parent_project_id"] is None


def test_scoped_http_archive_revalidates_the_hierarchy_snapshot(
    monkeypatch, tmp_path,
):
    with _hosted_client(monkeypatch, tmp_path) as client:
        setup = asyncio.run(_seed_workspace(client.app.state.db))
        db = client.app.state.db
        _run(_set_member_policy(
            db, setup["owner_id"],
            {
                "write_project": True,
                "delete_project": True,
                "delete_subproject": False,
            },
        ))
        member = {
            "Authorization": f"Bearer {setup['member_token']}",
            "X-Workspace-Tenant-Id": setup["owner_id"],
        }

        original_gate = server_module._member_policy_project_write_response
        raced = []

        async def authorize_then_add_child(request):
            response = await original_gate(request)
            if (
                request.method == "PATCH"
                and request.url.path
                == f"/projects/{setup['project']}/organization"
            ):
                raced.append(True)
                await db_module.create_project(
                    db,
                    "member-http-archive-race-child",
                    parent_project_id=setup["project"],
                )
            return response

        monkeypatch.setattr(
            server_module,
            "_member_policy_project_write_response",
            authorize_then_add_child,
        )
        response = client.patch(
            f"/projects/{setup['project']}/organization",
            headers=member,
            json={"status": "archived"},
        )

        assert raced == [True]
        assert response.status_code == 409, response.text
        saved = _run(db_module.get_project(db, setup["project"]))
        assert saved is not None
        assert saved["status"] == "active"


@pytest.mark.parametrize("batch", [False, True])
def test_scoped_http_delete_uses_the_hierarchy_snapshot_that_was_authorized(
    monkeypatch, tmp_path, batch,
):
    with _hosted_client(monkeypatch, tmp_path) as client:
        setup = asyncio.run(_seed_workspace(client.app.state.db))
        db = client.app.state.db
        child = _run(db_module.create_project(
            db, "member-http-delete-race-child", parent_project_id=setup["project"],
        ))
        _run(_set_member_policy(
            db, setup["owner_id"],
            {"delete_subproject": True, "delete_project": False},
        ))
        member = {
            "Authorization": f"Bearer {setup['member_token']}",
            "X-Workspace-Tenant-Id": setup["owner_id"],
        }

        original_gate = server_module._member_policy_project_write_response
        raced = []

        async def authorize_then_detach(request):
            response = await original_gate(request)
            if request.method == "DELETE" and request.url.path in {
                f"/projects/{child['id']}", "/projects",
            }:
                raced.append(True)
                await db_module.set_parent_project(db, child["id"], None)
            return response

        monkeypatch.setattr(
            server_module, "_member_policy_project_write_response", authorize_then_detach,
        )
        if batch:
            response = client.delete(
                "/projects", headers=member,
                params=[("project_id", child["id"])],
            )
        else:
            response = client.delete(f"/projects/{child['id']}", headers=member)

        assert raced == [True]
        assert response.status_code == 409, response.text
        saved = _run(db_module.get_project(db, child["id"]))
        assert saved is not None
        assert saved["parent_project_id"] is None


import meridian.server as server_module
from meridian import db as db_module
from meridian import member_policy


def _run(coro):
    return asyncio.run(coro)


def _scoped(project_id: str, *capabilities: str) -> member_policy.ScopedProjectIds:
    return member_policy.ScopedProjectIds(
        (project_id,),
        policy=member_policy.EffectivePolicy(
            caps=frozenset(capabilities), role=member_policy.ROLE_MEMBER,
        ),
    )


def _set_hook_scope(monkeypatch, scoped):
    async def _scope(_request):
        return scoped

    monkeypatch.setattr(server_module, "_hook_scoped_project_ids", _scope)


def _create_project(client):
    return _run(db_module.create_project(client.app.state.db, "member-policy-hooks"))


def test_scoped_member_without_write_project_is_denied_by_both_hooks(client, monkeypatch):
    project = _create_project(client)
    _set_hook_scope(monkeypatch, _scoped(project["id"]))

    start = client.post(
        "/hooks/session-start",
        json={"project_id": project["id"], "session_name": "denied-hook"},
    )
    stop = client.post("/hooks/stop", json={"project_id": project["id"]})

    for response in (start, stop):
        assert response.status_code == 403, response.text
        assert "write_project" in response.json()["detail"]


def test_scoped_member_with_write_project_can_use_both_hooks(client, monkeypatch):
    project = _create_project(client)
    session = client.post(
        "/sessions/register",
        json={"project_id": project["id"], "name": "allowed-hook"},
    ).json()
    _set_hook_scope(monkeypatch, _scoped(project["id"], member_policy.WRITE_PROJECT))

    start = client.post(
        "/hooks/session-start",
        json={"project_id": project["id"], "session_name": "allowed-hook"},
    )
    stop = client.post(
        "/hooks/stop",
        json={"project_id": project["id"], "session_id": session["id"]},
    )

    assert start.status_code == 200, start.text
    assert start.json()["meridian_session_id"]
    assert stop.status_code == 200, stop.text
    assert stop.json()["ok"] is True
