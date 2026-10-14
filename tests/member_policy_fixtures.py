"""Shared fixtures for the project-scoped member policy tests.

The rule every member-policy test follows: use SPLIT databases. A member's
grants and policy live in the AUTH database (``workspace_members``,
``member_policy``); the projects, and so the subproject hierarchy, live in the
workspace OWNER's tenant database. Tests that put both in one database (the way
the older hosted tests do, with an admin-plan owner whose workspace *is* the auth
DB) cannot tell a resolver that expands the scope against the wrong database from
a correct one: the wrong-DB expansion is silent and under-inclusive.

Three layers:

* :func:`build_split_dbs` / the ``split_dbs`` fixture: two bare databases, no
  app. For the resolver and for statement-trace tests.
* :func:`StatementTrace`: wraps a connection and records every statement.
* the ``hosted_client`` and ``split_workspace`` fixtures: a hosted TestClient on a
  fresh server import, and a workspace on it whose owner has a database of their
  own (injected in ``_deps._tenant_db_cache`` exactly where the Neon pool would
  sit), with a project-scoped member who talks to it through a Bearer token and
  the ``X-Workspace-Tenant-Id`` header.

Import with ``from member_policy_fixtures import ...`` (the tests directory is on
``sys.path``) and re-export the fixtures you use at the top of the test module:
``from member_policy_fixtures import hosted_client, split_dbs, split_workspace``.
"""
from __future__ import annotations

import asyncio
import contextlib
import importlib
import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterator

import pytest
import pytest_asyncio

from meridian import _deps
from meridian import db as db_module


