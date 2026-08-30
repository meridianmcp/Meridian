# v0.2.6 Board Reconciliation — Sprint Item 6935a3ff

**Type:** Planning/reconciliation only. No code edits, version bump, push, deploy, or
contaminated-checkout cleanup were performed. No sprint item was claimed, completed, or
modified by this pass other than 6935a3ff itself (already `in_progress`, claimed by this
session, `a0c162f5-214d-4e8d-ad61-d78109c17ff2`, at 2026-08-30 04:10:57).

**Repo state at time of audit:** worktree `.claude/worktrees/a0c162f5`, branch
`worktree/6935a3ff`, based on `dev` tip `c8a84f2e9c05a8cf3961f1f056ea78c99422e4bf`. Working
tree clean throughout — verified with `git status` before and after this pass.

**Project:** Meridian, `project_id = 5787cc92-ba7d-4788-b17c-28ab7938b839`.

---

## 1. Executive summary

- The live board has **327 items in the `v0.2.6` bucket** (`get_sprint_progress` and a
  direct `get_sprint_items` pull agree exactly): 275 done, 4 skipped, **36 pending**, **12
  in_progress**, 0 failed. **The pending+in_progress slice this report reconciles is 48
  items.**
- All four commits/facts named in this item's own citation were independently re-verified
  against real git history and real GitHub Actions runs (Section 3). **One of the four
  citations does not check out as given**: the SHA `c11d20ab9ad6242a149301441905113f84b7742`
  cited for the `normalize_repo_root` fix does not exist in this repository, locally or on
  GitHub (`gh api` returns 404/422 for it). The real, verified commit for that fix is
  **`7278a44fa455bb4d5c51217350536095b9f07e2d`**, confirmed both by `git log` on `dev` and by
  a real `gh run view` matching that exact `headSha` to a successful CI run. I record the
  real evidence, not the cited SHA — see Section 3.4 for the full trail.
- Beyond the four task-cited facts, real git evidence in this pass **independently surfaced
  two more `in_progress` v0.2.6 items whose described work is already merged into `dev` and
  passing its tests but never marked complete on the board** (`9154aa9a`, `f69b5f18` —
  Section 4.1), and **four more `in_progress` items with real, committed work that was never
  merged and is now sitting on an archived/orphaned worktree ref** (`ef8875a9`, `8d2ef784`,
  `2cf57fde`, `b71e0960` — Section 4.2).
- **Six items (four pending, two in_progress) carry a claim from an `actor` string
  (`rescue-sweep-recovery-frontier(2)-<id>`) that has no matching row anywhere in
  `list_sessions` and no matching commit anywhere in `git log --all`.** All six were
  "claimed" in the same ~10-second window on 2026-08-09 01:16:22–01:16:31 (plus one at
  2026-08-08 23:39:19 from an apparent earlier wave), 20+ days ago, with zero evidence of any
  work since. This is the strongest stale-claim signal found (Section 5.1).
- Two pending items already carry an objective `blocker_kind = "superseded"` field
  (`96437ed7`, `0925b521`) — no re-classification needed, just confirmed and reported
  (Section 6).
- The `96437ed7` / `9fcb2cc9` / `f56bd1b8` "Batch exposure B/D/C" family (children of parent
  `7ac7f633`) is entangled with a **still-`pending`, high-urgency HITL gate opened
  2026-08-08** documenting a real, previously-caught prompt-injection attempt against this
  exact item family. That HITL gate is unanswered as of this pass. I recommend it stay
  blocking (Section 6.2).
- **30 of the 36 pending v0.2.6 items were never claimed at all** (`actor` and `claimed_at`
  both null) — there is no claim to reconcile for them; they are listed in Section 7 for
  completeness only, with no action recommended.
- Section 8 gives a minimal keep/prune/reclassify plan. Nothing in this plan was executed —
  all of it is a recommendation for a human or a future executor session to act on.

---

## 2. Scope and method

Per the task's own scope note, auditing "every pending and in_progress item" board-wide
(2,280+ items total) is not tractable in one pass and would produce unverifiable guesses for
the vast majority of unrelated backlog. This pass is scoped to:

