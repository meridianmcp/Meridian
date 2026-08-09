"""c5c3fc5f (Round 2, item R2-A) — wire real ExecutionEvent capture at
Meridian's own boundaries.

9e83be4a (Round 1 proposal e143949d) defined the canonical, versioned,
append-only :class:`meridian.ai_log.ExecutionEvent` envelope and the
:func:`meridian.db.ai_log.append_event` storage primitive, but deliberately
wired NOTHING to it — see that module's own docstring's "SCOPE" section:
"Nothing in the running server calls append_event today." This module is
that wiring: a thin, fail-safe CAPTURE layer that real boundary call sites
in this codebase invoke, plus the actual call sites for the boundaries this
item's touches_resources gives it write access to.

BOUNDARIES WIRED BY THIS ITEM
------------------------------
  * **session** — ``session.started`` fires from the ``start_session`` /
    ``register_session`` MCP tools, at the single dispatch chokepoint every
    transport (HTTP, stdio, connector) funnels through
    (``meridian/mcp/handler.py::_dispatch_mcp_tool`` — see that function for
    the call site). There is no explicit "end session" tool/boundary
    anywhere in this codebase (a session goes stale by inactivity, not by an
    explicit close call), so ``session.ended`` has no real call site to wire
    yet; :func:`capture_session_ended` is provided, tested, and ready for
    whichever future item adds an explicit session-close boundary.
  * **tool** — ``tool.completed`` fires from the same
    ``_dispatch_mcp_tool`` chokepoint for every executor-session tool call
    (mirrors the existing activity-heartbeat gate in that function: only
    sessions in the in-memory ``_EXECUTOR_SESSIONS`` set, excluding the same
    polling/housekeeping tools ``_ACTIVITY_SKIP_TOOLS`` already excludes —
    reusing an established precedent rather than inventing a new noise
    policy). Only fires on the SUCCESS path (the dispatcher's own
    activity-heartbeat/planner-nudge side channels use the identical
    convention), so a capture failure can never leave an orphaned
    "invoked-but-never-completed" event pair; :func:`capture_tool_invoked`
    is provided for a future item that wants invoked/completed pairing.
  * **agent / subprocess** — :mod:`meridian.process_registry`'s own module
    docstring defines a "worker lease" as covering BOTH "MCP servers" and
    "subagents" that an external client (Claude Code, Codex, Claude Desktop)
    spawns outside Meridian's own process tree — i.e. agent-spawn and
    subprocess-spawn are the SAME boundary in this codebase's terms.
    :func:`meridian.process_registry.register_process` /
    :func:`meridian.process_registry.release_process` are the real,
    production-ready async wrapper around
    :meth:`~meridian.process_registry.ProcessLeaseBroker.register` /
    :meth:`~meridian.process_registry.ProcessLeaseBroker.release` that
    invokes :func:`capture_process_registered` / :func:`capture_process_released`
    when a caller supplies DB/project context. See that module for why the
    wrapper takes an injected ``capture`` callable rather than importing this
    module directly (keeps the lease broker's own "lightweight,
    dependency-free, local-machine" design contract intact).

BOUNDARIES DELIBERATELY NOT WIRED BY THIS ITEM
------------------------------------------------
``tunnel connect`` and ``worktree create/remove`` are named in this item's
context but have no real call site inside this item's locked
touches_resources:

  * A tunnel actually connects in ``meridian/tunnel_client.py`` /
    ``meridian/routes/tunnel.py`` — neither file is in this item's
    touches_resources, and ``tunnel_client.py`` in particular carries
    unrelated, concurrent, uncommitted work in the shared main checkout
    (Serena default-command-detection) this item must not disturb.
  * Git-worktree creation/removal is entirely EXECUTOR-side (a human/agent
    runs ``git worktree add``/``remove`` in a shell) — Meridian's own server
    code never executes a worktree operation itself. The
    ``worktree_setup_cmd``/``worktree_cleanup_cmd`` strings
    ``claim_sprint_item`` returns are advisory text for the executor, not a
    Meridian-side action with a boundary to instrument.

:func:`capture_event` is fully generic (``event_type`` is an open,
namespaced taxonomy per :mod:`meridian.ai_log`) — a future item that DOES
own ``tunnel_client.py`` or an eventual Meridian-side worktree action can
call it directly without needing anything new from this module.

ROBUSTNESS CONTRACT (this item's notes)
------------------------------------------
  * **Preserve append-only canonical hashes** — every capture helper here
    delegates straight to :func:`meridian.db.ai_log.append_event`, never
    reimplementing hashing/validation itself, so a captured event is
    byte-for-byte what :mod:`meridian.ai_log`'s own contract would produce.
  * **Redact secrets before persistence** — also inherited for free:
    ``append_event`` already runs every payload through
    ``meridian.secret_redaction.check_for_secrets`` before it is ever
    hashed or inserted (see that module's docstring). This module never
    bypasses that gate.
  * **Avoid duplicate events** — :func:`capture_session_started` /
    :func:`capture_session_ended` key their ``idempotency_key`` off the
    session id (a session starts/ends at most once); :func:`capture_process_registered` /
    :func:`capture_process_released` key off the lease's ``run_id``
    (already globally unique per :mod:`meridian.process_registry`). Generic
    tool-dispatch events have no natural caller-supplied retry key at this
    layer and are deliberately NOT deduped via idempotency_key — see
    :func:`capture_tool_completed`'s docstring.
  * **Disabled/failed sinks never lose the local event receipt** —
    :func:`capture_event` NEVER raises. Capture being globally disabled
    (``MERIDIAN_AI_LOG_CAPTURE_DISABLED``), missing project scope, a
    redaction rejection, or any other storage error all degrade to a
    logged warning and a ``None`` return — the boundary operation that
    triggered capture (a session starting, a tool call completing, a
    process registering) keeps its own real result/receipt untouched
    either way. See ``tests/test_ai_log_capture.py`` for the tests proving
    this for each failure mode.
  * **No OTel/Langfuse dependency** — this module imports only
    :mod:`meridian.db` (already a hard dependency of everything in this
    package) and the standard library.
"""
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

