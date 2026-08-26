# The Per-Mode Handoff Envelope Contract

Sprint item **b6502b35** (item_group `handoff-profile-parity`). This document
defines the envelope every `generate_handoff` mode should carry, which
context classes each mode is allowed to include, default mode selection, and
receiver behavior for the failure/degradation cases a handoff can arrive in.

**Scope note:** this is a *contract* document — it states what the shipped
code in `meridian/handoff.py`, `meridian/mcp/handler.py`,
`meridian/routes/handoff.py`, and `meridian/mcp/stdio_handler.py` actually
does today, grounded in specific field names and call sites, not aspirational
design. Where a field doesn't exist yet, that is stated explicitly rather
than implied. [`docs/meridian-handoff-contract.md`](meridian-handoff-contract.md)
is the companion reference for the token/body-integrity mechanism, bounded
payloads, and the receiving-executor checklist — this document does not
repeat that material, only cites it.

## Two envelopes, not one

A `generate_handoff` call produces two distinct envelopes that are easy to
conflate:

1. **The response envelope** — the JSON object the MCP/HTTP/stdio transport
   returns. Read programmatically; never parsed as text.
2. **The content envelope** — the XML/Markdown tags embedded *inside* the
   response's own `content` field (the rendered `/goal` block or full
   handoff). Bound into the goal-token's `body_hash` (see the companion doc's
   [Token and body integrity](meridian-handoff-contract.md#token-and-body-integrity)
   section) — this is what a receiving *executor* actually reads and acts on.

Some facts exist in both (`mode`), some only in one. The table below states
which, honestly, per field — this is the single biggest source of "the
contract implies X exists everywhere" confusion this document exists to
close.

## The envelope fields

| Field (contract intent) | Response envelope | Content envelope | Status |
|---|---|---|---|
| `project_id` | *(not present — caller already supplied it as an argument)* | `<project_start_config project_id="...">` (f471c4b8) | Pre-existing |
| `version` | `scope.requested_version` / `scope.effective_version` (b8f89491) | `<project_start_config version="...">` | Pre-existing |
| source session/run | `scope.session_id` (b8f89491); full session/run history via `run_timeline` (79491e26) | *(not separately embedded — the executor reads `<first_step>`'s literal `start_session(...)` call instead)* | Pre-existing |
| board revision | *(not present at the response level)* | `<handoff_manifest board_revision="...">` — **opt-in**, `emit_manifest=True` only (see `compute_board_revision`) | Pre-existing, opt-in |
| `mode` | `mode` (always present) | Implicit in the block's own shape (goal vs. full narrative etc.) | Pre-existing |
| executable / degraded status | `degraded` (timeout-only signal, 65c8b426); `capability_contract.executable` (98aaccf4); `blocker_policy` (b108f2e0); `docx_integrity.executable` | `<proposal_scope executable="..." degraded="...">` — **opt-in**, proposal-promotion flow only | Pre-existing |
| **persistence/retrievability status** | **`retrievable_via_load_handoff`** | — | **NEW (d2fc7465)** |
| next action | *(not present at the response level)* | `<first_step>` | Pre-existing |
| item counts | *(not present at the response level for the general case)* | `<executor_item_ids count="N">`; `handoff_manifest.items_total`/`items_truncated` — opt-in | Pre-existing |
| omitted IDs + reason | `force_include_rejected` (3cab355a, for `force_include_ids`); **`selected_scope.excluded_requested`** (for `selected_item_ids`) | `<excluded_wave_gate_pending>`; manifest `omitted_items` — opt-in | Pre-existing (`force_include_rejected`) + **NEW (d2fc7465, `selected_scope`)** |
| **selected-scope ids + closure** | **`selected_scope.selected_item_ids` / `.closure_item_ids` / `.closure_hash`** | `<selected_item_scope requested="..." closure="..." closure_hash="...">` (cffb9323) | Content: pre-existing. **Response field: NEW (d2fc7465)** |
| pointer policy/budget | `handoff_evidence_status` (8a883f60) | `<tool_requirements_truncated total="N" included="M">` etc. (248c0bb9) | Pre-existing |
| capability snapshot | `capability_contract` (98aaccf4, delegates to `meridian.capability_contract.build_capability_contract`) | — | Pre-existing |
| lineage | `run_timeline` (79491e26); `amended` (edd9c54b) | `<continuation_manifest>` — delta mode only (836ca1d5) | Pre-existing |
| content hash | *(no unconditional field)* — the nearest universal equivalent is the goal-token's `body_hash`, checked via `verify_handoff_token`, never returned in plaintext | `<proposal_scope content_hash="...">` — **opt-in**, proposal-promotion flow only | Pre-existing, narrower than the addendum's ask (see below) |

**On "content hash":** the addendum that prompted this document asked for a
content hash on every handoff so generation and retrieval can be proven to
agree. The mechanism that actually exists and is unconditional for every
`/goal`-producing path is the goal-token's `body_hash` (efaa918a) —
`verify_handoff_token(db, token, project_id, body=presented_body)` proves
byte-identity against what was hashed at mint time. There is no *separate*,
always-present `content_hash` **response field** distinct from the token
mechanism; `build_proposal_run_scope`'s own `content_hash` (a hash of
project/version/items/omitted, independent of body text) is real but scoped
to the proposal-promotion flow only, not general-purpose. Treat "verify via
`verify_handoff_token`" as the answer to "can I prove this content is
what was generated" today, rather than expecting a bare `content_hash` key
on every response.

