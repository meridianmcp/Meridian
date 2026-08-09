"""Tests for sprint item 1e537ed1 (PROFILE-8) -- the production-readiness
gate for the profile-layers subsystem shipped across PROFILE-1..7
(profile_contract.py, db/profile_layers.py, profile_cache.py,
routes/settings.py, the identity/generation binding into
start_session/handoff/tunnel, and the profile-aware batch_read/batch_mutate
adapters).

This is a CONTRACT MATRIX, not a from-scratch test suite: every line item
below already has substantial coverage in tests/test_profile_layers.py,
tests/test_profile_contract.py, tests/test_8d52b620_profile_cache.py,
tests/test_project_settings.py, tests/test_batch_read_mutate_133bfff6.py,
tests/test_handoff_executor_planner_lifecycle.py, tests/test_tunnel_routes.py,
and tests/test_capability_contract.py. This file adds ONLY what those files
do not already prove: integrated CHAINS across multiple handlers/surfaces,
cross-surface consistency checks (does surface A agree with surface B?), an
exhaustive lifecycle truth table, and -- the item's own hard requirement --
MEASURED (not assumed) evidence for the cache's Neon-avoidance claim. Every
test below either says in its docstring which existing test it deliberately
does NOT duplicate, or is new integration coverage across items PROFILE-5/6/7
built in separate sessions and never previously cross-checked against each
other.

-------------------------------------------------------------------------
NOT VERIFIED IN THIS SESSION -- read before trusting any pass/fail below
-------------------------------------------------------------------------
This is a local, non-production, non-authenticated test environment. The
following are explicitly OUT OF SCOPE for anything in this file and are
NOT proven by it, regardless of how confidently a docstring below talks
about "Redis" or "Neon":

  * A REAL production Redis instance's hit-rate or latency. Every
    Redis-shaped test in this file runs against an in-memory fake client
    (``_FakeRedisClient``, mirroring test_8d52b620_profile_cache.py's own
    established fake), never a live Redis connection.
  * A REAL Neon production database's query load. This dev environment is
    SQLite. The "measured Neon avoidance" test below counts real Python
    call-invocations of ``db.get_effective_profile`` (the same function a
    production deployment would call against Neon) -- a genuine,
    measured CALL-COUNT metric, but not a measurement taken against an
    actual Neon deployment or its real query planner/latency.
  * Any authenticated hosted usemeridian.us manifest/cache/generation
    refresh observed via ``mcp-debugger`` or otherwise. This session has
    no interactive OAuth / authenticated hosted session.
  * Any production SHA/deploy/smoke-test claim.

Everything else in this file is genuine, measured, local verification:
real SQLite DB fixture, real MCP/REST/batch handler code paths, and a real
(fake-Redis-backed) exercise of the cache's hit/miss/stale/outage state
machine and its telemetry counters.
"""
from __future__ import annotations

import asyncio
import json

import pytest
import pytest_asyncio

import meridian.server as server_module  # noqa: F401 -- load before mcp.handler (import-cycle guard)
from meridian import batch_mutate as bmut_module
from meridian import batch_read as br_module
from meridian import capability_manifest as capability_manifest_module
from meridian import db as db_module
from meridian import mcp_tools
from meridian import profile_cache
from meridian import profile_contract as pc
from meridian import redis_bridge
from meridian import tool_manifest as tool_manifest_module
from meridian.db import batch_management as bm
from meridian.mcp.handlers.project_tools import (
    handle_activate_profile_layer,
    handle_clone_profile_layer,
    handle_get_profile_layer_revisions,
    handle_reset_profile_layer,
    handle_save_profile_layer,
)
from meridian.profile_cache import (
    ProfileCacheKey,
    get_or_fetch,
    get_profile_cache_telemetry,
    invalidate,
    reset_profile_cache_telemetry,
)


@pytest_asyncio.fixture
async def project(db):
    return await db_module.create_project(db, "profile-matrix-proj")


def _mcp_call(client, name, arguments):
    r = client.post("/mcp/sse", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    })
    assert r.status_code == 200
    return r.json()


def _result(resp):
    assert resp.get("result") is not None, resp
    return json.loads(resp["result"]["content"][0]["text"])


# ===========================================================================
# 1. Built-in hosted default lifecycle -- exhaustive 4x4 transition matrix.
#
# tests/test_profile_layers.py already exercises representative transitions
# individually (full path, deprecated-can-reactivate, invalid transition,
# retired-is-terminal, idempotent-per-state parametrized over 4 states).
# This proves EVERY (from_state, to_state) cell against LIFECYCLE_TRANSITIONS
# itself as the oracle -- 16 cells total, including the 6 invalid off-
# diagonal cells no existing test enumerates individually. A future edit to
# LIFECYCLE_TRANSITIONS that isn't mirrored by a test update would be caught
# here even if every hand-picked existing test still passes.
# ===========================================================================

