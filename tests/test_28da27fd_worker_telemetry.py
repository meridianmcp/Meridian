"""Tests for meridian.worker_telemetry (sprint item 28da27fd).

Layout:
  * classify_failure / extract_item_id — pure-function unit tests, PLUS
    cross-checks against the REAL meridian.enqueue._run_worker and
    meridian.dispatcher._worker_prompt output (real subprocess spawns via
    the existing test db fixture — mirrors tests/test_core.py's own
    enqueue_claude_task test pattern) so classification/extraction never
    silently drifts from what the actual worker-routing code produces.
  * observe_task — pure function, in-memory dict fixtures only.
  * WorkerTelemetryLedger — observe/attach_evidence/summarize, persistence
    round-trip, and eviction.
  * "prove no worker can edit or claim source accidentally" — a static AST
    scan of worker_telemetry.py's own source, and a dynamic test asserting
    the module's only filesystem write anywhere is its own ledger file.
"""
from __future__ import annotations

import ast
import inspect
import sys
from pathlib import Path

import pytest

from meridian import db as db_module
from meridian import dispatcher as dispatcher_module
from meridian import enqueue as enqueue_module
from meridian import worker_telemetry as wt


# ---------------------------------------------------------------------------
# classify_failure — pure unit tests
# ---------------------------------------------------------------------------


def test_classify_failure_spawn_not_found():
    desc = f"{enqueue_module.ERROR_PREFIX}p\n\nworker command not found: nope (boom)"
    assert wt.classify_failure(desc) is wt.FailureClass.SPAWN_NOT_FOUND


def test_classify_failure_spawn_error():
    desc = f"{enqueue_module.ERROR_PREFIX}p\n\nfailed to spawn worker: OSError: boom"
    assert wt.classify_failure(desc) is wt.FailureClass.SPAWN_ERROR


def test_classify_failure_timeout():
    desc = f"{enqueue_module.ERROR_PREFIX}p\n\nworker timed out after 10.0s"
    assert wt.classify_failure(desc) is wt.FailureClass.TIMEOUT


def test_classify_failure_nonzero_exit():
    desc = f"{enqueue_module.ERROR_PREFIX}p\n\nexit code 2\nboom"
    assert wt.classify_failure(desc) is wt.FailureClass.NONZERO_EXIT


def test_classify_failure_unclassified():
    assert wt.classify_failure("some other failure text") is wt.FailureClass.UNCLASSIFIED


def test_classify_failure_none_and_empty_safe():
    assert wt.classify_failure(None) is wt.FailureClass.UNCLASSIFIED
    assert wt.classify_failure("") is wt.FailureClass.UNCLASSIFIED


# ---------------------------------------------------------------------------
# classify_failure — cross-checked against REAL _run_worker output
# ---------------------------------------------------------------------------

_OK_WORKER = [sys.executable, "-c", "import sys; sys.stdout.write(sys.argv[1])"]
_FAIL_WORKER = [sys.executable, "-c", "import sys; sys.stderr.write('boom'); sys.exit(3)"]


@pytest.mark.asyncio
async def test_classify_failure_matches_real_nonzero_exit(db):
    p = await db_module.create_project(db, "wt-nonzero")
    s = await db_module.register_session(db, p["id"], "sess")
    task = await enqueue_module.enqueue_claude_task(
        db, s["id"], p["id"], "trigger failure", worker_argv=_FAIL_WORKER, wait=True,
    )
    assert task["status"] == "failed"
    assert wt.classify_failure(task["description"]) is wt.FailureClass.NONZERO_EXIT


@pytest.mark.asyncio
async def test_classify_failure_matches_real_spawn_missing(db):
    p = await db_module.create_project(db, "wt-missing")
    s = await db_module.register_session(db, p["id"], "sess")
    task = await enqueue_module.enqueue_claude_task(
        db, s["id"], p["id"], "ignored",
        worker_argv=["definitely-not-a-real-binary-28da27fd"], wait=True,
    )
    assert task["status"] == "failed"
    assert wt.classify_failure(task["description"]) is wt.FailureClass.SPAWN_NOT_FOUND


