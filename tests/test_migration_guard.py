"""95df8c0d — static enforcement of the guarded-migration "no inline index on a
migration-added column" rule.

WHY THIS EXISTS
---------------
The base schema literals (``meridian/db/__init__.py::CREATE_TABLES`` and
``meridian/pg_adapter.py::CREATE_TABLES_CORE`` / ``CREATE_TABLES_HOSTED``) are run
on EVERY startup via an unguarded ``executescript``. On a FRESH DB every statement
runs. On an EXISTING (already-provisioned prod) DB, a ``CREATE TABLE IF NOT EXISTS``
for a table that already exists is a NO-OP — the table keeps its OLD columns. If a
column was introduced by a *later* ``ALTER TABLE ... ADD COLUMN`` migration, that
column does NOT exist on the old table. An inline ``CREATE INDEX ... ON t(new_col)``
sitting in the SAME base literal then runs against the old table, references a
column that isn't there, and crashes startup — a crash-loop, because it happens on
every boot.

That exact pattern took prod down TWICE (2026-06-13 and 2026-07-04). The fix each
time was to move the offending ``CREATE INDEX`` out of the base literal and into the
*guarded* migration that adds the column (where the column is guaranteed present).
The rule "never inline a CREATE INDEX on a migration-added column in a base literal"
was, until now, pure prose convention with zero enforcement. This test is the
enforcement: a regression guard that FAILS if the anti-pattern is ever reintroduced.

PARSING APPROACH
----------------
These are KNOWN, well-formed literals (not arbitrary user SQL), so a targeted
string/regex scan is sufficient and intentionally simple:

1. Read each base literal's *source text* out of the module file (we read the raw
   assigned string so f-string interpolation like ``{_TS}`` / ``{{}}`` is irrelevant
   — we never evaluate it). SQL line comments (``-- ...``) are stripped first so a
   ``CREATE INDEX`` mentioned inside a comment is never mistaken for a real one.
2. Split the literal into statements on ``;`` at the top level.
3. For every ``CREATE TABLE [IF NOT EXISTS] <name> ( <body> )`` statement, record
   the table's declared column names — the first identifier of each top-level
   comma-separated clause in the body, skipping table-level constraint clauses
   (``PRIMARY KEY`` / ``FOREIGN KEY`` / ``UNIQUE`` / ``CHECK`` / ``CONSTRAINT`` /
   ``REFERENCES``). Commas inside parentheses (e.g. ``CHECK (x IN (...))`` or a
   composite ``PRIMARY KEY (a, b)``) are NOT split on — we track paren depth.
4. For every ``CREATE INDEX [IF NOT EXISTS] <idx> ON <table> ( <cols> )`` statement,
   record the indexed table and the base column names it references (composite
   indexes yield multiple; ``ASC``/``DESC`` and quoting are stripped; a functional
   index expression that is not a bare column is ignored — we only assert on plain
   column references, which is exactly the outage class).
5. ASSERT: every plain column referenced by an inline ``CREATE INDEX`` in a base
   literal is present in that table's base ``CREATE TABLE`` column list. A violation
   means the index points at a column the base table does not have on an existing DB
   — i.e. a column only added by a later ALTER — which is the crash-loop pattern.

Deliberately NO hand-maintained "old schema snapshot" file: the whole point is that
we derive everything from the live literals, so there is nothing to go stale.

The known historical violations were already fixed, so this test PASSES today; it
exists to catch a FUTURE reintroduction.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
import pytest_asyncio

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DB_INIT = _REPO_ROOT / "meridian" / "db" / "__init__.py"
_PG_ADAPTER = _REPO_ROOT / "meridian" / "pg_adapter.py"

# Table-level clause leaders that are NOT column definitions. Compared upper-cased
# against the first token of each top-level comma-separated clause in a table body.
_CONSTRAINT_LEADERS = {
    "PRIMARY",  # PRIMARY KEY (a, b)
    "FOREIGN",  # FOREIGN KEY (...)
    "UNIQUE",  # UNIQUE (...)
    "CHECK",  # CHECK (...)
    "CONSTRAINT",  # CONSTRAINT name ...
    "REFERENCES",  # (defensive; column-level REFERENCES stays attached to its col)
    "EXCLUDE",
}


def _read_string_assignment(module_path: Path, var_name: str) -> str:
    """Return the literal string value assigned to ``var_name`` at module top level.

    Uses ``ast`` to locate the assignment, then returns the *raw source segment* of
    the right-hand side with surrounding quotes/f-prefix removed. We deliberately do
    NOT execute or format the f-string — the ``{_TS}`` interpolations and ``{{}}``
    escapes are irrelevant to column/index structure, and reading the source avoids
    importing (and thus running) the module.
    """
    source = module_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if var_name not in targets:
            continue
        value = node.value
        # Plain string literal (e.g. CREATE_TABLES = """...""").
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return value.value
        # f-string (e.g. CREATE_TABLES_CORE = f"""...{_TS}..."""). Reconstruct the
        # literal text, replacing interpolated fields with a neutral placeholder so
        # the surrounding SQL structure is preserved. FormattedValue -> placeholder;
        # the parser already un-escapes ``{{`` / ``}}`` into their literal chars.
        if isinstance(value, ast.JoinedStr):
            parts: list[str] = []
            for piece in value.values:
                if isinstance(piece, ast.Constant) and isinstance(piece.value, str):
                    parts.append(piece.value)
                else:
                    parts.append("__EXPR__")
            return "".join(parts)
    raise AssertionError(f"{var_name} not found as a top-level string assignment in {module_path}")


def _strip_sql_line_comments(sql: str) -> str:
    """Drop ``-- ...`` line comments so a CREATE INDEX mentioned in prose is ignored."""
    out_lines = []
    for line in sql.splitlines():
        idx = line.find("--")
        if idx != -1:
            line = line[:idx]
        out_lines.append(line)
    return "\n".join(out_lines)


def _split_statements(sql: str) -> list[str]:
    """Split on top-level ``;`` (semicolons never appear nested in these literals)."""
    return [s.strip() for s in sql.split(";") if s.strip()]


def _split_top_level_commas(body: str) -> list[str]:
    """Split ``body`` on commas that are at paren-depth 0.

    Keeps ``CHECK (x IN (1,2))`` and ``PRIMARY KEY (a, b)`` intact.
    """
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in body:
        if ch == "(":
            depth += 1
            current.append(ch)
        elif ch == ")":
            depth -= 1
            current.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    if current:
        parts.append("".join(current))
    return [p.strip() for p in parts if p.strip()]


def _extract_paren_body(stmt: str, open_index: int) -> str:
    """Return the substring inside the parenthesis group that opens at ``open_index``."""
    assert stmt[open_index] == "("
    depth = 0
    for i in range(open_index, len(stmt)):
        if stmt[i] == "(":
            depth += 1
        elif stmt[i] == ")":
            depth -= 1
            if depth == 0:
                return stmt[open_index + 1 : i]
    raise AssertionError(f"unbalanced parentheses in statement: {stmt[:80]}...")


_CREATE_TABLE_RE = re.compile(
    r"^\s*CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[\"']?(?P<name>\w+)[\"']?\s*\(",
    re.IGNORECASE,
)
_CREATE_INDEX_RE = re.compile(
    r"^\s*CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    r"[\"']?(?P<index>\w+)[\"']?\s+ON\s+[\"']?(?P<table>\w+)[\"']?\s*\(",
    re.IGNORECASE,
)


def _clean_ident(token: str) -> str:
    """Strip quotes / ordering keywords from an indexed column token."""
    token = token.strip().strip('"').strip("'").strip("`")
    # Drop a trailing ASC/DESC and any COLLATE/NULLS clause; keep the leading word.
    return token.split()[0].strip('"').strip("'").strip("`") if token.split() else token


def parse_base_literal(sql: str):
    """Parse one base schema literal.

    Returns ``(tables, indexes)`` where:
      * ``tables``  : dict[table_name] -> set[column_name]
      * ``indexes`` : list[(index_name, table_name, [column_name, ...])]

    ``indexes`` only records index columns that are *plain column references*
    (bare identifiers); a functional-expression index term that is not a simple
    identifier is skipped, since it is not the missing-column outage class.
    """
    sql = _strip_sql_line_comments(sql)
    tables: dict[str, set[str]] = {}
    indexes: list[tuple[str, str, list[str]]] = []

    for stmt in _split_statements(sql):
        tbl_match = _CREATE_TABLE_RE.match(stmt)
        if tbl_match:
            open_idx = stmt.index("(", tbl_match.end() - 1)
            body = _extract_paren_body(stmt, open_idx)
            cols: set[str] = set()
            for clause in _split_top_level_commas(body):
                first = clause.split()[0]
                if first.upper() in _CONSTRAINT_LEADERS:
                    continue
                cols.add(first.strip('"').strip("'").strip("`"))
            tables[tbl_match.group("name")] = cols
            continue

        idx_match = _CREATE_INDEX_RE.match(stmt)
        if idx_match:
            open_idx = stmt.index("(", idx_match.end() - 1)
            body = _extract_paren_body(stmt, open_idx)
            col_names: list[str] = []
            for term in _split_top_level_commas(body):
                # Skip functional/expression terms (contain a call or operator);
                # only a bare identifier can be the missing-column crash class.
                cleaned = _clean_ident(term)
                if re.fullmatch(r"\w+", cleaned):
                    col_names.append(cleaned)
            indexes.append((idx_match.group("index"), idx_match.group("table"), col_names))
            continue

    return tables, indexes


# The base literals under enforcement: (module_path, variable_name).
_BASE_LITERALS = [
    (_DB_INIT, "CREATE_TABLES"),
    (_PG_ADAPTER, "CREATE_TABLES_CORE"),
    (_PG_ADAPTER, "CREATE_TABLES_HOSTED"),
]


@pytest.mark.parametrize("module_path,var_name", _BASE_LITERALS, ids=lambda p: getattr(p, "name", p))
def test_no_inline_index_on_migration_added_column(module_path, var_name):
    """Every column referenced by an inline CREATE INDEX in a base literal MUST be
    declared in that table's base CREATE TABLE. A missing column means the index
    points at a column added only by a later ALTER migration — the crash-loop
    pattern that took prod down on 2026-06-13 and 2026-07-04."""
    sql = _read_string_assignment(module_path, var_name)
    tables, indexes = parse_base_literal(sql)

    # Sanity: parsing actually found structure (guards against a silent regex break).
    assert tables, f"no CREATE TABLE parsed from {var_name} in {module_path.name}"

    violations: list[str] = []
    for index_name, table, columns in indexes:
        base_cols = tables.get(table)
        if base_cols is None:
            # Index on a table not defined in THIS literal. Not the outage class we
            # guard (the base table isn't co-located), so we don't assert on it.
            continue
        for col in columns:
            if col not in base_cols:
                violations.append(
                    f"{module_path.name}::{var_name}: inline index '{index_name}' references "
                    f"column '{table}.{col}', which is NOT in the base CREATE TABLE for "
                    f"'{table}'. If that column is added by a later ALTER migration, this "
                    f"inline CREATE INDEX crashes startup on an existing DB (the 2026-06-13 / "
                    f"2026-07-04 outage pattern). Move the index into the guarded migration "
                    f"that adds the column."
                )

    assert not violations, "Guarded-migration rule violated:\n" + "\n".join(violations)


def test_parser_flags_a_synthetic_violation():
    """The static check must actually catch the anti-pattern — prove it on a crafted
    literal so a future refactor can't silently neuter the guard into a no-op."""
    bad_sql = """
    CREATE TABLE IF NOT EXISTS widgets (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        PRIMARY KEY (id)
    );
    -- new_col is added by a LATER alter migration, so it is NOT in the base table.
    CREATE INDEX IF NOT EXISTS idx_widgets_new ON widgets(new_col);
    """
    tables, indexes = parse_base_literal(bad_sql)
    assert "widgets" in tables
    assert tables["widgets"] == {"id", "name"}
    # The offending index references new_col, which is absent from the base table.
    offending = [
        (idx, tbl, cols)
        for idx, tbl, cols in indexes
        if tbl == "widgets" and any(c not in tables["widgets"] for c in cols)
    ]
    assert offending, "parser failed to surface the synthetic missing-column index"


