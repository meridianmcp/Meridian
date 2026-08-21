"""24f5146d — end-to-end integrity of the deterministic, idempotent,
provenance-closed script->artifact->document PROMOTION pipeline.

New test file (distinct from tests/test_handoff_docx_integrity_gate.py,
which covers the PRE-EXISTING render/equation-audit gate this item does not
touch). This file exercises the pieces THIS sprint item actually wires
together, end to end where practical, rather than re-testing any one piece
in isolation:

  1. The full promotion lifecycle: create_wave_run(promotion_targets=...)
     pins a base-hash precondition -> the ONE canonical merger lock
     (meridian.artifact_declaration.acquire/release_promotion_merger_lock,
     reusing meridian.db.locks.claim_file/release_file) serializes access ->
     a REAL tools.meridian_fallbacks.patch_manifest.PatchManifest is applied
     via tools.meridian_fallbacks.transactional_merge.apply_patch_manifest
     (reused verbatim, never reimplemented) -> finalize_wave_run requires
     and records that real MergeResult as promotion_evidence before the wave
     may be marked merged.
  2. finalize_wave_run's promotion-evidence contract is opt-in and fail
     closed: a run with no pinned promotion targets is completely
     unaffected; a run WITH pinned targets refuses to finalize without real,
     successful evidence for every one of them.
  3. meridian.dispatcher.Dispatcher's merger-lock awareness: an item
     declaring a promotion target already locked by another live session is
     skipped (not dispatched, dispatcher not stopped); two items declaring
     the SAME target in one pass dispatch at most one of them.
  4. meridian.outputs_indexer.check_promotion_source_freshness — the
     "script-run -> artifact" half of provenance closure.
  5. meridian.handoff.build_promotion_readiness_for_handoff — the new
     best-effort handoff enrichment surfacing promotion base-hash readiness.

Native whole-document DOM reserialization is never used anywhere in this
file or the code under test — every docx byte this file writes goes through
plain ``zipfile`` part replacement (mirrors
tools/meridian_fallbacks/tests/conftest.py's own minimal-docx builder,
duplicated here in miniature rather than cross-importing another test
package's fixtures).
"""
from __future__ import annotations

import io
import zipfile

import pytest

from meridian import artifact_declaration as ad
from meridian import db as db_module
from meridian import docx_integrity_gate as gate_module
from meridian import handoff as handoff_module
from meridian import outputs_indexer as outputs_indexer_module
from meridian import tool_requirements as tool_requirements_module
from meridian.db import wave_runs as wave_runs_module
from meridian.dispatcher import Dispatcher

from tools.meridian_fallbacks.patch_manifest import PatchManifest
from tools.meridian_fallbacks.transactional_merge import apply_patch_manifest


_GOOD_EVIDENCE = {"status": "ok", "exit_code": 0, "passed": 1, "failed": 0}


# ---------------------------------------------------------------------------
# Minimal, genuinely valid synthetic .docx bytes (ZIP-level only).
# ---------------------------------------------------------------------------

_CONTENT_TYPES_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    "</Types>"
).encode("utf-8")

_PACKAGE_RELS_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
    'Target="word/document.xml"/>'
    "</Relationships>"
).encode("utf-8")


def _document_xml(text: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p><w:sectPr/></w:body>"
        "</w:document>"
    ).encode("utf-8")


def _minimal_docx_bytes(text: str = "Original") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES_XML)
        zf.writestr("_rels/.rels", _PACKAGE_RELS_XML)
        zf.writestr("word/document.xml", _document_xml(text))
    return buf.getvalue()


async def _project(db, name: str) -> str:
    proj = await db_module.create_project(db, name)
    return proj["id"]


# ---------------------------------------------------------------------------
# 1 + 2. Full promotion lifecycle + finalize_wave_run's evidence contract.
# ---------------------------------------------------------------------------

