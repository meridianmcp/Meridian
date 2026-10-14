"""Image-storage rights policy: normalization, tiers, derivatives and overrides.

The pure tests cover ``meridian.rights_policy``. The async tests run the
human-approval override end to end through ``meridian.gate_override`` on the
real ``db`` fixture: request, answer, authorize, audit, replay and refusals.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from meridian import db as db_module
from meridian import gate_override
from meridian import rights_policy as rp
from meridian.rights_override import authorize_image_storage, request_rights_override
from meridian.rights_policy import (
    LINK_ONLY,
    RIGHTS_OVERRIDE_EVENT_TYPE,
    RIGHTS_OVERRIDE_GATE,
    STORE_IMAGE,
    STORE_UNMODIFIED,
    RightsOverride,
    RightsPolicyError,
    decide_image_storage,
    normalize_license,
    tier_for,
)

_YES = "Yes — approve this override"
_NO = "No — do not approve"
_REASON = "permission letter on file"


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("https://creativecommons.org/licenses/by-nc-nd/4.0/", "cc-by-nc-nd"),
    ("http://creativecommons.org/licenses/by/4.0/", "cc-by"),
    ("https://creativecommons.org/licenses/by-sa/3.0/deed.en", "cc-by-sa"),
    ("https://creativecommons.org/licenses/by-nd/4.0/legalcode", "cc-by-nd"),
    ("https://creativecommons.org/licenses/by-nc-sa/2.0/uk/", "cc-by-nc-sa"),
    ("https://creativecommons.org/licenses/by-nc/4.0", "cc-by-nc"),
    ("https://creativecommons.org/publicdomain/zero/1.0/", "cc0"),
    ("https://creativecommons.org/publicdomain/mark/1.0/", "public-domain"),
    ("https://creativecommons.org/share-your-work/public-domain/cc0/", "cc0"),
    ("https://creativecommons.org/about/cclicenses/", "unknown"),
    ("https://creativecommons.org/licenses/xyz/4.0/", "unknown"),
    ("https://example.com/licenses/by/4.0/", "unknown"),
])
def test_creativecommons_urls_normalize(raw, expected):
    assert normalize_license(raw) == expected


@pytest.mark.parametrize("raw, expected", [
    ("CC BY-NC-ND 4.0", "cc-by-nc-nd"),
    ("CC-BY-NC-4.0", "cc-by-nc"),
    ("cc-by", "cc-by"),
    ("CC BY 4.0 International", "cc-by"),
    ("Attribution 4.0 International", "cc-by"),
    ("Attribution-NonCommercial-NoDerivatives 4.0 International", "cc-by-nc-nd"),
    ("Attribution-ShareAlike 4.0", "cc-by-sa"),
    ("CC BY-NC-SA 4.0", "cc-by-nc-sa"),
    ("CC BY-ND 4.0", "cc-by-nd"),
    ("CC BY-SA 4.0", "cc-by-sa"),
    ("CC0", "cc0"),
    ("cc0 1.0 universal", "cc0"),
    ("public domain", "public-domain"),
    ("Public Domain Mark 1.0", "public-domain"),
    ("arXiv non-exclusive", "arxiv-nonexclusive"),
    ("arXiv perpetual, non-exclusive license", "arxiv-nonexclusive"),
    ("All Rights Reserved", "all-rights-reserved"),
    ("  cC   bY-sA   4.0  ", "cc-by-sa"),
    ("CC BY-NC-ND 4.0 ", "cc-by-nc-nd"),
])
def test_license_strings_normalize_in_any_case_and_spacing(raw, expected):
    assert normalize_license(raw) == expected


@pytest.mark.parametrize("raw", [
    None,
    "",
    "   ",
    42,
    "unknown",
    "Unknown",
    "something proprietary",
    "by-nc-nd 4.0",
    "CC BY-ND-SA 4.0",
    "CC BY-NC-ND-SA 4.0",
    "CC BY 4.0 (author-only)",
    "CC NC 4.0",
    "CC BY 3.0 IGO",
    "arXiv",
    "copyright 2024 Acme",
])
def test_unrecognised_input_is_unknown(raw):
    # "CC BY 3.0 IGO" is a deliberate fail-closed case: the IGO clause is not
    # in the known set, so it is unknown (link-only) rather than guessed.
    assert normalize_license(raw) == "unknown"


@pytest.mark.parametrize("canonical", [
    "cc0", "public-domain", "cc-by", "cc-by-sa", "cc-by-nd", "cc-by-nc",
    "cc-by-nc-sa", "cc-by-nc-nd", "arxiv-nonexclusive", "all-rights-reserved", "unknown",
])
def test_canonical_ids_are_idempotent(canonical):
    assert normalize_license(canonical) == canonical


# ---------------------------------------------------------------------------
# Tier table
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("canonical, tier", [
    ("cc0", STORE_IMAGE),
    ("public-domain", STORE_IMAGE),
    ("cc-by", STORE_IMAGE),
    ("cc-by-sa", STORE_IMAGE),
    ("cc-by-nd", STORE_UNMODIFIED),
    ("cc-by-nc", LINK_ONLY),
    ("cc-by-nc-sa", LINK_ONLY),
    ("cc-by-nc-nd", LINK_ONLY),
    ("arxiv-nonexclusive", LINK_ONLY),
    ("all-rights-reserved", LINK_ONLY),
    ("unknown", LINK_ONLY),
])
def test_tier_table(canonical, tier):
    assert tier_for(canonical) == tier


def test_tier_for_fails_closed_and_accepts_raw_strings():
    assert tier_for("made-up-license") == LINK_ONLY
    assert tier_for(None) == LINK_ONLY
    assert tier_for("CC BY 4.0") == STORE_IMAGE


# ---------------------------------------------------------------------------
# Decisions: storable, derivatives, link-only
# ---------------------------------------------------------------------------

def test_original_public_domain_image_is_stored_with_no_override():
    d = decide_image_storage("CC0")
    assert d.allowed is True
    assert d.tier == STORE_IMAGE
    assert d.license == "cc0"
    assert d.override_used is False
    assert d.audit is None
    assert d.share_alike_required is False


def test_derived_figure_keeps_its_license():
    d = decide_image_storage("CC BY 4.0", derived=True)
    assert d.allowed is True
    assert d.license == "cc-by"
    assert d.derived is True
    assert d.share_alike_required is False


def test_cc_by_sa_derivative_must_carry_the_share_alike_flag():
    assert decide_image_storage("cc-by-sa", derived=True).share_alike_required is True
    assert decide_image_storage("cc-by-sa", redrawn=True).share_alike_required is True
    original = decide_image_storage("cc-by-sa")
    assert original.allowed is True
    assert original.share_alike_required is False


def test_cc_by_nd_original_is_stored_as_an_unmodified_copy():
    d = decide_image_storage("CC BY-ND 4.0")
    assert d.allowed is True
    assert d.tier == STORE_UNMODIFIED
    assert d.override_used is False


@pytest.mark.parametrize("kwargs", [
    {"derived": True},
    {"redrawn": True},
    {"derived": True, "redrawn": True},
])
def test_cc_by_nd_refuses_redrawn_or_cropped_derivatives(kwargs):
    d = decide_image_storage("cc-by-nd", **kwargs)
    assert d.allowed is False
    assert d.tier == STORE_UNMODIFIED
    assert "unmodified" in d.reason
    assert "human-approved override is required" in d.reason
    assert d.override_used is False


@pytest.mark.parametrize("raw", [
    "CC BY-NC 4.0",
    "CC BY-NC-SA 4.0",
    "CC BY-NC-ND 4.0",
    "arXiv non-exclusive",
    "All Rights Reserved",
    "unknown",
    "",
    "mystery license",
])
def test_link_only_refuses_image_bytes(raw):
    d = decide_image_storage(raw)
    assert d.allowed is False
    assert d.tier == LINK_ONLY
    assert "link-only" in d.reason
    assert d.override_used is False
    assert d.audit is None


def test_link_only_refuses_derivatives_too():
    assert decide_image_storage("CC BY-NC 4.0", derived=True).allowed is False


def test_unknown_license_is_never_storable():
    d = decide_image_storage(None)
    assert d.allowed is False
    assert d.license == "unknown"
    assert "unknown license" in d.reason


def test_rights_policy_is_a_pure_leaf_with_no_database_imports():
    tree = ast.parse(Path(rp.__file__).read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
    assert names <= {"__future__", "re", "dataclasses", "typing"}


# ---------------------------------------------------------------------------
# Override: shape, approver identity, audit in the decision
# ---------------------------------------------------------------------------

def _override(**overrides):
    fields = {
        "hitl_id": "hitl-1",
        "subject_id": "fig-3",
        "approved_by": "adam",
        "reason": _REASON,
    }
    fields.update(overrides)
    return RightsOverride(**fields)


def test_a_human_override_lets_a_refused_tier_through_and_builds_the_audit():
    d = decide_image_storage("CC BY-NC 4.0", override=_override())
    assert d.allowed is True
    assert d.override_used is True
    assert d.tier == LINK_ONLY
    audit = d.audit
    assert audit["event_type"] == RIGHTS_OVERRIDE_EVENT_TYPE == "rights_storage_override"
    assert audit["gate"] == RIGHTS_OVERRIDE_GATE
    assert audit["subject_id"] == "fig-3"
    assert audit["hitl_id"] == "hitl-1"
    assert audit["approved_by"] == "adam"
    assert audit["reason"] == _REASON
    assert audit["tier"] == LINK_ONLY
    assert audit["license"] == "cc-by-nc"


def test_override_for_a_redrawn_nd_image_is_audited_with_its_tier():
    d = decide_image_storage("cc-by-nd", redrawn=True, override=_override())
    assert d.allowed is True
    assert d.override_used is True
    assert d.audit["tier"] == STORE_UNMODIFIED
    assert d.audit["redrawn"] is True


def test_override_is_not_used_when_the_tier_already_allows_the_bytes():
    d = decide_image_storage("cc-by", override=_override())
    assert d.allowed is True
    assert d.override_used is False
    assert d.audit is None


@pytest.mark.parametrize("approver", [
    "agent-7", "Agent", "session-42", "Session 3", "claude-opus", "CLAUDE", "auto", " AUTO ",
])
def test_an_agent_or_session_cannot_approve_a_rights_override(approver):
    with pytest.raises(RightsPolicyError) as exc:
        decide_image_storage("cc-by-nc", override=_override(approved_by=approver))
    assert exc.value.code == "AGENT_APPROVAL_REFUSED"


@pytest.mark.parametrize("fields, code", [
    ({"approved_by": ""}, "APPROVER_REQUIRED"),
    ({"approved_by": "   "}, "APPROVER_REQUIRED"),
    ({"reason": ""}, "OVERRIDE_REASON_REQUIRED"),
    ({"reason": "   "}, "OVERRIDE_REASON_REQUIRED"),
    ({"hitl_id": ""}, "HUMAN_APPROVAL_REQUIRED"),
    ({"subject_id": ""}, "OVERRIDE_INVALID"),
    ({"gate": "some_other_gate"}, "OVERRIDE_INVALID"),
])
def test_a_malformed_override_is_refused_even_when_it_is_not_needed(fields, code):
    with pytest.raises(RightsPolicyError) as exc:
        decide_image_storage("cc-by", override=_override(**fields))
    assert exc.value.code == code


@pytest.mark.parametrize("plain", [
    {"approved_by": "adam", "reason": "ok", "hitl_id": "h"},
    "adam",
    True,
])
def test_a_plain_value_is_never_an_override(plain):
    with pytest.raises(RightsPolicyError) as exc:
        decide_image_storage("cc-by-nc", override=plain)
    assert exc.value.code == "OVERRIDE_INVALID"


def test_rights_policy_error_is_a_value_error_with_a_code():
    err = RightsPolicyError("SOME_CODE", "message")
    assert isinstance(err, ValueError)
    assert err.code == "SOME_CODE"


# ---------------------------------------------------------------------------
# Override end to end through the gate-override mechanism (db fixture)
# ---------------------------------------------------------------------------

async def _project(db) -> str:
    project = await db_module.create_project(db, "rights-policy")
    return project["id"]


async def _hitl_count(db, project_id: str) -> int:
    async with db.execute(
        "SELECT COUNT(*) AS c FROM hitl_requests WHERE project_id = ?", (project_id,),
    ) as cur:
        row = await cur.fetchone()
    return row["c"] if isinstance(row, dict) else row[0]


async def _audit(db, project_id: str):
    return await db_module.get_action_audit_log(
        db, project_id=project_id, event_type=RIGHTS_OVERRIDE_EVENT_TYPE,
    )


async def _file_request(db, project_id: str, *, subject="fig-3", reason=_REASON):
    return await request_rights_override(
        db,
        project_id,
        subject_id=subject,
        reason=reason,
        description="CC BY-NC figure from a preprint",
    )


async def _approve(db, hitl_id: str, *, answer=_YES, answered_by="adam"):
    await db_module.answer_hitl_request(db, hitl_id, answer, answered_by=answered_by)


@pytest.mark.asyncio
async def test_request_files_a_require_human_gate_override_and_reuses_it(db):
    pid = await _project(db)
    hitl = await _file_request(db, pid)
    row = await db_module.get_hitl_request(db, hitl["id"])
    payload = json.loads(row["payload"])
    assert row["kind"] == gate_override.GATE_OVERRIDE_HITL_KIND
    assert row["status"] == "pending"
    assert row["answered_by"] is None
    assert payload["require_human"] is True
    assert payload["gate"] == RIGHTS_OVERRIDE_GATE
    assert payload["subject_id"] == "fig-3"
    again = await _file_request(db, pid)
    assert again["id"] == hitl["id"]


@pytest.mark.asyncio
async def test_request_without_a_reason_is_refused(db):
    pid = await _project(db)
    with pytest.raises(RightsPolicyError) as exc:
        await _file_request(db, pid, reason="   ")
    assert exc.value.code == "OVERRIDE_REASON_REQUIRED"


@pytest.mark.asyncio
async def test_request_without_a_subject_is_refused(db):
    pid = await _project(db)
    with pytest.raises(RightsPolicyError) as exc:
        await _file_request(db, pid, subject="  ")
    assert exc.value.code == "OVERRIDE_INVALID"


@pytest.mark.asyncio
async def test_a_public_license_stores_bytes_without_any_hitl_or_audit(db):
    pid = await _project(db)
    d = await authorize_image_storage(db, pid, "CC BY 4.0", subject_id="fig-1")
    assert d.allowed is True
    assert d.override_used is False
    assert await _hitl_count(db, pid) == 0
    assert await _audit(db, pid) == []


@pytest.mark.asyncio
async def test_link_only_refuses_bytes_and_files_nothing_without_a_hitl(db):
    pid = await _project(db)
    d = await authorize_image_storage(db, pid, "CC BY-NC 4.0", subject_id="fig-1")
    assert d.allowed is False
    assert d.tier == LINK_ONLY
    assert await _hitl_count(db, pid) == 0
    assert await _audit(db, pid) == []


@pytest.mark.asyncio
async def test_a_pending_approval_does_not_unlock_bytes(db):
    pid = await _project(db)
    hitl = await _file_request(db, pid)
    with pytest.raises(RightsPolicyError) as exc:
        await authorize_image_storage(
            db, pid, "CC BY-NC 4.0", subject_id="fig-3", hitl_id=hitl["id"], reason=_REASON,
        )
    assert exc.value.code == "HITL_NOT_ANSWERED"
    assert await _audit(db, pid) == []


@pytest.mark.asyncio
async def test_a_no_answer_does_not_unlock_bytes(db):
    pid = await _project(db)
    hitl = await _file_request(db, pid)
    await _approve(db, hitl["id"], answer=_NO)
    with pytest.raises(RightsPolicyError) as exc:
        await authorize_image_storage(
            db, pid, "CC BY-NC 4.0", subject_id="fig-3", hitl_id=hitl["id"], reason=_REASON,
        )
    assert exc.value.code == "HITL_NOT_APPROVED"
    assert await _audit(db, pid) == []


@pytest.mark.asyncio
async def test_an_auto_answered_approval_is_refused(db):
    pid = await _project(db)
    hitl = await _file_request(db, pid)
    await _approve(db, hitl["id"], answered_by="auto")
    with pytest.raises(RightsPolicyError) as exc:
        await authorize_image_storage(
            db, pid, "CC BY-NC 4.0", subject_id="fig-3", hitl_id=hitl["id"], reason=_REASON,
        )
    assert exc.value.code == "HITL_NOT_APPROVED"


@pytest.mark.asyncio
async def test_an_agent_answering_the_approval_is_refused_and_not_audited(db):
    pid = await _project(db)
    hitl = await _file_request(db, pid)
    await _approve(db, hitl["id"], answered_by="claude-executor")
    with pytest.raises(RightsPolicyError) as exc:
        await authorize_image_storage(
            db, pid, "CC BY-NC 4.0", subject_id="fig-3", hitl_id=hitl["id"], reason=_REASON,
        )
    assert exc.value.code == "AGENT_APPROVAL_REFUSED"
    assert await _audit(db, pid) == []


@pytest.mark.asyncio
async def test_a_human_approval_unlocks_bytes_once_and_is_audited(db):
    pid = await _project(db)
    hitl = await _file_request(db, pid)
    await _approve(db, hitl["id"])
    d = await authorize_image_storage(
        db,
        pid,
        "CC BY-NC 4.0",
        subject_id="fig-3",
        hitl_id=hitl["id"],
        reason=_REASON,
        consumed_by="executor-1",
    )
    assert d.allowed is True
    assert d.override_used is True
    assert d.tier == LINK_ONLY
    assert d.license == "cc-by-nc"

    rows = await _audit(db, pid)
    assert len(rows) == 1
    row = rows[0]
    detail = json.loads(row["detail"])
    assert row["event_type"] == RIGHTS_OVERRIDE_EVENT_TYPE
    assert row["actor"] == "adam"
    assert detail["subject_id"] == "fig-3"
    assert detail["reason"] == _REASON
    assert detail["hitl_id"] == hitl["id"]
    assert detail["tier"] == LINK_ONLY
    assert detail["license"] == "cc-by-nc"

    with pytest.raises(RightsPolicyError) as exc:
        await authorize_image_storage(
            db, pid, "CC BY-NC 4.0", subject_id="fig-3", hitl_id=hitl["id"], reason=_REASON,
        )
    assert exc.value.code == "HITL_ALREADY_USED"
    assert len(await _audit(db, pid)) == 1


@pytest.mark.asyncio
async def test_an_approval_cannot_be_spent_on_a_different_subject(db):
    pid = await _project(db)
    hitl = await _file_request(db, pid, subject="fig-3")
    await _approve(db, hitl["id"])
    with pytest.raises(RightsPolicyError) as exc:
        await authorize_image_storage(
            db, pid, "CC BY-NC 4.0", subject_id="fig-9", hitl_id=hitl["id"], reason=_REASON,
        )
    assert exc.value.code == "HITL_INVALID"
    # The refused attempt did not spend the approval, so the right subject still works.
    d = await authorize_image_storage(
        db, pid, "CC BY-NC 4.0", subject_id="fig-3", hitl_id=hitl["id"], reason=_REASON,
    )
    assert d.allowed is True


@pytest.mark.asyncio
async def test_a_missing_reason_is_refused_before_the_approval_is_spent(db):
    pid = await _project(db)
    hitl = await _file_request(db, pid)
    await _approve(db, hitl["id"])
    with pytest.raises(RightsPolicyError) as exc:
        await authorize_image_storage(
            db, pid, "CC BY-NC 4.0", subject_id="fig-3", hitl_id=hitl["id"], reason=None,
        )
    assert exc.value.code == "OVERRIDE_REASON_REQUIRED"
    d = await authorize_image_storage(
        db, pid, "CC BY-NC 4.0", subject_id="fig-3", hitl_id=hitl["id"], reason=_REASON,
    )
    assert d.allowed is True


@pytest.mark.asyncio
async def test_a_blank_subject_is_refused_when_an_approval_is_presented(db):
    pid = await _project(db)
    hitl = await _file_request(db, pid)
    await _approve(db, hitl["id"])
    with pytest.raises(RightsPolicyError) as exc:
        await authorize_image_storage(
            db, pid, "CC BY-NC 4.0", subject_id="", hitl_id=hitl["id"], reason=_REASON,
        )
    assert exc.value.code == "OVERRIDE_INVALID"


@pytest.mark.asyncio
async def test_a_redrawn_nd_image_needs_its_own_human_approval(db):
    pid = await _project(db)
    refused = await authorize_image_storage(
        db, pid, "cc-by-nd", subject_id="fig-7", redrawn=True,
    )
    assert refused.allowed is False
    assert refused.tier == STORE_UNMODIFIED
    hitl = await request_rights_override(
        db, pid, subject_id="fig-7", reason="publisher agreed to a redraw",
        description="redrawn figure from an ND source",
    )
    await _approve(db, hitl["id"])
    d = await authorize_image_storage(
        db,
        pid,
        "cc-by-nd",
        subject_id="fig-7",
        redrawn=True,
        hitl_id=hitl["id"],
        reason="publisher agreed to a redraw",
    )
    assert d.allowed is True
    assert d.override_used is True
    assert d.tier == STORE_UNMODIFIED
    rows = await _audit(db, pid)
    assert len(rows) == 1
    assert json.loads(rows[0]["detail"])["tier"] == STORE_UNMODIFIED


@pytest.mark.asyncio
async def test_a_failed_audit_write_blocks_the_override(db, monkeypatch):
    pid = await _project(db)
    hitl = await _file_request(db, pid)
    await _approve(db, hitl["id"])

    async def audit_store_down(*_args, **_kwargs):
        raise RuntimeError("audit store unavailable")

    monkeypatch.setattr(gate_override, "record_override_audit", audit_store_down)
    with pytest.raises(RuntimeError, match="audit store unavailable"):
        await authorize_image_storage(
            db, pid, "CC BY-NC 4.0", subject_id="fig-3", hitl_id=hitl["id"], reason=_REASON,
        )
