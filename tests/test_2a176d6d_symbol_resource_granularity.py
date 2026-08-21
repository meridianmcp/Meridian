"""Tests for coarse resource-declaration audit + repair (1b264ce3, Wave 1
follow-up to b4102313's live-lock-aware get_parallelizable_groups).

Scope: distinguish a `file:X` declaration that is provably redundant (the
SAME item also names specific `symbol:X::*` entries for that file, so its
own declaration already proves a narrower true footprint) from one that may
be genuinely intentional whole-file scope (no in-item evidence either way —
never guessed from title/notes text). Only the provably-redundant case is
ever auto-repaired, and never for an item holding a live claim.
"""
import pytest

from meridian import db as db_module
from meridian import server as srv


async def _project(db, name="granularity"):
    proj = await srv._dispatch_mcp_tool("create_project", {"name": name}, db, "/tmp")
    return proj["id"]


@pytest.mark.asyncio
async def test_predict_resource_granularity_classifies_every_shape():
    assert db_module._predict_resource_granularity("file:a.py") == "file"
    assert db_module._predict_resource_granularity("file:a.py:foo") == "file_legacy_symbol_suffix"
    assert db_module._predict_resource_granularity("symbol:a.py::foo") == "symbol"
    assert db_module._predict_resource_granularity("symbol:foo") == "malformed_symbol"
    assert db_module._predict_resource_granularity("db:workspace_notes") == "other"


@pytest.mark.asyncio
async def test_diagnose_flags_self_contradictory_file_and_symbol(db):
    pid = await _project(db)
    item = await db_module.add_sprint_item(
        db, pid, "v1", "declares both file and symbol on the same path",
        touches_resources=["file:a.py", "symbol:a.py::foo"],
    )

    result = await db_module.diagnose_resource_coarseness(db, pid, version="v1")
    hit = next(c for c in result["candidates"] if c["id"] == item["id"])
    assert hit["file"] == "a.py"
    assert hit["self_contradictory_symbols"] == ["symbol:a.py::foo"]
    assert result["self_contradictory_count"] == 1


@pytest.mark.asyncio
async def test_diagnose_does_not_flag_uncontested_lone_file_resource(db):
    pid = await _project(db)
    await db_module.add_sprint_item(
        db, pid, "v1", "only ever declares the whole file",
        touches_resources=["file:solo.py"],
    )

    result = await db_module.diagnose_resource_coarseness(db, pid, version="v1")
    assert result["candidates"] == []
    assert result["self_contradictory_count"] == 0


@pytest.mark.asyncio
async def test_diagnose_reports_contended_by_other_items(db):
    pid = await _project(db)
    coarse = await db_module.add_sprint_item(
        db, pid, "v1", "holds the whole shared file",
        touches_resources=["file:shared.py"],
    )
    await db_module.add_sprint_item(
        db, pid, "v1", "wants symbol x only", touches_resources=["symbol:shared.py::x"],
        force=True,
    )
    await db_module.add_sprint_item(
        db, pid, "v1", "wants symbol y only", touches_resources=["symbol:shared.py::y"],
        force=True,
    )

    result = await db_module.diagnose_resource_coarseness(db, pid, version="v1")
    hit = next(c for c in result["candidates"] if c["id"] == coarse["id"])
    assert hit["self_contradictory_symbols"] == []
    assert hit["contended_by"] == 2


@pytest.mark.asyncio
async def test_repair_narrows_self_contradictory_item(db):
    pid = await _project(db)
    item = await db_module.add_sprint_item(
        db, pid, "v1", "coarse plus specific",
        touches_resources=["file:a.py", "symbol:a.py::foo", "file:unrelated.py"],
    )

    result = await db_module.repair_resource_granularity(db, pid, item["id"])
    assert result["repaired"] is True
    assert result["reason"] is None
    assert result["removed_file_resources"] == ["file:a.py"]
    assert sorted(result["kept_resources"]) == ["file:unrelated.py", "symbol:a.py::foo"]

    after = await db_module.get_sprint_item(db, item["id"])
    stored = db_module.parse_touches_resources(after.get("touches_resources"))
    assert "file:a.py" not in stored
    assert "symbol:a.py::foo" in stored
    assert "file:unrelated.py" in stored


