"""Integration tests for sprint item 769e24a7 — wire active-lease accounting
into ``Dispatcher.dispatch_once`` so capacity actually releases on worker
completion.

Root cause this item fixes: ``Dispatcher.__init__`` already owned
``_active_leases`` / ``record_worker_lease`` / ``release_worker_lease``
(869d6198, see ``tests/test_869d6198_dispatcher_leases.py``), but
``dispatch_once`` never called them and still computed capacity from the
permanent, ever-growing ``_dispatched`` dedup ledger — so a long-running
dispatcher's effective concurrency silently throttled toward zero as workers
finished, since nothing ever freed a slot.

This file proves the fix end-to-end, per the item's own required coverage:
  * normal completion, failure, timeout, process death, cancellation,
    restart reconciliation
  * duplicate terminal notifications (idempotent release)
  * stale completion from an old attempt (attempt-id safety — a late signal
    for a superseded attempt must NEVER release a newer attempt's lease)
  * explicit retry / dedup preserved (an item already in ``_dispatched`` is
    never re-dispatched, even after its lease is released)
  * capacity reuse after a REAL worker terminal transition (the actual
    integration proof this item exists for)

Several tests spawn a real, tiny Python subprocess through the real
``meridian.enqueue.enqueue_claude_task`` / ``_run_worker`` — not a fake —
so the reconciliation path is proven against task_log rows those functions
themselves actually write, matching the rigor of
``tests/test_49e06bcb_lightweight_workers.py``'s own real-subprocess tests.
The process-death case mirrors ``tests/test_core.py::
test_watchdog_marks_dead_pid_failed``'s own "simulate the watchdog inline"
pattern and adds the Dispatcher-side reconciliation half that test does not
cover.
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest
import pytest_asyncio

from meridian import db as db_module
from meridian.dispatcher import Dispatcher
from meridian.enqueue import _run_worker, enqueue_claude_task

_OK_WORKER = [sys.executable, "-c", "import sys; sys.stdout.write('ok')"]
_FAIL_WORKER = [
    sys.executable, "-c", "import sys; sys.stderr.write('boom'); sys.exit(1)",
]
_SLOW_WORKER = [sys.executable, "-c", "import time; time.sleep(5)"]


@pytest_asyncio.fixture
async def project(db):
    proj = await db_module.create_project(db, "completion-proj")
    return proj["id"]


@pytest_asyncio.fixture
async def session(db, project):
    sess = await db_module.register_session(db, project, "s")
    return sess["id"]


def _groups_fn(groups: "list[list[dict]]"):
    async def _fn(db_, pid, version):
        return {"groups": groups}
    return _fn


class _FakeEnqueue:
    """Records enqueue calls; returns a fake pending task, never touches
    the real DB. Mirrors tests/test_dispatcher.py's own fake exactly."""

    def __init__(self):
        self.calls: "list[dict]" = []

    async def __call__(self, db, session_id, project_id, prompt, **kwargs):
        self.calls.append(
            {"session_id": session_id, "project_id": project_id, "prompt": prompt}
        )
        return {"id": f"task-{len(self.calls)}", "status": "pending"}


class _RealEnqueueSpy:
    """Wraps the REAL ``enqueue_claude_task`` so a dispatch_once pass writes
    a real task_log row (and, with ``worker_argv``, really spawns a
    subprocess), while forwarding whatever ``on_complete`` dispatch_once
    supplies so the push-notification path is exercised too."""

    def __init__(self, worker_argv, *, timeout: float = 10.0, wait: bool = True):
        self.worker_argv = worker_argv
        self.timeout = timeout
        self.wait = wait
        self.calls: "list[str]" = []

    async def __call__(self, db_, session_id, project_id, prompt, **kwargs):
        self.calls.append(prompt)
        return await enqueue_claude_task(
            db_, session_id, project_id, prompt,
            worker_argv=self.worker_argv,
            timeout=self.timeout,
            wait=self.wait,
            on_complete=kwargs.get("on_complete"),
        )


