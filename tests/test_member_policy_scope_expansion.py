"""Scope expansion of project-scoped members (lane L1).

A member invited to project P reaches P and every subproject of P, including ones
created later, without a new invite. The expansion is built in exactly one place,
``member_policy.resolve_member_scope``, and carries the member's effective policy
with it.

Everything here uses SPLIT databases (see ``member_policy_fixtures``): the grants
and the policy are in the auth database, the hierarchy in the owner's tenant
database. A resolver that expanded against the wrong database would be silent and
under-inclusive, and a single shared database could not show it.

Sections: the resolver on two bare databases; what each kind of caller pays;
the per-request resolution through ``_deps``; the HTTP stack on a hosted app; the
single-caller rule.
"""
from __future__ import annotations

import logging
import re
import types
from pathlib import Path

import pytest
from fastapi import Request

from meridian import _deps
from meridian import db as db_module
from meridian import member_policy as mp
from meridian.db import workspace as workspace_db
from member_policy_fixtures import (  # noqa: F401  (fixtures)
    StatementTrace,
    build_split_dbs,
    hosted_client,
    split_dbs,
    split_workspace,
    uniq,
)

SCOPE_MSG = "Project is outside your access scope."


class _FailingDb:
    """Passes everything through except statements mentioning ``needle``, which raise."""

    def __init__(self, inner, needle: str) -> None:
        self._inner = inner
        self._needle = needle

    def execute(self, sql, *args, **kwargs):
        if self._needle in str(sql):
            raise RuntimeError(f"induced failure on {self._needle}")
        return self._inner.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


async def _raw_parent(db, project_id, parent_id):
    """Write parent_project_id with no validation (states the API refuses)."""
    await db.execute("UPDATE projects SET parent_project_id = ? WHERE id = ?", (parent_id, project_id))
    await db.commit()


# ---------------------------------------------------------------------------
# the resolver on two bare databases
# ---------------------------------------------------------------------------

async def test_explicit_parent_grants_existing_children_and_carries_the_hierarchy(split_dbs):
    d = split_dbs
    await d.invite("member", d.ids["P"])
    scope = await d.resolve()

    assert type(scope) is mp.ScopedProjectIds
    assert list(scope) == [d.ids["P"], d.ids["S1"]]  # explicit first, then children
    assert scope.explicit == (d.ids["P"],)
    assert scope.parent_of == {d.ids["S1"]: d.ids["P"]}
    assert scope.role == "member" and scope.policy == mp.BUILTIN_POLICY
    assert scope.email == d.member["email"].lower()
    assert scope.workspace_tenant_id == d.owner["id"]
    assert scope.owner_email == d.owner["email"]
    # nothing of the other family
    assert d.ids["F"] not in scope and d.ids["FS"] not in scope


async def test_a_child_created_later_is_reachable_without_a_new_invite(split_dbs):
    d = split_dbs
    await d.invite("member", d.ids["P"])
    before = await d.resolve()
    later = await d.project(parent=d.ids["P"])
    after = await d.resolve()
    assert later not in before and later in after
    assert after.parent_of[later] == d.ids["P"]


async def test_children_of_a_deleted_parent_grant_nothing(split_dbs):
    d = split_dbs
    doomed = await d.project()
    orphan = await d.project(parent=doomed)
    await d.invite("member", doomed)
    assert orphan in await d.resolve()

    await db_module.delete_project(d.tenant_db, doomed)  # children are left behind
    scope = await d.resolve()
    assert list(scope) == [doomed]  # the ghost grant stays, the orphan is gone
    assert orphan not in scope and scope.parent_of == {}


async def test_a_depth_two_grandchild_is_never_in_scope(split_dbs):
    d = split_dbs
    await d.invite("member", d.ids["P"])
    grandchild = await d.project()
    await _raw_parent(d.tenant_db, grandchild, d.ids["S1"])  # a race the API now refuses
    scope = await d.resolve()
    assert d.ids["S1"] in scope and grandchild not in scope

    # a project nested after it gained children is a depth-2 chain too
    nested = await d.project()
    below = await d.project(parent=nested)
    await _raw_parent(d.tenant_db, nested, d.ids["P"])
    scope = await d.resolve()
    assert nested in scope and below not in scope


async def test_a_detached_child_leaves_the_scope(split_dbs):
    d = split_dbs
    await d.invite("member", d.ids["P"])
    assert d.ids["S1"] in await d.resolve()
    await db_module.set_parent_project(d.tenant_db, d.ids["S1"], None)
    assert d.ids["S1"] not in await d.resolve()


async def test_reparenting_into_a_granted_project_is_a_grant_edge(split_dbs):
    d = split_dbs
    await d.invite("member", d.ids["P"])
    stray = await d.project()
    assert stray not in await d.resolve()
    await db_module.set_parent_project(d.tenant_db, stray, d.ids["P"])
    assert stray in await d.resolve()


