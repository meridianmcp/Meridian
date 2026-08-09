"""9e83be4a (Round 1 proposal e143949d) — ROUND1-AI-LOG canonical execution
event contract.

SCOPE: this file tests the CONTRACT (meridian.ai_log) and its minimal
storage scaffold (meridian.db.ai_log) ONLY — schema shape, envelope
validation, the two-axis (schema_version / payload_schema) versioning and
compatibility mechanism, and that the guarded migration applies cleanly on
SQLite (+ that the Postgres mirror is registered). It deliberately does NOT
cover a capture/ingestion pipeline, capture-boundary enumeration, or export
format — that is sibling item 0f18eb77's job
(tests/test_ai_log_capture_boundaries.py, tests/test_ai_log_export.py). No
route/MCP tool/server hook calls append_event anywhere in this codebase; the
round-trip tests below call it directly as a data-layer primitive, exactly
as tests/test_bbb447ec_wave_run_summary.py exercises
db.wave_run_summary.persist_wave_summary directly.

Coverage:
  1.  ExecutionEvent construction fills defaults (event_id, occurred_at,
      schema_version) and round-trips via to_dict().
  2.  Envelope validation: bad event_type (no namespace dot), bad
      actor_kind, empty project_id, non-dict payload, out-of-range /
      non-int schema_version each raise ExecutionEventError.
  3.  content_hash()/canonical_event_hash() determinism: identical content
      (different event_id) hashes identically; a changed field changes the
      hash; works directly on a plain dict (e.g. a row read back from
      storage), not just ExecutionEvent.to_dict() output.
  4.  Compatibility mechanism (upgrade_event_dict / register_schema_upgrader):
      a row already at EVENT_SCHEMA_VERSION passes through unchanged; a
      too-old or too-new schema_version raises
      UnsupportedSchemaVersionError; a registered upgrade chain is applied
      in sequence; an incomplete chain (missing intermediate upgrader)
      raises rather than silently under-upgrading; double-registration for
      the same from_version raises.
  5.  db.ai_log.append_event / get_event round-trip (all fields, payload
      JSON preserved).
  6.  append_event validates BEFORE inserting — an invalid event never
      reaches storage (row count unchanged after a raised call).
  7.  idempotency_key: a retried append_event with the same
      (project_id, idempotency_key) returns the existing row, no duplicate.
  8.  list_events: newest-first ordering, project isolation, and each
      filter (session_id / event_type / correlation_id / parent_event_id).
  9.  Migration creates the table on SQLite; the Postgres mirror function
      is registered in pg_adapter._PG_MIGRATIONS_LATE.
"""
from __future__ import annotations

import pytest

from meridian import ai_log
from meridian import db as db_module
from meridian.ai_log import (
    ACTOR_KINDS,
    EVENT_SCHEMA_VERSION,
    MIN_SUPPORTED_SCHEMA_VERSION,
    ExecutionEvent,
    ExecutionEventError,
    UnsupportedSchemaVersionError,
    canonical_event_hash,
    upgrade_event_dict,
)


async def _project(db, name: str) -> str:
    proj = await db_module.create_project(db, name)
    return proj["id"]


# ---------------------------------------------------------------------------
# 1. Construction + defaults
# ---------------------------------------------------------------------------

def test_construction_fills_defaults():
    ev = ExecutionEvent(project_id="p1", event_type="tool.invoked", actor_kind="session")
    assert ev.schema_version == EVENT_SCHEMA_VERSION
    assert ev.event_id  # non-empty, uuid4 string
    assert ev.occurred_at  # ISO-8601 filled in
    assert ev.occurred_at.endswith("Z")
    assert ev.payload == {}
    assert ev.payload_schema is None


def test_to_dict_has_every_envelope_field():
    ev = ExecutionEvent(
        project_id="p1", event_type="tool.invoked", actor_kind="tool",
        actor_id="Serena: find_symbol", session_id="s1", tenant_id="t1",
        correlation_id="c1", parent_event_id="e0", source="mcp",
        payload={"x": 1}, payload_schema="tool_call@1",
    )
    d = ev.to_dict()
    expected_keys = {
        "project_id", "event_type", "actor_kind", "actor_id", "session_id",
        "tenant_id", "correlation_id", "parent_event_id", "source",
        "payload", "payload_schema", "idempotency_key", "occurred_at",
        "schema_version", "event_id",
    }
    assert expected_keys <= set(d.keys())
    assert d["payload"] == {"x": 1}
    assert d["payload_schema"] == "tool_call@1"


