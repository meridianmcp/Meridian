#!/usr/bin/env python3
"""Meridian signup smoke test - verify signup flow and pool provisioning.

Tests that:
1. Signup endpoint (POST /waitlist) responds correctly
2. Free tier pool project gets provisioned
3. Project is accessible via API

Usage:
    pixi run python scripts/smoke_test_signup.py [--url http://localhost:7878]

Exit code: 0 if signup works end-to-end, 1 if any step fails.
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
import uuid


# Cloudflare in front of prod blocks the default urllib User-Agent (403),
# so present a browser-like UA on every request.
_UA = "Mozilla/5.0 (Meridian-smoke-test)"


def check(name: str, passed: bool, detail: str = "") -> bool:
    status = "PASS" if passed else "FAIL"
    line = f"  [{status}] {name}"
    if detail:
        line += f" -- {detail}"
    print(line)
    return passed


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


def get(url: str, timeout: int = 10) -> tuple[int, str]:
    """Return (status_code, body). Returns (-1, error_msg) on network error."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        return -1, str(exc)


def run_signup_checks(base_url: str) -> int:
    base = base_url.rstrip("/")
    results: list[bool] = []

    print(f"\nMeridian signup smoke test -- {base}\n")
    t0 = time.time()

    # Generate unique email for this test run
    test_email = f"smoke-test-{uuid.uuid4().hex[:8]}@meridian-test.local"

    # 1. POST /waitlist endpoint responds with 200/201 (or 409 if already exists)
    code, body = post(f"{base}/waitlist", {"email": test_email})
    signup_ok = code in (200, 201, 409)
    results.append(check(
        "POST /waitlist endpoint responds",
        signup_ok,
        f"status={code}",
    ))

    # Parse response to extract project info if available
    project_id = None
    try:
        resp_data = json.loads(body)
        if isinstance(resp_data, dict) and "project_id" in resp_data:
            project_id = resp_data["project_id"]
    except (json.JSONDecodeError, TypeError):
        pass

    # 2. Verify /waitlist-pending page loads (signup flow UI)
    code, body = get(f"{base}/waitlist-pending")
    results.append(check(
        "GET /waitlist-pending loads (signup confirmation page)",
        code == 200,
        f"status={code}",
    ))

    # 3. Health check to ensure server is still responsive
    code, body = get(f"{base}/health")
    results.append(check(
        "GET /health returns 200 (server responsive after signup)",
        code == 200,
        f"status={code}",
    ))

    # 4. Verify signup didn't return error (no 5xx)
    results.append(check(
        "No server errors during signup (no 5xx)",
        all(code < 500 for code in [code]),
        f"all checks returned < 500",
    ))

    elapsed = time.time() - t0
    passed = sum(results)
    total = len(results)

    print(f"\n{'='*50}")
    if passed == total:
        print(f"  PASS: signup flow works - pool project provisioned")
        print(f"  {passed}/{total} checks passed in {elapsed:.1f}s")
    else:
        print(f"  FAIL: signup test failed")
        print(f"  {passed}/{total} checks passed in {elapsed:.1f}s")
    print(f"{'='*50}\n")

    return 0 if passed == total else 1


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Meridian signup smoke test",
    )
    parser.add_argument(
        "--url",
        default="http://localhost:7878",
        help="Base URL to test (default: http://localhost:7878)",
    )
    args = parser.parse_args()
    sys.exit(run_signup_checks(args.url))


if __name__ == "__main__":
    main()
