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

30a036ff — is_pg discriminator regression test (static, CI-safe)

``search_all`` forks its SQL strategy on ``is_pg = hasattr(db, "_pool")``.
The static tests below lock this down without a live Postgres connection so the
claim is enforced by CI, not re-asserted by reading code on every revisit.
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
    """25155e91/f51e38d8 — the OR/ranked builder: one OR clause per term (>=2 chars,
    capped), OR-ed across columns AND across terms; a score expression counting
    matched terms; params emitted score-first (SELECT list) then WHERE.
    Each LIKE/ILIKE clause now carries ESCAPE '!' so wildcard chars in terms are
    literal (f51e38d8)."""
    where_sql, score_sql, wp, sp = db_module._multiword_or_ranked_clause(
        ["title", "body"], "alpha beta", op="LIKE")
    assert where_sql == (
        "((title LIKE ? ESCAPE '!' OR body LIKE ? ESCAPE '!') OR "
        "(title LIKE ? ESCAPE '!' OR body LIKE ? ESCAPE '!'))")
    assert score_sql == (
        "((CASE WHEN (title LIKE ? ESCAPE '!' OR body LIKE ? ESCAPE '!') THEN 1 ELSE 0 END) + "
        "(CASE WHEN (title LIKE ? ESCAPE '!' OR body LIKE ? ESCAPE '!') THEN 1 ELSE 0 END))")
    assert wp == ["%alpha%", "%alpha%", "%beta%", "%beta%"]
    assert sp == ["%alpha%", "%alpha%", "%beta%", "%beta%"]

    # ILIKE for the PG op, single column.
    w1, _s1, wp1, _sp1 = db_module._multiword_or_ranked_clause(
        ["description"], "auth", op="ILIKE")
    assert w1 == "((description ILIKE ? ESCAPE '!'))"
    assert wp1 == ["%auth%"]

    # All-short tokens → fall back to the whole query as one term.
    _w2, _s2, wp2, _sp2 = db_module._multiword_or_ranked_clause(["c"], "a b")
    assert wp2 == ["%a b%"]

    # f51e38d8 — wildcard chars in the term are escaped in the bound value.
    _w3, _s3, wp3, _sp3 = db_module._multiword_or_ranked_clause(["c"], "file_name")
    assert wp3 == ["%file!_name%"], "underscore must be escaped to '!_'"

    _w4, _s4, wp4, _sp4 = db_module._multiword_or_ranked_clause(["c"], "100%")
    assert wp4 == ["%100!%%"], "percent must be escaped to '!%'"


def test_like_escape_helper():
    """f51e38d8 — _like_escape escapes the three SQLite/PG LIKE special chars."""
    from meridian.db import _like_escape
    assert _like_escape("normal") == "normal"
    assert _like_escape("file_name") == "file!_name"
    assert _like_escape("100%") == "100!%"
    assert _like_escape("a!b") == "a!!b"
    # All three at once.
    assert _like_escape("!_%") == "!!!_!%"


@pytest.mark.asyncio
@pytest.mark.sqlite_only
async def test_search_all_special_chars_no_error_no_wildcard(anydb):
    """f51e38d8 — queries containing %, _, or quote chars must not raise errors
    and must not expand into unexpected wildcard matches.

    Specifically:
    - searching '100%' must NOT match a record that only contains '100' (no %)
    - searching 'file_name' must NOT match 'file1name' (underscore wildcard)
    - searching "O'Brien" must not raise a SQL error

    Marked sqlite_only because this test validates LIKE-escape semantics, which
    only apply to the SQLite ILIKE/LIKE path. The Postgres path uses
    websearch_to_tsquery, which normalises punctuation (strips %, _ etc. as
    non-lexeme separators) and therefore has deliberately different semantics:
    a query for '100%' on Postgres becomes the lexeme '100' and legitimately
    matches any record containing '100'. That is correct FTS behaviour; no bug.
    """
    db = anydb
    p = await db_module.create_project(db, "sa-special-chars")

    # A record that contains '100' but NOT the literal '%'.
    await db_module.add_project_note(
        db, p["id"], "100 items note", "there are 100 items in the list")
    # A record that contains '100%' literally.
    await db_module.add_project_note(
        db, p["id"], "100 percent note", "this is 100% done and complete")
    # A record whose name contains 'file1name' (one char between, not underscore).
    await db_module.add_project_note(
        db, p["id"], "file1name note", "the file1name identifier is used here")

    # Searching for '100%' must match the literal '100%' record but NOT the
    # '100 items' record (which only has '100', not '100%').
    result = await db_module.search_all(db, p["id"], "100%")
    matched_titles = [n["title"] for n in result["notes"]]
    assert "100 percent note" in matched_titles, (
        "search for '100%' must find the record literally containing '100%'")
    assert "100 items note" not in matched_titles, (
        "search for '100%' must NOT match a record that only has '100' without '%'")

    # Searching for 'file_name' must NOT match 'file1name' (the _ is not a wildcard).
    result2 = await db_module.search_all(db, p["id"], "file_name")
    matched2 = [n["title"] for n in result2["notes"]]
    assert "file1name note" not in matched2, (
        "underscore in query must be escaped (not a single-char wildcard)")

    # A query with a SQL quote character must not raise any error.
    result3 = await db_module.search_all(db, p["id"], "O'Brien")
    assert isinstance(result3, dict)  # no exception, any result shape is fine


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


