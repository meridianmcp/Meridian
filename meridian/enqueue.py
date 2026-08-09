"""Paid-tier: non-blocking Claude task enqueuer.

The free-tier MCP tools are synchronous — each call returns the result
immediately. That breaks when a tool needs to invoke a long-running Claude
Code subprocess: MCP clients enforce timeouts in the tens of seconds, but
a real coding task can take minutes.

This module solves that. :func:`enqueue_claude_task` returns immediately
with a ``pending`` task-log row, then spawns an asyncio background task
that actually runs the subprocess. When the subprocess exits, the worker
updates the same task-log row to ``done`` (with stdout) or ``failed``
(with stderr and exit code). Polling ``get_tasks`` is how other sessions
see the result land.

This is a paid-tier feature per ROADMAP.md. It is intentionally kept in
its own module so the free-tier surface stays unchanged.
"""

from __future__ import annotations

import asyncio
import os
import shlex
from typing import Any

import aiosqlite

from . import db as db_module

# 769e24a7 — optional completion-notification hook, called with the FINAL
# task_log row (or a minimal {"id", "status"} fallback if the DB update
# itself returned None) once a worker reaches ANY terminal outcome — done,
# failed, timeout, spawn error, or cancellation. May be sync or async.
# Never required: every existing caller that doesn't pass one gets
# byte-identical behavior to before this hook existed. See
# meridian.dispatcher.Dispatcher.dispatch_once for the production consumer
# (a lightweight "wake the loop" signal — the actual capacity-release
# accounting lives in Dispatcher.reconcile_active_leases, deliberately kept
# out of this lower-level module, which has no concept of sprint items or
# leases).
OnCompleteFn = "Callable[[dict[str, Any]], Any] | None"

# Marker prefix so other sessions reading the task log can spot enqueued
# Claude tasks at a glance.
PROMPT_PREFIX = "[enqueued-claude-task] "
RESULT_PREFIX = "[claude-result] "
ERROR_PREFIX = "[claude-error] "

# Cap how much subprocess output we paste back into the task description.
# A whole Claude transcript can be huge; the task log is meant to be
# skim-able, not archival. Full output stays on disk if the worker writes
# it there.
MAX_OUTPUT_CHARS = 4000


def _default_worker_argv() -> list[str]:
    """Resolve the worker command for the Claude subprocess.

    Order of precedence:
    1. ``MERIDIAN_WORKER_CMD`` env var (shell-split)
    2. Built-in default: ``claude -p``

    The prompt is appended to whatever argv this returns.
    """
    env = os.environ.get("MERIDIAN_WORKER_CMD")
    if env:
        return shlex.split(env)
    return ["claude", "-p"]


def _truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    """Trim long subprocess output to keep the task log readable."""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated, {len(text) - limit} more chars]"


# 49e06bcb — lightweight worker execution classes + deterministic routing.
#
# See meridian/dispatcher.py::_worker_prompt (and its module-level
# _classify_worker_execution) for the actual routing DECISION — this
# module only consumes the result. Wire format: an optional, exact
# leading line on `prompt`. A prompt with no marker (every caller that
# predates this feature, and every existing test that hands _run_worker
# a plain prompt string directly) resolves to SESSION_WORKER — today's
# exact, unchanged behavior.
_DETERMINISTIC_WORKER_MARKER_LINE = "[worker-class: deterministic]"

# Distinct log-prefixes for the deterministic class so a downstream reader
# (e.g. a future worker-telemetry adapter — 28da27fd, out of scope here)
# can tell a scripted verification/evidence/bookkeeping run apart from a
# full, ambiguous Claude Code session from the task row's description
# alone, without re-parsing prompt text. The marker line itself never
# reaches the persisted description or the subprocess argv — see
# _route_worker_execution below.
DETERMINISTIC_PROMPT_PREFIX = "[enqueued-deterministic-task] "
DETERMINISTIC_RESULT_PREFIX = "[deterministic-result] "
DETERMINISTIC_ERROR_PREFIX = "[deterministic-error] "


