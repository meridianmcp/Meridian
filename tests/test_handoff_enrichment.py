"""Tests for 91ac0199 — generate_handoff code-pointer enrichment.

When the codebase is indexed and the per-project setting
``enrich_handoffs_with_code_pointers`` (default True) is on, generate_handoff
runs a code-graph search per pending sprint item and injects exact
{file, function, qualified_name} pointers into each item's handoff block.

The code-graph lives in an out-of-process MCP, so enrichment is driven through
an injectable ``graph_searcher`` callable. These tests inject a stub.
"""

from __future__ import annotations

import pytest

from meridian import db as db_module
from meridian import handoff as handoff_module


# ---------------------------------------------------------------------------
# setting helpers — default True, togglable via executor_config JSON
# ---------------------------------------------------------------------------


def test_setting_defaults_true_when_unset():
    assert handoff_module._code_pointers_enabled(None) is True
    assert handoff_module._code_pointers_enabled({}) is True
    assert handoff_module._code_pointers_enabled({"executor_config": {}}) is True
    assert (
        handoff_module._code_pointers_enabled({"executor_config": None}) is True
    )


def test_setting_respects_explicit_false():
    settings = {"executor_config": {"enrich_handoffs_with_code_pointers": False}}
    assert handoff_module._code_pointers_enabled(settings) is False


def test_setting_respects_explicit_true():
    settings = {"executor_config": {"enrich_handoffs_with_code_pointers": True}}
    assert handoff_module._code_pointers_enabled(settings) is True


def test_setting_stored_in_executor_config_no_migration():
    """The flag rides in the existing executor_config JSON — non-dict configs
    degrade to the default rather than raising."""
    assert handoff_module._code_pointers_enabled(
        {"executor_config": "not-a-dict"}
    ) is True


# ---------------------------------------------------------------------------
# match coercion / pointer normalization
# ---------------------------------------------------------------------------


def test_coerce_match_list_variants():
    assert handoff_module._coerce_match_list(None) == []
    assert handoff_module._coerce_match_list([1, 2]) == [1, 2]
    assert handoff_module._coerce_match_list({"results": [{"a": 1}]}) == [{"a": 1}]
    assert handoff_module._coerce_match_list({"matches": [{"b": 2}]}) == [{"b": 2}]
    # bare match dict becomes a one-element list
    assert handoff_module._coerce_match_list({"file": "x.py"}) == [{"file": "x.py"}]
    assert handoff_module._coerce_match_list("garbage") == []


def test_normalize_code_pointer_field_aliases():
    ptr = handoff_module._normalize_code_pointer(
        {"file_path": "meridian/db/__init__.py", "name": "get_goal"}
    )
    assert ptr == {
        "file": "meridian/db/__init__.py",
        "function": "get_goal",
        "qualified_name": "get_goal",
    }


def test_normalize_code_pointer_prefers_qualified_name():
    ptr = handoff_module._normalize_code_pointer(
        {
            "file": "a.py",
            "function": "foo",
            "qualified_name": "mod.Cls.foo",
        }
    )
    assert ptr["qualified_name"] == "mod.Cls.foo"


def test_normalize_code_pointer_returns_none_when_empty():
    assert handoff_module._normalize_code_pointer({}) is None
    assert handoff_module._normalize_code_pointer("nope") is None


# ---------------------------------------------------------------------------
# enrichment annotation (unit, no DB)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_annotate_code_pointers_injects_top_match():
    items = [{"id": "i1", "title": "Fix OAuth redirect bug", "status": "pending"}]

    def searcher(query):
        return [
            {
                "file": "meridian/hosted.py",
                "function": "oauth_redirect",
                "qualified_name": "hosted.oauth_redirect",
            },
            {"file": "other.py", "function": "ignored"},
        ]

    out = await handoff_module._annotate_code_pointers(items, searcher)
    assert out[0]["code_pointers"] == [
        {
            "file": "meridian/hosted.py",
            "function": "oauth_redirect",
            "qualified_name": "hosted.oauth_redirect",
        }
    ]


