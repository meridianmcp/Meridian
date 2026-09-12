"""Tests for sprint item 70061103 (W1-P) — the three new read-only HTTP GET
endpoints backing the dashboard's Experiments vtab:

    GET /projects/{project_id}/experiments
    GET /projects/{project_id}/experiments/{experiment_id}/runs
    GET /projects/{project_id}/experiments/{experiment_id}/events

These are thin wrappers around meridian.db.experiments (W1-M, 3f6b8715),
already covered at the DB layer by tests/test_experiments.py. This file
covers the NEW HTTP surface only: project/experiment existence -> 404
shaping, response envelopes, and the run_id/status query-param filters the
dashboard's list -> run drill-down -> event timeline flow depends on.
"""
from __future__ import annotations

import asyncio

import meridian.server  # noqa: F401 — ensure the app module is loaded
from meridian.db import experiments as exp_db


def _seed_session(client, name: str):
    """Create a project + session via the real HTTP endpoints, matching
    tests/test_core.py's test_queue_session_http_roundtrip pattern."""
    project_id = client.post("/projects", json={"name": name}).json()["id"]
    session_id = client.post(
        "/sessions/register", json={"project_id": project_id, "name": f"{name}-session"},
    ).json()["id"]
    return project_id, session_id


def _seed_experiment(client, project_id: str, session_id: str, name: str, hypothesis=None):
    db = client.app.state.db
    return asyncio.run(
        exp_db.create_experiment(db, project_id, session_id, name=name, hypothesis=hypothesis)
    )


def _seed_run(client, project_id: str, session_id: str, experiment_id: str, trial_label=None):
    db = client.app.state.db
    return asyncio.run(
        exp_db.start_experiment_run(
            db, project_id, session_id, experiment_id=experiment_id, trial_label=trial_label,
        )
    )


def _complete_run(client, project_id: str, session_id: str, run_id: str, outcome_summary: str, disposition: str):
    db = client.app.state.db
    return asyncio.run(
        exp_db.complete_experiment_run(
            db, project_id, session_id, run_id=run_id,
            outcome_summary=outcome_summary, disposition=disposition,
        )
    )


def _record_event(client, project_id: str, session_id: str, experiment_id: str, **kwargs):
    db = client.app.state.db
    return asyncio.run(
        exp_db.record_experiment_event(db, project_id, session_id, experiment_id=experiment_id, **kwargs)
    )


# ---------------------------------------------------------------------------
# GET /projects/{project_id}/experiments
# ---------------------------------------------------------------------------


def test_list_experiments_empty_state(client):
    """A fresh project with no experiments returns an empty, well-shaped
    envelope — the dashboard renders its 'No experiments tracked yet ...'
    empty state off exactly this response."""
    project_id, _session_id = _seed_session(client, "exp-api-empty")
    r = client.get(f"/projects/{project_id}/experiments")
    assert r.status_code == 200
    body = r.json()
    assert body == {"experiments": [], "count": 0}


def test_list_experiments_404_for_missing_project(client):
    r = client.get("/projects/does-not-exist/experiments")
    assert r.status_code == 404


def test_list_experiments_populated(client):
    """list_experiments orders by (created_at DESC, id DESC) — created_at has
    only second-level granularity in this test env, so two experiments
    created back-to-back can legitimately tie and then sort by their
    (effectively random) UUID id rather than creation order. Assert both
    landed with the right fields rather than depending on which is first."""
    project_id, session_id = _seed_session(client, "exp-api-populated")
    first = _seed_experiment(client, project_id, session_id, "First experiment", hypothesis="H1")
    second = _seed_experiment(client, project_id, session_id, "Second experiment", hypothesis="H2")

    r = client.get(f"/projects/{project_id}/experiments")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 2
    by_id = {e["id"]: e for e in body["experiments"]}
    assert set(by_id) == {first["id"], second["id"]}
    assert by_id[first["id"]]["name"] == "First experiment"
    assert by_id[first["id"]]["hypothesis"] == "H1"
    assert by_id[second["id"]]["name"] == "Second experiment"
    assert by_id[second["id"]]["hypothesis"] == "H2"
    assert by_id[second["id"]]["status"] == "active"


def test_list_experiments_status_filter(client):
    project_id, session_id = _seed_session(client, "exp-api-status-filter")
    _seed_experiment(client, project_id, session_id, "Active one")

    r_active = client.get(f"/projects/{project_id}/experiments?status=active")
    assert r_active.status_code == 200
    assert r_active.json()["count"] == 1

    r_archived = client.get(f"/projects/{project_id}/experiments?status=archived")
    assert r_archived.status_code == 200
    assert r_archived.json() == {"experiments": [], "count": 0}

    r_bad = client.get(f"/projects/{project_id}/experiments?status=bogus")
    assert r_bad.status_code == 400


# ---------------------------------------------------------------------------
# GET /projects/{project_id}/experiments/{experiment_id}/runs
# ---------------------------------------------------------------------------


def test_list_experiment_runs_empty_state(client):
    project_id, session_id = _seed_session(client, "exp-api-runs-empty")
    experiment = _seed_experiment(client, project_id, session_id, "No runs yet")

    r = client.get(f"/projects/{project_id}/experiments/{experiment['id']}/runs")
    assert r.status_code == 200
    assert r.json() == {"runs": [], "count": 0}


def test_list_experiment_runs_404_for_missing_experiment(client):
    project_id, _session_id = _seed_session(client, "exp-api-runs-404")
    r = client.get(f"/projects/{project_id}/experiments/does-not-exist/runs")
    assert r.status_code == 404


