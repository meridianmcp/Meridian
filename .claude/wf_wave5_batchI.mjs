export const meta = {
  name: 'wave5-batchI-easy-parallel',
  description: 'Parallel: 3 remaining easy items (Live collapsible, blog editable, prospect refinement)',
  phases: [{ title: 'Implement' }],
}

const FE = `Frontend is TS+Preact (strict tsc 0 errors). Source: meridian/static/dashboard-*.ts (dashboard.js/dashboard.bundle.js are BUILT — never edit; grep the feature's DOM ids/text to find the real .ts module). Commands (repo root): npm run typecheck ; npm run test (vitest). PRESERVE the file's existing CRLF line endings (don't reformat to LF). DO NOT run npm run build or edit dashboard.js/dashboard.bundle.js/asset-manifest.json (orchestrator rebuilds centrally). Edit ONLY your feature's function(s). Add a vitest test in the matching dashboard-<mod>.test.ts.`
const BE = `Backend: Python 3.12, psycopg3 (%s not ?; aiosqlite paths call await db.commit()). Schema changes HIGH-RISK — prefer JSON blob/heuristic. Tests in tests/test_w5_<slug>.py, unit/mock-only.`
const COMMON = `Never touch .env/meridian.toml/hooks.ps1/hooks.sh. Never 'git add -A' — stage exact files by path. RE-VERIFY premise vs current code (report in premise_recheck). Run tests under a HARD timeout (frontend: npm run typecheck && npm run test; backend: timeout 120 pixi run python -m pytest <file> --timeout=30). One commit, return commit_sha; if genuinely blocked/large/needs-live-debug: status="blocked" + recipe. Your final message IS the structured result.`

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
    id: '6153bc29', slug: '6153bc29', conv: FE,
    task: `UX: the "Live" active-sessions section (and possibly other version/release views) isn't collapsible, unlike most dashboard sections which use the existing collapsible <details>/toggle pattern. Find the "Live" active-sessions section render (grep "Live" / renderLiveSessions in meridian/static/dashboard.ts). Wrap it in the SAME collapsible pattern the other sections use (a <details> or the dashboard's existing collapse toggle), default-open. Edit ONLY the Live section render. Add a vitest test for the collapsible wrapper.`,
  },
  {
    id: 'e553fa7a', slug: 'e553fa7a', conv: FE + '\n' + BE,
    task: `BUG: Blog tab drafts aren't editable — once saved as a draft there's no UI to edit the title/body. RE-VERIFY the Blog tab render + save flow in meridian/static/dashboard.ts (grep "blog") and the blog handler in meridian/mcp/handler.py (save_blog_post / get_blog_posts). FIX: add an "Edit" affordance on each draft that repopulates the blog editor form with the draft's title/body so it can be re-saved (update in place), using the existing save_blog_post path (it should upsert by id — verify; if it only inserts, make the handler update when an id/slug is supplied, no schema change). Add tests (vitest for the edit-populates-form UI; a backend tests/test_w5_e553fa7a.py if you touch save_blog_post's upsert). Edit only the blog render/edit + the blog handler.`,
  },
  {
    id: '691f4e1c', slug: '691f4e1c', conv: BE,
    task: `FEAT (refinement of 926bf221/a8550238): at add_sprint_item / update_sprint_item time, auto-prospecting currently only populates touches_resources + a code_context HINT listing which search_graph/find_symbol calls the caller SHOULD run — it never enriches further. RE-VERIFY the add/update prospecting path in meridian/mcp/handler.py (_prospecting_result) + meridian/handoff.py. This item is distinct from 182468a6 (which enriches at generate_handoff time). Implement the refinement the item asks for — read its FULL live notes to confirm the exact ask (likely: also surface the inferred symbols / a richer code_context at add-time, OR make the inferred touches_resources higher quality). Keep it tunnel-independent (server can't reach the code index directly — 04a15d3f). If the real ask genuinely requires calling the tunnel/code-index (which add_sprint_item can't from a planning chat), implement the best tunnel-free enrichment and note the boundary. tests/test_w5_691f4e1c.py. Touch ONLY the prospecting helper region in handler.py + handoff.py.`,
  },
]

function buildPrompt(it) {
  return `You are a senior engineer implementing ONE Meridian item to production quality in an ISOLATED git worktree (base: dev).

SPRINT ITEM ${it.id}
Task: ${it.task}
${it.conv}
${COMMON}`
}

phase('Implement')
const results = await parallel(
  ITEMS.map((it) => () =>
    agent(buildPrompt(it), { label: `impl:${it.id}`, phase: 'Implement', isolation: 'worktree', schema: RESULT_SCHEMA })
  )
)
return results.filter(Boolean)