async def test_promotion_pipeline_hash_lock_apply_finalize_end_to_end(db, tmp_path):
    target = tmp_path / "thesis.docx"
    target.write_bytes(_minimal_docx_bytes("Original"))

    pid = await _project(db, "promo-e2e")
    session = await db_module.register_session(db, pid, "promo-worker")

    # -- plan: pin the base-hash precondition at wave creation.
    run = await db_module.create_wave_run(db, pid, promotion_targets=[str(target)])
    await db_module.advance_wave_run_status(db, run["id"], "running")
    pinned = await wave_runs_module.get_pinned_promotion_targets(db, run["id"])
    assert len(pinned) == 1
    assert pinned[0]["base_sha256"] == ad.compute_base_sha256(target)

    # -- preconditions still hold (nothing has touched the file yet).
    item = {
        "planned_output": ad.serialize_planned_output({
            "source_type": "code",
            "targets": [{
                "uri": "thesis.docx",
                "selector": {"type": "range", "start_line": 1, "end_line": 1},
            }],
            "promotion": {"base_sha256": pinned[0]["base_sha256"]},
        })
    }
    pre = ad.check_promotion_preconditions(item, target)
    assert pre["ok"] is True

    # -- acquire the ONE canonical merger lock before promoting.
    lock = await ad.acquire_promotion_merger_lock(db, str(target), session["id"])
    assert lock["claimed"] is True
    # A concurrent promotion attempt against the SAME target is refused.
    other_session = await db_module.register_session(db, pid, "promo-worker-2")
    blocked = await ad.acquire_promotion_merger_lock(db, str(target), other_session["id"])
    assert blocked["claimed"] is False

    # -- apply a REAL patch manifest (tools/meridian_fallbacks, reused verbatim).
    manifest = PatchManifest.create_from_file(target)
    assert manifest.base_sha256 == pinned[0]["base_sha256"]
    new_bytes = _document_xml("Promoted content from the script run")
    op = manifest.add_operation(
        "replace_part", "word/document.xml", "promote script output into body",
        payload=new_bytes,
    )
    result = apply_patch_manifest(manifest, payloads={op.op_id: new_bytes})
    assert result.success is True
    assert result.final_sha256 is not None
    assert manifest.status == "applied"

    # -- release the lock now that the apply is done.
    released = await ad.release_promotion_merger_lock(db, str(target), session["id"])
    assert released is True
    now_free = await ad.get_promotion_merger_lock(db, str(target), pid)
    assert now_free["file_lock"] is None

    # -- finalize with the REAL MergeResult as promotion_evidence.
    finalized = await db_module.finalize_wave_run(
        db, run["id"],
        evidence=_GOOD_EVIDENCE,
        promotion_evidence={str(target): result.to_dict()},
    )
    assert finalized["finalized"] is True
    assert finalized["already_finalized"] is False
    assert finalized["promotion_evidence"][str(target)]["success"] is True

    # -- idempotent retry: identical outcome, no new event.
    events_before = len(await db_module.get_wave_run_events(db, run["id"]))
    replay = await db_module.finalize_wave_run(db, run["id"])
    assert replay["already_finalized"] is True
    events_after = len(await db_module.get_wave_run_events(db, run["id"]))
    assert events_after == events_before

    # -- the target file on disk really did change (the promotion landed).
    assert target.read_bytes() != _minimal_docx_bytes("Original")


async def test_finalize_wave_run_requires_promotion_evidence_when_pinned(db, tmp_path):
    target = tmp_path / "thesis.docx"
    target.write_bytes(_minimal_docx_bytes())
    pid = await _project(db, "promo-missing-evidence")
    run = await db_module.create_wave_run(db, pid, promotion_targets=[str(target)])

    with pytest.raises(ValueError, match="promotion_evidence"):
        await db_module.finalize_wave_run(db, run["id"], evidence=_GOOD_EVIDENCE)


async def test_finalize_wave_run_rejects_failed_promotion_evidence(db, tmp_path):
    target = tmp_path / "thesis.docx"
    target.write_bytes(_minimal_docx_bytes())
    pid = await _project(db, "promo-failed-evidence")
    run = await db_module.create_wave_run(db, pid, promotion_targets=[str(target)])

    bad_evidence = {str(target): {"success": False, "error": "validation failed", "final_sha256": None}}
    with pytest.raises(ValueError, match="unsuccessful"):
        await db_module.finalize_wave_run(
            db, run["id"], evidence=_GOOD_EVIDENCE, promotion_evidence=bad_evidence,
        )


async def test_finalize_wave_run_rejects_dry_run_promotion_evidence(db, tmp_path):
    """A dry_run MergeResult never wrote anything to disk — it must not be
    accepted as proof the promotion actually landed."""
    target = tmp_path / "thesis.docx"
    target.write_bytes(_minimal_docx_bytes())
    pid = await _project(db, "promo-dryrun-evidence")
    run = await db_module.create_wave_run(db, pid, promotion_targets=[str(target)])

    manifest = PatchManifest.create_from_file(target)
    payload = _document_xml("dry run only")
    op = manifest.add_operation("replace_part", "word/document.xml", "preview", payload=payload)
    dry_result = apply_patch_manifest(manifest, payloads={op.op_id: payload}, dry_run=True)
    assert dry_result.success is True
    assert dry_result.dry_run is True

    with pytest.raises(ValueError, match="dry_run"):
        await db_module.finalize_wave_run(
            db, run["id"], evidence=_GOOD_EVIDENCE,
            promotion_evidence={str(target): dry_result.to_dict()},
        )


