"""Tests for W1-H (ab51cec0) — two independent handoff fixes:

(1) Handoff body-hash normalization for the ``/loop`` re-delivery wrapper.
    ``_build_quick_start_goal`` bakes a literal ``"/loop "`` wrapper in front
    of ``/goal`` when a project's loop-auto-continue setting is on (see
    ``_loop_prefix`` there), and that wrapper is already part of the body
    ``mint_handoff_token``/``verify_handoff_token`` hash. A host-side
    re-delivery/wakeup of a paused prompt (the ``/loop`` skill re-firing) can
    add or strip that SAME literal wrapper independently of what was baked in
    at mint time, so a legitimate, unmodified re-delivery could previously
    fail ``verify_handoff_token`` with a false-positive ``body_mismatch``.
    ``_loop_redelivery_body_variants``/``_body_hash_matches`` now tolerate
    exactly that one known literal difference — nothing else — so a genuinely
    tampered body still fails.

(2) ``_build_quick_start_goal``'s ``<excluded_superseded>`` /
    ``<excluded_unprospected>`` / ``<excluded_wave_gate_pending>`` /
    ``<excluded_dependency_not_satisfied>`` notes each rendered a ``count=``
    attribute computed from the RAW excluded-item list length, while the
    comma-joined id text in the same tag was built from a list FILTERED to
    items with a truthy ``id`` — so an excluded item with no ``id`` inflated
    ``count`` past the number of ids actually listed: a count that does not
    match what was actually omitted. Both numbers are now derived from the
    same filtered list.
"""
from __future__ import annotations

import re

import pytest

from meridian import handoff as handoff_module


# ---------------------------------------------------------------------------
# (1a) _loop_redelivery_body_variants / _body_hash_matches — pure unit tests
# ---------------------------------------------------------------------------


_GOAL_BODY = '/goal\n<sprint_items>Complete sprint items: a1.</sprint_items>'


def test_variants_identity_when_body_already_matches():
    variants = handoff_module._loop_redelivery_body_variants(_GOAL_BODY)
    assert variants[0] == _GOAL_BODY


def test_variants_include_prefix_added_form_for_a_bare_goal_body():
    variants = handoff_module._loop_redelivery_body_variants(_GOAL_BODY)
    assert "/loop " + _GOAL_BODY in variants


def test_variants_include_prefix_stripped_form_for_a_loop_wrapped_body():
    wrapped = "/loop " + _GOAL_BODY
    variants = handoff_module._loop_redelivery_body_variants(wrapped)
    assert _GOAL_BODY in variants


def test_variants_tolerate_extra_whitespace_after_loop():
    wrapped = "/loop   " + _GOAL_BODY  # 3 spaces, still "one wrapper"
    variants = handoff_module._loop_redelivery_body_variants(wrapped)
    assert _GOAL_BODY in variants


def test_variants_do_not_fabricate_a_prefix_for_non_goal_bodies():
    """A wave-run manifest (or any other non-/goal handoff body) never had a
    /loop wrapper to begin with — don't manufacture a spurious candidate for
    it, only the identity variant."""
    manifest_body = '{"wave_run_id": "w1", "items": ["a1"]}'
    variants = handoff_module._loop_redelivery_body_variants(manifest_body)
    assert variants == [manifest_body]


def test_body_hash_matches_true_for_loop_prefix_added_at_redelivery():
    expected_hash = handoff_module._hash_goal_body(_GOAL_BODY)
    presented = "/loop " + _GOAL_BODY
    assert handoff_module._body_hash_matches(presented, expected_hash) is True


def test_body_hash_matches_true_for_loop_prefix_stripped_at_redelivery():
    minted_with_loop = "/loop " + _GOAL_BODY
    expected_hash = handoff_module._hash_goal_body(minted_with_loop)
    presented = _GOAL_BODY  # host stripped the wrapper on resend
    assert handoff_module._body_hash_matches(presented, expected_hash) is True


