"""bbb447ec — immutable, queryable wave-completion summaries keyed by wave_id.

Coverage:
  1.  persist_wave_summary + get_wave_summary round-trip (all fields).
  2.  items[].outcome is validated against the explicit enum; a bad value
      (or a missing item_id) raises ValueError rather than being silently
      coerced or inferred.
  3.  test_receipts are validated as structured evidence (command/exit_code/
      passed/failed[/scope]); malformed receipts raise ValueError.
  4.  Two independent reads of the SAME summary return identical content
      (deterministic serialization).
  5.  record_wave_summary_correction appends a NEW row and leaves the
      original's content byte-identical, only setting superseded_by.
  6.  Correcting an already-superseded row is refused (linear chains).
  7.  get_wave_summary returns the chain tip (the correction), not the
      original, once a correction exists.
  8.  get_wave_summary_history returns every row, oldest first, superseded
      rows included.
  9.  Project isolation: a summary recorded for project A never appears in
      project B's query, even with the identical wave_id.
  10. Version isolation: a summary recorded under one version bucket never
      appears in a different version's query for the same project/wave_id.
  11. wave_run_id further narrows retrieval when supplied.
  12. Migration creates the table; PG registry includes the mirror.
  13. REST route parity: GET /projects/{id}/wave-summary/{wave_id} returns
      null before a summary exists, the persisted summary after, and honors
      ?version=/?include_history=.
"""
from __future__ import annotations

import pytest

from meridian import db as db_module
from meridian.db.wave_run_summary import WAVE_SUMMARY_ITEM_OUTCOMES


async def _project(db, name: str) -> str:
    proj = await db_module.create_project(db, name)
    return proj["id"]


_GOOD_ITEMS = [
    {"item_id": "item-a", "outcome": "completed"},
    {"item_id": "item-b", "outcome": "blocked"},
]
_GOOD_RECEIPT = {
    "command": "pytest -q tests/test_x.py",
    "exit_code": 0,
    "passed": 12,
    "failed": 0,
    "scope": "targeted",
}


# ---------------------------------------------------------------------------
# 1. Round-trip
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_persist_and_get_round_trip(db):
    pid = await _project(db, "wrs-roundtrip")
    created = await db_module.persist_wave_summary(
        db, pid, "wave-1",
        version="v1.0.0",
        wave_run_id="run-123",
        session_id="sess-1",
        board_revision_hash="sha256:deadbeef",
        items=_GOOD_ITEMS,
        commits=[{"sha": "abc123", "message": "fix"}],
        changed_resources=["file:meridian/db/wave_runs.py"],
        test_receipts=[_GOOD_RECEIPT],
        blockers=[{"item_id": "item-b", "reason": "dependency"}],
        exclusions=[{"item_id": "item-c", "reason": "stale"}],
        tool_availability=[{"tool": "Serena", "status": "available"}],
        handoff_status="generated",
        actor="sess-1",
    )
    assert created["wave_id"] == "wave-1"
    assert created["version_filter"] == "v1.0.0"
    assert created["wave_run_id"] == "run-123"
    assert created["board_revision_hash"] == "sha256:deadbeef"
    assert created["summary_hash"].startswith("sha256:")
    assert created["superseded_by"] is None
    assert created["supersedes"] is None

    fetched = await db_module.get_wave_summary(db, pid, "wave-1", version="v1.0.0")
    assert fetched is not None
    assert fetched["id"] == created["id"]
    # items were sorted canonically by item_id at persist time.
    assert [i["item_id"] for i in fetched["items"]] == ["item-a", "item-b"]
    assert fetched["test_receipts"][0]["command"] == _GOOD_RECEIPT["command"]
    assert fetched["commits"][0]["sha"] == "abc123"


@pytest.mark.asyncio
async def test_get_wave_summary_returns_none_when_never_recorded(db):
    pid = await _project(db, "wrs-none")
    assert await db_module.get_wave_summary(db, pid, "wave-99") is None


# ---------------------------------------------------------------------------
# 2. Explicit outcome enum
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_all_five_outcomes_are_accepted(db):
    pid = await _project(db, "wrs-outcomes")
    items = [
        {"item_id": f"item-{o}", "outcome": o}
        for o in sorted(WAVE_SUMMARY_ITEM_OUTCOMES)
    ]
    created = await db_module.persist_wave_summary(db, pid, "wave-1", items=items)
    assert {i["outcome"] for i in created["items"]} == WAVE_SUMMARY_ITEM_OUTCOMES


@pytest.mark.asyncio
async def test_invalid_outcome_is_rejected(db):
    pid = await _project(db, "wrs-bad-outcome")
    with pytest.raises(ValueError, match="invalid outcome"):
        await db_module.persist_wave_summary(
            db, pid, "wave-1",
            items=[{"item_id": "item-a", "outcome": "done"}],  # not in the enum
        )


