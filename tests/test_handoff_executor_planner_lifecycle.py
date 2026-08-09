"""Tests for 0d0cada7 — lease-local scheduler contract, handoff-rendering half.

meridian/db/sprint_items.py (get_parallelizable_groups / claim_parallel_batch),
meridian/db/locks.py (claim_symbol / release_symbol), meridian/mcp/handler.py
(_sprint_item_resource_claim_gate), and meridian/db/__init__.py (request_hitl)
each gained additive scheduler-lease diagnostics (plan_generation,
resource_blocked, claim_granularity, lease_expiry, wait_reason, retry_after,
blocker_context) — see tests/test_resource_locks.py and
tests/test_sprint_item_waves.py for that coverage. This file covers the other
half of the contract: the executor/planner LIFECYCLE surface —
meridian/handoff.py's new ``_build_scheduler_lease_clause`` and its wiring
into ``_build_quick_start_goal`` / ``generate_handoff``.

Motivating incident (from the sprint item's own notes): a live v0.2.6 run
had one session actively executing exactly ONE item while its broader
"planned backlog" made every OTHER authorized item look blocked for hours;
the executor emitted a native clarification instead of recording a Meridian
blocker or recomputing the residual work. The goal-string guidance added
here is what tells a receiving executor to poll with bounded backoff and
recompute via get_parallelizable_groups instead of repeating that mistake.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from meridian import db as db_module
from meridian import handoff as handoff_module


# ---------------------------------------------------------------------------
# _build_scheduler_lease_clause — unit-level (no DB), pinning the exact
# additive/backward-compatible contract the docstring promises.
# ---------------------------------------------------------------------------


def test_scheduler_lease_clause_empty_when_parallel_groups_none():
    assert handoff_module._build_scheduler_lease_clause(None) == ""


def test_scheduler_lease_clause_empty_when_parallel_groups_empty_dict():
    assert handoff_module._build_scheduler_lease_clause({}) == ""


def test_scheduler_lease_clause_empty_when_neither_field_present():
    """A hand-built dict from a pre-0d0cada7 code path (or an older cached
    fixture) carries neither key — must degrade to '', not KeyError."""
    assert handoff_module._build_scheduler_lease_clause({"groups": [], "blocked": []}) == ""


def test_scheduler_lease_clause_renders_plan_generation_tag():
    clause = handoff_module._build_scheduler_lease_clause({"plan_generation": "abc123deadbeef"})
    assert '<plan_generation value="abc123deadbeef" />' in clause
    assert "<resource_contention>" not in clause


def test_scheduler_lease_clause_xml_escapes_plan_generation():
    clause = handoff_module._build_scheduler_lease_clause({"plan_generation": 'a"b<c>'})
    assert '"' not in clause.split('value="', 1)[1].split('"', 1)[0].replace("&quot;", "")
    assert "&quot;" in clause  # the embedded quote was escaped, not left raw


def test_scheduler_lease_clause_renders_resource_contention_with_poll_guidance():
    clause = handoff_module._build_scheduler_lease_clause({
        "resource_blocked": [
            {
                "id": "item-1", "resource": "file:a.py",
                "holder_session_id": "holder-session-abcdef", "retry_after": 42,
            },
        ],
    })
    assert "<resource_contention>" in clause
    assert "</resource_contention>" in clause
    assert "item-1" in clause
    assert "file:a.py" in clause
    assert "42" in clause
    # The core behavioral guidance the incident's postmortem calls for.
    assert "do not open a native clarification" in clause
    assert "scheduler_blocker" in clause
    assert "get_parallelizable_groups" in clause


def test_scheduler_lease_clause_renders_both_fields_together():
    clause = handoff_module._build_scheduler_lease_clause({
        "plan_generation": "gen1",
        "resource_blocked": [{"id": "x", "resource": "file:x.py", "retry_after": 10}],
    })
    assert '<plan_generation value="gen1" />' in clause
    assert "<resource_contention>" in clause


# ---------------------------------------------------------------------------
# End-to-end via generate_handoff(mode="goal") — the clause is wired into the
# REAL /goal string, using the SAME get_parallelizable_groups() call
# generate_handoff already makes (no new call site, no new parameter).
# ---------------------------------------------------------------------------


def _sprint_items_tag_body(content: str) -> str:
    start = content.rindex("<sprint_items>") + len("<sprint_items>")
    end = content.index("</sprint_items>", start)
    return content[start:end]


@pytest.mark.asyncio
async def test_generate_handoff_goal_mode_includes_plan_generation(db, tmp_path):
    p = await db_module.create_project(db, "0d0cada7-goal-plan-generation")
    await db_module.add_sprint_item(db, p["id"], "v1", "solo item", prospect_bypass=True)
    _path, content, _amended = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="goal",
    )
    assert "<plan_generation value=" in content


@pytest.mark.asyncio
async def test_generate_handoff_goal_mode_no_contention_when_nothing_locked(db, tmp_path):
    """The common case: nothing external is held, so the goal must NOT claim
    resource contention that doesn't exist."""
    p = await db_module.create_project(db, "0d0cada7-goal-no-contention")
    await db_module.add_sprint_item(db, p["id"], "v1", "free item", prospect_bypass=True)
    _path, content, _amended = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="goal",
    )
    assert "<resource_contention>" not in content


