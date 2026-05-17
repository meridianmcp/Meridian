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


async def _run_worker(
    db: aiosqlite.Connection,
    task_id: str,
    prompt: str,
    argv: list[str],
    timeout: float | None,
) -> None:
    """Background coroutine: run the subprocess and update the task row.

    Never raises — failures are recorded on the task row as ``failed``.
    Each result branch returns; the caller awaits this coroutine only in
    tests (production fires it via :func:`asyncio.create_task`).
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            prompt,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        await db_module.update_task(
            db,
            task_id,
            status="failed",
            description=(
                f"{ERROR_PREFIX}{prompt}\n\n"
                f"worker command not found: {argv[0]} ({exc})"
            ),
        )
        return
    except Exception as exc:  # noqa: BLE001 — surface arbitrary spawn errors
        await db_module.update_task(
            db,
            task_id,
            status="failed",
            description=(
                f"{ERROR_PREFIX}{prompt}\n\n"
                f"failed to spawn worker: {type(exc).__name__}: {exc}"
            ),
        )
        return

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        await db_module.update_task(
            db,
            task_id,
            status="failed",
            description=(
                f"{ERROR_PREFIX}{prompt}\n\n"
                f"worker timed out after {timeout}s"
            ),
        )
        return

    stdout = stdout_bytes.decode("utf-8", errors="replace").strip()
    stderr = stderr_bytes.decode("utf-8", errors="replace").strip()

    if proc.returncode == 0:
        body = _truncate(stdout) if stdout else "(no output)"
        await db_module.update_task(
            db,
            task_id,
            status="done",
            description=f"{RESULT_PREFIX}{prompt}\n\n{body}",
        )
    else:
        body = stderr or stdout or "(no output)"
        await db_module.update_task(
            db,
            task_id,
            status="failed",
            description=(
                f"{ERROR_PREFIX}{prompt}\n\n"
                f"exit code {proc.returncode}\n{_truncate(body)}"
            ),
        )


async def enqueue_claude_task(
    db: aiosqlite.Connection,
    session_id: str,
    project_id: str,
    prompt: str,
    *,
    worker_argv: list[str] | None = None,
    timeout: float | None = 900.0,
    wait: bool = False,
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

    task = await db_module.log_task(
        db,
        session_id,
        project_id,
        f"{PROMPT_PREFIX}{prompt}",
        status="pending",
    )

    coro = _run_worker(db, task["id"], prompt, argv, timeout)
    if wait:
        await coro
        updated = await db_module.get_task(db, task["id"])
        return updated or task
    asyncio.create_task(coro)
    return task
