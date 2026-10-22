# Session recovery evaluation protocol — 2026-10-02

**Status:** pre-registered design; no empirical results
**Scope:** internal, controlled evaluation of Meridian's local-first session recovery workflow
**Freeze point:** before recovery implementation items are declared stable

## Purpose and limits

This protocol defines how to evaluate three different situations: routine progress checkpoints, continuation while the provider conversation is still available, and reconstruction after the originating session is dead or unavailable. It distinguishes native resume from reconstruction in a new session.

The study will measure task-state fidelity, goal and decision fidelity, artifact recovery, time to a verified useful action, and false reconstruction. It will not use real customer conversations, claim universal provider coverage, or treat an export as a resumable session. No measurements or efficacy claims are made by this document.

A test episode is a scripted task with a frozen ground-truth manifest. Run it in a disposable local project copy with synthetic conversation records and synthetic artifacts. Do not use actual user transcripts, credentials, home-directory paths, or private repository contents.

## Recovery levels

| Level | Incident presented to the operator | Correct recovery class |
|---|---|---|
| L0 — routine checkpoint | The same session is still running. A planned checkpoint boundary or ordinary pause occurs; the next action must use the saved progress state. | Continue the live session. A checkpoint is progress context, not proof that the process died. |
| L1 — live continuation | The conversation remains available under its provider-owned session/thread identity, including after compaction or reconnect. | Use the provider's supported native continuation when available; reconcile its context with the current Meridian board and current local state. |
| L2 — dead-session reconstruction | The originating process/session cannot be resumed, or its context is unavailable. A new session must recover only from evidence that remains accessible. | Reconstruct a bounded continuation. Label it reconstructed, expose missing sources, and re-derive live task state. |

A self-reported crash or graceful end is not by itself evidence that the work is lost. The test must separately score whether the recovery flow correctly identifies the process as unavailable and whether it recovers the work from the board and other sources. Unknown or unparsable liveness is scored as unknown, not guessed live or dead.

## Sources and authority

For each episode, freeze an expected-source manifest before running any condition. It records source availability, identity, freshness, and expected coverage.

- **Meridian state:** project goal/scope, sprint version and live item statuses, decisions/notes, session recovery record, active file claims, and checkpoint/handoff references. The live board is authoritative for current item status. A previously stored /goal body is never replayed as current board truth.
- **Manual or semi-automatic checkpoints:** progress snapshots and explicit decisions written at the checkpoint. Score only statements supported by the checkpoint or another listed source.
- **Conversation history:** the provider's currently available conversation/thread, when it still exists and the evaluator's assigned condition permits access.
- **Host-native history or resume:** provider-supported CLI, app, SDK, or protocol. Record provider, surface, client version, source ID (in the local fixture only), and whether the native operation actually resumed the same session.
- **Artifact pointers and stores:** typed pointers, local content-addressed artifacts, output manifests, and explicitly configured external stores. A path or pointer is not recovered content: verify the target and its hash where available.
- **Git/worktree:** include the frozen repository HEAD, dirty-file manifest, and worktree identity only in episodes where such a repository/worktree exists. Make the same Git/worktree snapshot available to every comparison arm for that episode; report its presence as a stratum. Chat-only episodes have no repository and must not be marked as failures for lacking one.

Authority rules: the live Meridian board wins for sprint status; current verified filesystem/Git state wins for current worktree facts; a provider record establishes only what its own supported interface can read or resume; a verified artifact establishes bytes and integrity, not the intent behind them. Stale, missing, ambiguous, or unsupported sources remain explicitly so. Never infer an exact event range from a coarse timestamp or claim an artifact was restored when only its pointer resolved.

## Study design

### L0 and L1 checks

Run four scripted templates at each level, with three repeated checkpoint/continuation events per template (12 events per level). Keep task, state transition, and timing identical across repetitions while rotating the event order.

- For L0, score what is captured at each scheduled checkpoint and what the still-live session can correctly carry into its next action. Record checkpoint latency and any lost or stale state.
- For L1, attempt the provider's documented continuation path using the same provider-owned session identity. Compare a native-only continuation with a continuation that also consults Meridian state. Confirm whether it resumed the same session; a new session is reconstruction and must be scored under L2 semantics.

These are protocol/acceptance checks; do not pool their scores with L2's evidence-bundle comparison.

### L2 evidence-bundle comparison

Use four frozen scenario templates, each run once under each of these three conditions (12 runs per supported provider/client-version stratum):

1. **M:** Meridian recovery state only.
2. **M+H:** Meridian plus readable, remaining provider-native conversation history. For L2, use a documented read/history interface; native resume is not an option because the scenario stipulates that the originating session cannot be resumed.
3. **M+H+P:** M+H plus pointer resolution and eligible artifact lookup/verification.

All three arms receive the same frozen Git/worktree snapshot when the scenario has one. Treat it as common context, not an added condition: the M arm is Meridian state plus that identical snapshot when present. Randomize the order of the three conditions within each template. Reset the local fixture between runs; no operator, model, or recovery pack may carry facts learned in a prior condition into a later condition. Use the same model/client configuration, tool permissions, time limit, and recovery instructions within a stratum. If an implementation supports more than one provider, report each provider/version stratum separately; do not pool it into a universal provider claim.

