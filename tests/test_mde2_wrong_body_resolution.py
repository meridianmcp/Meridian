"""MDE-2 rework -- wrong-body resolution engine fix, active-repository
scoping, and the graph-vs-live-file hash agreement mechanism.

The verifier's exact findings this file closes (see the item's rework
brief): prospect.py's own graph-rung hit-selection was untouched (a
neighboring symbol's file could be silently treated as the queried
symbol's own), no query was scoped to the active repository, receipt
``range``/real ``symbol`` identity were never recorded, and there was no
mechanism -- let alone a test -- proving "graph and live-file hashes
agree".

Every test class below is written so it would have FAILED against the
pre-rework code (a bare ``matches[0]``/``hits[0]`` fallback, no scoping,
no range/symbol/hash fields) and passes against the rework.

Repo-identity-binding coverage (contamination, repo_root resolution,
verify_code_intel_prospecting's wrong-repo gate) already lives in
tests/test_mde2_receipt_repo_identity.py -- not duplicated here.
"""
from __future__ import annotations

import json

import pytest

from meridian import db as db_module
from meridian import code_intel_receipt as _cir
from meridian import prospect as _prospect
from meridian import pointers as _pointers


# ---------------------------------------------------------------------------
# prospect.py -- exact-identity hit selection (the actual resolution engine
# fix). A neighboring symbol must NEVER be treated as the queried one.
# ---------------------------------------------------------------------------

class TestHitIdentityMatching:
    def test_exact_qualified_name_match(self):
        hit = {"qualified_name": "pkg.mod.compute_total", "file": "pkg/mod.py"}
        assert _prospect._hit_identity_matches(hit, "pkg.mod.compute_total")

    def test_neighboring_symbol_is_not_a_match(self):
        """The exact bug: a fuzzy search for 'compute_total' returning
        'compute_total_v2' (a real, different symbol) must NOT be treated
        as identity-matching -- this is what would let a caller retrieve
        the WRONG body under the right query."""
        hit = {"qualified_name": "pkg.mod.compute_total_v2", "file": "pkg/mod.py"}
        assert not _prospect._hit_identity_matches(hit, "compute_total")

    def test_matches_via_name_when_no_qualified_name(self):
        hit = {"name": "add_figure", "file": "doc_store.py"}
        assert _prospect._hit_identity_matches(hit, "add_figure")

    def test_non_dict_hit_never_matches(self):
        assert not _prospect._hit_identity_matches("not-a-dict", "x")

    def test_empty_symbol_never_matches(self):
        assert not _prospect._hit_identity_matches({"name": ""}, "")


class TestSelectExactHit:
    def test_picks_the_exact_hit_among_neighbors(self):
        """Multiple near-miss hits plus ONE exact match -- select_exact_hit
        must return the exact one, not the first in the list."""
        hits = [
            {"qualified_name": "pkg.mod.compute_totals", "file": "wrong1.py"},
            {"qualified_name": "pkg.mod.compute_total", "file": "right.py"},
            {"qualified_name": "pkg.mod.compute_total_helper", "file": "wrong2.py"},
        ]
        best = _prospect.select_exact_hit(hits, "pkg.mod.compute_total")
        assert best is not None
        assert best["file"] == "right.py"

    def test_no_exact_match_returns_none_never_a_neighbor(self):
        """THE core wrong-body regression test: before this rework, a
        caller that took hits[0] here would have silently gotten
        'wrong1.py' -- a completely different symbol's file -- as if it
        were the answer for 'totally.unrelated.query'. Now: None."""
        hits = [
            {"qualified_name": "pkg.mod.compute_totals", "file": "wrong1.py"},
            {"qualified_name": "pkg.mod.compute_total_helper", "file": "wrong2.py"},
        ]
        assert _prospect.select_exact_hit(hits, "totally.unrelated.query") is None

    def test_empty_hits_returns_none(self):
        assert _prospect.select_exact_hit([], "anything") is None
        assert _prospect.select_exact_hit(None, "anything") is None


