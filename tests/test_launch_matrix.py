"""Tests for build_launch_matrix (meridian/routes/tunnel.py) — ba31dedf.

build_launch_matrix joins the existing tenant/slot diagnostic layer
(build_tunnel_diagnostics) with Meridian PROJECT registration. These tests
exercise the pure join directly against the per-process socket/health
registries (mirroring tests/test_tunnel_diagnostics.py's pattern) — no real
WebSocket/network/DB touched.
"""
from __future__ import annotations

import json

import pytest

from meridian.routes import tunnel as tn

_TENANT = {"id": "tenant-ba31dedf", "plan": "pro"}


@pytest.fixture(autouse=True)
def _clean_diag_state():
    """Reset per-process tunnel registries so tests never leak state (mirrors
    test_tunnel_diagnostics.py's fixture — build_launch_matrix calls straight
    through to build_tunnel_diagnostics, which reads these same globals)."""
    def _reset():
        for d in (
            tn._tunnel_sockets, tn._tunnel_code_sockets, tn._tunnel_extract_sockets,
            tn._tunnel_ppt_sockets, tn._tunnel_word_sockets, tn._tunnel_dc_sockets,
            tn._tunnel_docs_sockets, tn._tunnel_zotero_sockets,
            tn._tunnel_outputs_sockets, tn._tunnel_debug_sockets,
            tn._tunnel_tool_routes, tn._slot_health, tn._slot_status_detail,
            tn._slot_unhealthy_since, tn._tools_list_changed_pending,
            tn._tenant_owner_instance,
        ):
            d.clear()
    _reset()
    yield
    _reset()


def _project(id_, name="proj", repo_path=None, executor_config=None):
    if executor_config is None:
        executor_config = {"repo_path": repo_path} if repo_path is not None else {}
    return {"id": id_, "name": name, "executor_config": executor_config}


# ---------------------------------------------------------------------------
# No tenant — still returns a row per project, never crashes
# ---------------------------------------------------------------------------

def test_no_tenant_still_returns_one_row_per_project():
    result = tn.build_launch_matrix(None, None, [_project("p1", repo_path="/opt/p1")])
    assert result["tenant_id"] is None
    assert len(result["rows"]) == 1
    row = result["rows"][0]
    assert row["project_id"] == "p1"
    assert row["hosted_route"] is None
    assert row["active_invocable"] is False


def test_no_projects_returns_empty_rows_but_valid_shape():
    result = tn.build_launch_matrix(_TENANT, None, [])
    assert result["rows"] == []
    assert "config_digest" in result
    assert "health_timestamp" in result


# ---------------------------------------------------------------------------
# Healthy join: project + live, healthy slot
# ---------------------------------------------------------------------------

def test_healthy_slot_joins_with_ok_scoped_project(tmp_path):
    tid = _TENANT["id"]
    tenant = dict(_TENANT, tunnel_plugins=json.dumps({"filesystem": {"enabled": True}}))
    tn._tunnel_sockets[tid] = object()  # fake live socket
    repo = str(tmp_path / "myproject")
    result = tn.build_launch_matrix(tenant, None, [_project("p1", repo_path=repo)])

    fs_rows = [r for r in result["rows"] if r["slot"] == "fs"]
    assert len(fs_rows) == 1
    row = fs_rows[0]
    assert row["local_launcher_status"] == "healthy"
    assert row["repo_scope_status"] == "ok"
    assert row["effective_repo_scope"] == repo
    assert row["active_invocable"] is True
    assert row["failure_class"] is None
    assert row["hosted_route"] == f"/tunnel-fs/{tid}"


def test_degraded_slot_is_not_invocable_and_carries_failure_class(tmp_path):
    tid = _TENANT["id"]
    tenant = dict(_TENANT, tunnel_plugins=json.dumps({"filesystem": {"enabled": True}}))
    tn._tunnel_sockets[tid] = object()
    tn._slot_health.setdefault(tid, {})["fs"] = False
    tn._slot_status_detail.setdefault(tid, {})["fs"] = {
        "reason": "unhealthy", "detail": "tools/list failed", "state": "degraded",
    }
    repo = str(tmp_path / "myproject")
    result = tn.build_launch_matrix(tenant, None, [_project("p1", repo_path=repo)])
    fs_row = next(r for r in result["rows"] if r["slot"] == "fs")
    assert fs_row["local_launcher_status"] == "degraded"
    assert fs_row["active_invocable"] is False
    assert fs_row["failure_class"] == "degraded"
    assert fs_row["last_health_result"]["last_error"] == "tools/list failed"
    assert "degraded" in fs_row["fallback"].lower()


# ---------------------------------------------------------------------------
# Repo-scope guard integration
# ---------------------------------------------------------------------------

def test_not_configured_repo_path_is_reported_distinctly():
    result = tn.build_launch_matrix(_TENANT, None, [_project("p1")])
    row = result["rows"][0]
    assert row["repo_scope_status"] == "not_configured"
    assert row["effective_repo_scope"] is None


@pytest.mark.parametrize("home_path", [
    "/home/user",
    "C:\\Users\\me",
])
def test_bare_home_directory_repo_path_fails_closed(home_path):
    tid = _TENANT["id"]
    tenant = dict(_TENANT, tunnel_plugins=json.dumps({"filesystem": {"enabled": True}}))
    tn._tunnel_sockets[tid] = object()  # slot IS healthy...
    result = tn.build_launch_matrix(tenant, None, [_project("p1", repo_path=home_path)])
    fs_row = next(r for r in result["rows"] if r["slot"] == "fs")
    assert fs_row["repo_scope_status"] == "rejected_home_directory"
    # ...but a bare-home scope must never be reported invocable regardless.
    assert fs_row["active_invocable"] is False


