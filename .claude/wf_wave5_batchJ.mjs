export const meta = {
  name: 'wave5-batchJ-ui-refactors',
  description: 'Parallel: 3 big UI refactors on disjoint frontend files',
  phases: [{ title: 'Implement' }],
}

const FE = `Frontend is TS+Preact (strict tsc 0 errors). Source: meridian/static/dashboard-*.ts (dashboard.js/dashboard.bundle.js are BUILT — never edit). Commands (repo root): npm run typecheck ; npm run test (vitest). PRESERVE the file's existing CRLF line endings (do NOT reformat to LF — it makes the diff whole-file). DO NOT run npm run build or edit dashboard.js/dashboard.bundle.js/asset-manifest.json (orchestrator rebuilds centrally). Add vitest tests in a matching dashboard-<mod>.test.ts.`
const COMMON = `Never touch .env/meridian.toml/hooks.ps1/hooks.sh. Never 'git add -A' — stage exact files by path. RE-VERIFY the premise vs current code (report in premise_recheck). Run npm run typecheck && npm run test — all green before committing. One commit, return commit_sha; if genuinely too large to finish well / blocked: status="blocked" with what you DID finish + a precise recipe for the rest. Your final message IS the structured result. Prioritize CORRECTNESS + not breaking existing behavior over completeness — this is production UI.`

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
    id: '00a1e56a', slug: '00a1e56a',
    task: `BUG/REFACTOR: meridian/static/dashboard-settings.ts is ~4855 lines and loadSettingsTab alone is ~464 lines — hard to maintain. RE-VERIFY the current structure. Refactor loadSettingsTab (and the biggest inline render blocks) by EXTRACTING cohesive sections into small, named, exported (window-registered, matching the file's existing global-script pattern) helper functions — e.g. one per settings CARD (account, notifications, executor-config, workspace, connect-clients, etc.). This must be STRICTLY BEHAVIOR-PRESERVING: same DOM ids, same event wiring, same save/load flow — you are only moving code into helpers and calling them, not changing behavior. Keep it CRLF. Add vitest tests asserting the extracted helpers produce the same key DOM ids / structure they did inline (a few representative sections). Do NOT change any other file. If the full file is too large to safely refactor in one pass, extract the LARGEST 4-6 sections cleanly (biggest maintainability win), verify behavior-preserving, and note which remain.`,
  },
  {
    id: '2d3b8424', slug: '2d3b8424',
    task: `FEAT: the dashboard has ~17 flat top-level vtabs — too chaotic. The Goal tab already uses the right pattern: data-gtab subtabs nested under one tab. EXTEND that mechanism to GROUP the flat top-level tabs into a few logical groups (e.g. Planning / Work / Content / Execution) with nested subtabs, so the top bar shows ~4-5 groups instead of 17 flat tabs. RE-VERIFY the current vtab system + the Goal tab's data-gtab implementation in meridian/static/dashboard.ts. Implement the grouping reusing the data-gtab pattern (don't invent a new mechanism); every existing tab must remain reachable (no tab dropped) and its content unchanged — this is pure IA/navigation grouping. Keep CRLF. Add vitest tests for the tab-grouping structure (each group contains its expected tabs; all original tabs still present). Touch ONLY the tab-bar / tab-routing structure in dashboard.ts. If a full 4-group reorg is too large to finish safely, implement the grouping mechanism + wire the clearest 2-3 groups, keep the rest flat, and note the remainder.`,
  },
  {
    id: 'd6b7da48', slug: 'd6b7da48',
    task: `FEAT: dashboard project list renders flat/ungrouped. Add "folders/spheres" — pure UI grouping across otherwise-unrelated top-level projects (e.g. group meridian-build + camerer_ms_graduation_2026 under a visual folder). RE-VERIFY: the project-list rendering in meridian/static/dashboard.ts; note the DB already has project.status (active|parked|archived) + project.priority but NO folder grouping. Implement CLIENT-SIDE folder grouping: let the user assign projects to a named folder (persist the folder assignment in localStorage — NO backend/schema change) and render the project list grouped by folder (collapsible), with an "ungrouped" catch-all. Keep it simple + behavior-preserving for existing project nav. Keep CRLF. Add vitest tests for the grouping helper (assign → grouped render; ungrouped catch-all; localStorage round-trip). Touch ONLY the project-list rendering region in dashboard.ts. If too large, ship the folder-assign + grouped-render core and note any polish left.`,
  },
]

function buildPrompt(it) {
  return `You are a senior frontend engineer implementing ONE Meridian dashboard item to production quality in an ISOLATED git worktree (base: dev). TypeScript + Preact.

SPRINT ITEM ${it.id}
Task: ${it.task}
${FE}
${COMMON}`
}

phase('Implement')
const results = await parallel(
  ITEMS.map((it) => () =>
    agent(buildPrompt(it), { label: `ui:${it.id}`, phase: 'Implement', isolation: 'worktree', schema: RESULT_SCHEMA })
  )
)
return results.filter(Boolean)
