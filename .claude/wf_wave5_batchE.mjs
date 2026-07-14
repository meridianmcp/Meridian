export const meta = {
  name: 'wave5-batchE-parallel',
  description: 'Parallel implement/verify 4 backend items (3 new bugs + trial-emails retry) in worktrees',
  phases: [{ title: 'Implement' }],
}

const CONVENTIONS = `
Meridian repo conventions (production SaaS — production-quality code only, no stubs):
- Python 3.12, FastAPI, psycopg3 (SQL %s NOT ?; autocommit — aiosqlite paths call await db.commit(), match surrounding code).
- Windows: the tunnel client spawns local MCP servers via Popen; the ProactorEventLoop is avoided (SelectorEventLoop). Be careful with cmd/c child-process lifetime on Windows.
- Schema changes are HIGH-RISK (SQLite migrations.py + PG pg_adapter.py parity + test_core count bump; a bad one caused a prod outage). PREFER a JSON blob over a new column.
HARD RULES: never touch .env / meridian.toml / hooks.ps1 / hooks.sh. Never 'git add -A'/'git add .'/'git commit -a' — stage ONLY the exact files you changed, by path. No secrets.
`

const TESTING = (slug) => `
Put ALL new tests in a NEW file tests/test_w5_${slug}.py (never append to shared test files). Unit-level with mocks ONLY — NO real servers/ports/network/sleeps.
Run under a HARD timeout: timeout 120 pixi run python -m pytest tests/test_w5_${slug}.py -q -p no:cacheprovider --timeout=30
If it times out or reaches the network, FIX the test (mock it). All green before committing.
`

