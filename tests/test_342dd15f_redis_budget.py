"""342dd15f — Per-tenant Redis command budget for the send_message
push-augmentation path.

Tests cover:
  - compute_redis_overage_action pure-function logic (Tier-1 warn, Tier-2
    hard_limit, None when below threshold, idempotency via notification_prefs).
  - publish_session_message: budget enforcement via _get_redis_commands_used
    and _increment_redis_commands helpers.
  - Budget check is a safe-open no-op when tenant_id/db are absent (self-hosted).
  - Tier-2 DISABLE: publish returns False without touching Redis when counter
    is at or above REDIS_BUDGET_DISABLE_COMMANDS.
  - Tier-1 counter increment on successful publish.
  - DB migration: redis_commands_used and redis_overage_cap_usd columns land
    with correct defaults (SQLite path; Postgres path guarded by db_pg fixture).
  - PG migration registry wiring: _migrate_pg_redis_overage_fields in HOSTED.
  - Migration registry count bump (covered by test_pg_migration_registry).
"""
from __future__ import annotations

import asyncio
import json

import pytest

from meridian import db as db_module
from meridian import redis_bridge
from meridian.hosted import (
    REDIS_BUDGET_DISABLE_COMMANDS,
    REDIS_BUDGET_WARN_COMMANDS,
    compute_redis_overage_action,
    _parse_notification_prefs,
    _with_redis_flag_recorded,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tenant(*, used: int = 0, prefs: dict | None = None, is_internal: bool = False) -> dict:
    """Minimal tenant dict for pure-function tests."""
    raw_prefs = json.dumps(prefs or {})
    return {
        "id": "t-test",
        "plan": "standard",
        "redis_commands_used": used,
        "notification_prefs": raw_prefs,
        "is_internal": is_internal,
    }


# ---------------------------------------------------------------------------
# compute_redis_overage_action — pure function
# ---------------------------------------------------------------------------


def test_below_tier1_returns_none():
    tenant = _tenant(used=0)
    assert compute_redis_overage_action(tenant) is None


def test_at_tier1_threshold_returns_warn_when_flag_not_set():
    tenant = _tenant(used=REDIS_BUDGET_WARN_COMMANDS)
    assert compute_redis_overage_action(tenant) == "warn"


def test_above_tier1_still_returns_warn_when_flag_not_set():
    tenant = _tenant(used=REDIS_BUDGET_WARN_COMMANDS + 1000)
    assert compute_redis_overage_action(tenant) == "warn"


def test_at_tier1_returns_none_when_warn_flag_already_set():
    """Idempotent — once the warning is sent, don't resend this month."""
    tenant = _tenant(used=REDIS_BUDGET_WARN_COMMANDS, prefs={"redis_warn_sent": True})
    assert compute_redis_overage_action(tenant) is None


def test_at_tier2_threshold_returns_hard_limit_when_not_sent():
    tenant = _tenant(used=REDIS_BUDGET_DISABLE_COMMANDS)
    assert compute_redis_overage_action(tenant) == "hard_limit"


def test_above_tier2_returns_hard_limit_when_not_sent():
    tenant = _tenant(used=REDIS_BUDGET_DISABLE_COMMANDS + 5000)
    assert compute_redis_overage_action(tenant) == "hard_limit"


def test_at_tier2_returns_none_when_hard_limit_flag_already_set():
    """Idempotent — hard-limit email only once per month."""
    tenant = _tenant(
        used=REDIS_BUDGET_DISABLE_COMMANDS,
        prefs={"redis_warn_sent": True, "redis_hard_limit_sent": True},
    )
    assert compute_redis_overage_action(tenant) is None


def test_internal_tenant_always_returns_none():
    """Internal tenants are exempt from budget alerts."""
    tenant = _tenant(used=REDIS_BUDGET_DISABLE_COMMANDS + 999_999, is_internal=True)
    assert compute_redis_overage_action(tenant) is None


def test_warn_flag_recorded_correctly():
    tenant = _tenant(used=0, prefs={"other_key": True})
    result = _with_redis_flag_recorded(tenant, "redis_warn_sent")
    prefs = json.loads(result)
    assert prefs["redis_warn_sent"] is True
    # Preserves other keys.
    assert prefs.get("other_key") is True


def test_hard_limit_flag_recorded_correctly():
    tenant = _tenant(used=0, prefs={"redis_warn_sent": True})
    result = _with_redis_flag_recorded(tenant, "redis_hard_limit_sent")
    prefs = json.loads(result)
    assert prefs["redis_hard_limit_sent"] is True
    assert prefs.get("redis_warn_sent") is True  # preserves existing flag


def test_parse_notification_prefs_tolerates_malformed():
    tenant = {"notification_prefs": "not-valid-json"}
    assert _parse_notification_prefs(tenant) == {}

    tenant2 = {"notification_prefs": None}
    assert _parse_notification_prefs(tenant2) == {}

    tenant3 = {}
    assert _parse_notification_prefs(tenant3) == {}


# ---------------------------------------------------------------------------
# DB migration: new columns land in init_db
# ---------------------------------------------------------------------------


def test_migration_adds_redis_columns_to_tenants():
    """redis_commands_used and redis_overage_cap_usd must be present after
    init_db (the SQLite migration path), so hosted-layer functions can read
    and write them without an AttributeError."""
    async def _run():
        db = await db_module.init_db(":memory:")
        try:
            # Create a minimal tenant row (without the new columns) by
            # bypassing the ORM and checking the PRAGMA directly.
            async with db.execute("PRAGMA table_info(tenants)") as cur:
                rows = await cur.fetchall()
            col_names = {
                (r["name"] if isinstance(r, dict) else r[1]) for r in rows
            }
            assert "redis_commands_used" in col_names, (
                "redis_commands_used column missing from tenants after init_db"
            )
            assert "redis_overage_cap_usd" in col_names, (
                "redis_overage_cap_usd column missing from tenants after init_db"
            )
        finally:
            await db.close()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# redis_bridge.publish_session_message — budget enforcement
# ---------------------------------------------------------------------------


class _FakeRedisClient:
    def __init__(self):
        self.published: list[tuple[str, str]] = []

    async def publish(self, channel: str, data: str) -> int:
        self.published.append((channel, data))
        return 0


@pytest.fixture(autouse=True)
def _reset_redis_bridge_cache(monkeypatch):
    redis_bridge.reset_redis_client_cache()
    monkeypatch.delenv("MERIDIAN_REDIS_URL", raising=False)
    yield
    redis_bridge.reset_redis_client_cache()


def test_publish_returns_false_and_skips_redis_when_over_tier2(monkeypatch):
    """Tier-2 DISABLE: when tenant's counter >= REDIS_BUDGET_DISABLE_COMMANDS,
    publish_session_message must return False without calling Redis at all."""
    fake = _FakeRedisClient()

    async def _fake_get_client():
        return fake

    monkeypatch.setattr(redis_bridge, "get_redis_client", _fake_get_client)

    async def _run():
        db = await db_module.init_db(":memory:")
        try:
            # Seed a tenant row with counter at the Tier-2 limit.
            await db.execute(
                "INSERT INTO tenants (id, email, plan, redis_commands_used, notification_prefs) "
                "VALUES (?, ?, ?, ?, ?)",
                ("t-over", "over@test.com", "standard",
                 REDIS_BUDGET_DISABLE_COMMANDS, "{}"),
            )
            await db.commit()

            ok = await redis_bridge.publish_session_message(
                "session-1",
                {"id": "m1", "payload": "x"},
                tenant_id="t-over",
                db=db,
            )
            assert ok is False, "Expected False (Tier-2 block)"
            assert len(fake.published) == 0, "Redis must not be called when over Tier-2"
        finally:
            await db.close()

    asyncio.run(_run())


def test_publish_succeeds_and_increments_counter_when_under_budget(monkeypatch):
    """Under budget: publish should succeed and increment redis_commands_used."""
    fake = _FakeRedisClient()

    async def _fake_get_client():
        return fake

    monkeypatch.setattr(redis_bridge, "get_redis_client", _fake_get_client)

    async def _run():
        db = await db_module.init_db(":memory:")
        try:
            await db.execute(
                "INSERT INTO tenants (id, email, plan, redis_commands_used, notification_prefs) "
                "VALUES (?, ?, ?, ?, ?)",
                ("t-under", "under@test.com", "standard", 100, "{}"),
            )
            await db.commit()

            ok = await redis_bridge.publish_session_message(
                "session-2",
                {"id": "m2", "payload": "y"},
                tenant_id="t-under",
                db=db,
            )
            assert ok is True
            assert len(fake.published) == 1

            # Counter must have been incremented.
            async with db.execute(
                "SELECT redis_commands_used FROM tenants WHERE id = ?", ("t-under",)
            ) as cur:
                row = await cur.fetchone()
            val = row["redis_commands_used"] if isinstance(row, dict) else row[0]
            assert int(val) == 101
        finally:
            await db.close()

    asyncio.run(_run())


def test_publish_without_tenant_id_is_unchecked_no_db_interaction(monkeypatch):
    """When tenant_id is absent (self-hosted path), budget logic is skipped
    entirely and publish proceeds normally."""
    fake = _FakeRedisClient()

    async def _fake_get_client():
        return fake

    monkeypatch.setattr(redis_bridge, "get_redis_client", _fake_get_client)

    async def _run():
        ok = await redis_bridge.publish_session_message(
            "session-3", {"id": "m3", "payload": "z"}
            # no tenant_id, no db
        )
        assert ok is True
        assert len(fake.published) == 1

    asyncio.run(_run())


def test_publish_budget_check_is_safe_open_on_db_error(monkeypatch):
    """If the DB query for redis_commands_used raises, the publish still
    proceeds (safe-open contract: budget check never blocks messages)."""
    fake = _FakeRedisClient()

    async def _fake_get_client():
        return fake

    monkeypatch.setattr(redis_bridge, "get_redis_client", _fake_get_client)

    class _BrokenCursor:
        async def __aenter__(self):
            raise RuntimeError("DB unavailable")

        async def __aexit__(self, *a):
            pass

    class _BrokenDb:
        def execute(self, *a, **kw):
            return _BrokenCursor()

        async def commit(self):
            pass

    async def _run():
        ok = await redis_bridge.publish_session_message(
            "session-4",
            {"id": "m4", "payload": "w"},
            tenant_id="t-broken",
            db=_BrokenDb(),
        )
        # Safe-open: should still succeed despite DB error during budget check.
        assert ok is True

    asyncio.run(_run())


def test_budget_constants_match_between_bridge_and_hosted():
    """redis_bridge and hosted.py must expose the same tier thresholds so
    enforcement and alerting are in sync."""
    from meridian.redis_bridge import (
        REDIS_BUDGET_WARN_COMMANDS as BRIDGE_WARN,
        REDIS_BUDGET_DISABLE_COMMANDS as BRIDGE_DISABLE,
        REDIS_BUDGET_ADMIN_ALERT_COMMANDS,
    )
    from meridian.hosted import (
        REDIS_BUDGET_WARN_COMMANDS as HOSTED_WARN,
        REDIS_BUDGET_DISABLE_COMMANDS as HOSTED_DISABLE,
    )

    assert BRIDGE_WARN == HOSTED_WARN == 500_000
    assert BRIDGE_DISABLE == HOSTED_DISABLE == 1_000_000
    assert REDIS_BUDGET_ADMIN_ALERT_COMMANDS == 2_000_000


# ---------------------------------------------------------------------------
# Migration wiring guard: PG registry must include the redis overage migration
# ---------------------------------------------------------------------------


def test_pg_migration_hosted_registry_includes_redis_overage_fields():
    """_migrate_pg_redis_overage_fields MUST be in _PG_MIGRATIONS_HOSTED so it
    runs on the main auth DB on startup.

    This is the wiring guard that should have caught the original prod bug:
    the function existed in code but was never added to the HOSTED registry
    tuple, so the columns were never created on the live tenants table and
    the $1/$2/$4 tier cost gates silently failed-open (counter always read 0).
    """
    from meridian import pg_adapter as pg_module

    hosted_names = [f.__name__ for f in pg_module._PG_MIGRATIONS_HOSTED]
    assert "_migrate_pg_redis_overage_fields" in hosted_names, (
        "_migrate_pg_redis_overage_fields is missing from _PG_MIGRATIONS_HOSTED — "
        "the redis budget columns will never be created on the live tenants table, "
        "making the Tier-1/2/3 cost gates permanently fail-open."
    )


def test_migration_redis_columns_have_sane_defaults():
    """After init_db, a freshly inserted tenant row must carry the right default
    values for the redis budget columns:
      - redis_commands_used  defaults to 0  (no commands issued yet)
      - redis_overage_cap_usd  defaults to NULL  (use code-defined tier limits)
    """
    async def _run():
        db = await db_module.init_db(":memory:")
        try:
            await db.execute(
                "INSERT INTO tenants (id, email, plan, notification_prefs) "
                "VALUES (?, ?, ?, ?)",
                ("t-defaults", "defaults@test.com", "standard", "{}"),
            )
            await db.commit()
            async with db.execute(
                "SELECT redis_commands_used, redis_overage_cap_usd "
                "FROM tenants WHERE id = ?",
                ("t-defaults",),
            ) as cur:
                row = await cur.fetchone()
            used = row["redis_commands_used"] if isinstance(row, dict) else row[0]
            cap = row["redis_overage_cap_usd"] if isinstance(row, dict) else row[1]
            assert int(used or 0) == 0, (
                f"redis_commands_used default should be 0, got {used!r}"
            )
            assert cap is None, (
                f"redis_overage_cap_usd default should be NULL, got {cap!r}"
            )
        finally:
            await db.close()

    asyncio.run(_run())


def test_cost_gate_is_not_fail_open_when_columns_exist(monkeypatch):
    """End-to-end gate: with columns present and counter AT the Tier-2 limit,
    the gate must NOT fail-open.  Verifies the entire path from DB column
    through _get_redis_commands_used to the publish short-circuit.

    This is the regression test for the prod bug: when redis_commands_used
    is missing, _get_redis_commands_used returns 0 (safe-open fallback), so
    the gate never fires regardless of real spend.  With the columns present
    and populated, Tier-2 must hard-block.
    """
    fake = _FakeRedisClient()

    async def _fake_get_client():
        return fake

    monkeypatch.setattr(redis_bridge, "get_redis_client", _fake_get_client)

    async def _run():
        db = await db_module.init_db(":memory:")
        try:
            await db.execute(
                "INSERT INTO tenants (id, email, plan, redis_commands_used, notification_prefs) "
                "VALUES (?, ?, ?, ?, ?)",
                ("t-gate", "gate@test.com", "standard",
                 REDIS_BUDGET_DISABLE_COMMANDS, "{}"),
            )
            await db.commit()

            # With the column present and counter at Tier-2, must block.
            result = await redis_bridge.publish_session_message(
                "sess-gate",
                {"id": "m-gate", "payload": "data"},
                tenant_id="t-gate",
                db=db,
            )
            assert result is False, (
                "Tier-2 gate failed-open: publish returned True despite "
                f"redis_commands_used={REDIS_BUDGET_DISABLE_COMMANDS}"
            )
            assert len(fake.published) == 0, (
                "Redis PUBLISH was called despite Tier-2 block — gate is broken"
            )
        finally:
            await db.close()

    asyncio.run(_run())


@pytest.mark.asyncio
async def test_pg_migration_adds_redis_columns_to_tenants(db_pg):
    """Postgres path: _migrate_pg_redis_overage_fields (in _PG_MIGRATIONS_HOSTED)
    must add redis_commands_used and redis_overage_cap_usd to the tenants table.

    Skipped unless TEST_DATABASE_URL is set. The SQLite equivalent is covered by
    test_migration_adds_redis_columns_to_tenants.
    """
    async with db_pg.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'tenants'",
    ) as cur:
        rows = await cur.fetchall()
    col_names = {r["column_name"] if isinstance(r, dict) else r[0] for r in rows}
    assert "redis_commands_used" in col_names, (
        "redis_commands_used missing from Postgres tenants after migration"
    )
    assert "redis_overage_cap_usd" in col_names, (
        "redis_overage_cap_usd missing from Postgres tenants after migration"
    )
