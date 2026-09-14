"""Tests for sprint item W1-K -- Derivative-document (DOCX) provenance
tooling: register_docx_derivative, verify_docx_diff, promote_docx_candidate.

Mirrors tests/test_experiments.py / tests/test_research_run.py's structure:
model-layer validation tests (no DB), db-layer lifecycle tests (the `db`
fixture), and MCP-schema/dispatch-level smoke tests.
"""
from __future__ import annotations

import pytest

from meridian import db as db_module
from meridian import docx_derivative as model
from meridian.db import docx_derivatives as derivative_db
import meridian.mcp_tools as mcp_tools
import meridian.server as server

_SRC_HASH = "a" * 64
_SRC_HASH_2 = "b" * 64
_DERIV_HASH = "c" * 64


async def _session(db, prefix: str):
    project = await db_module.create_project(db, prefix)
    session = await db_module.register_session(db, project["id"], f"{prefix}-session")
    return project, session


# ---------------------------------------------------------------------------
# Model-layer validation (no DB)
# ---------------------------------------------------------------------------


def test_validate_docx_path_requires_docx_suffix():
    with pytest.raises(model.DocxDerivativeError, match="is required"):
        model.validate_docx_path(None, field="source_path")
    with pytest.raises(model.DocxDerivativeError, match="must name a .docx file"):
        model.validate_docx_path("thesis/chapter1.pdf", field="source_path")
    assert model.validate_docx_path("thesis/chapter1.docx", field="source_path") == "thesis/chapter1.docx"


def test_validate_docx_path_allows_absolute_paths():
    """Deliberate deviation from research_run/experiment: a docx source or
    derivative routinely lives outside the repo (see module docstring)."""
    win_path = "C:\\Users\\adam\\Documents\\chapter1.docx"
    assert model.validate_docx_path(win_path, field="source_path") == win_path
    unix_path = "/home/adam/Documents/chapter1.docx"
    assert model.validate_docx_path(unix_path, field="source_path") == unix_path


def test_validate_content_hash_optional_and_bounded():
    assert model.validate_content_hash(None, field="derivative_content_hash", required=False) is None
    with pytest.raises(model.DocxDerivativeError, match="is required"):
        model.validate_content_hash(None, field="source_content_hash", required=True)
    with pytest.raises(model.DocxDerivativeError, match="exceeds the"):
        model.validate_content_hash("x" * 200, field="source_content_hash", required=True)
    assert model.validate_content_hash(_SRC_HASH, field="source_content_hash", required=True) == _SRC_HASH


def test_validate_status_closed_vocab():
    with pytest.raises(model.DocxDerivativeError, match="status must be one of"):
        model.validate_status("draft")
    assert model.validate_status("Candidate") == "candidate"


def test_validate_generated_at_defaults_to_now_and_rejects_garbage():
    assert model.validate_generated_at(None)
    with pytest.raises(model.DocxDerivativeError, match="not a valid ISO-8601"):
        model.validate_generated_at("not-a-timestamp")
    with pytest.raises(model.DocxDerivativeError, match="must be an ISO-8601 timestamp string"):
        model.validate_generated_at("   ")
    with pytest.raises(model.DocxDerivativeError, match="must be an ISO-8601 timestamp string"):
        model.validate_generated_at(12345)
    assert model.validate_generated_at("2026-01-01T00:00:00Z").startswith("2026-01-01T00:00:00")
    # A naive (no tz/offset) timestamp is stamped UTC rather than rejected.
    assert model.validate_generated_at("2026-01-01T00:00:00").endswith("+00:00")


def test_validate_text_rejects_non_string_and_blank_when_required():
    with pytest.raises(model.DocxDerivativeError, match="must be a string"):
        model.validate_generating_tool(12345)
    with pytest.raises(model.DocxDerivativeError, match="is required"):
        model.validate_docx_path("   ", field="source_path")


def test_validate_register_fields_rejects_secret_shaped_notes():
    with pytest.raises(ValueError):
        model.validate_register_fields(
            source_path="a.docx", derivative_path="b.docx",
            source_content_hash=_SRC_HASH,
            notes="api_key=sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
        )


