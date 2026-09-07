"""Unit tests for ``meridian.fallbacks.check_docx_promotion_evidence``
(ba0af0a4, DOCS-R2-B).

Pure-function tests only -- no .docx, no filesystem, no doc_store.py
involvement. Covers every signal in isolation (stage/canonical/observed
hash, render, provenance, convergence), the tri-state verdict's priority
order (a contradiction always wins over a degraded signal), the
"missing/blank required evidence is itself a contradiction" rule, and the
type-validation contract (never raises for malformed evidence VALUES --
they become a reported PROMOTION_CONTRADICTORY verdict -- but raises
TypeError for a malformed argument TYPE).

The doc_store.py wiring (update_paragraph / merge_paragraph_draft actually
calling this and reacting to its verdict) is covered separately in
tests/test_ba0af0a4_docx_promotion_wiring.py.
"""
from __future__ import annotations

import pytest

from meridian import fallbacks


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_matching_hashes_no_optional_evidence_is_verified():
    result = fallbacks.check_docx_promotion_evidence(
        "/tmp/doc.docx", "stage-hash-1", "canonical-hash-1", "canonical-hash-1",
    )
    assert result["verdict"] == fallbacks.PROMOTION_VERIFIED
    assert result["contradictions"] == []
    assert result["degraded_reasons"] == []
    assert result["reasons"] == []
    assert result["schema_version"] == fallbacks.PROMOTION_SCHEMA_VERSION
    assert result["docx_path"] == "/tmp/doc.docx"


def test_render_rendered_status_stays_verified():
    result = fallbacks.check_docx_promotion_evidence(
        "/tmp/doc.docx", "s1", "c1", "c1",
        render={"status": fallbacks.RENDERED, "backend": "soffice"},
    )
    assert result["verdict"] == fallbacks.PROMOTION_VERIFIED
    assert result["render"] == {"status": fallbacks.RENDERED, "backend": "soffice"}


def test_clean_provenance_and_convergence_stay_verified():
    result = fallbacks.check_docx_promotion_evidence(
        "/tmp/doc.docx", "s1", "c1", "c1",
        provenance={
            "provenance_type": "exact", "inconclusive": False,
            "output_sha256": "c1",
        },
        convergence={"inconclusive": False, "degraded": False},
    )
    assert result["verdict"] == fallbacks.PROMOTION_VERIFIED


# ---------------------------------------------------------------------------
# Contradictions -- the core "post-promotion hash" check this item is named for.
# ---------------------------------------------------------------------------


def test_observed_hash_mismatch_is_contradictory():
    """The literal check the item names: a promotion's own canonical hash
    disagrees with a fresh re-read of the file's current on-disk bytes --
    e.g. a different writer promoted in between."""
    result = fallbacks.check_docx_promotion_evidence(
        "/tmp/doc.docx", "s1", "canonical-hash-A", "observed-hash-B",
    )
    assert result["verdict"] == fallbacks.PROMOTION_CONTRADICTORY
    assert any("does not match canonical_hash" in r for r in result["contradictions"])
    assert result["degraded_reasons"] == []


@pytest.mark.parametrize("stage_hash", [None, "", "   ", 123, {}])
def test_blank_or_wrong_type_stage_hash_is_contradictory(stage_hash):
    result = fallbacks.check_docx_promotion_evidence(
        "/tmp/doc.docx", stage_hash, "c1", "c1",
    )
    assert result["verdict"] == fallbacks.PROMOTION_CONTRADICTORY
    assert any("stage_hash" in r for r in result["contradictions"])


@pytest.mark.parametrize("canonical_hash", [None, "", "  "])
def test_blank_canonical_hash_is_contradictory(canonical_hash):
    result = fallbacks.check_docx_promotion_evidence(
        "/tmp/doc.docx", "s1", canonical_hash, "o1",
    )
    assert result["verdict"] == fallbacks.PROMOTION_CONTRADICTORY
    assert any("canonical_hash" in r for r in result["contradictions"])


