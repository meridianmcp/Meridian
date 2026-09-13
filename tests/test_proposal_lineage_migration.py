"""Tests for sprint item 4eedeef8 — RECONCILE: legacy proposal-predecessor
reference audit and opt-in migration, without silent inference.

Covers ``meridian.proposal_lineage_audit`` (pure classification: predecessor
detection, the proposal-lineage audit, the promotion-evidence-backlink
audit, and migration planning) and ``meridian.db.proposal_reconciliation``
(the DB-facing wiring that fetches real rows and — only for explicitly
accepted candidates — writes through ``link_proposal_lineage`` /
``link_proposal_evidence``).
"""
from __future__ import annotations

import pytest

from meridian import db as db_module
from meridian.proposal_lineage_audit import (
    RECONCILE_SCHEMA_VERSION,
    _RELATION_KEYWORD_PATTERNS,
    audit_legacy_proposal_lineage,
    audit_promotion_evidence_backlinks,
    detect_predecessor_references,
    plan_legacy_migration,
)


# ---------------------------------------------------------------------------
# Part A (pure) — detect_predecessor_references
# ---------------------------------------------------------------------------


def test_detect_real_production_example_successor_to():
    """The exact real production shape this item was filed to audit:
    proposal 1ee11ede's body opens with 'This is a successor proposal to:'
    followed by predecessor bacf5c87-ed67-4472-89ab-afb009d826f0 on the next
    line."""
    text = (
        "This is a successor proposal to:\n"
        "- bacf5c87-ed67-4472-89ab-afb009d826f0 — Meridian-build: make "
        "symbol-level parallelism visible"
    )
    found = detect_predecessor_references(text)
    assert len(found) == 1
    assert found[0]["target_id"] == "bacf5c87-ed67-4472-89ab-afb009d826f0"
    assert found[0]["relation_type"] == "supersedes"


@pytest.mark.parametrize(
    "phrase,expected_relation",
    [
        ("supersedes", "supersedes"),
        ("duplicate of", "duplicates"),
        ("responds to", "responds_to"),
        ("refines", "refines"),
        ("forked from", "forks"),
        ("continuation of", "continues"),
        ("based on", "continues"),
    ],
)
def test_detect_each_keyword_maps_to_expected_relation_type(phrase, expected_relation):
    uid = "11111111-2222-3333-4444-555555555555"
    text = f"This proposal {phrase} {uid} per the earlier investigation."
    found = detect_predecessor_references(text)
    assert len(found) == 1
    assert found[0]["target_id"] == uid
    assert found[0]["relation_type"] == expected_relation


def test_detect_bare_uuid_with_no_keyword_is_not_a_candidate():
    """A bare id mention (no lineage language nearby) is never treated as
    evidence of a predecessor relation — this is the literal 'never
    silently infer' boundary this function enforces."""
    uid = "11111111-2222-3333-4444-555555555555"
    text = f"See also {uid} for related context, unrelated to this proposal."
    assert detect_predecessor_references(text) == []


def test_detect_keyword_outside_context_window_is_not_matched():
    uid = "11111111-2222-3333-4444-555555555555"
    filler = "x" * 500
    text = f"supersedes {filler} {uid}"
    assert detect_predecessor_references(text) == []


def test_detect_multiple_uuids_each_classified_independently():
    """Two id mentions close together must not cross-contaminate: uid_b's
    relation_type must come from ITS OWN nearest keyword ('duplicate of'),
    never from uid_a's more distant 'forked from'."""
    uid_a = "11111111-1111-1111-1111-111111111111"
    uid_b = "22222222-2222-2222-2222-222222222222"
    text = f"This forked from {uid_a} and also a duplicate of {uid_b}."
    found = detect_predecessor_references(text)
    targets = {f["target_id"]: f["relation_type"] for f in found}
    assert targets == {uid_a: "forks", uid_b: "duplicates"}


def test_detect_empty_or_non_string_input_never_raises():
    assert detect_predecessor_references("") == []
    assert detect_predecessor_references(None) == []  # type: ignore[arg-type]