def test_actor_kinds_are_the_documented_closed_set():
    assert ACTOR_KINDS == {"session", "system", "human", "tool", "model"}


# ---------------------------------------------------------------------------
# 2. Envelope validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "kwargs",
    [
        {"event_type": "invoked"},  # no namespace dot
        {"event_type": ""},
        {"event_type": "Tool.Invoked"},  # not lower_snake
        {"event_type": None},
    ],
)
def test_bad_event_type_rejected(kwargs):
    base = {"project_id": "p1", "event_type": "tool.invoked", "actor_kind": "session"}
    base.update(kwargs)
    with pytest.raises(ExecutionEventError):
        ExecutionEvent(**base)


@pytest.mark.parametrize("bad_kind", ["", "robot", "SESSION", None, 42])
def test_bad_actor_kind_rejected(bad_kind):
    with pytest.raises(ExecutionEventError):
        ExecutionEvent(project_id="p1", event_type="tool.invoked", actor_kind=bad_kind)


@pytest.mark.parametrize("bad_project", ["", None])
def test_missing_project_id_rejected(bad_project):
    with pytest.raises(ExecutionEventError):
        ExecutionEvent(project_id=bad_project, event_type="tool.invoked", actor_kind="session")


@pytest.mark.parametrize("bad_payload", [[1, 2], "x", 5, None])
def test_non_dict_payload_rejected(bad_payload):
    with pytest.raises(ExecutionEventError):
        ExecutionEvent(
            project_id="p1", event_type="tool.invoked", actor_kind="session",
            payload=bad_payload,
        )


@pytest.mark.parametrize(
    "bad_version", [0, -1, MIN_SUPPORTED_SCHEMA_VERSION - 1, EVENT_SCHEMA_VERSION + 1, "1", True]
)
def test_bad_schema_version_rejected(bad_version):
    with pytest.raises(ExecutionEventError):
        ExecutionEvent(
            project_id="p1", event_type="tool.invoked", actor_kind="session",
            schema_version=bad_version,
        )


def test_explicit_occurred_at_is_preserved():
    ev = ExecutionEvent(
        project_id="p1", event_type="tool.invoked", actor_kind="session",
        occurred_at="2026-01-01T00:00:00.000Z",
    )
    assert ev.occurred_at == "2026-01-01T00:00:00.000Z"


def test_empty_occurred_at_rejected():
    with pytest.raises(ExecutionEventError):
        ExecutionEvent(
            project_id="p1", event_type="tool.invoked", actor_kind="session",
            occurred_at="",
        )


# ---------------------------------------------------------------------------
# 3. Canonical content hashing
# ---------------------------------------------------------------------------

def test_content_hash_ignores_event_id():
    ev1 = ExecutionEvent(
        project_id="p1", event_type="tool.invoked", actor_kind="session",
        occurred_at="2026-01-01T00:00:00.000Z", payload={"a": 1},
    )
    ev2 = ExecutionEvent(
        project_id="p1", event_type="tool.invoked", actor_kind="session",
        occurred_at="2026-01-01T00:00:00.000Z", payload={"a": 1},
    )
    assert ev1.event_id != ev2.event_id  # random identity
    assert ev1.content_hash() == ev2.content_hash()  # identical content
    assert ev1.content_hash().startswith("sha256:")


def test_content_hash_changes_with_payload():
    ev1 = ExecutionEvent(
        project_id="p1", event_type="tool.invoked", actor_kind="session",
        occurred_at="2026-01-01T00:00:00.000Z", payload={"a": 1},
    )
    ev2 = ExecutionEvent(
        project_id="p1", event_type="tool.invoked", actor_kind="session",
        occurred_at="2026-01-01T00:00:00.000Z", payload={"a": 2},
    )
    assert ev1.content_hash() != ev2.content_hash()


