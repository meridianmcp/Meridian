export const meta = {
  name: 'wave5-batchC-parallel',
  description: 'Parallel implement/verify 3 disjoint-file backend items in isolated worktrees',
  phases: [{ title: 'Implement' }],
}

const CONVENTIONS = `
Meridian repo conventions (production SaaS — production-quality code only, no stubs):
- Python 3.12, FastAPI, psycopg3 (SQL uses %s NOT ?; literal % in LIKE = %%; autocommit — but aiosqlite paths call await db.commit(), match surrounding code).
- Schema changes are HIGH-RISK (SQLite migrations.py + PG pg_adapter.py parity + test_core count bump; a bad one caused a prod outage). PREFER a JSON blob over a new column.
HARD RULES: never touch .env / meridian.toml / hooks.ps1 / hooks.sh. Never 'git add -A'/'git add .'/'git commit -a' — stage ONLY the exact files you changed, by path. No secrets in code.
`

// ANTI-STALL: run the dedicated test file under a HARD outer timeout so a hanging
// test self-aborts instead of blocking forever (a prior agent hung on a heavy
// test). NEVER write a test that starts a real server, binds a port, sleeps, or
// makes a network call — unit-level with mocks only.
const TESTING = (slug) => `
Put ALL new tests in a NEW file tests/test_w5_${slug}.py (do NOT append to shared test files). Unit-level with mocks ONLY — no real servers/ports/network/sleeps.
Run with a HARD timeout so nothing can hang: timeout 120 pixi run python -m pytest tests/test_w5_${slug}.py -q -p no:cacheprovider --timeout=30
If it times out or a test tries to hit the network, FIX the test (mock it) — do not leave a hanging test. All green before committing.
`

const COMMIT = (id, slug) => `
Only when complete AND tests pass: git add <exact files by path>; git commit -m "feat(${id}): <summary>"; then git rev-parse HEAD → return as commit_sha. EXACTLY ONE commit. Do NOT push/merge.
If genuinely blocked / too large for a focused effort / a prior deferral still holds after you RE-VERIFY it against current code: DO NOT commit — return status="blocked" with a specific current-code reason + a concrete recipe.
`

const RESULT_SCHEMA = {
  type: 'object', additionalProperties: false,
  required: ['item_id', 'status', 'summary', 'files_changed', 'test_result'],
  properties: {
    item_id: { type: 'string' },
    status: { type: 'string', enum: ['done', 'blocked'] },
    summary: { type: 'string' },
    commit_sha: { type: 'string' },
    files_changed: { type: 'array', items: { type: 'string' } },
    test_result: { type: 'string' },
    premise_recheck: { type: 'string' },
    blocker_or_recipe: { type: 'string' },
  },
}

const ITEMS = [
  {
    id: 'badc7b34', slug: 'badc7b34_pretooluse_hook',
    files: 'meridian/mcp/handler.py (the fd726a05 vendor-neutral PreToolUse hook path)',
    task: `Extend the existing fd726a05 vendor-neutral PreToolUse hook (currently it only covers/redirects a narrow set) to cover more of the intended cases. RE-VERIFY current behavior: find the fd726a05 PreToolUse hook handling in meridian/mcp/handler.py (and/or meridian/routes/hooks.py) and read exactly what tools/conditions it currently intercepts and what the item wants extended (the truncated title says "extend ... (currently only ...)"). Implement the extension conservatively and test it. If the exact intended extension is ambiguous from code, implement the clearly-correct superset and note the interpretation. Tests in tests/test_w5_badc7b34_pretooluse_hook.py.`,
  },
  {
    id: '9665538a', slug: '9665538a_meridian_docs_slot',
    files: 'meridian/tunnel_plugins.py, meridian/routes/tunnel.py',
    task: `HIGH: meridian-docs (the extracted fast doc parser) is NOT wired into tunnel_plugins.py as a bundled tunnel slot. Add a BUILTIN_PLUGINS entry for meridian-docs (slot name, command/runtime to launch the meridian-docs MCP server, description, matching the shape of the existing docx/word/ppt entries), and add any per-slot routing needed in meridian/routes/tunnel.py IF the office-style slots use explicit per-slot routes (check how the 'word'/'ppt' slots are routed — custom plugins use the generic route at ~line 548; a first-class slot may need an explicit route). Mirror the existing docx-mcp/word slot wiring precisely. Tests in tests/test_w5_9665538a_meridian_docs_slot.py asserting the slot is registered in BUILTIN_PLUGINS with correct fields and resolves. RE-VERIFY it isn't already wired first.`,
  },
  {
    id: '967bb99b', slug: '967bb99b_toc_regen',
    files: 'packages/docparse/docparse/docs_intel.py (+ meridian/doc_store.py if surfaced there)',
    task: `HIGH: neither docx-mcp nor meridian-docs regenerates TOC / LOF (list of figures) / SEQ fields in a .docx — confirm the exact gap and implement docx-side support for detecting and (re)generating these field-driven structures, or at minimum extracting/representing them so downstream can regenerate. RE-VERIFY against current packages/docparse/docparse/docs_intel.py (note: docx citation extraction was just added there — build alongside it, do not conflict). OOXML: TOC/LOF/SEQ live as field codes (w:instrText with TOC \\o, SEQ Figure, etc.) plus cached results. Implement detection + a structured representation of these fields (and regeneration where feasible without a full Word layout engine — a real Word render is out of scope; be honest about that boundary). Tests in tests/test_w5_967bb99b_toc_regen.py with synthetic OOXML. If genuine regeneration requires a Word layout engine (out of scope), ship robust DETECTION + structured extraction + a clear boundary note rather than faking regeneration.`,
  },
]

function buildPrompt(it) {
  return `You are a senior engineer implementing ONE Meridian sprint item to production quality in an ISOLATED git worktree (base: dev).

SPRINT ITEM ${it.id}
Target file(s): ${it.files}
Task: ${it.task}

${CONVENTIONS}
${TESTING(it.slug)}
${COMMIT(it.id, it.slug)}

Process: (1) RE-VERIFY the premise against CURRENT code — read the target files; report findings in premise_recheck; don't trust stale notes. (2) Implement production-quality, tightly-scoped code. (3) Add + run the dedicated test file under the hard timeout; all green. (4) Commit exactly your files. (5) Return the structured result honestly (blocked with recipe if you couldn't finish). Your final message IS the structured result.`
}

phase('Implement')
const results = await parallel(
  ITEMS.map((it) => () =>
    agent(buildPrompt(it), { label: `impl:${it.id}`, phase: 'Implement', isolation: 'worktree', schema: RESULT_SCHEMA })
  )
)
return results.filter(Boolean)