def test_relation_keyword_patterns_only_use_real_relation_types():
    """Cross-check against the real enum (module docstring's documented
    'kept in lockstep, cross-checked by tests' contract) — a typo'd relation
    type here would silently produce candidates link_proposal_lineage
    rejects at write time instead of failing loudly at audit time."""
    from meridian.db.proposal_lineage import VALID_RELATION_TYPES

    for _pattern, relation_type in _RELATION_KEYWORD_PATTERNS:
        assert relation_type in VALID_RELATION_TYPES


# ---------------------------------------------------------------------------
# Part A (pure) — audit_legacy_proposal_lineage
# ---------------------------------------------------------------------------


UUID1 = "11111111-1111-1111-1111-111111111111"
UUID2 = "22222222-2222-2222-2222-222222222222"
UUID3 = "33333333-3333-3333-3333-333333333333"
UUID_UNKNOWN = "ffffffff-ffff-ffff-ffff-ffffffffffff"


def _proposal(pid, body="", tags=None, tenant_id=None, **extra):
    return {"id": pid, "body": body, "tags": tags, "tenant_id": tenant_id, **extra}


def test_audit_lineage_empty_input_is_a_valid_no_op_report():
    report = audit_legacy_proposal_lineage([])
    assert report["schema_version"] == RECONCILE_SCHEMA_VERSION
    assert report["scanned"] == 0
    for bucket in (
        "would_migrate", "already_linked", "blocked_cycle", "ambiguous",
        "out_of_scope", "skipped_unclassifiable", "errors",
    ):
        assert report[bucket] == []


def test_audit_lineage_basic_candidate_is_would_migrate():
    p1 = _proposal(UUID1, body="root idea")
    p2 = _proposal(UUID2, body=f"This is a successor proposal to: {UUID1}")
    report = audit_legacy_proposal_lineage([p1, p2])
    assert len(report["would_migrate"]) == 1
    cand = report["would_migrate"][0]
    assert cand["from_proposal_id"] == UUID2
    assert cand["to_proposal_id"] == UUID1
    assert cand["relation_type"] == "supersedes"
    assert cand["candidate_key"] == f"{UUID2}:{UUID1}:supersedes"


def test_audit_lineage_self_reference_is_out_of_scope():
    p1 = _proposal(UUID1, body=f"this supersedes {UUID1}")
    report = audit_legacy_proposal_lineage([p1])
    assert report["would_migrate"] == []
    assert len(report["out_of_scope"]) == 1
    assert "self-reference" in report["out_of_scope"][0]["reason"]


def test_audit_lineage_unknown_target_id_is_out_of_scope():
    p2 = _proposal(UUID2, body=f"this supersedes {UUID_UNKNOWN}")
    report = audit_legacy_proposal_lineage([p2])
    assert report["would_migrate"] == []
    assert len(report["out_of_scope"]) == 1
    assert report["out_of_scope"][0]["target_id"] == UUID_UNKNOWN


def test_audit_lineage_already_linked_is_excluded_from_would_migrate():
    p1 = _proposal(UUID1, body="root")
    p2 = _proposal(UUID2, body=f"this supersedes {UUID1}")
    existing = {UUID2: [{"from_proposal_id": UUID2, "to_proposal_id": UUID1, "relation_type": "supersedes"}]}
    report = audit_legacy_proposal_lineage([p1, p2], existing)
    assert report["would_migrate"] == []
    assert len(report["already_linked"]) == 1


def test_audit_lineage_cross_tenant_is_ambiguous_never_would_migrate():
    p1 = _proposal(UUID1, body="root", tenant_id="tenant-a")
    p2 = _proposal(UUID2, body=f"this supersedes {UUID1}", tenant_id="tenant-b")
    report = audit_legacy_proposal_lineage([p1, p2])
    assert report["would_migrate"] == []
    assert len(report["ambiguous"]) == 1
    assert report["ambiguous"][0]["proposal_id"] == UUID2


def test_audit_lineage_cycle_via_existing_edges_is_blocked():
    p1 = _proposal(UUID1, body="root")
    p2 = _proposal(UUID2, body=f"this supersedes {UUID1}")
    # Existing structural edge p1 -> p2 already means p1 can reach p2; the
    # text-derived candidate p2 -> p1 would close a 2-cycle.
    existing = {UUID1: [{"from_proposal_id": UUID1, "to_proposal_id": UUID2, "relation_type": "refines"}]}
    report = audit_legacy_proposal_lineage([p1, p2], existing)
    assert report["would_migrate"] == []
    assert len(report["blocked_cycle"]) == 1


