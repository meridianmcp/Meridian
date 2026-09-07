"""Acceptance tests for tools/meridian_fallbacks/figure_invariant_gate.py
(sprint item db63385b, "W31-B").

Covers, per that item's acceptance criteria, one fixture for each of the
five explicit verdicts plus JSON round-tripping:

  - Typography-only diff (style/layout changed, numeric+text identical,
    same bound source)                              -> INVARIANT_HOLDS
  - Numeric-content decoy (same bound source, numbers differ)
                                                      -> INVARIANT_VIOLATION
  - Text-content decoy (same bound source, text differs) -- and,
    symmetrically, a candidate with a MATCHING caption but a DIFFERENT
    bound source must still fail, never pass on loose label matching
                                                      -> INVARIANT_VIOLATION
                                                         / SOURCE_MISMATCH
  - Bound-source mismatch decoy (different resolved source entirely)
                                                      -> SOURCE_MISMATCH
  - Ambiguous/unresolvable candidate (maps onto AMBIGUOUS/RELOCATED-style
    provenance with multiple candidates)             -> AMBIGUOUS
  - Explicit no-generator/hold state (no resolvable source at all), its
    own distinct state, never conflated with AMBIGUOUS or a crash
                                                      -> NO_GENERATOR
  - JSON round-trip of the verdict dict and of FigureSlotPayload itself.

No canonical thesis document is touched anywhere in this file -- every
fixture is a small, disposable, synthetic payload built in-memory.
"""
from __future__ import annotations

import json

import pytest

from tools.meridian_fallbacks import figure_invariant_gate as fig_gate
from tools.meridian_fallbacks import output_provenance_gate as OPG
from tools.meridian_fallbacks.figure_invariant_gate import (
    AMBIGUOUS,
    INVARIANT_HOLDS,
    INVARIANT_VIOLATION,
    NO_GENERATOR,
    SOURCE_MISMATCH,
    FigureSlotPayload,
    compare_figure_invariants,
)


def _canonical_payload(**overrides) -> FigureSlotPayload:
    base = dict(
        bound_source={"kind": "rid", "value": "rId50"},
        numeric_values=(1.0, 2.0, 3.0),
        text_content=("Convergence", "plot"),
        typography={"font": "Calibri", "size": 11, "alignment": "center"},
        caption_text="Figure 21. Convergence plot for the primary model.",
        provenance_type=fig_gate.PROVENANCE_EXACT,
    )
    base.update(overrides)
    return FigureSlotPayload(**base)


# ---------------------------------------------------------------------------
# 1. INVARIANT_HOLDS -- typography-only diff.
# ---------------------------------------------------------------------------

class TestInvariantHolds:
    def test_typography_only_diff_holds(self):
        canonical = _canonical_payload()
        candidate = _canonical_payload(
            typography={"font": "Times New Roman", "size": 10, "alignment": "left"},
        )
        result = compare_figure_invariants(canonical, candidate)

        assert result["verdict"] == INVARIANT_HOLDS
        assert result["numeric_diff"]["changed"] is False
        assert result["text_diff"]["changed"] is False
        # Typography differences ARE recorded, for observability...
        assert set(result["typography_diff"]["changed_keys"]) == {"font", "size", "alignment"}
        # ...but never gate the verdict.
        assert result["verdict"] == INVARIANT_HOLDS

    def test_byte_identical_slots_hold(self):
        payload = _canonical_payload()
        result = compare_figure_invariants(payload, payload)
        assert result["verdict"] == INVARIANT_HOLDS
        assert result["reasons"]

    def test_holds_regardless_of_caption_text_difference(self):
        """A pure caption-wording edit (no identity, numeric, or text-content
        change) must still hold -- caption_text is carried for audit only."""
        canonical = _canonical_payload()
        candidate = _canonical_payload(caption_text="Fig. 21 -- convergence (revised wording).")
        result = compare_figure_invariants(canonical, candidate)
        assert result["verdict"] == INVARIANT_HOLDS


# ---------------------------------------------------------------------------
# 2. INVARIANT_VIOLATION -- numeric-content decoy.
# ---------------------------------------------------------------------------