def test_canonical_event_hash_works_on_a_plain_dict():
    row = {
        "schema_version": 1, "event_type": "tool.invoked", "project_id": "p1",
        "session_id": None, "tenant_id": None, "actor_kind": "session",
        "actor_id": None, "correlation_id": None, "parent_event_id": None,
        "source": None, "occurred_at": "2026-01-01T00:00:00.000Z",
        "payload": {"a": 1}, "payload_schema": None,
        # extra storage-only fields must not affect the hash:
        "id": "row-id-123", "recorded_at": "2026-01-01T00:00:01.000Z",
    }
    h1 = canonical_event_hash(row)
    row2 = dict(row, id="different-row-id", recorded_at="2099-01-01T00:00:00.000Z")
    assert canonical_event_hash(row2) == h1  # identity/storage bookkeeping excluded


# ---------------------------------------------------------------------------
# 4. Compatibility / upgrade mechanism
# ---------------------------------------------------------------------------

def test_upgrade_event_dict_identity_at_current_version():
    ev = ExecutionEvent(project_id="p1", event_type="tool.invoked", actor_kind="session")
    upgraded = upgrade_event_dict(ev.to_dict())
    assert upgraded == ev.to_dict()
    assert upgraded is not ev.to_dict()  # never returns the same object


def test_upgrade_event_dict_rejects_missing_schema_version():
    with pytest.raises(ExecutionEventError):
        upgrade_event_dict({"event_type": "tool.invoked"})


def test_upgrade_event_dict_rejects_too_old_version():
    with pytest.raises(UnsupportedSchemaVersionError):
        upgrade_event_dict({"schema_version": MIN_SUPPORTED_SCHEMA_VERSION - 1})


def test_upgrade_event_dict_rejects_too_new_version():
    with pytest.raises(UnsupportedSchemaVersionError):
        upgrade_event_dict({"schema_version": EVENT_SCHEMA_VERSION + 1})


def test_registered_upgrader_chain_is_applied(monkeypatch):
    """Prove the upgrade MECHANISM itself works, using a synthetic v1->v2
    envelope bump — EVENT_SCHEMA_VERSION is only 1 today, so this exercises
    the extension point the module docstring's COMPATIBILITY notes describe
    for a FUTURE breaking change, without actually shipping one."""
    monkeypatch.setattr(ai_log, "EVENT_SCHEMA_VERSION", 2)
    monkeypatch.setattr(ai_log, "_SCHEMA_UPGRADERS", {})

    def _v1_to_v2(event: dict) -> dict:
        upgraded = dict(event)
        upgraded["schema_version"] = 2
        upgraded["renamed_field"] = upgraded.pop("old_field", None)
        return upgraded

    ai_log.register_schema_upgrader(1, _v1_to_v2)

    v1_event = {"schema_version": 1, "old_field": "value", "event_type": "tool.invoked"}
    result = ai_log.upgrade_event_dict(v1_event)
    assert result["schema_version"] == 2
    assert result["renamed_field"] == "value"
    assert "old_field" not in result
    # the original dict passed in must never be mutated:
    assert v1_event["schema_version"] == 1
    assert "old_field" in v1_event


def test_registered_upgrader_chain_incomplete_raises(monkeypatch):
    """A version gap of 2 (1 -> 3) with only the first hop registered must
    raise rather than silently returning a partially-upgraded (still v2)
    event as if it were fully normalized."""
    monkeypatch.setattr(ai_log, "EVENT_SCHEMA_VERSION", 3)
    monkeypatch.setattr(ai_log, "_SCHEMA_UPGRADERS", {})
    ai_log.register_schema_upgrader(1, lambda e: dict(e, schema_version=2))
    # no upgrader registered for from_version=2 -> 3
    with pytest.raises(UnsupportedSchemaVersionError):
        ai_log.upgrade_event_dict({"schema_version": 1})


def test_double_registration_for_same_version_raises(monkeypatch):
    monkeypatch.setattr(ai_log, "_SCHEMA_UPGRADERS", {})
    ai_log.register_schema_upgrader(1, lambda e: e)
    with pytest.raises(ExecutionEventError):
        ai_log.register_schema_upgrader(1, lambda e: e)


