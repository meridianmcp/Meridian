"""Tests for the outputs annotations layer (9e02e448).

Covers:
* ``annotate_outputs`` MCP tool (via ``OutputsFtsIndex.add_annotation`` and the
  module-level ``annotate_outputs`` function).
* ``MERIDIAN_NOTES.md`` enforced auto-pickup: a MERIDIAN_NOTES.md placed at ANY
  level in the outputs tree MUST be ingested on every rebuild — this is the
  critical regression test the sprint spec demands.
* ``search_outputs`` auto-surfacing: each hit's ``annotations`` field carries
  any annotation keyed to the hit's own path OR a nearest-ancestor directory —
  no second tool call needed.
* Tier 1 (root) vs Tier 2 (sub-path) annotation semantics.
* Ancestor annotation surfacing (a note on a parent dir appears in a child hit).
* ``run_params`` round-trip through JSON.
"""
from __future__ import annotations

import json
import os

import pytest

from meridian import outputs_indexer as oi


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _write(path, text: str) -> None:
    os.makedirs(os.path.dirname(path) if os.path.dirname(path) else ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _seed_tree(tmp_path):
    """Minimal outputs tree with searchable content."""
    _write(str(tmp_path / "results.csv"), "temperature,pressure,volume\n300,101,22\n")
    _write(str(tmp_path / "metrics.json"), json.dumps({"accuracy": 0.91, "loss": 0.09}))
    return tmp_path


# ---------------------------------------------------------------------------
# annotate_outputs — tool-level (module function)
# ---------------------------------------------------------------------------

def test_annotate_outputs_stores_and_retrieves(tmp_path):
    """annotate_outputs(outputs_dir, path, note) upserts and is retrievable."""
    outputs = str(tmp_path / "outputs")
    os.makedirs(outputs)
    path = os.path.join(outputs, "run_42")

    result = oi.annotate_outputs(outputs, path, "PCA on, BFS off")
    assert result.get("error") is None, result
    assert result["note"] == "PCA on, BFS off"
    assert result["source"] == "tool"

    # The same cached index should have it retrievable.
    index = oi._get_cached_index(outputs)
    annotations = index.get_annotations_for_path(path)
    assert len(annotations) == 1
    assert annotations[0]["note"] == "PCA on, BFS off"


def test_annotate_outputs_run_params_roundtrip(tmp_path):
    """run_params is stored as JSON and returned as a dict."""
    outputs = str(tmp_path / "outputs")
    os.makedirs(outputs)
    path = os.path.join(outputs, "run_1")
    params = {"lr": 0.001, "epochs": 100, "batch_size": 32}

    result = oi.annotate_outputs(outputs, path, "training run", run_params=params)
    assert result.get("error") is None
    assert result["run_params"] == params

    index = oi._get_cached_index(outputs)
    annotations = index.get_annotations_for_path(path)
    assert annotations[0]["run_params"] == params


def test_annotate_outputs_upserts_on_same_path(tmp_path):
    """A second annotate_outputs call on the same path replaces the note."""
    outputs = str(tmp_path / "outputs")
    os.makedirs(outputs)
    path = os.path.join(outputs, "run_1")

    oi.annotate_outputs(outputs, path, "first note")
    oi.annotate_outputs(outputs, path, "second note (updated)")

    index = oi._get_cached_index(outputs)
    annotations = index.get_annotations_for_path(path)
    # Only ONE annotation should exist (upsert, not append for same source).
    assert len(annotations) == 1
    assert annotations[0]["note"] == "second note (updated)"


def test_annotate_outputs_tier1_root(tmp_path):
    """Tier 1: annotating outputs_dir itself (root annotation)."""
    outputs = str(tmp_path / "outputs")
    os.makedirs(outputs)

    result = oi.annotate_outputs(outputs, outputs, "This tree: PCA sweep experiment")
    assert result.get("error") is None
    assert result["path"] == outputs

    index = oi._get_cached_index(outputs)
    annotations = index.get_annotations_for_path(outputs)
    assert len(annotations) == 1
    assert "PCA sweep" in annotations[0]["note"]


def test_annotate_outputs_error_on_empty_note(tmp_path):
    """annotate_outputs returns an error dict for blank note."""
    outputs = str(tmp_path / "outputs")
    os.makedirs(outputs)
    result = oi.annotate_outputs(outputs, outputs, "")
    assert "error" in result


def test_annotate_outputs_error_on_missing_path(tmp_path):
    """annotate_outputs returns an error dict when path is blank."""
    outputs = str(tmp_path / "outputs")
    os.makedirs(outputs)
    result = oi.annotate_outputs(outputs, "", "some note")
    assert "error" in result


# ---------------------------------------------------------------------------
# MERIDIAN_NOTES.md enforced pickup — critical regression test
# ---------------------------------------------------------------------------

def test_meridian_notes_at_root_is_ingested_on_rebuild(tmp_path):
    """MERIDIAN_NOTES.md at the outputs root is ingested during rebuild.

    This is the critical regression test: the pickup is a GUARANTEED step in
    the directory walk, not an advisory convention. The test asserts that a
    MERIDIAN_NOTES.md at the top level of the outputs tree is present in the
    annotations table after rebuild(), never silently skipped.
    """
    outputs = str(tmp_path / "outputs")
    os.makedirs(outputs)
    _write(str(tmp_path / "outputs" / "results.csv"), "a,b\n1,2\n")
    _write(
        str(tmp_path / "outputs" / oi.MERIDIAN_NOTES_FILENAME),
        "Root-level annotation: this is the PCA sweep series.",
    )

    idx = oi.OutputsFtsIndex(outputs)
    try:
        idx.rebuild()
        # The annotation must exist keyed to the directory (outputs root).
        annotations = idx.get_annotations_for_path(outputs)
        assert len(annotations) >= 1, (
            f"MERIDIAN_NOTES.md at {outputs} was NOT ingested — "
            "enforced pickup is broken"
        )
        assert any("PCA sweep" in a["note"] for a in annotations), annotations
        assert any(a["source"] == oi.MERIDIAN_NOTES_FILENAME for a in annotations)
    finally:
        idx.close()


def test_meridian_notes_in_subdirectory_is_ingested(tmp_path):
    """MERIDIAN_NOTES.md anywhere in the tree (not just root) is ingested."""
    outputs = str(tmp_path / "outputs")
    subdir = os.path.join(outputs, "run_42")
    os.makedirs(subdir)
    _write(os.path.join(outputs, "top.csv"), "x\n1\n")
    _write(os.path.join(subdir, "results.csv"), "y\n2\n")
    _write(
        os.path.join(subdir, oi.MERIDIAN_NOTES_FILENAME),
        "Run 42: learning rate 0.001, overwritten 5x.",
    )

    idx = oi.OutputsFtsIndex(outputs)
    try:
        idx.rebuild()
        # The annotation is keyed to the subdirectory, not the root.
        annotations = idx.get_annotations_for_path(subdir)
        assert len(annotations) >= 1, (
            f"MERIDIAN_NOTES.md in subdirectory {subdir} was NOT ingested"
        )
        assert any("learning rate" in a["note"] for a in annotations)
        assert any(a["source"] == oi.MERIDIAN_NOTES_FILENAME for a in annotations)
    finally:
        idx.close()


def test_meridian_notes_at_multiple_levels_all_ingested(tmp_path):
    """MERIDIAN_NOTES.md at the root AND in a subdirectory are BOTH ingested."""
    outputs = str(tmp_path / "outputs")
    subdir = os.path.join(outputs, "sub")
    os.makedirs(subdir)
    _write(os.path.join(outputs, "t.csv"), "a\n1\n")
    _write(os.path.join(subdir, "u.csv"), "b\n2\n")
    _write(
        os.path.join(outputs, oi.MERIDIAN_NOTES_FILENAME),
        "Root note: overall experiment.",
    )
    _write(
        os.path.join(subdir, oi.MERIDIAN_NOTES_FILENAME),
        "Sub note: sub-experiment detail.",
    )

    idx = oi.OutputsFtsIndex(outputs)
    try:
        idx.rebuild()
        root_ann = idx.get_annotations_for_path(outputs)
        sub_ann = idx.get_annotations_for_path(subdir)
        assert any("overall experiment" in a["note"] for a in root_ann), root_ann
        assert any("sub-experiment" in a["note"] for a in sub_ann), sub_ann
    finally:
        idx.close()


def test_meridian_notes_pickup_runs_even_on_unchanged_tree(tmp_path):
    """MERIDIAN_NOTES.md is re-ingested on every rebuild(), not just when FTS changes.

    A new MERIDIAN_NOTES.md added AFTER the first rebuild must appear in annotations
    after a second rebuild() call, even if no FTS-indexed files changed.
    """
    outputs = str(tmp_path / "outputs")
    os.makedirs(outputs)
    _write(os.path.join(outputs, "r.csv"), "col\n1\n")

    idx = oi.OutputsFtsIndex(outputs)
    try:
        idx.rebuild()
        # No MERIDIAN_NOTES.md yet.
        assert idx.get_annotations_for_path(outputs) == []

        # Drop a MERIDIAN_NOTES.md and rebuild.
        _write(
            os.path.join(outputs, oi.MERIDIAN_NOTES_FILENAME),
            "Added after first build.",
        )
        idx.rebuild()
        annotations = idx.get_annotations_for_path(outputs)
        assert any("Added after first build" in a["note"] for a in annotations), (
            "MERIDIAN_NOTES.md was NOT picked up on subsequent rebuild"
        )
    finally:
        idx.close()


# ---------------------------------------------------------------------------
# search_outputs auto-surfacing
# ---------------------------------------------------------------------------

def test_search_outputs_hits_include_annotations(tmp_path):
    """search_outputs auto-includes annotations in each hit (no second call needed)."""
    outputs = _seed_tree(tmp_path / "outputs")
    outputs_str = str(outputs)
    path = str(outputs / "results.csv")

    # Add an annotation for the specific file.
    oi.annotate_outputs(outputs_str, path, "temperature sweep — final params")

    result = oi.search_outputs(outputs_str, "temperature pressure")
    hits = result.get("hits", [])
    assert hits, "Expected at least one hit for 'temperature pressure'"

    # The hit for results.csv should carry the annotation.
    hit_paths = [h["path"] for h in hits]
    csv_hit = next((h for h in hits if "results.csv" in h["path"]), None)
    assert csv_hit is not None, f"results.csv not in hits: {hit_paths}"
    assert "annotations" in csv_hit, "annotations key missing from hit"
    annotations = csv_hit["annotations"]
    assert any("temperature sweep" in a["note"] for a in annotations), annotations


def test_search_outputs_ancestor_annotation_surfaces_in_child_hit(tmp_path):
    """A parent-dir annotation surfaces in hits for files inside that directory."""
    outputs = str(tmp_path / "outputs")
    subdir = os.path.join(outputs, "run_42")
    os.makedirs(subdir)
    _write(os.path.join(subdir, "metrics.csv"), "loss,acc\n0.1,0.9\n")

    # Annotate the subdirectory (not the file).
    oi.annotate_outputs(outputs, subdir, "Run 42: best checkpoint")

    result = oi.search_outputs(outputs, "loss accuracy")
    hits = result.get("hits", [])
    csv_hit = next((h for h in hits if "metrics.csv" in h["path"]), None)
    assert csv_hit is not None, f"metrics.csv not in hits: {hits}"
    annotations = csv_hit.get("annotations", [])
    assert any("Run 42" in a["note"] for a in annotations), (
        f"parent-dir annotation not surfaced in child hit: {annotations}"
    )


def test_search_outputs_hit_with_no_annotation_has_empty_list(tmp_path):
    """Hits with no matching annotation have an empty annotations list, not missing key."""
    outputs = _seed_tree(tmp_path / "outputs")
    outputs_str = str(outputs)

    result = oi.search_outputs(outputs_str, "temperature")
    hits = result.get("hits", [])
    assert hits, "Expected hits"
    # No annotations added — every hit should have annotations=[] not missing key.
    for hit in hits:
        assert "annotations" in hit, f"annotations key missing from hit: {hit}"
        assert isinstance(hit["annotations"], list)


def test_meridian_notes_annotation_surfaces_in_search_results(tmp_path):
    """MERIDIAN_NOTES.md content appears in search_outputs annotations."""
    outputs = str(tmp_path / "outputs")
    os.makedirs(outputs)
    _write(os.path.join(outputs, "loss.csv"), "epoch,loss\n1,0.5\n2,0.3\n")
    _write(
        os.path.join(outputs, oi.MERIDIAN_NOTES_FILENAME),
        "Experiment: SGD with momentum=0.9",
    )

    result = oi.search_outputs(outputs, "epoch loss")
    hits = result.get("hits", [])
    csv_hit = next((h for h in hits if "loss.csv" in h["path"]), None)
    assert csv_hit is not None, f"loss.csv not in hits: {hits}"
    annotations = csv_hit.get("annotations", [])
    # The MERIDIAN_NOTES.md annotation (keyed to the parent dir = outputs root)
    # should surface on the child hit.
    assert any("SGD" in a["note"] for a in annotations), (
        f"MERIDIAN_NOTES.md content not surfaced in search hit: {annotations}"
    )


# ---------------------------------------------------------------------------
# get_annotations_for_path — direct unit tests
# ---------------------------------------------------------------------------

def test_get_annotations_for_path_returns_exact_match(tmp_path):
    outputs = str(tmp_path / "outputs")
    os.makedirs(outputs)
    idx = oi.OutputsFtsIndex(outputs)
    try:
        path = os.path.join(outputs, "run_1", "file.csv")
        idx.add_annotation(path, "exact match note")
        annotations = idx.get_annotations_for_path(path)
        assert len(annotations) == 1
        assert annotations[0]["note"] == "exact match note"
    finally:
        idx.close()


def test_get_annotations_for_path_returns_parent_annotation(tmp_path):
    outputs = str(tmp_path / "outputs")
    os.makedirs(outputs)
    idx = oi.OutputsFtsIndex(outputs)
    try:
        parent = os.path.join(outputs, "run_1")
        child = os.path.join(outputs, "run_1", "deep", "file.csv")
        idx.add_annotation(parent, "parent annotation")
        annotations = idx.get_annotations_for_path(child)
        assert any("parent annotation" in a["note"] for a in annotations), annotations
    finally:
        idx.close()


def test_get_annotations_for_path_blank_returns_empty(tmp_path):
    outputs = str(tmp_path / "outputs")
    os.makedirs(outputs)
    idx = oi.OutputsFtsIndex(outputs)
    try:
        assert idx.get_annotations_for_path("") == []
        assert idx.get_annotations_for_path("   ") == []
    finally:
        idx.close()
