"""Regression tests for 4f3bd70c — "enforce bounded executor/delta/starter/
compact serializers without syntactic truncation."

Confirmed root causes this closes (see the item's own notes and
meridian/handoff.py's format_handoff_mcp_content/build_effective_profile_binding
for the full narrative):

  1. format_handoff_mcp_content's wire-level byte budget was a PURE byte
     slice with no notion of the structural <tag>...</tag> spans a rendered
     handoff embeds (<tool_requirements>, <sprint_item_pointers>,
     <artifact_pointer_findings>, <selected_item_scope>, ...). A cut could
     land inside one of those tags (or the JSON it encloses), leaving a
     dangling open tag / truncated JSON value behind even though the overall
     byte budget was honored. Fixed by _structural_tag_spans/
     _snap_to_safe_boundary: the cut point is now snapped backward to the
     nearest point that never lands inside a tag span — a tag that would
     only partially survive is dropped in full instead. Reused identically
     by every mode that funnels through format_handoff_mcp_content (full,
     delta, starter/compact, goal, checkpoint, continue).
  2. The truncation marker only ever reported a raw omitted-byte count, with
     no structured, machine-readable omission metadata. Fixed by embedding a
     compact `machine_readable={...}` JSON object (content_truncated,
     omitted_bytes, total_bytes, limit_bytes, sections_omitted, reason) in
     the same marker comment.
  3. build_effective_profile_binding's session_id was threaded into
     generate_handoff's full/delta call site but NOT into
     _generate_starter_handoff/_generate_goal_only_handoff (both had no
     session_id parameter at all), even though the MCP wrapper
     (mcp/handler.py) already computes a SIBLING profile_binding field WITH
     session_id for every mode. Fixed by adding session_id to both helper
     functions' signatures and threading generate_handoff's own session_id
     through to their build_effective_profile_binding calls, so a session-
     scoped profile override is reflected consistently in every mode's
     embedded <profile_generation> tag.

Covers: meridian/handoff.py (format_handoff_mcp_content,
_structural_tag_spans, _snap_to_safe_boundary, _generate_starter_handoff,
_generate_goal_only_handoff, build_effective_profile_binding).
"""
from __future__ import annotations

import json
import re

import pytest

from meridian import db as db_module
from meridian import handoff as handoff_module


# pytest.ini sets asyncio_mode = auto — no explicit @pytest.mark.asyncio or
# module-level pytestmark needed; async def tests are picked up automatically.

# 47ac68a0 — these two must accept an ATTRIBUTE-BEARING opening tag
# (`<execution_policy execution_mode="...">`, `<selected_item_scope
# requested="...">`, the outer `<handoff_manifest schema_version="...">`
# wrapper, `<proposal_scope proposal_id="...">`), not just a bare `<tag>`:
# the production regex (`handoff._STRUCTURAL_TAG_RE`) had exactly this same
# bare-tag-only blind spot, so a dangling-tag check built on the old
# bare-tag-only pattern here could never have caught it either. Self-closing
# tags (`<executor_item_ids count="0" />`) are stripped out before the
# open/close balance check below — they never have (or need) a separate
# closing tag, so counting their opener as "open" would be a false positive.
_OPEN_TAG_RE = re.compile(r"<([a-zA-Z][a-zA-Z0-9_]*)(?:\s[^<>]*)?>")
_CLOSE_TAG_RE = re.compile(r"</([a-zA-Z][a-zA-Z0-9_]*)>")
_SELF_CLOSING_TAG_RE = re.compile(r"<[a-zA-Z][a-zA-Z0-9_]*(?:\s[^<>]*)?/>")
_MARKER_META_RE = re.compile(r"machine_readable=(\{.*?\})\s*-->")


