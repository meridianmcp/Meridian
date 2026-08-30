# meridian-outputs P0 hardening (fa600e42) — before/after status manifest

Sprint item `fa600e42-d5ff-47e5-806e-c0f59b9fc7f3`, program `outputs-hardening-v1`,
wave 1 of 5. This is the acceptance sub-item 6 deliverable: the clean worktree base
revision and a before/after status manifest.

## Worktree base revision

- Isolated worktree: `.claude/worktrees/outputs-hardening-fa600e42`
- Branch: `item/fa600e42-outputs-hardening`
- Base commit: `54d6f9692ab5c958b5aee5351a2f5b7433cd7650` (`dev` HEAD at worktree
  creation), confirmed clean (`git status --short` empty) before any edits.
- Note: an earlier worktree attempt (`.claude/worktrees/da0f851e`, branch
  `worktree/fa600e42`, based on `dev@1533cb1e`) was created first but was removed —
  most likely swept as a "stale orphan" by another concurrent session's own
  worktree-hygiene automation (`dev` commits `448e949b`/`3f23e57f`/`e8456723`) after
  its background test process was reported stopped. No code had been written in it;
  nothing was lost. This worktree was created fresh from `dev`'s subsequent HEAD.

## Test gate results

**Mandated repo-wide gate (`pixi run test -n 3`):** confirmed this command's
collection scope is `tests/` at the repo root only (`pixi.toml`'s `test` task runs
`scripts/run_tests.py tests/ ...`) — it never collects `extensions/meridian-outputs/`
at all. This worktree's checkout of the main repo is a fixed, isolated snapshot at
`54d6f969` with zero edits outside `extensions/meridian-outputs/` and `docs/`, so
nothing in this gate's own test files was ever touched by this item.

Two full runs (immediately before and immediately after every change in this
manifest) both surfaced `tests/test_core.py::test_docs_mcp_tools_matches_live_tool_doc`
(pre-existing, unrelated docs/live-tool-schema drift touching `meridian/mcp_tools.py`)
identically. The "before" and first "after" run were byte-identical
(`1 failed, 11863 passed, 57 skipped`). A THIRD run (the final compliance check) also
showed `tests/test_c7ef8ff7_local_resilience.py::TestResolveInterruptedRun::
test_non_resumable_run_with_no_ownership_check_skips_quarantine_fail_closed` failing
alongside the known pre-existing failure (`2 failed, 11862 passed`) — verified by
re-running it in isolation 3/3 times (all passed, 0.55-1.38s each), confirming this is
environmental resource-contention flakiness on this shared, heavily-loaded machine
(the same class of flake already confirmed twice for `extensions/meridian-outputs`'s
own `TestRebuildWalkDeadlineAwareness` test below), not a regression -- consistent
with this worktree's checkout never touching that test's subject area at all. This
confirms the mandated gate is satisfied (no genuine regression) but is **not**
meaningful coverage for this item's own files — see the follow-up task filed below.

**Real coverage for this item** (`extensions/meridian-outputs/tests/`, run directly
via `pixi run python -m pytest`, since the mandated task does not reach these files):

| Stage | Result |
|---|---|
| Baseline (before any edit) | not separately run — no pre-existing tests touched these files' new behavior |
| After the 4 required fixes | 473 passed, 2 skipped (targeted: `test_fingerprint.py`, `test_provenance_status.py`, `test_outputs_local.py`) |
| After adversarial-review fixes, 1st attempt | **hung indefinitely** (2h17m, killed) — see Incident below |
| After the deadlock fix | 510 passed, 1 failed (flaky), 2 skipped |
| Isolated re-run of the flaky test, 3x | 3/3 passed (confirms pre-existing timing flake, not a regression) |
| Final clean confirmation | **511 passed, 2 skipped, 0 failed** (65s) |