_LIFECYCLE_PATH_TO: dict[str, list[str]] = {
    "draft": [],
    "active": ["active"],
    "deprecated": ["active", "deprecated"],
    "retired": ["retired"],
}


@pytest.mark.parametrize("from_state", pc.LIFECYCLE_STATES)
@pytest.mark.parametrize("to_state", pc.LIFECYCLE_STATES)
async def test_lifecycle_full_transition_matrix_matches_declared_table(db, from_state, to_state):
    await db_module.set_profile_layer(db, "hosted_default", "global", fields={"max_pinned_decisions": 20})
    for step in _LIFECYCLE_PATH_TO[from_state]:
        await db_module.transition_hosted_default_lifecycle(db, "global", step)

    should_succeed = to_state == from_state or to_state in pc.LIFECYCLE_TRANSITIONS[from_state]
    if should_succeed:
        result = await db_module.transition_hosted_default_lifecycle(db, "global", to_state)
        assert result["lifecycle_state"] == to_state
    else:
        with pytest.raises(pc.ProfileContractError):
            await db_module.transition_hosted_default_lifecycle(db, "global", to_state)


# ===========================================================================
# 2. User save/clone/activate/reset/rollback -- integrated MCP handler CHAIN.
#
# tests/test_project_settings.py already unit-tests each of these 5 handlers
# independently. This exercises them as one realistic operator workflow --
# author a hosted_default draft, clone it to a candidate scope, activate the
# clone, confirm the audit trail sees exactly that activation, reset the
# clone back to empty -- and confirms the ORIGINAL source scope is untouched
# by any of it (a real "clone before you break it" workflow).
# ===========================================================================

async def test_mcp_handler_chain_save_clone_activate_revisions_reset(db):
    saved = await handle_save_profile_layer(
        {"scope_type": "hosted_default", "scope_id": "matrix-global",
         "fields": {"max_pinned_decisions": 30}},
        db, "/tmp", None, None,
    )
    assert "error" not in saved
    assert saved["lifecycle_state"] == "draft"
    assert saved["revision"] == 1

    cloned = await handle_clone_profile_layer(
        {"source_scope_type": "hosted_default", "source_scope_id": "matrix-global",
         "target_scope_type": "hosted_default", "target_scope_id": "matrix-global-v2"},
        db, "/tmp", None, None,
    )
    assert "error" not in cloned
    assert cloned["fields"] == {"max_pinned_decisions": 30}
    assert cloned["lifecycle_state"] == "draft"  # clone never inherits source lifecycle

    activated = await handle_activate_profile_layer(
        {"scope_id": "matrix-global-v2"}, db, "/tmp", None, None,
    )
    assert "error" not in activated
    assert activated["lifecycle_state"] == "active"

    revisions = await handle_get_profile_layer_revisions(
        {"scope_id": "matrix-global-v2"}, db, "/tmp", None, None,
    )
    # revision 1 (the clone's own initial write) then 2 (activate) -- newest first.
    assert [r["lifecycle_state"] for r in revisions] == ["active", "draft"]

    reset = await handle_reset_profile_layer(
        {"scope_type": "hosted_default", "scope_id": "matrix-global-v2"}, db, "/tmp", None, None,
    )
    assert "error" not in reset
    assert reset["revision"] == 0
    assert reset["fields"] == {}

    # the clone + activate + reset chain on the TARGET never touched the
    # SOURCE scope -- an idempotent resave of the source must still report
    # revision 1, not bumped.
    source_still = await handle_save_profile_layer(
        {"scope_type": "hosted_default", "scope_id": "matrix-global",
         "fields": {"max_pinned_decisions": 30}},
        db, "/tmp", None, None,
    )
    assert source_still["revision"] == 1


# ===========================================================================
# 3. All overlay precedence -- a value set at EVERY layer simultaneously,
#    PLUS a widen-blocked field in the SAME resolution.
#
# tests/test_profile_layers.py's test_get_effective_profile_full_layer_chain_precedence
# covers the general precedence chain; this specifically sets ONE field at
# all 5 conceptual layers at once (the project layer via the LEGACY
# get_project_settings authority -- FIELD_REGISTRY's real, production
# mechanism for that field, not a synthetic stand-in) and proves the
# narrow_only widen-block for a SECOND field coexists correctly in the same
# single resolution (existing tests exercise precedence and widen-blocking
# separately, never both in one merge).
# ===========================================================================