async def test_a_subproject_only_member_gets_no_parent_and_no_siblings(split_dbs):
    d = split_dbs
    sibling = await d.project(parent=d.ids["P"])
    await d.invite("member", d.ids["S1"])
    scope = await d.resolve()
    assert list(scope) == [d.ids["S1"]]
    assert d.ids["P"] not in scope and sibling not in scope
    assert scope.parent_of == {}


async def test_a_subproject_only_member_cannot_create_under_their_subproject(split_dbs):
    """The scope reaches S1, but the one-level rule still refuses a child of a
    subproject, so a subproject-only member has nowhere to create."""
    d = split_dbs
    await d.invite("member", d.ids["S1"])
    scope = await d.resolve()
    assert list(scope) == [d.ids["S1"]]
    with pytest.raises(ValueError, match="one level"):
        await db_module.create_project(d.tenant_db, uniq("nested"), parent_project_id=d.ids["S1"])


async def test_several_grants_are_a_union_with_no_duplicates(split_dbs):
    d = split_dbs
    await d.invite("member", d.ids["P"])
    await d.invite("member", d.ids["F"])
    await d.invite("member", d.ids["S1"])  # already inherited from P
    scope = await d.resolve()
    assert len(scope) == len(set(scope)) == 4
    assert set(scope) == {d.ids["P"], d.ids["S1"], d.ids["F"], d.ids["FS"]}
    assert scope.explicit == (d.ids["P"], d.ids["F"], d.ids["S1"])
    assert list(scope)[:3] == [d.ids["P"], d.ids["F"], d.ids["S1"]]  # explicit first


async def test_a_ghost_grant_is_harmless(split_dbs):
    d = split_dbs
    await d.invite("member", "no-such-project")
    scope = await d.resolve()
    assert list(scope) == ["no-such-project"] and scope.parent_of == {}


async def test_children_of_another_workspace_are_excluded(split_dbs):
    d = split_dbs
    other = await build_split_dbs(d.auth_db)
    try:
        await d.invite("member", d.ids["P"])
        await other.invite("member", other.ids["P"], email=d.member["email"])

        mine = await d.resolve()
        theirs = await other.resolve(email=d.member["email"])
        assert set(mine) == {d.ids["P"], d.ids["S1"]}
        assert set(theirs) == {other.ids["P"], other.ids["S1"]}
        # the wrong tenant DB is silently under-inclusive, which is why the
        # resolver is handed the owner's database and not looked up from the auth DB
        wrong = await d.resolve(tenant_db=other.tenant_db)
        assert list(wrong) == [d.ids["P"]]
    finally:
        await other.tenant_db.close()


async def test_an_empty_explicit_list_stays_scoped_and_never_opens_the_tenant_db(split_dbs, monkeypatch):
    d = split_dbs

    async def no_grants(*_a, **_k):
        return {
            "project_ids": [],
            "memberships": [],
            "policy_rows": {"default": None, "override": None},
        }

    monkeypatch.setattr(workspace_db, "get_member_authorization_context", no_grants)

    def never():
        raise AssertionError("the owner's database must not be opened for an empty scope")

    scope = await d.resolve(tenant_db=never)
    assert scope is not None and list(scope) == [] and scope.explicit == ()
    assert type(scope) is mp.ScopedProjectIds


async def test_tenant_db_may_be_a_connection_or_a_sync_or_async_factory(split_dbs):
    d = split_dbs
    await d.invite("member", d.ids["P"])
    opened = []

    async def open_async():
        opened.append("async")
        return d.tenant_db

    def open_sync():
        opened.append("sync")
        return d.tenant_db

    for tenant_db in (d.tenant_db, open_async, open_sync):
        assert set(await d.resolve(tenant_db=tenant_db)) == {d.ids["P"], d.ids["S1"]}
    assert opened == ["async", "sync"]


async def test_the_resolver_never_returns_a_plain_list(split_dbs):
    d = split_dbs
    await d.invite("member", d.ids["P"])
    for scope in (await d.resolve(), await d.resolve(email=d.member["email"].upper())):
        assert type(scope) is mp.ScopedProjectIds
    assert await d.resolve(email="stranger@example.com") is None


# --- policy and role through the resolver ----------------------------------

async def test_policy_comes_from_the_default_and_the_override_in_the_auth_db(split_dbs):
    d = split_dbs
    await d.invite("member", d.ids["P"])
    await d.set_default({"delete_subproject": True})
    scope = await d.resolve()
    assert scope.policy.allows(mp.DELETE_SUBPROJECT) and scope.policy.source(mp.DELETE_SUBPROJECT) == "default"

    await d.set_override({"delete_subproject": False, "write_workspace_level": True})
    scope = await d.resolve()
    assert not scope.policy.allows(mp.DELETE_SUBPROJECT)
    assert scope.policy.allows(mp.WRITE_WORKSPACE_LEVEL)
    assert scope.policy.source(mp.WRITE_WORKSPACE_LEVEL) == "override"

    # the same answer without any tenant-DB access
    effective = await mp.resolve_effective_policy(
        StatementTrace(d.auth_db), d.owner["id"], d.member["email"].upper())
    assert effective == scope.policy


