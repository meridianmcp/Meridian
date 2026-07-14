export const meta = {
  name: 'wave5-batchG-frontend-parallel',
  description: 'Parallel frontend items via SYMBOL partitioning (disjoint .ts symbols; bundle rebuilt centrally)',
  phases: [{ title: 'Implement' }],
}

// KEY: these items touch DISJOINT symbols/sections (three distinct sections of
// dashboard-settings.ts + a different file), so their source edits cherry-pick
// cleanly — exactly the symbol-level parallelization the maintainer asked for.
// The ONE shared conflict (the built dashboard.js bundle) is avoided: agents do
// NOT run the build; the orchestrator regenerates dashboard.js ONCE after merge.

const CONVENTIONS = `
Meridian frontend conventions (production SaaS — production-quality, no stubs):
- The dashboard is TypeScript + Preact (strict tsc, 0 errors required). Source lives in meridian/static/dashboard-*.ts. dashboard.js is the BUILT BUNDLE (generated) — DO NOT edit it.
- Commands (run from repo root): npm run typecheck (tsc --noEmit) ; npm run test (vitest run). Vitest tests are meridian/static/dashboard-*.test.ts.
HARD RULES:
- Edit ONLY the specific symbol/section named in YOUR task, in the named .ts module. Another agent owns other sections of the same file — do NOT touch them (keeps the merge clean).
- DO NOT run \`npm run build\` and DO NOT edit or stage meridian/static/dashboard.js (the bundle). The orchestrator rebuilds it once, centrally, after merging.
- Never touch .env / meridian.toml / hooks.ps1 / hooks.sh. Never 'git add -A'/'git add .'. Stage ONLY your exact .ts source + .test.ts files, by path.
`

const TESTING = `
Add or extend a vitest test in the matching meridian/static/dashboard-<module>.test.ts (create it if absent) covering your change. Then run:
  npm run typecheck    (MUST be 0 errors — it checks the whole .ts project)
  npm run test         (vitest — your new tests + existing all green)
Do NOT start a dev server or bind a port.
`

const COMMIT = (id) => `
Only when complete AND typecheck + vitest are green: git add <your exact .ts source + .test.ts by path>; git commit -m "feat(${id}): <summary>" (or fix); git rev-parse HEAD → commit_sha. EXACTLY ONE commit. Do NOT push/merge/build.
If genuinely blocked / too large / a prior deferral still holds after RE-VERIFY vs current code: DO NOT commit — return status="blocked" with a specific reason + recipe.
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
    id: '2ff2ff1f', slug: '2ff2ff1f',
    files: 'meridian/static/dashboard-settings.ts (the ws-handoff-template textarea ONLY)',
    task: `UX: the Handoff Format textarea (id ws-handoff-template) defaults to rows="6" — too small for its own use case (a full custom template using all 7 placeholders). RE-VERIFY in meridian/static/dashboard-settings.ts (confirmed rows="6"). FIX: give that ONE textarea a much larger default height (e.g. rows="16") and, if trivial, make it vertically resizable (style resize:vertical). Touch ONLY the ws-handoff-template textarea element — nothing else in the file.`,
  },
  {
    id: 'f2157803', slug: 'f2157803',
    files: 'meridian/static/dashboard-settings.ts (the context_threshold / max_turns slider inputs ONLY)',
    task: `UX: the project-settings sliders (context_threshold "checkpoint after N turns", max_turns) are poor UX — a slider hides the exact value and is fiddly. RE-VERIFY the slider inputs in meridian/static/dashboard-settings.ts. FIX: replace those range sliders with number inputs (type="number" with sensible min/max/step) OR a number input paired with the slider showing the live value — so the exact value is always visible and directly editable. Preserve the existing state binding / save wiring (same setting keys). Touch ONLY those slider input elements + their change handlers — not other settings sections.`,
  },
  {
    id: 'ca8c0d56', slug: 'ca8c0d56',
    files: 'meridian/static/dashboard-settings.ts (the Claude Code + Codex CLI setup sections ONLY)',
    task: `UX: the Claude Code and Codex CLI setup sections in meridian/static/dashboard-settings.ts are unnecessarily verbose — too much prose. RE-VERIFY those two sections. FIX: condense them to the essential copy-paste config + a one-line explanation each (keep the actual MCP config snippets intact and correct; trim the surrounding prose / redundant steps). Optionally make them collapsible if that pattern already exists in the file. Touch ONLY those two setup sections — not other parts of settings.`,
  },
  {
    id: 'f5e1ed49', slug: 'f5e1ed49',
    files: 'meridian/static/dashboard-plugins.ts (a reference-manager / Zotero status row)',
    task: `FEAT: Dashboard status indicator for the research/reference-manager backend (Zotero). RE-VERIFY — this was previously deferred as "can't probe local Zotero from prod", but that is NOW MOOT: item 39c117b1 just wired zotero-mcp as a first-class 'zotero' TUNNEL SLOT (SLOT_DISPLAY_NAMES 'zotero'→'zotero-mcp', /tunnel/status reports zotero_active). So the reference manager can now show a connected/not-detected status EXACTLY like the other tunnel slots (fs/code/word/docs) do in dashboard-plugins.ts. FIX: add a reference-manager (zotero) status row in meridian/static/dashboard-plugins.ts that reflects the zotero slot's tunnel active/health status from the /tunnel/status payload (zotero_active), mirroring how the existing slot rows render their status. Touch ONLY the plugins-status rendering in dashboard-plugins.ts. Add/extend a vitest test for the new row's status mapping.`,
  },
]

function buildPrompt(it) {
  return `You are a senior frontend engineer implementing ONE Meridian dashboard item to production quality in an ISOLATED git worktree (base: dev). The dashboard is TypeScript + Preact.

SPRINT ITEM ${it.id}
Target (edit ONLY this): ${it.files}
Task: ${it.task}

${CONVENTIONS}
${TESTING}
${COMMIT(it.id)}

Process: (1) RE-VERIFY the premise against CURRENT code (read the target .ts module; report in premise_recheck). (2) Implement production-quality, tightly-scoped code touching ONLY your named symbol/section. (3) Add the vitest test; run npm run typecheck + npm run test — all green. (4) Commit ONLY your .ts source + .test.ts (NOT dashboard.js). (5) Return the structured result honestly. Your final message IS the structured result.`
}

phase('Implement')
const results = await parallel(
  ITEMS.map((it) => () =>
    agent(buildPrompt(it), { label: `fe:${it.id}`, phase: 'Implement', isolation: 'worktree', schema: RESULT_SCHEMA })
  )
)
return results.filter(Boolean)