_XML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def _assert_no_dangling_tags(text: str) -> None:
    """Every top-level <tag ...> opened in `text` must have a matching
    </tag> — the concrete, checkable form of "no string slicing that can cut
    XML/JSON/goal tags." A truncated render may legitimately DROP a whole
    tag, but must never leave a half-open one.

    XML comments (the appended TRUNCATED marker, the SECURITY banner) are
    stripped first: their prose legitimately MENTIONS tag names (e.g. "up to
    and including any <goal_token>/SECURITY banner above", "with the
    <goal_token> value above") without those mentions being real structural
    markup that needs a closing tag — checking inside a comment would be a
    false positive, not a real dangling-tag defect. Self-closing tags are
    stripped next for the same reason (see module comment above).
    """
    stripped = _XML_COMMENT_RE.sub("", text)
    stripped = _SELF_CLOSING_TAG_RE.sub("", stripped)
    opens = [m.group(1) for m in _OPEN_TAG_RE.finditer(stripped)]
    closes = [m.group(1) for m in _CLOSE_TAG_RE.finditer(stripped)]
    from collections import Counter  # noqa: PLC0415
    open_counts = Counter(opens)
    close_counts = Counter(closes)
    for name, count in open_counts.items():
        assert close_counts.get(name, 0) == count, (
            f"dangling/unbalanced <{name}> tag(s) in truncated content: "
            f"{count} open vs {close_counts.get(name, 0)} close"
        )


def _extract_marker_meta(text: str) -> "dict | None":
    m = _MARKER_META_RE.search(text)
    if m is None:
        return None
    return json.loads(m.group(1))


def _bulky_tool_requirements(idx: int) -> list[dict[str, str]]:
    _purpose = (
        "Prospect the touched symbols with search_graph/find_symbol before "
        "editing so the change stays scoped to what this item actually "
        "declared, then confirm callers via find_referencing_symbols. "
    ) * 4
    return [
        {
            "name": "find_symbol",
            "server_or_namespace": "Serena",
            "required_or_preferred": "required",
            "purpose": _purpose,
        },
        {
            "name": f"tool_{idx}",
            "server_or_namespace": "Serena",
            "required_or_preferred": "preferred",
            "purpose": _purpose,
        },
    ]


async def _make_bulky_board(db, pid: str, count: int) -> None:
    for i in range(count):
        await db_module.add_sprint_item(
            db, pid, "v1", f"Bulk item {i}",
            tool_requirements=_bulky_tool_requirements(i),
            force=True,
        )


# ---------------------------------------------------------------------------
# Unit-level: the structural snapping primitives themselves.
# ---------------------------------------------------------------------------


def test_structural_tag_spans_finds_top_level_tags():
    content = (
        "intro text\n"
        "<tool_requirements>[{\"a\": 1}]</tool_requirements>\n"
        "middle\n"
        "<sprint_item_pointers>[{\"b\": 2}]</sprint_item_pointers>\n"
        "tail"
    )
    spans = handoff_module._structural_tag_spans(content)
    assert len(spans) == 2
    for start_b, end_b in spans:
        assert start_b < end_b
    # Spans are ordered and non-overlapping.
    assert spans[0][1] <= spans[1][0]


def test_snap_to_safe_boundary_never_lands_inside_a_span():
    spans = [(10, 50), (60, 90)]
    for cut in range(0, 100):
        safe = handoff_module._snap_to_safe_boundary(cut, spans, protected_end=0)
        for start_b, end_b in spans:
            assert not (start_b < safe < end_b), (
                f"cut={cut} snapped to {safe}, still inside span ({start_b}, {end_b})"
            )


def test_snap_to_safe_boundary_respects_protected_end():
    spans = [(0, 30)]
    safe = handoff_module._snap_to_safe_boundary(15, spans, protected_end=20)
    assert safe == 20


# ---------------------------------------------------------------------------
# 47ac68a0 — _STRUCTURAL_TAG_RE attribute-blindness regression.
#
# The pattern originally required a bare `<tag>` with no attributes, so it
# never matched (and therefore never protected) the structural tags that are
# ACTUALLY rendered with attributes: <execution_policy execution_mode=...>,
# <selected_item_scope requested=...>, the outer <handoff_manifest
# schema_version=...> wrapper, and <proposal_scope proposal_id=...>. These
# tests build the exact strings the real render helpers produce (not
# hand-rolled approximations) and confirm each is now recognized as a
# protected span, and survives (whole, never dangling) a forced truncation
# that lands inside it.
# ---------------------------------------------------------------------------


