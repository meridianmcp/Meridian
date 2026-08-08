"""Tests for 682005f4 — generate_handoff's 6th mode: mode="goal".

Returns ONLY the bare /goal block — no readiness header, no workspace
decisions/notes, no L0/L1/L2 context. Unlike the pre-existing "starter"/
"compact" mode (which never resolves code pointers at all), this mode reuses
the SAME ``_annotate_code_pointers`` / ``_annotate_resolved_pointers``
enrichment pipeline full/delta already use, and threads each item's resolved
pointer(s) INLINE into the goal block's own ``<sprint_items>`` tag (not the
separate L1 markdown section, which a goal-only mode strips away).

Also covers (c): the starter-mode preview line now honestly reflects "top 3
of N" instead of silently implying only 3 pending items exist.
"""

from __future__ import annotations

import json
import re

import pytest

from meridian import db as db_module
from meridian import handoff as handoff_module


def _assert_starts_with_goal(content: str) -> None:
    """The block starts with '/goal', optionally preceded by the '/loop '
    auto-continue prefix (workspace_settings.loop_enabled_default is True by
    default) — either is a valid bare-goal start."""
    assert re.match(r"^(/loop )?/goal\b", content.strip()), content[:80]


def _sprint_items_tag_body(content: str) -> str:
    """Extract the REAL <sprint_items>...</sprint_items> tag body.

    The SECURITY banner's own prose (see _mint_and_embed_goal_token) contains
    a literal, unclosed "<sprint_items>" substring as plain text ("cross-check
    <sprint_items> against a live get_sprint_items() call..."), which sorts
    BEFORE the real tag. A naive first-index search would therefore capture
    that banner sentence instead of the real tag's contents. Use the LAST
    "<sprint_items>" occurrence, which is always the real opening tag.
    """
    start = content.rindex("<sprint_items>") + len("<sprint_items>")
    end = content.index("</sprint_items>", start)
    return content[start:end]


# ---------------------------------------------------------------------------
# resolve_handoff_mode — the new mode value is recognized
# ---------------------------------------------------------------------------


def test_resolve_handoff_mode_accepts_goal():
    assert handoff_module.resolve_handoff_mode("goal") == "goal"


def test_resolve_handoff_mode_goal_not_confused_with_default():
    # An unrecognized string still falls back to 'full', not 'goal'.
    assert handoff_module.resolve_handoff_mode("gobbledygook") == "full"


# ---------------------------------------------------------------------------
# (a) generate_handoff(mode="goal") — ONLY the bare /goal block
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_handoff_goal_mode_returns_bare_goal_block(db, tmp_path):
    p = await db_module.create_project(db, "goal-mode-bare")
    await db_module.set_goal(
        db, p["id"], "ship it", north_star="Be the best", sprint="sprint-42"
    )
    await db_module.pin_decision(
        db, p["id"], "Use psycopg3", "asyncpg has DLL issues", "TECHNICAL"
    )
    await db_module.add_project_note(
        db, p["id"], "Strat note title", "strat note body", "strategy"
    )
    it = await db_module.add_sprint_item(db, p["id"], "v1", "Ship the bare goal mode")

    path, content, amended = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="goal"
    )

    assert amended is False
    assert path.endswith(f"{handoff_module.handoff_file_stem(p['id'])}_goal.md")

    # It IS a /goal block: directive, sprint items, token, stop conditions.
    _assert_starts_with_goal(content)
    assert "<executor_directive>" in content
    assert "<sprint_items>" in content
    assert it["id"] in content
    assert "<goal_token>" in content
    assert "<stop_conditions>" in content

    # It is NOT the readiness header.
    assert "HANDOFF READINESS" not in content
    # It is NOT the workspace decisions/notes block.
    assert "Workspace (applies to all projects)" not in content
    # It is NOT any L0/L1/L2 context.
    assert "MERIDIAN_CONTEXT" not in content
    assert "L0 — Core Context" not in content
    assert "L1 — Current State" not in content
    assert "L2 — History" not in content
    # Project-level decisions/notes/north-star/sprint text must not leak in.
    assert "Use psycopg3" not in content
    assert "Strat note title" not in content
    assert "Be the best" not in content
    assert "sprint-42" not in content
    assert "ship it" not in content


