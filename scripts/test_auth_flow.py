#!/usr/bin/env python3
"""Auth flow tests -- 56e04df2.

Uses Playwright (headless Chrome) to test auth pages against live/local site.
Falls back to urllib checks if Playwright is not installed.

Usage:
    pixi run python scripts/test_auth_flow.py [--url https://usemeridian.us]

Install Playwright: pip install playwright && playwright install chromium
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
import urllib.error
import urllib.request

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
SKIP = "\033[33mSKIP\033[0m"


def get(url: str, timeout: int = 20, allow_redirects: bool = True) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except Exception as exc:
        return -1, str(exc)


def post(url: str, data: dict, timeout: int = 15) -> tuple[int, str]:
    raw = json.dumps(data).encode()
    req = urllib.request.Request(url, data=raw, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except Exception as exc:
        return -1, str(exc)


def check(label: str, ok: bool, detail: str = "") -> bool:
    status = PASS if ok else FAIL
    print(f"  [{status}] {label}" + (f" -- {detail}" if detail else ""))
    return ok


def run_urllib_checks(base: str) -> int:
    """Basic URL-level checks without Playwright."""
    failures = 0

    # 1. /auth/login loads, shows OAuth buttons
    code, body = get(f"{base}/auth/login")
    ok = code == 200 and ("Google" in body or "GitHub" in body or "Sign in" in body)
    if not check("/auth/login -> 200 + sign-in options", ok, f"code={code}"):
        failures += 1

    # 2. /dashboard without auth -> redirect to /auth/login (not 500)
    code, body = get(f"{base}/dashboard")
    ok = code not in (500, -1)
    if not check("/dashboard without auth -> not 500", ok, f"code={code}"):
        failures += 1

    # 3. /demo loads without auth
    code, body = get(f"{base}/demo")
    ok = code == 200 and "backend-api-v2" in body
    if not check("/demo loads without auth", ok, f"code={code}"):
        failures += 1

    # 4. /pricing loads, shows 3 plan cards (Free, Solo, Team)
    code, body = get(f"{base}/pricing")
    ok = code == 200 and "Free" in body and "Solo" in body and "Team" in body
    if not check("/pricing -> 200 + Free/Solo/Team cards", ok, f"code={code}"):
        failures += 1

    # 5. /waitlist-pending loads (not 500)
    code, body = get(f"{base}/waitlist-pending")
    ok = code not in (500, -1)
    if not check("/waitlist-pending -> not 500", ok, f"code={code}"):
        failures += 1

    # 6. POST /waitlist with real email -> 201 or 409
    test_email = f"test-auth-check-{uuid.uuid4().hex[:8]}@meridian-test.invalid"
    code, body = post(f"{base}/waitlist", {"email": test_email})
    ok = code in (201, 409)
    if not check("POST /waitlist -> 201 or 409", ok, f"code={code}"):
        failures += 1

    # 7. SITE_PASSWORD not set (page loads without password prompt)
    code, body = get(f"{base}/")
    ok = code == 200 and "password" not in body[:500].lower()
    if not check("/ loads without password prompt", ok, f"code={code}"):
        failures += 1

    return failures


def run_playwright_checks(base: str) -> int:
    """Enhanced checks with Playwright headless Chrome."""
    try:
        from playwright.sync_api import sync_playwright  # type: ignore[import]
    except ImportError:
        print(f"  [{SKIP}] Playwright not installed -- run: pip install playwright && playwright install chromium")
        return 0

    failures = 0
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        # Check /auth/login for OAuth buttons
        page.goto(f"{base}/auth/login", wait_until="networkidle", timeout=15000)
        has_google = page.locator("text=Google").count() > 0
        has_github = page.locator("text=GitHub").count() > 0
        ok = has_google or has_github
        if not check("/auth/login has OAuth buttons (Playwright)", ok,
                     f"Google={has_google}, GitHub={has_github}"):
            failures += 1

        # /demo loads demo project
        page.goto(f"{base}/demo", wait_until="networkidle", timeout=15000)
        ok = "backend-api-v2" in page.content()
        if not check("/demo has backend-api-v2 (Playwright)", ok):
            failures += 1

        browser.close()

    return failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="https://usemeridian.us")
    parser.add_argument("--playwright", action="store_true", help="Run Playwright checks too")
    args = parser.parse_args()

    base = args.url.rstrip("/")
    print(f"\nAuth flow tests -- {base}\n")

    failures = run_urllib_checks(base)
    if args.playwright:
        failures += run_playwright_checks(base)

    print(f"\n{'All checks passed.' if failures == 0 else f'{failures} check(s) FAILED.'}\n")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