# ---------------------------------------------------------------------------
# reconcile_active_leases — direct, fast unit coverage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconcile_releases_on_done_status(db, project, session):
    """Normal completion."""
    task = await db_module.log_task(db, session, project, "work", "pending")
    await db_module.update_task(db, task["id"], status="done", description="ok")

    disp = Dispatcher(db, project)
    disp.record_worker_lease("item-done", task=task)

    released = await disp.reconcile_active_leases()
    assert released == [
        {"item_id": "item-done", "task_id": task["id"], "status": "done", "seq": 1}
    ]
    assert "item-done" not in disp._active_leases
    assert disp.last_released_lease == {"item_id": "item-done", "status": "done", "seq": 1}


@pytest.mark.asyncio
async def test_reconcile_releases_on_failed_status(db, project, session):
    """Explicit failure."""
    task = await db_module.log_task(db, session, project, "work", "pending")
    await db_module.update_task(db, task["id"], status="failed", description="boom")

    disp = Dispatcher(db, project)
    disp.record_worker_lease("item-fail", task=task)

    released = await disp.reconcile_active_leases()
    assert len(released) == 1
    assert released[0]["status"] == "failed"
    assert "item-fail" not in disp._active_leases


@pytest.mark.asyncio
async def test_reconcile_leaves_pending_or_in_progress_alone(db, project, session):
    pending_task = await db_module.log_task(db, session, project, "p", "pending")
    ip_task = await db_module.log_task(db, session, project, "ip", "pending")
    await db_module.update_task(db, ip_task["id"], status="in_progress")

    disp = Dispatcher(db, project)
    disp.record_worker_lease("item-pending", task=pending_task)
    disp.record_worker_lease("item-inprogress", task=ip_task)

    released = await disp.reconcile_active_leases()
    assert released == []
    assert set(disp._active_leases) == {"item-pending", "item-inprogress"}


@pytest.mark.asyncio
async def test_reconcile_skips_lease_with_missing_task_row(db, project):
    disp = Dispatcher(db, project)
    disp.record_worker_lease("item-ghost", task={"id": "does-not-exist-in-db"})

    released = await disp.reconcile_active_leases()
    assert released == []
    assert "item-ghost" in disp._active_leases  # left alone, never guessed at


@pytest.mark.asyncio
async def test_reconcile_skips_lease_with_no_task_id(db, project):
    disp = Dispatcher(db, project)
    disp.record_worker_lease("item-no-task", task=None)

    released = await disp.reconcile_active_leases()
    assert released == []
    assert "item-no-task" in disp._active_leases


@pytest.mark.asyncio
async def test_reconcile_duplicate_terminal_notifications_idempotent(db, project, session):
    task = await db_module.log_task(db, session, project, "work", "pending")
    await db_module.update_task(db, task["id"], status="done", description="done!")

    disp = Dispatcher(db, project)
    disp.record_worker_lease("item-dup", task=task)

    first = await disp.reconcile_active_leases()
    assert len(first) == 1
    assert disp.last_released_lease["item_id"] == "item-dup"

    # Calling reconcile again (e.g. next scheduled pass, or a duplicate
    # watchdog notification) must be a true no-op — nothing to release, no
    # error, no corruption of the diagnostics from the first release.
    second = await disp.reconcile_active_leases()
    assert second == []
    assert disp.last_released_lease["item_id"] == "item-dup"
    assert "item-dup" not in disp._active_leases


@pytest.mark.asyncio
async def test_reconcile_does_not_touch_dispatched_dedup_ledger(db, project, session):
    task = await db_module.log_task(db, session, project, "work", "pending")
    await db_module.update_task(db, task["id"], status="done")

    disp = Dispatcher(db, project)
    disp._dispatched.add("item-x")  # simulate dispatch_once's dedup marker
    disp.record_worker_lease("item-x", task=task)

    await disp.reconcile_active_leases()
    assert "item-x" not in disp._active_leases
    assert "item-x" in disp._dispatched  # permanent, unaffected by release


# ---------------------------------------------------------------------------
# attempt-id safety — stale completion for an old attempt
# ---------------------------------------------------------------------------