# ---------------------------------------------------------------------------
# c1ec3517 — CRITICAL: goal-mode is documented as "for a caller that wants
# nothing but the executor-facing directive itself, e.g. to hand straight to
# a fresh sub-agent with zero framing" -- but a fresh sub-agent with zero
# framing has no session_id and no project identity unless the bare /goal
# block carries an explicit self-start bootstrap. These tests prove the fix.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_handoff_goal_mode_is_self_starting(db, tmp_path):
    """The real project id is threaded all the way from generate_handoff into
    an explicit, copy-pasteable start_session(...) call inside <first_step>,
    so a cold receiving session can bootstrap itself without any other
    context (the whole point of mode="goal")."""
    p = await db_module.create_project(db, "goal-mode-self-start")
    await db_module.add_sprint_item(db, p["id"], "v1", "Ship self-starting goal")

    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="goal"
    )
    assert f'start_session(project_id="{p["id"]}"' in content
    assert 'project_name="goal-mode-self-start"' in content
    assert "REQUIRED FIRST CALL" in content
    _assert_starts_with_goal(content)

    start = content.index("<first_step>") + len("<first_step>")
    end = content.index("</first_step>", start)
    first_step = content[start:end]
    assert "start_session" in first_step
    assert 'get_sprint_items(status="pending")' in first_step

    # Still bare -- this fix must not leak readiness header/L0/L1/L2 context
    # back in; that "no framing" contract is the whole reason the bootstrap
    # has to live INSIDE the goal block itself in the first place.
    assert "HANDOFF READINESS" not in content
    assert "MERIDIAN_CONTEXT" not in content


@pytest.mark.asyncio
async def test_generate_handoff_goal_mode_empty_board_still_self_starts(db, tmp_path):
    """Before this fix, an empty pending board rendered NO <first_step> tag at
    all -- a fresh session landing on an empty board via mode="goal" had
    nothing telling it to bootstrap a session either. Must still self-start
    even with nothing pending."""
    p = await db_module.create_project(db, "goal-mode-empty-self-start")
    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="goal"
    )
    assert "<first_step>" in content
    assert f'start_session(project_id="{p["id"]}"' in content


def test_build_quick_start_goal_self_start_bootstrap_opt_in():
    """Unit-level: passing project_id/project_name/identity renders the
    bootstrap; every value is present and the tag stays valid XML text."""
    items = [{"id": "id1", "title": "First"}]
    out = handoff_module._build_quick_start_goal(
        items, project_id="proj-abc", project_name="Acme Widgets", identity="adam",
    )
    assert 'start_session(project_id="proj-abc"' in out
    assert 'project_name="Acme Widgets"' in out
    assert 'human_id="adam"' in out
    assert "REQUIRED FIRST CALL" in out

    import xml.etree.ElementTree as ET

    body = out.split("/goal", 1)[1].lstrip("\n")
    ET.fromstring(f"<goal_root>{body}</goal_root>")  # raises if malformed


def test_build_quick_start_goal_self_start_bootstrap_escapes_xml_specials():
    """A user-authored project_name can contain XML-special characters; the
    rendered /goal must still parse as well-formed XML."""
    import xml.etree.ElementTree as ET

    items = [{"id": "id1", "title": "First"}]
    out = handoff_module._build_quick_start_goal(
        items, project_id="proj-xyz", project_name="R&D <alpha> team",
    )
    body = out.split("/goal", 1)[1].lstrip("\n")
    ET.fromstring(f"<goal_root>{body}</goal_root>")  # raises if malformed
    assert "R&D <alpha> team" not in out  # raw unescaped form must not appear
    assert "R&amp;D &lt;alpha&gt; team" in out


def test_build_quick_start_goal_no_project_id_is_byte_identical_to_legacy():
    """Every pre-existing caller that has not opted into project_id must see
    NO change: the opt-in design must not regress the well-tested full/
    delta/starter surfaces, which already carry their own separate
    start_session mention outside this function (the Jinja "## Start a New
    Session" section and _render_starter_handoff's header line, respectively)."""
    items = [{"id": "id1", "title": "First"}]
    default_call = handoff_module._build_quick_start_goal(items)
    explicit_none = handoff_module._build_quick_start_goal(items, project_id=None)
    assert default_call == explicit_none
    assert "start_session" not in default_call
    assert "REQUIRED FIRST CALL" not in default_call

    # Same guarantee on the empty-board branch.
    empty_default = handoff_module._build_quick_start_goal([])
    empty_explicit_none = handoff_module._build_quick_start_goal([], project_id=None)
    assert empty_default == empty_explicit_none
    assert "<first_step>" not in empty_default