async def test_another_persons_override_is_not_read(split_dbs):
    d = split_dbs
    await d.invite("member", d.ids["P"])
    await d.set_override({"delete_project": True}, email="someone-else@example.com")
    assert not (await d.resolve()).policy.allows(mp.DELETE_PROJECT)


async def test_the_role_is_the_most_restrictive_across_every_accepted_row(split_dbs):
    d = split_dbs
    await d.invite("admin", d.ids["P"])
    await d.invite("viewer", d.ids["F"])
    await d.set_override({cid: True for cid in mp.CAPABILITY_IDS})
    scope = await d.resolve()
    assert scope.role == "viewer"
    assert scope.policy.caps == frozenset()
    assert set(scope.policy.sources.values()) == {"role_clamp"}


async def test_a_pending_invite_grants_and_clamps_nothing(split_dbs):
    d = split_dbs
    await d.invite("viewer", d.ids["F"], accepted=False)
    await d.invite("member", d.ids["P"])
    scope = await d.resolve()
    assert scope.role == "member" and scope.policy.allows(mp.WRITE_PROJECT)
    assert d.ids["F"] not in scope


@pytest.mark.parametrize("subject, text, expect_reason", [
    ("override", "{not json", mp.REASON_CORRUPT_OVERRIDE),
    ("default", "[]", mp.REASON_CORRUPT_DEFAULT),
])
async def test_a_corrupt_policy_row_denies_everything_and_is_logged(split_dbs, caplog, subject, text, expect_reason):
    d = split_dbs
    await d.invite("member", d.ids["P"])
    await d.raw_policy_row("*" if subject == "default" else d.member["email"].lower(), text)
    with caplog.at_level(logging.ERROR, logger="meridian.member_policy"):
        scope = await d.resolve()
    assert scope.policy.caps == frozenset()
    assert set(scope.policy.reasons.values()) == {expect_reason}
    assert any("unparseable" in r.getMessage() and r.levelno == logging.ERROR for r in caplog.records)
    # the scope itself is still the member's, only their capabilities are gone
    assert d.ids["S1"] in scope


# --- errors propagate, nothing is silently narrowed or widened --------------

@pytest.mark.parametrize("which, needle", [
    ("auth", "FROM workspace_members"),  # combined membership, role, and policy read
    ("tenant", "FROM projects"),          # live child expansion
])
async def test_a_failing_read_propagates_instead_of_narrowing_the_scope(split_dbs, which, needle):
    d = split_dbs
    await d.invite("member", d.ids["P"])
    auth = _FailingDb(d.auth_db, needle) if which == "auth" else d.auth_db
    tenant = _FailingDb(d.tenant_db, needle) if which == "tenant" else d.tenant_db
    with pytest.raises(RuntimeError, match="induced failure"):
        await d.resolve(auth_db=auth, tenant_db=tenant)


async def test_a_failing_tenant_db_factory_propagates(split_dbs):
    d = split_dbs
    await d.invite("member", d.ids["P"])

    async def boom():
        raise RuntimeError("tenant database not provisioned")

    with pytest.raises(RuntimeError, match="not provisioned"):
        await d.resolve(tenant_db=boom)


async def test_a_failing_owner_email_lookup_is_cosmetic(split_dbs, monkeypatch):
    d = split_dbs
    await d.invite("member", d.ids["P"])

    async def boom(*_a, **_k):
        raise RuntimeError("tenants unavailable")

    monkeypatch.setattr(db_module, "get_tenant_by_id", boom)
    scope = await d.resolve()
    assert scope.owner_email is None and d.ids["S1"] in scope


# ---------------------------------------------------------------------------
# what each kind of caller pays
# ---------------------------------------------------------------------------

def _never_open():
    raise AssertionError("the owner's tenant database must not be opened")


async def test_owner_wide_member_and_non_member_pay_one_statement_and_no_tenant_db(split_dbs):
    d = split_dbs
    stranger = await db_module.upsert_tenant(d.auth_db, f"stranger-{uniq('x')}@example.com")
    wide = await db_module.upsert_tenant(d.auth_db, f"wide-{uniq('x')}@example.com")
    mixed = await db_module.upsert_tenant(d.auth_db, f"mixed-{uniq('x')}@example.com")
    pending = await db_module.upsert_tenant(d.auth_db, f"pending-{uniq('x')}@example.com")
    await d.invite("member", None, email=wide["email"])
    await d.invite("admin", d.ids["P"], email=mixed["email"])
    await d.invite("member", None, email=mixed["email"])  # any workspace-wide row wins
    await d.invite("member", d.ids["P"], email=pending["email"], accepted=False)

    for email in (d.owner["email"], stranger["email"], wide["email"], mixed["email"], pending["email"]):
        auth = StatementTrace(d.auth_db, label="auth db")
        tenant = StatementTrace(d.tenant_db, explode=True, label="tenant db")
        assert await mp.resolve_member_scope(auth, tenant, d.owner["id"], email) is None, email
        assert auth.tables() == ["workspace_members"], (email, auth.statements)
        assert tenant.statements == []

        # and with the lazy factory the request path uses
        auth = StatementTrace(d.auth_db, label="auth db")
        assert await mp.resolve_member_scope(auth, _never_open, d.owner["id"], email) is None
        assert auth.tables() == ["workspace_members"]


