"""20c9adef — Postgres timestamp sub-second precision.

_TS / _DATETIME_NOW_EXPR now use 'YYYY-MM-DD HH24:MI:SS.US' so rapid-fire
rows created within the same wall-clock second get genuinely distinct
created_at strings (microsecond resolution).

Tests here run on Postgres only (db_pg fixture); SQLite's datetime('now')
remains at second precision and is intentionally not changed.
"""
from __future__ import annotations

import re

import pytest
import pytest_asyncio

from meridian import db as db_module


_TS_US_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{6}$"
)
"""YYYY-MM-DD HH:MM:SS.ffffff — the new Postgres timestamp format."""


@pytest.mark.asyncio
async def test_pg_timestamp_format_has_microseconds(db_pg) -> None:
    """A freshly-inserted row's created_at must include microseconds (.US).

    The Postgres DB-side DEFAULT expression now emits 26-character timestamps
    ('YYYY-MM-DD HH24:MI:SS.US'); this test asserts the actual stored format.
    """
    p = await db_module.create_project(db_pg, "ts-us-fmt")
    s = await db_module.register_session(db_pg, p["id"], "sess-fmt")
    t = await db_module.log_task(db_pg, s["id"], p["id"], "check format", "done")
    raw_ts = t.get("created_at") or ""
    assert _TS_US_PATTERN.match(raw_ts), (
        f"Expected 'YYYY-MM-DD HH:MM:SS.ffffff' microsecond format, got: {raw_ts!r}"
    )


@pytest.mark.asyncio
async def test_pg_rapid_inserts_have_distinct_timestamps(db_pg) -> None:
    """Two rapid-fire task_log inserts within the same second must get distinct
    created_at values on Postgres, so ordering queries return a stable sort.

    This is the core regression guard for 20c9adef: the old second-precision
    format made rows created in the same second IDENTICAL, breaking any
    "newest-first" ordering test whose inserts completed faster than 1 s.
    With microsecond precision, clock_timestamp() distinguishes them.
    """
    p = await db_module.create_project(db_pg, "ts-us-order")
    s = await db_module.register_session(db_pg, p["id"], "sess-order")
    t1 = await db_module.log_task(db_pg, s["id"], p["id"], "first task", "done")
    t2 = await db_module.log_task(db_pg, s["id"], p["id"], "second task", "done")
    t3 = await db_module.log_task(db_pg, s["id"], p["id"], "third task", "failed")

    ts1 = t1.get("created_at") or ""
    ts2 = t2.get("created_at") or ""
    ts3 = t3.get("created_at") or ""

    # All three must match the microsecond format.
    for ts, label in [(ts1, "t1"), (ts2, "t2"), (ts3, "t3")]:
        assert _TS_US_PATTERN.match(ts), (
            f"{label}.created_at should be microsecond format, got: {ts!r}"
        )

    # The three timestamps must be strictly increasing (distinguishable).
    assert ts1 < ts2 < ts3, (
        f"Rapid-fire inserts must have strictly ordered microsecond timestamps: "
        f"{ts1!r} < {ts2!r} < {ts3!r}"
    )

    # get_tasks returns newest-first; ordering must be deterministic.
    tasks = await db_module.get_tasks(db_pg, p["id"], limit=10)
    assert [t["id"] for t in tasks] == [t3["id"], t2["id"], t1["id"]], (
        "get_tasks must return tasks newest-first (created_at DESC); "
        "sub-second timestamps are required for deterministic ordering within "
        "the same wall-clock second."
    )