def uniq(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# Statement tracing
# ---------------------------------------------------------------------------

class StatementTrace:
    """Wrap a database connection and record every statement run through it.

    ``execute`` and ``cursor().execute`` are covered; anything else is passed
    through untouched. ``statements`` holds the SQL text in order. With
    ``explode=True`` any statement raises, which is how a test proves a code
    path touches the database NOT AT ALL.
    """

    def __init__(self, inner: Any, *, explode: bool = False, label: str = "db") -> None:
        self._inner = inner
        self._explode = explode
        self._label = label
        self.statements: list[str] = []

    def _record(self, sql: str) -> None:
        self.statements.append(" ".join(str(sql).split()))
        if self._explode:
            raise AssertionError(f"{self._label} must not be touched, ran: {self.statements[-1][:120]}")

    def execute(self, sql, *args, **kwargs):
        self._record(sql)
        return self._inner.execute(sql, *args, **kwargs)

    def cursor(self, *args, **kwargs):
        return _TracedCursor(self, self._inner.cursor(*args, **kwargs))

    def __getattr__(self, name: str):
        return getattr(self._inner, name)

    def reset(self) -> None:
        self.statements.clear()

    def tables(self) -> list[str]:
        """The table each recorded statement reads or writes (first FROM/INTO/UPDATE)."""
        import re

        out = []
        for sql in self.statements:
            m = re.search(r"\b(?:FROM|INTO|UPDATE|JOIN)\s+([a-z_]+)", sql, re.IGNORECASE)
            out.append(m.group(1).lower() if m else "?")
        return out


class _TracedCursor:
    def __init__(self, trace: StatementTrace, inner: Any) -> None:
        self._trace = trace
        self._inner = inner

    def execute(self, sql, *args, **kwargs):
        self._trace._record(sql)
        return self._inner.execute(sql, *args, **kwargs)

    def __getattr__(self, name: str):
        return getattr(self._inner, name)

    async def __aenter__(self):
        await self._inner.__aenter__()
        return self

    async def __aexit__(self, *exc):
        return await self._inner.__aexit__(*exc)


# ---------------------------------------------------------------------------
# Layer 1: two bare databases
# ---------------------------------------------------------------------------

@dataclass
class SplitDbs:
    """``auth_db`` holds tenants, workspace_members and member_policy;
    ``tenant_db`` is the workspace owner's database and holds the projects."""

    auth_db: Any
    tenant_db: Any
    owner: dict[str, Any]
    member: dict[str, Any]
    ids: dict[str, str] = field(default_factory=dict)

    async def invite(self, role: str = "member", project_id: "str | None" = None,
                     *, email: "str | None" = None, accepted: bool = True) -> dict[str, Any]:
        """Add one ``workspace_members`` row for the member (or ``email``)."""
        row = await db_module.create_workspace_invite(
            self.auth_db, self.owner["id"], email or self.member["email"], role,
            f"tok-{uuid.uuid4().hex}", project_id=project_id,
        )
        if accepted:
            row = await db_module.accept_workspace_invite(self.auth_db, row["id"])
        return row

    async def project(self, name: "str | None" = None, parent: "str | None" = None) -> str:
        """Create a project in the OWNER's database; returns its id."""
        created = await db_module.create_project(
            self.tenant_db, name or uniq("proj"), parent_project_id=parent,
        )
        return created["id"]

    async def set_default(self, policy: Any) -> None:
        text = policy if isinstance(policy, str) else json.dumps(policy)
        await db_module.set_member_policy_default(self.auth_db, self.owner["id"], text, "test")

    async def set_override(self, policy: Any, email: "str | None" = None) -> None:
        text = policy if isinstance(policy, str) else json.dumps(policy)
        await db_module.set_member_policy_override(
            self.auth_db, self.owner["id"], email or self.member["email"], text, "test",
        )

    async def raw_policy_row(self, subject: str, text: str) -> None:
        """Store ``text`` verbatim (corrupt JSON included) as a policy row,
        bypassing the validation of the set_* functions."""
        await self.auth_db.execute(
            "DELETE FROM member_policy WHERE tenant_id = ? AND subject = ?",
            (self.owner["id"], subject),
        )
        await self.auth_db.execute(
            "INSERT INTO member_policy (tenant_id, subject, policy, updated_by, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (self.owner["id"], subject, text, "test", "2026-01-01 00:00:00"),
        )
        await self.auth_db.commit()

    async def resolve(self, **kwargs):
        """``member_policy.resolve_member_scope`` for the member against the two DBs."""
        from meridian import member_policy

        return await member_policy.resolve_member_scope(
            kwargs.get("auth_db", self.auth_db),
            kwargs.get("tenant_db", self.tenant_db),
            kwargs.get("workspace_tenant_id", self.owner["id"]),
            kwargs.get("email", self.member["email"]),
        )


async def build_split_dbs(auth_db: Any, *, tenant_db: Any = None) -> SplitDbs:
    """Seed an owner and a member into ``auth_db`` and give the owner a separate
    tenant database (a fresh in-memory SQLite one unless ``tenant_db`` is given).

    The standard hierarchy in the owner's database: ``P`` (top-level) with the
    subproject ``S1``, and a foreign top-level ``F`` with its subproject ``FS``.
    No ``workspace_members`` row exists yet; call :meth:`SplitDbs.invite`.
    """
    owner = await db_module.upsert_tenant(auth_db, f"owner-{uuid.uuid4().hex[:8]}@example.com")
    member = await db_module.upsert_tenant(auth_db, f"member-{uuid.uuid4().hex[:8]}@example.com")
    if tenant_db is None:
        tenant_db = await db_module.init_db(":memory:")
    dbs = SplitDbs(auth_db=auth_db, tenant_db=tenant_db, owner=owner, member=member)
    dbs.ids["P"] = await dbs.project(uniq("P"))
    dbs.ids["S1"] = await dbs.project(uniq("S1"), parent=dbs.ids["P"])
    dbs.ids["F"] = await dbs.project(uniq("F"))
    dbs.ids["FS"] = await dbs.project(uniq("FS"), parent=dbs.ids["F"])
    return dbs


@pytest_asyncio.fixture
async def split_dbs(anydb):
    """The auth database is ``anydb`` (SQLite, and Postgres in the CI run); the
    owner's tenant database is a separate in-memory SQLite connection."""
    dbs = await build_split_dbs(anydb)
    try:
        yield dbs
    finally:
        await dbs.tenant_db.close()


# ---------------------------------------------------------------------------
# Layer 2: a hosted app with a split workspace
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def boot_hosted_client(monkeypatch, tmp_path) -> Iterator[Any]:
    """A hosted-mode TestClient on a fresh server import (same recipe as the
    older hosted tests; the auth database is the app's in-memory ``state.db``)."""
    monkeypatch.setenv("MERIDIAN_HOSTED", "true")
    monkeypatch.setenv("MERIDIAN_SESSION_SECRET", "test-secret")
    monkeypatch.setenv("MERIDIAN_DB", ":memory:")
    monkeypatch.setenv("MERIDIAN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MERIDIAN_DB_URL", "")
    monkeypatch.setenv("MERIDIAN_DEMO_DB_URL", "")
    monkeypatch.setenv("MERIDIAN_AUTH_DB", "")
    monkeypatch.setenv("MERIDIAN_SKIP_DEMO", "1")
    monkeypatch.setenv("MERIDIAN_GOAL_MD", str(tmp_path / "GOAL.md"))
    monkeypatch.setenv("MERIDIAN_MD_ROOT", str(tmp_path))
    import meridian.server as server_module
    from fastapi.testclient import TestClient

    _deps._reset_limiter_counts()
    with TestClient(importlib.reload(server_module).app) as client:
        yield client
    _deps._reset_limiter_counts()


@pytest.fixture
def hosted_client(monkeypatch, tmp_path):
    with boot_hosted_client(monkeypatch, tmp_path) as client:
        yield client


@dataclass
class SplitWorkspace:
    """A hosted workspace whose owner has a tenant database of their own."""

    client: Any
    dbs: SplitDbs
    owner_token: str
    member_token: str
    member_db: Any

    @property
    def auth_db(self):
        return self.dbs.auth_db

    @property
    def owner_db(self):
        return self.dbs.tenant_db

    @property
    def owner(self):
        return self.dbs.owner

    @property
    def member(self):
        return self.dbs.member

    @property
    def ids(self):
        return self.dbs.ids

    def member_headers(self, **extra: str) -> dict[str, str]:
        """The scoped member talking to the owner's workspace."""
        return {"Authorization": f"Bearer {self.member_token}",
                "X-Workspace-Tenant-Id": self.owner["id"], **extra}

    def owner_headers(self) -> dict[str, str]:
        """The owner in their own workspace: no workspace header."""
        return {"Authorization": f"Bearer {self.owner_token}"}

    def run(self, coro):
        return asyncio.run(coro)

    def invite(self, role: str = "member", project_id: "str | None" = None, **kw) -> dict[str, Any]:
        return self.run(self.dbs.invite(role, project_id, **kw))

    def project(self, name: "str | None" = None, parent: "str | None" = None) -> str:
        return self.run(self.dbs.project(name, parent))

    def set_default(self, policy: Any) -> None:
        self.run(self.dbs.set_default(policy))

    def set_override(self, policy: Any, email: "str | None" = None) -> None:
        self.run(self.dbs.set_override(policy, email))

    def owner_rows(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        """Raw SELECT against the OWNER's database (the unchanged-state proof)."""
        async def _go():
            async with self.owner_db.execute(sql, params) as cur:
                return [dict(r) for r in await cur.fetchall()]
        return self.run(_go())


@pytest.fixture
def split_workspace(hosted_client):
    """``SplitWorkspace`` on the hosted client: owner and member are non-admin
    ``pro`` tenants, each with a database of their own in the tenant-DB cache.
    The member has NO ``workspace_members`` row yet: call ``.invite(...)``."""
    c = hosted_client
    auth_db = c.app.state.db

    async def _seed():
        dbs = await build_split_dbs(auth_db)
        for tenant in (dbs.owner, dbs.member):
            await db_module.update_tenant(auth_db, tenant["id"], plan="pro")
        owner_token, _ = await db_module.create_api_token(auth_db, dbs.owner["id"])
        member_token, _ = await db_module.create_api_token(auth_db, dbs.member["id"])
        member_db = await db_module.init_db(":memory:")
        return dbs, owner_token, member_token, member_db

    dbs, owner_token, member_token, member_db = asyncio.run(_seed())
    _deps._tenant_db_cache[dbs.owner["id"]] = dbs.tenant_db
    _deps._tenant_db_cache[dbs.member["id"]] = member_db
    ws = SplitWorkspace(c, dbs, owner_token, member_token, member_db)
    try:
        yield ws
    finally:
        _deps._tenant_db_cache.pop(dbs.owner["id"], None)
        _deps._tenant_db_cache.pop(dbs.member["id"], None)
        asyncio.run(dbs.tenant_db.close())
        asyncio.run(member_db.close())
