"""Workspace-level writes must not reach the owner's database for a project-scoped member.

A project-scoped member of someone else's workspace reaches the owner's database through the
``X-Workspace-Tenant-Id`` header. The read side is covered in ``test_workspace_scope_leak.py``.
This file covers the write side.

What is enforced (the rule the tests below pin):

* every HTTP ``/workspace`` write to notes, decisions, settings, blog posts and the sprint
  board is refused for a project-scoped caller with 403 and the shared
  ``WORKSPACE_SCOPE_DENIED`` body, whatever their ``write_workspace_level`` grant. The
  middleware in ``server.py`` and the route guards both do this. Before this change a default
  member got the capability refusal body and a grant let the write through;
* the matching MCP write tools are in ``scope_guard.WORKSPACE_DENY`` and refused for a
  scoped caller regardless of grant. Before this change ``request_manual_issue_screening_toggle``
  (an ordinary ``project_write``) changed the owner's settings for a default member, and
  ``move_workspace_note_to_project`` moved an owner note into a project the member could read;
* the note move is refused on HTTP and MCP for a scoped caller, into any project;
* ``add_workspace_proposal`` is not covered by this rule. Proposals can be project-scoped, so
  it stays behind the owner-grantable ``write_workspace_level`` capability (see
  ``test_member_policy_http.py``);
* owners keep every write (strict 2xx on HTTP, a successful result over MCP);
* workspace-wide members are not refused by the scope rule (the same status they get today).
"""
from __future__ import annotations

import asyncio
import importlib
import json
from datetime import datetime, timezone

import pytest

import meridian.server  # noqa: F401 -- load the server first (import-cycle ordering)
from meridian import db as db_module
from meridian.mcp import scope_guard
from meridian.mcp_tools import member_policy_operation

WORKSPACE_DENIED = "Workspace-level data is not available to project-scoped members."
SCOPED_EMAIL = "wswrite-scoped@example.com"

#: Every table a workspace write can change. The snapshot reads all rows of each table with
#: no tenant filter, so a write stamped with any tenant id (or none) shows up as a change.
SNAPSHOT_TABLES = (
    "workspace_notes",
    "workspace_decisions",
    "workspace_settings",
    "workspace_sprint_items",
    "blog_posts",
    "workspace_proposals",
)

#: HTTP write cases, in an order that is valid for an owner (each case only touches rows no
#: earlier case removed): patch before move, delete a different note than the one moved.
HTTP_WRITE_CASES = (
    "create_note",
    "patch_note",
    "delete_note",
    "move_note",
    "create_decision",
    "delete_decision",
    "patch_settings",
    "create_blog_post",
    "create_sprint_item",
    "patch_sprint_item",
    "complete_sprint_item",
)

#: MCP workspace writes that stay behind the ``write_workspace_level`` capability (proposals
#: can be project-scoped, so the scope rule does not apply to them).
POLICY_GATED_MCP_WRITES = (
    "add_workspace_proposal",
)

#: MCP workspace writes refused outright for a project-scoped caller (``WORKSPACE_DENY``),
#: whatever their grant: notes, decisions, settings, blog posts, the sprint board, the
#: screening toggle and the note move.
SCOPE_DENIED_MCP_WRITES = (
    "add_workspace_note",
    "pin_workspace_decision",
    "update_workspace_settings",
    "save_blog_post",
    "add_workspace_sprint_item",
    "update_workspace_sprint_item",
    "complete_workspace_sprint_item",
    "request_manual_issue_screening_toggle",
    "move_workspace_note_to_project",
)


# ---------------------------------------------------------------------------
# Hosted fixtures (same recipe and seeding shape as tests/test_scope_http_wave2.py)
# ---------------------------------------------------------------------------

def _boot_hosted_client(monkeypatch, tmp_path):
    monkeypatch.setenv("MERIDIAN_HOSTED", "true")
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

    return TestClient(importlib.reload(server_module).app)


async def _row_id(db, table: str, title: str) -> str:
    async with db.execute(f"SELECT id FROM {table} WHERE title = ?", (title,)) as cur:
        row = await cur.fetchone()
    return str(row[0])


