export const meta = {
  name: 'wave5-batchD-parallel',
  description: 'Parallel implement/verify 6 backend items (incl. tonight 2 bugs) in isolated worktrees',
  phases: [{ title: 'Implement' }],
}

const CONVENTIONS = `
Meridian repo conventions (production SaaS — production-quality code only, no stubs):
- Python 3.12, FastAPI, psycopg3 (SQL %s NOT ?; literal % in LIKE = %%; autocommit — but aiosqlite paths call await db.commit(), match surrounding code).
- Tools accept project_name as an alternative to project_id ("resolved to the id internally; project_id wins if both given"). The MCP dispatcher has a resolution layer — find how existing tools resolve project_name→project_id and reuse it.
- Schema changes are HIGH-RISK (SQLite migrations.py + PG pg_adapter.py parity + test_core count bump; a bad one caused a prod outage). PREFER a JSON blob over a new column.
HARD RULES: never touch .env / meridian.toml / hooks.ps1 / hooks.sh. Never 'git add -A'/'git add .'/'git commit -a' — stage ONLY the exact files you changed, by path. No secrets.
`

const TESTING = (slug) => `
Put ALL new tests in a NEW file tests/test_w5_${slug}.py (never append to shared test files). Unit-level with mocks ONLY — NO real servers/ports/network/sleeps (a prior agent hung forever on a networked test).
Run under a HARD timeout so nothing can hang: timeout 120 pixi run python -m pytest tests/test_w5_${slug}.py -q -p no:cacheprovider --timeout=30
If it times out or a test reaches the network, FIX the test (mock it). All green before committing.
`

