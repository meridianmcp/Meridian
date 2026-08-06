"""v2.5 expanded test coverage — sprint items, config, notifications, admin health,
MCP responses, auth routes, team summary, and dashboard.js markers."""

from __future__ import annotations

import asyncio

import pytest

from meridian import db as db_module
from meridian.db import workspace as ws_module
from meridian.mcp.handlers import session_tools as st_mod


# ---------------------------------------------------------------------------
# Sprint item feedback + inline edit
# ---------------------------------------------------------------------------


def _make_project(client, name="sprint-test"):
    r = client.post("/projects", json={"name": name})
    assert r.status_code == 201
    return r.json()["id"]


def _make_sprint_item(client, pid, title="Task", version="v1.0"):
    r = client.post(f"/projects/{pid}/sprint-items", json={"title": title, "version": version})
    assert r.status_code == 201
    return r.json()


def test_sprint_item_create_via_http(client):
    """POST /sprint-items creates an item and returns it."""
    pid = _make_project(client)
    item = _make_sprint_item(client, pid, "Fix login", "v1.0")
    assert item["title"] == "Fix login"
    assert item["version"] == "v1.0"
    assert item["status"] == "pending"


def test_sprint_item_patch_feedback_thumb_up(client):
    """PATCH with feedback_thumb 1 stores thumbs-up."""
    pid = _make_project(client)
    item = _make_sprint_item(client, pid)
    r2 = client.patch(f"/projects/{pid}/sprint-items/{item['id']}", json={"feedback_thumb": 1})
    assert r2.status_code == 200
    assert r2.json()["feedback_thumb"] == 1


def test_sprint_item_patch_feedback_thumb_down(client):
    """PATCH with feedback_thumb -1 stores thumbs-down."""
    pid = _make_project(client)
    item = _make_sprint_item(client, pid)
    r2 = client.patch(f"/projects/{pid}/sprint-items/{item['id']}", json={"feedback_thumb": -1})
    assert r2.status_code == 200
    assert r2.json()["feedback_thumb"] == -1


def test_sprint_item_patch_feedback_thumb_invalid(client):
    """PATCH with feedback_thumb 0 is rejected."""
    pid = _make_project(client)
    item = _make_sprint_item(client, pid)
    r2 = client.patch(f"/projects/{pid}/sprint-items/{item['id']}", json={"feedback_thumb": 0})
    assert r2.status_code == 422


def test_sprint_item_patch_feedback_note(client):
    """PATCH with feedback_note stores the note text."""
    pid = _make_project(client)
    item = _make_sprint_item(client, pid)
    r2 = client.patch(f"/projects/{pid}/sprint-items/{item['id']}", json={"feedback_note": "great work"})
    assert r2.status_code == 200
    assert r2.json()["feedback_note"] == "great work"


def test_sprint_item_inline_edit_title(client):
    """PATCH can update item title."""
    pid = _make_project(client)
    item = _make_sprint_item(client, pid, "Old title")
    r2 = client.patch(f"/projects/{pid}/sprint-items/{item['id']}", json={"title": "New title"})
    assert r2.status_code == 200
    assert r2.json()["title"] == "New title"


def test_sprint_item_inline_edit_empty_title_rejected(client):
    """PATCH with empty title is rejected with 422."""
    pid = _make_project(client)
    item = _make_sprint_item(client, pid)
    r2 = client.patch(f"/projects/{pid}/sprint-items/{item['id']}", json={"title": ""})
    assert r2.status_code == 422


def test_sprint_item_inline_edit_version(client):
    """PATCH can update item version."""
    pid = _make_project(client)
    item = _make_sprint_item(client, pid)
    r2 = client.patch(f"/projects/{pid}/sprint-items/{item['id']}", json={"version": "v2.0"})
    assert r2.status_code == 200
    assert r2.json()["version"] == "v2.0"


def test_sprint_item_complete(client):
    """POST /complete transitions status to done."""
    pid = _make_project(client)
    item = _make_sprint_item(client, pid)
    r2 = client.post(f"/projects/{pid}/sprint-items/{item['id']}/complete")
    assert r2.status_code == 200
    assert r2.json()["status"] == "done"


def test_sprint_item_skip(client):
    """POST /skip transitions status to skipped."""
    pid = _make_project(client)
    item = _make_sprint_item(client, pid)
    r2 = client.post(f"/projects/{pid}/sprint-items/{item['id']}/skip")
    assert r2.status_code == 200
    assert r2.json()["status"] == "skipped"


def test_sprint_item_fail(client):
    """POST /fail transitions status to failed."""
    pid = _make_project(client)
    item = _make_sprint_item(client, pid)
    r2 = client.post(f"/projects/{pid}/sprint-items/{item['id']}/fail")
    assert r2.status_code == 200
    assert r2.json()["status"] == "failed"


def test_sprint_item_push_to_version(client):
    """POST /push moves item to a new version."""
    pid = _make_project(client)
    item = _make_sprint_item(client, pid)
    r2 = client.post(f"/projects/{pid}/sprint-items/{item['id']}/push", json={"to_version": "v1.1"})
    assert r2.status_code == 200
    pushed = r2.json()
    assert pushed["pushed_to"] == "v1.1"
    assert pushed["status"] == "pushed"


def test_sprint_item_push_requires_to_version(client):
    """POST /push without to_version returns 422."""
    pid = _make_project(client)
    item = _make_sprint_item(client, pid)
    r2 = client.post(f"/projects/{pid}/sprint-items/{item['id']}/push", json={})
    assert r2.status_code == 422


def test_sprint_item_list_returns_all_items(client):
    """GET /sprint-items returns all items for the project."""
    pid = _make_project(client)
    _make_sprint_item(client, pid, "A", "v1.0")
    _make_sprint_item(client, pid, "B", "v1.1")
    r2 = client.get(f"/projects/{pid}/sprint-items")
    assert r2.status_code == 200
    items = r2.json()
    assert len(items) >= 2
    titles = {i["title"] for i in items}
    assert {"A", "B"} <= titles


def test_sprint_item_delete(client):
    """DELETE /sprint-items/{id} removes the item."""
    pid = _make_project(client)
    item = _make_sprint_item(client, pid, "To delete")
    r2 = client.delete(f"/projects/{pid}/sprint-items/{item['id']}")
    assert r2.status_code == 204
    items = client.get(f"/projects/{pid}/sprint-items").json()
    assert not any(i["id"] == item["id"] for i in items)


# ---------------------------------------------------------------------------
# Config endpoint
# ---------------------------------------------------------------------------


def test_config_endpoint_returns_demo_mode_false(client):
    """GET /config returns demo_mode=false by default."""
    r = client.get("/config")
    assert r.status_code == 200
    assert r.json()["demo_mode"] is False


def test_config_endpoint_returns_demo_mode_true(client, monkeypatch):
    """GET /config returns demo_mode=true when MERIDIAN_DEMO=1."""
    monkeypatch.setenv("MERIDIAN_DEMO", "1")
    r = client.get("/config")
    assert r.status_code == 200
    assert r.json()["demo_mode"] is True


def test_config_endpoint_has_required_fields(client):
    """GET /config returns all expected fields."""
    r = client.get("/config")
    assert r.status_code == 200
    body = r.json()
    for field in ("server_url", "host", "port", "version", "db", "demo_mode"):
        assert field in body, f"Missing field: {field}"


def test_config_endpoint_returns_connections_list(client):
    """GET /config includes connections list."""
    r = client.get("/config")
    assert r.status_code == 200
    assert "connections" in r.json()
    assert isinstance(r.json()["connections"], list)


# ---------------------------------------------------------------------------
# Notification preferences (non-hosted mode returns 404)
# ---------------------------------------------------------------------------


def test_get_notification_prefs_returns_404_in_non_hosted_mode(client):
    """GET /settings/notifications returns 404 in non-hosted mode."""
    r = client.get("/settings/notifications")
    assert r.status_code == 404


def test_patch_notification_prefs_returns_404_in_non_hosted_mode(client):
    """PATCH /settings/notifications returns 404 in non-hosted mode."""
    r = client.patch("/settings/notifications", json={"hitl": True})
    assert r.status_code == 404


def test_patch_notification_prefs_persists_in_hosted_mode(monkeypatch, tmp_path):
    """PATCH /settings/notifications stores prefs for an authenticated tenant."""
    monkeypatch.setenv("MERIDIAN_HOSTED", "1")
    monkeypatch.setenv("MERIDIAN_SESSION_SECRET", "test-secret")
    with _make_gated_client(monkeypatch, tmp_path) as c:
        from meridian.hosted import _make_session_cookie

        db = c.app.state.db
        tenant = asyncio.run(db_module.upsert_tenant(db, "notify@example.com"))
        session = asyncio.run(
            db_module.create_user_session(db, tenant["id"], "2099-01-01 00:00:00")
        )
        c.cookies.set("meridian_session", _make_session_cookie(session["id"]))

        r = c.patch("/settings/notifications", json={"hitl": False, "sprint": False})
        assert r.status_code == 200, r.text
        assert r.json()["prefs"]["hitl"] is False
        assert r.json()["prefs"]["sprint"] is False

        r2 = c.get("/settings/notifications")
        assert r2.status_code == 200, r2.text
        assert r2.json()["prefs"]["hitl"] is False
        assert r2.json()["prefs"]["storage"] is True


# ---------------------------------------------------------------------------
# Admin health endpoint
# ---------------------------------------------------------------------------


def test_admin_health_requires_auth(client):
    """GET /admin/health returns 403 when unauthenticated."""
    r = client.get("/admin/health")
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# MCP context block
# ---------------------------------------------------------------------------


def test_context_block_404_for_missing_project(client):
    """GET /context-block returns 404 for unknown project."""
    r = client.get("/projects/does-not-exist-xyz/context-block")
    assert r.status_code == 404


def test_context_block_returns_data(client):
    """GET /context-block?mode=full returns a non-empty plain-text response."""
    pid = _make_project(client, "ctx-test")
    client.post(f"/projects/{pid}/goal", json={"content": "ship it"})
    r2 = client.get(f"/projects/{pid}/context-block?mode=full")
    assert r2.status_code == 200
    assert r2.text  # non-empty plain text


# ---------------------------------------------------------------------------
# Goal history API
# ---------------------------------------------------------------------------


def test_goal_history_returns_list(client):
    """GET /goal-history returns a list."""
    pid = _make_project(client, "history-test")
    r2 = client.get(f"/projects/{pid}/goal-history")
    assert r2.status_code == 200
    assert isinstance(r2.json(), list)


def test_goal_history_404_for_unknown_project(client):
    """GET /goal-history returns 404 for unknown project."""
    r = client.get("/projects/does-not-exist-xyz/goal-history")
    assert r.status_code == 404


def test_goal_history_shows_versions_after_set(client):
    """Goal history grows with each significant goal change."""
    pid = _make_project(client, "history-test")
    client.post(f"/projects/{pid}/goal", json={"content": "version 1"})
    client.post(f"/projects/{pid}/goal", json={"content": "version 2"})
    r2 = client.get(f"/projects/{pid}/goal-history")
    assert r2.status_code == 200
    assert len(r2.json()) >= 1


# ---------------------------------------------------------------------------
# Sprint item db-level functions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_sprint_item_creates_pending(db):
    """add_sprint_item returns a pending item."""
    p = await db_module.create_project(db, "alpha")
    item = await db_module.add_sprint_item(db, p["id"], "v1.0", "Fix auth")
    assert item["status"] == "pending"
    assert item["title"] == "Fix auth"
    assert item["version"] == "v1.0"


@pytest.mark.asyncio
async def test_complete_sprint_item_marks_done(db):
    """complete_sprint_item transitions to done."""
    p = await db_module.create_project(db, "alpha")
    item = await db_module.add_sprint_item(db, p["id"], "v1.0", "Task")
    done = await db_module.complete_sprint_item(db, p["id"], item["id"])
    assert done["status"] == "done"


@pytest.mark.asyncio
async def test_fail_sprint_item_marks_failed(db):
    """fail_sprint_item transitions to failed."""
    p = await db_module.create_project(db, "alpha")
    item = await db_module.add_sprint_item(db, p["id"], "v1.0", "Task")
    failed = await db_module.fail_sprint_item(db, p["id"], item["id"])
    assert failed["status"] == "failed"


@pytest.mark.asyncio
async def test_skip_sprint_item_marks_skipped(db):
    """skip_sprint_item transitions to skipped."""
    p = await db_module.create_project(db, "alpha")
    item = await db_module.add_sprint_item(db, p["id"], "v1.0", "Task")
    skipped = await db_module.skip_sprint_item(db, p["id"], item["id"])
    assert skipped["status"] == "skipped"


@pytest.mark.asyncio
async def test_push_sprint_item_marks_pushed(db):
    """push_sprint_item sets status=pushed and pushed_to version."""
    p = await db_module.create_project(db, "alpha")
    item = await db_module.add_sprint_item(db, p["id"], "v1.0", "Task")
    pushed = await db_module.push_sprint_item(db, p["id"], item["id"], "v1.1")
    assert pushed["status"] == "pushed"
    assert pushed["pushed_to"] == "v1.1"


@pytest.mark.asyncio
async def test_patch_sprint_item_feedback_thumb_db(db):
    """patch_sprint_item stores feedback_thumb correctly."""
    p = await db_module.create_project(db, "alpha")
    item = await db_module.add_sprint_item(db, p["id"], "v1.0", "Task")
    patched = await db_module.patch_sprint_item(db, p["id"], item["id"], feedback_thumb=1)
    assert patched["feedback_thumb"] == 1


@pytest.mark.asyncio
async def test_get_sprint_items_returns_all(db):
    """get_sprint_items returns items for the project."""
    p = await db_module.create_project(db, "alpha")
    await db_module.add_sprint_item(db, p["id"], "v1.0", "A")
    await db_module.add_sprint_item(db, p["id"], "v1.1", "B")
    items = await db_module.get_sprint_items(db, p["id"])
    assert len(items) == 2
    titles = {i["title"] for i in items}
    assert titles == {"A", "B"}


# ---------------------------------------------------------------------------
# Magic link auth routes
# ---------------------------------------------------------------------------


