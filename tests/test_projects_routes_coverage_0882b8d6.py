"""Coverage tests for meridian/routes/projects.py — sprint item 0882b8d6.

Targets high-impact uncovered branches identified from the 76% baseline:
- generate_codebase_map: invalid JSON, non-dict body, GraphvizMissingError (503),
  Exception from render_map (500)
- get/patch project_settings: 404 paths
- set_project_ntfy: 404 for unknown project, email-only body, webhook URL passthrough
- get/test notification: 404 + "both absent" 400 path
- set_project_parent HTTP route (entire untested path: missing key 400, ValueError 400, 404)
- patch_agent_instructions: empty-string-to-None branch
- get_goal: 404 when project doesn't exist
- patch_goal_mode: 404 + 422 paths
- get_goal_mode: 404 path
- list/create/delete worktrees HTTP routes (untested HTTP surface)
- export PDF: 404 guard (the fpdf body tests are already in test_core.py)
- patch_project_organization: 404 and 422 paths
- post_decision_endpoint: 404 and 422 (empty text) paths
- get_project_ntfy: 404 path
- start_worker_session: 404 when no task
- get_project_runs/run: 404 paths
- search_project_all: empty query fast-return
- session_timeline endpoint: 404 path
- rewind endpoint: invalid token 403 + days<=0 422
"""
from __future__ import annotations

