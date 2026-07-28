"""Tests for sprint item 649e095f — structured project capability manifest
foundation (deterministic-capability-handoffs, v0.2.5).

Covers:
1. meridian.capability_manifest — schema validation/normalization, secrets
   and machine-local absolute path rejection, deterministic ordering and
   content hash.
2. meridian.db — get/set_project_capability_manifest round trip, empty
   profile for a project with no persisted manifest, unknown-project error,
   SQLite/Postgres parity via the ``anydb`` fixture.
3. MCP tool surface — get_capability_manifest / set_capability_manifest
   registration and end-to-end dispatch (including deterministic rejection
   of malformed input).
"""
from __future__ import annotations

import json as _json

import pytest

from meridian import capability_manifest as cm
from meridian import db as db_module


# ---------------------------------------------------------------------------
# capability_manifest — schema validation/normalization (pure, no DB).
# ---------------------------------------------------------------------------

def _valid_capability(**overrides):
    base = {
        "id": "code-search",
        "purpose": "find symbols/functions/classes",
        "required_tools": ["Serena: find_symbol"],
    }
    base.update(overrides)
    return base


def test_normalize_capability_accepts_minimal_valid_entry():
    normalized = cm.normalize_capability(_valid_capability())
    assert normalized["id"] == "code-search"
    assert normalized["purpose"] == "find symbols/functions/classes"
    assert normalized["required_tools"] == ["Serena: find_symbol"]
    assert normalized["fallback_chain"] == []
    assert normalized["availability_policy"] == "required"
    assert normalized["verification_command"] is None
    assert normalized["provenance"] is None


def test_normalize_capability_accepts_full_entry():
    raw = _valid_capability(
        fallback_chain=["search_code_semantic", "grep"],
        availability_policy="degraded_ok",
        verification_command="pixi run test -k code_search",
        provenance={"source": "AGENTS.md", "section": "code intel"},
    )
    normalized = cm.normalize_capability(raw)
    assert normalized["fallback_chain"] == ["search_code_semantic", "grep"]
    assert normalized["availability_policy"] == "degraded_ok"
    assert normalized["verification_command"] == "pixi run test -k code_search"
    assert normalized["provenance"] == {"source": "AGENTS.md", "section": "code intel"}


@pytest.mark.parametrize("field", ["id", "purpose", "required_tools"])
def test_normalize_capability_rejects_missing_required_field(field):
    raw = _valid_capability()
    del raw[field]
    with pytest.raises(cm.CapabilityManifestError, match=field):
        cm.normalize_capability(raw)


def test_normalize_capability_rejects_unknown_field():
    raw = _valid_capability(unexpected_field="nope")
    with pytest.raises(cm.CapabilityManifestError, match="unknown capability field"):
        cm.normalize_capability(raw)


def test_normalize_capability_rejects_non_object():
    with pytest.raises(cm.CapabilityManifestError, match="must be an object"):
        cm.normalize_capability("not-a-dict")


def test_normalize_capability_rejects_empty_required_tools():
    with pytest.raises(cm.CapabilityManifestError, match="required_tools"):
        cm.normalize_capability(_valid_capability(required_tools=[]))


def test_normalize_capability_rejects_bad_availability_policy():
    with pytest.raises(cm.CapabilityManifestError, match="availability_policy"):
        cm.normalize_capability(_valid_capability(availability_policy="sometimes"))


def test_normalize_capability_rejects_secret_shaped_value():
    with pytest.raises(cm.CapabilityManifestError, match="secret-shaped"):
        cm.normalize_capability(
            _valid_capability(provenance="postgresql://user:hunter2@host/db")
        )


def test_normalize_capability_rejects_api_key_value():
    with pytest.raises(cm.CapabilityManifestError, match="secret-shaped"):
        cm.normalize_capability(_valid_capability(purpose="uses api_key: sk-abcdefghij1234"))


@pytest.mark.parametrize("bad_path", [
    r"C:\Users\adam\repo\tool.exe",
    "/home/adam/.local/bin/tool",
    "/Users/adam/repo/tool",
])
def test_normalize_capability_rejects_machine_local_absolute_path(bad_path):
    with pytest.raises(cm.CapabilityManifestError, match="machine-local absolute path"):
        cm.normalize_capability(_valid_capability(verification_command=bad_path))