@pytest.mark.asyncio
async def test_generate_handoff_goal_mode_surfaces_resource_contention(db, tmp_path):
    """The exact incident shape: an item is dependency-satisfied (claimable
    per the wave/dependency logic) but a DIFFERENT live session already
    holds its declared file. The /goal must say so explicitly and tell the
    executor to poll, not escalate."""
    p = await db_module.create_project(db, "0d0cada7-goal-contention")
    pid = p["id"]
    other = await db_module.register_session(db, pid, "other-live-session")
    item = await db_module.add_sprint_item(
        db, pid, "v1", "touches an externally locked file",
        touches_resources=["file:contended.py"], prospect_bypass=True,
    )
    pre = await db_module.claim_file(db, "contended.py", other["id"])
    assert pre["claimed"] is True

    _path, content, _amended = await handoff_module.generate_handoff(
        db, pid, str(tmp_path), skip_ai_summary=True, mode="goal",
    )
    assert "<resource_contention>" in content
    assert item["id"] in content
    assert "do not open a native clarification" in content
    # The item itself is still listed as claimable work, not silently dropped.
    assert item["id"] in _sprint_items_tag_body(content)


# ---------------------------------------------------------------------------
# request_hitl blocker_context — the "genuine conflict is persisted and
# visible through Meridian HITL/blocker APIs" acceptance criterion, exercised
# through the SAME live() lookup a scheduler-blocker caller would use to
# build blocker_context (get_parallelizable_groups' resource_blocked entry).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scheduler_blocker_hitl_round_trips_resource_blocked_entry(db):
    """An executor that sees a resource_blocked entry from
    get_parallelizable_groups can hand it straight to request_hitl's
    blocker_context and get a durable, queryable record back — the
    'structured Meridian blocker event' the contract calls for, distinct
    from an untracked native stop."""
    p = await db_module.create_project(db, "0d0cada7-scheduler-blocker-roundtrip")
    pid = p["id"]
    holder = await db_module.register_session(db, pid, "holder")
    waiting = await db_module.register_session(db, pid, "waiting")
    item = await db_module.add_sprint_item(
        db, pid, "v1", "contended item", touches_resources=["file:hot.py"],
    )
    pre = await db_module.claim_file(db, "hot.py", holder["id"])
    assert pre["claimed"] is True

    groups = await db_module.get_parallelizable_groups(db, pid, version="v1")
    assert groups["resource_blocked_count"] == 1
    entry = dict(groups["resource_blocked"][0])
    entry["item_id"] = entry["id"]  # request_hitl's blocker field is "item_id"
    entry["plan_generation"] = groups["plan_generation"]

    row = await db_module.request_hitl(
        db, pid, f"{item['id']} is waiting on a live resource lock",
        session_id=waiting["id"], kind="scheduler_blocker", blocker_context=entry,
    )
    assert row["kind"] == "scheduler_blocker"
    blocker = json.loads(row["payload"])["blocker"]
    assert blocker["item_id"] == item["id"]
    assert blocker["holder_session_id"] == holder["id"]
    assert blocker["wait_reason"] == "resource_locked"
    assert blocker["plan_generation"] == groups["plan_generation"]

    # Visible via the listing API too, not just the immediate return value.
    pending = await db_module.list_hitl_requests(db, pid, status="pending")
    assert any(r["id"] == row["id"] for r in pending)