def test_cross_project_repo_path_mismatch_fails_closed_for_both(tmp_path):
    tid = _TENANT["id"]
    tenant = dict(_TENANT, tunnel_plugins=json.dumps({"filesystem": {"enabled": True}}))
    tn._tunnel_sockets[tid] = object()
    shared_repo = str(tmp_path / "shared-checkout")
    projects = [
        _project("p1", name="Project One", repo_path=shared_repo),
        _project("p2", name="Project Two", repo_path=shared_repo),
    ]
    result = tn.build_launch_matrix(tenant, None, projects)
    for pid in ("p1", "p2"):
        row = next(r for r in result["rows"] if r["project_id"] == pid and r["slot"] == "fs")
        assert row["repo_scope_status"] == "cross_project_mismatch", pid
        assert row["active_invocable"] is False, pid


def test_distinct_repo_paths_across_projects_are_unaffected(tmp_path):
    tid = _TENANT["id"]
    tenant = dict(_TENANT, tunnel_plugins=json.dumps({"filesystem": {"enabled": True}}))
    tn._tunnel_sockets[tid] = object()
    projects = [
        _project("p1", repo_path=str(tmp_path / "repo-a")),
        _project("p2", repo_path=str(tmp_path / "repo-b")),
    ]
    result = tn.build_launch_matrix(tenant, None, projects)
    for pid in ("p1", "p2"):
        row = next(r for r in result["rows"] if r["project_id"] == pid and r["slot"] == "fs")
        assert row["repo_scope_status"] == "ok", pid
        assert row["active_invocable"] is True, pid


def test_executor_config_as_json_string_is_parsed(tmp_path):
    """DB-stored executor_config may round-trip as a JSON string rather than
    an already-parsed dict — the join must handle both shapes."""
    repo = str(tmp_path / "myproject")
    project = {"id": "p1", "name": "proj", "executor_config": json.dumps({"repo_path": repo})}
    result = tn.build_launch_matrix(_TENANT, None, [project])
    row = result["rows"][0]
    assert row["effective_repo_scope"] == repo
    assert row["repo_scope_status"] == "ok"


def test_malformed_executor_config_string_degrades_to_not_configured():
    project = {"id": "p1", "name": "proj", "executor_config": "{not json"}
    result = tn.build_launch_matrix(_TENANT, None, [project])
    row = result["rows"][0]
    assert row["repo_scope_status"] == "not_configured"


# ---------------------------------------------------------------------------
# Plugin-override reconciliation surfaced in the matrix (reuses resolve_plugins)
# ---------------------------------------------------------------------------

def test_stale_plugin_override_is_surfaced_on_the_row():
    """The extract slot's `previous_defaults` includes the pre-Serena default
    command — a saved override matching it exactly is flagged stale_override
    by resolve_plugins(); the matrix must surface that flag verbatim rather
    than silently overwriting the tenant's saved override."""
    tid = _TENANT["id"]
    stale_cmd = ["uvx", "mcp-server-code-extractor"]
    tenant = dict(
        _TENANT,
        tunnel_plugins=json.dumps({"code-extractor": {"command": stale_cmd}}),
    )
    result = tn.build_launch_matrix(tenant, None, [_project("p1")])
    extract_row = next(r for r in result["rows"] if r["slot"] == "extract")
    assert extract_row["plugin_stale_override"] is True
    assert extract_row["plugin_command"] == stale_cmd
    assert extract_row["plugin_newer_default_command"] is not None
    assert extract_row["plugin_newer_default_command"] != stale_cmd


def test_no_override_reports_stale_override_false():
    result = tn.build_launch_matrix(_TENANT, None, [_project("p1")])
    extract_row = next(r for r in result["rows"] if r["slot"] == "extract")
    assert extract_row["plugin_stale_override"] is False


# ---------------------------------------------------------------------------
# Diagnostics parity: config digest + health timestamp (acceptance criterion)
# ---------------------------------------------------------------------------

def test_config_digest_matches_underlying_diagnostics_manifest_hash():
    tenant = dict(_TENANT, tunnel_plugins=json.dumps({"filesystem": {"enabled": True}}))
    diag = tn.build_tunnel_diagnostics(tenant, None)
    matrix = tn.build_launch_matrix(tenant, None, [_project("p1")])
    assert matrix["config_digest"] == diag["connector_manifest"]["manifest_hash"]
    assert isinstance(matrix["config_digest"], str) and matrix["config_digest"]


def test_health_timestamp_is_a_real_float_and_present_on_every_row():
    result = tn.build_launch_matrix(_TENANT, None, [_project("p1"), _project("p2")])
    assert isinstance(result["health_timestamp"], float)
    for row in result["rows"]:
        assert row["health_timestamp"] == result["health_timestamp"]
        assert row["config_digest"] == result["config_digest"]


def test_run_id_is_unique_per_call():
    r1 = tn.build_launch_matrix(_TENANT, None, [_project("p1")])
    r2 = tn.build_launch_matrix(_TENANT, None, [_project("p1")])
    assert r1["run_id"] != r2["run_id"]