async def test_a_scoped_member_pays_a_fixed_budget_split_across_the_two_databases(split_dbs):
    d = split_dbs
    await d.invite("member", d.ids["P"])
    auth = StatementTrace(d.auth_db, label="auth db")
    tenant = StatementTrace(d.tenant_db, label="tenant db")
    scope = await mp.resolve_member_scope(auth, tenant, d.owner["id"], d.member["email"])
    assert d.ids["S1"] in scope
    # one authorization-context SELECT plus the best-effort owner-email lookup
    assert auth.tables() == ["workspace_members", "tenants"]
    assert len(auth.statements) == 2
    assert "workspace_members" in auth.statements[0] and "member_policy" in auth.statements[0]
    # one indexed children SELECT, on the owner's database and nowhere else
    assert tenant.tables() == ["projects"]
    assert not any("projects" in s for s in auth.statements)
    assert not any(t in " ".join(tenant.statements) for t in ("workspace_members", "member_policy"))


async def test_the_effective_policy_alone_never_touches_a_tenant_db(split_dbs):
    d = split_dbs
    await d.invite("member", d.ids["P"])
    auth = StatementTrace(d.auth_db)
    policy = await mp.resolve_effective_policy(auth, d.owner["id"], d.member["email"])
    assert policy == mp.BUILTIN_POLICY
    assert auth.tables() == ["member_policy", "workspace_members"]


# ---------------------------------------------------------------------------
# the per-request resolution through _deps
# ---------------------------------------------------------------------------

def _request(auth_db, *, headers=None, cookies=None):
    return types.SimpleNamespace(
        headers=headers or {}, cookies=cookies or {}, state=types.SimpleNamespace(),
        app=types.SimpleNamespace(state=types.SimpleNamespace(db=auth_db)),
    )


@pytest.fixture
def hosted_env(monkeypatch):
    monkeypatch.setenv("MERIDIAN_HOSTED", "1")


def _as(monkeypatch, tenant, opened):
    async def caller(_request, **_kw):
        return tenant

    async def tenant_db(_request):
        opened.append(1)
        return tenant_db.db

    monkeypatch.setattr(_deps, "_get_tenant_from_request", caller)
    monkeypatch.setattr(_deps, "_db", tenant_db)
    return tenant_db


async def test_requests_that_cannot_be_scoped_touch_no_database(split_dbs, monkeypatch):
    d = split_dbs
    exploding_auth = StatementTrace(d.auth_db, explode=True, label="auth db")
    ws = {"x-workspace-tenant-id": d.owner["id"]}

    async def no_caller(_request, **_kw):
        raise AssertionError("the caller must not even be resolved")

    monkeypatch.setattr(_deps, "_get_tenant_from_request", no_caller)
    monkeypatch.setattr(_deps, "_db", lambda _r: (_ for _ in ()).throw(AssertionError("_db")))

    # self-hosted
    monkeypatch.delenv("MERIDIAN_HOSTED", raising=False)
    assert await _deps._scoped_project_ids_for_request(_request(exploding_auth, headers=ws)) is None

    monkeypatch.setenv("MERIDIAN_HOSTED", "1")
    # demo cookie
    demo = _request(exploding_auth, headers=ws, cookies={_deps._DEMO_CONTEXT_COOKIE: "1"})
    assert await _deps._scoped_project_ids_for_request(demo) is None
    # no workspace header: the caller's own workspace
    assert await _deps._scoped_project_ids_for_request(_request(exploding_auth)) is None
    assert await _deps._scoped_project_ids_for_request(_request(exploding_auth, headers={"x-workspace-tenant-id": "  "})) is None


async def test_unauthenticated_and_own_workspace_requests_touch_no_database(split_dbs, hosted_env, monkeypatch):
    d = split_dbs
    exploding_auth = StatementTrace(d.auth_db, explode=True, label="auth db")
    ws = {"x-workspace-tenant-id": d.owner["id"]}
    opened: list = []

    _as(monkeypatch, None, opened)  # no credential resolved
    assert await _deps._scoped_project_ids_for_request(_request(exploding_auth, headers=ws)) is None

    _as(monkeypatch, {"id": d.owner["id"], "email": d.owner["email"]}, opened)  # header names their own tenant
    assert await _deps._scoped_project_ids_for_request(_request(exploding_auth, headers=ws)) is None
    assert opened == []


async def test_non_member_and_wide_member_cost_one_statement_and_never_open_the_owner_db(split_dbs, hosted_env, monkeypatch):
    d = split_dbs
    wide = await db_module.upsert_tenant(d.auth_db, f"wide-{uniq('x')}@example.com")
    await d.invite("member", None, email=wide["email"])
    opened: list = []
    for tenant in (d.member, wide):  # d.member has no row at all: a non-member
        _as(monkeypatch, {"id": tenant["id"], "email": tenant["email"]}, opened)
        auth = StatementTrace(d.auth_db)
        req = _request(auth, headers={"x-workspace-tenant-id": d.owner["id"]})
        assert await _deps._scoped_project_ids_for_request(req) is None
        assert auth.tables() == ["workspace_members"]
    assert opened == []


