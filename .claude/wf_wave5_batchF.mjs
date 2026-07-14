export const meta = {
  name: 'wave5-batchF-parallel',
  description: 'Parallel implement/verify 3 remaining disjoint backend items in worktrees',
  phases: [{ title: 'Implement' }],
}

const CONVENTIONS = `
Meridian repo conventions (production SaaS — production-quality code only, no stubs):
- Python 3.12, FastAPI, psycopg3 (SQL %s NOT ?; autocommit — aiosqlite paths call await db.commit(), match surrounding code).
- .ps1 files MUST stay pure ASCII (PS 5.1 reads BOM-less as cp1252; em-dashes/smart-quotes break the parser). Use -- and straight quotes; validate via [System.Management.Automation.Language.Parser]::ParseFile (0 errors).
- Schema changes are HIGH-RISK (SQLite migrations.py + PG pg_adapter.py parity + test_core count bump). PREFER a JSON blob over a new column.
HARD RULES: never touch .env / meridian.toml / hooks.ps1 / hooks.sh. Never 'git add -A'/'git add .'/'git commit -a' — stage ONLY the exact files you changed, by path. No secrets.
`

const TESTING = (slug) => `
Put ALL new tests in a NEW file tests/test_w5_${slug}.py (never append to shared test files). Unit-level with mocks ONLY — NO real servers/ports/network/sleeps.
Run under a HARD timeout: timeout 120 pixi run python -m pytest tests/test_w5_${slug}.py -q -p no:cacheprovider --timeout=30
All green before committing.
`

const COMMIT = (id, slug) => `
Only when complete AND tests pass: git add <exact files by path>; git commit -m "fix(${id}): <summary>" (or feat); git rev-parse HEAD → return as commit_sha. EXACTLY ONE commit. Do NOT push/merge.
If genuinely blocked / too large / too risky to do safely without live testing (esp. the live install path) / a prior deferral still holds after RE-VERIFY: DO NOT commit — return status="blocked" with a specific current-code reason + concrete recipe.
`

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

const ITEMS = [
  {
    id: '54ddd609', slug: '54ddd609_health_push',
    files: 'meridian/tunnel_client.py (_report_slot_health) + meridian/routes/tunnel.py (server receive)',
    task: `BUG: slot-health RECOVERY never reaches an already-connected claude.ai session. When a slot recovers, tunnel_client.py's _report_slot_health sends a plugin_status message and routes/tunnel.py updates server state — but nothing pushes a fresh tools/list to a session that already cached the old (failed/empty) tool list, so the recovered tools stay invisible until reconnect. RE-VERIFY both surfaces: client _report_slot_health (sends the plugin_status) and the server handler in routes/tunnel.py that receives plugin_status. FIX (server-side): on receiving a RECOVERY plugin_status (unhealthy→healthy transition), trigger an MCP "notifications/tools/list_changed" to connected sessions for that tenant (the standard MCP mechanism for "the tool set changed") — so a live session re-queries tools/list and sees the recovered slot's tools. If the server has no push channel to a connected MCP session, do the smallest correct thing (e.g. invalidate the cached tool list so the next call re-queries) and note the constraint. Keep the client side minimal (it already reports). Add tests/test_w5_54ddd609_health_push.py (a recovery plugin_status triggers the list_changed notification / cache invalidation; a non-recovery status does not). NOTE: c8e6b61c just edited SlotProxy in tunnel_client.py (your worktree has it) — _report_slot_health is a different function; build alongside.`,
  },
  {
    id: '0fba4cb6', slug: '0fba4cb6_model_efficiency',
    files: 'meridian/mcp/handler.py (a new MCP tool) + meridian/mcp_tools.py (its schema)',
    task: `FEAT (opt-in): model-efficiency analysis as a TOOL, not a hosted model. Adam's refined ask: offer classification as a tool with (at least) the MECHANICAL mode — zero-token, rule/heuristic classification of a task's likely model tier from signals already available (file count, touches_resources shape/count, title keyword signals like "refactor"/"typo"/"migration", sprint item size) — mirroring how the ultracode orchestration script spends zero model tokens. Implement a new MCP tool (e.g. analyze_model_efficiency or classify_task_tier) in meridian/mcp/handler.py + its schema in meridian/mcp_tools.py that takes a task/sprint-item descriptor and returns a deterministic tier suggestion (e.g. {tier: "haiku"|"sonnet"|"opus", signals: [...], rationale}) with NO model call. Keep it a pure, testable classifier. (A second LLM-backed "semantic" mode is out of scope for this pass — note it as a follow-up.) Add tests/test_w5_0fba4cb6_model_efficiency.py (a trivial "fix typo in README" → cheap tier; a "refactor auth across 12 files + migration" → expensive tier; deterministic, zero-token). Only touch the new tool's function region in handler.py + its mcp_tools.py schema entry.`,
  },
  {
    id: 'a1ba9aa8', slug: 'a1ba9aa8_installer_consolidate',
    files: 'install.ps1, scripts/hooks_install.ps1',
    task: `FEAT/REFACTOR: consolidate the overlapping installer scripts into ONE real client-connector entry point. RE-VERIFY current state: install.ps1 (now has the 5fb084fe -Component binary|hooks|both|custom switch) and scripts/hooks_install.ps1 (the hooks-only installer) overlap. Consolidate so install.ps1 is the single entry point and the hooks logic is a component of it (the -Component hooks path), with scripts/hooks_install.ps1 either removed or reduced to a thin shim that calls install.ps1 -Component hooks (keep backward compat for anyone curl-ing the old path). CRITICAL: this is the LIVE irm|iex install path — do NOT break it. Keep the exact working install flow; the consolidation must be behavior-preserving for the default (-Component both) case. Pure ASCII; validate with Parser::ParseFile (0 errors). Add tests/test_w5_a1ba9aa8_installer_consolidate.py (both scripts parse clean as ASCII; install.ps1 exposes the unified -Component path incl. hooks; the old hooks_install.ps1 path still resolves to the hooks install, whether shim or merged). If a safe behavior-preserving consolidation genuinely can't be verified without a live install run, do the low-risk part (make hooks_install.ps1 a thin shim to install.ps1 -Component hooks) and BLOCK the rest with a recipe.`,
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

Process: (1) RE-VERIFY the premise against CURRENT code (read the target files; report in premise_recheck). (2) Implement production-quality, tightly-scoped code touching ONLY the stated file(s). (3) Add + run the dedicated test file under the hard timeout; all green. (4) Commit exactly your files. (5) Return the structured result honestly (blocked + recipe if you couldn't finish). Your final message IS the structured result.`
}

phase('Implement')
const results = await parallel(
  ITEMS.map((it) => () =>
    agent(buildPrompt(it), { label: `impl:${it.id}`, phase: 'Implement', isolation: 'worktree', schema: RESULT_SCHEMA })
  )
)
return results.filter(Boolean)