def test_magic_link_request_includes_dev_link_when_resend_absent(client):
    """Without Resend, POST /auth/magic includes dev_link."""
    r = client.post("/auth/magic", json={"email": "devtest@example.com"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "dev_link" in body


# test_auth_magic_is_rate_limited and test_export_my_data_is_rate_limited moved
# to tests/test_rate_limit_serial.py — they need to run outside the -n auto +
# --cov sweep; see that file's module docstring for why.


def test_magic_link_verify_rejects_bad_token(client):
    """GET /auth/magic/verify with unknown token returns 401."""
    r = client.get("/auth/magic/verify?token=badtoken123")
    assert r.status_code == 401


def test_magic_link_request_missing_email_rejected(client):
    """POST /auth/magic without email returns 4xx."""
    r = client.post("/auth/magic", json={})
    assert r.status_code in (400, 422)


@pytest.mark.asyncio
async def test_magic_token_single_use(db):
    """A consumed magic token cannot be consumed again."""
    import hashlib as _h
    from datetime import datetime, timedelta, timezone
    raw = "single-use-token"
    token_hash = _h.sha256(raw.encode()).hexdigest()
    expires = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    await db_module.store_magic_token(db, "once@example.com", token_hash, expires)
    first = await db_module.consume_magic_token(db, token_hash)
    assert first is not None
    second = await db_module.consume_magic_token(db, token_hash)
    assert second is None


# ---------------------------------------------------------------------------
# Team summary API
# ---------------------------------------------------------------------------


def test_team_summary_returns_period_days(client):
    """GET /team/summary returns period_days and humans."""
    pid = _make_project(client, "team-proj")
    r2 = client.get(f"/team/summary?project_id={pid}&days=7")
    assert r2.status_code == 200
    body = r2.json()
    assert "period_days" in body
    assert "humans" in body
    assert isinstance(body["humans"], list)


def test_team_summary_groups_by_human(client):
    """Team summary with human-attributed sessions groups correctly."""
    pid = _make_project(client, "team-proj")
    # Use the correct session registration endpoint
    s_r = client.post("/sessions/register", json={
        "project_id": pid, "name": "alice-session", "human_id": "alice"
    })
    assert s_r.status_code == 201
    sid = s_r.json()["id"]
    # Log a task
    client.post("/tasks", json={
        "project_id": pid, "session_id": sid,
        "description": "Alice's task", "status": "done"
    })
    r2 = client.get(f"/team/summary?project_id={pid}&days=30")
    assert r2.status_code == 200
    humans = {h["human_id"]: h for h in r2.json()["humans"]}
    assert "alice" in humans
    assert humans["alice"]["tasks_done"] >= 1


# ---------------------------------------------------------------------------
# Session registration and task logging
# ---------------------------------------------------------------------------


def test_session_register_and_log_task(client):
    """Session registration + log_task round trip via HTTP."""
    pid = _make_project(client, "sess-proj")
    s_r = client.post("/sessions/register", json={"project_id": pid, "name": "test-sess"})
    assert s_r.status_code == 201
    sid = s_r.json()["id"]
    t_r = client.post("/tasks", json={
        "project_id": pid, "session_id": sid,
        "description": "Did a thing", "status": "done"
    })
    assert t_r.status_code == 201
    tasks = client.get(f"/projects/{pid}/tasks").json()
    assert any(t["description"] == "Did a thing" for t in tasks)


@pytest.mark.asyncio
async def test_session_status_lifecycle(db):
    """Session transitions active → closed properly."""
    p = await db_module.create_project(db, "sess-test")
    s = await db_module.register_session(db, p["id"], "lifecycle")
    assert s["status"] == "active"
    await db_module.close_session(db, s["id"])
    all_sess = await db_module.get_sessions(db, p["id"], active_only=False)
    closed = next(x for x in all_sess if x["id"] == s["id"])
    assert closed["status"] == "closed"


@pytest.mark.asyncio
async def test_log_task_with_human_id(db):
    """log_task stores human_id when session has one."""
    p = await db_module.create_project(db, "human-test")
    s = await db_module.register_session(db, p["id"], "human-sess", human_id="bob")
    t = await db_module.log_task(db, s["id"], p["id"], "Bob's task", "done")
    assert t["description"] == "Bob's task"


# ---------------------------------------------------------------------------
# Project CRUD HTTP
# ---------------------------------------------------------------------------


def test_project_create_and_rename(client):
    """POST /projects creates; POST /rename renames."""
    r = client.post("/projects", json={"name": "original"})
    assert r.status_code == 201
    pid = r.json()["id"]
    r2 = client.post(f"/projects/{pid}/rename", json={"name": "renamed"})
    assert r2.status_code == 200
    assert r2.json()["name"] == "renamed"


def test_project_delete_via_http(client):
    """DELETE /projects/{id} removes the project."""
    pid = _make_project(client, "to-delete")
    r2 = client.delete(f"/projects/{pid}")
    assert r2.status_code in (200, 204)
    r3 = client.get(f"/projects/{pid}")
    assert r3.status_code == 404


def test_project_list_returns_all(client):
    """GET /projects returns all created projects."""
    client.post("/projects", json={"name": "p1"})
    client.post("/projects", json={"name": "p2"})
    r = client.get("/projects")
    assert r.status_code == 200
    names = {p["name"] for p in r.json()}
    assert {"p1", "p2"} <= names


# ---------------------------------------------------------------------------
# Version regex in sprint items
# ---------------------------------------------------------------------------


def test_sprint_version_regex_handles_dotted_versions(client):
    """Sprint items with v1.0-bugs style versions work correctly."""
    pid = _make_project(client, "version-test")
    item = _make_sprint_item(client, pid, "Fix", "v1.0-bugs")
    assert item["version"] == "v1.0-bugs"


def test_sprint_version_regex_handles_semver(client):
    """Sprint items accept semver-style versions."""
    pid = _make_project(client, "version-test")
    item = _make_sprint_item(client, pid, "Task", "v2.1.3")
    assert item["version"] == "v2.1.3"


# ---------------------------------------------------------------------------
# North star and sprint goal fields
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_goal_with_north_star(db):
    """set_goal stores north_star separately from content."""
    p = await db_module.create_project(db, "ns-test")
    g = await db_module.set_goal(db, p["id"], "main content", north_star="Be the best")
    assert g["north_star"] == "Be the best"


@pytest.mark.asyncio
async def test_set_goal_with_sprint(db):
    """set_goal stores sprint field independently."""
    p = await db_module.create_project(db, "sprint-test")
    g = await db_module.set_goal(db, p["id"], "content", sprint="v1.0 — ship it")
    assert g["sprint"] == "v1.0 — ship it"


def test_set_goal_via_http(client):
    """POST /projects/{id}/goal stores and retrieves content."""
    pid = _make_project(client, "goal-test")
    r2 = client.post(f"/projects/{pid}/goal", json={"content": "build the thing"})
    assert r2.status_code == 200
    r3 = client.get(f"/projects/{pid}/goal")
    assert r3.status_code == 200
    assert r3.json()["content"] == "build the thing"


def test_set_sprint_via_http(client):
    """POST /projects/{id}/goal/sprint stores sprint field."""
    pid = _make_project(client, "sp-test")
    # Goal must exist before setting sprint
    client.post(f"/projects/{pid}/goal", json={"content": "initial content"})
    s_r = client.post("/sessions/register", json={"project_id": pid, "name": "sprint-sess"})
    sid = s_r.json()["id"]
    r2 = client.post(f"/projects/{pid}/goal/sprint", json={"sprint": "v2.0 — launch", "session_id": sid})
    assert r2.status_code == 200


# ---------------------------------------------------------------------------
# HITL (Human In The Loop)
# ---------------------------------------------------------------------------


def test_hitl_http_create_and_list(client):
    """POST /hitl creates a request; GET /hitl lists it."""
    pid = _make_project(client, "hitl-test")
    s_r = client.post("/sessions/register", json={"project_id": pid, "name": "hitl-sess"})
    sid = s_r.json()["id"]
    r2 = client.post(f"/projects/{pid}/hitl", json={
        "session_id": sid,
        "question": "Should I deploy?",
        "context": "Prod db is ready",
        "blocking": True,
    })
    assert r2.status_code == 201
    hitl_id = r2.json()["id"]
    r3 = client.get(f"/projects/{pid}/hitl?status=pending")
    assert r3.status_code == 200
    ids = {h["id"] for h in r3.json()}
    assert hitl_id in ids


def test_hitl_answer_resolves_request(client):
    """PATCH /hitl/{id} with action=answer transitions to answered."""
    pid = _make_project(client, "hitl-test")
    s_r = client.post("/sessions/register", json={"project_id": pid, "name": "hitl-sess"})
    sid = s_r.json()["id"]
    r2 = client.post(f"/projects/{pid}/hitl", json={
        "session_id": sid, "question": "Proceed?", "blocking": False
    })
    assert r2.status_code == 201
    hid = r2.json()["id"]
    r3 = client.patch(f"/hitl/{hid}", json={"action": "answer", "answer": "yes"})
    assert r3.status_code == 200
    assert r3.json()["status"] == "answered"


# ---------------------------------------------------------------------------
# Decisions pinned
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pin_decision_and_retrieve(db):
    """pin_decision stores a record that get_pinned_decisions returns."""
    p = await db_module.create_project(db, "pin-test")
    await db_module.pin_decision(db, p["id"], "Use psycopg3", "asyncpg causes issues", "TECHNICAL")
    pinned = await db_module.get_pinned_decisions(db, p["id"])
    assert len(pinned) == 1
    assert pinned[0]["title"] == "Use psycopg3"
    assert pinned[0]["category"] == "TECHNICAL"


@pytest.mark.asyncio
async def test_count_decisions_returns_correct_count(db):
    """count_decisions returns accurate active decision count."""
    p = await db_module.create_project(db, "count-test")
    assert await db_module.count_decisions(db, p["id"]) == 0
    await db_module.pin_decision(db, p["id"], "Dec 1", "body", "TECHNICAL")
    await db_module.pin_decision(db, p["id"], "Dec 2", "body", "STRATEGIC")
    assert await db_module.count_decisions(db, p["id"]) == 2


# ---------------------------------------------------------------------------
# Dashboard.js static markers
#
# v1.1-tests: Removed 7 pure substring-presence grep tests here
# (sprint-group-header / demo_mode / feedback_thumb / drawer-settings /
# "Remove connection" / goal-history / activeVersions). They asserted a literal
# string exists in the JS source — they break on a harmless rename and pass even
# when the feature is broken, so they carry near-zero regression value. The
# underlying behavior is covered server-side: demo mode by test_demo_ux.py,
# feedback by the feedback endpoint tests, goal history by the /goal-history
# route tests, and version grouping by the sprint-items endpoint tests.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Static pages
# ---------------------------------------------------------------------------


def test_terms_page_returns_200(client):
    """GET /terms returns an HTML 200."""
    r = client.get("/terms")
    assert r.status_code == 200
    assert "html" in r.headers.get("content-type", "").lower()


def test_privacy_page_returns_200(client):
    """GET /privacy returns an HTML 200."""
    r = client.get("/privacy")
    assert r.status_code == 200
    assert "html" in r.headers.get("content-type", "").lower()


# ---------------------------------------------------------------------------
# Config endpoint
# ---------------------------------------------------------------------------


def test_config_has_required_fields(client):
    """GET /config returns all required runtime fields."""
    r = client.get("/config")
    assert r.status_code == 200
    data = r.json()
    for key in ("server_url", "host", "port", "version", "db", "demo_mode"):
        assert key in data, f"missing key: {key}"


def test_config_demo_mode_is_boolean(client):
    """GET /config demo_mode field is always a boolean."""
    r = client.get("/config")
    assert r.status_code == 200
    assert isinstance(r.json()["demo_mode"], bool)


# ---------------------------------------------------------------------------
# Setup/needed
# ---------------------------------------------------------------------------


def test_setup_needed_true_when_no_projects(client):
    """GET /setup/needed returns {needed: true} for a fresh DB."""
    r = client.get("/setup/needed")
    assert r.status_code == 200
    # May be true or false depending on isolation; just check shape.
    assert "needed" in r.json()
    assert isinstance(r.json()["needed"], bool)


def test_setup_needed_false_after_project_created(client):
    """GET /setup/needed returns {needed: false} once a project exists."""
    _make_project(client, "setup-check")
    r = client.get("/setup/needed")
    assert r.status_code == 200
    assert r.json()["needed"] is False


# ---------------------------------------------------------------------------
# Project by-name
# ---------------------------------------------------------------------------


def test_project_by_name_found(client):
    """GET /projects/by-name/{name} returns project and goal summary."""
    _make_project(client, "findme")
    r = client.get("/projects/by-name/findme")
    assert r.status_code == 200
    data = r.json()
    assert "project" in data
    assert data["project"]["name"] == "findme"


def test_project_by_name_not_found_returns_404(client):
    """GET /projects/by-name/{name} returns 404 for unknown name."""
    r = client.get("/projects/by-name/totally-unknown-zzz")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# North star
# ---------------------------------------------------------------------------


def test_set_north_star_via_http(client):
    """POST /goal/north-star stores north_star field."""
    pid = _make_project(client, "ns-test")
    client.post(f"/projects/{pid}/goal", json={"content": "initial"})
    r = client.post(
        f"/projects/{pid}/goal/north-star",
        json={"north_star": "Build the best tool", "human_id": "any"},
    )
    assert r.status_code == 200
    assert r.json()["north_star"] == "Build the best tool"


def test_set_north_star_requires_existing_goal(client):
    """POST /goal/north-star returns 422 when no goal is set first."""
    pid = _make_project(client, "ns-nogoal")
    r = client.post(
        f"/projects/{pid}/goal/north-star",
        json={"north_star": "Some star", "human_id": "any"},
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Project stats
# ---------------------------------------------------------------------------


def test_project_stats_returns_shape(client):
    """GET /projects/{id}/stats returns expected keys."""
    pid = _make_project(client, "stats-test")
    r = client.get(f"/projects/{pid}/stats")
    assert r.status_code == 200
    data = r.json()
    assert "tasks_per_day" in data or "sprint_completion" in data or isinstance(data, dict)


def test_project_stats_404_for_unknown(client):
    """GET /projects/unknown/stats returns 404."""
    r = client.get("/projects/does-not-exist-zzz/stats")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Goal history
# ---------------------------------------------------------------------------


def test_goal_history_grows_with_versions(client):
    """GET /goal-history returns more entries as goal is updated."""
    pid = _make_project(client, "history-grow")
    client.post(f"/projects/{pid}/goal", json={"content": "v1"})
    client.post(f"/projects/{pid}/goal", json={"content": "v2"})
    r = client.get(f"/projects/{pid}/goal-history")
    assert r.status_code == 200
    assert len(r.json()) >= 1


# ---------------------------------------------------------------------------
# Session heartbeat
# ---------------------------------------------------------------------------


def test_session_heartbeat_returns_ok(client):
    """POST /sessions/{id}/heartbeat returns status ok."""
    pid = _make_project(client, "hb-test")
    s = client.post("/sessions/register", json={"project_id": pid, "name": "hb-sess"}).json()
    r = client.post(f"/sessions/{s['id']}/heartbeat")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_session_heartbeat_404_for_unknown(client):
    """POST /sessions/unknown/heartbeat returns 404."""
    r = client.post("/sessions/does-not-exist/heartbeat")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Sprint item delete
# ---------------------------------------------------------------------------


def test_sprint_item_delete(client):
    """DELETE /sprint-items/{id} removes the item."""
    pid = _make_project(client, "del-test")
    item = _make_sprint_item(client, pid, "To delete")
    r = client.delete(f"/projects/{pid}/sprint-items/{item['id']}")
    assert r.status_code == 204
    items = client.get(f"/projects/{pid}/sprint-items").json()
    assert not any(i["id"] == item["id"] for i in items)


# ---------------------------------------------------------------------------
# Tools list
# ---------------------------------------------------------------------------


def test_tools_endpoint_returns_list(client):
    """GET /tools returns a list of MCP tool definitions."""
    r = client.get("/tools")
    assert r.status_code == 200
    tools = r.json()
    assert isinstance(tools, list)
    assert len(tools) > 0
    assert "name" in tools[0]


# ---------------------------------------------------------------------------
# Notes CRUD
# ---------------------------------------------------------------------------


def test_notes_create_and_list(client):
    """POST /notes creates a note; GET /notes lists it."""
    pid = _make_project(client, "notes-test")
    r = client.post(f"/projects/{pid}/notes", json={"title": "My note", "body": "Some content"})
    assert r.status_code == 201
    note_id = r.json()["id"]
    r2 = client.get(f"/projects/{pid}/notes")
    assert r2.status_code == 200
    assert any(n["id"] == note_id for n in r2.json())


def test_notes_require_title_and_body(client):
    """POST /notes with missing body returns 400."""
    pid = _make_project(client, "notes-val")
    r = client.post(f"/projects/{pid}/notes", json={"title": "title only"})
    assert r.status_code == 400


def test_notes_delete(client):
    """DELETE /notes/{id} removes the note."""
    pid = _make_project(client, "notes-del")
    note = client.post(
        f"/projects/{pid}/notes", json={"title": "delete me", "body": "content"}
    ).json()
    r = client.delete(f"/projects/{pid}/notes/{note['id']}")
    assert r.status_code == 204


# ---------------------------------------------------------------------------
# Sessions list
# ---------------------------------------------------------------------------


def test_sessions_list_for_project(client):
    """GET /projects/{id}/sessions returns active sessions."""
    pid = _make_project(client, "sess-list")
    client.post("/sessions/register", json={"project_id": pid, "name": "s1"})
    client.post("/sessions/register", json={"project_id": pid, "name": "s2"})
    r = client.get(f"/projects/{pid}/sessions")
    assert r.status_code == 200
    names = {s["name"] for s in r.json()}
    assert "s1" in names and "s2" in names


# ---------------------------------------------------------------------------
# Tasks list
# ---------------------------------------------------------------------------


def test_tasks_list_for_project(client):
    """GET /projects/{id}/tasks returns logged tasks newest first."""
    pid = _make_project(client, "tasks-list")
    s = client.post("/sessions/register", json={"project_id": pid, "name": "t-sess"}).json()
    client.post("/tasks", json={"session_id": s["id"], "project_id": pid, "description": "task A", "status": "done"})
    client.post("/tasks", json={"session_id": s["id"], "project_id": pid, "description": "task B", "status": "done"})
    r = client.get(f"/projects/{pid}/tasks")
    assert r.status_code == 200
    descs = [t["description"] for t in r.json()]
    assert "task A" in descs and "task B" in descs


# ---------------------------------------------------------------------------
# Site password gate + demo cookie exemption (BUG 4 regression tests)
# ---------------------------------------------------------------------------


def _make_gated_client(monkeypatch, tmp_path):
    """Helper: returns a TestClient with SITE_PASSWORD set."""
    monkeypatch.setenv("MERIDIAN_DB", ":memory:")
    monkeypatch.setenv("MERIDIAN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MERIDIAN_DB_URL", "")
    monkeypatch.setenv("MERIDIAN_DEMO_DB_URL", "")
    monkeypatch.setenv("MERIDIAN_GOAL_MD", str(tmp_path / "GOAL.md"))
    monkeypatch.setenv("SITE_PASSWORD", "testpass123")
    from fastapi.testclient import TestClient
    import importlib
    import meridian.server as server_module
    server_module = importlib.reload(server_module)
    # Blank live-DB fallback env vars AFTER reload (reload re-reads .env).
    # These must never point at real DBs during unit tests.
    monkeypatch.setenv("MERIDIAN_AUTH_DB", "")
    monkeypatch.setenv("MERIDIAN_STANDARD_KEY", "")
    return TestClient(server_module.app)


def test_site_password_gate_blocks_without_cookie(monkeypatch, tmp_path):
    """Requests without the gate cookie receive the password gate HTML."""
    with _make_gated_client(monkeypatch, tmp_path) as c:
        r = c.get("/projects")
        # Gate returns HTML, not JSON
        assert r.status_code == 200
        assert "password" in r.text.lower() or "html" in r.headers.get("content-type", "").lower()


def test_site_password_gate_exempts_config_endpoint(monkeypatch, tmp_path):
    """GET /config is always exempt from the site password gate."""
    with _make_gated_client(monkeypatch, tmp_path) as c:
        r = c.get("/config")
        assert r.status_code == 200
        assert "version" in r.json()


def test_site_password_gate_exempts_health_endpoint(monkeypatch, tmp_path):
    """GET /health is always exempt from the site password gate."""
    with _make_gated_client(monkeypatch, tmp_path) as c:
        r = c.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"


def test_site_password_gate_exempts_demo_cookie(monkeypatch, tmp_path):
    """Requests with meridian_demo cookie bypass the site password gate."""
    with _make_gated_client(monkeypatch, tmp_path) as c:
        # Without demo cookie: gate serves HTML password form
        r_blocked = c.get("/projects")
        assert "text/html" in r_blocked.headers.get("content-type", "")
        # With demo cookie: passes gate (may 503 if demo_db not configured — that's OK,
        # the important thing is we get past the gate, not a password form)
        c.cookies.set("meridian_demo", "any-value")
        r_demo = c.get("/projects")
        # Gate bypassed: response is NOT the password gate HTML
        assert "text/html" not in r_demo.headers.get("content-type", "") or r_demo.status_code != 200


def test_site_password_gate_exempts_valid_hosted_session(monkeypatch, tmp_path):
    """Hosted users with a valid meridian_session cookie bypass the preview gate."""
    monkeypatch.setenv("MERIDIAN_HOSTED", "1")
    monkeypatch.setenv("MERIDIAN_SESSION_SECRET", "test-secret")
    with _make_gated_client(monkeypatch, tmp_path) as c:
        from meridian import db as db_module
        from meridian.hosted import _make_session_cookie

        db = c.app.state.db
        tenant = asyncio.run(db_module.upsert_tenant(db, "alice@example.com"))
        session = asyncio.run(
            db_module.create_user_session(db, tenant["id"], "2099-01-01 00:00:00")
        )
        c.cookies.set("meridian_session", _make_session_cookie(session["id"]))
        r = c.get("/projects")
        assert r.status_code in (200, 503)
        assert "text/html" not in r.headers.get("content-type", "")


def test_site_password_gate_exempts_static_assets(monkeypatch, tmp_path):
    """Static files are always exempt from the gate."""
    with _make_gated_client(monkeypatch, tmp_path) as c:
        r = c.get("/static/dashboard.css")
        # 200 or 404 (file may not be in test env), but NOT the gate HTML
        assert r.status_code in (200, 404)
        if r.status_code == 200:
            assert "password" not in r.text.lower()


# ---------------------------------------------------------------------------
# __main__.py port kill helper
# ---------------------------------------------------------------------------


def test_kill_port_noop_when_port_free():
    """_kill_port does nothing when the port is not in use."""
    from meridian.__main__ import _kill_port
    # Port 19999 is almost certainly free — should not raise
    _kill_port(19999)


def test_kill_port_callable():
    """_kill_port is importable and callable."""
    from meridian.__main__ import _kill_port
    assert callable(_kill_port)


def test_main_tunnel_no_kill_skips_port_loop(monkeypatch):
    """`--tunnel --no-kill` must not run the 8808-8813 port-kill loop. (a887155d)"""
    from meridian import __main__ as m
    from meridian import tunnel_client

    killed: list[int] = []
    monkeypatch.setattr(m, "_kill_port", lambda p: killed.append(p))

    async def _fake_run_tunnel(**_kwargs):
        return 0

    monkeypatch.setattr(tunnel_client, "run_tunnel", _fake_run_tunnel)

    rc = m.main(["--tunnel", "--no-kill", "--token", "sk_x"])
    assert rc == 0
    assert killed == []  # cleanup skipped entirely


def test_main_tunnel_default_kills_stale_ports(monkeypatch):
    """Without --no-kill, --tunnel sweeps the full 8808-8813 range. (a887155d)"""
    from meridian import __main__ as m
    from meridian import tunnel_client

    killed: list[int] = []
    monkeypatch.setattr(m, "_kill_port", lambda p: killed.append(p))

    async def _fake_run_tunnel(**_kwargs):
        return 0

    monkeypatch.setattr(tunnel_client, "run_tunnel", _fake_run_tunnel)

    rc = m.main(["--tunnel", "--token", "sk_x"])
    assert rc == 0
    assert killed == [8808, 8809, 8810, 8811, 8812, 8813]


def test_main_tunnel_recovers_from_closed_event_loop(monkeypatch):
    """Regression: main(--tunnel) must recover when the thread's event loop was
    closed by a prior test (Python 3.12 get_event_loop() raises) — this surfaced
    under pytest-xdist on Linux CI. _ensure_event_loop() creates a fresh loop."""
    import asyncio
    from meridian import __main__ as m
    from meridian import tunnel_client

    async def _fake_run_tunnel(**_kwargs):
        return 0

    monkeypatch.setattr(tunnel_client, "run_tunnel", _fake_run_tunnel)
    monkeypatch.setattr(m, "_kill_port", lambda p: None)
    # Reproduce the failure mode: the current loop is set but closed.
    dead = asyncio.new_event_loop()
    asyncio.set_event_loop(dead)
    dead.close()

    rc = m.main(["--tunnel", "--no-kill", "--token", "sk_x"])
    assert rc == 0
    assert not asyncio.get_event_loop().is_closed()


def test_kill_port_probe_has_timeout(monkeypatch):
    """The free-port probe sets a short timeout so a dropped SYN can't hang. (a887155d)"""
    import socket as _socket
    from meridian.__main__ import _kill_port

    timeouts: list[float] = []
    real_socket = _socket.socket

    class _SpySocket(real_socket):  # type: ignore[misc,valid-type]
        def settimeout(self, t):  # noqa: D401
            timeouts.append(t)
            return super().settimeout(t)

    monkeypatch.setattr(_socket, "socket", _SpySocket)
    _kill_port(19998)  # free port → returns fast
    assert timeouts and timeouts[0] is not None and timeouts[0] <= 1.0


# ---------------------------------------------------------------------------
# Dashboard HTML version placeholder
# ---------------------------------------------------------------------------


def test_dashboard_html_has_no_hardcoded_version():
    """The server-version element must not show a hardcoded v1.9.x."""
    import pathlib, re
    html = pathlib.Path("meridian/templates/dashboard.html").read_text(encoding="utf-8")
    # Extract the server-version element content
    match = re.search(r'id="server-version"[^>]*>([^<]*)<', html)
    assert match, "server-version element not found"
    version_text = match.group(1)
    assert "v1.9.x" not in version_text, f"Hardcoded v1.9.x in server-version element: {version_text!r}"


def test_dashboard_html_version_element_exists():
    """dashboard.html has the server-version element for dynamic JS population."""
    import pathlib
    html = pathlib.Path("meridian/templates/dashboard.html").read_text(encoding="utf-8")
    assert 'id="server-version"' in html


# ---------------------------------------------------------------------------
# DB-level: project get, list, delete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_project_returns_project(db):
    """get_project returns the created project by ID."""
    p = await db_module.create_project(db, "get-proj")
    fetched = await db_module.get_project(db, p["id"])
    assert fetched is not None
    assert fetched["id"] == p["id"]
    assert fetched["name"] == "get-proj"


@pytest.mark.asyncio
async def test_get_project_returns_none_for_unknown(db):
    """get_project returns None for unknown ID."""
    result = await db_module.get_project(db, "nonexistent-uuid-123")
    assert result is None


@pytest.mark.asyncio
async def test_list_projects_includes_created(db):
    """list_projects returns all created projects."""
    await db_module.create_project(db, "proj-list-a")
    await db_module.create_project(db, "proj-list-b")
    projects = await db_module.list_projects(db)
    names = {p["name"] for p in projects}
    assert "proj-list-a" in names
    assert "proj-list-b" in names


@pytest.mark.asyncio
async def test_delete_project_removes_from_list(db):
    """delete_project removes the project."""
    p = await db_module.create_project(db, "del-me-proj")
    await db_module.delete_project(db, p["id"])
    result = await db_module.get_project(db, p["id"])
    assert result is None


@pytest.mark.asyncio
async def test_delete_project_cleans_all_child_tables(db):
    """delete_project removes rows from every child table, not just the original 8.

    This test seeds rows in every table that has a project_id or session_id
    reference (where sessions cascade from the project) and then asserts every
    row is gone after delete_project, plus the project row itself is gone.
    """
    import uuid as _uuid

    def _uid():
        return str(_uuid.uuid4())

    # --- Create project + session ---
    pid = (await db_module.create_project(db, f"del-full-{_uid()[:8]}"))["id"]
    sess = await db_module.register_session(db, pid, "del-sess")
    sid = sess["id"]

    # --- goal_states ---
    await db_module.set_goal(db, pid, "del test goal")

    # --- sessions (already created above) ---

    # --- task_log ---
    await db_module.log_task(db, sid, pid, "del task")

    # --- sprint_items ---
    item = await db_module.add_sprint_item(db, pid, "v1.0", "del item")
    iid = item["id"]

    # --- decisions_pinned ---
    await db_module.pin_decision(db, pid, "del decision", "body", "TECHNICAL")

    # --- insights ---
    await db_module.create_insight(db, pid, "del insight", "del body")

    # --- project_notes ---
    await db_module.add_project_note(db, pid, "del note", "body")

    # --- hitl_requests ---
    await db_module.request_hitl(db, pid, "del question", session_id=sid)

    # --- session_notes (session child) ---
    await db_module.add_session_note(db, sid, "del session note", "body")

    # --- executor_runs (session + project child) ---
    await db_module.create_executor_run(db, sid, pid)

    # --- active_worktrees ---
    await db_module.register_worktree(db, sid, pid, "branch-del", "/tmp/del-wt")

    # --- session_activity ---
    await db_module.record_session_activity(db, sid, "test_tool", "del activity")

    # --- session_findings ---
    await db_module.store_finding(db, pid, "del finding", session_id=sid)

    # --- session_messages ---
    await db_module.send_message(db, pid, sid, "del message", from_session_id=sid)

    # --- sprint_item_pointers ---
    await db_module.add_sprint_item_pointer(
        db, pid, iid, "code",
        [{"uri": "file:///del.py", "selector": {"type": "range", "start_line": 1, "end_line": 2}}],
    )

    # --- sprint_version_descriptions ---
    await db_module.upsert_sprint_version_description(db, pid, "v1.0", "del desc")

    # --- handoffs ---
    await db_module.record_handoff(db, pid, "full", "del handoff body", session_id=sid)

    # --- codebase_graph_entities ---
    await db_module.upsert_graph_entities(
        db, pid, [{"qualified_name": "del.MyClass", "file": "del.py", "kind": "class"}]
    )

    # --- file_locks (via direct execute — claim_file has TTL expiry side-effects) ---
    expires = "2099-01-01 00:00:00"
    await db.execute(
        "INSERT INTO file_locks (id, file_path, session_id, expires_at) VALUES (?, ?, ?, ?)",
        (_uid(), "/del/file.py", sid, expires),
    )
    await db.commit()

    # --- resource_locks ---
    await db.execute(
        "INSERT INTO resource_locks (id, resource_id, resource_type, session_id, expires_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (_uid(), "file:/del/res.py", "file", sid, expires),
    )
    await db.commit()

    # --- file_symbol_claims ---
    await db.execute(
        "INSERT INTO file_symbol_claims "
        "(id, session_id, file_path, symbol_name, symbol_type, line_start, line_end) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (_uid(), sid, "/del/sym.py", "DelClass", "class", 1, 10),
    )
    await db.commit()

    # --- file_docx_region_claims ---
    await db.execute(
        "INSERT INTO file_docx_region_claims (id, session_id, file_path, element_id) "
        "VALUES (?, ?, ?, ?)",
        (_uid(), sid, "/del/doc.docx", "elem-del"),
    )
    await db.commit()

    # --- file_patch_counters ---
    await db.execute(
        "INSERT INTO file_patch_counters (id, session_id, file_path, patch_count) "
        "VALUES (?, ?, ?, ?)",
        (_uid(), sid, "/del/patched.py", 3),
    )
    await db.commit()

    # --- file_read_claims ---
    await db.execute(
        "INSERT INTO file_read_claims (id, file_path, session_id, expires_at) "
        "VALUES (?, ?, ?, ?)",
        (_uid(), "/del/read.py", sid, expires),
    )
    await db.commit()

    # --- session_graph_snapshots ---
    await db.execute(
        "INSERT INTO session_graph_snapshots "
        "(id, session_id, project_id, node_count, edge_count, hotspot_count, file_churn) "
        "VALUES (?, ?, ?, 1, 2, 3, 4)",
        (_uid(), sid, pid),
    )
    await db.commit()

    # --- NOW delete the project ---
    await db_module.delete_project(db, pid)

    # --- Assert project itself is gone ---
    assert await db_module.get_project(db, pid) is None

    # --- Assert every child table is clean ---
    async def _count(table, col="project_id"):
        async with db.execute(
            f"SELECT COUNT(*) AS cnt FROM {table} WHERE {col} = ?", (pid,)
        ) as cur:
            row = await cur.fetchone()
        return int(row["cnt"]) if row else 0

    async def _count_by_session(table):
        async with db.execute(
            f"SELECT COUNT(*) AS cnt FROM {table} WHERE session_id = ?", (sid,)
        ) as cur:
            row = await cur.fetchone()
        return int(row["cnt"]) if row else 0

    # Direct project_id columns
    assert await _count("goal_states") == 0, "goal_states not cleaned"
    assert await _count("task_log") == 0, "task_log not cleaned"
    assert await _count("sprint_items") == 0, "sprint_items not cleaned"
    assert await _count("decisions_pinned") == 0, "decisions_pinned not cleaned"
    assert await _count("insights") == 0, "insights not cleaned"
    assert await _count("project_notes") == 0, "project_notes not cleaned"
    assert await _count("hitl_requests") == 0, "hitl_requests not cleaned"
    assert await _count("sprint_item_pointers") == 0, "sprint_item_pointers not cleaned"
    assert await _count("sprint_version_descriptions") == 0, "sprint_version_descriptions not cleaned"
    assert await _count("session_findings") == 0, "session_findings not cleaned"
    assert await _count("session_messages") == 0, "session_messages not cleaned"
    assert await _count("codebase_graph_entities") == 0, "codebase_graph_entities not cleaned"
    assert await _count("active_worktrees") == 0, "active_worktrees not cleaned"
    assert await _count("handoffs") == 0, "handoffs not cleaned"
    assert await _count("executor_runs") == 0, "executor_runs not cleaned"
    # session_graph_snapshots has project_id column
    assert await _count("session_graph_snapshots") == 0, "session_graph_snapshots not cleaned"
    # sessions itself
    assert await _count("sessions") == 0, "sessions not cleaned"

    # Session-scoped (no project_id column; keyed by session_id)
    assert await _count_by_session("session_notes") == 0, "session_notes not cleaned"
    assert await _count_by_session("file_locks") == 0, "file_locks not cleaned"
    assert await _count_by_session("resource_locks") == 0, "resource_locks not cleaned"
    assert await _count_by_session("file_symbol_claims") == 0, "file_symbol_claims not cleaned"
    assert await _count_by_session("file_docx_region_claims") == 0, "file_docx_region_claims not cleaned"
    assert await _count_by_session("file_patch_counters") == 0, "file_patch_counters not cleaned"
    assert await _count_by_session("file_read_claims") == 0, "file_read_claims not cleaned"
    assert await _count_by_session("session_activity") == 0, "session_activity not cleaned"


@pytest.mark.asyncio
async def test_delete_project_in_progress_guard_raises(db):
    """delete_project raises ValueError when in_progress tasks exist."""
    import uuid as _uuid

    pid = (await db_module.create_project(db, f"del-guard-{_uuid.uuid4().hex[:8]}"))["id"]
    sess = await db_module.register_session(db, pid, "guard-sess")
    sid = sess["id"]
    task = await db_module.log_task(db, sid, pid, "running task", status="pending")
    await db_module.claim_task(db, task["id"], sid)

    with pytest.raises(ValueError, match="in_progress"):
        await db_module.delete_project(db, pid)

    # Project must still exist — the guard prevented deletion
    assert await db_module.get_project(db, pid) is not None


def test_is_no_such_table_only_matches_missing_table_errors():
    """_is_no_such_table returns True only for 'no such table' errors, not others.

    This verifies that the narrow exception guard in delete_project will NOT
    swallow a genuine FK-violation or other real database error.
    """
    import aiosqlite
    from meridian.db import _is_no_such_table

    # Should match: SQLite "no such table" error
    assert _is_no_such_table(aiosqlite.OperationalError("no such table: some_table"))
    # Should match: case-insensitive variant
    assert _is_no_such_table(aiosqlite.OperationalError("NO SUCH TABLE: x"))

    # Should NOT match: other OperationalError (e.g. FK violation, locked DB)
    assert not _is_no_such_table(aiosqlite.OperationalError("FOREIGN KEY constraint failed"))
    assert not _is_no_such_table(aiosqlite.OperationalError("database is locked"))
    assert not _is_no_such_table(aiosqlite.OperationalError("disk I/O error"))

    # Should NOT match: arbitrary exceptions
    assert not _is_no_such_table(RuntimeError("something went wrong"))
    assert not _is_no_such_table(ValueError("bad value"))
    assert not _is_no_such_table(Exception("generic error"))


def test_delete_project_http_returns_500_on_db_error(client):
    """DELETE /projects/{id} returns 500 (not 204) when the underlying delete raises.

    Confirms that routes/projects.py does not silently succeed when delete_project
    raises an unexpected exception.  Prior to the fix, the blanket except-pass in
    delete_project meant a real FK violation would cause delete_project to return
    normally (no exception) so the route always returned 204; now a real error
    propagates and FastAPI returns 500.
    """
    import uuid as _uuid
    from unittest.mock import patch, AsyncMock

    p = client.post("/projects", json={"name": f"del-500-{_uuid.uuid4().hex[:6]}"}).json()

    async def _raise(*_a, **_kw):
        raise RuntimeError("forced db error")

    with patch("meridian.db.delete_project", side_effect=_raise):
        r = client.delete(f"/projects/{p['id']}")

    # Must NOT be 204 (false success); should be a server-error status
    assert r.status_code >= 500, (
        f"Expected 5xx when delete_project raises RuntimeError, got {r.status_code}"
    )


# ---------------------------------------------------------------------------
# delete_project: batch (0e4980d4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_project_batch_deletes_multiple_ids(db):
    """delete_project(db, [id1, id2]) deletes every project in one call."""
    import uuid as _uuid

    pid1 = (await db_module.create_project(db, f"batch-a-{_uuid.uuid4().hex[:8]}"))["id"]
    pid2 = (await db_module.create_project(db, f"batch-b-{_uuid.uuid4().hex[:8]}"))["id"]
    pid3 = (await db_module.create_project(db, f"batch-c-{_uuid.uuid4().hex[:8]}"))["id"]

    await db_module.delete_project(db, [pid1, pid2])

    assert await db_module.get_project(db, pid1) is None
    assert await db_module.get_project(db, pid2) is None
    # untouched sibling project survives the batch
    assert await db_module.get_project(db, pid3) is not None


@pytest.mark.asyncio
async def test_delete_project_batch_in_progress_guard_blocks_whole_batch(db):
    """One project with an in_progress task aborts the entire batch (all-or-nothing)."""
    import uuid as _uuid

    pid_clean = (await db_module.create_project(db, f"batch-clean-{_uuid.uuid4().hex[:8]}"))["id"]
    pid_busy = (await db_module.create_project(db, f"batch-busy-{_uuid.uuid4().hex[:8]}"))["id"]
    sess = await db_module.register_session(db, pid_busy, "batch-guard-sess")
    task = await db_module.log_task(db, sess["id"], pid_busy, "running task", status="pending")
    await db_module.claim_task(db, task["id"], sess["id"])

    with pytest.raises(ValueError, match="in_progress"):
        await db_module.delete_project(db, [pid_clean, pid_busy])

    # Neither project was deleted — the guard runs before any DELETE statement.
    assert await db_module.get_project(db, pid_clean) is not None
    assert await db_module.get_project(db, pid_busy) is not None


@pytest.mark.asyncio
async def test_delete_project_single_str_still_works(db):
    """Passing a bare str (the original call shape) still deletes just that project."""
    p = await db_module.create_project(db, "batch-backcompat-proj")
    await db_module.delete_project(db, p["id"])
    assert await db_module.get_project(db, p["id"]) is None


def test_delete_projects_batch_http_deletes_all(client):
    """DELETE /projects?project_id=a&project_id=b removes both and returns a summary."""
    import uuid as _uuid

    p1 = client.post("/projects", json={"name": f"del-batch-http-a-{_uuid.uuid4().hex[:6]}"}).json()
    p2 = client.post("/projects", json={"name": f"del-batch-http-b-{_uuid.uuid4().hex[:6]}"}).json()

    r = client.delete("/projects", params={"project_id": [p1["id"], p2["id"]]})
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 2
    assert set(body["deleted"]) == {p1["id"], p2["id"]}

    assert client.get(f"/projects/{p1['id']}").status_code == 404
    assert client.get(f"/projects/{p2['id']}").status_code == 404


def test_delete_projects_batch_http_404_on_unknown_id(client):
    """Batch delete 404s and deletes nothing when one id in the batch is bogus.

    Note: the app's global 404 handler (meridian/server.py `_404_handler`)
    renders a generic HTML error page for every ``HTTPException(404, ...)``,
    discarding the JSON ``detail`` body — so this only asserts status code +
    survival, matching the convention used by the app's other 404 tests.
    """
    import uuid as _uuid

    p1 = client.post("/projects", json={"name": f"del-batch-http-c-{_uuid.uuid4().hex[:6]}"}).json()
    bogus_id = f"does-not-exist-{_uuid.uuid4().hex[:8]}"

    r = client.delete("/projects", params={"project_id": [p1["id"], bogus_id]})
    assert r.status_code == 404

    # The real project must survive — an unknown id in the batch aborts everything.
    assert client.get(f"/projects/{p1['id']}").status_code == 200


def test_delete_projects_batch_http_requires_project_id_param(client):
    """DELETE /projects with no project_id query param is a 422, not a silent no-op."""
    r = client.delete("/projects")
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# DB-level: get_goal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_goal_returns_none_before_set(db):
    """get_goal returns None for a project with no goal."""
    p = await db_module.create_project(db, "no-goal-db-proj")
    g = await db_module.get_goal(db, p["id"])
    assert g is None


@pytest.mark.asyncio
async def test_get_goal_returns_content_after_set(db):
    """get_goal returns content after set_goal."""
    p = await db_module.create_project(db, "has-goal-db-proj")
    await db_module.set_goal(db, p["id"], "the goal content")
    g = await db_module.get_goal(db, p["id"])
    assert g is not None
    assert g["content"] == "the goal content"


@pytest.mark.asyncio
async def test_get_goal_includes_north_star_field(db):
    """get_goal includes north_star field when set."""
    p = await db_module.create_project(db, "ns-goal-db-proj")
    await db_module.set_goal(db, p["id"], "content", north_star="Be the best")
    g = await db_module.get_goal(db, p["id"])
    assert g["north_star"] == "Be the best"


# ---------------------------------------------------------------------------
# DB-level: update_task, claim_task, release_task
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_task_status_to_done(db):
    """update_task transitions status to done."""
    p = await db_module.create_project(db, "upd-task-db-proj")
    s = await db_module.register_session(db, p["id"], "upd-sess")
    t = await db_module.log_task(db, s["id"], p["id"], "do work", "pending")
    updated = await db_module.update_task(db, t["id"], status="done")
    assert updated["status"] == "done"


@pytest.mark.asyncio
async def test_update_task_description_field(db):
    """update_task can change description."""
    p = await db_module.create_project(db, "desc-upd-proj")
    s = await db_module.register_session(db, p["id"], "desc-sess")
    t = await db_module.log_task(db, s["id"], p["id"], "old desc", "pending")
    updated = await db_module.update_task(db, t["id"], description="new desc")
    assert updated["description"] == "new desc"


@pytest.mark.asyncio
async def test_claim_task_marks_in_progress(db):
    """claim_task sets status to in_progress and stores claimed_by."""
    p = await db_module.create_project(db, "claim-db-proj")
    s = await db_module.register_session(db, p["id"], "claim-sess")
    t = await db_module.log_task(db, s["id"], p["id"], "claimable", "pending")
    claimed = await db_module.claim_task(db, t["id"], s["id"])
    assert claimed is not None
    assert claimed["status"] == "in_progress"
    assert claimed["claimed_by"] == s["id"]


@pytest.mark.asyncio
async def test_claim_task_returns_none_if_already_claimed(db):
    """claim_task returns None if the task is already claimed."""
    p = await db_module.create_project(db, "claim2-db-proj")
    s1 = await db_module.register_session(db, p["id"], "s1-claim")
    s2 = await db_module.register_session(db, p["id"], "s2-claim")
    t = await db_module.log_task(db, s1["id"], p["id"], "exclusive", "pending")
    await db_module.claim_task(db, t["id"], s1["id"])
    second = await db_module.claim_task(db, t["id"], s2["id"])
    assert second is None


@pytest.mark.asyncio
async def test_release_task_returns_true(db):
    """release_task returns True when the claim is held by the session."""
    p = await db_module.create_project(db, "rel-db-proj")
    s = await db_module.register_session(db, p["id"], "rel-sess")
    t = await db_module.log_task(db, s["id"], p["id"], "to release", "pending")
    await db_module.claim_task(db, t["id"], s["id"])
    released = await db_module.release_task(db, t["id"], s["id"])
    assert released is True


# ---------------------------------------------------------------------------
# DB-level: project notes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_and_get_project_note(db):
    """add_project_note stores; get_project_notes retrieves it."""
    p = await db_module.create_project(db, "note-db-proj")
    note = await db_module.add_project_note(db, p["id"], "My Note", "Note body")
    assert note["title"] == "My Note"
    notes = await db_module.get_project_notes(db, p["id"])
    assert any(n["id"] == note["id"] for n in notes)


@pytest.mark.asyncio
async def test_delete_project_note_returns_true(db):
    """delete_project_note removes the note and returns True."""
    p = await db_module.create_project(db, "del-note-db-proj")
    note = await db_module.add_project_note(db, p["id"], "Gone", "to be deleted")
    result = await db_module.delete_project_note(db, note["id"])
    assert result is True
    notes = await db_module.get_project_notes(db, p["id"])
    assert not any(n["id"] == note["id"] for n in notes)


@pytest.mark.asyncio
async def test_get_project_notes_empty_for_new_project(db):
    """get_project_notes returns empty list for a project with no notes."""
    p = await db_module.create_project(db, "empty-notes-proj")
    notes = await db_module.get_project_notes(db, p["id"])
    assert notes == []


# ---------------------------------------------------------------------------
# DB-level: update_pinned_decision
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_pinned_decision_body(db):
    """update_pinned_decision can change the body text."""
    p = await db_module.create_project(db, "dec-upd-db-proj")
    d = await db_module.pin_decision(db, p["id"], "Use SQLite", "original body", "TECHNICAL")
    updated = await db_module.update_pinned_decision(db, d["id"], body="new reason for sqlite")
    assert updated["body"] == "new reason for sqlite"


@pytest.mark.asyncio
async def test_update_pinned_decision_supersede(db):
    """update_pinned_decision can mark a decision as superseded."""
    p = await db_module.create_project(db, "sup-db-proj")
    d = await db_module.pin_decision(db, p["id"], "Old approach", "body", "TECHNICAL")
    updated = await db_module.update_pinned_decision(db, d["id"], status="superseded")
    assert updated["status"] == "superseded"


# ---------------------------------------------------------------------------
# DB-level: get_tasks with limit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_tasks_returns_logged(db):
    """get_tasks returns all tasks for the project."""
    p = await db_module.create_project(db, "get-tasks-db-proj")
    s = await db_module.register_session(db, p["id"], "tasks-sess")
    await db_module.log_task(db, s["id"], p["id"], "first task", "done")
    await db_module.log_task(db, s["id"], p["id"], "second task", "pending")
    tasks = await db_module.get_tasks(db, p["id"], limit=100)
    descs = {t["description"] for t in tasks}
    assert "first task" in descs
    assert "second task" in descs


@pytest.mark.asyncio
async def test_get_tasks_limit_caps_results(db):
    """get_tasks with limit=3 returns at most 3 tasks."""
    p = await db_module.create_project(db, "lim-tasks-db-proj")
    s = await db_module.register_session(db, p["id"], "lim-sess")
    for i in range(8):
        await db_module.log_task(db, s["id"], p["id"], f"task-{i}", "done")
    tasks = await db_module.get_tasks(db, p["id"], limit=3)
    assert len(tasks) <= 3


@pytest.mark.asyncio
async def test_get_pinned_decisions_returns_all(db):
    """get_pinned_decisions returns all pinned decisions."""
    p = await db_module.create_project(db, "multi-dec-db-proj")
    await db_module.pin_decision(db, p["id"], "Use psycopg3", "no asyncpg", "TECHNICAL")
    await db_module.pin_decision(db, p["id"], "Ship weekly", "keep cadence", "STRATEGIC")
    decisions = await db_module.get_pinned_decisions(db, p["id"])
    assert len(decisions) == 2
    categories = {d["category"] for d in decisions}
    assert "TECHNICAL" in categories
    assert "STRATEGIC" in categories


# ---------------------------------------------------------------------------
# HTTP: project get 404
# ---------------------------------------------------------------------------


def test_project_get_unknown_returns_404(client):
    """GET /projects/{id} returns 404 for unknown project."""
    r = client.get("/projects/totally-unknown-id-xyz-abc-zzz")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# HTTP: decisions-pinned CRUD
# ---------------------------------------------------------------------------


def test_decisions_pinned_list_empty_for_new_project(client):
    """GET /projects/{id}/decisions-pinned returns empty list."""
    pid = _make_project(client, "dec-pinned-list")
    r = client.get(f"/projects/{pid}/decisions-pinned")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_decisions_pinned_create_and_list(client):
    """POST /decisions-pinned creates; GET lists it."""
    pid = _make_project(client, "dec-pinned-create")
    r = client.post(f"/projects/{pid}/decisions-pinned", json={
        "title": "Use Postgres", "body": "scales better", "category": "TECHNICAL"
    })
    assert r.status_code == 201
    r2 = client.get(f"/projects/{pid}/decisions-pinned")
    assert r2.status_code == 200
    titles = [d["title"] for d in r2.json()]
    assert "Use Postgres" in titles


def test_decisions_pinned_patch_body(client):
    """PATCH /decisions-pinned/{id} can update the body."""
    pid = _make_project(client, "dec-patch-proj")
    d = client.post(f"/projects/{pid}/decisions-pinned", json={
        "title": "Approach A", "body": "original", "category": "TECHNICAL"
    }).json()
    did = d["id"]
    r = client.patch(f"/projects/{pid}/decisions-pinned/{did}", json={"body": "updated reason"})
    assert r.status_code == 200
    assert r.json()["body"] == "updated reason"


# ---------------------------------------------------------------------------
# HTTP: context-block modes
# ---------------------------------------------------------------------------


def test_context_block_mode_chat(client):
    """GET /context-block?mode=chat returns non-empty text."""
    pid = _make_project(client, "ctx-chat-proj")
    client.post(f"/projects/{pid}/goal", json={"content": "chat goal"})
    r = client.get(f"/projects/{pid}/context-block?mode=chat")
    assert r.status_code == 200
    assert r.text


def test_context_block_default_mode_returns_200(client):
    """GET /context-block without mode param returns 200."""
    pid = _make_project(client, "ctx-default-proj")
    client.post(f"/projects/{pid}/goal", json={"content": "some goal"})
    r = client.get(f"/projects/{pid}/context-block")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# HTTP: handoff generation
# ---------------------------------------------------------------------------


def test_handoff_generate_returns_content(client):
    """POST /projects/{id}/handoff generates a handoff with path and content."""
    pid = _make_project(client, "handoff-http-test")
    client.post(f"/projects/{pid}/goal", json={"content": "build the thing"})
    client.post("/sessions/register", json={"project_id": pid, "name": "hf-sess"})
    r = client.post(f"/projects/{pid}/handoff")
    assert r.status_code == 200
    body = r.json()
    assert "content" in body


# ---------------------------------------------------------------------------
# HTTP: goal includes north_star after set
# ---------------------------------------------------------------------------


def test_goal_get_includes_north_star_http(client):
    """GET /goal returns north_star after it's set."""
    pid = _make_project(client, "ns-goal-http-proj")
    client.post(f"/projects/{pid}/goal", json={"content": "base goal"})
    client.post(f"/projects/{pid}/goal/north-star", json={"north_star": "Be #1", "human_id": "adam"})
    r = client.get(f"/projects/{pid}/goal")
    assert r.status_code == 200
    assert r.json()["north_star"] == "Be #1"


# ---------------------------------------------------------------------------
# HTTP: claimable tasks endpoint
# ---------------------------------------------------------------------------


def test_claimable_tasks_endpoint(client):
    """GET /projects/{id}/tasks/claimable returns list."""
    pid = _make_project(client, "claimable-http-proj")
    s = client.post("/sessions/register", json={"project_id": pid, "name": "claim-sess"}).json()
    client.post("/tasks", json={
        "session_id": s["id"], "project_id": pid,
        "description": "claimable task", "status": "pending"
    })
    r = client.get(f"/projects/{pid}/tasks/claimable")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


# ---------------------------------------------------------------------------
# HTTP: rewind endpoint
# ---------------------------------------------------------------------------


def test_rewind_endpoint_returns_response(client):
    """GET /projects/{id}/rewind returns 200 or 404."""
    pid = _make_project(client, "rewind-http-test")
    r = client.get(f"/projects/{pid}/rewind")
    assert r.status_code in (200, 404)


# ---------------------------------------------------------------------------
# HTTP: tasks with limit query param
# ---------------------------------------------------------------------------


def test_tasks_list_with_limit_param(client):
    """GET /projects/{id}/tasks?limit=2 returns at most 2 items."""
    pid = _make_project(client, "limit-tasks-http")
    s = client.post("/sessions/register", json={"project_id": pid, "name": "lim-sess"}).json()
    for i in range(5):
        client.post("/tasks", json={
            "session_id": s["id"], "project_id": pid,
            "description": f"task-{i}", "status": "done"
        })
    r = client.get(f"/projects/{pid}/tasks?limit=2")
    assert r.status_code == 200
    assert len(r.json()) <= 2


# ---------------------------------------------------------------------------
# HTTP: team summary empty
# ---------------------------------------------------------------------------


def test_team_summary_empty_humans_for_new_project(client):
    """GET /team/summary returns humans=[] for a project with no sessions."""
    pid = _make_project(client, "empty-team-http-proj")
    r = client.get(f"/team/summary?project_id={pid}&days=7")
    assert r.status_code == 200
    assert r.json()["humans"] == []


# ---------------------------------------------------------------------------
# HTTP: sprint items multiple versions
# ---------------------------------------------------------------------------


def test_sprint_items_multiple_versions_listed(client):
    """Sprint items from multiple versions are all returned by GET."""
    pid = _make_project(client, "multi-ver-http-proj")
    _make_sprint_item(client, pid, "v1 task", "v1.0")
    _make_sprint_item(client, pid, "v2 task", "v2.0")
    r = client.get(f"/projects/{pid}/sprint-items")
    assert r.status_code == 200
    versions = {i["version"] for i in r.json()}
    assert "v1.0" in versions and "v2.0" in versions


# ---------------------------------------------------------------------------
# HTTP: sessions list active
# ---------------------------------------------------------------------------


def test_sessions_list_returns_registered(client):
    """GET /projects/{id}/sessions returns the registered sessions."""
    pid = _make_project(client, "sess-http-active")
    client.post("/sessions/register", json={"project_id": pid, "name": "new-sess"})
    r = client.get(f"/projects/{pid}/sessions")
    assert r.status_code == 200
    names = {s["name"] for s in r.json()}
    assert "new-sess" in names


# ---------------------------------------------------------------------------
# Account: export + delete (self-hosted = 404, db functions work)
# ---------------------------------------------------------------------------


def test_export_my_data_404_self_hosted(client):
    """/export/my-data returns 404 in self-hosted mode."""
    r = client.get("/export/my-data")
    assert r.status_code == 404


def test_delete_account_404_self_hosted(client):
    """/account/delete returns 404 in self-hosted mode."""
    r = client.post("/account/delete", json={"confirmation": "DELETE"})
    assert r.status_code == 404


@pytest.mark.anyio
async def test_export_tenant_data_returns_structure():
    """export_tenant_data returns expected top-level keys."""
    db = await db_module.init_db(":memory:")
    proj = await db_module.create_project(db, "export-proj")
    sess = await db_module.register_session(db, proj["id"], "s")
    await db_module.log_task(db, sess["id"], proj["id"], "task 1")

    # Create a minimal tenant row
    import uuid
    tid = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO tenants (id, email, plan) VALUES (?, ?, ?)",
        (tid, "export@example.com", "standard"),
    )
    await db.commit()

    data = await db_module.export_tenant_data(db, tid)

    assert "exported_at" in data
    assert data["tenant"]["email"] == "export@example.com"
    assert isinstance(data["api_tokens"], list)
    assert isinstance(data["workspace_members"], list)
    assert isinstance(data["projects"], list)
    # The project we created should appear
    proj_ids = [p["id"] for p in data["projects"]]
    assert proj["id"] in proj_ids


@pytest.mark.anyio
async def test_delete_tenant_records_removes_rows():
    """delete_tenant_records removes user_sessions, api_tokens, workspace_members, and tenant."""
    db = await db_module.init_db(":memory:")
    import uuid
    tid = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO tenants (id, email, plan) VALUES (?, ?, ?)",
        (tid, "del@example.com", "standard"),
    )
    # Insert a session and token
    from datetime import datetime, timezone, timedelta
    expires = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    await db.execute(
        "INSERT INTO user_sessions (id, tenant_id, expires_at) VALUES (?, ?, ?)",
        (str(uuid.uuid4()), tid, expires),
    )
    await db.execute(
        "INSERT INTO api_tokens (id, tenant_id, token_hash, label) VALUES (?, ?, ?, ?)",
        (str(uuid.uuid4()), tid, "fakehash", "test"),
    )
    await db.commit()

    await db_module.delete_tenant_records(db, tid)

    async with db.execute("SELECT COUNT(*) FROM tenants WHERE id = ?", (tid,)) as cur:
        row = await cur.fetchone()
    assert (row["count"] if isinstance(row, dict) else row[0]) == 0

    async with db.execute("SELECT COUNT(*) FROM user_sessions WHERE tenant_id = ?", (tid,)) as cur:
        row = await cur.fetchone()
    assert (row["count"] if isinstance(row, dict) else row[0]) == 0

    async with db.execute("SELECT COUNT(*) FROM api_tokens WHERE tenant_id = ?", (tid,)) as cur:
        row = await cur.fetchone()
    assert (row["count"] if isinstance(row, dict) else row[0]) == 0


def test_dashboard_js_has_export_button():
    """dashboard frontend contains the export download link."""
    from dashboard_src import dashboard_source
    js = dashboard_source()
    assert "/export/my-data" in js
    assert "Delete my account" in js


# ---------------------------------------------------------------------------
# Dunning flow
# ---------------------------------------------------------------------------


def test_stripe_webhook_payment_failed_ignored_without_customer(client):
    """invoice.payment_failed with no customer ID returns dunning_started (no-op)."""
    r = client.post("/webhooks/stripe", json={
        "type": "invoice.payment_failed",
        "data": {"object": {"customer": "", "customer_email": "x@x.com"}},
    })
    assert r.status_code == 200
    assert r.json()["status"] == "dunning_started"


def test_stripe_webhook_past_due_returns_dunning_started(client):
    """customer.subscription.past_due is handled and returns dunning_started."""
    r = client.post("/webhooks/stripe", json={
        "type": "customer.subscription.past_due",
        "data": {"object": {"customer": "cus_test_abc", "customer_email": "x@x.com"}},
    })
    assert r.status_code == 200
    assert r.json()["status"] == "dunning_started"


def test_stripe_webhook_unknown_event_ignored(client):
    """Unknown Stripe event types return ignored."""
    r = client.post("/webhooks/stripe", json={
        "type": "customer.subscription.deleted",
        "data": {"object": {}},
    })
    assert r.status_code == 200
    assert r.json()["status"] == "ignored"


@pytest.mark.anyio
async def test_dunning_fields_on_tenant_update():
    """payment_failed_at and dunning_email_sent can be set via update_tenant."""
    db = await db_module.init_db(":memory:")
    import uuid
    tid = str(uuid.uuid4())
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).isoformat()
    await db.execute(
        "INSERT INTO tenants (id, email, plan) VALUES (?, ?, ?)",
        (tid, "dunning@example.com", "standard"),
    )
    await db.commit()

    await db_module.update_tenant(db, tid, payment_failed_at=ts, dunning_email_sent=0)
    tenants = await db_module.get_tenants_with_payment_failures(db)
    assert any(t["id"] == tid for t in tenants)

    # Recovery: clear payment_failed_at
    await db_module.update_tenant(db, tid, payment_failed_at=None, dunning_email_sent=0)
    tenants = await db_module.get_tenants_with_payment_failures(db)
    assert not any(t["id"] == tid for t in tenants)


