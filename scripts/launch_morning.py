#!/usr/bin/env python3
"""Launch morning script -- e38a3888.

Run this the morning of HN launch. Unsets SITE_PASSWORD, waits for
the machine to restart, then runs the full 12-check playtester flow.

Usage:
    pixi run launch [--app meridian-hosted] [--url https://usemeridian.us] [--dry-run]
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

GREEN = "\033[32m"
RED = "\033[31m"
BOLD = "\033[1m"
RESET = "\033[0m"

BASE_URL = "https://usemeridian.us"
SCRIPTS_DIR = Path(__file__).parent


def step(n: int, total: int, msg: str) -> None:
    print(f"\n[{n}/{total}] {msg}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Meridian HN launch morning checklist")
    parser.add_argument("--app", default="meridian-hosted", help="Fly app name")
    parser.add_argument("--url", default=BASE_URL, help="Base URL to check")
    parser.add_argument("--dry-run", action="store_true", help="Skip flyctl + wait")
    args = parser.parse_args()

    base = args.url.rstrip("/")

    print("\n" + "=" * 60)
    print(f"  {BOLD}Meridian HN Launch Morning Checklist{RESET}")
    print("=" * 60)

    # ── Step 1: Unset SITE_PASSWORD ──────────────────────────────────────────
    step(1, 3, f"Unsetting SITE_PASSWORD on {args.app}...")
    if args.dry_run:
        print(f"  [DRY-RUN] flyctl secrets unset SITE_PASSWORD --app {args.app}")
    else:
        result = subprocess.run(
            ["flyctl", "secrets", "unset", "SITE_PASSWORD", "--app", args.app],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode == 0:
            print(f"  [{GREEN}OK{RESET}] SITE_PASSWORD unset")
        else:
            stderr = result.stderr.strip()[:200]
            print(f"  [{RED}WARN{RESET}] flyctl returned non-zero: {stderr}")
            print("  Continuing with smoke tests regardless...")

    # ── Step 2: Wait for restart ──────────────────────────────────────────────
    step(2, 3, "Waiting 10s for machine to restart...")
    if args.dry_run:
        print("  [DRY-RUN] Skipping wait")
    else:
        for remaining in range(10, 0, -1):
            print(f"  {remaining}s...", end="\r", flush=True)
            time.sleep(1)
        print("  Done.      ")

    # ── Step 3: 12-check playtester smoke test ────────────────────────────────
    step(3, 3, f"Running 12-check smoke test against {base}...")
    print()

    playtester = SCRIPTS_DIR / "test_playtester_flow.py"
    result = subprocess.run(
        [sys.executable, str(playtester), "--url", base],
        text=True,
        timeout=120,
    )
    passed = result.returncode == 0

    # ── Result banner ─────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    if passed:
        print(f"\n  {BOLD}{GREEN}██████████████████████████████████{RESET}")
        print(f"  {BOLD}{GREEN}   GO — SITE IS LIVE AND HEALTHY   {RESET}")
        print(f"  {BOLD}{GREEN}██████████████████████████████████{RESET}\n")
        print(f"  Post Show HN now → {BOLD}news.ycombinator.com/submit{RESET}")
        print("  Post time: 9-10am ET from home IP")
        print("  Upvote once immediately after posting")
    else:
        print(f"\n  {BOLD}{RED}████████████████████████████████████{RESET}")
        print(f"  {BOLD}{RED}  NO-GO — CHECKS FAILED, DO NOT POST  {RESET}")
        print(f"  {BOLD}{RED}████████████████████████████████████{RESET}\n")
        print("  Fix the failing checks above before posting.")
    print("=" * 60 + "\n")

    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