@pytest.mark.parametrize("observed_hash", [None, "", "  "])
def test_blank_observed_hash_is_contradictory(observed_hash):
    result = fallbacks.check_docx_promotion_evidence(
        "/tmp/doc.docx", "s1", "c1", observed_hash,
    )
    assert result["verdict"] == fallbacks.PROMOTION_CONTRADICTORY
    assert any("observed_hash" in r for r in result["contradictions"])


def test_unrecognized_render_status_is_contradictory_not_silently_accepted():
    result = fallbacks.check_docx_promotion_evidence(
        "/tmp/doc.docx", "s1", "c1", "c1",
        render={"status": "some-made-up-status"},
    )
    assert result["verdict"] == fallbacks.PROMOTION_CONTRADICTORY
    assert any("unrecognized status" in r for r in result["contradictions"])


def test_provenance_hash_disagreement_is_contradictory():
    """Two independently-computed hashes of the SAME promoted artifact
    disagreeing is a genuine cross-signal contradiction, distinct from the
    observed_hash check (which compares against the promoting writer's own
    fingerprint, not a second independent computation)."""
    result = fallbacks.check_docx_promotion_evidence(
        "/tmp/doc.docx", "s1", "canonical-hash", "canonical-hash",
        provenance={"provenance_type": "exact", "inconclusive": False,
                    "output_sha256": "a-totally-different-hash"},
    )
    assert result["verdict"] == fallbacks.PROMOTION_CONTRADICTORY
    assert any(
        "output_sha256" in r and "does not match canonical_hash" in r
        for r in result["contradictions"]
    )


def test_contradiction_always_wins_over_a_simultaneous_degraded_signal():
    """A hash mismatch (contradiction) plus a failed render (degraded) must
    report CONTRADICTORY, never DEGRADED -- contradictions are never masked
    by, or averaged with, a lesser degraded signal."""
    result = fallbacks.check_docx_promotion_evidence(
        "/tmp/doc.docx", "s1", "canonical-A", "observed-B",
        render={"status": fallbacks.FAILED, "reason": "soffice crashed"},
    )
    assert result["verdict"] == fallbacks.PROMOTION_CONTRADICTORY
    assert result["contradictions"]
    assert result["degraded_reasons"]  # both recorded, but verdict is contradictory


# ---------------------------------------------------------------------------
# Degraded (inconclusive/unavailable) -- never escalated to a contradiction
# on its own, and never silently treated as verified either.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [fallbacks.FAILED, fallbacks.UNAVAILABLE_WITH_REASON])
def test_render_not_rendered_is_degraded_matching_update_paragraphs_existing_contract(status):
    """Preserves update_paragraph's pre-existing, tested contract exactly:
    both FAILED and UNAVAILABLE_WITH_REASON are the SAME severity tier
    (downgradable via allow_degraded_render), never auto-escalated to a
    hard contradiction -- update_paragraph's own 50+ existing callers and
    tests/test_docx_word_com_regression.py depend on this."""
    result = fallbacks.check_docx_promotion_evidence(
        "/tmp/doc.docx", "s1", "c1", "c1",
        render={"status": status, "reason": "no backend"},
    )
    assert result["verdict"] == fallbacks.PROMOTION_DEGRADED
    assert result["contradictions"] == []
    assert result["degraded_reasons"]


def test_provenance_inconclusive_is_degraded_never_treated_as_confirmed_absent():
    """Mirrors output_provenance_gate.py's own documented contract: an
    unconverged scan is never proof of a problem, but also never silently
    folded into VERIFIED."""
    result = fallbacks.check_docx_promotion_evidence(
        "/tmp/doc.docx", "s1", "c1", "c1",
        provenance={"provenance_type": "unknown", "inconclusive": True},
    )
    assert result["verdict"] == fallbacks.PROMOTION_DEGRADED
    assert any("inconclusive" in r for r in result["degraded_reasons"])


