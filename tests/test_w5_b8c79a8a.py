"""Regression tests for b8c79a8a — Timeline showed "no activity yet" despite
real ``log_task`` activity.

Root cause: the ``GET /projects/{id}/timeline`` endpoint narrowed its ``tasks``
array to ``status in ("done", "failed")``. ``get_timeline`` only reads
``task_log`` (never session-lifecycle events), so that filter dropped genuine
logged activity — the same activity the standup digest and the heatmap
``daily_counts`` still counted. When a project's recent work was logged with a
non-terminal status (``in_progress``/``pending``), the endpoint returned an
empty ``tasks`` list, and the frontend's ``!tasks.length && !goal_events.length``
gate rendered "no activity yet" even though ``daily_counts`` was populated.

The fix returns every logged task from the endpoint. These tests lock in that
the timeline data path surfaces seeded task activity regardless of status.

Backend-only, unit-level: the ``db`` fixture drives the query directly and the
``client`` fixture (in-memory SQLite TestClient) drives the HTTP route — no
mocks, no network.
"""

from __future__ import annotations

import pytest

from meridian import db as db_module


# ---------------------------------------------------------------------------
# db-level: get_timeline is the source of truth the endpoint must not discard.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_timeline_returns_all_logged_statuses(db):
    """The db-level query returns every task_log row for the project — a
    session that only logged non-terminal progress still has timeline tasks."""
    p = await db_module.create_project(db, "meridian-build")
    s = await db_module.register_session(db, p["id"], "build-sess", human_id="adam")
    await db_module.log_task(db, s["id"], p["id"], "kicked off", "in_progress")
    await db_module.log_task(db, s["id"], p["id"], "still going", "pending")

    timeline = await db_module.get_timeline(db, p["id"])
    descriptions = {t["description"] for t in timeline["tasks"]}
    assert descriptions == {"kicked off", "still going"}
    # And the heatmap source counts them (kept consistent with the tasks list).
    assert timeline["daily_counts"][0]["count"] == 2


# ---------------------------------------------------------------------------
# endpoint-level: the /timeline route must not filter real activity back out.
# This is the exact regression — activity exists but the endpoint returned [].
# ---------------------------------------------------------------------------


def _seed(client, *, tasks):
    """Create a project + session and log ``tasks`` (list of (desc, status)).
    Returns the project id."""
    project = client.post("/projects", json={"name": "meridian-build"}).json()
    sess = client.post(
        "/sessions/register",
        json={"project_id": project["id"], "name": "build-sess", "human_id": "adam"},
    ).json()
    for desc, status in tasks:
        r = client.post(
            "/tasks",
            json={
                "session_id": sess["id"],
                "project_id": project["id"],
                "description": desc,
                "status": status,
            },
        )
        assert r.status_code < 400, r.text
    return project["id"]


def test_timeline_endpoint_surfaces_non_terminal_activity(client):
    """b8c79a8a regression: non-done/failed tasks must reach the endpoint
    payload so the frontend does not render "no activity yet"."""
    pid = _seed(
        client,
        tasks=[("in-flight work", "in_progress"), ("queued work", "pending")],
    )

    r = client.get(f"/projects/{pid}/timeline")
    assert r.status_code == 200
    body = r.json()

    assert isinstance(body["tasks"], list)
    # The bug returned []; the fix returns both logged rows.
    assert len(body["tasks"]) == 2
    descriptions = {t["description"] for t in body["tasks"]}
    assert descriptions == {"in-flight work", "queued work"}
    # daily_counts (heatmap) and tasks now agree — no split-brain empty gate.
    assert body["daily_counts"][0]["count"] == 2


def test_timeline_endpoint_still_includes_done_and_failed(client):
    """The broadened filter is additive: terminal statuses still appear
    alongside the newly-surfaced non-terminal ones."""
    pid = _seed(
        client,
        tasks=[
            ("shipped it", "done"),
            ("broke it", "failed"),
            ("working on it", "in_progress"),
        ],
    )

    r = client.get(f"/projects/{pid}/timeline")
    assert r.status_code == 200
    by_status = {t["description"]: t["status"] for t in r.json()["tasks"]}
    assert by_status == {
        "shipped it": "done",
        "broke it": "failed",
        "working on it": "in_progress",
    }


def test_timeline_endpoint_empty_project_stays_empty(client):
    """A project with no logged tasks still reports no activity — the empty
    state is legitimate; the bug was empty *despite* activity."""
    project = client.post("/projects", json={"name": "quiet"}).json()
    r = client.get(f"/projects/{project['id']}/timeline")
    assert r.status_code == 200
    body = r.json()
    assert body["tasks"] == []
    assert body["daily_counts"] == []