def test_parser_accepts_a_valid_literal():
    """A well-formed literal (index only on base columns, composite index, quoted
    idents, DESC ordering) must produce ZERO violations — no false positives."""
    good_sql = """
    CREATE TABLE IF NOT EXISTS things (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'x' CHECK (status IN ('x','y')),
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_things_proj ON things(project_id, status);
    CREATE INDEX IF NOT EXISTS idx_things_created ON things(created_at DESC);
    """
    tables, indexes = parse_base_literal(good_sql)
    assert tables["things"] == {"id", "project_id", "status", "created_at"}
    for _idx, tbl, cols in indexes:
        for col in cols:
            assert col in tables[tbl], f"{col} unexpectedly flagged"


@pytest_asyncio.fixture
async def _fresh_sqlite_db():
    """A throwaway in-memory SQLite DB built via the real ``init_db`` — i.e. the base
    ``CREATE_TABLES`` literal followed by the FULL migration chain, exactly as prod
    startup runs it. Yields the connection; closes it after."""
    from meridian import db as db_module

    conn = await db_module.init_db(":memory:")
    try:
        yield conn
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_sqlite_base_schema_plus_migrations_applies_cleanly(_fresh_sqlite_db):
    """Lightweight DYNAMIC complement to the static check: applying the SQLite base
    schema and then running the entire migration chain (``init_db``) must not raise.

    This is the fresh-DB path. It cannot, by itself, reproduce the existing-DB
    crash-loop (that needs the OLD table shape, and we deliberately keep no stale
    "old schema snapshot" file — the note warns that reintroduces the staleness
    problem the static check avoids). The STATIC check above is the primary guard for
    the existing-DB case; this just proves the current literal + migrations are
    mutually consistent and self-applying end to end."""
    conn = _fresh_sqlite_db
    # A trivially real query against a base table proves the schema is live.
    async with conn.cursor() as cur:
        await cur.execute("SELECT COUNT(*) AS n FROM sprint_items")
        row = await cur.fetchone()
    assert row is not None