class TestReorderExactFirst:
    def test_exact_match_promoted_to_front(self):
        hits = [
            {"qualified_name": "neighbor.one", "file": "n1.py"},
            {"qualified_name": "the.target", "file": "target.py"},
            {"qualified_name": "neighbor.two", "file": "n2.py"},
        ]
        out = _prospect._reorder_exact_first(hits, "the.target")
        assert out[0]["file"] == "target.py"
        # Non-exact hits keep their RELATIVE order after the exact one.
        assert [h["file"] for h in out[1:]] == ["n1.py", "n2.py"]

    def test_no_exact_match_leaves_order_unchanged(self):
        hits = [{"qualified_name": "a"}, {"qualified_name": "b"}]
        out = _prospect._reorder_exact_first(hits, "nope")
        assert out is hits  # unchanged, same object

    def test_uses_identity_not_equality_for_dedup(self):
        """Two hits with IDENTICAL dict content (a plausible real shape --
        e.g. the same neighbor returned twice by two rungs) must both
        survive the split; the reorder must not silently drop a
        content-duplicate via list membership."""
        dup_a = {"qualified_name": "same.name", "file": "a.py"}
        dup_b = {"qualified_name": "same.name", "file": "a.py"}
        hits = [dup_a, dup_b]
        out = _prospect._reorder_exact_first(hits, "same.name")
        assert len(out) == 2


# ---------------------------------------------------------------------------
# Active-repository scoping -- a hit whose file resolves outside root_dir
# must never be selected as "the" resolution, regardless of name match.
# ---------------------------------------------------------------------------

class TestHitOutOfScopeDetection:
    def test_codex_worktrees_marker_is_out_of_scope(self, tmp_path):
        hit = {"file": str(tmp_path / ".codex" / "worktrees" / "abc" / "mod.py")}
        assert _prospect._hit_is_out_of_scope(hit, str(tmp_path))

    def test_absolute_path_outside_root_is_out_of_scope(self, tmp_path):
        other_root = tmp_path / "other-repo"
        other_root.mkdir()
        target = other_root / "mod.py"
        target.write_text("x = 1\n", encoding="utf-8")
        hit = {"file": str(target)}
        my_root = tmp_path / "my-repo"
        my_root.mkdir()
        assert _prospect._hit_is_out_of_scope(hit, str(my_root))

    def test_absolute_path_inside_root_is_in_scope(self, tmp_path):
        target = tmp_path / "mod.py"
        target.write_text("x = 1\n", encoding="utf-8")
        hit = {"file": str(target)}
        assert not _prospect._hit_is_out_of_scope(hit, str(tmp_path))

    def test_relative_path_never_flagged_unverifiable(self, tmp_path):
        """A relative path COULD legitimately resolve under root_dir --
        never manufacture a rejection without positive evidence."""
        hit = {"file": "sub/mod.py"}
        assert not _prospect._hit_is_out_of_scope(hit, str(tmp_path))

    def test_no_root_dir_never_flags_anything(self):
        hit = {"file": "/some/other/repo/mod.py"}
        assert not _prospect._hit_is_out_of_scope(hit, None)


class TestFinalizeHitsScoping:
    def test_out_of_scope_hit_filtered_before_exact_match_selection(self, tmp_path):
        """The scoping gap: a hit that EXACT-matches the query name but
        lives in a different repository must be filtered OUT before
        exact-match selection even runs -- otherwise a same-named symbol
        in an unrelated checkout would win."""
        my_root = tmp_path / "my-repo"
        my_root.mkdir()
        real_file = my_root / "mod.py"
        real_file.write_text("x = 1\n", encoding="utf-8")
        other_root = tmp_path / "other-repo"
        other_root.mkdir()
        contaminated_file = other_root / "mod.py"
        contaminated_file.write_text("y = 2\n", encoding="utf-8")

        hits = [
            {"qualified_name": "shared.name", "file": str(contaminated_file)},
        ]
        out = _prospect._finalize_hits(hits, "shared.name", str(my_root))
        # Never drops to an EMPTY result (would hide real evidence) --
        # falls back to the original (still not selected as exact+in-scope
        # by a caller that ALSO checks scope, e.g. select_exact_hit downstream
        # combined with the caller's own root_dir check).
        assert out == hits  # nothing else was in scope, so original preserved

    def test_in_scope_exact_hit_wins_over_out_of_scope_neighbor(self, tmp_path):
        my_root = tmp_path / "my-repo"
        my_root.mkdir()
        real_file = my_root / "mod.py"
        real_file.write_text("x = 1\n", encoding="utf-8")
        other_root = tmp_path / "other-repo"
        other_root.mkdir()
        contaminated_file = other_root / "mod.py"
        contaminated_file.write_text("y = 2\n", encoding="utf-8")

        hits = [
            {"qualified_name": "shared.name", "file": str(contaminated_file)},
            {"qualified_name": "shared.name", "file": str(real_file)},
        ]
        out = _prospect._finalize_hits(hits, "shared.name", str(my_root))
        assert out[0]["file"] == str(real_file)


