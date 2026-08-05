"""Tests for sprint item 99c0c1be — separate requested parallelism from
host-enforced agent capacity, with a configurable target up to 16.

Covers the three layers touched by the item:

  * meridian/executor_config.py — normalize_parallelism_target,
    resolve_parallelism (the deterministic min() + limiting_reason model).
  * meridian/dispatcher.py — Dispatcher wires the model into dispatch_once's
    per-pass concurrency cap, recomputed every pass against the current
    first parallel-safe group.
  * meridian/db/sprint_items.py — get_parallelizable_groups surfaces the
    same fields (requested_parallelism, effective_parallelism, host_limit,
    configured_target, resource_safe_capacity, limiting_reason) so sprint
    grouping/handoff/diagnostics never have to re-derive the model.

Explicit scenarios required by the item: target 14, target 16, a lower
host-enforced limit, missing host metadata (must NOT collapse to serial),
and resource-conflict reduction (fewer disjoint items than the target).
"""
from __future__ import annotations

import pytest
import pytest_asyncio

from meridian import db as db_module
from meridian import dispatcher as dispatcher_module
from meridian import executor_config as ec


# ---------------------------------------------------------------------------
# executor_config.normalize_parallelism_target
# ---------------------------------------------------------------------------

def test_normalize_parallelism_target_clamps_to_ceiling():
    assert ec.normalize_parallelism_target(16) == 16
    assert ec.normalize_parallelism_target(20) == 16  # above ceiling -> clamped, not rejected
    assert ec.normalize_parallelism_target(1) == 1


@pytest.mark.parametrize("bad", [0, -5, None, "not-a-number", [], {}])
def test_normalize_parallelism_target_falls_back_to_default(bad):
    assert ec.normalize_parallelism_target(bad) == ec.DEFAULT_PARALLELISM_TARGET


# ---------------------------------------------------------------------------
# executor_config.resolve_parallelism — the deterministic min() model
# ---------------------------------------------------------------------------

def test_resolve_parallelism_target_14_no_other_constraint():
    result = ec.resolve_parallelism(14, configured_target=14)
    # requested == configured_target (tie): configured_target wins the label
    # per the documented priority order (host_limit > configured_target >
    # resource_safe_capacity > requested_parallelism).
    assert result == {
        "requested_parallelism": 14,
        "configured_target": 14,
        "host_limit": None,
        "resource_safe_capacity": None,
        "effective_parallelism": 14,
        "limiting_reason": "configured_target",
    }


def test_resolve_parallelism_target_16_permitted_when_nothing_else_constrains():
    """16 is the new ceiling -- permitted in full when the host/client allows it."""
    result = ec.resolve_parallelism(16, configured_target=16, resource_safe_capacity=16)
    assert result["effective_parallelism"] == 16
    assert result["configured_target"] == 16


def test_resolve_parallelism_lower_host_limit_always_wins():
    """A known host_limit below everything else is NEVER exceeded -- the
    'never claim to override a lower host-enforced limit' rule."""
    result = ec.resolve_parallelism(
        16, configured_target=16, host_limit=5, resource_safe_capacity=16
    )
    assert result["effective_parallelism"] == 5
    assert result["limiting_reason"] == "host_limit"
    assert result["host_limit"] == 5


def test_resolve_parallelism_missing_host_metadata_does_not_serialize():
    """host_limit=None (unknown) must be EXCLUDED from the min(), never
    coerced into a de-facto cap of 1 — an unrelated vendor UI cap being
    unreported must not serialize genuinely disjoint, resource-safe work."""
    result = ec.resolve_parallelism(
        16, configured_target=16, host_limit=None, resource_safe_capacity=16
    )
    assert result["host_limit"] is None
    assert result["effective_parallelism"] == 16
    assert result["limiting_reason"] in ("configured_target", "requested_parallelism")


def test_resolve_parallelism_resource_conflict_reduces_effective_value():
    """Even with a high target and no host limit, a small resource-safe batch
    (few genuinely disjoint items) caps the effective value below the target."""
    result = ec.resolve_parallelism(
        16, configured_target=16, resource_safe_capacity=3
    )
    assert result["effective_parallelism"] == 3
    assert result["limiting_reason"] == "resource_safe_capacity"


def test_resolve_parallelism_requested_below_everything_else():
    result = ec.resolve_parallelism(2, configured_target=16, host_limit=10, resource_safe_capacity=10)
    assert result["effective_parallelism"] == 2
    assert result["limiting_reason"] == "requested_parallelism"


def test_resolve_parallelism_invalid_requested_defaults_to_one():
    result = ec.resolve_parallelism(None, configured_target=4)
    assert result["requested_parallelism"] == 1
    result2 = ec.resolve_parallelism(-3, configured_target=4)
    assert result2["requested_parallelism"] == 1


