export const meta = {
  name: 'wave5-schema-sprint-columns',
  description: 'Add priority + blocker_kind columns to sprint_items (e08fee30 pt1 + 2282a636), full double migration path',
  phases: [{ title: 'Implement' }],
}

const RESULT_SCHEMA = {
  type: 'object', additionalProperties: false,
  required: ['item_id', 'status', 'summary', 'files_changed', 'test_result'],
  properties: {
    item_id: { type: 'string' }, status: { type: 'string', enum: ['done', 'blocked'] },
    summary: { type: 'string' }, commit_sha: { type: 'string' },
    files_changed: { type: 'array', items: { type: 'string' } }, test_result: { type: 'string' },
    premise_recheck: { type: 'string' }, blocker_or_recipe: { type: 'string' },
  },
}

const TASK = `Add TWO new columns to the sprint_items table, shipping two sprint items together (they're both simple sprint_items TEXT columns):
- e08fee30 (PART 1 ONLY): "priority" TEXT NOT NULL DEFAULT 'normal', app-layer enum {urgent, high, normal, low}. claim_sprint_item / get_sprint_items / get_parallelizable_groups must RESPECT it (order urgent-first within their existing ordering, so higher-priority pending items are claimed/grouped first). PART 2 of e08fee30 (a true RUNNING-session interrupt/preemption mechanism) is OUT OF SCOPE — the item itself says ship part 1 first and design part 2 deliberately; note it deferred.
- 2282a636: "blocker_kind" TEXT (nullable; NULL = ordinary, 'manual' = blocked on a real-world action outside Meridian — publish something, get an API key, talk to an advisor). It is DISTINCT from milestone_type='human' (which is about WHO executes; blocker_kind is about the BLOCKER KIND). A manual-pending item should be surfaced distinctly and NOT treated as an executor-doable "just claim it" pending (mirror how milestone_type='human' items are excluded from executor scoping in get_sprint_items/next-item logic).

CRITICAL — schema changes are the highest-risk change in this repo (a bad migration caused a 2026-07-04 prod outage). Do the FULL double path, exactly:
1. SQLite migration (meridian/db/migrations.py): add ONE new async _migrate_* function that calls _migrate_add_column_if_missing(db, "sprint_items", "priority", "TEXT NOT NULL DEFAULT 'normal'") and _migrate_add_column_if_missing(db, "sprint_items", "blocker_kind", "TEXT"). Mirror the existing _migrate_v25_feedback_and_notifications pattern. REGISTER it in the init_db migration runner in meridian/db/__init__.py (grep "_migrate_" await calls near line ~856 — add yours in sequence). Also add it to the module __all__ if migrations are exported there.
2. Base schema literal (meridian/db/__init__.py, CREATE TABLE sprint_items ~line 245): add "priority TEXT NOT NULL DEFAULT 'normal'," and "blocker_kind TEXT," as new columns. ABSOLUTELY DO NOT add any inline CREATE INDEX on the new columns in the base CREATE_TABLES literal (that crashes existing-DB startup — the exact 2026-07-04 outage). A plain ADD COLUMN is fine.
3. PostgreSQL parity (meridian/pg_adapter.py): find the PG migration list (grep _PG_MIGRATIONS / sprint_items ALTER) and add the equivalent ADD COLUMN IF NOT EXISTS for both columns, matching how the other sprint_items columns are handled there.
4. add_sprint_item + update_sprint_item (meridian/db/__init__.py): accept optional priority=/blocker_kind= params, validate the enum (raise ValueError on bad value like milestone_type does), include them in the INSERT/UPDATE column lists.
5. MCP dispatch (meridian/mcp/handler.py): pass priority/blocker_kind through the add_sprint_item / update_sprint_item handlers.
6. Tool schemas (meridian/mcp_tools.py): add priority + blocker_kind to the add_sprint_item and update_sprint_item input schemas (with the enum + description).
7. get_sprint_items + get_parallelizable_groups: ORDER BY priority (urgent first) alongside existing ordering; surface blocker_kind in the returned rows.
8. TEST COUNT: grep the tests for a migration-COUNT assertion (something like "assert ... == <N>" counting migrations/columns, likely in tests/test_core.py) and BUMP it to match the new migration. If none exists, skip.

Conventions: psycopg3 uses %s not ? (adapter converts); aiosqlite paths call await db.commit(). Tests that monkeypatch sys.platform must not touch Windows-only stdlib attrs (breaks Linux CI). Add tests/test_w5_schema_sprint_cols.py: priority persists + defaults 'normal' + bad value raises; blocker_kind persists + 'manual' distinct; get_sprint_items orders urgent-first; a manual-blocker item is excluded from / distinct in executor scoping. Run: pixi run test (FULL suite — a migration bug shows up as many failures) OR at minimum: pixi run python -m pytest tests/test_core.py tests/test_w5_schema_sprint_cols.py -q --timeout=60 -o addopts="".

Commit ONE commit (feat: sprint_items priority + blocker_kind columns) staging exactly the files you changed by path; return commit_sha + a clear list of every file changed. If any part is genuinely unsafe/unclear, do the safe subset and return status="blocked" with specifics rather than risk a broken migration. Your final message IS the structured result.`

phase('Implement')
const r = await agent(
  `You are a senior backend engineer making a careful, production-grade SCHEMA change to Meridian in an ISOLATED git worktree (base: dev). ${TASK}`,
  { label: 'schema:priority+blocker_kind', phase: 'Implement', isolation: 'worktree', schema: RESULT_SCHEMA }
)
return r
