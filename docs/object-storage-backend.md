# Object-storage backend (Tigris/S3) — local-first contract and activation gate

**Status as of this document: Tigris/S3 is INACTIVE. No dependency installed,
no bucket created, no credentials configured, nothing wired into default
routing.** This document exists so that stays true until a human deliberately
walks through the checklist in [Activation gate](#activation-gate) below.

Sprint item: `1d34c076` (build milestone), depending on investigation
`549e66c6` (`docs/meridian-tigris-s3-boundary-investigation-2026-08-25.md`,
in a sibling worktree — the authoritative source for the design rationale
summarized here). Follow-on human item `7197907f` (already filed) owns real
bucket creation, credentials, and smoke verification — **not** this document
or the code it describes.

## What shipped in this sprint item

- `meridian/object_store.py` — the provider-neutral `ObjectStoreBackend`
  Protocol, its error taxonomy, `LocalObjectStoreBackend` (a real adapter
  over the existing `meridian/artifact_store.py`), `FakeS3Backend` (an
  in-memory test double — **not** a real network client), and
  `TigrisObjectStoreBackend` (an explicit `NotImplementedError`-raising
  stub — see below).
- `meridian/db/object_sync_state.py` + the matching Postgres migration in
  `meridian/pg_adapter.py` (`_migrate_pg_object_sync_state`) — a small,
  relational table tracking per-`(project_id, content_hash)` sync state.
  This is coordination *metadata*; it never holds the actual bytes.
- Tests: `tests/test_object_store.py` (Protocol/backend contract, including
  the full `FakeS3Backend` acceptance-gate matrix) and
  `tests/test_object_sync_state.py` (DB-layer state machine).

Nothing in this list changes what any existing caller does. No route, MCP
tool, or background worker calls `LocalObjectStoreBackend` or
`object_sync_state` yet — wiring an actual caller in is future, opt-in work.

## The local-first / offline contract

This mirrors `artifact_store.py`'s own documented Redis-cache invariant,
applied to remote object storage instead of a read cache:

> A remote tier must never become the only copy or the write path. Every
> write lands on local disk first, synchronously, via
> `meridian.artifact_store.store_artifact`. A remote-sync miss, outage, or
> unconfigured backend must degrade to *latency* only — never to data loss
> or a wrong answer.

Concretely:

1. **Local disk is always authoritative.** `LocalObjectStoreBackend` is a
   thin, semantics-preserving adapter over `artifact_store.py` — it adds no
   new storage behavior, only the `ObjectStoreBackend` Protocol shape. Every
   artifact readable through it is exactly what `artifact_store.py` already
   stores today.
2. **A remote backend is purely additive.** Nothing currently constructs
   `TigrisObjectStoreBackend` (it can't be — see below), so today there is
   no remote tier at all. When one exists, a sync failure or an
   `unavailable` remote must never block a caller from reading/writing
   locally; `tests/test_object_store.py::test_offline_remote_backend_never_loses_local_data`
   is the regression guard for that invariant.
3. **`sync_failed` vs. `unavailable` are different problems.** A transient
   network/timeout/5xx (`ObjectStoreUnavailableError`, mapped to
   `sync_failed` in `object_sync_state`) is retry-eligible on backoff. A
   categorical auth/config/endpoint-down failure (`ObjectStoreAuthError`,
   mapped to `unavailable`) is not solved by retrying the same request — it
   needs a human to confirm the backend is reachable again before a
   re-enqueue sweep touches those rows. `db.list_object_sync_retry_eligible`
   returns exactly the `sync_failed`/`unavailable` set for this purpose;
   nothing in this codebase currently runs an automatic tight retry loop
   against a confirmed-down endpoint, by design.
4. **Content objects can't go "stale."** Because local content is addressed
   by `sha256` and immutable, new bytes always produce a new hash and a new
   object — never a mutation of an existing one. The `remote_stale` state in
   `object_sync_state.OBJECT_SYNC_STATES` is reserved for a *future* mutable
   "pointer" concept (e.g. "latest handoff bundle for project X") and is
   deliberately unused by every function shipped in this sprint item.

## Why `TigrisObjectStoreBackend` cannot do anything yet

`TigrisObjectStoreBackend.__init__` raises `NotImplementedError`
unconditionally — before touching the network, the filesystem, or even an
environment variable. This is intentional, not a placeholder bug:

- No S3-compatible client (`aioboto3`, `boto3`, `obstore`, or otherwise) is a
  dependency of this repo. Adding one requires an explicit supply-chain
  review this sprint item does not authorize (see investigation `549e66c6`
  §5).
- Even with a client approved and vendored, a real implementation needs to
  translate S3 `ClientError` status codes into this module's error taxonomy
  (`ObjectNotFoundError` / `PreconditionFailedError` /
  `ConditionalRequestConflictError` / `ObjectStoreAuthError` /
  `ObjectStoreUnavailableError` / `QuotaExceededError`) — unimplemented here.
- Tigris's own conditional-write (412/409) and presigned-URL behavior was
  **not** independently confirmed against a real bucket in the investigation
  (its docs site's relevant sub-pages 404'd) — the AWS S3 facts used as a
  working assumption for Tigris must be smoke-tested against a real bucket
  before any code depends on them.

## Configuration — opt-in env vars only (documented, not read yet)

No code in this repo reads any of the following today. They are documented
here so the *future* real implementation (and whoever provisions the bucket)
has one canonical list, and so nothing needs guessing later. **None of these
belong in `.env` or `meridian.toml` as placeholders — add them only when the
real backend is implemented and a bucket actually exists.**

| Variable | Purpose | Source |
|---|---|---|
| `TIGRIS_ENABLED` | Explicit opt-in flag a future capability-manifest check would gate on. Absent/false = inactive (today's actual state, unconditionally). | New, this design |
| `AWS_ENDPOINT_URL` | Tigris's S3-compatible endpoint (`https://t3.storage.dev`). | Tigris docs, AWS SDK convention |
| `AWS_ACCESS_KEY_ID` | Tigris access key (format `tid_...`). | Tigris docs |
| `AWS_SECRET_ACCESS_KEY` | Tigris secret key (format `tsec_...`). | Tigris docs |
| `AWS_REGION` | Region, if Tigris requires one for SDK compatibility. | AWS SDK convention |
| `TIGRIS_BUCKET` | Target bucket name. | New, this design |

Real values for these must only ever live in `.env` / `meridian.toml`
(self-hosted, never committed) or the hosted platform's secret store — never
in chat, task descriptions, sprint-item notes, or any committed file. This
document deliberately contains no real values, and none were touched while
writing it.

## Bucket-per-tenant vs. prefix-per-tenant — still `[OPEN]`

The investigation explicitly leaves this as a human decision (§4 `[OPEN]`)
rather than defaulting it silently: Tigris's credential-scoping granularity
(bucket-level vs. prefix-level) was not confirmed in the investigation's doc
fetches, so bucket-per-tenant only buys real isolation if credentials can
actually be scoped to it — unverified. The recommendation on file is
prefix-per-tenant for MVP (mirrors the already-shipped Neon
`pool_project_id`-then-`neon_project_id` pattern in `meridian/hosted.py`),
but this needs an explicit human sign-off before it's load-bearing for a
real deployment. `object_store.build_object_key`'s `tenant_id` parameter
works for either topology — nothing in the key-construction code assumes
one or the other.

## Activation gate

Exact order, reproduced from investigation `549e66c6` §9. **None of these
steps are performed by this sprint item; every one of them is a distinct,
later, explicit action:**

1. A human reviews the investigation report and this document; signs off on
   prefix-per-tenant vs. bucket-per-tenant (see above).
2. Supply-chain review of `aioboto3` (or a chosen alternative); only on
   approval does anyone touch `pyproject.toml`/`pixi.toml`.
3. ~~Implement `meridian/object_store.py` (Protocol + `LocalObjectStoreBackend`)~~
   — **done in this sprint item.** No credentials were required.
4. ~~Add the `object_sync_state` migration~~ — **done in this sprint item**
   (SQLite: `meridian/db/object_sync_state.py`; Postgres:
   `meridian/pg_adapter.py::_migrate_pg_object_sync_state`).
5. Implement `TigrisObjectStoreBackend` for real, behind the same Protocol;
   unit-test against a fake/mock backend only — no real credentials in CI.
6. A human provisions a real Tigris bucket + access key outside this
   repo/session; stores credentials only in `.env`/`meridian.toml` (never
   committed) or the hosted platform's secret store. **This is follow-on
   item `7197907f`.**
7. Run a live smoke test against that bucket confirming conditional-write
   (412/409) and presigned-URL behavior before relying on them further.
   **Also part of item `7197907f`.**
8. Wire `set_capability_manifest(capabilities=[{id: "object_storage_sync",
   required_tools: [...], fallback_chain: ["local_only"],
   availability_policy: "optional"}])` — start `optional`, never
   `required`, until production-proven.
9. Implement a sync worker + an MCP tool to trigger/inspect sync (e.g.
   `sync_artifacts_to_remote`), opt-in per project.
10. Pilot on one project; monitor `sync_failed`/`unavailable` rates in
    `object_sync_state` before wider rollout.

Steps 3 and 4 are complete as of this sprint item. Every other step remains
undone, in order, and none of them are implicitly authorized by this
document.

## Testing this without a real bucket

`meridian.object_store.FakeS3Backend` is an in-memory, S3-shaped test double
— no sockets, no dependency, not wired into any production code path. It
exists purely so the full contract (upload, download, head, hash-mismatch
detection, transient-failure retry, conditional-write conflict retry,
offline-fallback, tenant isolation, idempotency) can be exercised in CI. See
`tests/test_object_store.py` for the complete matrix, and
`tests/test_object_sync_state.py` for the DB-layer state-machine tests.