@pytest.mark.asyncio
async def test_annotate_code_pointers_awaits_coroutine_searcher():
    items = [{"id": "i1", "title": "wire pg adapter cursor", "status": "pending"}]

    async def searcher(query):
        return {"results": [{"file": "meridian/pg_adapter.py", "name": "cursor"}]}

    out = await handoff_module._annotate_code_pointers(items, searcher)
    assert out[0]["code_pointers"][0]["file"] == "meridian/pg_adapter.py"


@pytest.mark.asyncio
async def test_annotate_code_pointers_none_searcher_is_noop():
    items = [{"id": "i1", "title": "anything here", "status": "pending"}]
    out = await handoff_module._annotate_code_pointers(items, None)
    assert "code_pointers" not in out[0]


@pytest.mark.asyncio
async def test_annotate_code_pointers_never_raises_on_searcher_error():
    items = [{"id": "i1", "title": "explode please now", "status": "pending"}]

    def searcher(query):
        raise RuntimeError("graph backend down")

    out = await handoff_module._annotate_code_pointers(items, searcher)
    # Degrades to no pointer — must not propagate.
    assert "code_pointers" not in out[0]


@pytest.mark.asyncio
async def test_annotate_code_pointers_no_match_leaves_item_untouched():
    items = [{"id": "i1", "title": "some pending item", "status": "pending"}]
    out = await handoff_module._annotate_code_pointers(items, lambda q: [])
    assert "code_pointers" not in out[0]


# ---------------------------------------------------------------------------
# end-to-end via generate_handoff + template rendering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_handoff_injects_pointers_when_indexed(db, tmp_path):
    p = await db_module.create_project(db, "enrich-on")
    await db_module.set_goal(db, p["id"], "ship enrichment")
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

    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, graph_searcher=searcher
    )
    # The graph was actually queried with keywords from the item title.
    assert captured and "oauth" in captured[0]
    # The template renders a Code pointers section with the qualified name + file.
    assert "Code pointers:" in content
    assert "hosted.oauth_redirect" in content
    assert "meridian/hosted.py" in content


@pytest.mark.asyncio
async def test_generate_handoff_no_pointers_when_setting_off(db, tmp_path):
    p = await db_module.create_project(db, "enrich-off")
    await db_module.set_goal(db, p["id"], "ship enrichment")
    await db_module.add_sprint_item(db, p["id"], "v1", "Fix OAuth redirect bug")
    # Turn the flag off via executor_config (no migration / new column).
    await db_module.update_project_settings(
        db,
        p["id"],
        executor_config={"enrich_handoffs_with_code_pointers": False},
    )

    called = {"n": 0}

    def searcher(query):
        called["n"] += 1
        return [{"file": "x.py", "function": "y", "qualified_name": "y"}]

    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, graph_searcher=searcher
    )
    # Searcher must not be consulted at all when the setting is off.
    assert called["n"] == 0
    assert "Code pointers:" not in content


@pytest.mark.asyncio
async def test_generate_handoff_no_pointers_when_not_indexed(db, tmp_path):
    """No searcher (unindexed / code intel off) ⇒ graceful, no pointers, no
    crash. This is the default behaviour with no graph_searcher injected."""
    p = await db_module.create_project(db, "enrich-unindexed")
    await db_module.set_goal(db, p["id"], "ship enrichment")
    await db_module.add_sprint_item(db, p["id"], "v1", "Fix OAuth redirect bug")

    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True
    )
    assert "Code pointers:" not in content
    # Item itself still renders normally.
    assert "Fix OAuth redirect bug" in content