@pytest.mark.asyncio
async def test_classify_failure_matches_real_timeout(db):
    p = await db_module.create_project(db, "wt-timeout")
    s = await db_module.register_session(db, p["id"], "sess")
    slow = [sys.executable, "-c", "import time; time.sleep(5)"]
    task = await enqueue_module.enqueue_claude_task(
        db, s["id"], p["id"], "will hang", worker_argv=slow, timeout=0.5, wait=True,
    )
    assert task["status"] == "failed"
    assert wt.classify_failure(task["description"]) is wt.FailureClass.TIMEOUT


@pytest.mark.asyncio
async def test_classify_failure_none_for_successful_task(db):
    p = await db_module.create_project(db, "wt-success")
    s = await db_module.register_session(db, p["id"], "sess")
    task = await enqueue_module.enqueue_claude_task(
        db, s["id"], p["id"], "hello world", worker_argv=_OK_WORKER, wait=True,
    )
    assert task["status"] == "done"
    # classify_failure is only meaningful for failed tasks; observe_task
    # itself only classifies when status == FAILED (see below).
    record = wt.observe_task(task)
    assert record.failure_class is wt.FailureClass.NONE
    assert record.outcome is wt.WorkerOutcome.SUCCEEDED


# ---------------------------------------------------------------------------
# extract_item_id — cross-checked against the REAL dispatcher._worker_prompt
# ---------------------------------------------------------------------------


def test_extract_item_id_from_real_worker_prompt():
    item = {"id": "28da27fd-f85d-4f1f-8f5b-a90f46157e65", "title": "Do the thing", "resources": ["fileA"]}
    prompt = dispatcher_module._worker_prompt(item, "proj-123")
    # This is exactly what enqueue.enqueue_claude_task persists as the task
    # description: PROMPT_PREFIX + the dispatcher-built prompt, verbatim.
    description = f"{enqueue_module.PROMPT_PREFIX}{prompt}"
    assert wt.extract_item_id(description) == "28da27fd-f85d-4f1f-8f5b-a90f46157e65"


def test_extract_item_id_none_when_absent():
    assert wt.extract_item_id("no item id line here") is None
    assert wt.extract_item_id(None) is None
    assert wt.extract_item_id("") is None


@pytest.mark.asyncio
async def test_extract_item_id_round_trips_through_dispatcher_dispatch_once(db):
    """End-to-end: Dispatcher.dispatch_once() -> real enqueue -> real task
    row -> extract_item_id recovers the same item id dispatch_once dispatched."""
    proj = await db_module.create_project(db, "wt-dispatch")
    item_id = "9c9c9c9c-0000-4444-8888-111122223333"

    async def fake_get_groups(_db, _project_id, _version):
        return {"groups": [[{"id": item_id, "title": "Telemetry target", "resources": []}]]}

    class _RealEnqueueSpy:
        def __init__(self):
            self.calls = []

        async def __call__(self, db_, session_id, project_id, prompt, **kwargs):
            self.calls.append(prompt)
            return await enqueue_module.enqueue_claude_task(
                db_, session_id, project_id, prompt, worker_argv=_OK_WORKER, wait=True,
            )

    async def fake_evaluate_blockers(*_a, **_k):
        return {"run_stop": False, "quarantined_item_ids": []}

    spy = _RealEnqueueSpy()
    disp = dispatcher_module.Dispatcher(
        db, proj["id"], enqueue_fn=spy, get_groups_fn=fake_get_groups,
        evaluate_blockers_fn=fake_evaluate_blockers,
    )
    enqueued = await disp.dispatch_once()
    assert len(enqueued) == 1
    task = await db_module.get_task(db, enqueued[0]["id"])
    assert wt.extract_item_id(task["description"]) == item_id


# ---------------------------------------------------------------------------
# observe_task — pure function tests
# ---------------------------------------------------------------------------


def _task_row(**overrides):
    row = {
        "id": "task-1",
        "project_id": "proj-1",
        "session_id": "sess-1",
        "status": "pending",
        "description": f"{enqueue_module.PROMPT_PREFIX}Work ONLY on sprint item item-42: Do a thing\n",
        "created_at": "2026-08-09 12:00:00",
        "worker_pid": None,
    }
    row.update(overrides)
    return row


def test_observe_task_requires_id():
    with pytest.raises(ValueError):
        wt.observe_task({"status": "pending"})


