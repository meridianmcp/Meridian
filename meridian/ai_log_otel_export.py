"""R2-G — optional OTel / self-hosted-Langfuse EXPORT ADAPTER for the AI-log
event stream. SAFE-BY-DEFAULT, OPT-IN — mirrors the design contract
:mod:`meridian.semantic_search` already established for an optional,
heavier dependency (OFF by default, lazy import, never raises, degrades to
a no-op) and applies it to telemetry export instead of search.

BINDING ARCHITECTURAL DECISION (pinned 03112002) — read this before
extending this module. Meridian's own durable AI-log event stream
(:class:`meridian.ai_log.ExecutionEvent`, stored via
:mod:`meridian.db.ai_log`) is the CANONICAL record of agent activity.
Langfuse (or any other external observability tool) may consume an EXPORT
of this stream later; it must NEVER become a second source of truth. This
module is exactly that export — an adapter, never a capture path:

  * It only READS from ``ai_log_events`` (via
    :mod:`meridian.db.ai_log_export_config`'s bounded, ascending,
    resumable-cursor query) — it never writes to that table, and nothing in
    :mod:`meridian.ai_log`/:mod:`meridian.db.ai_log`/
    :mod:`meridian.session_tools` imports this module or knows it exists.
    ``tests/test_ai_log_contract_matrix.py``'s
    ``test_local_first_core_modules_have_no_hard_import_of_external_sinks``
    pins that those four "local-first core" modules never import redis/
    langfuse/opentelemetry — this module is deliberately OUTSIDE that list
    (it is the one place in this codebase that legitimately does).
  * It is triggered ON DEMAND (an explicit ``export_ai_log_otel`` MCP tool
    call, or an operator's own cron/script calling :func:`run_otel_export`
    directly) — there is no live hook on the ``append_event`` write path.
    This is deliberate, not a shortcut: the write path stays synchronous,
    single-purpose, and un-slowed by an external endpoint's latency by
    construction, since it is a completely separate call in a completely
    separate code path, never inline with a capture boundary.
  * A slow/unavailable external endpoint can still never block Meridian's
    OWN event loop even during an explicit export call: the one blocking
    call this module makes (``OTLPLogExporter.export`` — a synchronous
    HTTP POST under the hood) always runs via ``asyncio.to_thread`` under
    an ``asyncio.wait_for`` deadline (see :func:`_send_chunk_with_retry` /
    :func:`run_otel_export`), so a hung collector stalls one thread-pool
    worker, never the ASGI event loop other sessions' requests share.

SAFE-BY-DEFAULT CONTRACT
-------------------------
  * **OFF by default.** :func:`_global_enabled` is False unless
    ``MERIDIAN_AI_LOG_OTEL_ENABLED`` is truthy. With it unset, calling
    :func:`run_otel_export` returns ``{"status": "disabled", ...}``
    immediately — no lazy import is even attempted, no DB write beyond the
    read of an optional existing config row.
  * **No new hard dependency.** The real OTel Python SDK client library
    (``opentelemetry-sdk`` + ``opentelemetry-exporter-otlp-proto-http``) is
    declared ONLY under this project's ``[project.optional-dependencies]
    otel`` extra (pyproject.toml) — a normal ``pip install meridian-server``
    never pulls it in. :func:`_import_otel_deps` lazily imports it and
    returns ``None`` on ANY failure (a bare ``ImportError`` when the extra
    isn't installed, or any other exception — this codebase's own probe
    against opentelemetry-sdk 1.44.0 found the Logs SDK's
    ``opentelemetry.sdk._logs`` surface has already changed shape across
    versions, e.g. ``LogRecord``/``LogData`` existed in older releases and
    do not in 1.44; a version mismatch must degrade exactly like a missing
    package, never crash an export attempt). Every call site that touches
    the returned deps mapping is itself wrapped so a partial/unexpected
    surface still degrades to ``"unavailable"`` rather than propagating.
  * **Never raises.** :func:`run_otel_export` and :func:`get_export_status`
    catch everything; the worst outcome is a returned ``status: "error"``
    dict plus a logged warning — matches ``meridian.session_tools``'s
    "disabled/failed sinks never lose the local event receipt" contract
    (there is nothing here TO lose, since nothing upstream depends on this
    module's success).
  * **Bounded queues.** Each call fetches at most a clamped
    ``MERIDIAN_AI_LOG_OTEL_BATCH_SIZE`` (default 200, hard cap 1000) events,
    sent in clamped ``MERIDIAN_AI_LOG_OTEL_CHUNK_SIZE`` sub-batches (default
    50, hard cap 200) — never an unbounded fetch or an unbounded single
    HTTP payload.
  * **Retry/backoff on the export path ITSELF** — bounded exponential
    backoff-with-jitter, capped attempts per chunk
    (``MERIDIAN_AI_LOG_OTEL_MAX_RETRIES``), and a hard overall wall-clock
    deadline per call (``MERIDIAN_AI_LOG_OTEL_TOTAL_DEADLINE_S``). Retrying
    is entirely the EXPORT path's own concern — a caller of
    ``append_event``/``capture_event`` is never involved and never waits on
    any of this.
  * **Redaction, twice over.** ``append_event`` already hard-rejects any
    secret-shaped payload at WRITE time (fail-closed —
    :func:`meridian.secret_redaction.check_for_secrets`, see
    ``meridian.db.ai_log``'s docstring). This module additionally runs
    :func:`meridian.secret_redaction.redact` (mask, not reject) over each
    outgoing log body immediately before it would leave the local database
    boundary — defense in depth against a future write-time-gate bug or a
    historical row written before that gate existed, per this item's own
    explicit "redaction ... remain[s] authoritative" requirement.
  * **No secrets in persisted config.** Endpoint/service-name overrides may
    be stored per project (:mod:`meridian.db.ai_log_export_config` — see
    its own docstring), but an OTLP bearer token/API key is ALWAYS read
    fresh from an environment variable
    (``MERIDIAN_AI_LOG_OTEL_HEADERS``/the standard
    ``OTEL_EXPORTER_OTLP_HEADERS``) and never persisted anywhere.
  * **Local persistence stays authoritative.** This module never deletes,
    mutates, or supersedes an ``ai_log_events`` row. Losing every byte this
    module has ever sent changes nothing about what Meridian itself knows —
    a fresh export pass just resends from the last durable watermark (or
    from the beginning, if the watermark row itself is cleared).

Self-hosted Langfuse compatibility (this item's other explicit requirement)
is DOCUMENTATION, not a second wire protocol: self-hosted Langfuse exposes
an OTLP-compatible ingestion endpoint, so pointing
``MERIDIAN_AI_LOG_OTEL_ENDPOINT``/a project's ``otlp_endpoint`` at that
endpoint (and setting ``langfuse_compat``/``MERIDIAN_AI_LOG_OTEL_LANGFUSE_COMPAT``
purely as an informational hint reflected in :func:`get_export_status` and
the exported resource attributes) is the entire integration — see
``docs/configuration.md``'s "Optional: AI-log OTel / Langfuse export"
section. No Langfuse-specific SDK, wire format, or paid dependency is
introduced by this module.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
from datetime import datetime
from typing import Any
from urllib.parse import unquote

from meridian.db import ai_log_export_config as cfg_db
from meridian.secret_redaction import redact

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config knobs (all env-tunable; read fresh on every call so tests can
# monkeypatch os.environ / call sites without reloading this module — same
# convention as meridian.semantic_search / meridian.redis_bridge).
# ---------------------------------------------------------------------------

_ENV_ENABLED = "MERIDIAN_AI_LOG_OTEL_ENABLED"
_ENV_ENDPOINT = "MERIDIAN_AI_LOG_OTEL_ENDPOINT"
_ENV_HEADERS = "MERIDIAN_AI_LOG_OTEL_HEADERS"
_ENV_SERVICE_NAME = "MERIDIAN_AI_LOG_OTEL_SERVICE_NAME"
_ENV_LANGFUSE_COMPAT = "MERIDIAN_AI_LOG_OTEL_LANGFUSE_COMPAT"
_ENV_BATCH_SIZE = "MERIDIAN_AI_LOG_OTEL_BATCH_SIZE"
_ENV_CHUNK_SIZE = "MERIDIAN_AI_LOG_OTEL_CHUNK_SIZE"
_ENV_MAX_RETRIES = "MERIDIAN_AI_LOG_OTEL_MAX_RETRIES"
_ENV_BACKOFF_BASE_S = "MERIDIAN_AI_LOG_OTEL_BACKOFF_BASE_S"
_ENV_TIMEOUT_S = "MERIDIAN_AI_LOG_OTEL_TIMEOUT_S"
_ENV_TOTAL_DEADLINE_S = "MERIDIAN_AI_LOG_OTEL_TOTAL_DEADLINE_S"

_TRUTHY = {"1", "true", "yes", "on", "y", "t"}

_DEFAULT_BATCH_SIZE = 200
_MAX_BATCH_SIZE = 1000
_DEFAULT_CHUNK_SIZE = 50
_MAX_CHUNK_SIZE = 200
_DEFAULT_MAX_RETRIES = 3
_MAX_MAX_RETRIES = 10
_DEFAULT_BACKOFF_BASE_S = 0.5
_DEFAULT_TIMEOUT_S = 5.0
_DEFAULT_TOTAL_DEADLINE_S = 20.0


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUTHY


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _global_enabled() -> bool:
    """The single, explicit, default-OFF opt-in gate for the WHOLE feature.
    A project's own ``enabled`` override (see
    :func:`_effective_settings`) can only NARROW this (force one project
    off) — it can never widen a globally-disabled feature to "on" for one
    project. Matches this item's own notes: "behind an explicit feature
    flag (default OFF)" describes the feature, not a per-project switch."""
    return _env_truthy(_ENV_ENABLED)