Use synthetic fixtures that cover: (a) a clean handoff with pending and in-progress work, (b) a crash with dirty but hashable files, (c) unavailable or stale conversation history with a valid artifact pointer, and (d) chat-only work with no repository plus a missing or ambiguous external pointer. Across these cases, include at least one completed item, one changed constraint, one rejected approach, one decision with rationale, one stale source, and one genuinely unrecoverable source. The evaluator's manifest defines the expected facts and file hashes.

The four-template set is a bounded internal evaluation, not a population sample. Report per-template results and paired differences; do not use the small sample to claim general effectiveness across users or providers.

## Measures and scoring

At incident presentation, start a monotonic timer. Stop it at the first useful action that is both safe and independently verified against the frozen manifest (for example, reading the correct next pending item or validating the expected artifact hash). Cap each run at 20 minutes; a timeout is a failure, not a censored success.

For every run, record:

- **Goal/scope fidelity:** exact objective, project/subproject identity, scope boundaries, and explicit constraints recovered. Score each pre-labelled critical fact as correct, omitted, or wrong.
- **Task-state fidelity:** current step, pending/in-progress/done statuses, active claims, decisions and rationale, rejected approaches, and next action. Use a fact-level rubric with source references; report omissions separately from incorrect claims.
- **Insight/decision fidelity:** whether each manifest decision and its reason is recovered with the right scope and without inventing a stronger commitment.
- **Artifact recovery completeness:** count of expected artifacts marked present and accessible in the frozen manifest whose bytes were actually retrieved and hash-verified, divided by all such eligible artifacts. Score manifest-marked missing, stale, ambiguous, or pointer-only targets separately for correct identification.
- **Time to resume:** elapsed time to the first verified useful action, plus timeout count. Report median and range per condition and level.
- **False reconstruction rate:** unsupported or contradicted facts asserted as recovered divided by all asserted recovery facts. Also report the raw numerator and denominator; use N/A when no recovery facts were asserted.
- **Safety errors:** wrong project/session/worktree selection, a stale item presented as live, an already-completed action repeated, a fabricated exact source range, unverified bytes described as restored, or any destructive write. Count these individually; any destructive write or cross-project/source leak is an immediate protocol failure.

Two blinded reviewers independently score critical facts and safety errors from the run log and ground-truth manifest. Resolve disagreements by recording both original scores and a short adjudication. Preserve the source citation for each scored assertion. The run log contains synthetic IDs only.

## Unrecoverable versus best effort

Classify a fact or artifact as **unrecoverable** only when every authoritative source named in the frozen manifest is absent, inaccessible, deleted, invalid, or too ambiguous to resolve. The correct output is an explicit gap with the best available locator and reason; it is not a guessed reconstruction.

Classify a result as **best-effort continuation** when enough verified evidence supports a safe next step but one or more non-critical facts or artifacts remain unavailable. State the uncertainty and do not imply native resume, complete replay, or complete artifact restoration. A missing provider history is not fatal when Meridian and verified artifacts independently support the next step.

Exclude a run only for a fixture/harness defect discovered before its randomized condition is revealed. After condition reveal, count crashes, tool failures, missing data, and timeouts as outcomes. Report all exclusions and reruns.

## Predefined interpretation and release gates

Compare M+H and M+H+P with M using paired per-template differences for critical-fact accuracy, total task-state fidelity, artifact completeness, and time to resume. Report raw counts, medians, ranges, and a 95% paired bootstrap interval as descriptive uncertainty; with four templates, make no significance or generalization claim.

Recovery is **not ready for release** if any run leaks real/provider content to hosted Meridian, writes to the wrong project or worktree, repeats a completed action, performs an unplanned destructive write, or falsely claims an artifact is verified. It also fails the functional gate if any critical goal/scope/project identity fact is wrong or if a source marked unavailable is presented as verified. Report ordinary omissions and all other metric results even when a gate fails. A failure triggers remediation and a new, separately versioned rerun; do not replace or silently re-score the failed run.

## Freeze, artifacts, and reporting

Before the first scored run, record the release commit, dirty-state hash, provider/client and model versions, adapter versions, operating system, configured roots, exact recovery instructions, timeout, scenario manifests, and the hashes of all synthetic source fixtures. Freeze this protocol and the scoring rubric. A feasibility pilot may validate only the harness and must not alter outcomes, thresholds, or the scored scenario set after condition reveal.

Store raw run logs, synthetic conversations, manifests, and generated packs locally in a disposable evaluation directory. Review and redact before committing any report; commit only aggregate counts, sanitized scenario descriptions, version identifiers, and reproducibility instructions. Preserve failed runs. A separate empirical paper/workstream may publish measured results after implementation stabilizes. The Meridian Core design paper may cite that later work but must make no unmeasured efficacy claim.

The evaluation is gated on the recovery implementation being declared stable, including the protocol's relevant checkpoint, provider-history, pointer/artifact, and live-board surfaces. The protocol does not authorize collecting or uploading real conversations.

## Related discovery

The provider/source boundaries and local-only context-pack proposal are documented in [provider-native session recovery discovery](provider-native-session-recovery-contract-2026-10-02.md). This protocol measures later behavior; it does not convert that discovery into an efficacy result.
