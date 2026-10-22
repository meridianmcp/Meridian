# Recovery and project-state milestones

**Status:** implementation contract for local-first recovery milestones
**Scope:** compact Meridian project-state snapshots that complement routine checkpoints, provider-native history, and local artifact capture

## Product boundary

Routine `checkpoint` remains a small, replaceable progress snapshot. A project-state milestone is a separate append-only record created only for an explicit material transition or when the adaptive risk score reaches its threshold. It records pointers and state summaries; it does not copy provider transcripts, local working files, or artifact bytes.

The local provider/session catalog remains the source for native continuation and local transcript reconstruction. The local content-addressed artifact store remains the source for captured bytes and verified restoration. Meridian holds the project-scoped snapshot, source references, content hashes, and local artifact-manifest receipt supplied by the local client.

## Captured state

Each milestone contains:

- Project goal/scope from the live project row, with detected secrets redacted.
- Active pinned decisions, insights, and notes as source ids, titles, kinds, timestamps, and hashes of their redacted contents.
- A version-scoped sprint status count plus bounded item ids/titles for active work and recent completions.
- Existing evidence-pointer ids and hashes, without copying target bytes or local absolute paths.
- The local artifact manifest status and SHA-256 when the client supplies them. The hosted server labels this as a client report because it cannot independently read a workstation-local manifest.
- The explicit trigger, normalized risk signals, score, threshold, source-authority labels, capture timestamp, sequence, previous hash, and record hash.

The milestone itself receives a typed `project_state_milestone` pointer. Resolution is scoped to the owning project and verifies the stored hash before reporting the pointer as resolved.

## Escalation policy

`checkpoint` accepts a named material transition or bounded risk-signal list. Material transitions always append a milestone. Risk signals use fixed weights and a base threshold of four. The threshold rises by one for each three milestones captured in the preceding 24 hours, capped at seven, to coalesce repeated escalation storms. Artifact-hash mismatch and cross-project ambiguity force an immediate milestone regardless of that threshold.

No trigger and no risk signals keep checkpoint behavior routine. The caller can inspect the escalation result, and `get_project_state_milestones` returns the immutable records and integrity state.

## Integrity and retention

Records are inserted into a project-scoped table with a per-project sequence and previous-record hash. A canonical JSON serialization is hashed together with its project, session, trigger, sequence, timestamp, and previous hash. SQLite and PostgreSQL reject updates at the database layer; deleting a project cascades its records through the normal project-purge path. Every read recomputes the record hash; a mismatch is surfaced as unverified data.

Records are retained with the project state. A future retention/deletion policy must explicitly define how project deletion handles the append-only table and any external pointers. This feature does not provision Tigris or any remote artifact storage.

## Evaluation boundary

The frozen protocol in `recovery-evaluation-protocol-2026-10-02.md` remains the evaluation authority. This implementation adds the state and pointer surfaces only; it does not claim empirical recovery accuracy or provider-wide support. Evaluation fixtures must remain synthetic and local.