def test_observe_task_captures_fields():
    record = wt.observe_task(_task_row(), now=100.0)
    assert record.task_id == "task-1"
    assert record.project_id == "proj-1"
    assert record.session_id == "sess-1"
    assert record.item_id == "item-42"
    assert record.status is wt.TaskStatus.PENDING
    assert record.outcome is wt.WorkerOutcome.QUEUED
    assert record.first_observed_at == {"pending": 100.0}


def test_observe_task_first_observed_is_sticky():
    """A status seen twice keeps its FIRST observed timestamp."""
    record = wt.observe_task(_task_row(status="pending"), now=100.0)
    record = wt.observe_task(_task_row(status="pending"), existing=record, now=200.0)
    assert record.first_observed_at["pending"] == 100.0


def test_observe_task_transition_sequence_and_latency():
    record = wt.observe_task(_task_row(status="pending"), now=100.0)
    record = wt.observe_task(_task_row(status="in_progress", worker_pid=4242), existing=record, now=105.0)
    record = wt.observe_task(_task_row(status="done"), existing=record, now=130.0)

    assert record.status is wt.TaskStatus.DONE
    assert record.outcome is wt.WorkerOutcome.SUCCEEDED
    assert record.worker_pid == 4242
    assert record.queue_wait_seconds() == pytest.approx(5.0)
    assert record.observed_latency_seconds() == pytest.approx(30.0)


def test_observe_task_failed_sets_failure_class():
    row = _task_row(status="failed", description=f"{enqueue_module.ERROR_PREFIX}p\n\nexit code 1\nboom")
    record = wt.observe_task(row, now=100.0)
    assert record.outcome is wt.WorkerOutcome.FAILED
    assert record.failure_class is wt.FailureClass.NONZERO_EXIT


def test_observe_task_unknown_status_is_safe():
    record = wt.observe_task(_task_row(status="backlog"), now=1.0)
    assert record.status is wt.TaskStatus.UNKNOWN
    assert record.outcome is wt.WorkerOutcome.UNKNOWN


def test_observe_task_worker_pid_never_cleared_by_later_none():
    record = wt.observe_task(_task_row(status="in_progress", worker_pid=99), now=1.0)
    record = wt.observe_task(_task_row(status="in_progress", worker_pid=None), existing=record, now=2.0)
    assert record.worker_pid == 99


# ---------------------------------------------------------------------------
# WorkerTelemetryLedger — observe / attach_evidence / summarize
# ---------------------------------------------------------------------------


def test_ledger_observe_and_get():
    ledger = wt.WorkerTelemetryLedger()
    ledger.observe(_task_row(status="pending"))
    record = ledger.get("task-1")
    assert record is not None
    assert record.status is wt.TaskStatus.PENDING


def test_ledger_list_records_filters_by_project():
    ledger = wt.WorkerTelemetryLedger()
    ledger.observe(_task_row(id="t1", project_id="proj-a"))
    ledger.observe(_task_row(id="t2", project_id="proj-b"))
    only_a = ledger.list_records(project_id="proj-a")
    assert [r.task_id for r in only_a] == ["t1"]


def test_ledger_attach_evidence_requires_prior_observe():
    ledger = wt.WorkerTelemetryLedger()
    with pytest.raises(KeyError):
        ledger.attach_evidence("nope", sprint_item={"claimed_at": "x"})


def test_ledger_attach_evidence_sprint_item():
    ledger = wt.WorkerTelemetryLedger()
    ledger.observe(_task_row())
    record = ledger.attach_evidence(
        "task-1",
        sprint_item={"claimed_at": "2026-08-09T12:00:00", "completed_at": "2026-08-09T12:30:00"},
    )
    assert record.phase_evidence.implementation_started_at == "2026-08-09T12:00:00"
    assert record.phase_evidence.implementation_ended_at == "2026-08-09T12:30:00"
    # No executor_report supplied — integration falls back to sprint_item's completed_at.
    assert record.phase_evidence.integration_completed_at == "2026-08-09T12:30:00"


