# CATALYST 03b — handoff and exploration lifecycle

Reviewed 2026-10-05 against the Meridian `origin/dev` base `f0c798ee`. This is a source-level contract review, not a product change. The companion [application evidence ledger](claim-evidence-ledger.md) records broader claim and production limits.

## Lifecycle contracts

| Stage | Shipped today | Gap / proposed contract |
|---|---|---|
| Planner evacuation after lost, exhausted, or unsafe context | Handoff modes, durable project/session/task/decision records, and a checkpoint path can supply recovery inputs. | There is no single compact evacuation record that reconciles these sources, labels each fact with freshness/confidence, and lists unknowns. The planner must reconstruct from source records. Add a short packet of references and verified state; do not copy the full project history into every restart. |
| Executor returns to planner | Sprint-item status and task logs are durable; HITL is a separate request/answer flow. An executor-report schema exists and planner reads can surface its rows. | No generic report writer currently closes completed, partial, and blocked work into one planner-facing return. Reuse the schema where possible; add a small, validated writer and explicit outcome type. Keep item status authoritative. |
| Bounded exploration | Scratch research runs support project/session scoping, read-only or isolated-write modes, allowed paths, declared turn budget, expiry, receipt, and explicit keep/discard/promote disposition. Projects can have one-level parent links. | The scratch-run budget is stored but hard per-turn metering is unverified; runs lack a question/scope field and direct parent-request/item link. Promoting a run saves a finding, not a sprint item. Keep planner review and sprint-item creation explicit. |

## Handoff mode map

| Mode / path | Role and persistence | Correct use |
|---|---|---|
| `full` | Explicit archival/diagnostic narrative; only mode that adds workspace-wide decisions and notes. Uses the retrievable handoff path. | Rare audit or deep planning context. Its cross-project content makes it a poor routine resume packet. |
| `delta` | Per-session continuation with recent completions, active/pending items, and a continuation manifest. Uses the retrievable handoff path. | Continue a known session. It is narrower than `full`, but still a rendered handoff, not a typed evacuation record. |
| `goal` | Executor-facing `/goal` instructions; uses the trusted pending-goal/retrieval path. | Start or resume scoped execution. It carries directives and item context, not a planner's reconciled recovery assessment. |
| `continue` resume payload | Separate compact executor continuation path; returns session, live scoped pending items, and a ready-to-paste goal while skipping heavy L0/L1/L2 orientation. | Useful when the executor just needs the next assignment. It is not a planner evacuation packet or a handoff mode. |
| `starter` / `compact` | Short orientation and pending-item preview; ephemeral render intended for copying. | First-open orientation. It is not durable recovery state. |
| `planner` | Planner prompt and review scaffold; ephemeral render intended for copying. | Start a planning conversation. The name describes audience, but the render does not itself evacuate/rebuild the durable state. |
| `checkpoint` | Tool path, not another mode: captures session state, calls `delta`, stores a dashboard snapshot, and returns a bounded summary/next goal. | Fast checkpoint during work. The byte bound applies to the returned summary; do not assume it bounds the persisted delta. |
| compact refresh hook | Session-start reminder after compaction, not a handoff mode or project-state snapshot. | Re-orient the host to fetch current project context. |

