"""5dfe34b2 — opt-in manual-issue content-screening extension to the fdaa5b55
GitHub-issue automation.

This module is the DB/content layer for the discretionary, OFF-by-default
toggle (meridian.db.workspace.set_manual_issue_screening_enabled) that lets
Meridian's automated GitHub-issue action (comment/propose — still NEVER
auto-close, per fdaa5b55's own rule, which this module never touches) extend
its reach to issues Meridian did not itself create.

KEY FRAMING (Adam, verbatim from the design spec): "the issue creation on
GitHub is simply a non-Meridian user interface into what's currently going on
with the project" — the toggle governs WHO can trigger a read (an internal
person choosing to enable it), not whether the CONTENT read is trustworthy.
Anyone on the internet can file a GitHub issue regardless of who flipped the
toggle, so screening here is a genuine last line of defense, not a proof of
safety. Per OWASP LLM01, no heuristic technique fully mitigates prompt
injection — this is a real filter that catches obvious, documented shapes and
forces human review, not a claim of completeness.

This module has NO GitHub API access (same separation as the rest of
meridian/db — see meridian.mcp.handler._dispatch_github_tool for the only
place that speaks to the GitHub API with a tenant's PAT). It only ever
receives already-fetched text and decides what to log / flag / sanitize.

71fcfb39 — tool-isolated LLM calls: NOTHING in this module (or in the
discover_and_link_manual_issue flow that calls it, see
meridian.mcp.handler) invokes an LLM to read/summarize manual-issue content.
The screening below is purely pattern/heuristic (regex + literal character
checks) — deterministic, auditable, and cheap to run on every read. If a
FUTURE change adds an LLM call anywhere in this pipeline (e.g. to draft a
higher-quality human-readable proposal from issue content), that call MUST be
structurally isolated — zero tool access, pure text-in/text-out, given
already-screened/sanitized text only, never the raw pre-screening content —
enforcing the same "screen before use" invariant this module establishes for
the heuristic path. This comment is the enforcement mechanism until such a
call exists: there is deliberately no LLM-calling code path here to extend.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

import aiosqlite

from xml.sax.saxutils import escape as _xml_escape  # same discipline as
# fdaa5b55/cd038235 (db.sprint_items.build_github_completion_comment) and
# 5abf3e12 (meridian/handoff.py's _build_quick_start_goal).

# Shared helpers from the parent db package — same lazy-import pattern
# workspace.py uses to avoid a circular import while db/__init__.py is still
# initializing.
from meridian.db import (  # noqa: PLC0415
    _new_id,
    _row_to_dict,
)


# ---------------------------------------------------------------------------
# 18d25f05 — heuristic injection-pattern screening.
#
# NOT claimed as complete or perfect (OWASP LLM01 is explicit: no technique
# guarantees full mitigation of prompt injection). This is a real, hardcoded
# filter layer that catches obvious/documented shapes and forces human review
# instead of silent pass-through into any Meridian-controlled surface.
# ---------------------------------------------------------------------------

# Role-play markers: a line that looks like it's trying to open a fake
# system/assistant/user/tool turn, optionally inside a code fence.
_ROLE_PLAY_MARKER_RE = re.compile(
    r"(?im)^\s*(?:```)?\s*(system|assistant|user|tool)\s*:\s*\S"
)

# "ignore previous instructions"-class phrasing — a small set of common
# injection openers, not an exhaustive list.
_IGNORE_INSTRUCTIONS_RE = re.compile(
    r"(?i)\b(ignore|disregard|forget|override)\b[^.\n]{0,40}"
    r"\b(previous|prior|above|earlier|all|your)\b[^.\n]{0,40}"
    r"\b(instructions?|prompts?|rules?|context|directives?)\b"
)

# Suspicious embedded blocks pretending to be tool/system output.
_FAKE_TOOL_OUTPUT_RE = re.compile(
    r"(?i)(```\s*(?:output|tool[_-]?result|tool[_-]?output|function[_-]?results?)\b"
    r"|<tool_result>|<function_results>|<system>|\[tool output\]|\[system\]|\[/?INST\])"
)

# Zero-width / invisible unicode characters commonly used to hide or split
# injection payloads from naive text scanners.
_ZERO_WIDTH_CHARS = ("​", "‌", "‍", "﻿", "⁠", "᠎")

# A small, deliberately non-exhaustive set of Cyrillic/Greek characters that
# are common homoglyphs for Latin letters — enough to catch crude "look like
# an English instruction but bypass literal-string filters" tricks. Matching
# ANY of these in otherwise-Latin text is a coarse signal, not proof.
_HOMOGLYPH_CHARS = (
    "а", "е", "о", "р", "с", "у",  # а е о р с у
    "і", "ԁ", "ӏ", "α", "ο",             # і ԁ ӏ α ο
)

_SCREENING_REASON_LABELS: dict[str, str] = {
    "role_play_marker": "text opens a fake system/assistant/user/tool turn",
    "ignore_instructions_phrasing": "text tries to override prior instructions",
    "fake_tool_output_block": "text impersonates tool/system output",
    "zero_width_unicode": "text contains invisible zero-width unicode characters",
    "suspicious_homoglyph": "text contains non-Latin look-alike characters mixed into Latin text",
}


def screen_manual_issue_content(text: str | None) -> dict[str, Any]:
    """Run the hardcoded heuristic screen over ``text`` (an issue title, body,
    or comment body). Pure function, never raises, never touches the network
    or an LLM (see module docstring — 71fcfb39).

    Returns ``{"flagged": bool, "reasons": [str, ...], "reason_labels": [...]}``.
    An empty ``reasons`` list means nothing hardcoded fired — NOT a guarantee
    the content is safe, just that it didn't match a known shape.
    """
    if not text:
        return {"flagged": False, "reasons": [], "reason_labels": []}
    reasons: list[str] = []
    if _ROLE_PLAY_MARKER_RE.search(text):
        reasons.append("role_play_marker")
    if _IGNORE_INSTRUCTIONS_RE.search(text):
        reasons.append("ignore_instructions_phrasing")
    if _FAKE_TOOL_OUTPUT_RE.search(text):
        reasons.append("fake_tool_output_block")
    if any(ch in text for ch in _ZERO_WIDTH_CHARS):
        reasons.append("zero_width_unicode")
    if any(ch in text for ch in _HOMOGLYPH_CHARS):
        reasons.append("suspicious_homoglyph")
    return {
        "flagged": bool(reasons),
        "reasons": reasons,
        "reason_labels": [_SCREENING_REASON_LABELS[r] for r in reasons],
    }


def screen_manual_issue(title: str | None, body: str | None, comments: list[str]) -> dict[str, Any]:
    """Screen an entire manually-filed issue (title + body + every comment
    body) as one unit — flagged if ANY fragment flags. Returns the same shape
    as :func:`screen_manual_issue_content`, plus ``flagged_fragments`` (which
    of title/body/comment-N tripped a rule) for audit/debugging.
    """
    all_reasons: set[str] = set()
    flagged_fragments: list[str] = []
    fragments = [("title", title), ("body", body)]
    fragments.extend((f"comment[{i}]", c) for i, c in enumerate(comments or []))
    for label, frag in fragments:
        result = screen_manual_issue_content(frag)
        if result["flagged"]:
            all_reasons.update(result["reasons"])
            flagged_fragments.append(label)
    reasons = sorted(all_reasons)
    return {
        "flagged": bool(reasons),
        "reasons": reasons,
        "reason_labels": [_SCREENING_REASON_LABELS[r] for r in reasons],
        "flagged_fragments": flagged_fragments,
    }


# ---------------------------------------------------------------------------
# Rendering / escaping — reuse the SAME discipline as fdaa5b55/cd038235 and
# 5abf3e12, PLUS markdown/HTML neutralization specific to manual-issue text
# reaching a Meridian-controlled surface (comment draft, HITL question body).
# A human reviewing a proposed-action HITL is itself a rendering-surface
# attack vector, so raw HTML tags and auto-linkable URLs are neutralized too.
# ---------------------------------------------------------------------------

_RAW_HTML_TAG_RE = re.compile(r"<[^>\n]+>")
_BARE_URL_RE = re.compile(r"(https?://\S+)")


def sanitize_manual_issue_excerpt(text: str | None, max_len: int = 500) -> str:
    """Produce a safe-to-render excerpt of manual-issue-derived text for any
    Meridian-controlled surface (comment draft, HITL question/context body).

    1. Truncate to ``max_len`` chars.
    2. Strip raw HTML tags outright (escaping alone leaves the tag structure
       visible as text; stripping avoids even that).
    3. Wrap bare URLs in backticks so markdown renderers treat them as inert
       code rather than clickable/auto-linked domains.
    4. XML-escape the result (same helper as build_github_completion_comment).
    """
    if not text:
        return ""
    t = text[:max_len]
    t = _RAW_HTML_TAG_RE.sub("", t)
    t = _BARE_URL_RE.sub(r"`\1`", t)
    return _xml_escape(t)


# ---------------------------------------------------------------------------
# 2178b161 — append-only, hashed, timestamped raw-content log. Written BEFORE
# any screening/processing touches the content (forensic reconstruction:
# "what did Meridian SEE", independent of what screening flagged or missed).
# ---------------------------------------------------------------------------


async def log_raw_manual_issue_content(
    db: aiosqlite.Connection,
    project_id: str,
    issue_number: int,
    raw_content: str,
) -> dict[str, Any]:
    """Append ``raw_content`` (unmodified, unescaped, unscreened) to
    manual_issue_content_log with a sha256 content-hash column for
    tamper-evidence. No update/delete helper is ever provided for this table
    — append-only by construction, not just convention.
    """
    content_hash = hashlib.sha256((raw_content or "").encode("utf-8")).hexdigest()
    row_id = _new_id()
    await db.execute(
        "INSERT INTO manual_issue_content_log "
        "(id, project_id, issue_number, content_hash, raw_content) "
        "VALUES (?, ?, ?, ?, ?)",
        (row_id, project_id, issue_number, content_hash, raw_content or ""),
    )
    await db.commit()
    async with db.execute(
        "SELECT * FROM manual_issue_content_log WHERE id = ?", (row_id,)
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row) or {"id": row_id, "content_hash": content_hash}


async def get_raw_manual_issue_content_log(
    db: aiosqlite.Connection,
    project_id: str,
    issue_number: int | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Read back the forensic raw-content log, newest first (for tests /
    incident review). Read-only — there is deliberately no companion
    update/delete function anywhere in this module."""
    clauses = ["project_id = ?"]
    params: list[Any] = [project_id]
    if issue_number is not None:
        clauses.append("issue_number = ?")
        params.append(issue_number)
    params.append(max(1, int(limit)))
    async with db.execute(
        f"SELECT * FROM manual_issue_content_log WHERE {' AND '.join(clauses)} "
        "ORDER BY created_at DESC LIMIT ?",
        params,
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows if r is not None]  # type: ignore[misc]


