"""Tests for 704edefe — the reservation and integration-queue manifest
around ``claim_parallel_batch``.

``claim_parallel_batch`` (22cad9b8) already persisted an immutable
"what batch was decided" manifest and already rejected a duplicate
reservation (``BATCH_MANIFEST_EXISTS``, unless ``force_manifest=True``) and a
stale one (``STALE_PLAN_GENERATION``, via ``plan_generation`` vs. the live
board digest, 0d0cada7) — those two behaviors are pre-existing, exercised by
``tests/test_resource_locks.py``'s ``claim_parallel_batch`` suite, and are
NOT re-tested here.

This file covers what 704edefe adds to the manifest itself: per-resource
``resolved_symbols`` (a static prediction at persist time, overwritten with
the actual claim outcome once the attempt resolves), ``dependency_frontier``
(each item's ``depends_on`` edge and whether it was satisfied at reservation
time), ``expected_outputs`` (each item's own declared artifact
kind/output/policy), ``verifier_class`` (a derived verification-strictness
classification), and ``integration_order`` (the dependency-respecting
merge/integration sequence) — at three layers:

  1. The pure per-item helper functions in isolation
     (``_classify_verifier_class``, ``_expected_output_of``,
     ``_dependency_frontier_snapshot``, ``_compute_integration_order``).
  2. The low-level ``persist_batch_claim_manifest`` /
     ``mark_batch_claim_outcome`` plumbing in ``meridian/db/batch_claim.py``.
  3. The full ``claim_parallel_batch`` wiring, end to end.
"""
from __future__ import annotations

import pytest

from meridian import db as db_module


# ---------------------------------------------------------------------------
# 1. Pure per-item helper functions
# ---------------------------------------------------------------------------

def test_classify_verifier_class_strict_evidence_wins_over_everything():
    item = {
        "require_strict_evidence": True,
        "require_verification": True,
        "artifact_kind": "figure",
    }
    assert db_module._classify_verifier_class(item) == "strict_evidence"


def test_classify_verifier_class_verification_required_without_strict():
    item = {
        "require_strict_evidence": False,
        "require_verification": True,
        "artifact_kind": "figure",
    }
    assert db_module._classify_verifier_class(item) == "verification_required"


def test_classify_verifier_class_artifact_check_without_verification_flags():
    item = {
        "require_strict_evidence": False,
        "require_verification": False,
        "artifact_kind": "table",
    }
    assert db_module._classify_verifier_class(item) == "artifact_check"


def test_classify_verifier_class_standard_when_nothing_set():
    item = {"require_strict_evidence": False, "require_verification": False, "artifact_kind": None}
    assert db_module._classify_verifier_class(item) == "standard"
    assert db_module._classify_verifier_class({}) == "standard"


def test_expected_output_of_snapshots_artifact_declaration_trio_only():
    """Reads through the effective_* accessors (the canonical decode path
    for these JSON-TEXT-stored fields), not raw item.get(...) -- so
    artifact_policy comes back as the full MERGED policy (declared field
    overriding the project default), not the bare declared dict."""
    item = {
        "artifact_kind": "figure",
        "planned_output": {"source_type": "docs", "targets": []},
        "artifact_policy": {"artifact_pointer_check": "strict"},
        "title": "irrelevant field that must not leak into the snapshot",
        "status": "pending",
    }
    out = db_module._expected_output_of(item)
    assert out["artifact_kind"] == "figure"
    assert out["planned_output"] == {"source_type": "docs", "targets": []}
    assert out["artifact_policy"]["artifact_pointer_check"] == "strict"
    # Unset guard flags fall back to the project default (False), not absent.
    assert out["artifact_policy"]["require_exact_figure_output_pointer"] is False


