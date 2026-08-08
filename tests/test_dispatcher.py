"""Tests for the autonomous dispatcher daemon (item 57f7f7ba).

The dispatcher must:
  * dispatch a parallelizable group via the enqueue primitive,
  * never dispatch the same item twice (dedup),
  * wake immediately on a board_change trigger event,
  * bound concurrency at max_in_flight,
  * and — critically — NOT start in the server lifespan unless
    MERIDIAN_DISPATCHER_ENABLED == "1".

enqueue_claude_task is always mocked so NO real `claude -p` process spawns.
"""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio

from meridian import db as db_module
from meridian import dispatcher as dispatcher_module
from meridian.dispatcher import Dispatcher, is_enabled, start_dispatcher_if_enabled


@pytest_asyncio.fixture
async def project(db):
    proj = await db_module.create_project(db, "dispatch-proj")
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


# --- is_enabled / guardrail -------------------------------------------------


def test_is_enabled_default_off(monkeypatch):
    monkeypatch.delenv(dispatcher_module.ENABLE_ENV_VAR, raising=False)
    assert is_enabled() is False


@pytest.mark.parametrize("val", ["0", "true", "TRUE", "yes", "", "on"])
def test_is_enabled_strict(monkeypatch, val):
    monkeypatch.setenv(dispatcher_module.ENABLE_ENV_VAR, val)
    assert is_enabled() is False


def test_is_enabled_one(monkeypatch):
    monkeypatch.setenv(dispatcher_module.ENABLE_ENV_VAR, "1")
    assert is_enabled() is True


def test_start_if_enabled_noop_when_unset(monkeypatch):
    """GUARDRAIL: default OFF — returns None and starts nothing."""
    monkeypatch.delenv(dispatcher_module.ENABLE_ENV_VAR, raising=False)

    class _App:
        class state:  # noqa: N801
            pass

    result = start_dispatcher_if_enabled(_App, object(), "pid")
    assert result is None
    assert getattr(_App.state, "dispatcher", None) in (None,)


@pytest.mark.asyncio
async def test_start_if_enabled_starts_when_on(monkeypatch, db, project):
    monkeypatch.setenv(dispatcher_module.ENABLE_ENV_VAR, "1")
    fake = _FakeEnqueue()

    class _App:
        class state:  # noqa: N801
            pass

    disp = start_dispatcher_if_enabled(
        _App, db, project, interval=0.05, enqueue_fn=fake
    )
    assert disp is not None
    assert _App.state.dispatcher is disp
    await disp.stop()


# --- dispatch_once ----------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_once_enqueues_group(db, project):
    await db_module.add_sprint_item(
        db, project, "v1", "Item A", touches_resources=["file:fileA"]
    )
    await db_module.add_sprint_item(
        db, project, "v1", "Item B", touches_resources=["file:fileB"]
    )
    fake = _FakeEnqueue()
    disp = Dispatcher(db, project, enqueue_fn=fake)

    enqueued = await disp.dispatch_once()
    # Two non-conflicting items land in the first parallel group.
    assert len(enqueued) == 2
    assert len(fake.calls) == 2
    prompts = " ".join(c["prompt"] for c in fake.calls)
    assert "Item A" in prompts and "Item B" in prompts
    # All enqueued under the same lazily-created dispatcher session.
    sids = {c["session_id"] for c in fake.calls}
    assert len(sids) == 1


@pytest.mark.asyncio
async def test_dispatch_once_empty_board(db, project):
    fake = _FakeEnqueue()
    disp = Dispatcher(db, project, enqueue_fn=fake)
    assert await disp.dispatch_once() == []
    assert fake.calls == []


@pytest.mark.asyncio
async def test_dispatch_dedups_items(db, project):
    await db_module.add_sprint_item(
        db, project, "v1", "Only Item", touches_resources=["file:x"]
    )
    fake = _FakeEnqueue()
    disp = Dispatcher(db, project, enqueue_fn=fake)

    first = await disp.dispatch_once()
    assert len(first) == 1
    # Second pass: same item still pending on the board, but already dispatched.
    second = await disp.dispatch_once()
    assert second == []
    assert len(fake.calls) == 1


