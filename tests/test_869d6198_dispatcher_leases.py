"""Isolated tests for Dispatcher.__init__'s active worker lease accounting
and capacity release (sprint item 869d6198).

Scope note: these tests exercise ONLY the state and closures constructed in
``Dispatcher.__init__`` (``self._active_leases``, ``self.record_worker_lease``,
``self.release_worker_lease``, ``self.last_released_lease``) plus their
interaction with the pre-existing ``self._dispatched`` dedup ledger. They
deliberately do NOT call ``dispatch_once`` or otherwise exercise real
enqueue/board logic — that method is a separately-owned, concurrently-edited
symbol (sprint item 272d8f2c) in this same module, and this file must stay
parallel-safe with whatever it lands on. No live DB/event-loop is needed
either: ``Dispatcher.__init__`` never touches ``db`` beyond storing the
reference, so a plain sentinel stands in for it.
"""

from __future__ import annotations

from meridian.dispatcher import Dispatcher

_FAKE_DB = object()
_PROJECT_ID = "proj-869d6198"


def _make_dispatcher(**kwargs) -> Dispatcher:
    return Dispatcher(_FAKE_DB, _PROJECT_ID, **kwargs)


# --- initial state -----------------------------------------------------


def test_active_leases_start_empty():
    disp = _make_dispatcher()
    assert disp._active_leases == {}
    assert disp._lease_seq == 0
    assert disp.last_released_lease is None


def test_dispatched_ledger_untouched_by_init():
    """__init__'s pre-existing dedup/frontier ledger is unaffected."""
    disp = _make_dispatcher()
    assert disp._dispatched == set()
    assert isinstance(disp._dispatched, set)


def test_existing_diagnostics_preserved():
    """Pre-existing introspection attributes must still exist untouched."""
    disp = _make_dispatcher()
    assert disp.last_parallelism is None
    assert disp.last_blocker_decision is None
    assert disp.last_merger_lock_skips == []
    assert disp.last_lease_sweep is None


# --- record_worker_lease -------------------------------------------------


def test_record_worker_lease_adds_active_entry():
    disp = _make_dispatcher()
    disp.record_worker_lease("item-1")
    assert "item-1" in disp._active_leases
    lease = disp._active_leases["item-1"]
    assert lease["item_id"] == "item-1"
    assert lease["status"] == "active"
    assert lease["task"] is None
    assert lease["seq"] == 1


def test_record_worker_lease_stores_task_payload():
    disp = _make_dispatcher()
    task = {"id": "task-1", "status": "pending"}
    disp.record_worker_lease("item-1", task=task)
    assert disp._active_leases["item-1"]["task"] == task


def test_record_worker_lease_sequence_increments_across_items():
    disp = _make_dispatcher()
    disp.record_worker_lease("item-1")
    disp.record_worker_lease("item-2")
    disp.record_worker_lease("item-3")
    assert disp._lease_seq == 3
    assert [
        disp._active_leases[i]["seq"] for i in ("item-1", "item-2", "item-3")
    ] == [1, 2, 3]


def test_record_worker_lease_twice_refreshes_not_errors():
    """Re-registering the same item_id must never raise — retried enqueue
    paths are a normal occurrence, not corruption."""
    disp = _make_dispatcher()
    disp.record_worker_lease("item-1", task={"id": "task-a"})
    first_seq = disp._active_leases["item-1"]["seq"]
    disp.record_worker_lease("item-1", task={"id": "task-b"})
    assert disp._active_leases["item-1"]["task"] == {"id": "task-b"}
    assert disp._active_leases["item-1"]["seq"] > first_seq
    # Still only one entry for the item — a refresh, not a duplicate.
    assert len(disp._active_leases) == 1


def test_record_worker_lease_does_not_touch_dispatched_set():
    disp = _make_dispatcher()
    disp.record_worker_lease("item-1")
    assert disp._dispatched == set()


# --- release_worker_lease -------------------------------------------------