@pytest.mark.asyncio
async def test_outcome_is_never_inferred_from_narrative_text(db):
    """A 'summary'/'notes'-shaped field is not a substitute for a real
    outcome — omitting 'outcome' entirely must fail closed, not default to
    'completed' by guessing from prose."""
    pid = await _project(db, "wrs-no-inference")
    with pytest.raises(ValueError, match="invalid outcome"):
        await db_module.persist_wave_summary(
            db, pid, "wave-1",
            items=[{"item_id": "item-a", "summary": "looks like it worked fine"}],
        )


@pytest.mark.asyncio
async def test_missing_item_id_is_rejected(db):
    pid = await _project(db, "wrs-missing-id")
    with pytest.raises(ValueError, match="item_id"):
        await db_module.persist_wave_summary(
            db, pid, "wave-1", items=[{"outcome": "completed"}],
        )


@pytest.mark.asyncio
async def test_items_must_be_a_list(db):
    pid = await _project(db, "wrs-items-type")
    with pytest.raises(ValueError, match="list"):
        await db_module.persist_wave_summary(db, pid, "wave-1", items="not-a-list")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 3. Structured test-receipt evidence
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_test_receipt_requires_command_exit_code_and_counts(db):
    pid = await _project(db, "wrs-receipt-fields")
    for bad_receipt, msg in [
        ({"exit_code": 0, "passed": 1, "failed": 0}, "command"),
        ({"command": "pytest", "passed": 1, "failed": 0}, "exit_code"),
        ({"command": "pytest", "exit_code": 0, "failed": 0}, "passed"),
        ({"command": "pytest", "exit_code": 0, "passed": 1}, "failed"),
    ]:
        with pytest.raises(ValueError, match=msg):
            await db_module.persist_wave_summary(
                db, pid, "wave-1", items=_GOOD_ITEMS, test_receipts=[bad_receipt],
            )


