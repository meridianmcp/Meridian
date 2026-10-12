"""Decided additive symbol-lock changes. None of these changes what blocks.

B -- a read claim reports live symbol claims on the file as ADVISORY data
     (symbol name + holder session id only, capped at 20). Allow/deny unchanged.
C -- a coarse fallback (symbol: resource widened to a whole-file lock) writes one
     action_audit_log row per (item, file). Best-effort: a failed write is logged
     and the claim result is identical to the success case.
D -- a conflict whose blocking holder's session is idle carries owner_idle: true.
     The refusal itself is unchanged.
"""
from __future__ import annotations

import json
import logging

import pytest

import meridian.server  # noqa: F401 -- import before the handler to avoid the cycle
from meridian import db as db_module
from meridian.claim_guard import evaluate_claim_guard
from meridian.db import locks as locks_module
from meridian.mcp.handler import _sprint_item_resource_claim_gate

PY_TWO = "def alpha():\n    return 1\n\n\ndef beta():\n    return 2\n"
RECEIPT_EVENT = "resource_coarse_fallback_receipt"


async def _project(db, name: str):
    return (await db_module.create_project(db, name))["id"]


async def _set_status(db, session_id: str, status: str) -> None:
    await db.execute("UPDATE sessions SET status = ? WHERE id = ?", (status, session_id))
    await db.commit()


async def _coarse_rows(db, project_id: str) -> list[dict]:
    rows = await db_module.get_action_audit_log(
        db, project_id=project_id, event_type=RECEIPT_EVENT, limit=500,
    )
    return [{**r, **json.loads(r.get("detail") or "{}")} for r in rows]


# ---------------------------------------------------------------------------
# B -- read claims carry advisory symbol information (never blocking)
# ---------------------------------------------------------------------------

async def test_read_claim_reports_other_sessions_symbol_claims(db):
    pid = await _project(db, "b-read-advisory")
    writer = await db_module.register_session(db, pid, "writer")
    reader = await db_module.register_session(db, pid, "reader")
    assert (await db_module.claim_symbol(db, writer["id"], "pkg/mod.py", "alpha", PY_TWO))["claimed"]

    result = await db_module.claim_file(db, "pkg/mod.py", reader["id"], mode="read")

    assert result["claimed"] is True
    assert result["symbol_claims"] == [{"symbol_name": "alpha", "session_id": writer["id"]}]


async def test_read_claim_symbol_list_excludes_the_readers_own_claims(db):
    pid = await _project(db, "b-read-own")
    reader = await db_module.register_session(db, pid, "reader")
    assert (await db_module.claim_symbol(db, reader["id"], "pkg/own.py", "alpha", PY_TWO))["claimed"]

    result = await db_module.claim_file(db, "pkg/own.py", reader["id"], mode="read")

    assert result["claimed"] is True
    assert result["symbol_claims"] == []


async def test_read_claim_symbol_list_is_capped_at_20(db):
    pid = await _project(db, "b-read-cap")
    reader = await db_module.register_session(db, pid, "reader")
    source = "\n\n".join(f"def fn_{i}():\n    return {i}\n" for i in range(25))
    for i in range(25):
        s = await db_module.register_session(db, pid, f"w{i}")
        assert (await db_module.claim_symbol(db, s["id"], "pkg/big.py", f"fn_{i}", source))["claimed"]

    result = await db_module.claim_file(db, "pkg/big.py", reader["id"], mode="read")

    assert len(result["symbol_claims"]) == 20
    # Advisory entries carry the symbol name and holder session only -- no line ranges.
    assert all(set(entry) == {"symbol_name", "session_id"} for entry in result["symbol_claims"])


def test_read_mode_carries_advisory_symbol_list_without_line_ranges():
    claims = {
        "file_lock": None,
        "symbol_claims": [{"session_id": "other", "symbol_name": "alpha", "line_start": 1, "line_end": 2}],
        "read_claims": [],
    }
    r = evaluate_claim_guard(claims, "s1", mode="read")

    assert r["allow"] is True and r["reason"] == "clear"
    assert r["advisory"] == {"symbol_claims": [{"symbol_name": "alpha", "session_id": "other"}]}


def test_read_mode_advisory_never_changes_a_write_lock_denial():
    claims = {
        "file_lock": {"session_id": "other"},
        "symbol_claims": [{"session_id": "third", "symbol_name": "alpha"}],
        "read_claims": [],
    }
    r = evaluate_claim_guard(claims, "s1", mode="read")

    assert r["allow"] is False
    assert r["reason"] == "write_locked"
    assert r["holder"] == "other"


def test_read_mode_advisory_is_capped_at_20():
    claims = {
        "file_lock": None,
        "symbol_claims": [{"session_id": f"s{i}", "symbol_name": f"f{i}"} for i in range(30)],
        "read_claims": [],
    }
    r = evaluate_claim_guard(claims, "me", mode="read")

    assert len(r["advisory"]["symbol_claims"]) == 20