async def test_scoped_request_resolves_once_and_a_second_call_costs_nothing(split_dbs, hosted_env, monkeypatch):
    d = split_dbs
    await d.invite("member", d.ids["P"])
    opened: list = []
    _as(monkeypatch, {"id": d.member["id"], "email": d.member["email"]}, opened).db = d.tenant_db
    auth = StatementTrace(d.auth_db)
    req = _request(auth, headers={"x-workspace-tenant-id": d.owner["id"]})

    first = await _deps._scoped_project_ids_for_request(req)
    spent = list(auth.statements)
    assert type(first) is mp.ScopedProjectIds and set(first) == {d.ids["P"], d.ids["S1"]}
    assert spent and opened == [1]

    second = await _deps._scoped_project_ids_for_request(req)
    assert second is first and auth.statements == spent and opened == [1]

    # another request is resolved afresh: there is no cross-request cache
    other = _request(auth, headers={"x-workspace-tenant-id": d.owner["id"]})
    again = await _deps._scoped_project_ids_for_request(other)
    assert again is not first and again == first and len(auth.statements) == 2 * len(spent)


async def test_a_policy_edit_and_a_new_child_apply_to_the_very_next_request(split_dbs, hosted_env, monkeypatch):
    d = split_dbs
    grant = await d.invite("member", d.ids["P"])
    _as(monkeypatch, {"id": d.member["id"], "email": d.member["email"]}, []).db = d.tenant_db
    headers = {"x-workspace-tenant-id": d.owner["id"]}

    before = await _deps._scoped_project_ids_for_request(_request(d.auth_db, headers=headers))
    assert not before.policy.allows(mp.DELETE_SUBPROJECT)
    await d.set_default({"delete_subproject": True})
    new_child = await d.project(parent=d.ids["P"])
    after = await _deps._scoped_project_ids_for_request(_request(d.auth_db, headers=headers))
    assert after.policy.allows(mp.DELETE_SUBPROJECT) and new_child in after

    # revoking the membership narrows it just as fast
    await db_module.delete_workspace_member(d.auth_db, grant["id"], d.owner["id"])
    gone = await _deps._scoped_project_ids_for_request(_request(d.auth_db, headers=headers))
    assert gone is None


async def test_resolution_errors_propagate_out_of_the_request_function(split_dbs, hosted_env, monkeypatch):
    d = split_dbs
    await d.invite("member", d.ids["P"])
    opened: list = []
    tdb = _as(monkeypatch, {"id": d.member["id"], "email": d.member["email"]}, opened)
    tdb.db = d.tenant_db
    headers = {"x-workspace-tenant-id": d.owner["id"]}

    async def boom(*_a, **_k):
        raise RuntimeError("induced")

    for target, name in (
        (db_module, "get_subproject_map"),
        (workspace_db, "get_member_authorization_context"),
    ):
        with monkeypatch.context() as m:
            m.setattr(target, name, boom)
            req = _request(d.auth_db, headers=headers)
            with pytest.raises(RuntimeError, match="induced"):
                await _deps._scoped_project_ids_for_request(req)
            assert not hasattr(req.state, "_member_scope")  # a failure is never memoized as a scope

    async def no_db(_request):
        raise RuntimeError("tenant database not provisioned")

    monkeypatch.setattr(_deps, "_db", no_db)
    with pytest.raises(RuntimeError, match="not provisioned"):
        await _deps._scoped_project_ids_for_request(_request(d.auth_db, headers=headers))


# --- helpers that ride on the scope ------------------------------------------

def test_deny_unless_capability_and_the_scope_helpers_agree():
    scope = mp.make_scope(["p"])
    _deps._deny_unless_capability(None, mp.DELETE_PROJECT)  # unscoped callers are never refused
    _deps._deny_unless_capability(scope, mp.WRITE_PROJECT)
    with pytest.raises(mp.MemberPolicyDenied):
        _deps._deny_unless_capability(scope, mp.DELETE_PROJECT)
    # the scope refusal stays the plain-list-compatible helper's job and is unchanged
    with pytest.raises(Exception) as exc:
        _deps._deny_unless_in_scope(scope, "other")
    assert exc.value.status_code == 403 and exc.value.detail == SCOPE_MSG


