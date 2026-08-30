# AI-Log / Resource-Pressure Contract Matrix — Consolidation for Sprint Item 0f18eb77

**Type:** Planning/consolidation only. No production code was edited, no test file
was added or modified, and this item was not promoted beyond
planning/verification-only scope. The only file this pass writes is this document.

**Repo state at time of this pass:** worktree `.claude/worktrees/a0c162f5`, branch
`worktree/0f18eb77`, based on `dev` tip `985485da`. Working tree clean before and
after, aside from this new file.

**Item:** `0f18eb77` — "ROUND1-TESTS: build the contract and failure matrix for AI
logs, exporters, timeouts" (child of proposal `e143949d`).

**Declared `touches_resources` for this item:** `tests/test_ai_log_contract.py`,
`tests/test_ai_log_capture_boundaries.py`, `tests/test_ai_log_export.py`,
`tests/test_sprint_item_completion_resource_pressure.py`,
`tests/test_run_owned_resource_correlation.py`.

---

## 1. Why this deliverable is a document, not four new test files

The item's own instructions require picking one of two paths: (a) write real tests
against real existing behavior for the four missing declared files, or (b) if the
underlying proposal describes unbuilt features, write a specification document
instead. Direct verification in this pass (Section 2) found the actual situation is
neither cleanly (a) nor (b) — it is a third case the task anticipated and told me to
resolve on the evidence:

- **Three of the four missing files** (`test_ai_log_capture_boundaries.py`,
  `test_ai_log_export.py`, `test_sprint_item_completion_resource_pressure.py`) target
  areas that already have real, current, passing test coverage — written under
  different filenames by the Round 2 implementation tranche that this Round-1 item's
  own design work fanned into. Writing new files under the originally-declared names
  now would duplicate that coverage, not close a gap.
