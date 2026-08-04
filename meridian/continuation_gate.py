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
    for it in items or []:
        if not isinstance(it, dict):
            continue
        status = str(it.get("status") or "pending").strip().lower()
        if status in _TERMINAL_STATUSES:
            continue
        if _is_genuinely_blocked(it):
            blocked.append(it)
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
    elif blocked and not actionable:
        reason = (
            f"{len(blocked)} item(s) genuinely blocked (blocker_kind set); "
            "no actionable work remains"
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
        "reason": reason,
    }