async def test_finalize_wave_run_unaffected_when_no_promotion_targets_pinned(db):
    """Backward compatibility: zero promotion involvement -> zero new
    requirement, exactly the pre-24f5146d contract."""
    pid = await _project(db, "promo-not-involved")
    run = await db_module.create_wave_run(db, pid)
    await db_module.advance_wave_run_status(db, run["id"], "running")
    result = await db_module.finalize_wave_run(db, run["id"], evidence=_GOOD_EVIDENCE)
    assert result["finalized"] is True
    assert result["pinned_promotion_targets"] == []


async def test_transactional_merge_staleness_gate_is_the_real_precondition_enforcer(db, tmp_path):
    """The manifest's OWN base_sha256 staleness check (transactional_merge,
    reused verbatim) is what actually protects the apply -- a target that
    changed after the manifest was authored refuses to apply without
    allow_stale_base, independent of anything meridian.db does."""
    from tools.meridian_fallbacks.transactional_merge import MergeConflictError

    target = tmp_path / "thesis.docx"
    target.write_bytes(_minimal_docx_bytes("v1"))
    manifest = PatchManifest.create_from_file(target)

    # The file changes AFTER the manifest was authored.
    target.write_bytes(_minimal_docx_bytes("v2 -- changed out from under the manifest"))

    payload = _document_xml("v3")
    op = manifest.add_operation("replace_part", "word/document.xml", "promote", payload=payload)
    with pytest.raises(MergeConflictError):
        apply_patch_manifest(manifest, payloads={op.op_id: payload})


# ---------------------------------------------------------------------------
# 3. Dispatcher merger-lock awareness.
# ---------------------------------------------------------------------------

class _FakeEnqueue:
    def __init__(self):
        self.calls: list[dict] = []

    async def __call__(self, db, session_id, project_id, prompt, **kwargs):
        self.calls.append({"session_id": session_id, "prompt": prompt})
        return {"id": f"task-{len(self.calls)}", "status": "pending"}


def _promotion_planned_output(target_uri: str) -> dict:
    return {
        "source_type": "code",
        "targets": [{"uri": target_uri, "selector": {"type": "range", "start_line": 1, "end_line": 1}}],
        "promotion": {"merger_lock_key": target_uri},
    }


async def test_dispatcher_skips_item_whose_merger_lock_is_held_by_another_session(db):
    pid = await _project(db, "dispatch-lock-held")
    other_session = await db_module.register_session(db, pid, "external-promoter")
    target_uri = "outputs/thesis.docx"
    await ad.acquire_promotion_merger_lock(db, target_uri, other_session["id"])

    await db_module.add_sprint_item(
        db, pid, "v1", "Promote figure into thesis",
        touches_resources=["file:fileA"],
        planned_output=_promotion_planned_output(target_uri),
        force=True,
    )
    await db_module.add_sprint_item(
        db, pid, "v1", "Handle unrelated maintenance chore",
        touches_resources=["file:fileB"], force=True,
    )

    fake = _FakeEnqueue()
    disp = Dispatcher(db, pid, enqueue_fn=fake)
    enqueued = await disp.dispatch_once()

    # Only the unrelated item dispatches; the locked-target item is skipped.
    assert len(enqueued) == 1
    assert len(disp.last_merger_lock_skips) == 1
    assert disp.last_merger_lock_skips[0]["target"] == target_uri
    assert "held" in disp.last_merger_lock_skips[0]["reason"]


async def test_dispatcher_dedupes_same_pass_items_sharing_a_target(db):
    pid = await _project(db, "dispatch-lock-same-pass")
    target_uri = "outputs/thesis.docx"

    await db_module.add_sprint_item(
        db, pid, "v1", "Promote ablation chart",
        touches_resources=["file:fileA"],
        planned_output=_promotion_planned_output(target_uri),
        force=True,
    )
    await db_module.add_sprint_item(
        db, pid, "v1", "Insert results summary block",
        touches_resources=["file:fileB"],
        planned_output=_promotion_planned_output(target_uri),
        force=True,
    )

    fake = _FakeEnqueue()
    disp = Dispatcher(db, pid, enqueue_fn=fake)
    enqueued = await disp.dispatch_once()

    # Both items are resource-disjoint (fileA vs fileB) so they'd normally
    # land in the same parallel group -- but sharing a merger-lock target
    # means only ONE may dispatch this pass.
    assert len(enqueued) == 1
    assert len(disp.last_merger_lock_skips) == 1