@pytest.mark.asyncio
async def test_generate_handoff_goal_mode_empty_board(db, tmp_path):
    """No pending items: still a clean /goal block, no crash, no headers."""
    p = await db_module.create_project(db, "goal-mode-empty")
    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="goal"
    )
    _assert_starts_with_goal(content)
    assert "<executor_directive>Verify remaining work is complete.</executor_directive>" in content
    assert "HANDOFF READINESS" not in content
    assert "MERIDIAN_CONTEXT" not in content


@pytest.mark.asyncio
async def test_generate_handoff_goal_mode_project_not_found(db, tmp_path):
    with pytest.raises(ValueError):
        await handoff_module.generate_handoff(
            db, "nonexistent-project-id", str(tmp_path), mode="goal"
        )


@pytest.mark.asyncio
async def test_generate_handoff_goal_mode_via_resolve_handoff_mode(db, tmp_path):
    """The router path (resolve_handoff_mode -> generate_handoff) works too."""
    p = await db_module.create_project(db, "goal-mode-router")
    await db_module.add_sprint_item(db, p["id"], "v1", "Routed goal item")
    mode = handoff_module.resolve_handoff_mode("goal")
    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode=mode
    )
    _assert_starts_with_goal(content)
    assert "HANDOFF READINESS" not in content


# ---------------------------------------------------------------------------
# (a)+(b) — mode="goal" reuses the SAME enrichment pipeline as full/delta,
# and inlines resolved pointers into <sprint_items> itself.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_handoff_goal_mode_inlines_resolved_pointers(db, tmp_path):
    p = await db_module.create_project(db, "goal-mode-pointers")
    await db_module.set_goal(db, p["id"], "ship inline pointers in goal mode")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "Fix the migration guard")
    await db_module.add_sprint_item_pointer(
        db,
        p["id"],
        item["id"],
        "code",
        [
            {
                "uri": "file:meridian/db/migrations.py",
                "selector": {"type": "range", "start_line": 100, "end_line": 120},
            }
        ],
        label="guard site",
    )

    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="goal"
    )

    # The pointer must appear INSIDE the <sprint_items> tag itself (not in a
    # separate L1 section, which does not exist in this mode at all).
    sprint_items_block = _sprint_items_tag_body(content)
    assert item["id"] in sprint_items_block
    assert "meridian/db/migrations.py:100-120" in sprint_items_block
    assert "guard site" in sprint_items_block
    # And the surrounding L1 markdown section (full/delta's rendering
    # surface for the same data) genuinely does not exist in this mode.
    assert "Resolved pointers:" not in content


@pytest.mark.asyncio
async def test_generate_handoff_goal_mode_uses_code_pointer_searcher(db, tmp_path):
    """mode='goal' also runs the code-graph search enrichment (91ac0199),
    exactly like full/delta — this is the functional gap 'starter' has."""
    p = await db_module.create_project(db, "goal-mode-searcher")
    await db_module.add_sprint_item(db, p["id"], "v1", "Fix OAuth redirect bug")

    captured: list[str] = []

    def searcher(query):
        captured.append(query)
        return [
            {
                "file": "meridian/hosted.py",
                "function": "oauth_redirect",
                "qualified_name": "hosted.oauth_redirect",
            }
        ]

    # 8a883f60 — evidence_status is a pure-addition output param: passing it
    # must not change anything about `content` (still asserted below), and a
    # caller that doesn't pass it (every OTHER test in this file) must see
    # zero behavior change either.
    evidence_status: dict = {}
    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="goal",
        graph_searcher=searcher, evidence_status=evidence_status,
    )
    assert captured and "oauth" in captured[0]

    # All five capabilities are always reported, even in goal mode.
    assert set(evidence_status) == {
        "code_pointer_enrichment", "resolved_pointer_annotation",
        "freshness_requery", "wave_gate_exclusion", "graph_search_availability",
    }
    # A real, successful searcher call -> verified, not silently "maybe ok".
    assert evidence_status["code_pointer_enrichment"]["status"] == "verified"
    assert evidence_status["graph_search_availability"]["status"] == "verified"
    assert evidence_status["wave_gate_exclusion"]["status"] == "verified"
    # goal mode structurally never re-queries freshness (only full/delta do)
    # -- reported explicitly as skipped, not simply absent.
    assert evidence_status["freshness_requery"]["status"] == "skipped"
    for entry in evidence_status.values():
        assert entry["status"] in {"verified", "skipped", "failed", "degraded"}
        assert entry["reason"]  # exact reason always present, never blank


