"""28da27fd — worker verification, bookkeeping, and latency telemetry adapter.

WHY THIS IS A SEPARATE ADAPTER, NOT A CHANGE TO THE ROUTING IMPLEMENTATION
---------------------------------------------------------------------------
The actual worker-dispatch surface already exists and is untouched by this
module: :func:`meridian.dispatcher._worker_prompt` builds the prompt handed
to a worker, and :func:`meridian.enqueue._run_worker` spawns the subprocess
and writes the outcome onto a ``task_log`` row (``pending`` ->
``in_progress`` -> ``done``/``failed``, with the result/error text prefixed
by ``RESULT_PREFIX``/``ERROR_PREFIX``). This module never re-implements any
of that: it only *observes* task rows that surface already produces, and
*classifies*/*times* what it sees.

The reason a separate adapter is needed at all — rather than, say, adding
columns to ``task_log`` — is that ``task_log`` has exactly ONE timestamp
column (``created_at``, stamped once at INSERT). There is no per-transition
timestamp: the row does not remember *when* it moved from ``pending`` to
``in_progress``, or from ``in_progress`` to ``done``/``failed`` (see
``meridian/db/__init__.py``'s ``task_log`` DDL and ``update_task``, which
updates ``status``/``description`` only). Latency telemetry is therefore
necessarily an EXTERNAL, OBSERVATION-BASED concern: something has to look at
a task row repeatedly (a poller, a dispatcher hook, a dashboard refresh) and
record wall-clock time itself, the first moment it sees each status. That is
exactly what :class:`WorkerTelemetryLedger` does, in the same
"lightweight, dependency-free, local-machine JSON ledger" shape already
established by :mod:`meridian.process_registry` for worker-lease
bookkeeping (atomic tmp-file-then-``os.replace`` writes, an env-var
override for the persisted path, a lazy process-wide default instance) —
this module deliberately reuses that shape rather than inventing a third
persistence pattern.

FAILURE CLASSIFICATION IS GROUNDED IN THE REAL MESSAGE TEXT
---------------------------------------------------------------------------
:func:`classify_failure` recognizes the literal text fragments
``meridian.enqueue._run_worker`` ALREADY writes into a failed task's
description (``"worker command not found:"``, ``"failed to spawn worker:"``,
``"worker timed out after"``, ``"exit code "``). These are fingerprints of
text the existing code already produces, not a re-implementation of its
control flow — ``tests/test_28da27fd_worker_telemetry.py`` cross-checks the
classifier against REAL ``_run_worker`` output (spawn-missing, non-zero
exit, and timeout cases, run through the real subprocess machinery against a
real test database) so any future drift in ``enqueue.py``'s message format
is caught here rather than silently misclassified.

DISTINGUISHING implementation / verification / evidence / bookkeeping /
integration WORK
---------------------------------------------------------------------------
A dispatched worker is one opaque ``claude -p`` subprocess from this
module's vantage point — there is no visibility into what it does
internally. What CAN be correlated, honestly, is:

* **implementation** — a sprint item's own ``claimed_at``/``completed_at``
  (via ``get_sprint_items``/``get_sprint_item``), if the caller supplies the
  sprint-item row.
* **verification** — ``verification_runs`` rows
  (``meridian/db/verification_runs.py``, written by ``run_verification``),
  if the caller supplies them.
* **evidence / bookkeeping / integration** — an ``executor_reports`` row
  (``meridian/db/executor_reports.py``), whose ``tests``/``artifact_evidence``
  fields are evidence, whose ``item_outcomes``/``commits`` counts are
  bookkeeping volume, and whose ``accepted_at`` is the integration
  (planner-promotion) timestamp, if the caller supplies it.

:meth:`WorkerTelemetryLedger.attach_evidence` accepts any subset of these
(all optional) and folds them onto the ledger's own poll-observed timing for
the same ``task_id``. Every field this module cannot actually observe stays
``None``/empty rather than being fabricated — "persist enough evidence to
distinguish this work" is honored by giving each category its own named,
independently-nullable field, not by guessing a boundary this adapter never
witnessed.

INTEGRATION POINT FOR A HUMAN/ORCHESTRATOR TO WIRE UP
---------------------------------------------------------------------------
This module is intentionally NOT wired into ``meridian/dispatcher.py`` or
``meridian/enqueue.py`` — doing so was explicitly out of scope (see sprint
item 28da27fd's notes: "as a separate adapter... do not duplicate the
worker-routing implementation"), and both files are shared/high-contention
files this item does not own. The natural follow-up wiring, left for a
human or a future sprint item, mirrors the existing PID-watchdog /
lease-sweep pattern already present in this codebase:

1. A periodic task (same shape as ``Dispatcher``'s optional
   ``lease_sweep_fn`` hook, or the auto-summary loop's PID watchdog) calls
   ``db_module.get_tasks(db, project_id)`` and feeds each row through
   ``get_ledger().observe(task)``.
2. When a task's ``item_id`` (recovered via :func:`extract_item_id`) is
   known, the same loop can optionally fetch the correlated sprint item /
   verification runs / executor report and call
   ``get_ledger().attach_evidence(task_id, ...)``.
3. ``get_ledger().summarize(project_id=...)`` then gives a dashboard or a
   ``generate_handoff`` call a durable, queryable latency/failure rollup
   without either of the two routing modules ever importing this one.

SAFETY: THIS MODULE CANNOT CLAIM A SPRINT ITEM OR EDIT SOURCE
---------------------------------------------------------------------------
Every public entry point here is either a pure function (``classify_failure``,
``extract_item_id``, ``observe_task``, ``outcome_for_status``) or a method
on :class:`WorkerTelemetryLedger` whose only side effect is reading/writing
its OWN designated JSON ledger file (never a source path, never a path the
caller can redirect into the repository — see :func:`default_ledger_path`).
This module imports nothing capable of mutating sprint items, task rows, or
any file other than its own ledger:

* No ``claim_sprint_item`` / ``complete_sprint_item`` / any ``db_module``
  write function.
* No ``subprocess`` / ``asyncio.create_subprocess_exec`` — it cannot spawn a
  worker, only observe one that something else already spawned.
* No Serena/Write/Edit-style source-mutation import.

``tests/test_28da27fd_worker_telemetry.py`` proves this two ways: a static
AST scan of this module's own source rejecting any of the forbidden
names/imports above, and a dynamic test that exercises the full public API
against a scratch directory and asserts the ONLY file that appears anywhere
under it is this module's own ledger JSON file.
"""
from __future__ import annotations

