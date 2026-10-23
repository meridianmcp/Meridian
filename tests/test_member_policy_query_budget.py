"""Request-path statement budgets for project-scoped member authorization."""
from __future__ import annotations

import types

import pytest

from meridian import _deps
from meridian import db as db_module
from meridian import member_policy as mp
from meridian.db import workspace as workspace_db
from member_policy_fixtures import StatementTrace, split_dbs, uniq


@pytest.fixture
def hosted_env(monkeypatch):
    monkeypatch.setenv("MERIDIAN_HOSTED", "1")


def _request(auth_db, token: str, workspace_id: str):
    return types.SimpleNamespace(
        headers={
            "authorization": f"Bearer {token}",
            "x-workspace-tenant-id": workspace_id,
        },
        cookies={},
        state=types.SimpleNamespace(),
        app=types.SimpleNamespace(state=types.SimpleNamespace(db=auth_db)),
    )


@pytest.mark.asyncio
async def test_scoped_request_budget_and_live_revocation(split_dbs, hosted_env, monkeypatch):
    d = split_dbs
    grant = await d.invite("member", d.ids["P"])
    token, _ = await db_module.create_api_token(d.auth_db, d.member["id"])
    auth = StatementTrace(d.auth_db, label="auth db")
    owner_db = StatementTrace(d.tenant_db, label="owner db")

    async def get_owner_db(_request):
        return owner_db

    monkeypatch.setattr(_deps, "_db", get_owner_db)

    first = await _deps._scoped_project_ids_for_request(_request(auth, token, d.owner["id"]))
    assert type(first) is mp.ScopedProjectIds
    assert set(first) == {d.ids["P"], d.ids["S1"]}
    assert not first.policy.allows(mp.DELETE_SUBPROJECT)

    # Full first-request path: bearer-token lookup, one combined membership /
    # role / policy read, and the cosmetic owner-email read. Expansion is a
    # single live project query on the workspace owner's separate database.
    assert len(auth.statements) == 3
    combined = [s for s in auth.statements if "workspace_members" in s and "member_policy" in s]
    assert len(combined) == 1
    assert len(owner_db.statements) == 1 and "projects" in owner_db.statements[0]

    await d.set_default({"delete_subproject": True})
    new_child = await d.project(parent=d.ids["P"])
    auth.reset()
    owner_db.reset()
    fresh = await _deps._scoped_project_ids_for_request(_request(auth, token, d.owner["id"]))
    assert new_child in fresh
    assert fresh.policy.allows(mp.DELETE_SUBPROJECT)
    assert len(auth.statements) == 3
    assert len([s for s in auth.statements if "workspace_members" in s and "member_policy" in s]) == 1
    assert len(owner_db.statements) == 1

    # Revocation is visible on a new request without any auth cache; the now
    # unscoped member never causes an owner-database lookup.
    await db_module.delete_workspace_member(d.auth_db, grant["id"], d.owner["id"])
    auth.reset()
    owner_db.reset()
    revoked = await _deps._scoped_project_ids_for_request(_request(auth, token, d.owner["id"]))
    assert revoked is None
    assert len(auth.statements) == 2  # credential + combined authorization context
    assert owner_db.statements == []


@pytest.mark.asyncio
async def test_owner_wide_and_nonmember_request_budgets(split_dbs, hosted_env, monkeypatch):
    d = split_dbs
    wide = await db_module.upsert_tenant(d.auth_db, f"wide-{uniq('x')}@example.com")
    await d.invite("member", None, email=wide["email"])
    wide_token, _ = await db_module.create_api_token(d.auth_db, wide["id"])
    stranger = await db_module.upsert_tenant(d.auth_db, f"stranger-{uniq('x')}@example.com")
    stranger_token, _ = await db_module.create_api_token(d.auth_db, stranger["id"])
    owner_token, _ = await db_module.create_api_token(d.auth_db, d.owner["id"])

    auth = StatementTrace(d.auth_db, label="auth db")
    owner_db = StatementTrace(d.tenant_db, explode=True, label="owner db")

    async def get_owner_db(_request):
        return owner_db

    monkeypatch.setattr(_deps, "_db", get_owner_db)

    # Own-workspace views require one credential lookup but no membership or
    # project work. Workspace-wide and nonmember views each need the credential
    # plus one combined authorization lookup, and still avoid the owner DB.
    assert await _deps._scoped_project_ids_for_request(_request(auth, owner_token, d.owner["id"])) is None
    assert len(auth.statements) == 1

    auth.reset()
    assert await _deps._scoped_project_ids_for_request(_request(auth, wide_token, d.owner["id"])) is None
    assert len(auth.statements) == 2

    auth.reset()
    assert await _deps._scoped_project_ids_for_request(_request(auth, stranger_token, d.owner["id"])) is None
    assert len(auth.statements) == 2
    assert owner_db.statements == []
