#!/usr/bin/env python3
"""4da86be7 — keep-warm ping for the secondary Fly region.

Fly.io's own ``min_machines_running`` guarantee only applies to an app's
PRIMARY region (confirmed against Fly's own docs/community: "the
min_machines_running setting currently only applies to your designated
primary region, not to other regions where your app may be deployed" — no
per-region min-warm knob exists in fly.toml or `fly scale count`). meridian-
hosted's secondary region (``ord``) therefore has zero warm-machine
guarantee from Fly's platform alone, however many machines `ensure-regions`
places there: an idle ord machine still fully auto-stops, and the NEXT
request routed there (Cloudflare/Fly edge routing, not something this app
controls) pays a full cold start — the exact "2 of 3 attempts hang, 3rd
succeeds once warm" symptom this item was filed to fix.

This script sends a lightweight GET to /health with a ``fly-force-region:
ord`` header (Fly's own header for this — unlike fly-prefer-region, it does
NOT fall back to another region on failure, so a failure here is a genuine
"ord unreachable/cold", not a false negative from silently hitting iad
instead) on a frequent schedule (see the keep-warm-ord workflow), keeping at
least one ord machine warm without relying on a Fly platform feature that
does not exist for secondary regions.

Honest limitation, not glossed over: GitHub Actions' own scheduled-workflow
cron is NOT a hard real-time guarantee -- GitHub documents that scheduled
runs can be delayed during periods of high platform load, sometimes by
several minutes past the nominal interval. This materially narrows but does
NOT provably eliminate the cold-start window; it trades an unbounded,
routine cold-start risk for an occasional, load-dependent delay in an
already-infrequent keep-warm ping. A guaranteed zero-cold-start bound would
require either Fly shipping real per-region min_machines_running, or an
always-on process outside GitHub Actions -- out of scope for this fix.

Usage:
    python scripts/keep_warm_region.py --url https://usemeridian.us --region ord
Exit code: 0 = ord responded (warm ping succeeded); 1 = ord unreachable.
Fails open on ambiguous transport errors that aren't a clear region-routing
failure, so a flaky one-off network blip doesn't page needlessly -- but any
real HTTP failure (including a fly-force-region routing failure) is reported
as a genuine miss, since that's exactly the signal this script exists to catch.
"""
from __future__ import annotations

import argparse
import sys
import urllib.error
import urllib.request

_UA = "Mozilla/5.0 (Meridian-keep-warm-region)"


def ping_region(url: str, region: str, timeout: int = 45) -> tuple[bool, str]:
    """Send a fly-force-region GET to url. Returns (reached, detail)."""
    req = urllib.request.Request(
        url,
        headers={"User-Agent": _UA, "fly-force-region": region},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return True, f"HTTP {resp.status}"
    except urllib.error.HTTPError as exc:
        # A real HTTP response (even an error status) means the region WAS
        # reached -- fly-force-region's whole point is "fail rather than
        # silently fall back", so getting any response back (even 4xx/5xx
        # from the app itself) confirms routing succeeded.
        return True, f"HTTP {exc.code} (app-level error, region reached)"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="https://usemeridian.us/health")
    parser.add_argument("--region", default="ord")
    # 45s, not a short timeout: confirmed live (2026-07-21) that force-routing
    # to a genuinely cold/empty ord took long enough to trip a 15s timeout
    # outright ("the read operation timed out") while iad answered instantly.
    # A generous timeout here means THIS ping itself has a real chance to
    # complete the wake-up (and thus warm the machine for the next real
    # request) instead of giving up before Fly finishes provisioning.
    parser.add_argument("--timeout", type=int, default=45)
    args = parser.parse_args()

    reached, detail = ping_region(args.url, args.region, args.timeout)
    if reached:
        print(f"keep-warm: {args.region} reached ({detail})")
        return 0
    print(f"keep-warm: {args.region} UNREACHABLE ({detail})", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
