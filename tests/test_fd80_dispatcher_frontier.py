"""Tests for dynamic dispatcher frontier admission + failure re-frontiering
(item 272d8f2c, split from fd80f104).

``Dispatcher.dispatch_once`` used to dispatch ONLY ``groups[0]`` from
``get_parallelizable_groups`` — any leftover ``effective_parallelism``
capacity that ``groups[0]`` alone couldn't fill (because it was smaller than
the cap, or because quarantine / a merger-lock skip / a failed enqueue took
one of its members out of play THIS pass) was simply left idle, even when a
later group (``groups[1]``, ``groups[2]``, ...) held a genuinely
conflict-free candidate.

This file exercises the fix: dispatch_once now walks every group in the
server's own order, independently re-verifying conflict-freeness against
what THIS pass has actually admitted (never blindly trusting that two
different groups are mutually disjoint — they are not guaranteed to be), so
a skip anywhere genuinely reopens the frontier for a later candidate.

enqueue_claude_task is always mocked (or replaced with a per-test fake) so
NO real ``claude -p`` process ever spawns.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from meridian import db as db_module
from meridian.dispatcher import Dispatcher


@pytest_asyncio.fixture
async def project(db):
    proj = await db_module.create_project(db, "dispatch-frontier-proj")
    return proj["id"]


class _FakeEnqueue:
    """Records enqueue calls; returns a fake pending task. Never spawns."""

    def __init__(self):
        self.calls: list[dict] = []

    async def __call__(self, db, session_id, project_id, prompt, **kwargs):
        self.calls.append(
            {"session_id": session_id, "project_id": project_id, "prompt": prompt}
        )
        return {"id": f"task-{len(self.calls)}", "status": "pending"}


class _FlakyEnqueue:
    """Like _FakeEnqueue, but raises for any prompt containing one of the
    given (case-sensitive) substrings — used to simulate one item's enqueue
    failing while others in the same pass succeed."""

    def __init__(self, fail_substrings: "set[str]"):
        self._fail = set(fail_substrings)
        self.calls: list[dict] = []

    async def __call__(self, db, session_id, project_id, prompt, **kwargs):
        if any(s in prompt for s in self._fail):
            raise RuntimeError("simulated enqueue failure")
        self.calls.append(
            {"session_id": session_id, "project_id": project_id, "prompt": prompt}
        )
        return {"id": f"task-{len(self.calls)}", "status": "pending"}


def _titles(fake: "_FakeEnqueue | _FlakyEnqueue") -> list[str]:
    return [c["prompt"] for c in fake.calls]


# ---------------------------------------------------------------------------
# Real-board integration: quarantine on a groups[0] member reopens the
# frontier for a groups[1] item that only conflicted with the quarantined one.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_quarantine_on_first_group_admits_second_group_item(db, project):
    """A(urgent, file:x), B(high, file:x) conflicts with A, C(normal, file:y)
    is disjoint from both. The server colors groups[0]=[A, C],
    groups[1]=[B] (B conflicted with A when it was colored). Quarantining A
    frees a real slot within groups[0]'s own effective_parallelism ceiling
    (== len(groups[0]) == 2) — B, which only ever conflicted with A, must
    fill it in THIS pass rather than waiting for the next one.
    """
    await db_module.add_sprint_item(
        db, project, "v1", "Item A", touches_resources=["file:x"], priority="urgent",
    )
    await db_module.add_sprint_item(
        db, project, "v1", "Item B", touches_resources=["file:x"], priority="high",
    )
    await db_module.add_sprint_item(
        db, project, "v1", "Item C", touches_resources=["file:y"], priority="normal",
    )

    items = await db_module.get_sprint_items(db, project)
    a_id = next(i["id"] for i in items if i["title"] == "Item A")

    # Sanity: confirm the server really does color A+C into groups[0] and B
    # into groups[1] before asserting anything about dispatch behavior.
    plan = await db_module.get_parallelizable_groups(db, project, "v1")
    group_titles = [[it["title"] for it in g] for g in plan["groups"]]
    assert group_titles[0] == ["Item A", "Item C"]
    assert group_titles[1] == ["Item B"]
    assert plan["resource_safe_capacity"] == 2

    async def _quarantine_a(db_, pid, *, version=None, items=None, signals=None):
        return {"run_stop": False, "run_stop_reason": None, "quarantined_item_ids": [a_id]}

    fake = _FakeEnqueue()
    disp = Dispatcher(db, project, enqueue_fn=fake, evaluate_blockers_fn=_quarantine_a)

    enqueued = await disp.dispatch_once()

    assert len(enqueued) == 2
    prompts = _titles(fake)
    assert any("Item C" in p for p in prompts)
    assert any("Item B" in p for p in prompts)
    assert not any("Item A" in p for p in prompts)
    # Capacity was fully spent (2 == resource_safe_capacity) even though
    # groups[0] itself only contributed one admitted item.
    assert disp.last_parallelism["resource_safe_capacity"] == 2


# ---------------------------------------------------------------------------
# Injected-groups unit tests: precise, deterministic control over what
# dispatch_once is handed, independent of the real coloring algorithm.
# ---------------------------------------------------------------------------


def _groups_fn(groups: list[list[dict]]):
    async def _fn(db_, pid, version):
        return {"groups": groups}
    return _fn


@pytest.mark.asyncio
async def test_failed_enqueue_reopens_frontier_same_pass(db, project):
    """i1 and i2 share groups[0] (disjoint resources); i3 sits alone in
    groups[1] and conflicts with i1 only. i1's enqueue call raises — its
    resource must never be reserved, so i3 gets admitted in THIS SAME pass
    to spend the capacity i1's failure freed up.
    """
    groups = [
        [
            {"id": "i1", "title": "I1", "resources": ["file:a"]},
            {"id": "i2", "title": "I2", "resources": ["file:b"]},
        ],
        [{"id": "i3", "title": "I3", "resources": ["file:a"]}],
    ]
    fake = _FlakyEnqueue(fail_substrings={"I1"})
    disp = Dispatcher(db, project, enqueue_fn=fake, get_groups_fn=_groups_fn(groups))

    enqueued = await disp.dispatch_once()

    assert len(enqueued) == 2
    prompts = _titles(fake)
    assert any("I2" in p for p in prompts)
    assert any("I3" in p for p in prompts)
    assert not any("I1" in p for p in prompts)
    # i1 was never marked dispatched, so a later pass could retry it.
    assert "i1" not in disp._dispatched
    assert "i2" in disp._dispatched
    assert "i3" in disp._dispatched


@pytest.mark.asyncio
async def test_conflicting_second_group_item_not_admitted_when_capacity_full(db, project):
    """Negative/regression case: groups[0] alone fills the pass's entire
    capacity (== len(groups[0])) without any skip, so a groups[1] item that
    conflicts with groups[0] must NEVER be dispatched this pass — frontier
    admission only fills capacity a skip actually freed, it never lets a
    later group crowd out an uncontested earlier one.
    """
    groups = [
        [{"id": "i1", "title": "I1", "resources": ["file:a"]}],
        [{"id": "i2", "title": "I2", "resources": ["file:a"]}],
    ]
    fake = _FakeEnqueue()
    disp = Dispatcher(db, project, enqueue_fn=fake, get_groups_fn=_groups_fn(groups))

    enqueued = await disp.dispatch_once()

    assert len(enqueued) == 1
    assert "I1" in fake.calls[0]["prompt"]
    assert "i2" not in disp._dispatched


@pytest.mark.asyncio
async def test_three_groups_all_conflict_free_fill_capacity_in_order(db, project):
    """Three singleton groups, pairwise disjoint resources. groups[0]'s size
    is only 1, so effective_parallelism (== resource_safe_capacity) caps
    this pass at exactly 1 — frontier admission must never dispatch MORE
    than groups[0]'s size in a single pass, even when later groups hold
    additional, fully conflict-free candidates.
    """
    groups = [
        [{"id": "i1", "title": "I1", "resources": ["file:a"]}],
        [{"id": "i2", "title": "I2", "resources": ["file:b"]}],
        [{"id": "i3", "title": "I3", "resources": ["file:c"]}],
    ]
    fake = _FakeEnqueue()
    disp = Dispatcher(db, project, enqueue_fn=fake, get_groups_fn=_groups_fn(groups))

    enqueued = await disp.dispatch_once()

    assert len(enqueued) == 1
    assert "I1" in fake.calls[0]["prompt"]
    assert disp.last_parallelism["resource_safe_capacity"] == 1


@pytest.mark.asyncio
async def test_host_limit_still_bounds_admission_across_groups(db, project):
    """host_limit remains a hard ceiling on effective_parallelism even once
    frontier admission can reach into later groups — a bigger groups[0] and
    a filled groups[1] must not let the total exceed host_limit.
    """
    groups = [
        [
            {"id": "a1", "title": "A1", "resources": ["file:1"]},
            {"id": "a2", "title": "A2", "resources": ["file:2"]},
            {"id": "a3", "title": "A3", "resources": ["file:3"]},
        ],
        [{"id": "b1", "title": "B1", "resources": ["file:4"]}],
    ]
    fake = _FakeEnqueue()
    disp = Dispatcher(
        db, project, enqueue_fn=fake, get_groups_fn=_groups_fn(groups), host_limit=1,
    )

    enqueued = await disp.dispatch_once()

    assert len(enqueued) == 1
    assert "A1" in fake.calls[0]["prompt"]
    assert disp.last_parallelism["host_limit"] == 1
    assert disp.last_parallelism["effective_parallelism"] == 1


@pytest.mark.asyncio
async def test_undeclared_item_never_joins_after_something_else_admitted(db, project):
    """g1 (declared, file:m) and k1 (declared, file:n) share groups[0], so
    effective_parallelism == 2. An undeclared item u1 sits in groups[1].
    Quarantining k1 leaves one real slot open (in_flight=1 < cap=2) — but
    u1's footprint can't be proven disjoint from anything, so it must NOT
    fill that slot even though it is otherwise dependency-satisfied and
    unquarantined. (de730a25's "undeclared item is its own sequential
    group" invariant, re-applied dynamically by dispatch_once itself.)
    """
    groups = [
        [
            {"id": "g1", "title": "G1", "resources": ["file:m"]},
            {"id": "k1", "title": "K1", "resources": ["file:n"]},
        ],
        [{"id": "u1", "title": "U1", "resources": []}],
    ]

    async def _quarantine_k1(db_, pid, *, version=None, items=None, signals=None):
        return {"run_stop": False, "run_stop_reason": None, "quarantined_item_ids": ["k1"]}

    fake = _FakeEnqueue()
    disp = Dispatcher(
        db, project, enqueue_fn=fake, get_groups_fn=_groups_fn(groups),
        evaluate_blockers_fn=_quarantine_k1,
    )

    enqueued = await disp.dispatch_once()

    assert len(enqueued) == 1
    assert "G1" in fake.calls[0]["prompt"]
    assert "u1" not in disp._dispatched
    assert "k1" not in disp._dispatched


@pytest.mark.asyncio
async def test_declared_item_never_joins_after_undeclared_admitted(db, project):
    """White-box safety-net test: even in a (synthetic, not server-real)
    ordering where an undeclared item precedes a declared one in the SAME
    group, dispatch_once's own admission logic must still refuse to
    co-schedule the declared item once the undeclared one is running —
    dispatch_once never assumes anything about server group shape beyond
    what get_parallelizable_groups actually documents.
    """
    groups = [
        [
            {"id": "u1", "title": "U1", "resources": []},
            {"id": "d1", "title": "D1", "resources": ["file:z"]},
        ],
    ]
    fake = _FakeEnqueue()
    disp = Dispatcher(db, project, enqueue_fn=fake, get_groups_fn=_groups_fn(groups))

    enqueued = await disp.dispatch_once()

    assert len(enqueued) == 1
    assert "U1" in fake.calls[0]["prompt"]
    assert "d1" not in disp._dispatched


@pytest.mark.asyncio
async def test_dedup_still_applies_across_groups_on_next_pass(db, project):
    """An item admitted via frontier fill from a later group must still be
    tracked in self._dispatched exactly like a groups[0] item — a
    subsequent pass must not re-enqueue it.
    """
    groups = [
        [{"id": "i1", "title": "I1", "resources": ["file:a"]}],
        [{"id": "i2", "title": "I2", "resources": ["file:b"]}],
    ]
    fake = _FakeEnqueue()
    disp = Dispatcher(db, project, enqueue_fn=fake, get_groups_fn=_groups_fn(groups))

    first = await disp.dispatch_once()
    assert len(first) == 1  # capped at resource_safe_capacity == len(groups[0]) == 1
    assert "i1" in disp._dispatched

    second = await disp.dispatch_once()
    assert second == []
    assert len(fake.calls) == 1