def test_expected_output_of_undeclared_kind_and_output_but_policy_defaults():
    """artifact_kind/planned_output are "unknown" (None) when undeclared --
    never guessed. artifact_policy is different: effective_artifact_policy
    always resolves to a CONCRETE policy (the project default) even when
    nothing was declared, since completion always checks against SOME
    policy."""
    out = db_module._expected_output_of({})
    assert out["artifact_kind"] is None
    assert out["planned_output"] is None
    assert out["artifact_policy"] == {
        "artifact_pointer_check": "warn",
        "require_exact_figure_output_pointer": False,
        "require_exact_table_output_pointer": False,
        "allow_document_only_override": False,
    }


def test_compute_integration_order_no_dependencies_preserves_input_order():
    items_by_id = {"a": {"depends_on": None}, "b": {"depends_on": None}, "c": {"depends_on": None}}
    assert db_module._compute_integration_order(["c", "a", "b"], items_by_id) == ["c", "a", "b"]


def test_compute_integration_order_in_batch_dependency_sorts_after_parent():
    # c depends on b, b depends on a -- input order deliberately scrambled so
    # a pass would only happen if the order were genuinely dependency-aware.
    items_by_id = {
        "a": {"depends_on": None},
        "b": {"depends_on": "a"},
        "c": {"depends_on": "b"},
    }
    order = db_module._compute_integration_order(["c", "b", "a"], items_by_id)
    assert order.index("a") < order.index("b") < order.index("c")
    assert set(order) == {"a", "b", "c"}


def test_compute_integration_order_out_of_batch_dependency_ignored():
    # b depends on an item that is NOT in this batch at all -- must not
    # stall waiting for it; b is treated as immediately ready (the claim
    # gate already guarantees an out-of-batch dependency was done before b
    # could ever be claimed -- see _dependency_frontier_snapshot).
    items_by_id = {"a": {"depends_on": None}, "b": {"depends_on": "outside-item"}}
    order = db_module._compute_integration_order(["b", "a"], items_by_id)
    assert set(order) == {"a", "b"}


def test_compute_integration_order_never_hangs_on_a_cycle():
    # A malformed in-batch cycle (already rejected at write time by
    # update_sprint_item's cycle guard, but the helper must still terminate
    # defensively rather than loop forever if one somehow reached this far).
    items_by_id = {"a": {"depends_on": "b"}, "b": {"depends_on": "a"}}
    order = db_module._compute_integration_order(["a", "b"], items_by_id)
    assert set(order) == {"a", "b"}
    assert len(order) == 2


@pytest.mark.asyncio
async def test_dependency_frontier_snapshot_no_dependency(db):
    items_by_id = {"a": {"depends_on": None}}
    frontier = await db_module._dependency_frontier_snapshot(db, items_by_id)
    assert frontier == {"a": {"depends_on": None, "dependency_satisfied": True}}


@pytest.mark.asyncio
async def test_dependency_frontier_snapshot_in_batch_dependency_not_yet_done(db):
    items_by_id = {
        "a": {"depends_on": None, "status": "pending"},
        "b": {"depends_on": "a", "status": "pending"},
    }
    frontier = await db_module._dependency_frontier_snapshot(db, items_by_id)
    assert frontier["b"] == {
        "depends_on": "a", "dependency_satisfied": False, "dependency_in_batch": True,
    }


@pytest.mark.asyncio
async def test_dependency_frontier_snapshot_in_batch_dependency_done(db):
    items_by_id = {
        "a": {"depends_on": None, "status": "done"},
        "b": {"depends_on": "a", "status": "pending"},
    }
    frontier = await db_module._dependency_frontier_snapshot(db, items_by_id)
    assert frontier["b"] == {
        "depends_on": "a", "dependency_satisfied": True, "dependency_in_batch": True,
    }


@pytest.mark.asyncio
async def test_dependency_frontier_snapshot_out_of_batch_dependency_looked_up_live(db):
    """A dependency NOT present in items_by_id is resolved via a real
    get_sprint_item lookup against the live board, not assumed."""
    p = await db_module.create_project(db, "frontier-out-of-batch")
    pid = p["id"]
    sess = await db_module.register_session(db, pid, "orchestrator")
    parent = await db_module.add_sprint_item(db, pid, "v1", "parent")
    await db_module.claim_sprint_item(db, pid, parent["id"], actor=sess["id"])
    await db_module.complete_sprint_item(db, pid, parent["id"])

    items_by_id = {"b": {"depends_on": parent["id"]}}
    frontier = await db_module._dependency_frontier_snapshot(db, items_by_id)
    assert frontier["b"] == {
        "depends_on": parent["id"], "dependency_satisfied": True,
        "dependency_in_batch": False,
    }


