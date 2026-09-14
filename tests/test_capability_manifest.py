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


def test_manifest_hash_stable_across_larger_shuffle():
    """More than two entries, reverse order — the sort-by-id step in
    normalize_manifest must make this deterministic regardless of set size."""
    ids = ["delta", "alpha", "charlie", "bravo", "echo"]
    raw_forward = [_valid_capability(id=i) for i in ids]
    raw_reversed = [_valid_capability(id=i) for i in reversed(ids)]
    hash_forward = cm.manifest_hash(cm.normalize_manifest(raw_forward))
    hash_reversed = cm.manifest_hash(cm.normalize_manifest(raw_reversed))
    assert hash_forward == hash_reversed


@pytest.mark.parametrize("overrides_a,overrides_b", [
    ({"fallback_chain": ["grep"]}, {"fallback_chain": ["ripgrep"]}),
    ({"availability_policy": "required"}, {"availability_policy": "optional"}),
    ({"verification_command": "pixi run test -k x"}, {}),
    ({"provenance": {"source": "AGENTS.md"}}, {"provenance": {"source": "README.md"}}),
])
def test_manifest_hash_sensitive_to_each_optional_field(overrides_a, overrides_b):
    """The hash must change if any single optional field differs -- a hash
    that only reflected id/purpose/required_tools would silently mask drift
    in fallback/availability/provenance, defeating its use as a change
    detector for handoffs and caching."""
    hash_a = cm.manifest_hash(cm.normalize_manifest([_valid_capability(**overrides_a)]))
    hash_b = cm.manifest_hash(cm.normalize_manifest([_valid_capability(**overrides_b)]))
    assert hash_a != hash_b


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


async def test_get_project_capability_manifest_remembered_without_reset(db):
    """Acceptance criterion: once a manifest is set, repeated get calls must
    reflect it without the caller needing to set it again -- a capability
    profile is a project default, not a per-call prompt to be re-supplied."""
    project = await db_module.create_project(db, "cap-manifest-remembered")
    await db_module.set_project_capability_manifest(db, project["id"], [_valid_capability()])

    first = await db_module.get_project_capability_manifest(db, project["id"])
    second = await db_module.get_project_capability_manifest(db, project["id"])
    third = await db_module.get_project_capability_manifest(db, project["id"])
    assert first == second == third
    assert first["capabilities"][0]["id"] == "code-search"


def _assert_no_secret_or_local_path(value, *, context):
    """Defense-in-depth recursive scan: nothing reachable from a persisted
    manifest may contain a secret-shaped string or machine-local absolute
    path, even if some future change let one slip past normalize_manifest
    before it reached storage. Reuses capability_manifest's own patterns so
    this stays in lockstep with the real validation rules."""
    if isinstance(value, str):
        assert not cm._SECRET_LIKE_RE.search(value), (
            f"{context}: secret-shaped value leaked through storage: {value!r}"
        )
        assert not cm._ABSOLUTE_PATH_RE.search(value), (
            f"{context}: machine-local absolute path leaked through storage: {value!r}"
        )
    elif isinstance(value, dict):
        for key, sub in value.items():
            _assert_no_secret_or_local_path(sub, context=f"{context}.{key}")
    elif isinstance(value, (list, tuple)):
        for idx, sub in enumerate(value):
            _assert_no_secret_or_local_path(sub, context=f"{context}[{idx}]")


async def test_get_project_capability_manifest_no_secret_leakage_defense_in_depth(db):
    """No field of a fetched manifest may ever contain a raw secret pattern
    or machine-local path -- a defense-in-depth net around normalize_manifest,
    not a replacement for it (649e095f already rejects these at write time)."""
    project = await db_module.create_project(db, "cap-manifest-no-leak")
    capabilities = [_valid_capability(
        fallback_chain=["grep", "search_code_semantic"],
        provenance={"source": "AGENTS.md", "section": "code intel"},
        verification_command="pixi run test -k code_search",
    )]
    saved = await db_module.set_project_capability_manifest(db, project["id"], capabilities)
    _assert_no_secret_or_local_path(saved["capabilities"], context="saved")

    fetched = await db_module.get_project_capability_manifest(db, project["id"])
    _assert_no_secret_or_local_path(fetched["capabilities"], context="fetched")


async def test_set_project_capability_manifest_error_does_not_echo_secret_value(db):
    """Rejection must not leak the offending secret back verbatim in the
    error message -- the caller learns *that* a field was secret-shaped,
    never the value itself."""
    project = await db_module.create_project(db, "cap-manifest-no-echo")
    secret = "postgresql://user:hunter2@host/db"
    with pytest.raises(cm.CapabilityManifestError) as excinfo:
        await db_module.set_project_capability_manifest(
            db, project["id"], [_valid_capability(provenance=secret)]
        )
    assert "hunter2" not in str(excinfo.value)


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