> items where `version = 'v0.2.6'` AND (`status = 'pending'` OR `status = 'in_progress'`)

Method, in order:

1. `get_sprint_progress(project_id, version="v0.2.6")` for the authoritative bucket counts.
2. `get_sprint_items(status="pending", expand=true)` and `get_sprint_items(status="in_progress", expand=true)` — both came back too large for direct tool output (382,562 and 84,794 characters) and were saved to disk by the tool runtime; I filtered them with a small local Node.js script (`v0.2.6` only) rather than reading either raw dump by hand. The filtered counts (36 pending, 12 in_progress) match `get_sprint_progress`'s counts exactly, which is a real cross-check that the version filter is sound.
3. `list_sessions(project_id, status="all")` — 851 sessions returned, also saved to disk; looked up by exact session-id match for every `actor` UUID found on a v0.2.6 claim, rather than reading the full dump.
4. `git log --all --oneline` (4,408 commits across all local refs, including archived refs — see 4.2) grepped for the first-8-hex-character short form of every one of the 48 item ids, since this repo's own commit convention embeds that short id in messages (`fix(<shortid>): ...`).
5. For every candidate hit, checked `git merge-base --is-ancestor <sha> dev` to tell "merged into dev" from "exists only on some branch/ref."
6. For the two strongest "done but unrecorded" candidates, went one step further than commit messages, per this item's own instruction not to trust commit messages alone: confirmed the described code actually exists in the current tree (`grep` for the named functions/files) and ran the two most directly relevant test files with `pixi run python -m pytest <file> -q --timeout=60`. Both passed (25/25 and 42/42 — Section 4.1).
7. `gh run list --branch dev` / `gh run view` / `gh api repos/.../commits/<sha>` — real calls, real output, reproduced verbatim in Section 3.
8. `get_hitl_request` for the one open HITL gate touching this item slice.

I did **not** run the full test suite, did not touch any file outside this new report, and
did not claim/complete/modify any sprint item.

---

## 3. Task-cited facts — independently re-verified

### 3.1 CI-blocking stale-test fix — commit `1533cb1e`

```
$ git log --format='%H | %ci | %s' -1 1533cb1e
1533cb1e0e371af6db4d9a838d020a950dac2d7c | 2026-08-29 13:17:32 -0500 | fix(tests): retire ea49362c's stale zero-diff-vs-b0deb335 baseline tests
```

Real CI evidence (`gh run list --branch dev --limit 30`, exact rows):

```
completed  success  fix(tests): retire ea49362c's stale zero-diff-vs-b0deb335 baseline tests   Test (dev branch)   dev  push  33267948202  9m48s   2026-08-29T18:19:31Z
completed  success  fix(tests): retire ea49362c's stale zero-diff-vs-b0deb335 baseline tests   Deploy to Fly.io    dev  push  33267948197  11m29s  2026-08-29T18:19:31Z
completed  failure  fix(handoff): canonical role-correct serialization, stable content co…     Deploy to Fly.io    dev  push  33264699998  8m8s    2026-08-29T17:05:16Z
completed  failure  fix(handoff): canonical role-correct serialization, stable content co…     Test (dev branch)   dev  push  33264699895  9m50s   2026-08-29T17:05:16Z
```

`gh run list --branch dev --limit 5 --json ...` confirms the failing `headSha` was
`181746baa723efb61a9fb3dca5d904cb7c230590` (= `181746ba`, the commit immediately before
1533cb1e on dev) and the fixing `headSha` was `1533cb1e0e371af6db4d9a838d020a950dac2d7c`.
**Confirmed: dev CI was red on 181746ba and went green on 1533cb1e, both via real GitHub
Actions runs, not a commit-message claim.**

### 3.2 Stale-claim reset fix — commit `2b9eca9a` (item `f007e59e`)

```
$ git log --format='%H | %ci | %s' -1 2b9eca9a
2b9eca9a5d8520d81df95630ec57eca6739dfc3d | 2026-08-29 22:08:21 -0500 | fix(sprint): stale claim reset must clear owner metadata and restore wave eligibility (f007e59e)
```

