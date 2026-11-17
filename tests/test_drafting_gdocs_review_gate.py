"""Human-approval review gate in front of every Google Docs write.

Covers the v1 acceptance for the drafting write gate:

  A. A write with no recorded human approval raises REVIEW_REQUIRED before any
     transport call. The fake transport records zero calls.
  B. The approval is a require_human=True gate_override HITL. An approval
     answered by auto-answer, by an agent session, or by no named answerer is
     refused.
  C. The approval binds to the SHA-256 of the exact batchUpdate body. Any change
     to the body after approval refuses the write and does not spend the
     approval.
  D. An approval is single-use. Each granted write appends exactly one
     action_audit_log row.
"""
from __future__ import annotations

import json

import pytest

from meridian import db as db_module
from meridian import gate_override
from meridian.drafting import gdocs_review_gate as gate

_YES = "Yes — approve this override"
_NO = "No — do not approve"


class _FakeTransport:
    """Records every call. A gated write that reaches it is visible in ``calls``."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def batch_update(self, document_id: str, body: dict) -> dict:
        self.calls.append((document_id, body))
        return {"replies": [{}]}


def _insert_text(text: str, index: int = 1) -> dict:
    return {
        "requests": [{"insertText": {"location": {"index": index}, "text": text}}],
        "writeControl": {"requiredRevisionId": "rev-1"},
    }


def _insert_image(uri: str = "https://example.com/figure.png") -> dict:
    return {
        "requests": [{"insertInlineImage": {"location": {"index": 5}, "uri": uri}}],
        "writeControl": {"requiredRevisionId": "rev-1"},
    }


async def _project(db, name: str) -> str:
    proj = await db_module.create_project(db, name)
    return proj["id"]


async def _file_review(db, pid, *, body, write_kind="insertText", doc="doc-1"):
    return await gate.request_write_review(
        db, pid, document_id=doc, write_kind=write_kind, body=body, requested_by="executor",
    )


async def _approved(db, pid, *, body, answer=_YES, answered_by="adam",
                    write_kind="insertText", doc="doc-1") -> str:
    hitl = await _file_review(db, pid, body=body, write_kind=write_kind, doc=doc)
    await db_module.answer_hitl_request(db, hitl["id"], answer, answered_by=answered_by)
    return hitl["id"]


async def _audit_rows(db, pid):
    return await db_module.get_action_audit_log(
        db, project_id=pid, event_type=gate.GDOCS_WRITE_GRANTED_EVENT_TYPE,
    )


# ---------------------------------------------------------------------------
# A. No approval means no transport call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_without_approval_is_refused_before_transport(db):
    pid = await _project(db, "gdocs-no-approval")
    transport = _FakeTransport()
    with pytest.raises(gate.GDocsReviewError) as exc:
        await gate.gated_batch_update(
            db, transport, pid, document_id="doc-1", write_kind="insertText",
            body=_insert_text("hello"), approval_hitl_id=None,
        )
    assert exc.value.code == gate.REVIEW_REQUIRED
    assert transport.calls == []
    assert await _audit_rows(db, pid) == []


@pytest.mark.asyncio
async def test_write_with_blank_approval_id_is_refused_before_transport(db):
    pid = await _project(db, "gdocs-blank-approval")
    transport = _FakeTransport()
    with pytest.raises(gate.GDocsReviewError) as exc:
        await gate.gated_batch_update(
            db, transport, pid, document_id="doc-1", write_kind="insertText",
            body=_insert_text("hello"), approval_hitl_id="   ",
        )
    assert exc.value.code == gate.REVIEW_REQUIRED
    assert transport.calls == []


@pytest.mark.asyncio
async def test_unanswered_review_does_not_authorize_a_write(db):
    pid = await _project(db, "gdocs-unanswered")
    body = _insert_text("hello")
    pending = await _file_review(db, pid, body=body)
    transport = _FakeTransport()
    with pytest.raises(gate.GDocsReviewError) as exc:
        await gate.gated_batch_update(
            db, transport, pid, document_id="doc-1", write_kind="insertText",
            body=body, approval_hitl_id=pending["id"],
        )
    assert exc.value.code == gate.HITL_NOT_ANSWERED
    assert transport.calls == []


@pytest.mark.asyncio
async def test_a_no_answer_does_not_authorize_a_write(db):
    pid = await _project(db, "gdocs-no-answer")
    body = _insert_text("hello")
    hid = await _approved(db, pid, body=body, answer=_NO, answered_by="adam")
    transport = _FakeTransport()
    with pytest.raises(gate.GDocsReviewError) as exc:
        await gate.gated_batch_update(
            db, transport, pid, document_id="doc-1", write_kind="insertText",
            body=body, approval_hitl_id=hid,
        )
    assert exc.value.code == gate.HITL_NOT_APPROVED
    assert transport.calls == []


@pytest.mark.asyncio
async def test_approval_from_another_project_is_not_found(db):
    pid_a = await _project(db, "gdocs-proj-a")
    pid_b = await _project(db, "gdocs-proj-b")
    body = _insert_text("hello")
    hid = await _approved(db, pid_a, body=body)
    transport = _FakeTransport()
    with pytest.raises(gate.GDocsReviewError) as exc:
        await gate.gated_batch_update(
            db, transport, pid_b, document_id="doc-1", write_kind="insertText",
            body=body, approval_hitl_id=hid,
        )
    assert exc.value.code == gate.HITL_NOT_FOUND
    assert transport.calls == []


@pytest.mark.asyncio
async def test_unsupported_write_kind_is_refused_on_request_and_write(db):
    pid = await _project(db, "gdocs-bad-kind")
    body = {"requests": [{"deleteContentRange": {"range": {"startIndex": 1, "endIndex": 2}}}]}
    with pytest.raises(gate.GDocsReviewError) as exc:
        await _file_review(db, pid, body=body, write_kind="deleteContentRange")
    assert exc.value.code == gate.WRITE_KIND_UNSUPPORTED
    transport = _FakeTransport()
    with pytest.raises(gate.GDocsReviewError) as exc:
        await gate.gated_batch_update(
            db, transport, pid, document_id="doc-1", write_kind="deleteContentRange",
            body=body, approval_hitl_id="whatever",
        )
    assert exc.value.code == gate.WRITE_KIND_UNSUPPORTED
    assert transport.calls == []


@pytest.mark.asyncio
async def test_document_id_containing_separator_is_refused(db):
    pid = await _project(db, "gdocs-bad-doc")
    with pytest.raises(gate.GDocsReviewError) as exc:
        await _file_review(db, pid, body=_insert_text("x"), doc="doc|1")
    assert exc.value.code == gate.DOCUMENT_ID_INVALID


# ---------------------------------------------------------------------------
# B. The approval is a require_human HITL and agent or auto answers are refused
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_review_is_a_require_human_hitl_that_auto_answer_cannot_touch(db):
    pid = await _project(db, "gdocs-hitl-shape")
    await db_module.update_project_settings(db, pid, hitl_auto_answer=2)
    body = _insert_text("hello")
    hitl = await _file_review(db, pid, body=body)
    row = await db_module.get_hitl_request(db, hitl["id"])
    assert row["kind"] == gate_override.GATE_OVERRIDE_HITL_KIND
    assert row["status"] == "pending", "a write review must never be auto-answered"
    assert row["answered_by"] is None
    payload = json.loads(row["payload"])
    assert payload["require_human"] is True
    assert payload["gate"] == gate.GDOCS_WRITE_GATE
    # Filing the same body again reuses the pending review instead of spamming the queue.
    again = await _file_review(db, pid, body=body)
    assert again["id"] == hitl["id"]


@pytest.mark.asyncio
async def test_approval_answered_by_auto_is_refused(db):
    pid = await _project(db, "gdocs-auto")
    body = _insert_text("hello")
    hid = await _approved(db, pid, body=body, answered_by="auto")
    transport = _FakeTransport()
    with pytest.raises(gate.GDocsReviewError) as exc:
        await gate.gated_batch_update(
            db, transport, pid, document_id="doc-1", write_kind="insertText",
            body=body, approval_hitl_id=hid,
        )
    assert exc.value.code == gate.HITL_NOT_APPROVED
    assert transport.calls == []
    assert await _audit_rows(db, pid) == []


@pytest.mark.asyncio
async def test_approval_answered_by_an_agent_session_is_refused(db):
    pid = await _project(db, "gdocs-agent")
    agent = await db_module.register_session(db, pid, "executor-session")
    body = _insert_text("hello")
    hid = await _approved(db, pid, body=body, answered_by=agent["id"])
    transport = _FakeTransport()
    with pytest.raises(gate.GDocsReviewError) as exc:
        await gate.gated_batch_update(
            db, transport, pid, document_id="doc-1", write_kind="insertText",
            body=body, approval_hitl_id=hid,
        )
    assert exc.value.code == gate.HITL_NOT_APPROVED
    assert "agent session" in str(exc.value)
    assert transport.calls == []
    assert await _audit_rows(db, pid) == []


@pytest.mark.asyncio
async def test_approval_with_no_named_answerer_is_refused(db):
    pid = await _project(db, "gdocs-unattributed")
    body = _insert_text("hello")
    hid = await _approved(db, pid, body=body, answered_by="")
    transport = _FakeTransport()
    with pytest.raises(gate.GDocsReviewError) as exc:
        await gate.gated_batch_update(
            db, transport, pid, document_id="doc-1", write_kind="insertText",
            body=body, approval_hitl_id=hid,
        )
    assert exc.value.code == gate.HITL_NOT_APPROVED
    assert transport.calls == []


@pytest.mark.asyncio
async def test_a_hitl_of_another_kind_cannot_authorize_a_write(db):
    pid = await _project(db, "gdocs-wrong-kind")
    body = _insert_text("hello")
    filed = await db_module.request_hitl(
        db, pid, "unrelated question", kind="question", require_human=True,
    )
    await db_module.answer_hitl_request(db, filed["id"], _YES, answered_by="adam")
    transport = _FakeTransport()
    with pytest.raises(gate.GDocsReviewError) as exc:
        await gate.gated_batch_update(
            db, transport, pid, document_id="doc-1", write_kind="insertText",
            body=body, approval_hitl_id=filed["id"],
        )
    assert exc.value.code == gate.HITL_INVALID
    assert transport.calls == []


# ---------------------------------------------------------------------------
# C. The approval binds to the exact body
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_body_changed_after_approval_is_refused_and_approval_is_not_spent(db):
    pid = await _project(db, "gdocs-body-change")
    reviewed = _insert_text("reviewed text")
    changed = _insert_text("swapped text")
    hid = await _approved(db, pid, body=reviewed)
    transport = _FakeTransport()

    with pytest.raises(gate.GDocsReviewError) as exc:
        await gate.gated_batch_update(
            db, transport, pid, document_id="doc-1", write_kind="insertText",
            body=changed, approval_hitl_id=hid,
        )
    assert exc.value.code == gate.BODY_HASH_MISMATCH
    assert transport.calls == []
    assert await _audit_rows(db, pid) == []

    # The refused attempt did not spend the approval, so the reviewed body still goes.
    out = await gate.gated_batch_update(
        db, transport, pid, document_id="doc-1", write_kind="insertText",
        body=reviewed, approval_hitl_id=hid,
    )
    assert out["hitl_id"] == hid
    assert len(transport.calls) == 1


@pytest.mark.asyncio
async def test_body_hash_is_canonical_and_content_sensitive():
    a = {"requests": [{"insertText": {"text": "x", "location": {"index": 1}}}]}
    b = {"requests": [{"insertText": {"location": {"index": 1}, "text": "x"}}]}
    assert gate.body_sha256(a) == gate.body_sha256(b)
    changed = {"requests": [{"insertText": {"location": {"index": 2}, "text": "x"}}]}
    assert gate.body_sha256(a) != gate.body_sha256(changed)
    assert len(gate.body_sha256(a)) == 64


@pytest.mark.asyncio
async def test_non_object_or_non_json_body_is_refused():
    with pytest.raises(gate.GDocsReviewError) as exc:
        gate.body_sha256(["not", "an", "object"])
    assert exc.value.code == gate.WRITE_BODY_INVALID
    with pytest.raises(gate.GDocsReviewError) as exc:
        gate.body_sha256({"requests": [{"text": object()}]})
    assert exc.value.code == gate.WRITE_BODY_INVALID


@pytest.mark.asyncio
async def test_approval_for_one_write_kind_cannot_be_used_for_another(db):
    pid = await _project(db, "gdocs-kind-bind")
    body = _insert_text("hello")
    hid = await _approved(db, pid, body=body, write_kind="insertText")
    transport = _FakeTransport()
    with pytest.raises(gate.GDocsReviewError):
        await gate.gated_batch_update(
            db, transport, pid, document_id="doc-1", write_kind="caption",
            body=body, approval_hitl_id=hid,
        )
    assert transport.calls == []


# ---------------------------------------------------------------------------
# D. Single-use, one audit row per granted write, every write kind
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("write_kind", "body"),
    [
        ("insertText", _insert_text("hello")),
        ("insertInlineImage", _insert_image()),
        ("caption", _insert_text("Figure 1. Caption text", index=40)),
    ],
)
async def test_human_yes_grants_one_write_and_appends_one_audit_row(db, write_kind, body):
    pid = await _project(db, f"gdocs-grant-{write_kind}")
    hid = await _approved(db, pid, body=body, write_kind=write_kind, answered_by="adam")
    transport = _FakeTransport()

    out = await gate.gated_batch_update(
        db, transport, pid, document_id="doc-1", write_kind=write_kind,
        body=body, approval_hitl_id=hid, actor="executor-1",
    )

    assert out["result"] == {"replies": [{}]}
    assert out["hitl_id"] == hid
    assert out["approved_by"] == "adam"
    assert out["body_sha256"] == gate.body_sha256(body)
    assert transport.calls == [("doc-1", body)]

    rows = await _audit_rows(db, pid)
    assert len(rows) == 1
    detail = json.loads(rows[0]["detail"])
    assert detail["hitl_id"] == hid
    assert detail["body_sha256"] == gate.body_sha256(body)
    assert detail["document_id"] == "doc-1"
    assert detail["write_kind"] == write_kind
    assert detail["approved_by"] == "adam"
    assert detail["reason"]


@pytest.mark.asyncio
async def test_approval_is_single_use_and_second_write_is_refused(db):
    pid = await _project(db, "gdocs-single-use")
    body = _insert_text("hello")
    hid = await _approved(db, pid, body=body)
    transport = _FakeTransport()

    await gate.gated_batch_update(
        db, transport, pid, document_id="doc-1", write_kind="insertText",
        body=body, approval_hitl_id=hid,
    )
    with pytest.raises(gate.GDocsReviewError) as exc:
        await gate.gated_batch_update(
            db, transport, pid, document_id="doc-1", write_kind="insertText",
            body=body, approval_hitl_id=hid,
        )
    assert exc.value.code == gate.HITL_ALREADY_USED
    assert len(transport.calls) == 1, "the replayed approval must not reach the transport"
    assert len(await _audit_rows(db, pid)) == 1


@pytest.mark.asyncio
async def test_refused_writes_never_append_an_audit_row(db):
    pid = await _project(db, "gdocs-audit-quiet")
    body = _insert_text("hello")
    transport = _FakeTransport()
    for approval in (None, "missing-id"):
        with pytest.raises(gate.GDocsReviewError):
            await gate.gated_batch_update(
                db, transport, pid, document_id="doc-1", write_kind="insertText",
                body=body, approval_hitl_id=approval,
            )
    assert transport.calls == []
    assert await _audit_rows(db, pid) == []