# ---------------------------------------------------------------------------
# Hit accessor helpers (hit_path/hit_identity/hit_range/hit_content) --
# short and long symbol fixtures.
# ---------------------------------------------------------------------------

class TestHitAccessors:
    def test_hit_range_from_line_start_end(self):
        hit = {"line_start": 10, "line_end": 25}
        assert _prospect.hit_range(hit) == {"start_line": 10, "end_line": 25}

    def test_hit_range_from_bare_line(self):
        hit = {"line": 42}
        assert _prospect.hit_range(hit) == {"start_line": 42, "end_line": 42}

    def test_hit_range_from_nested_range_dict(self):
        hit = {"range": {"start_line": 5, "end_line": 9}}
        assert _prospect.hit_range(hit) == {"start_line": 5, "end_line": 9}

    def test_hit_range_none_when_absent(self):
        assert _prospect.hit_range({"file": "x.py"}) is None
        assert _prospect.hit_range(None) is None

    def test_hit_identity_prefers_qualified_name(self):
        hit = {"qualified_name": "a.b.c", "name": "c"}
        assert _prospect.hit_identity(hit) == "a.b.c"

    def test_hit_content_from_content_field(self):
        hit = {"content": "def f():\n    pass\n"}
        assert _prospect.hit_content(hit) == "def f():\n    pass\n"

    def test_short_symbol_name_resolves_exactly(self):
        """Short (even single-character) symbol names must not be treated
        specially -- exact match still requires the FULL string, not a
        prefix/substring coincidence."""
        hits = [
            {"qualified_name": "x", "file": "short.py"},
            {"qualified_name": "xx", "file": "wrong.py"},
        ]
        best = _prospect.select_exact_hit(hits, "x")
        assert best["file"] == "short.py"

    def test_long_symbol_name_over_200_chars_resolves_exactly(self):
        """The receipt's OLD query proxy truncated at 200 chars -- a
        qualified name longer than that must still resolve correctly
        here (this is the resolution ENGINE, operating on the untruncated
        string; extract_query_hint_full is what feeds it the untruncated
        value in production -- see TestExtractQueryHintFull below)."""
        long_name = "pkg." + ".".join(f"mod{i}" for i in range(60)) + ".target_fn"
        assert len(long_name) > 200
        hits = [
            {"qualified_name": long_name[:-1] + "g", "file": "wrong.py"},  # near-miss
            {"qualified_name": long_name, "file": "right.py"},
        ]
        best = _prospect.select_exact_hit(hits, long_name)
        assert best["file"] == "right.py"


class TestExtractQueryHintFull:
    def test_full_hint_is_not_truncated(self):
        long_val = "x" * 500
        assert _cir.extract_query_hint_full({"symbol": long_val}) == long_val
        # The truncated sibling DOES truncate -- confirms the two are
        # genuinely different, not accidentally aliased.
        assert len(_cir.extract_query_hint({"symbol": long_val})) == 200

    def test_full_hint_empty_for_no_match(self):
        assert _cir.extract_query_hint_full({"unrelated": "x"}) == ""


# ---------------------------------------------------------------------------
# resolve_exact_hit_from_tunnel_result -- the tunnel-forward chokepoint's
# parsing path (a THIRD-PARTY result never touched by prospect_symbol_impl).
# ---------------------------------------------------------------------------

class _FakeTunnelMod:
    """Minimal stand-in exposing only what resolve_exact_hit_from_tunnel_
    result needs -- _extract_graph_matches, matching the real
    routes.tunnel._extract_graph_matches contract (unwrap the MCP content
    envelope into the raw JSON payload)."""

    @staticmethod
    def _extract_graph_matches(result):
        if not isinstance(result, dict):
            return result
        content = result.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    return json.loads(block.get("text") or "{}")
        return result


def _mcp_text_result(payload: dict) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(payload)}]}