class TestNumericContentDecoy:
    def test_numeric_values_differ_same_source_is_violation(self):
        canonical = _canonical_payload()
        candidate = _canonical_payload(numeric_values=(1.0, 2.0, 3.5))
        result = compare_figure_invariants(canonical, candidate)

        assert result["verdict"] == INVARIANT_VIOLATION
        assert result["numeric_diff"]["changed"] is True
        assert result["text_diff"]["changed"] is False
        assert any("numeric" in r for r in result["reasons"])

    def test_numeric_reorder_without_value_change_is_still_reported_changed(self):
        canonical = _canonical_payload(numeric_values=(1.0, 2.0, 3.0))
        candidate = _canonical_payload(numeric_values=(3.0, 2.0, 1.0))
        result = compare_figure_invariants(canonical, candidate)

        assert result["verdict"] == INVARIANT_VIOLATION
        assert result["numeric_diff"]["changed"] is True
        assert result["numeric_diff"]["reordered"] is True
        assert result["numeric_diff"]["added"] == []
        assert result["numeric_diff"]["removed"] == []


# ---------------------------------------------------------------------------
# 3. INVARIANT_VIOLATION / SOURCE_MISMATCH -- text-content decoy, including
#    the "matching caption but different bound source" loose-label trap.
# ---------------------------------------------------------------------------

class TestTextContentDecoy:
    def test_text_content_differs_same_source_is_violation(self):
        canonical = _canonical_payload()
        candidate = _canonical_payload(text_content=("Divergence", "plot"))
        result = compare_figure_invariants(canonical, candidate)

        assert result["verdict"] == INVARIANT_VIOLATION
        assert result["text_diff"]["changed"] is True
        assert "Convergence" in result["text_diff"]["removed"] or "Divergence" in result["text_diff"]["added"]

    def test_matching_caption_but_different_bound_source_still_fails(self):
        """The loose-label trap: a candidate that copies the CANONICAL's
        exact caption text but resolves to a genuinely different figure
        must never be accepted just because the label matches."""
        canonical = _canonical_payload()
        decoy = _canonical_payload(
            bound_source={"kind": "rid", "value": "rId28"},
            caption_text=canonical.caption_text,  # identical label, different figure
        )
        result = compare_figure_invariants(canonical, decoy)

        assert result["verdict"] == SOURCE_MISMATCH
        assert result["verdict"] != INVARIANT_HOLDS
        assert canonical.caption_text == decoy.caption_text  # precondition: labels really do match


# ---------------------------------------------------------------------------
# 4. SOURCE_MISMATCH -- bound-source mismatch decoy.
# ---------------------------------------------------------------------------

class TestSourceMismatchDecoy:
    def test_different_rid_is_source_mismatch(self):
        canonical = _canonical_payload()
        candidate = _canonical_payload(bound_source={"kind": "rid", "value": "rId51"})
        result = compare_figure_invariants(canonical, candidate)

        assert result["verdict"] == SOURCE_MISMATCH
        assert result["canonical_bound_source"] != result["candidate_bound_source"]

    def test_different_kind_same_value_is_source_mismatch(self):
        """A resolved_path and a generating_script that happen to share a
        string value are NOT the same identity -- kind is part of the key."""
        canonical = _canonical_payload(bound_source={"kind": "resolved_path", "value": "outputs/fig.csv"})
        candidate = _canonical_payload(bound_source={"kind": "generating_script", "value": "outputs/fig.csv"})
        result = compare_figure_invariants(canonical, candidate)
        assert result["verdict"] == SOURCE_MISMATCH

    def test_relocated_provenance_is_source_mismatch_not_a_false_hold(self):
        """A candidate resolved only via content-hash relocation (see
        provenance_status.py's RELOCATED) is deliberately NOT treated as an
        equivalent match, even when bound_source and content happen to line
        up exactly -- this is the rule that would otherwise let a relocated
        candidate slip through as a false INVARIANT_HOLDS."""
        canonical = _canonical_payload(bound_source={"kind": "resolved_path", "value": "outputs/run1/fig.csv"})
        candidate = _canonical_payload(
            bound_source={"kind": "resolved_path", "value": "outputs/run1/fig.csv"},
            provenance_type=fig_gate.PROVENANCE_RELOCATED,
            provenance_candidates=({"path": "outputs/run1/fig.csv"},),
        )
        result = compare_figure_invariants(canonical, candidate)
        assert result["verdict"] == SOURCE_MISMATCH
        assert result["verdict"] != INVARIANT_HOLDS


# ---------------------------------------------------------------------------
# 5. AMBIGUOUS -- fails closed, never silently accepted.
# ---------------------------------------------------------------------------

