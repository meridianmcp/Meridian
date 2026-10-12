"""Fan-out ledger over ``ai_log_events`` (sprint item 5c78017e, slice 1).

A fan-out run is one orchestrator dispatching several parallel research
agents. Each agent answers one question and reports a verdict backed by a
short quote from a source. This module records each agent's result as one
durable ``ai_log_events`` row and reads a run back as a deterministic
timeline, so a fan-out's findings survive the orchestrator's context window
and can be audited later.

Storage contract
----------------
* ``event_type``       ``fanout.agent_result`` (one row per agent result)
* ``actor_kind``       ``model`` (the subagent that produced the result)
* ``actor_id``         the caller's ``agent_key``
* ``correlation_id``   the ``run_id`` (the whole run is one correlation)
* ``payload_schema``   ``agent_result@1``
* ``idempotency_key``  ``fanout:<run_id>:<agent_key>``. A retried batch never
  writes a second row for the same agent; the existing unique index on
  ``(project_id, idempotency_key)`` backs this.

Digests only
------------
A payload carries the url, a quote of at most :data:`QUOTE_MAX_CHARS`
characters, the verdict, a sha256 of the full quote and an optional
caller-supplied sha256 of the source page. Raw page content is never
accepted: any field outside :data:`ALLOWED_RESULT_FIELDS` rejects that
entry. Secret-shaped text is refused by the redaction gate inside
:func:`meridian.db.ai_log.append_event`.

Per-entry results
-----------------
:func:`record_fanout_results` takes a batch and reports one status per entry
(``recorded``, ``duplicate``, ``rejected`` or ``error``). One bad entry never
blocks the rest of the batch. Whole-call problems (bad ``run_id``, empty or
oversized batch) raise :class:`FanoutValidationError` before anything is
written.
"""
from __future__ import annotations

import hashlib
import logging
import re
from typing import Any

from meridian.db import _row_to_dict
from meridian.db.ai_log import append_event, build_run_timeline

logger = logging.getLogger(__name__)

#: ``ai_log_events.event_type`` for one agent's recorded result.
EVENT_TYPE = "fanout.agent_result"
#: ``ai_log_events.payload_schema`` describing the digest payload shape.
PAYLOAD_SCHEMA = "agent_result@1"
#: ``ai_log_events.source`` for rows written by this module.
EVENT_SOURCE = "fanout_ledger"

#: Maximum characters of a quote stored. Longer quotes are truncated and
#: flagged; the hash still covers the full quote the agent supplied.
QUOTE_MAX_CHARS = 280
#: Maximum characters of an optional per-agent note.
NOTE_MAX_CHARS = 500
#: Maximum characters of a source url.
URL_MAX_CHARS = 2048
#: Maximum entries accepted in one ``record_fanout_results`` call.
MAX_ENTRIES_PER_CALL = 100
#: Default and maximum events returned by :func:`get_fanout_run`.
DEFAULT_TIMELINE_LIMIT = 200
MAX_TIMELINE_LIMIT = 1000

#: Closed verdict vocabulary. Anything else rejects that entry.
VERDICTS: frozenset[str] = frozenset({
    "supports", "refutes", "mixed", "inconclusive", "not_found",
})

#: The only keys an entry may carry. Any other key (``body``, ``html``,
#: ``page_text``, ...) rejects the entry, which keeps raw pages out.
ALLOWED_RESULT_FIELDS: frozenset[str] = frozenset({
    "agent_key", "url", "quote", "verdict", "source_hash", "note",
})

_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._\-]{0,127}")
_HASH_RE = re.compile(r"(?:sha256:)?([0-9a-f]{64})")


class FanoutValidationError(ValueError):
    """A whole-call or per-entry input problem. The message is safe to show."""


def _validate_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise FanoutValidationError(
            f"{label} must be 1-128 characters: letters, digits, '.', '_' or '-', "
            "starting with a letter or digit"
        )
    return value


