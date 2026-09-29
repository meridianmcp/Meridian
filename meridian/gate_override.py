"""0ff5e59f — audited, human-bound gate overrides (a deliberately small slice).

Why this exists
---------------
The 2026-09-27 enforcement audit found that several completion/wave gates are
HARD but self-attestable: the same agent that is being gated can also pass the
override flag, type the evidence, or (because ``hitl_auto_answer`` defaults on)
have its own override HITL auto-answered. This module is the shared building
block for closing that gap, layered on machinery that already exists:

* ``db.request_hitl(require_human=True)`` — a HITL filed this way is
  structurally exempt from Meridian's auto-answer (e43e6941), so the agent side
  can never approve it through the auto-answer path.
* ``db.record_action_audit_event`` — the append-only ``action_audit_log`` that
  every other override in this codebase (strict evidence, code-intel receipt,
  test-run receipt, merge approval) already writes to.
* The completed-``require_human`` HITL verification precedent from cd495afa
  (``db.workspace.set_manual_issue_screening_enabled``).

What it provides
----------------
1. :func:`require_override_reason` — every override needs a non-empty reason.
2. :func:`record_override_audit` — the audit-log write for an override.
3. :func:`request_gate_override_hitl` — files the approval request. ``kind`` and
   ``require_human=True`` are HARDCODED here (mirrors
   ``request_manual_issue_screening_toggle``): neither the MCP ``request_hitl``
   tool (it clamps ``kind`` to question/correction) nor ``POST /hitl`` (it does
   not accept ``kind``) can mint a ``gate_override`` HITL, so the only writer of
   this kind is the server-side gate that actually needs the approval.
4. :func:`consume_gate_override_approval` — verifies an approval is genuine
   (right project, right kind, answered "yes", ``require_human`` at filing time,
   bound to THIS gate and THIS subject, never auto-answered) and marks it used
   so a single approval cannot be replayed against a second gate.

Known limit (documented, not hidden)
------------------------------------
"Human-bound" here means "not self-approvable through the auto-answer path".
The ``answer_hitl`` MCP tool and ``PATCH /hitl/{id}`` both funnel through
``server._answer_hitl_and_apply`` and neither can tell a human from an agent
holding the same token, so a fully compromised token can still answer this
HITL. Closing that needs an auth-channel distinction (dashboard session vs.
bearer token) which is a larger design than this slice; the same limit applies
to the cd495afa manual-issue-screening toggle this mirrors.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

#: HITL ``kind`` for an override approval request. Server-authored only.
GATE_OVERRIDE_HITL_KIND = "gate_override"

#: Stable gate identifiers (bound into the HITL payload so an approval for one
#: gate/subject can never be replayed against another).
GATE_WAVE_GATE_UNBOUND_PAYLOAD = "wave_gate_unbound_payload"

#: ``action_audit_log.event_type`` values written by :func:`record_override_audit`.
WAVE_GATE_UNBOUND_PAYLOAD_EVENT_TYPE = "wave_gate_unbound_payload_override"
CI_OVERRIDE_EVENT_TYPE = "sprint_item_ci_override"
FOREIGN_CLAIM_OVERRIDE_EVENT_TYPE = "sprint_item_foreign_claim_override"
PROSPECT_BYPASS_OVERRIDE_EVENT_TYPE = "sprint_item_prospect_bypass_override"

_YES_OPTION = "Yes — approve this override"
_NO_OPTION = "No — do not approve"


def wave_gate_subject(wave_label: str | None, version: str | None) -> str:
    """The subject string an unbound-payload wave-gate approval is bound to —
    ONE definition, shared by the handler that files the approval request and
    the DB function that spends it, so the two can never disagree."""
    return f"{(wave_label or '').strip()}|{(version or '').strip()}"


class GateOverrideError(ValueError):
    """An override was requested without the approval/reason it requires.

    ``code`` is a stable machine-readable discriminator (``OVERRIDE_REASON_
    REQUIRED``, ``HUMAN_APPROVAL_REQUIRED``, ``HITL_NOT_FOUND``,
    ``HITL_NOT_ANSWERED``, ``HITL_NOT_APPROVED``, ``HITL_INVALID``,
    ``HITL_ALREADY_USED``) so a handler can render a precise error and an
    executor can react without parsing prose.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def require_override_reason(reason: str | None, *, flag: str = "override") -> str:
    """Return ``reason`` stripped, or raise :class:`GateOverrideError` when it
    is missing/blank. An override with no stated reason is not auditable."""
    text = (reason or "").strip()
    if not text:
        raise GateOverrideError(
            "OVERRIDE_REASON_REQUIRED",
            f"{flag} requires a non-empty override_reason — an override with no "
            "stated reason is not auditable and is refused.",
        )
    return text


