# Delegated & observed external job execution (0b7bb873)

The escape hatch for researchers who already have their own Slurm/LSF/PBS/
HTCondor/AWS Batch/Kubernetes/SSH/RunPod scripts or multi-node CPU/GPU
launchers, so they can keep working even when Meridian or a managed
adapter (f6627d83) is unavailable. This is **not** a second scheduler —
Meridian never owns the user's account, queue, cluster topology, MPI/NCCL
settings, or credentials here.

## Three truthful modes

1. **Managed** — Meridian submits through a `JobProvider` adapter
   (`meridian.research.scheduler`, item f6627d83). Not part of this module.
2. **Delegated** — the user submits through their own native CLI/runner
   and Meridian attaches the resulting external job id
   (`meridian.research.external_jobs.attach_external_job`).
3. **Observed/manual** — the user runs arbitrary commands entirely outside
   Meridian and later imports a completed manifest
   (`meridian.research.external_jobs.import_external_manifest`).

## The boundary: who controls what

```
 Meridian control plane        Local/authorized runner        External scheduler / object storage
┌───────────────────────┐     ┌───────────────────────┐      ┌──────────────────────────────────┐
│ RunAttempt identity,   │     │ Has real credentials,  │      │ Slurm / LSF / AWS Batch / SkyPilot│
│ provenance_ref,        │◄────│ shells out to sbatch/  │─────►│ / RunPod / a plain SSH box.       │
│ attempt status         │     │ bsub/aws/sky/ssh, or   │      │ Owns the actual job, its queue,   │
│ (transition_attempt)   │     │ manually finalizes a   │      │ its topology, and its native      │
│                        │     │ receipt after the fact │      │ status/log/cancel operations.     │
└───────────────────────┘     └───────────────────────┘      └──────────────────────────────────┘
```

- Meridian **never** executes an arbitrary remote command itself — a local
  or otherwise-authorized runner performs the actual submission (or the
  user does it by hand) and hands Meridian a **bounded receipt**
  (`ExternalJobRef`/`ExternalRunReceipt`), not a live credential or an open
  channel to run more commands.
- **Object storage / outputs** (wherever the job's real output files live —
  a shared filesystem, S3, GCS) is read by whatever imports the manifest
  (a local runner, a human), never by Meridian reaching out to fetch them
  itself. `ExternalRunReceipt.output_refs` are POINTERS a caller already
  resolved, not something this module goes and fetches.
- **Local import/export works without `meridian --tunnel`** — every
  function in `meridian.research.external_jobs` takes a plain `db`
  connection and plain dataclasses; nothing here requires a hosted
  connector.

## The contract

`meridian/research/external_jobs.py`:

- **`ExternalJobRef`** — `launcher` (one of `sbatch`/`bsub`/`aws_batch`/
  `sky`/`nextflow`/`snakemake`/`ssh`/`custom` — never a new hardcoded field
  per launcher), `external_id` (opaque), `idempotency_key`,
  `launcher_reference` (a sanitized, secret-checked human-readable
  submission shape — e.g. `"sbatch train.sh --partition=gpu"` — never raw
  credentials or secret arguments), `account`/`partition`/`queue`, a
  `TaskTopology`, and `input_refs`.
- **`TaskTopology`** — `node_count`/`node_rank` (multi-node identity),
  `array_size`/`array_index` (array-job task identity), and
  `parent_launcher_id` (links a fan-out child back to its parent
  submission).
- **`ExternalRunReceipt`** — an EXPLICIT, caller-asserted outcome:
  `status` (must be a terminal `ATTEMPT_STATUSES` value or `'unknown'` —
  never `'queued'`/`'running'`, since a receipt describes something that
  already happened or genuinely can't be determined), `failure_class`
  (required exactly when `status` is `failed`/`crashed`), `output_refs`,
  `logs_ref`, `finalized_by`.
- **`attach_external_job(db, project_id, attempt_id, ref)`** — registers a
  delegated job against an existing `queued` `RunAttempt`, transitioning it
  to `running`. Idempotent on `ref.idempotency_key`: re-attaching the SAME
  key returns the SAME attempt unchanged — no duplicate run, per acceptance
  criterion 1.
- **`mark_external_status(db, project_id, attempt_id, status, ...)`** — a
  status update for an ATTACHED delegated job (polling, or a human's
  manual correction after an out-of-band preemption/requeue report). Goes
  through the same validated transition table every other attempt update
  uses (`meridian.experiment_model.validate_attempt_transition`) — an
  illegal jump from an external source is rejected, not blindly trusted.
- **`import_external_manifest(db, project_id, attempt_id, receipt)`** — the
  observed/manual finalization path. Does NOT require a prior `attach_*`
  call (an observed job was never attached while running). Refuses to
  finalize an attempt that's already terminal (a receipt is a one-time
  fact, not a silent overwrite).

`meridian/research/providers/delegated.py`:

- **`DelegatedProvider`** — the `JobProvider`-contract-conformant wrapper
  around an already-attached job, for callers that want symmetric
  submit/status/cancel access alongside `local`/`runpod` providers.
  `submit()` ALWAYS raises `UnsupportedOperation` (a delegated job is
  attached, never submitted, through this interface — use
  `attach_external_job` instead). `status`/`cancel`/`fetch_logs` delegate
  to OPTIONAL injected callables (`status_poller`/`cancel_fn`/`logs_fn`);
  omitting one degrades that operation to the truthful "unavailable"
  answer (`status` → `state='unknown'`, `cancel`/`fetch_logs` →
  `UnsupportedOperation`) rather than an error implying the job failed.

## Array jobs, fan-out/fan-in, and preemption

- An array job's `N` tasks are `N` separate `RunAttempt`s, each with its own
  `ExternalJobRef.topology.array_index`/`array_size` — Meridian never
  collapses them into one row, since each task has its own independent
  outcome.
- A fan-out launcher (one `sbatch` submission spawning several dependent
  jobs) sets `topology.parent_launcher_id` on each child `ExternalJobRef`
  to the parent's `external_id`, preserving the lineage without a second
  graph — a caller wanting the full fan-out group queries attempts by that
  shared value.
- Preemption/requeue performed BY the external scheduler needs no new
  Meridian concept, but does need the right status: while the scheduler is
  expected to requeue the SAME job, its true state is genuinely uncertain
  from Meridian's side, so record `mark_external_status(..., "unknown",
  detail="preempted, awaiting requeue")` — `'unknown'` is the one
  non-terminal status that accepts a transition back to `'running'` once
  the scheduler's own requeue resumes it (see `meridian.experiment_model`'s
  transition table). Reserve `mark_external_status(..., "crashed",
  failure_class="preempted")` for a preemption the scheduler will NOT
  retry — `'crashed'` is genuinely terminal (only transitions to itself);
  do not use it for a preemption you expect to resume.

## Credentials and secrets

`ExternalJobRef`/`ExternalRunReceipt` run every free-text field
(`launcher_reference`, `account`, `partition`, `queue`, `finalized_by`,
`detail`) through `meridian.secret_redaction.check_for_secrets` at
construction — fail-closed, the same convention already used for
`research_graph`/`experiment_model` persisted text. Nothing in this module
executes a command, opens a network connection, or reads credentials from
the environment; a live cloud/HPC submission is never required to exercise
any of it.
