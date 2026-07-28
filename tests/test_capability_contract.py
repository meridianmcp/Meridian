"""Tests for sprint item 98aaccf4 — machine-readable effective capability
contract emitted in start_session and generate_handoff (v0.2.5).

Builds on 649e095f's schema/validation module (meridian.capability_manifest)
and its DB persistence (db.get_project_capability_manifest /
set_project_capability_manifest). Covers:

1. meridian.capability_contract — pure/DB-backed contract building: degraded
   defaults (no sibling integration present), resolver/checker injection
   (simulating the 02038afe/ac80aaaf sibling items once they land),
   executable=false conditions, deterministic serialization/hashing, and the
   secret-scrubbing defense-in-depth.
2. handoff._build_required_tool_clause — unchanged output after the
   extract_required_tool_pins refactor (backward compatibility).
3. MCP tool surface — capability_contract present in start_session's
   orientation and in every generate_handoff mode (full/delta/starter/goal).
4. HTTP surface — capability_contract present in POST /projects/{id}/handoff.
"""
from __future__ import annotations

import json as _json

import pytest

from meridian import capability_contract as cc
from meridian import capability_manifest as cm
from meridian import db as db_module
from meridian import handoff as handoff_module


def _valid_capability(**overrides):
    base = {
        "id": "code-search",
        "purpose": "find symbols/functions/classes",
        "required_tools": ["Serena: find_symbol"],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# capability_contract.build_capability_contract — degraded defaults (ONLY
# 649e095f present, no sibling integration).
# ---------------------------------------------------------------------------

async def test_contract_empty_manifest_degrades_cleanly(db):
    project = await db_module.create_project(db, "cap-contract-empty")
    contract = await cc.build_capability_contract(db, project["id"])

    assert contract["schema_version"] == cc.CONTRACT_SCHEMA_VERSION
    assert contract["project_id"] == project["id"]
    assert contract["requested"]["capabilities"] == []
    assert contract["effective"]["capabilities"] == []
    assert contract["effective"]["source"] == "raw_manifest"
    assert contract["availability"]["status"] == "unknown"
    assert contract["availability"]["available"] == "unknown"
    assert contract["availability"]["missing"] == "unknown"
    assert contract["availability"]["degraded"] == "unknown"
    assert contract["manifest_hash"] == cm.manifest_hash([])
    assert contract["board_stale"] is False
    assert contract["executable"] is True
    assert contract["executable_reasons"] == []
    assert "generated_at" in contract
    assert "contract_hash" in contract


async def test_contract_requested_mirrors_persisted_manifest(db):
    project = await db_module.create_project(db, "cap-contract-requested")
    caps = [_valid_capability(id="alpha"), _valid_capability(id="beta")]
    saved = await db_module.set_project_capability_manifest(db, project["id"], caps)

    contract = await cc.build_capability_contract(db, project["id"])
    assert contract["requested"]["capabilities"] == saved["capabilities"]
    assert contract["requested"]["manifest_hash"] == saved["manifest_hash"]
    # No sibling profile-inheritance integration present -> effective == requested.
    assert contract["effective"]["capabilities"] == saved["capabilities"]
    assert contract["effective"]["source"] == "raw_manifest"
    assert contract["manifest_hash"] == cm.manifest_hash(saved["capabilities"])


async def test_contract_unknown_availability_never_forces_non_executable(db):
    """A 'required' capability with unresolved (unknown) availability must NOT
    be treated as missing — that would incorrectly flip executable=false on
    every project until the ac80aaaf sibling lands."""
    project = await db_module.create_project(db, "cap-contract-unknown-avail")
    await db_module.set_project_capability_manifest(
        db, project["id"], [_valid_capability(availability_policy="required")]
    )
    contract = await cc.build_capability_contract(db, project["id"])
    assert contract["availability"]["status"] == "unknown"
    assert contract["executable"] is True
    assert contract["executable_reasons"] == []


# ---------------------------------------------------------------------------
# Sibling-integration upgrade path (injected resolver/checker simulate
# 02038afe / ac80aaaf landing later).
# ---------------------------------------------------------------------------

async def test_contract_effective_resolver_upgrades_effective_section(db):
    project = await db_module.create_project(db, "cap-contract-resolver")
    requested = [_valid_capability(id="alpha")]
    await db_module.set_project_capability_manifest(db, project["id"], requested)

    injected = [_valid_capability(id="alpha"), _valid_capability(id="inherited")]

    def _fake_resolver(_db, _project_id, _requested):
        return injected

    contract = await cc.build_capability_contract(
        db, project["id"], effective_resolver=_fake_resolver,
    )
    assert contract["effective"]["source"] == "resolver"
    assert [c["id"] for c in contract["effective"]["capabilities"]] == ["alpha", "inherited"]
    # requested is untouched by the resolver.
    assert [c["id"] for c in contract["requested"]["capabilities"]] == ["alpha"]


async def test_contract_effective_resolver_exception_degrades_to_raw_manifest(db):
    project = await db_module.create_project(db, "cap-contract-resolver-crash")
    saved = await db_module.set_project_capability_manifest(
        db, project["id"], [_valid_capability()]
    )

    def _bad_resolver(_db, _project_id, _requested):
        raise RuntimeError("sibling module half-built")

    contract = await cc.build_capability_contract(
        db, project["id"], effective_resolver=_bad_resolver,
    )
    assert contract["effective"]["source"] == "raw_manifest"
    # Degrades to the normalized (already-persisted) manifest, not whatever
    # pre-normalization shape was originally passed to set_capability_manifest.
    assert contract["effective"]["capabilities"] == saved["capabilities"]


async def test_contract_availability_checker_flags_missing_required(db):
    project = await db_module.create_project(db, "cap-contract-avail-missing")
    await db_module.set_project_capability_manifest(
        db, project["id"],
        [_valid_capability(id="alpha", availability_policy="required")],
    )

    def _fake_checker(_capabilities):
        return {"available": [], "missing": ["alpha"], "degraded": []}

    contract = await cc.build_capability_contract(
        db, project["id"], availability_checker=_fake_checker,
    )
    assert contract["availability"]["status"] == "checked"
    assert contract["availability"]["missing"] == ["alpha"]
    assert contract["executable"] is False
    assert any("missing_required_capabilities" in r for r in contract["executable_reasons"])
    assert "alpha" in contract["executable_reasons"][0]


async def test_contract_availability_checker_missing_optional_stays_executable(db):
    project = await db_module.create_project(db, "cap-contract-avail-optional")
    await db_module.set_project_capability_manifest(
        db, project["id"],
        [_valid_capability(id="alpha", availability_policy="optional")],
    )

    def _fake_checker(_capabilities):
        return {"available": [], "missing": ["alpha"], "degraded": []}

    contract = await cc.build_capability_contract(
        db, project["id"], availability_checker=_fake_checker,
    )
    assert contract["availability"]["missing"] == ["alpha"]
    # 'alpha' is optional, not required -> does not block executability.
    assert contract["executable"] is True
    assert contract["executable_reasons"] == []


async def test_contract_availability_checker_exception_degrades_to_unknown(db):
    project = await db_module.create_project(db, "cap-contract-avail-crash")
    await db_module.set_project_capability_manifest(db, project["id"], [_valid_capability()])

    def _bad_checker(_capabilities):
        raise RuntimeError("sibling module half-built")

    contract = await cc.build_capability_contract(
        db, project["id"], availability_checker=_bad_checker,
    )
    assert contract["availability"]["status"] == "unknown"
    assert contract["executable"] is True


# ---------------------------------------------------------------------------
# board_stale -> executable=false
# ---------------------------------------------------------------------------

async def test_contract_board_stale_forces_non_executable(db):
    project = await db_module.create_project(db, "cap-contract-stale")
    contract = await cc.build_capability_contract(db, project["id"], board_stale=True)
    assert contract["board_stale"] is True
    assert contract["executable"] is False
    assert "stale_board_snapshot" in contract["executable_reasons"]


async def test_contract_both_missing_required_and_stale_reports_both_reasons(db):
    project = await db_module.create_project(db, "cap-contract-both")
    await db_module.set_project_capability_manifest(
        db, project["id"],
        [_valid_capability(id="alpha", availability_policy="required")],
    )

    def _fake_checker(_capabilities):
        return {"available": [], "missing": ["alpha"], "degraded": []}

    contract = await cc.build_capability_contract(
        db, project["id"], board_stale=True, availability_checker=_fake_checker,
    )
    assert contract["executable"] is False
    assert len(contract["executable_reasons"]) == 2


# ---------------------------------------------------------------------------
# Deterministic serialization / hashing.
# ---------------------------------------------------------------------------

async def test_contract_serialize_is_byte_stable_for_same_state(db):
    project = await db_module.create_project(db, "cap-contract-stable")
    await db_module.set_project_capability_manifest(db, project["id"], [_valid_capability()])

    contract_a = await cc.build_capability_contract(db, project["id"])
    contract_b = await cc.build_capability_contract(db, project["id"])
    # generated_at will differ (real wall-clock calls) but the canonical
    # serialization/hash must be identical for identical underlying state.
    assert cc.serialize_contract(contract_a) == cc.serialize_contract(contract_b)
    assert contract_a["contract_hash"] == contract_b["contract_hash"]


async def test_contract_hash_changes_when_capability_changes(db):
    project = await db_module.create_project(db, "cap-contract-hash-cap-change")
    await db_module.set_project_capability_manifest(db, project["id"], [_valid_capability(id="a")])
    contract_a = await cc.build_capability_contract(db, project["id"])

    await db_module.set_project_capability_manifest(db, project["id"], [_valid_capability(id="b")])
    contract_b = await cc.build_capability_contract(db, project["id"])

    assert contract_a["contract_hash"] != contract_b["contract_hash"]
    assert contract_a["manifest_hash"] != contract_b["manifest_hash"]


async def test_contract_hash_changes_when_tunnel_availability_state_changes(db):
    """A changed availability/tunnel-state result must change the contract hash
    even though the underlying capability manifest is untouched."""
    project = await db_module.create_project(db, "cap-contract-hash-tunnel-change")
    await db_module.set_project_capability_manifest(
        db, project["id"], [_valid_capability(id="alpha", availability_policy="optional")]
    )

    def _checker_all_available(_capabilities):
        return {"available": ["alpha"], "missing": [], "degraded": []}

    def _checker_now_degraded(_capabilities):
        return {"available": [], "missing": [], "degraded": ["alpha"]}

    contract_before = await cc.build_capability_contract(
        db, project["id"], availability_checker=_checker_all_available,
    )
    contract_after = await cc.build_capability_contract(
        db, project["id"], availability_checker=_checker_now_degraded,
    )
    assert contract_before["contract_hash"] != contract_after["contract_hash"]
    # The manifest itself never changed.
    assert contract_before["manifest_hash"] == contract_after["manifest_hash"]


def test_serialize_contract_excludes_generated_at_and_contract_hash():
    contract = {
        "schema_version": 1, "project_id": "p", "requested": {"capabilities": []},
        "effective": {"capabilities": [], "source": "raw_manifest"},
        "availability": {"status": "unknown", "available": "unknown", "missing": "unknown", "degraded": "unknown"},
        "manifest_hash": cm.manifest_hash([]), "board_stale": False,
        "executable": True, "executable_reasons": [],
        "generated_at": "2026-01-01T00:00:00+00:00",
        "contract_hash": "should-not-appear",
    }
    serialized = cc.serialize_contract(contract)
    assert "2026-01-01T00:00:00" not in serialized
    assert "should-not-appear" not in serialized
    parsed = _json.loads(serialized)
    assert "generated_at" not in parsed
    assert "contract_hash" not in parsed


# ---------------------------------------------------------------------------
# Never bind credentials — secret-scrubbing defense-in-depth.
# ---------------------------------------------------------------------------

async def test_contract_scrubs_secret_shaped_value_from_injected_effective(db):
    project = await db_module.create_project(db, "cap-contract-secret-effective")
    await db_module.set_project_capability_manifest(db, project["id"], [_valid_capability()])

    def _leaky_resolver(_db, _project_id, _requested):
        return [
            _valid_capability(
                id="leaky",
                provenance="postgresql://user:hunter2@host/db",
            )
        ]

    contract = await cc.build_capability_contract(
        db, project["id"], effective_resolver=_leaky_resolver,
    )
    assert contract["effective"]["capabilities"] == []
    assert contract["effective"]["source"] == "redacted_secret_shaped_value"
    assert contract["executable"] is False
    assert "redacted_secret_shaped_value" in contract["executable_reasons"]
    serialized = cc.serialize_contract(contract)
    assert "hunter2" not in serialized
    assert "postgresql://" not in serialized


async def test_contract_scrubs_secret_shaped_value_from_injected_availability(db):
    """A secret-shaped string smuggled INTO one of the actual available/
    missing/degraded id lists (the only fields build_capability_contract
    copies out of a checker's return value) must be scrubbed. An extra field
    a checker returns outside that schema (e.g. a stray 'note') is simply
    never copied into the contract at all -- so it can't leak either, but
    for a different reason (dropped, not scrubbed) than the case tested
    here."""
    project = await db_module.create_project(db, "cap-contract-secret-availability")
    await db_module.set_project_capability_manifest(db, project["id"], [_valid_capability()])

    def _leaky_checker(_capabilities):
        return {
            "available": [],
            "missing": ["bearer sk-abcdefghij1234567890"],
            "degraded": [],
        }

    contract = await cc.build_capability_contract(
        db, project["id"], availability_checker=_leaky_checker,
    )
    serialized = cc.serialize_contract(contract)
    assert "sk-abcdefghij1234567890" not in serialized
    assert contract["availability"]["status"] == "unknown"
    assert contract["availability"]["missing"] == "unknown"


async def test_contract_no_secrets_in_normal_operation(db):
    """No secrets/credentials appear anywhere in the emitted contract under
    normal operation (no sibling injection at all) — 649e095f's own write-time
    validation already guarantees this for `requested`."""
    project = await db_module.create_project(db, "cap-contract-no-secrets")
    await db_module.set_project_capability_manifest(
        db, project["id"],
        [_valid_capability(provenance={"source": "AGENTS.md", "section": "code intel"})],
    )
    contract = await cc.build_capability_contract(db, project["id"])
    serialized = cc.serialize_contract(contract)
    assert "hunter2" not in serialized
    assert "bearer" not in serialized.lower()
    assert "postgresql://" not in serialized
    assert "sk-" not in serialized


# ---------------------------------------------------------------------------
# extract_required_tool_pins — typed data shared with handoff's rendering.
# ---------------------------------------------------------------------------

def test_extract_required_tool_pins_pure_data():
    items = [
        {"id": "item1", "required_tool": "Serena: find_symbol"},
        {"id": "item2"},  # no required_tool
        {"required_tool": "Serena: find_symbol"},  # no id
    ]
    pins = cc.extract_required_tool_pins(items)
    assert pins == [{"item_id": "item1", "tool": "Serena: find_symbol"}]


def test_build_required_tool_clause_unchanged_after_refactor():
    """Backward compatibility: handoff._build_required_tool_clause must render
    byte-identical output after delegating extraction to
    capability_contract.extract_required_tool_pins (98aaccf4)."""
    items = [
        {"id": "aaa111", "required_tool": "Serena: replace_symbol_body"},
        {"id": "bbb222", "required_tool": "meridian__patch_file"},
    ]
    out = handoff_module._build_required_tool_clause(items)
    assert out.startswith("\n<required_tool>")
    assert out.endswith("</required_tool>")
    assert "aaa111: Serena: replace_symbol_body" in out
    assert "bbb222: meridian__patch_file" in out
    assert "hard requirement" in out
    assert "not a suggestion" in out
    assert handoff_module._build_required_tool_clause([]) == ""


# ---------------------------------------------------------------------------
# handoff.build_effective_capability_contract — the guarded wrapper both MCP
# call sites use.
# ---------------------------------------------------------------------------

async def test_build_effective_capability_contract_wrapper(db):
    project = await db_module.create_project(db, "cap-contract-wrapper")
    contract = await handoff_module.build_effective_capability_contract(db, project["id"])
    assert contract is not None
    assert contract["project_id"] == project["id"]


async def test_build_effective_capability_contract_wrapper_never_raises(db, monkeypatch):
    from meridian import capability_contract as _cc_mod

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(_cc_mod, "build_capability_contract", _boom)
    project = await db_module.create_project(db, "cap-contract-wrapper-boom")
    contract = await handoff_module.build_effective_capability_contract(db, project["id"])
    assert contract is None


# ---------------------------------------------------------------------------
# MCP tool surface.
# ---------------------------------------------------------------------------

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


def test_mcp_start_session_includes_capability_contract(client):
    pid = client.post("/projects", json={"name": "mcp-cap-contract-start"}).json()["id"]
    result = _result(_mcp_call(client, "start_session", {
        "project_id": pid, "session_name": "cap-contract-session",
    }))
    assert "capability_contract" in result
    contract = result["capability_contract"]
    assert contract is not None
    assert contract["project_id"] == pid
    assert "requested" in contract
    assert "effective" in contract
    assert "availability" in contract
    assert "executable" in contract
    assert "generated_at" in contract


@pytest.mark.parametrize("mode", ["full", "delta", "starter", "goal"])
def test_mcp_generate_handoff_includes_capability_contract_all_modes(client, mode):
    pid = client.post("/projects", json={"name": f"mcp-cap-contract-{mode}"}).json()["id"]
    sess = _result(_mcp_call(client, "start_session", {
        "project_id": pid, "session_name": f"cap-contract-{mode}",
    }))
    session_id = sess.get("session_id")
    result = _result(_mcp_call(client, "generate_handoff", {
        "project_id": pid, "mode": mode, "session_id": session_id,
    }))
    assert "capability_contract" in result
    contract = result["capability_contract"]
    assert contract is not None
    assert contract["project_id"] == pid
    assert contract["schema_version"] == cc.CONTRACT_SCHEMA_VERSION


def test_mcp_generate_handoff_capability_contract_no_secrets(client):
    pid = client.post("/projects", json={"name": "mcp-cap-contract-secrets"}).json()["id"]
    _result(_mcp_call(client, "set_capability_manifest", {
        "project_id": pid,
        "capabilities": [_valid_capability(provenance={"source": "AGENTS.md"})],
    }))
    result = _result(_mcp_call(client, "generate_handoff", {
        "project_id": pid, "mode": "goal",
    }))
    contract = result["capability_contract"]
    serialized = _json.dumps(contract)
    assert "hunter2" not in serialized
    assert "postgresql://" not in serialized


# ---------------------------------------------------------------------------
# HTTP surface.
# ---------------------------------------------------------------------------

def test_http_handoff_endpoint_includes_capability_contract(client):
    pid = client.post("/projects", json={"name": "http-cap-contract"}).json()["id"]
    r = client.post(f"/projects/{pid}/handoff")
    assert r.status_code == 200
    body = r.json()
    assert "capability_contract" in body
    assert body["capability_contract"]["project_id"] == pid


def test_http_planner_handoff_includes_capability_contract(client):
    pid = client.post("/projects", json={"name": "http-cap-contract-planner"}).json()["id"]
    r = client.get(f"/projects/{pid}/handoff/planner")
    assert r.status_code == 200
    body = r.json()
    assert "capability_contract" in body
    assert body["capability_contract"]["project_id"] == pid