async def test_dispatcher_unaffected_by_items_without_promotion_declarations(db):
    """The overwhelming majority case: no artifact declaration at all ->
    merger-lock awareness never engages, dispatch behaves exactly as
    before this feature existed."""
    pid = await _project(db, "dispatch-no-promotion")
    await db_module.add_sprint_item(
        db, pid, "v1", "Refactor the config loader", touches_resources=["file:fileA"], force=True,
    )
    await db_module.add_sprint_item(
        db, pid, "v1", "Add retry logic to the uploader", touches_resources=["file:fileB"], force=True,
    )

    fake = _FakeEnqueue()
    disp = Dispatcher(db, pid, enqueue_fn=fake)
    enqueued = await disp.dispatch_once()

    assert len(enqueued) == 2
    assert disp.last_merger_lock_skips == []


# ---------------------------------------------------------------------------
# 4. outputs_indexer.check_promotion_source_freshness — source-side
#    provenance closure.
# ---------------------------------------------------------------------------

def test_check_promotion_source_freshness_unresolved_when_not_indexed(tmp_path):
    outputs_dir = tmp_path / "outputs"
    outputs_dir.mkdir()
    result = outputs_indexer_module.check_promotion_source_freshness(
        str(outputs_dir), str(tmp_path / "never_indexed.png"), expected_sha256="a" * 64,
    )
    assert result["resolved"] is False
    assert result["fresh"] is None


def test_check_promotion_source_freshness_basename_fallback_is_not_authoritative(tmp_path):
    """A relocated source may be a useful hint, but cannot authorize promotion."""
    outputs_dir = tmp_path / "outputs"
    outputs_dir.mkdir()
    actual = outputs_dir / "run" / "figure.png"
    actual.parent.mkdir()
    actual.write_bytes(b"figure bytes")
    relocated = tmp_path / "docs_media" / "figure.png"

    result = outputs_indexer_module.check_promotion_source_freshness(
        str(outputs_dir), str(relocated), expected_sha256="a" * 64,
    )
    assert result["resolved"] is False
    assert result["match_type"] == "basename"
    assert result["fresh"] is None
    assert "basename" in result["reason"]


def test_check_promotion_source_freshness_no_expected_hash_is_unconfirmed(tmp_path):
    """Resolving with no expected_sha256 supplied is 'cannot confirm', never
    silently treated as fresh."""
    outputs_dir = tmp_path / "outputs"
    outputs_dir.mkdir()
    src = outputs_dir / "figure.png"
    src.write_bytes(b"figure bytes")

    result = outputs_indexer_module.check_promotion_source_freshness(str(outputs_dir), str(src))
    assert result["resolved"] is True
    assert result["fresh"] is None
    assert result["current_sha256"] is not None


def test_check_promotion_source_freshness_fresh_when_hash_matches(tmp_path):
    outputs_dir = tmp_path / "outputs"
    outputs_dir.mkdir()
    src = outputs_dir / "figure.png"
    src.write_bytes(b"figure bytes")
    expected = outputs_indexer_module._sha256_file(str(src))

    result = outputs_indexer_module.check_promotion_source_freshness(
        str(outputs_dir), str(src), expected_sha256=expected,
    )
    assert result["resolved"] is True
    assert result["fresh"] is True
    assert result["current_sha256"] == expected


def test_check_promotion_source_freshness_stale_when_content_no_longer_matches_expected(tmp_path):
    outputs_dir = tmp_path / "outputs"
    outputs_dir.mkdir()
    src = outputs_dir / "figure.png"
    src.write_bytes(b"original figure bytes")
    expected = outputs_indexer_module._sha256_file(str(src))

    # Content changes AFTER the expected hash was captured (e.g. the script
    # was re-run and regenerated the figure).
    src.write_bytes(b"regenerated figure bytes -- different content")

    result = outputs_indexer_module.check_promotion_source_freshness(
        str(outputs_dir), str(src), expected_sha256=expected,
    )
    assert result["resolved"] is True
    assert result["fresh"] is False
    assert result["current_sha256"] != expected


# ---------------------------------------------------------------------------
# 5. handoff.build_promotion_readiness_for_handoff.
# ---------------------------------------------------------------------------