def test_mcp_get_capability_manifest_repeated_calls_remember_without_reset(client):
    """Same acceptance criterion as the DB-level test, exercised through the
    actual MCP surface an executor calls: a second get_capability_manifest
    reflects a prior set without needing to set_capability_manifest again."""
    pid = client.post("/projects", json={"name": "mcp-cap-remembered"}).json()["id"]
    _mcp_call(client, "set_capability_manifest", {
        "project_id": pid,
        "capabilities": [_valid_capability()],
    })

    first = _result(_mcp_call(client, "get_capability_manifest", {"project_id": pid}))
    second = _result(_mcp_call(client, "get_capability_manifest", {"project_id": pid}))
    assert first == second
    assert first["capabilities"][0]["id"] == "code-search"


def test_mcp_set_capability_manifest_rejection_does_not_echo_secret_value(client):
    """Same no-echo guarantee as the DB-level test, checked at the MCP
    response boundary since that's what an executor session actually sees."""
    pid = client.post("/projects", json={"name": "mcp-cap-no-echo"}).json()["id"]
    result = _result(_mcp_call(client, "set_capability_manifest", {
        "project_id": pid,
        "capabilities": [_valid_capability(provenance="postgresql://u:hunter2@host/db")],
    }))
    assert "error" in result
    assert "hunter2" not in result["error"]


# ---------------------------------------------------------------------------
# 45049071 — openai_tunnel_adapter.default_capability_entry() is a REAL
# consumer of this module's public schema (normalize_capability /
# normalize_manifest / manifest_hash); capability_manifest.py itself needed
# no code changes for that feature (see docs/secure-openai-mcp-tunnel-
# adapter.md) — these tests are the seam-level proof that consuming this
# module's existing, generic API is sufficient.
# ---------------------------------------------------------------------------

def test_openai_tunnel_capability_entry_is_a_valid_manifest_entry():
    from meridian import openai_tunnel_adapter as ota

    entry = ota.default_capability_entry()
    # default_capability_entry() already normalizes internally; re-running
    # normalize_capability must be a byte-identical no-op.
    assert cm.normalize_capability(entry) == entry
    assert entry["availability_policy"] == "optional"


def test_openai_tunnel_capability_entry_round_trips_via_mcp_set_get(client):
    from meridian import openai_tunnel_adapter as ota

    pid = client.post("/projects", json={"name": "mcp-cap-openai-tunnel"}).json()["id"]
    entry = ota.default_capability_entry()
    set_result = _result(_mcp_call(client, "set_capability_manifest", {
        "project_id": pid, "capabilities": [entry],
    }))
    assert "error" not in set_result

    get_result = _result(_mcp_call(client, "get_capability_manifest", {"project_id": pid}))
    ids = [c["id"] for c in get_result["capabilities"]]
    assert ota.OPENAI_TUNNEL_CAPABILITY_ID in ids


# ---------------------------------------------------------------------------
# 74c591b6 (RESEARCH-OS WAVE B) — reconcile raw manifest PERSISTENCE (this
# module, already correct) with EFFECTIVE CONTRACT RESOLUTION: before this
# fix, db.get_effective_capability_profile (and the MCP tool it backs) knew
# nothing about the project_capabilities table at all -- a project that had
# only ever called set_capability_manifest got "capabilities: [],
# layers_applied: []" back from get_effective_capability_profile even though
# the SAME manifest was already correctly folded into start_session's
# capability_contract (via capability_contract._resolve_effective_
# capabilities' own, independent reconciliation). Confirmed live against the
# real meridian-build project (5787cc92-ba7d-4788-b17c-28ab7938b839) during
# this item's investigation: get_capability_manifest returned four populated
# local-first capabilities while get_effective_capability_profile reported
# layers_applied: [] for the identical project.
# ---------------------------------------------------------------------------

def _local_first_capabilities():
    """The four RESEARCH-OS WAVE A capabilities as actually persisted on the
    live meridian-build project at the time this item was worked (manifest
    hash dbaa1f5b7c43b936eeab624740c5283e5ca8424237f90137e5b3060c17ae9c18,
    re-verified current via get_capability_manifest during this item -- the
    hash recorded in the item's own notes, 4b3bd53b..., had already gone
    stale by the time this item was claimed)."""
    return [
        {
            "id": "code_prospecting",
            "purpose": "Structural code investigation with a local semantic/BM25 fallback when graph or Serena access is unavailable.",
            "required_tools": ["codebase-memory/Serena"],
            "fallback_chain": ["search_code_semantic", "search_code"],
            "availability_policy": "degraded_ok",
        },
        {
            "id": "outputs_provenance",
            "purpose": "Output discovery, provenance, and artifact identity checks for research results.",
            "required_tools": ["meridian-outputs"],
            "fallback_chain": ["local_outputs"],
            "availability_policy": "degraded_ok",
        },
        {
            "id": "research_evidence_envelope",
            "purpose": "Lossless research evidence and provenance exchange for claims, citations, artifacts, and handoffs.",
            "required_tools": ["meridian-outputs:research_evidence"],
            "fallback_chain": ["local-json-xml"],
            "availability_policy": "degraded_ok",
        },
        {
            "id": "research_retrieval",
            "purpose": "Bounded academic and prior-art retrieval for research planning and evidence capture; optional for implementation-only waves.",
            "required_tools": ["paper_search"],
            "fallback_chain": ["github_search", "social_search"],
            "availability_policy": "degraded_ok",
        },
    ]


