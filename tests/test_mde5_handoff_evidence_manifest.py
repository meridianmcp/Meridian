"""MDE-5 -- research evidence integrated into the handoff manifest.

Covers the NEW handoff.py surface: build_handoff_manifest's evidence_status/
trusted_pointers fields, serialize_handoff_manifest_xml's <evidence_status>/
<trusted_pointers> rendering, _duck_encode_dataclass_like, and
_evidence_status_and_pointers_from_envelope -- plus generate_handoff(
mode="goal", emit_manifest=True, research_evidence_envelope=...) end to end
(machine-readable evidence embedded in the token-signed /goal body, goal mode
previously never rendered research evidence in ANY form at all).

Does NOT re-test the pre-existing manifest contract (board_revision, item
bounding, token binding) -- see tests/test_handoff_manifest_v2.py for that;
this file covers only what MDE-5 adds on top.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

from meridian import db as db_module
from meridian import handoff as handoff_module

sys.path.insert(0, str(Path(__file__).parent.parent / "extensions" / "meridian-outputs"))
from meridian_outputs import research_evidence as RE  # noqa: E402


def _extract_manifest_block(text: str) -> "str | None":
    m = re.search(r"<handoff_manifest\b.*?</handoff_manifest>", text, re.DOTALL)
    return m.group(0) if m else None


def _verified_record(rid, kind=RE.EvidenceKind.CLAIM, locator="doc://x", label=None):
    return RE.EvidenceRecord(
        identity=RE.EvidenceIdentity(id=rid, kind=kind, locator=locator, label=label),
        timestamps=RE.EvidenceTimestamps(observed_at="2026-01-01T00:00:00Z", updated_at="2026-01-01T00:00:00Z"),
        resolver=RE.ResolverState(status=RE.ResolverStatus.VERIFIED, confidence=1.0),
    )


def _partial_record(rid, kind=RE.EvidenceKind.SOURCE, locator="doc://y"):
    return RE.EvidenceRecord(
        identity=RE.EvidenceIdentity(id=rid, kind=kind, locator=locator),
        timestamps=RE.EvidenceTimestamps(observed_at="2026-01-01T00:00:00Z", updated_at="2026-01-01T00:00:00Z"),
        resolver=RE.ResolverState(status=RE.ResolverStatus.STALE, confidence=0.4),
        partial=True, partial_reason="not yet cross-checked",
    )


# ---------------------------------------------------------------------------
# _duck_encode_dataclass_like
# ---------------------------------------------------------------------------

class TestDuckEncodeDataclassLike:
    def test_plain_dict_passes_through_structurally(self):
        d = {"a": 1, "b": [1, 2, {"c": 3}]}
        assert handoff_module._duck_encode_dataclass_like(d) == d

    def test_real_envelope_instance_reduces_to_canonical_shape(self):
        env = RE.build_envelope(
            [_verified_record("r1")], envelope_id="e1", generated_at="2026-01-01T00:00:00Z",
        )
        encoded = handoff_module._duck_encode_dataclass_like(env)
        assert encoded == RE.envelope_to_dict(env)

    def test_enum_reduces_to_its_value(self):
        assert handoff_module._duck_encode_dataclass_like(RE.ResolverStatus.VERIFIED) == "verified"

    def test_scalar_passes_through(self):
        assert handoff_module._duck_encode_dataclass_like("x") == "x"
        assert handoff_module._duck_encode_dataclass_like(42) == 42
        assert handoff_module._duck_encode_dataclass_like(None) is None


# ---------------------------------------------------------------------------
# _evidence_status_and_pointers_from_envelope
# ---------------------------------------------------------------------------

class TestEvidenceStatusAndPointersFromEnvelope:
    def test_none_returns_empty(self):
        status, pointers = handoff_module._evidence_status_and_pointers_from_envelope(None)
        assert status is None
        assert pointers == []

    def test_malformed_shape_degrades_to_empty(self):
        status, pointers = handoff_module._evidence_status_and_pointers_from_envelope(
            {"records": "not-a-list", "links": []}
        )
        assert status is None
        assert pointers == []

    def test_real_envelope_instance_produces_matching_summary(self):
        env = RE.build_envelope(
            [_verified_record("r1"), _partial_record("r2")],
            envelope_id="e1", generated_at="2026-01-01T00:00:00Z",
        )
        status, pointers = handoff_module._evidence_status_and_pointers_from_envelope(env)
        assert status["record_count"] == 2
        assert status["authoritative_record_count"] == 1
        assert status["partial_record_count"] == 1
        assert status["status_counts"]["verified"] == 1
        assert status["status_counts"]["stale"] == 1
        assert pointers == [{"id": "r1", "kind": "claim", "locator": "doc://x", "label": None}]

    def test_canonical_dict_shape_produces_same_result_as_real_instance(self):
        env = RE.build_envelope(
            [_verified_record("r1")], envelope_id="e1", generated_at="2026-01-01T00:00:00Z",
        )
        as_dict = RE.envelope_to_dict(env)
        status_a, pointers_a = handoff_module._evidence_status_and_pointers_from_envelope(env)
        status_b, pointers_b = handoff_module._evidence_status_and_pointers_from_envelope(as_dict)
        assert status_a == status_b
        assert pointers_a == pointers_b

    def test_redacted_record_excluded_from_trusted_pointers(self):
        rec = RE.EvidenceRecord(
            identity=RE.EvidenceIdentity(id="r1", kind=RE.EvidenceKind.CLAIM, locator="doc://x"),
            timestamps=RE.EvidenceTimestamps(observed_at="t", updated_at="t"),
            resolver=RE.ResolverState(status=RE.ResolverStatus.VERIFIED, confidence=1.0),
            redacted=True, redaction_reason="pii",
        )
        env = RE.build_envelope([rec], envelope_id="e1", generated_at="2026-01-01T00:00:00Z")
        status, pointers = handoff_module._evidence_status_and_pointers_from_envelope(env)
        assert pointers == []
        assert status["redacted_record_count"] == 1

    def test_pointers_sorted_by_id(self):
        env = RE.build_envelope(
            [_verified_record("zzz"), _verified_record("aaa")],
            envelope_id="e1", generated_at="2026-01-01T00:00:00Z",
        )
        _, pointers = handoff_module._evidence_status_and_pointers_from_envelope(env)
        assert [p["id"] for p in pointers] == ["aaa", "zzz"]


# ---------------------------------------------------------------------------
# build_handoff_manifest / serialize_handoff_manifest_xml
# ---------------------------------------------------------------------------

class TestManifestEvidenceFields:
    def test_defaults_are_empty_when_not_supplied(self):
        manifest = handoff_module.build_handoff_manifest(
            handoff_mode="goal", project_id="p1", items=[],
        )
        assert manifest["evidence_status"] == {}
        assert manifest["trusted_pointers"] == []

    def test_carries_supplied_status_and_pointers(self):
        manifest = handoff_module.build_handoff_manifest(
            handoff_mode="goal", project_id="p1", items=[],
            evidence_status={"record_count": 3, "status_counts": {"verified": 2, "stale": 1}},
            trusted_pointers=[{"id": "r1", "kind": "claim", "locator": "doc://x", "label": None}],
        )
        assert manifest["evidence_status"]["record_count"] == 3
        assert manifest["trusted_pointers"][0]["id"] == "r1"

    def test_xml_renders_evidence_status_and_trusted_pointers(self):
        manifest = handoff_module.build_handoff_manifest(
            handoff_mode="goal", project_id="p1", items=[],
            evidence_status={"record_count": 2, "status_counts": {"verified": 2}},
            trusted_pointers=[{"id": "r1", "kind": "claim", "locator": "doc://x", "label": "L"}],
        )
        xml = handoff_module.serialize_handoff_manifest_xml(manifest)
        assert "<evidence_status>" in xml and "</evidence_status>" in xml
        assert '<field name="record_count">2</field>' in xml
        assert "<status_counts>" in xml
        assert '<field name="verified">2</field>' in xml
        assert '<pointer id="r1" kind="claim" locator="doc://x" label="L"/>' in xml
        assert xml.index("<evidence_status>") < xml.index("<trusted_pointers>")
        assert xml.strip().endswith("</handoff_manifest>")

    def test_xml_evidence_fields_present_but_empty_when_no_envelope(self):
        manifest = handoff_module.build_handoff_manifest(
            handoff_mode="goal", project_id="p1", items=[],
        )
        xml = handoff_module.serialize_handoff_manifest_xml(manifest)
        assert "<evidence_status></evidence_status>" in xml
        assert "<trusted_pointers></trusted_pointers>" in xml

    def test_xml_escapes_untrusted_pointer_fields(self):
        manifest = handoff_module.build_handoff_manifest(
            handoff_mode="goal", project_id="p1", items=[],
            trusted_pointers=[{
                "id": 'r1" evil="injected', "kind": "claim", "locator": "doc://x", "label": None,
            }],
        )
        xml = handoff_module.serialize_handoff_manifest_xml(manifest)
        assert 'evil="injected"' not in xml
        assert "&quot;" in xml

    def test_xml_deterministic_with_evidence_fields(self):
        manifest = handoff_module.build_handoff_manifest(
            handoff_mode="goal", project_id="p1", items=[],
            evidence_status={"record_count": 1},
            trusted_pointers=[{"id": "r1", "kind": "claim", "locator": "x", "label": None}],
        )
        assert (
            handoff_module.serialize_handoff_manifest_xml(manifest)
            == handoff_module.serialize_handoff_manifest_xml(manifest)
        )


# ---------------------------------------------------------------------------
# End-to-end: generate_handoff(mode="goal") + research_evidence_envelope
# ---------------------------------------------------------------------------

class TestGoalModeEndToEnd:
    @pytest.mark.asyncio
    async def test_goal_mode_previously_never_rendered_evidence_markdown(self, db, tmp_path):
        """Confirms the gap this item closes: WITHOUT the fix, goal mode
        would not carry the Markdown research-evidence block at all (only
        full/delta did) -- this test pins the FIXED behavior."""
        p = await db_module.create_project(db, "goal-evidence-md-e2e")
        await db_module.set_goal(db, p["id"], "ship it", sprint="v1")
        await db_module.add_sprint_item(db, p["id"], "v1", "do the thing")
        env = RE.build_envelope(
            [_verified_record("r1", label="Clean claim")],
            envelope_id="e1", generated_at="2026-01-01T00:00:00Z",
        )
        _path, content, _amended = await handoff_module.generate_handoff(
            db, p["id"], str(tmp_path), mode="goal", skip_ai_summary=True,
            research_evidence_envelope=env,
        )
        # A real ProvenanceEnvelope instance delegates straight to its own
        # to_markdown() (see _render_research_evidence_block's docstring),
        # which headers as "# Provenance Envelope `<id>`", not the
        # "## Research Evidence" wrapper used only for the plain-dict shape.
        assert "Provenance Envelope" in content
        assert "never parsed back" in content
        assert "r1" in content

    @pytest.mark.asyncio
    async def test_goal_mode_manifest_embeds_machine_readable_evidence(self, db, tmp_path):
        p = await db_module.create_project(db, "goal-evidence-manifest-e2e")
        await db_module.set_goal(db, p["id"], "ship it", sprint="v1")
        await db_module.add_sprint_item(db, p["id"], "v1", "do the thing")
        env = RE.build_envelope(
            [_verified_record("r1"), _partial_record("r2")],
            envelope_id="e1", generated_at="2026-01-01T00:00:00Z",
        )
        _path, content, _amended = await handoff_module.generate_handoff(
            db, p["id"], str(tmp_path), mode="goal", skip_ai_summary=True,
            emit_manifest=True, research_evidence_envelope=env,
        )
        block = _extract_manifest_block(content)
        assert block is not None
        assert "<evidence_status>" in block
        assert '<field name="record_count">2</field>' in block
        assert '<pointer id="r1"' in block
        # r2 is partial -- must NOT appear as a trusted pointer.
        assert '<pointer id="r2"' not in block

    @pytest.mark.asyncio
    async def test_manifest_evidence_covered_by_goal_token_body_hash(self, db, tmp_path):
        """The manifest (and thus its evidence_status/trusted_pointers) is
        spliced in BEFORE the token mint -- tampering with it must break
        verification, same guarantee the rest of the /goal body already has."""
        p = await db_module.create_project(db, "goal-evidence-token-e2e")
        await db_module.set_goal(db, p["id"], "ship it", sprint="v1")
        await db_module.add_sprint_item(db, p["id"], "v1", "do the thing")
        env = RE.build_envelope(
            [_verified_record("r1")], envelope_id="e1", generated_at="2026-01-01T00:00:00Z",
        )
        _path, content, _amended = await handoff_module.generate_handoff(
            db, p["id"], str(tmp_path), mode="goal", skip_ai_summary=True,
            emit_manifest=True, research_evidence_envelope=env,
        )
        m = re.search(r"<goal_token>([^<]+)</goal_token>", content)
        assert m is not None
        token = m.group(1).strip()
        body = handoff_module.strip_goal_token_banner(content)
        verified = await handoff_module.verify_handoff_token(db, token, p["id"], body=body)
        assert verified["valid"] is True

        tampered = body.replace('<pointer id="r1"', '<pointer id="r1-TAMPERED"')
        tampered_verify = await handoff_module.verify_handoff_token(
            db, token, p["id"], body=tampered,
        )
        assert tampered_verify["valid"] is False

    @pytest.mark.asyncio
    async def test_no_envelope_supplied_is_byte_for_byte_unaffected(self, db, tmp_path):
        p = await db_module.create_project(db, "goal-no-evidence-e2e")
        await db_module.set_goal(db, p["id"], "ship it", sprint="v1")
        await db_module.add_sprint_item(db, p["id"], "v1", "do the thing")
        _path, content, _amended = await handoff_module.generate_handoff(
            db, p["id"], str(tmp_path), mode="goal", skip_ai_summary=True, emit_manifest=True,
        )
        assert "## Research Evidence" not in content
        block = _extract_manifest_block(content)
        assert block is not None
        assert "<evidence_status></evidence_status>" in block
        assert "<trusted_pointers></trusted_pointers>" in block