@pytest.mark.asyncio
async def test_dependency_frontier_snapshot_missing_dependency_resolves_unsatisfied(db):
    items_by_id = {"b": {"depends_on": "does-not-exist"}}
    frontier = await db_module._dependency_frontier_snapshot(db, items_by_id)
    assert frontier["b"]["dependency_satisfied"] is False
    assert frontier["b"]["dependency_in_batch"] is False


# ---------------------------------------------------------------------------
# 2. persist_batch_claim_manifest / mark_batch_claim_outcome plumbing
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_persist_batch_claim_manifest_stores_and_decodes_reservation_fields(db):
    p = await db_module.create_project(db, "reservation-persist-fields")
    pid = p["id"]
    sess = await db_module.register_session(db, pid, "orchestrator")
    a = await db_module.add_sprint_item(
        db, pid, "v1", "a", touches_resources=["file:a.py"], prospect_bypass=True,
    )
    manifest = await db_module.persist_batch_claim_manifest(
        db, pid, sess["id"], [a["id"]], {a["id"]: ["file:a.py"]}, ["file:a.py"],
        resolved_symbols=[{"resource": "file:a.py", "predicted_granularity": "file"}],
        dependency_frontier={a["id"]: {"depends_on": None, "dependency_satisfied": True}},
        expected_outputs={a["id"]: {"artifact_kind": None, "planned_output": None, "artifact_policy": None}},
        verifier_class={a["id"]: "standard"},
        integration_order=[a["id"]],
    )
    assert manifest["resolved_symbols"] == [{"resource": "file:a.py", "predicted_granularity": "file"}]
    assert manifest["dependency_frontier"] == {a["id"]: {"depends_on": None, "dependency_satisfied": True}}
    assert manifest["expected_outputs"] == {
        a["id"]: {"artifact_kind": None, "planned_output": None, "artifact_policy": None},
    }
    assert manifest["verifier_class"] == {a["id"]: "standard"}
    assert manifest["integration_order"] == [a["id"]]

    # Round-trips through a fresh fetch by id too, not just the insert echo.
    reread = await db_module.get_batch_claim_manifest_by_id(db, manifest["id"])
    assert reread["integration_order"] == [a["id"]]
    assert reread["verifier_class"] == {a["id"]: "standard"}


@pytest.mark.asyncio
async def test_persist_batch_claim_manifest_defaults_reservation_fields_when_omitted(db):
    """Omitting the new kwargs (any pre-704edefe caller) must still decode
    to empty containers, never None/KeyError -- backward compatible."""
    p = await db_module.create_project(db, "reservation-defaults")
    pid = p["id"]
    sess = await db_module.register_session(db, pid, "orchestrator")
    a = await db_module.add_sprint_item(
        db, pid, "v1", "a", touches_resources=["file:a.py"], prospect_bypass=True,
    )
    manifest = await db_module.persist_batch_claim_manifest(
        db, pid, sess["id"], [a["id"]], {a["id"]: ["file:a.py"]}, ["file:a.py"],
    )
    assert manifest["resolved_symbols"] == []
    assert manifest["dependency_frontier"] == {}
    assert manifest["expected_outputs"] == {}
    assert manifest["verifier_class"] == {}
    assert manifest["integration_order"] == []