def test_ledger_attach_evidence_verification_runs():
    ledger = wt.WorkerTelemetryLedger()
    ledger.observe(_task_row())
    runs = [
        {"started_at": "2026-08-09T12:05:00", "ended_at": "2026-08-09T12:06:00", "status": "ok", "exit_code": 0},
        {"started_at": "2026-08-09T12:10:00", "ended_at": "2026-08-09T12:11:00", "status": "ok", "exit_code": 0},
    ]
    record = ledger.attach_evidence("task-1", verification_runs=runs)
    ev = record.phase_evidence
    assert ev.verification_started_at == "2026-08-09T12:05:00"
    assert ev.verification_ended_at == "2026-08-09T12:11:00"
    assert ev.verification_run_count == 2
    assert ev.verification_exit_code == 0
    assert ev.verification_passed is True


def test_ledger_attach_evidence_verification_failed_run():
    ledger = wt.WorkerTelemetryLedger()
    ledger.observe(_task_row())
    runs = [{"started_at": "t0", "ended_at": "t1", "status": "ok", "exit_code": 1}]
    record = ledger.attach_evidence("task-1", verification_runs=runs)
    assert record.phase_evidence.verification_passed is False


def test_ledger_attach_evidence_executor_report():
    ledger = wt.WorkerTelemetryLedger()
    ledger.observe(_task_row())
    report = {
        "tests": {"cmd": "pytest", "exit_code": 0},
        "artifact_evidence": None,
        "created_at": "2026-08-09T12:40:00",
        "item_outcomes": [{"item_id": "item-42", "status": "done"}],
        "commits": ["abc123"],
        "accepted_at": "2026-08-09T12:45:00",
    }
    record = ledger.attach_evidence("task-1", executor_report=report)
    ev = record.phase_evidence
    assert ev.evidence_present is True
    assert ev.evidence_recorded_at == "2026-08-09T12:40:00"
    assert ev.bookkeeping_event_count == 2
    assert ev.integration_completed_at == "2026-08-09T12:45:00"


def test_ledger_attach_evidence_executor_report_overrides_sprint_item_integration():
    ledger = wt.WorkerTelemetryLedger()
    ledger.observe(_task_row())
    ledger.attach_evidence("task-1", sprint_item={"completed_at": "2026-08-09T12:30:00"})
    record = ledger.attach_evidence(
        "task-1", executor_report={"accepted_at": "2026-08-09T12:50:00"},
    )
    assert record.phase_evidence.integration_completed_at == "2026-08-09T12:50:00"


def test_ledger_summarize_counts_and_latency():
    clock = {"t": 0.0}

    def fake_clock():
        return clock["t"]

    ledger = wt.WorkerTelemetryLedger(clock=fake_clock)

    clock["t"] = 0.0
    ledger.observe(_task_row(id="ok-1", status="pending"))
    clock["t"] = 1.0
    ledger.observe(_task_row(id="ok-1", status="in_progress"))
    clock["t"] = 11.0
    ledger.observe(_task_row(id="ok-1", status="done"))

    clock["t"] = 0.0
    ledger.observe(_task_row(id="bad-1", status="pending"))
    clock["t"] = 1.0
    ledger.observe(_task_row(
        id="bad-1", status="failed",
        description=f"{enqueue_module.ERROR_PREFIX}p\n\nexit code 1\nboom",
    ))

    summary = ledger.summarize()
    assert summary["total_records"] == 2
    assert summary["by_outcome"] == {"succeeded": 1, "failed": 1}
    assert summary["by_failure_class"] == {"nonzero_exit": 1}
    lat = summary["latency_seconds"]
    assert lat["count"] == 2
    assert lat["min"] == pytest.approx(1.0)
    assert lat["max"] == pytest.approx(11.0)


def test_ledger_summarize_empty_ledger():
    ledger = wt.WorkerTelemetryLedger()
    summary = ledger.summarize()
    assert summary["total_records"] == 0
    assert summary["latency_seconds"] is None


# ---------------------------------------------------------------------------
# Persistence round-trip + eviction
# ---------------------------------------------------------------------------