class WorkerExecutionClass:
    """Lightweight descriptor for one worker execution class.

    Both classes below still spawn ``argv`` as a real subprocess and
    share IDENTICAL lease (PID recording via
    :func:`meridian.db.update_task_worker_pid`), timeout, and
    ``pending -> in_progress -> done/failed`` status-transition mechanics
    in :func:`_run_worker` — 49e06bcb's "preserve leases, receipts,
    project identity, and failure policy" requirement is met by NOT
    forking that plumbing per class. All a class actually varies is the
    three task-log prefixes used to label its run: enough for a
    downstream consumer to distinguish "targeted, deterministic
    verification/evidence/bookkeeping" from "full, ambiguous
    implementation session" without any extra state.
    """

    __slots__ = ("name", "prompt_prefix", "result_prefix", "error_prefix")

    def __init__(
        self, name: str, prompt_prefix: str, result_prefix: str, error_prefix: str
    ) -> None:
        self.name = name
        self.prompt_prefix = prompt_prefix
        self.result_prefix = result_prefix
        self.error_prefix = error_prefix


# The only execution class prior to 49e06bcb, unchanged: the same three
# module-level prefixes every existing caller/test already asserts on.
SESSION_WORKER = WorkerExecutionClass("session", PROMPT_PREFIX, RESULT_PREFIX, ERROR_PREFIX)
DETERMINISTIC_WORKER = WorkerExecutionClass(
    "deterministic",
    DETERMINISTIC_PROMPT_PREFIX,
    DETERMINISTIC_RESULT_PREFIX,
    DETERMINISTIC_ERROR_PREFIX,
)


def _route_worker_execution(prompt: str) -> "tuple[WorkerExecutionClass, str]":
    """Split an optional leading routing marker off ``prompt``.

    Returns ``(execution_class, effective_prompt)``. ``effective_prompt``
    has the marker line (and the single newline after it) removed so
    neither the subprocess argv nor the persisted task description ever
    see the internal routing marker — both look byte-identical to
    pre-49e06bcb output for the SESSION class. Any prompt without an
    EXACT, recognized marker on its own first line — including every
    prompt that predates this feature — resolves to ``SESSION_WORKER``,
    so this can never change behavior for an existing caller.
    """
    first_line, sep, rest = prompt.partition("\n")
    if sep and first_line == _DETERMINISTIC_WORKER_MARKER_LINE:
        return DETERMINISTIC_WORKER, rest
    return SESSION_WORKER, prompt