@pytest.mark.asyncio
async def test_generate_handoff_survives_searcher_blowup(db, tmp_path):
    """A searcher that raises must never break the mandatory handoff."""
    p = await db_module.create_project(db, "enrich-boom")
    await db_module.set_goal(db, p["id"], "ship enrichment")
    await db_module.add_sprint_item(db, p["id"], "v1", "Fix OAuth redirect bug")

    def searcher(query):
        raise RuntimeError("graph down")

    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, graph_searcher=searcher
    )
    # Handoff still produced; just no pointers.
    assert "MERIDIAN_CONTEXT" in content
    assert "Code pointers:" not in content


# ---------------------------------------------------------------------------
# c96577b3 — a skipped enrichment is SURFACED, not silently omitted. A
# zero-pointer handoff must be distinguishable from "enrichment not attempted".
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_handoff_surfaces_skip_note_when_no_searcher(db, tmp_path):
    """Enrichment enabled (default) but no live tunnel/graph searcher ⇒ the
    handoff surfaces a visible skip reason where the pointers would otherwise be,
    instead of silently omitting them."""
    p = await db_module.create_project(db, "enrich-skip-note")
    await db_module.set_goal(db, p["id"], "ship enrichment")
    await db_module.add_sprint_item(db, p["id"], "v1", "Fix OAuth redirect bug")

    # No graph_searcher injected, and no resolver registered ⇒ searcher is None.
    # Explicitly clear any resolver leaked from another test on this worker so the
    # searcher deterministically resolves to None.
    handoff_module.set_graph_searcher_resolver(None)
    try:
        _, content, _ = await handoff_module.generate_handoff(
            db, p["id"], str(tmp_path), skip_ai_summary=True
        )
    finally:
        handoff_module.set_graph_searcher_resolver(None)
    assert "enrichment skipped" in content
    assert "no live tunnel/graph searcher" in content
    # No actual pointers were produced.
    assert "Code pointers:" not in content


@pytest.mark.asyncio
async def test_generate_handoff_surfaces_skip_note_when_searcher_errors(db, tmp_path):
    """When the searcher blows up, the skip note is surfaced too (not silent)."""
    p = await db_module.create_project(db, "enrich-skip-err")
    await db_module.set_goal(db, p["id"], "ship enrichment")
    await db_module.add_sprint_item(db, p["id"], "v1", "Fix OAuth redirect bug")

    # A searcher that raises for EVERY query drives the annotate helper to raise
    # out (it swallows per-item errors, so force a top-level failure by making the
    # helper itself unusable via a non-callable object the branch will try to use).
    class _Boom:
        def __call__(self, query):
            raise RuntimeError("graph backend unreachable")

    # Monkeypatch _annotate_code_pointers to raise so the outer guard trips.
    import meridian.handoff as _h

    async def _raise(*a, **k):
        raise RuntimeError("enrichment pass exploded")

    orig = _h._annotate_code_pointers
    _h._annotate_code_pointers = _raise
    try:
        _, content, _ = await handoff_module.generate_handoff(
            db, p["id"], str(tmp_path), skip_ai_summary=True, graph_searcher=_Boom()
        )
    finally:
        _h._annotate_code_pointers = orig

    assert "enrichment skipped" in content
    assert "graph searcher errored" in content
    assert "MERIDIAN_CONTEXT" in content


# ---------------------------------------------------------------------------
# 10bfe531 — source_type-aware prospecting: MANUAL items are skipped, and non-code
# items are prospected via injected backends into a validated generic pointer
# (2976e168 primitive) instead of code_pointers.
# ---------------------------------------------------------------------------


def test_infer_pointer_source_type_classifies():
    infer = handoff_module._infer_pointer_source_type
    assert infer({"title": "Fix OAuth redirect bug"}) == "code"
    assert infer({"title": "Wire Zotero citation keys into the brief"}) == "citation"
    assert infer({"title": "get_document_structure docx outline persistence"}) == "docs"
    assert infer({"title": "Run the DocBank benchmark ablation"}) == "experiment"
    # tags participate in the classification too
    assert infer({"title": "misc", "tags": "latex,structure"}) == "docs"