async def test_build_promotion_readiness_for_handoff_reports_ok_and_stale(tmp_path):
    fresh_target = tmp_path / "fresh.docx"
    fresh_target.write_bytes(b"fresh content")
    stale_target = tmp_path / "stale.docx"
    stale_target.write_bytes(b"stale content")

    fresh_item = {
        "id": "item-fresh",
        "planned_output": ad.serialize_planned_output({
            "source_type": "code",
            "targets": [{"uri": "fresh.docx", "selector": {"type": "range", "start_line": 1, "end_line": 1}}],
            "promotion": {"base_sha256": ad.compute_base_sha256(fresh_target)},
        }),
    }
    stale_item = {
        "id": "item-stale",
        "planned_output": ad.serialize_planned_output({
            "source_type": "code",
            "targets": [{"uri": "stale.docx", "selector": {"type": "range", "start_line": 1, "end_line": 1}}],
            "promotion": {"base_sha256": "f" * 64},  # deliberately wrong hash
        }),
    }
    no_promotion_item = {
        "id": "item-no-promotion",
        "planned_output": ad.serialize_planned_output({
            "source_type": "code",
            "targets": [{"uri": "other.png", "selector": {"type": "range", "start_line": 1, "end_line": 1}}],
        }),
    }

    result = await handoff_module.build_promotion_readiness_for_handoff(
        db=None, project_id="unused", pending_items=[fresh_item, stale_item, no_promotion_item],
        output_dir=str(tmp_path),
    )
    assert result["unresolved_count"] == 1
    by_item = {c["item_id"]: c for c in result["checked"]}
    assert by_item["item-fresh"]["ok"] is True
    assert by_item["item-stale"]["ok"] is False
    assert "item-no-promotion" not in by_item


async def test_build_promotion_readiness_for_handoff_empty_for_no_items():
    result = await handoff_module.build_promotion_readiness_for_handoff(
        db=None, project_id="unused", pending_items=None, output_dir="/tmp",
    )
    assert result == {"checked": [], "unresolved_count": 0}


async def test_generate_handoff_promotion_readiness_out_param_is_purely_additive(db, tmp_path):
    """A caller that never passes promotion_readiness sees zero behavior
    change -- and a caller that DOES pass it gets it populated for the
    full/delta modes without altering the returned (path, content, amended)."""
    pid = await _project(db, "handoff-promo-readiness")
    await db_module.add_sprint_item(db, pid, "v1", "An ordinary item")

    out_dir = str(tmp_path / "handoff_out")
    readiness: dict = {}
    path, content, amended = await handoff_module.generate_handoff(
        db, pid, out_dir, mode="delta", skip_ai_summary=True,
        promotion_readiness=readiness,
    )
    assert path  # rendered successfully
    assert isinstance(readiness, dict)
    assert "checked" in readiness
    assert readiness["checked"] == []  # no item declared a promotion base hash

    # And the None-default path (every pre-existing caller) is unaffected.
    path2, content2, amended2 = await handoff_module.generate_handoff(
        db, pid, out_dir, mode="delta", skip_ai_summary=True,
    )
    assert path2


# ---------------------------------------------------------------------------
# 6. f6912e2d — artifact_recipe schema (execution_path / rollback_policy /
#    checks / focused_tests), the "no ambiguous filename/label match"
#    promotion guard, and the composed recipe-completeness verdict.
# ---------------------------------------------------------------------------

def _valid_recipe(**overrides):
    base = {
        "execution_path": "local",
        "rollback_policy": "transactional_atomic",
        "checks": {
            "structural_check_required": True,
            "word_com_render_check_required": True,
        },
        "focused_tests": [
            "tests/test_docx_integrity_gate.py::test_promotion_pipeline_hash_lock_apply_finalize_end_to_end",
        ],
    }
    base.update(overrides)
    return base


def test_normalize_artifact_recipe_none_passes_through():
    assert ad.normalize_artifact_recipe(None) is None


def test_normalize_artifact_recipe_accepts_valid_declaration():
    normalized = ad.normalize_artifact_recipe(_valid_recipe())
    assert normalized["execution_path"] == "local"
    assert normalized["rollback_policy"] == "transactional_atomic"
    assert normalized["checks"] == {
        "structural_check_required": True,
        "word_com_render_check_required": True,
        "outputs_provenance_check_required": False,
    }
    assert normalized["focused_tests"] == [
        "tests/test_docx_integrity_gate.py::test_promotion_pipeline_hash_lock_apply_finalize_end_to_end",
    ]


def test_normalize_artifact_recipe_defaults_checks_when_omitted():
    normalized = ad.normalize_artifact_recipe(
        {"execution_path": "hosted", "rollback_policy": "none", "focused_tests": ["tests/x.py::y"]}
    )
    assert normalized["checks"] == {
        "structural_check_required": False,
        "word_com_render_check_required": False,
        "outputs_provenance_check_required": False,
    }


def test_normalize_artifact_recipe_rejects_missing_required_fields():
    with pytest.raises(ad.ArtifactDeclarationError, match="execution_path"):
        ad.normalize_artifact_recipe({"rollback_policy": "none", "focused_tests": ["tests/x.py::y"]})
    with pytest.raises(ad.ArtifactDeclarationError, match="rollback_policy"):
        ad.normalize_artifact_recipe({"execution_path": "local", "focused_tests": ["tests/x.py::y"]})
    with pytest.raises(ad.ArtifactDeclarationError, match="focused_tests"):
        ad.normalize_artifact_recipe({"execution_path": "local", "rollback_policy": "none"})