- **A fifth file that already exists**, `tests/test_ai_log_contract_matrix.py`
  (shipped by item `e0b88967`, tranche "R2-H"), already **is** the consolidated
  cross-cutting contract/failure matrix this item's own notes describe as the
  end goal ("after Wave 1 reports, consolidate the contract/failure matrix and
  acceptance tests").
- **Only the fourth missing file**, `tests/test_run_owned_resource_correlation.py`,
  targets a genuine gap — and it is a gap in the underlying *feature*, not just in
  test coverage: the correlation primitives (run-owned session/agent/subprocess/
  tunnel/worktree/claim/lease linkage) do not exist in production code yet. Writing
  a test for it would mean testing vaporware, which the item's own hard constraints
  forbid.

Given that mix, the evidence-grounded action — consistent with "planning/
verification-only until explicit implementation promotion" — is this document:
a real pointer/cross-reference consolidation for the shipped areas, plus a real
specification (not tests, not code) for the one area that is genuinely still
unbuilt. No new test authorship, zero new production or test code, exactly as the
item requires.

---

## 2. Verified disposition of each originally-declared file

All four claims below were checked directly against the code in this worktree in
this pass (file listing, `grep`, and a serial pytest run — Section 5), not merely
carried over from the discovery note that scoped this item.

| Declared file (does not exist) | Verified real coverage today | Shipped by | Verified in this pass |
|---|---|---|---|
| `tests/test_ai_log_capture_boundaries.py` | `tests/test_ai_log_capture.py` — capture-layer boundary helpers (`capture_session_started/_ended`, `capture_tool_invoked/_completed`, `capture_process_registered/_released`), never-raising degrade contract, real `/mcp` dispatch wiring | `c5c3fc5f` (R2-A) | File exists; its own docstring states this exact scope; included in the 203-test run below |
| `tests/test_ai_log_export.py` | `tests/test_ai_log_artifacts.py` (export/purge MCP surface: `export_ai_log`, `export_ai_log_artifacts`, `purge_ai_log`, `artifact_store.export_artifacts`) + `tests/test_ai_log_retention.py` (`db.ai_log.export_events`, retention purge, redaction-on-write) | `c0168425` (R2-C) design predecessor `ea972129` | Both files exist; docstrings cross-reference each other to avoid duplication; included in the 203-test run below |
| `tests/test_sprint_item_completion_resource_pressure.py` | `tests/test_process_budget.py` (host memory/CPU budget monitor, quiesce/kill escalation, backoff) + `tests/test_complete_sprint_item.py` (resource-aware retry-after diagnostics via `process_budget.sample_server_process`, preserved verifier evidence across an interrupted completion) | `9c8336c4` (budget monitor) / `394bcbdf` (R2-E) | Both files exist; included in the 203-test run below |
| `tests/test_run_owned_resource_correlation.py` | **No equivalent exists under any name.** Confirmed directly: `meridian/process_registry.py`'s `WorkerLease` (line 171) has fields `run_id, client, pid, executable, cwd, cmdline, create_time, group_id, job_id, shared_runtime` and a logical-owner-identity field added under `39c8cf2c` — no `owner_session_id` or `owner_worktree_id`. `task_log.parent_session_id` exists in the schema (`meridian/pg_adapter.py:692`, SQLite migration `_migrate_parent_session_id` in `meridian/db/migrations.py:592`) and is **written** by `log_task`/`enqueue.py` but never appears in a `SELECT`, filter, or read-side helper anywhere in `meridian/` — grepped the whole package, zero read-side hits. No `build_run_inventory` or `list_child_sessions` function exists anywhere in the codebase. | Not shipped. Twin implementation item `2fa2fcad` ("R2-D") is still `pending`; its design predecessor `c978dc31` is `done` as a **design-only** item ("no code written") per its own notes. | Confirmed absent by direct grep/read in this pass, not inferred |

Additionally, the consolidated cross-cutting gate this item was tasked to eventually
produce ("consolidate the contract/failure matrix and acceptance tests") already
exists and already covers the eight axes proposal `e143949d`'s track 9 called out
(local-only, Redis available/unavailable, OTel/Langfuse available/unavailable,
redaction, timeout/recovery, resume/corrective handoff, project isolation, exact
deployment readiness):

- **`tests/test_ai_log_contract_matrix.py`** (item `e0b88967`, tranche "R2-H"),
  read directly in this pass. Its own docstring states it is the integration gate
  across `9e83be4a` / `ea972129` / `c5c3fc5f` / `79491e26` / `c0168425`, explicitly
  does not duplicate any of those items' own unit coverage, and documents a real
  gap it found and fixed in the same item (a corrective-handoff trace missing from
  the MCP dispatch path vs. the REST path) — i.e. it already functions as exactly
  the "contract and failure matrix" deliverable this item's title describes.

`tests/test_ai_log_contract.py` itself (the one declared file that does exist) was
left untouched, per the hard constraint to confirm zero regression against it. Its
own docstring still names this item (`0f18eb77`) as the owner of capture-boundary
and export coverage — that pointer is now stale (superseded by the files in the
table above) but updating another item's test file is out of scope for a
verification-only item on `0f18eb77`; it is noted here rather than edited.

---

## 3. The one genuine gap: run-owned resource correlation — specification for future implementation

This section is a **specification**, not a test file and not code, per the item's
hard constraint that unbuilt features must not be tested as if they exist. It lays
out what a future implementation of `2fa2fcad` ("R2-D: correlate run-owned
sessions, agents, subprocesses, tunnels, worktrees, claims, and leases") would need
to satisfy, so that whoever eventually implements it — and whoever writes
`tests/test_run_owned_resource_correlation.py` against the real implementation —
has a concrete contract to build and test against instead of starting from
proposal prose alone.

### 3.1 What "a run" needs to mean

Today, the closest thing to a "run" identifier is `process_registry.WorkerLease.run_id`
— but a lease is scoped to one external process, not to the full set of resources one
logical unit of work touches. A run-owned correlation layer needs a single identifier
that can be resolved to every one of:

- the Meridian **session** that initiated the work (`sessions.id`)
- any **child/subprocess sessions** it spawned (`task_log.parent_session_id` — written
  today, never read back; see Section 2)
- any **worker leases** it holds or held (`process_registry.WorkerLease`, keyed by
  `run_id`, `pid`, `create_time`)
- the **tunnel** connection(s) it used, if any (tunnel slot/connector identity —
  not currently linked to `run_id` or `session_id` anywhere in `meridian/`)
- the **worktree** it executed in (branch/path — currently only inferable from a
  session's `name` string by convention, e.g. `ctrlwave-<item>-<item>`, per the
  board-reconciliation report's Section 4.2 findings; not a structured field)
- any **file/symbol claims** it took out (`claim_file`/`release_file` rows) and any
  **sprint-item claims** it holds

### 3.2 Minimum data-model gaps (naming only — not a schema to implement here)

- `process_registry.WorkerLease` would need an owning-session reference
  (`owner_session_id`) and, if worktree-level attribution matters,
  `owner_worktree_id` — neither exists today.
- `task_log.parent_session_id` would need at least one read-side accessor
  (e.g. a `list_child_task_log_by_parent_session` style function) before any
  correlation query can use it — today the column is insert-only.
- A run inventory / lookup surface (the proposal's own language: something like
  `build_run_inventory` or `list_child_sessions`) would need to exist to fan a
  single `run_id` or `session_id` out to the resource types in 3.1. No such
  function exists anywhere in the codebase today (confirmed by direct search,
  Section 2).
- Tunnel-connector identity and worktree identity are not currently modeled as
  foreign keys against sessions/runs anywhere; either would need to be added, or
  correlation would need to stay convention-based (session-name parsing), which
  is fragile and already flagged as a manual-inference weak point elsewhere in
  this repo's own board reconciliation work.

### 3.3 Failure/contract axes a real `test_run_owned_resource_correlation.py` should cover once built

These mirror the axes proposal `e143949d` track 9 calls out generically, applied
specifically to run-owned resource correlation:

1. **Basic fan-out correctness** — given a `run_id`/`session_id`, the inventory
   returns exactly the sessions, leases, claims, and task_log rows that actually
   belong to that run, and none that belong to a sibling run in the same project.
2. **Cross-project isolation** — a run inventory query scoped to project A must
   never surface a resource owned by project B, even if IDs collide in shape.
3. **Orphaned lease survives its owning session** — a `WorkerLease` whose owning
   session has ended/archived must still resolve to that session's identity for
   historical correlation (not silently become unattributed), while a *live*
   query for "current owner" correctly reports the session as gone.
4. **Duplicate `run_id` across worktrees** — two worktrees that happen to reuse a
   `run_id` (e.g. via the shared-object-store cherry-pick pattern documented
   elsewhere in this repo's parallel-worktree tooling) must not be merged into one
   inventory; worktree identity must disambiguate them.
5. **Mid-correlation crash / partial write** — if the correlation write path spans
   more than one table (e.g. tagging a lease with `owner_session_id` and recording
   a task_log row), a crash between the two must leave the system in a state the
   read side can still reason about (either both-or-neither visible, not a
   half-linked resource silently misattributed).
6. **Tunnel restart re-parenting** — if a tunnel connection drops and reconnects
   mid-run, the correlation layer must decide (and this must be tested) whether
   the reconnected tunnel counts as the same run or a new one, and must not
   silently attribute post-restart activity to a run that already ended.
7. **Claim/lease TTL expiry racing a correlation read** — a claim or lease that
   auto-expires (2h TTL, per this repo's file-claim contract) while a correlation
   query is in flight must not produce an inconsistent read (e.g. reporting a
   claim as both held and expired in the same response).
8. **Session-name-convention fallback is a documented weak point, not a supported
   contract** — if any interim implementation leans on parsing session names
   (as today's ad-hoc worktree attribution does), that must be explicitly called
   out as a fallback with a real fully-qualified field as the intended replacement,
   not left as the permanent mechanism.

This specification intentionally stops at contract/failure-axis level. It does not
propose a schema migration, function signature, or any other production change —
that implementation work belongs to `2fa2fcad`, not to this verification-only item.

---

## 4. Board-hygiene note (separate finding, not part of the technical scoping above)

Independently of the technical analysis above, `docs/meridian-build-board-reconciliation-v0.md`
§5.1 (already merged to `dev` at commit `1b257dee`) flags this item's own current
claim as one of six synthetic stale claims: actor
`rescue-sweep-recovery-frontier-0f18eb77`, claimed 2026-08-08 23:39:19, with no
matching row in `list_sessions` and zero commits anywhere under this item's id in
`git log --all`. That report's recommendation is to release the claim back to
unclaimed `pending`. This is a board-hygiene signal, independent of and consistent
with (not contradicted by) the scoping conclusion in Sections 1–3 above, and is
relayed here for whoever owns board hygiene to action — it is not something this
verification-only pass acted on itself.

---

## 5. What this pass did and did not do

**Did:**

- Read, in full or in relevant part, all six existing test files in this area
  (`tests/test_ai_log_contract.py`, `tests/test_ai_log_capture.py`,
  `tests/test_ai_log_artifacts.py`, `tests/test_ai_log_retention.py`,
  `tests/test_process_budget.py`, `tests/test_complete_sprint_item.py`,
  `tests/test_ai_log_contract_matrix.py`, `tests/test_ai_log_timeline.py`) and
  confirmed their stated scope against their own docstrings.
- Directly verified (grep + read, not assumption) that
  `tests/test_ai_log_capture_boundaries.py`, `tests/test_ai_log_export.py`,
  `tests/test_sprint_item_completion_resource_pressure.py`, and
  `tests/test_run_owned_resource_correlation.py` do not exist anywhere in this
  worktree.
- Directly verified `process_registry.WorkerLease`'s real field list and confirmed
  no `owner_session_id`/`owner_worktree_id` fields exist.
- Directly verified `task_log.parent_session_id` is written (migration + insert
  call sites) but never read anywhere in `meridian/` (whole-package grep, zero
  read-side hits).
- Directly verified no `build_run_inventory` or `list_child_sessions` function
  exists anywhere in the codebase.
- Ran the full existing test surface for this area serially, not in parallel:
  ```
  pixi run python -m pytest tests/test_ai_log_contract.py tests/test_ai_log_capture.py \
    tests/test_ai_log_artifacts.py tests/test_ai_log_retention.py \
    tests/test_process_budget.py tests/test_complete_sprint_item.py \
    tests/test_ai_log_contract_matrix.py tests/test_ai_log_timeline.py \
    -p no:xdist -q --timeout=60
  ```
  Result: **203 passed in 12.70s**, zero failures, zero regressions — including the
  pre-existing `tests/test_ai_log_contract.py`.
- Wrote exactly one new file: this document.

**Did not:**

- Write, modify, or delete any production code file.
- Write, modify, or delete any test file (new or existing).
- Promote this item's scope to implementation.
- Claim, complete, or otherwise modify any sprint item, including `0f18eb77`
  itself.
- Run the full/parallel test suite (`pixi run test -n 3`) — only the scoped,
  serial, focused run above, as instructed.
- Touch `.env` or `meridian.toml`.