def test_release_worker_lease_stale_expected_seq_does_not_release_newer_attempt():
    """The exact hazard this item's notes call out: 'must not let a late
    completion release a newer attempt'. Pure unit test, no DB/event loop
    needed (mirrors tests/test_869d6198_dispatcher_leases.py's own
    ``_FAKE_DB = object()`` pattern)."""
    disp = Dispatcher(object(), "proj-stale")
    disp.record_worker_lease("item-1", task={"id": "task-a"})
    old_seq = disp._active_leases["item-1"]["seq"]

    # A legitimate later attempt supersedes the lease for the same item_id
    # (record_worker_lease's own docstring documents this "refresh" as
    # safe/expected — e.g. a future explicit-retry path re-registering).
    disp.record_worker_lease("item-1", task={"id": "task-b"})
    new_seq = disp._active_leases["item-1"]["seq"]
    assert new_seq != old_seq

    # A LATE completion signal for the OLD attempt arrives now.
    disp.release_worker_lease("item-1", status="done", expected_seq=old_seq)

    # Must NOT have released the newer attempt.
    assert "item-1" in disp._active_leases
    assert disp._active_leases["item-1"]["seq"] == new_seq
    assert disp._active_leases["item-1"]["task"] == {"id": "task-b"}
    assert disp.last_released_lease is None  # nothing was ever actually released
    assert disp.last_stale_release_attempts == [{
        "item_id": "item-1",
        "attempted_status": "done",
        "expected_seq": old_seq,
        "active_seq": new_seq,
    }]

    # The CORRECT (matching) release for the current attempt still works.
    disp.release_worker_lease("item-1", status="done", expected_seq=new_seq)
    assert "item-1" not in disp._active_leases
    assert disp.last_released_lease == {"item_id": "item-1", "status": "done", "seq": new_seq}


def test_release_worker_lease_expected_seq_omitted_is_unchanged_behavior():
    """Every pre-769e24a7 caller omits expected_seq — behavior must be
    byte-for-byte identical to before this feature existed."""
    disp = Dispatcher(object(), "proj-stale-2")
    disp.record_worker_lease("item-1", task={"id": "task-a"})
    disp.record_worker_lease("item-1", task={"id": "task-b"})  # refresh, seq=2
    disp.release_worker_lease("item-1")  # no expected_seq -> releases whatever is active
    assert "item-1" not in disp._active_leases
    assert disp.last_released_lease == {"item_id": "item-1", "status": "completed", "seq": 2}
    assert disp.last_stale_release_attempts == []


@pytest.mark.asyncio
async def test_reconcile_active_leases_is_attempt_safe_under_interleaving(db, project, session):
    """White-box race test: if a NEWER attempt gets registered for the same
    item_id WHILE reconcile_active_leases is awaiting the DB read for the
    OLDER attempt's task_id, the stale read must never release the newer
    attempt's lease. Proves reconcile_active_leases snapshots ``seq`` BEFORE
    the yielding await, not after."""
    old_task = await db_module.log_task(db, session, project, "old", "pending")
    await db_module.update_task(db, old_task["id"], status="done")

    disp = Dispatcher(db, project)
    disp.record_worker_lease("item-race", task=old_task)
    old_seq = disp._active_leases["item-race"]["seq"]

    real_get_task = db_module.get_task

    async def _racy_get_task(db_, task_id):
        if task_id == old_task["id"]:
            # Simulate a concurrent re-dispatch landing a NEWER attempt for
            # the same item_id while this read is still in flight.
            disp.record_worker_lease("item-race", task={"id": "new-task"})
        return await real_get_task(db_, task_id)

    import meridian.dispatcher as dispatcher_module
    orig = dispatcher_module.db_module.get_task
    dispatcher_module.db_module.get_task = _racy_get_task
    try:
        released = await disp.reconcile_active_leases()
    finally:
        dispatcher_module.db_module.get_task = orig

    assert released == []  # the stale read must not have released anything
    assert "item-race" in disp._active_leases
    assert disp._active_leases["item-race"]["seq"] != old_seq
    assert disp.last_stale_release_attempts, "expected a recorded stale-release collision"
    assert disp.last_stale_release_attempts[-1]["expected_seq"] == old_seq


