"""Regression guard for sprint item a8ff4caa — CI test-core/test-postgres
wall-clock roughly doubled (7-8m -> 13-15m) since August, with meridian/server.py's
``lifespan()`` accretion the live suspect.

INVESTIGATION FINDING (2026-09-24, this item): a fresh, completed
``pytest tests/test_core.py --durations=25 -p no:xdist --timeout=120 -q`` run
against current dev (see item notes / task log for the full table) shows NO
setup-phase cost anywhere in the top 25 slowest tests, and a total serial
wall-clock of 182s for 1129 passed + 6 skipped -- actually LOWER than the July
baseline of 313s for 946 tests, despite ~190 more tests being added since.

That is because the specific accretion this item hypothesized (the lifespan's
unconditional ``open_doc_store_for`` call resolving to a real on-disk SQLite
sidecar per test) was ALREADY fixed by two commits that landed on `dev` in the
24 hours before this investigation:
  - 2c3fe304 "perf(tests): default MERIDIAN_DOC_STORE_URL to :memory: in
    client fixture" (2026-09-23)
  - 0cbef4c9 "fix(tests): stop leaking MERIDIAN_DOC_STORE_URL across xdist
    workers" (2026-09-24)
Both already carry their own targeted regression coverage
(tests/test_doc_store.py::test_client_fixture_defaults_doc_store_to_memory
and the corrected env-var handling in
tests/test_5fdf8858_no_double_json_encode.py).

This file adds the piece that was still missing: a regression test for the
GENERAL CLASS of bug (lifespan() accreting per-test-fixture-setup cost),
not just the one already-fixed instance. Had a test like this existed in
July, it would have caught the doc-store regression the same day it was
introduced instead of two months later via a slow, easy-to-miss climb in
GitHub Actions run history.

Deliberately self-contained: it does NOT import tests/conftest.py's private
helpers, because this repo has multiple same-named ``tests`` packages
(e.g. ``extensions/meridian-codeindex/tests``) and, with no ``tests/__init__.py``
anywhere, Python's implicit-namespace-package resolution is not guaranteed to
pick *this* ``tests/conftest.py`` -- confirmed live: ``from tests.conftest
import _open_sqlite_from_template`` actually resolved to the extensions
package's conftest instead. So the fast-path setup (schema-template backup
copy) is duplicated here in miniature, deliberately, rather than imported.
"""
from __future__ import annotations

import os
import time

import aiosqlite
import pytest


@pytest.fixture(scope="module")
def _perf_schema_template(tmp_path_factory):
    """Build the SQLite schema once for this module (mirrors
    tests/conftest.py::_sqlite_schema_template, but module-scoped and
    independent so this file never depends on conftest internals)."""
    import asyncio

    from meridian import db as db_module

    template_path = tmp_path_factory.mktemp("perf-budget-schema") / "schema.db"

    async def _build() -> None:
        conn = await db_module.init_db(str(template_path))
        await conn.close()

    asyncio.run(_build())
    return template_path


async def _fresh_conn_from_template(template_path) -> aiosqlite.Connection:
    source = await aiosqlite.connect(str(template_path))
    try:
        target = await aiosqlite.connect(":memory:")
        await source.backup(target)
        target.row_factory = aiosqlite.Row
        await target.execute("PRAGMA foreign_keys = ON")
        return target
    finally:
        await source.close()


def test_client_fixture_setup_stays_within_perf_budget(
    _perf_schema_template, monkeypatch, tmp_path
):
    """Wall-clock canary for lifespan() per-TestClient-open cost.

    Opens several fresh TestClients back-to-back using the SAME fast-path
    building blocks the real ``client`` fixture uses (schema-template backup
    copy for the main DB, ``:memory:`` doc store, demo DB skipped) and
    asserts the average open+close cost stays under a generous budget.

    The budget (2.0s/iteration) is deliberately generous -- real measured
    cost as of this investigation is well under 1s per iteration even
    WITHOUT the schema-template optimization (raw ``init_db(":memory:")``
    measured ~0.7-1.3s/iteration; WITH the template fast-path it is faster
    still). This test is not trying to catch small drift -- it is a tripwire
    for exactly the class of regression this item investigated: a new
    unconditional per-test disk/network call added to lifespan() that
    multiplies out across ~400 client-fixture tests. A budget this loose
    will not flake on slower CI hardware but WILL catch a multi-x regression
    of the kind that silently doubled CI wall-clock over two months.
    """
    monkeypatch.setenv("MERIDIAN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MERIDIAN_DOC_STORE_URL", ":memory:")
    monkeypatch.setenv("MERIDIAN_DEMO_DB_URL", "")
    monkeypatch.setenv("MERIDIAN_SKIP_DEMO", "1")
    monkeypatch.setenv("MERIDIAN_DB", ":memory:")
    monkeypatch.setenv("MERIDIAN_DB_URL", "")
    monkeypatch.setenv("MERIDIAN_GOAL_MD", str(tmp_path / "GOAL.md"))
    monkeypatch.setenv("MERIDIAN_MD_ROOT", str(tmp_path))

    from meridian import db as db_module

    _real_init_db = db_module.init_db

    async def _init_db_from_template(path: str):
        if path == ":memory:":
            return await _fresh_conn_from_template(_perf_schema_template)
        return await _real_init_db(path)

    monkeypatch.setattr(db_module, "init_db", _init_db_from_template)

    from fastapi.testclient import TestClient
    import meridian.server as server_module

    N = 5
    BUDGET_S_PER_ITER = 2.0
    durations: list[float] = []
    for _ in range(N):
        t0 = time.perf_counter()
        with TestClient(server_module.app) as c:
            resp = c.get("/health")
            assert resp.status_code == 200
        durations.append(time.perf_counter() - t0)

    avg = sum(durations) / len(durations)
    assert avg < BUDGET_S_PER_ITER, (
        f"client-fixture-style TestClient open+close averaged {avg:.2f}s "
        f"over {N} iterations (budget {BUDGET_S_PER_ITER}s) -- lifespan() "
        f"has likely accreted a new unconditional per-test cost (disk I/O, "
        f"network call, or similar). See item a8ff4caa / "
        f"tests/PERF_test_core_durations.md for how to profile with "
        f"--durations=25. Per-iteration timings: "
        f"{[round(d, 3) for d in durations]}"
    )

    # No on-disk doc-store sidecar should have been created -- this is the
    # exact regression 2c3fe304/0cbef4c9 fixed; guard it here too so a
    # revert of either shows up as a budget AND a file-existence failure.
    sidecar = tmp_path / "doc_structure.db"
    assert not sidecar.exists(), (
        "doc store wrote an on-disk sidecar despite MERIDIAN_DOC_STORE_URL="
        "':memory:' -- exactly the CI-PERF-6 regression this item's "
        "investigation traced the CI slowdown to."
    )