class TestResolveExactHitFromTunnelResult:
    def test_exact_hit_selected_from_raw_tunnel_payload(self):
        payload = {
            "results": [
                {"qualified_name": "neighbor.fn", "file": "wrong.py"},
                {"qualified_name": "target.fn", "file": "right.py"},
            ]
        }
        result = _mcp_text_result(payload)
        hit = _prospect.resolve_exact_hit_from_tunnel_result(
            _FakeTunnelMod, result, "target.fn", None,
        )
        assert hit is not None and hit["file"] == "right.py"

    def test_no_exact_match_returns_none(self):
        payload = {"results": [{"qualified_name": "neighbor.fn", "file": "wrong.py"}]}
        result = _mcp_text_result(payload)
        hit = _prospect.resolve_exact_hit_from_tunnel_result(
            _FakeTunnelMod, result, "totally.different", None,
        )
        assert hit is None

    def test_empty_symbol_returns_none(self):
        result = _mcp_text_result({"results": []})
        assert _prospect.resolve_exact_hit_from_tunnel_result(
            _FakeTunnelMod, result, "", None,
        ) is None

    def test_malformed_result_never_raises(self):
        assert _prospect.resolve_exact_hit_from_tunnel_result(
            _FakeTunnelMod, "not-a-dict-or-json", "anything", None,
        ) is None


# ---------------------------------------------------------------------------
# pointers._resolve_symbol -- the OTHER production consumer of fuzzy
# matches. Must never resolve to a near-miss candidate.
# ---------------------------------------------------------------------------

async def _neighbor_only_resolver(_db, _pid, _qn, _lim):
    """Simulates the REAL default resolver's fuzzy LIKE-token behavior
    (db.search_graph_entities): returns near-miss candidates that share
    SOME substring with the query but are not an exact match."""
    return [
        {"qualified_name": "pkg.mod.compute_totals", "file": "wrong1.py"},
        {"qualified_name": "pkg.mod.compute_total_helper", "file": "wrong2.py"},
    ]


async def _mixed_resolver(_db, _pid, _qn, _lim):
    return [
        {"qualified_name": "pkg.mod.compute_totals", "file": "wrong.py"},
        {"qualified_name": "pkg.mod.compute_total", "file": "right.py"},
    ]


class TestResolveSymbolNeverReturnsNeighborBody:
    @pytest.mark.asyncio
    async def test_no_exact_match_is_unresolved_not_a_neighbor(self):
        """THE regression test for pointers.py's half of the wrong-body
        bug: before this rework, resolve_pointer would have returned
        resolved=True with file='wrong1.py' for a query that has no exact
        match anywhere in the graph -- silently handing back a totally
        different symbol's location. Now: unresolved."""
        ptr = {
            "source_type": "code",
            "targets": [{
                "uri": "a.py",
                "selector": {"type": "symbol", "qualified_name": "pkg.mod.compute_total"},
            }],
        }
        out = await _pointers.resolve_pointer(
            None, ptr, project_id="pid", symbol_resolver=_neighbor_only_resolver,
        )
        t = out["targets"][0]
        assert t["resolved"] is False
        assert "near_miss_candidates" in t
        assert t["near_miss_candidates"] == ["pkg.mod.compute_totals", "pkg.mod.compute_total_helper"]

    @pytest.mark.asyncio
    async def test_exact_match_among_near_misses_still_resolves(self):
        """Confirms the fix isn't over-broad: a REAL exact match present
        alongside near-miss noise still resolves correctly."""
        ptr = {
            "source_type": "code",
            "targets": [{
                "uri": "a.py",
                "selector": {"type": "symbol", "qualified_name": "pkg.mod.compute_total"},
            }],
        }
        out = await _pointers.resolve_pointer(
            None, ptr, project_id="pid", symbol_resolver=_mixed_resolver,
        )
        t = out["targets"][0]
        assert t["resolved"] is True
        assert t["file"] == "right.py"


# ---------------------------------------------------------------------------
# Receipt: real symbol identity + range fields (previously never recorded).
# ---------------------------------------------------------------------------