def test_list_experiment_runs_404_for_missing_project(client):
    r = client.get("/projects/does-not-exist/experiments/whatever/runs")
    assert r.status_code == 404


def test_list_experiment_runs_populated_and_status_filter(client):
    """Covers run drill-down: an experiment with an active run and a
    completed (kept) run, plus the dashboard's ?status= filter."""
    project_id, session_id = _seed_session(client, "exp-api-runs-populated")
    experiment = _seed_experiment(client, project_id, session_id, "Two trials")

    active_run = _seed_run(client, project_id, session_id, experiment["id"], trial_label="trial-active")
    done_run = _seed_run(client, project_id, session_id, experiment["id"], trial_label="trial-done")
    _complete_run(client, project_id, session_id, done_run["id"], "Confirmed the hypothesis.", "keep")

    r_all = client.get(f"/projects/{project_id}/experiments/{experiment['id']}/runs")
    assert r_all.status_code == 200
    body_all = r_all.json()
    assert body_all["count"] == 2
    statuses = {run["id"]: run["status"] for run in body_all["runs"]}
    assert statuses[active_run["id"]] == "active"
    assert statuses[done_run["id"]] == "completed"

    r_active = client.get(f"/projects/{project_id}/experiments/{experiment['id']}/runs?status=active")
    assert r_active.status_code == 200
    body_active = r_active.json()
    assert body_active["count"] == 1
    assert body_active["runs"][0]["id"] == active_run["id"]

    r_completed = client.get(f"/projects/{project_id}/experiments/{experiment['id']}/runs?status=completed")
    assert r_completed.status_code == 200
    body_completed = r_completed.json()
    assert body_completed["count"] == 1
    assert body_completed["runs"][0]["id"] == done_run["id"]
    assert body_completed["runs"][0]["disposition"] == "keep"
    assert body_completed["runs"][0]["outcome_summary"] == "Confirmed the hypothesis."


# ---------------------------------------------------------------------------
# GET /projects/{project_id}/experiments/{experiment_id}/events
# ---------------------------------------------------------------------------


def test_get_experiment_events_empty_state(client):
    project_id, session_id = _seed_session(client, "exp-api-events-empty")
    experiment = _seed_experiment(client, project_id, session_id, "No events yet")

    r = client.get(f"/projects/{project_id}/experiments/{experiment['id']}/events")
    assert r.status_code == 200
    assert r.json() == {"events": [], "count": 0}


def test_get_experiment_events_404_for_missing_experiment(client):
    project_id, _session_id = _seed_session(client, "exp-api-events-404")
    r = client.get(f"/projects/{project_id}/experiments/does-not-exist/events")
    assert r.status_code == 404


def test_get_experiment_events_404_for_missing_project(client):
    r = client.get("/projects/does-not-exist/experiments/whatever/events")
    assert r.status_code == 404


def test_get_experiment_events_timeline_and_run_id_filter(client):
    """The dashboard's event-timeline drilldown: dead_end/pivot/breakthrough/
    note/milestone chips, oldest first, optionally scoped to one run via
    ?run_id=. Exercises both the auto-written terminal-transition events
    (complete_experiment_run's HARD INVARIANT) and a manual enrichment event."""
    project_id, session_id = _seed_session(client, "exp-api-events-timeline")
    experiment = _seed_experiment(client, project_id, session_id, "Timeline experiment")

    run_a = _seed_run(client, project_id, session_id, experiment["id"], trial_label="run-a")
    run_b = _seed_run(client, project_id, session_id, experiment["id"], trial_label="run-b")

    # Manual 'pivot' note on run_a before it completes.
    _record_event(
        client, project_id, session_id, experiment["id"],
        run_id=run_a["id"], event_type="pivot", label="manual", body="Trying a different approach.",
    )
    # run_a completes as a dead end -> auto-writes a 'dead_end' event.
    _complete_run(client, project_id, session_id, run_a["id"], "This was a dead end.", "discard")
    # run_b completes cleanly -> auto-writes a 'milestone' event.
    _complete_run(client, project_id, session_id, run_b["id"], "Confirmed the hypothesis.", "keep")

    r_all = client.get(f"/projects/{project_id}/experiments/{experiment['id']}/events")
    assert r_all.status_code == 200
    body_all = r_all.json()
    assert body_all["count"] == 3
    # get_experiment_events orders by (created_at ASC, id ASC) — created_at has
    # only second-level granularity in this test env, so events minted within
    # the same wall-clock second can legitimately tie and then sort by their
    # (effectively random) UUID id rather than insertion order. Assert the
    # right *set* of events landed rather than an exact sequence.
    event_types = sorted(e["event_type"] for e in body_all["events"])
    assert event_types == ["dead_end", "milestone", "pivot"]

    r_run_a = client.get(
        f"/projects/{project_id}/experiments/{experiment['id']}/events?run_id={run_a['id']}"
    )
    assert r_run_a.status_code == 200
    body_run_a = r_run_a.json()
    assert body_run_a["count"] == 2
    assert {e["event_type"] for e in body_run_a["events"]} == {"pivot", "dead_end"}
    assert all(e["run_id"] == run_a["id"] for e in body_run_a["events"])

    r_run_b = client.get(
        f"/projects/{project_id}/experiments/{experiment['id']}/events?run_id={run_b['id']}"
    )
    assert r_run_b.status_code == 200
    body_run_b = r_run_b.json()
    assert body_run_b["count"] == 1
    assert body_run_b["events"][0]["event_type"] == "milestone"
    assert body_run_b["events"][0]["body"] == "Confirmed the hypothesis."