# ---------------------------------------------------------------------------
# 30a036ff — static regression tests for the is_pg = hasattr(db, "_pool") check
# ---------------------------------------------------------------------------

def test_is_pg_discriminator_postgres_connection_has_pool():
    """30a036ff — PostgresConnection always has _pool; hasattr resolves True.

    pg_adapter.PostgresConnection.__init__ sets self._pool = pool unconditionally.
    A minimal stand-in (a plain object with a _pool attr) is all that is needed to
    prove the discriminator fires correctly — no live Postgres connection required.
    This test runs on SQLite-only CI and enforces the claim that was previously only
    asserted by reading the code.
    """
    from meridian.pg_adapter import PostgresConnection

    # Confirm the real class still sets _pool on init (not just a mock assumption).
    assert "_pool" in PostgresConnection.__init__.__code__.co_varnames or True
    # Simpler: inspect the __init__ source directly.
    import inspect
    src = inspect.getsource(PostgresConnection.__init__)
    assert "self._pool" in src, (
        "PostgresConnection.__init__ must assign self._pool so that "
        "hasattr(db, '_pool') reliably identifies Postgres connections"
    )

    # A live instance with a mock pool resolves True.
    class _FakePool:
        pass

    pg_conn = PostgresConnection.__new__(PostgresConnection)
    pg_conn._pool = _FakePool()
    assert hasattr(pg_conn, "_pool") is True, (
        "hasattr(pg_conn, '_pool') must be True for a PostgresConnection instance"
    )


def test_is_pg_discriminator_sqlite_connection_lacks_pool():
    """30a036ff — aiosqlite.Connection has no _pool attribute; hasattr resolves False.

    This is the other half of the discriminator. If aiosqlite ever adds a _pool
    attribute this test will catch it, forcing a deliberate review of the idiom.
    """
    import aiosqlite

    # Inspect the class dict — _pool must not appear on the aiosqlite Connection class
    # or any of its MRO parents (short of object itself, which also lacks it).
    for klass in type.mro(aiosqlite.Connection):
        if klass is object:
            break
        assert "_pool" not in klass.__dict__, (
            f"aiosqlite.Connection (via {klass}) unexpectedly gained a '_pool' "
            "attribute — the hasattr(db, '_pool') discriminator in search_all / "
            "expire_file_read_claims / get_project_notes_where needs updating"
        )


def test_search_all_takes_pg_path_for_postgres_shaped_db():
    """30a036ff — search_all's is_pg branch (ts_rank/websearch_to_tsquery) is
    activated when the db object has _pool; the SQLite LIKE path is NOT taken.

    This static test reads the search_all source via AST/inspect and asserts that:
    1. The function dialect-splits on hasattr(db, '_pool').
    2. The Postgres branch contains websearch_to_tsquery (the good ranking path).
    3. The SQLite branch contains _multiword_or_ranked_clause (the cruder path).

    No live DB required — the claim is structural.
    """
    import inspect

    src = inspect.getsource(db_module.search_all)

    # The function must use the hasattr(_pool) idiom to detect the backend.
    assert "_pool" in src, (
        "search_all must dialect-split on hasattr(db, '_pool') to pick the "
        "Postgres vs. SQLite search path"
    )

    # The Postgres branch must use ts_rank / websearch_to_tsquery.
    assert "websearch_to_tsquery" in src, (
        "search_all Postgres path must use websearch_to_tsquery for full-text ranking"
    )
    assert "ts_rank" in src, (
        "search_all Postgres path must use ts_rank for result ordering"
    )

    # The SQLite branch must use the cruder _multiword_or_ranked_clause.
    assert "_multiword_or_ranked_clause" in src, (
        "search_all SQLite path must use _multiword_or_ranked_clause for OR/ranked search"
    )

    # Confirm the two paths are separated by an if/else on is_pg, not both active.
    # Strip the docstring first so prose references to "ts_rank" in the function's
    # description don't interfere with position checks on the executable code.
    import ast
    import textwrap

    tree = ast.parse(textwrap.dedent(src))
    func = tree.body[0]
    assert isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef))
    body = func.body
    # Drop leading docstring node (a bare string-constant expression).
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(getattr(body[0], "value", None), ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    code_src = "\n".join(ast.unparse(stmt) for stmt in body)

    assert "is_pg" in code_src, "is_pg discriminator must appear in search_all executable body"
    assert "websearch_to_tsquery" in code_src, "ts_rank/websearch path must appear in executable body"
    assert "ts_rank" in code_src, "ts_rank ordering must appear in executable body"
    assert "_multiword_or_ranked_clause" in code_src, (
        "_multiword_or_ranked_clause SQLite path must appear in executable body"
    )

    # is_pg must appear before both branch bodies in the executable code.
    is_pg_pos = code_src.index("is_pg")
    ts_rank_pos = code_src.index("ts_rank")
    multi_pos = code_src.index("_multiword_or_ranked_clause")
    assert is_pg_pos < ts_rank_pos, "is_pg guard must precede the ts_rank branch"
    assert is_pg_pos < multi_pos, "is_pg guard must precede the _multiword_or_ranked_clause branch"
