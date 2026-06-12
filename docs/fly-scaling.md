# Fly scaling for launch week

Operational reference. Source of truth: `meridian/limits.py` and
`meridian/hosted.py` (look for `_MAX_CUSTOMERS_PER_PROJECT`,
`_MAX_PROJECTS_STANDARD`, `_ALERT_THRESHOLD_STANDARD`).

## Recommended Fly config — launch week

- **min_machines_running = 2**
  - One machine drops to zero on idle but the next signup eats a 3-5s cold
    start, and OAuth flows reading `meridian_session` against a freshly-cold
    machine race the 30s gate-cookie deadline. Two warm machines absorb the
    spike without the cold-start latency tail.
- **soft_limit (concurrent requests per machine) = 100**
- **hard_limit = 200**
  - The `_body_size_guard_middleware` + slowapi rate limiter (100/min/IP) cap
    most floods well before this. Hard limit is the safety net.
- **autoscale: yes, max_machines_running = 4**
  - Two more machines on demand handles bursts; beyond four you're better
    off splitting workloads across regions.

## Pool capacity (Neon)

Per `meridian/hosted.py`:

- `_MAX_CUSTOMERS_PER_PROJECT = 8` — soft cap per pool project.
- Standard tier hard cap: `_MAX_PROJECTS_STANDARD` pool projects total.
- Standard tier soft alert: `_ALERT_THRESHOLD_STANDARD` (default 85) —
  triggers an admin email via `_send_capacity_alert` so an operator can
  expand capacity before the hard cap blocks signups.

The atomic claim path (`claim_pool_project_slot`, item 38) guarantees the
soft cap is never violated under concurrent signups. The hard cap is
enforced by `check_capacity` raising before provisioning begins.

## Load drill (item 38)

`scripts/load_drill_signup.py` fires N concurrent signups at a preview
deployment. Always pass `--confirm-not-prod`. The drill exits zero only
when every signup got a non-5xx, non-error response — so even pool
exhaustion must be served as a 200/202 or waitlist redirect.

## On-call

5xx alerting (item 39, `meridian/error_alerting.py`): admin gets ntfy +
email when the rolling 5-min 5xx count breaches
`MERIDIAN_5XX_ALERT_THRESHOLD` (default 10). Cooldown
`MERIDIAN_5XX_ALERT_COOLDOWN_SECS` (default 900s) prevents an incident
from paging us minute-by-minute. Configure either notifier:

- `MERIDIAN_ADMIN_NTFY_URL` — ntfy.sh URL or bare topic
- `ADMIN_EMAIL` + `RESEND_API_KEY` — email path