# ---------------------------------------------------------------------------
# a8c38d18 — HARDEN: run the eligible symbol-disjoint frontier as one
# super-wave, not artificial serial batch/macro-wave barriers.
#
# get_parallelizable_groups' "groups"/"macro_waves" are, per its own
# docstring, a presentation/diagnostic partition -- claim_sprint_item's
# atomic per-resource locking (18c488b6) is the actual concurrency-safety
# mechanism, not batch/wave completion order. The /goal text built by
# _build_quick_start_goal previously told the executor to "finish a batch
# before starting the next" / "finish each macro-wave before the next",
# which is an artificial barrier: a later-numbered batch/wave only means the
# greedy first-fit colorer found A conflict with something already placed,
# not that the WHOLE earlier group must finish first. These tests pin the
# corrected wording (dispatch the whole eligible frontier concurrently;
# retry only a claim that is actually rejected) both at the unit level
# (direct _build_quick_start_goal calls, mirroring test_2a654cb0_wave_runs.py
# / test_core.py's existing framing coverage) and end-to-end through a real
# generate_handoff(mode="goal") call.
# ---------------------------------------------------------------------------


def test_build_quick_start_goal_flat_batches_have_no_serial_barrier_directive():
    """e20db0be's flat batch listing must not tell the executor to finish a
    batch before starting the next."""
    items = [
        {"id": "a1", "version": None}, {"id": "b2", "version": None},
        {"id": "c3", "version": None},
    ]
    groups = {
        "group_count": 2,
        "groups": [
            [{"id": "a1", "title": "x"}, {"id": "b2", "title": "y"}],
            [{"id": "c3", "title": "z"}],
        ],
    }
    goal = handoff_module._build_quick_start_goal(items, parallel_groups=groups)
    # The old artificial-barrier phrasing must be gone.
    assert "finish a batch before starting the next" not in goal
    assert "finish each numbered batch before the next" not in goal
    assert "finish each macro-wave before the next" not in goal
    # The corrected guidance: dispatch concurrently, retry only rejections.
    assert "CONCURRENTLY" in goal
    assert "serial execution barrier" in goal.lower()
    assert "retry only that one item" in goal
    assert "claim_sprint_item atomically arbitrates" in goal
    # Existing batch listing/label content is preserved verbatim (test_core.py
    # / test_2a654cb0_wave_runs.py's own coverage of this framing).
    assert "resource-conflict-free batches" in goal
    assert "batch 1: a1, b2" in goal
    assert "batch 2: c3" in goal


