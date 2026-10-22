"""Workspace-level reads must not leak to project-scoped members.

A project-scoped member of someone else's workspace (an invite to one project, plus the
one-level subprojects of that grant) reaches the owner's database through the
``X-Workspace-Tenant-Id`` header. Project-owned data is scoped server-side; the
workspace-level data (notes, decisions, settings, blog posts, the workspace sprint board)
belongs to no project, so a project grant does not cover it. The same is true of the MCP
tools that serve that data and of the server-wide log tools.

Expected behaviour after the fix:

* a project-scoped caller gets 403 (HTTP) or a scope refusal (MCP) on every workspace read;
* owners and workspace-wide members are unchanged (no regression);
* self-hosted callers never reach the guard (the scope resolver returns ``None``).

The rows seeded here come in two kinds. Owner-stamped rows carry the owner's ``tenant_id``,
as the owner's own dashboard writes them. Pre-isolation rows carry ``tenant_id IS NULL``;
the workspace tenant filter (``tenant_id = caller OR tenant_id IS NULL``) lets those through
to every caller of the owner's database, so they are the rows a scoped member would read.
"""
from __future__ import annotations

import asyncio
import importlib
import json
from datetime import datetime, timezone

import pytest

import meridian.server  # noqa: F401 -- load the server first (import-cycle ordering)
from meridian import _deps
from meridian import db as db_module
from meridian.mcp import handler as mcp_handler
from meridian.mcp import scope_guard
from meridian.member_policy import CAPABILITY_IDS, EffectivePolicy, ScopedProjectIds
from meridian.roles import ROLE_MEMBER

WORKSPACE_DENIED = "Workspace-level data is not available to project-scoped members."
SCOPE_ERROR = "outside your access scope"

WORKSPACE_READS = (
    "/workspace/notes",
    "/workspace/decisions",
    "/workspace/settings",
    "/workspace/blog",
    "/workspace/sprint-items",
)

