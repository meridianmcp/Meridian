"""Tests for sprint item 5060eea1 — project family / template revision
DESIGN ARTIFACTS (parent item ddcf6984, "project-family-templates").

This item is design-only: no migration, route, handler, or DB function for
any of these concepts exists yet. Accordingly these tests validate ONLY the
design artifacts themselves:

1. The new Pydantic data-contract classes in ``meridian.models`` have the
   expected required fields, types, and defaults.
2. Example payloads round-trip through those models correctly (construct ->
   ``model_dump`` -> reconstruct, and JSON round trip).
3. The design doc at
   ``docs/meridian-project-family-template-revisions-design.md`` exists and
   contains every section required by this item's acceptance notes.

Nothing here assumes a real DB table, route, or MCP handler exists — there
is none yet. See the design doc itself for the full rationale.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from meridian import models


# ---------------------------------------------------------------------------
# 1 & 2. Pydantic model shape + round-trip
# ---------------------------------------------------------------------------


class TestProjectTemplateCreate:
    def test_minimal_construction_defaults(self):
        m = models.ProjectTemplateCreate(name="web-service-python")
        assert m.name == "web-service-python"
        assert m.description is None
        assert m.fields == {}
        assert m.schema_version == 1
        assert m.provenance is None
        assert m.created_by_human_id is None

    def test_requires_name(self):
        with pytest.raises(ValidationError):
            models.ProjectTemplateCreate()

    def test_rejects_empty_name(self):
        with pytest.raises(ValidationError):
            models.ProjectTemplateCreate(name="")

    def test_full_round_trip(self):
        payload = {
            "name": "web-service-python",
            "description": "FastAPI + pixi baseline",
            "fields": {"test_cmd": "pixi run test", "python_version": "3.12"},
            "schema_version": 2,
            "provenance": {"source": "AGENTS.md"},
            "created_by_human_id": "adam",
        }
        m = models.ProjectTemplateCreate(**payload)
        dumped = m.model_dump()
        assert dumped == payload
        # JSON round trip
        restored = models.ProjectTemplateCreate(**json.loads(json.dumps(dumped)))
        assert restored == m


class TestProjectTemplate:
    def test_required_fields(self):
        m = models.ProjectTemplate(id="t1", name="web-service-python", created_at="2026-08-29T00:00:00Z")
        assert m.id == "t1"
        assert m.schema_version == 1
        assert m.latest_revision_id is None
        assert m.latest_revision_number == 0
        assert m.forked_from_template_id is None
        assert m.forked_from_revision_id is None

    def test_fork_provenance_round_trip(self):
        m = models.ProjectTemplate(
            id="t2",
            name="web-service-python-strict",
            latest_revision_id="t2:r1",
            latest_revision_number=1,
            forked_from_template_id="t1",
            forked_from_revision_id="t1:r3",
            created_at="2026-08-29T00:00:00Z",
        )
        restored = models.ProjectTemplate(**json.loads(m.model_dump_json()))
        assert restored == m

    def test_missing_required_field_rejected(self):
        with pytest.raises(ValidationError):
            models.ProjectTemplate(id="t1", name="x")  # missing created_at


class TestTemplateRevisionCreateAndSnapshot:
    def test_revision_create_minimal(self):
        m = models.TemplateRevisionCreate(template_id="t1")
        assert m.fields == {}
        assert m.schema_version is None  # None => inherit template's current schema_version
        assert m.changelog is None

    def test_revision_snapshot_required_fields(self):
        with pytest.raises(ValidationError):
            models.TemplateRevisionSnapshot(id="r1", template_id="t1")  # missing revision_id/number/etc.

    def test_revision_snapshot_round_trip(self):
        payload = {
            "id": "row-uuid-1",
            "revision_id": "t1:r1",
            "template_id": "t1",
            "revision_number": 1,
            "schema_version": 1,
            "fields": {"test_cmd": "pixi run test"},
            "content_hash": "sha256:" + "0" * 64,
            "changelog": "initial revision",
            "provenance": None,
            "superseded_by_revision_id": None,
            "rollback_of_revision_id": None,
            "created_at": "2026-08-29T00:00:00Z",
        }
        m = models.TemplateRevisionSnapshot(**payload)
        assert m.model_dump() == payload
        restored = models.TemplateRevisionSnapshot(**json.loads(m.model_dump_json()))
        assert restored == m

    def test_revision_number_must_be_positive(self):
        base = dict(
            id="row-uuid-1", revision_id="t1:r0", template_id="t1",
            schema_version=1, fields={}, content_hash="sha256:" + "0" * 64,
            created_at="2026-08-29T00:00:00Z",
        )
        with pytest.raises(ValidationError):
            models.TemplateRevisionSnapshot(revision_number=0, **base)

    def test_supersession_pointer_is_settable(self):
        """Design doc section (f): superseded_by_revision_id is the only
        field on an otherwise-immutable revision that legitimately changes
        after creation, when a newer revision supersedes it."""
        m = models.TemplateRevisionSnapshot(
            id="row-uuid-1", revision_id="t1:r1", template_id="t1", revision_number=1,
            schema_version=1, fields={}, content_hash="sha256:" + "0" * 64,
            created_at="2026-08-29T00:00:00Z",
        )
        assert m.superseded_by_revision_id is None
        superseded = m.model_copy(update={"superseded_by_revision_id": "t1:r2"})
        assert superseded.superseded_by_revision_id == "t1:r2"
        # original untouched — model_copy does not mutate in place
        assert m.superseded_by_revision_id is None

    def test_rollback_of_revision_id_round_trip(self):
        m = models.TemplateRevisionSnapshot(
            id="row-uuid-9", revision_id="t1:r5", template_id="t1", revision_number=5,
            schema_version=1, fields={"a": 1}, content_hash="sha256:" + "1" * 64,
            rollback_of_revision_id="t1:r2", created_at="2026-08-29T00:00:00Z",
        )
        assert m.rollback_of_revision_id == "t1:r2"


class TestTemplateForkRequest:
    def test_required_fields(self):
        with pytest.raises(ValidationError):
            models.TemplateForkRequest(source_template_id="t1", source_revision_id="t1:r1")  # missing new_template_name

    def test_full_round_trip(self):
        payload = {
            "source_template_id": "t1",
            "source_revision_id": "t1:r3",
            "new_template_name": "web-service-python-strict",
            "description": "Stricter variant",
            "field_overrides": {"test_min": 90},
            "actor": "session-1",
        }
        m = models.TemplateForkRequest(**payload)
        assert m.model_dump() == payload


class TestTemplateOverrideAndChildOverride:
    def test_override_set_defaults(self):
        m = models.TemplateOverrideSet(child_project_id="c1", template_id="t1")
        assert m.fields == {}
        assert m.reset_fields == []
        assert m.expected_override_revision is None

    def test_override_set_round_trip(self):
        payload = {
            "child_project_id": "c1",
            "template_id": "t1",
            "fields": {"test_cmd": "pixi run test -k child_only"},
            "reset_fields": ["python_version"],
            "expected_override_revision": 2,
            "actor": "session-1",
        }
        m = models.TemplateOverrideSet(**payload)
        assert m.model_dump() == payload

    def test_child_template_override_round_trip(self):
        payload = {
            "child_project_id": "c1",
            "template_id": "t1",
            "fields": {"test_cmd": "pixi run test -k child_only"},
            "reset_fields": ["python_version"],
            "override_revision": 3,
            "content_hash": "sha256:" + "2" * 64,
            "updated_at": "2026-08-29T00:00:00Z",
        }
        m = models.ChildTemplateOverride(**payload)
        assert m.model_dump() == payload
        restored = models.ChildTemplateOverride(**json.loads(m.model_dump_json()))
        assert restored == m


class TestConfigDiffEntry:
    def test_defaults(self):
        m = models.ConfigDiffEntry(path="build.timeout_seconds", op="changed", base_value=30, new_value=60)
        assert m.source == "template"

    @pytest.mark.parametrize("op", ["added", "removed", "changed"])
    def test_valid_ops(self, op):
        m = models.ConfigDiffEntry(path="x", op=op)
        assert m.op == op

    def test_invalid_op_rejected(self):
        with pytest.raises(ValidationError):
            models.ConfigDiffEntry(path="x", op="renamed")

    def test_invalid_source_rejected(self):
        with pytest.raises(ValidationError):
            models.ConfigDiffEntry(path="x", op="added", source="child")  # must be "template" or "override"

    def test_override_source_round_trip(self):
        payload = {"path": "test_cmd", "op": "changed", "base_value": "pixi run test", "new_value": "pixi run test -k x", "source": "override"}
        m = models.ConfigDiffEntry(**payload)
        assert m.model_dump() == payload


class TestPreviewFlow:
    def test_preview_request_required_fields(self):
        with pytest.raises(ValidationError):
            models.TemplateAdoptionPreviewRequest(child_project_id="c1")  # missing candidate_revision_id

    def test_preview_response_defaults(self):
        m = models.TemplateOverridePreview(
            child_project_id="c1", template_id="t1", candidate_revision_id="t1:r2",
            candidate_effective_hash="sha256:" + "3" * 64,
        )
        assert m.current_revision_id is None
        assert m.diff == []
        assert m.conflicts == []
        assert m.compatible is True
        assert m.schema_version_change is False

    def test_preview_response_with_conflicts_round_trip(self):
        diff_entry = models.ConfigDiffEntry(path="python_version", op="changed", base_value="3.11", new_value="3.12", source="template")
        m = models.TemplateOverridePreview(
            child_project_id="c1",
            template_id="t1",
            current_revision_id="t1:r1",
            candidate_revision_id="t1:r2",
            current_effective_hash="sha256:" + "4" * 64,
            candidate_effective_hash="sha256:" + "5" * 64,
            diff=[diff_entry],
            conflicts=["python_version"],
            compatible=False,
            schema_version_change=True,
        )
        dumped = m.model_dump()
        assert dumped["conflicts"] == ["python_version"]
        assert dumped["diff"][0]["path"] == "python_version"
        restored = models.TemplateOverridePreview(**json.loads(m.model_dump_json()))
        assert restored == m


class TestAdoptRejectRollback:
    def test_adopt_request_defaults(self):
        m = models.TemplateAdoptRequest(child_project_id="c1", revision_id="t1:r2")
        assert m.force_accept_conflicts is False
        assert m.override_reason is None
        assert m.expected_snapshot_revision is None

    def test_adopt_request_forced_round_trip(self):
        payload = {
            "child_project_id": "c1",
            "revision_id": "t1:r2",
            "expected_snapshot_revision": 1,
            "force_accept_conflicts": True,
            "override_reason": "reviewed the python_version bump manually",
            "actor": "session-1",
        }
        m = models.TemplateAdoptRequest(**payload)
        assert m.model_dump() == payload

    def test_reject_request_round_trip(self):
        payload = {"child_project_id": "c1", "revision_id": "t1:r2", "reason": "not ready yet", "actor": "session-1"}
        m = models.TemplateRejectRequest(**payload)
        assert m.model_dump() == payload

    def test_child_rollback_request_round_trip(self):
        payload = {
            "child_project_id": "c1",
            "target_revision_id": "t1:r1",
            "expected_snapshot_revision": 4,
            "actor": "session-1",
        }
        m = models.ChildTemplateRollbackRequest(**payload)
        assert m.model_dump() == payload

    def test_template_rollback_request_round_trip(self):
        payload = {
            "template_id": "t1",
            "target_revision_id": "t1:r3",
            "changelog": "revert bad r4 edit",
            "actor": "session-1",
        }
        m = models.TemplateRevisionRollbackRequest(**payload)
        assert m.model_dump() == payload

    def test_rollback_requests_are_distinct_types(self):
        """Design doc section (g): rollback targets two different resources
        and is deliberately modeled as two distinct request shapes, not one
        polymorphic body."""
        assert models.ChildTemplateRollbackRequest is not models.TemplateRevisionRollbackRequest
        assert set(models.ChildTemplateRollbackRequest.model_fields) != set(
            models.TemplateRevisionRollbackRequest.model_fields
        )


class TestChildTemplateSnapshot:
    def test_defaults_for_never_adopted_child(self):
        m = models.ChildTemplateSnapshot(child_project_id="c1", template_id="t1")
        assert m.adopted_revision_id is None
        assert m.adopted_at is None
        assert m.snapshot_revision == 0
        assert m.effective_content_hash is None
        assert m.declined_revision_ids == []
        assert m.last_action is None

    @pytest.mark.parametrize("action", ["adopted", "rejected", "rolled_back"])
    def test_valid_last_actions(self, action):
        m = models.ChildTemplateSnapshot(child_project_id="c1", template_id="t1", last_action=action)
        assert m.last_action == action

    def test_invalid_last_action_rejected(self):
        with pytest.raises(ValidationError):
            models.ChildTemplateSnapshot(child_project_id="c1", template_id="t1", last_action="deleted")

    def test_full_round_trip(self):
        payload = {
            "child_project_id": "c1",
            "template_id": "t1",
            "adopted_revision_id": "t1:r2",
            "adopted_at": "2026-08-29T00:00:00Z",
            "snapshot_revision": 2,
            "effective_content_hash": "sha256:" + "6" * 64,
            "declined_revision_ids": ["t1:r1"],
            "last_action": "adopted",
            "updated_at": "2026-08-29T00:00:00Z",
        }
        m = models.ChildTemplateSnapshot(**payload)
        assert m.model_dump() == payload
        restored = models.ChildTemplateSnapshot(**json.loads(m.model_dump_json()))
        assert restored == m


class TestProjectFamilyView:
    def test_empty_members_default(self):
        m = models.ProjectFamilyView(template_id="t1", template_name="web-service-python")
        assert m.members == []
        assert m.latest_revision_id is None

    def test_with_members_round_trip(self):
        member = models.ChildTemplateSnapshot(
            child_project_id="c1", template_id="t1", adopted_revision_id="t1:r1", last_action="adopted",
        )
        m = models.ProjectFamilyView(
            template_id="t1", template_name="web-service-python", latest_revision_id="t1:r3", members=[member],
        )
        restored = models.ProjectFamilyView(**json.loads(m.model_dump_json()))
        assert restored == m
        assert restored.members[0].child_project_id == "c1"

    def test_is_not_a_parent_project_id_replacement(self):
        """Design doc 'Composition with the legacy mechanism': ProjectFamilyView
        carries no parent_project_id-shaped field — it is an orthogonal,
        read-only join, never a replacement for the existing hierarchy field."""
        assert "parent_project_id" not in models.ProjectFamilyView.model_fields


# ---------------------------------------------------------------------------
# Cross-model sanity: none of these new classes were wired onto any existing
# request/response model (purely additive, per this item's scope).
# ---------------------------------------------------------------------------


def test_existing_project_model_unmodified():
    """Guard against accidental scope creep: Project/ProjectCreate must not
    have grown any new template/family field as a side effect of this work."""
    project_fields = set(models.Project.model_fields)
    project_create_fields = set(models.ProjectCreate.model_fields)
    forbidden = {"template_id", "family_id", "adopted_revision_id", "child_template_snapshot"}
    assert not (project_fields & forbidden)
    assert not (project_create_fields & forbidden)


def test_existing_goal_state_model_unmodified():
    """GoalState's north_star_inherited/north_star_source_project_id
    (106519eb) must be untouched by this design-only item."""
    goal_fields = set(models.GoalState.model_fields)
    assert {"north_star_inherited", "north_star_source_project_id", "north_star", "sprint"} <= goal_fields


# ---------------------------------------------------------------------------
# 3. Design doc existence + required section coverage
# ---------------------------------------------------------------------------

_DESIGN_DOC_PATH = (
    Path(__file__).resolve().parent.parent
    / "docs"
    / "meridian-project-family-template-revisions-design.md"
)

# One required marker per acceptance-note sub-item (a)-(k). Matched
# case-sensitively against literal substrings that must appear in the doc —
# a simple content-presence check, per this item's own test-scope note.
_REQUIRED_SECTION_MARKERS: dict[str, str] = {
    "a_stable_revision_id_scheme": "## (a) Stable revision ID scheme",
    "b_content_hash": "## (b) Content hash / canonical serialization",
    "c_effective_configuration_resolution": "## (c) Effective configuration resolution algorithm",
    "d_diff_format": "## (d) Diff format",
    "e_compatibility_versioning": "## (e) Compatibility / versioning rules",
    "f_supersession": "## (f) Supersession semantics",
    "g_rollback": "## (g) Rollback",
    "h_conflict_handling": "## (h) Conflict handling",
    "i_determinism_no_secrets": "## (i) Determinism and auditability",
    "j_schema_sketch_future_work": "## (j) Schema sketch",
    "k_api_shapes": "## (k) API shapes",
}


class TestDesignDocCoverage:
    def test_design_doc_exists(self):
        assert _DESIGN_DOC_PATH.is_file(), f"design doc missing at {_DESIGN_DOC_PATH}"

    @pytest.fixture(scope="class")
    def doc_text(self):
        return _DESIGN_DOC_PATH.read_text(encoding="utf-8")

    @pytest.mark.parametrize("marker_name,marker_text", list(_REQUIRED_SECTION_MARKERS.items()))
    def test_required_section_present(self, doc_text, marker_name, marker_text):
        assert marker_text in doc_text, f"design doc missing required section marker: {marker_text!r}"

    def test_doc_mentions_backward_compat_with_legacy_parent_project_id(self, doc_text):
        assert "parent_project_id" in doc_text
        assert "north_star_inherited" in doc_text

    def test_doc_references_reused_prior_art(self, doc_text):
        """Design doc explicitly follows profile_layers.py's content-hash /
        revision idiom and board_snapshot_revisions' append-only-ledger idiom
        rather than inventing a parallel convention."""
        assert "profile_layers" in doc_text
        assert "board_snapshot_revisions" in doc_text

    def test_doc_states_no_implementation_yet(self, doc_text):
        assert "not implemented" in doc_text.lower() or "design only" in doc_text.lower()

    def test_doc_covers_all_seven_named_operations(self, doc_text):
        for op in ["create", "fork", "override", "preview", "adopt", "reject", "rollback"]:
            assert op in doc_text.lower(), f"design doc missing coverage of operation: {op!r}"
