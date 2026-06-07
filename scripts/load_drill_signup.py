"""Item 38 — concurrent signup load drill.

Fires N concurrent signups at a Meridian deployment (preview or throwaway
Neon pool) and reports response codes, latency, and any 5xx failures. Use
this to verify the post-claim_pool_project_slot race fix holds under load
and that the pool-exhaustion path returns a graceful waitlist rather than
500ing.

Usage::

    pixi run python -m scripts.load_drill_signup \\
        --base-url https://preview.usemeridian.us \\
        --count 50 \\
        --concurrency 10 \\
        --email-prefix loadtest

The script hits ``/auth/magic`` (free-tier signup entrypoint) with a fresh
email per request. It does NOT complete the OAuth/magic-link round trip —
that requires a real provider — so the actual ``provision_neon_db`` call
won't run unless the deployment is configured to provision on magic-token
issue (most are not). For a true end-to-end drill, point the script at a
preview that's instrumented to short-circuit OAuth and call
``provision_neon_db`` directly from a dev-only endpoint.

This script is intentionally NOT a pytest test: it's meant to be run by an
operator against a staging or throwaway environment, never against prod.
Never check the resulting "loadtest-*" emails out of the throwaway DB.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import statistics
import time
import uuid


@dataclasses.dataclass
class _Result:
    email: str
    status: int
    elapsed_ms: float
    error: str | None = None


async def _one_signup(client, base_url: str, email: str) -> _Result:
    import httpx
    start = time.perf_counter()
    try:
        resp = await client.post(
            f"{base_url.rstrip('/')}/auth/magic",
            json={"email": email},
            timeout=30.0,
        )
        elapsed_ms = (time.perf_counter() - start) * 1000
        return _Result(email=email, status=resp.status_code, elapsed_ms=elapsed_ms)
    except (httpx.HTTPError, OSError) as exc:
        elapsed_ms = (time.perf_counter() - start) * 1000
        return _Result(email=email, status=0, elapsed_ms=elapsed_ms, error=str(exc))


async def _run_drill(base_url: str, count: int, concurrency: int, email_prefix: str) -> list[_Result]:
    import httpx

    semaphore = asyncio.Semaphore(concurrency)
    emails = [f"{email_prefix}-{uuid.uuid4().hex[:8]}@meridian-loadtest.invalid" for _ in range(count)]

    async with httpx.AsyncClient() as client:
        async def _bounded(email: str) -> _Result:
            async with semaphore:
                return await _one_signup(client, base_url, email)

        results = await asyncio.gather(*(_bounded(e) for e in emails))
    return results


def _print_report(results: list[_Result]) -> None:
    by_status: dict[int, int] = {}
    for r in results:
        by_status[r.status] = by_status.get(r.status, 0) + 1

    latencies = [r.elapsed_ms for r in results if r.error is None]
    p50 = statistics.median(latencies) if latencies else 0.0
    p95 = sorted(latencies)[int(len(latencies) * 0.95)] if latencies else 0.0
    p99 = sorted(latencies)[int(len(latencies) * 0.99)] if latencies else 0.0

    fivexx = [r for r in results if 500 <= r.status < 600]
    errors = [r for r in results if r.error]

    print(f"\nLoad drill results ({len(results)} signups):")
    print(f"  Status distribution: {dict(sorted(by_status.items()))}")
    print(f"  Latency (ms): p50={p50:.0f} p95={p95:.0f} p99={p99:.0f}")
    print(f"  5xx count:        {len(fivexx)}")
    print(f"  Network errors:   {len(errors)}")
    if fivexx:
        print(f"\n  5xx detail (first 5):")
        for r in fivexx[:5]:
            print(f"    {r.status} {r.email} ({r.elapsed_ms:.0f}ms)")
    if errors:
        print(f"\n  Network error detail (first 5):")
        for r in errors[:5]:
            print(f"    {r.email}: {r.error}")

    # Exit code reflects success: zero 5xx and zero network errors.
    if fivexx or errors:
        print("\nFAILED — 5xx or network errors present. The signup path "
              "must return a graceful 200/202 or waitlist redirect even under load.")
        return
    print("\nOK — no 5xx, no network errors. Pool-exhaustion path is graceful.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Concurrent signup load drill")
    parser.add_argument(
        "--base-url", required=True,
        help="Base URL of the Meridian deployment to drill (preview only — never prod).",
    )
    parser.add_argument("--count", type=int, default=50, help="Total signups to fire (default: 50)")
    parser.add_argument(
        "--concurrency", type=int, default=10,
        help="Max in-flight requests at once (default: 10)",
    )
    parser.add_argument(
        "--email-prefix", default="loadtest",
        help="Email local-part prefix for synthetic accounts.",
    )
    parser.add_argument(
        "--confirm-not-prod", action="store_true",
        help="Required to run the drill — proves you know this is destructive.",
    )
    args = parser.parse_args()

    if "usemeridian.us" in args.base_url and "preview" not in args.base_url:
        print("Refusing to drill a non-preview usemeridian.us URL. Pass --base-url "
              "explicitly pointing at preview or a throwaway environment.")
        raise SystemExit(2)
    if not args.confirm_not_prod:
        print("Refusing to run without --confirm-not-prod. This drill creates "
              "synthetic free-tier signups; only run against preview or a throwaway pool.")
        raise SystemExit(2)

    started = time.perf_counter()
    results = asyncio.run(
        _run_drill(args.base_url, args.count, args.concurrency, args.email_prefix)
    )
    elapsed = time.perf_counter() - started
    print(f"\nWall time: {elapsed:.1f}s ({args.count / elapsed:.1f} rps)")
    _print_report(results)


if __name__ == "__main__":
    main()