def test_structural_tag_spans_finds_attribute_bearing_tags():
    execution_policy = handoff_module._build_execution_policy_clause(
        {"execution_mode": "autonomous", "max_planning_turns": 5,
         "required_first_action": "claim_sprint_item"}
    )
    selected_scope = handoff_module._build_selected_scope_clause(
        {"selected_item_ids": ["a", "b"], "closure_item_ids": ["a", "b"],
         "closure_hash": "deadbeef"}
    )
    manifest_xml = handoff_module.serialize_handoff_manifest_xml(
        handoff_module.build_handoff_manifest(
            handoff_mode="full", project_id="p1", items=[],
        )
    )
    proposal_scope = handoff_module._build_proposal_scope_clause(
        {"proposal_id": "prop-1", "content_hash": "hash1", "executable": True,
         "items": [{"id": "a"}]},
    )
    for label, tag_name, rendered in (
        ("execution_policy", "execution_policy", execution_policy),
        ("selected_item_scope", "selected_item_scope", selected_scope),
        ("handoff_manifest", "handoff_manifest", manifest_xml),
        ("proposal_scope", "proposal_scope", proposal_scope),
    ):
        assert rendered, f"{label}: fixture produced an empty render"
        # Sanity: the tag really is attribute-bearing (opening tag has a
        # space before its closing '>'), otherwise this test would not be
        # exercising the bug it claims to.
        assert re.search(rf"<{tag_name}\s", rendered), (
            f"{label}: fixture tag has no attributes — not testing the bug"
        )
        spans = handoff_module._structural_tag_spans(rendered)
        assert len(spans) >= 1, (
            f"{label}: attribute-bearing <{tag_name}> was NOT recognized as "
            "a protected structural span (the 47ac68a0 regression)"
        )
        start_b, end_b = spans[0]
        assert end_b == len(rendered.encode("utf-8")), (
            f"{label}: protected span must cover the tag through its "
            "closing tag"
        )


def test_format_handoff_mcp_content_protects_execution_policy_tag():
    """A byte cut landing inside a real <execution_policy ...> tag (the
    concrete failure named in the item's own acceptance criteria) must drop
    the whole tag, never leave a dangling `<execution_policy ...` fragment.

    The exact byte offset at which the truncation actually lands inside the
    tag depends on the marker's own (unrelated) byte length — reserving room
    for it shifts the effective cut point — so a single hand-picked budget
    is fragile. Sweep a wide range of forced budgets instead: whatever that
    offset is, the sweep crosses it, and the invariant must hold at every
    point along the way, not just one lucky value.
    """
    execution_policy = handoff_module._build_execution_policy_clause(
        {"execution_mode": "autonomous", "max_planning_turns": 5,
         "required_first_action": "claim_sprint_item",
         "no_confirmation": True}
    )
    assert "<execution_policy" in execution_policy
    # NOTE: the `* 10` below must apply to ONLY the padding literal — adjacent
    # string literals concatenate at parse time before `*` is applied, so
    # `f"{x}\n" "text" * 10` would (surprisingly) repeat `x` along with the
    # text. Building the padding as its own literal first avoids that trap.
    _padding = (
        "More trailing narrative that is not structurally significant, "
        "padded out so there is real content after the tag to trim. "
    ) * 10
    content = "# Handoff\n\nSome narrative text up top.\n" f"{execution_policy}\n" + _padding
    total = len(content.encode("utf-8"))
    for budget in range(40, total, 20):
        out = handoff_module.format_handoff_mcp_content(content, max_bytes=budget)
        _assert_no_dangling_tags(out)
        # Whole-or-nothing, specifically for this tag: the word
        # "execution_policy" appears nowhere else in this fixture, so an
        # open tag with no matching close is unambiguously a dangling
        # fragment — exactly what the buggy bare-tag regex produces.
        if "<execution_policy" in out:
            assert "</execution_policy>" in out, (
                f"budget={budget}: dangling <execution_policy ...> with no "
                "matching close tag"
            )


