"""Sprint item a8c33e9d -- proposal promotion materializes item pointers and
pins the executable_handoff step to the compact goal-mode handoff.

Regression coverage for ``meridian.proposal_promotion.commit_proposal_promotion``:

* validated proposal pointers are written as durable ``sprint_item_pointers``
  rows on the promoted sprint item (not only as proposal-level events);
* a reused (already-promoted) item gets the pointers once -- a retry never
  duplicates rows, and duplicate entries inside one call are skipped;
* a per-entry write failure is reported in the commit result, never silently
  dropped, and the promoted item is still created;
* a malformed pointer fails closed before any write;
* the executable_handoff step asks ``generate_handoff`` for ``mode="goal"``
  scoped to exactly the promoted item (never the 'full' archival dump);
* a pointer that commit accepted without an explicit ``target_kind`` is kept
  by materialization even when its local uri is not on disk (commit normalizes
  the omitted kind to ``"existing"``, which must not re-trigger the on-disk
  check at materialization), while a genuinely missing explicit ``existing``
  target is still rejected for any pointer written as a new row.

Every test passes ``infer_touches_resources=False`` so nothing depends on the
live git history of the worktree.
"""
from __future__ import annotations

import pytest

from meridian import db as db_module
from meridian import handoff as handoff_module
from meridian import proposal_promotion


async def _project(db, name: str) -> str:
    proj = await db_module.create_project(db, name)
    return proj["id"]


async def _proposal(db, title: str = "Idea", body: str = "body"):
    return await db_module.add_workspace_proposal(db, title, body)


def _pointers_for(proposal_id: str) -> list[dict]:
    """Two valid pointers that need no filesystem access.

    ``node_id`` on a ``meridian://`` uri mirrors what proposal_to_handoff
    attaches; the ``planned_new`` symbol target skips the on-disk existence
    check by design.
    """
    return [
        {
            "source_type": "proposal",
            "targets": [{
                "uri": f"meridian://workspace_proposal/{proposal_id}",
                "selector": {"type": "node_id", "id": proposal_id},
                "target_kind": "existing",
            }],
            "label": "Source proposal",
        },
        {
            "source_type": "code",
            "targets": [{
                "uri": "meridian/proposal_promotion.py",
                "selector": {
                    "type": "symbol",
                    "qualified_name": "meridian.proposal_promotion.commit_proposal_promotion",
                },
                "target_kind": "planned_new",
            }],
            "label": "Commit path",
        },
    ]


async def _count_sprint_items(db) -> int:
    async with db.execute("SELECT COUNT(*) AS n FROM sprint_items") as cur:
        row = await cur.fetchone()
    return row["n"] if isinstance(row, dict) else row[0]


async def _preview_and_commit(db, proposal_id, pid, depth, **kwargs):
    preview = await proposal_promotion.preview_proposal_promotion(
        db, proposal_id, pid, depth, infer_touches_resources=False,
    )
    return await proposal_promotion.commit_proposal_promotion(
        db, proposal_id, pid, depth, preview["preview_hash"],
        infer_touches_resources=False, **kwargs,
    )


# ---------------------------------------------------------------------------
# Materialization onto a freshly created sprint item
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pointers_materialized_as_sprint_item_pointer_rows(db):
    pid = await _project(db, "materialize-new-item")
    proposal = await _proposal(db, "Ship pointers", "needs evidence")
    pointers = _pointers_for(proposal["id"])

    result = await _preview_and_commit(
        db, proposal["id"], pid, "sprint_items", pointers=pointers,
    )

    sprint_item_id = result["committed"]["sprint_item"]["id"]
    materialized = result["committed"]["sprint_item_pointers"]
    assert materialized["sprint_item_id"] == sprint_item_id
    assert len(materialized["pointer_ids"]) == 2
    assert materialized["skipped_existing"] == 0
    assert materialized["errors"] == []

    rows = await db_module.get_sprint_item_pointers(db, sprint_item_id)
    assert len(rows) == 2
    assert {r["source_type"] for r in rows} == {"proposal", "code"}
    assert {r["label"] for r in rows} == {"Source proposal", "Commit path"}
    assert sorted(r["id"] for r in rows) == sorted(materialized["pointer_ids"])

    # The proposal-level audit trail is still written alongside.
    assert len(result["committed"]["pointer_event_ids"]) == 2