def test_ledger_persists_and_reloads(tmp_path):
    path = tmp_path / "ledger.json"
    ledger = wt.WorkerTelemetryLedger(persist_path=path)
    ledger.observe(_task_row(status="pending"))
    ledger.observe(_task_row(status="in_progress"))
    ledger.attach_evidence("task-1", sprint_item={"claimed_at": "x", "completed_at": "y"})

    assert path.exists()

    reloaded = wt.WorkerTelemetryLedger(persist_path=path)
    record = reloaded.get("task-1")
    assert record is not None
    assert record.status is wt.TaskStatus.IN_PROGRESS
    assert record.phase_evidence.implementation_started_at == "x"


def test_ledger_handles_corrupt_persisted_file(tmp_path):
    path = tmp_path / "ledger.json"
    path.write_text("{not valid json", encoding="utf-8")
    ledger = wt.WorkerTelemetryLedger(persist_path=path)
    assert ledger.list_records() == []


def test_ledger_handles_missing_persisted_file(tmp_path):
    path = tmp_path / "nested" / "ledger.json"
    ledger = wt.WorkerTelemetryLedger(persist_path=path)
    assert ledger.list_records() == []


def test_ledger_evicts_oldest_terminal_records_only():
    ledger = wt.WorkerTelemetryLedger(max_records=2)
    ledger.observe(_task_row(id="t1", created_at="2026-08-09T12:00:00", status="done"))
    ledger.observe(_task_row(id="t2", created_at="2026-08-09T12:01:00", status="done"))
    # t3 pushes the ledger over max_records=2; t1 (oldest terminal) is evicted.
    ledger.observe(_task_row(id="t3", created_at="2026-08-09T12:02:00", status="done"))
    ids = {r.task_id for r in ledger.list_records()}
    assert ids == {"t2", "t3"}


def test_ledger_never_evicts_in_flight_records():
    ledger = wt.WorkerTelemetryLedger(max_records=1)
    ledger.observe(_task_row(id="active", created_at="2026-08-09T12:00:00", status="in_progress"))
    ledger.observe(_task_row(id="done-1", created_at="2026-08-09T12:01:00", status="done"))
    ledger.observe(_task_row(id="done-2", created_at="2026-08-09T12:02:00", status="done"))
    ids = {r.task_id for r in ledger.list_records()}
    assert "active" in ids  # never evicted despite being over max_records


# ---------------------------------------------------------------------------
# default_ledger_path / get_ledger / reset_default_ledger
# ---------------------------------------------------------------------------


def test_default_ledger_path_uses_env_override(monkeypatch, tmp_path):
    override = tmp_path / "custom_ledger.json"
    monkeypatch.setenv("MERIDIAN_WORKER_TELEMETRY_PATH", str(override))
    assert wt.default_ledger_path() == override


def test_default_ledger_path_falls_back_to_home(monkeypatch):
    monkeypatch.delenv("MERIDIAN_WORKER_TELEMETRY_PATH", raising=False)
    path = wt.default_ledger_path()
    assert path == Path.home() / ".meridian" / "worker_telemetry.json"


def test_get_ledger_is_a_lazy_singleton(monkeypatch, tmp_path):
    monkeypatch.setenv("MERIDIAN_WORKER_TELEMETRY_PATH", str(tmp_path / "singleton.json"))
    wt.reset_default_ledger()
    first = wt.get_ledger()
    second = wt.get_ledger()
    assert first is second
    wt.reset_default_ledger()
    third = wt.get_ledger()
    assert third is not first
    wt.reset_default_ledger()


# ---------------------------------------------------------------------------
# "prove no worker can edit or claim source accidentally"
# ---------------------------------------------------------------------------

#: Names that must never appear as a called/imported symbol anywhere in
#: worker_telemetry.py — any of these would mean this "observability
#: adapter" had grown the ability to claim sprint-item work, mutate source,
#: or spawn a worker process itself, none of which is this module's job.
_FORBIDDEN_NAMES = frozenset({
    "claim_sprint_item",
    "complete_sprint_item",
    "update_sprint_item",
    "replace_symbol_body",
    "insert_after_symbol",
    "insert_before_symbol",
    "safe_delete_symbol",
    "rename_symbol",
    "create_subprocess_exec",
    "create_subprocess_shell",
    "Popen",
    "system",  # os.system
})
_FORBIDDEN_MODULES = frozenset({"subprocess"})


def _worker_telemetry_source() -> str:
    return inspect.getsource(wt)