# ---------------------------------------------------------------------------
# Lazy, guarded OTel dependency import — the ONLY place in this codebase
# that may import opentelemetry/langfuse. See module docstring's
# "No new hard dependency" contract.
# ---------------------------------------------------------------------------

def _import_otel_deps() -> "dict[str, Any] | None":
    """Return a namespace dict of the OTel SDK classes this module needs, or
    ``None`` if the optional ``otel`` extra isn't installed OR its API
    surface doesn't match what this module expects (see module docstring —
    the Logs SDK's ``_logs`` module is explicitly experimental/unstable
    across opentelemetry-sdk releases). This is THE seam tests monkeypatch
    to exercise the "dependency available" path without actually installing
    the real package — see tests/test_ai_log_otel_export.py.
    """
    try:
        from opentelemetry._logs import LogRecord as _ApiLogRecord
        from opentelemetry._logs import SeverityNumber as _SeverityNumber
        from opentelemetry.exporter.otlp.proto.http._log_exporter import (
            OTLPLogExporter as _OTLPLogExporter,
        )
        from opentelemetry.sdk._logs import ReadableLogRecord as _ReadableLogRecord
        from opentelemetry.sdk._logs.export import (
            LogRecordExportResult as _LogRecordExportResult,
        )
        from opentelemetry.sdk.resources import Resource as _Resource
        from opentelemetry.sdk.util.instrumentation import (
            InstrumentationScope as _InstrumentationScope,
        )
    except Exception:  # noqa: BLE001 -- any import-surface break degrades, never raises
        return None
    return {
        "LogRecord": _ApiLogRecord,
        "SeverityNumber": _SeverityNumber,
        "ReadableLogRecord": _ReadableLogRecord,
        "LogRecordExportResult": _LogRecordExportResult,
        "Resource": _Resource,
        "InstrumentationScope": _InstrumentationScope,
        "OTLPLogExporter": _OTLPLogExporter,
    }