# ---------------------------------------------------------------------------
# process death (PID watchdog-equivalent terminal write)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_death_releases_lease_via_reconciliation(db, project, session):
    """Mirrors tests/test_core.py::test_watchdog_marks_dead_pid_failed's own
    'simulate the watchdog inline' pattern, then adds the Dispatcher-side
    reconciliation half that test doesn't cover."""
    task = await db_module.log_task(db, session, project, "work", "pending")
    await db_module.update_task(db, task["id"], status="in_progress")
    dead_pid = 999999999
    await db_module.update_task_worker_pid(db, task["id"], dead_pid)

    disp = Dispatcher(db, project)
    disp.record_worker_lease("item-death", task=task)

    # --- simulate meridian/server.py::_auto_summary_loop's PID watchdog ---
    stale = await db_module.get_in_progress_tasks_with_pid(db)
    matched = [t for t in stale if t["id"] == task["id"]]
    assert len(matched) == 1
    for t in matched:
        pid = t["worker_pid"]
        try:
            os.kill(int(pid), 0)
        except (ProcessLookupError, PermissionError, OSError):
            await db_module.update_task(
                db, t["id"], status="failed",
                description=f"[claude-error] worker process died unexpectedly (PID {pid})",
            )
    # --- end simulated watchdog ---

    released = await disp.reconcile_active_leases()
    assert len(released) == 1
    assert released[0]["status"] == "failed"
    assert "item-death" not in disp._active_leases


# ---------------------------------------------------------------------------
# cancellation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_worker_cancellation_marks_failed_and_invokes_on_complete(db, session, project):
    task = await db_module.log_task(db, session, project, "t", "pending")
    completed: "list[dict]" = []

    def _on_complete(row):
        completed.append(row)

    run_task = asyncio.create_task(
        _run_worker(db, task["id"], "hello", _SLOW_WORKER, timeout=30, on_complete=_on_complete)
    )
    try:
        for _ in range(100):
            await asyncio.sleep(0.05)
            row = await db_module.get_task(db, task["id"])
            if row and row["status"] == "in_progress":
                break
        else:
            pytest.fail("never observed in_progress status")

        run_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run_task
    finally:
        if not run_task.done():
            run_task.cancel()

    final = await db_module.get_task(db, task["id"])
    assert final["status"] == "failed"
    assert "cancelled" in final["description"].lower()
    assert len(completed) == 1
    assert completed[0]["status"] == "failed"


@pytest.mark.asyncio
async def test_cancelled_worker_lease_reconciles_after_task_marked_failed(db, project, session):
    """Connects cancellation all the way through to lease release: the
    cancelled worker's task row goes 'failed', and reconciliation picks it
    up exactly like any other failure."""
    task = await db_module.log_task(db, session, project, "t", "pending")

    run_task = asyncio.create_task(
        _run_worker(db, task["id"], "hello", _SLOW_WORKER, timeout=30)
    )
    try:
        for _ in range(100):
            await asyncio.sleep(0.05)
            row = await db_module.get_task(db, task["id"])
            if row and row["status"] == "in_progress":
                break
        else:
            pytest.fail("never observed in_progress status")

        run_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run_task
    finally:
        if not run_task.done():
            run_task.cancel()

    disp = Dispatcher(db, project)
    disp.record_worker_lease("item-cancel", task=task)
    assert "item-cancel" in disp._active_leases

    released = await disp.reconcile_active_leases()
    assert len(released) == 1
    assert released[0]["item_id"] == "item-cancel"
    assert released[0]["status"] == "failed"
    assert "item-cancel" not in disp._active_leases