def test_build_quick_start_goal_macro_waves_have_no_serial_barrier_directive():
    """dcfbe55c's macro-wave-compressed listing must carry the same
    anti-barrier guidance as the flat batch listing -- a macro-wave label is
    a presentation-only packing of the real conflict-free groups, never a
    claim-safety boundary."""
    items = [{"id": f"i{n}", "version": None} for n in range(6)]
    groups = [
        [{"id": "i0"}, {"id": "i1"}], [{"id": "i2"}], [{"id": "i3"}],
        [{"id": "i4"}], [{"id": "i5"}],
    ]
    macro_waves = [
        {"batches": groups[0:2], "batch_count": 2, "item_count": 3},
        {"batches": groups[2:4], "batch_count": 2, "item_count": 2},
        {"batches": groups[4:5], "batch_count": 1, "item_count": 1},
    ]
    parallel_groups = {
        "group_count": 5, "groups": groups, "macro_waves": macro_waves,
        "requested_macro_wave_count": 3, "macro_wave_count": 3, "blocked": [],
    }
    goal = handoff_module._build_quick_start_goal(items, parallel_groups=parallel_groups)
    assert "finish each macro-wave before the next" not in goal
    assert "finish each numbered batch before the next" not in goal
    assert "finish a batch before starting the next" not in goal
    assert "CONCURRENTLY" in goal
    assert "serial execution barrier" in goal.lower()
    assert "retry only that one item" in goal
    assert "claim_sprint_item atomically arbitrates" in goal
    # Wave/macro-wave framing itself is preserved verbatim (2a654cb0 coverage).
    assert "macro-wave" in goal
    assert "presentation only" in goal
    assert "Wave 1 [batch 1: i0, i1; batch 2: i2]" in goal
    assert "Wave 2 [batch 3: i3; batch 4: i4]" in goal
    assert "Wave 3 [batch 5: i5]" in goal


@pytest.mark.asyncio
async def test_generate_handoff_goal_mode_super_wave_no_batch_barrier(db, tmp_path):
    """End-to-end acceptance check from the item's own notes: a real board
    with a genuinely parallel-safe frontier (one group ends up with 2+
    items) must render a /goal with no artificial batch-barrier directive,
    and must still list every item (nothing dropped by the reword).

    A and B share ``file:shared.py`` so they land in DIFFERENT groups; C is
    disjoint from both and first-fits into A's group, giving group 0 two
    items -- the exact shape ``_has_parallel`` requires (group_count > 1 AND
    at least one group with >1 item) to engage the flat-batch items_clause.
    """
    p = await db_module.create_project(db, "a8c38d18-super-wave-no-barrier")
    pid = p["id"]
    a = await db_module.add_sprint_item(
        db, pid, "v1", "touch shared file A",
        touches_resources=["file:shared.py"], prospect_bypass=True,
    )
    await db_module.add_sprint_item(
        db, pid, "v1", "touch shared file B",
        touches_resources=["file:shared.py"], prospect_bypass=True, force=True,
    )
    c = await db_module.add_sprint_item(
        db, pid, "v1", "touch disjoint file C",
        touches_resources=["file:disjoint.py"], prospect_bypass=True, force=True,
    )
    groups = await db_module.get_parallelizable_groups(db, pid, version="v1")
    assert groups["group_count"] > 1
    assert any(len(g) > 1 for g in groups["groups"])  # sanity: _has_parallel gate is met

    _path, content, _amended = await handoff_module.generate_handoff(
        db, pid, str(tmp_path), skip_ai_summary=True, mode="goal",
    )
    assert "finish a batch before starting the next" not in content
    assert "finish each numbered batch before the next" not in content
    assert "finish each macro-wave before the next" not in content
    assert "CONCURRENTLY" in content
    assert "serial execution barrier" in content.lower()
    assert a["id"] in content
    assert c["id"] in content


# ---------------------------------------------------------------------------
# PROFILE-6 (89a06e40) — bind effective profile identity/generation into
# start_session, generate_handoff, and the goal-mode inline /goal text.
# Pinned decision ee7bccc9 (project 5787cc92-ba7d-4788-b17c-28ab7938b839)
# covers the tunnel/connector half of this item — see tests/test_tunnel_routes.py
# for that coverage. This item's declared touches_resources named
# tests/test_tunnel_client.py for the tunnel half, but that file turned out
# to cover only the LOCAL tunnel *client* (meridian/tunnel_client.py), not
# the server-side meridian/routes/tunnel.py HTTP routes this item also
# touches — tests/test_tunnel_routes.py is the actual existing test file for
# those routes (confirmed by its own module docstring and end-to-end
# TestClient coverage of GET /tunnel/status/{tenant_id} and GET/PUT
# /tunnel/plugins), so the tunnel-route tests below live there instead.
# ---------------------------------------------------------------------------