def dependency_available() -> bool:
    """True iff the optional OTel client library is importable AND exposes
    the API surface this module expects. Never raises."""
    return _import_otel_deps() is not None


# ---------------------------------------------------------------------------
# Config resolution — merges an optional per-project override
# (meridian.db.ai_log_export_config) with environment defaults. The bearer
# token/API key is ALWAYS environment-only — see module docstring.
# ---------------------------------------------------------------------------

def _generic_otlp_logs_endpoint() -> str:
    """Fall back to the standard ``OTEL_EXPORTER_OTLP_ENDPOINT`` (a base
    collector URL, no signal suffix) + ``/v1/logs``, for interop with an
    operator's existing generic OTel setup. Empty string if unset."""
    base = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    return f"{base.rstrip('/')}/v1/logs" if base else ""


def _parse_headers(raw: str) -> "dict[str, str]":
    """Parse an ``OTEL_EXPORTER_OTLP_HEADERS``-shaped string
    (``key1=value1,key2=value2``, percent-encoded values per the OTel env
    var spec) into a plain header dict. Never raises — a malformed pair is
    silently skipped rather than aborting the whole parse."""
    headers: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        key, _, value = pair.partition("=")
        key = key.strip()
        if not key:
            continue
        try:
            headers[key] = unquote(value.strip())
        except Exception:  # noqa: BLE001 -- a bad escape must not abort the parse
            headers[key] = value.strip()
    return headers