@pytest.mark.asyncio
async def test_annotate_skips_manual_items():
    """A MANUAL/human item must never be prospected — no misleading pointer, and the
    searcher is not even consulted (verified fix for the pre-10bfe531 behaviour where
    MANUAL items reached the code-pointer pass)."""
    called: list[str] = []

    def searcher(query):
        called.append(query)
        return [{"file": "x.py", "function": "y", "qualified_name": "y"}]

    manual_by_title = {"id": "m1", "title": "MANUAL (Adam): form an LLC", "status": "pending"}
    # 943afe1e — a genuine manual signal is blocker_kind=='manual' (a real-world
    # action outside Meridian), NOT the mere presence of human_id (who is assigned).
    manual_by_blocker = {"id": "m2", "title": "install the binary", "blocker_kind": "manual", "status": "pending"}
    manual_by_milestone = {"id": "m3", "title": "capture screenshots", "milestone_type": "human", "status": "pending"}
    out = await handoff_module._annotate_code_pointers(
        [manual_by_title, manual_by_blocker, manual_by_milestone], searcher
    )
    assert called == []  # searcher never consulted for MANUAL items
    for it in out:
        assert "code_pointers" not in it
        assert "pointer_source_type" not in it


@pytest.mark.asyncio
async def test_annotate_non_code_item_uses_injected_prospector():
    """A non-code item is prospected with the matching injected backend and gets a
    validated generic pointer (not code_pointers)."""
    item = {
        "id": "c1",
        "title": "Wire Zotero citation keys into the planning brief",
        "status": "pending",
    }

    def citation_prospector(query):
        return [{
            "uri": "zotero://select/items/ABCD1234",
            "selector": {"type": "zotero_key", "key": "ABCD1234"},
            "label": "Kalai et al 2025",
        }]

    out = await handoff_module._annotate_code_pointers(
        [item], searcher=None, prospectors={"citation": citation_prospector}
    )
    assert out[0]["pointer_source_type"] == "citation"
    assert "code_pointers" not in out[0]
    ptr = out[0]["pointers"][0]
    assert ptr["source_type"] == "citation"
    assert ptr["targets"][0]["selector"]["key"] == "ABCD1234"
    assert ptr["label"] == "Kalai et al 2025"


@pytest.mark.asyncio
async def test_annotate_non_code_item_without_backend_is_not_mis_prospected():
    """A non-code item with only the code searcher wired gets NO pointer (never a
    spurious code pointer), but its inferred source_type is still recorded."""
    item = {
        "id": "d1",
        "title": "PAPER: OOXML-Graph docx structure benchmark",
        "status": "pending",
    }

    def code_searcher(query):
        return [{
            "file": "meridian/docs_intel.py",
            "function": "document_outline",
            "qualified_name": "document_outline",
        }]

    out = await handoff_module._annotate_code_pointers([item], code_searcher)
    assert out[0]["pointer_source_type"] in ("docs", "experiment", "citation")
    assert "code_pointers" not in out[0]
    assert "pointers" not in out[0]


# ---------------------------------------------------------------------------
# 182468a6 — deeper prospecting: touches_resources targeting, match validation,
# re-prospecting, and a surfaced (not silent) enrichment cap.
# ---------------------------------------------------------------------------


def test_touches_resource_terms_strips_prefixes():
    item = {"touches_resources": [
        "inferred:file:meridian/mcp/handler.py",
        "file:meridian/handoff.py:_render_starter_handoff",
        "db:sprint_items",
    ]}
    terms = handoff_module._touches_resource_terms(item)
    assert terms == [
        "meridian/mcp/handler.py",
        "meridian/handoff.py:_render_starter_handoff",
        "sprint_items",
    ]
    # JSON-string storage shape is tolerated too.
    import json as _json
    item2 = {"touches_resources": _json.dumps(["inferred:file:a/b.py"])}
    assert handoff_module._touches_resource_terms(item2) == ["a/b.py"]
    # Garbage / empty → no terms, never raises.
    assert handoff_module._touches_resource_terms({"touches_resources": None}) == []
    assert handoff_module._touches_resource_terms({"touches_resources": "not json"}) == ["not json"]