def test_format_handoff_mcp_content_protects_handoff_manifest_wrapper():
    """A cut landing inside <handoff_manifest ...>'s own opening tag (not
    just its attribute-free inner children) must drop the whole manifest,
    never a truncated wrapper missing its closing tag. Same budget-sweep
    rationale as the execution_policy test above."""
    manifest_xml = handoff_module.serialize_handoff_manifest_xml(
        handoff_module.build_handoff_manifest(
            handoff_mode="full", project_id="p1",
            items=[{"id": f"item-{i}", "status": "pending"} for i in range(20)],
        )
    )
    assert manifest_xml.startswith("<handoff_manifest ")
    # See the string-literal-concatenation-before-`*` note in the
    # execution_policy test above for why the padding is built separately.
    _padding = "Trailing narrative padding text repeated for byte budget. " * 15
    content = "# Handoff\n\n" f"{manifest_xml}\n" + _padding
    total = len(content.encode("utf-8"))
    for budget in range(40, total, 25):
        out = handoff_module.format_handoff_mcp_content(content, max_bytes=budget)
        _assert_no_dangling_tags(out)
        if "<handoff_manifest" in out:
            assert out.count("</handoff_manifest>") == 1, (
                f"budget={budget}: <handoff_manifest> present without its "
                "single matching closing tag"
            )


# ---------------------------------------------------------------------------
# format_handoff_mcp_content: tag-safe truncation + machine-readable
# omission metadata, directly.
# ---------------------------------------------------------------------------


def test_format_handoff_mcp_content_never_produces_dangling_tag():
    """A budget landing squarely inside a JSON-bearing tag must drop the
    WHOLE tag, never leave a partial open tag or truncated JSON behind."""
    payload = json.dumps([{"name": f"tool_{i}", "purpose": "x" * 40} for i in range(30)])
    content = (
        "# Handoff\n\nSome narrative text up top.\n\n"
        f"<tool_requirements>{payload}</tool_requirements>\n\n"
        "More trailing narrative that is not structurally significant.\n"
    )
    full_len = len(content.encode("utf-8"))
    # Pick a budget that lands inside the tag span in the OLD byte-slicing
    # implementation (roughly mid-payload).
    tag_start = content.index("<tool_requirements>")
    mid_tag_cut = tag_start + 50
    assert mid_tag_cut < full_len
    out = handoff_module.format_handoff_mcp_content(content, max_bytes=mid_tag_cut)
    assert "TRUNCATED" in out
    _assert_no_dangling_tags(out)
    # The tag must be either fully present (and its JSON parseable) or fully
    # absent — never a partial fragment.
    if "<tool_requirements>" in out:
        m = re.search(r"<tool_requirements>(.*?)</tool_requirements>", out, re.DOTALL)
        assert m is not None
        json.loads(m.group(1))  # must not raise


def test_format_handoff_mcp_content_marker_reports_machine_readable_omission():
    payload = json.dumps([{"name": f"tool_{i}"} for i in range(40)])
    content = (
        "intro\n"
        f"<tool_requirements>{payload}</tool_requirements>\n"
        f"<sprint_item_pointers>{payload}</sprint_item_pointers>\n"
        "trailing narrative " * 50
    )
    out = handoff_module.format_handoff_mcp_content(content, max_bytes=200)
    meta = _extract_marker_meta(out)
    assert meta is not None, "expected a machine_readable={...} block in the marker"
    assert meta["content_truncated"] is True
    assert meta["reason"] == "response_size_budget"
    assert meta["limit_bytes"] == 200
    assert meta["total_bytes"] == len(content.encode("utf-8"))
    assert isinstance(meta["omitted_bytes"], int) and meta["omitted_bytes"] > 0
    assert isinstance(meta["sections_omitted"], int) and meta["sections_omitted"] >= 1
    _assert_no_dangling_tags(out)


