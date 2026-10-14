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
    COMMERCIALIZED,
    RIGHTS_OVERRIDE_EVENT_TYPE,
    RIGHTS_OVERRIDE_GATE,
    STANDARD_PUBLICATION,
    ImageStorageDecision,
    RightsOverride,
    RightsPolicyError,
    decide_image_storage,
    normalize_use_mode,
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


async def _effective_use_mode(db: Any, project_id: str, requested: str | None) -> str:
    """The use mode the gate applies to this project.

    COMMERCIALIZED if EITHER the project's stored mode OR the explicit ``requested``
    argument is commercialized, otherwise STANDARD_PUBLICATION. The stored mode is
    read from the project row, so a caller cannot unlock a commercialized project
    by passing standard. A missing project is refused with PROJECT_NOT_FOUND. An
    unknown explicit value raises USE_MODE_INVALID. A stored value that is not
    exactly standard_publication (blank, unknown, or written by raw SQL) is treated
    as commercialized, so bad data can only tighten the gate, never loosen it.
    """
    from meridian import db as db_module  # noqa: PLC0415 — avoid import cycle

    try:
        stored = await db_module.get_project_use_mode(db, project_id)
    except ValueError as exc:
        raise RightsPolicyError(
            "PROJECT_NOT_FOUND",
            f"project {project_id!r} does not exist; no image bytes can be stored against it",
        ) from exc
    explicit_mode = normalize_use_mode(requested)
    stored_is_standard = stored == STANDARD_PUBLICATION
    if not stored_is_standard or explicit_mode == COMMERCIALIZED:
        return COMMERCIALIZED
    return STANDARD_PUBLICATION


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

    Call this before writing image bytes. The gate applies COMMERCIALIZED when
    EITHER the project's stored use mode (see ``db.set_project_use_mode``) OR the
    explicit ``use_mode`` argument is commercialized, and STANDARD_PUBLICATION
    otherwise. A missing project raises PROJECT_NOT_FOUND before anything is
    decided or written. With no tier refusal, or with no ``hitl_id``, it returns
    the decision and writes nothing. With a ``hitl_id`` and a refusal, it:

    1. requires a non-empty ``reason`` (checked before the approval is spent),
    2. verifies and spends the approval through ``consume_gate_override_approval``,
    3. builds a :class:`RightsOverride` whose ``approved_by`` is the HITL's own
       ``answered_by``, never a caller-supplied string,
    4. re-decides, refusing agent approvers, and
    5. writes the audit row. A failed audit write raises, so no override proceeds.
    """
    effective_mode = await _effective_use_mode(db, project_id, use_mode)
    decision = decide_image_storage(
        raw_license, derived=derived, redrawn=redrawn, use_mode=effective_mode,
    )
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
        raw_license, derived=derived, redrawn=redrawn, override=override,
        use_mode=effective_mode,
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