@pytest.mark.asyncio
async def test_repair_refuses_active_claim(db):
    pid = await _project(db)
    sess = await db_module.register_session(db, pid, "claimer")
    item = await db_module.add_sprint_item(
        db, pid, "v1", "coarse but about to be claimed",
        touches_resources=["file:b.py", "symbol:b.py::foo"],
        prospect_bypass=True,
    )
    claimed = await db_module.claim_sprint_item(db, pid, item["id"], actor=sess["id"])
    assert claimed["status"] == "in_progress"

    result = await db_module.repair_resource_granularity(db, pid, item["id"])
    assert result["repaired"] is False
    assert result["reason"] == "item_claimed"

    after = await db_module.get_sprint_item(db, item["id"])
    stored = db_module.parse_touches_resources(after.get("touches_resources"))
    assert "file:b.py" in stored  # untouched — 1b264ce3's hard rule


@pytest.mark.asyncio
async def test_repair_dry_run_previews_without_mutating(db):
    pid = await _project(db)
    item = await db_module.add_sprint_item(
        db, pid, "v1", "dry run candidate",
        touches_resources=["file:c.py", "symbol:c.py::foo"],
    )

    preview = await db_module.repair_resource_granularity(db, pid, item["id"], dry_run=True)
    assert preview["repaired"] is False
    assert preview["reason"] is None
    assert preview["removed_file_resources"] == ["file:c.py"]

    after = await db_module.get_sprint_item(db, item["id"])
    stored = db_module.parse_touches_resources(after.get("touches_resources"))
    assert "file:c.py" in stored  # dry run: nothing actually written


@pytest.mark.asyncio
async def test_repair_nothing_to_do_without_symbol_sibling(db):
    pid = await _project(db)
    item = await db_module.add_sprint_item(
        db, pid, "v1", "plain whole-file item, no evidence either way",
        touches_resources=["file:d.py"],
    )

    result = await db_module.repair_resource_granularity(db, pid, item["id"])
    assert result["repaired"] is False
    assert result["reason"] == "nothing_self_contradictory"

    after = await db_module.get_sprint_item(db, item["id"])
    stored = db_module.parse_touches_resources(after.get("touches_resources"))
    assert stored == ["file:d.py"]


@pytest.mark.asyncio
async def test_repaired_item_cobatches_after_narrowing(db):
    """End-to-end: the exact value proposition of Wave 1 on top of Wave 0.
    Before repair, a self-contradictory file:+symbol: declaration forces an
    item into its own group (file grain conflicts with ANY symbol in that
    file, including a genuinely disjoint one declared elsewhere). After
    repair, the two disjoint symbols co-batch."""
    pid = await _project(db)
    coarse = await db_module.add_sprint_item(
        db, pid, "v1", "coarse but really only touches foo",
        touches_resources=["file:shared3.py", "symbol:shared3.py::foo"],
    )
    other = await db_module.add_sprint_item(
        db, pid, "v1", "genuinely only touches bar",
        touches_resources=["symbol:shared3.py::bar"], force=True,
    )

    def _group_ids(result):
        return [{it["id"] for it in grp} for grp in result["groups"]]

    before = await db_module.get_parallelizable_groups(db, pid, version="v1")
    before_groups = _group_ids(before)
    assert not any({coarse["id"], other["id"]} <= g for g in before_groups)

    repair = await db_module.repair_resource_granularity(db, pid, coarse["id"])
    assert repair["repaired"] is True

    after = await db_module.get_parallelizable_groups(db, pid, version="v1")
    after_groups = _group_ids(after)
    assert any({coarse["id"], other["id"]} <= g for g in after_groups)
