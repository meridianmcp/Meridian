#!/usr/bin/env python3
"""Live API smoke test — 1c615f39.

Hits real prod endpoints (default: https://usemeridian.us).
No mocks. All checks must pass for exit 0.

Usage:
    pixi run python scripts/test_live.py [--url https://usemeridian.us]
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from typing import Any

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
SKIP = "\033[33mSKIP\033[0m"


def get(url: str, timeout: int = 15) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except Exception as exc:
        return -1, str(exc)


def post(url: str, data: dict[str, Any], timeout: int = 15) -> tuple[int, str]:
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
    print(f"  [{status}] {label}" + (f" — {detail}" if detail else ""))
    return ok


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="https://usemeridian.us", help="Base URL to test")
    parser.add_argument("--skip-neon", action="store_true", help="Skip the 6-minute Neon reconnect test")
    args = parser.parse_args()

    base = args.url.rstrip("/")
    failures = 0

    print(f"\nMeridian live API smoke test — {base}\n")

    # 1. GET /health → 200
    code, body = get(f"{base}/health")
    if not check("GET /health → 200", code == 200, f"got {code}"):
        failures += 1

    # 2. GET /demo → 200, contains "backend-api-v2"
    code, body = get(f"{base}/demo")
    ok = code == 200 and "backend-api-v2" in body
    if not check('GET /demo → 200 + "backend-api-v2"', ok, f"got {code}"):
        failures += 1

    # 3. GET /demo/projects → 200, at least 1 project
    code, body = get(f"{base}/demo/projects")
    try:
        projects = json.loads(body)
        ok = code == 200 and len(projects) >= 1
        detail = f"got {len(projects)} projects"
    except Exception:
        ok = False
        detail = f"JSON parse error, code={code}"
    if not check("GET /demo/projects → ≥1 project", ok, detail):
        failures += 1

    # 4. GET /demo/sessions → 200, at least 1 session
    code, body = get(f"{base}/demo/sessions")
    try:
        sessions = json.loads(body)
        ok = code == 200 and len(sessions) >= 1
        detail = f"got {len(sessions)} sessions"
    except Exception:
        ok = False
        detail = f"JSON parse error, code={code}"
    if not check("GET /demo/sessions → ≥1 session", ok, detail):
        failures += 1

    # 5. GET /pricing → 200, contains "Free" and "Solo"
    code, body = get(f"{base}/pricing")
    ok = code == 200 and "Free" in body and "Solo" in body
    if not check('GET /pricing → 200 + "Free" + "Solo"', ok, f"got {code}"):
        failures += 1

    # 6. GET /mcp/quickstart → 200, contains "start_session"
    code, body = get(f"{base}/mcp/quickstart")
    ok = code == 200 and "start_session" in body
    if not check('GET /mcp/quickstart → 200 + "start_session"', ok, f"got {code}"):
        failures += 1

    # 7. POST /waitlist test email → 201 or 409
    test_email = f"test-live-check-{uuid.uuid4().hex[:8]}@meridian-test.invalid"
    code, body = post(f"{base}/waitlist", {"email": test_email})
    ok = code in (201, 409)
    if not check(f"POST /waitlist test email → 201 or 409", ok, f"got {code}"):
        failures += 1

    # 8. GET /auth/login → 200 (not 500)
    code, body = get(f"{base}/auth/login")
    ok = code not in (500, -1)
    if not check("GET /auth/login → not 500", ok, f"got {code}"):
        failures += 1

    # 9. Neon reconnect: hit /health, sleep 6min, hit again
    if args.skip_neon:
        print(f"  [{SKIP}] Neon reconnect (--skip-neon)")
    else:
        print("  [....] Neon reconnect — sleeping 6 minutes (Ctrl+C to skip)...")
        try:
            time.sleep(360)
            code2, body2 = get(f"{base}/health", timeout=30)
            ok = code2 == 200
            if not check("Neon reconnect: /health after 6min idle → 200", ok, f"got {code2}"):
                failures += 1
        except KeyboardInterrupt:
            print(f"  [{SKIP}] Neon reconnect skipped by user")

    # 10. 10x parallel GET /demo
    results: list[int] = []
    threads = [threading.Thread(target=lambda: results.append(get(f"{base}/demo")[0])) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    ok = len(results) == 10 and all(c == 200 for c in results)
    detail = f"{sum(c==200 for c in results)}/10 returned 200"
    if not check("10x parallel GET /demo → all 200", ok, detail):
        failures += 1

    print(f"\n{'All checks passed.' if failures == 0 else f'{failures} check(s) FAILED.'}\n")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