`f007e59e` does not appear anywhere in the current v0.2.6 pending/in_progress slice (48
items, checked by grep against the filtered JSON) — consistent with it having already moved
to `done`.

### 3.3 Anthropic MCP Directory items — `f007e59e` / `68b7bd9a` / `f1c6dd63`

```
$ git log --format='%H | %ci | %s' -1 c8a84f2e
c8a84f2e9c05a8cf3961f1f056ea78c99422e4bf | 2026-08-29 23:08:36 -0500 | fix(disclosure): explicitly name connected GitHub repo in 5 tool descriptions (Anthropic Fix #3, f1c6dd63)
$ git log --format='%H | %ci | %s' -1 f7d8184e
f7d8184e2d35fc6aa60d113473bc3af405e5149f | 2026-08-29 22:45:01 -0500 | fix(disclosure): strengthen persistence notice for Anthropic MCP Directory Fix #2 (68b7bd9a)
```

All three of `f1c6dd63`, `68b7bd9a`, `f007e59e` are absent from the current pending/in_progress
v0.2.6 slice — consistent with "done." **Caveat, stated plainly rather than glossed over:**
`git branch -vv` shows local `dev` is **9 commits ahead of `origin/dev`** — these three
commits (plus 1533cb1e and the two "docs: meridian auto-update" commits) exist only in this
local checkout and have not been pushed. There is therefore **no CI run yet** covering
`c8a84f2e`, `f7d8184e`, or `2b9eca9a` specifically — the corroboration for these three is the
commits themselves plus their absence from the live pending/in_progress board (which this
session produced directly), not a green CI run. I am not claiming CI evidence I don't have.

### 3.4 `normalize_repo_root` — citation discrepancy found and resolved with real evidence

The item's own citation names SHA `c11d20ab9ad6242a149301441905113f84b7742`. Real, direct
verification:

```
$ git show -s --format='%H %ci %s' c11d20ab9ad6242a149301441905113f84b7742
fatal: ambiguous argument 'c11d20ab9ad6242a149301441905113f84b7742': unknown revision or path not in the working tree.

$ gh api repos/meridianmcp/Meridian/commits/c11d20ab9ad6242a149301441905113f84b7742
{"message":"No commit found for SHA: c11d20ab9ad6242a149301441905113f84b7742","status":"422"}

$ gh api repos/meridianmcp/Meridian/commits/c11d20ab9ad6242a149301441905113f84b7742/status
{"message":"Ref not found","status":"404"}
```

**This SHA does not exist in this repository, locally or on GitHub.** Per this item's own
instruction ("record the ... release evidence ... without inventing a sprint completion"), I
am not treating the cited SHA as valid just because it was handed to me — I checked it, and
it fails. What I found instead, from real `git log` and a real `gh` call:

```
$ git log --format='%H %ci %s' dev | grep "normalize_repo_root strips trailing backslash"
7278a44fa455bb4d5c51217350536095b9f07e2d 2026-08-26 16:54:43 -0500 fix(ci): normalize_repo_root strips trailing backslash before resolving

$ gh run view 33017497610 --json headSha,conclusion,displayTitle,status
{"conclusion":"success","displayTitle":"fix(ci): normalize_repo_root strips trailing backslash before resolving","headSha":"7278a44fa455bb4d5c51217350536095b9f07e2d","status":"completed"}
```

Both `Test (dev branch)` and `Deploy to Fly.io` completed with `conclusion: success` for this
exact `headSha`, per `gh run list --branch dev --limit 30`:

```
completed  success  fix(ci): normalize_repo_root strips trailing backslash before resolving   Test (dev branch)   dev  push  33017497610  9m17s   2026-08-26T21:55:08Z
completed  success  fix(ci): normalize_repo_root strips trailing backslash before resolving   Deploy to Fly.io    dev  push  33017497681  12m34s  2026-08-26T21:55:08Z
```

**Conclusion: the `normalize_repo_root` fix is real, merged to `dev`, and CI-green — but
under commit `7278a44fa455bb4d5c51217350536095b9f07e2d`, not the SHA originally cited.**
Whoever/whatever produced the `c11d20ab...` citation should be corrected; I am not treating
the mismatch as proof of anything sinister on its own (it's equally consistent with a
transcription slip or a rebase that changed the SHA), but it is exactly the kind of claim
this item told me to verify rather than accept, and it did not check out as given.