# ---------------------------------------------------------------------------
# dispatch_once integration: capacity formula, reconciliation ordering,
# retry/dedup preservation, real-worker terminal transitions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_once_capacity_uses_active_leases_not_dispatched(db, project):
    """Regression guard for the actual root cause: in_flight must be
    len(_active_leases), NOT len(_dispatched) (which only ever grows)."""
    groups = [[{"id": "i1", "title": "I1", "resources": ["file:a"]}]]
    fake = _FakeEnqueue()
    disp = Dispatcher(db, project, enqueue_fn=fake, get_groups_fn=_groups_fn(groups), max_in_flight=1)

    first = await disp.dispatch_once()
    assert len(first) == 1
    assert "i1" in disp._dispatched
    assert len(disp._active_leases) == 1

    # Release the lease directly (as reconciliation would once the row goes
    # terminal) WITHOUT touching _dispatched at all.
    disp.release_worker_lease("i1", status="done")
    assert disp._active_leases == {}
    assert "i1" in disp._dispatched  # dedup ledger permanently unaffected

    groups2 = [[
        {"id": "i1", "title": "I1", "resources": ["file:a"]},
        {"id": "i2", "title": "I2", "resources": ["file:b"]},
    ]]
    disp._get_groups = _groups_fn(groups2)
    second = await disp.dispatch_once()

    # i1 is still permanently deduped; i2 is admitted because in_flight is
    # now computed from _active_leases (0), not _dispatched (1).
    assert len(second) == 1
    assert any("I2" in c["prompt"] for c in fake.calls)
    assert not any("I1" in c["prompt"] for c in fake.calls[1:])


@pytest.mark.asyncio
async def test_capacity_reuse_after_real_worker_completes(db, project):
    """THE core integration proof this item exists for: a REAL worker
    subprocess completes, and a LATER, DIFFERENT item gets its slot. This
    test fails against the pre-fix code (len(_dispatched) never shrinks)."""
    groups_round1 = [[{"id": "i1", "title": "I1", "resources": ["file:a"]}]]
    spy = _RealEnqueueSpy(_OK_WORKER, wait=True)
    disp = Dispatcher(
        db, project, enqueue_fn=spy, get_groups_fn=_groups_fn(groups_round1), max_in_flight=1,
    )

    first = await disp.dispatch_once()
    assert len(first) == 1
    task1 = await db_module.get_task(db, first[0]["id"])
    assert task1["status"] == "done"  # wait=True -> already terminal by the time we see it
    assert len(disp._active_leases) == 1  # registered; not yet reconciled again

    groups_round2 = [[{"id": "i2", "title": "I2", "resources": ["file:b"]}]]
    disp._get_groups = _groups_fn(groups_round2)
    second = await disp.dispatch_once()

    assert len(second) == 1
    assert len(spy.calls) == 2
    assert any("I2" in p for p in spy.calls)
    assert len(disp._active_leases) == 1  # i1's lease released, i2's now active
    assert "i2" in disp._active_leases
    assert "i1" not in disp._active_leases


@pytest.mark.asyncio
async def test_item_never_redispatched_after_its_lease_is_released(db, project):
    """Explicit retry / dedup policy preserved: releasing a lease must NOT
    make dispatch_once treat the item as eligible again."""
    groups = [[{"id": "i1", "title": "I1", "resources": ["file:a"]}]]
    spy = _RealEnqueueSpy(_OK_WORKER, wait=True)
    disp = Dispatcher(db, project, enqueue_fn=spy, get_groups_fn=_groups_fn(groups), max_in_flight=1)

    first = await disp.dispatch_once()
    assert len(first) == 1
    assert "i1" in disp._dispatched

    # i1's worker already finished (wait=True); the NEXT pass's
    # reconciliation releases its lease — but i1 is still the only item on
    # this (unchanged) board and must NOT be dispatched a second time.
    second = await disp.dispatch_once()
    assert second == []
    assert len(spy.calls) == 1
    assert "i1" in disp._dispatched
    assert "i1" not in disp._active_leases  # released, but never re-dispatched


