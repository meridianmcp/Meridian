"""MDE-5 -- lossless research evidence envelopes, edge-complete.

Extends tests/test_research_evidence_envelope.py (0ea8fd3c) with the NEW
surface this item adds on top of the already-lossless JSON/XML codec:

  - Unknown top-level fields on a record/link/envelope survive a JSON AND an
    XML round trip verbatim (never silently dropped).
  - Explicit redaction state (redacted/redaction_reason), mirroring
    partial/partial_reason exactly, including the is_authoritative
    interaction.
  - XML namespace support on the root element -- round-trips through both a
    namespaced AND a legacy non-namespaced payload.
  - canonical_envelope_hash: order-independent content addressing.
  - merge_envelopes: "one partial resolver cannot erase other evidence."
  - evidence_status_summary / trusted_pointers: the bounded projections a
    handoff manifest embeds.
  - Deterministic IDs: build_envelope/EvidenceRecord ids never depend on
    Python's randomized hash() builtin.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "extensions" / "meridian-outputs"))

from meridian_outputs import research_evidence as RE  # noqa: E402


def _resolver(status=RE.ResolverStatus.VERIFIED, confidence=1.0):
    return RE.ResolverState(status=status, confidence=confidence)


def _identity(rid, kind=RE.EvidenceKind.CLAIM, locator="doc://x"):
    return RE.EvidenceIdentity(id=rid, kind=kind, locator=locator)


def _timestamps(observed="2026-01-01T00:00:00Z", updated="2026-01-01T00:00:00Z"):
    return RE.EvidenceTimestamps(observed_at=observed, updated_at=updated)


def _record(rid, *, status=RE.ResolverStatus.VERIFIED, partial=False,
            partial_reason=None, redacted=False, redaction_reason=None,
            updated="2026-01-01T00:00:00Z"):
    return RE.EvidenceRecord(
        identity=_identity(rid),
        timestamps=_timestamps(updated=updated),
        resolver=_resolver(status=status),
        partial=partial, partial_reason=partial_reason,
        redacted=redacted, redaction_reason=redaction_reason,
    )


# ---------------------------------------------------------------------------
# Unknown-field preservation
# ---------------------------------------------------------------------------

class TestUnknownFieldPreservation:
    def test_record_unknown_top_level_field_round_trips_json(self):
        d = RE.envelope_to_dict(RE.build_envelope(
            [_record("r1")], envelope_id="e1", generated_at="2026-01-01T00:00:00Z",
        ))
        d["records"][0]["future_field_v2"] = {"nested": [1, 2, 3]}
        env = RE.envelope_from_dict(d)
        assert env.records[0].extra_fields == {"future_field_v2": {"nested": [1, 2, 3]}}
        round_tripped = RE.envelope_to_dict(env)
        assert round_tripped["records"][0]["future_field_v2"] == {"nested": [1, 2, 3]}

    def test_record_unknown_field_round_trips_xml(self):
        d = RE.envelope_to_dict(RE.build_envelope(
            [_record("r1")], envelope_id="e1", generated_at="2026-01-01T00:00:00Z",
        ))
        d["records"][0]["vendor_extension"] = "some-value"
        env = RE.envelope_from_dict(d)
        xml = RE.serialize_provenance_envelope(env, "xml")
        back = RE.parse_provenance_envelope(xml, "xml")
        assert back.records[0].extra_fields.get("vendor_extension") == "some-value"
        assert back == env

    def test_link_unknown_field_round_trips(self):
        env = RE.build_envelope(
            [_record("r1"), _record("r2")],
            [RE.EvidenceLink(id="l1", relation="cites", source_id="r1", target_id="r2",
                              resolver=_resolver())],
            envelope_id="e1", generated_at="2026-01-01T00:00:00Z",
        )
        d = RE.envelope_to_dict(env)
        d["links"][0]["confidence_note"] = "manually reviewed"
        env2 = RE.envelope_from_dict(d)
        assert env2.links[0].extra_fields == {"confidence_note": "manually reviewed"}
        assert RE.envelope_to_dict(env2)["links"][0]["confidence_note"] == "manually reviewed"

    def test_envelope_level_unknown_field_round_trips(self):
        d = RE.envelope_to_dict(RE.build_envelope(
            envelope_id="e1", generated_at="2026-01-01T00:00:00Z",
        ))
        d["produced_by_tool_version"] = "9.9.9"
        env = RE.envelope_from_dict(d)
        assert env.extra_fields == {"produced_by_tool_version": "9.9.9"}
        assert RE.envelope_to_dict(env)["produced_by_tool_version"] == "9.9.9"

    def test_known_fields_never_shadowed_by_extra_fields(self):
        """extra_fields is computed to EXCLUDE known keys -- a malformed
        payload can't smuggle a bogus 'id'/'envelope_id' etc. through it."""
        d = RE.envelope_to_dict(RE.build_envelope(
            [_record("r1")], envelope_id="e1", generated_at="2026-01-01T00:00:00Z",
        ))
        env = RE.envelope_from_dict(d)
        assert "envelope_id" not in env.extra_fields
        assert "records" not in env.extra_fields


