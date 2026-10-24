"""Human-approval review gate in front of every Google Docs write (drafting v1).

Why this exists
---------------
A write to a Google Doc changes a document a person owns. The v1 rule is that
no such write reaches the Docs transport unless a human has reviewed the exact
``documents.batchUpdate`` body and approved it. The gate reuses the audited
override machinery that already backs the other gates in this codebase:

* ``gate_override.request_gate_override_hitl`` files the approval as a
  ``gate_override`` HITL with ``require_human=True``, so Meridian's auto-answer
  path can never approve it.
* ``gate_override.consume_gate_override_approval`` verifies the approval is
  genuine (same project, answered "yes", not auto-answered, not already spent)
  and spends it with a compare-and-swap, so one approval cannot be replayed.
* ``gate_override.record_override_audit`` appends one ``action_audit_log`` row
  per granted write.

What this module adds
---------------------
1. Every write is checked against :data:`WRITE_KINDS` before anything else.
2. The approval is bound to the SHA-256 of the canonical JSON of the exact
   batchUpdate body, together with the document id and the write kind. Changing
   the body after approval refuses the write, and the approval is not spent by
   the refused attempt.
3. An approval answered by an agent session (a session id registered to this
   project) or with no named answerer is refused. ``consume_gate_override_approval``
   already refuses the ``auto`` answerer; this module adds the agent-session case.
4. The transport is called only after every check above has passed.

Known limit
-----------
"Human" here means "not self-approvable through the auto-answer path or by an
agent session id". The ``answer_hitl`` tool and the ``/hitl`` endpoint cannot
tell a human from a holder of the same bearer token, so a compromised token can
still answer the approval. That is the same documented limit as
``gate_override`` and is out of scope for this slice.

A failure inside the transport call happens after the approval is spent and
after its audit row is written. The approval is then not reusable, and a new
review must be requested. Nothing is retried silently.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Protocol

from meridian import gate_override

#: ``gate`` value bound into the approval payload. Only this module files and
#: spends approvals with this gate name.
GDOCS_WRITE_GATE = "gdocs_write"

#: ``action_audit_log.event_type`` written once per granted write.
GDOCS_WRITE_GRANTED_EVENT_TYPE = "gdocs_write_review_granted"

#: The Google Docs write kinds this gate reviews. Anything else is refused.
WRITE_KINDS = frozenset({"insertText", "insertInlineImage", "caption"})

# Stable error codes. The approval-state codes match gate_override's codes so a
# caller can treat them the same way across gates.
REVIEW_REQUIRED = "REVIEW_REQUIRED"
WRITE_KIND_UNSUPPORTED = "WRITE_KIND_UNSUPPORTED"
WRITE_BODY_INVALID = "WRITE_BODY_INVALID"
DOCUMENT_ID_INVALID = "DOCUMENT_ID_INVALID"
BODY_HASH_MISMATCH = "BODY_HASH_MISMATCH"
HITL_NOT_FOUND = "HITL_NOT_FOUND"
HITL_NOT_ANSWERED = "HITL_NOT_ANSWERED"
HITL_NOT_APPROVED = "HITL_NOT_APPROVED"
HITL_INVALID = "HITL_INVALID"
HITL_ALREADY_USED = "HITL_ALREADY_USED"


class GDocsReviewError(ValueError):
    """A Google Docs write was refused before it reached the transport.

    ``code`` is a stable, machine-readable discriminator.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class GDocsTransport(Protocol):
    """The single Docs call this gate guards. The real adapter is not part of
    this checkout, so callers and tests supply their own implementation."""

    async def batch_update(self, document_id: str, body: dict[str, Any]) -> dict[str, Any]: ...


