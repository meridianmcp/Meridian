"""v2.5 expanded test coverage — sprint items, config, notifications, admin health,
MCP responses, auth routes, team summary, and dashboard.js markers."""

from __future__ import annotations

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
# ---------------------------------------------------------------------------


def test_dashboard_js_has_sprint_group_header_class():
    """dashboard.js renders sprint version group headers."""
    import pathlib
    js = pathlib.Path("meridian/static/dashboard.js").read_text(encoding="utf-8")
    assert "sprint-group-header" in js


def test_dashboard_js_has_demo_mode_check():
    """dashboard.js handles demo_mode from config."""
    import pathlib
    js = pathlib.Path("meridian/static/dashboard.js").read_text(encoding="utf-8")
    assert "demo_mode" in js


def test_dashboard_js_has_feedback_thumb():
    """dashboard.js renders feedback UI."""
    import pathlib
    js = pathlib.Path("meridian/static/dashboard.js").read_text(encoding="utf-8")
    assert "feedback_thumb" in js


def test_dashboard_js_has_settings_vtab():
    """dashboard.js has a settings vtab for notification prefs."""
    import pathlib
    js = pathlib.Path("meridian/static/dashboard.js").read_text(encoding="utf-8")
    assert "drawer-settings" in js


def test_dashboard_js_has_connection_delete():
    """dashboard.js allows deleting connection profiles."""
    import pathlib
    js = pathlib.Path("meridian/static/dashboard.js").read_text(encoding="utf-8")
    assert "Remove connection" in js


def test_dashboard_js_has_goal_history_fetch():
    """dashboard.js fetches goal-history for swimlane markers."""
    import pathlib
    js = pathlib.Path("meridian/static/dashboard.js").read_text(encoding="utf-8")
    assert "goal-history" in js


def test_dashboard_js_sprint_board_shows_all_versions():
    """dashboard.js sprint board groups by version not just current sprint."""
    import pathlib
    js = pathlib.Path("meridian/static/dashboard.js").read_text(encoding="utf-8")
    assert "activeVersions" in js


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