#: Kill switch: set MERIDIAN_AI_LOG_CAPTURE_DISABLED=1/true/yes to turn every
#: capture_* call in this module into a guaranteed no-op (matches the
#: MERIDIAN_HOSTED / MERIDIAN_DEMO boolean-env-var convention used elsewhere
#: in this codebase, e.g. meridian/server.py, meridian/code_index.py).
_CAPTURE_DISABLED_ENV_VAR = "MERIDIAN_AI_LOG_CAPTURE_DISABLED"


def capture_enabled() -> bool:
    """True unless :data:`_CAPTURE_DISABLED_ENV_VAR` is truthy. A plain
    function (not a module-level constant) so tests can flip the env var
    and see the change immediately without reloading this module."""
    return os.environ.get(_CAPTURE_DISABLED_ENV_VAR, "").lower() not in (
        "1", "true", "yes",
    )


async def capture_event(
    db: Any,
    *,
    project_id: "str | None",
    event_type: str,
    actor_kind: str,
    actor_id: "str | None" = None,
    session_id: "str | None" = None,
    tenant_id: "str | None" = None,
    correlation_id: "str | None" = None,
    parent_event_id: "str | None" = None,
    source: "str | None" = None,
    payload: "dict[str, Any] | None" = None,
    payload_schema: "str | None" = None,
    occurred_at: "str | None" = None,
    idempotency_key: "str | None" = None,
) -> "dict[str, Any] | None":
    """The one generic, NEVER-RAISING capture primitive every boundary
    helper in this module (and any future one) should funnel through.

    Returns the stored event dict on success. Returns ``None`` — logging a
    single-line warning, never raising — when:

      * capture is globally disabled (:func:`capture_enabled` is False);
      * ``project_id`` is falsy (many boundaries, e.g. a subprocess
        registered before any project is known, legitimately have none yet
        — :class:`meridian.ai_log.ExecutionEvent` requires a non-empty
        ``project_id``, so this is a soft skip, not an error);
      * :func:`meridian.db.ai_log.append_event` itself raises — an invalid
        envelope (:class:`meridian.ai_log.ExecutionEventError`), a
        redaction rejection (``ValueError`` from
        ``meridian.secret_redaction.check_for_secrets``), or any other
        storage-layer error (a closed connection, a locked SQLite file,
        ...).

    This is the mechanism behind this item's "disabled/failed sinks do not
    lose the local event receipt" requirement: the CALLER decides what its
    own "receipt" is (a session row, a completed tool result, a registered
    lease) and that receipt's own durability is never gated on this
    function succeeding.
    """
    if not capture_enabled():
        return None
    if not project_id:
        logger.debug(
            "ai_log capture skipped for event_type=%r: no project_id in scope",
            event_type,
        )
        return None
    from meridian import db as db_module  # noqa: PLC0415 — avoid import cycles at module load

    try:
        return await db_module.append_event(
            db,
            project_id,
            event_type,
            actor_kind,
            actor_id=actor_id,
            session_id=session_id,
            tenant_id=tenant_id,
            correlation_id=correlation_id,
            parent_event_id=parent_event_id,
            source=source,
            payload=payload or {},
            payload_schema=payload_schema,
            occurred_at=occurred_at,
            idempotency_key=idempotency_key,
        )
    except Exception:  # noqa: BLE001 — capture must never break its caller's own boundary
        logger.warning(
            "ai_log capture failed for event_type=%r project_id=%r (non-fatal, "
            "the triggering operation's own result is unaffected)",
            event_type, project_id, exc_info=True,
        )
        return None