---

## 4. Newly corroborated findings (beyond the task's own citations)

Grepping `git log --all` for the short-id form of all 48 v0.2.6 pending/in_progress items
surfaced real commits for **six** of the twelve `in_progress` items that the live board still
shows as unfinished. None of these six were claimed done by this pass — completing a sprint
item is out of scope for this document-only item — but they are reported here with full
evidence because that is exactly what 6935a3ff asks for.

### 4.1 Tier 1 — merged into `dev`, code present, tests passing (strong "done, unrecorded")

| Item | Title | Actor / session | Session status |
|---|---|---|---|
| `9154aa9a` | FEAT: add durable executor-report and corrective-handoff lifecycle for planner review and deterministic continuation | `c0cc71a3-b90e-45ae-b84d-aa62203f6cd6` ("v0.2.6-process-lifecycle-followup") | **archived**, last_seen 2026-08-08 17:31 |
| `f69b5f18` | TEST: prove dispatcher capacity release, terminal reconciliation, and restart recovery end to end | `81894c30-2daf-455a-856a-3d6507ac23f2` ("dispatcher-lifecycle-wave2-f69b5f18") | **archived**, last_seen 2026-08-10 01:07 |

Evidence, not commit messages alone:

- `9154aa9a`: commits `1f5f178e7acf9b08ca3e87b3e409b2529bd05638` and
  `a4887bc6e7e4fc53536aa89b0a807f33239182de` are real ancestors of `dev`
  (`git merge-base --is-ancestor ... dev` → exit 0). `meridian/executor_contract.py` exists
  in the current tree with `def build_executor_contract`. Ran
  `pixi run python -m pytest tests/test_handoff_executor_planner_lifecycle.py -q --timeout=60`
  → **42 passed in 7.71s**.
- `f69b5f18`: commits `cfd9342af746c3a222916f151e5458901d185289` and
  `0c1b696d7fadc831b4cf42017aab7f0010968738` are real ancestors of `dev`. Ran
  `pixi run python -m pytest tests/test_769e24a7_dispatcher_completion.py -q --timeout=60`
  → **25 passed in 7.25s**.

Both claiming sessions are archived and neither called `complete_sprint_item`. This looks
like genuine "done but unrecorded" work — recommend a human/executor confirm scope match
against the acceptance criteria in each item's notes, then complete them.

### 4.2 Tier 2 — real commits exist, never merged, worktree since archived as an orphan

On 2026-08-29 this repo ran a worktree-hygiene cleanup (commits `448e949b`, `3f23e57f`,
`e8456723` — visible at the top of `git log` on `dev`) that moved orphaned worktree branches
out of `refs/heads/*` into `refs/archive/worktree-cleanup-20260829/*` rather than deleting
them outright. Four v0.2.6 `in_progress` items have real work sitting in that archive that
never reached `dev`:

| Item | Title | Actor / session | Archived ref | Merged to dev? |
|---|---|---|---|---|
| `ef8875a9` | IMPLEMENT DOCS-R2-D: OMML equation-to-numbered-two-column-row conversion | `b9e9aadb-...` ("profile-reconciliation") | `refs/archive/worktree-cleanup-20260829/worktree/8d2ef784` | **No** |
| `8d2ef784` | IMPLEMENT DOCS-R2-A: port tested fallback contracts / bounded Word-COM failure semantics | same session, same ref | same ref | **No** |
| `2cf57fde` | FEAT: expose Redis runtime health, cache effectiveness, and Neon-avoidance diagnostics | `e6a00510-...` ("ctrlwave-2cf57fde-b71e0960") | `refs/archive/worktree-cleanup-20260829/worktree-agent-a7f5104cb61ac71af` | **No** |
| `b71e0960` | TEST: bounded cross-MCP batch fanout, discovery refresh, and tunnel cold-start stress contract | same session, same ref | same ref | **No** |

Verified negative (i.e., I checked that the described feature is genuinely absent from `dev`,
not just that a merge check failed):