# ---------------------------------------------------------------------------
# Redaction state
# ---------------------------------------------------------------------------

class TestRedaction:
    def test_redacted_record_requires_reason(self):
        with pytest.raises(RE.EnvelopeValidationError):
            _record("r1", redacted=True)

    def test_redacted_record_with_reason_is_valid(self):
        rec = _record("r1", redacted=True, redaction_reason="contains PII")
        assert rec.redacted is True
        assert rec.redaction_reason == "contains PII"

    def test_redacted_record_is_never_authoritative(self):
        rec = _record("r1", status=RE.ResolverStatus.VERIFIED,
                       redacted=True, redaction_reason="legal hold")
        assert rec.is_authoritative is False

    def test_redacted_link_requires_reason(self):
        with pytest.raises(RE.EnvelopeValidationError):
            RE.EvidenceLink(id="l1", relation="cites", source_id="a", target_id="b",
                             resolver=_resolver(), redacted=True)

    def test_to_markdown_shows_redacted_caveat_for_record(self):
        env = RE.build_envelope(
            [_record("r1", redacted=True, redaction_reason="contains PII")],
            envelope_id="e1", generated_at="2026-01-01T00:00:00Z",
        )
        md = env.to_markdown()
        assert "REDACTED" in md
        assert "contains PII" in md

    def test_to_markdown_shows_redacted_caveat_for_link(self):
        env = RE.build_envelope(
            [_record("r1"), _record("r2")],
            [RE.EvidenceLink(id="l1", relation="cites", source_id="r1", target_id="r2",
                              resolver=_resolver(), redacted=True, redaction_reason="withheld")],
            envelope_id="e1", generated_at="2026-01-01T00:00:00Z",
        )
        md = env.to_markdown()
        assert "REDACTED" in md
        assert "withheld" in md

    def test_redaction_round_trips_json_and_xml(self):
        env = RE.build_envelope(
            [_record("r1", redacted=True, redaction_reason="contains PII")],
            envelope_id="e1", generated_at="2026-01-01T00:00:00Z",
        )
        json_back = RE.parse_provenance_envelope(
            RE.serialize_provenance_envelope(env, "json"), "json",
        )
        xml_back = RE.parse_provenance_envelope(
            RE.serialize_provenance_envelope(env, "xml"), "xml",
        )
        assert json_back == env
        assert xml_back == env
        assert json_back.records[0].redacted is True


# ---------------------------------------------------------------------------
# XML namespace
# ---------------------------------------------------------------------------

class TestXmlNamespace:
    def test_serialized_xml_declares_namespace_on_root(self):
        env = RE.build_envelope(envelope_id="e1", generated_at="2026-01-01T00:00:00Z")
        xml = RE.serialize_provenance_envelope(env, "xml")
        assert f'xmlns="{RE._XML_NAMESPACE}"' in xml

    def test_namespaced_xml_round_trips(self):
        env = RE.build_envelope(
            [_record("r1")], envelope_id="e1", generated_at="2026-01-01T00:00:00Z",
        )
        xml = RE.serialize_provenance_envelope(env, "xml")
        back = RE.parse_provenance_envelope(xml, "xml")
        assert back == env

    def test_legacy_non_namespaced_xml_still_parses(self):
        """Backward compat: XML produced before the namespace was added (a
        bare <provenance_envelope type="dict"> root, no xmlns) must still
        parse correctly -- the codec is structure/attribute-driven, never
        tag-name-driven, so this must work with zero special-casing."""
        env = RE.build_envelope(
            [_record("r1")], envelope_id="e1", generated_at="2026-01-01T00:00:00Z",
        )
        xml = RE.serialize_provenance_envelope(env, "xml")
        legacy_xml = xml.replace(f' xmlns="{RE._XML_NAMESPACE}"', "")
        assert "xmlns" not in legacy_xml
        back = RE.parse_provenance_envelope(legacy_xml, "xml")
        assert back == env


