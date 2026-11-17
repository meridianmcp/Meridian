"""Plan store and owner-approval CLI tests. No network and no real InvenTree."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from meridian_inventree import plans
from meridian_inventree.approve_cli import (
    EXIT_APPROVED,
    EXIT_NOT_INTERACTIVE,
    EXIT_REFUSED,
    NOT_INTERACTIVE_MESSAGE,
    main,
)
from meridian_inventree.audit import AuditLog
from meridian_inventree.plans import ALLOWED_OPS, MAX_OPERATIONS, PlanError, PlanStore

PACKAGE_ROOT = Path(__file__).resolve().parents[1]


class _Terminal(io.StringIO):
    """Stands in for an interactive stdin."""

    def isatty(self) -> bool:
        return True


def _op(index: int, op: str = "create_part") -> dict[str, Any]:
    return {"op": op, "client_key": f"key-{index}", "params": {"name": f"Part {index}", "category": 3}}


def _preview(index: int) -> dict[str, Any]:
    return {"before": {}, "after": {"name": f"Part {index}", "category": 3}}


@pytest.fixture
def audit_path(tmp_path: Path) -> Path:
    return tmp_path / "audit" / "inventree.jsonl"


@pytest.fixture
def store(tmp_path: Path, audit_path: Path) -> PlanStore:
    # propose and approve need the audit log, the same file approve_cli writes to.
    return PlanStore(tmp_path / "plans", audit=AuditLog(audit_path))


@pytest.fixture
def proposed(store: PlanStore) -> dict[str, Any]:
    return store.propose([_op(0), _op(1)], [_preview(0), _preview(1)])


def _disk(store: PlanStore, plan_id: str) -> dict[str, Any]:
    return json.loads((store.root_dir / f"{plan_id}.json").read_text(encoding="utf-8"))


def _write_disk(store: PlanStore, plan_id: str, data: dict[str, Any]) -> None:
    (store.root_dir / f"{plan_id}.json").write_text(json.dumps(data), encoding="utf-8")


def _code(excinfo: pytest.ExceptionInfo[PlanError]) -> str:
    return excinfo.value.code


def _run_cli(
    store: PlanStore,
    audit_path: Path,
    plan_id: str,
    answer: str | BaseException | None,
    *,
    stdin: io.TextIOBase | None = None,
) -> tuple[int, str, str]:
    """Run approve_cli.main. answer=None means the owner must not be asked anything."""
    out, err = io.StringIO(), io.StringIO()

    def ask(prompt: str) -> str:
        if answer is None:
            raise AssertionError("the owner must not be prompted")
        if isinstance(answer, BaseException):
            raise answer
        return answer

    code = main(
        ["--plans-dir", str(store.root_dir), "--audit-file", str(audit_path), plan_id],
        stdin=stdin if stdin is not None else _Terminal(),
        stdout=out,
        stderr=err,
        read_line=ask,
    )
    return code, out.getvalue(), err.getvalue()


# --- propose ---------------------------------------------------------------


def test_propose_stores_a_proposed_plan_and_nothing_else(store: PlanStore, proposed: dict[str, Any]) -> None:
    assert proposed["status"] == "proposed"
    assert proposed["approval"] is None
    assert proposed["executed_at"] is None
    assert proposed["results"] is None
    assert len(proposed["plan_hash"]) == 64
    assert proposed["plan_hash"] == plans.compute_plan_hash(proposed["operations"], proposed["previews"])
    assert _disk(store, proposed["plan_id"]) == proposed
    assert [p.name for p in store.root_dir.iterdir()] == [f"{proposed['plan_id']}.json"]


@pytest.mark.parametrize("op_name", sorted(ALLOWED_OPS))
def test_each_v1_write_operation_is_accepted(store: PlanStore, op_name: str) -> None:
    plan = store.propose([_op(0, op_name)], [_preview(0)])
    assert plan["operations"][0]["op"] == op_name


def test_the_allowed_set_is_exactly_the_v1_write_operations() -> None:
    assert ALLOWED_OPS == frozenset(
        {"create_part", "update_part", "create_location", "create_stock_item", "transfer_stock", "adjust_stock_quantity"}
    )


def test_fifty_operations_are_allowed_and_fifty_one_are_refused(store: PlanStore) -> None:
    ops = [_op(i) for i in range(MAX_OPERATIONS)]
    previews = [_preview(i) for i in range(MAX_OPERATIONS)]
    assert len(store.propose(ops, previews)["operations"]) == MAX_OPERATIONS
    with pytest.raises(PlanError) as excinfo:
        store.propose([_op(i) for i in range(MAX_OPERATIONS + 1)], [_preview(i) for i in range(MAX_OPERATIONS + 1)])
    assert _code(excinfo) == "too_many_operations"


@pytest.mark.parametrize(
    ("operations", "previews", "code"),
    [
        ([], [], "empty_plan"),
        ("not a list", [], "invalid_operation"),
        ([_op(0, "delete_part")], [_preview(0)], "unknown_operation"),
        ([_op(0, "create_user")], [_preview(0)], "unknown_operation"),
        ([_op(0), _op(0)], [_preview(0), _preview(1)], "duplicate_client_key"),
        ([_op(0), _op(1)], [_preview(0)], "preview_mismatch"),
        ([{**_op(0), "extra": True}], [_preview(0)], "invalid_operation"),
        ([{**_op(0), "params": {"bad": object()}}], [_preview(0)], "invalid_operation"),
        ([{"op": "create_part", "client_key": "  ", "params": {}}], [_preview(0)], "invalid_operation"),
        ([{"op": "create_part", "client_key": "k", "params": []}], [_preview(0)], "invalid_operation"),
        ([_op(0)], [{"before": {}}], "invalid_preview"),
        ([_op(0)], [{"before": [], "after": {}}], "invalid_preview"),
    ],
)
def test_invalid_plans_are_refused_and_nothing_is_written(
    store: PlanStore, operations: Any, previews: Any, code: str
) -> None:
    with pytest.raises(PlanError) as excinfo:
        store.propose(operations, previews)
    assert _code(excinfo) == code
    assert list(store.root_dir.glob("*.json")) == []


@pytest.mark.parametrize("bad", ["../../secrets", "A" * 32, "abc", "", "0" * 31 + "/"])
def test_plan_ids_must_be_plain_lowercase_hex(store: PlanStore, bad: str) -> None:
    with pytest.raises(PlanError) as excinfo:
        store.get(bad)
    assert _code(excinfo) == "invalid_plan_id"


def test_unknown_plan_id_is_not_found(store: PlanStore) -> None:
    with pytest.raises(PlanError) as excinfo:
        store.get("0" * 32)
    assert _code(excinfo) == "not_found"


# --- execute gate ----------------------------------------------------------


def test_proposed_plan_cannot_execute(store: PlanStore, proposed: dict[str, Any]) -> None:
    with pytest.raises(PlanError) as excinfo:
        store.mark_executed(proposed["plan_id"], [])
    assert _code(excinfo) == "not_approved"
    assert store.get(proposed["plan_id"])["status"] == "proposed"


def test_approved_plan_executes_once(store: PlanStore, proposed: dict[str, Any]) -> None:
    plan_id = proposed["plan_id"]
    approved = store.approve(plan_id, proposed["plan_hash"])
    assert approved["status"] == "approved"
    assert approved["approval"]["approved_by"] == "owner"
    assert approved["approval"]["approved_hash"] == proposed["plan_hash"]

    # An approved plan was never started, so it cannot be recorded as run.
    with pytest.raises(PlanError) as excinfo:
        store.mark_executed(plan_id, [{"client_key": "key-0", "ok": True}])
    assert _code(excinfo) == "not_running"
    store.mark_running(plan_id)

    done = store.mark_executed(plan_id, [{"client_key": "key-0", "ok": True}])
    assert done["status"] == "executed"
    assert done["executed_at"] is not None
    assert store.get(plan_id)["results"] == [{"client_key": "key-0", "ok": True}]

    with pytest.raises(PlanError) as excinfo:
        store.mark_executed(plan_id, [])
    assert _code(excinfo) == "already_executed"


def test_failed_run_is_recorded_and_terminal(store: PlanStore, proposed: dict[str, Any]) -> None:
    plan_id = proposed["plan_id"]
    store.approve(plan_id, proposed["plan_hash"])
    store.mark_running(plan_id)
    failed = store.mark_executed(plan_id, [{"client_key": "key-0", "ok": False}], failed=True)
    assert failed["status"] == "failed"
    with pytest.raises(PlanError) as excinfo:
        store.mark_executed(plan_id, [])
    assert _code(excinfo) == "already_executed"


def test_approval_is_refused_twice_and_after_running(store: PlanStore, proposed: dict[str, Any]) -> None:
    plan_id = proposed["plan_id"]
    store.approve(plan_id, proposed["plan_hash"])
    with pytest.raises(PlanError) as excinfo:
        store.approve(plan_id, proposed["plan_hash"])
    assert _code(excinfo) == "already_approved"
    store.mark_running(plan_id)
    store.mark_executed(plan_id, [])
    with pytest.raises(PlanError) as excinfo:
        store.approve(plan_id, proposed["plan_hash"])
    assert _code(excinfo) == "already_executed"


def test_only_the_owner_can_approve(store: PlanStore, proposed: dict[str, Any]) -> None:
    with pytest.raises(PlanError) as excinfo:
        store.approve(proposed["plan_id"], proposed["plan_hash"], approved_by="agent")
    assert _code(excinfo) == "not_owner"
    assert _disk(store, proposed["plan_id"])["status"] == "proposed"


# --- edits and wrong hashes ------------------------------------------------


def test_edited_operations_are_refused_before_approval(store: PlanStore, proposed: dict[str, Any]) -> None:
    plan_id = proposed["plan_id"]
    data = _disk(store, plan_id)
    data["operations"][0]["params"]["category"] = 99
    _write_disk(store, plan_id, data)
    with pytest.raises(PlanError) as excinfo:
        store.approve(plan_id, proposed["plan_hash"])
    assert _code(excinfo) == "plan_tampered"
    assert _disk(store, plan_id)["status"] == "proposed"
    assert _disk(store, plan_id)["approval"] is None


def test_edited_plan_is_refused_at_execution(store: PlanStore, proposed: dict[str, Any]) -> None:
    plan_id = proposed["plan_id"]
    store.approve(plan_id, proposed["plan_hash"])
    data = _disk(store, plan_id)
    data["previews"][1]["after"]["name"] = "Swapped"
    _write_disk(store, plan_id, data)
    with pytest.raises(PlanError) as excinfo:
        store.mark_executed(plan_id, [])
    assert _code(excinfo) == "plan_tampered"
    assert _disk(store, plan_id)["status"] == "approved"
    with pytest.raises(PlanError) as excinfo:
        store.get(plan_id)
    assert _code(excinfo) == "plan_tampered"


def test_rehashed_edit_still_fails_the_approval_check(store: PlanStore, proposed: dict[str, Any]) -> None:
    plan_id = proposed["plan_id"]
    store.approve(plan_id, proposed["plan_hash"])
    data = _disk(store, plan_id)
    data["operations"][0]["params"]["category"] = 99
    data["plan_hash"] = plans.compute_plan_hash(data["operations"], data["previews"])
    _write_disk(store, plan_id, data)
    with pytest.raises(PlanError) as excinfo:
        store.mark_running(plan_id)
    assert _code(excinfo) == "approval_mismatch"
    assert _disk(store, plan_id)["status"] == "approved"


def test_plan_id_inside_the_file_must_match_its_name(store: PlanStore, proposed: dict[str, Any]) -> None:
    plan_id = proposed["plan_id"]
    data = _disk(store, plan_id)
    data["plan_id"] = "f" * 32
    _write_disk(store, plan_id, data)
    with pytest.raises(PlanError) as excinfo:
        store.get(plan_id)
    assert _code(excinfo) == "plan_tampered"


def test_wrong_typed_hash_is_refused(store: PlanStore, proposed: dict[str, Any]) -> None:
    plan_id = proposed["plan_id"]
    real = proposed["plan_hash"]
    lock = store.root_dir / f"{plan_id}.lock"
    for typed in ["0" * 64, real.upper(), real + " ", real[:-1], "", None]:
        with pytest.raises(PlanError) as excinfo:
            store.approve(plan_id, typed)  # type: ignore[arg-type]
        assert _code(excinfo) == "hash_mismatch"
    assert _disk(store, plan_id)["status"] == "proposed"
    assert _disk(store, plan_id)["approval"] is None
    assert not lock.exists()


def test_held_lock_reports_busy_and_is_not_removed(
    store: PlanStore, proposed: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(plans, "_LOCK_WAIT_SECONDS", 0.05)
    plan_id = proposed["plan_id"]
    lock = store.root_dir / f"{plan_id}.lock"
    lock.write_text("held", encoding="utf-8")
    with pytest.raises(PlanError) as excinfo:
        store.approve(plan_id, proposed["plan_hash"])
    assert _code(excinfo) == "plan_busy"
    assert lock.exists()
    assert _disk(store, plan_id)["status"] == "proposed"


def test_abandoned_lock_is_cleared(store: PlanStore, proposed: dict[str, Any]) -> None:
    plan_id = proposed["plan_id"]
    lock = store.root_dir / f"{plan_id}.lock"
    lock.write_text("abandoned", encoding="utf-8")
    old = time.time() - 10_000
    os.utime(lock, (old, old))
    approved = store.approve(plan_id, proposed["plan_hash"])
    assert approved["status"] == "approved"
    assert not lock.exists()


# --- non-TTY and terminal approval -----------------------------------------


def test_non_tty_approval_is_refused_in_process(store: PlanStore, proposed: dict[str, Any], audit_path: Path) -> None:
    code, _out, err = _run_cli(store, audit_path, proposed["plan_id"], None, stdin=io.StringIO())
    assert code == EXIT_NOT_INTERACTIVE == 2
    assert err == NOT_INTERACTIVE_MESSAGE + "\n"
    assert _disk(store, proposed["plan_id"])["status"] == "proposed"
    assert not audit_path.exists()


def test_null_device_is_not_a_terminal(store: PlanStore, proposed: dict[str, Any], audit_path: Path) -> None:
    # On Windows NUL reports isatty() as True. The console check must still refuse it.
    with open(os.devnull, encoding="utf-8") as devnull:
        code, out, err = _run_cli(store, audit_path, proposed["plan_id"], None, stdin=devnull)  # type: ignore[arg-type]
    assert code == EXIT_NOT_INTERACTIVE
    assert err == NOT_INTERACTIVE_MESSAGE + "\n"
    assert "plan_hash:" not in out
    assert _disk(store, proposed["plan_id"])["status"] == "proposed"


def test_non_tty_approval_exits_with_code_2_from_the_command_line(
    store: PlanStore, proposed: dict[str, Any], audit_path: Path
) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "meridian_inventree.approve_cli",
            "--plans-dir",
            str(store.root_dir),
            "--audit-file",
            str(audit_path),
            proposed["plan_id"],
        ],
        cwd=PACKAGE_ROOT,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 2
    assert NOT_INTERACTIVE_MESSAGE in completed.stderr
    assert "plan_hash:" not in completed.stdout
    assert _disk(store, proposed["plan_id"])["status"] == "proposed"


def test_owner_types_the_hash_and_the_plan_is_approved(
    store: PlanStore, proposed: dict[str, Any], audit_path: Path
) -> None:
    code, out, _err = _run_cli(store, audit_path, proposed["plan_id"], proposed["plan_hash"])
    assert code == EXIT_APPROVED == 0
    assert proposed["plan_hash"] in out
    assert _disk(store, proposed["plan_id"])["status"] == "approved"
    events = AuditLog(audit_path).read_events()
    assert [event["event"] for event in events] == ["plan_approved"]
    assert events[0]["plan_hash"] == proposed["plan_hash"]
    assert events[0]["approved_by"] == "owner"


def test_wrong_typed_hash_is_refused_and_audited(store: PlanStore, proposed: dict[str, Any], audit_path: Path) -> None:
    code, _out, err = _run_cli(store, audit_path, proposed["plan_id"], "not-the-hash")
    assert code == EXIT_REFUSED == 1
    assert "hash_mismatch" in err
    assert _disk(store, proposed["plan_id"])["status"] == "proposed"
    events = AuditLog(audit_path).read_events()
    assert events[-1]["event"] == "plan_approval_refused"
    assert events[-1]["reason"] == "hash_mismatch"
    assert "not-the-hash" not in audit_path.read_text(encoding="utf-8")


def test_closed_input_refuses_without_approving(store: PlanStore, proposed: dict[str, Any], audit_path: Path) -> None:
    code, _out, _err = _run_cli(store, audit_path, proposed["plan_id"], EOFError())
    assert code == EXIT_REFUSED
    assert _disk(store, proposed["plan_id"])["status"] == "proposed"


def test_summary_escapes_terminal_control_characters(store: PlanStore, audit_path: Path) -> None:
    plan = store.propose([{"op": "create_part", "client_key": "k\x1b[2J", "params": {}}], [_preview(0)])
    code, out, _err = _run_cli(store, audit_path, plan["plan_id"], "wrong")
    assert code == EXIT_REFUSED
    assert "\x1b" not in out
    assert "\\u001b" in out


@pytest.mark.parametrize("status_step", ["approve", "running", "execute"])
def test_plan_that_is_not_proposed_is_not_prompted(
    store: PlanStore, proposed: dict[str, Any], audit_path: Path, status_step: str
) -> None:
    plan_id = proposed["plan_id"]
    store.approve(plan_id, proposed["plan_hash"])
    if status_step in ("running", "execute"):
        store.mark_running(plan_id)
    if status_step == "execute":
        store.mark_executed(plan_id, [])
    code, _out, err = _run_cli(store, audit_path, plan_id, None)
    assert code == EXIT_REFUSED
    assert "only a proposed plan can be approved" in err


def test_unknown_plan_is_refused_from_the_command_line(store: PlanStore, audit_path: Path) -> None:
    code, _out, err = _run_cli(store, audit_path, "0" * 32, None)
    assert code == EXIT_REFUSED
    assert "not_found" in err


# --- running state (fix 2) ---------------------------------------------------


def test_mark_running_moves_only_an_approved_plan(store: PlanStore, proposed: dict[str, Any]) -> None:
    plan_id = proposed["plan_id"]
    with pytest.raises(PlanError) as excinfo:
        store.mark_running(plan_id)
    assert _code(excinfo) == "not_approved"
    assert _disk(store, plan_id)["status"] == "proposed"

    store.approve(plan_id, proposed["plan_hash"])
    running = store.mark_running(plan_id)
    assert running["status"] == "running"
    assert running["running_at"] is not None
    assert _disk(store, plan_id)["status"] == "running"

    # A running plan cannot be approved again, started again, or recorded without a result step.
    with pytest.raises(PlanError) as excinfo:
        store.approve(plan_id, proposed["plan_hash"])
    assert _code(excinfo) == "invalid_status"
    with pytest.raises(PlanError) as excinfo:
        store.mark_running(plan_id)
    assert _code(excinfo) == "already_running"

    store.mark_executed(plan_id, [{"client_key": "key-0", "ok": True}])
    with pytest.raises(PlanError) as excinfo:
        store.mark_running(plan_id)
    assert _code(excinfo) == "already_executed"


# --- approval shows the stored request (fix 5) -------------------------------


def test_approval_summary_shows_the_stored_request(store: PlanStore, audit_path: Path) -> None:
    previews = [
        {
            "before": {},
            "after": {"name": "Part 0"},
            "op": "create_part",
            "method": "POST",
            "path": "/api/part/",
            "body": {"name": "Part 0", "active": True},
        }
    ]
    plan = store.propose([_op(0)], previews)
    code, out, _err = _run_cli(store, audit_path, plan["plan_id"], "wrong")
    assert code == EXIT_REFUSED
    assert '["POST", "/api/part/", {"active": true, "name": "Part 0"}]' in out


# --- long values are printed in full, wrapped, never cut (fix B) ------------


def test_approval_summary_prints_a_long_notes_field_in_full(store: PlanStore, audit_path: Path) -> None:
    # No spaces in the notes, so the wrapped lines can be joined back into the exact JSON text.
    notes = "ab12" * 150  # 600 characters, well past the old 240-character cut
    previews = [
        {
            "before": {},
            "after": {"notes": notes},
            "method": "POST",
            "path": "/api/stock/",
            "body": {"part": 7, "quantity": "1", "notes": notes},
        }
    ]
    plan = store.propose(
        [{"op": "create_stock_item", "client_key": "k-long", "params": {"part": 7, "quantity": 1, "notes": notes}}],
        previews,
    )
    code, out, _err = _run_cli(store, audit_path, plan["plan_id"], "wrong")
    assert code == EXIT_REFUSED
    lines = out.splitlines()
    assert max(len(line) for line in lines) <= 100  # wrapped, so no line runs off the terminal
    joined = "".join(line.strip() for line in lines)
    assert joined.count(json.dumps(notes)) >= 3  # in params, in the stored request body, and in after
    assert "..." not in out


# --- a plan file held open on Windows is retried, then refused clearly (fix D) ---


def test_replace_retries_a_locked_plan_file_then_refuses_clearly(
    store: PlanStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_replace = os.replace
    pauses: list[float] = []
    attempts = {"count": 0}

    def fails_twice(source: str, target: object) -> None:
        attempts["count"] += 1
        if attempts["count"] <= 2:
            raise PermissionError(32, "the plan file is open in another process")
        real_replace(source, target)

    monkeypatch.setattr(plans.os, "replace", fails_twice)
    monkeypatch.setattr(plans.time, "sleep", pauses.append)
    proposed = store.propose([_op(0)], [_preview(0)])
    assert attempts["count"] == 3  # two refused attempts, then the third one succeeds
    assert pauses == [0.05, 0.05]
    assert _disk(store, proposed["plan_id"])["status"] == "proposed"
    assert [p.name for p in store.root_dir.iterdir()] == [f"{proposed['plan_id']}.json"]

    # A file that stays locked: six attempts in all, a clear refusal, and the plan file left as it was.
    attempts["count"] = 0
    pauses.clear()

    def always_locked(source: str, target: object) -> None:
        attempts["count"] += 1
        raise PermissionError(32, "the plan file is open in another process")

    monkeypatch.setattr(plans.os, "replace", always_locked)
    with pytest.raises(PlanError) as excinfo:
        store.approve(proposed["plan_id"], proposed["plan_hash"])
    assert _code(excinfo) == "plan_file_locked"
    assert attempts["count"] == 6
    assert pauses == [0.05] * 5
    assert _disk(store, proposed["plan_id"])["status"] == "proposed"
    assert [p.name for p in store.root_dir.iterdir()] == [f"{proposed['plan_id']}.json"]


# --- fix: a client_key with an unresolved unknown outcome is refused at propose and approve -------------


def test_client_key_with_an_unresolved_unknown_outcome_is_refused_at_propose_and_approve(
    store: PlanStore, audit_path: Path
) -> None:
    earlier = store.propose([_op(0)], [_preview(0)])  # proposed before the unknown outcome was recorded
    audit = AuditLog(audit_path)
    audit.append({"event": "op_unknown", "plan_id": "f" * 32, "client_key": "key-0", "status": "unknown"})

    with pytest.raises(PlanError) as info:
        store.propose([_op(0)], [_preview(0)])
    assert _code(info) == "key_unresolved"
    with pytest.raises(PlanError) as info:
        store.approve(earlier["plan_id"], earlier["plan_hash"])
    assert _code(info) == "key_unresolved"
    assert _disk(store, earlier["plan_id"])["status"] == "proposed"

    # An automatic op_failed does not settle an unknown outcome, because the first request may have been applied.
    audit.append({"event": "op_failed", "plan_id": "f" * 32, "client_key": "key-0", "status": "failed"})
    with pytest.raises(PlanError) as info:
        store.propose([_op(0)], [_preview(0)])
    assert _code(info) == "key_unresolved"

    # The owner's manual record settles it, and the key can be used again.
    audit.append(
        {"event": "op_failed", "plan_id": "f" * 32, "client_key": "key-0", "status": "failed", "manual": True}
    )
    assert store.propose([_op(0)], [_preview(0)])["status"] == "proposed"


def test_a_new_plan_with_an_unresolved_key_is_refused_at_propose(store: PlanStore, audit_path: Path) -> None:
    # A start with no settling event means the write may have reached InvenTree, so propose refuses the key.
    AuditLog(audit_path).append({"event": "op_started", "plan_id": "e" * 32, "client_key": "key-0"})

    with pytest.raises(PlanError) as info:
        store.propose([_op(0)], [_preview(0)])
    assert _code(info) == "key_unresolved"
    assert list(store.root_dir.iterdir()) == []  # the refused plan was not stored


# --- fix: every plan field printed at approval is checked first, and an unsafe one is never printed -----


def test_created_at_with_a_newline_and_an_escape_is_refused_at_approval_and_never_printed(
    store: PlanStore, proposed: dict[str, Any], audit_path: Path
) -> None:
    plan_id = proposed["plan_id"]
    data = _disk(store, plan_id)
    data["created_at"] = "2026-11-11T00:00:00+00:00\n\x1b[2J\x1b[31mAPPROVED"  # not covered by plan_hash
    _write_disk(store, plan_id, data)

    # answer=None: the owner must not be asked, because nothing may be shown first.
    code, out, err = _run_cli(store, audit_path, plan_id, None)

    assert code == EXIT_REFUSED == 1
    assert out == ""  # no part of the plan was printed
    assert "\x1b" not in err
    assert "APPROVED" not in err
    assert "unsafe_display" in err
    assert _disk(store, plan_id)["status"] == "proposed"
    assert AuditLog(audit_path).read_events()[-1]["reason"] == "unsafe_display"
