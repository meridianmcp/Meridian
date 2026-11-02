"""GET /workspace/overview: route, loopback, hosted and serialization cases (spec 9.1, R1-R7).

The builder has its own cases in ``tests/test_workspace_overview.py``. Here the route is
exercised through the real app: the loopback, Host, Origin and Sec-Fetch-Site checks (R1-R3),
hosted gating (R4, R5), the hosted hooks diagnostics (R6), the no-store header (R7), one
real document serialized through the route with no stub (review M5), and a failing section
that still answers 200 (review M3).

The guard dir and the home directory are temp paths, and the toml connection lookup is
stubbed, so no test reads the real machine.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from meridian import cbm_registry as reg
from meridian import db as dbm
from meridian import hook_paths as hp
from meridian import hosted_route_gate as gate
from meridian import toml_config
from meridian import workspace_overview as wo
from meridian.routes import workspace_overview as route
from meridian.routes import _local_only as local

PATH = "/workspace/overview"
LOOPBACK = ("127.0.0.1", 50000)
SCHEMA = "meridian-workspace-overview/1"


@pytest.fixture(autouse=True)
def _isolated_machine(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    guard = tmp_path / "guard"
    guard.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("MERIDIAN_GUARD_DIR", str(guard))
    monkeypatch.setattr(toml_config, "get_active_db_url", lambda: (None, "local"))
    monkeypatch.setattr(wo.socket, "gethostname", lambda: "laptop.corp.local")
    return home


@pytest.fixture
def stub_build(monkeypatch):
    """Route tests replace the builder and the DB accessor; the builder has its own tests."""
    calls: list[dict] = []

    async def fake_build(db, *, env, now, fs=None, guard_dir=None):
        calls.append({"db": db, "now": now})
        return {"schema": "stub"}

    async def fake_db(request):
        return "stub-db"

    monkeypatch.setattr(route, "build_overview", fake_build)
    monkeypatch.setattr(route, "_db", fake_db)
    return calls


@pytest.fixture
def server_module():
    import meridian.server as module

    return module


@pytest.fixture
def hosted_off(monkeypatch):
    monkeypatch.delenv("MERIDIAN_HOSTED", raising=False)


@pytest.fixture
def hosted_on(monkeypatch):
    monkeypatch.setenv("MERIDIAN_HOSTED", "1")


def _client(server_module, host: str = "127.0.0.1") -> TestClient:
    return TestClient(server_module.app, client=(host, 50000), base_url="http://localhost")


def _local_scope(headers=None, client=LOOPBACK):
    return {
        "type": "http", "method": "GET", "path": PATH,
        "headers": headers if headers is not None else [(b"host", b"localhost")],
        "query_string": b"", "client": client,
    }


# ---------------------------------------------------------------------------
# R1-R3: loopback, Host, Origin and Sec-Fetch-Site (spec 6.1)
# ---------------------------------------------------------------------------


def test_loopback_clients_get_the_overview(server_module, stub_build, hosted_off):
    for host in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
        response = _client(server_module, host).get(PATH)
        assert response.status_code == 200, host
        assert response.json() == {"schema": "stub"}
    assert len(stub_build) == 3


@pytest.mark.parametrize("client_host", ["10.0.0.5", "203.0.113.9", "testclient"])
def test_non_loopback_clients_are_refused(server_module, stub_build, hosted_off, client_host):
    response = _client(server_module, client_host).get(PATH)
    assert response.status_code == 403
    assert stub_build == []


def test_forwarded_headers_are_refused_even_from_loopback(server_module, stub_build, hosted_off):
    for header in ("X-Forwarded-For", "Forwarded", "X-Real-IP", "X-Forwarded-Host"):
        response = _client(server_module).get(PATH, headers={header: "203.0.113.9"})
        assert response.status_code == 403, header
    assert stub_build == []


@pytest.mark.parametrize("host_header", ["evil.example", "evil.example:7878", "localhost.evil.example"])
def test_a_rebinding_host_header_is_refused(server_module, stub_build, hosted_off, host_header):
    response = _client(server_module).get(PATH, headers={"Host": host_header})
    assert response.status_code == 403
    assert stub_build == []


@pytest.mark.parametrize("host_header", ["localhost", "127.0.0.1:7878", "[::1]:7878", "LOCALHOST:9000"])
def test_loopback_host_headers_are_accepted(server_module, stub_build, hosted_off, host_header):
    response = _client(server_module).get(PATH, headers={"Host": host_header})
    assert response.status_code == 200


def test_cross_site_fetch_is_refused_and_same_origin_is_accepted(server_module, stub_build, hosted_off):
    assert _client(server_module).get(PATH, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert _client(server_module).get(PATH, headers={"Sec-Fetch-Site": "same-origin"}).status_code == 200
    assert _client(server_module).get(PATH, headers={"Sec-Fetch-Site": "none"}).status_code == 200


def test_foreign_origin_is_refused_and_loopback_origin_is_accepted(server_module, stub_build, hosted_off):
    assert _client(server_module).get(PATH, headers={"Origin": "https://evil.example"}).status_code == 403
    assert _client(server_module).get(PATH, headers={"Origin": "http://localhost:7878"}).status_code == 200
    assert _client(server_module).get(PATH, headers={"Origin": "null"}).status_code == 403


def test_response_is_marked_no_store(server_module, stub_build, hosted_off):
    response = _client(server_module).get(PATH)
    assert response.headers["cache-control"] == "no-store"


async def test_request_without_a_client_is_refused_directly():
    scope = _local_scope(client=None)
    with pytest.raises(route.HTTPException) as info:
        await local.require_local_request(Request(scope), "refused")
    assert info.value.status_code == 403


def test_loopback_helpers_accept_and_reject_the_documented_forms():
    assert local.is_loopback_host("127.8.9.10") is True
    assert local.is_loopback_host("::ffff:127.0.0.1") is True
    assert local.is_loopback_host("192.168.1.2") is False
    assert local.is_loopback_host("not-an-ip") is False
    assert local.is_loopback_host_header(None) is False
    assert local.is_loopback_host_header("localhost:70000") is True
    assert local.is_loopback_host_header("localhost.example") is False
    assert local.is_loopback_origin("http://[::1]:7878") is True
    assert local.is_loopback_origin("ftp://localhost") is False
    assert local.is_loopback_origin(None) is False


# ---------------------------------------------------------------------------
# R4-R5: hosted gating (spec 6.2, 6.3)
# ---------------------------------------------------------------------------


def test_hosted_loopback_and_remote_both_get_404_when_the_gate_is_out_of_the_way(
    server_module, stub_build, hosted_on,
):
    server_module.app.dependency_overrides[gate.hosted_route_gate] = lambda: None
    try:
        assert _client(server_module).get(PATH).status_code == 404
        assert _client(server_module, "10.0.0.5").get(PATH).status_code == 404
    finally:
        server_module.app.dependency_overrides.pop(gate.hosted_route_gate, None)
    assert stub_build == []


def test_hosted_anonymous_caller_gets_401_from_the_app_gate(server_module, stub_build, hosted_on, monkeypatch):
    async def no_tenant(conn):
        return None

    monkeypatch.setattr(gate, "_get_tenant_from_request", no_tenant)
    response = _client(server_module).get(PATH)
    assert response.status_code == 401
    assert stub_build == []


def test_hosted_authenticated_tenant_gets_404(server_module, stub_build, hosted_on, monkeypatch):
    async def tenant(conn):
        return {"id": "tenant-1"}

    monkeypatch.setattr(gate, "_get_tenant_from_request", tenant)
    response = _client(server_module).get(PATH)
    assert response.status_code == 404
    assert stub_build == []


async def test_require_local_only_is_404_when_hosted(monkeypatch):
    monkeypatch.setenv("MERIDIAN_HOSTED", "1")
    with pytest.raises(route.HTTPException) as info:
        await route.require_local_only(Request(_local_scope()))
    assert info.value.status_code == 404


# ---------------------------------------------------------------------------
# R6: GET /hooks/diagnostics on hosted (spec 6.10, review B1)
# ---------------------------------------------------------------------------


def test_hosted_hooks_diagnostics_is_not_public_to_anonymous_callers(server_module, hosted_on, monkeypatch):
    async def no_tenant(conn):
        return None

    monkeypatch.setattr(gate, "_get_tenant_from_request", no_tenant)
    response = TestClient(server_module.app, client=LOOPBACK, base_url="http://localhost").get("/hooks/diagnostics")
    assert response.status_code == 401


def test_hosted_hooks_diagnostics_is_404_for_an_authenticated_tenant(server_module, hosted_on, monkeypatch):
    async def tenant(conn):
        return {"id": "tenant-1"}

    monkeypatch.setattr(gate, "_get_tenant_from_request", tenant)
    response = TestClient(server_module.app, client=LOOPBACK, base_url="http://localhost").get("/hooks/diagnostics")
    assert response.status_code == 404


def test_hosted_hooks_diagnostics_handler_refuses_even_past_the_gate(server_module, hosted_on):
    server_module.app.dependency_overrides[gate.hosted_route_gate] = lambda: None
    try:
        response = TestClient(server_module.app, client=LOOPBACK, base_url="http://localhost").get("/hooks/diagnostics")
    finally:
        server_module.app.dependency_overrides.pop(gate.hosted_route_gate, None)
    assert response.status_code == 404


def test_self_hosted_hooks_diagnostics_still_answers_from_loopback(server_module, hosted_off, monkeypatch):
    monkeypatch.setattr(hp, "resolve_active_repo_root", lambda cwd=None: None)
    response = TestClient(server_module.app, client=LOOPBACK, base_url="http://localhost").get("/hooks/diagnostics")
    assert response.status_code == 200
    assert response.json()["settings_found"] is False


# ---------------------------------------------------------------------------
# Handler wiring and serialization (review M3, M5)
# ---------------------------------------------------------------------------


async def test_endpoint_builds_the_document_from_the_request_database(monkeypatch, db):
    seen: dict = {}

    async def fake_db(request):
        return db

    async def fake_build(database, *, env, now, fs=None, guard_dir=None):
        seen.update(db=database, now=now, env=env)
        return {"schema": "stub"}

    monkeypatch.setattr(route, "_db", fake_db)
    monkeypatch.setattr(route, "build_overview", fake_build)
    response = await route.get_workspace_overview(Request(_local_scope()))
    assert response.headers["cache-control"] == "no-store"
    assert seen["db"] is db
    assert seen["now"] > 0
    assert isinstance(seen["env"], dict)


async def test_a_real_document_serializes_through_the_route_handler(db, tmp_path, monkeypatch):
    """Review M5: no stub on the builder. The rendered body must be strict JSON."""
    repo = tmp_path / "real-repo"
    (repo / ".git").mkdir(parents=True)
    project = await dbm.create_project(db, "real")
    await dbm.set_executor_config(db, project["id"], {"repo_paths": [{"cwd": str(repo), "hostname": "laptop"}]})
    await dbm.register_session(db, project["id"], "real-session")

    async def fake_db(request):
        return db

    monkeypatch.setattr(route, "_db", fake_db)
    response = await route.get_workspace_overview(Request(_local_scope()))
    assert response.status_code == 200
    body = json.loads(response.body)
    assert body["schema"] == SCHEMA
    assert [item["name"] for item in body["repos"]["items"]] == ["real-repo"]
    assert body["repos"]["items"][0]["location"]["match"] == "same_machine"
    assert [item["name"] for item in body["sessions"]["items"]] == ["real-session"]
    json.dumps(body, allow_nan=False)


def test_the_route_serves_a_real_document_over_http(server_module, hosted_off, monkeypatch):
    """Review M5, over the full HTTP stack: the real builder runs and the body is strict JSON."""

    async def stub_db(request):
        return "stub-db"

    monkeypatch.setattr(route, "_db", stub_db)
    response = _client(server_module).get(PATH)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["schema"] == SCHEMA
    assert ("projects", "db_error") in {(e["section"], e["code"]) for e in body["errors"]}
    json.dumps(body, allow_nan=False)


@pytest.mark.parametrize("target", ["_assemble", "_runtime"])
def test_a_failing_section_still_answers_200_with_a_gap(server_module, hosted_off, monkeypatch, target):
    """Review M3: an injected failure in one section gives HTTP 200 and the section_failed gap."""

    def boom(*args, **kwargs):
        raise RuntimeError("injected failure")

    async def stub_db(request):
        return "stub-db"

    monkeypatch.setattr(wo, target, boom)
    monkeypatch.setattr(route, "_db", stub_db)
    response = _client(server_module).get(PATH)
    assert response.status_code == 200
    body = response.json()
    assert body["schema"] == SCHEMA
    assert "section_failed" in {gap["code"] for gap in body["gaps"]}
    json.dumps(body, allow_nan=False)


async def test_nan_and_infinity_on_disk_still_serve_strict_json(monkeypatch, db, tmp_path):
    """Review R1: a NaN stamp in the snapshot and an Infinity audit timestamp are unknown values.

    The real builder runs. The route answers 200, and the body parses with a parser that refuses
    the NaN and Infinity literals.
    """
    guard = tmp_path / "guard"
    rows, roots = [], {}
    for name, epoch in (("nan-epoch", float("nan")), ("inf-epoch", float("inf"))):
        repo = tmp_path / name
        (repo / ".git").mkdir(parents=True)
        root = str(repo).replace("\\", "/")
        roots[name] = root
        rows.append({
            "name": name, "root": root, "root_key": reg.path_key(reg.norm_path(root)),
            "db": str(tmp_path / f"{name}.db"), "indexed_epoch": epoch, "nodes": 5,
        })
        project = await dbm.create_project(db, name)
        await dbm.set_executor_config(db, project["id"], {"repo_paths": [{"cwd": root, "hostname": "laptop"}]})
    snapshot = {"schema": reg.SNAPSHOT_SCHEMA, "built_at": 1790452800, "rows": rows, "pins": {}, "servers": {}}
    (guard / "snapshot.json").write_text(json.dumps(snapshot), encoding="utf-8")
    audit_event = {"event": "PreToolUse", "rule": "G3", "decision": "deny", "root": roots["nan-epoch"], "ts": float("inf")}
    (guard / "audit.log").write_text(json.dumps(audit_event) + "\n", encoding="utf-8")

    async def fake_db(request):
        return db

    def refuse_constant(token):
        raise ValueError(f"non-finite literal {token}")

    monkeypatch.setattr(route, "_db", fake_db)
    response = await route.get_workspace_overview(Request(_local_scope()))
    assert response.status_code == 200
    body = json.loads(response.body, parse_constant=refuse_constant)
    assert body["schema"] == SCHEMA
    assert body["repos"]["snapshot"]["error"] is None
    items = {item["name"]: item for item in body["repos"]["items"]}
    assert set(items) == {"nan-epoch", "inf-epoch"}
    for item in items.values():
        assert item["codebase_memory"]["index"]["age_days"] is None
    assert items["nan-epoch"]["activity"]["last_guard_event_at"] is None
    assert items["nan-epoch"]["activity"]["last_guard_event_reason"] == wo._AUDIT_NO_EVENT
    json.dumps(body, allow_nan=False)