async def record_override_audit(
    db: Any,
    event_type: str,
    project_id: str,
    *,
    subject_id: str | None,
    actor: str | None,
    reason: str | None,
    tenant_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append one ``action_audit_log`` row recording who overrode what and why.

    ``reason`` is REQUIRED (:func:`require_override_reason`). Raises rather than
    swallowing a write failure: a silently-dropped audit entry would defeat the
    point of an audit trail, so callers let this propagate and the override
    does not proceed.
    """
    text = require_override_reason(reason, flag=event_type)
    from meridian import db as db_module  # noqa: PLC0415 — avoid import cycle

    detail: dict[str, Any] = {"subject_id": subject_id, "reason": text}
    if extra:
        detail.update(extra)
    return await db_module.record_action_audit_event(
        db,
        event_type,
        tenant_id=tenant_id,
        project_id=project_id,
        actor=actor,
        detail=json.dumps(detail, default=str),
    )


def _loads_payload(raw: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


async def _find_pending_override_hitl(
    db: Any, project_id: str, gate: str, subject_id: str, reason: str,
) -> dict[str, Any] | None:
    """An already-pending, still-unanswered approval request for the SAME gate,
    subject and reason — reused so an executor retrying does not spam the human
    queue with duplicates."""
    from meridian import db as db_module  # noqa: PLC0415

    async with db.execute(
        "SELECT * FROM hitl_requests WHERE project_id = ? AND kind = ? "
        "AND status = 'pending' ORDER BY created_at DESC LIMIT 50",
        (project_id, GATE_OVERRIDE_HITL_KIND),
    ) as cur:
        rows = await cur.fetchall()
    for row in rows:
        hitl = db_module._row_to_dict(row) or {}  # noqa: SLF001
        payload = _loads_payload(hitl.get("payload"))
        if (
            payload.get("require_human") is True
            and payload.get("gate") == gate
            and payload.get("subject_id") == subject_id
            and payload.get("override_reason") == reason
        ):
            return hitl
    return None


async def request_gate_override_hitl(
    db: Any,
    project_id: str,
    *,
    gate: str,
    subject_id: str,
    reason: str,
    description: str,
    session_id: str | None = None,
    requested_by: str | None = None,
) -> dict[str, Any]:
    """File (or reuse) the human-approval HITL for one gate override.

    ``kind`` and ``require_human=True`` are hardcoded — a caller cannot weaken
    either — so Meridian's own auto-answer machinery is structurally forbidden
    from approving it. Returns the HITL row (its ``id`` is what the executor
    passes back as ``override_hitl_id`` once a human has answered "yes").
    """
    from meridian import db as db_module  # noqa: PLC0415

    text = require_override_reason(reason, flag=gate)
    existing = await _find_pending_override_hitl(db, project_id, gate, subject_id, text)
    if existing is not None:
        return existing
    question = (
        f"Approve gate override? Gate: {gate}. Subject: {subject_id}. "
        f"{description} Requested reason: {text}. "
        "Answer Yes ONLY if a human has reviewed this and accepts the override; "
        "an agent must not answer this request itself."
    )
    return await db_module.request_hitl(
        db,
        project_id,
        question,
        session_id=session_id,
        context=description,
        urgency="high",
        kind=GATE_OVERRIDE_HITL_KIND,
        options=[_YES_OPTION, _NO_OPTION],
        recommended=_NO_OPTION,
        require_human=True,
        payload=json.dumps({
            "gate": gate,
            "subject_id": subject_id,
            "override_reason": text,
            "requested_by": requested_by,
        }),
    )


async def consume_gate_override_approval(
    db: Any,
    project_id: str,
    hitl_id: str | None,
    *,
    gate: str,
    subject_id: str,
    consumed_by: str | None = None,
) -> dict[str, Any]:
    """Verify ``hitl_id`` is a genuine human approval for THIS gate/subject and
    mark it used. Raises :class:`GateOverrideError` (nothing consumed) unless
    the request:

    1. exists in this project,
    2. is ``kind == 'gate_override'``,
    3. is ``status == 'answered'``,
    4. was filed with ``require_human: true`` (persisted at filing time by
       ``request_hitl`` — never forgeable after the fact by an answer),
    5. is bound to the same ``gate`` and ``subject_id``,
    6. was not answered by the auto-answer machinery,
    7. carries an answer that affirms ("yes…"),
    8. has not already been consumed.

    Consumption is a compare-and-swap on the stored payload, so two concurrent
    callers cannot both spend one approval.
    """
    from meridian import db as db_module  # noqa: PLC0415

    hid = (hitl_id or "").strip()
    if not hid:
        raise GateOverrideError(
            "HUMAN_APPROVAL_REQUIRED",
            f"{gate} requires a human-approved override: pass override_hitl_id "
            "referencing an answered gate_override HITL.",
        )
    async with db.execute("SELECT * FROM hitl_requests WHERE id = ?", (hid,)) as cur:
        row = await cur.fetchone()
    hitl = db_module._row_to_dict(row)  # noqa: SLF001
    if hitl is None or hitl.get("project_id") != project_id:
        # Same answer for "no such id" and "another project's id" — never
        # confirm the existence of a foreign project's HITL.
        raise GateOverrideError("HITL_NOT_FOUND", f"override HITL {hid!r} not found in this project")
    if hitl.get("kind") != GATE_OVERRIDE_HITL_KIND:
        raise GateOverrideError(
            "HITL_INVALID",
            f"HITL {hid!r} is kind={hitl.get('kind')!r}, not {GATE_OVERRIDE_HITL_KIND!r} — "
            "it cannot authorize a gate override",
        )
    if hitl.get("status") != "answered":
        raise GateOverrideError(
            "HITL_NOT_ANSWERED",
            f"override HITL {hid!r} has not been answered yet (status="
            f"{hitl.get('status')!r}) — wait for a human to answer it",
        )
    raw_payload = hitl.get("payload")
    payload = _loads_payload(raw_payload)
    if payload.get("require_human") is not True:
        raise GateOverrideError(
            "HITL_INVALID",
            f"override HITL {hid!r} was not filed with require_human=True — refusing to "
            "trust it as a self-approval-proof authorization",
        )
    if payload.get("gate") != gate or payload.get("subject_id") != subject_id:
        raise GateOverrideError(
            "HITL_INVALID",
            f"override HITL {hid!r} approves gate={payload.get('gate')!r} "
            f"subject={payload.get('subject_id')!r}, not gate={gate!r} subject="
            f"{subject_id!r} — an approval cannot be reused for a different override",
        )
    if payload.get("override_consumed"):
        raise GateOverrideError(
            "HITL_ALREADY_USED",
            f"override HITL {hid!r} was already used once — request a new approval",
        )
    if (hitl.get("answered_by") or "").strip().lower() == "auto":
        raise GateOverrideError(
            "HITL_NOT_APPROVED",
            f"override HITL {hid!r} was auto-answered, not answered by a human",
        )
    if not (hitl.get("answer") or "").strip().lower().startswith("yes"):
        raise GateOverrideError(
            "HITL_NOT_APPROVED",
            f"override HITL {hid!r} answer ({hitl.get('answer')!r}) does not approve the override",
        )

    consumed = dict(payload)
    consumed["override_consumed"] = {
        "at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "by": consumed_by,
    }
    cursor = await db.execute(
        "UPDATE hitl_requests SET payload = ? WHERE id = ? AND payload = ?",
        (json.dumps(consumed), hid, raw_payload),
    )
    await db.commit()
    if cursor.rowcount == 0:
        raise GateOverrideError(
            "HITL_ALREADY_USED",
            f"override HITL {hid!r} was used concurrently by another call",
        )
    return {
        "hitl_id": hid,
        "answer": hitl.get("answer"),
        "answered_by": hitl.get("answered_by"),
        "answered_at": hitl.get("answered_at"),
    }
