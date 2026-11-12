"""Tests for the read-only desktop routes (meridian/routes/desktop_read.py).

Every test runs against a temporary database, a temporary code-search index
directory, and temporary repository and outputs trees. They are injected through
environment variables, so nothing touches the real user config, the real registry,
the real keyring, the real %LOCALAPPDATA%, or the network.
"""
from __future__ import annotations

import asyncio
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from meridian import code_search
from meridian import db as db_module
from meridian import outputs_indexer
from meridian.db import research_runs as run_db
from meridian.routes import desktop_read

CODE = "/desktop/code/search"
OUTPUTS = "/desktop/outputs/search"
RUNS = "/desktop/research/runs"
ROUTES = (CODE, OUTPUTS, RUNS)
# Query parameters each route accepts, used when a test needs a request that is
# otherwise valid.
VALID_PARAMS = {CODE: {"q": "needle"}, OUTPUTS: {"q": "needle"}, RUNS: {}}

LOOPBACK = ("127.0.0.1", 50000)
# TestClient's default peer is "testclient", which is not a loopback address.
NON_LOOPBACK_PEERS = (("203.0.113.9", 40000), ("10.0.0.5", 40000), ("testclient", 50000))
FORWARDED_HEADERS = ("X-Forwarded-For", "X-Forwarded-Host", "X-Real-IP", "Forwarded")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


async def _session(db, prefix: str):
    project = await db_module.create_project(db, prefix)
    session = await db_module.register_session(db, project["id"], f"{prefix}-session")
    return project, session


async def _seed_runs(db) -> dict[str, str]:
    a_project, a_session = await _session(db, "desk-alpha")
    b_project, b_session = await _session(db, "desk-beta")
    first = await run_db.start_research_run(
        db, a_project["id"], a_session["id"],
        mode="read_only", repository_id="desk-repo@main", turn_budget=5,
    )
    second = await run_db.start_research_run(
        db, a_project["id"], a_session["id"],
        mode="read_only", repository_id="desk-repo@main", turn_budget=5,
    )
    await run_db.complete_research_run(
        db, a_project["id"], a_session["id"],
        run_id=second["id"],
        receipt={"result_summary": "Traced the bug to the cache"},
        disposition="keep",
    )
    third = await run_db.start_research_run(
        db, b_project["id"], b_session["id"],
        mode="read_only", repository_id="desk-other@main", turn_budget=5,
    )
    # Pin started_at so newest-first ordering is deterministic.
    for run, stamp in (
        (first, "2026-01-01T00:00:00.000Z"),
        (third, "2026-01-02T00:00:00.000Z"),
        (second, "2026-01-03T00:00:00.000Z"),
    ):
        await db.execute(
            "UPDATE scratch_research_runs SET started_at = ? WHERE id = ?",
            (stamp, run["id"]),
        )
    await db.commit()
    return {
        "project_a": a_project["id"],
        "first": first["id"],
        "second": second["id"],
        "third": third["id"],
    }