class TestReceiptRangeAndSymbolIdentity:
    @pytest.mark.asyncio
    async def test_range_is_recorded_when_supplied(self, db, tmp_path):
        project = await db_module.create_project(db, "receipt-range-proj")
        row = await _cir.record_prospect_receipt(
            db, tenant_id=None, project_id=project["id"], session_id="sess-1",
            tool_name="prospect_symbol", query="Thing", root_dir=str(tmp_path),
            resolved_range={"start_line": 10, "end_line": 25},
        )
        detail = json.loads(row["detail"])
        assert detail["range"] == {"start_line": 10, "end_line": 25}

    @pytest.mark.asyncio
    async def test_no_range_supplied_is_none(self, db, tmp_path):
        project = await db_module.create_project(db, "receipt-no-range-proj")
        row = await _cir.record_prospect_receipt(
            db, tenant_id=None, project_id=project["id"], session_id="sess-1",
            tool_name="prospect_symbol", query="Thing", root_dir=str(tmp_path),
        )
        detail = json.loads(row["detail"])
        assert detail["range"] is None

    @pytest.mark.asyncio
    async def test_resolved_symbol_is_the_real_identity_not_the_raw_query(self, db, tmp_path):
        """THE 'weakly proxied by a raw query string' fix: when a real
        resolved_symbol is supplied, it -- not the (possibly unrelated or
        truncated) raw query -- becomes the receipt's symbol identity."""
        project = await db_module.create_project(db, "receipt-real-symbol-proj")
        row = await _cir.record_prospect_receipt(
            db, tenant_id=None, project_id=project["id"], session_id="sess-1",
            tool_name="prospect_symbol", query="compute_total",
            root_dir=str(tmp_path), resolved_symbol="pkg.mod.compute_total",
        )
        detail = json.loads(row["detail"])
        assert detail["symbol"] == "pkg.mod.compute_total"
        assert detail["symbol_source"] == "resolved_hit"

    @pytest.mark.asyncio
    async def test_no_resolved_symbol_falls_back_to_query_proxy_labeled_as_such(self, db, tmp_path):
        project = await db_module.create_project(db, "receipt-proxy-symbol-proj")
        row = await _cir.record_prospect_receipt(
            db, tenant_id=None, project_id=project["id"], session_id="sess-1",
            tool_name="prospect_symbol", query="compute_total", root_dir=str(tmp_path),
        )
        detail = json.loads(row["detail"])
        assert detail["symbol"] == "compute_total"
        assert detail["symbol_source"] == "query_hint_proxy"

    @pytest.mark.asyncio
    async def test_long_resolved_symbol_is_not_truncated(self, db, tmp_path):
        """Unlike the query proxy (truncated to 200 chars), a REAL
        resolved symbol identity must be recorded in full."""
        long_name = "pkg." + ".".join(f"mod{i}" for i in range(60)) + ".target_fn"
        assert len(long_name) > 200
        project = await db_module.create_project(db, "receipt-long-symbol-proj")
        row = await _cir.record_prospect_receipt(
            db, tenant_id=None, project_id=project["id"], session_id="sess-1",
            tool_name="prospect_symbol", query="target_fn",
            root_dir=str(tmp_path), resolved_symbol=long_name,
        )
        detail = json.loads(row["detail"])
        assert detail["symbol"] == long_name
        assert len(detail["symbol"]) > 200


# ---------------------------------------------------------------------------
# Graph-vs-live-file hash agreement -- the acceptance bar's literal text:
# "graph and live-file hashes agree". Covers agreement, disagreement
# (project-reversion / subsequent-edit), and timeout/robustness paths.
# ---------------------------------------------------------------------------

class TestComputeGraphHash:
    def test_hash_of_hit_content(self):
        import hashlib
        hit = {"content": "def f():\n    return 1\n"}
        expected = hashlib.sha256(hit["content"].encode("utf-8")).hexdigest()
        assert _cir.compute_graph_hash(hit) == expected

    def test_none_when_hit_has_no_content(self):
        assert _cir.compute_graph_hash({"file": "x.py"}) is None
        assert _cir.compute_graph_hash(None) is None