async def test_overlay_precedence_all_five_layers_and_widen_block_coexist(db):
    project = await db_module.create_project(db, "matrix-precedence-all-layers")

    await db_module.set_profile_layer(
        db, "hosted_default", "global",
        fields={"max_pinned_decisions": 5, "hitl_auto_answer": 0},
    )
    await db_module.transition_hosted_default_lifecycle(db, "global", "active")
    await db_module.set_profile_layer(db, "workspace", "singleton", fields={"max_pinned_decisions": 10})
    await db_module.set_profile_layer(db, "user", "matrix-alice", fields={"max_pinned_decisions": 15})
    # max_pinned_decisions' project-scope value is the LEGACY
    # get_project_settings/update_project_settings authority (FIELD_REGISTRY
    # legacy_source="project_settings") -- set_profile_layer would REJECT
    # this field at scope_type="project" (zero-duplication guard). This is
    # the real production mechanism, not a workaround.
    await db_module.update_project_settings(db, project["id"], max_pinned_decisions=20)
    await db_module.set_profile_layer(
        db, "session", "matrix-precedence-sess",
        fields={"max_pinned_decisions": 25, "hitl_auto_answer": 2},  # hitl_auto_answer=2 is a widen attempt
    )

    result = await db_module.get_effective_profile(
        db, project["id"], session_id="matrix-precedence-sess", user_scope_id="matrix-alice",
    )
    assert result["layers_applied"] == ["hosted_default", "workspace", "user", "project", "session"]

    # most-specific-wins for the ordinary field, across ALL 5 layers at once.
    assert result["fields"]["max_pinned_decisions"] == 25
    assert result["field_sources"]["max_pinned_decisions"] == "session"
    chain = [o for o in result["overrides"] if o["field"] == "max_pinned_decisions"]
    assert [o["to_layer"] for o in chain] == ["workspace", "user", "project", "session"]
    assert [o["from_layer"] for o in chain] == ["hosted_default", "workspace", "user", "project"]

    # the SAME resolution's narrow_only widen-block for a DIFFERENT field --
    # proves the two mechanisms compose correctly in one merge.
    assert result["fields"]["hitl_auto_answer"] == 0  # widen rejected, floor stands
    assert len(result["blocked_widens"]) == 1
    assert result["blocked_widens"][0]["field"] == "hitl_auto_answer"
    assert result["degraded"] is True
    assert "narrow_only_widen_blocked" in result["degraded_reasons"]


# ===========================================================================
# 4. Tenant/project isolation -- cross-reference, not a duplicate.
#
# The actual isolation FIX (PROFILE-7 review) lives in the batch_read
# 'profile' adapter's list_profile_layers OPERATION -- see
# tests/test_batch_read_mutate_133bfff6.py::test_profile_adapter_list_profile_layers_is_project_isolated.
# This pins the boundary that fix relies on: the raw DB primitive
# (db.list_profile_layers) is INTENTIONALLY unfiltered -- every persisted
# row, any project -- so a future change can't accidentally start filtering
# here (breaking legitimate unfiltered callers) or accidentally start relying
# on this function for isolation instead of the adapter.
# ===========================================================================

async def test_list_profile_layers_db_primitive_is_unfiltered_by_design(db):
    project_a = await db_module.create_project(db, "matrix-isolation-a")
    project_b = await db_module.create_project(db, "matrix-isolation-b")
    await db_module.set_profile_layer(db, "project", project_a["id"], fields={"claim_verification_mode": "strict"})
    await db_module.set_profile_layer(db, "project", project_b["id"], fields={"claim_verification_mode": "off"})

    rows = await db_module.list_profile_layers(db, "project")
    scope_ids = {r["scope_id"] for r in rows}
    assert {project_a["id"], project_b["id"]} <= scope_ids  # both visible -- unfiltered by design


# ===========================================================================
# 5. Prohibited values -- regex-identity confirmation + MCP handler surface.
#
# DB-level rejection (secret-shaped / absolute-path) is already exhaustively
# covered in tests/test_profile_layers.py. This adds two things that aren't
# covered there: (a) the module docstring's claim that profile_contract.py
# reuses capability_manifest's regexes "verbatim" is literally true (same
# object, not an independently-maintained copy that could drift), and (b)
# the MCP HANDLER surface (not just the bare db function) correctly turns
# the resulting ProfileContractError into a structured {error} dict.
# ===========================================================================

def test_profile_contract_reuses_capability_manifest_regexes_verbatim():
    assert pc._SECRET_LIKE_RE is capability_manifest_module._SECRET_LIKE_RE
    assert pc._ABSOLUTE_PATH_RE is capability_manifest_module._ABSOLUTE_PATH_RE


