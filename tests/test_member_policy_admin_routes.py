"""Hosted HTTP coverage for owner-managed member capability policies."""
from __future__ import annotations

import asyncio
import importlib
from datetime import datetime, timezone

import meridian.server  # noqa: F401
from meridian import db as db_module
from meridian.db import workspace as workspace_db


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


async def _seed_policy_workspace(db):
    owner = await db_module.upsert_tenant(db, "policy-owner@example.com")
    await db.execute("UPDATE tenants SET plan='admin' WHERE id=?", (owner["id"],))
    other = await db_module.upsert_tenant(db, "policy-other@example.com")
    scoped = await db_module.upsert_tenant(db, "policy-scoped@example.com")
    owner_token, _ = await db_module.create_api_token(db, owner["id"])
    other_token, _ = await db_module.create_api_token(db, other["id"])
    member_token, _ = await db_module.create_api_token(db, scoped["id"])
    project = (await db_module.create_project(db, "policy-project"))["id"]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    await db.execute(
        "INSERT INTO workspace_members "
        "(id, tenant_id, email, role, github_access, joined_at, project_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("policy-member", owner["id"], scoped["email"], "admin", "none", now, project),
    )
    await db.commit()
    return {
        "owner_id": owner["id"], "owner_token": owner_token,
        "other_token": other_token, "member_token": member_token,
        "member_id": "policy-member", "project_id": project,
    }