@pytest.mark.parametrize(
    "prov_type", ["unregistered", "unknown", "stale_by_script"],
)
def test_degraded_provenance_types_when_conclusive(prov_type):
    result = fallbacks.check_docx_promotion_evidence(
        "/tmp/doc.docx", "s1", "c1", "c1",
        provenance={"provenance_type": prov_type, "inconclusive": False},
    )
    assert result["verdict"] == fallbacks.PROMOTION_DEGRADED


def test_provenance_exact_and_directory_fallback_are_clean():
    for prov_type in ("exact", "directory_fallback"):
        result = fallbacks.check_docx_promotion_evidence(
            "/tmp/doc.docx", "s1", "c1", "c1",
            provenance={"provenance_type": prov_type, "inconclusive": False},
        )
        assert result["verdict"] == fallbacks.PROMOTION_VERIFIED, prov_type


def test_convergence_inconclusive_is_degraded():
    result = fallbacks.check_docx_promotion_evidence(
        "/tmp/doc.docx", "s1", "c1", "c1",
        convergence={"inconclusive": True, "degraded": True},
    )
    assert result["verdict"] == fallbacks.PROMOTION_DEGRADED
    assert any("inconclusive" in r for r in result["degraded_reasons"])


def test_convergence_degraded_without_inconclusive_is_degraded():
    result = fallbacks.check_docx_promotion_evidence(
        "/tmp/doc.docx", "s1", "c1", "c1",
        convergence={"inconclusive": False, "degraded": True, "partial_index": True, "pending_count": 3},
    )
    assert result["verdict"] == fallbacks.PROMOTION_DEGRADED
    assert any("degraded state" in r for r in result["degraded_reasons"])


def test_convergence_clean_is_verified():
    result = fallbacks.check_docx_promotion_evidence(
        "/tmp/doc.docx", "s1", "c1", "c1",
        convergence={"inconclusive": False, "degraded": False},
    )
    assert result["verdict"] == fallbacks.PROMOTION_VERIFIED


# ---------------------------------------------------------------------------
# Type-validation contract: TypeError only for a malformed ARGUMENT TYPE,
# never for malformed evidence CONTENT.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_path", [None, "", "   ", 123])
def test_bad_docx_path_type_raises_type_error(bad_path):
    with pytest.raises(TypeError):
        fallbacks.check_docx_promotion_evidence(bad_path, "s1", "c1", "c1")


@pytest.mark.parametrize("field", ["render", "provenance", "convergence"])
def test_non_mapping_optional_evidence_raises_type_error(field):
    with pytest.raises(TypeError):
        fallbacks.check_docx_promotion_evidence(
            "/tmp/doc.docx", "s1", "c1", "c1", **{field: "not-a-mapping"},
        )


def test_never_raises_for_malformed_evidence_values_only_for_types():
    """A non-string hash (e.g. an int, or a dict) is malformed CONTENT --
    reported as a contradiction, never a crash."""
    result = fallbacks.check_docx_promotion_evidence(
        "/tmp/doc.docx", 42, ["not", "a", "string"], None,
    )
    assert result["verdict"] == fallbacks.PROMOTION_CONTRADICTORY
    assert len(result["contradictions"]) >= 2


def test_result_is_json_shaped_and_defensively_copies_mappings():
    """render/provenance/convergence are echoed back as plain dict copies,
    not the caller's original mapping object, so a caller mutating its own
    input afterward cannot retroactively change an already-returned verdict
    dict."""
    render_in = {"status": fallbacks.RENDERED}
    result = fallbacks.check_docx_promotion_evidence(
        "/tmp/doc.docx", "s1", "c1", "c1", render=render_in,
    )
    render_in["status"] = fallbacks.FAILED
    assert result["render"]["status"] == fallbacks.RENDERED