async def test_handle_save_profile_layer_rejects_prohibited_values(db):
    secret_result = await handle_save_profile_layer(
        {"scope_type": "workspace", "scope_id": "singleton",
         "fields": {"executor_config.deploy_cmd": "curl -H 'api_key: sk-abcdefghij1234567890'"}},
        db, "/tmp", None, None,
    )
    assert "error" in secret_result

    path_result = await handle_save_profile_layer(
        {"scope_type": "workspace", "scope_id": "singleton",
         "fields": {"executor_config.test_cmd": r"C:\tools\run_tests.bat"}},
        db, "/tmp", None, None,
    )
    assert "error" in path_result

    fetched = await db_module.get_profile_layer(db, "workspace", "singleton")
    assert fetched["fields"] == {}  # neither rejected write ever persisted


# ===========================================================================
# 6. Optimistic concurrency -- cross-surface consistency (MCP / batch_mutate
#    agree on WHEN a write is stale).
#
# Each surface's own error SHAPE is already unit-tested independently:
# tests/test_project_settings.py (MCP {error, code: STALE_REVISION} + REST
# 409), tests/test_batch_read_mutate_133bfff6.py (batch_mutate's
# ERROR_CONFLICT). This proves they agree on the underlying condition (the
# SAME expected_revision mismatch) and that neither surface partially
# mutates the row on rejection.
# ===========================================================================

async def test_optimistic_concurrency_consistent_across_mcp_and_batch_surfaces(db, project):
    saved = await db_module.set_profile_layer(db, "workspace", "singleton", fields={"auto_worktrees": 1})
    stale_rev = saved["revision"] + 5

    mcp_result = await handle_save_profile_layer(
        {"scope_type": "workspace", "scope_id": "singleton",
         "fields": {"auto_worktrees": 0}, "expected_revision": stale_rev},
        db, "/tmp", None, None,
    )
    assert mcp_result.get("code") == "STALE_REVISION"

    batch_resp = await bmut_module.batch_mutate(
        db, project_id=project["id"],
        entries=[{"kind": "profile_layer", "scope_type": "workspace", "scope_id": "singleton",
                  "fields": {"auto_worktrees": 0}, "expected_revision": stale_rev,
                  "correlation_key": "conflict"}],
        mode="all_or_nothing", idempotency_key="matrix-concurrency-cross-surface",
    )
    assert batch_resp["results"][0]["error_code"] == bm.ERROR_CONFLICT

    unchanged = await db_module.get_profile_layer(db, "workspace", "singleton")
    assert unchanged["fields"]["auto_worktrees"] == 1
    assert unchanged["revision"] == saved["revision"]


# ===========================================================================
# 7 + 8. Redis hit/miss/stale/outage fallback, and MEASURED Neon-query
#    avoidance -- against a REAL AuthorityFetch calling db.get_effective_profile.
#
# Mirrors tests/test_8d52b620_profile_cache.py's established fake-Redis
# technique EXACTLY (same monkeypatch pattern), but wired to the real
# PROFILE cache namespace (NAMESPACE_EFFECTIVE_PROFILE) with a genuine
# AuthorityFetch closing over db.get_effective_profile -- not a synthetic
# {"resolved": True} stand-in. This is real, measurable, LOCAL verification
# of hit/miss/stale/outage behavior -- see the file header for what it does
# NOT prove (a live Redis instance, a live Neon deployment).
# ===========================================================================

