"""Tests for docs_intel.apply_reviewable_edit_transaction (0d62f067,
BE4ED581-W2: "batch reviewable section moves and caption edits into one
fail-closed writer transaction").

Scope of this pass (see the item's own priority list, and the module
comment directly above apply_reviewable_edit_transaction in docs_intel.py):
this composes prose-edit packets (4c992e91) + the four EXISTING structural
mutators (move_section / copy_section / relocate_table / relocate_figure,
fe989980's draft_output_path/wave_run_id) into one all-or-nothing chained
transaction. Caption normalization (edit_caption has no draft-mode support
yet) and cross-process single-writer locking (meridian.db.locks lives in
the DB-backed Meridian core package, not this stdlib-only extension) are
explicitly OUT of scope here -- deferred follow-up, documented in the
orchestrator's own module comment and docstring, not silently dropped.

Covers:
  - a successful multi-step chain (prose edit + move_section) produces
    EXACTLY ONE merge_draft_into_canonical promotion, preserves paragraph
    IDs and a bookmark/REF cross-reference across the whole chain, and
    cleans up every intermediate draft by default;
  - all-or-nothing rollback: a late step failing leaves canonical_path
    byte-identical and leaves NO orphaned staged draft files behind;
  - a merge-time failure (every step succeeded, promotion itself failed)
    keeps the final pre-merge draft on disk for inspection/retry, while
    still cleaning up now-superseded intermediate drafts, and restores
    canonical_path;
  - copy_section and relocate_table each work as a chained step kind (not
    just move_section);
  - the step dispatcher (_run_transaction_step) routes each of the five
    supported kinds to the correct underlying primitive with the expected
    injected docx_path/draft_output_path/wave_run_id, including
    relocate_figure (covered here at the dispatch level -- its own
    fixture-heavy behavioral contract is already covered by
    test_relocate_figure.py);
  - request-shape validation (empty/malformed steps, unsupported kind,
    missing wave_run_id/draft_dir) refuses before touching canonical_path
    or draft_dir at all;
  - a whole-document staleness mismatch (expected_source_fingerprint)
    short-circuits before any step runs.

All tests are pure Python (stdlib + pytest) -- no mcp, no network, and
(per the item's own instructions) no direct mutation of a real dissertation
document: every test writes a disposable, synthetic .docx to tmp_path.
"""
from __future__ import annotations

import io
import os
import zipfile
from typing import Any

import pytest

from meridian_docs import docs_intel


@pytest.fixture(autouse=True)
def _default_render_capability(monkeypatch):
    """Same rationale as test_fe989980_merge_draft.py's own fixture: the
    final merge_draft_into_canonical call this orchestrator makes invokes
    the real render-capability gate. Every test here exercises
    ORCHESTRATION correctness (chaining/rollback/cleanup), not the render
    gate itself, so stub a successful 'rendered' result by default."""
    monkeypatch.setattr(
        docs_intel.render_gate,
        "check_render_capability",
        lambda docx_path, **kwargs: {
            "status": "rendered",
            "backend": "test-stub",
            "detail": {"stub": True},
        },
    )


_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W14 = "http://schemas.microsoft.com/office/word/2010/wordml"