def _resolve_headers() -> "dict[str, str]":
    """ALWAYS environment-only — see module docstring's "No secrets in
    persisted config" contract. Meridian-specific var wins over the
    standard OTel one when both are set."""
    raw = os.environ.get(_ENV_HEADERS) or os.environ.get("OTEL_EXPORTER_OTLP_HEADERS") or ""
    return _parse_headers(raw)


def _effective_settings(
    project_cfg: "dict[str, Any] | None", *, batch_size_override: "int | None",
) -> "dict[str, Any]":
    """Merge a project's stored override row (may be ``None``) with
    environment defaults into one resolved settings dict. Pure function —
    no I/O, no side effects, safe to call from a read-only status check."""
    project_cfg = project_cfg or {}
    global_on = _global_enabled()
    project_enabled = project_cfg.get("enabled")  # 1 / 0 / None
    # A project can only NARROW (force off); it can never widen a globally
    # disabled feature to "on" for itself.
    effective_enabled = bool(global_on and project_enabled != 0)

    endpoint = (
        (project_cfg.get("otlp_endpoint") or "").strip()
        or os.environ.get(_ENV_ENDPOINT, "").strip()
        or os.environ.get("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT", "").strip()
        or _generic_otlp_logs_endpoint()
    )
    protocol = project_cfg.get("protocol") or "otlp_http"
    service_name = (
        (project_cfg.get("service_name") or "").strip()
        or os.environ.get(_ENV_SERVICE_NAME, "").strip()
        or os.environ.get("OTEL_SERVICE_NAME", "").strip()
        or "meridian"
    )
    langfuse_compat = bool(project_cfg.get("langfuse_compat")) or _env_truthy(_ENV_LANGFUSE_COMPAT)

    batch_size = batch_size_override if batch_size_override is not None else _env_int(
        _ENV_BATCH_SIZE, _DEFAULT_BATCH_SIZE,
    )
    batch_size = max(1, min(int(batch_size), _MAX_BATCH_SIZE))
    chunk_size = max(1, min(_env_int(_ENV_CHUNK_SIZE, _DEFAULT_CHUNK_SIZE), _MAX_CHUNK_SIZE, batch_size))
    max_retries = max(0, min(_env_int(_ENV_MAX_RETRIES, _DEFAULT_MAX_RETRIES), _MAX_MAX_RETRIES))
    backoff_base_s = max(0.05, min(_env_float(_ENV_BACKOFF_BASE_S, _DEFAULT_BACKOFF_BASE_S), 10.0))
    timeout_s = max(1.0, min(_env_float(_ENV_TIMEOUT_S, _DEFAULT_TIMEOUT_S), 30.0))

    return {
        "enabled": effective_enabled,
        "endpoint": endpoint or None,
        "protocol": protocol,
        "service_name": service_name,
        "langfuse_compat": langfuse_compat,
        "headers": _resolve_headers(),
        "batch_size": batch_size,
        "chunk_size": chunk_size,
        "max_retries": max_retries,
        "backoff_base_s": backoff_base_s,
        "timeout_s": timeout_s,
    }


def _total_deadline_s() -> float:
    return max(1.0, min(_env_float(_ENV_TOTAL_DEADLINE_S, _DEFAULT_TOTAL_DEADLINE_S), 60.0))


# ---------------------------------------------------------------------------
# Event -> OTLP log record conversion (redaction happens here, right before
# content would leave the local database boundary).
# ---------------------------------------------------------------------------

def _occurred_at_to_ns(occurred_at: "str | None") -> int:
    if occurred_at:
        try:
            dt = datetime.fromisoformat(occurred_at.replace("Z", "+00:00"))
            return int(dt.timestamp() * 1_000_000_000)
        except (ValueError, TypeError):
            pass
    return time.time_ns()