class _FakeRedisClient:
    """Minimal in-memory fake -- duplicated locally per this codebase's
    established per-file convention (tests/ has no __init__.py and no
    cross-test imports exist anywhere in this suite; see e.g.
    tests/test_tunnel_routes.py's own locally-duplicated _make_hosted_client/
    _new_tenant_token/_run helpers)."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get(self, key: str):
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None):
        self.store[key] = value
        return True

    async def delete(self, key: str):
        existed = key in self.store
        self.store.pop(key, None)
        return 1 if existed else 0


@pytest.fixture(autouse=True)
def _reset_profile_cache_state():
    redis_bridge.reset_redis_client_cache()
    reset_profile_cache_telemetry()
    yield
    redis_bridge.reset_redis_client_cache()
    reset_profile_cache_telemetry()


def _authority_counter(db, project_id: str, counters: dict[str, int]):
    async def _authority():
        counters["calls"] += 1
        return await db_module.get_effective_profile(db, project_id)
    return _authority


def _effective_profile_key(project_id: str, generation_key: str) -> ProfileCacheKey:
    return ProfileCacheKey(
        namespace=profile_cache.NAMESPACE_EFFECTIVE_PROFILE,
        scope_type="project",
        scope_id=project_id,
        profile_id=project_id,
        generation_key=generation_key,
        schema_version=1,
        resolver_version=1,
    )


async def test_redis_cache_hit_miss_stale_outage_against_real_get_effective_profile(db, monkeypatch):
    project = await db_module.create_project(db, "matrix-cache-behavior")
    fake = _FakeRedisClient()

    async def _fake_get_client():
        return fake

    monkeypatch.setattr(redis_bridge, "get_redis_client", _fake_get_client)

    counters = {"calls": 0}
    authority = _authority_counter(db, project["id"], counters)

    baseline = await db_module.get_effective_profile(db, project["id"])
    key = _effective_profile_key(project["id"], baseline["generation_key"])

    # MISS -- real authority call (db.get_effective_profile), value cached.
    miss = await get_or_fetch(key, authority)
    assert miss.outcome == "miss"
    assert counters["calls"] == 1
    assert miss.value["project_id"] == project["id"]

    # HIT -- zero additional authority calls.
    hit = await get_or_fetch(key, authority)
    assert hit.outcome == "hit"
    assert counters["calls"] == 1
    assert hit.value == miss.value

    # A genuine content change (new profile_layers write) -> new
    # generation_key -> plain miss by default (allow_stale_seconds=0),
    # never silently served stale.
    await db_module.set_profile_layer(db, "workspace", "singleton", fields={"auto_worktrees": 0})
    bumped = await db_module.get_effective_profile(db, project["id"])
    key_v2 = _effective_profile_key(project["id"], bumped["generation_key"])
    bumped_result = await get_or_fetch(key_v2, authority)
    assert bumped_result.outcome == "miss"
    assert counters["calls"] == 2

    # STALE fallback -- opt-in allow_stale_seconds: a new generation not yet
    # cached is served from the "latest known good" pointer with ZERO
    # additional authority calls.
    await db_module.set_profile_layer(db, "workspace", "singleton", fields={"auto_worktrees": 1})
    bumped_again = await db_module.get_effective_profile(db, project["id"])
    key_v3 = _effective_profile_key(project["id"], bumped_again["generation_key"])
    stale = await get_or_fetch(key_v3, authority, allow_stale_seconds=3600)
    assert stale.outcome == "stale_hit"
    assert counters["calls"] == 2  # no new authority call

    # OUTAGE -- Redis unavailable falls back safely to authority, never raises.
    async def _fake_get_client_down():
        return None

    monkeypatch.setattr(redis_bridge, "get_redis_client", _fake_get_client_down)
    outage = await get_or_fetch(key_v3, authority)
    assert outage.outcome == "bypass_redis_unavailable"
    assert counters["calls"] == 3
    assert outage.value["project_id"] == project["id"]


async def test_measured_neon_avoidance_call_count_evidence(db, monkeypatch):
    """THE measured-evidence test for this item's acceptance criterion:
    'Do not claim Redis savings without measured before/after evidence.'

    Counts REAL invocations of db.get_effective_profile (the 'Neon query'
    stand-in -- this dev environment is SQLite; per this item's own
    instructions the call-COUNT metric is what matters, not the specific
    backend) across N cache-eligible reads, a generation bump, and an
    explicit invalidate(). Every number asserted below is restated verbatim
    in this session's final report -- it is not a general "Redis reduces
    Neon load" claim, it is: in THIS measured scenario, these exact call
    counts occurred.
    """
    project = await db_module.create_project(db, "matrix-neon-avoidance")
    fake = _FakeRedisClient()

    async def _fake_get_client():
        return fake

    monkeypatch.setattr(redis_bridge, "get_redis_client", _fake_get_client)

    counters = {"calls": 0}
    authority = _authority_counter(db, project["id"], counters)

    baseline = await db_module.get_effective_profile(db, project["id"])
    key = _effective_profile_key(project["id"], baseline["generation_key"])

    n_reads = 5
    results = [await get_or_fetch(key, authority) for _ in range(n_reads)]
    # MEASURED: n_reads=5 reads of the SAME generation -> exactly 1 authority call.
    assert counters["calls"] == 1
    assert results[0].outcome == "miss"
    assert all(r.outcome == "hit" for r in results[1:])

    await db_module.set_profile_layer(db, "workspace", "singleton", fields={"auto_worktrees": 0})
    bumped = await db_module.get_effective_profile(db, project["id"])
    key_v2 = _effective_profile_key(project["id"], bumped["generation_key"])
    bump_result = await get_or_fetch(key_v2, authority)
    # MEASURED: a real content change (generation bump) -> exactly 1 MORE
    # authority call (2 total).
    assert counters["calls"] == 2
    assert bump_result.outcome == "miss"

    await invalidate(key_v2)
    reinvalidated = await get_or_fetch(key_v2, authority)
    # MEASURED: an explicit invalidate() of the SAME generation -> exactly 1
    # MORE authority call (3 total) on the next read.
    assert counters["calls"] == 3
    assert reinvalidated.outcome == "miss"

    telemetry = get_profile_cache_telemetry()
    # MEASURED telemetry snapshot for this exact scenario -- 7 total
    # get_or_fetch calls (5 + 1 + 1); 3 were real authority calls, 4 were
    # served entirely from cache with zero authority calls.
    assert telemetry["authority_calls"] == 3
    assert telemetry["authority_calls_avoided"] == 4
    assert telemetry["hits"] == 4
    assert telemetry["misses"] == 3
    assert telemetry["neon_avoidance_ratio"] == pytest.approx(4 / 7)


# ===========================================================================
# 9. Batch per-entry errors/idempotency -- ONE mixed profile_layer +
#    sprint_item_pointer SUCCESS test.
#
# tests/test_batch_read_mutate_133bfff6.py exercises profile_layer entries
# in ISOLATION for the success path, and mixes profile_layer with
# sprint_item_pointer only in the ROLLBACK test
# (test_batch_mutate_profile_layer_rollback_restores_prior_and_deletes_new).
# This proves the SUCCESS path for that exact mixed combination, which is
# otherwise untested -- both kinds genuinely commit atomically together.
# ===========================================================================

async def test_batch_mutate_mixed_profile_layer_and_sprint_item_pointer_success(db, project):
    item = await db_module.add_sprint_item(db, project["id"], "v1", "Mixed profile+pointer target")
    entries = [
        {"kind": "profile_layer", "scope_type": "workspace", "scope_id": "singleton",
         "fields": {"auto_worktrees": 0}, "correlation_key": "pl1"},
        {"kind": "sprint_item_pointer", "sprint_item_id": item["id"], "source_type": "file",
         "targets": [{"uri": "meridian/db/batch_management.py",
                     "selector": {"type": "range", "start_line": 1, "end_line": 5}}],
         "correlation_key": "p1"},
    ]
    resp = await bmut_module.batch_mutate(
        db, project_id=project["id"], entries=entries, mode="all_or_nothing",
        idempotency_key="matrix-mixed-success-1",
    )
    assert resp["status"] == "ok"
    assert resp["committed_count"] == 2
    assert resp["failures"] == []
    assert resp["rollback_status"] == "none"

    layer = await db_module.get_profile_layer(db, "workspace", "singleton")
    assert layer["fields"] == {"auto_worktrees": 0}
    ptrs = await db_module.get_sprint_item_pointers(db, item["id"])
    assert len(ptrs) == 1


# ===========================================================================
# 10. start_session and handoff modes -- SAME generation_key across two
#     independently-built surfaces.
#
# tests/test_handoff_executor_planner_lifecycle.py covers start_session's
# profile_binding and generate_handoff's sibling field + inline
# <profile_generation> tag SEPARATELY (they were built in different PROFILE-6
# sessions/commits). Nothing existing checks that a single session's
# start_session response and its subsequent generate_handoff(mode='goal')
# call report the IDENTICAL generation_key -- this proves that.
# ===========================================================================

def test_start_session_and_goal_handoff_share_same_generation_key(client):
    pid = client.post("/projects", json={"name": "matrix-goal-consistency"}).json()["id"]
    _mcp_call(client, "add_sprint_item", {
        "project_id": pid, "version": "v1", "title": "matrix goal consistency item",
    })

    sess = _result(_mcp_call(client, "start_session", {
        "project_id": pid, "session_name": "matrix-goal-consistency",
    }))
    start_key = sess["profile_binding"]["generation_key"]
    assert start_key.startswith("sha256:")

    handoff = _result(_mcp_call(client, "generate_handoff", {
        "project_id": pid, "mode": "goal", "session_id": sess.get("session_id"),
    }))
    # the MCP wrapper's own sibling profile_binding field (present regardless
    # of mode -- see meridian/mcp/handler.py's generate_handoff dispatch).
    assert handoff["profile_binding"]["generation_key"] == start_key

    # AND the inline <profile_generation> tag embedded in the rendered
    # /goal text itself -- the SAME key must appear in both independently-
    # built surfaces (start_session's enrichment block vs. the goal-string
    # renderer inside handoff.py).
    marker = '<profile_generation key="'
    idx = handoff["content"].index(marker) + len(marker)
    tag_key = handoff["content"][idx:handoff["content"].index('"', idx)]
    assert tag_key == start_key


# ===========================================================================
# 11. Tunnel/connector generation refresh -- cross-check against an
#     INDEPENDENT computation, not a re-test of PROFILE-6's own coverage.
#
# tests/test_tunnel_routes.py already proves profile_binding is present,
# shaped correctly, and changes when workspace config changes. This proves
# the route's reported generation_key/executable/degraded/restart_required/
# restart_report EQUAL what db.get_workspace_effective_profile computes
# independently for the same tenant state -- a genuine agreement check.
# ===========================================================================

def _make_matrix_hosted_client(monkeypatch, tmp_path):
    """Local duplicate of tests/test_tunnel_routes.py's _make_hosted_client
    helper -- this codebase's established per-file duplication convention."""
    monkeypatch.setenv("MERIDIAN_HOSTED", "true")
    monkeypatch.setenv("MERIDIAN_DB", ":memory:")
    monkeypatch.setenv("MERIDIAN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MERIDIAN_DB_URL", "")
    monkeypatch.setenv("MERIDIAN_DEMO_DB_URL", "")
    monkeypatch.setenv("MERIDIAN_SKIP_DEMO", "1")
    monkeypatch.setenv("MERIDIAN_GOAL_MD", str(tmp_path / "GOAL.md"))

    import importlib
    from fastapi.testclient import TestClient

    reloaded = importlib.reload(server_module)
    return TestClient(reloaded.app)


