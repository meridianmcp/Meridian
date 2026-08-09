"""Tests for e5a7ce7f — declarative deterministic tool-routing rules with
advisory semantic reranking and fail-closed mutation gates.

Covers:
  1. Schema validation for ``.meridian/tool-routing.toml``
     (normalize_rule / normalize_routing_config) — fail-closed on
     malformed input, deterministic ordering, secret/path rejection.
  2. load_routing_config — absence vs. malformed-file behavior.
  3. route()'s 5-layer priority chain, layer by layer and combined.
  4. The hard invariants: semantic reranking never removes a candidate,
     never authorizes a write, and never runs at all for code-intel/docx
     requests (answered at the category-match layer first).
  5. authorize_mutation() — the fail-closed mutation gate.
"""
from __future__ import annotations

import pytest

from meridian import tool_routing as tr


# ---------------------------------------------------------------------------
# Part 1 — normalize_rule / normalize_routing_config
# ---------------------------------------------------------------------------


def _valid_rule(**overrides):
    base = {
        "id": "docx-editing",
        "priority": 10,
        "match_keywords": ["docx"],
        "category": "docx",
    }
    base.update(overrides)
    return base


def test_normalize_rule_minimal_valid():
    normalized = tr.normalize_rule(_valid_rule())
    assert normalized["id"] == "docx-editing"
    assert normalized["priority"] == 10
    assert normalized["match_keywords"] == ["docx"]
    assert normalized["category"] == "docx"
    assert normalized["tool"] is None
    assert normalized["required_tools"] == []
    assert normalized["fallback_chain"] == []


def test_normalize_rule_rejects_unknown_field():
    with pytest.raises(tr.ToolRoutingConfigError, match="unknown rule field"):
        tr.normalize_rule(_valid_rule(bogus_field="x"))


def test_normalize_rule_requires_id_and_priority():
    with pytest.raises(tr.ToolRoutingConfigError, match="missing required field: id"):
        tr.normalize_rule({"priority": 1, "match_keywords": ["x"], "category": "docx"})
    with pytest.raises(tr.ToolRoutingConfigError, match="missing required field: priority"):
        tr.normalize_rule({"id": "x", "match_keywords": ["x"], "category": "docx"})


def test_normalize_rule_priority_must_be_int_not_bool():
    with pytest.raises(tr.ToolRoutingConfigError, match="priority must be an integer"):
        tr.normalize_rule(_valid_rule(priority=True))


def test_normalize_rule_requires_match_keywords_or_pattern():
    with pytest.raises(tr.ToolRoutingConfigError, match="match_keywords/match_pattern"):
        tr.normalize_rule({"id": "x", "priority": 1, "category": "docx"})


def test_normalize_rule_requires_category_or_tool():
    with pytest.raises(tr.ToolRoutingConfigError, match="category/tool"):
        tr.normalize_rule({"id": "x", "priority": 1, "match_keywords": ["docx"]})


def test_normalize_rule_rejects_invalid_regex():
    with pytest.raises(tr.ToolRoutingConfigError, match="not a valid regex"):
        tr.normalize_rule(_valid_rule(match_keywords=None, match_pattern="(unclosed"))


def test_normalize_rule_accepts_valid_pattern():
    normalized = tr.normalize_rule(
        _valid_rule(match_keywords=None, match_pattern=r"(?i)find (the )?symbol")
    )
    assert normalized["match_pattern"] == r"(?i)find (the )?symbol"


def test_normalize_rule_rejects_secret_shaped_value():
    with pytest.raises(tr.ToolRoutingConfigError):
        tr.normalize_rule(_valid_rule(notes="use bearer sk-abcdefghijklmno123"))


def test_normalize_rule_rejects_absolute_local_path():
    with pytest.raises(tr.ToolRoutingConfigError):
        tr.normalize_rule(_valid_rule(tool=r"C:\Users\adam\secret_tool.exe", category=None))


def test_normalize_routing_config_defaults():
    cfg = tr.normalize_routing_config({})
    assert cfg["mode"] == "shadow"
    assert cfg["confidence_threshold"] == tr._DEFAULT_CONFIDENCE_THRESHOLD
    assert cfg["rules"] == []


