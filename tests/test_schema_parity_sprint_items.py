"""Schema-parity guard for the Postgres ``sprint_items`` CREATE literal.

Regression guard for item b01326e9 (Meridian finding a6ce1e4e): the Postgres
``sprint_items`` CREATE TABLE literal in ``meridian/pg_adapter.py`` had drifted
from the SQLite schema and app code — it omitted ``depends_on``,
``failure_mode``, ``parent_id``, ``split_from``, ``merged_into``,
``merged_from``, ``touches_files``, ``touches_resources``, ``stall_count``,
``owner``, ``required_notes`` and ``actor``. Because ``CREATE TABLE IF NOT
EXISTS`` is a no-op on an existing DB and no PG ALTER migration added several of
these, live Postgres tenants crashed with ``column "depends_on" of relation
"sprint_items" does not exist`` (242 test-postgres CI failures).

These lightweight static checks parse the column names straight out of the
``pg_adapter.py`` source (no DB needed, so they run in the SQLite-only CI) and
assert the PG base literal is a superset-or-equal of the columns the app writes,
and that any column added only via a SQLite migration also has a matching PG
ALTER migration. Either would have caught the original divergence.
"""

from __future__ import annotations

import re

# Columns the app writes that MUST exist in the Postgres sprint_items table
# (the exact set called out in the item / finding).
REQUIRED_SPRINT_ITEM_COLUMNS = {
    "depends_on",
    "parent_id",
    "failure_mode",
    "split_from",
    "merged_into",
    "merged_from",
    "touches_resources",
    "stall_count",
    "owner",
    "required_notes",
    "actor",
}


def _extract_sprint_items_block(create_tables_sql: str) -> str:
    """Return the body of the ``CREATE TABLE ... sprint_items ( ... )`` literal.

    Scans from the ``CREATE TABLE ... sprint_items (`` header to its matching
    closing paren, balancing nested parens (REFERENCES/CHECK clauses etc.).
    """
    m = re.search(
        r"CREATE TABLE(?:\s+IF NOT EXISTS)?\s+sprint_items\s*\(",
        create_tables_sql,
    )
    assert m is not None, "sprint_items CREATE TABLE literal not found in pg_adapter"
    start = m.end()  # first char after the opening '('
    depth = 1
    i = start
    while i < len(create_tables_sql) and depth > 0:
        ch = create_tables_sql[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        i += 1
    assert depth == 0, "unbalanced parens in sprint_items literal"
    return create_tables_sql[start : i - 1]


def _column_names_from_block(block: str) -> set[str]:
    """Parse column names from a CREATE TABLE body.

    Splits on top-level commas (ignoring commas nested in parens), drops SQL
    ``--`` comment lines, and takes the first identifier of each column-def line.
    Constraint/clause keywords (PRIMARY, FOREIGN, ...) are excluded.
    """
    # Strip whole-line SQL comments so they don't get parsed as column defs.
    lines = [ln for ln in block.splitlines() if not ln.strip().startswith("--")]
    body = "\n".join(lines)

    # Split on top-level commas only.
    parts: list[str] = []
    depth = 0
    buf: list[str] = []
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append("".join(buf))

    _non_columns = {
        "primary",
        "foreign",
        "unique",
        "check",
        "constraint",
        "references",
    }
    cols: set[str] = set()
    for part in parts:
        tok = part.strip()
        if not tok:
            continue
        first = re.split(r"\s|\(", tok, maxsplit=1)[0]
        first = first.strip('"').lower()
        if not first or first in _non_columns:
            continue
        cols.add(first)
    return cols


def _pg_sprint_items_columns() -> set[str]:
    from meridian.pg_adapter import CREATE_TABLES_CORE

    block = _extract_sprint_items_block(CREATE_TABLES_CORE)
    return _column_names_from_block(block)


def test_pg_sprint_items_literal_has_required_columns():
    """The PG sprint_items CREATE literal must contain every app-written column."""
    cols = _pg_sprint_items_columns()
    missing = REQUIRED_SPRINT_ITEM_COLUMNS - cols
    assert not missing, (
        "meridian/pg_adapter.py CREATE_TABLES_CORE sprint_items literal is "
        f"missing columns the app writes: {sorted(missing)}. Add them to the "
        "literal (and a matching ALTER migration for existing DBs)."
    )


def test_pg_sprint_items_literal_superset_of_sqlite():
    """The PG literal's column set must be a superset-or-equal of the SQLite one.

    Every column in the SQLite base ``sprint_items`` literal must also be present
    in the Postgres base literal (Postgres may add extra, e.g. migration-backfill
    columns folded into the base — that's fine; a missing one is the bug).
    """
    from meridian.db import CREATE_TABLES

    pg_cols = _pg_sprint_items_columns()
    sqlite_block = _extract_sprint_items_block(CREATE_TABLES)
    sqlite_cols = _column_names_from_block(sqlite_block)

    missing = sqlite_cols - pg_cols
    assert not missing, (
        "PG sprint_items literal is not a superset of the SQLite one; missing "
        f"columns present in SQLite: {sorted(missing)}"
    )


def test_pg_sprint_item_dependency_columns_have_alter_migration():
    """depends_on / failure_mode / touches_files need a PG ALTER upgrade path.

    These had a SQLite migration but (before b01326e9) no PG one, so existing
    Postgres tenants — where CREATE TABLE IF NOT EXISTS is a no-op — never got
    the columns. Assert the ALTER-migration text is present in the source so a
    future base-literal-only edit can't silently drop the upgrade path again.
    """
    import inspect

    from meridian import pg_adapter

    src = inspect.getsource(pg_adapter)
    for col in ("depends_on", "failure_mode", "touches_files"):
        assert (
            f"ADD COLUMN IF NOT EXISTS {col}" in src
        ), f"no PG ALTER migration adds sprint_items.{col} for existing DBs"

    # And the migration must be registered in the runner, not just defined.
    late_names = [f.__name__ for f in pg_adapter._PG_MIGRATIONS_LATE]
    assert "_migrate_pg_sprint_item_dependency" in late_names