import dataclasses
import json
import os
import re
import statistics
import tempfile
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from . import enqueue as enqueue_module

# ---------------------------------------------------------------------------
# Constants mirrored (never re-defined) from meridian.enqueue, so this module
# can never silently drift from the actual prefixes _run_worker writes.
# ---------------------------------------------------------------------------

PROMPT_PREFIX = enqueue_module.PROMPT_PREFIX
RESULT_PREFIX = enqueue_module.RESULT_PREFIX
ERROR_PREFIX = enqueue_module.ERROR_PREFIX

# Literal message fragments meridian.enqueue._run_worker ALREADY writes into
# a failed task's description (see enqueue.py lines building each branch's
# `description=` argument to db_module.update_task). Recognizing this text
# is not a reimplementation of _run_worker's control flow — it is reading
# the outcome that code already committed to the task row.
_SPAWN_NOT_FOUND_MARKER = "worker command not found:"
_SPAWN_ERROR_MARKER = "failed to spawn worker:"
_TIMEOUT_MARKER = "worker timed out after"
_NONZERO_EXIT_MARKER = "exit code "

# meridian.dispatcher._worker_prompt's exact item-id line:
#   f"Work ONLY on sprint item {item_id}: {title}\n"
# Anchored to that literal phrase so a prompt built by the real
# _worker_prompt (persisted verbatim into the task description via
# enqueue.enqueue_claude_task's f"{PROMPT_PREFIX}{prompt}") can have its
# item id recovered after the fact.
_ITEM_ID_RE = re.compile(r"Work ONLY on sprint item ([^\s:]+):")