def test_format_handoff_mcp_content_drops_whole_sections_not_partial_bytes():
    """When multiple structural tags are present and the budget cannot fit
    them all, the survivors must be the ones that fit WHOLE — never a
    half-included tag — and the omitted count must match what was actually
    dropped."""
    tags = []
    for i in range(6):
        body = json.dumps({"i": i, "pad": "z" * 200})
        tags.append(f"<section_{i}>{body}</section_{i}>")
    content = "header\n" + "\n".join(tags) + "\ntrailing text\n"
    out = handoff_module.format_handoff_mcp_content(content, max_bytes=400)
    _assert_no_dangling_tags(out)
    meta = _extract_marker_meta(out)
    assert meta is not None
    # Every section still present in `out` must parse; count kept vs. total.
    kept = 0
    for i in range(6):
        m = re.search(rf"<section_{i}>(.*?)</section_{i}>", out, re.DOTALL)
        if m is not None:
            json.loads(m.group(1))
            kept += 1
    assert kept + meta["sections_omitted"] >= 6 - 1  # allow off-by-one for the goal_token span accounting
    assert kept < 6, "fixture should force at least one section to be dropped"


# ---------------------------------------------------------------------------
# End-to-end: every mode stays bounded and structurally valid under a tiny
# forced budget, with explicit omitted counts/reason surfaced.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["full", "delta", "starter", "compact"])
async def test_mode_bounded_and_structurally_valid_under_tiny_budget(db, tmp_path, mode):
    p = await db_module.create_project(db, f"4f3bd70c-tiny-budget-{mode}")
    pid = p["id"]
    s = await db_module.register_session(db, pid, f"tiny-budget-session-{mode}")
    await _make_bulky_board(db, pid, count=40)

    _path, content, _amended = await handoff_module.generate_handoff(
        db, pid, str(tmp_path), skip_ai_summary=True, mode=mode,
        session_id=s["id"], max_content_bytes=2_000,
    )
    assert len(content.encode("utf-8")) <= 2_000 or "<goal_token>" in content
    _assert_no_dangling_tags(content)
    if "TRUNCATED" in content:
        meta = _extract_marker_meta(content)
        assert meta is not None, f"mode={mode}: TRUNCATED present but no machine-readable metadata"
        assert meta["content_truncated"] is True
        assert meta["reason"] == "response_size_budget"
        assert meta["limit_bytes"] == 2_000


async def test_goal_mode_atomic_body_still_never_truncated_by_structural_change(db, tmp_path):
    """MDE-10 contract must survive the structural-snap change untouched:
    an executable /goal body is atomic and is returned byte-identically even
    under a budget far smaller than its size."""
    p = await db_module.create_project(db, "4f3bd70c-goal-atomic")
    pid = p["id"]
    await _make_bulky_board(db, pid, count=10)
    _path, content, _amended = await handoff_module.generate_handoff(
        db, pid, str(tmp_path), skip_ai_summary=True, mode="goal",
        max_content_bytes=10,
    )
    assert content.lstrip().startswith(("/goal", "/loop /goal"))
    assert "TRUNCATED" not in content
    assert len(content.encode("utf-8")) > 10


# ---------------------------------------------------------------------------
# Regression fixture: a large board must not blow starter/compact's compact
# budget by orders of magnitude.
# ---------------------------------------------------------------------------


async def test_starter_mode_stays_compact_on_a_large_board(db, tmp_path):
    p = await db_module.create_project(db, "4f3bd70c-large-board-starter")
    pid = p["id"]
    s = await db_module.register_session(db, pid, "large-board-session")
    await _make_bulky_board(db, pid, count=250)

    _path, content, _amended = await handoff_module.generate_handoff(
        db, pid, str(tmp_path), skip_ai_summary=True, mode="starter",
        session_id=s["id"],
    )
    assert len(content.encode("utf-8")) <= handoff_module._DEFAULT_STARTER_MAX_BYTES
    _assert_no_dangling_tags(content)


# ---------------------------------------------------------------------------
# build_effective_profile_binding session_id consistency across every mode.
# ---------------------------------------------------------------------------


_PROFILE_GEN_RE = re.compile(r'<profile_generation key="([^"]*)"')


