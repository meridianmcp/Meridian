"""e24f2daa — fail-closed CONSUMER for scripts.run_tests's durable
``TestRunRecord`` (2cebf4ae, already merged).

2cebf4ae built the PRODUCER side: ``scripts/run_tests.py`` streams a
structured, durable JSON snapshot of a local test run (state machine,
monotonic progress timestamps, bounded stdout/stderr tails, exit
code/signal, timeout classification, and a process-tree cleanup receipt) to
a small per-checkout state file next to its cross-platform run lock. Nothing
in this repository actually READ that file to decide pass/fail before this
module existed — ``complete_sprint_item`` and ``generate_handoff`` had no way
to distinguish "the suite genuinely passed" from "nothing ran, or something
crashed/hung/was cancelled, and we have no real evidence either way".

This module is that reader. Three pieces:

1. :func:`classify_test_run_record` / :func:`classify_subprocess_result` —
   the FAIL-CLOSED classifiers. Both return one of exactly six outcomes:
   ``passed`` / ``failed`` / ``infra_crash`` / ``timeout`` / ``cancelled`` /
   ``missing_or_ambiguous``. ``missing_or_ambiguous`` is the fallback for
   every case that is not unambiguously one of the other five — an absent
   record, a non-terminal state, a corrupt/foreign JSON shape, a "passed"
   state with no real exit code, an empty log with no results line, or a
   missing cleanup receipt. **Nothing ever defaults to ``passed``.**
   :func:`classify_test_run_record` reads the PRODUCER's own durable
   ``TestRunRecord`` (via :mod:`scripts.run_tests`) and independently
   re-verifies the evidence behind a self-reported ``passed`` state rather
   than trusting the label — an empty ``stdout_tail``/``stderr_tail`` with no
   captured results line is downgraded to ``missing_or_ambiguous`` even when
   ``state == "passed"``, exactly the "never let an empty log look like a
   pass" failure mode this item calls out by name.
   :func:`classify_subprocess_result` is the lighter-weight twin for a
   caller (``tool_discovery.run_targeted_tests``) that spawns its own ad hoc
   subprocess rather than going through ``scripts/run_tests.py`` and so has
   no durable record to read — same six-way vocabulary, same
   never-silently-passed discipline, derived straight from
   exit_code/signal/timed_out/captured output instead.

2. :func:`check_active_test_run` / :func:`check_duplicate_test_run` — the
   DUPLICATE-RUN / LEASE check. Reads the SAME cross-platform lock
   (``scripts.run_tests.TestRunLock``) the producer already uses to reject a
   second overlapping ``pixi run test`` invocation, so a caller here (a gate
   about to accept a "the suite passed" claim, or about to suggest starting
   a fresh full-suite run) can tell "a run is already in flight for this
   checkout" apart from "no run has happened yet" — and reject/link instead
   of silently piling a second full gate on top of the first.

3. :func:`get_test_run_evidence` / :func:`resolve_repo_root_for_session` /
   :func:`verify_test_run_receipt_evidence` /
   :func:`record_test_run_receipt_override` — the CONSUMER-FACING surface.
   ``get_test_run_evidence`` is the one-call convenience every wiring point
   below uses: load the current record for a checkout, classify it, and
   attach the duplicate-run probe, all in one dict. ``resolve_repo_root_for_
   session`` mirrors ``sprint_evidence_guard._check_wrong_worktree``'s
   worktree-resolution technique exactly (reuses
   ``db.get_active_worktree_for_session`` +
   ``worktree_cleanup.resolve_worktree_disk_path``) so a parallel-worktree
   session's OWN checkout is read, not the server's main checkout, whenever
   that can be resolved. ``verify_test_run_receipt_evidence`` /
   ``record_test_run_receipt_override`` are the opt-in, fail-closed
   completion gate + its audited override — same ``{"ok", "code", "message"}``
   contract shape as ``sprint_evidence_guard.verify_strict_completion_
   evidence`` and ``code_intel_receipt.verify_code_intel_prospecting``, and
   the override requires a non-empty ``reason`` for the identical
   auditability reason those two modules do.

Import-cycle note: this module is intentionally leaf-level. It imports
``scripts.run_tests`` (a namespace package with no ``__init__.py`` — importable
whenever the repo root is on ``sys.path``, true for the real server process
and the test suite; the import is lazy and guarded everywhere it's used, so a
process that genuinely cannot see ``scripts/`` degrades to
``missing_or_ambiguous`` rather than raising) and, lazily, ``meridian.db`` /
``meridian.worktree_cleanup`` — never ``meridian.handoff`` or
``meridian.server``, so both of those (and ``meridian.tool_discovery``) can
import this module at their own top level with no risk of a cycle.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

#: This file lives at meridian/test_run_receipt.py -- two directories up is
#: the repo root, mirroring meridian/server.py's own independent
#: ``_REPO_ROOT = Path(__file__).parent.parent`` computation. Recomputed
#: locally (not imported from meridian.server) specifically so this module
#: never has to import meridian.server -- see the import-cycle note above.
DEFAULT_REPO_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Classification vocabulary.
# ---------------------------------------------------------------------------

CLASS_PASSED = "passed"
CLASS_FAILED = "failed"
CLASS_INFRA_CRASH = "infra_crash"
CLASS_TIMEOUT = "timeout"
CLASS_CANCELLED = "cancelled"
CLASS_MISSING_OR_AMBIGUOUS = "missing_or_ambiguous"

CLASSIFICATIONS: frozenset[str] = frozenset({
    CLASS_PASSED, CLASS_FAILED, CLASS_INFRA_CRASH,
    CLASS_TIMEOUT, CLASS_CANCELLED, CLASS_MISSING_OR_AMBIGUOUS,
})
#: Every classification except CLASS_PASSED -- "a visible non-success
#: classification" per this item's own acceptance wording.
NON_SUCCESS_CLASSIFICATIONS: frozenset[str] = CLASSIFICATIONS - {CLASS_PASSED}

#: event_type recorded in action_audit_log for an audited override of a
#: blocked test-run-receipt completion gate. Mirrors sprint_evidence_guard's
#: OVERRIDE_EVENT_TYPE / code_intel_receipt's OVERRIDE_EVENT_TYPE naming.
OVERRIDE_EVENT_TYPE = "sprint_item_test_run_receipt_override"

#: Text markers that mean a pytest-xdist WORKER process itself died (or the
#: controller lost track of it) -- an infrastructure crash, never a real
#: assertion failure. Mirrors meridian.handoff._XDIST_WORKER_CRASH_MARKERS
#: exactly but is kept as an independent copy (not imported) so this module
#: never has to import meridian.handoff -- see the module docstring's
#: import-cycle note.
_INFRA_CRASH_TEXT_MARKERS: tuple[str, ...] = (
    "INTERNALERROR>", "MemoryError", "WorkerController",
    "Fatal Python error", "node down", "worker crashed",
)


def _import_run_tests() -> Any:
    """Lazy, guarded import of ``scripts.run_tests``.

    ``scripts/`` has no ``__init__.py`` (an implicit namespace package) and
    is only importable when the repo root is on ``sys.path`` -- true for the
    real server process (launched with ``cwd`` at the repo root, per
    AGENTS.md) and for the test suite (pytest adds the rootdir), but never
    something this module may ASSUME. Every caller of this helper treats an
    ``ImportError``/other exception as "no receipt subsystem available" and
    degrades to ``missing_or_ambiguous`` -- never a crash, never a silent
    pass.
    """
    import scripts.run_tests as rt  # noqa: PLC0415

    return rt


def _g(record: Any, name: str, default: Any = None) -> Any:
    """Attribute-or-key getter: accepts a real ``TestRunRecord`` dataclass
    instance, a plain ``dict`` (e.g. straight off ``json.loads``), or
    ``None``."""
    if record is None:
        return default
    if isinstance(record, dict):
        return record.get(name, default)
    return getattr(record, name, default)


def _is_real_exit_code(value: Any) -> bool:
    """True only for a concrete ``int`` exit status -- ``bool`` is an ``int``
    subclass in Python but is never a genuine process exit code (mirrors
    ``db.verification_runs.complete_verification_run``'s identical guard),
    and ``None`` means "no exit code was ever captured"."""
    return isinstance(value, int) and not isinstance(value, bool)


def _has_bounded_output_evidence(record: Any) -> bool:
    """True when the record carries SOME real evidence pytest actually ran
    and reported something -- a non-empty captured stdout/stderr tail, a
    detected results-summary line, or parsed pass/fail counts. False for a
    genuinely empty log, which must never be read as proof of a pass."""
    stdout = _g(record, "stdout_tail") or ""
    stderr = _g(record, "stderr_tail") or ""
    if (isinstance(stdout, str) and stdout.strip()) or (
        isinstance(stderr, str) and stderr.strip()
    ):
        return True
    if _g(record, "results_line_seen"):
        return True
    return _g(record, "passed") is not None or _g(record, "failed") is not None


def _cleanup_status(record: Any) -> str:
    """``"ok"`` / ``"incomplete"`` / ``"unknown"`` summary of the record's
    ``cleanup`` receipt. ``"incomplete"`` means the producer's own survivor
    sweep found live processes it could not confirm dead; ``"unknown"``
    means no cleanup receipt was ever captured (missing/malformed)."""
    cleanup = _g(record, "cleanup")
    if not isinstance(cleanup, dict):
        return "unknown"
    survivors = cleanup.get("survivors")
    if isinstance(survivors, list) and survivors:
        return "incomplete"
    if "survivors" in cleanup:
        return "ok"
    return "unknown"


def _infra_crash_marker(*texts: Any) -> "str | None":
    combined = "\n".join(t for t in texts if isinstance(t, str))
    return next((m for m in _INFRA_CRASH_TEXT_MARKERS if m in combined), None)


def _base_result(record: Any) -> dict[str, Any]:
    return {
        "classification": CLASS_MISSING_OR_AMBIGUOUS,
        "run_id": _g(record, "run_id"),
        "state": _g(record, "state"),
        "exit_code": _g(record, "exit_code"),
        "signal": _g(record, "signal"),
        "phase": _g(record, "state"),
        "last_progress_at": _g(record, "last_progress_at"),
        "timeout_kind": _g(record, "timeout_kind"),
        "cleanup_status": _cleanup_status(record),
        "cmd": _g(record, "command"),
        "cwd": _g(record, "cwd"),
        "owner_session": _g(record, "owner_session"),
        "passed": _g(record, "passed"),
        "failed": _g(record, "failed"),
        "reason": "",
    }


# ---------------------------------------------------------------------------
# 1. Classifiers.
# ---------------------------------------------------------------------------


def classify_test_run_record(record: Any) -> dict[str, Any]:
    """Classify a (possibly ``None``) ``TestRunRecord``-shaped object into
    exactly one of :data:`CLASSIFICATIONS`. Never raises. See the module
    docstring for the full contract; the short version: a ``passed`` state
    on disk is independently re-verified (real zero exit code, captured
    command/cwd, progress/heartbeat history, bounded output evidence, and a
    clean cleanup receipt) before this function will call it ``passed`` —
    every other terminal state maps straightforwardly, and anything
    non-terminal, corrupt, or unrecognized falls back to
    ``missing_or_ambiguous``.
    """
    result = _base_result(record)
    if record is None:
        result["reason"] = (
            "no test-run record found -- an absent receipt is never treated "
            "as evidence of success"
        )
        return result

    try:
        rt = _import_run_tests()
        terminal_states = rt.TERMINAL_STATES
        state_passed, state_failed = rt.STATE_PASSED, rt.STATE_FAILED
        state_crashed = rt.STATE_CRASHED
        state_timed_out, state_cancelled = rt.STATE_TIMED_OUT, rt.STATE_CANCELLED
    except Exception:  # noqa: BLE001 -- scripts.run_tests unavailable
        # Fall back to a hardcoded mirror of run_tests.py's stable state-name
        # vocabulary (part of its on-disk JSON contract, not an
        # implementation detail) so classification can still proceed.
        terminal_states = frozenset({"timed_out", "crashed", "cancelled", "passed", "failed"})
        state_passed, state_failed = "passed", "failed"
        state_crashed = "crashed"
        state_timed_out, state_cancelled = "timed_out", "cancelled"

    state = result["state"]
    if state not in terminal_states:
        result["reason"] = (
            f"run has not reached a terminal state (state={state!r}) -- a "
            "non-terminal or unknown run is never treated as passing evidence"
        )
        return result

    if state == state_cancelled:
        result["classification"] = CLASS_CANCELLED
        result["reason"] = _g(record, "error") or "run was cancelled before completion"
        return result

    if state == state_timed_out:
        result["classification"] = CLASS_TIMEOUT
        result["reason"] = f"run timed out (timeout_kind={result['timeout_kind']!r})"
        return result

    if state == state_crashed:
        result["classification"] = CLASS_INFRA_CRASH
        marker = _infra_crash_marker(_g(record, "stdout_tail"), _g(record, "stderr_tail"))
        result["reason"] = _g(record, "error") or (
            f"infrastructure crash (marker={marker!r})" if marker else "process crashed"
        )
        return result

    exit_code = result["exit_code"]
    valid_exit = _is_real_exit_code(exit_code)

    if state == state_failed:
        if not valid_exit:
            result["reason"] = (
                "state=failed but no real exit_code was captured -- ambiguous evidence"
            )
            return result
        result["classification"] = CLASS_FAILED
        result["reason"] = (
            f"{result['failed']} failing test(s)" if result["failed"] else
            f"non-zero exit_code {exit_code}"
        )
        return result

    if state == state_passed:
        problems: list[str] = []
        if not valid_exit or exit_code != 0:
            problems.append("no real zero exit_code captured")
        cmd = result["cmd"]
        if not isinstance(cmd, list) or not cmd:
            problems.append("command not captured")
        if not result["cwd"]:
            problems.append("cwd not captured")
        if _g(record, "last_progress_monotonic") is None and not result["last_progress_at"]:
            problems.append("no progress/heartbeat history captured")
        if not _has_bounded_output_evidence(record):
            problems.append("no stdout/stderr/results evidence captured (empty log)")
        if result["cleanup_status"] != "ok":
            problems.append(f"cleanup status is {result['cleanup_status']!r}, not confirmed clean")
        if problems:
            result["reason"] = "state=passed but required evidence is incomplete: " + "; ".join(problems)
            return result
        result["classification"] = CLASS_PASSED
        result["reason"] = (
            f"{result['passed']} passing test(s), clean exit" if result["passed"]
            else "clean exit, no failures reported"
        )
        return result

    # A future producer state this consumer doesn't yet recognize -- fail
    # closed rather than guessing at its meaning.
    result["reason"] = f"unrecognized terminal state {state!r}"
    return result


def classify_subprocess_result(
    *,
    exit_code: "int | None",
    signal: "int | None" = None,
    timed_out: bool = False,
    stdout: str = "",
    stderr: str = "",
    passed: "int | None" = None,
    failed: "int | None" = None,
) -> dict[str, Any]:
    """Classify a raw subprocess outcome (no durable ``TestRunRecord``
    available) into the same six-way vocabulary as
    :func:`classify_test_run_record`. Used by
    :func:`meridian.tool_discovery.run_targeted_tests`, which spawns its own
    ad hoc targeted-test subprocess rather than going through
    ``scripts/run_tests.py``. Never raises; never defaults to ``passed``.
    """
    result: dict[str, Any] = {
        "classification": CLASS_MISSING_OR_AMBIGUOUS,
        "exit_code": exit_code,
        "signal": signal,
        "passed": passed,
        "failed": failed,
        "reason": "",
    }
    if timed_out:
        result["classification"] = CLASS_TIMEOUT
        result["reason"] = "command timed out before completion"
        return result
    if signal is not None:
        if signal == 2:  # SIGINT
            result["classification"] = CLASS_CANCELLED
            result["reason"] = "process received SIGINT (cancelled)"
        else:
            result["classification"] = CLASS_INFRA_CRASH
            result["reason"] = f"process terminated by signal {signal}"
        return result
    if not _is_real_exit_code(exit_code):
        result["reason"] = "no real exit_code captured -- ambiguous evidence"
        return result

    marker = _infra_crash_marker(stdout, stderr)
    if exit_code < 0:
        result["classification"] = CLASS_INFRA_CRASH
        result["reason"] = f"negative exit_code {exit_code} indicates a signal-terminated crash"
        return result
    if exit_code in (3, 4):
        # pytest's own INTERNAL_ERROR (3) / USAGE_ERROR (4) exit codes -- an
        # infrastructure crash, not a normal assertion failure, whether or
        # not the output happens to also carry a recognizable text marker.
        result["classification"] = CLASS_INFRA_CRASH
        result["reason"] = (
            f"exit_code {exit_code} is pytest's own INTERNAL_ERROR/USAGE_ERROR "
            f"code{f' (marker={marker!r})' if marker else ''} -- infrastructure "
            "crash, not a test failure"
        )
        return result
    if marker is not None:
        result["classification"] = CLASS_INFRA_CRASH
        result["reason"] = f"output contains infra-crash marker {marker!r}"
        return result
    if exit_code == 0:
        combined = f"{stdout or ''}{stderr or ''}"
        if not (combined.strip() or passed is not None or failed is not None):
            result["reason"] = (
                "exit_code=0 but no output or result counts captured (empty "
                "log) -- never treated as passing evidence"
            )
            return result
        result["classification"] = CLASS_PASSED
        result["reason"] = f"{passed} passing test(s)" if passed else "clean exit"
        return result
    result["classification"] = CLASS_FAILED
    result["reason"] = (
        f"{failed} failing test(s)" if failed else f"non-zero exit_code {exit_code}"
    )
    return result


# ---------------------------------------------------------------------------
# 2. Duplicate-run / lease check.
# ---------------------------------------------------------------------------


def _resolved_root(repo_root: "str | Path | None") -> Path:
    root = Path(repo_root) if repo_root is not None else DEFAULT_REPO_ROOT
    try:
        return root.resolve()
    except OSError:
        return root


def check_active_test_run(repo_root: "str | Path | None" = None) -> dict[str, Any]:
    """Read-only probe of ``scripts.run_tests.TestRunLock`` for *repo_root*
    (defaults to :data:`DEFAULT_REPO_ROOT`). Never mutates the lock — this is
    a pure liveness check, reusing the exact same cross-platform PID-liveness
    primitive (``rt._pid_is_running``) the producer's own lock acquisition
    uses, so this can never disagree with the producer about whether a run
    is genuinely still in flight.

    Returns ``{"active", "checked", "run_id", "state", "owner_pid",
    "started_at", "last_progress_at", "lock_path", "state_path"}`` on a
    successful probe (``checked=True`` regardless of whether anything is
    active), or ``{"active": False, "checked": False, "error": ...}`` when
    the probe itself could not run (e.g. ``scripts.run_tests`` unimportable).
    ``active`` is True only when: the lock file exists, its recorded owner
    pid is confirmed alive right now, AND the current record (if any) has
    not yet reached a terminal state.
    """
    root = _resolved_root(repo_root)
    try:
        rt = _import_run_tests()
    except Exception as exc:  # noqa: BLE001
        return {"active": False, "checked": False, "error": f"scripts.run_tests unavailable: {exc}"}
    try:
        lock = rt.TestRunLock(root)
        record = rt.TestRunTracker.load_record(lock.state_path)
        owner_pid = lock._read_owner_pid()  # noqa: SLF001 -- same trusted subsystem, read-only
        lock_exists = lock.path.exists()
        pid_alive = bool(owner_pid) and owner_pid > 0 and rt._pid_is_running(owner_pid)  # noqa: SLF001
        non_terminal = record is None or record.state not in rt.TERMINAL_STATES
        active = bool(lock_exists and pid_alive and non_terminal)
        return {
            "active": active,
            "checked": True,
            "run_id": getattr(record, "run_id", None),
            "state": getattr(record, "state", None),
            "owner_pid": owner_pid,
            "started_at": getattr(record, "started_at", None),
            "last_progress_at": getattr(record, "last_progress_at", None),
            "lock_path": str(lock.path),
            "state_path": str(lock.state_path),
        }
    except Exception as exc:  # noqa: BLE001 -- a probe failure must never crash the caller
        return {"active": False, "checked": False, "error": str(exc)}


def check_duplicate_test_run(repo_root: "str | Path | None" = None) -> "dict[str, Any] | None":
    """Returns a structured duplicate-run report when another LIVE run
    already owns this checkout's test-run lock, or ``None`` when it is safe
    to proceed. Callers use this BEFORE starting/accepting a new full
    verification gate: reject the new attempt (or link to the existing
    ``run_id``) instead of silently letting two full gates race for the same
    checkout's resources.
    """
    probe = check_active_test_run(repo_root)
    if not probe.get("active"):
        return None
    return {
        "duplicate": True,
        "run_id": probe.get("run_id"),
        "state": probe.get("state"),
        "owner_pid": probe.get("owner_pid"),
        "started_at": probe.get("started_at"),
        "last_progress_at": probe.get("last_progress_at"),
        "message": (
            f"A test run is already active for this checkout "
            f"(run_id={probe.get('run_id')!r}, state={probe.get('state')!r}, "
            f"pid={probe.get('owner_pid')}). Link to it (wait for its result) "
            "instead of starting an overlapping gate — a second full run "
            "against the same checkout would silently consume duplicate "
            "resources and race the first one's lock/cleanup."
        ),
    }


# ---------------------------------------------------------------------------
# 3. Consumer-facing surface.
# ---------------------------------------------------------------------------


def get_test_run_evidence(repo_root: "str | Path | None" = None) -> dict[str, Any]:
    """One-call convenience: load the current record for *repo_root*,
    classify it, and attach the duplicate-run probe. This is what
    ``handoff.generate_handoff`` and ``complete_sprint_item`` call to
    surface run_id/state/exit_code/signal/phase/last_progress/timeout_kind/
    cleanup_status on their own evidence output.
    """
    root = _resolved_root(repo_root)
    duplicate = check_duplicate_test_run(root)
    record = None
    try:
        rt = _import_run_tests()
        lock = rt.TestRunLock(root)
        record = rt.TestRunTracker.load_record(lock.state_path)
    except Exception:  # noqa: BLE001
        record = None
    classified = classify_test_run_record(record)
    classified["repo_root"] = str(root)
    classified["duplicate_active_run"] = duplicate
    return classified


async def resolve_repo_root_for_session(
    db: Any,
    default_repo_root: "str | Path | None",
    session_id: "str | None",
) -> Path:
    """Prefer a SESSION's own registered worktree over *default_repo_root*
    (typically the server's main checkout), when one can be resolved.
    Mirrors ``sprint_evidence_guard._check_wrong_worktree``'s resolution
    technique exactly: a parallel-worktree executor session runs
    ``pixi run test`` from its OWN checkout, which owns a DIFFERENT
    ``TestRunLock`` (keyed by the checkout's own path hash) than the
    server's main checkout — reading the wrong one would systematically
    report ``missing_or_ambiguous`` for a session that in fact has a
    perfectly good local receipt. Falls back to *default_repo_root* (or
    :data:`DEFAULT_REPO_ROOT`) whenever the worktree cannot be resolved,
    which is always a safe, unverifiable-not-failed degrade.
    """
    root = _resolved_root(default_repo_root)
    if not session_id:
        return root
    try:
        from . import db as db_module  # noqa: PLC0415
        from . import worktree_cleanup  # noqa: PLC0415

        wt = await db_module.get_active_worktree_for_session(db, session_id)
        if not wt:
            return root
        wt_abs = worktree_cleanup.resolve_worktree_disk_path(root, wt["path"])
        if wt_abs.exists():
            return wt_abs
    except Exception:  # noqa: BLE001 -- unresolvable worktree is not itself a failure
        pass
    return root


async def verify_test_run_receipt_evidence(
    db: Any,
    repo_root: "str | Path | None",
    item: "dict[str, Any] | None" = None,
    *,
    session_id: "str | None" = None,
) -> dict[str, Any]:
    """The opt-in, fail-closed completion gate. Mirrors
    ``sprint_evidence_guard.verify_strict_completion_evidence`` /
    ``code_intel_receipt.verify_code_intel_prospecting``'s contract shape —
    never raises for an expected evidence problem; the caller decides
    applicability (a ``strict_test_evidence`` argument or the item's own
    ``require_strict_test_evidence`` flag) and only calls this when it
    already applies.

    Returns ``{"ok", "code", "message", "evidence"}``. ``ok`` is True only
    when the resolved evidence classifies as ``passed``.
    """
    root = await resolve_repo_root_for_session(db, repo_root, session_id)
    evidence = get_test_run_evidence(root)
    ok = evidence.get("classification") == CLASS_PASSED
    if ok:
        return {"ok": True, "code": None, "message": None, "evidence": evidence}
    return {
        "ok": False,
        "code": "TEST_RUN_RECEIPT_NOT_PASSED",
        "message": (
            "Refusing to complete: the latest test-run receipt for this "
            f"checkout classifies as {evidence.get('classification')!r} "
            f"({evidence.get('reason')}). Run the test suite via "
            "`pixi run test` (scripts/run_tests.py) so a durable, terminal "
            "receipt with a real exit code and a clean cleanup status exists "
            "for this claim, or pass override_test_run_receipt=true with a "
            "non-empty override_reason to explicitly acknowledge and "
            "complete anyway (audited)."
        ),
        "evidence": evidence,
    }


async def record_test_run_receipt_override(
    db: Any,
    project_id: str,
    item_id: str,
    *,
    actor: "str | None",
    reason: "str | None",
    evidence: "dict[str, Any]",
    tenant_id: "str | None" = None,
) -> dict[str, Any]:
    """Audit-log an explicit override of a blocked test-run-receipt gate.

    ``reason`` is REQUIRED and non-empty — mirrors ``sprint_evidence_guard.
    record_strict_evidence_override`` / ``code_intel_receipt.
    record_prospect_receipt_override`` exactly: an override with no stated
    reason is refused outright (``ValueError``), never silently accepted.
    """
    _reason = (reason or "").strip()
    if not _reason:
        raise ValueError(
            "override_reason is required and must be non-empty to override a "
            "blocked test-run-receipt gate -- an override with no stated "
            "reason is not auditable and is refused."
        )
    from . import db as db_module  # noqa: PLC0415

    detail = json.dumps({
        "item_id": item_id,
        "reason": _reason,
        "classification": evidence.get("classification"),
        "run_id": evidence.get("run_id"),
        "state": evidence.get("state"),
        "exit_code": evidence.get("exit_code"),
    })
    return await db_module.record_action_audit_event(
        db, OVERRIDE_EVENT_TYPE,
        tenant_id=tenant_id, project_id=project_id,
        actor=actor, detail=detail,
    )