def test_normalize_routing_config_rejects_unknown_top_level():
    with pytest.raises(tr.ToolRoutingConfigError, match="unknown top-level"):
        tr.normalize_routing_config({"bogus": {}})


def test_normalize_routing_config_rejects_bad_mode():
    with pytest.raises(tr.ToolRoutingConfigError, match="routing\\].mode"):
        tr.normalize_routing_config({"routing": {"mode": "yolo"}})


def test_normalize_routing_config_rejects_out_of_range_confidence():
    with pytest.raises(tr.ToolRoutingConfigError, match="confidence_threshold"):
        tr.normalize_routing_config({"routing": {"confidence_threshold": 1.5}})


def test_normalize_routing_config_rejects_duplicate_rule_ids():
    with pytest.raises(tr.ToolRoutingConfigError, match="duplicate rule id"):
        tr.normalize_routing_config({
            "rules": [_valid_rule(id="dup"), _valid_rule(id="dup", match_keywords=["other"])],
        })


def test_normalize_routing_config_deterministic_priority_ordering():
    """Rules always come back sorted (-priority, id) regardless of input order."""
    raw = {
        "rules": [
            _valid_rule(id="low", priority=1),
            _valid_rule(id="high", priority=100),
            _valid_rule(id="mid-b", priority=50),
            _valid_rule(id="mid-a", priority=50),
        ],
    }
    cfg = tr.normalize_routing_config(raw)
    ids_in_order = [r["id"] for r in cfg["rules"]]
    assert ids_in_order == ["high", "mid-a", "mid-b", "low"]


def test_normalize_routing_config_identical_input_normalizes_identically():
    raw = {"rules": [_valid_rule(id="a", priority=5), _valid_rule(id="b", priority=9)]}
    first = tr.normalize_routing_config(raw)
    second = tr.normalize_routing_config(raw)
    assert first == second


# ---------------------------------------------------------------------------
# Part 2 — load_routing_config (filesystem)
# ---------------------------------------------------------------------------


def test_load_routing_config_returns_none_when_absent(tmp_path):
    assert tr.load_routing_config(tmp_path / "nonexistent.toml") is None


def test_load_routing_config_valid_file(tmp_path):
    p = tmp_path / "tool-routing.toml"
    p.write_text(
        '[routing]\nmode = "advisory"\nconfidence_threshold = 0.8\n\n'
        '[[rules]]\nid = "docx-editing"\npriority = 100\n'
        'match_keywords = ["docx", "word document"]\ncategory = "docx"\n'
        'tool = "meridian-docs"\nrequired_tools = ["meridian-docs"]\n'
        'fallback_chain = ["Serena"]\n',
        encoding="utf-8",
    )
    cfg = tr.load_routing_config(p)
    assert cfg["mode"] == "advisory"
    assert cfg["confidence_threshold"] == 0.8
    assert cfg["rules"][0]["id"] == "docx-editing"
    assert cfg["rules"][0]["tool"] == "meridian-docs"


def test_load_routing_config_fails_closed_on_malformed_toml(tmp_path):
    p = tmp_path / "tool-routing.toml"
    p.write_text("this is [ not valid toml", encoding="utf-8")
    with pytest.raises(tr.ToolRoutingConfigError, match="invalid TOML"):
        tr.load_routing_config(p)


def test_load_routing_config_fails_closed_on_schema_violation(tmp_path):
    p = tmp_path / "tool-routing.toml"
    p.write_text('[routing]\nmode = "not-a-real-mode"\n', encoding="utf-8")
    with pytest.raises(tr.ToolRoutingConfigError):
        tr.load_routing_config(p)


def test_config_path_discovers_dotmeridian_dir(tmp_path):
    (tmp_path / ".meridian").mkdir()
    target = tmp_path / ".meridian" / "tool-routing.toml"
    target.write_text('[routing]\nmode = "shadow"\n', encoding="utf-8")
    found = tr._config_path(tmp_path)
    assert found == target


def test_config_path_none_when_no_dotmeridian_dir(tmp_path):
    assert tr._config_path(tmp_path) is None