async def test_require_capability_helper_orders_scope_before_capability(monkeypatch):
    scope = mp.make_scope(["p"], policy={"write_project": False})

    async def fake_scope(_request):
        return scope

    monkeypatch.setattr(_deps, "_scoped_project_ids_for_request", fake_scope)
    with pytest.raises(Exception) as exc:  # a foreign id keeps the scope message
        await _deps._require_capability(object(), mp.WRITE_PROJECT, project_id="other")
    assert getattr(exc.value, "detail", None) == SCOPE_MSG
    with pytest.raises(mp.MemberPolicyDenied):
        await _deps._require_capability(object(), mp.WRITE_PROJECT, project_id="p")
    with pytest.raises(mp.MemberPolicyDenied):
        await _deps._require_capability(object(), mp.WRITE_PROJECT)

    async def not_scoped(_request):
        return None

    monkeypatch.setattr(_deps, "_scoped_project_ids_for_request", not_scoped)
    assert await _deps._require_capability(object(), mp.DELETE_PROJECT, project_id="anything") is None


def test_foreign_workspace_header_guard():
    def req(value):
        return types.SimpleNamespace(headers={"x-workspace-tenant-id": value} if value is not None else {})

    _deps._reject_foreign_workspace_header(req(None), "me")
    _deps._reject_foreign_workspace_header(req(""), "me")
    _deps._reject_foreign_workspace_header(req("   "), "me")
    _deps._reject_foreign_workspace_header(req("me"), "me")
    _deps._reject_foreign_workspace_header(req(" me "), "me")
    for foreign in ("someone-else", "ME"):
        with pytest.raises(Exception) as exc:
            _deps._reject_foreign_workspace_header(req(foreign), "me")
        assert exc.value.status_code == 403
    with pytest.raises(Exception):
        _deps._reject_foreign_workspace_header(req("x"), None)


@pytest.mark.parametrize("method, path, perm", [
    ("PUT", "/workspace/member-policy/default", "policy"),
    ("PUT", "/workspace/member-policy", "policy"),
    ("PUT", "/workspace/member-policy/", "policy"),
    ("PUT", "/workspace/members/abc-123/policy", "policy"),
    ("DELETE", "/workspace/members/abc-123/policy", "policy"),
    ("DELETE", "/workspace/members/abc-123/policy/", "policy"),
    # the generic team-management mapping is untouched
    ("POST", "/workspace/invite", "invite"),
    ("PATCH", "/workspace/members/abc-123", "invite"),
    ("DELETE", "/workspace/members/abc-123", "invite"),
    ("GET", "/workspace/member-policy", None),
])
def test_member_policy_paths_map_to_the_owner_only_permission(method, path, perm):
    assert _deps._required_perm_for_request(method, path) == perm


def test_only_the_owner_holds_the_policy_permission():
    from meridian.roles import PERM_MANAGE_POLICY, has_perm

    assert PERM_MANAGE_POLICY == "policy"
    assert has_perm("owner", PERM_MANAGE_POLICY)
    for role in ("admin", "member", "viewer", None, "superuser"):
        assert not has_perm(role, PERM_MANAGE_POLICY)


# ---------------------------------------------------------------------------
# the HTTP stack on a hosted app with a split workspace
# ---------------------------------------------------------------------------

def _listing(ws, headers=None):
    r = ws.client.get("/projects", headers=headers or ws.member_headers())
    assert r.status_code == 200, r.text
    return {p["id"] for p in r.json()}


def _tasks(ws, project_id, headers=None):
    return ws.client.get(f"/projects/{project_id}/tasks", headers=headers or ws.member_headers())


def test_http_parent_grant_reaches_children_and_later_children(split_workspace):
    ws = split_workspace
    P, S1, F, FS = (ws.ids[k] for k in ("P", "S1", "F", "FS"))
    ws.invite("member", P)

    assert _listing(ws) == {P, S1}
    assert _tasks(ws, S1).status_code == 200
    for foreign in (F, FS):
        r = _tasks(ws, foreign)
        assert r.status_code == 403 and r.json() == {"detail": SCOPE_MSG}

    later = ws.project(parent=P)  # created after the grant, by anyone
    assert _listing(ws) == {P, S1, later}
    assert _tasks(ws, later).status_code == 200


def test_http_subproject_only_member_sees_only_that_subproject(split_workspace):
    ws = split_workspace
    P, S1 = ws.ids["P"], ws.ids["S1"]
    sibling = ws.project(parent=P)
    ws.invite("member", S1)
    assert _listing(ws) == {S1}
    assert _tasks(ws, S1).status_code == 200
    for outside in (P, sibling):
        assert _tasks(ws, outside).status_code == 403


def test_http_detach_reparent_orphan_and_depth_two_change_the_next_request(split_workspace):
    ws = split_workspace
    P, S1 = ws.ids["P"], ws.ids["S1"]
    ws.invite("member", P)
    assert _listing(ws) == {P, S1}

    stray = ws.project()
    assert _tasks(ws, stray).status_code == 403
    ws.run(db_module.set_parent_project(ws.owner_db, stray, P))  # a grant edge
    assert _tasks(ws, stray).status_code == 200

    grandchild = ws.project()
    ws.run(_raw_parent(ws.owner_db, grandchild, S1))
    assert _tasks(ws, grandchild).status_code == 403
    assert _listing(ws) == {P, S1, stray}

    ws.run(db_module.set_parent_project(ws.owner_db, S1, None))  # detached
    assert _tasks(ws, S1).status_code == 403
    assert _listing(ws) == {P, stray}

    ws.run(db_module.delete_project(ws.owner_db, P))  # parent deleted, stray orphaned
    assert _tasks(ws, stray).status_code == 403
    assert _listing(ws) == set()