def test_normalize_artifact_recipe_rejects_unknown_top_level_field():
    with pytest.raises(ad.ArtifactDeclarationError, match="unknown"):
        ad.normalize_artifact_recipe({**_valid_recipe(), "bogus_field": True})


def test_normalize_artifact_recipe_rejects_unknown_checks_field():
    with pytest.raises(ad.ArtifactDeclarationError, match="checks"):
        ad.normalize_artifact_recipe(
            {**_valid_recipe(), "checks": {"not_a_real_check": True}}
        )


def test_normalize_artifact_recipe_rejects_invalid_execution_path():
    with pytest.raises(ad.ArtifactDeclarationError, match="execution_path"):
        ad.normalize_artifact_recipe({**_valid_recipe(), "execution_path": "remote"})


def test_normalize_artifact_recipe_rejects_invalid_rollback_policy():
    with pytest.raises(ad.ArtifactDeclarationError, match="rollback_policy"):
        ad.normalize_artifact_recipe({**_valid_recipe(), "rollback_policy": "hope_for_the_best"})


def test_normalize_artifact_recipe_rejects_empty_focused_tests():
    with pytest.raises(ad.ArtifactDeclarationError, match="focused_tests"):
        ad.normalize_artifact_recipe({**_valid_recipe(), "focused_tests": []})


def test_normalize_artifact_recipe_rejects_non_string_focused_test_entry():
    with pytest.raises(ad.ArtifactDeclarationError, match="focused_tests"):
        ad.normalize_artifact_recipe({**_valid_recipe(), "focused_tests": [123]})


def test_normalize_artifact_recipe_dedupes_focused_tests_preserving_order():
    normalized = ad.normalize_artifact_recipe(
        {**_valid_recipe(), "focused_tests": ["tests/x.py::y", "tests/z.py::w", "tests/x.py::y"]}
    )
    assert normalized["focused_tests"] == ["tests/x.py::y", "tests/z.py::w"]


def test_normalize_artifact_recipe_caps_focused_tests():
    too_many = [f"tests/x.py::t{i}" for i in range(ad._MAX_FOCUSED_TESTS + 1)]
    with pytest.raises(ad.ArtifactDeclarationError, match="exceeding the cap"):
        ad.normalize_artifact_recipe({**_valid_recipe(), "focused_tests": too_many})


def test_normalize_artifact_recipe_rejects_local_absolute_path_in_focused_tests():
    """Reuses (never reimplements) capability_manifest's secret/local-path
    screen -- an absolute machine-local path has no business inside a
    project-shared recipe."""
    with pytest.raises(ad.ArtifactDeclarationError):
        ad.normalize_artifact_recipe(
            {**_valid_recipe(), "focused_tests": [r"C:\Users\someone\tests\test_x.py::y"]}
        )


def test_serialize_and_parse_artifact_recipe_round_trip():
    serialized = ad.serialize_artifact_recipe(_valid_recipe())
    assert isinstance(serialized, str)
    parsed = ad.parse_artifact_recipe(serialized)
    assert parsed == ad.normalize_artifact_recipe(_valid_recipe())


def test_serialize_artifact_recipe_none_for_no_declaration():
    assert ad.serialize_artifact_recipe(None) is None


def test_parse_artifact_recipe_degrades_on_malformed_json():
    assert ad.parse_artifact_recipe("{not valid json") is None
    assert ad.parse_artifact_recipe(None) is None


def test_effective_artifact_recipe_reads_declared_and_absent():
    item = {"artifact_recipe": ad.serialize_artifact_recipe(_valid_recipe())}
    assert ad.effective_artifact_recipe(item)["execution_path"] == "local"
    assert ad.effective_artifact_recipe({}) is None


def test_has_artifact_declaration_unaffected_by_artifact_recipe_alone():
    """f6912e2d's artifact_recipe is deliberately NOT folded into
    has_artifact_declaration -- an item declaring ONLY a recipe (no kind/
    planned_output/policy) must not flip this predicate, since
    <artifact_declaration> rendering does not surface artifact_recipe."""
    item = {"artifact_recipe": ad.serialize_artifact_recipe(_valid_recipe())}
    assert ad.has_artifact_declaration(item) is False


# --- check_no_ambiguous_promotion_match -------------------------------------

def test_no_ambiguous_match_ok_when_no_promotion_declared():
    result = ad.check_no_ambiguous_promotion_match({})
    assert result["ok"] is True
    assert result["resource_footprint_count"] == 0


