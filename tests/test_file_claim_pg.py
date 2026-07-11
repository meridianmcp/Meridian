"""949cf1e5 — regression coverage for the Postgres-only file_read_claims TTL crash.

`get_file_claims` / `claim_file` first call `expire_file_read_claims`, whose cleanup
`DELETE FROM file_read_claims WHERE expires_at < datetime('now')` crashed on Postgres
with ``operator does not exist: timestamp with time zone < text``.

Root cause: unlike every sibling lock table (file_locks / resource_locks /
file_symbol_claims), whose ``expires_at`` / ``claimed_at`` / ``released_at`` columns are
TEXT on Postgres, ``file_read_claims.expires_at`` is a TIMESTAMPTZ (pg_adapter). The
shared ``datetime('now')`` form is rewritten by pg_adapter into a ``to_char(...)`` *text*
expression, so the comparison became ``timestamptz < text`` — which Postgres rejects.
SQLite is loosely typed so it hid the mismatch, and CI is SQLite-only so it never caught
it. The fix dialect-splits the cleanup: ``now()`` (a real timestamp) on Postgres,
``datetime('now')`` (text ISO) on SQLite.

Two layers of coverage:
* Behavioural (``anydb``): the full claim -> get_file_claims -> TTL-cleanup path runs on
  SQLite always and on real Postgres when TEST_DATABASE_URL is set (auto-skipped
  locally, like the search_all PG tests). On the PG run this exercises the exact SQL
  that used to crash and asserts the expiry behaviour is correct.
* Static (SQLite-only CI safety net): assert the Postgres branch of
  ``expire_file_read_claims`` does NOT compare the timestamptz column against the text
  ``datetime('now')`` expression — the check that would have caught this in SQLite-only
  CI.
"""
from __future__ import annotations

import ast
import inspect
import textwrap

import pytest

from meridian import db as db_module


# --------------------------------------------------------------------------- #
# Behavioural — runs on SQLite always, real Postgres when TEST_DATABASE_URL set
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_read_claim_and_get_file_claims_no_crash(anydb):
    """claim_file(mode='read') + get_file_claims run the TTL-cleanup with no crash.

    On the Postgres variant this is the exact path that raised
    ``operator does not exist: timestamp with time zone < text`` before the fix.
    """
    db = anydb
    p = await db_module.create_project(db, "read-claim-pg")
    s = await db_module.register_session(db, p["id"], "reader-session")

    claim = await db_module.claim_file(
        db, "meridian/db/__init__.py", s["id"], mode="read"
    )
    assert claim["claimed"] is True
    assert claim["claim_mode"] == "read"

    # get_file_claims calls expire_file_read_claims first — the crash site on PG.
    result = await db_module.get_file_claims(db, "meridian/db/__init__.py")
    assert result["file_path"] == "meridian/db/__init__.py"
    # The live read claim is surfaced (still within its 2h TTL). read_claims is a
    # list of row dicts (from _all_read_claims).
    assert any(
        rc.get("session_id") == s["id"] for rc in result.get("read_claims", [])
    ), "live read claim must be reported by get_file_claims"


