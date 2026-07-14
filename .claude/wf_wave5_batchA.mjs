export const meta = {
  name: 'wave5-batchA-parallel',
  description: 'Parallel implement/verify 7 disjoint-file batch-1 items in isolated worktrees',
  phases: [{ title: 'Implement' }],
}

const CONVENTIONS = `
Meridian repo conventions (production SaaS — production-quality code only, no stubs/scaffolds):
- Python 3.12, FastAPI, psycopg3 (SQL uses %s NOT ?; the adapter converts ?→%s; literal % in LIKE = %%; autocommit=True, never conn.commit() on PG — but aiosqlite paths DO call await db.commit(), match surrounding code).
- Windows shell is PowerShell; a Bash tool is also available. Test cmd for a single file:
    pixi run python -m pytest <file> -q --timeout=60 -o addopts=""
- .ps1 files MUST stay pure ASCII (PS 5.1 reads BOM-less as cp1252; em-dashes/smart-quotes break the parser). Use -- and straight quotes.
- Schema changes are HIGH-RISK (need SQLite migrations.py + PG pg_adapter.py parity + a test_core migration-count bump; a bad one caused a prod outage). PREFER a JSON blob field over a new column when you need to persist small state.
HARD RULES (never violate):
- NEVER touch .env, meridian.toml, hooks.ps1, hooks.sh (not sprint_guard — those are different).
- NEVER 'git add -A' / 'git add .' / 'git commit -a'. Stage ONLY the exact files you changed, by path.
- No secrets in code/commits.
`

const TESTING = (slug) => `
Put ALL new tests in a NEW dedicated file tests/test_w5_${slug}.py (do NOT append to test_core.py or other shared test files — that would conflict when merged). If you must also touch an existing test file, keep it minimal.
Run: pixi run python -m pytest tests/test_w5_${slug}.py -q --timeout=60 -o addopts=""  (plus any existing test file your change impacts). Every test must pass before you commit.
`

const COMMIT = (id, slug) => `
When (and only when) your implementation is complete AND its tests pass:
  git add <the exact files you changed, by path>
  git commit -m "feat(${id}): <concise summary>"   (or fix(${id}) for a bugfix)
Then capture the commit sha: git rev-parse HEAD
Return that sha as commit_sha. Make EXACTLY ONE commit. Do NOT push. Do NOT merge.
If you conclude the item is genuinely blocked / too large to ship at production quality in a focused effort / a prior deferral still holds after you RE-VERIFY it against current code, then DO NOT commit — return status="blocked" with a specific, current-code-grounded reason and a concrete recipe (files + steps) for a future focused session.
`

const RESULT_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['item_id', 'status', 'summary', 'files_changed', 'test_result'],
  properties: {
    item_id: { type: 'string' },
    status: { type: 'string', enum: ['done', 'blocked'] },
    summary: { type: 'string', description: 'What you shipped, or why blocked.' },
    commit_sha: { type: 'string', description: 'Full sha of your single commit (done only), else "".' },
    files_changed: { type: 'array', items: { type: 'string' } },
    test_result: { type: 'string', description: 'e.g. "6 passed" or the failure.' },
    premise_recheck: { type: 'string', description: 'Result of re-verifying the item premise/any prior defer-note against CURRENT code.' },
    blocker_or_recipe: { type: 'string', description: 'If blocked: specific reason + concrete recipe. Else "".' },
  },
}