def test_http_each_workspace_expands_against_its_own_owners_database(split_workspace):
    ws = split_workspace
    other = ws.run(build_split_dbs(ws.auth_db))
    _deps._tenant_db_cache[other.owner["id"]] = other.tenant_db
    try:
        ws.invite("member", ws.ids["P"])
        ws.run(other.invite("member", other.ids["P"], email=ws.member["email"]))
        mine = _listing(ws)
        theirs = _listing(ws, {**ws.member_headers(), "X-Workspace-Tenant-Id": other.owner["id"]})
        assert mine == {ws.ids["P"], ws.ids["S1"]}
        assert theirs == {other.ids["P"], other.ids["S1"]}
    finally:
        _deps._tenant_db_cache.pop(other.owner["id"], None)
        ws.run(other.tenant_db.close())


def _count(monkeypatch, owner, name):
    calls = {"n": 0}
    real = getattr(owner, name)

    async def counting(*a, **k):
        calls["n"] += 1
        return await real(*a, **k)

    monkeypatch.setattr(owner, name, counting)
    return calls


def test_http_scope_is_resolved_once_per_request_across_middleware_and_route(split_workspace, monkeypatch):
    """The project-scope middleware, the route and the route's own helper calls all
    share one resolution: request.state is shared between the middleware layers and
    the endpoint (a miss would cost queries, not correctness)."""
    ws = split_workspace
    P, S1 = ws.ids["P"], ws.ids["S1"]
    ws.invite("member", P)

    async def probe(project_id: str, request: Request):
        first = await _deps._scoped_project_ids_for_request(request)
        second = await _deps._scoped_project_ids_for_request(request)
        return {
            "same": first is second,
            "kind": type(first).__name__,
            "scope": sorted(first),
            "explicit": list(first.explicit),
            "capabilities": first.policy.as_dict(),
        }

    ws.client.app.add_api_route("/projects/{project_id}/probe-scope", probe, methods=["GET"])
    resolver = _count(monkeypatch, mp, "resolve_member_scope")
    authorization_reads = _count(monkeypatch, workspace_db, "get_member_authorization_context")
    children_reads = _count(monkeypatch, db_module, "get_subproject_map")

    r = ws.client.get(f"/projects/{S1}/probe-scope", headers=ws.member_headers())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["same"] is True and body["kind"] == "ScopedProjectIds"
    assert body["scope"] == sorted([P, S1]) and body["explicit"] == [P]
    assert body["capabilities"] == dict(mp.BUILTIN_DEFAULTS)
    assert resolver["n"] == 1 and authorization_reads["n"] == 1 and children_reads["n"] == 1


def test_http_unscoped_callers_skip_live_project_expansion(split_workspace, monkeypatch):
    ws = split_workspace
    P = ws.ids["P"]
    wide_tenant = ws.run(db_module.upsert_tenant(ws.auth_db, f"wide-{uniq('x')}@example.com"))
    ws.run(db_module.update_tenant(ws.auth_db, wide_tenant["id"], plan="pro"))
    wide_token, _ = ws.run(db_module.create_api_token(ws.auth_db, wide_tenant["id"]))
    ws.run(ws.dbs.invite("member", None, email=wide_tenant["email"]))
    stranger = ws.run(db_module.upsert_tenant(ws.auth_db, f"stranger-{uniq('x')}@example.com"))
    ws.run(db_module.update_tenant(ws.auth_db, stranger["id"], plan="pro"))
    stranger_token, _ = ws.run(db_module.create_api_token(ws.auth_db, stranger["id"]))
    stranger_db = ws.run(db_module.init_db(":memory:"))
    _deps._tenant_db_cache[stranger["id"]] = stranger_db
    ws.invite("member", P)  # the scoped member exists too, for the control

    authorization = _count(monkeypatch, workspace_db, "get_member_authorization_context")
    children = _count(monkeypatch, db_module, "get_subproject_map")
    resolver = _count(monkeypatch, mp, "resolve_member_scope")

    try:
        # the owner, with no header and with a header naming their own tenant
        for headers in (ws.owner_headers(), {**ws.owner_headers(), "X-Workspace-Tenant-Id": ws.owner["id"]}):
            assert ws.client.get("/projects", headers=headers).status_code == 200
            assert ws.client.get(f"/projects/{P}/tasks", headers=headers).status_code == 200
        assert resolver["n"] == 0 and authorization["n"] == 0 and children["n"] == 0

        # a workspace-wide member sees every project and still costs nothing extra
        wide = {"Authorization": f"Bearer {wide_token}", "X-Workspace-Tenant-Id": ws.owner["id"]}
        assert {p["id"] for p in ws.client.get("/projects", headers=wide).json()} == {
            ws.ids[k] for k in ("P", "S1", "F", "FS")}
        assert ws.client.get(f"/projects/{ws.ids['F']}/tasks", headers=wide).status_code == 200
        assert authorization["n"] == 2 and children["n"] == 0

        # a non-member naming the workspace is not scoped either (they land on their own DB)
        out = {"Authorization": f"Bearer {stranger_token}", "X-Workspace-Tenant-Id": ws.owner["id"]}
        assert ws.client.get(f"/projects/{P}/tasks", headers=out).status_code in (200, 404)
        assert authorization["n"] == 3 and children["n"] == 0

        # control: the scoped member reads context and live children
        assert _listing(ws) == {P, ws.ids["S1"]}
        assert authorization["n"] == 4 and children["n"] == 1
    finally:
        _deps._tenant_db_cache.pop(stranger["id"], None)
        ws.run(stranger_db.close())