async def _run_worker(
    db: aiosqlite.Connection,
    task_id: str,
    prompt: str,
    argv: list[str],
    timeout: float | None,
    *,
    on_complete: OnCompleteFn = None,
) -> None:
    """Background coroutine: run the subprocess and update the task row.

    Never raises on a NORMAL (non-cancelled) exit — failures are recorded on
    the task row as ``failed``. Each result branch reaches the single
    ``_finish`` exit point below and returns; the caller awaits this
    coroutine only in tests (production fires it via
    :func:`asyncio.create_task`).

    49e06bcb — routes to one of two lightweight worker execution classes
    (module-level ``SESSION_WORKER`` / ``DETERMINISTIC_WORKER``) based on
    an optional leading marker line on ``prompt`` (see
    :func:`_route_worker_execution`; ``meridian.dispatcher._worker_prompt``
    is the only thing that ever emits the marker). Both classes share this
    exact subprocess/PID/timeout/status machinery — only the task-log
    prefixes differ — so leases, receipts, project identity, and failure
    policy are identical across classes. A prompt with no marker (every
    pre-49e06bcb caller) is ``SESSION_WORKER``, i.e. byte-identical to
    this function's prior behavior.

    769e24a7 — two additions, both purely additive over the pre-existing
    branches above:

    * ``on_complete`` (see module-level ``OnCompleteFn``): invoked exactly
      once, from the single ``_finish`` helper below, for EVERY terminal
      branch (spawn-not-found, spawn-error, timeout, done, failed, AND
      cancellation) with the final task row. A raising/broken callback is
      swallowed (best-effort — a caller's notification hook must never be
      able to corrupt this worker's own DB bookkeeping). ``None`` (the
      default, every pre-769e24a7 caller) skips the call entirely — zero
      behavior change.
    * Cancellation handling: if this coroutine's own asyncio Task is
      cancelled (e.g. the process is shutting down and something explicitly
      cancels a still-in-flight worker task) at ANY await point — subprocess
      creation, ``proc.communicate()``, or even the final DB update — the
      outer ``except asyncio.CancelledError`` kills the subprocess if one
      was started, marks the task row ``failed`` with a clear "worker
      cancelled" message (so it can never linger stuck at ``in_progress``
      forever), best-effort notifies ``on_complete``, and then RE-RAISES —
      preserving normal `asyncio` cancellation propagation instead of
      silently swallowing it.

    Fail-closed guarantee (post-769e24a7 follow-up): the region between
    spawning the subprocess and the final ``done``/``failed`` write —
    the ``in_progress`` status write, ``update_task_worker_pid``, decoding
    output, and the terminal ``_finish`` calls themselves — used to be
    completely unguarded. Since production always invokes this coroutine
    via ``asyncio.create_task`` (fire-and-forget, see
    :func:`enqueue_claude_task`'s ``wait=False`` default path), any
    unexpected exception there (e.g. a transient DB error) had no caller to
    propagate to: the task row was left stuck at a non-terminal status
    forever, and ``Dispatcher.reconcile_active_leases`` — which only
    releases a lease once status is ``done``/``failed`` — could never
    observe or free it, permanently occupying that lease's capacity. The
    outer ``except Exception`` below closes that gap: it reuses the same
    ``_finish`` write path as every other terminal branch to mark the row
    ``failed`` with the exception recorded, best-effort kills a still-live
    subprocess, and then — unlike ``CancelledError`` — does NOT re-raise,
    matching this function's documented "never raises on a normal
    (non-cancelled) exit" contract. ``asyncio.CancelledError`` is a
    ``BaseException`` subclass (not ``Exception``) since Python 3.8, so
    this broad clause can never intercept cancellation; the two branches
    stay mutually exclusive and cancellation semantics are unchanged.
    """
    worker_class, effective_prompt = _route_worker_execution(prompt)
    proc: "asyncio.subprocess.Process | None" = None

    async def _finish(status: str, description: str) -> None:
        updated = await db_module.update_task(
            db, task_id, status=status, description=description,
        )
        if on_complete is None:
            return
        try:
            result = on_complete(updated or {"id": task_id, "status": status})
            if asyncio.iscoroutine(result):
                await result
        except Exception:  # noqa: BLE001 — a bad callback must never corrupt the task row
            pass

    try:
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                effective_prompt,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            await _finish(
                "failed",
                f"{worker_class.error_prefix}{effective_prompt}\n\n"
                f"worker command not found: {argv[0]} ({exc})",
            )
            return
        except Exception as exc:  # noqa: BLE001 — surface arbitrary spawn errors
            await _finish(
                "failed",
                f"{worker_class.error_prefix}{effective_prompt}\n\n"
                f"failed to spawn worker: {type(exc).__name__}: {exc}",
            )
            return

        # v1.0.1 — mark in_progress and record PID immediately on spawn
        await db_module.update_task(
            db, task_id,
            status="in_progress",
            description=f"{worker_class.prompt_prefix}{effective_prompt}\n\n[worker PID: {proc.pid}]",
        )
        await db_module.update_task_worker_pid(db, task_id, proc.pid)

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            await _finish(
                "failed",
                f"{worker_class.error_prefix}{effective_prompt}\n\n"
                f"worker timed out after {timeout}s",
            )
            return

        stdout = stdout_bytes.decode("utf-8", errors="replace").strip()
        stderr = stderr_bytes.decode("utf-8", errors="replace").strip()

        if proc.returncode == 0:
            body = _truncate(stdout) if stdout else "(no output)"
            await _finish(
                "done",
                f"{worker_class.result_prefix}{effective_prompt}\n\n{body}",
            )
        else:
            body = stderr or stdout or "(no output)"
            await _finish(
                "failed",
                f"{worker_class.error_prefix}{effective_prompt}\n\n"
                f"exit code {proc.returncode}\n{_truncate(body)}",
            )
    except asyncio.CancelledError:
        # 769e24a7 — this worker's own asyncio Task was cancelled. Kill a
        # still-running subprocess (best-effort — it may already be dead or
        # never started) and leave the task row in a real terminal state
        # instead of stuck at pending/in_progress forever, then re-raise so
        # normal cancellation propagation is preserved.
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
                await proc.wait()
            except Exception:  # noqa: BLE001 — best-effort cleanup only
                pass
        try:
            await _finish(
                "failed",
                f"{worker_class.error_prefix}{effective_prompt}\n\nworker cancelled",
            )
        except Exception:  # noqa: BLE001 — cleanup on the way out must not mask the cancellation
            pass
        raise
    except Exception as exc:  # noqa: BLE001 — fail closed: see docstring. Any unexpected
        # exception anywhere in the try block above (the in_progress write,
        # update_task_worker_pid, output decoding, or even a terminal
        # _finish call itself) must still land the task row in a real
        # terminal state instead of leaving it stuck — production only ever
        # runs this coroutine fire-and-forget, so nothing else ever will.
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
                await proc.wait()
            except Exception:  # noqa: BLE001 — best-effort cleanup only
                pass
        try:
            await _finish(
                "failed",
                f"{worker_class.error_prefix}{effective_prompt}\n\n"
                f"unhandled worker error: {type(exc).__name__}: {exc}",
            )
        except Exception:  # noqa: BLE001 — must not raise out of a fail-closed handler;
            # the row may already be unwritable for the same underlying
            # reason, but this coroutine still must not propagate.
            pass