The public resolver prioritizes an explicit mode, then resumed-session `delta`, planner-role `planner`, and executor/unknown intent `goal`; it does not choose `full` implicitly. The Python-level `generate_handoff` default (`mode=None`) goes through the same resolver, so a caller that bypasses the transports and omits a mode does not get `full` either; internal callers pass an explicit bounded mode (`delta` for the session-close auto-save and the idle-expire loop, `goal` for proposal promotion). The `retrievable_via_load_handoff` flag means the mode uses a retrievable path; it does not prove an individual persistence write succeeded, since those writes are best-effort. See [mode resolution](../../meridian/handoff.py#L6387), [retrieval/persistence contract](../../meridian/handoff.py#L102), [planner and starter modes](../../meridian/handoff.py#L12571), and [checkpoint handler](../../meridian/mcp/handlers/session_tools.py#L27).

## Proposed minimal return shapes

Keep these as distinct records. An executor return answers “what happened to this assignment?” An evacuation packet answers “what state is safe for a replacement or resumed planner to trust?”

**Executor return — one screen, with stable pointers:** outcome (`completed`, `partial`, or `blocked`); project/session/version and sprint-item IDs; source handoff ID; completed and remaining work; evidence references (commit, test result, artifact ID/path) with verified/unverified status; blocker or decision needed; and one recommended next planner action. Completed work still uses the normal sprint completion path. Partial work remains open with its latest evidence. A genuine blocker links to its HITL request. Avoid pasting a second full handoff into the report.

**Planner evacuation — a compact recovery index:** trigger/reason and timestamp; project/session/version; last known board state with read time/revision; completed, in-progress, and pending item IDs; last executor return and handoff IDs; current HITL/decision IDs; evidence/artifact pointers; unresolved facts marked `unknown` with the reason; and the next safe planner action. Each factual claim should point to its durable source and freshness. When sources conflict, preserve both references and flag reconciliation rather than silently choosing one. The planner can fetch detail by ID only when needed.

**Bounded exploration — explicit envelope and closeout:** state one research question, scope, allowed resources, time/turn budget, expiry, evidence/deliverable location, parent project/request, and a stop condition before starting. The existing scratch-run receipt is bounded and self-reported; its artifact references are strings, not verified typed artifact pointers. A promotion may create a durable finding. The planner then reviews that finding and separately decides whether to create a sprint item or proposal. Do not describe this as automatic promotion to planned work.

## Evidence labels

- **SHIPPED in source:** six named handoff modes and their resolver; checkpoint-to-delta path; sprint/task/HITL records; the `executor_reports` data schema and systemic-wave-abort corrective-report writer; scratch research runs with explicit dispositions; one-level parent-project links.
- **TESTED in repository, not rerun for this document:** the handoff regression matrix covers retrieval behavior for full/delta/goal and starter non-clobbering; systemic wave invalidation has a durable corrective-report test. These cases do not prove a generic executor-return flow. `tests/test_executor_reports.py` covers an in-memory completion-phase registry, not the durable report table. See [handoff regression matrix](../../tests/test_782636cd_handoff_regression_matrix.py#L253) and [systemic-report test](../../tests/test_cc3864bd_systemic_wave_invalidation.py#L237).
- **UNKNOWN / not verified here:** whether every connected client exposes the same paths; whether a scratch-run expiry caller is active; hard enforcement of the declared turn budget; typed artifact-pointer verification for scratch receipts; and a general report submission/acceptance tool. Production source contains the report table, but the live production check in the evidence ledger verifies service liveness, not each workflow end to end.
- **PROPOSED:** a compact planner evacuation packet, a generic typed executor-return writer that links to existing board/HITL/evidence records, and an exploration envelope that records its question and parent request. Reuse existing report/run/project records; make planner acceptance the explicit step that can create parent sprint work.

## Source trail

- Handoff mode selection and rendering: [handoff.py](../../meridian/handoff.py#L6387), [full/delta preparation](../../meridian/handoff.py#L12771), [goal rendering](../../meridian/handoff.py#L14308), [planner rendering](../../meridian/handoff.py#L13780), [starter/compact rendering](../../meridian/handoff.py#L12640).
- Report schema and narrow current writer: [executor_reports.py](../../meridian/db/executor_reports.py#L232), [wave_runs.py](../../meridian/db/wave_runs.py#L786).
- Scratch-run fields, validation, and explicit disposition: [research_run.py](../../meridian/research_run.py#L55), [research_runs.py](../../meridian/db/research_runs.py#L197), [promotion boundary](../../meridian/db/research_runs.py#L397).
- Project parent linkage: [create parent-linked project](../../meridian/db/__init__.py#L1184), [change parent](../../meridian/db/__init__.py#L1459), [project handlers](../../meridian/mcp/handlers/project_tools.py#L54).
- Compact continue and planner report reads: [server resume payload](../../meridian/server.py) and [planner handoff route](../../meridian/routes/handoff.py).
- Task log and HITL stay separate: [task log](../../meridian/db/__init__.py#L4035), [HITL request](../../meridian/db/__init__.py#L9411).