const COMMIT = (id, slug) => `
Only when complete AND tests pass: git add <exact files by path>; git commit -m "fix(${id}): <summary>" (or feat(${id})); then git rev-parse HEAD → return as commit_sha. EXACTLY ONE commit. Do NOT push/merge.
If genuinely blocked / too large / a prior deferral still holds after you RE-VERIFY vs current code: DO NOT commit — return status="blocked" with a specific current-code reason + concrete recipe.
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
    id: '8f01cdfe', slug: '8f01cdfe_addsprint_projname',
    files: 'meridian/mcp/handler.py (add_sprint_item dispatch, ~line 3277)',
    task: `BUG: add_sprint_item throws a raw "KeyError: project_id" when called with project_name instead of project_id (or with neither + no active session) — even though the schema/description say project_name is an accepted alternative. FIX: in the add_sprint_item handler, resolve project_name→project_id the SAME way other tools do (find the existing resolution helper/layer in the dispatcher — grep for project_name resolution / get_project_by_name usage in meridian/mcp/handler.py) before args["project_id"] is read; and when NEITHER project_id nor a resolvable project_name is present, return a clean, descriptive error dict (NOT a raw KeyError). Test tests/test_w5_8f01cdfe_addsprint_projname.py: add via project_name works; add with neither returns a clean error (no KeyError). Only touch the add_sprint_item function region.`,
  },
  {
    id: '443d9453', slug: '443d9453_pointer_selector',
    files: 'meridian/mcp_tools.py (add_sprint_item_pointer schema/description) + meridian/pointers.py (validation)',
    task: `BUG: add_sprint_item_pointer's selector schema is undocumented/underspecified — a node_id selector needs field 'id' (not 'value'), and any subSelector without its own explicit 'type' is mishandled. RE-VERIFY against current meridian/pointers.py (selector parsing) + the add_sprint_item_pointer tool schema in meridian/mcp_tools.py. FIX: (1) document the selector schema precisely in the tool description (node_id → {"type":"node_id","id": ...}; each subSelector must carry its own "type"), and (2) make pointers.py validation give a clear error for a missing selector 'id'/'type' instead of silently mis-parsing. Test tests/test_w5_443d9453_pointer_selector.py: a node_id selector with 'id' resolves; one with 'value' (wrong) or a subSelector missing 'type' yields a clear validation error. Do NOT touch meridian/mcp/handler.py (avoid a merge collision) — keep changes to mcp_tools.py + pointers.py.`,
  },
  {
    id: 'e726810d', slug: 'e726810d_identity_dedup',
    files: 'meridian/handoff.py (_resolve_caller_identity) + meridian/mcp/handler.py (its wiring)',
    task: `BUG: "ajc123private" and "adam" show as TWO separate people in the dashboard active-sessions/standup view, though both are Adam. This is a gap in bdc251ec's identity resolution (meridian/handoff.py _resolve_caller_identity + its wiring in handler.py). RE-VERIFY how _resolve_caller_identity maps a human_id/email to a canonical display identity. FIX so a known alias set (e.g. the email local-part "ajc123private" and the display name "adam" for the same person) collapses to ONE canonical identity in the sessions/standup aggregation. Prefer a data-driven alias map (config/executor_config/workspace_settings JSON — no schema column) over hardcoding; if you must seed a default alias, make it overridable. Test tests/test_w5_e726810d_identity_dedup.py: two sessions under the two aliases aggregate to one person. In handler.py only touch the identity-resolution wiring (a different function than add_sprint_item/tool_search).`,
  },
  {
    id: '90d04961', slug: '90d04961_toolsearch_live',
    files: 'meridian/mcp/handler.py (tool_search)',
    task: `BUG: tool_search doesn't surface live tunnel-bridged tools — a slot that became active MID-session (word slot: list_plugins reports 42 real tools) is missing from tool_search results. Refined hypothesis (test it against code): tool_search snapshots available MCP tools ONCE at session start rather than re-querying live tools/list. RE-VERIFY in meridian/mcp/handler.py how tool_search builds its candidate tool set and whether it caches. FIX so tool_search reflects currently-active tunnel-bridged tools (re-query live tools/list, or invalidate the snapshot when slot health/activity changes) — bounded so it stays fast. Test tests/test_w5_90d04961_toolsearch_live.py (mock the tunnel tools/list; a tool that appears after the initial snapshot is now found). In handler.py only touch the tool_search function region (different function than add_sprint_item/identity).`,
  },
  {
    id: '0dfb107e', slug: '0dfb107e_slot_warn',
    files: 'meridian/tunnel_client.py',
    task: `BUG: tunnel startup gives ZERO warning when a core slot is disabled/misconfigured — e.g. the word slot is silently absent from the whole startup log, whereas filesystem's missing-root case DOES warn explicitly. RE-VERIFY meridian/tunnel_client.py: the startup per-slot lazy-spawn log lines + the office_proxies loop that decides which slots to attempt, and compare to the filesystem-root-missing warning path (which already warns correctly). FIX: mirror that pattern — emit a clear startup warning when a core/expected slot is disabled or misconfigured (so a silently-absent slot is visible). Test tests/test_w5_0dfb107e_slot_warn.py (a disabled/misconfigured core slot produces a warning log line; a healthy slot does not).`,
  },
  {
    id: '39c117b1', slug: '39c117b1_zotero_slot',
    files: 'meridian/tunnel_plugins.py, meridian/routes/tunnel.py (+ maybe meridian/zotero_client.py)',
    task: `Wire zotero-mcp as a CORE/default bundled tunnel slot — SAME shape as 9665538a just did for meridian-docs (which is now a first-class 'docs' slot). Adam's corrected direction (see item notes): zotero-mcp as a core/default tunnel plugin (uvx zotero-mcp, env ZOTERO_LOCAL=true — mirror the existing custom-plugin env example in test_custom_plugin_carries_env), NOT tunnel-proxying Meridian's own hand-rolled zotero_client. RE-VERIFY current BUILTIN_PLUGINS (the new 'docs' slot from 9665538a is the exact template to copy). Add a BUILTIN_PLUGINS entry for zotero-mcp on its own slot (e.g. 'zotero', next free port after 8818) with command ["uvx","zotero-mcp"], env {"ZOTERO_LOCAL":"true"}, plus the full per-slot routing in routes/tunnel.py mirroring the docs/word slot wiring (WS route, HTTP proxy, status, _TUNNEL_LABELS, SLOT_DISPLAY_NAMES, _label_maps, has_active_tunnel, active_tunnel_tenant_ids). ALSO update the a8a54fe9 KNOWN_PLUGIN_TOOLS catalog: mark zotero-mcp bundled=True, slot=<your slot>, owner_item=None (it's shipped now) so the catalog-completeness invariant in tests/test_w5_a8a54fe9_tunnel_bundle.py still holds — RUN that test file too. Add tests/test_w5_39c117b1_zotero_slot.py. NOTE: 9665538a added a 'docs' slot to these same files on dev already (your worktree includes it) — build ALONGSIDE it, do not remove it.`,
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

Process: (1) RE-VERIFY the premise against CURRENT code (read the target files; report in premise_recheck; don't trust stale notes). (2) Implement production-quality, tightly-scoped code touching ONLY the stated function(s)/file(s) to avoid cross-item merge collisions. (3) Add + run the dedicated test file under the hard timeout; all green. (4) Commit exactly your files. (5) Return the structured result honestly (blocked + recipe if you couldn't finish). Your final message IS the structured result.`
}

phase('Implement')
const results = await parallel(
  ITEMS.map((it) => () =>
    agent(buildPrompt(it), { label: `impl:${it.id}`, phase: 'Implement', isolation: 'worktree', schema: RESULT_SCHEMA })
  )
)
return results.filter(Boolean)