def test_audit_lineage_cycle_across_two_candidates_in_same_scan():
    """Neither candidate is yet written anywhere, but applying BOTH would
    close a cycle — the second one discovered must be blocked even though
    no existing_lineage_links were supplied at all."""
    p1 = _proposal(UUID1, body=f"this supersedes {UUID2}")
    p2 = _proposal(UUID2, body=f"this refines {UUID1}")
    report = audit_legacy_proposal_lineage([p1, p2])
    assert len(report["would_migrate"]) == 1
    assert len(report["blocked_cycle"]) == 1


def test_audit_lineage_duplicate_mention_is_deduplicated():
    p1 = _proposal(UUID1, body="root")
    p2 = _proposal(
        UUID2,
        body=f"this supersedes {UUID1}",
        tags=f"supersedes {UUID1} as well",
    )
    report = audit_legacy_proposal_lineage([p1, p2])
    assert len(report["would_migrate"]) == 1


def test_audit_lineage_shared_family_id_alone_is_never_a_candidate():
    """family_id is a plain compatibility grouping field — two proposals
    sharing one must NOT, by that fact alone, produce a lineage candidate
    (that would be exactly the silent inference this item forbids)."""
    p1 = _proposal(UUID1, body="root idea", family_id="fam-1")
    p2 = _proposal(UUID2, body="a later revision of the same idea", family_id="fam-1")
    report = audit_legacy_proposal_lineage([p1, p2])
    assert report["would_migrate"] == []
    assert report["scanned"] == 2


def test_audit_lineage_malformed_row_is_skipped_unclassifiable():
    report = audit_legacy_proposal_lineage([{"body": "no id here"}, "not-a-dict", None])
    assert len(report["skipped_unclassifiable"]) == 3
    assert report["errors"] == []


# ---------------------------------------------------------------------------
# plan_legacy_migration
# ---------------------------------------------------------------------------


def test_plan_migration_no_accept_plans_nothing():
    p1 = _proposal(UUID1, body="root")
    p2 = _proposal(UUID2, body=f"this supersedes {UUID1}")
    report = audit_legacy_proposal_lineage([p1, p2])
    plan = plan_legacy_migration(report, None)
    assert plan["to_apply"] == []
    assert plan["rejected_keys"] == []
    assert plan["available_candidate_keys"] == [f"{UUID2}:{UUID1}:supersedes"]


def test_plan_migration_accepts_only_named_candidate():
    p1 = _proposal(UUID1, body="root")
    p2 = _proposal(UUID2, body=f"this supersedes {UUID1}")
    p3 = _proposal(UUID3, body=f"this duplicates of {UUID1}")
    report = audit_legacy_proposal_lineage([p1, p2, p3])
    key = f"{UUID2}:{UUID1}:supersedes"
    plan = plan_legacy_migration(report, [key])
    assert [c["candidate_key"] for c in plan["to_apply"]] == [key]
    assert plan["rejected_keys"] == []


def test_plan_migration_unknown_key_is_rejected_not_silently_dropped():
    p1 = _proposal(UUID1, body="root")
    p2 = _proposal(UUID2, body=f"this supersedes {UUID1}")
    report = audit_legacy_proposal_lineage([p1, p2])
    key = f"{UUID2}:{UUID1}:supersedes"
    fabricated = "fake-from:fake-to:forks"
    plan = plan_legacy_migration(report, [key, fabricated])
    assert [c["candidate_key"] for c in plan["to_apply"]] == [key]
    assert plan["rejected_keys"] == [fabricated]


# ---------------------------------------------------------------------------
# Part B (pure) — audit_promotion_evidence_backlinks
# ---------------------------------------------------------------------------


def test_audit_evidence_not_promoted_is_never_a_candidate():
    p1 = _proposal("p1", body="raw idea")  # no promoted_to_sprint_item_id
    report = audit_promotion_evidence_backlinks([p1])
    assert report["scanned"] == 0
    assert report["would_migrate"] == []


