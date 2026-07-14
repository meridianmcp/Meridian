export const meta = {
  name: 'wave5-batchH-easy-parallel',
  description: 'Parallel easy items across 6 disjoint files (mixed frontend .ts + backend .py)',
  phases: [{ title: 'Implement' }],
}

const FE_CONV = `
Meridian frontend is TypeScript + Preact (strict tsc, 0 errors). Source: meridian/static/dashboard-*.ts (these auto-match "dashboard.js" in prospecting, but dashboard.js/dashboard.bundle.js are BUILT artifacts — find + edit the real .ts module; a quick grep for the feature's DOM ids/text tells you which module). Commands (repo root): npm run typecheck ; npm run test (vitest).
FRONTEND HARD RULES: edit ONLY your feature's function(s) in the ONE .ts module named. Keep the file's existing CRLF line endings (do NOT reformat the whole file to LF). DO NOT run \`npm run build\` or edit dashboard.js / dashboard.bundle.js / asset-manifest.json (the orchestrator rebuilds the bundle centrally). Add a vitest test in the matching dashboard-<mod>.test.ts (LF is fine for .test.ts).`

const BE_CONV = `
Meridian backend: Python 3.12, psycopg3 (%s not ?; aiosqlite paths call await db.commit()). Schema changes are HIGH-RISK — PREFER a JSON blob / heuristic over a new column. Put tests in tests/test_w5_<slug>.py, unit-level, mock-only.`

const COMMON = `
Never touch .env / meridian.toml / hooks.ps1 / hooks.sh. Never 'git add -A'. Stage ONLY your exact files by path. RE-VERIFY the premise against current code first (report in premise_recheck).
Run tests under a HARD timeout: timeout 120 pixi run python -m pytest <your test file> ...  (backend) OR npm run typecheck && npm run test (frontend). All green before committing.
Commit EXACTLY ONE commit (your files by path), return commit_sha. If genuinely blocked / needs live debugging you can't do from code / too large: DO NOT commit — return status="blocked" + a concrete recipe. Your final message IS the structured result.`

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
    id: '241b0d3b', slug: '241b0d3b', kind: 'fe',
    task: `UX: the dashboard's session summary / active-sessions list ordering is inconsistent — make it consistently MOST-RECENT-FIRST. Find the active-sessions rendering (grep for the sessions list / "active" sessions in meridian/static/dashboard-core.ts, likely, or dashboard.ts). Sort the sessions by last_seen (fallback created_at) descending before render, consistently everywhere that list appears. Add a vitest test asserting the sort helper orders most-recent-first.`,
  },
  {
    id: '78114fa6', slug: '78114fa6', kind: 'fe',
    task: `BUG: clicking "Use new default" on the stale plugin-override warning (word slot: docx-mcp-server -> docx-mcp) leaves the command field BLANK instead of populating "uvx docx-mcp". Find the stale-override warning + its "Use new default" handler in meridian/static/dashboard-plugins.ts. FIX so the handler writes the new default command into the command input (populate, not clear). RE-VERIFY the actual default source. Add a vitest test for the handler's populate behavior. NOTE: dashboard-plugins.ts already has a reference-manager status row (f5e1ed49) + f5e1ed49's test file — build alongside, edit only the stale-override handler.`,
  },
  {
    id: '70ac52e4', slug: '70ac52e4', kind: 'fe',
    task: `UX: the MCP tools dashboard page needs COLLAPSIBLE sections per category — 90+ tools now render as one flat list. Find the MCP-tools rendering in meridian/static/dashboard-mcp.ts. Group the tools by their category and render each category as a collapsible <details> section (match the existing collapsible pattern used elsewhere in the dashboard). Add a vitest test for the grouping/section helper.`,
  },
  {
    id: 'ddde437a', slug: 'ddde437a', kind: 'fe',
    task: `BUG: the Code Intelligence panel fails with a scary "Failed to load code intelligence: Error: HTTP 503". Likely the codebase-memory-mcp tunnel slot isn't connected, so the panel shows a raw 503 instead of a graceful "code index not connected — start the tunnel / connect the code slot" empty state. Find the Code Intelligence panel fetch/error handling (meridian/static/components/CodeIntelPanel.tsx or dashboard.ts). FIX: catch the 503 (and other not-connected states) and render a clear, friendly not-connected message with how to enable it, instead of the raw error. RE-VERIFY where the 503 comes from. Add a vitest test for the graceful-503 rendering. Edit ONLY the code-intel panel's fetch/render.`,
  },
  {
    id: 'b8c79a8a', slug: 'b8c79a8a', kind: 'be_fe',
    task: `BUG: the Timeline tab shows "no activity yet" for meridian-build even though log_task activity exists (it shows in the session list / standup digest for the same project). Something in the Timeline data path filters out the activity. RE-VERIFY: find the timeline endpoint in meridian/server.py (grep "timeline") + the timeline rendering in meridian/static/dashboard-timeline.ts. Diagnose why the timeline query returns empty despite task_log rows (likely a wrong filter — e.g. an event-type/kind filter, a join, or a project-id mismatch). FIX the query/filter so real log_task activity appears. Add a test (backend: tests/test_w5_b8c79a8a.py asserting the timeline query returns seeded task activity; keep it unit/db-level). If it's purely a frontend filter, fix that + a vitest test. If the true cause needs live data you can't reproduce, report a precise diagnosis + recipe.`,
  },
  {
    id: '22c274bd', slug: '22c274bd', kind: 'be',
    task: `GAP: workspace-scoped notes/decisions accumulate PROJECT-specific content (a thesis post-mortem, a project's CI patterns, a personal filesystem path all ended up in workspace-global notes). Workspace notes are meant to be tenant-global/cross-project. RE-VERIFY the add_workspace_note / add_workspace_decision handlers in meridian/mcp/handler.py. FIX: add a lightweight heuristic guard that DETECTS likely-project-specific content (e.g. an absolute filesystem path, a specific project name, a commit sha, "this project"/"the thesis" phrasing) and returns a soft WARNING in the result (not a hard block) suggesting it belongs in a project note instead — so the caller is nudged without losing the write. Pure heuristic, no schema change. Add tests/test_w5_22c274bd.py (project-specific content → warning; genuinely-global content → no warning). Touch ONLY the workspace-note/decision handler region in handler.py.`,
  },
]

function buildPrompt(it) {
  const conv = it.kind === 'be' ? BE_CONV : it.kind === 'fe' ? FE_CONV : (BE_CONV + FE_CONV)
  return `You are a senior engineer implementing ONE Meridian item to production quality in an ISOLATED git worktree (base: dev).

SPRINT ITEM ${it.id}
Task: ${it.task}
${conv}
${COMMON}`
}

phase('Implement')
const results = await parallel(
  ITEMS.map((it) => () =>
    agent(buildPrompt(it), { label: `impl:${it.id}`, phase: 'Implement', isolation: 'worktree', schema: RESULT_SCHEMA })
  )
)
return results.filter(Boolean)