# ---------------------------------------------------------------------------
# canonical_envelope_hash
# ---------------------------------------------------------------------------

class TestCanonicalEnvelopeHash:
    def test_same_records_different_construction_order_hash_equal(self):
        r1, r2 = _record("r1"), _record("r2")
        env_a = RE.build_envelope([r1, r2], envelope_id="e1", generated_at="2026-01-01T00:00:00Z")
        env_b = RE.build_envelope([r2, r1], envelope_id="e1", generated_at="2026-01-01T00:00:00Z")
        assert RE.canonical_envelope_hash(env_a) == RE.canonical_envelope_hash(env_b)

    def test_different_content_hashes_differ(self):
        env_a = RE.build_envelope([_record("r1")], envelope_id="e1", generated_at="2026-01-01T00:00:00Z")
        env_b = RE.build_envelope([_record("r2")], envelope_id="e1", generated_at="2026-01-01T00:00:00Z")
        assert RE.canonical_envelope_hash(env_a) != RE.canonical_envelope_hash(env_b)

    def test_hash_is_sha256_hex_by_default(self):
        env = RE.build_envelope(envelope_id="e1", generated_at="2026-01-01T00:00:00Z")
        h = RE.canonical_envelope_hash(env)
        assert len(h) == 64
        int(h, 16)  # raises if not valid hex

    def test_original_list_order_still_preserved_by_normal_serialize(self):
        """canonical_envelope_hash reorders its OWN working copy only --
        envelope_to_dict/serialize_provenance_envelope must still preserve
        the caller's original insertion order (no regression)."""
        r1, r2 = _record("r1"), _record("r2")
        env = RE.build_envelope([r2, r1], envelope_id="e1", generated_at="2026-01-01T00:00:00Z")
        RE.canonical_envelope_hash(env)  # exercise the reordering path
        d = RE.envelope_to_dict(env)
        assert [r["identity"]["id"] for r in d["records"]] == ["r2", "r1"]


# ---------------------------------------------------------------------------
# merge_envelopes -- "one partial resolver cannot erase other evidence"
# ---------------------------------------------------------------------------