def _build_log_record(event: "dict[str, Any]", deps: "dict[str, Any]", resource: Any, scope: Any) -> Any:
    """One ``ai_log_events`` row -> one ``ReadableLogRecord``. Raises on a
    genuinely malformed event (caller wraps this per-chunk and degrades
    rather than losing the whole run over one bad row)."""
    body_obj = {
        "event_id": event.get("id"),
        "payload_schema": event.get("payload_schema"),
        "payload": event.get("payload") or {},
    }
    body_json = json.dumps(body_obj, sort_keys=True, default=str)
    # Defense-in-depth mask -- see module docstring's "Redaction, twice
    # over" contract. May leave body_json not-valid-JSON if a match is
    # masked mid-string; acceptable, this body is carried as free text and
    # never re-parsed by any Meridian code.
    body_json = redact(body_json)

    attributes = {
        "meridian.event_type": event.get("event_type"),
        "meridian.project_id": event.get("project_id"),
        "meridian.session_id": event.get("session_id"),
        "meridian.tenant_id": event.get("tenant_id"),
        "meridian.actor_kind": event.get("actor_kind"),
        "meridian.actor_id": event.get("actor_id"),
        "meridian.correlation_id": event.get("correlation_id"),
        "meridian.parent_event_id": event.get("parent_event_id"),
        "meridian.source": event.get("source"),
        "meridian.schema_version": event.get("schema_version"),
    }
    attributes = {k: v for k, v in attributes.items() if v is not None}

    api_rec = deps["LogRecord"](
        timestamp=_occurred_at_to_ns(event.get("occurred_at")),
        observed_timestamp=time.time_ns(),
        trace_id=0,
        span_id=0,
        trace_flags=0,
        severity_text="INFO",
        severity_number=deps["SeverityNumber"].INFO,
        body=body_json,
        attributes=attributes,
    )
    return deps["ReadableLogRecord"](
        log_record=api_rec, resource=resource, instrumentation_scope=scope,
    )


def _chunked(seq: list, size: int):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


# ---------------------------------------------------------------------------
# Send path: bounded retry/backoff, always off the event loop thread.
# ---------------------------------------------------------------------------

async def _send_chunk_with_retry(
    deps: "dict[str, Any]", exporter: Any, records: list, *,
    max_retries: int, backoff_base_s: float, timeout_s: float,
) -> "tuple[bool, str | None]":
    """Send one bounded chunk, retrying with exponential backoff + jitter up
    to ``max_retries`` times. Always runs the actual (synchronous) exporter
    call via ``asyncio.to_thread`` under a per-attempt ``asyncio.wait_for``
    so a hung collector can never block the caller's event loop — see
    module docstring. Never raises: any exception from the exporter itself
    (network error, timeout, an unexpected SDK surface break) is caught and
    treated as a failed attempt, identically to an explicit FAILURE result.
    """
    last_error: "str | None" = None
    attempt = 0
    while True:
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(exporter.export, records), timeout=timeout_s,
            )
            if result == deps["LogRecordExportResult"].SUCCESS:
                return True, None
            last_error = f"exporter reported {result!r}"
        except Exception as exc:  # noqa: BLE001 -- network/timeout/SDK errors must never propagate
            last_error = f"{type(exc).__name__}: {exc}"
        if attempt >= max_retries:
            return False, last_error
        backoff = backoff_base_s * (2 ** attempt) + random.uniform(0, backoff_base_s)
        await asyncio.sleep(min(backoff, 30.0))
        attempt += 1


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

async def get_export_status(db: Any, project_id: str) -> "dict[str, Any]":
    """Read-only diagnostics — NO network attempt, no lazy-import side
    effect beyond the (local, fast) dependency probe. Safe to call as often
    as a dashboard/status check wants."""
    if not project_id:
        raise ValueError("project_id is required")
    project_cfg = await cfg_db.get_ai_log_export_config(db, project_id)
    effective = _effective_settings(project_cfg, batch_size_override=None)
    return {
        "project_id": project_id,
        "global_feature_enabled": _global_enabled(),
        "effective_enabled": effective["enabled"],
        "dependency_available": dependency_available(),
        "endpoint_configured": bool(effective["endpoint"]),
        "protocol": effective["protocol"],
        "langfuse_compat": effective["langfuse_compat"],
        "service_name": effective["service_name"],
        "config": project_cfg,
    }


