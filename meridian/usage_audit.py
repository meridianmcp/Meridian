"""fe3b44b6 -- Transcript cost-audit tool.

Parses a Claude Code session's on-disk transcript -- the main session's own
JSONL plus any subagent transcripts under ``subagents/**/agent-*.jsonl`` --
into a structured, PRICED usage report: main-loop vs. subagent-loop cost
split, prompt-read/cache-write/cache-read/output token accounting, cost per
workflow phase and per subagent label, and a handful of cost-hygiene
heuristics (startup-prefix tax, cold-cache rewrites, thinking-token share,
effort-level distribution).

This productizes a set of throwaway analysis scripts from the 2026-09-29
usage audit (Meridian note 7f27033d-5ff4-42af-bac1-765e74f066e8) into a real,
tested module. Nothing here talks to a live API or a database -- it is a
pure, offline transform: directory-of-JSONL in, :class:`UsageAuditReport` out.

HONESTY / SCOPE NOTES -- read before trusting a number from this module
-------------------------------------------------------------------------
* ``output_tokens`` summed from a transcript is a LOWER BOUND on what was
  actually billed (see :data:`MANDATORY_LOWER_BOUND_CAVEAT`, always present
  in ``UsageAuditReport.caveats``). A transcript file can be truncated
  (process killed mid-response), missing rotated/archived segments, or
  simply never durably flushed for the last request.
* Thinking-token share is EXACT for every turn whose transcript records
  ``usage.output_tokens_details.thinking_tokens``. For any other turn it is an
  ESTIMATE: the CHARACTER-length proportion of that turn's
  ``thinking``/``redacted_thinking`` content blocks against all its content
  blocks, applied to ``output_tokens`` -- a proxy for token count, not exact.
* Reconciliation against a cost-state/usage source: as of this writing
  (fe3b44b6, 2026-09-29) there is NO wired-up usage/cost persistence
  anywhere in this repo to automatically reconcile against.
  ``meridian/ai_log.py`` + ``meridian/db/ai_log.py`` define the
  ``ExecutionEvent`` / ``ai_log_events`` CONTRACT and STORAGE SCAFFOLD only
  -- per that module's own docstring, "nothing in the running server calls
  append_event today" (no route, no MCP tool, no capture pipeline is wired
  up). ``meridian/routes/billing.py`` is Stripe account/customer billing,
  unrelated to per-token usage. Reconciliation here is therefore an
  OPTIONAL, caller-supplied hook (``reconcile_against=``) rather than an
  automatic DB read; when omitted, :meth:`UsageAuditReport.reconciliation`
  says so explicitly instead of silently claiming "reconciled: ok".

TRANSCRIPT SCHEMA ASSUMED
--------------------------
One JSON object per line. Only lines with ``"type": "assistant"`` and a
``message.usage`` dict are priced (one such line == one billed API turn).
Fields read: ``timestamp`` (ISO-8601), ``message.id``, ``message.model``,
``message.content`` (list of content blocks: ``text``, ``thinking`` /
``redacted_thinking``, ``tool_use``, ``server_tool_use``), and
``message.usage`` with ``input_tokens``, ``cache_read_input_tokens``,
``output_tokens``, an optional nested ``cache_creation`` dict with
``ephemeral_5m_input_tokens`` / ``ephemeral_1h_input_tokens`` (falling back
to a flat ``cache_creation_input_tokens`` attributed entirely to the 5-minute
tier when the nested shape is absent -- see :func:`_build_usage_record`),
and an optional ``server_tool_use.web_search_requests`` count. This mirrors
the same transcript-usage fields ``meridian/guard_core.py``'s G17 cold-cache
guard already reads.

PHASE ATTRIBUTION
------------------
A raw transcript has no "workflow phase" field of its own. This module
derives phases in the MAIN loop only, from ``mark_chapter`` tool_use calls
(the same ``mcp__ccd_session__mark_chapter`` tool this codebase's own
sessions use to mark chapters) -- whichever chapter title was most recently
marked applies to every record from that point forward; records before the
first chapter marker fall under ``"(session start)"``. Only the short
``title`` argument is ever read out of that tool call; no other tool input,
and no text/thinking content, is inspected or stored (see "NO SECRETS"
below).

AGENT LABEL ATTRIBUTION
------------------------
A subagent transcript's label is the first path component under
``subagents/`` (e.g. ``subagents/<label>/agent-1.jsonl`` -> ``<label>``) --
structural metadata (a directory name), never message content.

EFFORT LEVEL
------------
There is no single confirmed, stable field name for a request's reasoning
effort in a Claude Code transcript as of this writing. :func:`_extract_effort`
checks a short list of plausible locations (``message.effort``,
``message.reasoning_effort``, and the same two keys at the top level of the
record) and falls back to ``"unknown"`` -- this is intentionally
forward-compatible/best-effort, not a documented guarantee.

NO SECRETS IN OUTPUT
----------------------
This module only ever extracts and reports: token counts, costs, models,
timestamps, request counts, subagent directory-derived labels, and
``mark_chapter`` phase titles. It never reads, stores, or serializes
message ``text``/``thinking`` content, tool_use ``input`` bodies (beyond
``mark_chapter``'s own ``title``), or tool_result content. See
``tests/test_sprint_fe3b44b6_usage_audit.py::test_report_never_leaks_message_content`` for
an automated check of this property.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


class UsageAuditError(RuntimeError):
    """Raised for a structurally invalid INPUT the caller must fix (a missing
    or ambiguous main transcript, a non-existent session dir, an unknown
    price-table version). Never raised for a single malformed JSONL line or
    an unrecognized model -- those are recorded as diagnostics/warnings and
    skipped instead, matching this repo's fail-open convention for
    best-effort transcript parsing (see ``meridian/guard_core.py``)."""


# ---------------------------------------------------------------------------
# Versioned, explicit price table
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelRates:
    """USD rates per 1,000,000 tokens for one model, at one price-table
    version. All five categories the report prices are named explicitly --
    no category is ever left to be "inferred" from another at price-lookup
    time (that computation, where it applies, happens once, at TABLE
    AUTHORING time -- see :data:`_PRICE_TABLE_2026_01_15` for the documented
    multipliers used to derive these)."""

    prompt_read_per_mtok: float
    cache_write_5m_per_mtok: float
    cache_write_1h_per_mtok: float
    cache_read_per_mtok: float
    output_per_mtok: float


@dataclass(frozen=True)
class PriceTable:
    """One named, versioned snapshot of model rates + the flat web-search
    rate. Never mutated after registration -- see :func:`_register_price_table`."""

    version: str
    effective_date: str
    source: str
    models: Mapping[str, ModelRates]
    web_search_per_1k_searches: float
    verified: bool = False


#: version -> PriceTable. Populated by :func:`_register_price_table` below.
#: Add a NEW entry (never edit an existing one in place) when prices change,
#: so a report generated against an OLD transcript can still be re-priced
#: against the table that was actually in effect at the time.
PRICE_TABLES: dict[str, PriceTable] = {}


def _register_price_table(table: PriceTable) -> None:
    if table.version in PRICE_TABLES:
        raise UsageAuditError(f"price table version {table.version!r} already registered")
    PRICE_TABLES[table.version] = table


_register_price_table(
    PriceTable(
        version="2026-01-15.1",
        effective_date="2026-01-15",
        source=(
            "UNVERIFIED carry-over: manually recorded snapshot of Anthropic's published API pricing "
            "(https://www.anthropic.com/pricing), USD per 1,000,000 tokens. "
            "Cache-write premiums follow Anthropic's documented prompt-caching "
            "multipliers relative to each model's base (uncached) input price: "
            "5-minute TTL write = 1.25x, 1-hour TTL write = 2.0x, cache read = "
            "0.1x. This is a point-in-time snapshot, not a live lookup -- "
            "re-verify against the pricing page before using it for real "
            "invoicing, and register a NEW versioned table below rather than "
            "editing this one in place when prices change."
        ),
        models={
            # model_id: ModelRates(prompt_read, cache_write_5m, cache_write_1h, cache_read, output)
            "claude-opus-5": ModelRates(15.00, 18.75, 30.00, 1.50, 75.00),
            "claude-sonnet-5": ModelRates(3.00, 3.75, 6.00, 0.30, 15.00),
            "claude-haiku-5": ModelRates(0.80, 1.00, 1.60, 0.08, 4.00),
            # Legacy aliases kept so a transcript recorded before the 5-series
            # (or one testing against an older deployed model) still prices.
            "claude-opus-4": ModelRates(15.00, 18.75, 30.00, 1.50, 75.00),
            "claude-sonnet-4": ModelRates(3.00, 3.75, 6.00, 0.30, 15.00),
            "claude-haiku-4": ModelRates(0.80, 1.00, 1.60, 0.08, 4.00),
        },
        web_search_per_1k_searches=10.00,
        verified=False,
    )
)

#: The price table version used when the caller doesn't name one explicitly.
CURRENT_PRICE_TABLE_VERSION = "2026-01-15.1"


def get_price_table(version: "str | None" = None) -> PriceTable:
    """Look up a registered :class:`PriceTable` by version, defaulting to
    :data:`CURRENT_PRICE_TABLE_VERSION`. Raises :class:`UsageAuditError` for
    an unknown version -- never silently falls back to a different table."""
    v = version or CURRENT_PRICE_TABLE_VERSION
    try:
        return PRICE_TABLES[v]
    except KeyError:
        raise UsageAuditError(
            f"unknown price_table_version {v!r}; known versions: {sorted(PRICE_TABLES)}"
        ) from None


#: A model id that carries a trailing snapshot-date suffix, e.g.
#: "claude-sonnet-5-20260115" or "claude-sonnet-5-20260115-v2".
_DATE_SUFFIX_RE = re.compile(r"-\d{8}(?:-[a-z0-9]+)*$")


def resolve_model_key(model: "str | None", table: PriceTable) -> "str | None":
    """Resolve a raw transcript model id to a key in ``table.models``, or
    ``None`` if this price table has no rates for it. Tries, in order: an
    exact match; the id with a trailing ``-YYYYMMDD[...]`` snapshot-date
    suffix stripped; the longest registered key that the id starts with
    (covers an unanticipated suffix format without guessing at unrelated
    models)."""
    if not model:
        return None
    if model in table.models:
        return model
    stripped = _DATE_SUFFIX_RE.sub("", model)
    if stripped in table.models:
        return stripped
    candidates = [k for k in table.models if model.startswith(k)]
    if candidates:
        return max(candidates, key=len)
    return None


# ---------------------------------------------------------------------------
# Parsed records
# ---------------------------------------------------------------------------

#: Cache TTLs used by the cold-cache-rewrite heuristic (see
#: :func:`_compute_cold_cache_events`).
CACHE_TTL_5M_SECONDS = 5 * 60
CACHE_TTL_1H_SECONDS = 60 * 60

#: A mark_chapter title is capped to this length before being used as a
#: phase label -- keeps a pathological chapter title from ballooning the
#: report, and mirrors mark_chapter's own "under 40 chars" guidance with
#: headroom.
MAX_PHASE_LABEL_LEN = 80

#: Assumed character weight for a redacted thinking block (its real text is
#: never present in the transcript to measure) when estimating thinking
#: share -- see :func:`_scan_content_blocks`.
_REDACTED_THINKING_CHAR_ESTIMATE = 200

#: Rough per-tool_use-block character overhead (beyond its name) used only
#: to keep the thinking-share denominator from being skewed to ~0 by a
#: turn that is a single, low-text tool call. Deliberately coarse -- see
#: :func:`_scan_content_blocks`'s docstring for why raw tool input is never
#: read for this.
_TOOL_USE_CHAR_OVERHEAD = 40


@dataclass
class UsageRecord:
    """One priced assistant turn, from either the main transcript or a
    subagent transcript."""

    message_id: "str | None"
    source: str  # "main" | "subagent"
    agent_label: "str | None"
    file_path: str
    line_no: int
    timestamp: "float | None"
    model: "str | None"
    input_tokens: int
    cache_write_5m_tokens: int
    cache_write_1h_tokens: int
    cache_read_tokens: int
    output_tokens: int
    web_search_requests: int
    thinking_chars: int
    total_content_chars: int
    effort: str
    chapter_title: "str | None"
    phase: "str | None" = None
    thinking_tokens: "int | None" = None
    subagent_phase: "str | None" = None


@dataclass
class ParseDiagnostics:
    """Structural (never content-bearing) stats about parsing one transcript
    file -- how many lines were blank/malformed/non-priced. Lets a caller
    sanity-check whether a file looks intact without seeing its content."""

    file_path: str
    lines_total: int = 0
    lines_blank: int = 0
    lines_malformed_json: int = 0
    assistant_lines: int = 0
    assistant_lines_without_usage: int = 0
    priced_records: int = 0


def _parse_iso_ts(value: Any) -> "float | None":
    """Epoch seconds from an ISO-8601 transcript timestamp, or a bare epoch
    number. Returns ``None`` for anything else -- never raises."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, str) or not value:
        return None
    s = value.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


