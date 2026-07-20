#!/usr/bin/env python
"""Outputs-indexing test harness -- fast, MCP-free, iterative by design.

Adam's explicit ask (2026-07-19): "a similar comprehensive well defined
testing, fail, find new broken code, return approach" for outputs indexing,
mirroring tunnel_smoke_test.py's structure -- but scoped for what this
actually is: pure Python logic (OutputsFtsIndex.rebuild() and friends in
extensions/meridian-outputs/meridian_outputs/outputs_local.py) with zero
MCP/tunnel dependency. Testing this through the full MCP stack wastes real
time for no benefit -- confirmed live: the tunnel harness was still running
19+ minutes into a single cycle while a direct REPL import-and-call test
found and confirmed 6ba77ada's actual root cause in under 2 minutes.

Run it directly::

    pixi run python scripts/test_outputs_indexing.py
    pixi run python scripts/test_outputs_indexing.py --n-files 200000 --fresh

WHY A SYNTHETIC TREE, NOT ONE OF ADAM'S REAL DIRECTORIES
-------------------------------------------------------------------------
The original bug reports (d9c76caa, c2021725) were confirmed against real
trees (205,542 files and 66,197 files respectively), but a permanent,
reusable regression test can't depend on a specific person's real directory
existing at a specific path -- it wouldn't run in CI, on a fresh clone, or
for anyone else. This generates a synthetic tree of the requested size once
(cached across runs by default) -- real files on real disk, exercising the
exact same os.walk/os.stat/ThreadPoolExecutor/Tantivy code paths, just with
trivial content. This is deliberately NOT a pytest fixture (tests/ should
stay CI-fast) -- this is an on-demand diagnostic script for exactly the
"iterate on a real bug against a realistically large tree" workflow this
was built for.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
EXT_PATH = REPO_ROOT / "extensions" / "meridian-outputs"
if str(EXT_PATH) not in sys.path:
    sys.path.insert(0, str(EXT_PATH))

from meridian_outputs.outputs_local import (  # noqa: E402
    DEFAULT_REBUILD_BUDGET_SECONDS,
    OutputsFtsIndex,
    _iter_safe_output_files,
)

DEFAULT_N_FILES = 70_000
DEFAULT_SYNTH_DIR = REPO_ROOT / "tmp" / "synth_outputs_test"
DEFAULT_DB_PATH = REPO_ROOT / "tmp" / "synth_outputs_test_index.duckdb"
# 6ba77ada -- imported directly (not a hardcoded duplicate literal) so this
# can never silently drift from the real production default again: this
# value previously hardcoded 5.0 with a comment claiming it "matches
# DEFAULT_REBUILD_BUDGET_SECONDS in production", but production's actual
# default had already been raised to 130.0 by 5845cc6d/d9c76caa -- 5.0 was
# stale, not a deliberately-chosen worst case. rebuild()'s only real
# production caller (search_outputs()) always passes
# DEFAULT_REBUILD_BUDGET_SECONDS unless a caller overrides it, so the
# default run of this script should exercise THAT budget to give an
# accurate signature of real behaviour. Pass --budget-s 5.0 explicitly to
# still stress-test an aggressive worst-case budget on demand.
DEFAULT_BUDGET_S = DEFAULT_REBUILD_BUDGET_SECONDS
# 6ba77ada -- rebuild()'s walk is deliberately batch-capped
# (_ResumableFileWalk._MAX_BATCH, currently 2000 paths/call) as a SAFETY
# bound independent of max_seconds: enumerating directory entries is cheap
# enough that an unbounded, deadline-only-gated drain() could hand back tens
# of thousands of paths in one call on a fast disk -- far more than Phase
# 1/2's real per-file I/O can analyse and persist in the same call's
# remaining budget (confirmed live: an uncapped batch made the backlog grow
# faster than it could ever shrink). That means a cold 70k-file tree
# converges in a bounded, predictable, roughly-linear number of calls
# (70_000 / 2_000 = 35) rather than in one or two -- by design, not because
# anything is stuck. 50 gives comfortable headroom over that exact figure
# for run-to-run variance (Phase 1/2 not perfectly clearing every batch)
# without letting a genuine regression (permanent stall) go unnoticed.
MAX_CALLS = 50


def generate_synth_tree(target_dir: Path, n_files: int, *, fresh: bool = False) -> None:
    """Create *n_files* trivial real files under *target_dir*, reusing an
    existing tree of sufficient size unless *fresh* is set. Real files on
    real disk (not e.g. a tmpfs mock) so os.walk/os.stat behave identically
    to production -- that fidelity is the whole point of this test shape.
    """
    if fresh and target_dir.exists():
        shutil.rmtree(target_dir)
    if target_dir.is_dir():
        existing = sum(len(files) for _, _, files in os.walk(target_dir))
        if existing >= n_files:
            print(f"reusing existing synth tree ({existing} files) at {target_dir}")
            return
    print(f"generating {n_files} synthetic files at {target_dir} "
          "(one-time cost, cached across runs)...")
    t0 = time.monotonic()
    for i in range(n_files):
        sub = target_dir / f"batch_{i // 1000:04d}"
        sub.mkdir(parents=True, exist_ok=True)
        # .csv (not .txt): outputs_local._TEXT_CONTENT_SUFFIXES only indexes
        # actual file CONTENT into the FTS body for .csv/.json -- anything
        # else (including .txt) only gets its filename indexed (see
        # _content_for_fts). A .txt extension here would make the
        # search("value=42") convergence check below permanently
        # unsatisfiable regardless of how well rebuild() itself behaves,
        # silently masking the very convergence signal this script exists
        # to observe.
        (sub / f"file_{i:07d}.csv").write_text(
            f"synthetic output file {i}\nrun_id=test\nvalue={i * 7 % 997}\n"
        )
    print(f"  generated in {time.monotonic() - t0:.1f}s")


def time_bare_walk(outputs_dir: Path) -> float:
    """Time _iter_safe_output_files() in isolation -- the exact measurement
    that found 6ba77ada: this function has zero deadline awareness and can
    alone exceed the whole rebuild() budget on a large tree, regardless of
    whether Phase 1/Tantivy are working correctly. A permanent regression
    check for this specific class of "step outside the deadline-checked
    region silently eats the whole budget" bug.
    """
    t0 = time.monotonic()
    paths = _iter_safe_output_files(str(outputs_dir))
    elapsed = time.monotonic() - t0
    print(f"  bare walk: {len(paths)} paths in {elapsed:.2f}s")
    return elapsed


def run_rebuild_cycles(
    outputs_dir: Path, db_path: Path, *, budget_s: float, n_files: int,
    max_calls: int = MAX_CALLS,
) -> dict:
    """Call rebuild() repeatedly with a tight deadline, exactly mirroring
    how a real MCP client hits this (many short-budget calls, not one
    unbounded one), and report structured convergence data.

    Returns a dict with pass/fail plus enough detail to diagnose a failure
    without re-running -- mirrors tunnel_smoke_test.py's SlotResult shape
    in spirit (a structured result, not just print statements), kept as a
    plain dict here since this script has no other consumer of the type yet.
    """
    idx = OutputsFtsIndex(str(outputs_dir), db_path=str(db_path))
    history: list[dict] = []
    try:
        for call_num in range(1, max_calls + 1):
            t0 = time.monotonic()
            n = idx.rebuild(max_seconds=budget_s)
            elapsed = time.monotonic() - t0
            hits = idx.search("value=42", limit=5)
            record = {
                "call": call_num, "rows": n, "elapsed_s": round(elapsed, 2),
                "partial": idx.last_rebuild_partial, "fts_built": idx._fts_built,
                "fts_pending": idx._fts_pending, "search_hits": len(hits),
            }
            history.append(record)
            print(f"  call {call_num}: rows={n} elapsed={elapsed:.2f}s "
                  f"partial={record['partial']} fts_built={record['fts_built']} "
                  f"search_hits={record['search_hits']}")
            if n >= n_files and idx._fts_built and not idx._fts_pending and hits:
                return {"converged": True, "calls_to_converge": call_num, "history": history}
        return {"converged": False, "calls_to_converge": None, "history": history}
    finally:
        idx.close()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    p.add_argument("--n-files", type=int, default=DEFAULT_N_FILES)
    p.add_argument("--budget-s", type=float, default=DEFAULT_BUDGET_S,
                    help="Per-call rebuild() deadline, matches production default.")
    p.add_argument("--max-calls", type=int, default=MAX_CALLS)
    p.add_argument("--fresh", action="store_true",
                    help="Regenerate the synthetic tree even if a large-enough one exists.")
    p.add_argument("--synth-dir", default=str(DEFAULT_SYNTH_DIR))
    p.add_argument("--db-path", default=str(DEFAULT_DB_PATH))
    args = p.parse_args()

    outputs_dir = Path(args.synth_dir)
    db_path = Path(args.db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if Path(args.db_path).exists() and args.fresh:
        Path(args.db_path).unlink()

    print("=== Outputs indexing test ===")
    generate_synth_tree(outputs_dir, args.n_files, fresh=args.fresh)

    print("\n--- Bare-walk deadline-awareness check (6ba77ada) ---")
    walk_s = time_bare_walk(outputs_dir)
    walk_finding = None
    if walk_s > args.budget_s:
        walk_finding = (
            f"FINDING: bare walk alone ({walk_s:.1f}s) exceeds the rebuild budget "
            f"({args.budget_s:.1f}s) -- Phase 1 can never get a meaningful chance "
            "to run. This is 6ba77ada's exact signature."
        )
        print(f"  {walk_finding}")

    print("\n--- rebuild()/search() convergence over repeated tight-budget calls ---")
    result = run_rebuild_cycles(
        outputs_dir, db_path, budget_s=args.budget_s,
        n_files=args.n_files, max_calls=args.max_calls,
    )

    print("\n=== Result ===")
    if result["converged"]:
        print(f"PASS: converged after {result['calls_to_converge']} call(s) -- "
              f"all {args.n_files} files indexed, FTS built, search returning real hits.")
        return 0
    print(f"FAIL: did not converge within {args.max_calls} calls.")
    if walk_finding:
        print(f"Likely cause: {walk_finding}")
    else:
        print("Bare walk was within budget -- failure is elsewhere in Phase 1/2 "
              "or the Tantivy commit path. Inspect the per-call history above.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