# ---------------------------------------------------------------------------
# DB-layer lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_creates_candidate_row(db):
    project, session = await _session(db, "docx-register")
    derivative = await derivative_db.register_docx_derivative(
        db, project["id"], session["id"],
        source_path="thesis/chapter1.docx", derivative_path="exports/chapter1.pdf.docx",
        source_content_hash=_SRC_HASH, generating_tool="pandoc 3.1",
    )
    assert derivative["status"] == "candidate"
    assert derivative["source_content_hash"] == _SRC_HASH
    assert derivative["generating_tool"] == "pandoc 3.1"
    assert derivative["creator_session_id"] == session["id"]
    assert derivative["generated_at"]
    assert derivative["created_at"] == derivative["updated_at"]

    fetched = await derivative_db.get_docx_derivative(db, project["id"], derivative_id=derivative["id"])
    assert fetched == derivative


@pytest.mark.asyncio
async def test_register_requires_valid_session(db):
    project = await db_module.create_project(db, "docx-badsession")
    with pytest.raises(ValueError, match="does not belong to project"):
        await derivative_db.register_docx_derivative(
            db, project["id"], "no-such-session",
            source_path="a.docx", derivative_path="b.docx",
            source_content_hash=_SRC_HASH,
        )


@pytest.mark.asyncio
async def test_register_rejects_empty_session_id(db):
    project = await db_module.create_project(db, "docx-emptysession")
    with pytest.raises(ValueError, match="session_id is required"):
        await derivative_db.register_docx_derivative(
            db, project["id"], "",
            source_path="a.docx", derivative_path="b.docx",
            source_content_hash=_SRC_HASH,
        )


@pytest.mark.asyncio
async def test_register_twice_creates_two_independent_candidates(db):
    """Re-rendering the same source/derivative pair over time is expected --
    each registration is its own row, never merged or overwritten."""
    project, session = await _session(db, "docx-reregister")
    first = await derivative_db.register_docx_derivative(
        db, project["id"], session["id"],
        source_path="thesis/chapter1.docx", derivative_path="exports/chapter1.docx",
        source_content_hash=_SRC_HASH,
    )
    second = await derivative_db.register_docx_derivative(
        db, project["id"], session["id"],
        source_path="thesis/chapter1.docx", derivative_path="exports/chapter1.docx",
        source_content_hash=_SRC_HASH_2,
    )
    assert first["id"] != second["id"]
    listed = await derivative_db.list_docx_derivatives(
        db, project["id"], source_path="thesis/chapter1.docx",
    )
    assert {d["id"] for d in listed} == {first["id"], second["id"]}


@pytest.mark.asyncio
async def test_verify_docx_diff_reports_not_stale_when_hash_matches(db):
    project, session = await _session(db, "docx-verify-fresh")
    derivative = await derivative_db.register_docx_derivative(
        db, project["id"], session["id"],
        source_path="a.docx", derivative_path="b.docx", source_content_hash=_SRC_HASH,
    )
    result = await derivative_db.verify_docx_diff(
        db, project["id"],
        derivative_id=derivative["id"], current_source_content_hash=_SRC_HASH,
    )
    assert result["is_stale"] is False
    assert result["source_changed"] is False
    assert result["derivative"]["last_verify_is_stale"] is False
    assert result["derivative"]["last_verified_at"]


@pytest.mark.asyncio
async def test_verify_docx_diff_flags_staleness_when_source_hash_changed(db):
    project, session = await _session(db, "docx-verify-stale")
    derivative = await derivative_db.register_docx_derivative(
        db, project["id"], session["id"],
        source_path="a.docx", derivative_path="b.docx", source_content_hash=_SRC_HASH,
    )
    result = await derivative_db.verify_docx_diff(
        db, project["id"],
        derivative_id=derivative["id"], current_source_content_hash=_SRC_HASH_2,
    )
    assert result["is_stale"] is True
    assert result["source_changed"] is True
    assert "source document has changed" in result["reason"]
    assert result["derivative"]["last_verify_is_stale"] is True
    assert result["derivative"]["last_verify_reason"] == result["reason"]


@pytest.mark.asyncio
async def test_verify_docx_diff_detects_derivative_drift_when_source_unchanged(db):
    project, session = await _session(db, "docx-verify-drift")
    derivative = await derivative_db.register_docx_derivative(
        db, project["id"], session["id"],
        source_path="a.docx", derivative_path="b.docx",
        source_content_hash=_SRC_HASH, derivative_content_hash=_DERIV_HASH,
    )
    result = await derivative_db.verify_docx_diff(
        db, project["id"],
        derivative_id=derivative["id"],
        current_source_content_hash=_SRC_HASH,
        current_derivative_content_hash="d" * 64,
    )
    assert result["is_stale"] is False  # source itself is unchanged
    assert result["derivative_changed"] is True
    assert "derivative's own current content hash" in result["reason"]