@pytest.mark.asyncio
async def test_no_pointers_supplied_leaves_sprint_item_pointers_untouched(db):
    pid = await _project(db, "no-pointers")
    proposal = await _proposal(db, "Plain promotion", "body")

    result = await _preview_and_commit(db, proposal["id"], pid, "sprint_items")

    sprint_item_id = result["committed"]["sprint_item"]["id"]
    assert "sprint_item_pointers" not in result["committed"]
    assert await db_module.get_sprint_item_pointers(db, sprint_item_id) == []


@pytest.mark.asyncio
async def test_duplicate_entries_within_one_call_are_written_once(db):
    pid = await _project(db, "dup-within-call")
    proposal = await _proposal(db, "Dup pointers", "body")
    first = _pointers_for(proposal["id"])[1]

    result = await _preview_and_commit(
        db, proposal["id"], pid, "sprint_items", pointers=[first, dict(first)],
    )

    sprint_item_id = result["committed"]["sprint_item"]["id"]
    materialized = result["committed"]["sprint_item_pointers"]
    assert len(materialized["pointer_ids"]) == 1
    assert materialized["skipped_existing"] == 1
    assert len(await db_module.get_sprint_item_pointers(db, sprint_item_id)) == 1


# ---------------------------------------------------------------------------
# Reused (already-promoted) item: pointers land once, retries do not duplicate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reused_item_gets_pointers_once_and_retry_does_not_duplicate(db, tmp_path):
    pid = await _project(db, "reused-item-pointers")
    proposal = await _proposal(db, "Already promoted", "body")
    promo = await db_module.promote_workspace_proposal(db, proposal["id"], pid)
    sprint_item_id = promo["sprint_item_id"]
    pointers = _pointers_for(proposal["id"])

    first = await _preview_and_commit(
        db, proposal["id"], pid, "executable_handoff",
        pointers=pointers, data_dir=str(tmp_path),
    )
    assert first["committed"]["sprint_item"]["reused_existing"] is True
    assert len(first["committed"]["sprint_item_pointers"]["pointer_ids"]) == 2

    second = await _preview_and_commit(
        db, proposal["id"], pid, "executable_handoff",
        pointers=pointers, data_dir=str(tmp_path),
    )
    retry = second["committed"]["sprint_item_pointers"]
    assert retry["pointer_ids"] == []
    assert retry["skipped_existing"] == 2
    assert retry["errors"] == []
    assert len(await db_module.get_sprint_item_pointers(db, sprint_item_id)) == 2


# ---------------------------------------------------------------------------
# Failure handling: fail closed before writes, report per-entry write errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_malformed_pointer_rejected_before_any_write(db):
    pid = await _project(db, "malformed-pointer")
    proposal = await _proposal(db, "Malformed", "body")
    bad = [{"source_type": "", "targets": []}]

    preview = await proposal_promotion.preview_proposal_promotion(
        db, proposal["id"], pid, "sprint_items", infer_touches_resources=False,
    )
    with pytest.raises(ValueError):
        await proposal_promotion.commit_proposal_promotion(
            db, proposal["id"], pid, "sprint_items", preview["preview_hash"],
            infer_touches_resources=False, pointers=bad,
        )

    assert await _count_sprint_items(db) == 0


@pytest.mark.asyncio
async def test_pointer_write_failure_is_reported_and_item_still_created(db, monkeypatch):
    pid = await _project(db, "pointer-write-failure")
    proposal = await _proposal(db, "Write fails", "body")
    real_add = db_module.add_sprint_item_pointer

    async def _boom(*args, **kwargs):
        raise RuntimeError("simulated pointer write failure")

    monkeypatch.setattr(db_module, "add_sprint_item_pointer", _boom)
    try:
        result = await _preview_and_commit(
            db, proposal["id"], pid, "sprint_items",
            pointers=_pointers_for(proposal["id"]),
        )
    finally:
        monkeypatch.setattr(db_module, "add_sprint_item_pointer", real_add)

    materialized = result["committed"]["sprint_item_pointers"]
    assert materialized["pointer_ids"] == []
    assert len(materialized["errors"]) == 2
    assert all("simulated pointer write failure" in e["error"] for e in materialized["errors"])

    # The promoted item exists (it was committed before the pointer step) and
    # the failure is visible to the caller rather than swallowed.
    assert await _count_sprint_items(db) == 1
    sprint_item_id = result["committed"]["sprint_item"]["id"]
    assert await db_module.get_sprint_item_pointers(db, sprint_item_id) == []