async def test_get_effective_capability_profile_reports_raw_manifest_as_applied_layer(db):
    """The core reconciliation fix: after set_project_capability_manifest,
    db.get_effective_capability_profile must report the manifest's
    capabilities as applied -- not the pre-fix empty result."""
    project = await db_module.create_project(db, "cap-reconcile-raw-manifest")
    saved = await db_module.set_project_capability_manifest(
        db, project["id"], _local_first_capabilities(),
    )

    effective = await db_module.get_effective_capability_profile(db, project["id"])

    assert effective["layers_applied"] == ["raw_manifest"]
    assert [c["id"] for c in effective["capabilities"]] == [
        "code_prospecting", "outputs_provenance",
        "research_evidence_envelope", "research_retrieval",
    ]
    assert effective["capabilities"] == saved["capabilities"]
    assert effective["capability_sources"] == {
        "code_prospecting": "raw_manifest",
        "outputs_provenance": "raw_manifest",
        "research_evidence_envelope": "raw_manifest",
        "research_retrieval": "raw_manifest",
    }
    # No capability_profiles-table layer was ever set for this project --
    # nothing to override, so no overrides/disables are recorded.
    assert effective["overrides"] == []
    assert effective["disabled"] == []


async def test_get_effective_capability_profile_empty_manifest_reports_no_layers(db):
    """Negative case: a project with NO raw manifest and no profile layers
    still resolves to the exact pre-fix empty result (byte-identical
    non-regression for the common "nothing declared yet" case)."""
    project = await db_module.create_project(db, "cap-reconcile-empty")
    effective = await db_module.get_effective_capability_profile(db, project["id"])
    assert effective["capabilities"] == []
    assert effective["layers_applied"] == []


async def test_get_effective_capability_profile_real_profile_layer_still_overrides_raw_manifest(db):
    """A genuine capability_profiles-table layer (set_capability_profile) is
    MORE specific than the raw manifest and must still win -- the raw
    manifest fix must not make it un-overridable."""
    project = await db_module.create_project(db, "cap-reconcile-override")
    await db_module.set_project_capability_manifest(
        db, project["id"], [_valid_capability(id="research_retrieval", required_tools=["paper_search"])],
    )
    await db_module.set_capability_profile(
        db, "project", project["id"],
        capabilities=[_valid_capability(id="research_retrieval", required_tools=["OVERRIDDEN"])],
    )

    effective = await db_module.get_effective_capability_profile(db, project["id"])
    assert effective["layers_applied"] == ["raw_manifest", "project"]
    assert effective["capabilities"][0]["required_tools"] == ["OVERRIDDEN"]
    assert effective["capability_sources"]["research_retrieval"] == "project"


def test_mcp_get_effective_capability_profile_reflects_raw_manifest_after_set_capability_manifest(client):
    """End-to-end MCP surface: the exact sequence the live meridian-build
    project exercised (set_capability_manifest, then
    get_effective_capability_profile) must agree, closing the gap this
    item's own preflight notes reported."""
    pid = client.post("/projects", json={"name": "mcp-cap-reconcile"}).json()["id"]
    set_result = _result(_mcp_call(client, "set_capability_manifest", {
        "project_id": pid, "capabilities": _local_first_capabilities(),
    }))
    assert "error" not in set_result

    effective = _result(_mcp_call(client, "get_effective_capability_profile", {"project_id": pid}))
    assert "raw_manifest" in effective["layers_applied"]
    assert {c["id"] for c in effective["capabilities"]} == {
        "code_prospecting", "outputs_provenance",
        "research_evidence_envelope", "research_retrieval",
    }


async def test_capability_contract_effective_still_agrees_with_get_effective_capability_profile(db):
    """The two consumers this item's notes flagged as disagreeing --
    capability_contract.build_capability_contract's own "effective" section
    and the standalone get_effective_capability_profile call -- must now
    report the SAME capability set for the SAME project state (they may
    still legitimately disagree on the `source` label, since the contract's
    "raw_manifest" vs "profile_inheritance" distinction is about which layer
    contributed, not about the resolved content)."""
    from meridian import capability_contract as cc

    project = await db_module.create_project(db, "cap-reconcile-parity")
    await db_module.set_project_capability_manifest(db, project["id"], _local_first_capabilities())

    effective_profile = await db_module.get_effective_capability_profile(db, project["id"])
    contract = await cc.build_capability_contract(db, project["id"])

    assert (
        [c["id"] for c in effective_profile["capabilities"]]
        == [c["id"] for c in contract["effective"]["capabilities"]]
    )
    assert contract["effective"]["source"] == "raw_manifest"
    # All four capabilities are degraded_ok -- none block executability even
    # with availability unresolved (no tenant/live_inventory in this pure
    # build_capability_contract call).
    assert contract["executable"] is True
    assert contract["executable_reasons"] == []