import importlib
import importlib.util
import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_project(client, name="cov-test-proj"):
    r = client.post("/projects", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()


# ===========================================================================
# generate_codebase_map — lines 239-271
# ===========================================================================


def test_codebase_map_invalid_json_body_returns_400(client):
    """generate_codebase_map: non-JSON body -> 400."""
    p = _make_project(client, "cov-cbmap-badjson")
    r = client.post(
        f"/projects/{p['id']}/codebase-map",
        content=b"not-valid-json",
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 400
    assert "invalid json" in r.json()["detail"].lower()


def test_codebase_map_non_dict_body_returns_400(client):
    """generate_codebase_map: body is a list (not a dict) -> 400."""
    p = _make_project(client, "cov-cbmap-list")
    r = client.post(
        f"/projects/{p['id']}/codebase-map",
        json=[1, 2, 3],
    )
    assert r.status_code == 400
    assert "object" in r.json()["detail"].lower()


def test_codebase_map_no_packages_returns_400(client):
    """generate_codebase_map: empty packages list -> 400 with 'index the repo' hint."""
    p = _make_project(client, "cov-cbmap-empty")
    r = client.post(
        f"/projects/{p['id']}/codebase-map",
        json={"packages": [], "edges": []},
    )
    assert r.status_code == 400
    assert "packages" in r.json()["detail"].lower()


def test_codebase_map_graphviz_missing_returns_503(client, monkeypatch):
    """generate_codebase_map: GraphvizMissingError from render_map -> 503.

    render_map is a sync function imported lazily inside the route body. We
    patch it on the codebase_map module so the route's ``from ..codebase_map
    import render_map`` picks up the stub.
    """
    from meridian.codebase_map import GraphvizMissingError
    import meridian.codebase_map as cbmap_mod

    def _fake_render(graph, out_path, *, hotspots_only=False, fmt="png"):
        raise GraphvizMissingError("dot not found; install graphviz")

    monkeypatch.setattr(cbmap_mod, "render_map", _fake_render)

    p = _make_project(client, "cov-cbmap-gviz")
    r = client.post(
        f"/projects/{p['id']}/codebase-map",
        json={"packages": ["meridian"], "edges": []},
    )
    assert r.status_code == 503
    body = r.json()
    assert body["error"] == "graphviz_missing"


def test_codebase_map_render_exception_returns_500(client, monkeypatch):
    """generate_codebase_map: unexpected exception from render_map -> 500."""
    import meridian.codebase_map as cbmap_mod

    def _explode(graph, out_path, *, hotspots_only=False, fmt="png"):
        raise RuntimeError("disk full")

    monkeypatch.setattr(cbmap_mod, "render_map", _explode)

    p = _make_project(client, "cov-cbmap-exc")
    r = client.post(
        f"/projects/{p['id']}/codebase-map",
        json={"packages": ["meridian"], "edges": []},
    )
    assert r.status_code == 500
    assert "map render failed" in r.json()["detail"]


# ===========================================================================
# get_project_settings — 404 path (line 226)
# ===========================================================================


def test_get_project_settings_404_unknown_project(client):
    """GET /projects/{id}/settings returns 404 for an unknown project."""
    r = client.get("/projects/no-such-project/settings")
    assert r.status_code == 404


# ===========================================================================
# patch_project_settings — 404 path (line 295-296)
# ===========================================================================


def test_patch_project_settings_404_unknown_project(client):
    """PATCH /projects/{id}/settings returns 404 for an unknown project."""
    r = client.patch(
        "/projects/no-such-project/settings",
        json={"max_pinned_decisions": 10},
    )
    assert r.status_code == 404


# ===========================================================================
# get_project_ntfy — 404 path
# ===========================================================================


def test_get_project_ntfy_404_unknown_project(client):
    """GET /projects/{id}/ntfy returns 404 for unknown project."""
    r = client.get("/projects/no-such-project/ntfy")
    assert r.status_code == 404


# ===========================================================================
# set_project_ntfy — various uncovered branches
# ===========================================================================


def test_set_project_ntfy_404_unknown_project(client):
    """PATCH /projects/{id}/ntfy returns 404 for unknown project."""
    r = client.patch(
        "/projects/no-such-project/ntfy",
        json={"notify_url": "https://ntfy.sh/test"},
    )
    assert r.status_code == 404


def test_set_project_ntfy_email_channel(client, monkeypatch):
    """PATCH with notify_email only saves the email without touching ntfy_url.

    Covers the 'if notify_email in body' branch plus the else branch for notify_url
    (no notify_url key -> reads existing value from DB).
    """
    import httpx

    class _FakeClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            pass
        async def post(self, url, **kw):
            class _R:
                status_code = 200
            return _R()

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _FakeClient())

    p = _make_project(client, "cov-ntfy-email")
    r = client.patch(
        f"/projects/{p['id']}/ntfy",
        json={"notify_email": "alerts@example.com"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["notify_email"] == "alerts@example.com"
    # ntfy_url unchanged (empty since never set)
    assert body["ntfy_url"] == ""


def test_set_project_ntfy_webhook_url_passthrough(client, monkeypatch):
    """PATCH with a non-ntfy webhook URL passes through verbatim (no suffix dedup)."""
    import httpx

    dispatched: list[str] = []

    class _FakeClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            pass
        async def post(self, url, **kw):
            dispatched.append(url)
            class _R:
                status_code = 200
            return _R()

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _FakeClient())

    p = _make_project(client, "cov-ntfy-webhook")
    webhook = "https://hooks.slack.com/services/abc/123"
    r = client.patch(
        f"/projects/{p['id']}/ntfy",
        json={"notify_url": webhook},
    )
    assert r.status_code == 200
    # webhook URLs pass through verbatim (no ntfy canonicalization)
    body = r.json()
    assert body["notify_url"] == webhook


def test_set_project_ntfy_clears_url(client, monkeypatch):
    """PATCH with empty string notify_url clears the stored value."""
    import httpx

    class _FakeClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            pass
        async def post(self, url, **kw):
            class _R:
                status_code = 200
            return _R()

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _FakeClient())

    p = _make_project(client, "cov-ntfy-clear")
    # First set a value
    client.patch(
        f"/projects/{p['id']}/ntfy",
        json={"notify_url": "https://ntfy.sh/cov-clear-topic"},
    )
    # Then clear it
    r = client.patch(
        f"/projects/{p['id']}/ntfy",
        json={"notify_url": ""},
    )
    assert r.status_code == 200
    assert r.json()["notify_url"] == ""


# ===========================================================================
# test_project_notification — 404 path
# ===========================================================================


def test_notify_test_endpoint_404_unknown_project(client):
    """POST /projects/{id}/notify/test returns 404 for unknown project."""
    r = client.post("/projects/no-such-project/notify/test")
    assert r.status_code == 404


# ===========================================================================
# set_project_parent HTTP route — POST /projects/{id}/parent (lines 511-549)
# The DB layer is tested in test_project_parent.py; this covers the HTTP surface.
# ===========================================================================


def test_set_project_parent_missing_key_returns_400(client):
    """POST /projects/{id}/parent with no parent_project_id key -> 400."""
    p = _make_project(client, "cov-parent-nokey")
    r = client.post(
        f"/projects/{p['id']}/parent",
        json={"something_else": "value"},
    )
    assert r.status_code == 400
    assert "parent_project_id" in r.json()["detail"]


def test_set_project_parent_404_unknown_project(client):
    """POST /projects/{id}/parent returns 404 when the project doesn't exist."""
    r = client.post(
        "/projects/no-such-project/parent",
        json={"parent_project_id": None},
    )
    assert r.status_code == 404


def test_set_project_parent_happy_path(client):
    """POST /projects/{id}/parent attaches a child to a parent and returns the updated project."""
    parent = _make_project(client, "cov-parent-parent")
    child = _make_project(client, "cov-parent-child")

    r = client.post(
        f"/projects/{child['id']}/parent",
        json={"parent_project_id": parent["id"]},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["parent_project_id"] == parent["id"]


def test_set_project_parent_detach(client):
    """POST /projects/{id}/parent with null detaches the child to top-level."""
    parent = _make_project(client, "cov-parent-detach-p")
    child = _make_project(client, "cov-parent-detach-c")

    # First attach
    client.post(
        f"/projects/{child['id']}/parent",
        json={"parent_project_id": parent["id"]},
    )
    # Then detach
    r = client.post(
        f"/projects/{child['id']}/parent",
        json={"parent_project_id": None},
    )
    assert r.status_code == 200
    assert r.json().get("parent_project_id") in (None, "")


def test_set_project_parent_self_parent_returns_400(client):
    """POST /projects/{id}/parent rejects self-referential parent -> 400 (ValueError)."""
    p = _make_project(client, "cov-parent-self")
    r = client.post(
        f"/projects/{p['id']}/parent",
        json={"parent_project_id": p["id"]},
    )
    assert r.status_code == 400


# ===========================================================================
# patch_agent_instructions — empty string -> None branch (line 475-476)
# ===========================================================================


def test_patch_agent_instructions_empty_string_resets_to_none(client):
    """PATCH agent-instructions with empty string clears it (not reset to default)."""
    p = _make_project(client, "cov-agent-instr-clear")
    r = client.patch(
        f"/projects/{p['id']}/agent-instructions",
        json={"agent_instructions": ""},
    )
    assert r.status_code == 200
    body = r.json()
    # empty string -> stored as None; the route returns the DB result
    assert body.get("agent_instructions") is None or body.get("agent_instructions") == ""


def test_patch_agent_instructions_null_resets_to_default(client):
    """PATCH agent-instructions with null resets to DEFAULT_AGENT_INSTRUCTIONS."""
    from meridian.agent_defaults import DEFAULT_AGENT_INSTRUCTIONS

    p = _make_project(client, "cov-agent-instr-reset")
    r = client.patch(
        f"/projects/{p['id']}/agent-instructions",
        json={"agent_instructions": None},
    )
    assert r.status_code == 200
    body = r.json()
    assert body.get("agent_instructions") == DEFAULT_AGENT_INSTRUCTIONS


# ===========================================================================
# get_goal — 404 when project doesn't exist (line 592)
# ===========================================================================


def test_get_goal_404_unknown_project(client):
    """GET /projects/{id}/goal returns 404 for an unknown project."""
    r = client.get("/projects/no-such-project/goal")
    assert r.status_code == 404


# ===========================================================================
# patch_goal_mode — 404 + 422 paths
# ===========================================================================


def test_patch_goal_mode_404_unknown_project(client):
    """PATCH /projects/{id}/goal-mode returns 404 for unknown project."""
    r = client.patch(
        "/projects/no-such-project/goal-mode",
        json={"mode": "auto"},
    )
    assert r.status_code == 404


def test_patch_goal_mode_invalid_mode_returns_422(client):
    """PATCH /projects/{id}/goal-mode with invalid mode returns 422."""
    p = _make_project(client, "cov-goalmode-422")
    r = client.patch(
        f"/projects/{p['id']}/goal-mode",
        json={"mode": "not-a-valid-mode"},
    )
    assert r.status_code == 422


# ===========================================================================
# get_goal_mode — 404 path
# ===========================================================================


def test_get_goal_mode_404_unknown_project(client):
    """GET /projects/{id}/goal-mode returns 404 for unknown project."""
    r = client.get("/projects/no-such-project/goal-mode")
    assert r.status_code == 404


# ===========================================================================
# patch_project_organization — 404 + 422 paths
# ===========================================================================


def test_patch_organization_404_unknown_project(client):
    """PATCH /projects/{id}/organization returns 404 for unknown project."""
    r = client.patch(
        "/projects/no-such-project/organization",
        json={"status": "active"},
    )
    assert r.status_code == 404


def test_patch_organization_invalid_status_returns_422(client):
    """PATCH /projects/{id}/organization with invalid status returns 422."""
    p = _make_project(client, "cov-org-422")
    r = client.patch(
        f"/projects/{p['id']}/organization",
        json={"status": "not-a-real-status"},
    )
    assert r.status_code == 422


def test_patch_organization_happy_path(client):
    """PATCH /projects/{id}/organization changes status and priority."""
    p = _make_project(client, "cov-org-happy")
    r = client.patch(
        f"/projects/{p['id']}/organization",
        json={"status": "parked", "priority": "P1"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "parked"
    assert body["priority"] == "P1"


# ===========================================================================
# post_decision_endpoint — 404 + 422 (empty text) paths
# ===========================================================================


def test_post_decision_404_unknown_project(client):
    """POST /projects/{id}/decisions returns 404 for unknown project."""
    r = client.post(
        "/projects/no-such-project/decisions",
        json={"text": "some decision"},
    )
    assert r.status_code == 404


def test_post_decision_empty_text_returns_422(client):
    """POST /projects/{id}/decisions with empty text returns 422."""
    p = _make_project(client, "cov-dec-422")
    r = client.post(
        f"/projects/{p['id']}/decisions",
        json={"text": ""},
    )
    assert r.status_code == 422
    assert "text is required" in r.json()["detail"]


def test_post_decision_missing_text_returns_422(client):
    """POST /projects/{id}/decisions with missing text key returns 422."""
    p = _make_project(client, "cov-dec-missing")
    r = client.post(
        f"/projects/{p['id']}/decisions",
        json={},
    )
    assert r.status_code == 422


# ===========================================================================
# Worktree HTTP routes (list, create, delete) — lines 981-1024
# ===========================================================================


def test_list_worktrees_404_unknown_project(client):
    """GET /projects/{id}/worktrees returns 404 for unknown project."""
    r = client.get("/projects/no-such-project/worktrees")
    assert r.status_code == 404


def test_list_worktrees_returns_empty_for_new_project(client):
    """GET /projects/{id}/worktrees returns [] for a fresh project."""
    p = _make_project(client, "cov-wt-list-empty")
    r = client.get(f"/projects/{p['id']}/worktrees")
    assert r.status_code == 200
    assert r.json() == []


def test_create_and_list_worktree(client):
    """POST /projects/{id}/worktrees registers a worktree; GET shows it."""
    from meridian import db as db_module
    import asyncio

    p = _make_project(client, "cov-wt-create")
    # Need a valid session_id; create one via the sessions endpoint.
    s = client.post(
        "/sessions/register",
        json={"project_id": p["id"], "name": "wt-session"},
    ).json()

    body = {
        "session_id": s["id"],
        "branch": "worktree/cov-test",
        "path": "../repo-cov-test",
    }
    r = client.post(f"/projects/{p['id']}/worktrees", json=body)
    assert r.status_code == 201
    wt = r.json()
    assert wt["branch"] == "worktree/cov-test"
    # eb2e44f8 — no base_sha/base_branch supplied above, so no manifest gets
    # created (backward compatible with pre-eb2e44f8 callers).
    assert "manifest" not in wt

    r2 = client.get(f"/projects/{p['id']}/worktrees")
    assert r2.status_code == 200
    ids = [w["id"] for w in r2.json()]
    assert wt["id"] in ids


def test_create_worktree_with_base_manifest_fields_persists_manifest(client):
    """eb2e44f8 — POST /projects/{id}/worktrees also supplying base_sha +
    base_branch (+ optional pid/repo_identity) persists an immutable base
    manifest alongside the worktree registration, returned under the
    'manifest' key."""
    p = _make_project(client, "cov-wt-manifest-create")
    s = client.post(
        "/sessions/register",
        json={"project_id": p["id"], "name": "wt-manifest-session"},
    ).json()

    body = {
        "session_id": s["id"],
        "branch": "worktree/cov-manifest-test",
        "path": "../repo-cov-manifest-test",
        "pid": 424242,
        "base_sha": "d" * 40,
        "base_branch": "dev",
        "repo_identity": "cov-test-repo",
    }
    r = client.post(f"/projects/{p['id']}/worktrees", json=body)
    assert r.status_code == 201
    wt = r.json()
    assert wt["pid"] == 424242
    assert "manifest" in wt
    manifest = wt["manifest"]
    assert manifest["base_sha"] == "d" * 40
    assert manifest["base_branch"] == "dev"
    assert manifest["repo_identity"] == "cov-test-repo"
    assert manifest["worktree_id"] == wt["id"]


def test_create_worktree_404_unknown_project(client):
    """POST /projects/{id}/worktrees returns 404 for unknown project."""
    r = client.post(
        "/projects/no-such-project/worktrees",
        json={"session_id": "s1", "branch": "b", "path": "p"},
    )
    assert r.status_code == 404


def test_delete_worktree_removes_it(client):
    """DELETE /projects/{id}/worktrees/{wt_id} marks the worktree removed."""
    p = _make_project(client, "cov-wt-delete")
    s = client.post(
        "/sessions/register",
        json={"project_id": p["id"], "name": "wt-del-sess"},
    ).json()

    wt = client.post(
        f"/projects/{p['id']}/worktrees",
        json={"session_id": s["id"], "branch": "worktree/del-test", "path": "../del"},
    ).json()

    r = client.delete(f"/projects/{p['id']}/worktrees/{wt['id']}")
    assert r.status_code == 204

    # Should no longer be in active list
    active = client.get(f"/projects/{p['id']}/worktrees").json()
    assert not any(w["id"] == wt["id"] for w in active)


def test_delete_worktree_404_not_found(client):
    """DELETE /projects/{id}/worktrees/{wt_id} returns 404 for unknown worktree."""
    p = _make_project(client, "cov-wt-del-404")
    r = client.delete(f"/projects/{p['id']}/worktrees/no-such-worktree")
    assert r.status_code == 404


# ===========================================================================
# export PDF — 404 guard
# The PDF endpoint imports fpdf at the TOP of the function body (before the
# project-existence check), so this test only runs when fpdf2 is installed.
# ===========================================================================

_fpdf_available = pytest.mark.skipif(
    importlib.util.find_spec("fpdf") is None,
    reason="fpdf2 not installed (dev-only dep)",
)


@_fpdf_available
def test_export_pdf_404_for_unknown_project(client):
    """GET /projects/{id}/export/pdf returns 404 for unknown project (fpdf required)."""
    r = client.get("/projects/no-such-project/export/pdf")
    assert r.status_code == 404


# ===========================================================================
# session_timeline endpoint — 404 path
# ===========================================================================


def test_session_timeline_404_unknown_project(client):
    """GET /projects/{id}/session-timeline returns 404 for unknown project."""
    r = client.get("/projects/no-such-project/session-timeline")
    assert r.status_code == 404


def test_session_timeline_returns_data(client):
    """GET /projects/{id}/session-timeline returns the timeline structure."""
    p = _make_project(client, "cov-st-ok")
    r = client.get(f"/projects/{p['id']}/session-timeline")
    assert r.status_code == 200


# ===========================================================================
# rewind — invalid token (403) + days <= 0 (422)
# ===========================================================================


def test_rewind_invalid_token_returns_403(client):
    """GET /rewind with a wrong token returns 403."""
    p = _make_project(client, "cov-rewind-badtok")
    r = client.get(f"/projects/{p['id']}/rewind?days=7&token=wrong-token")
    assert r.status_code == 403
    assert "invalid rewind token" in r.json()["detail"]


def test_rewind_days_zero_returns_422(client):
    """GET /rewind?days=0 returns 422 (days must be positive)."""
    p = _make_project(client, "cov-rewind-days0")
    r = client.get(f"/projects/{p['id']}/rewind?days=0")
    assert r.status_code == 422
    assert "positive" in r.json()["detail"]


def test_rewind_days_negative_returns_422(client):
    """GET /rewind?days=-5 returns 422."""
    p = _make_project(client, "cov-rewind-neg")
    r = client.get(f"/projects/{p['id']}/rewind?days=-5")
    assert r.status_code == 422


# ===========================================================================
# search_project_all — empty query fast-return (line 1246)
# ===========================================================================


def test_search_project_all_empty_query(client):
    """GET /projects/{id}/search with empty q returns empty result immediately."""
    p = _make_project(client, "cov-search-empty")
    r = client.get(f"/projects/{p['id']}/search?q=")
    assert r.status_code == 200
    body = r.json()
    assert body["tasks"] == []
    assert body["notes"] == []
    assert body["total"] == 0


def test_search_project_all_404_unknown_project(client):
    """GET /projects/{id}/search returns 404 for unknown project."""
    r = client.get("/projects/no-such-project/search?q=hello")
    assert r.status_code == 404


# ===========================================================================
# get_project_runs — 404 path
# ===========================================================================


def test_get_project_runs_404_unknown_project(client):
    """GET /projects/{id}/runs returns 404 for unknown project."""
    r = client.get("/projects/no-such-project/runs")
    assert r.status_code == 404


def test_get_project_runs_empty_for_new_project(client):
    """GET /projects/{id}/runs returns empty list for a project with no runs."""
    p = _make_project(client, "cov-runs-empty")
    r = client.get(f"/projects/{p['id']}/runs")
    assert r.status_code == 200
    assert r.json() == []


# ===========================================================================
# get_project_run — 404 when run not found or wrong project
# ===========================================================================


def test_get_project_run_404_not_found(client):
    """GET /projects/{id}/runs/{run_id} returns 404 for unknown run."""
    p = _make_project(client, "cov-run-404")
    r = client.get(f"/projects/{p['id']}/runs/no-such-run")
    assert r.status_code == 404


# ===========================================================================
# start_worker_session HTTP route — 404 when no claimable task
# ===========================================================================


def test_start_worker_session_404_no_task(client):
    """POST /projects/{id}/start-worker-session returns 404 when no task is available."""
    p = _make_project(client, "cov-sws-notask")
    r = client.post(f"/projects/{p['id']}/start-worker-session", json={})
    assert r.status_code == 404


# ===========================================================================
# set_goal — 403 when goal already locked + input size validation
# ===========================================================================


def test_set_goal_404_unknown_project(client):
    """POST /projects/{id}/goal returns 404 for unknown project."""
    r = client.post("/projects/no-such-project/goal", json={"content": "hello"})
    assert r.status_code == 404


# ===========================================================================
# set_north_star / set_sprint — 404 paths
# ===========================================================================


def test_set_north_star_404_unknown_project(client):
    """POST /projects/{id}/goal/north-star returns 404 for unknown project.

    SetNorthStarRequest requires north_star + human_id (both non-empty str).
    Without them FastAPI returns 422 before the route body runs.
    """
    r = client.post(
        "/projects/no-such-project/goal/north-star",
        json={"north_star": "Build something great", "human_id": "adam"},
    )
    assert r.status_code == 404


def test_set_sprint_404_unknown_project(client):
    """POST /projects/{id}/goal/sprint returns 404 for unknown project.

    SetSprintRequest requires sprint (non-empty str).
    """
    r = client.post(
        "/projects/no-such-project/goal/sprint",
        json={"sprint": "week 1"},
    )
    assert r.status_code == 404


# ===========================================================================
# rewind-token — 404 path
# ===========================================================================


def test_rewind_token_404_unknown_project(client):
    """POST /projects/{id}/rewind-token returns 404 for unknown project."""
    r = client.post("/projects/no-such-project/rewind-token")
    assert r.status_code == 404


# ===========================================================================
# goal_history — 404 path
# ===========================================================================


def test_goal_history_endpoint_404_unknown_project(client):
    """GET /projects/{id}/goal-history returns 404 for unknown project."""
    r = client.get("/projects/no-such-project/goal-history")
    assert r.status_code == 404


# ===========================================================================
# project stats — 404 path
# ===========================================================================


def test_get_project_stats_404_unknown_project(client):
    """GET /projects/{id}/stats returns 404 for unknown project."""
    r = client.get("/projects/no-such-project/stats")
    assert r.status_code == 404


def test_get_project_stats_happy_path(client):
    """GET /projects/{id}/stats returns a dict for a real project."""
    p = _make_project(client, "cov-stats-ok")
    r = client.get(f"/projects/{p['id']}/stats")
    assert r.status_code == 200
    # should have at least some keys
    assert isinstance(r.json(), dict)


# ===========================================================================
# webhook-token — minting token and 404-if-project-not-found
# ===========================================================================


def test_webhook_token_404_when_project_not_found(client):
    """GET /projects/{id}/webhook-token returns 404 when project doesn't exist.

    db.ensure_project_token returns None for unknown project -> route raises 404.
    """
    r = client.get("/projects/no-such-project/webhook-token")
    assert r.status_code == 404


# ===========================================================================
# events webhook — missing description returns 400
# ===========================================================================


def test_events_webhook_missing_description_returns_400(client):
    """POST /projects/{id}/events with valid token but no description -> 400."""
    p = _make_project(client, "cov-events-nodesc")
    tok = client.get(f"/projects/{p['id']}/webhook-token").json()["token"]
    r = client.post(
        f"/projects/{p['id']}/events",
        json={"session_name": "test", "type": "task_completed"},
        headers={"X-Meridian-Token": tok},
    )
    assert r.status_code == 400
    assert "description" in r.json()["detail"].lower()


def test_events_webhook_hitl_event_type(client):
    """POST /projects/{id}/events with type=hitl_request routes to request_hitl."""
    p = _make_project(client, "cov-events-hitl")
    tok = client.get(f"/projects/{p['id']}/webhook-token").json()["token"]
    r = client.post(
        f"/projects/{p['id']}/events",
        json={
            "type": "hitl_request",
            "session_name": "cov-agent",
            "description": "Awaiting human review of the migration",
            "urgency": "normal",
        },
        headers={"X-Meridian-Token": tok},
    )
    assert r.status_code == 201
    # hitl_request returns the HITL row directly (from request_hitl)
    body = r.json()
    assert "description" in body or "id" in body


# ===========================================================================
# get_project_by_name — 404 path (already in test_core.py but verifying we
# didn't regress the inline goal_summary shape for a project with no goal)
# ===========================================================================


def test_get_project_by_name_no_goal_returns_null_summary(client):
    """GET /projects/by-name/{name} for a project with no goal returns null goal_summary."""
    _make_project(client, "cov-byname-nogoal")
    r = client.get("/projects/by-name/cov-byname-nogoal")
    assert r.status_code == 200
    body = r.json()
    assert body["goal_version"] is None
    assert body["goal_summary"] is None


# ===========================================================================
# canonicalize_notify_target — unit-level coverage of the helper
# ===========================================================================


def test_canonicalize_notify_target_ntfy_variants():
    """_canonicalize_notify_target handles all ntfy.sh URL forms."""
    from meridian.routes.projects import _canonicalize_notify_target as canon

    assert canon("") is None
    assert canon(None) is None
    assert canon("   ") is None
    assert canon("https://ntfy.sh/my-topic") == "my-topic"
    assert canon("https://ntfy.sh/my-topic/") == "my-topic"
    assert canon("http://ntfy.sh/topic") == "topic"
    assert canon("ntfy.sh/topic") == "topic"
    assert canon("bare-topic") == "bare-topic"
    assert canon("you@example.com") == "you@example.com"
    assert canon("https://hooks.slack.com/services/abc") == "https://hooks.slack.com/services/abc"


def test_canonicalize_notify_target_bare_slash_returns_none():
    """_canonicalize_notify_target returns None for ntfy.sh/ with no topic."""
    from meridian.routes.projects import _canonicalize_notify_target as canon
    # ntfy.sh/ with empty topic -> None
    result = canon("https://ntfy.sh/")
    assert result is None