@pytest.mark.anyio
async def test_get_tenant_by_stripe_customer():
    """get_tenant_by_stripe_customer looks up by stripe_customer_id."""
    db = await db_module.init_db(":memory:")
    import uuid
    tid = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO tenants (id, email, plan) VALUES (?, ?, ?)",
        (tid, "stripe@example.com", "standard"),
    )
    await db.commit()
    await db_module.update_tenant(db, tid, stripe_customer_id="cus_test_999")

    found = await db_module.get_tenant_by_stripe_customer(db, "cus_test_999")
    assert found is not None
    assert found["email"] == "stripe@example.com"

    missing = await db_module.get_tenant_by_stripe_customer(db, "cus_nope")
    assert missing is None


# ---------------------------------------------------------------------------
# Overage billing — schema + parsing
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_overage_columns_on_tenant():
    """compute_overage_cap_usd and related fields can be set/read."""
    db = await db_module.init_db(":memory:")
    import uuid
    tid = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO tenants (id, email, plan) VALUES (?, ?, ?)",
        (tid, "over@example.com", "standard"),
    )
    await db.commit()
    await db_module.update_tenant(
        db, tid,
        compute_overage_cap_usd=5.0,
        storage_overage_cap_usd=2.0,
        compute_cu_hours_used=30.5,
        storage_gb_used=0.8,
    )
    t = await db_module.get_tenant_by_id(db, tid)
    assert float(t["compute_overage_cap_usd"]) == 5.0
    assert float(t["storage_overage_cap_usd"]) == 2.0
    assert float(t["compute_cu_hours_used"]) == 30.5
    assert float(t["storage_gb_used"]) == 0.8