@pytest.mark.asyncio
async def test_test_receipt_rejects_self_reported_boolean(db):
    pid = await _project(db, "wrs-receipt-bool")
    with pytest.raises(ValueError, match="list"):
        await db_module.persist_wave_summary(
            db, pid, "wave-1", items=_GOOD_ITEMS, test_receipts=True,  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_test_receipt_invalid_scope_is_rejected(db):
    pid = await _project(db, "wrs-receipt-scope")
    with pytest.raises(ValueError, match="scope"):
        await db_module.persist_wave_summary(
            db, pid, "wave-1", items=_GOOD_ITEMS,
            test_receipts=[{**_GOOD_RECEIPT, "scope": "smoke"}],
        )


@pytest.mark.asyncio
async def test_test_receipt_scope_defaults_to_targeted(db):
    pid = await _project(db, "wrs-receipt-default-scope")
    receipt_no_scope = {k: v for k, v in _GOOD_RECEIPT.items() if k != "scope"}
    created = await db_module.persist_wave_summary(
        db, pid, "wave-1", items=_GOOD_ITEMS, test_receipts=[receipt_no_scope],
    )
    assert created["test_receipts"][0]["scope"] == "targeted"


# ---------------------------------------------------------------------------
# 4. Deterministic serialization
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_same_summary_read_twice_is_byte_identical(db):
    import json as _json

    pid = await _project(db, "wrs-determinism")
    await db_module.persist_wave_summary(
        db, pid, "wave-1", version="v1.0.0",
        items=_GOOD_ITEMS, test_receipts=[_GOOD_RECEIPT],
        commits=[{"sha": "abc"}], handoff_status="generated",
    )
    first = await db_module.get_wave_summary(db, pid, "wave-1", version="v1.0.0")
    second = await db_module.get_wave_summary(db, pid, "wave-1", version="v1.0.0")
    assert first == second
    assert _json.dumps(first, sort_keys=True, default=str) == _json.dumps(
        second, sort_keys=True, default=str
    )
    assert first["summary_hash"] == second["summary_hash"]


@pytest.mark.asyncio
async def test_canonical_hash_is_independent_of_item_submission_order(db):
    """Two independently-persisted summaries for the SAME (project, wave_id,
    version, run) bucket, whose items are submitted in different order but
    are otherwise logically identical, must hash identically — the canonical
    sort in _validate_summary_items makes ordering not content."""
    pid = await _project(db, "wrs-hash-order")
    a = await db_module.persist_wave_summary(
        db, pid, "wave-1", items=[
            {"item_id": "item-b", "outcome": "blocked"},
            {"item_id": "item-a", "outcome": "completed"},
        ],
    )
    b = await db_module.persist_wave_summary(
        db, pid, "wave-1", items=[
            {"item_id": "item-a", "outcome": "completed"},
            {"item_id": "item-b", "outcome": "blocked"},
        ],
    )
    assert a["id"] != b["id"]  # two distinct rows...
    assert a["summary_hash"] == b["summary_hash"]  # ...with identical content


# ---------------------------------------------------------------------------
# 5-6. Append-only corrections
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_correction_appends_new_row_and_never_mutates_original_content(db):
    pid = await _project(db, "wrs-correction")
    original = await db_module.persist_wave_summary(
        db, pid, "wave-1", items=_GOOD_ITEMS, handoff_status="generated",
    )
    correction = await db_module.record_wave_summary_correction(
        db, original["id"],
        items=[{"item_id": "item-a", "outcome": "failed"}],
        reason="item-a actually failed post-merge CI",
        actor="sess-2",
    )
    assert correction["id"] != original["id"]
    assert correction["supersedes"] == original["id"]
    assert [i["item_id"] for i in correction["items"]] == ["item-a"]
    assert correction["items"][0]["outcome"] == "failed"
    # Unset fields on the correction inherit the original's content.
    assert correction["handoff_status"] == "generated"

    # The ORIGINAL row's content is byte-identical to what was first written —
    # only its superseded_by pointer changed.
    original_reread = await db_module.get_wave_summary_by_id(db, original["id"])
    assert original_reread["items"] == original["items"]
    assert original_reread["handoff_status"] == original["handoff_status"]
    assert original_reread["summary_hash"] == original["summary_hash"]
    assert original_reread["superseded_by"] == correction["id"]


@pytest.mark.asyncio
async def test_correcting_an_already_superseded_row_is_refused(db):
    pid = await _project(db, "wrs-double-correct")
    original = await db_module.persist_wave_summary(db, pid, "wave-1", items=_GOOD_ITEMS)
    await db_module.record_wave_summary_correction(
        db, original["id"], items=[{"item_id": "item-a", "outcome": "failed"}],
    )
    with pytest.raises(ValueError, match="already been superseded"):
        await db_module.record_wave_summary_correction(
            db, original["id"], items=[{"item_id": "item-a", "outcome": "skipped"}],
        )


@pytest.mark.asyncio
async def test_correcting_unknown_id_raises(db):
    pid = await _project(db, "wrs-unknown-correct")
    await _project(db, "wrs-unknown-correct-other")  # unrelated project, just to touch db
    with pytest.raises(ValueError, match="not found"):
        await db_module.record_wave_summary_correction(db, "does-not-exist", items=_GOOD_ITEMS)
    assert pid  # project created but unused beyond establishing a live db conn


@pytest.mark.asyncio
async def test_get_wave_summary_returns_the_correction_not_the_original(db):
    pid = await _project(db, "wrs-tip")
    original = await db_module.persist_wave_summary(db, pid, "wave-1", items=_GOOD_ITEMS)
    correction = await db_module.record_wave_summary_correction(
        db, original["id"], items=[{"item_id": "item-a", "outcome": "failed"}],
    )
    tip = await db_module.get_wave_summary(db, pid, "wave-1")
    assert tip["id"] == correction["id"]
    assert tip["id"] != original["id"]


@pytest.mark.asyncio
async def test_history_returns_every_row_oldest_first_including_superseded(db):
    pid = await _project(db, "wrs-history")
    original = await db_module.persist_wave_summary(db, pid, "wave-1", items=_GOOD_ITEMS)
    correction = await db_module.record_wave_summary_correction(
        db, original["id"], items=[{"item_id": "item-a", "outcome": "failed"}],
    )
    history = await db_module.get_wave_summary_history(db, pid, "wave-1")
    assert [h["id"] for h in history] == [original["id"], correction["id"]]
    assert history[0]["superseded_by"] == correction["id"]
    assert history[1]["superseded_by"] is None


# ---------------------------------------------------------------------------
# 9-11. Isolation + narrowing
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_project_isolation_same_wave_id(db):
    pid_a = await _project(db, "wrs-proj-a")
    pid_b = await _project(db, "wrs-proj-b")
    await db_module.persist_wave_summary(db, pid_a, "wave-1", items=_GOOD_ITEMS)

    assert await db_module.get_wave_summary(db, pid_a, "wave-1") is not None
    assert await db_module.get_wave_summary(db, pid_b, "wave-1") is None
    assert await db_module.get_wave_summary_history(db, pid_b, "wave-1") == []


@pytest.mark.asyncio
async def test_version_isolation_same_project_and_wave_id(db):
    pid = await _project(db, "wrs-version-iso")
    await db_module.persist_wave_summary(
        db, pid, "wave-1", version="v1.0.0", items=_GOOD_ITEMS,
    )
    assert await db_module.get_wave_summary(db, pid, "wave-1", version="v1.0.0") is not None
    assert await db_module.get_wave_summary(db, pid, "wave-1", version="v2.0.0") is None
    # Unscoped (no version) query uses the '' bucket, distinct from 'v1.0.0'.
    assert await db_module.get_wave_summary(db, pid, "wave-1") is None


@pytest.mark.asyncio
async def test_wave_run_id_narrows_retrieval(db):
    pid = await _project(db, "wrs-run-narrow")
    await db_module.persist_wave_summary(
        db, pid, "wave-1", wave_run_id="run-A", items=_GOOD_ITEMS,
    )
    await db_module.persist_wave_summary(
        db, pid, "wave-1", wave_run_id="run-B",
        items=[{"item_id": "item-x", "outcome": "failed"}],
    )
    run_a = await db_module.get_wave_summary(db, pid, "wave-1", wave_run_id="run-A")
    run_b = await db_module.get_wave_summary(db, pid, "wave-1", wave_run_id="run-B")
    assert run_a["wave_run_id"] == "run-A"
    assert run_b["wave_run_id"] == "run-B"
    assert run_a["id"] != run_b["id"]
    # Unscoped-by-run query returns the most recently created tip across runs.
    latest = await db_module.get_wave_summary(db, pid, "wave-1")
    assert latest["id"] == run_b["id"]


# ---------------------------------------------------------------------------
# 12. Migration / PG parity
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_migration_creates_wave_run_summaries_table(db):
    async with db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='wave_run_summaries'"
    ) as cur:
        row = await cur.fetchone()
    assert row is not None