@pytest.mark.asyncio
async def test_generate_handoff_goal_mode_survives_pointer_resolve_blowup(
    db, tmp_path, monkeypatch
):
    """A resolve failure degrades to no inline pointers, never breaks this
    mandatory handoff mode."""
    p = await db_module.create_project(db, "goal-mode-blowup")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "resilient item")
    await db_module.add_sprint_item_pointer(
        db,
        p["id"],
        item["id"],
        "code",
        [{"uri": "file:x.py", "selector": {"type": "range", "start_line": 1, "end_line": 2}}],
    )

    async def _boom(*_a, **_k):
        raise RuntimeError("pointer fetch exploded")

    monkeypatch.setattr(db_module, "get_sprint_item_pointers", _boom)

    # 8a883f60 — default (non-strict) call: content behavior is UNCHANGED
    # (both assertions below are the pre-existing ones, untouched), but the
    # blowup is now visible as an explicit failed capability with the EXACT
    # underlying reason instead of being indistinguishable from success.
    evidence_status: dict = {}
    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="goal",
        evidence_status=evidence_status,
    )
    assert "resilient item" not in content  # title isn't rendered raw in goal mode...
    assert item["id"] in content  # ...but the id still is, and nothing crashed.

    rpa = evidence_status["resolved_pointer_annotation"]
    assert rpa["status"] == "failed"
    assert "pointer fetch exploded" in rpa["reason"]  # exact cause, not generic
    assert rpa["fallback"]  # an approved fallback is documented

    # strict_evidence=True on the SAME broken state must fail CLOSED instead
    # of silently emitting the plausible-looking-but-incomplete goal above:
    # nothing is written for this call.
    with pytest.raises(handoff_module.HandoffEvidenceRequired) as excinfo:
        await handoff_module.generate_handoff(
            db, p["id"], str(tmp_path), skip_ai_summary=True, mode="goal",
            strict_evidence=True,
        )
    assert any(
        e["capability"] == "resolved_pointer_annotation" for e in excinfo.value.errors
    )
    assert excinfo.value.evidence_status["resolved_pointer_annotation"]["status"] == "failed"


# ---------------------------------------------------------------------------
# _build_goal_pointer_lines — pure renderer helper (unit)
# ---------------------------------------------------------------------------


def test_build_goal_pointer_lines_empty_when_no_items():
    assert handoff_module._build_goal_pointer_lines([]) == ""


def test_build_goal_pointer_lines_skips_items_without_pointers():
    items = [{"id": "abc123", "title": "no pointers here"}]
    assert handoff_module._build_goal_pointer_lines(items) == ""


def test_build_goal_pointer_lines_renders_target_and_label():
    items = [
        {
            "id": "abc123",
            "title": "Fix the thing",
            "resolved_pointers": [
                {
                    "source_type": "code",
                    "label": "the fix site",
                    "targets": ["meridian/db/migrations.py:100-120"],
                }
            ],
        }
    ]
    out = handoff_module._build_goal_pointer_lines(items)
    assert "abc123" in out
    assert "Fix the thing" in out
    assert "the fix site: meridian/db/migrations.py:100-120" in out


def test_build_goal_pointer_lines_multiple_items_and_targets():
    items = [
        {
            "id": "id1",
            "title": "First",
            "resolved_pointers": [{"source_type": "code", "targets": ["a.py:1-2"]}],
        },
        {
            "id": "id2",
            "title": "Second",
            "resolved_pointers": [
                {"source_type": "code", "targets": ["b.py:3-4", "c.py:5-6"]}
            ],
        },
    ]
    out = handoff_module._build_goal_pointer_lines(items)
    assert "id1" in out and "a.py:1-2" in out
    assert "id2" in out and "b.py:3-4" in out and "c.py:5-6" in out