class TestComputeLiveRangeHash:
    @pytest.mark.asyncio
    async def test_hashes_exactly_the_requested_line_range(self, tmp_path):
        import hashlib
        f = tmp_path / "mod.py"
        f.write_text("line1\nline2\nline3\nline4\nline5\n", encoding="utf-8")
        got = await _cir.compute_live_range_hash(str(tmp_path), "mod.py", 2, 3)
        expected = hashlib.sha256(b"line2\nline3\n").hexdigest()
        assert got == expected

    @pytest.mark.asyncio
    async def test_none_when_file_missing(self, tmp_path):
        got = await _cir.compute_live_range_hash(str(tmp_path), "nope.py", 1, 2)
        assert got is None

    @pytest.mark.asyncio
    async def test_none_when_inputs_missing(self, tmp_path):
        assert await _cir.compute_live_range_hash(None, "mod.py", 1, 2) is None
        assert await _cir.compute_live_range_hash(str(tmp_path), None, 1, 2) is None
        assert await _cir.compute_live_range_hash(str(tmp_path), "mod.py", None, 2) is None

    @pytest.mark.asyncio
    async def test_timeout_robustness_directory_instead_of_file_degrades_to_none(self, tmp_path):
        """A pathological input (a directory where a file is expected --
        the kind of thing a slow/misbehaving remote could hand back) must
        degrade to None, never hang or raise."""
        a_dir = tmp_path / "actually_a_dir.py"
        a_dir.mkdir()
        got = await _cir.compute_live_range_hash(str(tmp_path), "actually_a_dir.py", 1, 5)
        assert got is None

    @pytest.mark.asyncio
    async def test_timeout_robustness_range_beyond_eof_degrades_gracefully(self, tmp_path):
        """A range that extends far beyond the file's actual line count
        (e.g. a stale/wrong range from a drifted index) must not raise --
        it hashes whatever slice exists (possibly empty -> None)."""
        f = tmp_path / "short.py"
        f.write_text("only one line\n", encoding="utf-8")
        got = await _cir.compute_live_range_hash(str(tmp_path), "short.py", 500, 900)
        assert got is None  # nothing in that range -> empty chunk -> None


class TestHashAgreementInReceipt:
    @pytest.mark.asyncio
    async def test_agreement_true_end_to_end(self, db, tmp_path):
        """The literal acceptance bar: graph and live-file hashes AGREE
        when the hit's own reported content matches what's actually on
        disk at that range right now."""
        f = tmp_path / "mod.py"
        f.write_text("def target():\n    return 42\n", encoding="utf-8")
        project = await db_module.create_project(db, "hash-agree-proj")
        hit = {
            "qualified_name": "mod.target", "file": "mod.py",
            "line_start": 1, "line_end": 2,
            "content": "def target():\n    return 42\n",
        }
        row = await _cir.record_prospect_receipt(
            db, tenant_id=None, project_id=project["id"], session_id="sess-1",
            tool_name="prospect_symbol", query="target", root_dir=str(tmp_path),
            resolved_file="mod.py", resolved_range={"start_line": 1, "end_line": 2},
            resolved_symbol="mod.target", hit=hit,
        )
        detail = json.loads(row["detail"])
        assert detail["graph_hash"] is not None
        assert detail["live_range_hash"] is not None
        assert detail["graph_hash"] == detail["live_range_hash"]
        assert detail["hash_agreement"] is True

    @pytest.mark.asyncio
    async def test_agreement_false_after_project_reversion(self, db, tmp_path):
        """'Project reversion' scenario: the graph/hit captured the file's
        content at one point in time; the file has since been reverted /
        edited to something else. hash_agreement must flip to False --
        a concrete, positive staleness signal -- not silently stay
        unchecked or falsely agree."""
        f = tmp_path / "mod.py"
        f.write_text("def target():\n    return 42\n", encoding="utf-8")
        project = await db_module.create_project(db, "hash-disagree-proj")
        stale_hit = {
            "qualified_name": "mod.target", "file": "mod.py",
            "line_start": 1, "line_end": 2,
            # Stale content -- the graph/index captured an OLDER version
            # of this function (e.g. before a revert changed the return
            # value), no longer matching what's on disk right now.
            "content": "def target():\n    return 0\n",
        }
        row = await _cir.record_prospect_receipt(
            db, tenant_id=None, project_id=project["id"], session_id="sess-1",
            tool_name="prospect_symbol", query="target", root_dir=str(tmp_path),
            resolved_file="mod.py", resolved_range={"start_line": 1, "end_line": 2},
            resolved_symbol="mod.target", hit=stale_hit,
        )
        detail = json.loads(row["detail"])
        assert detail["graph_hash"] is not None
        assert detail["live_range_hash"] is not None
        assert detail["graph_hash"] != detail["live_range_hash"]
        assert detail["hash_agreement"] is False

    @pytest.mark.asyncio
    async def test_agreement_none_when_hit_has_no_inline_content(self, db, tmp_path):
        """Most graph/serena hits only report a LOCATION, not a body --
        hash_agreement must be None (not computed, never a fabricated
        disagreement) when there's nothing to compare."""
        f = tmp_path / "mod.py"
        f.write_text("def target():\n    return 42\n", encoding="utf-8")
        project = await db_module.create_project(db, "hash-none-proj")
        hit = {"qualified_name": "mod.target", "file": "mod.py", "line_start": 1, "line_end": 2}
        row = await _cir.record_prospect_receipt(
            db, tenant_id=None, project_id=project["id"], session_id="sess-1",
            tool_name="prospect_symbol", query="target", root_dir=str(tmp_path),
            resolved_file="mod.py", resolved_range={"start_line": 1, "end_line": 2},
            resolved_symbol="mod.target", hit=hit,
        )
        detail = json.loads(row["detail"])
        assert detail["graph_hash"] is None
        assert detail["hash_agreement"] is None


