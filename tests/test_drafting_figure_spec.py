"""draft-figure-spec-schema: figure spec validation, source rules, width bounds, stable hash."""
from __future__ import annotations

import hashlib
import json
import math

import pytest

from meridian.drafting.figure_spec import (
    DEFAULT_WIDTH_IN,
    MAX_WIDTH_IN,
    FigureSpecError,
    figure_spec_hash,
    validate_figure_spec,
)


def _plot_spec(**overrides):
    spec = {
        "kind": "plot",
        "id": "loss-curve",
        "caption": "Training loss over epochs.",
        "alt_text": "Line chart of loss falling from 2.1 to 0.3.",
        "label": "fig:loss-curve",
        "source": "figures/loss.csv",
    }
    spec.update(overrides)
    return spec


def _diagram_spec(**overrides):
    spec = {
        "kind": "diagram",
        "id": "pipeline-overview",
        "caption": "The drafting pipeline.",
        "alt_text": "Three boxes connected left to right.",
        "label": "fig:pipeline",
        "source": "graph LR\n  A --> B\n  B --> C",
    }
    spec.update(overrides)
    return spec


# --- valid specs -------------------------------------------------------------


def test_valid_plot_spec_validates_and_defaults_width():
    out = validate_figure_spec(_plot_spec())
    assert out["kind"] == "plot"
    assert out["source"] == "figures/loss.csv"
    assert out["width_in"] == DEFAULT_WIDTH_IN


def test_valid_diagram_spec_with_inline_text_validates():
    out = validate_figure_spec(_diagram_spec())
    assert out["kind"] == "diagram"
    assert "A --> B" in out["source"]


def test_diagram_may_use_a_relative_data_path():
    out = validate_figure_spec(_diagram_spec(source="figures/pipeline.mmd"))
    assert out["source"] == "figures/pipeline.mmd"


def test_validate_does_not_mutate_input():
    spec = _plot_spec()
    before = dict(spec)
    validate_figure_spec(spec)
    assert spec == before
    assert "width_in" not in spec


def test_default_width_constant_is_single_column():
    assert DEFAULT_WIDTH_IN == 6.5
    assert MAX_WIDTH_IN == 6.5


# --- kind / id / label / text fields ----------------------------------------


@pytest.mark.parametrize("kind", ["table", "PLOT", "", None])
def test_kind_must_be_plot_or_diagram(kind):
    with pytest.raises(FigureSpecError, match="kind"):
        validate_figure_spec(_plot_spec(kind=kind))


@pytest.mark.parametrize("fig_id", ["Loss-Curve", "loss_curve", "-loss", "loss-", "loss--curve", "", "   "])
def test_id_must_be_kebab_case(fig_id):
    with pytest.raises(FigureSpecError):
        validate_figure_spec(_plot_spec(id=fig_id))


@pytest.mark.parametrize("label", ["table:loss", "loss-curve", "fig:", "fig:   ", ""])
def test_label_must_start_with_fig_prefix_and_name(label):
    with pytest.raises(FigureSpecError, match="label"):
        validate_figure_spec(_plot_spec(label=label))


@pytest.mark.parametrize("field", ["caption", "alt_text"])
def test_caption_and_alt_text_must_be_non_empty(field):
    with pytest.raises(FigureSpecError, match=field):
        validate_figure_spec(_plot_spec(**{field: "   "}))
    spec = _plot_spec()
    del spec[field]
    with pytest.raises(FigureSpecError, match=field):
        validate_figure_spec(spec)


def test_missing_source_is_rejected():
    spec = _plot_spec()
    del spec["source"]
    with pytest.raises(FigureSpecError, match="source"):
        validate_figure_spec(spec)


def test_non_mapping_is_rejected():
    with pytest.raises(FigureSpecError, match="mapping"):
        validate_figure_spec(["not", "a", "spec"])  # type: ignore[arg-type]


# --- source rules: absolute and traversal paths ------------------------------


