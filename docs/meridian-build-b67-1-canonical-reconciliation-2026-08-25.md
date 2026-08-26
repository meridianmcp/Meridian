# B67-1: Canonical OOXML/OMML checkout, graph, and board reconciliation

**Date:** 2026-08-25
**Author:** session `4dfd2a59-bfd5-43e4-9e05-3ba60a78e140` (mde-megasprint-execution)
**Scope:** reconcile the post-handoff research submodule proposal (R-1..R-7),
the existing Track B / DOCX-integrity proposals, the live Meridian board, and
the canonical checkout. Planning/investigation only — no implementation.

## 1. Discrepancy matrix

This session performed the MDE/Track-B/Tigris megasprint referenced by the
board. Every "shipped" claim below is backed by a real commit on a named
branch, independently re-verified by a fresh Meridian session in every case
except where noted. **None of these branches have reached `dev` yet** — this
is the exact discrepancy B67-1's prospecting flagged (functions absent from
the canonical tree despite board records reporting them done).

| Claim (board item) | Board status | Commit / branch | Independent verification | Canonical (`dev`) evidence | Authoritative path | Follow-up |
|---|---|---|---|---|---|---|
| MDE-4 artifact registry (e1c979e3) | done | `c5af7210` → folded into `b7e9b172` on `mde-rework-44fc1ffe-536-2` | verified (`073e7c67`, PASS) | absent | branch above | land on `dev` |
| MDE-5 evidence envelopes (970c2a8a) | done | `a03a8af0` → `b7e9b172` | verified (`2b08d7a5`, PASS) | absent | branch above | land on `dev` |
| MDE-7 render receipts / `render_with_receipt` (1e6150ef) | done | `4f21a5e7` → `b7e9b172` | tests only (part of code-lane run) | absent | branch above | land on `dev` |
| MDE-8 batch transforms / `apply_batch_transform` (982f8564) | done | `554947bd` → `b7e9b172` | verified (`073e7c67`, PASS — flagged one commit-SHA citation error in the *first* verification pass, corrected in the second) | absent | branch above | land on `dev` |
| MDE-9 local lifecycle (c7ef8ff7) | done | `05642b5f` → `b7e9b172` | tests only | absent | branch above | land on `dev` |
| MDE-10 handoff reliability docs (0f5b2031) | done | `69dd3f4d` → `b7e9b172` | tests only | absent (doc-only; also see row below re: a *different*, uncommitted atomic-goal-payload code change with the same MDE-10 label) | branch above | land on `dev` |
| MDE-2 wrong-body resolution, rework (8982bdea) | done | `a9dc9bc6` → `b7e9b172` | verified (`7cbdc799`, PASS — first pass FAILed, second pass after rework PASSed) | absent | branch above | land on `dev` |
| MDE-3 release-transaction state machine, 3 passes (54619681) | done | `352e79f4` → `b7e9b172` | verified (`6babd2e2`, PASS — passes 1–2 FAILed on real gaps, pass 3 closed a genuine terminal-state correctness bug) | absent | branch above | land on `dev` |
| MDE-B1 `audit_equation_integrity` (3d0769ab) | done | part of `5629e3be` → `b7e9b172` | no independent verifier dispatched (require_verification=0 on this item) | **absent — confirmed by this session's own prospecting** | branch above | land on `dev`; consider a verification pass given no gate required one |
| MDE-B2 `compare_equation_structures`/`repair_equation_batch` (e4265dd1) | done | part of `5629e3be` → `b7e9b172` | none dispatched (require_verification=0) | **absent — confirmed** | branch above | same as above |
| `ec91e311` codeindex convergence fix | done | `f68e76ea` → `b7e9b172` | tests only | absent | branch above | land on `dev` |
| `455cfc36` bounded continuation handoffs | done | `a604ed92` → `b7e9b172` | tests only, re-verified this session (21/21, see below) | absent | branch above | land on `dev` |
| `aec043cb` handoff-scope leakage fix | done | `c620e554` → `b7e9b172` | re-verified **this session**, fresh: `tests/test_aec043cb_handoff_mode_scoping.py` 21/21 incl. explicit `mode="delta"` | absent | branch above | land on `dev` |
| `06bcaca2` (this sprint's P0 delta-leak report) | done (this session) | same as `aec043cb` — no new code | reproduced the live leak against the **deployed** MCP server (confirms it runs pre-fix `dev`), then disproved it as a code gap by running the real suite against the branch | absent (expected — same root cause) | branch above | land on `dev` — resolves automatically |
| `90f09d97` research-control-plane investigation | done | doc only, `wf_1570b112-581-1` | n/a (investigation) | doc not yet in `dev` | worktree above | land or copy the doc |
| `549e66c6` Tigris/S3 boundary investigation | done | doc only, `wf_1570b112-581-2` | n/a (investigation) | doc not yet in `dev` | worktree above | land or copy the doc |
| `1d34c076` Tigris/S3 inactive backend build | done | `0d401f92` on `worktree-wf_d6ecc180-c91-1` | tests only (54/54 new + 299/299 existing) | **absent — confirmed**, matches B67-1's own finding | branch above | land on `dev`, disjoint from the MDE branch |

**Root cause of every "absent from canonical" row:** all of this session's work
lives on two local branches (`mde-rework-44fc1ffe-536-2` tip `b7e9b172`, and
`worktree-wf_d6ecc180-c91-1` tip `0d401f92`), both diverged cleanly from `dev`
tip `12368304` (merge-base = `dev` tip exactly — `dev` has not moved). Landing
is blocked by a **git safety refusal, not a decision**: `dev` is checked out
in the main worktree, which has independent uncommitted state (next row),
so `git push . <branch>:dev` is correctly refused
(`refusing to update checked out branch`). This was not forced.

**The main-tree uncommitted diff is a separate, third thing** — not this
session's branches, not the dedicated `.codex/worktrees/mde-1-capability-handoff`
(confirmed still at `dev` tip with zero commits and zero uncommitted changes).
It is real, tested candidate work per the 2026-08-25 proposal's own §3
("Meridian repository changes now present in the working tree"):
`meridian/capability_contract.py:872-939` (unverified-capability fail-closed
state) and `meridian/handoff.py:1699-1800` (atomic executable-goal-payload
truncation fix — a *different* code path than this session's `455cfc36`
continuation-manifest-ordering fix, and not something this session's MDE-10
pass touched). It is preserved untouched, per hard rule, pending human
review/commit — it is candidate MDE-1 material, not proof of MDE-1 completion.

## 2. Product boundary

| Layer | Location | Status | Promotion gate |
|---|---|---|---|
| Thesis-local research code | `<external-research-workspace>/CURRENT_PROJECT_CODE/helpers/*` (outside this repo) | evidence/fixture only | must be extracted behind a typed `research_evidence` contract (R-1..R-3) before any Meridian product claim |
| Local fallback package | `CURRENT_PROJECT_CODE\tools\meridian_fallbacks\*` | reference implementation, explicitly non-product per its own integration note | each module needs an R-7 capability-doc entry naming its upstream Meridian replacement, or it stays local-only forever |
| Meridian Docs (product, this session) | `extensions/meridian-docs/meridian_docs/{docs_intel,render_gate}.py` | equation-integrity work (B1/B2) is real, committed-to-branch, unlanded product code | land the branch; no further extraction needed |
| Meridian Docs — OOXML package hardening (product, main tree) | `extensions/meridian-docs/meridian_docs/ooxml_integrity.py` + `tests/test_ooxml_integrity_contract.py` | **CORRECTED (2026-08-25, post-verification):** this file has never been committed to any ref in this repository — `git log --all -- .../ooxml_integrity.py` returns zero commits on every local and remote-tracking branch. It exists only as an untracked (`??`) file in the main tree's working directory, same bucket as row 3 below, not the branch bucket above. The `mde-rework-44fc1ffe-536-2` branch's `docs_intel.py` mentions it only in a prose comment; the real `from . import ooxml_integrity, render_gate` wiring is itself part of the main tree's uncommitted diff. | needs its own review/commit, same as the row below — "land the branch" does not apply, no branch contains it |
| Meridian Outputs (product) | `extensions/meridian-outputs/meridian_outputs/*` | MDE-4/5 artifact registry + evidence envelopes, unlanded product code | land the branch |
| Core handoff/capability (product) | `meridian/{handoff,capability_contract,mcp_tools}.py` | two independent unlanded fixes (`455cfc36`, `aec043cb`) + one uncommitted candidate (main tree, capability_contract.py unverified-state) | land the two branches; main-tree candidate needs human review + its own commit, not silent absorption into either branch |

## 3. R-1..R-7 proposal-to-sprint map

| Proposal item | Existing sprint mapping | Gap after this session's work lands | Notes |
|---|---|---|---|
| R-1 `ResearchEvidenceRun` | Partially covered by MDE-5's evidence-envelope work (`970c2a8a`, done) | Envelope model exists; the *run*-level (reproducible/diagnostic/archival/held/unverified) status field does not | New follow-on item once MDE-5 lands |
| R-2 typed correspondence/failure attribution | Not covered by any current sprint item | Fully open — this is thesis-local (`helpers/metrics.py`), no Meridian product item exists | Needs a new item; explicitly **not** authorized to reactivate without Adam's approval per this item's own constraints |
| R-3 shared aggregation/paired-comparison contract | Not covered | Fully open, same as R-2 | Same — do not create without approval |
| R-4 research artifact registry | Substantially covered by MDE-4 (`e1c979e3`, done, unlanded) | Once landed, check whether MDE-4's registry already satisfies "current-artifact lookup rejects duplicate live slots / drifted provenance" | Verification task, not new implementation |
| R-5 DOCX release transaction | Covered by MDE-3 (`54619681`, done after 3 passes, unlanded) — this **is** the Track B proposal's detailed plan, already implemented | Once landed: MDE-3's own pass-3 verifier confirmed all 5 acceptance bullets met | No new item needed |
| R-6 batch execution/render barriers | Covered by MDE-8 (`982f8564`, done, unlanded) + MDE-7 (`1e6150ef`, done, unlanded) | Once landed, re-check against R-6's specific wording | Verification task |
| R-7 capability/fallback synchronization | Not covered | Fully open — no capability-doc generation from the live manifest exists yet | Needs a new item |

## 4. Local-vs-tunnel-vs-hosted capability matrix

Already produced in full, this session, as part of item `90f09d97`'s own
deliverable: `docs/meridian-research-control-plane-investigation-2026-08-25.md`
(12-row matrix covering paper_search, github_search, web/archive capture,
codebase-memory/Serena, CodeIndex/BM25, meridian-docs/Word COM,
meridian-outputs, Zotero, local filesystem, Neon/Postgres, Redis, and object
storage). Not duplicated here — see that report for the authoritative
version, including the live-tested finding that `github_search` is **not**
actually keyless despite its own docstring (401/403 on live calls this
session).

## 5. Planner continuation

**Immediate, safe, no-decision-needed:** land `mde-rework-44fc1ffe-536-2`
(tip `b7e9b172`) and `worktree-wf_d6ecc180-c91-1` (tip `0d401f92`) onto `dev`
once the main tree's dev checkout is clean. This alone resolves the "absent
from canonical" row for every item above except R-2/R-3 (genuinely unbuilt)
and R-7 (genuinely unbuilt) and the main-tree candidate diff (needs human
review, separate from these two branches).

**Needs a human decision:** what to do with the main-tree uncommitted diff
(capability_contract.py unverified-state, handoff.py atomic-goal-payload
fix, docs_intel.py namespace-safe serializer wiring, pixi/pyproject
dependency additions). Options: (a) review and commit it as its own change
before/after the two branches land, (b) have a fresh MDE-1 pass (819ac6de)
re-derive equivalent work in an isolated worktree per that item's own
explicit "do not treat old state as proof of completion" instruction, or
(c) some combination. This report does not recommend one — it is a genuine
open question the proposal author flagged, not an implementation detail.

**New items to open (not opened by this planning-only pass):** R-2, R-3,
R-7 as scoped above. B67-1 explicitly reserves item creation for after this
discrepancy table is complete — that condition is now met.