_PROFILE_BINDING_KEYS = {
    "generation_key", "executable", "degraded", "restart_required", "restart_report",
}


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


# --- handoff.build_effective_profile_binding — the guarded wrapper ------


async def test_build_effective_profile_binding_wrapper_no_config(db):
    """A project with zero profile_layers config still resolves cleanly —
    no error, a sensible default projection (nothing degraded/blocked)."""
    project = await db_module.create_project(db, "89a06e40-binding-wrapper-empty")
    binding = await handoff_module.build_effective_profile_binding(db, project["id"])
    assert binding is not None
    assert set(binding.keys()) == _PROFILE_BINDING_KEYS
    assert binding["generation_key"].startswith("sha256:")
    assert binding["executable"] is True
    assert binding["degraded"] is False
    assert binding["restart_required"] is False
    assert binding["restart_report"] == {
        "tunnel": "none", "connector": "none", "capability": "none", "general": "none",
    }


async def test_build_effective_profile_binding_wrapper_reflects_configured_layers(db):
    project = await db_module.create_project(db, "89a06e40-binding-wrapper-configured")
    await db_module.set_profile_layer(
        db, "hosted_default", "global", fields={"tool_priority_map": {"a": "b"}},
    )
    await db_module.transition_hosted_default_lifecycle(db, "global", "active")
    before = await handoff_module.build_effective_profile_binding(db, project["id"])
    await db_module.set_profile_layer(
        db, "project", project["id"], fields={"claim_verification_mode": "strict"},
    )
    after = await handoff_module.build_effective_profile_binding(db, project["id"])
    assert before["generation_key"] != after["generation_key"]