@pytest.mark.anyio
async def test_list_tenants_with_neon():
    """list_tenants_with_neon returns only tenants with neon_project_id."""
    db = await db_module.init_db(":memory:")
    import uuid
    t1 = str(uuid.uuid4())
    t2 = str(uuid.uuid4())
    await db.execute("INSERT INTO tenants (id, email, plan) VALUES (?, ?, ?)", (t1, "neon@x.com", "standard"))
    await db.execute("INSERT INTO tenants (id, email, plan) VALUES (?, ?, ?)", (t2, "noneon@x.com", "standard"))
    await db.commit()
    await db_module.update_tenant(db, t1, neon_project_id="proj-abc-123")

    result = await db_module.list_tenants_with_neon(db)
    ids = [r["id"] for r in result]
    assert t1 in ids
    assert t2 not in ids


def test_parse_consumption_metrics_sums_correctly():
    """_parse_consumption_metrics aggregates across periods and timeframes."""
    from meridian.hosted import _parse_consumption_metrics
    data = {
        "periods": [
            {
                "consumption": [
                    {
                        "metrics": [
                            {"metric_name": "compute_unit_seconds", "value": 3600},
                            {"metric_name": "root_branch_bytes_month", "value": 1_000_000_000},
                        ]
                    },
                    {
                        "metrics": [
                            {"metric_name": "compute_unit_seconds", "value": 1800},
                        ]
                    },
                ]
            }
        ]
    }
    m = _parse_consumption_metrics(data)
    assert m["compute_unit_seconds"] == 5400
    assert m["root_branch_bytes_month"] == 1_000_000_000