# ---------------------------------------------------------------------------
# Session boundary
# ---------------------------------------------------------------------------

async def capture_session_started(
    db: Any,
    *,
    project_id: "str | None",
    session_id: str,
    actor_id: "str | None" = None,
    human_id: "str | None" = None,
    client: "str | None" = None,
    role: "str | None" = None,
    tenant_id: "str | None" = None,
) -> "dict[str, Any] | None":
    """Wired into ``start_session``/``register_session`` at
    ``meridian/mcp/handler.py::_dispatch_mcp_tool``. ``idempotency_key`` is
    keyed on ``session_id`` alone — a session starts at most once, so a
    retried/duplicate dispatch (e.g. a client retry after a network blip)
    can never record a second ``session.started`` row for the same session.
    """
    return await capture_event(
        db,
        project_id=project_id,
        event_type="session.started",
        actor_kind="session",
        actor_id=actor_id or session_id,
        session_id=session_id,
        tenant_id=tenant_id,
        source="mcp",
        payload={"human_id": human_id, "client": client, "role": role},
        payload_schema="session_started@1",
        idempotency_key=f"session_started:{session_id}",
    )


async def capture_session_ended(
    db: Any,
    *,
    project_id: "str | None",
    session_id: str,
    actor_id: "str | None" = None,
    tenant_id: "str | None" = None,
    reason: "str | None" = None,
) -> "dict[str, Any] | None":
    """Ready, tested, NOT YET wired — see this module's docstring for why:
    no explicit "end session" boundary exists in this codebase today for it
    to hook. ``idempotency_key`` mirrors :func:`capture_session_started`
    (keyed on ``session_id`` — a session ends at most once)."""
    return await capture_event(
        db,
        project_id=project_id,
        event_type="session.ended",
        actor_kind="session",
        actor_id=actor_id or session_id,
        session_id=session_id,
        tenant_id=tenant_id,
        source="mcp",
        payload={"reason": reason},
        payload_schema="session_ended@1",
        idempotency_key=f"session_ended:{session_id}",
    )


# ---------------------------------------------------------------------------
# Tool boundary
# ---------------------------------------------------------------------------

async def capture_tool_invoked(
    db: Any,
    *,
    project_id: "str | None",
    tool_name: str,
    correlation_id: str,
    session_id: "str | None" = None,
    tenant_id: "str | None" = None,
    actor_id: "str | None" = None,
    args_summary: "str | None" = None,
) -> "dict[str, Any] | None":
    """NOT wired to a production call site by this item (see module
    docstring — only the success-path ``tool.completed`` is wired, to avoid
    an orphaned invoked-with-no-completed pair). Provided, tested, and ready
    for a future item that wants full invoked/completed pairing with
    latency attribution via ``parent_event_id``."""
    return await capture_event(
        db,
        project_id=project_id,
        event_type="tool.invoked",
        actor_kind="tool",
        actor_id=actor_id or tool_name,
        session_id=session_id,
        tenant_id=tenant_id,
        correlation_id=correlation_id,
        source="mcp",
        payload={"tool": tool_name, "summary": args_summary},
        payload_schema="tool_invoked@1",
    )