#: The MCP tools that serve the same workspace-level or server-wide data, with arguments
#: that satisfy their schemas (the refusal happens before the handler runs either way).
WORKSPACE_TOOLS = (
    ("get_workspace_notes", {}),
    ("get_workspace_decisions", {}),
    ("get_workspace_settings", {}),
    ("get_workspace_sprint_items", {}),
    ("get_server_logs", {}),
    ("search_server_logs", {"query": "error"}),
    ("get_connection_log", {}),
    ("get_server_log_checkpoint", {}),
    ("get_latex_structure", {"file_path": "thesis/chapter1.tex"}),
    # Workspace-level writes that no workspace capability gates (see test_workspace_scope_writes.py).
    ("request_manual_issue_screening_toggle", {"enable": False}),
    ("move_workspace_note_to_project", {"note_id": "not-a-note"}),
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


async def _seed_workspace(db) -> dict:
    """Owner workspace: a scoped ADMIN member (project A), a workspace-wide member, and rows."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    owner = await db_module.upsert_tenant(db, "wsleak-owner@example.com")
    await db.execute("UPDATE tenants SET plan='admin' WHERE id=?", (owner["id"],))
    scoped = await db_module.upsert_tenant(db, "wsleak-scoped@example.com")
    wide = await db_module.upsert_tenant(db, "wsleak-wide@example.com")
    tok_owner, _ = await db_module.create_api_token(db, owner["id"])
    tok_scoped, _ = await db_module.create_api_token(db, scoped["id"])
    tok_wide, _ = await db_module.create_api_token(db, wide["id"])
    proj_a = (await db_module.create_project(db, "wsleak-proj-a"))["id"]
    proj_b = (await db_module.create_project(db, "wsleak-proj-b"))["id"]
    for mid, email, role, pid in (
        ("wsleak-wm-scoped", "wsleak-scoped@example.com", "admin", proj_a),
        ("wsleak-wm-wide", "wsleak-wide@example.com", "member", None),
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
    await db_module.pin_workspace_decision(db, "owner-decision", "owner decision body", "TECHNICAL", tenant_id=oid)
    await db_module.update_workspace_settings(db, display_name="Owner Workspace", tenant_id=oid)
    await db_module.save_blog_post(db, "owner-post", "owner post body", status="published", tenant_id=oid)
    await db_module.add_workspace_sprint_item(db, "owner-sprint-item", tenant_id=oid)
    await db_module.add_workspace_note(db, "legacy-note", "legacy body", None, tenant_id=None)
    await db_module.pin_workspace_decision(db, "legacy-decision", "legacy decision body", "TECHNICAL", tenant_id=None)
    await db_module.save_blog_post(db, "legacy-post", "legacy post body", status="published", tenant_id=None)
    await db_module.add_workspace_sprint_item(db, "legacy-sprint-item", tenant_id=None)
    return {
        "owner_id": oid, "proj_a": proj_a, "proj_b": proj_b,
        "tok_owner": tok_owner, "tok_scoped": tok_scoped, "tok_wide": tok_wide,
    }


def _headers(token: str, workspace_id: str | None = None) -> dict:
    headers = {"Authorization": f"Bearer {token}"}
    if workspace_id is not None:
        headers["X-Workspace-Tenant-Id"] = workspace_id
    return headers


# ---------------------------------------------------------------------------
# 1. HTTP: a project-scoped member is refused on every workspace read
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", WORKSPACE_READS)
def test_project_scoped_member_is_refused_on_each_workspace_read(monkeypatch, tmp_path, path):
    with _boot_hosted_client(monkeypatch, tmp_path) as c:
        w = asyncio.run(_seed_workspace(c.app.state.db))
        r = c.get(path, headers=_headers(w["tok_scoped"], w["owner_id"]))
        assert r.status_code == 403, r.text
        assert r.json() == {"detail": WORKSPACE_DENIED}
        # The denial carries none of the owner's rows.
        for leaked in ("owner-", "legacy-", "Owner Workspace"):
            assert leaked not in r.text


@pytest.mark.parametrize("path", WORKSPACE_READS)
def test_owner_and_workspace_wide_member_still_read_each_workspace_route(monkeypatch, tmp_path, path):
    with _boot_hosted_client(monkeypatch, tmp_path) as c:
        w = asyncio.run(_seed_workspace(c.app.state.db))
        owner = c.get(path, headers=_headers(w["tok_owner"]))
        assert owner.status_code == 200, owner.text
        wide = c.get(path, headers=_headers(w["tok_wide"], w["owner_id"]))
        assert wide.status_code == 200, wide.text


def test_owner_reads_their_own_workspace_rows_and_wide_member_keeps_todays_view(monkeypatch, tmp_path):
    with _boot_hosted_client(monkeypatch, tmp_path) as c:
        w = asyncio.run(_seed_workspace(c.app.state.db))
        owner = _headers(w["tok_owner"])
        wide = _headers(w["tok_wide"], w["owner_id"])

        owner_notes = {n["title"] for n in c.get("/workspace/notes", headers=owner).json()}
        assert {"owner-note", "legacy-note"} <= owner_notes
        owner_decisions = {d["title"] for d in c.get("/workspace/decisions", headers=owner).json()}
        assert {"owner-decision", "legacy-decision"} <= owner_decisions
        assert c.get("/workspace/settings", headers=owner).json()["display_name"] == "Owner Workspace"
        assert {p["title"] for p in c.get("/workspace/blog", headers=owner).json()} >= {"owner-post", "legacy-post"}
        assert {i["title"] for i in c.get("/workspace/sprint-items", headers=owner).json()} >= {
            "owner-sprint-item", "legacy-sprint-item",
        }

        # The workspace-wide member keeps the view it has today (no change for them).
        wide_notes = {n["title"] for n in c.get("/workspace/notes", headers=wide).json()}
        assert "legacy-note" in wide_notes


# ---------------------------------------------------------------------------
# 2. HTTP guard contract, self-hosted stub (the resolver is replaced, not the routes)
# ---------------------------------------------------------------------------

def _stub_scope(monkeypatch, scoped):
    async def _fake(request):
        return scoped

    monkeypatch.setattr(_deps, "_scoped_project_ids_for_request", _fake)


@pytest.mark.parametrize("path", WORKSPACE_READS)
def test_any_scoped_result_is_refused_even_an_empty_scope(client, monkeypatch, path):
    _stub_scope(monkeypatch, ScopedProjectIds(
        [], policy=EffectivePolicy(caps=frozenset(CAPABILITY_IDS), role=ROLE_MEMBER),
    ))
    r = client.get(path)
    assert r.status_code == 403, r.text
    assert r.json() == {"detail": WORKSPACE_DENIED}


@pytest.mark.parametrize("path", WORKSPACE_READS)
def test_unscoped_callers_are_unchanged_on_each_workspace_route(client, monkeypatch, path):
    _stub_scope(monkeypatch, None)
    assert client.get(path).status_code == 200


# ---------------------------------------------------------------------------
# 3. MCP: the same data through the tools is refused for a scoped caller
# ---------------------------------------------------------------------------

def _scoped(ids):
    return ScopedProjectIds(
        ids, policy=EffectivePolicy(caps=frozenset(CAPABILITY_IDS), role=ROLE_MEMBER),
    )


async def _mcp_call(db, tmp_path, name, args, scoped):
    return await mcp_handler._dispatch_mcp_tool(
        name, args, db, str(tmp_path), tenant=None, scoped_project_ids=scoped,
    )


def test_the_workspace_deny_table_covers_the_read_tools_under_test():
    # The write tools in WORKSPACE_DENY are checked in test_workspace_scope_writes.py.
    assert {name for name, _ in WORKSPACE_TOOLS} <= set(scope_guard.WORKSPACE_DENY)


@pytest.mark.parametrize("tool,args", WORKSPACE_TOOLS)
async def test_workspace_tool_is_refused_for_a_project_scoped_caller(db, tmp_path, tool, args):
    project = (await db_module.create_project(db, "wsleak-mcp-proj"))["id"]
    await db_module.add_workspace_note(db, "legacy-note", "legacy body", None, tenant_id=None)
    with pytest.raises(ValueError, match=SCOPE_ERROR):
        await _mcp_call(db, tmp_path, tool, args, _scoped([project]))
    # An empty scope is still a scoped caller and is refused the same way.
    with pytest.raises(ValueError, match=SCOPE_ERROR):
        await _mcp_call(db, tmp_path, tool, args, _scoped([]))


@pytest.mark.parametrize("tool,args", WORKSPACE_TOOLS)
async def test_workspace_tool_is_unchanged_for_an_unscoped_caller(db, tmp_path, tool, args):
    try:
        await _mcp_call(db, tmp_path, tool, args, None)
    except Exception as exc:  # noqa: BLE001 -- any outcome but the scope refusal is fine here
        assert SCOPE_ERROR not in str(exc), f"{tool} refused an unscoped caller: {exc}"


def _mcp_request(name: str, args: dict, rid: int = 1) -> dict:
    return {
        "jsonrpc": "2.0", "id": rid, "method": "tools/call",
        "params": {"name": name, "arguments": args},
    }


def _mcp_text_refused(text: str) -> bool:
    """True when a tools/call response body is a refusal. Parsed, because a tool payload is
    JSON-escaped inside the MCP text part, so a raw substring check such as ``'"error"'``
    never matches it."""
    try:
        body = json.loads(text)
    except ValueError:
        return True
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


def test_hosted_scoped_member_is_refused_on_workspace_tools_over_mcp(monkeypatch, tmp_path):
    with _boot_hosted_client(monkeypatch, tmp_path) as c:
        w = asyncio.run(_seed_workspace(c.app.state.db))
        scoped = _headers(w["tok_scoped"], w["owner_id"])
        wide = _headers(w["tok_wide"], w["owner_id"])

        refused = c.post("/mcp", headers=scoped, json=_mcp_request("get_workspace_notes", {}))
        assert refused.status_code == 200, refused.text
        assert SCOPE_ERROR in refused.text
        assert "owner-note" not in refused.text and "legacy-note" not in refused.text

        for name, args in WORKSPACE_TOOLS:
            text = c.post("/mcp", headers=scoped, json=_mcp_request(name, args)).text
            assert SCOPE_ERROR in text, f"{name} was not refused for a scoped member: {text[:300]}"

        # The workspace-wide member's call still runs and returns the note it could see before.
        allowed = c.post("/mcp", headers=wide, json=_mcp_request("get_workspace_notes", {}))
        assert "legacy-note" in allowed.text
        assert SCOPE_ERROR not in allowed.text


# ---------------------------------------------------------------------------
# 4. Context documents: handoff (mode=full), get_context_block and session start
# ---------------------------------------------------------------------------

#: Titles and bodies of every owner-stamped and pre-isolation (NULL tenant) workspace row
#: seeded above. A scoped member's context documents must contain none of them.
OWNER_WORKSPACE_MARKERS = (
    "owner-note", "owner body",
    "owner-decision", "owner decision body",
    "legacy-note", "legacy body",
    "legacy-decision", "legacy decision body",
    "Owner Workspace",
)


def test_scoped_member_full_handoff_carries_no_owner_workspace_rows(monkeypatch, tmp_path):
    with _boot_hosted_client(monkeypatch, tmp_path) as c:
        w = asyncio.run(_seed_workspace(c.app.state.db))
        owner_text = c.post(
            "/mcp", headers=_headers(w["tok_owner"]),
            json=_mcp_request("generate_handoff", {"project_id": w["proj_a"], "mode": "full"}),
        ).text
        # Control: the owner's own full handoff still renders the workspace records.
        assert "owner-decision" in owner_text, owner_text[:300]

        scoped_text = c.post(
            "/mcp", headers=_headers(w["tok_scoped"], w["owner_id"]),
            json=_mcp_request("generate_handoff", {"project_id": w["proj_a"], "mode": "full"}),
        ).text
        assert not _mcp_text_refused(scoped_text), scoped_text[:300]
        # The scoped member still gets a handoff for their own project.
        assert "wsleak-proj-a" in scoped_text, scoped_text[:300]
        for leaked in OWNER_WORKSPACE_MARKERS:
            assert leaked not in scoped_text, f"{leaked!r} leaked into a scoped full handoff"


def test_scoped_member_http_full_handoff_carries_no_owner_workspace_rows(monkeypatch, tmp_path):
    with _boot_hosted_client(monkeypatch, tmp_path) as c:
        w = asyncio.run(_seed_workspace(c.app.state.db))
        owner = c.post(
            f"/projects/{w['proj_a']}/handoff", headers=_headers(w["tok_owner"]),
            json={"mode": "full"},
        )
        assert owner.status_code == 200, owner.text
        assert "owner-decision" in owner.text, owner.text[:300]

        scoped = c.post(
            f"/projects/{w['proj_a']}/handoff", headers=_headers(w["tok_scoped"], w["owner_id"]),
            json={"mode": "full"},
        )
        assert scoped.status_code == 200, scoped.text
        assert "wsleak-proj-a" in scoped.text, scoped.text[:300]
        for leaked in OWNER_WORKSPACE_MARKERS:
            assert leaked not in scoped.text, f"{leaked!r} leaked into a scoped HTTP full handoff"


def test_scoped_member_context_block_carries_no_owner_or_legacy_workspace_rows(monkeypatch, tmp_path):
    with _boot_hosted_client(monkeypatch, tmp_path) as c:
        w = asyncio.run(_seed_workspace(c.app.state.db))
        owner_text = c.post(
            "/mcp", headers=_headers(w["tok_owner"]),
            json=_mcp_request("get_context_block", {"project_id": w["proj_a"]}),
        ).text
        # Control: the owner's own context block still carries the workspace section.
        assert "owner-decision" in owner_text, owner_text[:300]

        scoped_text = c.post(
            "/mcp", headers=_headers(w["tok_scoped"], w["owner_id"]),
            json=_mcp_request("get_context_block", {"project_id": w["proj_a"]}),
        ).text
        assert not _mcp_text_refused(scoped_text), scoped_text[:300]
        assert "wsleak-proj-a" in scoped_text, scoped_text[:300]
        for leaked in OWNER_WORKSPACE_MARKERS:
            assert leaked not in scoped_text, f"{leaked!r} leaked into a scoped context block"


def test_scoped_member_session_start_carries_no_owner_workspace_context(monkeypatch, tmp_path):
    # compact=False is the mode that returns the workspace_context block (the default compact
    # response drops it), so this is the mode a scoped caller can request to read the owner's rows.
    with _boot_hosted_client(monkeypatch, tmp_path) as c:
        w = asyncio.run(_seed_workspace(c.app.state.db))
        owner_text = c.post(
            "/mcp", headers=_headers(w["tok_owner"]),
            json=_mcp_request("start_session", {
                "project_id": w["proj_a"], "session_name": "wsleak-owner-start", "compact": False,
            }),
        ).text
        # Control: the owner's full session start still carries the workspace context.
        assert "owner-decision" in owner_text, owner_text[:300]

        scoped_text = c.post(
            "/mcp", headers=_headers(w["tok_scoped"], w["owner_id"]),
            json=_mcp_request("start_session", {
                "project_id": w["proj_a"], "session_name": "wsleak-scoped-start", "compact": False,
            }),
        ).text
        assert not _mcp_text_refused(scoped_text), scoped_text[:300]
        # Raw substring: the MCP text part is JSON-escaped, so the key appears as \"session_id\".
        assert "session_id" in scoped_text, scoped_text[:300]
        for leaked in OWNER_WORKSPACE_MARKERS:
            assert leaked not in scoped_text, f"{leaked!r} leaked into a scoped session start"


def test_scoped_member_http_session_start_carries_no_owner_workspace_context(monkeypatch, tmp_path):
    # POST /projects/{id}/start-session runs the composite in full mode, so its response
    # carries the workspace_context block unless the caller is project-scoped.
    with _boot_hosted_client(monkeypatch, tmp_path) as c:
        w = asyncio.run(_seed_workspace(c.app.state.db))
        owner = c.post(
            f"/projects/{w['proj_a']}/start-session", headers=_headers(w["tok_owner"]),
            json={"session_name": "wsleak-owner-http-start"},
        )
        assert owner.status_code == 200, owner.text
        # Control: the owner's session start still carries the workspace context.
        assert "owner-decision" in owner.text, owner.text[:300]

        scoped = c.post(
            f"/projects/{w['proj_a']}/start-session", headers=_headers(w["tok_scoped"], w["owner_id"]),
            json={"session_name": "wsleak-scoped-http-start"},
        )
        assert scoped.status_code == 200, scoped.text
        assert '"session_id"' in scoped.text, scoped.text[:300]
        for leaked in OWNER_WORKSPACE_MARKERS:
            assert leaked not in scoped.text, f"{leaked!r} leaked into a scoped HTTP session start"
