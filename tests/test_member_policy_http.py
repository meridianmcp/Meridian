"""Hosted HTTP enforcement for project-scoped member capabilities."""
from __future__ import annotations

import asyncio
import importlib
from datetime import datetime, timezone

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