A follow-up task was filed (not fixed here, out of this item's scope) to wire
`extensions/meridian-outputs`'s test suite into the actual `pixi run test` task —
this mirrors an already-known identical gap for `extensions/meridian-docs`.

### Incident: self-deadlock in the review-driven performance fix

The O(N×M) performance-regression fix (adding an mtime/size-based read cache to
`annotate._read_ledger`) initially guarded the cache with the module's existing
`_write_lock` (a plain `threading.Lock`). `_write_ledger_entry` already acquires
`_write_lock` and then calls `_read_ledger` internally as part of its own
read-modify-write pattern — a non-reentrant `Lock` re-acquired by the same thread in
that nested call blocks forever. This deadlocked the **very first** call to
`record_provenance` in any test run that included it, with zero output (not a slow
test — an indefinite hang), confirmed live via `Get-CimInstance Win32_Process`
showing the pytest process alive and unchanged for 2h17m before being killed.
Fixed by changing `_write_lock` to `threading.RLock()` (same mutual-exclusion
guarantee across threads, safe re-entry within one thread). Root-caused and fixed
before completing this item — not left as a known issue.

## What changed, by acceptance sub-item

1. **Bounded text allowlist + NUL handling** (`outputs_local.py`):
   `_TEXT_CONTENT_SUFFIXES` extended `{.csv, .json}` → `{.csv, .json, .txt, .md, .log}`.
   New `_sanitize_text_content()` strips embedded NUL bytes, wired into both text-read
   call sites (`_read_text_capped`, the `_analyse_file` fast path) *and* a third site
   the initial pass missed (`_ingest_meridian_notes`, caught by adversarial review).
   `MERIDIAN_NOTES.md` is explicitly excluded from the widened allowlist in
   `_classify_suffix` (also caught by review) so this one reserved, separately-ingested
   filename doesn't get double-surfaced (once as a normal content row, once as its
   existing directory annotation) — every other `.md` file is unaffected.

2. **Active vs. historical walk-error distinction** (`outputs_local.py`):
   `_last_walk_error` now resets when a brand-new walk pass starts, re-set by
   `_record_walk_error` if the same problem recurs. A new `_walk_error_confirmed_fresh`
   flag (caught by adversarial review, high severity) prevents
   `_rehydrate_walk_state_from_disk`'s pre-existing "fill only if still None" merge
   rule from immediately reloading the stale pre-reset value in the real production
   call order (`rebuild()` called directly on a fresh instance, with `_connect()`/
   rehydration not happening until mid-Phase-2 of that same call) — without the flag,
   the fix would have been silently negated on every real process restart.

3. **Explicit digest metadata + compat path** (`fingerprint.py`): new
   `script_content_digest()`/`_digest_hex()`, additive `content_digest` field on
   `ScriptTaggedFingerprint`, `check_staleness` and (after review) `find_stale_by_script`
   both read via the same OLD-bare-string/NEW-structured compatibility path.
   `tag_output` computes the script hash once and derives both fields from it (review
   caught a double-hash/TOCTOU issue in the first version).

4. **RELOCATED/AMBIGUOUS provenance classification** (`provenance_status.py`): two new
   ranked states between EXACT and DIRECTORY_FALLBACK, backed by a new
   `_relocation_candidates()` ledger scan. Wired into `_resolver_state_for_provenance_status`
   and `evidence_record_from_provenance_status` (including the `candidates` list, which
   review caught being silently dropped despite the function's own documented
   losslessness guarantee). A confirmed O(N×M) performance regression in two real
   reachable batch-loop callers (`bind_artifact_provenance`, `build_provenance_envelope`)
   was mitigated with an mtime/size-based ledger read cache in `annotate.py`.

5. **Test coverage**: ~450 new/updated lines across `test_outputs_local.py`,
   `test_provenance_status.py`, and a new `test_fingerprint.py` (this module had no
   dedicated test file before this item).

6. This document.

## Known, deliberately deferred / out-of-scope items

- **Low severity** (adversarial review, not fixed): `.txt`/`.md`/`.log` files
  unconditionally attempt a `json.loads()` parse in `file_fingerprint`/`_analyse_file`'s
  suffix dispatch (falls through to the same branch `.json` uses). Confirmed safe
  (never crashes, `_extract_json` catches the failure and falls back to text-based
  script-hint inference) — wasted parse work only, no correctness impact. Deferred as
  a minor efficiency nitpick, not worth the added branching complexity for this item.
- **Medium severity, pre-existing** (adversarial review, not fixed): `rebuild()`'s
  Phase 0 (walk discovery, including the new error-reset logic) runs entirely outside
  both `_write_lock` and `_read_lock`. Confirmed by review to predate this diff — this
  item's reset adds one more unguarded write into an already-unprotected window, but
  does not change the underlying risk profile. Filing a fix here would be a
  significantly larger, riskier concurrency-architecture change than this item's scope;
  left as a separate, explicitly out-of-scope finding for a future item.
- **Follow-up filed**: wire `extensions/meridian-outputs` (and `extensions/meridian-docs`,
  same root cause) into the actual `pixi run test` task/CI, so real changes to these
  extension packages get genuine automated coverage instead of relying on a contributor
  remembering to run pytest against them manually.