class TestMergeEnvelopes:
    def test_authoritative_base_survives_partial_incoming(self):
        base = RE.build_envelope(
            [_record("r1", status=RE.ResolverStatus.VERIFIED)],
            envelope_id="base", generated_at="2026-01-01T00:00:00Z",
        )
        incoming = RE.build_envelope(
            [_record("r1", status=RE.ResolverStatus.UNAVAILABLE,
                      partial=True, partial_reason="resolver timed out",
                      updated="2026-06-01T00:00:00Z")],
            envelope_id="incoming", generated_at="2026-06-01T00:00:00Z",
        )
        merged = RE.merge_envelopes(base, incoming)
        assert len(merged.records) == 1
        assert merged.records[0].resolver.status is RE.ResolverStatus.VERIFIED
        assert merged.records[0].is_authoritative is True

    def test_partial_resolver_cannot_erase_verified_even_when_newer(self):
        """The core acceptance property: incoming is MORE RECENT but still
        only partial -- it must NOT win over an already-verified record."""
        base = _record("r1", status=RE.ResolverStatus.VERIFIED, updated="2020-01-01T00:00:00Z")
        incoming = _record("r1", status=RE.ResolverStatus.AMBIGUOUS, partial=True,
                            partial_reason="conflicting sources", updated="2030-01-01T00:00:00Z")
        base_env = RE.build_envelope([base], envelope_id="b", generated_at="2020-01-01T00:00:00Z")
        incoming_env = RE.build_envelope([incoming], envelope_id="i", generated_at="2030-01-01T00:00:00Z")
        merged = RE.merge_envelopes(base_env, incoming_env)
        assert merged.records[0].resolver.status is RE.ResolverStatus.VERIFIED
        assert merged.records[0].partial is False

    def test_authoritative_incoming_upgrades_partial_base(self):
        base = _record("r1", status=RE.ResolverStatus.HELD, partial=True, partial_reason="pending")
        incoming = _record("r1", status=RE.ResolverStatus.VERIFIED)
        base_env = RE.build_envelope([base], envelope_id="b", generated_at="2026-01-01T00:00:00Z")
        incoming_env = RE.build_envelope([incoming], envelope_id="i", generated_at="2026-01-01T00:00:00Z")
        merged = RE.merge_envelopes(base_env, incoming_env)
        assert merged.records[0].is_authoritative is True

    def test_both_non_authoritative_most_recent_wins(self):
        base = _record("r1", status=RE.ResolverStatus.STALE, partial=True,
                        partial_reason="old", updated="2020-01-01T00:00:00Z")
        incoming = _record("r1", status=RE.ResolverStatus.DEGRADED, partial=True,
                            partial_reason="new attempt", updated="2026-01-01T00:00:00Z")
        base_env = RE.build_envelope([base], envelope_id="b", generated_at="2020-01-01T00:00:00Z")
        incoming_env = RE.build_envelope([incoming], envelope_id="i", generated_at="2026-01-01T00:00:00Z")
        merged = RE.merge_envelopes(base_env, incoming_env)
        assert merged.records[0].resolver.status is RE.ResolverStatus.DEGRADED

    def test_ids_present_on_only_one_side_carry_through(self):
        base = RE.build_envelope([_record("only-base")], envelope_id="b", generated_at="2026-01-01T00:00:00Z")
        incoming = RE.build_envelope([_record("only-incoming")], envelope_id="i", generated_at="2026-01-01T00:00:00Z")
        merged = RE.merge_envelopes(base, incoming)
        ids = {r.identity.id for r in merged.records}
        assert ids == {"only-base", "only-incoming"}

    def test_merged_envelope_partial_when_either_side_partial(self):
        base = RE.build_envelope([_record("r1")], envelope_id="b", generated_at="2026-01-01T00:00:00Z")
        incoming = RE.build_envelope(
            [_record("r2")], envelope_id="i", generated_at="2026-01-01T00:00:00Z",
            partial=True, partial_reason="incomplete source scan",
        )
        merged = RE.merge_envelopes(base, incoming)
        assert merged.partial is True
        assert "incomplete source scan" in merged.partial_reason

    def test_merge_never_mutates_inputs(self):
        base = RE.build_envelope([_record("r1")], envelope_id="b", generated_at="2026-01-01T00:00:00Z")
        incoming = RE.build_envelope([_record("r2")], envelope_id="i", generated_at="2026-01-01T00:00:00Z")
        base_ids_before = [r.identity.id for r in base.records]
        RE.merge_envelopes(base, incoming)
        assert [r.identity.id for r in base.records] == base_ids_before
        assert len(incoming.records) == 1

    def test_link_authoritative_wins_over_partial_link(self):
        good_link = RE.EvidenceLink(id="l1", relation="cites", source_id="a", target_id="b",
                                     resolver=_resolver(status=RE.ResolverStatus.VERIFIED))
        partial_link = RE.EvidenceLink(id="l1", relation="cites", source_id="a", target_id="b",
                                        resolver=_resolver(status=RE.ResolverStatus.UNAVAILABLE),
                                        partial=True, partial_reason="target unresolved")
        base = RE.build_envelope([_record("a"), _record("b")], [good_link],
                                  envelope_id="b", generated_at="2026-01-01T00:00:00Z")
        incoming = RE.build_envelope([_record("a"), _record("b")], [partial_link],
                                      envelope_id="i", generated_at="2026-01-01T00:00:00Z")
        merged = RE.merge_envelopes(base, incoming)
        assert merged.links[0].is_authoritative is True


# ---------------------------------------------------------------------------
# evidence_status_summary / trusted_pointers
# ---------------------------------------------------------------------------