async def test_build_effective_profile_binding_wrapper_never_raises(db, monkeypatch):
    async def _boom(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(handoff_module.db_module, "get_effective_profile", _boom)
    project = await db_module.create_project(db, "89a06e40-binding-wrapper-boom")
    binding = await handoff_module.build_effective_profile_binding(db, project["id"])
    assert binding is None


# --- _build_quick_start_goal — inline <profile_generation> tag ----------


def test_build_quick_start_goal_profile_generation_tag_rendered_when_key_given():
    items = [{"id": "i1", "version": None}]
    goal = handoff_module._build_quick_start_goal(
        items, profile_generation_key="sha256:deadbeef", profile_restart_required=True,
    )
    assert '<profile_generation key="sha256:deadbeef" restart_required="true"/>' in goal


def test_build_quick_start_goal_profile_generation_tag_absent_by_default():
    """Backward compat: every existing caller not yet updated (both kwargs
    default to None/False) must render byte-identical output — no tag."""
    items = [{"id": "i1", "version": None}]
    goal = handoff_module._build_quick_start_goal(items)
    assert "<profile_generation" not in goal


def test_build_quick_start_goal_profile_generation_omitted_matches_explicit_none():
    items = [{"id": "i1", "version": None}]
    default_call = handoff_module._build_quick_start_goal(items)
    explicit_none = handoff_module._build_quick_start_goal(
        items, profile_generation_key=None, profile_restart_required=False,
    )
    assert default_call == explicit_none


def test_build_quick_start_goal_profile_generation_tag_absent_on_empty_board_by_default():
    assert "<profile_generation" not in handoff_module._build_quick_start_goal([])


def test_build_quick_start_goal_profile_generation_tag_rendered_on_empty_board():
    goal = handoff_module._build_quick_start_goal(
        [], profile_generation_key="sha256:abc123", profile_restart_required=False,
    )
    assert '<profile_generation key="sha256:abc123" restart_required="false"/>' in goal


def test_build_quick_start_goal_profile_generation_key_is_xml_escaped():
    items = [{"id": "i1", "version": None}]
    goal = handoff_module._build_quick_start_goal(
        items, profile_generation_key='a"b<c>',
    )
    tag_start = goal.index('<profile_generation key="') + len('<profile_generation key="')
    tag_value = goal[tag_start:goal.index('"', tag_start)]
    assert '"' not in tag_value.replace("&quot;", "")
    assert "&quot;" in tag_value


# --- generate_handoff end-to-end — sibling field + inline goal-mode tag -


@pytest.mark.parametrize("mode", ["full", "delta"])
def test_mcp_generate_handoff_includes_profile_binding(client, mode):
    pid = client.post("/projects", json={"name": f"mcp-89a06e40-binding-{mode}"}).json()["id"]
    sess = _result(_mcp_call(client, "start_session", {
        "project_id": pid, "session_name": f"89a06e40-binding-{mode}",
    }))
    session_id = sess.get("session_id")
    result = _result(_mcp_call(client, "generate_handoff", {
        "project_id": pid, "mode": mode, "session_id": session_id,
    }))
    assert "profile_binding" in result
    binding = result["profile_binding"]
    assert binding is not None
    assert set(binding.keys()) == _PROFILE_BINDING_KEYS


def test_http_handoff_endpoint_includes_profile_binding(client):
    pid = client.post("/projects", json={"name": "http-89a06e40-binding"}).json()["id"]
    r = client.post(f"/projects/{pid}/handoff")
    assert r.status_code == 200
    body = r.json()
    assert "profile_binding" in body
    assert body["profile_binding"] is not None
    assert set(body["profile_binding"].keys()) == _PROFILE_BINDING_KEYS


def test_http_planner_handoff_includes_profile_binding(client):
    pid = client.post("/projects", json={"name": "http-89a06e40-binding-planner"}).json()["id"]
    r = client.get(f"/projects/{pid}/handoff/planner")
    assert r.status_code == 200
    body = r.json()
    assert "profile_binding" in body
    assert body["profile_binding"] is not None


async def test_generate_handoff_goal_mode_includes_profile_generation_tag(db, tmp_path):
    """mode='goal' returns ONLY the rendered /goal text (no sibling fields
    — see generate_handoff's own docstring), so the inline tag is this
    mode's sole profile-identity signal."""
    p = await db_module.create_project(db, "89a06e40-goal-profile-tag")
    await db_module.add_sprint_item(db, p["id"], "v1", "solo item", prospect_bypass=True)
    _path, content, _amended = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="goal",
    )
    assert "<profile_generation key=" in content
    assert 'restart_required="false"' in content


# --- handle_start_session — profile_binding orientation field -----------


def test_mcp_start_session_includes_profile_binding_no_config(client):
    pid = client.post(
        "/projects", json={"name": "mcp-89a06e40-start-binding-empty"}
    ).json()["id"]
    result = _result(_mcp_call(client, "start_session", {
        "project_id": pid, "session_name": "89a06e40-start-binding-empty",
    }))
    assert "profile_binding" in result
    binding = result["profile_binding"]
    assert binding is not None
    assert set(binding.keys()) == _PROFILE_BINDING_KEYS
    assert binding["executable"] is True
    assert binding["degraded"] is False


def test_mcp_start_session_includes_profile_binding_with_configured_layers(client):
    pid = client.post(
        "/projects", json={"name": "mcp-89a06e40-start-binding-configured"}
    ).json()["id"]

    async def _configure():
        db = client.app.state.db
        await db_module.set_profile_layer(
            db, "workspace", "singleton", fields={"auto_worktrees": 0},
        )

    asyncio.run(_configure())

    result = _result(_mcp_call(client, "start_session", {
        "project_id": pid, "session_name": "89a06e40-start-binding-configured",
    }))
    binding = result["profile_binding"]
    assert binding is not None
    assert binding["generation_key"].startswith("sha256:")