async def _matrix_new_tenant_token(db, email: str) -> str:
    tenant = await db_module.upsert_tenant(db, email)
    raw, _row = await db_module.create_api_token(db, tenant["id"], label="t")
    return raw


def test_tunnel_status_profile_binding_matches_independent_workspace_resolution(monkeypatch, tmp_path):
    with _make_matrix_hosted_client(monkeypatch, tmp_path) as client:
        raw_token = asyncio.run(
            _matrix_new_tenant_token(client.app.state.db, "matrix-tunnel-cross-check@example.com")
        )
        hdr = {"Authorization": f"Bearer {raw_token}"}
        tenant_id = client.get("/me", headers=hdr).json()["tenant_id"]

        asyncio.run(db_module.set_profile_layer(
            client.app.state.db, "workspace", "singleton", fields={"auto_worktrees": 0},
        ))

        route_binding = client.get(f"/tunnel/status/{tenant_id}").json()["profile_binding"]
        independent = asyncio.run(db_module.get_workspace_effective_profile(client.app.state.db))

        assert route_binding["generation_key"] == independent["generation_key"]
        assert route_binding["executable"] == independent["executable"]
        assert route_binding["degraded"] == independent["degraded"]
        assert route_binding["restart_required"] == independent["restart_required"]
        assert route_binding["restart_report"] == independent["restart_report"]