def test_body_hash_matches_true_for_exact_match_unchanged():
    expected_hash = handoff_module._hash_goal_body(_GOAL_BODY)
    assert handoff_module._body_hash_matches(_GOAL_BODY, expected_hash) is True


def test_body_hash_matches_false_for_genuine_tamper_even_with_loop_wrapper():
    """The normalization must not widen what counts as a match for any OTHER
    difference: prepending /loop to a body whose CONTENT was also edited
    must still fail."""
    expected_hash = handoff_module._hash_goal_body(_GOAL_BODY)
    tampered = "/loop " + _GOAL_BODY.replace("a1", "a1-FABRICATED")
    assert handoff_module._body_hash_matches(tampered, expected_hash) is False


def test_body_hash_matches_false_for_genuine_tamper_without_loop_wrapper():
    expected_hash = handoff_module._hash_goal_body(_GOAL_BODY)
    tampered = _GOAL_BODY.replace("a1", "a1-FABRICATED")
    assert handoff_module._body_hash_matches(tampered, expected_hash) is False


# ---------------------------------------------------------------------------
# (1b) verify_handoff_token integration — DB-backed body_hash check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_handoff_token_tolerates_loop_prefix_added_at_redelivery(db):
    """Mint over a bare (no loop_enabled) body; a /loop-wrapped re-delivery of
    the SAME body still verifies ok, not body_mismatch."""
    project_id = "w1h-loop-prefix-added"
    token = await handoff_module.mint_handoff_token(db, project_id, body=_GOAL_BODY)

    result = await handoff_module.verify_handoff_token(
        db, token, project_id, body="/loop " + _GOAL_BODY,
    )
    assert result["valid"] is True
    assert result["reason"] == "ok"


@pytest.mark.asyncio
async def test_verify_handoff_token_tolerates_loop_prefix_stripped_at_redelivery(db):
    """Mint over a loop_enabled body (wrapper baked in at mint time); a
    re-delivery that stripped the wrapper still verifies ok."""
    project_id = "w1h-loop-prefix-stripped"
    minted_body = "/loop " + _GOAL_BODY
    token = await handoff_module.mint_handoff_token(db, project_id, body=minted_body)

    result = await handoff_module.verify_handoff_token(
        db, token, project_id, body=_GOAL_BODY,
    )
    assert result["valid"] is True
    assert result["reason"] == "ok"


@pytest.mark.asyncio
async def test_verify_handoff_token_loop_normalization_does_not_mask_tampering(db):
    """A genuine tamper (item ids/content changed) must still be rejected as
    body_mismatch even when wrapped in a /loop prefix, and must NOT consume
    the token (mirrors the pre-existing body_mismatch non-consuming contract)."""
    project_id = "w1h-loop-prefix-real-tamper"
    token = await handoff_module.mint_handoff_token(db, project_id, body=_GOAL_BODY)

    tampered = "/loop " + _GOAL_BODY.replace("a1", "a1-FABRICATED")
    mismatch = await handoff_module.verify_handoff_token(
        db, token, project_id, body=tampered,
    )
    assert mismatch["valid"] is False
    assert mismatch["reason"] == "body_mismatch"

    # token must still be usable afterward with the real body — proves the
    # mismatch above did not consume it.
    correct = await handoff_module.verify_handoff_token(
        db, token, project_id, body=_GOAL_BODY,
    )
    assert correct["valid"] is True
    assert correct["reason"] == "ok"


# ---------------------------------------------------------------------------
# (2) <excluded_*> count== the number of ids actually listed
# ---------------------------------------------------------------------------


def _tag_count_and_ids(goal_text: str, tag: str) -> "tuple[int, list[str]]":
    m = re.search(rf'<{tag} count="(\d+)">([^<]*)</{tag}>', goal_text)
    assert m, f"expected a <{tag} count=...> tag in:\n{goal_text}"
    count = int(m.group(1))
    ids = [x for x in m.group(2).split(", ") if x]
    return count, ids


