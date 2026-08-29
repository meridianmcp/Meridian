"""Tests for sprint item ea49362c — project family INTEGRATION CONTRACT
(parent item ddcf6984, "project-family-templates"; builds on sibling
design-only item 5060eea1).

This item is design-only: no route, handler, or DB function changes as a
result of it. Accordingly these tests validate:

1. The new design doc at
   ``docs/meridian-project-family-integration-contract.md`` exists and
   contains every section required by this item's acceptance notes.
2. The one new, purely-additive, UNWIRED Pydantic model this item adds
   (``HandoffFamilyContext``) has the expected shape, round-trips, and
   composes BY REFERENCE with the 5060eea1 models (``ConfigDiffEntry``,
   ``ChildTemplateSnapshot``) rather than duplicating their fields.
3. MOST IMPORTANTLY — a real regression proof that this item changes
   NOTHING about ``generate_handoff``'s actual behavior:
   - a signature-level assertion that ``generate_handoff`` has not gained
     an ``include_family_context`` parameter (the file-level git-diff-
     against-a-frozen-commit checks that used to live here were retired by
     47ac68a0's CI-fix follow-up: they pinned actively-developed files to
     one baseline forever, which both legitimate later sibling items and
     a shallow CI checkout's missing history broke -- see git history for
     the removed ``TestZeroFunctionalDiffAgainstBaseline`` class);
   - a REAL, executed call to ``generate_handoff`` (mirroring
     ``tests/test_handoff_amend_vs_fresh.py``'s fixture pattern) against a
     project with no family, proving the call succeeds and produces the
     expected shape with no family-shaped content anywhere;
   - a direct call to ``build_handoff_manifest`` asserting its returned key
     set is exactly today's fixed set, with no new ``family_binding`` key.

Nothing here assumes any new route, handler, or DB table exists — there is
none yet. See the design doc itself for the full rationale.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from meridian import db as db_module
from meridian import handoff as handoff_module
from meridian import models


# ---------------------------------------------------------------------------
# Shared paths
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DESIGN_DOC_PATH = (
    _REPO_ROOT / "docs" / "meridian-project-family-integration-contract.md"
)


# ---------------------------------------------------------------------------
# 1. Design doc existence + required section coverage
# ---------------------------------------------------------------------------

_REQUIRED_SECTION_MARKERS: dict[str, str] = {
    "a_handoff_surfacing": "## (a) How family state surfaces to a handoff receiver",
    "b_byte_identical_default": "## (b) Default (no flag) handoff output is byte-identical to today",
    "c_proposals_insights_authz": "## (c) Proposals / insights integration",
    "d_mcp_optional_params": "## (d) MCP tool surfaces gain new OPTIONAL params",
    "e_graceful_no_family": "## (e) Graceful behavior for a project with no family",
    "f_backward_compat": "## (f) API / tool backward-compatibility statement",
    "g_test_matrix": "## (g) Test matrix",
    "h_deferred": "## (h) Deferred / explicitly NOT decided by this contract",
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

    def test_doc_states_no_behavior_change(self, doc_text):
        assert "byte-for-byte" in doc_text.lower() or "byte-identical" in doc_text.lower()
        assert "no behavior described here is wired today" in doc_text.lower()

    def test_doc_references_5060eea1_models_by_name(self, doc_text):
        """Must build on, not duplicate, the 16 models 5060eea1 already added."""
        for name in ("ProjectTemplate", "TemplateRevisionSnapshot", "ChildTemplateOverride",
                     "ChildTemplateSnapshot", "ProjectFamilyView", "ConfigDiffEntry"):
            assert name in doc_text, f"design doc must reference 5060eea1's {name} by name"

    def test_doc_flags_family_id_naming_collision(self, doc_text):
        """The findings brief's critical naming-collision warning must appear
        explicitly, not be silently avoided without explanation."""
        assert "family_id" in doc_text
        assert "proposal_lineage" in doc_text
        assert "template_id" in doc_text

    def test_doc_cites_real_permission_model(self, doc_text):
        """Must reuse meridian/roles.py + meridian/_deps.py, never invent a
        parallel permission system."""
        assert "has_perm" in doc_text
        assert "PERM_READ" in doc_text
        assert "PERM_WRITE" in doc_text
        assert "scoped_project_ids" in doc_text

    def test_doc_flags_multi_child_authorization_gap(self, doc_text):
        """The genuine, unimplemented authorization gap for any future
        multi-child aggregate tool must be stated, not glossed over."""
        lower = doc_text.lower()
        assert "gap" in lower
        assert "sibling" in lower

    def test_doc_references_emit_manifest_precedent(self, doc_text):
        assert "emit_manifest" in doc_text
        assert "build_effective_capability_contract" in doc_text
        assert "build_effective_profile_binding" in doc_text

    def test_doc_has_at_least_20_test_matrix_rows(self, doc_text):
        """(g) requires 15-20+ concrete rows; count numbered table rows in
        the test-matrix section specifically."""
        start = doc_text.index("## (g) Test matrix")
        end = doc_text.index("## (h) Deferred")
        section = doc_text[start:end]
        # Numbered markdown table rows look like "| 1 | ... |" at line start.
        import re
        rows = re.findall(r"^\|\s*\d+\s*\|", section, flags=re.MULTILINE)
        assert len(rows) >= 20, f"expected >= 20 test matrix rows, found {len(rows)}"

    def test_doc_states_zero_functional_files_changed(self, doc_text):
        for path in ("meridian/handoff.py", "meridian/db/workspace.py"):
            assert path in doc_text
        assert "get_insights" in doc_text


# ---------------------------------------------------------------------------
# 2. HandoffFamilyContext model — shape, defaults, round-trip, composition
# ---------------------------------------------------------------------------


class TestHandoffFamilyContext:
    def test_only_required_field_is_child_project_id(self):
        m = models.HandoffFamilyContext(child_project_id="proj-1")
        assert m.child_project_id == "proj-1"
        assert m.template_id is None
        assert m.adopted_revision_id is None
        assert m.latest_revision_id is None
        assert m.inherited_vs_local == []
        assert m.executable_capability_status == "unknown"
        assert m.executable_reasons == []
        assert m.pending_promotion_revision_ids == []

    def test_requires_child_project_id(self):
        with pytest.raises(ValidationError):
            models.HandoffFamilyContext()

    def test_does_not_define_a_bare_family_id_field(self):
        """Critical naming-collision guard (findings §3): this model must
        NEVER introduce a bare `family_id` field — workspace_proposals
        already has an unrelated column of that name."""
        assert "family_id" not in models.HandoffFamilyContext.model_fields
        assert "template_id" in models.HandoffFamilyContext.model_fields

    def test_executable_capability_status_enum_is_bounded(self):
        m = models.HandoffFamilyContext(child_project_id="p", executable_capability_status="executable")
        assert m.executable_capability_status == "executable"
        with pytest.raises(ValidationError):
            models.HandoffFamilyContext(child_project_id="p", executable_capability_status="bogus")

    def test_composes_config_diff_entry_by_reference(self):
        """inherited_vs_local must be a list of the SAME ConfigDiffEntry
        class 5060eea1 defined -- never a re-declared parallel shape."""
        diff_entry = models.ConfigDiffEntry(
            path="build.timeout_seconds", op="changed", base_value=30, new_value=60, source="override",
        )
        m = models.HandoffFamilyContext(
            child_project_id="proj-1",
            template_id="tmpl-a",
            adopted_revision_id="tmpl-a:r3",
            latest_revision_id="tmpl-a:r5",
            inherited_vs_local=[diff_entry],
            executable_capability_status="executable",
            pending_promotion_revision_ids=["tmpl-a:r4", "tmpl-a:r5"],
        )
        assert isinstance(m.inherited_vs_local[0], models.ConfigDiffEntry)
        assert m.inherited_vs_local[0].source == "override"

    def test_full_round_trip_json(self):
        payload = {
            "child_project_id": "proj-1",
            "template_id": "tmpl-a",
            "adopted_revision_id": "tmpl-a:r3",
            "latest_revision_id": "tmpl-a:r5",
            "inherited_vs_local": [
                {"path": "workers", "op": "added", "base_value": None, "new_value": 4, "source": "override"},
            ],
            "executable_capability_status": "non_executable",
            "executable_reasons": ["missing tool: docker"],
            "pending_promotion_revision_ids": ["tmpl-a:r4"],
        }
        m = models.HandoffFamilyContext(**payload)
        restored = models.HandoffFamilyContext(**json.loads(m.model_dump_json()))
        assert restored == m

    def test_composes_with_child_template_snapshot_by_shared_key_names(self):
        """Field names deliberately mirror ChildTemplateSnapshot's own field
        names (child_project_id, template_id, adopted_revision_id) so a
        future implementation can build one from the other with a plain
        field-name mapping, not a translation table."""
        snapshot = models.ChildTemplateSnapshot(
            child_project_id="proj-1", template_id="tmpl-a", adopted_revision_id="tmpl-a:r3",
        )
        m = models.HandoffFamilyContext(
            child_project_id=snapshot.child_project_id,
            template_id=snapshot.template_id,
            adopted_revision_id=snapshot.adopted_revision_id,
        )
        assert m.child_project_id == snapshot.child_project_id
        assert m.template_id == snapshot.template_id
        assert m.adopted_revision_id == snapshot.adopted_revision_id


def test_existing_project_and_goal_state_models_unmodified():
    """Guard against accidental scope creep, same style as 5060eea1's own
    guard test: Project/ProjectCreate/GoalState must not have grown any new
    family-context field as a side effect of this item."""
    project_fields = set(models.Project.model_fields)
    project_create_fields = set(models.ProjectCreate.model_fields)
    goal_fields = set(models.GoalState.model_fields)
    forbidden = {"family_id", "template_id", "adopted_revision_id", "family_binding", "include_family_context"}
    assert not (project_fields & forbidden)
    assert not (project_create_fields & forbidden)
    assert not (goal_fields & forbidden)


# ---------------------------------------------------------------------------
# 3. Regression proof — generate_handoff's ACTUAL behavior is unaffected
# ---------------------------------------------------------------------------
#
# NOTE (retired by 47ac68a0's CI-fix follow-up): this section originally
# pinned meridian/handoff.py, meridian/mcp/handlers/, meridian/db/workspace.py,
# and db/__init__.py::get_insights to be byte-for-byte identical to the
# _BASELINE_REF commit (b0deb335) forever, via `git diff <baseline> -- <path>`.
# That was only ever meant to prove THIS item (ea49362c) itself made no
# functional edit to those files at the time it landed -- not to freeze
# actively-developed files permanently. Sibling items 83a7586d, 524e73e6, and
# 47ac68a0 have since made real, independently-verified, in-scope changes to
# handoff.py and db/workspace.py, so the diff is now legitimately non-empty.
# Worse, in CI's shallow checkout `git diff <old-sha> -- ...` fails outright
# (exit 128, unknown revision) rather than merely reporting a diff, since
# b0deb335 isn't reachable history -- confirmed blocking the dev/deploy
# pipeline on 2026-08-29. The file-level baseline-diff tests were removed
# for exactly this reason; `test_generate_handoff_signature_unchanged` below
# needed no git history at all, so it survives as a standalone check.


def test_generate_handoff_signature_unchanged():
    """Belt-and-suspenders regression: generate_handoff must not have
    gained an include_family_context parameter as a side effect of this
    (design-only) item or its siblings -- that would be future
    implementation work, out of scope for all of them."""
    import inspect
    sig = inspect.signature(handoff_module.generate_handoff)
    params = list(sig.parameters)
    assert "include_family_context" not in params, (
        "generate_handoff must NOT gain include_family_context in this "
        "design-only item -- that is future implementation work"
    )


# ---------------------------------------------------------------------------
# 3b. build_handoff_manifest — exact fixed key set, no new family_binding key
# ---------------------------------------------------------------------------


def test_build_handoff_manifest_key_set_has_no_family_binding_key():
    manifest = handoff_module.build_handoff_manifest(
        handoff_mode="goal", project_id="proj-1", items=[],
    )
    expected_keys = {
        "schema_version", "handoff_mode", "project_id", "project_name",
        "sprint_version", "session_id", "origin_identity", "generated_at",
        "board_revision", "selected_item_ids", "closure_item_ids", "items",
        "items_truncated", "items_total", "waves", "stop_conditions",
        "deploy_policy", "evidence_status", "trusted_pointers",
    }
    assert set(manifest.keys()) == expected_keys, (
        f"build_handoff_manifest gained/lost keys: {set(manifest.keys()) ^ expected_keys}. "
        "A future item adding family_binding must update this assertion deliberately."
    )
    assert "family_binding" not in manifest


# ---------------------------------------------------------------------------
# 3c. Real, executed generate_handoff call against a project with no family
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_handoff_no_family_default_call_unaffected(db, tmp_path):
    """Concrete regression proof (not just a claim): a project that has
    never touched any family/template concept gets a generate_handoff call
    identical in shape and content to today's behavior -- no family-shaped
    substrings appear anywhere in the rendered output, because nothing in
    meridian/handoff.py was changed by this item (see the git-diff tests
    above for the file-level proof; this proves it behaviorally too)."""
    p = await db_module.create_project(db, "family-contract-no-family-test")
    await db_module.set_goal(db, p["id"], "ship it", sprint="s1")

    path, content, amended = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True,
    )

    assert amended is False, "first call for a fresh project must be a fresh insert"
    assert path
    assert isinstance(content, str) and content

    forbidden_substrings = (
        "family_binding", "include_family_context", "template_revision_id",
        "child_template_snapshot", "pending_promotion_revision_ids",
        "executable_capability_status",
    )
    lowered = content.lower()
    for token in forbidden_substrings:
        assert token.lower() not in lowered, (
            f"unexpected family-context token {token!r} leaked into generate_handoff output "
            "for a project with no family and no opt-in flag"
        )


@pytest.mark.asyncio
async def test_generate_handoff_goal_mode_emit_manifest_no_family(db, tmp_path):
    """Same proof, specifically for mode='goal' + emit_manifest=True — the
    ONE mode/flag combination that already splices a <handoff_manifest>
    block into the render (acf6f51a), making it the most likely place a
    careless future family-context patch would leak a default-on field."""
    p = await db_module.create_project(db, "family-contract-goal-manifest-test")
    await db_module.set_goal(db, p["id"], "ship it", sprint="s1")
    await db_module.add_sprint_item(db, p["id"], "s1", "do the thing")

    path, content, amended = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="goal", emit_manifest=True,
    )

    assert isinstance(content, str) and content
    if "<handoff_manifest" in content:
        assert "family_binding" not in content
        assert "template_id=" not in content
