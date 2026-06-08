# Safety limits

Meridian enforces a small set of size and count guard-rails on every
project. They are **not quotas** — the thresholds are set far above
any honest single-team workload. They exist to keep one runaway loop
or buggy script from filling shared infrastructure (Postgres
connections, disk, request queue) for everyone else on the same
cluster.

If you ever hit one of these limits during normal use, that's a bug
on our side or a sign that something legitimate has outgrown the
default. Reach out at <hello@usemeridian.us> and we'll raise your cap.

## Defaults

| Limit | Default | What it guards |
|---|---:|---|
| `PROJECTS_PER_TENANT` | 1,000 | Project rows in your tenant DB |
| `SPRINT_ITEMS_PER_PROJECT` | 50,000 | Sprint items in one project |
| `NOTES_PER_PROJECT` | 100,000 | Per-project wiki notes |
| `DECISIONS_PER_PROJECT` | 10,000 | Pinned + archived decisions |
| `SESSIONS_PER_PROJECT` | 100,000 | Lifetime session rows |
| `TASKS_PER_PROJECT` | 1,000,000 | Logged task rows |
| `OPEN_HITL_PER_PROJECT` | 1,000 | Pending human-in-the-loop requests |
| `BODY_BYTES` | 100,000 | Single request body size |

## What a hit looks like

When a create call would push you past a limit you get **HTTP 429**
with a JSON body:

```json
{
  "detail": "Safety limit reached — protects shared infra. Legit need? hello@usemeridian.us · See docs.usemeridian.us/safety-limits (limit: notes_per_project=100000)",
  "kind": "notes_per_project",
  "limit": 100000,
  "current": 100000
}
```

The `kind` field is stable; you can match on it from your client to
decide whether to retry, archive, or surface a UI message.

## Overriding the limits

Every threshold is read from an environment variable at startup, so
self-hosted operators can dial the cap up (or down) without a code
change:

```bash
# .env or your systemd unit
MERIDIAN_LIMIT_NOTES_PER_PROJECT=500000
MERIDIAN_LIMIT_BODY_BYTES=500000
```

The variable names map 1:1 to the defaults table above with the
`MERIDIAN_LIMIT_` prefix.

On the **hosted tier (usemeridian.us)** the defaults are baseline.
If your workload needs more headroom, email
<hello@usemeridian.us> with a one-line description of what you're
doing — raises are routine and we usually turn them around within
the day.

## Why these specific numbers?

They're round numbers two to three orders of magnitude above what
any single team has ever come close to in production. A typical
project ships with a few hundred notes, a few thousand task rows,
fewer than ten thousand decisions over its entire lifetime. The
limits exist to catch the pathological cases — an infinite loop
appending the same note, a runaway HITL fanout — not to gate
features.
