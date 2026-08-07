"""Autonomous dispatcher daemon — poll for parallelizable sprint groups and
fan them out to Claude workers (item 57f7f7ba).

The dispatcher is a background asyncio loop. On each pass it asks the DB for
the next batch of *parallelizable* sprint items
(:func:`meridian.db.get_parallelizable_groups`) and enqueues a Claude worker
for each item in the first group, via :func:`meridian.enqueue.enqueue_claude_task`
(which spawns ``claude -p``). Items already dispatched in this process are
tracked so the same item is never dispatched twice.

The loop sleeps ``interval`` seconds between passes, but a ``board_change``
event (an :class:`asyncio.Event`) can wake it immediately — call
:meth:`Dispatcher.trigger` (or :meth:`Dispatcher.notify_board_change`) to force
an out-of-band dispatch pass right after, e.g., a sprint board mutation.

=========================================================================
CRITICAL GUARDRAIL — DEFAULT OFF
=========================================================================
This module ships disabled. The multi-tenant PRODUCTION server must NEVER
auto-spawn ``claude -p`` worker processes for tenants' boards by default.

:func:`start_dispatcher_if_enabled` — the only function the server lifespan
calls — is a *no-op* unless the environment variable
``MERIDIAN_DISPATCHER_ENABLED`` is exactly ``"1"``. When it is unset (the
default, including all production deploys) NO loop is started and NO worker is
ever spawned. Enabling it is an explicit, opt-in operator decision (e.g. a
single-tenant self-hosted automation box).

Concurrency is bounded (``max_in_flight``) so an enabled dispatcher can never
enqueue an unbounded number of workers, and the loop cancels cleanly on
shutdown.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Awaitable, Callable

import aiosqlite

from . import artifact_declaration as artifact_declaration_module
from . import db as db_module
from . import enqueue as enqueue_module
from . import executor_config as executor_config_module
from . import process_registry as process_registry_module

logger = logging.getLogger(__name__)

# Env var that gates the whole feature. Must equal "1" to enable. Anything
# else (unset, "0", "true", "") leaves the dispatcher off.
ENABLE_ENV_VAR = "MERIDIAN_DISPATCHER_ENABLED"

# Default seconds between polling passes when no board_change wakes the loop.
DEFAULT_INTERVAL_S = 30.0

# Default cap on concurrently dispatched workers. Bounds resource use so an
# enabled dispatcher can never fan out an unbounded number of subprocesses.
# 99c0c1be — sourced from executor_config.DEFAULT_PARALLELISM_TARGET so the
# dispatcher's default and the shared parallelism model never drift apart;
# the numeric value (4) is unchanged from before this feature existed.
DEFAULT_MAX_IN_FLIGHT = executor_config_module.DEFAULT_PARALLELISM_TARGET


# Signature of the enqueue primitive — injectable so tests can substitute a
# fake that never spawns a real subprocess.
EnqueueFn = Callable[..., Awaitable[dict[str, Any]]]

# 315b0a63 — signature of the optional lease-sweep hook: a zero-arg callable
# returning the list of expired-but-still-alive worker leases (see
# process_registry.ProcessLeaseBroker.report_unowned_survivors). Sync, not
# async — the broker itself does only local file/dict I/O, never network.
LeaseSweepFn = Callable[[], "list[dict[str, Any]]"]


def is_enabled() -> bool:
    """True only when the dispatcher is explicitly enabled via env var.

    The check is intentionally strict (``== "1"``) so a stray ``"true"`` or
    ``"0"`` never accidentally arms worker spawning in production.
    """
    return os.environ.get(ENABLE_ENV_VAR) == "1"


def _worker_prompt(item: dict[str, Any], project_id: str) -> str:
    """Build the worker prompt for one sprint item.

    Kept small and deterministic so tests can assert on it. The worker is a
    full Claude Code session pointed at a single sprint item.
    """
    item_id = item.get("id", "")
    title = (item.get("title") or "").strip()
    resources = item.get("resources") or []
    res_line = (
        f"It touches these resources (claim/lock them first): {', '.join(resources)}.\n"
        if resources
        else ""
    )
    return (
        f"You are an autonomous Meridian worker for project {project_id}.\n"
        f"Work ONLY on sprint item {item_id}: {title}\n"
        f"{res_line}"
        f"Claim the item (claim_sprint_item), implement it to production quality, "
        f"run the test suite, then call complete_sprint_item when done."
    )


def _promotion_target_for_item(item: dict[str, Any]) -> "str | None":
    """24f5146d — the docx merger-lock target ``item`` declares, or ``None``.

    Reads ``planned_output.promotion.merger_lock_key`` via
    :func:`meridian.artifact_declaration.effective_promotion` — the SAME
    normalized identifier :func:`meridian.artifact_declaration.
    acquire_promotion_merger_lock` derives its lock key from. An item with
    no artifact declaration at all (the overwhelming majority of sprint
    items) returns ``None`` here and is completely unaffected by the
    merger-lock awareness this function enables in :meth:`Dispatcher.
    dispatch_once`.
    """
    try:
        promotion = artifact_declaration_module.effective_promotion(item)
    except Exception:  # noqa: BLE001 — a malformed declaration must never break dispatch
        return None
    if not promotion:
        return None
    target = promotion.get("merger_lock_key")
    return target if isinstance(target, str) and target.strip() else None


class Dispatcher:
    """Background loop that dispatches parallelizable sprint groups to workers.

    One dispatcher is scoped to a single ``project_id``. The loop is started
    with :meth:`start` and stopped with :meth:`stop`; both are idempotent.
    """

    def __init__(
        self,
        db: aiosqlite.Connection,
        project_id: str,
        *,
        interval: float = DEFAULT_INTERVAL_S,
        max_in_flight: int = DEFAULT_MAX_IN_FLIGHT,
        host_limit: int | None = None,
        requested_parallelism: int | None = None,
        version: str | None = None,
        enqueue_fn: EnqueueFn | None = None,
        get_groups_fn: Callable[..., Awaitable[dict[str, Any]]] | None = None,
        evaluate_blockers_fn: Callable[..., Awaitable[dict[str, Any]]] | None = None,
        lease_sweep_fn: "LeaseSweepFn | None" = None,
    ) -> None:
        self.db = db
        self.project_id = project_id
        self.interval = interval
        # 99c0c1be — max_in_flight is now this dispatcher's CONFIGURED
        # parallelism target (clamped to [1, executor_config.
        # PARALLELISM_TARGET_CEILING] == 16, same rule as executor_config.
        # parallelism_target), not a bare hard cap. The actual per-pass
        # concurrency ceiling is the deterministic
        # min(requested, configured_target, host_limit, resource_safe_capacity)
        # computed fresh every dispatch_once() pass (see below), because
        # resource_safe_capacity — the size of the current first parallel-safe
        # group — legitimately varies pass to pass as the board changes.
        # Every existing caller that passed max_in_flight as a hard
        # concurrency cap keeps working identically: with no host_limit and a
        # first group never smaller than max_in_flight, effective_parallelism
        # == max_in_flight exactly as before this feature existed.
        self.configured_target = executor_config_module.normalize_parallelism_target(
            max_in_flight
        )
        # host_limit is a genuinely different axis from configured_target: it
        # is what the HOST/CLIENT reports, not what this project asks for.
        # None (default) means "unknown" and is excluded from the min() below
        # — never coerced into a de-facto cap of 1 (see executor_config.
        # resolve_parallelism's module docstring).
        self.host_limit = host_limit
        # None (default) means "derive requested_parallelism from the size of
        # the current first parallel-safe group each pass" — see dispatch_once.
        self.requested_parallelism = requested_parallelism
        # Back-compat: some callers/tests read disp.max_in_flight directly as
        # "the configured cap."
        self.max_in_flight = self.configured_target
        self.version = version
        # Injectable seams for testing — default to the real primitives.
        self._enqueue = enqueue_fn or enqueue_module.enqueue_claude_task
        self._get_groups = get_groups_fn or db_module.get_parallelizable_groups
        # b108f2e0 — typed blocker triage seam: default to the real DB-backed
        # evaluator. Injectable so tests can assert dispatch_once's
        # quarantine/run-stop behavior without a real board.
        self._evaluate_blockers = evaluate_blockers_fn or db_module.evaluate_board_blockers
        # 315b0a63 — None (default) disables the lease-sweep hook entirely,
        # matching this module's own "ships disabled unless explicitly
        # wired" guardrail: a caller opts in by passing a real sweep
        # function (see start_dispatcher_if_enabled below) or a test
        # double. Never constructed internally from here — this class stays
        # decoupled from process_registry unless a caller asks for it.
        self._lease_sweep = lease_sweep_fn
        # Most recent lease-sweep result (list of expired-but-alive lease
        # dicts), for introspection/tests. None until the hook is wired AND
        # a pass has run at least once.
        self.last_lease_sweep: "list[dict[str, Any]] | None" = None
        # Event the loop awaits with a timeout; set() forces an immediate pass.
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._stopped = False
        # Item ids already handed to a worker in this process — never re-dispatch.
        self._dispatched: set[str] = set()
        # Session row the workers are enqueued under. Created lazily on first
        # dispatch so an idle dispatcher leaves no rows behind.
        self._session_id: str | None = None
        # 99c0c1be — diagnostics: the most recent resolve_parallelism() result,
        # so a caller/dashboard can see WHY the last pass ran at the width it
        # did (requested_parallelism, effective_parallelism, host_limit,
        # configured_target, resource_safe_capacity, limiting_reason). None
        # until the first dispatch_once() pass with a non-empty board.
        self.last_parallelism: dict[str, Any] | None = None
        # b108f2e0 — last blocker-triage decision this dispatcher observed,
        # for introspection/tests. None until the first dispatch_once pass.
        self.last_blocker_decision: dict[str, Any] | None = None
        # 24f5146d — items skipped THIS pass because another live session
        # already holds the ONE canonical docx merger lock
        # (artifact_declaration.acquire/release/get_promotion_merger_lock)
        # for their declared promotion target. Diagnostics only — mirrors
        # last_blocker_decision's introspection role; [] until the first
        # dispatch_once pass finds a promotion-declaring item at all.
        self.last_merger_lock_skips: list[dict[str, Any]] = []

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> asyncio.Task[None]:
        """Start the background loop. Idempotent — returns the existing task."""
        if self._task is None or self._task.done():
            self._stopped = False
            self._task = asyncio.create_task(self.run())
        return self._task

    async def stop(self) -> None:
        """Cancel the loop and await its exit. Safe to call when not running."""
        self._stopped = True
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        self._task = None

    def trigger(self) -> None:
        """Wake the loop for an immediate dispatch pass (board_change hook)."""
        self._wake.set()

    # Alias matching the board_change vocabulary used elsewhere.
    notify_board_change = trigger

    # -- core loop ---------------------------------------------------------

    async def run(self) -> None:
        """Loop until cancelled, dispatching once per pass.

        Waits up to ``interval`` seconds between passes, but returns early when
        :meth:`trigger` sets the wake event. Never dies on a per-pass error.
        """
        # Do one pass immediately on startup, then settle into the wait cycle.
        while not self._stopped:
            try:
                await self.dispatch_once()
            except asyncio.CancelledError:
                break
            except Exception:  # noqa: BLE001 — a bad pass must not kill the loop
                logger.exception("dispatcher pass failed")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass  # normal interval elapsed
            except asyncio.CancelledError:
                break
            finally:
                self._wake.clear()

    async def _ensure_session(self) -> str:
        """Lazily create the worker-parent session this dispatcher enqueues under."""
        if self._session_id is None:
            session = await db_module.register_session(
                self.db,
                self.project_id,
                "dispatcher",
                session_type="worker",
            )
            self._session_id = session["id"]
        return self._session_id

    async def dispatch_once(self) -> list[dict[str, Any]]:
        """Run a single dispatch pass; return the task rows enqueued this pass.

        Dispatches the first parallelizable group, skipping any item already
        dispatched in this process, and stops once the pass's deterministic
        ``effective_parallelism`` (see ``executor_config.resolve_parallelism``)
        is reached so the number of live workers stays bounded.

        b108f2e0 — typed blocker triage runs BEFORE any enqueue this pass:

        * A fail-closed blocker (``run_stop=True`` — verified_security /
          integrity_corruption / run_global_blocker, or an explicit
          project ``run_stop`` policy) stops this dispatcher entirely
          (``self._stopped = True``) and enqueues nothing, mirroring the
          spec's "preserve explicit fail-closed stops" requirement.
        * Otherwise, any item in ``quarantined_item_ids`` is SKIPPED (never
          enqueued) but does NOT stop the pass — other, disjoint items in
          the same group still dispatch normally. This is the actual fix
          for the incident this module exists for: one under-scoped item
          no longer halts an otherwise-executable autonomous run.

        Best-effort: a failure evaluating blockers degrades to "no
        quarantine this pass" (dispatch proceeds unfiltered) rather than
        ever silently stopping the dispatcher over an enrichment failure —
        only an ACTUAL fail-closed classification stops it.

        24f5146d — merger-lock awareness: an item declaring a docx
        promotion target (``planned_output.promotion``) is SKIPPED this pass
        (never enqueued, never stops the dispatcher) when
        ``artifact_declaration.get_promotion_merger_lock`` reports another
        live session already holds that target's ONE canonical merger lock,
        or when an earlier item in this SAME pass already claimed it —
        never dispatch two workers at a promotion target concurrently. This
        is a best-effort, READ-ONLY heuristic: the dispatcher itself never
        acquires or releases the lock (it only enqueues a worker process; it
        has no synchronous visibility into when that worker's OWN
        acquire_promotion_merger_lock / apply_patch_manifest / release
        lifecycle actually runs) — the real, race-proof guarantee is
        transactional_merge.apply_patch_manifest's own base-hash staleness
        check plus the lock itself, enforced wherever a promotion is
        actually applied. See ``self.last_merger_lock_skips`` for
        diagnostics.
        """
        self.last_merger_lock_skips = []
        try:
            decision = await self._evaluate_blockers(
                self.db, self.project_id, version=self.version,
            )
        except Exception:  # noqa: BLE001 — blocker triage must never break dispatch
            logger.exception("blocker-policy evaluation failed; dispatching unfiltered")
            decision = None
        self.last_blocker_decision = decision

        # 315b0a63 — optional, best-effort worker-lease sweep. Runs before
        # any enqueue this pass so a stale/crashed-worker report is fresh
        # by the time this pass's decisions are logged; a sweep failure
        # never blocks or alters dispatch (same "must not break dispatch"
        # contract as blocker-policy evaluation above).
        if self._lease_sweep is not None:
            try:
                survivors = list(self._lease_sweep() or [])
                self.last_lease_sweep = survivors
                if survivors:
                    logger.warning(
                        "dispatcher: %d worker lease(s) expired with process still "
                        "alive (crashed client?): %s",
                        len(survivors),
                        [s.get("run_id") for s in survivors if isinstance(s, dict)],
                    )
            except Exception:  # noqa: BLE001 — best-effort, must not break dispatch
                logger.exception("worker-lease sweep failed")

        if decision and decision.get("run_stop"):
            logger.warning(
                "dispatcher halted by fail-closed blocker policy: %s",
                decision.get("run_stop_reason"),
            )
            self._stopped = True
            return []

        quarantined = set((decision or {}).get("quarantined_item_ids") or [])

        result = await self._get_groups(self.db, self.project_id, self.version)
        groups = (result or {}).get("groups") or []
        if not groups:
            return []

        first_group = groups[0]
        # 99c0c1be — resource_safe_capacity (the size of THIS pass's first
        # conflict-free group) is recomputed every pass since it legitimately
        # varies as the board changes. Folding it into the SAME deterministic
        # min() the rest of the parallelism model uses means wave planning
        # never serializes disjoint, resource-safe work just because some
        # OTHER input (e.g. an unreported host_limit) happens to be unknown —
        # an unknown host_limit is simply excluded from the min(), never
        # treated as 1.
        requested = (
            self.requested_parallelism
            if self.requested_parallelism is not None
            else len(first_group)
        )
        parallelism = executor_config_module.resolve_parallelism(
            requested,
            configured_target=self.configured_target,
            host_limit=self.host_limit,
            resource_safe_capacity=len(first_group),
        )
        self.last_parallelism = parallelism
        cap = parallelism["effective_parallelism"]

        in_flight = len(self._dispatched)
        if in_flight >= cap:
            return []

        enqueued: list[dict[str, Any]] = []
        # 24f5146d — targets this SAME pass has already decided to dispatch a
        # promotion for; a second item in the same pass sharing that target
        # is deferred to a later pass rather than raced against the first.
        claimed_targets_this_pass: set[str] = set()
        # Only the first group is safe to fan out simultaneously; later groups
        # depend on it draining. Run one group per pass.
        for item in first_group:
            item_id = item.get("id")
            if not item_id or item_id in self._dispatched:
                continue
            if item_id in quarantined:
                # Quarantined — skip THIS item only; other disjoint items in
                # the group keep dispatching normally (quarantine_continue).
                continue
            if in_flight >= cap:
                break
            promotion_target = _promotion_target_for_item(item)
            if promotion_target is not None:
                try:
                    lock_status = await artifact_declaration_module.get_promotion_merger_lock(
                        self.db, promotion_target, self.project_id,
                    )
                except Exception:  # noqa: BLE001 — a lock-status read failure never blocks dispatch
                    logger.exception(
                        "merger-lock status check failed for target %s", promotion_target,
                    )
                    lock_status = None
                held_by_other = bool((lock_status or {}).get("file_lock"))
                if held_by_other or promotion_target in claimed_targets_this_pass:
                    self.last_merger_lock_skips.append({
                        "item_id": item_id,
                        "target": promotion_target,
                        "reason": (
                            "merger lock held by another live session"
                            if held_by_other
                            else "another item in this same pass already claims this target"
                        ),
                    })
                    continue
                claimed_targets_this_pass.add(promotion_target)
            session_id = await self._ensure_session()
            prompt = _worker_prompt(item, self.project_id)
            try:
                task = await self._enqueue(
                    self.db,
                    session_id,
                    self.project_id,
                    prompt,
                )
            except Exception:  # noqa: BLE001 — one bad enqueue must not abort the pass
                logger.exception("failed to enqueue worker for item %s", item_id)
                continue
            # Mark dispatched only after a successful enqueue so a failure is retried.
            self._dispatched.add(item_id)
            in_flight += 1
            enqueued.append(task)
        return enqueued


def start_dispatcher_if_enabled(
    app: Any,
    db: aiosqlite.Connection,
    project_id: str,
    **kwargs: Any,
) -> Dispatcher | None:
    """Lifespan hook — start the dispatcher ONLY when explicitly enabled.

    GUARDRAIL: returns ``None`` and starts nothing unless
    ``MERIDIAN_DISPATCHER_ENABLED == "1"``. In production (and any default
    deploy) the env var is unset, so this is a no-op and NO worker process is
    ever spawned. The returned :class:`Dispatcher` (when enabled) is stashed on
    ``app.state.dispatcher`` so the lifespan teardown can stop it cleanly.
    """
    if not is_enabled():
        return None
    # 315b0a63 — wire the real lease-sweep hook by default once the
    # dispatcher itself is (explicitly, opt-in) enabled: report_unowned_
    # survivors() is read-only (no directory/file created unless a lease
    # was actually ever registered) so this adds no new side effects for a
    # host that never uses the worker-lease broker at all. A caller that
    # explicitly passes lease_sweep_fn (e.g. a test double, or None to
    # opt out) always wins over this default.
    kwargs.setdefault(
        "lease_sweep_fn",
        lambda: [
            lease.to_dict()
            for lease in process_registry_module.get_broker().report_unowned_survivors()
        ],
    )
    dispatcher = Dispatcher(db, project_id, **kwargs)
    dispatcher.start()
    try:
        app.state.dispatcher = dispatcher
    except Exception:  # noqa: BLE001 — app may be a stub in tests
        pass
    logger.info("autonomous dispatcher started for project %s", project_id)
    return dispatcher