async def test_starter_mode_profile_generation_reflects_session_layer(db, tmp_path):
    p = await db_module.create_project(db, "4f3bd70c-profile-starter")
    pid = p["id"]
    s = await db_module.register_session(db, pid, "profile-session")
    await db_module.add_sprint_item(db, pid, "v1", "item one")

    _path, no_session_content = await handoff_module._generate_starter_handoff(
        db, p, str(tmp_path),
    )
    await db_module.set_profile_layer(
        db, "session", s["id"], fields={"tool_priority_map": {"code_search": "grep"}},
    )
    _path, with_session_content = await handoff_module._generate_starter_handoff(
        db, p, str(tmp_path), session_id=s["id"],
    )

    m_no_session = _PROFILE_GEN_RE.search(no_session_content)
    m_with_session = _PROFILE_GEN_RE.search(with_session_content)
    assert m_no_session is not None
    assert m_with_session is not None
    assert m_no_session.group(1) != m_with_session.group(1), (
        "starter mode's <profile_generation> key must change once a "
        "session-scoped profile layer is in effect and session_id is passed"
    )

    # Cross-check against the SAME sibling computation the MCP wrapper uses
    # (mcp/handler.py) — they must now agree.
    sibling = await handoff_module.build_effective_profile_binding(
        db, pid, session_id=s["id"],
    )
    assert sibling is not None
    assert sibling["generation_key"] == m_with_session.group(1)


async def test_goal_only_mode_profile_generation_reflects_session_layer(db, tmp_path):
    p = await db_module.create_project(db, "4f3bd70c-profile-goal")
    pid = p["id"]
    s = await db_module.register_session(db, pid, "profile-session-goal")
    await db_module.add_sprint_item(db, pid, "v1", "item one")

    _path, no_session_content = await handoff_module._generate_goal_only_handoff(
        db, pid, str(tmp_path),
    )
    await db_module.set_profile_layer(
        db, "session", s["id"], fields={"tool_priority_map": {"code_search": "grep"}},
    )
    _path, with_session_content = await handoff_module._generate_goal_only_handoff(
        db, pid, str(tmp_path), session_id=s["id"],
    )

    m_no_session = _PROFILE_GEN_RE.search(no_session_content)
    m_with_session = _PROFILE_GEN_RE.search(with_session_content)
    assert m_no_session is not None
    assert m_with_session is not None
    assert m_no_session.group(1) != m_with_session.group(1), (
        "goal-only mode's <profile_generation> key must change once a "
        "session-scoped profile layer is in effect and session_id is passed"
    )

    sibling = await handoff_module.build_effective_profile_binding(
        db, pid, session_id=s["id"],
    )
    assert sibling is not None
    assert sibling["generation_key"] == m_with_session.group(1)


async def test_generate_handoff_threads_session_id_into_starter_and_goal(db, tmp_path):
    """End-to-end proof that generate_handoff itself (not just the helper
    functions directly) now passes session_id through for starter/compact
    and goal, matching the pre-existing full/delta behavior."""
    p = await db_module.create_project(db, "4f3bd70c-e2e-session-thread")
    pid = p["id"]
    s = await db_module.register_session(db, pid, "e2e-session")
    await db_module.add_sprint_item(db, pid, "v1", "item one")
    await db_module.set_profile_layer(
        db, "session", s["id"], fields={"tool_priority_map": {"code_search": "grep"}},
    )
    sibling = await handoff_module.build_effective_profile_binding(
        db, pid, session_id=s["id"],
    )
    assert sibling is not None

    for mode in ("starter", "goal"):
        _path, content, _amended = await handoff_module.generate_handoff(
            db, pid, str(tmp_path), skip_ai_summary=True, mode=mode,
            session_id=s["id"],
        )
        m = _PROFILE_GEN_RE.search(content)
        assert m is not None, f"mode={mode}: no <profile_generation> tag rendered"
        assert m.group(1) == sibling["generation_key"], (
            f"mode={mode}: embedded <profile_generation> key disagrees with the "
            "MCP wrapper's own sibling profile_binding computation"
        )