def test_excluded_superseded_count_matches_listed_ids_with_an_idless_item():
    items = [
        {"id": "a1", "title": "keep", "status": "pending", "version": "v1"},
        {"title": "no-id-blocked", "status": "pending", "version": "v1",
         "blocker_kind": "superseded"},  # no "id" key at all
        {"id": "a3", "title": "blocked", "status": "pending", "version": "v1",
         "blocker_kind": "superseded"},
    ]
    goal = handoff_module._build_quick_start_goal(items, version="v1")
    count, ids = _tag_count_and_ids(goal, "excluded_superseded")
    assert count == len(ids) == 1
    assert ids == ["a3"]


def test_excluded_unprospected_count_matches_listed_ids_with_an_idless_item():
    items = [
        {"id": "a1", "title": "keep", "status": "pending", "version": "v1"},
        {"title": "no-id-unprospected", "status": "pending", "version": "v1",
         "touches_resources": '["file:x.py"]'},  # no "id" key
        {"id": "a3", "title": "unprospected", "status": "pending", "version": "v1",
         "touches_resources": '["file:y.py"]'},
    ]
    goal = handoff_module._build_quick_start_goal(
        items, version="v1", pointer_evidence_ids=frozenset(),
    )
    count, ids = _tag_count_and_ids(goal, "excluded_unprospected")
    assert count == len(ids) == 1
    assert ids == ["a3"]


def test_excluded_wave_gate_pending_count_matches_listed_ids_with_an_idless_item():
    items = [
        {"id": "a1", "title": "keep", "status": "pending", "version": "v1",
         "wave": "wave-1"},
        {"title": "no-id-gated", "status": "pending", "version": "v1",
         "wave": "wave-3"},  # no "id" key
        {"id": "a3", "title": "gated", "status": "pending", "version": "v1",
         "wave": "wave-3"},
    ]
    goal = handoff_module._build_quick_start_goal(
        items, version="v1",
        wave_gate_pending=[{"wave_end": "wave-2", "gate_passed": False}],
    )
    count, ids = _tag_count_and_ids(goal, "excluded_wave_gate_pending")
    assert count == len(ids) == 1
    assert ids == ["a3"]


def test_excluded_dependency_not_satisfied_count_matches_listed_ids_with_an_idless_item():
    _blockers = [{"id": "outside-item", "status": "pending"}]
    items = [
        {"id": "a1", "title": "keep", "status": "pending", "version": "v1"},
        {"title": "no-id-dep-blocked", "status": "pending", "version": "v1",
         "frontier_ready": False,
         "frontier_blocking_predecessors": _blockers},  # no "id" key
        {"id": "a3", "title": "dep-blocked", "status": "pending", "version": "v1",
         "frontier_ready": False,
         "frontier_blocking_predecessors": _blockers},
    ]
    goal = handoff_module._build_quick_start_goal(items, version="v1")
    count, ids = _tag_count_and_ids(goal, "excluded_dependency_not_satisfied")
    assert count == len(ids) == 1
    assert ids == ["a3"]


def test_excluded_superseded_count_unaffected_when_every_item_has_an_id():
    """Non-regression: the common case (every item has a real id) renders
    exactly the same count it always did."""
    items = [
        {"id": "a1", "title": "keep", "status": "pending", "version": "v1"},
        {"id": "a2", "title": "blocked-1", "status": "pending", "version": "v1",
         "blocker_kind": "superseded"},
        {"id": "a3", "title": "blocked-2", "status": "pending", "version": "v1",
         "blocker_kind": "superseded"},
    ]
    goal = handoff_module._build_quick_start_goal(items, version="v1")
    count, ids = _tag_count_and_ids(goal, "excluded_superseded")
    assert count == len(ids) == 2
    assert set(ids) == {"a2", "a3"}