# ---------------------------------------------------------------------------
# Part 3 — route() layer by layer
# ---------------------------------------------------------------------------


def test_route_no_wiring_returns_unknown():
    """No manifest, no config, no bm25/semantic — falls through to unknown."""
    decision = tr.route("do something nobody declared a rule for")
    assert decision.stage == "unknown"
    assert decision.tool is None
    assert decision.candidates == []
    assert decision.blocked is False
    assert decision.reason == "no_match_return_candidates_not_guess"


def test_route_layer1_capability_manifest_available():
    manifest = [{
        "id": "code-search",
        "purpose": "find symbols and functions",
        "required_tools": ["Serena: find_symbol"],
        "fallback_chain": ["codebase-memory"],
        "availability_policy": "required",
    }]
    decision = tr.route(
        "please code-search for the symbol",
        manifest=manifest,
        tool_inventory={"Serena: find_symbol", "codebase-memory"},
    )
    assert decision.stage == "capability_manifest"
    assert decision.tool == "Serena: find_symbol"
    assert decision.blocked is False


def test_route_layer1_capability_manifest_required_unavailable_blocks():
    manifest = [{
        "id": "code-search",
        "purpose": "find symbols and functions",
        "required_tools": ["Serena: find_symbol"],
        "fallback_chain": [],
        "availability_policy": "required",
    }]
    decision = tr.route(
        "please code-search for the symbol",
        manifest=manifest,
        tool_inventory=set(),  # nothing available
    )
    assert decision.stage == "capability_manifest"
    assert decision.blocked is True
    assert decision.reason == "capability_required_unavailable:code-search"
    assert decision.tool is None


def test_route_layer1_capability_manifest_optional_falls_through():
    """optional/degraded_ok with nothing available doesn't block -- falls
    through to later layers instead of claiming the request."""
    manifest = [{
        "id": "code-search",
        "purpose": "find symbols and functions",
        "required_tools": ["Serena: find_symbol"],
        "fallback_chain": [],
        "availability_policy": "optional",
    }]
    decision = tr.route(
        "please code-search for the symbol",
        manifest=manifest,
        tool_inventory=set(),
    )
    # Falls through to layer 3 (category match on "code" keyword).
    assert decision.stage == "category_match"
    assert decision.blocked is False


def test_route_layer1_skipped_when_tool_inventory_unknown():
    """tool_inventory=None means availability can't be determined -- layer 1
    is skipped entirely rather than guessing available/unavailable."""
    manifest = [{
        "id": "code-search",
        "purpose": "find symbols and functions",
        "required_tools": ["Serena: find_symbol"],
        "availability_policy": "required",
    }]
    decision = tr.route("please code-search for the symbol", manifest=manifest, tool_inventory=None)
    assert decision.stage != "capability_manifest"


def test_route_layer2_explicit_rule_wins_over_category_match():
    cfg = tr.normalize_routing_config({
        "rules": [{
            "id": "docx-editing",
            "priority": 100,
            "match_keywords": ["docx"],
            "category": "docx",
            "tool": "meridian-docs",
            "required_tools": ["meridian-docs"],
        }],
    })
    decision = tr.route("edit this docx chapter", routing_config=cfg)
    assert decision.stage == "explicit_rule"
    assert decision.tool == "meridian-docs"
    assert decision.reason == "explicit_rule_match:docx-editing"


def test_route_layer2_rule_priority_order():
    """Higher-priority rule wins when both match the same text."""
    cfg = tr.normalize_routing_config({
        "rules": [
            {"id": "low-pri", "priority": 1, "match_keywords": ["docx"], "tool": "low-tool"},
            {"id": "high-pri", "priority": 99, "match_keywords": ["docx"], "tool": "high-tool"},
        ],
    })
    decision = tr.route("edit this docx chapter", routing_config=cfg)
    assert decision.tool == "high-tool"


def test_route_layer2_pattern_match():
    cfg = tr.normalize_routing_config({
        "rules": [{
            "id": "symbol-lookup",
            "priority": 10,
            "match_pattern": r"(?i)find (the )?symbol",
            "tool": "Serena: find_symbol",
        }],
    })
    decision = tr.route("Please find the symbol for X", routing_config=cfg)
    assert decision.stage == "explicit_rule"
    assert decision.tool == "Serena: find_symbol"


