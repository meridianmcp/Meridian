"""Tests for sprint item d2539453 — SCHEMA: lint_finding, structured paper
audit output with version pinning.

Covers:
  * meridian.db.lint_finding — the migration (idempotent, wired into
    init_db), the closed severity/status vocabularies, CRUD (create/get/
    list/set_status), and the pure check_finding_freshness comparator.
  * meridian.models.LintFindingCreate / LintFinding / LintFindingStatusUpdate
    — Pydantic validation, valid and invalid cases.

Focused and serial-safe: every test builds its own project row on the
per-test `db` fixture and touches no external state.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from meridian import db as db_module
from meridian import models
from meridian.db import lint_finding as lf


# ---------------------------------------------------------------------------
# Closed vocabularies — pure functions, no DB.
# ---------------------------------------------------------------------------


def test_severity_and_status_vocabularies_are_documented():
    assert lf.LINT_FINDING_SEVERITIES == {"error", "warning", "info"}
    assert lf.LINT_FINDING_STATUSES == {"open", "acknowledged", "resolved", "dismissed"}


def test_validate_severity_accepts_all_and_normalizes_case():
    for severity in lf.LINT_FINDING_SEVERITIES:
        assert lf.validate_lint_finding_severity(severity) == severity
        assert lf.validate_lint_finding_severity(severity.upper()) == severity


def test_validate_severity_rejects_unknown():
    with pytest.raises(ValueError, match="severity must be one of"):
        lf.validate_lint_finding_severity("critical")


def test_validate_status_accepts_all_and_normalizes_case():
    for status in lf.LINT_FINDING_STATUSES:
        assert lf.validate_lint_finding_status(status) == status
        assert lf.validate_lint_finding_status(status.upper()) == status


def test_validate_status_rejects_unknown():
    with pytest.raises(ValueError, match="status must be one of"):
        lf.validate_lint_finding_status("archived")


# ---------------------------------------------------------------------------
# check_finding_freshness — pure, fail-open comparator (mirrors
# doc_store._docx_staleness_check's contract).
# ---------------------------------------------------------------------------


def test_freshness_matching_fingerprint_is_none():
    finding = {"source_fingerprint": "abc123"}
    assert lf.check_finding_freshness(finding, "abc123") is None


def test_freshness_mismatched_fingerprint_reports_stale():
    finding = {"source_fingerprint": "abc123"}
    result = lf.check_finding_freshness(finding, "def456")
    assert result is not None
    assert result["stale"] is True
    assert result["pinned_source_fingerprint"] == "abc123"
    assert result["current_source_fingerprint"] == "def456"


def test_freshness_fails_open_with_no_current_fingerprint():
    """No current fingerprint to compare against -> never conclude staleness."""
    finding = {"source_fingerprint": "abc123"}
    assert lf.check_finding_freshness(finding, None) is None
    assert lf.check_finding_freshness(finding, "") is None


def test_freshness_fails_open_with_no_stored_fingerprint():
    finding = {"source_fingerprint": None}
    assert lf.check_finding_freshness(finding, "def456") is None


# ---------------------------------------------------------------------------
# Migration — idempotent, wired into the real init_db startup chain.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_migration_creates_table_and_indexes(db):
    async with db.execute("PRAGMA table_info(lint_findings)") as cur:
        cols = {row["name"] for row in await cur.fetchall()}
    assert cols == {
        "id", "project_id", "document_id", "audit_run_id", "linter_name",
        "linter_version", "source_fingerprint", "category", "finding_type",
        "severity", "message", "location", "detail", "status", "resolved_by",
        "resolution_note", "created_at", "updated_at", "resolved_at",
    }
    async with db.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='lint_findings'"
    ) as cur:
        index_names = {row["name"] for row in await cur.fetchall()}
    assert "idx_lint_findings_project" in index_names
    assert "idx_lint_findings_document" in index_names
    assert "idx_lint_findings_audit_run" in index_names


@pytest.mark.asyncio
async def test_migration_is_idempotent(db):
    # init_db (via the `db` fixture's schema template) already ran this
    # migration once; running it again directly must not raise.
    await lf._migrate_lint_finding(db)
    await lf._migrate_lint_finding(db)


def test_pg_mirror_is_registered_in_late_migrations():
    """Postgres mirror exists and is wired into the late-migration tuple —
    see tests/test_core.py::test_pg_migration_registry_matches_historical_order
    for the full-registry version of this check."""
    from meridian import pg_adapter as pg_module

    assert pg_module._migrate_pg_lint_finding in pg_module._PG_MIGRATIONS_LATE


# ---------------------------------------------------------------------------
# CRUD round-trip.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_and_get_round_trip(db):
    project = await db_module.create_project(db, "lint-1")
    created = await lf.create_lint_finding(
        db, project["id"],
        linter_name="meridian_docs.audit_document",
        linter_version="1.0.0",
        source_fingerprint="a" * 64,
        category="caption",
        finding_type="orphan_image",
        message="image at paragraph 12 has no adjacent caption",
        severity="warning",
        document_id="doc-1",
        audit_run_id="run-1",
        location={"para_id": "p12"},
        detail={"composite_size": 1},
    )
    assert created["project_id"] == project["id"]
    assert created["severity"] == "warning"
    assert created["status"] == "open"
    assert created["location"] == {"para_id": "p12"}
    assert created["detail"] == {"composite_size": 1}
    assert created["resolved_at"] is None

    fetched = await lf.get_lint_finding(db, project["id"], created["id"])
    assert fetched == created


@pytest.mark.asyncio
async def test_create_defaults_severity_to_warning_and_allows_null_optionals(db):
    project = await db_module.create_project(db, "lint-2")
    created = await lf.create_lint_finding(
        db, project["id"],
        linter_name="meridian_docs.audit_equation_style",
        linter_version="2026.08.1",
        source_fingerprint="b" * 64,
        category="equation",
        finding_type="missing_trailing_punctuation",
        message="equation 3 is missing trailing punctuation",
    )
    assert created["severity"] == "warning"
    assert created["document_id"] is None
    assert created["audit_run_id"] is None
    assert created["location"] is None
    assert created["detail"] is None


@pytest.mark.asyncio
async def test_get_cross_project_returns_none(db):
    p1 = await db_module.create_project(db, "lint-3a")
    p2 = await db_module.create_project(db, "lint-3b")
    created = await lf.create_lint_finding(
        db, p1["id"],
        linter_name="x", linter_version="1", source_fingerprint="c" * 64,
        category="citation", finding_type="dangling_ref", message="dangling citation",
    )
    assert await lf.get_lint_finding(db, p2["id"], created["id"]) is None
    assert await lf.get_lint_finding(db, p1["id"], created["id"]) is not None


@pytest.mark.asyncio
async def test_get_unknown_id_returns_none(db):
    project = await db_module.create_project(db, "lint-4")
    assert await lf.get_lint_finding(db, project["id"], "does-not-exist") is None


@pytest.mark.asyncio
async def test_create_rejects_empty_required_fields(db):
    project = await db_module.create_project(db, "lint-5")
    with pytest.raises(ValueError, match="non-empty"):
        await lf.create_lint_finding(
            db, project["id"],
            linter_name="   ", linter_version="1", source_fingerprint="d" * 64,
            category="equation", finding_type="x", message="msg",
        )
    with pytest.raises(ValueError, match="non-empty"):
        await lf.create_lint_finding(
            db, "", linter_name="x", linter_version="1", source_fingerprint="d" * 64,
            category="equation", finding_type="x", message="msg",
        )


@pytest.mark.asyncio
async def test_create_rejects_invalid_severity(db):
    project = await db_module.create_project(db, "lint-6")
    with pytest.raises(ValueError, match="severity must be one of"):
        await lf.create_lint_finding(
            db, project["id"],
            linter_name="x", linter_version="1", source_fingerprint="e" * 64,
            category="equation", finding_type="x", message="msg", severity="fatal",
        )


@pytest.mark.asyncio
async def test_create_rejects_secret_looking_message(db):
    project = await db_module.create_project(db, "lint-7")
    with pytest.raises(ValueError, match="Refusing to persist"):
        await lf.create_lint_finding(
            db, project["id"],
            linter_name="x", linter_version="1", source_fingerprint="f" * 64,
            category="equation", finding_type="x",
            message="sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        )


@pytest.mark.asyncio
async def test_create_rejects_unknown_project_fk(db):
    with pytest.raises(Exception, match="(?i)foreign key"):
        await lf.create_lint_finding(
            db, "no-such-project",
            linter_name="x", linter_version="1", source_fingerprint="0" * 64,
            category="equation", finding_type="x", message="msg",
        )


@pytest.mark.asyncio
async def test_list_filters_by_document_status_severity(db):
    project = await db_module.create_project(db, "lint-8")
    f1 = await lf.create_lint_finding(
        db, project["id"], linter_name="x", linter_version="1",
        source_fingerprint="1" * 64, category="equation", finding_type="a",
        message="finding a", severity="error", document_id="doc-A",
    )
    f2 = await lf.create_lint_finding(
        db, project["id"], linter_name="x", linter_version="1",
        source_fingerprint="1" * 64, category="caption", finding_type="b",
        message="finding b", severity="info", document_id="doc-B",
    )
    await lf.set_lint_finding_status(db, project["id"], f2["id"], "resolved")

    all_findings = await lf.list_lint_findings(db, project["id"])
    assert {f["id"] for f in all_findings} == {f1["id"], f2["id"]}

    by_doc = await lf.list_lint_findings(db, project["id"], document_id="doc-A")
    assert [f["id"] for f in by_doc] == [f1["id"]]

    by_status = await lf.list_lint_findings(db, project["id"], status="resolved")
    assert [f["id"] for f in by_status] == [f2["id"]]

    by_severity = await lf.list_lint_findings(db, project["id"], severity="error")
    assert [f["id"] for f in by_severity] == [f1["id"]]


@pytest.mark.asyncio
async def test_list_scoped_to_project(db):
    p1 = await db_module.create_project(db, "lint-9a")
    p2 = await db_module.create_project(db, "lint-9b")
    await lf.create_lint_finding(
        db, p1["id"], linter_name="x", linter_version="1",
        source_fingerprint="2" * 64, category="equation", finding_type="a",
        message="p1 finding",
    )
    assert await lf.list_lint_findings(db, p2["id"]) == []
    assert len(await lf.list_lint_findings(db, p1["id"])) == 1


# ---------------------------------------------------------------------------
# Status lifecycle.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_status_transition_stamps_resolved_at(db):
    project = await db_module.create_project(db, "lint-10")
    created = await lf.create_lint_finding(
        db, project["id"], linter_name="x", linter_version="1",
        source_fingerprint="3" * 64, category="equation", finding_type="a",
        message="msg",
    )
    assert created["resolved_at"] is None

    acknowledged = await lf.set_lint_finding_status(
        db, project["id"], created["id"], "acknowledged"
    )
    assert acknowledged["status"] == "acknowledged"
    assert acknowledged["resolved_at"] is None

    resolved = await lf.set_lint_finding_status(
        db, project["id"], created["id"], "resolved",
        resolved_by="ajc123private@gmail.com", resolution_note="fixed the caption",
    )
    assert resolved["status"] == "resolved"
    assert resolved["resolved_by"] == "ajc123private@gmail.com"
    assert resolved["resolution_note"] == "fixed the caption"
    assert resolved["resolved_at"] is not None


@pytest.mark.asyncio
async def test_set_status_reopen_clears_resolved_at(db):
    project = await db_module.create_project(db, "lint-11")
    created = await lf.create_lint_finding(
        db, project["id"], linter_name="x", linter_version="1",
        source_fingerprint="4" * 64, category="equation", finding_type="a",
        message="msg",
    )
    resolved = await lf.set_lint_finding_status(db, project["id"], created["id"], "dismissed")
    assert resolved["resolved_at"] is not None

    reopened = await lf.set_lint_finding_status(db, project["id"], created["id"], "open")
    assert reopened["status"] == "open"
    assert reopened["resolved_at"] is None


@pytest.mark.asyncio
async def test_set_status_same_status_is_idempotent_noop(db):
    project = await db_module.create_project(db, "lint-12")
    created = await lf.create_lint_finding(
        db, project["id"], linter_name="x", linter_version="1",
        source_fingerprint="5" * 64, category="equation", finding_type="a",
        message="msg",
    )
    same = await lf.set_lint_finding_status(db, project["id"], created["id"], "open")
    assert same["status"] == "open"


@pytest.mark.asyncio
async def test_set_status_unknown_finding_raises(db):
    project = await db_module.create_project(db, "lint-13")
    with pytest.raises(ValueError, match="not found in project"):
        await lf.set_lint_finding_status(db, project["id"], "no-such-id", "resolved")


@pytest.mark.asyncio
async def test_set_status_rejects_invalid_status(db):
    project = await db_module.create_project(db, "lint-14")
    created = await lf.create_lint_finding(
        db, project["id"], linter_name="x", linter_version="1",
        source_fingerprint="6" * 64, category="equation", finding_type="a",
        message="msg",
    )
    with pytest.raises(ValueError, match="status must be one of"):
        await lf.set_lint_finding_status(db, project["id"], created["id"], "archived")


# ---------------------------------------------------------------------------
# Pydantic models — valid and invalid cases.
# ---------------------------------------------------------------------------


def test_lint_finding_create_valid():
    model = models.LintFindingCreate(
        project_id="proj-1",
        linter_name="meridian_docs.audit_document",
        linter_version="1.0.0",
        source_fingerprint="a" * 64,
        category="caption",
        finding_type="orphan_image",
        message="image has no caption",
    )
    assert model.severity == "warning"
    assert model.document_id is None
    assert model.audit_run_id is None


def test_lint_finding_create_accepts_optional_fields():
    model = models.LintFindingCreate(
        project_id="proj-1",
        linter_name="x",
        linter_version="1",
        source_fingerprint="a" * 64,
        category="equation",
        finding_type="gap",
        message="msg",
        severity="error",
        document_id="doc-1",
        audit_run_id="run-1",
        location={"para_id": "p1"},
        detail={"k": "v"},
    )
    assert model.severity == "error"
    assert model.document_id == "doc-1"
    assert model.location == {"para_id": "p1"}


@pytest.mark.parametrize(
    "field,value",
    [
        ("project_id", ""),
        ("linter_name", ""),
        ("linter_version", ""),
        ("source_fingerprint", ""),
        ("category", ""),
        ("finding_type", ""),
        ("message", ""),
    ],
)
def test_lint_finding_create_rejects_empty_required_string(field, value):
    kwargs = dict(
        project_id="proj-1",
        linter_name="x",
        linter_version="1",
        source_fingerprint="a" * 64,
        category="equation",
        finding_type="gap",
        message="msg",
    )
    kwargs[field] = value
    with pytest.raises(ValidationError):
        models.LintFindingCreate(**kwargs)


def test_lint_finding_create_missing_required_field_raises():
    with pytest.raises(ValidationError):
        models.LintFindingCreate(
            project_id="proj-1",
            linter_name="x",
            linter_version="1",
            source_fingerprint="a" * 64,
            category="equation",
            # finding_type omitted
            message="msg",
        )


def test_lint_finding_create_rejects_invalid_severity():
    with pytest.raises(ValidationError):
        models.LintFindingCreate(
            project_id="proj-1",
            linter_name="x",
            linter_version="1",
            source_fingerprint="a" * 64,
            category="equation",
            finding_type="gap",
            message="msg",
            severity="fatal",
        )


def test_lint_finding_response_model_round_trips_a_db_row_dict():
    row = {
        "id": "f1",
        "project_id": "proj-1",
        "document_id": None,
        "audit_run_id": None,
        "linter_name": "x",
        "linter_version": "1",
        "source_fingerprint": "a" * 64,
        "category": "equation",
        "finding_type": "gap",
        "severity": "info",
        "message": "msg",
        "location": None,
        "detail": None,
        "status": "open",
        "resolved_by": None,
        "resolution_note": None,
        "created_at": "2026-09-07 00:00:00",
        "updated_at": "2026-09-07 00:00:00",
        "resolved_at": None,
    }
    model = models.LintFinding(**row)
    assert model.id == "f1"
    assert model.severity == "info"


def test_lint_finding_status_update_valid_and_invalid():
    valid = models.LintFindingStatusUpdate(project_id="proj-1", status="resolved")
    assert valid.status == "resolved"
    with pytest.raises(ValidationError):
        models.LintFindingStatusUpdate(project_id="proj-1", status="archived")