const ITEMS = [
  {
    id: '5fb084fe', slug: '5fb084fe_ps1_components',
    files: 'install.ps1',
    task: `install.ps1 needs COMPONENT SELECTION instead of always installing everything: a -Component parameter accepting binary | hooks | both | custom (default: both), with real per-component behavior and a confirmation/summary line of what will be installed. RE-VERIFY the prior defer-note ("best done with the a1ba9aa8 installer consolidation"): a1ba9aa8 is a separate large refactor that is still open — adding a clean, self-contained -Component switch to the CURRENT install.ps1 is useful now and is NOT blocked by it, so implement it now unless you find a concrete current-code reason it can't be done cleanly. Keep the file pure ASCII. If the script has no test harness, add a lightweight ASCII/parse-validation test in tests/test_w5_5fb084fe_ps1_components.py using [System.Management.Automation.Language.Parser]::ParseFile via a subprocess OR a pure-Python assertion that the -Component param + branches exist and the file is ASCII.`,
  },
  {
    id: '736d300e', slug: '736d300e_ruff_blocking',
    files: 'pyproject.toml, .github/workflows/test.yml',
    task: `ruff runs in CI but is non-blocking ("ruff check . || true", comment "violations do not fail CI yet"). Make it BLOCKING without a huge cleanup: RE-VERIFY the current ruff violation count (run: pixi run python -m ruff check . 2>/dev/null | tail; if ruff isn't installed in the default env, note that). Then make a CLEAN rule subset blocking — e.g. configure ruff [tool.ruff.lint] to select the codes that are ALREADY fully clean (at minimum F821/F811/F401-family undefined/redefinition/import errors — the correctness-critical ones) and flip test.yml so THAT scoped check runs WITHOUT "|| true" (blocking), while any noisy style backlog (E501 etc.) stays non-blocking or ignored. The goal: real correctness-linting is now enforced and CI stays green. Verify locally that the scoped ruff check passes. Add tests/test_w5_736d300e_ruff_blocking.py asserting test.yml no longer has "|| true" on the blocking ruff line and pyproject encodes the selected rule set.`,
  },
  {
    id: '4c01841b', slug: '4c01841b_tool_perms',
    files: 'meridian/routes/tunnel.py (list_tunnel_tools)',
    task: `BUG (root cause uncertain): claude.ai's Tool Permissions screen for Meridian tunnel tools misbehaves (e.g. tools not individually listed / not toggleable / duplicated / missing annotations). Investigate list_tunnel_tools in meridian/routes/tunnel.py and how it shapes the tools it returns (names, annotations, uniqueness, slot-prefixing). Identify the concrete defect from the code (e.g. duplicate names collapsing entries, missing 'annotations'/'title', non-unique names across slots). Fix what you can concretely justify from the code and add tests/test_w5_4c01841b_tool_perms.py. If the root cause genuinely can't be determined from server-side code alone (it's a claude.ai UI behavior), do the server-side hardening that is clearly correct (unique, well-annotated tool entries) and report the residual uncertainty specifically.`,
  },
  {
    id: '75d2196d', slug: '75d2196d_docx_citations',
    files: 'meridian/docs_intel.py, meridian/doc_store.py',
    task: `GAP: docx citation-marker extraction doesn't exist — currently citations are only extracted from LaTeX, not from .docx. Add extraction of citation markers from .docx documents in meridian/docs_intel.py (mirror how latex_intel.py surfaces citations), and surface them through meridian/doc_store.py where document structure/citations are exposed. docx citations appear as Zotero/Mendeley field codes (w:instrText containing ADDIN ZOTERO_ITEM / CSL_CITATION JSON, or footnote/endnote references). Parse the docx (python-docx or the existing OOXML parsing already in the repo — check docs_intel.py for existing docx handling) to extract citation markers into a structured list. Add tests/test_w5_75d2196d_docx_citations.py with a small synthetic docx (build one in-test with python-docx or a zipfile of minimal OOXML) proving markers are extracted. If the repo already has partial docx-citation code, extend it; RE-VERIFY the "doesn't exist" premise first.`,
  },
  {
    id: 'a8a54fe9', slug: 'a8a54fe9_tunnel_bundle',
    files: 'meridian/tunnel_plugins.py',
    task: `PRODUCT GAP: the tunnel doesn't BUNDLE plugin tools (docx-mcp, codebase-memory-mcp, meridian-docs, zotero-mcp) — a user has to wire each MCP server themselves. RE-VERIFY current state of BUILTIN_PLUGINS in meridian/tunnel_plugins.py: which slots/servers are already bundled vs missing. Note related open items 9665538a (meridian-docs wiring) and 39c117b1 (zotero) exist — do NOT duplicate their exact scope; this item is the general "bundle the known plugin tools as first-class built-ins" gap. If you can cleanly add one or more missing built-in plugin definitions (command/runtime/slot/description, matching the existing BUILTIN_PLUGINS shape) with tests in tests/test_w5_a8a54fe9_tunnel_bundle.py, do so. If this genuinely overlaps 9665538a/39c117b1 such that doing it here would collide, implement the non-overlapping part and report precisely which parts belong to those items (blocked with recipe for the overlap).`,
  },
  {
    id: '9f7bfcca', slug: '9f7bfcca_trial_emails',
    files: 'meridian/server.py (+ meridian/hosted.py if needed)',
    task: `FEAT: Trial-expiration reminder emails at 14/7/1 days left before a free trial ends. Infra exists (tenants have trial_started_at + inactivity_expires_at; server computes days_remaining for the dashboard banner; hosted.py already sends emails via Resend and has background loops in the server lifespan). Implement a daily reminder pass that, for each tenant whose trial has 14/7/1 days left, sends ONE reminder email per threshold. IDEMPOTENCY IS REQUIRED and must NOT use a new schema column (schema changes are high-risk) — persist which thresholds were sent in an existing JSON field on the tenant (check tenants row for a JSON/settings blob) or a lightweight existing table; if no JSON field exists, RE-VERIFY and either reuse task_log/an existing table or report blocked with the exact schema-free idempotency design. Wire the pass into the existing lifespan background-loop pattern (see _auto_summary_loop / _version_check_loop / _run_session_keepalive_loop in server.py) guarded to hosted mode. Add tests/test_w5_9f7bfcca_trial_emails.py that mocks the email sender and time to prove: correct thresholds fire, each fires once (idempotent), non-trial/expired tenants are skipped. If a schema-free idempotency path genuinely doesn't exist, ship the computation+send+loop with the cleanest available idempotency and clearly note any residual.`,
  },
  {
    id: '427b7902', slug: '427b7902_ci_gate',
    files: '.github/workflows/deploy.yml, meridian/mcp/handler.py (complete_sprint_item)',
    task: `URGENT, two changes that must ship together: (1) AUTO-PROMOTE dev->main on green CI: currently merge-to-main requires an explicit workflow_dispatch promote=yes; a green test.yml alone does not fire the promote (deploy.yml around lines 144-148). Make a green CI on dev automatically fire the dev->main promote (respect the existing exactly-once / anti-recursion design — study deploy.yml's merge-to-main + the GITHUB_TOKEN anti-recursion note before changing; do NOT create an infinite deploy loop). (2) HARD-BLOCK complete_sprint_item on non-green CI: it already computes a ci_verification field but only WARNS. Make complete_sprint_item REFUSE completion (raise, like the EVIDENCE_REQUIRED gate) when CI status for the item's commit is genuinely failing — with an override/escape hatch consistent with existing patterns, and never blocking when CI status is simply unknown/absent (only on a real failing status). Add tests/test_w5_427b7902_ci_gate.py for the complete_sprint_item refusal logic (green/unknown allowed, failing refused, override works). For the deploy.yml half, be conservative and correct; if auto-promote can't be made safe without risking a loop, implement the complete_sprint_item gate (fully tested) and report the deploy.yml design precisely rather than shipping something risky.`,
  },
]