@pytest.fixture
def desk(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    index_dir = tmp_path / "code-index"
    monkeypatch.delenv("MERIDIAN_HOSTED", raising=False)
    monkeypatch.setenv("MERIDIAN_CODE_SEARCH_DIR", str(index_dir))
    monkeypatch.setenv("MERIDIAN_DESKTOP_CODE_ROOT", str(repo))
    monkeypatch.setenv(desktop_read.OUTPUTS_DIR_ENV, str(outputs))
    conn = asyncio.run(db_module.init_db(str(tmp_path / "meridian.db")))
    app = FastAPI()
    app.include_router(desktop_read.router)
    app.state.db = conn
    try:
        with TestClient(app, client=LOOPBACK) as client:
            yield {
                "app": app,
                "client": client,
                "conn": conn,
                "repo": repo,
                "outputs": outputs,
                "index_dir": index_dir,
                "tmp": tmp_path,
            }
    finally:
        asyncio.run(conn.close())


# ---------------------------------------------------------------------------
# Registration and configuration
# ---------------------------------------------------------------------------


def test_server_app_registers_the_desktop_routes():
    import meridian.server as server

    # Read the public OpenAPI paths: on newer FastAPI, app.routes holds included
    # routers as wrapper objects rather than flat route objects.
    paths = server.app.openapi()["paths"]
    for path in ROUTES:
        assert "get" in paths[path], path


def test_default_code_root_is_the_repository_that_contains_meridian(monkeypatch):
    import meridian

    monkeypatch.delenv(desktop_read.CODE_ROOT_ENV, raising=False)
    expected = Path(meridian.__file__).resolve().parents[1]
    assert desktop_read._configured_code_root() == expected


# ---------------------------------------------------------------------------
# Code search
# ---------------------------------------------------------------------------


def test_code_search_not_indexed_and_never_builds_an_index(desk):
    response = desk["client"].get(CODE, params={"q": "needle"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["index_state"] == "not_indexed"
    assert body["results"] == []
    # A GET must not index or walk the tree.
    assert list(desk["index_dir"].glob("*.sqlite")) == []


def test_code_search_ready_returns_path_line_snippet_and_score(desk):
    _write(desk["repo"] / "src" / "alpha.py", "import os\n\ndef find_needle():\n    return 'needle'\n")
    _write(desk["repo"] / "docs" / "notes.md", "nothing relevant here\n")
    code_search.refresh_index(desk["repo"])

    response = desk["client"].get(CODE, params={"q": "needle"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["index_state"] == "ready"
    assert len(body["results"]) == 1
    hit = body["results"][0]
    assert set(hit) == {"path", "line", "snippet", "score"}
    assert hit["path"] == "src/alpha.py"
    assert hit["line"] == 3
    assert "needle" in hit["snippet"]
    assert isinstance(hit["score"], float)


def test_code_search_zero_hits_is_still_ready(desk):
    _write(desk["repo"] / "a.py", "alpha beta\n")
    code_search.refresh_index(desk["repo"])
    body = desk["client"].get(CODE, params={"q": "absentword"}).json()
    assert body["index_state"] == "ready"
    assert body["results"] == []


def test_code_search_only_returns_files_inside_the_configured_root(desk):
    _write(desk["tmp"] / "outside" / "secret.py", "outsidetoken = 1\n")
    _write(desk["repo"] / "inside.py", "insidetoken = 1\n")
    code_search.refresh_index(desk["repo"])

    client = desk["client"]
    assert client.get(CODE, params={"q": "outsidetoken"}).json()["results"] == []
    # A path-shaped query is only text: it cannot make the route read that file.
    assert client.get(CODE, params={"q": "../outside/secret.py"}).json()["results"] == []
    hits = client.get(CODE, params={"q": "insidetoken"}).json()["results"]
    assert [hit["path"] for hit in hits] == ["inside.py"]


def test_code_search_refuses_an_index_built_for_another_root(desk):
    other = desk["tmp"] / "other"
    _write(other / "other.py", "shared needle\n")
    code_search.refresh_index(other)
    # Place another repository's index where this repository's index is looked up.
    shutil.copyfile(
        code_search.db_path_for(other), code_search.db_path_for(desk["repo"]),
    )

    body = desk["client"].get(CODE, params={"q": "needle"}).json()
    assert body["index_state"] == "not_indexed"
    assert body["results"] == []


def test_code_search_unavailable_when_the_index_is_unreadable(desk):
    _write(desk["repo"] / "a.py", "needle\n")
    code_search.refresh_index(desk["repo"])
    code_search.db_path_for(desk["repo"]).write_bytes(b"this is not a sqlite database" * 100)

    response = desk["client"].get(CODE, params={"q": "needle"})
    assert response.status_code == 200
    assert response.json()["index_state"] == "unavailable"
    assert "Traceback" not in response.text


def test_code_search_limit_is_clamped(desk):
    for index in range(55):
        _write(desk["repo"] / f"file_{index:02d}.py", "clampterm\n")
    code_search.refresh_index(desk["repo"])

    client = desk["client"]

    def count(limit: int) -> int:
        return len(client.get(CODE, params={"q": "clampterm", "limit": limit}).json()["results"])

    assert count(500) == code_search.MAX_LIMIT
    assert count(0) == 1
    assert count(-3) == 1


# ---------------------------------------------------------------------------
# Outputs search
# ---------------------------------------------------------------------------


def test_outputs_search_not_indexed_without_a_configured_directory(desk, monkeypatch):
    monkeypatch.delenv(desktop_read.OUTPUTS_DIR_ENV, raising=False)
    body = desk["client"].get(OUTPUTS, params={"q": "temperature"}).json()
    assert body["index_state"] == "not_indexed"
    assert body["results"] == []


def test_outputs_search_not_indexed_when_the_configured_directory_is_missing(desk, monkeypatch):
    monkeypatch.setenv(desktop_read.OUTPUTS_DIR_ENV, str(desk["tmp"] / "absent"))
    body = desk["client"].get(OUTPUTS, params={"q": "temperature"}).json()
    assert body["index_state"] == "not_indexed"
    assert body["results"] == []


def test_outputs_search_ready_returns_the_indexed_fields(desk):
    _write(desk["outputs"] / "runs" / "alpha.csv", "temperature,pressure\n300,1\n")
    _write(desk["outputs"] / "runs" / "beta.json", '{"temperature": 5}\n')

    response = desk["client"].get(OUTPUTS, params={"q": "temperature"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["index_state"] == "ready"
    alpha = next(hit for hit in body["results"] if hit["path"].endswith("alpha.csv"))
    assert set(alpha) == set(desktop_read._OUTPUT_FIELDS)
    assert alpha["score"] is not None
    assert "line" not in alpha and "snippet" not in alpha


def test_outputs_search_limit_is_clamped_to_at_least_one(desk):
    for index in range(3):
        _write(desk["outputs"] / f"run_{index}.csv", "shared,value\n1,2\n")
    body = desk["client"].get(OUTPUTS, params={"q": "shared", "limit": 0}).json()
    assert body["index_state"] == "ready"
    assert len(body["results"]) == 1


def test_outputs_search_reports_a_search_error_as_unavailable(desk, monkeypatch):
    monkeypatch.setattr(
        outputs_indexer, "search_outputs",
        lambda *args, **kwargs: {"error": "internal detail", "hits": []},
    )
    body = desk["client"].get(OUTPUTS, params={"q": "temperature"}).json()
    assert body["index_state"] == "unavailable"
    assert "internal detail" not in str(body)


def test_outputs_search_flags_a_partial_index(desk, monkeypatch):
    monkeypatch.setattr(
        outputs_indexer, "search_outputs",
        lambda *args, **kwargs: {"hits": [], "partial": True},
    )
    body = desk["client"].get(OUTPUTS, params={"q": "temperature"}).json()
    assert body["index_state"] == "ready"
    assert body["partial"] is True


def test_outputs_search_unavailable_when_the_index_raises(desk, monkeypatch):
    def explode(*args, **kwargs):
        raise RuntimeError("duckdb failure at /private/location")

    monkeypatch.setattr(outputs_indexer, "search_outputs", explode)
    response = desk["client"].get(OUTPUTS, params={"q": "temperature"})
    assert response.status_code == 200
    body = response.json()
    assert body["index_state"] == "unavailable"
    assert "duckdb" not in response.text
    assert "Traceback" not in response.text


def test_outputs_search_sends_hits_as_the_same_list_as_results(desk):
    _write(desk["outputs"] / "runs" / "alpha.csv", "temperature,pressure\n300,1\n")
    body = desk["client"].get(OUTPUTS, params={"q": "temperature"}).json()
    assert body["index_state"] == "ready"
    assert body["results"]
    assert body["hits"] == body["results"]


def test_outputs_search_hits_are_empty_when_no_index_is_configured(desk, monkeypatch):
    monkeypatch.delenv(desktop_read.OUTPUTS_DIR_ENV, raising=False)
    body = desk["client"].get(OUTPUTS, params={"q": "temperature"}).json()
    assert body["hits"] == []
    assert body["results"] == []


def test_outputs_search_convergence_has_the_fields_the_desktop_client_reads(desk):
    _write(desk["outputs"] / "runs" / "alpha.csv", "temperature,pressure\n300,1\n")
    body = desk["client"].get(OUTPUTS, params={"q": "temperature"}).json()
    convergence = body["convergence"]
    assert set(convergence) == {
        "partial_index", "pending_count", "degraded", "inconclusive",
        "walk_errors", "db_write_error", "last_rebuilt_at",
    }
    assert convergence["partial_index"] is False
    assert convergence["pending_count"] == 0
    assert convergence["degraded"] is False
    assert convergence["walk_errors"] == []
    assert convergence["db_write_error"] is None
    stamp = convergence["last_rebuilt_at"]
    assert isinstance(stamp, str) and stamp.endswith("+00:00")
    # The index_state string is still sent alongside the new object.
    assert body["index_state"] == "ready"


def test_outputs_search_partial_index_without_a_snapshot_is_degraded(desk, monkeypatch):
    monkeypatch.setattr(
        outputs_indexer, "search_outputs",
        lambda *args, **kwargs: {"hits": [], "partial": True},
    )
    body = desk["client"].get(OUTPUTS, params={"q": "temperature"}).json()
    assert body["partial"] is True
    assert body["convergence"]["partial_index"] is True
    assert body["convergence"]["degraded"] is True


def test_outputs_search_pending_and_write_failures_are_reported_without_detail(desk, monkeypatch):
    monkeypatch.setattr(
        outputs_indexer, "search_outputs",
        lambda *args, **kwargs: {
            "hits": [], "partial": True, "pending_count": 7,
            "db_write_error": "disk I/O error at /private/outputs.db",
        },
    )
    response = desk["client"].get(OUTPUTS, params={"q": "temperature"})
    body = response.json()
    convergence = body["convergence"]
    assert convergence["pending_count"] == 7
    assert convergence["inconclusive"] is True
    assert convergence["degraded"] is True
    assert convergence["db_write_error"] == desktop_read.INDEX_DB_WRITE_MESSAGE
    assert "/private" not in response.text


def test_outputs_search_passes_the_indexer_snapshot_through_without_paths(desk, monkeypatch):
    snapshot = {
        "partial_index": False,
        "pending_count": 0,
        "degraded": True,
        "inconclusive": True,
        "walk_errors": ["/secret/one", "/secret/two"],
        "db_write_error": None,
        "last_rebuilt_at": datetime(2024, 11, 12, 8, 26, 40, tzinfo=timezone.utc).timestamp(),
    }
    monkeypatch.setattr(
        outputs_indexer, "search_outputs",
        lambda *args, **kwargs: {"hits": [], "convergence": snapshot, "degraded": True},
    )
    response = desk["client"].get(OUTPUTS, params={"q": "temperature"})
    convergence = response.json()["convergence"]
    assert convergence["degraded"] is True
    assert convergence["walk_errors"] == [desktop_read.INDEX_WALK_MESSAGE] * 2
    assert convergence["last_rebuilt_at"] == "2024-11-12T08:26:40+00:00"
    assert "/secret" not in response.text


def test_outputs_search_without_an_index_is_never_a_confirmed_miss(desk, monkeypatch):
    monkeypatch.setenv(desktop_read.OUTPUTS_DIR_ENV, str(desk["tmp"] / "absent"))
    body = desk["client"].get(OUTPUTS, params={"q": "temperature"}).json()
    assert body["index_state"] == "not_indexed"
    assert body["convergence"]["degraded"] is True
    assert body["convergence"]["last_rebuilt_at"] is None


def test_outputs_search_unavailable_reports_a_degraded_convergence_and_no_hits(desk, monkeypatch):
    def explode(*args, **kwargs):
        raise RuntimeError("duckdb failure")

    monkeypatch.setattr(outputs_indexer, "search_outputs", explode)
    body = desk["client"].get(OUTPUTS, params={"q": "temperature"}).json()
    assert body["index_state"] == "unavailable"
    assert body["hits"] == []
    assert body["results"] == []
    assert body["convergence"]["degraded"] is True
    assert body["convergence"]["inconclusive"] is True


# ---------------------------------------------------------------------------
# Research runs
# ---------------------------------------------------------------------------


def test_research_runs_empty_database_returns_no_runs(desk):
    response = desk["client"].get(RUNS)
    assert response.status_code == 200, response.text
    assert response.json() == {"runs": [], "count": 0}


def test_research_runs_are_newest_first_across_projects(desk):
    ids = asyncio.run(_seed_runs(desk["conn"]))
    body = desk["client"].get(RUNS).json()

    assert body["count"] == 3
    assert [run["id"] for run in body["runs"]] == [ids["second"], ids["third"], ids["first"]]
    second, third, first = body["runs"]
    assert second["title"] == "Traced the bug to the cache"
    assert second["state"] == "completed"
    assert second["project_id"] == ids["project_a"]
    assert second["started_at"] == "2026-01-03T00:00:00.000Z"
    assert second["completed_at"]
    assert third["title"] == "read_only run in desk-other@main"
    assert third["state"] == "active"
    assert first["state"] == "active"
    # Paths and the full receipt are not part of the desktop view.
    assert "allowed_paths" not in second
    assert "result_receipt" not in second


def test_research_runs_project_filter_and_limit(desk):
    ids = asyncio.run(_seed_runs(desk["conn"]))
    client = desk["client"]

    scoped = client.get(RUNS, params={"project_id": ids["project_a"]}).json()
    assert [run["id"] for run in scoped["runs"]] == [ids["second"], ids["first"]]

    limited = client.get(RUNS, params={"limit": 2}).json()
    assert [run["id"] for run in limited["runs"]] == [ids["second"], ids["third"]]
    assert client.get(RUNS, params={"limit": 0}).json()["count"] == 1
    assert client.get(RUNS, params={"project_id": "no-such-project"}).json()["runs"] == []


def test_research_runs_rejects_an_overlong_project_id(desk):
    response = desk["client"].get(RUNS, params={"project_id": "p" * 129})
    assert response.status_code == 400
    assert response.json() == {"error": "project_id is too long"}


def test_research_runs_store_failure_is_a_plain_503(desk, monkeypatch):
    asyncio.run(db_module.create_project(desk["conn"], "desk-solo"))

    def explode(*args, **kwargs):
        raise RuntimeError("sql failure near /secret/path")

    monkeypatch.setattr(run_db, "list_research_runs", explode)
    response = desk["client"].get(RUNS)
    assert response.status_code == 503
    assert response.json() == {"error": "Research runs are unavailable right now."}
    assert "secret" not in response.text
    assert "Traceback" not in response.text


# ---------------------------------------------------------------------------
# Gate: hosted refusal, path refusal, query validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("route", ROUTES)
def test_hosted_mode_answers_404_on_every_desktop_route(desk, monkeypatch, route):
    monkeypatch.setenv("MERIDIAN_HOSTED", "1")
    response = desk["client"].get(route, params={"q": "needle"})
    assert response.status_code == 404
    assert "error" in response.json()


def test_app_wide_hosted_gate_refuses_anonymous_callers_before_the_handler(desk, monkeypatch):
    from fastapi import Depends

    from meridian.hosted_route_gate import hosted_route_gate

    monkeypatch.setenv("MERIDIAN_HOSTED", "1")
    # The real app attaches this gate to every router; the desktop routes are not
    # allowlisted, so an anonymous hosted caller must get 401 and no data.
    gated = FastAPI(dependencies=[Depends(hosted_route_gate)])
    gated.include_router(desktop_read.router)
    gated.state.db = desk["conn"]
    with TestClient(gated) as client:
        for route in ROUTES:
            response = client.get(route, params={"q": "needle"})
            assert response.status_code == 401, route
            assert "results" not in response.json() and "runs" not in response.json()


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("param", ["path", "root", "outputs_dir", "file"])
def test_path_parameters_are_refused_not_ignored(desk, route, param):
    response = desk["client"].get(route, params={"q": "needle", param: "/etc"})
    assert response.status_code == 400
    assert response.json()["error"].startswith("Filesystem paths are not accepted")


@pytest.mark.parametrize("route", [CODE, OUTPUTS])
def test_blank_missing_or_oversized_query_is_a_plain_400(desk, route):
    client = desk["client"]
    assert client.get(route, params={"q": "   "}).status_code == 400
    assert client.get(route).json() == {"error": "q is required"}
    oversized = client.get(route, params={"q": "x" * (desktop_read.MAX_QUERY_CHARS + 1)})
    assert oversized.status_code == 400
    assert "at most" in oversized.json()["error"]


# ---------------------------------------------------------------------------
# Loopback only: peer address and forwarding headers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("peer", NON_LOOPBACK_PEERS)
def test_non_loopback_peer_is_refused_on_every_desktop_route(desk, route, peer):
    outsider = TestClient(desk["app"], client=peer)

    response = outsider.get(route, params=VALID_PARAMS[route])

    assert response.status_code == 403
    assert "results" not in response.json() and "runs" not in response.json()


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("header", FORWARDED_HEADERS)
def test_loopback_peer_with_a_forwarding_header_is_refused(desk, route, header):
    client = desk["client"]
    assert client.get(route, params=VALID_PARAMS[route]).status_code == 200

    value = "for=203.0.113.9" if header == "Forwarded" else "203.0.113.9"
    response = client.get(route, params=VALID_PARAMS[route], headers={header: value})

    assert response.status_code == 403
    assert "results" not in response.json() and "runs" not in response.json()


# ---------------------------------------------------------------------------
# Query parameter allowlist
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("route", ROUTES)
def test_unknown_query_parameter_is_refused_and_named(desk, route):
    response = desk["client"].get(route, params={"bogus": "1"})

    assert response.status_code == 400
    assert "'bogus'" in response.json()["error"]


def test_research_runs_does_not_accept_a_search_query(desk):
    response = desk["client"].get(RUNS, params={"q": "needle"})

    assert response.status_code == 400
    assert "'q'" in response.json()["error"]
    assert "project_id" in response.json()["error"]


# ---------------------------------------------------------------------------
# Symlinks that leave the repository
# ---------------------------------------------------------------------------


def test_a_symlink_that_leaves_the_repository_is_never_indexed_or_read(desk, monkeypatch):
    outside = desk["tmp"] / "outside" / "secret.py"
    _write(outside, "escapedtoken = 1\n")
    _write(desk["repo"] / "inside.py", "insidetoken = 1\n")
    try:
        os.symlink(outside, desk["repo"] / "linked.py")
        os.symlink(desk["repo"] / "inside.py", desk["repo"] / "alias.py")
    except OSError:
        pytest.skip("creating a symlink needs extra privilege on this platform")

    reads: list[Path] = []
    real_read = code_search._read_text

    def recording_read(path, size):
        reads.append(Path(path).resolve())
        return real_read(path, size)

    monkeypatch.setattr(code_search, "_read_text", recording_read)
    code_search.refresh_index(desk["repo"])

    client = desk["client"]
    assert client.get(CODE, params={"q": "escapedtoken"}).json()["results"] == []
    assert outside.resolve() not in reads
    hits = client.get(CODE, params={"q": "insidetoken"}).json()["results"]
    # A symlink whose target stays inside the repository is still indexed.
    assert {hit["path"] for hit in hits} == {"inside.py", "alias.py"}
