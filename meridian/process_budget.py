"""9c8336c4 — host-local memory/CPU budgets + quarantine for Meridian-owned
processes.

Confirmed host evidence (2026-08-04): a Windows Resource-Exhaustion-Detector
event at 2026-08-03 12:23 reported ``codebase-memory-mcp.exe`` consuming
8,253,239,296 bytes (~7.7 GiB); other events reported Python indexers around
2.7 GB each in normal operation. Cleanup-after-exit alone (the existing
``process_lifecycle``/orphan-reaper machinery) is not sufficient against this
failure mode: an ALIVE, runaway owned indexer can exhaust the host well
before any idle-cleanup path ever fires.

This module is the budget-enforcement *primitive* layered on top of
``process_lifecycle.py`` (3c4ed79d): given a pid this module's caller has
ALREADY proven it owns (an :class:`process_lifecycle.OwnedProcessHandle`,
a ``SerenaDaemon`` this pool spawned itself, ...), sample its current
memory/CPU footprint and decide whether it is within budget, should be
warned (a graceful "quiesce" signal — the first consecutive breach), or
should be forcibly terminated (a SECOND consecutive breach, i.e. the process
did not recover on its own within one sample interval).

Design contract (per the sprint notes):

* **Host-local, never project-shared.** Configuration is read from
  environment variables only (:func:`load_host_budget_config`) — never from
  ``meridian.toml`` or any DB-persisted, cross-machine config surface. A
  budget that is fine on one developer's laptop is not necessarily fine on a
  CI runner or a teammate's machine.
* **Only proven-owned processes are ever touched.** This module never
  discovers a pid on its own (no process enumeration, no name matching) —
  every public entry point takes a pid the CALLER already knows it owns.
  Unknown/system/peer processes are simply never passed in, so they can
  never be evaluated, let alone throttled or killed.
* **Graceful before forced.** :meth:`ProcessBudgetMonitor.evaluate` never
  jumps straight to ``"kill"`` — the first consecutive breach is reported as
  ``"quiesce"`` only (a caller-visible warning; nothing is torn down), giving
  the process one full sample interval to come back under budget on its own.
  Only a SECOND, still-breached sample escalates to ``"kill"``.
* **Bounded watchdog backoff.** After a caller reports back that a killed
  process survived the attempt (:meth:`ProcessBudgetMonitor.record_kill_outcome`),
  the monitor backs off (doubling, capped at ``max_backoff_seconds``) before
  it will recommend acting on that pid again — this module never recommends
  hammering ``taskkill``/``killpg`` on a tight loop against a stuck survivor.
* **Machine-readable report.** Every :meth:`evaluate` call returns a
  :class:`BudgetReport` (``.to_dict()`` is JSON-safe) carrying the sample
  (current/peak working set, private bytes, commit, sample time), the
  budget in effect, the action taken, and a human-readable reason — this is
  the "machine-readable reason/report" the sprint notes ask for.

This module deliberately does **not** attempt to diagnose or fix the
separate Windows kernel paged/nonpaged pool pressure item (6f465466) — that
is host/kernel-level memory pressure, categorically different from a single
process's own working set exceeding a budget. Callers that want to report
both should query 6f465466's own diagnostics independently; nothing here
merges the two.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Literal

BudgetAction = Literal["none", "quiesce", "kill"]

# ---------------------------------------------------------------------------
# Host-local configuration (environment variables only — see module docstring)
# ---------------------------------------------------------------------------

ENABLED_ENV = "MERIDIAN_PROCESS_BUDGET_ENABLED"
MAX_MEMORY_MB_ENV = "MERIDIAN_PROCESS_BUDGET_MAX_MEMORY_MB"
MAX_CPU_PERCENT_ENV = "MERIDIAN_PROCESS_BUDGET_MAX_CPU_PERCENT"
SAMPLE_SECONDS_ENV = "MERIDIAN_PROCESS_BUDGET_SAMPLE_SECONDS"
GRACE_SECONDS_ENV = "MERIDIAN_PROCESS_BUDGET_GRACE_SECONDS"
MAX_BACKOFF_SECONDS_ENV = "MERIDIAN_PROCESS_BUDGET_MAX_BACKOFF_SECONDS"

# Defaults sit ABOVE documented normal usage (~2.7 GB per indexer, per the
# 2026-08-04 host evidence above) but well BELOW the confirmed 8 GB runaway
# incident, so routine operation is never penalized while a genuine runaway
# is still caught within two sample intervals.
_DEFAULT_MAX_MEMORY_MB = 6144.0
_DEFAULT_MAX_CPU_PERCENT = 400.0  # generous multi-core ceiling
_DEFAULT_SAMPLE_SECONDS = 30.0
_DEFAULT_GRACE_SECONDS = 15.0
_DEFAULT_MAX_BACKOFF_SECONDS = 300.0


@dataclass(frozen=True)
class ProcessBudget:
    """Host-local, per-run/per-slot memory & CPU budget.

    ``enabled=False`` is the explicit opt-out this module preserves (see
    :data:`ENABLED_ENV`) — :meth:`ProcessBudgetMonitor.evaluate` always
    returns action ``"none"`` / reason ``"budget_disabled"`` when set,
    regardless of any sample. The class default (``enabled=True`` with the
    generous ceilings above) is the "safe default": enforcement is on, but
    tuned not to trip on ordinary usage.
    """

    enabled: bool = True
    max_memory_bytes: "float | None" = _DEFAULT_MAX_MEMORY_MB * 1024 * 1024
    max_cpu_percent: "float | None" = _DEFAULT_MAX_CPU_PERCENT
    sample_interval_seconds: float = _DEFAULT_SAMPLE_SECONDS
    grace_seconds: float = _DEFAULT_GRACE_SECONDS
    max_backoff_seconds: float = _DEFAULT_MAX_BACKOFF_SECONDS


def _env_float(name: str, default: "float | None") -> "float | None":
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


def load_host_budget_config() -> ProcessBudget:
    """Build a :class:`ProcessBudget` from host-local environment variables.

    Every variable is optional; an unset or unparsable value falls back to
    the module default rather than raising — configuration must never be
    able to crash a spawn/watchdog path. Set ``MERIDIAN_PROCESS_BUDGET_MAX_MEMORY_MB=0``
    (or any falsy float) to disable the memory ceiling specifically while
    keeping CPU enforcement (and vice versa); set
    ``MERIDIAN_PROCESS_BUDGET_ENABLED=0`` to disable budget enforcement
    entirely (the documented opt-out).
    """
    enabled = _env_bool(ENABLED_ENV, True)
    max_memory_mb = _env_float(MAX_MEMORY_MB_ENV, _DEFAULT_MAX_MEMORY_MB)
    max_cpu_percent = _env_float(MAX_CPU_PERCENT_ENV, _DEFAULT_MAX_CPU_PERCENT)
    sample_seconds = _env_float(SAMPLE_SECONDS_ENV, _DEFAULT_SAMPLE_SECONDS) or _DEFAULT_SAMPLE_SECONDS
    grace_seconds = _env_float(GRACE_SECONDS_ENV, _DEFAULT_GRACE_SECONDS) or _DEFAULT_GRACE_SECONDS
    max_backoff = (
        _env_float(MAX_BACKOFF_SECONDS_ENV, _DEFAULT_MAX_BACKOFF_SECONDS)
        or _DEFAULT_MAX_BACKOFF_SECONDS
    )
    return ProcessBudget(
        enabled=enabled,
        max_memory_bytes=(max_memory_mb * 1024 * 1024) if max_memory_mb else None,
        max_cpu_percent=max_cpu_percent if max_cpu_percent else None,
        sample_interval_seconds=sample_seconds,
        grace_seconds=grace_seconds,
        max_backoff_seconds=max_backoff,
    )


# ---------------------------------------------------------------------------
# Sampling
# ---------------------------------------------------------------------------


@dataclass
class BudgetSample:
    """One point-in-time resource sample for a pid.

    ``current_bytes`` is the process's current working set (RSS on POSIX,
    working-set size on Windows). ``peak_bytes``/``private_bytes``/
    ``commit_bytes`` are Windows-only (``psutil``'s ``pmem`` exposes
    ``peak_wset``/``private``/``pagefile`` there); they stay ``None`` on
    POSIX, where psutil's memory_info() does not report them — never
    fabricated.
    """

    pid: int
    sample_time: float
    current_bytes: "int | None" = None
    peak_bytes: "int | None" = None
    private_bytes: "int | None" = None
    commit_bytes: "int | None" = None
    cpu_percent: "float | None" = None


def sample_process(
    pid: int, proc_factory: "Callable[[int], Any] | None" = None
) -> "BudgetSample | None":
    """Best-effort resource sample for *pid*. ``None`` when ``psutil`` is
    unavailable, the process has already exited, or the sample otherwise
    fails — never raises.

    *proc_factory* is the test injection point (defaults to
    ``psutil.Process``) — same pattern as
    ``process_lifecycle.Win32JobAPI``'s injectable ``api_loader`` and
    ``serena_pool``'s injectable ``spawn``/``pid_alive``.
    """
    factory = proc_factory
    if factory is None:
        try:
            import psutil  # type: ignore
        except Exception:  # noqa: BLE001
            return None
        factory = psutil.Process
    try:
        proc = factory(pid)
        mem = proc.memory_info()
        current = getattr(mem, "rss", None)
        private = getattr(mem, "private", None)
        commit = getattr(mem, "pagefile", None)
        peak = getattr(mem, "peak_wset", None)
        try:
            cpu = proc.cpu_percent(interval=None)
        except Exception:  # noqa: BLE001
            cpu = None
        return BudgetSample(
            pid=pid,
            sample_time=time.time(),
            current_bytes=current,
            peak_bytes=peak,
            private_bytes=private,
            commit_bytes=commit,
            cpu_percent=cpu,
        )
    except Exception:  # noqa: BLE001 — process gone / access denied / psutil error
        return None


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


@dataclass
class BudgetReport:
    """Machine-readable outcome of one :meth:`ProcessBudgetMonitor.evaluate`
    call. ``survivor`` is filled in by the caller (via
    :meth:`ProcessBudgetMonitor.record_kill_outcome`'s effect on the NEXT
    report) only where relevant — a bare ``evaluate()`` result never claims
    to know the outcome of an action it did not itself perform."""

    label: str
    pid: int
    run_id: "str | None"
    sample: "BudgetSample | None"
    budget: ProcessBudget
    action: BudgetAction
    reason: str
    peak_current_bytes: "int | None" = None

    def to_dict(self) -> "dict[str, Any]":
        return {
            "label": self.label,
            "pid": self.pid,
            "run_id": self.run_id,
            "action": self.action,
            "reason": self.reason,
            "peak_current_bytes": self.peak_current_bytes,
            "budget": {
                "enabled": self.budget.enabled,
                "max_memory_bytes": self.budget.max_memory_bytes,
                "max_cpu_percent": self.budget.max_cpu_percent,
                "sample_interval_seconds": self.budget.sample_interval_seconds,
            },
            "sample": None
            if self.sample is None
            else {
                "sample_time": self.sample.sample_time,
                "current_bytes": self.sample.current_bytes,
                "peak_bytes": self.sample.peak_bytes,
                "private_bytes": self.sample.private_bytes,
                "commit_bytes": self.sample.commit_bytes,
                "cpu_percent": self.sample.cpu_percent,
            },
        }


# ---------------------------------------------------------------------------
# Monitor / enforcement decision
# ---------------------------------------------------------------------------


class ProcessBudgetMonitor:
    """Per-owned-process budget tracker.

    Pure decision logic — :meth:`evaluate` never itself terminates or
    signals anything. Ownership verification and the actual kill mechanism
    stay with the caller (``tunnel_client``'s owned-process backend,
    ``SerenaDaemonPool._release_daemon``), which is what makes "only
    processes proven owned ... may be throttled or terminated" true by
    construction: this monitor only ever evaluates a pid the caller already
    knew it owned and chose to pass in.
    """

    def __init__(
        self,
        label: str,
        budget: "ProcessBudget | None" = None,
        *,
        run_id: "str | None" = None,
    ):
        self.label = label
        self.budget = budget or load_host_budget_config()
        self.run_id = run_id
        self._peak_current_bytes = 0
        self._consecutive_breaches = 0
        self._backoff_seconds = 0.0
        self._next_eligible_time = 0.0

    def _breach_reason(self, sample: BudgetSample) -> "str | None":
        if self.budget.max_memory_bytes is not None and sample.current_bytes is not None:
            if sample.current_bytes > self.budget.max_memory_bytes:
                return (
                    f"memory {sample.current_bytes} bytes exceeds budget "
                    f"{self.budget.max_memory_bytes:.0f} bytes"
                )
        if self.budget.max_cpu_percent is not None and sample.cpu_percent is not None:
            if sample.cpu_percent > self.budget.max_cpu_percent:
                return (
                    f"cpu {sample.cpu_percent:.1f}% exceeds budget "
                    f"{self.budget.max_cpu_percent:.1f}%"
                )
        return None

    def evaluate(
        self, pid: int, sample: "BudgetSample | None", *, now: "float | None" = None
    ) -> BudgetReport:
        """Decide the action for the latest *sample* of *pid*.

        Returns a :class:`BudgetReport`. Never raises: a missing sample
        (``None``) always yields action ``"none"``.
        """
        when = time.monotonic() if now is None else now
        if not self.budget.enabled:
            return BudgetReport(
                label=self.label, pid=pid, run_id=self.run_id, sample=sample,
                budget=self.budget, action="none", reason="budget_disabled",
                peak_current_bytes=self._peak_current_bytes,
            )
        if sample is None:
            # Can't sample (psutil missing / process gone) — never guess at
            # a breach we have no evidence for.
            self._consecutive_breaches = 0
            return BudgetReport(
                label=self.label, pid=pid, run_id=self.run_id, sample=None,
                budget=self.budget, action="none", reason="no_sample",
                peak_current_bytes=self._peak_current_bytes,
            )
        if sample.current_bytes is not None:
            self._peak_current_bytes = max(self._peak_current_bytes, sample.current_bytes)

        if when < self._next_eligible_time:
            # Bounded watchdog backoff — still cooling down after a previous
            # kill attempt whose survivor was reported via
            # record_kill_outcome(survived=True).
            return BudgetReport(
                label=self.label, pid=pid, run_id=self.run_id, sample=sample,
                budget=self.budget, action="none", reason="backoff_cooldown",
                peak_current_bytes=self._peak_current_bytes,
            )

        reason = self._breach_reason(sample)
        if reason is None:
            self._consecutive_breaches = 0
            return BudgetReport(
                label=self.label, pid=pid, run_id=self.run_id, sample=sample,
                budget=self.budget, action="none", reason="within_budget",
                peak_current_bytes=self._peak_current_bytes,
            )

        self._consecutive_breaches += 1
        if self._consecutive_breaches == 1:
            # Graceful quiesce: warn only, give the process one full sample
            # interval to recover on its own before anything is forced.
            return BudgetReport(
                label=self.label, pid=pid, run_id=self.run_id, sample=sample,
                budget=self.budget, action="quiesce", reason=reason,
                peak_current_bytes=self._peak_current_bytes,
            )

        # Still breached on a later sample — escalate to forced termination.
        return BudgetReport(
            label=self.label, pid=pid, run_id=self.run_id, sample=sample,
            budget=self.budget, action="kill", reason=reason,
            peak_current_bytes=self._peak_current_bytes,
        )

    def record_kill_outcome(self, survived: bool, *, now: "float | None" = None) -> None:
        """Caller reports back whether the process was still alive right
        after acting on a ``"kill"`` report.

        ``survived=False`` (confirmed gone) resets all breach/backoff state
        so a FUTURE process reusing this monitor (e.g. a respawned slot)
        starts clean. ``survived=True`` applies bounded exponential backoff
        (doubling each time, capped at ``budget.max_backoff_seconds``) so a
        stuck/undead survivor is not resampled and re-killed on every tick.
        """
        when = time.monotonic() if now is None else now
        if not survived:
            self._consecutive_breaches = 0
            self._backoff_seconds = 0.0
            self._next_eligible_time = 0.0
            return
        self._backoff_seconds = min(
            self.budget.max_backoff_seconds,
            max(
                self.budget.sample_interval_seconds,
                (self._backoff_seconds * 2) or self.budget.sample_interval_seconds,
            ),
        )
        self._next_eligible_time = when + self._backoff_seconds