# ---------------------------------------------------------------------------
# Handoff mode: executable_handoff must request the bounded goal packet
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_executable_handoff_requests_goal_mode_scoped_to_promoted_item(db, tmp_path, monkeypatch):
    pid = await _project(db, "handoff-goal-mode")
    proposal = await _proposal(db, "Goal mode", "body")
    captured: dict = {}
    handoff_path = str(tmp_path / "handoff.md")

    async def _fake_generate_handoff(db_arg, project_id, data_dir, **kwargs):
        captured["project_id"] = project_id
        captured["data_dir"] = data_dir
        captured["kwargs"] = kwargs
        return handoff_path, "goal body", False

    monkeypatch.setattr(handoff_module, "generate_handoff", _fake_generate_handoff)

    result = await _preview_and_commit(
        db, proposal["id"], pid, "executable_handoff", data_dir=str(tmp_path),
    )

    sprint_item_id = result["committed"]["sprint_item"]["id"]
    assert captured["kwargs"]["mode"] == "goal"
    assert captured["kwargs"]["selected_item_ids"] == [sprint_item_id]
    assert captured["kwargs"]["skip_ai_summary"] is True
    assert result["committed"]["handoff"]["path"] == handoff_path


# ---------------------------------------------------------------------------
# Materialization must not re-run the on-disk existence check for pointers
# that commit already accepted; genuinely new pointers keep that check.
# ---------------------------------------------------------------------------

_MISSING_LOCAL_URI = "meridian/a8c33e9d_no_such_target.py"


def _implicit_missing_local_pointer() -> dict:
    """Pre-300a063d shape: a local-path target with NO ``target_kind`` key.

    Commit never disk-checks an implicit target, so it accepts this even though
    the file is absent. Materialization must keep it too.
    """
    return {
        "source_type": "code",
        "targets": [{
            "uri": _MISSING_LOCAL_URI,
            "selector": {
                "type": "symbol",
                "qualified_name": "meridian.a8c33e9d_no_such_target.run",
            },
        }],
        "label": "Implicit local target",
    }


@pytest.mark.asyncio
async def test_pointer_accepted_without_target_kind_is_kept_by_materialization(db):
    pid = await _project(db, "implicit-kind-materialized")
    proposal = await _proposal(db, "Implicit kind", "body")

    result = await _preview_and_commit(
        db, proposal["id"], pid, "sprint_items",
        pointers=[_implicit_missing_local_pointer()],
    )

    sprint_item_id = result["committed"]["sprint_item"]["id"]
    materialized = result["committed"]["sprint_item_pointers"]
    assert materialized["errors"] == []
    assert len(materialized["pointer_ids"]) == 1
    rows = await db_module.get_sprint_item_pointers(db, sprint_item_id)
    assert len(rows) == 1
    assert rows[0]["source_type"] == "code"
    assert rows[0]["targets"][0]["uri"] == _MISSING_LOCAL_URI


@pytest.mark.asyncio
async def test_explicit_existing_missing_target_still_rejected_for_new_pointer(db):
    pid = await _project(db, "explicit-missing-direct")
    item = await db_module.add_sprint_item(db, pid, "v1", "Direct add")

    with pytest.raises(ValueError, match="no file exists"):
        await db_module.add_sprint_item_pointer(
            db, pid, item["id"], "code",
            [{
                "uri": _MISSING_LOCAL_URI,
                "target_kind": "existing",
                "selector": {"type": "symbol", "qualified_name": "x.run"},
            }],
        )

    assert await db_module.get_sprint_item_pointers(db, item["id"]) == []


@pytest.mark.asyncio
async def test_commit_rejects_explicit_missing_existing_target_before_any_write(db):
    pid = await _project(db, "explicit-missing-commit")
    proposal = await _proposal(db, "Explicit missing", "body")
    bad = [{
        "source_type": "code",
        "targets": [{
            "uri": _MISSING_LOCAL_URI,
            "target_kind": "existing",
            "selector": {"type": "symbol", "qualified_name": "x.run"},
        }],
    }]

    preview = await proposal_promotion.preview_proposal_promotion(
        db, proposal["id"], pid, "sprint_items", infer_touches_resources=False,
    )
    with pytest.raises(ValueError, match="no file exists"):
        await proposal_promotion.commit_proposal_promotion(
            db, proposal["id"], pid, "sprint_items", preview["preview_hash"],
            infer_touches_resources=False, pointers=bad,
        )

    assert await _count_sprint_items(db) == 0
