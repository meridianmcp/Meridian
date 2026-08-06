# Tunnel child MCP runtime contract (preflight)

Status: standalone module, **not yet wired into the live spawn path**. See
"Integration boundary" below.

## Why

Live tunnel evidence (2026-08-05): an isolated `uvx --from <path>` child
resolved `mcp==2.0.0`, which removed the `mcp.server.fastmcp` import that
`meridian-docs` and `meridian-outputs` depend on. The child crashed on
import, and the only visible symptom to a user was a misleading early
connect/disconnect in the client. [106caa76](../CLAUDE.md) pinned the SDK
major (`mcp>=1.27,<2`) to close the immediate hole; this contract adds a way
to *detect* the next such incompatibility before a slot is ever advertised,
instead of after a user hits it live.

## What `meridian/tunnel_preflight.py` does

Given a slot's label and launch command, `preflight_child_entrypoint()`:

1. Resolves the effective executable (`resolve_effective_executable`, a
   `shutil.which` lookup — diagnostic only, not authoritative).
2. Spawns the command with `stdin` piped, using the same
   `tunnel_client._spawn_kwargs()` Windows process-group handling the live
   spawn path already uses.
3. Closes `stdin` and waits up to a budget (`timeout`). A well-behaved MCP
   stdio server treats stdin EOF as shutdown and exits 0; an import-time
   dependency failure crashes almost instantly with a traceback on stderr —
   both resolve well inside a short budget.
4. Classifies the outcome into one of three buckets, reusing
   `tunnel_client`'s existing pure classifiers as the single source of truth:
   - **Deterministic failure** — `SlotState.DEPENDENCY_MISSING` (missing
     launcher, or a `ModuleNotFoundError`/`ImportError` signature in stderr —
     via `_classify_launch_exception` / `_classify_stderr_signature`) or
     `SlotState.CHILD_CRASHED` (nonzero exit, no recognized signature).
     `recommend_quarantine=True`.
   - **Cold-start timeout** — `SlotState.STARTUP_TIMEOUT`: still running when
     the budget expires. Explicitly **not** deterministic —
     `recommend_quarantine=False`. Retry with a larger budget, don't
     quarantine.
   - **Healthy** — `SlotState.HEALTHY`, exit code 0.

`preflight_for_label(label, command)` is a convenience wrapper that derives
the timeout from `tunnel_client._cold_spawn_budget(label)`, so cold-fetch
slots (`dc`/`ppt`/`word`/`docs`/`zotero`) automatically get the same larger
allowance the live spawn path already gives them — one source of truth for
cold-fetch awareness, not a second copy of the slot list.

## Machine-readable result / human-readable reason

`PreflightDiagnostic` (frozen dataclass) carries both:

- `state` (a `tunnel_client.SlotState` value — `.value` is the wire-safe
  string), `reason` (short code), `exit_code`, `duration_seconds`,
  `stdout_tail`/`stderr_tail` (last 4000 chars) — the machine-readable half.
- `human_reason` — a plain-English sentence naming the slot and what
  happened, suitable for a dashboard hint (matches the existing
  `_preflight_failure_hint` convention in `tunnel_client.py`).
- `as_dict()` — JSON-serializable form for logging/API responses.

## Quarantine (advisory, not wired to live state)

`PreflightQuarantineTracker(threshold=N)` counts **consecutive**
`recommend_quarantine=True` results per label and returns
`QuarantineDecision(quarantined=True, ...)` once the streak hits the
threshold. A cold-start timeout never counts toward the streak, and any
healthy or non-deterministic result resets it. The tracker is in-memory,
per-instance, and does **not** touch `SlotProxy`'s own `SlotState.QUARANTINED`
lifecycle — it is the decision logic a future integration item calls, not a
second source of truth for live slot state.

## Integration boundary (deliberately deferred)

This item implements the preflight module and its focused tests only. It
does **not** modify `SlotProxy`, `resolve_plugins`, or any other
`meridian/tunnel_client.py` state, for two reasons:

1. **Concurrency** — `meridian/tunnel_client.py` is a high-contention file
   with other tunnel-control-plane sprint items in flight concurrently
   (idle-restart/recovery, an OpenAI Secure MCP Tunnel transport adapter).
   Editing it here would risk a claim conflict or rebase churn against work
   this item has no visibility into.
2. **Scope** — wiring a pre-advertisement check into the live spawn/slot-
   advertisement path is a control-plane behavior change (when does a slot
   become visible to a client?) that deserves its own review, once the
   concurrent edits above have landed and the integration point is stable.

A follow-up item should call `preflight_for_label()` (or
`preflight_child_entrypoint()` directly) before a slot is advertised, and
feed the result into a `PreflightQuarantineTracker` (or wire
`QuarantineDecision` into `SlotState.QUARANTINED` directly) using the
existing `claim_file`/`claim_symbol` boundaries on `meridian/tunnel_client.py`.

## Non-goals of this item

- Does not alter the Serena command or any existing tunnel control-plane
  behavior.
- Does not change which Windows shell-wrapper vs. direct-executable strategy
  a slot uses (tracked separately).
- Does not persist quarantine state across process restarts.