## Persistence and retrievability — the machine-readable contract

This is the concrete fix for the addendum's "PERSISTENCE READINESS" /
"FINAL READINESS GATE" bug report (a fresh `generate_handoff(mode='goal')`
disagreeing with an immediate `load_handoff()`).

**What was already fixed, before this sprint item ran (aec043cb):**
`mode='goal'` is wired into the exact same
`_persist_handoff_history_and_pending_goal` call full/delta have always used
— it writes to the `handoffs` history table (`db.record_handoff`/
`db.amend_handoff`) **and** the trusted `pending_goal` channel
(`db.set_pending_goal`) that `start_session`'s `pending_goal` field and
`load_handoff()` both read from. A regression test proving this
(`tests/test_aec043cb_handoff_mode_scoping.py::
test_load_handoff_returns_the_stored_omitted_mode_body`) already existed on
dev tip before this sprint item started.

**What this pass makes explicit (d2fc7465):** which modes are retrievable
was previously something a caller had to infer from source, or discover the
hard way via a stale-looking `load_handoff()` read. Every `generate_handoff`
response (all three transports) now carries:

```
"retrievable_via_load_handoff": true | false
```

backed by one pure function, `handoff.handoff_mode_is_retrievable(mode)`, so
the three transports (`meridian/mcp/handler.py`,
`meridian/routes/handoff.py`, `meridian/mcp/stdio_handler.py`) cannot drift
out of agreement with each other:

| Mode | Persists to `handoffs` + `pending_goal`? | `retrievable_via_load_handoff` |
|---|---|---|
| `full` | Yes | `true` |
| `delta` | Yes | `true` |
| `goal` | Yes (aec043cb) | `true` |
| `starter` / `compact` | **No** — a call-and-forget render meant to be pasted directly, by design | `false` |
| `planner` | **No** — a directive prompt for a *human* planning session, by design | `false` |
| `l0_fallback` (emergency timeout degrade) | No — never reaches the persistence step | `false` |

This is the explicit, machine-readable version of "if goal-mode is
intentionally ephemeral/non-persistent by design, make that an explicit,
documented, machine-readable contract" the addendum asked for — generalized
to every mode, not just `goal`. `tests/test_782636cd_handoff_regression_matrix.py`
carries the dedicated regression: generating `goal` then `starter` in
sequence and confirming `load_handoff()` still returns the `goal` content,
never the ephemeral `starter` preview and never a stale/broken record.

## `selected_scope` — omitted-ids visibility on partial exclusion