def test_write_mode_result_has_no_advisory_key():
    claims = {"file_lock": None, "symbol_claims": [{"session_id": "other", "symbol_name": "a"}], "read_claims": []}

    assert "advisory" not in evaluate_claim_guard(claims, "s1", mode="write")


# ---------------------------------------------------------------------------
# C -- coarse-fallback receipts (best-effort, one row per item and file)
# ---------------------------------------------------------------------------

async def test_gate_coarse_fallback_writes_one_receipt_per_item_and_file(db):
    pid = await _project(db, "c-gate-receipt")
    item = await db_module.add_sprint_item(
        db, pid, "v1", "c-gate", touches_resources=["symbol:pkg/c1.py::alpha", "symbol:pkg/c1.py::beta"],
        prospect_bypass=True,
    )
    sess = await db_module.register_session(db, pid, "w")

    gate = await _sprint_item_resource_claim_gate(db, pid, item["id"], sess["id"])

    assert gate["ok"] is True
    assert all(e["claim_granularity"] == "coarse" for e in gate["lock_scope"])
    rows = await _coarse_rows(db, pid)
    assert len(rows) == 1  # two coarse symbols in one file -> one receipt
    assert rows[0]["item_id"] == item["id"]
    assert rows[0]["file_path"] == "pkg/c1.py"
    assert rows[0]["reason"] == "no_source_supplied"

    # Re-running the gate for the same item and file does not add a second row.
    await _sprint_item_resource_claim_gate(db, pid, item["id"], sess["id"])
    assert len(await _coarse_rows(db, pid)) == 1


async def test_gate_real_symbol_lock_writes_no_coarse_receipt(db):
    pid = await _project(db, "c-gate-symbol")
    item = await db_module.add_sprint_item(
        db, pid, "v1", "c-sym", touches_resources=["symbol:pkg/c6.py::alpha"], prospect_bypass=True,
    )
    sess = await db_module.register_session(db, pid, "w")

    gate = await _sprint_item_resource_claim_gate(
        db, pid, item["id"], sess["id"], resource_contents={"pkg/c6.py": PY_TWO},
    )

    assert gate["ok"] is True
    assert gate["lock_scope"][0]["claim_granularity"] == "symbol"
    assert await _coarse_rows(db, pid) == []


async def test_gate_claim_identical_when_coarse_receipt_write_fails(db, monkeypatch, caplog):
    real_record = db_module.record_action_audit_event

    async def failing_for_coarse(conn, event_type, **kwargs):
        if event_type == RECEIPT_EVENT:
            raise RuntimeError("audit store unavailable")
        return await real_record(conn, event_type, **kwargs)

    async def gate_projection(path: str, name: str):
        pid = await _project(db, name)
        item = await db_module.add_sprint_item(
            db, pid, "v1", name, touches_resources=[f"symbol:{path}::alpha"], prospect_bypass=True,
        )
        sess = await db_module.register_session(db, pid, "w")
        gate = await _sprint_item_resource_claim_gate(db, pid, item["id"], sess["id"])
        return gate["ok"], [
            (e["scope"], e["acquired"], e["claim_granularity"], e.get("fallback_reason"))
            for e in gate["lock_scope"]
        ]

    ok_case = await gate_projection("pkg/c3ok.py", "c3-ok")
    monkeypatch.setattr(db_module, "record_action_audit_event", failing_for_coarse)
    with caplog.at_level(logging.WARNING):
        failed_case = await gate_projection("pkg/c3fail.py", "c3-fail")

    assert failed_case == ok_case
    assert failed_case[0] is True
    assert any("coarse" in r.getMessage().lower() and r.levelno >= logging.WARNING for r in caplog.records)


async def test_parallel_batch_coarse_fallback_writes_receipt(db):
    pid = await _project(db, "c-batch-receipt")
    sess = await db_module.register_session(db, pid, "orchestrator")
    a = await db_module.add_sprint_item(
        db, pid, "v1", "a", touches_resources=["symbol:pkg/c4.py::alpha"], prospect_bypass=True,
    )

    result = await db_module.claim_parallel_batch(db, pid, sess["id"], [a["id"]])

    assert result["ok"] is True
    assert result["resource_claims"][0]["claim_granularity"] == "coarse"
    rows = await _coarse_rows(db, pid)
    assert len(rows) == 1
    assert rows[0]["item_id"] == a["id"]
    assert rows[0]["file_path"] == "pkg/c4.py"


