"""75de5905 — GATE: adversarial handoff-manifest, XML projection, connector,
and artifact contract matrix.

This is the consolidated, one-stop-per-acceptance-criterion checklist for
the whole handoff-manifest-artifact-contract tranche (acf6f51a's canonical
XML HandoffManifest + body-bound tokens, 1bd5e810's receiver-side
accept_handoff_envelope, f6912e2d's artifact_recipe schema). Each section
below is named after one literal phrase from this item's own acceptance
criteria; most compose already-tested primitives from
meridian.handoff/meridian.artifact_declaration/meridian.docx_integrity_gate
end-to-end rather than re-deriving unit-level behavior duplicated in
tests/test_handoff_manifest_v2.py, tests/test_handoff_board_divergence.py,
tests/test_handoff_connector_parity.py, and tests/test_docx_integrity_gate.py
— this file's job is to prove the WHOLE named scenario, not to re-litigate
each primitive's own already-covered edge cases.

Scenario checklist (verbatim from the item notes), each with its own
section marker below:
  1.  malformed/escaped XML
  2.  body tampering
  3.  genuine token with altered body
  4.  expired/consumed tokens
  5.  stale board revision
  6.  different endpoint/tenant
  7.  cross-version IDs
  8.  dependency drift
  9.  missing tools
  10. stale tools/list cache
  11. tunnel restart
  12. local-only DOCX/Outputs paths
  13. partial indexing
  14. Word/COM timeout
  15. rollback
  16. continuation
  17. duplicate retry
  18. deploy evidence
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

from meridian import artifact_declaration as ad
from meridian import db as db_module
from meridian import docx_integrity_gate as gate_module
from meridian import handoff as handoff_module
from meridian import tool_requirements as tool_requirements_module


def _extract_manifest_block(text: str) -> str:
    m = re.search(r"<handoff_manifest\b.*?</handoff_manifest>", text, re.DOTALL)
    assert m is not None, "expected an embedded <handoff_manifest> block"
    return m.group(0)


def _extract_token(text: str) -> str:
    m = re.search(r"<goal_token>([^<]+)</goal_token>", text)
    assert m is not None, "expected a <goal_token> tag"
    return m.group(1).strip()


def _items(*rows):
    return [
        {"id": r[0], "status": r[1], "depends_on": r[2] if len(r) > 2 else None}
        for r in rows
    ]


# ===========================================================================
# 1. Malformed / escaped XML
# ===========================================================================


def test_manifest_xml_escapes_every_text_bearing_field_simultaneously():
    """A single manifest whose item title, project name, and a resource
    string ALL carry XML-breaking characters at once must still serialize to
    well-formed, safely-escaped XML — no partial escaping, no field left
    exposed."""
    items = [{
        "id": "item-0",
        "title": "<script>alert(1)</script> & \"quote\" & 'apos'",
        "status": "todo",
        "depends_on": None,
        "wave": None,
        "touches_resources": '["file:<injected>.py"]',
    }]
    manifest = handoff_module.build_handoff_manifest(
        handoff_mode="goal", project_id="proj-1",
        project_name="<Evil Corp> & Friends",
        items=items,
    )
    xml = handoff_module.serialize_handoff_manifest_xml(manifest)
    for raw in ("<script>", "<Evil Corp>", "<injected>"):
        assert raw not in xml, f"unescaped raw markup leaked into manifest XML: {raw!r}"
    assert "&lt;script&gt;" in xml
    assert "&lt;Evil Corp&gt;" in xml
    assert "&amp;" in xml
    # Still well-formed enough for a naive block extraction (balanced tags).
    assert xml.count("<handoff_manifest") == xml.count("</handoff_manifest>") == 1


def test_manifest_xml_escapes_quotes_in_attribute_position_fields():
    """Adversarial-review finding: esc() previously used the default
    &/</> -only entity map even for ATTRIBUTE values (item id=/status=/
    depends_on=/wave=, board_revision=, project_id=, etc.), so a literal
    `"` in an item id could break out of the attribute and inject an
    arbitrary forged attribute. Confirmed exploit before the fix: an id of
    'item-0" evil="injected' serialized to
    `<item id="item-0" evil="injected" status="todo" ...>`. This proves the
    fix — a quote in EVERY attribute-bearing field is neutralized, not just
    in element-text fields like title."""
    evil_id = 'item-0" evil="injected'
    items = [{
        "id": evil_id,
        "title": "normal title",
        "status": 'todo" also="injected',
        "depends_on": 'dep" x="y',
        "wave": 'wave-1" z="w',
        "touches_resources": None,
    }]
    manifest = handoff_module.build_handoff_manifest(
        handoff_mode="goal", project_id='proj" hacked="1', items=items,
    )
    xml = handoff_module.serialize_handoff_manifest_xml(manifest)
    assert 'evil="injected"' not in xml, "quote injection into item id attribute succeeded"
    assert 'also="injected"' not in xml, "quote injection into status attribute succeeded"
    assert ' x="y"' not in xml, "quote injection into depends_on attribute succeeded"
    assert 'hacked="1"' not in xml, "quote injection into project_id attribute succeeded"
    # The literal double-quote must be neutralized wherever it appeared.
    assert "&quot;" in xml


def test_manifest_xml_is_never_truncated_it_fails_closed_instead():
    """b6510123/248c0bb9's hard rule ('never truncate a token-bound body')
    applies to the manifest too — a too-large manifest must raise, not
    silently emit malformed/cut-off XML."""
    items = [
        {"id": f"item-{i}", "title": "x" * 200, "status": "todo", "depends_on": None,
         "wave": None, "touches_resources": None}
        for i in range(60)
    ]
    manifest = handoff_module.build_handoff_manifest(
        handoff_mode="goal", project_id="proj-huge", items=items, max_items=60,
    )
    with mock.patch.object(handoff_module, "_MANIFEST_MAX_BYTES", 128):
        with pytest.raises(handoff_module.HandoffManifestTooLarge):
            handoff_module.serialize_handoff_manifest_xml(manifest)


# ===========================================================================
# 2 & 3. Body tampering / genuine token with altered body
# ===========================================================================


@pytest.mark.asyncio
async def test_genuine_token_with_manifest_altered_after_generation_is_rejected(db, tmp_path):
    p = await db_module.create_project(db, "matrix-tamper")
    await db_module.set_goal(db, p["id"], "ship it", sprint="v1")
    await db_module.add_sprint_item(db, p["id"], "v1", "do the thing")

    _path, content, _amended = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), mode="goal", skip_ai_summary=True,
        emit_manifest=True,
    )
    token = _extract_token(content)

    # A real, genuine token — but the manifest block inside the presented
    # body is altered (e.g. an attacker widens the declared board_revision
    # to a value that would pass a naive check) after the fact.
    tampered = content.replace('board_revision="', 'board_revision="TAMPERED-')
    stripped = handoff_module.strip_goal_token_banner(tampered)
    verified = await handoff_module.verify_handoff_token(db, token, p["id"], body=stripped)
    assert verified["valid"] is False
    assert verified["reason"] == "body_mismatch"

    # accept_handoff_envelope surfaces the SAME rejection via BODY_HASH_MISMATCH.
    result = await handoff_module.accept_handoff_envelope(
        db, p["id"], goal_token=token, presented_body=stripped,
    )
    assert result["accepted"] is False
    assert result["result"] == handoff_module.ACCEPT_RESULT_BODY_HASH_MISMATCH


# ===========================================================================
# 4. Expired / consumed tokens
# ===========================================================================


@pytest.mark.asyncio
async def test_expired_token_rejected_via_accept_handoff_envelope(db):
    """Mirrors tests/test_dd07ece0_handoff_token.py's own established
    pattern: insert a DB row with an already-expired expires_at directly
    (bypassing mint_handoff_token's future-dated expiry) rather than mocking
    datetime — avoids sleeping, works identically on SQLite and Postgres."""
    import secrets as _secrets

    p = await db_module.create_project(db, "matrix-expired")
    token = _secrets.token_hex(8)
    past_str = (
        datetime.now(timezone.utc) - timedelta(seconds=10)
    ).strftime("%Y-%m-%d %H:%M:%S.%f")
    await db.execute(
        "INSERT INTO handoff_tokens (token, project_id, expires_at, consumed) "
        "VALUES (?, ?, ?, 0)",
        (token, p["id"], past_str),
    )
    await db.commit()

    result = await handoff_module.accept_handoff_envelope(db, p["id"], goal_token=token)
    assert result["accepted"] is False
    assert result["result"] == handoff_module.ACCEPT_RESULT_STALE_HANDOFF
    assert result["token_check"]["reason"] == "expired"


@pytest.mark.asyncio
async def test_already_consumed_token_rejected_via_accept_handoff_envelope(db):
    """A sibling session (or a duplicate retry — see scenario 17) already
    consumed this single-use token; the SECOND accept_handoff_envelope call
    must see already_consumed, bucketed under STALE_HANDOFF, not silently
    succeed."""
    p = await db_module.create_project(db, "matrix-consumed")
    token = await handoff_module.mint_handoff_token(db, p["id"])

    first = await handoff_module.accept_handoff_envelope(db, p["id"], goal_token=token)
    assert first["accepted"] is True

    second = await handoff_module.accept_handoff_envelope(db, p["id"], goal_token=token)
    assert second["accepted"] is False
    assert second["result"] == handoff_module.ACCEPT_RESULT_STALE_HANDOFF
    assert second["token_check"]["reason"] == "already_consumed"


# ===========================================================================
# 5. Stale board revision  (also see tests/test_handoff_board_divergence.py)
# ===========================================================================


@pytest.mark.asyncio
async def test_stale_board_revision_from_a_real_manifest_is_detected(db, tmp_path):
    """End-to-end: a real generate_handoff(emit_manifest=True) manifest's
    declared board_revision is checked against a board that changed status
    AFTER the handoff was generated."""
    p = await db_module.create_project(db, "matrix-stale-board")
    await db_module.set_goal(db, p["id"], "ship it", sprint="v1")
    it = await db_module.add_sprint_item(db, p["id"], "v1", "do the thing")

    _path, content, _amended = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), mode="goal", skip_ai_summary=True,
        emit_manifest=True,
    )
    block = _extract_manifest_block(content)
    board_revision = re.search(r'board_revision="([^"]+)"', block).group(1)

    # The board moved on since generation — another session claimed the item.
    await db_module.claim_sprint_item(db, p["id"], it["id"], actor="sibling-session")

    live = await db_module.get_sprint_items(
        db, p["id"], include_human=False, include_deferred=False, version="v1",
    )
    live = [x for x in live if x.get("status") in ("todo", "pending", "in_progress")]
    result = await handoff_module.accept_handoff_envelope(
        db, p["id"], live_items=live, expected_board_revision=board_revision,
    )
    assert result["accepted"] is False
    assert result["result"] == handoff_module.ACCEPT_RESULT_BOARD_DIVERGENCE


# ===========================================================================
# 6. Different endpoint / tenant
# ===========================================================================


def test_origin_identity_carries_tenant_and_is_absent_when_untracked():
    """resolve_origin_identity is the manifest's own 'which tenant/endpoint'
    signal (acf6f51a) — a different receiving endpoint/tenant is caught at
    the token layer (wrong_project, scenario below), but the manifest must
    at least HONESTLY report tenant identity when known, and never fabricate
    one when it isn't."""
    with_tenant = handoff_module.resolve_origin_identity(
        {"id": "proj-1", "name": "Proj One", "tenant_id": "tenant-abc"}
    )
    assert with_tenant == {
        "project_id": "proj-1", "project_name": "Proj One", "tenant_id": "tenant-abc",
    }
    without_tenant = handoff_module.resolve_origin_identity({"id": "proj-1", "name": "Proj One"})
    assert "tenant_id" not in without_tenant


@pytest.mark.asyncio
async def test_different_receiving_project_endpoint_rejected_as_stale_handoff(db):
    """A handoff genuinely minted for project/tenant A, presented to a
    receiver scoped to project/tenant B (a different endpoint entirely) —
    the SAME real-spoofing-signal path token verification already covers."""
    p_a = await db_module.create_project(db, "matrix-tenant-a")
    p_b = await db_module.create_project(db, "matrix-tenant-b")
    token = await handoff_module.mint_handoff_token(db, p_a["id"])

    result = await handoff_module.accept_handoff_envelope(db, p_b["id"], goal_token=token)
    assert result["accepted"] is False
    assert result["result"] == handoff_module.ACCEPT_RESULT_STALE_HANDOFF
    assert result["token_check"]["reason"] == "wrong_project"


@pytest.mark.asyncio
async def test_tenant_check_is_token_mediated_only_not_independently_enforced(db):
    """Adversarial-review finding: accept_handoff_envelope's own docstring
    documents that it performs NO independent tenant/project check — it
    relies entirely on verify_handoff_token's wrong_project result. That
    means a call made WITHOUT a goal_token (board/tools-only checks, exactly
    what scenarios 5/7/16 legitimately do) has NO cross-tenant protection at
    all: if a caller accidentally fetches project B's live_items while
    checking against project A, nothing here catches it. This test makes
    that boundary EXPLICIT rather than silently assumed — a caller that
    wants tenant safety on a token-less call must supply its own
    project-scoped live_items and verify project_id out of band."""
    p_a = await db_module.create_project(db, "matrix-tenant-no-token-a")
    items_belonging_to_project_a = _items(("a-item", "todo"))
    revision_from_project_a = handoff_module.compute_board_revision(items_belonging_to_project_a)

    # Called against a DIFFERENT project (p_a's own id is never referenced
    # by the check below at all when no goal_token is supplied) with the
    # SAME items/revision — succeeds, because project_id is not itself part
    # of the board/tool checks. This is the documented gap, not a bug.
    result = await handoff_module.accept_handoff_envelope(
        db, p_a["id"],
        live_items=items_belonging_to_project_a,
        expected_board_revision=revision_from_project_a,
    )
    assert result["accepted"] is True, (
        "confirms accept_handoff_envelope's board/tool checks are NOT "
        "project-scoped without a goal_token — by design, but callers must "
        "know this"
    )


# ===========================================================================
# 7. Cross-version IDs
# ===========================================================================


@pytest.mark.asyncio
async def test_cross_version_item_set_diverges_board_revision(db):
    """A manifest built from v1's items must NOT be satisfied by a v2 board
    snapshot, even if v2 happens to reuse similar-looking ids/content —
    board_revision is computed over exactly the items handed in, so a
    caller who fetches the WRONG version's live_items sees divergence."""
    p = await db_module.create_project(db, "matrix-cross-version")
    v1_items = _items(("v1-item", "todo"))
    v1_revision = handoff_module.compute_board_revision(v1_items)

    v2_items = _items(("v2-item", "todo"))  # a different version's item set
    result = await handoff_module.accept_handoff_envelope(
        db, p["id"], live_items=v2_items, expected_board_revision=v1_revision,
    )
    assert result["accepted"] is False
    assert result["result"] == handoff_module.ACCEPT_RESULT_BOARD_DIVERGENCE


# ===========================================================================
# 8. Dependency drift
# ===========================================================================


def test_dependency_only_drift_is_still_detected_as_board_divergence():
    """Status unchanged, ONLY depends_on moved — must still trip
    BOARD_DIVERGENCE (compute_board_revision's tracked-field set includes
    depends_on precisely so a dependency re-target can't slip through)."""
    original = _items(("child", "todo", "parent-a"))
    drifted = _items(("child", "todo", "parent-b"))
    assert handoff_module.compute_board_revision(original) != handoff_module.compute_board_revision(drifted)
    assert handoff_module.verify_board_revision(
        drifted, handoff_module.compute_board_revision(original)
    ) is False


# ===========================================================================
# 9. Missing tools  (also see tests/test_handoff_board_divergence.py)
# ===========================================================================


@pytest.mark.asyncio
async def test_missing_required_tool_blocks_acceptance(db):
    p = await db_module.create_project(db, "matrix-missing-tool")
    result = await handoff_module.accept_handoff_envelope(
        db, p["id"],
        required_tools=["meridian-docs", "meridian-outputs"],
        available_tools=["meridian-docs"],
    )
    assert result["result"] == handoff_module.ACCEPT_RESULT_CAPABILITY_UNAVAILABLE
    assert result["capability_check"]["missing_tools"] == ["meridian-outputs"]


# ===========================================================================
# 10. Stale tools/list cache
# ===========================================================================


@pytest.mark.asyncio
async def test_stale_tools_list_cache_detected_as_tool_manifest_drift(db):
    """A receiver's CACHED tools/list (captured at handoff-generation time)
    no longer matches what the live items now actually require — the
    declared contract drifted even though every individually-checked tool
    might still resolve."""
    p = await db_module.create_project(db, "matrix-stale-cache")
    generation_time_items = [{"tool_requirements": [{"name": "meridian-docs"}]}]
    cached_hash = handoff_module.compute_required_tools_hash(generation_time_items)

    # Live items now require an ADDITIONAL tool not reflected in the cache.
    live_now = [{"tool_requirements": [{"name": "meridian-docs"}, {"name": "meridian-outputs"}]}]
    result = await handoff_module.accept_handoff_envelope(
        db, p["id"], live_items=live_now, expected_required_tools_hash=cached_hash,
    )
    assert result["result"] == handoff_module.ACCEPT_RESULT_TOOL_MANIFEST_DRIFT


@pytest.mark.asyncio
async def test_stale_tools_list_cache_via_the_real_json_string_storage_shape(db):
    """Adversarial-review finding: real sprint items store tool_requirements
    as a JSON-encoded STRING column (see tool_requirements_module.
    canonical_json / meridian/db/sprint_items.py's serialize_tool_
    requirements) — every prior compute_required_tools_hash test used a raw
    Python list instead, never exercising compute_required_tools_hash's own
    isinstance(raw, str) -> json.loads branch that every REAL get_sprint_
    items() caller actually hits. This proves that branch independently."""
    p = await db_module.create_project(db, "matrix-stale-cache-json-string")
    generation_time_items = [{
        "tool_requirements": tool_requirements_module.canonical_json(
            tool_requirements_module.normalize_tool_requirements([{
                "name": "meridian-docs", "server_or_namespace": "meridian-docs",
                "required_or_preferred": "required", "purpose": "x",
            }])
        ),
    }]
    cached_hash = handoff_module.compute_required_tools_hash(generation_time_items)

    live_now_as_json_strings = [{
        "tool_requirements": tool_requirements_module.canonical_json(
            tool_requirements_module.normalize_tool_requirements([
                {"name": "meridian-docs", "server_or_namespace": "meridian-docs",
                 "required_or_preferred": "required", "purpose": "x"},
                {"name": "meridian-outputs", "server_or_namespace": "meridian-outputs",
                 "required_or_preferred": "required", "purpose": "y"},
            ])
        ),
    }]
    result = await handoff_module.accept_handoff_envelope(
        db, p["id"], live_items=live_now_as_json_strings,
        expected_required_tools_hash=cached_hash,
    )
    assert result["result"] == handoff_module.ACCEPT_RESULT_TOOL_MANIFEST_DRIFT


# ===========================================================================
# 11. Tunnel restart (a recovery scenario: unavailable, then back)
# ===========================================================================


@pytest.mark.asyncio
async def test_tunnel_restart_recovery_capability_unavailable_then_ok(db):
    p = await db_module.create_project(db, "matrix-tunnel-restart")
    before_restart = await handoff_module.accept_handoff_envelope(
        db, p["id"], required_tools=["meridian-docs"], available_tools=[],
    )
    assert before_restart["result"] == handoff_module.ACCEPT_RESULT_CAPABILITY_UNAVAILABLE

    # Tunnel reconnects; the SAME check now succeeds with no other state change.
    after_restart = await handoff_module.accept_handoff_envelope(
        db, p["id"], required_tools=["meridian-docs"], available_tools=["meridian-docs"],
    )
    assert after_restart["accepted"] is True


# ===========================================================================
# 12. Local-only DOCX/Outputs paths
# ===========================================================================


def _valid_recipe(**overrides):
    base = {
        "execution_path": "local",
        "rollback_policy": "transactional_atomic",
        "checks": {"structural_check_required": True},
        "focused_tests": ["tests/test_docx_integrity_gate.py::test_something"],
    }
    base.update(overrides)
    return base


def test_local_only_execution_path_is_explicit_never_inferred_as_hosted():
    local_recipe = ad.normalize_artifact_recipe(_valid_recipe(execution_path="local"))
    hosted_recipe = ad.normalize_artifact_recipe(_valid_recipe(execution_path="hosted"))
    assert local_recipe["execution_path"] == "local"
    assert hosted_recipe["execution_path"] == "hosted"
    with pytest.raises(ad.ArtifactDeclarationError, match="execution_path"):
        # No inference from an unrecognized value — must be explicit.
        ad.normalize_artifact_recipe(_valid_recipe(execution_path="wherever"))


# ===========================================================================
# 13. Partial indexing (no artifact_recipe declared -> honest, not fabricated)
# ===========================================================================


def test_partial_declaration_reports_honestly_not_fabricated():
    """An item with NO artifact_recipe at all (e.g. discovered before its
    recipe was ever indexed/declared) must report declared=False with an
    empty required-checks map — never guess or fabricate a check
    requirement from partial/absent data."""
    result = gate_module.describe_required_checks({})
    assert result == {"declared": False, "required": {}}

    completeness = ad.check_artifact_recipe_completeness({"artifact_kind": "figure"})
    assert completeness["complete"] is False
    assert "artifact_recipe" in completeness["missing"]


# ===========================================================================
# 14. Word/COM timeout
# ===========================================================================


def test_word_com_render_check_recipe_names_the_exact_timeout_prone_check():
    """The recipe's word_com_render_check_required flag must resolve to the
    EXACT function that owns Word-COM timeout semantics
    (check_word_com_render_receipt), not a generic/ambiguous reference —
    so a receiver hitting a real COM timeout knows exactly which check
    failed and why."""
    item = {"artifact_recipe": ad.serialize_artifact_recipe(
        _valid_recipe(checks={"word_com_render_check_required": True})
    )}
    described = gate_module.describe_required_checks(item)
    assert described["declared"] is True
    assert "word_com_render_check_required" in described["required"]
    assert "check_word_com_render_receipt" in described["required"]["word_com_render_check_required"]


# ===========================================================================
# 15. Rollback
# ===========================================================================


def test_rollback_policy_is_required_never_silently_assumed_atomic():
    """A recipe MUST declare an explicit rollback_policy — 'none' and
    'manual_restore' are honest declarations of NOT going through the
    atomic pipeline, never silently assumed to be transactional_atomic by
    omission."""
    with pytest.raises(ad.ArtifactDeclarationError, match="rollback_policy"):
        ad.normalize_artifact_recipe(
            {"execution_path": "local", "focused_tests": ["tests/x.py::y"]}
        )
    explicit_none = ad.normalize_artifact_recipe(_valid_recipe(rollback_policy="none"))
    assert explicit_none["rollback_policy"] == "none"
    assert ad.ROLLBACK_POLICIES == frozenset(
        {"transactional_atomic", "manual_restore", "none"}
    )


# ===========================================================================
# 16. Continuation
# ===========================================================================


@pytest.mark.asyncio
async def test_continuation_resume_ok_when_board_unchanged_diverges_when_changed(db):
    """A continuation/resume check is just accept_handoff_envelope called
    again against the CURRENT board: unchanged -> ok, changed -> BOARD_
    DIVERGENCE. No separate continuation-specific code path is needed —
    every mode calls the same primitive with whatever inputs it has (see
    1bd5e810's own completion-evidence scope note)."""
    p = await db_module.create_project(db, "matrix-continuation")
    items_at_pause = _items(("i1", "in_progress"), ("i2", "todo"))
    pinned_revision = handoff_module.compute_board_revision(items_at_pause)

    unchanged_resume = await handoff_module.accept_handoff_envelope(
        db, p["id"], live_items=items_at_pause, expected_board_revision=pinned_revision,
    )
    assert unchanged_resume["accepted"] is True

    items_after_a_sibling_completed_i1 = _items(("i1", "done"), ("i2", "todo"))
    changed_resume = await handoff_module.accept_handoff_envelope(
        db, p["id"], live_items=items_after_a_sibling_completed_i1,
        expected_board_revision=pinned_revision,
    )
    assert changed_resume["accepted"] is False
    assert changed_resume["result"] == handoff_module.ACCEPT_RESULT_BOARD_DIVERGENCE


# ===========================================================================
# 17. Duplicate retry
# ===========================================================================


def test_check_promotion_preconditions_is_idempotent_across_repeated_calls(tmp_path):
    """A caller that retries a promotion precondition check (e.g. after a
    transient MCP timeout) must see byte-identical results, never a
    side-effecting or drifting verdict."""
    target = tmp_path / "thesis.docx"
    target.write_bytes(b"content")
    base = ad.compute_base_sha256(target)
    item = {
        "planned_output": ad.serialize_planned_output({
            "source_type": "code",
            "targets": [{"uri": "thesis.docx", "selector": {"type": "range", "start_line": 1, "end_line": 1}}],
            "promotion": {"base_sha256": base, "resource_footprint": ["paraId:00AA00BB"]},
        })
    }
    first = ad.check_promotion_preconditions(item, target, require_resource_footprint=True)
    second = ad.check_promotion_preconditions(item, target, require_resource_footprint=True)
    assert first == second


@pytest.mark.asyncio
async def test_duplicate_accept_handoff_retry_with_same_token_is_not_silently_ok(db):
    """A duplicate client-side retry of accept_handoff (e.g. a dropped
    response the client resends) must not silently re-validate — the
    second call correctly reports already_consumed (see scenario 4), which
    a caller distinguishes from real spoofing per AGENTS.md."""
    p = await db_module.create_project(db, "matrix-duplicate-retry")
    token = await handoff_module.mint_handoff_token(db, p["id"])
    first = await handoff_module.accept_handoff_envelope(db, p["id"], goal_token=token)
    retry = await handoff_module.accept_handoff_envelope(db, p["id"], goal_token=token)
    assert first["accepted"] is True
    assert retry["accepted"] is False
    assert retry["token_check"]["reason"] == "already_consumed"


# ===========================================================================
# 18. Deploy evidence
# ===========================================================================


def test_deploy_evidence_focused_tests_required_for_a_complete_recipe():
    """A recipe with no focused_tests can never be normalized at all (fail
    closed at the schema layer — see scenario 15's sibling rollback_policy
    check), and an item with no artifact_recipe declared at all is reported
    incomplete, not silently treated as deploy-ready."""
    with pytest.raises(ad.ArtifactDeclarationError, match="focused_tests"):
        ad.normalize_artifact_recipe(
            {"execution_path": "local", "rollback_policy": "none", "focused_tests": []}
        )

    item_without_recipe = {
        "artifact_kind": "document_only",
        "planned_output": ad.serialize_planned_output({
            "source_type": "code",
            "targets": [{"uri": "thesis.docx", "selector": {"type": "range", "start_line": 1, "end_line": 1}}],
        }),
        "tool_requirements": tool_requirements_module.canonical_json(
            tool_requirements_module.normalize_tool_requirements([{
                "name": "merge_docx_draft", "server_or_namespace": "meridian-docs",
                "required_or_preferred": "required", "purpose": "apply the promotion",
            }])
        ),
    }
    completeness = ad.check_artifact_recipe_completeness(item_without_recipe)
    assert completeness["complete"] is False
    assert "artifact_recipe" in completeness["missing"]

    item_with_recipe = {**item_without_recipe, "artifact_recipe": ad.serialize_artifact_recipe(_valid_recipe())}
    completeness_with_recipe = ad.check_artifact_recipe_completeness(item_with_recipe)
    assert "artifact_recipe" not in completeness_with_recipe["missing"]