class TestEvidenceStatusSummary:
    def test_counts_by_status(self):
        env = RE.build_envelope(
            [
                _record("r1", status=RE.ResolverStatus.VERIFIED),
                _record("r2", status=RE.ResolverStatus.VERIFIED),
                _record("r3", status=RE.ResolverStatus.STALE, partial=True, partial_reason="x"),
            ],
            envelope_id="e1", generated_at="2026-01-01T00:00:00Z",
        )
        summary = RE.evidence_status_summary(env)
        assert summary["status_counts"]["verified"] == 2
        assert summary["status_counts"]["stale"] == 1
        assert summary["record_count"] == 3
        assert summary["authoritative_record_count"] == 2
        assert summary["partial_record_count"] == 1

    def test_redacted_records_counted_separately(self):
        env = RE.build_envelope(
            [_record("r1", redacted=True, redaction_reason="pii")],
            envelope_id="e1", generated_at="2026-01-01T00:00:00Z",
        )
        summary = RE.evidence_status_summary(env)
        assert summary["redacted_record_count"] == 1
        assert summary["authoritative_record_count"] == 0

    def test_empty_envelope_summary(self):
        env = RE.build_envelope(envelope_id="e1", generated_at="2026-01-01T00:00:00Z")
        summary = RE.evidence_status_summary(env)
        assert summary["record_count"] == 0
        assert all(v == 0 for v in summary["status_counts"].values())


class TestTrustedPointers:
    def test_only_authoritative_records_included(self):
        env = RE.build_envelope(
            [
                _record("r1", status=RE.ResolverStatus.VERIFIED),
                _record("r2", status=RE.ResolverStatus.STALE, partial=True, partial_reason="x"),
                _record("r3", status=RE.ResolverStatus.VERIFIED, redacted=True, redaction_reason="pii"),
            ],
            envelope_id="e1", generated_at="2026-01-01T00:00:00Z",
        )
        pointers = RE.trusted_pointers(env)
        assert [p["id"] for p in pointers] == ["r1"]

    def test_sorted_by_id(self):
        env = RE.build_envelope(
            [_record("zzz"), _record("aaa")],
            envelope_id="e1", generated_at="2026-01-01T00:00:00Z",
        )
        pointers = RE.trusted_pointers(env)
        assert [p["id"] for p in pointers] == ["aaa", "zzz"]

    def test_limit_caps_without_erroring(self):
        env = RE.build_envelope(
            [_record("a"), _record("b"), _record("c")],
            envelope_id="e1", generated_at="2026-01-01T00:00:00Z",
        )
        pointers = RE.trusted_pointers(env, limit=1)
        assert len(pointers) == 1

    def test_pointer_shape(self):
        env = RE.build_envelope(
            [RE.EvidenceRecord(
                identity=RE.EvidenceIdentity(id="r1", kind=RE.EvidenceKind.SOURCE,
                                              locator="https://example.org/x", label="Example"),
                timestamps=_timestamps(), resolver=_resolver(),
            )],
            envelope_id="e1", generated_at="2026-01-01T00:00:00Z",
        )
        pointers = RE.trusted_pointers(env)
        assert pointers == [{
            "id": "r1", "kind": "source", "locator": "https://example.org/x", "label": "Example",
        }]


# ---------------------------------------------------------------------------
# Deterministic IDs -- do not depend on Python's randomized hash()
# ---------------------------------------------------------------------------

class TestDeterministicIds:
    def test_module_never_calls_builtin_hash(self):
        """Static (AST) guard: research_evidence.py must never CALL the
        builtin hash() (PYTHONHASHSEED-randomized per process) to derive any
        id or ordering -- only hashlib (compute_content_hash/make_hash) is
        used. AST-based (not a text/regex scan) so a docstring merely
        MENTIONING ``hash()`` in prose can never produce a false positive."""
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(RE))
        offending = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "hash"
        ]
        assert offending == [], "found builtin hash() call(s) in research_evidence.py"

    def test_content_hash_is_reproducible_across_calls(self):
        h1 = RE.compute_content_hash("identical content")
        h2 = RE.compute_content_hash("identical content")
        assert h1 == h2
        assert len(h1) == 64

    def test_canonical_envelope_hash_reproducible_across_calls(self):
        env = RE.build_envelope(
            [_record("r1")], envelope_id="e1", generated_at="2026-01-01T00:00:00Z",
        )
        assert RE.canonical_envelope_hash(env) == RE.canonical_envelope_hash(env)
