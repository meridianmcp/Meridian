"""25155e91 — search_all graceful degradation for multi-word NL queries.

Regression coverage for the bug where a long multi-word natural-language query
returned ZERO results the moment one rare/absent token was present, even though
several terms clearly matched a record. Root cause: search_all ANDed every query
term (SQLite: _multiword_match_clause; Postgres: websearch_to_tsquery, whose
whitespace default is AND). The fix ORs the terms and ranks by how many matched,
so results degrade gracefully to the most-relevant rows instead of to nothing.

The cross-backend tests use the ``anydb`` fixture, which runs on SQLite and
(when TEST_DATABASE_URL is set) Postgres, so both code paths are covered; the PG
variant auto-skips locally.
"""

from __future__ import annotations

import pytest

from meridian import db as db_module


# The item's exact repro query.
_LONG_QUERY = "img127 coverage gap single-path BFS x=768 x=1511"


@pytest.mark.asyncio
async def test_search_all_multiword_nl_query_degrades_gracefully(anydb):
    """25155e91 — the item's exact failing query.

    A record whose text contains several of the query's terms (img127, coverage,
    gap, single-path, BFS) but not the punctuation-heavy coordinate tokens
    (x=768, x=1511) must still be FOUND. Before the fix the AND semantics
    required every term, so this returned zero.
    """
    db = anydb
    p = await db_module.create_project(db, "sa-nl-degrade")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "img127 coverage gap")
    await db_module.patch_sprint_item(
        db, p["id"], item["id"],
        notes="single-path BFS traversal misses a coverage gap around img127")

    # Single-word query works (baseline that always worked).
    single = await db_module.search_all(db, p["id"], "img127")
    assert any(i["title"] == "img127 coverage gap" for i in single["sprint_items"])

    # The full multi-word NL query now ALSO finds the record (was 0 before).
    result = await db_module.search_all(db, p["id"], _LONG_QUERY)
    assert any(i["title"] == "img127 coverage gap" for i in result["sprint_items"]), (
        "multi-word NL query must degrade gracefully to the relevant record, "
        "not return zero because 'x=768'/'x=1511' are absent")


@pytest.mark.asyncio
async def test_search_all_multiword_unrelated_still_empty(anydb):
    """A wholly unrelated multi-word query still returns nothing — graceful
    degradation must not turn into 'match everything'."""
    db = anydb
    p = await db_module.create_project(db, "sa-unrelated")
    await db_module.add_sprint_item(db, p["id"], "v1", "img127 coverage gap")
    result = await db_module.search_all(db, p["id"], "nonexistent zzzzz qqqqq")
    assert result["total"] == 0


@pytest.mark.asyncio
async def test_search_all_partial_match_beats_none(anydb):
    """A record matching SOME terms surfaces; a decoy matching NONE does not."""
    db = anydb
    p = await db_module.create_project(db, "sa-partial")
    await db_module.add_project_note(
        db, p["id"], "Relevant", "img127 coverage gap single-path notes")
    await db_module.add_project_note(
        db, p["id"], "Decoy", "completely unrelated content about billing")
    result = await db_module.search_all(db, p["id"], _LONG_QUERY)
    titles = [n["title"] for n in result["notes"]]
    assert "Relevant" in titles
    assert "Decoy" not in titles


@pytest.mark.asyncio
async def test_search_all_ranks_more_complete_matches_first(anydb):
    """Rows matching more query terms rank ahead of rows matching fewer."""
    db = anydb
    p = await db_module.create_project(db, "sa-rank")
    # 'more' matches 3 terms; 'fewer' matches 1. Distinct enough that ts_rank
    # (PG) and the term-count score (SQLite) both order 'more' first.
    await db_module.add_project_note(
        db, p["id"], "more", "alpha beta gamma appear together here")
    await db_module.add_project_note(
        db, p["id"], "fewer", "alpha appears but not the others at all")
    result = await db_module.search_all(db, p["id"], "alpha beta gamma")
    titles = [n["title"] for n in result["notes"]]
    assert titles[:2] == ["more", "fewer"], (
        f"expected the 3-term match ranked first, got {titles}")