def test_audit_evidence_missing_link_is_would_migrate():
    p1 = _proposal("p1", promoted_to_sprint_item_id="si-1", project_id="proj-1")
    report = audit_promotion_evidence_backlinks([p1])
    assert len(report["would_migrate"]) == 1
    cand = report["would_migrate"][0]
    assert cand["candidate_key"] == "p1:sprint_item:si-1"
    assert cand["entity_id"] == "si-1"


def test_audit_evidence_existing_link_is_already_linked():
    p1 = _proposal("p1", promoted_to_sprint_item_id="si-1", project_id="proj-1")
    existing = {"p1": [{"entity_type": "sprint_item", "entity_id": "si-1"}]}
    report = audit_promotion_evidence_backlinks([p1], existing)
    assert report["would_migrate"] == []
    assert len(report["already_linked"]) == 1


def test_audit_evidence_missing_project_id_is_out_of_scope():
    p1 = _proposal("p1", promoted_to_sprint_item_id="si-1", project_id=None)
    report = audit_promotion_evidence_backlinks([p1])
    assert report["would_migrate"] == []
    assert len(report["out_of_scope"]) == 1


# ---------------------------------------------------------------------------
# DB integration — meridian.db.proposal_reconciliation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_legacy_proposal_lineage_no_op_on_empty_board(db):
    report = await db_module.audit_legacy_proposal_lineage(db)
    assert report["scanned"] == 0
    assert report["would_migrate"] == []
    assert report["proposal_scan_truncated"] is False


@pytest.mark.asyncio
async def test_migrate_legacy_proposal_lineage_end_to_end(db):
    p1 = await db_module.add_workspace_proposal(db, "Root idea", "the original idea")
    p2 = await db_module.add_workspace_proposal(
        db, "Better idea",
        f"This is a successor proposal to:\n- {p1['id']} — the original idea",
    )

    report = await db_module.audit_legacy_proposal_lineage(db)
    keys = [c["candidate_key"] for c in report["would_migrate"]]
    expected_key = f"{p2['id']}:{p1['id']}:supersedes"
    assert expected_key in keys

    # dry_run=True (the default) must never write, even with a matching accept.
    dry = await db_module.migrate_legacy_proposal_lineage(
        db, accept=[expected_key],
    )
    assert dry["dry_run"] is True
    assert dry["applied"] == []
    links_before = await db_module.get_proposal_lineage_links(db, p2["id"])
    assert links_before == []

    # dry_run=False + explicit accept actually migrates it.
    result = await db_module.migrate_legacy_proposal_lineage(
        db, accept=[expected_key], dry_run=False,
    )
    assert len(result["applied"]) == 1
    assert result["errors"] == []
    links_after = await db_module.get_proposal_lineage_links(db, p2["id"])
    assert len(links_after) == 1
    assert links_after[0]["from_proposal_id"] == p2["id"]
    assert links_after[0]["to_proposal_id"] == p1["id"]
    assert links_after[0]["relation_type"] == "supersedes"

    # Idempotent retry: a fresh audit no longer sees this as would_migrate
    # (it's already_linked now), so re-accepting the SAME key is rejected —
    # never silently re-applied, never a duplicate row.
    report2 = await db_module.audit_legacy_proposal_lineage(db)
    assert expected_key not in [c["candidate_key"] for c in report2["would_migrate"]]
    replay = await db_module.migrate_legacy_proposal_lineage(
        db, accept=[expected_key], dry_run=False,
    )
    assert replay["applied"] == []
    assert replay["rejected_keys"] == [expected_key]
    links_replay = await db_module.get_proposal_lineage_links(db, p2["id"])
    assert len(links_replay) == 1  # still exactly one row, no duplicate


@pytest.mark.asyncio
async def test_migrate_legacy_proposal_lineage_no_accept_is_a_no_op(db):
    p1 = await db_module.add_workspace_proposal(db, "Root idea", "root")
    await db_module.add_workspace_proposal(
        db, "Better idea", f"this supersedes {p1['id']}",
    )
    result = await db_module.migrate_legacy_proposal_lineage(db, dry_run=False)
    assert result["applied"] == []
    assert result["would_apply"] == []