def test_normalize_manifest_empty_and_none():
    assert cm.normalize_manifest(None) == []
    assert cm.normalize_manifest([]) == []


def test_normalize_manifest_rejects_non_list():
    with pytest.raises(cm.CapabilityManifestError, match="must be a list"):
        cm.normalize_manifest({"id": "x"})


def test_normalize_manifest_rejects_duplicate_ids():
    raw = [_valid_capability(id="dupe"), _valid_capability(id="dupe", purpose="other")]
    with pytest.raises(cm.CapabilityManifestError, match="duplicate capability id"):
        cm.normalize_manifest(raw)


def test_normalize_manifest_deterministic_ordering():
    raw_a = [_valid_capability(id="zebra"), _valid_capability(id="alpha")]
    raw_b = [_valid_capability(id="alpha"), _valid_capability(id="zebra")]
    norm_a = cm.normalize_manifest(raw_a)
    norm_b = cm.normalize_manifest(raw_b)
    assert [c["id"] for c in norm_a] == ["alpha", "zebra"]
    assert norm_a == norm_b


def test_manifest_hash_stable_across_input_order():
    raw_a = [_valid_capability(id="zebra"), _valid_capability(id="alpha")]
    raw_b = [_valid_capability(id="alpha"), _valid_capability(id="zebra")]
    hash_a = cm.manifest_hash(cm.normalize_manifest(raw_a))
    hash_b = cm.manifest_hash(cm.normalize_manifest(raw_b))
    assert hash_a == hash_b


def test_manifest_hash_changes_with_content():
    hash_empty = cm.manifest_hash(cm.normalize_manifest([]))
    hash_one = cm.manifest_hash(cm.normalize_manifest([_valid_capability()]))
    assert hash_empty != hash_one


def test_has_capability_manifest():
    assert cm.has_capability_manifest([]) is False
    assert cm.has_capability_manifest(None) is False
    assert cm.has_capability_manifest([_valid_capability()]) is True


# ---------------------------------------------------------------------------
# DB layer — get/set_project_capability_manifest.
# ---------------------------------------------------------------------------

async def test_get_project_capability_manifest_empty_profile_for_new_project(db):
    """Old/new projects with no persisted manifest get an empty profile back,
    never an error — 649e095f's explicit acceptance criterion."""
    project = await db_module.create_project(db, "cap-manifest-empty")
    result = await db_module.get_project_capability_manifest(db, project["id"])
    assert result["capabilities"] == []
    assert result["manifest_version"] == cm.MANIFEST_SCHEMA_VERSION
    assert result["updated_at"] is None
    assert result["manifest_hash"] == cm.manifest_hash([])


async def test_set_project_capability_manifest_round_trip(db):
    project = await db_module.create_project(db, "cap-manifest-round-trip")
    capabilities = [_valid_capability(id="b"), _valid_capability(id="a")]

    saved = await db_module.set_project_capability_manifest(db, project["id"], capabilities)
    assert [c["id"] for c in saved["capabilities"]] == ["a", "b"]
    assert saved["manifest_hash"] == cm.manifest_hash(cm.normalize_manifest(capabilities))
    assert saved["updated_at"] is not None

    fetched = await db_module.get_project_capability_manifest(db, project["id"])
    assert fetched["capabilities"] == saved["capabilities"]
    assert fetched["manifest_hash"] == saved["manifest_hash"]


async def test_set_project_capability_manifest_overwrites_wholesale(db):
    project = await db_module.create_project(db, "cap-manifest-overwrite")
    await db_module.set_project_capability_manifest(db, project["id"], [_valid_capability(id="first")])
    second = await db_module.set_project_capability_manifest(db, project["id"], [_valid_capability(id="second")])
    assert [c["id"] for c in second["capabilities"]] == ["second"]


async def test_set_project_capability_manifest_rejects_malformed_input(db):
    project = await db_module.create_project(db, "cap-manifest-malformed")
    with pytest.raises(cm.CapabilityManifestError):
        await db_module.set_project_capability_manifest(
            db, project["id"], [_valid_capability(availability_policy="bogus")]
        )
    # Rejected write must not partially persist.
    fetched = await db_module.get_project_capability_manifest(db, project["id"])
    assert fetched["capabilities"] == []