@pytest.mark.asyncio
async def test_search_all_single_word_unchanged(anydb):
    """A single-word query behaves exactly as before (title + body match)."""
    db = anydb
    p = await db_module.create_project(db, "sa-single")
    await db_module.add_sprint_item(db, p["id"], "v1", "Implement rate limiting")
    result = await db_module.search_all(db, p["id"], "limiting")
    assert any(i["title"] == "Implement rate limiting" for i in result["sprint_items"])


@pytest.mark.asyncio
async def test_search_all_result_shape_has_no_internal_score(anydb):
    """The internal ranking column (_match_score on SQLite) never leaks into
    returned rows — result shape stays identical across backends."""
    db = anydb
    p = await db_module.create_project(db, "sa-shape")
    await db_module.add_project_note(db, p["id"], "N", "alpha beta gamma")
    result = await db_module.search_all(db, p["id"], "alpha beta")
    assert result["notes"]
    for row in result["notes"]:
        assert "_match_score" not in row


def test_multiword_or_ranked_clause_helper():
    """25155e91 — the OR/ranked builder: one OR clause per term (>=2 chars,
    capped), OR-ed across columns AND across terms; a score expression counting
    matched terms; params emitted score-first (SELECT list) then WHERE."""
    where_sql, score_sql, wp, sp = db_module._multiword_or_ranked_clause(
        ["title", "body"], "alpha beta", op="LIKE")
    assert where_sql == (
        "((title LIKE ? OR body LIKE ?) OR (title LIKE ? OR body LIKE ?))")
    assert score_sql == (
        "((CASE WHEN (title LIKE ? OR body LIKE ?) THEN 1 ELSE 0 END) + "
        "(CASE WHEN (title LIKE ? OR body LIKE ?) THEN 1 ELSE 0 END))")
    assert wp == ["%alpha%", "%alpha%", "%beta%", "%beta%"]
    assert sp == ["%alpha%", "%alpha%", "%beta%", "%beta%"]

    # ILIKE for the PG op, single column.
    w1, _s1, wp1, _sp1 = db_module._multiword_or_ranked_clause(
        ["description"], "auth", op="ILIKE")
    assert w1 == "((description ILIKE ?))"
    assert wp1 == ["%auth%"]

    # All-short tokens → fall back to the whole query as one term.
    _w2, _s2, wp2, _sp2 = db_module._multiword_or_ranked_clause(["c"], "a b")
    assert wp2 == ["%a b%"]


def test_or_tsquery_source_helper():
    """25155e91 — the Postgres OR-tsquery rewriter joins terms with ' or ' so
    websearch_to_tsquery builds an OR (a | b | c), strips boundary operator
    chars, drops literal or/and, and passes a single term through unchanged."""
    assert db_module._or_tsquery_source("alpha beta gamma") == "alpha or beta or gamma"
    # Single term unchanged (preserves existing single-word / stemmed behavior).
    assert db_module._or_tsquery_source("alpha") == "alpha"
    # Boundary quotes / leading NOT stripped so a term can't flip the whole
    # query into a phrase or negation.
    assert db_module._or_tsquery_source('"alpha" -beta') == "alpha or beta"
    # Literal reserved words are dropped from the join (no-op lexemes).
    assert db_module._or_tsquery_source("alpha or beta") == "alpha or beta"
    # Punctuation-heavy coordinate tokens survive as terms (websearch splits
    # intra-token punctuation itself).
    assert db_module._or_tsquery_source("x=768 x=1511") == "x=768 or x=1511"
    # Empty / whitespace-only falls back to the original string.
    assert db_module._or_tsquery_source("") == ""
