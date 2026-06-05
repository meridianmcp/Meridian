#!/usr/bin/env python3
"""Live API smoke test -- 1c615f39.

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


# Cloudflare in front of prod blocks the default urllib User-Agent (403),
# so present a browser-like UA on every request.
_UA = "Mozilla/5.0 (Meridian-smoke-test)"


def get(url: str, timeout: int = 15, cookie: str | None = None) -> tuple[int, str]:
    headers = {"User-Agent": _UA}
    if cookie:
        headers["Cookie"] = cookie
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except Exception as exc:
        return -1, str(exc)


def get_demo_cookie(base: str) -> str:
    """Hit /demo to obtain the short-lived cookie that routes API calls to the
    isolated demo DB. Returns the cookie pair (e.g. ``meridian_demo=1``)."""
    try:
        req = urllib.request.Request(f"{base}/demo", headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.headers.get("Set-Cookie", "")
            return raw.split(";")[0] if raw else "meridian_demo=1"
    except Exception:
        return "meridian_demo=1"


def post(url: str, data: dict[str, Any], timeout: int = 15) -> tuple[int, str]:
    raw = json.dumps(data).encode()
    req = urllib.request.Request(url, data=raw, headers={"Content-Type": "application/json", "User-Agent": _UA}, method="POST")
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="https://usemeridian.us", help="Base URL to test")
    parser.add_argument("--skip-neon", action="store_true", help="Skip the 6-minute Neon reconnect test")
    args = parser.parse_args()

    base = args.url.rstrip("/")
    failures = 0

    print(f"\nMeridian live API smoke test -- {base}\n")

    # 1. GET /health -> 200
    code, body = get(f"{base}/health")
    if not check("GET /health -> 200", code == 200, f"got {code}"):
        failures += 1

    # 2. GET /demo -> 200 (dashboard shell; data loads via cookie-routed API)
    code, body = get(f"{base}/demo")
    ok = code == 200 and "dashboard" in body.lower()
    if not check("GET /demo -> 200 (dashboard shell)", ok, f"got {code}"):
        failures += 1

    # The demo dashboard routes its API calls to the isolated demo DB via a
    # short-lived cookie set by /demo. Projects/sessions live on the normal
    # API routes, not /demo/* paths.
    demo_cookie = get_demo_cookie(base)

    # 3. GET /projects (demo cookie) -> 200, at least 1 project
    code, body = get(f"{base}/projects", cookie=demo_cookie)
    first_project_id = None
    try:
        projects = json.loads(body)
        ok = code == 200 and isinstance(projects, list) and len(projects) >= 1
        if ok:
            first_project_id = projects[0].get("id")
        detail = f"got {len(projects) if isinstance(projects, list) else '?'} projects"
    except Exception:
        ok = False
        detail = f"JSON parse error, code={code}"
    if not check("GET /projects (demo) -> >=1 project", ok, detail):
        failures += 1

    # 4. GET /projects/{id}/sessions (demo cookie) -> 200, list returned
    if first_project_id:
        code, body = get(f"{base}/projects/{first_project_id}/sessions", cookie=demo_cookie)
        try:
            sessions = json.loads(body)
            ok = code == 200 and isinstance(sessions, list)
            detail = f"got {len(sessions)} sessions"
        except Exception:
            ok = False
            detail = f"JSON parse error, code={code}"
    else:
        ok = False
        detail = "no project id from previous check"
    if not check("GET /projects/{id}/sessions (demo) -> 200 list", ok, detail):
        failures += 1

    # 5. GET /pricing -> 200, contains "Free" and "Pro"
    code, body = get(f"{base}/pricing")
    ok = code == 200 and "Free" in body and "Pro" in body
    if not check('GET /pricing -> 200 + "Free" + "Pro"', ok, f"got {code}"):
        failures += 1

    # 6. GET /mcp/quickstart -> 200, contains "start_session"
    code, body = get(f"{base}/mcp/quickstart")
    ok = code == 200 and "start_session" in body
    if not check('GET /mcp/quickstart -> 200 + "start_session"', ok, f"got {code}"):
        failures += 1

    # 7. POST /waitlist test email -> 201 or 409
    test_email = f"test-live-check-{uuid.uuid4().hex[:8]}@meridian-test.invalid"
    code, body = post(f"{base}/waitlist", {"email": test_email})
    ok = code in (201, 409)
    if not check(f"POST /waitlist test email -> 201 or 409", ok, f"got {code}"):
        failures += 1

    # 8. GET /auth/login -> 200 (not 500)
    code, body = get(f"{base}/auth/login")
    ok = code not in (500, -1)
    if not check("GET /auth/login -> not 500", ok, f"got {code}"):
        failures += 1

    # 9. Neon reconnect: hit /health, sleep 6min, hit again
    if args.skip_neon:
        print(f"  [{SKIP}] Neon reconnect (--skip-neon)")
    else:
        print("  [....] Neon reconnect -- sleeping 6 minutes (Ctrl+C to skip)...")
        try:
            time.sleep(360)
            code2, body2 = get(f"{base}/health", timeout=30)
            ok = code2 == 200
            if not check("Neon reconnect: /health after 6min idle -> 200", ok, f"got {code2}"):
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
    if not check("10x parallel GET /demo -> all 200", ok, detail):
        failures += 1

    print(f"\n{'All checks passed.' if failures == 0 else f'{failures} check(s) FAILED.'}\n")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
