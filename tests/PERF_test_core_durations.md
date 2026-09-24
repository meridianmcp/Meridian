# test-core duration regression — slowest-tests analysis (item bad568b5)

Follow-up to commit `696bf9d6`, which added `--durations=20` to the `test-core`
(and `test-postgres`) jobs in `.github/workflows/test.yml` so the CI duration
regression (**53s → 301s**) could be attributed to real per-test timings instead
of guessed. The instrumentation shipped, but the actual analysis was never
recorded. This note records it, from a real local run.

## How this was measured

Main-env interpreter, `test_core.py` only (the bulk of the suite), no xdist so the
per-test setup cost is visible and not smeared across workers:

```
.pixi/envs/default/python.exe -m pytest tests/test_core.py \
    --durations=25 -q -p no:cacheprovider --timeout=120
```

Result: `946 passed, 6 skipped in 313.26s` — i.e. `test_core.py` alone is ~313s
serially, which lines up with the observed regression.

## The genuinely slowest tests — and what "slow" actually means

The `--durations=25` table is the key finding, and it is *not* what a naive read
expects. The slowest **`call`** phases are small:

```
2.31s call     test_langgraph_checkpointer_put_graceful_failure
1.21s call     test_git_status_endpoint_returns_shape
```

Everything else in the top 25 is a **`setup`** phase of ~0.8–1.0s, spread across
many otherwise-trivial tests, e.g.:

```
1.18s setup    test_oauth_device_slow_down_on_fast_poll
1.03s setup    test_tunnel_plugins_check_returns_installed_flag_for_python
1.01s setup    test_timeline_tasks_newest_first
0.99s setup    test_health
0.94s setup    test_vtab_drawer_always_visible
...
```

`test_health` is a one-line `client.get("/health")`. Timed in isolation it is
`0.48s setup / 0.01s call`. **The cost is entirely fixture setup, not the test
body.**

## Root cause: the `client` fixture reloads `meridian.server` per test

`tests/conftest.py::client` ends with:

```python
import importlib
import meridian.server as server_module
server_module = importlib.reload(server_module)   # <-- per-test
with TestClient(server_module.app) as c:
    yield c
```

`importlib.reload(meridian.server)` re-executes the entire FastAPI app module —
all route decorators and every MCP tool registration — on **every** test that
requests `client`. In `test_core.py`, **390 of the ~946 tests use `client`**
(`grep -c "def test.*client"` = 390). At ~0.5–1.0s per reload that is roughly
**190–300s of pure module-reload overhead**, which dominates total runtime.

This also explains the *regression shape*: the reload cost scales with the size
of `meridian.server`. As routes/MCP tools were added over time, each reload got
heavier, so the same 390 `client` tests silently drifted 53s → 301s without any
single test becoming individually slow. `-n auto` in CI parallelizes this across
workers (so wall-clock in CI is lower than the 313s serial number here), but the
aggregate CPU cost is real and grows with every route added.

Secondary, smaller contributors:
- `test_langgraph_checkpointer_put_graceful_failure` (2.31s call) — the single
  slowest real test body; exercises a graceful-failure path in the langgraph
  checkpointer.
- `test_git_status_endpoint_returns_shape` (1.21s call) — shells out to `git`
  (subprocess spawn) inside the test body.

No evidence of `time.sleep`-based waits or network seams being the dominant
cost in `test_core.py`; the reload-per-test fixture is the overwhelming driver.

## Recommended follow-up (NOT done here — this item is investigation-only)

The obvious win is to stop reloading `meridian.server` on every `client` test.
Options, in rough order of payoff/safety:
1. Reload `meridian.server` **once per env-config**, not per test — most `client`
   tests set the same env (in-memory SQLite). A session-scoped app built once,
   with per-test state isolation via DB reset rather than module reload, would
   remove the bulk of the 190–300s. The reload exists so lifespan re-reads env
   vars; tests that need *different* env (Postgres, demo) are the minority and
   can opt into a fresh reload.
2. Failing that, split CI into fast/slow jobs so the `client`-heavy file doesn't
   gate quick feedback.

Either change is a behavioral refactor of a shared fixture touching hundreds of
tests and is out of scope for this investigation item; it should be its own
sprint item with a full-suite before/after run.

## test.yml status

`--durations=20` is present and retained on both `test-core` (line 31) and
`test-postgres` (line 91) so the next green run keeps capturing per-test timings.
No change required to keep the instrumentation; verified present as of this note.