# ===========================================================================
# 12. tools/list manifest generation/hash -- confirm the 8 PROFILE-5 MCP
#     tools are actually present in the real listing, AND that the
#     manifest_hash mechanism (already exhaustively tested in
#     tests/test_capability_contract.py for the project-level
#     capability_profiles system) also reacts to the profile subsystem's
#     OWN capability-shaped field (capability_manifest_ref) -- a different
#     integration point, confirmed independent in
#     tests/test_profile_layers.py::test_capability_profile_and_profile_layers_tables_are_independent.
# ===========================================================================

_PROFILE5_TOOL_NAMES = [
    "list_profile_layers", "get_profile_layer", "save_profile_layer",
    "clone_profile_layer", "activate_profile_layer", "reset_profile_layer",
    "get_profile_layer_revisions", "get_effective_profile",
]


def test_profile5_tools_present_in_real_tool_listing_and_connector_manifest():
    names = {t["name"] for t in mcp_tools._MCP_TOOLS_LIST}
    missing = [n for n in _PROFILE5_TOOL_NAMES if n not in names]
    assert missing == [], f"missing profile MCP tools: {missing}"

    manifest = tool_manifest_module.build_tool_manifest(mcp_tools._MCP_TOOLS_LIST)
    manifest_names = {t["name"] for t in manifest["tools"]}
    still_missing = [n for n in _PROFILE5_TOOL_NAMES if n not in manifest_names]
    assert still_missing == [], f"missing from connector manifest: {still_missing}"


def test_removing_any_profile_tool_changes_manifest_revision():
    rev_full = tool_manifest_module.tool_manifest_revision(mcp_tools._MCP_TOOLS_LIST)
    for tool_name in _PROFILE5_TOOL_NAMES:
        without = [t for t in mcp_tools._MCP_TOOLS_LIST if t["name"] != tool_name]
        rev_without = tool_manifest_module.tool_manifest_revision(without)
        assert rev_without != rev_full, f"removing {tool_name!r} did not change manifest revision"


async def test_capability_manifest_ref_roundtrips_and_manifest_hash_changes(db):
    manifest_v1 = [{"id": "code-search", "purpose": "search code", "required_tools": ["grep"]}]
    manifest_v2 = manifest_v1 + [{"id": "docs-search", "purpose": "search docs", "required_tools": ["meridian-docs"]}]

    await db_module.set_profile_layer(db, "workspace", "singleton", fields={"capability_manifest_ref": manifest_v1})
    layer1 = await db_module.get_profile_layer(db, "workspace", "singleton")
    hash1 = capability_manifest_module.manifest_hash(
        capability_manifest_module.normalize_manifest(layer1["fields"]["capability_manifest_ref"])
    )

    await db_module.set_profile_layer(db, "workspace", "singleton", fields={"capability_manifest_ref": manifest_v2})
    layer2 = await db_module.get_profile_layer(db, "workspace", "singleton")
    hash2 = capability_manifest_module.manifest_hash(
        capability_manifest_module.normalize_manifest(layer2["fields"]["capability_manifest_ref"])
    )
    assert hash1 != hash2

    hash1_again = capability_manifest_module.manifest_hash(
        capability_manifest_module.normalize_manifest(manifest_v1)
    )
    assert hash1 == hash1_again  # deterministic: identical content hashes identically


