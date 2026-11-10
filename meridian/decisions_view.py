"""Formal view of open human decisions.

A decision is a pending HITL question that carries answer options (the payload
written by ``request_hitl``). This module only reads rows and shapes them for
the ``list_decisions`` MCP tool: numbered options, the recommended option, the
urgency and whether a human must answer. It never answers anything; answering
stays with ``answer_hitl``.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

#: Higher number sorts first. Unknown urgencies sort after normal.
_URGENCY_RANK = {"blocking": 0, "high": 1, "normal": 2}


def _payload(row: Mapping[str, Any]) -> dict[str, Any]:
    """The row's payload as a dict. Stored as JSON text; bad or missing data gives {}."""
    raw = row.get("payload")
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _option_labels(payload: Mapping[str, Any]) -> list[str]:
    """Non-empty option labels, in the order they were filed."""
    raw = payload.get("options") or []
    if not isinstance(raw, list):
        return []
    return [str(opt) for opt in raw if str(opt).strip()]


def _recommended_number(payload: Mapping[str, Any], labels: list[str]) -> int | None:
    """1-based number of the recommended option, or None when it is absent or unmatched.

    ``recommended`` may be a 0-based index or the option text itself.
    """
    rec = payload.get("recommended")
    if isinstance(rec, bool):
        return None
    if isinstance(rec, int) and 0 <= rec < len(labels):
        return rec + 1
    if isinstance(rec, str) and rec in labels:
        return labels.index(rec) + 1
    return None


def decision_view(row: Mapping[str, Any]) -> dict[str, Any]:
    """Shape one HITL row as a decision for a human to read and answer."""
    payload = _payload(row)
    labels = _option_labels(payload)
    return {
        "id": row.get("id"),
        "project_id": row.get("project_id"),
        "question": row.get("question") or "",
        "context": row.get("context") or "",
        "urgency": row.get("urgency") or "normal",
        "created_at": row.get("created_at"),
        "require_human": bool(payload.get("require_human", False)),
        "options": [{"number": i, "label": label} for i, label in enumerate(labels, start=1)],
        "recommended": _recommended_number(payload, labels),
    }


def is_open_decision(row: Mapping[str, Any]) -> bool:
    """A pending question with at least one answer option."""
    return (
        (row.get("kind") or "question") == "question"
        and row.get("status") == "pending"
        and bool(_option_labels(_payload(row)))
    )


def open_decisions(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """The open decisions from ``rows``, most urgent first, oldest first within an urgency."""
    views = [decision_view(row) for row in rows if is_open_decision(row)]
    views.sort(
        key=lambda v: (
            _URGENCY_RANK.get(v["urgency"], len(_URGENCY_RANK)),
            str(v.get("created_at") or ""),
        )
    )
    return {"count": len(views), "decisions": views}