- `ef8875a9`: `find . -iname "test_equation_numbering*"` → no results anywhere in the tree.
- `8d2ef784`: `find . -iname "test_fallback_contracts*"` → no results anywhere in the tree.
- `2cf57fde`: `meridian/redis_bridge.py` and `get_redis_client` do exist in `dev` (from
  unrelated earlier work), but there is no diagnostics-exposure function matching this item's
  description in `meridian/routes/tunnel.py` — the specific feature this item describes is
  not present.
- `b71e0960`: no `tests/test_batch_read.py` (the file this item's own `touches_resources`
  names) exists anywhere; the only similarly-named file is
  `tests/test_batch_read_mutate_133bfff6.py`, which belongs to a different, already-merged
  item (`133bfff6`), not this one.

Both sessions behind this work are `archived` in `list_sessions`, and their session names
literally reference the item ids they worked (`ctrlwave-2cf57fde-b71e0960`,
`dispatcher-lifecycle-wave2-f69b5f18` above) — a real, human-legible link between claim and
worktree, not an inference. **Classification: genuinely attempted, real partial work exists,
but not done and not currently active — the claim is stale and the work is orphaned.**
Recommend: recover the archived ref's diff for review (`git diff dev refs/archive/worktree-cleanup-20260829/worktree/8d2ef784` etc.), decide if it's salvageable, then release the stale claim either way.

---

## 5. Stale-claim candidates

### 5.1 Synthetic `actor` string, no session record, no commits — strongest signal

Six v0.2.6 items carry an `actor` of the form `rescue-sweep-recovery-frontier(2)-<item-id>`.
This string is **not a session id** — I looked up every one of them in the 851-row
`list_sessions(status="all")` result and none exist as a session name or id. I also grepped
all 4,408 commits across every ref for each of these six item ids and found zero matches.

| Item | Title | `actor` | `claimed_at` |
|---|---|---|---|
| `84f77597` | FEAT: expose tenant-safe move_workspace_note_to_project ... | `rescue-sweep-recovery-frontier2-84f77597` | 2026-08-09 01:16:31 |
| `4544bbe5` | FEAT: add declarative document profiles for equations, captions, sections ... | `rescue-sweep-recovery-frontier2-4544bbe5` | 2026-08-09 01:16:26 |
| `d17a437a` | FEAT: add bounded cross-MCP batch research fanout for code, docs, outputs, diagnostics | `rescue-sweep-recovery-frontier2-d17a437a` | 2026-08-09 01:16:24 |
| `0f18eb77` | ROUND1-TESTS: build the contract and failure matrix for AI logs, exporters, timeouts | `rescue-sweep-recovery-frontier-0f18eb77` | 2026-08-08 23:39:19 |
| `efea329f` | CRITICAL: enforce hard project and tenant isolation across handoffs ... | `rescue-sweep-recovery-frontier2-efea329f` | 2026-08-09 01:16:22 |
| `f56bd1b8` | Batch exposure C — refresh hosted tools/list and run authenticated batch_read/batch_mutate smoke verification | `rescue-sweep-recovery-frontier2-f56bd1b8` | 2026-08-09 01:16:26 |

Five of the six were claimed within the same 9-second window (01:16:22–01:16:31), which is
itself evidence this was one batch/automated claiming event rather than five separate human
or executor decisions. It is now 20+ days later with no session record and no code trace for
any of the five. **Recommend: release these claims and return the items to unclaimed
`pending` (or `todo`) for a fresh claim** — do not treat any of them as "in progress" for
planning purposes.

### 5.2 Real session, real claim, no evidence of resulting work

| Item | Title | Actor / session | Session status |
|---|---|---|---|
| `b47a7456` | HARDEN: make code-intel cold-spawn tools/list readiness truthful, bounded, and recoverable | `31e79d59-...` ("meridian-docs-local") | archived, last_seen 2026-08-21 06:02 (35 min after claim) |
| `b1fee417` | W31-A: implement exact DOCX media-to-output-to-generator provenance binding and resolver receipts | `f398d1c9-...` ("exact-output-provenance-hotfix") | archived, last_seen 2026-08-11 22:46 (~50 min after claim) |