class TestAmbiguous:
    def test_ambiguous_provenance_type_fails_closed(self):
        canonical = _canonical_payload()
        candidate = _canonical_payload(
            bound_source={"kind": None, "value": None},
            provenance_type=fig_gate.PROVENANCE_AMBIGUOUS,
            provenance_candidates=({"path": "a.csv"}, {"path": "b.csv"}),
        )
        result = compare_figure_invariants(canonical, candidate)

        assert result["verdict"] == AMBIGUOUS
        assert result["verdict"] != INVARIANT_HOLDS
        assert any("ambiguous" in r.lower() for r in result["reasons"])

    def test_explicit_ambiguous_bound_source_kind_fails_closed(self):
        """A caller that already knows it has a tie (independent of any
        outputs-provenance lookup) can mark bound_source kind="ambiguous"
        directly."""
        canonical = _canonical_payload()
        candidate = _canonical_payload(bound_source={"kind": "ambiguous", "value": None})
        result = compare_figure_invariants(canonical, candidate)
        assert result["verdict"] == AMBIGUOUS

    def test_relocated_with_multiple_candidates_is_treated_as_ambiguous(self):
        """RELOCATED's own contract (provenance_status.py) is exactly ONE
        hash match; a caller that mislabels a multi-candidate result as
        RELOCATED anyway is treated defensively as AMBIGUOUS rather than
        trusting the (inconsistent) single-match claim."""
        canonical = _canonical_payload()
        candidate = _canonical_payload(
            provenance_type=fig_gate.PROVENANCE_RELOCATED,
            provenance_candidates=({"path": "a.csv"}, {"path": "b.csv"}),
        )
        result = compare_figure_invariants(canonical, candidate)
        assert result["verdict"] == AMBIGUOUS

    def test_ambiguous_on_canonical_side_also_fails_closed(self):
        canonical = _canonical_payload(
            bound_source={"kind": None, "value": None},
            provenance_type=fig_gate.PROVENANCE_AMBIGUOUS,
            provenance_candidates=({"path": "a.csv"}, {"path": "b.csv"}),
        )
        candidate = _canonical_payload()
        result = compare_figure_invariants(canonical, candidate)
        assert result["verdict"] == AMBIGUOUS


# ---------------------------------------------------------------------------
# 6. NO_GENERATOR -- its own explicit state, never a crash, never AMBIGUOUS.
# ---------------------------------------------------------------------------

class TestNoGenerator:
    def test_candidate_with_no_source_at_all_is_no_generator(self):
        canonical = _canonical_payload()
        candidate = _canonical_payload(
            bound_source={"kind": None, "value": None},
            provenance_type=None,
        )
        result = compare_figure_invariants(canonical, candidate)

        assert result["verdict"] == NO_GENERATOR
        assert result["verdict"] != AMBIGUOUS

    def test_unknown_provenance_type_with_no_bound_source_is_no_generator(self):
        canonical = _canonical_payload()
        candidate = _canonical_payload(
            bound_source={"kind": None, "value": None},
            provenance_type=fig_gate.PROVENANCE_UNKNOWN,
        )
        result = compare_figure_invariants(canonical, candidate)
        assert result["verdict"] == NO_GENERATOR

    def test_unregistered_provenance_type_with_no_bound_source_is_no_generator(self):
        canonical = _canonical_payload()
        candidate = _canonical_payload(
            bound_source={"kind": None, "value": None},
            provenance_type=fig_gate.PROVENANCE_UNREGISTERED,
        )
        result = compare_figure_invariants(canonical, candidate)
        assert result["verdict"] == NO_GENERATOR

    def test_canonical_with_no_source_is_also_no_generator(self):
        canonical = _canonical_payload(bound_source={"kind": None, "value": None}, provenance_type=None)
        candidate = _canonical_payload()
        result = compare_figure_invariants(canonical, candidate)
        assert result["verdict"] == NO_GENERATOR

    def test_no_generator_never_raises(self):
        """Two completely empty payloads must produce the explicit
        NO_GENERATOR state, not an exception."""
        result = compare_figure_invariants(FigureSlotPayload(), FigureSlotPayload())
        assert result["verdict"] == NO_GENERATOR


# ---------------------------------------------------------------------------
# 7. JSON round-tripping -- every "receipt" in this package must survive
#    json.dumps/json.loads with no data loss.
# ---------------------------------------------------------------------------