def test_route_layer3_category_match_code_intel():
    decision = tr.route("refactor the codebase search index")
    assert decision.stage == "category_match"
    assert "code-intel" in decision.category


def test_route_layer3_category_match_docx():
    decision = tr.route("update the thesis docx chapter")
    assert decision.stage == "category_match"
    assert "docx" in decision.category


def test_route_layer3_custom_category_affinity():
    """Beyond-Meridian's-own-tools: caller-supplied affinity mapping works
    identically -- this is the "generalize beyond Meridian's own tools" gap
    closure (finding 5569beca / decision 2a3a3882)."""
    custom = {"deploy": "infra", "kubernetes": "infra"}
    decision = tr.route("deploy the new kubernetes manifest", category_affinity=custom)
    assert decision.stage == "category_match"
    assert decision.category == ["infra"]


def test_route_layer4_bm25_candidates():
    def fake_bm25(text):
        return [("tool_a", "some text about tool a"), ("tool_b", "some text about tool b")]

    decision = tr.route("something with no category match at all zzqx", bm25_fn=fake_bm25)
    assert decision.stage == "bm25"
    assert decision.candidates == ["tool_a", "tool_b"]
    assert decision.confidence is None


def test_route_layer4_bm25_fn_exception_degrades_to_unknown():
    def broken_bm25(text):
        raise RuntimeError("boom")

    decision = tr.route("zzqx no match text", bm25_fn=broken_bm25)
    assert decision.stage == "unknown"


def test_route_layer5_semantic_reorders_bm25_candidates():
    def fake_bm25(text):
        return [("tool_a", "text a"), ("tool_b", "text b"), ("tool_c", "text c")]

    class FakeMatch:
        def __init__(self, id_, fused_score, confident):
            self.id = id_
            self.fused_score = fused_score
            self.confident = confident

    def fake_semantic(query, candidates):
        # Rerank so tool_c is best, tool_a second, tool_b last.
        return [
            FakeMatch("tool_c", 0.9, True),
            FakeMatch("tool_a", 0.5, False),
            FakeMatch("tool_b", 0.2, False),
        ]

    decision = tr.route(
        "zzqx no category match text", bm25_fn=fake_bm25, semantic_fn=fake_semantic
    )
    assert decision.stage == "semantic_rerank"
    assert decision.candidates == ["tool_c", "tool_a", "tool_b"]
    assert decision.confidence == 0.9
    # Membership preserved exactly.
    assert set(decision.candidates) == {"tool_a", "tool_b", "tool_c"}


def test_route_layer5_semantic_never_removes_a_candidate():
    def fake_bm25(text):
        return [("tool_a", "text a"), ("tool_b", "text b")]

    class FakeMatch:
        def __init__(self, id_):
            self.id = id_
            self.fused_score = 0.4
            self.confident = False

    def partial_semantic(query, candidates):
        # Only scores ONE of the two candidates -- the other must still
        # survive in the output (reorder-only, never drop).
        return [FakeMatch("tool_b")]

    decision = tr.route("zzqx text", bm25_fn=fake_bm25, semantic_fn=partial_semantic)
    assert set(decision.candidates) == {"tool_a", "tool_b"}


def test_route_layer5_semantic_exception_degrades_to_bm25_decision():
    def fake_bm25(text):
        return [("tool_a", "text a")]

    def broken_semantic(query, candidates):
        raise RuntimeError("model unavailable")

    decision = tr.route("zzqx text", bm25_fn=fake_bm25, semantic_fn=broken_semantic)
    assert decision.stage == "bm25"
    assert decision.candidates == ["tool_a"]


def test_route_layer5_semantic_never_touches_blocked_or_tool_fields():
    """Structural guarantee: dataclasses.replace only ever changes
    stage/candidates/confidence/reason."""
    def fake_bm25(text):
        return [("tool_a", "text a")]

    class FakeMatch:
        id = "tool_a"
        fused_score = 0.99
        confident = True

    def fake_semantic(query, candidates):
        return [FakeMatch()]

    decision = tr.route("zzqx text", bm25_fn=fake_bm25, semantic_fn=fake_semantic)
    assert decision.tool is None
    assert decision.blocked is False
    assert decision.required_tools == []
    assert decision.fallback_chain == []