async def test_parallel_batch_claim_identical_when_coarse_receipt_write_fails(db, monkeypatch, caplog):
    real_record = db_module.record_action_audit_event

    async def failing_for_coarse(conn, event_type, **kwargs):
        if event_type == RECEIPT_EVENT:
            raise RuntimeError("audit store unavailable")
        return await real_record(conn, event_type, **kwargs)

    async def batch_projection(path: str, name: str):
        pid = await _project(db, name)
        sess = await db_module.register_session(db, pid, "orchestrator")
        item = await db_module.add_sprint_item(
            db, pid, "v1", name, touches_resources=[f"symbol:{path}::alpha"], prospect_bypass=True,
        )
        result = await db_module.claim_parallel_batch(db, pid, sess["id"], [item["id"]])
        return result["ok"], [
            (rc["scope"], rc["claim_granularity"], rc.get("fallback_reason"))
            for rc in result["resource_claims"]
        ]

    ok_case = await batch_projection("pkg/c5ok.py", "c5-ok")
    monkeypatch.setattr(db_module, "record_action_audit_event", failing_for_coarse)
    with caplog.at_level(logging.WARNING):
        failed_case = await batch_projection("pkg/c5fail.py", "c5-fail")

    assert failed_case == ok_case
    assert failed_case[0] is True
    assert any("coarse" in r.getMessage().lower() and r.levelno >= logging.WARNING for r in caplog.records)


# ---------------------------------------------------------------------------
# D -- owner_idle note on conflicts whose blocking holder is idle (non-blocking)
# ---------------------------------------------------------------------------

async def test_file_locked_conflict_flags_idle_holder(db):
    pid = await _project(db, "d-file-locked-idle")
    holder = await db_module.register_session(db, pid, "holder")
    other = await db_module.register_session(db, pid, "other")
    assert (await db_module.claim_file(db, "pkg/d1.py", holder["id"], mode="write"))["claimed"]
    await _set_status(db, holder["id"], "idle")

    result = await db_module.claim_symbol(db, other["id"], "pkg/d1.py", "alpha", PY_TWO)

    assert result["claimed"] is False
    assert result["reason"] == "file_locked"
    assert result["holder_session_id"] == holder["id"]
    assert result.get("owner_idle") is True


async def test_file_locked_conflict_has_no_owner_idle_for_active_holder(db):
    pid = await _project(db, "d-file-locked-active")
    holder = await db_module.register_session(db, pid, "holder")
    other = await db_module.register_session(db, pid, "other")
    assert (await db_module.claim_file(db, "pkg/d2.py", holder["id"], mode="write"))["claimed"]

    result = await db_module.claim_symbol(db, other["id"], "pkg/d2.py", "alpha", PY_TWO)

    assert result["claimed"] is False
    assert result["reason"] == "file_locked"
    assert "owner_idle" not in result


async def test_symbol_conflict_flags_idle_holder_without_changing_refusal(db, monkeypatch):
    pid = await _project(db, "d-symbol-conflict-idle")
    holder = await db_module.register_session(db, pid, "holder")
    other = await db_module.register_session(db, pid, "other")
    held = await db_module.claim_symbol(db, holder["id"], "pkg/d3.py", "alpha", PY_TWO)
    assert held["claimed"] is True
    await _set_status(db, holder["id"], "idle")

    # The real conflict query admits only active/live holders, so an idle holder
    # reaches this response only if its status flips after the query. Simulate
    # that window by returning the holder's claim as the conflict.
    async def conflicting_claims(conn, file_path, exclude_session_id=None):
        return [{
            "symbol_name": "alpha", "symbol_type": "function",
            "line_start": held["line_start"], "line_end": held["line_end"],
            "session_id": holder["id"], "session_name": "holder",
        }]

    monkeypatch.setattr(locks_module, "_live_symbol_claims_for_file", conflicting_claims)
    result = await db_module.claim_symbol(db, other["id"], "pkg/d3.py", "alpha", PY_TWO)

    assert result["claimed"] is False
    assert result["reason"] == "symbol_conflict"
    assert result["holder_session_id"] == holder["id"]
    assert result["owner_idle"] is True


async def test_gate_conflict_carries_owner_idle_and_stays_refused(db):
    pid = await _project(db, "d-gate-idle")
    holder = await db_module.register_session(db, pid, "holder")
    sess = await db_module.register_session(db, pid, "w")
    item = await db_module.add_sprint_item(
        db, pid, "v1", "d-gate", touches_resources=["symbol:pkg/d4.py::alpha"], prospect_bypass=True,
    )
    assert (await db_module.claim_file(db, "pkg/d4.py", holder["id"], mode="write"))["claimed"]
    await _set_status(db, holder["id"], "idle")

    gate = await _sprint_item_resource_claim_gate(
        db, pid, item["id"], sess["id"], resource_contents={"pkg/d4.py": PY_TWO},
    )

    assert gate["ok"] is False
    assert gate["error"] == "RESOURCE_LOCKED"
    assert gate["conflicts"][0]["conflict"]["owner_idle"] is True