class TestJSONRoundTrip:
    @pytest.mark.parametrize("canonical,candidate", [
        (_canonical_payload(), _canonical_payload(typography={"font": "Arial"})),
        (_canonical_payload(), _canonical_payload(numeric_values=(9.9,))),
        (_canonical_payload(), _canonical_payload(bound_source={"kind": "rid", "value": "rId99"})),
        (
            _canonical_payload(),
            _canonical_payload(bound_source={"kind": None, "value": None}, provenance_type=fig_gate.PROVENANCE_AMBIGUOUS),
        ),
        (_canonical_payload(), FigureSlotPayload()),
    ])
    def test_verdict_dict_round_trips_through_json_with_no_loss(self, canonical, candidate):
        result = compare_figure_invariants(canonical, candidate)
        text = json.dumps(result)
        assert json.loads(text) == result

    def test_figure_slot_payload_round_trips_through_dict(self):
        payload = _canonical_payload()
        reconstructed = FigureSlotPayload.from_dict(payload.to_dict())
        assert reconstructed.to_dict() == payload.to_dict()

    def test_figure_slot_payload_from_dict_tolerates_missing_keys(self):
        payload = FigureSlotPayload.from_dict({})
        assert payload.bound_source == {"kind": None, "value": None}
        assert payload.numeric_values == ()
        assert payload.text_content == ()
        assert payload.provenance_candidates == ()

    def test_figure_slot_payload_from_dict_partial_keys(self):
        payload = FigureSlotPayload.from_dict({"numeric_values": [1, 2, 3]})
        assert payload.numeric_values == (1, 2, 3)
        assert payload.bound_source == {"kind": None, "value": None}


# ---------------------------------------------------------------------------
# 8. Input coercion -- accepts a plain mapping as well as a FigureSlotPayload,
#    and rejects a genuinely unusable type rather than silently misbehaving.
# ---------------------------------------------------------------------------

class TestInputCoercion:
    def test_plain_dict_payloads_are_accepted_directly(self):
        canonical = {
            "bound_source": {"kind": "rid", "value": "rId50"},
            "numeric_values": [1, 2],
            "text_content": ["a", "b"],
        }
        candidate = {
            "bound_source": {"kind": "rid", "value": "rId50"},
            "numeric_values": [1, 2],
            "text_content": ["a", "b"],
        }
        result = compare_figure_invariants(canonical, candidate)
        assert result["verdict"] == INVARIANT_HOLDS

    def test_mixed_payload_and_dict_arguments_are_accepted(self):
        canonical = _canonical_payload()
        candidate = canonical.to_dict()
        result = compare_figure_invariants(canonical, candidate)
        assert result["verdict"] == INVARIANT_HOLDS

    @pytest.mark.parametrize("bad", [None, 42, "not-a-payload", ["also", "not"]])
    def test_unusable_argument_type_raises_type_error(self, bad):
        with pytest.raises(TypeError):
            compare_figure_invariants(bad, _canonical_payload())
        with pytest.raises(TypeError):
            compare_figure_invariants(_canonical_payload(), bad)


# ---------------------------------------------------------------------------
# 9. Priority order -- ambiguous/relocated/no-generator signals must win
#    even when bound_source values would otherwise coincidentally agree.
# ---------------------------------------------------------------------------

class TestPriorityOrder:
    def test_ambiguous_wins_over_matching_bound_source(self):
        canonical = _canonical_payload()
        candidate = _canonical_payload(
            provenance_type=fig_gate.PROVENANCE_AMBIGUOUS,
            provenance_candidates=({"path": "a"}, {"path": "b"}),
        )
        # bound_source matches exactly, numeric/text match exactly -- an
        # implementation that checked source-equality FIRST would wrongly
        # report INVARIANT_HOLDS here.
        result = compare_figure_invariants(canonical, candidate)
        assert result["verdict"] == AMBIGUOUS

    def test_no_generator_wins_over_ambiguous_when_truly_empty(self):
        """An entirely empty candidate (no bound source, no provenance
        signal whatsoever) must resolve to NO_GENERATOR, not be
        misclassified as AMBIGUOUS just because identity is unknown."""
        canonical = _canonical_payload()
        candidate = FigureSlotPayload()
        result = compare_figure_invariants(canonical, candidate)
        assert result["verdict"] == NO_GENERATOR


# ---------------------------------------------------------------------------
# 10. Manifest / status-vocabulary parity with this package's own
#     output_provenance_gate.py (both mirror the same five pre-existing
#     provenance_status.py constants).
# ---------------------------------------------------------------------------

class TestProvenanceVocabularyParity:
    @pytest.mark.parametrize("name", [
        "EXACT", "UNREGISTERED", "UNKNOWN", "STALE_BY_SCRIPT",
    ])
    def test_shared_provenance_constants_match_sibling_gate(self, name):
        assert getattr(fig_gate, f"PROVENANCE_{name}") == getattr(OPG, name)

    def test_directory_fallback_constant_matches_sibling_gate(self):
        assert fig_gate.PROVENANCE_DIRECTORY_FALLBACK == OPG.DIRECTORY_FALLBACK

    def test_all_verdicts_are_distinct_strings(self):
        assert len(set(fig_gate.FIGURE_INVARIANT_VERDICTS)) == len(fig_gate.FIGURE_INVARIANT_VERDICTS)