def test_metrics_to_cu_hours():
    """_metrics_to_cu_hours converts compute_unit_seconds to hours."""
    from meridian.hosted import _metrics_to_cu_hours
    assert _metrics_to_cu_hours({"compute_unit_seconds": 7200}) == 2.0
    assert _metrics_to_cu_hours({}) == 0.0


def test_metrics_to_storage_gb():
    """_metrics_to_storage_gb converts bytes_month to GB."""
    from meridian.hosted import _metrics_to_storage_gb
    result = _metrics_to_storage_gb({"root_branch_bytes_month": 2_000_000_000})
    assert abs(result - 2.0) < 0.001


def test_settings_usage_404_self_hosted(client):
    """GET /settings/usage returns 404 in self-hosted mode."""
    r = client.get("/settings/usage")
    assert r.status_code == 404


def test_settings_usage_patch_404_self_hosted(client):
    """PATCH /settings/usage returns 404 in self-hosted mode."""
    r = client.patch("/settings/usage", json={"compute_cap": 5})
    assert r.status_code == 404


def test_dashboard_js_has_usage_section():
    """dashboard frontend contains the usage progress bar section."""
    from dashboard_src import dashboard_source
    js = dashboard_source()
    assert "/settings/usage" in js
    assert "CU-hrs" in js


