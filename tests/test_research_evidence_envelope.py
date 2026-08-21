"""Tests for meridian_outputs.research_evidence (sprint item 0ea8fd3c).

Covers:
  - The full typed evidence model: EvidenceKind (all 11 required kinds),
    ResolverStatus (all 6 required states), EvidenceHash, EvidenceRevision,
    EvidenceTimestamps, ResolverState, EvidenceIdentity, EvidenceRecord,
    EvidenceLink, ProvenanceEnvelope.
  - Validation: confidence bounds, invalid kind/status strings, partial
    records/links/envelopes requiring a partial_reason, duplicate record
    ids -- all raise EnvelopeValidationError, never a bare KeyError/
    ValueError escaping the module.
  - Lossless round-trip through JSON AND through XML
    (parse(serialize(env)) == env for both formats), including a partial
    record/link and a dangling link endpoint.
  - Malformed JSON/XML payloads raise EnvelopeValidationError.
  - to_markdown() never presents a partial/unresolved record or link as
    though it were fully verified -- it always carries a visible caveat --
    while a fully verified, non-partial record/link gets none.
  - compute_content_hash / make_hash consistency with hashlib directly.
  - build_envelope's ergonomic auto-id/auto-timestamp construction.

Note (session 19477436-02d7-439f-a1dd-110b03e616f5, 2026-08-21): this item's
touches_resources also lists provenance_status.py/outputs_local.py/
handoff.py for a planned bridge (converting get_provenance_status() output
into a typed EvidenceRecord). That integration was deliberately NOT added in
this pass -- meridian/handoff.py stayed locked by a live sibling session
(research-os-resilience-investigation) through 6 retry attempts, and per
this repo's lock-contention protocol no file outside a landed claim gets
edited. research_evidence.py itself has zero dependency on any of those
three files, so it is fully self-contained and testable on its own; the
provenance_status bridge is left for a follow-up pass once the lock clears.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "extensions" / "meridian-outputs"))

from meridian_outputs import research_evidence as RE


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _verified_resolver(confidence: float = 0.95) -> RE.ResolverState:
    return RE.ResolverState(
        status=RE.ResolverStatus.VERIFIED,
        confidence=confidence,
        resolved_at="2026-08-21T00:00:00+00:00",
        resolver="unit-test",
        reason="matched exactly",
    )


def _timestamps() -> RE.EvidenceTimestamps:
    return RE.EvidenceTimestamps(
        observed_at="2026-08-20T00:00:00+00:00",
        updated_at="2026-08-21T00:00:00+00:00",
        revisions=[RE.EvidenceRevision(revision_id="rev-1", created_at="2026-08-20T00:00:00+00:00", note="first seen")],
    )


def _identity(kind: RE.EvidenceKind, rec_id: str) -> RE.EvidenceIdentity:
    return RE.EvidenceIdentity(
        id=rec_id,
        kind=kind,
        locator=f"file:///outputs/{rec_id}.csv",
        label=f"label for {rec_id}",
        external_ids={"doi": f"10.1234/{rec_id}"},
    )


def _record(kind: RE.EvidenceKind, rec_id: str, *, partial: bool = False, partial_reason=None,
            resolver: "RE.ResolverState | None" = None) -> RE.EvidenceRecord:
    return RE.EvidenceRecord(
        identity=_identity(kind, rec_id),
        timestamps=_timestamps(),
        resolver=resolver or _verified_resolver(),
        hashes=[RE.make_hash("some content", fingerprint="shape:v1")],
        partial=partial,
        partial_reason=partial_reason,
        attributes={"note": "unit test record"},
    )


# ---------------------------------------------------------------------------
# EvidenceKind / ResolverStatus coverage
# ---------------------------------------------------------------------------

class TestEnums:
    def test_all_required_evidence_kinds_present(self):
        required = {
            "claim", "source", "citation", "dataset", "code", "run",
            "output", "figure", "table", "document", "review",
        }
        assert {k.value for k in RE.EvidenceKind} == required

    def test_all_required_resolver_statuses_present(self):
        required = {"verified", "stale", "held", "ambiguous", "unavailable", "degraded"}
        assert {s.value for s in RE.ResolverStatus} == required


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class TestValidation:
    def test_confidence_out_of_range_rejected(self):
        with pytest.raises(RE.EnvelopeValidationError):
            RE.ResolverState(status=RE.ResolverStatus.VERIFIED, confidence=1.5)
        with pytest.raises(RE.EnvelopeValidationError):
            RE.ResolverState(status=RE.ResolverStatus.VERIFIED, confidence=-0.1)

    def test_invalid_status_string_rejected(self):
        with pytest.raises(RE.EnvelopeValidationError):
            RE.ResolverState(status="not_a_real_status", confidence=0.5)

    def test_invalid_kind_string_rejected(self):
        with pytest.raises(RE.EnvelopeValidationError):
            RE.EvidenceIdentity(id="x", kind="not_a_real_kind", locator="file:///x")

    def test_partial_record_requires_reason(self):
        with pytest.raises(RE.EnvelopeValidationError):
            _record(RE.EvidenceKind.OUTPUT, "r1", partial=True, partial_reason=None)

    def test_partial_link_requires_reason(self):
        with pytest.raises(RE.EnvelopeValidationError):
            RE.EvidenceLink(
                id="l1", relation="cites", source_id="a", target_id="b",
                resolver=_verified_resolver(), partial=True, partial_reason=None,
            )

    def test_duplicate_record_ids_rejected(self):
        rec = _record(RE.EvidenceKind.CLAIM, "dup")
        with pytest.raises(RE.EnvelopeValidationError):
            RE.build_envelope(records=[rec, rec])

    def test_partial_envelope_requires_reason(self):
        with pytest.raises(RE.EnvelopeValidationError):
            RE.ProvenanceEnvelope(
                envelope_id="e1", generated_at="2026-08-21T00:00:00+00:00", partial=True,
            )

    def test_unsupported_format_rejected(self):
        env = RE.build_envelope(envelope_id="e1", generated_at="2026-08-21T00:00:00+00:00")
        with pytest.raises(RE.EnvelopeValidationError):
            RE.serialize_provenance_envelope(env, format="yaml")
        with pytest.raises(RE.EnvelopeValidationError):
            RE.parse_provenance_envelope("{}", format="yaml")

    def test_malformed_json_rejected_as_envelope_error(self):
        with pytest.raises(RE.EnvelopeValidationError):
            RE.parse_provenance_envelope("{not valid json", format="json")

    def test_malformed_xml_rejected_as_envelope_error(self):
        with pytest.raises(RE.EnvelopeValidationError):
            RE.parse_provenance_envelope("<not><closed>", format="xml")

    def test_json_missing_required_key_rejected(self):
        with pytest.raises(RE.EnvelopeValidationError):
            RE.parse_provenance_envelope('{"version": "1.0"}', format="json")


# ---------------------------------------------------------------------------
# Round-trip: JSON and XML
# ---------------------------------------------------------------------------

class TestRoundTrip:
    def _sample_envelope(self) -> RE.ProvenanceEnvelope:
        claim = _record(RE.EvidenceKind.CLAIM, "claim-1")
        source = _record(
            RE.EvidenceKind.SOURCE, "source-1", partial=True,
            partial_reason="DOI resolution still pending",
            resolver=RE.ResolverState(status=RE.ResolverStatus.AMBIGUOUS, confidence=0.4),
        )
        figure = _record(RE.EvidenceKind.FIGURE, "figure-1")
        link_ok = RE.EvidenceLink(
            id="link-1", relation="cites", source_id="claim-1", target_id="source-1",
            resolver=_verified_resolver(), note="direct citation",
        )
        link_dangling = RE.EvidenceLink(
            id="link-2", relation="produced_by", source_id="figure-1", target_id="run-not-in-envelope",
            resolver=RE.ResolverState(status=RE.ResolverStatus.UNAVAILABLE, confidence=0.0),
            partial=True, partial_reason="generating run record not yet ingested",
        )
        return RE.build_envelope(
            records=[claim, source, figure],
            links=[link_ok, link_dangling],
            envelope_id="env-fixed-1",
            generated_at="2026-08-21T00:00:00+00:00",
        )

    def test_json_round_trip_equal(self):
        env = self._sample_envelope()
        payload = RE.serialize_provenance_envelope(env, format="json")
        restored = RE.parse_provenance_envelope(payload, format="json")
        assert restored == env

    def test_xml_round_trip_equal(self):
        env = self._sample_envelope()
        payload = RE.serialize_provenance_envelope(env, format="xml")
        restored = RE.parse_provenance_envelope(payload, format="xml")
        assert restored == env

    def test_json_and_xml_carry_the_same_canonical_data(self):
        env = self._sample_envelope()
        from_json = RE.parse_provenance_envelope(
            RE.serialize_provenance_envelope(env, format="json"), format="json"
        )
        from_xml = RE.parse_provenance_envelope(
            RE.serialize_provenance_envelope(env, format="xml"), format="xml"
        )
        assert from_json == from_xml == env

    def test_json_serialization_is_deterministic(self):
        env = self._sample_envelope()
        first = RE.serialize_provenance_envelope(env, format="json")
        second = RE.serialize_provenance_envelope(env, format="xml")
        # Re-serializing the same envelope object always yields identical output.
        assert RE.serialize_provenance_envelope(env, format="json") == first
        assert RE.serialize_provenance_envelope(env, format="xml") == second

    def test_dangling_link_endpoint_detected_but_not_fatal(self):
        env = self._sample_envelope()
        dangling = env.dangling_link_endpoints()
        assert dangling == ["link-2"]
        # Constructing/serializing an envelope with a dangling, explicitly
        # partial/unavailable link edge must never raise -- that's the whole
        # point of allowing partial edges.
        RE.serialize_provenance_envelope(env, format="json")


# ---------------------------------------------------------------------------
# Markdown projection: partial/unresolved records must never look authoritative
# ---------------------------------------------------------------------------

class TestMarkdownProjection:
    def test_verified_non_partial_record_has_no_caveat(self):
        rec = _record(RE.EvidenceKind.CLAIM, "clean-claim")
        env = RE.build_envelope(records=[rec], envelope_id="e", generated_at="t")
        md = env.to_markdown()
        line = [l for l in md.splitlines() if "clean-claim" in l][0]
        assert "PARTIAL" not in line
        assert "AMBIGUOUS" not in line
        assert "UNAVAILABLE" not in line

    def test_partial_record_carries_visible_caveat(self):
        rec = _record(
            RE.EvidenceKind.SOURCE, "shaky-source", partial=True,
            partial_reason="not yet cross-checked",
            resolver=RE.ResolverState(status=RE.ResolverStatus.AMBIGUOUS, confidence=0.3),
        )
        env = RE.build_envelope(records=[rec], envelope_id="e", generated_at="t")
        md = env.to_markdown()
        line = [l for l in md.splitlines() if "shaky-source" in l][0]
        assert "PARTIAL" in line
        assert "AMBIGUOUS" in line
        assert "not yet cross-checked" in line

    def test_non_verified_but_non_partial_record_still_carries_status_caveat(self):
        rec = _record(
            RE.EvidenceKind.OUTPUT, "stale-output",
            resolver=RE.ResolverState(status=RE.ResolverStatus.STALE, confidence=0.6),
        )
        env = RE.build_envelope(records=[rec], envelope_id="e", generated_at="t")
        md = env.to_markdown()
        line = [l for l in md.splitlines() if "stale-output" in l][0]
        assert "STALE" in line

    def test_markdown_header_declares_itself_a_projection(self):
        env = RE.build_envelope(envelope_id="e", generated_at="t")
        md = env.to_markdown()
        assert "projection" in md.lower()
        assert "never parsed back" in md.lower()

    def test_partial_envelope_flagged_in_markdown(self):
        env = RE.build_envelope(
            envelope_id="e", generated_at="t", partial=True,
            partial_reason="index still converging",
        )
        md = env.to_markdown()
        assert "PARTIAL ENVELOPE" in md
        assert "index still converging" in md

    def test_link_caveats_mirror_record_caveats(self):
        a = _record(RE.EvidenceKind.CLAIM, "a")
        b = _record(RE.EvidenceKind.SOURCE, "b")
        link = RE.EvidenceLink(
            id="l", relation="cites", source_id="a", target_id="b",
            resolver=RE.ResolverState(status=RE.ResolverStatus.HELD, confidence=0.2),
            partial=True, partial_reason="under legal hold",
        )
        env = RE.build_envelope(records=[a, b], links=[link], envelope_id="e", generated_at="t")
        md = env.to_markdown()
        line = [l for l in md.splitlines() if "--[cites]-->" in l][0]
        assert "HELD" in line
        assert "PARTIAL" in line
        assert "under legal hold" in line


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------

class TestHashing:
    def test_compute_content_hash_matches_hashlib_directly(self):
        content = "hello evidence envelope"
        expected = hashlib.sha256(content.encode("utf-8")).hexdigest()
        assert RE.compute_content_hash(content) == expected

    def test_compute_content_hash_accepts_bytes(self):
        content = b"\x00\x01raw-bytes"
        expected = hashlib.sha256(content).hexdigest()
        assert RE.compute_content_hash(content) == expected

    def test_make_hash_wraps_algorithm_and_fingerprint(self):
        h = RE.make_hash("content", fingerprint="shape:v2")
        assert h.algorithm == "sha256"
        assert h.value == hashlib.sha256(b"content").hexdigest()
        assert h.fingerprint == "shape:v2"

    def test_unsupported_algorithm_raises_envelope_error(self):
        with pytest.raises(RE.EnvelopeValidationError):
            RE.compute_content_hash("x", algorithm="not-a-real-algo")


# ---------------------------------------------------------------------------
# build_envelope ergonomics
# ---------------------------------------------------------------------------

class TestBuildEnvelope:
    def test_auto_generates_id_and_timestamp(self):
        env = RE.build_envelope()
        assert env.envelope_id
        assert env.generated_at
        assert env.records == []
        assert env.links == []

    def test_is_authoritative_properties(self):
        good = _record(RE.EvidenceKind.CLAIM, "good")
        bad = _record(
            RE.EvidenceKind.CLAIM, "bad", partial=True, partial_reason="incomplete",
            resolver=RE.ResolverState(status=RE.ResolverStatus.DEGRADED, confidence=0.1),
        )
        assert good.is_authoritative is True
        assert bad.is_authoritative is False
