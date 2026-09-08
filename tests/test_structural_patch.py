"""Tests for sprint item 6d109127 -- SCHEMA: structural_patch, a proposed
manuscript structural edit behind a human approval gate.

Covers:
  * meridian.structural_patch -- the pure closed-vocabulary/transition/
    validation layer (no DB import).
  * meridian.db.structural_patch -- the persistence layer (create/get/list/
    transition), on SQLite via the `db` fixture -- proves the migration is
    actually wired into init_db, not just directly callable.
  * meridian.models.StructuralPatch{,Create,Decision} -- the Pydantic wire
    shapes, valid + invalid cases.

Focused, serial-safe: these tests share no external state.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from meridian import db as db_module
from meridian import structural_patch as sp
from meridian.models import StructuralPatch, StructuralPatchCreate, StructuralPatchDecision


# ---------------------------------------------------------------------------
# meridian.structural_patch -- pure vocabulary, transitions, validators.
# ---------------------------------------------------------------------------


def test_patch_operations_and_statuses_are_documented():
    assert sp.PATCH_OPERATIONS == {"insert", "delete", "move", "replace", "reorder"}
    assert sp.PATCH_STATUSES == {
        "proposed", "approved", "rejected", "withdrawn", "superseded", "applied",
    }
    assert sp.PATCH_TERMINAL_STATUSES == {
        "rejected", "withdrawn", "superseded", "applied",
    }
    assert sp.PATCH_TERMINAL_STATUSES <= sp.PATCH_STATUSES
    assert "proposed" not in sp.PATCH_TERMINAL_STATUSES
    assert "approved" not in sp.PATCH_TERMINAL_STATUSES


def test_validate_operation_accepts_all_and_rejects_unknown():
    for op in sp.PATCH_OPERATIONS:
        assert sp.validate_operation(op) == op
        assert sp.validate_operation(op.upper()) == op
    with pytest.raises(ValueError, match="operation must be one of"):
        sp.validate_operation("bogus")


def test_validate_status_accepts_all_and_rejects_unknown():
    for status in sp.PATCH_STATUSES:
        assert sp.validate_status(status) == status
        assert sp.validate_status(status.upper()) == status
    with pytest.raises(ValueError, match="status must be one of"):
        sp.validate_status("bogus")


def test_transition_same_status_is_always_a_noop():
    for status in sp.PATCH_STATUSES:
        assert sp.validate_transition(status, status) == status


def test_legal_transition_proposed_to_approved_to_applied():
    assert sp.validate_transition("proposed", "approved") == "approved"
    assert sp.validate_transition("approved", "applied") == "applied"


def test_legal_transition_proposed_to_rejected_or_withdrawn():
    assert sp.validate_transition("proposed", "rejected") == "rejected"
    assert sp.validate_transition("proposed", "withdrawn") == "withdrawn"


def test_approved_can_still_be_rejected_or_superseded_before_application():
    assert sp.validate_transition("approved", "rejected") == "rejected"
    assert sp.validate_transition("approved", "superseded") == "superseded"


def test_illegal_transition_from_terminal_status_is_rejected():
    with pytest.raises(ValueError, match="illegal structural-patch transition 'applied' -> 'rejected'"):
        sp.validate_transition("applied", "rejected")
    with pytest.raises(ValueError, match="illegal structural-patch transition"):
        sp.validate_transition("rejected", "approved")


def test_illegal_transition_skipping_approval_is_rejected():
    """applied is only reachable from approved -- there is no path that
    skips the human approval gate."""
    with pytest.raises(ValueError, match="illegal structural-patch transition 'proposed' -> 'applied'"):
        sp.validate_transition("proposed", "applied")


def test_withdrawn_only_reachable_from_proposed():
    with pytest.raises(ValueError, match="illegal structural-patch transition"):
        sp.validate_transition("approved", "withdrawn")


def test_is_terminal_status():
    assert sp.is_terminal_status("applied") is True
    assert sp.is_terminal_status("rejected") is True
    assert sp.is_terminal_status("proposed") is False
    assert sp.is_terminal_status("approved") is False


def test_validate_rationale_strips_and_bounds():
    assert sp.validate_rationale("  reword the intro  ") == "reword the intro"
    assert sp.validate_rationale(None) is None
    assert sp.validate_rationale("") is None
    with pytest.raises(ValueError, match="exceeds the"):
        sp.validate_rationale("x" * (sp.MAX_RATIONALE_CHARS + 1))
    with pytest.raises(ValueError, match="must be a string"):
        sp.validate_rationale(123)


def test_validate_rationale_rejects_secret_looking_text():
    with pytest.raises(ValueError, match="Refusing to"):
        sp.validate_rationale(
            "use sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        )


def test_validate_content_hash_accepts_a_sha256_hexdigest():
    digest = "a" * 64
    assert sp.validate_content_hash(digest) == digest
    assert sp.validate_content_hash(None) is None


def test_validate_content_hash_bounds_length():
    with pytest.raises(ValueError, match="exceeds the"):
        sp.validate_content_hash("a" * (sp.MAX_CONTENT_HASH_CHARS + 1))


def test_validate_payload_defaults_and_round_trips():
    assert sp.validate_payload(None) == {}
    payload = {"new_text": "Methods", "level": 2, "kind": "heading"}
    assert sp.validate_payload(payload) == payload


def test_validate_payload_rejects_non_dict():
    with pytest.raises(ValueError, match="must be an object"):
        sp.validate_payload(["not", "a", "dict"])


def test_validate_payload_rejects_non_json_leaf():
    class NotJson:
        pass

    with pytest.raises(ValueError, match="non-JSON value"):
        sp.validate_payload({"weird": NotJson()})


def test_validate_payload_rejects_secret_looking_string():
    with pytest.raises(ValueError, match="Refusing to"):
        sp.validate_payload(
            {"note": "sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"}
        )


def test_validate_payload_rejects_oversized_payload():
    with pytest.raises(ValueError, match="exceeds"):
        sp.validate_payload({"blob": "x" * (sp.MAX_PAYLOAD_BYTES + 10)})


# ---------------------------------------------------------------------------
# meridian.db.structural_patch -- persistence, on the `db` fixture (real
# init_db startup chain).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_structural_patch_wired_into_full_init_db(db):
    project = await db_module.create_project(db, "manuscript-proj")
    session = await db_module.register_session(db, project["id"], "editor-session")
    patch = await db_module.create_structural_patch(
        db, project["id"], session["id"],
        document_id="doc-1", operation="insert",
        payload={"kind": "heading", "text": "Discussion"},
        rationale="Add a missing Discussion section header.",
    )
    assert patch["project_id"] == project["id"]
    assert patch["document_id"] == "doc-1"
    assert patch["operation"] == "insert"
    assert patch["status"] == "proposed"
    assert patch["payload"] == {"kind": "heading", "text": "Discussion"}
    assert patch["proposed_by_session_id"] == session["id"]
    assert patch["decided_by_human_id"] is None
    assert patch["decided_at"] is None
    assert patch["applied_at"] is None


@pytest.mark.asyncio
async def test_create_structural_patch_rejects_unknown_operation(db):
    project = await db_module.create_project(db, "p1")
    session = await db_module.register_session(db, project["id"], "s1")
    with pytest.raises(ValueError, match="operation must be one of"):
        await db_module.create_structural_patch(
            db, project["id"], session["id"], document_id="doc-1", operation="teleport",
        )


@pytest.mark.asyncio
async def test_create_structural_patch_rejects_session_from_another_project(db):
    project_a = await db_module.create_project(db, "p-a")
    project_b = await db_module.create_project(db, "p-b")
    session_b = await db_module.register_session(db, project_b["id"], "s-b")
    with pytest.raises(ValueError, match="does not belong to project"):
        await db_module.create_structural_patch(
            db, project_a["id"], session_b["id"], document_id="doc-1", operation="delete",
        )


@pytest.mark.asyncio
async def test_create_structural_patch_requires_document_id(db):
    project = await db_module.create_project(db, "p2")
    session = await db_module.register_session(db, project["id"], "s2")
    with pytest.raises(ValueError, match="non-empty document_id"):
        await db_module.create_structural_patch(
            db, project["id"], session["id"], document_id="  ", operation="delete",
        )


@pytest.mark.asyncio
async def test_get_structural_patch_scoped_to_project(db):
    project_a = await db_module.create_project(db, "gp-a")
    project_b = await db_module.create_project(db, "gp-b")
    session_a = await db_module.register_session(db, project_a["id"], "s")
    patch = await db_module.create_structural_patch(
        db, project_a["id"], session_a["id"], document_id="doc-1", operation="delete",
        target_element_id="el-1",
    )
    # Right id, wrong project -- must behave exactly like a nonexistent id.
    assert await db_module.get_structural_patch(db, project_b["id"], patch["id"]) is None
    fetched = await db_module.get_structural_patch(db, project_a["id"], patch["id"])
    assert fetched is not None
    assert fetched["target_element_id"] == "el-1"


@pytest.mark.asyncio
async def test_list_structural_patches_filters_by_document_and_status(db):
    project = await db_module.create_project(db, "lp")
    session = await db_module.register_session(db, project["id"], "s")
    p1 = await db_module.create_structural_patch(
        db, project["id"], session["id"], document_id="doc-A", operation="insert",
    )
    await db_module.create_structural_patch(
        db, project["id"], session["id"], document_id="doc-B", operation="delete",
    )
    await db_module.transition_structural_patch(
        db, project["id"], p1["id"], "approved", decided_by_human_id="human-1",
    )

    only_a = await db_module.list_structural_patches(db, project["id"], document_id="doc-A")
    assert [p["id"] for p in only_a] == [p1["id"]]

    only_approved = await db_module.list_structural_patches(db, project["id"], status="approved")
    assert [p["id"] for p in only_approved] == [p1["id"]]

    everything = await db_module.list_structural_patches(db, project["id"])
    assert len(everything) == 2


@pytest.mark.asyncio
async def test_transition_to_approved_requires_decided_by_human_id(db):
    project = await db_module.create_project(db, "tp1")
    session = await db_module.register_session(db, project["id"], "s")
    patch = await db_module.create_structural_patch(
        db, project["id"], session["id"], document_id="doc-1", operation="replace",
    )
    with pytest.raises(ValueError, match="requires decided_by_human_id"):
        await db_module.transition_structural_patch(db, project["id"], patch["id"], "approved")


@pytest.mark.asyncio
async def test_transition_rejects_decided_by_human_id_on_non_decision_status(db):
    project = await db_module.create_project(db, "tp2")
    session = await db_module.register_session(db, project["id"], "s")
    patch = await db_module.create_structural_patch(
        db, project["id"], session["id"], document_id="doc-1", operation="replace",
    )
    with pytest.raises(ValueError, match="only valid when transitioning"):
        await db_module.transition_structural_patch(
            db, project["id"], patch["id"], "withdrawn", decided_by_human_id="human-1",
        )


@pytest.mark.asyncio
async def test_full_approval_gate_lifecycle_to_applied(db):
    project = await db_module.create_project(db, "lifecycle")
    session = await db_module.register_session(db, project["id"], "s")
    patch = await db_module.create_structural_patch(
        db, project["id"], session["id"], document_id="doc-1", operation="move",
        base_content_hash="a" * 64,
    )
    assert patch["status"] == "proposed"

    approved = await db_module.transition_structural_patch(
        db, project["id"], patch["id"], "approved",
        decided_by_human_id="reviewer-1", decision_note="Looks correct.",
    )
    assert approved["status"] == "approved"
    assert approved["decided_by_human_id"] == "reviewer-1"
    assert approved["decision_note"] == "Looks correct."
    assert approved["decided_at"] is not None
    assert approved["applied_at"] is None

    applied = await db_module.transition_structural_patch(
        db, project["id"], patch["id"], "applied",
    )
    assert applied["status"] == "applied"
    assert applied["applied_at"] is not None

    # Terminal: cannot un-apply.
    with pytest.raises(ValueError, match="illegal structural-patch transition"):
        await db_module.transition_structural_patch(db, project["id"], patch["id"], "rejected")


@pytest.mark.asyncio
async def test_applied_is_unreachable_without_prior_approval(db):
    project = await db_module.create_project(db, "gate")
    session = await db_module.register_session(db, project["id"], "s")
    patch = await db_module.create_structural_patch(
        db, project["id"], session["id"], document_id="doc-1", operation="delete",
    )
    with pytest.raises(ValueError, match="illegal structural-patch transition 'proposed' -> 'applied'"):
        await db_module.transition_structural_patch(db, project["id"], patch["id"], "applied")


@pytest.mark.asyncio
async def test_transition_not_found_raises(db):
    project = await db_module.create_project(db, "nf")
    with pytest.raises(ValueError, match="not found in project"):
        await db_module.transition_structural_patch(
            db, project["id"], "nonexistent-id", "approved", decided_by_human_id="h",
        )


@pytest.mark.asyncio
async def test_create_structural_patch_with_supersedes_link(db):
    project = await db_module.create_project(db, "supersede")
    session = await db_module.register_session(db, project["id"], "s")
    original = await db_module.create_structural_patch(
        db, project["id"], session["id"], document_id="doc-1", operation="insert",
    )
    revised = await db_module.create_structural_patch(
        db, project["id"], session["id"], document_id="doc-1", operation="insert",
        supersedes_patch_id=original["id"],
    )
    assert revised["supersedes_patch_id"] == original["id"]

    # Superseding the original is a separate, explicit call -- not automatic.
    original_reloaded = await db_module.get_structural_patch(db, project["id"], original["id"])
    assert original_reloaded["status"] == "proposed"
    superseded = await db_module.transition_structural_patch(
        db, project["id"], original["id"], "superseded",
    )
    assert superseded["status"] == "superseded"


@pytest.mark.asyncio
async def test_create_structural_patch_rejects_unknown_supersedes_id(db):
    project = await db_module.create_project(db, "bad-supersede")
    session = await db_module.register_session(db, project["id"], "s")
    with pytest.raises(ValueError, match="supersedes_patch_id"):
        await db_module.create_structural_patch(
            db, project["id"], session["id"], document_id="doc-1", operation="insert",
            supersedes_patch_id="does-not-exist",
        )


@pytest.mark.asyncio
async def test_structural_patches_table_has_no_check_constraint_on_status(db):
    """Structural guarantee behind this module's documented "no CHECK
    constraint" design choice (see db.structural_patch's module docstring):
    the vocabulary can grow later without a disruptive SQLite table rebuild."""
    async with db.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'structural_patches'"
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    assert "CHECK" not in row["sql"].upper()


# ---------------------------------------------------------------------------
# meridian.models.StructuralPatch{,Create,Decision} -- Pydantic wire shapes.
# ---------------------------------------------------------------------------


def test_structural_patch_create_valid():
    model = StructuralPatchCreate(
        project_id="p1", session_id="s1", document_id="doc-1", operation="insert",
        payload={"text": "New section"}, rationale="clarity",
    )
    assert model.operation == "insert"
    assert model.payload == {"text": "New section"}
    assert model.target_element_id is None
    assert model.supersedes_patch_id is None


def test_structural_patch_create_rejects_unknown_operation():
    with pytest.raises(ValidationError):
        StructuralPatchCreate(
            project_id="p1", session_id="s1", document_id="doc-1", operation="teleport",
        )


def test_structural_patch_create_requires_document_id():
    with pytest.raises(ValidationError):
        StructuralPatchCreate(project_id="p1", session_id="s1", document_id="", operation="insert")


def test_structural_patch_response_model_round_trip():
    model = StructuralPatch(
        id="sp-1", project_id="p1", document_id="doc-1", target_element_id="el-1",
        operation="replace", payload={"text": "x"}, rationale=None,
        base_content_hash="a" * 64, status="proposed", proposed_by_session_id="s1",
        decided_by_human_id=None, decision_note=None, decided_at=None, applied_at=None,
        supersedes_patch_id=None, created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
    )
    assert model.status == "proposed"
    dumped = model.model_dump()
    assert dumped["operation"] == "replace"


def test_structural_patch_response_model_rejects_unknown_status():
    with pytest.raises(ValidationError):
        StructuralPatch(
            id="sp-1", project_id="p1", document_id="doc-1", operation="insert",
            status="unknown-status", proposed_by_session_id="s1",
            created_at="2026-01-01T00:00:00Z", updated_at="2026-01-01T00:00:00Z",
        )


def test_structural_patch_decision_requires_decided_by_human_id():
    with pytest.raises(ValidationError):
        StructuralPatchDecision(project_id="p1", decision="approved", decided_by_human_id="")


def test_structural_patch_decision_valid():
    model = StructuralPatchDecision(
        project_id="p1", decision="rejected", decided_by_human_id="reviewer-1",
        decision_note="Not aligned with the outline.",
    )
    assert model.decision == "rejected"
