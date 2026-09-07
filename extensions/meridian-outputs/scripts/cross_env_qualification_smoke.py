"""Cross-environment qualification smoke test (gap register MO-LIM-03).

MO-LIM-03 asked for evidence that Meridian-Outputs' convergence/qualification
behavior is not an artifact of one specific machine -- same-machine
environment tampering had already been tested, but a genuinely different
OS/filesystem/hardware environment had not. The real 660,153-file/466 GiB
SUT_Compressed qualification (see the companion meridian-outputs-research
repo's docs/meridian-outputs-post-hardening-results.md section 5.5) cannot
run here: that corpus is local, real research data, never moved off the
machine it lives on, and CI runners don't have the disk for it anyway.

This script is the honest, declared substitute: it generates a small,
synthetic, disposable corpus on whatever machine it runs on, then drives the
SAME core protocol the real qualification harness uses (repeatedly call
rebuild() until get_convergence_state().converged is True), asserting a
clean, complete, exact-count convergence. Run identically on this repo's own
Windows dev machine and on a GitHub Actions ubuntu-latest runner, a match
between the two is real (if modest-scale) cross-machine/cross-OS evidence --
not a claim about the 466 GiB regime specifically.
"""

from __future__ import annotations

import os
import random
import string
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from meridian_outputs import outputs_local as ol  # noqa: E402

FILE_COUNT = 3000
MAX_CALLS = 50
SUFFIXES = (".csv", ".json", ".txt", ".md")


def _generate_corpus(corpus_dir: str) -> None:
    rng = random.Random(42)  # fixed seed -- same synthetic corpus every run
    for i in range(FILE_COUNT):
        suffix = SUFFIXES[i % len(SUFFIXES)]
        body = "".join(rng.choices(string.ascii_lowercase, k=80))
        path = os.path.join(corpus_dir, f"file_{i:05d}{suffix}")
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"col_{i}\n{body}\n")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="meridian-outputs-cross-env-") as tmp:
        corpus_dir = os.path.join(tmp, "corpus")
        os.makedirs(corpus_dir, exist_ok=True)
        print(f"generating {FILE_COUNT} synthetic files under {corpus_dir}")
        _generate_corpus(corpus_dir)

        db_path = os.path.join(tmp, "index.duckdb")
        idx = ol.OutputsFtsIndex(corpus_dir, db_path=db_path)
        calls = 0
        converged = False
        started = time.monotonic()
        state = None
        try:
            while not converged and calls < MAX_CALLS:
                idx.rebuild()
                state = idx.get_convergence_state()
                converged = state.converged
                calls += 1
            elapsed = time.monotonic() - started

            print(
                f"calls={calls} converged={converged} "
                f"indexed_count={state.indexed_count if state else None} "
                f"expected_count={state.expected_count if state else None} "
                f"elapsed_s={elapsed:.2f} platform={sys.platform}"
            )

            if not converged:
                print(f"FAIL: did not converge within {MAX_CALLS} calls")
                return 1
            if state is None or state.indexed_count != FILE_COUNT:
                print(
                    f"FAIL: indexed_count {state.indexed_count if state else None} "
                    f"!= expected {FILE_COUNT}"
                )
                return 1
            if state.last_error is not None:
                print(f"FAIL: last_error is set on a converged index: {state.last_error}")
                return 1

            hits = idx.search("col_0")
            if not hits:
                print("FAIL: a known term returned zero search hits on a converged index")
                return 1
        finally:
            idx.close()

        print("CROSS-ENVIRONMENT QUALIFICATION SMOKE TEST: PASS")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