@pytest.mark.asyncio
async def test_verify_docx_diff_rejects_unknown_derivative(db):
    project, _session_row = await _session(db, "docx-verify-missing")
    with pytest.raises(ValueError, match="not found in project"):
        await derivative_db.verify_docx_diff(
            db, project["id"],
            derivative_id="no-such-id", current_source_content_hash=_SRC_HASH,
        )


@pytest.mark.asyncio
async def test_promote_transitions_candidate_to_accepted_with_audit_fields(db):
    project, session = await _session(db, "docx-promote")
    derivative = await derivative_db.register_docx_derivative(
        db, project["id"], session["id"],
        source_path="a.docx", derivative_path="b.docx", source_content_hash=_SRC_HASH,
    )
    result = await derivative_db.promote_docx_candidate(
        db, project["id"], session["id"], derivative_id=derivative["id"],
    )
    assert result["idempotent_retry"] is False
    assert result["superseded_derivative_id"] is None
    assert result["derivative"]["status"] == "accepted"
    assert result["derivative"]["promoted_at"]
    assert result["derivative"]["promoted_by_session_id"] == session["id"]


@pytest.mark.asyncio
async def test_promote_supersedes_previously_accepted_derivative_for_same_source(db):
    """Real state transition + audit trail (mirrors promote_experiment_run's
    precedent): promoting a NEW candidate for a source that already has an
    accepted derivative demotes the old one to 'superseded' in the same call."""
    project, session = await _session(db, "docx-promote-supersede")
    first = await derivative_db.register_docx_derivative(
        db, project["id"], session["id"],
        source_path="a.docx", derivative_path="v1.docx", source_content_hash=_SRC_HASH,
    )
    await derivative_db.promote_docx_candidate(db, project["id"], session["id"], derivative_id=first["id"])

    second = await derivative_db.register_docx_derivative(
        db, project["id"], session["id"],
        source_path="a.docx", derivative_path="v2.docx", source_content_hash=_SRC_HASH_2,
    )
    result = await derivative_db.promote_docx_candidate(
        db, project["id"], session["id"], derivative_id=second["id"],
    )
    assert result["derivative"]["status"] == "accepted"
    assert result["superseded_derivative_id"] == first["id"]

    old = await derivative_db.get_docx_derivative(db, project["id"], derivative_id=first["id"])
    assert old["status"] == "superseded"
    assert old["superseded_at"]
    assert old["superseded_by_derivative_id"] == second["id"]


@pytest.mark.asyncio
async def test_promote_is_idempotent_on_already_accepted_derivative(db):
    project, session = await _session(db, "docx-promote-idempotent")
    derivative = await derivative_db.register_docx_derivative(
        db, project["id"], session["id"],
        source_path="a.docx", derivative_path="b.docx", source_content_hash=_SRC_HASH,
    )
    first = await derivative_db.promote_docx_candidate(
        db, project["id"], session["id"], derivative_id=derivative["id"],
    )
    second = await derivative_db.promote_docx_candidate(
        db, project["id"], session["id"], derivative_id=derivative["id"],
    )
    assert second["idempotent_retry"] is True
    assert second["derivative"]["promoted_at"] == first["derivative"]["promoted_at"]


@pytest.mark.asyncio
async def test_promote_rejects_superseded_derivative(db):
    project, session = await _session(db, "docx-promote-superseded-guard")
    first = await derivative_db.register_docx_derivative(
        db, project["id"], session["id"],
        source_path="a.docx", derivative_path="v1.docx", source_content_hash=_SRC_HASH,
    )
    await derivative_db.promote_docx_candidate(db, project["id"], session["id"], derivative_id=first["id"])
    second = await derivative_db.register_docx_derivative(
        db, project["id"], session["id"],
        source_path="a.docx", derivative_path="v2.docx", source_content_hash=_SRC_HASH_2,
    )
    await derivative_db.promote_docx_candidate(db, project["id"], session["id"], derivative_id=second["id"])

    # `first` is now superseded -- must never be re-promotable.
    with pytest.raises(ValueError, match="cannot be re-promoted"):
        await derivative_db.promote_docx_candidate(db, project["id"], session["id"], derivative_id=first["id"])


@pytest.mark.asyncio
async def test_promote_rejects_unknown_derivative(db):
    project, session = await _session(db, "docx-promote-missing")
    with pytest.raises(ValueError, match="not found in project"):
        await derivative_db.promote_docx_candidate(
            db, project["id"], session["id"], derivative_id="no-such-id",
        )


