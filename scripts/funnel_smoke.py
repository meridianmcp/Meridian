#!/usr/bin/env python3
"""Item 40 — nightly funnel smoke: walk the conversion funnel against a live
deployment and fail loudly if any stage breaks.

Unlike :mod:`scripts.smoke_test` (deploy-time, broad health gate), this checks
the *acquisition funnel* a visitor actually traverses, in order:

    landing → pricing → demo → waitlist signup → install-mcp

so a silent regression in any conversion step (a broken CTA, a 5xx on the
demo, a signup endpoint that stops accepting) surfaces overnight rather than
when a user reports it. Run nightly by ``.github/workflows/funnel-smoke.yml``,
which pages the admin (ntfy) on a non-zero exit.

Usage:
    pixi run python scripts/funnel_smoke.py --url https://usemeridian.us
    pixi run funnel-smoke   # alias in pixi.toml (defaults to prod)

Exit code: 0 if every funnel stage passes, 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
import uuid

# Cloudflare in front of prod 403s the default urllib UA, so present a
# browser-like UA on every request (matches scripts/smoke_test_signup.py).
_UA = "Mozilla/5.0 (Meridian-funnel-smoke)"


def check(name: str, passed: bool, detail: str = "") -> bool:
    status = "PASS" if passed else "FAIL"
    line = f"  [{status}] {name}"
    if detail:
        line += f" -- {detail}"
    print(line)
    return passed


def get(url: str, timeout: int = 15) -> tuple[int, str]:
    """Return (status_code, body). Returns (-1, error_msg) on network error."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        return -1, str(exc)


def post(url: str, body: dict, timeout: int = 15) -> tuple[int, str]:
    """POST JSON body. Returns (status_code, body_text)."""
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": _UA},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        return -1, str(exc)


def run_funnel(base_url: str) -> int:
    base = base_url.rstrip("/")
    results: list[bool] = []

    print(f"\nMeridian funnel smoke -- {base}\n")
    t0 = time.time()

    # Stage 1 — landing page loads and shows a primary conversion CTA.
    code, body = get(f"{base}/")
    low = body.lower()
    results.append(check(
        "1. landing loads with a signup/get-started CTA",
        code == 200 and ("get started" in low or "sign up" in low
                         or "waitlist" in low or "/auth/" in low),
        f"status={code}",
    ))

    # Stage 2 — pricing page loads with tier copy.
    code, body = get(f"{base}/pricing")
    low = body.lower()
    results.append(check(
        "2. pricing loads with tiers",
        code == 200 and ("free" in low or "solo" in low or "pro" in low),
        f"status={code}",
    ))

    # Stage 3 — demo renders the seeded dashboard shell (not an error page).
    code, body = get(f"{base}/demo")
    is_demo = "Meridian Demo" in body and "dashboard" in body.lower()
    results.append(check(
        "3. demo renders seeded dashboard",
        code == 200 and is_demo,
        f"status={code}, is_demo_shell={is_demo}",
    ))

    # Stage 4 — signup endpoint still accepts a new lead. Unique email so the
    # nightly run never collides with itself; 409 (already exists) also counts.
    test_email = f"funnel-smoke-{uuid.uuid4().hex[:8]}@meridian-test.local"
    code, body = post(f"{base}/waitlist", {"email": test_email})
    results.append(check(
        "4. waitlist signup accepts a new lead",
        code in (200, 201, 409),
        f"status={code}",
    ))

    # Stage 5 — install-mcp page (the post-signup activation step) loads.
    code, body = get(f"{base}/install-mcp")
    results.append(check(
        "5. install-mcp activation page loads",
        code == 200,
        f"status={code}",
    ))

    # Stage 6 — server is still healthy after the funnel walk (no 5xx induced).
    code, body = get(f"{base}/health")
    results.append(check(
        "6. /health still 200 after funnel walk",
        code == 200,
        f"status={code}",
    ))

    elapsed = time.time() - t0
    passed = sum(results)
    total = len(results)
    print(f"\n{'='*52}")
    if passed == total:
        print(f"  PASS: conversion funnel intact ({passed}/{total}) in {elapsed:.1f}s")
    else:
        print(f"  FAIL: {total - passed} funnel stage(s) broken "
              f"({passed}/{total}) in {elapsed:.1f}s")
    print(f"{'='*52}\n")
    return 0 if passed == total else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Meridian nightly funnel smoke")
    parser.add_argument(
        "--url",
        default="https://usemeridian.us",
        help="Base URL to test (default: https://usemeridian.us)",
    )
    args = parser.parse_args()
    sys.exit(run_funnel(args.url))


if __name__ == "__main__":
    main()