def _worker_telemetry_ast() -> ast.Module:
    return ast.parse(_worker_telemetry_source(), filename=wt.__file__)


def test_module_source_has_no_forbidden_imports():
    tree = _worker_telemetry_ast()
    imported_modules: set[str] = set()
    imported_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_modules.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported_modules.add(node.module.split(".")[0])
            for alias in node.names:
                imported_names.add(alias.name)
    assert not (imported_modules & _FORBIDDEN_MODULES), (
        f"worker_telemetry.py must never import a process-spawning module; "
        f"found: {imported_modules & _FORBIDDEN_MODULES}"
    )
    assert not (imported_names & _FORBIDDEN_NAMES), (
        f"worker_telemetry.py must never import a claim/edit-capable symbol; "
        f"found: {imported_names & _FORBIDDEN_NAMES}"
    )


def test_module_source_has_no_forbidden_calls():
    tree = _worker_telemetry_ast()
    called_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                called_names.add(func.id)
            elif isinstance(func, ast.Attribute):
                called_names.add(func.attr)
    offending = called_names & _FORBIDDEN_NAMES
    assert not offending, f"worker_telemetry.py must never call: {offending}"


def test_module_only_imports_enqueue_prefix_constants_from_worker_routing():
    """Confirms this module reads meridian.enqueue's string constants only
    (PROMPT_PREFIX/RESULT_PREFIX/ERROR_PREFIX) and never imports
    meridian.dispatcher or meridian.db at module level — the adapter
    observes dispatcher/enqueue OUTPUT (already-fetched task dicts), it
    never imports the dispatch machinery or any DB write surface itself."""
    assert wt.PROMPT_PREFIX == enqueue_module.PROMPT_PREFIX
    assert wt.RESULT_PREFIX == enqueue_module.RESULT_PREFIX
    assert wt.ERROR_PREFIX == enqueue_module.ERROR_PREFIX

    tree = _worker_telemetry_ast()
    # Relative imports (`from . import X` / `from .X import Y`) are the only
    # way this module could reach into the rest of the meridian package.
    relative_imports = [n for n in tree.body if isinstance(n, ast.ImportFrom) and n.level >= 1]
    imported_symbols = {
        (n.module or alias.name) for n in relative_imports for alias in n.names
    }
    # The only intra-package (meridian.*) import at module level is
    # `from . import enqueue as enqueue_module` — never dispatcher, never db.
    assert imported_symbols == {"enqueue"}


def test_dynamic_only_ledger_file_touches_disk(tmp_path):
    """Exercise the full public API against a scratch directory that also
    contains a fake 'source file', then assert the ONLY file this module
    ever wrote anywhere under tmp_path is its own designated ledger path —
    nothing resembling source was created, modified, or deleted."""
    source_sentinel = tmp_path / "meridian" / "not_really_source.py"
    source_sentinel.parent.mkdir(parents=True)
    source_sentinel.write_text("# untouched sentinel\n", encoding="utf-8")
    sentinel_mtime_before = source_sentinel.stat().st_mtime_ns
    sentinel_contents_before = source_sentinel.read_text(encoding="utf-8")

    ledger_path = tmp_path / "telemetry" / "ledger.json"
    ledger = wt.WorkerTelemetryLedger(persist_path=ledger_path)

    record = ledger.observe(_task_row(status="pending"))
    ledger.observe(_task_row(status="in_progress"))
    ledger.observe(_task_row(status="done"))
    ledger.attach_evidence(
        "task-1",
        sprint_item={"claimed_at": "a", "completed_at": "b"},
        verification_runs=[{"started_at": "c", "ended_at": "d", "status": "ok", "exit_code": 0}],
        executor_report={"tests": {"exit_code": 0}, "created_at": "e", "accepted_at": "f"},
    )
    ledger.summarize()
    assert record.task_id == "task-1"

    # The source sentinel must be byte-for-byte and mtime-for-mtime untouched.
    assert source_sentinel.stat().st_mtime_ns == sentinel_mtime_before
    assert source_sentinel.read_text(encoding="utf-8") == sentinel_contents_before

    all_files = sorted(p for p in tmp_path.rglob("*") if p.is_file())
    assert all_files == sorted([source_sentinel, ledger_path])