@pytest.mark.asyncio
async def test_mark_batch_claim_outcome_overwrites_resolved_symbols_when_supplied(db):
    p = await db_module.create_project(db, "reservation-mark-outcome")
    pid = p["id"]
    sess = await db_module.register_session(db, pid, "orchestrator")
    a = await db_module.add_sprint_item(
        db, pid, "v1", "a", touches_resources=["file:a.py"], prospect_bypass=True,
    )
    manifest = await db_module.persist_batch_claim_manifest(
        db, pid, sess["id"], [a["id"]], {a["id"]: ["file:a.py"]}, ["file:a.py"],
        resolved_symbols=[{"resource": "file:a.py", "predicted_granularity": "file"}],
    )
    updated = await db_module.mark_batch_claim_outcome(
        db, manifest["id"], "claimed",
        resolved_symbols=[{"resource": "file:a.py", "scope": "file", "claim_granularity": "file"}],
    )
    assert updated["resolved_symbols"] == [
        {"resource": "file:a.py", "scope": "file", "claim_granularity": "file"},
    ]


@pytest.mark.asyncio
async def test_mark_batch_claim_outcome_leaves_resolved_symbols_untouched_when_omitted(db):
    p = await db_module.create_project(db, "reservation-mark-outcome-omit")
    pid = p["id"]
    sess = await db_module.register_session(db, pid, "orchestrator")
    a = await db_module.add_sprint_item(
        db, pid, "v1", "a", touches_resources=["file:a.py"], prospect_bypass=True,
    )
    manifest = await db_module.persist_batch_claim_manifest(
        db, pid, sess["id"], [a["id"]], {a["id"]: ["file:a.py"]}, ["file:a.py"],
        resolved_symbols=[{"resource": "file:a.py", "predicted_granularity": "file"}],
    )
    updated = await db_module.mark_batch_claim_outcome(db, manifest["id"], "failed")
    assert updated["resolved_symbols"] == [{"resource": "file:a.py", "predicted_granularity": "file"}]


# ---------------------------------------------------------------------------
# 3. Full claim_parallel_batch wiring, end to end
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_claim_parallel_batch_populates_all_reservation_fields_on_success(db):
    p = await db_module.create_project(db, "reservation-e2e-success")
    pid = p["id"]
    sess = await db_module.register_session(db, pid, "orchestrator")
    a = await db_module.add_sprint_item(
        db, pid, "v1", "a", touches_resources=["file:a.py"], prospect_bypass=True,
    )
    b = await db_module.add_sprint_item(
        db, pid, "v1", "b", touches_resources=["symbol:b.py::foo"], prospect_bypass=True,
    )
    result = await db_module.claim_parallel_batch(
        db, pid, sess["id"], [a["id"], b["id"]],
        resource_contents={"b.py": "def foo():\n    return 1\n"},
    )
    assert result["ok"] is True
    manifest = result["manifest"]

    # resolved_symbols reflects the ACTUAL claim outcome (post-attempt),
    # not just the pre-attempt prediction: a real file/symbol scope with a
    # claim_granularity, keyed to the claiming item too.
    by_resource = {e["resource"]: e for e in manifest["resolved_symbols"]}
    assert by_resource["file:a.py"]["scope"] == "file"
    assert by_resource["file:a.py"]["claim_granularity"] == "file"
    assert by_resource["file:a.py"]["item_id"] == a["id"]
    assert by_resource["symbol:b.py::foo"]["scope"] == "symbol"
    assert by_resource["symbol:b.py::foo"]["claim_granularity"] == "symbol"
    assert by_resource["symbol:b.py::foo"]["item_id"] == b["id"]

    # dependency_frontier: neither item declares a dependency.
    assert manifest["dependency_frontier"] == {
        a["id"]: {"depends_on": None, "dependency_satisfied": True},
        b["id"]: {"depends_on": None, "dependency_satisfied": True},
    }

    # expected_outputs: neither item declared an artifact_kind/planned_output
    # (both read back as None -- never guessed), but artifact_policy always
    # resolves to the concrete project-default policy (effective_artifact_
    # policy never reports "unknown").
    _default_policy = {
        "artifact_pointer_check": "warn",
        "require_exact_figure_output_pointer": False,
        "require_exact_table_output_pointer": False,
        "allow_document_only_override": False,
    }
    assert manifest["expected_outputs"] == {
        a["id"]: {"artifact_kind": None, "planned_output": None, "artifact_policy": _default_policy},
        b["id"]: {"artifact_kind": None, "planned_output": None, "artifact_policy": _default_policy},
    }

    # verifier_class: no verification flags, no artifact_kind -> standard.
    assert manifest["verifier_class"] == {a["id"]: "standard", b["id"]: "standard"}

    # integration_order: no in-batch dependency edges -> original order,
    # also promoted to the top-level success result for convenience.
    assert manifest["integration_order"] == [a["id"], b["id"]]
    assert result["integration_order"] == [a["id"], b["id"]]


