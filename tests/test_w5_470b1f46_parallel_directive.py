"""470b1f46 — start_session's orchestration hint must actively DIRECT parallel
fan-out (not just present group data) when the board is parallel-safe."""
from __future__ import annotations

import pytest

import meridian.db as dbm
import meridian.server as srv


def _grouping(groups):
    return {
        "groups": groups,
        "group_count": len(groups),
        "eligible_count": sum(len(g) for g in groups),
        "blocked": [],
        "undeclared_count": 0,
    }


@pytest.mark.asyncio
async def test_parallel_directive_present_when_a_group_has_multiple_items(monkeypatch):
    async def fake_groups(db, pid, ver):
        return _grouping([
            [{"id": "a", "title": "A"}, {"id": "b", "title": "B"}],
            [{"id": "c", "title": "C"}],
        ])
    monkeypatch.setattr(dbm, "get_parallelizable_groups", fake_groups)
    hint = await srv._build_orchestration_hint(None, "p1", None)
    assert hint["recommended_strategy"] == "parallel"
    assert "parallel_directive" in hint
    d = hint["parallel_directive"]
    assert "PARALLELIZE" in d
    # Reports how many groups can actually run concurrently (here: 1).
    assert "1 group" in d


@pytest.mark.asyncio
async def test_no_parallel_directive_when_all_groups_single_item(monkeypatch):
    async def fake_groups(db, pid, ver):
        return _grouping([[{"id": "a", "title": "A"}], [{"id": "b", "title": "B"}]])
    monkeypatch.setattr(dbm, "get_parallelizable_groups", fake_groups)
    hint = await srv._build_orchestration_hint(None, "p1", None)
    assert hint["recommended_strategy"] == "sequential"
    assert "parallel_directive" not in hint