async def test_set_project_capability_manifest_unknown_project_raises(db):
    with pytest.raises(ValueError, match="unknown project"):
        await db_module.set_project_capability_manifest(db, "does-not-exist", [_valid_capability()])


async def test_capability_manifest_cross_backend_parity(anydb):
    """SQLite and Postgres persist and round-trip the manifest identically."""
    project = await db_module.create_project(anydb, "cap-manifest-parity")
    capabilities = [_valid_capability(id="parity-check")]
    saved = await db_module.set_project_capability_manifest(anydb, project["id"], capabilities)
    fetched = await db_module.get_project_capability_manifest(anydb, project["id"])
    assert fetched["capabilities"] == saved["capabilities"]
    assert fetched["manifest_hash"] == cm.manifest_hash(cm.normalize_manifest(capabilities))


# ---------------------------------------------------------------------------
# MCP tool surface — registration + end-to-end dispatch.
# ---------------------------------------------------------------------------

def test_capability_manifest_tools_registered():
    from meridian import mcp_tools

    names = {t["name"] for t in mcp_tools._MCP_TOOLS_LIST}
    assert "get_capability_manifest" in names
    assert "set_capability_manifest" in names
    assert mcp_tools._TOOL_CATEGORY["get_capability_manifest"] == "config"
    assert mcp_tools._TOOL_CATEGORY["set_capability_manifest"] == "config"


def _mcp_call(client, name, arguments):
    r = client.post("/mcp/sse", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    })
    assert r.status_code == 200
    return r.json()


def _result(resp):
    assert resp.get("result") is not None, resp
    return _json.loads(resp["result"]["content"][0]["text"])


def test_mcp_get_capability_manifest_empty_for_fresh_project(client):
    pid = client.post("/projects", json={"name": "mcp-cap-empty"}).json()["id"]
    result = _result(_mcp_call(client, "get_capability_manifest", {"project_id": pid}))
    assert result["capabilities"] == []
    assert "error" not in result


def test_mcp_set_then_get_capability_manifest_round_trip(client):
    pid = client.post("/projects", json={"name": "mcp-cap-round-trip"}).json()["id"]
    set_result = _result(_mcp_call(client, "set_capability_manifest", {
        "project_id": pid,
        "capabilities": [_valid_capability()],
    }))
    assert set_result["capabilities"][0]["id"] == "code-search"

    get_result = _result(_mcp_call(client, "get_capability_manifest", {"project_id": pid}))
    assert get_result["capabilities"] == set_result["capabilities"]
    assert get_result["manifest_hash"] == set_result["manifest_hash"]


def test_mcp_set_capability_manifest_rejects_malformed_input(client):
    pid = client.post("/projects", json={"name": "mcp-cap-malformed"}).json()["id"]
    result = _result(_mcp_call(client, "set_capability_manifest", {
        "project_id": pid,
        "capabilities": [{"id": "bad", "purpose": "missing required_tools"}],
    }))
    assert "error" in result
    assert "required_tools" in result["error"]


def test_mcp_set_capability_manifest_rejects_secret_shaped_value(client):
    pid = client.post("/projects", json={"name": "mcp-cap-secret"}).json()["id"]
    result = _result(_mcp_call(client, "set_capability_manifest", {
        "project_id": pid,
        "capabilities": [_valid_capability(provenance="postgresql://u:p@host/db")],
    }))
    assert "error" in result
    assert "secret-shaped" in result["error"]


def test_mcp_capability_manifest_requires_project_id(client):
    result = _result(_mcp_call(client, "get_capability_manifest", {}))
    assert "error" in result

    result2 = _result(_mcp_call(client, "set_capability_manifest", {"capabilities": []}))
    assert "error" in result2


def test_mcp_set_capability_manifest_requires_capabilities_field(client):
    pid = client.post("/projects", json={"name": "mcp-cap-required"}).json()["id"]
    result = _result(_mcp_call(client, "set_capability_manifest", {"project_id": pid}))
    assert "error" in result