# ---------------------------------------------------------------------------
# Regression: include_pointer_lines defaults off — full/delta's <sprint_items>
# rendering is unaffected (byte-for-byte) by this change.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_mode_sprint_items_unaffected_by_pointer_lines(db, tmp_path):
    p = await db_module.create_project(db, "goal-mode-full-regression")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "Fix the migration guard")
    await db_module.add_sprint_item_pointer(
        db,
        p["id"],
        item["id"],
        "code",
        [{"uri": "file:x.py", "selector": {"type": "range", "start_line": 1, "end_line": 2}}],
    )
    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="full"
    )
    sprint_items_block = _sprint_items_tag_body(content)
    assert sprint_items_block.strip() == f"Complete sprint items: {item['id']}."


def test_build_quick_start_goal_default_no_pointer_lines():
    items = [
        {
            "id": "id1",
            "title": "First",
            "resolved_pointers": [{"source_type": "code", "targets": ["a.py:1-2"]}],
        }
    ]
    out = handoff_module._build_quick_start_goal(items)
    start = out.index("<sprint_items>")
    end = out.index("</sprint_items>")
    assert "a.py:1-2" not in out[start:end]


def test_build_quick_start_goal_include_pointer_lines_opt_in():
    items = [
        {
            "id": "id1",
            "title": "First",
            "resolved_pointers": [{"source_type": "code", "targets": ["a.py:1-2"]}],
        }
    ]
    out = handoff_module._build_quick_start_goal(items, include_pointer_lines=True)
    start = out.index("<sprint_items>")
    end = out.index("</sprint_items>")
    assert "a.py:1-2" in out[start:end]


# ---------------------------------------------------------------------------
# (c) starter-mode preview honestly reflects "top 3 of N" instead of implying
# only 3 pending items exist.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_starter_preview_honestly_labels_top_3_of_n(db, tmp_path):
    p = await db_module.create_project(db, "starter-preview-honest")
    for i in range(5):
        # b0d42ef6 — force=True bypasses the duplicate-title guard, which
        # would otherwise reject near-identical titles like these.
        await db_module.add_sprint_item(
            db, p["id"], "v1", f"pending item {i}", force=True
        )
    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="starter"
    )
    assert "# Pending (top 3 of 5" in content
    # The full batch (with every id) is covered by the next test.


@pytest.mark.asyncio
async def test_starter_preview_plain_header_when_three_or_fewer(db, tmp_path):
    p = await db_module.create_project(db, "starter-preview-small")
    it1 = await db_module.add_sprint_item(db, p["id"], "v1", "only item")
    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="starter"
    )
    assert "\n# Pending\n" in content
    assert "top 3 of" not in content
    assert it1["id"][:8] in content


@pytest.mark.asyncio
async def test_starter_preview_full_batch_includes_all_pending_ids(db, tmp_path):
    """The claimable batch in quick_start_goal always carries every pending
    id, even though the preview list above it only shows 3."""
    p = await db_module.create_project(db, "starter-preview-full-batch")
    ids = []
    for i in range(5):
        it = await db_module.add_sprint_item(
            db, p["id"], "v1", f"pending item {i}", force=True
        )
        ids.append(it["id"])
    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="starter"
    )
    for iid in ids:
        assert iid in content


@pytest.mark.asyncio
async def test_goal_exposes_authoritative_executor_item_id_manifest(db, tmp_path):
    """Executor handoffs expose every claimable ID outside presentation prose."""
    p = await db_module.create_project(db, "executor-item-id-manifest")
    ids = []
    for i in range(5):
        item = await db_module.add_sprint_item(
            db, p["id"], "v1", f"manifest item {i}", force=True
        )
        ids.append(item["id"])

    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="goal"
    )

    match = re.search(
        r'<executor_item_ids count="(?P<count>\d+)">(?P<ids>[^<]+)</executor_item_ids>',
        content,
    )
    assert match is not None
    assert int(match.group("count")) == len(ids)
    assert match.group("ids").split(",") == ids


# ---------------------------------------------------------------------------
# 9c6cac08 (665 follow-up) — deterministic paste-ready serialization and
# scope fidelity, specific to the bare goal-only mode this file covers.
# ---------------------------------------------------------------------------


_GOAL_TOKEN_RE = re.compile(r"<goal_token>[^<]*</goal_token>")


def _strip_goal_token(content: str) -> str:
    return _GOAL_TOKEN_RE.sub("<goal_token>STRIPPED</goal_token>", content)


