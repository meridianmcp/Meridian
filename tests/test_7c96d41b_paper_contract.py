"""Tests for sprint item 7c96d41b — SCHEMA: paper_contract, the first-class
versioned editorial intent document for Meridian's manuscript/paper
editorial tooling line.

Covers:
  * meridian.paper_contract — the pure closed-vocabulary/fingerprint layer.
  * meridian.models — PaperContractContent/PaperContract/
    PaperContractRevision Pydantic validation (valid + invalid cases).
  * meridian.db.paper_contract — the persistence layer (create_paper_contract/
    create_paper_contract_revision/approve_paper_contract_revision/
    get_paper_contract/get_paper_contract_by_key/
    list_paper_contract_revisions), on SQLite via the `db` fixture (the real
    init_db startup chain — proves the migration is actually wired in, not
    just directly callable).
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from meridian import db as db_module
from meridian import paper_contract as pc
from meridian import models


# ---------------------------------------------------------------------------
# meridian.paper_contract — pure vocabulary, fingerprint.
# ---------------------------------------------------------------------------


def test_contract_and_revision_statuses_are_documented():
    assert pc.CONTRACT_STATUSES == {"draft", "active", "archived"}
    assert pc.REVISION_APPROVAL_STATUSES == {"pending", "approved", "rejected"}


def test_validate_contract_status_accepts_all_and_rejects_unknown():
    for status in pc.CONTRACT_STATUSES:
        assert pc.validate_contract_status(status) == status
        assert pc.validate_contract_status(status.upper()) == status
    with pytest.raises(ValueError, match="status must be one of"):
        pc.validate_contract_status("bogus")
    with pytest.raises(ValueError, match="status must be one of"):
        pc.validate_contract_status(None)


def test_validate_revision_approval_status_accepts_all_and_rejects_unknown():
    for status in pc.REVISION_APPROVAL_STATUSES:
        assert pc.validate_revision_approval_status(status) == status
    with pytest.raises(ValueError, match="approval_status must be one of"):
        pc.validate_revision_approval_status("accepted")


def test_content_fingerprint_deterministic_regardless_of_key_order():
    a = pc.content_fingerprint({"working_title": "Draft", "word_limit": 4000})
    b = pc.content_fingerprint({"word_limit": 4000, "working_title": "Draft"})
    assert a == b
    assert a.startswith("sha256:")


def test_content_fingerprint_differs_for_different_content():
    a = pc.content_fingerprint({"working_title": "Draft A"})
    b = pc.content_fingerprint({"working_title": "Draft B"})
    assert a != b


# ---------------------------------------------------------------------------
# meridian.models — Pydantic validation, valid + invalid.
# ---------------------------------------------------------------------------


def test_paper_contract_content_valid_minimal():
    content = models.PaperContractContent(working_title="A study of things")
    assert content.working_title == "A study of things"
    assert content.scope_in == []
    assert content.scope_out == []
    assert content.word_limit is None


def test_paper_contract_content_valid_full():
    content = models.PaperContractContent(
        working_title="A study of things",
        target_venue="NeurIPS",
        audience="ML researchers",
        thesis="Thing X causes thing Y under condition Z.",
        scope_in=["thing X", "thing Y"],
        scope_out=["unrelated thing W"],
        required_sections=["Abstract", "Method", "Results"],
        style_guide="APA",
        citation_style="apa",
        word_limit=8000,
        constraints=["no overclaiming causality without the RCT"],
    )
    assert content.word_limit == 8000
    assert content.required_sections == ["Abstract", "Method", "Results"]


def test_paper_contract_content_rejects_missing_working_title():
    with pytest.raises(ValidationError):
        models.PaperContractContent()


def test_paper_contract_content_rejects_empty_working_title():
    with pytest.raises(ValidationError):
        models.PaperContractContent(working_title="")


def test_paper_contract_content_rejects_non_positive_word_limit():
    with pytest.raises(ValidationError):
        models.PaperContractContent(working_title="Draft", word_limit=0)
    with pytest.raises(ValidationError):
        models.PaperContractContent(working_title="Draft", word_limit=-100)


def test_paper_contract_rejects_unknown_status():
    with pytest.raises(ValidationError):
        models.PaperContract(
            id="c1",
            project_id="p1",
            paper_key="main",
            title="A paper",
            status="in_review",  # not one of draft|active|archived
            created_at="2026-01-01 00:00:00",
            updated_at="2026-01-01 00:00:00",
        )


def test_paper_contract_valid_defaults():
    contract = models.PaperContract(
        id="c1",
        project_id="p1",
        paper_key="main",
        title="A paper",
        created_at="2026-01-01 00:00:00",
        updated_at="2026-01-01 00:00:00",
    )
    assert contract.status == "draft"
    assert contract.current_revision_id is None
    assert contract.latest_revision_number == 0


def test_paper_contract_revision_rejects_unknown_approval_status():
    with pytest.raises(ValidationError):
        models.PaperContractRevision(
            id="r1",
            contract_id="c1",
            revision_number=1,
            content=models.PaperContractContent(working_title="Draft"),
            content_hash="sha256:abc",
            approval_status="under_review",
            created_at="2026-01-01 00:00:00",
        )


def test_paper_contract_revision_rejects_non_positive_revision_number():
    with pytest.raises(ValidationError):
        models.PaperContractRevision(
            id="r1",
            contract_id="c1",
            revision_number=0,
            content=models.PaperContractContent(working_title="Draft"),
            content_hash="sha256:abc",
            created_at="2026-01-01 00:00:00",
        )


def test_paper_contract_revision_approval_requires_non_empty_human_id():
    with pytest.raises(ValidationError):
        models.PaperContractRevisionApproval(revision_id="r1", approved_by_human_id="")


# ---------------------------------------------------------------------------
# meridian.db.paper_contract — persistence, on the `db` fixture (real
# init_db startup chain — proves the migration is actually wired in).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_paper_contract_wired_into_full_init_db(db):
    project = await db_module.create_project(db, "paper-contract-wiring")
    contract = await db_module.create_paper_contract(
        db, project["id"], "main-paper", "Editorial contract for the main paper",
        created_by_human_id="adam",
    )
    assert contract["project_id"] == project["id"]
    assert contract["paper_key"] == "main-paper"
    assert contract["status"] == "draft"
    assert contract["current_revision_id"] is None
    assert contract["latest_revision_number"] == 0


@pytest.mark.asyncio
async def test_create_paper_contract_requires_non_empty_fields(db):
    project = await db_module.create_project(db, "paper-contract-validation")
    with pytest.raises(ValueError, match="non-empty paper_key"):
        await db_module.create_paper_contract(db, project["id"], "  ", "Title")
    with pytest.raises(ValueError, match="non-empty title"):
        await db_module.create_paper_contract(db, project["id"], "main", "  ")
    with pytest.raises(ValueError, match="non-empty project_id"):
        await db_module.create_paper_contract(db, "  ", "main", "Title")


@pytest.mark.asyncio
async def test_create_paper_contract_duplicate_paper_key_raises(db):
    project = await db_module.create_project(db, "paper-contract-dup")
    await db_module.create_paper_contract(db, project["id"], "main-paper", "Main paper")
    with pytest.raises(ValueError, match="already exists"):
        await db_module.create_paper_contract(db, project["id"], "main-paper", "Different title")


@pytest.mark.asyncio
async def test_same_paper_key_allowed_across_different_projects(db):
    project_a = await db_module.create_project(db, "paper-contract-proj-a")
    project_b = await db_module.create_project(db, "paper-contract-proj-b")
    a = await db_module.create_paper_contract(db, project_a["id"], "main-paper", "Paper A")
    b = await db_module.create_paper_contract(db, project_b["id"], "main-paper", "Paper B")
    assert a["id"] != b["id"]


@pytest.mark.asyncio
async def test_get_paper_contract_scoped_to_project(db):
    project_a = await db_module.create_project(db, "paper-contract-scope-a")
    project_b = await db_module.create_project(db, "paper-contract-scope-b")
    contract = await db_module.create_paper_contract(db, project_a["id"], "main-paper", "Paper A")

    # Right id, wrong project -> None, never leaks existence cross-project.
    assert await db_module.get_paper_contract(db, project_b["id"], contract["id"]) is None
    assert await db_module.get_paper_contract(db, project_a["id"], contract["id"]) is not None
    assert await db_module.get_paper_contract_by_key(db, project_b["id"], "main-paper") is None
    fetched = await db_module.get_paper_contract_by_key(db, project_a["id"], "main-paper")
    assert fetched is not None and fetched["id"] == contract["id"]


@pytest.mark.asyncio
async def test_create_revision_requires_existing_contract_and_nonempty_content(db):
    project = await db_module.create_project(db, "paper-contract-revision-guards")
    with pytest.raises(ValueError, match="not found"):
        await db_module.create_paper_contract_revision(db, project["id"], "nonexistent-id", {"working_title": "x"})

    contract = await db_module.create_paper_contract(db, project["id"], "main-paper", "Main paper")
    with pytest.raises(ValueError, match="non-empty content"):
        await db_module.create_paper_contract_revision(db, project["id"], contract["id"], {})


@pytest.mark.asyncio
async def test_revision_round_trip_numbering_and_content(db):
    project = await db_module.create_project(db, "paper-contract-revision-roundtrip")
    contract = await db_module.create_paper_contract(db, project["id"], "main-paper", "Main paper")

    content_1 = models.PaperContractContent(working_title="Draft v1", word_limit=6000).model_dump()
    rev1 = await db_module.create_paper_contract_revision(
        db, project["id"], contract["id"], content_1, change_summary="initial draft", created_by="exec-session-1",
    )
    assert rev1["revision_number"] == 1
    assert rev1["approval_status"] == "pending"
    assert rev1["content"]["working_title"] == "Draft v1"
    assert rev1["content_hash"] == pc.content_fingerprint(content_1)
    assert rev1["superseded_by_revision_id"] is None

    content_2 = models.PaperContractContent(working_title="Draft v2", word_limit=7000).model_dump()
    rev2 = await db_module.create_paper_contract_revision(
        db, project["id"], contract["id"], content_2, change_summary="tightened scope",
    )
    assert rev2["revision_number"] == 2

    # Creating revisions never pins current_revision_id -- only approval does.
    contract_after = await db_module.get_paper_contract(db, project["id"], contract["id"])
    assert contract_after["current_revision_id"] is None
    assert contract_after["latest_revision_number"] == 2
    assert contract_after["status"] == "draft"

    revisions = await db_module.list_paper_contract_revisions(db, project["id"], contract["id"])
    assert [r["revision_number"] for r in revisions] == [1, 2]


@pytest.mark.asyncio
async def test_approval_gate_pins_current_revision_and_supersedes_prior(db):
    project = await db_module.create_project(db, "paper-contract-approval-gate")
    contract = await db_module.create_paper_contract(db, project["id"], "main-paper", "Main paper")

    rev1 = await db_module.create_paper_contract_revision(
        db, project["id"], contract["id"],
        models.PaperContractContent(working_title="Draft v1").model_dump(),
    )
    approved1 = await db_module.approve_paper_contract_revision(db, project["id"], rev1["id"], "adam")
    assert approved1["approval_status"] == "approved"
    assert approved1["approved_by_human_id"] == "adam"
    assert approved1["approved_at"] is not None

    contract_after_1 = await db_module.get_paper_contract(db, project["id"], contract["id"])
    assert contract_after_1["current_revision_id"] == rev1["id"]
    assert contract_after_1["status"] == "active"

    rev2 = await db_module.create_paper_contract_revision(
        db, project["id"], contract["id"],
        models.PaperContractContent(working_title="Draft v2").model_dump(),
    )
    approved2 = await db_module.approve_paper_contract_revision(db, project["id"], rev2["id"], "adam")
    assert approved2["approval_status"] == "approved"

    contract_after_2 = await db_module.get_paper_contract(db, project["id"], contract["id"])
    assert contract_after_2["current_revision_id"] == rev2["id"]

    rev1_after = await db_module.get_paper_contract_revision(db, project["id"], rev1["id"])
    assert rev1_after["superseded_by_revision_id"] == rev2["id"]


@pytest.mark.asyncio
async def test_approval_is_idempotent_on_already_approved_revision(db):
    project = await db_module.create_project(db, "paper-contract-approval-idempotent")
    contract = await db_module.create_paper_contract(db, project["id"], "main-paper", "Main paper")
    rev = await db_module.create_paper_contract_revision(
        db, project["id"], contract["id"],
        models.PaperContractContent(working_title="Draft v1").model_dump(),
    )
    first = await db_module.approve_paper_contract_revision(db, project["id"], rev["id"], "adam")
    second = await db_module.approve_paper_contract_revision(db, project["id"], rev["id"], "adam")
    assert first["approved_at"] == second["approved_at"]


@pytest.mark.asyncio
async def test_approval_requires_nonempty_human_id(db):
    project = await db_module.create_project(db, "paper-contract-approval-requires-human")
    contract = await db_module.create_paper_contract(db, project["id"], "main-paper", "Main paper")
    rev = await db_module.create_paper_contract_revision(
        db, project["id"], contract["id"],
        models.PaperContractContent(working_title="Draft v1").model_dump(),
    )
    with pytest.raises(ValueError, match="non-empty approved_by_human_id"):
        await db_module.approve_paper_contract_revision(db, project["id"], rev["id"], "")