async def run_otel_export(
    db: Any, project_id: str, *, batch_size: "int | None" = None,
) -> "dict[str, Any]":
    """Run ONE bounded export pass for *project_id*: fetch new events since
    the durable watermark, send them (chunked, with retry/backoff), advance
    the watermark past every chunk that sent successfully. Never raises —
    see module docstring's "Never raises" contract; every branch below
    returns a structured ``{"project_id", "status", "sent_count", ...}``
    dict instead.

    ``status`` is one of: ``disabled`` (feature flag off), ``unavailable``
    (dependency missing or no endpoint configured — retrying without
    changing config won't help), ``idle`` (enabled and healthy, nothing new
    to send), ``sent`` (the whole fetched batch sent), ``degraded`` (part of
    the batch sent before a chunk exhausted its retries — the watermark
    still advanced past every chunk that DID send), ``sync_failed`` (the
    very first chunk failed — nothing sent this pass), or ``error`` (an
    unexpected exception was caught).
    """
    if not project_id:
        raise ValueError("project_id is required")
    try:
        return await asyncio.wait_for(
            _run_otel_export_inner(db, project_id, batch_size),
            timeout=_total_deadline_s(),
        )
    except asyncio.TimeoutError:
        logger.warning("ai_log_otel_export: overall deadline exceeded for project %s", project_id)
        try:
            await cfg_db.record_export_attempt(
                db, project_id, status="degraded",
                last_error="export deadline exceeded", bump_retry=True,
            )
        except Exception:  # noqa: BLE001 -- recording the failure must not itself raise
            logger.warning("ai_log_otel_export: failed to record deadline-exceeded state", exc_info=True)
        return {"project_id": project_id, "status": "degraded", "sent_count": 0, "reason": "export deadline exceeded"}
    except Exception as exc:  # noqa: BLE001 -- this function's whole "never raises" contract
        logger.warning("ai_log_otel_export: unexpected error for project %s", project_id, exc_info=True)
        # Defense in depth (same posture as _build_log_record's body
        # redaction): this is a catch-all for ANY exception anywhere in the
        # export path, including the actual OTLP HTTP call carrying
        # effective["headers"] -- some HTTP client/SDK error reprs echo
        # request details. Mask before this ever reaches the persisted,
        # project-shared last_error column or a caller-visible result dict.
        safe_reason = redact(str(exc))
        try:
            await cfg_db.record_export_attempt(db, project_id, status="error", last_error=safe_reason)
        except Exception:  # noqa: BLE001
            pass
        return {"project_id": project_id, "status": "error", "sent_count": 0, "reason": safe_reason}


