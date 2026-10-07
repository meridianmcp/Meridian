# Neon Tenant Pool Isolation: Design and Measurement

**Sprint Item:** 1b2fbebe  
**Status:** Design + advisory telemetry shipped; thresholds uncalibrated; nothing acts automatically  
**Last Updated:** 2026-10-07  

Code: `meridian/pool_telemetry.py` (meter, classifier, advisory recommender, alert ledger),
`meridian/pool_timing.py` (ASGI timing middleware), `GET /admin/pool-load`
(`meridian/routes/admin.py`), tests `tests/test_pool_telemetry.py`,
`tests/test_pool_load_wiring.py`, `tests/test_tenant_pool_isolation.py`.

---

## 1. Scope and Non-Goals

**Scope:**
- Measure and define guardrails for Neon shared-compute tenant pools in the hosted tier.
- Design telemetry to detect noisy-tenant impact on co-tenants.
- Specify placement rules and migration procedures (manual, owner-gated, not automated).
- Create synthetic and staging test plans to validate routing logic.
- Document evidence gates before any cost-saving claims.

**Non-Goals:**
- Automated tenant migration or live Neon quota changes in production.
- Per-workspace-member compute allocation or cost attribution.
- Cost-saving claims without before/after Neon consumption evidence.
- Changes to central Meridian DB or Meridian-Auth DB placement (both shared, separate from tenant pools).

---

## 2. Verified Topology

**VERIFIED:** Tenant provisioning and pool registration.  
Source: `meridian/hosted.py:1615` `provision_neon_db`, `meridian/db/__init__.py:8387` `claim_pool_project_slot`.

```
Hosted Tenant (tenant row)
  ├─ neon_db_url (encrypted)
  ├─ neon_project_id (Neon project id, e.g. green-glitter-12345678)
  └─ pool_project_id (Meridian pool registry key)
       │
       └─> Neon Pool Project (neon_pool_projects table)
            ├─ id (pool registry key)
            ├─ neon_project_id (UNIQUE)
            ├─ tier (free | standard | pro)
            └─ customer_count (0 ≤ count ≤ MAX_CUSTOMERS_PER_PROJECT; a claim needs count < max)
                 │
                 └─> Neon Project
                      ├─ Branch (default)
                      └─> Compute Endpoint
                           ├─ active_time_seconds (Neon-measured)
                           ├─ compute_time_seconds (Neon-measured)
                           └─ current_state (available | suspended | …)
```

**Claim Logic:** VERIFIED, source `meridian/db/__init__.py:8387`.  
When a tenant is provisioned, `claim_pool_project_slot(tier)` executes an atomic UPDATE-with-subquery:
1. Select a pool project where `tier = requested_tier` AND `customer_count < MAX_CUSTOMERS_PER_PROJECT`.
2. Increment `customer_count` and return the pool project `id`.
3. If no suitable pool exists, return `None`; provisioning then creates a new pool project via `_create_neon_pool_project`.

**Pool Capacity Limits:** VERIFIED, sources `meridian/hosted.py:1214` (env `MAX_CUSTOMERS_PER_PROJECT`, default 8), `hosted.py:1215` (`_MAX_PROJECTS_STANDARD` = 90).

**Dedicated Project Precedent:** VERIFIED, source `meridian/plans.py` (playtester plan), `hosted.py:1657`, `hosted.py:1872`.  
Plans marked `is_unbilled_plan()` (e.g., playtester/pro) are allocated their own Neon project with `customer_count = MAX_CUSTOMERS_PER_PROJECT` (effectively full, blocking new claims). Teardown via `_drop_tenant_neon_database`.

---

## 3. Shared-Compute Boundary

**Provider Fact:** VERIFIED per Neon MCP tools and product documentation, verified 2026-10-07.

All databases within a single Neon project/branch are served by that branch's compute endpoint, contending for the same compute unit (CU) pool.

**What Neon Exposes:**
- Per endpoint: `last_active`, `current_state`, `suspended_at`.
- Per project/branch: `compute_time_seconds`, `active_time_seconds` (via Neon consumption API).
- **NOT exposed:** Per-database CU attribution, query-level logs, or `pg_stat_statements`.

