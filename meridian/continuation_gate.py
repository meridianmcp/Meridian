"""ecc8b280 — machine-readable continuation / terminal-ready gate.

Closes an observed premature-termination / reward-hacking failure mode: an
autonomous executor completed a batch, explicitly acknowledged two newly
claimed items and a remaining batch, then yielded with "let me know" despite
running in autonomous/no-confirmation mode and with no genuine blocker on
file. Nothing in the protocol previously forced that distinction to be
machine-readable — ``_execution_mode_directive`` only emits prose policy,
``complete_sprint_item`` validates a single item, and ``generate_handoff``
renders remaining work without asserting anything about whether stopping is
actually allowed.

This module computes that state as a small, pure function so it can be
reused identically from ``get_sprint_progress``, ``generate_handoff``, and
``complete_sprint_item`` without three divergent copies of the same logic.
It deliberately does no I/O: callers pass in an already-fetched, already
version/session-scoped item list.

Terminology:

``continuation_required``
    True when actionable work remains and the project's execution mode is
    ``autonomous`` — the executor may NOT treat the session as finished.

``terminal_ready``
    ``not continuation_required``. True either because no actionable work
    remains, or because the project is in ``interactive`` mode (which
    already has its own human-confirmation gate before every claim, so a
    second hard block here would be redundant).

A "genuine blocker" is a *structural* signal — an item carrying a non-empty
``blocker_kind`` — not free text in ``notes``. This mirrors
``meridian/mcp/handler.py``'s ``_detect_notes_blocker_drift``, which already
flags the opposite drift (notes describing a blocker with no ``blocker_kind``
set). A pending item with prose like "blocked, need input" but no
``blocker_kind`` is exactly the reward-hacking shape this gate exists to
catch, so it does NOT count as a genuine blocker escape.

7e7d9a43 — a future-``deferred_until`` item is a DIFFERENT, deliberately
backburnered signal (not a structural blocker) and is tracked in its own
``deferred``/``deferred_count``/``deferred_item_ids`` bucket rather than
folded into ``blocked``/``blocked_count`` — those two fields keep their
pre-existing pure "structural blocker_kind" meaning for every caller that
already reads them. Either way it is excluded from ``actionable`` the same
as a genuine blocker: an autonomous executor must not be told a
deliberately-backburnered item is part of "remaining work."
"""
from __future__ import annotations

from typing import Any

# Statuses that still require executor action.
_ACTIONABLE_STATUSES = frozenset({"pending", "todo", "in_progress", "indeterminate"})

# Statuses that are already terminal and never block completion.
_TERMINAL_STATUSES = frozenset({"done", "failed", "skipped", "pushed"})

_VALID_EXECUTION_MODES = frozenset({"autonomous", "interactive"})


def _is_genuinely_blocked(item: dict[str, Any]) -> bool:
    """True only when the item carries a structured ``blocker_kind``.

    Free-text notes claiming a blocker do not count — see module docstring.
    """
    return bool((item or {}).get("blocker_kind"))


def _parse_deferral_ts(value: Any) -> "datetime | None":  # noqa: F821
    """7e7d9a43 — parse a ``deferred_until`` value into a naive-UTC datetime.

    Duplicated from ``meridian.db.sprint_items._parse_deferral_ts`` (not
    imported) for the SAME reason ``_is_hard_blocked_sprint_item``/
    ``_is_manual_sprint_item`` are duplicated between ``db/sprint_items.py``
    and ``handoff.py``: ``db/sprint_items.py`` imports this module
    (``from .. import continuation_gate``), so importing back would be a
    circular dependency. Keep in sync with the db/sprint_items.py original.
    """
    from datetime import datetime, timezone

    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        s = str(value).strip()
        if not s:
            return None
        s = s.replace("Z", "+00:00")
        dt = None
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                try:
                    dt = datetime.strptime(s, fmt)
                    break
                except ValueError:
                    continue
        if dt is None:
            return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _is_future_deferred(item: dict[str, Any]) -> bool:
    """7e7d9a43 — True when the item's ``deferred_until`` is in the future.

    Mirrors ``meridian.db.sprint_items._is_deferred`` exactly (fail-open on
    garbage/unparseable values). A future-deferred item is deliberately
    backburnered — distinct from a genuine structural ``blocker_kind`` — but
    must be excluded from ``actionable`` the same way: an autonomous
    executor must not be told a backburnered item is part of "remaining
    actionable work" any more than a superseded/manual one. Confirmed gap
    this closes: before this existed, ``compute_continuation_state`` had no
    awareness of ``deferred_until`` at all, so ``get_sprint_progress``'s
    ``continuation.actionable_item_ids`` (the one caller that passes an
    UNFILTERED item list — see that handler's own ``_all`` — every other
    caller already pre-filters deferred items via
    ``get_sprint_items(include_deferred=False)`` before this function ever
    sees them) could list a deliberately backburnered item as actionable.
    """
    raw = (item or {}).get("deferred_until")
    if not raw:
        return False
    dt = _parse_deferral_ts(raw)
    if dt is None:
        return False
    from datetime import datetime as _dt_cls

    return dt > _dt_cls.utcnow()


