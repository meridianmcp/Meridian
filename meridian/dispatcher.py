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

from . import db as db_module
from . import enqueue as enqueue_module

logger = logging.getLogger(__name__)

# Env var that gates the whole feature. Must equal "1" to enable. Anything
# else (unset, "0", "true", "") leaves the dispatcher off.
ENABLE_ENV_VAR = "MERIDIAN_DISPATCHER_ENABLED"

# Default seconds between polling passes when no board_change wakes the loop.
DEFAULT_INTERVAL_S = 30.0

# Default cap on concurrently dispatched workers. Bounds resource use so an
# enabled dispatcher can never fan out an unbounded number of subprocesses.
DEFAULT_MAX_IN_FLIGHT = 4


# Signature of the enqueue primitive — injectable so tests can substitute a
# fake that never spawns a real subprocess.
EnqueueFn = Callable[..., Awaitable[dict[str, Any]]]


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
        version: str | None = None,
        enqueue_fn: EnqueueFn | None = None,
        get_groups_fn: Callable[..., Awaitable[dict[str, Any]]] | None = None,
    ) -> None:
        self.db = db
        self.project_id = project_id
        self.interval = interval
        self.max_in_flight = max(1, int(max_in_flight))
        self.version = version
        # Injectable seams for testing — default to the real primitives.
        self._enqueue = enqueue_fn or enqueue_module.enqueue_claude_task
        self._get_groups = get_groups_fn or db_module.get_parallelizable_groups
        # Event the loop awaits with a timeout; set() forces an immediate pass.
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._stopped = False
        # Item ids already handed to a worker in this process — never re-dispatch.
        self._dispatched: set[str] = set()
        # Session row the workers are enqueued under. Created lazily on first
        # dispatch so an idle dispatcher leaves no rows behind.
        self._session_id: str | None = None

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
        dispatched in this process, and stops once ``max_in_flight`` is
        reached so the number of live workers stays bounded.
        """
        result = await self._get_groups(self.db, self.project_id, self.version)
        groups = (result or {}).get("groups") or []
        if not groups:
            return []

        in_flight = len(self._dispatched)
        if in_flight >= self.max_in_flight:
            return []

        enqueued: list[dict[str, Any]] = []
        # Only the first group is safe to fan out simultaneously; later groups
        # depend on it draining. Run one group per pass.
        for item in groups[0]:
            item_id = item.get("id")
            if not item_id or item_id in self._dispatched:
                continue
            if in_flight >= self.max_in_flight:
                break
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
    dispatcher = Dispatcher(db, project_id, **kwargs)
    dispatcher.start()
    try:
        app.state.dispatcher = dispatcher
    except Exception:  # noqa: BLE001 — app may be a stub in tests
        pass
    logger.info("autonomous dispatcher started for project %s", project_id)
    return dispatcher