def test_resolve_parallelism_ignores_non_positive_host_limit_and_capacity():
    # 0/negative/garbage host_limit or resource_safe_capacity are treated the
    # same as "unknown" -- excluded, not coerced to some floor value.
    result = ec.resolve_parallelism(
        10, configured_target=10, host_limit=0, resource_safe_capacity=-1
    )
    assert result["host_limit"] is None
    assert result["resource_safe_capacity"] is None
    assert result["effective_parallelism"] == 10


# ---------------------------------------------------------------------------
# Dispatcher wiring — dispatch_once uses the SAME deterministic model
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def db():
    conn = await db_module.init_db(":memory:")
    yield conn
    await conn.close()


@pytest_asyncio.fixture
async def project(db):
    proj = await db_module.create_project(db, "parallelism-dispatch-proj")
    return proj["id"]


class _FakeEnqueue:
    def __init__(self):
        self.calls: list[dict] = []

    async def __call__(self, db, session_id, project_id, prompt, **kwargs):
        self.calls.append({"session_id": session_id, "project_id": project_id})
        return {"id": f"task-{len(self.calls)}", "status": "pending"}


async def _seed_disjoint_items(db, project_id, n: int):
    for i in range(n):
        await db_module.add_sprint_item(
            db, project_id, "v1", f"Item {i}", touches_resources=[f"file:r{i}.py"]
        )


@pytest.mark.asyncio
async def test_dispatcher_target_14(db, project):
    await _seed_disjoint_items(db, project, 20)
    fake = _FakeEnqueue()
    disp = dispatcher_module.Dispatcher(db, project, enqueue_fn=fake, max_in_flight=14)
    enqueued = await disp.dispatch_once()
    assert len(enqueued) == 14
    assert disp.last_parallelism["effective_parallelism"] == 14
    assert disp.last_parallelism["configured_target"] == 14
    assert disp.last_parallelism["limiting_reason"] == "configured_target"


@pytest.mark.asyncio
async def test_dispatcher_target_16(db, project):
    """16 is the new configurable ceiling: permitted in full when enough
    disjoint work exists and nothing else (host/resources) constrains it."""
    await _seed_disjoint_items(db, project, 20)
    fake = _FakeEnqueue()
    disp = dispatcher_module.Dispatcher(db, project, enqueue_fn=fake, max_in_flight=16)
    enqueued = await disp.dispatch_once()
    assert len(enqueued) == 16
    assert disp.last_parallelism["effective_parallelism"] == 16
    assert disp.last_parallelism["configured_target"] == 16
    assert disp.last_parallelism["host_limit"] is None


@pytest.mark.asyncio
async def test_dispatcher_lower_host_limit_caps_below_target(db, project):
    """A configured target of 16 must never be honored above a known,
    lower host_limit."""
    await _seed_disjoint_items(db, project, 20)
    fake = _FakeEnqueue()
    disp = dispatcher_module.Dispatcher(
        db, project, enqueue_fn=fake, max_in_flight=16, host_limit=6,
    )
    enqueued = await disp.dispatch_once()
    assert len(enqueued) == 6
    assert disp.last_parallelism["limiting_reason"] == "host_limit"
    assert disp.last_parallelism["host_limit"] == 6


@pytest.mark.asyncio
async def test_dispatcher_missing_host_metadata_still_reaches_target(db, project):
    """No host_limit reported (the default) must not serialize disjoint,
    resource-safe work down to one-at-a-time."""
    await _seed_disjoint_items(db, project, 20)
    fake = _FakeEnqueue()
    disp = dispatcher_module.Dispatcher(db, project, enqueue_fn=fake, max_in_flight=16)
    assert disp.host_limit is None
    enqueued = await disp.dispatch_once()
    assert len(enqueued) == 16
    assert disp.last_parallelism["host_limit"] is None


@pytest.mark.asyncio
async def test_dispatcher_resource_conflict_reduces_dispatch_below_target(db, project):
    """A high target (16) does not fan out beyond the number of genuinely
    disjoint (resource-conflict-free) items actually on the board."""
    await _seed_disjoint_items(db, project, 3)
    fake = _FakeEnqueue()
    disp = dispatcher_module.Dispatcher(db, project, enqueue_fn=fake, max_in_flight=16)
    enqueued = await disp.dispatch_once()
    assert len(enqueued) == 3
    assert disp.last_parallelism["effective_parallelism"] == 3
    assert disp.last_parallelism["limiting_reason"] == "resource_safe_capacity"