class TaskStatus(str, Enum):
    """The subset of ``task_log.status`` values relevant to a dispatched
    worker task. ``task_log`` also allows ``pending-hitl``/``backlog``/
    ``future``/``backburner`` (see ``meridian/db/__init__.py``'s CHECK
    constraint) but ``enqueue._run_worker`` never writes those — they fall
    back to :attr:`UNKNOWN` here rather than raising, since an unrelated
    (non-worker) task_log row is a legitimate thing this adapter might be
    handed.
    """

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    FAILED = "failed"
    UNKNOWN = "unknown"

    @classmethod
    def coerce(cls, value: Any) -> "TaskStatus":
        try:
            return cls(value)
        except ValueError:
            return cls.UNKNOWN


class WorkerOutcome(str, Enum):
    """High-level bucket a task's current status maps to, for aggregation."""

    QUEUED = "queued"
    IN_FLIGHT = "in_flight"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"


class FailureClass(str, Enum):
    """Why a terminal ``failed`` task actually failed, per
    :func:`classify_failure`. :attr:`NONE` is the value for any
    non-``failed`` task (there is nothing to classify yet)."""

    NONE = "none"
    SPAWN_NOT_FOUND = "spawn_not_found"
    SPAWN_ERROR = "spawn_error"
    TIMEOUT = "timeout"
    NONZERO_EXIT = "nonzero_exit"
    UNCLASSIFIED = "unclassified_failure"


_STATUS_TO_OUTCOME: dict[TaskStatus, WorkerOutcome] = {
    TaskStatus.PENDING: WorkerOutcome.QUEUED,
    TaskStatus.IN_PROGRESS: WorkerOutcome.IN_FLIGHT,
    TaskStatus.DONE: WorkerOutcome.SUCCEEDED,
    TaskStatus.FAILED: WorkerOutcome.FAILED,
    TaskStatus.UNKNOWN: WorkerOutcome.UNKNOWN,
}


def outcome_for_status(status: Any) -> WorkerOutcome:
    """Map a raw (or already-coerced) task status to its telemetry bucket."""
    return _STATUS_TO_OUTCOME[TaskStatus.coerce(status)]


def classify_failure(description: "str | None") -> FailureClass:
    """Pure text classifier over an already-terminal task's description.

    Only meaningful for a task whose status is ``failed`` — callers pass the
    task's ``description`` column verbatim. Checked in the same order
    ``_run_worker`` can produce them (spawn errors happen before the process
    ever runs, so they are checked first); :attr:`FailureClass.UNCLASSIFIED`
    is returned for a failed task whose description matches none of the
    known patterns (e.g. hand-written or produced by something other than
    ``_run_worker``) rather than raising or guessing.
    """
    text = description or ""
    if _SPAWN_NOT_FOUND_MARKER in text:
        return FailureClass.SPAWN_NOT_FOUND
    if _SPAWN_ERROR_MARKER in text:
        return FailureClass.SPAWN_ERROR
    if _TIMEOUT_MARKER in text:
        return FailureClass.TIMEOUT
    if _NONZERO_EXIT_MARKER in text:
        return FailureClass.NONZERO_EXIT
    return FailureClass.UNCLASSIFIED


def extract_item_id(description: "str | None") -> "str | None":
    """Recover the sprint-item id a dispatcher-built prompt embeds, from an
    already-persisted task description (``PROMPT_PREFIX`` + the prompt
    ``dispatcher._worker_prompt`` produced). Returns ``None`` when the text
    doesn't match — e.g. a task not created via the dispatcher at all."""
    if not description:
        return None
    match = _ITEM_ID_RE.search(description)
    return match.group(1) if match else None


@dataclass
class PhaseEvidence:
    """Optional, richer per-phase evidence a caller may attach once it has
    correlated sources beyond the bare task row. Every field stays
    ``None``/empty/zero until explicitly attached via
    :meth:`WorkerTelemetryLedger.attach_evidence` — this module never
    fetches these itself (it has no DB import beyond the three read-only
    string constants mirrored from ``meridian.enqueue`` above), so
    "distinguishing implementation/verification/evidence/bookkeeping/
    integration work" degrades honestly to "not yet correlated" rather than
    fabricating a phase boundary nothing here actually observed.
    """

    implementation_started_at: "str | None" = None  # sprint_items.claimed_at
    implementation_ended_at: "str | None" = None  # sprint_items.completed_at
    verification_started_at: "str | None" = None  # min(verification_runs.started_at)
    verification_ended_at: "str | None" = None  # max(verification_runs.ended_at)
    verification_exit_code: "int | None" = None
    verification_passed: "bool | None" = None
    verification_run_count: int = 0
    evidence_present: bool = False  # executor_report.tests / artifact_evidence non-empty
    evidence_recorded_at: "str | None" = None  # executor_report.created_at
    bookkeeping_event_count: int = 0  # len(item_outcomes) + len(commits), or similar
    integration_completed_at: "str | None" = None  # executor_report.accepted_at (preferred) or sprint_items.completed_at

    def to_dict(self) -> "dict[str, Any]":
        return dataclasses.asdict(self)