# ---------------------------------------------------------------------------
# search_tasks — LIKE-based (SQLite) and MCP dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_tasks_like_returns_matches(db):
    """search_tasks returns tasks whose descriptions match the query."""
    p = await db_module.create_project(db, "search-test")
    s = await db_module.register_session(db, p["id"], "s1")
    await db_module.log_task(db, s["id"], p["id"], "Fix auth bug in login flow", "done")
    await db_module.log_task(db, s["id"], p["id"], "Add rate limiting middleware", "done")
    await db_module.log_task(db, s["id"], p["id"], "Refactor database connection pool", "done")
    results = await db_module.search_tasks(db, p["id"], "rate limiting")
    assert len(results) >= 1
    assert any("rate" in r["description"].lower() for r in results)


@pytest.mark.asyncio
async def test_search_tasks_no_results(db):
    """search_tasks returns empty list when nothing matches."""
    p = await db_module.create_project(db, "search-empty")
    s = await db_module.register_session(db, p["id"], "s1")
    await db_module.log_task(db, s["id"], p["id"], "Fix login", "done")
    results = await db_module.search_tasks(db, p["id"], "xyznonexistentterm")
    assert results == []


@pytest.mark.asyncio
async def test_search_tasks_respects_limit(db):
    """search_tasks limit parameter caps the results."""
    p = await db_module.create_project(db, "search-limit")
    s = await db_module.register_session(db, p["id"], "s1")
    for i in range(10):
        await db_module.log_task(db, s["id"], p["id"], f"Fix bug number {i} in auth", "done")
    results = await db_module.search_tasks(db, p["id"], "Fix bug", limit=3)
    assert len(results) <= 3


@pytest.mark.asyncio
async def test_search_tasks_multiword_non_contiguous(db):
    """fcf90f3a — a multi-word query matches when its terms appear in the
    description but NOT as a contiguous phrase (the old single %query% LIKE
    returned zero here). AND semantics: a term absent everywhere still misses."""
    p = await db_module.create_project(db, "search-multiword")
    s = await db_module.register_session(db, p["id"], "s1")
    await db_module.log_task(
        db, s["id"], p["id"],
        "Fix the authentication bug in the login flow", "done")
    # Terms present but reordered / non-adjacent → previously 0 results.
    results = await db_module.search_tasks(db, p["id"], "authentication login")
    assert len(results) == 1
    assert await db_module.search_tasks(db, p["id"], "authentication payments") == []


def test_multiword_match_clause_helper():
    """fcf90f3a — the term-AND builder: one clause per term (>=2 chars, capped),
    OR-ed across columns, AND-ed across terms; single token falls back to whole."""
    from meridian.db import _multiword_match_clause
    sql, params = _multiword_match_clause(["title", "body"], "RAG problem", op="LIKE")
    assert sql == "((title LIKE ? OR body LIKE ?) AND (title LIKE ? OR body LIKE ?))"
    assert params == ["%RAG%", "%RAG%", "%problem%", "%problem%"]
    sql1, params1 = _multiword_match_clause(["description"], "auth", op="ILIKE")
    assert sql1 == "((description ILIKE ?))"
    assert params1 == ["%auth%"]
    # All-short tokens → fall back to the whole query as one term.
    _, params2 = _multiword_match_clause(["c"], "a b", op="LIKE")
    assert params2 == ["%a b%"]


def test_search_tasks_http_endpoint(client):
    """GET /projects/{id}/tasks/search?q=... returns matching tasks."""
    pid = client.post("/projects", json={"name": "search-http"}).json()["id"]
    sid = client.post(
        "/sessions/register", json={"project_id": pid, "name": "s1"}
    ).json()["id"]
    client.post("/tasks", json={
        "session_id": sid, "project_id": pid,
        "description": "Implement Redis rate limiting", "status": "done"
    })
    r = client.get(f"/projects/{pid}/tasks/search?q=rate+limiting")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list)
    assert any("rate" in t["description"].lower() for t in body)


def test_search_tasks_http_empty_query(client):
    """GET /projects/{id}/tasks/search with no q returns empty list."""
    pid = client.post("/projects", json={"name": "search-empty-q"}).json()["id"]
    r = client.get(f"/projects/{pid}/tasks/search")
    assert r.status_code == 200
    assert r.json() == []


# ---------------------------------------------------------------------------
# 6e25b507 — search_all + get_notes body/notes column search
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_all_searches_sprint_item_notes(db):
    """search_all matches sprint items by notes field, not just title."""
    p = await db_module.create_project(db, "sa-sprint-notes")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "Fix login page")
    await db_module.patch_sprint_item(db, p["id"], item["id"], notes="involves psycopg3 connection pool refactor")
    result = await db_module.search_all(db, p["id"], "psycopg3")
    titles_in_sprint = [i["title"] for i in result["sprint_items"]]
    assert "Fix login page" in titles_in_sprint


@pytest.mark.asyncio
async def test_search_all_sprint_title_still_matches(db):
    """search_all still matches sprint items by title after notes column added."""
    p = await db_module.create_project(db, "sa-sprint-title")
    await db_module.add_sprint_item(db, p["id"], "v1", "Implement rate limiting")
    result = await db_module.search_all(db, p["id"], "rate limiting")
    assert any(i["title"] == "Implement rate limiting" for i in result["sprint_items"])


@pytest.mark.asyncio
async def test_search_all_multiword_terms_across_fields(db):
    """fcf90f3a — search_all matches when each query term appears in SOME text
    field of a row (AND across terms, OR across the row's fields), even when the
    exact phrase never appears contiguously."""
    p = await db_module.create_project(db, "sa-multiword")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "RAG evaluation harness")
    await db_module.patch_sprint_item(
        db, p["id"], item["id"],
        notes="covers the problem definition and retrieval augmented generation")
    # "RAG" is in the title, "problem"/"definition" in the notes — the exact
    # phrase never appears, so the old single-LIKE returned nothing.
    result = await db_module.search_all(db, p["id"], "RAG problem definition")
    assert any(i["title"] == "RAG evaluation harness" for i in result["sprint_items"])
    # Space-separated query terms still match hyphen-joined / interspersed content.
    result2 = await db_module.search_all(db, p["id"], "retrieval generation")
    assert any(i["title"] == "RAG evaluation harness" for i in result2["sprint_items"])


# ---------------------------------------------------------------------------
# 82e0b887 — Postgres tsvector full-text search (search_all / search_tasks).
#   On PG, search matches stemmed / morphological word forms via
#   websearch_to_tsquery + ts_rank. The SQLite path stays pure substring
#   keyword matching (no stemming). These tests prove both:
#     * SQLite: a stemmed non-substring query does NOT match (behavior
#       unchanged — keyword semantics only).
#     * Postgres (gated on TEST_DATABASE_URL, skips cleanly otherwise): the
#       same stemmed query DOES match where the old substring-AND missed.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_all_sqlite_no_stemming_still_keyword(db):
    """82e0b887 / 25155e91 — the SQLite path matches substrings only, no
    stemming. A query where NO term is a substring of the stored text
    ("logon session" vs a note reading "authentication for the users") must NOT
    match on SQLite — stemmed / morphological matching is a Postgres-only
    capability. Guards that the tsvector branch didn't leak into the SQLite
    keyword path.

    (25155e91 changed the query from "authenticating user": under the new
    OR/ranked semantics that DOES match, because "user" is a genuine substring of
    "users" — a keyword hit, not stemming. The miss case now uses terms that
    appear nowhere in the body so it still proves "no stemming on SQLite".)"""
    p = await db_module.create_project(db, "sa-sqlite-nostem")
    await db_module.add_project_note(
        db, p["id"], "Auth design", "authentication for the users")
    # Substring keyword query DOES match (path works as before).
    hit = await db_module.search_all(db, p["id"], "authentication users")
    assert any(n["title"] == "Auth design" for n in hit["notes"])
    # No query term is a substring of the body → no match on SQLite (no FTS /
    # stemming would be needed to relate "logon"/"session" to the stored text).
    miss = await db_module.search_all(db, p["id"], "logon session")
    assert not any(n["title"] == "Auth design" for n in miss["notes"])


@pytest.mark.asyncio
async def test_search_all_pg_tsvector_stemmed_match(db_pg):
    """82e0b887 — on Postgres, search_all matches a STEMMED / non-substring
    query via websearch_to_tsquery. A note body "authentication for the users"
    is found by the query "authenticating user" — the exact substrings
    "authenticating"/"user" never appear, so the old substring-AND (and the
    SQLite path) return zero. SKIPS cleanly when TEST_DATABASE_URL is unset."""
    db = db_pg
    p = await db_module.create_project(db, "sa-pg-tsvector")
    await db_module.add_project_note(
        db, p["id"], "Auth design", "authentication for the users")
    result = await db_module.search_all(db, p["id"], "authenticating user")
    assert any(n["title"] == "Auth design" for n in result["notes"]), (
        "tsvector should stem 'authenticating'->'authentication' and match")
    # Also confirm the other content types stem: a decision body.
    await db_module.pin_decision(
        db, p["id"], "Retry policy",
        "the workers retry failed jobs with backoff", "TECHNICAL")
    dres = await db_module.search_all(db, p["id"], "retrying worker")
    assert any(d["title"] == "Retry policy" for d in dres["decisions"]), (
        "tsvector should stem 'retrying'->'retry', 'worker'->'workers'")


@pytest.mark.asyncio
async def test_search_tasks_pg_tsvector_stemmed_match(db_pg):
    """82e0b887 — search_tasks on Postgres additively matches stemmed queries
    via the tsvector predicate OR'd alongside pg_trgm similarity(). A task
    "authentication for the users" is returned for query "authenticating user"
    even though similarity() alone would likely fall below the 0.05 trigram
    threshold for a non-substring form. SKIPS when TEST_DATABASE_URL is unset."""
    db = db_pg
    p = await db_module.create_project(db, "st-pg-tsvector")
    s = await db_module.register_session(db, p["id"], "s1")
    await db_module.log_task(
        db, s["id"], p["id"], "authentication for the users", "done")
    results = await db_module.search_tasks(db, p["id"], "authenticating user")
    assert any("authentication" in r["description"].lower() for r in results), (
        "tsvector predicate should widen search_tasks to stemmed matches")


@pytest.mark.asyncio
async def test_get_project_notes_query_searches_body(db):
    """get_project_notes query= searches body text, not just tags."""
    p = await db_module.create_project(db, "gnotes-body-search")
    await db_module.add_project_note(
        db, p["id"], "Deploy checklist", "Remember to set DATABASE_URL in production", tags="ops"
    )
    await db_module.add_project_note(
        db, p["id"], "Dev setup", "Run pixi install then pixi run test", tags="setup"
    )
    matches = await db_module.get_project_notes(db, p["id"], query="DATABASE_URL")
    assert len(matches) == 1
    assert matches[0]["title"] == "Deploy checklist"


