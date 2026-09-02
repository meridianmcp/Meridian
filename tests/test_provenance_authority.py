"""Tests for meridian.provenance_authority (PROV-CANONICAL, 7d9b8251):
authority matrix, provenance-contract capability evaluation + receipt, and
the action_audit_log / research_graph_nodes legacy classifiers.

This module is a pure-function LEAF (see its own module docstring) -- no
DB, no meridian_outputs import at runtime. Where this test file DOES import
meridian_outputs.research_evidence (off sys.path, same convention every
other cross-package test in this repo already uses), it is only to
cross-check that provenance_authority's plain-string status/kind literals
never drift from the real enum values they mirror.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from meridian import provenance_authority as PA


# ---------------------------------------------------------------------------
# Literal-vs-real-enum drift guard
# ---------------------------------------------------------------------------

class TestLiteralsMatchRealEnums:
    def test_resolver_status_literals_match_real_enum(self) -> None:
        sys.path.insert(0, str(Path(__file__).parent.parent / "extensions" / "meridian-outputs"))
        try:
            from meridian_outputs import research_evidence as RE
        except ModuleNotFoundError:
            pytest.skip("meridian_outputs not importable in this environment")
        real_values = {s.value for s in RE.ResolverStatus}
        assert PA.RESOLVER_STATUS_VALUES == real_values

    def test_evidence_kind_literals_match_real_enum(self) -> None:
        sys.path.insert(0, str(Path(__file__).parent.parent / "extensions" / "meridian-outputs"))
        try:
            from meridian_outputs import research_evidence as RE
        except ModuleNotFoundError:
            pytest.skip("meridian_outputs not importable in this environment")
        real_values = {k.value for k in RE.EvidenceKind}
        assert PA.EVIDENCE_KIND_VALUES == real_values

    def test_action_audit_event_map_only_uses_real_kind_and_status_values(self) -> None:
        for event_type, (kind, status, _rationale) in PA._ACTION_AUDIT_EVIDENCE_EVENT_MAP.items():
            assert kind in PA.EVIDENCE_KIND_VALUES, event_type
            assert status in PA.RESOLVER_STATUS_VALUES, event_type


# ---------------------------------------------------------------------------
# Authority matrix
# ---------------------------------------------------------------------------

class TestAuthorityMatrix:
    def test_matrix_is_non_empty_and_well_formed(self) -> None:
        assert len(PA.AUTHORITY_MATRIX) >= 8
        for row in PA.AUTHORITY_MATRIX:
            assert set(row) == {"question", "authoritative_system", "rationale"}
            assert row["question"].strip()
            assert row["authoritative_system"].strip()
            assert row["rationale"].strip()

    def test_questions_are_unique(self) -> None:
        questions = [row["question"] for row in PA.AUTHORITY_MATRIX]
        assert len(questions) == len(set(questions))

    def test_authoritative_system_for_exact_match(self) -> None:
        question = PA.AUTHORITY_MATRIX[0]["question"]
        expected = PA.AUTHORITY_MATRIX[0]["authoritative_system"]
        assert PA.authoritative_system_for(question) == expected

    def test_authoritative_system_for_unknown_question_is_none(self) -> None:
        assert PA.authoritative_system_for("does this exist? no.") is None

    def test_research_evidence_and_artifact_registry_both_present_and_distinct(self) -> None:
        """Direct regression guard for discovery-brief gap #1: the matrix
        must record that research_evidence and artifact_registry answer
        DIFFERENT questions (envelope representation vs. durable identity),
        not silently treat one as a superset of the other."""
        systems = {row["authoritative_system"] for row in PA.AUTHORITY_MATRIX}
        assert any("ProvenanceEnvelope" in s for s in systems)
        assert any("artifact_registry" in s for s in systems)


# ---------------------------------------------------------------------------
# Provenance-contract capability evaluation
# ---------------------------------------------------------------------------

class TestEvaluateProvenanceContractCapability:
    def test_unconfigured_project_is_executable_with_zero_behavior_change(self) -> None:
        result = PA.evaluate_provenance_contract_capability(None)
        assert result == {
            "configured": False,
            "capability_id": PA.PROVENANCE_CONTRACT_CAPABILITY_ID,
            "availability_policy": None,
            "satisfied": None,
            "tool_used": None,
            "executable": True,
            "executable_reasons": [],
        }

    def test_empty_capabilities_list_is_also_unconfigured(self) -> None:
        result = PA.evaluate_provenance_contract_capability([])
        assert result["configured"] is False
        assert result["executable"] is True

    def test_required_capability_satisfied_by_available_tool(self) -> None:
        caps = [{
            "id": PA.PROVENANCE_CONTRACT_CAPABILITY_ID,
            "purpose": "x", "required_tools": ["tool_a"], "fallback_chain": [],
            "availability_policy": "required",
        }]
        result = PA.evaluate_provenance_contract_capability(caps, available_tools={"tool_a"})
        assert result["configured"] is True
        assert result["satisfied"] is True
        assert result["tool_used"] == "tool_a"
        assert result["executable"] is True
        assert result["executable_reasons"] == []

    def test_required_capability_falls_back_in_chain_order(self) -> None:
        caps = [{
            "id": PA.PROVENANCE_CONTRACT_CAPABILITY_ID,
            "purpose": "x", "required_tools": ["tool_a"],
            "fallback_chain": ["tool_b", "tool_c"],
            "availability_policy": "required",
        }]
        result = PA.evaluate_provenance_contract_capability(caps, available_tools={"tool_c"})
        assert result["tool_used"] == "tool_c"
        assert result["executable"] is True

    def test_required_capability_unsatisfied_blocks_executable(self) -> None:
        caps = [{
            "id": PA.PROVENANCE_CONTRACT_CAPABILITY_ID,
            "purpose": "x", "required_tools": ["tool_a"], "fallback_chain": [],
            "availability_policy": "required",
        }]
        result = PA.evaluate_provenance_contract_capability(caps, available_tools=set())
        assert result["satisfied"] is False
        assert result["executable"] is False
        assert result["executable_reasons"]

    def test_no_available_tools_signal_fails_closed_for_required(self) -> None:
        caps = [{
            "id": PA.PROVENANCE_CONTRACT_CAPABILITY_ID,
            "purpose": "x", "required_tools": ["tool_a"], "fallback_chain": [],
            "availability_policy": "required",
        }]
        result = PA.evaluate_provenance_contract_capability(caps, available_tools=None)
        assert result["satisfied"] is False
        assert result["executable"] is False

    def test_optional_capability_unsatisfied_degrades_not_blocks(self) -> None:
        caps = [{
            "id": PA.PROVENANCE_CONTRACT_CAPABILITY_ID,
            "purpose": "x", "required_tools": ["tool_a"], "fallback_chain": [],
            "availability_policy": "optional",
        }]
        result = PA.evaluate_provenance_contract_capability(caps, available_tools=set())
        assert result["satisfied"] is False
        assert result["executable"] is True
        assert result["executable_reasons"]

    def test_degraded_ok_capability_unsatisfied_degrades_not_blocks(self) -> None:
        caps = [{
            "id": PA.PROVENANCE_CONTRACT_CAPABILITY_ID,
            "purpose": "x", "required_tools": ["tool_a"], "fallback_chain": [],
            "availability_policy": "degraded_ok",
        }]
        result = PA.evaluate_provenance_contract_capability(caps, available_tools=set())
        assert result["executable"] is True

    def test_unrelated_capability_in_manifest_is_ignored(self) -> None:
        caps = [{
            "id": "some_other_capability",
            "purpose": "x", "required_tools": ["tool_a"],
            "availability_policy": "required",
        }]
        result = PA.evaluate_provenance_contract_capability(caps, available_tools=set())
        assert result["configured"] is False
        assert result["executable"] is True


# ---------------------------------------------------------------------------
# build_provenance_contract_receipt
# ---------------------------------------------------------------------------

class TestBuildProvenanceContractReceipt:
    def test_shape_matches_docx_integrity_gate_convention(self) -> None:
        receipt = PA.build_provenance_contract_receipt()
        assert receipt["schema_version"] == PA.PROVENANCE_CONTRACT_SCHEMA_VERSION
        assert "executable" in receipt
        assert "executable_reasons" in receipt
        assert isinstance(receipt["executable_reasons"], list)
        assert "generated_at" in receipt

    def test_no_capabilities_no_summaries_is_executable(self) -> None:
        receipt = PA.build_provenance_contract_receipt()
        assert receipt["executable"] is True
        assert receipt["failed_record_count"] == 0
        assert receipt["envelopes_checked"] == 0

    def test_failed_records_counted_but_do_not_block_alone(self) -> None:
        summaries = [{
            "envelope_id": "e1",
            "status_counts": {"failed": 2, "pending_retry": 1, "verified": 5},
            "record_count": 8,
            "authoritative_record_count": 5,
        }]
        receipt = PA.build_provenance_contract_receipt(evidence_summaries=summaries)
        assert receipt["failed_record_count"] == 2
        assert receipt["pending_retry_record_count"] == 1
        assert receipt["envelopes_checked"] == 1
        assert receipt["executable"] is True
        assert any("FAILED" in r for r in receipt["executable_reasons"])

    def test_malformed_summary_entries_skipped_not_fatal(self) -> None:
        summaries = [None, "not-a-dict", 42, {"status_counts": None}]
        receipt = PA.build_provenance_contract_receipt(evidence_summaries=summaries)
        # only the last (a dict, even with status_counts=None) counts
        assert receipt["envelopes_checked"] == 1
        assert receipt["failed_record_count"] == 0

    def test_required_capability_unsatisfied_makes_whole_receipt_non_executable(self) -> None:
        caps = [{
            "id": PA.PROVENANCE_CONTRACT_CAPABILITY_ID,
            "purpose": "x", "required_tools": ["tool_a"], "fallback_chain": [],
            "availability_policy": "required",
        }]
        receipt = PA.build_provenance_contract_receipt(capabilities=caps, available_tools=set())
        assert receipt["executable"] is False
        assert receipt["capability"]["configured"] is True


# ---------------------------------------------------------------------------
# classify_action_audit_log_rows
# ---------------------------------------------------------------------------

class TestClassifyActionAuditLogRows:
    def test_empty_input(self) -> None:
        report = PA.classify_action_audit_log_rows(None)
        assert report["source"] == "action_audit_log"
        assert report["scanned"] == 0
        assert report["would_migrate"] == []

    def test_known_receipt_event_type_maps_to_verified_code(self) -> None:
        rows = [{"id": "a1", "event_type": "code_intel_prospect_receipt"}]
        report = PA.classify_action_audit_log_rows(rows)
        assert report["scanned"] == 1
        assert len(report["would_migrate"]) == 1
        entry = report["would_migrate"][0]
        assert entry["candidate_evidence_kind"] == "code"
        assert entry["candidate_resolver_status"] == "verified"

    def test_known_override_event_type_maps_to_degraded(self) -> None:
        rows = [{"id": "a2", "event_type": "code_intel_receipt_override"}]
        report = PA.classify_action_audit_log_rows(rows)
        assert report["would_migrate"][0]["candidate_resolver_status"] == "degraded"

    def test_governance_event_type_is_out_of_scope_not_ambiguous(self) -> None:
        rows = [
            {"id": "g1", "event_type": "cross_project_quarantine"},
            {"id": "g2", "event_type": "manual_issue_screening_enabled"},
            {"id": "g3", "event_type": "velocity_anomaly"},
        ]
        report = PA.classify_action_audit_log_rows(rows)
        assert len(report["out_of_scope"]) == 3
        assert report["would_migrate"] == []
        assert report["ambiguous"] == []

    def test_missing_id_or_event_type_skipped_unclassifiable(self) -> None:
        rows = [{"event_type": "code_intel_prospect_receipt"}, {"id": "a3"}]
        report = PA.classify_action_audit_log_rows(rows)
        assert report["scanned"] == 2
        assert len(report["skipped_unclassifiable"]) == 2
        assert report["would_migrate"] == []

    def test_non_dict_row_skipped_unclassifiable_not_fatal(self) -> None:
        report = PA.classify_action_audit_log_rows(["not-a-dict", 42])
        assert report["scanned"] == 2
        assert len(report["skipped_unclassifiable"]) == 2

    def test_mixed_batch_counts_add_up(self) -> None:
        rows = [
            {"id": "a1", "event_type": "code_intel_prospect_receipt"},
            {"id": "g1", "event_type": "cross_project_quarantine"},
            {"id": "bad"},
        ]
        report = PA.classify_action_audit_log_rows(rows)
        assert report["scanned"] == 3
        assert len(report["would_migrate"]) == 1
        assert len(report["out_of_scope"]) == 1
        assert len(report["skipped_unclassifiable"]) == 1

    def test_dry_run_flag_passed_through(self) -> None:
        report = PA.classify_action_audit_log_rows([], dry_run=False)
        assert report["dry_run"] is False


# ---------------------------------------------------------------------------
# classify_research_graph_nodes
# ---------------------------------------------------------------------------

class TestClassifyResearchGraphNodes:
    def test_empty_input(self) -> None:
        report = PA.classify_research_graph_nodes(None)
        assert report["source"] == "research_graph_nodes"
        assert report["scanned"] == 0

    def test_superseded_node_maps_to_stale_cleanly(self) -> None:
        nodes = [{"id": "n1", "identity_key": "path/to/file.py", "node_type": "code", "status": "superseded"}]
        report = PA.classify_research_graph_nodes(nodes)
        assert len(report["would_migrate"]) == 1
        entry = report["would_migrate"][0]
        assert entry["candidate_resolver_status"] == "stale"

    def test_active_node_maps_to_ambiguous_with_explicit_rationale(self) -> None:
        """Regression guard for discovery-brief gap #1/#8: an active
        research-graph row must NEVER be silently upgraded to VERIFIED."""
        nodes = [{"id": "n2", "identity_key": "path/to/file.py", "node_type": "code", "status": "active"}]
        report = PA.classify_research_graph_nodes(nodes)
        entry = report["would_migrate"][0]
        assert entry["candidate_resolver_status"] == "ambiguous"
        assert "no independent confidence signal" in entry["rationale"]

    def test_unrecognized_status_is_ambiguous(self) -> None:
        nodes = [{"id": "n3", "identity_key": "x", "node_type": "code", "status": "quarantined"}]
        report = PA.classify_research_graph_nodes(nodes)
        assert report["would_migrate"] == []
        assert len(report["ambiguous"]) == 1

    def test_missing_required_field_skipped_unclassifiable(self) -> None:
        nodes = [{"id": "n4", "node_type": "code", "status": "active"}]  # no identity_key
        report = PA.classify_research_graph_nodes(nodes)
        assert len(report["skipped_unclassifiable"]) == 1

    def test_out_of_scope_always_empty(self) -> None:
        nodes = [{"id": "n5", "identity_key": "x", "node_type": "code", "status": "active"}]
        report = PA.classify_research_graph_nodes(nodes)
        assert report["out_of_scope"] == []


# ---------------------------------------------------------------------------
# classify_legacy_provenance_sources (combined wrapper)
# ---------------------------------------------------------------------------

class TestClassifyLegacyProvenanceSources:
    def test_both_omitted_returns_empty_but_valid_report(self) -> None:
        report = PA.classify_legacy_provenance_sources()
        assert report["total_scanned"] == 0
        assert report["action_audit_log"]["scanned"] == 0
        assert report["research_graph_nodes"]["scanned"] == 0
        assert report["schema_version"] == PA.AUTHORITY_MATRIX_SCHEMA_VERSION

    def test_only_one_source_supplied_still_valid(self) -> None:
        report = PA.classify_legacy_provenance_sources(
            action_audit_log_rows=[{"id": "a1", "event_type": "code_intel_prospect_receipt"}],
        )
        assert report["total_scanned"] == 1
        assert report["research_graph_nodes"]["scanned"] == 0

    def test_total_scanned_sums_both_sources(self) -> None:
        report = PA.classify_legacy_provenance_sources(
            action_audit_log_rows=[{"id": "a1", "event_type": "velocity_anomaly"}],
            research_graph_nodes=[
                {"id": "n1", "identity_key": "x", "node_type": "code", "status": "active"},
                {"id": "n2", "identity_key": "y", "node_type": "code", "status": "superseded"},
            ],
        )
        assert report["total_scanned"] == 3

    def test_dry_run_propagates_to_both_sub_reports(self) -> None:
        report = PA.classify_legacy_provenance_sources(dry_run=False)
        assert report["action_audit_log"]["dry_run"] is False
        assert report["research_graph_nodes"]["dry_run"] is False