Both sessions are real (present in `list_sessions`) and both went `archived` shortly after
claiming, with no commit found under either item id anywhere in `git log --all`. Unlike
Section 4.2, there is no evidence any code was even committed for these two — the session
may have explored and abandoned, or worked in an uncommitted state that was lost.
**Recommend: release both claims as stale; no salvage action indicated (nothing to
recover).**

### 5.3 Correctly-tagged block — not a bug, confirmed working as intended

`0d74c8c8` (in_progress, "INVESTIGATE: reproduce and surface codebase-memory index worker
crashes...") already carries `blocker_kind = "manual"` and its session
(`997f376e-...`, "bounded-readiness-inspection") is archived, last_seen 2026-08-24 04:17,
~10 minutes after claim. The manual-block tag is consistent with the session's short life —
this looks like an investigation that correctly escalated to a human rather than silently
stalling. **No reclassification needed; this one is not stale, it's parked as designed.**

---

## 6. Superseded / duplicate candidates

### 6.1 Objectively tagged, confirmed as-is

Two pending items already carry `blocker_kind = "superseded"` in the live board data — I am
reporting this field, not re-deriving it:

- `96437ed7` — "Batch exposure B — promote the verified batch_read/batch_mutate lineage
  through dev to main and production" (`parent_id = 7ac7f633`).
- `0925b521` — "TEST: contract-test complete handoff delivery across modes, transports,
  tokens, truncation, and corrective continuation."

Both are consistent with a genuinely-superseded status (see 6.2 for `96437ed7`'s context);
I did not find a specific successor item id recorded for `0925b521` and am not inventing one.

### 6.2 The Batch-exposure family sits behind a real, unresolved, high-urgency HITL gate

`96437ed7` (superseded, above), `9fcb2cc9` ("Batch exposure D", unclaimed pending), and
`f56bd1b8` ("Batch exposure C", stale rescue-sweep claim per 5.1) are all children of parent
`7ac7f633`. Pulling the actual HITL request behind this (`get_hitl_request`,
id `04be5832-06e9-4a96-b2e5-b88c9d2e0436`, opened 2026-08-08 18:39:59, **still `status:
pending`, `urgency: high`, unanswered**):

> "Before anyone promotes item 7ac7f633 (batch_read/batch_mutate -> production) or otherwise
> merges dev to main right now: dev-branch CI ... has failed on its last 3 consecutive runs
> today ... Separately, tonight at 15:46 a sibling session (loop-goal-executor) caught and
> correctly refused a tampered /goal block (genuine token, edited body) that tried to push
> these exact same "Batch exposure A-D" / batch_read/batch_mutate-to-production items under a
> no_confirmation=true banner discouraging HITL escalation (task ae90c657)."

This is real, on-record evidence of a prior prompt-injection attempt against this exact item
family, and the resulting HITL gate has sat unanswered for three weeks. The underlying
`batch_read`/`batch_mutate` feature itself is confirmed merged and present in `dev`
(`meridian/batch_read.py`, `meridian/batch_mutate.py` exist; `git log` shows they landed via
unrelated items `133bfff6` and `77369699`) — what's blocked is specifically *promoting/exposing
it to production* under `7ac7f633`. **Recommend: leave `96437ed7`/`9fcb2cc9`/`f56bd1b8`
blocked, do not let any executor session claim or advance them, and surface HITL
`04be5832` to the human for an explicit answer** — it should not still be silently pending
three weeks later.

### 6.3 Naming collisions checked, not confirmed duplicates

Several items share a short label across *different* `item_group`s (e.g. two items each
labeled "R2-B": `ba0af0a4` under `meridian-docs-round2` vs. `d26b9943` under
`round2-ai-log-implementation`; two "R2-D": `ef8875a9` under `meridian-docs-round2` vs.
`2fa2fcad` under `round2-executor-reliability`; two "W2": `0d62f067` and `e03b41ef` both
under `be4ed581-prose-editing-path`). I checked titles and `touches_resources` for each pair
and they describe genuinely different work in different files — **these are coincidental
short-label reuse across separate tracks, not duplicates**, and I am not recommending any
merge/prune action on them.

---

## 7. Not independently verified in this pass

**30 of the 36 pending v0.2.6 items have never been claimed** (`actor` and `claimed_at` both
`null`). There is no claim to reconcile for these — they are ordinary queued backlog, listed
here only for completeness, not because their status is in doubt:

| id | title | item_group |
|---|---|---|
| 862f6522 | FIX: unify continuation resume with the canonical manifest and reject stale cross-version goals | handoff-lifecycle |
| 6587d613 | HARDEN: make canonical-root, worktree, ignored-path, and artifact-subtree identity explicit for every indexer and external tool | scoped-discovery-and-fallback |
| 6507e83a | C84-W3: harden DOCX leases, atomic package replacement, provenance identity, collision prevention, and crash recovery | c84-docx-transaction-hardening |
| cdd0ef6c | FEAT: cross-client session recovery registry with environment-bound resume and crash-safe sprint continuation | session-recovery-and-transport |
| 34f76536 | FIX: restore missing Claude Code stop hooks and make hook-path generation deterministic across Windows and POSIX | rescue-sweep-recovery |
| ba0af0a4 | IMPLEMENT DOCS-R2-B: make DOCX promotion and local-index evidence fail closed when receipts are missing or contradictory | meridian-docs-round2 |
| 07229675 | IMPLEMENT DOCS-R2-C: completion-integrity reconciliation for unresolved HITL, stale postconditions, missing receipts, and superseded work | meridian-docs-round2 |
| 5cc3d745 | W31-C: enforce complete promoted/held/skipped/ambiguous slot manifests before asset promotion | meridian-docs-outputs-w31 |
| dcf78192 | FOLLOW-UP: finish completion reliability after event-loop unblock with bounded critical-path recovery | completion-reliability |
| d004d5f3 | FINAL: triple-resolution integration gate for lineage, completion reliability, and frontier safety | triple-resolution-control-plane |
| 362ad382 | 75D-G FOLLOW-UP: add atomic resolve + fingerprint + provenance binding operation | proposal-75d-output-provenance-handoff |
| 3e36c301 | 75D-H TEST: integration matrix for Windows paths, planned outputs, fallbacks, mismatches, document-only work, and DOCX promotion | proposal-75d-output-provenance-handoff |
| c39a1bd3 | CONTROL-PLANE FIX: reconcile per-project execution posture and expose a durable autonomous-project setting | executor-critical-path-remediation |
| 9fcb2cc9 | Batch exposure D — independent release verifier, evidence receipt, and parent completion decision | (see 6.2) |
| d26b9943 | IMPLEMENT R2-B: ship exact-first scoped AI-log search and deterministic bounded indexing APIs | round2-ai-log-implementation |
| 2fa2fcad | IMPLEMENT R2-D: correlate run-owned sessions, agents, subprocesses, tunnels, worktrees, claims, and leases | round2-executor-reliability |
| 8c047a44 | IMPLEMENT DOCS-R2-E: make convergence, reindex, local-pointer, and manifest evidence explicit and generated | meridian-docs-round2 |
| 4d13cdd8 | GATE: reconcile scheduler, reservation, and lightweight-worker contracts then run the consolidated verification gate | executor-critical-path-remediation |
| 89d940d0 | W31-D: make partial Outputs indexing and unavailable code-intel fallback state explicit in promotion receipts | meridian-docs-outputs-w31 |
| db63385b | W31-B: add typography-only numeric invariants and fail-closed figure-slot comparison | meridian-docs-outputs-w31 |
| ff1843dc | API: expose proposal successor creation, typed relations, lineage queries, and handoff rendering | proposal-lineage |
| 39389897 | GATE: verify proposal lineage isolation, promotion, duplicate retries, and dynamic handoff delivery | proposal-lineage |
| c027922d | BUG: completing a sprint item cross-attributes a session's OTHER concurrently-claimed items' resources into the completing item's touches_resources | handoff-manifest-artifact-contract |
| 69e17837 | 75D-C FOLLOW-UP: keep planned_new pointers valid for planning but ineligible as completed provenance | proposal-75d-output-provenance-handoff |
| 949f64d4 | 75D-B FOLLOW-UP: expose one explicit meridian-outputs resolution state machine | proposal-75d-output-provenance-handoff |
| 4c992e91 | BE4ED581-W1: define reviewable prose-edit packets with anchor re-resolution and drift rejection | be4ed581-prose-editing-path |
| 0d62f067 | BE4ED581-W2: batch reviewable section moves and caption edits into one fail-closed writer transaction | be4ed581-prose-editing-path |
| e03b41ef | BE4ED581-W2: enforce a hard boundary between prose packets and typed OMML packets | be4ed581-prose-editing-path |
| 95589230 | IMPLEMENT R2-G: add optional OTel and self-hosted Langfuse interoperability without making external telemetry authoritative | round2-telemetry-optional |
| 4eedeef8 | RECONCILE: migrate and audit legacy proposal predecessor references without silent inference | proposal-lineage |

I did not attempt to guess a done/stale/duplicate classification for any of these 30 — none
of them have a claim, a matching commit, or any other objective signal to check against, so
"not independently verified" is the honest answer, not a placeholder for "probably fine."

---

## 8. Minimal keep/prune/reclassify plan (recommendation only — nothing executed)

| Item(s) | Current state | Recommended action | Evidence basis |
|---|---|---|---|
| `9154aa9a`, `f69b5f18` | `in_progress`, archived sessions | **Complete** — verify acceptance criteria against the merged code, then `complete_sprint_item` | §4.1: real merged commits + passing targeted tests |
| `ef8875a9`, `8d2ef784`, `2cf57fde`, `b71e0960` | `in_progress`, archived sessions, work orphaned | **Release claim; triage the archived ref before re-queuing** — `git diff dev refs/archive/worktree-cleanup-20260829/worktree/8d2ef784` and `.../worktree-agent-a7f5104cb61ac71af` to see if the work is salvageable; re-open as fresh `pending` either way | §4.2: real unmerged commits, features confirmed absent from `dev` |
| `84f77597`, `4544bbe5`, `d17a437a`, `0f18eb77`, `efea329f`, `f56bd1b8` | 4 pending + 2 in_progress, synthetic `rescue-sweep-recovery-frontier(2)` actor | **Release claim, return to unclaimed `pending`/`todo`** | §5.1: no session record, no commits, batch-claimed in a 9-second window 20+ days ago |
| `b47a7456`, `b1fee417` | `in_progress`, real archived sessions, no resulting code | **Release claim, return to unclaimed `pending`/`todo`** | §5.2: real session existed and died quickly; no salvage target |
| `0d74c8c8` | `in_progress`, `blocker_kind=manual` | **Keep as-is** | §5.3: correctly parked, not stale |
| `96437ed7`, `0925b521` | `pending`, `blocker_kind=superseded` | **Keep as-is** | §6.1: already objectively tagged |
| `9fcb2cc9` | `pending`, unclaimed, child of `7ac7f633` | **Keep blocked; do not let any session claim it until HITL `04be5832` is answered** | §6.2 |
| HITL `04be5832` | `pending`, `urgency: high`, unanswered since 2026-08-08 | **Escalate to Adam for an explicit answer** — it documents a real prior injection attempt and has been open for three weeks | §6.2 |
| Remaining 30 pending items (§7) | `pending`, never claimed | **No action** — ordinary backlog | §7 |

---

## 9. What this pass did and did not do

- Did: read-only Meridian MCP calls (`start_session` as planner role, `get_sprint_progress`,
  `get_sprint_items` ×2, `list_sessions`, `get_hitl_request`); read-only `git`
  (`log`, `show`, `branch`, `for-each-ref`, `merge-base`, `status`) inside the assigned
  worktree only; read-only `gh` (`run list`, `run view`, `api commits/.../status`,
  `api commits/...`); two scoped, read-only `pixi run pytest` invocations against existing
  test files (no test or source file was modified); wrote exactly one new file, this report.
- Did not: claim, complete, or otherwise modify any sprint item; edit any source, test, or
  config file; run the full test suite; commit, push, merge, tag, or deploy anything; touch
  `.env` or `meridian.toml`; run `hooks.ps1`/`hooks.sh`; run any destructive git command.