@pytest.mark.asyncio
async def test_claim_parallel_batch_verifier_class_per_item_classification(db):
    p = await db_module.create_project(db, "reservation-verifier-class")
    pid = p["id"]
    sess = await db_module.register_session(db, pid, "orchestrator")

    strict = await db_module.add_sprint_item(
        db, pid, "v1", "strict", touches_resources=["file:strict.py"], prospect_bypass=True,
    )
    await db_module.patch_sprint_item(db, pid, strict["id"], require_strict_evidence=True)

    verify = await db_module.add_sprint_item(
        db, pid, "v1", "verify", touches_resources=["file:verify.py"], prospect_bypass=True,
    )
    await db_module.patch_sprint_item(db, pid, verify["id"], require_verification=True)

    artifact = await db_module.add_sprint_item(
        db, pid, "v1", "artifact", touches_resources=["file:artifact.py"], prospect_bypass=True,
        artifact_kind="figure",
    )

    standard = await db_module.add_sprint_item(
        db, pid, "v1", "standard", touches_resources=["file:standard.py"], prospect_bypass=True,
    )

    result = await db_module.claim_parallel_batch(
        db, pid, sess["id"], [strict["id"], verify["id"], artifact["id"], standard["id"]],
    )
    assert result["ok"] is True
    vc = result["manifest"]["verifier_class"]
    assert vc[strict["id"]] == "strict_evidence"
    assert vc[verify["id"]] == "verification_required"
    assert vc[artifact["id"]] == "artifact_check"
    assert vc[standard["id"]] == "standard"


@pytest.mark.asyncio
async def test_claim_parallel_batch_expected_outputs_captures_artifact_declaration(db):
    p = await db_module.create_project(db, "reservation-expected-outputs")
    pid = p["id"]
    sess = await db_module.register_session(db, pid, "orchestrator")
    planned_output = {
        "source_type": "docs",
        "targets": [{
            "uri": "outputs/figures/ablation.png",
            "selector": {"type": "range", "start_line": 1, "end_line": 1},
            "target_kind": "planned_new",
        }],
        "provenance_required": True,
    }
    artifact_policy = {"artifact_pointer_check": "strict", "require_exact_figure_output_pointer": True}
    item = await db_module.add_sprint_item(
        db, pid, "v1", "figure item", touches_resources=["file:fig.py"], prospect_bypass=True,
        artifact_kind="figure", planned_output=planned_output, artifact_policy=artifact_policy,
    )
    result = await db_module.claim_parallel_batch(db, pid, sess["id"], [item["id"]])
    assert result["ok"] is True
    eo = result["manifest"]["expected_outputs"][item["id"]]
    assert eo["artifact_kind"] == "figure"
    assert eo["planned_output"]["targets"][0]["uri"] == "outputs/figures/ablation.png"
    assert eo["artifact_policy"]["artifact_pointer_check"] == "strict"