def _sha256_of(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_entry(raw: Any) -> dict[str, Any]:
    """Validate one entry and build its ``agent_result@1`` payload.

    Returns ``{"agent_key": str, "payload": dict}``. Raises
    :class:`FanoutValidationError` for any rule violation.
    """
    if not isinstance(raw, dict):
        raise FanoutValidationError("entry must be an object")
    unknown = sorted(set(raw) - ALLOWED_RESULT_FIELDS)
    if unknown:
        raise FanoutValidationError(
            "unknown field(s) not accepted in a digest: " + ", ".join(unknown)
        )
    agent_key = _validate_id(raw.get("agent_key"), "agent_key")

    url = raw.get("url")
    if url is not None:
        if not isinstance(url, str) or not url.strip() or len(url) > URL_MAX_CHARS:
            raise FanoutValidationError(
                f"url must be a non-empty string of at most {URL_MAX_CHARS} characters"
            )
        url = url.strip()

    quote = raw.get("quote")
    quote_hash: str | None = None
    quote_truncated = False
    if quote is not None:
        if not isinstance(quote, str):
            raise FanoutValidationError("quote must be a string")
        try:
            quote_hash = _sha256_of(quote)
        except UnicodeEncodeError as exc:
            raise FanoutValidationError("quote must be valid UTF-8 text") from exc
        if len(quote) > QUOTE_MAX_CHARS:
            quote = quote[:QUOTE_MAX_CHARS]
            quote_truncated = True

    verdict = raw.get("verdict")
    if not isinstance(verdict, str) or verdict not in VERDICTS:
        raise FanoutValidationError(
            "verdict must be one of " + ", ".join(sorted(VERDICTS))
        )

    source_hash = raw.get("source_hash")
    if source_hash is not None:
        match = _HASH_RE.fullmatch(source_hash) if isinstance(source_hash, str) else None
        if match is None:
            raise FanoutValidationError(
                "source_hash must be a sha256 digest (64 lowercase hex characters, "
                "optionally prefixed 'sha256:')"
            )
        source_hash = "sha256:" + match.group(1)

    note = raw.get("note")
    if note is not None:
        if not isinstance(note, str) or len(note) > NOTE_MAX_CHARS:
            raise FanoutValidationError(
                f"note must be a string of at most {NOTE_MAX_CHARS} characters"
            )

    return {
        "agent_key": agent_key,
        "payload": {
            "agent_key": agent_key,
            "url": url,
            "quote": quote,
            "quote_truncated": quote_truncated,
            "quote_hash": quote_hash,
            "source_hash": source_hash,
            "verdict": verdict,
            "note": note,
        },
    }


async def _find_existing_event_id(db: Any, project_id: str, key: str) -> str | None:
    async with db.execute(
        "SELECT id FROM ai_log_events WHERE project_id = ? AND idempotency_key = ?",
        (project_id, key),
    ) as cur:
        row = await cur.fetchone()
    data = _row_to_dict(row)
    return data["id"] if data else None


async def record_fanout_results(
    db: Any,
    project_id: str,
    run_id: Any,
    entries: Any,
    *,
    session_id: "str | None" = None,
) -> dict[str, Any]:
    """Record one batch of agent results for ``run_id``; see module docstring.

    Each entry is written independently. Returns the per-entry ``results``
    list (``index``, ``agent_key``, ``status``, and ``event_id`` or ``error``)
    plus tallies. Never echoes a rejected payload back to the caller.
    """
    if not project_id or not isinstance(project_id, str):
        raise FanoutValidationError("project_id is required")
    run_id = _validate_id(run_id, "run_id")
    if not isinstance(entries, list) or not entries:
        raise FanoutValidationError("entries must be a non-empty list")
    if len(entries) > MAX_ENTRIES_PER_CALL:
        raise FanoutValidationError(
            f"at most {MAX_ENTRIES_PER_CALL} entries per call; split the batch"
        )

    results: list[dict[str, Any]] = []
    tally = {"recorded": 0, "duplicate": 0, "rejected": 0, "error": 0}
    for index, raw in enumerate(entries):
        raw_key = raw.get("agent_key") if isinstance(raw, dict) else None
        try:
            norm = normalize_entry(raw)
        except FanoutValidationError as exc:
            results.append({
                "index": index,
                "agent_key": raw_key if isinstance(raw_key, str) else None,
                "status": "rejected",
                "error": str(exc),
            })
            tally["rejected"] += 1
            continue

        agent_key = norm["agent_key"]
        key = f"fanout:{run_id}:{agent_key}"
        try:
            existing = await _find_existing_event_id(db, project_id, key)
            if existing is not None:
                results.append({
                    "index": index, "agent_key": agent_key,
                    "status": "duplicate", "event_id": existing,
                })
                tally["duplicate"] += 1
                continue
            event = await append_event(
                db,
                project_id,
                EVENT_TYPE,
                "model",
                actor_id=agent_key,
                session_id=session_id,
                correlation_id=run_id,
                source=EVENT_SOURCE,
                payload=norm["payload"],
                payload_schema=PAYLOAD_SCHEMA,
                idempotency_key=key,
            )
            results.append({
                "index": index, "agent_key": agent_key,
                "status": "recorded", "event_id": event["id"],
            })
            tally["recorded"] += 1
        except ValueError as exc:
            # Redaction rejection (check_for_secrets) or an envelope rejection.
            # The message names the pattern or field, never the matched text.
            results.append({
                "index": index, "agent_key": agent_key,
                "status": "rejected", "error": str(exc),
            })
            tally["rejected"] += 1
        except Exception as exc:  # noqa: BLE001 - one storage failure must not sink the batch
            logger.warning(
                "fanout ledger write failed for run %r agent %r (%s)",
                run_id, agent_key, type(exc).__name__, exc_info=True,
            )
            results.append({
                "index": index, "agent_key": agent_key,
                "status": "error",
                "error": f"{type(exc).__name__}: storage write failed",
            })
            tally["error"] += 1

    return {
        "run_id": run_id,
        "project_id": project_id,
        "requested": len(entries),
        **tally,
        "results": results,
    }


async def get_fanout_run(
    db: Any,
    project_id: str,
    run_id: Any,
    *,
    limit: Any = None,
) -> dict[str, Any]:
    """Read one run back as a deterministic timeline plus a verdict tally.

    Built on :func:`meridian.db.ai_log.build_run_timeline` filtered by
    ``correlation_id == run_id``. When more events match than ``limit``, the
    newest are kept and ``truncated`` is true. Read-only.
    """
    if not project_id or not isinstance(project_id, str):
        raise FanoutValidationError("project_id is required")
    run_id = _validate_id(run_id, "run_id")
    try:
        bound = int(limit) if limit is not None else DEFAULT_TIMELINE_LIMIT
    except (TypeError, ValueError):
        bound = DEFAULT_TIMELINE_LIMIT
    bound = max(1, min(bound, MAX_TIMELINE_LIMIT))

    events = await build_run_timeline(
        db, project_id, correlation_id=run_id, limit=bound,
    )
    agents: list[dict[str, Any]] = []
    verdict_counts: dict[str, int] = {}
    for event in events:
        if event.get("event_type") != EVENT_TYPE:
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        verdict = payload.get("verdict")
        verdict_counts[verdict] = verdict_counts.get(verdict, 0) + 1
        agents.append({
            "event_id": event.get("id"),
            "agent_key": event.get("actor_id"),
            "occurred_at": event.get("occurred_at"),
            "verdict": verdict,
            "url": payload.get("url"),
            "quote": payload.get("quote"),
            "quote_truncated": payload.get("quote_truncated"),
            "quote_hash": payload.get("quote_hash"),
            "source_hash": payload.get("source_hash"),
            "note": payload.get("note"),
        })
    return {
        "run_id": run_id,
        "project_id": project_id,
        "count": len(agents),
        "verdict_counts": verdict_counts,
        "agents": agents,
        "limit": bound,
        "truncated": len(events) >= bound,
    }


# ---------------------------------------------------------------------------
# MCP handlers (dispatched from meridian/mcp/handler.py::_handle_session_tools)
# ---------------------------------------------------------------------------

def _optional_session_id(args: dict[str, Any]) -> "str | None":
    value = args.get("session_id")
    if isinstance(value, str) and value.strip():
        return value.strip()[:128]
    return None


async def handle_record_fanout_results(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: record_fanout_results. Batched, per-entry best-effort digests."""
    project_id = args.get("project_id")
    if not project_id:
        return {"error": "record_fanout_results requires project_id"}
    try:
        return await record_fanout_results(
            db,
            project_id,
            args.get("run_id"),
            args.get("entries"),
            session_id=_optional_session_id(args),
        )
    except FanoutValidationError as exc:
        return {"error": str(exc)}


async def handle_get_fanout_run(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: get_fanout_run. Read-only timeline of one run's digests."""
    project_id = args.get("project_id")
    if not project_id:
        return {"error": "get_fanout_run requires project_id"}
    try:
        return await get_fanout_run(
            db, project_id, args.get("run_id"), limit=args.get("limit"),
        )
    except FanoutValidationError as exc:
        return {"error": str(exc)}