def _canonical(body: Any) -> str:
    """Canonical JSON for a batchUpdate body: sorted keys, no whitespace."""
    if not isinstance(body, dict):
        raise GDocsReviewError(WRITE_BODY_INVALID, "a batchUpdate body must be a JSON object")
    try:
        return json.dumps(
            body, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise GDocsReviewError(
            WRITE_BODY_INVALID, f"batchUpdate body is not plain JSON: {exc}",
        ) from exc


def body_sha256(body: Any) -> str:
    """SHA-256 hex digest of the canonical JSON of ``body``.

    Two dicts with the same content hash the same regardless of key order. Any
    change to content changes the digest.
    """
    return hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()


def _check_document_and_kind(document_id: str, write_kind: str) -> None:
    if not isinstance(document_id, str) or not document_id or "|" in document_id:
        raise GDocsReviewError(
            DOCUMENT_ID_INVALID, "document_id must be a non-empty string without '|'",
        )
    if write_kind not in WRITE_KINDS:
        raise GDocsReviewError(
            WRITE_KIND_UNSUPPORTED,
            f"write kind {write_kind!r} is not reviewable; supported kinds are "
            f"{sorted(WRITE_KINDS)}",
        )


def _subject(document_id: str, write_kind: str, digest: str) -> str:
    """The subject an approval is bound to: document, write kind and body hash."""
    return f"{document_id}|{write_kind}|{digest}"


def _describe(document_id: str, write_kind: str, digest: str) -> str:
    return f"Google Docs {write_kind} on document {document_id} (body sha256 {digest})"


async def request_write_review(
    db: Any,
    project_id: str,
    *,
    document_id: str,
    write_kind: str,
    body: dict[str, Any],
    requested_by: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """File, or reuse, the human-approval HITL for exactly this write.

    The approval question shows the full canonical body, so the reviewer sees
    what will be sent. The approval is bound to the body's SHA-256, so it covers
    this body only.
    """
    _check_document_and_kind(document_id, write_kind)
    digest = body_sha256(body)
    description = (
        f"Review the exact Google Docs batchUpdate body before approving. "
        f"Document: {document_id}. Write: {write_kind}. "
        f"Canonical body: {_canonical(body)}"
    )
    return await gate_override.request_gate_override_hitl(
        db,
        project_id,
        gate=GDOCS_WRITE_GATE,
        subject_id=_subject(document_id, write_kind, digest),
        reason=_describe(document_id, write_kind, digest),
        description=description,
        session_id=session_id,
        requested_by=requested_by,
    )


async def _is_agent_session(db: Any, project_id: str, candidate: str) -> bool:
    """True when ``candidate`` is a session id registered to this project."""
    async with db.execute(
        "SELECT id FROM sessions WHERE id = ? AND project_id = ?", (candidate, project_id),
    ) as cur:
        return (await cur.fetchone()) is not None


async def gated_batch_update(
    db: Any,
    transport: GDocsTransport,
    project_id: str,
    *,
    document_id: str,
    write_kind: str,
    body: dict[str, Any],
    approval_hitl_id: str | None,
    actor: str | None = None,
) -> dict[str, Any]:
    """Send one Google Docs write, but only with a human-approved review.

    Every refusal happens before ``transport.batch_update`` is called. On a
    grant, the approval is spent and one audit row is written, then the body is
    sent exactly as reviewed. Returns ``{"result", "hitl_id", "body_sha256",
    "approved_by"}``.
    """
    from meridian import db as db_module  # noqa: PLC0415 — avoid import cycle

    _check_document_and_kind(document_id, write_kind)
    digest = body_sha256(body)
    subject = _subject(document_id, write_kind, digest)

    hid = (approval_hitl_id or "").strip()
    if not hid:
        raise GDocsReviewError(
            REVIEW_REQUIRED,
            f"{write_kind} on document {document_id} needs a human-approved review. "
            "Call request_write_review and wait for a human to answer Yes.",
        )

    hitl = await db_module.get_hitl_request(db, hid)
    if hitl is None or hitl.get("project_id") != project_id:
        # Same answer for "no such id" and "another project's id".
        raise GDocsReviewError(HITL_NOT_FOUND, f"review HITL {hid!r} not found in this project")

    payload = gate_override._loads_payload(hitl.get("payload"))  # noqa: SLF001
    bound = payload.get("subject_id")
    if payload.get("gate") == GDOCS_WRITE_GATE and isinstance(bound, str):
        parts = bound.split("|")
        if (
            len(parts) == 3
            and parts[0] == document_id
            and parts[1] == write_kind
            and parts[2] != digest
        ):
            raise GDocsReviewError(
                BODY_HASH_MISMATCH,
                f"the body was changed after review: the approval covers sha256 {parts[2]}, "
                f"this body is sha256 {digest}. Request a new review for the new body.",
            )

    if hitl.get("status") == "answered":
        answered_by = (hitl.get("answered_by") or "").strip()
        if not answered_by or answered_by.lower() == "auto":
            raise GDocsReviewError(
                HITL_NOT_APPROVED,
                f"review HITL {hid!r} has no named human answerer (auto-answer or unattributed) "
                "and cannot authorize a Google Docs write",
            )
        if await _is_agent_session(db, project_id, answered_by):
            raise GDocsReviewError(
                HITL_NOT_APPROVED,
                f"review HITL {hid!r} was answered by an agent session; only a human may "
                "approve a Google Docs write",
            )

    try:
        spent = await gate_override.consume_gate_override_approval(
            db, project_id, hid, gate=GDOCS_WRITE_GATE, subject_id=subject, consumed_by=actor,
        )
    except gate_override.GateOverrideError as exc:
        raise GDocsReviewError(exc.code, str(exc)) from exc

    reason = str(payload.get("override_reason") or "").strip() or _describe(
        document_id, write_kind, digest,
    )
    await gate_override.record_override_audit(
        db,
        GDOCS_WRITE_GRANTED_EVENT_TYPE,
        project_id,
        subject_id=subject,
        actor=actor,
        reason=reason,
        extra={
            "document_id": document_id,
            "write_kind": write_kind,
            "body_sha256": digest,
            "hitl_id": hid,
            "approved_by": spent["answered_by"],
        },
    )

    result = await transport.batch_update(document_id, body)
    return {
        "result": result,
        "hitl_id": hid,
        "body_sha256": digest,
        "approved_by": spent["answered_by"],
    }