**Consequence:** Per-tenant compute attribution must be instrumented in Meridian, not derived from Neon logs.

**Request Routing:** VERIFIED, source `meridian/_deps.py:196`.  
At request time, `_open_tenant_db_by_id` decrypts the tenant's `neon_db_url` and opens a `psycopg AsyncConnectionPool` cached in `_tenant_db_cache` (unbounded in-process). Changing a tenant's pool requires:
1. Update `neon_db_url` and `pool_project_id` on the tenant row (one transaction).
2. Evict the cached pool on all Fly instances (manual or via coordinated shutdown).

**Billing & Consumption Infrastructure:** VERIFIED, sources `hosted.py:2411` `_fetch_neon_consumption`, `hosted.py:2503` `run_overage_check`.  
- Consumption API polled daily at 03:00 UTC and hourly for storage overage.
- Responses cached; per-tenant telemetry must reuse these caches, never wake endpoints separately.

---

## 4. Measured Baseline

**Data source:** VERIFIED as recorded, not re-measured here. Owner snapshot of 2026-10-07
(decision 8e278f88, task 272417c2, note e8c1e595). These are billing-period readings, not a
controlled experiment, and the available Neon query logs were empty (`pg_stat_statements` is not
installed), so they show the *shape* of consumption, not which query caused it.

| Project | Active time | Compute | Notes |
|---|---|---|---|
| Central `Meridian` (control plane: tenant registry and shared data; billed org) | ~149 h (536,276 s) | ~37.88 CU-h | The project that dominates the observed usage line |
| `Meridian-Auth` (separate project, production org) | ~86.7 h (312,236 s) | ~22.5 CU-h | Not part of any tenant pool |
| Newer free pool project A (max 2 CU, 100 h active/compute quota) | ~3.0 h (10,636 s) | not captured | A tenant pool |
| Newer free pool project B (max 2 CU, 100 h active/compute quota) | ~0.34 h (1,240 s) | not captured | A tenant pool |
| Older free pool project (max 8 CU, no quota) | none | none | 0 databases; inactive since June |

An earlier reading of the central project for 1-7 October (note e8c1e595) gave 36.28 compute
hours, 145.1 active hours and about 9.05 GiB of transfer; it is the same project as the first
row, read earlier in the period, not an aggregate of the table.

**Conclusion (VERIFIED against the numbers above):** tenant pools are currently a small share of
compute. The central control plane dominates, and its recurring keepalive and polling work is the
lead cause (finding df0054cd, decision 09a7f778, first mitigation in worktree
`neon-idle-wakeup-cost`). **Pool isolation is a guardrail for growth, not a current cost fix.**
No saving is claimed here.

---

## 5. Telemetry Design

**What exists (VERIFIED, shipped with this item):**