@pytest.mark.asyncio
async def test_goal_mode_repeated_calls_deterministic_modulo_token(db, tmp_path):
    """Two generate_handoff(mode='goal') calls against IDENTICAL DB state
    (including a durable pointer, which mode='goal' inlines) must be
    byte-identical apart from the single-use goal_token."""
    p = await db_module.create_project(db, "goal-mode-determinism")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "Fix the migration guard")
    await db_module.add_sprint_item_pointer(
        db,
        p["id"],
        item["id"],
        "code",
        [
            {
                "uri": "file:meridian/db/migrations.py",
                "selector": {"type": "range", "start_line": 100, "end_line": 120},
            }
        ],
        label="guard site",
    )

    _, content_a, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="goal"
    )
    _, content_b, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="goal"
    )
    assert content_a != content_b  # tokens differ — fresh nonce every call
    assert _strip_goal_token(content_a) == _strip_goal_token(content_b)


@pytest.mark.asyncio
async def test_goal_mode_scope_equals_requested_pending_items_exactly(db, tmp_path):
    """With no exclusions in play, the claimable batch's id set must equal
    EXACTLY the set get_sprint_items(version=..., pending) returns for that
    version — no fewer (silent drop), no more (silent broadening from
    another version)."""
    p = await db_module.create_project(db, "goal-mode-scope-exact")
    ids_v1 = set()
    for i in range(3):
        it = await db_module.add_sprint_item(
            db, p["id"], "v1", f"v1 item {i}", force=True
        )
        ids_v1.add(it["id"])
    await db_module.add_sprint_item(db, p["id"], "v2", "v2 item, out of scope")

    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="goal", version="v1",
    )
    sprint_items_block = _sprint_items_tag_body(content)
    emitted_ids = {
        tok.strip().rstrip(".") for tok in
        sprint_items_block.split("Complete sprint items:", 1)[-1].split(",")
        if tok.strip().rstrip(".")
    }
    assert emitted_ids == ids_v1


# ---------------------------------------------------------------------------
# 70c10ca3 (b730 follow-up) — _build_artifact_pointer_findings_clause: the
# batch /goal's own ``<artifact_pointer_findings>`` XML surface for 88f82c15's
# warn/strict artifact-pointer verdict enriched with 3196ba0e's readiness
# verification, so a MULTI-item /goal run sees the warning inline too — not
# only when a caller separately requests a single-item build_item_briefing.
# ---------------------------------------------------------------------------


def test_build_artifact_pointer_findings_clause_empty_for_no_data():
    assert handoff_module._build_artifact_pointer_findings_clause([]) == ""
    # No warning active for this item -> still empty.
    assert handoff_module._build_artifact_pointer_findings_clause(
        [{"id": "x", "artifact_pointer_finding": None}]
    ) == ""


def test_build_artifact_pointer_findings_clause_embeds_canonical_json():
    from meridian import pointers as pointers_module

    items = [{
        "id": "item-1",
        "artifact_pointer_finding": {
            "item_id": "item-1",
            "warning_code": "insufficient_pointer_bare_docx",
            "pointer_status": "weak",
            "ready": False,
            "affected_pointer_ids": ["ptr-1"],
            "target_readiness": [{"pointer_id": "ptr-1", "ready": False, "targets": []}],
        },
    }]
    out = handoff_module._build_artifact_pointer_findings_clause(items)
    assert out.startswith("\n<artifact_pointer_findings>")
    assert out.endswith("</artifact_pointer_findings>")
    body = out[len("\n<artifact_pointer_findings>"):-len("</artifact_pointer_findings>")]
    embedded = json.loads(body)
    assert embedded == pointers_module.assemble_artifact_pointer_findings_from_annotated_items(items)


@pytest.mark.asyncio
async def test_generate_handoff_goal_mode_renders_artifact_pointer_findings_clause(db, tmp_path):
    p = await db_module.create_project(db, "goal-mode-artifact-findings")
    item = await db_module.add_sprint_item(
        db, p["id"], "v1", "Regenerate the results table with new benchmark numbers",
        artifact_policy={"artifact_pointer_check": "strict"},
    )
    stored = await db_module.add_sprint_item_pointer(
        db, p["id"], item["id"], "docs",
        [{"uri": "outputs/report.docx",
          "selector": {"type": "range", "start_line": 1, "end_line": 1}}],
    )

    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="goal",
    )
    assert "<artifact_pointer_findings>" in content
    start = content.index("<artifact_pointer_findings>") + len("<artifact_pointer_findings>")
    end = content.index("</artifact_pointer_findings>")
    findings = json.loads(content[start:end])
    assert len(findings) == 1
    finding = findings[0]
    assert finding["item_id"] == item["id"]
    assert finding["warning_code"] == "insufficient_pointer_bare_docx"
    assert finding["pointer_status"] == "weak"
    assert finding["ready"] is False
    assert finding["affected_pointer_ids"] == [str(stored["id"])]


