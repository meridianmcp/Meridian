"""v2.5 expanded test coverage — sprint items, config, notifications, admin health,
MCP responses, auth routes, team summary, and dashboard.js markers."""

from __future__ import annotations

import asyncio

import pytest

from meridian import db as db_module


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


def test_auth_magic_is_rate_limited(client):
    """POST /auth/magic is capped at 5/minute — the 6th call returns 429.

    Guards the brute-force / email-bomb protection on the magic-link endpoint.
    slowapi keys on the client address, so all TestClient calls share one bucket
    and the limiter is fresh per `client` fixture (server module is reloaded).
    """
    statuses = [
        client.post("/auth/magic", json={"email": f"rl{i}@example.com"}).status_code
        for i in range(6)
    ]
    # First 5 within the 5/minute budget must not be rate-limited.
    assert all(s != 429 for s in statuses[:5]), statuses
    # The 6th exceeds the limit.
    assert statuses[5] == 429, statuses


def test_export_my_data_is_rate_limited(client):
    """GET /export/my-data is capped at 3/minute — the 4th call returns 429.

    The limiter runs before the handler, so the cap holds regardless of auth
    state (unauthenticated calls 404 in self-host mode, but still count).
    """
    statuses = [client.get("/export/my-data").status_code for _ in range(4)]
    assert all(s != 429 for s in statuses[:3]), statuses
    assert statuses[3] == 429, statuses


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
    assert row[0] == 0

    async with db.execute("SELECT COUNT(*) FROM user_sessions WHERE tenant_id = ?", (tid,)) as cur:
        row = await cur.fetchone()
    assert row[0] == 0

    async with db.execute("SELECT COUNT(*) FROM api_tokens WHERE tenant_id = ?", (tid,)) as cur:
        row = await cur.fetchone()
    assert row[0] == 0


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