@pytest.mark.asyncio
async def test_claim_parallel_batch_dependency_frontier_out_of_batch_dependency(db):
    p = await db_module.create_project(db, "reservation-dep-frontier")
    pid = p["id"]
    sess = await db_module.register_session(db, pid, "orchestrator")
    parent = await db_module.add_sprint_item(db, pid, "v1", "parent")
    await db_module.claim_sprint_item(db, pid, parent["id"], actor=sess["id"])
    await db_module.complete_sprint_item(db, pid, parent["id"])

    child = await db_module.add_sprint_item(
        db, pid, "v1", "child", touches_resources=["file:child.py"], prospect_bypass=True,
        depends_on=parent["id"],
    )
    result = await db_module.claim_parallel_batch(db, pid, sess["id"], [child["id"]])
    assert result["ok"] is True
    frontier = result["manifest"]["dependency_frontier"][child["id"]]
    assert frontier == {
        "depends_on": parent["id"],
        "dependency_satisfied": True,
        "dependency_in_batch": False,
    }


@pytest.mark.asyncio
async def test_claim_parallel_batch_manifest_immutability_unaffected_by_new_fields(db):
    """704edefe must not regress the pre-existing "reject duplicate
    reservation" guard (BATCH_MANIFEST_EXISTS)."""
    p = await db_module.create_project(db, "reservation-immutability-still-works")
    pid = p["id"]
    sess = await db_module.register_session(db, pid, "orchestrator")
    a = await db_module.add_sprint_item(
        db, pid, "v1", "a", touches_resources=["file:a.py"], prospect_bypass=True,
    )
    first = await db_module.claim_parallel_batch(db, pid, sess["id"], [a["id"]])
    assert first["ok"] is True
    assert first["manifest"]["integration_order"] == [a["id"]]

    with pytest.raises(ValueError):
        await db_module.persist_batch_claim_manifest(
            db, pid, sess["id"], [a["id"]], {a["id"]: ["file:a.py"]}, ["file:a.py"],
        )


@pytest.mark.asyncio
async def test_claim_parallel_batch_failure_still_records_partial_reservation_fields(db):
    """A batch that fails MID-ATTEMPT (a real cross-session resource
    conflict, as opposed to an up-front composition/undeclared-resource
    rejection that never persists a manifest at all -- covered by
    test_resource_locks.py) still gets a durable manifest: the
    dependency_frontier/expected_outputs/verifier_class/integration_order
    predictions (computed before the attempt even started) are present, and
    resolved_symbols reflects exactly how far the resource loop actually
    got before the conflict aborted the batch."""
    p = await db_module.create_project(db, "reservation-failure-fields")
    pid = p["id"]
    sess = await db_module.register_session(db, pid, "orchestrator")
    stranger = await db_module.register_session(db, pid, "stranger")
    a = await db_module.add_sprint_item(
        db, pid, "v1", "a", touches_resources=["file:a.py"], prospect_bypass=True,
    )
    b = await db_module.add_sprint_item(
        db, pid, "v1", "b", touches_resources=["file:b.py"], prospect_bypass=True,
    )
    pre = await db_module.claim_file(db, "b.py", stranger["id"])
    assert pre["claimed"] is True

    result = await db_module.claim_parallel_batch(db, pid, sess["id"], [a["id"], b["id"]])
    assert result["ok"] is False
    assert result["error"] == "BATCH_RESOURCE_CONFLICT"

    manifest = await db_module.get_batch_claim_manifest(
        db, pid, db_module.compute_batch_key([a["id"], b["id"]]),
    )
    assert manifest["status"] == "failed"

    # a's resource WAS resolved (scope="file") before b's conflict aborted
    # the batch -- resolved_symbols reflects that partial progress.
    by_resource = {e["resource"]: e for e in manifest["resolved_symbols"]}
    assert by_resource["file:a.py"]["scope"] == "file"
    assert by_resource["file:a.py"]["claim_granularity"] == "file"
    # b.py's resource never got far enough to be appended to resource_claims
    # (the conflict was detected before that happened).
    assert "file:b.py" not in by_resource

    # The predictions computed BEFORE the attempt started are still present.
    assert set(manifest["dependency_frontier"].keys()) == {a["id"], b["id"]}
    assert manifest["integration_order"] == [a["id"], b["id"]]
    assert manifest["verifier_class"] == {a["id"]: "standard", b["id"]: "standard"}
