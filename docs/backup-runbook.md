# Backup & restore runbook

Operational reference for recovering Meridian's data after corruption,
accidental deletion, or a bad migration. Applies to any Neon-Postgres-backed
deployment (hosted prod and self-hosted alike).

## What holds state

Meridian is stateless at the app tier (Fly machines are disposable). All
durable state lives in **Neon Postgres**:

| Store | Holds | Lost if gone |
|-------|-------|--------------|
| Auth DB (`MERIDIAN_AUTH_DB` / `MERIDIAN_DB_URL`) | tenants, user_sessions, api_tokens, waitlist | every login + every account |
| Per-project DBs | projects, goal_states, sessions, task_log, sprint_items, decisions | all coordination history |
| Demo DB (`MERIDIAN_DEMO_DB_URL`) | seeded demo content | demo page (re-seedable) |

The demo DB is re-seedable from `scripts/demo.py` and is **not** business
critical. The auth DB and per-project DBs are.

## How backups happen

Neon provides continuous backups via **point-in-time restore (PITR)** — every
database can be restored to any moment inside its history-retention window
(plan-dependent; verify the window in the Neon console under *Settings →
Storage*). There is no cron job to maintain: Neon's WAL retention is the
backup. Two consequences:

1. **Retention is finite.** Confirm the retention window meets your RPO. If a
   corruption is discovered after the window closes, the data is unrecoverable
   — so the restore drill below must run inside that window.
2. **A restore is a *branch*, not an in-place rewind.** You restore by creating
   a Neon branch at a past timestamp, validating it, then repointing the app's
   connection string at the branch (or promoting it).

## Restore procedure

> Do not restore in place over a live database. Always restore to a new branch
> first, validate, then cut over.

1. **Identify the target timestamp** — the last known-good moment before the
   incident (UTC).
2. **Create a restore branch** in the Neon console (*Branches → Create branch →
   "From a point in time"*) or via API:
   ```bash
   # NEON_API_KEY referenced by name only — never paste the value.
   curl -s -X POST "https://console.neon.tech/api/v2/projects/$PROJECT_ID/branches" \
     -H "Authorization: Bearer $NEON_API_KEY" \
     -H "Content-Type: application/json" \
     -d '{"branch":{"name":"restore-YYYYMMDD","parent_timestamp":"2026-06-07T00:00:00Z"}}'
   ```
3. **Get the branch connection string** (*Connection Details → select the
   restore branch*).
4. **Validate** against the branch before cutover — point a staging/preview app
   at it, or query directly:
   ```bash
   psql "$RESTORE_BRANCH_URL" -c "SELECT count(*) FROM tenants;"
   psql "$RESTORE_BRANCH_URL" -c "SELECT max(created_at) FROM task_log;"
   ```
   Confirm the row counts and the latest timestamps match the known-good state.
5. **Cut over** — set the app's connection string secret to the restore branch
   and redeploy:
   ```bash
   flyctl secrets set MERIDIAN_DB_URL="$RESTORE_BRANCH_URL" --app meridian-hosted
   ```
   Fly redeploys on a secret change; the post-deploy `/health` smoke +
   auto-rollback in `deploy.yml` guard the cutover.
6. **Verify prod** — `curl https://usemeridian.us/health` returns 200, log in,
   confirm a project's task history is present.

## Restore drill (quarterly — owner action)

A backup you have never restored is a hypothesis, not a backup. Once a quarter,
prove the path works **without touching prod**:

1. Create a restore branch from ~24h ago (above, steps 1–3).
2. Point **`meridian-preview`** at the branch:
   `flyctl secrets set MERIDIAN_DB_URL="$RESTORE_BRANCH_URL" --app meridian-preview`.
3. Run the smoke + funnel checks against preview:
   `pixi run smoke-test-preview` and
   `python scripts/funnel_smoke.py --url https://meridian-preview.fly.dev`.
4. Spot-check data integrity (`tenants`, `task_log` counts vs. prod).
5. **Tear down**: restore preview's original `MERIDIAN_DB_URL` and delete the
   restore branch so it stops accruing storage.

Record the drill date + result. The actual execution of this drill against live
Neon infrastructure is an **operator/owner action** (it touches real secrets and
billed infra) — it is intentionally not automated.

## RPO / RTO targets

- **RPO** (max acceptable data loss): bounded by Neon's retention window —
  effectively near-zero within the window (PITR is continuous).
- **RTO** (max acceptable downtime): branch creation is seconds; validation +
  cutover + redeploy is the long pole. Budget ~15–30 min end to end.
