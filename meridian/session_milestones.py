"""Pointer-first project-state milestones and checkpoint escalation.

Milestones are deliberately separate from routine ``sessions.checkpoint_data``:
they are append-only, content-hashed snapshots written only for an explicit
material transition or a risk threshold. Raw provider conversations and
artifact bytes stay in their own stores.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from meridian import db as db_module
from meridian.db.sprint_items import get_sprint_item_pointers
from meridian.pointers import build_project_state_milestone_pointer
from meridian.secret_redaction import redact

_MATERIAL_TRANSITIONS = frozenset(
    {
        "manual_requested",
        "goal_scope_changed",
        "decision_committed",
        "sprint_item_completed",
        "provider_session_unavailable",
        "artifact_captured",
        "recovery_verified",
        "release_preparation",
    }
)
_RISK_WEIGHTS = {
    "provider_unavailable": 3,
    "dirty_worktree": 2,
    "artifact_integrity_uncertain": 3,
    "artifact_hash_mismatch": 5,
    "stale_source": 2,
    "missing_source": 2,
    "cross_project_ambiguity": 5,
    "unexpected_active_claims": 1,
    "large_uncommitted_change": 2,
}
_IMMEDIATE_RISK_SIGNALS = frozenset(
    {"artifact_hash_mismatch", "cross_project_ambiguity"}
)
_MANIFEST_STATES = frozenset(
    {"available", "not_configured", "degraded", "unavailable"}
)
_MAX_BOARD_ITEMS = 120
_MAX_SOURCE_RECORDS = 100
_MAX_POINTERS = 300


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _sha256(value: str | bytes) -> str:
    raw = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(raw).hexdigest()


def _safe_text(value: Any, *, limit: int = 2400) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return redact(value)[:limit]


def _content_reference(row: dict[str, Any], *, kind: str, project_id: str) -> dict[str, Any]:
    body = row.get("body")
    safe_body = redact(body) if isinstance(body, str) else ""
    title = _safe_text(row.get("title"), limit=300) or ""
    row_id = str(row.get("id") or "")
    return {
        "id": row_id,
        "title": title,
        "kind": kind,
        "status": row.get("status"),
        "content_sha256": _sha256(safe_body) if safe_body else None,
        "content_redacted": bool(body and safe_body != body),
        "source_uri": f"meridian://project/{project_id}/{kind}/{row_id}",
        "updated_at": row.get("updated_at") or row.get("created_at"),
    }


def _manifest_receipt(value: Any) -> dict[str, Any]:
    if value is None:
        return {
            "status": "not_reported",
            "sha256": None,
            "occurrence_count": None,
            "source": "local_client_report",
            "server_verified": False,
        }
    if not isinstance(value, dict):
        raise ValueError("artifact_manifest must be an object")
    status = value.get("status")
    if status not in _MANIFEST_STATES:
        raise ValueError("artifact_manifest.status is not supported")
    digest = value.get("sha256")
    if digest is not None and (
        not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
    ):
        raise ValueError("artifact_manifest.sha256 must be a lowercase SHA-256 digest")
    count = value.get("occurrence_count")
    if count is not None and (not isinstance(count, int) or isinstance(count, bool) or count < 0):
        raise ValueError("artifact_manifest.occurrence_count must be a non-negative integer")
    return {
        "status": status,
        "sha256": digest,
        "occurrence_count": count,
        "source": "local_client_report",
        "server_verified": False,
    }


def assess_checkpoint_escalation(
    *,
    milestone_trigger: str | None,
    risk_signals: list[str] | None,
    recent_milestone_count: int = 0,
) -> dict[str, Any]:
    """Apply explicit-transition rules and a bounded, cadence-aware risk gate."""
    trigger = milestone_trigger
    if trigger is not None and trigger not in _MATERIAL_TRANSITIONS:
        raise ValueError(f"milestone_trigger must be one of {sorted(_MATERIAL_TRANSITIONS)}")
    if risk_signals is None:
        risk_signals = []
    if not isinstance(risk_signals, list) or len(risk_signals) > 20:
        raise ValueError("risk_signals must be a list containing at most 20 values")
    if any(not isinstance(signal, str) or signal not in _RISK_WEIGHTS for signal in risk_signals):
        raise ValueError(f"risk_signals must use the supported values {sorted(_RISK_WEIGHTS)}")
    signals = sorted(set(risk_signals))
    recent = max(0, int(recent_milestone_count))
    # Repeated deep captures raise the threshold modestly for 24 hours to
    # coalesce event storms. The cap keeps the system sensitive to real risk.
    threshold = min(7, 4 + recent // 3)
    score = min(20, sum(_RISK_WEIGHTS[signal] for signal in signals))
    forced = trigger is not None
    immediate = bool(_IMMEDIATE_RISK_SIGNALS.intersection(signals))
    captured = forced or immediate or score >= threshold
    if forced:
        reason = "material_transition"
    elif immediate:
        reason = "critical_risk_signal"
    elif captured:
        reason = "risk_threshold_reached"
    else:
        reason = "routine_checkpoint"
    return {
        "captured": captured,
        "reason": reason,
        "milestone_trigger": trigger,
        "risk_signals": signals,
        "risk_score": score,
        "risk_threshold": threshold,
        "recent_milestones_24h": recent,
    }


async def _recent_milestone_count(db: Any, project_id: str) -> int:
    rows = await db_module.list_project_state_milestones(db, project_id, limit=100)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    count = 0
    for row in rows:
        raw = row.get("captured_at")
        if not isinstance(raw, str):
            continue
        try:
            captured = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if captured >= cutoff:
            count += 1
    return count


async def _build_snapshot(
    db: Any,
    project_id: str,
    session_id: str,
    *,
    version: str | None,
    escalation: dict[str, Any],
    artifact_manifest: dict[str, Any],
) -> dict[str, Any]:
    project = await db_module.get_project(db, project_id)
    if project is None:
        raise ValueError("project was not found")
    async with db.execute(
        "SELECT id, project_id, name, status FROM sessions WHERE id = ?",
        (session_id,),
    ) as cursor:
        session = await cursor.fetchone()
    if session is None or session["project_id"] != project_id:
        raise ValueError("session does not belong to the selected project")

    items = await db_module.get_sprint_items(db, project_id, version=version)
    counts = Counter(str(item.get("status") or "unknown") for item in items)
    active_statuses = {
        "pending", "todo", "in_progress", "provisional_complete", "indeterminate"
    }
    active_items = [item for item in items if item.get("status") in active_statuses]
    done_items = [item for item in items if item.get("status") == "done"]
    visible_items = active_items[:_MAX_BOARD_ITEMS]
    if len(visible_items) < _MAX_BOARD_ITEMS:
        visible_items.extend(done_items[-min(20, _MAX_BOARD_ITEMS - len(visible_items)):])
    board_snapshot = {
        "version": version,
        "counts_by_status": dict(sorted(counts.items())),
        "items": [
            {
                "id": str(item.get("id") or ""),
                "title": _safe_text(item.get("title"), limit=300),
                "status": item.get("status"),
                "updated_at": item.get("updated_at") or item.get("completed_at"),
            }
            for item in visible_items
        ],
        "omitted_item_count": max(0, len(active_items) + min(20, len(done_items)) - len(visible_items)),
    }

    decisions = await db_module.get_pinned_decisions(db, project_id, include_superseded=False)
    notes = await db_module.get_project_notes(
        db, project_id, bodies=True, limit=_MAX_SOURCE_RECORDS
    )
    insights = await db_module.get_insights(db, project_id)
    durable_records = {
        "decisions": [
            _content_reference(row, kind="decisions", project_id=project_id)
            for row in decisions[:_MAX_SOURCE_RECORDS]
        ],
        "notes": [
            _content_reference(row, kind="notes", project_id=project_id)
            for row in notes[:_MAX_SOURCE_RECORDS]
        ],
        "insights": [
            _content_reference(row, kind="insights", project_id=project_id)
            for row in insights[:_MAX_SOURCE_RECORDS]
        ],
        "truncated": {
            "decisions": max(0, len(decisions) - _MAX_SOURCE_RECORDS),
            "notes": max(0, len(notes) - _MAX_SOURCE_RECORDS),
            "insights": max(0, len(insights) - _MAX_SOURCE_RECORDS),
        },
    }

    pointer_refs: list[dict[str, Any]] = []
    for item in visible_items:
        if len(pointer_refs) >= _MAX_POINTERS:
            break
        for pointer in await get_sprint_item_pointers(db, str(item.get("id") or "")):
            if len(pointer_refs) >= _MAX_POINTERS:
                break
            material = {
                "id": pointer.get("id"),
                "source_type": pointer.get("source_type"),
                "label": pointer.get("label"),
                "targets": pointer.get("targets") if isinstance(pointer.get("targets"), list) else [],
            }
            pointer_refs.append(
                {
                    "sprint_item_id": str(item.get("id") or ""),
                    "pointer_id": str(pointer.get("id") or ""),
                    "source_type": pointer.get("source_type"),
                    "label": _safe_text(pointer.get("label"), limit=200),
                    "target_count": len(material["targets"]),
                    "pointer_sha256": _sha256(_canonical_json(material)),
                    "source_uri": (
                        f"meridian://project/{project_id}/sprint-items/"
                        f"{item.get('id')}/pointers/{pointer.get('id')}"
                    ),
                }
            )

    goal = await db_module.get_goal(db, project_id)
    goal_content = goal.get("content") if isinstance(goal, dict) else None
    if isinstance(goal_content, dict):
        goal_content = goal_content.get("content") or _canonical_json(goal_content)
    scope = {
        "project_name": _safe_text(project.get("name"), limit=300),
        "goal_content": _safe_text(goal_content, limit=4800),
        "goal_version": goal.get("version") if isinstance(goal, dict) else None,
        "north_star": _safe_text(goal.get("north_star"), limit=2400)
        if isinstance(goal, dict) else None,
        "sprint": _safe_text(goal.get("sprint"), limit=2400)
        if isinstance(goal, dict) else None,
        "north_star_inherited": bool(goal.get("north_star_inherited"))
        if isinstance(goal, dict) else False,
        "north_star_source_project_id": goal.get("north_star_source_project_id")
        if isinstance(goal, dict) else None,
    }
    return {
        "schema_version": 1,
        "project_id": project_id,
        "session": {
            "id": session_id,
            "name": _safe_text(session["name"], limit=200),
            "status": session["status"],
        },
        "captured_at": datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z"),
        "goal_scope": scope,
        "sprint_progress": board_snapshot,
        "durable_records": durable_records,
        "evidence_pointers": pointer_refs,
        "artifact_manifest": artifact_manifest,
        "escalation": escalation,
        "source_authority": {
            "board": "live_meridian_database",
            "project_scope": "live_meridian_database",
            "artifact_manifest": "unverified_local_client_report",
            "provider_history": "not_copied",
            "raw_chat": "not_copied",
            "artifact_bytes": "not_copied",
        },
    }


async def maybe_capture_project_state_milestone(
    db: Any,
    project_id: str,
    session_id: str,
    *,
    version: str | None = None,
    milestone_trigger: str | None = None,
    risk_signals: list[str] | None = None,
    artifact_manifest: Any = None,
) -> dict[str, Any]:
    """Escalate a checkpoint into a deep milestone when material or risky."""
    receipt = _manifest_receipt(artifact_manifest)
    signals = list(risk_signals or [])
    if receipt["status"] in {"degraded", "unavailable"}:
        signals.append("artifact_integrity_uncertain")
    recent = await _recent_milestone_count(db, project_id) if milestone_trigger or signals else 0
    escalation = assess_checkpoint_escalation(
        milestone_trigger=milestone_trigger,
        risk_signals=signals,
        recent_milestone_count=recent,
    )
    if not escalation["captured"]:
        return {"captured": False, "escalation": escalation}
    snapshot = await _build_snapshot(
        db,
        project_id,
        session_id,
        version=version,
        escalation=escalation,
        artifact_manifest=receipt,
    )
    record = await db_module.append_project_state_milestone(
        db,
        project_id,
        session_id,
        trigger=milestone_trigger or escalation["reason"],
        risk_score=escalation["risk_score"],
        risk_threshold=escalation["risk_threshold"],
        snapshot=snapshot,
    )
    return {
        "captured": True,
        "escalation": escalation,
        "milestone": record,
        "pointer": build_project_state_milestone_pointer(record),
    }