@pytest.mark.asyncio
async def test_expire_file_read_claims_drops_expired_keeps_live(anydb):
    """The TTL cleanup deletes a lapsed read claim and keeps a live one.

    Exercises the real comparison on both backends: a claim whose expires_at is in
    the past must be swept, a claim expiring in the future must survive. On Postgres
    the column is TIMESTAMPTZ, so this validates the ``now()`` comparison end-to-end.
    """
    db = anydb
    p = await db_module.create_project(db, "read-claim-ttl")
    live = await db_module.register_session(db, p["id"], "live-reader")
    stale = await db_module.register_session(db, p["id"], "stale-reader")

    # Live claim (default 2h TTL) — must survive the sweep.
    await db_module.claim_file(
        db, "meridian/server.py", live["id"], mode="read"
    )
    # Stale claim: insert directly with an expires_at one hour in the past.
    # datetime('now', '-1 hours') is a text expr on SQLite and (via pg_adapter)
    # a to_char(...) text value that Postgres implicitly casts into the
    # TIMESTAMPTZ column on INSERT — insertion has never been the failing path.
    await db.execute(
        "INSERT INTO file_read_claims "
        "(id, file_path, session_id, claimed_at, expires_at) "
        "VALUES (?, ?, ?, datetime('now'), datetime('now', '-1 hours'))",
        (db_module._new_id(), "meridian/server.py", stale["id"]),
    )
    await db.commit()

    # Run the cleanup directly (the crash site).
    await db_module.expire_file_read_claims(db)

    survivors = await _read_claim_sessions(db, "meridian/server.py")
    assert live["id"] in survivors, "live read claim must survive the TTL sweep"
    assert stale["id"] not in survivors, "expired read claim must be swept"


# --------------------------------------------------------------------------- #
# Static safety net — catches the timestamptz-vs-text bug class in SQLite-only CI
# --------------------------------------------------------------------------- #

def test_expire_query_does_not_compare_timestamptz_column_to_text():
    """The Postgres branch must not compare expires_at against datetime('now').

    file_read_claims.expires_at is TIMESTAMPTZ on Postgres; pg_adapter turns
    ``datetime('now')`` into a text ``to_char(...)`` expression, so any
    ``expires_at < datetime('now')`` on the PG path is a ``timestamptz < text``
    crash. Assert the source keeps the text form gated behind the SQLite branch and
    uses a real timestamp (``now()``) on Postgres. This runs even on SQLite-only CI,
    which is where the original bug slipped through.
    """
    src = inspect.getsource(db_module.expire_file_read_claims)

    # Strip the docstring so prose that mentions datetime('now') (explaining the bug)
    # doesn't confuse the executable-code check below. Parse the function body via AST
    # and reconstruct the source without the leading string-expression statement.
    tree = ast.parse(textwrap.dedent(src))
    func = tree.body[0]
    assert isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef)
    body = func.body
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(getattr(body[0], "value", None), ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]  # drop the docstring statement
    code_src = "\n".join(ast.unparse(stmt) for stmt in body)

    # Must branch on the Postgres backend (the hasattr(db, "_pool") idiom).
    assert "_pool" in code_src, (
        "expire_file_read_claims must dialect-split on the PG backend"
    )

    # Reduce the executable source to the Postgres branch: everything up to the
    # `else:` that begins the SQLite branch. Normalize quotes since ast.unparse may
    # re-emit string literals with double quotes.
    pg_branch = code_src.split("else:", 1)[0].replace('"', "'")
    assert "now()" in pg_branch, "PG branch must compare against a real now() timestamp"
    assert "datetime('now'" not in pg_branch, (
        "PG branch must NOT compare the TIMESTAMPTZ column against datetime('now') "
        "(pg_adapter rewrites it to text -> 'timestamp with time zone < text' crash)"
    )

    # And run the adapter to prove the emitted PG SQL isn't a text comparison.
    from meridian.pg_adapter import _pg_adapt_sql

    pg_sql, _ = _pg_adapt_sql(
        "DELETE FROM file_read_claims WHERE expires_at < now()", ()
    )
    assert "to_char(" not in pg_sql, (
        "the fixed PG comparison must stay a timestamp comparison, not to_char text"
    )


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

async def _read_claim_sessions(db, file_path: str) -> set[str]:
    """Session ids holding a read claim on ``file_path`` (direct table read)."""
    async with db.execute(
        "SELECT session_id FROM file_read_claims WHERE file_path = ?",
        (file_path,),
    ) as cur:
        rows = await cur.fetchall()
    result: set[str] = set()
    for row in rows:
        d = row if isinstance(row, dict) else {"session_id": row[0]}
        result.add(d["session_id"])
    return result
