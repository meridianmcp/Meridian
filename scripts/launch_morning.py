#!/usr/bin/env python3
"""Launch morning script -- e38a3888.

Run this the morning of HN launch. Unsets SITE_PASSWORD, waits for
the machine to restart, then verifies all key endpoints are healthy.

Usage:
    pixi run python scripts/launch_morning.py [--app meridian-hosted] [--dry-run]
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
import urllib.error
import urllib.request

PASS = "\033[32mOK\033[0m"
FAIL = "\033[31mX\033[0m"

BASE_URL = "https://usemeridian.us"


def check_url(url: str, expected_text: str | None = None, timeout: int = 20) -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            body = r.read().decode()
            if expected_text and expected_text not in body:
                return False, f"missing '{expected_text}' in response"
            return True, f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except Exception as exc:
        return False, str(exc)


def main() -> int:
    parser = argparse.ArgumentParser(description="Meridian HN launch morning checklist")
    parser.add_argument("--app", default="meridian-hosted", help="Fly app name")
    parser.add_argument("--url", default=BASE_URL, help="Base URL to check")
    parser.add_argument("--dry-run", action="store_true", help="Skip flyctl commands")
    args = parser.parse_args()

    base = args.url.rstrip("/")
    failures = 0

    print("\n" + "=" * 55)
    print("  Meridian HN Launch Morning Checklist")
    print("=" * 55 + "\n")

    # Step 1: Unset SITE_PASSWORD
    if args.dry_run:
        print(f"[DRY-RUN] Would run: flyctl secrets unset SITE_PASSWORD --app {args.app}")
    else:
        print(f"[1/6] Unsetting SITE_PASSWORD on {args.app}...")
        result = subprocess.run(
            ["flyctl", "secrets", "unset", "SITE_PASSWORD", "--app", args.app],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode == 0:
            print(f"  [{PASS}] SITE_PASSWORD unset")
        else:
            print(f"  [{FAIL}] flyctl failed: {result.stderr.strip()[:200]}")
            print("  Continuing with checks anyway...")

    # Step 2: Wait for restart
    if not args.dry_run:
        print("\n[2/6] Waiting 30s for machine to restart...")
        for i in range(30, 0, -5):
            print(f"  {i}s remaining...", end="\r")
            time.sleep(5)
        print("  Done waiting.          ")
    else:
        print("[DRY-RUN] Skipping 30s wait")

    # Step 3: Health check
    print(f"\n[3/6] Checking {base}/health...")
    ok, detail = check_url(f"{base}/health")
    status = PASS if ok else FAIL
    print(f"  [{status}] /health -- {detail}")
    if not ok:
        failures += 1

    # Step 4: Demo check
    print(f"\n[4/6] Checking {base}/demo...")
    ok, detail = check_url(f"{base}/demo", expected_text="backend-api-v2")
    status = PASS if ok else FAIL
    print(f"  [{status}] /demo -- {detail}")
    if not ok:
        failures += 1

    # Step 5: Pricing check
    print(f"\n[5/6] Checking {base}/pricing...")
    ok, detail = check_url(f"{base}/pricing", expected_text="Free")
    status = PASS if ok else FAIL
    print(f"  [{status}] /pricing -- {detail}")
    if not ok:
        failures += 1

    # Step 6: Auth login check
    print(f"\n[6/6] Checking {base}/auth/login...")
    ok, detail = check_url(f"{base}/auth/login")
    status = PASS if ok else FAIL
    print(f"  [{status}] /auth/login -- {detail}")
    if not ok:
        failures += 1

    print("\n" + "=" * 55)
    if failures == 0:
        print(f"  [{PASS}] Site is live and healthy -- ready to post.")
        print("\n  HN post template: SHOW_HN.md")
        print("  Post time: 9-10am ET from home IP")
        print("  Upvote once immediately after posting")
    else:
        print(f"  [{FAIL}] {failures} check(s) FAILED -- do NOT post until fixed.")
    print("=" * 55 + "\n")

    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