@pytest.mark.asyncio
async def test_failed_worker_reconciles_and_frees_capacity(db, project):
    groups_round1 = [[{"id": "i1", "title": "I1", "resources": ["file:a"]}]]
    spy = _RealEnqueueSpy(_FAIL_WORKER, wait=True)
    disp = Dispatcher(
        db, project, enqueue_fn=spy, get_groups_fn=_groups_fn(groups_round1), max_in_flight=1,
    )

    first = await disp.dispatch_once()
    assert len(first) == 1
    task1 = await db_module.get_task(db, first[0]["id"])
    assert task1["status"] == "failed"

    groups_round2 = [[{"id": "i2", "title": "I2", "resources": ["file:b"]}]]
    disp._get_groups = _groups_fn(groups_round2)
    second = await disp.dispatch_once()
    assert len(second) == 1
    assert disp.last_released_lease["status"] == "failed"


@pytest.mark.asyncio
async def test_timeout_worker_reconciles_and_frees_capacity(db, project):
    groups_round1 = [[{"id": "i1", "title": "I1", "resources": ["file:a"]}]]
    spy = _RealEnqueueSpy(_SLOW_WORKER, timeout=0.3, wait=True)
    disp = Dispatcher(
        db, project, enqueue_fn=spy, get_groups_fn=_groups_fn(groups_round1), max_in_flight=1,
    )

    first = await disp.dispatch_once()
    assert len(first) == 1
    task1 = await db_module.get_task(db, first[0]["id"])
    assert task1["status"] == "failed"
    assert "timed out" in task1["description"]

    groups_round2 = [[{"id": "i2", "title": "I2", "resources": ["file:b"]}]]
    disp._get_groups = _groups_fn(groups_round2)
    second = await disp.dispatch_once()
    assert len(second) == 1


@pytest.mark.asyncio
async def test_on_complete_wakes_dispatcher_without_waiting_for_interval(db, project):
    """769e24a7's responsiveness hook: a background (wait=False) worker's
    completion wakes the dispatcher loop immediately, instead of leaving the
    freed capacity undiscovered until the next scheduled (here: 100s away)
    pass."""
    groups = [[{"id": "i1", "title": "I1", "resources": ["file:a"]}]]
    spy = _RealEnqueueSpy(_OK_WORKER, wait=False)
    disp = Dispatcher(
        db, project, enqueue_fn=spy, get_groups_fn=_groups_fn(groups),
        interval=100.0, max_in_flight=1,
    )

    enqueued = await disp.dispatch_once()
    assert len(enqueued) == 1

    for _ in range(200):
        await asyncio.sleep(0.02)
        if disp._wake.is_set():
            break
    assert disp._wake.is_set(), "on_complete never woke the dispatcher loop"


# ---------------------------------------------------------------------------
# restart reconciliation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restart_fresh_dispatcher_has_no_phantom_capacity(db, project):
    """A brand-new Dispatcher instance (simulating a process restart) must
    start with zero active leases and full capacity, completely unaffected
    by an older, now-orphaned in-memory Dispatcher's state — in-memory
    leases never survive a restart, and stale/terminal DB rows from a prior
    process never leak into a fresh instance's capacity accounting."""
    groups_a = [[{"id": "i1", "title": "I1", "resources": ["file:a"]}]]
    spy = _RealEnqueueSpy(_OK_WORKER, wait=True)
    disp_a = Dispatcher(
        db, project, enqueue_fn=spy, get_groups_fn=_groups_fn(groups_a), max_in_flight=1,
    )
    enqueued_a = await disp_a.dispatch_once()
    assert len(enqueued_a) == 1
    assert len(disp_a._active_leases) == 1  # registered; disp_a never reconciled again

    # "Restart": a fresh Dispatcher instance, same db/project, zero shared
    # in-memory state with disp_a.
    groups_b = [[{"id": "i2", "title": "I2", "resources": ["file:b"]}]]
    fake_b = _FakeEnqueue()
    disp_b = Dispatcher(
        db, project, enqueue_fn=fake_b, get_groups_fn=_groups_fn(groups_b), max_in_flight=1,
    )
    assert disp_b._active_leases == {}
    assert disp_b._dispatched == set()

    enqueued_b = await disp_b.dispatch_once()
    assert len(enqueued_b) == 1  # full capacity available, unaffected by disp_a
    assert "i2" in disp_b._dispatched