@pytest.mark.parametrize(
    "source",
    [
        "/etc/passwd",
        "\\\\server\\share\\loss.csv",
        "C:\\data\\loss.csv",
        "c:/data/loss.csv",
        "~/loss.csv",
    ],
)
def test_absolute_paths_are_rejected(source):
    with pytest.raises(FigureSpecError, match="absolute"):
        validate_figure_spec(_plot_spec(source=source))
    with pytest.raises(FigureSpecError, match="absolute"):
        validate_figure_spec(_diagram_spec(source=source))


@pytest.mark.parametrize(
    "source",
    [
        "../secret.csv",
        "figures/../../secret.csv",
        "..\\secret.csv",
        "figures/..",
        "..",
    ],
)
def test_traversal_paths_are_rejected(source):
    with pytest.raises(FigureSpecError, match=r"\.\."):
        validate_figure_spec(_plot_spec(source=source))


def test_plot_rejects_inline_multiline_source():
    with pytest.raises(FigureSpecError, match="only a diagram"):
        validate_figure_spec(_plot_spec(source="graph LR\n  A --> B"))


# --- width bounds ------------------------------------------------------------


@pytest.mark.parametrize("width", [6.5, 6, 3, 0.5, 1e-6])
def test_width_within_bounds_is_accepted(width):
    out = validate_figure_spec(_plot_spec(width_in=width))
    assert out["width_in"] == float(width)


@pytest.mark.parametrize("width", [0, 0.0, -1, -6.5, 6.6, 7, 100])
def test_width_outside_bounds_is_rejected(width):
    with pytest.raises(FigureSpecError, match="width_in"):
        validate_figure_spec(_plot_spec(width_in=width))


@pytest.mark.parametrize("width", [True, False, "6.5", None, [6.5]])
def test_width_must_be_a_real_number(width):
    with pytest.raises(FigureSpecError, match="width_in"):
        validate_figure_spec(_plot_spec(width_in=width))


@pytest.mark.parametrize("width", [math.nan, math.inf, -math.inf])
def test_non_finite_width_is_rejected(width):
    with pytest.raises(FigureSpecError, match="width_in"):
        validate_figure_spec(_plot_spec(width_in=width))


# --- spec_hash ---------------------------------------------------------------


def test_hash_is_sha256_hex_of_canonical_json():
    spec = validate_figure_spec(_plot_spec())
    canonical = json.dumps(spec, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert figure_spec_hash(_plot_spec()) == hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert len(figure_spec_hash(_plot_spec())) == 64


def test_hash_is_stable_under_key_reordering():
    spec = _plot_spec()
    reordered = dict(reversed(list(spec.items())))
    assert list(reordered) != list(spec)
    assert figure_spec_hash(spec) == figure_spec_hash(reordered)


def test_omitted_width_hashes_same_as_explicit_default():
    assert figure_spec_hash(_plot_spec()) == figure_spec_hash(_plot_spec(width_in=DEFAULT_WIDTH_IN))
    assert figure_spec_hash(_plot_spec(width_in=6)) == figure_spec_hash(_plot_spec(width_in=6.0))


def test_hash_changes_when_content_changes():
    assert figure_spec_hash(_plot_spec()) != figure_spec_hash(_plot_spec(caption="Different caption."))
    assert figure_spec_hash(_plot_spec()) != figure_spec_hash(_plot_spec(width_in=4.0))


def test_spec_hash_field_is_excluded_from_its_own_digest():
    base = figure_spec_hash(_plot_spec())
    with_field = dict(_plot_spec(), spec_hash=base)
    assert figure_spec_hash(with_field) == base


def test_hash_rejects_invalid_spec():
    with pytest.raises(FigureSpecError):
        figure_spec_hash(_plot_spec(source="/abs/path.csv"))


def test_hash_rejects_non_json_encodable_extra_field():
    with pytest.raises(FigureSpecError, match="JSON"):
        figure_spec_hash(_plot_spec(extra={"nested": object()}))