_PHASE_EVIDENCE_FIELDS: frozenset[str] = frozenset(f.name for f in dataclasses.fields(PhaseEvidence))


@dataclass
class WorkerRunTelemetry:
    """One dispatched worker task's observed timing, status, and (optional)
    phase evidence. Built/refreshed by :func:`observe_task`; never
    constructed by reaching into the database directly."""

    task_id: str
    project_id: "str | None" = None
    session_id: "str | None" = None
    item_id: "str | None" = None
    worker_pid: "int | None" = None
    status: TaskStatus = TaskStatus.UNKNOWN
    outcome: WorkerOutcome = WorkerOutcome.UNKNOWN
    failure_class: FailureClass = FailureClass.NONE
    # task_log.created_at — the ONE timestamp column the underlying row
    # actually carries (queue time). See module docstring for why every
    # OTHER timestamp below is necessarily poll-observed, not read from a
    # column that doesn't exist.
    created_at: "str | None" = None
    # status.value -> wall-clock seconds (time.time()) this ledger FIRST
    # observed that status for this task. Poll-granularity only.
    first_observed_at: "dict[str, float]" = field(default_factory=dict)
    phase_evidence: PhaseEvidence = field(default_factory=PhaseEvidence)

    def to_dict(self) -> "dict[str, Any]":
        return {
            "task_id": self.task_id,
            "project_id": self.project_id,
            "session_id": self.session_id,
            "item_id": self.item_id,
            "worker_pid": self.worker_pid,
            "status": self.status.value,
            "outcome": self.outcome.value,
            "failure_class": self.failure_class.value,
            "created_at": self.created_at,
            "first_observed_at": dict(self.first_observed_at),
            "phase_evidence": self.phase_evidence.to_dict(),
        }

    def observed_latency_seconds(self) -> "float | None":
        """Wall-clock seconds between this ledger's first observation of a
        queued/in-flight status and its first observation of a terminal
        (done/failed) status. ``None`` until both ends have been observed —
        poll-granularity only (see module docstring)."""
        start = self.first_observed_at.get(TaskStatus.PENDING.value)
        if start is None:
            start = self.first_observed_at.get(TaskStatus.IN_PROGRESS.value)
        end = self.first_observed_at.get(TaskStatus.DONE.value)
        if end is None:
            end = self.first_observed_at.get(TaskStatus.FAILED.value)
        if start is None or end is None:
            return None
        return max(0.0, end - start)

    def queue_wait_seconds(self) -> "float | None":
        """Wall-clock seconds between first-observed ``pending`` and
        first-observed ``in_progress``. ``None`` until both are observed."""
        pending = self.first_observed_at.get(TaskStatus.PENDING.value)
        in_progress = self.first_observed_at.get(TaskStatus.IN_PROGRESS.value)
        if pending is None or in_progress is None:
            return None
        return max(0.0, in_progress - pending)