@pytest.mark.asyncio
async def test_dispatcher_default_unchanged_when_unconfigured(db, project):
    """Back-compat: an unconfigured Dispatcher (no kwargs beyond enqueue_fn)
    still behaves exactly as before this feature — cap is
    DEFAULT_PARALLELISM_TARGET (4), the historical DEFAULT_MAX_IN_FLIGHT."""
    await _seed_disjoint_items(db, project, 10)
    fake = _FakeEnqueue()
    disp = dispatcher_module.Dispatcher(db, project, enqueue_fn=fake)
    enqueued = await disp.dispatch_once()
    assert len(enqueued) == 4
    assert dispatcher_module.DEFAULT_MAX_IN_FLIGHT == 4


# ---------------------------------------------------------------------------
# get_parallelizable_groups — sprint grouping / diagnostics surfacing
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_parallelizable_groups_surfaces_parallelism_fields(db, project):
    await _seed_disjoint_items(db, project, 20)
    result = await db_module.get_parallelizable_groups(db, project)
    for key in (
        "requested_parallelism", "effective_parallelism", "host_limit",
        "configured_target", "resource_safe_capacity", "limiting_reason",
    ):
        assert key in result
    # No executor_config persisted -> falls back to the shared default (4),
    # same numeric ceiling the dispatcher used before this feature existed.
    assert result["configured_target"] == ec.DEFAULT_PARALLELISM_TARGET
    assert result["effective_parallelism"] == ec.DEFAULT_PARALLELISM_TARGET
    assert result["host_limit"] is None


@pytest.mark.asyncio
async def test_get_parallelizable_groups_picks_up_persisted_target_16(db, project):
    """configured_target defaults to the project's persisted
    executor_config.parallelism_target -- so callers (e.g. handoff.py) that
    invoke get_parallelizable_groups unmodified automatically see it."""
    await _seed_disjoint_items(db, project, 20)
    await db_module.set_executor_config(db, project, {"parallelism_target": 16})
    result = await db_module.get_parallelizable_groups(db, project)
    assert result["configured_target"] == 16
    assert result["effective_parallelism"] == 16
    assert result["limiting_reason"] == "configured_target"


@pytest.mark.asyncio
async def test_get_parallelizable_groups_explicit_kwarg_overrides_persisted(db, project):
    await _seed_disjoint_items(db, project, 20)
    await db_module.set_executor_config(db, project, {"parallelism_target": 16})
    result = await db_module.get_parallelizable_groups(db, project, configured_target=8)
    assert result["configured_target"] == 8
    assert result["effective_parallelism"] == 8


@pytest.mark.asyncio
async def test_get_parallelizable_groups_lower_host_limit(db, project):
    await _seed_disjoint_items(db, project, 20)
    result = await db_module.get_parallelizable_groups(
        db, project, configured_target=16, host_limit=7,
    )
    assert result["effective_parallelism"] == 7
    assert result["limiting_reason"] == "host_limit"


@pytest.mark.asyncio
async def test_get_parallelizable_groups_missing_host_metadata_not_serialized(db, project):
    """No host_limit passed -> excluded from the min(), never treated as 1
    -- wave planning must not serialize disjoint work over an unknown cap."""
    await _seed_disjoint_items(db, project, 20)
    result = await db_module.get_parallelizable_groups(db, project, configured_target=16)
    assert result["host_limit"] is None
    assert result["effective_parallelism"] == 16


@pytest.mark.asyncio
async def test_get_parallelizable_groups_resource_conflict_reduction(db, project):
    """Fewer genuinely disjoint items than the target reduces the effective
    value -- the coloring/grouping itself is unaffected by this feature."""
    await _seed_disjoint_items(db, project, 5)
    result = await db_module.get_parallelizable_groups(db, project, configured_target=16)
    assert result["resource_safe_capacity"] == 5
    assert result["effective_parallelism"] == 5
    assert result["limiting_reason"] == "resource_safe_capacity"
    # The coloring itself (groups/group_count) is unchanged by this feature.
    assert result["group_count"] == 1
    assert len(result["groups"][0]) == 5


@pytest.mark.asyncio
async def test_get_parallelizable_groups_empty_board_defaults_to_one(db, project):
    result = await db_module.get_parallelizable_groups(db, project)
    assert result["groups"] == []
    assert result["resource_safe_capacity"] is None
    assert result["effective_parallelism"] == 1


@pytest.mark.asyncio
async def test_get_parallelizable_groups_unknown_project_degrades_gracefully(db):
    """A bogus project_id must not make the parallelism diagnostics blow up
    the whole call -- get_executor_config raises ValueError internally, which
    is caught and treated as 'no configured_target' (falls back to default)."""
    result = await db_module.get_parallelizable_groups(db, "no-such-project-id")
    assert result["configured_target"] == ec.DEFAULT_PARALLELISM_TARGET