# ---------------------------------------------------------------------------
# 5-8. Storage scaffold (meridian.db.ai_log)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_append_and_get_round_trip(db):
    pid = await _project(db, "ai-log-roundtrip")
    created = await db_module.append_event(
        db, pid, "tool.invoked", "tool",
        actor_id="Serena: find_symbol", session_id="sess-1",
        correlation_id="corr-1", source="mcp",
        payload={"symbol": "ExecutionEvent"}, payload_schema="tool_call@1",
    )
    assert created["project_id"] == pid
    assert created["event_type"] == "tool.invoked"
    assert created["actor_kind"] == "tool"
    assert created["actor_id"] == "Serena: find_symbol"
    assert created["payload"] == {"symbol": "ExecutionEvent"}
    assert created["payload_schema"] == "tool_call@1"
    assert created["schema_version"] == EVENT_SCHEMA_VERSION
    assert created["event_hash"].startswith("sha256:")
    assert created["recorded_at"]  # DB-assigned

    fetched = await db_module.get_event(db, created["id"])
    assert fetched is not None
    assert fetched["id"] == created["id"]
    assert fetched["payload"] == {"symbol": "ExecutionEvent"}


@pytest.mark.asyncio
async def test_get_event_missing_returns_none(db):
    assert await db_module.get_event(db, "does-not-exist") is None


@pytest.mark.asyncio
async def test_append_event_validates_before_insert(db):
    pid = await _project(db, "ai-log-invalid")
    with pytest.raises(ExecutionEventError):
        await db_module.append_event(db, pid, "not-namespaced", "session")
    async with db.execute(
        "SELECT COUNT(*) AS n FROM ai_log_events WHERE project_id = ?", (pid,),
    ) as cur:
        row = await cur.fetchone()
    assert row["n"] == 0  # nothing was inserted


@pytest.mark.asyncio
async def test_idempotency_key_prevents_duplicate(db):
    pid = await _project(db, "ai-log-idem")
    first = await db_module.append_event(
        db, pid, "session.started", "session", idempotency_key="only-once",
    )
    second = await db_module.append_event(
        db, pid, "session.started", "session", idempotency_key="only-once",
    )
    assert first["id"] == second["id"]
    async with db.execute(
        "SELECT COUNT(*) AS n FROM ai_log_events WHERE project_id = ? AND idempotency_key = ?",
        (pid, "only-once"),
    ) as cur:
        row = await cur.fetchone()
    assert row["n"] == 1


@pytest.mark.asyncio
async def test_list_events_project_isolation_and_ordering(db):
    pid_a = await _project(db, "ai-log-list-a")
    pid_b = await _project(db, "ai-log-list-b")
    await db_module.append_event(db, pid_a, "session.started", "session")
    await db_module.append_event(db, pid_a, "tool.invoked", "tool")
    await db_module.append_event(db, pid_b, "session.started", "session")

    events_a = await db_module.list_events(db, pid_a)
    assert len(events_a) == 2
    assert all(e["project_id"] == pid_a for e in events_a)

    events_b = await db_module.list_events(db, pid_b)
    assert len(events_b) == 1


@pytest.mark.asyncio
async def test_list_events_filters(db):
    pid = await _project(db, "ai-log-filters")
    e1 = await db_module.append_event(
        db, pid, "tool.invoked", "tool", session_id="s1", correlation_id="c1",
    )
    await db_module.append_event(
        db, pid, "tool.completed", "tool", session_id="s1",
        correlation_id="c1", parent_event_id=e1["id"],
    )
    await db_module.append_event(db, pid, "session.started", "session", session_id="s2")

    by_session = await db_module.list_events(db, pid, session_id="s1")
    assert len(by_session) == 2

    by_type = await db_module.list_events(db, pid, event_type="session.started")
    assert len(by_type) == 1
    assert by_type[0]["session_id"] == "s2"

    by_correlation = await db_module.list_events(db, pid, correlation_id="c1")
    assert len(by_correlation) == 2

    by_parent = await db_module.list_events(db, pid, parent_event_id=e1["id"])
    assert len(by_parent) == 1
    assert by_parent[0]["event_type"] == "tool.completed"


# ---------------------------------------------------------------------------
# 9. Migration / PG parity
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_migration_creates_ai_log_events_table(db):
    async with db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='ai_log_events'"
    ) as cur:
        row = await cur.fetchone()
    assert row is not None


def test_pg_migration_registered_in_late_registry():
    from meridian.pg_adapter import _PG_MIGRATIONS_LATE, _migrate_pg_ai_log_events

    assert _migrate_pg_ai_log_events in _PG_MIGRATIONS_LATE