@pytest.mark.parametrize(
    ("target", "name"),
    [(db_module, "get_subproject_map"), (workspace_db, "get_member_authorization_context")],
)
def test_http_a_failing_resolution_step_is_a_503_with_the_header_and_harmless_without(
    split_workspace, monkeypatch, target, name,
):
    ws = split_workspace
    P = ws.ids["P"]
    ws.invite("member", P)
    assert _tasks(ws, P).status_code == 200

    async def boom(*_a, **_k):
        raise RuntimeError("induced")

    monkeypatch.setattr(target, name, boom)
    for project in (P, ws.ids["F"]):
        r = _tasks(ws, project)
        assert r.status_code == 503 and r.json() == {"detail": "scope check unavailable"}, (project, r.text)
    # the tool is not run on /mcp either
    rpc = ws.client.post("/mcp", headers=ws.member_headers(), json={
        "jsonrpc": "2.0", "id": 9, "method": "tools/call",
        "params": {"name": "get_tasks", "arguments": {"project_id": P}}})
    assert rpc.status_code == 503
    assert rpc.json()["error"]["message"] == "scope check unavailable" and "result" not in rpc.json()
    # callers without the workspace header never reach the resolver
    assert ws.client.get(f"/projects/{P}/tasks", headers=ws.owner_headers()).status_code in (200, 404)
    assert ws.client.get("/projects", headers=ws.owner_headers()).status_code == 200


def test_http_an_unreachable_owner_database_is_a_503_never_an_unscoped_pass(split_workspace):
    ws = split_workspace
    ws.invite("member", ws.ids["P"])
    _deps._tenant_db_cache.pop(ws.owner["id"])  # the owner's pool is gone and cannot be reopened
    try:
        for project in (ws.ids["P"], ws.ids["F"]):
            r = _tasks(ws, project)
            assert r.status_code == 503 and r.json() == {"detail": "scope check unavailable"}
    finally:
        _deps._tenant_db_cache[ws.owner["id"]] = ws.owner_db


def test_http_the_patch_seam_still_works_and_a_plain_list_only_breaks_policy_code(split_workspace, monkeypatch):
    ws = split_workspace
    P, F = ws.ids["P"], ws.ids["F"]
    ws.invite("member", F)  # the real scope is F; the patch below says P

    async def patched(_request):
        return [P]  # a plain list, the way older tests patch the resolver

    monkeypatch.setattr(_deps, "_scoped_project_ids_for_request", patched)
    assert _tasks(ws, P).status_code == 200  # consumers of the flat list are unaffected
    assert _tasks(ws, F).status_code == 403
    with pytest.raises(mp.MemberPolicyUnavailable):  # but policy code refuses a plain list in hosted mode
        mp.require_capability([P], mp.WRITE_PROJECT)

    async def carried(_request):
        return mp.make_scope([P], explicit=[P])

    monkeypatch.setattr(_deps, "_scoped_project_ids_for_request", carried)
    assert _tasks(ws, P).status_code == 200 and _tasks(ws, F).status_code == 403


# ---------------------------------------------------------------------------
# the single-caller rule
# ---------------------------------------------------------------------------

_REPO = Path(__file__).resolve().parent.parent
_CALL = re.compile(r"get_scoped_project_ids_for_member\s*\(")


def _callers() -> set[str]:
    found = set()
    for path in (_REPO / "meridian").rglob("*.py"):
        rel = path.relative_to(_REPO).as_posix()
        if rel.startswith("meridian/db/"):
            continue  # the definition and the package re-export
        if _CALL.search(path.read_text(encoding="utf-8", errors="replace")):
            found.add(rel)
    return found


def test_only_the_sse_per_message_path_still_uses_the_legacy_scope_lookup():
    """``db.get_scoped_project_ids_for_member`` returns the EXPLICIT grants only.
    The regular request resolver uses the combined authorization-context read;
    only server.py's per-SSE-message lookup remains for its own lane."""
    assert _callers() == {"meridian/server.py"}


@pytest.mark.xfail(
    strict=False,
    reason="lane L4 replaces the direct lookup at meridian/server.py (SSE per message) with "
           "resolve_member_scope; once it has, drop this marker and fold the check into the test above",
)
def test_the_sse_per_message_lookup_goes_through_the_resolver():
    assert _callers() == {"meridian/member_policy.py"}