# ---------------------------------------------------------------------------
# d86d70a5 — wave-relative velocity/anomaly signal.
#
# Adam explicitly rejected a flat action-count-per-time-window circuit
# breaker: this project's own normal operation regularly completes 6-9 sprint
# items in one wave/batch, each with its own linked issue, so a naive
# absolute threshold ("N actions in T minutes = anomaly") would misfire on
# healthy throughput. Instead: compare action velocity against the size of
# the actual completing wave/batch (assign_sprint_waves's `wave` label is the
# structural signal — see meridian.db.sprint_items.assign_sprint_waves /
# get_parallelizable_groups). 8 actions correlated to a real 8-item wave
# completion is normal; 8 actions with no such correlated batch behind them
# is the actual anomaly.
# ---------------------------------------------------------------------------

# Small constant slack so staggered near-window completions of a genuine wave
# (items finishing a few seconds/minutes apart, not perfectly simultaneously)
# don't false-positive right at the wave-size boundary.
_ANOMALY_SLACK = 3
# Floor below which we never flag — a wave of 1-2 items triggering 1-2
# actions is trivially normal and not worth escalating.
_ANOMALY_FLOOR = 3


async def check_manual_issue_action_velocity(
    db: aiosqlite.Connection,
    project_id: str,
    *,
    triggering_item: dict[str, Any] | None = None,
    window_minutes: int = 15,
) -> dict[str, Any]:
    """Wave-relative anomaly signal for manual-issue GitHub actions.

    ``triggering_item`` is the sprint item whose completion is about to
    trigger (or just triggered) a manual-issue action — its ``wave`` label
    (assigned by assign_sprint_waves) is the structural "genuine batch"
    correlate: the number of OTHER sprint items sharing that same wave label
    is the expected action volume. An item with no wave label at all
    correlates to a batch-of-one, so any burst beyond that is anomalous.

    The hard-ceiling backstop is tied to the project's own total sprint-item
    count (never larger than "every item in the project could plausibly act
    at once") rather than an arbitrary small constant, per the design spec's
    "tied to something structurally meaningful" requirement.

    Returns ``{"recent_actions", "wave_label", "wave_size", "threshold",
    "hard_ceiling", "is_anomalous"}``. Never raises — a lookup failure
    degrades to "not anomalous" (fail-open on the *signal*, since the actual
    action-count-vs-threshold decision, not this helper, is what gates
    behavior, and this is a non-blocking escalation signal, not a hard gate —
    see the caller in meridian.mcp.handler).
    """
    from meridian.db import get_action_audit_log, get_sprint_items  # noqa: PLC0415
    from datetime import datetime, timedelta, timezone

    try:
        since = (
            datetime.now(timezone.utc) - timedelta(minutes=max(1, window_minutes))
        ).strftime("%Y-%m-%d %H:%M:%S")
        recent_events = await get_action_audit_log(
            db, project_id=project_id, event_type="manual_issue_action", since=since,
            limit=1000,
        )
        recent_actions = len(recent_events)

        all_items = await get_sprint_items(db, project_id, include_manual_blocker=True)
        wave_label = (triggering_item or {}).get("wave")
        if wave_label:
            wave_size = sum(1 for it in all_items if it.get("wave") == wave_label) or 1
        else:
            wave_size = 1
        threshold = max(wave_size, 1) + _ANOMALY_SLACK
        hard_ceiling = max(len(all_items), 20)
        is_anomalous = (
            recent_actions > _ANOMALY_FLOOR
            and (recent_actions > threshold or recent_actions > hard_ceiling)
        )
        return {
            "recent_actions": recent_actions,
            "wave_label": wave_label,
            "wave_size": wave_size,
            "threshold": threshold,
            "hard_ceiling": hard_ceiling,
            "is_anomalous": is_anomalous,
        }
    except Exception:  # noqa: BLE001 — never let a signal failure break the caller
        return {
            "recent_actions": 0, "wave_label": None, "wave_size": 1,
            "threshold": _ANOMALY_SLACK + 1, "hard_ceiling": 20,
            "is_anomalous": False,
        }