@pytest.mark.asyncio
async def test_max_in_flight_bounds_concurrency(db, project):
    for i in range(5):
        await db_module.add_sprint_item(
            db, project, "v1", f"Item {i}", touches_resources=[f"file:r{i}"]
        )
    fake = _FakeEnqueue()
    disp = Dispatcher(db, project, enqueue_fn=fake, max_in_flight=2)

    enqueued = await disp.dispatch_once()
    assert len(enqueued) == 2
    # A further pass enqueues nothing more — we are at the cap.
    assert await disp.dispatch_once() == []
    assert len(fake.calls) == 2


@pytest.mark.asyncio
async def test_dispatch_skips_failed_enqueue(db, project):
    await db_module.add_sprint_item(
        db, project, "v1", "Boom", touches_resources=["file:x"]
    )

    async def _boom(*a, **k):
        raise RuntimeError("spawn failed")

    disp = Dispatcher(db, project, enqueue_fn=_boom)
    enqueued = await disp.dispatch_once()
    assert enqueued == []
    # Not marked dispatched, so a later (working) pass can retry.
    fake = _FakeEnqueue()
    disp._enqueue = fake
    again = await disp.dispatch_once()
    assert len(again) == 1


# --- run loop + trigger -----------------------------------------------------


@pytest.mark.asyncio
async def test_run_loop_dispatches_then_trigger(db, project):
    await db_module.add_sprint_item(
        db, project, "v1", "Loop Item", touches_resources=["file:x"]
    )
    fake = _FakeEnqueue()
    # Long interval so the only fast pass beyond the first is via trigger().
    disp = Dispatcher(db, project, interval=100.0, enqueue_fn=fake)
    disp.start()

    # First immediate pass dispatches the item.
    for _ in range(50):
        await asyncio.sleep(0.01)
        if fake.calls:
            break
    assert len(fake.calls) == 1

    # Add a new item and fire the board_change trigger for an immediate pass.
    await db_module.add_sprint_item(
        db, project, "v1", "Late Item", touches_resources=["file:y"]
    )
    disp.trigger()

    for _ in range(50):
        await asyncio.sleep(0.01)
        if len(fake.calls) >= 2:
            break
    assert len(fake.calls) == 2
    assert any("Late Item" in c["prompt"] for c in fake.calls)

    await disp.stop()
    assert disp._task is None


@pytest.mark.asyncio
async def test_notify_board_change_alias(db, project):
    disp = Dispatcher(db, project)
    assert disp.notify_board_change == disp.trigger
    disp.notify_board_change()
    assert disp._wake.is_set()


@pytest.mark.asyncio
async def test_start_is_idempotent(db, project):
    fake = _FakeEnqueue()
    disp = Dispatcher(db, project, interval=100.0, enqueue_fn=fake)
    t1 = disp.start()
    t2 = disp.start()
    assert t1 is t2
    await disp.stop()


@pytest.mark.asyncio
async def test_stop_without_start_is_safe(db, project):
    disp = Dispatcher(db, project)
    await disp.stop()  # no task — must not raise


@pytest.mark.asyncio
async def test_run_loop_survives_pass_error(db, project):
    """A failing dispatch pass must not kill the loop."""
    calls = {"n": 0}

    async def _flaky_groups(db_, pid, version):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        return {"groups": []}

    disp = Dispatcher(
        db, project, interval=0.02, get_groups_fn=_flaky_groups
    )
    disp.start()
    for _ in range(50):
        await asyncio.sleep(0.01)
        if calls["n"] >= 2:
            break
    await disp.stop()
    assert calls["n"] >= 2  # loop kept going after the first error


def test_worker_prompt_includes_resources():
    item = {"id": "abc123", "title": "Do thing", "resources": ["fileA", "fileB"]}
    prompt = dispatcher_module._worker_prompt(item, "proj-1")
    assert "abc123" in prompt
    assert "Do thing" in prompt
    assert "fileA" in prompt and "fileB" in prompt
    assert "proj-1" in prompt


def test_worker_prompt_no_resources():
    item = {"id": "abc123", "title": "Do thing"}
    prompt = dispatcher_module._worker_prompt(item, "proj-1")
    assert "abc123" in prompt
    assert "resources" not in prompt.lower()


