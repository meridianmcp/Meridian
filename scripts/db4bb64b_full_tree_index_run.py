#!/usr/bin/env python
"""db4bb64b -- full, unimpaired end-to-end index run over the ENTIRE
SUT_Compressed tree (244,191 real files), measuring real wall-clock time
to full convergence, after the queued perf fixes (4f78e70 pyarrow bulk
insert, 7fee82e scandir walk, 4972a6d bounded regex fallback, c73c0dd7
Tantivy heap_size, a849e3d5 cpu_count workers, 3535b9ad configurable
_MAX_BATCH, 1a799e52 db_write_error surfacing) all landed.

Deliberately NOT scripts/test_outputs_indexing.py -- that script generates
a SYNTHETIC tree unconditionally and would pollute this real research data
directory with fake files. This script only READS the real tree; the only
write is OutputsFtsIndex's own normal, self-contained
<outputs_dir>/.meridian-outputs-cache/index.duckdb (the same cache path
every real production call already uses -- not something special to this
script).

Run it directly::

    pixi run python scripts/db4bb64b_full_tree_index_run.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
EXT_PATH = REPO_ROOT / "extensions" / "meridian-outputs"
if str(EXT_PATH) not in sys.path:
    sys.path.insert(0, str(EXT_PATH))

from meridian_outputs.outputs_local import OutputsFtsIndex  # noqa: E402

REAL_TREE = r"C:\Users\13144\Documents\Masters_Thesis\datasets\SUT_Compressed"
PER_CALL_BUDGET_S = 60.0
MAX_CALLS = 500  # safety cap; a real run should converge in far fewer calls


def converged(idx: OutputsFtsIndex) -> bool:
    return (
        (idx._walk_state is None or idx._walk_state.exhausted)
        and not idx.last_rebuild_partial
        and not idx._pending_stale
        and not idx._fts_pending
    )


def main() -> int:
    root = Path(REAL_TREE)
    if not root.is_dir():
        print(f"FATAL: {REAL_TREE!r} does not exist or is not a directory.")
        return 2

    n_files_hint = sum(1 for _ in root.rglob("*") if _.is_file())
    print("=== db4bb64b: full SUT_Compressed tree index run ===")
    print(f"Target: {REAL_TREE}")
    print(f"Real file count under target (informational, via rglob): {n_files_hint}")
    print(f"Per-call budget: {PER_CALL_BUDGET_S}s, max {MAX_CALLS} calls")

    idx = OutputsFtsIndex(str(root))
    total_start = time.monotonic()
    calls = 0
    history: list[dict] = []
    try:
        while calls < MAX_CALLS:
            calls += 1
            call_start = time.monotonic()
            total_indexed = idx.rebuild(max_seconds=PER_CALL_BUDGET_S)
            call_elapsed = time.monotonic() - call_start
            row = {
                "call": calls,
                "elapsed_s": round(call_elapsed, 3),
                "total_indexed": total_indexed,
                "total_in_index": len(idx._row_cache),
                "partial": idx.last_rebuild_partial,
                "fts_pending": idx._fts_pending,
                "pending_stale": len(idx._pending_stale),
                "walk_exhausted": (idx._walk_state is None or idx._walk_state.exhausted),
                "db_write_error": idx.last_db_write_error,
            }
            history.append(row)
            print(
                f"  call {calls}: {call_elapsed:.2f}s, indexed={total_indexed}, "
                f"in_index={row['total_in_index']}, partial={row['partial']}, "
                f"fts_pending={row['fts_pending']}, pending_stale={row['pending_stale']}, "
                f"walk_exhausted={row['walk_exhausted']}, "
                f"db_write_error={row['db_write_error']!r}"
            )
            if row["db_write_error"]:
                print(f"WARNING: DB write error surfaced on call {calls}: {row['db_write_error']}")
            if converged(idx):
                break

        total_elapsed = time.monotonic() - total_start
        print("\n=== Result ===")
        if converged(idx):
            print(
                f"CONVERGED after {calls} call(s), total wall-clock: "
                f"{total_elapsed:.2f}s ({total_elapsed / 60:.2f} min). "
                f"Final total_in_index={len(idx._row_cache)}."
            )
            # Prove the FTS index is actually queryable, not just DB rows written.
            search_result = idx.search("a", limit=1)
            print(f"Post-convergence search smoke check returned {len(search_result)} hit(s) (sanity only).")
            return 0
        print(
            f"DID NOT CONVERGE within {MAX_CALLS} calls / "
            f"{total_elapsed:.2f}s wall-clock. Last state: "
            f"partial={idx.last_rebuild_partial}, fts_pending={idx._fts_pending}, "
            f"pending_stale={len(idx._pending_stale)}."
        )
        return 1
    finally:
        idx.close()


if __name__ == "__main__":
    sys.exit(main())