def test_pg_migration_registered_in_late_registry():
    from meridian.pg_adapter import _PG_MIGRATIONS_LATE, _migrate_pg_wave_run_summaries

    assert _migrate_pg_wave_run_summaries in _PG_MIGRATIONS_LATE


@pytest.mark.asyncio
async def test_persist_wave_summary_is_exported_from_db_package(db):
    """Sanity check that the bottom-of-file import block in db/__init__.py
    actually bound every public symbol this module exposes."""
    for name in (
        "persist_wave_summary", "get_wave_summary", "get_wave_summary_by_id",
        "get_wave_summary_history", "record_wave_summary_correction",
        "WAVE_SUMMARY_ITEM_OUTCOMES", "WAVE_SUMMARY_TEST_SCOPES",
        "canonical_wave_summary_hash",
    ):
        assert hasattr(db_module, name), f"meridian.db.{name} not exported"


# ---------------------------------------------------------------------------
# 13. REST route parity (GET /projects/{project_id}/wave-summary/{wave_id})
# ---------------------------------------------------------------------------

def test_route_returns_null_summary_before_anything_recorded(client):
    pid = client.post("/projects", json={"name": "wrs-route-empty"}).json()["id"]
    resp = client.get(f"/projects/{pid}/wave-summary/wave-1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["summary"] is None
    assert body["wave_id"] == "wave-1"


def test_route_returns_persisted_summary(client):
    """Seed a summary directly through the TestClient's own DB connection
    (``client.app.state.db`` — same pattern as test_milestones.py's token
    fixture) then confirm the route serializes it, including a correction
    chain in the history."""
    import asyncio

    pid = client.post("/projects", json={"name": "wrs-route-present"}).json()["id"]

    async def _seed():
        db = client.app.state.db
        original = await db_module.persist_wave_summary(
            db, pid, "wave-1",
            items=[{"item_id": "item-a", "outcome": "completed"}],
            test_receipts=[_GOOD_RECEIPT],
        )
        await db_module.record_wave_summary_correction(
            db, original["id"],
            items=[{"item_id": "item-a", "outcome": "failed"}],
            reason="regressed after merge",
        )

    asyncio.run(_seed())

    resp = client.get(f"/projects/{pid}/wave-summary/wave-1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["summary"] is not None
    assert body["summary"]["items"][0]["outcome"] == "failed"  # the correction, not the original
    assert "history" not in body  # include_history defaults off

    resp_hist = client.get(
        f"/projects/{pid}/wave-summary/wave-1", params={"include_history": "1"},
    )
    history = resp_hist.json()["history"]
    assert len(history) == 2
    assert history[0]["items"][0]["outcome"] == "completed"  # original, oldest first
    assert history[1]["items"][0]["outcome"] == "failed"      # the correction


def test_route_returns_404_for_unknown_project(client):
    resp = client.get("/projects/does-not-exist/wave-summary/wave-1")
    assert resp.status_code == 404


def test_route_version_and_include_history_query_params_are_threaded(client):
    pid = client.post("/projects", json={"name": "wrs-route-params"}).json()["id"]
    resp = client.get(
        f"/projects/{pid}/wave-summary/wave-1",
        params={"version": "v1.0.0", "include_history": "1"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["version"] == "v1.0.0"
    assert body["summary"] is None
    assert body["history"] == []
