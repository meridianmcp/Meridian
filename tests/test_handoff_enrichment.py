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

    _, content = await handoff_module.generate_handoff(
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

    _, content = await handoff_module.generate_handoff(
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

    _, content = await handoff_module.generate_handoff(
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

    _, content = await handoff_module.generate_handoff(
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
        _, content = await handoff_module.generate_handoff(
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
        _, content = await handoff_module.generate_handoff(
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
    manual_by_human = {"id": "m2", "title": "install the binary", "human_id": "adam", "status": "pending"}
    manual_by_milestone = {"id": "m3", "title": "capture screenshots", "milestone_type": "human", "status": "pending"}
    out = await handoff_module._annotate_code_pointers(
        [manual_by_title, manual_by_human, manual_by_milestone], searcher
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