The other addendum item ("expose `selected_item_ids` safely... return
explicit `selected_scope_ids` plus omitted IDs") was **already implemented**
on dev tip before this sprint item ran:

- The public MCP schema already accepts `selected_item_ids` (cffb9323).
- `generate_handoff` already validates every id, dependency-closes the
  selection, and fails closed (`HandoffSelectionError`) on any invalid id
  (94f48e4d) — before this sprint item started.
- The closure ids and a stable hash were already rendered into the body's
  `<selected_item_scope>` tag, bound into the same token `body_hash` as the
  rest of the content.

**The genuine remaining gap, closed by this pass:** a caller that requested a
*partial* exclusion — some, not all, of the requested ids dropped by a
downstream claimability gate (unprospected/backburner/manual/
wave_gate_pending) — had no structured way to learn *which* ids were dropped
and *why*. That signal (`excluded_requested`, with a machine-readable reason
per id) already existed internally (fb82e51f), but was only ever surfaced on
**total** exclusion, as the `HANDOFF_SCOPE_NON_EXECUTABLE` error's
`excluded_requested` field. A caller whose call *succeeded* with a partial
exclusion got nothing.

Every `generate_handoff` response now carries a `selected_scope` field,
`null` when `selected_item_ids` was never passed, otherwise:

```json
{
  "selected_item_ids": ["<id requested by the caller>", "..."],
  "closure_item_ids": ["<selected ids plus any pending depends_on ancestor>", "..."],
  "closure_hash": "<sha256 of the sorted closure — matches <selected_item_scope closure_hash=...>>",
  "requested_ids": ["<same as selected_item_ids, post claimability filtering>"],
  "executable_ids": ["<requested ids that survived every downstream filter>"],
  "excluded_requested": [{"id": "...", "reason": "unprospected|backburner|manual|wave_gate_pending|not_in_pending_batch"}],
  "all_excluded": false
}
```

`all_excluded: true` is exactly the condition that instead raises
`HandoffScopeNonExecutable` before anything renders — so a caller only ever
sees `selected_scope` with `all_excluded: false` on a successful response;
the total-failure case is the existing structured error, unchanged.

## Context classes allowed per mode

Unchanged by this sprint item — stated here for completeness, since it is
part of the envelope contract:

| Mode | Workspace decisions/notes (cross-project) | L0/L1/L2 narrative | Self-start bootstrap |
|---|:---:|:---:|:---:|
| `full` | **Yes** (only mode that includes them) | Yes | No |
| `delta` | No | Compact (what changed) | No |
| `planner` | No | Yes (project-scoped) | No |
| `starter` / `compact` | No | No (light preview only) | Yes |
| `goal` | No | No (zero framing) | Yes |

The invariant (aec043cb): **an omitted `mode` argument never silently
resolves to `full`.** `full` — the only mode that unconditionally prepends
every workspace decision/note across every project — is returned *only* for
an explicit `mode='full'` request. See
[`resolve_handoff_mode`](../meridian/handoff.py)'s own docstring for the
full intent-based resolution order (resumed session → `delta`; role=executor
→ `goal`; role=planner → `planner`; unknown intent → `goal`, never `full`).
`tests/test_aec043cb_handoff_mode_scoping.py` is the existing regression
suite for this table's workspace-context-exclusion column.

## Default mode selection

Already governed by `resolve_handoff_mode` (aec043cb) — restated here as
part of the envelope contract rather than re-derived:

1. An explicit, recognized `requested_mode` always wins.
2. A session that already produced a handoff this process resolves to
   `delta` (continuation).
3. A session registered `role="executor"` resolves to `goal`.
4. A session registered `role="planner"` resolves to `planner`.
5. Anything else (no session, or an unregistered role) resolves to `goal` —
   the narrowest, leak-free option — **never** `full`.

## Receiver behavior

**Missing/degraded capability:** `capability_contract.executable` reflects
whether a `required` capability (per the project's capability manifest — see
`AGENTS.md`'s Capability Manifests section) is genuinely available; a
receiver should check this before treating a handoff as ready to execute
rather than discovering non-executability mid-run. `blocker_policy`
(b108f2e0) carries the typed per-item triage when specific items are
quarantined.

**Stale board:** `board_stale` (passed into `build_effective_capability_contract`)
is `true` whenever the handoff's own board/profile snapshot is known
incomplete — currently set on the `l0_fallback` emergency-timeout path.
`retrievable_via_load_handoff` is `false` on that same path (see above),
compounding the signal: a receiver seeing `degraded: true` should not expect
`load_handoff()` to later hand back this exact render either.

**Expired/consumed token:** unchanged — see the companion contract doc's
[Verifying](meridian-handoff-contract.md#verifying) section for the full
`reason` → `recovery` table (`ok`/`not_found`/`wrong_project`/
`body_mismatch`/`already_consumed`/`expired`/no-token-at-all) and which
reasons are genuine spoofing signals vs. "a sibling likely got there first."

**Degraded/offline:** the `l0_fallback` mode (65c8b426) is the concrete
instance — an emergency 4-field render (north star + pinned decisions) used
when the full generation path times out. `mode: "l0_fallback"`,
`degraded: true`, and (as of this pass) `retrievable_via_load_handoff: false`
together make this unambiguous rather than requiring a receiver to notice an
unexpectedly-short `full` handoff.

## Legacy-caller compatibility

Both fields this pass adds (`selected_scope`, `retrievable_via_load_handoff`)
are pure additions to the response dict on all three transports:

- A caller that never passes `selected_item_ids` sees `selected_scope: null`
  — no change to `content`, `mode`, or any other field.
- `retrievable_via_load_handoff` is a new key with no prior meaning to
  collide with; every existing caller that ignores unknown response keys
  (the overwhelming common case for a JSON-RPC-style tool result) is
  unaffected.
- No existing parameter's default behavior, error type, or `content` byte
  shape changed. `tests/test_782636cd_handoff_regression_matrix.py` and the
  full pre-existing handoff test suite (30 files) both pass unmodified
  against this change.

## See also

- [`meridian-handoff-contract.md`](meridian-handoff-contract.md) — token/body
  integrity, bounded payloads, board-revision drift detection, evidence
  sections, and the receiving-executor checklist (extended by this sprint
  item's own DOCS task — see its "Receiver runbook" section).
- `AGENTS.md` — the executor-facing quick-reference trust rules and the
  capability-manifest/fallback contract.
- `meridian/handoff.py` — `handoff_mode_is_retrievable`,
  `_resolve_selected_item_scope`, `_build_quick_start_goal`'s
  `selected_scope_outcome` docstring — the actual source this document
  describes.