def test_no_ambiguous_match_ok_when_no_planned_output_at_all():
    item = {"planned_output": None}
    result = ad.check_no_ambiguous_promotion_match(item)
    assert result["ok"] is True


def test_no_ambiguous_match_rejects_promotion_with_empty_resource_footprint():
    item = {
        "planned_output": ad.serialize_planned_output({
            "source_type": "code",
            "targets": [{"uri": "thesis.docx", "selector": {"type": "range", "start_line": 1, "end_line": 1}}],
            "promotion": {"base_sha256": "a" * 64},
        })
    }
    result = ad.check_no_ambiguous_promotion_match(item)
    assert result["ok"] is False
    assert result["resource_footprint_count"] == 0
    assert "ambiguous" in result["reason"]


def test_no_ambiguous_match_accepts_promotion_with_a_typed_anchor():
    item = {
        "planned_output": ad.serialize_planned_output({
            "source_type": "code",
            "targets": [{"uri": "thesis.docx", "selector": {"type": "range", "start_line": 1, "end_line": 1}}],
            "promotion": {
                "base_sha256": "a" * 64,
                "resource_footprint": ["table:results-summary"],
            },
        })
    }
    result = ad.check_no_ambiguous_promotion_match(item)
    assert result["ok"] is True
    assert result["resource_footprint_count"] == 1


# --- check_promotion_preconditions's new opt-in require_resource_footprint --

def test_check_promotion_preconditions_default_ignores_ambiguous_match(tmp_path):
    """Backward compatibility: the pre-existing default (False) never
    changes behavior for a promotion with no resource_footprint -- this is
    the EXACT scenario test_promotion_pipeline_hash_lock_apply_finalize_end_to_end
    (above, unmodified) already relies on."""
    target = tmp_path / "thesis.docx"
    target.write_bytes(b"content")
    base = ad.compute_base_sha256(target)
    item = {
        "planned_output": ad.serialize_planned_output({
            "source_type": "code",
            "targets": [{"uri": "thesis.docx", "selector": {"type": "range", "start_line": 1, "end_line": 1}}],
            "promotion": {"base_sha256": base},
        })
    }
    result = ad.check_promotion_preconditions(item, target)
    assert result["ok"] is True
    assert result["no_ambiguous_match_check"]["ok"] is False  # surfaced, never suppressed


def test_check_promotion_preconditions_opt_in_blocks_ambiguous_match(tmp_path):
    target = tmp_path / "thesis.docx"
    target.write_bytes(b"content")
    base = ad.compute_base_sha256(target)
    item = {
        "planned_output": ad.serialize_planned_output({
            "source_type": "code",
            "targets": [{"uri": "thesis.docx", "selector": {"type": "range", "start_line": 1, "end_line": 1}}],
            "promotion": {"base_sha256": base},
        })
    }
    result = ad.check_promotion_preconditions(item, target, require_resource_footprint=True)
    assert result["ok"] is False
    assert "ambiguous" in result["reason"]


def test_check_promotion_preconditions_opt_in_passes_with_a_typed_anchor(tmp_path):
    target = tmp_path / "thesis.docx"
    target.write_bytes(b"content")
    base = ad.compute_base_sha256(target)
    item = {
        "planned_output": ad.serialize_planned_output({
            "source_type": "code",
            "targets": [{"uri": "thesis.docx", "selector": {"type": "range", "start_line": 1, "end_line": 1}}],
            "promotion": {"base_sha256": base, "resource_footprint": ["paraId:00AA00BB"]},
        })
    }
    result = ad.check_promotion_preconditions(item, target, require_resource_footprint=True)
    assert result["ok"] is True


# --- check_artifact_recipe_completeness -------------------------------------

def test_recipe_completeness_not_applicable_when_no_artifact_kind():
    result = ad.check_artifact_recipe_completeness({})
    assert result["applicable"] is False
    assert result["complete"] is True
    assert result["missing"] == []


def test_recipe_completeness_reports_every_missing_piece():
    item = {"artifact_kind": "document_only"}
    result = ad.check_artifact_recipe_completeness(item)
    assert result["applicable"] is True
    assert result["complete"] is False
    assert "planned_output" in result["missing"]
    assert "artifact_recipe" in result["missing"]
    assert "tool_requirements" in result["missing"]
    assert result["reason"] is not None