def _normalize_execution_mode(execution_mode: str | None) -> str:
    normalized = (execution_mode or "autonomous").strip().lower()
    if normalized not in _VALID_EXECUTION_MODES:
        normalized = "autonomous"
    return normalized


def compute_continuation_state(
    items: list[dict[str, Any]] | None,
    *,
    execution_mode: str | None = "autonomous",
) -> dict[str, Any]:
    """Compute the continuation/terminal-ready gate for a scoped item list.

    ``items`` should already be scoped by the caller (by project/version/
    session, as appropriate) — this function applies no filtering beyond
    status/``blocker_kind`` inspection, so the exact same call is safe from
    ``get_sprint_progress``, ``generate_handoff``, and
    ``complete_sprint_item`` without re-deriving scoping logic three times.

    Returns a plain, JSON-serializable dict — never raises.
    """
    normalized_mode = _normalize_execution_mode(execution_mode)

    actionable: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        status = str(it.get("status") or "pending").strip().lower()
        if status in _TERMINAL_STATUSES:
            continue
        if _is_genuinely_blocked(it):
            blocked.append(it)
            continue
        # 7e7d9a43 — a future-deferred item is deliberately backburnered, not
        # a genuine blocker_kind escape: tracked separately from `blocked` so
        # blocked_count/blocked_item_ids keep their existing pure
        # "structural blocker_kind" meaning (no behavior change for any
        # existing caller reading those two fields), but still excluded from
        # `actionable` — same "excluded but surfaced, never silently
        # dropped" convention as the blocker_kind escape just above.
        if _is_future_deferred(it):
            deferred.append(it)
            continue
        # Anything else (including unrecognised future statuses) fails
        # closed as actionable rather than silently disappearing.
        actionable.append(it)

    actionable_pending = [
        i for i in actionable if str(i.get("status") or "pending").lower() != "in_progress"
    ]
    actionable_in_progress = [
        i for i in actionable if str(i.get("status") or "").lower() == "in_progress"
    ]

    # Interactive mode already requires human confirmation before every
    # claim (see _execution_mode_directive / AGENTS.md session protocol) —
    # only autonomous mode gets the hard continuation block here.
    continuation_required = bool(actionable) and normalized_mode == "autonomous"
    terminal_ready = not continuation_required

    if not items:
        reason = "no scoped sprint items"
    elif continuation_required:
        reason = (
            f"{len(actionable)} actionable item(s) remain "
            f"({len(actionable_pending)} pending/todo, "
            f"{len(actionable_in_progress)} in_progress) with no recorded "
            "blocker_kind while execution_mode=autonomous"
        )
    elif normalized_mode != "autonomous":
        reason = f"execution_mode={normalized_mode} — human confirmation gate applies instead"
    elif blocked and not actionable and not deferred:
        reason = (
            f"{len(blocked)} item(s) genuinely blocked (blocker_kind set); "
            "no actionable work remains"
        )
    elif deferred and not actionable:
        # 7e7d9a43 — deferred-only remainder (with or without a genuine
        # blocker_kind item alongside it) is still terminal-ready: neither
        # bucket is claimable right now.
        reason = (
            f"{len(deferred)} item(s) deferred to a future date"
            + (f" and {len(blocked)} genuinely blocked" if blocked else "")
            + "; no actionable work remains"
        )
    else:
        reason = "all scoped items are terminal"

    return {
        "continuation_required": continuation_required,
        "terminal_ready": terminal_ready,
        "execution_mode": normalized_mode,
        "actionable_count": len(actionable),
        "actionable_pending_count": len(actionable_pending),
        "actionable_in_progress_count": len(actionable_in_progress),
        "actionable_item_ids": [i.get("id") for i in actionable],
        "blocked_count": len(blocked),
        "blocked_item_ids": [i.get("id") for i in blocked],
        # 7e7d9a43 — future-deferred items: excluded from actionable/blocked,
        # surfaced here instead of silently vanishing from the report.
        "deferred_count": len(deferred),
        "deferred_item_ids": [i.get("id") for i in deferred],
        "reason": reason,
    }