## Follow-up (item a8ff4caa, 2026-09-24) — accretion hypothesis re-checked, already fixed

CI wall-clock for `test-core`/`test-postgres` was observed to have roughly
doubled again since August (7-8m -> 13-15m), with `meridian/server.py`'s
`lifespan()` accreting several sequential per-startup steps (doc-store open,
OAuth cache hydration, tenant re-key, tunnel-tenant notify loop) flagged as
the live suspect — plausible because those steps re-run on every single
`client`-fixture `TestClient` open (~390+ tests in `test_core.py` alone).

**Re-ran the methodology from this doc exactly**, on current `dev`
(`timeout 900 pixi run python -m pytest tests/test_core.py --durations=25
-p no:xdist --timeout=120 -q > durations.log 2>&1; echo "exit=$?"`, no pipe
to `tail` this time so the real exit code is visible):

```
1129 passed, 6 skipped in 182.04s (0:03:02)
exit=0
```

No hang. And — contrary to what the accretion hypothesis predicted — the
`--durations=25` table shows **zero setup-phase entries** in the slowest 25
(the July table above was almost entirely `setup`-phase entries at
~0.8-1.0s each). Every entry now is a `call`-phase cost from an individual
test's own body (a real subprocess spawn, a real network-call attempt with
timeout, etc.), and total serial wall-clock is **182s — lower than the July
baseline of 313s — despite ~190 more tests** having been added since.

**Why:** the exact accretion this item hypothesized was already fixed by two
commits that landed on `dev` in the 24 hours immediately before this
re-check (both already present in the `origin/dev` this investigation
branched from):

- `2c3fe304` (2026-09-23) — `perf(tests): default MERIDIAN_DOC_STORE_URL to
  :memory: in client fixture`. The lifespan's unconditional
  `open_doc_store_for(...)` call was resolving to a real on-disk SQLite
  sidecar per test (open + schema create + WAL/journal) — this, not the
  other lifespan steps, was the actual dominant cost.
- `0cbef4c9` (2026-09-24) — `fix(tests): stop leaking MERIDIAN_DOC_STORE_URL
  across xdist workers`, closing a gap where the `:memory:` default from the
  commit above could be defeated by a leaked real path from an earlier test
  in the same xdist worker process.

Both already have real regression coverage
(`tests/test_doc_store.py::test_client_fixture_defaults_doc_store_to_memory`,
plus the corrected env-var handling in
`tests/test_5fdf8858_no_double_json_encode.py`). This investigation adds one
more: `tests/test_a8ff4caa_lifespan_perf_budget.py` guards the *general
class* of bug (a wall-clock budget on repeated fresh `TestClient` opens)
rather than only the one already-fixed instance, so a *future* accretion of
this kind fails fast in CI instead of silently doubling wall-clock over two
months again.

**Real CI history** (`gh run list`/`gh run view` job timings on `dev`,
2026-09-20 through 2026-09-24) shows a directional improvement consistent
with — though not yet a large enough sample to fully confirm — these fixes:
`test-core` job duration was ~11-12m in the runs before either fix (Sep 20)
and ~9-10m in the runs after both landed (Sep 24). Small sample (the fixes
are less than a day old as of this note), so treat this as corroborating,
not conclusive on its own.

**NOT fixed by this investigation, explicitly out of scope:**
- `test-postgres` is not merely slow — in every run sampled in the Sep 20-24
  window (both before and after the two fixes above), it **concluded
  `failure`**, not just a long duration. That looks like a separate,
  more fundamental correctness/flakiness problem, independent of this
  item's wall-clock-perf question, and needs its own investigation.
- The separate catastrophic-hang pattern (`scripts/run_tests.py`'s
  `post_results_hang` timeout_kind; see the 2026-09-17 incident,
  commits `288dbae3`/`f74e95a1`) is NOT addressed here. This re-check's
  clean `exit=0` with a complete durations table is at least evidence
  the hang did not reproduce in THIS run, but one clean run does not rule
  out an intermittent leak-driven hang — see
  `reference_aiosqlite_exit_hang_diagnosis` for the follow-up technique
  if it recurs.

**Conclusion:** no further code fix was needed for the specific hypothesis
this item raised — it was already fixed by other in-flight work before this
item was picked up. Closing as verified-already-fixed, with one added
regression test guarding the general class of bug going forward.