| Piece | Where | Role |
|---|---|---|
| `_note_pool_tenant` | `meridian/_deps.py` (`_db`) | Once a hosted request has resolved its tenant database, stashes `(tenant_id, neon_project_id)` on the request. `_tenant_pool_ids` is filled when `_open_tenant_db_by_id` loads the tenant row. A workspace member's request is attributed to the *workspace* tenant whose database it hits. |
| `PoolTimingMiddleware` | `meridian/pool_timing.py`, registered last (outermost) in `server.py` | Pure ASGI. Measures request arrival to response *headers* (the handler's own work, including its queries). Streaming bodies and SSE are not included. Requests that never resolved a tenant (static files, public pages, probes, self-hosted) are not recorded. |
| `TenantPoolMeter` / `METER` | `meridian/pool_telemetry.py` | In-memory per-tenant and per-pool windows, classifier, advisory recommender. |
| `AlertLedger`, `new_advisories()` | same | One advisory per key per cooldown (default 1 h). Nothing is sent yet: the caller decides. |
| `GET /admin/pool-load` | `meridian/routes/admin.py` | Admin and admin-password gated like `/admin/health`; returns the redacted snapshot with `"scope": "this server process only"`. `top_n` is clamped to 1-50. |

**Constraints that are part of the design:**
- **In memory only, per server process (per Fly machine).** It never writes to Postgres or Redis: telemetry that wakes Neon defeats its purpose. With several machines each reports only what it served; cross-machine aggregation is a later, Redis-backed step.
- **Redacted.** Tenants appear as `t_` plus 10 hex characters of a salted SHA-256; pools as `p_` plus the last 8 characters of the Neon project id (`p_unpooled` when there is none). The snapshot never contains a raw tenant id, project id, email, URL, query text or request body.
- **Bounded.** At most 2,000 tracked tenants (least-recently-seen eviction, counted in `evicted_tenants`), at most 15 one-minute buckets per tenant, and a fixed 11-bin latency histogram (5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000 ms plus overflow) per bucket; raw samples are never stored. A tenant silent for a whole window drops out of memory.
- **Cheap on the request path.** `record` is O(1) amortised under a lock, and `record_request` swallows every error. `MERIDIAN_POOL_TELEMETRY=0` (or `false`/`off`) turns recording off without a restart.

**Reading a snapshot:** `top_tenants[].state` is one of `insufficient_evidence`, `idle`, `normal`, `hot`.
`pools[].p95_ms` is the upper bound of the histogram bin holding the 95th percentile. A hot tenant
whose pool-mates show a high `p95_ms` is a noisy neighbour; a hot tenant alone in its pool, or
whose neighbours are healthy, is not an isolation problem.

**Known gaps (be explicit):**
- The latency is time-to-first-byte of the HTTP response, not per-query time, and Neon's own per-database attribution does not exist, so a slow co-tenant request is evidence of contention, not proof.
- Only requests that go through `_db()` are counted; background loops (keepalive, auto-summary) and the MCP stdio server are not.
- No alert is delivered: `new_advisories()` is available to a caller but is not scheduled anywhere. Wiring it into the existing hosted maintenance loop touches `server.py` and was deliberately left for a separate change.
- Thresholds are provisional (section 6). Until they are calibrated against at least a week of real traffic, treat every `hot` label as a prompt to look, not a verdict.

---

## 6. Thresholds

**Provisional Defaults Table** (UNMEASURED placeholders; advisory only, nothing acts automatically):

| Metric | Default | Rationale | Status |
|--------|---------|-----------|--------|
| Bucket duration | 60 s | Trade-off between granularity and memory | TBD (validation) |
| Window size | 15 buckets | 15 min observation; captures short bursts | TBD (validation) |
| Min observed buckets | 5 | Below this, insufficient evidence; not flagged | TBD (validation) |
| Idle threshold | < 1 req/min | Tenant not actively using the pool | PROPOSED |
| Hot threshold | ≥ 120 req/min sustained | The last 10 *completed* one-minute buckets all at or above the rate (the bucket still filling is ignored, otherwise a steady tenant would drop out of hot at every minute rollover) | PROPOSED |
| Hot hysteresis | Leaves hot below 60% of hot rate | Prevents flapping | PROPOSED |
| Noisy-neighbour p95 | ≥ 1500 ms | Latency spike indicating contention | TBD (needs real workload data) |
| Dominant share | ≥ 70% of pool requests | One tenant consuming most of the pool | PROPOSED |
| Alert cooldown | 1 hour per key | The existing capacity alert (`_send_capacity_alert`) has no de-duplication | PROPOSED |

**Calibration Procedure:** PROPOSED.

1. Deploy telemetry to staging and production.
2. Observe snapshots for one week with default thresholds (collect data).
3. Identify false positives (flagged hot but causing no impact) and false negatives (not flagged but causing visible co-tenant latency).
4. Adjust thresholds (e.g., hot_threshold, noisy_p95) based on observed correlation.
5. Rerun one-week observation to validate; document results in a follow-up ADR.
6. Pin thresholds in code and environment defaults.

---

## 7. Placement Guardrails

**Advisory Rules** (not yet automated; for human decision-making):

| Tenant State | Rule | Rationale |
|--------------|------|-----------|
| Not hot | Stay pooled | No isolation benefit |
| Hot, alone in pool | Stay pooled | Isolation benefits nobody |
| Hot + co-tenant p95 ≥ noisy threshold | Recommend dedicated 1-tenant project | Isolate hot tenant; recover co-tenant latency |
| Hot + dominant share (≥70%) but healthy co-tenant p95 | Recommend small hot pool | Keep cost low; monitor co-tenant impact |
| Hot + several co-tenants all degraded | Same as the noisy-neighbour row; splitting a pool in two is an open question (section 11) | Not implemented in `recommend_placement` |

**Dedicated Project Budgets** (PROPOSED, subject to owner decision):  
Reuse `PLAN_LIMITS` per tenant plan as monthly ceilings:

| Plan | CU-hours | Storage GB | Warning at | Alert at |
|------|----------|-----------|-----------|----------|
| free | 10 | 0.1 | 8 CU-h | 10 CU-h |
| standard | 50 | 1.0 | 40 CU-h | 50 CU-h |
| pro | 200 | 10.0 | 160 CU-h | 200 CU-h |

**Alert and degrade behaviour** (PROPOSED, owner decision):
- At 80% of a dedicated project's monthly ceiling: notify the owner once per cooldown.
- At 100%: notify the owner. Do **not** throttle or bill automatically; a dedicated project holds one tenant, so there is nobody to stop admitting. This matches decision 788e2dc7 (playtester: warnings fire, overage never bills).
- For a shared pool: when its measured contention stays above the noisy-neighbour threshold, mark the pool full (`customer_count = max`, the playtester-registry trick) so no new tenant is claimed into it, and recommend moving the hot tenant out. Existing tenants are never moved automatically.

---

## 8. Migration and Rollback Runbook

**Status:** PROPOSED, manual procedure, NOT automated.  
**Execution:** Owner-gated; requires explicit separate approval per tenant move.

### Preconditions

1. Decision captured in Meridian (e.g., `pin_decision` with recommendation reason).
2. Off-peak window scheduled (low traffic expected for source tenant).
3. Before/after Neon consumption plan documented (what metrics to compare, reporting period).
4. Backup/snapshot of source tenant DB taken and verified.

### Procedure

#### Step 1: Create Dedicated Project
**Action:**  
Use the same provisioning path the `playtester` plan uses for its dedicated project (`provision_neon_db`, `hosted.py:1657`). Register the new project in `neon_pool_projects` as **full** (`customer_count = max`), exactly like the playtester precedent, so `claim_pool_project_slot` never places another tenant in it.

**Verification:**  
- New Neon project is accessible and responds to health checks.
- Pool registry reflects the new project.

**Rollback:**  
Drop the Neon project if creation was the only step taken.

#### Step 2: Read-Only Window
**Action:**  
There is **no read-only switch for a tenant today** (not verified to exist anywhere in `meridian/`). Either build one first, or run the move inside an announced maintenance window with the tenant's writers (sessions, tunnel clients) stopped. Quiesce for at least 30 seconds.

**Verification:**  
- Confirm no writes are occurring (query application logs or connection pool state).

**Rollback:**  
Remove read-only flag; resume connections.

#### Step 3: Logical Dump and Restore
**Action:**  
1. Dump the source tenant database (logical backup, `pg_dump` or equivalent).
2. Restore to the new dedicated project database.
3. Verify row counts per table match source.
4. Run table-level checksums (e.g., `md5(row_count || coalesce(max(id), 0))` per table) to confirm parity.

**Verification:**  
- Row counts match within 0 rows (absolute equality).
- Checksums match.
- No constraint violations or errors during restore.

**Rollback:**  
Drop the new database; source remains intact and read-only.

#### Step 4: Connection String Swap
**Action:**  
In a single database transaction:
1. Update the tenant row: set `neon_db_url` (encrypted with `tenant_crypto`, never logged) and `neon_project_id`/`pool_project_id` to the new dedicated project.
2. Decrement `customer_count` on the source pool project.
3. Leave the new project's `customer_count` at max (it was registered full in step 1).
4. Commit.

**Verification:**  
- Transaction succeeded.
- Tenant row updated with new URLs/IDs.
- Pool registry counts adjusted.

**Rollback:**  
Reverse the transaction: swap connection strings back, restore pool counts.

#### Step 5: Cache Eviction
**Action:**  
Every Fly machine caches the tenant's connection pool in `_tenant_db_cache` and never refreshes it, so each machine must drop it. No eviction endpoint exists today (PROPOSED: a small admin-only one); until then use a rolling restart of the machines.

**Verification:**  
- Next request from that tenant connects to the new database (log inspection or connection counter verification).

**Rollback:**  
Restart Fly instances to reload the old connection string from the tenant row (already swapped back if Step 4 rolled back).

#### Step 6: Retention Window
**Action:**  
Keep the source tenant database intact for a retention window (e.g., 7 days). Monitor the new dedicated project for any anomalies.

**Verification:**  
- New project metrics are healthy (no errors, reasonable latency).
- Application logs show successful connections.

**Rollback (if needed within retention window):**  
Swap connection strings back to the source pool; clear the cache again; the source data was never dropped.

#### Step 7: Cleanup
**Action:**  
After the retention window, drop the source tenant database from the source pool project. Decrement `customer_count` on the source pool. (The source pool project itself is not dropped unless it becomes empty.)

**Verification:**  
- Source database is dropped.
- Pool registry count decremented.

---

## 9. Synthetic and Staging Drills

### Synthetic Drill (In-Repo)

**Location:** `tests/test_tenant_pool_isolation.py` (VERIFIED, shipped with this item).

**What It Validates:**
- Routing logic: that a hot tenant is correctly classified and guardrails recommend it for isolation.
- Recommendation engine: that placement rules produce sensible output (e.g., hot + noisy co-tenant → dedicated project).
- Latency model: that pool contention is modeled and observed in the simulation.

**Limitations:**
- Simulated concurrency, not real Neon CU limits.
- No network latency or Neon autoscaling behavior.
- Hard-coded contention model, not empirical Neon physics.

**Test Structure:**
1. Set up N tenants in a shared simulated pool with limited concurrency (e.g., concurrency_limit=10).
2. Tenant A floods with requests; other tenants issue normal load.
3. Assert that A's p95 latency is high, co-tenants' p95 inflates, but tenants in a different pool are unaffected.
4. Run the classifier on the snapshot; assert A is flagged hot and as a noisy neighbour.
5. Run the recommender; assert it recommends A for a dedicated project.
6. Simulate migration: move A to a new solo pool.
7. Re-run load; assert A's latency is unchanged but co-tenants recover.

**How to Run:**
```bash
pixi run test tests/test_tenant_pool_isolation.py -v
```

### Staging Drill (Against Real Neon, Disposable)

**Status:** PROPOSED, requires owner approval and a cost-bounded plan.

**Preconditions:**
1. Create a separate Neon project for testing (not the production org), with autoscaling and quota limits to bound cost.
   - Quota: a small per-project compute-time quota set in the Neon project settings (value TBD with the owner), so the project suspends itself rather than running on.
   - Compute: max 2 CU autoscaling, matching the newer production pools, to induce contention quickly.
   - Spend: a hard budget the owner approves in advance (TBD); the harness stops itself when it is reached.

2. Prepare a test harness (PROPOSED, to be written):
   - Create 4 test tenants in the staging Neon project pool.
   - Tenant A: high-volume workload (e.g., 500 req/s for 60 seconds, simulating a data export).
   - Tenants B, C, D: steady baseline load (e.g., 5 req/s each).
   - Measure: request latency (p50/p95/p99), error rate, Neon active_time_seconds, compute_time_seconds.
   - Record snapshots every 10 seconds.

3. Run the drill:
   - Phase 1 (5 min, baseline): All four tenants pooled, normal load. Record baseline metrics.
   - Phase 2 (5 min, contention): Tenant A starts flood. Record inflated latencies for B, C, D.
   - Phase 3 (5 min, recovery): Manually move A to a dedicated project (use production runbook steps 1–5). Record B, C, D recovery.
   - Phase 4 (5 min, solo): A alone in dedicated project, repeat its workload. Record A's latency (should not improve, since it was not constrained by co-tenants).

**Success Criteria:**
- Phase 1: All tenants p95 < 500 ms.
- Phase 2: Tenants B, C, D p95 > 1000 ms (at least 2x inflation).
- Phase 3: Within 2 min of A's migration, B, C, D p95 < 600 ms (recovery).
- Phase 4: A's p95 does not decrease (no co-tenant constraint was the limiting factor).

**Teardown:**
- Delete the test Neon project.
- Verify Neon consumption stopped (no further charges).

**Report Output:**
- Before/after latency distributions (tables or histograms).
- Neon CU and active_time comparisons (Phase 1 vs. Phase 2 vs. Phase 3).
- Recommendation: confirm that pool isolation works as designed, or identify physics gaps (e.g., autoscaling or background jobs that change observed contention).

---

## 10. Evidence Gates

**No cost-saving claim is valid without demonstrating all of the following:**

1. **Baseline Consumption Snapshot:** Before any migrations, record per-pool Neon consumption (compute_time_seconds, active_time_seconds, billing impact) for at least one full week. Store in Meridian decision log.

2. **Tenant Migration Log:** For each tenant migrated to a dedicated project, document:
   - Source pool (which shared pool it left).
   - Reason (noisy neighbour evidence, hot classification, or other).
   - Migration date and time.
   - Source and destination Neon project IDs.

3. **Post-Migration Consumption:** For at least one full week after all planned migrations, record per-pool and per-dedicated-project consumption. Compare compute_time_seconds and active_time_seconds to baseline.

4. **Co-Tenant Impact Measurement:** For each pool from which a tenant was migrated:
   - Before and after p95 latencies of remaining co-tenants (from telemetry snapshots).
   - Request-rate changes.
   - Error-rate changes.

5. **Cost Calculation:** Compute before/after monthly cost (Neon pricing × compute hours + storage overages) and quantify the absolute savings in USD. Do not claim savings from theoretical optimizations (e.g., "if we reduce autoscaling max CU").

6. **Attestation:** Owner review and written sign-off in Meridian decision log, referencing the above evidence.

**Failure to demonstrate any gate:** Claim is not valid. Revert any migrations or continue in a monitoring-only mode.

---

## 11. Open Questions for the Owner

1. **Threshold Calibration:** Are the provisional defaults (hot_threshold = 120 req/min, noisy_p95 = 1500 ms) reasonable given your expected workload profile? Should we run a week-long staging drill first before deploying to production?

2. **Dedicated Project Reuse:** If Tenant A is isolated to a dedicated project and later becomes idle, should it be re-pooled with others (to save Neon cost), or should it stay isolated indefinitely? This requires a depooling runbook and decision logic.

3. **Multi-Tenant Hot Pools:** The guardrails suggest "small hot pool" as an option (multiple hot tenants in one project). Should we define a size limit (e.g., max 3 tenants per hot pool)? How do we handle a fourth hot tenant that arrives?

4. **Cost Allocation:** Should the per-plan CU-hour budgets (Table in Section 7) be adjusted for specific high-value customers? Should playtester/pro get higher limits?

5. **Alert Routing:** When a dedicated project hits 80% of its CU-hour budget, who is notified and how? Email, Slack, PagerDuty, or a Meridian admin panel flag?

6. **Grace Period:** Should a newly-isolated tenant get a grace period (e.g., first 48 hours) before hard budget enforcement, to account for migration-related cleanup queries?

7. **Staging Drill Timing:** When should the disposable Neon staging drill run? As a one-time validation, or as a recurring (e.g., quarterly) check?

8. **Backwards Compatibility:** Are there any existing contracts (e.g., SLAs, documentation) that assume all standard/pro tenants have unbounded compute? Should we announce the new budgets proactively?