def test_recipe_completeness_flags_ambiguous_promotion_match():
    item = {
        "artifact_kind": "document_only",
        "planned_output": ad.serialize_planned_output({
            "source_type": "code",
            "targets": [{"uri": "thesis.docx", "selector": {"type": "range", "start_line": 1, "end_line": 1}}],
            "promotion": {"base_sha256": "a" * 64},  # no resource_footprint
        }),
        "artifact_recipe": ad.serialize_artifact_recipe(_valid_recipe()),
        "tool_requirements": tool_requirements_module.canonical_json(
            tool_requirements_module.normalize_tool_requirements([{
                "name": "merge_docx_draft", "server_or_namespace": "meridian-docs",
                "required_or_preferred": "required", "purpose": "apply the promotion",
            }])
        ),
    }
    result = ad.check_artifact_recipe_completeness(item)
    assert result["complete"] is False
    assert any("ambiguous" in m for m in result["missing"])


def test_recipe_completeness_true_when_every_piece_present():
    item = {
        "artifact_kind": "document_only",
        "planned_output": ad.serialize_planned_output({
            "source_type": "code",
            "targets": [{"uri": "thesis.docx", "selector": {"type": "range", "start_line": 1, "end_line": 1}}],
            "promotion": {
                "base_sha256": "a" * 64,
                "resource_footprint": ["table:results-summary"],
            },
        }),
        "artifact_recipe": ad.serialize_artifact_recipe(_valid_recipe()),
        "tool_requirements": tool_requirements_module.canonical_json(
            tool_requirements_module.normalize_tool_requirements([{
                "name": "merge_docx_draft", "server_or_namespace": "meridian-docs",
                "required_or_preferred": "required", "purpose": "apply the promotion",
            }])
        ),
    }
    result = ad.check_artifact_recipe_completeness(item)
    assert result["complete"] is True
    assert result["missing"] == []
    assert result["reason"] is None


# ---------------------------------------------------------------------------
# 7. docx_integrity_gate.RECIPE_CHECK_REGISTRY / describe_required_checks.
# ---------------------------------------------------------------------------

def test_recipe_check_registry_covers_every_declared_check_flag():
    """Every checks.* flag artifact_declaration's schema accepts must have
    an EXACT registry entry -- the two must never drift apart."""
    assert set(gate_module.RECIPE_CHECK_REGISTRY) == set(ad._RECIPE_CHECK_FIELDS)
    for reference in gate_module.RECIPE_CHECK_REGISTRY.values():
        assert isinstance(reference, str) and reference.strip()


def test_describe_required_checks_no_recipe_declared():
    result = gate_module.describe_required_checks({})
    assert result == {"declared": False, "required": {}}


def test_describe_required_checks_resolves_only_true_flags():
    item = {
        "artifact_recipe": ad.serialize_artifact_recipe({
            "execution_path": "local",
            "rollback_policy": "manual_restore",
            "checks": {"outputs_provenance_check_required": True},
            "focused_tests": ["tests/test_outputs_provenance.py::test_x"],
        })
    }
    result = gate_module.describe_required_checks(item)
    assert result["declared"] is True
    assert set(result["required"]) == {"outputs_provenance_check_required"}
    assert "meridian-outputs" in result["required"]["outputs_provenance_check_required"]


def test_word_com_render_check_reference_is_real_and_timeout_aware(tmp_path):
    """75de5905 (adversarial gate) — RECIPE_CHECK_REGISTRY's
    word_com_render_check_required entry names render_gate.
    check_word_com_render_receipt by string; this proves that reference is
    not just a string but a REAL, importable, callable function whose
    result genuinely distinguishes a Word/COM TIMEOUT from other failure
    classes (detail.timed_out), rather than collapsing every failure into
    one undifferentiated bucket. Robust across platforms/CI runners:
    asserts the three-state contract and the timed_out diagnostic's
    presence, never a fixed outcome (a real Word/COM environment and a
    Word-less Linux CI runner legitimately produce different statuses)."""
    # Same guarded sys.path pattern tests/test_meridian_docs_equations.py
    # already establishes for importing the extension package directly.
    import os as _os
    import sys as _sys

    _ext_path = _os.path.abspath(
        _os.path.join(_os.path.dirname(__file__), "..", "extensions", "meridian-docs")
    )
    if _ext_path not in _sys.path:
        _sys.path.insert(0, _ext_path)
    from meridian_docs.render_gate import check_word_com_render_receipt  # noqa: PLC0415

    target = tmp_path / "not-a-real-docx.docx"
    target.write_bytes(b"stub content, not a real OOXML package")

    result = check_word_com_render_receipt(str(target))
    assert result["status"] in ("rendered", "unavailable-with-reason", "failed")
    if result["status"] == "failed":
        # The exact field describe_required_checks's registry entry implies
        # exists: a receiver can tell "this specific attempt timed out" from
        # "this failed for some other reason" without guessing.
        detail = result.get("detail") or {}
        assert "timed_out" in detail
        assert isinstance(detail["timed_out"], bool)