@pytest.mark.asyncio
async def test_annotate_prospects_from_touches_resources_when_present():
    """182468a6 — the query targets the declared touches_resources, not the title
    keywords, when resources are present."""
    seen: list[str] = []

    def searcher(query):
        seen.append(query)
        return [{"file": "meridian/db/__init__.py", "function": "x", "qualified_name": "x"}]

    item = {
        "id": "i1", "status": "pending",
        "title": "terse title with no useful keywords",
        "touches_resources": ["inferred:file:meridian/db/__init__.py"],
    }
    out = await handoff_module._annotate_code_pointers([item], searcher)
    assert seen == ["meridian/db/__init__.py"]  # queried the resource, not the title
    assert out[0]["code_pointers"][0]["file"] == "meridian/db/__init__.py"
    assert out[0]["prospect_status"] == "prospected"


@pytest.mark.asyncio
async def test_annotate_validates_matches_skips_locationless_first():
    """182468a6 — a first match with no usable location is skipped for the first
    match that validates, instead of yielding None from matches[0]."""
    def searcher(query):
        return [
            {"score": 0.9},  # no file/function/qualified — unusable
            {"file": "meridian/server.py", "function": "route", "qualified_name": "route"},
        ]

    item = {"id": "i1", "title": "add a route to server", "status": "pending"}
    out = await handoff_module._annotate_code_pointers([item], searcher)
    assert out[0]["code_pointers"][0]["file"] == "meridian/server.py"
    assert out[0]["prospect_status"] == "prospected"


@pytest.mark.asyncio
async def test_annotate_no_reprospect_by_default_but_reprospect_overwrites():
    """182468a6 — an item that already has a pointer is left alone by default
    (status 'cached'), but reprospect=True re-runs and can correct it."""
    item = {
        "id": "i1", "title": "fix the widget", "status": "pending",
        "code_pointers": [{"file": "old.py", "function": "stale", "qualified_name": "stale"}],
    }

    def searcher(query):
        return [{"file": "new.py", "function": "fresh", "qualified_name": "fresh"}]

    # Default: cached, untouched.
    out = await handoff_module._annotate_code_pointers([dict(item)], searcher)
    assert out[0]["code_pointers"][0]["file"] == "old.py"
    assert out[0]["prospect_status"] == "cached"

    # reprospect=True: overwritten with the fresh match.
    out2 = await handoff_module._annotate_code_pointers([dict(item)], searcher, reprospect=True)
    assert out2[0]["code_pointers"][0]["file"] == "new.py"
    assert out2[0]["prospect_status"] == "prospected"


@pytest.mark.asyncio
async def test_annotate_surfaces_cap_beyond_limit():
    """182468a6 — items beyond the enrichment cap are marked skipped_cap (not
    silently ignored), so the caller can report partial coverage."""
    cap = handoff_module._MAX_ENRICHED_ITEMS
    items = [
        {"id": f"i{n}", "title": f"pending item {n}", "status": "pending"}
        for n in range(cap + 3)
    ]

    def searcher(query):
        return [{"file": "f.py", "function": "g", "qualified_name": "g"}]

    out = await handoff_module._annotate_code_pointers(items, searcher)
    # The overflow items carry the cap signal...
    assert all(it.get("prospect_status") == "skipped_cap" for it in out[cap:])
    # ...and were NOT prospected.
    assert all("code_pointers" not in it for it in out[cap:])
    # ...while in-cap items were prospected.
    assert out[0]["prospect_status"] == "prospected"