# ---------------------------------------------------------------------------
# Part 4 — pinned categories never reach semantic reranking
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("category", sorted(tr.PINNED_CATEGORIES))
def test_pinned_categories_are_answered_before_bm25_or_semantic(category):
    assert category in ("code-intel", "docx")


def test_code_intel_request_never_reaches_semantic_layer():
    """A code-related query is answered at layer 3 (category_match) —
    layers 4/5 never run, so semantic reranking structurally cannot
    replace Serena/codebase-memory for code, per AGENTS.md."""
    called = {"bm25": False, "semantic": False}

    def bm25_fn(text):
        called["bm25"] = True
        return [("should", "not be reached")]

    def semantic_fn(query, candidates):
        called["semantic"] = True
        return []

    decision = tr.route(
        "please search the codebase for this symbol",
        bm25_fn=bm25_fn,
        semantic_fn=semantic_fn,
    )
    assert decision.stage == "category_match"
    assert "code-intel" in decision.category
    assert called == {"bm25": False, "semantic": False}


def test_docx_request_never_reaches_semantic_layer():
    called = {"bm25": False, "semantic": False}

    def bm25_fn(text):
        called["bm25"] = True
        return [("should", "not be reached")]

    def semantic_fn(query, candidates):
        called["semantic"] = True
        return []

    decision = tr.route(
        "update the thesis docx with a new equation",
        bm25_fn=bm25_fn,
        semantic_fn=semantic_fn,
    )
    assert decision.stage == "category_match"
    assert "docx" in decision.category
    assert called == {"bm25": False, "semantic": False}


# ---------------------------------------------------------------------------
# Part 5 — authorize_mutation (fail-closed mutation gate)
# ---------------------------------------------------------------------------


def _decision(**overrides):
    base = dict(
        stage="capability_manifest", tool="Serena: find_symbol", category=None,
        candidates=["Serena: find_symbol"], confidence=1.0, reason="x",
        blocked=False, mode="advisory", required_tools=[], fallback_chain=[],
    )
    base.update(overrides)
    return tr.RoutingDecision(**base)


def test_authorize_mutation_capability_manifest_stage_authorized():
    ok, reason = tr.authorize_mutation(_decision(stage="capability_manifest"))
    assert ok is True
    assert reason == "authorized"


def test_authorize_mutation_explicit_rule_stage_authorized():
    ok, reason = tr.authorize_mutation(_decision(stage="explicit_rule"))
    assert ok is True


@pytest.mark.parametrize("stage", ["category_match", "bm25", "semantic_rerank", "unknown"])
def test_authorize_mutation_non_deterministic_stages_never_authorized(stage):
    ok, reason = tr.authorize_mutation(_decision(stage=stage))
    assert ok is False
    assert reason == f"stage_{stage}_cannot_authorize_write"


def test_authorize_mutation_blocked_decision_refused():
    ok, reason = tr.authorize_mutation(_decision(blocked=True))
    assert ok is False
    assert reason == "decision_is_blocked"


def test_authorize_mutation_no_tool_refused():
    ok, reason = tr.authorize_mutation(_decision(tool=None))
    assert ok is False
    assert reason == "no_tool_resolved"


def test_authorize_mutation_tool_not_in_inventory_refused():
    ok, reason = tr.authorize_mutation(
        _decision(tool="ghost-tool"), tool_inventory={"Serena: find_symbol"}
    )
    assert ok is False
    assert reason == "tool_not_in_inventory"


def test_authorize_mutation_tool_in_inventory_authorized():
    ok, reason = tr.authorize_mutation(
        _decision(tool="Serena: find_symbol"), tool_inventory={"Serena: find_symbol"}
    )
    assert ok is True


def test_authorize_mutation_never_checks_inventory_when_omitted():
    ok, reason = tr.authorize_mutation(_decision(tool="anything"), tool_inventory=None)
    assert ok is True