async def _run_otel_export_inner(
    db: Any, project_id: str, batch_size_arg: "int | None",
) -> "dict[str, Any]":
    project_cfg = await cfg_db.get_ai_log_export_config(db, project_id)
    effective = _effective_settings(project_cfg, batch_size_override=batch_size_arg)

    if not effective["enabled"]:
        return {
            "project_id": project_id, "status": "disabled", "sent_count": 0,
            "reason": "AI-log OTel export is disabled (set MERIDIAN_AI_LOG_OTEL_ENABLED=1 to opt in)",
        }
    if not effective["endpoint"]:
        await cfg_db.record_export_attempt(
            db, project_id, status="unavailable", last_error="no OTLP endpoint configured",
        )
        return {
            "project_id": project_id, "status": "unavailable", "sent_count": 0,
            "reason": "no OTLP endpoint configured (MERIDIAN_AI_LOG_OTEL_ENDPOINT or a project override)",
        }

    deps = _import_otel_deps()
    if deps is None:
        await cfg_db.record_export_attempt(
            db, project_id, status="unavailable",
            last_error="opentelemetry OTel packages not installed or unsupported API surface",
        )
        return {
            "project_id": project_id, "status": "unavailable", "sent_count": 0,
            "reason": "opentelemetry OTel packages not installed -- pip install 'meridian-server[otel]'",
        }

    after_recorded_at = (project_cfg or {}).get("last_exported_recorded_at")
    after_event_ids = cfg_db.decode_exported_ids_at_watermark(
        (project_cfg or {}).get("last_exported_ids_at_watermark"),
    )
    events = await cfg_db.fetch_new_events_for_export(
        db, project_id,
        after_recorded_at=after_recorded_at, after_event_ids=after_event_ids,
        limit=effective["batch_size"],
    )
    if not events:
        await cfg_db.record_export_attempt(db, project_id, status="idle", reset_retry=True)
        return {"project_id": project_id, "status": "idle", "sent_count": 0, "reason": "no new events to export"}

    try:
        resource_attrs: dict[str, Any] = {"service.name": effective["service_name"]}
        if effective["langfuse_compat"]:
            resource_attrs["meridian.otel_sink_hint"] = "langfuse_otlp"
        resource = deps["Resource"].create(resource_attrs)
        scope = deps["InstrumentationScope"]("meridian.ai_log_otel_export")
        exporter = deps["OTLPLogExporter"](
            endpoint=effective["endpoint"],
            headers=effective["headers"] or None,
            timeout=effective["timeout_s"],
        )
    except Exception as exc:  # noqa: BLE001 -- construction failure is CATEGORICAL, not transient
        # Defense in depth: the exporter is constructed WITH
        # effective["headers"] (the bearer token/API key, read fresh from
        # env -- see module docstring). A construction-time validation error
        # is the closest point in this whole module to where that secret
        # value could end up echoed into an exception message. Mask before
        # it reaches the persisted, project-shared last_error column.
        safe_reason = redact(f"exporter construction failed: {exc}")
        await cfg_db.record_export_attempt(
            db, project_id, status="unavailable", last_error=safe_reason,
        )
        return {
            "project_id": project_id, "status": "unavailable", "sent_count": 0,
            "reason": safe_reason,
        }

    sent_count = 0
    last_ok_event: "dict[str, Any] | None" = None
    degraded_reason: "str | None" = None
    # Running "ids already sent at the current watermark recorded_at" set --
    # seeded from whatever was already durable (from a prior pass), then
    # grown (same recorded_at) or reset (a strictly later recorded_at) as
    # each chunk in THIS pass sends. See fetch_new_events_for_export's
    # docstring for why a single last-sent id can silently drop a same-
    # second sibling row and why this full-set tracking is required instead.
    watermark_recorded_at = after_recorded_at
    watermark_ids: list[str] = list(after_event_ids)
    for chunk in _chunked(events, effective["chunk_size"]):
        try:
            records = [_build_log_record(e, deps, resource, scope) for e in chunk]
        except Exception as exc:  # noqa: BLE001 -- a malformed row must not lose the whole run
            degraded_reason = redact(f"failed to build log record(s): {exc}")
            break
        ok, err = await _send_chunk_with_retry(
            deps, exporter, records,
            max_retries=effective["max_retries"],
            backoff_base_s=effective["backoff_base_s"],
            timeout_s=effective["timeout_s"],
        )
        if not ok:
            # Defense in depth: this is the ACTUAL OTLP HTTP call carrying
            # effective["headers"] (the bearer token/API key). A connection/
            # timeout/SDK error's message is the single most plausible place
            # for that value to be echoed back by an HTTP client's own error
            # repr -- mask before it reaches the persisted last_error column
            # or a caller-visible result dict (see _send_chunk_with_retry's
            # own "never raises" contract for why err is always plain text).
            degraded_reason = redact(err) if err is not None else err
            break
        sent_count += len(chunk)
        last_ok_event = chunk[-1]
        for _ev in chunk:
            _ev_recorded_at = _ev["recorded_at"]
            if _ev_recorded_at != watermark_recorded_at:
                watermark_recorded_at = _ev_recorded_at
                watermark_ids = [_ev["id"]]
            else:
                watermark_ids.append(_ev["id"])
        # Durable partial progress: advance the watermark after EVERY
        # successful chunk, not only once at the very end, so a crash or
        # deadline timeout between chunks can never re-send what already
        # went through.
        await cfg_db.record_export_success(
            db, project_id,
            last_exported_recorded_at=watermark_recorded_at,
            last_exported_event_id=last_ok_event["id"],
            exported_ids_at_watermark=watermark_ids,
            status="sending",
        )

    try:
        exporter.shutdown()
    except Exception:  # noqa: BLE001 -- best-effort cleanup only
        pass

    if degraded_reason is None:
        assert last_ok_event is not None  # events was non-empty and every chunk succeeded
        await cfg_db.record_export_success(
            db, project_id,
            last_exported_recorded_at=watermark_recorded_at,
            last_exported_event_id=last_ok_event["id"],
            exported_ids_at_watermark=watermark_ids,
            status="sent",
        )
        return {"project_id": project_id, "status": "sent", "sent_count": sent_count, "batch_size": len(events)}

    final_status = "degraded" if sent_count > 0 else "sync_failed"
    await cfg_db.record_export_attempt(
        db, project_id, status=final_status, last_error=degraded_reason, bump_retry=True,
    )
    return {
        "project_id": project_id, "status": final_status, "sent_count": sent_count,
        "batch_size": len(events), "reason": degraded_reason,
    }