const COMMIT = (id, slug) => `
Only when complete AND tests pass: git add <exact files by path>; git commit -m "fix(${id}): <summary>"; git rev-parse HEAD → return as commit_sha. EXACTLY ONE commit. Do NOT push/merge.
If genuinely blocked (e.g. the fix requires the PyInstaller build pipeline, not Python source) / too large / a prior deferral still holds after RE-VERIFY: DO NOT commit — return status="blocked" with a specific current-code reason + concrete recipe.
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
    id: '6b31f481', slug: '6b31f481_serena_prefix',
    files: 'meridian/routes/tunnel.py (list_tunnel_tools / title-prefixing helper)',
    task: `BUG (follow-up to 4c01841b, which is closed): Serena / code-extractor tools ("Find Declaration", "Find Symbol", "Read Memory") show with ZERO connector prefix in claude.ai's Tool Permissions, sitting unlabeled — unlike filesystem's, which are namespaced. RE-VERIFY in meridian/routes/tunnel.py's list_tunnel_tools + the title-prefixing/namespacing helper (the same code 4c01841b touched, and SLOT_DISPLAY_NAMES). Hypothesis to confirm: the prefix helper has a hardcoded set of known connector/source names that is MISSING 'serena' / 'code-extractor' (the extract slot), so those tools fall through unprefixed; the ppt slot's tools may also not register a source. FIX: ensure every slot's tools (esp. the extract/serena slot) get a namespaced title/source via SLOT_DISPLAY_NAMES or the prefix map — no slot left unlabeled. Add tests/test_w5_6b31f481_serena_prefix.py (a serena/extract tool gets a namespaced title, mirroring the 4c01841b filesystem test). NOTE: 39c117b1 just added a 'zotero' slot to SLOT_DISPLAY_NAMES in this file (your worktree has it) — build alongside, don't remove.`,
  },
  {
    id: 'c8e6b61c', slug: 'c8e6b61c_dc_respawn',
    files: 'meridian/tunnel_client.py (SlotProxy.is_running / liveness)',
    task: `BUG: the DC (Desktop Commander) tunnel slot respawns constantly ("spawning proxy … on port 8813" repeating dozens of times rapidly). ROOT CAUSE already traced in the item: SlotProxy.is_running checks self._proc.poll() is None on the Popen handle, but DC's command ["cmd","/c","npx","-y","@wonderwhy-er/desktop-commander@latest"] on Windows: cmd /c RETURNS once its command returns, and if npx spawns the actual MCP server as a separate detached child, the cmd handle dies immediately → poll() reports not-running → the slot respawns on every request. RE-VERIFY SlotProxy in meridian/tunnel_client.py. FIX liveness so a slot whose launcher (cmd/npx) has exited but whose SERVER child is still listening on the port is considered running — e.g. is_running should ALSO check the port is accepting connections (a quick socket connect to 127.0.0.1:port), not rely solely on the launcher Popen handle; OR spawn without the cmd/c indirection so the tracked process IS the server. Keep it cross-platform (don't break the non-Windows path). Add tests/test_w5_c8e6b61c_dc_respawn.py mocking the Popen handle + a port-open check (no real process/port): a dead launcher handle + open port ⇒ is_running True (no respawn); dead handle + closed port ⇒ not running.`,
  },
  {
    id: 'e5e20464', slug: 'e5e20464_binary_tunnel',
    files: 'meridian/__main__.py (the --tunnel CLI entrypoint)',
    task: `URGENT BUG: the installed meridian-connect binary (v0.1.9) under --tunnel does NOT behave like source: it starts a local web dashboard on port 7700 and NEVER lazy-spawns any plugin slots, whereas \`pixi run python -m meridian --tunnel\` from source works. RE-VERIFY meridian/__main__.py: how the --tunnel flag is dispatched — does it correctly enter the tunnel client (spawn slots) path, or does an argument-parsing / frozen-app (PyInstaller sys.frozen) branch fall through to the dashboard server instead? Common causes: (a) --tunnel not parsed the same way in the frozen entrypoint, (b) a sys.frozen / __main__ guard that routes the binary to the dashboard, (c) the tunnel path requires a module import that the binary doesn't bundle. FIX what is fixable in meridian/__main__.py (argument dispatch / frozen-aware routing so --tunnel always enters the tunnel client). If the true root cause is the PyInstaller build spec / packaging (NOT Python source) — e.g. a missing hidden import or the binary's entrypoint script — then do the source-side hardening you can justify and BLOCK with a precise recipe naming the build file to change. Add tests/test_w5_e5e20464_binary_tunnel.py (argument dispatch: --tunnel routes to the tunnel path, not the dashboard, incl. a simulated sys.frozen).`,
  },
  {
    id: '9f7bfcca', slug: '9f7bfcca_trial_emails',
    files: 'meridian/server.py (+ meridian/hosted.py if needed)',
    task: `FEAT: Trial-expiration reminder emails at 14 / 7 / 1 days before a free trial ends. Infra exists: tenants have trial_started_at + inactivity_expires_at; the server computes days_remaining for the dashboard banner; hosted.py already sends email via Resend and the server lifespan already runs background loops (_auto_summary_loop / _version_check_loop / _run_session_keepalive_loop). Implement a daily pass that emails each trial tenant once per {14,7,1}-day threshold. IDEMPOTENCY WITHOUT A SCHEMA COLUMN (required): persist which thresholds were sent in an EXISTING per-tenant JSON field (inspect the tenants row for a JSON/settings/metadata blob; if genuinely none exists, use a lightweight existing table like task_log keyed by tenant+threshold) — do NOT add a migration. Wire the pass into the lifespan background-loop pattern, guarded to hosted mode. CRITICAL ANTI-STALL: the test MUST mock the email sender AND any time source and MUST NOT start a loop, sleep, bind a port, or hit the network — test the pure "which thresholds fire for this tenant, once each" logic as a plain function. Add tests/test_w5_9f7bfcca_trial_emails.py: correct thresholds fire; each fires once (idempotent); non-trial/expired tenants skipped. If a schema-free idempotency path genuinely does not exist, BLOCK with the exact design rather than adding a column.`,
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

Process: (1) RE-VERIFY the premise against CURRENT code (read the target files; report in premise_recheck; don't trust stale notes). (2) Implement production-quality, tightly-scoped code touching ONLY the stated file(s). (3) Add + run the dedicated test file under the hard timeout; all green. (4) Commit exactly your files. (5) Return the structured result honestly (blocked + recipe if you couldn't finish). Your final message IS the structured result.`
}

phase('Implement')
const results = await parallel(
  ITEMS.map((it) => () =>
    agent(buildPrompt(it), { label: `impl:${it.id}`, phase: 'Implement', isolation: 'worktree', schema: RESULT_SCHEMA })
  )
)
return results.filter(Boolean)