def observe_task(
    task: "dict[str, Any]",
    *,
    existing: "WorkerRunTelemetry | None" = None,
    now: "float | None" = None,
) -> WorkerRunTelemetry:
    """Pure function: derive/refresh a :class:`WorkerRunTelemetry` from one
    already-fetched ``task_log`` row (as returned by ``db.get_task`` /
    ``db.get_tasks``). Never touches the database or filesystem — see
    :class:`WorkerTelemetryLedger` for the persisted bookkeeping layer built
    on top of this.

    Passing the PRIOR record back in via ``existing`` (as
    :class:`WorkerTelemetryLedger` does) is what makes ``first_observed_at``
    cumulative across repeated calls: a status already recorded once is
    never overwritten with a later timestamp.
    """
    task_id = task.get("id")
    if not task_id:
        raise ValueError("task dict is missing an 'id' — cannot build telemetry without one")
    clock = time.time() if now is None else now
    status = TaskStatus.coerce(task.get("status"))
    description = task.get("description") or ""

    record = existing if existing is not None else WorkerRunTelemetry(task_id=str(task_id))
    record.project_id = task.get("project_id") or record.project_id
    record.session_id = task.get("session_id") or record.session_id
    worker_pid = task.get("worker_pid")
    if worker_pid is not None:
        record.worker_pid = worker_pid
    record.created_at = task.get("created_at") or record.created_at
    item_id = extract_item_id(description)
    if item_id:
        record.item_id = item_id
    record.status = status
    record.outcome = outcome_for_status(status)
    record.failure_class = classify_failure(description) if status is TaskStatus.FAILED else FailureClass.NONE
    record.first_observed_at.setdefault(status.value, clock)
    return record


def _percentile(sorted_values: "list[float]", pct: float) -> float:
    """Nearest-rank percentile over an already-sorted, non-empty list.
    ``pct`` in ``[0, 1]``. Kept dependency-free (no numpy) — the ledger's
    latency samples are never large enough to need anything fancier."""
    if not sorted_values:
        raise ValueError("cannot compute a percentile of an empty sequence")
    if len(sorted_values) == 1:
        return sorted_values[0]
    idx = round(pct * (len(sorted_values) - 1))
    idx = max(0, min(len(sorted_values) - 1, idx))
    return sorted_values[idx]


# ---------------------------------------------------------------------------
# Persisted bookkeeping ledger — mirrors meridian.process_registry's
# "lightweight, dependency-free, local-machine JSON file" shape.
# ---------------------------------------------------------------------------

_LEDGER_PATH_ENV_VAR = "MERIDIAN_WORKER_TELEMETRY_PATH"

#: Bound on how many TERMINAL (done/failed) records the ledger retains once
#: persisted — a long-lived dispatcher must not grow this file unboundedly.
#: A still-queued/in-flight record is NEVER evicted regardless of this cap.
DEFAULT_MAX_RECORDS = 2000


def default_ledger_path() -> Path:
    """Where the persisted ledger lives — ``~/.meridian/worker_telemetry.json``
    unless overridden by ``MERIDIAN_WORKER_TELEMETRY_PATH`` (same override
    pattern as ``process_registry.default_registry_path``). Always outside
    any git-tracked source tree — this is bookkeeping data, never source."""
    override = os.environ.get(_LEDGER_PATH_ENV_VAR, "").strip()
    if override:
        return Path(override)
    return Path.home() / ".meridian" / "worker_telemetry.json"


def _record_from_dict(data: "dict[str, Any]") -> WorkerRunTelemetry:
    evidence_data = data.get("phase_evidence") or {}
    evidence = PhaseEvidence(**{k: v for k, v in evidence_data.items() if k in _PHASE_EVIDENCE_FIELDS})
    first_observed = data.get("first_observed_at") or {}
    return WorkerRunTelemetry(
        task_id=str(data["task_id"]),
        project_id=data.get("project_id"),
        session_id=data.get("session_id"),
        item_id=data.get("item_id"),
        worker_pid=data.get("worker_pid"),
        status=TaskStatus.coerce(data.get("status")),
        outcome=(
            WorkerOutcome(data["outcome"])
            if data.get("outcome") in {o.value for o in WorkerOutcome}
            else WorkerOutcome.UNKNOWN
        ),
        failure_class=(
            FailureClass(data["failure_class"])
            if data.get("failure_class") in {f.value for f in FailureClass}
            else FailureClass.NONE
        ),
        created_at=data.get("created_at"),
        first_observed_at={str(k): float(v) for k, v in first_observed.items()},
        phase_evidence=evidence,
    )