# --- b108f2e0: typed blocker triage integration ------------------------------


@pytest.mark.asyncio
async def test_dispatch_once_skips_quarantined_item_continues_others(db, project):
    """A quarantined item is skipped this pass, but a disjoint item in the
    same group still dispatches — the core fix: one blocked item never
    stalls an otherwise-executable autonomous run.
    """
    await db_module.add_sprint_item(
        db, project, "v1", "Blocked Item", touches_resources=["file:blocked"]
    )
    await db_module.add_sprint_item(
        db, project, "v1", "Fine Item", touches_resources=["file:fine"]
    )
    fake = _FakeEnqueue()

    async def _fake_evaluate(db_, pid, *, version=None, items=None, signals=None):
        return {
            "run_stop": False,
            "run_stop_reason": None,
            "quarantined_item_ids": ["will-not-match-but-set-below"],
        }

    disp = Dispatcher(db, project, enqueue_fn=fake, evaluate_blockers_fn=_fake_evaluate)

    # Resolve the real blocked item's id from the live board, then re-point
    # the fake evaluator at it (mirrors how a real evaluate_board_blockers
    # call would report it).
    items = await db_module.get_sprint_items(db, project)
    blocked_id = next(i["id"] for i in items if i["title"] == "Blocked Item")

    async def _fake_evaluate2(db_, pid, *, version=None, items=None, signals=None):
        return {"run_stop": False, "run_stop_reason": None, "quarantined_item_ids": [blocked_id]}

    disp._evaluate_blockers = _fake_evaluate2

    enqueued = await disp.dispatch_once()
    assert len(enqueued) == 1
    assert len(fake.calls) == 1
    assert "Fine Item" in fake.calls[0]["prompt"]
    assert disp._stopped is False
    assert disp.last_blocker_decision["quarantined_item_ids"] == [blocked_id]


@pytest.mark.asyncio
async def test_dispatch_once_halts_on_fail_closed_blocker(db, project):
    """A fail-closed blocker (verified_security / integrity_corruption /
    run_global_blocker, or an explicit run_stop policy) stops the dispatcher
    entirely and enqueues nothing.
    """
    await db_module.add_sprint_item(
        db, project, "v1", "Item A", touches_resources=["file:a"]
    )
    fake = _FakeEnqueue()

    async def _fake_evaluate(db_, pid, *, version=None, items=None, signals=None):
        return {
            "run_stop": True,
            "run_stop_reason": "fail_closed_blocker:verified_security;items:x",
            "quarantined_item_ids": [],
        }

    disp = Dispatcher(db, project, enqueue_fn=fake, evaluate_blockers_fn=_fake_evaluate)
    enqueued = await disp.dispatch_once()
    assert enqueued == []
    assert fake.calls == []
    assert disp._stopped is True


@pytest.mark.asyncio
async def test_dispatch_once_survives_blocker_evaluation_failure(db, project):
    """A blocker-evaluation failure degrades to unfiltered dispatch — it
    must never itself stop the dispatcher (only an ACTUAL fail-closed
    classification does that).
    """
    await db_module.add_sprint_item(
        db, project, "v1", "Item A", touches_resources=["file:a"]
    )
    fake = _FakeEnqueue()

    async def _boom_evaluate(db_, pid, *, version=None, items=None, signals=None):
        raise RuntimeError("evaluation blew up")

    disp = Dispatcher(db, project, enqueue_fn=fake, evaluate_blockers_fn=_boom_evaluate)
    enqueued = await disp.dispatch_once()
    assert len(enqueued) == 1
    assert disp._stopped is False


@pytest.mark.asyncio
async def test_dispatch_once_uses_real_evaluate_board_blockers_by_default(db, project):
    """End-to-end (no injected evaluator): an empty-scope CRITICAL item is
    quarantined by the REAL db.evaluate_board_blockers, while an
    independent, well-scoped item still dispatches.
    """
    await db_module.add_sprint_item(
        db, project, "v1", "CRITICAL empty item", notes="", priority="urgent",
    )
    await db_module.add_sprint_item(
        db, project, "v1", "Fine Item", touches_resources=["file:fine"]
    )
    fake = _FakeEnqueue()
    disp = Dispatcher(db, project, enqueue_fn=fake)
    enqueued = await disp.dispatch_once()
    assert len(enqueued) == 1
    assert "Fine Item" in fake.calls[0]["prompt"]
    assert disp._stopped is False