def test_owner_policy_routes_expose_defaults_sources_and_override_inheritance(monkeypatch, tmp_path):
    with _hosted_client(monkeypatch, tmp_path) as client:
        setup = asyncio.run(_seed_policy_workspace(client.app.state.db))
        owner = {"Authorization": f"Bearer {setup['owner_token']}"}
        defaults = client.get("/workspace/member-policy", headers=owner)
        assert defaults.status_code == 200, defaults.text
        initial = defaults.json()
        assert initial["default_policy"]["write_project"] is None
        assert initial["default_policy"]["delete_project"] is None
        assert initial["builtin_defaults"]["write_project"] is True
        assert initial["builtin_defaults"]["delete_project"] is False
        catalog = initial["catalog"]
        assert [capability["id"] for capability in catalog] == list(initial["builtin_defaults"])
        write_project = next(capability for capability in catalog if capability["id"] == "write_project")
        assert write_project["label"] == "Edit project content"
        assert write_project["sentence"] == "Edit everything in their project and its subprojects"
        assert write_project["builtin_default"] is True

        saved = client.patch(
            "/workspace/member-policy", headers=owner,
            json={"policy": {"write_project": False}},
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["default_policy"]["write_project"] is False
        member_url = f"/workspace/members/{setup['member_id']}/policy"
        member = client.get(member_url, headers=owner)
        assert member.status_code == 200, member.text
        assert member.json()["effective"]["capabilities"]["write_project"] is False
        assert member.json()["effective"]["sources"]["write_project"] == "default"

        changed = client.patch(
            member_url, headers=owner,
            json={"policy": {"write_project": True}},
        )
        assert changed.status_code == 200, changed.text
        assert changed.json()["effective"]["capabilities"]["write_project"] is True
        assert changed.json()["effective"]["sources"]["write_project"] == "override"
        summary = client.get("/workspace/member-policy", headers=owner)
        assert summary.status_code == 200, summary.text
        override = summary.json()["overrides"][0]
        assert override["member_id"] == setup["member_id"]
        assert override["role"] == "admin"
        assert override["project_id"] == setup["project_id"]
        assert override["effective"]["sources"]["write_project"] == "override"

        inherited = client.patch(
            member_url, headers=owner,
            json={"policy": {"write_project": None}},
        )
        assert inherited.status_code == 200, inherited.text
        assert inherited.json()["policy"]["write_project"] is None
        assert inherited.json()["effective"]["capabilities"]["write_project"] is False
        assert inherited.json()["effective"]["sources"]["write_project"] == "default"
        assert client.delete(member_url, headers=owner).status_code == 204


def test_policy_routes_are_owner_only_and_member_ids_are_workspace_scoped(monkeypatch, tmp_path):
    with _hosted_client(monkeypatch, tmp_path) as client:
        setup = asyncio.run(_seed_policy_workspace(client.app.state.db))
        url = f"/workspace/members/{setup['member_id']}/policy"
        outsider = {
            "Authorization": f"Bearer {setup['other_token']}",
            "X-Workspace-Tenant-Id": setup["owner_id"],
        }
        assert client.get("/workspace/member-policy", headers=outsider).status_code == 403
        assert client.patch(
            "/workspace/member-policy", headers=outsider,
            json={"policy": {"write_project": True}},
        ).status_code == 403

        owner = {
            "Authorization": f"Bearer {setup['owner_token']}",
            "X-Workspace-Tenant-Id": setup["owner_id"],
        }
        assert client.get(
            "/workspace/members/not-this-workspace/policy", headers=owner,
        ).status_code == 404
        assert client.patch(
            url, headers=owner, json={"policy": {"unknown_capability": True}},
        ).status_code == 400


def test_policy_summary_includes_inherited_members_with_fixed_query_count(monkeypatch, tmp_path):
    with _hosted_client(monkeypatch, tmp_path) as client:
        setup = asyncio.run(_seed_policy_workspace(client.app.state.db))
        db = client.app.state.db
        member = asyncio.run(db_module.upsert_tenant(db, "policy-second@example.com"))
        project = asyncio.run(db_module.create_project(db, "policy-second-project"))
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        asyncio.run(db.execute(
            "INSERT INTO workspace_members "
            "(id, tenant_id, email, role, github_access, joined_at, project_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("policy-second-member", setup["owner_id"], member["email"], "member", "none", now, project["id"]),
        ))
        asyncio.run(db.commit())
        asyncio.run(db_module.set_member_policy_default(
            db, setup["owner_id"], {"write_project": False}, "policy-owner@example.com",
        ))

        row_reads = 0
        role_reads = 0
        list_reads = 0
        original_rows = db_module.get_member_policy_rows
        original_roles = db_module.get_member_roles
        original_list = workspace_db.list_member_policy_members

        async def count_rows(*args, **kwargs):
            nonlocal row_reads
            row_reads += 1
            return await original_rows(*args, **kwargs)

        async def count_roles(*args, **kwargs):
            nonlocal role_reads
            role_reads += 1
            return await original_roles(*args, **kwargs)

        class CountingConnection:
            def __init__(self, inner):
                self.inner = inner
                self.statements = []

            def execute(self, sql, params=()):
                self.statements.append(sql)
                return self.inner.execute(sql, params)

            def __getattr__(self, name):
                return getattr(self.inner, name)

        async def count_list(database, tenant_id):
            nonlocal list_reads
            list_reads += 1
            counted = CountingConnection(database)
            result = await original_list(counted, tenant_id)
            assert len(counted.statements) == 1
            return result

        monkeypatch.setattr(db_module, "get_member_policy_rows", count_rows)
        monkeypatch.setattr(db_module, "get_member_roles", count_roles)
        monkeypatch.setattr(workspace_db, "list_member_policy_members", count_list)
        summary = client.get(
            "/workspace/member-policy",
            headers={"Authorization": f"Bearer {setup['owner_token']}"},
        )
        assert summary.status_code == 200, summary.text
        entries = summary.json()["overrides"]
        assert {entry["email"] for entry in entries} == {
            "policy-scoped@example.com", "policy-second@example.com",
        }
        for entry in entries:
            assert entry["policy"]["write_project"] is None
            assert entry["effective"]["capabilities"]["write_project"] is False
            assert entry["effective"]["sources"]["write_project"] == "default"
        assert row_reads == 1  # workspace default, independent of member count
        assert role_reads == 0
        assert list_reads == 1


def test_corrupt_policy_patch_fails_closed_and_explicit_reset_recovers(monkeypatch, tmp_path):
    with _hosted_client(monkeypatch, tmp_path) as client:
        setup = asyncio.run(_seed_policy_workspace(client.app.state.db))
        db = client.app.state.db
        asyncio.run(db.execute(
            "INSERT INTO member_policy (tenant_id, subject, policy, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (setup["owner_id"], "*", "{broken", "2026-01-01T00:00:00+00:00"),
        ))
        asyncio.run(db.execute(
            "INSERT INTO member_policy (tenant_id, subject, policy, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (setup["owner_id"], "policy-scoped@example.com", "[false]", "2026-01-01T00:00:00+00:00"),
        ))
        asyncio.run(db.commit())
        owner = {"Authorization": f"Bearer {setup['owner_token']}"}

        default_patch = client.patch(
            "/workspace/member-policy", headers=owner,
            json={"policy": {"write_project": False}},
        )
        assert default_patch.status_code == 409, default_patch.text
        member_url = f"/workspace/members/{setup['member_id']}/policy"
        override_patch = client.patch(
            member_url, headers=owner,
            json={"policy": {"write_project": False}},
        )
        assert override_patch.status_code == 409, override_patch.text
        assert asyncio.run(db_module.get_member_policy_rows(
            db, setup["owner_id"], "policy-scoped@example.com",
        )) == {"default": "{broken", "override": "[false]"}

        default_reset = client.patch(
            "/workspace/member-policy", headers=owner, json={"reset": True},
        )
        assert default_reset.status_code == 200, default_reset.text
        assert default_reset.json()["default_policy"]["write_project"] is None
        assert asyncio.run(db_module.get_member_policy_rows(
            db, setup["owner_id"], None,
        ))["default"] == "{}"

        override_reset = client.patch(
            member_url, headers=owner, json={"reset": True},
        )
        assert override_reset.status_code == 200, override_reset.text
        assert override_reset.json()["policy"]["write_project"] is None
        assert asyncio.run(db_module.get_member_policy_rows(
            db, setup["owner_id"], "policy-scoped@example.com",
        ))["override"] is None


def test_role_clamp_cannot_be_overridden_by_member_policy(monkeypatch, tmp_path):
    with _hosted_client(monkeypatch, tmp_path) as client:
        setup = asyncio.run(_seed_policy_workspace(client.app.state.db))
        owner = {"Authorization": f"Bearer {setup['owner_token']}"}
        member_url = f"/workspace/members/{setup['member_id']}/policy"
        changed = client.patch(
            member_url, headers=owner,
            json={"policy": {"write_project": True}},
        )
        assert changed.status_code == 200, changed.text

        async def _demote_to_viewer():
            db = client.app.state.db
            await db.execute(
                "UPDATE workspace_members SET role='viewer' WHERE id=? AND tenant_id=?",
                (setup["member_id"], setup["owner_id"]),
            )
            await db.commit()

        asyncio.run(_demote_to_viewer())
        effective = client.get(member_url, headers=owner)
        assert effective.status_code == 200, effective.text
        readout = effective.json()["effective"]["readout"]["write_project"]
        assert readout["allowed"] is False
        assert readout["source"] == "role_clamp"


async def test_member_policy_readout_is_backend_agnostic(anydb):
    """The route's DB-backed projection works on both SQLite and Postgres."""
    owner = await db_module.upsert_tenant(anydb, "member-policy-anydb-owner@example.com")
    member = await db_module.upsert_tenant(anydb, "member-policy-anydb-user@example.com")
    project_id = (await db_module.create_project(anydb, "member-policy-anydb-project"))["id"]
    await anydb.execute(
        "INSERT INTO workspace_members "
        "(id, tenant_id, email, role, github_access, joined_at, project_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("member-policy-anydb-row", owner["id"], member["email"], "admin", "none", "2026-01-01 00:00:00", project_id),
    )
    await db_module.set_member_policy_default(
        anydb, owner["id"], {"write_project": False}, owner["email"],
    )
    await db_module.set_member_policy_override(
        anydb, owner["id"], member["email"], {"write_project": True}, owner["email"],
    )
    from meridian.routes.workspace import _member_policy_view

    result = await _member_policy_view(
        anydb, owner["id"], member["email"], member_id="member-policy-anydb-row",
        role="admin", project_id=project_id,
    )
    assert result["effective"]["capabilities"]["write_project"] is True
    assert result["effective"]["sources"]["write_project"] == "override"