def _num(value: Any) -> int:
    """``int(value)`` for a real (non-bool) number, else 0. Never raises."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    return 0


def _scan_content_blocks(content: Any) -> "tuple[int, int, int, str | None]":
    """Walk one assistant turn's ``message.content`` blocks and return
    ``(thinking_chars, other_chars, web_search_count, chapter_title)``.

    Deliberately reads ONLY: the length of thinking text (never the text
    itself), the length of visible text (never the text itself), a
    tool_use block's ``name`` (never its ``input``) -- with exactly ONE
    documented exception: a ``mark_chapter``-named tool_use's ``input.title``
    is read out (and length-capped) specifically to build the phase label
    this report exists to show. No other tool input, and no tool_result
    content, is ever inspected here.
    """
    thinking_chars = 0
    other_chars = 0
    web_search_count = 0
    chapter_title: "str | None" = None
    if not isinstance(content, list):
        return 0, 0, 0, None
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype in ("thinking", "redacted_thinking"):
            text = block.get("thinking")
            thinking_chars += len(text) if isinstance(text, str) else _REDACTED_THINKING_CHAR_ESTIMATE
        elif btype == "text":
            text = block.get("text")
            other_chars += len(text) if isinstance(text, str) else 0
        elif btype == "tool_use":
            name = block.get("name") if isinstance(block.get("name"), str) else ""
            other_chars += len(name) + _TOOL_USE_CHAR_OVERHEAD
            if "mark_chapter" in name.lower():
                title = None
                inp = block.get("input")
                if isinstance(inp, dict):
                    raw_title = inp.get("title")
                    if isinstance(raw_title, str) and raw_title.strip():
                        title = raw_title.strip()[:MAX_PHASE_LABEL_LEN]
                chapter_title = title or "(unnamed chapter)"
        elif btype == "server_tool_use":
            if block.get("name") == "web_search":
                web_search_count += 1
    return thinking_chars, other_chars, web_search_count, chapter_title


def _extract_effort(record_obj: dict, message: dict) -> str:
    """Best-effort reasoning-effort label for one turn -- see the module
    docstring's EFFORT LEVEL section for why this checks several plausible
    locations instead of one confirmed field."""
    for container, key in (
        (message, "effort"),
        (message, "reasoning_effort"),
        (record_obj, "perTurnEffort"),
        (record_obj, "effort"),
        (record_obj, "reasoning_effort"),
    ):
        if not isinstance(container, dict):
            continue
        val = container.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip().lower()
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            return str(val)
    return "unknown"


def _build_usage_record(
    obj: dict,
    message: dict,
    usage: dict,
    *,
    source: str,
    agent_label: "str | None",
    file_path: str,
    line_no: int,
    subagent_phase: "str | None" = None,
) -> UsageRecord:
    input_tokens = _num(usage.get("input_tokens"))
    cache_read = _num(usage.get("cache_read_input_tokens"))
    output_tokens = _num(usage.get("output_tokens"))
    cache_creation = usage.get("cache_creation")
    if isinstance(cache_creation, dict):
        cache_write_5m = _num(cache_creation.get("ephemeral_5m_input_tokens"))
        cache_write_1h = _num(cache_creation.get("ephemeral_1h_input_tokens"))
    else:
        # Flat legacy shape: a single cache_creation_input_tokens number with
        # no TTL breakdown (this is also all meridian/guard_core.py's G17
        # reader ever looks at). Attributed entirely to the 5-minute tier,
        # the default when a cache_control block doesn't request 1h -- this
        # can understate 1h-tier writes for a transcript that used 1h
        # caching but only ever emitted the flat field.
        cache_write_5m = _num(usage.get("cache_creation_input_tokens"))
        cache_write_1h = 0
    web_search = 0
    server_tool_use = usage.get("server_tool_use")
    if isinstance(server_tool_use, dict):
        web_search = _num(server_tool_use.get("web_search_requests"))
    thinking_chars, other_chars, content_web_search, chapter_title = _scan_content_blocks(
        message.get("content")
    )
    web_search = max(web_search, content_web_search)
    message_id = message.get("id") if isinstance(message.get("id"), str) else None
    model = message.get("model") if isinstance(message.get("model"), str) else None
    details = usage.get("output_tokens_details")
    thinking_tokens: "int | None" = None
    if isinstance(details, dict):
        raw_thinking = details.get("thinking_tokens")
        if isinstance(raw_thinking, (int, float)) and not isinstance(raw_thinking, bool):
            thinking_tokens = _num(raw_thinking)
    return UsageRecord(
        message_id=message_id,
        source=source,
        agent_label=agent_label,
        file_path=file_path,
        line_no=line_no,
        timestamp=_parse_iso_ts(obj.get("timestamp")),
        model=model,
        input_tokens=input_tokens,
        cache_write_5m_tokens=cache_write_5m,
        cache_write_1h_tokens=cache_write_1h,
        cache_read_tokens=cache_read,
        output_tokens=output_tokens,
        web_search_requests=web_search,
        thinking_chars=thinking_chars,
        total_content_chars=thinking_chars + other_chars,
        effort=_extract_effort(obj, message),
        chapter_title=chapter_title,
        thinking_tokens=thinking_tokens,
        subagent_phase=subagent_phase,
    )


def _parse_transcript_file(
    path: Path,
    *,
    source: str,
    agent_label: "str | None",
    subagent_phase: "str | None" = None,
) -> "tuple[list[UsageRecord], ParseDiagnostics]":
    diag = ParseDiagnostics(file_path=str(path))
    records: list[UsageRecord] = []
    try:
        # newline="" keeps any "\r" so it is stripped per line below, instead of
        # being folded into "\n" by universal-newline translation.
        with path.open("r", encoding="utf-8", errors="replace", newline="") as fh:
            text = fh.read()
    except OSError as exc:
        raise UsageAuditError(f"could not read transcript file {path}: {exc}") from exc
    # JSONL is delimited by "\n" only. str.splitlines() also breaks on U+2028,
    # U+2029, U+0085 and similar separators, which may appear unescaped inside a
    # valid JSON string and would split one record into two malformed fragments.
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()  # the text after the final newline is not a line
    diag.lines_total = len(lines)
    for line_no, raw in enumerate(lines, start=1):
        line = raw.strip()  # also drops the trailing "\r" of CRLF endings
        if not line:
            diag.lines_blank += 1
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            diag.lines_malformed_json += 1
            continue
        if not isinstance(obj, dict) or obj.get("type") != "assistant":
            continue
        message = obj.get("message")
        if not isinstance(message, dict):
            continue
        diag.assistant_lines += 1
        usage = message.get("usage")
        if not isinstance(usage, dict):
            diag.assistant_lines_without_usage += 1
            continue
        records.append(
            _build_usage_record(
                obj, message, usage, source=source, agent_label=agent_label,
                file_path=str(path), line_no=line_no, subagent_phase=subagent_phase,
            )
        )
        diag.priced_records += 1
    return records, diag


def _merge_duplicate(kept: UsageRecord, dup: UsageRecord) -> None:
    """Fold a repeated ``message.id`` line into the record already kept for it.

    Claude Code writes one JSONL line per content block of an assistant
    message, and the same message id (with its usage block) can recur on
    several lines. Usage counters only grow within one message, so each
    counter takes the LARGER of the two values -- the same rule the 2026-09-29
    throwaway audit scripts used (keep the usage block with the greatest
    output_tokens). Keeping the first-seen line would under-count output
    tokens for streamed messages. Content character counts are summed because
    each line carries a distinct block. Identity fields (timestamp, line
    number, file) stay with the first line.
    """
    kept.input_tokens = max(kept.input_tokens, dup.input_tokens)
    kept.cache_write_5m_tokens = max(kept.cache_write_5m_tokens, dup.cache_write_5m_tokens)
    kept.cache_write_1h_tokens = max(kept.cache_write_1h_tokens, dup.cache_write_1h_tokens)
    kept.cache_read_tokens = max(kept.cache_read_tokens, dup.cache_read_tokens)
    kept.output_tokens = max(kept.output_tokens, dup.output_tokens)
    kept.web_search_requests = max(kept.web_search_requests, dup.web_search_requests)
    if dup.thinking_tokens is not None:
        kept.thinking_tokens = max(kept.thinking_tokens or 0, dup.thinking_tokens)
    kept.thinking_chars += dup.thinking_chars
    kept.total_content_chars += dup.total_content_chars
    if kept.chapter_title is None:
        kept.chapter_title = dup.chapter_title
    if kept.effort == "unknown":
        kept.effort = dup.effort
    if kept.model is None:
        kept.model = dup.model


def _dedupe(records: list[UsageRecord]) -> "tuple[list[UsageRecord], int]":
    """Fold every repeated ``message_id`` into its first occurrence (see
    :func:`_merge_duplicate`). A record with no ``message_id`` is never
    treated as a duplicate of anything -- there's nothing to dedupe it
    against. Returns ``(unique_records, duplicate_lines_folded)``."""
    kept_by_id: dict[str, UsageRecord] = {}
    out: list[UsageRecord] = []
    dup_count = 0
    for r in records:
        if r.message_id:
            kept = kept_by_id.get(r.message_id)
            if kept is not None:
                _merge_duplicate(kept, r)
                dup_count += 1
                continue
            kept_by_id[r.message_id] = r
        out.append(r)
    return out, dup_count


def _assign_phases(main_records: list[UsageRecord]) -> None:
    """In file order, apply the most-recently-seen chapter title (from a
    mark_chapter tool_use) to every record from that point forward."""
    current = "(session start)"
    for r in main_records:
        if r.chapter_title:
            current = r.chapter_title
        r.phase = current


# ---------------------------------------------------------------------------
# Discovery: main transcript + subagent transcripts under a session dir
# ---------------------------------------------------------------------------


def discover_transcript_files(
    session_dir: Path, main_transcript_path: "Path | None" = None
) -> "tuple[Path, list[Path]]":
    """Return ``(main_transcript_path, subagent_transcript_paths)`` for a
    session directory.

    The MAIN transcript is either ``main_transcript_path`` (validated to
    exist) or, when omitted, the sole top-level ``*.jsonl`` file directly in
    ``session_dir`` -- zero or more-than-one such file is a hard error
    (:class:`UsageAuditError`) asking the caller to disambiguate explicitly,
    rather than silently guessing. Subagent transcripts are every file
    matching ``subagents/**/agent-*.jsonl`` under ``session_dir``, sorted for
    deterministic output.
    """
    session_dir = Path(session_dir)
    if not session_dir.is_dir():
        raise UsageAuditError(f"session_dir does not exist or is not a directory: {session_dir}")
    if main_transcript_path is not None:
        main = Path(main_transcript_path)
        if not main.is_file():
            raise UsageAuditError(f"main_transcript_path does not exist: {main}")
    else:
        candidates = sorted(p for p in session_dir.glob("*.jsonl") if p.is_file())
        if not candidates:
            raise UsageAuditError(
                f"no top-level *.jsonl transcript found in {session_dir}; "
                "pass main_transcript_path explicitly"
            )
        if len(candidates) > 1:
            names = ", ".join(p.name for p in candidates)
            raise UsageAuditError(
                f"ambiguous: {len(candidates)} top-level *.jsonl files in {session_dir} "
                f"({names}); pass main_transcript_path explicitly"
            )
        main = candidates[0]
    subagent_files = sorted(session_dir.glob("subagents/**/agent-*.jsonl"))
    return main, subagent_files


def _agent_label_for(path: Path, session_dir: Path) -> str:
    """The first path component under ``<session_dir>/subagents/`` -- a
    directory name, never message content."""
    try:
        rel = path.relative_to(session_dir / "subagents")
    except ValueError:
        return path.stem
    parts = rel.parts
    if len(parts) >= 2:
        return parts[0]
    return path.stem


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------


@dataclass
class PricedRecord:
    rates_key: "str | None"
    input_cost: float
    cache_write_5m_cost: float
    cache_write_1h_cost: float
    cache_read_cost: float
    output_cost: float
    web_search_cost: float


def price_record(record: UsageRecord, table: PriceTable) -> PricedRecord:
    """Price one record against ``table``. A model this table has no rates
    for prices as all-zero (tokens are still counted correctly elsewhere --
    only its COST is unknown); the caller is expected to surface that via
    ``UsageAuditReport.unpriced_models``."""
    key = resolve_model_key(record.model, table)
    rates = table.models.get(key) if key else None
    if rates is None:
        return PricedRecord(key, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    mtok = 1_000_000.0
    return PricedRecord(
        rates_key=key,
        input_cost=record.input_tokens / mtok * rates.prompt_read_per_mtok,
        cache_write_5m_cost=record.cache_write_5m_tokens / mtok * rates.cache_write_5m_per_mtok,
        cache_write_1h_cost=record.cache_write_1h_tokens / mtok * rates.cache_write_1h_per_mtok,
        cache_read_cost=record.cache_read_tokens / mtok * rates.cache_read_per_mtok,
        output_cost=record.output_tokens / mtok * rates.output_per_mtok,
        web_search_cost=record.web_search_requests / 1000.0 * table.web_search_per_1k_searches,
    )


@dataclass
class TokenCostBreakdown:
    """Token counts + USD costs for one slice of the report (totals, a
    source, a phase, or an agent)."""

    request_count: int = 0
    input_tokens: int = 0
    cache_write_5m_tokens: int = 0
    cache_write_1h_tokens: int = 0
    cache_read_tokens: int = 0
    output_tokens: int = 0
    web_search_requests: int = 0
    input_cost: float = 0.0
    cache_write_5m_cost: float = 0.0
    cache_write_1h_cost: float = 0.0
    cache_read_cost: float = 0.0
    output_cost: float = 0.0
    web_search_cost: float = 0.0
    total_tokens: int = 0
    total_cost: float = 0.0


def _apply(breakdown: TokenCostBreakdown, record: UsageRecord, priced: PricedRecord) -> None:
    breakdown.request_count += 1
    breakdown.input_tokens += record.input_tokens
    breakdown.cache_write_5m_tokens += record.cache_write_5m_tokens
    breakdown.cache_write_1h_tokens += record.cache_write_1h_tokens
    breakdown.cache_read_tokens += record.cache_read_tokens
    breakdown.output_tokens += record.output_tokens
    breakdown.web_search_requests += record.web_search_requests
    breakdown.input_cost += priced.input_cost
    breakdown.cache_write_5m_cost += priced.cache_write_5m_cost
    breakdown.cache_write_1h_cost += priced.cache_write_1h_cost
    breakdown.cache_read_cost += priced.cache_read_cost
    breakdown.output_cost += priced.output_cost
    breakdown.web_search_cost += priced.web_search_cost


def _finalize(breakdown: TokenCostBreakdown) -> None:
    breakdown.total_tokens = (
        breakdown.input_tokens
        + breakdown.cache_write_5m_tokens
        + breakdown.cache_write_1h_tokens
        + breakdown.cache_read_tokens
        + breakdown.output_tokens
    )
    breakdown.total_cost = (
        breakdown.input_cost
        + breakdown.cache_write_5m_cost
        + breakdown.cache_write_1h_cost
        + breakdown.cache_read_cost
        + breakdown.output_cost
        + breakdown.web_search_cost
    )


# ---------------------------------------------------------------------------
# Cost-hygiene heuristics: startup-prefix tax, cold-cache rewrites,
# thinking-token share, effort distribution
# ---------------------------------------------------------------------------


@dataclass
class PrefixTaxEntry:
    """Estimated cost of repeatedly paying for the same startup context, one
    entry per "loop" (the main transcript, or one subagent transcript file).
    ``tax_tokens = first_request_context_tokens * request_count`` -- the
    literal formula this feature was specified with: what it would cost, in
    tokens, to send the first request's full context on EVERY request in
    this loop (an upper-bound hypothetical, not what was actually billed --
    real caching means most of those tokens are usually billed at the far
    cheaper cache-read rate instead)."""

    loop: str
    first_request_context_tokens: int
    request_count: int
    tax_tokens: int
    estimated_tax_cost: float
    priced_model: "str | None"


def _group_by_loop(
    main_records: list[UsageRecord], subagent_records: list[UsageRecord]
) -> "dict[str, list[UsageRecord]]":
    """One "loop" per independent request stream: the whole main transcript,
    and each subagent transcript FILE (not each label -- two files under the
    same agent label are two separate loops)."""
    loops: "dict[str, list[UsageRecord]]" = {}
    if main_records:
        loops["main"] = main_records
    by_file: "dict[str, list[UsageRecord]]" = {}
    for r in subagent_records:
        by_file.setdefault(r.file_path, []).append(r)
    for file_path, recs in by_file.items():
        label = recs[0].agent_label or "unknown"
        loops[f"subagent:{label}:{Path(file_path).name}"] = recs
    return loops


def _compute_prefix_tax(
    loops: "dict[str, list[UsageRecord]]", table: PriceTable
) -> list[PrefixTaxEntry]:
    entries: list[PrefixTaxEntry] = []
    for loop_name, recs in loops.items():
        if not recs:
            continue
        first = recs[0]
        context_tokens = (
            first.input_tokens
            + first.cache_write_5m_tokens
            + first.cache_write_1h_tokens
            + first.cache_read_tokens
        )
        n = len(recs)
        tax_tokens = context_tokens * n
        key = resolve_model_key(first.model, table)
        rates = table.models.get(key) if key else None
        cost = (tax_tokens / 1_000_000.0) * rates.prompt_read_per_mtok if rates else 0.0
        entries.append(
            PrefixTaxEntry(
                loop=loop_name,
                first_request_context_tokens=context_tokens,
                request_count=n,
                tax_tokens=tax_tokens,
                estimated_tax_cost=cost,
                priced_model=key,
            )
        )
    return entries


@dataclass
class ColdCacheEvent:
    """One suspected cache-rewrite: the gap since the previous request in
    this loop exceeded the applicable cache TTL, and/or the model changed."""

    loop: str
    after_line: int
    gap_seconds: "float | None"
    reason: str  # "ttl_expired" | "model_switch" | "ttl_expired+model_switch"
    tokens_rewritten: int
    estimated_cost: float


def _compute_cold_cache_events(
    loops: "dict[str, list[UsageRecord]]", table: PriceTable
) -> list[ColdCacheEvent]:
    events: list[ColdCacheEvent] = []
    for loop_name, recs in loops.items():
        for prev, curr in zip(recs, recs[1:]):
            reasons: list[str] = []
            gap: "float | None" = None
            if prev.timestamp is not None and curr.timestamp is not None:
                gap = curr.timestamp - prev.timestamp
                prev_had_cache = (
                    prev.cache_write_5m_tokens > 0
                    or prev.cache_write_1h_tokens > 0
                    or prev.cache_read_tokens > 0
                )
                ttl = CACHE_TTL_1H_SECONDS if prev.cache_write_1h_tokens > 0 else CACHE_TTL_5M_SECONDS
                if prev_had_cache and gap > ttl:
                    reasons.append("ttl_expired")
            if prev.model and curr.model and prev.model != curr.model:
                reasons.append("model_switch")
            if not reasons:
                continue
            key = resolve_model_key(curr.model, table)
            rates = table.models.get(key) if key else None
            rewritten = curr.cache_write_5m_tokens + curr.cache_write_1h_tokens
            if rates:
                cost = (
                    curr.cache_write_5m_tokens / 1_000_000.0 * rates.cache_write_5m_per_mtok
                    + curr.cache_write_1h_tokens / 1_000_000.0 * rates.cache_write_1h_per_mtok
                )
            else:
                cost = 0.0
            events.append(
                ColdCacheEvent(
                    loop=loop_name,
                    after_line=prev.line_no,
                    gap_seconds=gap,
                    reason="+".join(reasons),
                    tokens_rewritten=rewritten,
                    estimated_cost=cost,
                )
            )
    return events


@dataclass
class ThinkingShareReport:
    estimated_thinking_output_tokens: float
    total_output_tokens: int
    share_pct: float
    method_note: str
    exact_records: int = 0
    char_estimated_records: int = 0


_THINKING_SHARE_METHOD_NOTE = (
    "Thinking share uses the EXACT output_tokens_details.thinking_tokens count "
    "for every turn whose transcript records it. For any other turn it is an "
    "ESTIMATE: that turn's thinking/redacted_thinking character length as a "
    "proportion of all its content blocks, applied to output_tokens. Character "
    "length is a proxy for token count, not exact."
)


def _compute_thinking_share(records: list[UsageRecord]) -> ThinkingShareReport:
    total_output = sum(r.output_tokens for r in records)
    thinking_total = 0.0
    exact_records = 0
    char_estimated_records = 0
    for r in records:
        if r.thinking_tokens is not None:
            exact_records += 1
            thinking_total += min(r.thinking_tokens, r.output_tokens)
        elif r.total_content_chars > 0 and r.output_tokens > 0:
            char_estimated_records += 1
            thinking_total += r.output_tokens * (r.thinking_chars / r.total_content_chars)
    share_pct = (thinking_total / total_output * 100.0) if total_output else 0.0
    return ThinkingShareReport(
        estimated_thinking_output_tokens=thinking_total,
        total_output_tokens=total_output,
        share_pct=share_pct,
        method_note=_THINKING_SHARE_METHOD_NOTE,
        exact_records=exact_records,
        char_estimated_records=char_estimated_records,
    )


# ---------------------------------------------------------------------------
# Reconciliation against an (optional, caller-supplied) cost-state source
# ---------------------------------------------------------------------------


@dataclass
class ReconciliationResult:
    available: bool
    source_description: str
    transcript_output_tokens: int
    source_output_tokens: "int | None" = None
    delta_output_tokens: "int | None" = None
    looks_truncated: bool = False
    matched_message_ids: int = 0
    unmatched_source_message_ids: int = 0
    note: str = ""


#: A reconciliation source reporting more than this fraction of additional
#: output tokens than the transcript captured is flagged as "looks
#: truncated" -- a small delta is expected noise (e.g. a source that rounds,
#: or double-counts a retried request); a large, one-sided delta is the
#: signal this exists to catch.
_TRUNCATION_DELTA_FRACTION = 0.02

_NO_RECONCILIATION_SOURCE_NOTE = (
    "No cost-state/usage reconciliation source was supplied. This repo was "
    "checked for one (fe3b44b6, 2026-09-29): meridian/db/ai_log.py's "
    "ai_log_events table is a schema SCAFFOLD only -- by its own module "
    "docstring, nothing in the running server calls append_event with an "
    "LLM usage payload today (no route, no MCP tool, no capture pipeline is "
    "wired up yet). meridian/routes/billing.py is Stripe account/customer "
    "billing, unrelated to per-token usage. There is currently nothing in "
    "this repo to auto-reconcile against. Pass reconcile_against=[...] "
    "(records with 'output_tokens', and 'message_id' where available) if "
    "you have an external source, e.g. an Anthropic Console usage export."
)


def _reconcile(
    records: list[UsageRecord], reconcile_against: "Sequence[Mapping[str, Any]] | None"
) -> ReconciliationResult:
    transcript_total = sum(r.output_tokens for r in records)
    if not reconcile_against:
        return ReconciliationResult(
            available=False,
            source_description="none supplied",
            transcript_output_tokens=transcript_total,
            note=_NO_RECONCILIATION_SOURCE_NOTE,
        )
    rows = list(reconcile_against)
    source_total = 0
    matched = 0
    unmatched = 0
    seen_ids = {r.message_id for r in records if r.message_id}
    for row in rows:
        ot = row.get("output_tokens") if isinstance(row, Mapping) else None
        if isinstance(ot, (int, float)) and not isinstance(ot, bool):
            source_total += int(ot)
        mid = row.get("message_id") if isinstance(row, Mapping) else None
        if mid:
            if mid in seen_ids:
                matched += 1
            else:
                unmatched += 1
    delta = source_total - transcript_total
    looks_truncated = delta > 0 and (
        source_total == 0 or (delta / source_total) > _TRUNCATION_DELTA_FRACTION
    )
    return ReconciliationResult(
        available=True,
        source_description=f"{len(rows)} supplied record(s)",
        transcript_output_tokens=transcript_total,
        source_output_tokens=source_total,
        delta_output_tokens=delta,
        looks_truncated=looks_truncated,
        matched_message_ids=matched,
        unmatched_source_message_ids=unmatched,
        note=(
            "looks_truncated=True means the reconciliation source reports "
            "meaningfully more output tokens than this transcript captured "
            "(> {:.0%} shortfall), consistent with a truncated/incomplete "
            "local transcript file rather than measurement noise.".format(
                _TRUNCATION_DELTA_FRACTION
            )
        ),
    )


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------

MANDATORY_LOWER_BOUND_CAVEAT = (
    "output_tokens summed from this transcript is a LOWER BOUND on actual "
    "billed output tokens: a transcript can be truncated (process killed "
    "mid-response), missing rotated/archived JSONL segments, or missing a "
    "request whose response was never durably written to disk. Treat every "
    "cost figure in this report as 'at least this much', not exact, unless "
    "reconciliation.available is true and reconciliation.looks_truncated is "
    "false."
)


@dataclass
class UsageAuditReport:
    generated_at: str
    session_dir: str
    price_table_version: str
    main_transcript_path: str
    subagent_transcript_paths: list
    request_count: dict
    duplicate_message_ids_dropped: int
    totals: TokenCostBreakdown
    by_source: dict
    by_phase: dict
    by_agent: dict
    startup_prefix_tax: list
    startup_prefix_tax_total_cost: float
    cold_cache_rewrites: list
    cold_cache_rewrites_total_cost: float
    thinking_share: ThinkingShareReport
    effort_distribution: dict
    unpriced_models: list
    parse_diagnostics: list
    reconciliation: ReconciliationResult
    caveats: list

    def to_dict(self) -> dict:
        return _round_floats(dataclasses.asdict(self))


def _round_floats(obj: Any, ndigits: int = 6) -> Any:
    if isinstance(obj, float):
        return round(obj, ndigits)
    if isinstance(obj, dict):
        return {k: _round_floats(v, ndigits) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_round_floats(v, ndigits) for v in obj]
    return obj


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _load_subagent_meta(transcript: Path) -> dict:
    """The optional ``agent-<id>.meta.json`` sidecar beside a subagent
    transcript. Only ``description`` and ``workflowPhase`` are ever read from
    it. Any read or parse problem yields ``{}`` -- the sidecar is best-effort
    labelling and is never required."""
    meta_path = transcript.parent / (transcript.stem + ".meta.json")
    try:
        raw = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _meta_text(meta: Mapping[str, Any], key: str) -> "str | None":
    value = meta.get(key)
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()[:MAX_PHASE_LABEL_LEN]


def build_report(
    session_dir: "str | Path",
    *,
    main_transcript_path: "str | Path | None" = None,
    price_table_version: "str | None" = None,
    reconcile_against: "Sequence[Mapping[str, Any]] | None" = None,
) -> UsageAuditReport:
    """Parse a Claude Code session directory into a priced
    :class:`UsageAuditReport`. See the module docstring for the assumed
    on-disk layout, transcript schema, and every honesty caveat.

    Parameters
    ----------
    session_dir:
        Directory containing the main transcript (a single top-level
        ``*.jsonl``, unless ``main_transcript_path`` is given) and,
        optionally, a ``subagents/`` tree of ``agent-*.jsonl`` files.
    main_transcript_path:
        Explicit override when ``session_dir`` doesn't hold exactly one
        top-level ``*.jsonl``.
    price_table_version:
        Which registered :data:`PRICE_TABLES` entry to price against.
        Defaults to :data:`CURRENT_PRICE_TABLE_VERSION`.
    reconcile_against:
        Optional external usage records (e.g. ``[{"message_id": ...,
        "output_tokens": ...}, ...]``) to reconcile the transcript's
        (lower-bound) output-token total against. See the module docstring's
        "Reconciliation" note for why this repo has no automatic source
        today.
    """
    session_dir_path = Path(session_dir)
    table = get_price_table(price_table_version)
    main_override = Path(main_transcript_path) if main_transcript_path is not None else None
    main_path, subagent_paths = discover_transcript_files(session_dir_path, main_override)

    main_records, main_diag = _parse_transcript_file(main_path, source="main", agent_label=None)
    subagent_records: list[UsageRecord] = []
    diagnostics = [main_diag]
    for p in subagent_paths:
        meta = _load_subagent_meta(p)
        label = _meta_text(meta, "description") or _agent_label_for(p, session_dir_path)
        recs, diag = _parse_transcript_file(
            p, source="subagent", agent_label=label,
            subagent_phase=_meta_text(meta, "workflowPhase"),
        )
        subagent_records.extend(recs)
        diagnostics.append(diag)

    all_records, dup_count = _dedupe(main_records + subagent_records)
    main_records = [r for r in all_records if r.source == "main"]
    subagent_records = [r for r in all_records if r.source == "subagent"]
    _assign_phases(main_records)

    totals = TokenCostBreakdown()
    by_source = {"main": TokenCostBreakdown(), "subagent": TokenCostBreakdown()}
    by_phase: "dict[str, TokenCostBreakdown]" = {}
    by_agent: "dict[str, TokenCostBreakdown]" = {}
    unpriced_models: set = set()

    for r in main_records + subagent_records:
        priced = price_record(r, table)
        if r.model and priced.rates_key is None:
            unpriced_models.add(r.model)
        _apply(totals, r, priced)
        _apply(by_source[r.source], r, priced)
        if r.source == "main":
            phase = r.phase or "(session start)"
            agent_key = None
        else:
            phase = f"subagent:{r.subagent_phase or 'unphased'}"
            agent_key = r.agent_label or "unknown"
        by_phase.setdefault(phase, TokenCostBreakdown())
        _apply(by_phase[phase], r, priced)
        if agent_key is not None:
            by_agent.setdefault(agent_key, TokenCostBreakdown())
            _apply(by_agent[agent_key], r, priced)

    _finalize(totals)
    for b in by_source.values():
        _finalize(b)
    for b in by_phase.values():
        _finalize(b)
    for b in by_agent.values():
        _finalize(b)

    loops = _group_by_loop(main_records, subagent_records)
    prefix_tax = _compute_prefix_tax(loops, table)
    cold_cache_events = _compute_cold_cache_events(loops, table)
    thinking_share = _compute_thinking_share(main_records + subagent_records)
    effort_distribution = dict(Counter(r.effort for r in main_records + subagent_records))
    reconciliation = _reconcile(main_records + subagent_records, reconcile_against)

    caveats = [MANDATORY_LOWER_BOUND_CAVEAT, _THINKING_SHARE_METHOD_NOTE]
    if not table.verified:
        caveats.append(
            f"Price table {table.version} is NOT verified against the live Anthropic "
            "pricing page: every dollar figure in this report is indicative only "
            "until the rates are checked and a verified table is registered."
        )
    if unpriced_models:
        caveats.append(
            "The following models appeared in the transcript but have no rates in "
            f"price table {table.version}: {sorted(unpriced_models)}. Their tokens "
            "are counted correctly but their cost contribution is 0 -- every total "
            "in this report is therefore also a LOWER BOUND on cost specifically "
            "because of these models. Add rates to the price table to fix."
        )
    if not reconciliation.available:
        caveats.append(_NO_RECONCILIATION_SOURCE_NOTE)
    elif reconciliation.looks_truncated:
        caveats.append(
            "Reconciliation source reports more output tokens than this transcript "
            "captured -- this transcript looks truncated/incomplete (see "
            "'reconciliation' for the exact delta)."
        )

    return UsageAuditReport(
        generated_at=_utc_now_iso(),
        session_dir=str(session_dir_path),
        price_table_version=table.version,
        main_transcript_path=str(main_path),
        subagent_transcript_paths=[str(p) for p in subagent_paths],
        request_count={
            "main": len(main_records),
            "subagent": len(subagent_records),
            "total": len(main_records) + len(subagent_records),
        },
        duplicate_message_ids_dropped=dup_count,
        totals=totals,
        by_source=by_source,
        by_phase=by_phase,
        by_agent=by_agent,
        startup_prefix_tax=prefix_tax,
        startup_prefix_tax_total_cost=sum(e.estimated_tax_cost for e in prefix_tax),
        cold_cache_rewrites=cold_cache_events,
        cold_cache_rewrites_total_cost=sum(e.estimated_cost for e in cold_cache_events),
        thinking_share=thinking_share,
        effort_distribution=effort_distribution,
        unpriced_models=sorted(unpriced_models),
        parse_diagnostics=diagnostics,
        reconciliation=reconciliation,
        caveats=caveats,
    )


# ---------------------------------------------------------------------------
# Human-readable summary + CLI
# ---------------------------------------------------------------------------


def format_report_text(report: UsageAuditReport) -> str:
    lines: list[str] = []
    lines.append(f"Usage audit for {report.session_dir}")
    lines.append(f"Price table: {report.price_table_version}")
    lines.append(
        f"Requests: {report.request_count['total']} total "
        f"({report.request_count['main']} main, {report.request_count['subagent']} subagent); "
        f"{report.duplicate_message_ids_dropped} duplicate message_id(s) dropped"
    )
    lines.append(f"Total cost: ${report.totals.total_cost:.4f} ({report.totals.total_tokens} tokens)")
    lines.append("")
    lines.append("By source:")
    for name, b in report.by_source.items():
        lines.append(
            f"  {name:10s} ${b.total_cost:.4f}  "
            f"(read={b.input_tokens} write5m={b.cache_write_5m_tokens} "
            f"write1h={b.cache_write_1h_tokens} cache_read={b.cache_read_tokens} "
            f"output={b.output_tokens})"
        )
    if report.by_phase:
        lines.append("")
        lines.append("By phase (main chapters; subagent phases as subagent:<phase>):")
        for name, b in sorted(report.by_phase.items(), key=lambda kv: -kv[1].total_cost):
            lines.append(f"  {name}: ${b.total_cost:.4f} ({b.request_count} requests)")
    if report.by_agent:
        lines.append("")
        lines.append("By agent:")
        for name, b in sorted(report.by_agent.items(), key=lambda kv: -kv[1].total_cost):
            lines.append(f"  {name}: ${b.total_cost:.4f} ({b.request_count} requests)")
    lines.append("")
    lines.append(
        f"Startup-prefix tax (estimate): ${report.startup_prefix_tax_total_cost:.4f} "
        f"across {len(report.startup_prefix_tax)} loop(s)"
    )
    lines.append(
        f"Cold-cache rewrites: {len(report.cold_cache_rewrites)} event(s), "
        f"${report.cold_cache_rewrites_total_cost:.4f} estimated wasted rewrite cost"
    )
    lines.append(
        f"Thinking-token share: ~{report.thinking_share.share_pct:.1f}% of "
        f"{report.thinking_share.total_output_tokens} output tokens (estimate)"
    )
    lines.append(f"Effort distribution: {report.effort_distribution}")
    if report.unpriced_models:
        lines.append(f"WARNING: unpriced models seen: {report.unpriced_models}")
    lines.append("")
    lines.append("Reconciliation: " + ("available" if report.reconciliation.available else "NOT AVAILABLE"))
    lines.append("  " + report.reconciliation.note)
    lines.append("")
    lines.append("Caveats:")
    for c in report.caveats:
        lines.append(f"  - {c}")
    return "\n".join(lines)


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m meridian.usage_audit",
        description="Priced per-source/phase cost breakdown from a Claude Code session transcript.",
    )
    parser.add_argument("session_dir", help="Directory containing the session's main *.jsonl and optional subagents/ tree")
    parser.add_argument("--main-transcript", dest="main_transcript", default=None, help="Explicit main transcript path override")
    parser.add_argument("--price-table-version", dest="price_table_version", default=None)
    parser.add_argument(
        "--reconcile-file", dest="reconcile_file", default=None,
        help="Path to a JSON file: a list of {message_id?, output_tokens} records (or a single such object)",
    )
    parser.add_argument("--json", dest="as_json", action="store_true", help="Print machine-readable JSON instead of a text summary")
    args = parser.parse_args(argv)

    reconcile_against = None
    if args.reconcile_file:
        raw = json.loads(Path(args.reconcile_file).read_text(encoding="utf-8"))
        reconcile_against = raw if isinstance(raw, list) else [raw]

    try:
        report = build_report(
            args.session_dir,
            main_transcript_path=args.main_transcript,
            price_table_version=args.price_table_version,
            reconcile_against=reconcile_against,
        )
    except UsageAuditError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.as_json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    else:
        print(format_report_text(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