@pytest.mark.asyncio
async def test_get_project_notes_query_searches_title(db):
    """get_project_notes query= also matches on title."""
    p = await db_module.create_project(db, "gnotes-title-search")
    await db_module.add_project_note(db, p["id"], "Auth migration guide", "Step by step")
    await db_module.add_project_note(db, p["id"], "Unrelated note", "Nothing here")
    matches = await db_module.get_project_notes(db, p["id"], query="migration")
    assert len(matches) == 1
    assert "migration" in matches[0]["title"].lower()


@pytest.mark.asyncio
async def test_get_project_notes_query_and_tag_combined(db):
    """get_project_notes filters by both tag and query when both are provided."""
    p = await db_module.create_project(db, "gnotes-combo")
    await db_module.add_project_note(
        db, p["id"], "Prod secret rotation", "Rotate the API key now", tags="ops,security"
    )
    await db_module.add_project_note(
        db, p["id"], "Dev secret", "Local dev API key", tags="dev"
    )
    await db_module.add_project_note(
        db, p["id"], "Ops checklist", "Deploy and verify health endpoint", tags="ops"
    )
    matches = await db_module.get_project_notes(db, p["id"], tag="ops", query="secret")
    assert len(matches) == 1
    assert matches[0]["title"] == "Prod secret rotation"


def test_get_notes_http_query_param(client):
    """GET /projects/{id}/notes?query=X searches title and body."""
    pid = client.post("/projects", json={"name": "http-notes-query"}).json()["id"]
    client.post(f"/projects/{pid}/notes", json={"title": "Env setup", "body": "Set REDIS_URL in .env file"})
    client.post(f"/projects/{pid}/notes", json={"title": "Deployment", "body": "Push to main to deploy"})
    r = client.get(f"/projects/{pid}/notes?query=REDIS_URL")
    assert r.status_code == 200
    hits = r.json()
    assert len(hits) == 1
    assert hits[0]["title"] == "Env setup"


# ---------------------------------------------------------------------------
# 13e2c1e6 — search_all full-text snippets across body fields
# ---------------------------------------------------------------------------


def test_search_snippet_centers_on_match():
    """_search_snippet returns a window centered on the matching term."""
    text = "alpha beta gamma delta DATABASE_URL epsilon zeta eta theta"
    snip = db_module._search_snippet(text, "database_url", window=10)
    assert "DATABASE_URL" in snip
    # Window of 10 chars each side keeps the snippet far shorter than the body.
    assert len(snip) < len(text)


def test_search_snippet_adds_ellipses_when_clipped():
    """_search_snippet adds leading/trailing ellipses when clipped."""
    text = "x" * 200 + " psycopg3 " + "y" * 200
    snip = db_module._search_snippet(text, "psycopg3", window=20)
    assert snip.startswith("…")
    assert snip.endswith("…")
    assert "psycopg3" in snip


def test_search_snippet_empty_when_no_body_or_no_match():
    """_search_snippet returns '' for empty body or absent term."""
    assert db_module._search_snippet(None, "x") == ""
    assert db_module._search_snippet("", "x") == ""
    assert db_module._search_snippet("hello world", "absent") == ""


@pytest.mark.asyncio
async def test_search_all_returns_body_snippet_for_notes(db):
    """search_all attaches a body snippet for note body matches."""
    p = await db_module.create_project(db, "sa-snippet-notes")
    await db_module.add_project_note(
        db, p["id"], "Ops runbook",
        "The deploy step requires you to export DATABASE_URL before migrating.",
    )
    result = await db_module.search_all(db, p["id"], "DATABASE_URL")
    assert len(result["notes"]) == 1
    assert "DATABASE_URL" in result["notes"][0]["snippet"]


@pytest.mark.asyncio
async def test_search_all_snippet_for_task_decision_sprint(db):
    """search_all snippets cover task descriptions, decision and sprint bodies."""
    p = await db_module.create_project(db, "sa-snippet-all")
    s = await db_module.register_session(db, p["id"], "s1")
    await db_module.log_task(
        db, s["id"], p["id"], "Investigate flaky uvicorn worker restart loop", "done"
    )
    await db_module.pin_decision(
        db, p["id"], "Use SelectorEventLoop",
        "ProactorEventLoop deadlocks on Windows watchfiles imports.",
    )
    item = await db_module.add_sprint_item(db, p["id"], "v1", "Harden event loop")
    await db_module.patch_sprint_item(
        db, p["id"], item["id"], notes="Pin SelectorEventLoop in __main__.py for win32."
    )

    tasks = await db_module.search_all(db, p["id"], "uvicorn")
    assert "uvicorn" in tasks["tasks"][0]["snippet"].lower()

    decs = await db_module.search_all(db, p["id"], "deadlocks")
    assert "deadlocks" in decs["decisions"][0]["snippet"].lower()

    sprints = await db_module.search_all(db, p["id"], "SelectorEventLoop")
    sprint_snips = [i["snippet"] for i in sprints["sprint_items"]]
    assert any("SelectorEventLoop" in s for s in sprint_snips)


@pytest.mark.asyncio
async def test_search_all_title_only_match_has_empty_snippet(db):
    """A title-only sprint match yields an empty body snippet, not an error."""
    p = await db_module.create_project(db, "sa-title-only")
    await db_module.add_sprint_item(db, p["id"], "v1", "Implement rate limiting")
    result = await db_module.search_all(db, p["id"], "rate limiting")
    matches = [i for i in result["sprint_items"] if i["title"] == "Implement rate limiting"]
    assert matches
    assert matches[0]["snippet"] == ""


# ---------------------------------------------------------------------------
# 0dc5a35d — planning_search: ranked, scoped planning search (v1).
#
# search_all is a compatibility universal-search surface with no ranked-
# result contract, no source-type/status/version filters, no explainable
# scores, and no pagination. planning_search is the SEPARATE operation that
# fills that gap (see the big module comment above db.planning_search).
# These tests cover the acceptance criteria's required scenarios: multiword/
# non-contiguous matches, stemming, phrase behavior, ranking stability,
# empty/no-result queries, tenant/project/version isolation, pagination,
# SQLite/Postgres parity, stale-index metadata, and backward compatibility
# (the search_all/search_synthesis-specific backward-compat tests live in
# tests/test_search_all.py per this item's touches_resources).
# ---------------------------------------------------------------------------


async def _make_tenant_scoped_project(db, name, email):
    """Helper: a project whose creator resolves to a real tenant, so
    workspace_proposal (tenant-scoped, not project-scoped) can be exercised."""
    tenant = await db_module.upsert_tenant(db, email)
    project = await db_module.create_project(db, name, human_id=email)
    return project, tenant


# --- contract shape --------------------------------------------------------


@pytest.mark.asyncio
async def test_planning_search_result_contract_shape(db):
    """Every result carries the full required contract: source entity/id,
    title, bounded snippet, deterministic score, rank explanation, status,
    version, created_at — and the envelope carries filters/pagination/
    backend/freshness metadata."""
    p = await db_module.create_project(db, "ps-contract")
    await db_module.add_project_note(
        db, p["id"], "Ops runbook", "alpha beta content for the contract test"
    )
    result = await db_module.planning_search(db, p["id"], "alpha beta")

    assert set(result.keys()) == {
        "query", "filters", "results", "total_matched", "has_more",
        "next_cursor", "backend", "freshness", "skipped_source_types",
    }
    assert result["filters"]["project_id"] == p["id"]
    assert result["results"], "expected at least one match"
    row = result["results"][0]
    assert set(row.keys()) == {
        "source_type", "source_id", "title", "snippet", "score",
        "rank_explanation", "status", "version", "created_at",
    }
    assert row["source_type"] == "note"
    assert isinstance(row["score"], float)
    assert row["rank_explanation"]  # non-empty, human-readable
    assert "alpha" in row["snippet"].lower() or "beta" in row["snippet"].lower()

    freshness = result["freshness"]
    assert set(freshness.keys()) == {
        "index_type", "generated_at", "stale", "capped",
        "capped_source_types", "pool_cap",
    }
    assert result["backend"] in (
        "sqlite_fts5_bm25", "sqlite_bm25_like_fallback", "postgres_tsvector_ts_rank",
    )


# --- multiword / non-contiguous matches ------------------------------------


@pytest.mark.asyncio
async def test_planning_search_multiword_non_contiguous_match(anydb):
    """A record whose text contains several (non-contiguous) query terms but
    not all of them must still be found and ranked above an unrelated
    decoy — the same graceful-degradation contract search_all established."""
    db = anydb
    p = await db_module.create_project(db, "ps-multiword")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "img127 coverage gap")
    await db_module.patch_sprint_item(
        db, p["id"], item["id"],
        notes="single-path BFS traversal misses a coverage gap around img127",
    )
    result = await db_module.planning_search(
        db, p["id"], "img127 coverage gap single-path BFS x=768 x=1511",
        source_types=["sprint_item"],
    )
    titles = [r["title"] for r in result["results"]]
    assert "img127 coverage gap" in titles


@pytest.mark.asyncio
async def test_planning_search_partial_match_beats_unrelated_decoy(anydb):
    db = anydb
    p = await db_module.create_project(db, "ps-partial")
    await db_module.add_project_note(
        db, p["id"], "Relevant", "img127 coverage gap single-path notes"
    )
    await db_module.add_project_note(
        db, p["id"], "Decoy", "completely unrelated content about billing"
    )
    result = await db_module.planning_search(
        db, p["id"], "img127 coverage gap single-path BFS x=768 x=1511",
        source_types=["note"],
    )
    titles = [r["title"] for r in result["results"]]
    assert "Relevant" in titles
    assert "Decoy" not in titles


# --- stemming ----------------------------------------------------------


@pytest.mark.asyncio
async def test_planning_search_stemming_sqlite_fts5(db):
    """'authenticating' must find a row containing 'authentication' — real
    linguistic stemming via SQLite FTS5's 'porter unicode61' tokenizer."""
    p = await db_module.create_project(db, "ps-stem-fts5")
    await db_module.add_project_note(
        db, p["id"], "Auth note",
        "authentication for the user is required before checkout",
    )
    result = await db_module.planning_search(
        db, p["id"], "authenticating", source_types=["note"]
    )
    assert result["backend"] == "sqlite_fts5_bm25"
    assert any(r["title"] == "Auth note" for r in result["results"])


@pytest.mark.asyncio
async def test_planning_search_stemming_sqlite_bm25_fallback(db, monkeypatch):
    """Same stemming behavior must hold on the BM25-fallback tier (naive
    stemmer) when the sqlite3 build lacks FTS5 — forced via monkeypatch."""
    async def _unavailable(_db):
        return False

    monkeypatch.setattr(db_module, "_sqlite_fts5_available", _unavailable)
    p = await db_module.create_project(db, "ps-stem-fallback")
    await db_module.add_project_note(
        db, p["id"], "Auth note",
        "authentication for the user is required before checkout",
    )
    result = await db_module.planning_search(
        db, p["id"], "authenticating", source_types=["note"]
    )
    assert result["backend"] == "sqlite_bm25_like_fallback"
    assert any(r["title"] == "Auth note" for r in result["results"])


def test_planning_search_naive_stem_helper():
    """0dc5a35d — the fallback tier's naive stemmer bridges the specific
    verb/noun-form pairs the search tests rely on."""
    assert db_module._planning_naive_stem("authenticating") == db_module._planning_naive_stem(
        "authentication"
    )
    assert db_module._planning_naive_stem("cats") == db_module._planning_naive_stem("cat")
    # Short words are left alone (stem would be too short to be meaningful).
    assert db_module._planning_naive_stem("is") == "is"


# --- phrase behavior ---------------------------------------------------


@pytest.mark.asyncio
async def test_planning_search_phrase_match_sqlite_fts5(db):
    """A quoted phrase requires the words CONTIGUOUS and in order; a note
    with the same words scrambled must not match the phrase query."""
    p = await db_module.create_project(db, "ps-phrase-fts5")
    await db_module.add_project_note(
        db, p["id"], "Contiguous", "single-path BFS misses a coverage gap around img127"
    )
    await db_module.add_project_note(
        db, p["id"], "Scrambled", "gap coverage img127 out of phrase order decoy text"
    )
    result = await db_module.planning_search(
        db, p["id"], '"coverage gap"', source_types=["note"]
    )
    titles = [r["title"] for r in result["results"]]
    assert "Contiguous" in titles
    assert "Scrambled" not in titles


@pytest.mark.asyncio
async def test_planning_search_phrase_match_sqlite_bm25_fallback(db, monkeypatch):
    async def _unavailable(_db):
        return False

    monkeypatch.setattr(db_module, "_sqlite_fts5_available", _unavailable)
    p = await db_module.create_project(db, "ps-phrase-fallback")
    await db_module.add_project_note(
        db, p["id"], "Contiguous", "single-path BFS misses a coverage gap around img127"
    )
    await db_module.add_project_note(
        db, p["id"], "Scrambled", "gap coverage img127 out of phrase order decoy text"
    )
    result = await db_module.planning_search(
        db, p["id"], '"coverage gap"', source_types=["note"]
    )
    assert result["backend"] == "sqlite_bm25_like_fallback"
    titles = [r["title"] for r in result["results"]]
    assert "Contiguous" in titles
    assert "Scrambled" not in titles


def test_planning_pg_tsquery_source_preserves_phrase_quotes():
    """0dc5a35d — unlike _or_tsquery_source (search_all's helper, which
    strips quotes and OR's every word individually), the planning_search
    helper keeps a quoted phrase intact so websearch_to_tsquery treats it as
    a FOLLOWED-BY phrase, not two independent OR'd words."""
    src = db_module._planning_pg_tsquery_source('"coverage gap" single-path')
    assert '"coverage gap"' in src
    assert "single-path" in src
    # Contrast with the OR-degradation helper search_all uses, which would
    # have split the phrase into independently OR'd words.
    assert db_module._or_tsquery_source('"coverage gap"') == "coverage or gap"


# --- ranking stability / tie-break -----------------------------------------


@pytest.mark.asyncio
async def test_planning_search_ranks_more_complete_matches_first(anydb):
    db = anydb
    p = await db_module.create_project(db, "ps-rank")
    await db_module.add_project_note(
        db, p["id"], "more", "alpha beta gamma appear together here"
    )
    await db_module.add_project_note(
        db, p["id"], "fewer", "alpha appears but not the others at all"
    )
    result = await db_module.planning_search(
        db, p["id"], "alpha beta gamma", source_types=["note"]
    )
    titles = [r["title"] for r in result["results"]]
    assert titles[:2] == ["more", "fewer"], f"expected the fuller match ranked first, got {titles}"


@pytest.mark.asyncio
async def test_planning_search_deterministic_order_across_repeated_calls(db):
    """Identical queries against unchanged data must return results in the
    EXACT same order every time — the stable tie-break contract."""
    p = await db_module.create_project(db, "ps-stable")
    for i in range(6):
        await db_module.add_project_note(db, p["id"], f"Note {i}", "alpha shared term")
    r1 = await db_module.planning_search(db, p["id"], "alpha", source_types=["note"])
    r2 = await db_module.planning_search(db, p["id"], "alpha", source_types=["note"])
    ids1 = [r["source_id"] for r in r1["results"]]
    ids2 = [r["source_id"] for r in r2["results"]]
    assert ids1 == ids2
    assert len(ids1) == len(set(ids1)), "no duplicate rows within a single page"