# ---------------------------------------------------------------------------
# 88f82c15 (b730 follow-up) — artifact_pointer_policy wiring:
# _annotate_resolved_pointers attaches the warn/strict policy verdict
# (pointers.evaluate_artifact_pointer_policy) per pending item; the readiness
# block surfaces a BLOCKING line for strict-mode violations;
# build_item_briefing renders a per-item <artifact_pointer_policy> clause.
# ---------------------------------------------------------------------------

import json as _json


@pytest.mark.asyncio
async def test_annotate_resolved_pointers_attaches_artifact_pointer_policy(db):
    """Computed even with ZERO stored durable pointers — 'missing_pointer'
    is itself a possible verdict."""
    p = await db_module.create_project(db, "artifact-policy-annotate")
    item = await db_module.add_sprint_item(
        db, p["id"], "v1", "Insert a new ablation chart figure into the results",
        artifact_policy={"artifact_pointer_check": "warn"},
    )
    items = [{"id": item["id"], "title": item["title"], "artifact_policy": item["artifact_policy"]}]
    out = await handoff_module._annotate_resolved_pointers(db, p["id"], items)
    policy_result = out[0]["artifact_pointer_policy"]
    assert policy_result["item_id"] == item["id"]
    assert policy_result["warning_code"] == "missing_pointer"
    assert policy_result["ready"] is True


@pytest.mark.asyncio
async def test_annotate_resolved_pointers_flags_durable_bare_docx_pointer_as_strict_blocking(db):
    """A durable sprint_item_pointer whose target is a bare .docx path is
    INSUFFICIENT figure/table evidence; under a strict policy the item is
    reported not-ready, and the durable pointer's own id is named."""
    p = await db_module.create_project(db, "artifact-policy-strict")
    item = await db_module.add_sprint_item(
        db, p["id"], "v1", "Regenerate the results table with new benchmark numbers",
        artifact_policy={"artifact_pointer_check": "strict"},
    )
    stored = await db_module.add_sprint_item_pointer(
        db, p["id"], item["id"], "docs",
        [{
            "uri": "outputs/report.docx",
            "selector": {"type": "range", "start_line": 1, "end_line": 1},
        }],
    )
    items = [{"id": item["id"], "title": item["title"], "artifact_policy": item["artifact_policy"]}]
    out = await handoff_module._annotate_resolved_pointers(db, p["id"], items)
    policy_result = out[0]["artifact_pointer_policy"]
    assert policy_result["warning_code"] == "insufficient_pointer_bare_docx"
    assert policy_result["ready"] is False
    assert policy_result["affected_pointer_ids"] == [str(stored["id"])]


@pytest.mark.asyncio
async def test_annotate_resolved_pointers_not_sensitive_item_no_policy_warning(db):
    p = await db_module.create_project(db, "artifact-policy-safe")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "Renumber figure captions")
    items = [{"id": item["id"], "title": item["title"]}]
    out = await handoff_module._annotate_resolved_pointers(db, p["id"], items)
    policy_result = out[0]["artifact_pointer_policy"]
    assert policy_result["warning_code"] is None
    assert policy_result["ready"] is True


# ---------------------------------------------------------------------------
# _build_artifact_policy_blocking_warnings — pure, no DB needed
# ---------------------------------------------------------------------------


def test_build_artifact_policy_blocking_warnings_strict_mode_blocks():
    items = [{
        "id": "blk-1",
        "title": "Insert a new ablation chart figure",
        "artifact_policy": _json.dumps({"artifact_pointer_check": "strict"}),
    }]
    warnings = handoff_module._build_artifact_policy_blocking_warnings(items)
    assert len(warnings) == 1
    assert "blk-1" in warnings[0]
    assert "NOT EXECUTABLE" in warnings[0]


def test_build_artifact_policy_blocking_warnings_warn_mode_does_not_block():
    items = [{
        "id": "blk-2",
        "title": "Insert a new ablation chart figure",
        "artifact_policy": _json.dumps({"artifact_pointer_check": "warn"}),
    }]
    assert handoff_module._build_artifact_policy_blocking_warnings(items) == []


