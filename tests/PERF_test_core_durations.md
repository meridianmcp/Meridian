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