# ===========================================================================
# 13. Stale-client refresh -- restart_report / restart_required end-to-end
#     through get_effective_profile AND project_profile_binding.
#
# executor_config.repo_path is the FIELD_REGISTRY entry classified
# restart_class='restart_required', component='connector'. This confirms a
# change to it produces restart_required=True + restart_report['connector']
# == 'restart_required' through get_effective_profile, AND that the compact
# projection reaching start_session/generate_handoff/tunnel clients
# (project_profile_binding) carries the identical signal -- the literal
# "stale-client refresh" contract this item names.
# ===========================================================================

async def test_restart_required_field_change_surfaces_through_binding(db):
    project = await db_module.create_project(db, "matrix-restart-required")
    baseline = await db_module.get_effective_profile(db, project["id"])

    await db_module.set_profile_layer(
        db, "session", "matrix-restart-sess",
        fields={"executor_config.repo_path": r"C:\repo\matrix-restart"},
    )
    result = await db_module.get_effective_profile(
        db, project["id"], session_id="matrix-restart-sess",
        previous_fields=baseline["fields"],
    )
    assert result["restart_required"] is True
    assert result["restart_report"]["connector"] == "restart_required"
    # scoped, not a blanket "everything needs a restart" signal.
    assert result["restart_report"]["general"] == "none"
    assert result["restart_report"]["tunnel"] == "none"
    assert result["restart_report"]["capability"] == "none"

    binding = pc.project_profile_binding(result)
    assert binding["restart_required"] is True
    assert binding["restart_report"]["connector"] == "restart_required"


# ===========================================================================
# 14. Production-safe rollback -- a fresh project/tenant that has NEVER
#     touched any profile_layers row (the common case: every project that
#     existed before this subsystem shipped) degrades gracefully across
#     every integration point PROFILE-5/6/7 touch.
# ===========================================================================

def test_fresh_project_degrades_gracefully_across_project_scoped_surfaces(client):
    pid = client.post("/projects", json={"name": "matrix-fresh-rollback"}).json()["id"]
    db = client.app.state.db

    r = client.get(f"/projects/{pid}/effective-profile")
    assert r.status_code == 200
    eff = r.json()
    assert eff["executable"] is True
    assert eff["degraded"] is False
    assert eff["layers_applied"] == ["project"]  # only the always-present synthetic project layer

    sess = _result(_mcp_call(client, "start_session", {
        "project_id": pid, "session_name": "matrix-fresh-rollback",
    }))
    assert sess["profile_binding"]["executable"] is True
    assert sess["profile_binding"]["degraded"] is False

    for mode in ("full", "delta", "goal"):
        handoff = _result(_mcp_call(client, "generate_handoff", {
            "project_id": pid, "mode": mode, "session_id": sess.get("session_id"),
        }))
        assert handoff["profile_binding"]["executable"] is True
        assert handoff["profile_binding"]["degraded"] is False

    resp = asyncio.run(br_module.batch_read(
        db, project_id=pid,
        requests=[{"request_id": "eff", "adapter": "profile", "operation": "get_effective_profile"}],
    ))
    assert resp["results"][0]["status"] == "ok"
    assert resp["results"][0]["result"]["executable"] is True


def test_fresh_tenant_tunnel_routes_degrade_gracefully_with_zero_profile_config(monkeypatch, tmp_path):
    with _make_matrix_hosted_client(monkeypatch, tmp_path) as client:
        raw_token = asyncio.run(
            _matrix_new_tenant_token(client.app.state.db, "matrix-fresh-tenant@example.com")
        )
        hdr = {"Authorization": f"Bearer {raw_token}"}
        tenant_id = client.get("/me", headers=hdr).json()["tenant_id"]

        status = client.get(f"/tunnel/status/{tenant_id}")
        assert status.status_code == 200
        status_binding = status.json()["profile_binding"]
        assert status_binding["executable"] is True
        assert status_binding["degraded"] is False

        plugins = client.get("/tunnel/plugins", headers=hdr)
        assert plugins.status_code == 200
        plugins_binding = plugins.json()["profile_binding"]
        assert plugins_binding["executable"] is True
        assert plugins_binding["degraded"] is False