def _make_docx_bytes(xml: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", xml)
    return buf.getvalue()


def _write_docx(tmp_path, xml: str, name: str = "canonical.docx") -> str:
    path = str(tmp_path / name)
    with open(path, "wb") as fh:
        fh.write(_make_docx_bytes(xml))
    return path


def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def _body_texts(path: str) -> list[str]:
    _raw, root = docs_intel._load_docx_xml_stdlib(path)
    body = root.find(docs_intel._q(_W, "body"))
    out = []
    for p in body.iter(docs_intel._q(_W, "p")):
        text = "".join(t.text or "" for t in p.iter(docs_intel._q(_W, "t")))
        if text:
            out.append(text)
    return out


def _body_para_ids(path: str) -> list[str | None]:
    _raw, root = docs_intel._load_docx_xml_stdlib(path)
    body = root.find(docs_intel._q(_W, "body"))
    return [p.get(docs_intel._q(_W14, "paraId")) for p in body.iter(docs_intel._q(_W, "p"))]


# A two-section document with a bookmark on the second heading and a
# REF field elsewhere pointing at it -- lets tests confirm a cross-reference
# survives the whole chain, plus a bare table for relocate_table coverage.
_DOC_XML = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:p w14:paraId="H0000001">
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Introduction</w:t></w:r>
    </w:p>
    <w:p w14:paraId="P0000001">
      <w:r><w:t>Original intro paragraph.</w:t></w:r>
    </w:p>
    <w:p w14:paraId="H0000002">
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:bookmarkStart w:id="0" w:name="_Ref100"/>
      <w:r><w:t>Results</w:t></w:r>
      <w:bookmarkEnd w:id="0"/>
    </w:p>
    <w:p w14:paraId="P0000002">
      <w:r><w:t>Results body paragraph.</w:t></w:r>
    </w:p>
    <w:p w14:paraId="P0000003">
      <w:r><w:t xml:space="preserve">See </w:t></w:r>
      <w:fldSimple w:instr="REF _Ref100 \\h"><w:r><w:t>Results</w:t></w:r></w:fldSimple>
      <w:r><w:t xml:space="preserve"> for details.</w:t></w:r>
    </w:p>
    <w:tbl>
      <w:tblPr><w:tblStyle w:val="TableGrid"/></w:tblPr>
      <w:tblGrid><w:gridCol w:w="2000"/></w:tblGrid>
      <w:tr><w:tc><w:p><w:r><w:t>Cell</w:t></w:r></w:p></w:tc></w:tr>
    </w:tbl>
    <w:p w14:paraId="P0000004">
      <w:r><w:t>End paragraph.</w:t></w:r>
    </w:p>
    <w:sectPr/>
  </w:body>
</w:document>
"""


def _prose_step(doc_path: str, para_id: str, new_text: str) -> dict[str, Any]:
    packet = docs_intel.build_prose_edit_packet(doc_path, {"para_id": para_id}, new_text)
    assert packet["status"] == "built", packet
    return {"kind": "prose_edit_packets", "params": {"packets": [packet]}}


def _move_step(section_id: str, destination_anchor_para_id: str, **extra: Any) -> dict[str, Any]:
    return {
        "kind": "move_section",
        "params": {
            "section_id": section_id,
            "destination_anchor_para_id": destination_anchor_para_id,
            **extra,
        },
    }


# ---------------------------------------------------------------------------
# Successful chains
# ---------------------------------------------------------------------------

class TestSuccessfulTransaction:
    def test_prose_then_move_produces_exactly_one_merge_call(self, tmp_path, monkeypatch) -> None:
        canonical = _write_docx(tmp_path, _DOC_XML)
        draft_dir = str(tmp_path / "drafts")

        merge_calls: list[tuple[str, str]] = []
        real_merge = docs_intel.merge_draft_into_canonical

        def _counting_merge(canonical_path, draft_path, **kwargs):
            merge_calls.append((canonical_path, draft_path))
            return real_merge(canonical_path, draft_path, **kwargs)

        monkeypatch.setattr(docs_intel, "merge_draft_into_canonical", _counting_merge)

        steps = [
            _prose_step(canonical, "P0000001", "Edited intro paragraph."),
            _move_step("H0000002", "H0000001", destination_position="before"),
        ]
        result = docs_intel.apply_reviewable_edit_transaction(
            canonical, steps, draft_dir, "wave-1",
        )

        assert result["transaction"] is True, result
        assert result["merged"] is True
        assert len(merge_calls) == 1
        assert len(result["steps_applied"]) == 2

        texts = _body_texts(canonical)
        assert "Edited intro paragraph." in texts
        assert "Original intro paragraph." not in texts
        # Results section (H0000002's own heading + body) now precedes
        # Introduction -- move_section actually ran, chained on top of the
        # prose edit's own draft.
        assert texts.index("Results body paragraph.") < texts.index("Edited intro paragraph.")

        # Cross-reference survives the whole chain: the bookmark travelled
        # with the moved heading, and find_references_to still resolves it.
        refs = docs_intel.find_references_to(canonical, "H0000002")
        assert "error" not in refs
        assert refs.get("reference_count", len(refs.get("references", []))) >= 1

    def test_paragraph_ids_preserved_across_the_whole_chain(self, tmp_path) -> None:
        canonical = _write_docx(tmp_path, _DOC_XML)
        draft_dir = str(tmp_path / "drafts")
        before_ids = set(_body_para_ids(canonical))

        steps = [
            _prose_step(canonical, "P0000001", "Edited intro paragraph."),
            _move_step("H0000002", "H0000001", destination_position="before"),
        ]
        result = docs_intel.apply_reviewable_edit_transaction(canonical, steps, draft_dir, "wave-ids")

        assert result["transaction"] is True, result
        after_ids = set(_body_para_ids(canonical))
        # Every original paraId is still present -- move_section/prose
        # edits never regenerate ids for content they didn't duplicate.
        assert before_ids == after_ids

    def test_cleanup_drafts_default_removes_every_staged_file(self, tmp_path) -> None:
        canonical = _write_docx(tmp_path, _DOC_XML)
        draft_dir = str(tmp_path / "drafts")

        steps = [
            _prose_step(canonical, "P0000001", "Edited intro paragraph."),
            _move_step("H0000002", "H0000001", destination_position="before"),
        ]
        result = docs_intel.apply_reviewable_edit_transaction(canonical, steps, draft_dir, "wave-cleanup")

        assert result["transaction"] is True, result
        assert result["cleanup"]["failed"] == []
        assert len(result["cleanup"]["deleted"]) == 2
        remaining = os.listdir(draft_dir) if os.path.isdir(draft_dir) else []
        assert remaining == []

    def test_cleanup_drafts_false_keeps_staged_files_on_success(self, tmp_path) -> None:
        canonical = _write_docx(tmp_path, _DOC_XML)
        draft_dir = str(tmp_path / "drafts")

        steps = [_prose_step(canonical, "P0000001", "Edited intro paragraph.")]
        result = docs_intel.apply_reviewable_edit_transaction(
            canonical, steps, draft_dir, "wave-keep", cleanup_drafts=False,
        )

        assert result["transaction"] is True, result
        assert result["cleanup"] == {"deleted": [], "failed": []}
        remaining = os.listdir(draft_dir)
        assert len(remaining) == 1

    def test_copy_section_step_works_in_a_chain(self, tmp_path) -> None:
        canonical = _write_docx(tmp_path, _DOC_XML)
        draft_dir = str(tmp_path / "drafts")

        steps = [{
            "kind": "copy_section",
            "params": {
                "section_id": "H0000001",
                "destination_anchor_para_id": "H0000002",
                "destination_position": "after",
                "trim_original_to": "See the copy below.",
            },
        }]
        result = docs_intel.apply_reviewable_edit_transaction(canonical, steps, draft_dir, "wave-copy")

        assert result["transaction"] is True, result
        texts = _body_texts(canonical)
        assert "See the copy below." in texts
        # The copy's own body paragraph text is duplicated verbatim.
        assert texts.count("Original intro paragraph.") == 1

    def test_relocate_table_step_works_in_a_chain(self, tmp_path) -> None:
        canonical = _write_docx(tmp_path, _DOC_XML)
        draft_dir = str(tmp_path / "drafts")

        steps = [{
            # The bare <w:tbl> is the 6th body child (0-based index 5) in
            # _DOC_XML: H1, P1, H2, P2, P3, tbl, P4.
            "kind": "relocate_table",
            "params": {"table_index": 5, "destination_anchor_para_id": "H0000001", "destination_position": "before"},
        }]
        result = docs_intel.apply_reviewable_edit_transaction(canonical, steps, draft_dir, "wave-table")

        assert result["transaction"] is True, result
        _raw, root = docs_intel._load_docx_xml_stdlib(canonical)
        body = root.find(docs_intel._q(_W, "body"))
        first_child_tag = list(body)[0].tag.rsplit("}", 1)[-1]
        assert first_child_tag == "tbl"


# ---------------------------------------------------------------------------
# All-or-nothing rollback
# ---------------------------------------------------------------------------

class TestRollback:
    def test_late_step_failure_leaves_canonical_untouched_and_no_orphaned_drafts(self, tmp_path) -> None:
        canonical = _write_docx(tmp_path, _DOC_XML)
        original_bytes = _read_bytes(canonical)
        draft_dir = str(tmp_path / "drafts")

        steps = [
            _prose_step(canonical, "P0000001", "Edited intro paragraph."),
            _move_step("H0000002", "H0000001", destination_position="before"),
            # Step 2 targets a section_id that does not exist -- fails.
            _move_step("H_DOES_NOT_EXIST", "H0000001"),
        ]
        result = docs_intel.apply_reviewable_edit_transaction(canonical, steps, draft_dir, "wave-rollback")

        assert result["transaction"] is False
        assert result["reason"] == "step_failed"
        assert result["failed_step_index"] == 2
        assert result["failed_step_kind"] == "move_section"
        assert "error" in result["step_result"]
        assert len(result["steps_applied"]) == 2

        # canonical_path was only ever READ during this whole batch.
        assert _read_bytes(canonical) == original_bytes

        # No orphaned staged files survive a rejected batch.
        remaining = os.listdir(draft_dir) if os.path.isdir(draft_dir) else []
        assert remaining == []
        assert result["cleanup"]["failed"] == []

    def test_first_step_failure_leaves_canonical_untouched(self, tmp_path) -> None:
        canonical = _write_docx(tmp_path, _DOC_XML)
        original_bytes = _read_bytes(canonical)
        draft_dir = str(tmp_path / "drafts")

        steps = [_move_step("H_DOES_NOT_EXIST", "H0000001")]
        result = docs_intel.apply_reviewable_edit_transaction(canonical, steps, draft_dir, "wave-first-fail")

        assert result["transaction"] is False
        assert result["reason"] == "step_failed"
        assert result["failed_step_index"] == 0
        assert result["steps_applied"] == []
        assert _read_bytes(canonical) == original_bytes
        remaining = os.listdir(draft_dir) if os.path.isdir(draft_dir) else []
        assert remaining == []


# ---------------------------------------------------------------------------
# Merge-time failure (every step succeeded, promotion itself did not)
# ---------------------------------------------------------------------------

class TestMergeFailure:
    def test_merge_failure_keeps_final_draft_but_cleans_up_intermediate_ones(self, tmp_path, monkeypatch) -> None:
        canonical = _write_docx(tmp_path, _DOC_XML)
        original_bytes = _read_bytes(canonical)
        draft_dir = str(tmp_path / "drafts")

        # Force the render-capability gate to report a genuine render
        # failure -- merge_draft_into_canonical restores canonical from its
        # own backup and returns merged=False, exactly like a structural
        # verification failure.
        monkeypatch.setattr(
            docs_intel.render_gate,
            "check_render_capability",
            lambda docx_path, **kwargs: {"status": "failed", "backend": "test-stub", "detail": {}},
        )

        steps = [
            _prose_step(canonical, "P0000001", "Edited intro paragraph."),
            _move_step("H0000002", "H0000001", destination_position="before"),
        ]
        result = docs_intel.apply_reviewable_edit_transaction(canonical, steps, draft_dir, "wave-merge-fail")

        assert result["transaction"] is False
        assert result["reason"] == "merge_failed"
        assert result["merged"] is False
        assert len(result["steps_applied"]) == 2

        # canonical_path restored to its pre-transaction content.
        assert _read_bytes(canonical) == original_bytes

        # The final pre-merge draft is kept on disk for inspection/retry...
        final_draft_path = result["final_draft_path"]
        assert os.path.exists(final_draft_path)
        # ...but the now-superseded intermediate draft (step 0's output) was
        # cleaned up.
        remaining = sorted(os.listdir(draft_dir))
        assert remaining == [os.path.basename(final_draft_path)]


# ---------------------------------------------------------------------------
# Request-shape validation -- refused before touching anything
# ---------------------------------------------------------------------------

class TestRequestValidation:
    def test_empty_steps_refused(self, tmp_path) -> None:
        canonical = _write_docx(tmp_path, _DOC_XML)
        draft_dir = str(tmp_path / "drafts")
        result = docs_intel.apply_reviewable_edit_transaction(canonical, [], draft_dir, "wave-x")
        assert result == {
            "transaction": False,
            "reason": "invalid_request",
            "error": "steps must be a non-empty list",
        }
        assert not os.path.isdir(draft_dir)

    def test_missing_wave_run_id_refused(self, tmp_path) -> None:
        canonical = _write_docx(tmp_path, _DOC_XML)
        draft_dir = str(tmp_path / "drafts")
        steps = [_prose_step(canonical, "P0000001", "x")]
        result = docs_intel.apply_reviewable_edit_transaction(canonical, steps, draft_dir, "")
        assert result["transaction"] is False
        assert result["reason"] == "invalid_request"
        assert not os.path.isdir(draft_dir)

    def test_missing_draft_dir_refused(self, tmp_path) -> None:
        canonical = _write_docx(tmp_path, _DOC_XML)
        steps = [_prose_step(canonical, "P0000001", "x")]
        result = docs_intel.apply_reviewable_edit_transaction(canonical, steps, "", "wave-x")
        assert result["transaction"] is False
        assert result["reason"] == "invalid_request"

    def test_unsupported_step_kind_refused(self, tmp_path) -> None:
        canonical = _write_docx(tmp_path, _DOC_XML)
        original_bytes = _read_bytes(canonical)
        draft_dir = str(tmp_path / "drafts")
        steps = [{"kind": "edit_caption", "params": {"caption_para_id": "F0000001", "new_label_text": "x"}}]
        result = docs_intel.apply_reviewable_edit_transaction(canonical, steps, draft_dir, "wave-x")
        assert result["transaction"] is False
        assert result["reason"] == "invalid_request"
        assert "unsupported kind" in result["error"]
        assert _read_bytes(canonical) == original_bytes
        assert not os.path.isdir(draft_dir)

    def test_step_missing_required_param_refused(self, tmp_path) -> None:
        canonical = _write_docx(tmp_path, _DOC_XML)
        draft_dir = str(tmp_path / "drafts")
        steps = [{"kind": "move_section", "params": {"section_id": "H0000001"}}]
        result = docs_intel.apply_reviewable_edit_transaction(canonical, steps, draft_dir, "wave-x")
        assert result["transaction"] is False
        assert result["reason"] == "invalid_request"
        assert "destination_anchor_para_id" in result["error"]

    def test_second_step_invalid_refuses_before_running_first_step(self, tmp_path) -> None:
        """0d62f067 -- every step is structurally validated UP FRONT, before
        step 0 even runs, so an invalid step later in the list can never
        leave a dangling draft from an earlier, otherwise-valid step."""
        canonical = _write_docx(tmp_path, _DOC_XML)
        draft_dir = str(tmp_path / "drafts")
        steps = [
            _prose_step(canonical, "P0000001", "Edited intro paragraph."),
            {"kind": "not_a_real_kind", "params": {}},
        ]
        result = docs_intel.apply_reviewable_edit_transaction(canonical, steps, draft_dir, "wave-x")
        assert result["transaction"] is False
        assert result["reason"] == "invalid_request"
        assert not os.path.isdir(draft_dir)

    def test_document_changed_before_apply_short_circuits(self, tmp_path) -> None:
        canonical = _write_docx(tmp_path, _DOC_XML)
        draft_dir = str(tmp_path / "drafts")
        steps = [_prose_step(canonical, "P0000001", "Edited intro paragraph.")]
        result = docs_intel.apply_reviewable_edit_transaction(
            canonical, steps, draft_dir, "wave-x",
            expected_source_fingerprint="not-the-real-hash",
        )
        assert result["transaction"] is False
        assert result["reason"] == "document_changed_before_apply"
        assert not os.path.isdir(draft_dir)


# ---------------------------------------------------------------------------
# Step dispatcher -- every supported kind routes to the right primitive
# ---------------------------------------------------------------------------

class TestStepDispatch:
    @pytest.mark.parametrize(
        "kind,params,target_fn_name,expected_kwargs",
        [
            (
                "move_section",
                {"section_id": "H1", "destination_anchor_para_id": "H2", "allow_bookmark_split": True},
                "move_section",
                {
                    "docx_path": "SRC", "section_id": "H1", "destination_anchor_para_id": "H2",
                    "destination_position": "after", "allow_bookmark_split": True,
                    "draft_output_path": "DEST", "wave_run_id": "wave-disp",
                },
            ),
            (
                "copy_section",
                {"section_id": "H1", "destination_anchor_para_id": "H2", "trim_original_to": "moved"},
                "copy_section",
                {
                    "docx_path": "SRC", "section_id": "H1", "destination_anchor_para_id": "H2",
                    "destination_position": "after", "trim_original_to": "moved",
                    "allow_relationship_reuse": False,
                    "draft_output_path": "DEST", "wave_run_id": "wave-disp",
                },
            ),
            (
                "relocate_table",
                {"table_index": 0, "destination_anchor_para_id": "H2"},
                "relocate_table",
                {
                    "docx_path": "SRC", "table_index": 0, "destination_anchor_para_id": "H2",
                    "destination_position": "after", "allow_bookmark_split": False,
                    "draft_output_path": "DEST", "wave_run_id": "wave-disp",
                },
            ),
            (
                "relocate_figure",
                {"figure_index": 1, "destination_anchor_para_id": "H2"},
                "relocate_figure",
                {
                    "docx_path": "SRC", "figure_index": 1, "destination_anchor_para_id": "H2",
                    "destination_position": "after", "allow_bookmark_split": False,
                    "draft_output_path": "DEST", "wave_run_id": "wave-disp",
                    "artifact_provenance": None,
                },
            ),
        ],
    )
    def test_dispatch_routes_to_expected_primitive_with_expected_kwargs(
        self, monkeypatch, kind, params, target_fn_name, expected_kwargs,
    ) -> None:
        captured: dict[str, Any] = {}

        def _stub(**kwargs):
            captured.update(kwargs)
            return {"status": "stubbed"}

        monkeypatch.setattr(docs_intel, target_fn_name, _stub)
        result = docs_intel._run_transaction_step(kind, params, "SRC", "DEST", "wave-disp")

        assert result == {"status": "stubbed"}
        assert captured == expected_kwargs

    def test_dispatch_routes_prose_edit_packets(self, monkeypatch) -> None:
        captured: dict[str, Any] = {}

        def _stub(document_path, packets, draft_output_path, *, expected_source_fingerprint=None):
            captured.update({
                "document_path": document_path, "packets": packets,
                "draft_output_path": draft_output_path,
                "expected_source_fingerprint": expected_source_fingerprint,
            })
            return {"applied": True}

        monkeypatch.setattr(docs_intel, "apply_prose_edit_packets", _stub)
        packets = [{"packet_kind": "prose_edit"}]
        result = docs_intel._run_transaction_step(
            "prose_edit_packets", {"packets": packets}, "SRC", "DEST", "wave-disp",
        )

        assert result == {"applied": True}
        assert captured == {
            "document_path": "SRC", "packets": packets, "draft_output_path": "DEST",
            "expected_source_fingerprint": None,
        }
