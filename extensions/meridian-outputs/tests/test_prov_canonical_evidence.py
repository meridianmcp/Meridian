"""Tests for PROV-CANONICAL (7d9b8251) additions to
meridian_outputs.research_evidence:

  - ResolverStatus gains PENDING_RETRY and FAILED (purely additive to the
    prior six-value set).
  - EvidenceRecord gains scope (EvidenceScope), schema_version,
    operation_key, parent_ids -- all optional, all round-trip losslessly
    through JSON/XML exactly like every pre-existing field.

Does NOT re-test anything already covered by test_research_evidence_envelope.py
(tests/) or test_mde5_evidence_lossless.py (tests/) -- this file is scoped
to the NEW surface only.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from meridian_outputs import research_evidence as RE


def _make_identity(id_: str = "rec-1") -> RE.EvidenceIdentity:
    return RE.EvidenceIdentity(id=id_, kind=RE.EvidenceKind.CODE, locator="meridian/foo.py")


def _make_timestamps() -> RE.EvidenceTimestamps:
    return RE.EvidenceTimestamps(observed_at="2026-01-01T00:00:00+00:00", updated_at="2026-01-01T00:00:00+00:00")


class TestResolverStatusNewMembers:
    def test_pending_retry_and_failed_are_members(self) -> None:
        assert RE.ResolverStatus.PENDING_RETRY.value == "pending_retry"
        assert RE.ResolverStatus.FAILED.value == "failed"

    def test_eight_total_members(self) -> None:
        assert len(list(RE.ResolverStatus)) == 8

    def test_neither_new_member_is_authoritative(self) -> None:
        for status in (RE.ResolverStatus.PENDING_RETRY, RE.ResolverStatus.FAILED):
            state = RE.ResolverState(status=status, confidence=0.3)
            assert state.is_authoritative is False

    def test_status_counts_summary_includes_new_members(self) -> None:
        rec = RE.EvidenceRecord(
            identity=_make_identity(),
            timestamps=_make_timestamps(),
            resolver=RE.ResolverState(status=RE.ResolverStatus.FAILED, confidence=0.1),
            partial=True,
            partial_reason="terminally failed",
        )
        env = RE.build_envelope(records=[rec], envelope_id="e1", generated_at="2026-01-01T00:00:00+00:00")
        summary = RE.evidence_status_summary(env)
        assert summary["status_counts"]["failed"] == 1
        assert summary["status_counts"]["pending_retry"] == 0

    def test_resolver_state_accepts_new_status_from_raw_string(self) -> None:
        state = RE.ResolverState(status="pending_retry", confidence=0.5)
        assert state.status is RE.ResolverStatus.PENDING_RETRY


class TestEvidenceScope:
    def test_defaults_all_none(self) -> None:
        scope = RE.EvidenceScope()
        assert scope.project_id is None
        assert scope.tenant_id is None
        assert scope.subproject_id is None
        assert scope.version is None
        assert scope.sprint_item_id is None

    def test_record_defaults_to_no_scope(self) -> None:
        rec = RE.EvidenceRecord(
            identity=_make_identity(), timestamps=_make_timestamps(),
            resolver=RE.ResolverState(status=RE.ResolverStatus.VERIFIED, confidence=1.0),
        )
        assert rec.scope is None
        assert rec.schema_version == 1
        assert rec.operation_key is None
        assert rec.parent_ids == []

    def test_record_accepts_full_scope_and_new_fields(self) -> None:
        scope = RE.EvidenceScope(
            project_id="proj-1", tenant_id="tenant-1", subproject_id="sub-1",
            version="v0.3.x", sprint_item_id="7d9b8251",
        )
        rec = RE.EvidenceRecord(
            identity=_make_identity(), timestamps=_make_timestamps(),
            resolver=RE.ResolverState(status=RE.ResolverStatus.VERIFIED, confidence=1.0),
            scope=scope, schema_version=2, operation_key="op-42",
            parent_ids=["rec-0", "rec-neg1"],
        )
        assert rec.scope == scope
        assert rec.schema_version == 2
        assert rec.operation_key == "op-42"
        assert rec.parent_ids == ["rec-0", "rec-neg1"]

    def test_record_coerces_raw_dict_scope(self) -> None:
        rec = RE.EvidenceRecord(
            identity=_make_identity(), timestamps=_make_timestamps(),
            resolver=RE.ResolverState(status=RE.ResolverStatus.VERIFIED, confidence=1.0),
            scope={"project_id": "proj-1"},  # type: ignore[arg-type]
        )
        assert isinstance(rec.scope, RE.EvidenceScope)
        assert rec.scope.project_id == "proj-1"

    def test_invalid_schema_version_rejected(self) -> None:
        for bad in (0, -1, "1", 1.5, True):
            try:
                RE.EvidenceRecord(
                    identity=_make_identity(), timestamps=_make_timestamps(),
                    resolver=RE.ResolverState(status=RE.ResolverStatus.VERIFIED, confidence=1.0),
                    schema_version=bad,  # type: ignore[arg-type]
                )
            except RE.EnvelopeValidationError:
                continue
            raise AssertionError(f"schema_version={bad!r} should have been rejected")


class TestScopeAndNewFieldsRoundTrip:
    def _build_env(self) -> RE.ProvenanceEnvelope:
        scope = RE.EvidenceScope(
            project_id="proj-1", tenant_id="tenant-1", subproject_id=None,
            version="v0.3.x", sprint_item_id="7d9b8251",
        )
        rec = RE.EvidenceRecord(
            identity=_make_identity(), timestamps=_make_timestamps(),
            resolver=RE.ResolverState(status=RE.ResolverStatus.PENDING_RETRY, confidence=0.4),
            scope=scope, schema_version=3, operation_key="op-1",
            parent_ids=["parent-a", "parent-b"],
            partial=True, partial_reason="in flight",
        )
        return RE.build_envelope(records=[rec], envelope_id="e1", generated_at="2026-01-01T00:00:00+00:00")

    def test_json_round_trip_preserves_new_fields(self) -> None:
        env = self._build_env()
        payload = RE.serialize_provenance_envelope(env, format="json")
        restored = RE.parse_provenance_envelope(payload, format="json")
        assert restored == env
        rec = restored.records[0]
        assert rec.scope.project_id == "proj-1"
        assert rec.scope.tenant_id == "tenant-1"
        assert rec.scope.subproject_id is None
        assert rec.schema_version == 3
        assert rec.operation_key == "op-1"
        assert rec.parent_ids == ["parent-a", "parent-b"]

    def test_xml_round_trip_preserves_new_fields(self) -> None:
        env = self._build_env()
        payload = RE.serialize_provenance_envelope(env, format="xml")
        restored = RE.parse_provenance_envelope(payload, format="xml")
        assert restored == env
        rec = restored.records[0]
        assert rec.scope.sprint_item_id == "7d9b8251"
        assert rec.parent_ids == ["parent-a", "parent-b"]

    def test_no_scope_round_trips_as_none(self) -> None:
        rec = RE.EvidenceRecord(
            identity=_make_identity(), timestamps=_make_timestamps(),
            resolver=RE.ResolverState(status=RE.ResolverStatus.VERIFIED, confidence=1.0),
        )
        env = RE.build_envelope(records=[rec], envelope_id="e2", generated_at="2026-01-01T00:00:00+00:00")
        payload = RE.serialize_provenance_envelope(env, format="json")
        restored = RE.parse_provenance_envelope(payload, format="json")
        assert restored.records[0].scope is None
        assert restored == env

    def test_canonical_hash_stable_with_new_fields(self) -> None:
        env = self._build_env()
        h1 = RE.canonical_envelope_hash(env)
        h2 = RE.canonical_envelope_hash(self._build_env())
        assert h1 == h2

    def test_foreign_unknown_top_level_key_still_preserved_via_extra_fields(self) -> None:
        """MDE-5's extra_fields contract must still work unchanged now that
        four more real keys are known -- an UNRECOGNIZED key (not one of the
        new ones) must still round-trip via extra_fields, never dropped."""
        raw = {
            "identity": {"id": "rec-x", "kind": "code", "locator": "x.py"},
            "timestamps": {"observed_at": "2026-01-01T00:00:00+00:00", "updated_at": "2026-01-01T00:00:00+00:00"},
            "resolver": {"status": "verified", "confidence": 1.0},
            "schema_version": 1,
            "totally_unknown_future_field": {"nested": True},
        }
        rec = RE._record_from_dict(raw)  # noqa: SLF001 -- exercising the internal parse path directly
        assert rec.extra_fields == {"totally_unknown_future_field": {"nested": True}}
        back = RE._encode(rec)  # noqa: SLF001
        assert back["totally_unknown_future_field"] == {"nested": True}
        assert back["schema_version"] == 1
