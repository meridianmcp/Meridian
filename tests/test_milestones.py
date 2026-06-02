"""Tests for milestone_type field on sprint_items."""

from __future__ import annotations

import asyncio
import json
import pytest

from meridian import db as db_module


def _setup_authed_project(client, project_name: str) -> tuple[str, dict]:
    """Create project + tenant + API token. Returns (project_id, mcp_headers)."""
    proj_r = client.post("/projects", json={"name": project_name})
    assert proj_r.status_code == 201
    pid = proj_r.json()["id"]

    async def _create_token():
        db = client.app.state.db
        tenant = await db_module.upsert_tenant(db, f"{project_name}@test.invalid")
        raw, _ = await db_module.create_api_token(db, tenant["id"])
        return raw

    token = asyncio.run(_create_token())
    return pid, {"Authorization": f"Bearer {token}"}


def _mcp_call(client, headers, name, arguments):
    return client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": name, "arguments": arguments}},
        headers=headers,
    )


# ---------------------------------------------------------------------------
# DB layer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_sprint_item_default_milestone_type(db):
    p = await db_module.create_project(db, "milestone-test")
    item = await db_module.add_sprint_item(db, p["id"], "v1.0", "Ship it")
    assert item["milestone_type"] == "task"


@pytest.mark.asyncio
async def test_add_sprint_item_milestone_type(db):
    p = await db_module.create_project(db, "milestone-test")
    item = await db_module.add_sprint_item(
        db, p["id"], "v1.0", "First paying customer",
        milestone_type="milestone"
    )
    assert item["milestone_type"] == "milestone"


@pytest.mark.asyncio
async def test_add_sprint_item_invalid_milestone_type(db):
    p = await db_module.create_project(db, "milestone-test")
    with pytest.raises(ValueError, match="milestone_type"):
        await db_module.add_sprint_item(
            db, p["id"], "v1.0", "Bad item",
            milestone_type="invalid"
        )


@pytest.mark.asyncio
async def test_get_sprint_items_includes_milestone_type(db):
    p = await db_module.create_project(db, "milestone-test")
    await db_module.add_sprint_item(db, p["id"], "v1.0", "Task A")
    await db_module.add_sprint_item(
        db, p["id"], "v1.0", "Milestone X",
        milestone_type="milestone"
    )
    items = await db_module.get_sprint_items(db, p["id"])
    assert len(items) == 2
    types = {i["title"]: i["milestone_type"] for i in items}
    assert types["Task A"] == "task"
    assert types["Milestone X"] == "milestone"


@pytest.mark.asyncio
async def test_milestone_items_are_returned_in_regular_list(db):
    p = await db_module.create_project(db, "milestone-test")
    for i in range(3):
        await db_module.add_sprint_item(db, p["id"], "v1.0", f"Task {i}")
    await db_module.add_sprint_item(
        db, p["id"], "v1.0", "Big milestone", milestone_type="milestone"
    )
    items = await db_module.get_sprint_items(db, p["id"])
    assert len(items) == 4
    milestones = [i for i in items if i["milestone_type"] == "milestone"]
    assert len(milestones) == 1
    assert milestones[0]["title"] == "Big milestone"


@pytest.mark.asyncio
async def test_milestone_field_persists_after_status_update(db):
    p = await db_module.create_project(db, "milestone-test")
    m = await db_module.add_sprint_item(
        db, p["id"], "v1.0", "Ship v1", milestone_type="milestone"
    )
    await db_module.complete_sprint_item(db, p["id"], m["id"])
    updated = await db_module.get_sprint_item(db, m["id"])
    assert updated is not None
    assert updated["milestone_type"] == "milestone"
    assert updated["status"] == "done"


@pytest.mark.asyncio
async def test_multiple_milestones(db):
    p = await db_module.create_project(db, "milestone-test")
    milestones = [
        "v1.0-alpha-shipped",
        "first-paying-customer",
        "100-stars",
        "hosted-beta-open",
    ]
    for name in milestones:
        await db_module.add_sprint_item(
            db, p["id"], "launch", name, milestone_type="milestone"
        )
    items = await db_module.get_sprint_items(db, p["id"])
    found = {i["title"] for i in items if i["milestone_type"] == "milestone"}
    assert found == set(milestones)


# ---------------------------------------------------------------------------
# REST endpoint
# ---------------------------------------------------------------------------


def test_rest_sprint_items_milestone_type_field(client):
    r = client.post("/projects", json={"name": "milestone-rest-test"})
    assert r.status_code == 201
    pid = r.json()["id"]
    # Create a regular task and a milestone via REST
    r2 = client.post(f"/projects/{pid}/sprint-items",
                     json={"version": "v1.0", "title": "Regular task"})
    assert r2.status_code == 201
    assert r2.json()["milestone_type"] == "task"


def test_rest_sprint_items_returns_milestone_type(client):
    r = client.post("/projects", json={"name": "milestone-rest-test"})
    pid = r.json()["id"]
    client.post(f"/projects/{pid}/sprint-items",
                json={"version": "v1.0", "title": "A task"})
    items = client.get(f"/projects/{pid}/sprint-items").json()
    assert len(items) == 1
    assert "milestone_type" in items[0]
    assert items[0]["milestone_type"] == "task"


def test_mcp_add_sprint_item_milestone_type(client):
    pid, headers = _setup_authed_project(client, "ms-mcp-milestone-test")
    resp = _mcp_call(client, headers, "add_sprint_item", {
        "project_id": pid,
        "version": "v1.0",
        "title": "First Paying Customer",
        "milestone_type": "milestone",
    })
    assert resp.status_code == 200
    result = json.loads(resp.json()["result"]["content"][0]["text"])
    assert result["milestone_type"] == "milestone"
    assert result["title"] == "First Paying Customer"


def test_mcp_add_sprint_item_default_is_task(client):
    pid, headers = _setup_authed_project(client, "ms-mcp-default-test")
    resp = _mcp_call(client, headers, "add_sprint_item", {
        "project_id": pid,
        "version": "v1.0",
        "title": "Normal task",
    })
    assert resp.status_code == 200
    result = json.loads(resp.json()["result"]["content"][0]["text"])
    assert result["milestone_type"] == "task"


def test_mcp_add_sprint_item_invalid_milestone_type_returns_error(client):
    pid, headers = _setup_authed_project(client, "ms-mcp-invalid-test")
    resp = _mcp_call(client, headers, "add_sprint_item", {
        "project_id": pid,
        "version": "v1.0",
        "title": "Bad item",
        "milestone_type": "bad_type",
    })
    assert resp.status_code == 200
    body = resp.json()
    # ValueError → JSON-RPC error response (has "error" key not "result")
    assert "error" in body or "error" in json.loads(
        body.get("result", {}).get("content", [{}])[0].get("text", "{}")
    )