def test_release_worker_lease_removes_active_entry():
    disp = _make_dispatcher()
    disp.record_worker_lease("item-1")
    assert "item-1" in disp._active_leases
    disp.release_worker_lease("item-1")
    assert "item-1" not in disp._active_leases


def test_release_worker_lease_default_status_completed():
    disp = _make_dispatcher()
    disp.record_worker_lease("item-1")
    disp.release_worker_lease("item-1")
    assert disp.last_released_lease == {"item_id": "item-1", "status": "completed", "seq": 1}


def test_release_worker_lease_explicit_failed_status():
    disp = _make_dispatcher()
    disp.record_worker_lease("item-1")
    disp.release_worker_lease("item-1", status="failed")
    assert disp.last_released_lease["status"] == "failed"
    assert disp.last_released_lease["item_id"] == "item-1"


def test_release_worker_lease_idempotent_when_never_registered():
    """Releasing an item_id with no active lease is a no-op, not an error —
    a normal race in an async dispatch loop."""
    disp = _make_dispatcher()
    disp.release_worker_lease("never-dispatched")
    assert disp._active_leases == {}
    assert disp.last_released_lease == {
        "item_id": "never-dispatched",
        "status": "completed",
        "seq": None,
    }


def test_release_worker_lease_idempotent_when_released_twice():
    disp = _make_dispatcher()
    disp.record_worker_lease("item-1")
    disp.release_worker_lease("item-1")
    # Second release of the same item must not raise and must not resurrect
    # any state.
    disp.release_worker_lease("item-1", status="failed")
    assert disp._active_leases == {}
    assert disp.last_released_lease["status"] == "failed"
    assert disp.last_released_lease["seq"] is None


def test_release_worker_lease_does_not_touch_dispatched_set():
    """Frontier selection must be unaffected: an item stays in
    ``_dispatched`` forever, independent of its lease being released."""
    disp = _make_dispatcher()
    disp._dispatched.add("item-1")  # simulate dispatch_once's dedup marker
    disp.record_worker_lease("item-1")
    disp.release_worker_lease("item-1")
    assert "item-1" in disp._dispatched
    assert "item-1" not in disp._active_leases


def test_release_worker_lease_only_removes_targeted_item():
    disp = _make_dispatcher()
    disp.record_worker_lease("item-1")
    disp.record_worker_lease("item-2")
    disp.release_worker_lease("item-1")
    assert "item-1" not in disp._active_leases
    assert "item-2" in disp._active_leases


# --- capacity-release semantics (in_flight vs. dispatched) ---------------


def test_active_lease_count_reflects_capacity_not_cumulative_total():
    """The core bug this item fixes: capacity accounting must be able to
    shrink as workers finish, unlike len(self._dispatched) which only ever
    grows. This test asserts the DECOUPLED primitive dispatch_once's
    capacity math is meant to consume behaves correctly in isolation."""
    disp = _make_dispatcher()
    for item_id in ("item-1", "item-2", "item-3"):
        disp._dispatched.add(item_id)
        disp.record_worker_lease(item_id)

    assert len(disp._dispatched) == 3
    assert len(disp._active_leases) == 3

    # Two of the three child workers finish.
    disp.release_worker_lease("item-1", status="completed")
    disp.release_worker_lease("item-2", status="failed")

    # Cumulative/frontier ledger is untouched — still 3, forever.
    assert len(disp._dispatched) == 3
    # ACTIVE accounting shrank — capacity for 2 more workers is now free.
    assert len(disp._active_leases) == 1
    assert "item-3" in disp._active_leases


# --- multiple independent Dispatcher instances ----------------------------


def test_lease_state_is_per_instance_not_shared():
    """Closures are rebuilt fresh per __init__ call — no accidental sharing
    of mutable state across Dispatcher instances (e.g. via a class-level
    default)."""
    disp_a = _make_dispatcher()
    disp_b = _make_dispatcher()

    disp_a.record_worker_lease("item-1")
    assert "item-1" in disp_a._active_leases
    assert disp_b._active_leases == {}
    assert disp_a._active_leases is not disp_b._active_leases
    assert disp_a.record_worker_lease is not disp_b.record_worker_lease