# --- 315b0a63: optional worker-lease sweep hook ------------------------------


@pytest.mark.asyncio
async def test_dispatch_once_skips_lease_sweep_when_not_wired(db, project):
    """Default (lease_sweep_fn=None) — no hook call, dispatch behaves
    exactly as before this feature existed."""
    fake = _FakeEnqueue()
    disp = Dispatcher(db, project, enqueue_fn=fake)
    await disp.dispatch_once()
    assert disp.last_lease_sweep is None


@pytest.mark.asyncio
async def test_dispatch_once_calls_lease_sweep_when_wired(db, project):
    calls = []

    def _sweep():
        calls.append(1)
        return [{"run_id": "orphan-1"}]

    fake = _FakeEnqueue()
    disp = Dispatcher(db, project, enqueue_fn=fake, lease_sweep_fn=_sweep)
    await disp.dispatch_once()
    assert calls == [1]
    assert disp.last_lease_sweep == [{"run_id": "orphan-1"}]


@pytest.mark.asyncio
async def test_dispatch_once_survives_lease_sweep_failure(db, project):
    """A broken lease-sweep hook must not break dispatch — same contract
    as blocker-policy evaluation failures."""
    await db_module.add_sprint_item(
        db, project, "v1", "Item A", touches_resources=["file:a"]
    )

    def _boom_sweep():
        raise RuntimeError("sweep blew up")

    fake = _FakeEnqueue()
    disp = Dispatcher(db, project, enqueue_fn=fake, lease_sweep_fn=_boom_sweep)
    enqueued = await disp.dispatch_once()
    assert len(enqueued) == 1
    assert disp._stopped is False


@pytest.mark.asyncio
async def test_dispatch_once_lease_sweep_empty_result_recorded(db, project):
    fake = _FakeEnqueue()
    disp = Dispatcher(db, project, enqueue_fn=fake, lease_sweep_fn=lambda: [])
    await disp.dispatch_once()
    assert disp.last_lease_sweep == []


@pytest.mark.asyncio
async def test_start_if_enabled_wires_default_lease_sweep(monkeypatch, db, project, tmp_path):
    """start_dispatcher_if_enabled wires a real, working default hook once
    the dispatcher itself is opted in — read-only (report_unowned_
    survivors), so it adds no new side effects for a host that never
    registers any lease. Uses a tmp_path-isolated registry (and resets the
    process_registry module singleton before/after) so this test never
    reads/pollutes a real home directory or a sibling test's state."""
    from meridian import process_registry as process_registry_module

    monkeypatch.setenv(dispatcher_module.ENABLE_ENV_VAR, "1")
    monkeypatch.setenv("MERIDIAN_LEASE_REGISTRY_PATH", str(tmp_path / "leases.json"))
    process_registry_module.reset_default_broker()
    fake = _FakeEnqueue()

    class _App:
        class state:  # noqa: N801
            pass

    try:
        disp = start_dispatcher_if_enabled(_App, db, project, interval=0.05, enqueue_fn=fake)
        assert disp is not None
        assert disp._lease_sweep is not None
        result = disp._lease_sweep()
        assert result == []  # nothing registered — report_unowned_survivors is empty
        await disp.stop()
    finally:
        process_registry_module.reset_default_broker()


@pytest.mark.asyncio
async def test_start_if_enabled_explicit_none_lease_sweep_opts_out(monkeypatch, db, project):
    """An explicit lease_sweep_fn=None from the caller wins over the
    default wiring — setdefault() must not clobber an explicit opt-out."""
    monkeypatch.setenv(dispatcher_module.ENABLE_ENV_VAR, "1")
    fake = _FakeEnqueue()

    class _App:
        class state:  # noqa: N801
            pass

    disp = start_dispatcher_if_enabled(
        _App, db, project, interval=0.05, enqueue_fn=fake, lease_sweep_fn=None,
    )
    assert disp is not None
    assert disp._lease_sweep is None
    await disp.stop()