function buildPrompt(it) {
  return `You are a senior engineer implementing ONE Meridian sprint item to production quality, working in an ISOLATED git worktree on branch base dev.

SPRINT ITEM ${it.id}
Target file(s): ${it.files}
Task: ${it.task}

${CONVENTIONS}
${TESTING(it.slug)}
${COMMIT(it.id, it.slug)}

Process (follow in order):
1. RE-VERIFY the premise against CURRENT code before writing anything. Read the target files. If a prior session's note claimed this was done/deferred, cross-check it against what the code actually shows now and state your finding in premise_recheck. Do NOT blindly trust old notes.
2. Implement production-quality code (no stubs). Keep the change tightly scoped to this item's file(s).
3. Add the dedicated test file and run it (plus any impacted existing test file). All green.
4. Commit exactly your files (by path). Capture the sha.
5. Return the structured result. Be honest: if you could not fully finish or it's genuinely blocked, status="blocked" with a concrete reason+recipe and NO commit. Your final message IS the structured result (the schema is enforced).`
}

phase('Implement')
const results = await parallel(
  ITEMS.map((it) => () =>
    agent(buildPrompt(it), {
      label: `impl:${it.id}`,
      phase: 'Implement',
      isolation: 'worktree',
      schema: RESULT_SCHEMA,
    })
  )
)
return results.filter(Boolean)
