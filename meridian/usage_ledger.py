"""Host-neutral usage ledger, slice 1: transcript ingest and local report (824acdc6).

Deliberately narrow: no table, no migration, no MCP tool, no network, no DB.
It parses Claude Code transcript JSONL into usage records, dedupes them by API
message id, prices them through ``meridian.pricing``, and returns an aggregate
local report. The usage_events table, the Codex and OTel adapters, and
claim/complete budget enforcement are later slices.

Transcript facts encoded here (from the 2026 usage audit):

- one API message can appear on several JSONL lines, so dedupe by message.id;
- a repeated message keeps the per-field MAX of its lines, because transcript
  output_tokens is a lower bound and the largest line is the least under-counted;
- a sidechain line (isSidechain true) is a subagent request, otherwise main;
- cache writes split by TTL come from usage.cache_creation when present. When
  the split is absent, subagent writes are treated as 5m and main-loop writes
  as 1h, which is what the audit observed.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from meridian.pricing import PriceTable, cost_micros

TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_5m_tokens",
    "cache_write_1h_tokens",
)
UNKNOWN_MODEL = "unknown"


@dataclass(frozen=True)
class UsageRecord:
    """Token usage for one API message, before pricing."""

    message_id: str
    model: str
    query_source: str  # "main" or "subagent"
    session_id: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_5m_tokens: int = 0
    cache_write_1h_tokens: int = 0


@dataclass(frozen=True)
class ParseResult:
    """Records from a transcript, plus a count of lines that could not be read."""

    records: list[UsageRecord]
    malformed: int


class _MalformedLine(Exception):
    """Internal: a line carries usage data that cannot be trusted."""


def _token_count(source: Mapping[str, Any], key: str) -> int:
    """Missing or null counts as zero. Anything else must be a non-negative int.

    A string, bool, float or negative count is rejected instead of coerced,
    because a silently wrong token count would understate cost without any signal.
    """
    value = source.get(key)
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _MalformedLine(key)
    return value


def _record_from_obj(obj: Mapping[str, Any]) -> UsageRecord | None:
    """Return None for a line with no API usage. Raise _MalformedLine for bad usage."""
    message = obj.get("message")
    if not isinstance(message, Mapping):
        return None
    usage = message.get("usage")
    if usage is None:
        return None
    if not isinstance(usage, Mapping):
        raise _MalformedLine("usage")
    message_id = message.get("id")
    if not isinstance(message_id, str) or not message_id:
        raise _MalformedLine("message id")

    model = message.get("model")
    if not isinstance(model, str) or not model:
        model = UNKNOWN_MODEL
    query_source = "subagent" if obj.get("isSidechain") is True else "main"
    session_id = obj.get("sessionId")
    if not isinstance(session_id, str):
        session_id = ""

    input_tokens = _token_count(usage, "input_tokens")
    output_tokens = _token_count(usage, "output_tokens")
    cache_read = _token_count(usage, "cache_read_input_tokens")
    cache_write_total = _token_count(usage, "cache_creation_input_tokens")

    split = usage.get("cache_creation")
    if split is None:
        if query_source == "subagent":
            write_5m, write_1h = cache_write_total, 0
        else:
            write_5m, write_1h = 0, cache_write_total
    elif isinstance(split, Mapping):
        write_5m = _token_count(split, "ephemeral_5m_input_tokens")
        write_1h = _token_count(split, "ephemeral_1h_input_tokens")
        # An unsplit remainder of the total has no TTL bucket; it is treated as 5m.
        if write_5m + write_1h < cache_write_total:
            write_5m += cache_write_total - (write_5m + write_1h)
    else:
        raise _MalformedLine("cache_creation")

    return UsageRecord(
        message_id=message_id,
        model=model,
        query_source=query_source,
        session_id=session_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        cache_write_5m_tokens=write_5m,
        cache_write_1h_tokens=write_1h,
    )


def parse_claude_code_jsonl(lines: Iterable[str]) -> ParseResult:
    """Parse Claude Code transcript lines into usage records.

    Lines with no API usage (user turns, tool results) are skipped silently.
    Lines that are not JSON objects, or whose usage is malformed, are counted
    in ``malformed`` and never raised, so one bad line cannot hide the rest.
    """
    records: list[UsageRecord] = []
    malformed = 0
    for raw in lines:
        text = raw.strip()
        if not text:
            continue
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            malformed += 1
            continue
        if not isinstance(obj, Mapping):
            malformed += 1
            continue
        try:
            record = _record_from_obj(obj)
        except _MalformedLine:
            malformed += 1
            continue
        if record is not None:
            records.append(record)
    return ParseResult(records=records, malformed=malformed)


def _merge(prior: UsageRecord, later: UsageRecord) -> UsageRecord:
    return replace(
        prior,
        model=prior.model if prior.model != UNKNOWN_MODEL else later.model,
        query_source=(
            "subagent" if "subagent" in (prior.query_source, later.query_source) else "main"
        ),
        session_id=prior.session_id or later.session_id,
        input_tokens=max(prior.input_tokens, later.input_tokens),
        output_tokens=max(prior.output_tokens, later.output_tokens),
        cache_read_tokens=max(prior.cache_read_tokens, later.cache_read_tokens),
        cache_write_5m_tokens=max(prior.cache_write_5m_tokens, later.cache_write_5m_tokens),
        cache_write_1h_tokens=max(prior.cache_write_1h_tokens, later.cache_write_1h_tokens),
    )


def dedupe_by_message_id(records: Iterable[UsageRecord]) -> list[UsageRecord]:
    """One record per API message id, in first-seen order, per-field max across lines."""
    merged: dict[str, UsageRecord] = {}
    for record in records:
        prior = merged.get(record.message_id)
        merged[record.message_id] = record if prior is None else _merge(prior, record)
    return list(merged.values())


def build_local_report(
    records: Iterable[UsageRecord],
    table: PriceTable,
    *,
    malformed_lines: int = 0,
) -> dict[str, Any]:
    """Aggregate deduped usage by (model, query_source) with a cost per group.

    A group whose messages include an unpriced model reports cost_micros None
    rather than a partial sum. ``cost_complete`` is True only when every message
    priced, so the total is never presented as complete when it is not.
    """
    deduped = dedupe_by_message_id(records)
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    total_micros = 0
    unpriced_total = 0
    for record in deduped:
        key = (record.model, record.query_source)
        group = groups.get(key)
        if group is None:
            group = {
                "model": record.model,
                "query_source": record.query_source,
                "messages": 0,
                **{name: 0 for name in TOKEN_FIELDS},
                "cost_micros": 0,
                "unpriced_messages": 0,
            }
            groups[key] = group
        group["messages"] += 1
        for name in TOKEN_FIELDS:
            group[name] += getattr(record, name)
        priced = cost_micros(record, table)
        if priced is None:
            group["unpriced_messages"] += 1
            unpriced_total += 1
        else:
            group["cost_micros"] += priced
            total_micros += priced

    rows = []
    for key in sorted(groups):
        group = groups[key]
        if group["unpriced_messages"]:
            group["cost_micros"] = None
        rows.append(group)

    return {
        "price_table_version": table.version,
        "messages": len(deduped),
        "malformed_lines": malformed_lines,
        "unpriced_messages": unpriced_total,
        "cost_complete": unpriced_total == 0,
        "total_cost_micros": total_micros,
        "by_model_and_source": rows,
    }