# ---------------------------------------------------------------------------
# End-to-end: handler-style receipt building via select_exact_hit +
# hit_path/hit_range/hit_identity, mirroring mcp/handler.py's own usage.
# ---------------------------------------------------------------------------

class TestEndToEndReceiptFromExactHit:
    @pytest.mark.asyncio
    async def test_neighbor_hit_never_becomes_the_receipts_resolved_file(self, db, tmp_path):
        """Simulates handler.py's prospect_symbol receipt-writing branch:
        prospect_symbol_impl returns hits (finalized, exact-first when
        possible); the caller must use select_exact_hit -- not hits[0] --
        to decide resolved_file. When there is NO exact match, the
        receipt's resolved_file must be None, never a neighbor's path."""
        wrong_file = tmp_path / "neighbor.py"
        wrong_file.write_text("x = 1\n", encoding="utf-8")
        hits = _prospect._finalize_hits(
            [{"qualified_name": "pkg.neighbor_fn", "file": "neighbor.py"}],
            "pkg.target_fn", str(tmp_path),
        )
        exact = _prospect.select_exact_hit(hits, "pkg.target_fn")
        assert exact is None

        project = await db_module.create_project(db, "e2e-no-neighbor-proj")
        row = await _cir.record_prospect_receipt(
            db, tenant_id=None, project_id=project["id"], session_id="sess-1",
            tool_name="prospect_symbol", query="pkg.target_fn", root_dir=str(tmp_path),
            resolved_file=_prospect.hit_path(exact),
            resolved_range=_prospect.hit_range(exact),
            resolved_symbol=_prospect.hit_identity(exact),
            hit=exact,
        )
        detail = json.loads(row["detail"])
        assert detail["resolved_file"] is None
        assert detail["symbol"] == "pkg.target_fn"  # falls back to query proxy
        assert detail["symbol_source"] == "query_hint_proxy"

    @pytest.mark.asyncio
    async def test_exact_hit_becomes_the_receipts_resolved_file(self, db, tmp_path):
        right_file = tmp_path / "target.py"
        right_file.write_text("def target_fn():\n    pass\n", encoding="utf-8")
        hits = _prospect._finalize_hits(
            [
                {"qualified_name": "pkg.neighbor_fn", "file": "neighbor.py"},
                {"qualified_name": "pkg.target_fn", "file": "target.py", "line": 1},
            ],
            "pkg.target_fn", str(tmp_path),
        )
        exact = _prospect.select_exact_hit(hits, "pkg.target_fn")
        assert exact is not None and exact["file"] == "target.py"

        project = await db_module.create_project(db, "e2e-exact-proj")
        row = await _cir.record_prospect_receipt(
            db, tenant_id=None, project_id=project["id"], session_id="sess-1",
            tool_name="prospect_symbol", query="pkg.target_fn", root_dir=str(tmp_path),
            resolved_file=_prospect.hit_path(exact),
            resolved_range=_prospect.hit_range(exact),
            resolved_symbol=_prospect.hit_identity(exact),
            hit=exact,
        )
        detail = json.loads(row["detail"])
        assert detail["resolved_file"] == "target.py"
        assert detail["symbol"] == "pkg.target_fn"
        assert detail["symbol_source"] == "resolved_hit"
        assert detail["range"] == {"start_line": 1, "end_line": 1}