async def capture_tool_completed(
    db: Any,
    *,
    project_id: "str | None",
    tool_name: str,
    ok: bool,
    duration_ms: float,
    correlation_id: "str | None" = None,
    parent_event_id: "str | None" = None,
    session_id: "str | None" = None,
    tenant_id: "str | None" = None,
    actor_id: "str | None" = None,
    error_type: "str | None" = None,
) -> "dict[str, Any] | None":
    """Wired at ``meridian/mcp/handler.py::_dispatch_mcp_tool`` — the single
    chokepoint every transport (HTTP, stdio, connector) funnels through
    (see that function's own comments). Fires once per successful dispatch
    for executor-session tool calls, mirroring the SAME
    ``_EXECUTOR_SESSIONS`` + ``_ACTIVITY_SKIP_TOOLS`` gate that function's
    pre-existing activity-heartbeat side channel already uses.

    No ``idempotency_key`` — a generic MCP tool call carries no
    caller-supplied stable retry identity at this dispatch layer (unlike
    ``complete_sprint_item``'s own ``correlation_id`` argument), so a
    genuine client-side retry of the same logical call is recorded as two
    distinct ``tool.completed`` events. This is a deliberate, documented
    limitation, not an oversight: inventing a synthetic key from
    tool name + args would risk the opposite bug (silently DROPPING two
    genuinely different calls that happen to share the same arguments,
    e.g. two separate ``get_sprint_items()`` polls a second apart).
    """
    return await capture_event(
        db,
        project_id=project_id,
        event_type="tool.completed",
        actor_kind="tool",
        actor_id=actor_id or tool_name,
        session_id=session_id,
        tenant_id=tenant_id,
        correlation_id=correlation_id,
        parent_event_id=parent_event_id,
        source="mcp",
        payload={
            "tool": tool_name,
            "ok": ok,
            "duration_ms": round(duration_ms, 3),
            "error_type": error_type,
        },
        payload_schema="tool_completed@1",
    )


# ---------------------------------------------------------------------------
# Agent / subprocess boundary
# ---------------------------------------------------------------------------
#
# meridian.process_registry.register_process / release_process are the real
# wiring for this boundary (see that module) -- they call the two helpers
# below via an injected `capture` callable rather than this module importing
# process_registry, keeping the dependency direction one-way (session_tools
# -> nothing process-registry-specific; process_registry -> nothing
# session_tools-specific either, it only calls whatever callable it's given).

async def capture_process_registered(
    db: Any,
    *,
    project_id: "str | None",
    run_id: str,
    client: str,
    session_id: "str | None" = None,
    tenant_id: "str | None" = None,
    executable: "str | None" = None,
    cwd: "str | None" = None,
    owner_key: "str | None" = None,
) -> "dict[str, Any] | None":
    """Covers BOTH "agent spawn" and "subprocess spawn" — see
    :mod:`meridian.process_registry`'s own module docstring: a "worker
    lease" is explicitly defined there as covering "MCP servers or
    subagents" a client spawns outside Meridian's process tree, i.e. this
    codebase does not distinguish the two at the lease-broker layer.
    ``idempotency_key`` is keyed on ``run_id`` alone (already globally
    unique per lease — see ``process_lifecycle.new_run_id``), so a retried
    registration attempt for the same lease never double-records.
    """
    return await capture_event(
        db,
        project_id=project_id,
        event_type="agent.registered",
        actor_kind="system",
        actor_id=client,
        session_id=session_id,
        tenant_id=tenant_id,
        source="process_registry",
        payload={
            "run_id": run_id, "client": client, "executable": executable,
            "cwd": cwd, "owner_key": owner_key,
        },
        payload_schema="agent_registered@1",
        idempotency_key=f"agent_registered:{run_id}",
    )


async def capture_process_released(
    db: Any,
    *,
    project_id: "str | None",
    run_id: str,
    client: str,
    session_id: "str | None" = None,
    tenant_id: "str | None" = None,
) -> "dict[str, Any] | None":
    """See :func:`capture_process_registered` — same boundary, the release
    half. ``idempotency_key`` keyed on ``run_id`` (a lease releases at most
    once; :meth:`meridian.process_registry.ProcessLeaseBroker.release`
    itself already raises for a second release attempt via
    ``_get_owned``'s ``released`` check, so this mirrors that same
    single-release invariant at the event layer)."""
    return await capture_event(
        db,
        project_id=project_id,
        event_type="agent.released",
        actor_kind="system",
        actor_id=client,
        session_id=session_id,
        tenant_id=tenant_id,
        source="process_registry",
        payload={"run_id": run_id, "client": client},
        payload_schema="agent_released@1",
        idempotency_key=f"agent_released:{run_id}",
    )