class WorkerTelemetryLedger:
    """In-memory, optionally file-persisted bookkeeping ledger of
    :class:`WorkerRunTelemetry` records, one per dispatched ``task_id``.

    This is READ/RECORD-ONLY with respect to Meridian's own state: it never
    imports or calls ``claim_sprint_item`` / ``complete_sprint_item`` / any
    ``sprint_items``-mutating function, and its only filesystem write is its
    own designated JSON file (never a source path) — see the module
    docstring's "SAFETY" section and
    ``tests/test_28da27fd_worker_telemetry.py``'s static + dynamic proof
    tests.
    """

    def __init__(
        self,
        *,
        persist_path: "Path | None" = None,
        clock: Any = time.time,
        autosave: bool = True,
        max_records: int = DEFAULT_MAX_RECORDS,
    ) -> None:
        self._records: "dict[str, WorkerRunTelemetry]" = {}
        self._clock = clock
        self._persist_path = persist_path
        self._autosave = autosave and persist_path is not None
        self._max_records = max(1, int(max_records))
        if self._persist_path is not None:
            self._load()

    # -- persistence -------------------------------------------------------

    def _load(self) -> None:
        assert self._persist_path is not None
        try:
            raw = self._persist_path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            return
        try:
            data = json.loads(raw) if raw.strip() else {}
        except ValueError:
            return  # corrupt file — start from an empty ledger rather than crash
        for row in data.get("records", []) or []:
            try:
                record = _record_from_dict(row)
            except Exception:  # noqa: BLE001 — one bad row must not break the whole load
                continue
            self._records[record.task_id] = record

    def _save(self) -> None:
        if self._persist_path is None:
            return
        self._persist_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"records": [record.to_dict() for record in self._records.values()]}
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self._persist_path.parent), prefix=".worker_telemetry_", suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            os.replace(tmp_name, self._persist_path)
        finally:
            try:
                if os.path.exists(tmp_name):
                    os.remove(tmp_name)
            except OSError:
                pass

    def _maybe_save(self) -> None:
        if self._autosave:
            self._save()

    def _evict_if_needed(self) -> None:
        """Drop the oldest TERMINAL (done/failed) records once over
        ``max_records``. A queued/in-flight record is never evicted — an
        active worker's telemetry must never silently disappear mid-run."""
        if len(self._records) <= self._max_records:
            return
        terminal = [
            r for r in self._records.values()
            if r.outcome in (WorkerOutcome.SUCCEEDED, WorkerOutcome.FAILED)
        ]
        terminal.sort(key=lambda r: r.created_at or "")
        overflow = len(self._records) - self._max_records
        for record in terminal[:overflow]:
            self._records.pop(record.task_id, None)

    # -- observation ---------------------------------------------------------

    def observe(self, task: "dict[str, Any]") -> WorkerRunTelemetry:
        """Record one snapshot of a task row's status/description/pid. Safe
        (and expected) to call repeatedly for the same ``task_id`` as its
        status advances — see :func:`observe_task`."""
        task_id = str(task.get("id"))
        existing = self._records.get(task_id)
        record = observe_task(task, existing=existing, now=self._clock())
        self._records[record.task_id] = record
        self._evict_if_needed()
        self._maybe_save()
        return record

    def attach_evidence(
        self,
        task_id: str,
        *,
        sprint_item: "dict[str, Any] | None" = None,
        verification_runs: "list[dict[str, Any]] | None" = None,
        executor_report: "dict[str, Any] | None" = None,
    ) -> WorkerRunTelemetry:
        """Fold optional, already-fetched correlated evidence onto a
        previously :meth:`observe`-d task's :class:`PhaseEvidence`. Raises
        ``KeyError`` if ``observe`` was never called for ``task_id`` — this
        method only enriches an existing observation, it never fabricates
        one (there would be no ``status``/timing to attach evidence to)."""
        record = self._records.get(task_id)
        if record is None:
            raise KeyError(f"no observed telemetry for task_id {task_id!r} — call observe() first")
        evidence = record.phase_evidence

        if sprint_item:
            evidence.implementation_started_at = sprint_item.get("claimed_at") or evidence.implementation_started_at
            evidence.implementation_ended_at = sprint_item.get("completed_at") or evidence.implementation_ended_at

        if verification_runs:
            started = [r.get("started_at") for r in verification_runs if r.get("started_at")]
            ended = [r.get("ended_at") for r in verification_runs if r.get("ended_at")]
            if started:
                evidence.verification_started_at = min(started)
            if ended:
                evidence.verification_ended_at = max(ended)
            # Replace, not accumulate: callers pass the full current snapshot
            # of correlated runs (e.g. list_verification_runs' whole result),
            # not a delta — accumulating here would double-count every time
            # the same growing list is re-attached on a later poll.
            evidence.verification_run_count = len(verification_runs)
            latest = verification_runs[-1]
            if latest.get("exit_code") is not None:
                evidence.verification_exit_code = latest.get("exit_code")
            if latest.get("status") is not None:
                evidence.verification_passed = latest.get("status") == "ok" and latest.get("exit_code") == 0

        if executor_report:
            tests = executor_report.get("tests")
            artifact_evidence = executor_report.get("artifact_evidence")
            evidence.evidence_present = bool(tests) or bool(artifact_evidence)
            evidence.evidence_recorded_at = executor_report.get("created_at") or evidence.evidence_recorded_at
            evidence.bookkeeping_event_count = len(executor_report.get("item_outcomes") or []) + len(
                executor_report.get("commits") or []
            )
            accepted_at = executor_report.get("accepted_at")
            if accepted_at:
                evidence.integration_completed_at = accepted_at

        if evidence.integration_completed_at is None and sprint_item:
            evidence.integration_completed_at = sprint_item.get("completed_at")

        self._maybe_save()
        return record

    # -- reads -----------------------------------------------------------

    def get(self, task_id: str) -> "WorkerRunTelemetry | None":
        return self._records.get(task_id)

    def list_records(self, *, project_id: "str | None" = None) -> "list[WorkerRunTelemetry]":
        out = list(self._records.values())
        if project_id is not None:
            out = [r for r in out if r.project_id == project_id]
        return sorted(out, key=lambda r: r.created_at or "")

    def summarize(self, *, project_id: "str | None" = None) -> "dict[str, Any]":
        """Aggregate rollup across observed records: counts by outcome and
        failure class, plus latency percentiles over records with an
        observable end-to-end latency (see
        :meth:`WorkerRunTelemetry.observed_latency_seconds`). Pure read —
        never mutates the ledger or touches disk."""
        records = self.list_records(project_id=project_id)
        latencies = [lat for lat in (r.observed_latency_seconds() for r in records) if lat is not None]
        by_outcome: "dict[str, int]" = {}
        by_failure_class: "dict[str, int]" = {}
        for record in records:
            by_outcome[record.outcome.value] = by_outcome.get(record.outcome.value, 0) + 1
            if record.failure_class is not FailureClass.NONE:
                by_failure_class[record.failure_class.value] = by_failure_class.get(record.failure_class.value, 0) + 1

        summary: "dict[str, Any]" = {
            "total_records": len(records),
            "by_outcome": by_outcome,
            "by_failure_class": by_failure_class,
            "latency_seconds": None,
        }
        if latencies:
            sorted_latencies = sorted(latencies)
            summary["latency_seconds"] = {
                "count": len(sorted_latencies),
                "min": sorted_latencies[0],
                "max": sorted_latencies[-1],
                "mean": statistics.fmean(sorted_latencies),
                "median": statistics.median(sorted_latencies),
                "p95": _percentile(sorted_latencies, 0.95),
            }
        return summary


# ---------------------------------------------------------------------------
# Process-wide default ledger — mirrors process_registry.get_broker() /
# reset_default_broker()'s lazy-singleton pattern.
# ---------------------------------------------------------------------------

_default_ledger: "WorkerTelemetryLedger | None" = None


def get_ledger() -> WorkerTelemetryLedger:
    """Lazily-constructed, process-wide default ledger, persisted to
    :func:`default_ledger_path`."""
    global _default_ledger
    if _default_ledger is None:
        _default_ledger = WorkerTelemetryLedger(persist_path=default_ledger_path())
    return _default_ledger


def reset_default_ledger() -> None:
    """Test seam: drop the cached singleton so the next :func:`get_ledger`
    call re-reads ``MERIDIAN_WORKER_TELEMETRY_PATH`` (or the real home
    directory) from scratch."""
    global _default_ledger
    _default_ledger = None
