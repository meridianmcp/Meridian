"""Activity-by-domain daily aggregation on get_project_stats (c975b6ef).

GROUP BY source_type + day over the pointer primitive: which
docs/web/experiment/code/citation targets were touched each day. Daily aggregate
totals only (never time-of-day/session detail).
"""
from __future__ import annotations

import pytest

from meridian import db as db_module


@pytest.mark.asyncio
async def test_activity_by_domain_aggregates_pointer_source_types(db):
    p = await db_module.create_project(db, "activity")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "cite the sources")
    specs = [
        ("web", "https://x/a", {"type": "text_quote", "exact": "q1"}),
        ("web", "https://x/b", {"type": "text_quote", "exact": "q2"}),
        ("docs", "doc:1", {"type": "node_id", "id": "e1"}),
        ("code", "a.py", {"type": "range", "start_line": 1, "end_line": 2}),
    ]
    for st, uri, sel in specs:
        await db_module.add_sprint_item_pointer(
            db, p["id"], item["id"], st, [{"uri": uri, "selector": sel}]
        )

    stats = await db_module.get_project_stats(db, p["id"], 30)
    assert "activity_by_domain" in stats and "activity_domains" in stats
    assert set(stats["activity_domains"]) == {"web", "docs", "code"}
    # Per-day rows cover the whole window; the days-list length matches period.
    assert len(stats["activity_by_domain"]) == 30
    # Aggregate across days: web=2, docs=1, code=1 (all created today).
    agg: dict[str, int] = {}
    for d in stats["activity_by_domain"]:
        assert d["total"] == sum(d["by_domain"].values())
        for k, v in d["by_domain"].items():
            agg[k] = agg.get(k, 0) + v
    assert agg == {"web": 2, "docs": 1, "code": 1}


@pytest.mark.asyncio
async def test_activity_by_domain_empty_when_no_pointers(db):
    p = await db_module.create_project(db, "activity-empty")
    stats = await db_module.get_project_stats(db, p["id"], 14)
    assert stats["activity_domains"] == []
    assert all(d["total"] == 0 and d["by_domain"] == {} for d in stats["activity_by_domain"])


def test_activity_stats_endpoint_includes_domains(client):
    pid = client.post("/projects", json={"name": "activity-ep"}).json()["id"]
    r = client.get(f"/projects/{pid}/stats")
    assert r.status_code == 200
    body = r.json()
    assert "activity_by_domain" in body and "activity_domains" in body
