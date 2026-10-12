"""Tests for sprint item 502206be -- REST surface for project capability manifests.

Covers GET/PUT /projects/{project_id}/capability-manifest:

1. Read: a project with no manifest gets the empty profile; an unknown project
   is a 404.
2. Write round trip: PUT replaces the manifest wholesale, canonicalizes order,
   and GET / the MCP ``get_capability_manifest`` tool return the same content
   and hash (one persistence path, two front doors).
3. Validation: malformed manifests (unknown field, secret-shaped value, bad
   body shape, invalid JSON) are rejected and NEVER partially written; the
   secret value is not echoed back in the error.
4. Authorization: the route enforces PERM_SETTINGS itself when a workspace
   role is in play, because the generic middleware only maps ``/settings``
   paths to that permission.
"""
from __future__ import annotations

import json as _json

from meridian import capability_manifest as cm
from meridian.routes import projects as projects_routes


def _valid_capability(**overrides):
    base = {
        "id": "code-search",
        "purpose": "find symbols/functions/classes",
        "required_tools": ["Serena: find_symbol"],
    }
    base.update(overrides)
    return base


def _manifest_url(project_id: str) -> str:
    return f"/projects/{project_id}/capability-manifest"


def _create_project(client, name: str) -> str:
    return client.post("/projects", json={"name": name}).json()["id"]


def _mcp_call(client, name, arguments):
    r = client.post("/mcp/sse", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    })
    assert r.status_code == 200
    return r.json()


def _mcp_result(resp):
    assert resp.get("result") is not None, resp
    return _json.loads(resp["result"]["content"][0]["text"])


# ---------------------------------------------------------------------------
# 1. Read path
# ---------------------------------------------------------------------------

def test_get_capability_manifest_fresh_project_returns_empty_profile(client):
    pid = _create_project(client, "rest-cap-fresh")
    r = client.get(_manifest_url(pid))
    assert r.status_code == 200
    body = r.json()
    assert body["project_id"] == pid
    assert body["capabilities"] == []
    assert body["manifest_hash"] == cm.manifest_hash([])


def test_get_capability_manifest_unknown_project_is_404(client):
    r = client.get(_manifest_url("no-such-project"))
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# 2. Write round trip + parity with the MCP tool surface
# ---------------------------------------------------------------------------

def test_put_capability_manifest_round_trip_is_canonical(client):
    pid = _create_project(client, "rest-cap-round-trip")
    payload = {"capabilities": [
        _valid_capability(id="zeta-search"),
        _valid_capability(id="alpha-search", availability_policy="degraded_ok",
                          fallback_chain=["grep"]),
    ]}
    r = client.put(_manifest_url(pid), json=payload)
    assert r.status_code == 200, r.text
    put_body = r.json()
    # Deterministic ordering: sorted by capability id regardless of input order.
    assert [c["id"] for c in put_body["capabilities"]] == ["alpha-search", "zeta-search"]
    expected_hash = cm.manifest_hash(cm.normalize_manifest(payload["capabilities"]))
    assert put_body["manifest_hash"] == expected_hash

    got = client.get(_manifest_url(pid)).json()
    assert got["capabilities"] == put_body["capabilities"]
    assert got["manifest_hash"] == expected_hash


def test_put_capability_manifest_replaces_wholesale_and_empty_clears(client):
    pid = _create_project(client, "rest-cap-replace")
    client.put(_manifest_url(pid), json={"capabilities": [_valid_capability()]})
    r = client.put(_manifest_url(pid), json={"capabilities": []})
    assert r.status_code == 200
    assert r.json()["capabilities"] == []
    assert client.get(_manifest_url(pid)).json()["capabilities"] == []


def test_rest_put_is_visible_to_mcp_get_capability_manifest(client):
    pid = _create_project(client, "rest-cap-mcp-parity")
    put_body = client.put(
        _manifest_url(pid), json={"capabilities": [_valid_capability()]}
    ).json()
    mcp_body = _mcp_result(_mcp_call(client, "get_capability_manifest", {"project_id": pid}))
    assert mcp_body["capabilities"] == put_body["capabilities"]
    assert mcp_body["manifest_hash"] == put_body["manifest_hash"]


def test_put_capability_manifest_unknown_project_is_404(client):
    r = client.put(_manifest_url("no-such-project"), json={"capabilities": []})
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# 3. Validation: deterministic rejection, never a partial write
# ---------------------------------------------------------------------------

def test_put_rejects_unknown_field_with_422_and_writes_nothing(client):
    pid = _create_project(client, "rest-cap-unknown-field")
    bad = _valid_capability(surprise="nope")
    r = client.put(_manifest_url(pid), json={"capabilities": [_valid_capability(), bad]})
    assert r.status_code == 422
    assert "surprise" in r.json()["detail"]
    # The good entry in the same request must not have been persisted either.
    assert client.get(_manifest_url(pid)).json()["capabilities"] == []


def test_put_rejects_duplicate_ids_with_422(client):
    pid = _create_project(client, "rest-cap-dupes")
    r = client.put(_manifest_url(pid), json={"capabilities": [
        _valid_capability(), _valid_capability(),
    ]})
    assert r.status_code == 422
    assert client.get(_manifest_url(pid)).json()["capabilities"] == []


def test_put_rejects_secret_shaped_value_without_echoing_it(client):
    pid = _create_project(client, "rest-cap-secret")
    secret = "postgresql://user:hunter2@host/db"
    r = client.put(_manifest_url(pid), json={"capabilities": [
        _valid_capability(provenance=secret),
    ]})
    assert r.status_code == 422
    assert "hunter2" not in r.text
    assert client.get(_manifest_url(pid)).json()["capabilities"] == []


def test_put_rejects_body_without_capabilities_list_with_400(client):
    pid = _create_project(client, "rest-cap-bad-body")
    r = client.put(_manifest_url(pid), json={"capabilities": "not-a-list"})
    assert r.status_code == 400
    r2 = client.put(_manifest_url(pid), json=["not", "an", "object"])
    assert r2.status_code == 400


def test_put_rejects_invalid_json_with_400(client):
    pid = _create_project(client, "rest-cap-invalid-json")
    r = client.put(
        _manifest_url(pid),
        content=b"{not json",
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# 4. Authorization: PERM_SETTINGS enforced in-route for workspace roles
# ---------------------------------------------------------------------------

def _fake_enforcement(role):
    async def _ctx(_request):
        return ("member@example.com", "tenant-under-test", role)
    return _ctx


def test_put_capability_manifest_forbidden_for_role_without_settings(client, monkeypatch):
    pid = _create_project(client, "rest-cap-member-denied")
    # 'member' has PERM_WRITE but not PERM_SETTINGS (see roles.py).
    monkeypatch.setattr(projects_routes, "_enforcement_context", _fake_enforcement("member"))
    r = client.put(_manifest_url(pid), json={"capabilities": [_valid_capability()]})
    assert r.status_code == 403
    monkeypatch.undo()
    assert client.get(_manifest_url(pid)).json()["capabilities"] == []


def test_put_capability_manifest_allowed_for_owner_role(client, monkeypatch):
    pid = _create_project(client, "rest-cap-owner-allowed")
    monkeypatch.setattr(projects_routes, "_enforcement_context", _fake_enforcement("owner"))
    r = client.put(_manifest_url(pid), json={"capabilities": [_valid_capability()]})
    assert r.status_code == 200
    assert [c["id"] for c in r.json()["capabilities"]] == ["code-search"]
