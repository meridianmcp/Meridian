"""Database-bound half of the image-storage rights policy.

:func:`meridian.rights_policy.decide_image_storage` is pure and cannot tell a
human from an agent. This module makes the override real, using the existing
gate-override mechanism (:mod:`meridian.gate_override`):

* :func:`request_rights_override` files the approval request as a
  ``gate_override`` HITL with ``require_human=True``. Meridian's auto-answer
  never touches it.
* :func:`authorize_image_storage` is the one call a byte-writing path makes
  before it writes image bytes. It verifies the answered approval (human "yes",
  bound to this gate and subject, not auto-answered, single use), refuses agent
  approvers, and writes the ``action_audit_log`` row. Any failure raises, so
  the write does not proceed.

Known limit (inherited from ``gate_override``): the approval is human-bound
against auto-answer, not against a holder of the same bearer token, which can
still answer the HITL.
"""
from __future__ import annotations

from typing import Any

from meridian import gate_override
from meridian.rights_policy import (
    RIGHTS_OVERRIDE_EVENT_TYPE,
    RIGHTS_OVERRIDE_GATE,
    ImageStorageDecision,
    RightsOverride,
    RightsPolicyError,
    decide_image_storage,
)


def _subject(subject_id: Any) -> str:
    text = subject_id.strip() if isinstance(subject_id, str) else ""
    if not text:
        raise RightsPolicyError(
            "OVERRIDE_INVALID", "an override must name its subject (figure, table or source id)",
        )
    return text


def _wrap(exc: gate_override.GateOverrideError) -> RightsPolicyError:
    return RightsPolicyError(exc.code, str(exc))


async def request_rights_override(
    db: Any,
    project_id: str,
    *,
    subject_id: str,
    reason: str | None,
    description: str,
    requested_by: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """File, or reuse, the human-approval HITL for one image-storage override.

    Returns the HITL row. Its ``id`` is what the caller passes back as
    ``hitl_id`` once a human has answered "yes".
    """
    subject = _subject(subject_id)
    try:
        return await gate_override.request_gate_override_hitl(
            db,
            project_id,
            gate=RIGHTS_OVERRIDE_GATE,
            subject_id=subject,
            reason=reason or "",
            description=description,
            session_id=session_id,
            requested_by=requested_by,
        )
    except gate_override.GateOverrideError as exc:
        raise _wrap(exc) from exc


async def authorize_image_storage(
    db: Any,
    project_id: str,
    raw_license: Any,
    *,
    subject_id: str,
    derived: bool = False,
    redrawn: bool = False,
    use_mode: str | None = None,
    hitl_id: str | None = None,
    reason: str | None = None,
    consumed_by: str | None = None,
) -> ImageStorageDecision:
    """Decide whether a byte write may proceed, and audit any human override.

    Call this before writing image bytes. ``use_mode`` is the project's use
    mode; a commercialized project must pass ``COMMERCIALIZED``, or the gate
    will not apply. With no tier refusal, or with no ``hitl_id``, it returns
    the decision and writes nothing. With a ``hitl_id`` and a refusal, it:

    1. requires a non-empty ``reason`` (checked before the approval is spent),
    2. verifies and spends the approval through ``consume_gate_override_approval``,
    3. builds a :class:`RightsOverride` whose ``approved_by`` is the HITL's own
       ``answered_by``, never a caller-supplied string,
    4. re-decides, refusing agent approvers, and
    5. writes the audit row. A failed audit write raises, so no override proceeds.
    """
    # With no use_mode given, the policy's own default applies, so this module
    # never keeps a second copy of it.
    mode_kwargs = {} if use_mode is None else {"use_mode": use_mode}
    decision = decide_image_storage(raw_license, derived=derived, redrawn=redrawn, **mode_kwargs)
    hid = hitl_id.strip() if isinstance(hitl_id, str) else ""
    if decision.allowed or not hid:
        return decision
    subject = _subject(subject_id)
    try:
        approved_reason = gate_override.require_override_reason(reason, flag=RIGHTS_OVERRIDE_GATE)
    except gate_override.GateOverrideError as exc:
        raise _wrap(exc) from exc
    try:
        approval = await gate_override.consume_gate_override_approval(
            db,
            project_id,
            hid,
            gate=RIGHTS_OVERRIDE_GATE,
            subject_id=subject,
            consumed_by=consumed_by,
        )
    except gate_override.GateOverrideError as exc:
        raise _wrap(exc) from exc
    override = RightsOverride(
        hitl_id=hid,
        subject_id=subject,
        approved_by=str(approval.get("answered_by") or ""),
        reason=approved_reason,
    )
    approved = decide_image_storage(
        raw_license, derived=derived, redrawn=redrawn, override=override, **mode_kwargs,
    )
    await gate_override.record_override_audit(
        db,
        RIGHTS_OVERRIDE_EVENT_TYPE,
        project_id,
        subject_id=subject,
        actor=override.approved_by,
        reason=approved_reason,
        extra={
            "hitl_id": hid,
            "tier": approved.tier,
            "license": approved.license,
            "derived": approved.derived,
            "redrawn": approved.redrawn,
        },
    )
    return approved