async def _seed(db) -> dict:
    """Owner workspace with owner-stamped and pre-isolation (NULL tenant) rows; a scoped ADMIN
    member of project A; a workspace-wide member. Returns the tokens, the owner id, the project
    ids and the ids of the rows the write cases target."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    owner = await db_module.upsert_tenant(db, "wswrite-owner@example.com")
    await db.execute("UPDATE tenants SET plan='admin' WHERE id=?", (owner["id"],))
    scoped = await db_module.upsert_tenant(db, SCOPED_EMAIL)
    wide = await db_module.upsert_tenant(db, "wswrite-wide@example.com")
    tok_owner, _ = await db_module.create_api_token(db, owner["id"])
    tok_scoped, _ = await db_module.create_api_token(db, scoped["id"])
    tok_wide, _ = await db_module.create_api_token(db, wide["id"])
    proj_a = (await db_module.create_project(db, "wswrite-proj-a"))["id"]
    proj_b = (await db_module.create_project(db, "wswrite-proj-b"))["id"]
    for mid, email, role, pid in (
        ("wswrite-wm-scoped", SCOPED_EMAIL, "admin", proj_a),
        ("wswrite-wm-wide", "wswrite-wide@example.com", "member", None),
    ):
        await db.execute(
            "INSERT INTO workspace_members "
            "(id, tenant_id, email, role, github_access, joined_at, project_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (mid, owner["id"], email, role, "none", now, pid),
        )
    await db.commit()

    oid = owner["id"]
    await db_module.add_workspace_note(db, "owner-note", "owner body", None, tenant_id=oid)
    await db_module.add_workspace_note(db, "owner-note-2", "owner body 2", None, tenant_id=oid)
    await db_module.add_workspace_note(db, "legacy-note", "legacy body", None, tenant_id=None)
    await db_module.pin_workspace_decision(db, "owner-decision", "owner decision body", "TECHNICAL", tenant_id=oid)
    await db_module.update_workspace_settings(db, display_name="Owner Workspace", tenant_id=oid)
    await db_module.save_blog_post(db, "owner-post", "owner post body", status="published", tenant_id=oid)
    await db_module.add_workspace_sprint_item(db, "owner-sprint-item", tenant_id=oid)
    return {
        "owner_id": oid, "proj_a": proj_a, "proj_b": proj_b,
        "tok_owner": tok_owner, "tok_scoped": tok_scoped, "tok_wide": tok_wide,
        "note_id": await _row_id(db, "workspace_notes", "owner-note"),
        "note2_id": await _row_id(db, "workspace_notes", "owner-note-2"),
        "legacy_note_id": await _row_id(db, "workspace_notes", "legacy-note"),
        "decision_id": await _row_id(db, "workspace_decisions", "owner-decision"),
        "item_id": await _row_id(db, "workspace_sprint_items", "owner-sprint-item"),
    }


async def _snapshot(db) -> dict:
    """Every row of every workspace table, across all tenants (no tenant filter)."""
    out: dict = {}
    for table in SNAPSHOT_TABLES:
        async with db.execute(f"SELECT * FROM {table}") as cur:
            out[table] = [dict(row) for row in await cur.fetchall()]
    return out


def _changed_tables(before: dict, after: dict) -> list[str]:
    return [t for t in SNAPSHOT_TABLES if before[t] != after[t]]


def _headers(token: str, workspace_id: str | None = None) -> dict:
    headers = {"Authorization": f"Bearer {token}"}
    if workspace_id is not None:
        headers["X-Workspace-Tenant-Id"] = workspace_id
    return headers


def _http_write(case: str, w: dict) -> tuple[str, str, dict | None]:
    """(method, path, json body) for one HTTP write case."""
    return {
        "create_note": ("POST", "/workspace/notes", {"title": "scoped-note", "body": "scoped body"}),
        "patch_note": ("PATCH", f"/workspace/notes/{w['note_id']}", {"title": "scoped-edit"}),
        "delete_note": ("DELETE", f"/workspace/notes/{w['note2_id']}", None),
        "move_note": ("POST", f"/workspace/notes/{w['note_id']}/move", {"project_id": w["proj_a"]}),
        "create_decision": ("POST", "/workspace/decisions", {"title": "scoped-decision", "body": "scoped decision body"}),
        "delete_decision": ("DELETE", f"/workspace/decisions/{w['decision_id']}", None),
        "patch_settings": ("PATCH", "/workspace/settings", {"display_name": "Scoped Rename"}),
        "create_blog_post": ("POST", "/workspace/blog", {"title": "scoped-post", "body": "scoped post body", "status": "published"}),
        "create_sprint_item": ("POST", "/workspace/sprint-items", {"title": "scoped-item"}),
        "patch_sprint_item": ("PATCH", f"/workspace/sprint-items/{w['item_id']}", {"status": "done"}),
        "complete_sprint_item": ("POST", f"/workspace/sprint-items/{w['item_id']}/complete", None),
    }[case]


def _mcp_write_calls(w: dict) -> dict:
    return {
        "add_workspace_note": {"title": "scoped-note", "body": "scoped body"},
        "pin_workspace_decision": {"title": "scoped-decision", "body": "scoped decision body", "category": "TECHNICAL"},
        "update_workspace_settings": {"display_name": "Scoped Rename"},
        "request_manual_issue_screening_toggle": {"enable": False},
        "save_blog_post": {"title": "scoped-post", "body": "scoped post body", "status": "published"},
        "add_workspace_sprint_item": {"title": "scoped-item"},
        "update_workspace_sprint_item": {"item_id": w["item_id"], "status": "done"},
        "complete_workspace_sprint_item": {"item_id": w["item_id"]},
        "move_workspace_note_to_project": {"note_id": w["legacy_note_id"], "project_id": w["proj_a"]},
        "add_workspace_proposal": {"title": "scoped-proposal", "body": "scoped proposal body"},
    }


def _mcp_request(name: str, args: dict, rid: int = 1) -> dict:
    return {
        "jsonrpc": "2.0", "id": rid, "method": "tools/call",
        "params": {"name": name, "arguments": args},
    }


def _mcp_refused(resp) -> bool:
    """True when an MCP tools/call response is a refusal: a JSON-RPC error, an isError result,
    or a tool result whose payload is an ``{"error": ...}`` object."""
    body = resp.json()
    if "error" in body:
        return True
    result = body.get("result") or {}
    if result.get("isError"):
        return True
    for part in result.get("content") or []:
        try:
            payload = json.loads(part.get("text") or "")
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict) and "error" in payload:
            return True
    return False


# ---------------------------------------------------------------------------
# 1. HTTP: a default project-scoped member is refused on every workspace write, no change
# ---------------------------------------------------------------------------

def test_default_scoped_member_is_refused_on_each_workspace_write_route(monkeypatch, tmp_path):
    with _boot_hosted_client(monkeypatch, tmp_path) as c:
        w = asyncio.run(_seed(c.app.state.db))
        scoped = _headers(w["tok_scoped"], w["owner_id"])
        failures = []
        for case in HTTP_WRITE_CASES:
            before = asyncio.run(_snapshot(c.app.state.db))
            method, path, body = _http_write(case, w)
            r = c.request(method, path, headers=scoped, json=body)
            if r.status_code != 403 or r.json() != {"detail": WORKSPACE_DENIED}:
                failures.append(f"{case}: HTTP {r.status_code} {r.text[:120]}")
            changed = _changed_tables(before, asyncio.run(_snapshot(c.app.state.db)))
            if changed:
                failures.append(f"{case}: changed owner tables {changed}")
        assert not failures, "\n".join(failures)


def test_scoped_member_with_workspace_grant_is_still_refused_on_each_workspace_write_route(monkeypatch, tmp_path):
    """The owner's write_workspace_level grant does not lift the rule for a project-scoped member:
    workspace notes, decisions, settings, blog posts and the sprint board belong to no project."""
    with _boot_hosted_client(monkeypatch, tmp_path) as c:
        w = asyncio.run(_seed(c.app.state.db))
        asyncio.run(db_module.set_member_policy_override(
            c.app.state.db, w["owner_id"], SCOPED_EMAIL,
            {"write_workspace_level": True}, "wswrite-grant-all",
        ))
        scoped = _headers(w["tok_scoped"], w["owner_id"])
        failures = []
        for case in HTTP_WRITE_CASES:
            before = asyncio.run(_snapshot(c.app.state.db))
            method, path, body = _http_write(case, w)
            r = c.request(method, path, headers=scoped, json=body)
            if r.status_code != 403 or r.json() != {"detail": WORKSPACE_DENIED}:
                failures.append(f"{case}: HTTP {r.status_code} {r.text[:120]}")
            changed = _changed_tables(before, asyncio.run(_snapshot(c.app.state.db)))
            if changed:
                failures.append(f"{case}: changed owner tables {changed}")
        assert not failures, "\n".join(failures)


@pytest.mark.parametrize("actor", ["owner", "wide"])
def test_owner_and_workspace_wide_member_keep_each_workspace_write(monkeypatch, tmp_path, actor):
    with _boot_hosted_client(monkeypatch, tmp_path) as c:
        w = asyncio.run(_seed(c.app.state.db))
        headers = _headers(w["tok_owner"]) if actor == "owner" else _headers(w["tok_wide"], w["owner_id"])
        failures = []
        for case in HTTP_WRITE_CASES:
            method, path, body = _http_write(case, w)
            r = c.request(method, path, headers=headers, json=body)
            if actor == "owner":
                if r.status_code not in (200, 201, 204):
                    failures.append(f"{case}: HTTP {r.status_code} {r.text[:120]}")
            elif WORKSPACE_DENIED in r.text:
                failures.append(f"{case}: refused by the scope rule: {r.text[:120]}")
        assert not failures, "\n".join(failures)


def test_scoped_member_with_workspace_grant_cannot_move_a_workspace_note_over_http(monkeypatch, tmp_path):
    with _boot_hosted_client(monkeypatch, tmp_path) as c:
        w = asyncio.run(_seed(c.app.state.db))
        # The owner grants the workspace capability to this member. The grant lets workspace
        # writes through the policy gate, but moving a workspace note into a project hands the
        # member the note's content, so the scope rule must still refuse it.
        asyncio.run(db_module.set_member_policy_override(
            c.app.state.db, w["owner_id"], SCOPED_EMAIL,
            {"write_workspace_level": True}, "wswrite-grant-test",
        ))
        before = asyncio.run(_snapshot(c.app.state.db))
        r = c.post(
            f"/workspace/notes/{w['legacy_note_id']}/move",
            headers=_headers(w["tok_scoped"], w["owner_id"]),
            json={"project_id": w["proj_a"]},
        )
        assert r.status_code == 403, r.text
        assert r.json() == {"detail": WORKSPACE_DENIED}
        assert _changed_tables(before, asyncio.run(_snapshot(c.app.state.db))) == []


# ---------------------------------------------------------------------------
# 2. MCP: the workspace write tools are refused for a scoped member over the real /mcp endpoint
# ---------------------------------------------------------------------------

def test_the_workspace_write_tools_are_denied_or_policy_gated():
    for name in SCOPE_DENIED_MCP_WRITES:
        assert name in scope_guard.WORKSPACE_DENY, name
    for name in POLICY_GATED_MCP_WRITES:
        assert member_policy_operation(name) == "workspace_write", name


def test_hosted_scoped_member_is_refused_on_workspace_write_tools_over_mcp(monkeypatch, tmp_path):
    with _boot_hosted_client(monkeypatch, tmp_path) as c:
        w = asyncio.run(_seed(c.app.state.db))
        scoped = _headers(w["tok_scoped"], w["owner_id"])
        failures = []
        for name, args in _mcp_write_calls(w).items():
            before = asyncio.run(_snapshot(c.app.state.db))
            resp = c.post("/mcp", headers=scoped, json=_mcp_request(name, args))
            if not _mcp_refused(resp):
                failures.append(f"{name}: not refused: {resp.text[:200]}")
            changed = _changed_tables(before, asyncio.run(_snapshot(c.app.state.db)))
            if changed:
                failures.append(f"{name}: changed owner tables {changed}")
        assert not failures, "\n".join(failures)


def test_scoped_member_with_workspace_grant_is_still_refused_on_workspace_write_tools_over_mcp(monkeypatch, tmp_path):
    """Same rule over MCP: a grant does not let a project-scoped member write workspace-level
    data. add_workspace_proposal is excluded: proposals are not in the workspace write set the
    task covers (they can be project-scoped), so they stay behind write_workspace_level."""
    with _boot_hosted_client(monkeypatch, tmp_path) as c:
        w = asyncio.run(_seed(c.app.state.db))
        asyncio.run(db_module.set_member_policy_override(
            c.app.state.db, w["owner_id"], SCOPED_EMAIL,
            {"write_workspace_level": True}, "wswrite-grant-mcp",
        ))
        scoped = _headers(w["tok_scoped"], w["owner_id"])
        failures = []
        for name, args in _mcp_write_calls(w).items():
            if name == "add_workspace_proposal":
                continue
            before = asyncio.run(_snapshot(c.app.state.db))
            resp = c.post("/mcp", headers=scoped, json=_mcp_request(name, args))
            if not _mcp_refused(resp):
                failures.append(f"{name}: not refused: {resp.text[:200]}")
            changed = _changed_tables(before, asyncio.run(_snapshot(c.app.state.db)))
            if changed:
                failures.append(f"{name}: changed owner tables {changed}")
        assert not failures, "\n".join(failures)


def test_owner_mcp_workspace_write_tools_still_succeed(monkeypatch, tmp_path):
    with _boot_hosted_client(monkeypatch, tmp_path) as c:
        w = asyncio.run(_seed(c.app.state.db))
        owner = _headers(w["tok_owner"])
        failures = []
        for name, args in _mcp_write_calls(w).items():
            resp = c.post("/mcp", headers=owner, json=_mcp_request(name, args))
            if _mcp_refused(resp):
                failures.append(f"{name}: refused for the owner: {resp.text[:200]}")
        assert not failures, "\n".join(failures)
