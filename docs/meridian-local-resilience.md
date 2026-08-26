# Local resilience

`meridian/local_resilience.py` hardens local lifecycle hygiene for
operations that run entirely on the machine Meridian (or an extension like
`meridian-docs`/`meridian-outputs`) happens to be running on: Word/
LibreOffice renders, DOCX draft writes, output-index caches. None of this
touches hosted state — it's local filesystem and local process hygiene,
the "what happens when a crash interrupts a local operation" story.

## The problem this closes

`extensions/meridian-docs/meridian_docs/render_gate.py` already bounds a
single render *call* well: a Word COM or LibreOffice attempt that hangs is
terminated on a timeout, and the owned child process is cleaned up by pid.
What none of that code can protect against is the **host process itself**
dying mid-operation — killed, out of memory, power loss. When that happens:

- A `tempfile.TemporaryDirectory` never runs its finalizer, leaving a
  disposable render byproduct directory behind forever.
- A child process (WINWORD.EXE, soffice.bin) the crashed process spawned
  and would have terminated itself is now a true orphan.
- A prestage/draft write in progress has no record of whether it's safe to
  just retry, or whether the artifact needs a closer look before anything
  touches it again.

`local_resilience.py` makes all three **detectable and resolvable on the
next process start** — "restart scavenging" — plus two related local-
hygiene primitives: bounded quotas that degrade *visibly*, and a guard that
keeps temporary/draft artifacts off OneDrive.

## Durable temp-run manifests

Before starting a risky local operation, record it:

```python
from meridian import local_resilience as lr

run = lr.start_temp_run(
    manifest_dir="/path/to/.meridian-local-runs",
    kind="word_com_render",
    owner_pid=os.getpid(),
    resumable=True,          # safe to just retry from scratch if interrupted
    process_name="WINWORD.EXE",
    temp_paths=[],           # any temp/draft files this run owns
)
...
lr.complete_temp_run(manifest_dir, run["run_id"])   # or fail_temp_run(...)
```

The manifest (and every action taken on it) is written to a local, atomic
JSON ledger — `os.replace`, same idiom `annotate.py`'s provenance ledger and
`render_gate.py`'s own render-receipt ledger already use. `resumable` is
the caller's own honest declaration: a read-only render is always safe to
retry; a partially-written draft usually is not.

## Restart scavenging

At process/service startup, scan for runs a crash left stranded:

```python
scan = lr.scan_interrupted_runs(manifest_dir)
for run in scan["interrupted"]:
    outcome = lr.resolve_interrupted_run(
        manifest_dir, run["run_id"],
        quarantine_root="/path/to/.meridian-quarantine",
        ownership_check=my_ownership_classifier,   # fail-closed if omitted
    )
```

`scan_interrupted_runs` is pure detection — a run still `"started"` whose
`owner_pid` is no longer alive. `resolve_interrupted_run` is deterministic,
never a heuristic:

- **Resumable runs** resolve as `resolved_resume` — nothing is touched,
  the caller's own next attempt is expected to redo the work.
- **Non-resumable runs** resolve as `resolved_quarantine` — their
  `temp_paths` are handed to `meridian/worktree_cleanup.py`'s existing
  reversible archive-move primitives (`build_quarantine_manifest` +
  `quarantine_temp_outputs`), gated by the same fail-closed
  `ownership_check` contract that module already established: omit the
  classifier and nothing is moved, ever.
- If the run recorded an owned `process_name`, cleanup is attempted
  regardless of the resume/quarantine outcome — identity-checked via
  `terminate_owned_process` (pid + optional name verification) so a PID
  the OS has since reused for an unrelated process is never touched.

Every step — start, complete, process cleanup, quarantine — is appended as
an **auditable receipt** to the same ledger:

```python
lr.list_cleanup_receipts(manifest_dir, run_id=run["run_id"])
```

## Crash recovery for render_gate.py's own temp directories

`render_gate.py`'s Word-COM and LibreOffice backends each open a
`tempfile.TemporaryDirectory(prefix=render_gate.RENDER_TEMPDIR_PREFIX)`.
That self-cleans on a normal exception, but not a hard process kill.
`reap_stale_render_tempdirs` is the crash-recovery sweep:

```python
lr.reap_stale_render_tempdirs(max_age_seconds=3600.0)
```

The distinctive `meridian_render_gate_` prefix *is* the ownership signal
(the same way `.meridian-outputs-cache` is elsewhere in this codebase) —
no `ownership_check` needed. A directory is only removed once it's older
than `max_age_seconds`, so an in-progress render is never touched.

`meridian-docs` and `meridian-outputs` are standalone packages with zero
dependency on this `meridian` core package (they run via `uvx` on their
own), so this reaper can't *import* `render_gate.RENDER_TEMPDIR_PREFIX` —
its `prefix` default is the same literal, kept in sync by documented
convention on both sides, not by import.

## Bounded quotas that degrade visibly

```python
status = lr.enforce_local_quota(prestage_dir, max_bytes=500_000_000)
if not status["allowed"]:
    # status["reason"] is a real, actionable explanation -- never a silent drop.
    ...
```

`check_local_quota` is the read-only report; `enforce_local_quota` is the
write-gating wrapper a caller should call *before* writing a new
prestage/draft/temp file. Exhaustion is always an explicit `allowed=False`
with a reason — never a silently-dropped write, never unbounded disk
growth. `extensions/meridian-outputs/meridian_outputs/outputs_local.py`
ships a self-contained, same-shape companion,
`get_cache_quota_status(outputs_dir, ...)`, for its own
`.meridian-outputs-cache/` directory — that package can't import this
module either, for the same standalone-package reason above.

## OneDrive must never receive temporary or draft artifacts

```python
guard = lr.assert_disk_only_prestage_path(destination_path)
if not guard["allowed"]:
    raise RuntimeError(guard["reason"])
```

A partial/torn write can sync mid-write, and a draft can leak into a
shared cloud folder — `is_onedrive_path`/`assert_disk_only_prestage_path`
refuse a destination under a configured `OneDrive*` environment variable
root, or one that merely contains a `OneDrive`-shaped path segment (a
secondary, best-effort signal for a path authored on/for a different
machine). Any prestage/draft writer should call this before its first
write to a caller-supplied destination.

## Local paths never enter shared capability manifests

This module's own state — manifests, receipts, quota reports — lives only
in a local JSON ledger under a caller-chosen local directory, never fed
into `set_capability_manifest`/`db.set_project_capability_manifest`. The
one function that *does* build a capability-manifest-shaped entry,
`summarize_for_capability_manifest()`, takes no local-path parameter at
all and is self-validated through the real
`meridian.capability_manifest.normalize_capability` before being returned
— an accidental future edit that introduced a path-shaped field would
raise `CapabilityManifestError` immediately, not leak quietly into shared,
multi-machine project state.

## Design notes

- **Fail-closed by default, injected callables for anything dangerous** —
  the same pattern `worktree_cleanup.py`'s quarantine trio already
  established. Omitting `ownership_check` means "refuse", never "guess".
- **One PID-liveness convention** — `os.kill(pid, 0)`, the same catch
  tuple `worktree_cleanup._pid_is_alive` / `orphan_reaper.py` /
  `tunnel_client.py` already use.
- **No hard cross-package import.** `meridian-outputs` and `meridian-docs`
  are separately-installable, `uvx`-runnable packages with their own
  `pyproject.toml`. Anything shared across that boundary (a tempdir
  prefix, a quota-report shape) is a documented string/shape convention,
  never an import in either direction.
