#!/usr/bin/env python3
"""Meridian smoke test — 8 checks against a live deployment.

Usage:
    pixi run python scripts/smoke_test.py --url https://meridian-preview.fly.dev
    pixi run python scripts/smoke_test.py --url http://localhost:7878
    pixi run smoke-test-preview   # alias in pixi.toml

Exit code: 0 if all checks pass, 1 if any fail.
Gate: run against meridian-preview.fly.dev before merging to main / tagging prod.
"""

import argparse
import sys
import time
import urllib.request
import urllib.error
import json as _json


def check(name: str, passed: bool, detail: str = "") -> bool:
    status = "PASS" if passed else "FAIL"
    line = f"  [{status}] {name}"
    if detail:
        line += f" — {detail}"
    print(line)
    return passed


def get(url: str, timeout: int = 10) -> tuple[int, str]:
    """Return (status_code, body). Returns (-1, error_msg) on network error."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        return -1, str(exc)


def post(url: str, body: dict, timeout: int = 10) -> tuple[int, str]:
    """POST JSON body. Returns (status_code, body_text)."""
    data = _json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        return -1, str(exc)


def run_checks(base_url: str) -> int:
    base = base_url.rstrip("/")
    results: list[bool] = []

    print(f"\nMeridian smoke test → {base}\n")
    t0 = time.time()

    # 1. Health check
    code, body = get(f"{base}/health")
    results.append(check("GET /health → 200", code == 200, f"got {code}"))

    # 2. Landing page loads
    code, body = get(f"{base}/")
    results.append(check(
        "GET / → 200 landing page",
        code == 200 and "meridian" in body.lower(),
        f"status={code}, has 'meridian'={'meridian' in body.lower()}",
    ))

    # 3. Demo page loads with seeded content
    code, body = get(f"{base}/demo")
    results.append(check(
        "GET /demo → 200 (dashboard HTML)",
        code == 200 and "Meridian Dashboard" in body,
        f"status={code}, has dashboard={'Meridian Dashboard' in body}",
    ))

    # 4. Pricing page
    code, body = get(f"{base}/pricing")
    results.append(check(
        "GET /pricing → 200 with tiers",
        code == 200 and ("free" in body.lower() or "solo" in body.lower()),
        f"status={code}, has tier text={'free' in body.lower()}",
    ))

    # 5. Waitlist endpoint
    code, body = post(f"{base}/waitlist", {"email": "smoke-test@example.com"})
    results.append(check(
        "POST /waitlist → 200 or 201 or 409",
        code in (200, 201, 409),  # 409 = already registered, that's fine
        f"got {code}",
    ))

    # 6. MCP tools doc
    code, body = get(f"{base}/mcp/tools-doc")
    results.append(check(
        "GET /mcp/tools-doc → 200 contains start_session",
        code == 200 and "start_session" in body,
        f"status={code}, has start_session={'start_session' in body}",
    ))

    # 7. hooks/session-start endpoint exists (wrong method → 405, or 200)
    code, body = get(f"{base}/hooks/session-start")
    results.append(check(
        "GET /hooks/session-start → 405 (endpoint exists)",
        code in (405, 422),  # FastAPI returns 405 Method Not Allowed for wrong method
        f"got {code} (expected 405 or 422)",
    ))

    # 8. Config endpoint
    code, body = get(f"{base}/config")
    results.append(check(
        "GET /config → 200 with version field",
        code == 200 and "version" in body,
        f"status={code}, has version={'version' in body}",
    ))

    elapsed = time.time() - t0
    passed = sum(results)
    total = len(results)
    print(f"\n{'='*40}")
    print(f"  {passed}/{total} checks passed in {elapsed:.1f}s")
    if passed == total:
        print("  ALL GREEN — safe to deploy")
    else:
        print(f"  {total - passed} FAILED — fix before deploying to production")
    print(f"{'='*40}\n")
    return 0 if passed == total else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Meridian smoke test")
    parser.add_argument(
        "--url",
        default="http://localhost:7878",
        help="Base URL to test (default: http://localhost:7878)",
    )
    args = parser.parse_args()
    sys.exit(run_checks(args.url))


if __name__ == "__main__":
    main()