@pytest.mark.asyncio
async def test_list_docx_derivatives_scopes_by_project_and_status(db):
    project_a, session_a = await _session(db, "docx-list-a")
    project_b, session_b = await _session(db, "docx-list-b")
    d1 = await derivative_db.register_docx_derivative(
        db, project_a["id"], session_a["id"],
        source_path="a.docx", derivative_path="b.docx", source_content_hash=_SRC_HASH,
    )
    await derivative_db.register_docx_derivative(
        db, project_b["id"], session_b["id"],
        source_path="a.docx", derivative_path="b.docx", source_content_hash=_SRC_HASH,
    )
    await derivative_db.promote_docx_candidate(db, project_a["id"], session_a["id"], derivative_id=d1["id"])

    project_a_all = await derivative_db.list_docx_derivatives(db, project_a["id"])
    assert {d["id"] for d in project_a_all} == {d1["id"]}

    accepted_only = await derivative_db.list_docx_derivatives(db, project_a["id"], status="accepted")
    assert {d["id"] for d in accepted_only} == {d1["id"]}

    candidate_only = await derivative_db.list_docx_derivatives(db, project_a["id"], status="candidate")
    assert candidate_only == []


# ---------------------------------------------------------------------------
# MCP schema / dispatch
# ---------------------------------------------------------------------------


def test_mcp_tools_advertise_docx_derivative_surface():
    names = {tool["name"] for tool in mcp_tools._MCP_TOOLS_LIST}
    assert {"register_docx_derivative", "verify_docx_diff", "promote_docx_candidate"} <= names


def test_docx_derivative_tools_have_docx_category():
    by_name = {tool["name"]: tool for tool in mcp_tools._MCP_TOOLS_LIST}
    for name in ("register_docx_derivative", "verify_docx_diff", "promote_docx_candidate"):
        assert by_name[name]["category"] == "docx"
        assert by_name[name]["annotations"]["readOnlyHint"] is False


@pytest.mark.asyncio
async def test_mcp_dispatch_register_verify_promote_end_to_end(db, tmp_path):
    project, session = await _session(db, "docx-mcp")
    registered = await server._dispatch_mcp_tool(
        "register_docx_derivative",
        {
            "project_id": project["id"], "session_id": session["id"],
            "source_path": "thesis/chapter1.docx", "derivative_path": "exports/chapter1.pdf.docx",
            "source_content_hash": _SRC_HASH,
        },
        db, str(tmp_path),
    )
    derivative_id = registered["derivative"]["id"]
    assert registered["derivative"]["status"] == "candidate"

    fresh = await server._dispatch_mcp_tool(
        "verify_docx_diff",
        {"project_id": project["id"], "derivative_id": derivative_id, "current_source_content_hash": _SRC_HASH},
        db, str(tmp_path),
    )
    assert fresh["is_stale"] is False

    stale = await server._dispatch_mcp_tool(
        "verify_docx_diff",
        {"project_id": project["id"], "derivative_id": derivative_id, "current_source_content_hash": _SRC_HASH_2},
        db, str(tmp_path),
    )
    assert stale["is_stale"] is True

    promoted = await server._dispatch_mcp_tool(
        "promote_docx_candidate",
        {"project_id": project["id"], "session_id": session["id"], "derivative_id": derivative_id},
        db, str(tmp_path),
    )
    assert promoted["derivative"]["status"] == "accepted"
    assert promoted["idempotent_retry"] is False

    missing = await server._dispatch_mcp_tool(
        "verify_docx_diff",
        {"project_id": project["id"], "derivative_id": "no-such-id", "current_source_content_hash": _SRC_HASH},
        db, str(tmp_path),
    )
    assert "error" in missing

    rejected = await server._dispatch_mcp_tool(
        "register_docx_derivative",
        {
            "project_id": project["id"], "session_id": session["id"],
            "source_path": "thesis/chapter1.pdf", "derivative_path": "exports/chapter1.pdf.docx",
            "source_content_hash": _SRC_HASH,
        },
        db, str(tmp_path),
    )
    assert "error" in rejected

    promote_missing = await server._dispatch_mcp_tool(
        "promote_docx_candidate",
        {"project_id": project["id"], "session_id": session["id"], "derivative_id": "no-such-id"},
        db, str(tmp_path),
    )
    assert "error" in promote_missing
