# Research job scheduler (f6627d83)

Provider-neutral scheduling for research jobs, layered strictly on top of
the existing `Experiment -> Run -> RunAttempt` identity model
(`meridian.experiment_model` / `meridian.db.experiment_model`, item
4376e655). This is **not** a second scheduler database, queue engine, or
provider-specific core: Meridian owns experiment/run identity, policy,
provenance, budgets, idempotency, and normalized outcomes; adapters and
external runners own placement and execution.

## The contract

`meridian/research/providers/base.py` defines four shapes every provider
implements against:

- **`JobSpec`** — provider-neutral: `project_id`/`experiment_id`/`run_id`/
  `attempt_id` (binds to an existing RunAttempt), `idempotency_key`
  (required — every `submit` must be safe to retry), `command`, `image`,
  `env`, `inputs`, a generic `ResourceRequest` (cpu/memory/gpu/timeout/
  max_retries), `budget_usd`, and `provider_config` — the ONLY place
  provider-specific fields belong. Nothing RunPod-specific (or
  Slurm-specific, or SkyPilot-specific) ever appears outside that one dict.
- **`JobHandle`** — a provider's own opaque reference to a submitted job.
- **`JobStatus`** — `state` (one of `meridian.experiment_model.
  ATTEMPT_STATUSES` — the SAME vocabulary a `RunAttempt` uses, never a
  second one), optional `failure_class`, `output_refs`, `logs_ref`.
  `state='unknown'` is a first-class, truthful answer, not an error.
- **`ProviderCapabilities`** — what a provider can actually do
  (`can_cancel`, `can_stream_logs`, `can_fetch_artifacts`,
  `supports_gpu`/`supports_spot`/`supports_preemption_signal`/
  `supports_networking_config`, `supports_retries`). `JobProvider`'s base
  `cancel`/`fetch_logs`/`fetch_artifacts` methods check the matching flag
  and raise `UnsupportedOperation` when it's `False` — an adapter that
  hasn't implemented an operation cannot silently no-op as if it succeeded.

## Providers shipped in this item

- **`local`** (`providers/local.py`) — deterministic in-process dry-run.
  No subprocess, no network, no credentials. Outcome is controlled via
  `provider_config["simulate"]` (`succeed`/`fail`/`crash`/`hang`) so every
  scheduler code path (submit, poll-to-success, poll-to-failure, cancel,
  timeout, idempotent resubmit) has a deterministic test.
- **`runpod`** (`providers/runpod.py`) — the first concrete cloud adapter.
  Requires an INJECTED client (`RunPodClientProtocol`: `create_pod`/
  `get_pod`/`stop_pod`/`terminate_pod`) — this module never constructs a
  real client or reads credentials itself, so nothing here can create a
  pod, spend credits, or touch the network on its own. Tests inject a fake
  client; a real integration is future work, out of this item's scope.
  Raw provider errors are passed through `meridian.secret_redaction.redact`
  before ending up in a `JobStatus.detail` or a raised exception.

## The scheduler layer

`meridian/research/scheduler.py` binds a `JobProvider` to an existing
`RunAttempt` (`meridian.db.experiment_model`). It owns no state of its
own — a job's `JobHandle` is stored in the attempt's existing
`provenance_ref` JSON field (`{"job_handle": {...}}`), not a new table.

- `submit_run_attempt` — validates the spec against the provider's
  capabilities (e.g. a GPU request against a provider with
  `supports_gpu=False` is rejected before `provider.submit` is ever
  called) and against `budget_usd` (`BudgetExceeded`), then transitions the
  attempt `queued -> running` **only after** `provider.submit` returns
  successfully. If `submit` raises — including a timeout — the attempt
  stays `queued`: resubmitting with the same `idempotency_key` is always
  safe. This is "no false success after a submit timeout."
- `poll_run_attempt` — maps a provider's `JobStatus` onto the attempt's
  next transition via the existing `meridian.experiment_model` transition
  table. An illegal/stale transition (the provider reports something
  "behind" what Meridian already knows) is a benign no-op, not an error.
  An unclassified failure defaults to `failure_class='unknown'`.
- `cancel_run_attempt` — fails closed (`UnsupportedOperation`) if the
  provider doesn't support cancellation.
- `check_attempt_timeout` — a `queued`/`running` attempt past its
  `resources.timeout_seconds` is transitioned to `'unknown'` (never
  guessed as `'failed'`), with a best-effort provider cancel attempt.
- `should_retry` / `next_retry_delay_seconds` — bounded retry/backoff
  policy: a transient failure class (`infra_error`/`timeout`/`preempted`/
  `oom`/`unknown`) under `resources.max_retries` is retryable; a genuine
  `user_error`/`dependency_error` is not.

**Restart recovery** is not reimplemented here — it composes directly with
item 4376e655's `meridian.db.experiment_model.reconcile_stale_attempts`. An
attempt reconciled to `'unknown'` after a server restart can still be
resolved forward by a later `poll_run_attempt` call.

## Deferred adapter seams (not built in this item)

Per the reuse-gate research backing this item, none of the following are
embedded as a Meridian-owned scheduler core — they are either optional
future adapters behind the SAME `JobProvider` contract, or out of scope
entirely:

- **SkyPilot** — strongest optional provider-of-providers candidate for
  portable cloud/Kubernetes/Slurm/RunPod execution. Optional adapter only.
- **Parsl** — strongest optional native HPC adapter (local/Slurm/LSF/PBS/
  Kubernetes/cloud). Optional adapter only.
- **Nextflow / Snakemake** — external scientific workflow runners; not
  embedded as Meridian's scheduler.
- **Temporal / Prefect / Argo** — durable orchestration / Kubernetes
  workflow tools; do not replace Meridian's experiment/provenance control
  plane.
- **HTCondor/DAGMan, arbitrary Slurm/LSF/AWS/SSH/HPC scripts** — supported
  through the delegated/native submission path (see the follow-on
  EXP-DELEGATED item), not a new `JobProvider` per site.

## Where does new lifecycle logic belong?

Before adding any new job-lifecycle code, decide which of these it is —
never invent a fifth place:

1. **Local provider** (`providers/local.py`) — deterministic, in-process,
   credential-free behavior needed for tests or a genuinely local run.
2. **Direct RunPod adapter** (`providers/runpod.py`) — RunPod-specific
   state mapping/polling/cancellation behind the existing contract.
3. **A future SkyPilot/Parsl adapter** — anything that should work
   uniformly across multiple cloud/HPC backends Meridian doesn't want to
   special-case one at a time.
4. **Delegated external-job path** (follow-on EXP-DELEGATED item) — a
   user's own Slurm/LSF/Kubernetes/SSH/custom multi-machine script that
   Meridian should attach to, not submit or own.