def test_build_artifact_policy_blocking_warnings_off_mode_does_not_block():
    items = [{
        "id": "blk-3",
        "title": "Insert a new ablation chart figure",
        "artifact_policy": _json.dumps({"artifact_pointer_check": "off"}),
    }]
    assert handoff_module._build_artifact_policy_blocking_warnings(items) == []


def test_build_artifact_policy_blocking_warnings_false_positive_document_only():
    items = [{
        "id": "blk-4",
        "title": "Insert a new ablation chart figure",
        "artifact_kind": "document_only",
        "artifact_policy": _json.dumps({"artifact_pointer_check": "strict"}),
    }]
    assert handoff_module._build_artifact_policy_blocking_warnings(items) == []


def test_build_artifact_policy_blocking_warnings_never_raises_on_bad_input():
    assert handoff_module._build_artifact_policy_blocking_warnings(None) == []
    assert handoff_module._build_artifact_policy_blocking_warnings("not-a-list") == []  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# _build_readiness_block — artifact_policy_blocking rendering
# ---------------------------------------------------------------------------


def test_build_readiness_block_renders_blocking_lines_and_summary():
    block = handoff_module._build_readiness_block(
        "week-1", 2, 1,
        artifact_policy_blocking=["✗ NOT EXECUTABLE — item x1 [figure] fails strict artifact_pointer_check (missing_pointer): fix it"],
    )
    assert "HANDOFF NOT READY" in block
    assert "item x1" in block
    warn_idx = block.index("HANDOFF NOT READY")
    close_idx = block.index("=========================")
    assert warn_idx < close_idx


def test_build_readiness_block_backward_compatible_without_artifact_policy_blocking():
    block = handoff_module._build_readiness_block("week-1", 2, 1)
    assert "=== HANDOFF READINESS ===" in block
    assert "HANDOFF NOT READY" not in block


# ---------------------------------------------------------------------------
# build_item_briefing — <artifact_pointer_policy> clause
# ---------------------------------------------------------------------------


def _extract_clause(briefing: str, tag: str):
    open_tag, close_tag = f"<{tag}>", f"</{tag}>"
    if open_tag not in briefing:
        return None
    start = briefing.index(open_tag) + len(open_tag)
    end = briefing.index(close_tag)
    return _json.loads(briefing[start:end])


def test_build_item_briefing_renders_artifact_pointer_policy_when_active():
    item = {
        "id": "item-uuid", "title": "Insert a new ablation chart figure",
        "artifact_policy": _json.dumps({"artifact_pointer_check": "strict"}),
    }
    briefing = handoff_module.build_item_briefing(item)
    embedded = _extract_clause(briefing, "artifact_pointer_policy")
    assert embedded is not None
    assert embedded["warning_code"] == "missing_pointer"
    assert embedded["ready"] is False


def test_build_item_briefing_omits_artifact_pointer_policy_when_off():
    item = {
        "id": "item-uuid", "title": "Insert a new ablation chart figure",
        "artifact_policy": _json.dumps({"artifact_pointer_check": "off"}),
    }
    briefing = handoff_module.build_item_briefing(item)
    assert "<artifact_pointer_policy>" not in briefing


def test_build_item_briefing_omits_artifact_pointer_policy_when_not_sensitive():
    item = {"id": "item-uuid", "title": "Renumber figure captions"}
    briefing = handoff_module.build_item_briefing(item)
    assert "<artifact_pointer_policy>" not in briefing


def test_build_item_briefing_omits_artifact_pointer_policy_when_sufficient_evidence():
    item = {
        "id": "item-uuid", "title": "Insert a new ablation chart figure",
        "touches_resources": _json.dumps(["file:outputs/figures/ablation.png"]),
        "artifact_policy": _json.dumps({"artifact_pointer_check": "strict"}),
    }
    briefing = handoff_module.build_item_briefing(item)
    assert "<artifact_pointer_policy>" not in briefing