@pytest.mark.asyncio
async def test_generate_handoff_goal_mode_omits_artifact_pointer_findings_when_no_warning(db, tmp_path):
    p = await db_module.create_project(db, "goal-mode-artifact-findings-none")
    await db_module.add_sprint_item(db, p["id"], "v1", "Renumber figure captions")
    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="goal",
    )
    assert "<artifact_pointer_findings>" not in content


@pytest.mark.asyncio
async def test_generate_handoff_goal_mode_artifact_pointer_findings_deterministic(db, tmp_path):
    """Repeated calls against IDENTICAL DB state must render byte-identical
    <artifact_pointer_findings> content — apart from the single-use goal_token
    (the only field allowed to differ)."""
    p = await db_module.create_project(db, "goal-mode-artifact-findings-determinism")
    item = await db_module.add_sprint_item(
        db, p["id"], "v1", "Regenerate the results table with new benchmark numbers",
        artifact_policy={"artifact_pointer_check": "strict"},
    )
    await db_module.add_sprint_item_pointer(
        db, p["id"], item["id"], "docs",
        [{"uri": "outputs/report.docx",
          "selector": {"type": "range", "start_line": 1, "end_line": 1}}],
    )

    _, content_a, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="goal",
    )
    _, content_b, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="goal",
    )
    assert _strip_goal_token(content_a) == _strip_goal_token(content_b)


@pytest.mark.asyncio
async def test_generate_handoff_goal_mode_artifact_pointer_findings_respect_version_scope(
    db, tmp_path
):
    """f9bacd5b (b730 follow-up, final gate) — a version-scoped mode='goal'
    request must not leak an <artifact_pointer_findings> entry from a
    DIFFERENT sprint version, and must not silently drop the entry that
    genuinely belongs to the requested version. Mirrors
    test_goal_mode_scope_equals_requested_pending_items_exactly's own
    version-scope-exactness style, applied to the artifact-pointer-findings
    clause specifically (not previously covered for this clause)."""
    p = await db_module.create_project(db, "goal-mode-artifact-findings-version-scope")
    item_v1 = await db_module.add_sprint_item(
        db, p["id"], "v1", "Regenerate the results table with new benchmark numbers",
    )
    await db_module.add_sprint_item_pointer(
        db, p["id"], item_v1["id"], "docs",
        [{"uri": "outputs/report.docx",
          "selector": {"type": "range", "start_line": 1, "end_line": 1}}],
    )
    item_v2 = await db_module.add_sprint_item(
        db, p["id"], "v2", "Insert a new ablation chart figure into the results section",
    )
    await db_module.add_sprint_item_pointer(
        db, p["id"], item_v2["id"], "docs",
        [{"uri": "outputs/figures/",
          "selector": {"type": "range", "start_line": 1, "end_line": 1}}],
    )

    _, content_v1, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="goal", version="v1",
    )
    findings_v1 = json.loads(
        content_v1[
            content_v1.index("<artifact_pointer_findings>") + len("<artifact_pointer_findings>"):
            content_v1.index("</artifact_pointer_findings>")
        ]
    )
    ids_v1 = {f["item_id"] for f in findings_v1}
    assert item_v1["id"] in ids_v1  # requested-version finding present, not dropped
    assert item_v2["id"] not in ids_v1  # other-version finding absent, not leaked

    _, content_v2, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="goal", version="v2",
    )
    findings_v2 = json.loads(
        content_v2[
            content_v2.index("<artifact_pointer_findings>") + len("<artifact_pointer_findings>"):
            content_v2.index("</artifact_pointer_findings>")
        ]
    )
    ids_v2 = {f["item_id"] for f in findings_v2}
    assert item_v2["id"] in ids_v2
    assert item_v1["id"] not in ids_v2