@pytest.mark.asyncio
async def test_migrate_legacy_proposal_lineage_rejects_fabricated_key(db):
    p1 = await db_module.add_workspace_proposal(db, "Root idea", "root")
    await db_module.add_workspace_proposal(
        db, "Better idea", f"this supersedes {p1['id']}",
    )
    fabricated = "not-a-real-candidate:also-fake:supersedes"
    result = await db_module.migrate_legacy_proposal_lineage(
        db, accept=[fabricated], dry_run=False,
    )
    assert result["applied"] == []
    assert result["rejected_keys"] == [fabricated]


@pytest.mark.asyncio
async def test_audit_legacy_proposal_lineage_cross_tenant_never_migrated(db):
    p1 = await db_module.add_workspace_proposal(
        db, "Root idea", "root", tenant_id="tenant-a",
    )
    p2 = await db_module.add_workspace_proposal(
        db, "Better idea", f"this supersedes {p1['id']}", tenant_id="tenant-b",
    )
    report = await db_module.audit_legacy_proposal_lineage(db)
    keys = [c["candidate_key"] for c in report["would_migrate"]]
    assert f"{p2['id']}:{p1['id']}:supersedes" not in keys
    ambiguous_ids = [a["proposal_id"] for a in report["ambiguous"]]
    assert p2["id"] in ambiguous_ids


@pytest.mark.asyncio
async def test_audit_legacy_proposal_lineage_blocks_real_cycle(db):
    p2 = await db_module.add_workspace_proposal(db, "P2", "plain")
    p1 = await db_module.add_workspace_proposal(
        db, "P1", f"this supersedes {p2['id']}",
    )
    # Real structural edge the OTHER direction already exists.
    await db_module.link_proposal_lineage(db, p2["id"], p1["id"], "supersedes")

    report = await db_module.audit_legacy_proposal_lineage(db)
    keys = [c["candidate_key"] for c in report["would_migrate"]]
    assert f"{p1['id']}:{p2['id']}:supersedes" not in keys
    cycle_from = [c["proposal_id"] for c in report["blocked_cycle"]]
    assert p1["id"] in cycle_from


@pytest.mark.asyncio
async def test_migrate_legacy_promotion_evidence_end_to_end(db):
    project = await db_module.create_project(db, "Recon Test Project")
    proposal = await db_module.add_workspace_proposal(
        db, "Do the thing", "body", project_id=project["id"],
    )
    promoted = await db_module.promote_workspace_proposal(
        db, proposal["id"], project["id"],
    )
    si_id = promoted["sprint_item_id"]

    # promote_workspace_proposal already auto-links evidence (6cdc5df3) — simulate
    # the LEGACY gap this item audits by deleting that row directly, mirroring
    # a pre-6cdc5df3 promotion or a promotion whose auto-link call failed.
    await db.execute(
        "DELETE FROM proposal_evidence_links WHERE proposal_id = ?",
        (proposal["id"],),
    )
    await db.commit()

    report = await db_module.audit_legacy_promotion_evidence(db)
    expected_key = f"{proposal['id']}:sprint_item:{si_id}"
    assert expected_key in [c["candidate_key"] for c in report["would_migrate"]]

    dry = await db_module.migrate_legacy_promotion_evidence(db)
    assert dry["dry_run"] is True
    assert dry["applied"] == []

    result = await db_module.migrate_legacy_promotion_evidence(db, dry_run=False)
    assert len(result["applied"]) == 1
    links = await db_module.get_proposal_links(db, project["id"], proposal["id"])
    assert any(
        link["entity_type"] == "sprint_item" and link["entity_id"] == si_id
        for link in links
    )

    # Idempotent retry.
    replay = await db_module.migrate_legacy_promotion_evidence(db, dry_run=False)
    assert replay["applied"] == []
    report2 = await db_module.audit_legacy_promotion_evidence(db)
    assert expected_key not in [c["candidate_key"] for c in report2["would_migrate"]]


@pytest.mark.asyncio
async def test_audit_legacy_proposal_references_combined_wrapper(db):
    p1 = await db_module.add_workspace_proposal(db, "Root idea", "root")
    await db_module.add_workspace_proposal(
        db, "Better idea", f"this supersedes {p1['id']}",
    )
    combined = await db_module.audit_legacy_proposal_references(db)
    assert "proposal_lineage" in combined
    assert "promotion_evidence" in combined
    assert combined["total_would_migrate"] >= 1