async def enqueue_claude_task(
    db: aiosqlite.Connection,
    session_id: str,
    project_id: str,
    prompt: str,
    *,
    worker_argv: list[str] | None = None,
    timeout: float | None = 900.0,
    wait: bool = False,
    parent_session_id: str | None = None,
    on_complete: OnCompleteFn = None,
) -> dict[str, Any]:
    """Queue a Claude subprocess for async execution; return the pending task.

    Args:
        db: live aiosqlite connection.
        session_id: the session enqueueing the work.
        project_id: project the task belongs to.
        prompt: text passed as the final argv to the worker.
        worker_argv: override the worker command. Defaults to the result of
            :func:`_default_worker_argv` (``MERIDIAN_WORKER_CMD`` or
            ``["claude", "-p"]``).
        timeout: seconds before the worker is killed. ``None`` for no limit.
        wait: if True, block until the worker finishes before returning.
            Used by tests; production callers leave it False.
        on_complete: 769e24a7 — optional hook invoked (with the final task
            row) the moment the worker reaches ANY terminal outcome. See
            :func:`_run_worker` and module-level ``OnCompleteFn``. ``None``
            (default) is a no-op — every pre-769e24a7 caller is unaffected.
            Meaningless when ``wait=True`` since the caller already awaits
            the terminal state directly, but still honored for consistency
            (and exercised the same way — the callback still fires from
            inside ``_run_worker`` before this function returns).

    Returns:
        Dict with the task row as it stands when this function returns. If
        ``wait`` is False the row will be ``pending``; if True it will
        already be ``done`` or ``failed``.
    """
    if not prompt or not prompt.strip():
        raise ValueError("prompt must be a non-empty string")

    argv = worker_argv if worker_argv is not None else _default_worker_argv()
    if not argv:
        raise ValueError("worker command resolved to empty argv")

    # v1.2.1 — propagate parent_session_id so the timeline can
    # show 'this worker run was kicked off by that session'.
    # Defaults to the calling session itself when not specified.
    effective_parent = parent_session_id or session_id
    task = await db_module.log_task(
        db,
        session_id,
        project_id,
        f"{PROMPT_PREFIX}{prompt}",
        status="pending",
        parent_session_id=effective_parent,
    )

    coro = _run_worker(db, task["id"], prompt, argv, timeout, on_complete=on_complete)
    if wait:
        await coro
        updated = await db_module.get_task(db, task["id"])
        return updated or task
    asyncio.create_task(coro)
    return task