# --- empty / no-result queries ----------------------------------------


@pytest.mark.asyncio
async def test_planning_search_empty_query_returns_no_results_no_error(db):
    p = await db_module.create_project(db, "ps-empty")
    await db_module.add_project_note(db, p["id"], "N", "alpha beta gamma")
    result = await db_module.planning_search(db, p["id"], "")
    assert result["results"] == []
    assert result["total_matched"] == 0
    assert result["has_more"] is False
    assert result["next_cursor"] is None


@pytest.mark.asyncio
async def test_planning_search_whitespace_only_query_returns_no_results(db):
    p = await db_module.create_project(db, "ps-whitespace")
    await db_module.add_project_note(db, p["id"], "N", "alpha beta gamma")
    result = await db_module.planning_search(db, p["id"], "   ")
    assert result["results"] == []


@pytest.mark.asyncio
async def test_planning_search_unrelated_query_returns_no_results(anydb):
    db = anydb
    p = await db_module.create_project(db, "ps-unrelated")
    await db_module.add_sprint_item(db, p["id"], "v1", "img127 coverage gap")
    result = await db_module.planning_search(db, p["id"], "nonexistent zzzzz qqqqq")
    assert result["total_matched"] == 0
    assert result["results"] == []


# --- project / version / tenant isolation ----------------------------------


@pytest.mark.asyncio
async def test_planning_search_project_isolation(db):
    """A row that matches the query but belongs to a DIFFERENT project must
    never appear — every source type is scoped by project_id (or, for
    workspace_proposal, by tenant_id — see the tenant test below)."""
    p1 = await db_module.create_project(db, "ps-iso-1")
    p2 = await db_module.create_project(db, "ps-iso-2")
    await db_module.add_project_note(db, p2["id"], "Other project note", "alpha shared secret")
    result = await db_module.planning_search(db, p1["id"], "alpha", source_types=["note"])
    assert result["results"] == []


@pytest.mark.asyncio
async def test_planning_search_version_isolation(db):
    """The version filter scopes sprint_items exactly — a match in a
    DIFFERENT version must not leak into a version-filtered query."""
    p = await db_module.create_project(db, "ps-version")
    await db_module.add_sprint_item(db, p["id"], "v1", "alpha sprint item one")
    await db_module.add_sprint_item(db, p["id"], "v2", "alpha sprint item two")
    result = await db_module.planning_search(
        db, p["id"], "alpha", source_types=["sprint_item"], version="v1"
    )
    titles = [r["title"] for r in result["results"]]
    assert titles == ["alpha sprint item one"]


@pytest.mark.asyncio
async def test_planning_search_workspace_proposal_tenant_scoped_positive(db):
    """workspace_proposal is workspace(tenant)-scoped, not project-scoped
    (5c4dcc0f) — a proposal for the project's OWN resolved tenant IS found."""
    p, tenant = await _make_tenant_scoped_project(db, "ps-tenant-pos", "owner@example.com")
    await ws_module.add_workspace_proposal(
        db, "Alpha proposal", "alpha idea body text", tenant_id=tenant["id"]
    )
    result = await db_module.planning_search(
        db, p["id"], "alpha", source_types=["workspace_proposal"]
    )
    titles = [r["title"] for r in result["results"]]
    assert "Alpha proposal" in titles
    assert result["skipped_source_types"] == {}


@pytest.mark.asyncio
async def test_planning_search_workspace_proposal_cross_tenant_isolation(db):
    """A proposal that belongs to a DIFFERENT tenant than the querying
    project's own resolved tenant must never leak into the results."""
    p, _own_tenant = await _make_tenant_scoped_project(db, "ps-tenant-self", "self@example.com")
    other_tenant = await db_module.upsert_tenant(db, "other@example.com")
    await ws_module.add_workspace_proposal(
        db, "Other tenant proposal", "alpha idea body text", tenant_id=other_tenant["id"]
    )
    result = await db_module.planning_search(
        db, p["id"], "alpha", source_types=["workspace_proposal"]
    )
    assert result["results"] == []


@pytest.mark.asyncio
async def test_planning_search_workspace_proposal_skipped_when_no_tenant(db):
    """A project with no resolvable tenant (self-hosted / no creator_human_id)
    SKIPS workspace_proposal explicitly rather than erroring or guessing."""
    p = await db_module.create_project(db, "ps-no-tenant")
    result = await db_module.planning_search(
        db, p["id"], "alpha", source_types=["workspace_proposal"]
    )
    assert result["results"] == []
    assert "workspace_proposal" in result["skipped_source_types"]


# --- status filter / type filter -----------------------------------------


@pytest.mark.asyncio
async def test_planning_search_decision_defaults_to_active_only(db):
    """Matches search_all's/every other read path's convention: decisions
    default to active-only unless status is explicitly given."""
    p = await db_module.create_project(db, "ps-decision-status")
    await db_module.pin_decision(db, p["id"], "Active decision alpha", "alpha body text")
    archived = await db_module.pin_decision(db, p["id"], "Archived decision alpha", "alpha body")
    await db_module.update_pinned_decision(db, archived["id"], status="superseded")

    default_result = await db_module.planning_search(
        db, p["id"], "alpha", source_types=["decision"]
    )
    assert [r["title"] for r in default_result["results"]] == ["Active decision alpha"]

    explicit_result = await db_module.planning_search(
        db, p["id"], "alpha", source_types=["decision"], status="superseded"
    )
    assert [r["title"] for r in explicit_result["results"]] == ["Archived decision alpha"]


@pytest.mark.asyncio
async def test_planning_search_source_type_filter_restricts_results(db):
    p = await db_module.create_project(db, "ps-type-filter")
    await db_module.add_project_note(db, p["id"], "Alpha note", "alpha shared term")
    await db_module.add_sprint_item(db, p["id"], "v1", "alpha shared term sprint")
    both = await db_module.planning_search(db, p["id"], "alpha")
    assert {r["source_type"] for r in both["results"]} == {"note", "sprint_item"}

    notes_only = await db_module.planning_search(
        db, p["id"], "alpha", source_types=["note"]
    )
    assert {r["source_type"] for r in notes_only["results"]} == {"note"}


@pytest.mark.asyncio
async def test_planning_search_unknown_source_type_yields_empty_not_ignored(db):
    """An explicit source_types filter that resolves to no known type is
    honoured (zero results), not silently treated as 'search everything'."""
    p = await db_module.create_project(db, "ps-bogus-type")
    await db_module.add_project_note(db, p["id"], "Alpha note", "alpha shared term")
    result = await db_module.planning_search(
        db, p["id"], "alpha", source_types=["not_a_real_type"]
    )
    assert result["results"] == []
    assert result["filters"]["source_types"] == []


# --- findings source type ---------------------------------------------


@pytest.mark.asyncio
async def test_planning_search_finding_source_type(db):
    p = await db_module.create_project(db, "ps-finding")
    await db_module.store_finding(
        db, p["id"], "alpha finding content body", title="Alpha finding"
    )
    result = await db_module.planning_search(db, p["id"], "alpha", source_types=["finding"])
    titles = [r["title"] for r in result["results"]]
    assert "Alpha finding" in titles


# --- pagination --------------------------------------------------------


@pytest.mark.asyncio
async def test_planning_search_pagination_covers_all_rows_no_dupes_no_gaps(db):
    p = await db_module.create_project(db, "ps-page")
    for i in range(5):
        await db_module.add_project_note(db, p["id"], f"Note {i}", f"alpha beta gamma common {i}")

    seen: "list[str]" = []
    cursor = 0
    pages = 0
    while True:
        result = await db_module.planning_search(
            db, p["id"], "alpha beta gamma", source_types=["note"], limit=2, cursor=cursor
        )
        seen.extend(r["source_id"] for r in result["results"])
        pages += 1
        assert pages <= 10, "pagination did not terminate"
        if not result["has_more"]:
            assert result["next_cursor"] is None
            break
        cursor = result["next_cursor"]
    assert len(seen) == len(set(seen)) == 5


@pytest.mark.asyncio
async def test_planning_search_cursor_matches_get_project_notes_page_contract(db):
    """The pagination envelope mirrors get_project_notes_page's existing
    cursor contract (has_more / next_cursor / integer offset cursor)."""
    p = await db_module.create_project(db, "ps-page-contract")
    for i in range(3):
        await db_module.add_project_note(db, p["id"], f"Note {i}", "alpha term")
    page1 = await db_module.planning_search(
        db, p["id"], "alpha", source_types=["note"], limit=2, cursor=0
    )
    assert page1["has_more"] is True
    assert page1["next_cursor"] == 2
    page2 = await db_module.planning_search(
        db, p["id"], "alpha", source_types=["note"], limit=2, cursor=page1["next_cursor"]
    )
    assert page2["has_more"] is False
    assert page2["next_cursor"] is None
    assert len(page1["results"]) + len(page2["results"]) == 3


# --- SQLite / Postgres parity ----------------------------------------


def test_planning_search_dialect_splits_on_is_pg():
    """Static structural check (no live Postgres needed, mirrors
    test_search_all_takes_pg_path_for_postgres_shaped_db): planning_search
    forks on hasattr(db, '_pool') and the Postgres branch uses ts_rank /
    websearch_to_tsquery while the SQLite branch uses the FTS5/BM25 helpers."""
    import inspect

    src = inspect.getsource(db_module.planning_search)
    assert "_pool" in src
    assert "_planning_pg_source_results" in src
    assert "_planning_sqlite_source_results" in src


@pytest.mark.asyncio
async def test_planning_search_same_top_result_across_backends(anydb):
    """Cross-backend parity: given the same corpus, both backends surface
    the same best match as the #1 result (exact score formulas legitimately
    differ — ts_rank vs. bm25 — but relevance ordering must agree on the
    obvious case)."""
    db = anydb
    p = await db_module.create_project(db, "ps-parity")
    await db_module.add_project_note(
        db, p["id"], "more", "alpha beta gamma appear together repeatedly here"
    )
    await db_module.add_project_note(
        db, p["id"], "fewer", "alpha appears but not the others at all"
    )
    result = await db_module.planning_search(
        db, p["id"], "alpha beta gamma", source_types=["note"]
    )
    assert result["results"][0]["title"] == "more"


@pytest.mark.asyncio
async def test_planning_search_postgres_backend_label(db_pg):
    """PG-only (auto-skips locally without TEST_DATABASE_URL): the backend
    metadata correctly reports the Postgres tsvector/ts_rank path."""
    p = await db_module.create_project(db_pg, "ps-pg-backend")
    await db_module.add_project_note(db_pg, p["id"], "N", "alpha beta gamma")
    result = await db_module.planning_search(db_pg, p["id"], "alpha", source_types=["note"])
    assert result["backend"] == "postgres_tsvector_ts_rank"
    assert result["freshness"]["index_type"] == "on_the_fly_tsvector"


# --- stale-index / freshness metadata --------------------------------------


@pytest.mark.asyncio
async def test_planning_search_freshness_metadata_always_fresh(db):
    """Every backend here is computed fresh per call (on-the-fly tsvector on
    PG, an ephemeral connection-local TEMP fts5 index or computed BM25 on
    SQLite) — there is no persisted index that could go stale, so
    freshness.stale is always False and generated_at is a real timestamp."""
    p = await db_module.create_project(db, "ps-fresh")
    await db_module.add_project_note(db, p["id"], "N", "alpha beta gamma")
    result = await db_module.planning_search(db, p["id"], "alpha", source_types=["note"])
    freshness = result["freshness"]
    assert freshness["stale"] is False
    assert freshness["generated_at"]
    assert freshness["capped"] is False
    assert freshness["capped_source_types"] == []
    assert freshness["pool_cap"] == db_module._PLANNING_SEARCH_POOL_CAP


@pytest.mark.asyncio
async def test_planning_search_capped_pool_flagged_in_metadata(db, monkeypatch):
    """When a source type's candidate pool exceeds the bounded cap, the
    response explicitly flags it as capped (search_all's own 'never return
    unbounded raw rows' constraint, made visible/inspectable here)."""
    monkeypatch.setattr(db_module, "_PLANNING_SEARCH_POOL_CAP", 3)
    p = await db_module.create_project(db, "ps-capped")
    for i in range(6):
        await db_module.add_project_note(db, p["id"], f"Note {i}", "alpha shared term")
    result = await db_module.planning_search(
        db, p["id"], "alpha", source_types=["note"], limit=100
    )
    assert result["freshness"]["capped"] is True
    assert result["freshness"]["capped_source_types"] == ["note"]
    assert result["total_matched"] == 3


# --- backend selection & fallback plumbing ----------------------------


@pytest.mark.asyncio
async def test_planning_search_backend_reflects_fts5_probe(db, monkeypatch):
    async def _unavailable(_db):
        return False

    monkeypatch.setattr(db_module, "_sqlite_fts5_available", _unavailable)
    p = await db_module.create_project(db, "ps-backend-fallback")
    await db_module.add_project_note(db, p["id"], "N", "alpha beta gamma")
    result = await db_module.planning_search(db, p["id"], "alpha", source_types=["note"])
    assert result["backend"] == "sqlite_bm25_like_fallback"
    assert result["freshness"]["index_type"] == "computed_bm25_over_candidate_pool"


def test_sqlite_fts5_temp_table_never_persists_to_main_schema():
    """0dc5a35d — the FTS5 index used by planning_search is a connection-
    local TEMP table (never written to the actual database file), so this
    feature needed no schema migration. Structural guard against a future
    change accidentally promoting it to a persistent table."""
    import inspect

    src = inspect.getsource(db_module._planning_sqlite_fts5_rank)
    assert "temp." in src, "the FTS5 virtual table must be created in the temp schema"
    assert "CREATE VIRTUAL TABLE temp." in src


# --- MCP handler --------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_planning_search_returns_planning_search_contract(db):
    p = await db_module.create_project(db, "ps-handler")
    await db_module.add_project_note(db, p["id"], "Handler note", "alpha beta content")
    result = await st_mod.handle_planning_search(
        {"project_id": p["id"], "query": "alpha beta", "source_types": ["note"]},
        db, "/tmp", None, None,
    )
    assert result["results"]
    assert result["results"][0]["title"] == "Handler note"


@pytest.mark.asyncio
async def test_handle_planning_search_requires_project_id(db):
    result = await st_mod.handle_planning_search(
        {"query": "alpha"}, db, "/tmp", None, None,
    )
    assert "error" in result


@pytest.mark.asyncio
async def test_handle_planning_search_accepts_empty_query(db):
    """An explicitly empty query string is a VALID call (zero results, no
    error) — only a missing project_id/query key is rejected."""
    p = await db_module.create_project(db, "ps-handler-empty")
    result = await st_mod.handle_planning_search(
        {"project_id": p["id"], "query": ""}, db, "/tmp", None, None,
    )
    assert "error" not in result
    assert result["results"] == []
